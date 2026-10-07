"""Schema migration registry — keeps old checkpoints loadable across
`AEFState` schema changes (constraint #4).

A migration is a pure function `dict -> dict` keyed by the `schema_version`
it migrates *from*. `load_state` walks the chain from a checkpoint's
recorded version up to `CURRENT_SCHEMA_VERSION` before validating.

Every future state-schema change MUST register a migration here, and the
CI graph-IR schema-compat check (`.github/workflows/ci.yml`) fails the build
if a fixture checkpoint from a prior version can no longer be loaded.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from aef.state.schema import CURRENT_SCHEMA_VERSION, AEFState

MigrationFn = Callable[[dict[str, Any]], dict[str, Any]]


class MigrationCycleError(RuntimeError):
    pass


class NoMigrationPathError(RuntimeError):
    pass


class MigrationRegistry:
    def __init__(self) -> None:
        self._migrations: dict[str, MigrationFn] = {}

    def register(self, from_version: str, fn: MigrationFn) -> None:
        if from_version in self._migrations:
            raise ValueError(f"migration from schema_version={from_version!r} already registered")
        self._migrations[from_version] = fn

    def migrate(
        self, raw: Mapping[str, Any], target_version: str = CURRENT_SCHEMA_VERSION
    ) -> dict[str, Any]:
        data: dict[str, Any] = dict(raw)
        version = data.get("schema_version", "0.0.0")
        seen: set[str] = set()
        while version != target_version:
            if version in seen:
                raise MigrationCycleError(f"migration cycle detected at schema_version={version!r}")
            seen.add(version)
            fn = self._migrations.get(version)
            if fn is None:
                raise NoMigrationPathError(
                    f"no migration registered from schema_version={version!r} "
                    f"toward target={target_version!r}"
                )
            data = fn(data)
            new_version = data.get("schema_version", version)
            if new_version == version:
                raise MigrationCycleError(
                    f"migration from schema_version={version!r} did not advance the version"
                )
            version = new_version
        return data


DEFAULT_MIGRATIONS = MigrationRegistry()


def load_state(
    raw: Mapping[str, Any], registry: MigrationRegistry = DEFAULT_MIGRATIONS
) -> AEFState:
    """Load a possibly-old checkpoint dict into the current `AEFState` shape."""
    migrated = registry.migrate(raw, CURRENT_SCHEMA_VERSION)
    return AEFState.model_validate(migrated)
