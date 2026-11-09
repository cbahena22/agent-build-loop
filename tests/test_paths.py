import ntpath
import posixpath
import sys

import pytest

from agent_build_loop import paths
from agent_build_loop.paths import PathRuleError, Target, classify, matches_pattern, patterns_overlap, validate_goal, validate_pattern, validate_topic

# ---- topic, goal, pattern ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("topic", ["add-summary", "rename-total", "ab", "a" + "b" * 39])
def test_good_topics(topic):
    assert validate_topic(topic) == topic


@pytest.mark.parametrize("topic", ["", "a", "--upload-pack", "-x", "Add", "1abc", "a b", "a/b", "a" + "b" * 40, "ab_cd", "ab.cd"])
def test_bad_topics(topic):
    with pytest.raises(PathRuleError):
        validate_topic(topic)


@pytest.mark.parametrize("goal", ["x", "Add a summary line", "é" * 5, "a" * 200])
def test_good_goals(goal):
    assert validate_goal(goal) == goal


@pytest.mark.parametrize("goal", ["", "a" * 201, "line one\nline two", "tab\there", "bell\x07", "nul\x00", "ret\rurn", "sep\u2028x"])
def test_bad_goals(goal):
    with pytest.raises(PathRuleError):
        validate_goal(goal)


GOOD_PATTERNS = ["a.py", "print_quotes/", "print_quotes/**", "print_quotes/totals.py", "tests/test_summary.py", "docs/a b.md"]
BAD_PATTERNS = [
    "", "/abs.py", "//server/share/x", "C:/x.py", "C:x.py", "\\\\server\\share\\x", "\\\\?\\C:\\x", "a\\b.py", "../x.py", "a/../x.py",
    "a/./x.py", "./x.py", ".", "..", "a//b.py", "file.py::$DATA", "file.py:alt", "x.py.", "x.py ", "dir./f.py", "a*.py", "*.py", "a/*/b.py",
    "**", "a/**/b", "a/b/**/", "a?.py", "a[1].py", ".git/config", ".GIT/config", "a/.git/config", ".worktrees/x/a.py", ".Worktrees/", "bell\x07.py", "/**",
]


@pytest.mark.parametrize("p", GOOD_PATTERNS)
def test_good_patterns(p):
    assert validate_pattern(p) == p


@pytest.mark.parametrize("p", BAD_PATTERNS)
def test_bad_patterns(p):
    with pytest.raises(PathRuleError):
        validate_pattern(p)


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("print_quotes/", "print_quotes/totals.py", True),
        ("print_quotes/totals.py", "print_quotes/**", True),
        ("print_quotes/", "print_quotes/**", True),
        ("print_quotes/", "print_quotes_old/", False),
        ("print_quotes/totals.py", "print_quotes/totals.py", True),
        ("print_quotes/totals.py", "print_quotes/pricing.py", False),
        ("PRINT_QUOTES/Totals.py", "print_quotes/totals.py", True),
        ("a/b/", "a/", True),
        ("a/", "ab.py", False),
    ],
)
def test_overlap(a, b, expected):
    assert patterns_overlap(a, b) is expected
    assert patterns_overlap(b, a) is expected


def test_matches_pattern_and_fold():
    assert matches_pattern("print_quotes/", "print_quotes/totals.py")
    assert matches_pattern("print_quotes/**", "print_quotes/sub/x.py")
    assert not matches_pattern("print_quotes/", "print_quotes_old/x.py")
    assert not matches_pattern("print_quotes/totals.py", "PRINT_QUOTES/Totals.py")
    assert matches_pattern("print_quotes/totals.py", "PRINT_QUOTES/Totals.py", fold=True)
    assert not matches_pattern("a.py", "a.py.bak")


# ---- classify, both flavours -------------------------------------------------------------------------------------

DRIVE = "C:" + "\\"
NT = dict(
    flavour="nt",
    repo_root=DRIVE + "work\\kestrel",
    worktrees={"add-summary": DRIVE + "work\\kestrel\\.worktrees\\add-summary", "add-summary-old": DRIVE + "work\\kestrel\\.worktrees\\add-summary-old"},
    git_dirs=[DRIVE + "work\\kestrel\\.git"],
    base_dir=DRIVE + "work\\kestrel",
)
POSIX = dict(
    flavour="posix",
    repo_root="/work/kestrel",
    worktrees={"add-summary": "/work/kestrel/.worktrees/add-summary", "add-summary-old": "/work/kestrel/.worktrees/add-summary-old"},
    git_dirs=["/work/kestrel/.git"],
    base_dir="/work/kestrel",
)


def c(raw, ctx, base=None):
    ctx = dict(ctx)
    if base is not None:
        ctx["base_dir"] = base
    return classify(raw, ctx["base_dir"], ctx["repo_root"], ctx["worktrees"], ctx["git_dirs"], flavour=ctx["flavour"])


def test_main_and_worktree_posix():
    assert c("print_quotes/totals.py", POSIX) == Target("main", None, "print_quotes/totals.py")
    assert c("/work/kestrel/pyproject.toml", POSIX) == Target("main", None, "pyproject.toml")
    wt = "/work/kestrel/.worktrees/add-summary/print_quotes/summary.py"
    assert c(wt, POSIX) == Target("worktree", "add-summary", "print_quotes/summary.py")
    assert c("print_quotes/summary.py", POSIX, base="/work/kestrel/.worktrees/add-summary") == Target("worktree", "add-summary", "print_quotes/summary.py")


def test_main_and_worktree_nt():
    assert c("print_quotes\\totals.py", NT) == Target("main", None, "print_quotes/totals.py")
    assert c("print_quotes/totals.py", NT) == Target("main", None, "print_quotes/totals.py")
    wt = DRIVE + "work\\kestrel\\.worktrees\\add-summary\\print_quotes\\summary.py"
    assert c(wt, NT) == Target("worktree", "add-summary", "print_quotes/summary.py")


@pytest.mark.parametrize("ctx", [POSIX, NT], ids=["posix", "nt"])
def test_prefix_name_is_not_the_worktree(ctx):
    sep = "\\" if ctx is NT else "/"
    t = c(ctx["worktrees"]["add-summary-old"] + sep + "x.py", ctx)
    assert t.kind == "worktree" and t.topic == "add-summary-old"
    t2 = c(ctx["repo_root"] + sep + ".worktrees" + sep + "add-summary-oldest" + sep + "x.py", ctx)
    assert t2.topic is None  # inside main's folder, but not a known worktree


@pytest.mark.parametrize("ctx", [POSIX, NT], ids=["posix", "nt"])
def test_dotdot_escapes(ctx):
    sep = "\\" if ctx is NT else "/"
    assert c(f"..{sep}outside.py", ctx).kind == "outside"
    assert c(f"print_quotes{sep}..{sep}..{sep}x.py", ctx).kind == "outside"
    inside = ctx["worktrees"]["add-summary"] + sep + f"..{sep}..{sep}pyproject.toml"
    assert c(inside, ctx) == Target("main", None, "pyproject.toml")  # leaving the worktree lands in main, and says so


@pytest.mark.parametrize("ctx", [POSIX, NT], ids=["posix", "nt"])
def test_git_dir_and_settings_kinds(ctx):
    sep = "\\" if ctx is NT else "/"
    root, wt = ctx["repo_root"], ctx["worktrees"]["add-summary"]
    assert c(root + sep + ".git" + sep + "config", ctx).kind == "git-dir"
    assert c(root + sep + ".git" + sep + "agent-build-loop" + sep + "approvals" + sep + "x.json", ctx).kind == "git-dir"
    assert c(root + sep + ".claude" + sep + "settings.local.json", ctx) == Target("settings", None, ".claude/settings.local.json")
    assert c(root + sep + ".claude" + sep + "settings.json", ctx).kind == "settings"
    assert c(wt + sep + ".claude" + sep + "settings.local.json", ctx) == Target("settings", "add-summary", ".claude/settings.local.json")
    assert c(wt + sep + ".git", ctx).kind == "git-dir"  # the .git file of a worktree
    assert c(root + sep + ".claude" + sep + "commands" + sep + "x.md", ctx).kind == "main"


@pytest.mark.parametrize("ctx", [POSIX, NT], ids=["posix", "nt"])
def test_empty_and_odd_input(ctx):
    assert c("", ctx).kind == "unknown"
    assert c("a\x00b", ctx).kind == "unknown"
    assert c(None, ctx).kind == "unknown"


def test_nt_drive_unc_and_extended_forms():
    assert c("\\\\?\\" + DRIVE + "work\\kestrel\\print_quotes\\totals.py", NT) == Target("main", None, "print_quotes/totals.py")
    assert c("\\\\?\\UNC\\server\\share\\x.py", NT).kind == "unknown"
    assert c("\\\\server\\share\\x.py", NT).kind == "unknown"
    assert c("D:" + "\\work\\kestrel\\x.py", NT).kind == "outside"


def test_nt_case_folding_keeps_the_original_spelling():
    t = c(DRIVE.lower() + "WORK\\Kestrel\\PRINT_QUOTES\\Totals.py", NT)
    assert t.kind == "main" and t.relpath == "PRINT_QUOTES/Totals.py"
    assert c(DRIVE + "work\\KESTREL\\.GIT\\config", NT).kind == "git-dir"
    assert c("PRINT_QUOTES/Totals.py", NT).relpath == "PRINT_QUOTES/Totals.py"


def test_posix_is_case_sensitive():
    assert c("/WORK/kestrel/x.py", POSIX).kind == "outside"


def test_nt_streams_and_trailing_dots():
    assert c("print_quotes\\totals.py::$DATA", NT).relpath == "print_quotes/totals.py"
    assert c("print_quotes\\totals.py:alt", NT).relpath == "print_quotes/totals.py"
    assert c("print_quotes\\totals.py.", NT).relpath == "print_quotes/totals.py"
    assert c("print_quotes\\totals.py  ", NT).relpath == "print_quotes/totals.py"
    assert c("print_quotes.\\totals.py", NT).relpath == "print_quotes/totals.py"
    assert c(".git.\\config", NT).kind == "git-dir"


def test_nt_unexpanded_short_name_is_not_trusted():
    assert c("PRINT_~1\\totals.py", NT).kind == "unknown"
    assert c("print_quotes\\TOTALS~1.PY", NT).kind == "unknown"


def test_extended_prefix_stripper():
    assert paths.strip_extended_prefix("\\\\?\\C:" + "\\x") == "C:" + "\\x"
    assert paths.strip_extended_prefix("\\\\?\\UNC\\h\\s\\x") == "\\\\h\\s\\x"
    assert paths.strip_extended_prefix("plain") == "plain"


def test_native_symlink_is_followed(tmp_path):
    root = tmp_path / "repo root"
    (root / "inside").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    link = root / "inside" / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlink rights on this machine")
    t = classify(str(link / "x.py"), str(root), str(root), {}, [str(root / ".git")])
    assert t.kind == "outside"
    t2 = classify("inside/x.py", str(root), str(root), {}, [str(root / ".git")])
    assert t2 == Target("main", None, "inside/x.py")


def test_stream_suffix_cannot_hide_a_dotdot_walk():
    wt = NT["worktrees"]["add-summary"]
    t = c(wt + "\\print_quotes\\a.py:x\\..\\..\\..\\..\\.git\\hooks\\pre-commit", NT)
    assert t.kind == "git-dir"  # '..' is applied first, so the real target is the git dir, never print_quotes/a.py
    assert c(wt + "\\a.py:x\\..\\..\\..\\.git\\hooks\\pre-commit", NT).kind in ("git-dir", "unknown")
    assert c("print_quotes\\totals.py::$DATA", NT).relpath == "print_quotes/totals.py"


def test_dot_only_segments_are_not_trusted_on_nt():
    assert c("print_quotes\\.. \\x.py", NT).kind == "unknown"
    assert c("print_quotes\\...\\x.py", NT).kind == "unknown"


@pytest.mark.parametrize("raw", ["\\\\?\\GLOBALROOT\\Device\\HarddiskVolume3\\work\\kestrel\\pyproject.toml", "\\\\?\\Volume{0000}\\x.py", "\\\\.\\pipe\\x"])
def test_device_paths_are_unknown(raw):
    assert c(raw, NT).kind == "unknown"


@pytest.mark.parametrize("p", ["con.py", "nul", "a/COM1.txt", "lpt9", "PRINT_~1/", "a/TOTALS~1.PY"])
def test_reserved_device_and_short_names_refused(p):
    with pytest.raises(PathRuleError):
        validate_pattern(p)


@pytest.mark.parametrize("topic", ["main", "person"])
def test_role_names_are_not_topics(topic):
    with pytest.raises(PathRuleError, match="role name"):
        validate_topic(topic)


UNC = "\\" * 2
ADMIN_SHARE = "C" + "$"


@pytest.mark.parametrize(
    "raw",
    [
        UNC + "localhost\\" + ADMIN_SHARE + "\\work\\kestrel\\pyproject.toml",  # loopback admin share: may be the repo itself
        UNC + "127.0.0.1\\" + ADMIN_SHARE + "\\work\\kestrel\\x.py",
        UNC + "?\\UNC\\localhost\\" + ADMIN_SHARE + "\\work\\kestrel\\x.py",
        UNC + "?\\Volume{" + "0" * 8 + "-0000-0000-0000-" + "0" * 12 + "}\\work\\kestrel\\x.py",
        UNC + ".\\pipe\\x",
        "print_quotes\\.\\..   \\x.py",
    ],
)
def test_unclassifiable_nt_paths_are_unknown_not_outside(raw):
    assert c(raw, NT).kind == "unknown"


@pytest.mark.parametrize("ctx", [POSIX, NT], ids=["posix", "nt"])
def test_outside_means_proved_outside(ctx):
    sep = "\\" if ctx is NT else "/"
    other = ("Q" + ":" + "\\elsewhere") if ctx is NT else "/elsewhere"
    assert c(other + sep + "x.py", ctx) == Target("outside")
    assert c(f"..{sep}..{sep}x.py", ctx) == Target("outside")


def test_kinds_are_constants():
    assert (paths.OUTSIDE, paths.UNKNOWN) == ("outside", "unknown")


# ---- Windows only: a real junction and a real 8.3 name ----------------------------------------------------------

windows_only = pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows-only file system check")


@windows_only
def test_junction_from_a_worktree_into_main_is_main(tmp_path):
    import _winapi

    root = tmp_path / "repo root"
    (root / "print_quotes").mkdir(parents=True)
    wt = root / ".worktrees" / "add-summary"
    wt.mkdir(parents=True)
    junction = wt / "jump"
    _winapi.CreateJunction(str(root / "print_quotes"), str(junction))  # a junction needs no admin rights
    t = classify(str(junction / "totals.py"), str(wt), str(root), {"add-summary": str(wt)}, [str(root / ".git")])
    assert t == Target("main", None, "print_quotes/totals.py")
    t2 = classify("jump\\totals.py", str(wt), str(root), {"add-summary": str(wt)}, [str(root / ".git")])
    assert t2 == Target("main", None, "print_quotes/totals.py")  # relative through the junction lands in main too


@windows_only
def test_real_short_name_is_expanded_before_classifying(tmp_path):
    from agent_build_loop import mask

    root = tmp_path / "repo root"
    long_dir = root / "a rather long folder name"
    long_dir.mkdir(parents=True)
    short = mask.short_form(str(long_dir))
    if short is None or "~" not in short:
        pytest.skip("8.3 short names are disabled on this volume")
    t = classify(short + "\\x.py", str(root), str(root), {}, [str(root / ".git")])
    assert t == Target("main", None, "a rather long folder name/x.py")
