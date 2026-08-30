"""The V2/V3 Day-2 demonstration: two independently defined teams, one bus.

    investment/director  --command-->  team://research/web-researcher
                                              |
                                       research.report.ready
                                              |
    investment/director  <--event-----  (bus fan-out)
                         -> proposal -> critic -> synthesis -> human -> done

Each team has its own repository, its own artifact store and its own context.
The bus carries references and correlation metadata only.
"""
import asyncio
from pathlib import Path

import pytest

import app.agents          # registers the investment team's agents
import teams.research.agents.web_researcher  # noqa: F401  registers the second team
from app.agents.base import AgentContext, StubLLMRunner, dispatch
from app.db.repository import Repository
from app.kernel.artifacts import FilesystemArtifactStore
from app.kernel.context import FakeKernelContext
from app.kernel.models import Task, TaskStatus
from app.kernel.runtime import Kernel
from app.kernel.subscriptions import SubscriptionRegistry
from app.kernel.team_spec import load_team_spec
from bus.adapters.restate import RestateBusAdapter
from bus.models.address import Address
from bus.models.message import Message, MessageKind
from bus.routing.registry import TeamRegistry

SESSION = "workstation-01"


class TeamHarness:
    """One team: its own spec, repository, artifact store and context."""

    def __init__(self, directory: str, tmp_path: Path, bus=None):
        self.directory = Path(directory)
        self.spec = load_team_spec(directory)
        self.spec.load_entrypoints()
        self.team_id = self.spec.team_id
        self.project_id = self.team_id

        self.repo = Repository.from_url("sqlite://")
        self.repo.init_schema()
        self.spec.seed_into(self.repo)

        self.store = FilesystemArtifactStore(root=tmp_path / self.team_id)
        self.ctx = FakeKernelContext(key=self.project_id)
        self.bus = bus

    def kernel(self, ctx=None) -> Kernel:
        return Kernel(ctx=ctx or self.ctx, repository=self.repo,
                      subscriptions=SubscriptionRegistry(self.repo),
                      artifacts=self.store, bus=self.bus,
                      team_id=self.team_id, session_id=SESSION,
                      public_topics=self.spec.public.topics)

    def agent_ctx(self) -> AgentContext:
        # One journal per invocation, exactly as Restate scopes it.
        return AgentContext(kernel=self.kernel(self.ctx.invocation()),
                            llm=StubLLMRunner(),
                            prompts_dir=self.directory / "prompts")

    def local_task(self, send) -> Task:
        """Materialise the receiving team's own task for an incoming send.

        A team never inherits the sender's task row — it reconstructs its own
        bounded task from the envelope. This is what the real Restate handler
        does on entry, and it handles both shapes it can arrive in: a direct
        team-local send, and a bus Message envelope from another team.
        """
        payload = send.payload

        if "kind" in payload:            # a bus envelope
            inner = payload.get("payload") or {}
            topic = payload.get("topic")
            # The MESSAGE id, deliberately — never the sender's task id.
            # A team mints its own task; reusing the sender's collides with the
            # sender's own record when both teams share a launch. The origin is
            # preserved in `source` and `correlation_id` instead.
            task_id = payload["id"]
            task_type = f"on:{topic}" if topic else inner.get("type", "handle")
            objective = inner.get("objective", "") or f"react to {topic}"
            input_refs = payload.get("artifact_refs") or {}
            source = f"team://{payload['source_team']}/{payload['source_agent']}"
            correlation_id = payload.get("correlation_id")
        else:                            # a direct team-local send
            task_id = payload["task_id"]
            task_type = payload.get("type", "handle")
            objective = payload.get("objective", "")
            input_refs = payload.get("input_refs", {})
            source = None
            correlation_id = None

        existing = self.repo.get_task(task_id)
        if existing is not None:
            return existing

        return self.repo.save_task(Task(
            id=task_id, project_id=self.project_id, type=task_type,
            objective=objective, assigned_agent=send.agent,
            input_refs=input_refs, source=source,
            correlation_id=correlation_id))


async def drive(harnesses, *, decision="approve", rounds=50):
    seen: set[int] = set()
    inflight: list[asyncio.Task] = []

    for _ in range(rounds):
        progressed = False
        for harness in harnesses:
            for send in list(harness.ctx.sends):
                if id(send) in seen or send.delay is not None:
                    continue
                seen.add(id(send))
                task = harness.local_task(send)
                inflight.append(asyncio.create_task(
                    dispatch(send.agent, harness.agent_ctx(), task,
                             team=harness.team_id)))
                progressed = True

        await asyncio.sleep(0)

        for harness in harnesses:
            for run in harness.repo.list_waiting_runs(harness.project_id):
                harness.ctx.resolve_awakeable(run.awakeable_id,
                                              {"decision": decision})
        await asyncio.sleep(0)

        if not progressed and all(t.done() for t in inflight):
            break

    await asyncio.wait_for(asyncio.gather(*inflight), timeout=10)


@pytest.fixture
def session(tmp_path):
    registry = TeamRegistry(session_id=SESSION)
    harnesses: dict[str, TeamHarness] = {}

    def ctx_factory(team_id: str):
        return harnesses[team_id].ctx

    bus = RestateBusAdapter(registry, ctx_factory)

    for directory in ("teams/investment", "teams/research"):
        harness = TeamHarness(directory, tmp_path, bus=bus)
        harnesses[harness.team_id] = harness
        registry.register(harness.spec.to_descriptor())

    return {"registry": registry, "bus": bus, "teams": harnesses,
            "investment": harnesses["investment"],
            "research": harnesses["research"]}


# --------------------------------------------------------- V2 criteria 1 & 2

def test_both_teams_register_with_one_bus_session(session):
    assert session["registry"].team_ids() == ["investment", "research"]
    assert session["registry"].session_id == SESSION


def test_teams_are_independently_defined(session):
    investment = session["registry"].get("investment")
    research = session["registry"].get("research")
    assert set(investment.agent_names).isdisjoint(research.agent_names)
    assert research.public_commands == ["research_company"]


# ----------------------------------------------------- V2 criteria 3, 4 & 5

@pytest.fixture
async def delegated_run(session):
    investment = session["investment"]
    await investment.kernel().send(
        agent="director", task="evaluate_company_delegated",
        objective="Evaluate whether Company X is attractive at its valuation.")
    await drive(list(session["teams"].values()))
    return session


async def test_director_commands_the_other_team(delegated_run):
    research = delegated_run["research"]
    assert [t.assigned_agent for t in research.repo.list_tasks("research")] \
        == ["web-researcher"]


async def test_second_team_completes_and_publishes(delegated_run):
    research = delegated_run["research"]
    assert "research.report.ready" in {e.topic for e in research.repo.list_events("research")}
    assert [a.created_by for a in research.repo.list_artifacts("research")] == ["web-researcher"]


async def test_investment_team_wakes_from_the_cross_team_event(delegated_run):
    """The headline: work started in one team is continued by another."""
    investment = delegated_run["investment"]
    topics = {e.topic for e in investment.repo.list_events("investment")}
    assert "proposal.ready" in topics, "the investment team never resumed"
    assert "critique.complete" in topics


async def test_contexts_stay_isolated_across_the_boundary(delegated_run):
    """Each team keeps its own tasks, artifacts and timeline."""
    investment, research = delegated_run["investment"], delegated_run["research"]
    assert investment.repo.list_tasks("research") == []
    assert research.repo.list_tasks("investment") == []
    assert investment.repo.list_runs("research") == []


async def test_a_foreign_artifact_crosses_as_a_reference_not_a_body(delegated_run):
    """The bus carries the ID; the bytes stay with the team that made them."""
    investment, research = delegated_run["investment"], delegated_run["research"]

    produced = {a.id for a in research.repo.list_artifacts("research")}
    shared = [a for a in investment.repo.list_artifacts("investment")
              if a.id in produced]

    assert len(shared) == 1, "exactly the research report should have crossed"
    reference = shared[0]
    assert reference.meta.get("external") is True
    assert reference.path == "", "a reference must carry no local path"
    assert reference.created_by.startswith("team://research/")

    # The body genuinely is not in the investment team's store.
    with pytest.raises(KeyError):
        await investment.store.get(reference.id)

    # ...and every artifact investment actually produced has a real body.
    for artifact in investment.repo.list_artifacts("investment"):
        if artifact.id != reference.id:
            assert await investment.store.get(artifact.id)


# --------------------------------------------------------- V2 criterion 6

async def test_a_bus_topic_wakes_subscribers_in_more_than_one_team(session):
    registry = session["registry"]
    registry.get("research").subscriptions.append(("market.changed", "web-researcher"))
    registry.get("investment").subscriptions.append(("market.changed", "director"))

    woken = await session["bus"].publish("market.changed", Message(
        kind=MessageKind.EVENT, topic="market.changed", session_id=SESSION,
        source_team="investment", source_agent="monitor"))

    assert {a.team for a in woken} == {"investment", "research"}


# --------------------------------------------------------- V2 criterion 8

async def test_a_duplicate_message_id_does_no_duplicate_work(session):
    research = session["research"]
    command = Message(kind=MessageKind.COMMAND, session_id=SESSION,
                      source_team="investment", source_agent="director",
                      destination="team://research/web-researcher",
                      payload={"task_id": "task_dup", "type": "research_company",
                               "objective": "X"})

    await session["bus"].send(Address.parse(command.destination), command)
    first = len(research.ctx.sends)
    await session["bus"].send(Address.parse(command.destination), command)

    assert len(research.ctx.sends) == first, "redelivery created duplicate work"


# --------------------------------------------------------- V2 criterion 9

async def test_human_approval_pauses_and_resumes_across_the_flow(delegated_run):
    investment = delegated_run["investment"]
    approvals = [r for r in investment.repo.list_runs("investment")
                 if r.event_type == "approval.requested"]
    assert approvals, "the delegated flow never reached a human"
    assert approvals[0].output_refs["decision"] == "approve"
    assert investment.repo.list_waiting_runs("investment") == []


async def test_rejection_propagates(session):
    investment = session["investment"]
    await investment.kernel().send(agent="director",
                                   task="evaluate_company_delegated",
                                   objective="Evaluate X")
    await drive(list(session["teams"].values()), decision="reject")
    assert any(t.status is TaskStatus.REJECTED
               for t in investment.repo.list_tasks("investment"))


# -------------------------------------------------------- V2 criterion 10

async def test_correlation_ids_connect_the_cross_team_path(session):
    origin = Message(kind=MessageKind.COMMAND, session_id=SESSION,
                     source_team="investment", source_agent="director",
                     destination="team://research/web-researcher",
                     trace_id="0af7651916cd43dd8448eb211c80319c")
    hop = origin.caused(kind=MessageKind.EVENT, topic="research.report.ready",
                        source_team="research", source_agent="web-researcher")

    assert hop.correlation_id == origin.id
    assert hop.causation_id == origin.id
    assert hop.trace_id == origin.trace_id
    assert hop.session_id == origin.session_id


async def test_bus_messages_stay_small(delegated_run):
    """The bus carries references, never reports."""
    message = Message(kind=MessageKind.EVENT, topic="research.report.ready",
                      session_id=SESSION, source_team="research",
                      source_agent="web-researcher",
                      artifact_refs={"report": "art_abc123"})
    assert not message.is_oversized
    assert len(message.model_dump_json()) < 1024


# ------------------------------------------------------------------- V3

async def test_the_second_team_needed_no_runtime_changes():
    """V3's thesis: a new team is config + prompts + agents, nothing else."""
    import ast

    source = Path("teams/research/agents/web_researcher.py").read_text()
    tree = ast.parse(source)
    imported = {n.module.split(".")[0] for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module}

    assert "restate" not in imported, "a team author must never import Restate"
    assert "bus" not in imported, "a team author must never import the bus"
    assert imported <= {"__future__", "app"}, f"unexpected imports: {imported}"
