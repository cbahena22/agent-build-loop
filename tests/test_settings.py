import json
import shlex
import subprocess
import sys

import pytest

from agent_build_loop import settings


def test_command_shape_and_forward_slashes():
    cmd = settings.hook_command("pre-tool-use", "add-summary", python="C:" + "\\Python313\\python.exe")
    assert cmd == '"C:/Python313/python.exe" -m agent_build_loop hook pre-tool-use --as add-summary'
    assert settings.hook_command("session-start", "main").endswith("hook session-start --as main")


def test_command_with_spaces_splits_the_same_everywhere():
    py = "/opt/my python/bin/python"
    cmd = settings.hook_command("pre-tool-use", "main", python=py)
    argv = shlex.split(cmd)
    assert argv == [py, "-m", "agent_build_loop", "hook", "pre-tool-use", "--as", "main"]
    again = subprocess.list2cmdline(argv)
    assert again.startswith('"/opt/my python/bin/python" -m')
    assert shlex.split(again) == argv


@pytest.mark.parametrize("event,role,py", [("other", "main", None), ("pre-tool-use", "Bad", None), ("pre-tool-use", "main; rm", None), ("pre-tool-use", "x", None),
                                           ("pre-tool-use", "main", 'a"b'), ("pre-tool-use", "main", "a\nb")])
def test_bad_inputs_refused(event, role, py):
    with pytest.raises(ValueError):
        settings.hook_command(event, role, python=py)


def test_write_is_idempotent_and_matches_claude_code_shape(tmp_path):
    p = settings.write_hooks(tmp_path, "add-summary")
    first = p.read_text(encoding="utf-8")
    settings.write_hooks(tmp_path, "add-summary")
    assert p.read_text(encoding="utf-8") == first
    data = json.loads(first)
    pre = data["hooks"]["PreToolUse"]
    assert len(pre) == 1 and pre[0]["matcher"] == settings.MATCHER and pre[0]["hooks"][0]["type"] == "command"
    assert len(data["hooks"]["SessionStart"]) == 1
    assert first.isascii()
    assert sys.executable.replace("\\", "/") in pre[0]["hooks"][0]["command"]


def test_merge_keeps_other_keys_and_other_hooks(tmp_path):
    p = tmp_path / ".claude" / "settings.local.json"
    p.parent.mkdir()
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
    p.write_text(json.dumps({"permissions": {"allow": ["Read"]}, "hooks": {"PreToolUse": [other], "Stop": [{"hooks": []}]}}), encoding="utf-8-sig")
    settings.write_hooks(tmp_path, "main")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["permissions"] == {"allow": ["Read"]} and data["hooks"]["Stop"] == [{"hooks": []}]
    assert other in data["hooks"]["PreToolUse"] and len(data["hooks"]["PreToolUse"]) == 2
    settings.write_hooks(tmp_path, "add-summary")  # changing the role replaces our entry, not the other one
    data = json.loads(p.read_text(encoding="utf-8"))
    cmds = [h["command"] for e in data["hooks"]["PreToolUse"] for h in e["hooks"]]
    assert cmds.count("echo hi") == 1 and sum("--as add-summary" in c for c in cmds) == 1 and not any("--as main" in c for c in cmds)


def test_broken_existing_file_is_not_overwritten(tmp_path):
    p = tmp_path / ".claude" / "settings.local.json"
    p.parent.mkdir()
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        settings.write_hooks(tmp_path, "main")
    assert p.read_text(encoding="utf-8") == "{not json"
    p.write_text("[1]", encoding="utf-8")
    with pytest.raises(ValueError):
        settings.write_hooks(tmp_path, "main")


def test_remove_hooks_is_the_recovery_step(tmp_path):
    p = tmp_path / ".claude" / "settings.local.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"permissions": {"allow": []}}), encoding="utf-8")
    settings.write_hooks(tmp_path, "main")
    assert settings.remove_hooks(tmp_path) is True
    assert json.loads(p.read_text(encoding="utf-8")) == {"permissions": {"allow": []}}
    assert settings.remove_hooks(tmp_path) is False
    assert settings.remove_hooks(tmp_path / "nowhere") is False
