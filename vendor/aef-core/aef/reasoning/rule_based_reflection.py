"""`RuleBasedCritic` / `RuleBasedJudge` — the first real implementations of
the `Critic`/`Judge` interfaces in `reflection.py`, which until now were
pure `NotImplementedError` stubs.

Built to the same standard as `RuleBasedEvaluator`
(`aef/services/eval/rule_based.py`): no LLM call, no vendor SDK, no clock
read — everything is computed from signals already recorded on `AEFState`.
That is what `Critique.grounded_in` and `reflection.py`'s own docstring
call for ("grounded in external signals — tool errors, eval failures"), and
it is what makes both classes testable against hand-built adversarial state
rather than only against a live model.

An LLM-backed Critic/Judge is a legitimate later addition once this
skeleton exists and has been exercised against a real graph run. Starting
there would ship a plausible-looking implementation nothing has run.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from aef.reasoning.reflection import Critic, Critique, Judge, Judgment
from aef.state import AEFState

# Reflections are appended to `AEFState.reflections` *and* written to memory,
# and both are read back into a context window later. An unbounded error
# string would silently consume the context budget several steps downstream,
# so every quoted signal is excerpted.
MAX_EXCERPT_CHARS = 160
MAX_QUOTED_SIGNALS = 5


def failure_signals(state: AEFState) -> tuple[str, ...]:
    """Citations for every recorded failure signal, as `field[index]` refs
    that resolve against `state`.

    This is the **single** "what counts as a failure" convention for the
    reflection slice. It deliberately reuses the `result.get("error")`
    truthiness rule that `RuleBasedEvaluator._tool_call_accuracy` already
    established (`aef/services/eval/rule_based.py`) — two competing answers
    to "did this tool call fail?" across one codebase would be a bug in the
    making, so `RuleBasedCritic` and `reflect_node` both call this rather
    than each deciding for themselves.

    Presence in `state.errors` is itself a signal: a node appended it to
    report a failure, whatever shape the entry happens to have.
    """
    signals = [f"errors[{i}]" for i in range(len(state.errors))]
    signals += [
        f"tool_results[{i}]" for i, result in enumerate(state.tool_results) if result.get("error")
    ]
    return tuple(signals)


def _excerpt(value: object) -> str:
    text = str(value)
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    return text[: MAX_EXCERPT_CHARS - 1] + "…"


def _describe(state: AEFState, ref: str) -> str:
    field_name, _, rest = ref.partition("[")
    index = int(rest.rstrip("]"))
    entry: dict[str, object] = getattr(state, field_name)[index]
    # `errors` / `tool_results` are `list[dict[str, Any]]` — nothing
    # constrains the shape, so a missing "error" key must not raise. Fall
    # back to the whole entry, which is still the honest evidence.
    return f"{ref}: {_excerpt(entry.get('error') or entry)}"


@dataclass(frozen=True)
class RuleBasedCritic(Critic):
    """Templated, not generated, verbal feedback. Every claim it makes is
    backed by a citation in `grounded_in` that resolves to a real entry on
    the state it was given."""

    def critique(self, state: AEFState) -> Critique:
        signals = failure_signals(state)
        tool_total = len(state.tool_results)

        if not signals:
            return Critique(
                verbal_feedback=(
                    f"no failure signals: 0 error(s) recorded, "
                    f"{tool_total} tool call(s), none failed"
                ),
                grounded_in=(),
            )

        error_count = sum(1 for ref in signals if ref.startswith("errors["))
        tool_failures = len(signals) - error_count
        quoted = [_describe(state, ref) for ref in signals[:MAX_QUOTED_SIGNALS]]
        if len(signals) > MAX_QUOTED_SIGNALS:
            quoted.append(f"(+{len(signals) - MAX_QUOTED_SIGNALS} more)")

        return Critique(
            verbal_feedback=(
                f"{error_count} error(s) recorded; "
                f"{tool_failures}/{tool_total} tool call(s) failed. " + "; ".join(quoted)
            ),
            grounded_in=signals,
        )


@dataclass(frozen=True)
class RuleBasedJudge(Judge):
    """Weighted score over `state.scores`, against a caller-supplied rubric
    of non-negative weights. Deterministic and fully explainable: the
    `Judgment` reports the weights applied and names what drove the score."""

    rubric: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Validate at construction, not at judge() time. `Judgment.score` is
        # `float`, not `float | None`, so a misconfigured rubric has no
        # honest return value at scoring time — 0.0 would be
        # indistinguishable from a genuine zero.
        if not self.rubric:
            raise ValueError(
                "RuleBasedJudge.rubric must not be empty — a judge with no rubric has "
                "nothing to score, and returning 0.0 would be indistinguishable from a "
                "genuine zero"
            )
        non_finite = sorted(k for k, w in self.rubric.items() if not math.isfinite(w))
        if non_finite:
            raise ValueError(
                f"RuleBasedJudge.rubric weights must be finite (no inf/-inf/nan) — got "
                f"non-finite weights for: {non_finite}"
            )
        negative = sorted(k for k, w in self.rubric.items() if w < 0)
        if negative:
            # A negative weight means "lower is better", which inverts the
            # anti-omission property below: a missing metric would then
            # *improve* the score. Forbidden until there is a design for it.
            raise ValueError(
                f"RuleBasedJudge.rubric weights must be non-negative — got negative "
                f"weights for: {negative}. A negative weight inverts the "
                f"missing-scores-as-zero rule and would reward omitting a metric."
            )
        if sum(self.rubric.values()) <= 0:
            raise ValueError(
                "RuleBasedJudge.rubric weights must sum to a positive value — an "
                "all-zero rubric divides by zero and yields nan, which AEFState.scores "
                "rejects several steps later instead of here (docs/adr/0022)"
            )

    def judge(self, state: AEFState) -> Judgment:
        # A rubric key absent from `state.scores` contributes 0.0 rather
        # than being skipped. Skipping would let a candidate raise its score
        # by simply not reporting a metric — reward hacking by omission.
        # Scoring it zero means omission can never help.
        missing = tuple(sorted(k for k in self.rubric if k not in state.scores))
        contributions = {k: state.scores.get(k, 0.0) * w for k, w in self.rubric.items()}
        score = sum(contributions.values()) / sum(self.rubric.values())

        ranked = sorted(contributions.items(), key=lambda kv: (-kv[1], kv[0]))
        drivers = ", ".join(f"{k}={v:.4g}" for k, v in ranked[:3])
        rationale = (
            f"weighted score {score:.4g} over {len(self.rubric)} rubric term(s); top: {drivers}"
        )
        if missing:
            rationale += f"; missing from state.scores (counted as 0.0): {', '.join(missing)}"

        return Judgment(score=score, rubric=dict(self.rubric), rationale=rationale)
