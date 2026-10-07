"""What a status surface is allowed to claim.

This module is deliberately the first thing built, and it contains no
rendering. A dashboard is a trust surface: a wrong number on it is believed by
someone who has stopped reading the underlying data, which is the whole reason
they wanted a dashboard. So the constraints go in a type before they go in a
template.

Three decisions live here, each with an ADR (0107) behind it:

1. **A panel with no data cannot render green.** Not by convention — `Unknown`
   has no `state` field at all, so there is no branch in which it produces
   `HEALTHY`. `Digest` already carries `halt_channel_configured` and
   `runs_recorded` for exactly this reason, stated in its own source: "a system
   that reports nothing looks identical to one with nothing to report." A
   dashboard is where that stops being a nuisance and becomes authoritative.

2. **The page is read-only.** `FORBIDDEN_HTML_CONSTRUCTS` is declared here and
   imported by the renderer's test rather than re-listed there — the same
   derived-not-duplicated rule the gate catalogue follows, because two lists
   nobody compares drift (ADR 0091).

3. **No field is emitted whose disclosure was not decided.** `disclosure_of`
   raises on an unregistered field. An export is a file that gets committed,
   attached to tickets and pasted into chat; "nothing sensitive here" is a
   claim, and this makes it one somebody had to type.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import Generic, TypeVar

T = TypeVar("T")


class PanelState(StrEnum):
    """What a panel says about the thing it watches.

    `UNKNOWN` is not a styling choice and not a third kind of bad. It is the
    honest reading when the data that would answer the question is absent —
    and it is the reading this whole module exists to make unavoidable.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class UnknownReason(StrEnum):
    """Why a panel cannot answer.

    Enumerated rather than free-text because the operator's next action
    differs per reason, and "unknown" with no cause sends them looking in the
    wrong place — the defect class ADR 0074 named ("a refusal that misnames
    its own cause sends the operator to fix the wrong thing").
    """

    NO_LEDGER = "no_ledger"
    LEDGER_EMPTY = "ledger_empty"
    # "The chain was not checked", NOT "the chain failed". A failed
    # verification is something we KNOW — it is `Known(..., DEGRADED)`, and
    # loudly. Collapsing the two would let a detected forgery render as an
    # absence, which is the quieter and worse of the two.
    LEDGER_NOT_VERIFIED = "ledger_not_verified"
    NO_RUNS_RECORDED = "no_runs_recorded"
    MONITOR_NEVER_RAN = "monitor_never_ran"
    WINDOW_TOO_YOUNG = "window_too_young"
    HALT_CHANNEL_UNCONFIGURED = "halt_channel_unconfigured"
    CORPUS_EMPTY = "corpus_empty"
    NO_GRAPH = "no_graph"
    NO_LOOP_STATE = "no_loop_state"
    # The fleet page's reasons (EXPORT_MISSING / STALE / UNREADABLE) are NOT
    # declared here. They were, and no panel used them — which is ADR 0101's
    # rule turned on this module's own code: a forward-declared member with no
    # caller reads as a present feature. They arrive in Milestone 4 with the
    # fleet panels that consume them, and `test_every_unknown_reason_has_a_home`
    # makes adding one without a panel fail.


@dataclass(frozen=True)
class Unknown:
    """No data behind this panel.

    Has no `state` field, on purpose. The rule "a panel with no data must not
    render green" is not enforced by a check that could be forgotten; there is
    simply no attribute to read, and `mypy --strict` rejects any access to
    `.state` on a `Reading` that has not been narrowed to `Known` first.
    """

    reason: UnknownReason
    remedy: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reason, UnknownReason):
            raise TypeError(
                f"reason must be an UnknownReason, got {type(self.reason).__name__}. "
                f"A free-text cause is one nobody can route on."
            )


@dataclass(frozen=True)
class Known(Generic[T]):
    """A value, and what it means.

    Refuses `PanelState.UNKNOWN`: holding a value and reporting that you have
    none is a contradiction, and permitting it would reopen by the back door
    exactly the confusion `Unknown` exists to close.
    """

    value: T
    state: PanelState

    def __post_init__(self) -> None:
        # Found by this milestone's adversarial round, and it defeated the
        # module's central claim in four lines using nothing but the real API:
        #
        #     Digest(...).acceptance_rate  ->  None   (when proposed == 0)
        #     Known(value=None, state=HEALTHY)  ->  a GREEN panel
        #
        # `Digest.acceptance_rate`, `beating_manual_editing` and
        # `MonitorResult.observed_pass_rate` all return `X | None` where None
        # means "nothing to compare" — which is precisely UNKNOWN wearing an
        # Optional. An adapter converting those to panels would have produced
        # a green dashboard for a loop that had never run.
        #
        # So None is refused here rather than handled downstream: the None
        # branch of an optional IS the Unknown branch, and making the caller
        # write that out is the only version of this rule that survives the
        # next adapter somebody adds.
        if self.value is None:
            raise ValueError(
                "Known(value=None) is the Unknown case wearing an Optional. Every source "
                "in this repo that can answer 'nothing to compare' says so with None — "
                "Digest.acceptance_rate, Digest.beating_manual_editing, "
                "MonitorResult.observed_pass_rate. Map that branch to "
                "Unknown(reason=...) explicitly; the operator needs the cause, and a "
                "green panel over a None is the exact failure this module exists to stop."
            )
        # `PanelState` is a StrEnum, so its members ARE strings — which means
        # `Known(value=1, state="unknown")` slipped past the identity check
        # below and produced a Known that claimed to be Unknown. Reproduced.
        if not isinstance(self.state, PanelState):
            raise TypeError(
                f"state must be a PanelState, got {type(self.state).__name__}. PanelState is "
                f"a StrEnum, so a bare string compares unequal to every member under `is` "
                f"and silently defeats both this guard and Panel.is_green."
            )
        if self.state is PanelState.UNKNOWN:
            raise ValueError(
                "Known(state=UNKNOWN) is a contradiction — a reading that holds a value "
                "knows something. Return Unknown(reason=...) instead, which carries the "
                "cause the operator needs."
            )

    @classmethod
    def optional(
        cls,
        value: T | None,
        state: PanelState,
        *,
        reason: UnknownReason,
        remedy: str = "",
    ) -> Reading[T]:
        """The honest conversion for a source that returns `X | None`.

        Provided because refusing None without offering the correct spelling
        just relocates the mistake — the next adapter would write
        `Known(value=x or 0, ...)`, which is the same green panel with an
        extra step.
        """
        if value is None:
            return Unknown(reason=reason, remedy=remedy)
        return cls(value=value, state=state)


# Generic alias. `Reading[int]` is either "we don't know" or "we know, and here
# is what it means" — with no third case, and no way to spell "we don't know,
# and it's fine."
Reading = Unknown | Known[T]


@dataclass(frozen=True)
class PanelSpec:
    """One panel's contract: what it watches, and precisely what makes it
    UNKNOWN rather than HEALTHY.

    Written down per panel because "it shows unknown when there's no data" is
    the kind of sentence that sounds complete and specifies nothing.
    """

    key: str
    title: str
    watches: str
    unknown_when: tuple[UnknownReason, ...]

    def __post_init__(self) -> None:
        if not self.unknown_when:
            raise ValueError(
                f"panel {self.key!r} declares no UNKNOWN condition. Every panel in this "
                f"system reads from data that can be absent; a panel that claims it "
                f"cannot be unknown has not been thought about."
            )


@dataclass(frozen=True)
class Panel:
    """A spec bound to a reading.

    `state` is the single place a `Reading` becomes a `PanelState`, and it is
    total: `Unknown` maps to `UNKNOWN` and nothing else can.
    """

    spec: PanelSpec
    reading: Reading[object]

    def __post_init__(self) -> None:
        # Reproduced in this milestone's adversarial round: any object with a
        # `.state` attribute set to HEALTHY rendered green, because
        # `isinstance(reading, Unknown)` was False and the else-branch trusted
        # whatever it was handed. Duck typing is the wrong default at a trust
        # boundary — `Reading` is a closed union of exactly two cases and this
        # keeps it closed at runtime as well as in the type.
        if not isinstance(self.reading, (Unknown, Known)):
            raise TypeError(
                f"reading must be Unknown or Known, got {type(self.reading).__name__}. "
                f"Reading is a closed union of two cases; anything else reaching here "
                f"renders whatever `.state` it happens to carry."
            )
        # Round 2: `unknown_when` was documentation. Every panel listed the
        # reasons it could be unknown for, and nothing checked — so the halt
        # panel cheerfully reported `corpus_empty`, which sends the operator to
        # fix the corpus while the loop is halted. ADR 0092's defect class
        # ("a declared thing with no enforcement reads as an enforced thing"),
        # and ADR 0074's ("a refusal that misnames its own cause sends the
        # operator to fix the wrong thing") in the same line of code.
        if isinstance(self.reading, Unknown) and self.reading.reason not in self.spec.unknown_when:
            raise ValueError(
                f"panel {self.spec.key!r} cannot be unknown for reason "
                f"{self.reading.reason.value!r}; it declares "
                f"{[r.value for r in self.spec.unknown_when]}. Either the reason is wrong — "
                f"and it is what the operator will act on — or the spec never anticipated "
                f"this case and should say so explicitly."
            )
        # Defence in depth against a `Known` subclass that skips its own
        # __post_init__. Not because a malicious subclass is the threat model,
        # but because the zone rule survived three defeats of the allowlist by
        # not being the only check (ADR 0093).
        if isinstance(self.reading, Known) and self.reading.value is None:
            raise ValueError(
                f"panel {self.spec.key!r} holds Known(value=None) — no data, rendered as "
                f"though there were. Known refuses this at construction; reaching it here "
                f"means the guard was bypassed."
            )

    @property
    def state(self) -> PanelState:
        if isinstance(self.reading, Unknown):
            return PanelState.UNKNOWN
        return self.reading.state

    @property
    def is_green(self) -> bool:
        return self.state is PanelState.HEALTHY


# --------------------------------------------------------------------------
# The panel catalogue (Milestone 1a).
# --------------------------------------------------------------------------

PANELS: tuple[PanelSpec, ...] = (
    PanelSpec(
        key="halt",
        title="Kill switch",
        watches="whether the loop is halted, and why",
        unknown_when=(UnknownReason.NO_LOOP_STATE,),
    ),
    PanelSpec(
        key="ledger_integrity",
        title="Ledger integrity",
        watches="the hash chain over every loop event",
        unknown_when=(
            UnknownReason.NO_LEDGER,
            UnknownReason.NO_LOOP_STATE,
            # Round 2 found this missing: the most security-relevant reason in
            # the enum had no panel to appear on. A chain nobody checked and a
            # chain that passed are different claims, and the export exists in
            # part to stop them being rendered the same.
            UnknownReason.LEDGER_NOT_VERIFIED,
        ),
    ),
    PanelSpec(
        key="halt_channel",
        title="Halt notification",
        watches="whether a halt would reach a human",
        unknown_when=(UnknownReason.HALT_CHANNEL_UNCONFIGURED, UnknownReason.NO_LOOP_STATE),
    ),
    PanelSpec(
        key="acceptance",
        title="Acceptance rate",
        watches="merged over proposed, in the window",
        unknown_when=(
            UnknownReason.NO_LEDGER,
            UnknownReason.LEDGER_EMPTY,
            UnknownReason.WINDOW_TOO_YOUNG,
        ),
    ),
    PanelSpec(
        key="post_merge",
        title="Post-merge health",
        watches="live outcomes after each merge, and rollbacks",
        unknown_when=(
            UnknownReason.MONITOR_NEVER_RAN,
            UnknownReason.NO_RUNS_RECORDED,
            UnknownReason.WINDOW_TOO_YOUNG,
        ),
    ),
    PanelSpec(
        key="drift",
        title="Cumulative drift",
        watches="how far Zone A has moved from the blessed baseline",
        unknown_when=(UnknownReason.NO_LOOP_STATE, UnknownReason.LEDGER_EMPTY),
    ),
    PanelSpec(
        key="corpus",
        title="Golden corpus",
        watches="scenario count, and that it never shrinks",
        unknown_when=(UnknownReason.CORPUS_EMPTY, UnknownReason.NO_LOOP_STATE),
    ),
    PanelSpec(
        key="agent_graph",
        title="Agent graph",
        watches="node topology, side effects, HITL-gated edges",
        unknown_when=(UnknownReason.NO_GRAPH,),
    ),
    PanelSpec(
        key="loop_graph",
        title="Proposal lifecycle",
        watches="occupancy of each state from proposed to merged",
        unknown_when=(UnknownReason.NO_LEDGER, UnknownReason.LEDGER_EMPTY),
    ),
    PanelSpec(
        key="runs",
        title="Recorded runs",
        watches="whether any agent traffic was captured at all",
        unknown_when=(UnknownReason.NO_RUNS_RECORDED,),
    ),
)

# A read-only view. Round 3: this was a plain dict, so `PANELS_BY_KEY["halt"]
# = PanelSpec(..., unknown_when=tuple(UnknownReason))` widened the enforcement
# B1 had just installed — a control one assignment away from being switched off.
PANELS_BY_KEY: Mapping[str, PanelSpec] = MappingProxyType({spec.key: spec for spec in PANELS})


def panel_spec(key: str) -> PanelSpec:
    try:
        return PANELS_BY_KEY[key]
    except KeyError:
        raise KeyError(f"no panel {key!r}; known panels are {sorted(PANELS_BY_KEY)}") from None


# --------------------------------------------------------------------------
# Read-only (Milestone 1c).
# --------------------------------------------------------------------------

# Declared here and imported by the renderer's tests rather than re-listed,
# because two lists nobody compares drift (ADR 0091).
#
# This list used to be WIDER and wrong. It banned `<button`, `onclick=` and
# `onchange=` as proxies for "a control", and the first page that needed to
# switch between views tripped it. A tab that changes which locally-loaded data
# is drawn writes nothing, requests nothing and stores nothing — the proxy was
# catching INTERACTIVITY when the property being protected is a WRITE PATH.
#
# Replaced rather than trimmed, on ADR 0093's precedent, and the replacement is
# stricter where it counts: it adds sessionStorage, indexedDB, external
# src/href/@import/url(http...) references, and Math.random — the last of which
# caught a real violation in mind.py, where edge particle phases were seeded
# randomly, so identical data drew a different picture on every load.
#
# The page still cannot act. A page with a write path is an unaudited control
# plane reachable by anyone who can open a file: no authentication, no audit
# entry, no HITL gate — and this loop's approval story routes through a signed
# manifest held by a person.
FORBIDDEN_HTML_CONSTRUCTS: tuple[str, ...] = (
    # Write paths and network egress — the actual property being protected.
    "<form",
    "<input",
    "<textarea",
    "onsubmit=",
    "fetch(",
    "XMLHttpRequest",
    "navigator.sendBeacon",
    "WebSocket",
    "localStorage",
    "sessionStorage",
    "indexedDB",
    "document.cookie",
    # External references the browser would load. A single-file artifact that
    # pulls a font or a script is not offline, and it fails SILENTLY — the page
    # still renders, just wrong, which is the worst of the available failures.
    "@import",
    'src="http',
    "src='http",
    'href="http',
    "href='http",
    "url(http",
    # Non-determinism in layout or rendering. The same data must produce the
    # same picture, or the persisted layout is a lie and no two screenshots
    # agree. This one caught a live violation in mind.py.
    "Math.random",
)


# --------------------------------------------------------------------------
# Disclosure (Milestone 1d).
# --------------------------------------------------------------------------


class Disclosure(StrEnum):
    PUBLIC = "public"
    """Safe in a file that gets committed and pasted into a ticket."""

    REDACTED = "redacted"
    """Emitted as a count, a digest or a type name — never the value."""

    EXCLUDED = "excluded"
    """Never emitted in any form."""


class DisclosureError(RuntimeError):
    """A field was emitted whose disclosure nobody decided."""


# Every field the export may carry, with the decision and its reason. An
# unregistered field raises rather than defaulting — a default here would be a
# policy applied to fields nobody looked at, which is the opposite of the point.
_FIELD_DISCLOSURE: dict[str, Disclosure] = {
    # Identity and provenance of the export itself.
    "schema_version": Disclosure.PUBLIC,
    "generated_at": Disclosure.PUBLIC,
    "repo_name": Disclosure.PUBLIC,
    "graph_id": Disclosure.PUBLIC,
    "graph_version": Disclosure.PUBLIC,
    # Absolute paths leak the operator's home directory and the machine's
    # layout, and break the byte-stability the export is meant to have.
    "repo_root": Disclosure.EXCLUDED,
    "state_dir": Disclosure.EXCLUDED,
    "absolute_path": Disclosure.EXCLUDED,
    # Ledger. The head hash is a digest of data that is itself in the repo.
    "ledger_verified": Disclosure.PUBLIC,
    "ledger_entry_count": Disclosure.PUBLIC,
    "ledger_head_hash": Disclosure.PUBLIC,
    "ledger_error": Disclosure.PUBLIC,
    # Digest counters — aggregates, no payload.
    "proposed": Disclosure.PUBLIC,
    "merged": Disclosure.PUBLIC,
    "rejected": Disclosure.PUBLIC,
    "escalated": Disclosure.PUBLIC,
    "rolled_back": Disclosure.PUBLIC,
    "blessed": Disclosure.PUBLIC,
    "halts": Disclosure.PUBLIC,
    "security_events": Disclosure.PUBLIC,
    "scenarios_added": Disclosure.PUBLIC,
    "drift": Disclosure.PUBLIC,
    "owner_edits": Disclosure.PUBLIC,
    "runs_recorded": Disclosure.PUBLIC,
    "halt_channel_configured": Disclosure.PUBLIC,
    # Halt state. The reason is the operator's own words and is the entire
    # point of showing the halt; it is public deliberately, and the operator
    # writing it is the one deciding what it says.
    "kill_switch_engaged": Disclosure.PUBLIC,
    "kill_switch_reason": Disclosure.PUBLIC,
    # Candidate identity. All of this is already in git history.
    "proposal_id": Disclosure.PUBLIC,
    "branch": Disclosure.PUBLIC,
    "base_sha": Disclosure.PUBLIC,
    "head_sha": Disclosure.PUBLIC,
    "commit_message": Disclosure.PUBLIC,
    "gate_outcomes": Disclosure.PUBLIC,
    "approved_by": Disclosure.PUBLIC,
    # Graph topology.
    "node_id": Disclosure.PUBLIC,
    "edge_targets": Disclosure.PUBLIC,
    "side_effects": Disclosure.PUBLIC,
    "deterministic": Disclosure.PUBLIC,
    "requires_hitl": Disclosure.PUBLIC,
    "relative_path": Disclosure.PUBLIC,
    # Run telemetry.
    "run_id": Disclosure.PUBLIC,
    "node_duration_ms": Disclosure.PUBLIC,
    "token_count": Disclosure.PUBLIC,
    "model_name": Disclosure.PUBLIC,
    "pass_rate": Disclosure.PUBLIC,
    # Error text is the field most likely to carry a secret by accident: a
    # traceback that stringifies a connection URL, a client that echoes an
    # Authorization header. The type and the count answer the operator's
    # question ("is it failing, and how"); the message does not, and cannot be
    # scanned safely.
    "error_message": Disclosure.REDACTED,
    "error_type": Disclosure.PUBLIC,
    "error_count": Disclosure.PUBLIC,
    # A tenant tag list in a shared file is a customer list. The canary needs
    # per-tenant stratification; the dashboard needs only the shape of it.
    "tenant_tag": Disclosure.REDACTED,
    "tenant_count": Disclosure.PUBLIC,
    # The salt fingerprint is a deterministic function of the salt. ADR 0106
    # made it a 200k-iteration PBKDF2 precisely because it is an offline
    # oracle; publishing it in a file that gets pasted around hands an attacker
    # the oracle and the leisure to use it. Restarts prove same-population
    # against a fingerprint held in the loop state, not one in the export.
    "canary_salt_fingerprint": Disclosure.EXCLUDED,
    "canary_keyed": Disclosure.PUBLIC,
    "canary_percent": Disclosure.PUBLIC,
    # Never, in any form.
    "prompt_text": Disclosure.EXCLUDED,
    "response_text": Disclosure.EXCLUDED,
    "tool_arguments": Disclosure.EXCLUDED,
    "working_memory": Disclosure.EXCLUDED,
    "retrieved_context": Disclosure.EXCLUDED,
    "signing_key": Disclosure.EXCLUDED,
    "canary_salt": Disclosure.EXCLUDED,
    "api_key": Disclosure.EXCLUDED,
    "environment": Disclosure.EXCLUDED,
}

# Read-only view. Round 3: the registry was a plain dict, so
# `FIELD_DISCLOSURE["signing_key"] = Disclosure.PUBLIC` flipped an EXCLUDED
# field to PUBLIC at runtime — a security policy that any imported module
# could rewrite. Deciding a disclosure and then leaving the decision
# writable is most of the way back to not having decided.
FIELD_DISCLOSURE: Mapping[str, Disclosure] = MappingProxyType(_FIELD_DISCLOSURE)

# Round 4, against round 3's own fix: `MappingProxyType` is a VIEW, not a copy.
# Wrapping the dict and leaving the dict NAMED meant
# `contract._FIELD_DISCLOSURE["signing_key"] = Disclosure.PUBLIC` still worked
# — the freeze looked installed and was not. Deleting the name closes the
# accidental path (an import touching the wrong symbol), which is the threat
# here. It does not close a determined one: the object is still reachable
# through the proxy's referents via `gc`. Saying "immutable" would be the
# overclaim this program keeps writing ADRs about; the accurate word is
# "not writable by accident."
del _FIELD_DISCLOSURE


def disclosure_of(field: str) -> Disclosure:
    """The decided disclosure for `field`, or raise.

    Raising is the enforcement of the program's HARD-STOP #7. A default would
    silently apply somebody's guess to every field added later, which is how a
    decision becomes an accident.
    """
    try:
        return FIELD_DISCLOSURE[field]
    except KeyError:
        raise DisclosureError(
            f"field {field!r} has no disclosure decision. Add it to FIELD_DISCLOSURE with "
            f"a reason before emitting it — an export is committed, attached to tickets and "
            f"pasted into chat, and 'nothing sensitive here' is a claim, not an argument."
        ) from None


def redacted_form(value: str | bytes | int | float) -> str:
    """The emittable stand-in for a REDACTED value: a stable, truncated digest.

    Stable so the fleet page can count distinct tenants and group identical
    errors without ever holding either. Truncated because the full digest buys
    nothing here and invites being treated as an identifier.

    **What this does and does not buy, stated rather than implied.** It stops
    the value being *read*. It does not make it unguessable: a digest over a
    low-entropy domain — a tenant tag, a known error string — is confirmable by
    anyone holding a candidate list. That is strictly better than emitting the
    tag, and it is not anonymity. Making it unguessable would need a key, and a
    key in the export is the ADR 0106 mistake with the serial numbers filed off.
    """
    if isinstance(value, str):
        material = value.encode("utf-8")
    elif isinstance(value, bytes):
        material = value
    elif isinstance(value, (bool, int, float)):
        material = repr(value).encode("utf-8")
    else:
        # Round 4: the body was `repr(value)`, and `repr` of an object without
        # its own `__repr__` embeds a memory address —
        #   <ErrorLike object at 0x100c0a510>
        # so two IDENTICAL values hashed differently. That breaks grouping (the
        # only reason this is a digest rather than a constant) and byte-stability
        # (Milestone 2's 2e), and it fails in the direction where the export
        # looks fine and the numbers are noise.
        raise DisclosureError(
            f"redaction needs a canonical form; {type(value).__name__} has none. Convert "
            f"deliberately at the call site — `str(exc)` for an exception, and say which "
            f"part of the object is the value. `repr()` here embedded a memory address, "
            f"so equal values hashed differently."
        )
    return "sha256:" + sha256(material).hexdigest()[:16]


def prepare(field: str, value: object) -> object:
    """The only sanctioned way to put a value into the export.

    Replaces a boolean `emittable()` that this milestone's third adversarial
    round showed to be a trap: it returned `True` for REDACTED, so the obvious
    caller —

        if emittable(field):
            payload[field] = value

    — emitted the raw `error_message` and the raw `tenant_tag`, which are
    precisely the two fields the registry marks as needing redaction. The
    function was correct against its own docstring ("may appear in the export at
    all, in any form") and wrong against every way anyone would use it, which is
    the more dangerous kind of correct.

    So there is no boolean any more. A three-valued policy gets a three-way
    function that applies the policy itself, and the safe path is the only path.
    """
    disclosure = disclosure_of(field)
    if disclosure is Disclosure.EXCLUDED:
        raise DisclosureError(
            f"field {field!r} is EXCLUDED and must not be emitted in any form. If the "
            f"export needs something about it, emit a different field with its own "
            f"disclosure decision — a count, a boolean, a type name."
        )
    # Round 5: this passed None straight through for a PUBLIC field, which is
    # A1 again one layer down — `Known` refuses None and its sibling did not,
    # so the same "nothing to compare" value that could not become a green
    # panel could still become a `null` in the export, and a null in JSON reads
    # as a value that happens to be empty rather than as an absence.
    #
    # Two APIs enforcing the same rule differently is the seam three of ten
    # defects lived in during the predecessor program: each half correct, the
    # join wrong.
    if value is None:
        raise DisclosureError(
            f"field {field!r} was given None. As with Known(value=None), the None branch "
            f"of an optional IS the absent case — omit the field, or emit a companion "
            f"field that says why it is absent. A null in the export reads as a value "
            f"that happens to be empty."
        )
    if disclosure is Disclosure.REDACTED:
        # The literal tuple, not a named constant: mypy narrows on the
        # former and not the latter, and a second list of the same types
        # is the drift ADR 0091 names. `redacted_form`'s signature is the
        # one source of truth, and it is the one the type checker reads.
        if not isinstance(value, (str, bytes, bool, int, float)):
            # Same cause as `redacted_form`'s own refusal, raised here so the
            # narrowing is visible to the type checker rather than deferred to
            # a runtime branch mypy cannot see through.
            raise DisclosureError(
                f"field {field!r} is REDACTED but {type(value).__name__} has no canonical "
                f"form to hash. Convert deliberately at the call site — `str(exc)` for an "
                f"exception — and say which part of the object is the value."
            )
        return redacted_form(value)
    return value
