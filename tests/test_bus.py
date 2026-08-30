"""The V2 seam. Defined in V1, local-only implementation."""
import inspect

import pytest

from app.kernel.bus import Address, BusAdapter, LocalBusAdapter, TeamDescriptor
from app.kernel.subscriptions import SubscriptionRegistry


@pytest.fixture
def bus(repo):
    return LocalBusAdapter(SubscriptionRegistry(repo), team_id="investment")


def test_local_adapter_satisfies_the_protocol(bus):
    assert isinstance(bus, BusAdapter)


def test_bare_agent_name_is_a_local_address():
    addr = Address.parse("finance")
    assert addr.is_local and addr.agent == "finance"


def test_hierarchical_address_parses():
    addr = Address.parse("team://research/web-researcher")
    assert (addr.team, addr.agent) == ("research", "web-researcher")
    assert not addr.is_local
    assert str(addr) == "team://research/web-researcher"


def test_malformed_address_is_rejected():
    with pytest.raises(ValueError):
        Address.parse("team://onlyteam")


async def test_local_address_resolves(bus):
    assert await bus.resolve(Address.parse("finance")) == "finance"


async def test_same_team_address_resolves(bus):
    assert await bus.resolve(Address.parse("team://investment/finance")) == "finance"


async def test_cross_team_address_fails_loudly_in_v1(bus):
    """V1 must not silently misroute — V2 is what makes this work."""
    with pytest.raises(NotImplementedError, match="V2 bus adapter"):
        await bus.resolve(Address.parse("team://research/web-researcher"))


async def test_registration_round_trips(bus):
    await bus.register_team(TeamDescriptor(team_id="investment", version=1,
                                           agents={"critic": ["challenge"]}))
    assert bus.known_teams() == ["investment"]
    await bus.unregister_team("investment")
    assert bus.known_teams() == []


async def test_bus_resolves_topic_subscribers(bus):
    assert set(await bus.subscribers_for("proposal.ready")) == {"critic", "finance"}


def test_contract_imports_no_restate_module():
    """CLAUDE.md: no Restate SDK type may appear in this contract."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("app/kernel/bus.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "restate" not in imported


def test_contract_annotations_use_no_restate_types():
    from typing import get_type_hints
    for method in ("register_team", "resolve", "subscribers_for", "subscribe"):
        hints = get_type_hints(getattr(BusAdapter, method))
        assert not any("estate" in str(v) for v in hints.values()), method
