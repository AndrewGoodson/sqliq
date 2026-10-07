"""Builds real `ModelProvider` instances from `ModelProviderConfig`.

Scoped deliberately narrow: only `impl: anthropic` has a real adapter
(`aef.providers.anthropic_provider.AnthropicProvider`) today. A full
config-to-`Services` factory (memory/knowledge_graph/tools/policies) needs
a plugin-registry design this repo doesn't have yet — `MemoryConfig`
alone doesn't carry enough information to construct a real `mem0.Memory()`
(no embedder/vector-store/LLM choice in the schema), and there is no tool
registry mapping `tools.allow` name strings to `Tool` objects anywhere.
See docs/adr/0014 for why that's Phase 2 scope, not deferred-by-oversight.

This module exists because `impl: anthropic` specifically *is* fully
buildable today, and leaving it unbuilt was a real bug: `aef run` could
not run this repo's own `examples/hello_agent`, since nothing ever wired
a `model_provider` at all (see docs/adr/0014).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, overload

from aef.config.schema import (
    CONTEXT_IMPLS,
    CommandProviderConfig,
    ContextConfig,
    HaltChannelConfig,
    ModelProviderConfig,
    PoliciesConfig,
    ShadowConfig,
    ToolsConfig,
)
from aef.providers.base import Effort, FallbackProvider, ModelProvider
from aef.security.tool import PolicyConfig
from aef.services.context.base import Retriever
from aef.services.knowledge.base import KnowledgeStore
from aef.services.memory.base import MemoryStore

if TYPE_CHECKING:  # pragma: no cover - typing only, see build_containment_mode
    from aef.harness.monitoring import HaltChannel
    from aef.harness.shadow import ContainmentMode

# `claude_code` first: in the repos this scaffold is built for, the harness
# login is the only credential there is (ADR 0112).
_SUPPORTED_IMPLS = ("claude_code", "codex", "grok", "anthropic", "command")


class UnsupportedProviderImplError(NotImplementedError):
    def __init__(self, impl: str) -> None:
        super().__init__(
            f"no ModelProvider adapter for impl={impl!r} yet; only "
            f"{_SUPPORTED_IMPLS!r} are implemented (see aef/providers/)"
        )


def _build_single(
    impl: str,
    model: str,
    command: CommandProviderConfig | None = None,
    effort: Effort | None = None,
) -> ModelProvider:
    if impl == "claude_code":
        from aef.providers.harness_provider import ClaudeCodeProvider

        return ClaudeCodeProvider(default_model=model, effort=effort)
    if impl == "codex":
        from aef.providers.harness_provider import CodexProvider

        return CodexProvider(default_model=model)
    if impl == "grok":
        from aef.providers.harness_provider import GrokProvider

        return GrokProvider(default_model=model)
    if impl == "command":
        # A harness this repo has never seen, described by the owner rather
        # than guessed at here (ADR 0154). `ModelProviderConfig` already
        # refused an `impl: command` with no block, so this cannot be None.
        from aef.providers.command_provider import CommandProvider

        if command is None:
            raise UnsupportedProviderImplError(impl)
        return CommandProvider(
            argv=command.argv,
            model_argv=command.model_argv,
            system_argv=command.system_argv,
            stdin=command.stdin,
            output=command.output,
            output_pointer=command.output_pointer,
            usage_pointer=command.usage_pointer,
            output_usage_pointer=command.output_usage_pointer,
            # The owner's containment assertion, carried through unverified so
            # `PromptAgentNode` can record it as theirs (ADR 0169).
            isolation=command.isolation,
            default_model=model,
            timeout_s=command.timeout_s,
        )
    if impl == "anthropic":
        # Imported lazily: aef-core's `anthropic` extra is optional (constraint
        # #3's vendor isolation means only providers/ touches the SDK at all),
        # so importing aef.config itself must not require it — only actually
        # building an anthropic-backed provider should.
        from aef.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(effort=effort)
    raise UnsupportedProviderImplError(impl)


@overload
def build_model_provider(config: ModelProviderConfig) -> ModelProvider: ...


@overload
def build_model_provider(config: None) -> None: ...


def build_model_provider(config: ModelProviderConfig | None) -> ModelProvider | None:
    """Builds the primary provider, and wraps it with `FallbackProvider` if
    `config.fallback` is non-empty. Every listed impl (primary and
    fallback) must be supported — an unbuildable fallback fails loudly at
    construction time rather than being silently dropped, matching this
    repo's established default-deny-on-ambiguity discipline (ADR 0010-0013)."""
    # `config.model` validated for months and nothing read it (the ADR 0100
    # shape). It is the provider's default now; a request naming its own
    # model still wins.
    if config is None:
        return None
    providers = [_build_single(config.impl, config.model, config.command, config.effort)]
    providers.extend(
        _build_single(impl, config.model, effort=config.effort) for impl in config.fallback
    )
    if len(providers) == 1:
        return providers[0]
    return FallbackProvider(providers)


def build_policy_config(tools: ToolsConfig, policies: PoliciesConfig) -> PolicyConfig:
    """The runtime policy an adopter's `aef.yaml` asks for.

    Until this existed, `policies.require_hitl_above_risk` and `tools.allow`
    validated and were then ignored — an adopter setting a HITL threshold
    believed it enforced and got the engine's own default instead (ADR 0014,
    ADR 0079).

    **`tools.allow` is a list of SCOPES, not of tool names.** The engine gates
    on `tool.required_scopes`, so an allowlist of names could not authorise
    anything: every tool would still be denied for missing scopes and the
    field would do nothing. Names are the *deny* axis — `policies.forbid` —
    because a name is the right handle for "never this one" and a scope is
    the right handle for "this capability is permitted".

    Deny-by-default survives: an empty `tools.allow` allows nothing, which is
    the same answer an unconfigured engine gives.
    """
    return PolicyConfig(
        allowed_scopes=frozenset(tools.allow),
        forbidden_tool_names=frozenset(policies.forbid),
        require_hitl_above_risk=policies.require_hitl_above_risk,
    )


def build_retriever(
    config: ContextConfig | None,
    *,
    memory: MemoryStore,
    agent_id: str | None = None,
    knowledge: KnowledgeStore | None = None,
) -> Retriever | None:
    """The retriever an `aef.yaml` asks for, or `None` when it asks for none.

    Takes the already-built `MemoryStore` rather than constructing one: a
    retriever reading a DIFFERENT store than the one its agent writes to
    would retrieve nothing and look like an empty memory. Two constructions
    of the same dependency drifting apart is the failure ADR 0091 records.

    `knowledge` is the same story one layer up, and was missing for a whole
    release (MERGE_READY_LOOP A1, closed in ADR 0118): a retriever built from
    `aef.yaml` had no knowledge store, so the consolidated layer was reachable
    only by hand-constructing `Services` — the shape ADR 0101 deleted
    `GraphStore` for.
    """
    if config is None:
        return None
    if config.impl == "memory":
        from aef.services.context.memory_retriever import MemoryRetriever

        # An unset knob is OMITTED rather than passed as a literal, so
        # `MemoryRetriever`'s dataclass defaults remain the single source of
        # every measured default (ADR 0110's boost, ADR 0116's half-life).
        # Writing them out here would be a second copy of a number set by
        # measurement, and two copies of one measurement is the drift ADR 0091
        # records. See ADR 0193 for why the fields exist at all.
        knobs: dict[str, Any] = {}
        if config.knowledge_boost is not None:
            knobs["knowledge_boost"] = config.knowledge_boost
        if config.staleness_half_life is not None:
            knobs["staleness_half_life"] = config.staleness_half_life
        if config.knowledge_min_occurrences is not None:
            knobs["knowledge_min_occurrences"] = config.knowledge_min_occurrences

        return MemoryRetriever(
            memory=memory,
            agent_id=agent_id,
            knowledge=knowledge,
            max_token_budget=config.token_budget,
            **knobs,
        )
    # Unreachable while `ContextConfig` validates against the same set, and
    # kept anyway: the two would otherwise be a pair that must agree with
    # nothing checking that they do (ADR 0091).
    raise UnsupportedRetrieverImplError(config.impl)


class UnsupportedRetrieverImplError(NotImplementedError):
    def __init__(self, impl: str) -> None:
        super().__init__(
            f"no Retriever for context.impl={impl!r}; implemented: "
            f"{', '.join(sorted(CONTEXT_IMPLS))}"
        )


def build_containment_mode(config: ShadowConfig) -> ContainmentMode:
    """`shadow.containment` as the enum the shadow harness runs on (ADR 0161).

    A local import: `aef.config` must not depend on `aef.harness` at module
    scope — the harness reads configs, and a cycle between the two would make
    either unimportable on its own. The string set is checked against the enum
    by `tests/harness/test_contained_shadow.py`, so the two spellings of this
    one security decision cannot drift apart (ADR 0091).
    """
    from aef.harness.shadow import ContainmentMode

    return ContainmentMode(config.containment)


def build_halt_channel(config: HaltChannelConfig | None) -> HaltChannel | None:
    """`halt_channel:` as the thing the loop actually runs (ADR 0195).

    `None` in, `None` out — an absent block means no channel, which is what
    `aef loop digest` reports as `Halt channel configured: NO`. Deliberately
    not a default: a channel this repo invented would be an alarm the owner
    never chose and cannot be reached by, which is worse than the honest `NO`
    it replaces.

    A local import for `build_containment_mode`'s reason: `aef.config` must not
    depend on `aef.harness` at module scope, or neither is importable alone.
    """
    if config is None:
        return None

    from aef.harness.monitoring import HaltChannel

    return HaltChannel(argv=tuple(config.argv), timeout_s=config.timeout_s)
