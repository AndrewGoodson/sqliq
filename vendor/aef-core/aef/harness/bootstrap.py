"""A corpus on day one — running the graph once per owner-supplied input.

`aef adopt` writes a `corpus/README.md` and nothing else, and G2/G3 correctly
refuse an empty corpus. So the loop an adopter has just installed can do
nothing until they hand-record scenarios one `aef loop record` invocation at
a time, each needing its own `--working-memory` JSON blob to reach a failing
case at all (ADR 0074 added that flag; it did not make it cheap). This module
turns a file of inputs into a train corpus in one command.

Four rules, each with a precedent this module is not allowed to break.

**Always TRAIN. Never validation, never holdout, and no flag to override.**
Verbatim from `harvest.py`: if the system could fill the set that gates it,
the gate would measure the system's own choices. `recorder.py` refuses the
holdout without explicit consent; here, as in harvest, there is not even a
way to ask.

**Never labels `expected`.** Only an owner can say a task *should* have
failed (ADR 0060), and a `must_fail` label the system invented is a tripwire
the system set for itself. Bootstrap records what happened and prints which
ids the owner should consider marking. An `expected` key in the inputs file
is REFUSED rather than ignored — an owner who wrote a claim and had it
silently dropped would believe the corpus carries it.

**Refuses to overwrite an existing scenario id**, and refuses the whole
invocation before running anything, so a batch that collides halfway through
does not leave a half-written corpus behind. The check and its wording are
`recorder.refuse_existing_ids`, shared rather than restated — a second copy
of a rule is ADR 0091's drift waiting to happen.

**Reports the failure count, and says so when it is zero.** A corpus where
everything already passes cannot demonstrate an improvement: every gate that
reads it has nothing to hold a candidate to. That is a finding about the
inputs, not a success, and it is printed as one.

**And "failed" means what the failure-memory producer means by it** (ADR 0174).
The count was `classify`'s alone — did the run raise, or end with a failed plan
— while the owner's `checks` sat in the same inputs file, unread by the report.
S3b reproduced the consequence: eight inputs, three of them content negatives
the owner's own checks caught, and `0 of 8 recorded run(s) failed. A corpus
where everything passes cannot demonstrate an improvement` printed underneath
them. The two observations stay distinguishable in the line — a crash and a
wrong answer need different fixes — but they are one count, and it is the same
count `check_failure_record` writes memory from.

**Fresh services per input.** The gates re-execute each scenario in
isolation, with its own `InMemoryMemoryStore` (`harvest._reexecution_services`).
A bootstrap that shared one store across inputs would record traces whose
later runs depended on what earlier ones remembered — scenarios that cannot
reproduce alone, which is the one thing a corpus must never contain.

**And the reflections still have to outlive the process** (ADR 0145). The
fourth thing an adopted repo needed before `aef loop cycle` could propose
anything was *a failing run whose memory the cycle can read*, and ADR 0139
measured bootstrap as unable to supply it: every input got its own in-memory
store, so the `MemoryRecord` a failing run's reflect node wrote died with the
process and the next `aef loop cycle --memory M` said `no admissible failure
memory: no candidate this cycle`. `RunScopedMemory` below resolves the two
requirements instead of trading one away — reads stay scoped to the run that
made them, writes ALSO land in a durable sink — and it invents nothing: the
sink holds exactly the records the graph's own reflect node wrote, and a
graph that reflects on nothing leaves an empty sink (ADR 0060).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from aef.harness.check_memory import record_check_outcomes
from aef.harness.checks import CheckError, TaskCheck
from aef.harness.corpus import Expected, Scenario, Source, Split
from aef.harness.outcome import classify
from aef.harness.recorder import RecorderError, record_to_corpus, refuse_existing_ids
from aef.kernel import Services
from aef.kernel.graph import Graph
from aef.services.memory.base import MemoryKind, MemoryRecord, MemoryStore
from aef.services.memory.in_memory import InMemoryMemoryStore
from aef.state import AEFState

DEFAULT_PREFIX = "bootstrap"

# `CassetteProvider`'s wording for "this run asked a model something and there
# was nothing to ask". Matched rather than re-raised because the recorder
# reports per-input errors as strings — see `_provider_lines`. If the provider
# ever rephrases this, the hint stops firing and a test says so.
NO_PROVIDER_MARKER = "no live provider to fall through to"

# Everything an input may say. Anything else is a typo or a claim this
# command does not honour, and both are worth an error rather than a shrug.
INPUT_KEYS = frozenset({"id", "objective", "working_memory", "notes", "checks", "budget_ms"})

# Keys that name a rule bootstrap enforces. Refused by name, with the
# command that DOES accept them, so an owner never believes a claim landed.
REFUSED_KEYS: dict[str, str] = {
    "expected": (
        "only an owner can say a task should have failed (ADR 0060), and bootstrap has just "
        "run the task, so any label it wrote would be one the system set for itself. Record "
        "the tripwire deliberately instead: aef loop record <module> --expected must_fail"
    ),
    "split": (
        "bootstrap writes the train split only, as harvest does: if the system could fill "
        "the set that gates it, the gate would measure the system's own choices. Use "
        "aef loop record --split validation for a scenario the gates score against"
    ),
}


class BootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunScopedMemory(MemoryStore):
    """Reads see one run; writes are also kept somewhere that outlives it.

    Two rules meet here and neither may be given up.

    *Isolation* (ADR 0138): a scenario that only reproduces after its
    predecessors have run is a scenario no gate can trust, so a bootstrap
    input must never READ what another input remembered. Every query and
    every `get` therefore goes to `scratch` alone — an `InMemoryMemoryStore`
    built fresh for this input and thrown away after it.

    *Durability* (ADR 0145): the failing run's reflection is the evidence
    `aef loop cycle` proposes from, and a store discarded at process exit
    hands it nothing. So each write is mirrored into `sink`, the same
    `FileMemoryStore` the cycle reads.

    The mirror is a copy of the record the node wrote — same id, same
    `run_id`, same content — because bootstrap is not allowed to author
    memory. It records what the run did; it never decides that a run failed
    (ADR 0060). A graph with no reflect node writes nothing here and the
    sink stays empty, which is the honest report that this repo has no
    producer of failure memory yet.

    `sink=None` is the pre-0145 behaviour exactly: scratch only.
    """

    scratch: MemoryStore
    sink: MemoryStore | None = None
    # Ids actually mirrored, so the command can report what the graph left
    # behind rather than what it hopes was left behind.
    mirrored: list[str] = field(default_factory=list)

    def write(self, record: MemoryRecord) -> str:
        record_id = self.scratch.write(record)
        if self.sink is not None:
            self.sink.write(record)
            self.mirrored.append(record.id)
        return record_id

    def get(self, record_id: str) -> MemoryRecord | None:
        return self.scratch.get(record_id)

    def query(
        self,
        kind: MemoryKind,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        tags: tuple[str, ...] = (),
        limit: int = 10,
    ) -> list[MemoryRecord]:
        return self.scratch.query(kind, run_id=run_id, agent_id=agent_id, tags=tags, limit=limit)


@dataclass(frozen=True)
class BootstrapInput:
    """One task to run. `checks` and `budget_ms` are the OWNER's claims about
    the answer (ADR 0113) and are carried through verbatim — unlike
    `expected`, they are a specification of the task written before the run,
    not a judgement about what the run turned out to do, so nothing about
    reading them here lets the system grade itself."""

    id: str
    objective: str
    working_memory: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    checks: tuple[TaskCheck, ...] = ()
    budget_ms: float | None = None


def _parse_input(raw: Any, index: int, prefix: str) -> BootstrapInput:
    where = f"input {index}"
    if not isinstance(raw, dict):
        raise BootstrapError(f"{where}: each input must be a JSON object, got {type(raw).__name__}")
    for key in raw:
        if key in REFUSED_KEYS:
            raise BootstrapError(f"{where}: {key!r} is not accepted — {REFUSED_KEYS[key]}")
    unknown = sorted(set(raw) - INPUT_KEYS)
    if unknown:
        raise BootstrapError(
            f"{where}: unknown key(s) {unknown}; an input may set {sorted(INPUT_KEYS)}"
        )

    objective = raw.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        raise BootstrapError(f"{where}: 'objective' is required and must be a non-empty string")

    scenario_id = raw.get("id", f"{prefix}-{index}")
    if not isinstance(scenario_id, str) or not scenario_id.strip():
        raise BootstrapError(f"{where}: 'id' must be a non-empty string")

    working_memory = raw.get("working_memory", {})
    if not isinstance(working_memory, dict):
        raise BootstrapError(
            f"{where}: 'working_memory' must be a JSON object — it seeds "
            f"AEFState.working_memory, which is how an input reaches the agent's FAILING "
            f"cases (ADR 0074)"
        )

    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        raise BootstrapError(f"{where}: 'notes' must be a string")

    raw_checks = raw.get("checks", [])
    if not isinstance(raw_checks, list):
        raise BootstrapError(f"{where}: 'checks' must be a JSON list of check objects")
    checks: list[TaskCheck] = []
    for entry in raw_checks:
        if not isinstance(entry, dict):
            raise BootstrapError(f"{where}: a check must be a JSON object")
        try:
            checks.append(TaskCheck.from_payload(entry))
        except CheckError as exc:
            # Malformed here, before anything runs: a check that loaded wrong
            # would score its scenario 0 forever and read as a regression.
            raise BootstrapError(f"{where}: {exc}") from exc

    budget_ms = raw.get("budget_ms")
    if budget_ms is not None and not isinstance(budget_ms, int | float):
        raise BootstrapError(f"{where}: 'budget_ms' must be a number of milliseconds")

    return BootstrapInput(
        id=scenario_id,
        objective=objective,
        working_memory=dict(working_memory),
        notes=notes,
        checks=tuple(checks),
        budget_ms=None if budget_ms is None else float(budget_ms),
    )


def load_inputs(path: Path, *, prefix: str = DEFAULT_PREFIX) -> tuple[BootstrapInput, ...]:
    """Parse an inputs file.

    Shape, documented here and in `--inputs`' help: a JSON list of objects,
    or an object with an `inputs` list. Each object needs `objective` and may
    set `id` (default `<prefix>-<n>`), `working_memory`, `notes`, `checks`
    and `budget_ms`. Objective plus working memory is the minimum because
    that is exactly what `AEFState` needs to reach a task at all, failing
    ones included.
    """
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise BootstrapError(f"no inputs file at {path}") from exc
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"{path}: not valid JSON: {exc}") from exc

    if isinstance(payload, dict):
        payload = payload.get("inputs")
    if not isinstance(payload, list):
        raise BootstrapError(
            f"{path}: expected a JSON list of inputs, or an object with an 'inputs' list"
        )
    if not payload:
        raise BootstrapError(
            f"{path}: no inputs. Bootstrap runs the graph once per input; with none there "
            f"is nothing to record and the corpus stays empty."
        )
    return tuple(_parse_input(raw, i + 1, prefix) for i, raw in enumerate(payload))


@dataclass(frozen=True)
class BootstrapOutcome:
    """What one bootstrap did. `failed` is the number the whole command
    exists to report."""

    recorded: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    # (id, message) for an input whose run raised before producing a trace.
    # Nothing is recorded for those: a scenario with no trace pins nothing.
    errored: tuple[tuple[str, str], ...] = ()
    # Model calls this recording spent. Recording is the ONE pass that is
    # supposed to be live — the cassette the gates replay from does not exist
    # until something makes the call once (ADR 0123) — so the number is
    # reported rather than buried. Counted from the cassettes the recorder
    # pinned, never from an estimate.
    model_calls: int = 0
    # Records the graph's own reflect node left in the durable sink.
    # `None` means no durable store was asked for, which is a different fact
    # from "asked for and nothing was written" — the second is a finding
    # about the graph and is reported as one. Bootstrap authors none of these
    # records either way (ADR 0060).
    memory_records: int | None = None
    # Runs that answered cleanly and failed one of the OWNER's checks, each of
    # which left one `kind="failure"` record (ADR 0174).
    #
    # Kept apart from `failed` because they are different observations —
    # `classify` answers "did this run raise or end with a failed plan", a
    # check answers "was the answer right" — and reporting one as the other
    # would tell an owner their run crashed when it merely got the task wrong.
    # But BOTH are failures of the run, and `failure_count` below is the number
    # the report leads with, because the alternative is what S3b reproduced:
    # eight inputs, three content negatives the owner's own checks caught, and
    # `0 of 8 recorded run(s) failed. A corpus where everything passes cannot
    # demonstrate an improvement` printed underneath them.
    check_failed: tuple[str, ...] = ()

    @property
    def failure_ids(self) -> tuple[str, ...]:
        """Every recorded run that failed, either way, in recording order.

        A union rather than a sum: a run whose plan ended `failed` without
        populating `state.errors` is counted by `classify` AND can carry a
        failed check, and counting it twice would report more failures than
        there were runs.
        """
        both = set(self.failed) | set(self.check_failed)
        return tuple(sid for sid in self.recorded if sid in both)

    @property
    def lines(self) -> tuple[str, ...]:
        out: list[str] = [
            f"recorded {len(self.recorded)} scenario(s) in the {Split.TRAIN.value} split"
        ]
        for sid in self.recorded:
            if sid in self.failed:
                label = "FAILED"
            elif sid in self.check_failed:
                # Distinct from FAILED on purpose: the run did what it was
                # asked and answered wrong, which is a different thing for an
                # owner to look at than a crash.
                label = "WRONG "
            else:
                label = "passed"
            out.append(f"  {label}  {sid}")
        for sid, message in self.errored:
            out.append(f"  ERRORED (nothing recorded)  {sid}: {message}")
        total = len(self.recorded)
        if not total:
            # Never the zero-failure sentence: "everything passed" and
            # "nothing ran" are different facts, and printing the first for
            # the second is exactly the green-light-for-nothing shape this
            # repo keeps finding. The adoptee case that produced it: every
            # input raised because the migrated node builds its own vendor
            # client and there is no credential (the K1 defect).
            out.append(
                "NOTHING was recorded and the corpus is unchanged. Every input raised "
                "before producing a trace, so there is no run to pin; a scenario with an "
                "empty trace pins nothing and would pass every gate vacuously."
            )
        elif self.failure_ids:
            # ONE definition of failure, and it is the one the memory producer
            # uses. Split into its two halves on the same line, because "N
            # raised" and "N answered wrong" call for different fixes and a
            # single number hides which one an owner is looking at.
            out.append(
                f"{len(self.failure_ids)} of {total} recorded run(s) FAILED: "
                f"{len(self.failed)} raised or ended with a failed plan, "
                f"{len(self.check_failed)} failed an owner check — the task metric, "
                f"which fails without an error (ADR 0113)."
            )
            if self.failed:
                out.append(
                    "  Bootstrap labels nothing: only an owner can say a task SHOULD have "
                    "failed (ADR 0060). Consider marking one of these a tripwire — "
                    f"{', '.join(self.failed)}"
                )
            if self.check_failed:
                # A run that ANSWERED and failed an owner check is already
                # evidence (ADR 0174) and is NOT a tripwire candidate: the
                # recorder refuses `must_fail` on a task the agent completed
                # (ADR 0060's guard), so advising it would send the owner into
                # a refusal. Say what these are instead — FIRST_DAY promised a
                # line here and printed none when every failure was WRONG.
                out.append(
                    "  The owner-check failures are content negatives — already evidence "
                    "the loop can learn from. A `must_fail` label is for a task the agent "
                    "cannot complete at all; these completed and were wrong: "
                    f"{', '.join(self.check_failed)}"
                )
        else:
            out.append(
                f"0 of {total} recorded run(s) failed. A corpus where everything passes "
                f"cannot demonstrate an improvement — every gate reading it has nothing to "
                f"hold a candidate to. Add inputs whose working_memory drives the agent "
                f"into its failing cases."
            )
        out.extend(self._provider_lines())
        out.extend(self._memory_lines())
        return tuple(out)

    def _provider_lines(self) -> tuple[str, ...]:
        """What the recording spent, or what it needed and did not have.

        The cost is printed because recording is the one pass that is
        *supposed* to be live, and a live pass whose price nobody states is
        one an adopter discovers on an invoice.
        """
        if self.model_calls:
            return (
                f"  recording spent {self.model_calls} live model call(s). Recording is the "
                f"one pass that is SUPPOSED to be live: the gates replay these from each "
                f"scenario's cassette and need no credential (ADR 0123).",
            )
        if any(NO_PROVIDER_MARKER in message for _, message in self.errored):
            # The provider error already names `model_provider.impl in
            # aef.yaml`. What it cannot name is the flag that gets that file
            # to THIS command — which is how ADR 0139 reached "a model-calling
            # graph cannot get its first corpus" with the flag sitting unused
            # in the parser.
            return (
                "  This graph calls a model and no provider was configured, so the recording "
                "had nothing to record. Pass --config <aef.yaml> to bootstrap: it builds the "
                "same model provider `aef run` builds, and recording is the one pass that is "
                "SUPPOSED to be live — the cassette the gates replay from does not exist "
                "until something makes the call once (ADR 0123).",
            )
        return ()

    def _memory_lines(self) -> tuple[str, ...]:
        """What the graph's reflect node left behind, and what its silence means."""
        if self.memory_records is None or not self.recorded:
            return ()
        if self.memory_records:
            lines = [
                f"  {self.memory_records} memory record(s) written to the durable store — "
                f"what the graph's own reflect node observed, nothing bootstrap decided. "
                f"`aef loop cycle --memory <the same file>` proposes from these."
            ]
            if self.check_failed:
                lines.append(
                    f"  {len(self.check_failed)} of them is/are a check-derived FAILURE record: "
                    f"the owner's check, evaluated against what the run produced (ADR 0174). "
                    f"A signature recurring in two distinct runs becomes a lesson (ADR 0110)."
                )
            return tuple(lines)
        return (
            "  no memory records: this graph wrote none, so `aef loop cycle --memory` will "
            "say `no admissible failure memory` and propose nothing. Route a node to a "
            "`reflect` node (obligation 2) — bootstrap records what the run did and never "
            "invents a failure to fill the gap (ADR 0060).",
        )

    def tripwire_commands(
        self, module: str, corpus: str, inputs: Sequence[BootstrapInput]
    ) -> tuple[str, ...]:
        """The `aef loop record` line the owner runs to turn one failed input
        into a tripwire, with its objective and working memory filled in.

        Generated, never executed. The label is the owner's act (ADR 0060),
        and `record_run` still refuses `must_fail` on a task the agent
        completes — so this is a suggestion the recorder itself checks.
        """
        by_id = {i.id: i for i in inputs}
        commands: list[str] = []
        for sid in self.failed:
            item = by_id.get(sid)
            if item is None:  # pragma: no cover - failed ids come from inputs
                continue
            commands.append(
                f"aef loop record {module} --corpus {corpus} "
                f"--scenario-id {sid}-tripwire --objective {json.dumps(item.objective)} "
                f"--working-memory {json.dumps(json.dumps(item.working_memory))} "
                f"--split validation --expected must_fail"
            )
        return tuple(commands)


def _final_state(scenario: Scenario) -> AEFState:
    state = scenario.initial_state
    for record in scenario.trace:
        state = record.delta.apply(state)
    return state


def bootstrap(
    corpus_root: Path,
    graph: Graph,
    services_factory: Callable[[MemoryStore], Services],
    *,
    inputs: Sequence[BootstrapInput],
    now: datetime,
    agent_id: str,
    memory_sink: MemoryStore | None = None,
) -> BootstrapOutcome:
    """Run `graph` once per input and record each run as a TRAIN scenario.

    `services_factory` is called once PER INPUT and is HANDED the memory
    store to build `Services` over — it does not choose one. That is the
    point: the store is a `RunScopedMemory` this function builds, and both
    of its rules (read isolation, durable writes) are properties of
    bootstrap, not of whichever caller assembled the container. When the
    caller picked the store, the isolation rule lived at the call site and
    the durability rule could not be expressed at all.

    `memory_sink` is where the graph's reflections are ALSO written — the
    same `FileMemoryStore` `aef loop cycle --memory` reads. Bootstrap writes
    nothing to it itself; a graph that reflects on nothing leaves it empty
    (ADR 0060, ADR 0145).

    There is no `split` parameter and no `expected` parameter, deliberately:
    both are rules, and a rule with a keyword argument is a default.
    """
    # Whole-batch, before anything runs. A collision found halfway through
    # would leave scenarios written for the inputs before it, which is a
    # corpus nobody asked for and a command nobody can safely re-run.
    refuse_existing_ids(corpus_root, [i.id for i in inputs])

    recorded: list[str] = []
    failed: list[str] = []
    errored: list[tuple[str, str]] = []
    model_calls = 0
    mirrored = 0
    check_failed: list[str] = []

    for item in inputs:
        state = AEFState(
            run_id=item.id,
            agent_id=agent_id,
            objective=item.objective,
            working_memory=dict(item.working_memory),
        )
        # Fresh scratch per input, sink shared: isolation for what this run
        # READS, durability for what it WROTE. See `RunScopedMemory`.
        memory = RunScopedMemory(scratch=InMemoryMemoryStore(), sink=memory_sink)
        # Built once and reused below: the check-derived failure record runs
        # the SAME `Critic`/`Judge` this run's reflect node ran, not a second
        # pair built for the occasion (ADR 0091).
        services = services_factory(memory)
        try:
            result = record_to_corpus(
                corpus_root,
                graph,
                state,
                services,
                scenario_id=item.id,
                split=Split.TRAIN,
                recorded_at=now,
                notes=item.notes or "bootstrapped from an owner-supplied input",
                # Never a claim. Explicit rather than defaulted so a change
                # to it is a change to this line, which a test can catch.
                expected=Expected.UNSPECIFIED,
                checks=item.checks,
                budget_ms=item.budget_ms,
                # So `harvest`'s daily rate limit does not charge this
                # command's scenarios against it. A 12-input bootstrap used
                # to exhaust a limit of 5 and drop every real production
                # failure harvested in the next 24h (ADR 0141).
                source=Source.BOOTSTRAP,
            )
        except RecorderError:
            # The overwrite refusal and the empty-trace refusal are both
            # conditions the whole invocation should stop on, not per-input
            # noise to summarise.
            raise
        except Exception as exc:  # noqa: BLE001 - a node that raised is reported, not recorded
            errored.append((item.id, f"{type(exc).__name__}: {exc}"))
            # Records this run wrote before it raised still exist and are
            # still counted. Its MODEL calls are not: the recording cassette
            # lives inside `record_run` and dies with the exception, so
            # `model_calls` UNDER-reports an errored input that had already
            # paid for a call. Stated rather than rounded up — see ADR 0145.
            mirrored += len(memory.mirrored)
            continue

        recorded.append(result.scenario.id)
        model_calls += len(result.scenario.model_calls)
        final = _final_state(result.scenario)
        # A failed owner CHECK is a failure signal the reflect node cannot
        # see: it is the task metric, evaluated here, after the run (ADR
        # 0113), and `failure_signals` reads only `state.errors` and
        # `state.tool_results`. Without this an agent that ANSWERS — every
        # migrated prompt-file agent — writes `kind="success"` on the run that
        # failed the owner's check, and `aef loop cycle` says `no admissible
        # failure memory` forever (ADR 0157, ADR 0155, closed by ADR 0174).
        #
        # This is still not bootstrap authoring memory. The check is the
        # owner's, declared in the inputs file before the run; the observed
        # value is the run's; and `check_failure_record` writes nothing when
        # the checks hold or when the run raised on its own. Compare the
        # `expected` key three lines of REFUSED_KEYS above: a label saying a
        # task SHOULD have failed is a judgement bootstrap may not make, and
        # `BootstrapInput` already draws the distinction in its docstring.
        if record_check_outcomes(
            memory=memory,
            checks=item.checks,
            final_state=final,
            critic=services.require_critic(),
            judge=services.require_judge(),
            run_id=item.id,
            agent_id=agent_id,
            created_at=now,
            graph_version=graph.version,
        ):
            check_failed.append(item.id)
        mirrored += len(memory.mirrored)
        # The same `classify` the gates read and `record_run` checks
        # MUST_FAIL against, so "failed" here means what it means to G2.
        outcome = classify(final, result.scenario.trace, terminated=True)
        if not outcome.passed:
            failed.append(result.scenario.id)

    return BootstrapOutcome(
        recorded=tuple(recorded),
        failed=tuple(failed),
        errored=tuple(errored),
        model_calls=model_calls,
        memory_records=None if memory_sink is None else mirrored,
        check_failed=tuple(check_failed),
    )
