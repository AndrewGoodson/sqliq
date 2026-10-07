"""`GraphExecutor` — runs a `CompiledGraph` one super-step (one node) at a
time. This is the control plane: sequencing and routing here are pure
bookkeeping over what nodes return, with zero model calls or I/O of its own.

Every super-step is checkpointed (if a `DurabilityBackend` is configured) and
traced (if a `Tracer` is configured) — observability and durability are
opt-in via DI, never hardwired.

`run()` always starts at `graph.entry_node`. Calling it again with a
previously-checkpointed `AEFState` does NOT resume a crashed/paused run —
it restarts from the top and re-executes every node again against the
already-advanced state, duplicating any non-pure side effects. Use
`resume()` to actually continue from where a run left off; see
`DurabilityBackend.save_cursor`/`load_cursor` and docs/adr/0009.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass

from aef.kernel.contracts import Context, Edge, Node, Route, Services, _End, hitl_approval_key
from aef.kernel.durability import CorruptedCheckpointError
from aef.kernel.graph import CompiledGraph, Graph
from aef.observability import semconv
from aef.state import AEFState, StateDelta


class GraphExecutionError(RuntimeError):
    pass


class RoutingViolationError(RuntimeError):
    """A node returned a route not backed by any declared, currently-true
    edge (report §4 / blueprint §2.2: edges are typed and declared
    statically — a node cannot silently invent a new transition)."""


class HumanApprovalRequiredError(RuntimeError):
    """A node routed across an `Edge` declared `requires_human_approval=True`
    without that specific edge being pre-approved via
    `Services.hitl_approvals` (constraint #6: deny-by-default, explicit HITL
    for consequential actions). See docs/adr/0011."""


@dataclass(frozen=True)
class NodeExecutionRecord:
    """One super-step, captured for replay (`aef.kernel.replay`)."""

    node_id: str
    input_state: AEFState
    context: Context
    delta: StateDelta
    route: Route
    # True when this record is the error/fallback path: the node's `fn`
    # raised and it declared a `fallback_node_id` (ADR 0036), so `delta` is
    # a synthesized error-delta and `route` is the fallback target. Replay
    # MUST trust such a record rather than re-executing `fn` to verify
    # determinism — re-execution would just raise the original exception
    # again (ADR 0039). `False` for the normal path.
    is_fallback: bool = False


@dataclass(frozen=True)
class ExecutionResult:
    final_state: AEFState
    trace: tuple[NodeExecutionRecord, ...] | None = None


def _snapshot(state: AEFState) -> AEFState:
    """A deep copy for the trace record, falling back to the live object.

    A `threading.Lock` or open handle in `working_memory` is not
    deep-copyable, and raising here would make an agent that parks one there
    unrunnable — legal before ADR 0087 (ADR 0089). Such a value cannot be
    checkpointed either, so the fallback loses a purity guarantee that was
    never available for it.
    """
    try:
        return state.model_copy(deep=True)
    except (TypeError, ValueError):
        return state


class GraphExecutor:
    def __init__(
        self, compiled: CompiledGraph, services: Services, *, max_steps: int = 1000
    ) -> None:
        self._graph: Graph = compiled.graph
        self._services = services
        self._max_steps = max_steps

    def run(self, initial_state: AEFState, *, record_trace: bool = False) -> ExecutionResult:
        return self._run_from(initial_state, self._graph.entry_node, record_trace=record_trace)

    def resume(self, run_id: str, *, record_trace: bool = False) -> ExecutionResult:
        """Continue a previously-checkpointed run from wherever it left off,
        using the durability backend's saved cursor — unlike `run()`, this
        does not re-execute nodes that already completed."""
        durability = self._services.require_durability()
        state = durability.load_latest(run_id)
        if state is None:
            raise GraphExecutionError(
                f"no checkpoints found for run_id={run_id!r}; nothing to resume"
            )

        # The state may have been REWOUND past a corrupt checkpoint
        # (ADR 0031), and the cursor was not — it still names the node that
        # was to run after the checkpoint that could not be read. Pairing a
        # rewound state with a live cursor SILENTLY SKIPS every node in
        # between: a three-node run resumed as if the middle node had never
        # existed, with no error, and the resume then overwrote the torn file
        # so the evidence disappeared (ADR 0086).
        #
        # Both numbers are right here. Refusing is the only safe answer: the
        # work between them is lost, and continuing would produce a final
        # state that never existed.
        recorded = durability.list_checkpoints(run_id)
        if recorded and state.checkpoint_seq < max(recorded):
            raise CorruptedCheckpointError(
                f"cannot resume run_id={run_id!r}: the newest readable checkpoint is "
                f"seq {state.checkpoint_seq} but seq {max(recorded)} exists and could not "
                f"be read. Resuming from the rewound state would skip every node between "
                f"them and report success. Repair or remove the damaged checkpoint."
            )

        cursor = durability.load_cursor(run_id)
        if cursor is None:
            # The backend verified the cursor belongs to this checkpoint.
            # Missing/stale cursor data is an error, never evidence of END.
            return ExecutionResult(final_state=state, trace=() if record_trace else None)

        return self._run_from(state, cursor, record_trace=record_trace)

    def _run_from(
        self, initial_state: AEFState, start_node: str | _End, *, record_trace: bool
    ) -> ExecutionResult:
        state = initial_state
        current: str | _End = start_node
        trace: list[NodeExecutionRecord] = []
        durability = self._services.durability

        for step in range(self._max_steps + 1):
            if isinstance(current, _End):
                if durability is not None:
                    durability.save_cursor(state.run_id, None)
                return ExecutionResult(
                    final_state=state, trace=tuple(trace) if record_trace else None
                )

            # END consumes no node budget. Inspect it after the last permitted
            # execution, but never execute an (N+1)th node (ADR 0207).
            if step == self._max_steps:
                break

            node = self._graph.nodes.get(current)
            if node is None:
                raise GraphExecutionError(f"no such node {current!r}")

            input_state = state  # the node's input, needed if a HITL gate blocks below
            ctx = Context(
                run_id=state.run_id,
                graph_version=self._graph.version,
                trace_id=state.provenance[-1].trace_id if state.provenance else state.run_id,
                node_id=node.id,
                now=self._services.clock(),
                idempotency_key=(
                    node.idempotency_key_fn(state) if node.idempotency_key_fn is not None else None
                ),
            )

            # Snapshot BEFORE the node runs. A node receives the live state
            # object, and nothing stops it mutating a nested structure in
            # place — which rewrote the very record that is supposed to be
            # the history of what it was given. `StateDelta.apply` is now
            # deeply pure (ADR 0087), but that governs apply's OUTPUT; this
            # is apply's INPUT, and it is the thing the record holds.
            recorded_input = _snapshot(state) if record_trace else state
            delta, route, fallback_target = self._execute_node(node, state, ctx)
            new_state = delta.apply(state)
            if record_trace:
                trace.append(
                    NodeExecutionRecord(
                        node_id=node.id,
                        input_state=recorded_input,
                        context=ctx,
                        delta=delta,
                        route=route,
                        is_fallback=fallback_target is not None,
                    )
                )
            state = new_state
            if fallback_target is not None:
                # The node raised and declared a fallback. A fallback is an
                # error handler — it fires unconditionally, bypassing normal
                # edge-condition resolution (which would otherwise mask the
                # original error with a RoutingViolationError when no
                # true-condition edge to the fallback exists). The target is
                # validated as a real node by the loop's own node lookup next
                # iteration. See docs/adr/0036.
                if durability is not None:
                    durability.save_checkpoint(state)
                    durability.save_cursor(state.run_id, fallback_target)
                current = fallback_target
                continue
            try:
                next_node = self._resolve_route(node, route, state)
            except HumanApprovalRequiredError:
                # The gate blocks AFTER this node ran but the run must stay
                # resumable. Persist a checkpoint of the node's INPUT state and
                # a cursor pointing back at this node, so resume() re-executes
                # it from the same input once approval is granted. Without this,
                # a gate on the entry node's edge left nothing checkpointed and
                # resume() raised "nothing to resume" (review Finding 2 / ADR
                # 0032). Re-execution of the gated node on resume is deliberate
                # at-least-once semantics (matches LangGraph interrupt() /
                # Temporal activities); the node's idempotency_key is the
                # mitigation and the kernel does NOT dedupe (ADR 0010).
                if durability is not None:
                    durability.save_checkpoint(input_state)
                    durability.save_cursor(input_state.run_id, current)
                raise
            if durability is not None:
                durability.save_checkpoint(state)
                durability.save_cursor(
                    state.run_id, None if isinstance(next_node, _End) else next_node
                )
            current = next_node

        raise GraphExecutionError(f"exceeded max_steps={self._max_steps} without reaching END")

    def _execute_node(
        self, node: Node, state: AEFState, ctx: Context
    ) -> tuple[StateDelta, Route, str | None]:
        """Returns (delta, route, fallback_target). `fallback_target` is the
        node id to route to unconditionally because the node raised and
        declared a `fallback_node_id`; `None` on the normal (no-exception)
        path, where `route` is what the node returned."""
        tracer = self._services.tracer
        attributes: dict[str, object] = {
            semconv.AEF_RUN_ID: ctx.run_id,
            semconv.AEF_NODE_ID: node.id,
            semconv.AEF_GRAPH_VERSION: ctx.graph_version,
            semconv.AEF_NODE_DETERMINISTIC: node.deterministic,
            semconv.AEF_NODE_SIDE_EFFECTS: node.side_effects.value,
            semconv.AEF_CHECKPOINT_SEQ: state.checkpoint_seq,
        }
        if node.telemetry_tags:
            attributes[semconv.AEF_NODE_TELEMETRY_TAGS] = node.telemetry_tags
        span_cm = (
            tracer.span(f"aef.node.{node.id}", attributes)
            if tracer is not None
            else nullcontext(None)
        )
        with span_cm as span:
            try:
                delta, route = node.fn(state, ctx, self._services)
            except Exception as exc:
                if node.fallback_node_id is not None:
                    if span is not None:
                        span.record_exception(exc)
                    error_delta = StateDelta(
                        errors=[
                            {
                                "node_id": node.id,
                                "error": str(exc),
                                "error_type": type(exc).__name__,
                            }
                        ]
                    )
                    return error_delta, node.fallback_node_id, node.fallback_node_id
                raise
            if span is not None and delta.provenance:
                total_tokens = sum(p.token_cost for p in delta.provenance)
                span.set_attribute(semconv.GEN_AI_USAGE_OUTPUT_TOKENS, total_tokens)
                last_model = delta.provenance[-1].model
                if last_model is not None:
                    span.set_attribute(semconv.GEN_AI_RESPONSE_MODEL, last_model)
            return delta, route, None

    def _resolve_route(self, node: Node, route: Route, state: AEFState) -> str | _End:
        if isinstance(route, _End):
            return route
        if isinstance(route, tuple):
            raise NotImplementedError(
                "fan-out routes are part of the Edge/Route contract but are not yet "
                "executed — BSP-style parallel super-steps are deferred to a Phase 2 "
                "executor; see docs/adr/0007"
            )
        candidates = self._graph.edges_from(node.id)
        for edge in candidates:
            if route in edge.targets and edge.condition(state):
                if edge.requires_human_approval and not self._services.has_hitl_approval(
                    edge.from_node, route
                ):
                    key = hitl_approval_key(edge.from_node, route)
                    raise HumanApprovalRequiredError(
                        f"edge {edge.from_node!r} -> {route!r} requires human approval; "
                        f"grant it via Services(hitl_approvals=frozenset({{{key!r}}})) "
                        f"before calling run()/resume() again"
                    )
                if edge.requires_deterministic_fallback:
                    self._record_emergent_routing(edge, route)
                return route
        raise RoutingViolationError(
            f"node {node.id!r} routed to {route!r}, but no declared edge from {node.id!r} "
            f"to {route!r} currently has a true condition"
        )

    def _record_emergent_routing(self, edge: Edge, route: str) -> None:
        """Blueprint §2.2: an edge an LLM-decided route may take without a
        deterministic equivalent must say so "explicitly and out loud" — a
        marker span is that "out loud," since `requires_deterministic_fallback`
        alone (declared on the Edge) was previously never surfaced anywhere."""
        tracer = self._services.tracer
        if tracer is None:
            return
        with tracer.span(
            "aef.edge.emergent_routing",
            {
                semconv.AEF_EDGE_REQUIRES_DETERMINISTIC_FALLBACK: True,
                semconv.AEF_NODE_ID: edge.from_node,
                "aef.edge.to_node": route,
            },
        ):
            pass
