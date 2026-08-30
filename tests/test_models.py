"""Domain model contract. These types appear in the public kernel API."""
import pytest

from app.kernel.ids import new_id
from app.kernel.models import Artifact, Event, Subscription, Task, TaskStatus


def test_ids_are_prefixed_and_unique():
    a, b = new_id("task"), new_id("task")
    assert a.startswith("task_") and b.startswith("task_")
    assert a != b


def test_ids_are_stable_length():
    assert len({len(new_id("run")) for _ in range(50)}) == 1


def test_task_has_prd_required_fields():
    t = Task(project_id="proj_1", type="analyze", objective="Value Company X",
             assigned_agent="finance")
    for field in ("id", "project_id", "type", "objective", "assigned_agent",
                  "status", "created_at", "parent_task_id", "input_refs"):
        assert hasattr(t, field), f"PRD requires Task.{field}"
    assert t.status is TaskStatus.PENDING
    assert t.parent_task_id is None
    assert t.input_refs == {}


def test_task_status_covers_waiting_for_human():
    assert TaskStatus.WAITING_FOR_HUMAN.value == "WAITING_FOR_HUMAN"


def test_task_round_trips_through_json():
    t = Task(project_id="proj_1", type="analyze", objective="o",
             assigned_agent="critic", input_refs={"proposal": "art_x"})
    assert Task.model_validate_json(t.model_dump_json()) == t


def test_event_carries_references_not_payloadsize():
    e = Event(topic="proposal.ready", project_id="proj_1",
              payload={"artifact_id": "art_1"})
    assert e.topic == "proposal.ready"
    assert e.id.startswith("evt_")


def test_artifact_has_prd_required_metadata():
    a = Artifact(project_id="proj_1", task_id="task_1", type="markdown",
                 path="/tmp/x.md", created_by="research")
    for field in ("id", "project_id", "task_id", "type", "path",
                  "created_by", "created_at", "meta"):
        assert hasattr(a, field), f"PRD requires Artifact.{field}"


def test_subscription_maps_topic_to_agent():
    s = Subscription(topic="proposal.ready", agent="critic")
    assert (s.topic, s.agent) == ("proposal.ready", "critic")
