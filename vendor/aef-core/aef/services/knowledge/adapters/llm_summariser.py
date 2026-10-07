"""LLM-backed summarisation for `RuleBasedConsolidator` (ADR 0110, increment
I5) — WikiSkill's consolidation step done by a model instead of by taking the
most recent occurrence verbatim.

**This is possible at all because of ADR 0110's write-time decision.**
`ReplayEngine` re-executes deterministic nodes and asserts their output
matches, so anything a node calls during a replayed run must answer identically
twice. A model does not. Consolidating at WRITE time makes the entry stored
data by the time any retriever reads it, so a non-deterministic summariser
never sits inside a replayed read path. That was the reason for the decision
and this module is the thing it was reserving room for.

**It swaps prose and nothing else.** The grouping, the two-distinct-runs
threshold, the per-run dedupe and the agent keying all stay on the single code
path I4 measured. The summariser is handed a signature and that signature's
records, and may return one string.

No vendor SDK is imported here. It depends on `ModelProvider`, which is the
vendor-neutral surface `FallbackProvider` exists to make substitutable
(constraint #3) — so this file would be legal anywhere, and lives under
`adapters/` because that is where a swappable backend belongs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aef.providers.base import (
    CompletionRequest,
    ModelProvider,
    ModelProviderError,
    ProviderMessage,
)
from aef.services.memory.base import MemoryRecord

# Hard ceiling on the stored summary, in characters. A model returning a wall
# of text would spend a retriever's whole `context_budget_tokens` on one entry
# and crowd out every other lesson — which is precisely the defect I4 measured
# consolidation as REMOVING. Truncated rather than rejected: a slightly clipped
# lesson is still a lesson, while dropping it loses the consolidation.
MAX_SUMMARY_CHARS = 600

# Records shown to the model per call. Bounds the prompt, and is a real ceiling
# on what the summary can reflect rather than only a cost control — stated here
# for the same reason `candidates_per_kind` states it.
MAX_RECORDS_IN_PROMPT = 20

SYSTEM_PROMPT = (
    "You summarise recurring failures observed by a software agent. "
    "Given several records of the SAME recurring failure, write one short "
    "paragraph naming what keeps going wrong and what it has in common across "
    "runs. Be concrete and factual. Describe only what the records show — do "
    "not speculate about causes they do not mention, and do not propose fixes. "
    "Reply with the paragraph only."
)


@dataclass(frozen=True)
class LLMSummariser:
    """A `SummariseFn` backed by a `ModelProvider`.

    Usage: `RuleBasedConsolidator(summarise=LLMSummariser(provider=p, model=m))`.
    """

    provider: ModelProvider
    model: str
    max_tokens: int = 512
    # Zero, so two consolidations of the same records agree as often as the
    # provider allows. This is NOT a determinism guarantee and is not claimed
    # as one — it is why the entry is stored rather than recomputed.
    temperature: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)

    def __call__(self, signature: str, records: list[MemoryRecord]) -> str | None:
        """Return the summary, or `None` to fall back to verbatim feedback.

        **Never raises.** A provider outage during consolidation must not fail
        the run, and must not leave a partially-written or error-shaped lesson
        in the store: returning `None` leaves the rule-based text in place, so
        the worst case is the previous behaviour rather than a corrupted or
        absent entry. Telemetry must never block the thing it observes —
        `factory/rejection_telemetry`-style reasoning, applied here.
        """
        prompt = _build_prompt(signature, records)
        try:
            result = self.provider.complete(
                CompletionRequest(
                    messages=(ProviderMessage(role="user", content=prompt),),
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    metadata=dict(self.metadata),
                )
            )
        except ModelProviderError:
            return None
        except Exception:  # noqa: BLE001
            # An adapter is contracted to raise ModelProviderError, but a
            # summariser that trusted that contract and was wrong would take
            # the agent's run down with it. Consolidation is observability, not
            # the work.
            return None

        text = (result.content or "").strip()
        if not text:
            return None
        return text[:MAX_SUMMARY_CHARS]


def _build_prompt(signature: str, records: list[MemoryRecord]) -> str:
    """One agent's records for ONE signature, oldest first.

    Cross-agent isolation is not enforced here and does not need to be: the
    consolidator groups by `(agent_id, signature)` before calling a summariser,
    so a group is single-agent by construction. Said explicitly because it
    would be an easy thing to assume was checked twice and find was checked
    zero times.
    """
    lines = [f"Recurring failure signature: {signature}", ""]
    shown = records[-MAX_RECORDS_IN_PROMPT:]
    if len(records) > len(shown):
        lines.append(f"(showing the {len(shown)} most recent of {len(records)} occurrences)")
        lines.append("")
    for i, record in enumerate(shown, start=1):
        feedback = record.content.get("verbal_feedback")
        objective = record.content.get("objective")
        lines.append(f"Occurrence {i} (run {record.run_id}):")
        lines.append(f"  objective: {objective}")
        lines.append(f"  observed: {feedback}")
    return "\n".join(lines)
