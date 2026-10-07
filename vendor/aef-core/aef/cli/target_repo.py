"""Target-only adoption and mechanical migration for the /target-repo skill.

Invoke through scripts/target_repo.py: it disables bytecode writes BEFORE
importing this package and excludes the target's code from the import path.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

from aef.cli.adopt import run_adopt
from aef.cli.migrate import DEFAULT_MIGRATED_OUT, report, run_migrate


class TargetRefused(ValueError):
    """The selected tree or output path violates the target-only boundary."""


def _git_common_dir(root: Path) -> Path | None:
    """Read Git metadata without invoking Git, hooks, or changing its index."""
    git_dir = root / ".git"
    if git_dir.is_file():
        marker = git_dir.read_text(encoding="utf-8").strip()
        if not marker.startswith("gitdir: "):
            raise TargetRefused(f"refusing malformed Git metadata: {git_dir}")
        git_dir = (root / marker.removeprefix("gitdir: ")).resolve()
    if not git_dir.is_dir():
        return None
    common = git_dir / "commondir"
    if common.is_file():
        return (git_dir / common.read_text(encoding="utf-8").strip()).resolve()
    return git_dir.resolve()


def validate_target(target: Path, source: Path) -> Path:
    if not target.is_absolute():
        raise TargetRefused("refusing relative target: provide an explicit absolute directory")
    root = target.resolve(strict=True)
    source = source.resolve(strict=True)
    if not root.is_dir():
        raise TargetRefused("refusing target: expected an existing directory")
    if root == source or root in source.parents or source in root.parents:
        raise TargetRefused("refusing target: source and target directories overlap")
    source_git = _git_common_dir(source)
    target_git = _git_common_dir(root)
    if target_git is not None and (
        target_git == source_git or target_git == source or source in target_git.parents
    ):
        raise TargetRefused("refusing target: it shares Git metadata with the source repo")
    return root


def guard_output(root: Path, path: Path) -> None:
    """Reject links and nonregular entries at every component of a write.

    Adoption preserves linked owner files without requesting a write; those
    safe skips need no refusal. Every actual write must pass this guard, and
    migration destinations are also checked before adoption starts.
    This protects the generator's writes in a stationary working tree. It is
    not an OS sandbox against a process concurrently swapping path components.
    """
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise TargetRefused(f"refusing output outside target: {path}") from exc
    if ".." in relative.parts or not relative.parts:
        raise TargetRefused(f"refusing invalid output path: {path}")
    current = root
    # Recheck the root too: it may have been replaced since target validation.
    for part in ("", *relative.parts):
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise TargetRefused(f"refusing symlink in output path: {current}")
        if current != path:
            if not stat.S_ISDIR(metadata.st_mode):
                raise TargetRefused(f"refusing nondirectory output parent: {current}")
        elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
            raise TargetRefused(f"refusing nonregular or hard-linked output: {current}")
    if not path.resolve().is_relative_to(root):
        raise TargetRefused(f"refusing resolved output outside target: {path}")


def main(target_argument: str, *, profile: str = "offline", with_workflows: bool = False) -> int:
    try:
        source = Path(__file__).resolve().parents[2]
        root = validate_target(Path(target_argument).expanduser(), source)

        def guard(path: Path) -> None:
            guard_output(root, path)

        # Discover and validate graph destinations before adoption writes. In
        # particular, an agents/ symlink must not turn migration into a write
        # to the source or another repository.
        preview = run_migrate(root, write=False)
        for destination in (DEFAULT_MIGRATED_OUT, *(p.out_relative for p in preview.prompt_agents)):
            guard(root / destination)
        adopted = run_adopt(root, write_guard=guard, profile=profile, with_workflows=with_workflows)
        migrated = run_migrate(root, write_guard=guard)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"target-repo: {exc}", file=sys.stderr)
        print(
            "No source writes requested. Inspect the target for any earlier outputs.",
            file=sys.stderr,
        )
        return 1

    print(f"Target: {root}")
    print(f"Source (read-only): {source}")
    for path in adopted.written_files:
        print(f"created: {path.relative_to(root)}")
    for path in adopted.appended_files:
        print(f"updated AEF block: {path.relative_to(root)}")
    for path in adopted.skipped_files:
        reason = adopted.skip_reasons.get(path, "already exists")
        print(f"preserved: {path.relative_to(root)} ({reason})")
    print(report(migrated))
    print(
        "Mechanical integration complete; runtime behavior and learning quality are not verified."
    )
    print(f"Continue semantic integration inside {root}; follow AEF_MIGRATION_CHECKLIST.md.")
    return 0
