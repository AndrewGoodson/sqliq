"""Building the tree a gate actually runs against.

A candidate cannot simply be checked out and tested: that would run the
branch's harness, which is the thing ADR 0047 exists to prevent. Nor can the
base ref be tested alone — it does not contain the change.

The workspace is the **post-merge state**: every file as of the base ref,
with the candidate's **Zone A files only** overlaid. That is exactly what the
repository would look like after this candidate merged, because a candidate
that could alter anything outside Zone A would have been rejected before
reaching here.

Constructing it this way rather than by `git checkout <branch>` means the
trust boundary holds even if a zone check upstream has a bug: the overlay
loop refuses any path it cannot classify Zone A, so a Zone B or C file has no
route into the tree gates run against.
"""

from __future__ import annotations

from pathlib import Path

from aef.harness.candidate import CandidateDiff
from aef.harness.git import GitRepo
from aef.harness.trust import TrustBoundaryError, _prepare_empty_destination
from aef.harness.zones import Zone, ZonePolicy, classify_path


def build_candidate_workspace(
    repo: GitRepo,
    diff: CandidateDiff,
    dest: Path,
    policy: ZonePolicy | None = None,
) -> Path:
    """Materialise base-ref tree + the candidate's Zone A overlay into `dest`."""
    dest = _prepare_empty_destination(dest)

    _materialise_tree(repo, diff.base_sha, dest)

    for entry in diff.entries:
        zone = classify_path(entry.path, policy)
        if zone is not Zone.A:
            # Defence in depth: G0 should already have rejected this
            # candidate. If it did not, the file still never lands.
            raise TrustBoundaryError(
                f"refusing to overlay {entry.path!r} into the candidate workspace: it is "
                f"Zone {zone.value}, and only Zone A may differ from the base ref"
            )
        target = (dest / entry.path).resolve()
        if not target.is_relative_to(dest):  # pragma: no cover - zones reject traversal first
            raise TrustBoundaryError(f"refusing to write {entry.path!r} outside {dest}")

        if entry.is_deletion:
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(repo.run_bytes("show", f"{diff.head_sha}:{entry.path}"))
        target.chmod(0o644)

    return dest


def _materialise_tree(repo: GitRepo, sha: str, dest: Path) -> None:
    """Write every blob at `sha` into `dest`, enumerated from the tree.

    Deliberately NOT `git archive`. Archive honours `export-ignore` in
    `.gitattributes`, so a repo that excludes `tests/` from its sdist — an
    ordinary, sensible thing to do — would get a workspace with its test
    suite missing, and G1 would run a suite that no longer contains the test
    that fails. Confirmed by building such a repo, not inferred: with
    `tests/ export-ignore` at the base ref, the materialised workspace had no
    tests at all and reported nothing wrong.

    `ls-tree` reports the tree as committed and ignores export attributes, so
    what lands is what the base ref actually contains.
    """
    listing = repo.run_bytes("ls-tree", "-r", "-z", sha)
    dest_resolved = dest.resolve()
    for record in listing.decode("utf-8", errors="replace").split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        parts = meta.split()
        if len(parts) < 3:  # pragma: no cover - git does not emit this
            continue
        mode = parts[0]
        # Only regular files land. Symlinks (120000) and gitlinks (160000)
        # are refused for the same reason candidate.py refuses them.
        if mode not in ("100644", "100755"):
            continue
        target = (dest / path).resolve()
        if not target.is_relative_to(dest_resolved):  # pragma: no cover - git paths are relative
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(repo.run_bytes("show", f"{sha}:{path}"))
        target.chmod(0o755 if mode == "100755" else 0o644)
