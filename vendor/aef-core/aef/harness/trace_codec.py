"""JSON codec for execution traces.

`NodeExecutionRecord` carries three things with no JSON form of their own:
`Context` (a frozen dataclass holding a `datetime`), the `END` sentinel, and
`Route`'s union shape. Without a codec there is no corpus, and without a
corpus there is no G2 — so this is the substrate the whole gate pipeline
stands on.

**`Route` is tagged, not bare.** `END` is a singleton object, not a string,
precisely so it can never collide with a node id (`contracts.py`). Encoding
it as `"END"` would reintroduce exactly that collision the moment someone
names a node `END`, and the failure would be a silently wrong route rather
than an error. Every route therefore encodes as `{"kind": ...}`.

**Output is canonical.** Keys sorted, fixed separators, so re-encoding the
same record is byte-identical. Every downstream gate compares serialized
traces; a codec whose output varied run to run would make all of them flake.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from aef.kernel.contracts import END, Context, Route
from aef.kernel.executor import NodeExecutionRecord
from aef.state import AEFState, StateDelta

TRACE_FORMAT_VERSION = "1.0.0"


class TraceCodecError(ValueError):
    """A trace payload that cannot be decoded. Never silently repaired: a
    corpus entry that decodes to something other than what was recorded is
    worse than one that fails loudly."""


def encode_route(route: Route) -> dict[str, Any]:
    if route is END:
        return {"kind": "end"}
    if isinstance(route, tuple):
        return {"kind": "fanout", "ids": list(route)}
    if isinstance(route, str):
        return {"kind": "node", "id": route}
    raise TraceCodecError(f"unencodable route of type {type(route).__name__}: {route!r}")


def decode_route(payload: dict[str, Any]) -> Route:
    kind = payload.get("kind")
    if kind == "end":
        return END
    if kind == "node":
        node_id = payload.get("id")
        if not isinstance(node_id, str):
            raise TraceCodecError(f"route kind 'node' needs a string id, got {node_id!r}")
        return node_id
    if kind == "fanout":
        ids = payload.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise TraceCodecError(f"route kind 'fanout' needs a list of string ids, got {ids!r}")
        return tuple(ids)
    raise TraceCodecError(f"unknown route kind {kind!r}; expected one of end/node/fanout")


def encode_context(context: Context) -> dict[str, Any]:
    return {
        "run_id": context.run_id,
        "graph_version": context.graph_version,
        "trace_id": context.trace_id,
        "node_id": context.node_id,
        "now": context.now.isoformat(),
        "idempotency_key": context.idempotency_key,
        "attempt": context.attempt,
    }


def decode_context(payload: dict[str, Any]) -> Context:
    try:
        return Context(
            run_id=payload["run_id"],
            graph_version=payload["graph_version"],
            trace_id=payload["trace_id"],
            node_id=payload["node_id"],
            now=datetime.fromisoformat(payload["now"]),
            idempotency_key=payload.get("idempotency_key"),
            attempt=payload.get("attempt", 1),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TraceCodecError(f"malformed context payload: {exc}") from exc


def encode_record(record: NodeExecutionRecord) -> dict[str, Any]:
    return {
        "node_id": record.node_id,
        "input_state": record.input_state.model_dump(mode="json"),
        "context": encode_context(record.context),
        "delta": record.delta.model_dump(mode="json"),
        "route": encode_route(record.route),
        "is_fallback": record.is_fallback,
    }


def decode_record(payload: dict[str, Any]) -> NodeExecutionRecord:
    try:
        return NodeExecutionRecord(
            node_id=payload["node_id"],
            input_state=AEFState.model_validate(payload["input_state"]),
            context=decode_context(payload["context"]),
            delta=StateDelta.model_validate(payload["delta"]),
            route=decode_route(payload["route"]),
            is_fallback=payload.get("is_fallback", False),
        )
    except KeyError as exc:
        raise TraceCodecError(f"trace record missing required field {exc}") from exc


def encode_trace(trace: tuple[NodeExecutionRecord, ...]) -> list[dict[str, Any]]:
    return [encode_record(r) for r in trace]


def decode_trace(payload: list[dict[str, Any]]) -> tuple[NodeExecutionRecord, ...]:
    if not isinstance(payload, list):
        raise TraceCodecError(f"trace must be a list of records, got {type(payload).__name__}")
    return tuple(decode_record(r) for r in payload)


def dumps(value: Any) -> str:
    """Canonical JSON: sorted keys, fixed separators, trailing newline.

    Byte-stability is a requirement, not a nicety — every gate downstream
    compares serialized traces, so a codec that emitted keys in dict order
    would make each of them flake intermittently.
    """
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def loads(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise TraceCodecError(f"corpus entry is not valid JSON: {exc}") from exc
