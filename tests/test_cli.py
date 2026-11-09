import io
import subprocess

import pytest

from agent_build_loop import cli, gitio, person, trust, worktrees
from agent_build_loop.person import NotAPerson
from tests.conftest import commit_all, run_cli


def call(args, capsys, cwd, monkeypatch):
    monkeypatch.chdir(cwd)
    code = cli.main(args)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_new_list_claim_end_handover(repo_dir, capsys, monkeypatch):
    code, out, _ = call(["list"], capsys, repo_dir, monkeypatch)
    assert (code, out.strip()) == (0, "no open claims")
    code, out, _ = call(["new", "add-summary", "--owns", "print_quotes/summary.py", "--goal", "Add a summary"], capsys, repo_dir, monkeypatch)
    assert code == 0 and "opened add-summary" in out and ".worktrees/add-summary" in out
    code, out, _ = call(["claim", "add-summary", "--add", "tests/test_summary.py"], capsys, repo_dir, monkeypatch)
    assert code == 0 and "tests/test_summary.py" in out
    code, out, _ = call(["list"], capsys, repo_dir, monkeypatch)
    assert "add-summary" in out and "w/add-summary" in out
    code, out, _ = call(["handover", "write", "--note", "n1", "--waiting", "w1"], capsys, repo_dir, monkeypatch)
    assert code == 0 and "0001.md" in out
    code, out, _ = call(["handover", "show"], capsys, repo_dir, monkeypatch)
    assert "n1" in out and "w1" in out and "add-summary" in out
    code, out, _ = call(["end", "add-summary"], capsys, repo_dir, monkeypatch)
    assert code == 0 and "ended" in out


def test_handover_show_when_none(repo_dir, capsys, monkeypatch):
    assert call(["handover", "show"], capsys, repo_dir, monkeypatch)[1].strip() == "no handover yet"


def test_refusals_exit_one_and_say_why(repo_dir, capsys, monkeypatch):
    code, _, err = call(["new", "add-summary", "--owns", "pyproject.toml", "--goal", "g"], capsys, repo_dir, monkeypatch)
    assert code == 1 and "shared" in err
    code, _, err = call(["new", "add-summary", "--owns", "x.py", "--goal", "two\nlines"], capsys, repo_dir, monkeypatch)
    assert code == 1 and "goal" in err
    code, _, err = call(["end", "missing-one"], capsys, repo_dir, monkeypatch)
    assert code == 1 and "no open claim" in err


def test_commands_for_the_main_session_refuse_inside_a_worktree(repo_dir, capsys, monkeypatch):
    call(["new", "add-summary", "--owns", "x.py", "--goal", "g"], capsys, repo_dir, monkeypatch)
    wt = repo_dir / ".worktrees" / "add-summary"
    code, _, err = call(["new", "other-one", "--owns", "y.py", "--goal", "g"], capsys, wt, monkeypatch)
    assert code == 1 and "main session" in err
    code, out, _ = call(["list"], capsys, wt, monkeypatch)
    assert code == 0 and "add-summary" in out  # reading is fine from anywhere


def test_discard_needs_a_person(repo_dir, capsys, monkeypatch, env):
    call(["new", "add-summary", "--owns", "print_quotes/summary.py", "--goal", "g"], capsys, repo_dir, monkeypatch)
    wt = repo_dir / ".worktrees" / "add-summary"
    (wt / "print_quotes" / "summary.py").write_text("X = 1\n", encoding="utf-8")
    commit_all(wt, "work", env)
    code, _, err = call(["end", "add-summary", "--discard"], capsys, repo_dir, monkeypatch)  # stdin is not a terminal under pytest
    assert code == 1 and "not a terminal" in err
    assert gitio.rev("refs/heads/w/add-summary", repo_dir) is not None


def test_bad_project_config_is_exit_two(repo_dir, capsys, monkeypatch, env):
    (repo_dir / ".abl.toml").write_text("check = []\n", encoding="utf-8")
    commit_all(repo_dir, "break", env)
    code, _, err = call(["list"], capsys, repo_dir, monkeypatch)
    assert code == 2 and "check" in err


def test_not_a_repository_is_a_clean_error(tmp_path, capsys, monkeypatch):
    plain = tmp_path / "plain folder"
    plain.mkdir()
    code, _, err = call(["list"], capsys, plain, monkeypatch)
    assert code == 1 and err.startswith("abl: git rev-parse failed")


def test_output_has_no_machine_paths(repo_dir, capsys, monkeypatch, tmp_path):
    outs = []
    for args in (["list"], ["new", "add-summary", "--owns", "x.py", "--goal", "g"], ["list"], ["handover", "write", "--note", f"see {repo_dir}/docs"], ["handover", "show"]):
        _, out, err = call(args, capsys, repo_dir, monkeypatch)
        outs += [out, err]
    joined = "\n".join(outs)
    assert str(repo_dir) not in joined and str(tmp_path) not in joined


def test_list_and_handover_refuse_when_trust_changed_and_no_filter_runs(repo_dir, capsys, monkeypatch, env):
    call(["new", "add-summary", "--owns", "print_quotes/totals.py", "--goal", "g"], capsys, repo_dir, monkeypatch)
    wt = repo_dir / ".worktrees" / "add-summary"
    marker = repo_dir.parent / "filter-ran.txt"
    gitio.git("config", "filter.x.clean", f"touch '{marker.as_posix()}'; cat", cwd=repo_dir, env_extra=env)
    (wt / "print_quotes" / ".gitattributes").write_text("*.py filter=x\n", encoding="utf-8")  # untracked, in the worker tree
    with open(wt / "print_quotes" / "totals.py", "a", encoding="utf-8") as f:
        f.write("# changed\n")  # a modified tracked file: git status would run the clean filter on it
    for args in (["list"], ["handover", "write", "--note", "n"]):
        code, out, err = call(args, capsys, repo_dir, monkeypatch)
        assert code == 1 and "trust-changed" in err and out == ""
        assert not marker.exists()
    trust.record(worktrees.open_repo(repo_dir).store, repo_dir / ".git", "main")  # the person accepts it (abl init --rehash)
    code, _, _ = call(["list"], capsys, repo_dir, monkeypatch)
    assert code == 0 and marker.exists()  # control: without the check, this list would have run the planted filter


def test_unexpected_error_is_one_fixed_line(repo_dir, capsys, monkeypatch):
    real_run = subprocess.run

    def slow_git(argv, *a, **k):
        if list(argv[1:]) == ["--version"]:
            return real_run(argv, *a, **k)
        raise subprocess.TimeoutExpired(cmd=[*argv, str(repo_dir)], timeout=1, stderr=str(repo_dir).encode())

    monkeypatch.setattr(gitio.subprocess, "run", slow_git)
    code, out, err = call(["list"], capsys, repo_dir, monkeypatch)
    assert (code, out, err) == (1, "", cli.UNEXPECTED + "\n")
    assert "Traceback" not in err and str(repo_dir) not in err and "TimeoutExpired" not in err


def test_python_dash_m_runs(repo_dir):
    r = run_cli("list", cwd=repo_dir)
    assert r.returncode == 0 and b"no open claims" in r.stdout
    r = run_cli("--help", cwd=repo_dir)
    assert r.returncode == 0 and b"abl" in r.stdout
    assert run_cli("nonsense", cwd=repo_dir).returncode == 2


def test_person_gate_on_non_tty_and_retype():
    class NoTty:
        def isatty(self):
            return False

    class Tty:
        def isatty(self):
            return True

    with pytest.raises(NotAPerson, match="not a terminal"):
        person.require_person("x", "do it", stdin=NoTty())
    with pytest.raises(NotAPerson, match="did not match"):
        person.require_person("abc12345", "do it", stdin=Tty(), input_fn=lambda _p: "nope", stdout=io.StringIO())
    person.require_person("abc12345", "do it", stdin=Tty(), input_fn=lambda _p: " abc12345 ", stdout=io.StringIO())

    def eof(_p):
        raise EOFError

    with pytest.raises(NotAPerson):
        person.require_person("x", "do it", stdin=Tty(), input_fn=eof, stdout=io.StringIO())
