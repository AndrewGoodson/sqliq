"""`Evaluator` interface and `EvaluationRecord` — the atomic unit consumed by
the self-improvement loop (report §8/§10, blueprint Part 9.2). Every graph
execution is scored; nothing about *how* it's scored is hardcoded here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from aef.state import AEFState


@dataclass(frozen=True)
class EvaluationRecord:
    run_id: str
    task_completion: float  # 0..1
    tool_call_accuracy: float | None = None  # 0..1, None if no tools were called
    trajectory_quality: float | None = None  # 0..1, did the plan DAG execute sanely
    cost_tokens: int = 0
    # cost_dollars/latency_ms are None when an Evaluator didn't (or
    # couldn't) compute them, NOT 0.0 — an evaluator that can't measure
    # dollar cost (no pricing table configured) or latency (fewer than two
    # provenance timestamps to diff) must say so with None, not report a
    # silently-wrong zero indistinguishable from "genuinely free/instant."
    cost_dollars: float | None = None
    latency_ms: float | None = None
    domain_gates: dict[str, bool] = field(default_factory=dict)  # e.g. {"sharpe_gate": True}
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        # A run passes only if the task actually completed AND every domain
        # gate holds. Domain gates are ADDITIONAL hard constraints on top of
        # task completion, not a replacement for it — the previous
        # `all(gates) if gates else task_completion >= 0.5` silently dropped
        # task_completion (and the errors driving it to 0.0) the moment any
        # gate was configured, so an errored run could be marked passed just
        # because a Sharpe gate happened to hold (docs/adr/0038).
        return self.task_completion >= 0.5 and all(self.domain_gates.values())


class Evaluator(ABC):
    @abstractmethod
    def evaluate(self, state: AEFState) -> EvaluationRecord:
        """Score a completed (or in-flight) run's state. Pure function of
        the state it's given — an Evaluator must not mutate `state`."""
        raise NotImplementedError
