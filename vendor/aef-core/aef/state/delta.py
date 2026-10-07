"""`StateDelta` — the partial-update half of a node's fixed return type
`StateDelta + Route` (constraint #2). Application is a pure function of
`(StateDelta, AEFState) -> AEFState`: no I/O, no clock reads beyond what the
caller supplies via `Provenance.ts`, fully reproducible for replay.

State is append-mostly (report §5): list-valued fields concatenate, dict-valued
fields merge with the delta taking priority, scalar fields override only when
the delta sets them.
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, Field, field_validator

from aef.state.schema import AEFState, Message, Plan, Provenance


def _copy(value: Any) -> Any:
    """Deep-copy where possible, share where it is not.

    ADR 0087 made `apply` deeply pure with a bare `deepcopy`, and that turned
    a `threading.Lock`, an open file, or a client handle parked in
    `working_memory` from legal into fatal — `AEFState`'s own docstring names
    that field as where per-agent data belongs, and such values worked before.
    Inside `scenario_runner` the raise became a 0.0 for every scenario, for
    candidate and incumbent and cohort alike: the ADR 0075/0079 shape again
    (ADR 0089).

    Purity for the data that can be copied; the previous aliasing behaviour
    for the handles that cannot. A value that cannot be deep-copied also
    cannot be checkpointed, so it was never surviving a round-trip anyway.
    """
    try:
        return deepcopy(value)
    except (TypeError, ValueError):
        return value


def _non_finite_paths(value: Any, path: str = "") -> list[str]:
    """Every location holding a non-finite float, walked depth-first."""
    if isinstance(value, float):
        return [] if math.isfinite(value) else [path or "<root>"]
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            out.extend(_non_finite_paths(item, f"{path}.{key}" if path else str(key)))
        return out
    if isinstance(value, list | tuple):
        out = []
        for index, item in enumerate(value):
            out.extend(_non_finite_paths(item, f"{path}[{index}]"))
        return out
    return []


class StateDelta(BaseModel):
    messages: list[Message] = Field(default_factory=list)
    # Full REPLACE, not merge, when set — unlike the list/dict fields below.
    # A node updating one field of an existing plan (e.g. just its status)
    # must start from `state.plan.model_copy(update={...})`, not construct a
    # fresh `Plan(...)`, or it will silently drop subgoals/reusable_key. See
    # examples/hello_agent/graph.py's summarize_node for the safe pattern.
    plan: Plan | None = None
    working_memory: dict[str, Any] = Field(default_factory=dict)
    context_budget_tokens: int | None = Field(default=None, gt=0)
    retrieved_context: list[dict[str, Any]] = Field(default_factory=list)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    reflections: list[str] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)

    @field_validator("scores")
    @classmethod
    def _scores_must_be_finite(cls, value: dict[str, float]) -> dict[str, float]:
        # AEFState.scores is exactly where a Financial Agent's Sharpe/PF/
        # MaxDD-style domain gates land (report §16) — a division-by-zero
        # in a real metric calculation (e.g. zero volatility) produces inf
        # or nan. `model_dump_json()` silently rewrites both as JSON `null`
        # on the very first checkpoint write (confirmed directly, not
        # assumed — standard JSON has no Infinity/NaN literal), so a
        # corrupted score would look like a perfectly valid, merely-absent
        # one on every subsequent load: a domain gate reading
        # `state.scores.get("sharpe", 0)` would silently see a default
        # instead of an error, potentially flipping a pass/fail decision
        # with no signal anything went wrong. Reject at construction time
        # instead — the node that computed the bad value is what should
        # fail loudly, not a checkpoint several steps later. See docs/adr/0022.
        non_finite = {k: v for k, v in value.items() if not math.isfinite(v)}
        if non_finite:
            raise ValueError(
                f"scores must be finite (no inf/-inf/nan) — got non-finite values for: "
                f"{sorted(non_finite)}. A non-finite score usually means a division by "
                f"zero upstream (e.g. zero volatility in a Sharpe ratio) — fix the "
                f"computation, don't pass the result through."
            )
        return value

    @field_validator("working_memory", "retrieved_context", "tool_results", "errors")
    @classmethod
    def _payloads_must_be_finite(cls, value: Any) -> Any:
        """The ADR 0022 guard, on the fields it did not cover.

        It rejected a non-finite `scores` value and left `working_memory`
        alone — the field `AEFState`'s own docstring names as where per-agent
        data belongs. A NaN there is rewritten to JSON `null` by the first
        checkpoint write, exactly as ADR 0022 describes, so a corrupted value
        reads as a merely-absent one forever after (ADR 0087).
        """
        bad = sorted(_non_finite_paths(value))
        if bad:
            raise ValueError(
                f"non-finite float (inf/-inf/nan) at: {bad}. JSON has no literal for these, "
                f"so the first checkpoint write rewrites them as null and the corruption "
                f"becomes indistinguishable from an absent value. Fix the computation "
                f"upstream (ADR 0022)."
            )
        return value

    def apply(self, state: AEFState) -> AEFState:
        """Return a NEW state. Deeply, not shallowly.

        `model_copy(update=...)` rebuilds the top-level containers and shares
        every nested object. `NodeExecutionRecord.input_state` aliases the
        live state, so a node mutating a nested dict **rewrote trace records
        that are supposed to be history** — and the same delta applied twice
        leaked nested values between the two results and back into the delta
        itself (ADR 0087).

        The existing purity test asserted `state.working_memory == {}` and
        `state.checkpoint_seq`, which passes under full nested aliasing while
        reading as if it pins deep purity.

        `deepcopy` on the agent-supplied payloads only. `messages`,
        `provenance` and `plan` are frozen or immutable-by-contract models;
        the `Any`-typed dicts and lists are where arbitrary mutable structure
        lives.
        """
        return state.model_copy(
            update={
                "messages": [*state.messages, *self.messages],
                "plan": self.plan if self.plan is not None else state.plan,
                "working_memory": _copy({**state.working_memory, **self.working_memory}),
                "context_budget_tokens": (
                    self.context_budget_tokens
                    if self.context_budget_tokens is not None
                    else state.context_budget_tokens
                ),
                "retrieved_context": _copy([*state.retrieved_context, *self.retrieved_context]),
                "tool_results": _copy([*state.tool_results, *self.tool_results]),
                "reflections": [*state.reflections, *self.reflections],
                "scores": {**state.scores, **self.scores},
                "errors": _copy([*state.errors, *self.errors]),
                "provenance": [*state.provenance, *self.provenance],
                "checkpoint_seq": state.checkpoint_seq + 1,
            }
        )
