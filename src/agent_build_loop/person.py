"""Person-only commands: they need a terminal and an interactive retype. Claude Code's shell tools have no terminal."""
import sys


class NotAPerson(Exception):
    """stdin is not a terminal, or the retype did not match (exit 1)."""


def is_terminal(stream) -> bool:
    """True only for an interactive terminal.

    On Windows `isatty()` is also True for the NUL device (what `</dev/null` gives a native program), so a real
    console is required there: `GetConsoleMode` succeeds only on a console handle. A Git Bash (mintty) window
    without a pseudo console hands programs a pipe and is refused too (the safe side; use PowerShell or `winpty`).
    """
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty) or not isatty():
        return False
    if sys.platform != "win32":
        return True
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return True  # an in-memory stand-in that says it is a terminal (tests); a real stream always has a fileno
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
        mode = ctypes.c_uint32()
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))  # type: ignore[attr-defined]
    except (OSError, ValueError, AttributeError):
        return False


def require_tty(what: str, stdin=None) -> None:
    """The first gate of a person-only command, checked before any state is read."""
    if not is_terminal(stdin or sys.stdin):
        raise NotAPerson(f"{what} must be run by the person in their own terminal (stdin is not a terminal)")


def require_person(expected: str, what: str, *, stdin=None, stdout=None, input_fn=input) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    require_tty(what, stdin)
    stdout.write(f"{what}\nType {expected!r} to confirm: ")
    stdout.flush()
    try:
        typed = input_fn("")
    except EOFError as exc:
        raise NotAPerson("no confirmation typed") from exc
    if typed.strip() != expected:
        raise NotAPerson("the confirmation did not match; nothing was changed")
