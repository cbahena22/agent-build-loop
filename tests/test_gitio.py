import os

import pytest

from agent_build_loop import gitio
from agent_build_loop.gitio import GitError


def test_version_check():
    assert gitio.require_git()[:2] >= gitio.MIN_GIT


def test_environment_is_scrubbed_and_allowlisted(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/nowhere")
    monkeypatch.setenv("GIT_SSH_COMMAND", "evil")
    monkeypatch.setenv("git_index_file", "/nowhere")
    env = gitio.scrubbed_env({"GIT_AUTHOR_NAME": "x"})
    assert not [k for k in env if k.upper() in ("GIT_DIR", "GIT_SSH_COMMAND", "GIT_INDEX_FILE")]
    assert env["GIT_AUTHOR_NAME"] == "x" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert "PATH" in env
    for bad in ("GIT_DIR", "GIT_EXEC_PATH", "GIT_SSH_COMMAND", "GIT_CONFIG_COUNT", "PATH"):
        with pytest.raises(ValueError):
            gitio.scrubbed_env({bad: "x"})


def test_a_git_dir_in_the_environment_does_not_redirect_calls(repo_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    assert gitio.toplevel(repo_dir)  # still finds the repository from cwd


def test_failure_message_is_fixed_and_stderr_is_kept_apart(repo_dir):
    with pytest.raises(GitError) as exc:
        gitio.git("rev-parse", "--verify", "no-such-ref-here", cwd=repo_dir)
    assert str(exc.value) == "git rev-parse failed (exit 128)"
    assert exc.value.first_line
    assert exc.value.first_line not in str(exc.value)


def test_hooks_and_fsmonitor_are_off(repo_dir, env):
    marker = repo_dir / "ran.txt"
    hook = repo_dir / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n", encoding="utf-8")
    hook.chmod(0o755)
    gitio.git("config", "core.fsmonitor", "touch " + marker.name, cwd=repo_dir, env_extra=env)
    gitio.git("switch", "-q", "-c", "other", cwd=repo_dir, env_extra=env)
    gitio.git("status", cwd=repo_dir)
    assert not marker.exists()
    assert "hooksPath" not in (repo_dir / ".git" / "config").read_text(encoding="utf-8")  # the override is per call, not stored


def test_safe_ref():
    assert gitio.safe_ref("refs/heads/main") == "refs/heads/main"
    assert gitio.safe_ref("w/add-summary") == "w/add-summary"
    for bad in ("--upload-pack=x", "-x", "", "a..b", "a b", "a;b", "a/", "x.lock", "a\nb", "$(x)"):
        with pytest.raises(ValueError):
            gitio.safe_ref(bad)


def test_rev_and_blob_helpers(repo_dir):
    assert len(gitio.rev("refs/heads/main", repo_dir)) == 40
    assert gitio.rev("refs/heads/nothing", repo_dir) is None
    assert gitio.blob_sha("refs/heads/main", ".abl.toml", repo_dir)
    assert gitio.blob_sha("refs/heads/main", "no-such-file", repo_dir) is None
    assert "unittest" in gitio.show_blob("refs/heads/main", ".abl.toml", repo_dir)
    assert gitio.show_blob("refs/heads/main", "nope", repo_dir) is None
    with pytest.raises(ValueError):
        gitio.rev("--version", repo_dir)


def test_clean_and_dirty(repo_dir):
    assert gitio.is_clean_tracked(repo_dir) and not gitio.is_dirty(repo_dir)
    (repo_dir / "new.txt").write_text("x", encoding="utf-8")
    assert gitio.is_clean_tracked(repo_dir) and gitio.is_dirty(repo_dir)
    (repo_dir / "pyproject.toml").write_text("changed", encoding="utf-8")
    assert not gitio.is_clean_tracked(repo_dir)


def test_empty_call_is_refused(tmp_path):
    with pytest.raises(ValueError):
        gitio.git(cwd=tmp_path)


def test_no_shell_even_with_metacharacters(repo_dir):
    r = gitio.git("log", "--format=%s", "--", "a;b && echo hi", cwd=repo_dir)
    assert "hi" not in r.out.split() and os.path.exists(repo_dir)
