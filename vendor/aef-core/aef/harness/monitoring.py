"""Post-merge monitoring, auto-rollback, the kill switch, and the digest.

ADR 0045 removed the human from the merge path. These four mechanisms are
what replaced them, and they are **load-bearing rather than optional**:
until this module exists, Tier-1 auto-merge must stay off, because an
auto-merge with nothing watching afterwards is unobserved in both
directions.

Three rules here are not the obvious ones.

**Ambiguity rolls back.** When the signal is unclear — too few observations,
or a difference inside the noise — the change is reverted, not held pending
more data. Reverting a good change costs one re-proposal; keeping a bad one
compounds through every subsequent merge that builds on it. "Wait and see"
is the option that quietly accumulates risk, so it is not offered.

**A rollback triggered by something the gates passed halts the loop.** It
means the gates have a blind spot. Continuing to merge through a known blind
spot is how a system with good local decisions ends up somewhere bad, and
the halt is what turns one bad merge into a bounded incident.

**The digest reports whether the loop is worth running at all.** Not only
whether it is safe. `05-approval-policy.md` §6 lists "no measurable benefit
over the owner editing code directly" as a halt criterion, and a digest that
cannot say so is a status page rather than an oversight surface.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from aef.harness.ledger import EventKind, LedgerEntry

# Q-A2 owner default: long enough to see real traffic variety.
DEFAULT_MIN_OBSERVATIONS = 20
DEFAULT_WINDOW = timedelta(days=7)
# A regression this size is acted on; below it, the difference is noise at
# these sample sizes and is treated as ambiguous rather than as evidence.
DEFAULT_REGRESSION_MARGIN = 0.05

KILL_SWITCH_FILENAME = "HALTED"


class Verdict(StrEnum):
    HEALTHY = "healthy"
    REGRESSED = "regressed"
    AMBIGUOUS = "ambiguous"


class Action(StrEnum):
    KEEP = "keep"
    ROLLBACK = "rollback"


@dataclass(frozen=True)
class Observation:
    """One live run after a merge."""

    at: datetime
    passed: bool
    cost_tokens: int = 0


@dataclass(frozen=True)
class MonitorPolicy:
    window: timedelta = DEFAULT_WINDOW
    min_observations: int = DEFAULT_MIN_OBSERVATIONS
    regression_margin: float = DEFAULT_REGRESSION_MARGIN

    def __post_init__(self) -> None:
        if self.min_observations < 1:
            raise ValueError("min_observations must be at least 1")
        if not 0.0 <= self.regression_margin < 1.0:
            raise ValueError("regression_margin must be within [0.0, 1.0)")


@dataclass(frozen=True)
class MonitorResult:
    verdict: Verdict
    action: Action
    reason: str
    observed_pass_rate: float | None
    baseline_pass_rate: float
    observations: int
    settled: bool

    @property
    def halts_loop(self) -> bool:
        """A rollback of a change every gate passed means the gates have a
        blind spot, and merging on through it is the failure this halts."""
        return self.action is Action.ROLLBACK and self.verdict is Verdict.REGRESSED


def evaluate_window(
    observations: list[Observation] | tuple[Observation, ...],
    *,
    baseline_pass_rate: float,
    merged_at: datetime,
    now: datetime,
    policy: MonitorPolicy | None = None,
) -> MonitorResult:
    """Judge one merged change from its live runs."""
    policy = policy or MonitorPolicy()
    in_window = [o for o in observations if merged_at <= o.at <= merged_at + policy.window]
    count = len(in_window)
    settled = count >= policy.min_observations or now >= merged_at + policy.window

    if count == 0:
        return MonitorResult(
            verdict=Verdict.AMBIGUOUS,
            # Not yet settled, so nothing is rolled back — but the verdict is
            # ambiguous, never "healthy". Absence of evidence is not health.
            action=Action.ROLLBACK if settled else Action.KEEP,
            reason=(
                "no live runs observed yet"
                if not settled
                else "the window closed with no live runs at all — nothing can be concluded, "
                "so the change reverts rather than persisting unverified"
            ),
            observed_pass_rate=None,
            baseline_pass_rate=baseline_pass_rate,
            observations=0,
            settled=settled,
        )

    observed = sum(1 for o in in_window if o.passed) / count

    if observed < baseline_pass_rate - policy.regression_margin:
        return MonitorResult(
            verdict=Verdict.REGRESSED,
            action=Action.ROLLBACK,
            reason=(
                f"live pass rate {observed:.3f} is below the pre-merge baseline "
                f"{baseline_pass_rate:.3f} by more than {policy.regression_margin:.3f}"
            ),
            observed_pass_rate=observed,
            baseline_pass_rate=baseline_pass_rate,
            observations=count,
            settled=settled,
        )

    if count < policy.min_observations:
        return MonitorResult(
            verdict=Verdict.AMBIGUOUS,
            # Rollback-by-default: reverting a good change costs one
            # re-proposal; keeping a bad one compounds through every merge
            # built on top of it.
            action=Action.ROLLBACK if settled else Action.KEEP,
            reason=(
                f"only {count} of {policy.min_observations} observations"
                + (
                    "; the window closed without enough evidence, so the change reverts"
                    if settled
                    else "; still collecting"
                )
            ),
            observed_pass_rate=observed,
            baseline_pass_rate=baseline_pass_rate,
            observations=count,
            settled=settled,
        )

    return MonitorResult(
        verdict=Verdict.HEALTHY,
        action=Action.KEEP,
        reason=(
            f"live pass rate {observed:.3f} holds against the baseline "
            f"{baseline_pass_rate:.3f} over {count} run(s)"
        ),
        observed_pass_rate=observed,
        baseline_pass_rate=baseline_pass_rate,
        observations=count,
        settled=True,
    )


# --------------------------------------------------------------------------
# Kill switch
# --------------------------------------------------------------------------


class LoopHaltedError(RuntimeError):
    """The loop is halted. Raised rather than returned so no caller can
    proceed by ignoring a return value."""


@dataclass(frozen=True)
class KillSwitch:
    """A file, deliberately. The owner must be able to stop the loop without
    the loop's cooperation, from a shell, under stress, without reading
    documentation — so the mechanism is `touch <root>/HALTED`, and the loop
    checks it before doing anything rather than reporting it afterwards."""

    root: Path

    @property
    def path(self) -> Path:
        return self.root / KILL_SWITCH_FILENAME

    @property
    def engaged(self) -> bool:
        return self.path.is_file()

    @property
    def reason(self) -> str:
        return self.path.read_text().strip() if self.engaged else ""

    def engage(self, reason: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.path.write_text(reason.strip() + "\n")

    def release(self) -> None:
        """Owner-only by convention and by consequence: nothing in the
        harness calls this. Restarting after a halt is a decision, and a loop
        that could clear its own halt has no halt."""
        self.path.unlink(missing_ok=True)

    def check(self) -> None:
        if self.engaged:
            raise LoopHaltedError(
                f"the self-rewiring loop is halted: {self.reason or '(no reason recorded)'}. "
                f"Remove {self.path} to resume — a deliberate act, not an automatic one."
            )


REASON_PLACEHOLDER = "{reason}"
# Long enough for a stack of ledger detail, short enough that a channel
# cannot be used to exfiltrate a corpus through the notifier.
MAX_NOTIFICATION_BYTES = 16_384


@dataclass(frozen=True)
class HaltNotification:
    """What one attempt to reach the owner actually did.

    `delivered` is the command's exit status, not the owner's attention. This
    type cannot know whether anybody read anything, and it says so by naming
    the field after the thing it can observe.
    """

    at: datetime
    argv: tuple[str, ...]
    delivered: bool
    detail: str

    def render(self) -> str:
        mark = "delivered" if self.delivered else "FAILED"
        return f"{self.at.isoformat()} {mark}: {self.argv[0]} — {self.detail}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            # argv[0] only. The rest of an owner's command line is where a
            # token ends up when somebody writes one there, and this payload
            # goes into the ledger, which is committed evidence.
            "command": self.argv[0],
            "delivered": self.delivered,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class HaltChannel:
    """The command an owner named to be told the loop stopped (ADR 0195).

    An argv template, run as a subprocess. No network code, no dependency, no
    SDK: this repo does not know how its owner is paged, so it runs what it is
    given. `{reason}` in any argv element is replaced by the halt's reason; the
    whole notification — reason, time, and the ledger's last entry — is written
    to the command's stdin as one JSON object, so a channel that wants
    everything reads stdin and a channel that wants a subject line uses the
    slot.

    **Three things it deliberately is not.** It does not retry: a channel that
    retried would be a queue, and a queue that loses its process loses the
    message anyway — the durable record is the ledger, and this is the
    doorbell. It does not page anyone by itself; it runs a command, and whether
    that command reaches a human is the command's business. And it is exactly
    as reliable as what the owner wrote, which is why `notify` returns what it
    managed rather than raising, and why a failure is recorded beside the halt
    instead of replacing it.
    """

    argv: tuple[str, ...]
    timeout_s: float = 30.0
    # Injected for tests and for a caller that wants to route the command
    # somewhere else. `None` means a real subprocess.
    runner: Callable[[tuple[str, ...], str, float], tuple[int, str]] | None = None

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("a halt channel with no argv would tell nobody; name a command")
        if self.timeout_s <= 0:
            raise ValueError(f"halt channel timeout must be positive; got {self.timeout_s}")

    @property
    def description(self) -> str:
        """What the digest prints. argv[0] and the argument count — never the
        arguments, which is where an owner's token ends up."""
        return f"{self.argv[0]} ({len(self.argv) - 1} argument(s))"

    def notify(
        self, reason: str, *, at: datetime, last_entry: LedgerEntry | None
    ) -> HaltNotification:
        payload = json.dumps(
            {
                "event": "halt",
                "at": at.isoformat(),
                "reason": reason,
                "last_ledger_entry": (
                    {
                        "sequence": last_entry.sequence,
                        "kind": last_entry.kind.value,
                        "at": last_entry.at.isoformat(),
                        "proposal_id": last_entry.proposal_id,
                        "summary": last_entry.summary,
                    }
                    if last_entry is not None
                    else None
                ),
            },
            sort_keys=True,
        )[:MAX_NOTIFICATION_BYTES]
        argv = tuple(part.replace(REASON_PLACEHOLDER, reason) for part in self.argv)

        try:
            code, detail = (self.runner or _run_halt_command)(argv, payload, self.timeout_s)
        except Exception as exc:  # noqa: BLE001 - a failed channel must not mask the halt
            return HaltNotification(
                at=at,
                argv=argv,
                delivered=False,
                detail=f"{type(exc).__name__}: {exc}; the halt still stands",
            )
        return HaltNotification(
            at=at,
            argv=argv,
            delivered=code == 0,
            detail=detail or (f"exit {code}" if code else "exit 0"),
        )


def _run_halt_command(argv: tuple[str, ...], payload: str, timeout_s: float) -> tuple[int, str]:
    """The default runner: one subprocess, the payload on stdin, bounded.

    `shell=False` and no `env=` widening. A halt channel runs while the loop is
    already in a bad state, so it gets the narrowest invocation that can work.
    """
    import subprocess

    completed = subprocess.run(  # noqa: S603 - argv is owner-supplied by construction
        list(argv),
        input=payload,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    tail = (completed.stderr or completed.stdout or "").strip().splitlines()
    return completed.returncode, (tail[-1][:200] if tail else f"exit {completed.returncode}")


@dataclass(frozen=True)
class HaltNotifier:
    """Getting a halt in front of the owner (Q-A5).

    A halt that only fails a CI job depends on someone reading GitHub email,
    which `05-approval-policy.md` §7 says it must not. Three channels, in
    increasing order of reach:

    1. `HALT.md` at the repo root — impossible to miss on the next checkout
    2. a ledger entry — durable and bisectable
    3. a POST to a webhook the owner configures **outside the repo**, or a
       `HaltChannel` command the owner names in `aef.yaml` (ADR 0195)

    The webhook URL is passed in, never read here and never committed: no
    secret ships in this repository. The channel is an argv template read from
    the config, which is why it can exist in the repo at all — it names a
    program, not a credential. If NEITHER is set the digest says so **loudly on
    every run**, because an unconfigured alarm that stays quiet is worse than
    none: it looks like a working one.
    """

    webhook_url: str | None = None
    # Injected so the notifier is testable without a network, and so the
    # harness is not the thing that decides how to reach the outside world.
    poster: Callable[[str, str], None] | None = None
    # The owner's `halt_channel:` block, already built. `None` means none.
    channel: HaltChannel | None = None

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url) or self.channel is not None

    @property
    def description(self) -> str:
        """What the digest prints. Both channels named, neither's arguments."""
        parts = []
        if self.channel is not None:
            parts.append(f"command {self.channel.description}")
        if self.webhook_url:
            parts.append("webhook (AEF_HALT_WEBHOOK)")
        return ", ".join(parts)

    def notify(
        self,
        reason: str,
        *,
        repo_root: Path,
        at: datetime,
        last_entry: LedgerEntry | None = None,
    ) -> tuple[str, ...]:
        """Returns what it actually managed to do, not what it attempted."""
        done: list[str] = []

        halt_file = repo_root / "HALT.md"
        halt_file.write_text(
            f"# The self-rewiring loop is HALTED\n\n"
            f"**{at.isoformat()}**\n\n{reason}\n\n"
            f"Nothing will run until this is resolved. Read the ledger, then remove the\n"
            f"kill-switch file to resume — resuming is a decision, not a formality.\n"
        )
        done.append(f"wrote {halt_file}")

        if not self.configured:
            done.append(
                "NO HALT CHANNEL CONFIGURED — nothing was sent to anyone. Name a command in "
                "`halt_channel:` (aef.yaml) so a halt reaches you without you looking for it."
            )
            return tuple(done)

        if self.channel is not None:
            # Never raises: `HaltChannel.notify` converts every failure into a
            # recorded `HaltNotification`, because a channel that threw here
            # would replace the halt with the channel's own error.
            done.append(self.channel.notify(reason, at=at, last_entry=last_entry).render())

        if self.webhook_url:
            try:
                self._post(self.webhook_url, f"aef loop HALTED at {at.isoformat()}: {reason}")
                done.append("posted to the configured halt webhook")
            except Exception as exc:  # noqa: BLE001 - a failed notification must not hide the halt
                done.append(
                    f"halt webhook POST FAILED ({type(exc).__name__}); the halt still stands"
                )
        return tuple(done)

    def _post(self, url: str, body: str) -> None:
        if self.poster is not None:
            self.poster(url, body)
            return
        import json as _json
        import urllib.request

        request = urllib.request.Request(
            url,
            data=_json.dumps({"text": body}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(request, timeout=10).close()  # noqa: S310 - owner-supplied URL


# --------------------------------------------------------------------------
# The digest
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Digest:
    """The owner's real oversight surface: trends, not diffs."""

    since: datetime
    until: datetime
    proposed: int = 0
    merged: int = 0
    rejected: int = 0
    escalated: int = 0
    rolled_back: int = 0
    halts: int = 0
    blessed: int = 0
    security_events: int = 0
    # The subset of `security_events` that are `EventKind.CONTAINMENT` — a
    # shadow that ran without container containment and said so. Counted in
    # `security_events`, because the owner must know the candidate was not
    # contained; rendered separately, because the ordinary cause is no Docker
    # on the runner or `containment: off` in the config, and reporting S5's
    # fallback every Monday as "a proposal reached for the harness" is an
    # alarm firing on the normal case (ADR 0167).
    containment_events: int = 0
    containment_reasons: tuple[str, ...] = ()
    scenarios_added: int | None = None
    drift: float = 0.0
    owner_edits: int = 0
    # Two ways this loop can be quietly inert. Both are reported every run,
    # because a system that reports nothing looks identical to one with
    # nothing to report.
    halt_channel_configured: bool = False
    # What the channel IS, when there is one — `argv[0]` and an argument
    # count, never the arguments (ADR 0195). `""` when unconfigured, which is
    # the same fact `halt_channel_configured` carries and is rendered instead
    # of it so the owner reads the command rather than a yes.
    halt_channel: str = ""
    # The last few halt notifications, read from the ledger's own HALTED
    # entries. Reported because a channel that is configured and FAILING looks
    # exactly like a working one from the config alone — which is the failure
    # this whole line exists to end.
    halt_notifications: tuple[str, ...] = ()
    runs_recorded: int = 0

    @property
    def acceptance_rate(self) -> float | None:
        return (self.merged / self.proposed) if self.proposed else None

    @property
    def net_accepted(self) -> int:
        return self.merged - self.rolled_back

    @property
    def beating_manual_editing(self) -> bool | None:
        """Whether the loop is delivering more accepted change than the owner
        editing code directly.

        `None` when there is nothing to compare — reported honestly rather
        than defaulting to `True`, which would make a loop that does nothing
        look like a loop that is winning.
        """
        if self.owner_edits == 0 and self.net_accepted == 0:
            return None
        return self.net_accepted > self.owner_edits

    def render(self) -> str:
        rate = (
            f"{self.acceptance_rate:.0%}"
            if self.acceptance_rate is not None
            else "n/a (none proposed)"
        )
        benefit = {
            None: "n/a — nothing to compare yet",
            True: "yes",
            False: "NO — the loop is not out-performing manual editing",
        }[self.beating_manual_editing]

        lines = [
            f"# Self-rewiring digest, {self.since:%Y-%m-%d} to {self.until:%Y-%m-%d}",
            "",
            f"- Proposed: {self.proposed}",
            f"- Merged: {self.merged} (acceptance rate {rate})",
            f"- Rolled back: {self.rolled_back} — net accepted {self.net_accepted}",
            f"- Rejected: {self.rejected}",
            f"- Escalated to you: {self.escalated}",
            f"- Security events: {self.security_events}",
            f"- Halts: {self.halts}",
            "- Scenarios added to the corpus: "
            + (
                str(self.scenarios_added)
                if self.scenarios_added is not None
                else "unknown (not measured)"
            ),
            f"- Drift from the blessed baseline: {self.drift:.3f}"
            + ("" if self.blessed else " (no baseline blessed in this window)"),
            f"- Out-performing you editing code directly: {benefit}",
            f"- Production runs recorded: {self.runs_recorded}",
            "- Halt channel configured: "
            + (f"yes — {self.halt_channel}" if self.halt_channel_configured else "NO"),
        ]
        if not self.halt_channel_configured:
            lines += [
                "",
                "**No halt channel is configured.** If the loop halts, nothing will tell "
                "you — you will find out by noticing it stopped. Name a command in "
                "`halt_channel:` (aef.yaml); it is run with the halt's reason and the "
                "ledger's last entry, and this repo ships no guess about how you are paged.",
            ]
        if self.halt_notifications:
            # A configured channel that fails looks identical to a working one
            # from the config alone. These lines are the difference.
            lines += ["", "Halt notifications, most recent last:"]
            lines += [f"- {line}" for line in self.halt_notifications]
            if any("FAILED" in line for line in self.halt_notifications):
                lines += [
                    "  **A halt notification FAILED.** The halt still stands; what did not "
                    "happen is you being told about it. Fix the command, not the loop.",
                ]
        if self.runs_recorded == 0:
            lines += [
                "",
                "**No production runs were recorded**, so the corpus cannot grow and the "
                "gates keep measuring what the agent used to do. Pass `--record-runs` "
                "from your deployment.",
            ]
        elif self.scenarios_added == 0:
            # The line nobody drew. `Production runs recorded: 5` and
            # `Scenarios added to the corpus: 0` sat in one report with
            # nothing between them, and the warning above fires only on a
            # ZERO — so an owner who had followed its advice five times read
            # a digest that said nothing at all about the corpus not growing
            # (ADR 0163 §10, closed in ADR 0190).
            lines += [
                "",
                f"**{self.runs_recorded} recorded, 0 admitted to the corpus.** Recording "
                f"is not harvesting: a run becomes a scenario only if `aef loop harvest` "
                f"re-executes it identically and its behaviour survives redaction. Which "
                f"of those it was is in that command's own report line — run `aef loop "
                f"harvest <graph> --runs <dir> --corpus <dir>` and read it. Two answers "
                f"are ordinary — every run passed (harvest promotes failures unless "
                f"`--include-successes`), or the daily limit held them back — and one is "
                f"not: `REJECTED, did not re-execute deterministically` on every run "
                f"means the ingestion path is broken, not quiet.",
            ]
        # Two different things wearing one sentence. `security_event: True` was
        # rendered as "a proposal reached for the harness" whatever wrote it,
        # so an uncontained shadow — a runner with no Docker, or an owner who
        # wrote `containment: off` — was reported to that owner every Monday
        # as an attack (ADR 0167). Counted the same; named differently.
        if self.security_events > self.containment_events:
            lines += ["", "**A proposal reached for the harness. Read the ledger.**"]
        if self.containment_events:
            lines += ["", "**Shadow runs were not contained.**"]
            lines += [
                f"- shadow ran uncontained: {reason}"
                for reason in (self.containment_reasons or ("no reason recorded",))
            ]
            lines += [
                "  Counted as a security event because the candidate was not contained, "
                "not because it attacked anything. Install docker or podman, or say so "
                "deliberately in the config.",
            ]
        if self.beating_manual_editing is False:
            lines += [
                "",
                "Halt criterion 5 (`05-approval-policy.md` §6): no measurable benefit over "
                "editing directly. The program should stop for cost-benefit reasons, not "
                "only safety ones.",
            ]
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(
            {
                "since": self.since.isoformat(),
                "until": self.until.isoformat(),
                "proposed": self.proposed,
                "merged": self.merged,
                "rejected": self.rejected,
                "escalated": self.escalated,
                "rolled_back": self.rolled_back,
                "halts": self.halts,
                "security_events": self.security_events,
                "containment_events": self.containment_events,
                "containment_reasons": list(self.containment_reasons),
                "scenarios_added": self.scenarios_added,
                "drift": self.drift,
                "owner_edits": self.owner_edits,
                "acceptance_rate": self.acceptance_rate,
                "beating_manual_editing": self.beating_manual_editing,
                "halt_channel_configured": self.halt_channel_configured,
                "halt_channel": self.halt_channel,
                "halt_notifications": list(self.halt_notifications),
            },
            indent=2,
            sort_keys=True,
        )


_COUNTED: dict[EventKind, str] = {
    EventKind.PROPOSED: "proposed",
    EventKind.MERGED: "merged",
    EventKind.REJECTED: "rejected",
    EventKind.ESCALATED: "escalated",
    EventKind.ROLLED_BACK: "rolled_back",
    EventKind.HALTED: "halts",
    # Absent before, so the owner's weekly report never mentioned that a
    # baseline had been set — the one event every drift number is measured
    # against (ADR 0075).
    EventKind.BLESSED: "blessed",
}


def build_digest(
    entries: tuple[LedgerEntry, ...],
    *,
    since: datetime,
    until: datetime,
    drift: float = 0.0,
    scenarios_added: int | None = None,
    owner_edits: int = 0,
    halt_channel_configured: bool = False,
    halt_channel: str = "",
    max_halt_notifications: int = 5,
    runs_recorded: int = 0,
) -> Digest:
    counts = dict.fromkeys(_COUNTED.values(), 0)
    security_events = 0
    containment_events = 0
    containment_reasons: list[str] = []
    notifications: list[str] = []

    for entry in entries:
        if not since <= entry.at <= until:
            continue
        field_name = _COUNTED.get(entry.kind)
        if field_name is not None:
            counts[field_name] += 1
        if entry.detail.get("security_event"):
            security_events += 1
        if entry.kind is EventKind.CONTAINMENT:
            containment_events += 1
            reason = str(entry.detail.get("reason") or entry.summary or "no reason recorded")
            if reason not in containment_reasons:
                containment_reasons.append(reason)
        # Read from the ledger rather than passed in, so the digest reports
        # what the loop actually managed at halt time — including from a
        # process that is long gone (ADR 0195).
        if entry.kind is EventKind.HALTED:
            payload = entry.detail.get("halt_notification")
            if isinstance(payload, dict):
                mark = "delivered" if payload.get("delivered") else "FAILED"
                notifications.append(
                    f"{payload.get('at', entry.at.isoformat())} {mark}: "
                    f"{payload.get('command', '?')} — {payload.get('detail', '')}"
                )
            else:
                notifications.append(
                    f"{entry.at.isoformat()} NOT SENT: no halt channel was configured "
                    f"when the loop halted"
                )

    # A channel description implies a configured channel; the two cannot
    # disagree, because a digest that printed a command beside `NO` would be
    # unreadable.
    configured = halt_channel_configured or bool(halt_channel)
    return Digest(
        since=since,
        until=until,
        security_events=security_events,
        containment_events=containment_events,
        containment_reasons=tuple(containment_reasons),
        scenarios_added=scenarios_added,
        drift=drift,
        owner_edits=owner_edits,
        halt_channel_configured=configured,
        halt_channel=halt_channel,
        halt_notifications=tuple(notifications[-max_halt_notifications:]),
        runs_recorded=runs_recorded,
        **counts,
    )


# --------------------------------------------------------------------------
# Halt criteria (05-approval-policy.md §6)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HaltAssessment:
    should_halt: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def assess_halt(
    *,
    gated_rollback: bool = False,
    zone_violation: bool = False,
    drift_exhausted_twice: bool = False,
    consecutive_escalation_rejections: int = 0,
    digest: Digest | None = None,
) -> HaltAssessment:
    """The five halt criteria, evaluated together."""
    reasons: list[str] = []
    if gated_rollback:
        reasons.append(
            "a rollback was triggered by something every gate passed — the gates have a "
            "blind spot, and merging on through a known blind spot is the failure this stops"
        )
    if zone_violation:
        reasons.append(
            "a proposal reached for the harness or the core — a category signal, not a "
            "normal rejection"
        )
    if drift_exhausted_twice:
        reasons.append("the drift budget was exhausted twice in quick succession")
    if consecutive_escalation_rejections >= 2:
        reasons.append(
            f"{consecutive_escalation_rejections} consecutive escalations resolved by "
            f"rejection — the proposer is working outside its evidence base"
        )
    if digest is not None and digest.beating_manual_editing is False:
        reasons.append(
            "no measurable benefit over the owner editing code directly — the program is "
            "cost without benefit and should stop regardless of safety"
        )
    return HaltAssessment(should_halt=bool(reasons), reasons=tuple(reasons))


# --------------------------------------------------------------------------
# Is the scheduled cycle producing anything? (ADR 0165)
# --------------------------------------------------------------------------
#
# The metric that would have caught the defect ADR 0165 reproduces: this
# repo's own nightly `aef loop cycle` passed no `--memory`, printed "no
# memory store configured: nothing to learn from, no candidate", and exited
# 0 — every night, for as long as the workflow had existed, with nothing
# anywhere saying so.
#
# **The ledger cannot answer this on its own, and that is the whole problem.**
# `cycle` writes a ledger entry when it PROPOSES. A cycle that proposes
# nothing writes nothing at all, so the ledger is byte-identical between "a
# loop nobody has ever run" and "a loop that has run 180 times and produced
# nothing". Silence is the failure's own signature, which is exactly why a
# missing signal cannot be the alarm.
#
# So the attempt is journalled separately, by the CLI, on every cycle — and
# the alarm is `attempts exist AND no proposal in the last N of them`.

CYCLE_JOURNAL_FILENAME = "cycles.jsonl"

# Three nightly cycles in a row with nothing proposed. Not a tuned number: it
# is "long enough that one quiet night is not an alarm, short enough that a
# broken invocation is caught inside a week".
DEFAULT_QUIET_CYCLES = 3


@dataclass(frozen=True)
class CycleAttempt:
    """One TURN of the loop, whatever it produced.

    A turn, not an invocation, and the distinction is load-bearing. `aef loop
    cycle` runs one turn per invocation, so for it the two coincide; `aef loop
    run --turns 10` runs ten, each of which is a separate chance to propose.
    Journalling a `run` as a single attempt would make ten quiet turns count
    once, and the staleness alarm — which counts consecutive quiet attempts —
    would need thirty turns to fire instead of three (ADR 0167).
    """

    at: datetime
    proposed: bool
    # The last line the turn printed — the reason, in the loop's own words.
    verdict: str = ""
    # Which subcommand produced it: "cycle", "run". Recorded because the two
    # write into ONE journal and "which command has gone quiet" is the first
    # question anyone reading a stale loop asks. Empty for entries written
    # before ADR 0167, which is why nothing keys off it.
    command: str = ""


def record_cycle_attempt(
    state_root: Path, *, at: datetime, proposed: bool, verdict: str = "", command: str = ""
) -> None:
    """Append one attempt. Called by every `aef loop` subcommand that runs a
    turn — `cycle` and `run` — on EVERY path, including the ones that raise.

    Deliberately NOT the ledger: the ledger is a tamper-evident hash chain of
    decisions about candidates, and "a cycle ran and decided nothing" is not
    a decision about a candidate. Putting non-decisions in it would also mean
    the cycle mutates the audit trail on every no-op run.

    "On every path" is the ADR 0167 correction. This call used to sit after
    `cmd_cycle`'s `try`, so a turn that died on a halt or a config error
    returned before reaching it — and a nightly cycle failing the same way
    every night left a journal as empty as one nobody had ever run, which is
    the exact ambiguity the journal exists to remove.
    """
    state_root.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"at": at.isoformat(), "proposed": proposed, "verdict": verdict}
    if command:
        payload["command"] = command
    line = json.dumps(payload, sort_keys=True)
    with (state_root / CYCLE_JOURNAL_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_cycle_attempts(state_root: Path) -> tuple[CycleAttempt, ...]:
    """Attempts in the order they were written. A malformed line is skipped.

    Skipping is right here and wrong in `FileMemoryStore`: this journal is a
    monitoring signal, and a monitor that refuses to report because one line
    is corrupt is a monitor that goes dark exactly when something is wrong.
    """
    path = state_root / CYCLE_JOURNAL_FILENAME
    if not path.is_file():
        return ()
    out: list[CycleAttempt] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            out.append(
                CycleAttempt(
                    at=datetime.fromisoformat(str(payload["at"])),
                    proposed=bool(payload["proposed"]),
                    verdict=str(payload.get("verdict", "")),
                    command=str(payload.get("command", "")),
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return tuple(out)


@dataclass(frozen=True)
class CycleStaleness:
    """How long since the loop last did the thing it exists to do."""

    cycles_run: int = 0
    cycles_since_last_proposal: int = 0
    days_since_last_cycle: float | None = None
    days_since_last_proposed: float | None = None
    days_since_last_accepted: float | None = None
    last_verdict: str = ""
    warning: str | None = None

    def lines(self) -> tuple[str, ...]:
        def age(value: float | None) -> str:
            return "never" if value is None else f"{value:.1f} day(s) ago"

        out = [
            f"cycles run: {self.cycles_run} (last {age(self.days_since_last_cycle)})",
            f"last PROPOSED: {age(self.days_since_last_proposed)}",
            f"last KEPT/MERGED: {age(self.days_since_last_accepted)}",
        ]
        if self.warning:
            out.append(self.warning)
        return tuple(out)


def assess_cycle_staleness(
    entries: tuple[LedgerEntry, ...],
    attempts: tuple[CycleAttempt, ...],
    *,
    now: datetime,
    quiet_cycles: int = DEFAULT_QUIET_CYCLES,
) -> CycleStaleness:
    """Report — and name — a loop that runs and produces nothing.

    `PROPOSED` comes from the ledger because a proposal IS a ledger event.
    Acceptance is `KEPT` or `MERGED`: `KEPT` is `aef loop run` advancing its
    local branch (ADR 0114) and `MERGED` is Tier-1, which is off. Either one
    means a candidate survived every gate, which is the thing being aged.
    """

    def _days(at: datetime | None) -> float | None:
        return None if at is None else (now - at).total_seconds() / 86400.0

    def _latest(*kinds: EventKind) -> datetime | None:
        matching = [e.at for e in entries if e.kind in kinds]
        return max(matching) if matching else None

    last_proposed = _latest(EventKind.PROPOSED)
    last_accepted = _latest(EventKind.KEPT, EventKind.MERGED)

    quiet = 0
    for attempt in reversed(attempts):
        if attempt.proposed:
            break
        quiet += 1

    warning: str | None = None
    # `attempts` non-empty is the load-bearing half of the condition: a loop
    # nobody has run is not stale, it is unstarted, and warning about it
    # would train the reader to ignore the line.
    if attempts and quiet >= quiet_cycles:
        verdict = attempts[-1].verdict or "no reason recorded"
        warning = (
            f"WARNING: SCHEDULED CYCLE PRODUCING NOTHING — {quiet} consecutive cycle(s) "
            f"have run and proposed nothing. Last verdict: {verdict!r}. A cycle that "
            f"runs every night and never proposes is exit 0 having done nothing "
            f"(ADR 0139); check that --memory names a file something actually writes."
        )

    return CycleStaleness(
        cycles_run=len(attempts),
        cycles_since_last_proposal=quiet,
        days_since_last_cycle=_days(attempts[-1].at if attempts else None),
        days_since_last_proposed=_days(last_proposed),
        days_since_last_accepted=_days(last_accepted),
        last_verdict=attempts[-1].verdict if attempts else "",
        warning=warning,
    )
