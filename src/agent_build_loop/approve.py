"""`abl approve <topic>`: the person's approval for one head sha. Needs a terminal and a retype of the head's 8 chars."""
import json

from agent_build_loop import events, gitio, person, worktrees
from agent_build_loop.audit import record
from agent_build_loop.paths import validate_topic
from agent_build_loop.state import atomic_write, utc_now


def approve(repo: worktrees.Repo, topic: str, confirm=person.require_person) -> str:
    """Record `{head, utc}` for the branch's current head. A new commit voids it (merge compares heads)."""
    validate_topic(topic)
    worktrees.require_trust(repo)  # before anything else: a planted hook or config must not ride on the approval
    code, line = record.status(repo, topic)
    if code != 0:
        raise worktrees.Refused(f"approve needs a current PASS or PASS-WITH-NOTES audit ({line})")
    head = gitio.rev(f"refs/heads/w/{topic}", repo.main_root)
    claim = repo.store.load_claim(topic)
    confirm(head[:8], f"Approve merging {topic} at {head[:8]}: {claim.goal if claim else ''}\n{line}")
    repo.store.ensure()
    atomic_write(repo.store.path("approvals", f"{topic}.json"), json.dumps({"head": head, "utc": utc_now()}, indent=2, sort_keys=True) + "\n")
    events.append(repo.store, "person", "approve", topic, {"head": head[:8]})
    return f"approved {topic} at {head[:8]}"
