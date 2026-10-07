"""The archive — every accepted change, restorable exactly.

Must exist before anything is ever accepted, so that anything accepted can
be undone.

**It stores content, not references.** The three-reviewer audit found this
directly (04 §1.9): a palette ref is a *name*, and the code behind that name
changes under ordinary human PRs. An archive recording "version 7 used
`agents.planner:build`" would restore a version whose *meaning* had silently
changed. So each entry stores the bytes of every Zone A file plus a SHA-256
of each, and a restore verifies the digests before writing anything.

**It does not depend on git.** Git shas are recorded because they are useful
provenance, but the content lives in the archive: an archive that stops
working after `git gc`, a force-push, or a branch deletion is not an archive.

**It is append-only.** Versions are monotonic and an existing version is
never overwritten — `record()` refuses. A rollback does not delete the
version it rolled back from; it appends a new version whose content is the
old one, so the history of what was tried stays legible.

A **second, deliberately separate store** lives at the bottom of this module:
the *lineage* archive (ADR 0160), which records every candidate `run_loop`
produced — kept and rejected alike — so DGM-style parent sampling survives
across invocations. It is not a version store and no rollback path reads it;
the comment above `LINEAGE_DIRNAME` says why keeping the two apart is a
safety property rather than a filing preference.

Writes use the kernel's crash-safe helper (`durability._atomic_write_text`:
temp file in the same directory, `fsync`, `os.replace`, directory `fsync`) —
deliberately the same implementation the checkpointer uses rather than a
second one that would need proving separately (ADR 0031).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from dataclasses import replace as _replace
from datetime import datetime
from pathlib import Path
from typing import Any

from aef.harness.zones import segment_refusal
from aef.kernel.durability import _atomic_write_text

ENTRY_FILENAME = "entry.json"
FILES_DIRNAME = "files"


class ArchiveError(RuntimeError):
    pass


class ArchiveIntegrityError(ArchiveError):
    """Stored content does not match its recorded digest. Its own type
    because this is corruption or tampering, not a missing entry."""


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class ArchiveEntry:
    version: int
    graph_id: str
    base_sha: str
    head_sha: str
    recorded_at: datetime
    file_digests: dict[str, str] = field(default_factory=dict)
    gate_report: tuple[str, ...] = ()
    rolled_back_from: int | None = None
    notes: str = ""
    # WHICH Zone A tree this entry is the baseline of. A baseline is the whole
    # agent root, so the root is part of what was blessed — and nothing wrote
    # it down until ADR 0167, so a baseline blessed under `--agent-root
    # .claude/agents` and a later cycle at the default `agents` root compared
    # two disjoint trees and charged the first candidate 1.000 drift.
    # Defaults to "" so every entry written before this field existed still
    # loads, and "" means "not recorded" rather than "the repo root".
    agent_root: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "graph_id": self.graph_id,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "recorded_at": self.recorded_at.isoformat(),
            "file_digests": dict(sorted(self.file_digests.items())),
            "gate_report": list(self.gate_report),
            "rolled_back_from": self.rolled_back_from,
            "notes": self.notes,
            "agent_root": self.agent_root,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ArchiveEntry:
        try:
            return cls(
                version=int(payload["version"]),
                graph_id=payload["graph_id"],
                base_sha=payload["base_sha"],
                head_sha=payload["head_sha"],
                recorded_at=datetime.fromisoformat(payload["recorded_at"]),
                file_digests=dict(payload.get("file_digests", {})),
                gate_report=tuple(payload.get("gate_report", ())),
                rolled_back_from=payload.get("rolled_back_from"),
                notes=payload.get("notes", ""),
                agent_root=str(payload.get("agent_root", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ArchiveError(f"malformed archive entry: {exc}") from exc


def _graph_dir(root: Path, graph_id: str) -> Path:
    """`<root>/<graph_id>`, and the graph id must be ONE safe path segment.

    It was `root / graph_id` with nothing between them, and a graph id is not
    an internal token: `aef migrate` takes it from a persona's `name:`
    frontmatter and prints it in its report as the value to hand
    `aef loop bless --graph-id`. Reproduced (ADR 0168) — `record(root,
    "../escape", ...)` with `root` at `state/archive`:

        recorded version 1
        archive root contents: []
        WROTE state/escape/v000001/entry.json
        WROTE state/escape/v000001/files/agents/graph.py

    One level ABOVE the archive root it was handed, with the archive root left
    empty. `pathlib` makes the absolute form worse still: `root / "/etc/x"`
    discards `root` entirely.

    Refused rather than sanitised, and refused in the ONE place every read and
    every write goes through, so `versions()` cannot report on a directory
    `record()` would not create. Sanitising would silently map two ids onto one
    archive, which for an append-only store is the failure it exists to
    prevent. `aef migrate` no longer mints such an id (`PromptAgentSite.
    graph_id`); this is the containment behind that, for an id typed by hand.
    """
    refusal = segment_refusal(graph_id)
    if refusal:
        raise ArchiveError(
            f"graph_id {graph_id!r} is not usable as an archive directory: {refusal}. "
            f"A graph id is joined onto the archive root, so it must be one path "
            f"segment — no '/', no '..', no leading '/'. `aef migrate` reports the "
            f"safe id it generated for each agent; pass that."
        )
    return root / graph_id


def _version_dir(root: Path, graph_id: str, version: int) -> Path:
    return _graph_dir(root, graph_id) / f"v{version:06d}"


def versions(root: Path, graph_id: str) -> tuple[int, ...]:
    directory = _graph_dir(root, graph_id)
    if not directory.is_dir():
        return ()
    found = sorted(
        int(p.name[1:]) for p in directory.iterdir() if p.is_dir() and p.name.startswith("v")
    )
    return tuple(found)


def next_version(root: Path, graph_id: str) -> int:
    existing = versions(root, graph_id)
    return (existing[-1] + 1) if existing else 1


def record(
    root: Path,
    graph_id: str,
    *,
    files: dict[str, bytes],
    base_sha: str,
    head_sha: str,
    recorded_at: datetime,
    gate_report: tuple[str, ...] = (),
    rolled_back_from: int | None = None,
    notes: str = "",
    agent_root: str = "",
) -> ArchiveEntry:
    """Append a new version. Refuses to overwrite an existing one."""
    version = next_version(root, graph_id)
    directory = _version_dir(root, graph_id, version)
    if directory.exists():  # pragma: no cover - next_version precludes it
        raise ArchiveError(f"archive version {version} for {graph_id!r} already exists")

    entry = ArchiveEntry(
        version=version,
        graph_id=graph_id,
        base_sha=base_sha,
        head_sha=head_sha,
        recorded_at=recorded_at,
        file_digests={path: digest(content) for path, content in files.items()},
        gate_report=gate_report,
        rolled_back_from=rolled_back_from,
        notes=notes,
        agent_root=agent_root,
    )

    files_dir = directory / FILES_DIRNAME
    for path, content in files.items():
        target = (files_dir / path).resolve()
        if not target.is_relative_to(files_dir.resolve()):
            raise ArchiveError(f"refusing to archive {path!r} outside the version directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    directory.mkdir(parents=True, exist_ok=True)
    # Written last, and atomically: the entry file is what makes a version
    # real, so a crash mid-write leaves a directory with no entry rather than
    # an entry describing files that are not all there.
    _atomic_write_text(
        directory / ENTRY_FILENAME, json.dumps(entry.to_payload(), indent=2, sort_keys=True) + "\n"
    )
    return entry


def load_entry(root: Path, graph_id: str, version: int) -> ArchiveEntry:
    path = _version_dir(root, graph_id, version) / ENTRY_FILENAME
    if not path.is_file():
        raise ArchiveError(f"no archived version {version} for graph {graph_id!r}")
    try:
        return ArchiveEntry.from_payload(json.loads(path.read_text()))
    except json.JSONDecodeError as exc:
        raise ArchiveError(f"{path}: not valid JSON: {exc}") from exc


def read_files(root: Path, graph_id: str, version: int) -> dict[str, bytes]:
    """Archived content, **digest-verified**. Raises rather than returning
    content that does not match what was recorded."""
    entry = load_entry(root, graph_id, version)
    files_dir = _version_dir(root, graph_id, version) / FILES_DIRNAME

    contents: dict[str, bytes] = {}
    for path, expected in entry.file_digests.items():
        blob = files_dir / path
        if not blob.is_file():
            raise ArchiveIntegrityError(f"{graph_id} v{version}: archived file {path!r} is missing")
        content = blob.read_bytes()
        actual = digest(content)
        if actual != expected:
            raise ArchiveIntegrityError(
                f"{graph_id} v{version}: {path!r} digest mismatch — recorded {expected}, "
                f"found {actual}. The archive has been altered; a rollback from it would "
                f"restore something other than what was accepted."
            )
        contents[path] = content
    return contents


def verify(root: Path, graph_id: str, version: int) -> None:
    read_files(root, graph_id, version)


def rollback(
    root: Path,
    graph_id: str,
    version: int,
    dest: Path,
    *,
    recorded_at: datetime,
    notes: str = "",
) -> ArchiveEntry:
    """Restore `version`'s content into `dest` and append it as a NEW version.

    A rollback is itself an owner-visible change, so it is archived rather
    than pretended never to have happened. The version rolled back *from* is
    left in place: erasing it would hide the very thing a rollback is
    evidence about.
    """
    contents = read_files(root, graph_id, version)
    previous = next_version(root, graph_id) - 1

    dest = dest.resolve()
    for path, content in contents.items():
        target = (dest / path).resolve()
        if not target.is_relative_to(dest):  # pragma: no cover - record() precludes it
            raise ArchiveError(f"refusing to restore {path!r} outside {dest}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    source = load_entry(root, graph_id, version)
    return record(
        root,
        graph_id,
        files=contents,
        base_sha=source.base_sha,
        head_sha=source.head_sha,
        recorded_at=recorded_at,
        gate_report=(f"rollback to v{version}",),
        rolled_back_from=previous,
        notes=notes or f"rollback to v{version}",
    )


def check_never_shrinks(root: Path, graph_id: str, known: tuple[int, ...]) -> None:
    """Raise unless every previously-recorded version is still present."""
    present = set(versions(root, graph_id))
    missing = sorted(set(known) - present)
    if missing:
        raise ArchiveError(
            f"archive for {graph_id!r} lost version(s) {missing}; history that can be "
            f"deleted is not an audit trail"
        )


# ---------------------------------------------------------------------------
# The lineage store — DGM's archive, across invocations (ADR 0160)
# ---------------------------------------------------------------------------
#
# WHY THIS IS A SEPARATE STORE, BESIDE THE VERSIONS ABOVE RATHER THAN INSIDE
# THEM. Everything above records *accepted content*: the bytes of every Zone A
# file at a version an owner blessed or the loop merged, digest-verified, and
# `check_never_shrinks` makes losing one a preflight failure. Lineage records
# something else — every candidate a `run_loop` produced, INCLUDING the ones
# the gates rejected, which are exactly the members whose content must never
# be restorable by `rollback` and must never appear in `versions()`, whose
# first element `_blessed_files` reads as the baseline G5 measures drift
# against. Writing rejects into that store would put un-gated content one
# `aef loop monitor` rollback away from Zone A, and would make an ordinary
# loop run trip an integrity invariant that exists to protect merges. So:
# same directory tree, same durability discipline, same digests, a different
# file, and no rollback path reads it.
#
# It is resume state, not the audit trail — the ledger is the audit trail and
# is hash-chained. A lineage record therefore carries a digest of its own
# payload (corruption is detected) but no chain (a torn history is not an
# integrity emergency): the remedy for a corrupt lineage file is to delete it,
# which costs the loop its resume state and costs the audit trail nothing.

LINEAGE_DIRNAME = "lineage"


class LineageIntegrityError(ArchiveError):
    """A persisted lineage record does not match its recorded digest."""


@dataclass(frozen=True)
class LineageRecord:
    """One member of the lineage archive, as persisted.

    `kept` is the gate verdict: True when every gate passed and the loop
    advanced its kept branch onto this candidate, False when it was reverted.
    Rejected members are recorded — they are DGM's stepping stones, and a
    stepping stone nobody wrote down is not one.
    """

    run_id: str
    turn: int
    ref: str
    tree: str
    parent_ref: str | None
    score: float | None
    kept: bool
    disposition: str | None
    recorded_at: datetime
    children: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "turn": self.turn,
            "ref": self.ref,
            "tree": self.tree,
            "parent_ref": self.parent_ref,
            "score": self.score,
            "kept": self.kept,
            "disposition": self.disposition,
            "recorded_at": self.recorded_at.isoformat(),
            "children": self.children,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> LineageRecord:
        try:
            return cls(
                run_id=str(payload["run_id"]),
                turn=int(payload["turn"]),
                ref=str(payload["ref"]),
                tree=str(payload["tree"]),
                parent_ref=payload["parent_ref"],
                score=payload["score"],
                kept=bool(payload["kept"]),
                disposition=payload["disposition"],
                recorded_at=datetime.fromisoformat(payload["recorded_at"]),
                children=int(payload.get("children", 0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ArchiveError(f"malformed lineage record: {exc}") from exc


def lineage_path(root: Path, graph_id: str) -> Path:
    """`<root>/<graph_id>.jsonl`, with ADR 0168's refusal.

    The same hole, in a store written after the fix landed: a graph id comes
    from a persona's `name:` frontmatter or an owner's `--graph-id`, and
    `root / f"{graph_id}.jsonl"` with `graph_id="../escape"` writes one level
    above the state directory. Refused in the one place every lineage read and
    write goes through, for the same reason `_graph_dir` refuses rather than
    sanitises: two ids silently mapping onto one file is exactly the failure
    an append-only store exists to prevent.
    """
    refusal = segment_refusal(graph_id)
    if refusal:
        raise ArchiveError(
            f"graph_id {graph_id!r} is not usable as a lineage filename: {refusal}. "
            f"A graph id is joined onto the lineage directory, so it must be one path "
            f"segment — no '/', no '..', no leading '/'."
        )
    return root / f"{graph_id}.jsonl"


def _lineage_digest(payload: dict[str, Any]) -> str:
    return digest(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def append_lineage(root: Path, graph_id: str, record: LineageRecord) -> LineageRecord:
    """Append one member. Append mode with flush+fsync, like the ledger: a
    torn tail line fails its own digest check on the next read rather than
    being silently absorbed as a member that was never proposed."""
    # The id is checked BEFORE anything is created: a refusal that has already
    # made a directory is a refusal that leaves a trace of the thing it
    # refused.
    path = lineage_path(root, graph_id)
    payload = record.to_payload()
    line = (
        json.dumps({"record": payload, "digest": _lineage_digest(payload)}, sort_keys=True) + "\n"
    )
    root.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return record


def read_lineage(root: Path, graph_id: str) -> tuple[LineageRecord, ...]:
    """Every persisted member, digest-verified, oldest first."""
    path = lineage_path(root, graph_id)
    if not path.is_file():
        return ()
    out: list[LineageRecord] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            envelope = json.loads(raw)
            payload = envelope["record"]
            recorded = envelope["digest"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise LineageIntegrityError(f"{path}:{number}: not a lineage record: {exc}") from exc
        actual = _lineage_digest(payload)
        if actual != recorded:
            raise LineageIntegrityError(
                f"{path}:{number}: digest mismatch — recorded {recorded}, found {actual}. "
                f"The lineage archive has been altered; a parent sampled from it would not "
                f"be the candidate that was gated. Delete the file to start a fresh lineage: "
                f"it is resume state, and the ledger keeps the audit trail."
            )
        out.append(LineageRecord.from_payload(payload))
    return tuple(out)


def fold_lineage(records: Sequence[LineageRecord]) -> dict[str, LineageRecord]:
    """One member per ref, in the order the refs first appear (ADR 0198).

    THE fold, in one place, because there were two and they disagreed. The
    file is append-only and a member is written **twice**: once when it is
    gated, and once at the end of the run that proposed from it, carrying the
    `children` count — which only exists when the run is over.

    Everything about a member except that count is a fact about the moment it
    was gated and is never re-measured: its ref, its tree, its parent, its
    score, its verdict. So the fold takes the FIRST record for a ref and the
    LARGEST children count across all of them.

    The fold that was here — last record wins, wholesale — read the closing
    record's fields as an update, and the closing record for a **resumed
    root** carries none of them. `run_loop` constructs its root from the kept
    branch with `parent_ref=None`, so its closing record overwrote the
    candidate's own parent and gate verdict with nulls, and one further
    invocation erased the fact that the kept branch had ever been a candidate.
    Measured on the J2b greedy arm (ADR 0198): turn 1 kept `a4d0bfe4` from
    `c01e9b06` at 0.818; after the resumed invocation the same ref read back
    `parent_ref None, disposition None`, so the count of kept members with a
    parent — and the stepping-stone statistic built on it — was 0 for a run
    that had kept one. An archive whose whole claim is that it remembers
    where the loop has been may not forget the parent of its own head.
    """
    folded: dict[str, LineageRecord] = {}
    for record in records:
        seen = folded.get(record.ref)
        if seen is None:
            folded[record.ref] = record
        elif record.children > seen.children:
            folded[record.ref] = _replace(seen, children=record.children)
    return folded
