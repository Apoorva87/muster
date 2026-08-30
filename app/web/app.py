"""The local timeline and human-approval page.

Two rules shape this module:

* **No Restate types here.** Approve/Reject call an injected
  :class:`ApprovalResolver`; resolving the durable promise is somebody else's
  job (CLAUDE.md: Restate SDK types must not appear in a public signature).
  That seam is also what makes the whole page testable with a fake.
* **No network at render time.** Everything — CSS included — is inlined, so the
  page works on a laptop with the wifi off.

Read-only views come straight from :class:`~app.db.repository.Repository`; the
page holds no state of its own.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.db.models import RunRow
from app.db.repository import Repository
from app.kernel.models import RunRecord

TEMPLATES = Path(__file__).parent / "templates"

APPROVE = "approve"
REJECT = "reject"


class ApprovalResolver(Protocol):
    """Resumes a workflow parked on a human decision.

    The production implementation resolves a Restate awakeable; tests pass a
    recorder. Either way the web layer only knows these two strings.
    """

    async def resolve(self, awakeable_id: str, decision: str) -> None:
        ...


# ------------------------------------------------------------------ rendering


def _format_duration(run: RunRecord) -> str:
    ms = run.duration_ms
    if ms is None:
        if run.finished_at is None:
            return "—"
        started = run.started_at
        ms = max(0, int((run.finished_at - started).total_seconds() * 1000))
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms // 60000}m {(ms % 60000) // 1000}s"


def _format_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).strftime("%H:%M:%S")


def _format_date(value: datetime | None) -> str:
    if value is None:
        return "—"
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _is_waiting(run: RunRecord) -> bool:
    """A run the human can act on: parked on a promise, not yet finished."""
    return run.awakeable_id is not None and run.finished_at is None


def _project_ids(repository: Repository) -> list[str]:
    """Every project that has produced a run.

    ``Repository`` has no ``list_projects`` yet, so this reads the runs table
    directly. If the repository grows the method, it wins automatically and
    this fallback can be deleted.
    """
    lister = getattr(repository, "list_projects", None)
    if callable(lister):
        return list(lister())
    with repository.engine.connect() as conn:
        rows = conn.execute(
            select(RunRow.project_id).distinct().order_by(RunRow.project_id)
        )
        return [r[0] for r in rows]


# -------------------------------------------------------------------- factory


def create_app(repository: Repository, resolver: ApprovalResolver) -> FastAPI:
    """Build the page over an explicit repository and approval seam."""
    app = FastAPI(title="Muster")
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters["duration"] = _format_duration
    templates.env.filters["clock"] = _format_time
    templates.env.filters["stamp"] = _format_date

    def _timeline(request: Request, project_id: str):
        runs = repository.list_runs(project_id)
        return templates.TemplateResponse(
            request,
            "timeline.html",
            {
                "project_id": project_id,
                "runs": runs,
                "waiting_count": sum(1 for r in runs if _is_waiting(r)),
                "is_waiting": _is_waiting,
                "projects": _project_ids(repository),
            },
        )

    @app.get("/")
    def index(request: Request):
        projects = _project_ids(repository)
        if len(projects) == 1:
            return _timeline(request, projects[0])
        return templates.TemplateResponse(
            request,
            "projects.html",
            {
                "projects": [
                    {"id": p, "runs": repository.list_runs(p),
                     "waiting": len(repository.list_waiting_runs(p))}
                    for p in projects
                ],
            },
        )

    @app.get("/project/{project_id}")
    def project(request: Request, project_id: str):
        return _timeline(request, project_id)

    @app.get("/run/{run_id}")
    def run_detail(request: Request, run_id: str):
        run = repository.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")
        artifacts: list[Any] = []
        if run.task_id:
            artifacts = [a for a in repository.list_artifacts(run.project_id)
                         if a.task_id == run.task_id]
        return templates.TemplateResponse(
            request,
            "run.html",
            {
                "run": run,
                "task": repository.get_task(run.task_id) if run.task_id else None,
                "artifacts": artifacts,
                "waiting": _is_waiting(run),
            },
        )

    async def _decide(run_id: str, decision: str) -> RedirectResponse:
        run = repository.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")
        if run.awakeable_id is None:
            # Decision D3: without a persisted promise ID there is nothing to
            # resume. Say so plainly instead of blowing up.
            raise HTTPException(
                status_code=409,
                detail=f"run {run_id} is not waiting for a human decision",
            )
        await resolver.resolve(run.awakeable_id, decision)
        # The workflow owns the run's final status; we only retire the promise
        # so the button cannot be pressed twice.
        repository.clear_awakeable(run_id)
        return RedirectResponse(f"/project/{run.project_id}", status_code=303)

    @app.post("/approve/{run_id}")
    async def approve(run_id: str):
        return await _decide(run_id, APPROVE)

    @app.post("/reject/{run_id}")
    async def reject(run_id: str):
        return await _decide(run_id, REJECT)

    return app
