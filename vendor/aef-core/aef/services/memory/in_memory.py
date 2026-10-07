"""Real, dependency-free `MemoryStore` backend — the Phase 0/1 default and
what `examples/` runs against."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from threading import RLock

from aef.services.memory.base import MemoryKind, MemoryRecord, MemoryStore


class InMemoryMemoryStore(MemoryStore):
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._records: dict[str, MemoryRecord] = {}
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._lock = RLock()

    def write(self, record: MemoryRecord) -> str:
        with self._lock:
            created_at = record.created_at if record.created_at is not None else self._clock()
            stored = dataclasses.replace(
                record,
                content=deepcopy(record.content),
                created_at=created_at,
            )
            self._records[record.id] = stored
        return record.id

    def query(
        self,
        kind: MemoryKind,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        tags: tuple[str, ...] = (),
        limit: int = 10,
    ) -> list[MemoryRecord]:
        if limit < 0:
            raise ValueError(f"limit must be non-negative; got {limit}")
        with self._lock:
            wanted_tags = set(tags)
            matches = [
                r
                for r in self._records.values()
                if r.kind == kind
                and (run_id is None or r.run_id == run_id)
                and (agent_id is None or r.agent_id == agent_id)
                and wanted_tags.issubset(r.tags)
            ]
            matches.sort(
                key=lambda r: r.created_at or datetime.min.replace(tzinfo=UTC), reverse=True
            )
            return [_snapshot(record) for record in matches[:limit]]

    def get(self, record_id: str) -> MemoryRecord | None:
        with self._lock:
            record = self._records.get(record_id)
            return None if record is None else _snapshot(record)


def _snapshot(record: MemoryRecord) -> MemoryRecord:
    return dataclasses.replace(record, content=deepcopy(record.content))
