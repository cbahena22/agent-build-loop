"""Worktrees and claims: `new`, `claim --add`, `list`, `end`. Every git call goes through gitio."""
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from agent_build_loop import claims as claimlib
from agent_build_loop import config, events, gitio, settings, trust
from agent_build_loop.claims import Claim
from agent_build_loop.paths import PathRuleError, validate_topic
from agent_build_loop.state import Store, utc_now

EXCLUDE_LINES = (".worktrees/", ".claude/settings.local.json")


class Refused(Exception):
    """A rule said no (exit 1). The message is safe to print."""


@dataclass
class Repo:
    main_root: Path
    common_dir: Path
    store: Store
    settings: dict
    cfg: config.ProjectConfig

    @property
    def main_branch(self) -> str:
        return self.settings["main_branch"]


def open_repo(cwd) -> Repo:
    """Find the repository and read the person settings and `.abl.toml`. Runs before any trust check, so it uses only
    `rev-parse` and `cat-file blob` (with hooks and fsmonitor off): no git hook, filter, textconv or fsmonitor
    program can run here, whatever a worker planted in the git dir."""
    common = Path(gitio.common_dir(cwd))
    if common.name != ".git":
        raise Refused("only a normal repository (a .git folder in the main checkout) is supported; not a bare or separate git dir")
    store = Store(common)
    st = store.load_settings()
    cfg = config.load(common, st["main_branch"])
    return Repo(main_root=common.parent, common_dir=common, store=store, settings=st, cfg=cfg)


def whoami(cwd, repo: Repo) -> str:
    """`main`, or the topic when `cwd` is inside `.worktrees/<topic>`. A mistake guard, not authorization."""
    try:
        rel = Path(os.path.realpath(cwd)).relative_to(os.path.realpath(repo.main_root / ".worktrees"))
    except ValueError:
        return "main"
    return rel.parts[0] if rel.parts else "main"


def require_main(cwd, repo: Repo) -> None:
    role = whoami(cwd, repo)
    if role != "main":
        raise Refused(f"this command is for the main session; you are in the worktree for {role}")


def ensure_exclude(common_dir: Path) -> None:
    """Keep worktrees and the per-worktree hook file out of `git status` (the untracked, local-only exclude file)."""
    path = common_dir / "info" / "exclude"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    missing = [line for line in EXCLUDE_LINES if line not in existing]
    if missing:
        text = "\n".join(existing + missing) + "\n"
        path.write_text(text, encoding="utf-8", newline="\n")


def _main_clean(repo: Repo) -> None:
    if not gitio.is_clean_tracked(repo.main_root):
        raise Refused(f"{repo.main_branch} has uncommitted tracked changes; commit or stash them first")


def require_trust(repo: Repo) -> None:
    """Refuse before any git call when files outside the branches changed, or nothing was ever trusted."""
    if not repo.store.path("trust.json").exists():
        raise Refused("nothing is trusted yet: the person runs abl init first")
    diff = trust.changed(repo.store, repo.common_dir, repo.main_branch, repo.main_root)
    if diff:
        raise Refused(trust.explain(diff))


def record_own_change(repo: Repo, **change) -> None:
    """Re-record trust accepting only this command's own change (see `trust.record_change`)."""
    try:
        trust.record_change(repo.store, repo.common_dir, repo.main_branch, repo.main_root, **change)
    except trust.TrustChanged as exc:
        raise Refused(f"done, but something else changed meanwhile, so trust was not re-recorded. {exc}") from exc


class WorktreeRedirected(Refused):
    """A worker tree's `.git` file no longer points at its own admin folder (it could make git run a planted filter)."""


def _same(a, b) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def admin_dir(repo: Repo, worktree: Path) -> Path | None:
    """The real admin folder of a linked worktree, found from the common dir side (its `gitdir` back-pointer),
    never by asking git inside the tree: `<worktree>/.git` is a file the worker can rewrite."""
    root = repo.common_dir / "worktrees"
    if not root.is_dir():
        return None
    found = []
    for d in sorted(root.iterdir()):
        try:
            back = (d / "gitdir").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if back and _same(back, worktree / ".git"):
            found.append(d)
    return found[0] if len(found) == 1 else None


def admin_name(repo: Repo, worktree: Path) -> str | None:
    d = admin_dir(repo, worktree)
    return d.name if d is not None else None


def worktree_git(repo: Repo, worktree: Path, *args: str, check: bool = True) -> gitio.Result:
    """Run git for a worker tree as the main session: `--git-dir` = its real admin folder (whose config the trust
    record covers), `--work-tree` = the tree. Refuses when the tree's `.git` file points anywhere else."""
    admin = admin_dir(repo, worktree)
    rel = worktree.relative_to(repo.main_root).as_posix() if worktree.is_relative_to(repo.main_root) else worktree.name
    dotgit = worktree / ".git"
    try:
        text = dotgit.read_text(encoding="utf-8").strip() if dotgit.is_file() and not dotgit.is_symlink() else ""
    except OSError:
        text = ""
    if admin is None or not text.startswith("gitdir:") or not _same(text[len("gitdir:"):].strip(), admin):
        raise WorktreeRedirected(f"the .git file of {rel} does not point at its own admin folder; look at it, then the person runs: "
                                 f"abl end {worktree.name} --discard")
    return gitio.git(f"--git-dir={admin}", f"--work-tree={worktree}", *args, cwd=worktree, check=check)


def worktree_dirty(repo: Repo, worktree: Path) -> bool:
    return worktree_git(repo, worktree, "status", "--porcelain").out.strip() != ""


def new(repo: Repo, topic: str, owns: list[str], goal: str, model: str = "", effort: str = "") -> tuple[Claim, str]:
    validate_topic(topic)
    repo.store.ensure()
    with repo.store.lock("claims"):
        require_trust(repo)
        try:
            owns = claimlib.validate_new(topic, goal, owns, repo.cfg.shared, repo.store.load_claims())
        except PathRuleError as exc:
            raise Refused(str(exc)) from exc
        _main_clean(repo)
        base = gitio.rev(f"refs/heads/{repo.main_branch}", repo.main_root)
        if base is None:
            raise Refused(f"branch {repo.main_branch} has no commits")
        branch = f"w/{topic}"
        rel = f".worktrees/{topic}"
        if gitio.rev(f"refs/heads/{branch}", repo.main_root) is not None or (repo.main_root / rel).exists():
            raise Refused(f"branch or folder for {topic} already exists")
        ensure_exclude(repo.common_dir)
        gitio.git("worktree", "add", "-b", branch, "--", rel, base, cwd=repo.main_root)
        claim = Claim(topic=topic, branch=branch, worktree=rel, owns=owns, goal=goal, model=model, effort=effort, created_utc=utc_now(), base_sha=base)
        try:
            claim = Claim.from_json(claim.to_json())  # the same strict check every reader applies
            repo.store.save_claim(claim)
            settings.write_hooks(repo.main_root / rel, topic)
        except Exception:
            gitio.git("worktree", "remove", "--force", "--", rel, cwd=repo.main_root, check=False)
            gitio.git("branch", "-D", "--", branch, cwd=repo.main_root, check=False)
            repo.store.remove_claim(topic)
            raise
        events.append(repo.store, "main", "claim-open", topic, {"owns": owns})
        record_own_change(repo, added=(admin_name(repo, repo.main_root / rel) or topic,))
    hint = f"cd {rel}   then run:  claude"
    return claim, hint


def claim_add(repo: Repo, topic: str, patterns: list[str]) -> Claim:
    validate_topic(topic)
    repo.store.ensure()
    with repo.store.lock("claims"):
        require_trust(repo)
        claim = repo.store.load_claim(topic)
        if claim is None:
            raise Refused(f"no open claim for {topic}")
        others = repo.store.load_claims()
        try:
            merged = claimlib.check_owns(claim.owns + list(patterns), repo.cfg.shared, others, topic=topic)
        except PathRuleError as exc:
            raise Refused(str(exc)) from exc
        claim.owns = merged
        repo.store.save_claim(Claim.from_json(claim.to_json()))
        events.append(repo.store, "main", "claim-add", topic, {"owns": list(patterns)})
        # no git work here, so nothing to re-record: the checked record stays as it was
    return claim


@dataclass
class Row:
    topic: str
    branch: str
    head: str
    ahead: int
    dirty: bool
    verdict: str
    verdict_current: bool
    approved: bool


def list_rows(repo: Repo) -> list[Row]:
    """Rows for `abl list` and the handover. Runs `git status` in every worker tree, so it checks trust first."""
    require_trust(repo)
    rows = []
    for c in repo.store.load_claims():
        wt = repo.main_root / c.worktree
        head = gitio.rev(f"refs/heads/{c.branch}", repo.main_root) or ""
        ahead = 0
        if head:
            out = gitio.git("rev-list", "--count", f"refs/heads/{repo.main_branch}..{head}", cwd=repo.main_root, check=False).out.strip()
            ahead = int(out) if out.isdigit() else 0
        dirty = wt.is_dir() and worktree_dirty(repo, wt)
        audit = repo.store.read_record("audits", c.topic)
        approval = repo.store.read_record("approvals", c.topic)
        verdict = str(audit.get("verdict", "none")) if audit else "none"
        rows.append(
            Row(
                topic=c.topic,
                branch=c.branch,
                head=head[:8],
                ahead=ahead,
                dirty=dirty,
                verdict=verdict,
                verdict_current=bool(audit and head and audit.get("head") == head),
                approved=bool(approval and head and approval.get("head") == head),
            )
        )
    return rows


def format_rows(rows: list[Row]) -> str:
    if not rows:
        return "no open claims"
    head = ["topic", "branch", "head", "ahead", "dirty", "audit", "current", "approved"]
    body = [[r.topic, r.branch, r.head or "-", str(r.ahead), "yes" if r.dirty else "no", r.verdict,
             "yes" if r.verdict_current else "no", "yes" if r.approved else "no"] for r in rows]
    widths = [max(len(x[i]) for x in [head] + body) for i in range(len(head))]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in [head] + body)


def end(repo: Repo, topic: str, discard: bool = False) -> str:
    """Remove the worktree, branch and claim. An unmerged branch needs `discard` (the CLI makes that person-only)."""
    validate_topic(topic)
    with repo.store.lock("claims"):
        require_trust(repo)
        claim = repo.store.load_claim(topic)
        if claim is None:
            raise Refused(f"no open claim for {topic}")
        head = gitio.rev(f"refs/heads/{claim.branch}", repo.main_root)
        main_sha = gitio.rev(f"refs/heads/{repo.main_branch}", repo.main_root)
        wt = repo.main_root / claim.worktree
        merged = head is None or (
            main_sha is not None
            and gitio.git("merge-base", "--is-ancestor", head, main_sha, cwd=repo.main_root, check=False).code == 0
        )
        if not merged and not discard:
            raise Refused(f"{claim.branch} has commits that are not on {repo.main_branch}; merge it, or the person runs: abl end {topic} --discard")
        if not discard and wt.is_dir() and worktree_dirty(repo, wt):
            raise Refused(f"the worktree for {topic} has uncommitted changes")
        name = close(repo, claim)
        events.append(repo.store, "person" if discard else "main", "claim-end", topic, {"discard": discard})
        record_own_change(repo, removed=(name,) if name else ())
    return f"ended {topic}"


def close(repo: Repo, claim: Claim) -> str | None:
    """Remove a claim's worktree, branch and claim file (callers hold claims.lock and checked the rules).

    Returns the worktree's admin folder name, for `record_own_change(removed=...)`."""
    wt = repo.main_root / claim.worktree
    name = admin_name(repo, wt)
    if wt.is_dir():
        gitio.git("worktree", "remove", "--force", "--", claim.worktree, cwd=repo.main_root)
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
    gitio.git("worktree", "prune", cwd=repo.main_root, check=False)
    if gitio.rev(f"refs/heads/{claim.branch}", repo.main_root) is not None:
        gitio.git("branch", "-D", "--", claim.branch, cwd=repo.main_root)
    repo.store.remove_claim(claim.topic)
    return name
