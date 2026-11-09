"""Append-only event log. Actors are roles, paths are repo-relative, nothing machine-specific is written."""
import json
import os
import re
from pathlib import Path

from agent_build_loop.paths import TOPIC_RE
from agent_build_loop.state import Store, utc_now

MAX_BYTES = 1_000_000
_ABS_PATH_RE = re.compile(r"(^|[\s\"'=(])([A-Za-z]:[\\/]|\\\\|/[A-Za-z0-9_.~-]+/)")


class EventError(ValueError):
    pass


def _check_text(value, where: str) -> None:
    if isinstance(value, str):
        if _ABS_PATH_RE.search(value):
            raise EventError(f"{where} holds an absolute path; events use repo-relative paths")
    elif isinstance(value, dict):
        for k, v in value.items():
            _check_text(k, where)
            _check_text(v, where)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _check_text(v, where)
    elif value is not None and not isinstance(value, (int, float, bool)):
        raise EventError(f"{where} must be JSON text, numbers or lists")


def validate(actor: str, event: str, topic: str | None, detail) -> None:
    if actor not in ("person", "main") and not (isinstance(actor, str) and TOPIC_RE.match(actor)):
        raise EventError("actor must be person, main or a topic")
    if not isinstance(event, str) or not re.match(r"^[a-z][a-z0-9-]{1,39}$", event):
        raise EventError("event must be a kebab-case word")
    if topic is not None and not (isinstance(topic, str) and TOPIC_RE.match(topic)):
        raise EventError("topic is not a valid topic")
    _check_text(detail, "detail")


def append(store: Store, actor: str, event: str, topic: str | None = None, detail=None) -> dict:
    validate(actor, event, topic, detail)
    row = {"utc": utc_now(), "actor": actor, "event": event, "topic": topic, "detail": detail}
    line = json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n"
    log = store.path("events.jsonl")
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists() and log.stat().st_size >= MAX_BYTES:
        os.replace(log, store.path("events.1.jsonl"))
    with open(log, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)
    return row


def read(store: Store) -> list[dict]:
    """All rows, oldest first (the rotated file first). Lines that do not parse are skipped."""
    rows: list[dict] = []
    for name in ("events.1.jsonl", "events.jsonl"):
        p: Path = store.path(name)
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows
