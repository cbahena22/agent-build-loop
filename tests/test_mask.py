import os

import pytest

from agent_build_loop import mask
from agent_build_loop.mask import Masker

WIN_ROOT = "C:" + "\\" + "Us" + "ers\\Demo Person\\AppData\\Local\\Temp\\abl-demo"
HOME = "C:" + "\\" + "Us" + "ers\\Demo Person"
SHORT = "C:" + "\\" + "Us" + "ers\\DEMOPE~1\\AppData\\Local\\Temp\\abl-demo"


def win_masker():
    return Masker([(WIN_ROOT, "<demo>"), (HOME, "~")], ignore_case=True, resolve=False, short=lambda p: SHORT if p == WIN_ROOT else None)


SPELLINGS = {
    "backslash": WIN_ROOT + "\\proj\\a.py",
    "forward": WIN_ROOT.replace("\\", "/") + "/proj/a.py",
    "extended": "\\\\?\\" + WIN_ROOT + "\\proj\\a.py",
    "git-bash": "/c/" + WIN_ROOT[3:].replace("\\", "/") + "/proj/a.py",
    "short-8.3": SHORT + "\\proj\\a.py",
    "other-case": WIN_ROOT.upper() + "\\PROJ\\a.py",
}


@pytest.mark.parametrize("name", SPELLINGS)
def test_each_spelling_of_the_demo_root_is_hidden(name):
    out = win_masker().text(f"wrote {SPELLINGS[name]} ok")
    assert out.startswith("wrote <demo>") and out.endswith("a.py ok")
    assert "Demo Person" not in out and "DEMOPE" not in out and "Us" + "ers" not in out
    assert "<demo>" in out


def test_home_is_tilde_and_the_longer_root_wins():
    m = win_masker()
    assert m.text(HOME + "\\notes.txt") == "~\\notes.txt"
    assert m.text(WIN_ROOT + "\\x") == "<demo>\\x"
    assert m.text(HOME.replace("\\", "/") + "/Documents") == "~/Documents"
    assert m.text("/c/" + HOME[3:].replace("\\", "/") + "/Documents") == "~/Documents"


def test_a_longer_name_is_not_cut_in_the_middle():
    m = win_masker()
    assert m.text(HOME + "-old\\x") == HOME + "-old\\x"
    assert m.text(HOME + "s\\x") == HOME + "s\\x"
    assert m.text("in " + HOME + ".") == "in ~."


def test_case_sensitive_when_asked():
    m = Masker([("/ho" + "me/demo", "~")], ignore_case=False, resolve=False, short=None)
    assert m.text("/ho" + "me/demo/x") == "~/x" and m.text("/HO" + "ME/demo/x") == "/HO" + "ME/demo/x"


def test_posix_roots_and_several_per_line():
    m = Masker([("/ho" + "me/demo", "~"), ("/tmp/abl run", "<tmp>")], ignore_case=False, resolve=False, short=None)
    assert m.text("/ho" + "me/demo/a and /tmp/abl run/b and /tmp/other") == "~/a and <tmp>/b and /tmp/other"


def test_degenerate_roots_are_skipped():
    m = Masker([("", "<x>"), ("/", "<root>")], ignore_case=False, resolve=False, short=None)
    assert m.text("/etc/passwd and text") == "/etc/passwd and text"


def test_default_masker_hides_home_and_temp(tmp_path, monkeypatch):
    home = tmp_path / "home dir"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    m = mask.default_masker(str(tmp_path / "repo here"))
    out = m.text(f"{tmp_path / 'repo here' / 'x.py'} and {home / 'f.txt'}")
    assert out == f"<demo>{os.sep}x.py and ~{os.sep}f.txt"
    assert str(tmp_path) not in out


def test_real_symlinked_root_is_masked_in_both_spellings(tmp_path):
    real = tmp_path / "real folder"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlink rights on this machine")
    m = Masker([(str(link), "<demo>")], short=None)
    assert m.text(str(real / "a")) .startswith("<demo>") and m.text(str(link / "a")).startswith("<demo>")


def test_short_form_returns_none_off_windows():
    if os.name != "nt":
        assert mask.short_form("/tmp") is None


@pytest.mark.skipif(os.name != "nt", reason="Windows-only file system check")
def test_real_short_name_is_masked(tmp_path):
    long_dir = tmp_path / "a rather long folder name" / "demo root"
    long_dir.mkdir(parents=True)
    short = mask.short_form(str(long_dir))  # the real GetShortPathNameW spelling
    if short is None or "~" not in short:
        pytest.skip("8.3 short names are disabled on this volume")
    m = Masker([(str(long_dir), "<demo>")])
    for spelling in (short, short.lower(), str(long_dir)):
        assert m.text(f"see {spelling}\\proj\\a.py") == "see <demo>\\proj\\a.py"
