"""Offline AEF learning candidates. No database content or automatic promotion."""
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from aef.services.knowledge.consolidate import RuleBasedConsolidator
from aef.services.knowledge.in_memory import InMemoryKnowledgeStore
from aef.services.memory.base import MemoryRecord
from aef.services.memory.in_memory import InMemoryMemoryStore
from pydantic import BaseModel, ConfigDict, Field


class Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: UUID
    agent: Literal["azure", "sql"]
    control: Literal["private_endpoint", "entra_only", "metadata_scope", "schema_review"]
    outcome: Literal["failure", "success"]


class OutcomeBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcomes: list[Outcome] = Field(max_length=500)


def learn(batch: OutcomeBatch) -> dict:
    memory = InMemoryMemoryStore(clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    knowledge = InMemoryKnowledgeStore()
    for event in batch.outcomes:
        # Enumerated signals only. No identifiers, prompts, schema or row content.
        memory.write(MemoryRecord(
            kind=event.outcome, agent_id=event.agent, run_id=str(event.run_id),
            id=f"{event.run_id}:{event.agent}:{event.control}:{event.outcome}",
            content={"failing_nodes": [event.control], "objective": event.control,
                     "feedback": f"Review {event.control} evidence and regression coverage."}))
    candidates = []
    for agent in ("azure", "sql"):
        for entry in RuleBasedConsolidator().consolidate(memory, knowledge, agent_id=agent):
            candidates.append({"agent": entry.agent_id, "signal": entry.signature,
                               "distinct_runs": entry.occurrence_count,
                               "evidence_ids": list(entry.source_record_ids),
                               "status": "REVIEW REQUIRED"})
    return {"mode": "offline_candidate_learning", "candidates": candidates,
            "automatic_promotion": False, "policy_changes": False,
            "next_step": "Human review, separate source change, regression tests and release review."}
