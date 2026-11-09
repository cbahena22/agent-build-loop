"""Path rules. Pure functions: the path flavour (Windows or POSIX) is a parameter so both are tested everywhere.

`classify` returns one of these kinds: `main`, `worktree`, `git-dir`, `settings`, `outside` (proved to be outside
the repository: it resolved to a local path under none of the known roots) and `unknown` (it could not be classified:
empty or NUL input, a device or volume-GUID path such as `\\\\?\\Volume{...}\\`, a UNC path such as `\\\\localhost\\C$\\`,
a dot-only segment, a stream name before the last segment, an unexpanded 8.3 name, or an OS error while resolving).
Callers must deny `unknown`; it is never a safe default.
"""
import ntpath
import os
import posixpath
import re
import sys
from dataclasses import dataclass

TOPIC_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
SHORT_NAME_RE = re.compile(r"~\d")
RESERVED_TOPICS = frozenset({"main", "person"})
_DEVICE_RE = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_GLOB_CHARS = set("*?[]{}")
OUTSIDE = "outside"
UNKNOWN = "unknown"


class PathRuleError(ValueError):
    """A claim pattern, topic or goal broke a rule; the message names the rule."""


def validate_topic(topic: str) -> str:
    if not isinstance(topic, str) or not TOPIC_RE.match(topic):
        raise PathRuleError("topic must match ^[a-z][a-z0-9-]{1,39}$")
    if topic in RESERVED_TOPICS:
        raise PathRuleError(f"topic {topic!r} is a role name and cannot be used")
    return topic


def validate_goal(goal: str) -> str:
    if not isinstance(goal, str) or not 1 <= len(goal) <= 200:
        raise PathRuleError("goal must be 1-200 characters")
    if _CONTROL_RE.search(goal) or not goal.isprintable():
        raise PathRuleError("goal must be printable text on one line")
    return goal


# ---- claim patterns -------------------------------------------------------------------------------------------


def split_pattern(pattern: str) -> tuple[str, bool]:
    """Return (path, is_folder). `d/` and `d/**` both mean the folder `d`."""
    if pattern.endswith("/**"):
        return pattern[:-3], True
    if pattern.endswith("/"):
        return pattern[:-1], True
    return pattern, False


def validate_pattern(pattern: str) -> str:
    """Check one repo-relative POSIX pattern. Returns it unchanged or raises PathRuleError."""
    if not isinstance(pattern, str) or not pattern:
        raise PathRuleError("empty pattern")
    if _CONTROL_RE.search(pattern):
        raise PathRuleError("control character in pattern")
    if "\\" in pattern:
        raise PathRuleError("backslash in pattern (use /)")
    if ":" in pattern:
        raise PathRuleError("':' in pattern (drive letter or NTFS stream)")
    if pattern.startswith("/") or pattern.startswith("//"):
        raise PathRuleError("absolute path")
    path, _folder = split_pattern(pattern)
    if not path:
        raise PathRuleError("a pattern must name a file or a folder below the repository root")
    if _GLOB_CHARS & set(path):
        raise PathRuleError("wildcards are only allowed as a final /**")
    for part in path.split("/"):
        if part == "":
            raise PathRuleError("empty path segment")
        if part in (".", ".."):
            raise PathRuleError("'.' or '..' segment")
        if part != part.rstrip(". "):
            raise PathRuleError("trailing dot or space in a segment")
        if _DEVICE_RE.match(part):
            raise PathRuleError("reserved device name")
        if SHORT_NAME_RE.search(part):
            raise PathRuleError("8.3 short name (use the long name)")
    first = path.split("/")[0].lower()
    if first in (".git", ".worktrees") or any(p.lower() == ".git" for p in path.split("/")):
        raise PathRuleError("path under .git/ or .worktrees/")
    return pattern


def _norm(pattern: str) -> tuple[str, bool]:
    path, folder = split_pattern(pattern)
    return path.lower(), folder


def patterns_overlap(a: str, b: str) -> bool:
    """True when one pattern's scope contains the other's (case-insensitive: the safe side on every OS)."""
    pa, fa = _norm(a)
    pb, fb = _norm(b)
    if not fa and not fb:
        return pa == pb
    if fa and fb:
        return pa == pb or pa.startswith(pb + "/") or pb.startswith(pa + "/")
    file_p, dir_p = (pa, pb) if not fa else (pb, pa)
    return file_p == dir_p or file_p.startswith(dir_p + "/")


def matches_pattern(pattern: str, relpath: str, *, fold: bool = False) -> bool:
    """Is the repo-relative POSIX `relpath` inside `pattern`?"""
    path, folder = split_pattern(pattern)
    rel = relpath
    if fold:
        path, rel = path.lower(), rel.lower()
    if folder:
        return rel == path or rel.startswith(path + "/")
    return rel == path


# ---- classify -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    kind: str  # "main" | "worktree" | "git-dir" | "settings" | "outside" | "unknown"
    topic: str | None = None
    relpath: str | None = None


def _flavour_module(flavour: str | None):
    if flavour is None:
        return ntpath if sys.platform == "win32" else posixpath
    if flavour in ("nt", "windows"):
        return ntpath
    if flavour == "posix":
        return posixpath
    raise ValueError(f"unknown flavour {flavour!r}")


def strip_extended_prefix(raw: str) -> str:
    """`\\\\?\\C:\\x` -> `C:\\x`; `\\\\?\\UNC\\h\\s` -> `\\\\h\\s`; also the `\\\\.\\` device form."""
    for prefix in ("\\\\?\\UNC\\", "//?/UNC/"):
        if raw[: len(prefix)].upper() == prefix.upper():
            return "\\\\" + raw[len(prefix):]
    for prefix in ("\\\\?\\", "//?/", "\\\\.\\", "//./"):
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return raw


def _expand_short(path: str) -> str:
    """Expand 8.3 names where the OS can (Windows only; elsewhere the path is returned unchanged)."""
    if sys.platform != "win32" or "~" not in path:
        return path
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetLongPathNameW(path, buf, len(buf))  # type: ignore[attr-defined]
        return buf.value if 0 < n < len(buf) else path
    except Exception:  # pragma: no cover - best effort
        return path


def native_resolve(path: str) -> str:
    return _expand_short(os.path.realpath(path))


def _identity_resolve(mod):
    return lambda p: mod.normpath(p)


def _prefix_rel(mod, fold: bool, root: str, path: str) -> str | None:
    """Return the POSIX relative path of `path` under `root`, or None when it is not inside."""
    a = mod.normcase(path) if fold else path
    r = mod.normcase(root) if fold else root
    r = r.rstrip("\\/") if len(r) > 1 else r
    if a == r:
        return ""
    sep = "\\" if mod is ntpath else "/"
    prefix = r if r.endswith(sep) else r + sep
    if mod is ntpath:
        a = a.replace("/", "\\")
        pfx = prefix.replace("/", "\\")
        shown = path.replace("/", "\\")
    else:
        pfx, shown = prefix, path
    if a.startswith(pfx):
        return shown[len(pfx):].replace("\\", "/")
    return None


def classify(raw_path, base_dir, repo_root, worktrees, git_dirs, *, flavour=None, resolve=None) -> Target:
    """Say what `raw_path` is, relative to the repository.

    worktrees: {topic: worktree folder}; git_dirs: folders that count as git internals (git dir, common dir, state store).
    `resolve` follows symlinks and junctions (default: the real file system when `flavour` is the native one,
    else plain normalisation, which is what the table-driven tests use).
    "outside" means proved outside the repository; anything it cannot classify is "unknown" (callers must deny).
    """
    mod = _flavour_module(flavour)
    is_nt = mod is ntpath
    native = mod is (ntpath if sys.platform == "win32" else posixpath)
    if resolve is None:
        resolve = native_resolve if native else _identity_resolve(mod)
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        return Target(UNKNOWN)
    try:
        p = raw_path
        base = base_dir
        if is_nt:
            p, base = strip_extended_prefix(raw_path), strip_extended_prefix(base_dir)
            for original, stripped in ((raw_path, p), (base_dir, base)):
                if stripped != original and not re.match(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\/?.])", stripped):
                    return Target(UNKNOWN)  # a device or volume-GUID path, not a drive or UNC path
        if not mod.isabs(p):
            p = mod.join(base, p)
        stream_ok = None
        if is_nt:
            drive, rest = mod.splitdrive(p)
            segs = []
            for s in re.split(r"[\\/]", rest):
                if s not in (".", "..", "") and not s.strip(". "):
                    return Target(UNKNOWN)  # a segment of only dots and spaces: Windows may read it as '..'
                segs.append(s if s in (".", "..") else s.rstrip(". "))
            p = mod.normpath(drive + "\\".join(segs))
            drive, rest = mod.splitdrive(p)
            parts_ = rest.split("\\")
            if any(":" in s for s in parts_[:-1]):
                return Target(UNKNOWN)  # a stream name can only end the path
            parts_[-1] = parts_[-1].split(":", 1)[0].rstrip(". ")
            p = drive + "\\".join(parts_)
        resolved = resolve(mod.normpath(p))
        if is_nt:
            resolved = strip_extended_prefix(resolved)
        root = resolve(mod.normpath(repo_root))
        wts = {t: resolve(mod.normpath(w)) for t, w in worktrees.items()}
        gds = [resolve(mod.normpath(g)) for g in git_dirs]
    except (OSError, ValueError):
        return Target(UNKNOWN)

    for g in gds:
        if _prefix_rel(mod, is_nt, g, resolved) is not None:
            return Target("git-dir")
    candidates = sorted(wts.items(), key=lambda kv: -len(kv[1]))
    for topic, wt in candidates:
        rel = _prefix_rel(mod, is_nt, wt, resolved)
        if rel is not None:
            return _inside("worktree", topic, rel, is_nt)
    rel = _prefix_rel(mod, is_nt, root, resolved)
    if rel is not None:
        return _inside("main", None, rel, is_nt)
    if is_nt and resolved.replace("/", "\\").startswith("\\\\"):
        return Target(UNKNOWN)  # a UNC path (\\localhost\C$\..., a loopback name or a share) may still reach the repo
    return Target(OUTSIDE)


_SETTINGS_RE = re.compile(r"^\.claude/settings[^/]*\.json$", re.IGNORECASE)


def _inside(kind: str, topic: str | None, rel: str, is_nt: bool) -> Target:
    parts = [s for s in rel.split("/") if s]
    if any(p.lower() == ".git" for p in parts):
        return Target("git-dir")
    if _SETTINGS_RE.match("/".join(parts)):
        return Target("settings", topic, "/".join(parts))
    if is_nt and any(SHORT_NAME_RE.search(p) for p in parts):
        return Target(UNKNOWN)  # an 8.3 name the OS could not expand: do not guess
    return Target(kind, topic, "/".join(parts))
