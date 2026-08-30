"""The muster-buzz skill is a document, so test what can actually go stale.

A skill that names files, commands and constants rots silently as the repo
moves. These assertions fail loudly instead.
"""
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO / ".claude/skills/muster-buzz"
SKILL = SKILL_DIR / "SKILL.md"
REFERENCES = ("reference/rooms.md", "reference/operating.md")


@pytest.fixture(scope="module")
def text():
    return SKILL.read_text()


@pytest.fixture(scope="module")
def frontmatter(text):
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md must open with YAML frontmatter"
    return yaml.safe_load(match.group(1))


def test_skill_exists():
    assert SKILL.is_file()


def test_frontmatter_has_name_and_description(frontmatter):
    assert frontmatter["name"] == "muster-buzz"
    assert len(frontmatter["description"]) > 80


@pytest.mark.parametrize("phrase", [
    "connect my team to Buzz", "run muster from chat", "set up a Buzz room",
    "start a project from chat",
])
def test_description_carries_the_triggering_phrases(frontmatter, phrase):
    assert phrase in frontmatter["description"]


@pytest.mark.parametrize("name", REFERENCES)
def test_reference_files_exist(name):
    assert (SKILL_DIR / name).is_file()


@pytest.mark.parametrize("document", [SKILL, *[SKILL_DIR / r for r in REFERENCES]])
def test_every_repo_path_the_skill_names_still_exists(document):
    """The commonest way a skill like this rots."""
    body = document.read_text()
    paths = set(re.findall(r"`((?:app|bus|teams|demo|tests|template|scripts)/[\w./-]+)`", body))
    missing = [p for p in paths if not (REPO / p).exists()]
    assert not missing, f"{document.name} names paths that no longer exist: {missing}"


def test_it_states_the_control_plane_boundary(text):
    """The single most important thing to get right about Buzz."""
    collapsed = " ".join(text.split())
    assert "control plane" in collapsed and "not the transport" in collapsed
    assert "Restate" in collapsed


def test_it_warns_that_derived_keys_are_dev_only(text):
    collapsed = " ".join(text.split()).lower()
    assert "seed" in collapsed and "private key" in collapsed


def test_it_covers_the_whole_interview(text):
    """A setup skill is only useful if it asks the questions that matter."""
    lowered = " ".join(text.split()).lower()
    for topic in ("relay", "channel", "allow-list", "approve", "identity",
                  "never appear"):
        assert topic in lowered, f"interview omits {topic!r}"


def test_it_names_the_commands_the_room_actually_accepts(text):
    from bus.adapters.buzz_live import COMMAND_PATTERN

    verbs = re.search(r"\((run\|[^)]*)\)", COMMAND_PATTERN.pattern).group(1).split("|")
    for verb in verbs:
        assert f"`{verb}" in text, f"skill does not document the {verb!r} command"


def test_semantic_topics_described_match_the_code(text):
    """If the allow-list changes, the skill must not keep promising the old one."""
    from bus.adapters.buzz import SEMANTIC_TOPICS

    # Collapse whitespace: markdown wraps lines, and a phrase split across two
    # lines is a formatting detail, not a missing topic.
    prose = " ".join(text.split()).lower()
    for topic in SEMANTIC_TOPICS:
        # "proposal.ready" is described to humans as "proposal ready".
        phrase = topic.removeprefix("system.").replace(".", " ")
        assert phrase in prose, f"skill omits the {topic!r} moment ({phrase!r})"


def test_it_names_the_filtered_internal_events(text):
    for internal in ("event.delivered", "event.published", "wakeup.scheduled"):
        assert internal in text


def test_verification_commands_reference_real_tests(text):
    for path in re.findall(r"tests/test_\w+\.py", text):
        assert (REPO / path).is_file(), f"{path} does not exist"


def test_it_points_at_the_working_demo(text):
    assert "make buzz-demo" in text
    assert (REPO / "demo/buzz_session.py").is_file()


def test_make_target_exists():
    assert "buzz-demo:" in (REPO / "Makefile").read_text()


def test_muster_new_hands_off_to_this_skill():
    """The two skills must chain: create a team, then put it in a room."""
    assert "muster-buzz" in (REPO / ".claude/skills/muster-new/SKILL.md").read_text()


def test_it_does_not_claim_buzz_carries_agent_to_agent_traffic(text):
    """The misunderstanding this skill exists to prevent."""
    collapsed = " ".join(text.split()).lower()
    assert "human" in collapsed and "agent" in collapsed
    assert "agent↔agent goes over the bus" in collapsed or \
           "agent-to-agent" in collapsed or "agent↔agent" in collapsed
