"""Phase 4 interface stubs: the graph evolution engine (report §11,
blueprint Part 11) — automatic node/edge mutation, pruning, merging, and
specialization, gated by shadow execution, a null-hypothesis-baseline
control, and bounded/reversible deployment.

DISABLED BY DEFAULT (constraint #7): `EvolutionConfig.enabled` defaults to
`False` and every interface below remains abstract. The seven supporting
mechanisms are now implemented in `aef/harness/`, but live-traffic and
real-tenant evidence does not yet support enabling this subsystem. Re-enabling
also remains an explicit owner decision, not a flag this package may infer from
unit-test completeness. The criteria that decision must assess are:

  1. Shadow execution: every structural mutation runs against live traffic
     in read-only, no-side-effect mode for a minimum trace count before
     it's even eligible for promotion (blueprint §4.3).
  2. Null-hypothesis baseline: a randomized-mutation control group must be
     beaten, not just an absolute score threshold — this is RAPTOR's own
     anti-overfitting discipline applied to graph mutations (blueprint
     §4.3, §11.2).
  3. Golden-trace regression: every promoted graph version must still pass
     100% of the accumulated golden-trace corpus; the corpus is never
     allowed to shrink (blueprint §2.4, §11.2).
  4. Bounded mutation rate: at most K structural mutations per graph per
     time window (blueprint §4.3).
  5. Cumulative-drift monitoring: track accumulated sub-threshold edits — a
     documented failure mode where several compliant edits produced a
     later regression (report §11).
  6. Canary rollout: stratified by tenant tag, gated on percentiles not
     means, with the previous stable version kept warm >=24h so rollback
     never cold-starts (report §11).
  7. Human-in-the-loop approval above a configurable risk threshold, with
     signed release manifests (report §11, constraint #6).

See docs/roadmap.md Phase 4 and docs/adr/0006 for the full gating decision.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class EvolutionConfig:
    enabled: bool = False  # owner-gated; live validation is still missing
    max_mutations_per_window: int = 0
    canary_traffic_fraction: float = 0.0
    require_hitl_above_risk: float = 0.0

    def __post_init__(self) -> None:
        if self.enabled:
            raise NotImplementedError(
                "evolution.enabled=True is rejected: all seven Phase 4 safety mechanisms "
                "are implemented, but have not been validated against live traffic and real "
                "tenants. Enabling evolution remains an explicit owner decision; see "
                "docs/roadmap.md Phase 4 and docs/trust/promotion-trust-case.md."
            )


@dataclass(frozen=True)
class MutationCandidate:
    graph_id: str
    description: str
    diff_summary: str
    predicted_improvement: float | None = None


class MutationProposer(ABC):
    @abstractmethod
    def propose(self, graph_id: str) -> list[MutationCandidate]:
        raise NotImplementedError("MutationProposer is a Phase 4 interface; evolution is disabled")


class ArchiveStore(ABC):
    """Darwin-Gödel-Machine-style archive: every variant is retained,
    including less-performant ancestors, because they can serve as
    stepping stones or rollback points rather than dead ends (report §11)."""

    @abstractmethod
    def archive(self, graph_id: str, version: str) -> None:
        raise NotImplementedError("ArchiveStore is a Phase 4 interface; evolution is disabled")

    @abstractmethod
    def rollback(self, graph_id: str, to_version: str) -> None:
        raise NotImplementedError("ArchiveStore is a Phase 4 interface; evolution is disabled")


class EvalGate(ABC):
    """Combines shadow execution + null-hypothesis baseline + golden-trace
    regression into one pass/fail decision before a mutation is eligible
    for canary rollout."""

    @abstractmethod
    def check(self, candidate: MutationCandidate) -> bool:
        raise NotImplementedError("EvalGate is a Phase 4 interface; evolution is disabled")


class CanaryController(ABC):
    @abstractmethod
    def start_canary(self, graph_id: str, version: str, *, traffic_fraction: float) -> None:
        raise NotImplementedError("CanaryController is a Phase 4 interface; evolution is disabled")

    @abstractmethod
    def promote(self, graph_id: str, version: str) -> None:
        raise NotImplementedError("CanaryController is a Phase 4 interface; evolution is disabled")

    @abstractmethod
    def rollback(self, graph_id: str, version: str) -> None:
        raise NotImplementedError("CanaryController is a Phase 4 interface; evolution is disabled")
