"""G2 — outcome non-regression over the golden corpus.

The backbone gate, and the expensive one, which is why all four cheap gates
run before it.

**Re-execution, not replay.** `ReplayEngine` verifies that a *recorded* run
reproduces; it can say nothing about a graph that has changed, because it
re-executes recorded nodes against recorded inputs. G2 runs each scenario's
initial state through the **candidate** graph and compares outcomes.

**Outcome, not path.** See `outcome.py`: comparing execution paths admits
only no-ops. Only previously-passing scenarios are held, and routing
divergence is reported rather than rejected.

The candidate's graph is loaded and run **inside the sandbox, in the
post-merge workspace** — whose `aef/` comes from the base ref (ADR 0047).
State, routing, classification, and scoring stay in the parent harness; only
the candidate's node functions execute in the worker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from aef.harness.corpus import Corpus, Scenario, Split
from aef.harness.gates.base import Gate, GateContext, GateOutcome, GateResult
from aef.harness.isolated_suite import run_corpus_isolated
from aef.harness.outcome import Comparison, Outcome
from aef.harness.workspace import build_candidate_workspace
from aef.security.tool import PolicyConfig

# Which splits hold the candidate. The holdout is the owner's and is never
# spent on a routine gate run.
GATED_SPLITS: tuple[Split, ...] = (Split.TRAIN, Split.VALIDATION)


def _paused_note(paused: int, added: int) -> str:
    """Say out loud that the harness did not approve anything.

    The gate deliberately supplies no `hitl_approvals`, so a HITL-gated
    scenario cannot be scored on behaviour. That is a real limit on what this
    verdict covers, and a verdict that does not state its own limits invites
    being read as broader than it is (ADR 0081).
    """
    if not paused:
        return ""
    note = (
        f" {paused} scenario(s) stopped at a human-approval gate and were NOT approved by "
        f"the harness — their behaviour is unmeasured, not passing."
    )
    if added:
        note += f" {added} of those are gates the incumbent did not have."
    return note


class G2ExecutionError(RuntimeError):
    pass


# How much of a failure string the verdict carries. First line only, because a
# traceback-shaped reason turns a one-line ledger row into a page; long enough
# that "cannot import ...: TypeError: the 'package' argument is required" fits.
FAILURE_EXCERPT_CHARS = 200


def _excerpt(failure: str) -> str:
    first = failure.strip().splitlines()[0] if failure.strip() else ""
    if len(first) > FAILURE_EXCERPT_CHARS:
        return first[: FAILURE_EXCERPT_CHARS - 1] + "…"
    return first


def _with_failure(summary: str, failure: str | None) -> str:
    return f"{summary} — {_excerpt(failure)}" if failure else summary


def _why_note(regressions: list[Comparison], failures: dict[str, str]) -> str:
    """Say WHY, in the reason line, when the runner knows.

    "1 previously-passing scenario(s) no longer pass" is what a candidate that
    changed behaviour gets AND what a candidate whose entrypoint could not be
    imported got, and those are not the same verdict (ADR 0177). The runner
    computed the difference and `_execute` threw it away.
    """
    said = [failures[c.scenario_id] for c in regressions if c.scenario_id in failures]
    if not said:
        return ""
    note = f" {len(said)} of them failed rather than answered: {_excerpt(said[0])}"
    if len(said) > 1:
        note += f" (and {len(said) - 1} more)"
    return note


@dataclass(frozen=True)
class G2OutcomeNonRegression(Gate):
    id: str = "G2"
    corpus: Corpus | None = None
    # No default, for the reason `LoopConfig.entrypoint` has none (ADR 0074):
    # a default naming a layout the adopting repo does not have fails as an
    # import error inside a gate, which reads as an ordinary rejection. The
    # default was deleted from `LoopConfig` and survived here — so the driver
    # correctly reported "no entrypoint configured: G2/G3 will refuse" and G2
    # went and imported `agents.graph` anyway, crashing instead of refusing
    # (ADR 0075).
    entrypoint: str | None = None
    splits: tuple[Split, ...] = field(default_factory=lambda: GATED_SPLITS)
    # Supplied by the driver when the cohort run already executed the
    # candidate over this corpus. Materialising a variant and loading its
    # graph is the expensive step; re-running it to ask a second question
    # about the same execution would double the cost of the most expensive
    # gate for nothing.
    precomputed: dict[str, Outcome] | None = None
    policy_config: PolicyConfig | None = None

    def run(self, ctx: GateContext) -> GateResult:
        if self.corpus is None or not self.corpus.scenarios:
            # An empty corpus must never read as a pass. It means the gate
            # has no evidence, which is a Tier-2 escalation (ADR 0045
            # condition 7), not an endorsement.
            return GateResult(
                gate=self.id,
                outcome=GateOutcome.FAIL,
                reason=(
                    "no corpus scenarios to gate against — absence of evidence is not "
                    "evidence of non-regression; escalate rather than admit"
                ),
            )

        scenarios = [s for s in self.corpus.scenarios if s.split in self.splits]
        if not scenarios:
            return GateResult(
                gate=self.id,
                outcome=GateOutcome.FAIL,
                reason=(
                    f"corpus holds no scenarios in the gated splits "
                    f"{[s.value for s in self.splits]}"
                ),
            )

        # scenario id -> what the runner said went wrong. Empty when the
        # outcomes came from the cohort run, which reports outcomes only.
        failures: dict[str, str] = {}
        if self.precomputed is not None:
            candidate_outcomes = self.precomputed
        else:
            # `workspace-G2`, its own. G1 has already materialised a tree in
            # this same `ctx.workdir` and RUN BUILD COMMANDS IN IT, and
            # `trust._prepare_empty_destination` refuses a non-empty
            # destination — so while both gates named `workspace`, every
            # candidate that got past G1 without a precomputed cohort died
            # here with `TrustBoundaryError: scratch destination ... must be
            # empty`, which reads as a rejection and is a gate that could not
            # judge (ADR 0170 defect 1, reported as ADR 0157 defect 1).
            #
            # Reusing G1's tree would have been the smaller diff and the wrong
            # fix: the emptiness rule is what guarantees a gate runs against
            # base-ref + Zone A overlay and nothing else, and G1's copy has
            # had candidate-influenced build commands writing into it.
            workspace = build_candidate_workspace(
                ctx.repo, ctx.verdict.diff, ctx.workdir / f"workspace-{self.id}", ctx.zone_policy
            )
            candidate_outcomes, failures = self._execute(ctx, workspace, scenarios)

        comparisons: list[Comparison] = []
        missing: list[str] = []
        for scenario in scenarios:
            produced = candidate_outcomes.get(scenario.id)
            if produced is None:
                missing.append(scenario.id)
                continue
            comparisons.append(
                Comparison(
                    scenario_id=scenario.id,
                    incumbent=recorded_outcome(scenario),
                    candidate=produced,
                    expected=scenario.expected.value,
                )
            )

        if missing:
            # A scenario the candidate could not run at all is a failure, not
            # a gap: silently skipping it is how a corpus stops binding.
            return GateResult(
                gate=self.id,
                outcome=GateOutcome.FAIL,
                reason=f"{len(missing)} scenario(s) produced no outcome under the candidate",
                evidence=tuple(sorted(missing)),
            )

        tripwires = [c for c in comparisons if c.tripwire_hit]
        if tripwires:
            # Reported separately and as a security event: a tripwire hit is
            # not one bad scenario among many, it is evidence that the
            # self-report every other score rests on is unreliable (ADR 0060).
            return GateResult(
                gate=self.id,
                outcome=GateOutcome.FAIL,
                reason=(
                    f"{len(tripwires)} tripwire scenario(s) the owner labelled impossible "
                    f"now report success — the agent's self-report is unreliable, so no "
                    f"score derived from it means anything"
                ),
                evidence=tuple(c.summary for c in tripwires),
                security_event=True,
            )

        regressions = [c for c in comparisons if c.regressed]
        diverged = [c for c in comparisons if c.routing_diverged and not c.regressed]
        paused = [c for c in comparisons if c.candidate.hitl_paused]
        added = [c for c in comparisons if c.gate_added]

        if regressions:
            return GateResult(
                gate=self.id,
                outcome=GateOutcome.FAIL,
                reason=(
                    f"{len(regressions)} previously-passing scenario(s) no longer pass "
                    f"(zero tolerance)" + _why_note(regressions, failures)
                ),
                evidence=tuple(
                    _with_failure(c.summary, failures.get(c.scenario_id)) for c in regressions
                ),
            )

        return GateResult(
            gate=self.id,
            outcome=GateOutcome.PASS,
            reason=(
                f"{len(comparisons)} scenario(s) re-executed; every previously-passing one "
                f"still passes. {len(diverged)} changed routing (reported, not rejected)."
                + _paused_note(len(paused), len(added))
            ),
            evidence=tuple(c.summary for c in diverged + added),
        )

    def _execute(
        self, ctx: GateContext, workspace: Path, scenarios: list[Scenario]
    ) -> tuple[dict[str, Outcome], dict[str, str]]:
        """Re-execute the corpus with the candidate isolated.

        The candidate answers one node at a time in a worker subprocess and
        never sees a scenario id, an `Outcome`, or how many scenarios exist.
        This method concludes (ADR 0094).
        """
        if self.entrypoint is None:
            raise G2ExecutionError(
                "no entrypoint configured, so there is no graph to re-execute the corpus "
                "against. Pass --entrypoint <module>:<factory> naming the function that "
                "builds your graph."
            )
        results = run_corpus_isolated(
            workspace,
            scenarios,
            entrypoint=self.entrypoint,
            policy=self.policy_config,
            # The same confinement a sandboxed command gets. G2 already had
            # the policy in `ctx`; the worker was simply not given it
            # (ADR 0095).
            sandbox=ctx.sandbox_policy,
        )
        # The failure STRINGS as well as the outcomes. Dropping them here is
        # ADR 0177's R1 tail: when the candidate's worker could not import the
        # entrypoint at all, the worker said
        #
        #   IsolationError: worker for '.claude/agents/migrated/reviewer/graph.py:
        #   build_graph' failed: cannot import ...: TypeError: the 'package'
        #   argument is required to perform a relative import
        #
        # and the verdict this method fed said only "1 previously-passing
        # scenario(s) no longer pass (zero tolerance)". An import error and a
        # behavioural regression are different facts about a candidate, and
        # the ledger has to be able to tell them apart.
        return (
            {sid: r.outcome for sid, r in results.items()},
            {sid: r.failure for sid, r in results.items() if r.failure},
        )


def recorded_outcome(scenario: Scenario) -> Outcome:
    """The incumbent's outcome, reconstructed from the recorded trace.

    Derived rather than stored: a scenario file records what *happened*, and
    deriving the classification means changing the classification rule
    re-classifies the whole corpus consistently instead of leaving old
    entries judged by an old rule.
    """
    final_state = scenario.initial_state
    for record in scenario.trace:
        final_state = record.delta.apply(final_state)

    from aef.kernel.contracts import END

    terminated = bool(scenario.trace) and scenario.trace[-1].route is END
    from aef.harness.outcome import classify

    return classify(final_state, scenario.trace, terminated=terminated)
