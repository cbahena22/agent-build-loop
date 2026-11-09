import json
import stat

import pytest

from agent_build_loop import gitio, trust, worktrees
from agent_build_loop.paths import PathRuleError
from agent_build_loop.worktrees import Refused
from tests.conftest import commit_all


def test_new_list_claim_add_end(repo, env):
    claim, hint = worktrees.new(repo, "add-summary", ["print_quotes/summary.py", "tests/test_summary.py"], "Add a summary line", "sonnet", "high")
    wt = repo.main_root / ".worktrees" / "add-summary"
    assert wt.is_dir() and claim.branch == "w/add-summary" and claim.base_sha == gitio.rev("refs/heads/main", repo.main_root)
    assert ".worktrees/add-summary" in hint and "claude" in hint
    assert (wt / ".claude" / "settings.local.json").exists()
    assert gitio.rev("refs/heads/w/add-summary", repo.main_root) == claim.base_sha

    rows = worktrees.list_rows(repo)
    assert [(r.topic, r.ahead, r.dirty, r.verdict, r.approved) for r in rows] == [("add-summary", 0, False, "none", False)]
    assert "add-summary" in worktrees.format_rows(rows) and "w/add-summary" in worktrees.format_rows(rows)

    worktrees.claim_add(repo, "add-summary", ["docs/summary.md"])
    assert repo.store.load_claim("add-summary").owns[-1] == "docs/summary.md"
    with pytest.raises(Refused, match="shared"):
        worktrees.claim_add(repo, "add-summary", ["pyproject.toml"])
    with pytest.raises(Refused, match="no open claim"):
        worktrees.claim_add(repo, "missing-one", ["x.py"])

    (wt / "scratch.txt").write_text("x", encoding="utf-8")
    assert worktrees.list_rows(repo)[0].dirty
    with pytest.raises(Refused, match="uncommitted"):
        worktrees.end(repo, "add-summary")  # branch has no commits (merged), but the folder is dirty
    (wt / "scratch.txt").unlink()
    worktrees.end(repo, "add-summary")
    assert not wt.exists() and gitio.rev("refs/heads/w/add-summary", repo.main_root) is None
    assert repo.store.load_claims() == []
    assert "no open claims" in worktrees.format_rows(worktrees.list_rows(repo))


def test_list_shows_ahead_and_audit_state(repo, env):
    worktrees.new(repo, "add-summary", ["print_quotes/summary.py"], "g")
    wt = repo.main_root / ".worktrees" / "add-summary"
    (wt / "print_quotes" / "summary.py").write_text("X = 1\n", encoding="utf-8")
    head = commit_all(wt, "work", env)
    row = worktrees.list_rows(repo)[0]
    assert row.ahead == 1 and row.head == head[:8] and not row.dirty
    repo.store.ensure()
    repo.store.path("audits", "add-summary.json").write_text(json.dumps({"head": head, "verdict": "PASS"}), encoding="utf-8")
    repo.store.path("approvals", "add-summary.json").write_text(json.dumps({"head": head}), encoding="utf-8")
    row = worktrees.list_rows(repo)[0]
    assert (row.verdict, row.verdict_current, row.approved) == ("PASS", True, True)
    (wt / "print_quotes" / "summary.py").write_text("X = 2\n", encoding="utf-8")
    commit_all(wt, "more", env)
    row = worktrees.list_rows(repo)[0]
    assert (row.verdict, row.verdict_current, row.approved) == ("PASS", False, False)


@pytest.mark.parametrize("topic", ["--upload-pack", "-x", "Add", "a", "a b", "../x"])
def test_option_like_topics_are_refused(repo, topic):
    with pytest.raises(PathRuleError):
        worktrees.new(repo, topic, ["x.py"], "g")
    assert not (repo.main_root / ".worktrees").exists() or not list((repo.main_root / ".worktrees").iterdir())


def test_overlap_shared_and_goal_refused_without_side_effects(repo):
    worktrees.new(repo, "add-summary", ["print_quotes/"], "g")
    for topic, owns, goal in (("other-one", ["print_quotes/totals.py"], "g"), ("third-one", ["pyproject.toml"], "g"), ("fourth-one", ["x.py"], "line\nbreak")):
        with pytest.raises((Refused, PathRuleError)):
            worktrees.new(repo, topic, owns, goal)
        assert gitio.rev(f"refs/heads/w/{topic}", repo.main_root) is None
    assert [c.topic for c in repo.store.load_claims()] == ["add-summary"]


def test_new_refuses_dirty_tracked_main_and_existing_branch(repo, env):
    (repo.main_root / "pyproject.toml").write_text("changed\n", encoding="utf-8")
    with pytest.raises(Refused, match="uncommitted"):
        worktrees.new(repo, "add-summary", ["x.py"], "g")
    gitio.git("checkout", "--", "pyproject.toml", cwd=repo.main_root, env_extra=env)
    gitio.git("branch", "w/add-summary", cwd=repo.main_root, env_extra=env)
    with pytest.raises(Refused, match="already exists"):
        worktrees.new(repo, "add-summary", ["x.py"], "g")


def test_end_refuses_unmerged_branch_but_discard_removes_it(repo, env):
    worktrees.new(repo, "add-summary", ["print_quotes/summary.py"], "g")
    wt = repo.main_root / ".worktrees" / "add-summary"
    (wt / "print_quotes" / "summary.py").write_text("X = 1\n", encoding="utf-8")
    commit_all(wt, "work", env)
    with pytest.raises(Refused, match="not on main"):
        worktrees.end(repo, "add-summary")
    assert wt.exists() and repo.store.load_claim("add-summary")
    worktrees.end(repo, "add-summary", discard=True)
    assert not wt.exists() and gitio.rev("refs/heads/w/add-summary", repo.main_root) is None


def test_end_after_a_merge_needs_no_discard(repo, env):
    worktrees.new(repo, "add-summary", ["print_quotes/summary.py"], "g")
    wt = repo.main_root / ".worktrees" / "add-summary"
    (wt / "print_quotes" / "summary.py").write_text("X = 1\n", encoding="utf-8")
    commit_all(wt, "work", env)
    gitio.git("merge", "-q", "--no-ff", "-m", "merge", "--end-of-options", "w/add-summary", cwd=repo.main_root, env_extra=env)
    worktrees.end(repo, "add-summary")
    assert repo.store.load_claims() == []


def test_planted_post_checkout_hook_does_not_run_on_new(repo):
    marker = repo.main_root / "hook-ran.txt"
    hook = repo.common_dir / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n", encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(Refused, match="trust-changed"):
        worktrees.new(repo, "add-summary", ["x.py"], "g")
    trust.record(repo.store, repo.common_dir, "main")  # even when the person accepts the hook, git must not run it
    worktrees.new(repo, "add-summary", ["x.py"], "g")
    assert not marker.exists()


def test_exclude_lines_are_added_once(repo):
    worktrees.new(repo, "add-summary", ["x.py"], "g")
    worktrees.ensure_exclude(repo.common_dir)
    text = (repo.common_dir / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert text.count(".worktrees/") == 1 and text.count(".claude/settings.local.json") == 1
    assert not gitio.is_dirty(repo.main_root)  # worktree folder and hook file stay out of status


def test_hook_settings_in_the_worktree_carry_the_topic(repo):
    worktrees.new(repo, "add-summary", ["x.py"], "g")
    data = json.loads((repo.main_root / ".worktrees" / "add-summary" / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    cmds = [h["command"] for ev in data["hooks"].values() for e in ev for h in e["hooks"]]
    assert cmds and all(c.endswith("--as add-summary") for c in cmds)


def test_whoami_is_a_cwd_mistake_guard(repo):
    worktrees.new(repo, "add-summary", ["x.py"], "g")
    wt = repo.main_root / ".worktrees" / "add-summary"
    assert worktrees.whoami(repo.main_root, repo) == "main"
    assert worktrees.whoami(wt, repo) == "add-summary"
    assert worktrees.whoami(wt / "print_quotes", repo) == "add-summary"
    with pytest.raises(Refused):
        worktrees.require_main(wt, repo)
    worktrees.require_main(repo.main_root, repo)


def test_open_repo_from_inside_a_worktree_finds_the_same_store(repo):
    worktrees.new(repo, "add-summary", ["x.py"], "g")
    inner = worktrees.open_repo(repo.main_root / ".worktrees" / "add-summary")
    assert inner.store.root == repo.store.root and inner.main_root == repo.main_root
    assert [c.topic for c in inner.store.load_claims()] == ["add-summary"]


def test_a_worker_branch_config_cannot_loosen_shared(repo, env):
    worktrees.new(repo, "add-summary", ["x.py"], "g")
    wt = repo.main_root / ".worktrees" / "add-summary"
    (wt / ".abl.toml").write_text("shared = []\n", encoding="utf-8")
    commit_all(wt, "loosen", env)
    inner = worktrees.open_repo(wt)
    assert "pyproject.toml" in inner.cfg.shared
    with pytest.raises(Refused, match="shared"):
        worktrees.new(inner, "other-one", ["pyproject.toml"], "g")
