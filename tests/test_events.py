import json

import pytest

from agent_build_loop import events
from agent_build_loop.events import EventError
from agent_build_loop.state import Store


@pytest.mark.parametrize("actor", ["person", "main", "add-summary"])
def test_roles_are_accepted(tmp_path, actor):
    row = events.append(Store(tmp_path), actor, "claim-open", "add-summary", {"owns": ["a/b.py"]})
    assert row["actor"] == actor


@pytest.mark.parametrize("actor", ["Root", "Demo Person", "user@host", "", "PERSON", "a", "x" * 41, None, 5])
def test_other_actors_are_rejected(tmp_path, actor):
    with pytest.raises(EventError):
        events.append(Store(tmp_path), actor, "claim-open")


@pytest.mark.parametrize(
    "detail",
    ["C:" + "\\work\\x", "see " + "C:" + "/work/x", "\\\\server\\share\\x", "/ho" + "me/demo/x", {"p": "/tmp/x/y"}, ["ok", "/var/log/x"], "path=/etc/passwd/"],
)
def test_absolute_paths_are_rejected(tmp_path, detail):
    with pytest.raises(EventError):
        events.append(Store(tmp_path), "main", "deny", None, detail)


def test_relative_paths_and_odd_event_names(tmp_path):
    store = Store(tmp_path)
    events.append(store, "main", "deny", None, {"path": "print_quotes/totals.py", "rule": "claim-escape"})
    for bad in ("Deny", "x", "has space", "a_b", 5):
        with pytest.raises(EventError):
            events.append(store, "main", bad)
    with pytest.raises(EventError):
        events.append(store, "main", "deny", "Bad Topic")
    with pytest.raises(EventError):
        events.append(store, "main", "deny", None, {"x": object()})


def test_lines_are_ascii_json_append_only(tmp_path):
    store = Store(tmp_path)
    events.append(store, "main", "note", None, "café")
    events.append(store, "person", "note", None, "two")
    lines = store.path("events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and all(line.isascii() for line in lines)
    assert json.loads(lines[0])["detail"] == "café"
    assert [r["actor"] for r in events.read(store)] == ["main", "person"]


def test_rotation_at_one_megabyte(tmp_path):
    store = Store(tmp_path)
    events.append(store, "main", "note", None, "first")
    with open(store.path("events.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"pad": "x" * events.MAX_BYTES}) + "\n")
    events.append(store, "main", "note", None, "after")
    assert store.path("events.1.jsonl").exists()
    rows = events.read(store)
    assert rows[-1]["detail"] == "after" and any(r.get("detail") == "first" for r in rows)
    assert store.path("events.jsonl").stat().st_size < 1000


def test_read_skips_garbage(tmp_path):
    store = Store(tmp_path)
    events.append(store, "main", "note")
    with open(store.path("events.jsonl"), "a", encoding="utf-8") as fh:
        fh.write("not json\n[1]\n")
    assert len(events.read(store)) == 1
