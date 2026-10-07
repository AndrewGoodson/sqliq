"""`Optimizer` — Phase 3 interface stub for offline prompt/program
optimization (report §8 medium cadence / blueprint Part 4.1 L2: GEPA, DSPy).

Consumes `EvaluationRecord`s (aef.services.eval.base) across a batch of runs
and proposes candidate prompt/program edits. Nothing here executes until
Phase 3, and even then every candidate must clear the evaluation gates in
`aef/evolution/` before promotion (constraint #7).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from aef.services.eval.base import EvaluationRecord


@dataclass(frozen=True)
class OptimizationCandidate:
    target_node_id: str
    description: str
    payload: dict[str, Any]  # e.g. a revised prompt/few-shot set
    predicted_improvement: float | None = None


class Optimizer(ABC):
    @abstractmethod
    def propose(self, records: list[EvaluationRecord]) -> list[OptimizationCandidate]:
        raise NotImplementedError("Optimizer is a Phase 3 interface; no backend is wired yet")
