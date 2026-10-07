"""The loop driver — the orchestration that was missing.

Every piece of the harness existed and was tested; nothing strung them
together. This module is that wiring, and it is where the ordering
decisions live.

**The kill switch is checked before anything else.** Not after reading the
ledger, not after computing the candidate — first. A halt evaluated after
the work is a report, not a stop.

**The ledger chain is verified before any command acts**, and a broken chain
refuses rather than being appended to. Appending onto damage buries the
tampering further from where it happened (ADR 0055).

**A Zone B or Zone C violation halts the loop.** It is not an ordinary
rejection to be logged and moved past — `05-approval-policy.md` §6 makes it
a halt criterion, because a proposal reaching for the judge is a category
signal about the proposer, not a fact about that one candidate.

**Tier-1 auto-merge is off, and the driver cannot turn it on.** Enabling it
is an owner action; `LoopConfig.tier1_enabled` exists so the code path can be
tested, and every caller in this repo passes `False`.
"""

from __future__ import annotations

import json
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aef.config import build_policy_config
from aef.config.factory import build_halt_channel
from aef.config.loader import AgentConfigError, load_agent_config_text
from aef.config.schema import AgentConfig
from aef.harness import archive, ledger
from aef.harness.candidate import CandidateVerdict, inspect_candidate
from aef.harness.corpus import (
    MANIFEST_FILENAME,
    Corpus,
    CorpusManifest,
    Scenario,
    Split,
    check_never_shrinks,
    load_manifest,
)
from aef.harness.gates.base import Gate, GateContext, PipelineResult, run_pipeline
from aef.harness.gates.g0_static_safety import G0StaticSafety
from aef.harness.gates.g1_builds import G1Builds
from aef.harness.gates.g2_outcome import GATED_SPLITS, G2OutcomeNonRegression
from aef.harness.gates.g3_improvement import DEFAULT_MIN_COHORT_SIZE, G3Improvement
from aef.harness.gates.g4_separation import G4SeparationOfPowers
from aef.harness.gates.g5_rate_drift import DRIFT_EXHAUSTED, AcceptedChange, G5RateAndDrift
from aef.harness.git import GitError, GitRepo
from aef.harness.monitoring import (
    Action,
    Digest,
    HaltChannel,
    KillSwitch,
    MonitorPolicy,
    Observation,
    Verdict,
    assess_halt,
    build_digest,
    evaluate_window,
)
from aef.harness.proposer import Proposal
from aef.harness.review import Decision, Disposition, decide, render_report
from aef.harness.sandbox import NetworkPolicy, SandboxPolicy, with_harness_login
from aef.harness.suite import CohortBuilder
from aef.harness.workspace import build_candidate_workspace
from aef.harness.zones import DEFAULT_AGENT_PATH, DEFAULT_AGENT_ROOT, ZonePolicy
from aef.observability.base import Tracer
from aef.providers.base import ModelProvider
from aef.security.tool import PolicyConfig

# The gates that judge a candidate WITHOUT executing it.
_CHEAP: frozenset[str] = frozenset({"G0", "G1", "G4", "G5"})

# The proposers `cycle` can run (ADR 0122, ADR 0157). `rule_based` is the
# default and `llm` is opt-in: measured on the flaky fixture and the demo agent
# before the default was chosen, and ADR 0122 carries the numbers.
#
# `rule_based_prompt` edits a `.md` persona instead of Python (ADR 0157), and
# it is NOT the default for any agent path, including a markdown one. The
# reason is stated rather than assumed: nothing available at this point knows
# what kind of node the graph's entry is — `Node` carries no kind, and
# `_build_proposer` is handed a `LoopConfig`, never a `Graph` (`cycle`'s
# `graph=` is optional and exists for `harvest`). The only signal in reach is
# the agent path's suffix, which is a filename convention rather than a fact
# about the agent, and switching proposers on a filename would make
# `--proposer` mean different things in different repos. An owner names it.
PROPOSERS: tuple[str, ...] = ("rule_based", "rule_based_prompt", "llm")

OBSERVATIONS_FILENAME = "observations.jsonl"

# Branches `cycle` and `run_loop` create for their own candidates. Named once
# so `resolve_default_base_ref` below can refuse to *inherit* one: a candidate
# proposed from an un-gated candidate would have its diff, its G0 budget and
# its G5 drift all measured against a baseline nothing blessed.
LOOP_BRANCH_PREFIX = "loop/"

# THE last-resort base ref, and the only place in `aef/` allowed to name a
# branch. It is a fallback, not the default: `resolve_default_base_ref` asks
# the repository first and reaches this only when the repository has no answer
# (a detached HEAD with no remote, or a repo with no commits at all).
#
# It is here for the reason `zones.DEFAULT_AGENT_PATH` is where it is — beside
# the thing it is a default *for*, which is `LoopConfig.base_ref` below, so the
# CLI's `--base` default and the dataclass default cannot drift apart. And it
# is a *fallback* for the reason ADR 0149 gave for that neighbour: a default
# naming a layout the adopting repo does not have makes the documented
# sequence exit 0 having done nothing. `agents/demo/graph.py` was that for the
# agent PATH; the literal `main` was the same shape for the base REF, and a
# real repo whose default branch is `azure-agent/uptime-monitoring` reached it
# from the documented defaults (ADR 0187's F-M8-1, reproduced; ADR 0189).
FALLBACK_BASE_REF = "main"

# Where the halt channel is read from when the invocation named no `--config`
# (ADR 0195). Not a guess: `aef adopt` writes `aef.yaml` at the repo root, the
# whole onboarding kit names it, and every other `--config` in this repo
# defaults an owner to typing it. It matters that this constant exists rather
# than being spelled at two call sites, because `_halt` and `digest` MUST
# resolve the same channel — a digest reporting `yes` while a halt found
# nothing is the exact failure this feature closes, one level up.
DEFAULT_CONFIG_PATH = "aef.yaml"

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_HALTED = 2
# The command could not do its job — an unexpected exception, or a
# configuration error that stops the turn before it starts.
#
# Distinct from `EXIT_REJECTED` because `aef/cli/main.py`'s catch-all returns
# **1** for any exception, and 1 is also "this candidate is no good, the system
# is working". The rendered nightly workflow fails the job on `status >= 2`,
# so a bad config, a missing corpus, an import error, a provider that is down,
# or the `agents.migrated.graph` placeholder whose `build_graph()` raises
# `NotImplementedError` all read as a healthy rejection and the job stays
# green — and, since the exception escaped before the attempt was journalled,
# `cycles.jsonl` gained nothing and the staleness alarm could never fire for
# those nights either (reproduced, ADR 0167).
#
# 3 rather than reusing 2: a halt and a crash call for different actions
# (release the kill switch versus fix the invocation), and `>= 2` catches both.
EXIT_ERROR = 3


@dataclass(frozen=True)
class LoopPaths:
    """Everything the loop persists. All of it is Zone B."""

    root: Path

    @property
    def ledger_dir(self) -> Path:
        return self.root

    @property
    def archive_dir(self) -> Path:
        return self.root / "archive"

    @property
    def lineage_dir(self) -> Path:
        """The DGM lineage archive (ADR 0160) — every candidate `run_loop`
        produced, kept and rejected. Beside `archive/`, never inside it: see
        the comment above `archive.LINEAGE_DIRNAME` for why the accepted-content
        store must not learn to hold un-gated content."""
        return self.root / archive.LINEAGE_DIRNAME

    @property
    def observations(self) -> Path:
        return self.root / OBSERVATIONS_FILENAME

    @property
    def kill_switch(self) -> KillSwitch:
        return KillSwitch(root=self.root)


@dataclass(frozen=True)
class LoopConfig:
    repo: GitRepo
    paths: LoopPaths
    # A FALLBACK, not the answer. Every CLI path resolves this from the
    # repository itself (`resolve_default_base_ref`); this default exists for
    # the API caller who constructs a `LoopConfig` by hand, and `_preflight`
    # refuses it by name rather than no-opping if the repo has no such ref.
    base_ref: str = FALLBACK_BASE_REF
    graph_id: str = "default"
    zone_policy: ZonePolicy = field(default_factory=ZonePolicy)
    corpus: Corpus | None = None
    monitor_policy: MonitorPolicy = field(default_factory=MonitorPolicy)
    # Attested by the CI container, passed in from the CLI. Never read from
    # the environment inside `sandbox.py` — the value must come from Zone B
    # configuration, not from anything a candidate can set.
    network_isolated: bool = False
    # HARD-STOP. Present so the merge path is reachable in a test; no caller
    # in this repo passes True. Turning it on is an owner action (ADR 0045).
    tier1_enabled: bool = False
    # No default. A default naming a layout the adopting repo does not have
    # fails as an import traceback buried in a ledger note, and reads as an
    # ordinary gate rejection (ADR 0069 defect 3, ADR 0074).
    entrypoint: str | None = None
    build_commands: tuple[tuple[str, ...], ...] | None = None
    cohort_size: int = 5
    cohort_seed: int = 0
    # Path to the agent config, READ FROM THE BASE REF. `None` means
    # deny-by-default, which is what an unconfigured production run gets.
    config_path: str | None = None
    # One span per gate run, when the owner wants them. Injected rather than
    # constructed, for the same reason nodes take a Tracer via Services.
    tracer: Tracer | None = None
    # Per-gate overrides, e.g. G0's max_changed_lines. Declared on
    # `GateContext` since ADR 0044 and never supplied.
    gate_limits: dict[str, Any] = field(default_factory=dict)
    gates: tuple[Gate, ...] | None = None
    sandbox_image: str | None = None
    # Which proposer `cycle` runs (ADR 0122). "llm" needs a provider and a
    # model; both are refused loudly at construction rather than at the first
    # turn, so a misconfigured run never reaches the ledger.
    proposer: str = "rule_based"
    proposer_provider: ModelProvider | None = None
    proposer_model: str | None = None
    # How a scenario's recorded model calls are replayed under the gates
    # (ADR 0123). "fail": a request the recording never saw is a failed
    # node — deterministic, credential-free, the default. "live": misses go
    # to the provider named in the base ref's `model_provider`, and the
    # score is a live one; the opt-in for scoring a prompt change.
    cassette_miss: str = "fail"
    # The `Graph.id` the PROPOSER matches recorded evidence against, when it
    # differs from the archive key above.
    #
    # `graph_id` was ONE field serving TWO namespaces, and ADR 0125 separated
    # those namespaces on purpose: `archive.versions` reads it as a directory
    # name (what `aef loop bless` blessed under), and `RuleBasedPromptProposer`
    # reads it as a `Graph.id` to admit or drop each memory record by. ADR
    # 0176's F2 could therefore only half-fix "cycle drops all the evidence
    # without --graph-id": deriving the id from the corpus moved the ARCHIVE
    # key out from under a baseline the owner had already blessed, and G5 then
    # rejected every candidate for having nothing to compare to (measured —
    # `test_an_adopted_repo_gates_a_candidate_end_to_end` went red). So it
    # derived only when there was nothing to orphan and printed a WARNING
    # otherwise, which left a real configuration in which the evidence was
    # still dropped and the command said so instead of fixing it.
    #
    # With two fields the derivation cannot orphan anything: `graph_id` stays
    # the archive key, `evidence_graph_id` follows the corpus, and the warn
    # branch is gone (ADR 0182). `None` means "the same as `graph_id`", so
    # every caller that never heard of this field behaves exactly as before.
    evidence_graph_id: str | None = None
    # The owner's halt channel, when a caller has already built one (ADR
    # 0195). `None` does NOT mean "no channel": it means "not supplied here",
    # and `_halt_channel` then reads `halt_channel:` from the agent config at
    # the BASE REF — same read as every other rule a candidate is judged by,
    # so a candidate cannot silence its own halt by editing its branch.
    halt_channel: HaltChannel | None = None
    # How many candidates one TURN may try (ADR 0200). Default 1, which is
    # exactly what every turn did before this field existed: the proposer
    # offered several and `cycle` took `proposals[0]`.
    #
    # N means N INDEPENDENT gate passes, never one pass judging a set. The
    # gates' verdict logic is untouched: each candidate is materialised on its
    # own branch, gated on its own, and recorded in the ledger on its own; the
    # turn then keeps the best of those that PASSED. The cost is linear —
    # N x (cohort_size + 2) corpus passes — which is why the default is 1 and
    # raising it is an owner's decision with a number attached.
    candidates_per_turn: int = 1
    # How many scenarios are held back from this turn to judge it afterwards
    # (ADR 0200). 0 — the default — is the behaviour that existed before:
    # nothing is held back and nothing automated reads a held-out set.
    #
    # Above 0, `audit_slice` draws that many TRAIN scenarios from the corpus
    # as it stood at the START of the turn, using the corpus's own scenario
    # ids and the calendar date and NOTHING ELSE. They are removed from what
    # G2/G3 score and their records are made inadmissible to the proposer, so
    # the loop is neither judged on them during gating nor able to learn from
    # them — and, critically, the loop cannot choose which ones they are.
    audit_slice_size: int = 0

    @property
    def evidence_id(self) -> str:
        """The `Graph.id` recorded evidence is admitted under. Read by the
        proposer and by nothing that touches the archive."""
        return self.evidence_graph_id or self.graph_id

    def __post_init__(self) -> None:
        if self.cassette_miss not in ("fail", "live"):
            raise ValueError(f"cassette_miss must be 'fail' or 'live', got {self.cassette_miss!r}")
        if self.cohort_size < DEFAULT_MIN_COHORT_SIZE:
            raise ValueError(
                f"cohort_size {self.cohort_size} is below G3's minimum of "
                f"{DEFAULT_MIN_COHORT_SIZE}; every candidate would be rejected for an "
                f"undersized cohort. Raise it, or change G3's floor deliberately."
            )
        if self.candidates_per_turn < 1:
            raise ValueError(
                f"candidates_per_turn must be at least 1; got {self.candidates_per_turn}. "
                f"Zero would make a turn that proposes nothing indistinguishable from a turn "
                f"whose proposer had nothing to say."
            )
        if self.audit_slice_size < 0:
            raise ValueError(
                f"audit_slice_size must be 0 (off) or positive; got {self.audit_slice_size}"
            )
        if self.proposer not in PROPOSERS:
            raise ValueError(f"proposer must be one of {PROPOSERS}, got {self.proposer!r}")
        if self.proposer == "llm" and (self.proposer_provider is None or not self.proposer_model):
            raise ValueError(
                "proposer='llm' needs proposer_provider (a ModelProvider) and proposer_model "
                "(the model it asks); neither has a default, because a proposer that "
                "silently ran rule-based when asked for a model would report a measurement "
                "it never made"
            )

    def sandbox_policy(self) -> SandboxPolicy:
        # An image beats an attestation. `network_isolated=True` is the CI
        # job saying "I am inside a --network none container"; an image is
        # the local run BUILDING one, and the difference is that the second
        # is verified by a probe rather than asserted (ADR 0102). That is 4b:
        # the two places can now make the same claim on the same evidence.
        if self.sandbox_image is not None:
            return SandboxPolicy(
                network=NetworkPolicy.REQUIRE_ISOLATED, container_image=self.sandbox_image
            )
        if self.network_isolated:
            return SandboxPolicy(
                network=NetworkPolicy.REQUIRE_ISOLATED, network_isolation_attested=True
            )
        return SandboxPolicy(network=NetworkPolicy.ACKNOWLEDGED_UNISOLATED)

    def default_gates(self) -> tuple[Gate, ...]:
        """All six, always registered, with no evidence attached.

        A gate with no evidence **fails** rather than being omitted —
        omitting would make a candidate look gated when it was not. Real
        evidence is attached by `_gates_with_evidence` at gate time, because
        building it requires the candidate diff.
        """
        return (
            G0StaticSafety(),
            G1Builds(commands=self.build_commands) if self.build_commands else G1Builds(),
            G4SeparationOfPowers(),
            G5RateAndDrift(),
            G2OutcomeNonRegression(corpus=self.corpus),
            G3Improvement(),
        )


@dataclass(frozen=True)
class GateRun:
    decision: Decision
    result: PipelineResult
    report: str
    exit_code: int
    halted: bool = False
    # G3's task-metric means for this candidate and the incumbent it was
    # gated against, when the behavioural gates ran (ADR 0121). None when a
    # cheap gate rejected first and nothing was scored.
    candidate_score: float | None = None
    incumbent_score: float | None = None


class BaseRefError(RuntimeError):
    """The base ref does not exist in this repository.

    A **configuration error**, in `EXIT_ERROR`'s sense of "stops the turn
    before it starts", and deliberately not a verdict: every gate reads the
    base ref, the candidate branch is cut from it and the diff is taken
    against it, so there is nothing to judge and nothing was judged.
    """


def resolve_default_base_ref(repo: GitRepo) -> str:
    """What `--base` means when the owner did not say — asked of the
    repository, once, in one function the CLI imports (ADR 0149's rule).

    The order, and why it is this order:

    1. **`refs/remotes/origin/HEAD`**, when it is a symbolic ref. This is the
       repository's own published answer to "what is the default branch", it
       is what a candidate would eventually target, and — unlike anything
       derived from HEAD — it does not change when the operator checks
       something else out. A nightly cycle and an interactive one must resolve
       the same base or the two runs are not comparable.
    2. **The branch HEAD is on**, when it is not one of the loop's own
       (`LOOP_BRANCH_PREFIX`). A fresh `git init -b trunk` has no remote at
       all, so nothing above can answer; the branch the operator is working on
       is then the only statement the repository makes about which line of
       development is current. The loop-branch exclusion is what keeps this
       from being circular: a cycle run while an un-gated candidate is checked
       out must not base the next candidate on it.
    3. **`FALLBACK_BASE_REF`**, for a detached HEAD with no remote and a repo
       with no commits — the cases where the repository has no answer at all.
       Nothing that works today changes, because a repo with `origin/HEAD` or
       a `main` checkout resolves to `main` at step 1 or 2.

    **The term that is deliberately absent**: ADR 0187's F-M8-1 sketched
    "the branch HEAD was on when the state dir was created" between 1 and 2.
    It would need a new persisted file under `--state`, i.e. new Zone B state
    and a new format, to disambiguate exactly one case — the operator sitting
    on a `loop/` branch — which step 2's exclusion handles directly from what
    already exists. A default that has to invent state to be derivable is the
    shape ADR 0139 argues against.

    Never raises: a repository that cannot answer any of these returns the
    fallback, and `require_base_ref` is what refuses.
    """
    origin_head = repo.symbolic_ref("refs/remotes/origin/HEAD")
    if origin_head:
        # `--short` renders it `origin/main`; the branch it names is the tail.
        branch = origin_head.split("/", 1)[1] if "/" in origin_head else origin_head
        if branch and repo.ref_exists(origin_head):
            return branch if repo.ref_exists(branch) else origin_head
    current = repo.symbolic_ref("HEAD")
    if current and not current.startswith(LOOP_BRANCH_PREFIX) and repo.ref_exists(current):
        return current
    return FALLBACK_BASE_REF


def require_base_ref(repo: GitRepo, ref: str) -> None:
    """Refuse, by name, a base ref this repository does not have.

    Before any other work: every "no candidate" sentence downstream describes
    a file, and a missing REF is not a missing file. The refusal lists the
    branches that do exist because a refusal naming only what is absent is not
    actionable.
    """
    if repo.ref_exists(ref):
        return
    if not repo.is_repo():
        # A plain directory has no refs to be right or wrong about, and this
        # is not the mistake this function exists to catch. Every command that
        # genuinely needs a repository fails on its own first call out to git
        # with git's own message, which is the behaviour that was already
        # there; `aef loop doctor` is a diagnostic and must still run and say
        # what is missing rather than refuse to look.
        return
    branches = repo.branch_names()
    have = ", ".join(branches) if branches else "(none)"
    raise BaseRefError(
        f"base ref {ref!r} does not exist in {repo.root.resolve()}. This repository's "
        f"branches are: "
        f"{have}. Pass --base <ref> naming one of them; the default is this repository's "
        f"own default branch (origin/HEAD, else the branch you are on), not the literal "
        f"{FALLBACK_BASE_REF!r}."
    )


class PolicyConfigError(RuntimeError):
    """`--config` was given and could not be turned into a policy.

    Loud, because the alternative was silent deny-by-default: a run that
    looks configured, is not, and says nothing (ADR 0090)."""


class LoopStateInsideRepoError(RuntimeError):
    """The loop's state directory sits inside the repository it judges."""


def _check_state_is_outside_the_repo(config: LoopConfig) -> None:
    """Refuse to run with loop state inside the working tree.

    Found by running the driver rather than by reading it: with `--state
    .loop` inside the repo, an ordinary `git add -A` sweeps the ledger and
    archive into the candidate's own commit. The ledger then appears as
    added lines in the diff being judged, every candidate looks like it
    touches Zone C, and the audit trail becomes part of the thing it is
    auditing. Deny rather than document.
    """
    repo_root = config.repo.root.resolve()
    state_root = config.paths.root.resolve()
    if state_root == repo_root or state_root.is_relative_to(repo_root):
        raise LoopStateInsideRepoError(
            f"loop state directory {state_root} is inside the repository {repo_root}. "
            f"The ledger and archive would be swept into candidate diffs by `git add -A`, "
            f"making the audit trail part of what it audits. Put --state outside the "
            f"working tree."
        )


def _corpus_baseline_manifest(config: LoopConfig, corpus: Corpus) -> CorpusManifest:
    """The never-shrinks baseline: from the BASE REF where that is possible.

    Reading it from the working tree lets a candidate delete a scenario and
    its manifest entry in one commit and pass. Reading it from the base ref
    does not, which is why the base ref is tried first and the working tree is
    a documented fallback for a corpus that lives outside the repository or is
    untracked (ADR 0141).
    """
    root = corpus.root.resolve()
    try:
        rel = root.relative_to(config.repo.root.resolve())
    except ValueError:
        return load_manifest(root)  # corpus outside the repo; nothing to read from git
    ref = f"{config.base_ref}:{rel.as_posix()}/{MANIFEST_FILENAME}"
    try:
        raw = config.repo.run_bytes("show", ref)
    except GitError:  # untracked, or no such ref — neither is a shrink
        return load_manifest(root)
    try:
        return CorpusManifest.from_payload(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        return load_manifest(root)


def _preflight(config: LoopConfig) -> tuple[ledger.LedgerEntry, ...]:
    """State location, kill switch, then ledger, archive and corpus integrity.
    In that order.

    The kill switch check comes before any work, but after the location
    check: a state directory in the wrong place means the kill switch itself
    is inside the repo, so its answer cannot be trusted.
    """
    _check_state_is_outside_the_repo(config)
    config.paths.kill_switch.check()  # raises LoopHaltedError
    entries = ledger.read(config.paths.ledger_dir)
    # The ledger's hash chain was verified on every command and the
    # ARCHIVE's append-only property never was — so a deleted version left
    # `status` reporting healthy while the rollback target it names no longer
    # existed. Both are the audit trail; both get checked (ADR 0075).
    archive.check_never_shrinks(
        config.paths.archive_dir,
        config.graph_id,
        tuple(v for _, v in ledger.merged_versions(config.paths.ledger_dir)),
    )
    # And the CORPUS's, which had the same shape of hole for longer:
    # `corpus.check_never_shrinks` had no production caller at all, so
    # deleting the two scenarios an agent fails raised `aef loop score` from
    # 0.6667 to 1.0000 with nothing complaining, while
    # `recorder.refuse_existing_ids` cited this guard as its justification
    # (ADR 0141). It is the same audit-trail argument as the two above.
    if config.corpus is not None:
        check_never_shrinks(
            config.corpus,
            _corpus_baseline_manifest(config, config.corpus),
            baseline_ref=config.base_ref,
        )
    # A live gate pass hands the operator's own harness login to the process
    # that runs candidate code, so the repo has to have said yes in writing.
    # Checked HERE, before a proposal is journalled, rather than at the first
    # cassette miss: a refusal that arrives after the candidate branch exists
    # reads as a rejection of the candidate. Costs nothing under the default
    # `cassette_miss="fail"`, which returns before reading any config.
    _live_provider_from_base_ref(config)
    return entries


def _halt_channel(config: LoopConfig) -> HaltChannel | None:
    """The command to run when this loop halts, or `None` (ADR 0195).

    Read from the BASE REF, like every other rule a candidate is judged by
    (ADR 0082/0181). A halt channel a candidate could edit on its own branch
    is one a candidate could delete on its own branch, and the halt most worth
    delivering is the one a candidate caused.

    **One resolver, used by both `_halt` and `digest`.** That is the whole
    point of the function existing: a digest reporting `Halt channel
    configured: yes` while a halt found nothing to run would be the same class
    of defect as the line this feature replaces, one level up.

    Every failure resolves to `None` — no config, no `halt_channel:` block, an
    unreadable file, a repo `git show` cannot read. The digest then says `NO`,
    which is true: nothing would run. Raising here instead would mean a
    malformed config could stop a halt from being recorded, and the halt
    matters more than the notification.
    """
    if config.halt_channel is not None:
        return config.halt_channel
    path = config.config_path or DEFAULT_CONFIG_PATH
    if Path(path).is_absolute():
        return None
    try:
        if not config.repo.path_exists_at(config.base_ref, path):
            return None
        agent_config = load_agent_config_text(
            config.repo.show(config.base_ref, path), source=f"{config.base_ref}:{path}"
        )
    except (AgentConfigError, GitError, OSError):
        return None
    return build_halt_channel(agent_config.halt_channel)


def _halt(config: LoopConfig, *, at: datetime, proposal_id: str, reasons: tuple[str, ...]) -> None:
    joined = "; ".join(reasons)
    config.paths.kill_switch.engage(joined)

    # The channel runs BEFORE the ledger entry so that the entry can record
    # what it managed — and AFTER the kill switch, so a channel that hangs to
    # its timeout cannot leave the loop running. `HaltChannel.notify` converts
    # every failure into a recorded result rather than raising, for the reason
    # the ADR states plainly: a failed notification must never mask the halt.
    channel = _halt_channel(config)
    detail: dict[str, Any] = {"reasons": list(reasons)}
    if channel is not None:
        existing = ledger.read(config.paths.ledger_dir)
        notification = channel.notify(joined, at=at, last_entry=existing[-1] if existing else None)
        detail["halt_notification"] = notification.to_payload()

    ledger.append(
        config.paths.ledger_dir,
        kind=ledger.EventKind.HALTED,
        at=at,
        proposal_id=proposal_id,
        summary=joined,
        detail=detail,
    )


def _agent_config_from_base_ref(config: LoopConfig) -> AgentConfig | None:
    """The adopter's `aef.yaml`, as of the BASE REF.

    Not from the workspace and not from the working tree. `aef.yaml` is Zone
    C — not agent-writable — but the gate must still read it the way it reads
    every other rule it judges by, or a candidate that edited it would be
    judged under rules it supplied. Same reason `trust.py` exists: if an agent
    can modify what judges it, the judgement carries no information
    (ADR 0044, ADR 0082).

    A missing or unreadable config is deny-by-default, never a pass.
    """
    if config.config_path is None:
        return None

    # `--repo`, `--state` and `--workdir` are all filesystem paths, so an
    # absolute `--config` is the natural thing to type — and `git show
    # <ref>:/abs/path` finds nothing, so the configured policy was silently
    # discarded and the run continued deny-by-default with no message. A
    # policy that quietly does not apply is worse than one that refuses
    # (ADR 0090).
    if Path(config.config_path).is_absolute():
        raise PolicyConfigError(
            f"--config must be a path INSIDE the repository, relative to its root — got "
            f"{config.config_path!r}. The gate reads it from the base ref via `git show`, "
            f"which cannot resolve an absolute path."
        )

    if not config.repo.path_exists_at(config.base_ref, config.config_path):
        raise PolicyConfigError(
            f"{config.config_path!r} does not exist at {config.base_ref!r}. The gate reads "
            f"policy from the base ref so a candidate cannot widen its own rules; a config "
            f"that exists only on the candidate branch is exactly what that prevents."
        )

    try:
        raw = config.repo.show(config.base_ref, config.config_path)
        agent_config = load_agent_config_text(raw, source=f"{config.base_ref}:{config.config_path}")
    except AgentConfigError as exc:
        # Deny-by-default is the right VALUE and silence was the wrong
        # delivery: an invalid config became indistinguishable from a
        # deliberately restrictive one.
        raise PolicyConfigError(
            f"agent config at {config.base_ref}:{config.config_path} did not load, so no "
            f"policy could be built: {exc}"
        ) from exc
    return agent_config


def _policy_from_base_ref(config: LoopConfig) -> PolicyConfig | None:
    """The adopter's configured policy, as of the BASE REF (see above).
    A missing config is deny-by-default, never a pass."""
    agent_config = _agent_config_from_base_ref(config)
    if agent_config is None:
        return None
    return build_policy_config(agent_config.tools, agent_config.policies)


class AgentSourceMissingError(Exception):
    """`--agent-path` names a file that is not in the base ref.

    Not a `PolicyConfigError`, for `LiveGatingWithoutConfigError`'s reason:
    the rules were read fine, the INVOCATION points at nothing. `EXIT_ERROR`.
    """


class LiveGatingWithoutConfigError(Exception):
    """`--cassette-miss live` was asked for and NO config says which provider.

    Deliberately **not** a `PolicyConfigError`, unlike its sibling below.
    That class means "the rules could not be read", which every CLI command
    reports as `EXIT_REJECTED` — a verdict on a candidate. This is a fault in
    the INVOCATION: nobody typed `--config`, so there is nothing to build a
    provider from, and reporting it as a rejection is what makes CI retry it
    forever (ADR 0075's rule, ADR 0167's exit code). It reaches `EXIT_ERROR`.

    Reproduced (ADR 0191's F1). `_live_provider_from_base_ref` returned
    `None` for a missing config BEFORE reading the opt-in, so live mode ran
    with no provider at all. Every scenario then failed with

        ModelProviderError: cassette miss (...) and no live provider to fall
        through to

    which names a `ModelProviderError`, so ADR 0185's `is_dead_call` — gated
    only on the mode string — classified **every scenario in the corpus** as a
    dead call. Each was retried (the whole corpus ran twice), and up to 25% of
    them were then EXCLUDED from G3 rather than scored 0: the identical
    per-scenario numbers give `G3 FAIL — 1 previously-passing scenario(s) now
    score below 0.5` when counted and `G3 PASS — candidate mean 1 beats the
    control cohort's p95 of 0.9429` when excluded. Above the refusal floor G3
    refused with "the model was not answering" when the truth was "you omitted
    --config". Throughout, the ledger recorded `live_model_calls: false`.
    """


class LiveGatingDisabledError(PolicyConfigError):
    """`--cassette-miss live` was asked for and the repo has not opted in.

    A `PolicyConfigError` on purpose: this is the same class of refusal as an
    unreadable `aef.yaml` — the run cannot be judged under rules nobody
    supplied — and the CLI already reports that class by name instead of
    letting it surface as a rejected candidate (ADR 0090).
    """


def _live_provider_from_base_ref(config: LoopConfig) -> dict[str, Any] | None:
    """Which provider the worker may build for cassette misses (ADR 0123).

    Only under `cassette_miss="live"`, and only what the BASE REF's
    `model_provider` declares — read the way the policy is read, so a
    candidate cannot point the gate at a provider of its choosing by editing
    the workspace's config. `None` otherwise, and a live miss with no config
    then fails in the worker naming the absence.

    **The WHOLE block crosses, not `{impl, model}`** (ADR 0181, closing ADR
    0158's F-M5-2). `ModelProviderConfig` is data — a strict pydantic model
    that round-trips through `model_dump(mode="json")` — and putting two of
    its fields on the wire meant the worker rebuilt a config the schema then
    refused: `impl: command` carries its argv template in a `command:` block,
    so every scenario failed with `worker refused configuration` and G2 read
    that as a behavioural regression. The credential-free provider — the one
    ADR 0154 points every new adopter at — was the one provider a live gate
    pass could not use.

    **And it is gated on an explicit per-repo opt-in.** See
    `GatesConfig.live_model_calls`: the worker is where candidate code runs,
    so serving its model calls live means the operator's own harness login is
    reachable from that process. Off by default; refused by name when asked
    for without it.
    """
    if config.cassette_miss != "live":
        return None
    agent_config = _agent_config_from_base_ref(config)
    if agent_config is None:
        # NOT `return None`. That was the hole (ADR 0191's F1): live mode with
        # no provider is not "live gating off", it is a run in which every
        # model call is guaranteed to fail, and ADR 0185 then classifies every
        # one of those failures as a dead call and excludes it. Refused HERE
        # rather than in the CLI so that every caller gets it — the library
        # entry points (`gate`, `cycle`, `run`) as well as the four commands.
        raise LiveGatingWithoutConfigError(
            "--cassette-miss live needs a --config: there is no aef.yaml for the gate to read "
            f"model_provider from at {config.base_ref!r}, so no provider can be built and "
            "EVERY model call the corpus makes would miss with 'no live provider to fall "
            "through to'. Those misses name a ModelProviderError, so each one classifies as a "
            "dead call, is retried once, and is then EXCLUDED from G3 — a run in which the "
            "model was never reached would read as a run the model answered. Pass --config "
            "<path to aef.yaml, relative to the repository root> with gates.live_model_calls: "
            "true, or drop --cassette-miss live and score from the recorded cassettes."
        )
    if not agent_config.gates.live_model_calls or agent_config.model_provider is None:
        raise LiveGatingDisabledError(
            f"live gating is off in {config.config_path or 'aef.yaml'}; a prompt candidate "
            f"cannot be scored from a cassette — set gates.live_model_calls: true, which "
            f"lets a candidate's code spend your harness quota. Until then "
            f"`--cassette-miss live` is refused rather than run: without the opt-in the "
            f"gate's worker inherits no login, every miss fails, and G2 reports that as "
            f"'previously-passing scenario(s) no longer pass' — an artifact of the "
            f"environment rather than a judgement of the prompt (ADR 0158, ADR 0181)."
        )
    return dict(agent_config.model_provider.model_dump(mode="json"))


def _live_model_calls(config: LoopConfig) -> bool:
    """Whether THIS gate pass lets a candidate's model calls reach a provider.

    Recorded on the `gated` ledger event. The audit trail could otherwise not
    answer the one question the opt-in exists to make answerable: did the
    candidate that was judged here run under the operator's login?
    """
    return _live_provider_from_base_ref(config) is not None


def _worker_sandbox_policy(
    config: LoopConfig, live_provider: dict[str, Any] | None
) -> SandboxPolicy:
    """The sandbox the gate's worker runs under, widened only when it must be.

    Tied to `live_provider` rather than to `cassette_miss` alone: with no
    `--config` there is no provider to build worker-side, so widening the
    allowlist would inherit a credential for calls that can never be made.
    The narrowest widening that serves the opt-in, and nothing wider.
    """
    policy = config.sandbox_policy()
    if live_provider is None:
        return policy
    return with_harness_login(policy)


def _cheap_gates(config: LoopConfig, verdict: CandidateVerdict, now: datetime) -> tuple[Gate, ...]:
    """G0, G1, G4, G5 — everything that can reject a candidate WITHOUT
    executing its corpus. Run first so a rejected candidate never runs."""
    if config.gates is not None:
        return tuple(g for g in config.gates if g.id in _CHEAP)
    gates = [g for g in config.default_gates() if g.id in _CHEAP]
    return tuple(_with_g5(config, verdict, now, gates))


def _behavioural_only(gates: tuple[Gate, ...]) -> tuple[Gate, ...]:
    return tuple(g for g in gates if g.id not in _CHEAP)


class CorpusGraphMismatchError(RuntimeError):
    """The corpus holds several graphs' recordings and none are this one's.

    Named, and raised, rather than returning an empty scenario list: an empty
    corpus reads to G2/G3 as "no evidence", which escalates — the same
    outcome a corpus this loop simply cannot use, with none of the
    information about why (ADR 0125).
    """


def _scenarios_for_graph(
    config: LoopConfig, scenarios: tuple[Scenario, ...]
) -> tuple[tuple[Scenario, ...], str]:
    """The gated scenarios recorded FROM this graph.

    `corpus/` now holds two agents' recordings (`demo_agent` and
    `summary_agent`, ADR 0123), and the gates ran every one of them against
    whichever graph the entrypoint named — so a demo candidate was scored on
    summary scenarios it could not possibly satisfy and every mean was
    diluted by them. `aef loop score` already filtered (it can load the
    graph in-process and read `graph.id`); the gates, which cannot, did not.

    The rule, stated because it is a choice rather than a deduction:

    - Some gated scenario carries `graph_id == config.graph_id` → gate on
      exactly those. This is the case the mixed corpus creates.
    - No scenario matches and the corpus records ONE graph → gate on all of
      them. `--graph-id` defaults to `"default"` and is the ARCHIVE key, a
      different namespace from `graph.id`; a single-graph corpus has nothing
      to disambiguate, so requiring the two namespaces to agree would break
      every existing single-graph corpus to fix a mixed-corpus defect.
    - No scenario matches and the corpus records SEVERAL graphs → refuse by
      name. There is no defensible subset, and running all of them is the
      defect.
    """
    matching = tuple(s for s in scenarios if s.graph_id == config.graph_id)
    present = sorted({s.graph_id for s in scenarios})
    if matching:
        return matching, (
            f"{len(matching)}/{len(scenarios)} gated scenario(s) recorded from "
            f"graph {config.graph_id!r}"
        )
    if len(present) == 1:
        return scenarios, (
            f"corpus records one graph ({present[0]!r}); gating all "
            f"{len(scenarios)} gated scenario(s)"
        )
    raise CorpusGraphMismatchError(
        f"the corpus records {len(present)} graphs ({', '.join(repr(g) for g in present)}) "
        f"and none of them is --graph-id {config.graph_id!r}. Gating one graph against "
        f"another's scenarios dilutes every mean with scenarios the candidate cannot "
        f"satisfy. Pass --graph-id naming the graph this loop improves."
    )


@dataclass(frozen=True)
class AuditSlice:
    """The scenarios that judge this turn, and how they were chosen.

    ADR 0200. The reviewer's third clause was that the holdout "is read by no
    automated comparison — only by `--i-am-spending-the-holdout` by hand", and
    the decision recorded there is that it stays that way: the owner's holdout
    is two scenarios, and a statistic over two is not a comparison. What an
    automated read gets instead is a slice of the TRAIN split, **rotated by
    the calendar**, held back from the gates for the turn that is judged by it.

    The rule this exists to keep is one sentence: **the loop may never choose
    which scenarios are in the set that judges it.** `pick` therefore takes
    the corpus and a date and NOTHING ELSE — no score, no ledger, no memory,
    no candidate, no proposer state. A test pins the signature, because the
    guarantee is exactly the absence of those arguments.
    """

    ids: tuple[str, ...]
    rotation_key: str
    drawn_from: int

    @property
    def is_empty(self) -> bool:
        return not self.ids

    def describe(self) -> str:
        if self.is_empty:
            return "audit slice: empty (nothing held back)"
        return (
            f"audit slice {self.rotation_key}: {len(self.ids)} of {self.drawn_from} train "
            f"scenario(s) held back — {', '.join(self.ids)}"
        )


def audit_slice(corpus: Corpus | None, *, at: datetime, size: int) -> AuditSlice:
    """Which scenarios are held back to judge the loop on `at`'s date.

    Deterministic in `(the corpus's train ids, the date)`. Rotating means a
    slice spent tonight is not the slice spent tomorrow, so no single group of
    scenarios becomes a second validation split by being read every night; and
    because the draw is an arithmetic function of the sorted ids and the day
    number, nothing the loop computes can move it.

    **What the loop CAN do, stated rather than glossed:** `harvest` promotes
    production runs into the train split, so over many nights the loop grows
    the POOL this draws from. It cannot grow tonight's pool — the corpus this
    reads is the one loaded before the turn's harvest ran — and it cannot
    choose which members of the pool are drawn on any night. That is a
    narrower guarantee than the owner's holdout gives, and it is why the
    holdout stays the owner's.
    """
    key = f"{at:%Y-%m-%d}"
    if corpus is None or size < 1:
        return AuditSlice(ids=(), rotation_key=key, drawn_from=0)
    pool = sorted(s.id for s in corpus.scenarios if s.split is Split.TRAIN)
    # Never hold back so much that G2/G3 lose their evidence: at most half the
    # train split, and never the whole of it. A held-out set that empties the
    # gated set does not test the loop, it disables it.
    take = min(size, len(pool) // 2)
    if take < 1:
        return AuditSlice(ids=(), rotation_key=key, drawn_from=len(pool))
    start = (at.date().toordinal() * take) % len(pool)
    ids = tuple(pool[(start + i) % len(pool)] for i in range(take))
    return AuditSlice(ids=ids, rotation_key=key, drawn_from=len(pool))


def _audit_slice_for(config: LoopConfig, now: datetime) -> AuditSlice:
    return audit_slice(config.corpus, at=now, size=config.audit_slice_size)


def _corpus_with_audit_held_out(config: LoopConfig, held: AuditSlice) -> Corpus | None:
    """The corpus the PROPOSER is allowed to learn from this turn.

    The held-back scenarios are relabelled `validation`, which is the existing
    machinery for "recorded, gated by the owner's rules, and not citable":
    `MemoryEvidence.from_store` already refuses to cite a record whose
    `run_id` is a validation or holdout scenario. Relabelling rather than
    deleting matters — a scenario removed from the corpus handed to the
    proposer would also stop `check_never_shrinks` from seeing it.
    """
    if config.corpus is None or held.is_empty:
        return config.corpus
    ids = set(held.ids)
    return replace(
        config.corpus,
        scenarios=tuple(
            replace(s, split=Split.VALIDATION) if s.id in ids else s
            for s in config.corpus.scenarios
        ),
    )


@dataclass(frozen=True)
class AuditComparison:
    """A held-out read of a candidate the gates already passed.

    It does NOT gate. The moment this changed a keep/revert decision the loop
    would be selected against the slice, and a set the loop is selected
    against is not held out — the same argument that keeps the owner's holdout
    owner-only, one level down. It is recorded, printed and surfaced, and a
    regression here is the signal that the gates have a blind spot.
    """

    rotation_key: str
    ids: tuple[str, ...]
    candidate_mean: float | None
    incumbent_mean: float | None
    regressed: tuple[str, ...]
    note: str

    @property
    def delta(self) -> float | None:
        if self.candidate_mean is None or self.incumbent_mean is None:
            return None
        return self.candidate_mean - self.incumbent_mean

    def line(self) -> str:
        if self.candidate_mean is None or self.incumbent_mean is None:
            return f"audit ({self.rotation_key}): not read — {self.note}"
        delta = self.delta
        verdict = "REGRESSED" if self.regressed else "held"
        return (
            f"audit ({self.rotation_key}) on {len(self.ids)} held-back scenario(s): "
            f"candidate {self.candidate_mean:.4f} vs incumbent {self.incumbent_mean:.4f} "
            f"(delta {delta:+.4f}) — {verdict}"
            + (f"; regressed: {', '.join(self.regressed)}" if self.regressed else "")
        )


def _run_audit(
    config: LoopConfig,
    verdict: CandidateVerdict,
    *,
    held: AuditSlice,
    workdir: Path,
) -> AuditComparison:
    """Score the candidate and the incumbent on the held-back slice.

    Two corpus passes over `len(held.ids)` scenarios, and no control cohort:
    this is not a second G3. G3 asks "did a reasoned change beat random
    changes on the set it was gated on"; this asks the one question the gated
    set cannot answer — "does it still hold on scenarios nothing in this turn
    was allowed to see".
    """
    from aef.harness.suite import _materialise_base, run_variant

    if config.entrypoint is None or config.corpus is None:
        return AuditComparison(
            rotation_key=held.rotation_key,
            ids=held.ids,
            candidate_mean=None,
            incumbent_mean=None,
            regressed=(),
            note="no entrypoint configured, so nothing can execute the held-back scenarios",
        )
    scenarios = tuple(s for s in config.corpus.scenarios if s.id in set(held.ids))
    if not scenarios:
        return AuditComparison(
            rotation_key=held.rotation_key,
            ids=held.ids,
            candidate_mean=None,
            incumbent_mean=None,
            regressed=(),
            note="the held-back ids are not in the corpus this turn loaded",
        )

    policy_config = _policy_from_base_ref(config)
    live_provider = _live_provider_from_base_ref(config)
    sandbox = _worker_sandbox_policy(config, live_provider)
    try:
        candidate_ws = build_candidate_workspace(
            config.repo, verdict.diff, workdir / "audit" / "candidate", config.zone_policy
        )
        candidate = run_variant(
            candidate_ws,
            scenarios,
            label="audit-candidate",
            entrypoint=config.entrypoint,
            policy=sandbox,
            policy_config=policy_config,
            cassette_miss=config.cassette_miss,
            live_provider=live_provider,
        )
        incumbent_ws = _materialise_base(config.repo, verdict.diff, workdir / "audit" / "incumbent")
        incumbent = run_variant(
            incumbent_ws,
            scenarios,
            label="audit-incumbent",
            entrypoint=config.entrypoint,
            policy=sandbox,
            policy_config=policy_config,
            cassette_miss=config.cassette_miss,
            live_provider=live_provider,
        )
    except Exception as exc:  # noqa: BLE001 - a failed READ must not fail the turn
        # The audit is a read, not a gate. A harness fault here (a workspace
        # that will not materialise, a worker that dies) must be REPORTED as
        # an unread audit rather than turned into a verdict on the candidate —
        # the candidate has already been judged by six gates that did run.
        return AuditComparison(
            rotation_key=held.rotation_key,
            ids=held.ids,
            candidate_mean=None,
            incumbent_mean=None,
            regressed=(),
            note=f"could not be read ({type(exc).__name__}: {exc})",
        )

    if not candidate.scores.n or not incumbent.scores.n:
        return AuditComparison(
            rotation_key=held.rotation_key,
            ids=held.ids,
            candidate_mean=None,
            incumbent_mean=None,
            regressed=(),
            note="one of the two arms produced no score at all, so there is nothing to compare",
        )
    regressed = tuple(
        sorted(
            sid
            for sid, before in incumbent.scores.per_scenario.items()
            if candidate.scores.per_scenario.get(sid, 0.0) < before
        )
    )
    return AuditComparison(
        rotation_key=held.rotation_key,
        ids=held.ids,
        candidate_mean=candidate.scores.mean,
        incumbent_mean=incumbent.scores.mean,
        regressed=regressed,
        note=f"2 corpus pass(es) over {len(scenarios)} held-back scenario(s)",
    )


def _gates_with_evidence(
    config: LoopConfig, verdict: CandidateVerdict, workdir: Path, now: datetime
) -> tuple[tuple[Gate, ...], str]:
    """Attach real evidence to G2, G3 and G5.

    Without this, G3 returns FAIL on every run because nothing constructs a
    `CohortVerdict` (ADR 0051), and G5 returns FAIL because nothing supplies
    a blessed baseline. The gates were built and tested; they had no caller
    feeding them, which meant the pipeline could not pass a candidate even
    with a perfect corpus.

    `now` is the caller's clock, threaded down rather than read from config.
    It used to come from `LoopConfig.now_for_gates`, which **no CLI command
    ever set** — so G5 ran with `now=None`, failed, and the fail-fast pipeline
    stopped before G2 and G3. Those two gates had never executed outside a
    test (ADR 0074).

    G5 is wired **before** the cohort is attempted, because a cohort failure
    is not evidence about the baseline: wiring it afterwards meant one
    `SuiteError` disabled three gates and made G5 report a missing baseline
    that was sitting in the archive.

    Building the cohort costs N+2 corpus passes. It is NOT skipped merely
    because it is expensive — a gate that is dropped when it is inconvenient
    is not a gate.
    """
    if config.gates is not None:
        return _behavioural_only(config.gates), "gates supplied explicitly"

    gates = _with_g5(config, verdict, now, list(config.default_gates()))

    if config.entrypoint is None:
        return _behavioural_only(tuple(gates)), (
            "no entrypoint configured: G2/G3 will refuse. Pass --entrypoint "
            "<module>:<factory> naming the function that builds your graph."
        )
    if config.corpus is None or not config.corpus.scenarios:
        return _behavioural_only(tuple(gates)), "no corpus: G2/G3 will refuse for lack of evidence"

    scenarios = tuple(s for s in config.corpus.scenarios if s.split in GATED_SPLITS)
    # HELD BACK BEFORE THE GATES SEE THEM (ADR 0200). A scenario the candidate
    # is scored on during gating is a scenario the loop is selected on, and a
    # set the loop is selected on is not held out. `audit_slice_size` defaults
    # to 0, so with no owner opt-in this subtracts nothing and every existing
    # measurement stands.
    held = _audit_slice_for(config, now)
    held_note = ""
    if not held.is_empty:
        before = len(scenarios)
        scenarios = tuple(s for s in scenarios if s.id not in set(held.ids))
        held_note = f"; {before - len(scenarios)} held back for the audit ({held.rotation_key})"
    if not scenarios:
        return _behavioural_only(tuple(gates)), "no gated-split scenarios: G2/G3 will refuse"
    scenarios, graph_note = _scenarios_for_graph(config, scenarios)
    graph_note += held_note

    policy_config = _policy_from_base_ref(config)
    # One read, two uses: the provider the worker may build, and whether its
    # environment has to carry the login that provider needs. Computing them
    # apart is how the two could disagree (ADR 0091), and the disagreement
    # would be a credential inherited for calls nothing makes.
    live_provider = _live_provider_from_base_ref(config)
    builder = CohortBuilder(
        repo=config.repo,
        entrypoint=config.entrypoint,
        policy=_worker_sandbox_policy(config, live_provider),
        zone_policy=config.zone_policy,
        cohort_size=config.cohort_size,
        seed=config.cohort_seed,
        policy_config=policy_config,
        cassette_miss=config.cassette_miss,
        live_provider=live_provider,
    )
    try:
        cohort_verdict, candidate_run, note = builder.build(
            verdict.diff, scenarios, workdir / "variants"
        )
    except Exception as exc:  # noqa: BLE001 - see below
        # ANY exception, not just SuiteError. `_gates_with_evidence` sits
        # BETWEEN the two `run_pipeline` calls, so `_run_traced`'s
        # raise-to-FAIL conversion (ADR 0090) does not cover it — a KeyError
        # from `_parse`, a GitError from workspace materialisation, anything
        # at all escaped `gate()` and left the proposal with a PROPOSED
        # ledger entry and no verdict. The exact hole ADR 0090 §1 says was
        # closed, on the one path that executes candidate code (ADR 0093).
        # Reported, not swallowed: G2/G3 stay in the pipeline and refuse,
        # so the candidate escalates rather than slipping through ungated.
        # G5 keeps its evidence — see the docstring.
        return _behavioural_only(
            tuple(gates)
        ), f"could not build evidence ({exc}); G2/G3 will refuse"

    rebuilt: list[Gate] = []
    for g in gates:
        if isinstance(g, G2OutcomeNonRegression):
            rebuilt.append(
                G2OutcomeNonRegression(
                    # The SAME scenarios the cohort ran. G2 reads its corpus
                    # itself and reports anything absent from `precomputed`
                    # as missing — so handing it the unfiltered corpus after
                    # filtering the cohort would turn every other graph's
                    # scenario into a missing one, which is a rejection.
                    corpus=replace(config.corpus, scenarios=scenarios),
                    precomputed=candidate_run.outcomes,
                    policy_config=policy_config,
                )
            )
        elif isinstance(g, G3Improvement):
            # G3's cohort floor is deliberately left at its own default.
            # Passing the configured size through as the floor made the guard
            # unsatisfiable — the builder generates exactly that many members,
            # so the comparison could never be true, and a two-member cohort
            # passed with p95 computed over two samples. A floor that moves
            # with the thing it floors is not a floor (ADR 0063).
            rebuilt.append(G3Improvement(verdict=cohort_verdict))
        else:
            rebuilt.append(g)
    return _behavioural_only(tuple(rebuilt)), f"{note}; {graph_note}"


def _with_g5(
    config: LoopConfig, verdict: CandidateVerdict, now: datetime, gates: list[Gate]
) -> list[Gate]:
    baseline = _blessed_baseline(config)
    if baseline is None:
        return gates
    return [
        G5RateAndDrift(
            baseline_files=baseline,
            candidate_files=_candidate_files(config, verdict),
            history=_accepted_history(config),
            now=now,
        )
        if isinstance(g, G5RateAndDrift)
        else g
        for g in gates
    ]


def _blessed_baseline(config: LoopConfig) -> dict[str, bytes] | None:
    """The owner-blessed archive version G5 measures drift against."""
    existing = archive.versions(config.paths.archive_dir, config.graph_id)
    if not existing:
        return None
    return archive.read_files(config.paths.archive_dir, config.graph_id, existing[0])


def _candidate_files(config: LoopConfig, verdict: CandidateVerdict) -> dict[str, bytes]:
    """The candidate's **whole Zone A tree**, not just the files it changed.

    Drift is `structural_drift(baseline, candidate)`, which unions the two key
    sets. Passing only the changed files made every blessed file the candidate
    left alone score as fully deleted, and every added file score against a
    denominator missing the untouched tree — so the *first* candidate after a
    blessing was rejected for 0.583 drift it had not caused (ADR 0074).
    """
    root = config.zone_policy.agent_root
    paths = set(config.repo.list_tree(verdict.diff.head_sha, root))
    # A deletion inside Zone A is real drift and must survive: it is absent
    # from the head tree by definition, so the union with the baseline's keys
    # is what charges it.
    return {p: config.repo.run_bytes("show", f"{verdict.diff.head_sha}:{p}") for p in sorted(paths)}


def _accepted_history(config: LoopConfig) -> tuple[AcceptedChange, ...]:
    entries = ledger.read(config.paths.ledger_dir)
    return tuple(
        AcceptedChange(version=int(e.detail.get("archive_version", 0)), at=e.at)
        for e in entries
        if e.kind is ledger.EventKind.MERGED
    )


def _drift_exhausted_twice(entries: tuple[ledger.LedgerEntry, ...]) -> bool:
    """Halt criterion 3. Read from the ledger, which has always held the
    evidence — `assess_halt` accepted the flag and nothing ever computed it,
    so two of the five criteria were dead parameters (ADR 0074).

    "In quick succession" is read as the two most recent gated verdicts: a
    proposer that exhausts the drift budget, is told so, and immediately does
    it again is not responding to the signal.
    """
    drift_failures = [
        any(
            g.get("gate") == "G5"
            and g.get("outcome") == "fail"
            and g.get("reason", "").startswith(DRIFT_EXHAUSTED)
            for g in e.detail.get("gates", [])
        )
        for e in entries
        if e.kind is ledger.EventKind.GATED
    ]
    return len(drift_failures) >= 2 and all(drift_failures[-2:])


def _consecutive_escalation_rejections(entries: tuple[ledger.LedgerEntry, ...]) -> int:
    """Halt criterion 4: escalations subsequently resolved by rejection.

    Counted over the TRAILING run of proposals, stopping at the first one
    that ended any other way. The first implementation only reset on
    `MERGED` — which is written solely on the auto-merge path, and Tier-1 is
    off — so nothing reset it from the CLI and it was a lifetime counter.
    Two re-gated proposals thirty days apart, with twenty ordinary rejections
    between them, halted the loop for "working outside its evidence base"
    (ADR 0080).

    Escalation is the NORMAL terminal state while Tier-1 is off, so this has
    to be about a trailing pattern or it is about nothing.
    """
    outcome: dict[str, list[ledger.EventKind]] = {}
    order: list[str] = []
    for entry in entries:
        if entry.kind not in (
            ledger.EventKind.ESCALATED,
            ledger.EventKind.REJECTED,
            ledger.EventKind.MERGED,
        ):
            continue
        if entry.proposal_id not in outcome:
            outcome[entry.proposal_id] = []
            order.append(entry.proposal_id)
        outcome[entry.proposal_id].append(entry.kind)

    run = 0
    for proposal_id in reversed(order):
        kinds = outcome[proposal_id]
        escalated_then_rejected = (
            ledger.EventKind.ESCALATED in kinds
            and ledger.EventKind.REJECTED in kinds
            and kinds.index(ledger.EventKind.ESCALATED) < kinds.index(ledger.EventKind.REJECTED)
        )
        if not escalated_then_rejected:
            break
        run += 1
    return run


def gate(
    config: LoopConfig,
    head_ref: str,
    *,
    now: datetime,
    workdir: Path,
    proposal: Proposal | None = None,
) -> GateRun:
    """Evaluate one candidate branch end to end."""
    # FIRST. Every gate below reads the base ref — the diff is taken against
    # it, G5's baseline tree is listed at it, the policy is loaded from it —
    # so a ref that does not exist makes each of them fail describing
    # something else (ADR 0187 F-M8-1, closed in 0189). Not inside
    # `_preflight`, deliberately: `monitor` shares it and reads no ref, and a
    # readonly `aef loop monitor` must not start requiring one.
    require_base_ref(config.repo, config.base_ref)
    _preflight(config)

    proposal_id = f"{head_ref}@{config.repo.rev_parse(head_ref)[:12]}"
    verdict = inspect_candidate(config.repo, config.base_ref, head_ref, config.zone_policy)

    ledger.append(
        config.paths.ledger_dir,
        kind=ledger.EventKind.PROPOSED,
        at=now,
        proposal_id=proposal_id,
        summary=f"{verdict.diff.changed_files} file(s), {verdict.diff.changed_lines} line(s)",
        detail={"base": config.base_ref, "head": head_ref, "paths": list(verdict.diff.paths)},
    )

    ctx = GateContext(
        # `tracer` had exactly one production construction site and it passed
        # neither of these, so `_run_traced`'s traced branch never executed
        # outside a test and G0's `max_changed_lines`/`max_changed_files`
        # overrides were unreachable by construction (ADR 0092).
        tracer=config.tracer,
        limits=dict(config.gate_limits),
        repo=config.repo,
        base_ref=config.base_ref,
        head_ref=head_ref,
        verdict=verdict,
        workdir=workdir,
        zone_policy=config.zone_policy,
        sandbox_policy=config.sandbox_policy(),
    )
    # TWO PASSES, and the split is a containment boundary, not an
    # optimisation. Building evidence EXECUTES THE CANDIDATE'S CODE — N+2
    # corpus passes in subprocesses — and it used to happen before
    # `run_pipeline` ran anything. So G0's import allowlist, the control that
    # exists for exactly this, ran second: a candidate G0 would reject for
    # `import socket` had already run its module-level code by then.
    # `gates/base.py` states the opposite ordering as the design ("all four
    # cheap gates run before the expensive corpus re-execution in G2. G4 is
    # deliberately early: a proposal reaching for its own tests is rejected
    # before it gets to run them"). It was not true (ADR 0085).
    #
    # Canonical order is preserved exactly — G0,G1,G4,G5 then G2,G3 — and a
    # candidate rejected by a cheap gate now never executes at all.
    candidate_score: float | None = None
    incumbent_score: float | None = None
    cheap = run_pipeline(_cheap_gates(config, verdict, now), ctx)
    if not cheap.passed:
        result = cheap
        rejected_by = cheap.failed_at.gate if cheap.failed_at else "a cheap gate"
        evidence_note = (
            f"not built: {rejected_by} rejected the candidate first, so its code was never executed"
        )
    else:
        behavioural, evidence_note = _gates_with_evidence(config, verdict, workdir, now)
        result = PipelineResult(results=cheap.results + run_pipeline(behavioural, ctx).results)
        cohort_verdict = next(
            (g.verdict for g in behavioural if isinstance(g, G3Improvement) and g.verdict),
            None,
        )
        if cohort_verdict is not None and cohort_verdict.candidate.n and cohort_verdict.incumbent.n:
            candidate_score = cohort_verdict.candidate.mean
            incumbent_score = cohort_verdict.incumbent.mean

    ledger.append(
        config.paths.ledger_dir,
        kind=ledger.EventKind.GATED,
        at=now,
        proposal_id=proposal_id,
        summary=f"ran {', '.join(result.ran)}; passed={result.passed}",
        detail={
            "gates": [
                {"gate": r.gate, "outcome": r.outcome.value, "reason": r.reason}
                for r in result.results
            ],
            # NOT `security_event`. The REJECTED entry below carries that key,
            # and the digest counts one per entry carrying it — so a single
            # incident was reported to the owner as two (ADR 0074). The gate
            # results above already record which gate raised it.
            "security_gates": [r.gate for r in result.results if r.security_event],
            "evidence": evidence_note,
            # The memory records this proposal was grounded in. Dropped
            # before, so the audit trail could not answer "what did the
            # proposer read to justify this" after the fact (ADR 0075).
            #
            # Rendered to strings, not passed as objects: `ledger.append`
            # JSON-serialises `detail`, and `Citation` is a frozen dataclass.
            # Writing the objects raised `Object of type Citation is not JSON
            # serializable` and killed `aef loop cycle` outright — the fix for
            # a dropped audit trail broke the command it was auditing, and the
            # test defending the wire only asserted source text (ADR 0078).
            "grounded_in": [str(c) for c in proposal.grounded_in] if proposal else [],
            # Which proposer produced it, so a ledger read after the fact can
            # separate the model's candidates from the rule-based ones.
            "proposer": config.proposer if proposal else None,
            # Did this gate pass run candidates under the operator's own
            # harness login? (ADR 0181.) The opt-in is a widening of the
            # blast radius, and a widening that leaves no trace in the audit
            # trail is one nobody can audit — the same argument
            # `shadow.containment` records its non-default modes on.
            "live_model_calls": _live_model_calls(config),
        },
    )

    decision = decide(result, tier1_enabled=config.tier1_enabled)
    report = _render(config, head_ref, proposal_id, result, decision, proposal)

    if result.security_events:
        # Halt criterion 2: a proposal reaching for the judge is a category
        # signal about the proposer, not a fact about this one candidate.
        assessment = assess_halt(zone_violation=True)
        ledger.append(
            config.paths.ledger_dir,
            kind=ledger.EventKind.REJECTED,
            at=now,
            proposal_id=proposal_id,
            summary=decision.reason,
            detail={"security_event": True},
        )
        _halt(config, at=now, proposal_id=proposal_id, reasons=assessment.reasons)
        return GateRun(
            decision=decision,
            result=result,
            report=report,
            exit_code=EXIT_HALTED,
            halted=True,
        )

    if decision.disposition is Disposition.REJECT:
        ledger.append(
            config.paths.ledger_dir,
            kind=ledger.EventKind.REJECTED,
            at=now,
            proposal_id=proposal_id,
            summary=decision.reason,
        )
        # Halt criteria 3 and 4, read from a ledger that now includes this
        # run. `assess_halt` accepted both flags and NOTHING EVER COMPUTED
        # THEM — two of five criteria were dead parameters while the ledger
        # held the evidence all along (ADR 0074, fixed in ADR 0077).
        history = ledger.read(config.paths.ledger_dir)
        assessment = assess_halt(
            drift_exhausted_twice=_drift_exhausted_twice(history),
            consecutive_escalation_rejections=_consecutive_escalation_rejections(history),
        )
        if assessment.should_halt:
            _halt(config, at=now, proposal_id=proposal_id, reasons=assessment.reasons)
            return GateRun(
                decision=decision,
                result=result,
                report=report,
                exit_code=EXIT_HALTED,
                halted=True,
            )
        return GateRun(
            decision=decision,
            result=result,
            report=report,
            exit_code=EXIT_REJECTED,
            candidate_score=candidate_score,
            incumbent_score=incumbent_score,
        )

    if decision.disposition is Disposition.ESCALATE:
        ledger.append(
            config.paths.ledger_dir,
            kind=ledger.EventKind.ESCALATED,
            at=now,
            proposal_id=proposal_id,
            summary=decision.reason,
            detail={"question": decision.question},
        )
        # Exit 0: an escalation is a successful run that produced a question,
        # not a failure. A non-zero exit here would train whoever reads CI to
        # treat "needs your decision" as "broken".
        return GateRun(
            decision=decision,
            result=result,
            report=report,
            exit_code=EXIT_OK,
            candidate_score=candidate_score,
            incumbent_score=incumbent_score,
        )

    # AUTO_MERGE — unreachable while tier1_enabled is False.
    files = {
        entry.path: config.repo.run_bytes("show", f"{verdict.diff.head_sha}:{entry.path}")
        for entry in verdict.diff.entries
        if not entry.is_deletion
    }
    archived = archive.record(
        config.paths.archive_dir,
        config.graph_id,
        files=files,
        base_sha=verdict.diff.base_sha,
        head_sha=verdict.diff.head_sha,
        recorded_at=now,
        gate_report=tuple(f"{r.gate}: {r.reason}" for r in result.results),
    )
    ledger.append(
        config.paths.ledger_dir,
        kind=ledger.EventKind.MERGED,
        at=now,
        proposal_id=proposal_id,
        summary=decision.reason,
        # No baseline_pass_rate is written, deliberately. Nothing in the harness
        # measures a live pass rate, and writing a literal 1.0 asserted that
        # production was perfect before every merge — so any agent below 95%
        # live read as REGRESSED, which halts the loop permanently with a
        # message blaming the gates (ADR 0072). Absence now means unmeasured.
        detail={"archive_version": archived.version},
    )
    return GateRun(
        decision=decision,
        result=result,
        report=report,
        exit_code=EXIT_OK,
        candidate_score=candidate_score,
        incumbent_score=incumbent_score,
    )


def _render(
    config: LoopConfig,
    head_ref: str,
    proposal_id: str,
    result: PipelineResult,
    decision: Decision,
    proposal: Proposal | None = None,
) -> str:
    """Render a report for a candidate BRANCH.

    `gate` judges a branch, which may not have come from this repo's proposer
    at all, so there is no `Proposal` object to render. The diff is taken
    from git rather than left as a placeholder — a report whose evidence
    section says "see the diff elsewhere" is the rubber stamp M9's ordering
    was designed to prevent, just with an extra step.
    """
    try:
        diff = config.repo.run("diff", f"{config.base_ref}...{head_ref}")
    except Exception:  # noqa: BLE001 - a missing diff must not lose the report
        diff = f"(could not render a diff for {proposal_id})\n"

    # A branch that came from this repo's proposer HAS a rationale and
    # citations; rendering the stub for it reported a memory-grounded
    # proposal as "none (control-cohort member)" in the owner's own review
    # report — the audit trail contradicting the thing it audits (ADR 0075).
    stub = Proposal(
        id=proposal_id,
        path=proposal.path if proposal else "(candidate branch)",
        original="",
        proposed=diff or "(empty diff)\n",
        rationale=proposal.rationale if proposal else f"candidate branch {proposal_id}",
        grounded_in=proposal.grounded_in if proposal else (),
        # Only the stub is exempt from the grounding requirement. A real
        # proposal already satisfied it at construction.
        is_control=proposal.is_control if proposal else True,
    )
    report = render_report(stub, result, decision)
    # render_report emits a unified diff of original->proposed; here the
    # "proposed" content IS already a diff, so present it directly.
    marker = "## The change itself"
    head, _, _ = report.partition(marker)
    return f"{head}{marker}\n\n```diff\n{diff.rstrip()}\n```\n"


# --------------------------------------------------------------------------
# monitor
# --------------------------------------------------------------------------


def load_observations(path: Path) -> tuple[Observation, ...]:
    """`{"at": iso, "passed": bool, "cost_tokens": int}` per line.

    **Nothing in this repo writes this file.** A deployment running agents in
    production emits it; the loop only reads it. Recorded plainly because a
    monitoring system with no input silently reports every window as
    unobserved — which, correctly, rolls everything back.
    """
    if not path.is_file():
        return ()
    out: list[Observation] = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            out.append(
                Observation(
                    at=datetime.fromisoformat(payload["at"]),
                    passed=bool(payload["passed"]),
                    cost_tokens=int(payload.get("cost_tokens", 0)),
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}:{number}: malformed observation: {exc}") from exc
    return tuple(out)


@dataclass(frozen=True)
class MonitorRun:
    checked: int = 0
    rolled_back: tuple[str, ...] = ()
    halted: bool = False
    halt_reasons: tuple[str, ...] = ()
    lines: tuple[str, ...] = ()
    exit_code: int = EXIT_OK


def monitor(config: LoopConfig, *, now: datetime, restore_to: Path | None = None) -> MonitorRun:
    entries = _preflight(config)
    observations = load_observations(config.paths.observations)

    # BLESSED entries are baselines, not merges: they have no predecessor to
    # roll back to. Filtering on kind alone would be enough today; the
    # `blessed` detail check is belt-and-braces against a mislabelled entry.
    merges = [
        e for e in entries if e.kind is ledger.EventKind.MERGED and not e.detail.get("blessed")
    ]
    rolled_back_versions = {
        e.detail.get("archive_version") for e in entries if e.kind is ledger.EventKind.ROLLED_BACK
    }

    lines: list[str] = []
    rolled: list[str] = []
    gated_rollback = False

    # NEWEST FIRST. `archive.rollback(v)` APPENDS v's content as a new
    # version, so reverting in ledger order made each rollback undo the
    # previous one: with v2 and v3 both un-settled, reverting v2 restored the
    # baseline and reverting v3 then restored v2 — reinstating the first
    # regression, having reported both as rolled back. Reverting newest-first
    # unwinds the stack in the order it was built (ADR 0084).
    for merge in sorted(
        merges, key=lambda e: int(e.detail.get("archive_version") or 0), reverse=True
    ):
        version = merge.detail.get("archive_version")
        if version is None or version in rolled_back_versions:
            continue
        measured = merge.detail.get("baseline_pass_rate")
        if measured is None:
            # Unmeasured, so nothing can be concluded about regression. Report
            # it as such: AMBIGUOUS still rolls back (rollback-by-default,
            # ADR 0056) but does NOT halt the loop, because there is no
            # evidence the gates missed anything.
            lines.append(
                f"{merge.proposal_id} (v{version}): no measured pre-merge baseline, so "
                f"regression cannot be judged — reverting without blaming the gates"
            )
            _rollback_merge(config, merge, int(version), now, restore_to, "no measured baseline")
            rolled.append(merge.proposal_id)
            continue

        result = evaluate_window(
            observations,
            baseline_pass_rate=float(measured),
            merged_at=merge.at,
            now=now,
            policy=config.monitor_policy,
        )
        lines.append(f"{merge.proposal_id} (v{version}): {result.verdict.value} — {result.reason}")

        if result.action is not Action.ROLLBACK:
            continue

        _rollback_merge(config, merge, int(version), now, restore_to, result.reason)
        rolled.append(merge.proposal_id)
        if result.verdict is Verdict.REGRESSED:
            gated_rollback = True

    assessment = assess_halt(gated_rollback=gated_rollback)
    if assessment.should_halt:
        _halt(config, at=now, proposal_id="(monitor)", reasons=assessment.reasons)

    return MonitorRun(
        checked=len(merges),
        rolled_back=tuple(rolled),
        halted=assessment.should_halt,
        halt_reasons=assessment.reasons,
        lines=tuple(lines),
        exit_code=EXIT_HALTED if assessment.should_halt else EXIT_OK,
    )


# --------------------------------------------------------------------------
# digest / status
# --------------------------------------------------------------------------


def _rollback_merge(
    config: LoopConfig,
    merge: ledger.LedgerEntry,
    version: int,
    now: datetime,
    restore_to: Path | None,
    reason: str,
) -> None:
    """Restore the state that preceded `version`, not `version` itself.

    `archive.rollback(v)` restores v's CONTENT. Passing the version the merge
    *produced* therefore restored the regressing change — the rollback
    reinstated exactly what it was reverting (ADR 0072). The target is the
    version before it.
    """
    target = version - 1
    existing = archive.versions(config.paths.archive_dir, config.graph_id)
    if target not in existing:
        # Refuse rather than restore the wrong thing. A rollback with no
        # predecessor to return to is a gap in the archive, not a licence to
        # reinstate the change being reverted.
        raise archive.ArchiveError(
            f"cannot roll back {merge.proposal_id}: no archived version {target} preceding "
            f"v{version} for graph {config.graph_id!r}. Archive a blessed baseline before "
            f"enabling any path that merges."
        )

    archive.rollback(
        config.paths.archive_dir,
        config.graph_id,
        target,
        restore_to or (config.paths.root / "restored"),
        recorded_at=now,
        notes=reason,
    )
    ledger.append(
        config.paths.ledger_dir,
        kind=ledger.EventKind.ROLLED_BACK,
        at=now,
        proposal_id=merge.proposal_id,
        summary=reason,
        detail={"archive_version": version, "restored_version": target},
    )


def digest(
    config: LoopConfig,
    *,
    since: datetime,
    until: datetime,
    owner_edits: int = 0,
    halt_channel_configured: bool = False,
    runs_recorded: int = 0,
) -> Digest:
    # Deliberately NOT behind the kill switch: reading the record of why the
    # loop halted is exactly what you want to do while it is halted.
    entries = ledger.read(config.paths.ledger_dir)
    # THE SAME resolver `_halt` uses (ADR 0195). The caller's flag is OR-ed in
    # rather than overriding, because `aef loop digest` passes the webhook it
    # read from the environment and the config block is a second channel, not
    # a competing answer.
    channel = _halt_channel(config)
    return build_digest(
        entries,
        since=since,
        until=until,
        owner_edits=owner_edits,
        halt_channel_configured=halt_channel_configured or channel is not None,
        halt_channel=(
            channel.description
            if channel is not None
            else ("webhook (AEF_HALT_WEBHOOK)" if halt_channel_configured else "")
        ),
        runs_recorded=runs_recorded,
    )


@dataclass(frozen=True)
class Status:
    halted: bool
    halt_reason: str
    ledger_entries: int
    ledger_ok: bool
    ledger_error: str
    archived_versions: tuple[int, ...]
    open_merges: int

    def render(self) -> str:
        lines = [
            f"kill switch : {'ENGAGED — ' + self.halt_reason if self.halted else 'clear'}",
            f"ledger      : {self.ledger_entries} entries, "
            + ("chain verified" if self.ledger_ok else f"BROKEN — {self.ledger_error}"),
            f"archive     : {len(self.archived_versions)} version(s)",
            f"open windows: {self.open_merges} merge(s) not yet settled or rolled back",
        ]
        return "\n".join(lines)


def status(config: LoopConfig) -> Status:
    """Reports rather than raises — `status` is what you run when something
    is wrong, so it must work when the ledger is broken and the loop halted."""
    _check_state_is_outside_the_repo(config)
    switch = config.paths.kill_switch
    entries: tuple[ledger.LedgerEntry, ...] = ()
    ledger_ok, ledger_error = True, ""
    try:
        entries = ledger.read(config.paths.ledger_dir)
    except ledger.LedgerError as exc:
        ledger_ok, ledger_error = False, str(exc)

    merged = {e.detail.get("archive_version") for e in entries if e.kind is ledger.EventKind.MERGED}
    rolled = {
        e.detail.get("archive_version") for e in entries if e.kind is ledger.EventKind.ROLLED_BACK
    }

    return Status(
        halted=switch.engaged,
        halt_reason=switch.reason,
        ledger_entries=len(entries),
        ledger_ok=ledger_ok,
        ledger_error=ledger_error,
        archived_versions=archive.versions(config.paths.archive_dir, config.graph_id),
        open_merges=len(merged - rolled),
    )


def default_digest_window(now: datetime) -> tuple[datetime, datetime]:
    """Q-A4 owner default: weekly."""
    return now - timedelta(days=7), now


def observations_payload(observations: tuple[Observation, ...]) -> list[dict[str, Any]]:
    return [
        {"at": o.at.isoformat(), "passed": o.passed, "cost_tokens": o.cost_tokens}
        for o in observations
    ]


# --------------------------------------------------------------------------
# cycle — the whole loop, once
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateAttempt:
    """One candidate this turn built and gated, winner or not (ADR 0200).

    Every field is what the GATES concluded about that candidate on its own
    pass. There is no aggregate verdict over a set anywhere in this record,
    because there is no aggregate gate pass: N candidates means N independent
    passes, and this is one row of the ledger's own account of them.
    """

    proposal_id: str
    branch: str
    disposition: str | None
    score: float | None
    incumbent_score: float | None
    passed: bool
    exit_code: int
    reason: str


@dataclass(frozen=True)
class CycleRun:
    harvested: tuple[str, ...] = ()
    proposed: str | None = None
    decision: Decision | None = None
    lines: tuple[str, ...] = ()
    exit_code: int = EXIT_OK
    score: float | None = None  # G3 candidate mean, when the behavioural gates ran
    incumbent_score: float | None = None
    # Live model calls this turn's PROPOSER spent, whether or not it produced
    # anything. Zero for every proposer that asks no model. It is not a gate
    # cost and not a scoring cost — those are the corpus passes — it is the
    # cost of asking for a candidate (ADR 0170).
    proposer_calls: int = 0
    # Every candidate this turn tried, in the order it tried them, WINNER
    # INCLUDED (ADR 0200). One entry when `candidates_per_turn` is 1, which is
    # the default and is what a turn always did.
    attempts: tuple[CandidateAttempt, ...] = ()
    # The held-out read of the winner, when a slice was held back and the
    # winner passed the gates. Advisory: it changes no disposition.
    audit: AuditComparison | None = None

    @property
    def losers(self) -> tuple[CandidateAttempt, ...]:
        """The candidates that were gated and not chosen — stepping stones the
        turn produced and the caller must not mistake for un-gated ideas."""
        return tuple(a for a in self.attempts if a.proposal_id != self.proposed)


def cycle(
    config: LoopConfig,
    *,
    now: datetime,
    workdir: Path,
    runs_dir: Path | None = None,
    corpus_root: Path | None = None,
    graph: Any = None,
    memory: Any = None,
    agent_path: str = DEFAULT_AGENT_PATH,
) -> CycleRun:
    """One turn of the loop: harvest -> propose -> gate -> record.

    **Nothing is pushed and nothing is merged.** The candidate branch is
    created in the local checkout only, which needs no repository permission
    at all — a job with `contents: read` can do it. Pushing is what needs
    write access, and the gate job must never have it (ADR 0057).

    **`config.candidates_per_turn` candidates per turn, default 1.** A loop
    that can emit many per cycle can exhaust the rate budget in a single run,
    and every candidate costs cohort+2 corpus passes to gate — so the number
    is an owner's decision with a measured price, not a default (ADR 0200).
    Above 1, each candidate gets its own branch, its own workdir and its own
    independent gate pass; the turn then keeps the best that passed, and the
    ledger records every one of them.
    """
    # FIRST, for the reason ADR 0187's F-M8-1 records: `path_exists_at` cannot
    # tell an absent FILE from an absent REF, so the "no agent source at
    # <persona> in <ref>" line 60 lines down blamed a persona that was present
    # and this function returned a `CycleRun` at exit 0 — on a real repo whose
    # default branch is `azure-agent/uptime-monitoring`, reached from the
    # documented defaults. Ahead of `_preflight` so that no message about the
    # kill switch, the ledger or the corpus can arrive first and be believed.
    require_base_ref(config.repo, config.base_ref)
    entries = _preflight(config)  # kill switch, then ledger chain — in that order
    lines: list[str] = [f"ledger verified: {len(entries)} entr(ies)"]

    harvested: tuple[str, ...] = ()
    if runs_dir is not None and corpus_root is not None and graph is not None:
        from aef.harness.harvest import harvest as _harvest

        outcome = _harvest(runs_dir, corpus_root, graph, now=now)
        harvested = outcome.promoted
        lines.extend(outcome.lines)

    if memory is None:
        lines.append("no memory store configured: nothing to learn from, no candidate")
        return CycleRun(harvested=harvested, lines=tuple(lines))

    from aef.harness.proposer import MemoryEvidence

    # `graph_id=config.evidence_id` — the filter is applied ONCE, here, where
    # the evidence is assembled, and therefore for every proposer. It used to
    # be `RuleBasedPromptProposer`'s alone, so `--graph-id demo_agent` with
    # the default `rule_based` proposer grounded a change to
    # `agents/demo/graph.py` in three `summary_agent` records (reproduced,
    # ADR 0191's F2). `evidence_id`, not `graph_id`, for ADR 0182's reason:
    # this is the `Graph.id` namespace, not the archive key.
    # The audit slice is subtracted from the proposer's evidence too (ADR
    # 0200), not only from what the gates score. A lesson learned from a
    # scenario that is about to judge the candidate is that scenario reaching
    # the proposer by proxy — the same leak `MemoryEvidence` already refuses
    # for validation and holdout records, and it is refused here by the same
    # mechanism rather than by a second one. Empty by default.
    held_for_evidence = _audit_slice_for(config, now)
    if not held_for_evidence.is_empty:
        lines.append(held_for_evidence.describe())
    evidence = MemoryEvidence.from_store(
        memory,
        _corpus_with_audit_held_out(config, held_for_evidence),
        graph_id=config.evidence_id,
    )
    if evidence.excluded:
        lines.append(
            f"{len(evidence.excluded)} memory record(s) excluded as validation/holdout-derived"
        )
    if evidence.foreign:
        lines.append(
            f"{len(evidence.foreign)} memory record(s) excluded as belonging to a graph other "
            f"than {config.evidence_id!r}"
        )
    if not evidence.records:
        # The proposer does not speculate. No recorded failures means no
        # hypothesis, which is a legitimate outcome and not an error.
        #
        # WHICH emptiness, when the answer is known: an operator whose store
        # is full and whose cycle proposes nothing needs to be told that the
        # records were another graph's, or the remedy ("record failures") is
        # the opposite of the right one ("point --graph-id at the graph those
        # runs came from"). `cmd_cycle` journals `lines[-1]` as the verdict,
        # so it has to be on THIS line and not a note above it (ADR 0165).
        detail = (
            f": all {len(evidence.foreign)} record(s) came from scenarios of another graph, "
            f"not {config.evidence_id!r}"
            if evidence.foreign
            else ""
        )
        lines.append(f"no admissible failure memory{detail}: no candidate this cycle")
        return CycleRun(harvested=harvested, lines=tuple(lines))

    # The BASE REF's source, not the working tree's. The candidate branch is
    # built from `base_ref` and the diff is taken against it, so proposing
    # from whatever happens to be checked out laundered every un-proposed
    # working-tree change into the candidate: the rationale said "raising
    # RETRY_BUDGET from 3 to 4" while the diff G0 sized and scanned also
    # carried an `import os` nobody had reasoned about. Reading from the same
    # ref the diff is against makes the artefact judged the artefact proposed
    # (ADR 0078).
    # `_preflight` has already established that the REF exists, so a False
    # here can only mean the FILE is missing at it — which is what the
    # sentence says. Asserted rather than assumed, because the two questions
    # collapsing into this one line is exactly F-M8-1 (ADR 0187 / 0189).
    # And a REFUSAL, not a verdict. ADR 0189 got the sentence right and left
    # the disposition wrong: a ref that exists without the file in it is a
    # configuration error — `--agent-path` names something that is not there —
    # and returning `no candidate` at exit 0 makes it indistinguishable from
    # the honest "nothing to propose". This repo's OWN nightly cycle was in
    # exactly that state: `DEFAULT_AGENT_PATH` is `agents/migrated/graph.py`,
    # which is what `aef migrate` writes into an ADOPTING repo (ADR 0149) and
    # which aef-core does not have; the workflow passed no `--agent-path`; and
    # with a non-empty memory the cycle printed
    #
    #     no agent source at agents/migrated/graph.py in main
    #     (the ref exists; the file is not in it): no candidate
    #
    # and exited 0, one line further down the same road ADR 0188 walked
    # (reproduced, ADR 0191's F6). The workflow now passes
    # `--agent-path agents/demo/graph.py`; this makes the state that produced
    # that line impossible to mistake for health from any caller.
    if not config.repo.path_exists_at(config.base_ref, agent_path):
        raise AgentSourceMissingError(
            f"no agent source at {agent_path} in {config.base_ref} (the ref exists; the file "
            f"is not in it), so there is nothing for the proposer to edit. This is the "
            f"invocation, not a verdict: pass --agent-path naming a graph that exists at "
            f"{config.base_ref}." + _graph_files_at(config, agent_path)
        )

    source = config.repo.show(config.base_ref, agent_path)
    proposer = _build_proposer(config)
    proposals = proposer.propose_from_memory(
        evidence,
        proposal_id=f"cycle-{now:%Y%m%dT%H%M%S}",
        path=agent_path,
        source=source,
    )
    spend = _proposer_spend(proposer)
    if not proposals:
        # WHY it produced nothing, when the proposer can say. "produced
        # nothing from the available evidence" is true of a two-record store
        # below the recurrence threshold, of a lesson already in the prompt,
        # and of a proposer pointed at a file it cannot edit — three different
        # operator actions behind one sentence (ADR 0157).
        #
        # The spend note is appended to THIS line rather than added as its
        # own, because `cmd_cycle` journals `lines[-1]` as the verdict when
        # nothing was proposed (ADR 0165) — so a call spent on a discarded
        # reply reaches `cycles.jsonl` instead of only the terminal.
        lines.append(
            "the proposer produced nothing from the available evidence"
            + _no_proposal_reason(proposer, evidence, agent_path, source)
            + (f" [{spend}]" if spend else "")
        )
        return CycleRun(
            harvested=harvested,
            lines=tuple(lines),
            proposer_calls=_proposer_calls(proposer),
        )

    # HOW MANY of what the proposer offered this turn will actually be built
    # and gated (ADR 0200). `proposals[0]` was here, with the comment "at most
    # one candidate per cycle, deliberately" — and the rest were dropped
    # unmeasured: on the fixture in `docs/research/night-1/01-repro-a.txt` the
    # rule-based proposer offered THREE and the turn created ONE branch.
    #
    # The default is still 1, so nothing moves for a caller that has not asked
    # for more. Above 1 the turn gates each candidate on its OWN pass and
    # keeps the best that passed; the gates' verdict logic is untouched and
    # never sees a set.
    chosen = _candidates_for_this_turn(proposals, config.candidates_per_turn)
    if len(chosen) > 1:
        lines.append(
            f"{len(chosen)} candidate(s) this turn (of {len(proposals)} offered); each is "
            f"gated on its own pass, costing {config.cohort_size + 2} corpus pass(es) each"
        )
    if spend:
        lines.append(spend)

    attempts: list[CandidateAttempt] = []
    runs: list[GateRun] = []
    halted = False
    for index, proposal in enumerate(chosen, start=1):
        branch = f"loop/{proposal.id}"
        _materialise_candidate_branch(config, branch, agent_path, proposal.proposed)
        prefix = f"candidate {index}/{len(chosen)}: " if len(chosen) > 1 else ""
        lines.append(
            f"{prefix}proposed {proposal.id} on local branch {branch} (never pushed; "
            f"proposer={config.proposer})"
        )
        if "[llm proposer fell back" in proposal.rationale:
            lines.append(proposal.rationale[proposal.rationale.index("[llm proposer fell back") :])
        # A scratch dir PER CANDIDATE when there is more than one, for exactly
        # the reason `run_loop` gives one per turn: G1 materialises into
        # `workdir/workspace` and `trust._prepare_empty_destination` refuses a
        # non-empty one, so a shared workdir would reject candidate 2 with a
        # TrustBoundaryError before any behavioural gate ran (ADR 0122's
        # defect, one level down). Unchanged at N=1 so no existing path moves.
        one = config.candidates_per_turn == 1
        candidate_workdir = workdir if one else workdir / f"cand-{index}"
        run = gate(config, branch, now=now, workdir=candidate_workdir, proposal=proposal)
        runs.append(run)
        passed = run.decision.disposition in (Disposition.ESCALATE, Disposition.AUTO_MERGE)
        attempts.append(
            CandidateAttempt(
                proposal_id=proposal.id,
                branch=branch,
                disposition=run.decision.disposition.value,
                score=run.candidate_score,
                incumbent_score=run.incumbent_score,
                passed=passed and not run.halted,
                exit_code=run.exit_code,
                reason=run.decision.reason,
            )
        )
        lines.append(f"{prefix}gated: {run.decision.disposition.value} — {run.decision.reason}")
        if run.exit_code == EXIT_HALTED:
            # A halt is the loop stopping, not a candidate to skip past. The
            # remaining candidates are not built and not gated.
            halted = True
            if index < len(chosen):
                lines.append(
                    f"halted at candidate {index}/{len(chosen)}; the remaining "
                    f"{len(chosen) - index} were not built"
                )
            break

    best = _best_attempt(attempts)
    winner, won = attempts[best], runs[best]
    if len(attempts) > 1:
        lines.append(
            f"kept the best of {len(attempts)} gated candidate(s): {winner.proposal_id} "
            f"({winner.disposition}, score {winner.score})"
        )
        ledger.append(
            config.paths.ledger_dir,
            kind=ledger.EventKind.CANDIDATES,
            at=now,
            proposal_id=winner.proposal_id,
            summary=(
                f"{len(attempts)} candidate(s) gated this turn; kept {winner.proposal_id} "
                f"({winner.disposition})"
            ),
            detail={
                "offered": len(proposals),
                "gated": len(attempts),
                "chosen": winner.proposal_id,
                "corpus_passes_each": config.cohort_size + 2,
                "candidates": [
                    {
                        "proposal_id": a.proposal_id,
                        "branch": a.branch,
                        "disposition": a.disposition,
                        "score": a.score,
                        "passed": a.passed,
                        "reason": a.reason,
                    }
                    for a in attempts
                ],
            },
        )

    audit: AuditComparison | None = None
    held = _audit_slice_for(config, now)
    if not held.is_empty and winner.passed and not halted:
        # Only a candidate the GATES passed is worth a held-out read: an audit
        # of something already rejected spends the slice to confirm a verdict
        # that has been reached.
        audit = _run_audit(
            config,
            inspect_candidate(config.repo, config.base_ref, winner.branch, config.zone_policy),
            held=held,
            workdir=workdir,
        )
        lines.append(audit.line())
        ledger.append(
            config.paths.ledger_dir,
            kind=ledger.EventKind.AUDIT,
            at=now,
            proposal_id=winner.proposal_id,
            summary=audit.line(),
            detail={
                "rotation_key": audit.rotation_key,
                "scenarios": list(audit.ids),
                "candidate_mean": audit.candidate_mean,
                "incumbent_mean": audit.incumbent_mean,
                "regressed": list(audit.regressed),
                "note": audit.note,
                # Said in the entry itself, because a reader a month later
                # must not have to infer it: this read did not gate.
                "advisory": True,
            },
        )
    elif not held.is_empty:
        lines.append(
            f"audit ({held.rotation_key}): not read — "
            + ("the turn halted" if halted else "no candidate passed the gates")
        )

    return CycleRun(
        harvested=harvested,
        proposed=winner.proposal_id,
        decision=won.decision,
        lines=tuple(lines),
        exit_code=won.exit_code,
        score=won.candidate_score,
        incumbent_score=won.incumbent_score,
        proposer_calls=_proposer_calls(proposer),
        attempts=tuple(attempts),
        audit=audit,
    )


def _candidates_for_this_turn(proposals: tuple[Proposal, ...], wanted: int) -> tuple[Proposal, ...]:
    """The first `wanted` DISTINCT proposals, in the proposer's own order.

    Distinct by proposed content: two proposals that would produce the same
    tree cost two full gate passes and can only reach the same verdict, and
    `run_loop` already treats a repeated tree as the proposer having nothing
    new to say.
    """
    seen: set[str] = set()
    picked: list[Proposal] = []
    for proposal in proposals:
        if proposal.proposed in seen:
            continue
        seen.add(proposal.proposed)
        picked.append(proposal)
        if len(picked) == wanted:
            break
    return tuple(picked)


def _best_attempt(attempts: list[CandidateAttempt]) -> int:
    """Index of the candidate the turn keeps: the highest-scoring one that PASSED.

    With nothing passing, the FIRST attempt — which is what a one-candidate
    turn has always returned, so the N=1 path is unchanged by construction.
    Ties break on order, so the choice is deterministic and re-derivable from
    the ledger's own roster.
    """
    passed = [(i, a) for i, a in enumerate(attempts) if a.passed]
    if not passed:
        return 0
    # `if ... is not None`, not `or`: a legitimate score of 0.0 is not
    # "unscored", and `score or -1.0` would rank it below an unscored pass.
    return max(
        passed,
        key=lambda pair: (pair[1].score if pair[1].score is not None else -1.0, -pair[0]),
    )[0]


def _proposer_spend(proposer: Any) -> str:
    """What the proposer spent this turn, in one line, or "".

    Read with `getattr` for the same reason `no_proposal_reason` is (ADR
    0157): the two proposers that make no model call have nothing to report
    and must not be made to grow a field to say so.
    """
    spend = getattr(proposer, "spend", None)
    note = getattr(spend, "note", None)
    return str(note()) if callable(note) else ""


def _proposer_calls(proposer: Any) -> int:
    spend = getattr(proposer, "spend", None)
    calls = getattr(spend, "calls", 0)
    return int(calls) if isinstance(calls, int) else 0


def _graph_files_at(config: LoopConfig, missing: str) -> str:
    """ " Try these:" plus the graph files that DO exist at the base ref.

    Read from the ref with `git ls-tree`, never from the working tree, for
    the same reason every other question this function's caller asks is: the
    candidate is built from the ref, so the ref is what an operator has to
    point `--agent-path` into. Silent when there is nothing to suggest —
    a list of nothing is worse than no list.
    """
    root = config.zone_policy.agent_root or DEFAULT_AGENT_ROOT
    found = sorted(
        p
        for p in config.repo.list_tree(config.base_ref, root)
        if p.endswith(".py") and p != missing
    )
    if not found:
        return f" Nothing under {root}/ at {config.base_ref} is a Python file."
    return f" Python files under {root}/ at {config.base_ref}: {', '.join(found)}."


def _no_proposal_reason(proposer: Any, evidence: Any, agent_path: str, source: str) -> str:
    """The proposer's own account of an empty result, when it has one.

    An optional hook, read with `getattr`, because the two older proposers
    predate it and a protocol change would be a change to the file that
    defines what a proposal IS. A proposer that cannot explain itself gets the
    generic sentence, plus the one hint that is computable here: a non-Python
    agent path is a prompt file, and `rule_based`/`llm` have no operation to
    perform on one (ADR 0157).
    """
    explain = getattr(proposer, "no_proposal_reason", None)
    if callable(explain):
        reason = explain(evidence, path=agent_path, source=source)
        return f": {reason}" if reason else ""
    if not agent_path.endswith(".py"):
        return (
            f": {agent_path} is not Python, and this proposer edits numeric constants and "
            f"Python structure. A prompt file's proposer is --proposer rule_based_prompt."
        )
    return ""


def _build_proposer(config: LoopConfig) -> Any:
    """The proposer `config.proposer` names (ADR 0122). Both share
    `propose_from_memory(evidence, *, proposal_id, path, source)`; the LLM
    one takes the zone policy and G0's line budget from the same config the
    gates read, so it refuses what they would refuse."""
    from aef.harness.proposer import RuleBasedProposer

    if config.proposer == "rule_based_prompt":
        from aef.harness.prompt_proposer import RuleBasedPromptProposer

        # The zone policy, graph id and corpus come from the same config the
        # gates read, so the proposer refuses the paths G0 would refuse and
        # learns only from this graph's own runs.
        #
        # `evidence_id`, not `graph_id`: this is the ONE reader of the
        # `Graph.id` namespace, and separating it from the archive key is what
        # lets `aef loop cycle` derive the id from its own corpus without
        # moving the key a baseline was blessed under (ADR 0125's two
        # namespaces, ADR 0176's F2, split in ADR 0182). It defaults TO
        # `graph_id`, so a caller that sets only the old field is unchanged.
        return RuleBasedPromptProposer(
            zone_policy=config.zone_policy,
            graph_id=config.evidence_id,
            corpus=config.corpus,
        )
    if config.proposer != "llm":
        return RuleBasedProposer()
    from aef.harness.gates.g0_static_safety import DEFAULT_MAX_CHANGED_LINES
    from aef.harness.llm_proposer import LLMProposer

    assert config.proposer_provider is not None and config.proposer_model  # __post_init__
    return LLMProposer(
        provider=config.proposer_provider,
        model=config.proposer_model,
        zone_policy=config.zone_policy,
        max_changed_lines=int(
            config.gate_limits.get("max_changed_lines", DEFAULT_MAX_CHANGED_LINES)
        ),
    )


@dataclass(frozen=True)
class LoopTurn:
    turn: int
    proposed: str | None
    disposition: Disposition | None
    kept: bool
    kept_ref: str  # the kept branch's commit after this turn
    parent_ref: str = ""  # what this turn proposed FROM
    score: float | None = None
    duplicate: bool = False  # tree already in the archive; neither kept nor reverted


@dataclass
class ArchiveMember:
    """One candidate, DGM-style (ADR 0121): its score, its parent, and how
    many children have been proposed from it. Children count is the novelty
    term — a member that has spawned many attempts is less worth sampling
    again than one that has spawned none.

    Since ADR 0160 a REJECTED candidate is a member too (`kept=False`, with
    the gate verdict in `disposition`). DGM's archive keeps stepping stones,
    and a candidate the gates turned down is the canonical stepping stone: it
    is a place in the search space that was reached and measured. What it is
    not is a place the kept branch may point at, or a duplicate — see
    `_latest_kept` and the duplicate check in `run_loop`.
    """

    ref: str
    score: float | None
    parent_ref: str | None
    children: int = 0
    tree: str = ""
    kept: bool = True
    disposition: str | None = None
    # True when this member was loaded from a previous invocation's lineage
    # file rather than produced by this run. Reported, not acted on.
    resumed: bool = False


@dataclass(frozen=True)
class LoopRun:
    """What `run_loop` did: the autoresearch shape inside the gates (ADR 0114),
    with DGM's archive when `sample_parents` is on (ADR 0121)."""

    turns: tuple[LoopTurn, ...]
    kept_branch: str
    kept_ref: str
    stopped_because: str
    lines: tuple[str, ...]
    archive: tuple[ArchiveMember, ...] = ()

    @property
    def resumed_members(self) -> int:
        """Members this run inherited from a previous invocation's lineage."""
        return sum(1 for m in self.archive if m.resumed)

    @property
    def kept_count(self) -> int:
        return sum(1 for t in self.turns if t.kept)

    @property
    def reverted_count(self) -> int:
        return sum(
            1 for t in self.turns if t.proposed is not None and not t.kept and not t.duplicate
        )

    @property
    def distinct_kept_trees(self) -> int:
        """Distinct trees among the KEPT non-root members. `m.kept` is not
        redundant since ADR 0160 put rejected candidates in the archive: drop
        it and every rejection inflates the diversity number this A/B is
        decided on."""
        return len({m.tree for m in self.archive if m.parent_ref is not None and m.kept})

    @property
    def distinct_gated_trees(self) -> int:
        """Distinct trees among everything gated, kept or not — the size of
        the search actually explored, which is not the same number."""
        return len({m.tree for m in self.archive if m.parent_ref is not None})


class KeptBranchCheckedOutError(RuntimeError):
    """`run_loop` was started while HEAD is the branch it advances.

    The loop moves the kept branch with `update-ref`, which changes where the
    branch points WITHOUT touching the index or the working tree. When that
    branch is also HEAD, the result is a repository whose index disagrees
    with its own HEAD commit: `git status` reads as a staged reversal of the
    change the loop just kept, and the next `git commit` in that checkout
    would undo it. Reproduced, not argued (ADR 0125).

    `update-ref` is deliberate — `merge`/`reset` on the checked-out branch is
    the loop touching a person's working tree, which ADR 0114 refuses. So the
    refusal is the fix: the reviewer stands somewhere else.
    """


def _refuse_if_kept_branch_is_checked_out(config: LoopConfig, kept_branch: str) -> None:
    head = config.repo.run("rev-parse", "--abbrev-ref", "HEAD").strip()
    if head != kept_branch:
        return
    raise KeptBranchCheckedOutError(
        f"HEAD is {kept_branch!r}, the branch this loop advances with update-ref — "
        f"which would leave your index reading as a staged reversal of whatever the "
        f"loop keeps. Check out another branch first "
        f"(e.g. `git checkout {config.base_ref}`) and re-run; {kept_branch} is for "
        f"reviewing, and `git log {kept_branch}` reads it without standing on it."
    )


def _ensure_branch(config: LoopConfig, branch: str) -> str:
    try:
        return config.repo.rev_parse(branch)
    except Exception:  # noqa: BLE001 - "no such ref" is the only thing we act on
        config.repo.run("branch", branch, config.base_ref)
        return config.repo.rev_parse(branch)


def _tree_of(config: LoopConfig, ref: str) -> str:
    return config.repo.run("rev-parse", f"{ref}^{{tree}}").strip()


def _parent_weight(member: ArchiveMember) -> float:
    """DGM's sampling rule in miniature: a sigmoid of the score, scaled by
    1/(1+children). A member with no score yet (the root, before anything
    was gated against it) weighs as a 0.5.

    **A rejected member with no score weighs zero** (ADR 0160). Rejected
    candidates are in the archive as stepping stones, and a stepping stone
    that reached G3 has a measured task metric — it is a real position in the
    search space and competes for parenthood on that number, which is the
    whole DGM claim. A candidate rejected by a CHEAP gate has no number:
    G0 refused its zone, G1 could not build it, G5 said it drifted too far.
    Giving it the root's 0.5 would let an unbuildable or out-of-zone tree
    outbid a measured one, and the fallback score would be a fabrication
    about a candidate nothing ever measured. So it is recorded and not
    sampleable, and the recording is the point: the run's account of where it
    went includes the places it could not stand.
    """
    if member.score is None and not member.kept:
        return 0.0
    score = 0.5 if member.score is None else member.score
    fitness = 1.0 / (1.0 + math.exp(-10.0 * (score - 0.5)))
    return fitness / (1.0 + member.children)


@dataclass(frozen=True)
class LineageEntry:
    """One persisted member as an owner reads it (ADR 0198).

    J0b's second deduction on dimension 6 was that the archive had *"no
    owner-facing `aef loop lineage list` — only a counts line"*. A store whose
    only surface is `archive: 9 member(s), 0 distinct kept tree(s)` cannot be
    used to answer the question the store exists for — *which* candidate came
    from which, and which of them the next turn may build on.

    `sampleable` is not re-derived here. It is `_parent_weight` — the function
    the sampler itself calls — plus the ref check `_resume_lineage` applies,
    because a listing that computed eligibility its own way would eventually
    disagree with the sampler and the listing would be the lie.
    """

    record: archive.LineageRecord
    weight: float
    ref_resolves: bool

    @property
    def sampleable(self) -> bool:
        return self.weight > 0.0 and self.ref_resolves

    @property
    def why_not(self) -> str:
        """Empty when it is sampleable; otherwise the reason, in the sampler's
        own terms rather than a restatement of the flag."""
        if self.sampleable:
            return ""
        if not self.ref_resolves:
            return "ref no longer resolves — history only"
        return "rejected before G3 scored it — recorded, never a parent"


def read_lineage_entries(
    lineage_dir: Path, graph_id: str, *, repo: GitRepo | None = None
) -> tuple[LineageEntry, ...]:
    """Every persisted member, folded, in the order the refs first appear.

    `repo` is optional so a listing still works against a state directory whose
    repository is elsewhere; without it every ref is reported as resolving,
    which is the honest default — the alternative is claiming a member is dead
    because nothing was available to look.
    """
    entries: list[LineageEntry] = []
    for record in archive.fold_lineage(archive.read_lineage(lineage_dir, graph_id)).values():
        member = ArchiveMember(
            ref=record.ref,
            score=record.score,
            parent_ref=record.parent_ref,
            children=record.children,
            tree=record.tree,
            kept=record.kept,
            disposition=record.disposition,
            resumed=True,
        )
        resolves = True
        if repo is not None:
            try:
                repo.rev_parse(record.ref)
            except Exception:  # noqa: BLE001 - "unknown revision" is the only thing we act on
                resolves = False
        entries.append(
            LineageEntry(record=record, weight=_parent_weight(member), ref_resolves=resolves)
        )
    return tuple(entries)


def _choose_parent(archive: list[ArchiveMember], rng: random.Random) -> ArchiveMember:
    weights = [_parent_weight(m) for m in archive]
    if not any(weights):  # pragma: no cover - the root always weighs > 0
        return [m for m in archive if m.kept][0]
    return rng.choices(archive, weights=weights, k=1)[0]


def _ref_resolves(config: LoopConfig, ref: str) -> bool:
    try:
        config.repo.rev_parse(ref)
    except Exception:  # noqa: BLE001 - "unknown revision" is the only thing we act on
        return False
    return True


def _resume_lineage(
    config: LoopConfig, members: list[ArchiveMember], root: ArchiveMember
) -> tuple[int, int, set[str], set[str]]:
    """Seed this run's archive from the persisted lineage (ADR 0160).

    Returns (members added, records whose ref no longer resolves, trees
    rejected in an earlier invocation, trees kept in an earlier invocation).

    A member is added — and so becomes proposable-from — only if its ref
    still resolves in this repository. A candidate branch can be deleted or
    garbage-collected between invocations, and `replace(config, base_ref=...)`
    on a dead ref would fail the turn rather than skip the member. A record
    whose ref is gone still contributes its TREE, so duplicate detection and
    the rejected-tree stop keep working on history the repo can no longer
    check out. This is the same distinction `archive.py`'s header draws: the
    content archive stores bytes so it survives `git gc`; the lineage archive
    stores references and says out loud what it loses when they die.
    """
    records = archive.read_lineage(config.paths.lineage_dir, config.graph_id)
    rejected = {r.tree for r in records if not r.kept}
    kept_trees = {r.tree for r in records if r.kept}
    # Fold by ref through `archive.fold_lineage` — THE fold, shared with
    # `aef loop lineage list`, because two of them disagreed (ADR 0198).
    # `run_loop` appends a closing record for every member it proposed from,
    # carrying the children count the novelty term needs; without the fold,
    # resuming would read the count as it stood at the moment the member was
    # created — always zero — and the term that pushes the sampler away from
    # over-explored parents would reset itself every invocation, which is the
    # knob quietly not working rather than the knob being off. What the fold
    # must NOT do is let that closing record overwrite the immutable facts:
    # see `fold_lineage` for the resumed root that erased its own parent.
    folded = archive.fold_lineage(records)
    added = 0
    gone = 0
    seen = {root.ref}
    for record in folded.values():
        if record.ref == root.ref:
            # The previous run's kept head IS this run's root. One member —
            # but the root inherits what was measured about it, or the loop
            # would re-weight last night's best candidate as an unscored 0.5
            # and forget how many children it has already spawned.
            if root.score is None:
                root.score = record.score
            root.children = max(root.children, record.children)
            continue
        if record.ref in seen:
            continue
        if not _ref_resolves(config, record.ref):
            gone += 1
            continue
        seen.add(record.ref)
        members.append(
            ArchiveMember(
                ref=record.ref,
                score=record.score,
                parent_ref=record.parent_ref,
                children=record.children,
                tree=record.tree,
                kept=record.kept,
                disposition=record.disposition,
                resumed=True,
            )
        )
        added += 1
    return added, gone, rejected, kept_trees


def _persist_member(
    config: LoopConfig,
    member: ArchiveMember,
    *,
    run_id: str,
    turn: int,
    at: datetime,
    enabled: bool,
) -> None:
    if not enabled:
        return
    archive.append_lineage(
        config.paths.lineage_dir,
        config.graph_id,
        archive.LineageRecord(
            run_id=run_id,
            turn=turn,
            ref=member.ref,
            tree=member.tree,
            parent_ref=member.parent_ref,
            score=member.score,
            kept=member.kept,
            disposition=member.disposition,
            recorded_at=at + timedelta(seconds=turn),
            children=member.children,
        ),
    )


def _greedy_parent(archive: list[ArchiveMember], kept_ref: str) -> ArchiveMember:
    """Greedy's parent: the member the kept branch currently points at.

    This was `archive[-1]`, and ADR 0160 broke that identity twice. Rejected
    candidates are members now, so `archive[-1]` after a rejection is the
    rejected candidate and greedy would silently propose from un-gated
    content. And resumed members are appended after the root in *file* order,
    not in the order the kept branch moved, so on a second invocation
    `archive[-1]` was an ancestor — the loop proposed the same change it had
    already kept, and `test_a_second_run_resumes_from_the_existing_kept_branch`
    caught it. Both mutations are under test.

    Naming the ref rather than a position makes the greedy contract explicit:
    the latest kept state is wherever the kept branch is.
    """
    for member in reversed(archive):
        if member.ref == kept_ref and member.kept:
            return member
    return [m for m in archive if m.kept][0]  # pragma: no cover - root always matches


def run_loop(
    config: LoopConfig,
    *,
    now: datetime,
    workdir: Path,
    turns: int,
    budget_seconds: float,
    kept_branch: str = "loop/kept",
    clock: Callable[[], float] = time.monotonic,
    cycle_fn: Callable[..., CycleRun] | None = None,
    sample_parents: bool = False,
    seed: int = 0,
    persist_lineage: bool = True,
    **cycle_kwargs: Any,
) -> LoopRun:
    """Keep/revert on the metric, inside the gates (ADR 0114).

    autoresearch's loop is: propose, measure, keep if better, else revert,
    repeat until the budget is spent. This is that loop with the gates as
    the measurement and one deliberate difference: **"keep" advances a
    LOCAL branch, never `main`.** Every gate passing means the candidate
    beat the null cohort on the task metric with zero per-scenario
    regression (G3) — and `decide()` still returns ESCALATE, because
    Tier-1 auto-merge is off and stays off. The kept branch is what a person
    reviews and merges; what the loop gains is that the next proposal is
    made FROM the kept state, so improvements stack instead of each cycle
    re-proposing from the same base.

    With `sample_parents` (ADR 0121) the next proposal is made from a parent
    SAMPLED from the archive of everything gated so far — weighted by score
    and against how many children it already has — rather than always from
    the latest kept. Stepping stones survive; the kept branch points at the
    best-scoring KEPT member. Off by default: measured, not assumed.

    Since ADR 0160 the archive is durable and complete rather than in-memory
    and kept-only:

    - every gated candidate is written to `paths.lineage_dir` with its gate
      verdict and score, **rejects included** — they are the stepping stones
      DGM's archive exists to keep, and `_parent_weight` makes a rejected
      member sampleable exactly when G3 gave it a number;
    - the next invocation loads that file and resumes from it, so a parent
      kept last night can be proposed from tonight;
    - duplicate detection runs over the loaded set as well as this run's, so
      a second invocation cannot spend N+2 corpus passes re-gating a tree the
      first one already kept or already rejected.

    `persist_lineage=False` turns all of that off for a caller that wants a
    self-contained run (the A/B rig runs each arm on fresh state instead).

    Stops on: the turn count, the wall-clock budget, a halt, a turn that
    produced no candidate, or a candidate whose tree matches one already
    rejected in this run — the proposer is deterministic from its evidence,
    so re-gating the same rejected diff would spend N+2 corpus passes to
    learn nothing. A candidate whose tree matches one already KEPT is a
    duplicate: neither kept nor reverted, and the loop continues, because a
    different parent may produce something new.
    """
    _cycle = cycle if cycle_fn is None else cycle_fn
    started = clock()
    rng = random.Random(seed)
    # Before `_ensure_branch` cuts anything from it: `rev_parse` two lines
    # below would raise `GitError: git rev-parse ... failed (128)`, which
    # names git's exit code and not the configuration mistake (ADR 0189).
    require_base_ref(config.repo, config.base_ref)
    # BEFORE anything is created or gated: standing on the kept branch makes
    # every `update-ref` below leave a staged reversal behind (ADR 0125).
    _refuse_if_kept_branch_is_checked_out(config, kept_branch)
    kept_ref = _ensure_branch(config, kept_branch)
    main_before = config.repo.rev_parse(config.base_ref)
    root = ArchiveMember(ref=kept_ref, score=None, parent_ref=None, tree=_tree_of(config, kept_ref))
    members: list[ArchiveMember] = [root]
    lines: list[str] = [f"kept branch {kept_branch} at {kept_ref[:12]} (from {config.base_ref})"]
    rejected_trees: set[str] = set()
    # Every tree this loop has ever kept, in this run or a previous one. A set
    # rather than a scan of `members` because a persisted member whose ref has
    # been deleted or garbage-collected is not a member — it cannot be a
    # parent — but its tree must still be recognised as one the loop already
    # kept, or the deletion of a candidate branch would silently re-open a
    # search the loop had closed.
    kept_trees: set[str] = {root.tree}
    run_id = f"{now:%Y%m%dT%H%M%S}"
    if persist_lineage:
        resumed, gone, seeded_rejects, seeded_kept = _resume_lineage(config, members, root)
        rejected_trees |= seeded_rejects
        kept_trees |= seeded_kept
        lines.append(
            f"lineage: resumed {resumed} member(s) from "
            f"{archive.lineage_path(config.paths.lineage_dir, config.graph_id)}"
            + (f"; {gone} whose ref no longer resolves are history only" if gone else "")
        )
    done: list[LoopTurn] = []
    stopped_because = f"turn budget of {turns} exhausted"
    for turn in range(1, turns + 1):
        elapsed = clock() - started
        if elapsed > budget_seconds:
            stopped_because = (
                f"wall-clock budget of {budget_seconds:.0f}s exceeded after {turn - 1} turn(s)"
            )
            break
        parent = (
            _choose_parent(members, rng) if sample_parents else _greedy_parent(members, kept_ref)
        )
        parent.children += 1
        turn_config = replace(config, base_ref=parent.ref)
        # A scratch dir PER TURN. G1 materialises the candidate into
        # `workdir/workspace` and `trust._prepare_empty_destination` refuses a
        # non-empty one — so with one workdir for the whole run, every turn
        # after the first was rejected by G1 with a TrustBoundaryError before
        # any behavioural gate ran, whatever the proposer had done. Found by
        # RUNNING the I10 measurement (ADR 0122): the "turn 2 rejected" in
        # ADR 0114's and 0121's runs was this, not G3.
        run = _cycle(
            turn_config,
            now=now + timedelta(seconds=turn),
            workdir=workdir / f"turn-{turn}",
            **cycle_kwargs,
        )
        lines.extend(f"turn {turn}: {line}" for line in run.lines)
        if run.proposed is None:
            done.append(LoopTurn(turn, None, None, False, kept_ref, parent.ref))
            stopped_because = f"turn {turn} produced no candidate"
            break
        candidate_branch = f"loop/{run.proposed}"
        disposition = run.decision.disposition if run.decision else None
        passed = disposition in (Disposition.ESCALATE, Disposition.AUTO_MERGE)
        tree = _tree_of(config, candidate_branch)
        # A turn that tried several candidates gated all of them, and the ones
        # it did not keep were REJECTED by the gates on their own passes (ADR
        # 0200). Their trees have to be remembered here or the next turn can
        # spend cohort+2 corpus passes re-gating a diff this run already
        # refused. The winner's own tree is recorded below, as it always was.
        for loser in run.losers:
            if not loser.passed:
                rejected_trees.add(_tree_of(config, loser.branch))
        candidate_ref = config.repo.rev_parse(candidate_branch)
        # Kept members only. Since rejected candidates joined the archive
        # (ADR 0160), matching against every member would turn the loop's
        # "re-proposed a tree already rejected — the proposer has nothing new"
        # stop into a `continue`, and the loop would spin on the same rejected
        # diff for the whole turn budget.
        if tree in kept_trees:
            done.append(
                LoopTurn(
                    turn, run.proposed, disposition, False, kept_ref, parent.ref, run.score, True
                )
            )
            lines.append(f"turn {turn}: duplicate of a kept tree from {parent.ref[:12]}; skipped")
            continue
        if passed and run.exit_code != EXIT_HALTED:
            if root.score is None and run.incumbent_score is not None:
                root.score = run.incumbent_score
            member = ArchiveMember(
                ref=candidate_ref,
                score=run.score,
                parent_ref=parent.ref,
                tree=tree,
                kept=True,
                disposition=disposition.value if disposition else None,
            )
            members.append(member)
            kept_trees.add(tree)
            _persist_member(
                config, member, run_id=run_id, turn=turn, at=now, enabled=persist_lineage
            )
            # The kept branch points at the best-scoring KEPT member (greedy
            # mode: always the newest, since each is proposed from the last).
            best = max(
                (m for m in members if m.parent_ref is not None and m.kept),
                key=lambda m: (m.score if m.score is not None else -1.0, m.ref),
            )
            if sample_parents:
                target = best.ref
            else:
                target = candidate_ref
            config.repo.run("update-ref", f"refs/heads/{kept_branch}", target, kept_ref)
            kept_ref = target
            ledger.append(
                config.paths.ledger_dir,
                kind=ledger.EventKind.KEPT,
                at=now + timedelta(seconds=turn),
                proposal_id=run.proposed,
                summary=f"every gate passed; {kept_branch} -> {kept_ref[:12]} (not main)",
                detail={
                    "turn": turn,
                    "kept_branch": kept_branch,
                    "kept_ref": kept_ref,
                    "candidate_ref": candidate_ref,
                    "parent_ref": parent.ref,
                    "score": run.score,
                    "incumbent_score": run.incumbent_score,
                },
            )
            lines.append(
                f"turn {turn}: KEPT {candidate_ref[:12]} from {parent.ref[:12]} "
                f"(score {run.score}); {kept_branch}@{kept_ref[:12]}"
            )
            done.append(
                LoopTurn(turn, run.proposed, disposition, True, kept_ref, parent.ref, run.score)
            )
        else:
            # A rejected candidate is an archive member too (ADR 0160): the
            # gates measured a position in the search space and refused to
            # stand on it, which is information. `_parent_weight` decides
            # whether it can be proposed from — it can, iff G3 gave it a
            # score.
            member = ArchiveMember(
                ref=candidate_ref,
                score=run.score,
                parent_ref=parent.ref,
                tree=tree,
                kept=False,
                disposition=disposition.value if disposition else None,
            )
            members.append(member)
            _persist_member(
                config, member, run_id=run_id, turn=turn, at=now, enabled=persist_lineage
            )
            done.append(
                LoopTurn(turn, run.proposed, disposition, False, kept_ref, parent.ref, run.score)
            )
            lines.append(
                f"turn {turn}: reverted ({disposition.value if disposition else 'halted'})"
                + (
                    f", archived as a stepping stone (score {run.score})"
                    if run.score is not None
                    else ", no score: archived but not sampleable"
                )
            )
            if run.exit_code == EXIT_HALTED:
                stopped_because = f"halted at turn {turn}"
                break
            if tree in rejected_trees:
                stopped_because = (
                    f"turn {turn} re-proposed a tree already rejected; the proposer has nothing new"
                )
                break
            rejected_trees.add(tree)
    if config.repo.rev_parse(config.base_ref) != main_before:  # pragma: no cover - invariant
        raise RuntimeError(f"{config.base_ref} moved during run_loop; this must never happen")
    # Closing records: the children counts, which only exist once the run is
    # over. `_resume_lineage` folds by ref and takes the last, so these
    # supersede the at-creation records without rewriting a byte of them —
    # append-only, and the history of what was tried stays legible.
    for member in members:
        if member.children:
            _persist_member(config, member, run_id=run_id, turn=0, at=now, enabled=persist_lineage)
    lines.append(f"stopped: {stopped_because}")
    return LoopRun(
        turns=tuple(done),
        kept_branch=kept_branch,
        kept_ref=kept_ref,
        stopped_because=stopped_because,
        lines=tuple(lines),
        archive=tuple(members),
    )


def _materialise_candidate_branch(config: LoopConfig, branch: str, path: str, content: str) -> None:
    """Create the candidate as a LOCAL branch. Never pushed.

    The starting position is recorded as a SHA, not a branch name.
    `rev-parse --abbrev-ref HEAD` returns the literal string `"HEAD"` on a
    detached checkout — which is the normal CI shape, since
    `actions/checkout` with a ref or SHA detaches — so the restore was
    `git checkout HEAD`, a no-op, and the job was left standing on the
    candidate branch with an un-gated mutation in its working tree. The next
    cycle then read that mutation as its starting point and proposed on top
    of it while still diffing against the base (ADR 0078).
    """
    # The branch name when there is one, so an owner running this on `main`
    # is put back on `main` rather than left detached; the SHA otherwise.
    symbolic = config.repo.run("rev-parse", "--abbrev-ref", "HEAD").strip()
    original = symbolic if symbolic != "HEAD" else config.repo.run("rev-parse", "HEAD").strip()
    config.repo.run("checkout", "-q", "-B", branch, config.base_ref)
    try:
        target = config.repo.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        config.repo.run("add", "--", path)
        config.repo.run(
            "-c",
            "user.email=loop@aef",
            "-c",
            "user.name=aef-loop",
            "commit",
            "-q",
            "-m",
            f"loop: {branch}",
        )
    finally:
        # `--force` because the candidate content is committed on the branch
        # by this point; without it a leftover working-tree difference would
        # abort the restore and strand the caller on the candidate.
        config.repo.run("checkout", "-q", "--force", original)
