"""Reviewers: a protocol with a stub and a replay of recorded (here: synthetic, hand-written) replies. No model call."""
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_build_loop.audit.brief import AuditBrief

VERDICTS = ("PASS", "PASS-WITH-NOTES", "BLOCK")
REPLAY_KIND = "abl-review-replay"
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_TEXT = 500


@dataclass(frozen=True)
class Finding:
    path: str
    reason: str


@dataclass(frozen=True)
class ReviewResult:
    verdict: str
    findings: tuple[Finding, ...] = ()


class ReviewerError(Exception):
    """The reviewer could not give a reply (a replay miss, an unreadable transcript). Nothing is recorded."""


class Reviewer(Protocol):
    name: str

    def review(self, brief: AuditBrief) -> ReviewResult: ...


UNPARSABLE = ReviewResult("BLOCK", (Finding("-", "reviewer output could not be parsed"),))


def parse_reply(reply) -> ReviewResult:
    """Strict: exactly {"verdict", "findings"}, a known verdict, findings of exactly {"path", "reason"} strings.
    Anything else is a BLOCK."""
    if not isinstance(reply, dict) or set(reply) != {"verdict", "findings"}:
        return UNPARSABLE
    verdict, findings = reply["verdict"], reply["findings"]
    if verdict not in VERDICTS or not isinstance(findings, list):
        return UNPARSABLE
    out = []
    for f in findings:
        if not isinstance(f, dict) or set(f) != {"path", "reason"}:
            return UNPARSABLE
        if not all(isinstance(f[k], str) and 0 < len(f[k]) <= MAX_TEXT for k in ("path", "reason")):
            return UNPARSABLE
        out.append(Finding(f["path"], f["reason"]))
    return ReviewResult(verdict, tuple(out))


class StubReviewer:
    name = "stub"

    def review(self, brief: AuditBrief) -> ReviewResult:
        return ReviewResult("PASS", (Finding("-", "stub reviewer: no model"),))


class ReplayReviewer:
    """Look the reply up by the brief's sha256. A miss is an error ("not recorded"), never a live call.

    File format (JSONL): a header line `{"kind": "abl-review-replay", "synthetic": true|false, "note": "..."}`,
    then one `{"brief_sha256": "<64 hex>", "topic": "...", "reply": {...}}` per recorded review."""

    name = "replay"

    def __init__(self, source):
        try:
            text = Path(source).read_text(encoding="utf-8") if not hasattr(source, "read_text") else source.read_text(encoding="utf-8")
        except OSError as exc:
            raise ReviewerError("the replay file could not be read") from exc
        lines = [ln for ln in text.splitlines() if ln.strip()]
        try:
            rows = [json.loads(ln) for ln in lines]
        except ValueError as exc:
            raise ReviewerError("the replay file is not JSON lines") from exc
        if not rows or not isinstance(rows[0], dict) or rows[0].get("kind") != REPLAY_KIND or not isinstance(rows[0].get("synthetic"), bool):
            raise ReviewerError("the replay file has no abl-review-replay header")
        self.synthetic = rows[0]["synthetic"]
        self.replies: dict[str, object] = {}
        for row in rows[1:]:
            if not isinstance(row, dict) or not _SHA_RE.match(str(row.get("brief_sha256", ""))) or "reply" not in row:
                raise ReviewerError("a replay row has no brief_sha256 or reply")
            self.replies[row["brief_sha256"]] = row["reply"]

    def review(self, brief: AuditBrief) -> ReviewResult:
        sha = brief.sha256
        if sha not in self.replies:
            raise ReviewerError(f"not recorded: the replay has no reply for brief {sha[:12]}")
        return parse_reply(self.replies[sha])


def from_spec(spec: str | None) -> Reviewer:
    """`stub` (default) or `replay:<file>`."""
    if not spec or spec == "stub":
        return StubReviewer()
    if spec.startswith("replay:") and len(spec) > len("replay:"):
        return ReplayReviewer(spec[len("replay:"):])
    raise ValueError("--reviewer takes stub or replay:<file>")
