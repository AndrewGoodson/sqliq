"""G5 — rate, drift, and budget.

Every other gate judges one candidate in isolation. G5 is the only one that
asks about the *sequence*, and it exists because a system that accepts
individually-defensible changes forever still ends up somewhere nobody
chose.

**Drift is distance from the baseline, not the sum of steps.** This
distinction is load-bearing. Summing step sizes makes an A→B→A round trip
look like two changes' worth of drift when the system is exactly where it
started; measuring distance from the baseline correctly reports zero. But
that round trip **still consumes rate**, because the cost of churn is real
even when the net displacement is not.

**Drift is measured in lines, not files.** ADR 0053 originally counted the
fraction of *files* that differ, on the reasoning that a line-level metric
would invite spreading a change thinly across many files. That reasoning was
wrong, and running it proved so: spreading affects a *file-count* budget (a
file counts once however small the change), not a line-count one, where the
denominator is the same either way. Worse, file-level granularity is
degenerate for a small agent — a two-line change to a one-file agent scored
1.0, identical to a total rewrite, so no budget below 1.0 could admit
anything at all. G0's `max_changed_files` covers the spreading concern
directly (ADR 0049); drift measures magnitude. See ADR 0058.

**Rebaselining is rate-limited, not merely owner-only.** The superseded G5
made rebaselining an owner decision and stopped there, which leaves a
standing-pressure hole (04 §1.9): once the drift budget binds, *every*
subsequent proposal is blocked until someone rebaselines, so the pressure to
rebaseline never lets up and the budget stops meaning anything. A rate limit
on rebaselines is what makes the drift budget a budget rather than a
speed bump.

The gate **reports its numbers on pass as well as fail** — a budget nobody
can see approaching is one that only ever announces itself by blocking
something.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from aef.harness.gates.base import Gate, GateContext, GateOutcome, GateResult

DEFAULT_WINDOW = timedelta(days=7)
DEFAULT_MAX_ACCEPTED_PER_WINDOW = 3
DEFAULT_MAX_DRIFT = 0.5
DEFAULT_MAX_REBASELINES_PER_WINDOW = 1


@dataclass(frozen=True)
class AcceptedChange:
    version: int
    at: datetime


@dataclass(frozen=True)
class DriftBudget:
    window: timedelta = DEFAULT_WINDOW
    max_accepted_per_window: int = DEFAULT_MAX_ACCEPTED_PER_WINDOW
    max_drift: float = DEFAULT_MAX_DRIFT
    max_rebaselines_per_window: int = DEFAULT_MAX_REBASELINES_PER_WINDOW

    def __post_init__(self) -> None:
        if not 0.0 < self.max_drift <= 1.0:
            raise ValueError(
                f"max_drift must be within (0.0, 1.0] — 0 would block every change and a "
                f"value above 1 is unreachable, so neither is a budget; got {self.max_drift}"
            )
        if self.max_accepted_per_window < 1:
            raise ValueError("max_accepted_per_window must be at least 1")
        if self.window <= timedelta(0):
            raise ValueError("window must be positive")


def _lines(blob: bytes | None) -> list[bytes]:
    return blob.splitlines() if blob else []


def structural_drift(baseline: dict[str, bytes], current: dict[str, bytes]) -> float:
    """Fraction of LINES that differ, measured as the actual edit size.

    Distance, not path length: a line changed and changed back contributes
    nothing, which is the correct reading of "how far have we drifted from
    what the owner blessed".

    Line-level rather than file-level (ADR 0058): a two-line change to a
    one-file agent is not the same as rewriting it, and the file-level metric
    scored both 1.0.

    Edit size rather than position-wise (ADR 0064): comparing index-by-index
    treated a pure insertion as changing every line after it, so adding one
    line at the top of a 100-line file scored **1.000** against a true 0.010 —
    a hundredfold over-report that would reject any candidate adding an
    import. `SequenceMatcher` charges an insertion one line wherever it lands.
    Measured, not suspected.
    """
    paths = set(baseline) | set(current)
    if not paths:
        return 0.0

    differing = 0
    total = 0
    for path in paths:
        before = _lines(baseline.get(path))
        after = _lines(current.get(path))
        total += max(len(before), len(after))
        if before == after:
            continue
        matcher = SequenceMatcher(a=before, b=after, autojunk=False)
        common = sum(block.size for block in matcher.get_matching_blocks())
        differing += max(len(before), len(after)) - common

    if total == 0:
        # Both sides are empty files. Nothing changed, and dividing would
        # raise rather than say so.
        return 0.0
    return differing / total


# The exact phrase halt criterion 3 keys on. A constant rather than a
# substring of the prose: matching `"drift" in reason` also matched the
# "no owner-blessed baseline to measure DRIFT against" refusal — the state
# every fresh adopter starts in — and would have halted the loop on the
# second run of every new repo. Caught by a planted-fault test after my own
# planted fault used the wrong string (ADR 0077).
DRIFT_EXHAUSTED = "cumulative drift"


@dataclass(frozen=True)
class G5RateAndDrift(Gate):
    id: str = "G5"
    baseline_files: dict[str, bytes] | None = None
    candidate_files: dict[str, bytes] | None = None
    history: tuple[AcceptedChange, ...] = ()
    rebaselines: tuple[datetime, ...] = ()
    rebaseline_requested: bool = False
    now: datetime | None = None
    budget: DriftBudget = field(default_factory=DriftBudget)

    def run(self, ctx: GateContext) -> GateResult:
        # Two distinct failures, two distinct messages. They shared one for a
        # while, and the shared text named the baseline — so a candidate that
        # failed for a missing clock was told its owner had never blessed
        # anything, while version 1 sat in the archive. A refusal that
        # misnames its own cause sends the operator to fix the wrong thing
        # (ADR 0074).
        if self.baseline_files is None or self.candidate_files is None:
            return GateResult(
                gate=self.id,
                outcome=GateOutcome.FAIL,
                reason=(
                    "no owner-blessed baseline to measure drift against — without a "
                    "reference point this gate has no signal, so the candidate escalates. "
                    "Create one with `aef loop bless`."
                ),
            )
        if self.now is None:
            return GateResult(
                gate=self.id,
                outcome=GateOutcome.FAIL,
                reason=(
                    "no clock supplied, so the acceptance-rate window cannot be evaluated. "
                    "This is a wiring fault in the caller, not a fact about the candidate."
                ),
            )

        if self.rebaseline_requested:
            return self._judge_rebaseline()

        cutoff = self.now - self.budget.window
        recent = [c for c in self.history if c.at > cutoff]
        drift = structural_drift(self.baseline_files, self.candidate_files)
        numbers = (
            f"{len(recent)}/{self.budget.max_accepted_per_window} accepted in the last "
            f"{self.budget.window.days}d",
            f"drift {drift:.3f}/{self.budget.max_drift:.3f} from the blessed baseline",
        )

        if len(recent) >= self.budget.max_accepted_per_window:
            return GateResult(
                gate=self.id,
                outcome=GateOutcome.FAIL,
                reason=(
                    f"rate budget exhausted: {len(recent)} change(s) accepted in the last "
                    f"{self.budget.window.days} day(s), limit {self.budget.max_accepted_per_window}"
                ),
                evidence=numbers,
            )

        if drift > self.budget.max_drift:
            return GateResult(
                gate=self.id,
                outcome=GateOutcome.FAIL,
                reason=(
                    f"{DRIFT_EXHAUSTED}: {drift:.3f} exceeds the budget of "
                    f"{self.budget.max_drift:.3f} — individually-small accepted changes have "
                    f"accumulated past what the owner blessed. This is a decision to make, "
                    f"not a threshold to raise"
                ),
                evidence=numbers,
            )

        # Reported on pass too: a budget nobody can see approaching is one
        # that only announces itself by blocking something.
        return GateResult(gate=self.id, outcome=GateOutcome.PASS, reason="; ".join(numbers))

    def _judge_rebaseline(self) -> GateResult:
        assert self.now is not None
        cutoff = self.now - self.budget.window
        recent = [at for at in self.rebaselines if at > cutoff]
        if len(recent) >= self.budget.max_rebaselines_per_window:
            return GateResult(
                gate=self.id,
                outcome=GateOutcome.FAIL,
                reason=(
                    f"rebaseline rate limit reached: {len(recent)} in the last "
                    f"{self.budget.window.days} day(s), limit "
                    f"{self.budget.max_rebaselines_per_window}. Without this limit the drift "
                    f"budget is not a budget — once it binds, every proposal pushes for a "
                    f"rebaseline and the pressure never lets up"
                ),
            )
        return GateResult(
            gate=self.id,
            outcome=GateOutcome.FAIL,
            reason=(
                "a rebaseline is an owner decision, not a gate outcome — escalating "
                "(Tier 2). The question is 'do we accept where we have drifted to?', which "
                "is a judgement, not a measurement"
            ),
            security_event=True,
        )
