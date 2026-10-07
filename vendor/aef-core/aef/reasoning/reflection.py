"""Critic/Judge separation (report §9, blueprint Part 4.1 layer L1). Critic
gives verbal feedback (Reflexion-style, grounded in external signals — tool
errors, eval failures); Judge gives scores against an explicit rubric.
Output from both is written to failure/success memory
(`aef.services.memory`) so lessons persist across runs, not just within one.

These are the interfaces. The real rule-based implementations live in
`rule_based_reflection.py`, and `nodes.make_reflect_node` is the wiring that
runs them through `Services` and persists the result (ADR 0046). An
LLM-backed Critic/Judge remains a legitimate later addition; it changes no
interface here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from aef.state import AEFState


@dataclass(frozen=True)
class Critique:
    verbal_feedback: str
    grounded_in: tuple[str, ...] = ()  # e.g. tool-error ids, eval-record ids


class Critic(ABC):
    @abstractmethod
    def critique(self, state: AEFState) -> Critique:
        raise NotImplementedError("Critic is a Phase 3 interface; no backend is wired yet")


@dataclass(frozen=True)
class Judgment:
    score: float
    rubric: dict[str, float] = field(default_factory=dict)
    rationale: str = ""


class Judge(ABC):
    @abstractmethod
    def judge(self, state: AEFState) -> Judgment:
        raise NotImplementedError("Judge is a Phase 3 interface; no backend is wired yet")
