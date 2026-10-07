"""The golden corpus — recorded runs, split three ways, that never shrinks.

Three properties this module exists to guarantee.

**Splits are decided at record time and are immutable.** `train` is the only
split a proposer may cite (`grounded_in`); `validation` is what gates score
against; `holdout` is the owner's, rotated, and never shown to the proposer.
If a scenario could move between splits, holdout leaks into train and G3's
"beats the control cohort on held-out data" becomes a measurement of
memorisation. So the split is stored *inside* the scenario, and loading
verifies it against the directory it was found in — a scenario moved on disk
fails loudly rather than quietly joining a different split.

**The corpus never shrinks.** A gate suite that can be made to pass by
deleting the scenario that fails is not a gate suite. `CorpusManifest`
records every id ever admitted; `check_never_shrinks` fails if one goes
missing.

That paragraph used to end "This is checked in CI, from the base ref, so a
candidate cannot retire its own counterexample", and **none of it was true**:
`check_never_shrinks` had no caller outside its own tests, `save_manifest` had
none at all, and no CI job read either. Reproduced — delete the two scenarios
the agent fails, `aef loop score` rises 0.6667 to 1.0000, exit 0, nothing
complains — while `recorder.refuse_existing_ids` justified its own rule by
citing this guard (ADR 0141). Now: `save_scenario` records the id as it admits
it, and `harness/loop.py`'s `_preflight` calls `check_never_shrinks` on every
`cycle`, `gate` and `run`, beside the archive check that was already there.
The baseline is read from the base ref when the corpus is tracked in the repo
and from the working tree otherwise — the second is weaker (a candidate that
deletes the scenario *and* its manifest entry in one commit passes it) and is
the fallback, not the design.

**Re-execution is deterministic.** A scenario pins the clock values its
original run observed (`fixed_clock`). Without that, re-executing produces
different `Context.now` values and every trace comparison diverges for
reasons that have nothing to do with the candidate.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from aef.config.schema import ContextConfig
from aef.harness.checks import CheckError, TaskCheck
from aef.harness.memory_store import memory_payload, memory_record
from aef.harness.trace_codec import (
    TRACE_FORMAT_VERSION,
    TraceCodecError,
    decode_trace,
    dumps,
    encode_trace,
    loads,
)
from aef.kernel.executor import NodeExecutionRecord
from aef.providers.cassette_provider import RecordedCall
from aef.services.memory.base import MemoryRecord
from aef.state import AEFState

MANIFEST_FILENAME = "manifest.json"


class Expected(StrEnum):
    """What the OWNER says should happen — not what the agent did.

    The distinction is the whole point (ADR 0060). A recorded trace says the
    agent failed; it does not say failing was correct. Without that
    statement, a candidate that flips a failing scenario to "passing" is
    indistinguishable from one that learned to do the task — and a candidate
    that simply reports success passes every gate.

    `MUST_FAIL` is a tripwire: a task genuinely beyond the agent's remit,
    where claiming success is a lie rather than an improvement.
    """

    UNSPECIFIED = "unspecified"  # legacy/unlabelled; carries no claim
    MUST_PASS = "must_pass"
    MUST_FAIL = "must_fail"


class Split(StrEnum):
    TRAIN = "train"  # the only split a proposer may cite
    VALIDATION = "validation"  # what the gates score against
    HOLDOUT = "holdout"  # the owner's; never shown to the proposer


class Source(StrEnum):
    """Which command admitted this scenario.

    Added because `harvest`'s daily rate limit counted **every** scenario
    recorded in the last 24 hours, and `bootstrap` stamps `recorded_at = now`
    on every one it writes. So the K5 pilot sequence — adopt, bootstrap,
    run for real, harvest — silently dropped every real production failure:
    a 12-input bootstrap exhausted a limit of 5 and `harvest` reported
    `promoted 0, 3 held back by the daily rate limit`, exit 0 (reproduced,
    ADR 0141).

    The limit exists so that one bad deploy cannot fill the corpus with a
    single incident. That is a statement about *harvest's own* promotions, so
    only `HARVEST` counts against it.

    `UNSPECIFIED` is what every scenario written before this field existed
    loads as, and it carries no claim — exactly like `Expected.UNSPECIFIED`
    one enum up.
    """

    UNSPECIFIED = "unspecified"  # legacy; recorded before provenance existed
    BOOTSTRAP = "bootstrap"  # aef loop bootstrap
    HARVEST = "harvest"  # aef loop harvest, from a real run
    RECORD = "record"  # aef loop record, one deliberate owner act


class CorpusError(RuntimeError):
    pass


class CorpusShrankError(CorpusError):
    """A scenario that was previously admitted is gone. Deliberately its own
    exception type: this is not a normal validation failure, it is the one
    failure mode that would let a candidate erase its own counterexample."""


@dataclass(frozen=True)
class Scenario:
    id: str
    split: Split
    graph_id: str
    graph_version: str
    initial_state: AEFState
    trace: tuple[NodeExecutionRecord, ...]
    recorded_at: datetime
    notes: str = ""
    # Owner-supplied ground truth. Defaults to UNSPECIFIED so every existing
    # scenario keeps its exact meaning: "this is what happened", no claim
    # about what should have.
    expected: Expected = Expected.UNSPECIFIED
    # Owner-declared task checks (ADR 0113): the part of the score that can
    # fail without an error. Data, never code — see aef/harness/checks.py.
    checks: tuple[TaskCheck, ...] = ()
    # Wall-clock budget judged on the RUNNER's stopwatch, not the pinned
    # clock: under `fixed_clock` the recorded latency replays verbatim and
    # says nothing about the candidate.
    budget_ms: float | None = None
    # Every model completion the recording made, keyed by request (ADR 0123).
    # Re-execution serves these from a `CassetteProvider` so a graph that
    # calls a model replays deterministically and the gate needs no
    # credential. Empty for a legacy scenario and for any graph that never
    # asked a model anything — the pinned clock's rule, one layer up.
    model_calls: tuple[RecordedCall, ...] = ()
    # Which command admitted it (ADR 0141). Not a claim about the run — a fact
    # about how it got here, read only by harvest's rate limit.
    source: Source = Source.UNSPECIFIED
    initial_memory: tuple[MemoryRecord, ...] | None = None
    provider_isolation: tuple[str, ...] = ()
    provider_name: str = ""
    # Approved retrieval settings, not arbitrary services or live permissions.
    # None preserves pre-ADR-0210 recordings' default retrieval behavior.
    context_config: ContextConfig | None = None

    @property
    def clock_values(self) -> tuple[datetime, ...]:
        """Every `Context.now` the original run observed, in order."""
        return tuple(record.context.now for record in self.trace)

    def to_payload(self) -> dict[str, Any]:
        return {
            "format_version": TRACE_FORMAT_VERSION,
            "id": self.id,
            "split": self.split.value,
            "graph_id": self.graph_id,
            "graph_version": self.graph_version,
            "initial_state": self.initial_state.model_dump(mode="json"),
            "trace": encode_trace(self.trace),
            "recorded_at": self.recorded_at.isoformat(),
            "notes": self.notes,
            "expected": self.expected.value,
            "checks": [check.to_payload() for check in self.checks],
            "budget_ms": self.budget_ms,
            "model_calls": [call.to_payload() for call in self.model_calls],
            "source": self.source.value,
            "initial_memory": None
            if self.initial_memory is None
            else [memory_payload(r) for r in self.initial_memory],
            "provider_isolation": list(self.provider_isolation),
            "provider_name": self.provider_name,
            "context_config": None
            if self.context_config is None
            else self.context_config.model_dump(mode="json"),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Scenario:
        try:
            return cls(
                id=payload["id"],
                split=Split(payload["split"]),
                graph_id=payload["graph_id"],
                graph_version=payload["graph_version"],
                initial_state=AEFState.model_validate(payload["initial_state"]),
                trace=decode_trace(payload["trace"]),
                recorded_at=datetime.fromisoformat(payload["recorded_at"]),
                notes=payload.get("notes", ""),
                expected=Expected(payload.get("expected", Expected.UNSPECIFIED.value)),
                checks=tuple(TaskCheck.from_payload(c) for c in payload.get("checks", ())),
                budget_ms=(
                    None if payload.get("budget_ms") is None else float(payload["budget_ms"])
                ),
                # Absent in every scenario recorded before ADR 0123: loads as
                # none, which is exactly what those recordings made.
                model_calls=tuple(
                    RecordedCall.from_payload(c) for c in payload.get("model_calls", ())
                ),
                # Absent in every scenario recorded before ADR 0141, which is
                # exactly what UNSPECIFIED means: nobody recorded who wrote it.
                source=Source(payload.get("source", Source.UNSPECIFIED.value)),
                initial_memory=None
                if payload.get("initial_memory") is None
                else tuple(memory_record(r) for r in payload["initial_memory"]),
                provider_isolation=tuple(
                    sorted(str(v) for v in payload.get("provider_isolation", ()))
                ),
                provider_name=str(payload.get("provider_name", "")),
                context_config=None
                if payload.get("context_config") is None
                else ContextConfig.model_validate(payload["context_config"]),
            )
        except CheckError as exc:
            # `CheckError` is a `ValueError`, so it would otherwise be reported
            # as "malformed scenario payload" — which is the wrong lead for a
            # pattern that parses perfectly and is refused because running it
            # would not terminate (ADR 0166). The check's own message carries
            # the rewrite; this only says the check is why the file is refused.
            raise CorpusError(f"unusable check: {exc}") from exc
        except (KeyError, ValueError) as exc:
            raise CorpusError(f"malformed scenario payload: {exc}") from exc


def fixed_clock(scenario: Scenario) -> Callable[[], datetime]:
    """A `Services.clock` replaying the scenario's recorded timestamps.

    Pinning the clock is what makes re-execution comparable at all. Running
    past the recorded sequence raises rather than inventing a value: a
    candidate that takes MORE steps than the incumbent is a real behavioural
    difference, and silently handing it a fresh `now` would hide it behind a
    timestamp mismatch instead of reporting it.
    """
    remaining = list(scenario.clock_values)
    consumed = 0

    def clock() -> datetime:
        nonlocal consumed
        if not remaining:
            raise CorpusError(
                f"scenario {scenario.id!r} pinned {consumed} clock value(s) but the run asked "
                f"for another — the candidate executed more steps than the recording, which "
                f"is a behavioural difference to report, not a timestamp to invent"
            )
        consumed += 1
        return remaining.pop(0)

    return clock


@dataclass(frozen=True)
class CorpusManifest:
    """Every scenario id ever admitted, with its split. The never-shrinks
    ledger; lives in Zone B and is read from the base ref."""

    ids: dict[str, Split] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {"scenarios": {sid: split.value for sid, split in sorted(self.ids.items())}}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CorpusManifest:
        raw = payload.get("scenarios", {})
        if not isinstance(raw, dict):
            raise CorpusError("manifest 'scenarios' must be an object")
        return cls(ids={sid: Split(value) for sid, value in raw.items()})


@dataclass(frozen=True)
class Corpus:
    root: Path
    scenarios: tuple[Scenario, ...] = ()

    def split(self, split: Split) -> tuple[Scenario, ...]:
        return tuple(s for s in self.scenarios if s.split is split)

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(s.id for s in self.scenarios)

    def manifest(self) -> CorpusManifest:
        return CorpusManifest(ids={s.id: s.split for s in self.scenarios})


def scenario_path(root: Path, scenario: Scenario) -> Path:
    return root / scenario.split.value / f"{scenario.id}.json"


def save_scenario(root: Path, scenario: Scenario) -> Path:
    path = scenario_path(root, scenario)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(scenario.to_payload()))
    _record_in_manifest(root, scenario)
    return path


def _record_in_manifest(root: Path, scenario: Scenario) -> None:
    """Add this id to the never-shrinks ledger, as it is admitted.

    `CorpusManifest` and `check_never_shrinks` existed from the start and
    **nothing ever wrote a manifest** — so the ledger was empty everywhere and
    the check, wherever it ran, passed vacuously. Writing it here rather than
    from a separate command is the point: the manifest has to be updated by
    the same act that admits the scenario, or the two describe different
    corpora (ADR 0141).

    A union, never a rewrite. Regenerating the manifest from the corpus on
    disk would forget precisely the scenario that had just been deleted, which
    is the deletion this ledger exists to notice.
    """
    manifest = load_manifest(root)
    if manifest.ids.get(scenario.id) is scenario.split:
        return
    save_manifest(root, CorpusManifest(ids={**manifest.ids, scenario.id: scenario.split}))


def load_scenario(path: Path) -> Scenario:
    try:
        scenario = Scenario.from_payload(loads(path.read_text()))
    except TraceCodecError as exc:
        raise CorpusError(f"{path}: {exc}") from exc
    except CorpusShrankError:  # pragma: no cover - not raised by from_payload
        raise
    except CorpusError as exc:
        # `from_payload` names the missing key and nothing else, so an adopter
        # with forty scenarios read `malformed scenario payload: 'graph_id'`
        # and had no way to tell which file. Every other refusal in this
        # module leads with the path; this one now does too (ADR 0141).
        raise CorpusError(f"{path}: {exc}") from exc

    # The directory is a claim; the file is the record. A scenario moved
    # from holdout/ into train/ would otherwise silently become citable by
    # the proposer, which is precisely the leak the split exists to prevent.
    claimed = path.parent.name
    if claimed != scenario.split.value:
        raise CorpusError(
            f"{path}: scenario {scenario.id!r} declares split {scenario.split.value!r} but sits "
            f"in {claimed!r}/. A scenario's split is fixed at record time — moving files "
            f"between splits leaks the holdout into the proposer's evidence base."
        )
    if path.stem != scenario.id:
        raise CorpusError(
            f"{path}: filename {path.stem!r} does not match scenario id {scenario.id!r}"
        )
    return scenario


def load_corpus(root: Path) -> Corpus:
    scenarios: list[Scenario] = []
    for split in Split:
        directory = root / split.value
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            scenarios.append(load_scenario(path))

    duplicates = _duplicated(s.id for s in scenarios)
    if duplicates:
        raise CorpusError(f"duplicate scenario id(s) across splits: {sorted(duplicates)}")
    return Corpus(root=root, scenarios=tuple(scenarios))


def _duplicated(ids: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for value in ids:
        (dupes if value in seen else seen).add(value)
    return dupes


def save_manifest(root: Path, manifest: CorpusManifest) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / MANIFEST_FILENAME
    path.write_text(dumps(manifest.to_payload()))
    return path


def load_manifest(root: Path) -> CorpusManifest:
    path = root / MANIFEST_FILENAME
    if not path.is_file():
        return CorpusManifest()
    return CorpusManifest.from_payload(loads(path.read_text()))


LOOP_BRANCH_PREFIX = "loop/"
"""Branches this loop creates. A baseline read from one of these is a
baseline the loop itself left behind (ADR 0204's F-Q1-6)."""


def reconcile_command(root: Path) -> str:
    """The command an OWNER runs to make the manifest describe the files on
    disk. Named by every refusal that a stale manifest can cause, because a
    control whose only remedy is hand-editing JSON is a control adopters route
    around (ADR 0176)."""
    return f"aef loop corpus reconcile --corpus {root}"


def _split_move_remedy(corpus: Corpus, baseline_ref: str) -> str:
    """The remedy that actually fixes the condition named, which depends on
    WHERE the baseline came from.

    `reconcile` rewrites the manifest ON DISK from the files on disk. That is
    the right answer when the baseline is the working tree's own manifest. It
    is a no-op when the baseline was read from a ref, because the stale
    manifest is committed on that ref and `reconcile` cannot reach it — and
    the loop's own kept branch is exactly such a ref, left behind by an
    earlier turn while the default branch moved on.

    Found on the marlin install (ADR 0204's F-Q1-6): the refusal named
    `reconcile`, `reconcile` answered "nothing to reconcile", and the refusal
    then repeated itself unchanged. The check is correct and stays; what was
    wrong was telling an owner to run a command that cannot help.
    """
    if baseline_ref and baseline_ref.startswith(LOOP_BRANCH_PREFIX):
        return (
            f"The baseline came from `{baseline_ref}`, a branch THIS LOOP created and has "
            f"not advanced, so it still carries the manifest from before the move — "
            f"`{reconcile_command(corpus.root)}` rewrites the manifest on disk and cannot "
            f"reach a manifest committed on another ref, so it will answer 'nothing to "
            f"reconcile' and this refusal will repeat. If the move was deliberate, delete "
            f"the stale branch (`git branch -D {baseline_ref}`) or advance it to the ref "
            f"you moved on; the next turn then reads the manifest you meant."
        )
    if baseline_ref:
        return (
            f"The baseline came from `{baseline_ref}`. If the move was deliberate, make it "
            f"on that ref too — `{reconcile_command(corpus.root)}` rewrites the manifest on "
            f"disk and cannot reach one committed elsewhere."
        )
    return f"If the move was deliberate, run `{reconcile_command(corpus.root)}` by hand."


def check_never_shrinks(
    corpus: Corpus, baseline: CorpusManifest, *, baseline_ref: str = ""
) -> None:
    """Raise unless `corpus` still contains every id `baseline` recorded.

    Growth is fine and expected; a scenario changing split is not, because
    that is deletion from one split dressed as an addition to another.

    The refusal now NAMES the reconcile command. It is not a softening: the
    check fires exactly as often as before, on exactly the same condition, and
    the command it names is one an owner types. Reproduced (ADR 0176): copy a
    corpus directory, delete one scenario file, and the next `aef loop cycle`
    exits 3 with `corpus shrank` — correct, and with no documented way back
    except editing `manifest.json` by hand.
    """
    missing = sorted(set(baseline.ids) - corpus.ids)
    if missing:
        raise CorpusShrankError(
            f"corpus shrank: {len(missing)} previously-admitted scenario(s) are gone: "
            f"{missing}. A suite that can be made to pass by deleting the failing case is "
            f"not a suite. If those scenarios were retired deliberately — or this corpus "
            f"was copied and its manifest came with it — run `{reconcile_command(corpus.root)}` "
            f"BY HAND: it rewrites the manifest from the files on disk and prints every id "
            f"it drops. No loop subcommand does this for you; a loop that can rewrite its "
            f"own evidence ledger has no ledger."
        )
    current = corpus.manifest().ids
    moved = sorted(
        f"{sid}: {baseline.ids[sid].value} -> {current[sid].value}"
        for sid in baseline.ids
        if current[sid] is not baseline.ids[sid]
    )
    if moved:
        raise CorpusShrankError(
            f"scenario(s) changed split, which erases evidence: {moved}. "
            + _split_move_remedy(corpus, baseline_ref)
        )


@dataclass(frozen=True)
class ReconcileReport:
    """What `reconcile_manifest` changed, so the CLI can print it and a test
    can assert it without parsing prose."""

    root: Path
    dropped: dict[str, Split] = field(default_factory=dict)
    moved: dict[str, tuple[Split, Split]] = field(default_factory=dict)
    added: dict[str, Split] = field(default_factory=dict)
    kept: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.dropped or self.moved or self.added)


def reconcile_manifest(root: Path, *, write: bool = True) -> ReconcileReport:
    """Rewrite `manifest.json` from the scenarios actually on disk.

    **This is an owner action and has exactly one caller: the CLI subcommand
    `aef loop corpus reconcile`.** `tests/harness/test_corpus_reconcile.py`
    AST-scans `aef/` and fails if anything else calls it — because the
    never-shrinks manifest is the audit trail the gates are measured against,
    and a loop that reconciles its own ledger between turns can delete the
    scenario it fails and call the result an improvement. That is ADR 0060's
    shape: the harness prints the command, a person runs it.

    `_record_in_manifest` deliberately does a UNION and never a rewrite, for
    the same reason. This function is the rewrite, and it is why it is not
    reachable from `cycle` or `run`.
    """
    corpus = load_corpus(root)
    baseline = load_manifest(root)
    current = corpus.manifest().ids
    report = ReconcileReport(
        root=root,
        dropped={sid: split for sid, split in sorted(baseline.ids.items()) if sid not in current},
        moved={
            sid: (baseline.ids[sid], current[sid])
            for sid in sorted(baseline.ids)
            if sid in current and current[sid] is not baseline.ids[sid]
        },
        added={sid: split for sid, split in sorted(current.items()) if sid not in baseline.ids},
        kept=len(current),
    )
    if write and report.changed:
        save_manifest(root, corpus.manifest())
    return report
