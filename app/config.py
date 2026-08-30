"""Environment-backed settings.

Model choice must not be coupled to the coordination kernel, so nothing here is
required to run the kernel or its tests — only to run the real service.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "muster"
    postgres_user: str = "muster"
    postgres_password: str = "change-me"

    restate_ingress_url: str = "http://localhost:8080"
    restate_admin_url: str = "http://localhost:9070"
    muster_service_port: int = 9080

    artifact_root: Path = Path("./data/artifacts")
    web_port: int = 8000

    #: One of app.runtime.llm.PROVIDERS: stub | anthropic | openai | ollama
    #: | claude_code | codex. Default costs nothing and needs no network.
    llm_provider: str = "stub"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_effort: str = "medium"
    llm_timeout: float = 600.0

    bus_adapter: str = "local"
    team_id: str = "investment"

    @property
    def database_url(self) -> str:
        return (f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
                f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}")


def load_settings() -> Settings:
    return Settings()
