"""Replace machine paths in output: every spelling of each root, case-insensitive on Windows."""
import os
import re
import sys
import tempfile

_SEP = r"[\\/]+"
_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/]")


def short_form(path: str) -> str | None:
    """The 8.3 short spelling of `path` (Windows only; None elsewhere or when the OS has none)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf))  # type: ignore[attr-defined]
        return buf.value if 0 < n < len(buf) and buf.value != path else None
    except Exception:  # pragma: no cover - best effort
        return None


def _components(path: str) -> tuple[str | None, list[str]]:
    """(drive letter or None, path components) for a Windows- or POSIX-style absolute path."""
    p = path
    if p.startswith(("\\\\?\\", "//?/")):
        p = p[4:]
    m = _DRIVE_RE.match(p)
    if m:
        return m.group(1), [c for c in re.split(r"[\\/]+", p[3:]) if c]
    m2 = re.match(r"^/([A-Za-z])(?:/|$)", p)  # Git Bash form of a drive path (/c/...)
    if m2 and len(p) > 2 and sys.platform == "win32":
        return m2.group(1), [c for c in re.split(r"[\\/]+", p[2:]) if c]
    return None, [c for c in re.split(r"[\\/]+", p) if c]


def pattern_for(path: str, ignore_case: bool) -> re.Pattern | None:
    """A regex matching `path` in its backslash, forward-slash, `\\\\?\\` and `/c/...` spellings."""
    drive, comps = _components(path)
    if not comps:
        return None
    body = "".join(_SEP + re.escape(c) for c in comps)
    if drive:
        d = re.escape(drive)
        head = rf"(?:\\\\\?\\)?(?:{d}:|/{d})"
    else:
        head = ""
    return re.compile(head + body + r"(?![\w~\-])", re.IGNORECASE if ignore_case else 0)


class Masker:
    """roots: (folder, label) pairs, e.g. (demo root, "<demo>"), (home, "~"). Longer paths are replaced first."""

    def __init__(self, roots, *, ignore_case: bool | None = None, resolve: bool = True, short=short_form):
        self.ignore_case = (sys.platform == "win32") if ignore_case is None else ignore_case
        found: list[tuple[int, int, re.Pattern, str]] = []
        for root, label in roots:
            if not root:
                continue
            spellings = {root}
            if resolve:
                spellings.add(os.path.realpath(root))
            for s in list(spellings):
                alt = short(s) if short else None
                if alt:
                    spellings.add(alt)
            for s in spellings:
                pat = pattern_for(s, self.ignore_case)
                if pat is not None:
                    drive, comps = _components(s)
                    found.append((len(comps), len(s), pat, label))
        found.sort(key=lambda t: (-t[0], -t[1]))
        self._rules = [(pat, label) for _n, _l, pat, label in found]

    def text(self, value: str) -> str:
        for pat, label in self._rules:
            value = pat.sub(lambda _m, label=label: label, value)
        return value


def default_masker(*extra_roots) -> Masker:
    """Masker for the usual roots: any extra roots given, the home folder (`~`) and the temp folder."""
    roots = [(r, "<demo>") for r in extra_roots]
    home = os.path.expanduser("~")
    if home and home != "~":
        roots.append((home, "~"))
    roots.append((tempfile.gettempdir(), "<tmp>"))
    return Masker(roots)
