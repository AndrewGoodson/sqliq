"""`RuleBasedEvaluator` — a real, dependency-free `Evaluator` for Phase 0/1.
No LLM-as-judge call: task completion, tool-call accuracy, and trajectory
quality are computed straight from `AEFState`. LLM-judge evaluators (report
§9 — DeepEval/Ragas-style) are a Phase 3 addition once the eval infra this
harness establishes is trustworthy enough to build on.

`domain_gates` is exactly the pluggable per-agent surface report §16
describes (e.g. a Financial Agent's Sharpe/PF/MaxDD gates, an Azure
Security Agent's blast-radius gate) — a name-keyed dict of
`AEFState -> bool` predicates evaluated fresh on every call.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from aef.services.eval.base import EvaluationRecord, Evaluator
from aef.state import AEFState

GateFn = Callable[[AEFState], bool]


@dataclass(frozen=True)
class RuleBasedEvaluator(Evaluator):
    domain_gates: dict[str, GateFn] = field(default_factory=dict)

    def evaluate(self, state: AEFState) -> EvaluationRecord:
        has_errors = len(state.errors) > 0
        plan_done = state.plan is not None and state.plan.status == "done"
        if plan_done and not has_errors:
            task_completion = 1.0
        elif has_errors:
            task_completion = 0.0
        else:
            task_completion = 0.5

        return EvaluationRecord(
            run_id=state.run_id,
            task_completion=task_completion,
            tool_call_accuracy=self._tool_call_accuracy(state),
            trajectory_quality=self._trajectory_quality(state),
            cost_tokens=sum(p.token_cost for p in state.provenance),
            # cost_dollars stays None: no pricing table exists anywhere in
            # Phase 0/1 to convert cost_tokens into a dollar figure, and a
            # fabricated number would be worse than an honest "unmeasured."
            cost_dollars=None,
            latency_ms=self._latency_ms(state),
            domain_gates={name: gate(state) for name, gate in self.domain_gates.items()},
        )

    @staticmethod
    def _latency_ms(state: AEFState) -> float | None:
        """Wall-clock span between the first and last recorded node
        execution, from Provenance.ts — the only timing data AEFState
        carries. Needs at least two timestamps to have a span at all."""
        if len(state.provenance) < 2:
            return None
        timestamps = [p.ts for p in state.provenance]
        return (max(timestamps) - min(timestamps)).total_seconds() * 1000

    @staticmethod
    def _tool_call_accuracy(state: AEFState) -> float | None:
        if not state.tool_results:
            return None
        successes = sum(1 for result in state.tool_results if not result.get("error"))
        return successes / len(state.tool_results)

    @staticmethod
    def _trajectory_quality(state: AEFState) -> float | None:
        if state.plan is None:
            return None
        if state.plan.status == "done":
            return 1.0
        if state.plan.status == "failed":
            return 0.0
        return 0.5
