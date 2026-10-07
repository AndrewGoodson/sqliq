"""`DurabilityBackend` — checkpoint persistence (report §2.3 tier 1 / blueprint
Part 2.3). Every super-step's resulting `AEFState` is written through here.

Deviation from the blueprint's literal Part 2.3 (Temporal-wrapped, Postgres-
tiered durability): Phase 0/1 ships `InMemoryDurabilityBackend` and
`FileDurabilityBackend` only — both real and tested, both requiring zero new
infrastructure. The report's own Recommendation #1 says a durable backend
like Temporal is only needed once a run must "survive worker restarts or
exceed ~1 hour of wall-clock" — not true of anything built here yet. Postgres
and Temporal backends are declared as typed stubs so swapping them in later
is a pure adapter addition; see docs/adr/0002.

Beyond the checkpointed `AEFState` itself, a backend also tracks a small
per-run "cursor" — the id of the node that should run next, or `None` once
the run has reached `END`. Without this, "resuming" a run means calling
`GraphExecutor.run()` with the latest checkpointed state, which restarts at
`graph.entry_node` and silently re-executes every node that already ran
(duplicating any non-pure side effects) — a real gap found by testing
resume, not a hypothetical one; see docs/adr/0009. `GraphExecutor.resume()`
is what actually continues a crashed/paused run correctly, and it depends
on this cursor.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path

from aef.state import AEFState, load_state


def _atomic_write_text(path: Path, text: str) -> None:
    """Crash-consistent file write: write to a temp file in the SAME
    directory (same filesystem, so the final rename is atomic on POSIX),
    flush + fsync the data to disk, then os.replace() over the target — and
    fsync the containing directory so the rename itself is durable. A
    process crash at any point leaves either the old file fully intact or
    the new file fully intact, never a torn/truncated file. This is the
    write-side counterpart to ADR 0026's read-side hardening; see ADR 0031.

    Plain `path.write_text()` (the previous implementation) is NOT atomic:
    a crash mid-write leaves a truncated file, which for the newest
    checkpoint bricked `load_latest`, and for the in-place `cursor.json`
    rewrite corrupted the resume pointer itself."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        pending = memoryview(text.encode("utf-8"))
        while pending:
            written = os.write(fd, pending)
            if written <= 0:
                raise OSError(f"atomic write to {path} made no progress")
            pending = pending[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    # Durably record the rename in the directory entry, so a crash right
    # after replace() can't lose the just-renamed file.
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class CorruptedCheckpointError(RuntimeError):
    """Checkpoint data cannot establish a consistent resume state.

    This includes malformed JSON, an inconsistent identity or cursor, and
    an attempted replacement of an immutable checkpoint (ADRs 0026/0209).
    """


def _validate_checkpoint_retry(run_id: str, checkpoint_seq: int, *, identical: bool) -> None:
    if not identical:
        raise CorruptedCheckpointError(
            f"checkpoint run_id={run_id!r}, checkpoint_seq={checkpoint_seq} is immutable; "
            "cannot replace its state while an existing cursor may still refer to it"
        )


def _validate_cursor_checkpoint(
    run_id: str, checkpoint_seq: object, latest_seq: int | None
) -> None:
    if checkpoint_seq is not None and (type(checkpoint_seq) is not int or checkpoint_seq < 0):
        raise CorruptedCheckpointError(
            f"cursor for run_id={run_id!r} has an invalid checkpoint_seq; "
            "expected a non-negative integer or null"
        )
    if checkpoint_seq != latest_seq:
        raise CorruptedCheckpointError(
            f"cursor for run_id={run_id!r} is bound to checkpoint seq {checkpoint_seq!r}, "
            f"but the latest checkpoint is seq {latest_seq!r}. "
            "Cannot safely resume an incomplete or legacy checkpoint/cursor pair; "
            "inspect the recorded state and repair the cursor before resuming."
        )


class DurabilityBackend(ABC):
    @abstractmethod
    def save_checkpoint(self, state: AEFState) -> None:
        """Persist an immutable run/sequence identity. Identical retries may
        succeed; changed state at an existing identity must raise before
        replacing it. Cursor consistency depends on this guarantee (ADR 0209)."""
        raise NotImplementedError

    @abstractmethod
    def load_latest(self, run_id: str) -> AEFState | None:
        raise NotImplementedError

    @abstractmethod
    def load_checkpoint(self, run_id: str, checkpoint_seq: int) -> AEFState | None:
        raise NotImplementedError

    @abstractmethod
    def list_checkpoints(self, run_id: str) -> list[int]:
        raise NotImplementedError

    @abstractmethod
    def save_cursor(self, run_id: str, next_node: str | None) -> None:
        """Record which node should run next for `run_id`, or `None` if the
        run has reached `END`. Called after every super-step, alongside
        `save_checkpoint`. Implementations must bind the cursor to that
        checkpoint and reject an inconsistent pair when loading (ADR 0209)."""
        raise NotImplementedError

    @abstractmethod
    def load_cursor(self, run_id: str) -> str | None:
        """The node id to resume at, or `None` if the run already completed.
        A missing, unbound or stale cursor when checkpoints exist must raise
        `CorruptedCheckpointError`, never imply completion. Callers distinguish
        "never started" from "completed" via
        `load_latest`/`list_checkpoints` returning nothing at all — this
        method alone cannot tell those two cases apart."""
        raise NotImplementedError


class InMemoryDurabilityBackend(DurabilityBackend):
    """Round-trips every checkpoint through JSON (not just object references)
    so the migration path (`load_state`) is genuinely exercised on every
    read, exactly as a real out-of-process backend would."""

    def __init__(self) -> None:
        self._store: dict[str, dict[int, str]] = {}
        self._cursors: dict[str, tuple[str | None, int | None]] = {}
        self._latest_written: dict[str, int] = {}

    def save_checkpoint(self, state: AEFState) -> None:
        run = self._store.setdefault(state.run_id, {})
        payload = state.model_dump_json()
        existing = run.get(state.checkpoint_seq)
        if existing is not None:
            _validate_checkpoint_retry(
                state.run_id, state.checkpoint_seq, identical=existing == payload
            )
        else:
            run[state.checkpoint_seq] = payload
        self._latest_written[state.run_id] = state.checkpoint_seq

    def load_latest(self, run_id: str) -> AEFState | None:
        run = self._store.get(run_id)
        if not run:
            return None
        latest_seq = max(run)
        return load_state(json.loads(run[latest_seq]))

    def load_checkpoint(self, run_id: str, checkpoint_seq: int) -> AEFState | None:
        run = self._store.get(run_id)
        if not run or checkpoint_seq not in run:
            return None
        return load_state(json.loads(run[checkpoint_seq]))

    def list_checkpoints(self, run_id: str) -> list[int]:
        return sorted(self._store.get(run_id, {}))

    def save_cursor(self, run_id: str, next_node: str | None) -> None:
        self._cursors[run_id] = (next_node, self._latest_written.get(run_id))

    def load_cursor(self, run_id: str) -> str | None:
        next_node, checkpoint_seq = self._cursors.get(run_id, (None, None))
        _validate_cursor_checkpoint(
            run_id, checkpoint_seq, max(self._store.get(run_id, {}), default=None)
        )
        return next_node


class FileDurabilityBackend(DurabilityBackend):
    """One JSON file per (run_id, checkpoint_seq) under `root_dir`, plus a
    `cursor.json` sidecar per run. Survives process restart without any
    external service — the honest stand-in for a "Postgres checkpointer
    suffices" deployment (report Recommendation #1)."""

    def __init__(self, root_dir: Path) -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._latest_written: dict[str, int] = {}

    def _run_dir(self, run_id: str, *, create: bool = True) -> Path:
        # `run_id` was joined onto the root verbatim, so `../../escaped`
        # wrote outside the backend entirely. Containment cannot rest on
        # callers passing well-formed ids — this is the boundary, so the
        # check belongs here as well as in the schema (ADR 0086).
        if not run_id or run_id in (".", "..") or "/" in run_id or "\\" in run_id:
            raise ValueError(
                f"run_id {run_id!r} resolves outside the checkpoint root or is not a "
                f"single segment; a run id is a directory name, not a path"
            )
        run_dir = (self._root / run_id).resolve()
        root = self._root.resolve()
        if run_dir != root and root not in run_dir.parents:
            raise ValueError(
                f"run_id {run_id!r} resolves outside the checkpoint root {root}; "
                f"a run id is a directory name, not a path"
            )
        if create:
            run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def save_checkpoint(self, state: AEFState) -> None:
        path = self._run_dir(state.run_id, create=True) / f"{state.checkpoint_seq}.json"
        # Refuse changed or corrupt existing identities before any write.
        # Rewriting them could leave a previously committed cursor pointing
        # at different state even though its sequence binding still matches.
        existing = self.load_checkpoint(state.run_id, state.checkpoint_seq)
        payload = state.model_dump_json()
        if existing is not None:
            _validate_checkpoint_retry(
                state.run_id,
                state.checkpoint_seq,
                identical=existing.model_dump_json() == payload,
            )
        else:
            _atomic_write_text(path, payload)
        self._latest_written[state.run_id] = state.checkpoint_seq

    def load_latest(self, run_id: str) -> AEFState | None:
        # Walk newest-first, falling back past any corrupt checkpoint to the
        # highest *loadable* one (ADR 0031). Atomic writes (see
        # _atomic_write_text) mean a torn file should never exist in the
        # first place — but a file corrupted by something outside this
        # backend (disk fault, manual edit, a pre-atomic-write legacy crash)
        # must not brick resume when an earlier good checkpoint is right
        # there. If EVERY checkpoint is corrupt, the last error propagates —
        # returning None would be indistinguishable from "never ran".
        checkpoints = self.list_checkpoints(run_id)
        last_error: CorruptedCheckpointError | None = None
        for seq in sorted(checkpoints, reverse=True):
            try:
                return self.load_checkpoint(run_id, seq)
            except CorruptedCheckpointError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        return None

    def load_checkpoint(self, run_id: str, checkpoint_seq: int) -> AEFState | None:
        path = self._run_dir(run_id, create=False) / f"{checkpoint_seq}.json"
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise CorruptedCheckpointError(
                f"checkpoint {path} (run_id={run_id!r}, checkpoint_seq={checkpoint_seq}) "
                f"is not valid JSON: {exc}"
            ) from exc
        state = load_state(raw)
        if state.run_id != run_id or state.checkpoint_seq != checkpoint_seq:
            raise CorruptedCheckpointError(
                f"checkpoint {path} identity does not match the requested "
                f"run_id={run_id!r}, checkpoint_seq={checkpoint_seq}; payload contains "
                f"run_id={state.run_id!r}, checkpoint_seq={state.checkpoint_seq}"
            )
        return state

    def list_checkpoints(self, run_id: str) -> list[int]:
        run_dir = self._run_dir(run_id, create=False)
        if not run_dir.exists():
            return []
        # Only `<int>.json` files are checkpoints — `cursor.json` and any
        # other non-checkpoint `.json` file sharing this directory (a stray
        # backup, a future sidecar file) must not be mistaken for one.
        # Reproduced directly: a single non-numeric-stem `.json` file here
        # used to raise ValueError from `int(p.stem)` and take down
        # list_checkpoints — and therefore load_latest — for the entire
        # run_id, not just the offending file.
        return sorted(int(p.stem) for p in run_dir.glob("*.json") if p.stem.isdigit())

    def save_cursor(self, run_id: str, next_node: str | None) -> None:
        # Highest-priority atomic write: cursor.json is overwritten IN PLACE
        # every super-step, so a torn write here corrupts the resume pointer
        # itself (not just one checkpoint). See _atomic_write_text / ADR 0031.
        cursor_path = self._run_dir(run_id, create=True) / "cursor.json"
        # The executor just saved this checkpoint. Remember its sequence so
        # recording N steps does not enumerate N growing histories. A fresh
        # instance supporting a manual cursor repair reads the history once.
        checkpoint_seq = self._latest_written.get(run_id)
        if checkpoint_seq is None:
            checkpoint_seq = max(self.list_checkpoints(run_id), default=None)
        _atomic_write_text(
            cursor_path, json.dumps({"next_node": next_node, "checkpoint_seq": checkpoint_seq})
        )

    def load_cursor(self, run_id: str) -> str | None:
        cursor_path = self._run_dir(run_id, create=False) / "cursor.json"
        if not cursor_path.exists():
            _validate_cursor_checkpoint(
                run_id, None, max(self.list_checkpoints(run_id), default=None)
            )
            return None
        # Same read-side corruption guard as load_checkpoint (ADR 0026):
        # atomic writes (ADR 0031) stop this backend producing a torn cursor,
        # but external corruption (disk fault, manual edit, a pre-atomic-write
        # legacy crash) must surface as a diagnosable error naming the run,
        # not a bare JSONDecodeError that bricks resume() opaquely.
        try:
            data = json.loads(cursor_path.read_text())
        except json.JSONDecodeError as exc:
            raise CorruptedCheckpointError(
                f"cursor {cursor_path} (run_id={run_id!r}) is not valid JSON: {exc}"
            ) from exc
        if not isinstance(data, dict) or "next_node" not in data:
            raise CorruptedCheckpointError(
                f"cursor {cursor_path} (run_id={run_id!r}) must be a JSON object "
                "containing next_node"
            )
        next_node = data.get("next_node")
        if next_node is not None and not isinstance(next_node, str):
            raise CorruptedCheckpointError(
                f"cursor {cursor_path} (run_id={run_id!r}) has invalid next_node; "
                "expected a string or null"
            )
        _validate_cursor_checkpoint(
            run_id, data.get("checkpoint_seq"), max(self.list_checkpoints(run_id), default=None)
        )
        if isinstance(next_node, str):
            return next_node
        return None


class PostgresDurabilityBackend(DurabilityBackend):
    """Phase 2 stub. Would back onto the same PostgreSQL instance already
    used for checkpoints in production LangGraph deployments (report §2.3
    tier 1). No `psycopg`/`psycopg2` import here — nothing is implemented
    yet, so nothing vendor-specific needs importing."""

    def __init__(self, dsn: str) -> None:
        raise NotImplementedError(
            "PostgresDurabilityBackend is a Phase 2 stub; see docs/roadmap.md"
        )

    def save_checkpoint(self, state: AEFState) -> None:
        raise NotImplementedError

    def load_latest(self, run_id: str) -> AEFState | None:
        raise NotImplementedError

    def load_checkpoint(self, run_id: str, checkpoint_seq: int) -> AEFState | None:
        raise NotImplementedError

    def list_checkpoints(self, run_id: str) -> list[int]:
        raise NotImplementedError

    def save_cursor(self, run_id: str, next_node: str | None) -> None:
        raise NotImplementedError

    def load_cursor(self, run_id: str) -> str | None:
        raise NotImplementedError


class TemporalDurabilityBackend(DurabilityBackend):
    """Phase 2+ stub for the "tier 2" durability guarantee (blueprint §2.3):
    wraps node execution as Temporal Activities so a dead worker's run can be
    resumed by replaying Workflow event history on another worker. Gated
    behind the report's own change trigger — only worth adding once a run
    must survive worker restarts or exceed ~1hr wall-clock (Recommendation #1).
    """

    def __init__(self, *, task_queue: str) -> None:
        raise NotImplementedError(
            "TemporalDurabilityBackend is a Phase 2+ stub; see docs/roadmap.md"
        )

    def save_checkpoint(self, state: AEFState) -> None:
        raise NotImplementedError

    def load_latest(self, run_id: str) -> AEFState | None:
        raise NotImplementedError

    def load_checkpoint(self, run_id: str, checkpoint_seq: int) -> AEFState | None:
        raise NotImplementedError

    def list_checkpoints(self, run_id: str) -> list[int]:
        raise NotImplementedError

    def save_cursor(self, run_id: str, next_node: str | None) -> None:
        raise NotImplementedError

    def load_cursor(self, run_id: str) -> str | None:
        raise NotImplementedError
