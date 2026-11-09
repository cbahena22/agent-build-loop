"""The only way this package runs git: argv only, hooks and fsmonitor off, a scrubbed environment."""
import atexit
import os
import re
import shutil
import subprocess  # nosec B404
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass

MIN_GIT = (2, 30)
ENV_ALLOWLIST = frozenset(
    {
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_DATE",
    }
)
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_empty_hooks_dir: str | None = None
_default_extra: dict = {}


class GitError(Exception):
    """A git call failed. `str(e)` is a fixed message; the first stderr line is kept apart for masked printing."""

    def __init__(self, subcommand: str, code: int, first_line: str = ""):
        super().__init__(f"git {subcommand} failed (exit {code})")
        self.subcommand = subcommand
        self.code = code
        self.first_line = first_line


@dataclass
class Result:
    code: int
    out: str


def _hooks_dir() -> str:
    global _empty_hooks_dir
    if _empty_hooks_dir is None or not os.path.isdir(_empty_hooks_dir):
        _empty_hooks_dir = tempfile.mkdtemp(prefix="abl-nohooks-")
        atexit.register(shutil.rmtree, _empty_hooks_dir, True)
    return _empty_hooks_dir


@contextmanager
def extra_env(env_extra: dict):
    """Apply allowlisted variables to every git call in this block (the demo pins identity, dates and an empty global config)."""
    global _default_extra
    scrubbed_env(env_extra)  # validate now
    saved = _default_extra
    _default_extra = {**saved, **env_extra}
    try:
        yield
    finally:
        _default_extra = saved


def scrubbed_env(env_extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    for k, v in {**_default_extra, **(env_extra or {})}.items():
        if k not in ENV_ALLOWLIST:
            raise ValueError(f"environment variable {k} is not allowed for git calls")
        env[k] = v
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def safe_ref(ref: str) -> str:
    """A ref or branch name that cannot be read as an option and has no odd characters."""
    if not isinstance(ref, str) or not _REF_RE.match(ref) or ".." in ref or ref.endswith(("/", ".lock")):
        raise ValueError("unsafe ref name")
    return ref


def git(*args: str, cwd, env_extra: dict | None = None, check: bool = True, input_text: str | None = None, timeout: float | None = 120) -> Result:
    """Run `git <args>` in `cwd`. Never uses a shell; stderr is not echoed (a failure gives a fixed message)."""
    if not args:
        raise ValueError("no git subcommand")
    argv = [
        "git",
        "-c", f"core.hooksPath={_hooks_dir()}",
        "-c", "core.fsmonitor=false",
        "-c", "core.quotePath=false",
        *args,
    ]
    proc = subprocess.run(  # nosec B603
        argv,
        cwd=str(cwd),
        env=scrubbed_env(env_extra),
        input=None if input_text is None else input_text.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    out = proc.stdout.decode("utf-8", errors="replace")
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise GitError(args[0], proc.returncode, err[0] if err else "")
    return Result(proc.returncode, out)


def require_git() -> tuple[int, ...]:
    """Return the git version tuple or raise RuntimeError when git is missing or older than MIN_GIT."""
    try:
        proc = subprocess.run(["git", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30)  # nosec B603 B607
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("git was not found on PATH") from exc
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", proc.stdout.decode("utf-8", errors="replace"))
    if not m:
        raise RuntimeError("could not read the git version")
    ver = tuple(int(x) for x in m.groups() if x is not None)
    if ver[:2] < MIN_GIT:
        raise RuntimeError(f"git {MIN_GIT[0]}.{MIN_GIT[1]} or newer is required")
    return ver


# ---- small helpers used across modules --------------------------------------------------------------------------


def common_dir(cwd) -> str:
    return git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd).out.strip()


def git_dir(cwd) -> str:
    return git("rev-parse", "--path-format=absolute", "--git-dir", cwd=cwd).out.strip()


def toplevel(cwd) -> str:
    return git("rev-parse", "--show-toplevel", cwd=cwd).out.strip()


def rev(ref: str, cwd) -> str | None:
    r = git("rev-parse", "--verify", "--quiet", "--end-of-options", f"{safe_ref(ref)}^{{commit}}", cwd=cwd, check=False)
    return r.out.strip() or None if r.code == 0 else None


def blob_sha(ref: str, path: str, cwd) -> str | None:
    """Blob id of `path` at `ref`, or None when absent."""
    r = git("rev-parse", "--verify", "--quiet", "--end-of-options", f"{safe_ref(ref)}:{path}", cwd=cwd, check=False)
    return r.out.strip() if r.code == 0 and r.out.strip() else None


def show_blob(ref: str, path: str, cwd) -> str | None:
    """Text of `path` at `ref`, or None. Plumbing only (`cat-file blob`): no textconv, no filter, no program from config runs."""
    sha = blob_sha(ref, path, cwd)
    if sha is None:
        return None
    return git("cat-file", "blob", sha, cwd=cwd).out


def is_clean_tracked(cwd) -> bool:
    return git("status", "--porcelain", "--untracked-files=no", cwd=cwd).out.strip() == ""


def is_dirty(cwd) -> bool:
    return git("status", "--porcelain", cwd=cwd).out.strip() != ""
