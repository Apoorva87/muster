"""Importing this package registers every V1 agent exactly once."""

from app.agents import critic, director, finance, monitor, research  # noqa: F401
from app.agents.base import (AgentContext, LLMRunner, StubLLMRunner, agent,
                             dispatch, get_agent, registered_agents)

__all__ = ["AgentContext", "LLMRunner", "StubLLMRunner", "agent", "dispatch",
           "get_agent", "registered_agents"]
