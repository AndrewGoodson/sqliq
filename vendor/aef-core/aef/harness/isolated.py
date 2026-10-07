"""Running a candidate's graph without letting it report on itself.

The parent builds a `Graph` whose node functions are PROXIES: each call
serialises `(node_id, state, context)` to a worker subprocess and reads back
`(delta, route)`. Everything else — applying the delta, resolving the route,
counting steps, recording the trace, classifying the outcome, scoring — runs
here, in a process the candidate's code never enters.

That is the whole idea. `GraphExecutor` is untouched and does not know the
difference; the only thing that moved is where `node.fn` runs (ADR 0094).

What a candidate can still do: return a lying `StateDelta`. That is the node
contract, and it is what the corpus, the MUST_FAIL tripwires and G2 exist to
judge. What it can no longer do: claim scenarios it never ran, fabricate the
aggregate report, or exit cleanly mid-run and have that read as success.
"""

from __future__ import annotations

import json
import select
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import uuid4

from aef.harness.container import ContainerRuntime, _force_remove, container_argv
from aef.harness.sandbox import SandboxPolicy, child_preexec, kill_process_group, scrubbed_env
from aef.harness.trace_codec import decode_route
from aef.kernel.contracts import Context, Edge, Node, Route, Services, SideEffect
from aef.kernel.graph import Graph
from aef.state import AEFState, StateDelta

WORKER_MODULE = "aef.harness.node_worker"

# The interpreter INSIDE the image. `sys.executable` is the parent's path and
# means nothing in a container — a venv path from the host resolves to nothing
# there, and the failure reads as a broken entrypoint rather than a wrong
# interpreter.
PYTHON_IN_CONTAINER = "python"


def _frame(value: Any) -> str:
    """One frame, one line. `trace_codec.dumps` pretty-prints for corpus
    files on disk; a newline-delimited protocol needs the opposite."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _unframe(text: str) -> Any:
    return json.loads(text)


class IsolationError(RuntimeError):
    """The worker could not be started, or stopped answering."""


@dataclass
class _Proxy:
    """Stands in for one agent-authored node function."""

    node_id: str
    session: NodeWorkerSession

    def __call__(
        self, state: AEFState, ctx: Context, services: Services
    ) -> tuple[StateDelta, Route]:
        return self.session.evaluate(self.node_id, state, ctx)


class NodeWorkerSession:
    """One worker subprocess, reused across every node of every scenario.

    Reused deliberately: a fresh process per node would make module-level
    agent state behave differently under the gate than in production, and the
    gate is supposed to re-execute what was recorded.
    """

    def __init__(
        self,
        entrypoint: str,
        *,
        workdir: Path,
        sandbox: SandboxPolicy,
        extra_env: dict[str, str] | None = None,
        step_timeout_s: float | None = None,
        container: ContainerRuntime | None = None,
        read_only_mounts: dict[str, str] | None = None,
    ) -> None:
        """The worker gets the SAME confinement a sandboxed command gets.

        ADR 0094 moved the candidate into this worker and left it a plain
        subprocess — no rlimits, no process group, an ad-hoc environment — so
        the defence against a *dishonest* candidate was bought by dropping the
        defence against a *runaway* one. Both now apply (ADR 0095).

        `step_timeout_s` bounds ONE node evaluation. `SandboxPolicy.timeout_s`
        bounds a whole command, and a worker is long-lived by design — a node
        that never returns would otherwise hang the parent forever, since the
        parent is the thing doing the waiting.
        """
        self._sandbox = sandbox
        self._timed_out = False
        self._closed = False
        self._step_timeout_s = step_timeout_s if step_timeout_s is not None else sandbox.timeout_s
        self._container = container
        env = scrubbed_env(sandbox)
        env.update(extra_env or {})

        if container is not None:
            # The worker runs INSIDE the container, so the node functions the
            # parent proxies to execute with no network and a read-only root.
            # This is what closes the shadow bypass: the policy engine denies
            # tool CALLS, and a node that simply opened a file was outside it
            # — demonstrated, not argued (trust case §2.1).
            #
            # The inverted control is unchanged and is the reason this is
            # worth doing at all: the parent still owns state, routing and the
            # step count, so a contained candidate cannot forge the final
            # state a shadow comparison reads (ADR 0094).
            argv = container_argv(
                [PYTHON_IN_CONTAINER, "-m", WORKER_MODULE, entrypoint],
                runtime=container,
                policy=sandbox,
                workdir=workdir,
                name=f"aef-worker-{uuid4().hex[:16]}",
                interactive=True,
                read_only_mounts=read_only_mounts,
            )
            self._container_name = argv[argv.index("--name") + 1]
            self._proc = subprocess.Popen(
                argv,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        else:
            self._container_name = ""
            self._proc = subprocess.Popen(
                [sys.executable, "-m", WORKER_MODULE, entrypoint],
                cwd=str(workdir),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                preexec_fn=child_preexec(sandbox),
            )
        try:
            self.description = self._handshake(entrypoint)
        except BaseException:
            # Popen succeeded, so this object owns the process even when an
            # unexpected handshake shape raises outside a specific validation
            # branch.  Never return control with the worker still alive.
            self.close()
            raise

    def _handshake(self, entrypoint: str) -> dict[str, Any]:
        line = self._readline()
        if line is None:
            self.close()
            raise IsolationError(f"worker for {entrypoint!r} exited before describing its graph")
        try:
            payload = _unframe(line)
        except Exception as exc:  # noqa: BLE001 - candidate output on the same stream
            self.close()
            raise IsolationError(
                f"worker for {entrypoint!r} did not describe its graph; the first line was "
                f"{line[:200]!r}. Agent code printing to stdout before the graph is built "
                f"lands here — it cannot forge a result, but it can make the worker "
                f"unusable."
            ) from exc
        if not isinstance(payload, dict) or "graph" not in payload:
            self.close()
            detail = payload.get("error") if isinstance(payload, dict) else None
            why = detail or f"unexpected first frame {line[:200]!r}"
            raise IsolationError(f"worker for {entrypoint!r} failed: {why}")
        graph = payload["graph"]
        if not isinstance(graph, dict):
            self.close()
            raise IsolationError(
                f"worker for {entrypoint!r} described a non-object graph ({type(graph).__name__})"
            )
        for required in ("id", "version", "entry_node", "nodes", "edges"):
            if required not in graph:
                self.close()
                raise IsolationError(
                    f"worker for {entrypoint!r} described a graph with no {required!r}"
                )
        return graph

    def _readline(self) -> str | None:
        """Read one frame, or give up at the deadline.

        `readline()` on a pipe blocks forever. The parent is what waits, so a
        node that never returns hangs the GATE — the one process that must
        not be stoppable by the candidate (ADR 0095). `select` bounds it, and
        a timeout kills the worker's whole process group so descendants go
        too.
        """
        assert self._proc.stdout is not None
        ready, _, _ = select.select([self._proc.stdout], [], [], self._step_timeout_s)
        if not ready:
            kill_process_group(self._proc.pid)
            self._timed_out = True
            return None
        line = self._proc.stdout.readline()
        return line.strip() or None

    def configure(self, settings: dict[str, Any]) -> None:
        """Hand the worker per-scenario settings the parent owns.

        Today that is the scenario's model cassette (ADR 0123): the recorded
        calls, the miss policy, and — only when the owner opted into
        `cassette_miss="live"` — which provider to build for misses. The
        worker still never learns what a scenario is; it learns what the
        model said last time, which its own node could have hard-coded
        anyway. The parent keeps everything that judges.
        """
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(_frame({"configure": settings}) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise IsolationError(f"worker died before it could be configured: {exc}") from exc
        line = self._readline()
        if line is None:
            raise IsolationError("worker produced no reply to configure; it exited or was killed")
        response = _unframe(line)
        if not isinstance(response, dict) or response.get("configured") is not True:
            detail = response.get("error") if isinstance(response, dict) else response
            raise IsolationError(f"worker refused configuration: {detail}")

    def evaluate(self, node_id: str, state: AEFState, ctx: Context) -> tuple[StateDelta, Route]:
        assert self._proc.stdin is not None
        request = {
            "node_id": node_id,
            "state": _unframe(state.model_dump_json()),
            "context": {
                "run_id": ctx.run_id,
                "graph_version": ctx.graph_version,
                "trace_id": ctx.trace_id,
                "node_id": ctx.node_id,
                "now": ctx.now.isoformat(),
                "idempotency_key": ctx.idempotency_key,
                "attempt": ctx.attempt,
            },
        }
        try:
            self._proc.stdin.write(_frame(request) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise IsolationError(f"worker died before node {node_id!r} could run: {exc}") from exc

        line = self._readline()
        if line is None:
            why = (
                f"did not answer within {self._step_timeout_s:g}s and was killed"
                if self._timed_out
                else "exited or was killed mid-step"
            )
            raise IsolationError(
                f"worker produced no result for node {node_id!r} — it {why}. "
                f"A run that stopped is not a run that passed."
            )
        response = _unframe(line)
        if "error" in response:
            # Re-raised so `GraphExecutor` handles it exactly as it handles a
            # node raising in-process: fallback route if declared, error entry
            # otherwise. The parent's semantics do not change because the node
            # happens to live elsewhere.
            raise NodeEvaluationError(response["error"])
        delta = StateDelta.model_validate(response["delta"])
        return delta, decode_route(response["route"])

    @property
    def returncode(self) -> int | None:
        """`None` while the worker is alive. A candidate can end its own
        process; the parent needs to notice rather than keep asking."""
        return self._proc.poll()

    @property
    def timed_out(self) -> bool:
        """A node exceeded the per-step deadline. Distinct from the worker
        dying on its own, because the operator needs to tell them apart."""
        return self._timed_out

    @property
    def is_contained(self) -> bool:
        """Whether this worker runs inside a container.

        A `NodeWorkerSession` with `container=None` is a plain subprocess: real
        confinement (rlimits, process group, scrubbed env) and NO filesystem or
        network boundary. Callers that need the boundary must ask for THIS, not
        for "is there a session" — the adversarial round for ADR 0105 found a
        runner claiming containment because any truthy object had been passed.
        """
        return self._container is not None

    @property
    def closed(self) -> bool:
        """Set by `close()`, not inferred from the process.

        `poll()` returns None until the killed process is reaped, so a session
        closed microseconds ago still read as open — and the caller's next
        node evaluation failed as a dead worker and was recorded as CANDIDATE
        behaviour. An explicit flag rather than a symptom, which is what
        ADR 0064 measured about inferring a marker.
        """
        return self._closed

    def close(self) -> None:
        self._closed = True
        if self._proc.poll() is None:
            if self._container is not None:
                # Ask the DAEMON. Killing the `docker run` client leaves the
                # container running — Milestone 4 reproduced exactly that, one
                # still alive two seconds after the gate reported a timeout,
                # and it is ADR 0093's defect in a third place. The daemon owns
                # the container's lifetime, so nothing else ends it.
                _force_remove(self._container.binary, self._container_name)
            else:
                # The GROUP, not just the child: `child_preexec` gave it its
                # own session precisely so descendants can be reached
                # (ADR 0093).
                kill_process_group(self._proc.pid)
            self._proc.kill()
            # Killing is not reaping.  Repeated variant evaluation otherwise
            # accumulates zombie children until some later Popen happens to
            # run subprocess's global cleanup.
            self._proc.wait()
        for stream in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except (BrokenPipeError, OSError, ValueError):
                # The worker is gone — which is one of the things a candidate
                # can do to itself. Cleanup must not turn that into an
                # exception the caller has to handle twice (ADR 0094).
                pass

    def __enter__(self) -> NodeWorkerSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class NodeEvaluationError(RuntimeError):
    """The node raised in the worker. Surfaced here so the parent's executor
    treats it identically to an in-process raise."""


def _parent_side_key(node_id: str) -> Callable[[AEFState], str]:
    """A stand-in idempotency key.

    `Node` requires one for any impure node (ADR 0010). The candidate's real
    key function lives in the worker; the parent needs *a* callable for the
    invariant to hold, and this key is only consumed by HITL dedup, which the
    parent owns.
    """

    def key(state: AEFState) -> str:
        return f"{node_id}:{state.run_id}:{state.checkpoint_seq}"

    return key


def graph_from(session: NodeWorkerSession) -> Graph:
    """Rebuild the candidate's graph in the PARENT, with proxied functions.

    The shape is the candidate's declaration — the same declaration G0 and G4
    read from the base ref. What changes is who acts on it.
    """
    description = session.description
    nodes: dict[str, Node] = {}
    for spec in description["nodes"]:
        side_effects = SideEffect(spec["side_effects"])
        nodes[spec["id"]] = Node(
            id=spec["id"],
            version=spec["version"],
            fn=_Proxy(node_id=spec["id"], session=session),
            deterministic=bool(spec["deterministic"]),
            side_effects=side_effects,
            # The contract requires a key fn for any impure node. The real one
            # lives in the worker; the parent needs *a* callable so `Node`'s
            # own invariant holds, and the key is only used for HITL dedup.
            idempotency_key_fn=(
                _parent_side_key(spec["id"]) if side_effects is not SideEffect.PURE else None
            ),
            telemetry_tags=tuple(spec.get("telemetry_tags", ())),
            fallback_node_id=spec.get("fallback_node_id"),
        )

    edges = [
        Edge(
            from_node=spec["from_node"],
            to_node=(
                spec["to_node"] if isinstance(spec["to_node"], str) else tuple(spec["to_node"])
            ),
            priority=int(spec.get("priority", 0)),
            requires_human_approval=bool(spec.get("requires_human_approval", False)),
            requires_deterministic_fallback=bool(
                spec.get("requires_deterministic_fallback", False)
            ),
        )
        for spec in description["edges"]
    ]

    return Graph(
        id=description["id"],
        version=description["version"],
        nodes=nodes,
        edges=edges,
        entry_node=description["entry_node"],
    )
