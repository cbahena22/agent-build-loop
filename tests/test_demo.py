"""`abl demo`, the end-to-end check: exact counts and last line, the same shas on every run, no machine paths."""
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from agent_build_loop import demo, gitio
from agent_build_loop.audit.reviewer import ReplayReviewer

from .conftest import run_cli

LAST = "guard: 1 denied | audits: 1 BLOCK, 2 PASS | merges: 1 clean, 1 rolled back | model calls: 0 ($0.00)"


def _env_with_temp(folder: Path) -> dict:
    e = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_") and k != "CLAUDE_PROJECT_DIR"}
    e.update(TEMP=str(folder), TMP=str(folder), TMPDIR=str(folder))
    return e


def _run_demo_cli(tmp_path: Path, name: str, extra=None):
    folder = tmp_path / name
    folder.mkdir()
    env = _env_with_temp(folder)
    env.update(extra or {})
    r = subprocess.run([sys.executable, "-m", "agent_build_loop", "demo"], cwd=str(folder), env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900, check=False)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-2000:]
    return r.stdout.decode("utf-8").replace("\r\n", "\n")  # text-mode stdout on Windows


def _final_sha(out: str) -> str:
    return re.search(r"^main: ([0-9a-f]{40})$", out, re.M).group(1)


@pytest.fixture(scope="module")
def two_runs(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("demo runs")
    return _run_demo_cli(tmp, "first run"), _run_demo_cli(tmp, "second run")


def test_last_line_and_counts(two_runs):
    out = two_runs[0]
    assert out.splitlines()[-1] == LAST
    assert out.count("guard DENY:") == 1 and "claim-escape (print_quotes/totals.py)" in out
    assert len(re.findall(r"audit \S+ at [0-9a-f]{8}: PASS(?:-WITH-NOTES)?$", out, re.M)) == 2
    assert "audit rename-total at" in out and "BLOCK (claim-escape, shared-path)" in out
    assert "refused: no-pass: rename-total" in out
    assert "rolled back to" in out


def test_final_main_is_the_add_summary_merge_and_the_same_in_two_folders(two_runs):
    first, second = two_runs
    merged = re.search(r"merged add-summary at ([0-9a-f]{8})", first).group(1)
    assert _final_sha(first).startswith(merged)
    assert _final_sha(first) == _final_sha(second)
    before_after = re.search(r"main before ([0-9a-f]{8}), after ([0-9a-f]{8})", first)
    assert before_after.group(1) == before_after.group(2) == merged


def test_pass_audits_hit_the_recorded_briefs():
    keys = set(ReplayReviewer(demo.data_file("transcripts", "reviews.jsonl")).replies)
    seen = []

    class Spy(ReplayReviewer):
        def review(self, brief):
            seen.append(brief.sha256)
            return super().review(brief)

    last, _final = demo.run(reviewer=Spy(demo.data_file("transcripts", "reviews.jsonl")))
    assert last == LAST
    assert len(seen) == 2 and set(seen) == keys


def test_personal_git_config_changes_nothing(tmp_path, monkeypatch):
    """HOME / USERPROFILE with a .gitconfig that signs commits, a failing hooks path and another e-mail."""
    home = tmp_path / "home with config"
    (home / "failing hooks").mkdir(parents=True)
    hook = home / "failing hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    other = "someone" + "@" + "elsewhere.example"
    (home / ".gitconfig").write_text(f"[user]\n\temail = {other}\n[commit]\n\tgpgsign = true\n[core]\n\thooksPath = {(home / 'failing hooks').as_posix()}\n",
                                     encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    work = tmp_path / "temp"
    work.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(work))
    last, final = demo.run(keep=True)
    assert last == LAST
    kept = next(work.glob("abl demo *")) / "print-quotes"
    with gitio.extra_env({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(kept.parent / "empty-global-gitconfig")}):
        mails = gitio.git("log", "--all", "--format=%ae %ce", cwd=kept).out.split()
        assert gitio.rev("refs/heads/main", kept) == final
    assert set(mails) == {"demo@example.invalid"}
    demo._rmtree(kept.parent)


def _no_machine_paths(text: str) -> None:
    pats = [r"Users[\\/]", "/ho" + "me/", "/c/" + "Users"]
    host = platform.node()
    for p in pats:
        assert not re.search(p, text, re.I), p
    if host:
        assert not re.search(r"[\\/]" + re.escape(host) + r"([\\/]|$)", text, re.I | re.M)


def test_demo_init_and_list_output_have_no_machine_paths(two_runs, repo):
    _no_machine_paths(two_runs[0])
    r = run_cli("init", "--dry-run", cwd=repo.main_root)
    assert r.returncode == 0
    _no_machine_paths(r.stdout.decode() + r.stderr.decode())
    r = run_cli("list", cwd=repo.main_root)
    assert r.returncode == 0
    _no_machine_paths(r.stdout.decode() + r.stderr.decode())


def test_transcripts_are_labelled_synthetic_and_declare_effects():
    for topic in (demo.A, demo.B):
        rows = demo.load_transcript(topic)
        assert rows
        for row in rows:
            if row["tool_name"] == "Bash":
                assert set(row["effect"]) <= {"commit", "edit"}
    header = json.loads(demo.data_file("transcripts", "reviews.jsonl").read_text().splitlines()[0])
    assert header["synthetic"] is True and "Synthetic" in header["note"]


def test_the_scripted_approval_needs_the_demo_marker(tmp_path):
    d = demo.Demo(tmp_path, lambda _s: None, reviewer=None)
    (tmp_path / demo.MARKER).write_text("not this run", encoding="utf-8")
    with pytest.raises(demo.DemoError, match="demo's own folder"):
        d.person_approves(demo.A)
