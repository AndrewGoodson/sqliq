"""The fixed node/edge contract (constraint #2) and the DI `Services`
container (constraint #3) that makes it possible.

Node signature is fixed and non-negotiable:

    (AEFState, Context, Services) -> tuple[StateDelta, Route]

Nodes never reach for globals, never construct their own clients, never read
env vars. Everything arrives via `Services`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from aef.kernel.durability import DurabilityBackend
from aef.observability.base import Tracer
from aef.providers.base import ModelProvider
from aef.reasoning.reflection import Critic, Judge
from aef.security.tool import PolicyEngine, Tool
from aef.services.context.base import Retriever
from aef.services.eval.base import Evaluator
from aef.services.knowledge.base import KnowledgeStore
from aef.services.memory.base import MemoryStore
from aef.services.optimizers.base import Optimizer
from aef.state import AEFState, StateDelta


class ServiceNotConfiguredError(RuntimeError):
    def __init__(self, service_name: str) -> None:
        super().__init__(
            f"service {service_name!r} was not configured on this Services container; "
            f"wire it in via config before any node that depends on it can run"
        )


def hitl_approval_key(from_node: str, to_node: str) -> str:
    """Canonical key for a granted human approval to cross a specific edge.

    `Edge.requires_human_approval` is meaningless without something that
    actually checks it — see `Services.hitl_approvals` and docs/adr/0011.

    The encoding is JSON rather than `f"{from_node}->{to_node}"`, because the
    bare delimiter was not an identity — it was a **collision**. Node ids may
    themselves contain `->`, and with self-coding they are agent-authored, so:

        hitl_approval_key("a->b", "c") == "a->b->c"
        hitl_approval_key("a", "b->c") == "a->b->c"

    An approval minted for one edge therefore satisfied the gate on a
    *different* edge, which contradicts ADR 0011's claim that approvals are
    edge-specific and explicit. JSON quotes and escapes both components, so
    distinct edges cannot render alike.

    Nothing persists these keys — `Services.hitl_approvals` is an in-memory
    frozenset supplied per run — so there is no stored approval to migrate, and
    every caller in the tree mints keys through this function rather than
    hand-writing the string.
    """
    return json.dumps([from_node, to_node], separators=(",", ":"))


@dataclass(frozen=True)
class Services:
    """Dependency-injection container. Every backend a node might need is a
    field here; nodes receive an instance and never construct their own."""

    model_provider: ModelProvider | None = None
    memory: MemoryStore | None = None
    # `graph_store` was here, unread, from Phase 0 until ADR 0101 deleted it.
    # A slot no code fills and no config can configure is a promise, and the
    # config already refuses `knowledge_graph` outright (ADR 0100).
    retriever: Retriever | None = None
    evaluator: Evaluator | None = None
    # Consolidated knowledge (ADR 0110). A slot rather than a global for the
    # same reason `critic`/`judge` needed one: constraint #2 fixes the node
    # signature, so a node cannot reach a store that is not on `Services` at
    # all. Unlike the `graph_store` slot deleted above, this one is filled —
    # `make_consolidate_node` requires it, and `agent_services` defaults it.
    knowledge: KnowledgeStore | None = None
    # Reflection backends. Constraint #2 (fixed node signature, DI-only, no
    # globals) means a Critic/Judge cannot be reached from a node at all
    # without a slot here — these two were the only pluggable backends
    # missing one, which is why `aef/reasoning/reflection.py` sat unwireable.
    critic: Critic | None = None
    judge: Judge | None = None
    tracer: Tracer | None = None
    tools: Mapping[str, Tool] = field(default_factory=dict)
    policy_engine: PolicyEngine | None = None
    optimizer: Optimizer | None = None
    durability: DurabilityBackend | None = None
    # Edges the caller has explicitly pre-approved for this run, keyed by
    # hitl_approval_key(from_node, to_node). GraphExecutor consults this
    # before crossing any Edge with requires_human_approval=True; an edge
    # not in this set is refused, not silently allowed (constraint #6:
    # deny-by-default, explicit HITL approval for consequential actions).
    hitl_approvals: frozenset[str] = frozenset()
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def has_hitl_approval(self, from_node: str, to_node: str) -> bool:
        return hitl_approval_key(from_node, to_node) in self.hitl_approvals

    def require_model_provider(self) -> ModelProvider:
        if self.model_provider is None:
            raise ServiceNotConfiguredError("model_provider")
        return self.model_provider

    def require_memory(self) -> MemoryStore:
        if self.memory is None:
            raise ServiceNotConfiguredError("memory")
        return self.memory

    def require_knowledge(self) -> KnowledgeStore:
        if self.knowledge is None:
            raise ServiceNotConfiguredError("knowledge")
        return self.knowledge

    def require_evaluator(self) -> Evaluator:
        if self.evaluator is None:
            raise ServiceNotConfiguredError("evaluator")
        return self.evaluator

    def require_retriever(self) -> Retriever:
        if self.retriever is None:
            raise ServiceNotConfiguredError("retriever")
        return self.retriever

    def require_critic(self) -> Critic:
        if self.critic is None:
            raise ServiceNotConfiguredError("critic")
        return self.critic

    def require_judge(self) -> Judge:
        if self.judge is None:
            raise ServiceNotConfiguredError("judge")
        return self.judge

    def require_tracer(self) -> Tracer:
        if self.tracer is None:
            raise ServiceNotConfiguredError("tracer")
        return self.tracer

    def require_durability(self) -> DurabilityBackend:
        if self.durability is None:
            raise ServiceNotConfiguredError("durability")
        return self.durability

    def require_policy_engine(self) -> PolicyEngine:
        if self.policy_engine is None:
            raise ServiceNotConfiguredError("policy_engine")
        return self.policy_engine


@dataclass(frozen=True)
class Context:
    """Per-execution, per-node context. `now` is captured once by the
    executor via `Services.clock` and handed to the node — nodes never call
    the clock themselves, which is what keeps them replayable.

    `idempotency_key` is `node.idempotency_key_fn(state)`, computed by the
    executor before calling `node.fn` — without this field a node has no
    way to reach its own `idempotency_key_fn`, since that's a sibling field
    on `Node`, not something passed into the function body. The kernel does
    NOT enforce idempotency on the node's behalf (see docs/adr/0010): it
    computes and exposes the key so the node can pass it to whatever
    external system it calls, which is where real deduplication has to
    happen. `None` for nodes with no `idempotency_key_fn` (i.e. pure nodes).
    """

    run_id: str
    graph_version: str
    trace_id: str
    node_id: str
    now: datetime
    idempotency_key: str | None = None
    # Forward-declared, not yet wired: the executor always constructs Context
    # with attempt=1 and nothing increments it — a node CANNOT currently tell
    # a first run from an at-least-once resume/HITL re-execution (ADR 0032)
    # via this field. A retry/attempt-tracking loop is a later addition; until
    # then, do not read `attempt` expecting it to reflect re-execution count.
    attempt: int = 1


class SideEffect(StrEnum):
    PURE = "pure"
    IO = "io"
    EXTERNAL_CALL = "external_call"
    MUTATING = "mutating"


@dataclass(frozen=True)
class CostModel:
    """A node's declared *predicted* cost (blueprint §2.1) — for the
    Phase 2 planner's resource budgeting and the Phase 4 evolution engine's
    Pareto-aware candidate selection (accuracy vs. cost vs. latency), per
    the report. No Phase 0/1 code reads this yet, unlike `idempotency_key_fn`
    and `Edge.requires_human_approval`/`requires_deterministic_fallback`
    (see docs/adr/0010, 0011) — those had a validation or safety story that
    made an unwired field misleading; this one doesn't, since nothing in
    Phase 0/1 does planning or evolution to consume it. It's genuinely
    forward-declared, not a gap."""

    tokens: int = 0
    latency_p50_ms: float = 0.0
    latency_p99_ms: float = 0.0
    dollars_per_call: float = 0.0


class _End:
    """Sentinel returned as `Route` to terminate graph execution. A dedicated
    type (not a string) so it can never collide with a real node id."""

    _instance: _End | None = None

    def __new__(cls) -> _End:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "END"


END: Final[_End] = _End()

# `Route` may name one node, several (fan-out), or `END`. Fan-out is part of
# the *contract* (blueprint §2.2 declares `to_node: str | list[str]`) but the
# Phase 0/1 `GraphExecutor` only executes single-target routes — see
# `GraphExecutor.run`'s NotImplementedError and docs/adr/0007. Declaring the
# full contract now means a Phase 2 BSP-style executor is an executor change,
# not a schema change.
Route = str | tuple[str, ...] | _End

NodeFn = Callable[[AEFState, Context, Services], "tuple[StateDelta, Route]"]
IdempotencyKeyFn = Callable[[AEFState], str]


class NodeContractError(ValueError):
    pass


@dataclass(frozen=True)
class Node:
    id: str
    version: str  # semver, independent of graph version (blueprint §2.1)
    fn: NodeFn
    deterministic: bool  # constraint #1: enforced by the replay engine
    side_effects: SideEffect = SideEffect.PURE
    cost_model: CostModel = field(default_factory=CostModel)
    idempotency_key_fn: IdempotencyKeyFn | None = None
    telemetry_tags: tuple[str, ...] = ()
    fallback_node_id: str | None = None

    def __post_init__(self) -> None:
        if self.side_effects is not SideEffect.PURE and self.idempotency_key_fn is None:
            raise NodeContractError(
                f"node {self.id!r} declares side_effects={self.side_effects.value!r} "
                f"but has no idempotency_key_fn (blueprint §2.1 requires one whenever "
                f"side_effects != pure)"
            )


RouteCondition = Callable[[AEFState], bool]


def _always(state: AEFState) -> bool:
    return True


@dataclass(frozen=True, eq=False)
class Edge:
    from_node: str
    to_node: str | tuple[str, ...]  # tuple = fan-out declaration; see Route above
    condition: RouteCondition = _always
    priority: int = 0
    requires_human_approval: bool = False
    # blueprint §2.2: any edge an LLM-decided route may take without a
    # deterministic equivalent must say so explicitly and out loud.
    requires_deterministic_fallback: bool = False

    @property
    def targets(self) -> tuple[str, ...]:
        return (self.to_node,) if isinstance(self.to_node, str) else self.to_node

    def _condition_key(self) -> object:
        # Two lambdas/functions defined at the same source location produce
        # equal (in CPython, often the literal same) __code__ objects across
        # separate calls that (re)define them — e.g. every call to a
        # build_graph() function. Comparing __code__ instead of the raw
        # callable's object identity lets Graph.diff() correctly treat a
        # freshly-rebuilt, structurally-identical graph as unchanged instead
        # of reporting every edge with a non-default condition as both added
        # and removed on every rebuild — a real, demonstrated bug (see
        # docs/adr/0020), not a hypothetical one.
        return getattr(self.condition, "__code__", self.condition)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Edge):
            return NotImplemented
        return (
            self.from_node == other.from_node
            and self.to_node == other.to_node
            and self._condition_key() == other._condition_key()
            and self.priority == other.priority
            and self.requires_human_approval == other.requires_human_approval
            and self.requires_deterministic_fallback == other.requires_deterministic_fallback
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.from_node,
                self.to_node,
                self._condition_key(),
                self.priority,
                self.requires_human_approval,
                self.requires_deterministic_fallback,
            )
        )
