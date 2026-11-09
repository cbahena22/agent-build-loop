import stat

import pytest

from agent_build_loop import gitio, trust, worktrees
from tests.conftest import commit_all


@pytest.fixture
def armed(repo):
    trust.record(repo.store, repo.common_dir, "main")
    return repo


def test_unchanged_repo_is_unchanged(armed):
    assert trust.changed(armed.store, armed.common_dir, "main") == []


def test_never_recorded_counts_as_changed(repo):
    repo.store.path("trust.json").unlink()
    assert set(trust.changed(repo.store, repo.common_dir, "main")) == set(trust.PART_LABELS)


def test_new_hook_file(armed):
    hook = armed.common_dir / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    assert trust.changed(armed.store, armed.common_dir, "main") == ["hooks"]


def test_sample_hooks_are_ignored(armed):
    (armed.common_dir / "hooks" / "pre-commit.sample").write_text("# sample", encoding="utf-8")
    assert trust.changed(armed.store, armed.common_dir, "main") == []


def test_fsmonitor_line(armed, env):
    gitio.git("config", "core.fsmonitor", "some-program", cwd=armed.main_root, env_extra=env)
    assert trust.changed(armed.store, armed.common_dir, "main") == ["config"]


def test_filter_smudge_line(armed, env):
    gitio.git("config", "filter.x.smudge", "some-program", cwd=armed.main_root, env_extra=env)
    assert trust.changed(armed.store, armed.common_dir, "main") == ["config"]


def test_attributes_line(armed):
    info = armed.common_dir / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("*.py filter=x\n", encoding="utf-8")
    assert trust.changed(armed.store, armed.common_dir, "main") == ["attributes"]


def test_config_worktree_line(armed):
    d = armed.common_dir / "worktrees" / "add-summary"
    d.mkdir(parents=True)
    (d / "config.worktree").write_text("[core]\n\tfsmonitor = x\n", encoding="utf-8")
    assert trust.changed(armed.store, armed.common_dir, "main") == ["worktree_configs"]


def test_filter_in_main_config_worktree(armed, env):
    """With extensions.worktreeConfig on, git reads <common>/config.worktree for the main checkout: it must count."""
    gitio.git("config", "extensions.worktreeConfig", "true", cwd=armed.main_root, env_extra=env)
    trust.record(armed.store, armed.common_dir, "main")  # the person accepted the extension itself
    (armed.common_dir / "config.worktree").write_text('[filter "x"]\n\tclean = some-program\n', encoding="utf-8")
    assert trust.changed(armed.store, armed.common_dir, "main") == ["config"]
    with pytest.raises(worktrees.Refused, match="trust-changed"):
        worktrees.require_trust(armed)


def test_included_config_file_counts(armed, env, tmp_path):
    extra = tmp_path / "extra gitconfig"
    extra.write_text("[core]\n\tautocrlf = false\n", encoding="utf-8")
    gitio.git("config", "include.path", extra.as_posix(), cwd=armed.main_root, env_extra=env)
    trust.record(armed.store, armed.common_dir, "main")
    extra.write_text('[core]\n\tautocrlf = false\n[filter "x"]\n\tclean = some-program\n', encoding="utf-8")
    assert trust.changed(armed.store, armed.common_dir, "main") == ["config"]


def test_global_config_counts(armed, tmp_path):
    (tmp_path / ".gitconfig").write_text('[filter "x"]\n\tclean = some-program\n', encoding="utf-8")  # HOME is tmp_path
    assert trust.changed(armed.store, armed.common_dir, "main") == ["config"]


def test_fingerprint_is_stable_across_calls(armed):
    assert trust.fingerprint(armed.common_dir, "main") == trust.fingerprint(armed.common_dir, "main")


def test_main_project_config_commit(armed, env):
    (armed.main_root / ".abl.toml").write_text('check = ["python", "-m", "unittest"]\n', encoding="utf-8")
    commit_all(armed.main_root, "change config", env)
    assert trust.changed(armed.store, armed.common_dir, "main") == ["project_config"]


def test_worker_branch_commit_does_not_change_it(armed, env):
    gitio.git("switch", "-q", "-c", "w/other", cwd=armed.main_root, env_extra=env)
    (armed.main_root / ".abl.toml").write_text("shared = []\n", encoding="utf-8")
    (armed.main_root / "print_quotes" / "extra.py").write_text("X = 1\n", encoding="utf-8")
    commit_all(armed.main_root, "worker work", env)
    assert trust.changed(armed.store, armed.common_dir, "main") == []


def test_rehash_clears_it(armed):
    (armed.common_dir / "info").mkdir(exist_ok=True)
    (armed.common_dir / "info" / "attributes").write_text("* eol=lf\n", encoding="utf-8")
    assert trust.changed(armed.store, armed.common_dir, "main")
    trust.record(armed.store, armed.common_dir, "main")
    assert trust.changed(armed.store, armed.common_dir, "main") == []


def test_explain_names_what_changed():
    msg = trust.explain(["hooks", "config"])
    assert "git hooks" in msg and "git config" in msg and "abl init --rehash" in msg and msg.startswith("trust-changed")


def test_new_end_and_claim_keep_the_record_current(armed):
    worktrees.new(armed, "add-summary", ["print_quotes/summary.py"], "a summary")
    assert trust.changed(armed.store, armed.common_dir, "main") == []
    worktrees.claim_add(armed, "add-summary", ["tests/test_summary.py"])
    assert trust.changed(armed.store, armed.common_dir, "main") == []
    worktrees.end(armed, "add-summary")
    assert trust.changed(armed.store, armed.common_dir, "main") == []


def test_new_refuses_when_nothing_was_ever_trusted(repo):
    repo.store.path("trust.json").unlink()
    with pytest.raises(worktrees.Refused, match="abl init"):
        worktrees.new(repo, "add-summary", ["x.py"], "g")
    assert not repo.store.path("trust.json").exists()  # refusing must not create the record


def test_planted_filter_and_hook_stop_new_claim_and_end_before_any_git_call(armed, env):
    worktrees.new(armed, "add-summary", ["a.py"], "g")
    marker = armed.main_root / "filter-ran.txt"
    gitio.git("config", "filter.x.smudge", f"touch '{marker.as_posix()}'", cwd=armed.main_root, env_extra=env)
    (armed.common_dir / "info").mkdir(exist_ok=True)
    (armed.common_dir / "info" / "attributes").write_text("*.py filter=x\n", encoding="utf-8")
    for call in (lambda: worktrees.new(armed, "other-one", ["b.py"], "g"),
                 lambda: worktrees.claim_add(armed, "add-summary", ["c.py"]),
                 lambda: worktrees.end(armed, "add-summary")):
        with pytest.raises(worktrees.Refused, match="trust-changed"):
            call()
    assert not marker.exists()
    # still flagged, not re-recorded (the worker tree reads the same planted filter, so its view changed too)
    assert trust.changed(armed.store, armed.common_dir, "main") == ["config", "attributes", "worktree_configs"]
    assert gitio.rev("refs/heads/w/other-one", armed.main_root) is None
