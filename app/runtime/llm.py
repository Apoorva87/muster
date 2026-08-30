"""Pluggable LLM providers.

Model choice must not be coupled to the coordination kernel (V1 PRD), so every
provider satisfies one narrow protocol — ``LLMRunner.run()`` — and the kernel
never learns which one is in use.

Two shapes of provider live here:

* **api**  — a hosted or local model endpoint (Anthropic, OpenAI, Ollama).
* **cli**  — an agentic coding CLI driven as a subprocess (Claude Code, Codex).
  These are not chat completions: they run a whole agent with its own tools and
  return a final answer. Useful when you want an agent that can read files and
  run commands, not just produce text.

Every SDK is an optional dependency. Importing this module never requires any
of them; a provider raises a clear, actionable error only when selected.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from typing import Literal

ProviderKind = Literal["stub", "api", "cli"]

#: Anthropic's current default. See docs/prd — model choice is configuration.
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
DEFAULT_OPENAI_MODEL = "gpt-5.2"
DEFAULT_OLLAMA_MODEL = "llama3.2"


class ProviderError(RuntimeError):
    """The selected provider cannot run — with instructions for fixing it."""


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    kind: ProviderKind
    default_model: str
    install: str = ""
    needs_key: str = ""
    binary: str = ""
    summary: str = ""


PROVIDERS: dict[str, ProviderSpec] = {
    "stub": ProviderSpec(
        "stub", "stub", "", summary="Deterministic. No network, no cost, no model."),
    "anthropic": ProviderSpec(
        "anthropic", "api", DEFAULT_ANTHROPIC_MODEL,
        install="uv sync --extra anthropic", needs_key="ANTHROPIC_API_KEY",
        summary="Claude via the official Anthropic SDK."),
    "openai": ProviderSpec(
        "openai", "api", DEFAULT_OPENAI_MODEL,
        install="uv sync --extra openai", needs_key="OPENAI_API_KEY",
        summary="OpenAI via the official SDK."),
    "ollama": ProviderSpec(
        "ollama", "api", DEFAULT_OLLAMA_MODEL,
        install="uv sync --extra openai", binary="ollama",
        summary="A local model through Ollama's OpenAI-compatible endpoint."),
    "claude_code": ProviderSpec(
        "claude_code", "cli", "", binary="claude",
        summary="Claude Code as a subprocess agent — has tools and file access."),
    "codex": ProviderSpec(
        "codex", "cli", "", binary="codex",
        summary="Codex CLI as a subprocess agent — has tools and file access."),
}


def provider_names() -> list[str]:
    return sorted(PROVIDERS)


# --------------------------------------------------------------------- stub


class StubRunner:
    """Deterministic stand-in. Keeps the whole test suite model-free."""

    provider = "stub"
    model = ""

    async def run(self, *, instructions: str, input: str, agent: str = "") -> str:
        head = input.strip().splitlines()[0] if input.strip() else "(no input)"
        return f"[{agent or 'agent'}] {head[:200]}"


# ---------------------------------------------------------------------- api


class AnthropicRunner:
    """Claude through the official Anthropic SDK."""

    provider = "anthropic"

    def __init__(self, *, model: str = DEFAULT_ANTHROPIC_MODEL, api_key: str = "",
                 effort: str = "medium", max_tokens: int = 16000,
                 timeout: float = 600.0) -> None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:
            raise ProviderError(
                "provider 'anthropic' needs the SDK: uv sync --extra anthropic") from exc

        self.model = model
        self._effort = effort
        self._max_tokens = max_tokens
        try:
            # A bare client also resolves an `ant auth login` profile, so an
            # unset ANTHROPIC_API_KEY does not mean there are no credentials.
            self._client = (AsyncAnthropic(api_key=api_key, timeout=timeout)
                            if api_key else AsyncAnthropic(timeout=timeout))
        except Exception as exc:
            raise ProviderError(
                "provider 'anthropic' has no credentials: set ANTHROPIC_API_KEY, "
                f"or run `ant auth login`. ({exc})") from exc

    async def run(self, *, instructions: str, input: str, agent: str = "") -> str:
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=self._max_tokens,
            system=instructions,
            thinking={"type": "adaptive"},
            output_config={"effort": self._effort},
            messages=[{"role": "user", "content": input}],
        )
        if response.stop_reason == "refusal":
            detail = getattr(response.stop_details, "explanation", "") or ""
            raise ProviderError(f"{agent or 'agent'}: model declined. {detail}".strip())
        return "".join(b.text for b in response.content if b.type == "text")


class OpenAICompatibleRunner:
    """OpenAI, or anything speaking its API — Ollama included."""

    def __init__(self, *, provider: str, model: str, api_key: str = "",
                 base_url: str = "", timeout: float = 600.0) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise ProviderError(
                f"provider {provider!r} needs the SDK: uv sync --extra openai") from exc

        self.provider = provider
        self.model = model
        try:
            # Ollama ignores the key but the SDK requires one to be present.
            self._client = AsyncOpenAI(
                api_key=api_key or ("ollama" if provider == "ollama" else None),
                base_url=base_url or None, timeout=timeout)
        except Exception as exc:
            key = "OPENAI_API_KEY" if provider == "openai" else "LLM_API_KEY"
            raise ProviderError(
                f"provider {provider!r} has no credentials: set {key}. ({exc})") from exc

    async def run(self, *, instructions: str, input: str, agent: str = "") -> str:
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": instructions},
                      {"role": "user", "content": input}],
        )
        return response.choices[0].message.content or ""


# ---------------------------------------------------------------------- cli


class CliAgentRunner:
    """Drive an agentic coding CLI as a subprocess.

    Unlike an API provider this runs a full agent — it can read files and use
    tools — and returns its final answer. Slower and more capable; the same
    ``LLMRunner`` protocol either way, so no agent code changes.
    """

    def __init__(self, *, provider: str, binary: str, args: list[str],
                 system_flag: str | None, model: str = "",
                 timeout: float = 900.0, cwd: str | None = None) -> None:
        self.provider = provider
        self.model = model
        self._binary = binary
        self._args = args
        self._system_flag = system_flag
        self._timeout = timeout
        self._cwd = cwd

    def _command(self, instructions: str, prompt: str) -> list[str]:
        command = [self._binary, *self._args]
        if self.model:
            command += ["--model" if self.provider == "claude_code" else "-m", self.model]
        if self._system_flag and instructions:
            command += [self._system_flag, instructions]
            body = prompt
        else:
            # Codex has no system-prompt flag; fold the role into the prompt.
            body = f"{instructions}\n\n---\n\n{prompt}" if instructions else prompt
        return [*command, body]

    async def run(self, *, instructions: str, input: str, agent: str = "") -> str:
        if shutil.which(self._binary) is None:
            raise ProviderError(
                f"provider {self.provider!r} needs the {self._binary!r} CLI on PATH")

        process = await asyncio.create_subprocess_exec(
            *self._command(instructions, input),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(),
                                                    timeout=self._timeout)
        except asyncio.TimeoutError:
            process.kill()
            raise ProviderError(
                f"{self.provider} timed out after {self._timeout}s") from None

        if process.returncode != 0:
            raise ProviderError(
                f"{self.provider} exited {process.returncode}: "
                f"{stderr.decode(errors='replace').strip()[:400]}")
        return stdout.decode(errors="replace").strip()


def claude_code_runner(*, model: str = "", timeout: float = 900.0,
                       cwd: str | None = None) -> CliAgentRunner:
    return CliAgentRunner(provider="claude_code", binary="claude",
                          args=["-p"], system_flag="--append-system-prompt",
                          model=model, timeout=timeout, cwd=cwd)


def codex_runner(*, model: str = "", timeout: float = 900.0,
                 cwd: str | None = None) -> CliAgentRunner:
    return CliAgentRunner(provider="codex", binary="codex",
                          args=["exec"], system_flag=None,
                          model=model, timeout=timeout, cwd=cwd)


# ------------------------------------------------------------------ factory


def build_runner(provider: str, *, model: str = "", api_key: str = "",
                 base_url: str = "", effort: str = "medium",
                 timeout: float = 600.0, cwd: str | None = None):
    """Construct a runner for ``provider``. Raises ProviderError on a bad name."""
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise ProviderError(
            f"unknown LLM provider {provider!r}; available: {provider_names()}")

    resolved = model or spec.default_model

    if provider == "stub":
        return StubRunner()
    if provider == "anthropic":
        return AnthropicRunner(model=resolved, api_key=api_key, effort=effort,
                               timeout=timeout)
    if provider == "openai":
        return OpenAICompatibleRunner(provider="openai", model=resolved,
                                      api_key=api_key, base_url=base_url,
                                      timeout=timeout)
    if provider == "ollama":
        return OpenAICompatibleRunner(
            provider="ollama", model=resolved, api_key="ollama",
            base_url=base_url or "http://localhost:11434/v1", timeout=timeout)
    if provider == "claude_code":
        return claude_code_runner(model=model, timeout=timeout, cwd=cwd)
    if provider == "codex":
        return codex_runner(model=model, timeout=timeout, cwd=cwd)

    raise ProviderError(f"provider {provider!r} has no constructor")  # pragma: no cover


class LLMRegistry:
    """Resolves a runner per agent, caching one instance per (provider, model).

    A team may set a default provider and override it per agent in team.yaml —
    a cheap local model for routine work, a stronger one for the critic.
    """

    def __init__(self, *, provider: str = "stub", model: str = "",
                 api_key: str = "", base_url: str = "", effort: str = "medium",
                 timeout: float = 600.0, cwd: str | None = None) -> None:
        self.default_provider = provider
        self.default_model = model
        self._settings = {"api_key": api_key, "base_url": base_url,
                          "effort": effort, "timeout": timeout, "cwd": cwd}
        self._cache: dict[tuple[str, str], object] = {}

    def for_agent(self, provider: str | None = None, model: str | None = None):
        chosen = provider or self.default_provider
        chosen_model = model if model is not None else self.default_model
        key = (chosen, chosen_model)
        if key not in self._cache:
            self._cache[key] = build_runner(chosen, model=chosen_model,
                                            **self._settings)
        return self._cache[key]

    def describe(self) -> str:
        spec = PROVIDERS[self.default_provider]
        model = self.default_model or spec.default_model
        return f"{self.default_provider}" + (f" ({model})" if model else "")


def registry_from_settings(settings) -> LLMRegistry:
    """Build a registry from ``app.config.Settings``."""
    return LLMRegistry(
        provider=settings.llm_provider,
        model=settings.llm_model,
        api_key=settings.llm_api_key or os.environ.get(
            PROVIDERS.get(settings.llm_provider, PROVIDERS["stub"]).needs_key, ""),
        base_url=settings.llm_base_url,
        effort=getattr(settings, "llm_effort", "medium"),
        timeout=getattr(settings, "llm_timeout", 600.0))
