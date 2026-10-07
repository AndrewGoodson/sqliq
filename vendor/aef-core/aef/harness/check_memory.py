"""A failed owner check becomes failure memory (ADR 0174).

The wire this closes was found from both ends on the same night.

**From M4's end** (ADR 0157): a prompt-file agent that *answers* cannot
produce failure memory. `make_reflect_node` writes `kind="failure"` iff
`failure_signals(state)` is non-empty, and that reads `state.errors` and
`state.tool_results`. An owner **check** is the task metric (ADR 0113),
evaluated by the harness *after* the run, and it never touches either. So a
bootstrapped run that failed its owner's check wrote
`kind="success", verbal_feedback="no failure signals: 0 error(s) recorded…"`
and `aef loop cycle` said `no admissible failure memory` forever.

**From S1's end** (ADR 0155): zero knowledge entries formed in any of the four
ACE arms, because every record on the summary split was a `success` whose
signature is `"success:" + objective` — six distinct objectives, six unique
signatures, and ADR 0110's two-run threshold unreachable by construction.
Two of those six runs *failed an owner check*.

One producer answers both. It evaluates the owner's checks against the final
state and, when any fail, runs the **real** `Critic`/`Judge` over that state
with the check failures supplied as evidence, and writes one
`kind="failure"` `MemoryRecord`.

## Three rules it is built to

**It never invents a failure** (ADR 0060, ADR 0145). The check is the owner's,
written before the run; the observed value is the run's. This module decides
neither. It is the same distinction `BootstrapInput` already draws: `expected`
is a judgement about what a run turned out to do and is refused, while
`checks` are "a specification of the task written before the run". Evaluating
a specification the owner wrote against output the run produced is recording
what happened.

**It never writes when the run also errored.** A run that raised already has a
failure record from its own reflect node, and `score_scenario` itself ignores
the check fraction when `final_state.errors` is non-empty — a second record
built from checks the scorer disregarded would be evidence of something
nobody measured.

**It never copies the check's expected value.** ACE's method and teaching to
the test are the same operation (ADR 0157), and the one thing that separates
them is whether the lesson carries the answer. `RuleBasedPromptProposer` pastes
an entry's `latest_feedback` verbatim into the agent's persona, so a literal
`contains 'VERDICT:'` in this record's `verbal_feedback` is a target string
appended to the prompt. The rendering below names the state path and the
operator in prose — never `check.value`. What it still leaks is stated in ADR
0174 and is not nothing.

**It never copies the run's own output either** (ADR 0180). The first version
of this module quoted the observed value — `observed 406 words, 2836 chars:
'**No. The Accela connector…'` — on the reasoning that the observation is the
run's, not the owner's, so recording it records what happened. True, and
insufficient: that excerpt is the model's previous answer, and this record's
text is a **prompt surface**. ADR 0162 rig B measured the cost. A lesson whose
text carried a 38-word example summary made two at-cap runs LONGER (23 → 28
words against a cap of 25; 38 → 41 against 38) and broke the very cap check
the lesson describes. So the line keeps the COUNTS the harness computed —
words, characters, the check's path and operator, and `checks_passed` /
`checks_total` — and drops the excerpt. The output stays reachable for a human
in the run's recorded trace (`OUTPUT_LOCATION`); it never reaches a lesson.

## The signature, and why it drops the expected value too

`default_signature` keys a check-derived failure on `check:<path>:<op>` per
failed check — the check's identity **without** its value. Two consequences,
both deliberate:

- The same field failing the same kind of check on two different inputs
  **recurs**, which is what makes ADR 0110's two-run rule reachable at all.
  Keyed with the value's hash instead, the summary split produces two
  singleton groups and still zero entries (measured, ADR 0174).
- Nothing that reaches a prompt as a provenance marker (`<!-- aef
  sig=… -->`, `render_retrieved_context`'s `[label]`) carries the answer.

The cost is that two required-substring failures on one field merge into one
lesson. That is one behaviour — "this field keeps omitting a required term" —
not a catch-all across unrelated failures, which is the thing
`consolidate.py` warns against.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime

from aef.harness.checks import TaskCheck, evaluate_checks, resolve
from aef.reasoning.nodes import retrieved_signatures
from aef.reasoning.reflection import Critic, Judge
from aef.services.memory.base import MemoryRecord, MemoryStore
from aef.state import AEFState, StateDelta

# The "node" credited with OBSERVING the failure. `make_reflect_node` records
# `ctx.node_id` here; the observer of a check is the harness's scorer, which is
# not a graph node, and naming a real node would attribute the observation to
# code that never ran. `failing_nodes` stays empty for the same reason: no node
# raised, so there is nothing to attribute (ADR 0096's field means the node
# that CAUSED the error).
CHECK_OBSERVER_NODE_ID = "task_checks"

# Key prefix for one failed check inside a signature. Read by
# `consolidate.default_signature`, which joins the keys with ">" exactly as it
# joins failing node ids.
CHECK_KEY_PREFIX = "check:"

# Prefix on this producer's DERIVED record ids. `MemoryRecord.id` defaults to a
# fresh uuid4, which makes "the same observation, recorded twice" two records;
# these ids are a hash of (agent, run, failed check keys) instead, so a store
# keyed by id collapses a repeat and a store that appends can still be told
# what happened. See `record_check_outcomes` for the other half.
CHECK_RECORD_ID_PREFIX = "checkfail-"

# Where a reader goes for the text this record is ABOUT, carried as its own
# content key rather than inside the failure lines. It is for a human reading
# the store; the lines are for a model reading a lesson, and the `Critic`
# excerpts each quoted signal at 160 characters, so a sentence spent here
# would push the observation out of `verbal_feedback` — measured, the first
# draft of this constant did exactly that.
#
# The output itself is in the recorded scenario: `corpus/<split>/<id>.json`,
# whose `trace` replays to the final state, so the value the check read is
# `resolve(final_state, check.path)`. `aef loop score` prints it per failing
# check too. A human can always see it; a lesson never carries it (ADR 0180).
OUTPUT_LOCATION = (
    "the observed value is not recorded here — replay the run's trace "
    "(corpus/<split>/<scenario>.json) and read the check's own state path"
)

# The prose each operator gets, with the expected value REMOVED. Present as a
# table rather than an f-string chain so that adding an op to `checks.OPS`
# without deciding how it reads here fails loudly in `_describe_failure`
# instead of silently rendering the value.
_OP_PROSE: dict[str, str] = {
    "contains": "does not contain a required substring the owner declared",
    "equals": "does not equal the value the owner declared",
    "regex": "does not match the pattern the owner declared",
    "exists": "was never recorded",
    "max_words": "is longer than the owner's maximum",
    "min_words": "is shorter than the owner's minimum",
}


def check_key(check: TaskCheck) -> str:
    """One failed check's identity, value excluded.

    A path containing `>` would be split by `consolidate._failure_nodes` when
    it parses a signature for the helpful/harmful tally. Grouping is exact
    string equality and is unaffected; only the tally could mis-parse, and a
    state path with `>` in it is not something the checks in this repo
    produce. Recorded rather than guarded, because a guard here would have to
    rewrite the path and two paths would then collide.
    """
    return f"{CHECK_KEY_PREFIX}{check.path}:{check.op}"


def _observed(final_state: AEFState, check: TaskCheck) -> str:
    """The SHAPE of what the run produced, and nothing of its content.

    This used to end `: '<the first 59 characters of the output>'`, and that
    excerpt is the model's own prior answer pasted into its next prompt — the
    record's `verbal_feedback` is what `RuleBasedPromptProposer` appends to a
    persona and what `render_retrieved_context` renders as a lesson bullet.
    ADR 0162 rig B measured what that costs: a 330-character bullet whose
    lesson text contained a 38-word example summary made two at-cap runs
    LONGER (23→28 words against a cap of 25, 38→41 against 38) and broke the
    very check the lesson is about. ADR 0110's rule — *the model is never
    trusted with provenance; it may write one prose string* — applied one
    layer down: the observation is the harness's, so the harness states it in
    numbers it computed, and the text stays where a human can read it.

    A non-string value is reported by type alone. `repr(True)` is as much the
    run's own output as a sentence is, and its LENGTH is the answer on a
    boolean field (4 versus 5 characters), which is the leak ADR 0174's
    residual list names as unavoidable for a small-domain `equals` check —
    unavoidable there, gratuitous here.
    """
    found, value = resolve(final_state, check.path)
    if not found:
        return "<missing>"
    if not isinstance(value, str):
        return f"a non-text value of type {type(value).__name__}"
    return f"{len(value.split())} words, {len(value)} chars"


def _describe_failure(final_state: AEFState, check: TaskCheck) -> str:
    """One line of evidence about one failed check, with the owner's expected
    value left out. `check.value` is never read here — asserted by a test that
    plants an unusual literal in a check and greps the whole record for it."""
    prose = _OP_PROSE.get(check.op)
    if prose is None:  # pragma: no cover - `OPS` and `_OP_PROSE` are pinned equal by a test
        prose = "did not satisfy the owner's check"
    if check.op == "exists":
        return f"check failed: {check.path} {prose}"
    return f"check failed: {check.path} {prose}; observed {_observed(final_state, check)}"


def check_failure_record(
    *,
    checks: Sequence[TaskCheck],
    final_state: AEFState,
    critic: Critic,
    judge: Judge,
    run_id: str,
    agent_id: str,
    created_at: datetime,
    graph_version: str = "",
    tags: tuple[str, ...] = (),
) -> MemoryRecord | None:
    """The `kind="failure"` record for a run whose owner checks did not hold,
    or `None` when there is nothing to record.

    `None` when: no checks were declared, every check held, or the run
    recorded an error of its own — see the module docstring for why the third
    is not an omission.

    **The derived state.** The `Critic` reads `state.errors`, so the check
    failures are supplied by applying a `StateDelta` carrying them to a LOCAL
    copy of the final state. That copy never leaves this function: the
    scenario, the trace, `classify`, `score_scenario` and the corpus all see
    the state the run actually produced. Injecting the failures into the real
    run state instead would make the run look like it raised — `classify`
    would report it failed for the wrong reason, and `score_scenario` would
    stop counting the check fraction *because* the checks failed, which is a
    metric that changes when you measure it (ADR 0174 rejects that design and
    says so).

    `Critique.grounded_in` therefore indexes the derived evidence, not the
    recorded state; the same lines sit at the same indices in the record's
    `check_failures`, so every citation still resolves to something a reader
    can see.
    """
    if not checks:
        return None
    if final_state.errors:
        return None
    report = evaluate_checks(checks, final_state)
    if not report.failures:
        return None

    lines = [_describe_failure(final_state, check) for check in report.failed]
    keys: list[str] = []
    for check in report.failed:
        key = check_key(check)
        if key not in keys:
            keys.append(key)

    derived = StateDelta(errors=[{"error": line} for line in lines]).apply(final_state)
    critique = critic.critique(derived)
    judgment = judge.judge(derived)

    return MemoryRecord(
        id=check_record_id(agent_id=agent_id, run_id=run_id, keys=keys),
        kind="failure",
        content={
            "verbal_feedback": critique.verbal_feedback,
            "grounded_in": list(critique.grounded_in),
            "score": judgment.score,
            "rubric": dict(judgment.rubric),
            "rationale": judgment.rationale,
            "node_id": CHECK_OBSERVER_NODE_ID,
            # No node raised. `failing_nodes` names the node that CAUSED an
            # error and there is none; `failed_checks` is what this failure is
            # keyed on instead (see `consolidate.default_signature`).
            "failing_nodes": [],
            "failed_checks": keys,
            "check_failures": lines,
            "checks_passed": report.passed,
            "checks_total": report.total,
            # Not rendered into any prompt — `render_retrieved_context` reads
            # the summary or the feedback and nothing else — and not copied
            # into a `KnowledgeEntry`, whose content is a fixed set of keys.
            # It is here so a human reading the store knows the excerpt's
            # absence is deliberate and where the text went (ADR 0180).
            "output_location": OUTPUT_LOCATION,
            "retrieved_signatures": retrieved_signatures(final_state),
            "graph_version": graph_version,
            "objective": final_state.objective,
        },
        run_id=run_id,
        agent_id=agent_id,
        tags=tags,
        created_at=created_at,
    )


def check_record_id(*, agent_id: str, run_id: str, keys: Sequence[str]) -> str:
    """This producer's record id: a hash of what the observation IS.

    Two evaluations of the same owner checks against the same run's final
    state are one observation, so they are one id. `MemoryRecord.id` otherwise
    defaults to a fresh uuid4 and the second call would be a second record —
    which is not a cosmetic duplicate: `KnowledgeEntry.source_record_ids` is
    the entry's only measure of how well-evidenced it is, and ADR 0110's
    threshold is *distinct runs*, so a producer wired at two call sites could
    inflate the evidence for a lesson simply by being wired twice.
    """
    digest = hashlib.sha256("\x00".join([agent_id, run_id, *keys]).encode()).hexdigest()
    return f"{CHECK_RECORD_ID_PREFIX}{digest[:32]}"


def record_check_outcomes(
    *,
    memory: MemoryStore,
    checks: Sequence[TaskCheck],
    final_state: AEFState,
    critic: Critic,
    judge: Judge,
    run_id: str,
    agent_id: str,
    created_at: datetime,
    graph_version: str = "",
    tags: tuple[str, ...] = (),
) -> MemoryRecord | None:
    """`check_failure_record`, written to `memory` at most once per
    `(agent_id, run_id, failed check keys)`. Returns the record — the one just
    written, or the one already there — or `None` when there was none.

    **This is the function every caller that scores a run against owner checks
    should call**, and it is one function rather than a call site because ADR
    0174 wired the producer into `bootstrap` alone. ADR 0175 then measured what
    that costs: over a 17-scenario scored split the seeded lesson's
    `runs_since_last_seen` climbs monotonically — every scored run writes a
    `success` record and none can write a check failure — so ADR 0116's
    staleness demotion walks the lesson from rank 0 to rank 39, and the two
    owner-check negatives late in the split never saw it. The lesson could not
    be re-seen because nothing outside `bootstrap` could produce it.

    **Idempotent, so wiring it in more than one place cannot double-count.**
    Two guards, because they fail on different stores: the record's id is
    derived (`check_record_id`), which collapses a repeat in any store keyed by
    id; and this function first asks the store whether it already holds a
    failure record for this run with these keys, which covers an append-only
    store such as `FileMemoryStore` and covers a second process. The known
    hole is stated rather than left to be found: `bootstrap.RunScopedMemory`
    answers queries from its per-input scratch, so a repeat write into its
    durable *sink* from a later invocation is caught by the derived id and not
    by the query.

    **What it does NOT do is decide where it is safe to call.** ADR 0174
    refused `scenario_runner.run_scenario` because the gates re-execute corpus
    scenarios and a gate run that writes to the adopter's durable store lets
    scoring a candidate manufacture the evidence for the next one. That
    refusal stands: the store is the caller's to supply, and a gate path
    supplies none.
    """
    record = check_failure_record(
        checks=checks,
        final_state=final_state,
        critic=critic,
        judge=judge,
        run_id=run_id,
        agent_id=agent_id,
        created_at=created_at,
        graph_version=graph_version,
        tags=tags,
    )
    if record is None:
        return None
    existing = _already_recorded(memory, record)
    if existing is not None:
        return existing
    memory.write(record)
    return record


def _already_recorded(memory: MemoryStore, record: MemoryRecord) -> MemoryRecord | None:
    """The failure record this run already holds for these checks, if any.

    `limit` is generous rather than 1: the query is most-recent-first over
    every failure record of the run, and a run's own reflect node may have
    written several. Reading a few and comparing keys is cheaper than being
    wrong.
    """
    if not record.run_id:  # pragma: no cover - `check_failure_record` requires a run id
        return None
    keys = record.content.get("failed_checks")
    for candidate in memory.query(
        "failure", run_id=record.run_id, agent_id=record.agent_id, limit=64
    ):
        if candidate.id == record.id or candidate.content.get("failed_checks") == keys:
            return candidate
    return None
