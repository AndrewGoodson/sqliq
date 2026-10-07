"""`aef eval` — score a checkpointed run with `RuleBasedEvaluator`."""

from __future__ import annotations

from pathlib import Path

from aef.config import build_domain_gates, load_agent_config
from aef.kernel import FileDurabilityBackend
from aef.services.eval.base import EvaluationRecord
from aef.services.eval.rule_based import RuleBasedEvaluator


def build_evaluator(config_path: str | Path | None) -> RuleBasedEvaluator:
    """The evaluator an `aef.yaml` asks for, including its `evaluator.suites`.

    One builder for every scoring site, for the reason ADR 0091 records about
    service lists: two constructions of the same thing drift, and the drift is
    invisible until something scores differently in two places. Without a
    config there are no suites, and the evaluator is the same one Phase 0/1
    always used.
    """
    if config_path is None:
        return RuleBasedEvaluator()
    config = load_agent_config(config_path)
    return RuleBasedEvaluator(domain_gates=build_domain_gates(config.evaluator))


def eval_run(
    checkpoints_dir: Path, run_id: str, *, config_path: str | Path | None = None
) -> EvaluationRecord:
    backend = FileDurabilityBackend(checkpoints_dir)
    state = backend.load_latest(run_id)
    if state is None:
        raise ValueError(f"no checkpoints found for run_id={run_id!r} under {checkpoints_dir}")
    return build_evaluator(config_path).evaluate(state)
