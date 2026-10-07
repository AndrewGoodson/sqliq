"""Skill proposals — WikiSkill's third layer with the self-modification
removed (ADR 0117).

WikiSkill's loop is experience -> wiki -> executable skills, and its skill
updater rewrites the agent's skills at runtime. That third step is
`aef/evolution/` here, and it is hard-disabled (constraint #7): the trust
case's three findings are untouched by anything in this module. What this
module does instead is the governed version: it renders each well-evidenced
`KnowledgeEntry` as a DRAFT skill file under a proposals directory, for a
person to read, edit, and move into `.claude/skills/` by hand.

Three properties, each asserted by a test:

- **Never writes under `.claude/`, `agents/`, or anywhere a harness reads.**
  The output directory is the caller's, and a draft that landed where a
  coding agent loads skills would be a runtime self-modification with extra
  steps.
- **Never overwrites.** A proposal a person has already edited is theirs.
- **Every claim in the draft is computed from the entry.** Occurrence
  counts, run ids, first/last seen and provenance come from the store; the
  lesson text is the entry's own. Nothing here calls a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from aef.services.knowledge.base import KnowledgeEntry, KnowledgeStore

# Where a harness loads skills or agents from. A draft must never land here.
HARNESS_DIRS: tuple[str, ...] = (".claude", ".cursor", ".github", "agents")
DEFAULT_MIN_OCCURRENCES = 3


class SkillProposalError(RuntimeError):
    pass


@dataclass(frozen=True)
class SkillProposal:
    slug: str
    path: Path
    entry: KnowledgeEntry
    written: bool  # False when a draft already existed and was left alone


def slug_for(entry: KnowledgeEntry) -> str:
    """`failure:fetch>parse` -> `failure-fetch-parse`. Stable per entry key,
    so a re-run finds its own earlier draft rather than writing a second."""
    raw = f"{entry.kind}-{entry.signature.split(':', 1)[-1]}"
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return slug or "lesson"


def render_skill(entry: KnowledgeEntry) -> str:
    content = entry.content
    lesson = content.get("summary") or content.get("latest_feedback") or "(no lesson text)"
    failing = content.get("failing_nodes") or []
    runs = content.get("run_ids") or []
    first = entry.first_seen.isoformat() if entry.first_seen else "unknown"
    last = entry.last_seen.isoformat() if entry.last_seen else "unknown"
    nodes = ", ".join(str(n) for n in failing) if failing else "n/a"
    lines = [
        "---",
        f"name: {slug_for(entry)}",
        f"description: PROPOSED, not adopted — a lesson consolidated from {entry.occurrence_count} "
        f"run(s) of agent {entry.agent_id or 'any'}; failing node(s): {nodes}. "
        "Review, edit, then move into .claude/skills/ yourself.",
        "---",
        "",
        f"# {entry.signature}",
        "",
        "**Status: proposal.** Written by `aef loop skills` from the knowledge store.",
        "Nothing loads this file until a person moves it under `.claude/skills/`.",
        "",
        "## Lesson",
        "",
        str(lesson),
        "",
        "## Evidence (computed from the store, not written by a model)",
        "",
        f"- kind: {entry.kind}",
        f"- occurrences (distinct runs): {entry.occurrence_count}",
        f"- confidence: {entry.confidence:.2f}",
        f"- runs since last seen: {entry.runs_since_last_seen}",
        f"- in context and the run avoided this failure (helpful): {entry.helpful}",
        f"- in context and this failure recurred anyway (harmful): {entry.harmful}",
        f"- in context, this failure resolved, another failed (harmful elsewhere): "
        f"{entry.harmful_elsewhere}",
        f"- first seen: {first}",
        f"- last seen: {last}",
        f"- run ids: {', '.join(str(r) for r in runs) if runs else 'n/a'}",
        f"- source record ids: {', '.join(entry.source_record_ids)}",
        "",
        "## Before adopting",
        "",
        "- Does the lesson still apply? `runs since last seen` above says how long it has",
        "  gone without recurring.",
        "- Is the fix a skill (guidance for the agent) or a code change (a candidate for",
        "  `aef loop run`)? A skill tells the agent what to do; it does not change what",
        "  the graph can do.",
        "",
    ]
    return "\n".join(lines)


def propose_skills(
    knowledge: KnowledgeStore,
    out_dir: Path,
    *,
    agent_id: str | None,
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
    kinds: tuple[str, ...] = ("failure",),
) -> tuple[SkillProposal, ...]:
    """Draft one skill per entry at or above `min_occurrences`, never
    overwriting, never under a harness directory."""
    out_dir = out_dir.resolve()
    for part in out_dir.parts:
        if part in HARNESS_DIRS:
            raise SkillProposalError(
                f"refusing to write skill proposals under {part!r} ({out_dir}): a draft that "
                f"lands where a harness loads skills is a runtime self-modification, which "
                f"aef/evolution/ is disabled to prevent. Use a proposals directory."
            )
    proposals: list[SkillProposal] = []
    for kind in kinds:
        for entry in knowledge.query(
            kind,  # type: ignore[arg-type]
            agent_id=agent_id,
            min_occurrences=min_occurrences,
            limit=1000,
        ):
            slug = slug_for(entry)
            path = out_dir / slug / "SKILL.md"
            if path.exists():
                proposals.append(SkillProposal(slug=slug, path=path, entry=entry, written=False))
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render_skill(entry), encoding="utf-8")
            proposals.append(SkillProposal(slug=slug, path=path, entry=entry, written=True))
    return tuple(proposals)
