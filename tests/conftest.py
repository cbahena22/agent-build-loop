import importlib.resources
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from agent_build_loop import gitio

PERSON = {"name": "Demo Person", "email": "demo@example.invalid"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(self, *a, **k):
        raise AssertionError("tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


def git_env(tmp_path: Path, n: int = 1) -> dict:
    """Pinned identity and an empty global config, passed to gitio as the allowed extras."""
    empty = tmp_path / "empty gitconfig"
    empty.write_text("", encoding="utf-8")
    stamp = f"2026-01-01T00:00:{n:02d}+0000"
    return {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(empty),
        "GIT_AUTHOR_NAME": PERSON["name"],
        "GIT_AUTHOR_EMAIL": PERSON["email"],
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_NAME": PERSON["name"],
        "GIT_COMMITTER_EMAIL": PERSON["email"],
        "GIT_COMMITTER_DATE": stamp,
    }


def commit_all(cwd: Path, message: str, env: dict) -> str:
    gitio.git("add", "-A", cwd=cwd, env_extra=env)
    gitio.git("commit", "-q", "-m", message, cwd=cwd, env_extra=env)
    return gitio.git("rev-parse", "HEAD", cwd=cwd, env_extra=env).out.strip()


def copy_example(dest: Path) -> None:
    src = importlib.resources.files("agent_build_loop") / "data" / "example" / "print-quotes"
    shutil.copytree(str(src), str(dest), dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))


@pytest.fixture
def env(tmp_path):
    return git_env(tmp_path)


@pytest.fixture
def repo_dir(tmp_path, env, monkeypatch):
    return build_repo(tmp_path, env, monkeypatch)


def build_repo(tmp_path: Path, env: dict, monkeypatch) -> Path:
    """A repository in a folder whose name has a space, holding the example project on branch main, trust recorded."""
    root = tmp_path / "my repo"
    root.mkdir()
    gitio.git("init", "-q", "-b", "main", cwd=root, env_extra=env)
    for k, v in (("user.name", PERSON["name"]), ("user.email", PERSON["email"]), ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
        gitio.git("config", k, v, cwd=root, env_extra=env)
    copy_example(root)
    commit_all(root, "start", env)
    # abl's own git calls scrub GIT_*; point HOME at an empty folder so no personal config leaks in
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from agent_build_loop import trust, worktrees

    r = worktrees.open_repo(root)
    trust.record(r.store, r.common_dir, "main")  # what `abl init` does: the person has looked
    return root


@pytest.fixture
def repo(repo_dir):
    from agent_build_loop import worktrees

    return worktrees.open_repo(repo_dir)


def run_cli(*argv, cwd, extra_env=None):
    """Run `python -m agent_build_loop` as a child process with a clean-ish environment."""
    import os

    e = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    e.update(extra_env or {})
    return subprocess.run([sys.executable, "-m", "agent_build_loop", *argv], cwd=str(cwd), env=e,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, check=False)
