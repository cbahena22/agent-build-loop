"""`abl demo`: the scripted end-to-end check. Two fake workers on a temp copy of the example project.

Every tool call from a worker transcript goes through the real guard as a subprocess, with a hook payload on stdin
exactly as Claude Code sends it. When the guard allows a call, the demo applies the call's declared effect (a file
write or edit, or a fixed `git add` + `git commit`); it never runs command text from a transcript. Git identity,
dates and config are pinned, so every sha is the same on every run and machine. No model call: the reviewer is a
replay of hand-written, synthetic replies.
"""
import importlib.resources
import json
import os
import secrets
import shutil
import stat
import subprocess  # nosec B404
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from agent_build_loop import approve as approvelib
from agent_build_loop import gitio, handover, init, mask, worktrees
from agent_build_loop import merge as mergelib
from agent_build_loop.audit import run as auditrun
from agent_build_loop.audit.reviewer import ReplayReviewer
from agent_build_loop.paths import validate_pattern

IDENTITY = ("Demo Person", "demo@example.invalid")
MARKER = "abl-demo.marker"
A, B = "add-summary", "rename-total"
GOALS = {A: "add a quote summary", B: "rename total to quote_total"}
OWNS = {A: ["print_quotes/summary.py", "tests/test_summary.py"], B: ["print_quotes/totals.py", "tests/test_totals.py"]}
TRANSCRIPT_KIND = "abl-worker-transcript"


class DemoError(Exception):
    pass


@dataclass
class Tally:
    denied: int = 0
    audits: list = field(default_factory=list)
    clean: int = 0
    rolled_back: int = 0

    def last_line(self) -> str:
        block = sum(v == "BLOCK" for v in self.audits)
        passed = sum(v in ("PASS", "PASS-WITH-NOTES") for v in self.audits)
        return (f"guard: {self.denied} denied | audits: {block} BLOCK, {passed} PASS | "
                f"merges: {self.clean} clean, {self.rolled_back} rolled back | model calls: 0 ($0.00)")


def data_file(*parts: str):
    return importlib.resources.files("agent_build_loop").joinpath("data", *parts)


def git_env(step: int, global_config: Path) -> dict:
    stamp = f"2026-01-01T00:00:{step:02d}+0000"
    return {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_AUTHOR_NAME": IDENTITY[0], "GIT_AUTHOR_EMAIL": IDENTITY[1], "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_NAME": IDENTITY[0], "GIT_COMMITTER_EMAIL": IDENTITY[1], "GIT_COMMITTER_DATE": stamp}


def load_transcript(topic: str) -> list[dict]:
    text = data_file("transcripts", f"{topic}.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    head = rows[0] if rows else {}
    if head.get("kind") != TRANSCRIPT_KIND or head.get("synthetic") is not True or head.get("topic") != topic:
        raise DemoError(f"transcript {topic} has no synthetic header")
    for row in rows[1:]:
        if set(row) - {"phase", "tool_name", "tool_input", "effect"} or not isinstance(row.get("tool_input"), dict):
            raise DemoError(f"transcript {topic} has an unexpected row")
    return rows[1:]


def _rel(path: str) -> str:
    validate_pattern(path)  # repo-relative, no '..', no drive, no stream
    return path


def _rmtree(path: Path) -> None:
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.chmod(os.path.join(root, name), stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


class Demo:
    def __init__(self, base: Path, say, reviewer=None):
        self.base = base
        self.root = base / "print-quotes"
        self.say = say
        self.tally = Tally()
        self.step = 0
        self.global_config = base / "empty-global-gitconfig"
        self.nonce = secrets.token_hex(16)
        self.reviewer = reviewer or ReplayReviewer(data_file("transcripts", "reviews.jsonl"))
        self.repo: worktrees.Repo | None = None

    # -- helpers
    def at_next_step(self):
        self.step += 1
        return gitio.extra_env(git_env(self.step, self.global_config))

    def cmd(self, text: str, who: str = "main session") -> None:
        self.say("")
        self.say(f"PS> {text}   ({who})")

    def reload(self) -> worktrees.Repo:
        self.repo = worktrees.open_repo(self.root)
        return self.repo

    def main_sha(self) -> str:
        return gitio.rev("refs/heads/main", self.root)

    # -- setup
    def setup(self) -> None:
        self.global_config.write_text("", encoding="utf-8")
        (self.base / MARKER).write_text(self.nonce, encoding="utf-8")
        shutil.copytree(str(data_file("example", "print-quotes")), str(self.root), ignore=shutil.ignore_patterns("__pycache__"))
        with self.at_next_step():
            gitio.git("init", "-q", "-b", "main", cwd=self.root)
            for k, v in (("user.name", IDENTITY[0]), ("user.email", IDENTITY[1]), ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
                gitio.git("config", k, v, cwd=self.root)
            gitio.git("add", "-A", cwd=self.root)
            gitio.git("commit", "-q", "-m", "Start print-quotes", cwd=self.root)
        self.say(f"Kestrel Print Co. (fictional): two workers on print-quotes; main starts at {self.main_sha()[:8]}")
        self.cmd("abl init")
        with self.at_next_step():
            for line in init.setup(self.reload()):
                self.say(f"  {line}")

    def open_worker(self, topic: str) -> None:
        self.cmd(f"abl new {topic} --owns {' '.join(OWNS[topic])} --goal \"{GOALS[topic]}\"")
        with self.at_next_step():
            claim, hint = worktrees.new(self.reload(), topic, OWNS[topic], GOALS[topic])
        self.say(f"  opened {claim.topic} on {claim.branch}; the person runs: {hint}")

    # -- workers
    def guard(self, topic: str, tool_name: str, tool_input: dict) -> str | None:
        """Run the real hook as Claude Code would. Returns the deny reason, or None when allowed."""
        cwd = self.root / ".worktrees" / topic
        data = {"session_id": f"demo-{topic}", "transcript_path": f"{topic}.jsonl", "cwd": str(cwd), "permission_mode": "default",
                "hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": tool_input, "tool_use_id": f"toolu_demo_{self.step}"}
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_") and k != "CLAUDE_PROJECT_DIR"}
        proc = subprocess.run([sys.executable, "-m", "agent_build_loop", "hook", "pre-tool-use", "--as", topic],  # nosec B603
                              input=json.dumps(data).encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              cwd=str(cwd), env=env, timeout=120, check=False)
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            raise DemoError("the guard hook failed to run")
        if not out:
            return None
        decision = json.loads(out)["hookSpecificOutput"]
        if decision.get("permissionDecision") != "deny":
            return None
        return str(decision.get("permissionDecisionReason", "denied"))

    def apply(self, topic: str, row: dict) -> None:
        wt = self.root / ".worktrees" / topic
        tool, tin = row["tool_name"], dict(row["tool_input"])
        effect = row.get("effect")
        if tool in ("Write", "Edit"):
            rel = _rel(tin["file_path"])
            tin["file_path"] = str(wt / rel)
            shown = f"{tool} {rel}"
        else:
            shown = f"{tool}: {tin.get('command', '')}"
        self.say(f"  {topic} -> {shown}")
        reason = self.guard(topic, tool, tin)
        if reason is not None:
            self.tally.denied += 1
            self.say(f"    guard DENY: {reason.split('. ')[0]}")
            return
        if tool == "Write":
            p = wt / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(tin["content"], encoding="utf-8", newline="\n")
        elif tool == "Edit":
            self._edit(wt / rel, tin["old_string"], tin["new_string"])
        elif effect and "edit" in effect:
            e = effect["edit"]
            self._edit(wt / _rel(e["path"]), e["old"], e["new"])
            self.say(f"    allowed (the shell guard does not see this write); effect: edits {e['path']}")
        elif effect and "commit" in effect:
            with self.at_next_step():
                gitio.git("add", "-A", cwd=wt)
                gitio.git("commit", "-q", "-m", str(effect["commit"]), cwd=wt)
            self.say(f"    committed {gitio.rev(f'refs/heads/w/{topic}', self.root)[:8]}")
        else:
            raise DemoError("a transcript row has no declared effect")

    @staticmethod
    def _edit(path: Path, old: str, new: str) -> None:
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            raise DemoError("an edit in the transcript does not match the file once")
        path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")

    def play(self, topic: str, phase: int) -> None:
        self.say("")
        self.say(f"[worker {topic}, a Claude Code session in .worktrees/{topic} (played from a synthetic transcript)]")
        for row in load_transcript(topic):
            if row.get("phase", 1) == phase:
                self.apply(topic, row)

    # -- main session and the person
    def audit(self, topic: str) -> str:
        self.cmd(f"abl audit {topic} --reviewer replay")
        rec = auditrun.audit(self.reload(), topic, self.reviewer)
        self.tally.audits.append(rec["verdict"])
        blocked = [c for c, s in rec["checks"] if s == "block"]
        self.say(f"  audit {topic} at {rec['head'][:8]}: {rec['verdict']}" + (f" ({', '.join(blocked)})" if blocked else ""))
        for f in rec["findings"]:
            self.say(f"    finding: {f['path']}: {f['reason']}")
        return rec["verdict"]

    def person_approves(self, topic: str) -> None:
        """The person's step, scripted. Accepts only this demo's own folder (the marker holds this run's nonce)."""
        if self.root.parent != self.base or (self.base / MARKER).read_text(encoding="utf-8") != self.nonce:
            raise DemoError("the scripted approval runs only inside the demo's own folder")
        head = gitio.rev(f"refs/heads/w/{topic}", self.root)
        self.cmd(f"abl approve {topic}", "the person, at a terminal")
        self.say(f"  typed {head[:8]} to confirm")
        self.say(f"  {approvelib.approve(self.reload(), topic, confirm=lambda expected, what: None)}")

    def merge(self, topic: str) -> None:
        self.cmd(f"abl merge {topic}")
        before = self.main_sha()
        try:
            with self.at_next_step():
                result = mergelib.merge(self.reload(), topic)
        except mergelib.MergeRefused as exc:
            self.say(f"  refused: {exc}")
            return
        for line in result.message.splitlines():
            self.say(f"  {line}")
        if result.code == 0:
            self.tally.clean += 1
        elif "rolled back" in result.message:
            self.tally.rolled_back += 1
            self.say(f"  main before {before[:8]}, after {self.main_sha()[:8]}")

    def run(self) -> str:
        self.global_config.write_text("", encoding="utf-8")
        with gitio.extra_env(git_env(0, self.global_config)):  # every git call: no system or personal config
            return self._run()

    def _run(self) -> str:
        self.setup()
        self.open_worker(A)
        self.open_worker(B)
        self.play(A, 1)
        self.play(B, 1)
        if self.audit(A) != "BLOCK":
            self.person_approves(A)
        self.merge(A)
        self.audit(B)
        self.merge(B)
        self.play(B, 2)
        if self.audit(B) != "BLOCK":
            self.person_approves(B)
        self.merge(B)
        waiting = f"{B}: run abl claim {B} --add print_quotes/summary.py tests/test_summary.py, then update the caller or keep total() as an alias"
        self.cmd(f'abl handover write --waiting "{waiting}"')
        path = handover.write(self.reload(), [], [waiting], utc=f"2026-01-01T00:00:{self.step + 1:02d}Z")
        self.say(f"  wrote handover {path.name}")
        self.cmd("abl handover show")
        for line in (handover.newest(self.repo.store) or "").splitlines():
            self.say(f"  {line}")
        final = self.main_sha()
        self.say("")
        self.say(f"main: {final}")
        return final


def run(keep: bool = False, say=None, reviewer=None) -> tuple[str, str]:
    """Run the demo. Returns (last line, final main sha). Prints through the masker (the folder shows as <demo>)."""
    base = Path(tempfile.mkdtemp(prefix="abl demo "))
    masker = mask.default_masker(str(base))
    lines: list[str] = []

    def emit(text: str) -> None:
        line = masker.text(text)
        lines.append(line)
        if say is not None:
            say(line)

    try:
        if gitio.git("rev-parse", "--show-toplevel", cwd=base, check=False).code == 0:
            raise DemoError("the temp folder is inside a git work tree; set TEMP or TMPDIR to a folder outside one")
        demo = Demo(base, emit, reviewer)
        final = demo.run()
        last = demo.tally.last_line()
        if keep:
            emit("kept the demo folder <demo> (a folder named 'abl demo ...' in your temp folder)")
        emit(last)
        return last, final
    finally:
        if not keep:
            _rmtree(base)
