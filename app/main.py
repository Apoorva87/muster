"""Composition root.

Wires the repository, artifact store, subscriptions and agents together, and
exposes both entrypoints:

    uv run python -m app.main migrate   # create schema + seed subscriptions
    uv run python -m app.main web       # local timeline + approvals
    uv run python -m app.main serve     # Restate agent service (needs [durable])
    uv run python -m app.main run "..." # run the team here and now, no Docker
    uv run python -m app.main memory    # what the team has learned
"""

from __future__ import annotations

import os
import sys

from app.config import Settings, load_settings
from app.db.repository import Repository
from app.kernel.artifacts import FilesystemArtifactStore
from app.kernel.subscriptions import SubscriptionRegistry


def build_repository(settings: Settings) -> Repository:
    return Repository.from_url(settings.database_url)


def build_store(settings: Settings) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(root=settings.artifact_root)


def migrate(settings: Settings) -> None:
    repo = build_repository(settings)
    repo.init_schema()
    repo.seed_default_subscriptions()
    topics = SubscriptionRegistry(repo).topics()
    print(f"schema ready; {len(topics)} topics seeded: {', '.join(topics)}")


def web(settings: Settings) -> None:
    import uvicorn

    from app.web.app import create_app

    repo = build_repository(settings)

    class IngressResolver:
        """Resolves an awakeable by calling the local Restate ingress.

        Kept here rather than in the web layer so the UI stays free of any
        transport concern (and of any Restate type).
        """

        async def resolve(self, awakeable_id: str, decision: str) -> None:
            import httpx

            url = f"{settings.restate_ingress_url}/restate/awakeables/{awakeable_id}/resolve"
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(url, json={"decision": decision})
                response.raise_for_status()

    uvicorn.run(create_app(repo, IngressResolver()),
                host="0.0.0.0", port=settings.web_port)


def serve(settings: Settings) -> None:
    try:
        from app.runtime.durable import serve as serve_durable
    except ImportError as exc:  # pragma: no cover - needs the optional extra
        raise SystemExit(
            "The durable service needs the optional extra:\n"
            "    uv sync --extra durable\n"
            f"(original error: {exc})") from exc
    serve_durable(settings)


def run(settings: Settings, objective: str = "", *,
        cross_team: bool = False, decision: str = "approve") -> None:
    """Execute a project in this process and print the timeline.

    No Restate, no Postgres, no Docker. Not durable — this is the path for
    watching the choreography and developing agents. `make dev` is the durable
    one; the agents and team.yaml are identical either way.
    """
    import asyncio

    from app.local_runner import LocalRunner, drive
    from app.runtime.llm import registry_from_settings

    objective = objective or "Evaluate whether Company X is attractive at its valuation."
    root = settings.artifact_root
    llm = registry_from_settings(settings)
    print(f"model: {llm.describe()}")

    async def _go() -> list[LocalRunner]:
        runners: list[LocalRunner] = []
        bus = None

        if cross_team:
            from bus.adapters.restate import RestateBusAdapter
            from bus.routing.registry import TeamRegistry

            registry = TeamRegistry(session_id="workstation-01")
            by_id: dict[str, LocalRunner] = {}
            bus = RestateBusAdapter(registry, lambda t: by_id[t].ctx)

            for directory in ("teams/investment", "teams/research"):
                runner = LocalRunner(directory, repository=Repository.from_url("sqlite://"),
                                     artifact_root=root, llm=llm, bus=bus,
                                     session_id="workstation-01",
                                     memory_backend=settings.memory_backend,
                                     recall_limit=settings.memory_recall_limit)
                by_id[runner.team_id] = runner
                registry.register(runner.spec.to_descriptor())
                runners.append(runner)
        else:
            runners.append(LocalRunner("teams/investment",
                                       repository=Repository.from_url("sqlite://"),
                                       artifact_root=root, llm=llm,
                                       memory_backend=settings.memory_backend,
                                       recall_limit=settings.memory_recall_limit))

        head = runners[0]
        await head.kernel().send(
            agent="director",
            task="evaluate_company_delegated" if cross_team else "evaluate_company",
            objective=objective)
        await drive(runners, auto_approve=decision)
        return runners

    runners = asyncio.run(_go())

    for runner in runners:
        runs = runner.repo.list_runs(runner.project_id)
        print(f"\n=== {runner.team_id} ({len(runs)} runs) ===")
        for record in runs:
            duration = f"{record.duration_ms}ms" if record.duration_ms is not None else "-"
            print(f"  {record.started_at:%H:%M:%S}  {record.agent:<14} "
                  f"{record.event_type:<20} {record.status:<18} {duration}")

        artifacts = runner.repo.list_artifacts(runner.project_id)
        if artifacts:
            print(f"  artifacts: " + ", ".join(
                f"{a.type}/{a.created_by}" + ("*" if a.meta.get("external") else "")
                for a in artifacts))

    print("\n(* = reference to another team's artifact; body stays with its owner)")
    print("Not durable — this ran in-process. Use `make dev` for the durable path.")


def memory(settings: Settings, team: str = "") -> None:
    """Show what a team has learned. Memory is files — this just reads them."""
    import asyncio

    from app.memory import store_from_settings

    team_id = team or settings.team_id
    root = settings.memory_root_for(team_id)
    print(f"backend: {settings.memory_backend}   team: {team_id}   root: {root}")

    if settings.memory_backend == "none":
        print("\nmemory is disabled (MEMORY_BACKEND=none)")
        return
    if not root.exists():
        print(f"\nno memory yet — {root} does not exist")
        return

    store = store_from_settings(settings, team_id)

    async def _show() -> None:
        count = await store.rebuild_index()
        notes = await store.recall("", limit=50)
        print(f"\n{count} note(s):\n")
        for ref in notes:
            print(f"  {ref.render()}")
        print(f"\nThese are markdown files under {root} — read, edit or revert "
              f"them with git.")

    asyncio.run(_show())


def providers(settings: Settings) -> None:
    """Show every LLM provider, whether it is usable here, and how to enable it."""
    import shutil

    from app.runtime.llm import PROVIDERS

    print(f"configured: LLM_PROVIDER={settings.llm_provider}"
          + (f"  LLM_MODEL={settings.llm_model}" if settings.llm_model else ""))
    print()
    print(f"  {'provider':<13}{'kind':<6}{'ready':<8}{'default model':<18}how")
    for name in sorted(PROVIDERS):
        spec = PROVIDERS[name]
        if spec.kind == "stub":
            ready, how = True, "always available"
        elif spec.binary:
            ready = shutil.which(spec.binary) is not None
            how = f"needs {spec.binary!r} on PATH"
        else:
            try:
                __import__(name)
                ready = True
            except ImportError:
                ready = False
            how = spec.install
            if spec.needs_key and not os.environ.get(spec.needs_key):
                how += f" + {spec.needs_key}"
        print(f"  {name:<13}{spec.kind:<6}{'yes' if ready else 'no':<8}"
              f"{spec.default_model or '-':<18}{how}")
    print("\nSet LLM_PROVIDER in .env, or override per agent in team.yaml:")
    print("  agents:\n    critic:\n      provider: anthropic\n      model: claude-opus-5")


COMMANDS = {"migrate": migrate, "web": web, "serve": serve, "run": run,
            "providers": providers, "memory": memory}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] not in COMMANDS:
        print(f"usage: python -m app.main [{'|'.join(COMMANDS)}] [objective] "
              "[--cross-team] [--reject]", file=sys.stderr)
        return 2

    command, rest = args[0], args[1:]
    settings = load_settings()

    if command == "run":
        flags = [a for a in rest if a.startswith("--")]
        words: list[str] = []
        skip = False
        for index, token in enumerate(rest):
            if skip:
                skip = False
                continue
            if token == "--provider" and index + 1 < len(rest):
                settings.llm_provider, skip = rest[index + 1], True
            elif token == "--model" and index + 1 < len(rest):
                settings.llm_model, skip = rest[index + 1], True
            elif token == "--memory" and index + 1 < len(rest):
                settings.memory_backend, skip = rest[index + 1], True
            elif not token.startswith("--"):
                words.append(token)
        run(settings, " ".join(words),
            cross_team="--cross-team" in flags,
            decision="reject" if "--reject" in flags else "approve")
    elif command == "memory":
        memory(settings, rest[0] if rest else "")
    else:
        COMMANDS[command](settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
