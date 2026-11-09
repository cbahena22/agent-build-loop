"""Audit records: `audits/<topic>.json` (bound to one head sha) and a readable `.md`; `abl verdict`."""
import json

from agent_build_loop import gitio
from agent_build_loop.paths import validate_topic
from agent_build_loop.state import Store, atomic_write

PASSING = ("PASS", "PASS-WITH-NOTES")


def save(store: Store, topic: str, rec: dict) -> None:
    validate_topic(topic)
    store.ensure()
    atomic_write(store.path("audits", f"{topic}.json"), json.dumps(rec, indent=2, sort_keys=True, ensure_ascii=True) + "\n")
    atomic_write(store.path("audits", f"{topic}.md"), render_md(rec))


def load(store: Store, topic: str) -> dict | None:
    return store.read_record("audits", topic)


def render_md(rec: dict) -> str:
    lines = [f"# Audit: {rec['topic']}", "", f"- Verdict: **{rec['verdict']}**", f"- Head: `{rec['head'][:12]}`  base: `{rec['base'][:12]}`",
             f"- Reviewer: {rec['reviewer']}{'' if rec.get('reviewer_ran') else ' (not run: a deterministic check blocked)'}",
             f"- Brief sha256: `{rec.get('brief_sha256') or '-'}`", f"- Written: {rec['utc']}", "", "## Checks", ""]
    lines += [f"- {cid}: {status}" for cid, status in rec["checks"]]
    lines += ["", "## Findings", ""]
    lines += [f"- {f['path']}: {f['reason']}" for f in rec["findings"]] or ["- (none)"]
    return "\n".join(lines) + "\n"


def status(repo, topic: str) -> tuple[int, str]:
    """(exit code, line) for `abl verdict`: 0 a current PASS / PASS-WITH-NOTES, 1 BLOCK, 2 missing or stale."""
    rec = load(repo.store, topic)
    claim = repo.store.load_claim(topic)
    if rec is None:
        return 2, f"{topic}: no audit yet"
    head = gitio.rev(f"refs/heads/w/{topic}", repo.main_root) if claim else None
    if head is None:
        return 2, f"{topic}: no open branch"
    if rec.get("head") != head:
        return 2, f"{topic}: stale: the audit is for {str(rec.get('head'))[:8]}, the branch is at {head[:8]}; run abl audit {topic}"
    verdict = rec.get("verdict")
    if verdict in PASSING:
        return 0, f"{topic}: {verdict} for {head[:8]}"
    return 1, f"{topic}: {verdict} for {head[:8]}"
