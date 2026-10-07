"""The null hypothesis for a prose candidate (ADR 0170).

`ControlCohortGenerator` mutates module-level numeric constants. A persona
file has none, so `CohortBuilder` refused to build a cohort for a `.md`
candidate, so G3 refused for want of a null hypothesis — and **a prompt
candidate could be rejected but never accepted** (ADR 0157 defect 2, ADR 0139
requirement 2, ADR 0148's re-measurement of it). That limit is the cohort's,
not the proposer's.

## The control this module builds, and what it asks

`RuleBasedPromptProposer` appends **one bullet** under a `## Lessons (aef)`
heading (ADR 0157). The honest null for that change is therefore:

> does appending an *irrelevant* bullet **of this shape and this length**, in
> this section, help as much as the real one?

So each cohort member is the incumbent persona with one placebo bullet in the
place the treatment bullet went — built by taking the candidate and
**substituting the placebo text for the treatment's**, which is a stronger
form of ADR 0054's "use the same machinery" rule than re-running the appender
would be: the heading, the blank lines, the insertion point and any eviction
the append performed are byte-identical to the candidate's, so the *only*
variable between a control and the candidate is the words in one bullet. The
placebo matches the treatment bullet's **token count** and carries no content
word of it.

## Three designs rejected, and why — because a control is an argument

1. **A lesson drawn from another graph's memory.** The brief's first
   suggestion, and it is not a null: another graph's lesson *was reasoned
   about*, it is merely grounded in different evidence, and real advice can
   transfer. `proposer.py` states what makes the numeric cohort a null — "it
   is what changes that were not reasoned about score" — and a real lesson
   fails that test. It would answer "does any lesson help", which is a
   different and easier question than "does THIS lesson help". (It is also
   unreachable: `CohortBuilder` has no memory store, and giving it one would
   put another graph's records inside the gate.)

2. **The treatment bullet with its content words shuffled.** Rejected on the
   evidence of ADR 0157's own caveat: the lesson that moved a scenario there
   contained the literal string `contains 'VERDICT:'`, and the agent then
   emitted `VERDICT:`. A shuffle preserves every content word, so a placebo
   built that way carries the treatment's active ingredient intact. That is
   not a control, it is the treatment with the word order damaged — the exact
   shape `_assert_no_leak` below exists to refuse.

3. **Deleting the bullet.** That is the *incumbent*, which G3 already scores
   separately, and a cohort of five incumbents has zero variance: p95
   collapses onto the incumbent mean and "beats the cohort" degenerates into
   "beats the incumbent" — precisely the comparison ADR 0051 says proves
   nothing.

## What this control does NOT cover, stated rather than discovered

The placebo controls for the **presence, shape, position and length** of a
bullet. It does not control for the *plausibility of its content*: a null made
of plausible-but-wrong lessons would be a stronger test, and it cannot be
built here, because generating plausible prose needs a model and
`gates/base.py` requires every gate to be deterministic ("a candidate's
acceptance never depends on a model call"). So the residual risk is a prompt
edit that helps only because the agent attends to any confident-sounding
sentence; this cohort would not catch that, and nothing in the harness does.

Scope: this module builds a cohort for a **lessons-section bullet append**,
which is the only prose edit anything in this repo proposes. Any other change
to a prompt file still has no null hypothesis and G3 still refuses — by
`ProseCohortError`, naming what it saw.
"""

from __future__ import annotations

import difflib
import hashlib
import random
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from aef.harness.prompt_proposer import (
    _BULLET_RE,
    _MARKER_RE,
    DEFAULT_SECTION_HEADING,
    MAX_BULLET_CHARS,
    PROMPT_SUFFIXES,
    _Section,
)
from aef.harness.proposer import Proposal, ProposalError

# Task-neutral filler. Every word here is deliberately contentless about any
# objective an agent could be given: no verb the agent could obey, no noun
# naming a domain, no token that could appear in an owner's check. The pool is
# large enough that `size` members drawn from it differ from one another, and
# small enough that a reader can verify by eye that none of it is advice.
NEUTRAL_VOCABULARY: tuple[str, ...] = (
    "placeholder",
    "filler",
    "spacer",
    "padding",
    "blank",
    "neutral",
    "inert",
    "idle",
    "vacant",
    "hollow",
    "plain",
    "quiet",
    "still",
    "empty",
    "nominal",
    "ordinary",
    "unremarkable",
    "routine",
    "generic",
    "arbitrary",
    "nondescript",
    "unrelated",
    "immaterial",
    "incidental",
    "peripheral",
    "tangential",
    "marginal",
    "background",
    "ambient",
    "residual",
    "leftover",
    "surplus",
    "spare",
    "extra",
    "additional",
    "further",
    "another",
    "similar",
    "adjacent",
    "parallel",
)

# Words too common to carry information, excluded from the leak test so an
# ordinary English overlap ("the", "a", "not") is not read as a leaked lesson.
_STOPWORDS: frozenset[str] = frozenset(
    """
    the and for that with this from was were are its it's have has had not but you your they
    them their there then than when what which who whom whose how why into onto out off over
    under about above below after before again more most some such only own same too very can
    will just should now been being does did doing while where both each few nor any all one
    two three
    """.split()
)

# What counts as a content word for the leak test: three letters or more, so a
# shared "a"/"is"/"to" is not evidence of a leak, and long enough that a real
# domain token ("verdict", "credentials", "clearwater") is always caught.
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'_-]{2,}")

# The marker a placebo bullet carries. Deliberately NOT a real signature: a
# reader of a materialised control workspace must be able to tell at a glance
# that this bullet is a control and was never proposed by anything.
CONTROL_SIGNATURE_PREFIX = "control-placebo"


class ProseCohortError(ProposalError):
    """No prose null hypothesis could be built for this candidate."""


class ProseCohortLeakError(ProseCohortError):
    """A cohort member carries the treatment's own content.

    Its own type because this is not "the cohort is small" or "the file is the
    wrong kind" — it is a control that is not a control, and a G3 verdict
    computed against it would be the candidate measured against itself. The
    reward-hacking shape, refused rather than reported.
    """


def _content_words(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS)


def _lesson_text(bullet: str) -> str:
    """A bullet line reduced to the prose a model would read as the lesson.

    The list marker and the `<!-- aef ... -->` provenance comment are shape,
    not content: every bullet in the section carries them, so counting them as
    content would make every placebo look like a leak.
    """
    stripped = _BULLET_RE.sub("", bullet.strip(), count=1)
    return re.sub(r"<!--.*?-->", " ", stripped).strip()


def _assert_no_leak(treatment: str, placebo: str) -> None:
    """Refuse a placebo that shares a content word with the treatment.

    Vacuous when the treatment has no content words of its own — a lesson
    made entirely of stopwords cannot be leaked, and saying so is better than
    pretending the check bound.
    """
    shared = sorted(_content_words(treatment) & _content_words(placebo))
    if shared:
        raise ProseCohortLeakError(
            f"control bullet shares {len(shared)} content word(s) with the candidate's "
            f"own lesson ({', '.join(shared[:6])}): a placebo carrying the treatment's "
            f"text is not a null hypothesis, it is the candidate measured against itself. "
            f"Refusing to build a cohort G3 would read as a threshold."
        )


def _added_bullet(source: str, candidate: str, heading: str) -> str:
    """The one bullet the candidate added under `heading`.

    Also the check that the candidate IS a lessons-section edit: every
    inserted line must be a bullet, the heading, or blank, and every deleted
    line must be a bullet this loop wrote (an eviction — ADR 0157). Anything
    else is a prose change this module has no null for, and it says so instead
    of quietly cohorting the wrong thing.
    """
    source_lines = source.splitlines()
    candidate_lines = candidate.splitlines()
    wanted = heading.strip()

    matcher = difflib.SequenceMatcher(a=source_lines, b=candidate_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        for line in source_lines[i1:i2]:
            if _MARKER_RE.match(line) is None:
                raise ProseCohortError(
                    f"the candidate removed {line.strip()!r}, which this loop did not write. "
                    f"A prose control cohort is defined for an append under {wanted!r}; "
                    f"there is no null hypothesis for an arbitrary rewrite of a persona."
                )
        for line in candidate_lines[j1:j2]:
            if line.strip() and line.strip() != wanted and _BULLET_RE.match(line) is None:
                raise ProseCohortError(
                    f"the candidate added {line.strip()!r}, which is neither a bullet nor "
                    f"{wanted!r}. A prose control cohort is defined for an append under that "
                    f"heading; there is no null hypothesis for an arbitrary rewrite."
                )

    before = _Section.find(source, heading)
    after = _Section.find(candidate, heading)
    added = Counter(after.lines[i] for i in after.bullet_indices()) - Counter(
        before.lines[i] for i in before.bullet_indices()
    )
    bullets = list(added.elements())
    if len(bullets) != 1:
        raise ProseCohortError(
            f"expected the candidate to add exactly one bullet under {wanted!r}; it added "
            f"{len(bullets)}. The prose null hypothesis is 'an irrelevant bullet of the same "
            f"shape', and it is undefined for a candidate that added none or several."
        )
    return bullets[0]


@dataclass(frozen=True)
class ProseControlCohortGenerator:
    """Length-matched, information-free placebo bullets.

    Seeded from the generator's own seed **and the candidate's digest**, so
    the cohort a candidate was measured against can be re-derived from the
    candidate alone — the same auditability rule `ControlCohortGenerator`
    states for the numeric cohort.
    """

    seed: int = 0
    heading: str = DEFAULT_SECTION_HEADING
    max_bullet_chars: int = MAX_BULLET_CHARS
    vocabulary: tuple[str, ...] = field(default_factory=lambda: NEUTRAL_VOCABULARY)

    def generate(
        self,
        *,
        path: str,
        source: str,
        candidate: str,
        size: int,
    ) -> tuple[Proposal, ...]:
        """`size` distinct placebo variants of `source`.

        `source` is the **incumbent** persona and `candidate` is the proposed
        one; the cohort mutates the incumbent, for the reason `suite.py`
        records at length — a cohort drawn from the candidate asks whether
        random *further* changes match it, and the threshold rises to meet the
        candidate so nothing can ever pass.
        """
        if size < 1:
            raise ProseCohortError(f"control cohort size must be at least 1, got {size}")
        if not path.lower().endswith(PROMPT_SUFFIXES):
            raise ProseCohortError(
                f"cannot build a prose control cohort for {path!r}: this cohort appends a "
                f"markdown bullet and is defined for {', '.join(PROMPT_SUFFIXES)} only"
            )

        treatment_bullet = _added_bullet(source, candidate, self.heading)
        treatment = _lesson_text(treatment_bullet)
        tokens = len(treatment.split())
        if tokens < 1:
            raise ProseCohortError(
                f"the candidate's bullet {treatment_bullet.strip()!r} carries no lesson text, "
                f"so there is no length to match and nothing to control for"
            )

        pool = tuple(w for w in self.vocabulary if w not in _content_words(treatment))
        if len(pool) < 2:
            raise ProseCohortError(
                f"the neutral vocabulary shares all but {len(pool)} word(s) with the "
                f"candidate's lesson, so no information-free bullet of matching length can "
                f"be drawn from it"
            )

        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        rng = random.Random(f"{self.seed}:{digest}")

        cohort: list[Proposal] = []
        seen: set[str] = set()
        attempts = 0
        while len(cohort) < size and attempts < size * 40:
            attempts += 1
            text = " ".join(rng.choice(pool) for _ in range(tokens))
            # Refused, never repaired: a placebo that reached the treatment's
            # vocabulary is a bug in this generator, and quietly redrawing
            # would hide it.
            _assert_no_leak(treatment, text)
            bullet = _render_control_bullet(len(cohort), text, self.max_bullet_chars)
            proposed = _substitute(candidate, treatment_bullet, bullet)
            if proposed in seen or proposed == source:
                continue
            seen.add(proposed)
            cohort.append(
                Proposal(
                    id=f"control-prose-{self.seed}-{len(cohort)}",
                    path=path,
                    original=source,
                    proposed=proposed,
                    rationale="",
                    grounded_in=(),
                    # Ungrounded BY DESIGN — that is the null hypothesis.
                    is_control=True,
                )
            )

        if len(cohort) < size:  # pragma: no cover - 40n draws from a pool of >=2
            raise ProseCohortError(
                f"could only generate {len(cohort)} of {size} DISTINCT placebo bullets for "
                f"{path!r}; a short cohort would silently weaken G3's threshold and a cohort "
                f"of repeats is a point mass rather than a distribution"
            )
        return tuple(cohort)


def _substitute(candidate: str, treatment_bullet: str, placebo_bullet: str) -> str:
    """The candidate with its one bullet's text swapped for a placebo's.

    Line-for-line: everything the append did to the file — the heading it may
    have created, the blank lines it stepped over, the bullet it may have
    evicted — is preserved exactly, so the control differs from the candidate
    in one line and in nothing else.
    """
    lines = candidate.splitlines()
    index = lines.index(treatment_bullet)  # it came from this list
    lines[index] = placebo_bullet
    text = "\n".join(lines)
    return text + "\n" if candidate.endswith("\n") else text


def _render_control_bullet(index: int, text: str, max_chars: int) -> str:
    """A placebo bullet with the treatment's shape and none of its content."""
    flattened = " ".join(text.split())
    if len(flattened) > max_chars:
        flattened = flattened[: max_chars - 1].rstrip() + "…"
    return f"- <!-- aef sig={CONTROL_SIGNATURE_PREFIX}-{index} runs=2 --> {flattened}"


def prose_cohort_targets(paths: Sequence[str]) -> tuple[str, ...]:
    """The prompt files among `paths`, in the order given."""
    return tuple(p for p in paths if p.lower().endswith(PROMPT_SUFFIXES))


__all__ = [
    "CONTROL_SIGNATURE_PREFIX",
    "NEUTRAL_VOCABULARY",
    "ProseCohortError",
    "ProseCohortLeakError",
    "ProseControlCohortGenerator",
    "prose_cohort_targets",
]
