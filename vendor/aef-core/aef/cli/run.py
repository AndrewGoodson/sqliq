"""`aef run` — execute a graph module's `build_graph()` against an
objective. memory/tracer are always in-memory backends; the model
provider is real (built from `--config`'s `model_provider.impl`) if
config is given, otherwise unconfigured — a graph module that calls
`services.require_model_provider()` without `--config` gets a clear
`ServiceNotConfiguredError`, not a silent no-op.

Durability defaults to in-memory (a one-off run, nothing persisted) but
`checkpoints_dir` switches it to `FileDurabilityBackend` — this is what
makes `aef run --checkpoints-dir X` followed by `aef eval`/`aef trace
--checkpoints-dir X --run-id <id>` an actually-chainable workflow. Before
this, `aef run` always used `InMemoryDurabilityBackend`, whose data is
discarded the instant the process exits — `aef eval`/`aef trace` could
never find anything a prior `aef run` produced, confirmed by actually
running the two in sequence, not inferred from reading the code. See
docs/adr/0021.

Only `model_provider` is config-driven so far: `memory`/`knowledge_graph`/
`tools`/`policies` need a real plugin-registry design this repo doesn't
have yet (`MemoryConfig` alone can't build a real `mem0.Memory()`, and
there's no registry mapping `tools.allow` name strings to `Tool` objects).
See docs/adr/0014.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from aef.config import (
    build_domain_gates,
    build_model_provider,
    build_policy_config,
    build_retriever,
    load_agent_config,
)
from aef.config.factory import build_containment_mode
from aef.config.schema import ContextConfig, ShadowConfig
from aef.harness.graph_loading import (
    ensure_cwd_importable,
    import_graph_module,
    looks_like_a_path,
)
from aef.harness.memory_store import FileMemoryStore
from aef.harness.replay_inputs import recorded_context
from aef.harness.shadow import ContainmentMode
from aef.kernel import (
    DurabilityBackend,
    FileDurabilityBackend,
    GraphExecutor,
    InMemoryDurabilityBackend,
)
from aef.providers.base import ModelProvider
from aef.providers.cassette_provider import CassetteProvider
from aef.security.tool import FileAuditLogWriter, PolicyConfig
from aef.services.knowledge.consolidate import RuleBasedConsolidator
from aef.services.knowledge.in_memory import InMemoryKnowledgeStore
from aef.services.memory.base import MemoryStore
from aef.services.memory.in_memory import InMemoryMemoryStore
from aef.services.runtime import agent_services
from aef.state import AEFState


@dataclass(frozen=True)
class RunConfig:
    """Everything `--config` contributes to a run's `Services`.

    **One construction site, two commands.** `aef run` and `aef loop
    bootstrap` both turn an `aef.yaml` into the pieces `agent_services`
    needs, and they used to do it in two places: `run_graph_module` built
    the provider, the policy config, the context config and the reflection
    impl; `cmd_bootstrap` built the provider and the reflection impl and
    nothing else. So a recording made by bootstrap ran under the engine's
    DEFAULT policy while the same graph under `aef run` ran under the
    adopter's — and the corpus would have pinned behaviour production never
    had. That is ADR 0091's drift shape, and this dataclass is the fix:
    both callers read the config here or not at all.

    `None` config path means no `aef.yaml` was given, and every field keeps
    the unconfigured default — a graph calling `require_model_provider()`
    then gets a clear refusal, never a silent no-op.

    **`containment_mode` has no consumer inside `aef/` yet, and that is
    stated rather than covered over.** Shadow execution still has no
    production caller (ADR 0161, ADR 0173): what this field buys is that when
    one is written it reads the owner's choice from here instead of accepting
    a signature default, and that `shadow_for` will no longer let it forget.
    A field carried for a caller that does not exist is a promise; the reason
    it is not the kind ADR 0101 deleted is that the wire is short, it is
    exercised end-to-end by tests through `shadow_for`, and its absence was a
    live defect rather than a deferred feature.
    """

    model_provider: ModelProvider | None = None
    policy_config: PolicyConfig | None = None
    context: ContextConfig | None = None
    reflection: str = "rule_based"
    reflection_model: str | None = None
    # `shadow.containment`, as the enum the shadow harness runs on (ADR 0173).
    # THE one place a caller reads the owner's containment choice from: before
    # this, the field validated in `aef.yaml`, `build_containment_mode` had
    # zero callers, and `shadow_for`'s `mode` defaulted to `auto` — so an owner
    # who wrote `off` got a container and an owner who wrote `fallback` got
    # `auto`'s refusal. Reproduced both ways; ADR 0173.
    #
    # The unconfigured value is DERIVED from `ShadowConfig()` rather than
    # written as `ContainmentMode.AUTO`, so "no aef.yaml" and "an aef.yaml
    # with no shadow block" cannot drift into two different answers.
    containment_mode: ContainmentMode = field(
        default_factory=lambda: build_containment_mode(ShadowConfig())
    )


def build_run_config(config_path: str | Path | None) -> RunConfig:
    """Read an `aef.yaml` into the parts `agent_services` takes.

    `build_domain_gates` is called and its result discarded ON PURPOSE: an
    unresolvable evaluator suite should stop the run before it costs
    anything, naming itself, rather than at the end of one (ADR 0100). The
    evaluator that actually uses the suites is built at scoring time.

    **The order below is a decision, and it changed.** `run_graph_module`
    used to build the model provider first and validate the evaluator suites
    second, so a config with both an unbuildable provider and an
    unresolvable suite reported the provider. Extracting this function
    reversed it by accident (ADR 0145 did not notice; ADR 0149 reproduced
    it). It is kept reversed and PINNED by
    `test_build_run_config_reports_the_evaluator_before_the_provider`:
    **validate before constructing.** `build_domain_gates` resolves names and
    builds nothing; `build_model_provider` constructs a live provider object
    and, for `impl: anthropic`, imports a vendor SDK to do it. Reporting the
    cheap, local, purely-declarative error first is the better answer, and
    the point of pinning it is that "which error the adopter sees" stops
    being whichever statement a refactor happened to leave on top.
    """
    if config_path is None:
        return RunConfig()
    config = load_agent_config(config_path)
    build_domain_gates(config.evaluator)
    return RunConfig(
        model_provider=build_model_provider(config.model_provider),
        policy_config=build_policy_config(config.tools, config.policies),
        context=config.context,
        reflection=config.reflection.impl,
        reflection_model=config.model_provider.model if config.model_provider is not None else None,
        containment_mode=build_containment_mode(config.shadow),
    )


# `import_graph_module` and `looks_like_a_path` USED TO BE DEFINED HERE, and
# that was the whole of ADR 0177's R1. ADR 0168 §M4 taught `aef run` that an
# entrypoint may be a file path — because `aef migrate --agent-root
# .claude/agents` writes a graph no dotted name can spell — and taught exactly
# one of the repo's three loaders. `aef/harness/scenario_runner.py` and
# `aef/harness/node_worker.py` kept their own `importlib.import_module`, so
# `aef loop score` on the documented opt-in exited 1 and G2 loaded the
# incumbent with one loader and the candidate with the other, turning an
# import error into "1 previously-passing scenario(s) no longer pass".
#
# There is one implementation now and it lives in the HARNESS, because the
# harness may not import the CLI. These names stay importable from here: the
# generated `AGENT_INTEGRATION.md`, `aef/cli/loop.py` and three test modules
# all reach for `aef.cli.run.import_graph_module`, and a second definition is
# what this fix exists to delete.
_ensure_cwd_importable = ensure_cwd_importable
__all__ = ["import_graph_module", "looks_like_a_path", "load_graph_module", "run_graph_module"]


def load_graph_module(module_path: str):  # type: ignore[no-untyped-def]
    """Import a module and call its `build_graph()`.

    Shared with `aef loop record`, which needs the same graph the runner
    would execute — recording against a different graph than production runs
    would make the corpus describe something nobody ships.
    """
    module = import_graph_module(module_path)
    build_graph = getattr(module, "build_graph", None)
    if build_graph is None:
        raise ValueError(f"module {module_path!r} has no build_graph() function")
    return build_graph()


def append_observation(path: Path, *, at: str, passed: bool, cost_tokens: int) -> None:
    """One JSON line per live run, for post-merge monitoring.

    Nothing wrote this file before M12, so every monitoring window reported
    as unobserved — which, correctly, rolled everything back. Monitoring with
    no input is not monitoring; it is a very expensive way to revert.
    """
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": at, "passed": passed, "cost_tokens": cost_tokens})
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def run_graph_module(
    module_path: str,
    *,
    agent_id: str,
    objective: str,
    working_memory: dict[str, object] | None = None,
    config_path: str | Path | None = None,
    checkpoints_dir: str | Path | None = None,
    record_runs_dir: str | Path | None = None,
    memory_path: str | Path | None = None,
    audit_log_path: str | Path | None = None,
    judge_rubric: dict[str, float] | None = None,
) -> AEFState:
    module = import_graph_module(module_path)
    build_graph = getattr(module, "build_graph", None)
    if build_graph is None:
        raise ValueError(f"module {module_path!r} has no build_graph() function")

    # `evaluator.suites` reaches the evaluator, and `policies`/`tools.allow`
    # reach the run — both validated and were ignored before (ADR 0014, 0082,
    # 0092, 0100). All of it is read in ONE place now, shared with
    # `aef loop bootstrap`, so a recording cannot run under a different
    # configuration than production does. See `build_run_config`.
    run_config = build_run_config(config_path)
    model_provider = run_config.model_provider
    policy_config = run_config.policy_config
    context_config = run_config.context
    reflection = run_config.reflection
    reflection_model = run_config.reflection_model

    # ONE recorder, and this is the command that did not use it (ADR 0149's
    # rule, ADR 0190's finding). `aef loop record` and `aef loop bootstrap`
    # both wrap the configured provider in a recording `CassetteProvider` and
    # carry `recording.recorded` onto what they write; `aef run --record-runs`
    # built its `RecordedRun` with no `model_calls=` at all — and it is the
    # ONLY recording path `harvest`, `cycle --runs` and the generated nightly
    # workflow are fed from. So harvest's determinism re-check replayed every
    # real run against an EMPTY cassette, the call failed, and the run was
    # rejected as non-deterministic: `RecordedRun.model_calls`' own docstring
    # named that outcome — "a correct-looking rejection for the wrong reason"
    # — before anything had been observed doing it. Reproduced on five real
    # runs of a real repo (ADR 0163 §6) and offline for free (ADR 0190).
    #
    # Wrapped ONLY when the run is being recorded, unlike `recorder.py` which
    # wraps always: with no `--config` there is no provider, and this module's
    # contract is that `require_model_provider()` then raises
    # `ServiceNotConfiguredError` rather than a cassette miss. A plain
    # `aef run` is byte-for-byte the run it was.
    recording: CassetteProvider | None = None
    if record_runs_dir is not None and model_provider is not None:
        recording = CassetteProvider(model_provider, on_miss="live")
        model_provider = recording

    durability: DurabilityBackend = (
        FileDurabilityBackend(Path(checkpoints_dir))
        if checkpoints_dir is not None
        else InMemoryDurabilityBackend()
    )

    graph = build_graph()
    # Critic and judge are wired because obligation 2 tells an adopter to put a
    # reflect node in their graph, and every reflect node requires them. Without
    # this, following obligation 2 made obligation 3 (observations, produced by
    # `aef run`) impossible: the run died with ServiceNotConfiguredError. Two
    # documented requirements contradicted each other (ADR 0073).
    #
    # A durable store when a memory path is given: reflections the loop can
    # never read back are not learning.
    memory: MemoryStore = (
        FileMemoryStore(path=Path(memory_path)) if memory_path else InMemoryMemoryStore()
    )
    # Same list the gate path uses (aef/services/runtime.py, ADR 0091), so
    # an agent that runs here can be re-executed there. Four separate defects
    # were the two lists drifting apart.
    # Built from the SAME memory store the agent writes to. A retriever over
    # a different store retrieves nothing and reads as an empty memory.
    # One knowledge store for the retriever AND the consolidate node, or the
    # lessons the graph writes are never the lessons it reads (A1, ADR 0118).
    #
    # And it is REBUILT FROM THE DURABLE MEMORY BEFORE THE GRAPH RUNS, because
    # a fresh store per process plus a retrieve node that runs before the
    # consolidate node means no consolidated lesson is ever in context on any
    # CLI run: `retrieved_signatures` was `[]` in every `aef run`, so ADR
    # 0118's helpful/harmful tally had no producer on the assembled path and
    # A1 was closed only for a caller that constructs `Services` by hand
    # (ADR 0125, reproduced end-to-end through `run_graph_module`).
    # Recompute rather than persist: the consolidator is a stateless function
    # of the memory store by design (ADR 0110), so re-deriving is exactly
    # equivalent to having stored it, with no second source of truth about
    # what has been seen. With an in-memory store there is nothing to rebuild
    # from — the run is a one-off — so this is skipped.
    initial_memory = (
        memory.snapshot(agent_id=agent_id)
        if record_runs_dir is not None and isinstance(memory, FileMemoryStore)
        else None
    )
    knowledge = InMemoryKnowledgeStore()
    if memory_path is not None:
        RuleBasedConsolidator().consolidate(memory, knowledge, agent_id=agent_id)
    retriever = build_retriever(
        context_config, memory=memory, agent_id=agent_id, knowledge=knowledge
    )
    services = agent_services(
        model_provider=model_provider,
        memory=memory,
        knowledge=knowledge,
        retriever=retriever,
        durability=durability,
        policy=policy_config,
        judge_rubric=judge_rubric,
        audit_log=FileAuditLogWriter(Path(audit_log_path)) if audit_log_path else None,
        reflection=reflection,
        reflection_model=reflection_model,
        # Without a `context:` block `build_retriever` returns None and
        # `agent_services` defaults one — over THIS durable, multi-agent store.
        # Unscoped, that handed the run another tenant's records (ADR 0125).
        agent_id=agent_id,
    )
    # Without this an adopter cannot produce a FAILING run from the CLI, so the
    # workflow LOOP.md documents ("record scenarios that fail as well as ones
    # that pass") could not be followed at all (ADR 0070).
    state = AEFState(
        run_id=str(uuid.uuid4()),
        agent_id=agent_id,
        objective=objective,
        working_memory=dict(working_memory or {}),
    )
    executor = GraphExecutor(graph.compile(), services)
    # Bind the actual built-in retriever before execution; never serialize
    # arbitrary configuration or grant replay a provider (ADR 0210).
    context_snapshot = recorded_context(services.retriever) if record_runs_dir is not None else None
    # Tracing is on only when the run is being recorded: a trace costs memory
    # proportional to the run, and every other caller wants the final state.
    result = executor.run(state, record_trace=record_runs_dir is not None)

    if record_runs_dir is not None and result.trace is not None:
        from datetime import UTC, datetime

        from aef.harness.harvest import RecordedRun, save_run

        save_run(
            Path(record_runs_dir),
            RecordedRun(
                run_id=state.run_id,
                graph_id=graph.id,
                graph_version=graph.version,
                initial_state=state,
                trace=result.trace,
                at=datetime.now(UTC),
                model_calls=recording.recorded if recording is not None else (),
                initial_memory=initial_memory,
                context_config=context_snapshot,
                # The provider's own containment declaration, read from the
                # object that answered rather than from config, so the
                # determinism re-check can reproduce ADR 0169's containment
                # record instead of writing `isolation: []` against it
                # (ADR 0163's F-M6-2, closed in ADR 0190).
                provider_isolation=(
                    tuple(sorted(recording.isolation)) if recording is not None else ()
                ),
                # The wrapped provider's name, not the wrapper's — see
                # `RecordedRun.provider_name` for why it is kept even though
                # the containment record currently says 'cassette' on both
                # sides (ADR 0182's open item 1).
                provider_name=(
                    recording.inner.name
                    if recording is not None and recording.inner is not None
                    else ""
                ),
            ),
        )

    return result.final_state
