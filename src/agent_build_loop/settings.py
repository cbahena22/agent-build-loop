"""Write the Claude Code hook entries (`.claude/settings.local.json`) that carry a session's identity."""
import json
import re
import sys
from pathlib import Path

from agent_build_loop.paths import TOPIC_RE
from agent_build_loop.state import atomic_write

MATCHER = "Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell|mcp__.*"
MARKER = "agent_build_loop hook"
SETTINGS_RELPATH = ".claude/settings.local.json"
_ROLE_RE = re.compile(r"^(main|[a-z][a-z0-9-]{1,39})$")


def hook_command(event: str, role: str, python: str | None = None) -> str:
    """`"<python>" -m agent_build_loop hook <event> --as <role>`; forward slashes, works in Git Bash and splits the same in `shlex`."""
    if event not in ("pre-tool-use", "session-start"):
        raise ValueError("unknown hook event")
    if not _ROLE_RE.match(role) or (role != "main" and not TOPIC_RE.match(role)):
        raise ValueError("role must be main or a topic")
    exe = (python or sys.executable).replace("\\", "/")
    if '"' in exe or "\n" in exe or "\r" in exe:
        raise ValueError("interpreter path has characters that cannot be quoted")
    return f'"{exe}" -m agent_build_loop hook {event} --as {role}'


def _ours(entry) -> bool:
    return isinstance(entry, dict) and any(
        isinstance(h, dict) and MARKER in str(h.get("command", "")) for h in entry.get("hooks", [])
    )


def write_hooks(directory, role: str, python: str | None = None) -> Path:
    """Merge our PreToolUse and SessionStart entries into `<directory>/.claude/settings.local.json`.

    Other keys and other hook entries stay. Running it twice gives the same file.
    """
    path = Path(directory) / SETTINGS_RELPATH
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            raise ValueError("existing settings file is not valid JSON; not overwriting it") from exc
        if not isinstance(data, dict):
            raise ValueError("existing settings file is not a JSON object")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("existing 'hooks' entry is not an object")
    wanted = {
        "PreToolUse": {"matcher": MATCHER, "hooks": [{"type": "command", "command": hook_command("pre-tool-use", role, python)}]},
        "SessionStart": {"hooks": [{"type": "command", "command": hook_command("session-start", role, python)}]},
    }
    for event, entry in wanted.items():
        kept = [e for e in hooks.get(event, []) if not _ours(e)]
        hooks[event] = kept + [entry]
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True) + "\n")
    return path


def hooks_installed(directory, role: str, python: str | None = None) -> bool:
    """True when the file already holds exactly our two entries for `role` (so `write_hooks` would change nothing)."""
    path = Path(directory) / SETTINGS_RELPATH
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return False
    want = {"PreToolUse": hook_command("pre-tool-use", role, python), "SessionStart": hook_command("session-start", role, python)}
    for event, command in want.items():
        ours = [e for e in hooks.get(event, []) if _ours(e)]
        if len(ours) != 1 or [h.get("command") for h in ours[0].get("hooks", [])] != [command]:
            return False
        if event == "PreToolUse" and ours[0].get("matcher") != MATCHER:
            return False
    return True


def remove_hooks(directory) -> bool:
    """Take our entries out again (the person's recovery step). Returns True when the file changed."""
    path = Path(directory) / SETTINGS_RELPATH
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    changed = False
    hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
    for event in list(hooks):
        kept = [e for e in hooks[event] if not _ours(e)]
        if len(kept) != len(hooks[event]):
            changed = True
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if changed:
        if not hooks:
            data.pop("hooks", None)
        atomic_write(path, json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True) + "\n")
    return changed
