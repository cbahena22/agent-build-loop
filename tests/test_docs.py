"""Doc tests: links resolve, the AI-use line is present, README numbers equal the committed eval results, the demo line is real."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
AI_LINE = "Designed, specified and reviewed by me; most code written by Claude Code agents I directed."
DOCS = [ROOT / "README.md", ROOT / "SECURITY.md", *sorted((ROOT / "docs").glob("*.md"))]


def test_every_relative_link_resolves():
    bad = []
    for doc in DOCS:
        for target in re.findall(r"\]\(([^)\s]+)\)", doc.read_text(encoding="utf-8")):
            if re.match(r"(?i)^(?:[a-z]+:|#)", target):
                continue
            path = target.split("#")[0]
            if path and not (doc.parent / path).resolve().exists():
                bad.append(f"{doc.name}: {target}")
    assert bad == []


def test_the_ai_use_line_is_present():
    assert AI_LINE in README


def test_the_readme_quotes_the_headline_lines_of_the_committed_eval():
    headlines = [ln for ln in (ROOT / "evals" / "results" / "eval.md").read_text(encoding="utf-8").splitlines() if re.match(r"^(red-team|merge) set: ", ln)]
    assert len(headlines) == 2
    for line in headlines:
        assert line in README.splitlines()


def test_the_readme_numbers_equal_eval_json_when_the_os_matches():
    data = json.loads((ROOT / "evals" / "results" / "eval.json").read_text(encoding="utf-8"))
    here = "windows" if os.name == "nt" else "posix"
    sets = data["sets"]
    for name, label in (("red-team", "red-team set"), ("merge", "merge set")):
        s = sets[name]
        m = re.search(rf"(?m)^{label}: (\d+) / (\d+) matched \(n = (\d+): (\d+) scored(?:, (\d+) known misses)?, (\d+) skipped\)$", README)
        assert m, label
        got = tuple(int(x or 0) for x in m.groups())
        want = (s["matched"], s["scored"], s["n"], s["scored"], s["known_gaps"], s["skipped"])
        # the committed file is the source of truth; on another OS the README must still quote it, not a fresh run
        assert got == want, (label, got, want)
    if data["os"] == here:
        assert data["model_calls"] == 0 and data["cost_usd"] == 0
    for miss in sets["red-team"]["known_misses"]:
        assert f"`{miss}`" in README


def test_the_readme_demo_line_equals_the_last_line_of_abl_demo(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_CONFIG_DIR")}
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(tmp_path)
    r = subprocess.run([sys.executable, "-m", "agent_build_loop", "demo"], cwd=str(tmp_path), env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=600)
    assert r.returncode == 0, r.stdout.decode("utf-8", "replace")[-500:]
    last = r.stdout.decode("utf-8").rstrip().splitlines()[-1]
    assert last in README.splitlines()


def test_the_readme_has_the_platforms_line_and_the_limits_it_promises():
    assert "Core modules tested on Linux; hook, merge gate and demo tested on Windows 11 (PowerShell 5.1 and Git Bash)." in README
    assert "guard, not a sandbox" in README
    assert "winpty abl approve" in README
    assert "winpty abl approve" in (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")


def test_no_absolute_claims_without_a_limit_nearby():
    for word in ("fully", "every change"):
        assert word not in README.lower()


def test_the_decisions_file_numbers_run_from_one():
    nums = [int(n) for n in re.findall(r"(?m)^## ADR-(\d{3}):", (ROOT / "docs" / "decisions.md").read_text(encoding="utf-8"))]
    assert nums == list(range(1, len(nums) + 1)) and 6 <= len(nums) <= 9


def test_the_license_names_the_holder():
    assert "Copyright (c) 2026 cbahena22" in (ROOT / "LICENSE").read_text(encoding="utf-8")


def test_readme_images_exist_and_the_svg_is_inert():
    for rel in re.findall(r"\]\((docs/[^)\s]+\.(?:png|gif|svg))\)", README):
        assert (ROOT / rel).is_file(), rel
    svg = (ROOT / "docs" / "img" / "build-loop.svg").read_text(encoding="utf-8")
    assert "<script" not in svg and "<foreignObject" not in svg and "href" not in svg.replace("xlink:href", "")
    assert "security read" not in svg and "handoff" not in svg


def test_the_moved_write_up_has_one_line_about_the_private_product():
    text = (ROOT / "docs" / "build-loop.md").read_text(encoding="utf-8")
    assert "larger private multi-tenant investigation console; happy to walk through it" in text
    assert "quality-assurance" not in text and "12,000" not in text


# ---- abl2: the plain-English explainer and the new first screen ----
EXPLAINER = ROOT / "docs" / "explainer.md"
JARGON = ("claim", "worktree", "BLOCK", "PASS", "rolled back", "hook", "audit")


def test_every_file_and_test_the_explainer_links_to_exists():
    text = EXPLAINER.read_text(encoding="utf-8")
    rules = re.findall(r"(?m)^(\d)\. \*\*", text)
    assert rules == ["1", "2", "3", "4", "5"]
    for body in re.split(r"(?m)^(?=\d\. \*\*)", text.split("## The five rules")[1].split("![")[0])[1:]:
        links = re.findall(r"\]\(\.\./((?:src|tests)/[^)\s#]+)\)", body)
        assert any(p.startswith("src/") for p in links) and any(p.startswith("tests/") for p in links), body[:60]
        for rel in links:
            assert (ROOT / rel).is_file(), rel
        for name, rel in re.findall(r"`(test_\w+)` in \[`[^`]+`\]\(\.\./(tests/[^)\s]+)\)", body):
            assert re.search(rf"(?m)^def {name}\(", (ROOT / rel).read_text(encoding="utf-8")), (name, rel)
        assert re.search(r"`test_\w+` in", body), body[:60]


def test_the_explainer_is_short_and_keeps_one_analogy():
    text = EXPLAINER.read_text(encoding="utf-8")
    words = len(re.sub(r"\(\.\./?[^)]*\)|`[^`]*`", "", text).split())
    assert 400 <= words <= 800, words
    assert "building site" in text and "site manager" in text and "inspector" in text
    assert "not make the model smarter" in text and "guard, not a sandbox" in text
    assert "Nothing merges without a person's approval" in text


def test_the_explainer_explains_a_word_before_using_it():
    text = EXPLAINER.read_text(encoding="utf-8")
    head = text.split("## The five rules")[0]
    for word in JARGON:
        assert word not in head, word
    assert "git *worktree*: a" in text and "*claim*, the list of files" in text


def test_the_readme_first_screen_is_the_problem_then_five_rules_then_the_picture():
    lines = README.splitlines()
    assert lines[0] == "# agent-build-loop" and lines[2].startswith("Several Claude Code sessions working on one project")
    numbered = [ln for ln in lines[:20] if re.match(r"^\d\. ", ln)]
    assert [ln[0] for ln in numbered] == list("12345") and all(len(ln) < 100 for ln in numbered)
    first_image = next(i for i, ln in enumerate(lines) if ln.startswith("!["))
    assert "docs/img/team.svg" in lines[first_image]
    screen = "\n".join(lines[:first_image])
    assert "docs/explainer.md" in screen
    for word in ("BLOCK", "PASS", "claim", "worktree", "rolled back", "hook"):
        assert word not in screen, word
    # the demo line is explained in words right after it, and the glossary sits below the quickstart
    assert "the guard stopped 1 edit, the reviewer blocked 1 change" in README
    assert README.index("## 30-second quickstart") < README.index("## The words") < README.index("## How it works")


def test_the_team_picture_is_inert_readable_and_phone_sized():
    svg = (ROOT / "docs" / "img" / "team.svg").read_text(encoding="utf-8")
    assert "<script" not in svg and "<foreignObject" not in svg and "href" not in svg.replace("xlink:href", "")
    width = int(re.search(r'viewBox="0 0 (\d+) \d+"', svg).group(1))
    assert width <= 360
    sizes = [float(n) for n in re.findall(r"font:\s*\d+\s+([\d.]+)px", svg)]
    assert sizes and min(sizes) >= 14
    assert "prefers-color-scheme: dark" in svg and re.search(r'<rect class="bg"', svg)
    png = (ROOT / "docs" / "img" / "team.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) < 400 * 1024
    assert b"tEXt" not in png and b"iTXt" not in png and b"zTXt" not in png
    assert "docs/img/team.svg" in README and "img/team.svg" in EXPLAINER.read_text(encoding="utf-8")


def test_the_longer_write_up_keeps_its_diagram_and_points_to_the_explainer():
    text = (ROOT / "docs" / "build-loop.md").read_text(encoding="utf-8")
    assert "img/build-loop.svg" in text and "(explainer.md)" in text


def test_the_explainer_opens_without_naming_another_chat_product():
    text = EXPLAINER.read_text(encoding="utf-8")
    assert "You have used a chat assistant." in text and "ChatGPT" not in text
