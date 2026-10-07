"""A durable `MemoryStore` for the loop.

`InMemoryMemoryStore` is real and fully tested, and it is discarded when the
process exits. That is fine for a single graph run and fatal for a loop whose
whole premise is learning across runs: `aef loop cycle` constructed one
inline, so the evidence the proposer reads was empty on every invocation and
the reflection wire (ADR 0065) could never fire from the CLI (ADR 0069).

JSONL, append-only, one record per line. Deliberately not a database: the
loop writes a handful of records per run, the file is readable by a human
investigating a halt, and a dependency would have to earn its place.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from aef.services.memory.base import MemoryKind, MemoryRecord, MemoryStore


def _at(raw: object) -> datetime | None:
    return datetime.fromisoformat(str(raw)) if raw else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def memory_payload(record: MemoryRecord) -> dict[str, Any]:
    """Lossless wire representation shared by durable memory and replay."""
    return {
        "kind": record.kind,
        "content": deepcopy(record.content),
        "run_id": record.run_id,
        "agent_id": record.agent_id,
        "tags": list(record.tags),
        "id": record.id,
        "created_at": _iso(record.created_at),
        "valid_from": _iso(record.valid_from),
        "valid_until": _iso(record.valid_until),
    }


def memory_record(payload: dict[str, Any]) -> MemoryRecord:
    return MemoryRecord(
        kind=payload["kind"],
        content=deepcopy(payload["content"]),
        run_id=payload.get("run_id"),
        agent_id=payload.get("agent_id"),
        tags=tuple(payload.get("tags", ())),
        id=payload["id"],
        created_at=_at(payload.get("created_at")),
        valid_from=_at(payload.get("valid_from")),
        valid_until=_at(payload.get("valid_until")),
    )


@dataclass(frozen=True)
class FileMemoryStore(MemoryStore):
    """Records persist across processes, which is what makes the loop learn."""

    path: Path

    def snapshot(self, *, agent_id: str) -> tuple[MemoryRecord, ...]:
        """Capture this tenant's inputs before execution, never another tenant's."""
        return tuple(r for r in self._load() if r.agent_id == agent_id)

    def _load(self) -> list[MemoryRecord]:
        if not self.path.is_file():
            return []
        out: list[MemoryRecord] = []
        for number, line in enumerate(self.path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                out.append(memory_record(payload))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                # Loud, not skipped: a memory file that silently drops records
                # gives the proposer less evidence than it thinks it has.
                raise ValueError(f"{self.path}:{number}: malformed memory record: {exc}") from exc
        return out

    def write(self, record: MemoryRecord) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = memory_payload(record)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
        return record.id

    def get(self, record_id: str) -> MemoryRecord | None:
        return next((r for r in self._load() if r.id == record_id), None)

    def query(
        self,
        kind: MemoryKind,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        tags: tuple[str, ...] = (),
        limit: int = 10,
    ) -> list[MemoryRecord]:
        return _query_records(self._load(), kind, run_id, agent_id, tags, limit)


def _query_records(
    records: list[MemoryRecord],
    kind: MemoryKind,
    run_id: str | None,
    agent_id: str | None,
    tags: tuple[str, ...],
    limit: int,
) -> list[MemoryRecord]:
    if limit < 0:
        raise ValueError("limit must be non-negative")
    matches = [
        r
        for r in records
        if r.kind == kind
        and (run_id is None or r.run_id == run_id)
        and (agent_id is None or r.agent_id == agent_id)
        and all(t in r.tags for t in tags)
    ]
    return list(reversed(matches))[:limit]  # most-recent-first


class SnapshotMemoryStore(MemoryStore):
    """Disposable replay of FileMemoryStore's append order and duplicate ids.

    InMemoryMemoryStore sorts by timestamps and overwrites duplicate ids;
    those semantics cannot reproduce an append-only file. No source path is
    retained here, so a replay cannot write back into production evidence.
    """

    def __init__(self, records: tuple[MemoryRecord, ...]) -> None:
        self._records = list(deepcopy(records))

    def write(self, record: MemoryRecord) -> str:
        self._records.append(deepcopy(record))
        return record.id

    def get(self, record_id: str) -> MemoryRecord | None:
        return deepcopy(next((r for r in self._records if r.id == record_id), None))

    def query(
        self,
        kind: MemoryKind,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        tags: tuple[str, ...] = (),
        limit: int = 10,
    ) -> list[MemoryRecord]:
        return deepcopy(_query_records(self._records, kind, run_id, agent_id, tags, limit))
