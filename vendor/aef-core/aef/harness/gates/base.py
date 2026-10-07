"""Gate contract and the pipeline that runs them.

**Canonical order: `G0 → G1 → G4 → G5 → G2 → G3`** (04 §2.4). All four cheap
gates run before the expensive corpus re-execution in G2. G4 is deliberately
early: a proposal reaching for its own tests is rejected before it gets to
run them.

Fail-fast, and **nothing is ever applied**. The pipeline evaluates a
candidate; it does not merge it. There is therefore nothing to roll back on
failure — rollback (M6) concerns changes that were already merged.

Every gate is control-plane and **must be deterministic**: a candidate's
acceptance never depends on a model call. The proposer may be
non-deterministic; the judgement of it may not.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from aef.harness.candidate import CandidateVerdict
from aef.harness.git import GitRepo
from aef.harness.sandbox import SandboxPolicy
from aef.harness.zones import ZonePolicy
from aef.observability.base import Tracer

# The order gates run in. A gate absent from this tuple never runs; a gate
# present but unimplemented is simply not registered yet.
CANONICAL_ORDER: tuple[str, ...] = ("G0", "G1", "G4", "G5", "G2", "G3")


class GateOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True)
class GateResult:
    gate: str
    outcome: GateOutcome
    reason: str = ""
    evidence: tuple[str, ...] = ()
    security_event: bool = False

    @property
    def passed(self) -> bool:
        return self.outcome is GateOutcome.PASS


@dataclass(frozen=True)
class GateContext:
    """Everything a gate may read. Deliberately explicit: a gate that reached
    for the filesystem or the environment directly could be influenced by the
    candidate it is judging."""

    repo: GitRepo
    base_ref: str
    head_ref: str
    verdict: CandidateVerdict
    workdir: Path
    zone_policy: ZonePolicy = field(default_factory=ZonePolicy)
    sandbox_policy: SandboxPolicy | None = None
    limits: dict[str, Any] = field(default_factory=dict)
    # Optional: one span per gate run. Injected rather than constructed, for
    # the same reason nodes take a Tracer via Services — a gate that built
    # its own exporter would be reaching outside its inputs.
    tracer: Tracer | None = None


class Gate(ABC):
    id: str

    @abstractmethod
    def run(self, ctx: GateContext) -> GateResult:
        raise NotImplementedError


@dataclass(frozen=True)
class PipelineResult:
    results: tuple[GateResult, ...]

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)

    @property
    def failed_at(self) -> GateResult | None:
        return next((r for r in self.results if not r.passed), None)

    @property
    def security_events(self) -> tuple[GateResult, ...]:
        return tuple(r for r in self.results if r.security_event)

    @property
    def ran(self) -> tuple[str, ...]:
        return tuple(r.gate for r in self.results)


def run_pipeline(gates: list[Gate] | tuple[Gate, ...], ctx: GateContext) -> PipelineResult:
    """Run `gates` in canonical order, stopping at the first failure.

    Ordering is imposed here rather than trusted from the caller's list, so a
    caller cannot — accidentally or otherwise — schedule G2's expensive run
    before the cheap gate that would have rejected the candidate outright.
    """
    unknown = sorted({g.id for g in gates} - set(CANONICAL_ORDER))
    if unknown:
        raise ValueError(f"gate(s) not in the canonical order: {unknown}")

    ordered = sorted(gates, key=lambda g: CANONICAL_ORDER.index(g.id))
    results: list[GateResult] = []
    for gate in ordered:
        result = _run_traced(gate, ctx)
        results.append(result)
        if not result.passed:
            break
    return PipelineResult(results=tuple(results))


def _run_traced(gate: Gate, ctx: GateContext) -> GateResult:
    """Run one gate, converting any raise into a FAIL.

    A gate that raises escaped `run_pipeline` entirely — out of `gate()`, out
    of `cmd_gate`, and out of the process — so the candidate got no verdict
    and the ledger got no `GATED` entry at all. That is a hole in a
    tamper-evident audit trail, and it is candidate-triggerable: a graph
    whose factory fails to load reaches it (ADR 0090).

    FAIL, not skip. A gate that could not judge has not cleared the
    candidate, and the reason names the exception so the operator can tell a
    broken gate from a bad candidate.

    `Exception`, not `BaseException`: a KeyboardInterrupt or SystemExit is
    the operator stopping the run, and swallowing that would make the loop
    hard to stop — which `KillSwitch` exists precisely to avoid.
    """
    try:
        return _run_untraced(gate, ctx)
    except Exception as exc:  # noqa: BLE001 - a raising gate is a failed gate
        return GateResult(
            gate=gate.id,
            outcome=GateOutcome.FAIL,
            reason=(
                f"gate raised {type(exc).__name__}: {exc}. A gate that could not judge has "
                f"not cleared this candidate."
            ),
        )


def _run_untraced(gate: Gate, ctx: GateContext) -> GateResult:
    if ctx.tracer is None:
        return gate.run(ctx)
    with ctx.tracer.span(f"aef.harness.gate.{gate.id}", {"aef.gate.id": gate.id}) as span:
        result = gate.run(ctx)
        span.set_attribute("aef.gate.outcome", result.outcome.value)
        span.set_attribute("aef.gate.security_event", result.security_event)
        if result.reason:
            span.set_attribute("aef.gate.reason", result.reason)
        return result
