"""`aef loop` — the CLI surface over the self-rewiring driver.

Two boundary decisions live here rather than deeper in the harness.

**Network-isolation attestation is a CLI flag**, not something `sandbox.py`
reads from the environment. The value has to come from Zone B configuration
(the CI workflow), and an env read inside the sandbox would put a
security-relevant input somewhere closer to the candidate.

**A halted loop exits 2, distinct from a rejection's 1.** "Stop, something is
wrong with the system" and "this candidate is no good" are different
outcomes, and a workflow that cannot tell them apart will retry the first.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from aef.harness.archive import ArchiveError, lineage_path
from aef.harness.checks import TaskCheck
from aef.harness.corpus import (
    CorpusError,
    CorpusShrankError,
    Expected,
    Split,
    check_never_shrinks,
    load_corpus,
    load_manifest,
)
from aef.harness.git import GitRepo
from aef.harness.graph_loading import DEFAULT_GRAPH_FACTORY as DEFAULT_GRAPH_FACTORY
from aef.harness.graph_loading import GRAPH_REFERENCE_HELP, split_entrypoint
from aef.harness.loop import (
    EXIT_ERROR,
    EXIT_HALTED,
    EXIT_OK,
    EXIT_REJECTED,
    PROPOSERS,
    BaseRefError,
    CorpusGraphMismatchError,
    KeptBranchCheckedOutError,
    LoopConfig,
    LoopPaths,
    LoopRun,
    LoopStateInsideRepoError,
    PolicyConfigError,
    _check_state_is_outside_the_repo,
    _halt_channel,
    default_digest_window,
    require_base_ref,
    resolve_default_base_ref,
)
from aef.harness.loop import digest as loop_digest
from aef.harness.loop import gate as loop_gate
from aef.harness.loop import monitor as loop_monitor
from aef.harness.loop import status as loop_status
from aef.harness.monitoring import LoopHaltedError
from aef.harness.recorder import record_to_corpus
from aef.harness.zones import DEFAULT_AGENT_PATH, DEFAULT_AGENT_ROOT, ZonePolicy
from aef.providers.base import ModelProvider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aef.kernel import Graph
    from aef.state import AEFState

# `DEFAULT_AGENT_PATH` is one string behind four `--agent-path` flags and
# `_warn_unmet_obligations`, which is five places for one default.
#
# It used to be DERIVED here as `f"{DEFAULT_AGENT_ROOT}/demo/graph.py"` — the
# root came from `zones`, and `demo` was spelled out, which is aef-core's own
# fixture directory and exists in no adopted repo. ADR 0147 claimed it had
# rebuilt this constant from `DEFAULT_AGENT_ROOT`, and it had: it rebuilt the
# ROOT and kept the `demo`. So a repo that ran `adopt` then `migrate` and
# nothing else got `loop doctor` printing an unrunnable `bless` line and
# `loop cycle` exiting 0 having done nothing (reproduced, ADR 0149).
#
# It is imported now, never derived, and `zones` derives it once from what
# `aef migrate` writes.

# One flag, TWO meanings, and four of the five `--agent-path` arguments
# carried no help text at all while `--proposer`'s own help documented the
# second one. So the only place the persona form was written down was under
# a different flag, and preflight — which reads this one — understood only
# the first (reproduced, ADR 0178).
_AGENT_PATH_HELP = (
    "the module that builds your graph, repo-relative — e.g. "
    f"{DEFAULT_AGENT_PATH} (the default), or {DEFAULT_AGENT_ROOT}/migrated/<agent>/graph.py, "
    "which is what `aef migrate` prints for a prompt agent. SECOND FORM: with "
    "`--proposer rule_based_prompt` this names the PERSONA `.md` the proposer appends its "
    "lesson to (ADR 0157) — the preflight obligations then resolve that persona to the "
    "graph `aef migrate` generated for it, so both meanings are answered from one flag "
    "(ADR 0178). Left at its default, obligation 6 widens to every graph in the repo, "
    "because the path is then this package's guess (ADR 0167)."
)


# The archive key an omitted `--graph-id` means. It is a different namespace
# from `Graph.id` (see `harness.loop._scenarios_for_graph`), which is why it
# has a value of its own rather than being required.
DEFAULT_GRAPH_ID = "default"

# ONE spelling of "which graph", shared by every `aef loop` subcommand that
# takes one, and one help string so no two of them can describe it differently.
#
# There used to be two. `aef loop score` took `module:factory` and refused
# anything else; `bootstrap`, `record`, `harvest` and `cycle --module` took a
# bare module (ADR 0168 added a file path) and refused a colon. The same graph
# was therefore named two ways by two subcommands of one command, and each
# spelling failed on the other (reproduced, ADR 0176):
#
#     $ aef loop score agents.demo.graph --corpus corpus
#     error: entrypoint must be 'module:factory', got 'agents.demo.graph'
#     $ aef loop bootstrap agents.demo.graph:build_graph --corpus corpus ...
#     error: No module named 'agents.demo.graph:build_graph'
#
# The union is what every one of them accepts now. Nothing was narrowed: each
# subcommand still accepts every spelling it accepted before.
#
# There were, in fact, THREE. ADR 0176 fixed the five in-process callers and
# left `--entrypoint` — the string handed to the OUT-OF-PROCESS gates — on
# `harness.scenario_runner.load_graph`, which demanded `module:factory` and
# refused both other forms. Both the string and the splitter now live in
# `aef/harness/graph_loading.py`, which the harness and the CLI can each read
# and neither has to copy (ADR 0182).
#
# `split_graph_reference` is the name ADR 0176 published; it IS
# `split_entrypoint` now rather than a second implementation of it.
# `DEFAULT_GRAPH_FACTORY` is re-exported from this module for the same reason
# (`import X as X` above), so both names keep working from both import paths
# while there is only one definition of each.
split_graph_reference = split_entrypoint

# ONE help string for `--entrypoint` too, on all three subcommands that take
# it. It had three: "module:factory that builds your graph, e.g. …" (gate),
# "module:factory that builds your graph; G2/G3 refuse without it" (cycle) and
# "module:factory; G2/G3 refuse without it" (run) — three descriptions of one
# flag, all three of them wrong about what it accepts (ADR 0182). The shared
# sentence is `GRAPH_REFERENCE_HELP`, so `--module` and `--entrypoint` now
# describe the same three forms in the same words, which is the point:
# `--module agents/x/graph.py --entrypoint agents/x/graph.py` used to accept
# the first and refuse the second inside one invocation.
ENTRYPOINT_HELP = (
    "the graph the OUT-OF-PROCESS gates build to re-execute the corpus. G2 and G3 "
    "refuse without it — they have to execute the corpus to have anything to say. "
    "There is deliberately no default: one would name a layout your repo may not have "
    "and fail as an import error inside a gate rejection. " + GRAPH_REFERENCE_HELP
)


def load_graph_reference(reference: str) -> Graph:
    """The one loader behind every `aef loop` subcommand that names a graph.

    Strictly stronger than the two it replaces. It keeps ADR 0168's file-path
    import (which `scenario_runner.load_graph` never had) AND ADR 0085's
    control (which `cli.run.load_graph_module` never had): a factory that
    raises `SystemExit` must not exit this process cleanly, so the call is
    guarded by `except BaseException`, not `except Exception`.
    """
    from aef.cli.run import import_graph_module
    from aef.harness.scenario_runner import EntrypointError
    from aef.kernel import Graph

    try:
        module_ref, factory_name = split_graph_reference(reference)
    except ValueError as exc:
        raise EntrypointError(str(exc)) from exc
    try:
        module = import_graph_module(module_ref)
    except (ImportError, ValueError) as exc:
        raise EntrypointError(f"cannot import {module_ref!r}: {exc}") from exc
    factory = getattr(module, factory_name, None)
    if factory is None:
        raise EntrypointError(f"{module_ref!r} has no {factory_name}(). {GRAPH_REFERENCE_HELP}")
    try:
        graph = factory()
    except BaseException as exc:  # noqa: BLE001 - ADR 0085, see the docstring
        raise EntrypointError(f"{reference} raised {type(exc).__name__}: {exc}") from exc
    if not isinstance(graph, Graph):
        raise EntrypointError(f"{reference} returned {type(graph).__name__}, expected a Graph")
    return graph


class GraphIdError(RuntimeError):
    """`--graph-id` cannot be settled against the corpus this cycle loaded.

    Named and raised rather than guessed. Guessing which of several graphs a
    cycle is improving picks the evidence the proposer is grounded in, and a
    wrong guess is indistinguishable from having no evidence — which is the
    exact ambiguity ADR 0176's F2 is about.
    """


def graph_id(args: argparse.Namespace) -> str:
    """The settled archive key: what `--graph-id` says, or the default.

    One function, because `--graph-id` is read by `_config`, `bless` and
    `doctor` and three copies of `args.graph_id or "default"` would drift the
    first time the rule gained a case (ADR 0091's shape).
    """
    return getattr(args, "graph_id", None) or DEFAULT_GRAPH_ID


def _blessed_under(args: argparse.Namespace, candidate: str) -> bool:
    """Does the loop state hold a blessed baseline under this archive key?

    Read-only, and a bad key answers False rather than raising: this is used
    to decide how loudly to talk about a mismatch, and `bless`/`doctor` are
    where a malformed `--graph-id` is refused by name (ADR 0167).
    """
    from aef.harness import archive

    state = getattr(args, "state", None)
    if not state:
        return False
    try:
        return bool(archive.versions(LoopPaths(root=Path(state)).archive_dir, candidate))
    except (ArchiveError, OSError, ValueError):
        return False


class GraphIdResolution(NamedTuple):
    """What `resolve_graph_id_from_corpus` decided. `derived` is set only when
    the value was taken FROM the corpus, so the verdict line can say so without
    re-reading the note it printed."""

    note: str | None = None
    derived: str | None = None


def resolve_graph_id_from_corpus(args: argparse.Namespace) -> GraphIdResolution:
    """Settle the EVIDENCE graph id against the corpus, mutating `args`.
    Raises `GraphIdError` when it cannot be settled.

    THE DEFECT (reproduced, ADR 0176). `aef loop cycle` without `--graph-id`
    took the archive key `"default"`, and `RuleBasedPromptProposer` drops every
    memory record whose run id belongs to a scenario of another graph — so a
    corpus bootstrapped from one prompt agent gave

        2 record(s) dropped as another graph's scenario;
        no admissible failure record for this graph

    and exit 0, while the same command with `--graph-id demo_agent` proposed.
    The graph id was sitting in the corpus the command had already loaded.

    THE COMPLICATION, AND WHY IT IS GONE. `--graph-id` used to be TWO things:
    the archive key G5 reads its blessed baseline under, and — via
    `_build_proposer` — a `Graph.id` the prompt proposer matches scenarios
    against. ADR 0125 separated those namespaces deliberately, so deriving
    unconditionally moved the archive key out from under a baseline the owner
    had already blessed, and G5 then rejected every candidate for having
    nothing to compare to. Measured, not guessed:
    `test_an_adopted_repo_gates_a_candidate_end_to_end` went from a gated
    candidate to `not built: G5 rejected the candidate first`. ADR 0176 could
    therefore only derive when there was nothing to orphan, and WARNED in the
    two configurations where there was — leaving the evidence dropped and
    saying so instead of fixing it.

    `LoopConfig` has two fields now (ADR 0182). `graph_id` is the archive key
    and this function NEVER moves it; `evidence_graph_id` is the `Graph.id`
    the proposer admits records under, and deriving it cannot orphan anything.
    So the rule is one rule with one refusal:

    - no `--corpus`, or an empty one -> derive nothing;
    - `--graph-id` given and naming a graph in the corpus -> that value is
      both, exactly as before, and nothing is said;
    - `--graph-id` given and naming NO graph in the corpus: the archive-key
      namespace (ADR 0125). The key is kept and the EVIDENCE id is derived
      from the corpus when that is unambiguous — which is what the second
      warning used to describe instead of doing. With nothing blessed under it
      either, it is neither namespace, so it is a typo and is refused;
    - `--graph-id` omitted, corpus records one graph -> derive the evidence
      id, say so, leave the key alone;
    - `--graph-id` omitted, corpus records several -> refuse, listing them.
      There is no defensible choice, and guessing picks the evidence the
      proposer is grounded in.
    """
    corpus_dir = Path(args.corpus) if getattr(args, "corpus", None) else None
    if corpus_dir is None or not corpus_dir.is_dir():
        return GraphIdResolution()
    scenarios = load_corpus(corpus_dir).scenarios
    present = sorted({s.graph_id for s in scenarios})
    if not present:
        return GraphIdResolution()
    listed = ", ".join(repr(g) for g in present)
    given = getattr(args, "graph_id", None)

    if given is not None:
        if given in present:
            return GraphIdResolution()
        if not _blessed_under(args, given):
            raise GraphIdError(
                f"--graph-id {given!r} names no graph in the corpus at {corpus_dir}, which "
                f"records {listed}, and no blessed baseline sits under it either — so it is "
                f"neither the corpus's graph nor a live archive key. Every failure record "
                f"tied to those scenarios would be dropped as another graph's and the cycle "
                f"would report having no evidence while holding all of it (ADR 0176). Pass "
                f"one of the ids above, or omit --graph-id and let it be derived."
            )
        if len(present) > 1:
            # A live archive key AND an ambiguous corpus. The key is not a
            # typo and is kept; which graph's evidence to ground in is still
            # not guessable, so this is the one place a warning survives.
            return GraphIdResolution(
                note=(
                    f"--graph-id {given!r} names no graph in the corpus at {corpus_dir} "
                    f"({listed}) and IS the key a blessed baseline sits under, so it is "
                    f"being read as the archive key (ADR 0125). WARNING: the corpus "
                    f"records {len(present)} graphs, so the evidence id cannot be derived "
                    f"either — --proposer rule_based_prompt will drop every failure record "
                    f"tied to those scenarios as another graph's. Bootstrap a corpus for "
                    f"one graph, or bless under the id you mean to improve."
                )
            )
        derived = present[0]
        args.evidence_graph_id = derived
        return GraphIdResolution(
            note=(
                f"--graph-id {given!r} names no graph in the corpus at {corpus_dir} and IS "
                f"the key a blessed baseline sits under, so it is kept as the ARCHIVE key "
                f"(ADR 0125); the evidence id is derived from the corpus instead: "
                f"{derived!r}, from its {len(scenarios)} scenario(s), which record one graph"
            ),
            derived=derived,
        )

    if len(present) > 1:
        raise GraphIdError(
            f"--graph-id was not given and the corpus at {corpus_dir} records "
            f"{len(present)} graphs ({listed}). Which one this loop improves decides "
            f"which recorded failures the proposer may ground in, so it is not "
            f"guessable: pass --graph-id naming one of them."
        )

    derived = present[0]
    if derived == graph_id(args):
        # Already the same string; deriving would say something about nothing.
        return GraphIdResolution()
    args.evidence_graph_id = derived
    return GraphIdResolution(
        note=(
            f"--graph-id not given; the evidence graph id is derived as {derived!r} from "
            f"the {len(scenarios)} scenario(s) in {corpus_dir}, which record one graph. "
            f"The archive key is unchanged ({graph_id(args)!r}), so a blessed baseline "
            f"stays where it was blessed (ADR 0182)"
        ),
        derived=derived,
    )


def _build_commands(args: argparse.Namespace) -> tuple[tuple[str, ...], ...] | None:
    """Repo-specific build commands, or None to take G1's default.

    Each `--build-command` is a whole shell-free command, split on spaces.
    Repeatable, because a green bar is usually more than one command.
    """
    raw = getattr(args, "build_command", None)
    if not raw:
        return None
    return tuple(tuple(c.split()) for c in raw)


_BASE_REF_HELP = (
    "the ref a candidate is proposed FROM and diffed AGAINST. Default: this "
    "repository's own default branch — origin/HEAD when there is a remote, else the "
    "branch you are on (never a loop/ branch). A ref that does not exist is refused by "
    "name, not reported as a missing agent file (ADR 0189)."
)


def _proposer(args: argparse.Namespace) -> tuple[str, ModelProvider | None, str | None]:
    """`--proposer llm` builds the harness provider (ADR 0112: the session's
    own login, no key) for the model `--proposer-model` names. The default
    is rule-based, by measurement (ADR 0122)."""
    proposer = getattr(args, "proposer", "rule_based")
    if proposer != "llm":
        return proposer, None, None
    model = getattr(args, "proposer_model", None)
    if not model:
        raise ValueError("--proposer llm needs --proposer-model <model id>")
    from aef.config.factory import build_model_provider
    from aef.config.schema import ModelProviderConfig

    return (
        proposer,
        build_model_provider(ModelProviderConfig(impl="claude_code", model=model)),
        model,
    )


def _config(args: argparse.Namespace) -> LoopConfig:
    corpus_dir = Path(args.corpus) if getattr(args, "corpus", None) else None
    repo = GitRepo(root=Path(args.repo))
    proposer, proposer_provider, proposer_model = _proposer(args)
    config = LoopConfig(
        proposer=proposer,
        proposer_provider=proposer_provider,
        proposer_model=proposer_model,
        repo=repo,
        paths=LoopPaths(root=Path(args.state)),
        # ONE derivation, here, for every subcommand that builds a config —
        # ADR 0149's rule that a default derives from one place. `--base` is
        # `None` unless the owner said, and `None` means "ask the repository"
        # rather than "the literal main", which is what made a repo whose
        # default branch is not `main` no-op at exit 0 (ADR 0187 / 0189).
        base_ref=getattr(args, "base", None) or resolve_default_base_ref(repo),
        graph_id=graph_id(args),
        # Two fields, two namespaces (ADR 0125, split in ADR 0182). `None`
        # unless `resolve_graph_id_from_corpus` derived one, and `None` means
        # "the same as the archive key" — so every subcommand that never
        # derives is byte-for-byte the configuration it had before.
        evidence_graph_id=getattr(args, "evidence_graph_id", None),
        corpus=load_corpus(corpus_dir) if corpus_dir and corpus_dir.is_dir() else None,
        network_isolated=bool(getattr(args, "network_isolated", False)),
        sandbox_image=getattr(args, "sandbox_image", None),
        build_commands=_build_commands(args),
        entrypoint=getattr(args, "entrypoint", None),
        config_path=getattr(args, "config", None),
        # Both sides of G5's drift metric must describe the SAME tree.
        # `bless` took an agent_root and `_config` never set a zone_policy,
        # so a non-default root gave the baseline and the candidate two
        # different trees — the ADR 0074 defect, latent (ADR 0084).
        zone_policy=ZonePolicy(agent_root=getattr(args, "agent_root", DEFAULT_AGENT_ROOT)),
        # Never wired to a flag. Enabling Tier-1 auto-merge is an owner
        # action against the source, not something a CI invocation can do by
        # passing an argument (ADR 0045).
        tier1_enabled=False,
        # "fail" unless the owner asked for live scoring by name (ADR 0123).
        cassette_miss=getattr(args, "cassette_miss", "fail"),
        # 1 unless the owner asked for more by name (ADR 0200). `getattr` with
        # the dataclass's own default, so the six subcommands that define no
        # such flag build exactly the configuration they built before.
        candidates_per_turn=int(getattr(args, "candidates", 1) or 1),
        audit_slice_size=int(getattr(args, "audit_slice", 0) or 0),
    )
    # Every `aef loop` subcommand that takes BOTH --repo and --state passes
    # through here — the nine `_common()` wires — and until ADR 0167 only five
    # of them refused a state directory inside the repository, because the
    # refusal lived in `harness.loop._preflight` and `bless`, `digest`,
    # `harvest` and `doctor` do not call it. Reproduced: `aef loop bless
    # --state <repo>/state` printed "blessed ... as baseline v1" and left
    # `archive/` and `ledger.jsonl` INSIDE the repo, where `git add -A` sweeps
    # the audit trail into the candidate diff being judged — the exact failure
    # `_check_state_is_outside_the_repo` exists to prevent, reached from a
    # command that never asked it.
    #
    # The same function, imported rather than re-implemented: two copies of
    # one predicate is the ADR 0091 shape, and this pair would drift the first
    # time the rule gained a case.
    _check_state_is_outside_the_repo(config)
    return config


def _warn_unmet_obligations(args: argparse.Namespace, config: LoopConfig) -> None:
    """Print the obligations `aef loop doctor` would flag, before running.

    ADR 0137 §2 said "`Preflight.ready` is false while it stands, so the gates
    refuse", and `Preflight.render()` said the same. Neither was true: nothing
    outside `aef loop doctor` had ever read `ready`, so a repo whose model
    calls are invisible went straight from `doctor` exit 1 to `cycle`
    proposing and gating (reproduced, ADR 0141).

    A WARNING and not a refusal, decided rather than defaulted. Obligation 4
    is only knowable at this boundary — the halt webhook is read from the
    environment here on purpose, never inside the harness — so a refusal could
    live only in the CLI, and `harness.loop.cycle()` is importable, so the
    claim would still have been false for the API `aef loop run` uses. Three
    of the six enforce themselves later anyway (G2/G3 on an empty corpus, G5
    on a missing baseline, and no reflect route means no failure memory so the
    proposer never proposes), and turning five long-advisory obligations into
    blockers breaks the documented first-day sequence, in which observations
    do not exist yet. Strengthening a control is an owner's decision, not a
    fix wave's side effect. So: say it, loudly, at the moment it matters.
    """
    from aef.harness.preflight import preflight

    corpus_root = Path(args.corpus) if getattr(args, "corpus", None) else None
    if corpus_root is None:
        return  # nothing to preflight against; `loop doctor` is the place to ask
    result = preflight(
        repo_root=Path(args.repo),
        state_root=config.paths.root,
        corpus_root=corpus_root,
        agent_path=getattr(args, "agent_path", None) or DEFAULT_AGENT_PATH,
        agent_root=getattr(args, "agent_root", DEFAULT_AGENT_ROOT),
        # Obligation 6 over EVERY graph when the path is this package's guess
        # rather than the owner's answer (ADR 0167/0168).
        scan_all_graphs=_agent_path_is_defaulted(args),
        graph_id=config.graph_id,
        halt_channel_configured=(
            _halt_channel(config) is not None
            or bool(getattr(_halt_notifier(), "configured", False))
        ),
        observations=config.paths.observations,
    )
    if result.ready:
        return
    names = ", ".join(o.name for o in result.unmet)
    print(
        f"  preflight: {len(result.unmet)} of {len(result.obligations)} obligation(s) unmet "
        f"({names}). ADVISORY — this command does not refuse on them; run "
        f"`aef loop doctor` for each fix."
    )


# ---------------------------------------------------------------------------
# The containment warning, surfaced (ADR 0179's open item, closed in ADR 0182)
# ---------------------------------------------------------------------------
#
# ADR 0179 R3 moved `prompt_agent.persona_in_user_turn` out of `state.errors`
# — where it zeroed the task metric, became failure memory and got pasted into
# the persona by the proposer — and into the containment record the node
# writes every run, with `containment_warnings(state)` as THE reader. It said
# so plainly: "Until that lands, the fact lives in the trace and in
# `working_memory` and nothing prints it."
#
# Reproduced (ADR 0182): a real migrated prompt agent, bootstrapped through a
# real `impl: command` provider whose argv template has no `{system}` slot,
# with the fact demonstrably on the recorded scenario —
#
#     scenario q-1: containment_warnings -> [('prompt_agent', {'isolation':
#       ['single_turn', 'user_turn_persona'], 'message': "provider … declares
#       no system channel…
#
# — and `aef loop doctor` printing six obligations and `aef loop cycle`
# printing a verdict, neither of them mentioning it.
#
# WHICH STATES ARE READ, and why not the memory store. `--memory` was checked
# and deliberately carries nothing: ADR 0179's whole finding is that this fact
# is not a failure, so `make_reflect_node` never writes it into a
# `MemoryRecord`, and a reader that went looking there would find nothing on
# every repo. The two places an executed run's state survives are the recorded
# runs under `--runs` and the corpus scenarios, and both are read here.
#
# A WARNING, not an obligation. `Preflight` is a fixed list of six things an
# owner must SUPPLY; this is a property of a provider they already installed,
# with no fix inside the repo — exactly the distinction ADR 0179 drew when it
# refused to leave the fact in `state.errors`. So it is printed beside the
# report rather than added to it, and it never changes an exit code.
CONTAINMENT_ADR = "see ADR 0179"


def _final_state_of(initial: AEFState, trace: Sequence[Any]) -> AEFState:
    state = initial
    for record in trace:
        state = record.delta.apply(state)
    return state


def _containment_lines(states: Iterable[AEFState]) -> list[str]:
    """One line per DISTINCT provider that sent a persona in the user turn.

    Per provider and not per run: the fact is a property of the provider, so a
    corpus of two hundred scenarios recorded against one CLI is one sentence,
    not two hundred. Sorted by provider name, for the reason
    `containment_warnings` sorts by node id — a line order that depends on
    which scenario happened to load first is one more thing to hold in the
    head.
    """
    from aef.reasoning.prompt_agent import containment_warnings

    seen: dict[str, str] = {}
    for state in states:
        for node_id, warning in containment_warnings(state):
            provider = str(warning.get("provider", "unknown"))
            if provider in seen:
                continue
            isolation = warning.get("isolation")
            listed = (
                ", ".join(str(item) for item in isolation)
                if isinstance(isolation, list) and isolation
                else "none declared"
            )
            seen[provider] = (
                f"{node_id}: persona sent in the USER turn by provider {provider!r} "
                f"(isolation: {listed}) — {CONTAINMENT_ADR}"
            )
    return [seen[provider] for provider in sorted(seen)]


def _corpus_states(args: argparse.Namespace) -> list[AEFState]:
    """The final state of every scenario in `--corpus`. These ARE the runs the
    gates re-execute, so a warning on one is a warning about the evidence this
    turn is judging against."""
    root = Path(args.corpus) if getattr(args, "corpus", None) else None
    if root is None or not root.is_dir():
        return []
    try:
        scenarios = load_corpus(root).scenarios
    except CorpusError:
        # A malformed corpus is somebody else's error to report; this is a
        # note printed beside a result, and losing the result to it would be
        # the tail wagging the dog.
        return []
    return [_final_state_of(s.initial_state, s.trace) for s in scenarios]


def _latest_run_state(args: argparse.Namespace) -> list[AEFState]:
    """The MOST RECENT recorded run under `--runs`, or nothing.

    The most recent one only: the question this answers is "what does the
    provider you are running now do", and an answer assembled from a year of
    runs would name a CLI the owner replaced in March.
    """
    root = Path(args.runs) if getattr(args, "runs", None) else None
    if root is None or not root.is_dir():
        return []
    from aef.harness.harvest import HarvestError, load_runs

    try:
        runs = load_runs(root)
    except HarvestError:
        return []
    if not runs:
        return []
    latest = max(runs, key=lambda r: r.at)
    return [_final_state_of(latest.initial_state, latest.trace)]


def _print_containment_summary(args: argparse.Namespace) -> None:
    """The cycle/gate summary line(s). Silent when there is nothing to say."""
    for line in _containment_lines([*_corpus_states(args), *_latest_run_state(args)]):
        print(f"  {line}")


def cmd_gate(args: argparse.Namespace) -> int:
    refusal = _require_config_for_live_cassette(args, "gate")
    if refusal is not None:
        return refusal
    config = _config(args)
    refusal = _refuse_missing_base_ref(config)
    if refusal is not None:
        return refusal
    _warn_unmet_obligations(args, config)
    try:
        run = loop_gate(
            config,
            args.head,
            now=datetime.now(UTC),
            workdir=Path(args.workdir),
        )
    except PolicyConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    except LoopHaltedError as exc:
        print(f"HALTED: {exc}")
        return EXIT_HALTED

    print(run.report)
    _print_containment_summary(args)
    if run.halted:
        print("\nLOOP HALTED — a proposal reached for the harness. Read the ledger.")
    return run.exit_code


def cmd_monitor(args: argparse.Namespace) -> int:
    config = _config(args)
    # A deployment writes observations wherever it runs; the monitor reads
    # <state>/observations.jsonl. Nothing connected the two, so every window
    # reported unobserved and silently reverted (ADR 0072).
    if args.observations:
        source = Path(args.observations)
        if not source.is_file():
            print(f"error: no observations file at {source}", file=sys.stderr)
            return EXIT_REJECTED
        target = config.paths.observations
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            target.write_text(source.read_text())

    try:
        run = loop_monitor(config, now=datetime.now(UTC))
    except PolicyConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    except LoopHaltedError as exc:
        print(f"HALTED: {exc}")
        return EXIT_HALTED

    print(f"checked {run.checked} merged change(s)")
    for line in run.lines:
        print(f"  {line}")
    for proposal_id in run.rolled_back:
        print(f"  ROLLED BACK: {proposal_id}")

    # The metric that would have caught ADR 0165: a loop that runs on a timer
    # and produces nothing. The monitor is the right place for it because the
    # monitor is the thing that runs hourly whether or not anything happened,
    # and "nothing happened" is the condition being reported.
    from aef.harness import ledger as _ledger
    from aef.harness.monitoring import assess_cycle_staleness, read_cycle_attempts

    try:
        entries = _ledger.read(config.paths.ledger_dir)
    except _ledger.LedgerError as exc:
        # A staleness report is not worth losing the rollback result over, and
        # a broken chain is already `aef loop status`'s headline.
        print(f"  cycle staleness unavailable: {exc}")
    else:
        staleness = assess_cycle_staleness(
            entries, read_cycle_attempts(config.paths.root), now=datetime.now(UTC)
        )
        for line in staleness.lines():
            print(f"  {line}")

    if run.halted:
        print("\nLOOP HALTED:")
        for reason in run.halt_reasons:
            print(f"  - {reason}")
    return run.exit_code


def cmd_digest(args: argparse.Namespace) -> int:
    config = _config(args)
    since, until = default_digest_window(datetime.now(UTC))
    from aef.harness.harvest import load_runs

    notifier = _halt_notifier()
    result = loop_digest(
        config,
        since=since,
        until=until,
        owner_edits=args.owner_edits,
        halt_channel_configured=bool(getattr(notifier, "configured", False)),
        runs_recorded=len(load_runs(Path(args.runs))) if args.runs else 0,
    )
    print(result.to_json() if args.json else result.render())
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    config = _config(args)
    status = loop_status(config)
    print(status.render())
    # A broken ledger or an engaged kill switch is not a healthy system, and
    # `status` is often the thing a workflow keys off.
    return EXIT_HALTED if (status.halted or not status.ledger_ok) else EXIT_OK


def cmd_record(args: argparse.Namespace) -> int:
    from aef.cli.run import build_run_config
    from aef.harness.recorder import RecorderError

    graph = load_graph_reference(args.module)
    from aef.services.knowledge.in_memory import InMemoryKnowledgeStore
    from aef.services.memory.in_memory import InMemoryMemoryStore

    # Everything `--config` contributes, read through `aef run`'s OWN
    # construction site (ADR 0145's rule, applied to the caller ADR 0145 did
    # not touch — ADR 0149).
    #
    # This used to be a private `load_agent_config` + `build_model_provider`
    # that took the provider and the reflection impl and dropped `policies`,
    # `tools.allow`, `evaluator.suites` and `context` on the floor. It is the
    # WORST place in the repo for that drift, because `aef loop record
    # --expected must_fail` is the only documented way to mint a tripwire:
    # the recording ran deny-by-default, the graph's tool call was denied,
    # the run "failed", and the MUST_FAIL guard accepted the label on
    # evidence that came from the dropped config rather than from the task
    # being beyond the agent. `scenario_runner` then applies the OWNER's
    # policy at gate time, the same scenario passes, `tripwire_hit` fires and
    # G2 rejects every candidate forever reporting reward hacking (ADR 0149,
    # reproduced end to end).
    run_config = build_run_config(getattr(args, "config", None))
    memory = InMemoryMemoryStore()
    knowledge = InMemoryKnowledgeStore()

    try:
        checks = _parse_checks(getattr(args, "check", None))
    except (ValueError, TypeError) as exc:
        print(f"error: --check: {exc}", file=sys.stderr)
        return EXIT_REJECTED

    # `RecorderError` is a NAMED refusal of what the operator asked for — an
    # id that already exists, a `--expected must_fail` on a task the agent
    # completed (ADR 0149's tripwire guard) — so it is a rejection, exit 1,
    # and must be caught by name. It used to reach `main()`'s catch-all,
    # which returned 1 for everything; now that a crash is EXIT_ERROR, a
    # refusal that is not caught by name would be reported as one (ADR 0182).
    # `cmd_bootstrap` catches exactly this family for exactly this reason.
    try:
        recorded = _record(args, graph, run_config, memory, knowledge, checks)
    except (RecorderError, CorpusError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    print(f"recorded {recorded.scenario.id} ({recorded.scenario.split.value}) -> {recorded.path}")
    print(f"  {len(recorded.scenario.trace)} node execution(s) pinned")
    print(
        f"  {len(recorded.scenario.model_calls)} model call(s) pinned, "
        f"{len(recorded.scenario.checks)} check(s)"
    )
    return EXIT_OK


def _record(
    args: argparse.Namespace,
    graph: Graph,
    run_config: Any,
    memory: Any,
    knowledge: Any,
    checks: tuple[TaskCheck, ...],
) -> Any:
    """`cmd_record`'s call to the recorder, lifted so the `try` around it
    wraps the recorder and nothing else — a broad `try` over the services
    construction would turn a genuine crash there into a rejection."""
    from aef.config import build_retriever
    from aef.services.runtime import agent_services
    from aef.state import AEFState

    return record_to_corpus(
        Path(args.corpus),
        graph,
        AEFState(
            run_id=args.scenario_id,
            agent_id=args.agent_id,
            objective=args.objective,
            # Without this the recorder could only ever run the agent with an
            # empty working memory — so every recorded scenario landed on the
            # agent's happy path, and the failing cases LOOP.md tells owners to
            # record (and every tripwire) were unreachable from the CLI that
            # records them (ADR 0074).
            working_memory=json.loads(args.working_memory) if args.working_memory else {},
        ),
        # Same wiring as `aef run`: an agent following obligation 2 has a
        # reflect node, and a bare Services() cannot run one. Recording is
        # useless if it cannot record the agent the adopter was told to build
        # (ADR 0073).
        # Same list the gates use, so a scenario recorded here can be
        # re-executed there (aef/services/runtime.py, ADR 0091).
        agent_services(
            memory=memory,
            knowledge=knowledge,
            model_provider=run_config.model_provider,
            # The adopter's policy, not the engine's default. Without this a
            # scenario pins behaviour production never had, and a tripwire
            # pins a denial rather than an impossibility (ADR 0149).
            policy=run_config.policy_config,
            # Over the SAME stores the container carries, or the retriever
            # reads a different memory than the agent writes (ADR 0091/0118).
            retriever=build_retriever(
                run_config.context,
                memory=memory,
                agent_id=args.agent_id,
                knowledge=knowledge,
            ),
            reflection=run_config.reflection,
            reflection_model=run_config.reflection_model,
            agent_id=args.agent_id,
        ),
        scenario_id=args.scenario_id,
        split=Split(args.split),
        recorded_at=datetime.now(UTC),
        notes=args.notes,
        allow_holdout=args.i_am_spending_the_holdout,
        expected=Expected(args.expected),
        checks=checks,
        budget_ms=getattr(args, "budget_ms", None),
    )


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """A corpus on day one: run the configured graph once per input and
    record each run as a TRAIN scenario (ADR 0138).

    Everything that makes this safe lives in `aef.harness.bootstrap` — train
    only, no `expected` label, no overwrite, and the failure count reported.
    This function is the boundary: it reads the file, builds the same
    services `aef loop record` builds, and prints.
    """
    from aef.cli.run import build_run_config
    from aef.config import build_retriever
    from aef.harness.bootstrap import BootstrapError, bootstrap, load_inputs
    from aef.harness.memory_store import FileMemoryStore
    from aef.harness.recorder import RecorderError
    from aef.kernel import Services
    from aef.services.knowledge.in_memory import InMemoryKnowledgeStore
    from aef.services.memory.base import MemoryStore
    from aef.services.runtime import agent_services

    # Bootstrap writes to corpus/, which IS the evidence every behavioural
    # gate is measured against, so it honours the same halt `harvest` does
    # (ADR 0069): a halted loop must not have its gate evidence changed
    # underneath it.
    #
    # This used to read `if getattr(args, "state", None):` — a guard whose
    # enforcement was opt-in by the caller, on a flag ADR 0138 made optional
    # and ADR 0138's OWN Evidence block then omitted. So the documented
    # invocation grew a halted loop's corpus and exited 0, while the same
    # command with `--state` exited 2 (reproduced, ADR 0141). A control that
    # only binds when you ask for it is not a control. One of the two flags is
    # now required, and `--no-loop-state` makes "there is no loop yet" a thing
    # the owner SAYS rather than something silence is read as.
    if not getattr(args, "state", None) and not getattr(args, "no_loop_state", False):
        print(
            "error: bootstrap writes to corpus/, which is the evidence every behavioural "
            "gate is measured against, so it must be able to see the kill switch (ADR 0069). "
            "Pass --state <dir> — the same directory every other loop subcommand takes — or "
            "--no-loop-state if there is genuinely no loop yet. Neither was given, and "
            "silence used to mean 'do not check', which grew the corpus of a HALTED loop "
            "(ADR 0141).",
            file=sys.stderr,
        )
        return EXIT_REJECTED
    if getattr(args, "state", None):
        try:
            LoopPaths(root=Path(args.state)).kill_switch.check()
        except LoopHaltedError as exc:
            print(f"HALTED: {exc}")
            return EXIT_HALTED

    graph = load_graph_reference(args.module)

    # The provider `aef run` would use, from the same config, so what gets
    # recorded is what production would have said — identical to `record`.
    #
    # Read through `build_run_config`, which is `aef run`'s OWN code path
    # (ADR 0145). This used to be a second construction site that built the
    # provider and the reflection impl and silently dropped `policies`,
    # `tools.allow` and `evaluator.suites` — so a scenario recorded here
    # pinned behaviour under the engine's default policy while production ran
    # under the adopter's. Two constructions of one dependency drifting apart
    # is ADR 0091's finding, four times over.
    run_config = build_run_config(getattr(args, "config", None))

    def services(memory: MemoryStore) -> Services:
        # A FACTORY, and the STORE IS HANDED IN: `bootstrap` builds a
        # per-input `RunScopedMemory` — reads isolated so a scenario cannot
        # depend on what an earlier input remembered (ADR 0138), writes
        # mirrored into `--memory` so the failing run's reflection outlives
        # the process (ADR 0145).
        #
        # Knowledge is fresh per input and is NOT consolidated from the
        # durable sink, which is where this deliberately parts company with
        # `aef run --memory` (which does rebuild it). Consolidating would put
        # lessons learned from input 3 into input 4's context, and the
        # scenario recorded for input 4 would then be one that cannot
        # reproduce alone. Isolation wins; the ADR says so out loud.
        knowledge = InMemoryKnowledgeStore()
        return agent_services(
            memory=memory,
            knowledge=knowledge,
            model_provider=run_config.model_provider,
            policy=run_config.policy_config,
            # Over the SAME stores the container carries, or the retriever
            # reads a different memory than the agent writes (ADR 0091/0118).
            retriever=build_retriever(
                run_config.context,
                memory=memory,
                agent_id=args.agent_id,
                knowledge=knowledge,
            ),
            reflection=run_config.reflection,
            reflection_model=run_config.reflection_model,
            agent_id=args.agent_id,
        )

    try:
        inputs = load_inputs(Path(args.inputs), prefix=args.prefix)
        outcome = bootstrap(
            Path(args.corpus),
            graph,
            services,
            inputs=inputs,
            now=datetime.now(UTC),
            agent_id=args.agent_id,
            # The same file `aef loop cycle --memory` reads. Durable, because
            # the point of it is that the reflection outlives this process:
            # ADR 0139 measured that a bootstrapped repo still could not
            # propose anything, and this was the missing half.
            memory_sink=(
                FileMemoryStore(path=Path(args.memory)) if getattr(args, "memory", None) else None
            ),
        )
    except (BootstrapError, RecorderError, CorpusError) as exc:
        # `CorpusError` too: `refuse_existing_ids` calls `load_corpus`, so ONE
        # malformed file anywhere in the corpus makes bootstrap fail. It
        # landed in `main()`'s catch-all, which prints the message but returns
        # 1 rather than this command's own EXIT_REJECTED, and the message
        # named no file. Both fixed here and in `corpus.load_scenario`
        # (ADR 0141's suspected item, confirmed as a naming defect rather than
        # the traceback it was reported as).
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REJECTED

    for line in outcome.lines:
        print(line)
    for command in outcome.tripwire_commands(args.module, args.corpus, inputs):
        print(f"    {command}")
    # A command that recorded nothing did not seed a corpus, and a workflow
    # keying off exit 0 would believe it had. The ready loop's rule: the
    # tooling does not report green for something that did not happen.
    return EXIT_OK if outcome.recorded else EXIT_REJECTED


def _parse_checks(raw: list[str] | None) -> tuple[TaskCheck, ...]:
    """`--check` values: each a JSON object or a JSON list of objects.
    Malformed checks fail here, before anything runs — a check that loaded
    wrong would score the scenario 0 forever and read as a regression."""
    checks: list[TaskCheck] = []
    for item in raw or ():
        parsed = json.loads(item)
        entries = parsed if isinstance(parsed, list) else [parsed]
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"a check must be a JSON object, got {type(entry).__name__}")
            checks.append(TaskCheck.from_payload(entry))
    return tuple(checks)


def _score_attribution(result: dict[str, Any]) -> dict[str, object]:
    """Why one scenario scored what it did — `{}` when nothing went wrong.

    `run_scenario` already computes both halves: a `failure` string when the
    run raised, and the list of checks that did not hold when it ran cleanly
    and answered wrongly. `loop score` emitted NEITHER, so a `0.0000` meaning
    "the provider died" and a `0.0000` meaning "the answer was wrong" were
    indistinguishable in the report, and telling them apart in ADR 0156
    required inferring from split-level token accounting. They are different
    facts about a candidate and the report now says which one it is.
    """
    detail: dict[str, object] = {}
    if result.get("failure"):
        detail["failure"] = result["failure"]
    if result.get("paused"):
        detail["hitl_paused"] = result["paused"]
    checks = result.get("checks")
    if isinstance(checks, dict):
        failed = list(checks.get("failures", ()))
        if failed:
            detail["checks"] = f"{checks.get('passed')}/{checks.get('total')} passed"
            detail["checks_failed"] = failed
    if result.get("budget_exceeded"):
        detail["budget_exceeded"] = True
    return detail


def cmd_score(args: argparse.Namespace) -> int:
    """The task metric, read directly (ADR 0113): the incumbent graph over
    the corpus, one scalar per split with its honest statistics, and — with
    `--repeat` — the variance of that scalar across identical runs, which is
    the noise floor any claimed improvement has to clear.

    Runs in-process: this scores a graph the OWNER trusts (the incumbent),
    never a candidate. Candidates are scored by `loop gate`, isolated.
    Holdout is excluded unless asked for by name — it is the owner's only
    independent read, and reading it casually spends it.
    """
    from aef.harness.evaluation import ScoreSet
    from aef.harness.memory_store import FileMemoryStore
    from aef.harness.scenario_runner import run_scenario

    corpus = load_corpus(Path(args.corpus))
    # A metric read over a corpus that lost its failing cases is not the same
    # metric. Reproduced: delete the two scenarios the agent fails and this
    # command reports 0.6667 -> 1.0000, exit 0, with nothing complaining
    # (ADR 0141). `corpus.check_never_shrinks` had no production caller at
    # all; the gates now run it too, in `harness/loop.py::_preflight`.
    #
    # Retiring a scenario deliberately means editing `corpus/manifest.json`,
    # which is a visible, reviewable act in git — not something a deletion
    # can do silently.
    try:
        check_never_shrinks(corpus, load_manifest(Path(args.corpus)))
    except CorpusShrankError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    graph = load_graph_reference(args.entrypoint)
    splits = [Split(s) for s in args.splits.split(",")]
    if Split.HOLDOUT in splits and not args.i_am_spending_the_holdout:
        print(
            "refusing to score the holdout split without --i-am-spending-the-holdout",
            file=sys.stderr,
        )
        return EXIT_REJECTED

    # Model calls replay from each scenario's cassette (ADR 0123). Under
    # --cassette-miss live the provider in --config answers what the
    # recording never saw, and the report says how many times it did: a
    # score with live misses is a different measurement from a replayed one.
    cassette_miss = getattr(args, "cassette_miss", "fail")
    # Same refusal as the three turn-running commands, for the same reason:
    # with no `--config` there is no provider, so `--cassette-miss live`
    # means "every call misses" and the report's own mode line would still
    # read LIVE (ADR 0191's F1, one command over).
    refusal = _require_config_for_live_cassette(args, "score")
    if refusal is not None:
        return refusal
    live_provider = None
    # The OWNER'S policy, from the owner's own `aef.yaml` — the same one
    # `aef run --config` applies and the same one `_policy_from_base_ref`
    # hands the gates. This read `None`, so `loop score` judged every tool
    # call deny-by-default while both other paths allowed it: reproduced at
    # 0.0 here against 1.0 there, on identical code and one config file
    # (ADR 0125). Two lists that must agree, with nothing checking that they
    # did — ADR 0091's finding, in a third place.
    #
    # Read through `build_run_config` — `aef run`'s own site — for the same
    # reason `record` and `bootstrap` do. This was the THIRD private parse of
    # `aef.yaml` inside this one file, and while it happened to build the
    # policy correctly it skipped `build_domain_gates`, so `loop score
    # --config` was the one command that would score a whole corpus under a
    # config whose evaluator suites do not resolve (ADR 0149).
    from aef.cli.run import build_run_config

    run_config = build_run_config(getattr(args, "config", None))
    score_policy = run_config.policy_config
    # Only under `live`: a replayed score must not be mistaken for a live one,
    # and the provider is built either way now (cheaply, and with no
    # credential read — ADR 0123), so the gate stays on the USE, not the
    # construction.
    if cassette_miss == "live":
        live_provider = run_config.model_provider

    # Only the scenarios recorded FROM this graph. A corpus may hold several
    # graphs' recordings (the demo's and the summary agent's); scoring one
    # graph against another's scenarios measures nothing about either.
    other_graph = sorted(s.id for s in corpus.scenarios if s.graph_id != graph.id)

    # `--memory`: the durable store a FAILED OWNER CHECK is recorded into,
    # through ADR 0180's one idempotent producer (ADR 0182, K3-5).
    #
    # ADR 0174 gave the check-failure producer to `bootstrap` and refused it
    # to every GATE path, and that refusal stands: a gate that wrote to the
    # adopter's store would let scoring a candidate manufacture the next one's
    # evidence. But wiring it into bootstrap ALONE let staleness walk a lesson
    # out of the prompt while nothing ever re-saw it — K2's S1b measured
    # `runs_since_last_seen` climbing to 17 by the seventeenth scenario, and
    # 0 with the producer on the scored split.
    #
    # `aef loop score` is the one caller that is neither: it scores the
    # INCUMBENT the owner already trusts, in-process, over the owner's own
    # corpus, with the owner naming the file. So this is opt-in, off by
    # default, and the isolated path is untouched — see the flag's help.
    memory_store = (
        FileMemoryStore(path=Path(args.memory)) if getattr(args, "memory", None) else None
    )

    runs: list[dict[str, ScoreSet]] = []
    # split -> scenario id -> why it scored what it did. First repeat only, to
    # match `per_scenario`, which is also read off the first run.
    attribution: dict[str, dict[str, dict[str, object]]] = {}
    hits = misses = 0
    for repeat_index in range(args.repeat):
        per_split: dict[str, ScoreSet] = {}
        for split in splits:
            scenarios = tuple(s for s in corpus.split(split) if s.graph_id == graph.id)
            per_scenario: dict[str, float] = {}
            cost = 0
            for scenario in scenarios:
                result = run_scenario(
                    scenario,
                    graph,
                    score_policy,
                    cassette_miss=cassette_miss,
                    live_provider=live_provider,
                    # `record_check_outcomes` is idempotent on
                    # (run_id, signature), so `--repeat 5` and a second
                    # `aef loop score` over the same corpus both leave ONE
                    # record per failed check (ADR 0180).
                    memory=memory_store,
                )
                per_scenario[scenario.id] = float(result["score"])
                cost += int(result["cost_tokens"])
                stats = result.get("cassette", {})
                hits += int(stats.get("hits", 0))
                misses += int(stats.get("misses", 0))
                if repeat_index == 0:
                    why = _score_attribution(result)
                    if why:
                        attribution.setdefault(split.value, {})[scenario.id] = why
            per_split[split.value] = ScoreSet(
                label=split.value, per_scenario=per_scenario, cost_tokens=cost
            )
        runs.append(per_split)

    report: dict[str, object] = {
        "entrypoint": args.entrypoint,
        "graph_id": graph.id,
        "repeat": args.repeat,
        "skipped_other_graph": other_graph,
        "cassette": {"on_miss": cassette_miss, "hits": hits, "misses": misses},
    }
    for split in splits:
        sets = [run[split.value] for run in runs]
        first = sets[0]
        if first.n == 0:
            report[split.value] = {"n": 0}
            continue
        means = [s.mean for s in sets]
        spread = max(means) - min(means)
        lo, hi = first.confidence_interval_95
        with_checks = sum(1 for s in corpus.split(split) if s.checks and s.graph_id == graph.id)
        report[split.value] = {
            "n": first.n,
            "with_checks": with_checks,
            "mean": round(first.mean, 4),
            "stdev": round(first.stdev, 4),
            "ci95": [round(lo, 4), round(hi, 4)],
            "cost_tokens": first.cost_tokens,
            "per_scenario": {k: round(v, 4) for k, v in sorted(first.per_scenario.items())},
            # Across identical runs. Anything an increment claims must exceed this.
            "repeat_mean_spread": round(spread, 6),
            # WHY each sub-1.0 score is what it is. Absent keys mean nothing
            # went wrong; a scenario appears here only if it has something to
            # say (ADR 0166).
            "attribution": attribution.get(split.value, {}),
        }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return EXIT_OK
    print(f"task metric — {args.entrypoint} ({graph.id}) — repeat={args.repeat}")
    mode = "LIVE" if misses and cassette_miss == "live" else "replayed"
    print(
        f"  model calls: {hits} cassette hit(s), {misses} miss(es), "
        f"on_miss={cassette_miss} — {mode}"
    )
    if other_graph:
        print(f"  skipped {len(other_graph)} scenario(s) recorded from another graph")
    for split in splits:
        row = report[split.value]
        assert isinstance(row, dict)
        if row["n"] == 0:
            print(f"  {split.value:<11} n=0")
            continue
        print(
            f"  {split.value:<11} n={row['n']:<3} with_checks={row['with_checks']:<3} "
            f"mean={row['mean']:.4f} stdev={row['stdev']:.4f} "
            f"ci95=[{row['ci95'][0]:.4f}, {row['ci95'][1]:.4f}] "
            f"repeat_spread={row['repeat_mean_spread']:.6f}"
        )
        why_by_id = attribution.get(split.value, {})
        for sid, score in row["per_scenario"].items():
            print(f"      {score:.4f}  {sid}")
            why = why_by_id.get(sid, {})
            if "failure" in why:
                print(f"                raised: {why['failure']}")
            if "hitl_paused" in why:
                print(f"                paused: {why['hitl_paused']}")
            failed_checks = why.get("checks_failed", [])
            if isinstance(failed_checks, list):
                for line in failed_checks:
                    print(f"                check failed: {line}")
            if why.get("budget_exceeded"):
                print("                budget_ms exceeded")
    return EXIT_OK


def cmd_harvest(args: argparse.Namespace) -> int:
    from aef.harness.harvest import harvest

    # Harvest writes to corpus/, which IS the evidence every behavioural gate
    # is measured against. A halted loop must not have its gate evidence
    # changed underneath it (ADR 0069).
    config = _config(args)
    try:
        config.paths.kill_switch.check()
    except PolicyConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    except LoopHaltedError as exc:
        print(f"HALTED: {exc}")
        return EXIT_HALTED

    outcome = harvest(
        Path(args.runs),
        Path(args.corpus),
        load_graph_reference(args.module),
        now=datetime.now(UTC),
        include_successes=args.include_successes,
        daily_limit=args.daily_limit,
    )
    for line in outcome.lines:
        print(line)
    return EXIT_OK


def _halt_notifier() -> object:
    """Read the halt webhook from the environment, at the CLI boundary.

    Never read inside the harness: the URL is owner configuration that lives
    outside this repository, and a module that reaches for the environment
    itself is one the candidate is closer to influencing.
    """
    import os

    from aef.harness.monitoring import HaltNotifier

    return HaltNotifier(webhook_url=os.environ.get("AEF_HALT_WEBHOOK") or None)


# argparse's own code for a usage error, and deliberately not `EXIT_REJECTED`.
# `cmd_cycle`'s comment below already says why: a missing flag is a
# configuration error, and reporting it as a verdict on a candidate makes CI
# retry it forever (ADR 0075). It shares the number with `EXIT_HALTED`, which
# is fine and was checked rather than assumed — every caller of exit 2 is told
# "stop, do not retry, a human must look", and that is exactly the right
# instruction for an invocation that cannot work. The two are distinguishable
# in the output: a halt prints `HALTED:` on stdout, this prints `error:` on
# stderr, as argparse's own usage errors do.
EXIT_USAGE = 2


def _require_memory_flag(args: argparse.Namespace, command: str = "cycle") -> int | None:
    """One of `--memory` / `--no-memory`, or refuse. `None` means proceed.

    This is the `--state` / `--no-loop-state` pattern from ADR 0141, applied
    to the flag whose absence made this repo's own nightly cycle a no-op.
    `.github/workflows/loop-monitor.yml` ran `aef loop cycle` daily with no
    `--memory`, so `cycle` took the `memory=None` branch, printed "no memory
    store configured: nothing to learn from, no candidate", and **exited 0** —
    every night since the workflow was written, with nothing saying so
    (reproduced, ADR 0165). That is ADR 0139's failure shape, scheduled.

    Enforced HERE and not only in the parser, deliberately: L6 found a
    parser-level control that a handler-level caller walked straight past, and
    `harness.loop.cycle()` is importable by anything. A guard that only binds
    when the argument vector is the thing being checked is not a guard.

    **`command` is a parameter because ADR 0165 fixed one of two commands.**
    `aef loop run` reads the same flag through the same `FileMemoryStore(...)
    if args.memory else None` expression and had no guard at all: five
    `loop run --turns 2` invocations without `--memory` each exited 0 having
    proposed nothing, and `loop monitor` reported `cycles run: 0 (last never)`
    with no warning — the "unstarted versus dead" ambiguity 0165 §2 says it
    removed, one subcommand over (reproduced, ADR 0167).
    `tests/cli/test_loop_turn_commands.py` now derives the list of
    turn-running subcommands from this module's AST, so a third twin cannot
    be added without the test naming it.
    """
    if getattr(args, "memory", None) or getattr(args, "no_memory", False):
        return None
    print(
        f"error: a {command} without memory cannot propose — with no recorded failures the "
        f"proposer has nothing to ground in, so the {command} prints 'no memory store "
        f"configured: nothing to learn from, no candidate' and exits 0, which reads as "
        f"success (ADR 0139). Pass --memory <file> — the SAME file `aef loop bootstrap "
        f"--memory` and the reflect node write — or pass --no-memory to say you mean "
        f"that. Neither was given, and silence used to mean --no-memory: this repo's own "
        f"scheduled cycle was a no-op every night (ADR 0165), and `aef loop run` was the "
        f"same command with no guard at all (ADR 0167).",
        file=sys.stderr,
    )
    return EXIT_USAGE


def _require_config_for_live_cassette(args: argparse.Namespace, command: str) -> int | None:
    """`--cassette-miss live` needs a `--config`, or refuse. `None` proceeds.

    The provider a live miss falls through to is read from `--config`'s
    `model_provider`, at the base ref, and there is no other source for it.
    Without the flag there is no provider, so EVERY model call in the corpus
    misses and raises `ModelProviderError: ... no live provider to fall
    through to` — and under ADR 0185 each of those classified as a *dead
    call*, was retried (the corpus ran twice), and was then EXCLUDED from
    G3's comparison rather than scored. The same per-scenario numbers gave
    `G3 FAIL` counted and `G3 PASS` excluded, and past the refusal floor G3
    said "the model was not answering" when the truth was "you omitted
    --config" (reproduced, ADR 0191's F1).

    `EXIT_ERROR`, not `EXIT_USAGE` and not `EXIT_REJECTED`: a nightly job's
    rule is `>= 2` fails, and of the two codes above 1 this is the one whose
    remedy is "fix the invocation" rather than "release the kill switch"
    (ADR 0167 §6, ADR 0178). The harness refuses too — see
    `loop.LiveGatingWithoutConfigError` — so a library caller cannot walk
    past this the way ADR 0165's caller walked past a parser-level guard;
    this one exists to make the message arrive before anything is journalled.
    """
    if getattr(args, "cassette_miss", "fail") != "live":
        return None
    if getattr(args, "config", None):
        return None
    print(
        f"error: `{command} --cassette-miss live` needs --config. The provider a live "
        f"cassette miss falls through to is read from that file's model_provider block (at "
        f"the base ref, so a candidate cannot choose its own), and there is no other source "
        f"for it. With no --config there is no provider, every model call misses with 'no "
        f"live provider to fall through to', and those misses are excluded from the "
        f"comparison as dead calls — so a run in which the model was never reached reports "
        f"as one the model answered (ADR 0191). Pass --config <path to aef.yaml, relative "
        f"to the repository root> with gates.live_model_calls: true, or drop --cassette-miss "
        f"live and score from the recorded cassettes.",
        file=sys.stderr,
    )
    return EXIT_ERROR


def _agent_path_is_defaulted(args: argparse.Namespace) -> bool:
    """Was `--agent-path` left at `DEFAULT_AGENT_PATH`?

    When it was, the path is this package's guess about a repo it has not
    looked at, and a diagnostic that reports on one file out of nine is how
    ADR 0168's false pass happened — obligation 6 answered about the call-site
    stub while eight generated graphs went unopened. An owner who types the
    default explicitly gets the wider scan too: it is a superset, and the
    named path is still scanned inside it.
    """
    return str(getattr(args, "agent_path", DEFAULT_AGENT_PATH)) == DEFAULT_AGENT_PATH


def _require_agent_path_under_root(args: argparse.Namespace, command: str) -> int | None:
    """`--agent-path` must name a file inside `--agent-root`. `None` = proceed.

    `DEFAULT_AGENT_PATH` lives under `DEFAULT_AGENT_ROOT`. Widen the root —
    `--agent-root .claude/agents`, which is exactly what `aef migrate` tells a
    prompt-file repo to do (ADR 0152) — and leave `--agent-path` at its
    default, and the two describe different trees: the default path is then
    **Zone C** under the root in force, so the loop may not propose changes to
    it, `bless` would not archive it, and every obligation `doctor` reports is
    about a file the loop cannot touch.

    Reproduced (ADR 0167): with the root widened and the path left alone,
    `loop doctor` reported six obligations about `agents/migrated/graph.py`
    and printed a `bless` fix line that drops `--agent-root` entirely, and
    `loop cycle` exited **0** with "the proposer produced nothing from the
    available evidence". Neither said the two flags disagreed.

    Only checked when the root is non-default. Under the default root this
    would be a new refusal on invocations no finding here reproduced a problem
    with, and strengthening a control is an owner's decision rather than a fix
    wave's side effect (ADR 0141's rule, applied to itself).
    """
    from aef.harness.zones import Zone, inspect_path

    root = getattr(args, "agent_root", DEFAULT_AGENT_ROOT)
    path = getattr(args, "agent_path", None)
    if not path or root == DEFAULT_AGENT_ROOT:
        return None
    verdict = inspect_path(path, ZonePolicy(agent_root=root))
    if verdict.zone is Zone.A:
        return None

    repo = Path(getattr(args, "repo", "."))
    try:
        found = sorted(
            p.relative_to(repo).as_posix() for p in (repo / root).glob("migrated/*/graph.py")
        )
    except (OSError, ValueError):  # pragma: no cover - a glob over a missing dir yields nothing
        found = []
    suggestion = (
        "one of " + ", ".join(found[:3]) + ("..." if len(found) > 3 else "")
        if found
        else f"{root}/migrated/<agent>/graph.py — the per-agent path `aef migrate` printed"
    )
    left_at_default = (
        " It was left at its default, and that default names a file under the DEFAULT "
        f"root {DEFAULT_AGENT_ROOT!r}."
        if path == DEFAULT_AGENT_PATH
        else ""
    )
    print(
        f"error: `loop {command}` was given --agent-root {root!r} and --agent-path "
        f"{path!r}, and {path!r} is not inside {root!r}.{left_at_default} {verdict.reason}. "
        f"Zone A is the only tree the loop may propose changes to and the only tree "
        f"`aef loop bless` archives, so proceeding would report obligations about — or "
        f"bless — a file this loop cannot touch. Pass --agent-path naming a graph under "
        f"{root!r}: {suggestion}.",
        file=sys.stderr,
    )
    return EXIT_USAGE


def _require_module_for_runs(args: argparse.Namespace, command: str = "cycle") -> int | None:
    """`--runs` needs `--module`, or refuse. `None` means proceed.

    The harvest leg of a turn runs only when a graph OBJECT was built, and
    `cmd_cycle` builds one from `--module` alone:

        graph = load_graph_reference(args.module) if args.module else None

    while `harness/loop.py::cycle` gates its harvest on `runs_dir is not None
    and corpus_root is not None and graph is not None`. So `--runs` given with
    `--entrypoint` and no `--module` — the spelling a widened-root prompt repo
    uses, and the spelling ADR 0163's pilot and ADR 0181 both used — is
    ACCEPTED, its path is validated, and nothing happens. Reproduced on two
    invocations one flag apart, both `--no-memory`: arm A printed no harvest
    line and exited 0 with five recorded runs sitting at the path it was
    given; arm B, plus `--module`, printed `promoted 0 run(s) to the train
    split` and listed them (ADR 0163's F-M6-3, closed in ADR 0190).

    That is ADR 0139's shape — exit 0 having done nothing — and ADR 0176's
    "two spellings of which graph" in a fourth place. Refusing is the fix
    rather than silently deriving a module from `--entrypoint`, because the
    two flags do not have to name the same graph and guessing which one the
    owner meant is how a run gets harvested against the wrong entrypoint.

    **`EXIT_ERROR`, not `EXIT_USAGE`.** Under the loop's exit vocabulary (0
    verdict / 1 REJECTED / 2 HALTED / 3 ERROR, ADR 0182's K3-1), 2 is what the
    rendered nightly workflow reads as a halt and 3 is what it summarises as
    "fix the invocation" — which is exactly this. The two refusals above it in
    `cmd_cycle` return `EXIT_USAGE`; unifying the three is ADR 0182's own open
    item 3 and an owner's decision, not this fix's.
    """
    if not getattr(args, "runs", None) or getattr(args, "module", None):
        return None
    print(
        f"error: `loop {command}` was given --runs but no --module, and --runs does "
        f"nothing without it. The harvest leg needs a graph OBJECT to re-execute each "
        f"recorded run against, and this command builds one from --module only — "
        f"--entrypoint is read by the gates' scenario runner, in a subprocess, and "
        f"never becomes the graph the harvest leg is handed. Without --module the path "
        f"is validated, the runs are never read, and the turn exits 0 having silently "
        f"skipped the step it was asked to perform (ADR 0139's shape; reproduced in ADR "
        f"0163's F-M6-3). Pass --module <the same graph>, or drop --runs to say you did "
        f"not want the harvest leg.",
        file=sys.stderr,
    )
    return EXIT_ERROR


def _journal_turn(
    state_root: Path, *, at: datetime, proposed: bool, verdict: str, command: str
) -> None:
    """One line in `<state>/cycles.jsonl`, from whichever command ran a turn.

    Takes the state ROOT rather than a `LoopConfig`, because the failures most
    worth journalling include the ones where `_config` itself raised.
    """
    from aef.harness.monitoring import record_cycle_attempt

    record_cycle_attempt(state_root, at=at, proposed=proposed, verdict=verdict, command=command)


def cmd_cycle(args: argparse.Namespace) -> int:
    from aef.harness.loop import cycle as loop_cycle
    from aef.harness.memory_store import FileMemoryStore

    refusal = _require_memory_flag(args)
    if refusal is not None:
        return refusal
    refusal = _require_agent_path_under_root(args, "cycle")
    if refusal is not None:
        return refusal
    refusal = _require_module_for_runs(args, "cycle")
    if refusal is not None:
        return refusal
    refusal = _require_config_for_live_cassette(args, "cycle")
    if refusal is not None:
        return refusal

    # Journalled in a `finally`, whatever happened — including the paths that
    # raise. `record_cycle_attempt` used to sit AFTER this `try`, so a turn
    # that died on a halt or a config error returned without journalling
    # anything: a nightly cycle failing the same way every night left a
    # `cycles.jsonl` byte-identical to one nobody had ever run, which is the
    # exact ambiguity the journal exists to remove (reproduced, ADR 0167).
    #
    # `_config` and `load_graph_reference` are INSIDE the try, because that is
    # where a bad `--config`, an unreadable corpus and the import error from
    # `agents.migrated.graph`'s `NotImplementedError` placeholder all live —
    # the failures a scheduled loop is most likely to repeat every night.
    proposed = False
    journal = True
    verdict = "the cycle did not complete and recorded no verdict"
    state_root = Path(args.state)
    derived: str | None = None
    try:
        # BEFORE `_config`, which freezes `graph_id` into `LoopConfig`. Inside
        # the `try` so a malformed corpus is journalled like every other way
        # this command can die (ADR 0167).
        resolution = resolve_graph_id_from_corpus(args)
        if resolution.note is not None:
            print(f"  {resolution.note}")
        derived = resolution.derived
        config = _config(args)
        _warn_unmet_obligations(args, config)
        graph = load_graph_reference(args.module) if args.module else None
        run = loop_cycle(
            config,
            now=datetime.now(UTC),
            workdir=Path(args.workdir),
            runs_dir=Path(args.runs) if args.runs else None,
            corpus_root=Path(args.corpus) if args.corpus else None,
            graph=graph,
            # Durable, not in-process: a store constructed here would be
            # empty every invocation and the proposer would never see a
            # recorded failure (ADR 0069).
            # `Path(None)` raises TypeError, which main() catches and turns
            # into exit 1 — the code that means "this candidate is no good".
            # A missing flag is a configuration error and must not be
            # reported as a verdict on a candidate, or CI retries it forever
            # (ADR 0075). None reaches the driver's own explicit refusal.
            memory=FileMemoryStore(path=Path(args.memory)) if args.memory else None,
            agent_path=args.agent_path,
        )
    except LoopStateInsideRepoError as exc:
        # The one failure that must NOT be journalled: the journal lives under
        # `--state`, so writing it would create the very directory just
        # refused. Caught before the generic handler for that reason alone.
        print(f"error: {exc}", file=sys.stderr)
        journal = False
        return EXIT_ERROR
    except GraphIdError as exc:
        # An ambiguous or unknown graph id is a fault in the INVOCATION, not a
        # verdict on a candidate. The second blind re-score (ADR 0188) ran
        # this repo's own nightly command against its two-graph corpus: exit
        # 1, which the workflow's own case statement reads as "a rejection —
        # the system working". It was a broken job reported as a healthy one.
        print(f"error: {exc}", file=sys.stderr)
        verdict = f"error ({type(exc).__name__}): {exc}"
        return EXIT_ERROR
    except (PolicyConfigError, CorpusGraphMismatchError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        verdict = f"error ({type(exc).__name__}): {exc}"
        return EXIT_REJECTED
    except LoopHaltedError as exc:
        print(f"HALTED: {exc}")
        verdict = f"HALTED ({type(exc).__name__}): {exc}"
        return EXIT_HALTED
    except Exception as exc:
        # Named, journalled, and reported as **EXIT_ERROR**, not as a
        # rejection. `main()`'s catch-all returns 1 for any exception and 1 is
        # also "this candidate is no good", so a nightly workflow whose rule is
        # `status >= 2` read a broken config, a missing corpus, an import
        # error and the `agents.migrated.graph` placeholder's
        # `NotImplementedError` as a healthy rejection and stayed green — for
        # as many nights as it took someone to look (reproduced, ADR 0167).
        print(f"error ({type(exc).__name__}): {exc}", file=sys.stderr)
        verdict = f"error ({type(exc).__name__}): {exc}"
        return EXIT_ERROR
    else:
        for line in run.lines:
            print(f"  {line}")
        # Beside the verdict, because the evidence this turn gated on is what
        # the note is about (ADR 0179's open item, closed in ADR 0182).
        _print_containment_summary(args)

        # One line saying what this turn actually produced, in words, because
        # "  no memory store configured..." three lines up in a CI log is not
        # a verdict anyone reads. This is the line the workflow tees into
        # $GITHUB_STEP_SUMMARY.
        proposed = run.proposed is not None
        verdict = (
            f"proposed {run.proposed} — {run.decision or 'no decision recorded'}"
            if run.proposed is not None
            else (run.lines[-1] if run.lines else "nothing to report")
        )
        if len(run.attempts) > 1:
            # In the VERDICT, not only in the lines above it: a turn that
            # gated three candidates and kept one spent three times the corpus
            # passes, and a journal that records only the winner reads exactly
            # like a turn that tried once (ADR 0200).
            verdict = f"{verdict} [best of {len(run.attempts)} gated candidate(s)]"
        if run.audit is not None:
            verdict = f"{verdict} [{run.audit.line()}]"
        if not getattr(args, "memory", None):
            verdict = f"{verdict} (--no-memory was passed: this cycle could not propose)"
        if derived is not None:
            # In the VERDICT line, not only three lines up: which graph's
            # evidence this turn was allowed to read is part of what the turn
            # decided, and the verdict is the line a workflow tees into its
            # step summary.
            #
            # It says "evidence graph id" rather than "--graph-id" since ADR
            # 0182: the archive key is no longer what moved, and a summary
            # naming the flag would send a reader to check an archive that had
            # not changed.
            verdict = f"{verdict} [evidence graph id derived from --corpus: {derived!r}]"
        print(f"cycle verdict: {verdict}")
        return run.exit_code
    finally:
        # Journalled whatever happened, including the nothing. `cycle` writes
        # a ledger entry only when it proposes, so a loop that produces
        # nothing leaves a ledger indistinguishable from a loop nobody has
        # ever run — which is precisely how a nightly no-op stayed invisible
        # (ADR 0165), and how a nightly halt stayed invisible after it
        # (ADR 0167).
        if journal:
            _journal_turn(
                state_root,
                at=datetime.now(UTC),
                proposed=proposed,
                verdict=verdict,
                command="cycle",
            )


def cmd_run(args: argparse.Namespace) -> int:
    """autoresearch's loop inside the gates (ADR 0114): N turns or a
    wall-clock budget, keep on a LOCAL branch, never main.

    Same two controls as `cycle`, for the same reason and one wave later.
    This command read `FileMemoryStore(...) if args.memory else None` with no
    guard and journalled nothing at all, so five `loop run --turns 2`
    invocations without `--memory` each exited 0 having done nothing and left
    `loop monitor` saying `cycles run: 0 (last never)` with no warning
    (reproduced, ADR 0167). ADR 0165 fixed one of the two commands that run a
    turn; this is the other.

    **One journal entry per TURN, not per invocation.** The staleness alarm
    counts consecutive attempts that proposed nothing, and each turn is a
    separate chance to propose: ten quiet turns recorded as one attempt would
    need thirty turns to reach a threshold meant to fire after three.
    """
    from aef.harness.loop import run_loop
    from aef.harness.memory_store import FileMemoryStore

    refusal = _require_memory_flag(args, "run")
    if refusal is not None:
        return refusal
    refusal = _require_agent_path_under_root(args, "run")
    if refusal is not None:
        return refusal
    refusal = _require_config_for_live_cassette(args, "run")
    if refusal is not None:
        return refusal

    # Inside the try for the same reason as `cmd_cycle`: a bad `--config`, an
    # unreadable corpus and the `agents.migrated.graph` placeholder's
    # `NotImplementedError` are the failures a scheduled loop repeats, and
    # every one of them happens here (ADR 0167).
    state_root = Path(args.state)
    try:
        # BEFORE `_config`, which freezes the evidence id into `LoopConfig` —
        # the same call, in the same position, that `cmd_cycle` has made since
        # ADR 0176. **`cmd_run` never made it**, and until ADR 0200 nothing
        # noticed, because the only thing that would notice is a `loop run`
        # against a corpus whose graph id is not the literal `"default"` —
        # which is every migrated prompt-agent repo, and which nothing in this
        # program had run unattended.
        #
        # Found BY the unattended night (ADR 0200, part C) and reproduced side
        # by side on one repo, one corpus and one memory file:
        #
        #     cycle> --graph-id not given; the evidence graph id is derived as
        #            'price-freshness-reviewer' … proposed cycle-…-prompt
        #     run>   2 memory record(s) excluded as belonging to a graph other
        #            than 'default'
        #     run>   no admissible failure memory … no candidate this cycle
        #     run>   stopped: turn 1 produced no candidate
        #
        # `aef loop run` — the multi-turn driver, the one a night uses — could
        # not propose at all on an adopting repo. ADR 0165's shape exactly: one
        # of the two turn-running commands fixed, the other left, and the
        # difference invisible because the broken one exits 0.
        resolution = resolve_graph_id_from_corpus(args)
        if resolution.note is not None:
            print(f"  {resolution.note}")
        config = _config(args)
        # The ONE loader, like the other five (ADR 0176 F3, completed by ADR
        # 0182). This was `cli.run.load_graph_module`, which took a dotted
        # module and a file path but refused `module:factory` — the sixth
        # in-process caller and the last on the old loader — and which lacks
        # ADR 0085's `BaseException` guard, so a candidate factory raising
        # `SystemExit` exited this process cleanly.
        # `run` had the identical silent no-op ADR 0190 closed on `cycle`:
        # `--runs` with no graph reference harvested nothing and said so
        # nowhere (reported by L2, closed here).
        refusal = _require_module_for_runs(args, "run")
        if refusal is not None:
            return refusal
        graph = load_graph_reference(args.module) if args.module else None
        run = run_loop(
            config,
            now=datetime.now(UTC),
            workdir=Path(args.workdir),
            turns=args.turns,
            budget_seconds=args.budget_minutes * 60.0,
            kept_branch=args.kept_branch,
            runs_dir=Path(args.runs) if args.runs else None,
            corpus_root=Path(args.corpus) if args.corpus else None,
            graph=graph,
            memory=FileMemoryStore(path=Path(args.memory)) if args.memory else None,
            agent_path=args.agent_path,
            sample_parents=args.sample_parents,
            seed=args.seed,
            persist_lineage=not args.no_lineage,
        )
    except LoopStateInsideRepoError as exc:
        # Not journalled: the journal lives under `--state`, so writing it
        # would create the directory just refused.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except GraphIdError as exc:
        # EXIT_ERROR, beside `cmd_cycle`'s identical clause and for ADR 0188's
        # reason: an ambiguous or unknown graph id is a fault in the
        # INVOCATION, and reporting it as 1 makes a misconfigured job
        # indistinguishable from a healthy rejection to the workflow's own
        # case statement.
        print(f"error: {exc}", file=sys.stderr)
        _journal_run_failure(state_root, exc)
        return EXIT_ERROR
    except (PolicyConfigError, KeptBranchCheckedOutError, CorpusGraphMismatchError) as exc:
        # Named refusals, printed as one line rather than a traceback: each
        # names an operator action (stand somewhere else; pass --graph-id).
        print(f"error: {exc}", file=sys.stderr)
        _journal_run_failure(state_root, exc)
        return EXIT_REJECTED
    except LoopHaltedError as exc:
        print(f"HALTED: {exc}")
        _journal_run_failure(state_root, exc, halted=True)
        return EXIT_HALTED
    except Exception as exc:
        # EXIT_ERROR, not `main()`'s catch-all 1 — see `cmd_cycle`.
        print(f"error ({type(exc).__name__}): {exc}", file=sys.stderr)
        _journal_run_failure(state_root, exc)
        return EXIT_ERROR
    for line in run.lines:
        print(f"  {line}")
    print(
        f"kept {run.kept_count}, reverted {run.reverted_count}, "
        f"{run.kept_branch}@{run.kept_ref[:12]} — review and merge by hand; "
        f"Tier-1 auto-merge is off"
    )
    # The archive numbers, printed rather than left in a dataclass nobody
    # reads: distinct KEPT trees is the diversity number `--sample-parents`
    # is judged on, and distinct GATED trees is how much search it bought.
    print(
        f"archive: {len(run.archive)} member(s) "
        f"({run.resumed_members} resumed from earlier run(s)), "
        f"{run.distinct_kept_trees} distinct kept tree(s), "
        f"{run.distinct_gated_trees} distinct gated tree(s); "
        f"sampling {'on' if args.sample_parents else 'off'}"
    )
    _journal_run_turns(state_root, run, chose_no_memory=not getattr(args, "memory", None))
    return EXIT_HALTED if "halted" in run.stopped_because else EXIT_OK


def _journal_run_failure(state_root: Path, exc: BaseException, *, halted: bool = False) -> None:
    """A `run` that never reached a turn still ran, and the journal must say
    so — otherwise a nightly `loop run` dying on the same halt every night
    leaves the monitor unable to tell it from a loop nobody has started."""
    prefix = "HALTED" if halted else "error"
    _journal_turn(
        state_root,
        at=datetime.now(UTC),
        proposed=False,
        verdict=f"{prefix} ({type(exc).__name__}): {exc}",
        command="run",
    )


def _journal_run_turns(state_root: Path, run: LoopRun, *, chose_no_memory: bool) -> None:
    """One entry per turn, all stamped at the moment the run finished.

    A single timestamp for the whole run is the honest one: the entries are
    written together, at the end, and nothing in `assess_cycle_staleness`
    reads anything finer than the last attempt's age. What it does read is
    the ORDER and the COUNT, and both are per turn.
    """
    turns = run.turns
    at = datetime.now(UTC)
    suffix = " (--no-memory was passed: this run could not propose)" if chose_no_memory else ""
    if not turns:
        # A run whose budget expired, or that stopped before turn 1, still
        # occupied a scheduled slot and produced nothing.
        _journal_turn(
            state_root,
            at=at,
            proposed=False,
            verdict=f"no turn ran — {run.stopped_because}" + suffix,
            command="run",
        )
        return
    for turn in turns:
        if turn.proposed is None:
            verdict = f"turn {turn.turn}: no candidate — {run.stopped_because}"
        else:
            outcome = "kept" if turn.kept else ("duplicate" if turn.duplicate else "reverted")
            decision = turn.disposition.value if turn.disposition else "no decision recorded"
            verdict = f"turn {turn.turn}: proposed {turn.proposed} — {decision} ({outcome})"
        _journal_turn(
            state_root,
            at=at,
            proposed=turn.proposed is not None,
            verdict=verdict + suffix,
            command="run",
        )


def cmd_skills(args: argparse.Namespace) -> int:
    """Draft skill proposals from the knowledge store (ADR 0117). Writes
    under --out only; never under .claude/, agents/ or any harness dir, and
    never over an existing draft."""
    from aef.harness.memory_store import FileMemoryStore
    from aef.harness.skills import SkillProposalError, propose_skills
    from aef.services.knowledge.consolidate import RuleBasedConsolidator
    from aef.services.knowledge.in_memory import InMemoryKnowledgeStore

    # The knowledge store is rebuilt from the durable memory store, because
    # the consolidator is a stateless recompute (ADR 0110) and no file-backed
    # knowledge store exists yet — the memory file IS the evidence.
    knowledge = InMemoryKnowledgeStore()
    RuleBasedConsolidator().consolidate(
        FileMemoryStore(path=Path(args.memory)), knowledge, agent_id=args.agent_id
    )
    try:
        proposals = propose_skills(
            knowledge,
            Path(args.out),
            agent_id=args.agent_id,
            min_occurrences=args.min_occurrences,
        )
    except SkillProposalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    for p in proposals:
        state = "wrote" if p.written else "kept existing"
        print(f"  {state} {p.path}  ({p.entry.occurrence_count} run(s))")
    print(
        f"{sum(p.written for p in proposals)} proposal(s) written, "
        f"{sum(not p.written for p in proposals)} left as-is. Nothing is adopted until a "
        f"person moves a draft under .claude/skills/."
    )
    return EXIT_OK


def _refuse_missing_base_ref(config: LoopConfig) -> int | None:
    """`EXIT_ERROR` and a named refusal when `--base` names no ref here.

    `cycle`, `gate`, `run` and `monitor` reach `harness.loop._preflight`,
    which asks the same question of the same function. `bless` and `doctor`
    do not reach it at all — and `doctor` is the readiness command, so a repo
    whose base ref does not exist was reported READY and then no-opped at exit
    0 on the next step (ADR 0187 F-M8-1, closed in 0189). Asking here as well
    is the cheapest way for the answer to be the same in all six.
    """
    try:
        require_base_ref(config.repo, config.base_ref)
    except BaseRefError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return None


def cmd_bless(args: argparse.Namespace) -> int:
    from aef.harness.preflight import BlessError, bless

    refusal = _require_agent_path_under_root(args, "bless")
    if refusal is not None:
        return refusal
    config = _config(args)
    refusal = _refuse_missing_base_ref(config)
    if refusal is not None:
        return refusal
    try:
        config.paths.kill_switch.check()
    except PolicyConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    except LoopHaltedError as exc:
        print(f"HALTED: {exc}")
        return EXIT_HALTED
    try:
        entry = bless(
            repo_root=Path(args.repo),
            state_root=config.paths.root,
            agent_path=args.agent_path,
            # Recorded INTO the archive entry from here on. A baseline is the
            # whole Zone A tree, so which tree it is is part of what was
            # blessed — and until ADR 0167 nothing wrote it down, so a
            # baseline blessed under `--agent-root .claude/agents` and a
            # later cycle at the default root compared two different trees
            # and charged the first candidate 1.000 drift (reproduced).
            agent_root=args.agent_root,
            graph_id=graph_id(args),
            at=datetime.now(UTC),
            note=args.note,
        )
    except BlessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    except ArchiveError as exc:
        # `archive.versions`/`record` refuse a `--graph-id` that is not one
        # safe path segment (ADR 0168). Only `BlessError` was caught here, so
        # a hand-typed `--graph-id ../x` reached `main()`'s catch-all and was
        # reported as exit 1 — the code that means "the candidate was
        # rejected, the system is working". It is a configuration error: name
        # it, and exit 3 so the nightly rule fails the job (ADR 0167).
        print(f"error (invalid --graph-id): {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"blessed {args.agent_path} as baseline v{entry.version} for graph {graph_id(args)!r}")
    print("  G5 now has a reference point to measure drift against.")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    from aef.harness.preflight import preflight

    refusal = _require_agent_path_under_root(args, "doctor")
    if refusal is not None:
        return refusal
    config = _config(args)
    refusal = _refuse_missing_base_ref(config)
    if refusal is not None:
        return refusal
    try:
        result = preflight(
            repo_root=Path(args.repo),
            state_root=config.paths.root,
            corpus_root=Path(args.corpus),
            agent_path=args.agent_path,
            agent_root=args.agent_root,
            scan_all_graphs=_agent_path_is_defaulted(args),
            graph_id=graph_id(args),
            halt_channel_configured=(
                _halt_channel(config) is not None
                or bool(getattr(_halt_notifier(), "configured", False))
            ),
            observations=Path(args.observations)
            if args.observations
            else config.paths.observations,
        )
    except ArchiveError as exc:
        # Obligation 5 reads `archive.versions`, which refuses a `--graph-id`
        # that is not one safe path segment (ADR 0168). Nothing here caught
        # it, so the refusal reached `main()`'s catch-all and was reported as
        # exit 1 — the code that means "the candidate was rejected, the system
        # is working". Named here, exit 3 (ADR 0167).
        print(f"error (invalid --graph-id): {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(result.render())
    # BELOW the report and outside `Preflight`, deliberately. The six
    # obligations are things an owner must SUPPLY and each carries a `fix:`
    # that is an edit in this repo; a persona that travelled in the user turn
    # is a property of a CLI they already installed, and there is no edit that
    # gives a binary a `--system-prompt` flag. Putting it in the list would
    # make `doctor` exit 1 forever on a correctly configured Codex adopter —
    # which is ADR 0179's own finding, one surface over.
    lines = _containment_lines([*_latest_run_state(args), *_corpus_states(args)])
    if lines:
        width = max(len(o.name) for o in result.obligations)
        print()
        for line in lines:
            print(f"  [!!] {'persona channel':<{width}}  {line}")
        print(
            "       Not an obligation and not a fix: the persona is untrusted-channel text "
            "on this provider, so ADR 0152's containment story does not hold for it. Read "
            "ADR 0179 before treating a low score from such a run as the agent's fault."
        )
    return EXIT_OK if result.ready else EXIT_REJECTED


def cmd_lineage_list(args: argparse.Namespace) -> int:
    """Every member of the lineage archive, with its parent and whether the
    sampler may build on it (ADR 0198).

    J0b scored dimension 6 with two deductions, and this closes the second of
    them verbatim: *"no owner-facing `aef loop lineage list` — only a counts
    line."* `aef loop run` prints `archive: N member(s) ... M distinct kept
    tree(s)`, which says how big the search was and nothing about its shape.
    The information about a search is in the rejections — where the loop went
    and could not stand — and that is what this prints.

    Read-only, and it takes no `--config` and loads no graph: it opens one
    JSONL file, verifies each record's digest on the way (`read_lineage`), and
    prints. A listing that had to build a `LoopConfig` would be unavailable in
    exactly the situation an owner wants it — after a run that could not
    start.
    """
    from aef.harness.loop import read_lineage_entries

    paths = LoopPaths(root=Path(args.state))
    key = graph_id(args)
    repo_root = Path(args.repo)
    repo = GitRepo(root=repo_root) if (repo_root / ".git").exists() else None
    entries = read_lineage_entries(paths.lineage_dir, key, repo=repo)
    path = lineage_path(paths.lineage_dir, key)
    if args.json:
        print(
            json.dumps(
                {
                    "graph_id": key,
                    "path": str(path),
                    "members": [
                        {
                            **entry.record.to_payload(),
                            "weight": entry.weight,
                            "sampleable": entry.sampleable,
                            "why_not": entry.why_not,
                            "ref_resolves": entry.ref_resolves,
                        }
                        for entry in entries
                    ],
                },
                indent=2,
            )
        )
        return EXIT_OK
    if not entries:
        # Not an error: a loop that has run no turn has an empty lineage, and
        # so does one run with `--no-lineage`. Both are worth naming, because
        # "no members" with no explanation reads as a broken command.
        print(f"no lineage for {key!r} at {path}")
        print(
            "  Either no `aef loop run` has gated a candidate under this --state and "
            "--graph-id yet, or it was run with --no-lineage."
        )
        return EXIT_OK
    print(f"lineage for {key!r} — {len(entries)} member(s) — {path}")
    print(f"  {'ref':<12}  {'parent':<12}  {'score':>7}  {'verdict':<9}  {'kids':>4}  sampleable")
    for entry in entries:
        record = entry.record
        parent = record.parent_ref[:12] if record.parent_ref else "-"
        score = "-" if record.score is None else f"{record.score:.4f}"
        verdict = record.disposition or ("kept" if record.kept else "-")
        mark = "yes" if entry.sampleable else f"no  ({entry.why_not})"
        print(
            f"  {record.ref[:12]:<12}  {parent:<12}  {score:>7}  {verdict:<9}  "
            f"{record.children:>4}  {mark}"
        )
    kept = sum(1 for e in entries if e.record.kept)
    sampleable = sum(1 for e in entries if e.sampleable)
    # THE number ADR 0198 was asked for, on the surface an owner reads rather
    # than only in a research JSONL: a kept member whose parent the gates
    # rejected is a stepping stone that produced a better descendant.
    folded = {e.record.ref: e.record for e in entries}
    stones = [
        e
        for e in entries
        if e.record.kept
        and e.record.parent_ref
        and e.record.parent_ref in folded
        and not folded[e.record.parent_ref].kept
    ]
    print(f"  {kept} kept, {len(entries) - kept} rejected; {sampleable} sampleable as a parent")
    for entry in stones:
        stone = folded[entry.record.parent_ref or ""]
        print(
            f"  stepping stone: {entry.record.ref[:12]} was KEPT from {stone.ref[:12]}, "
            f"which the gates REJECTED ({stone.disposition})"
        )
    if not stones:
        print("  no kept member descends from a rejected one")
    return EXIT_OK


def cmd_corpus_reconcile(args: argparse.Namespace) -> int:
    """Rewrite `manifest.json` from the scenario files on disk. OWNER ONLY.

    THE DEFECT (reproduced, ADR 0176). Copy a corpus directory, delete one
    scenario, and the next cycle exits 3:

        error (CorpusShrankError): corpus shrank: 1 previously-admitted
        scenario(s) are gone: ['s-2']. A suite that can be made to pass by
        deleting the failing case is not a suite.

    The refusal is right and is NOT weakened here: it fires on exactly the
    same condition, and this command does not disable it. What was missing was
    any way back. There was no `corpus` subcommand at all, so the only remedy
    was hand-editing JSON — and a control whose remedy is hand-editing JSON is
    a control people route around by deleting the manifest, which loses every
    id it was keeping.

    Why it is a separate command an owner types, and never a step inside
    `cycle`/`run`: the manifest is the never-shrinks ledger the behavioural
    gates are measured against. A loop that reconciles its own ledger between
    turns can delete the scenario it fails and score the result as an
    improvement. Same shape as ADR 0060's tripwire suggestion — the harness
    prints the command; a person runs it.
    `tests/harness/test_corpus_reconcile.py` AST-scans `aef/` and fails if any
    caller other than this function appears.
    """
    from aef.harness.corpus import reconcile_manifest

    root = Path(args.corpus)
    if not root.is_dir():
        print(f"error: no corpus directory at {root}", file=sys.stderr)
        return EXIT_REJECTED
    try:
        report = reconcile_manifest(root)
    except CorpusError as exc:
        # A scenario file that does not load is NOT reconciled away. Dropping
        # an id because its file is malformed would let a corrupt write retire
        # evidence, which is the deletion this ledger exists to notice.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REJECTED

    if not report.changed:
        print(
            f"manifest already describes the {report.kept} scenario(s) on disk at {root}; "
            f"nothing to reconcile"
        )
        return EXIT_OK

    for sid, split in report.dropped.items():
        print(f"  DROPPED  {sid} (was {split.value}) — no file on disk")
    for sid, (was, now) in report.moved.items():
        print(f"  MOVED    {sid} {was.value} -> {now.value}")
    for sid, split in report.added.items():
        print(f"  ADDED    {sid} ({split.value}) — on disk, not in the manifest")
    print(
        f"manifest rewritten from disk: {len(report.dropped)} dropped, "
        f"{len(report.moved)} moved, {len(report.added)} added, "
        f"{report.kept} scenario(s) now recorded"
    )
    if report.dropped:
        print(
            "  Those ids are no longer admitted evidence. Commit this manifest in its own "
            "reviewable change — retiring a scenario is an owner's decision and the diff "
            "is the record of it (ADR 0141)."
        )
    return EXIT_OK


# The `aef loop` subcommands that RUN A TURN — a chance to propose, and so a
# line in the cycle journal whatever happened to them. Derived nowhere: the
# same two names `tests/cli/test_loop_turn_commands.py` derives from this
# module's AST, and that test fails if a third handler starts driving a turn.
LOOP_TURN_COMMANDS = frozenset({"cycle", "run"})


def _journal_crash(command: str, args: argparse.Namespace, exc: BaseException) -> None:
    """A turn that died on an unexpected exception is still a turn that ran.

    Best-effort by design: the journal is a diagnostic, and losing the exit
    code because the journal write itself failed would replace one wrong
    answer with a worse one.
    """
    state = getattr(args, "state", None)
    if not state or isinstance(exc, LoopStateInsideRepoError):
        # `--state` inside the repo is the one failure that must NOT be
        # journalled: the journal lives under `--state`, so writing it would
        # create the directory just refused (ADR 0167).
        return
    try:
        _journal_turn(
            Path(state),
            at=datetime.now(UTC),
            proposed=False,
            verdict=f"error ({type(exc).__name__}): {exc}",
            command=command,
        )
    except OSError:  # pragma: no cover - a journal write that cannot happen
        pass


def _report_crash_as_error(
    name: str, handler: Callable[[argparse.Namespace], int]
) -> Callable[[argparse.Namespace], int]:
    """Wrap one `aef loop` handler so an unexpected exception is EXIT_ERROR.

    THE DEFECT (reproduced, ADR 0182). Eleven of the thirteen `aef loop`
    subcommands let an exception reach `aef/cli/main.py`'s catch-all, which
    prints `error: <exc>` and returns **1** — and 1 is also `EXIT_REJECTED`,
    "the candidate was rejected, the system is working":

        $ aef loop score agents.demo.graph --corpus <a file> --splits bogus
        error: 'bogus' is not a valid Split
        exit=1   <- EXIT_REJECTED (a verdict on a candidate)
        $ aef loop record agents.demo.graph --corpus <under a file> ...
        error: [Errno 20] Not a directory: '.../afile.txt/under-a-file/train'
        exit=1   <- EXIT_REJECTED
        $ aef loop harvest agents.demo.no_such_module ...
        error: cannot import 'agents.demo.no_such_module': No module named ...
        exit=1   <- EXIT_REJECTED

    That is ADR 0167's R3 — *a crash's remedy is not a rejection's* — still
    standing for the majority of the surface after G1a fixed `cycle`/`run`
    and ADR 0167 fixed `doctor`/`bless`. The rendered nightly workflow fails
    the job on `-ge 2` and gives exit 3 its own summary ("the cycle crashed;
    no kill switch is set, fix the invocation", ADR 0178), so a crashed
    `score`/`record`/`harvest`/`skills`/... stayed green.

    **WHY THIS AND NOT `main()`'s CATCH-ALL.** Returning 3 there would change
    `aef adopt`, `aef migrate`, `aef init`, `aef run`, `aef eval`, `aef trace`
    and `aef doctor` too. Those commands have no exit-code vocabulary at all:
    1 is the ordinary "this command failed" every CLI returns, nothing
    distinguishes a rejection from a crash for them because they issue no
    verdicts, and no finding has reproduced a problem with their code.
    Strengthening a control on invocations nobody has shown a problem with is
    an owner's decision, not a fix wave's side effect (ADR 0141's rule). The
    exit-code vocabulary — 0 verdict / 1 REJECTED / 2 HALTED / 3 ERROR, and
    the `-ge 2` CI rule that reads it — is the LOOP's, so the wrapper is the
    loop's too.

    **WHY A WRAPPER AND NOT A `try` PER HANDLER.** Thirteen handlers, each
    free to forget, is how eleven of them came to be wrong at once. Applied
    once over everything `add_loop_parser` registers, a fourteenth subcommand
    cannot forget — and `test_every_loop_subcommand_reports_a_crash_as_an_error`
    enumerates them from the real parser, which is G1a's own pattern.
    """

    def wrapped(args: argparse.Namespace) -> int:
        try:
            return handler(args)
        except Exception as exc:
            # Named, not a bare traceback: the exception TYPE is what tells a
            # reader whether to fix the invocation or the repo.
            print(f"error ({type(exc).__name__}): {exc}", file=sys.stderr)
            if name in LOOP_TURN_COMMANDS:
                _journal_crash(name, args, exc)
            return EXIT_ERROR

    # `SystemExit` and `KeyboardInterrupt` are deliberately NOT caught: an
    # operator's Ctrl-C is not this loop's error, and argparse's own usage
    # exits must keep argparse's code.
    wrapped.__name__ = getattr(handler, "__name__", name)
    wrapped.__doc__ = handler.__doc__
    wrapped.__wrapped__ = handler  # type: ignore[attr-defined]
    return wrapped


def _wrap_loop_handlers(subs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Apply `_report_crash_as_error` to every handler under `aef loop`.

    Recurses into a subcommand that is itself a group (`loop corpus
    reconcile`), because a nested handler is exactly the one a per-handler
    `try` would be most likely to miss. `LOOP_TURN_COMMANDS` holds top-level
    names only, so the leaf name is the right one to carry.
    """
    for name, sub in subs.choices.items():
        handler = sub.get_default("handler")
        if handler is None:
            for action in sub._actions:
                if isinstance(action, argparse._SubParsersAction):
                    _wrap_loop_handlers(action)
            continue
        sub.set_defaults(handler=_report_crash_as_error(name, handler))


def add_loop_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("loop", help="drive the self-rewiring loop (gate/monitor/digest)")
    loop_subs = p.add_subparsers(dest="loop_command", required=True)

    def _common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--repo", default=".", help="git repository root")
        sub.add_argument(
            "--state", required=True, help="loop state dir (ledger, archive, kill switch)"
        )
        # `default=None`, NOT `"default"`, so that omitting the flag is
        # distinguishable from typing it. `aef loop cycle` derives the value
        # from the corpus it already loaded when it is omitted (ADR 0176);
        # every other subcommand falls back to DEFAULT_GRAPH_ID through
        # `graph_id()`, which is exactly what `default="default"` used to do.
        sub.add_argument(
            "--graph-id",
            default=None,
            help=(
                "the archive key, and the graph whose recorded scenarios are this loop's "
                "evidence. Omitted on `cycle`, it is DERIVED from --corpus when the corpus "
                "records exactly one graph, and refused when it records several. Elsewhere "
                f"an omitted value means {DEFAULT_GRAPH_ID!r}."
            ),
        )
        sub.add_argument(
            "--agent-root",
            default=DEFAULT_AGENT_ROOT,
            help=(
                "the Zone A root — the only directory the loop may propose changes to. "
                "Both sides of G5's drift metric are read from it, so `bless` and the "
                "gate must be given the same value or they describe different trees."
            ),
        )

    def _proposer_flags(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--proposer",
            choices=list(PROPOSERS),
            default="rule_based",
            help=(
                "which proposer writes the candidate. rule_based (default): numeric steps "
                "and the bounded structural catalogue, deterministic. rule_based_prompt: "
                "for a `.md` prompt-file agent (--agent-path <persona>.md), appends the "
                "highest-recurrence consolidated lesson as ONE bullet under a "
                "'## Lessons (aef)' section — deterministic, no model call, provenance in "
                "the bullet (ADR 0157). llm: a model writes the whole file, code validates "
                "it against G0/G4 and falls back to rule_based on any failure (ADR 0122). "
                "Off by default, by measurement. Not inferred from the agent path: "
                "--proposer means the same thing in every repo."
            ),
        )
        sub.add_argument(
            "--proposer-model",
            default=None,
            help="model id the llm proposer asks, through the claude_code harness login; "
            "required with --proposer llm, no default so no model id is hardcoded here",
        )

    def _turn_shape_flags(sub: argparse.ArgumentParser) -> None:
        """How many candidates a turn tries, and how much it holds back (ADR 0200)."""
        sub.add_argument(
            "--candidates",
            type=int,
            default=1,
            metavar="N",
            help=(
                "how many candidates one TURN may try (default 1 — what a turn always "
                "did: the proposer offered several and the cycle took the first). Above "
                "1, each candidate is materialised on its own branch and gated on its "
                "OWN independent pass; the turn keeps the best that passed and the "
                "ledger records every one. The gates never judge a set. COST IS LINEAR: "
                "N x (cohort+2) corpus passes per turn, so N=2 doubles the most "
                "expensive thing the loop does."
            ),
        )
        sub.add_argument(
            "--audit-slice",
            type=int,
            default=0,
            metavar="N",
            help=(
                "hold N train scenarios back from this turn and read the kept candidate "
                "against the incumbent on them afterwards (default 0 — off; ADR 0200). "
                "Which N is a function of the corpus's own scenario ids and the calendar "
                "date and nothing else, so the loop cannot choose the set that judges "
                "it; they rotate daily. Held back from the GATES and from the "
                "proposer's evidence, both. The read is ADVISORY — it is recorded and "
                "printed and changes no disposition, because a set the loop is selected "
                "against is not held out. This is NOT the owner's holdout, which stays "
                "owner-only behind --i-am-spending-the-holdout."
            ),
        )

    def _cassette_miss(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--cassette-miss",
            default="fail",
            choices=["fail", "live"],
            help=(
                "what a model request the recording never saw does (ADR 0123). 'fail' "
                "(default): the node fails and the score is deterministic — no credential "
                "needed. 'live': the request goes to the provider in --config's "
                "model_provider and the score is a LIVE one, reported as such. Use it to "
                "score a prompt change on purpose, not to make a gate pass."
            ),
        )

    p_gate = loop_subs.add_parser("gate", help="evaluate one candidate branch")
    _common(p_gate)
    p_gate.add_argument(
        "--base",
        default=None,
        help=_BASE_REF_HELP,
    )
    p_gate.add_argument("--head", required=True)
    p_gate.add_argument("--workdir", required=True, help="scratch dir for gate execution")
    p_gate.add_argument("--corpus", default=None)
    p_gate.add_argument(
        "--config",
        default=None,
        help=(
            "path to aef.yaml, whose `policies` and `tools.allow` become the policy "
            "engine the corpus runs under. READ FROM THE BASE REF, never from the "
            "candidate — otherwise a candidate could widen the rules it is judged by."
        ),
    )
    p_gate.add_argument("--entrypoint", default=None, help=ENTRYPOINT_HELP)
    _cassette_miss(p_gate)
    p_gate.add_argument(
        "--agent-path",
        default=DEFAULT_AGENT_PATH,
        help="Read only to report unmet preflight obligations before gating (ADR 0141); "
        "the gates themselves take --entrypoint. " + _AGENT_PATH_HELP,
    )
    p_gate.add_argument(
        "--network-isolated",
        action="store_true",
        help="attest that the caller (a CI container) provides network isolation. "
        "A process cannot revoke its own network access; passing this without real "
        "isolation makes the sandbox's report untrue.",
    )
    p_gate.add_argument(
        "--sandbox-image",
        default=None,
        help="run candidate code inside a container built from this image, with "
        "--network none, --read-only and --cap-drop ALL. Unlike --network-isolated, "
        "which the caller ASSERTS, this is verified by a probe before anything runs — "
        "so the result's network_isolated=True is measured. Needs docker or podman; "
        "raises rather than degrading if neither is available.",
    )
    p_gate.add_argument(
        "--build-command",
        action="append",
        default=None,
        help="a command G1 must pass, e.g. 'python -m pytest -q'. Repeatable. "
        "Defaults to pytest only — anything more is repo-specific.",
    )
    p_gate.set_defaults(handler=cmd_gate)

    p_monitor = loop_subs.add_parser("monitor", help="evaluate post-merge windows, auto-rollback")
    _common(p_monitor)
    p_monitor.add_argument(
        "--observations",
        default=None,
        help="the observations JSONL your deployment wrote. Without it the monitor sees "
        "no live runs and every window reverts for lack of evidence.",
    )
    p_monitor.set_defaults(handler=cmd_monitor)

    p_digest = loop_subs.add_parser("digest", help="weekly trend report")
    _common(p_digest)
    p_digest.add_argument("--json", action="store_true")
    p_digest.add_argument("--owner-edits", type=int, default=0)
    p_digest.add_argument("--runs", default=None, help="runs dir, to report whether any exist")
    p_digest.set_defaults(handler=cmd_digest)

    p_status = loop_subs.add_parser("status", help="kill switch, ledger integrity, open windows")
    _common(p_status)
    p_status.set_defaults(handler=cmd_status)

    p_record = loop_subs.add_parser("record", help="promote a real run into a corpus scenario")
    p_record.add_argument(
        "module",
        metavar="GRAPH",
        help=GRAPH_REFERENCE_HELP,
    )
    p_record.add_argument("--corpus", required=True)
    p_record.add_argument("--scenario-id", required=True)
    p_record.add_argument("--objective", required=True)
    p_record.add_argument("--agent-id", default="recorder")
    p_record.add_argument("--split", default="train", choices=[s.value for s in Split])
    p_record.add_argument(
        "--expected",
        default=Expected.UNSPECIFIED.value,
        choices=[e.value for e in Expected],
        help=(
            "the OWNER's claim about this task, which no recording can supply. "
            "must_fail makes the scenario a TRIPWIRE: a task genuinely beyond the agent's "
            "remit, where claiming success is a lie rather than an improvement. Without at "
            "least one, the gates cannot detect reward hacking (ADR 0060)."
        ),
    )
    p_record.add_argument("--notes", default="")
    p_record.add_argument(
        "--working-memory",
        default=None,
        help=(
            "JSON object seeding AEFState.working_memory, e.g. '{\"difficulty\": 99}'. "
            "This is how you drive the agent into the FAILING cases worth recording — a "
            "corpus where everything already passes cannot demonstrate an improvement, and "
            "a tripwire has to be a task the agent genuinely cannot do."
        ),
    )
    p_record.add_argument(
        "--i-am-spending-the-holdout",
        action="store_true",
        help="required to write to the holdout split. It is the owner's only independent "
        "read of whether the loop improves anything; filling it casually destroys that "
        "independence silently.",
    )
    p_record.add_argument(
        "--config",
        default=None,
        help=(
            "aef.yaml path; wires the real model_provider (e.g. impl: claude_code) so a "
            "graph that calls a model can be recorded. Every call it makes is pinned in "
            "the scenario and replayed by the gates without a credential (ADR 0123)."
        ),
    )
    p_record.add_argument(
        "--check",
        action="append",
        default=None,
        help=(
            "an OWNER check on the final state, as JSON: a single object "
            '\'{"path": "working_memory.summary", "op": "contains", "value": "X"}\' '
            "or a JSON list of them. Repeatable. Ops: equals, contains, regex, exists "
            "(ADR 0113), max_words, min_words (ADR 0166 — use these for a word cap; "
            "the regex form of one is a ReDoS). Data, never code."
        ),
    )
    p_record.add_argument(
        "--budget-ms",
        type=float,
        default=None,
        help="wall-clock budget for the re-execution, judged on the runner's stopwatch",
    )
    p_record.set_defaults(handler=cmd_record)

    p_bootstrap = loop_subs.add_parser(
        "bootstrap",
        help="a corpus on day one: run the graph once per input, record each as a scenario",
        description=(
            "INPUTS FILE SHAPE: a JSON list of objects, or an object with an "
            "'inputs' list. Each object needs `objective` (string) and may set "
            "`id` (default <prefix>-<n>), `working_memory` (object seeding "
            "AEFState.working_memory — how you reach the agent's FAILING cases), "
            "`notes`, `checks` (owner checks, ADR 0113) and `budget_ms`.\n\n"
            'EXAMPLE: [{"objective": "summarise the ticket", '
            '"working_memory": {"difficulty": 9}, '
            '"checks": [{"path": "scores.quality", "op": "equals", '
            '"value": 1.0}]}]\n\n'
            "WHAT IT WILL NOT DO. It writes the TRAIN split only and offers no "
            "flag to override, the same rule as `harvest`: if the system could "
            "fill the set that gates it, the gate would measure the system's own "
            "choices. It never labels `expected` — only an owner can say a task "
            "SHOULD have failed (ADR 0060) — and an `expected` key in the file is "
            "refused rather than ignored. It refuses the whole invocation if any "
            "id already exists, before running anything."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_bootstrap.add_argument(
        "module",
        metavar="GRAPH",
        help=GRAPH_REFERENCE_HELP,
    )
    p_bootstrap.add_argument("--corpus", required=True)
    p_bootstrap.add_argument(
        "--inputs", required=True, help="JSON file of inputs; see the description above"
    )
    p_bootstrap.add_argument(
        "--prefix",
        default="bootstrap",
        help="scenario id prefix for inputs that do not name one (default: bootstrap). "
        "Stable on purpose: re-running the same file is refused rather than "
        "overwriting what it recorded.",
    )
    p_bootstrap.add_argument("--agent-id", default="bootstrap")
    p_bootstrap.add_argument(
        "--state",
        default=None,
        help="loop state dir. Not `required=True` as on every other loop subcommand, "
        "because this is the day-one command and there may be no loop yet — but one of "
        "--state and --no-loop-state MUST be given. An engaged kill switch stops the "
        "run: corpus/ is gate evidence and a halted loop must not have it changed "
        "underneath it (ADR 0069).",
    )
    p_bootstrap.add_argument(
        "--no-loop-state",
        action="store_true",
        help="assert that no loop state directory exists yet, so there is no kill switch "
        "to consult. An assertion, not a default: omitting --state used to mean this "
        "silently, and a halted loop's corpus grew from the documented invocation "
        "(ADR 0141). If a loop does exist, pass --state instead.",
    )
    p_bootstrap.add_argument(
        "--memory",
        default=None,
        help="durable memory file — the SAME file you pass to `aef loop cycle --memory`. "
        "Each run's reflections are written here as well as to its own isolated store, "
        "so a failing input leaves failure memory the proposer can read; without it "
        "the cycle says `no admissible failure memory` and proposes nothing (ADR 0145). "
        "Bootstrap writes only what the graph's reflect node observed and never invents "
        "a failure (ADR 0060), so a graph with no reflect node leaves this empty.",
    )
    p_bootstrap.add_argument(
        "--config",
        default=None,
        help=(
            "aef.yaml path; wires the real model_provider (e.g. impl: claude_code) so a "
            "graph that calls a model can be recorded — RECORDING IS THE ONE PASS THAT IS "
            "SUPPOSED TO BE LIVE, and the number of calls it spends is reported. Every "
            "call is pinned in the scenario and replayed by the gates without a credential "
            "(ADR 0123). Built through `aef run`'s own code path, so `policies`, "
            "`tools.allow` and `evaluator.suites` reach the recording too (ADR 0145)."
        ),
    )
    p_bootstrap.set_defaults(handler=cmd_bootstrap)

    p_score = loop_subs.add_parser(
        "score",
        help="the task metric: score the incumbent graph over the corpus",
        description=(
            "Scores IN-PROCESS. A scenario's `budget_ms` is judged on this "
            "stopwatch, and the gates' isolated path adds per-node IPC to the "
            "same measurement (~0.7 ms more on a 4-node graph, ADR 0113), so a "
            "budget calibrated here can fail at the gate — leave headroom."
        ),
    )
    p_score.add_argument("entrypoint", metavar="GRAPH", help=GRAPH_REFERENCE_HELP)
    p_score.add_argument("--corpus", required=True)
    p_score.add_argument(
        "--splits",
        default="train,validation",
        help="comma-separated; holdout needs --i-am-spending-the-holdout",
    )
    p_score.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run the whole suite N times and report the spread of the mean — the noise "
        "floor an improvement must clear",
    )
    p_score.add_argument("--json", action="store_true")
    p_score.add_argument("--i-am-spending-the-holdout", action="store_true")
    _cassette_miss(p_score)
    p_score.add_argument(
        "--config",
        default=None,
        help="aef.yaml path. Its tools/policies block is the policy the scored "
        "run gets — the same one `aef run --config` and the gates apply; without "
        "it the run is deny-by-default. Its model_provider also answers cassette "
        "misses under --cassette-miss live.",
    )
    p_score.add_argument(
        "--memory",
        default=None,
        help=(
            "durable memory JSONL — the SAME file `aef loop bootstrap --memory`, `aef run "
            "--memory` and `aef loop cycle --memory` use. Given it, a scenario whose OWNER "
            "CHECK fails is recorded as failure memory through the one producer (ADR 0180), "
            "idempotently: --repeat 5 and a second run over the same corpus leave ONE record "
            "per failed check. Off by default. WHY IT MATTERS: with the producer wired into "
            "`bootstrap` only, a lesson's `runs_since_last_seen` climbs on every scored "
            "scenario and staleness walks it out of the prompt while nothing ever re-sees it "
            "(the graph does NOT read this store on the score path — it writes the "
            "failure records a later `cycle --memory` can learn from; retrieval into "
            "the scored prompt is a `run`/`cycle` property (final seam hunt, ADR 0191)). "
            "WHAT IT DOES NOT DO: this is "
            "the IN-PROCESS path only. The gates' isolated path runs each scenario in a "
            "worker with no store and never writes one — a gate that could would let scoring "
            "a candidate manufacture the next one's evidence, which ADR 0174 refused and this "
            "does not reopen."
        ),
    )
    p_score.set_defaults(handler=cmd_score)

    p_skills = loop_subs.add_parser(
        "skills", help="draft skill PROPOSALS from consolidated knowledge (never adopted here)"
    )
    p_skills.add_argument("--memory", required=True, help="durable memory store the agent wrote")
    p_skills.add_argument("--agent-id", required=True)
    p_skills.add_argument("--out", required=True, help="proposals dir; not a harness dir")
    p_skills.add_argument("--min-occurrences", type=int, default=3)
    p_skills.set_defaults(handler=cmd_skills)

    p_harvest = loop_subs.add_parser(
        "harvest", help="promote recorded production runs into corpus scenarios"
    )
    _common(p_harvest)
    p_harvest.add_argument(
        "module",
        metavar="GRAPH",
        help=GRAPH_REFERENCE_HELP,
    )
    p_harvest.add_argument("--runs", required=True, help="dir of runs from `aef run --record-runs`")
    p_harvest.add_argument("--corpus", required=True)
    p_harvest.add_argument(
        "--daily-limit",
        type=int,
        default=5,
        help="cap promotions per day. One bad deploy can produce thousands of failing "
        "runs; without a cap the corpus fills with a single incident and the gates "
        "start measuring that incident instead of the agent.",
    )
    p_harvest.add_argument(
        "--include-successes",
        action="store_true",
        help="also promote runs that passed. Off by default: failures carry the "
        "information, and auto-promoting successes inflates the pass rate the gates "
        "measure against.",
    )
    p_harvest.set_defaults(handler=cmd_harvest)

    p_cycle = loop_subs.add_parser(
        "cycle", help="one turn of the loop: harvest -> propose -> gate -> record"
    )
    _common(p_cycle)
    p_cycle.add_argument(
        "--base",
        default=None,
        help=_BASE_REF_HELP,
    )
    p_cycle.add_argument("--workdir", required=True)
    p_cycle.add_argument(
        "--module",
        default=None,
        metavar="GRAPH",
        help=GRAPH_REFERENCE_HELP,
    )
    p_cycle.add_argument("--runs", default=None, help="dir from `aef run --record-runs`")
    p_cycle.add_argument("--corpus", default=None)
    p_cycle.add_argument(
        "--config",
        default=None,
        help=(
            "path to aef.yaml, whose `policies` and `tools.allow` become the policy "
            "engine the corpus runs under. READ FROM THE BASE REF, never from the "
            "candidate — otherwise a candidate could widen the rules it is judged by."
        ),
    )
    p_cycle.add_argument("--entrypoint", default=None, help=ENTRYPOINT_HELP)
    _cassette_miss(p_cycle)
    p_cycle.add_argument("--agent-path", default=DEFAULT_AGENT_PATH, help=_AGENT_PATH_HELP)
    p_cycle.add_argument(
        "--memory",
        default=None,
        help="path to the durable memory JSONL the reflect node writes — the SAME file "
        "`aef loop bootstrap --memory` and `aef run --memory` write. Without it the "
        "proposer has no recorded failures to ground in and will never propose, so one "
        "of --memory and --no-memory MUST be given (ADR 0165).",
    )
    p_cycle.add_argument(
        "--no-memory",
        action="store_true",
        help="assert that this cycle is deliberately running with no failure memory. An "
        "assertion, not a default: omitting --memory used to mean this silently, and "
        "this repo's own nightly `aef loop cycle` therefore printed `no memory store "
        "configured: nothing to learn from, no candidate` and exited 0 every night for "
        "as long as the workflow existed, which reads as success (ADR 0165). The cycle "
        "still runs — harvest, ledger verification, preflight — it just cannot propose.",
    )
    p_cycle.add_argument(
        "--build-command",
        action="append",
        default=None,
        help="a command G1 must pass, e.g. 'python -m pytest -q'. Repeatable. "
        "Defaults to pytest only — anything more is repo-specific.",
    )
    _proposer_flags(p_cycle)
    _turn_shape_flags(p_cycle)
    p_cycle.set_defaults(handler=cmd_cycle)

    p_run = loop_subs.add_parser(
        "run",
        help="N turns of propose -> gate -> keep-or-revert on a local kept branch (never main)",
    )
    _common(p_run)
    p_run.add_argument(
        "--base",
        default=None,
        help=_BASE_REF_HELP,
    )
    p_run.add_argument("--workdir", required=True)
    p_run.add_argument(
        "--module",
        default=None,
        metavar="GRAPH",
        help=GRAPH_REFERENCE_HELP,
    )
    p_run.add_argument("--runs", default=None, help="dir from `aef run --record-runs`")
    p_run.add_argument("--corpus", default=None)
    p_run.add_argument("--config", default=None, help="aef.yaml path, read from the base ref")
    p_run.add_argument("--entrypoint", default=None, help=ENTRYPOINT_HELP)
    _cassette_miss(p_run)
    p_run.add_argument(
        "--memory",
        default=None,
        help="durable memory store the proposer reads — the SAME file `aef loop bootstrap "
        "--memory` and `aef run --memory` write. Without it no turn can propose, so one of "
        "--memory and --no-memory MUST be given (ADR 0167).",
    )
    p_run.add_argument(
        "--no-memory",
        action="store_true",
        help="assert that this run is deliberately running with no failure memory. An "
        "assertion, not a default: omitting --memory used to mean this silently, and "
        "`aef loop run` then exited 0 having proposed nothing, with the state directory "
        "left empty and `aef loop monitor` reporting `cycles run: 0 (last never)` — "
        "indistinguishable from a loop nobody had started (ADR 0167).",
    )
    p_run.add_argument("--agent-path", default=DEFAULT_AGENT_PATH, help=_AGENT_PATH_HELP)
    p_run.add_argument("--turns", type=int, default=10)
    p_run.add_argument("--budget-minutes", type=float, default=60.0)
    p_run.add_argument(
        "--kept-branch",
        default="loop/kept",
        help="local branch that advances on every kept candidate; a person merges it",
    )
    p_run.add_argument(
        "--sample-parents",
        action="store_true",
        help="propose each turn from a parent SAMPLED from the lineage archive — DGM's "
        "rule, sigmoid(score)/(1+children) — instead of always from the latest kept "
        "(ADR 0121, ADR 0160). Stepping stones, including gated-and-rejected ones that "
        "reached a score, stay eligible. Off by default: measured, not assumed. The "
        "kept branch still points at the best-scoring KEPT member.",
    )
    p_run.add_argument(
        "--seed",
        type=int,
        default=0,
        help="RNG seed for --sample-parents, so a sampled run is reproducible",
    )
    p_run.add_argument(
        "--no-lineage",
        action="store_true",
        help="do not read or write <state>/lineage/<graph-id>.jsonl. By default the "
        "lineage archive persists across invocations: tonight's run can propose from a "
        "parent last night kept, and will not re-gate a tree an earlier run already "
        "kept or rejected. Pass this for a self-contained run (an A/B arm, a demo).",
    )
    # `gate` and `cycle` have had this since G1 existed; `run` never did, and
    # `_build_commands` reads it with `getattr`, so `aef loop run` silently
    # took G1's default `python -m pytest -q` — the WHOLE suite, per candidate,
    # per turn. Found by running J2's first live turn (ADR 0160): the only
    # thing the arm measured was `G1 rejected it: build command failed
    # (timed out)`, before any behavioural gate, on a candidate the model had
    # just been paid to write.
    p_run.add_argument(
        "--build-command",
        action="append",
        default=None,
        help="a command G1 must pass, e.g. 'python -m pytest -q'. Repeatable. "
        "Defaults to pytest only — anything more is repo-specific.",
    )
    _proposer_flags(p_run)
    _turn_shape_flags(p_run)
    p_run.set_defaults(handler=cmd_run)

    p_bless = loop_subs.add_parser(
        "bless", help="archive the current Zone A state as the owner-blessed baseline"
    )
    _common(p_bless)
    p_bless.add_argument("--agent-path", default=DEFAULT_AGENT_PATH, help=_AGENT_PATH_HELP)
    p_bless.add_argument(
        "--base",
        default=None,
        help=_BASE_REF_HELP,
    )
    p_bless.add_argument("--note", default="", help="why this state is the baseline")
    p_bless.set_defaults(handler=cmd_bless)

    p_lineage = loop_subs.add_parser(
        "lineage", help="read the DGM lineage archive — every candidate, kept and rejected"
    )
    lineage_subs = p_lineage.add_subparsers(dest="lineage_command", required=True)
    p_lineage_list = lineage_subs.add_parser(
        "list",
        help="every member with its parent, score, verdict and whether it can be a parent",
        description=(
            "The archive an owner can read (ADR 0198).\n\n"
            "`aef loop run` prints one counts line — how many members, how many "
            "distinct trees — which says how big the search was and nothing about "
            "its shape. The information about a search is in the REJECTIONS: where "
            "the loop went and could not stand, and which of those places the next "
            "turn may still build on. `sampleable` is `_parent_weight`, the "
            "function the sampler itself calls, not a second opinion about it: a "
            "candidate a cheap gate refused has no task metric and can never be a "
            "parent, and a member whose branch has been deleted is history rather "
            "than a starting point.\n\n"
            "Read-only. It opens one JSONL file, checks each record's digest, and "
            "prints; it builds no config and loads no graph, so it works after a "
            "run that could not start."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _common(p_lineage_list)
    p_lineage_list.add_argument("--json", action="store_true")
    p_lineage_list.set_defaults(handler=cmd_lineage_list)

    p_corpus = loop_subs.add_parser(
        "corpus", help="owner actions on a corpus directory (never run by the loop)"
    )
    corpus_subs = p_corpus.add_subparsers(dest="corpus_command", required=True)
    p_reconcile = corpus_subs.add_parser(
        "reconcile",
        help="rewrite manifest.json from the scenario files on disk",
        description=(
            "Makes the never-shrinks manifest describe the scenarios actually "
            "present, and prints every id it drops.\n\n"
            "RUN IT BY HAND. Nothing in `aef loop cycle` or `aef loop run` calls "
            "this, and a test enforces that: the manifest is the ledger the "
            "behavioural gates are measured against, and a loop that can rewrite "
            "its own ledger between turns can retire the scenario it fails and "
            "call the result an improvement. The `corpus shrank` refusal stays "
            "exactly as strict; this is the documented way back from a corpus "
            "that was COPIED with its manifest, or a scenario retired on "
            "purpose. Commit the result on its own — the diff is the record."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_reconcile.add_argument("--corpus", required=True, help="the corpus directory")
    p_reconcile.set_defaults(handler=cmd_corpus_reconcile)

    p_doctor = loop_subs.add_parser(
        "doctor", help="report all six loop obligations at once, with the fix for each"
    )
    _common(p_doctor)
    p_doctor.add_argument("--corpus", default="corpus")
    p_doctor.add_argument("--agent-path", default=DEFAULT_AGENT_PATH, help=_AGENT_PATH_HELP)
    p_doctor.add_argument(
        "--base",
        default=None,
        help=_BASE_REF_HELP,
    )
    p_doctor.add_argument(
        "--config", default=None, help="repo-relative config read from the base ref"
    )
    p_doctor.add_argument("--observations", default=None)
    p_doctor.add_argument(
        "--runs",
        default=None,
        help="dir from `aef run --record-runs`. Read for ONE thing: whether the most "
        "recent recorded run had its persona sent in the USER turn, which is a property "
        "of the provider you installed and is reported as a warning rather than an "
        "obligation (ADR 0179). The corpus is read for the same thing without this flag.",
    )
    p_doctor.set_defaults(handler=cmd_doctor)

    # LAST, over everything registered above, including the nested `corpus`
    # group: a crash is EXIT_ERROR on EVERY `aef loop` subcommand, and a
    # fourteenth added below this line is covered without being told to be.
    _wrap_loop_handlers(loop_subs)
