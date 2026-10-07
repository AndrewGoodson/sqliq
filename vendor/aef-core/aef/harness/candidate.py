"""Reading a candidate — what the agent actually proposes.

A candidate is `git diff base...head`: the changes on the branch since its
merge-base with the base ref. The candidate is **input** to the harness,
never part of it (`trust.py`).

Three escapes live at this layer rather than in `zones.py`, because they are
invisible to a path classifier:

1. **Rename detection.** With it on, moving `aef/kernel/executor.py` to
   `agents/executor.py` reports only the destination, so the zone check sees
   one Zone A file and waves through deletion of a core one. `--no-renames`
   (see `git.py`) reports both halves.
2. **Symlinks.** `agents/link -> ../aef/kernel/executor.py` is a Zone A path
   by every string test, and writing through it lands in Zone C.
3. **Submodules (gitlinks).** A submodule entry is a Zone A path that pulls
   in an arbitrary external tree.

So the mode of every landed blob is checked, not just its path.
"""

from __future__ import annotations

from dataclasses import dataclass

from aef.harness.git import GitRepo
from aef.harness.zones import ZonePolicy, ZoneVerdict, enforce_zones

MODE_REGULAR = "100644"
MODE_ABSENT = "000000"  # the destination side of a deletion

# Deny-by-default: only a plain regular file may land. Everything else is
# named so the rejection explains itself.
MODE_NAMES = {
    "100755": "executable file",
    "120000": "symlink",
    "160000": "submodule (gitlink)",
}
# A symlink or submodule is an escape *mechanism*; an executable bit is only
# a privilege the agent does not need. The first pair are security events.
ESCAPE_MODES = frozenset({"120000", "160000"})


@dataclass(frozen=True)
class DiffEntry:
    path: str
    status: str
    src_mode: str
    dst_mode: str
    added_lines: int = 0
    removed_lines: int = 0
    # git reports "-" for line counts it will not compute (binary files, or
    # anything a `.gitattributes` marks binary). Defaulting that to 0 made a
    # 5,050-line rewrite report as 1 changed line and sail past the size
    # budget — measured, not hypothesised. Unmeasurable must deny.
    size_unknown: bool = False

    @property
    def is_deletion(self) -> bool:
        return self.dst_mode == MODE_ABSENT


@dataclass(frozen=True)
class ModeViolation:
    path: str
    mode: str
    reason: str
    security_event: bool


@dataclass(frozen=True)
class CandidateDiff:
    base_ref: str
    head_ref: str
    base_sha: str
    head_sha: str
    entries: tuple[DiffEntry, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(e.path for e in self.entries)

    @property
    def changed_files(self) -> int:
        return len(self.entries)

    @property
    def changed_lines(self) -> int:
        return sum(e.added_lines + e.removed_lines for e in self.entries)

    @property
    def unmeasurable(self) -> tuple[str, ...]:
        """Paths whose change size git would not compute. A size budget
        cannot be applied to these, so they are rejected rather than counted
        as zero."""
        return tuple(e.path for e in self.entries if e.size_unknown and not e.is_deletion)

    @property
    def is_empty(self) -> bool:
        return not self.entries


@dataclass(frozen=True)
class CandidateVerdict:
    diff: CandidateDiff
    zones: ZoneVerdict
    mode_violations: tuple[ModeViolation, ...]

    @property
    def allowed(self) -> bool:
        return self.zones.allowed and not self.mode_violations

    @property
    def security_events(self) -> tuple[str, ...]:
        return tuple(v.path for v in self.zones.security_events) + tuple(
            v.path for v in self.mode_violations if v.security_event
        )

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(v.reason for v in self.zones.rejected) + tuple(
            v.reason for v in self.mode_violations
        )


def _split_z(raw: bytes) -> list[str]:
    text = raw.decode("utf-8", errors="replace")
    return [part for part in text.split("\0") if part]


def _parse_numstat(raw: bytes) -> dict[str, tuple[int, int, bool]]:
    """One NUL-terminated record per file: `added\\tremoved\\tpath`.

    Note this is NOT the `--raw -z` shape (which puts the path in its own
    NUL-separated field) — verified against real git output rather than
    assumed. Only the first two tabs delimit, so a tab inside a filename
    stays in the path. Binary files report `-` for both counts.
    """
    counts: dict[str, tuple[int, int, bool]] = {}
    for record in _split_z(raw):
        added, _, rest = record.partition("\t")
        removed, _, path = rest.partition("\t")
        if not path:  # pragma: no cover - only reachable with rename detection on
            continue
        counts[path] = (
            int(added) if added.isdigit() else 0,
            int(removed) if removed.isdigit() else 0,
            not (added.isdigit() and removed.isdigit()),
        )
    return counts


def read_candidate(repo: GitRepo, base_ref: str, head_ref: str) -> CandidateDiff:
    """`:<src_mode> <dst_mode> <src_sha> <dst_sha> <status>\\0<path>\\0`."""
    # Resolve both movable names before any diff command. Otherwise a branch
    # update between numstat and raw_diff can pair the small line count from
    # one commit with the paths and blobs from another.
    base_sha = repo.rev_parse(base_ref)
    head_sha = repo.rev_parse(head_ref)
    counts = _parse_numstat(repo.numstat_diff(base_sha, head_sha))
    fields = _split_z(repo.raw_diff(base_sha, head_sha))

    entries: list[DiffEntry] = []
    for meta, path in zip(fields[::2], fields[1::2], strict=False):
        parts = meta.lstrip(":").split()
        if len(parts) < 5:  # pragma: no cover - git does not emit this
            continue
        added, removed, unknown = counts.get(path, (0, 0, True))
        entries.append(
            DiffEntry(
                path=path,
                status=parts[4],
                src_mode=parts[0],
                dst_mode=parts[1],
                added_lines=added,
                removed_lines=removed,
                size_unknown=unknown,
            )
        )

    return CandidateDiff(
        base_ref=base_ref,
        head_ref=head_ref,
        base_sha=base_sha,
        head_sha=head_sha,
        entries=tuple(entries),
    )


def check_modes(diff: CandidateDiff) -> tuple[ModeViolation, ...]:
    violations: list[ModeViolation] = []
    for entry in diff.entries:
        mode = entry.dst_mode
        if mode in (MODE_REGULAR, MODE_ABSENT):
            continue
        name = MODE_NAMES.get(mode, f"unrecognised git mode {mode}")
        escape = mode in ESCAPE_MODES
        violations.append(
            ModeViolation(
                path=entry.path,
                mode=mode,
                reason=(
                    f"{entry.path}: {name} (mode {mode}) may not land — only a regular "
                    f"file ({MODE_REGULAR}) is permitted"
                    + (
                        "; a Zone A path pointing outside Zone A is an escape, not a proposal"
                        if escape
                        else ""
                    )
                ),
                security_event=escape,
            )
        )
    return tuple(violations)


def inspect_candidate(
    repo: GitRepo, base_ref: str, head_ref: str, policy: ZonePolicy | None = None
) -> CandidateVerdict:
    diff = read_candidate(repo, base_ref, head_ref)
    return CandidateVerdict(
        diff=diff,
        zones=enforce_zones(diff.paths, policy),
        mode_violations=check_modes(diff),
    )
