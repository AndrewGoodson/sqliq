"""Milestone 1: the graph that accumulates.

A snapshot forgets. This is the part that remembers — where nodes sat, how
often each path has ever been taken, when each was last taken, and when each
node was first seen at all.

## Where the data comes from, and where it does not

Everything here is derived from `Provenance`, which the executor already writes
for every node execution (`node_id`, `ts`, `trace_id`, `token_cost`). Crucially,
**consecutive provenance entries within one run ARE the edge traversals**: if a
run recorded `classify` then `fetch_invoice`, the edge between them fired. No
new collection, no new field, no instrumentation added to make the picture
prettier — which is HARD-STOP #8.

What is **not** recoverable from `Provenance`: per-node error counts. It has no
error field. So node health reads UNKNOWN from this source rather than being
inferred, and that is a finding to report rather than a gap to paper over.

## The two quantities, kept separate on purpose

The program prompt requires that "never fired" and "stopped firing" not render
the same, and the way to guarantee that is to stop conflating them into one
number:

- `traversals` — cumulative, **never decays**. Drawn as thickness. It answers
  "how much has this path ever been used".
- `last_traversed` — a timestamp, turned into a recency at render time. Drawn
  as brightness. It answers "is this path used NOW".

So a path the agent has abandoned is **thick and dim** — visibly a road that
was once busy. A path never taken is **thin and dashed** — UNKNOWN, not
healthy, not abandoned. A single decayed scalar would have collapsed those two
into the same pixel, which is the whole failure this design avoids.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import Any

MEMORY_FILENAME = "mind-memory.json"
MEMORY_VERSION = 1

# Positions are rounded before they are written. Unrounded floats churn the
# export on every generation, which destroys the byte-stability the export is
# meant to have — and a file that differs every time cannot be diffed, so
# nobody notices the change that mattered. One decimal is well under a pixel
# at any zoom this page renders at.
POSITION_PRECISION = 1

# How recent counts as "live". Not a decay constant — nothing decays here — but
# the window at render time that separates a path in use from one abandoned.
DEFAULT_LIVE_WINDOW_DAYS = 7.0


class MemoryError_(RuntimeError):
    """Named with a trailing underscore: `MemoryError` is a builtin, and
    shadowing it in a module that also does I/O is how an except clause quietly
    starts catching the wrong thing."""


@dataclass(frozen=True)
class NodeMemory:
    node_id: str
    first_seen: datetime
    last_seen: datetime
    total_runs: int = 0
    token_cost: int = 0
    x: float | None = None
    y: float | None = None

    @property
    def placed(self) -> bool:
        """Whether this node has a remembered position.

        A node without one is NEW, and the page relaxes it into place rather
        than dropping it at the origin — the moment a new node appears is the
        most informative frame in the product, so it must be visible rather
        than instantaneous.
        """
        return self.x is not None and self.y is not None

    def with_position(self, x: float, y: float) -> NodeMemory:
        # Reproduced: an infinite or NaN coordinate serialises as a bare
        # `Infinity` / `NaN`, which is not valid JSON. `JSON.parse` throws, the
        # script block dies, and the page renders NOTHING — one bad float takes
        # out the whole graph. `json.dumps` emits these happily by default.
        if not (isfinite(x) and isfinite(y)):
            raise ValueError(
                f"node {self.node_id!r} given a non-finite position ({x}, {y}). "
                f"json.dumps writes bare Infinity/NaN, which JSON.parse rejects — the whole "
                f"page would render empty because of one coordinate."
            )
        return replace(
            self,
            x=round(x, POSITION_PRECISION),
            y=round(y, POSITION_PRECISION),
        )


@dataclass(frozen=True)
class EdgeMemory:
    source: str
    target: str
    traversals: int = 0
    last_traversed: datetime | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.target)

    def days_since(self, now: datetime) -> float | None:
        """None when never traversed — which is the UNKNOWN case, and callers
        must branch on it rather than substituting a large number. A big
        recency and no recency look identical once they are both floats."""
        if self.last_traversed is None:
            return None
        return max(0.0, (now - self.last_traversed).total_seconds() / 86400.0)


@dataclass(frozen=True)
class GraphMemory:
    graph_id: str
    updated_at: datetime
    nodes: Mapping[str, NodeMemory]
    edges: Mapping[tuple[str, str], EdgeMemory]
    version: int = MEMORY_VERSION

    @property
    def total_traversals(self) -> int:
        return sum(e.traversals for e in self.edges.values())

    def new_since(self, at: datetime) -> tuple[str, ...]:
        """Nodes first seen at or after `at`. This is what the page highlights."""
        return tuple(sorted(n.node_id for n in self.nodes.values() if n.first_seen >= at))

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "graph_id": self.graph_id,
            "updated_at": self.updated_at.astimezone(UTC).isoformat(),
            "nodes": [
                {
                    "node_id": n.node_id,
                    "first_seen": n.first_seen.astimezone(UTC).isoformat(),
                    "last_seen": n.last_seen.astimezone(UTC).isoformat(),
                    "total_runs": n.total_runs,
                    "token_cost": n.token_cost,
                    "x": n.x,
                    "y": n.y,
                }
                for n in sorted(self.nodes.values(), key=lambda n: n.node_id)
            ],
            "edges": [
                {
                    "source": e.source,
                    "target": e.target,
                    "traversals": e.traversals,
                    "last_traversed": (
                        e.last_traversed.astimezone(UTC).isoformat()
                        if e.last_traversed is not None
                        else None
                    ),
                }
                for e in sorted(self.edges.values(), key=lambda e: e.key)
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), sort_keys=True, separators=(",", ":"), indent=2)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> GraphMemory:
        version = payload.get("version")
        if version != MEMORY_VERSION:
            # Refused, not migrated — the same call `release.py` and
            # `export.py` make. A reader that accepts an older shape is
            # checking different claims than it believes it is.
            raise MemoryError_(
                f"memory version {version!r} is not {MEMORY_VERSION}; this reader would be "
                f"interpreting a different shape than the one it was written for"
            )
        nodes = {
            n["node_id"]: NodeMemory(
                node_id=n["node_id"],
                first_seen=datetime.fromisoformat(n["first_seen"]),
                last_seen=datetime.fromisoformat(n["last_seen"]),
                total_runs=n.get("total_runs", 0),
                token_cost=n.get("token_cost", 0),
                x=n.get("x"),
                y=n.get("y"),
            )
            for n in payload.get("nodes", [])
        }
        edges = {}
        for e in payload.get("edges", []):
            mem = EdgeMemory(
                source=e["source"],
                target=e["target"],
                traversals=e.get("traversals", 0),
                last_traversed=(
                    datetime.fromisoformat(e["last_traversed"]) if e.get("last_traversed") else None
                ),
            )
            edges[mem.key] = mem
        return cls(
            graph_id=payload["graph_id"],
            updated_at=datetime.fromisoformat(payload["updated_at"]),
            nodes=nodes,
            edges=edges,
        )


def empty(graph_id: str, *, at: datetime) -> GraphMemory:
    return GraphMemory(graph_id=graph_id, updated_at=at, nodes={}, edges={})


def load(state_dir: Path, graph_id: str, *, at: datetime) -> GraphMemory:
    """Read remembered state, or start fresh.

    A missing file is a first run, not an error. A CORRUPT file is an error and
    is raised: silently starting fresh would erase the accumulated history and
    present the result as a graph that has simply never seen much, which is the
    absence-looks-like-health failure one layer down.
    """
    path = state_dir / MEMORY_FILENAME
    if not path.is_file():
        return empty(graph_id, at=at)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryError_(
            f"{path} is unreadable: {exc}. Refusing to start fresh — that would erase the "
            f"accumulated history and redraw it as a graph that has never seen much."
        ) from exc
    memory = GraphMemory.from_payload(payload)
    if memory.graph_id != graph_id:
        raise MemoryError_(
            f"{path} remembers graph {memory.graph_id!r}, not {graph_id!r}. Two graphs "
            f"sharing one memory file would merge their topologies into a picture of "
            f"neither."
        )
    return memory


def save(memory: GraphMemory, state_dir: Path) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / MEMORY_FILENAME
    path.write_text(memory.to_json(), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Folding observations in.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunTrace:
    """One recorded run, as the sequence of nodes it actually executed.

    Built from `Provenance` — `aef trace` already returns exactly this, in
    order. The ORDER is the whole point: consecutive entries are the edges.
    """

    node_ids: Sequence[str]
    at: datetime
    token_costs: Sequence[int] = ()

    def edges(self) -> tuple[tuple[str, str], ...]:
        return tuple(zip(self.node_ids, self.node_ids[1:], strict=False))


def observe(
    memory: GraphMemory,
    traces: Iterable[RunTrace],
    *,
    at: datetime,
    declared_nodes: Iterable[str] = (),
    declared_edges: Iterable[tuple[str, str]] = (),
) -> GraphMemory:
    """Fold recorded runs into remembered state.

    `declared_nodes`/`declared_edges` come from the graph's static topology, so
    a node that exists but has never executed still APPEARS — with zero runs,
    which renders as UNKNOWN. Omitting it would hide exactly the thing an
    operator most needs to see: a node nothing ever reaches.
    """
    nodes = dict(memory.nodes)
    edges = dict(memory.edges)

    for node_id in declared_nodes:
        if node_id not in nodes:
            nodes[node_id] = NodeMemory(node_id=node_id, first_seen=at, last_seen=at)

    for source, target in declared_edges:
        key = (source, target)
        if key not in edges:
            edges[key] = EdgeMemory(source=source, target=target)

    for trace in traces:
        costs = list(trace.token_costs) + [0] * (len(trace.node_ids) - len(trace.token_costs))
        for node_id, cost in zip(trace.node_ids, costs, strict=False):
            existing = nodes.get(node_id)
            if existing is None:
                nodes[node_id] = NodeMemory(
                    node_id=node_id,
                    first_seen=trace.at,
                    last_seen=trace.at,
                    total_runs=1,
                    token_cost=cost,
                )
            else:
                nodes[node_id] = replace(
                    existing,
                    # `min`, not "keep the old one": a trace can arrive out of
                    # order, and first_seen must be the earliest ever observed
                    # or the Evolution view draws the wrong birthday.
                    first_seen=min(existing.first_seen, trace.at),
                    last_seen=max(existing.last_seen, trace.at),
                    total_runs=existing.total_runs + 1,
                    token_cost=existing.token_cost + cost,
                )
        for key in trace.edges():
            existing_edge = edges.get(key)
            if existing_edge is None:
                edges[key] = EdgeMemory(
                    source=key[0], target=key[1], traversals=1, last_traversed=trace.at
                )
            else:
                previous = existing_edge.last_traversed
                edges[key] = replace(
                    existing_edge,
                    traversals=existing_edge.traversals + 1,
                    last_traversed=trace.at if previous is None else max(previous, trace.at),
                )

    return GraphMemory(
        graph_id=memory.graph_id,
        updated_at=at,
        nodes=nodes,
        edges=edges,
        version=memory.version,
    )


def remember_positions(
    memory: GraphMemory, positions: Mapping[str, tuple[float, float]]
) -> GraphMemory:
    nodes = dict(memory.nodes)
    for node_id, (x, y) in positions.items():
        existing = nodes.get(node_id)
        if existing is not None:
            nodes[node_id] = existing.with_position(x, y)
    return replace(memory, nodes=nodes)


def liveness(
    edge: EdgeMemory, *, now: datetime, window_days: float = DEFAULT_LIVE_WINDOW_DAYS
) -> float | None:
    """How live this path is, in [0, 1] — or None if it has never fired.

    None is the UNKNOWN case and callers MUST branch on it. Returning 0.0 for
    "never traversed" would make it identical to "traversed long ago", and
    those are the two states this whole module exists to keep apart.
    """
    days = edge.days_since(now)
    if days is None:
        return None
    if window_days <= 0:
        raise ValueError("window_days must be positive")
    return max(0.0, min(1.0, 1.0 - (days / window_days)))
