"""`abl audit <topic>`: deterministic checks first, then the reviewer on a fresh-context brief, bound to one head sha."""
from agent_build_loop import events, gitio, trust, worktrees
from agent_build_loop.audit import checks, record
from agent_build_loop.audit.brief import AuditBrief, clean_diff, load_checklist
from agent_build_loop.audit.reviewer import Reviewer
from agent_build_loop.paths import validate_topic
from agent_build_loop.state import utc_now


def _finding(path: str, reason: str) -> dict:
    return {"path": path, "reason": reason}


def audit(repo: worktrees.Repo, topic: str, reviewer: Reviewer) -> dict:
    """Run the audit and write its record. Returns the record. ReviewerError (a replay miss) records nothing."""
    validate_topic(topic)
    claim = repo.store.load_claim(topic)
    if claim is None:
        raise worktrees.Refused(f"no open claim for {topic}")
    head = gitio.rev(f"refs/heads/{claim.branch}", repo.main_root)
    if head is None:
        raise worktrees.Refused(f"branch {claim.branch} is gone")
    base = claim.base_sha
    rec = {"topic": topic, "head": head, "base": base, "reviewer": reviewer.name, "reviewer_ran": False,
           "brief_sha256": None, "check": {"status": "skipped", "sha": head}, "utc": utc_now()}

    changed = trust.changed(repo.store, repo.common_dir, repo.main_branch, repo.main_root)
    if changed:
        rec.update(verdict="BLOCK", checks=[["trust-changed", "block"]], findings=[_finding("-", trust.explain(changed))])
        return _finish(repo, topic, rec)

    found: list[list[str]] = [["trust-changed", "pass"]]
    findings: list[dict] = []
    count = checks.commit_count(repo, base, head)
    found.append(["no-commits", "block" if count == 0 else "pass"])
    if count == 0:
        findings.append(_finding("-", "the branch has no commits after its base"))
    paths = checks.changed_paths(repo, base, head)
    escape = checks.outside_claim(claim, paths)
    found.append(["claim-escape", "block" if escape else "pass"])
    findings += [_finding(p, "changed outside the claim") for p in escape]
    shared = checks.shared_hits(repo.cfg, paths)
    found.append(["shared-path", "block" if shared else "pass"])
    findings += [_finding(p, "a shared path; only the main session changes it") for p in shared]
    diff = checks.diff_text(repo, base, head)
    secrets = checks.secret_lines(diff)
    found.append(["secret-shape", "block" if secrets else "pass"])
    if secrets:
        findings.append(_finding("-", f"{secrets} added line(s) look like a key or secret (not shown)"))
    handoff = gitio.show_blob(head, f"handoff/{topic}.md", repo.main_root)
    ok = checks.handoff_ok(handoff, topic)
    found.append(["handoff-missing", "pass" if ok else "block"])
    if not ok:
        findings.append(_finding(f"handoff/{topic}.md", f"missing, or its last line is not READY TO MERGE: {topic}"))
    if any(status == "block" for _c, status in found):
        found.append(["check-failed", "skipped"])
    else:
        result = checks.run_check(repo, head)
        rec["check"]["status"] = result
        found.append(["check-failed", "pass" if result == "PASS" else "block"])
        if result != "PASS":
            findings.append(_finding("-", "the check command failed on a clean checkout of the head"))

    brief = AuditBrief(topic=topic, goal=claim.goal, owns=tuple(claim.owns), diff=clean_diff(diff),
                       handoff=handoff or "", checks=tuple((c, s) for c, s in found), checklist=load_checklist())
    rec["brief_sha256"] = brief.sha256
    rec["checks"] = found
    if any(status == "block" for _c, status in found):
        rec.update(verdict="BLOCK", findings=findings)
    else:
        result = reviewer.review(brief)  # may raise ReviewerError: nothing is recorded
        rec.update(verdict=result.verdict, reviewer_ran=True, findings=findings + [_finding(f.path, f.reason) for f in result.findings])
    return _finish(repo, topic, rec)


def _finish(repo, topic: str, rec: dict) -> dict:
    record.save(repo.store, topic, rec)
    events.append(repo.store, "main", "audit", topic, {"verdict": rec["verdict"], "head": rec["head"][:8]})
    return rec
