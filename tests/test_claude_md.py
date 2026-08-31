"""CLAUDE.md is an agent's map of this repo. These tests keep the map honest.

A wrong map is worse than no map: it sends the next agent to files that moved
and commands that no longer exist. Everything asserted here is derived from the
code, including the claims about what is *unfinished* — when someone finishes
one of those, this suite tells them to say so.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "CLAUDE.md"


@pytest.fixture(scope="module")
def text():
    return DOC.read_text()


# ------------------------------------------------------------------ paths

def test_every_path_it_names_exists(text):
    paths = set(re.findall(
        r"`((?:app|bus|teams|demo|tests|template|docs|scripts|\.claude)"
        r"/[\w./-]*)`", text))
    missing = [p for p in sorted(paths) if not (REPO / p.rstrip("/")).exists()]
    assert not missing, f"CLAUDE.md names paths that do not exist: {missing}"


def test_the_orientation_table_covers_every_top_level_package(text):
    """A map that silently omits a package sends the reader nowhere."""
    packages = {p.name for p in REPO.iterdir()
                if p.is_dir() and (p / "__init__.py").exists()}
    packages |= {"docs", "scripts", "template"}
    missing = [p for p in sorted(packages) if f"`{p}/" not in text]
    assert not missing, f"CLAUDE.md does not mention: {missing}"


def test_referenced_docs_resolve(text):
    docs = set(re.findall(r"`(docs/[\w./-]+\.md)`", text))
    assert docs, "CLAUDE.md should point at the PRDs and specs"
    missing = [d for d in sorted(docs) if not (REPO / d).exists()]
    assert not missing, f"CLAUDE.md links missing docs: {missing}"


# --------------------------------------------------------------- commands

def test_every_make_target_it_names_exists(text):
    makefile = (REPO / "Makefile").read_text()
    # Backticks required: prose like "twenty rejections make twenty notes"
    # is not a command reference.
    targets = set(re.findall(r"`make ([a-z-]+)`", text))
    missing = [t for t in sorted(targets)
               if not re.search(rf"^{re.escape(t)}:", makefile, re.MULTILINE)]
    assert not missing, f"CLAUDE.md names missing make targets: {missing}"


def test_every_cli_command_it_names_is_real(text):
    from app.main import COMMANDS

    named = set(re.findall(r"app\.main ([a-z]+)", text))
    unreal = sorted(named - set(COMMANDS))
    assert not unreal, f"CLAUDE.md names commands that do not exist: {unreal}"

    listed = re.search(r"`app/main\.py` \| CLI: (.+?) \|", text)
    assert listed, "CLAUDE.md no longer lists the CLI commands"
    documented = set(re.findall(r"`([a-z]+)`", listed.group(1)))
    assert documented == set(COMMANDS), (
        f"CLI drifted: CLAUDE.md says {sorted(documented)}, "
        f"code has {sorted(COMMANDS)}")


# ------------------------------------------------------------ the rules

def test_the_prime_directive_is_still_stated(text):
    assert ("Our code describes agent semantics. Restate handles "
            "distributed-systems semantics.") in text


def test_the_kernel_context_seam_is_described(text):
    """The single fact that explains why the suite needs no infrastructure."""
    from app.kernel.context import FakeKernelContext, KernelContext  # noqa: F401

    assert "KernelContext" in text and "FakeKernelContext" in text


def test_documented_quirks_are_still_present_in_the_code():
    assert "StaticPool" in (REPO / "app/db/repository.py").read_text()
    assert "MUSTER_ENGINE" in (REPO / "scripts/compose.sh").read_text()
    assert "--no-dev" in (REPO / "setup.sh").read_text()
    assert "field_validator" in (REPO / "app/kernel/team_spec.py").read_text()
    assert "teams/*/memory/" in (REPO / ".gitignore").read_text()


def test_memory_backends_and_permissions_match_the_code(text):
    from app.memory import BACKENDS

    assert "MEMORY_BACKEND=none" in text and "none" in BACKENDS


# ------------------------------------------ honesty about unfinished work

def test_it_still_claims_the_start_route_is_missing(text):
    """When someone wires the button, this fails — update the doc."""
    routes = (REPO / "app/web/app.py").read_text()
    claims_missing = "No `POST /start`" in text
    actually_missing = 'post("/start"' not in routes
    assert claims_missing == actually_missing, (
        "CLAUDE.md and app/web/app.py disagree about whether /start exists")


def test_it_still_claims_there_is_no_license(text):
    claims_missing = "No `LICENSE` file" in text
    assert claims_missing == (not (REPO / "LICENSE").exists()), (
        "a LICENSE was added — CLAUDE.md still says there is none")


def test_the_crash_recovery_test_it_points_at_exists(text):
    assert "tests/test_crash_recovery.py" in text
    assert (REPO / "tests/test_crash_recovery.py").exists()


def test_the_skills_it_names_exist(text):
    for skill in ("muster-new", "muster-buzz"):
        assert skill in text
        assert (REPO / ".claude/skills" / skill / "SKILL.md").exists()
