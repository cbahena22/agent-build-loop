"""Approve + merge gate on real git: clean merge, conflict, failing checks, rollback with reset --keep, stale state, locks."""
import json
import os
import time

import pytest

from agent_build_loop import events, gitio, init, trust, worktrees
from agent_build_loop.approve import approve
from agent_build_loop.audit import record
from agent_build_loop.audit.reviewer import StubReviewer
from agent_build_loop.audit.run import audit
from agent_build_loop.merge import MergeRefused, merge
from agent_build_loop.state import LockBusy, LockStale

from .conftest import commit_all
from .test_audit import A, TEST, good_branch, open_worker, write_files

B = "rename-total"


def yes(expected, what):
    return None


def ready(repo, topic=A):
    audit(repo, topic, StubReviewer())
    approve(repo, topic, confirm=yes)


def main_head(repo):
    return gitio.rev("refs/heads/main", repo.main_root)


def rename_branch(repo, env):
    w = open_worker(repo, B, ("print_quotes/totals.py", "tests/test_totals.py"))
    for rel in ("print_quotes/totals.py", "tests/test_totals.py"):
        p = w / rel
        p.write_text(p.read_text().replace("total(", "quote_total(").replace("import total", "import quote_total"), encoding="utf-8", newline="\n")
    write_files(w, {f"handoff/{B}.md": f"renamed\n\nREADY TO MERGE: {B}\n"})
    commit_all(w, "rename", env)
    return w


def test_clean_merge_closes_the_claim_and_keeps_trust(repo, env):
    good_branch(repo, env)
    ready(repo)
    before = main_head(repo)
    result = merge(repo, A)
    assert result.code == 0 and "undo: git revert -m 1" in result.message
    after = main_head(repo)
    assert after != before
    assert gitio.git("rev-list", "--parents", "-n", "1", after, cwd=repo.main_root).out.split()[1] == before  # --no-ff
    assert repo.store.load_claim(A) is None and not (repo.main_root / ".worktrees" / A).exists()
    assert gitio.rev(f"refs/heads/w/{A}", repo.main_root) is None
    assert trust.changed(repo.store, repo.common_dir, "main", repo.main_root) == []
    msg = gitio.git("log", "-1", "--format=%B", cwd=repo.main_root).out
    assert msg.startswith(f"Merge w/{A}: add a quote summary") and "Audit: PASS" in msg
    assert [e["event"] for e in events.read(repo.store)][-1] == "merge"


def test_conflict_aborts_and_leaves_main_untouched(repo, env):
    good_branch(repo, env)
    ready(repo)
    write_files(repo.main_root, {"print_quotes/summary.py": "# main wrote this first\n"})
    commit_all(repo.main_root, "main side", env)
    before = main_head(repo)
    with pytest.raises(MergeRefused, match="conflict"):
        merge(repo, A)
    assert main_head(repo) == before and gitio.is_clean_tracked(repo.main_root)
    assert not (repo.common_dir / "MERGE_HEAD").exists()


def test_branch_check_failure_is_not_merged(repo, env):
    w = good_branch(repo, env)
    write_files(w, {"tests/test_summary.py": TEST.replace("2.40", "9.99")})
    commit_all(w, "wrong", env)
    head = gitio.rev(f"refs/heads/w/{A}", repo.main_root)
    # an audit record that did not run the check (e.g. written by another tool) makes merge run it on the branch
    rec = {"topic": A, "head": head, "base": "0" * 40, "verdict": "PASS", "checks": [], "check": {"status": "skipped", "sha": head},
           "reviewer": "stub", "reviewer_ran": True, "brief_sha256": None, "findings": [], "utc": "2026-01-01T00:00:00Z"}
    record.save(repo.store, A, rec)
    approve(repo, A, confirm=yes)
    before = main_head(repo)
    with pytest.raises(MergeRefused, match="branch-check-failed"):
        merge(repo, A)
    assert main_head(repo) == before


def test_main_check_failure_rolls_back_and_keeps_untracked_files(repo, env):
    good_branch(repo, env)
    rename_branch(repo, env)  # opened before add-summary merges: its base has no caller of total()
    ready(repo)
    assert merge(repo, A).code == 0
    ready(repo, B)
    (repo.main_root / "scratch notes.txt").write_text("mine", encoding="utf-8")
    before = main_head(repo)
    result = merge(repo, B)
    assert result.code == 1 and f"rolled back to {before[:8]}" in result.message
    assert main_head(repo) == before and gitio.is_clean_tracked(repo.main_root)
    assert (repo.main_root / "scratch notes.txt").read_text() == "mine"
    assert repo.store.load_claim(B) is not None  # still open for the follow-up
    assert [e["event"] for e in events.read(repo.store)][-1] == "rollback"


def test_rollback_refused_by_a_local_change_keeps_it_and_names_the_manual_step(repo, env):
    good_branch(repo, env)
    rename_branch(repo, env)
    ready(repo)
    merge(repo, A)
    ready(repo, B)
    before = main_head(repo)

    def local_edit(r):
        p = r.main_root / "print_quotes" / "totals.py"
        p.write_text(p.read_text() + "# a local edit\n", encoding="utf-8")

    result = merge(repo, B, after_merge=local_edit)
    assert result.code == 1
    assert "rollback was refused: local changes" in result.message and f"git reset --keep {before[:8]}" in result.message
    assert (repo.main_root / "print_quotes" / "totals.py").read_text().endswith("# a local edit\n")
    assert main_head(repo) != before


def test_head_moved_after_the_audit_is_refused_as_stale(repo, env):
    w = good_branch(repo, env)
    ready(repo)
    write_files(w, {"tests/test_summary.py": TEST + "\n"})
    commit_all(w, "after the audit", env)
    with pytest.raises(MergeRefused, match="stale-audit"):
        merge(repo, A)


def test_no_approval_is_refused_and_a_new_commit_voids_one(repo, env):
    w = good_branch(repo, env)
    audit(repo, A, StubReviewer())
    with pytest.raises(MergeRefused, match="no-approval"):
        merge(repo, A)
    approve(repo, A, confirm=yes)
    write_files(w, {"tests/test_summary.py": TEST + "\n"})
    commit_all(w, "after approval", env)
    audit(repo, A, StubReviewer())
    with pytest.raises(MergeRefused, match="no-approval"):
        merge(repo, A)


def test_approval_off_merges_without_one(repo, env):
    good_branch(repo, env)
    init.set_settings(repo, ["require_approval=false"], confirm=yes)
    repo = worktrees.open_repo(repo.main_root)
    audit(repo, A, StubReviewer())
    assert merge(repo, A).code == 0


def test_block_verdict_is_refused_and_approve_needs_a_pass(repo, env):
    w = good_branch(repo, env)
    write_files(w, {"print_quotes/pricing.py": "# escape\n"})
    commit_all(w, "escape", env)
    audit(repo, A, StubReviewer())
    with pytest.raises(worktrees.Refused, match="current PASS"):
        approve(repo, A, confirm=yes)
    with pytest.raises(MergeRefused, match="no-pass"):
        merge(repo, A)


def test_dirty_main_or_worktree_is_refused(repo, env):
    w = good_branch(repo, env)
    ready(repo)
    (w / "print_quotes" / "summary.py").write_text("# uncommitted\n", encoding="utf-8")
    with pytest.raises(MergeRefused, match="worktree-dirty"):
        merge(repo, A)
    gitio.git("checkout", "--", "print_quotes/summary.py", cwd=w)
    (repo.main_root / "README.md").write_text("x", encoding="utf-8")
    (repo.main_root / "print_quotes" / "pricing.py").write_text("# dirty\n", encoding="utf-8")
    with pytest.raises(MergeRefused, match="main-dirty"):
        merge(repo, A)


def test_trust_changed_refuses_the_merge(repo, env):
    good_branch(repo, env)
    ready(repo)
    (repo.common_dir / "hooks" / "post-merge").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    with pytest.raises(MergeRefused, match="trust-changed"):
        merge(repo, A)


def test_a_second_concurrent_merge_is_refused_by_the_lock(repo, env):
    good_branch(repo, env)
    ready(repo)
    with repo.store.lock("merge"):
        with pytest.raises(LockBusy):
            merge(repo, A)


def test_a_stale_lock_with_a_dead_pid_is_reported_not_broken(repo, env):
    good_branch(repo, env)
    ready(repo)
    lock = repo.store.path("merge.lock")
    lock.write_text(json.dumps({"pid": 2 ** 22 + 12345, "utc": "2026-01-01T00:00:00Z"}), encoding="utf-8")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    with pytest.raises(LockStale, match="merge.lock"):
        merge(repo, A)
    assert lock.exists()


def overwrite(path, text):
    """Write in place: git marks a worktree's .git file hidden on Windows, and a plain 'w' open of a hidden file is refused."""
    with open(path, "r+", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.truncate()


def test_a_redirected_worktree_git_file_is_refused_and_its_filter_never_runs(repo, env, tmp_path):
    """Hardening: a worker points <wt>/.git at its own git dir with a filter driver; main's git calls for that
    tree (list, merge, end, session start) must not follow it."""
    import sys

    from agent_build_loop.hooks import session_start

    w = good_branch(repo, env)
    ready(repo)
    marker = tmp_path / "filter ran.txt"
    script = tmp_path / "plant.py"
    script.write_text(f"open({str(marker)!r}, 'w').close()\nimport sys\nsys.stdout.write(sys.stdin.read())\n", encoding="utf-8")
    evil = tmp_path / "evil git"
    gitio.git("init", "-q", "-b", "main", str(evil), cwd=tmp_path, env_extra=env)
    cmd = f'"{sys.executable.replace(chr(92), "/")}" "{script.as_posix()}"'
    for key in ("filter.x.clean", "filter.x.smudge"):
        gitio.git("config", "--file", str(evil / ".git" / "config"), key, cmd, cwd=tmp_path)
    original = (w / ".git").read_text(encoding="utf-8")
    overwrite(w / ".git", f"gitdir: {(evil / '.git').as_posix()}\n")
    (w / ".gitattributes").write_text("* filter=x\n", encoding="utf-8")

    with pytest.raises(worktrees.WorktreeRedirected):
        worktrees.list_rows(repo)
    with pytest.raises(MergeRefused, match="worktree-redirected"):
        merge(repo, A)
    with pytest.raises(worktrees.Refused):  # unmerged, or redirected: either way it never runs git in the tree
        worktrees.end(repo, A)
    text = session_start.main_text(type("Ctx", (), {"repo": repo})())
    assert "does not point at its own admin folder" in text
    assert not marker.exists()

    # control: plain git in that folder follows the planted file and runs its filter (add always cleans; status may not)
    gitio.git("add", "-A", cwd=w, check=False)
    assert marker.exists()
    overwrite(w / ".git", original)


def test_approve_refuses_when_trust_changed(repo, env):
    """Edge case: approve checks trust first, so a planted hook never rides on the person's approval."""
    good_branch(repo, env)
    audit(repo, A, StubReviewer())
    (repo.common_dir / "hooks" / "post-merge").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    with pytest.raises(worktrees.Refused, match="trust-changed"):
        approve(repo, A, confirm=yes)
    assert not repo.store.path("approvals", f"{A}.json").exists()


def project_blob(repo):
    return json.loads(repo.store.path("trust.json").read_text(encoding="utf-8"))["parts"]["project_config"]


def test_a_branch_changing_abl_toml_is_blocked_by_the_audit_with_default_shared(repo, env):
    w = good_branch(repo, env)
    write_files(w, {".abl.toml": 'check = ["python", "-c", "pass"]\n'})
    commit_all(w, "loosen the check", env)
    rec = audit(repo, A, StubReviewer())
    assert rec["verdict"] == "BLOCK" and dict(rec["checks"])["claim-escape"] == "block" and dict(rec["checks"])["shared-path"] == "block"
    with pytest.raises(MergeRefused, match="no-pass"):
        merge(repo, A)


def test_a_branch_changing_abl_toml_is_refused_even_when_shared_leaves_it_out(repo, env):
    """Edge case: a project whose `shared` omits .abl.toml lets a claim own it; the audit passes, so merge refuses
    `project-config` itself (before the fix it merged and re-recorded the new config as trusted)."""
    root = repo.main_root
    write_files(root, {".abl.toml": 'check = ["python", "-m", "unittest", "-q"]\nshared = []\n'})
    commit_all(root, "person: no shared paths", env)
    trust.record(repo.store, repo.common_dir, "main")  # the person looked (abl init --rehash)
    repo = worktrees.open_repo(root)
    w = open_worker(repo, A, ("print_quotes/summary.py", "tests/test_summary.py", ".abl.toml"))
    from .test_audit import HANDOFF, SUMMARY

    write_files(w, {"print_quotes/summary.py": SUMMARY, "tests/test_summary.py": TEST, f"handoff/{A}.md": HANDOFF,
                    ".abl.toml": 'check = ["python", "-c", "pass"]\nshared = []\n'})
    commit_all(w, "summary + a looser check", env)
    ready(repo)
    before, trusted = main_head(repo), project_blob(repo)
    with pytest.raises(MergeRefused, match="project-config"):
        merge(repo, A)
    assert main_head(repo) == before and project_blob(repo) == trusted
    assert trust.changed(repo.store, repo.common_dir, "main", root) == []


def test_an_unchanged_abl_toml_leaves_the_trust_record_as_it_was(repo, env):
    good_branch(repo, env)
    ready(repo)
    trusted = project_blob(repo)
    assert merge(repo, A).code == 0
    assert project_blob(repo) == trusted


def criss_cross(repo, env):
    """Hardening: two merge bases; `git merge-base` alone may pick the one where .abl.toml looks unchanged."""
    from agent_build_loop import evals

    root = repo.main_root
    write_files(root, {".abl.toml": 'check = ["python", "-m", "unittest", "-q"]\ncheck_timeout_s = 120\nshared = []\n'})
    commit_all(root, "person: no shared paths", env)
    trust.record(repo.store, repo.common_dir, "main")
    repo = worktrees.open_repo(root)
    evals.criss_cross_history(repo)  # same git environment as the trust record (the repo has its own identity)
    head = gitio.rev(f"refs/heads/w/{A}", root)
    bases = gitio.git("merge-base", "--all", main_head(repo), head, cwd=root).out.split()
    assert len(bases) == 2
    assert any(gitio.blob_sha(b, ".abl.toml", root) == gitio.blob_sha(head, ".abl.toml", root) for b in bases)
    repo = worktrees.open_repo(root)
    ready(repo)
    return repo


def test_a_criss_cross_history_that_reverts_abl_toml_is_refused(repo, env):
    repo = criss_cross(repo, env)
    before, blob = main_head(repo), gitio.blob_sha("refs/heads/main", ".abl.toml", repo.main_root)
    with pytest.raises(MergeRefused, match="project-config"):
        merge(repo, A)
    assert main_head(repo) == before and gitio.blob_sha("refs/heads/main", ".abl.toml", repo.main_root) == blob


def test_a_merge_that_changes_main_abl_toml_anyway_is_rolled_back(repo, env, monkeypatch):
    """The second line: whatever the base check says, a merge commit that changes main's .abl.toml is undone."""
    from agent_build_loop import merge as mergelib

    repo = criss_cross(repo, env)
    monkeypatch.setattr(mergelib, "branch_changes_project_config", lambda *a: False)
    before = main_head(repo)
    with pytest.raises(MergeRefused, match=f"project-config: .*rolled back to {before[:8]}"):
        merge(repo, A)
    assert main_head(repo) == before and gitio.is_clean_tracked(repo.main_root)
    assert trust.changed(repo.store, repo.common_dir, "main", repo.main_root) == []


def test_record_change_refuses_a_new_project_config(repo, env):
    """A merge no longer hands record_change an accepted .abl.toml blob: any change there leaves the record as it was."""
    write_files(repo.main_root, {".abl.toml": 'check = ["python", "-c", "pass"]\n'})
    commit_all(repo.main_root, "main side", env)
    saved = repo.store.path("trust.json").read_bytes()
    with pytest.raises(trust.TrustChanged, match="project config"):
        trust.record_change(repo.store, repo.common_dir, "main", repo.main_root)
    assert repo.store.path("trust.json").read_bytes() == saved


def test_merge_refusals_are_logged(repo, env):
    good_branch(repo, env)
    audit(repo, A, StubReviewer())
    with pytest.raises(MergeRefused):
        merge(repo, A)
    last = events.read(repo.store)[-1]
    assert last["event"] == "merge-refused" and last["detail"] == {"rule": "no-approval"}
    assert not repo.store.path("merge.lock").exists()
