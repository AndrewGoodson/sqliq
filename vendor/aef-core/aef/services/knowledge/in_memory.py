"""Real, dependency-free `KnowledgeStore` backend — the default, and what the
consolidator (I2) and the A/B eval (I4) run against."""

from __future__ import annotations

import dataclasses
from copy import deepcopy
from datetime import UTC, datetime
from threading import RLock

from aef.services.knowledge.base import KnowledgeEntry, KnowledgeKind, KnowledgeStore

# Sort floor for an entry with no `last_seen`. Aware, because comparing a
# naive datetime against an aware one raises, and a store that raises while
# ordering would turn a missing timestamp into an unreadable store.
_EPOCH = datetime.min.replace(tzinfo=UTC)


class InMemoryKnowledgeStore(KnowledgeStore):
    def __init__(self) -> None:
        self._entries: dict[tuple[str | None, str], KnowledgeEntry] = {}
        self._lock = RLock()

    def upsert(self, entry: KnowledgeEntry) -> KnowledgeEntry:
        with self._lock:
            existing = self._entries.get(entry.key)
            merged = entry if existing is None else _merge(existing, entry)
            stored = dataclasses.replace(merged, content=deepcopy(merged.content))
            self._entries[entry.key] = stored
        return _snapshot(stored)

    def query(
        self,
        kind: KnowledgeKind,
        *,
        agent_id: str | None = None,
        min_occurrences: int = 1,
        limit: int = 10,
    ) -> list[KnowledgeEntry]:
        if limit < 0:
            raise ValueError(f"limit must be non-negative; got {limit}")
        if min_occurrences < 1:
            raise ValueError(
                f"min_occurrences must be at least 1; got {min_occurrences}. An entry "
                f"cannot exist with zero provenance, so a floor below 1 is a filter that "
                f"reads as meaningful and filters nothing."
            )
        with self._lock:
            matches = [
                e
                for e in self._entries.values()
                if e.kind == kind
                and (agent_id is None or e.agent_id == agent_id)
                and e.occurrence_count >= min_occurrences
            ]
            # Most recent first, ties broken by key. A stable TOTAL order, for
            # the reason `MemoryRetriever` sorts to one: two runs of the same
            # node must see the same entries in the same order, or
            # `ReplayEngine` reports a determinism violation that is really a
            # sort-order artefact. `agent_id or ""` because None does not
            # compare against str.
            matches.sort(
                key=lambda e: (
                    _sort_ts(e.last_seen),
                    _invert(e.agent_id),
                    _invert(e.signature),
                ),
                reverse=True,
            )
            return [_snapshot(entry) for entry in matches[:limit]]

    def get(self, key: tuple[str | None, str]) -> KnowledgeEntry | None:
        with self._lock:
            entry = self._entries.get(key)
            return None if entry is None else _snapshot(entry)


def _merge(existing: KnowledgeEntry, incoming: KnowledgeEntry) -> KnowledgeEntry:
    """Union the evidence; keep the newer consolidation's content.

    Order-preserving dedupe rather than `set()`: the provenance list is the
    entry's audit trail, and a trail whose order changes between two identical
    consolidations is not one anybody can diff.
    """
    seen = list(existing.source_record_ids)
    known = set(seen)
    for record_id in incoming.source_record_ids:
        if record_id not in known:
            known.add(record_id)
            seen.append(record_id)
    return dataclasses.replace(
        incoming,
        source_record_ids=tuple(seen),
        first_seen=_earliest(existing.first_seen, incoming.first_seen),
        last_seen=_latest(existing.last_seen, incoming.last_seen),
    )


def _earliest(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _latest(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _sort_ts(value: datetime | None) -> datetime:
    return value if value is not None else _EPOCH


class _Inverted:
    """Sorts opposite to the wrapped string, so a `reverse=True` sort can put
    timestamps newest-first while keeping the tie-break ASCENDING by key.

    Without this the tie-break would also invert, which is still a total order
    but a surprising one: two entries seen at the same instant would come back
    in reverse alphabetical order.
    """

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __lt__(self, other: _Inverted) -> bool:
        return self.value > other.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Inverted) and self.value == other.value

    def __hash__(self) -> int:
        return hash(self.value)


def _invert(value: str | None) -> _Inverted:
    return _Inverted(value or "")


def _snapshot(entry: KnowledgeEntry) -> KnowledgeEntry:
    return dataclasses.replace(entry, content=deepcopy(entry.content))
