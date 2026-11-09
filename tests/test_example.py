import importlib.resources
import os
import shutil
import subprocess
import sys

from agent_build_loop import config
from tests.conftest import copy_example


def clean_env():
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LC_ALL")
    return {k: v for k, v in os.environ.items() if k.upper() in keep}


def test_example_check_argv_passes_in_a_temp_copy(tmp_path):
    work = tmp_path / "copy with space"
    copy_example(work)
    cfg = config.parse((work / ".abl.toml").read_text(encoding="utf-8"))
    r = subprocess.run(config.check_argv(cfg), cwd=str(work), env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=cfg.check_timeout_s, check=False)
    assert r.returncode == 0, r.stdout.decode("utf-8", "replace")


def test_example_check_fails_when_the_code_breaks(tmp_path):
    work = tmp_path / "broken"
    copy_example(work)
    (work / "print_quotes" / "totals.py").write_text("def total(lines):\n    return 0\n", encoding="utf-8")
    cfg = config.parse((work / ".abl.toml").read_text(encoding="utf-8"))
    r = subprocess.run(config.check_argv(cfg), cwd=str(work), env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120, check=False)
    assert r.returncode != 0


def test_example_shape():
    root = importlib.resources.files("agent_build_loop") / "data" / "example" / "print-quotes"
    for name in ("print_quotes/__init__.py", "print_quotes/totals.py", "print_quotes/pricing.py", "tests/__init__.py", "tests/test_totals.py",
                 "tests/test_pricing.py", "pyproject.toml", ".abl.toml", ".gitattributes"):
        assert (root / name).is_file(), name
    assert 'version = "0.1.0"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in (root / ".gitattributes").read_text(encoding="utf-8")


def test_example_has_no_machine_or_person_data():
    root = importlib.resources.files("agent_build_loop") / "data" / "example" / "print-quotes"
    text = "".join(p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file() and p.suffix in (".py", ".toml"))
    assert "@" not in text and "http" not in text


def test_copy_is_independent_of_the_package_data(tmp_path):
    work = tmp_path / "w"
    copy_example(work)
    shutil.rmtree(work)
    assert (importlib.resources.files("agent_build_loop") / "data" / "example" / "print-quotes" / "pyproject.toml").is_file()
