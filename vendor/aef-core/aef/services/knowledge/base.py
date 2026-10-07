"""`KnowledgeStore` — the persistent consolidated layer of ADR 0110, and the
middle of WikiSkill's three (arXiv:2608.27454).

A `MemoryRecord` is what happened once. A `KnowledgeEntry` is what has now
happened more than once, which is the only difference that makes it knowledge
rather than an episode.

No vendor SDK is imported here, and none may be: constraint #3 puts vendor
imports under `aef/providers/` or `aef/services/*/adapters/` only.

Nothing in this package imports `aef.evolution` and nothing in `aef.evolution`
imports this package. That is stated in ADR 0110 as a structural property, not
an aspiration — a knowledge layer does not move any of the three findings that
make `docs/trust/promotion-trust-case.md` recommend against auto-merge.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

# The kinds consolidation draws on. Deliberately a SUBSET of `MemoryKind`, not
# a copy of it: `working` is this run's own scratch and consolidating it across
# runs would be meaningless, and `semantic` carries a temporal validity window
# that this layer has no story for (ADR 0101 records why no knowledge graph is
# wired). Narrower than the source taxonomy on purpose.
KnowledgeKind = Literal["failure", "success"]

# Occurrence count at which `confidence` reaches 0.5. Saturating rather than
# linear so that the difference between 2 and 4 occurrences moves confidence
# more than the difference between 20 and 22 — which is how evidence actually
# accumulates. Named rather than buried because I4's A/B may well move it.
CONFIDENCE_HALF_LIFE = 4


@dataclass(frozen=True)
class KnowledgeEntry:
    """One consolidated lesson, keyed by `(agent_id, signature)`.

    `signature` is a derived, deterministic value — NOT a hash of a whole
    record. Two runs of the same failure differ in `run_id`, timestamps and
    excerpt text, so a whole-record hash consolidates nothing at all (ADR
    0110). The function that derives it lives with the consolidator; this
    dataclass only requires that it be stable.
    """

    signature: str
    kind: KnowledgeKind
    content: dict[str, Any]

    # REQUIRED, and required for the reason `MemoryRetriever.agent_id` is:
    # that field was found by an adversarial round to have defaulted to
    # "every agent", so a retriever built without thinking about it handed
    # one agent another agent's recorded failures. The identical seam exists
    # here and is worse, because a merged entry cannot be un-merged later.
    # `None` still means "not agent-scoped" and is still expressible — what
    # changed is that it must be CHOSEN.
    agent_id: str | None

    # Provenance: the `MemoryRecord.id`s this entry was consolidated from.
    # ADR 0101 deleted `GraphStore` partly for having no provenance story for
    # retrieved facts; an entry that cannot say what it was derived from is a
    # claim with no evidence, and this layer would be repeating that error.
    source_record_ids: tuple[str, ...] = ()

    first_seen: datetime | None = None
    last_seen: datetime | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    # Distinct runs of this agent recorded AFTER `last_seen` — how long the
    # lesson has gone without recurring (ADR 0116). Recomputed by every
    # consolidation from the records, never incremented; a field rather than
    # a `content` key so it costs no retrieval budget (rendering `content` is
    # what the retriever spends tokens on, and I4 measured that trade).
    runs_since_last_seen: int = 0
    # ACE's outcome signal (ADR 0118), computed by the consolidator from the
    # records. Three outcomes, not two, because ADR 0162 rig B measured a run
    # that had the lesson in context, RESOLVED the failure the lesson names,
    # and broke a different owner check in the same run — and the two-outcome
    # version scored it `helpful`, crediting the lesson for the failure it
    # caused (ADR 0180). Surfaced in chunk metadata and skill drafts; not a
    # ranking input on any of the three — see the retriever.
    #
    # - `helpful`           in context, and the run produced NO failure at all.
    # - `harmful`           in context, and the run reproduced THIS failure.
    # - `harmful_elsewhere` in context, this failure did not recur, and the run
    #                       failed something else instead.
    helpful: int = 0
    harmful: int = 0
    harmful_elsewhere: int = 0

    def __post_init__(self) -> None:
        # Validation here rather than at write/serialise time — ADR 0108's
        # precedent, where validation living in `to_payload()` let an invalid
        # object be constructed and passed around, failing only if it was
        # ever serialised.
        if not self.signature:
            raise ValueError("signature must be non-empty; it is the entry's identity")
        if not self.source_record_ids:
            raise ValueError(
                "source_record_ids must be non-empty: an entry with no provenance is a "
                "claim with no evidence, and could not be traced back to what produced it"
            )
        if len(set(self.source_record_ids)) != len(self.source_record_ids):
            raise ValueError(
                f"source_record_ids must be unique; got {self.source_record_ids!r}. "
                f"A repeated id inflates occurrence_count, which is the entry's only "
                f"measure of how well-evidenced it is."
            )
        if self.helpful < 0 or self.harmful < 0 or self.harmful_elsewhere < 0:
            raise ValueError("helpful/harmful/harmful_elsewhere tallies must be non-negative")
        if self.runs_since_last_seen < 0:
            raise ValueError(
                f"runs_since_last_seen must be non-negative; got {self.runs_since_last_seen}"
            )
        if (
            self.first_seen is not None
            and self.last_seen is not None
            and self.first_seen > self.last_seen
        ):
            raise ValueError(
                f"first_seen ({self.first_seen}) must not be after last_seen ({self.last_seen})"
            )

    @property
    def key(self) -> tuple[str | None, str]:
        """Identity. A tuple, not a joined string — a string key built from an
        agent id and a signature collides the moment either contains the
        delimiter, which is the defect `f7e8f01` fixed for approval keys in
        the kernel."""
        return (self.agent_id, self.signature)

    @property
    def occurrence_count(self) -> int:
        """DERIVED from provenance, never stored alongside it. Two fields
        recording the same quantity are two lists nobody compares, and they
        drift (ADR 0091)."""
        return len(self.source_record_ids)

    @property
    def confidence(self) -> float:
        """How well-evidenced this entry is, in `[0, 1)`, monotonic in
        occurrence count and saturating.

        This is a property of the ENTRY. It is deliberately not the retrieval
        multiplier: how much a confident entry outranks a raw record is a
        policy of the retriever, configurable and measured by I4's A/B. Mixing
        the measurement into the policy would make the thumb on the scale
        impossible to remove without editing stored data.
        """
        n = self.occurrence_count
        return n / (n + CONFIDENCE_HALF_LIFE)


class KnowledgeStore(ABC):
    @abstractmethod
    def upsert(self, entry: KnowledgeEntry) -> KnowledgeEntry:
        """Merge `entry` into whatever is already stored under its `key`,
        returning the stored result.

        The MERGE LIVES HERE, not in the consolidator. Keying by signature is
        only a real invariant if one place enforces it; if each consolidator
        merged for itself, two of them would drift and the second would be a
        silent duplicate-knowledge bug.

        Merge semantics: `source_record_ids` union preserving first-seen
        order, `first_seen` the earlier, `last_seen` the later, `content` and
        `tags` from the incoming entry (the newer consolidation wins, since it
        was computed from a superset of the evidence).
        """
        raise NotImplementedError

    @abstractmethod
    def query(
        self,
        kind: KnowledgeKind,
        *,
        agent_id: str | None = None,
        min_occurrences: int = 1,
        limit: int = 10,
    ) -> list[KnowledgeEntry]:
        """Most-recently-seen-first entries matching `kind` and the filters.

        `limit` must be non-negative; zero returns nothing. `min_occurrences`
        must be at least 1 — an entry cannot exist with zero provenance, so a
        floor of 0 would be a filter that reads as meaningful and filters
        nothing.
        """
        raise NotImplementedError

    @abstractmethod
    def get(self, key: tuple[str | None, str]) -> KnowledgeEntry | None:
        raise NotImplementedError
