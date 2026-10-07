"""An LLM proposer with the same contract as the rule-based one (ADR 0122).

`RuleBasedProposer` has one structural idea and a numeric step; from any
parent it emits exactly one candidate, and ADR 0121 measured what that
costs: an archive with nothing to sample. This module gives the loop a
proposer with a repertoire, under three rules carried over from every other
model-in-the-loop slice in this repo (ADR 0110, 0115):

**The model writes prose and a file; code decides everything else.** The
citations are computed from `MemoryEvidence` — never claimed by the model —
and go through the same `_check_citations` the rule-based proposer uses, so
a proposal citing validation or holdout is refused before a model is asked
anything. The reply is validated in code: exactly one fenced block, parses
as Python, addressed to the same Zone A path, within G0's line budget, no
finding G0's own scanner would raise (the import allowlist is IMPORTED from
G0, not copied), no owner-only declaration touched (G4's list, through the
transformation catalogue's own check), and a non-empty diff.

**Any failure falls back to the rule-based proposer and says so.** A model
outage, an unparseable reply, a reply that reaches for `subprocess` — each
is a reason in the rationale of the rule-based proposal that replaces it,
so a reader of the ledger can see the model was asked and what it did.

**And when the fallback proposes nothing, the spend is still reported.** The
rationale was the *only* place the rejection was written down, so on a repo
where the fallback has nothing to say — a prompt file, where the rule-based
proposer edits numeric constants and a `.md` has none — one live call was
spent, the reply was discarded, and `aef loop cycle` printed a sentence about
markdown that never mentioned the model (ADR 0170 defect 3, reported as ADR
0157 defect 5). `ProposerSpend` below is where that now lives: it is mutable,
deliberately, because a model call is an event in time and not a function of
the inputs — nothing about the *proposal* is derived from it.

**No gate changes.** Everything validated here is validated again by the
gates on the candidate branch; this is the proposer declining to spend N+2
corpus passes on a candidate G0 would reject in milliseconds, not a second
copy of the gates.

Vendor isolation (constraint #3): imports `ModelProvider` from
`aef.providers.base`, never a vendor SDK.
"""

from __future__ import annotations

import ast
import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from aef.harness.gates.g0_static_safety import (
    DEFAULT_IMPORT_ALLOWLIST,
    DEFAULT_MAX_CHANGED_LINES,
    FORBIDDEN_AEF_SUBPACKAGES,
    scan_source,
)
from aef.harness.gates.g4_separation import OWNER_ONLY_FIELDS
from aef.harness.proposer import (
    Citation,
    MemoryEvidence,
    Proposal,
    ProposalError,
    RuleBasedProposer,
    UngroundedProposalError,
    _check_citations,
)
from aef.harness.transformations import TransformationError, _assert_controls_untouched
from aef.harness.zones import ZonePolicy, inspect_path
from aef.providers.base import (
    CompletionRequest,
    ModelProvider,
    ModelProviderError,
    ProviderMessage,
)

DEFAULT_MAX_TOKENS = 16000
MAX_PROSE_CHARS = 600
MAX_FEEDBACK_CHARS = 240

_FENCE_RE = re.compile(r"```([^\n]*)\n(.*?)\n?```", re.DOTALL)

SYSTEM_PROMPT = (
    "You are the proposer in a gated self-improvement loop for a Python agent graph. "
    "You are given ONE agent source file, the recorded failures of runs of that agent, "
    "and the node(s) those failures blame. Propose one coherent change to the file that "
    "addresses the recorded failures. Explain it in at most 120 words of plain prose, "
    "then return the WHOLE modified file in exactly one fenced code block whose opening "
    "fence is ```python <path>. Rules the gates enforce, so a reply that breaks one is "
    "discarded: keep the node signature (state, ctx, services) -> (StateDelta, route); "
    "import only from these top-level modules: {allowlist} (and never from {forbidden}); "
    "never use eval, exec, compile, __import__, or reach interpreter internals; do not "
    "add, remove or change any of these declarations: {owner_only}; change at most "
    "{max_lines} lines; do not rename or move the file. Refer only to the evidence shown."
)


class LLMProposalRejected(ProposalError):
    """The model's reply failed validation. Its own type so the fallback can
    catch exactly this and a citation violation, which is never recoverable,
    keeps propagating."""


@dataclass
class ProposerSpend:
    """What asking the model cost, and what came of it.

    Mutable and shared with the proposer that owns it — the one piece of state
    in this module, and it holds no influence over any proposal: nothing reads
    it back, the gates never see it, and clearing it changes no candidate. It
    exists so that a call the loop *spent* cannot vanish from the record when
    the thing that spent it produced nothing.

    `calls` counts **attempts**, incremented before the provider returns,
    because a request that errors after it was sent has still been spent. It
    is a floor on the true cost, never an under-report.
    """

    calls: int = 0
    rejections: tuple[str, ...] = ()

    def record_attempt(self) -> None:
        self.calls += 1

    def record_rejection(self, exc: BaseException) -> None:
        self.rejections = (*self.rejections, f"{type(exc).__name__}: {exc}")

    def note(self) -> str:
        """One line for the cycle summary and the journal, or "" when the
        model was never asked. Only the FIRST rejection is rendered: a cycle
        asks once, and a list would invite reading a retry loop into it."""
        if not self.calls:
            return ""
        plural = "" if self.calls == 1 else "s"
        why = f"; rejected — {self.rejections[0]}" if self.rejections else ""
        return f"llm proposer spent {self.calls} live model call{plural}{why}"


@dataclass(frozen=True)
class _Reply:
    prose: str
    fence_info: str
    body: str


def _parse_reply(text: str) -> _Reply:
    blocks = list(_FENCE_RE.finditer(text))
    if len(blocks) != 1:
        raise LLMProposalRejected(
            f"expected exactly one fenced code block in the reply, found {len(blocks)}"
        )
    block = blocks[0]
    prose = (text[: block.start()] + text[block.end() :]).strip()
    body = block.group(2)
    if not body.endswith("\n"):
        body += "\n"
    return _Reply(prose=prose[:MAX_PROSE_CHARS], fence_info=block.group(1).strip(), body=body)


def _changed_lines(original: str, proposed: str) -> int:
    """Added plus removed lines, the way `CandidateDiff.changed_lines` sums
    git's numstat, so the budget applied here is the budget G0 applies."""
    added = removed = 0
    for line in difflib.unified_diff(
        original.splitlines(), proposed.splitlines(), lineterm="", n=0
    ):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added + removed


@dataclass(frozen=True)
class LLMProposer:
    """Prose and a file from the model; citations, validation and the
    fallback from code."""

    provider: ModelProvider
    model: str
    fallback: RuleBasedProposer = field(default_factory=RuleBasedProposer)
    zone_policy: ZonePolicy = field(default_factory=ZonePolicy)
    # G0's own values. Overridable so a `gate_limits` override reaches the
    # proposer too; the DEFAULTS are imported, never restated.
    import_allowlist: frozenset[str] = DEFAULT_IMPORT_ALLOWLIST
    max_changed_lines: int = DEFAULT_MAX_CHANGED_LINES
    max_tokens: int = DEFAULT_MAX_TOKENS
    # Per-proposer, not per-call: one `cycle` builds one proposer and asks it
    # once, so this accumulates that turn's spend and nothing else.
    spend: ProposerSpend = field(default_factory=ProposerSpend)

    def propose_from_memory(
        self,
        evidence: MemoryEvidence,
        *,
        proposal_id: str,
        path: str,
        source: str,
    ) -> tuple[Proposal, ...]:
        """Same contract as `RuleBasedProposer.propose_from_memory`: nothing
        from no evidence; otherwise one grounded proposal — the model's when
        it validates, the rule-based proposer's (saying why) when it does not."""
        citations = evidence.citations()
        if not citations:
            return ()
        failing = tuple(node for node, _ in evidence.failing_nodes())
        try:
            return (
                self.propose(
                    proposal_id=proposal_id,
                    path=path,
                    source=source,
                    citations=citations,
                    failing_nodes=failing,
                ),
            )
        except (LLMProposalRejected, ModelProviderError) as exc:
            # Recorded HERE, not only in the rationale below: when the
            # fallback also proposes nothing there is no rationale to carry
            # it, and the call disappeared (ADR 0170 defect 3).
            self.spend.record_rejection(exc)
            reason = f"[llm proposer fell back to rule-based: {exc}]"
            return tuple(
                replace(p, rationale=f"{p.rationale} {reason}")
                for p in self.fallback.propose_from_memory(
                    evidence, proposal_id=proposal_id, path=path, source=source
                )
            )

    def propose(
        self,
        *,
        proposal_id: str,
        path: str,
        source: str,
        citations: Sequence[Citation],
        failing_nodes: Sequence[str] = (),
    ) -> Proposal:
        """Ask once; validate in code; raise rather than emit anything doubtful.

        Order matters: the citation check and the zone check run BEFORE the
        model is called, because a proposal that would cite the holdout or
        target the harness is not a proposal to improve, it is one to refuse —
        and a model call spent on it is a call spent on nothing.
        """
        _check_citations(citations)
        if not citations:
            raise UngroundedProposalError(
                f"refusing to propose against {path!r} with no evidence — the proposer does "
                f"not speculate"
            )
        zone = inspect_path(path, self.zone_policy)
        if not zone.allowed:
            raise ProposalError(
                f"refusing to propose against {path!r}: {zone.reason}. Only Zone A is "
                f"agent-writable, and a model is not asked to write anywhere else."
            )

        reply = self._ask(path, source, citations, failing_nodes)
        proposed = self._validate(path, source, reply)
        evidence_line = "; ".join(str(c) for c in citations)
        prose = reply.prose or "(the model gave no rationale)"
        return Proposal(
            id=f"{proposal_id}-llm",
            path=path,
            original=source,
            proposed=proposed,
            # Added, not substituted (ADR 0110): the computed citation list is
            # what the prose must stay traceable to.
            rationale=f"{prose}\n\nEvidence: {evidence_line}",
            grounded_in=tuple(citations),
        )

    # -- the model call ------------------------------------------------------

    def _ask(
        self,
        path: str,
        source: str,
        citations: Sequence[Citation],
        failing_nodes: Sequence[str],
    ) -> _Reply:
        system = SYSTEM_PROMPT.format(
            allowlist=", ".join(sorted(self.import_allowlist)),
            forbidden=", ".join(FORBIDDEN_AEF_SUBPACKAGES),
            owner_only=", ".join(sorted(OWNER_ONLY_FIELDS)),
            max_lines=self.max_changed_lines,
        )
        feedback = "\n".join(
            f"- {c.source}: {(c.detail or '(no feedback recorded)')[:MAX_FEEDBACK_CHARS]}"
            for c in citations
        )
        nodes = ", ".join(failing_nodes) if failing_nodes else "(none recorded)"
        user = (
            f"File: {path}\n\nRecorded failures (memory records, train-admissible only):\n"
            f"{feedback}\n\nNodes the failures blame: {nodes}\n\n"
            f"Current source:\n```python {path}\n{source}```\n\n"
            f"Propose the change."
        )
        # Counted before the provider answers: a request that errors after it
        # left has still been spent, and the number this loop reports must be
        # a floor on the real cost rather than a count of successes.
        self.spend.record_attempt()
        result = self.provider.complete(
            CompletionRequest(
                messages=(
                    ProviderMessage(role="system", content=system),
                    ProviderMessage(role="user", content=user),
                ),
                model=self.model,
                max_tokens=self.max_tokens,
            )
        )
        text = (result.content or "").strip()
        if not text:
            raise LLMProposalRejected("the model returned nothing")
        return _parse_reply(text)

    # -- validation, all in code ---------------------------------------------

    def _validate(self, path: str, source: str, reply: _Reply) -> str:
        proposed = reply.body

        # Same path. The write target is `path` by construction — the model
        # has no channel to redirect it — but a reply that NAMES a different
        # file was answering a different question.
        named = reply.fence_info.split(None, 1)[1].strip() if " " in reply.fence_info else ""
        if named and named != path:
            raise LLMProposalRejected(
                f"the reply is addressed to {named!r}, not to {path!r}; refusing to write "
                f"one file's content to another"
            )

        try:
            ast.parse(proposed, filename=path)
        except SyntaxError as exc:
            raise LLMProposalRejected(
                f"the reply does not parse as Python: line {exc.lineno}: {exc.msg}"
            ) from exc

        if proposed == source:
            raise LLMProposalRejected("the reply is identical to the current source (empty diff)")

        changed = _changed_lines(source, proposed)
        if changed > self.max_changed_lines:
            raise LLMProposalRejected(
                f"{changed} changed lines exceeds G0's budget of {self.max_changed_lines}"
            )

        # G0's scanner, on the reply, minus anything the incumbent already
        # carried: the proposer refuses what G0 would refuse and nothing else.
        before = {f.problem for f in scan_source(path, source, self.import_allowlist)}
        new = [
            f for f in scan_source(path, proposed, self.import_allowlist) if f.problem not in before
        ]
        if new:
            raise LLMProposalRejected(
                f"G0 would reject the reply: {'; '.join(str(f) for f in new[:3])}"
            )

        # G4's owner-only declarations, through the catalogue's own check.
        try:
            _assert_controls_untouched(source, proposed)
        except TransformationError as exc:
            raise LLMProposalRejected(str(exc)) from exc

        return proposed
