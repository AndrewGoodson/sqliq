"""Running the corpus against a variant, and building G3's cohort verdict.

This is the piece that made G2 and G3 able to pass at all. Both gates were
built and tested; neither had anything supplying it with evidence. G3 in
particular returned FAIL on every run because nothing constructed a
`CohortVerdict` — the control cohort generator existed (ADR 0054) and had no
caller.

**One execution answers both gates.** Materialising a variant and loading its
graph is the expensive step, so `scenario_runner` emits the outcome *and* the
score from a single pass. Running the corpus twice to answer two questions
about the same execution would double the cost of the most expensive gate in
the pipeline for nothing.

**The cohort costs N+2 corpus passes**, and there is no way around that: the
null hypothesis is "what do random changes score", and answering it means
scoring random changes. That expense is the price of the claim G3 makes, and
it is reported rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aef.harness.candidate import CandidateDiff
from aef.harness.corpus import Scenario
from aef.harness.evaluation import CohortVerdict, ScoreSet
from aef.harness.git import GitRepo
from aef.harness.isolated_suite import run_corpus_isolated
from aef.harness.outcome import Outcome
from aef.harness.proposer import ControlCohortGenerator, Proposal, ProposalError
from aef.harness.prose_cohort import ProseControlCohortGenerator, prose_cohort_targets
from aef.harness.sandbox import SandboxPolicy
from aef.harness.workspace import build_candidate_workspace
from aef.harness.zones import ZonePolicy
from aef.security.tool import PolicyConfig


class SuiteError(RuntimeError):
    pass


@dataclass(frozen=True)
class VariantRun:
    """One variant's results over the whole corpus."""

    label: str
    outcomes: dict[str, Outcome]
    scores: ScoreSet
    # Why individual scenarios produced nothing, as the runner reported it.
    # Carried so a harness fault is distinguishable from a bad candidate.
    failures: tuple[str, ...] = ()
    # Scenarios whose model call DIED (after one retry) rather than being
    # answered wrongly, and the ones a retry rescued. Empty on every
    # replayed run — see `scenario_runner.is_dead_call` (ADR 0185).
    dead: frozenset[str] = frozenset()
    retried: frozenset[str] = frozenset()

    @property
    def scenario_ids(self) -> frozenset[str]:
        return frozenset(self.outcomes)


def _policy_payload(config: PolicyConfig) -> dict[str, object]:
    return {
        "allowed_scopes": sorted(config.allowed_scopes),
        "forbidden_tool_names": sorted(config.forbidden_tool_names),
        "require_hitl_above_risk": config.require_hitl_above_risk,
    }


def run_variant(
    workspace: Path,
    scenarios: list[Scenario] | tuple[Scenario, ...],
    *,
    label: str,
    entrypoint: str,
    policy: SandboxPolicy,
    policy_config: PolicyConfig | None = None,
    cassette_miss: str = "fail",
    live_provider: dict[str, Any] | None = None,
) -> VariantRun:
    """Score one already-materialised workspace over the corpus.

    The candidate's code runs in a worker subprocess that is asked for one
    node at a time and never learns what a scenario is; the outcome, the
    score and the count are concluded HERE (ADR 0094). The previous
    arrangement — a subprocess that ran the corpus and printed the results —
    made the candidate the author of the evidence judging it, and three
    attempts to secure that channel were each defeated (ADR 0085, 0088, 0093).
    """
    results = run_corpus_isolated(
        workspace,
        list(scenarios),
        entrypoint=entrypoint,
        policy=policy_config,
        # ADR 0094 accepted this policy and ignored it, so the process running
        # candidate code had no rlimits, no process group and an ad-hoc
        # environment. Applied now (ADR 0095).
        sandbox=policy,
        cassette_miss=cassette_miss,
        live_provider=live_provider,
    )
    return VariantRun(
        label=label,
        outcomes={sid: r.outcome for sid, r in results.items()},
        scores=ScoreSet(
            label=label,
            per_scenario={sid: r.score for sid, r in results.items()},
            cost_tokens=sum(r.cost_tokens for r in results.values()),
        ),
        failures=tuple(f"{sid}: {r.failure}" for sid, r in sorted(results.items()) if r.failure),
        dead=frozenset(sid for sid, r in results.items() if r.dead_call),
        retried=frozenset(sid for sid, r in results.items() if r.retried),
    )


@dataclass(frozen=True)
class CohortPlan:
    """What a full G3 evaluation will cost, computed before spending it."""

    cohort_size: int
    scenarios: int

    @property
    def corpus_passes(self) -> int:
        """candidate + incumbent + one per control."""
        return self.cohort_size + 2

    @property
    def scenario_executions(self) -> int:
        return self.corpus_passes * self.scenarios

    def describe(self) -> str:
        return (
            f"{self.corpus_passes} corpus pass(es) "
            f"({self.scenario_executions} scenario execution(s)): 1 candidate + 1 incumbent "
            f"+ {self.cohort_size} random control(s)"
        )


@dataclass(frozen=True)
class CohortBuilder:
    """Builds the `CohortVerdict` G3 refuses to run without.

    The cohort mutates the **same files the candidate changed**, using the
    same machinery (ADR 0054). A cohort drawn from anywhere else would be
    measuring a different distribution than the candidate sits in, and
    beating it would prove nothing about the candidate.
    """

    repo: GitRepo
    entrypoint: str
    policy: SandboxPolicy
    zone_policy: ZonePolicy = ZonePolicy()
    cohort_size: int = 5
    seed: int = 0
    policy_config: PolicyConfig | None = None
    # ADR 0123. "fail" keeps every variant's score deterministic; "live" is
    # the owner's opt-in, and `live_provider` is read from the base ref.
    cassette_miss: str = "fail"
    live_provider: dict[str, Any] | None = None

    def plan(self, scenarios: tuple[Scenario, ...]) -> CohortPlan:
        return CohortPlan(cohort_size=self.cohort_size, scenarios=len(scenarios))

    def build(
        self,
        diff: CandidateDiff,
        scenarios: tuple[Scenario, ...],
        workroot: Path,
    ) -> tuple[CohortVerdict, VariantRun, str]:
        """Returns `(verdict, candidate_run, cost_note)`.

        The candidate's `VariantRun` is returned too so G2 can reuse it —
        the outcomes and the scores came from the same execution.
        """
        if not scenarios:
            raise SuiteError("cannot build a cohort against an empty corpus")

        candidate_ws = build_candidate_workspace(
            self.repo, diff, workroot / "candidate", self.zone_policy
        )
        candidate = run_variant(
            candidate_ws,
            scenarios,
            label="candidate",
            entrypoint=self.entrypoint,
            policy=self.policy,
            policy_config=self.policy_config,
            cassette_miss=self.cassette_miss,
            live_provider=self.live_provider,
        )

        incumbent_ws = _materialise_base(self.repo, diff, workroot / "incumbent")
        incumbent = run_variant(
            incumbent_ws,
            scenarios,
            label="incumbent",
            entrypoint=self.entrypoint,
            policy=self.policy,
            policy_config=self.policy_config,
            cassette_miss=self.cassette_miss,
            live_provider=self.live_provider,
        )

        controls = tuple(
            run_variant(
                ws,
                scenarios,
                label=label,
                entrypoint=self.entrypoint,
                policy=self.policy,
                policy_config=self.policy_config,
                cassette_miss=self.cassette_miss,
                live_provider=self.live_provider,
            )
            for label, ws in self._control_workspaces(diff, workroot)
        )

        plan = self.plan(scenarios)
        # The UNION across every arm, and it has to be the union. A scenario
        # dropped from the candidate alone would leave its mean computed over
        # one scenario set and the p95 it must beat over another — see
        # `CohortVerdict.without`. A dead call in a CONTROL matters most of
        # all: scored 0.0 it drags that control's mean down, which drags p95
        # down, which LOWERS the bar the candidate has to clear. Excluding it
        # raises the bar back (ADR 0185).
        runs = (candidate, incumbent, *controls)
        verdict = CohortVerdict(
            candidate=candidate.scores,
            incumbent=incumbent.scores,
            cohort=tuple(run.scores for run in controls),
            dead_scenarios=frozenset().union(*(run.dead for run in runs)),
            retried_scenarios=frozenset().union(*(run.retried for run in runs)),
        )
        return verdict, candidate, plan.describe()

    def _control_workspaces(self, diff: CandidateDiff, workroot: Path) -> list[tuple[str, Path]]:
        """One workspace per control mutation of a file the candidate touched.

        Two kinds of candidate, two null hypotheses, and the same rule behind
        both: the cohort mutates what the candidate mutated, in the way the
        candidate mutated it.

        - a **Python** candidate is controlled by random numeric-constant
          mutations (`ControlCohortGenerator`, ADR 0054);
        - a **prompt** candidate is controlled by length-matched placebo
          bullets (`ProseControlCohortGenerator`, ADR 0170). Before that
          existed, a `.md` candidate reached this method, found no `.py`
          entry, and G3 refused for want of a null — so a prompt candidate
          could be rejected and never accepted (ADR 0157 defect 2).

        Python wins when a candidate touched both, unchanged: the numeric
        cohort is the older and better-measured of the two.

        **The materialisation loop stays here, in one place, for both kinds.**
        It was tempting to give the prose branch its own — it is five lines —
        and `test_controls_are_built_from_the_incumbent_not_the_candidate`
        exists precisely because ADR 0074's fix for this loop had been applied
        to one code path and not the other. A second copy is a second place
        for that to happen again.
        """
        live = [e.path for e in diff.entries if not e.is_deletion]
        source_path, controls = self._controls(diff, live)

        # Controls are materialised from the INCUMBENT, not from the candidate
        # workspace. Overlaying the candidate diff and then replacing only
        # `targets[0]` left every control carrying the candidate's changes to
        # files 1..n — so on a two-file candidate all five controls scored
        # identically to the candidate, p95 rose to meet it, and G3 could
        # never pass. This is the same failure the comment below records for
        # the single-file case; the fix had been applied to one file only
        # (ADR 0074).
        made: list[tuple[str, Path]] = []
        for control in controls:
            workspace = _materialise_base(self.repo, diff, workroot / control.id)
            (workspace / source_path).write_text(control.proposed)
            made.append((control.id, workspace))
        return made

    def _controls(self, diff: CandidateDiff, live: list[str]) -> tuple[str, tuple[Proposal, ...]]:
        """`(path, cohort)` — which file the controls mutate, and how."""
        targets = [p for p in live if p.endswith(".py")]
        if targets:
            return targets[0], self._numeric_controls(diff, targets[0])
        prose = prose_cohort_targets(live)
        if prose:
            return prose[0], self._prose_controls(diff, prose[0])
        raise SuiteError(
            "the candidate changed no Python file and no prompt file, so there is nothing "
            "to mutate for a control cohort and G3 has no null hypothesis to test against"
        )

    def _incumbent_source(self, diff: CandidateDiff, source_path: str) -> str:
        """The INCUMBENT's source, not the candidate's. The null hypothesis is
        "would a random change to the incumbent have done as well as this
        reasoned change to the incumbent" — so the cohort must start where the
        candidate started. Mutating the candidate instead asks whether random
        *further* changes match it, which is a different question with a much
        higher answer: perturbing an already-improved variant frequently keeps
        the improvement, so the threshold rises to meet the candidate and
        nothing can ever beat it. Found by running the pipeline against a
        candidate that should plainly have passed.
        """
        if not self.repo.path_exists_at(diff.base_sha, source_path):
            raise SuiteError(
                f"{source_path!r} does not exist at the base ref, so there is no incumbent "
                f"version to mutate. A cohort drawn from the candidate itself tests the "
                f"wrong hypothesis; a new file needs a different control design."
            )
        return self.repo.show(diff.base_sha, source_path)

    def _numeric_controls(self, diff: CandidateDiff, source_path: str) -> tuple[Proposal, ...]:
        """Random numeric-constant mutations of the incumbent (ADR 0054)."""
        source = self._incumbent_source(diff, source_path)
        try:
            return ControlCohortGenerator(seed=self.seed).generate(
                path=source_path, source=source, size=self.cohort_size
            )
        except ProposalError as exc:
            raise SuiteError(f"cannot build a control cohort: {exc}") from exc

    def _prose_controls(self, diff: CandidateDiff, source_path: str) -> tuple[Proposal, ...]:
        """Length-matched placebo bullets (ADR 0170).

        Same two refusals as the numeric branch: a file with no incumbent
        version is refused, and anything the generator cannot build a real
        null for becomes a `SuiteError` — so G3 refuses, never passes.
        """
        source = self._incumbent_source(diff, source_path)
        candidate = self.repo.show(diff.head_sha, source_path)
        try:
            return ProseControlCohortGenerator(seed=self.seed).generate(
                path=source_path, source=source, candidate=candidate, size=self.cohort_size
            )
        except ProposalError as exc:
            raise SuiteError(f"cannot build a control cohort: {exc}") from exc


def _materialise_base(repo: GitRepo, diff: CandidateDiff, dest: Path) -> Path:
    """The incumbent: the base ref with no candidate overlay at all."""
    empty = CandidateDiff(
        base_ref=diff.base_ref,
        head_ref=diff.base_ref,
        base_sha=diff.base_sha,
        head_sha=diff.base_sha,
        entries=(),
    )
    return build_candidate_workspace(repo, empty, dest)
