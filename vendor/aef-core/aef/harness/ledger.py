"""The proposal ledger — every candidate, gate outcome, decision, merge, and
rollback, in one append-only record.

Under tiered auto-merge (ADR 0045) there is no human in the merge path, so
the ledger *is* the audit trail. Two properties follow, and neither is
optional.

**Append-only, and tamper-evident.** Each entry carries the SHA-256 of the
previous entry, so the file is a hash chain: altering or removing an entry
breaks every link after it. "Append-only by convention" is not a property, it
is a hope — the whole point is that a system which can merge its own changes
could also, in principle, edit the record of having done so. It cannot edit
this one without the next verification failing.

**Bisectable.** Every merge names the proposal, the gate results with their
numbers, and the archive version it produced. A regression found in
production can be walked back to the change that introduced it without
guesswork, which is exactly what a post-merge monitoring system (M10) needs
to act on.

The chain is verified on read, not merely verifiable. A ledger that is only
checked when someone remembers to check is not evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64
LEDGER_FILENAME = "ledger.jsonl"


class LedgerError(RuntimeError):
    pass


class LedgerTamperedError(LedgerError):
    """The hash chain does not verify. Its own type: this is not a parse
    failure, it is evidence that the audit trail was altered."""


class EventKind(StrEnum):
    PROPOSED = "proposed"
    GATED = "gated"
    ESCALATED = "escalated"
    REJECTED = "rejected"
    MERGED = "merged"
    # An owner-blessed baseline. Deliberately NOT `MERGED`: the monitor rolls
    # every merged version back to its predecessor, and a baseline has none —
    # it would try to restore version 0 and raise (ADR 0073).
    BLESSED = "blessed"
    # `loop run` advanced its local kept branch to this candidate because
    # every gate passed (ADR 0114). Deliberately NOT `MERGED`: nothing
    # reached main, and the monitor must not try to roll it back.
    KEPT = "kept"
    ROLLED_BACK = "rolled_back"
    HALTED = "halted"
    # What a shadow run's evidence was gathered UNDER (ADR 0161). A gate
    # outcome cannot be read without it, and an uncontained shadow whose only
    # trace is a line on stderr is a condition nobody reviewing the ledger a
    # week later can see. Entries for an uncontained run also carry
    # `security_event: True`, which is what puts them in the owner's digest —
    # under their OWN heading there, because the ordinary cause is no Docker
    # on the runner or an owner who wrote `containment: off`, and reporting
    # that every Monday as "a proposal reached for the harness" is an alarm
    # firing on the normal case (ADR 0167).
    CONTAINMENT = "containment"
    # The roster of a turn that tried more than one candidate, and which one
    # it kept (ADR 0200). Each candidate ALSO has its own PROPOSED/GATED/
    # REJECTED-or-ESCALATED entries — this adds the one fact those cannot
    # carry, which is that they were alternatives to each other and how the
    # choice between them was made. Not counted by the digest: it would
    # double-count proposals that are already counted one by one.
    CANDIDATES = "candidates"
    # A held-out read of a candidate the gates already passed (ADR 0200).
    # ADVISORY: it changes no disposition, and its entry says so, because a
    # set the loop is selected against is not a set that is held out.
    AUDIT = "audit"


@dataclass(frozen=True)
class LedgerEntry:
    sequence: int
    kind: EventKind
    at: datetime
    proposal_id: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    previous_hash: str = GENESIS_HASH

    def body(self) -> dict[str, Any]:
        """Everything the hash covers. `entry_hash` is excluded by
        construction — a hash cannot cover itself."""
        return {
            "sequence": self.sequence,
            "kind": self.kind.value,
            "at": self.at.isoformat(),
            "proposal_id": self.proposal_id,
            "summary": self.summary,
            "detail": self.detail,
            "previous_hash": self.previous_hash,
        }

    @property
    def entry_hash(self) -> str:
        canonical = json.dumps(self.body(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_line(self) -> str:
        payload = self.body()
        payload["entry_hash"] = self.entry_hash
        return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> LedgerEntry:
        try:
            return cls(
                sequence=int(payload["sequence"]),
                kind=EventKind(payload["kind"]),
                at=datetime.fromisoformat(payload["at"]),
                proposal_id=payload["proposal_id"],
                summary=payload["summary"],
                detail=dict(payload.get("detail", {})),
                previous_hash=payload["previous_hash"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerError(f"malformed ledger entry: {exc}") from exc


def _path(root: Path) -> Path:
    return root / LEDGER_FILENAME


def read(root: Path) -> tuple[LedgerEntry, ...]:
    """Every entry, with the chain verified. Raises rather than returning a
    ledger whose links do not hold."""
    path = _path(root)
    if not path.is_file():
        return ()

    entries: list[LedgerEntry] = []
    expected_previous = GENESIS_HASH
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"{path}:{number}: not valid JSON: {exc}") from exc

        recorded_hash = payload.pop("entry_hash", None)
        entry = LedgerEntry.from_payload(payload)

        if entry.entry_hash != recorded_hash:
            raise LedgerTamperedError(
                f"{path}:{number}: entry {entry.sequence} hash mismatch — recorded "
                f"{recorded_hash}, computed {entry.entry_hash}. The entry was altered after "
                f"it was written."
            )
        if entry.previous_hash != expected_previous:
            raise LedgerTamperedError(
                f"{path}:{number}: entry {entry.sequence} does not follow the previous one "
                f"({entry.previous_hash} != {expected_previous}). An entry was removed, "
                f"reordered, or inserted."
            )
        if entry.sequence != len(entries) + 1:
            raise LedgerTamperedError(
                f"{path}:{number}: expected sequence {len(entries) + 1}, got {entry.sequence}"
            )
        entries.append(entry)
        expected_previous = entry.entry_hash

    return tuple(entries)


def append(
    root: Path,
    *,
    kind: EventKind,
    at: datetime,
    proposal_id: str,
    summary: str,
    detail: dict[str, Any] | None = None,
) -> LedgerEntry:
    """Append one entry, chained to the current tail.

    Reads and verifies the whole ledger first: appending onto a broken chain
    would extend the damage and make the tampering harder to locate.
    """
    existing = read(root)
    entry = LedgerEntry(
        sequence=len(existing) + 1,
        kind=kind,
        at=at,
        proposal_id=proposal_id,
        summary=summary,
        detail=detail or {},
        previous_hash=existing[-1].entry_hash if existing else GENESIS_HASH,
    )

    root.mkdir(parents=True, exist_ok=True)
    path = _path(root)
    # Append mode with an explicit flush+fsync: a torn tail line fails the
    # chain check on the next read rather than being silently absorbed.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry.to_line())
        handle.flush()
        import os

        os.fsync(handle.fileno())
    return entry


def verify(root: Path) -> int:
    """Verify the chain and return the number of entries."""
    return len(read(root))


def history_for(root: Path, proposal_id: str) -> tuple[LedgerEntry, ...]:
    return tuple(e for e in read(root) if e.proposal_id == proposal_id)


def merged_versions(root: Path) -> tuple[tuple[str, int], ...]:
    """`(proposal_id, archive_version)` for every merge, in order — the
    bisection index a post-merge regression is walked back through."""
    out: list[tuple[str, int]] = []
    for entry in read(root):
        if entry.kind is EventKind.MERGED and "archive_version" in entry.detail:
            out.append((entry.proposal_id, int(entry.detail["archive_version"])))
    return tuple(out)
