from pathlib import Path

import pytest

from agent_build_loop import events, handover, worktrees
from agent_build_loop.worktrees import Row

GOLDEN = Path(__file__).parent / "golden" / "handover.md"
ROWS = [
    Row("add-summary", "w/add-summary", "1a2b3c4d", 2, False, "PASS", True, True),
    Row("rename-total", "w/rename-total", "5e6f7a8b", 1, True, "BLOCK", False, False),
]
MERGES = [
    {"utc": "2026-01-01T00:00:05Z", "event": "merge", "topic": "add-summary", "detail": "clean"},
    {"utc": "2026-01-01T00:00:09Z", "event": "rollback", "topic": "rename-total", "detail": "main check failed"},
]


def test_render_matches_the_golden_file():
    text = handover.render(ROWS, MERGES, ["rename-total: add summary.py to its claim"], ["first note", "second note"], "2026-01-01T00:01:00Z")
    assert text == GOLDEN.read_text(encoding="utf-8")


def test_empty_state_renders_none_sections():
    text = handover.render([], [], [], [], "2026-01-01T00:00:00Z")
    assert text.count("(none)") == 4 and "Nothing is waiting" in text


def test_control_characters_are_stripped_and_lines_stay_one_line():
    text = handover.render([], [], ["a\x1b[31mred\x07\nforged section"], ["x\u202eevil\u200b"], "t")
    assert "\x1b" not in text and "\x07" not in text and "\u202e" not in text and "\u200b" not in text
    assert "a [31mred forged section" in text


def test_section_line_cap():
    text = handover.render([], [], [], [f"note {i}" for i in range(100)], "t")
    notes = text.split("## Notes\n")[1].split("## Next action")[0].strip().splitlines()
    assert len(notes) == handover.MAX_LINES + 1 and notes[-1].endswith("more lines not shown")
    assert "note 39" in text and "note 40" not in text


def test_byte_cap():
    text = handover.render([], [], ["w" * 190] * 40, ["x" * 190] * 40, "t")
    assert len(text.encode("utf-8")) <= handover.MAX_BYTES and text.endswith("[truncated at 8 KB]\n")


@pytest.mark.parametrize(
    "rows,waiting,expected",
    [
        ([], ["x"], "Settle"),
        ([Row("a-b", "w/a-b", "x", 1, False, "none", False, False)], [], "abl audit a-b"),
        ([Row("a-b", "w/a-b", "x", 1, False, "PASS", True, False)], [], "approves a-b"),
        ([Row("a-b", "w/a-b", "x", 1, False, "PASS", True, True)], [], "abl merge a-b"),
        ([], [], "abl new"),
    ],
)
def test_next_action(rows, waiting, expected):
    assert expected in handover.next_action(rows, waiting)


def test_write_numbering_is_monotonic_and_survives_a_deleted_middle(repo):
    p1 = handover.write(repo, ["one"], [])
    p2 = handover.write(repo, ["two"], [])
    p3 = handover.write(repo, ["three"], [])
    assert [p.name for p in (p1, p2, p3)] == ["0001.md", "0002.md", "0003.md"]
    p2.unlink()
    assert handover.write(repo, ["four"], []).name == "0004.md"
    assert "four" in handover.newest(repo.store)


def test_write_keeps_the_newest_n(repo, monkeypatch):
    object.__setattr__(repo.cfg, "handover_keep", 2)
    for i in range(5):
        handover.write(repo, [f"n{i}"], [])
    assert handover.numbers(repo.store) == [4, 5]


def test_write_reads_state_and_events_since_the_last_handover(repo):
    worktrees.new(repo, "add-summary", ["print_quotes/summary.py"], "g")
    events.append(repo.store, "main", "merge", "add-summary", "clean")
    first = handover.write(repo, [], ["wait for the person"]).read_text(encoding="utf-8")
    assert "add-summary: branch w/add-summary" in first and "merge add-summary clean" in first and "wait for the person" in first
    second = handover.write(repo, [], []).read_text(encoding="utf-8")
    assert "merge add-summary clean" not in second


def test_show_with_nothing_written(repo):
    assert handover.newest(repo.store) is None


def test_masker_is_applied_on_write(repo):
    from agent_build_loop import mask

    m = mask.Masker([(str(repo.main_root), "<demo>")])
    path = handover.write(repo, [f"look in {repo.main_root}/docs"], [], masker=m)
    text = path.read_text(encoding="utf-8")
    assert "<demo>/docs" in text and str(repo.main_root) not in text
    assert events.read(repo.store)[-1]["detail"] == {"file": "0001.md"}
