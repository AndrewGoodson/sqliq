"""`mem0ai` adapter for `MemoryStore`. This module (and no other outside
`providers/`/`services/*/adapters/`) is allowed to import `mem0` directly
(constraint #3).

Mem0's native retrieval is semantic (query text -> ranked matches); AEF's
`MemoryStore.query` is taxonomy/filter-based (kind + run_id + agent_id +
tags, no free-text query). This adapter bridges the gap by round-tripping
AEF's own fields through Mem0's `metadata` dict and falling back to a
tag/kind-derived query string when no semantic query is available — retrieval
quality for tag-only lookups is therefore best-effort, not a guarantee. Real
semantic recall (the reason to use Mem0 at all) still works when callers pass
meaningful tags — verified against a real, fully-local mem0 backend
(fastembed + faiss, zero LLM calls) during development; see docs/adr/0004.

Two hard constraints of real mem0, discovered by testing against a real
backend rather than only the fake client below, are enforced explicitly
here rather than left to surface as a cryptic mem0 traceback:

1. `mem0.Memory.add()`/`.search()` both require at least one of
   `user_id`/`agent_id`/`run_id` — mem0 has no concept of a fully unscoped
   memory. `write()`/`query()` raise a clear `ValueError` up front if a
   `MemoryRecord`/query has neither `agent_id` nor `run_id` set, instead of
   letting mem0 reject it deep inside `search()`.
2. `get()` cannot be implemented as an empty-query semantic search (mem0
   rejects empty/whitespace-only queries outright). Instead, `write()`
   records the native mem0 memory id mem0 itself returns, and `get()` calls
   `mem0.Memory.get(native_id)` directly — a real by-id lookup, not a
   semantic-search workaround.

A client is injected (same pattern as `AnthropicProvider`) rather than
constructed here, since a real `mem0.Memory()` needs an LLM + embedder
configured and this adapter's own translation logic is what needs testing,
not Mem0 itself.
"""

from __future__ import annotations

from typing import Any, Protocol

from aef.services.memory.base import MemoryKind, MemoryRecord, MemoryStore

_AEF_KIND_KEY = "aef_kind"
_AEF_TAGS_KEY = "aef_tags"
_AEF_RECORD_ID_KEY = "aef_record_id"


class Mem0IdentityRequiredError(ValueError):
    """Raised when a MemoryRecord or query has neither agent_id nor run_id
    set. Real mem0 requires every memory to be scoped to at least one of
    user_id/agent_id/run_id — there is no unscoped-memory mode to fall
    back to, so this is a hard constraint of the adapter, not a choice."""


class _Mem0Client(Protocol):
    def add(
        self,
        messages: list[dict[str, str]],
        *,
        user_id: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        infer: bool = True,
    ) -> Any: ...

    def search(
        self,
        query: str,
        *,
        top_k: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> Any: ...

    def get(self, memory_id: str) -> Any: ...


class Mem0Adapter(MemoryStore):
    def __init__(self, client: _Mem0Client) -> None:
        self._client = client
        # Our MemoryRecord.id -> mem0's own native memory id, so get() can
        # do a real by-id lookup instead of a semantic-search workaround.
        self._native_ids: dict[str, str] = {}

    def write(self, record: MemoryRecord) -> str:
        if record.agent_id is None and record.run_id is None:
            raise Mem0IdentityRequiredError(
                f"MemoryRecord {record.id!r} (kind={record.kind!r}) has neither agent_id nor "
                f"run_id set; mem0 requires at least one to scope a memory"
            )

        text = str(record.content.get("text", ""))
        extra_content = {k: v for k, v in record.content.items() if k != "text"}
        metadata = {
            _AEF_KIND_KEY: record.kind,
            _AEF_TAGS_KEY: list(record.tags),
            _AEF_RECORD_ID_KEY: record.id,
            **extra_content,
        }
        response = self._client.add(
            [{"role": "user", "content": text}],
            user_id=record.agent_id,
            run_id=record.run_id,
            metadata=metadata,
            infer=False,  # store AEF's own record verbatim; no LLM fact-extraction pass
        )
        results = response.get("results") if isinstance(response, dict) else None
        if results:
            self._native_ids[record.id] = results[0]["id"]
        return record.id

    def query(
        self,
        kind: MemoryKind,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        tags: tuple[str, ...] = (),
        limit: int = 10,
    ) -> list[MemoryRecord]:
        if limit < 0:
            raise ValueError(f"limit must be non-negative; got {limit}")
        if agent_id is None and run_id is None:
            raise Mem0IdentityRequiredError(
                "query() requires at least one of agent_id or run_id; mem0 has no "
                "unscoped search mode"
            )
        if limit == 0:
            return []

        filters: dict[str, Any] = {}
        if agent_id is not None:
            filters["user_id"] = agent_id
        if run_id is not None:
            filters["run_id"] = run_id
        query_text = " ".join(tags) if tags else kind
        raw_results = self._client.search(query_text, top_k=limit, filters=filters)
        hits = (
            raw_results.get("results", raw_results)
            if isinstance(raw_results, dict)
            else raw_results
        )

        records: list[MemoryRecord] = []
        wanted_tags = set(tags)
        for hit in hits:
            hit_metadata = hit.get("metadata") or {}
            hit_kind = hit_metadata.get(_AEF_KIND_KEY, kind)
            if hit_kind != kind:
                continue
            hit_tags = tuple(hit_metadata.get(_AEF_TAGS_KEY, []))
            if not wanted_tags.issubset(hit_tags):
                continue
            records.append(
                MemoryRecord(
                    kind=hit_kind,
                    content={"text": hit.get("memory", "")},
                    run_id=hit.get("run_id"),
                    agent_id=hit.get("user_id"),
                    tags=hit_tags,
                    id=hit_metadata.get(_AEF_RECORD_ID_KEY, hit.get("id", "")),
                )
            )
        return records[:limit]

    def get(self, record_id: str) -> MemoryRecord | None:
        native_id = self._native_ids.get(record_id)
        if native_id is None:
            return None
        # No try/except here: real mem0.Memory.get() already returns None
        # cleanly for a not-found id (confirmed by reading its source — it
        # checks `if not memory: return None` internally, never raises for
        # that case) — the `if not hit` check below is what "not found"
        # actually means. A blanket except-Exception here previously
        # caught genuine failures too (a downed vector store, an internal
        # mem0 bug, a malformed native_id) and silently returned None for
        # those as well, indistinguishable from a legitimately absent
        # record. See docs/adr/0030.
        hit = self._client.get(native_id)
        if not hit:
            return None
        hit_metadata = hit.get("metadata") or {}
        return MemoryRecord(
            kind=hit_metadata.get(_AEF_KIND_KEY, "working"),
            content={"text": hit.get("memory", "")},
            run_id=hit.get("run_id"),
            agent_id=hit.get("user_id"),
            tags=tuple(hit_metadata.get(_AEF_TAGS_KEY, [])),
            id=record_id,
        )
