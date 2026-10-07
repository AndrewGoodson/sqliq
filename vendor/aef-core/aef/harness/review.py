"""The review surface — what a non-expert owner actually reads.

ADR 0045 moved the human gate from every change to the decisions gates
cannot make, and said escalations must be "phrased as decisions a non-expert
can make, never as code diffs". This module is where that is honoured or
quietly broken.

So the report leads with **the decision being asked for**, then the evidence
with its numbers, and puts the diff last. A report that opens with a diff
invites the rubber stamp the tiered policy exists to eliminate — the owner
scrolls, sees code they cannot evaluate, and approves.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from enum import StrEnum

from aef.harness.gates.base import PipelineResult
from aef.harness.proposer import Proposal


class Disposition(StrEnum):
    AUTO_MERGE = "auto_merge"
    ESCALATE = "escalate"
    REJECT = "reject"


@dataclass(frozen=True)
class Decision:
    disposition: Disposition
    reason: str
    question: str = ""  # only for ESCALATE — the thing the owner must answer


def decide(result: PipelineResult, *, tier1_enabled: bool) -> Decision:
    """Map a pipeline result onto a disposition.

    `tier1_enabled` defaults off everywhere it is called, and must stay off
    until M10's post-merge monitoring exists (ADR 0045). With no human in the
    merge path and nothing watching afterwards, an auto-merge is unobserved
    in both directions.
    """
    if result.security_events:
        return Decision(
            disposition=Disposition.REJECT,
            reason=(
                "a proposal reached for the harness, its own tests, or a safety "
                "declaration — this is a security event, not a candidate"
            ),
        )
    if not result.passed:
        failed = result.failed_at
        return Decision(
            disposition=Disposition.REJECT,
            reason=f"{failed.gate} rejected it: {failed.reason}" if failed else "no gates ran",
        )
    if not tier1_enabled:
        return Decision(
            disposition=Disposition.ESCALATE,
            reason="every gate passed, but Tier-1 auto-merge is not enabled",
            question=(
                "All automated checks passed. Post-merge monitoring is not yet in place, so "
                "nothing would notice if this turned out badly after merging. Merge anyway, "
                "or hold until monitoring exists?"
            ),
        )
    return Decision(
        disposition=Disposition.AUTO_MERGE,
        reason=f"all {len(result.results)} gate(s) passed within budget",
    )


def render_diff(proposal: Proposal) -> str:
    return "".join(
        difflib.unified_diff(
            proposal.original.splitlines(keepends=True),
            proposal.proposed.splitlines(keepends=True),
            fromfile=f"a/{proposal.path}",
            tofile=f"b/{proposal.path}",
        )
    )


def render_report(proposal: Proposal, result: PipelineResult, decision: Decision) -> str:
    """A report ordered decision-first, evidence-second, diff-last."""
    lines: list[str] = [f"# Proposal {proposal.id}", ""]

    lines += ["## What is being asked", ""]
    if decision.disposition is Disposition.ESCALATE:
        lines += [decision.question or decision.reason, ""]
    else:
        lines += [f"**{decision.disposition.value}** — {decision.reason}", ""]

    lines += ["## Why it was proposed", "", proposal.rationale or "(no rationale)", ""]

    lines += ["## Evidence it was based on", ""]
    if proposal.grounded_in:
        lines += [f"- {citation}" for citation in proposal.grounded_in]
    else:
        # Only reachable for a control-cohort member; a real proposal cannot
        # be constructed without citations (ADR 0054).
        lines.append("- none (control-cohort member)")
    lines.append("")

    lines += ["## What the checks found", ""]
    if not result.results:
        lines.append("No gates ran. That is not the same as passing.")
    for gate_result in result.results:
        mark = "PASS" if gate_result.passed else "FAIL"
        lines.append(f"- **{gate_result.gate} {mark}** — {gate_result.reason}")
        lines += [f"    - {item}" for item in gate_result.evidence]
    lines.append("")

    lines += ["## The change itself", "", "```diff", render_diff(proposal).rstrip("\n"), "```", ""]
    return "\n".join(lines)
