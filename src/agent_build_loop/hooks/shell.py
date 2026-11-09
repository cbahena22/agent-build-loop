"""Best-effort shell parsing for the guard: split a Bash or PowerShell line into simple commands (argv lists).

This is defence in depth, not a sandbox. It understands quotes, `;`, `&&`, `||`, `|`, `&`, newlines, `( )`,
`$(...)`, Bash backticks, PowerShell's backtick escape, and nested `bash -c`, `sh -c`, `cmd /c`,
`powershell -Command`, `pwsh -c`. Anything it cannot split raises ShellParseError (the guard then denies).
"""
import re

MAX_DEPTH = 5
_SEPARATORS = set(";&|\n\r(){}")
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_POWERSHELLS = {"powershell", "pwsh"}
_PREFIXES = {"env", "command", "exec", "sudo", "nohup", "time", "nice", "builtin", "call", "start", "invoke-command",
             "unbuffer", "winpty", "stdbuf", "spawn", "uvx", "xargs"}
_RUNNERS = {"uv", "pipx", "poetry", "pdm", "hatch"}  # `uv run cmd`
_PREFIXES_WITH_ARG = {"timeout"}  # `timeout 5 cmd`: one value before the program
_SCRIPT_RUNNERS = {"script", "expect"}  # `script -q -c "cmd" /dev/null`, `expect -c "spawn cmd"`: a command string
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


class ShellParseError(ValueError):
    pass


class _Cmd:
    """The result of one split: argv lists plus nested command texts found in `$(...)` or backticks."""

    def __init__(self):
        self.commands: list[list[str]] = []
        self.nested: list[str] = []


def _find_close_paren(text: str, i: int) -> int:
    """`text[i]` is just after `$(`; return the index of the matching `)`."""
    depth = 1
    quote = None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == '"':
                i += 1
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ShellParseError("unbalanced $(")


def _split(text: str, flavour: str) -> _Cmd:
    ps = flavour == "powershell"
    esc = "`" if ps else "\\"
    out = _Cmd()
    seg: list[str] = []
    cur: list[str] | None = None

    def end_token():
        nonlocal cur
        if cur is not None:
            seg.append("".join(cur))
            cur = None

    def end_segment():
        nonlocal seg
        end_token()
        if seg:
            out.commands.append(seg)
        seg = []

    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t":
            end_token()
        elif ch in _SEPARATORS:
            end_segment()
        elif ch in "<>":
            end_token()
            seg.append(ch)
        elif ch == esc:
            if i + 1 < n:
                if text[i + 1] == "\n":
                    i += 2
                    continue
                cur = (cur or []) + [text[i + 1]]
                i += 1
            else:
                cur = (cur or []) + [ch]
        elif ch == "'":
            j = i + 1
            buf = []
            while True:
                if j >= n:
                    raise ShellParseError("unclosed single quote")
                if text[j] == "'":
                    if ps and j + 1 < n and text[j + 1] == "'":  # PowerShell '' inside '...'
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            cur = (cur or []) + buf
            i = j
        elif ch == '"':
            j = i + 1
            buf = []
            while True:
                if j >= n:
                    raise ShellParseError("unclosed double quote")
                c = text[j]
                if c == '"':
                    break
                if c == esc and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                if c == "$" and j + 1 < n and text[j + 1] == "(":
                    k = _find_close_paren(text, j + 2)
                    out.nested.append(text[j + 2 : k])
                    buf.append("$(...)")
                    j = k + 1
                    continue
                if not ps and c == "`":
                    k = text.find("`", j + 1)
                    if k < 0:
                        raise ShellParseError("unclosed backtick")
                    out.nested.append(text[j + 1 : k])
                    j = k + 1
                    continue
                buf.append(c)
                j += 1
            cur = (cur or []) + buf
            i = j
        elif ch == "$" and i + 1 < n and text[i + 1] == "(":
            k = _find_close_paren(text, i + 2)
            out.nested.append(text[i + 2 : k])
            cur = (cur or []) + list("$(...)")
            i = k
        elif not ps and ch == "`":
            k = text.find("`", i + 1)
            if k < 0:
                raise ShellParseError("unclosed backtick")
            out.nested.append(text[i + 1 : k])
            i = k
        else:
            cur = (cur or []) + [ch]
        i += 1
    end_segment()
    return out


def program(token: str) -> str:
    """Lower-case base name without folder or Windows extension: `C:/x/Python.EXE` -> `python`."""
    base = token.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".com", ".ps1"):
        if base.endswith(ext):
            return base[: -len(ext)]
    return base


def strip_prefixes(argv: list[str]) -> list[str]:
    """Drop `VAR=x`, `env -i`, `sudo`, `&` and similar wrappers in front of the real program."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        prog = program(tok)
        runner = prog in _RUNNERS and argv[i + 1 : i + 2] == ["run"]
        if _ASSIGN_RE.match(tok) or tok in ("&", ".", "!"):
            i += 1
        elif prog in _PREFIXES or prog in _PREFIXES_WITH_ARG or runner:
            i += 2 if runner else 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 1
            if prog in _PREFIXES_WITH_ARG and i < len(argv):
                i += 1
        else:
            break
    return argv[i:]


def _nested_script(argv: list[str]) -> tuple[str, str] | None:
    """(flavour, script) when argv runs another shell on a command string."""
    if not argv:
        return None
    prog = program(argv[0])
    rest = argv[1:]
    if prog in _SCRIPT_RUNNERS:
        for i, tok in enumerate(rest):
            if tok.startswith("--command="):
                return "bash", tok.split("=", 1)[1]
            if tok == "--command" or (tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]):
                return ("bash", rest[i + 1]) if i + 1 < len(rest) else ("bash", "")
        return None
    if prog in _SHELLS:
        for i, tok in enumerate(rest):
            if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
                return ("bash", rest[i + 1]) if i + 1 < len(rest) else ("bash", "")
        return None
    if prog == "cmd":
        for i, tok in enumerate(rest):
            if tok.lower() in ("/c", "/k", "/r"):
                return "powershell", " ".join(rest[i + 1 :])
        return None
    if prog in _POWERSHELLS:
        for i, tok in enumerate(rest):
            low = tok.lower()
            if low.startswith("-") and len(low) > 1 and "-command".startswith(low) and low not in ("-",):
                return "powershell", " ".join(rest[i + 1 :])
            if low == "-ec" or (len(low) >= 2 and "-encodedcommand".startswith(low)):
                raise ShellParseError("an encoded command cannot be checked")
        return None
    return None


def commands(text: str, flavour: str = "bash", depth: int = 0) -> list[list[str]]:
    """Every simple command in `text`, nested shells and substitutions included, wrappers stripped."""
    if depth > MAX_DEPTH:
        raise ShellParseError("commands nested too deep")
    parsed = _split(text, flavour)
    found: list[list[str]] = []
    for argv in parsed.commands:
        argv = strip_prefixes(argv)
        if not argv:
            continue
        found.append(argv)
        nested = _nested_script(argv)
        if nested is not None:
            found += commands(nested[1], nested[0], depth + 1)
    for inner in parsed.nested:
        found += commands(inner, flavour, depth + 1)
    return found
