"""Phase-4 criterion 1: **shadow execution against live traffic before
promotion eligibility.**

> The candidate runs alongside the incumbent on real input; only its
> DIVERGENCE is recorded; nothing it returns reaches a user.

This is the criterion that most changes the evidence available, because it is
the only one that observes a candidate on traffic the corpus never captured.
The corpus is recorded past; shadow is the present.

## The part that cannot be paraphrased away

Running a candidate on live input means its nodes actually execute. A node
declaring `side_effects=EXTERNAL_CALL` makes that call — for real, on real
input, twice, because the incumbent made it too. "Nothing it returns reaches a
user" says nothing about what it *does* on the way.

So shadow execution here is **refused** for any graph whose candidate nodes
declare a side effect this harness cannot suppress, and suppression is not
invented for the occasion: it is the existing `PolicyEngine`, deny-by-default,
with an empty scope set. A tool call the candidate attempts is denied and
recorded as a denial. A node that reaches around the policy to do I/O directly
is outside what any of this can see, and that is stated rather than covered
over — it is the same boundary G0's static scan has always had.

`ShadowRunner.observe` therefore returns **the incumbent's state, always**.
The candidate's is compared and dropped. There is no code path in which a
caller can accidentally return the shadow result: the type it gets back does
not contain one.

## Only divergence is recorded

Not both traces. A shadow log that stores every candidate output is a second
copy of production data with none of its access controls, and it grows without
bound while answering a question — *did these differ, and where* — that a
diff answers in a fraction of the space.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from aef.harness.container import (
    ContainerRuntime,
    available_runtimes,
    detect_container_runtime,
)
from aef.harness.isolated import NodeWorkerSession, graph_from
from aef.harness.sandbox import NetworkPolicy, SandboxPolicy, SandboxUnavailableError
from aef.kernel.contracts import Services, SideEffect
from aef.kernel.executor import GraphExecutor
from aef.kernel.graph import Graph
from aef.security.tool import PolicyConfig, PolicyEngine
from aef.services.runtime import _EphemeralDurability
from aef.state import AEFState

# Fields compared between the two final states. Deliberately not "everything":
# `checkpoint_seq` and `provenance` differ by construction on any two runs, so
# including them would report every observation as divergent and the signal
# would be noise within one run.
COMPARED_FIELDS: tuple[str, ...] = (
    "working_memory",
    "retrieved_context",
    "tool_results",
    "errors",
    "plan",
    "scores",
)


class ShadowError(RuntimeError):
    pass


class UncontainedShadowError(ShadowError):
    """A shadow asked to run without containment, without saying so.

    Fatal rather than a warning, for the reason ADR 0102 gives about
    sandboxes: one that silently provides less than it claims is worse than
    none, because the claim is what gets trusted.
    """


class UnsuppressableSideEffectError(ShadowError):
    """The candidate declares an effect shadowing cannot contain.

    Fatal rather than a warning: the alternative is running it and finding
    out, on live input, which is the one thing shadow execution exists to
    avoid.
    """


@dataclass(frozen=True)
class Divergence:
    """Where the two runs differed. Empty means they agreed."""

    fields: tuple[str, ...] = ()
    incumbent_path: tuple[str, ...] = ()
    candidate_path: tuple[str, ...] = ()
    candidate_failed: str = ""
    policy_denials: int = 0

    @property
    def diverged(self) -> bool:
        return bool(self.fields or self.routing_diverged or self.candidate_failed)

    @property
    def routing_diverged(self) -> bool:
        return self.incumbent_path != self.candidate_path


@dataclass(frozen=True)
class ShadowObservation:
    """One live request, observed.

    `state` is the INCUMBENT's, and it is the only state here. A candidate
    result a caller could reach for is a candidate result that eventually
    reaches a user.
    """

    state: AEFState
    divergence: Divergence
    # What this evidence was gathered under. `SandboxCapabilities` exists for
    # the same reason (ADR 0102): a gate outcome can never be read without
    # knowing the conditions it ran under, and an uncontained shadow
    # observation is a weaker claim that would otherwise be indistinguishable
    # from a contained one.
    contained: bool = False

    @property
    def agreed(self) -> bool:
        return not self.divergence.diverged


@dataclass(frozen=True)
class ShadowReport:
    """Accumulated evidence. What promotion eligibility is read from."""

    observations: int = 0
    divergences: int = 0
    candidate_failures: int = 0
    diverging_fields: dict[str, int] = field(default_factory=dict)
    # Counted, not flagged. A report is only as strong as its weakest
    # observation, and averaging that away is how a mixed run reads as a clean
    # one — so the count is kept and `contained` is derived from it.
    uncontained_observations: int = 0

    @property
    def contained(self) -> bool:
        """True only if there IS evidence and all of it was contained.

        Zero observations is not containment. The same reasoning as
        `divergence_rate`: an empty report claiming `contained=True` asserts a
        property of evidence that does not exist, and "never ran" would again
        be indistinguishable from the good case (reproduced).
        """
        return self.observations > 0 and self.uncontained_observations == 0

    @property
    def divergence_rate(self) -> float:
        # Zero observations is not zero divergence. Returning 0.0 would make
        # "never ran" indistinguishable from "always agreed", and the second
        # is the one that earns promotion.
        if self.observations == 0:
            raise ShadowError(
                "no observations: a divergence rate over zero live requests is not 0.0, it "
                "is undefined, and reporting it as 0.0 makes 'never ran' look like 'always "
                "agreed'"
            )
        return self.divergences / self.observations

    def with_observation(self, observation: ShadowObservation) -> ShadowReport:
        fields = dict(self.diverging_fields)
        for name in observation.divergence.fields:
            fields[name] = fields.get(name, 0) + 1
        return ShadowReport(
            observations=self.observations + 1,
            divergences=self.divergences + (0 if observation.agreed else 1),
            candidate_failures=self.candidate_failures
            + (1 if observation.divergence.candidate_failed else 0),
            diverging_fields=fields,
            uncontained_observations=self.uncontained_observations
            + (0 if observation.contained else 1),
        )


def _suppressed_services(base: Services) -> Services:
    """The candidate's services: everything readable, nothing actable.

    `PolicyConfig()` with no allowed scopes is the engine's own
    deny-by-default (constraint #6), reused rather than reimplemented. A
    bespoke "shadow mode" flag would be a second security decision to keep in
    agreement with the first, which is how the two service lists drifted
    (ADR 0091).
    """
    return Services(
        model_provider=base.model_provider,
        memory=base.memory,
        # Passed through, exactly like `memory`, and for the divergence reason
        # rather than a permissive one: a candidate reading an EMPTY wiki while
        # the incumbent reads a full one would diverge for a harness reason and
        # report it as a candidate defect — which is what this function exists
        # to prevent. The consequence is stated rather than hidden: a shadow's
        # consolidate node writes into the live knowledge store. That is not a
        # NEW exposure, it is `memory`'s existing one one layer up, since every
        # entry is derived from records the shadow's reflect node already wrote
        # there. Suppression denies TOOL CALLS; it never claimed to contain
        # direct store writes (ADR 0105).
        knowledge=base.knowledge,
        retriever=base.retriever,
        evaluator=base.evaluator,
        critic=base.critic,
        judge=base.judge,
        tracer=base.tracer,
        tools=base.tools,
        policy_engine=PolicyEngine(PolicyConfig()),
        optimizer=base.optimizer,
        # Each observation starts from the supplied state, not the live
        # run's checkpoint history. Reusing that history either overwrites
        # the incumbent or rejects a legitimate differing candidate under
        # the immutable run/sequence contract (ADR 0209). Keep candidate
        # checkpoints private and discard them after the comparison.
        durability=_EphemeralDurability(),
        # NOT inherited. An approval the incumbent was granted is an approval
        # for the incumbent's call, and handing it to a candidate lets the
        # shadow cross a gate the owner opened for something else.
        hitl_approvals=frozenset(),
        clock=base.clock,
    )


def assert_shadowable(graph: Graph) -> None:
    """Refuse a graph whose effects shadowing cannot contain."""
    offenders = [
        node.id for node in graph.nodes.values() if node.side_effects is SideEffect.MUTATING
    ]
    if offenders:
        raise UnsuppressableSideEffectError(
            f"node(s) {', '.join(sorted(offenders))} declare side_effects=mutating. Shadow "
            f"execution runs the candidate on LIVE input, so a mutating node mutates — for "
            f"real, a second time, alongside the incumbent's. The policy engine denies tool "
            f"calls; it cannot un-write a write."
        )


def _field_value(state: AEFState, name: str) -> Any:
    value = getattr(state, name)
    return value.model_dump() if hasattr(value, "model_dump") else value


def _compare(incumbent: AEFState, candidate: AEFState) -> tuple[str, ...]:
    return tuple(
        name
        for name in COMPARED_FIELDS
        if _field_value(incumbent, name) != _field_value(candidate, name)
    )


@dataclass(frozen=True)
class ShadowRunner:
    """Runs both, returns one.

    `max_steps` is passed to both executors from one value so a divergence
    cannot be an artefact of the candidate having been given more room.
    """

    incumbent: Graph
    candidate: Graph
    max_steps: int = 1000
    # Supplied by `contained_candidate_graph`. Its presence is what makes this
    # runner contained — a fact, not an assertion: the session IS the
    # container, so there is nothing to keep in agreement with anything.
    session: NodeWorkerSession | None = None
    # The explicit opt-out. Named for what it costs rather than for what it
    # enables, so nobody sets it without reading it.
    uncontained: bool = False

    @property
    def contained(self) -> bool:
        """Contained only if the session actually runs inside a container.

        REPRODUCED in this fix's adversarial round: `session is not None` let
        any truthy object make the runner report `contained=True` while the
        candidate ran in-process — a false capability report, which is the
        class ADR 0102 exists to prevent. A `NodeWorkerSession` with
        `container=None` is a plain subprocess and is caught by the same
        check.
        """
        return isinstance(self.session, NodeWorkerSession) and self.session.is_contained

    def __post_init__(self) -> None:
        # At construction, not at first request. A runner that can never
        # legally run is a configuration error, and finding out on live
        # traffic is finding out too late (the same reasoning as
        # `SandboxPolicy.__post_init__`).
        #
        # CONTAINMENT IS THE DEFAULT, and the shape is the one
        # `SandboxPolicy` already uses for network isolation: refuse unless
        # the containment is really there, and make running without it
        # something a caller states rather than inherits. The trust case
        # demonstrated why — suppression by `PolicyEngine` denies tool CALLS,
        # and a node that imported `pathlib` and wrote to disk was never
        # making one (ADR 0105).
        if self.session is not None and self.uncontained:
            raise ShadowError(
                "uncontained=True was passed alongside a container session; one of the two "
                "is a mistake, and guessing which would mislabel the evidence"
            )
        if self.session is not None and not isinstance(self.session, NodeWorkerSession):
            raise ShadowError(
                f"session must be a NodeWorkerSession, not {type(self.session).__name__}. "
                f"Containment is read from the session, so an object that merely occupies "
                f"the slot would make this runner claim a boundary it does not have"
            )
        if self.session is not None and not self.session.is_contained:
            raise UncontainedShadowError(
                "the supplied session runs its worker as a plain subprocess, not in a "
                "container: real rlimits and a scrubbed environment, but no filesystem or "
                "network boundary. Pass a session built by `contained_candidate_graph`, or "
                "uncontained=True to say plainly that this run has no boundary."
            )
        if self.session is not None and self.session.closed:
            raise ShadowError(
                "this session is already closed, so every node evaluation would fail as a "
                "dead worker and be recorded as a CANDIDATE divergence. A harness fault "
                "reported as candidate behaviour is the mistake ADR 0074 names"
            )
        if self.session is None and not self.uncontained:
            raise UncontainedShadowError(
                "shadow execution runs a candidate's code on LIVE input, and an in-process "
                "shadow contains only its TOOL CALLS — a node that opens a file directly is "
                "outside the policy engine, demonstrated in the trust case §2.1. Build the "
                "candidate with `contained_candidate_graph(...)` and pass the session it "
                "returns. To run without containment anyway, pass uncontained=True; every "
                "observation will record contained=False and a report containing one is "
                "downgraded for all of them."
            )
        assert_shadowable(self.candidate)

    def observe(self, state: AEFState, services: Services) -> ShadowObservation:
        """Serve `state` from the incumbent; compare the candidate silently."""
        incumbent_result = GraphExecutor(
            self.incumbent.compile(), services, max_steps=self.max_steps
        ).run(state.model_copy(deep=True), record_trace=True)

        shadow_services = _suppressed_services(services)
        failed = ""
        candidate_state: AEFState | None = None
        candidate_path: tuple[str, ...] = ()
        try:
            candidate_result = GraphExecutor(
                self.candidate.compile(), shadow_services, max_steps=self.max_steps
            ).run(state.model_copy(deep=True), record_trace=True)
            candidate_state = candidate_result.final_state
            candidate_path = tuple(r.node_id for r in (candidate_result.trace or ()))
        except Exception as exc:  # noqa: BLE001
            # A candidate that raises is a divergence, not an outage. The
            # incumbent has already produced the answer this request is served
            # from, and letting the shadow's failure propagate would make
            # observing a candidate more dangerous than not observing it.
            failed = f"{type(exc).__name__}: {exc}"

        incumbent_path = tuple(r.node_id for r in (incumbent_result.trace or ()))
        divergence = Divergence(
            fields=_compare(incumbent_result.final_state, candidate_state)
            if candidate_state is not None
            else (),
            incumbent_path=incumbent_path,
            candidate_path=candidate_path,
            candidate_failed=failed,
            policy_denials=sum(
                1 for e in (candidate_state.errors if candidate_state else []) if _is_denial(e)
            ),
        )
        return ShadowObservation(
            state=incumbent_result.final_state,
            divergence=divergence,
            contained=self.contained,
        )


def _is_denial(entry: dict[str, Any]) -> bool:
    from aef.harness.outcome import POLICY_DENIED_KEY

    return bool(entry.get(POLICY_DENIED_KEY))


def contained_candidate_graph(
    entrypoint: str,
    *,
    workdir: Path,
    runtime: ContainerRuntime,
    sandbox: SandboxPolicy | None = None,
    read_only_mounts: dict[str, str] | None = None,
) -> tuple[Graph, NodeWorkerSession]:
    """The candidate, as a graph whose nodes execute INSIDE a container.

    Closes the bypass the trust case demonstrated (§2.1). Suppression by
    `PolicyEngine` denies tool CALLS; a node that imported `pathlib` and wrote
    to disk was never making one, and it wrote. A container has no network and
    a read-only root, so the same node now fails at the filesystem instead of
    succeeding quietly.

    **The inverted control is why this is worth doing rather than just
    sandboxing the whole run.** The parent keeps state, routing and the step
    count; the worker evaluates one node and returns `(delta, route)`. A
    contained candidate therefore cannot forge the final state a shadow
    comparison reads — which matters more here than in the gate, because a
    forged "identical" state hides a divergence and makes the candidate look
    SAFER than it is (ADR 0094).

    Returns the graph and the session. The caller passes BOTH to
    `ShadowRunner` — the session is what makes the runner contained, and it
    must be closed afterwards, because an unclosed one leaves a container
    running (ADR 0093's defect in its third location):

    ```python
    graph, session = contained_candidate_graph(entrypoint, workdir=w, runtime=rt)
    try:
        runner = ShadowRunner(incumbent=incumbent, candidate=graph, session=session)
        ...
    finally:
        session.close()
    ```
    """
    session = NodeWorkerSession(
        entrypoint,
        workdir=workdir,
        sandbox=sandbox or SandboxPolicy(network=NetworkPolicy.ACKNOWLEDGED_UNISOLATED),
        container=runtime,
        read_only_mounts=read_only_mounts,
    )
    try:
        graph = graph_from(session)
        # Still refused, container or not. Containment stops a node reaching
        # the host; it does not stop a MUTATING node mutating whatever it was
        # declared to mutate, and shadowing runs it on live input.
        assert_shadowable(graph)
    except BaseException:
        session.close()
        raise
    return graph, session


# --------------------------------------------------------------------------
# Containment is RESOLVED, not hand-built (ADR 0161)
# --------------------------------------------------------------------------
#
# ADR 0105 made containment the default by REFUSAL: `ShadowRunner` will not
# construct without a container session. That closed the silent bypass and
# left a gap the trust case's second finding still sat in — nothing here ever
# *provided* the container. A caller on a box with a running daemon and a
# built image got the same flat refusal as a caller with neither, and the only
# one-line way forward was `uncontained=True`. The path of least resistance
# was the bypass.
#
# `resolve_containment` closes that: on a box with a runtime and a verified
# image, the container is what you get without asking. Where the runtime or
# the image is missing, `auto` still REFUSES and names which of the two it
# was. It does not fall back on its own — an automatic in-process fallback
# would be strictly weaker than the refusal ADR 0105 shipped, and weakening a
# control to make a run complete is the thing this program does not do.
#
# The fallback is kept, because a mode nobody can reach is a deletion rather
# than a control, and an adopter who cannot build a worker image still needs
# to run a shadow and see what it cost them. It is reached only by an owner
# writing it in `aef.yaml`, and every such run is named on stderr AND recorded
# in the ledger as a security event.


class ContainmentMode(StrEnum):
    """What an owner asked for. The values are `shadow.containment` verbatim.

    Deliberately not a bool. "Contained or not" is what a run REPORTS; what an
    owner CONFIGURES is a policy about a resource that may or may not be
    there, and collapsing the two makes "no image on this box"
    indistinguishable from "we decided not to bother".
    """

    # Contain, or refuse and say what was missing. Never runs uncontained.
    AUTO = "auto"
    # Contain when possible; run in-process when not, loudly. An owner
    # statement, recorded as one.
    FALLBACK = "fallback"
    # Never contain. Also an owner statement, also recorded.
    OFF = "off"


# The named reasons. Constants rather than f-strings at the call site so a
# test can pin the exact words an operator will be shown — a refusal that
# misnames its own cause sends them to fix the wrong thing (ADR 0074).
NO_RUNTIME_REASON = "no container runtime found"
NO_IMAGE_REASON = "no container image configured (shadow.image is unset)"
IMAGE_UNAVAILABLE_PREFIX = "image unavailable: "
OWNER_OPT_OUT_REASON = "owner opted out in aef.yaml: shadow.containment: off"


@dataclass(frozen=True)
class ContainmentDecision:
    """What was asked for, what was obtained, and why they differ.

    `contained` is what actually happened. It is never inferred from `mode`:
    an owner asking for `auto` on a box with no daemon gets a decision that
    says so, and a decision object that quietly reported the request back
    would be the false capability report `SandboxCapabilities` exists to
    prevent (ADR 0102).
    """

    mode: ContainmentMode
    contained: bool
    reason: str
    runtime: ContainerRuntime | None = None
    image: str | None = None

    @property
    def owner_opted_out(self) -> bool:
        """True when the uncontained run was a CHOICE rather than a shortfall.

        Kept apart from `contained` because the two need different answers: a
        shortfall is fixed by installing a runtime or building an image, and a
        choice is fixed only by the owner changing their mind.
        """
        return not self.contained and self.mode is not ContainmentMode.AUTO

    def warning(self) -> str:
        """The line an operator is shown. Empty when there is nothing to warn about."""
        if self.contained:
            return ""
        if self.mode is ContainmentMode.OFF:
            head = "shadow containment is OFF by owner choice"
        else:
            head = "shadow containment FELL BACK to in-process by owner choice"
        return (
            f"aef: {head} — {self.reason}. The candidate's nodes run in this process: the "
            f"policy engine denies its TOOL CALLS and nothing contains a direct file write "
            f"(trust case section 2.1). Every observation records contained=False and the "
            f"whole report is downgraded for all of them."
        )

    def ledger_detail(self) -> dict[str, Any]:
        """The ledger entry's `detail`.

        `security_event` is set on an uncontained run, and nothing here reads
        it — `aef.harness.monitoring.build_digest` counts it, so a fallback
        reaches the owner's weekly digest without anyone having to remember to
        go looking for a stderr line from a week ago.
        """
        detail: dict[str, Any] = {
            "containment": {
                "mode": self.mode.value,
                "contained": self.contained,
                "reason": self.reason,
                "owner_opted_out": self.owner_opted_out,
                "runtime": self.runtime.binary if self.runtime else None,
                "image": self.image,
                "isolation_verified": bool(self.runtime and self.runtime.verified),
            }
        }
        if not self.contained:
            detail["security_event"] = True
        return detail

    def summary_line(self) -> str:
        """One line for a cycle summary. Always says which it was."""
        if self.contained:
            binary = self.runtime.binary if self.runtime else "?"
            return f"shadow containment: contained ({binary}, image {self.image})"
        return f"shadow containment: NOT contained ({self.reason})"


def resolve_containment(
    *,
    image: str | None,
    mode: ContainmentMode,
    binary: str | None = None,
    verify: bool = True,
    detect: Callable[..., ContainerRuntime] | None = None,
    runtimes: Callable[[], tuple[str, ...]] | None = None,
) -> ContainmentDecision:
    """Decide how this shadow run will be contained. Raises under `auto`.

    `mode` is required for the same reason `shadow_for`'s is (ADR 0173): this
    is the function that turns the owner's `shadow.containment` into a
    decision, so a default here is the same silent override one level down.

    `detect` and `runtimes` are injected so the no-runtime and bad-image paths
    can be exercised on a box that HAS both — a fallback nobody has ever seen
    taken is a fallback nobody knows the shape of, and this repo has three
    ADRs about branches that were wrong the first time they ran.

    Bound here rather than in the signature's defaults so that patching the
    module attribute reaches this function too: a default argument evaluated
    at import time would leave `shadow_for` unreachable from a test that has
    no injection point of its own.
    """
    detect = detect or detect_container_runtime
    runtimes = runtimes or available_runtimes

    if mode is ContainmentMode.OFF:
        return ContainmentDecision(
            mode=mode, contained=False, reason=OWNER_OPT_OUT_REASON, image=image
        )

    if not image:
        return _unavailable(mode, NO_IMAGE_REASON, image)
    if not runtimes():
        return _unavailable(mode, NO_RUNTIME_REASON, image)

    try:
        runtime = detect(image, binary=binary, verify=verify)
    except SandboxUnavailableError as exc:
        # Includes the probe that did not behave in both directions. A runtime
        # whose isolation could not be MEASURED is not one this may use:
        # `container.py` refuses to claim what it did not observe, and
        # accepting that refusal as "close enough" here would launder exactly
        # the claim it declined to make.
        return _unavailable(mode, f"{IMAGE_UNAVAILABLE_PREFIX}{exc}", image)

    return ContainmentDecision(
        mode=mode,
        contained=True,
        reason=f"contained by {runtime.binary} with image {image}"
        + ("; isolation verified" if runtime.verified else "; isolation NOT verified"),
        runtime=runtime,
        image=image,
    )


def _unavailable(mode: ContainmentMode, reason: str, image: str | None) -> ContainmentDecision:
    """`auto` refuses; `fallback` falls back, having been told to."""
    if mode is ContainmentMode.AUTO:
        raise UncontainedShadowError(
            f"shadow containment is unavailable: {reason}. shadow.containment is 'auto', "
            f"which contains the candidate or refuses — it does not run it uncontained, "
            f"because an in-process shadow contains only its TOOL CALLS and a node that "
            f"opens a file directly is outside the policy engine (trust case section 2.1). "
            f"Build a worker image and set shadow.image, or set shadow.containment: "
            f"fallback to accept an uncontained shadow here — that choice is recorded in "
            f"the ledger."
        )
    return ContainmentDecision(mode=mode, contained=False, reason=reason, image=image)


def record_containment_decision(
    root: Path,
    decision: ContainmentDecision,
    *,
    proposal_id: str,
    at: datetime | None = None,
) -> None:
    """Put the decision in the audit trail, contained or not.

    Both directions, deliberately. Recording only the fallbacks would make
    "the shadow ran contained" and "no shadow ran at all" the same absence,
    which is the shape `ShadowReport.contained` already refuses over zero
    observations.
    """
    from aef.harness import ledger

    ledger.append(
        root,
        kind=ledger.EventKind.CONTAINMENT,
        at=at or datetime.now(UTC),
        proposal_id=proposal_id,
        summary=decision.summary_line(),
        detail=decision.ledger_detail(),
    )


@dataclass
class Shadow:
    """A shadow runner, the session behind it, and the conditions it runs under.

    The three travel together because reading any one without the others is
    how a gate outcome gets read as stronger than it is. `close()` is not
    optional: an unclosed session leaves a container running (ADR 0093's
    defect in its third location), so this is also a context manager.
    """

    runner: ShadowRunner
    decision: ContainmentDecision
    session: NodeWorkerSession | None = None

    def close(self) -> None:
        if self.session is not None:
            self.session.close()

    def __enter__(self) -> Shadow:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def shadow_for(
    incumbent: Graph,
    *,
    entrypoint: str,
    workdir: Path,
    mode: ContainmentMode,
    image: str | None = None,
    in_process_candidate: Graph | None = None,
    sandbox: SandboxPolicy | None = None,
    read_only_mounts: dict[str, str] | None = None,
    ledger_root: Path | None = None,
    proposal_id: str = "shadow",
    warn: Callable[[str], None] | None = None,
    decision: ContainmentDecision | None = None,
) -> Shadow:
    """The shadow a caller should build. Contained wherever containment exists.

    This is the whole of what "containment on by default" means in code: with
    a runtime and a verified image on the box, a caller whose `aef.yaml` says
    nothing in particular gets a containerised candidate. Without them, `auto`
    refuses and names the missing half.

    **`mode` has no default, deliberately (ADR 0173).** It used to default to
    `ContainmentMode.AUTO`, which read as "the safe default" and was in fact a
    silent override: `shadow.containment` validated in `aef.yaml`, no code
    read it, and an owner who wrote `off` or `fallback` got `auto` anyway —
    the first a container they had declined, the second a refusal instead of
    the fallback that mode exists to give them. Reproduced both ways. The
    default was a promise the signature made and the code broke, which is
    ADR 0101's rule, so it is gone: the caller says which mode, and the one
    place to get it from a config is
    `aef.config.factory.build_containment_mode(config.shadow)` — carried on
    `RunConfig.containment_mode` for anything that already reads an
    `aef.yaml`.

    **Shadow execution still has no production caller** (ADR 0161 said so and
    it is still true; this function is not one). This increment makes the wire
    exist and removes the silent default; it does not add the caller. Adding
    one is a design decision, not a patch: it needs the trust case's §2.1
    conditions on live-input shadowing — a worker image containing the
    adopter's own `aef`, a decision about which live requests may be
    duplicated onto a candidate, and an owner who has read what an
    `EXTERNAL_CALL` node does when it runs twice.

    `in_process_candidate` is used only on the uncontained path, and it is
    required there rather than loaded for you: running a candidate's module
    inside this interpreter is the thing being avoided, and it should be a
    line the caller wrote.
    """
    decision = decision or resolve_containment(image=image, mode=mode)
    emit = warn if warn is not None else _warn_to_stderr
    if not decision.contained:
        emit(decision.warning())
    if ledger_root is not None:
        record_containment_decision(ledger_root, decision, proposal_id=proposal_id)

    if decision.contained:
        if decision.runtime is None:  # pragma: no cover - resolve_containment guarantees it
            raise ShadowError("a contained decision carries no runtime; refusing to guess")
        graph, session = contained_candidate_graph(
            entrypoint,
            workdir=workdir,
            runtime=decision.runtime,
            sandbox=sandbox,
            read_only_mounts=read_only_mounts,
        )
        try:
            runner = ShadowRunner(incumbent=incumbent, candidate=graph, session=session)
        except BaseException:
            session.close()
            raise
        return Shadow(runner=runner, decision=decision, session=session)

    if in_process_candidate is None:
        raise ShadowError(
            f"{decision.reason}: this shadow will run in-process, so the candidate graph "
            f"must be supplied as `in_process_candidate`. It is not imported for you — "
            f"loading a candidate's module into this interpreter is exactly what "
            f"containment prevents, and it should be a line someone wrote on purpose."
        )
    # THE ONLY `uncontained=True` IN `aef/`, and it is unreachable under the
    # default mode: `resolve_containment` raises before returning an
    # uncontained decision unless the owner wrote `fallback` or `off`.
    # `tests/harness/test_contained_shadow.py` pins both halves of that — the
    # single call site, and that `auto` cannot reach it.
    uncontained_runner = ShadowRunner(
        incumbent=incumbent, candidate=in_process_candidate, uncontained=True
    )
    return Shadow(runner=uncontained_runner, decision=decision, session=None)


def _warn_to_stderr(message: str) -> None:
    if message:
        print(message, file=sys.stderr)
