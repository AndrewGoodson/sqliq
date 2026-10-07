"""`ModelProvider` — the stable interface every LLM vendor sits behind.

No vendor SDK is imported here (constraint #3). Concrete adapters
(`aef/providers/anthropic_provider.py`, future openai/gemini/local adapters)
import their vendor SDK and implement this interface; `kernel/` and
`reasoning/` only ever see `ModelProvider`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

Role = Literal["system", "user", "assistant", "tool"]

# How much a current Anthropic model thinks. Opus 5.5 made this the only
# control (thinking cannot be disabled) and moved its default from `high` to
# `medium`, so an owner who wants the old depth has to name it.
Effort = Literal["low", "medium", "high", "xhigh", "max"]

ISOLATION_PROPERTIES: frozenset[str] = frozenset(
    {
        # -- containment: what the call CANNOT do -------------------------
        "no_tools",
        "no_mcp",
        "no_web_search",
        "no_subagents",
        "single_turn",
        "no_project_context",
        "read_only_fs",
        "no_local_execution",
        # -- the channel the system message travels in --------------------
        "system_role",
        "user_turn_persona",
    }
)
"""The vocabulary of `ModelProvider.isolation`. Closed, because an
unrecognised name in an owner's `aef.yaml` is a typo that would otherwise
read as a weaker claim than the one they meant to make.

- `no_tools` — the model is offered no tool at all.
- `no_mcp` — no MCP server is reachable from the call.
- `no_web_search` — no web search / web fetch tool.
- `no_subagents` — the call cannot spawn a subagent.
- `single_turn` — the run is capped at one agent turn.
- `no_project_context` — the operator's own instruction files, skills,
  plugins and hooks do not reach the call.
- `read_only_fs` — the process is sandboxed to a read-only filesystem. It
  may still READ; this is weaker than `no_tools`, never a substitute.
- `no_local_execution` — this provider spawns no local process at all.

`system_role` and `user_turn_persona` are **mutually exclusive** and describe
where a `system` message ends up, not what the call can do:

- `system_role` — the system text is delivered in the backend's own system
  channel (`--system-prompt`, `system=`), which is what ADR 0152's safety
  story rests on: the persona is the system message.
- `user_turn_persona` — the backend has no system channel, so the system text
  is concatenated into the USER turn. Declaring nothing about the channel is
  a third state and means exactly that: no claim. `PromptAgentNode` warns on
  `user_turn_persona` and stays silent on no-claim, because manufacturing a
  claim from an absence is how F4 happened in the first place.

Note that `no_tools` is a claim about *effect*, never about a flag having
been sent. `grok --tools ""` was measured on 1.0.5 to leave every built-in
tool available — the run read a planted file and quoted it back — while
`claude --tools ""` is documented by its own `--help` as "Use \\"\\" to disable
all tools". Same spelling, different semantics; see ADR 0169.
"""


CHANNEL_PROPERTIES: frozenset[str] = frozenset({"system_role", "user_turn_persona"})
"""The mutually exclusive pair inside `ISOLATION_PROPERTIES` that says WHERE a
system message ends up rather than what the call cannot do.

Named as its own set because the two halves of `isolation` do not compose the
same way. Every other property is a **claim** — "this call reaches no tool" —
and a set of claims combines by intersection: a chain enforces only what all
of its members enforce. `user_turn_persona` is a **hazard marker**, and
intersecting hazards erases them, which is the wrong direction (ADR 0179, R4).
"""


def validate_isolation(properties: object, *, where: str = "isolation") -> frozenset[str]:
    """Normalise a declared isolation set, refusing anything unrecognised.

    Deny-by-default applied to a *claim*: an owner who writes `no_tool` gets
    an error rather than a silently smaller set, and nobody downstream has to
    wonder whether an unfamiliar name meant something."""
    if isinstance(properties, str) or not isinstance(properties, (list, tuple, set, frozenset)):
        raise ValueError(
            f"{where} must be a list of property names, got {type(properties).__name__}"
        )
    names = frozenset(str(p) for p in properties)
    unknown = sorted(names - ISOLATION_PROPERTIES)
    if unknown:
        raise ValueError(
            f"{where} names unknown isolation propert{'y' if len(unknown) == 1 else 'ies'} "
            f"{unknown}; known: {sorted(ISOLATION_PROPERTIES)}"
        )
    if {"system_role", "user_turn_persona"} <= names:
        raise ValueError(
            f"{where} declares both 'system_role' and 'user_turn_persona'. A system message "
            f"travels in one channel or the other; declaring both says nothing."
        )
    return names


@dataclass(frozen=True)
class ProviderMessage:
    role: Role
    content: str


@dataclass(frozen=True)
class CompletionRequest:
    messages: tuple[ProviderMessage, ...]
    model: str
    # Caps thinking plus reply together on current models; 1024 could be
    # spent entirely on thinking and return no text. The vendor guide's
    # non-streaming default is ~16000.
    max_tokens: int = 16000
    # Vendor-neutral. The Anthropic adapter does not forward it: current
    # Anthropic models reject sampling parameters outright.
    temperature: float = 1.0
    metadata: dict[str, str] = field(default_factory=dict)


MODEL_ATTRIBUTION_VALUES: tuple[str, ...] = (
    "requested",
    "alias",
    "sole",
    "usage_match",
    "heuristic",
    "unknown",
)
"""How confident `CompletionResult.model` is, in descending order.

- `requested` — the backend reported usage under exactly the name asked for.
- `alias` — the requested name resolved to one dated id (`claude-opus-5` ->
  `claude-opus-5-20260101`).
- `sole` — the backend reported one model and there was nothing to confuse it
  with.
- `usage_match` — several models were billed, none matched the requested
  name, and exactly one row's token counts equal the payload's top-level
  `usage` — which is the answering call's own usage. Deterministic.
- `heuristic` — **a guess.** Several models were billed, none matched a
  requested name (usually because none was requested), and the name was
  picked by output volume. Measured wrong 1 time in 36 (ADR 0169): a judge
  call whose whole reply is `{"quality": 0.9}` writes ~12 output tokens,
  which is fewer than the CLI's own helper model writes, so "the answering
  model writes the answer" is false exactly for the shortest replies.
- `unknown` — no usage map at all; `model` is whatever was requested.

Recorded rather than hidden. A provenance record that names a model is read
as evidence for which model a measurement was taken on, and a guess that
cannot be told apart from a fact is the ADR 0154 defect one level up."""


@dataclass(frozen=True)
class CompletionResult:
    content: str
    model: str
    input_tokens: int
    """The UNCACHED input only, on every backend that separates them.

    This is not the prompt's cost and was mistaken for it. ADR 0126's
    211,470-vs-4,684 comparison is a claim about total context, and this
    field held 2 on the calls that produced it — the rest was in the cache
    counters below, which nothing retained (ADR 0169). Use
    `total_input_tokens` for anything cost- or isolation-shaped.
    """
    output_tokens: int
    stop_reason: str | None = None
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    model_attribution: str = "requested"
    """One of `MODEL_ATTRIBUTION_VALUES`. Defaults to `requested` so a
    provider that asks for a model and gets it says so without ceremony."""

    @property
    def total_input_tokens(self) -> int:
        """Everything the model was shown: uncached + cache reads + cache
        writes. The only input number that means the same thing on a warm
        cache as on a cold one, and therefore the only one an isolation guard
        can be written against (ADR 0154 learned this the hard way when a
        de-isolated provider passed a guard reading `input_tokens` alone)."""
        return self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens


class ModelProviderError(RuntimeError):
    """Raised by a provider adapter on any failure (auth, rate limit, network).

    `FallbackProvider` catches this specifically — adapters must not let
    vendor-specific exception types leak past their own module.
    """


class ModelProvider(ABC):
    """One vendor's model surface, reduced to what the reasoning plane needs."""

    name: str

    @property
    def isolation(self) -> frozenset[str]:
        """What this provider's own call actually enforces — see
        `ISOLATION_PROPERTIES` for the vocabulary.

        This exists because a containment claim was being **stamped** rather
        than evidenced. `aef migrate` wrote "THE PROMPT RUNS; THE AGENT'S
        TOOLS DO NOT ... the harness adapters send `--tools \"\"` with
        `--max-turns 1`" into every generated module, and
        `make_prompt_agent_node` said "nothing in this path can open a file,
        spawn a process or reach a network service". Neither sentence was
        true of every provider that path can be pointed at: `CommandProvider`
        sends whatever an owner's argv template says, `CodexProvider` sends
        neither flag, and `grok --tools ""` was measured to suppress nothing
        (ADR 0169). One claim, five backends, no join.

        **The default is the empty set: no claim.** A provider that has not
        thought about this says nothing rather than inheriting somebody
        else's guarantees, and `PromptAgentNode` records the empty set as
        what it is.
        """
        return frozenset()

    @abstractmethod
    def complete(self, request: CompletionRequest) -> CompletionResult:
        """Synchronous completion call. Raises `ModelProviderError` on failure."""
        raise NotImplementedError


class FallbackProvider(ModelProvider):
    """Tries each provider in order; falls through to the next on
    `ModelProviderError`. This is the vendor-neutrality mechanism (report
    Recommendation #1 / constraint #3): callers depend only on
    `ModelProvider`, never on which vendor actually answered.
    """

    name = "fallback"

    def __init__(self, providers: list[ModelProvider]) -> None:
        if not providers:
            raise ValueError("FallbackProvider requires at least one provider")
        self._providers = tuple(providers)

    @property
    def isolation(self) -> frozenset[str]:
        """Containment claims INTERSECT; the channel marker does not.

        **The claims.** A chain is only as isolated as its least-isolated
        member, because the caller does not choose who answers: the primary
        failing is exactly when the fallback runs, and a property the fallback
        does not enforce was never enforced for that call. Union would
        describe a call that never happens; the primary's own set would
        describe the happy path and be wrong precisely when it matters. A
        member declaring nothing therefore empties the chain's claims, which
        is the intended behaviour and not an edge case.

        **The channel is not a claim, and intersecting it was inverted** (ADR
        0179, R4). `system_role` and `user_turn_persona` are mutually
        exclusive per provider, so a heterogeneous chain — `claude_code` +
        `codex`, the pair `aef.yaml`'s `fallback:` list is most likely to
        hold — intersected to neither, `persona_role()` returned `"unknown"`,
        and `PromptAgentNode` stayed silent *exactly* in the case the marker
        exists to report. Reproduced: `fallback role=unknown isolation=[]`,
        and through the node with the `codex` member answering, no containment
        warning at all.

        So the hazard wins, which is the same argument the intersection makes,
        applied to a marker that points the other way:

        - any member declaring `user_turn_persona` -> the chain declares it;
        - otherwise, `system_role` only when **every** member declares it —
          a member that says nothing about its channel leaves the chain with
          no channel claim, because manufacturing a claim out of an absence
          is how the unconditional containment sentence survived five
          providers (ADR 0169).

        This is deliberately conservative rather than exact. `isolation` is a
        static declaration read before any call, so "the member that actually
        answered" is not available to it without turning a property into
        per-call mutable state shared across threads. The cost of being
        conservative is a containment note on a run whose persona did travel
        in the system channel; the cost of being exact-or-silent is no note
        on a run whose persona did not. Under ADR 0179 that note is a recorded
        fact rather than an error against the task, so the first cost is
        attention and the second was evidence.
        """
        sets = [p.isolation for p in self._providers]
        if not sets:  # pragma: no cover - __init__ refuses an empty chain
            return frozenset()
        claims = frozenset.intersection(*[s - CHANNEL_PROPERTIES for s in sets])
        if any("user_turn_persona" in s for s in sets):
            return claims | {"user_turn_persona"}
        if all("system_role" in s for s in sets):
            return claims | {"system_role"}
        return claims

    def complete(self, request: CompletionRequest) -> CompletionResult:
        errors: list[tuple[str, Exception]] = []
        for provider in self._providers:
            try:
                return provider.complete(request)
            except ModelProviderError as exc:
                errors.append((provider.name, exc))
        summary = "; ".join(f"{name}: {exc}" for name, exc in errors)
        raise ModelProviderError(f"all providers failed: {summary}")
