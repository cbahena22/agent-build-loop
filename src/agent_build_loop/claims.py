"""Claims: which paths a worker owns. Pure validation plus the on-disk shape; locking lives in state.py."""
import re
from dataclasses import asdict, dataclass, field

from agent_build_loop.paths import (
    PathRuleError,
    matches_pattern,
    patterns_overlap,
    validate_goal,
    validate_pattern,
    validate_topic,
)

_FIELDS = ("topic", "branch", "worktree", "owns", "goal", "model", "effort", "created_utc", "base_sha")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,40}$")


@dataclass
class Claim:
    topic: str
    branch: str
    worktree: str  # repo-relative POSIX, e.g. ".worktrees/add-summary"
    owns: list[str] = field(default_factory=list)
    goal: str = ""
    model: str = ""
    effort: str = ""
    created_utc: str = ""
    base_sha: str = ""

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "Claim":
        """Strict: unknown or missing keys, wrong types and bad values are refused (a hand-edited file is data)."""
        if not isinstance(data, dict) or set(data) != set(_FIELDS):
            raise PathRuleError("claim file has the wrong keys")
        c = cls(**data)
        validate_topic(c.topic)
        validate_goal(c.goal)
        if c.branch != f"w/{c.topic}" or c.worktree != f".worktrees/{c.topic}":
            raise PathRuleError("claim branch or worktree does not match its topic")
        if not isinstance(c.owns, list) or not c.owns:
            raise PathRuleError("claim owns list is empty")
        for p in c.owns:
            validate_pattern(p)
        if c.base_sha and not _SHA_RE.match(c.base_sha):
            raise PathRuleError("claim base_sha is not a full sha")
        for label in (c.model, c.effort):
            if label and not _LABEL_RE.match(label):
                raise PathRuleError("model or effort label has unexpected characters")
        return c


def check_owns(owns, shared, others, *, topic: str | None = None) -> list[str]:
    """Validate a list of owns patterns against `shared` and the other open claims. Returns the cleaned list."""
    if not owns:
        raise PathRuleError("owns list is empty")
    cleaned: list[str] = []
    for p in owns:
        validate_pattern(p)
        if p in cleaned:
            continue
        for s in shared:
            if patterns_overlap(p, s):
                raise PathRuleError(f"{p} touches a shared path ({s}); only the main session edits shared paths")
        for other in others:
            if other.topic == topic:
                continue
            for q in other.owns:
                if patterns_overlap(p, q):
                    raise PathRuleError(f"{p} overlaps {q}, owned by {other.topic}")
        for q in cleaned:
            if patterns_overlap(p, q):
                raise PathRuleError(f"{p} overlaps {q} in the same list")
        cleaned.append(p)
    return cleaned


def validate_new(topic, goal, owns, shared, others) -> list[str]:
    validate_topic(topic)
    validate_goal(goal)
    if any(c.topic == topic for c in others):
        raise PathRuleError(f"a claim for {topic} already exists")
    return check_owns(owns, shared, others)


def owns(claim: Claim, relpath: str, *, fold: bool = False) -> bool:
    return any(matches_pattern(p, relpath, fold=fold) for p in claim.owns)
