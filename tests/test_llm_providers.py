"""Provider selection. Model choice must stay decoupled from the kernel."""
import asyncio

import pytest

from app.kernel.team_spec import load_team_spec
from app.runtime.llm import (PROVIDERS, LLMRegistry, ProviderError, StubRunner,
                             build_runner, provider_names)


def test_every_documented_provider_is_constructible_or_reports_why():
    for name in provider_names():
        try:
            build_runner(name)
        except ProviderError as exc:
            # An unavailable provider must say how to enable it.
            assert any(word in str(exc)
                       for word in ("uv sync", "PATH", "credentials")), exc


def test_unknown_provider_lists_the_valid_ones():
    with pytest.raises(ProviderError, match="unknown LLM provider"):
        build_runner("gpt9000")


def test_stub_needs_nothing():
    assert isinstance(build_runner("stub"), StubRunner)


async def test_stub_is_deterministic():
    runner = build_runner("stub")
    first = await runner.run(instructions="i", input="hello", agent="critic")
    second = await runner.run(instructions="i", input="hello", agent="critic")
    assert first == second


def test_cli_providers_are_subprocess_shaped():
    for name in ("claude_code", "codex"):
        runner = build_runner(name)
        assert PROVIDERS[name].kind == "cli"
        assert runner._binary in ("claude", "codex")


def test_claude_code_passes_the_role_as_a_system_prompt():
    command = build_runner("claude_code")._command("be terse", "hello")
    assert command[:2] == ["claude", "-p"]
    assert "--append-system-prompt" in command
    assert command[-1] == "hello", "the prompt must stay the final positional arg"


def test_codex_folds_the_role_into_the_prompt():
    """Codex has no system-prompt flag, so the role must not be silently lost."""
    command = build_runner("codex")._command("be terse", "hello")
    assert command[:2] == ["codex", "exec"]
    assert "be terse" in command[-1] and "hello" in command[-1]


def test_model_flag_differs_per_cli():
    assert "--model" in build_runner("claude_code", model="claude-opus-5")._command("", "x")
    assert "-m" in build_runner("codex", model="o5")._command("", "x")


def test_ollama_defaults_to_the_local_endpoint():
    spec = PROVIDERS["ollama"]
    assert spec.default_model and spec.kind == "api"


def test_anthropic_default_model_is_current():
    assert PROVIDERS["anthropic"].default_model == "claude-opus-5"


# ------------------------------------------------------------------ registry

def test_registry_caches_one_runner_per_provider_and_model():
    registry = LLMRegistry(provider="stub")
    assert registry.for_agent() is registry.for_agent()


def test_registry_falls_back_to_the_team_default():
    registry = LLMRegistry(provider="stub")
    assert isinstance(registry.for_agent(None, None), StubRunner)


def test_registry_honours_a_per_agent_override():
    registry = LLMRegistry(provider="stub")
    default = registry.for_agent()
    overridden = registry.for_agent("claude_code", "")
    assert overridden is not default
    assert overridden.provider == "claude_code"


def test_registry_describe_is_human_readable():
    assert "stub" in LLMRegistry(provider="stub").describe()


# ----------------------------------------------------------------- team.yaml

def test_team_yaml_supports_a_per_agent_model_override(tmp_path):
    path = tmp_path / "team.yaml"
    path.write_text("""
team: {id: mixed}
agents:
  worker:
    entrypoint: app.agents.research
  critic:
    entrypoint: app.agents.critic
    provider: anthropic
    model: claude-opus-5
""")
    spec = load_team_spec(path)
    assert spec.llm_for("critic") == ("anthropic", "claude-opus-5")
    assert spec.llm_for("worker") == (None, None)
    assert spec.llm_for("nobody") == (None, None)


def test_real_teams_inherit_the_deployment_default():
    spec = load_team_spec("teams/investment")
    assert all(spec.llm_for(a) == (None, None) for a in spec.agent_names)


# ------------------------------------------------------------------ kernel

def test_the_kernel_never_imports_a_provider():
    """Model choice must not be coupled to the coordination kernel."""
    import ast
    import pathlib

    for module in pathlib.Path("app/kernel").glob("*.py"):
        tree = ast.parse(module.read_text())
        names = {n.module.split(".")[0] for n in ast.walk(tree)
                 if isinstance(n, ast.ImportFrom) and n.module}
        names |= {a.name.split(".")[0] for n in ast.walk(tree)
                  if isinstance(n, ast.Import) for a in n.names}
        assert not names & {"anthropic", "openai"}, f"{module} imports a model SDK"
