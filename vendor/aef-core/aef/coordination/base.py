"""Phase 5 interface stubs: multi-agent coordination (report §15, blueprint
Part 12). AEF's default is single-agent-with-subagent-readers (report Key
Finding #6) — full coordination is an opt-in escalation, not the baseline.
This module exists so that escalation is later an additive change, not a
rewrite; nothing here is wired into `GraphExecutor`. A coordinated run is,
mechanically, N separate `Graph` executions plus a supervisor policy over
how their `AEFState`/messages are shared.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum


class AgentRole(StrEnum):
    SUPERVISOR = "supervisor"
    PLANNER = "planner"
    RESEARCH_WORKER = "research_worker"
    CRITIC = "critic"
    JUDGE = "judge"
    TOOL_AGENT = "tool_agent"
    MEMORY_AGENT = "memory_agent"


@dataclass(frozen=True)
class HandoffRequest:
    from_agent_id: str
    to_agent_id: str
    objective: str
    # blueprint §12.2: every handoff must apply an explicit input filter —
    # passing the full transcript by default is the OpenAI Agents SDK
    # failure mode AEF avoids.
    input_filter_applied: bool


class Coordinator(ABC):
    """Deterministic hierarchical handoff is AEF's default pattern for every
    regulated domain in scope (blueprint §12.2); emergent routing is an
    explicit, logged, opt-in exception, never the default."""

    @abstractmethod
    def handoff(self, request: HandoffRequest) -> str:
        """Returns the target agent's run_id."""
        raise NotImplementedError("Coordinator is a Phase 5 interface; no backend is wired yet")
