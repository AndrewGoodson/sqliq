"""`MemoryStore` — the pluggable interface behind the six-type CoALA-grounded
taxonomy (report §7 / blueprint §5.1): working, episodic, semantic,
procedural, failure, success, plus tool-call memory.

No vendor SDK is imported here. `InMemoryMemoryStore` (this package) is a
real, fully-tested backend; `adapters/mem0_adapter.py` wraps `mem0ai`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

MemoryKind = Literal[
    "working",
    "episodic",
    "semantic",
    "procedural",
    "failure",
    "success",
    "tool",
]


@dataclass(frozen=True)
class MemoryRecord:
    kind: MemoryKind
    content: dict[str, Any]
    run_id: str | None = None
    agent_id: str | None = None
    tags: tuple[str, ...] = ()
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime | None = None
    # Temporal validity window (report §7, Zep/Graphiti rationale, blueprint
    # §5.2): a fact can be true only between these bounds. None means
    # "unbounded" on that side. Only meaningful for kind="semantic".
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class MemoryStore(ABC):
    @abstractmethod
    def write(self, record: MemoryRecord) -> str:
        """Persist a record; returns its id (== record.id)."""
        raise NotImplementedError

    @abstractmethod
    def query(
        self,
        kind: MemoryKind,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        tags: tuple[str, ...] = (),
        limit: int = 10,
    ) -> list[MemoryRecord]:
        """Most-recent-first records matching kind and the given filters.
        `tags` filters to records containing ALL given tags. `limit` must be
        non-negative; zero returns no records."""
        raise NotImplementedError

    @abstractmethod
    def get(self, record_id: str) -> MemoryRecord | None:
        raise NotImplementedError
