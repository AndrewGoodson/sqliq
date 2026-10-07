"""`Graph` — a versioned, validated, diffable, visualizable collection of
`Node`/`Edge`. Topology is static and declared up front (report §4); the
handful of dynamic-routing decisions a node makes at runtime are checked
against this declared topology by `GraphExecutor`, never trusted blindly.

Mandatory subgraph API per blueprint Part 14: `compile()`, `validate()`,
`diff(other)`, `visualize()`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from aef.kernel.contracts import Edge, Node


class GraphValidationError(ValueError):
    pass


def _mermaid_escape_label(text: str) -> str:
    """Escape text embedded inside a Mermaid quoted label (`["..."]`). `"`
    would otherwise close the label early; Mermaid decodes `#quot;` back to
    a literal double quote when rendered. Newlines/carriage returns would
    otherwise break a flowchart line (one statement per line) — collapsed
    to a space since there's no multi-line-safe quoting for this context."""
    return text.replace('"', "#quot;").replace("\n", " ").replace("\r", " ")


@dataclass(frozen=True)
class Graph:
    id: str
    version: str  # semver, independent per subgraph (blueprint §2.4)
    nodes: Mapping[str, Node]
    edges: Sequence[Edge]
    entry_node: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", MappingProxyType(dict(self.nodes)))
        object.__setattr__(self, "edges", tuple(self.edges))

    def validate(self) -> None:
        errors: list[str] = []
        if self.entry_node not in self.nodes:
            errors.append(f"entry_node {self.entry_node!r} is not a declared node")
        for edge in self.edges:
            if edge.from_node not in self.nodes:
                errors.append(f"edge.from_node {edge.from_node!r} is not a declared node")
            for target in edge.targets:
                if target not in self.nodes:
                    errors.append(f"edge from {edge.from_node!r} routes to unknown node {target!r}")
        for node in self.nodes.values():
            if node.fallback_node_id is not None and node.fallback_node_id not in self.nodes:
                errors.append(
                    f"node {node.id!r} fallback_node_id {node.fallback_node_id!r} "
                    f"is not a declared node"
                )
        if errors:
            raise GraphValidationError("; ".join(errors))

    def edges_from(self, node_id: str) -> list[Edge]:
        """Declared outgoing edges for `node_id`, highest priority first —
        the deterministic tie-break blueprint §2.2 requires. Equal-priority
        edges are returned in the order they were declared in `edges=[...]`
        at construction time: Python's `sorted()` is guaranteed stable, so
        this is a real, well-defined tie-break, not incidental behavior —
        see test_edges_from_ties_preserve_declaration_order."""
        return sorted((e for e in self.edges if e.from_node == node_id), key=lambda e: -e.priority)

    def compile(self) -> CompiledGraph:
        self.validate()
        return CompiledGraph(graph=self)

    def diff(self, other: Graph) -> GraphDiff:
        self_ids, other_ids = set(self.nodes), set(other.nodes)
        nodes_added = frozenset(other_ids - self_ids)
        nodes_removed = frozenset(self_ids - other_ids)
        nodes_changed = frozenset(
            nid
            for nid in (self_ids & other_ids)
            if self.nodes[nid].version != other.nodes[nid].version
        )
        self_edges, other_edges = set(self.edges), set(other.edges)
        return GraphDiff(
            nodes_added=nodes_added,
            nodes_removed=nodes_removed,
            nodes_changed=nodes_changed,
            edges_added=tuple(other_edges - self_edges),
            edges_removed=tuple(self_edges - other_edges),
        )

    def visualize(self) -> str:
        """Mermaid flowchart source — pasted straight into docs/PRs.

        Node ids are declared in code today, not derived from untrusted
        runtime input — but this still generates syntactically-safe output
        regardless of what a node id contains, rather than assuming it's
        always a bare identifier. Reproduced directly: a node id containing
        `"]` followed by more Mermaid syntax used to break out of its own
        label and inject arbitrary extra statements into the diagram (the
        previous version interpolated `node_id` directly as both the raw
        Mermaid node identifier AND inside a quoted label with no
        escaping). See docs/adr/0028.
        """
        safe_ids = {node_id: f"n{i}" for i, node_id in enumerate(self.nodes)}
        lines = ["flowchart TD"]
        for node_id in self.nodes:
            marker = " (entry)" if node_id == self.entry_node else ""
            label = _mermaid_escape_label(f"{node_id}{marker}")
            lines.append(f'  {safe_ids[node_id]}["{label}"]')
        for edge in self.edges:
            label = f"|p{edge.priority}|" if edge.priority else ""
            from_id = safe_ids.get(edge.from_node, edge.from_node)
            for target in edge.targets:
                target_id = safe_ids.get(target, target)
                lines.append(f"  {from_id} -->{label} {target_id}")
        return "\n".join(lines)


@dataclass(frozen=True)
class CompiledGraph:
    graph: Graph


@dataclass(frozen=True)
class GraphDiff:
    nodes_added: frozenset[str] = field(default_factory=frozenset)
    nodes_removed: frozenset[str] = field(default_factory=frozenset)
    nodes_changed: frozenset[str] = field(default_factory=frozenset)
    edges_added: tuple[Edge, ...] = ()
    edges_removed: tuple[Edge, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (
            self.nodes_added
            or self.nodes_removed
            or self.nodes_changed
            or self.edges_added
            or self.edges_removed
        )
