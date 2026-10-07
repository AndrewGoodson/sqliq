"""Recorded service inputs. Never a live capability or a production write path."""

from __future__ import annotations

from collections.abc import Iterable

from aef.config.schema import ContextConfig
from aef.harness.memory_store import SnapshotMemoryStore
from aef.providers.base import (
    CompletionRequest,
    CompletionResult,
    ModelProvider,
    ModelProviderError,
)
from aef.services.context.base import Retriever
from aef.services.context.memory_retriever import MemoryRetriever
from aef.services.knowledge.consolidate import RuleBasedConsolidator
from aef.services.knowledge.in_memory import InMemoryKnowledgeStore
from aef.services.memory.base import MemoryRecord, MemoryStore
from aef.services.memory.in_memory import InMemoryMemoryStore


def recorded_context(retriever: Retriever | None) -> ContextConfig | None:
    """Capture only the built-in retriever's four configuration knobs.

    Read the service that actually ran, including resolved defaults. This is
    data, never a serialized service, arbitrary YAML, or provider permission.
    Custom retrievers remain outside this recording contract (ADR 0210).
    """
    if type(retriever) is not MemoryRetriever:
        return None
    return ContextConfig(
        impl="memory",
        token_budget=retriever.max_token_budget,
        knowledge_boost=retriever.knowledge_boost,
        staleness_half_life=retriever.staleness_half_life,
        knowledge_min_occurrences=retriever.knowledge_min_occurrences,
    )


class RecordedIsolation(ModelProvider):
    """Replay an observed declaration; never answer a completion or grant tools."""

    DEFAULT_NAME = "recorded-isolation"

    def __init__(self, isolation: Iterable[str], name: str = "") -> None:
        self._isolation = frozenset(isolation)
        self.name = name or self.DEFAULT_NAME

    @property
    def isolation(self) -> frozenset[str]:
        return self._isolation

    def complete(self, request: CompletionRequest) -> CompletionResult:
        raise ModelProviderError(
            "no live provider to fall through to: replay has no live provider by design; "
            "this object carries "
            "the recorded provider's isolation declaration and answers nothing. A request "
            "reaching it means the cassette missed, which is a behavioural difference."
        )


def replay_memory(
    records: tuple[MemoryRecord, ...] | None,
    agent_id: str,
) -> tuple[MemoryStore, InMemoryKnowledgeStore]:
    """Rebuild pre-run knowledge exactly as the CLI does with durable memory.

    None means a legacy recording with no captured memory. Do not backfill
    it from today's file or pretend the missing inputs were recorded.
    """
    memory: MemoryStore = InMemoryMemoryStore() if records is None else SnapshotMemoryStore(records)
    knowledge = InMemoryKnowledgeStore()
    if records is not None:
        if any(r.agent_id != agent_id for r in records):
            raise ValueError("recorded memory contains another agent's records")
        RuleBasedConsolidator().consolidate(memory, knowledge, agent_id=agent_id)
    return memory, knowledge
