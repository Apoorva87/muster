"""Composition root.

Wires the repository, artifact store, subscriptions and agents together, and
exposes both entrypoints:

    uv run python -m app.main migrate   # create schema + seed subscriptions
    uv run python -m app.main web       # local timeline + approvals
    uv run python -m app.main serve     # Restate agent service (needs [durable])
"""

from __future__ import annotations

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


COMMANDS = {"migrate": migrate, "web": web, "serve": serve}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] not in COMMANDS:
        print(f"usage: python -m app.main [{'|'.join(COMMANDS)}]", file=sys.stderr)
        return 2
    COMMANDS[args[0]](load_settings())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
