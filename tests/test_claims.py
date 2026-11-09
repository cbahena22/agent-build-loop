import pytest

from agent_build_loop import claims
from agent_build_loop.claims import Claim
from agent_build_loop.config import DEFAULT_SHARED
from agent_build_loop.paths import PathRuleError

SHA = "a" * 40


def mk(topic="add-summary", owns=("print_quotes/summary.py",), **kw):
    base = dict(topic=topic, branch=f"w/{topic}", worktree=f".worktrees/{topic}", owns=list(owns), goal="g", model="sonnet", effort="high", created_utc="2026-01-01T00:00:00Z", base_sha=SHA)
    base.update(kw)
    return Claim(**base)


def test_round_trip():
    c = mk()
    assert Claim.from_json(c.to_json()) == c


@pytest.mark.parametrize(
    "patch",
    [
        {"extra": 1},
        {"topic": "Bad"},
        {"branch": "w/other"},
        {"worktree": "elsewhere"},
        {"owns": []},
        {"owns": ["../x"]},
        {"goal": "two\nlines"},
        {"base_sha": "xyz"},
        {"model": "bad model!"},
    ],
)
def test_from_json_is_strict(patch):
    data = mk().to_json()
    data.update(patch)
    with pytest.raises(PathRuleError):
        Claim.from_json(data)


def test_missing_key_refused():
    data = mk().to_json()
    del data["goal"]
    with pytest.raises(PathRuleError):
        Claim.from_json(data)


def test_empty_owns_refused():
    with pytest.raises(PathRuleError):
        claims.check_owns([], DEFAULT_SHARED, [])


@pytest.mark.parametrize("pattern", ["pyproject.toml", ".abl.toml", ".claude/settings.local.json", ".claude/", ".gitignore", ".gitattributes"])
def test_shared_paths_cannot_be_owned(pattern):
    with pytest.raises(PathRuleError, match="shared"):
        claims.check_owns([pattern], DEFAULT_SHARED, [])


def test_overlap_with_another_open_claim():
    other = mk("rename-total", ["print_quotes/"])
    with pytest.raises(PathRuleError, match="rename-total"):
        claims.check_owns(["print_quotes/totals.py"], DEFAULT_SHARED, [other])
    assert claims.check_owns(["tests/test_summary.py"], DEFAULT_SHARED, [other]) == ["tests/test_summary.py"]


def test_own_claim_is_not_an_overlap_when_adding():
    mine = mk(owns=["print_quotes/summary.py"])
    got = claims.check_owns(mine.owns + ["tests/test_summary.py"], DEFAULT_SHARED, [mine], topic=mine.topic)
    assert got == ["print_quotes/summary.py", "tests/test_summary.py"]


def test_overlap_inside_one_list_and_duplicates():
    with pytest.raises(PathRuleError):
        claims.check_owns(["a/", "a/b.py"], DEFAULT_SHARED, [])
    assert claims.check_owns(["a.py", "a.py"], DEFAULT_SHARED, []) == ["a.py"]


def test_validate_new_rejects_duplicate_topic_and_bad_goal():
    with pytest.raises(PathRuleError):
        claims.validate_new("add-summary", "g", ["x.py"], DEFAULT_SHARED, [mk()])
    with pytest.raises(PathRuleError):
        claims.validate_new("other-one", "line\nbreak", ["x.py"], DEFAULT_SHARED, [])


def test_owns_function():
    c = mk(owns=["print_quotes/", "tests/test_summary.py"])
    assert claims.owns(c, "print_quotes/summary.py")
    assert claims.owns(c, "tests/test_summary.py")
    assert not claims.owns(c, "tests/test_totals.py")
    assert not claims.owns(c, "PRINT_QUOTES/summary.py")
    assert claims.owns(c, "PRINT_QUOTES/summary.py", fold=True)
