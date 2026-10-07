"""`make_reflect_node` — the wiring that makes reflection actually reach
memory.

`Critic`/`Judge` produce values; nothing persists them. This factory builds
a real `Node` (fixed signature, DI-only, contract-compliant) that runs both
through `Services` and writes a `MemoryRecord(kind="failure"|"success")`,
which is the CoALA taxonomy the memory module's docstring commits to but
which nothing in the repo previously produced from a reflection.

Grounding the loop this way is the prerequisite for anything downstream
that learns from runs: a proposer can only cite failure/success memory if
something writes it.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from aef.kernel.contracts import END, Context, Node, Route, Services, SideEffect
from aef.reasoning.rule_based_reflection import failure_signals
from aef.services.knowledge.consolidate import RuleBasedConsolidator
from aef.services.memory.base import MemoryKind, MemoryRecord
from aef.state import AEFState, StateDelta


def _failing_nodes(state: AEFState) -> list[str]:
    """Node ids that appended an error, in first-seen order.

    Order preserved rather than sorted: the first failure is usually the
    cause and the rest are consequences, and a set would throw that away.
    Entries with no `node_id` are skipped — an error whose origin was not
    recorded cannot be attributed to a node, and guessing is worse than
    omitting.
    """
    seen: list[str] = []
    for entry in state.errors:
        node_id = entry.get("node_id")
        if isinstance(node_id, str) and node_id and node_id not in seen:
            seen.append(node_id)
    return seen


KNOWLEDGE_SOURCE_PREFIX = "knowledge:"


def retrieved_signatures(state: AEFState) -> list[str]:
    """Signatures of the consolidated lessons this run was shown, read from
    `state.retrieved_context`. Computed from the chunks the retrieve node
    wrote, so the outcome signal ADR 0118 records is about lessons the agent
    actually had in context — not lessons that existed."""
    seen: list[str] = []
    for chunk in state.retrieved_context:
        source = chunk.get("source")
        if not isinstance(source, str) or not source.startswith(KNOWLEDGE_SOURCE_PREFIX):
            continue
        metadata = chunk.get("metadata")
        signature = metadata.get("signature") if isinstance(metadata, dict) else None
        if isinstance(signature, str) and signature and signature not in seen:
            seen.append(signature)
    return seen


LESSON_HEADER = "Lessons from this agent's earlier runs (most relevant first):"

# The keys a chunk's rendered content may carry the lesson under, in the order
# a reader should prefer them. `summary` is the LLM summariser's one prose
# field (ADR 0110); `latest_feedback` is what `_build_entry` copies verbatim
# from the most recent occurrence; `verbal_feedback` is what a raw
# `MemoryRecord` written by `make_reflect_node` holds. Order matters and is
# the only place this module knows anything about those two producers.
_LESSON_TEXT_KEYS = ("summary", "latest_feedback", "verbal_feedback")

# How many characters of a bullet's text survive. A LAST-RESORT guard, not the
# budget: `MemoryRetriever` already enforces `context_budget_tokens`, and
# truncating here would double-count that. It exists because the fallback path
# renders a whole JSON blob when no known key is present, and one unbounded
# blob would swamp the prompt it is meant to inform.
_MAX_BULLET_CHARS = 600


def _lesson_text(chunk: dict[str, object]) -> str:
    """The human-readable lesson inside one retrieved chunk, or "".

    `MemoryRetriever` renders a record's or entry's `content` dict with
    `json.dumps(..., sort_keys=True)`, so the text arrives as JSON. Parsed
    defensively — nothing constrains a chunk's shape, and a chunk this
    function cannot read must degrade to its raw text rather than vanish,
    because a silently dropped lesson is indistinguishable from an empty
    store.
    """
    raw = chunk.get("content")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    text = raw
    if isinstance(parsed, dict):
        for key in _LESSON_TEXT_KEYS:
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
    # One line per bullet: a lesson containing a newline would otherwise break
    # the list this renders into, and a model reads the fragment after the
    # break as an instruction of its own.
    collapsed = " ".join(text.split())
    return collapsed[:_MAX_BULLET_CHARS]


def _lesson_label(chunk: dict[str, object]) -> str:
    """A short provenance tag for one bullet, or "".

    The signature when the chunk is a consolidated entry, the kind otherwise.
    A lesson shown to a model without a hint of where it came from is the
    thing ADR 0101 deleted `GraphStore` for tolerating.
    """
    metadata = chunk.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    signature = metadata.get("signature")
    if isinstance(signature, str) and signature:
        return " ".join(signature.split())
    kind = metadata.get("kind")
    return " ".join(kind.split()) if isinstance(kind, str) and kind else ""


def render_retrieved_context(state: AEFState, *, max_items: int = 5) -> str:
    """The lessons in `state.retrieved_context`, as a short bulleted block a
    node can paste into a prompt — or `""` when there are none.

    **Why this exists.** ADR 0118 gave `Retriever` its first production caller
    (`make_retrieve_node`) and that claim was true and insufficient: the node
    wrote chunks onto `state.retrieved_context` and, until this helper, the
    only code in the package that read them back was
    `retrieved_signatures()`, which computes the helpful/harmful tally. No
    node put a retrieved lesson in front of a model. A caller that writes
    state nobody reads is a caller in name — measured, not argued: with arms
    (a) no-retrieve, (b) raw records and (c) records + knowledge, the six
    summary validation scenarios produced one identical SHA-256 over every
    rendered prompt (ADR 0155).

    Generic on purpose. It reads `AEFState` and nothing else — no services,
    no clock, no randomness — so it is safe inside a `deterministic=True`
    node and belongs to no single agent. The `retrieve` node decides WHEN to
    retrieve, the retriever decides WHAT and under what budget, and this
    decides only how the result reads.

    `max_items` caps the bullets, most-relevant-first: the retriever already
    sorted by score and pruned to `context_budget_tokens`, so this is a
    legibility bound, not a second budget.
    """
    if max_items < 0:
        raise ValueError(
            f"max_items must be non-negative; got {max_items}. A negative cap would "
            f"silently render nothing, which is indistinguishable from an empty store."
        )
    bullets: list[str] = []
    for chunk in state.retrieved_context:
        if len(bullets) >= max_items:
            break
        if not isinstance(chunk, dict):
            continue
        text = _lesson_text(chunk)
        if not text:
            continue
        label = _lesson_label(chunk)
        bullets.append(f"- [{label}] {text}" if label else f"- {text}")
    if not bullets:
        # Empty string, not a header with no bullets: a caller that appends
        # this unconditionally must produce a byte-identical prompt when
        # nothing was retrieved, or every cassette recorded before retrieval
        # existed misses and scores 0 (ADR 0123's replay contract).
        return ""
    return "\n".join([LESSON_HEADER, *bullets])


def make_retrieve_node(
    *,
    node_id: str = "retrieve",
    version: str = "0.1.0",
    route: Route = END,
    query_fn: Callable[[AEFState], str] | None = None,
) -> Node:
    """A `Node` that asks `Services.retriever` for context and writes the
    chunks to `state.retrieved_context` (ADR 0118).

    Until this existed `Retriever` was a declared injection point with no
    production caller: `MemoryRetriever` was "the first thing to enforce
    `context_budget_tokens`" and nothing in a run ever invoked it. The
    budget it enforces is the state's own, so one number governs both what
    the owner configured and what a node receives.

    The query defaults to the objective. Ranking is the retriever's policy;
    this node only decides when retrieval happens (before the work) and
    where the result goes (onto state, as plain dicts, so the reflect node
    can later say which lessons were in context when the run went the way
    it went).
    """
    ask = query_fn if query_fn is not None else (lambda s: s.objective)

    def retrieve_fn(state: AEFState, ctx: Context, services: Services) -> tuple[StateDelta, Route]:
        chunks = services.require_retriever().retrieve(
            ask(state), token_budget=state.context_budget_tokens
        )
        return (
            StateDelta(
                retrieved_context=[
                    {
                        "content": c.content,
                        "source": c.source,
                        "relevance_score": c.relevance_score,
                        "token_estimate": c.token_estimate,
                        "metadata": dict(c.metadata),
                    }
                    for c in chunks
                ]
            ),
            route,
        )

    return Node(
        id=node_id,
        version=version,
        fn=retrieve_fn,
        # Reads a store that other runs write to. Declared non-deterministic so
        # replay trusts the recorded chunks rather than re-querying a store
        # that has since learned more.
        deterministic=False,
        side_effects=SideEffect.IO,
        idempotency_key_fn=lambda s: f"{s.run_id}:{node_id}:{s.checkpoint_seq}",
    )


def make_reflect_node(
    *,
    node_id: str = "reflect",
    version: str = "0.1.0",
    route: Route = END,
    memory_tags: tuple[str, ...] = (),
) -> Node:
    """A `Node` that critiques and judges the current state, appends the
    verbal feedback to `state.reflections`, and persists the whole thing to
    `Services.memory` as failure/success memory.

    `route` is where control goes afterwards; it defaults to `END` so the
    node is usable as a terminal reflection step with no configuration.
    """

    def reflect_fn(state: AEFState, ctx: Context, services: Services) -> tuple[StateDelta, Route]:
        critique = services.require_critic().critique(state)
        judgment = services.require_judge().judge(state)
        # Same convention the Critic used — not a second opinion about what
        # counts as failure (see `failure_signals`).
        kind: MemoryKind = "failure" if failure_signals(state) else "success"

        services.require_memory().write(
            MemoryRecord(
                kind=kind,
                content={
                    "verbal_feedback": critique.verbal_feedback,
                    "grounded_in": list(critique.grounded_in),
                    "score": judgment.score,
                    "rubric": dict(judgment.rubric),
                    "rationale": judgment.rationale,
                    # The node that OBSERVED the failure. Kept, and no longer
                    # the only one recorded — see `failing_nodes` below.
                    "node_id": ctx.node_id,
                    # The nodes that CAUSED it. `ctx.node_id` here is the
                    # reflect node, so a reader of this record could not tell
                    # which node had actually failed — the failing id sat in
                    # `state.errors[i]["node_id"]`, which this node read to
                    # build the feedback text and then discarded.
                    #
                    # A numeric proposer never needed it. A structural one
                    # cannot begin without it: "add a fallback to the flaky
                    # node" requires knowing which node was flaky (ADR 0096).
                    "failing_nodes": _failing_nodes(state),
                    # Which consolidated lessons were IN CONTEXT for this run.
                    # This is what lets the consolidator tally a lesson as
                    # helpful (retrieved, then the run went well) or harmful
                    # (retrieved, and the same failure recurred) — ACE's
                    # signal, computed rather than asked of a model (ADR 0118).
                    "retrieved_signatures": retrieved_signatures(state),
                    "graph_version": ctx.graph_version,
                    "objective": state.objective,
                },
                run_id=state.run_id,
                agent_id=state.agent_id,
                tags=memory_tags,
                created_at=ctx.now,
            )
        )

        # Deliberately does NOT write `judgment.score` into `state.scores`.
        # The Judge reads `state.scores`; feeding its own output back would
        # make a second reflection step judge its previous judgement — a
        # self-referential term the rubric was never written to weigh.
        return StateDelta(reflections=[critique.verbal_feedback]), route

    return Node(
        id=node_id,
        version=version,
        fn=reflect_fn,
        # The *output* is a pure function of state, but the node writes to
        # the memory store. `ReplayEngine` re-executes nodes declared
        # `deterministic=True` (replay.py) — declaring True here would append
        # a duplicate reflection to the store on every replay. Declared False
        # so replay trusts the record instead of repeating the I/O.
        deterministic=False,
        side_effects=SideEffect.IO,
        # `checkpoint_seq` distinguishes successive reflections within one
        # run (a graph may reflect more than once) while staying stable
        # across a retry of the same step, which is exactly the at-least-once
        # resume semantics the key exists for (docs/adr/0010).
        idempotency_key_fn=lambda s: f"{s.run_id}:{node_id}:{s.checkpoint_seq}",
    )


def make_consolidate_node(
    *,
    node_id: str = "consolidate",
    version: str = "0.1.0",
    route: Route = END,
    consolidator: RuleBasedConsolidator | None = None,
) -> Node:
    """A `Node` that folds this agent's repeated failure/success memory into
    `Services.knowledge` (ADR 0110, WikiSkill's middle layer).

    Its own node rather than a hook inside `make_reflect_node`, and the reason
    is scope: reflection is WITHIN-run, consolidation reads ACROSS runs.
    Folding the second into the first would give one node two write paths
    under a single idempotency key, and would make the wiki impossible to omit
    for a graph that does not want one.

    Ordering note, since it is easy to get backwards: this reads what
    reflection has already written, so it belongs AFTER a reflect node. Placed
    before one, it simply consolidates without this run's record — no error,
    which is precisely why it is worth stating here.
    """
    engine = consolidator if consolidator is not None else RuleBasedConsolidator()

    def consolidate_fn(
        state: AEFState, ctx: Context, services: Services
    ) -> tuple[StateDelta, Route]:
        engine.consolidate(
            services.require_memory(),
            services.require_knowledge(),
            # This agent's own memory. `None` would mean "every agent", which
            # is the widening default an adversarial round already found on
            # `MemoryRetriever.agent_id`.
            agent_id=state.agent_id,
        )
        # Deliberately empty. The node's product is in the knowledge store, and
        # copying a summary of it onto state would create a second, staler
        # account of what was consolidated (ADR 0091).
        return StateDelta(), route

    return Node(
        id=node_id,
        version=version,
        fn=consolidate_fn,
        # Reads and writes two stores. Declaring True would make `ReplayEngine`
        # re-execute it, re-running consolidation during replay — harmless only
        # because the store dedupes, which is not a property to lean on.
        deterministic=False,
        side_effects=SideEffect.IO,
        idempotency_key_fn=lambda s: f"{s.run_id}:{node_id}:{s.checkpoint_seq}",
    )
