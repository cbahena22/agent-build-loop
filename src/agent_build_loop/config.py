"""Project config: `.abl.toml`, read from the main branch ref, never from `HEAD` or a worker branch."""
import re
import sys
import tomllib
from dataclasses import dataclass, field

from agent_build_loop import gitio
from agent_build_loop.paths import PathRuleError, validate_pattern

DEFAULT_SHARED = ["pyproject.toml", ".abl.toml", ".claude/**", ".gitignore", ".gitattributes"]
_PROGRAM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_KEYS = {"check", "check_timeout_s", "shared", "deny_mcp_for_workers", "handover_keep"}


class ConfigError(Exception):
    """A config problem that names the key (exit 2)."""

    def __init__(self, key: str, message: str):
        super().__init__(f"{key}: {message}")
        self.key = key


@dataclass(frozen=True)
class ProjectConfig:
    check: tuple[str, ...] = ("python", "-m", "unittest", "-q")
    check_timeout_s: int = 600
    shared: tuple[str, ...] = tuple(DEFAULT_SHARED)
    deny_mcp_for_workers: bool = True
    handover_keep: int = 20


def _int(data: dict, key: str, default: int, lo: int, hi: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(key, "must be a whole number")
    if not lo <= value <= hi:
        raise ConfigError(key, f"must be between {lo} and {hi}")
    return value


def parse(text: str) -> ProjectConfig:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(".abl.toml", f"not valid TOML ({exc})") from exc
    for key in data:
        if key not in _KEYS:
            raise ConfigError(key, "unknown key")
    base = ProjectConfig()
    check = data.get("check", list(base.check))
    if not isinstance(check, list) or not check or not all(isinstance(x, str) and x for x in check):
        raise ConfigError("check", "must be a non-empty list of strings (an argv list, not a shell line)")
    if not _PROGRAM_RE.match(check[0]):
        raise ConfigError("check", "the first item must be a bare program name such as python (no path, no spaces)")
    shared = data.get("shared", list(base.shared))
    if not isinstance(shared, list) or not all(isinstance(x, str) for x in shared):
        raise ConfigError("shared", "must be a list of strings")
    for p in shared:
        try:
            validate_pattern(p)
        except PathRuleError as exc:
            raise ConfigError("shared", f"{p!r}: {exc}") from exc
    deny = data.get("deny_mcp_for_workers", base.deny_mcp_for_workers)
    if not isinstance(deny, bool):
        raise ConfigError("deny_mcp_for_workers", "must be true or false")
    return ProjectConfig(
        check=tuple(check),
        check_timeout_s=_int(data, "check_timeout_s", base.check_timeout_s, 1, 86400),
        shared=tuple(shared),
        deny_mcp_for_workers=deny,
        handover_keep=_int(data, "handover_keep", base.handover_keep, 1, 1000),
    )


def load(common_dir, main_branch: str) -> ProjectConfig:
    """Read `.abl.toml` from `refs/heads/<main_branch>` through the common dir. A missing file means defaults."""
    text = gitio.show_blob(f"refs/heads/{main_branch}", ".abl.toml", common_dir)
    return ProjectConfig() if text is None else parse(text)


def check_argv(cfg: ProjectConfig) -> list[str]:
    """The check command as an argv list; a bare `python` is this interpreter."""
    argv = list(cfg.check)
    if argv[0] in ("python", "python3") or (sys.platform == "win32" and argv[0].lower() in ("python.exe", "py")):
        argv[0] = sys.executable
    return argv
