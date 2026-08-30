"""team.yaml — V3's declarative team contract.

A new team should be configuration plus domain code. These tests are the
generic checks V3 requires of any team, run against the two real teams.
"""
import textwrap

import pytest

from app.kernel.team_spec import SpecError, TeamSpec, load_team_spec

TEAMS = ("teams/investment", "teams/research")


def write(tmp_path, body: str):
    path = tmp_path / "team.yaml"
    path.write_text(textwrap.dedent(body))
    return path


# ------------------------------------------------------------- the real teams

@pytest.mark.parametrize("directory", TEAMS)
def test_real_team_config_validates(directory):
    load_team_spec(directory)


@pytest.mark.parametrize("directory", TEAMS)
def test_every_configured_agent_loads(directory):
    load_team_spec(directory).load_entrypoints()


@pytest.mark.parametrize("directory", TEAMS)
def test_subscriptions_reference_declared_agents(directory):
    spec = load_team_spec(directory)
    for topic, agent in spec.subscription_pairs():
        assert agent in spec.agents, f"{topic} -> {agent} is not declared"


@pytest.mark.parametrize("directory", TEAMS)
def test_public_topics_are_namespaced(directory):
    spec = load_team_spec(directory)
    for topic in spec.public.topics:
        assert topic.startswith(f"{spec.team_id}."), topic


def test_the_two_teams_are_independently_defined():
    investment = load_team_spec("teams/investment")
    research = load_team_spec("teams/research")
    assert investment.team_id != research.team_id
    assert not set(investment.agent_names) & set(research.agent_names), \
        "teams must not share agent names in one bus session"


def test_research_team_is_minimal():
    """V3: do not create agents to simulate job titles."""
    spec = load_team_spec("teams/research")
    assert len(spec.agents) == 1
    assert spec.public.commands == ["research_company"]


# ---------------------------------------------------------------- validation

def test_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(SpecError, match="no team.yaml"):
        load_team_spec(tmp_path)


def test_broken_yaml_is_a_clear_error(tmp_path):
    path = write(tmp_path, "team: {id: x\n  bad")
    with pytest.raises(SpecError, match="not valid YAML"):
        load_team_spec(path)


def test_subscription_to_undeclared_agent_is_rejected(tmp_path):
    path = write(tmp_path, """
        team: {id: broken}
        agents:
          worker: {entrypoint: app.agents.research}
        subscriptions:
          - {topic: a.b, agent: ghost}
        """)
    with pytest.raises(SpecError, match="no such agent"):
        load_team_spec(path)


def test_team_with_no_agents_is_rejected(tmp_path):
    path = write(tmp_path, "team: {id: empty}\nagents: {}\n")
    with pytest.raises(SpecError, match="declares no agents"):
        load_team_spec(path)


def test_duplicate_subscription_is_rejected(tmp_path):
    path = write(tmp_path, """
        team: {id: dupe}
        agents:
          worker: {entrypoint: app.agents.research}
        subscriptions:
          - {topic: a.b, agent: worker}
          - {topic: a.b, agent: worker}
        """)
    with pytest.raises(SpecError, match="duplicate"):
        load_team_spec(path)


def test_bad_team_id_is_rejected(tmp_path):
    path = write(tmp_path, """
        team: {id: "not a slug!"}
        agents:
          worker: {entrypoint: app.agents.research}
        """)
    with pytest.raises(ValueError):
        load_team_spec(path)


def test_unimportable_entrypoint_is_a_clear_error(tmp_path):
    path = write(tmp_path, """
        team: {id: typo}
        agents:
          worker: {entrypoint: app.agents.nosuchmodule}
        """)
    with pytest.raises(SpecError, match="not importable"):
        load_team_spec(path).load_entrypoints()


# ------------------------------------------------------------- bus projection

def test_spec_projects_to_a_bus_descriptor():
    descriptor = load_team_spec("teams/research").to_descriptor()
    assert descriptor.team_id == "research"
    assert descriptor.agents_with("research_company") == ["web-researcher"]
    assert descriptor.public_topics == ["research.report.ready"]


def test_standalone_team_never_needs_the_bus():
    """A team must work with V1 alone; the bus import is lazy."""
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path("app/kernel/team_spec.py").read_text())
    top_level = {n.module.split(".")[0] for n in tree.body
                 if isinstance(n, ast.ImportFrom) and n.module}
    top_level |= {a.name.split(".")[0] for n in tree.body
                  if isinstance(n, ast.Import) for a in n.names}
    assert "bus" not in top_level


def test_spec_seeds_the_v1_subscription_table(repo):
    spec = load_team_spec("teams/research")
    spec.seed_into(repo)
    assert repo.subscribers_for("research.requested") == ["web-researcher"]


def test_template_ships_a_valid_team_yaml():
    """The thing new teams copy must itself be valid."""
    spec = TeamSpec.model_validate(
        __import__("yaml").safe_load(open("template/team.yaml").read()))
    spec.check()
    assert spec.team_id == "myteam"
