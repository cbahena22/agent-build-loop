"""`abl merge <topic>`: the merge gate. Fresh audit + approval for the pinned head, check on the branch and on main,
automatic rollback with `git reset --keep` when main's check fails. Never pushes."""
from dataclasses import dataclass

from agent_build_loop import events, gitio, trust, worktrees
from agent_build_loop.audit import checks, record
from agent_build_loop.paths import validate_topic


class MergeRefused(worktrees.Refused):
    def __init__(self, rule: str, message: str):
        super().__init__(f"{rule}: {message}")
        self.rule = rule


@dataclass
class MergeResult:
    code: int
    message: str


def merge(repo: worktrees.Repo, topic: str, *, after_merge=None) -> MergeResult:
    """`after_merge(repo)` runs between the merge commit and main's check (tests use it to simulate a local edit)."""
    validate_topic(topic)
    repo.store.ensure()
    with repo.store.lock("merge"):
        try:
            return _merge_locked(repo, topic, after_merge)
        except MergeRefused as exc:
            events.append(repo.store, "main", "merge-refused", topic, {"rule": exc.rule})
            raise
        except Exception:
            events.append(repo.store, "main", "merge-error", topic, None)
            raise


def branch_changes_project_config(root, before: str, head: str) -> bool:
    """True when `.abl.toml` at `head` differs from any merge base (every base: a criss-cross history has several)."""
    bases = gitio.git("merge-base", "--all", before, head, cwd=root, check=False).out.split()
    want = gitio.blob_sha(head, ".abl.toml", root)
    return not bases or any(gitio.blob_sha(b, ".abl.toml", root) != want for b in bases)


def _merge_locked(repo, topic, after_merge) -> MergeResult:
    root = repo.main_root
    main_ref = f"refs/heads/{repo.main_branch}"
    changed = trust.changed(repo.store, repo.common_dir, repo.main_branch, root)
    if changed:
        raise MergeRefused("trust-changed", trust.explain(changed))
    claim = repo.store.load_claim(topic)
    if claim is None:
        raise MergeRefused("no-claim", f"no open claim for {topic}")
    on = gitio.git("symbolic-ref", "-q", "HEAD", cwd=root, check=False).out.strip()
    if on != main_ref:
        raise MergeRefused("not-on-main", f"the main checkout is not on {repo.main_branch}")
    if not gitio.is_clean_tracked(root):
        raise MergeRefused("main-dirty", f"{repo.main_branch} has uncommitted tracked changes")
    wt = root / claim.worktree
    try:
        dirty = wt.is_dir() and worktrees.worktree_dirty(repo, wt)
    except worktrees.WorktreeRedirected as exc:
        raise MergeRefused("worktree-redirected", str(exc)) from exc
    if dirty:
        raise MergeRefused("worktree-dirty", f"the worktree for {topic} has uncommitted changes; the worker commits first")
    head = gitio.rev(f"refs/heads/{claim.branch}", root)
    if head is None:
        raise MergeRefused("no-branch", f"branch {claim.branch} is gone")
    code, line = record.status(repo, topic)
    if code == 2:
        raise MergeRefused("stale-audit", line)
    if code != 0:
        raise MergeRefused("no-pass", line)
    rec = record.load(repo.store, topic)
    if repo.settings["require_approval"]:
        approval = repo.store.read_record("approvals", topic)
        if not approval or approval.get("head") != head:
            raise MergeRefused("no-approval", f"the person has not approved {topic} at {head[:8]} (abl approve {topic}, at their terminal)")
    before = gitio.rev(main_ref, root)
    project = gitio.blob_sha(before, ".abl.toml", root)
    if branch_changes_project_config(root, before, head):
        raise MergeRefused("project-config", f"{claim.branch} changes .abl.toml; it is changed only on {repo.main_branch} with no claims open")
    if not (rec["check"].get("status") == "PASS" and rec["check"].get("sha") == head):
        if checks.run_check(repo, head) != "PASS":
            raise MergeRefused("branch-check-failed", f"the check failed on {claim.branch} at {head[:8]}; not merged")
    message = f"Merge {claim.branch}: {claim.goal}\n\nAudit: {rec['verdict']} {head[:8]}"
    r = gitio.git("merge", "--no-ff", "--no-edit", "--no-log", "--no-stat", "-m", message, "--end-of-options", head, cwd=root, check=False)
    if r.code != 0:
        gitio.git("merge", "--abort", cwd=root, check=False)
        now = gitio.rev(main_ref, root)
        extra = "" if now == before else f"; check main: it is at {(now or '-')[:8]}, expected {before[:8]}"
        raise MergeRefused("conflict", f"{claim.branch} does not merge cleanly into {repo.main_branch}; the merge was aborted{extra}")
    merged = gitio.rev(main_ref, root)
    if gitio.blob_sha(merged, ".abl.toml", root) != project:
        # the merge itself changed main's .abl.toml (a history the base check did not foresee): undo it
        back = gitio.git("reset", "--keep", "--end-of-options", before, cwd=root, check=False)
        undone = back.code == 0 and gitio.rev(main_ref, root) == before
        raise MergeRefused("project-config", f"merging {claim.branch} changed .abl.toml on {repo.main_branch}; " +
                           (f"rolled back to {before[:8]}" if undone else f"the rollback was refused; run: git reset --keep {before[:8]}"))
    if after_merge is not None:
        after_merge(repo)
    if checks.run_check(repo, merged) != "PASS":
        back = gitio.git("reset", "--keep", "--end-of-options", before, cwd=root, check=False)
        if back.code == 0 and gitio.rev(main_ref, root) == before:
            events.append(repo.store, "main", "rollback", topic, {"merge": merged[:8], "to": before[:8]})
            return MergeResult(1, f"merged, but the check failed on {repo.main_branch}; rolled back to {before[:8]}")
        events.append(repo.store, "main", "rollback-refused", topic, {"merge": merged[:8], "to": before[:8]})
        return MergeResult(1, f"the check failed on {repo.main_branch} and the rollback was refused: local changes; "
                              f"{repo.main_branch} is at the merge commit {merged[:8]}; save them, then run: git reset --keep {before[:8]}")
    with repo.store.lock("claims"):
        name = worktrees.close(repo, claim)
        repo.store.path("approvals", f"{topic}.json").unlink(missing_ok=True)
    events.append(repo.store, "main", "merge", topic, {"head": head[:8], "merge": merged[:8]})
    note = ""
    try:
        worktrees.record_own_change(repo, removed=(name,) if name else ())
    except worktrees.Refused as exc:
        note = f"\n{exc}"
    return MergeResult(0, f"merged {topic} at {merged[:8]}; check PASS on {repo.main_branch}; claim closed\n"
                          f"undo: git revert -m 1 {merged[:8]}{note}")
