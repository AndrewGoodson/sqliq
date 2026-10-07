"""Redaction at the harvest boundary (ADR 0119).

`harvest` promotes real runs into the corpus, and the corpus lives in git.
A real run's objective, working memory and tool results carry whatever the
tenant typed: addresses, tokens, account numbers. Without a redaction step,
"learn from live runs" means "commit tenant data", and the trust case's
criterion about real tenants cannot be met by a system that leaks them.

Three rules.

**Redact the INPUT, then re-execute; never patch the recorded trace.** A
trace redacted in place is a run that never happened — the agent saw the
real values. Harvest already refuses a run that does not re-execute
identically; redaction reuses that: the redacted initial state is re-run,
and the run is admitted only if it still fails the same way. A graph whose
behaviour depended on the secret (it branched on the token) changes
behaviour under redaction and is REJECTED, not recorded. The scenario the
corpus keeps is then honestly "given this redacted input, this happened".

**Scan the output before writing.** After re-execution, the scenario that
would be saved is scanned with the same patterns. A match means the graph
reintroduced a secret from somewhere the input redaction could not reach
(a tool, an environment), and the run is rejected. This is the planted-fault
check on the redactor itself, run every time rather than once at authoring.

**Patterns are data, and conservative by default.** Emails, bearer/API
tokens, and long opaque strings. False positives cost a scenario; false
negatives cost a tenant. Owners extend the list; nothing here shortens it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from aef.state import AEFState

# Order matters only for the label a match gets; a string matching two
# patterns is redacted by the first. Every pattern carries its own name so the
# report says WHICH shape matched — "a UUID leaked" and "something long and
# opaque leaked" are different findings for whoever has to go and rotate it.
#
# The six shapes below `aws_key` are ADR 0197. M6's pilot (ADR 0163) scanned
# a real repo clean and then named its own residual: marlin's boundary rule is
# written around a subscription UUID and the list did not match it, because
# ADR 0126 had removed `-` from `opaque_secret`'s character class to stop a
# hyphenated plain-English objective being redacted into a placeholder. The
# answer is not to put `-` back — that reintroduces the false positive — but
# to name the hyphenated shapes explicitly, which is what prefix- and
# structure-anchored patterns do and what shape-guessing by length cannot.
DEFAULT_PATTERNS: tuple[tuple[str, str], ...] = (
    # First, because the credential in `scheme://user:password@host` ends in
    # something the `email` pattern matches: before this existed, a leaked
    # connection string was reported as "an email address", which sends the
    # operator to rotate the wrong thing.
    (
        "connection_string",
        r"\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@]+:[^\s:/@]+@[^\s/]+",
    ),
    ("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    # ADR 0197 widened the middle of this. It used to be
    # `[-_](?:live|test|ant|proj)?[-_]?[A-Za-z0-9]{16,}`, a fixed vocabulary of
    # ONE optional segment, and `sk-ant-api03-<36>` — the shape of a real
    # Anthropic API key, the credential this repo's own adopters are most
    # likely to hold — matched NOTHING: `ant` consumed the vocabulary slot and
    # `api03` was left in front of a class that has no hyphen. Now up to three
    # short segments may sit between the prefix and the body, so the pattern
    # follows the vendor convention rather than a list of the four words
    # somebody happened to think of.
    ("api_key", r"\b(?:sk|pk|rk|ak)(?:[-_][A-Za-z0-9]{2,10}){0,3}[-_][A-Za-z0-9]{16,}\b"),
    ("bearer", r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    # Three dot-separated base64url segments with a JOSE header. Kept after
    # `bearer` on purpose: `Bearer <jwt>` is a bearer header and is reported as
    # one, while a bare JWT — in a tool result, a log line, a working-memory
    # value — now gets its own name instead of being torn into two
    # `opaque_secret` matches at the dots.
    (
        "jwt",
        r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}",
    ),
    # AKIA is the long-lived access key; the other five prefixes are the
    # temporary/role/instance ids that leak just as usefully.
    ("aws_key", r"\b(?:AKIA|ASIA|ABIA|ACCA|AIDA|AROA)[0-9A-Z]{16}\b"),
    # GitHub's prefixed tokens: personal (ghp), OAuth (gho), user-to-server
    # (ghu), server-to-server (ghs), refresh (ghr).
    ("github_token", r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    # Slack bot/user/app/refresh/legacy tokens.
    ("slack_token", r"\bxox[baprse]-[A-Za-z0-9-]{10,}\b"),
    # 8-4-4-4-12 hex. This is the shape ADR 0163 named as the pilot's residual.
    # It cannot revive ADR 0126's false positive: the false positive was a
    # hyphenated *English* slug, and every group here is hex-only and
    # length-exact, so `migrate-the-customer-billing-pipeline-to-v2-...` has no
    # substring that fits. Nor does a 64-hex SHA digest, which carries no
    # hyphens at all.
    (
        "uuid",
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
    ),
    # Long, high-entropy-looking: 40+ chars carrying BOTH a letter and a
    # digit, and no hyphen. All three constraints are corrections (ADR 0126):
    # the first version matched any 40+ run of `[A-Za-z0-9+/=_-]`, so
    # `migrate-the-customer-billing-pipeline-to-v2-with-zero-downtime` — a
    # plain-English objective — was redacted, and the harvested scenario was
    # admitted with a placeholder objective, which is a scenario that no
    # longer tests what the run did. Hyphenated key shapes are not lost:
    # `api_key` and `bearer` already carry them, and they are prefix-anchored
    # rather than shape-guessed.
    (
        "opaque_secret",
        r"\b(?=[A-Za-z0-9+/=_]*[A-Za-z])(?=[A-Za-z0-9+/=_]*\d)[A-Za-z0-9+/=_]{40,}\b",
    ),
)


class RedactionError(ValueError):
    pass


@dataclass(frozen=True)
class RedactionPolicy:
    patterns: tuple[tuple[str, str], ...] = DEFAULT_PATTERNS
    # Keys dropped from `working_memory` outright, whatever they hold.
    drop_working_memory_keys: tuple[str, ...] = ("api_key", "token", "secret", "password")
    _compiled: tuple[tuple[str, re.Pattern[str]], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        compiled = []
        for label, pattern in self.patterns:
            try:
                compiled.append((label, re.compile(pattern)))
            except re.error as exc:
                raise RedactionError(
                    f"redaction pattern {label!r} does not compile: {exc}"
                ) from exc
        object.__setattr__(self, "_compiled", tuple(compiled))

    def redact_text(self, text: str) -> tuple[str, int]:
        count = 0
        for label, pattern in self._compiled:
            text, n = pattern.subn(f"[REDACTED:{label}]", text)
            count += n
        return text, count

    def redact_value(self, value: Any) -> tuple[Any, int]:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            out: dict[Any, Any] = {}
            total = 0
            for k, v in value.items():
                v2, n = self.redact_value(v)
                out[k] = v2
                total += n
            return out, total
        if isinstance(value, list):
            items = [self.redact_value(v) for v in value]
            return [v for v, _ in items], sum(n for _, n in items)
        return value, 0

    def redact_state(self, state: AEFState) -> tuple[AEFState, int]:
        """A redacted copy of the state and the number of substitutions,
        including dropped working-memory keys."""
        payload = state.model_dump(mode="json")
        dropped = 0
        wm = payload.get("working_memory") or {}
        for key in list(wm):
            if key in self.drop_working_memory_keys:
                del wm[key]
                dropped += 1
        # The harness's OWN identifiers are held out by exact field path, the
        # same rule `harvest._scannable` applies to the output scan and for the
        # same reason: `run_id` is a `uuid4` this process assigned, it carries
        # no tenant information, and since ADR 0197 gave the policy a `uuid`
        # pattern it matches every time. Redacting it rewrote the key the
        # grounding chain joins on — a harvested scenario could no longer be
        # traced to the failure record that justified a candidate (ADR 0192's
        # F-N7-4, reproduced: `run_id` in, `[REDACTED:uuid]` out).
        #
        # By path, never by pattern: a tenant-typed UUID anywhere else in the
        # state is still redacted, which `test_a_tenant_uuid_is_still_redacted`
        # pins from the other side.
        held_out = {k: payload.pop(k) for k in ("run_id", "agent_id") if k in payload}
        redacted, count = self.redact_value(payload)
        redacted.update(held_out)
        return AEFState.model_validate(redacted), count + dropped

    def find(self, value: Any) -> tuple[str, ...]:
        """Labels of every pattern that still matches anywhere in `value`.
        Empty means clean. This is the output scan.

        Patterns are applied IN ORDER, each to the text the previous ones
        already substituted — the same way `redact_text` works — so the labels
        reported are the labels a redaction would actually stamp. Before ADR
        0197 this searched the raw text with every pattern independently, and
        one leaked database URL came back as
        `("connection_string", "email")`: two findings for one credential,
        naming a shape nobody has to rotate.

        The detector is not weakened by this. If any pattern matches the raw
        text, let P be the first such in list order; no pattern before P
        matched, so nothing before P changed the text, so P still matches.
        Non-empty before ⟺ non-empty after.
        """
        text = _flatten(value)
        labels = []
        for label, pattern in self._compiled:
            text, n = pattern.subn(f"[REDACTED:{label}]", text)
            if n:
                labels.append(label)
        return tuple(labels)


def _flatten(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(f"{k}\n{_flatten(v)}" for k, v in value.items())
    if isinstance(value, list | tuple):
        return "\n".join(_flatten(v) for v in value)
    return str(value)
