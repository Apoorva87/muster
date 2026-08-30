"""Starting work — one API, every surface.

Until now a project could only begin from the CLI (`python -m app.main run`).
Three surfaces need the same thing — a button in the web UI, a chat command, an
external trigger — so the launch path lives here once and they all call it:

    launcher = Launcher()
    result = await launcher.launch("Evaluate Company X")   # parks on the human
    print(result.summary())
    await launcher.resolve(result.waiting[0].id, "approve")  # and finishes

This module is the *composition* of pieces that already exist — `LocalRunner`,
`drive`, the LLM registry, the repository — and adds no runtime semantics of
its own. It is the in-process path: not durable, no Restate, no Docker. `make
dev` remains the durable one; agents and team.yaml are identical either way.

Project scoping
---------------
`LocalRunner` defaults ``project_id`` to the team id, so two launches from one
Launcher would write into a single timeline and their runs would interleave.
Instead every launch mints a short token and scopes each participating team's
project to ``<team-id>-<token>`` (``investment-3f9a2c11``). Consequences:

* runs, tasks, events and artifacts of one launch never collide with another's;
* each team keeps **one** repository across every launch, so the web UI can
  list all of a team's projects on one page;
* a cross-team launch shares its token, so ``investment-3f9a2c11`` and
  ``research-3f9a2c11`` are visibly the same piece of work while each team
  still owns its own timeline.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from app.config import Settings, load_settings
from app.db.repository import Repository
from app.kernel.models import Artifact, RunRecord, TaskStatus
from app.kernel.team_spec import TeamSpec, load_team_spec
from app.local_runner import LocalRunner, drive
from app.runtime.llm import registry_from_settings

#: The team a bare ``Launcher()`` starts, matching `python -m app.main run`.
DEFAULT_TEAMS: tuple[str, ...] = ("teams/investment",)
#: ...and what `--cross-team` adds to it.
CROSS_TEAM_TEAMS: tuple[str, ...] = ("teams/investment", "teams/research")

#: Session identity for the bus, as in `app/main.py:run`.
SESSION_ID = "workstation-01"

#: How long a launch may keep working before we call it stuck.
DRIVE_TIMEOUT = 900.0
#: A launch parked on a human waits for the human, not for a clock.
PARKED_TIMEOUT = 86_400.0
#: How often the settle loop looks at the repository.
POLL = 0.02

_HUMAN_STATUS = {
    "WAITING_FOR_HUMAN": "waiting for a human decision",
    "COMPLETE": "complete",
    "REJECTED": "rejected by a human",
    "FAILED": "failed",
}


class LaunchError(RuntimeError):
    """A launch could not be started or resumed."""


class UnknownTeam(LaunchError):
    """The requested team is not one this Launcher was built with."""


# ------------------------------------------------------------------- result


class LaunchResult(BaseModel):
    """A snapshot of one launch, taken when it finished or parked on a human.

    It covers every team that took part, so a cross-team launch reports as one
    piece of work even though each team keeps its own timeline.
    """

    project_id: str
    team_id: str
    task_id: str
    objective: str
    status: str
    runs: list[RunRecord] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    waiting: list[RunRecord] = Field(default_factory=list)

    @property
    def agents(self) -> list[str]:
        """Every agent that appears in the timeline, kernel bookkeeping aside."""
        return sorted({r.agent for r in self.runs if r.agent != "kernel"})

    def summary(self) -> str:
        """A short, human-readable report a chat client can post verbatim."""
        state = _HUMAN_STATUS.get(self.status, self.status.lower())
        lines = [
            f"{self.team_id}: {self.objective}",
            f"{state} — project {self.project_id}, "
            f"{len(self.runs)} run{'' if len(self.runs) == 1 else 's'}, "
            f"{len(self.artifacts)} artifact{'' if len(self.artifacts) == 1 else 's'}",
        ]
        if self.agents:
            lines.append("agents: " + ", ".join(self.agents))
        for run in self.waiting:
            lines.append(f"waiting on you: {run.agent} — {run.event_type} ({run.id})")
        if not self.waiting:
            lines.append("nothing is waiting on you.")
        return "\n".join(lines)


@dataclass
class _Launch:
    """One launch in flight: its runners and the task draining their sends."""

    project_id: str
    team_id: str
    token: str
    objective: str
    task_id: str
    runners: dict[str, LocalRunner]
    driver: asyncio.Task | None = None
    settled: dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------- launcher


class Launcher:
    """Starts and supervises team runs. The one way work begins.

    One Launcher can start any number of launches; each gets its own project
    (see the module docstring) while every team keeps a single repository, so
    ``repository_for("investment")`` is a stable handle the web UI can hold.
    """

    def __init__(self, *, teams: list[str] | None = None,
                 settings: Settings | None = None,
                 llm: Any = None,
                 artifact_root: str | Path | None = None,
                 cross_team: bool = False) -> None:
        self.settings = settings or load_settings()
        self.cross_team = cross_team
        self.llm = llm if llm is not None else registry_from_settings(self.settings)
        self.artifact_root = Path(
            artifact_root if artifact_root is not None else self.settings.artifact_root)

        entries = list(teams) if teams else list(
            CROSS_TEAM_TEAMS if cross_team else DEFAULT_TEAMS)
        self._directories: dict[str, Path] = {}
        self._specs: dict[str, TeamSpec] = {}
        for entry in entries:
            path = Path(entry)
            spec = load_team_spec(path)
            self._directories[spec.team_id] = path if path.is_dir() else path.parent
            self._specs[spec.team_id] = spec

        #: Head team first: the one a launch addresses when none is named.
        self._order: list[str] = list(self._directories)
        self._repositories: dict[str, Repository] = {}
        self._launches: dict[str, _Launch] = {}
        self._latest: _Launch | None = None

    # ------------------------------------------------------------- surface

    @property
    def team_ids(self) -> list[str]:
        return list(self._order)

    @property
    def runners(self) -> dict[str, LocalRunner]:
        """The runners of the most recent launch, keyed by team id.

        Empty until something has been launched — runners are per launch,
        because a launch is what owns a project id.
        """
        return dict(self._latest.runners) if self._latest else {}

    def repository_for(self, team_id: str) -> Repository:
        """This team's store, shared by every launch of it.

        In-memory SQLite, matching `python -m app.main run`: the in-process
        path is not durable, and this keeps it free of Postgres and Docker.
        Hand this object to :func:`app.web.app.create_app` and the UI sees
        every project the team has run.
        """
        key = self._resolve(team_id)
        if key not in self._repositories:
            repository = Repository.from_url("sqlite://")
            repository.init_schema()
            self._repositories[key] = repository
        return self._repositories[key]

    async def launch(self, objective: str, *, team: str | None = None,
                     task: str | None = None,
                     auto_approve: str | None = None) -> LaunchResult:
        """Start work and wait until it finishes or parks on a human.

        ``auto_approve`` answers the human decision point for you ("approve" /
        "reject"). Leave it None — the default — and the workflow stays parked
        so a person can answer it later through :meth:`resolve`; that is what
        the web button and the chat command need.
        """
        objective = objective.strip()
        if not objective:
            raise LaunchError("an objective is required to start work")

        team_id = self._resolve(team) if team else self._order[0]
        launch = self._build(team_id, objective)
        agent, task_type = self._entrypoint(team_id, task)

        head = launch.runners[team_id]
        record = await head.kernel().send(agent=agent, task=task_type,
                                          objective=objective)
        launch.task_id = record.id

        self._launches[launch.project_id] = launch
        self._latest = launch
        launch.driver = asyncio.create_task(drive(
            list(launch.runners.values()), auto_approve=auto_approve,
            timeout=DRIVE_TIMEOUT if auto_approve is not None else PARKED_TIMEOUT))

        if auto_approve is not None:
            await launch.driver          # drive answers the human; it will end
        else:
            await self._settle(launch)   # ...otherwise stop when it parks
        return self.result(launch.project_id)

    async def resolve(self, run_id: str, decision: str) -> None:
        """Answer a parked run, then let the workflow run on to completion.

        Awaiting this returns once the launch has finished (or parked again on
        a second decision), so a caller can report the outcome immediately.
        """
        for launch in self._launches.values():
            for runner in launch.runners.values():
                run = runner.repo.get_run(run_id)
                if run is None or run.project_id != runner.project_id:
                    continue
                if run.awakeable_id is None:
                    raise LaunchError(
                        f"run {run_id} is not waiting for a human decision")
                runner.ctx.resolve_awakeable(run.awakeable_id,
                                             {"decision": decision})
                await self._settle(launch)
                return
        raise LaunchError(f"unknown run: {run_id}")

    def waiting(self) -> list[RunRecord]:
        """Every run, across every launch, parked on a human right now."""
        parked: list[RunRecord] = []
        for launch in self._launches.values():
            parked.extend(self._waiting_in(launch))
        return parked

    def result(self, project_id: str | None = None) -> LaunchResult:
        """Re-snapshot a launch — after :meth:`resolve`, for instance."""
        launch = (self._launches[project_id] if project_id is not None
                  else self._latest)
        if launch is None:
            raise LaunchError("nothing has been launched yet")

        runs: list[RunRecord] = []
        artifacts: list[Artifact] = []
        rejected = False
        for runner in launch.runners.values():
            runs.extend(runner.repo.list_runs(runner.project_id))
            artifacts.extend(runner.repo.list_artifacts(runner.project_id))
            rejected = rejected or any(t.status is TaskStatus.REJECTED
                                       for t in runner.repo.list_tasks(runner.project_id))

        parked = self._waiting_in(launch)
        if parked:
            status = "WAITING_FOR_HUMAN"
        elif any(r.status == "FAILED" for r in runs):
            status = "FAILED"
        elif rejected:
            status = "REJECTED"
        else:
            status = "COMPLETE"

        return LaunchResult(
            project_id=launch.project_id, team_id=launch.team_id,
            task_id=launch.task_id, objective=launch.objective, status=status,
            runs=sorted(runs, key=lambda r: r.started_at),
            artifacts=artifacts, waiting=parked)

    async def aclose(self) -> None:
        """Drop any launch still parked on a human. For shutdown and tests."""
        drivers = [l.driver for l in self._launches.values()
                   if l.driver is not None and not l.driver.done()]
        for driver in drivers:
            driver.cancel()
        if drivers:
            await asyncio.gather(*drivers, return_exceptions=True)

    # ------------------------------------------------------------ internals

    def _resolve(self, team: str) -> str:
        """Map a team id — or the directory it was configured with — to its id."""
        if team in self._directories:
            return team
        for team_id, directory in self._directories.items():
            if Path(team) in (directory, directory / "team.yaml"):
                return team_id
        raise UnknownTeam(
            f"unknown team {team!r}; this launcher knows: {sorted(self._directories)}")

    def _entrypoint(self, team_id: str, task: str | None) -> tuple[str, str]:
        """Which agent to wake, and with what task type.

        A team declares what it can be asked for in ``public.commands``; the
        first is its front door. Cross-team mode asks for the delegated variant
        of that command, the convention `app/agents/director.py` uses to send
        part of the work to another team. Pass ``task=`` to override either.
        """
        spec = self._specs[team_id]
        agent = "director" if "director" in spec.agents else spec.agent_names[0]
        if task:
            return agent, task
        command = spec.public.commands[0] if spec.public.commands else "handle"
        if self.cross_team and len(self._order) > 1:
            command = f"{command}_delegated"
        return agent, command

    def _build(self, team_id: str, objective: str) -> _Launch:
        """Construct this launch's runners over a fresh project id."""
        token = uuid4().hex[:8]
        participants = ([team_id] + [t for t in self._order if t != team_id]
                        if self.cross_team else [team_id])

        bus = None
        registry = None
        runners: dict[str, LocalRunner] = {}
        if self.cross_team:
            # Imported here, never at module top level: a standalone V1 team
            # must work with the bus package absent (CLAUDE.md).
            from bus.adapters.restate import RestateBusAdapter
            from bus.routing.registry import TeamRegistry

            registry = TeamRegistry(session_id=SESSION_ID)
            bus = RestateBusAdapter(registry, lambda t: runners[t].ctx)

        for participant in participants:
            runner = LocalRunner(
                self._directories[participant],
                repository=self.repository_for(participant),
                artifact_root=self.artifact_root, llm=self.llm, bus=bus,
                session_id=SESSION_ID if self.cross_team else "local",
                project_id=f"{participant}-{token}")
            runners[participant] = runner
            if registry is not None:
                registry.register(runner.spec.to_descriptor())

        return _Launch(project_id=runners[team_id].project_id, team_id=team_id,
                       token=token, objective=objective, task_id="",
                       runners=runners)

    def _waiting_in(self, launch: _Launch) -> list[RunRecord]:
        parked: list[RunRecord] = []
        for runner in launch.runners.values():
            parked.extend(runner.repo.list_waiting_runs(runner.project_id))
        return parked

    def _fingerprint(self, launch: _Launch) -> tuple[int, ...]:
        """Cheap "has anything happened" signal, read from the repository."""
        counts: list[int] = []
        for runner in launch.runners.values():
            counts.append(len(runner.repo.list_runs(runner.project_id)))
            counts.append(len(runner.repo.list_tasks(runner.project_id)))
            counts.append(len(runner.repo.list_artifacts(runner.project_id)))
        return tuple(counts)

    async def _settle(self, launch: _Launch) -> None:
        """Wait until the launch ends, or parks on a human and goes quiet.

        ``drive`` only returns when nothing is left in flight, and a workflow
        suspended on a human never is — so waiting on the driver alone would
        block until its timeout. We stop instead at the first moment a run is
        parked *and* the timeline has stopped moving, leaving the driver alive
        to carry on the instant :meth:`resolve` answers it.
        """
        previous: tuple[int, ...] | None = None
        assert launch.driver is not None
        while not launch.driver.done():
            fingerprint = self._fingerprint(launch)
            if fingerprint == previous and self._waiting_in(launch):
                return
            previous = fingerprint
            await asyncio.sleep(POLL)
        await launch.driver  # surface a TimeoutError or an agent's failure


class LauncherResolver:
    """Adapts a :class:`Launcher` to the web layer's ``ApprovalResolver``.

    The UI knows an awakeable id; the Launcher owns the runner holding that
    promise. This is the ten lines between them, so the Approve button finishes
    a run the same page started.
    """

    def __init__(self, launcher: Launcher) -> None:
        self._launcher = launcher

    async def resolve(self, awakeable_id: str, decision: str) -> None:
        for run in self._launcher.waiting():
            if run.awakeable_id == awakeable_id:
                await self._launcher.resolve(run.id, decision)
                return
        raise LaunchError(f"no parked run holds awakeable {awakeable_id}")
