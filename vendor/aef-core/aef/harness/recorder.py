"""Promoting a real run into a corpus scenario.

The corpus is what every behavioural gate stands on, so how entries get into
it matters as much as what the gates do with them.

**Scenarios are recorded, never hand-authored.** A hand-written scenario
encodes what someone *believed* the graph does; a recorded one encodes what
it did. The difference shows up exactly when they diverge, which is the case
the corpus exists to catch.

**The holdout is not writable by default.** `train` is the proposer's
evidence, `validation` is what gates score against, and `holdout` is the
owner's only independent read. A recorder that filled the holdout as
casually as it fills train would destroy that independence without anyone
noticing — so writing there needs an explicit, separate act.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from aef.harness.checks import TaskCheck
from aef.harness.corpus import (
    Expected,
    Scenario,
    Source,
    Split,
    load_corpus,
    save_scenario,
)
from aef.harness.memory_store import FileMemoryStore
from aef.harness.outcome import classify
from aef.harness.replay_inputs import recorded_context
from aef.kernel import GraphExecutor, Services
from aef.kernel.graph import Graph
from aef.providers.cassette_provider import CassetteProvider
from aef.state import AEFState


class RecorderError(RuntimeError):
    pass


class HoldoutWriteRefused(RecorderError):
    """Writing to the owner's holdout without explicit consent. Its own type
    because it is not a validation failure — it is a request to spend the
    only independent evidence the owner has."""


@dataclass(frozen=True)
class RecordedScenario:
    scenario: Scenario
    path: Path


def refuse_existing_ids(root: Path, scenario_ids: Sequence[str]) -> None:
    """Raise unless every id is new to the corpus at `root`, and distinct.

    Silently replacing a scenario is how a corpus stops binding: the entry
    that used to fail is gone, and `check_never_shrinks` cannot tell, because
    the id is still there.

    Batch-shaped because `bootstrap` records many scenarios in one act and
    must refuse the whole invocation before writing any of them. One
    implementation and one wording, shared with `record_to_corpus` — a second
    copy of a rule is where the two drift apart (ADR 0091).
    """
    seen: set[str] = set()
    repeated: set[str] = set()
    for sid in scenario_ids:
        (repeated if sid in seen else seen).add(sid)
    if repeated:
        raise RecorderError(
            f"scenario id(s) requested more than once in the same batch: {sorted(repeated)}. The "
            f"second recording would overwrite the first before anything could read it."
        )
    existing = {s.id for s in load_corpus(root).scenarios} if root.is_dir() else set()
    clashing = sorted(set(scenario_ids) & existing)
    if clashing:
        raise RecorderError(
            f"scenario id(s) already exist: {clashing}. Overwriting would replace the "
            f"behaviour the corpus recorded with the behaviour it has now, which is "
            f"precisely the regression a corpus exists to catch. Pick a new id."
        )


def record_run(
    graph: Graph,
    initial_state: AEFState,
    services: Services,
    *,
    scenario_id: str,
    split: Split = Split.TRAIN,
    recorded_at: datetime,
    notes: str = "",
    allow_holdout: bool = False,
    expected: Expected = Expected.UNSPECIFIED,
    checks: tuple[TaskCheck, ...] = (),
    budget_ms: float | None = None,
    source: Source = Source.RECORD,
) -> Scenario:
    """Execute `graph` and capture the run as a `Scenario`.

    The clock is whatever `services` supplies; the recorded `Context.now`
    values become the scenario's pinned clock (ADR 0048), so a re-execution
    observes exactly what the recording did. Model calls are pinned the same
    way (ADR 0123). `checks` and `budget_ms` are the OWNER's claims about the
    answer (ADR 0113) and are stored, not evaluated, here — the recording
    says what happened; the checks say what should have.
    """
    if split is Split.HOLDOUT and not allow_holdout:
        raise HoldoutWriteRefused(
            "refusing to write to the holdout split. It is the owner's only independent "
            "read of whether the loop is improving anything, and a recorder that fills it "
            "as casually as it fills train destroys that independence silently. Pass "
            "allow_holdout=True to spend it deliberately."
        )

    # Every model call the run makes is captured (ADR 0123): the recording
    # cassette starts empty and lets each distinct request through to the
    # caller's provider, keeping the answer. Re-execution then serves those
    # answers and needs no credential. Wrapped ALWAYS, not only when a
    # provider is configured — a graph that calls a model with none
    # configured fails naming the miss, which is the same errored run it was.
    provider = services.model_provider
    context_snapshot = recorded_context(services.retriever)
    initial_memory = (
        services.memory.snapshot(agent_id=initial_state.agent_id)
        if isinstance(services.memory, FileMemoryStore)
        else None
    )
    recording = CassetteProvider(provider, on_miss="live")
    services = replace(services, model_provider=recording)

    result = GraphExecutor(graph.compile(), services).run(initial_state, record_trace=True)
    if result.trace is None:  # pragma: no cover - record_trace=True guarantees it
        raise RecorderError("execution produced no trace to record")
    if not result.trace:
        raise RecorderError(
            f"scenario {scenario_id!r} executed no nodes; an empty trace pins nothing and "
            f"would pass every gate vacuously"
        )

    # Same `classify` the gates read, so "the agent completed it" means here
    # exactly what it means to G2.
    if (
        expected is Expected.MUST_FAIL
        and classify(result.final_state, result.trace, terminated=True).passed
    ):
        raise RecorderError(
            f"refusing to label {scenario_id!r} MUST_FAIL: the agent just completed it. A "
            f"tripwire must be impossible in principle, not merely hard — labelling an "
            f"achievable task MUST_FAIL makes every real improvement look like reward "
            f"hacking, which is the opposite of what the tripwire is for. Record a task "
            f"genuinely beyond this agent's remit."
        )

    return Scenario(
        id=scenario_id,
        split=split,
        graph_id=graph.id,
        graph_version=graph.version,
        initial_state=initial_state,
        trace=result.trace,
        recorded_at=recorded_at,
        notes=notes,
        expected=expected,
        checks=checks,
        budget_ms=budget_ms,
        model_calls=recording.recorded,
        initial_memory=initial_memory,
        context_config=context_snapshot,
        provider_isolation=tuple(sorted(provider.isolation)) if provider is not None else (),
        provider_name=provider.name if provider is not None else "",
        # Which command admitted it (ADR 0141). RECORD by default because
        # this function IS `aef loop record`; `bootstrap` and `harvest` say
        # so, and `harvest`'s daily limit charges only its own.
        source=source,
    )


def record_to_corpus(
    root: Path,
    graph: Graph,
    initial_state: AEFState,
    services: Services,
    *,
    scenario_id: str,
    split: Split = Split.TRAIN,
    recorded_at: datetime,
    notes: str = "",
    allow_holdout: bool = False,
    expected: Expected = Expected.UNSPECIFIED,
    checks: tuple[TaskCheck, ...] = (),
    budget_ms: float | None = None,
    source: Source = Source.RECORD,
) -> RecordedScenario:
    """Record and persist, refusing to overwrite an existing scenario —
    see `refuse_existing_ids`, which is that rule."""
    refuse_existing_ids(root, [scenario_id])

    scenario = record_run(
        graph,
        initial_state,
        services,
        scenario_id=scenario_id,
        split=split,
        recorded_at=recorded_at,
        notes=notes,
        allow_holdout=allow_holdout,
        expected=expected,
        checks=checks,
        budget_ms=budget_ms,
        source=source,
    )
    return RecordedScenario(scenario=scenario, path=save_scenario(root, scenario))
