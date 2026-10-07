"""`CassetteProvider` — a `ModelProvider` that replays recorded completions
(ADR 0123).

A corpus scenario whose graph calls a model could not be re-executed by the
gates: the gate sandbox has no credentials, and even where it had one, a live
call answers differently every time, so the score would move for reasons that
have nothing to do with the candidate. Until this existed the only graphs the
corpus could hold were ones that never asked a model anything — and so the
only task the metric could see was "did it raise" (ADR 0113 fixed the scorer;
the corpus still had no content task to score).

The cassette is the fix. Recording wraps the live provider, lets every call
through, and keeps `(request, result)` pairs. Replay serves a request whose
**key** — a stable hash of the messages, the model and the token cap — matches
a recorded call, and never touches the inner provider for a hit. A miss is
the interesting case:

- `on_miss="fail"` raises `ModelProviderError` naming the miss. This is the
  gate's default, and the reason is determinism: a prompt the recording never
  saw is a behavioural difference between candidate and incumbent, and it is
  reported as an errored node — never silently answered live.
- `on_miss="live"` calls the inner provider and records the answer, which is
  how a scenario is recorded in the first place and how a prompt change is
  scored live, deliberately, with the cost stated.

No vendor SDK is imported. This is a wrapper over `ModelProvider`; the inner
provider is whatever the caller built (ADR 0112's harness provider, in the
repos this scaffold is for).

What is NOT in the key, and why: `temperature` — the Anthropic adapter does
not forward it and the harness adapters cannot — and `metadata`, which is
telemetry. Two requests that differ only there are the same question.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from aef.providers.base import (
    CompletionRequest,
    CompletionResult,
    ModelProvider,
    ModelProviderError,
    ProviderMessage,
    Role,
)

OnMiss = Literal["fail", "live"]
ON_MISS_VALUES: tuple[str, ...] = ("fail", "live")

_ROLES: frozenset[str] = frozenset({"system", "user", "assistant", "tool"})


def request_key(request: CompletionRequest) -> str:
    """A stable identity for one question: messages (role and content, in
    order), the model asked for, and the token cap. Recomputed from the
    request every time, never stored as an authority — see
    `RecordedCall.from_payload`."""
    canonical = {
        "messages": [[m.role, m.content] for m in request.messages],
        "model": request.model,
        "max_tokens": request.max_tokens,
    }
    encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CassetteError(ValueError):
    """A malformed recorded call. Raised at LOAD time so a corrupt cassette
    fails the load, not the first scenario that happens to hit it."""


@dataclass(frozen=True)
class RecordedCall:
    """One `(request, result)` pair. The request is kept in full so a person
    reading the corpus can see what was asked, not just a hash of it."""

    messages: tuple[ProviderMessage, ...]
    model: str
    max_tokens: int
    result: CompletionResult

    @property
    def request(self) -> CompletionRequest:
        return CompletionRequest(
            messages=self.messages, model=self.model, max_tokens=self.max_tokens
        )

    @property
    def key(self) -> str:
        return request_key(self.request)

    @classmethod
    def of(cls, request: CompletionRequest, result: CompletionResult) -> RecordedCall:
        return cls(
            messages=tuple(request.messages),
            model=request.model,
            max_tokens=request.max_tokens,
            result=result,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "request": {
                "messages": [{"role": m.role, "content": m.content} for m in self.messages],
                "model": self.model,
                "max_tokens": self.max_tokens,
            },
            "result": {
                "content": self.result.content,
                "model": self.result.model,
                "input_tokens": self.result.input_tokens,
                "output_tokens": self.result.output_tokens,
                "stop_reason": self.result.stop_reason,
                # Added by ADR 0169. Absent in every cassette recorded before
                # it, which `from_payload` reads as 0 / "requested" — the
                # values those recordings implied, since `input_tokens` was
                # the only input number anything kept.
                "cache_read_input_tokens": self.result.cache_read_input_tokens,
                "cache_creation_input_tokens": self.result.cache_creation_input_tokens,
                "model_attribution": self.result.model_attribution,
            },
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RecordedCall:
        try:
            request = payload["request"]
            result = payload["result"]
            messages: list[ProviderMessage] = []
            for m in request["messages"]:
                role = str(m["role"])
                if role not in _ROLES:
                    raise CassetteError(f"recorded message has role {role!r}")
                messages.append(ProviderMessage(role=_as_role(role), content=str(m["content"])))
            call = cls(
                messages=tuple(messages),
                model=str(request["model"]),
                max_tokens=int(request["max_tokens"]),
                result=CompletionResult(
                    content=str(result["content"]),
                    model=str(result["model"]),
                    input_tokens=int(result["input_tokens"]),
                    output_tokens=int(result["output_tokens"]),
                    stop_reason=(
                        None if result.get("stop_reason") is None else str(result["stop_reason"])
                    ),
                    cache_read_input_tokens=int(result.get("cache_read_input_tokens", 0)),
                    cache_creation_input_tokens=int(result.get("cache_creation_input_tokens", 0)),
                    model_attribution=str(result.get("model_attribution", "requested")),
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, CassetteError):
                raise
            raise CassetteError(f"malformed recorded call: {exc}") from exc
        # The stored key is a claim; the request is the record. A cassette
        # whose key does not derive from its own request has been edited by
        # hand, and serving it would answer a different question than the
        # one it says it answers.
        stored = payload.get("key")
        if stored is not None and stored != call.key:
            raise CassetteError(
                f"recorded call key {str(stored)[:12]!r} does not match its request "
                f"({call.key[:12]!r}); the key is derived, never authored"
            )
        return call


def _as_role(role: str) -> Role:
    # Narrowing for the type checker; membership was checked by the caller.
    if role == "system":
        return "system"
    if role == "user":
        return "user"
    if role == "assistant":
        return "assistant"
    return "tool"


def _describe(request: CompletionRequest) -> str:
    first_user = next((m.content for m in request.messages if m.role == "user"), "")
    excerpt = first_user if len(first_user) <= 80 else first_user[:79] + "…"
    return (
        f"key {request_key(request)[:12]}, model {request.model!r}, "
        f"{len(request.messages)} message(s), max_tokens {request.max_tokens}, "
        f"first user text {excerpt!r}"
    )


class CassetteProvider(ModelProvider):
    """Serve recorded completions; on a miss, fail or go live (see module)."""

    name = "cassette"

    def __init__(
        self,
        inner: ModelProvider | None,
        cassette: Iterable[RecordedCall] = (),
        *,
        on_miss: str = "fail",
    ) -> None:
        if on_miss not in ON_MISS_VALUES:
            raise ValueError(f"on_miss must be one of {ON_MISS_VALUES}, got {on_miss!r}")
        self._inner = inner
        self._on_miss: OnMiss = "live" if on_miss == "live" else "fail"
        self._by_key: dict[str, RecordedCall] = {}
        for call in cassette:
            # Last one wins, deterministically — a cassette with two answers
            # to one question is a recording that asked twice; the recorder
            # itself never produces that (a repeat is a hit).
            self._by_key[call.key] = call
        self._recorded: list[RecordedCall] = []
        self.hits = 0
        self.misses = 0

    @property
    def isolation(self) -> frozenset[str]:
        """The inner provider's declaration, forwarded unchanged; nothing at
        all when there is no inner provider.

        A wrapper cannot be more isolated than what it wraps: under
        `on_miss="live"` the inner provider is what actually runs, so its
        properties are the chain's properties, and a cassette that reported
        its own (a replay spawns no process, opens no file) would let a
        recording launder an unisolated backend's guarantees.

        With `inner=None` — the gates' replay-only configuration — this
        returns the empty set, which reads as **no claim**, not as "nothing is
        enforced". A pure replay genuinely executes nothing, but the answers
        it serves were produced by a provider this object no longer holds, and
        inventing that provider's properties is the failure mode ADR 0169
        exists to close. `PromptAgentNode` records the empty set as no claim
        and does not warn on it, so replaying the corpus is unaffected.
        """
        return self._inner.isolation if self._inner is not None else frozenset()

    @property
    def on_miss(self) -> OnMiss:
        return self._on_miss

    @property
    def inner(self) -> ModelProvider | None:
        return self._inner

    @property
    def recorded(self) -> tuple[RecordedCall, ...]:
        """Every call answered LIVE through this provider, in order. Hits are
        not repeated here: what a recording captures is what the model said,
        once per distinct question."""
        return tuple(self._recorded)

    @property
    def calls(self) -> tuple[RecordedCall, ...]:
        """The whole cassette as it stands: what was loaded plus what was
        recorded. This is what a scenario persists."""
        return tuple(self._by_key.values())

    def complete(self, request: CompletionRequest) -> CompletionResult:
        key = request_key(request)
        hit = self._by_key.get(key)
        if hit is not None:
            self.hits += 1
            return hit.result
        self.misses += 1
        if self._on_miss == "fail":
            raise ModelProviderError(
                f"cassette miss ({_describe(request)}): no recorded completion for this "
                f"request and on_miss='fail'. The candidate asked the model something the "
                f"recording never saw — a behavioural difference, reported rather than "
                f"answered live. Score with cassette_miss='live' to record it deliberately."
            )
        if self._inner is None:
            raise ModelProviderError(
                f"cassette miss ({_describe(request)}) and no live provider to fall through "
                f"to: on_miss='live' needs an inner ModelProvider (model_provider.impl in "
                f"aef.yaml, e.g. claude_code — ADR 0112)"
            )
        result = self._inner.complete(request)
        call = RecordedCall.of(request, result)
        self._by_key[key] = call
        self._recorded.append(call)
        return result
