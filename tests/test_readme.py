"""The README makes checkable claims. These check them.

A README that names files, commands and counts rots silently as the repo moves,
and a stale one is worse than a short one — it sends readers to things that are
not there. Everything asserted here is derived from the code.
"""
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"


@pytest.fixture(scope="module")
def text():
    return README.read_text()


def test_readme_exists(text):
    assert len(text) > 2000, "a README this short cannot explain the repo"


# ------------------------------------------------------------------ paths

def test_every_path_in_the_layout_block_exists(text):
    """The repository layout is the easiest section to let rot."""
    block = re.search(r"## Repository layout\s*\n+```text\n(.*?)```", text, re.DOTALL)
    assert block, "the README no longer documents the repository layout"

    # Only the entries at column 0 are repo-root paths; the indented ones are
    # nested under whichever root precedes them.
    top = re.findall(r"^([\w.][\w.-]*/)", block.group(1), re.MULTILINE)
    missing = [p for p in sorted(set(top)) if not (REPO / p).exists()]
    assert not missing, f"layout names directories that do not exist: {missing}"


def test_inline_repo_paths_exist(text):
    paths = set(re.findall(
        r"`((?:app|bus|teams|demo|tests|template|docs|scripts|\.claude)/[\w./-]+)`", text))
    missing = [p for p in sorted(paths) if not (REPO / p).exists()]
    assert not missing, f"README names paths that do not exist: {missing}"


# --------------------------------------------------------------- commands

def test_every_make_target_it_names_exists(text):
    makefile = (REPO / "Makefile").read_text()
    # Backticks required: prose like "twenty rejections make twenty notes"
    # is not a command reference.
    targets = set(re.findall(r"`make ([a-z-]+)`", text))
    missing = [t for t in sorted(targets)
               if not re.search(rf"^{re.escape(t)}:", makefile, re.MULTILINE)]
    assert not missing, f"README names missing make targets: {missing}"


def test_every_user_facing_cli_command_is_documented(text):
    from app.main import COMMANDS

    # `serve` is reached through `make dev`, which the README does show.
    expected = set(COMMANDS) - {"serve"}
    missing = [c for c in sorted(expected) if f"app.main {c}" not in text]
    assert not missing, f"README never shows: {missing}"


# ------------------------------------------------------------ capabilities

def test_every_llm_provider_is_listed(text):
    from app.runtime.llm import PROVIDERS

    missing = [p for p in PROVIDERS if f"`{p}`" not in text]
    assert not missing, f"README omits provider(s): {missing}"


def test_every_memory_backend_is_listed(text):
    from app.memory import BACKENDS

    missing = [b for b in BACKENDS if f"`{b}`" not in text]
    assert not missing, f"README omits memory backend(s): {missing}"


def test_every_prd_it_links_exists(text):
    for link in re.findall(r"\(docs/prd/[\w.-]+\)", text):
        target = link.strip("()")
        assert (REPO / target).exists(), f"broken PRD link: {target}"


def test_both_skills_it_advertises_exist(text):
    for skill in ("muster-new", "muster-buzz"):
        assert skill in text, f"README never mentions the {skill} skill"
        assert (REPO / ".claude/skills" / skill / "SKILL.md").is_file()


# ------------------------------------------------------------------ claims

def test_the_test_count_it_claims_is_true(text):
    """A number in a README is a promise. Keep it or drop it."""
    claimed = re.search(r"(\d{3,})\s+tests", text)
    assert claimed, "the README claims a test count; keep it accurate or remove it"

    collected = subprocess.run(
        ["uv", "run", "pytest", "--collect-only", "-q",
         "-o", "addopts=-m 'not integration'"],
        cwd=REPO, capture_output=True, text=True, timeout=300)
    actual = len(re.findall(r"^\S+::", collected.stdout, re.MULTILINE))

    assert abs(int(claimed.group(1)) - actual) <= 15, (
        f"README claims {claimed.group(1)} tests; pytest collects {actual}")


def test_it_states_the_design_rule(text):
    """If this sentence ever leaves, the project has lost its thesis."""
    collapsed = " ".join(
        line.lstrip("> ").strip() for line in text.splitlines()).replace("  ", " ")
    assert "code describes agent semantics" in collapsed
    assert "Restate handles distributed-systems semantics" in collapsed


def test_it_warns_that_in_process_is_not_durable(text):
    assert "NOT durable" in text


def test_it_is_honest_about_what_is_unfinished(text):
    """A README that only lists wins is marketing, not documentation."""
    collapsed = " ".join(text.split()).lower()
    assert "not yet done" in collapsed or "unfinished" in collapsed
    assert "license for muster itself has not been chosen" in collapsed
