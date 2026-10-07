"""`Retriever` — Phase 2 interface stub for the context-engineering cascade
(report §12 / blueprint Part 7). Retrieval, ranking, pruning, compression,
and assembly are not implemented in Phase 0/1; nodes needing context today
read `AEFState.retrieved_context` directly, populated by whatever upstream
node wrote to it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RetrievedChunk:
    content: str
    source: str
    relevance_score: float
    token_estimate: int
    metadata: dict[str, Any]


class Retriever(ABC):
    @abstractmethod
    def retrieve(self, query: str, *, token_budget: int) -> list[RetrievedChunk]:
        """Retrieve -> rank -> prune -> compress -> assemble, bounded by
        `token_budget` (report §12). Not implemented until Phase 2."""
        raise NotImplementedError("Retriever is a Phase 2 interface; no backend is wired yet")
