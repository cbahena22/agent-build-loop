"""Publish hygiene: secrets, paths, e-mails, URLs, imports (stdlib-only, no shell)."""
import ast
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SKIP_DIRS = {".venv", "out", "__pycache__", ".git", "dist", "build", ".pytest_cache"}
SKIP_FILES = {"requirements-dev.lock", "requirements-dev.in"}
BINARY = {".gif", ".png", ".jpg", ".pyc", ".whl"}


def tracked_files():
    """Files git tracks in this folder (or would track: not ignored). Falls back to a walk outside a repository."""
    try:
        r = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=str(ROOT), stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, check=True, timeout=60)
        names = [n for n in r.stdout.decode("utf-8").split("\0") if n]
        found = [ROOT / n for n in names]
    except (OSError, subprocess.SubprocessError):
        found = [p for p in ROOT.rglob("*") if p.is_file()]
    return [p for p in found if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS) and ".egg-info" not in str(p)]


def text_files():
    return [p for p in tracked_files() if p.suffix not in BINARY and p.name not in SKIP_FILES]


def read(p):
    return p.read_text(encoding="utf-8", errors="replace")


def src_py():
    """The package's own modules (the example project under data/ is a separate program, copied out by the demo)."""
    return [p for p in SRC.rglob("*.py") if "data" not in p.relative_to(SRC).parts]


def test_there_are_files_to_check():
    names = {p.relative_to(ROOT).as_posix() for p in tracked_files()}
    assert {"pyproject.toml", "requirements-dev.lock", "pytest.ini", "LICENSE", "SECURITY.md", ".gitignore"} <= names
    assert any(n.startswith("src/agent_build_loop/") for n in names)


def test_runtime_imports_are_stdlib_or_the_package():
    allowed = set(sys.stdlib_module_names) | {"agent_build_loop"}
    bad = []
    for p in src_py():
        for node in ast.walk(ast.parse(read(p))):
            if isinstance(node, ast.Import):
                bad += [f"{p.name}: {a.name}" for a in node.names if a.name.split(".")[0] not in allowed]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and node.module.split(".")[0] not in allowed:
                bad.append(f"{p.name}: {node.module}")
    assert bad == []


def test_no_shell_calls_anywhere_in_python():
    bad = []
    for p in [q for q in tracked_files() if q.suffix == ".py"]:
        for node in ast.walk(ast.parse(read(p))):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "shell" and not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                        bad.append(f"{p.name}:{node.lineno} shell=")
                f = node.func
                if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "os" and f.attr in ("system", "popen"):
                    bad.append(f"{p.name}:{node.lineno} os.{f.attr}")
    assert bad == []


def test_no_url_in_src():
    pat = re.compile(r"(?i)\b(?:https?|ftp|wss?)://")
    assert [p.name for p in SRC.rglob("*") if p.is_file() and p.suffix not in BINARY and pat.search(read(p))] == []


def test_no_network_modules_in_src():
    banned = {"socket", "urllib", "http", "ftplib", "smtplib", "ssl", "requests", "asyncio"}
    bad = []
    for p in src_py():
        for node in ast.walk(ast.parse(read(p))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
            bad += [f"{p.name}: {n}" for n in names if n.split(".")[0] in banned]
    assert bad == []


def test_no_absolute_home_paths_or_user_names():
    pats = [re.compile("/ho" + r"me/\w+"), re.compile("/Us" + r"ers/\w+"), re.compile("[A-Za-z]:" + r"\\+" + "Us" + "ers" + r"\\"), re.compile("/c/Us" + "ers")]
    bad = [f"{p.relative_to(ROOT)}" for p in text_files() for pat in pats if pat.search(read(p))]
    assert bad == []


def test_no_emails_outside_reserved_domains():
    pat = re.compile(r"\b[\w.+-]+@(?!(?:[\w-]+\.)*(?:example|invalid|test)\b)[\w-]+\.[A-Za-z]{2,}\b")
    assert [str(p.relative_to(ROOT)) for p in text_files() if pat.search(read(p))] == []


def test_adr_numbers_do_not_exceed_the_public_decisions_file():
    decisions = ROOT / "docs" / "decisions.md"
    count = len(re.findall(r"(?m)^#+\s*ADR-\d{3}\b", read(decisions))) if decisions.exists() else 0
    bad = []
    for p in text_files():
        for m in re.finditer(r"\bADR-(\d{3})\b", read(p)):
            if int(m.group(1)) > count:
                bad.append(f"{p.relative_to(ROOT)}: {m.group(0)}")
    assert bad == []


def test_no_key_shaped_text():
    pats = [
        re.compile("sk" + r"-[A-Za-z0-9_-]{20,}"),
        re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\s*[:=]\s*[\"'][^\"']{6,}"),
        re.compile("-----BEGIN [A-Z ]*PRIVATE" + " KEY"),
        re.compile(r"\bAK" + r"IA[0-9A-Z]{16}\b"),
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
        re.compile(r"\b(?![0-9a-fA-F]+\b)[A-Za-z0-9]{40,}\b"),
    ]
    bad = []
    for p in text_files():
        t = read(p)
        bad += [f"{p.relative_to(ROOT)}: {pat.pattern[:20]}" for pat in pats if pat.search(t)]
    assert bad == []


def test_lock_file_has_hashes_and_the_windows_only_dependency():
    lock = read(ROOT / "requirements-dev.lock")
    assert "--hash=sha256:" in lock and "colorama" in lock
    bare = [ln for ln in lock.splitlines() if re.match(r"^[A-Za-z0-9_.-]+==", ln) and "\\" not in ln]
    assert bare == []


def test_no_runtime_dependencies_are_declared():
    import tomllib

    meta = tomllib.loads(read(ROOT / "pyproject.toml"))
    assert meta["project"]["dependencies"] == [] and meta["project"]["requires-python"] == ">=3.11"
    assert meta["project"]["scripts"] == {"abl": "agent_build_loop.cli:main"}


def test_wheel_carries_the_package_data(tmp_path):
    work = tmp_path / "build copy"
    shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".venv", "build", "dist", "*.egg-info", "__pycache__", ".pytest_cache", "out"))
    out = tmp_path / "wheelhouse"
    r = subprocess.run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(out), str(work)], env=dict(os.environ),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600, check=False)
    assert r.returncode == 0, r.stdout.decode("utf-8", "replace")[-2000:]
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1
    names = set(zipfile.ZipFile(wheels[0]).namelist())
    base = "agent_build_loop/data/"
    for need in ("checklist.md", "cases/guard_cases.jsonl", "cases/merge_cases.jsonl", "transcripts/README.md",
                 "example/print-quotes/pyproject.toml", "example/print-quotes/.abl.toml", "example/print-quotes/.gitattributes",
                 "example/print-quotes/print_quotes/totals.py", "example/print-quotes/print_quotes/pricing.py",
                 "example/print-quotes/tests/__init__.py", "example/print-quotes/tests/test_totals.py"):
        assert base + need in names, need
    assert not any(n.startswith("tests/") for n in names)
