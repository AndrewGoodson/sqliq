"""The child process that evaluates ONE node at a time and nothing else.

Three times a control was added to stop a candidate authoring the evidence
that judged it, and three times it was defeated (ADR 0085, 0088, 0093). Every
one of those fixes tried to make an in-process channel trustworthy, and none
could: the candidate's code and the reporting code shared an interpreter, so
any channel the reporter could write, the candidate could write.

So the reporting moved out. **This process does not know what a scenario is,
how many there are, what an `Outcome` is, or whether anything passed.** It
loads a graph, and then answers one question repeatedly: given this node, this
state and this context, what `(delta, route)` does the node return?

The parent owns the state, the routing, the step count, the trace and the
classification. What a candidate can still lie about is exactly what a node
returns — which is the surface the corpus, the tripwires and G2 are built to
judge. It can no longer claim scenarios it never ran, fabricate an aggregate,
or exit cleanly and have that read as success (ADR 0094).

Protocol: newline-delimited JSON on stdin/stdout, one request per line.
Deliberately boring — a candidate that writes garbage to stdout produces a
frame the parent cannot decode, which is a failed node, which is a result the
parent already knows how to handle.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from aef.harness.graph_loading import import_graph_module, split_entrypoint
from aef.harness.trace_codec import decode_context, encode_route
from aef.kernel.contracts import Edge, Node
from aef.kernel.graph import Graph
from aef.services.runtime import agent_services
from aef.state import AEFState, StateDelta


def _frame(value: Any) -> str:
    """One frame, one line. `trace_codec.dumps` pretty-prints for corpus
    files on disk; a newline-delimited protocol needs the opposite."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _unframe(text: str) -> Any:
    return json.loads(text)


class WorkerError(RuntimeError):
    pass


def load_graph(entrypoint: str) -> Graph:
    """`module:factory` **or** `path/to/graph.py:factory`. Any raise —
    including `SystemExit` — is a bad entrypoint, not a clean exit (ADR 0085).

    The file form matters here more than anywhere else. This worker is the
    CANDIDATE side of G2; the incumbent side is reconstructed from the
    recording, which `aef loop record` could load because it goes through
    `aef run`'s importer. While this function had its own
    `importlib.import_module`, the two sides resolved an entrypoint
    differently, and under `--agent-root .claude/agents` the candidate side
    failed to import while the incumbent side did not — which G2 read as a
    behavioural regression (ADR 0177). One loader, both sides.
    """
    try:
        module_name, attribute = split_entrypoint(entrypoint)
    except ValueError as exc:
        raise WorkerError(
            f"entrypoint {entrypoint!r} must be '<module or file>:<factory>'"
        ) from exc
    try:
        module = import_graph_module(module_name)
    except BaseException as exc:  # noqa: BLE001 - agent-authored import
        # `TypeError` reaches here too — `importlib.import_module` raises it,
        # not `ImportError`, for a name with a leading dot — and the
        # entrypoint is named as well as the module, because the parent
        # reports this string and "cannot import '.claude/...'" without the
        # entrypoint does not say which candidate could not be loaded.
        raise WorkerError(
            f"cannot import {module_name!r} (from entrypoint {entrypoint!r}): "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    factory = getattr(module, attribute, None)
    if factory is None:
        raise WorkerError(f"{module_name!r} has no attribute {attribute!r}")
    try:
        graph = factory()
    except BaseException as exc:  # noqa: BLE001 - agent-authored factory
        raise WorkerError(f"{entrypoint} raised {type(exc).__name__}: {exc}") from exc
    if not isinstance(graph, Graph):
        raise WorkerError(f"{entrypoint} returned {type(graph).__name__}, expected a Graph")
    return graph


def describe(graph: Graph) -> dict[str, Any]:
    """The graph's SHAPE, for the parent to drive with.

    This is the candidate's declaration about itself, exactly like its source
    — and it is judged the same way, by G0 and G4 reading the base ref. What
    matters here is that the parent, not the child, decides what to do with
    it.
    """
    return {
        "id": graph.id,
        "version": graph.version,
        "entry_node": graph.entry_node,
        "nodes": [
            {
                "id": node.id,
                "version": node.version,
                "deterministic": node.deterministic,
                "side_effects": node.side_effects.value,
                "fallback_node_id": node.fallback_node_id,
                "telemetry_tags": list(node.telemetry_tags),
                "has_idempotency_key_fn": node.idempotency_key_fn is not None,
            }
            for node in graph.nodes.values()
        ],
        "edges": [
            {
                "from_node": edge.from_node,
                "to_node": edge.to_node if isinstance(edge.to_node, str) else list(edge.to_node),
                "priority": edge.priority,
                "requires_human_approval": edge.requires_human_approval,
                "requires_deterministic_fallback": edge.requires_deterministic_fallback,
            }
            for edge in graph.edges
        ],
    }


def _evaluate(graph: Graph, request: dict[str, Any], services: Any) -> dict[str, Any]:
    node: Node | None = graph.nodes.get(request["node_id"])
    if node is None:
        return {"error": f"no such node {request['node_id']!r}"}
    state = AEFState.model_validate(request["state"])
    context = decode_context(request["context"])
    try:
        delta, route = node.fn(state, context, services)
    except BaseException as exc:  # noqa: BLE001 - the node is agent-authored
        # Reported as data, not raised. The parent decides what a failing node
        # means — fallback, error entry, or failed scenario — because the
        # parent is what the gates read.
        return {"error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(delta, StateDelta):
        return {"error": f"node returned {type(delta).__name__}, expected a StateDelta"}
    return {"delta": json.loads(delta.model_dump_json()), "route": encode_route(route)}


def _edges_for(graph: Graph) -> list[Edge]:  # pragma: no cover - kept for symmetry
    return list(graph.edges)


def _configure(services: Any, settings: Any) -> Any:
    """Everything the parent owns about ONE scenario, in one frame.

    The cassette (ADR 0123), the harness's `PolicyConfig`, the scenario's
    agent id and its pinned clock. All four are the parent's to supply and
    none of them can be read here: the workspace's `aef.yaml` is the
    candidate's to edit, so a worker that built its own policy from it would
    be judged under rules it wrote (ADR 0082).

    The POLICY is why this frame grew. Since ADR 0094 moved the node bodies
    out, `agent_services()` in this process took no policy at all — so a node
    calling `services.policy_engine.evaluate` was judged deny-by-default no
    matter what the base ref configured, and candidate, incumbent and every
    cohort member scored the same 0.0. That is the ADR 0073/0075/0079/0091
    shape for a sixth time, one process boundary further out (ADR 0125).

    Legacy scenarios carry `memory` and `knowledge` over. Recorded snapshots
    instead reset both per scenario (ADR 0205). One worker
    serves the whole corpus so that store-level agent state behaves as it does
    in production, and a fresh store per scenario would quietly undo that.
    Everything else is rebuilt, from `agent_services` — the same one list
    ADR 0091 exists to keep.

    The clock is pinned to the scenario's recorded values so a node that
    reads it gets a recording rather than wall time. Its cursor is
    INDEPENDENT of the parent's: the executor consumes the parent's copy to
    build each `Context.now`, and that consumption happens in another
    process. `Context.now` — the value the contract says a node actually
    uses ("nodes never call the clock themselves", `kernel/contracts.py`) —
    is pinned exactly, by the parent, and unaffected by this.
    """
    from datetime import datetime

    from aef.config.factory import build_retriever
    from aef.config.schema import ContextConfig
    from aef.harness.memory_store import memory_record
    from aef.harness.replay_inputs import RecordedIsolation, replay_memory
    from aef.harness.scenario_runner import policy_config_from_payload
    from aef.providers.cassette_provider import CassetteProvider, RecordedCall
    from aef.services.runtime import agent_services as build_services

    if not isinstance(settings, dict):
        raise WorkerError(f"configure payload must be an object, got {type(settings).__name__}")
    calls = tuple(RecordedCall.from_payload(c) for c in settings.get("model_calls", ()))
    on_miss = str(settings.get("on_miss", "fail"))
    live = settings.get("live")
    inner = None
    if live is not None:
        from aef.config.factory import build_model_provider
        from aef.config.schema import ModelProviderConfig

        # `model_validate`, not two keyword arguments. The parent serialises
        # the base ref's WHOLE `model_provider` block, so this rebuilds the
        # provider the owner configured rather than a two-field approximation
        # of it: `impl: command` keeps its argv template, its output pointer
        # and its `isolation:` assertion, and `fallback:` keeps its chain.
        # While only `{impl, model}` crossed, an `impl: command` config was
        # refused by this schema — correctly, there was no `command:` block —
        # and every scenario failed as `worker refused configuration`, which
        # G2 reported as a behavioural regression (ADR 0158's F-M5-2, closed
        # in ADR 0181).
        inner = build_model_provider(ModelProviderConfig.model_validate(live))
    elif settings.get("provider_isolation") or settings.get("provider_name"):
        inner = RecordedIsolation(
            settings.get("provider_isolation", ()), settings.get("provider_name", "")
        )

    agent_id = settings.get("agent_id")
    memory, knowledge = services.memory, services.knowledge
    if settings.get("initial_memory") is not None:
        # Every captured scenario starts at its own recorded boundary. The
        # previous scenario's writes must not become this one's inputs.
        memory, knowledge = replay_memory(
            tuple(memory_record(r) for r in settings["initial_memory"]),
            str(agent_id),
        )
    clock_values = [datetime.fromisoformat(v) for v in settings.get("clock_values", ())]
    context_config = (
        None
        if settings.get("context_config") is None
        else ContextConfig.model_validate(settings["context_config"])
    )
    return build_services(
        memory=memory,
        knowledge=knowledge,
        model_provider=CassetteProvider(inner, calls, on_miss=on_miss),
        retriever=build_retriever(
            context_config,
            memory=memory,
            knowledge=knowledge,
            agent_id=str(agent_id) if agent_id is not None else None,
        ),
        policy=policy_config_from_payload(settings.get("policy")),
        agent_id=str(agent_id) if agent_id is not None else None,
        **({"clock": _pinned_clock(clock_values)} if clock_values else {}),
    )


def _pinned_clock(values: list[Any]) -> Any:
    """`fixed_clock`'s rule, worker-side: replay, never invent.

    Running past the recording raises rather than returning wall time —
    inventing a timestamp is how a non-deterministic candidate scores as a
    deterministic one.
    """
    remaining = list(values)

    def clock() -> Any:
        if not remaining:
            raise WorkerError(
                "the run asked for more clock values than the recording pinned; "
                "a candidate taking more steps than the recording is a behavioural "
                "difference to report, not a timestamp to invent"
            )
        return remaining.pop(0)

    return clock


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: python -m {__package__}.node_worker <module:factory>", file=sys.stderr)
        return 2

    try:
        graph = load_graph(argv[1])
    except WorkerError as exc:
        sys.stdout.write(_frame({"error": str(exc)}) + "\n")
        sys.stdout.flush()
        return 1

    services = agent_services()
    sys.stdout.write(_frame({"graph": describe(graph)}) + "\n")
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = _unframe(line)
        except Exception as exc:  # noqa: BLE001 - a malformed frame is a failed step
            response: dict[str, Any] = {"error": f"malformed request: {exc}"}
        else:
            if isinstance(request, dict) and "configure" in request:
                try:
                    services = _configure(services, request["configure"])
                except Exception as exc:  # noqa: BLE001 - reported, the parent decides
                    response = {"error": f"configure failed: {type(exc).__name__}: {exc}"}
                else:
                    response = {"configured": True}
            else:
                response = _evaluate(graph, request, services)
        sys.stdout.write(_frame(response) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main(sys.argv))
