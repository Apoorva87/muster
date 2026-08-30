"""The bus session control page — V2's shared control-plane visibility.

Three read-only views over the registry, in the same visual language as the V1
timeline (``app.web.app``):

* ``/``            — the muster roll: every registered team, its health, and how
                     much work it currently has in flight.
* ``/team/{id}``   — one team's published contract, and the way through to its
                     V1 project/event timeline.
* ``/topics``      — the routing table across every team, so cross-team wiring
                     is visible at a glance.

Two rules carried over from the V1 page, for the same reasons:

* **No Restate types here.** This module reads a :class:`TeamRegistry` and a
  :class:`Repository` and renders them. Nothing else.
* **No network at render time.** The templates extend V1's ``base.html``, whose
  CSS is inlined, so the page works with the wifi off.

Reusing V1's base template is deliberate: this is the same product's control
page gaining a session view, not a second UI. The only thing added on top is
the health vocabulary (``.h-healthy`` / ``.h-degraded`` / ``.h-unreachable``),
which follows V1's existing ``--tone`` pill convention.

Project → team mapping
----------------------
V1 keys everything by ``project_id`` and knows nothing about teams: a run
record has no team column, and a project ID is just the Restate Virtual Object
key (``app.kernel.runtime.Kernel.project_id``). Within a bus session each team
runs its own V1 runtime, so the only mapping the code actually supports today
is **``project_id == team_id``**. That is what :func:`projects_for_team`
implements, and it is the single place to change when teams grow a real
project index — every count on these pages goes through it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates

from app.db.repository import Repository
from app.web import app as v1_web
from bus.models.address import Address
from bus.models.team import TeamDescriptor
from bus.routing.registry import TeamRegistry, UnknownTeam

TEMPLATES = Path(__file__).parent / "templates"
#: V1's templates come along so this page can extend its base and inherit the
#: design tokens, the status vocabulary and the dark-mode handling verbatim.
V1_TEMPLATES = Path(v1_web.__file__).parent / "templates"

RUNNING = "RUNNING"


# ------------------------------------------------------------------ counting


def projects_for_team(team_id: str) -> list[str]:
    """The V1 project IDs that belong to ``team_id``.

    See the module docstring: V1 has no team column, so a team's project is the
    one named after it. Kept as a function because it is the seam a richer
    mapping would replace.
    """
    return [team_id]


def team_counts(repository: Repository, team_id: str) -> dict[str, int]:
    """Runs, running and waiting counts for a team, straight from V1 state."""
    runs = [r for pid in projects_for_team(team_id)
            for r in repository.list_runs(pid)]
    waiting = [r for pid in projects_for_team(team_id)
               for r in repository.list_waiting_runs(pid)]
    return {
        "runs": len(runs),
        "running": sum(1 for r in runs if r.status == RUNNING),
        "waiting": len(waiting),
    }


def _team_row(repository: Repository, team: TeamDescriptor) -> dict[str, Any]:
    return {
        "team_id": team.team_id,
        "description": team.description,
        "health": team.health.value,
        "agents": len(team.agents),
        **team_counts(repository, team.team_id),
    }


# ------------------------------------------------------------- routing table


def routing_table(registry: TeamRegistry) -> list[dict[str, Any]]:
    """Every topic on the bus, with who publishes it and who listens.

    A topic appears if any team subscribes to it or declares it in
    ``public_topics``; that way a topic published into the void, or one
    subscribed to but declared by nobody, is visible rather than silently
    missing.
    """
    teams = registry.teams()
    topics = sorted(
        {topic for team in teams for topic, _ in team.subscriptions}
        | {topic for team in teams for topic in team.public_topics}
    )
    table: list[dict[str, Any]] = []
    for topic in topics:
        subscribers: list[Address] = registry.subscribers_for(topic)
        listening = sorted({a.team for a in subscribers if a.team})
        table.append({
            "topic": topic,
            "publishers": [t.team_id for t in teams if topic in t.public_topics],
            "subscribers": subscribers,
            "teams": listening,
            "cross_team": len(listening) > 1,
        })
    return table


# -------------------------------------------------------------------- factory


def create_app(
    registry: TeamRegistry,
    repository: Repository,
    *,
    timeline_base: str = "",
) -> FastAPI:
    """Build the control page over an explicit registry and repository.

    ``timeline_base`` prefixes the links out to the V1 timeline, for when that
    UI is served on another port (``app.config.Settings.web_port``). Empty
    means "same origin", which is the single-process default.
    """
    app = FastAPI(title="Muster Bus")
    templates = Jinja2Templates(directory=[str(TEMPLATES), str(V1_TEMPLATES)])
    # Same filters, same formatting as the V1 timeline.
    templates.env.filters["duration"] = v1_web._format_duration
    templates.env.filters["clock"] = v1_web._format_time
    templates.env.filters["stamp"] = v1_web._format_date

    timeline: Callable[[str], str] = lambda pid: f"{timeline_base}/project/{pid}"
    templates.env.globals["timeline_url"] = timeline

    def _descriptor(team_id: str) -> TeamDescriptor:
        try:
            return registry.get(team_id)
        except UnknownTeam:
            raise HTTPException(
                status_code=404,
                detail=f"unknown team: {team_id}",
            ) from None

    @app.get("/")
    def session(request: Request):
        return templates.TemplateResponse(
            request,
            "session.html",
            {
                "nav": "session",
                "session_id": registry.session_id,
                "teams": [_team_row(repository, t) for t in registry.teams()],
            },
        )

    @app.get("/team/{team_id}")
    def team_detail(request: Request, team_id: str):
        descriptor = _descriptor(team_id)
        return templates.TemplateResponse(
            request,
            "team.html",
            {
                "nav": "team",
                "session_id": registry.session_id,
                "team": descriptor,
                "health": descriptor.health.value,
                "counts": team_counts(repository, team_id),
                "projects": [
                    {"id": pid,
                     "runs": repository.list_runs(pid),
                     "waiting": len(repository.list_waiting_runs(pid))}
                    for pid in projects_for_team(team_id)
                ],
            },
        )

    @app.get("/topics")
    def topics(request: Request):
        return templates.TemplateResponse(
            request,
            "topics.html",
            {
                "nav": "topics",
                "session_id": registry.session_id,
                "rows": routing_table(registry),
            },
        )

    return app
