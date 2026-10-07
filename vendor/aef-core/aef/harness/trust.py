"""Explicit base-ref reads for Zone B harness content.

The system-level trust property is that the *executing AEF package* comes from
a trusted checkout or installation while the candidate is fetched as data.
The supplied CI workflows satisfy that property by checking out ``main`` and
installing AEF before fetching the candidate ref. Local callers must establish
the same provenance themselves; a library cannot attest where its own imported
code came from.

``BaseRefHarness`` is a narrower primitive. It pins a base commit and can read
or materialise Zone B files from that commit. The loop driver does not route
all gate, corpus, suite, or workflow reads through this class. ADR 0047 records
the distinction and corrects its original, broader claim.

The zone check (``zones.py``) still rejects Zone B diffs as a fail-closed
signal. Candidate workspaces are reconstructed from Git objects rather than
copied from the candidate working tree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from aef.harness.git import GitError, GitRepo
from aef.harness.zones import Zone, classify_path


class TrustBoundaryError(RuntimeError):
    """Raised when something tries to read non-harness code through the
    harness API, or to materialise outside the scratch directory."""


def _prepare_empty_destination(dest: Path) -> Path:
    """Resolve a caller-owned scratch directory without following its leaf."""
    if dest.is_symlink():
        raise TrustBoundaryError(f"scratch destination {dest} may not be a symlink")
    resolved = dest.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    if next(resolved.iterdir(), None) is not None:
        raise TrustBoundaryError(f"scratch destination {resolved} must be empty")
    return resolved


@dataclass(frozen=True)
class BaseRefHarness:
    """Reads harness content as of `base_ref`, and refuses anything else."""

    repo: GitRepo
    base_ref: str
    _base_sha: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_base_sha", self.repo.rev_parse(self.base_ref))

    @property
    def base_sha(self) -> str:
        """Pinned once so a concurrent push to the base branch cannot swap
        the harness mid-run."""
        return self._base_sha

    def read(self, path: str) -> str:
        """Contents of a **Zone B** file as of the base ref.

        Refuses any other zone: this API exists to load the judge, and
        loading candidate code through it would defeat the entire boundary.
        Read the candidate with `candidate.py` instead — deliberately a
        different module with a different name.
        """
        zone = classify_path(path)
        if zone is not Zone.B:
            raise TrustBoundaryError(
                f"refusing to read {path!r} through the trust boundary: it is Zone "
                f"{zone.value}, and this API loads harness code (Zone B) only. Candidate "
                f"content is read via aef.harness.candidate, never executed as a gate."
            )
        return self.repo.show(self.base_sha, path)

    def harness_paths(self) -> tuple[str, ...]:
        """Every Zone B path present at the base ref."""
        listing = self.repo.run_bytes("ls-tree", "-r", "--name-only", "-z", self.base_sha)
        names = listing.decode("utf-8", errors="replace").split("\0")
        return tuple(sorted(n for n in names if n and classify_path(n) is Zone.B))

    def materialize(self, dest: Path) -> Path:
        """Write the base ref's harness tree into `dest` and return it.

        Callers that explicitly need a base-ref Zone B tree can run from this
        directory. Only Zone B lands here — verified per path rather than
        assumed from the export command, so a mis-specified path filter cannot
        quietly widen what gets trusted.
        """
        dest = _prepare_empty_destination(dest)
        for path in self.harness_paths():
            target = (dest / path).resolve()
            if not target.is_relative_to(dest):  # pragma: no cover - git cannot emit this
                raise TrustBoundaryError(f"refusing to materialise {path!r} outside {dest}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.repo.show(self.base_sha, path))
        return dest

    def verify_base_is_ancestor(self, head_ref: str) -> None:
        """Assert the candidate and base share history.

        Despite this method's historical name, the base need not be an
        ancestor: candidate diffs intentionally start at the merge base. A ref
        with no merge base is not a candidate against this incumbent.
        """
        try:
            merge_base = self.repo.merge_base(self.base_ref, head_ref)
        except GitError as exc:
            raise TrustBoundaryError(
                f"{head_ref!r} shares no history with base ref {self.base_ref!r}; it is not "
                f"a candidate against this incumbent"
            ) from exc
        if not merge_base:  # pragma: no cover - git errors instead
            raise TrustBoundaryError(f"no merge base between {self.base_ref!r} and {head_ref!r}")
