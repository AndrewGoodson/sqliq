"""Scoring a candidate over the corpus, and the statistics G3 gates on.

Two things worth stating before the code, because both are places where a
plausible implementation would quietly mislead.

**Aggregate means have no power at this sample size.** With n≈20 scenarios,
"candidate mean 0.81 vs incumbent 0.78" is noise. `ScoreSet` therefore
reports a confidence interval alongside the mean and G3 *gates* on a
deterministic per-scenario rule plus a percentile comparison against a
control cohort — never on the means alone.

**Beating the incumbent is not evidence of improvement.** Generate enough
random variants of anything and some will score higher on a fixed set of
scenarios by chance. That is the null hypothesis, and the only way to
reject it is to score a cohort of *random* mutations the same way and
require the candidate to beat them, not merely to beat the incumbent
(GEPA/AlphaEvolve both make this point; the Phase-4 gate criteria list it
as criterion 2). A candidate that beats the incumbent but sits inside the
random cohort's spread has demonstrated nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from dataclasses import replace as _replace

from aef.harness.checks import evaluate_checks
from aef.harness.corpus import Scenario
from aef.services.eval.base import EvaluationRecord
from aef.services.eval.rule_based import RuleBasedEvaluator
from aef.state import AEFState


@dataclass(frozen=True)
class ScoreSet:
    """Per-scenario scores for one variant, plus the honest statistics."""

    label: str
    per_scenario: dict[str, float] = field(default_factory=dict)
    cost_tokens: int = 0

    @property
    def n(self) -> int:
        return len(self.per_scenario)

    @property
    def mean(self) -> float:
        if not self.per_scenario:
            raise ValueError(f"score set {self.label!r} is empty; there is no mean to report")
        return sum(self.per_scenario.values()) / self.n

    @property
    def stdev(self) -> float:
        if self.n < 2:
            return 0.0
        mean = self.mean
        return math.sqrt(sum((v - mean) ** 2 for v in self.per_scenario.values()) / (self.n - 1))

    @property
    def confidence_interval_95(self) -> tuple[float, float]:
        """Normal-approximation CI on the mean. Reported, never gated on —
        at n≈20 it is wide enough that overlapping intervals are the norm,
        which is precisely the fact a mean-comparison would hide."""
        if self.n < 2:
            return (self.mean, self.mean)
        margin = 1.96 * self.stdev / math.sqrt(self.n)
        return (self.mean - margin, self.mean + margin)

    @property
    def passing(self) -> frozenset[str]:
        return frozenset(sid for sid, score in self.per_scenario.items() if score >= 0.5)

    def percentile(self, p: float) -> float:
        """Linear-interpolated percentile of the per-scenario scores."""
        if not self.per_scenario:
            raise ValueError(f"score set {self.label!r} is empty")
        if not 0.0 <= p <= 100.0:
            raise ValueError(f"percentile must be within [0, 100], got {p}")
        ordered = sorted(self.per_scenario.values())
        if len(ordered) == 1:
            return ordered[0]
        position = (p / 100.0) * (len(ordered) - 1)
        low = math.floor(position)
        high = math.ceil(position)
        if low == high:
            return ordered[low]
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def score_scenario(
    scenario: Scenario, final_state: AEFState, *, elapsed_ms: float
) -> EvaluationRecord:
    """THE task metric's record for one re-execution (ADR 0113). One function
    for every scoring path — in-process runner and isolated suite — because
    two constructions of a score drift and the drift is a phantom regression
    (ADR 0091).

    `task_completion` is the rule-based evaluator's, refined by the owner's
    checks: with checks declared it becomes the fraction that hold, still
    zeroed by an error (a right answer followed by a raise did not complete)
    and by a blown wall-clock budget. Without checks it is unchanged — a
    scenario that declares nothing about its answer makes no claim.
    """
    record = RuleBasedEvaluator().evaluate(final_state)
    metadata = dict(record.metadata)
    metadata["elapsed_ms"] = elapsed_ms
    task_completion = record.task_completion
    if scenario.checks:
        report = evaluate_checks(scenario.checks, final_state)
        metadata["checks"] = {
            "passed": report.passed,
            "total": report.total,
            "failures": list(report.failures),
        }
        if not final_state.errors:
            task_completion = report.fraction
    if scenario.budget_ms is not None and elapsed_ms > scenario.budget_ms:
        metadata["budget_exceeded"] = True
        task_completion = 0.0
    return _replace(record, task_completion=task_completion, metadata=metadata)


def score_of(record: EvaluationRecord) -> float:
    """The scalar G3 compares. `task_completion` gated by the domain gates,
    which is `EvaluationRecord.passed`'s logic expressed as a score rather
    than a boolean — a failed domain gate zeroes the score outright rather
    than being averaged away (ADR 0038)."""
    if not all(record.domain_gates.values()):
        return 0.0
    return record.task_completion


def _without(scores: ScoreSet, drop: frozenset[str]) -> ScoreSet:
    return _replace(
        scores,
        per_scenario={sid: v for sid, v in scores.per_scenario.items() if sid not in drop},
    )


def build_score_set(label: str, records: dict[str, EvaluationRecord]) -> ScoreSet:
    return ScoreSet(
        label=label,
        per_scenario={sid: score_of(r) for sid, r in records.items()},
        cost_tokens=sum(r.cost_tokens for r in records.values()),
    )


@dataclass(frozen=True)
class CohortVerdict:
    """Did the candidate beat the random-mutation cohort, or merely the
    incumbent?"""

    candidate: ScoreSet
    incumbent: ScoreSet
    cohort: tuple[ScoreSet, ...]
    percentile: float = 95.0
    # Scenarios whose MODEL CALL DIED — in any arm — rather than being
    # answered (ADR 0185). Empty on every replayed run, because a replay
    # attempts no call that can die; populated only under
    # `--cassette-miss live`. G3 reads this; nothing else does.
    dead_scenarios: frozenset[str] = frozenset()
    # Of those, the ones the runner retried once and that survived the
    # retry. Reported so the bounded retry is visible rather than silent.
    retried_scenarios: frozenset[str] = frozenset()

    @property
    def cohort_means(self) -> tuple[float, ...]:
        return tuple(s.mean for s in self.cohort)

    def without(self, scenario_ids: frozenset[str] | set[str]) -> CohortVerdict:
        """The same verdict with `scenario_ids` dropped from EVERY arm.

        Symmetry is the whole point. Dropping a scenario from the candidate
        alone would compute its mean over one scenario set and the threshold
        it must beat over another, which is not a comparison. Dropping it
        everywhere leaves candidate, incumbent and every control judged on
        exactly the scenarios that were actually answered.

        `cost_tokens` is a per-arm total and is carried through unchanged: a
        scenario whose call died contributed zero tokens to it, so there is
        nothing to subtract.
        """
        drop = frozenset(scenario_ids)
        if not drop:
            return self
        return _replace(
            self,
            candidate=_without(self.candidate, drop),
            incumbent=_without(self.incumbent, drop),
            cohort=tuple(_without(s, drop) for s in self.cohort),
        )

    @property
    def threshold(self) -> float:
        """The score the candidate must exceed: the `percentile`-th
        percentile of the control cohort's means."""
        means = ScoreSet(
            label="cohort-means",
            per_scenario={s.label: s.mean for s in self.cohort},
        )
        return means.percentile(self.percentile)

    @property
    def beats_cohort(self) -> bool:
        return bool(self.cohort) and self.candidate.mean > self.threshold

    @property
    def beats_incumbent(self) -> bool:
        return self.candidate.mean > self.incumbent.mean

    @property
    def report(self) -> tuple[str, ...]:
        low, high = self.candidate.confidence_interval_95
        lines = [
            f"candidate mean {self.candidate.mean:.4g} (95% CI {low:.4g}..{high:.4g}, "
            f"n={self.candidate.n})",
            f"incumbent mean {self.incumbent.mean:.4g}",
        ]
        if self.cohort:
            lines.append(
                f"control cohort of {len(self.cohort)} random mutation(s): "
                f"p{self.percentile:g} of means = {self.threshold:.4g}"
            )
            if self.beats_incumbent and not self.beats_cohort:
                lines.append(
                    "candidate beats the incumbent but sits inside the random cohort's "
                    "spread — that is the null hypothesis, not an improvement"
                )
        return tuple(lines)
