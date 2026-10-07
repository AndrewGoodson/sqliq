"""Per-agent configuration schema — report §16, implemented verbatim plus
`evolution` (constraint #7: must stay disabled).

Every sub-model forbids unknown keys so a typo or a stale field fails
loudly at load time instead of being silently ignored (constraint from the
"## Config" section: "fail loudly with a readable error on unknown keys").
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from aef.providers.base import Effort


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# The retrievers that exist. Named here so `ContextConfig` can refuse anything
# else at LOAD time rather than at first use (ADR 0101).
CONTEXT_IMPLS: frozenset[str] = frozenset({"memory"})


class CommandProviderConfig(_StrictModel):
    """`model_provider.impl: command` — a harness described entirely here
    (ADR 0154). See `aef.providers.command_provider` for the security
    properties; this block is the whole interface an owner gets.

    Validation is delegated to `validate_template`, the same function
    `CommandProvider.__init__` calls, so a bad template fails at config-load
    time AND at construction time with the identical message. Two validators
    that could disagree is the drift ADR 0091 records.
    """

    argv: list[str]
    model_argv: list[str] = []
    system_argv: list[str] = []
    stdin: bool = False
    output: str = "stdout"
    output_pointer: str | None = None
    usage_pointer: str | None = None
    output_usage_pointer: str | None = None
    isolation: list[str] = []
    """What the owner ASSERTS this CLI's argv enforces, e.g.
    `[no_tools, single_turn, no_project_context]`. Never verified against the
    binary and recorded in the trace as an assertion (ADR 0169) — because
    `--tools ""` disables every tool on `claude` and disables nothing on
    `grok`, so no amount of reading an unknown template can tell this repo
    which one an owner has. The default is the empty list, which claims
    nothing; `system_role`/`user_turn_persona` are refused here because the
    `{system}` slot already decides them."""
    timeout_s: float = 600.0

    @model_validator(mode="after")
    def _template_must_be_runnable(self) -> CommandProviderConfig:
        # Imported here, not at module scope: `aef.config` must stay
        # importable without the optional vendor extras, which is what
        # `test_importing_aef_config_does_not_require_anthropic` pins.
        # `aef.providers.command_provider` imports no SDK, but the lazy
        # import keeps that guarantee independent of what it grows into.
        from aef.providers.command_provider import (
            OUTPUT_MODES,
            validate_command_isolation,
            validate_template,
        )

        validate_template(
            self.argv,
            model_argv=self.model_argv,
            system_argv=self.system_argv,
            stdin=self.stdin,
        )
        validate_command_isolation(self.isolation)
        if self.output not in OUTPUT_MODES:
            raise ValueError(f"command.output={self.output!r} is not one of {sorted(OUTPUT_MODES)}")
        if self.output == "json_pointer" and self.output_pointer is None:
            raise ValueError(
                "command.output is 'json_pointer' but command.output_pointer is unset: "
                "there is nothing to follow, so every reply would be empty."
            )
        if self.output != "json_pointer" and self.output_pointer is not None:
            raise ValueError(
                f"command.output_pointer is set while command.output is {self.output!r}, "
                f"where it is never read; see docs/adr/0154."
            )
        if self.timeout_s <= 0:
            raise ValueError(f"command.timeout_s must be positive; got {self.timeout_s}")
        return self


class HaltChannelConfig(_StrictModel):
    """`halt_channel:` — the command run when the loop halts (ADR 0195).

    Until this existed `aef loop digest` printed `Halt channel configured: NO`
    on every run, and that line was accurate: a halt at 3am wrote `HALT.md`
    into a checkout nobody was looking at and appended a ledger entry nobody
    was reading. The loop stopped, correctly, and told no one.

    **An argv template, the same shape as `model_provider.impl: command`**
    (ADR 0154), and for the same reason: this repo has no idea how its owner
    is paged. It ships no webhook, no SDK, no dependency and no guess — the
    owner writes the command, and what the command reaches is the owner's
    business. `["/usr/bin/curl", "-fsS", "-XPOST", "-d{reason}", "https://…"]`
    and `["/usr/bin/logger", "-t", "aef", "{reason}"]` are both this block.

    `{reason}` is a slot, not a value: each occurrence is replaced by the
    halt's reason. It is optional, because the same information — plus the
    ledger's last entry, which is what says WHICH candidate and WHEN — is
    written to the command's stdin as one JSON object regardless. A channel
    that wants everything reads stdin; a channel that wants a subject line
    uses the slot.

    **What this block does not do** is as important as what it does, and it is
    stated in the ADR too: it does not retry, it does not queue, it does not
    page anyone by itself, and it is exactly as reliable as the command the
    owner writes. A failure of the channel is recorded and **never masks the
    halt** — the loop is halted either way.
    """

    argv: list[str]
    timeout_s: float = 30.0

    @model_validator(mode="after")
    def _the_command_must_be_runnable(self) -> HaltChannelConfig:
        if not self.argv:
            raise ValueError(
                "halt_channel.argv is empty: there is no program to run, so a halt would "
                "still tell nobody. Remove the block or name a command."
            )
        if not self.argv[0].strip():
            raise ValueError("halt_channel.argv[0] is blank; it must name the program to run")
        if self.timeout_s <= 0:
            raise ValueError(
                f"halt_channel.timeout_s must be positive; got {self.timeout_s}. A channel "
                f"that is never given time to run is an unconfigured one wearing a config."
            )
        return self


_EFFORT_IMPLS = ("claude_code", "anthropic")


class ModelProviderConfig(_StrictModel):
    impl: str
    model: str
    fallback: list[str] = []
    # Only `impl: command` reads this. Present-but-ignored is refused below
    # for the same reason `knowledge_graph.impl` is (ADR 0100).
    command: CommandProviderConfig | None = None
    # How much the model thinks. Unset, each model keeps its own default —
    # which on Claude Opus 5.5 is `medium`, one level below Opus 5's `high`
    # (docs/model-checks/2026-09-23-claude-opus-5-5.md, ADR 0212).
    effort: Effort | None = None

    @model_validator(mode="after")
    def _effort_only_where_something_reads_it(self) -> ModelProviderConfig:
        if self.effort is None:
            return self
        ignored = [i for i in (self.impl, *self.fallback) if i not in _EFFORT_IMPLS]
        if ignored:
            raise ValueError(
                f"model_provider.effort is set but {ignored!r} would ignore it; only "
                f"{_EFFORT_IMPLS!r} have an effort control. Remove `effort` or those impls."
            )
        return self

    @model_validator(mode="after")
    def _command_block_matches_the_impl(self) -> ModelProviderConfig:
        if self.impl == "command" and self.command is None:
            raise ValueError(
                "model_provider.impl is 'command' but no `command:` block is present. "
                "There is no argv template to run; see docs/adr/0154."
            )
        if self.impl != "command" and self.command is not None:
            raise ValueError(
                f"model_provider.command is set while impl is {self.impl!r}, which never "
                f"reads it. A block that validates and is ignored lets an owner believe a "
                f"harness is configured; set impl: command or remove the block."
            )
        if "command" in self.fallback:
            raise ValueError(
                "'command' cannot appear in model_provider.fallback: there is exactly one "
                "`command:` block, so a fallback entry would have no template of its own "
                "and would silently duplicate the primary."
            )
        return self


class MemoryConfig(_StrictModel):
    impl: str
    backend: str | None = None

    @field_validator("impl")
    @classmethod
    def _must_name_constructible_memory(cls, value: str) -> str:
        if value != "in_memory":
            raise ValueError(
                f"memory.impl={value!r} names no runtime builder. Implemented: in_memory. "
                "A config that names an unwired store would silently run against volatile "
                "in-memory storage; see docs/adr/0014."
            )
        return value

    @field_validator("backend")
    @classmethod
    def _reject_ignored_backend(cls, value: str | None) -> str | None:
        if value is not None:
            raise ValueError(
                f"memory.backend={value!r} is not wired and would be ignored. Remove it "
                "until a runtime builder supports durable memory configuration; see "
                "docs/adr/0014."
            )
        return value


class KnowledgeGraphConfig(_StrictModel):
    impl: str
    ontology: str | None = None

    @field_validator("impl")
    @classmethod
    def _no_builder_exists(cls, value: str) -> str:
        # Same treatment `extends` got, for the same reason (ADR 0084): a
        # field that validates any string and is read by nothing lets an
        # owner believe a knowledge graph is attached when no code has ever
        # constructed one. `aef/services/knowledge_graph/` is a typed
        # interface with `NotImplementedError` bodies (Phase 2), so there is
        # nothing for any `impl` to name.
        #
        # The DESIGN reason this is a refusal rather than a builder, stated
        # because the milestone asked for one and not for an excuse: a
        # knowledge-graph adapter needs a retrieval contract the node
        # signature does not yet carry. `Services` hands a node its
        # dependencies, and a KG is only useful if a node can ASK it
        # something — which means a query interface, a result shape the
        # context engine can budget, and a provenance story for retrieved
        # facts. None of those three exist. Building a constructor before
        # them produces a service nothing can call, which is the defect
        # class ADR 0092 named (ADR 0100).
        raise ValueError(
            f"knowledge_graph.impl={value!r} names a builder that does not exist — "
            f"aef/services/knowledge_graph/ is a typed interface with no implementation "
            f"(Phase 2), so this block would be silently ignored. Remove it until a "
            f"knowledge graph is wired; see docs/adr/0100."
        )


class ContextConfig(_StrictModel):
    """Retrieval, the one Phase-2 interface Milestone 3's triage kept.

    `impl` is refused unless it names something that exists, for the same
    reason `knowledge_graph` is (ADR 0100): a block that validates while
    nothing reads it lets an owner believe retrieval is configured.
    """

    impl: str
    # Defaults to the run's own `AEFState.context_budget_tokens` when unset,
    # so the budget has ONE source unless an owner deliberately overrides it
    # for retrieval specifically.
    token_budget: int | None = None

    # The three knowledge-layer knobs, added by ADR 0193. Until then this
    # block carried `impl` and `token_budget` and nothing else, and
    # `build_retriever` passed `max_token_budget` alone — so
    # `knowledge_boost`, `staleness_half_life` and `knowledge_min_occurrences`
    # were reachable ONLY by hand-constructing `Services`, which is the shape
    # ADR 0101 deleted `GraphStore` for. Two of the three have defaults set by
    # measurement (ADR 0110, ADR 0116) and ADR 0193 measures a +0.0470 task
    # metric at a third value of the first; an adopter who read those ADRs
    # could not act on any of it.
    #
    # `None` means "whatever `MemoryRetriever` defaults to", and it is None
    # rather than a repeated literal ON PURPOSE: a measured default written
    # down twice is two numbers that must agree with nothing checking that
    # they do (ADR 0091). `build_retriever` omits an unset field, so the
    # dataclass stays the single source of every default —
    # `tests/config/test_context_knobs.py` asserts exactly that.
    knowledge_boost: float | None = None
    staleness_half_life: int | None = None
    knowledge_min_occurrences: int | None = None

    @field_validator("impl")
    @classmethod
    def _must_name_a_real_retriever(cls, value: str) -> str:
        if value not in CONTEXT_IMPLS:
            raise ValueError(
                f"context.impl={value!r} names no retriever. Implemented: "
                f"{', '.join(sorted(CONTEXT_IMPLS))}. A block naming an unbuilt backend "
                f"would validate and be ignored; see docs/adr/0101."
            )
        return value

    @field_validator("token_budget")
    @classmethod
    def _must_be_positive(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError(
                f"context.token_budget must be positive; got {value}. A zero budget admits "
                f"no chunk, so retrieval would silently return nothing."
            )
        return value

    # The three below mirror `MemoryRetriever.__post_init__`'s refusals at
    # config-load time, with the same reasons, for the reason
    # `CommandProviderConfig` states: an `aef.yaml` that loads clean must not
    # construct an invalid runtime object at first use.

    @field_validator("knowledge_boost")
    @classmethod
    def _boost_must_be_non_negative(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError(
                f"context.knowledge_boost must be non-negative; got {value}. A negative "
                f"boost would rank the best-evidenced entries LAST, which is the opposite "
                f"of what the field's name promises."
            )
        return value

    @field_validator("staleness_half_life")
    @classmethod
    def _half_life_must_be_non_negative(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError(
                f"context.staleness_half_life must be non-negative; got {value}. A "
                f"negative half-life would rank the STALEST entries first. 0 disables the "
                f"demotion; see docs/adr/0116."
            )
        return value

    @field_validator("knowledge_min_occurrences")
    @classmethod
    def _min_occurrences_must_be_at_least_one(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError(f"context.knowledge_min_occurrences must be at least 1; got {value}.")
        return value


class EvaluatorConfig(_StrictModel):
    suites: list[str] = []


class ToolsConfig(_StrictModel):
    allow: list[str] = []
    sandbox: str | None = None
    creds: str | None = None


class PoliciesConfig(_StrictModel):
    require_hitl_above_risk: float = 0.0
    forbid: list[str] = []

    @field_validator("require_hitl_above_risk")
    @classmethod
    def _must_be_in_range(cls, value: float) -> float:
        # Mirrors the runtime guard on aef.security.tool.PolicyConfig at
        # config-load time (a config that loads clean must not construct an
        # invalid runtime PolicyConfig once policies are wired — Phase 2, ADR
        # 0014). A NaN threshold makes `risk > threshold` silently False for
        # every risk; a threshold >= 1.0 makes the gate unreachable since risk
        # is capped at 1.0 (ADR 0025/0035). Bound to [0.0, 1.0).
        if not math.isfinite(value):
            raise ValueError(
                f"require_hitl_above_risk must be finite (no inf/-inf/nan) — got {value!r}"
            )
        if not (0.0 <= value < 1.0):
            raise ValueError(
                f"require_hitl_above_risk must be within [0.0, 1.0) so the HITL gate stays "
                f"reachable (risk is capped at 1.0) — got {value!r}"
            )
        return value


class EvolutionSettings(_StrictModel):
    """Mirrors `aef.evolution.engine.EvolutionConfig`. Kept as a separate,
    plain (non-raising) model here so a config file with `enabled: true`
    fails with a normal, readable pydantic ValidationError at load time
    rather than an exception from deep inside the evolution engine."""

    enabled: bool = False

    @field_validator("enabled")
    @classmethod
    def _must_stay_disabled(cls, value: bool) -> bool:
        if value:
            raise ValueError(
                "evolution.enabled=True is rejected: all seven Phase 4 safety mechanisms "
                "are implemented, but have not been validated against live traffic and real "
                "tenants. Enabling evolution remains an explicit owner decision; see "
                "docs/roadmap.md Phase 4 and docs/trust/promotion-trust-case.md"
            )
        return value


REFLECTION_IMPLS: frozenset[str] = frozenset({"rule_based", "llm"})


class ReflectionConfig(_StrictModel):
    """Which Critic/Judge the reflect node runs (ADR 0115). `llm` needs the
    run's model provider; `aef run` refuses at load if there is none, for the
    same reason `context.impl` refuses an unknown retriever: a block that
    validates while nothing can honour it lets an owner believe it is on."""

    impl: str = "rule_based"

    @field_validator("impl")
    @classmethod
    def _must_name_a_real_reflection(cls, value: str) -> str:
        if value not in REFLECTION_IMPLS:
            raise ValueError(f"reflection.impl={value!r} is not one of {sorted(REFLECTION_IMPLS)}")
        return value


# The containment modes a shadow run can be configured with (ADR 0161). The
# strings are the enum VALUES of `aef.harness.shadow.ContainmentMode`, and a
# test asserts the two sets are identical — two spellings of one security
# decision that could disagree is the drift ADR 0091 records. Named here
# rather than imported so `aef.config` keeps no dependency on `aef.harness`.
CONTAINMENT_MODES: frozenset[str] = frozenset({"auto", "fallback", "off"})


class ShadowConfig(_StrictModel):
    """How a shadow run is contained (ADR 0161).

    `auto` is the default and it does not fall back: a container when a
    runtime and a verified image are available, and a REFUSAL naming what was
    missing when they are not. The two non-default modes are owner statements
    and are recorded as such in the ledger — an owner who accepts an
    uncontained shadow says so in this file, and the run says so back.
    """

    containment: str = "auto"
    # The worker image. There is no default because there is no image this
    # repo can ship: it must contain the adopter's own `aef` and its
    # dependencies (trust case §2.1). `None` under `auto` is a refusal that
    # names the missing image, not a silent downgrade.
    image: str | None = None

    @field_validator("containment", mode="before")
    @classmethod
    def _yaml_off_is_a_word_here(cls, value: object) -> object:
        """YAML 1.1 reads a bare `off` as the boolean false (and `on` as true),
        so `containment: off` — the exact spelling the mode is named by —
        arrives here as `False` and used to fail with a type error that
        never mentioned YAML. `False` can only have been `off`, so it is
        accepted as that mode; `True` has no mode to map to and is refused
        with the fix (quote it). Found by fix wave H2 (ADR 0173)."""
        if value is False:
            return "off"
        if value is True:
            raise ValueError(
                "shadow.containment: YAML read a bare word as the boolean true (`on`/`yes`); "
                'quote the mode — containment: "auto" | "fallback" | "off"'
            )
        return value

    @field_validator("containment")
    @classmethod
    def _must_name_a_real_mode(cls, value: str) -> str:
        if value not in CONTAINMENT_MODES:
            raise ValueError(
                f"shadow.containment={value!r} is not one of {sorted(CONTAINMENT_MODES)}. "
                f"'auto' contains the candidate when a runtime and image are available and "
                f"refuses when they are not; 'fallback' and 'off' accept an uncontained "
                f"shadow and are recorded in the ledger as owner choices."
            )
        return value


class GatesConfig(_StrictModel):
    """What the loop's gates are allowed to do to this repo's owner (ADR 0181).

    One field so far, and it is a security decision rather than a tuning knob.
    """

    live_model_calls: bool = False
    """May a candidate's model calls go LIVE inside the gates' sandbox worker?

    **Default false, and that default is the containment property.** The
    worker is the one place the harness executes code an agent wrote, and its
    environment is scrubbed to `sandbox.DEFAULT_ENV_ALLOWLIST` so that no
    credential is inherited — which is why `claude -p` answers `Not logged in`
    there (ADR 0158's F-M5-3). Setting this true adds
    `sandbox.HARNESS_LOGIN_ENV` to that allowlist for the gate's worker, and
    the consequence is worth stating in the owner's own words: *a candidate's
    code can then spend your harness quota.*

    It is the price of `--cassette-miss live`, which is the only honest way to
    score a changed prompt — a changed prompt is a changed cassette key, so
    replay scores it 0 and that is an artifact, not a verdict (ADR 0123/0126).
    With this false, `--cassette-miss live` is refused by name rather than
    quietly rejecting every candidate: the second is what happened for the
    whole of ADR 0158.

    Recorded on every `gated` ledger event, so a reader of the audit trail can
    see which gate passes ran candidates under the operator's login.
    """


class AgentConfig(_StrictModel):
    extends: str = "_base"

    @field_validator("extends")
    @classmethod
    def _inheritance_is_not_implemented(cls, value: str) -> str:
        # Nothing resolves a base config — not a missing one, and not a
        # present one either. The field validated any string, so
        # `extends: production-base` loaded clean and silently inherited
        # nothing: an owner could believe a shared policy applied when no
        # code had ever read it. Rejecting the non-default is the smallest
        # honest answer until inheritance exists (ADR 0014, ADR 0084).
        if value != "_base":
            raise ValueError(
                f"config inheritance is not implemented, so extends={value!r} would be "
                f"silently ignored — nothing resolves a base config. Inline the settings "
                f"you need, or leave extends at its default '_base'."
            )
        return value

    # Required choice: null explicitly selects an offline, tool-only runtime.
    model_provider: ModelProviderConfig | None
    memory: MemoryConfig
    knowledge_graph: KnowledgeGraphConfig | None = None
    context: ContextConfig | None = None
    reflection: ReflectionConfig = ReflectionConfig()
    evaluator: EvaluatorConfig = EvaluatorConfig()
    tools: ToolsConfig = ToolsConfig()
    policies: PoliciesConfig = PoliciesConfig()
    shadow: ShadowConfig = ShadowConfig()
    gates: GatesConfig = GatesConfig()
    # `None` means no channel, which is what the digest reports as `NO` — an
    # absence stated loudly rather than a default that looks configured
    # (ADR 0195).
    halt_channel: HaltChannelConfig | None = None
    objectives: str
    evolution: EvolutionSettings = EvolutionSettings()

    @model_validator(mode="after")
    def _offline_settings_are_consistent(self) -> AgentConfig:
        if self.model_provider is None:
            if self.reflection.impl == "llm":
                raise ValueError("reflection.impl=llm requires a non-null model_provider")
            if self.gates.live_model_calls:
                raise ValueError("gates.live_model_calls=true requires a non-null model_provider")
        return self
