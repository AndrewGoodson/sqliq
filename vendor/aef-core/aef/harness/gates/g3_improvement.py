"""G3 — improvement, measured against a null hypothesis.

G2 established the candidate broke nothing. G3 asks whether it *helped*, and
refuses to answer from a comparison that cannot support the conclusion.

Four rejection rules:

1. **No control cohort ⇒ FAIL.** Beating the incumbent proves nothing:
   generate enough random variants and some score higher by chance on a
   fixed scenario set. Without a cohort there is no null hypothesis to
   reject, so the gate has no signal — escalate (Tier 2), never pass.
2. **Inside the cohort's spread ⇒ FAIL**, even if the incumbent is beaten.
   This is the null hypothesis holding.
3. **A previously-passing scenario now scoring below threshold ⇒ FAIL**,
   regardless of the aggregate. Zero tolerance, deterministic, and
   independent of any statistic — this is the rule that still binds when
   the sample is too small for the statistics to say anything.
4. **Cost blow-up ⇒ FAIL.** A candidate scoring marginally better while
   spending far more is not an improvement.
5. **Too many dead calls ⇒ FAIL, "could not judge".** Added by ADR 0185, and
   it is a refusal rather than a rejection: see below.

Confidence intervals are **reported, never gated on**. At n≈20 they overlap
routinely, and gating on means would launder noise into a decision. The
paired sign test added by ADR 0185 is reported on exactly the same terms.

## A dead call is not a wrong answer (ADR 0185)

ADR 0156 measured the live floor on Opus and found a third of its spread was
one transient provider failure: a call that RAISED and a call that returned
`""` both produce `score 0.0000, cost_tokens 0`, and nothing told them apart.
Under K1 (ADR 0181) every gate pass can now execute live, so every gate pass
meets this.

Scoring a dead call 0.0 asserts something that was never measured. The
candidate's prompt was not evaluated on that scenario at all — no answer was
produced, right or wrong — so a 0.0 there is not evidence of harm any more
than it is evidence of help. Reproduced before the change: a stub provider
that exits non-zero on its third call made G3 report *"1 previously-passing
scenario(s) now score below 0.5"* against a candidate that had answered every
scenario it was asked.

So a scenario whose call died — after ONE retry, in `run_corpus_isolated` —
is **excluded from the comparison**, and three things bound that:

- **The exclusion is symmetric.** `CohortVerdict.without` drops the scenario
  from the candidate, the incumbent AND every control. A one-sided drop would
  compute the candidate's mean over one scenario set and the p95 it must beat
  over another. This also *raises* the bar in the commonest case: a dead call
  in a control drags that control's mean down, which drags p95 down, which is
  the direction that lets a bad candidate through.
- **The refusal floor.** Past `MAX_DEAD_FRACTION` of the scenarios, G3 stops
  judging and says so. A candidate that makes the provider die is not thereby
  cleared; it is escalated, exactly as an absent cohort escalates it.
- **Every exclusion is named in the evidence**, with the retry count, so a
  human reads what was not measured rather than inferring it from token
  accounting the way ADR 0156 had to.

**Nothing about a replayed run changes.** `is_dead_call` returns `False`
unless the run was live, because under `cassette_miss="fail"` no call is
attempted and a `ModelProviderError` is `CassetteProvider` reporting a MISS —
a behavioural difference, and the strongest signal the replayed path has
(ADR 0123 caught the planted regression that way, at 0.0000).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from aef.harness.evaluation import CohortVerdict, ScoreSet
from aef.harness.gates.base import Gate, GateContext, GateOutcome, GateResult

DEFAULT_COHORT_PERCENTILE = 95.0
DEFAULT_MIN_COHORT_SIZE = 5
DEFAULT_MAX_COST_RATIO = 1.5
PASS_THRESHOLD = 0.5
# Above this share of the gated scenarios, G3 refuses to judge rather than
# judging on the survivors. On the six-scenario summary corpus ADR 0156
# measured, this permits exactly one dead call — the count that increment
# actually saw — and refuses at two.
MAX_DEAD_FRACTION = 0.25


@dataclass(frozen=True)
class PairedComparison:
    """Candidate vs incumbent, scenario by scenario. **Reported, never gated
    on** — see `paired_sign_test`."""

    down: tuple[str, ...]
    up: tuple[str, ...]
    same: tuple[str, ...]
    p_value: float

    @property
    def lines(self) -> tuple[str, ...]:
        line = (
            f"paired: {len(self.down)} down / {len(self.up)} up / "
            f"{len(self.same)} same (sign test p={self.p_value:.4g})"
        )
        if not self.down:
            return (line,)
        named = ", ".join(self.down[:5])
        if len(self.down) > 5:
            named += f", +{len(self.down) - 5} more"
        return (line, f"paired: candidate scored lower on {named}")


def paired_sign_test(candidate: ScoreSet, incumbent: ScoreSet) -> PairedComparison:
    """A two-sided exact sign test over the scenarios BOTH arms were scored on.

    **This decides nothing.** The p95 rule below is unchanged and is the only
    thing that produces a verdict; this line exists so a human — and the trust
    case — can read what the mean cannot.

    ADR 0156 is the reason. Its planted regression (the must-mention
    instruction deleted from the summary prompt) moved the mean of means by
    0.0278 against a floor spread of 0.1666 — invisible, and dimension 1
    correctly did not move. The same numbers, paired per scenario, are not
    invisible: `sum-17` fell in 3 of 3 repeats and `sum-18` in 3 of 3, while
    nothing fell the other way consistently. Pooled over all 18 (scenario,
    repeat) pairs: **7 down / 3 up / 8 same, p=0.3438**.

    Note what that p is. It does **not** reach significance, and it is
    reported here at four figures precisely so nobody claims it does. Six
    scenarios per pass is not enough for a sign test either — per repeat the
    same data gives p = 0.625, 1.0, 1.0. The direction is legible; the
    significance is not, and the fix for that is ADR 0156's consequence 2
    (more scenarios), not a smaller threshold.

    Ties count as ties rather than being split, which is the conservative
    convention: a scenario both arms scored identically is evidence of
    nothing, and assigning it a direction would manufacture a difference.
    """
    shared = sorted(set(candidate.per_scenario) & set(incumbent.per_scenario))
    down = tuple(s for s in shared if candidate.per_scenario[s] < incumbent.per_scenario[s])
    up = tuple(s for s in shared if candidate.per_scenario[s] > incumbent.per_scenario[s])
    same = tuple(s for s in shared if s not in set(down) | set(up))
    n = len(down) + len(up)
    if n == 0:
        # No scenario moved in either direction. p=1 is the honest reading:
        # under the null of "each move is a coin flip", zero moves is the
        # least surprising outcome there is.
        return PairedComparison(down=down, up=up, same=same, p_value=1.0)
    k = min(len(down), len(up))
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2**n)
    return PairedComparison(down=down, up=up, same=same, p_value=min(1.0, 2.0 * tail))


@dataclass(frozen=True)
class G3Improvement(Gate):
    id: str = "G3"
    verdict: CohortVerdict | None = None
    min_cohort_size: int = DEFAULT_MIN_COHORT_SIZE
    max_cost_ratio: float = DEFAULT_MAX_COST_RATIO
    max_dead_fraction: float = MAX_DEAD_FRACTION

    def run(self, ctx: GateContext) -> GateResult:
        verdict = self.verdict
        if verdict is None or not verdict.cohort:
            return GateResult(
                gate=self.id,
                outcome=GateOutcome.FAIL,
                reason=(
                    "no null-hypothesis control cohort — beating the incumbent proves "
                    "nothing without one, so this gate has no signal and the candidate "
                    "escalates rather than passing"
                ),
            )

        if len(verdict.cohort) < self.min_cohort_size:
            return GateResult(
                gate=self.id,
                outcome=GateOutcome.FAIL,
                reason=(
                    f"control cohort of {len(verdict.cohort)} is below the minimum of "
                    f"{self.min_cohort_size}; a percentile over too few samples is not a "
                    f"threshold, it is a coin flip"
                ),
            )

        refusal = self._check_dead_calls(verdict)
        if refusal is not None:
            return refusal
        # Everything below judges the scenarios that were actually ANSWERED.
        # With no dead calls — every replayed run, and most live ones —
        # `without` returns the verdict unchanged and this is a no-op.
        judged = verdict.without(verdict.dead_scenarios)

        regressed = _regressed_scenarios(judged.incumbent, judged.candidate)
        if regressed:
            # Deterministic and independent of any statistic — the rule that
            # still binds when the sample is too small to say anything.
            return GateResult(
                gate=self.id,
                outcome=GateOutcome.FAIL,
                reason=(
                    f"{len(regressed)} previously-passing scenario(s) now score below "
                    f"{PASS_THRESHOLD} (zero tolerance, regardless of the aggregate)"
                ),
                evidence=tuple(sorted(regressed)) + self._evidence(verdict, judged),
            )

        cost_failure = self._check_cost(verdict, judged)
        if cost_failure is not None:
            return cost_failure

        if not judged.beats_cohort:
            return GateResult(
                gate=self.id,
                outcome=GateOutcome.FAIL,
                reason=(
                    f"candidate does not beat the p{judged.percentile:g} of the random "
                    f"control cohort — this is the null hypothesis, not an improvement"
                ),
                evidence=self._evidence(verdict, judged),
            )

        return GateResult(
            gate=self.id,
            outcome=GateOutcome.PASS,
            reason=(
                f"candidate mean {judged.candidate.mean:.4g} beats the control cohort's "
                f"p{judged.percentile:g} of {judged.threshold:.4g}"
            ),
            evidence=self._evidence(verdict, judged),
        )

    def _check_dead_calls(self, verdict: CohortVerdict) -> GateResult | None:
        """Refuse to judge when too much of the corpus was never answered.

        A gate that judged on the survivors of a mostly-dead run would be
        reporting a verdict about three scenarios and calling it a verdict
        about twelve. And it would be the loophole the exclusion rule needs
        closing: a candidate that kills the provider on the scenarios it does
        badly on must not be able to shrink the corpus until it wins. Past
        the ceiling the answer is neither PASS nor "regression" but **could
        not judge**, which escalates the candidate exactly as an absent
        cohort does.
        """
        dead = verdict.dead_scenarios
        if not dead:
            return None
        total = len(verdict.candidate.per_scenario) or len(verdict.incumbent.per_scenario)
        if total == 0:
            return None
        fraction = len(dead) / total
        if fraction <= self.max_dead_fraction:
            return None
        return GateResult(
            gate=self.id,
            outcome=GateOutcome.FAIL,
            reason=(
                f"could not judge: {len(dead)} dead call(s) of {total} scenario(s) "
                f"({fraction:.0%}), over the {self.max_dead_fraction:.0%} ceiling. The model "
                f"was not answering, so neither a pass nor a rejection would be about this "
                f"candidate"
            ),
            evidence=_dead_call_lines(verdict) + verdict.report,
        )

    def _evidence(self, verdict: CohortVerdict, judged: CohortVerdict) -> tuple[str, ...]:
        """The judged comparison, what was excluded from it, and the paired
        line. The paired line is last and is read by nothing in this file."""
        return (
            *judged.report,
            *_dead_call_lines(verdict),
            *paired_sign_test(judged.candidate, judged.incumbent).lines,
        )

    def _check_cost(self, verdict: CohortVerdict, judged: CohortVerdict) -> GateResult | None:
        """Cost is SELF-REPORTED, and this rule says so.

        `cost_tokens` is `sum(p.token_cost for p in state.provenance)`, and
        `Provenance` is written by agent-authored nodes in their own
        `StateDelta`. Nothing else produces it. So a candidate that deletes
        `provenance=[...]` reports zero tokens and this rule cannot bind —
        the same shape as ADR 0080's `recovered` marker, on the cost axis
        (ADR 0092).

        Deleting the reporting is not a cheaper agent, it is an agent that
        stopped saying. An incumbent that reported and a candidate that does
        not is therefore treated as a cost violation, not a free pass. The
        rule remains unenforceable against an agent that never reported at
        all, and that limit is stated in the gate's own evidence rather than
        left for someone to discover.
        """
        incumbent_cost = verdict.incumbent.cost_tokens
        if incumbent_cost <= 0:
            # Nothing to compare against; say so rather than dividing by zero
            # or silently treating "unmeasured" as "free".
            return None

        if verdict.candidate.cost_tokens <= 0:
            return GateResult(
                gate=self.id,
                outcome=GateOutcome.FAIL,
                reason=(
                    f"the incumbent reported {incumbent_cost} tokens and the candidate "
                    f"reports none. Cost is self-reported from Provenance the agent emits, "
                    f"so dropping it reads as free rather than cheap — a candidate that "
                    f"stopped reporting has not shown it costs less"
                ),
                evidence=(
                    f"incumbent {incumbent_cost} tokens, candidate 0",
                    *self._evidence(verdict, judged),
                ),
            )

        ratio = verdict.candidate.cost_tokens / incumbent_cost
        if ratio <= self.max_cost_ratio:
            return None
        return GateResult(
            gate=self.id,
            outcome=GateOutcome.FAIL,
            reason=(
                f"cost blow-up: candidate spends {ratio:.2f}x the incumbent's tokens, over "
                f"the {self.max_cost_ratio:.2f}x budget — a marginal score gain bought with "
                f"a large cost increase is not an improvement"
            ),
            evidence=(
                f"incumbent {incumbent_cost} tokens, candidate {verdict.candidate.cost_tokens}",
                *self._evidence(verdict, judged),
            ),
        )


def _dead_call_lines(verdict: CohortVerdict) -> tuple[str, ...]:
    """What was excluded and why, named rather than inferred.

    ADR 0156 had to establish that repeat 3's `sum-13: 0.00` was a failed
    call rather than a wrong answer by comparing split-level token totals
    across repeats. Nobody should have to do that again.
    """
    dead = verdict.dead_scenarios
    rescued = sorted(verdict.retried_scenarios - dead)
    total = len(verdict.candidate.per_scenario) or len(verdict.incumbent.per_scenario)
    lines: list[str] = []
    if dead:
        lines.append(
            f"excluded {len(dead)} of {total} scenario(s) from BOTH arms: the model call "
            f"died (retried once, died again) so the candidate was never evaluated on "
            f"{', '.join(sorted(dead))}"
        )
    if rescued:
        # Reported even when nothing was excluded: the retry spent a real
        # call against somebody's quota, and a silent extra call is how a
        # bounded retry becomes an unbounded one nobody notices.
        lines.append(f"retried once and answered on the second attempt: {', '.join(rescued)}")
    return tuple(lines)


def _regressed_scenarios(incumbent: ScoreSet, candidate: ScoreSet) -> set[str]:
    """Scenario ids the incumbent passed and the candidate does not.

    A scenario the incumbent passed that the candidate did not score at all
    counts as regressed: a missing score is not a passing one.
    """
    return {
        sid for sid in incumbent.passing if candidate.per_scenario.get(sid, 0.0) < PASS_THRESHOLD
    }
