"""The `muster-new` skill is a document, so test what a document can get wrong.

A skill that tells Claude to read `template/team.yaml` and to emit a `team.yaml`
in a particular shape is a *contract against the repo*. The real failure mode is
not that it is badly written — it is that the repo moves and the skill quietly
starts giving stale instructions: a path that no longer exists, a provider list
that gained a member, a never-edit list that drifted from template/README.md.

These tests fail the moment that happens.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.kernel.team_spec import TeamSpec
from app.runtime.llm import provider_names

REPO = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO / ".claude" / "skills" / "muster-new"
SKILL = SKILL_DIR / "SKILL.md"
REFERENCES = ("reference/topology.md", "reference/checklist.md")

#: Phrases the skill must trigger on. The whole point of a skill is that it fires
#: without being named, so these live in the description or nowhere.
TRIGGERS = ("new muster team", "create an agent team", "bootstrap a team")

FENCE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)
INLINE = re.compile(r"`([^`\n]+)`")
#: A repo path: has a separator, and none of the characters that mark a
#: placeholder (`<id>`), a glob, a URL or prose.
PATHLIKE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*(?:/[A-Za-z0-9_.\-]+)+/?$")


def read(name: str) -> str:
    return (SKILL_DIR / name).read_text(encoding="utf-8")


def split_frontmatter(text: str) -> tuple[dict, str]:
    assert text.startswith("---\n"), "SKILL.md must open with YAML frontmatter"
    _, raw, body = text.split("---\n", 2)
    return yaml.safe_load(raw), body


def prose(text: str) -> str:
    """The document minus fenced code blocks — instructions, not examples."""
    return FENCE.sub("", text)


def referenced_paths(text: str) -> set[str]:
    """Every repo path the document points a reader at."""
    found: set[str] = set()
    for span in INLINE.findall(prose(text)):
        for token in span.split():
            token = token.rstrip(".,;:)")
            if PATHLIKE.match(token):
                found.add(token)
    return found


# ------------------------------------------------------------------ structure

def test_skill_exists():
    assert SKILL.is_file(), f"no skill at {SKILL}"


def test_frontmatter_is_valid_yaml_with_name_and_description():
    meta, body = split_frontmatter(SKILL.read_text(encoding="utf-8"))
    assert isinstance(meta, dict), "frontmatter must be a mapping"
    assert meta.get("name") == "muster-new"
    assert isinstance(meta.get("description"), str) and meta["description"].strip()
    assert body.strip(), "SKILL.md has frontmatter but no procedure"


@pytest.mark.parametrize("phrase", TRIGGERS)
def test_description_carries_the_triggering_phrases(phrase):
    meta, _ = split_frontmatter(SKILL.read_text(encoding="utf-8"))
    assert phrase in meta["description"].lower(), (
        f"the skill will not fire on {phrase!r}")


@pytest.mark.parametrize("name", REFERENCES)
def test_referenced_reference_files_exist(name):
    assert (SKILL_DIR / name).is_file()
    assert name in SKILL.read_text(encoding="utf-8"), (
        f"{name} exists but the skill never points at it")


# ------------------------------------------------------- it must not go stale

@pytest.mark.parametrize("document", ("SKILL.md",) + REFERENCES)
def test_every_path_the_skill_names_still_exists(document):
    """A skill that tells Claude to read a moved file is worse than no skill."""
    missing = [p for p in referenced_paths(read(document))
               if not (REPO / p).exists() and not (SKILL_DIR / p).exists()]
    assert not missing, f"{document} points at paths that no longer exist: {sorted(missing)}"


def test_skill_names_the_real_provider_list():
    text = SKILL.read_text(encoding="utf-8")
    missing = [name for name in provider_names() if name not in text]
    assert not missing, (
        f"app/runtime/llm.py offers providers the skill never mentions: {missing}")


def test_never_edit_list_matches_the_template_readme():
    """template/README.md is the source of record for what a team owner must not touch."""
    readme = (REPO / "template" / "README.md").read_text(encoding="utf-8")
    section = readme.split("## What you must never need to edit", 1)[1]
    sentence = section.split(".", 1)[0]
    items = [i.strip() for i in " ".join(sentence.split()).split(",")]
    assert len(items) >= 6, f"failed to parse the never-edit list: {items}"

    checklist = read("reference/checklist.md")
    missing = [i for i in items if i.lower() not in checklist.lower()]
    assert not missing, (
        f"reference/checklist.md has drifted from template/README.md: {missing}")


# ------------------------------------------------- the emitted contract works

def yaml_team_blocks(text: str) -> list[dict]:
    blocks = re.findall(r"^```yaml\n(.*?)^```", text, re.DOTALL | re.MULTILINE)
    parsed = [yaml.safe_load(b) for b in blocks]
    return [p for p in parsed if isinstance(p, dict) and "team" in p]


def test_skill_embeds_a_team_yaml_example():
    assert yaml_team_blocks(SKILL.read_text(encoding="utf-8")), (
        "the skill must show a complete team.yaml, not describe one")


def test_embedded_team_yaml_validates():
    """The example is what Claude will copy. It must survive load_team_spec."""
    for raw in yaml_team_blocks(SKILL.read_text(encoding="utf-8")):
        spec = TeamSpec.model_validate(raw)
        spec.check()
        assert spec.agents, "example declares no agents"
        for topic in spec.public.topics:
            assert topic.startswith(f"{spec.team_id}."), (
                f"example public topic {topic!r} is not namespaced")
        for name, agent in spec.agents.items():
            assert agent.entrypoint.startswith(f"teams.{spec.team_id}.agents."), (
                f"example entrypoint for {name!r} does not follow teams.<id>.agents.<module>")


# --------------------------------------------------------- the procedure itself

def test_procedure_quotes_the_v3_topology_rule():
    """The critique step is the point of the skill; it must argue from the PRD."""
    text = SKILL.read_text(encoding="utf-8").lower()
    prd = (REPO / "docs" / "prd" / "v3-custom-teams.md").read_text(encoding="utf-8").lower()
    clause = "do not create agents merely to simulate job titles"
    assert clause in prd, "the PRD moved; re-check what the skill quotes"
    assert clause in text, "the skill must quote V3's topology rule verbatim"


def test_procedure_covers_every_required_step():
    text = SKILL.read_text(encoding="utf-8").lower()
    for step in ("interview", "critique", "generate", "verify", "hand off"):
        assert step in text, f"the procedure is missing its {step!r} step"


def test_verification_commands_are_present_and_runnable_as_written():
    text = SKILL.read_text(encoding="utf-8")
    assert "load_team_spec" in text and "load_entrypoints()" in text, (
        "the skill must verify the spec imports every entrypoint")
    assert "uv run pytest" in text, "verification must run under uv"
    assert "system python" not in text.lower()
    assert not re.search(r"(?<!uv run )\bpython3? -m pytest\b", text), (
        "tests must be invoked as `uv run pytest`")


def test_skill_forbids_runtime_imports_in_team_code():
    """The one rule the existing suite already enforces on team authors."""
    text = SKILL.read_text(encoding="utf-8")
    assert "app.agents.base" in text and "app.kernel.models" in text
    assert "restate" in text.lower() and "bus" in text.lower()


@pytest.mark.parametrize("name", REFERENCES)
def test_reference_files_are_substantive(name):
    body = read(name)
    assert len(body.splitlines()) > 20, f"{name} is a stub"
