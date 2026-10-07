"""`RuleBasedPromptProposer` — ACE's method on a prompt file (ADR 0157).

`RuleBasedProposer` edits module-level numeric constants and applies a bounded
structural catalogue; both are AST operations, and a `.md` persona has no AST.
`LLMProposer` rewrites a whole file, but validates the reply with `ast.parse`
and G0's Python scanner, so a markdown reply is rejected before it can be
proposed. On a prompt-file repo — which is *every* eligible repo the
2026-09-04 survey looked at — neither proposer can produce a candidate at all.

This one can, and it does it the way the knowledge layer already computes:

**Take the highest-recurrence admissible failure lesson and append it as ONE
bullet under a `## Lessons (aef)` section at the end of the persona.**

Four rules, each of which is a rule this repo already made somewhere else:

1. **The two-run rule stands** (ADR 0110). A signature must have recurred in
   two *distinct runs* before it is a lesson; one occurrence is an episode.
   That threshold is not re-implemented here — `RuleBasedConsolidator` owns
   it, and this module consolidates the admissible records through it.
2. **The prompt never becomes the model's.** No model is called. The bullet
   text is the entry's own recorded feedback, verbatim, carrying its
   signature and occurrence count in an HTML comment so a reader of the
   persona can trace the sentence back to the runs that produced it. Nothing
   here paraphrases, and nothing here writes a count it did not compute.
3. **Existing bullets are never rewritten.** A bullet is appended; the file's
   other bytes are unchanged. When the section is full (`max_bullets`,
   default 5) the *stalest* bullet this proposer wrote is evicted — by
   `runs_since_last_seen` (ADR 0116), looked up live from the knowledge the
   records currently support — and an owner-written bullet is never evicted
   at all. If owner bullets fill the section, the proposer says so and
   proposes nothing.
4. **Zone A or nothing** (ADR 0147/0152). A path that does not classify as
   Zone A under the gate's own `ZonePolicy` raises `PromptOutsideZoneAError`
   — a named refusal, not an empty result, because a proposer aimed at the
   harness is a configuration to fix rather than a cycle with nothing to say.
5. **A provider fact is not a lesson** (ADR 0179). A record naming a
   `prompt_agent.*` containment type describes the CLI the owner installed —
   whether it has a `--system-prompt` flag — and no sentence a persona can
   contain will change it. Such records are dropped from the evidence and
   counted in the reason, so the cycle says what it refused rather than
   quietly finding less. This is the second line of defence; the first is
   that the node stopped recording that fact as an error at all.

Determinism: no clock read, no randomness, every ordering a stable total
order. The same evidence produces the same bullet, and a second cycle over
unchanged evidence finds the lesson already present and proposes nothing —
saying which lesson and why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from aef.harness.corpus import Corpus
from aef.harness.proposer import MemoryEvidence, Proposal, ProposalError
from aef.harness.zones import ZonePolicy, inspect_path
from aef.reasoning.prompt_agent import (
    PROVIDER_FACT_TYPE_PREFIX,
    parse_codex_agent,
    replace_codex_instructions,
)
from aef.services.knowledge.base import KnowledgeEntry
from aef.services.knowledge.consolidate import (
    DEFAULT_MIN_OCCURRENCES,
    RuleBasedConsolidator,
    default_signature,
)
from aef.services.knowledge.in_memory import InMemoryKnowledgeStore
from aef.services.memory.base import MemoryRecord
from aef.services.memory.in_memory import InMemoryMemoryStore

# The heading this proposer owns. Everything above it in the persona is the
# author's; everything under it up to the next heading is this loop's working
# area, and the name says which loop wrote it.
DEFAULT_SECTION_HEADING = "## Lessons (aef)"

# How many bullets the section may hold. ADR 0157: a prompt that grows without
# bound stops being a persona and becomes a changelog, and every added token is
# paid on every run of the agent forever.
DEFAULT_MAX_BULLETS = 5

# One bullet's ceiling. `verbal_feedback` is written by `RuleBasedCritic` and
# is normally one line, but nothing constrains it, and an unbounded bullet is
# an unbounded edit to a file that G5 measures drift on.
MAX_BULLET_CHARS = 400

# The suffixes this proposer will edit. A `.py` agent path under
# `--proposer rule_based_prompt` is a configuration mistake — appending a
# markdown heading to Python is a syntax error, and G1 would find it, but the
# proposer should not be the thing that emits it.
PROMPT_SUFFIXES = (".md", ".markdown", ".txt", ".toml")

_MARKER_RE = re.compile(r"^-\s+<!--\s*aef\s+sig=(?P<sig>\S+)\s+runs=(?P<runs>\d+)\s*-->")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+")
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s")

# A signature is written into an HTML comment, so it may not contain the two
# characters that end one, nor whitespace (the marker is parsed back by a
# whitespace-delimited regex). `default_signature` produces neither, but a
# custom `signature_fn` could, and a marker that cannot be read back is a
# bullet this proposer would duplicate on the next cycle.
_UNWRITABLE_IN_SIGNATURE = ("--", ">", "<")

# A record naming one of these describes THE PROVIDER THE RUN GOT, not what
# the run did, and must never become a bullet in somebody's persona.
#
# The first line of defence is that `PromptAgentNode` no longer records such a
# fact as an error, so no reflect node writes one as failure memory (ADR 0179,
# R3). This is the second: a memory file written by an older `aef`, or a
# future node that reaches for `state.errors` for the same wrong reason, still
# cannot put "your CLI has no --system-prompt flag" in front of the model.
#
# Matched as a TOKEN in the record's own text rather than on the signature,
# and the reason is a measurement: the signature of the reproduced case is
# `failure:prompt_agent`, which is a NODE id — refusing that prefix would
# discard every genuine lesson the prompt-agent node ever produces. The error
# type is the only thing in the record that names the provider property, and
# the record carries it inside `verbal_feedback` because that is what
# `RuleBasedCritic` quotes. A text match is the honest description of what
# this can see.
_PROVIDER_FACT_RE = re.compile(re.escape(PROVIDER_FACT_TYPE_PREFIX) + r"[A-Za-z0-9_]+")


class PromptOutsideZoneAError(ProposalError):
    """The prompt file the proposer was aimed at is not Zone A.

    Its own type, and raised rather than returned as "nothing to propose",
    for the reason `UngroundedProposalError` has its own type: a proposer
    pointed outside the agent-writable tree is a configuration error an
    operator must fix, and reporting it as a quiet cycle is how a loop goes
    silently inert (ADR 0139).
    """


@dataclass(frozen=True)
class _Decision:
    """What one call worked out, so the reason survives an empty result.

    `cycle` prints "the proposer produced nothing from the available
    evidence", which is true and useless: *why* is the whole diagnostic. This
    carries it without giving the proposer mutable state — the same input
    recomputes the same decision.
    """

    proposals: tuple[Proposal, ...]
    reason: str


@dataclass(frozen=True)
class RuleBasedPromptProposer:
    """Deterministic, evidence-first, no model call. One bullet per cycle."""

    max_bullets: int = DEFAULT_MAX_BULLETS
    heading: str = DEFAULT_SECTION_HEADING
    zone_policy: ZonePolicy = field(default_factory=ZonePolicy)
    # Restricts the evidence to runs of THIS graph when both are known. The
    # rule is `MemoryEvidence`'s own: deny what is *known* to belong to
    # another graph's scenario, and admit a run id the corpus has never heard
    # of, because that is production experience and it is exactly what this
    # exists to learn from.
    #
    # Since ADR 0191 the AUTHORITATIVE application of that rule is
    # `MemoryEvidence.from_store(..., graph_id=...)`, so that every proposer
    # gets it and not just this one — being this proposer's private filter
    # was the defect. Kept here because a caller that builds a
    # `MemoryEvidence` by hand (the tests, and any direct library use) never
    # goes through `from_store`, and the filter is idempotent: applying it to
    # already-filtered records drops nothing.
    graph_id: str | None = None
    corpus: Corpus | None = None
    max_bullet_chars: int = MAX_BULLET_CHARS

    def __post_init__(self) -> None:
        if self.max_bullets < 1:
            raise ValueError(
                f"max_bullets must be at least 1; got {self.max_bullets}. Zero would make "
                f"every cycle evict what the last one wrote, which is a loop, not a limit."
            )
        if self.max_bullet_chars < 1:
            raise ValueError(f"max_bullet_chars must be positive; got {self.max_bullet_chars}")
        if not _HEADING_RE.match(self.heading):
            raise ValueError(
                f"heading must be a markdown heading (# .. ######); got {self.heading!r}"
            )

    # -- the proposer protocol ----------------------------------------------

    def propose_from_memory(
        self,
        evidence: MemoryEvidence,
        *,
        proposal_id: str,
        path: str,
        source: str,
    ) -> tuple[Proposal, ...]:
        """Same signature as `RuleBasedProposer.propose_from_memory`, so
        `_build_proposer` can hand either one to `cycle`."""
        return self._decide(evidence, proposal_id=proposal_id, path=path, source=source).proposals

    def no_proposal_reason(
        self,
        evidence: MemoryEvidence,
        *,
        path: str,
        source: str,
    ) -> str:
        """Why `propose_from_memory` returned nothing. Pure: it recomputes the
        same decision rather than remembering the last one."""
        return self._decide(evidence, proposal_id="explain", path=path, source=source).reason

    # -- the decision --------------------------------------------------------

    def _decide(
        self,
        evidence: MemoryEvidence,
        *,
        proposal_id: str,
        path: str,
        source: str,
    ) -> _Decision:
        verdict = inspect_path(path, self.zone_policy)
        if not verdict.allowed:
            raise PromptOutsideZoneAError(
                f"refusing to propose a prompt edit to {path!r}: {verdict.reason}. Only Zone A "
                f"is agent-writable; a prompt outside it cannot be blessed as a baseline "
                f"either, so a candidate against it could never be measured (ADR 0147)."
            )
        if not path.lower().endswith(PROMPT_SUFFIXES):
            return _Decision(
                (),
                f"{path} is not a prompt file (expected one of "
                f"{', '.join(PROMPT_SUFFIXES)}); this proposer appends a markdown bullet and "
                f"would corrupt anything else. Use --proposer rule_based for source files.",
            )

        records, foreign, provider_facts = self._admissible(evidence)
        note = f"{foreign} record(s) dropped as another graph's scenario; " if foreign else ""
        if provider_facts:
            note += (
                f"{provider_facts} record(s) dropped as a provider fact rather than a "
                f"lesson (a `{PROVIDER_FACT_TYPE_PREFIX}*` containment note describes the "
                f"CLI the owner installed, not what this agent did, and a persona cannot "
                f"act on it); "
            )
        if not records:
            return _Decision((), f"{note}no admissible failure record for this graph")

        entries = self._lessons(records)
        if not entries:
            return _Decision((), f"{note}{self._recurrence_reason(records)}")

        body = (
            parse_codex_agent(source, source=path).body
            if path.lower().endswith(".toml")
            else source
        )
        section = _Section.find(body, self.heading)
        present = section.signatures()
        fresh = [e for e in entries if e.signature not in present]
        if not fresh:
            top = entries[0]
            return _Decision(
                (),
                f"{note}every admissible lesson is already in {self.heading!r} — the "
                f"highest-recurrence one, {top.signature!r} "
                f"({top.occurrence_count} run(s)), was appended by an earlier cycle. "
                f"Nothing changed, so there is nothing to propose.",
            )

        entry = fresh[0]
        unwritable = [c for c in _UNWRITABLE_IN_SIGNATURE if c in entry.signature]
        if unwritable or any(ch.isspace() for ch in entry.signature):
            return _Decision(
                (),
                f"{note}the highest-recurrence lesson's signature {entry.signature!r} cannot be "
                f"written into a provenance marker (it contains "
                f"{unwritable or 'whitespace'}), and a bullet whose provenance cannot be read "
                f"back would be appended again every cycle.",
            )

        staleness = {e.signature: e.runs_since_last_seen for e in entries}
        try:
            proposed = section.append(
                body,
                _render_bullet(entry, self.max_bullet_chars),
                max_bullets=self.max_bullets,
                staleness=staleness,
            )
        except _SectionFull as exc:
            return _Decision((), f"{note}{exc}")

        if path.lower().endswith(".toml"):
            proposed = replace_codex_instructions(source, proposed)

        if proposed == source:  # pragma: no cover - `append` never returns the input
            return _Decision((), f"{note}the rendered bullet changed nothing")

        citations = tuple(
            evidence.cite(record_id, detail=f"{entry.signature} recurred")
            for record_id in entry.source_record_ids
            if record_id in evidence.ids
        )
        if not citations:  # pragma: no cover - provenance comes from these records
            return _Decision((), f"{note}the lesson's provenance is not admissible evidence")

        return _Decision(
            (
                Proposal(
                    id=f"{proposal_id}-prompt",
                    path=path,
                    original=source,
                    proposed=proposed,
                    rationale=(
                        f"consolidated lesson {entry.signature!r} recurred in "
                        f"{entry.occurrence_count} distinct run(s) "
                        f"(helpful {entry.helpful} / harmful {entry.harmful}, "
                        f"{entry.runs_since_last_seen} run(s) since last seen); appended "
                        f"verbatim as one bullet under {self.heading!r}. No model was asked; "
                        f"the text and every count are computed from the records."
                    ),
                    grounded_in=citations,
                ),
            ),
            f"appended a bullet for {entry.signature!r}",
        )

    # -- evidence ------------------------------------------------------------

    def _admissible(self, evidence: MemoryEvidence) -> tuple[tuple[MemoryRecord, ...], int, int]:
        """`evidence.records` minus another graph's, minus provider facts.

        `MemoryEvidence` has already removed validation- and holdout-derived
        records; this removes a *train* record belonging to a different
        graph's scenario, which is not a leak but is another agent's lesson,
        and ADR 0110's `agent_id` finding is that merging those is worse than
        having none.

        And it removes a record that describes the PROVIDER rather than the
        run — see `_PROVIDER_FACT_RE`. Returns the two drop counts separately
        because they are two different things for an operator to fix: one is a
        corpus that mixes graphs, the other is a CLI with no system channel.
        """
        kept = evidence.records
        foreign_count = 0
        if self.corpus is not None and self.graph_id is not None:
            foreign = frozenset(
                s.id for s in self.corpus.scenarios if s.graph_id and s.graph_id != self.graph_id
            )
            after = tuple(r for r in kept if r.run_id not in foreign)
            foreign_count = len(kept) - len(after)
            kept = after
        after_facts = tuple(r for r in kept if not _names_a_provider_fact(r))
        return after_facts, foreign_count, len(kept) - len(after_facts)

    def _lessons(self, records: tuple[MemoryRecord, ...]) -> list[KnowledgeEntry]:
        """Consolidate the admissible records and return the failure entries,
        highest recurrence first.

        The consolidator is reused rather than reimplemented: the two-run
        threshold, the one-representative-per-run rule, `runs_since_last_seen`
        and the helpful/harmful tallies are all measured behaviour (ADR
        0110/0116/0118), and a second copy of them here would drift.
        """
        memory = InMemoryMemoryStore()
        for record in records:
            memory.write(record)
        knowledge = InMemoryKnowledgeStore()
        RuleBasedConsolidator(kinds=("failure",)).consolidate(memory, knowledge, agent_id=None)
        entries = knowledge.query("failure", limit=len(records) or 1)
        # Highest recurrence first; ties by most recent, then by signature —
        # one stable total order, so two cycles over one store choose the same
        # lesson. Not two sorts: the second would invert the first's tie-break.
        entries.sort(key=_order)
        return entries

    def _recurrence_reason(self, records: tuple[MemoryRecord, ...]) -> str:
        """Name the recurrence actually seen, so "nothing to propose" is a
        measurement rather than a shrug."""
        runs: dict[str, set[str]] = {}
        unsigned = 0
        for record in records:
            signature = default_signature(record)
            if signature is None or not record.run_id:
                unsigned += 1
                continue
            runs.setdefault(signature, set()).add(record.run_id)
        best = max((len(v) for v in runs.values()), default=0)
        return (
            f"no lesson yet: {len(records)} admissible failure record(s), "
            f"{len(runs)} distinct signature(s), the most recurrent seen in {best} distinct "
            f"run(s) — {DEFAULT_MIN_OCCURRENCES} are required, because one occurrence is an "
            f"episode and not a lesson (ADR 0110)"
            + (
                f"; {unsigned} record(s) carried no signable failing node or run id"
                if unsigned
                else ""
            )
        )


def _names_a_provider_fact(record: MemoryRecord) -> bool:
    """Does this record's text name a `prompt_agent.*` containment fact?

    Every string in `content` is scanned, not just `verbal_feedback`: nothing
    constrains that dict's shape, `rationale` and `grounded_in` are rendered
    from the same state, and a filter that reads one field is a filter one
    refactor away from reading none of the right ones.
    """
    for value in record.content.values():
        if isinstance(value, str) and _PROVIDER_FACT_RE.search(value):
            return True
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and _PROVIDER_FACT_RE.search(item):
                    return True
    return False


def _order(entry: KnowledgeEntry) -> tuple[int, float, str]:
    """Highest recurrence, then most recent, then signature ascending."""
    seen = entry.last_seen.timestamp() if entry.last_seen is not None else float("-inf")
    return (-entry.occurrence_count, -seen, entry.signature)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _render_bullet(entry: KnowledgeEntry, max_chars: int) -> str:
    """One bullet: a provenance marker, then the recorded feedback verbatim.

    The marker is an HTML comment because markdown renders none of it and
    every coding harness passes it through as part of the prompt — so the
    model sees the provenance too, which is the point of provenance.

    The feedback is the entry's own text. Two transformations are applied and
    both are stated: newlines are collapsed to single spaces (a bullet is one
    line, and a multi-line insert would break the list), and the result is
    truncated at `max_chars` with an ellipsis. Nothing is paraphrased.
    """
    text = _lesson_text(entry)
    flattened = " ".join(text.split())
    if len(flattened) > max_chars:
        flattened = flattened[: max_chars - 1].rstrip() + "…"
    return (
        f"- <!-- aef sig={entry.signature} runs={entry.occurrence_count} --> "
        f"{flattened or '(no feedback recorded)'}"
    )


def _lesson_text(entry: KnowledgeEntry) -> str:
    """The entry's lesson text.

    `latest_feedback` is what `RuleBasedConsolidator` stores verbatim from the
    most recent occurrence. `summary` exists only when an LLM summariser ran
    (ADR 0110), and it is deliberately NOT preferred: the verbatim text is the
    record of what was observed, and a paraphrase in its place makes the
    lesson untraceable to its evidence — the same trade ADR 0110 made when it
    kept both texts.
    """
    for key in ("latest_feedback", "summary"):
        value = entry.content.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


class _SectionFull(RuntimeError):
    pass


@dataclass(frozen=True)
class _Section:
    """Where the lessons section is in the file, as line indices.

    `start` is the heading's line, `end` is one past the section's last line
    (the next heading of the same or shallower depth, or EOF). `present` is
    False when the file has no such heading yet.
    """

    lines: tuple[str, ...]
    start: int
    end: int
    present: bool
    heading: str

    @classmethod
    def find(cls, source: str, heading: str) -> _Section:
        lines = tuple(source.splitlines())
        wanted = heading.strip()
        depth = len(_HEADING_RE.match(wanted).group("hashes"))  # type: ignore[union-attr]
        for index, line in enumerate(lines):
            if line.strip() != wanted:
                continue
            end = len(lines)
            for later in range(index + 1, len(lines)):
                match = _HEADING_RE.match(lines[later])
                if match is not None and len(match.group("hashes")) <= depth:
                    end = later
                    break
            return cls(lines=lines, start=index, end=end, present=True, heading=wanted)
        return cls(lines=lines, start=len(lines), end=len(lines), present=False, heading=wanted)

    def bullet_indices(self) -> tuple[int, ...]:
        return tuple(
            i
            for i in range(self.start + 1, self.end)
            if _BULLET_RE.match(self.lines[i]) is not None
        )

    def signatures(self) -> frozenset[str]:
        """Signatures of the bullets this proposer wrote, read back from their
        markers. An owner's bullet has no marker and contributes none."""
        found = set()
        for index in self.bullet_indices():
            match = _MARKER_RE.match(self.lines[index])
            if match is not None:
                found.add(match.group("sig"))
        return frozenset(found)

    def append(
        self,
        source: str,
        bullet: str,
        *,
        max_bullets: int,
        staleness: dict[str, int],
    ) -> str:
        """`source` with `bullet` added, evicting the stalest aef bullet if the
        section would otherwise exceed `max_bullets`.

        Existing bullets are copied byte-for-byte; nothing but an evicted line
        is removed. The file's trailing newline is preserved.
        """
        bullets = list(self.bullet_indices())
        drop: set[int] = set()
        while len(bullets) - len(drop) + 1 > max_bullets:
            evictable = [i for i in bullets if i not in drop and _MARKER_RE.match(self.lines[i])]
            if not evictable:
                raise _SectionFull(
                    f"{self.heading!r} already holds {len(bullets)} bullet(s), the limit is "
                    f"{max_bullets}, and none of them was written by this loop — an "
                    f"owner-written bullet is never evicted. Raise max_bullets or remove one "
                    f"by hand."
                )
            # Stalest first; the EARLIEST bullet breaks a tie, so the section
            # behaves like a queue rather than dropping whatever was written
            # last. A total order, so two cycles evict the same line.
            drop.add(max(evictable, key=lambda i: (self._staleness(i, staleness), -i)))

        kept = [line for i, line in enumerate(self.lines) if i not in drop]
        # Indices shift once lines are dropped; recompute the insertion point
        # against the surviving list rather than against the original.
        offset = sum(1 for i in drop if i < self.end)
        if self.present:
            insert_at = self.end - offset
            # Trailing blank lines belong to the gap before the next heading,
            # not to the list: insert above them so the bullet joins the list.
            while insert_at > self.start + 1 - offset and not kept[insert_at - 1].strip():
                insert_at -= 1
            block = [bullet]
        else:
            insert_at = len(kept)
            while insert_at > 0 and not kept[insert_at - 1].strip():
                insert_at -= 1
            block = ["", self.heading, "", bullet]

        merged = kept[:insert_at] + block + kept[insert_at:]
        text = "\n".join(merged)
        return text + "\n" if source.endswith("\n") or not source else text

    def _staleness(self, index: int, staleness: dict[str, int]) -> int:
        """`runs_since_last_seen` for the bullet's signature.

        A signature the current records no longer support is the stalest thing
        in the file — it is a lesson whose evidence has fallen below the
        two-run threshold — so it is evicted before any live one. Demote,
        never delete, is a *retrieval* policy (ADR 0116); a prompt with a hard
        bullet budget has to drop something, and this is the something.
        """
        match = _MARKER_RE.match(self.lines[index])
        if match is None:  # pragma: no cover - only marked bullets are evictable
            return -1
        return staleness.get(match.group("sig"), 1 << 30)


__all__ = [
    "DEFAULT_MAX_BULLETS",
    "DEFAULT_SECTION_HEADING",
    "MAX_BULLET_CHARS",
    "PROMPT_SUFFIXES",
    "PromptOutsideZoneAError",
    "RuleBasedPromptProposer",
]
