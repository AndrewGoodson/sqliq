"""Milestone 2: one versioned JSON document per repo, from data already on disk.

This is the scalability seam, and it is worth more than the HTML. A server was
rejected for a design reason, not effort: N repos would need N daemons, N
ports, N lifecycles and a network dependency inside a scaffold whose sandbox
refuses to run without attested network isolation. A file has none of those
properties, and a fleet roll-up over N files needs nothing running anywhere.

Every value that reaches the payload goes through `contract.prepare`, which
raises for a field whose disclosure nobody decided. That is the milestone's
acceptance criterion — before this module existed, the disclosure registry had
no production caller, which is ADR 0092's defect class pointed at ADR 0107's
own rule.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aef.dash.contract import (
    PANELS,
    Known,
    Panel,
    PanelState,
    Reading,
    Unknown,
    UnknownReason,
    panel_spec,
    prepare,
)
from aef.harness import ledger
from aef.harness.monitoring import Digest, KillSwitch, build_digest

# Bumped when the payload SHAPE changes. A reader that accepted an older shape
# would be checking a different set of claims than it believes — the same
# reasoning as MANIFEST_VERSION in release.py, which refuses rather than
# migrates.
SCHEMA_VERSION = 1

DEFAULT_WINDOW_DAYS = 7


class ExportError(RuntimeError):
    pass


@dataclass(frozen=True)
class StatusExport:
    """What one repo reports about itself."""

    repo_name: str
    generated_at: datetime
    panels: Mapping[str, Panel]
    graph_mermaid: str = ""
    loop_mermaid: str = ""
    schema_version: int = SCHEMA_VERSION
    facts: Mapping[str, Any] = field(default_factory=dict)

    @property
    def worst_state(self) -> PanelState:
        """DEGRADED beats UNKNOWN beats HEALTHY.

        UNKNOWN outranks HEALTHY deliberately: a page whose summary reads green
        while three panels have no data behind them is the exact failure ADR
        0107 exists to prevent, one level up at the summary.
        """
        states = {panel.state for panel in self.panels.values()}
        if PanelState.DEGRADED in states:
            return PanelState.DEGRADED
        if PanelState.UNKNOWN in states:
            return PanelState.UNKNOWN
        return PanelState.HEALTHY

    def to_payload(self) -> dict[str, Any]:
        panels: dict[str, Any] = {}
        for key, panel in sorted(self.panels.items()):
            entry: dict[str, Any] = {
                "title": panel.spec.title,
                "watches": panel.spec.watches,
                "state": panel.state.value,
            }
            if isinstance(panel.reading, Unknown):
                entry["reason"] = panel.reading.reason.value
                entry["remedy"] = panel.reading.remedy
            else:
                entry["value"] = panel.reading.value
            panels[key] = entry

        return {
            "schema_version": self.schema_version,
            "repo_name": self.repo_name,
            "generated_at": self.generated_at.astimezone(UTC).isoformat(),
            "worst_state": self.worst_state.value,
            "panels": panels,
            "graph_mermaid": self.graph_mermaid,
            "loop_mermaid": self.loop_mermaid,
            "facts": dict(sorted(self.facts.items())),
        }

    def to_json(self) -> str:
        """`sort_keys` and fixed separators so the same state produces the same
        bytes. A byte-stable export is committable and diffable, which is how a
        human notices a change nobody announced."""
        return json.dumps(self.to_payload(), sort_keys=True, separators=(",", ":"), indent=2)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> StatusExport:
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ExportError(
                f"schema_version {version!r} is not {SCHEMA_VERSION}; this reader checks a "
                f"different set of claims than that export makes"
            )
        panels: dict[str, Panel] = {}
        for key, body in payload.get("panels", {}).items():
            spec = panel_spec(key)
            reading: Reading[object]
            if body.get("state") == PanelState.UNKNOWN.value:
                reading = Unknown(
                    reason=UnknownReason(body["reason"]),
                    remedy=body.get("remedy", ""),
                )
            else:
                reading = Known(value=body["value"], state=PanelState(body["state"]))
            panels[key] = Panel(spec=spec, reading=reading)
        return cls(
            repo_name=payload["repo_name"],
            generated_at=datetime.fromisoformat(payload["generated_at"]),
            panels=panels,
            graph_mermaid=payload.get("graph_mermaid", ""),
            loop_mermaid=payload.get("loop_mermaid", ""),
            facts=payload.get("facts", {}),
        )


# --------------------------------------------------------------------------
# Reading the real sources.
# --------------------------------------------------------------------------


def _ledger_panel(state_dir: Path | None) -> tuple[Reading[object], int, str]:
    """Returns the reading, the entry count, and any verification error.

    Verification runs here and its RESULT goes into the export as a
    first-class field. A dashboard that renders an unverified chain is worse
    than no dashboard: it converts a forged ledger into a green page. If
    verification fails the export is still produced, carrying the failure —
    an export that refuses to exist tells a fleet roll-up nothing, and silence
    is the state an attacker wants.
    """
    if state_dir is None:
        return (
            Unknown(
                reason=UnknownReason.NO_LOOP_STATE,
                remedy="run `aef loop gate` at least once, or pass --state",
            ),
            0,
            "",
        )
    if not (state_dir / ledger.LEDGER_FILENAME).is_file():
        return (
            Unknown(
                reason=UnknownReason.NO_LEDGER,
                remedy=f"no {ledger.LEDGER_FILENAME} in {state_dir.name}; the loop has not run",
            ),
            0,
            "",
        )
    try:
        count = ledger.verify(state_dir)
    except ledger.LedgerError as exc:
        return Known(value=False, state=PanelState.DEGRADED), 0, str(exc)
    return Known(value=True, state=PanelState.HEALTHY), count, ""


def _halt_panel(state_dir: Path | None) -> tuple[Reading[object], str]:
    if state_dir is None:
        return Unknown(
            reason=UnknownReason.NO_LOOP_STATE,
            remedy="pass --state to point at the loop state dir",
        ), ""
    switch = KillSwitch(state_dir)
    if switch.engaged:
        return Known(value=True, state=PanelState.DEGRADED), switch.reason
    return Known(value=False, state=PanelState.HEALTHY), ""


def _acceptance_panel(digest: Digest | None) -> Reading[object]:
    if digest is None:
        return Unknown(
            reason=UnknownReason.NO_LEDGER,
            remedy="the loop has recorded no events yet",
        )
    # The A1 defect, spelled correctly. `acceptance_rate` is None at zero
    # proposals, and that None is the absent case, not a zero.
    return Known.optional(
        digest.acceptance_rate,
        PanelState.HEALTHY if (digest.acceptance_rate or 0) > 0 else PanelState.DEGRADED,
        reason=UnknownReason.LEDGER_EMPTY,
        remedy="nothing has been proposed in the window; nothing to accept or reject",
    )


def _loop_mermaid(entries: tuple[ledger.LedgerEntry, ...]) -> str:
    """The proposal lifecycle, with occupancy on each transition.

    A separate graph from the agent's own topology, deliberately: they answer
    different questions and are built from different data, and conflating them
    is the design error the program prompt names.
    """
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.kind.value] = counts.get(entry.kind.value, 0) + 1

    def n(kind: str) -> str:
        return f"{counts.get(kind, 0)}"

    return "\n".join(
        [
            "flowchart LR",
            f'  proposed["proposed<br/>{n("proposed")}"]',
            f'  gated["gated<br/>{n("gated")}"]',
            f'  escalated["escalated<br/>{n("escalated")}"]',
            f'  merged["merged<br/>{n("merged")}"]',
            f'  rejected["rejected<br/>{n("rejected")}"]',
            f'  rolled_back["rolled back<br/>{n("rolled_back")}"]',
            f'  blessed["blessed<br/>{n("blessed")}"]',
            f'  halted["halted<br/>{n("halted")}"]',
            "  proposed --> gated",
            "  gated --> escalated",
            "  gated --> rejected",
            "  escalated --> merged",
            "  merged --> rolled_back",
            "  rolled_back --> halted",
            "  blessed -.-> proposed",
        ]
    )


def build_export(
    *,
    repo_name: str,
    state_dir: Path | None,
    now: datetime,
    graph_mermaid: str = "",
    runs_recorded: int = 0,
    corpus_scenarios: int = 0,
    halt_channel_configured: bool = False,
    drift: float = 0.0,
    owner_edits: int = 0,
) -> StatusExport:
    """Assemble one repo's status from whatever is actually there.

    Must produce a useful export for a repo with agents but NO loop state at
    all — the common case in a fleet, and the one that decides adoptability.
    Every loop panel reads UNKNOWN; the graph and run panels still work.
    """
    if state_dir is not None and not state_dir.is_dir():
        state_dir = None

    ledger_reading, entry_count, ledger_error = _ledger_panel(state_dir)
    halt_reading, halt_reason = _halt_panel(state_dir)

    entries: tuple[ledger.LedgerEntry, ...] = ()
    digest: Digest | None = None
    if state_dir is not None and entry_count > 0:
        entries = ledger.read(state_dir)
        digest = build_digest(
            entries,
            since=now.replace(microsecond=0) - _window(),
            until=now,
            drift=drift,
            scenarios_added=corpus_scenarios,
            owner_edits=owner_edits,
            halt_channel_configured=halt_channel_configured,
            runs_recorded=runs_recorded,
        )

    readings: dict[str, Reading[object]] = {
        "halt": halt_reading,
        "ledger_integrity": ledger_reading,
        "halt_channel": (
            Known(value=True, state=PanelState.HEALTHY)
            if halt_channel_configured
            else Unknown(
                reason=UnknownReason.HALT_CHANNEL_UNCONFIGURED,
                remedy="set a halt webhook; without one a halt reaches nobody",
            )
        ),
        "acceptance": _acceptance_panel(digest),
        "post_merge": (
            Known(value=digest.net_accepted, state=PanelState.HEALTHY)
            if digest is not None and digest.merged > 0
            else Unknown(
                reason=UnknownReason.MONITOR_NEVER_RAN,
                remedy="run `aef loop monitor` after a merge",
            )
        ),
        "drift": (
            Known(
                value=round(drift, 4),
                state=PanelState.HEALTHY if drift < 0.25 else PanelState.DEGRADED,
            )
            if digest is not None
            else Unknown(
                reason=UnknownReason.NO_LOOP_STATE,
                remedy="drift is measured against the blessed baseline; run `aef loop bless`",
            )
        ),
        "corpus": (
            Known(value=corpus_scenarios, state=PanelState.HEALTHY)
            if corpus_scenarios > 0
            else Unknown(
                reason=UnknownReason.CORPUS_EMPTY,
                remedy="run `aef loop harvest` to promote recorded runs into scenarios",
            )
        ),
        "agent_graph": (
            Known(value=graph_mermaid.count("\n") + 1, state=PanelState.HEALTHY)
            if graph_mermaid
            else Unknown(
                reason=UnknownReason.NO_GRAPH,
                remedy="point --graph at a module exposing build_graph()",
            )
        ),
        "loop_graph": (
            Known(value=len(entries), state=PanelState.HEALTHY)
            if entries
            else Unknown(
                reason=UnknownReason.LEDGER_EMPTY,
                remedy="the loop has recorded no lifecycle events yet",
            )
        ),
        "runs": (
            Known(value=runs_recorded, state=PanelState.HEALTHY)
            if runs_recorded > 0
            else Unknown(
                reason=UnknownReason.NO_RUNS_RECORDED,
                remedy="no agent traffic captured; run `aef run --record`",
            )
        ),
    }

    panels = {spec.key: Panel(spec=spec, reading=readings[spec.key]) for spec in PANELS}

    # Every one of these goes through `prepare`, which raises for a field whose
    # disclosure nobody decided. This is the milestone's acceptance criterion.
    facts: dict[str, Any] = {
        "schema_version": prepare("schema_version", SCHEMA_VERSION),
        "repo_name": prepare("repo_name", repo_name),
        "ledger_verified": prepare("ledger_verified", isinstance(ledger_reading, Known)),
        "ledger_entry_count": prepare("ledger_entry_count", entry_count),
        "kill_switch_engaged": prepare(
            "kill_switch_engaged", isinstance(halt_reading, Known) and bool(halt_reading.value)
        ),
        "runs_recorded": prepare("runs_recorded", runs_recorded),
        "scenarios_added": prepare("scenarios_added", corpus_scenarios),
        "halt_channel_configured": prepare("halt_channel_configured", halt_channel_configured),
        "drift": prepare("drift", round(drift, 4)),
        "owner_edits": prepare("owner_edits", owner_edits),
    }
    if ledger_error:
        facts["ledger_error"] = prepare("ledger_error", ledger_error)
    if halt_reason:
        facts["kill_switch_reason"] = prepare("kill_switch_reason", halt_reason)
    if digest is not None:
        for counter in ("proposed", "merged", "rejected", "escalated", "rolled_back", "blessed"):
            facts[counter] = prepare(counter, getattr(digest, counter))

    return StatusExport(
        repo_name=repo_name,
        generated_at=now,
        panels=panels,
        graph_mermaid=graph_mermaid,
        loop_mermaid=_loop_mermaid(entries),
        facts=facts,
    )


def _window() -> Any:
    from datetime import timedelta

    return timedelta(days=DEFAULT_WINDOW_DAYS)
