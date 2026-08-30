"""Topic -> agents. The public API must hide that this is a SQL table."""
import pytest

from app.db.repository import Repository
from app.kernel.subscriptions import SubscriptionRegistry


@pytest.fixture
def registry():
    repo = Repository.from_url("sqlite://")
    repo.init_schema()
    repo.seed_default_subscriptions()
    return SubscriptionRegistry(repo)


def test_topic_fans_out_to_multiple_agents(registry):
    assert set(registry.subscribers_for("proposal.ready")) == {"critic", "finance"}


def test_market_changed_wakes_two_subscribers(registry):
    """PRD demo requirement: proves fan-out."""
    assert len(registry.subscribers_for("market.changed")) >= 2


def test_unknown_topic_is_not_an_error(registry):
    assert registry.subscribers_for("nobody.listens") == []


def test_subscribe_adds_a_route(registry):
    registry.subscribe("custom.topic", "monitor")
    assert registry.subscribers_for("custom.topic") == ["monitor"]


def test_subscribe_is_idempotent(registry):
    registry.subscribe("custom.topic", "monitor")
    registry.subscribe("custom.topic", "monitor")
    assert registry.subscribers_for("custom.topic") == ["monitor"]


def test_registry_exposes_no_sql_types(registry):
    """Implementation must stay hidden behind the abstraction."""
    public = [n for n in dir(registry) if not n.startswith("_")]
    assert public == ["subscribe", "subscribers_for", "topics"]
