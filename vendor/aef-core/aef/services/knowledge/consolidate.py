"""`RuleBasedConsolidator` — the first real producer for `KnowledgeStore`, and
the middle step of WikiSkill's loop (arXiv:2608.27454): experience in,
consolidated knowledge out.

Rule-based, for the reason `RuleBasedCritic` was (ADR 0046): no LLM, no vendor
SDK, no clock read. Everything is computed from records already written by
`make_reflect_node`, which makes it testable against hand-built adversarial
records rather than only against a live model. An LLM-backed consolidator is
I5, and it changes nothing about this interface.

**Stateless recompute, deliberately.** This reads the memory store, groups, and
emits — it keeps no cursor and no "already consolidated" marker. Re-running
over an unchanged store therefore produces the same entries with the same
provenance, which the store's order-preserving dedupe absorbs into no change at
all. A cursor would be a second source of truth about what has been seen, and
ADR 0091's finding is that two records of one fact drift.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from aef.services.knowledge.base import KnowledgeEntry, KnowledgeKind, KnowledgeStore
from aef.services.memory.base import MemoryRecord, MemoryStore

# A signature is a stable, derived identity for "the same thing going wrong
# again". `None` means THIS RECORD CANNOT BE SIGNED and must be dropped —
# never bucketed into a catch-all, which would merge unrelated failures into
# one entry and manufacture a lesson nobody learned.
SignatureFn = Callable[[MemoryRecord], str | None]

# Produces the human-readable lesson text for one group of records. Injected
# rather than branched on, so the LLM-backed variant (I5) swaps ONLY the prose
# and inherits the grouping, the two-run threshold, the per-run dedupe and the
# agent keying — all of which I4 measured. One code path, one set of rules.
SummariseFn = Callable[[str, list[MemoryRecord]], str | None]

# Records read per kind before grouping. This BOUNDS THE ANSWER, not just the
# work: the store returns most-recent-first, so a failure whose earlier
# occurrences sit past this depth consolidates with a lower count than it has
# earned. Stated here rather than discovered later — the same disclosure
# `MemoryRetriever.candidates_per_kind` carries.
DEFAULT_CANDIDATES_PER_KIND = 500

# Distinct RUNS a signature must appear in before it is knowledge. Two, because
# one is an episode. See `_representatives_by_run` for why runs and not records.
DEFAULT_MIN_OCCURRENCES = 2

_EPOCH = datetime.min.replace(tzinfo=UTC)


def default_signature(record: MemoryRecord) -> str | None:
    """`(kind, failed_checks | failing_nodes)` for failures, `(kind,
    objective)` for successes.

    `failing_nodes` is the key because ADR 0096 established it as the field a
    structural proposer cannot begin without — *"add a fallback to the flaky
    node" requires knowing which node was flaky* — and `make_reflect_node`
    records it for exactly that reason.

    `failed_checks` is read FIRST and comes from `harness.check_memory`, whose
    records describe a run that answered cleanly and failed the owner's task
    metric (ADR 0174). It is a different failure with a different key because
    no node failed: `failing_nodes` is empty on those records by construction,
    which under the node rule alone made them unsignable and therefore
    invisible to this layer. Each key is the check's identity *without its
    expected value* (`check:<path>:<op>`), which is what lets the same field
    failing the same kind of check on two different inputs recur — the
    condition ADR 0155 found the summary split could never meet — and what
    keeps a target string out of every provenance marker rendered into a
    prompt.

    Returns `None` when the record carries nothing stable to key on. A failure
    with no recorded failing node is unattributable, and the precedent for
    what to do about that is already in this repo: `_failing_nodes` skips
    errors whose origin was not recorded, because guessing is worse than
    omitting.

    `content` is `dict[str, Any]` and nothing constrains its shape, so every
    read here is defensive by necessity, not by superstition.
    """
    if record.kind == "failure":
        checks = record.content.get("failed_checks")
        if isinstance(checks, (list, tuple)):
            keys = [c for c in checks if isinstance(c, str) and c]
            if keys:
                # Order preserved for the same reason `failing_nodes` is: the
                # checks are the owner's, in the order they declared them.
                return "failure:" + ">".join(keys)
        raw = record.content.get("failing_nodes")
        if not isinstance(raw, (list, tuple)):
            return None
        nodes = [n for n in raw if isinstance(n, str) and n]
        if not nodes:
            return None
        # Order preserved, not sorted: `_failing_nodes` keeps first-seen order
        # because the first failure is usually the cause and the rest are
        # consequences. A->B and B->A are different failures.
        return "failure:" + ">".join(nodes)

    if record.kind == "success":
        objective = record.content.get("objective")
        if not isinstance(objective, str) or not objective:
            return None
        return "success:" + objective

    return None


@dataclass(frozen=True)
class RuleBasedConsolidator:
    """Group records into `KnowledgeEntry`s and upsert them.

    Deterministic given the same store contents: no clock read (times come
    from `MemoryRecord.created_at`), no randomness, and every ordering is a
    stable total order.
    """

    signature_fn: SignatureFn = default_signature
    # `None` keeps the entry's text as the most recent occurrence's feedback,
    # verbatim. Anything else may only produce PROSE — see `_build_entry`,
    # where every provenance field is computed from the records and none is
    # taken from the summariser.
    summarise: SummariseFn | None = None
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES
    candidates_per_kind: int = DEFAULT_CANDIDATES_PER_KIND
    kinds: tuple[KnowledgeKind, ...] = ("failure", "success")

    def __post_init__(self) -> None:
        if self.min_occurrences < 2:
            raise ValueError(
                f"min_occurrences must be at least 2; got {self.min_occurrences}. A "
                f"threshold of 1 promotes every one-off episode to knowledge, which is "
                f"the failure this layer exists to avoid."
            )
        if self.candidates_per_kind <= 0:
            raise ValueError(
                f"candidates_per_kind must be positive; got {self.candidates_per_kind}"
            )

    def consolidate(
        self,
        memory: MemoryStore,
        knowledge: KnowledgeStore,
        *,
        agent_id: str | None,
    ) -> list[KnowledgeEntry]:
        """Read `memory`, upsert everything at or above threshold into
        `knowledge`, and return the entries written (newest last-seen first).

        `agent_id` is REQUIRED and has no default, matching
        `MemoryRetriever.agent_id` — a default of "every agent" was found by an
        adversarial round to hand one agent another's recorded failures.
        Passing `None` genuinely means "all agents" and stays safe here
        regardless, because grouping keys on each record's OWN `agent_id`; it
        reads across agents, it never merges across them.
        """
        groups: dict[tuple[str | None, str], list[MemoryRecord]] = {}
        # Every run this agent has recorded, by its latest record time. This is
        # what "runs since last seen" is counted against (ADR 0116): a lesson
        # that has stopped recurring while the agent keeps running is stale,
        # and the retriever demotes it rather than this layer deleting it.
        runs_seen: dict[tuple[str | None, str], datetime] = {}
        # (agent, run) -> the signatures in context, and the signature the run
        # itself produced. ACE's helpful/harmful tally is derived from these
        # two per run (ADR 0118).
        in_context: dict[tuple[str | None, str], set[str]] = {}
        produced: dict[tuple[str | None, str], set[str]] = {}
        # The subset of `produced` written by records the reflect node (or the
        # harness's check producer) called a FAILURE. Kept apart from
        # `produced` rather than filtered out of it by prefix, because a custom
        # `signature_fn` need not spell failures `failure:…` — the record's own
        # `kind` is the fact, and inferring it from the string would be the
        # same guess `_failure_nodes` refuses to make (ADR 0180).
        produced_failures: dict[tuple[str | None, str], set[str]] = {}
        for kind in self.kinds:
            for record in memory.query(kind, agent_id=agent_id, limit=self.candidates_per_kind):
                if record.run_id and record.created_at is not None:
                    key = (record.agent_id, record.run_id)
                    runs_seen[key] = max(runs_seen.get(key, _EPOCH), record.created_at)
                signature = self.signature_fn(record)
                if record.run_id:
                    run_key = (record.agent_id, record.run_id)
                    shown = record.content.get("retrieved_signatures")
                    if isinstance(shown, (list, tuple)):
                        in_context.setdefault(run_key, set()).update(
                            s for s in shown if isinstance(s, str)
                        )
                    if signature is not None:
                        produced.setdefault(run_key, set()).add(signature)
                        if record.kind == "failure":
                            produced_failures.setdefault(run_key, set()).add(signature)
                if signature is None:
                    continue
                groups.setdefault((record.agent_id, signature), []).append(record)

        written: list[KnowledgeEntry] = []
        for (record_agent_id, signature), records in groups.items():
            representatives = _representatives_by_run(records)
            if len(representatives) < self.min_occurrences:
                continue
            entry = _build_entry(signature, record_agent_id, representatives, self.summarise)
            helpful, harmful, harmful_elsewhere = _tally(
                signature,
                entry.kind,
                record_agent_id,
                in_context,
                produced,
                produced_failures,
            )
            entry = dataclasses.replace(
                entry,
                runs_since_last_seen=_runs_since(entry.last_seen, record_agent_id, runs_seen),
                helpful=helpful,
                harmful=harmful,
                harmful_elsewhere=harmful_elsewhere,
            )
            written.append(knowledge.upsert(entry))

        written.sort(key=lambda e: (_sort_ts(e.last_seen), e.signature), reverse=True)
        return written


def _representatives_by_run(records: list[MemoryRecord]) -> list[MemoryRecord]:
    """At most ONE record per run, most recent within that run.

    This is what makes `occurrence_count` mean "recurred". A graph may reflect
    more than once in a single run — `make_reflect_node`'s idempotency key is
    keyed on `checkpoint_seq` precisely because successive reflections within
    one run are expected — so counting records would let one bad run report
    itself as a well-established pattern. It is the same class of defect as
    counting a re-consolidated record twice, which the store already refuses.

    Records with no `run_id` are DROPPED: a record that cannot be attributed
    to a run cannot be evidence that something recurred ACROSS runs, and
    inventing a run for it would be the guess this repo's precedent forbids.
    """
    by_run: dict[str, MemoryRecord] = {}
    for record in records:
        if not record.run_id:
            continue
        incumbent = by_run.get(record.run_id)
        if incumbent is None or _rank(record) > _rank(incumbent):
            by_run[record.run_id] = record
    return sorted(by_run.values(), key=_rank)


def _build_entry(
    signature: str,
    agent_id: str | None,
    representatives: list[MemoryRecord],
    summarise: SummariseFn | None = None,
) -> KnowledgeEntry:
    """`representatives` is already sorted oldest-first by `_rank`."""
    newest = representatives[-1]
    kind: KnowledgeKind = "failure" if newest.kind == "failure" else "success"

    content: dict[str, object] = {
        "signature": signature,
        # From the most recent occurrence, not a merge of all of them:
        # concatenating feedback across runs produces text no run ever
        # produced, and a lesson nobody can trace to an execution.
        "latest_feedback": newest.content.get("verbal_feedback"),
        "objective": newest.content.get("objective"),
        "run_ids": [r.run_id for r in representatives],
    }

    # The ONLY field a summariser may influence. Everything above and below is
    # computed from the records: a summariser that could write `run_ids` or
    # `source_record_ids` could fabricate evidence for its own lesson, and an
    # entry's occurrence count is the sole measure of how well-established it
    # is. A summariser returning None or empty leaves the verbatim feedback in
    # place rather than blanking it.
    if summarise is not None:
        summary = summarise(signature, representatives)
        if summary:
            content["summary"] = summary
    failing_nodes = newest.content.get("failing_nodes")
    if isinstance(failing_nodes, (list, tuple)):
        content["failing_nodes"] = [n for n in failing_nodes if isinstance(n, str)]

    timestamps = [r.created_at for r in representatives if r.created_at is not None]
    return KnowledgeEntry(
        signature=signature,
        kind=kind,
        content=content,
        agent_id=agent_id,
        source_record_ids=tuple(r.id for r in representatives),
        first_seen=min(timestamps) if timestamps else None,
        last_seen=max(timestamps) if timestamps else None,
        tags=newest.tags,
    )


def _runs_since(
    last_seen: datetime | None,
    agent_id: str | None,
    runs_seen: dict[tuple[str | None, str], datetime],
) -> int:
    """Distinct runs of THIS agent whose latest record is newer than the
    entry's last occurrence. Computed from the records every consolidation,
    never stored as a counter (ADR 0091: two records of one fact drift).
    Records without a run or a timestamp cannot be counted and are not
    guessed at; an entry with no `last_seen` has nothing to count from."""
    if last_seen is None:
        return 0
    return sum(
        1
        for (run_agent, _), latest in runs_seen.items()
        if run_agent == agent_id and latest > last_seen
    )


def _tally(
    signature: str,
    kind: KnowledgeKind,
    agent_id: str | None,
    in_context: dict[tuple[str | None, str], set[str]],
    produced: dict[tuple[str | None, str], set[str]],
    produced_failures: dict[tuple[str | None, str], set[str]],
) -> tuple[int, int, int]:
    """Runs of this agent that had `signature` in context, split THREE ways. A
    lesson never shown to a run scores nothing at all — absence of evidence.

    | the run… | outcome |
    |---|---|
    | reproduced this failure | `harmful` |
    | failed nothing | `helpful` |
    | resolved this failure and failed something else | `harmful_elsewhere` |

    Four rules, each a fix for a defect somebody reproduced.

    1. **Only `failure` entries are tallied** (ADR 0126, erratum to 0118). A
       success entry keeps `(0, 0, 0)`. The tally asks "was this failure
       avoided", and every run that repeats a success necessarily re-produces
       the success signature, so a success lesson scored `harmful` once per
       time it worked — the metric read backwards on exactly the entries it
       was most confident about.
    2. **Reproduced means the entry's failing-node list appears, in order,
       inside a failure signature the run produced** (ADR 0126). Not string
       equality: a run whose `fetch` failure cascaded into `parse` signs
       itself `failure:fetch>parse`, which is a different string from the
       `failure:fetch` lesson it was shown, so equality counted the run
       *helpful* — the lesson was credited with preventing the very failure
       that had just happened. Order is kept (a subsequence, not a set
       subset) because `default_signature` states that `A>B` and `B>A` are
       different failures. A signature this rule cannot parse as a node list
       — a custom `signature_fn` — still matches itself by equality.
    3. **`helpful` requires that the run failed NOTHING** (ADR 0180). ADR
       0118's two outcomes had no room for "had it in context and failed
       DIFFERENTLY", so a run that resolved the lesson's failure and broke
       another owner check counted toward the good column. That is not a
       corner case: it is the single instance ADR 0162's rig B produced
       (`sum-35-priory-gatehouse` — the word-cap lesson shortened the summary
       from 30 words to 28 and the shortened text stopped matching a content
       regex), and the shipped tally read `helpful=7 harmful=3` with the one
       genuinely harmful run inside the 7. A signal that moves the wrong way
       as harm rises is worse than no signal, which is why ADR 0162 refused
       to rank on it.
    4. **Failure is the record's `kind`, not a prefix on its signature.** The
       third outcome is decided by `produced_failures`, which is populated
       only from records the producer marked `kind="failure"`. A run's
       `success:<objective>` signature is not a failure however it is spelled,
       and a custom `signature_fn` may spell failures any way it likes.

    Rule 3 changes what `helpful` MEANS, and the honest reading of it is
    narrow: the consolidator sees records, not scenarios, so it cannot know
    whether the other failure had passed on some previous run of the same
    scenario. What it can say is that the run had the lesson and still failed,
    which is enough to keep it out of the good column and not enough to blame
    the lesson for it. ADR 0180 states that limit and does NOT rank on the
    third counter — surfacing it is the whole change.
    """
    if kind != "failure":
        return 0, 0, 0
    entry_nodes = _failure_nodes(signature)
    helpful = harmful = harmful_elsewhere = 0
    for run_key, shown in in_context.items():
        if run_key[0] != agent_id or signature not in shown:
            continue
        if _reproduced(signature, entry_nodes, produced.get(run_key, set())):
            harmful += 1
        elif produced_failures.get(run_key):
            harmful_elsewhere += 1
        else:
            helpful += 1
    return helpful, harmful, harmful_elsewhere


def _failure_nodes(signature: str) -> tuple[str, ...] | None:
    """The node list `default_signature` encoded, or `None` for any signature
    that is not in that shape (a custom `signature_fn`, or a success)."""
    if not signature.startswith("failure:"):
        return None
    nodes = tuple(n for n in signature[len("failure:") :].split(">") if n)
    return nodes or None


def _reproduced(signature: str, entry_nodes: tuple[str, ...] | None, produced: set[str]) -> bool:
    if signature in produced:
        return True
    if entry_nodes is None:
        return False
    for candidate in produced:
        candidate_nodes = _failure_nodes(candidate)
        if candidate_nodes is not None and _is_subsequence(entry_nodes, candidate_nodes):
            return True
    return False


def _is_subsequence(needle: tuple[str, ...], haystack: tuple[str, ...]) -> bool:
    """`needle`'s elements appear in `haystack` in the same relative order."""
    it = iter(haystack)
    return all(node in it for node in needle)


def _rank(record: MemoryRecord) -> tuple[datetime, str]:
    """Total order over records. Id breaks the tie so two records written in
    the same instant still order stably — an unstable order here would change
    which record is 'representative' between two identical consolidations."""
    return (_sort_ts(record.created_at), record.id)


def _sort_ts(value: datetime | None) -> datetime:
    return value if value is not None else _EPOCH
