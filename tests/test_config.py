import sys

import pytest

from agent_build_loop import config, gitio
from agent_build_loop.config import ConfigError, ProjectConfig
from tests.conftest import commit_all


def test_defaults_when_empty():
    assert config.parse("") == ProjectConfig()


def test_full_file():
    cfg = config.parse(
        'check = ["python", "-m", "unittest", "-q"]\ncheck_timeout_s = 30\nshared = ["pyproject.toml", "docs/**"]\n'
        "deny_mcp_for_workers = false\nhandover_keep = 3\n"
    )
    assert cfg.check_timeout_s == 30 and cfg.shared == ("pyproject.toml", "docs/**") and cfg.deny_mcp_for_workers is False and cfg.handover_keep == 3


@pytest.mark.parametrize(
    "text,key",
    [
        ("surprise = 1", "surprise"),
        ("check = []", "check"),
        ('check = "python -m unittest"', "check"),
        ("check = [1]", "check"),
        ('check = ["/usr/bin/python"]', "check"),
        ('check = ["C:\\\\py\\\\python.exe"]', "check"),
        ('check = ["my tool"]', "check"),
        ('check = ["-m"]', "check"),
        ('check = ["python", ""]', "check"),
        ("check_timeout_s = 0", "check_timeout_s"),
        ('check_timeout_s = "9"', "check_timeout_s"),
        ("check_timeout_s = true", "check_timeout_s"),
        ('shared = "pyproject.toml"', "shared"),
        ('shared = ["../x"]', "shared"),
        ('deny_mcp_for_workers = "yes"', "deny_mcp_for_workers"),
        ("handover_keep = 0", "handover_keep"),
        ("handover_keep = 1.5", "handover_keep"),
        ("this is not toml", ".abl.toml"),
    ],
)
def test_errors_name_the_key(text, key):
    with pytest.raises(ConfigError) as exc:
        config.parse(text)
    assert exc.value.key == key
    assert key in str(exc.value)


def test_check_argv_resolves_python():
    argv = config.check_argv(ProjectConfig())
    assert argv[0] == sys.executable and argv[1:] == ["-m", "unittest", "-q"]
    assert config.check_argv(ProjectConfig(check=("git", "status")))[0] == "git"


def test_missing_file_means_defaults(tmp_path, env):
    root = tmp_path / "bare project"
    root.mkdir()
    gitio.git("init", "-q", "-b", "main", cwd=root, env_extra=env)
    (root / "a.txt").write_text("x", encoding="utf-8")
    commit_all(root, "one", env)
    assert config.load(root / ".git", "main") == ProjectConfig()


def test_readers_use_the_main_ref_not_the_branch(repo_dir, env):
    common = repo_dir / ".git"
    before = config.load(common, "main")
    assert "pyproject.toml" in before.shared
    gitio.git("switch", "-q", "-c", "w/evil", cwd=repo_dir, env_extra=env)
    (repo_dir / ".abl.toml").write_text("shared = []\n", encoding="utf-8")
    commit_all(repo_dir, "loosen", env)
    assert config.load(common, "main") == before  # the worker branch changed nothing the readers see
    gitio.git("switch", "-q", "main", cwd=repo_dir, env_extra=env)
    assert config.load(common, "main") == before


def test_a_bad_config_on_main_is_exit_two_material(repo_dir, env):
    (repo_dir / ".abl.toml").write_text('check = []\n', encoding="utf-8")
    commit_all(repo_dir, "break", env)
    with pytest.raises(ConfigError):
        config.load(repo_dir / ".git", "main")
