"""Executing one recorded scenario, in this process.

**This is no longer how the gates score a candidate.** It was, and the
contract was "run the corpus in a subprocess and print the results" — which
made the candidate the author of the evidence judging it. Three attempts to
secure that channel were each defeated (ADR 0085, 0088, 0093), because the
candidate's code and the reporting code shared an interpreter.

The gates now use `isolated_suite.run_corpus_isolated`: the candidate answers
one node at a time in a worker that never learns what a scenario is, and the
parent concludes (ADR 0094). The stdout marker protocol that used to live
here has been REMOVED rather than left behind, so it cannot be wired back by
someone who reads only its docstring.

What remains is the in-process helper: given a scenario and a graph you
already trust — the incumbent's, a test's — run it and classify. It executes
agent code in the calling process and must not be used to score a candidate.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from aef.config.factory import build_retriever
from aef.harness.checks import CheckError
from aef.harness.corpus import Scenario, fixed_clock
from aef.harness.evaluation import score_of, score_scenario
from aef.harness.graph_loading import import_graph_module, split_entrypoint
from aef.harness.outcome import classify
from aef.harness.replay_inputs import RecordedIsolation, replay_memory
from aef.kernel import GraphExecutor, HumanApprovalRequiredError
from aef.kernel.graph import Graph
from aef.providers.base import ModelProvider
from aef.providers.cassette_provider import CassetteProvider
from aef.security.tool import PolicyConfig
from aef.services.memory.base import MemoryStore
from aef.services.runtime import agent_services
from aef.state import AEFState

# The same rubric `aef run` and `aef loop record` default to. A gate that
# re-executed a recorded scenario under a different rubric would be comparing
# two different measurements and calling the difference a regression.
DEFAULT_RUBRIC: dict[str, float] = {"quality": 1.0}


class EntrypointError(RuntimeError):
    pass


# The exception an adapter raises when a call could not be answered. Adapters
# "must not let vendor-specific exception types leak past their own module"
# (`aef/providers/base.py`), so this one name covers every backend.
MODEL_DEATH_ERRORS = frozenset({"ModelProviderError"})


def _error_type_chain(failure: str) -> tuple[str, ...]:
    """The exception type names in a runner failure string, outermost first.

    Both scoring paths format a failure as `f"{type(exc).__name__}: {exc}"`,
    and the isolated path nests one inside another because the worker frames
    a node's exception the same way before the parent re-raises it — so a
    provider death arrives as
    `"NodeEvaluationError: ModelProviderError: <adapter's message>"`.
    Only the leading segments that look like exception classes are type
    names — a bare identifier in CapWords, the convention every exception in
    this repo and the standard library follows. The message that follows may
    contain anything, colons included, and stops the walk:
    `"...: ModelProviderError: claude: exited 1"` yields the two type names
    and not `claude`.

    What this cannot do is tell a real `ModelProviderError` from a candidate
    that raised one itself, or that spelled the name into another exception's
    message. Nothing reading a string can. The exclusion those feed is
    bounded by a retry and a refusal floor instead of by this function
    (see `G3Improvement`, ADR 0185).
    """
    names: list[str] = []
    for segment in failure.split(": "):
        if segment.isidentifier() and segment[:1].isupper():
            names.append(segment)
            continue
        break
    return tuple(names)


def recorded_node_failure(state: AEFState) -> str | None:
    """Diagnostic exception classes only; never a dead-call exemption.

    A fallback preserves a node error in state instead of raising. Surface
    its type without copying exception bodies (which may contain secrets).
    State is candidate-controlled: this text must not excuse a regression.
    """
    names: list[str] = []
    for error in state.errors:
        kind = error.get("error_type")
        if isinstance(kind, str) and kind.isidentifier() and kind[:1].isupper():
            names.append(kind)
        message = error.get("error")
        if isinstance(message, str):
            names.extend(_error_type_chain(message))
    unique = list(dict.fromkeys(names))
    return "recorded node failure: " + ", ".join(unique) if unique else None


def is_dead_call(failure: str | None, *, cassette_miss: str, live_provider_present: bool) -> bool:
    """Did the model call RAISE, as distinct from answering wrongly?

    Three conditions, and the second and third are both load-bearing.

    1. Some exception in the chain is a `ModelProviderError`.
    2. **The run was live.** Under `cassette_miss="fail"` — the default, and
       what every replayed gate pass uses — no call is ever attempted, so
       nothing can die: a `ModelProviderError` there is `CassetteProvider`
       reporting a MISS, which is a behavioural difference and the exact
       signal the replayed path detects a changed prompt with (ADR 0123
       measured the planted regression as 0.0000 with 36 misses). Calling
       that a dead call would excuse the strongest evidence the gates have.
       So a dead call is only possible on the live path K1 opened.
    3. **A provider was actually there to die.** ADR 0185 gated only on the
       MODE STRING, and a mode string is a request, not a fact: with
       `cassette_miss="live"` and `live_provider=None` the cassette misses
       and raises `ModelProviderError: ... and no live provider to fall
       through to` — nothing was ever called, yet all four scenarios of the
       reproduction came back `dead_call=True retried=True`, the corpus ran
       twice, and G3 read `PASS` on numbers that read `FAIL` when counted
       (reproduced, ADR 0191's F1). A miss with no provider is a MISS: it is
       scored 0, and it is never a dead call. The caller passes this as a
       fact about the run it just performed, rather than letting this
       function re-derive it from the mode it was asked for.

    A candidate can of course raise `ModelProviderError` from its own node
    body under `--cassette-miss live`, and this function cannot tell that
    apart. That is why the exclusion is bounded twice over — one retry, then
    a refusal floor — rather than trusted (see `G3Improvement`, ADR 0185).
    """
    if failure is None or cassette_miss != "live" or not live_provider_present:
        return False
    return bool(MODEL_DEATH_ERRORS & set(_error_type_chain(failure)))


def load_graph(entrypoint: str) -> Graph:
    """`package.module:factory` **or** `path/to/graph.py:factory` -> the
    `Graph` that factory returns.

    The file form is not a convenience: `aef migrate --agent-root
    .claude/agents` (ADR 0152 §4, the only way a persona becomes Zone A)
    writes `.claude/agents/migrated/<module>/graph.py`, and no dotted spelling
    of that path exists. This function used to call `importlib.import_module`
    directly, so on the documented opt-in:

        $ aef loop score '.claude/agents/migrated/reviewer/graph.py:build_graph' ...
        error: the 'package' argument is required to perform a relative import
        for '.claude/agents/migrated/reviewer/graph.py'

    — exit 1, which is `EXIT_REJECTED`. `import_graph_module` is the ONE
    loader `aef run`, `aef loop record` and `node_worker` also use now, so
    both sides of G2 resolve an entrypoint identically (ADR 0177).
    """
    try:
        module_name, attribute = split_entrypoint(entrypoint)
    except ValueError as exc:
        raise EntrypointError(str(exc)) from exc
    try:
        module = import_graph_module(module_name)
    except (ImportError, ValueError, TypeError) as exc:
        # TypeError as well as ImportError: `importlib.import_module` raises
        # `TypeError: the 'package' argument is required...` for a name with a
        # leading dot, and that escaped this handler entirely — out of the
        # gate, out of the CLI — instead of being reported as a bad
        # entrypoint. ValueError is what `import_graph_module` raises when a
        # path form names no file.
        raise EntrypointError(
            f"cannot import {module_name!r} (from entrypoint {entrypoint!r}): "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    try:
        factory = getattr(module, attribute)
    except AttributeError as exc:
        raise EntrypointError(f"{module_name!r} has no attribute {attribute!r}") from exc

    try:
        graph = factory()
    except BaseException as exc:  # noqa: BLE001 - see below
        # BaseException, not Exception. `SystemExit` is a BaseException, so
        # `raise SystemExit(0)` sailed through this handler and exited the
        # process CLEANLY — and a candidate that had already printed forged
        # JSON then owned the runner's entire output. Two added lines scored
        # a broken agent 1.0 instead of 0.0 and passed all six gates
        # (ADR 0085). Agent-authored code does not get to decide that this
        # process is finished.
        raise EntrypointError(f"{entrypoint} raised {type(exc).__name__}: {exc}") from exc
    if not isinstance(graph, Graph):
        raise EntrypointError(f"{entrypoint} returned {type(graph).__name__}, expected a Graph")
    return graph


def policy_config_from_payload(payload: dict[str, Any] | None) -> PolicyConfig:
    """The policy the HARNESS supplies, never one the candidate provides.

    Passed in as data, read by the caller from the BASE REF. Reading
    `aef.yaml` from the workspace here would let a candidate widen its own
    policy by editing a Zone C file — the gate would then be judging it under
    rules it wrote (ADR 0082). An absent payload means deny-by-default, which
    is what an unconfigured production run also gets.
    """
    if not payload:
        return PolicyConfig()
    return PolicyConfig(
        allowed_scopes=frozenset(payload.get("allowed_scopes", ())),
        forbidden_tool_names=frozenset(payload.get("forbidden_tool_names", ())),
        require_hitl_above_risk=float(payload.get("require_hitl_above_risk", 0.0)),
    )


def policy_payload(policy: PolicyConfig | None) -> dict[str, Any] | None:
    """The inverse of `policy_config_from_payload`, for the isolated path.

    The node bodies run in a worker process (ADR 0094), so the policy the
    harness supplies has to cross a pipe to reach the engine those nodes
    consult. `None` stays `None` — deny-by-default is what an unconfigured
    run gets on both sides, and encoding it as an empty object would make
    "no policy" and "an empty policy" indistinguishable on the wire.

    Sorted, because the frame is compared byte-for-byte in tests and a
    frozenset's iteration order is not stable across processes.
    """
    if policy is None:
        return None
    return {
        "allowed_scopes": sorted(policy.allowed_scopes),
        "forbidden_tool_names": sorted(policy.forbidden_tool_names),
        "require_hitl_above_risk": policy.require_hitl_above_risk,
    }


def run_scenario(
    scenario: Scenario,
    graph: Graph,
    policy: PolicyConfig | None = None,
    *,
    cassette_miss: str = "fail",
    live_provider: ModelProvider | None = None,
    memory: MemoryStore | None = None,
) -> dict[str, Any]:
    """One scenario, answering both gates' questions from one execution.

    `memory` — a durable store to record a FAILED OWNER CHECK into as failure
    memory (ADR 0174's producer, made one idempotent function by ADR 0180).
    Default `None` keeps every gate path exactly as it was: a gate that wrote
    to the durable store would let scoring a candidate manufacture the next
    one's evidence, which ADR 0174 refused. Only a caller that owns the store
    (`aef loop score --memory`, the scored half of a measurement) passes one.

    Model calls are served from the scenario's cassette (ADR 0123).
    `cassette_miss="fail"` — the default — makes a request the recording
    never saw an errored node, so the score is deterministic and needs no
    credential; `"live"` sends misses to `live_provider` and the payload's
    `cassette` block says how many, so a live score is never mistaken for a
    replayed one.

    `critic` and `judge` are wired here for the same reason `aef run` and
    `aef loop record` wire them (ADR 0073): **every reflect node requires
    them**, and LOOP.md obligation 2 tells every adopter to add one. This was
    the third construction site and the only one that was missed — and it is
    the one the gates use, so an adopter who satisfied obligation 2 made every
    scenario crash with `ServiceNotConfiguredError`, scoring the candidate,
    the incumbent and all five cohort members 0.0. G3 rejected every candidate
    forever, and `doctor` reported the obligation green (ADR 0075).

    Memory is in-process and thrown away: the gate re-executes recorded
    scenarios to compare behaviour, and writing to the adopter's durable store
    would let a gate run mutate the evidence a later proposal is built from.
    Both services are rule-based and deterministic, which the sandbox requires.
    """
    # One list, shared with `aef run` — see aef/services/runtime.py. Four
    # separate defects were "the gate path lacks a service the node needs"
    # (ADR 0073/0075/0079/0089); the cause each time was drift between two
    # lists nobody compared (ADR 0091).
    inner = live_provider
    if inner is None and (scenario.provider_isolation or scenario.provider_name):
        inner = RecordedIsolation(scenario.provider_isolation, scenario.provider_name)
    cassette = CassetteProvider(inner, scenario.model_calls, on_miss=cassette_miss)
    runtime_memory, knowledge = replay_memory(
        scenario.initial_memory, scenario.initial_state.agent_id
    )
    services = agent_services(
        clock=fixed_clock(scenario),
        policy=policy,
        # Memory is in-process and thrown away: writing to the adopter's
        # durable store would let a gate run mutate the evidence a later
        # proposal is built from.
        memory=runtime_memory,
        knowledge=knowledge,
        agent_id=scenario.initial_state.agent_id,
        model_provider=cassette,
        retriever=build_retriever(
            scenario.context_config,
            memory=runtime_memory,
            knowledge=knowledge,
            agent_id=scenario.initial_state.agent_id,
        ),
    )
    started = time.monotonic()
    try:
        result = GraphExecutor(graph.compile(), services).run(
            scenario.initial_state, record_trace=True
        )
    except HumanApprovalRequiredError as exc:
        # A PAUSE, not a failure — and deliberately NOT an approval.
        #
        # `Services.hitl_approvals` is left empty on purpose. Supplying it
        # here would mean the gate approves things on the owner's behalf so
        # that the corpus can execute, which is the harness granting itself
        # the permission the edge exists to withhold. The cost is that a
        # HITL-gated scenario cannot be scored on behaviour; the benefit is
        # that the gate is never bypassed to make measurement convenient.
        #
        # This is the only place `hitl_paused` is set, and it is set from a
        # caught kernel exception rather than from anything the candidate
        # wrote (ADR 0081).
        return {
            "outcome": {
                "terminated": False,
                "plan_status": None,
                "error_count": 0,
                "policy_denials": 0,
                "node_path": [],
                "hitl_paused": True,
            },
            "score": 0.0,
            "cost_tokens": 0,
            "paused": f"{exc}",
        }
    except Exception as exc:  # noqa: BLE001 - any failure is an outcome, not a crash
        failure = f"{type(exc).__name__}: {exc}"
        failed: dict[str, Any] = {
            "outcome": {
                "terminated": False,
                "plan_status": None,
                "error_count": 1,
                "policy_denials": 0,
                "node_path": [],
            },
            "score": 0.0,
            "cost_tokens": 0,
            "failure": failure,
            "cassette": {"hits": cassette.hits, "misses": cassette.misses},
        }
        if is_dead_call(
            failure,
            cassette_miss=cassette_miss,
            live_provider_present=live_provider is not None,
        ):
            # The SAME classifier the isolated path uses, from the same
            # string, for the reason ADR 0091 states: two constructions of
            # one judgement drift, and the drift is a phantom.
            failed["dead_call"] = True
        return failed

    elapsed_ms = (time.monotonic() - started) * 1000.0
    outcome = classify(result.final_state, result.trace, terminated=True)
    try:
        record = score_scenario(scenario, result.final_state, elapsed_ms=elapsed_ms)
    except CheckError as exc:
        # ONE scenario's unusable check is one scenario's zero, never the
        # suite's. `score_scenario` sat OUTSIDE this try until ADR 0177: a
        # check that raised — the 10,000-character backstop firing on a
        # perfectly linear content pattern, for one — propagated out of
        # `run_scenario`, out of `cmd_score`, and killed every remaining
        # scenario in the corpus. Reproduced: a two-scenario corpus where the
        # first summary is 12,000 characters printed `error: refusing to run
        # regex check ...` and exited 1 (EXIT_REJECTED), and the second
        # scenario — which scores fine — never ran.
        #
        # The OUTCOME is the real one: the graph ran and terminated, and what
        # failed is the owner's check, not the candidate's behaviour. So G2's
        # question is answered honestly and only the score is withheld.
        return {
            "outcome": outcome.to_payload(),
            "score": 0.0,
            "cost_tokens": 0,
            "elapsed_ms": elapsed_ms,
            "failure": f"unusable check: {exc}",
            "cassette": {"hits": cassette.hits, "misses": cassette.misses},
        }
    if memory is not None:
        # Without this, a scored split writes a success record per run and
        # `runs_since_last_seen` climbs while the lesson can never be re-seen
        # (S1b: 17 by the seventeenth scenario, 0 with the producer here).
        from aef.harness.check_memory import record_check_outcomes

        record_check_outcomes(
            memory=memory,
            checks=scenario.checks,
            final_state=result.final_state,
            critic=services.require_critic(),
            judge=services.require_judge(),
            run_id=scenario.id,
            agent_id=scenario.initial_state.agent_id or "",
            # EXECUTION time, not the scenario's recording time: freshness
            # (`runs_since_last_seen`) is ordered by `created_at`, and a record
            # dated when the scenario was RECORDED sorts before every lesson
            # seeded later, so a scored run could never refresh one (final seam
            # hunt, ADR 0191: 11 of 17 validation scenarios could not).
            created_at=datetime.now(UTC),
            graph_version=graph.version,
        )
    payload: dict[str, Any] = {
        "outcome": outcome.to_payload(),
        "score": score_of(record),
        "cost_tokens": record.cost_tokens,
        "elapsed_ms": elapsed_ms,
        # How the model was answered: hits replayed, misses went live (or
        # failed). A score with misses > 0 under "live" is a LIVE score.
        "cassette": {"hits": cassette.hits, "misses": cassette.misses},
    }
    node_failure = recorded_node_failure(result.final_state)
    if node_failure is not None:
        payload["failure"] = node_failure
    if "checks" in record.metadata:
        payload["checks"] = record.metadata["checks"]
    if record.metadata.get("budget_exceeded"):
        payload["budget_exceeded"] = True
    return payload
