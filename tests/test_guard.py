"""The PreToolUse guard: every rule has an allow and a deny case, on payloads shaped like Claude Code's hook input."""
import json
import os
import subprocess
import sys

import pytest

from agent_build_loop import events, worktrees
from agent_build_loop.hooks import guard

from .conftest import build_repo, commit_all, git_env

WIN = sys.platform == "win32"
A, B = "add-summary", "rename-total"


@pytest.fixture(autouse=True)
def no_project_dir(monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)


@pytest.fixture(scope="module")
def two(tmp_path_factory):
    """One repository with two open claims for the whole module (building it costs seconds on Windows)."""
    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("guard")
    root = build_repo(tmp, git_env(tmp), mp)
    repo = worktrees.open_repo(root)
    worktrees.new(repo, A, ["print_quotes/summary.py", "tests/test_summary.py"], "add a quote summary")
    worktrees.new(repo, B, ["print_quotes/totals.py", "tests/test_totals.py"], "rename total to quote_total")
    yield worktrees.open_repo(root)
    mp.undo()


def payload(tool, cwd, **tool_input):
    return {"session_id": "s1", "transcript_path": "t.jsonl", "cwd": str(cwd), "permission_mode": "default",
            "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input, "tool_use_id": "toolu_1"}


def decide(role, data):
    raw = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
    return guard.run(role, raw)


def wt(repo, topic):
    return repo.main_root / ".worktrees" / topic


def write(repo, role, path, cwd=None):
    cwd = cwd or (wt(repo, role) if role != "main" else repo.main_root)
    return decide(role, payload("Write", cwd, file_path=str(path), content="x"))


def shell(repo, role, command, tool="Bash"):
    cwd = wt(repo, role) if role != "main" else repo.main_root
    return decide(role, payload(tool, cwd, command=command, description="d"))


# ---- no claim -------------------------------------------------------------------------------------------------


def test_no_open_claim_allows_everything(repo):
    assert write(repo, "main", repo.store.path("approvals", "x.json")).allow
    assert decide("main", b"not json").allow
    assert shell(repo, "main", "abl approve x").allow


# ---- worker editor rules --------------------------------------------------------------------------------------


def test_worker_owns_its_claim_and_handoff(two):
    w = wt(two, A)
    assert write(two, A, w / "print_quotes" / "summary.py").allow
    assert write(two, A, w / "handoff" / f"{A}.md").allow
    assert decide(A, payload("Edit", w, file_path=str(w / "tests" / "test_summary.py"), old_string="a", new_string="b")).allow
    assert decide(A, payload("MultiEdit", w, file_path=str(w / "tests" / "test_summary.py"), edits=[])).allow
    assert decide(A, payload("NotebookEdit", w, notebook_path=str(w / "print_quotes" / "summary.py"), new_source="x")).allow


def test_worker_claim_escape_shared_other_main_outside(two, tmp_path):
    w = wt(two, A)
    d = decide(A, payload("Edit", w, file_path=str(w / "print_quotes" / "totals.py"), old_string="a", new_string="b"))
    assert not d.allow and d.rule == "claim-escape"
    assert write(two, A, w / "pyproject.toml").rule == "shared-path"
    assert write(two, A, w / ".gitignore").rule == "shared-path"
    assert write(two, A, wt(two, B) / "print_quotes" / "totals.py").rule == "other-worktree"
    assert write(two, A, two.main_root / "print_quotes" / "summary.py").rule == "main-checkout"
    assert write(two, A, tmp_path / "elsewhere.txt").rule == "outside-repo"
    assert write(two, A, w / "handoff" / f"{B}.md").rule == "claim-escape"


def test_relative_paths_resolve_from_the_payload_cwd(two):
    w = wt(two, A)
    assert decide(A, payload("Write", w, file_path="print_quotes/summary.py", content="x")).allow
    assert decide(A, payload("Write", w, file_path="../../print_quotes/summary.py", content="x")).rule == "main-checkout"
    assert decide(A, payload("Write", w, file_path=f"../{B}/print_quotes/totals.py", content="x")).rule == "other-worktree"


def test_cwd_in_main_with_worker_identity_is_still_the_worker(two):
    """Identity is the --as baked into the settings file, not where the session stands."""
    d = write(two, A, two.main_root / "README.md", cwd=two.main_root)
    assert not d.allow and d.rule == "main-checkout"
    assert write(two, "main", two.main_root / "README.md").allow


def test_worker_and_main_cannot_touch_the_state_store_or_git_dir(two):
    for role in (A, "main"):
        for target in (two.store.path("approvals", f"{A}.json"), two.store.path("audits", f"{A}.json"), two.store.path("claims", f"{A}.json"),
                       two.store.path("settings.json"), two.store.path("trust.json"), two.common_dir / "config",
                       two.common_dir / "hooks" / "post-checkout"):
            assert write(two, role, target).rule == "git-dir", (role, target)
    assert write(two, A, wt(two, A) / ".git").rule == "git-dir"


def test_settings_files_are_denied_to_every_session(two):
    for role in (A, "main"):
        for target in (two.main_root / ".claude" / "settings.local.json", two.main_root / ".claude" / "settings.json",
                       wt(two, A) / ".claude" / "settings.local.json"):
            d = decide(role, payload("Edit", wt(two, A), file_path=str(target), old_string="a", new_string="b"))
            assert d.rule == "settings-file", (role, target)


def test_user_level_claude_settings_are_denied(two, tmp_path, monkeypatch):
    """~/.claude/settings*.json and $CLAUDE_CONFIG_DIR can switch every hook off (disableAllHooks)."""
    home = tmp_path / "home dir"
    config = tmp_path / "config dir"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    for target in (home / ".claude" / "settings.json", home / ".claude" / "settings.local.json", config / "settings.json"):
        assert write(two, "main", target).rule == "settings-file", target
    assert write(two, "main", home / ".claude" / "notes.md").allow
    assert write(two, "main", home / "settings.json").allow


def test_project_config_is_denied_to_every_session(two):
    assert write(two, "main", two.main_root / ".abl.toml").rule == "project-config"
    assert write(two, A, wt(two, A) / ".abl.toml").rule == "project-config"


def test_main_rules(two):
    assert write(two, "main", two.main_root / "docs" / "notes.md").allow
    assert write(two, "main", two.main_root / "print_quotes" / "summary.py").rule == "claimed-path"
    assert write(two, "main", two.main_root / "print_quotes" / "pricing.py").allow
    assert write(two, "main", wt(two, A) / "print_quotes" / "summary.py").rule == "worktree-path"
    assert write(two, "main", two.main_root / ".worktrees" / "stray" / "x.py").rule == "worktree-path"


def test_case_folding_on_windows(two):
    w = wt(two, A)
    d = write(two, A, w / "PRINT_QUOTES" / "Summary.py")
    assert d.allow if WIN else d.rule == "claim-escape"
    assert write(two, "main", two.main_root / "Print_Quotes" / "SUMMARY.py").rule == ("claimed-path" if WIN else None) or not WIN


def test_unknown_paths_are_denied_never_treated_as_outside(two):
    for raw in ("\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\x.py", "\\\\localhost\\C$\\x\\y.py", "a\x00b.py", ""):
        if raw.startswith("\\\\") and not WIN:
            continue
        for role in (A, "main"):
            d = write(two, role, raw)
            assert not d.allow and d.rule in ("unknown-path", "bad-payload"), (role, raw, d)


def test_mcp_tools_denied_for_workers_only(two):
    assert decide(A, payload("mcp__files__write", wt(two, A), path="x")).rule == "mcp-tool"
    assert decide("main", payload("mcp__files__write", two.main_root, path="x")).allow


def test_worker_branch_config_does_not_loosen_the_guard(repo, env):
    """A worker commits an .abl.toml that empties `shared`; the guard reads main's, so pyproject.toml stays denied."""
    worktrees.new(repo, A, ["print_quotes/summary.py"], "add a quote summary")
    w = wt(repo, A)
    (w / ".abl.toml").write_text('check = ["python", "-m", "unittest", "-q"]\nshared = []\n', encoding="utf-8")
    commit_all(w, "loosen", env)
    assert write(repo, A, w / "pyproject.toml").rule == "shared-path"


# ---- Windows path forms (run on the PC, skipped elsewhere) -------------------------------------------------------


@pytest.mark.skipif(not WIN, reason="junctions, \\\\?\\ and 8.3 names exist only on Windows")
def test_windows_junction_extended_prefix_and_short_names(two):
    import _winapi

    w = wt(two, A)
    link = w / "print_quotes" / "escape"
    _winapi.CreateJunction(str(two.main_root / "print_quotes"), str(link))
    try:
        assert write(two, A, link / "totals.py").rule == "main-checkout"
    finally:
        os.rmdir(link)
    assert write(two, A, "\\\\?\\" + str(w / "print_quotes" / "summary.py")).allow
    assert write(two, A, "\\\\?\\" + str(two.main_root / "print_quotes" / "totals.py")).rule == "main-checkout"
    assert write(two, A, str(w / "print_quotes" / "totals.py::$DATA")).rule == "claim-escape"
    assert write(two, A, str(w / "print_quotes" / "summary.py.")).allow
    from agent_build_loop.mask import short_form

    short = short_form(str(two.main_root))
    if short is None:
        pytest.skip("8.3 names are off on this volume")
    assert write(two, A, short + "\\print_quotes\\totals.py").rule == "main-checkout"
    assert write(two, "main", short + "\\print_quotes\\summary.py").rule == "claimed-path"


# ---- shell rules ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    "git push", "git push origin w/add-summary", "git -C .. merge w/rename-total", '"git" "push"', "echo hi; git push",
    "echo hi && git rebase main", "git status || git reset --hard", "git checkout main", "git switch main",
    "git worktree remove x", "git config core.hooksPath x", "git update-ref refs/heads/main HEAD", "git branch -D w/x",
    "git tag v1", "git -c core.fsmonitor=x status", "git --git-dir=../../.git log", "echo x > .git/hooks/post-checkout",
    "cat foo | tee ../../.git/config", "git pull",
    # ref writers (hardening): a commit after symbolic-ref would land on main
    "git symbolic-ref HEAD refs/heads/main", "git replace HEAD~1 HEAD", "git fetch --update-head-ok . HEAD:main",
    "git notes add -m x", "git filter-branch -f --tree-filter true",
])
def test_worker_shell_denies(two, command):
    assert not shell(two, A, command).allow, command


@pytest.mark.parametrize("command", ["echo hi; git push", "git status; git merge main"])
def test_worker_powershell_denies(two, command):
    assert not shell(two, A, command, tool="PowerShell").allow


@pytest.mark.parametrize("command", [
    "git status", "git add -A && git commit -m 'add summary'", "git diff", "git log --oneline", "git checkout -- print_quotes/summary.py",
    "python -m unittest -q", "abl list", "abl verdict add-summary", "abl handover show", "cat .gitignore",
])
def test_worker_shell_allows(two, command):
    assert shell(two, A, command).allow, command


@pytest.mark.parametrize("command", ["abl new x --owns a --goal g", "abl audit add-summary", "abl merge add-summary",
                                     "abl claim add-summary --add x", "abl init", "abl handover write"])
def test_worker_abl_limits(two, command):
    assert shell(two, A, command).rule == "worker-abl"


PERSON_ONLY = [
    "abl approve add-summary", "abl.exe approve add-summary", "C:/tools/venv/Scripts/abl.exe approve add-summary",
    "python -m agent_build_loop approve add-summary", "py -m agent_build_loop init --rehash", "python3 -I -m agent_build_loop end x --discard",
    "abl init --set require_approval=false", "abl init --remove-hooks", "abl init --rehash",
    "bash -c 'abl approve add-summary'", 'sh -c "abl end add-summary --discard"', "cmd /c abl init --remove-hooks",
    'powershell -Command "abl init --set require_approval=false"', "pwsh -c 'abl approve add-summary'",
    "echo $(abl approve add-summary)", "echo `abl approve add-summary`", "true && abl approve add-summary",
    "bash -c \"sh -c 'abl approve add-summary'\"", "env FOO=1 abl approve add-summary", "X=1 abl approve add-summary",
    "python -c \"from agent_build_loop import approve; approve.approve(None, 'x')\"",
    # pty and runner wrappers (hardening): a pty plus the retype in one command
    "script -q -c \"abl approve add-summary\" /dev/null <<< 1234abcd", "script -ec 'abl approve add-summary' /dev/null", "script --command='abl approve add-summary' /dev/null",
    "unbuffer abl approve add-summary", "expect -c \"spawn abl approve add-summary\"", "winpty abl approve add-summary",
    "uv run abl approve add-summary", "pipx run abl init --rehash", "timeout 30 abl approve add-summary",
    "some-wrapper --flag abl init --rehash", "stdbuf -o0 python -m agent_build_loop approve add-summary",
]


@pytest.mark.parametrize("command", PERSON_ONLY)
@pytest.mark.parametrize("role", ["main", A])
def test_person_only_commands_denied_for_every_session(two, role, command):
    d = shell(two, role, command)
    assert not d.allow and d.rule == "person-only", (role, command, d)


@pytest.mark.parametrize("command", ['& "abl" approve add-summary', "echo a; abl approve add-summary",
                                     "powershell -c 'abl init --rehash'", "& python -m agent_build_loop approve x"])
def test_person_only_in_powershell(two, command):
    assert shell(two, "main", command, tool="PowerShell").rule == "person-only"


def test_encoded_and_unbalanced_commands_are_denied_while_claims_are_open(two):
    assert shell(two, "main", "powershell -EncodedCommand ZQBjAGgAbwA=", tool="PowerShell").rule == "shell-unparsable"
    assert shell(two, A, "echo 'unclosed").rule == "shell-unparsable"


def test_main_shell_is_free_except_person_only(two):
    for command in ("git merge w/add-summary", "abl new x --owns docs/x.md --goal g", "abl init --dry-run", "abl audit add-summary"):
        assert shell(two, "main", command).allow, command


# ---- fail closed ----------------------------------------------------------------------------------------------


def test_fail_closed_on_bad_payloads(two, monkeypatch):
    w = wt(two, A)
    monkeypatch.chdir(w)  # an unreadable payload has no cwd: the hook process's own folder is the session's
    assert decide(A, b"not json").rule == "bad-payload"
    assert decide(A, b"[]").rule == "bad-payload"
    assert decide(A, json.dumps({"tool_name": "Write"}).encode()).rule == "bad-payload"
    assert decide(A, payload("Write", w, content="no path")).rule == "bad-payload"
    assert decide(A, payload("Bash", w, command=["git", "push"])).rule == "bad-payload"
    big = payload("Write", w, file_path=str(w / "print_quotes" / "summary.py"), content="x" * (2 * 1024 * 1024))
    assert decide(A, big).rule == "bad-payload"
    assert guard.run(A, None).rule == "bad-payload"
    assert guard.run("Not A Role", b"{}").rule == "bad-payload"


def test_guard_exception_denies(two, monkeypatch):
    monkeypatch.chdir(two.main_root)
    monkeypatch.setattr(guard, "_editor", lambda *a: 1 / 0)
    assert write(two, A, wt(two, A) / "print_quotes" / "summary.py").rule == "guard-error"


def test_utf8_bom_payload(two):
    w = wt(two, A)
    raw = "\ufeff".encode("utf-8") + json.dumps(payload("Write", w, file_path=str(w / "print_quotes" / "summary.py"), content="\u00e9")).encode("utf-8")
    assert decide(A, raw).allow


def test_every_deny_is_logged_with_the_role_and_no_absolute_path(two):
    w = wt(two, A)
    seen = len(events.read(two.store))
    write(two, A, w / "print_quotes" / "totals.py")
    shell(two, A, f'git -C "{two.main_root.as_posix()}" push')
    rows = [r for r in events.read(two.store)[seen:] if r["event"] == "guard-deny"]
    assert [r["actor"] for r in rows] == [A, A]
    assert rows[0]["detail"]["rule"] == "claim-escape" and rows[0]["detail"]["path"] == "print_quotes/totals.py"
    assert "command" not in rows[1]["detail"]  # it held an absolute path
    assert str(two.main_root) not in json.dumps(rows)


def test_hook_subprocess_prints_ascii_deny_json_or_nothing(two):
    w = wt(two, A)
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_") and k != "CLAUDE_PROJECT_DIR"}

    def run(data):
        return subprocess.run([sys.executable, "-m", "agent_build_loop", "hook", "pre-tool-use", "--as", A], input=json.dumps(data).encode(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(w), check=False, timeout=60)

    denied = run(payload("Edit", w, file_path=str(w / "print_quotes" / "totals.py"), old_string="\u00e9", new_string="b"))
    assert denied.returncode == 0
    out = json.loads(denied.stdout)
    assert denied.stdout.decode("ascii")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "claim-escape" in out["hookSpecificOutput"]["permissionDecisionReason"]
    allowed = run(payload("Write", w, file_path=str(w / "print_quotes" / "summary.py"), content="x"))
    assert allowed.returncode == 0 and allowed.stdout == b""
