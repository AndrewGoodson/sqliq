"""LLM-backed Critic and Judge (ADR 0115) — the Phase-3 slice ADR 0046
deferred, buildable now because ADR 0112 gave the runtime a model that needs
no key.

Two rules carried over from the summariser (ADR 0110), because they are
what keeps a model in the loop honest:

**The model writes prose; code computes every counted field.** The critic's
`grounded_in` is `failure_signals(state)` — the same citations the
rule-based critic emits — never something the model claimed to have seen.
The judge's `score` is the rule-based weighted mean over per-term scores the
model returned, with the rule-based anti-omission rule intact: a rubric term
the model did not score counts as 0.0. A model that returns nothing usable
falls back to the rule-based implementation and says so in the rationale.

**The judge sees the answer.** Evidence is the errors, the tool results, the
scores, the reflections — and the string-valued `working_memory` entries,
which on a content task is where the answer is (ADR 0126). Without them the
judges were scoring a run they could not read.

The numbers that used to be quoted in this docstring now live where they can
be re-run: **`docs/research/i14/` and `docs/research/i14b/` — the scripts, the
raw judgments and the reports** (ADRs 0159, 0171, 0202). Read them before
citing an agreement figure.

Three findings from them belong here, because each one changes what an
agreement number is worth:

- ADR 0159: with the answer in evidence the LLM judge's scores rose from the
  blind run's 0.23–0.50 to 0.82–0.90 — but on a corpus that is 15/18 pass,
  where answering "pass" to everything scores 15/18 and the judge scored
  exactly that. **AUC 0.322.**
- ADR 0171: on a corpus with seven true negatives, AUC 1.000 — of ONE failure
  family, word-cap overruns, which `len(summary.split()) > cap` detects
  without a model.
- ADR 0202: on a set built so that a word count cannot grade it — twelve
  matched cases, six content failures (wrong unit, superseded figure, swapped
  entities, fabricated reason, dropped condition, unsupported claim), every
  case WITHIN its cap so pass-everything, fail-everything and a word counter
  all score 6/12 — this judge agrees with the owner's labels **12/12, AUC
  1.000, and scores the correct summary above the corrupted one in 6 of 6
  matched pairs**, with a maximum position delta of 0.100. The corpus's own
  regex checks score 9/12 on the same set, so the judge is measured against
  the non-model instrument it would replace and not only against chance.
  Its negatives are AUTHORED, though: this agent does not produce them (ADR
  0171 tried to record some and got none), so that is a judge measurement and
  never an agent failure rate.

**And a judge that ranks two model outputs against each other** —
`PairwiseRanker` (ADR 0162). It is separate from `LLMJudge` because grading one
state and choosing between two are different questions, and because only the
second one can exhibit self-preference: until it existed, the rubric's
dimension-3 requirement for a self-preference control had nothing to attach to.
It ships with the control on by default (`allow_self_ranking=False`), for a
measured reason — see the class docstring's table and
`docs/research/j4/`.

**Bias controls are structural, not requested.** The evidence the judge sees
is capped per item (`MAX_EXCERPT_CHARS`, and `MAX_ANSWER_CHARS` for the
answer class) so a longer failure cannot read as a
worse or a better one by bulk alone; and the judge asks twice with the
evidence in opposite orders and averages — a position-swap control for
single-item grading — reporting the disagreement as `position_delta` so a
judge that scores the same state differently depending on what it read
first is visible rather than averaged away silently.

Vendor isolation (constraint #3) holds: this module imports `ModelProvider`,
never a vendor SDK.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from aef.providers.base import (
    CompletionRequest,
    ModelProvider,
    ModelProviderError,
    ProviderMessage,
)
from aef.reasoning.reflection import Critic, Critique, Judge, Judgment
from aef.reasoning.rule_based_reflection import (
    MAX_EXCERPT_CHARS,
    RuleBasedCritic,
    RuleBasedJudge,
    failure_signals,
)
from aef.state import AEFState

MAX_EVIDENCE_ITEMS = 12
DEFAULT_MAX_TOKENS = 2000

# `working_memory` strings are excerpted at a HIGHER cap than everything else,
# and the reason is that they are not corroborating evidence — on a content
# task the answer lives here, and it is the thing being scored (ADR 0126).
# `MAX_EXCERPT_CHARS` is 160; a 35-word summary is roughly 250 characters, so
# the shared cap would hand the judge the first two thirds of every answer and
# ask it to score completeness. 600 covers the corpus's longest answer with
# headroom and is still a cap: an unbounded working memory cannot flood the
# prompt, and every entry in this class is cut at the same length, so bulk
# still cannot read as quality WITHIN the class.
MAX_ANSWER_CHARS = 600
# ... and only the first few, so a state carrying many strings cannot crowd
# the errors that explain the answer out of the total item cap.
MAX_WORKING_MEMORY_ITEMS = 4

CRITIC_SYSTEM = (
    "You are the critic in an agent's reflection step. You are given the objective, "
    "the recorded errors and tool results of one run, and nothing else. Write a short "
    "verbal critique: what went wrong, the most likely cause, and one concrete change "
    "worth trying next run. Refer only to evidence shown. Do not invent errors, tools, "
    "or outcomes that are not listed. Plain prose, at most 120 words."
)

JUDGE_SYSTEM = (
    "You are the judge in an agent's reflection step. Score ONE run against a rubric. "
    "Reply with a single JSON object mapping each rubric term to a number from 0.0 to "
    "1.0 and nothing else. Length of the evidence is not quality: a run with more "
    "recorded output is not better or worse for having more of it. Score only what "
    "the evidence supports."
)


@dataclass(frozen=True)
class _Evidence:
    items: tuple[str, ...]  # already excerpted; order is the caller's

    def render(self, *, reverse: bool) -> str:
        ordered = tuple(reversed(self.items)) if reverse else self.items
        return "\n".join(f"- {item}" for item in ordered) if ordered else "- (none)"


def _excerpt(value: object, limit: int = MAX_EXCERPT_CHARS) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _evidence(state: AEFState) -> _Evidence:
    items: list[str] = []
    # First, because it is what the run produced. ADR 0123's A/B found both
    # judges scoring a run they could not read — neither's evidence contained
    # the answer — and ADR 0159 re-ran that A/B on this code, live, with the
    # script and every judgment committed under `docs/research/i14/`.
    # Every counted field is still computed from the state; this only lets
    # the model see what it is being asked about. Sorted by key so two runs
    # with the same memory render the same prompt.
    strings = [
        (key, value)
        for key, value in sorted(state.working_memory.items(), key=lambda kv: kv[0])
        if isinstance(value, str) and value
    ]
    for key, value in strings[:MAX_WORKING_MEMORY_ITEMS]:
        items.append(f"working_memory[{key}]: {_excerpt(value, MAX_ANSWER_CHARS)}")
    for i, error in enumerate(state.errors):
        detail = error.get("error") or error.get("message") or error
        items.append(f"errors[{i}] ({error.get('node_id', '?')}): {_excerpt(detail)}")
    for i, result in enumerate(state.tool_results):
        status = "FAILED" if result.get("error") else "ok"
        items.append(f"tool_results[{i}] {status}: {_excerpt(result)}")
    if state.scores:
        items.append(f"scores: {_excerpt(dict(state.scores))}")
    for i, reflection in enumerate(state.reflections):
        items.append(f"reflections[{i}]: {_excerpt(reflection)}")
    items = items[:MAX_EVIDENCE_ITEMS]
    return _Evidence(items=tuple(items))


def _complete(provider: ModelProvider, model: str, system: str, user: str, max_tokens: int) -> str:
    result = provider.complete(
        CompletionRequest(
            messages=(
                ProviderMessage(role="system", content=system),
                ProviderMessage(role="user", content=user),
            ),
            model=model,
            max_tokens=max_tokens,
        )
    )
    return (result.content or "").strip()


@dataclass(frozen=True)
class LLMCritic(Critic):
    """Prose from the model, citations from the state."""

    provider: ModelProvider
    model: str
    fallback: RuleBasedCritic = field(default_factory=RuleBasedCritic)
    max_tokens: int = DEFAULT_MAX_TOKENS

    def critique(self, state: AEFState) -> Critique:
        baseline = self.fallback.critique(state)
        evidence = _evidence(state)
        prompt = (
            f"Objective: {state.objective}\n\nEvidence:\n{evidence.render(reverse=False)}\n\n"
            "Write the critique."
        )
        try:
            text = _complete(self.provider, self.model, CRITIC_SYSTEM, prompt, self.max_tokens)
        except ModelProviderError as exc:
            return Critique(
                verbal_feedback=f"{baseline.verbal_feedback} [llm critic unavailable: {exc}]",
                grounded_in=baseline.grounded_in,
            )
        if not text:
            return Critique(
                verbal_feedback=f"{baseline.verbal_feedback} [llm critic returned nothing]",
                grounded_in=baseline.grounded_in,
            )
        # Added, not substituted (ADR 0110): the rule-based line is the
        # evidence the prose must remain traceable to.
        return Critique(
            verbal_feedback=f"{text}\n\nEvidence: {baseline.verbal_feedback}",
            grounded_in=failure_signals(state),
        )


@dataclass(frozen=True)
class LLMJudge(Judge):
    """Per-term scores from the model; the weighted score, the omission rule,
    and the position-swap control from code."""

    provider: ModelProvider
    model: str
    rubric: Mapping[str, float]
    position_swap: bool = True
    max_tokens: int = 400
    fallback: RuleBasedJudge | None = None
    _baseline: RuleBasedJudge = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Reuse the rule-based validation of the rubric: same rules, same
        # error messages, one place.
        object.__setattr__(self, "_baseline", RuleBasedJudge(rubric=dict(self.rubric)))

    @property
    def _rule_based(self) -> RuleBasedJudge:
        return self.fallback if self.fallback is not None else self._baseline

    def judge(self, state: AEFState) -> Judgment:
        evidence = _evidence(state)
        terms = sorted(self.rubric)
        orders = (False, True) if self.position_swap else (False,)
        samples: list[dict[str, float]] = []
        failures: list[str] = []
        for reverse in orders:
            prompt = (
                f"Objective: {state.objective}\n\nRubric terms: {', '.join(terms)}\n\n"
                f"Evidence:\n{evidence.render(reverse=reverse)}\n\n"
                'Reply with JSON only, e.g. {"' + terms[0] + '": 0.5}.'
            )
            try:
                text = _complete(self.provider, self.model, JUDGE_SYSTEM, prompt, self.max_tokens)
            except ModelProviderError as exc:
                failures.append(str(exc))
                continue
            parsed = _parse_scores(text, terms)
            if parsed is None:
                failures.append(f"unparseable reply: {text[:80]!r}")
                continue
            samples.append(parsed)
        if not samples:
            base = self._rule_based.judge(state)
            return Judgment(
                score=base.score,
                rubric=base.rubric,
                rationale=f"{base.rationale} [llm judge fell back: {'; '.join(failures)}]",
            )
        # Code owns the arithmetic. Missing terms count 0.0 (anti-omission),
        # values are clamped, and the position-swap samples are averaged.
        per_term = {
            term: sum(_clamp(s.get(term, 0.0)) for s in samples) / len(samples) for term in terms
        }
        weight_sum = sum(self.rubric.values())
        score = sum(per_term[t] * self.rubric[t] for t in terms) / weight_sum
        missing = tuple(t for t in terms if any(t not in s for s in samples))
        delta = 0.0
        if len(samples) == 2:
            first = sum(_clamp(samples[0].get(t, 0.0)) * self.rubric[t] for t in terms) / weight_sum
            second = (
                sum(_clamp(samples[1].get(t, 0.0)) * self.rubric[t] for t in terms) / weight_sum
            )
            delta = abs(first - second)
        drivers = ", ".join(f"{t}={per_term[t]:.4g}" for t in terms[:3])
        rationale = (
            f"llm-judged weighted score {score:.4g} over {len(terms)} rubric term(s) "
            f"from {len(samples)} sample(s); {drivers}; position_delta={delta:.4g}"
        )
        if missing:
            rationale += f"; missing from the model's reply (counted as 0.0): {', '.join(missing)}"
        if failures:
            rationale += f"; {len(failures)} sample(s) failed: {'; '.join(failures)}"
        return Judgment(score=score, rubric=dict(self.rubric), rationale=rationale)


class SelfRankingError(RuntimeError):
    """A judge was asked to rank a candidate its own model wrote.

    Raised rather than warned, and refused rather than fallen back to, for the
    reason ADR 0105 gives about containment: an automatic fallback would be
    weaker than a refusal, and a bias control that can be reached by accident
    is not a control.
    """


def _bare_model(name: str) -> str:
    """A model name with its context-window suffix and case removed, so
    `claude-opus-5[1m]` and `claude-opus-5` are recognised as one model.

    The suffix is how the harness CLI reports a 1M-context variant
    (`modelUsage` keys it that way, see ADR 0169), and a guard that missed it
    would let a judge rank its own output whenever the two names were written
    differently — which is exactly how such a guard fails in practice.
    """
    head = name.split("[", 1)[0]
    return head.strip().lower()


@dataclass(frozen=True)
class Candidate:
    """One text to be ranked, and the model that wrote it.

    `model` is not decoration: it is the field the self-preference guard reads,
    and it never enters the prompt. `label` is the caller's name for this
    candidate and also never enters the prompt — the ranker presents the two
    candidates positionally as "A" and "B" so a label like "incumbent" or
    "gpt" cannot leak authorship into the thing being controlled for.
    """

    label: str
    text: str
    model: str


@dataclass(frozen=True)
class Ranking:
    """The outcome of one pairwise comparison. Code owns every field here; the
    model supplies one letter per sample and nothing it writes can add an
    outcome."""

    winner: str | None  # the winning Candidate's label, or None
    consistent: bool  # the position swap agreed (or was not requested)
    verdicts: tuple[str | None, ...]  # winning label per sample, in order
    rationale: str


RANKER_SYSTEM = (
    "You are comparing two candidate summaries of the same passage, written against the "
    "same instructions. Decide which one better satisfies those instructions. Reply with a "
    'single JSON object and nothing else: {"better": "A"} or {"better": "B"}. Judge only '
    "the text shown. Length of a candidate is not quality in itself."
)


@dataclass(frozen=True)
class PairwiseRanker:
    """Rank two model outputs against each other, with a self-preference guard.

    **Why this exists.** Until ADR 0162 nothing in this repo had a judge
    ranking model outputs against each other: `LLMJudge` grades one state at a
    time, so a judge could never prefer its own writing, and the
    self-preference control the rubric's dimension 3 asks for had nowhere to
    attach. ADR 0171 closed with that gap named as the reason dimension 3 was
    7 and not 8.

    **What was measured** (ADR 0162, `docs/research/j4/`, 11 pairs of summaries
    of the same passages, 66 live judgments, position-swapped):

    | judge | wrote one? | prefers the Opus text | agrees with the checks | position-unstable |
    |---|---|---|---|---|
    | `claude-opus-5[1m]` | yes (A) | 0.714 | 2/3 | 4/11 |
    | `claude-haiku-4-5-20251001` | yes (B) | 0.444 | 5/5 | 2/11 |
    | `claude-sonnet-5` | **no** | 0.455 | 5/5 | **0/11** |

    Read against the disinterested judge, the Opus judge's self-preference is
    **+0.260** and the Haiku judge's is **+0.010**. So the effect is real, it is
    one model's rather than a symmetric artefact of the design, and the judge
    that wrote neither candidate was both the most stable under a position swap
    and the most often right about the owner's own checks.

    Hence `allow_self_ranking=False` by DEFAULT: a ranker whose judge model
    wrote one of the candidates refuses, and an owner who wants it anyway says
    so in one field. That default is the measurement's, not a preference — and
    changing it should mean re-running `docs/research/j4/run_j4_selfpref.py`,
    not editing this line.

    **On an act that CHOOSES rather than measures** (ADR 0202,
    `docs/research/i14b/`, 24 judgments): asked to pick which of two summaries
    of the same passage to keep — one correct, one carrying a single content
    error — both `claude-opus-5[1m]` and `claude-sonnet-5` chose the correct
    one on **6 of 6** pairs with **0 of 6** position inconsistencies, against
    the grading path's one error and two position deltas above 0.5. Choosing
    was the steadier instrument of the two on that set. The guard was
    exercised on the same act and refuses both ways round: 0 calls when the
    judge model is declared, 1 call when it is `""` and only the answer names
    it. **Still nothing in `run_loop` calls this**; `cycle` takes
    `proposals[0]`, and putting a judge there is a change to
    `aef/harness/loop.py`, not to this file.

    The guard fires twice, because a model name is not always known before the
    call: once on the DECLARED judge model, and once on the model that actually
    ANSWERED (`CompletionResult.model`). The second costs one call to detect
    and is the only way an alias or an empty `model` — where the harness
    session's default answers, which is the repo's own default configuration —
    can be caught at all. Stated rather than hidden: with `model=""` a
    self-ranking attempt is refused after one call, not before it.
    """

    provider: ModelProvider
    model: str
    position_swap: bool = True
    max_tokens: int = 200
    # DENY BY DEFAULT (constraint #6, and ADR 0162's measurement).
    allow_self_ranking: bool = False

    def prompt(self, task: str, first: Candidate, second: Candidate) -> str:
        """The judge's user turn. `task` is the instruction both writers were
        given, passed in verbatim rather than restated, so the judge grades
        against the same text the writers saw. Neither candidate's `label` nor
        its `model` appears anywhere."""
        return (
            f"Instructions given to both writers:\n{task}\n\n"
            f"Candidate A:\n{first.text}\n\n"
            f"Candidate B:\n{second.text}\n\n"
            "Which candidate better satisfies the instructions? "
            'Reply with JSON only, e.g. {"better": "A"}.'
        )

    def _guard(self, seen: str, candidates: tuple[Candidate, ...], *, when: str) -> None:
        if self.allow_self_ranking or not seen:
            return
        bare = _bare_model(seen)
        for candidate in candidates:
            if candidate.model and _bare_model(candidate.model) == bare:
                raise SelfRankingError(
                    f"judge model {seen!r} wrote candidate {candidate.label!r} "
                    f"({candidate.model!r}), detected {when}. On ADR 0162's rig this judge "
                    f"preferred its own model's output on 0.714 of pairs against a "
                    f"disinterested judge's 0.455, and agreed with the owner's checks 2/3 "
                    f"against 5/5. Rank with a model that wrote neither candidate, or set "
                    f"allow_self_ranking=True to say you accept the bias."
                )

    def rank(self, task: str, left: Candidate, right: Candidate) -> Ranking:
        if left.label == right.label:
            raise ValueError(
                f"both candidates are labelled {left.label!r}; a ranking whose winner cannot "
                f"be named is not a ranking"
            )
        pair = (left, right)
        self._guard(self.model, pair, when="before the call, from the declared judge model")
        orders: tuple[tuple[Candidate, Candidate], ...] = (
            ((left, right), (right, left)) if self.position_swap else ((left, right),)
        )
        verdicts: list[str | None] = []
        failures: list[str] = []
        for first, second in orders:
            try:
                result = self.provider.complete(
                    CompletionRequest(
                        messages=(
                            ProviderMessage(role="system", content=RANKER_SYSTEM),
                            ProviderMessage(role="user", content=self.prompt(task, first, second)),
                        ),
                        model=self.model,
                        max_tokens=self.max_tokens,
                    )
                )
            except ModelProviderError as exc:
                failures.append(str(exc))
                verdicts.append(None)
                continue
            self._guard(result.model, pair, when="after the call, from the model that answered")
            choice = _parse_choice(result.content or "")
            if choice is None:
                failures.append(f"unparseable reply: {(result.content or '')[:80]!r}")
                verdicts.append(None)
                continue
            verdicts.append(first.label if choice == "A" else second.label)
        named = [v for v in verdicts if v is not None]
        consistent = len(named) == len(verdicts) and len(set(named)) == 1
        winner = named[0] if consistent and named else None
        rationale = (
            f"pairwise ranking over {len(verdicts)} sample(s); verdicts {verdicts}; "
            f"consistent={consistent}; judge={self.model or '(provider default)'}"
        )
        if failures:
            rationale += f"; {len(failures)} sample(s) failed: {'; '.join(failures)}"
        return Ranking(
            winner=winner,
            consistent=consistent,
            verdicts=tuple(verdicts),
            rationale=rationale,
        )


def _parse_choice(text: str) -> str | None:
    """`"A"` or `"B"` from the reply, or `None`. Same discipline as
    `_parse_scores`: the first JSON object, anything else refused, and a value
    outside the two allowed letters is not a verdict."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload: Any = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("better")
    if not isinstance(value, str):
        return None
    label = value.strip().upper()
    return label if label in {"A", "B"} else None


def _clamp(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return min(1.0, max(0.0, number))


def _parse_scores(text: str, terms: list[str]) -> dict[str, float] | None:
    """The first JSON object in the reply, keyed by rubric term. Tolerates a
    code fence or a sentence around it; refuses anything that is not an
    object. Unknown keys are dropped — the model does not get to add terms."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload: Any = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return {k: _clamp(v) for k, v in payload.items() if k in terms}
