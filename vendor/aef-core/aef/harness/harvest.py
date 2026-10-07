"""Corpus auto-growth — promoting real runs into scenarios.

A static corpus stops binding as behaviour moves. Every gate is measured
against it, so a corpus that never grows quietly becomes a test of what the
agent used to do.

Four decisions, each of which the obvious alternative gets wrong.

**Failures are promoted automatically; successes only on request.** Failures
carry the information. Auto-promoting successes inflates the pass rate the
gates measure against, so the corpus drifts toward "everything passes" — and
a corpus where everything already passes cannot demonstrate an improvement.

**Always TRAIN. Never validation, never holdout, and no flag to override.**
If production could write to validation, the set that gates candidates would
be shaped by the same system being gated. The holdout is the owner's, and
`recorder.py` already refuses it without explicit consent; here there is not
even a way to ask.

**A run that does not re-execute deterministically is REJECTED, not
recorded.** A flaky scenario makes every downstream gate unreliable, and one
admitted flake poisons every future comparison — the cost is not one bad
scenario, it is a corpus nobody can trust.

**Rate-limited, and the limit counts HARVEST's own promotions only.** One bad
deploy can produce thousands of failing runs; without a limit the corpus fills
with a single incident and the gates start measuring that incident instead of
the agent. That is a statement about what *this* command writes — and the
first version counted every scenario recorded in the last 24 hours, whoever
wrote it. `aef loop bootstrap` stamps `recorded_at = now` on every scenario it
records, so the K5 pilot sequence (adopt, bootstrap, run for real, harvest)
silently dropped every real production failure: bootstrap 12 inputs, harvest 3
real runs, `promoted 0 run(s)`, `3 held back by the daily rate limit`, exit 0
(reproduced, ADR 0141). `Scenario.source` now says who wrote each one and only
`Source.HARVEST` is charged.

**One graph per invocation.** A run whose `graph_id` is not the id of the graph
on the command line is skipped and reported, not re-executed against it. There
was no such filter: every run in the directory was re-executed against whatever
entrypoint was passed and, if it reproduced, promoted with its OWN `graph_id`
stamped on it — a scenario whose label and whose trace describe two different
graphs. On the ADR 0163 pilot this was invisible because a foreign run missed
the cassette and was reported as non-deterministic instead (ADR 0190).

**Re-execution replays the recorded provider's containment declaration.** The
clock is pinned (ADR 0048) and the model is pinned (ADR 0126), and a third
input was not: `PromptAgentNode` writes `provider.isolation` into
`working_memory` on every run (ADR 0169) and the re-check compares the encoded
trace byte for byte, so a replay-only cassette — which declares nothing,
correctly — made every prompt-agent run look non-deterministic. The
declaration recorded at capture time now travels on the `RecordedRun` and is
replayed; nothing live is ever reached (ADR 0190).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from aef.config.factory import build_retriever
from aef.config.schema import ContextConfig
from aef.harness.corpus import (
    Expected,
    Scenario,
    Source,
    Split,
    load_corpus,
    save_scenario,
)
from aef.harness.corpus import fixed_clock as _fixed_clock
from aef.harness.memory_store import memory_payload, memory_record
from aef.harness.outcome import is_recovered
from aef.harness.redaction import RedactionPolicy
from aef.harness.replay_inputs import RecordedIsolation as _RecordedIsolation
from aef.harness.replay_inputs import replay_memory
from aef.harness.trace_codec import decode_trace, dumps, encode_trace, loads
from aef.kernel import GraphExecutor, Services
from aef.kernel.executor import NodeExecutionRecord
from aef.kernel.graph import Graph
from aef.providers.cassette_provider import CassetteProvider, RecordedCall
from aef.services.memory.base import MemoryRecord
from aef.services.runtime import agent_services
from aef.state import AEFState

DEFAULT_DAILY_LIMIT = 5
# On by default: the corpus lives in git (ADR 0119). `redaction=None` turns it off.
DEFAULT_REDACTION = RedactionPolicy()


class HarvestError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecordedRun:
    """A production run, captured completely enough to become a scenario."""

    run_id: str
    graph_id: str
    graph_version: str
    initial_state: AEFState
    trace: tuple[NodeExecutionRecord, ...]
    at: datetime
    # Every model completion the run made (ADR 0123's cassette, ADR 0126).
    # Without these a run whose graph calls a model cannot re-execute at all:
    # the determinism re-check runs with no credential, the call fails, and
    # the run is rejected as non-deterministic — a correct-looking rejection
    # for the wrong reason. Empty for a legacy recorded run and for any graph
    # that never asked a model anything, exactly as `Scenario.model_calls` is.
    model_calls: tuple[RecordedCall, ...] = ()
    # What the provider that answered those calls DECLARED about its own
    # containment, sorted, captured at recording time (ADR 0169's `isolation`
    # set; ADR 0190 records it here).
    #
    # This is data, not a derivation: `PromptAgentNode` writes
    # `working_memory["<node>__containment"]` from `provider.isolation` on
    # every run, and the determinism re-check compares the encoded trace byte
    # for byte. A replay-only cassette has no inner provider and therefore
    # declares nothing, so without this field the re-execution wrote
    # `isolation: [], persona_role: 'unknown'` against a recorded
    # `['no_mcp', ..., 'system_role'], 'system'` and EVERY run of every
    # `aef migrate`-generated prompt-agent graph was rejected as
    # non-deterministic (ADR 0163's F-M6-2, reproduced offline).
    #
    # `persona_role` is deliberately NOT stored beside it: it is
    # `persona_role(isolation)` and nothing else, and a second copy of a
    # derived value is a second thing that can disagree.
    #
    # Empty for a legacy recorded run and for a provider that declared
    # nothing, in which case the replay declares nothing either — which is
    # exactly what it did before, so no legacy run's verdict moves.
    provider_isolation: tuple[str, ...] = ()
    # The name of the provider the recording wrapper wrapped — `command`,
    # `claude_code`, `codex`, … — kept beside its isolation set for one
    # reason, which is forward-compatibility rather than display.
    #
    # `containment["provider"]` is `provider.name`, and today that is the
    # recording wrapper's own `'cassette'` on BOTH sides of the comparison
    # (ADR 0182's open item 1: the wrapper masks the provider name in the
    # containment record, on `bootstrap`'s path and now on this one). It
    # therefore matches by accident. The day that open item is closed —
    # `CassetteProvider.name` forwarding its inner provider's — recording
    # would say `'command'` and a replay whose shim named itself would say
    # something else, and F-M6-2 would reopen in a new spelling. Recorded
    # here, the replay shim answers with the same name the recording had,
    # whichever of the two `CassetteProvider` decides to report.
    provider_name: str = ""
    # Tenant-scoped durable memory BEFORE this run; None for legacy captures.
    initial_memory: tuple[MemoryRecord, ...] | None = None
    # The built-in retriever's approved scalar settings, captured before execution.
    context_config: ContextConfig | None = None

    @property
    def failed(self) -> bool:
        """Did this run fail, as opposed to recovering from something?

        Errors the agent explicitly marked recovered do not count. Before
        that distinction existed, harvest promoted every recovered run into
        the corpus as a failure — so the corpus recorded successful recovery
        as the thing the loop should learn to stop doing (ADR 0076).
        """
        final = self.initial_state
        for record in self.trace:
            final = record.delta.apply(final)
        unrecovered = [e for e in final.errors if not is_recovered(e)]
        return bool(unrecovered) or (final.plan is not None and final.plan.status == "failed")

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "graph_id": self.graph_id,
            "graph_version": self.graph_version,
            "initial_state": self.initial_state.model_dump(mode="json"),
            "trace": encode_trace(self.trace),
            "at": self.at.isoformat(),
            "model_calls": [call.to_payload() for call in self.model_calls],
            "provider_isolation": list(self.provider_isolation),
            "provider_name": self.provider_name,
            "initial_memory": None
            if self.initial_memory is None
            else [memory_payload(r) for r in self.initial_memory],
            "context_config": None
            if self.context_config is None
            else self.context_config.model_dump(mode="json"),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RecordedRun:
        try:
            return cls(
                run_id=payload["run_id"],
                graph_id=payload["graph_id"],
                graph_version=payload["graph_version"],
                initial_state=AEFState.model_validate(payload["initial_state"]),
                trace=decode_trace(payload["trace"]),
                at=datetime.fromisoformat(payload["at"]),
                # Legacy runs recorded before the cassette existed load with
                # none, and behave exactly as they did.
                model_calls=tuple(
                    RecordedCall.from_payload(c) for c in payload.get("model_calls", ())
                ),
                # Sorted on load as well as on capture: the containment record
                # the node writes is `sorted(isolation)`, and a payload someone
                # hand-edited into another order would replay a set that
                # encodes differently from the one that was recorded.
                provider_isolation=tuple(
                    sorted(str(s) for s in payload.get("provider_isolation", ()))
                ),
                provider_name=str(payload.get("provider_name", "")),
                initial_memory=None
                if payload.get("initial_memory") is None
                else tuple(memory_record(r) for r in payload["initial_memory"]),
                context_config=None
                if payload.get("context_config") is None
                else ContextConfig.model_validate(payload["context_config"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HarvestError(f"malformed recorded run: {exc}") from exc


def save_run(root: Path, run: RecordedRun) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{run.run_id}.json"
    path.write_text(dumps(run.to_payload()))
    return path


def load_runs(root: Path) -> tuple[RecordedRun, ...]:
    if not root.is_dir():
        return ()
    return tuple(
        RecordedRun.from_payload(loads(p.read_text())) for p in sorted(root.glob("*.json"))
    )


@dataclass(frozen=True)
class HarvestOutcome:
    promoted: tuple[str, ...] = ()
    skipped_passing: tuple[str, ...] = ()
    skipped_existing: tuple[str, ...] = ()
    # Runs of a DIFFERENT graph than the one named on the command line.
    # `harvest()` iterated the runs directory with no filter and stamped each
    # promoted scenario with the run's own `graph_id` while having re-executed
    # it against the graph it was handed — so a scenario could enter the
    # corpus labelled 'graph-a' carrying the trace of 'graph-b's entrypoint.
    # On the pilot this was masked: a run from another persona missed the
    # cassette and was rejected as non-deterministic instead. The masking was
    # F-M6-1's doing, and it stops the moment F-M6-1 is fixed (ADR 0163 §6,
    # third observation; ADR 0190).
    skipped_other_graph: tuple[str, ...] = ()
    rejected_nondeterministic: tuple[str, ...] = ()
    skipped_rate_limited: tuple[str, ...] = ()
    # Redaction (ADR 0119): the run's behaviour depended on something the
    # redactor removed, or a secret survived into the scenario that would
    # have been written. Neither is recorded.
    rejected_redaction_changed_behaviour: tuple[str, ...] = ()
    rejected_unredactable: tuple[str, ...] = ()
    redactions: int = 0
    # What the rate limit was and what had already spent it. "held back by the
    # daily rate limit" named a rule and no arithmetic, so a run held back by a
    # budget something ELSE had consumed read exactly like one held back by
    # harvest's own volume (ADR 0141).
    daily_limit: int = DEFAULT_DAILY_LIMIT
    harvested_today: int = 0
    other_sources_today: tuple[tuple[str, int], ...] = ()

    @property
    def lines(self) -> tuple[str, ...]:
        out: list[str] = [f"promoted {len(self.promoted)} run(s) to the train split"]
        for label, items in (
            ("already in the corpus", self.skipped_existing),
            ("recorded from another graph, not re-executed here", self.skipped_other_graph),
            ("passed, not promoted", self.skipped_passing),
            ("REJECTED, did not re-execute deterministically", self.rejected_nondeterministic),
            ("held back by the daily rate limit", self.skipped_rate_limited),
            (
                "REJECTED, behaviour changed under redaction",
                self.rejected_redaction_changed_behaviour,
            ),
            ("REJECTED, a secret survived redaction", self.rejected_unredactable),
        ):
            if items:
                out.append(f"  {len(items)} {label}: {', '.join(sorted(items)[:5])}")
            if items and label.startswith("held back"):
                out.append(
                    f"      the limit is {self.daily_limit} HARVESTED scenario(s) per 24h; "
                    f"{self.harvested_today} had been harvested before this run and "
                    f"{len(self.promoted)} were promoted by it"
                )
                if self.other_sources_today:
                    spent = ", ".join(
                        f"{count} from {name}" for name, count in self.other_sources_today
                    )
                    out.append(
                        f"      ({spent} today, which do NOT count against it — "
                        f"the limit is on this command's own promotions, ADR 0141)"
                    )
        if self.redactions:
            out.append(f"  {self.redactions} substitution(s) made by the redaction policy")
        return tuple(out)


def _reexecution_services(
    scenario: Scenario, isolation: tuple[str, ...] = (), provider_name: str = ""
) -> Services:
    """Mirrors `scenario_runner.run_scenario` — harvest asks the same
    question the gates do, so it has to ask it of the same environment.

    That includes the cassette (ADR 0126). The clock was pinned here and the
    model was not, so a harvested run whose graph calls a model re-executed
    against no provider at all, failed, and was rejected as
    "non-deterministic" — the rejection a flaky run gets, for a run that was
    perfectly reproducible. `on_miss="fail"` and no live provider, because a
    harvest that reaches the network to decide whether a run is deterministic
    has already lost the property it is checking.

    And it includes the recorded provider's `isolation` declaration (ADR
    0190): pinning the clock and the model still left one input to the run
    unpinned, and a prompt-agent node reads it on every execution. Still no
    live provider — `_RecordedIsolation` answers nothing — so the property
    this function is checking is intact.
    """
    cassette = CassetteProvider(
        _RecordedIsolation(isolation, provider_name) if (isolation or provider_name) else None,
        scenario.model_calls,
        on_miss="fail",
    )
    memory, knowledge = replay_memory(scenario.initial_memory, scenario.initial_state.agent_id)
    return agent_services(
        clock=_fixed_clock(scenario),
        memory=memory,
        knowledge=knowledge,
        agent_id=scenario.initial_state.agent_id,
        model_provider=cassette,
        retriever=build_retriever(
            scenario.context_config,
            memory=memory,
            knowledge=knowledge,
            agent_id=scenario.initial_state.agent_id,
        ),
    )


def _reexecutes_identically(run: RecordedRun, graph: Graph) -> bool:
    """Re-run from the recorded initial state with the recorded clock.

    A scenario that does not reproduce is worse than no scenario: every gate
    downstream compares against it, so one admitted flake makes every future
    comparison unreliable.
    """
    scenario = Scenario(
        id=run.run_id,
        split=Split.TRAIN,
        graph_id=run.graph_id,
        graph_version=run.graph_version,
        initial_state=run.initial_state,
        trace=run.trace,
        recorded_at=run.at,
        model_calls=run.model_calls,
        initial_memory=run.initial_memory,
        context_config=run.context_config,
        provider_isolation=run.provider_isolation,
        provider_name=run.provider_name,
    )
    try:
        # The same services the gate runner supplies. A bare `Services()`
        # here made every reflect-node or policy-gated agent fail the
        # determinism re-check for a missing service rather than for
        # non-determinism, so harvest silently promoted nothing (ADR 0079).
        result = GraphExecutor(
            graph.compile(),
            _reexecution_services(scenario, run.provider_isolation, run.provider_name),
        ).run(run.initial_state, record_trace=True)
    except Exception:  # noqa: BLE001 - any failure to reproduce is a rejection
        return False
    if result.trace is None:
        return False
    return dumps(encode_trace(result.trace)) == dumps(encode_trace(run.trace))


def _reexecute(
    state: AEFState, run: RecordedRun, graph: Graph
) -> tuple[NodeExecutionRecord, ...] | None:
    """Trace of re-running `graph` on `state` under the run's recorded clock,
    or None if it failed to run at all."""
    scenario = Scenario(
        id=run.run_id,
        split=Split.TRAIN,
        graph_id=run.graph_id,
        graph_version=run.graph_version,
        initial_state=state,
        trace=run.trace,
        recorded_at=run.at,
        model_calls=run.model_calls,
        initial_memory=run.initial_memory,
        context_config=run.context_config,
        provider_isolation=run.provider_isolation,
        provider_name=run.provider_name,
    )
    try:
        result = GraphExecutor(
            graph.compile(),
            _reexecution_services(scenario, run.provider_isolation, run.provider_name),
        ).run(state, record_trace=True)
    except Exception:  # noqa: BLE001 - any failure to reproduce is a rejection
        return None
    return result.trace


#: Fields the HARNESS generates and the tenant never types. Each is dropped
#: from the output scan by exact path, never by teaching a pattern to ignore a
#: shape — the shape is the point, and a UUID in an objective is exactly the
#: thing ADR 0197 added a pattern for.
_HARNESS_IDENTIFIERS: tuple[tuple[str, ...], ...] = (
    ("id",),
    ("initial_state", "run_id"),
)


def _blank_values(node: Any, values: set[str]) -> Any:
    """`node` with every occurrence of a string in `values` replaced by a
    fixed placeholder. Used to hold the harness's own identifiers out of the
    output scan by value rather than by an ever-growing list of field paths
    (ADR 0192's F-N7-4)."""
    if isinstance(node, dict):
        return {k: _blank_values(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [_blank_values(v, values) for v in node]
    if isinstance(node, str):
        for value in values:
            node = node.replace(value, "<harness-id>")
        return node
    return node


def _scannable(scenario: Scenario) -> dict[str, Any]:
    """The scenario payload the output scan reads: everything except the
    identifiers the harness itself generated.

    Two kinds, and both are the same argument.

    A `RecordedCall.key` is a 64-character SHA-256 hex string the harness
    computes from the request — it is not tenant text, it is recomputed on
    load rather than trusted, and it matches `opaque_secret` every single
    time. Left in, it rejected EVERY model-calling run as
    "a secret survived redaction" (found while fixing ADR 0126's F12; the
    twenty summary scenarios ADR 0123 recorded all match it too).

    A run id is a `uuid4` the harness assigns, and it is the scenario's `id`
    and its `initial_state.run_id`. It carries no tenant information for the
    same reason the digest does not, and since ADR 0197 gave the policy a
    `uuid` pattern it matches every single time too — which rejected every
    harvest of a recorded run (`tests/cli/test_run.py::test_a_recorded_run_
    re_executes_identically_and_is_harvested` caught it).

    Dropping is by exact field path, not by weakening the pattern: a UUID a
    tenant typed into an objective, or one a tool returned, is still scanned
    and still rejects the run — which is the whole point of ADR 0197 and of
    ADR 0163's residual.

    Everything a model was actually asked and answered is still scanned.
    """
    payload = scenario.to_payload()
    # The scenario's own run id is not only at the two paths below: the harness
    # threads it through every trace record's `input_state.run_id`, and its
    # `context.run_id`, `context.trace_id` and `context.idempotency_key`
    # (ADR 0192's F-N7-4, found on the peptideindex pilot when the corpus could
    # not be rebuilt). A path list that must name each of those is a list that
    # grows silently the next time a field carries the id, so the hold-out is
    # by VALUE: the exact identifiers THIS harness assigned to THIS scenario,
    # wherever they appear. A UUID from anywhere else — a tenant's, a tool's,
    # one typed into an objective — is a different string and still rejects the
    # run, which is what ADR 0197's pattern is for.
    harness_ids = {i for i in (scenario.id, scenario.initial_state.run_id) if i}
    if harness_ids:
        payload = _blank_values(payload, harness_ids)
    for path in _HARNESS_IDENTIFIERS:
        node: Any = payload
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(path[-1], None)
    calls = payload.get("model_calls")
    if isinstance(calls, list):
        for call in calls:
            if isinstance(call, dict):
                call.pop("key", None)
    # A pre-run snapshot has provenance from earlier CLI runs. Exempt only
    # their UUID4 metadata field, never matching values inside lesson content
    # or the trace. Those still carry tenant evidence and must be scanned.
    for record in payload.get("initial_memory") or ():
        run_id = record.get("run_id")
        try:
            generated_id = UUID(run_id) if isinstance(run_id, str) else None
        except ValueError:
            generated_id = None
        if generated_id is not None and generated_id.version == 4 and str(generated_id) == run_id:
            record.pop("run_id", None)
    # Consolidation repeats those provenance ids in structured knowledge
    # chunks. Scan the rest of each chunk, and the original memory content,
    # normally. Do not blank matching values in objectives or free text.
    prior_ids = {r.run_id for r in scenario.initial_memory or () if r.run_id}

    def scan_chunks(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                scan_chunks(child)
        elif isinstance(value, dict):
            if str(value.get("source", "")).startswith("knowledge:"):
                content = value.get("content")
                if isinstance(content, str):
                    try:
                        structured = json.loads(content)
                    except (ValueError, TypeError):
                        structured = None
                    if isinstance(structured, dict):
                        ids = structured.get("run_ids")
                        if isinstance(ids, list) and all(
                            isinstance(i, str) and i in prior_ids for i in ids
                        ):
                            structured.pop("run_ids")
                            value["content"] = json.dumps(structured, sort_keys=True)
            for child in value.values():
                scan_chunks(child)

    scan_chunks(payload)
    return payload


def _behaviour(initial: AEFState, trace: tuple[NodeExecutionRecord, ...]) -> tuple[Any, ...]:
    """What must survive redaction for the scenario to still be the same
    failure: the node path, which nodes errored, and the plan's status.
    Text is allowed to differ — that is what redaction changes."""
    final = initial
    for record in trace:
        final = record.delta.apply(final)
    failing = tuple(e.get("node_id") for e in final.errors if not is_recovered(e))
    status = final.plan.status if final.plan is not None else None
    return (tuple(r.node_id for r in trace), failing, status)


def harvest(
    runs_dir: Path,
    corpus_root: Path,
    graph: Graph,
    *,
    now: datetime,
    include_successes: bool = False,
    daily_limit: int = DEFAULT_DAILY_LIMIT,
    redaction: RedactionPolicy | None = DEFAULT_REDACTION,
) -> HarvestOutcome:
    """`redaction` is ON by default and `None` turns it off explicitly — the
    corpus lives in git, and a harvest that writes tenant text unless told
    not to is the wrong default (ADR 0119)."""
    corpus = load_corpus(corpus_root) if corpus_root.is_dir() else None
    existing = {s.id for s in corpus.scenarios} if corpus else set()

    cutoff = now - timedelta(days=1)
    # HARVEST's own promotions only. A scenario `bootstrap` or `record`
    # wrote today is not this command filling the corpus with one incident,
    # and charging it here dropped every real run of the K5 pilot sequence
    # (ADR 0141).
    recent = tuple(s for s in corpus.scenarios if s.recorded_at > cutoff) if corpus else ()
    already_today = sum(1 for s in recent if s.source is Source.HARVEST)
    others_today = {
        source: sum(1 for s in recent if s.source is source)
        for source in (Source.BOOTSTRAP, Source.RECORD, Source.UNSPECIFIED)
    }
    budget = max(daily_limit - already_today, 0)

    promoted: list[str] = []
    passing: list[str] = []
    duplicate: list[str] = []
    flaky: list[str] = []
    limited: list[str] = []
    changed: list[str] = []
    unredactable: list[str] = []
    other_graph: list[str] = []
    substitutions = 0

    for run in load_runs(runs_dir):
        if run.run_id in existing:
            duplicate.append(run.run_id)
            continue
        # BEFORE the determinism re-check, because re-executing another
        # graph's run against this entrypoint is the thing being prevented,
        # not a cheaper way of detecting it: a run that happens to reproduce
        # would be promoted, stamped with its own `graph_id`, and the corpus
        # would hold a scenario whose recorded graph and re-executed graph
        # are two different graphs (ADR 0190).
        if run.graph_id != graph.id:
            other_graph.append(run.run_id)
            continue
        if not run.failed and not include_successes:
            passing.append(run.run_id)
            continue
        if len(promoted) >= budget:
            limited.append(run.run_id)
            continue
        if not _reexecutes_identically(run, graph):
            flaky.append(run.run_id)
            continue

        initial_state, trace = run.initial_state, run.trace
        notes = "harvested from a real run; re-execution verified"
        if redaction is not None:
            redacted_state, count = redaction.redact_state(run.initial_state)
            if count:
                # Redact the input, re-execute, keep THAT trace — never patch
                # the recorded one. Admit only if the failure is the same
                # failure; text may differ, behaviour may not.
                redacted_trace = _reexecute(redacted_state, run, graph)
                if redacted_trace is None or _behaviour(
                    redacted_state, redacted_trace
                ) != _behaviour(run.initial_state, run.trace):
                    changed.append(run.run_id)
                    continue
                initial_state, trace = redacted_state, redacted_trace
                notes += f"; {count} redaction(s) applied to the input before re-execution"
                substitutions += count
        scenario = Scenario(
            id=run.run_id,
            split=Split.TRAIN,
            graph_id=run.graph_id,
            graph_version=run.graph_version,
            initial_state=initial_state,
            trace=trace,
            recorded_at=run.at,
            notes=notes,
            # The cassette travels with the scenario, or the gates that
            # re-execute it hit the same wall harvest just cleared.
            model_calls=run.model_calls,
            initial_memory=run.initial_memory,
            context_config=run.context_config,
            provider_isolation=run.provider_isolation,
            provider_name=run.provider_name,
            # Harvested runs carry NO owner claim. Only a human can say a
            # task should have failed, and a MUST_FAIL label invented by
            # the system would be a tripwire the system set for itself.
            expected=Expected.UNSPECIFIED,
            # Provenance, so this command's rate limit charges this command
            # and nothing else (ADR 0141).
            source=Source.HARVEST,
        )
        # The output scan: a secret that survived the input redaction came
        # from somewhere the redactor cannot reach. Rejected, not written.
        if redaction is not None and redaction.find(_scannable(scenario)):
            unredactable.append(run.run_id)
            continue
        save_scenario(corpus_root, scenario)
        promoted.append(run.run_id)

    return HarvestOutcome(
        daily_limit=daily_limit,
        harvested_today=already_today,
        other_sources_today=tuple(
            (source.value, count) for source, count in others_today.items() if count
        ),
        promoted=tuple(promoted),
        skipped_passing=tuple(passing),
        skipped_existing=tuple(duplicate),
        skipped_other_graph=tuple(other_graph),
        rejected_nondeterministic=tuple(flaky),
        skipped_rate_limited=tuple(limited),
        rejected_redaction_changed_behaviour=tuple(changed),
        rejected_unredactable=tuple(unredactable),
        redactions=substitutions,
    )
