"""`aef trace` — print the provenance trail for a checkpointed run.

`AEFState.provenance` is itself the execution trace (report §5: node_id,
graph_version, model, ts, trace_id, token_cost per node execution) — there
is no separate trace store to query yet, so this reads it straight off the
latest checkpoint.
"""

from __future__ import annotations

from pathlib import Path

from aef.kernel import FileDurabilityBackend
from aef.state import Provenance


def trace_run(checkpoints_dir: Path, run_id: str) -> list[Provenance]:
    backend = FileDurabilityBackend(checkpoints_dir)
    state = backend.load_latest(run_id)
    if state is None:
        raise ValueError(f"no checkpoints found for run_id={run_id!r} under {checkpoints_dir}")
    return state.provenance
