"""Every new team must ship one end-to-end scenario. Start here.

These are the generic checks V3 requires of any team; add your own domain
scenario below them.
"""

import pytest

from app.kernel.team_spec import load_team_spec

TEAM_DIR = "."  # point at your team directory


@pytest.fixture
def spec():
    return load_team_spec(TEAM_DIR)


def test_team_config_validates(spec):
    spec.check()


def test_every_configured_agent_loads(spec):
    spec.load_entrypoints()


def test_subscriptions_reference_declared_agents(spec):
    for topic, agent_name in spec.subscription_pairs():
        assert agent_name in spec.agents, f"{topic} -> {agent_name} is not declared"


def test_public_topics_are_namespaced(spec):
    for topic in spec.public.topics:
        assert topic.startswith(f"{spec.team_id}."), (
            f"public topic {topic!r} should be namespaced with the team id")
