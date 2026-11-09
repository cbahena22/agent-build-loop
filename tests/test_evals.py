"""`abl eval`: the case files cover the documented red-team and merge lists, the scoring rules, and one real run that matches the committed results."""
import json
import os
import sys
from pathlib import Path

import pytest

from agent_build_loop import evals
from agent_build_loop.hooks import guard

from .conftest import run_cli

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals" / "results"
MERGE_RULES = {"trust-changed", "no-claim", "not-on-main", "main-dirty", "worktree-dirty", "worktree-redirected", "no-branch",
               "stale-audit", "no-pass", "no-approval", "project-config", "branch-check-failed", "conflict"}


@pytest.fixture(scope="module")
def guard_rows():
    return evals.load_cases("guard_cases.jsonl")


@pytest.fixture(scope="module")
def merge_rows():
    return evals.load_cases("merge_cases.jsonl")


def test_red_team_set_covers_the_design_list(guard_rows):
    ids = {r["id"] for r in guard_rows}
    assert len(guard_rows) >= 34
    design = {
        # claim escapes
        "escape-dotdot-inside-tree", "escape-dotdot-to-main", "escape-case-other-file", "escape-case-main-claimed", "escape-junction",
        "escape-extended-prefix", "escape-short-name", "escape-ntfs-stream", "escape-trailing-dot", "escape-other-worktree", "escape-main-checkout",
        # git dir, state store, settings, .abl.toml
        "store-write-by-main", "store-write-by-worker", "git-dir-hook-by-main", "git-dir-config-by-worker", "settings-local-by-main",
        "settings-local-by-worker", "abl-toml-by-main", "abl-toml-by-worker",
        # shell
        "shell-git-push", "shell-git-c-dotdot-merge", "shell-quoted-git-push", "shell-chained-git-push", "shell-powershell-chained",
        # person-only forms
        "person-approve-main", "person-approve-worker", "person-init-rehash", "person-init-set", "person-init-remove-hooks", "person-end-discard",
        "person-python-m", "person-python-c", "person-bash-c", "person-powershell-call",
        # planted git state, through trust-changed
        "planted-post-checkout-hook", "planted-core-fsmonitor", "planted-filter-smudge",
        # red-team rows (hardening)
        "merge-redirected-git-file", "shell-symbolic-ref-main", "shell-fetch-update-head-ok", "person-script-c", "person-unbuffer",
        "settings-user-level-by-main",
        # known gaps
        "gap-python-c-writes-a-file", "gap-script-the-worker-wrote", "gap-shell-write-other-worktree", "gap-shell-write-outside-repo",
        "gap-mcp-write-when-allowed",
    }
    assert design <= ids, sorted(design - ids)
    gaps = {r["id"] for r in guard_rows if r["known_gap"]}
    assert gaps == {i for i in design if i.startswith("gap-")}
    assert all(r["expected"] == "allow" and not r["rule"] for r in guard_rows if r["known_gap"])


def test_every_rule_named_is_a_real_rule_id(guard_rows):
    for r in guard_rows:
        if r["expected"] == "deny":
            known = guard.RULES if r["via"] == "hook" else {"trust-changed"} if r["via"] == "trust" else MERGE_RULES
            assert r["rule"] in known, r["id"]


def test_merge_set_is_the_design_list(merge_rows):
    assert [(r["id"], r["expected"], r["rule"]) for r in merge_rows] == [
        ("clean", "merged", ""), ("conflict", "refused", "conflict"), ("branch-check-fails", "refused", "branch-check-failed"),
        ("main-check-fails-rollback", "rolled-back", "rollback"), ("rollback-refused-by-local-change", "rollback-refused", "rollback-refused"),
        ("stale-audit", "refused", "stale-audit"), ("no-approval", "refused", "no-approval"), ("block-verdict", "refused", "no-pass"),
        ("concurrent-merge", "lock", "LockBusy"), ("stale-lock", "lock", "LockStale"),
    ]


@pytest.mark.parametrize("line", [
    "not json",
    json.dumps({"id": "Bad_Id", "as": "main", "via": "hook", "payload": {}, "expected": "allow", "rule": "", "known_gap": False, "os": "any"}),
    json.dumps({"id": "x", "as": "main", "via": "hook", "payload": {}, "expected": "deny", "rule": "", "known_gap": False, "os": "any"}),
    json.dumps({"id": "x", "as": "main", "via": "hook", "payload": {}, "expected": "deny", "rule": "r", "known_gap": True, "os": "any"}),
    json.dumps({"id": "x", "as": "main", "via": "hook", "payload": {}, "expected": "allow", "rule": "", "known_gap": False, "os": "mac"}),
    json.dumps({"id": "x", "as": "main", "via": "shell", "payload": {}, "expected": "allow", "rule": "", "known_gap": False, "os": "any"}),
    json.dumps({"id": "x", "as": "main", "via": "hook", "payload": {}, "expected": "allow", "rule": "", "known_gap": False, "os": "any", "run": "rm"}),
])
def test_malformed_case_rows_are_refused(line):
    with pytest.raises(evals.EvalError):
        evals.parse_cases("guard_cases.jsonl", line + "\n")


def test_repeated_ids_are_refused():
    row = json.dumps({"id": "clean", "scenario": "clean", "expected": "merged", "rule": "", "os": "any"})
    with pytest.raises(evals.EvalError, match="repeated"):
        evals.parse_cases("merge_cases.jsonl", f"{row}\n{row}\n")


def _row(id_, expected="deny", rule="git-dir", gap=False):
    return {"id": id_, "expected": expected, "rule": rule, "known_gap": gap, "os": "any"}


def test_scoring_excludes_known_gaps_and_flags_mismatches_and_closed_gaps():
    cases = [_row("a"), _row("b"), _row("g", "allow", "", True), _row("h", "allow", "", True), _row("s")]
    rows = [evals._result(cases[0], "deny", "git-dir"), evals._result(cases[1], "deny", "claim-escape"),
            evals._result(cases[2], "allow", ""), evals._result(cases[3], "deny", "x"), evals._skipped(cases[4], "needs windows")]
    s = evals.summary(rows, cases)
    assert (s["n"], s["scored"], s["run"], s["matched"], s["skipped"]) == (5, 3, 2, 1, 1)
    assert s["mismatches"] == ["b"] and s["known_misses"] == ["g"] and s["gaps_closed"] == ["h"] and s["skipped_rows"] == ["s"]
    report = {"sets": {"red-team": s}}
    assert not evals.ok(report)
    assert evals.headline("red-team", s) == "red-team set: 1 / 2 matched (n = 5: 3 scored, 1 known misses, 1 skipped: need another OS)"


REPORT = {"os": "posix", "sets": {}, "rows": {"red-team": []}}


def test_write_replaces_a_linked_result_file_and_leaves_its_target(tmp_path):
    """Hardening: a branch could commit evals/results/eval.md as a link; the write must not follow it."""
    target = tmp_path / "victim.txt"
    target.write_text("keep", encoding="utf-8")
    out = tmp_path / "results"
    out.mkdir()
    try:
        os.symlink(target, out / "eval.md")
    except (OSError, NotImplementedError):
        pytest.skip("no symlink rights on this machine")
    evals.write(REPORT, out)
    assert target.read_text(encoding="utf-8") == "keep"
    assert not (out / "eval.md").is_symlink() and (out / "eval.md").read_text(encoding="utf-8").startswith("# abl eval")


def test_write_refuses_a_linked_out_folder(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "evals"
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(real), str(link))  # no symlink rights needed
        if not hasattr(Path, "is_junction"):
            pytest.skip("junction detection needs Python 3.12")
    else:
        os.symlink(real, link)
    for out in (link, link / "results"):
        with pytest.raises(evals.EvalError, match="is a link"):
            evals.write(REPORT, out)
    assert list(real.iterdir()) == []


def test_abl_eval_runs_both_sets_and_matches_the_committed_results(tmp_path):
    """The end-to-end check: a fresh run (with a caller's CLAUDE_PROJECT_DIR set, which the eval must ignore) writes
    eval.{md,json} whose numbers follow from the case files; on the OS the committed files came from, they are equal."""
    out = tmp_path / "out dir"
    r = run_cli("eval", "--out", str(out), cwd=tmp_path, extra_env={"CLAUDE_PROJECT_DIR": str(ROOT)})
    text = r.stdout.decode("utf-8")
    assert r.returncode == 0, (text, r.stderr.decode("utf-8", "replace"))
    report = json.loads((out / "eval.json").read_text(encoding="utf-8"))
    md = (out / "eval.md").read_text(encoding="utf-8")
    guard_cases, merge_cases = evals.load_cases("guard_cases.jsonl"), evals.load_cases("merge_cases.jsonl")
    red, merge = report["sets"]["red-team"], report["sets"]["merge"]
    gaps = sum(c["known_gap"] for c in guard_cases)
    other_os = sum(not evals.applies(c) for c in guard_cases)
    assert (red["n"], red["scored"], red["known_gaps"], red["skipped"]) == (len(guard_cases), len(guard_cases) - gaps, gaps, other_os)
    assert red["matched"] == red["run"] == red["scored"] - other_os and not red["mismatches"] and len(red["known_misses"]) == gaps
    assert (merge["n"], merge["matched"]) == (len(merge_cases), len(merge_cases))
    assert report["os"] == evals.OS_NAME and report["model_calls"] == 0
    for name, s in report["sets"].items():
        assert evals.headline(name, s) in text and evals.headline(name, s) in md
    assert str(tmp_path) not in md + json.dumps(report) and str(Path.home()) not in md + json.dumps(report)
    committed = json.loads((RESULTS / "eval.json").read_text(encoding="utf-8"))
    if committed["os"] == report["os"]:
        assert report == committed
        assert md == (RESULTS / "eval.md").read_text(encoding="utf-8")
    else:
        assert committed["sets"]["merge"]["matched"] == committed["sets"]["merge"]["n"]
