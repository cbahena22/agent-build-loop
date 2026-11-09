"""The PreToolUse guard: `abl hook pre-tool-use --as <role>`.

Reads Claude Code's hook payload (JSON on stdin), prints a deny decision or nothing (the normal permission flow).
With no open claim it allows everything. While any claim is open it fails closed: an error, an unreadable or
oversized payload, an unknown shape or a path it cannot classify is a deny with a fixed reason.
Identity is the `--as` role baked into the settings file the hook was loaded from, never the payload's `cwd`.
"""
import json
import os
import re
import sys
from dataclasses import dataclass

from agent_build_loop import claims as claimlib
from agent_build_loop import events, gitio, worktrees
from agent_build_loop.hooks import shell
from agent_build_loop.paths import TOPIC_RE, UNKNOWN, classify, matches_pattern

MAX_PAYLOAD = 1_000_000
EDITOR_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
SHELL_TOOLS = {"Bash": "bash", "PowerShell": "powershell"}
WORKER_GIT_DENY = {"push", "pull", "fetch", "merge", "rebase", "reset", "switch", "worktree", "config", "update-ref", "symbolic-ref",
                   "tag", "replace", "notes", "filter-branch"}
_BRANCH_CHANGE = {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f", "--force"}
_PYTHON_RE = re.compile(r"^(python[0-9.]*|pythonw|py)$")
_GIT_DIR_RE = re.compile(r"(?:^|[\s\"'=/\\:;(])\.git(?:[\\/]|$|[\s\"';)])")
_PERSON_WORDS = ("approve", "rehash", "remove_hooks", "remove-hooks", "discard", "--set", "require_person")
RECOVERY = "If this is wrong, the person can run `abl init --remove-hooks` in their own terminal."

RULES = {
    "bad-payload": "the hook input is unreadable, too large or not shaped like a Claude Code PreToolUse payload",
    "guard-error": "the guard could not check this call, so it is denied while claims are open",
    "state-unreadable": "the abl state could not be read, so the call is denied while claims may be open",
    "unknown-path": "the path could not be classified (device, UNC, short name or odd form)",
    "git-dir": "files inside the git dir and the abl state store are not edited by any session",
    "settings-file": "Claude Code settings files (project and user level) carry the guard; no session edits them while claims are open",
    "project-config": ".abl.toml is changed only on the main branch with no claims open",
    "no-claim": "this worker has no open claim",
    "claim-escape": "the path is outside this worker's claim",
    "shared-path": "shared paths are edited only by the main session",
    "other-worktree": "the path is in another worker's worktree",
    "main-checkout": "workers edit only inside their own worktree",
    "outside-repo": "workers edit only inside their own worktree",
    "worktree-path": "the main session does not edit files inside .worktrees/",
    "claimed-path": "an open claim owns this path; merge that worker first",
    "mcp-tool": "MCP tools are off for workers (deny_mcp_for_workers)",
    "shell-unparsable": "the command could not be split safely",
    "person-only": "only the person runs this, in their own terminal",
    "worker-abl": "workers may run only abl list, abl verdict and abl handover show",
    "git-config-override": "workers do not pass -c or --config-env to git",
    "git-other-repo": "workers do not point git at another git dir or work tree",
    "git-command": ("workers do not run this git command (push, pull, fetch, merge, rebase, reset, checkout of a branch, switch, worktree, "
                    "config, update-ref, symbolic-ref, branch delete or move, tag, replace, notes, filter-branch)"),
    "git-dir-write": "workers do not touch .git/ by name from the shell",
}


@dataclass(frozen=True)
class Decision:
    allow: bool
    rule: str = ""
    path: str | None = None

    def reason(self) -> str:
        where = f" ({self.path})" if self.path else ""
        return f"abl guard: {self.rule}{where}: {RULES.get(self.rule, self.rule)}. {RECOVERY}"


ALLOW = Decision(True)


def deny(rule: str, path: str | None = None) -> Decision:
    return Decision(False, rule, path)


@dataclass
class Context:
    role: str
    repo: worktrees.Repo
    claims: list
    cwd: str

    @property
    def is_worker(self) -> bool:
        return self.role != "main"

    def claim(self):
        return next((c for c in self.claims if c.topic == self.role), None)


def parse_payload(raw: bytes) -> dict | None:
    """The payload as a dict, or None when it is too large, not UTF-8 JSON, or the wrong shape."""
    if raw is None or len(raw) > MAX_PAYLOAD:
        return None
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("tool_name"), str) or not isinstance(data.get("tool_input"), dict):
        return None
    if data.get("hook_event_name", "PreToolUse") != "PreToolUse":
        return None
    if "cwd" in data and not isinstance(data["cwd"], str):
        return None
    return data


# ---- editor tools ---------------------------------------------------------------------------------------------

_SETTINGS_NAME_RE = re.compile(r"^settings[^/\\]*\.json$", re.IGNORECASE)


def user_settings_dirs() -> list[str]:
    """Folders whose `settings*.json` Claude Code reads for every project: `~/.claude` and `$CLAUDE_CONFIG_DIR`."""
    dirs = []
    for home in {os.path.expanduser("~"), os.environ.get("USERPROFILE", ""), os.environ.get("HOME", "")}:
        if home and home != "~":
            dirs.append(os.path.join(home, ".claude"))
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        dirs.append(os.environ["CLAUDE_CONFIG_DIR"])
    return dirs


def is_user_settings(raw_path: str, cwd: str) -> bool:
    """A user-level Claude Code settings file (it could switch every hook off, e.g. `disableAllHooks`)."""
    try:
        p = os.path.realpath(os.path.join(cwd, raw_path))
    except (OSError, ValueError):
        return True
    if not _SETTINGS_NAME_RE.match(os.path.basename(p)):
        return False
    parent = os.path.normcase(os.path.dirname(p))
    return any(parent == os.path.normcase(os.path.realpath(d)) for d in user_settings_dirs())


def _editor(ctx: Context, raw_path) -> Decision:
    if not isinstance(raw_path, str) or not raw_path:
        return deny("bad-payload")
    repo = ctx.repo
    wts = {c.topic: str(repo.main_root / c.worktree) for c in ctx.claims}
    target = classify(raw_path, ctx.cwd, str(repo.main_root), wts, [str(repo.common_dir)])
    rel = target.relpath
    if target.kind == UNKNOWN:
        return deny("unknown-path")
    if target.kind == "outside" and is_user_settings(raw_path, ctx.cwd):
        return deny("settings-file")
    if target.kind == "git-dir":
        return deny("git-dir")
    if target.kind == "settings":
        return deny("settings-file", rel)
    fold = sys.platform == "win32"
    if rel is not None and rel.lower() == ".abl.toml":
        return deny("project-config", rel)
    if ctx.is_worker:
        claim = ctx.claim()
        if claim is None:
            return deny("no-claim", rel)
        if target.kind == "outside":
            return deny("outside-repo")
        if target.kind == "main":
            return deny("main-checkout", rel)
        if target.topic != ctx.role:
            return deny("other-worktree", rel)
        if rel == f"handoff/{ctx.role}.md" or (fold and rel.lower() == f"handoff/{ctx.role}.md"):
            return ALLOW
        if any(matches_pattern(s, rel, fold=fold) for s in repo.cfg.shared):
            return deny("shared-path", rel)
        if claimlib.owns(claim, rel, fold=fold):
            return ALLOW
        return deny("claim-escape", rel)
    # main
    if target.kind == "worktree" or (rel is not None and rel.lower().split("/")[0] == ".worktrees"):
        return deny("worktree-path", rel)
    if target.kind == "main" and any(claimlib.owns(c, rel, fold=fold) for c in ctx.claims):
        return deny("claimed-path", rel)
    return ALLOW


# ---- shell tools ----------------------------------------------------------------------------------------------


def abl_args(argv: list[str]) -> list[str] | None:
    """The arguments after `abl`, `abl.exe`, `python -m agent_build_loop` or `py -m agent_build_loop`; None otherwise."""
    if not argv:
        return None
    prog = shell.program(argv[0])
    if prog == "abl":
        return argv[1:]
    if _PYTHON_RE.match(prog):
        for i, tok in enumerate(argv[1:], start=1):
            if tok == "-m" and i + 1 < len(argv) and argv[i + 1].startswith("agent_build_loop"):
                return argv[i + 2 :]
            if tok.startswith("-m") and tok[2:].startswith("agent_build_loop"):
                return argv[i + 1 :]
    return None


def _words(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def is_person_only(args: list[str]) -> bool:
    words = _words(args)
    sub = words[0] if words else ""
    if sub == "approve":
        return True
    if sub == "init":
        return any(a.startswith("-") and a not in ("--dry-run", "-h", "--help") for a in args)
    if sub == "end":
        return any(a.startswith("--d") for a in args)
    return False


def _python_code_is_person_only(argv: list[str]) -> bool:
    if not _PYTHON_RE.match(shell.program(argv[0])):
        return False
    for i, tok in enumerate(argv[1:], start=1):
        if tok == "-c" and i + 1 < len(argv):
            code = argv[i + 1]
            return "agent_build_loop" in code and any(w in code for w in _PERSON_WORDS)
    return False


def git_rule(args: list[str]) -> str | None:
    """The rule a worker's `git <args>` breaks, or None."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-c" or a.startswith("--config-env"):
            return "git-config-override"
        if a.split("=", 1)[0] in ("--git-dir", "--work-tree", "--namespace", "--exec-path"):
            return "git-other-repo"
        if a == "-C":
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        break
    sub = args[i].lower() if i < len(args) else ""
    rest = args[i + 1 :]
    if sub in WORKER_GIT_DENY:
        return "git-command"
    if sub == "checkout" and (not rest or rest[0] != "--"):
        return "git-command"
    if sub == "branch" and any(t in _BRANCH_CHANGE or t.startswith("--delete") or t.startswith("--move") for t in rest):
        return "git-command"
    return None


def _shell(ctx: Context, text, flavour: str) -> Decision:
    if not isinstance(text, str):
        return deny("bad-payload")
    try:
        cmds = shell.commands(text, flavour)
    except shell.ShellParseError:
        return deny("shell-unparsable")
    for argv in cmds:
        # any position, so an unknown wrapper (`foo --x abl approve t`) still shows the person-only form
        if any(is_person_only(rest) for rest in (abl_args(argv[i:]) for i in range(len(argv))) if rest is not None):
            return deny("person-only")
        args = abl_args(argv)
        if args is not None:
            if ctx.is_worker:
                words = _words(args)
                if not (words[:1] in (["list"], ["verdict"]) or words[:2] == ["handover", "show"]):
                    return deny("worker-abl")
        if _python_code_is_person_only(argv):
            return deny("person-only")
        if ctx.is_worker and shell.program(argv[0]) == "git":
            rule = git_rule(argv[1:])
            if rule:
                return deny(rule)
    if ctx.is_worker and _GIT_DIR_RE.search(text):
        return deny("git-dir-write")
    return ALLOW


def decide(ctx: Context, payload: dict) -> Decision:
    tool = payload["tool_name"]
    tin = payload["tool_input"]
    if tool in EDITOR_TOOLS:
        return _editor(ctx, tin.get(EDITOR_TOOLS[tool]))
    if tool in SHELL_TOOLS:
        return _shell(ctx, tin.get("command"), SHELL_TOOLS[tool])
    if tool.startswith("mcp__"):
        if ctx.is_worker and ctx.repo.cfg.deny_mcp_for_workers:
            return deny("mcp-tool")
        return ALLOW
    return ALLOW


# ---- entry point ----------------------------------------------------------------------------------------------


def _valid_role(role: str | None) -> bool:
    return role is None or role == "main" or bool(isinstance(role, str) and TOPIC_RE.match(role))


def load_context(role_arg: str | None, payload_cwd: str | None) -> Context:
    """Find the repository from `CLAUDE_PROJECT_DIR`, then the payload `cwd`, then the process folder."""
    base = os.environ.get("CLAUDE_PROJECT_DIR") or payload_cwd or os.getcwd()
    repo = worktrees.open_repo(base)
    role = role_arg or worktrees.whoami(base, repo)
    return Context(role=role, repo=repo, claims=repo.store.load_claims(), cwd=payload_cwd or base)


def _log(ctx: Context, payload: dict | None, d: Decision) -> None:
    tool = payload.get("tool_name") if payload else None
    detail = {"tool": tool if isinstance(tool, str) else None, "rule": d.rule, "path": d.path}
    cmd = payload.get("tool_input", {}).get("command") if payload else None
    topic = ctx.role if ctx.is_worker else None
    try:
        if isinstance(cmd, str):
            try:
                events.append(ctx.repo.store, ctx.role, "guard-deny", topic, {**detail, "command": cmd[:200]})
                return
            except events.EventError:
                pass  # the command text held an absolute path: log without it
        events.append(ctx.repo.store, ctx.role, "guard-deny", topic, detail)
    except Exception:  # noqa: BLE001 # nosec B110 - logging never changes the decision (the call is still denied)
        pass


def run(role_arg: str | None, raw: bytes | None) -> Decision:
    """The decision for one payload (no output). Fails closed while any claim is open."""
    if not _valid_role(role_arg):
        return deny("bad-payload")
    payload = parse_payload(raw) if raw is not None else None
    cwd = payload.get("cwd") if payload else None
    try:
        ctx = load_context(role_arg, cwd)
    except gitio.GitError:
        # not inside a git repository: nothing here can be claimed, unless this hook belongs to a worker
        return ALLOW if role_arg in (None, "main") else deny("state-unreadable")
    except Exception:  # noqa: BLE001 - fail closed
        return deny("state-unreadable")
    if not ctx.claims:
        return ALLOW
    if payload is None:
        d = deny("bad-payload")
    else:
        try:
            d = decide(ctx, payload)
        except Exception:  # noqa: BLE001 - fail closed
            d = deny("guard-error")
    if not d.allow:
        _log(ctx, payload, d)
    return d


def output(d: Decision) -> str:
    """What the hook prints: a deny object (ASCII JSON) or nothing."""
    if d.allow:
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": d.reason()}}, ensure_ascii=True)


def hook_main(role_arg: str | None, stdin=None, stdout=None) -> int:
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout
    try:
        raw = stdin.read(MAX_PAYLOAD + 1)
    except Exception:  # noqa: BLE001 - unreadable stdin
        raw = None
    try:
        d = run(role_arg, raw)
    except Exception:  # noqa: BLE001 - the last line of fail closed
        d = deny("guard-error")
    text = output(d)
    if text:
        stdout.write(text + "\n")
        stdout.flush()
    return 0
