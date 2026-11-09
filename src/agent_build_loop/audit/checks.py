"""Deterministic audit checks. Each BLOCK wins over any reviewer verdict and skips the reviewer.

All git reads use the pinned head sha and plumbing-safe flags (no external diff, no textconv, fixed prefixes and
context), so the diff text is the same on every machine.
"""
import re
import shutil
import subprocess  # nosec B404
import tempfile
from pathlib import Path

from agent_build_loop import config, gitio
from agent_build_loop.paths import matches_pattern

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# key- and secret-shaped added lines; every pattern is built from parts so this file does not match itself
_SECRET_PATTERNS = (
    re.compile("-----BEGIN [A-Z ]*" + "PRIV" + "ATE KEY-----"),
    re.compile(r"\b(?:AK" + r"IA|AS" + r"IA)[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(?:pass" + r"word|sec" + r"ret|tok" + r"en)\s*[:=]\s*[\"'][^\"'\s]{16,}[\"']"),
)
_DIFF_FLAGS = ("--no-color", "--no-ext-diff", "--no-textconv", "-U3", "--inter-hunk-context=0", "--src-prefix=a/",
               "--dst-prefix=b/", "--diff-algorithm=myers", "--indent-heuristic", "--no-relative")
_DIFF_CONFIG = ("-c", "diff.suppressBlankEmpty=false", "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false")


def _range(base: str, head: str) -> str:
    if not (_SHA_RE.match(base) and _SHA_RE.match(head)):
        raise ValueError("audit range needs two full shas")
    return f"{base}...{head}"


def commit_count(repo, base: str, head: str) -> int:
    if not (_SHA_RE.match(base) and _SHA_RE.match(head)):
        raise ValueError("audit range needs two full shas")
    out = gitio.git("rev-list", "--count", f"{base}..{head}", cwd=repo.main_root).out.strip()
    return int(out) if out.isdigit() else 0


def changed_paths(repo, base: str, head: str) -> list[str]:
    """Every path the branch changed, both sides of a rename or copy."""
    out = gitio.git(*_DIFF_CONFIG, "diff", "--name-status", "-z", "-M", *_DIFF_FLAGS, _range(base, head), "--", cwd=repo.main_root).out
    fields = [f for f in out.split("\0")]
    paths: list[str] = []
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i]
        n = 2 if status[:1] in ("R", "C") else 1
        paths += fields[i + 1 : i + 1 + n]
        i += 1 + n
    return sorted(set(paths))


def diff_text(repo, base: str, head: str) -> str:
    return gitio.git(*_DIFF_CONFIG, "diff", "-M", *_DIFF_FLAGS, _range(base, head), "--", cwd=repo.main_root).out


def added_lines(diff: str) -> list[str]:
    return [ln[1:] for ln in diff.replace("\r\n", "\n").split("\n") if ln.startswith("+") and not ln.startswith("+++")]


def secret_lines(diff: str) -> int:
    """How many added lines look like a key or secret (the lines themselves are never printed or stored)."""
    return sum(1 for ln in added_lines(diff) if any(p.search(ln) for p in _SECRET_PATTERNS))


def outside_claim(claim, paths: list[str]) -> list[str]:
    handoff = f"handoff/{claim.topic}.md"
    return [p for p in paths if p != handoff and not any(matches_pattern(o, p) for o in claim.owns)]


def shared_hits(cfg, paths: list[str]) -> list[str]:
    return [p for p in paths if any(matches_pattern(s, p) for s in cfg.shared)]


def handoff_ok(text: str | None, topic: str) -> bool:
    if text is None:
        return False
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    return bool(lines) and lines[-1] == f"READY TO MERGE: {topic}"


def run_check(repo, sha: str) -> str:
    """Run the project's `check` argv on a clean detached checkout of `sha` in a temp folder. "PASS" or "FAIL"
    (a timeout or a missing program is FAIL). The checkout is removed afterwards."""
    if not _SHA_RE.match(sha):
        raise ValueError("check needs a full sha")
    tmp = Path(tempfile.mkdtemp(prefix="abl-check-"))
    tree = tmp / "tree"
    try:
        gitio.git("worktree", "add", "--detach", "--", str(tree), sha, cwd=repo.main_root)
        env = gitio.scrubbed_env()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            proc = subprocess.run(config.check_argv(repo.cfg), cwd=str(tree), env=env, stdin=subprocess.DEVNULL,  # nosec B603
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=repo.cfg.check_timeout_s, check=False)
            return "PASS" if proc.returncode == 0 else "FAIL"
        except (subprocess.TimeoutExpired, OSError):
            return "FAIL"
    finally:
        gitio.git("worktree", "remove", "--force", "--", str(tree), cwd=repo.main_root, check=False)
        shutil.rmtree(tmp, ignore_errors=True)
        gitio.git("worktree", "prune", cwd=repo.main_root, check=False)
