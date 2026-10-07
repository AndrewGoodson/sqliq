"""Anthropic adapter for `ModelProvider`. This module (and no other outside
`providers/`/`services/*/adapters/`) is allowed to import the `anthropic`
SDK directly (constraint #3).
"""

from __future__ import annotations

from typing import Any, Protocol, cast

import anthropic

from aef.providers.base import (
    CompletionRequest,
    CompletionResult,
    Effort,
    ModelProvider,
    ModelProviderError,
)


class _AnthropicClient(Protocol):
    """The slice of `anthropic.Anthropic` this adapter actually calls —
    lets tests inject a fake client without touching the network."""

    messages: Any


class AnthropicProvider(ModelProvider):
    name = "anthropic"

    #: Structural, not flag-derived: there is no CLI here and nothing to read
    #: an argv back from. `messages.create` is called with no `tools`
    #: parameter, so the model has none to call and no agent loop to take a
    #: second turn in; an API has no `CLAUDE.md` discovery, no MCP server and
    #: no local process; and the system text is the `system=` parameter
    #: rather than a prefix on the user turn. Every one of those is a
    #: property of the call this module makes, and a `tools=` argument added
    #: here later must retract `no_tools` in the same edit.
    _ISOLATION = frozenset(
        {
            "no_tools",
            "no_mcp",
            "single_turn",
            "no_project_context",
            "no_local_execution",
            "system_role",
        }
    )

    @property
    def isolation(self) -> frozenset[str]:
        return self._ISOLATION

    def __init__(
        self,
        api_key: str | None = None,
        client: _AnthropicClient | None = None,
        effort: Effort | None = None,
    ) -> None:
        self._effort = effort
        self._client: _AnthropicClient = (
            client
            if client is not None
            else cast(_AnthropicClient, anthropic.Anthropic(api_key=api_key))
        )

    def complete(self, request: CompletionRequest) -> CompletionResult:
        # The Messages API has no "tool" role: tool results travel inside a
        # `user` message as `tool_result` content blocks, and
        # `ProviderMessage.content` is a plain string that cannot carry one.
        # Forwarding the role verbatim produced a vendor 400 naming a field
        # the caller never wrote. Refuse it here, by name, before the network.
        unsupported = [m.role for m in request.messages if m.role == "tool"]
        if unsupported:
            raise ModelProviderError(
                "anthropic adapter cannot send role 'tool': the Messages API carries tool "
                "results as `tool_result` blocks inside a user message, which "
                "ProviderMessage's string content cannot express"
            )
        system = "\n".join(m.content for m in request.messages if m.role == "system") or None
        messages = [
            {"role": m.role, "content": m.content} for m in request.messages if m.role != "system"
        ]
        # CompletionRequest.metadata is vendor-neutral (arbitrary str keys);
        # Anthropic's own API only accepts a single "user_id" key under
        # `metadata` (for their abuse-detection tracking). Pass it through
        # when present rather than silently dropping it — every other
        # metadata key is meaningless to this specific vendor and is
        # correctly ignored here, not a place a translation layer can do
        # anything about it.
        user_id = request.metadata.get("user_id")
        anthropic_metadata = {"user_id": user_id} if user_id is not None else None
        # `CompletionRequest.temperature` is deliberately NOT forwarded. Every
        # current Anthropic model (Fable 5/5.1, Opus 5.5/5/4.8/4.7, Sonnet 5)
        # rejects sampling parameters with a 400, so an adapter that sent it
        # could not talk to any of them. The field stays on the vendor-neutral
        # request for adapters whose vendor still honours it.
        # No `thinking` and no `tool_choice`: Opus 5.5 rejects disabled
        # thinking, `budget_tokens` and forced tool use with a 400. Effort is
        # the one thinking control left, and it is sent only when the owner
        # set it — omitted, each model keeps its own default.
        extra: dict[str, Any] = {}
        if self._effort is not None:
            extra["output_config"] = {"effort": self._effort}
        try:
            response = self._client.messages.create(
                model=request.model,
                max_tokens=request.max_tokens,
                system=system,
                messages=messages,
                metadata=anthropic_metadata,
                **extra,
            )
        except anthropic.AnthropicError as exc:
            # Catch the TRUE base, not just APIError: the SDK has
            # AnthropicError subclasses that are NOT APIError (e.g.
            # WorkloadIdentityError from the auth/credentials token-fetch path,
            # RetryableError on retry exhaustion). Catching only APIError let
            # those leak past this adapter as raw vendor types, violating this
            # module's no-vendor-exceptions-escape contract and defeating
            # FallbackProvider (which only catches ModelProviderError, so a
            # leaked vendor exception aborts the whole fallback chain instead
            # of falling through to a healthy provider). See docs/adr/0037.
            raise ModelProviderError(f"anthropic error: {exc}") from exc

        # A safety classifier can decline with HTTP 200, `stop_reason ==
        # "refusal"` and no text. Returned as `content == ""` that is
        # indistinguishable from an empty answer. Raising the adapter's own
        # error is the vendor guide's "opt into a fallback" advice expressed
        # in this repo's mechanism: `FallbackProvider` catches exactly this
        # and tries the next provider; a lone provider fails loudly instead.
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            raise ModelProviderError(
                f"anthropic refusal (category={category!r}): no content returned"
            )
        content = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        return CompletionResult(
            content=content,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason,
        )
