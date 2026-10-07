"""Container-backed containment — the first thing here that can report
`network_isolated=True` truthfully.

`sandbox.py` states plainly what a single process cannot enforce: it cannot
revoke its own network access, and `cwd` is a default rather than a jail.
Everything downstream has assumed a containment that did not exist — G0's
import allowlist is a static scan standing in for a boundary.

This module is an **option, not a replacement** (Milestone 4a). Nothing is
weakened because it exists: rlimits, env scrubbing, the timeout and the whole
static-scan pipeline all still apply, and a candidate rejected by G0 still
never runs. Defence in depth is why the zone rule survived three defeats of
the import allowlist (ADR 0085 -> 0088 -> 0093).

**Isolation is VERIFIED, not declared.** `SandboxPolicy.network_isolation_
attested` is a bool a caller passes in; honest, and unchecked. A runtime here
earns `network_isolated=True` by actually running a probe that tries to open a
socket and confirming it fails — and the probe is itself validated against the
negative case, because "the connection failed" is what a broken image looks
like too. Both directions were measured before this module was written:

    docker run --rm --network none  ... -> network blocked: OSError
    docker run --rm                 ... -> network reachable

**Degrading is loud.** No runtime, no image, or a probe that does not behave
in both directions raises `SandboxUnavailableError`. A sandbox that silently
provides less than it claims is worse than none, because the claim is what
gets trusted.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from aef.harness.sandbox import (
    SandboxCapabilities,
    SandboxPolicy,
    SandboxResult,
    SandboxUnavailableError,
    probe_rlimits,
)

# In preference order. Both take the same flags for everything used here.
SUPPORTED_RUNTIMES: tuple[str, ...] = ("docker", "podman")

# Where the workspace is mounted inside the container. Fixed rather than
# derived from the host path: the host path appears in error messages the
# candidate can read, and it is one more thing that differs between the local
# and CI runs for no reason.
WORKDIR_MOUNT = "/aef-workspace"

# The probe. Written as one string so the exact text can be asserted in a
# test — a paraphrased probe is how a detector passes its own check and is
# still wrong (three times in this program).
NETWORK_PROBE = (
    "import socket,sys\n"
    "try:\n"
    "    socket.create_connection(('1.1.1.1', 53), timeout=5)\n"
    "    sys.exit(0)\n"
    "except OSError:\n"
    "    sys.exit(42)\n"
)
PROBE_BLOCKED_CODE = 42
# Docker and podman both exit 125 when the RUNTIME itself failed to start the
# container (missing image, bad flag) rather than when the container's command
# failed. Kept apart from a probe verdict so the two cannot be confused.
RUNTIME_COULD_NOT_RUN = 125

DEFAULT_PROBE_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class ContainerRuntime:
    """A container runtime whose isolation has been MEASURED.

    Construct via `detect_container_runtime`; the constructor is not the
    place to make a claim this dataclass cannot support. `verified` is set
    only by a probe that ran.
    """

    binary: str
    image: str
    verified: bool = False

    def __post_init__(self) -> None:
        if not self.binary or not self.image:
            raise SandboxUnavailableError("a container runtime needs both a binary and an image")


def _run(argv: Sequence[str], *, timeout_s: float) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(list(argv), capture_output=True, timeout=timeout_s, check=False)
    except FileNotFoundError as exc:
        raise SandboxUnavailableError(f"{argv[0]!r} is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxUnavailableError(f"{' '.join(argv[:3])} timed out after {timeout_s}s") from exc


def _force_remove(binary: str, name: str) -> None:
    """Kill and remove a container by name, best effort.

    Called on timeout. `subprocess.run(timeout=...)` kills the `docker run`
    CLIENT, and the container it started keeps running: reproduced, one
    container still alive two seconds after the gate reported a timeout. The
    daemon owns the container's lifetime, so the only thing that ends it is
    asking the daemon.

    Failures are swallowed deliberately: the container may already be gone,
    and a cleanup error must not replace the timeout the caller needs to hear
    about.
    """
    try:
        subprocess.run(
            [binary, "rm", "--force", name], capture_output=True, timeout=30.0, check=False
        )
    except (OSError, subprocess.SubprocessError):
        pass


def available_runtimes() -> tuple[str, ...]:
    """Runtime binaries present AND with a responsive daemon.

    `shutil.which` alone is not enough: Docker Desktop leaves the client on
    PATH when the daemon is stopped, so `which docker` succeeds and every
    subsequent command fails with a connection error. Checked by asking the
    daemon a question rather than by looking for the file.
    """
    found: list[str] = []
    for binary in SUPPORTED_RUNTIMES:
        if shutil.which(binary) is None:
            continue
        try:
            probe = _run([binary, "info", "--format", "{{.ServerVersion}}"], timeout_s=30.0)
        except SandboxUnavailableError:
            continue
        if probe.returncode == 0:
            found.append(binary)
    return tuple(found)


def verify_network_isolation(binary: str, image: str, *, timeout_s: float | None = None) -> None:
    """Prove this runtime blocks egress, or raise saying why not.

    **Both directions.** The isolated run must fail to connect AND the
    unisolated run must succeed. Checking only the first would accept an
    image with no Python, a broken DNS setup, or a host with no network at
    all — every one of which looks exactly like working isolation from
    inside a single assertion. This is the negative control the
    reproduce-first method requires, and it was run by hand before this
    function existed.
    """
    timeout_s = timeout_s if timeout_s is not None else DEFAULT_PROBE_TIMEOUT_S

    isolated = _run(
        [binary, "run", "--rm", "--network", "none", image, "python", "-c", NETWORK_PROBE],
        timeout_s=timeout_s,
    )
    if isolated.returncode == RUNTIME_COULD_NOT_RUN:
        # Distinguished, because a refusal that misnames its own cause sends
        # the operator to fix the wrong thing (ADR 0074). Exit 125 is the
        # runtime saying it never started the container at all — a missing
        # image, a bad flag — which is not a statement about isolation.
        raise SandboxUnavailableError(
            f"{binary} could not run image {image!r} (exit {RUNTIME_COULD_NOT_RUN}); the "
            f"container never started, so nothing was measured about isolation. "
            f"stderr: {isolated.stderr.decode('utf-8', 'replace')[:300]}"
        )
    if isolated.returncode != PROBE_BLOCKED_CODE:
        raise SandboxUnavailableError(
            f"{binary} did not block egress with --network none: the probe exited "
            f"{isolated.returncode}, expected {PROBE_BLOCKED_CODE}. "
            f"stderr: {isolated.stderr.decode('utf-8', 'replace')[:300]}"
        )

    control = _run(
        [binary, "run", "--rm", image, "python", "-c", NETWORK_PROBE],
        timeout_s=timeout_s,
    )
    if control.returncode != 0:
        raise SandboxUnavailableError(
            f"the negative control failed: without --network none the probe still could not "
            f"connect (exit {control.returncode}). The probe therefore proves nothing about "
            f"isolation — this host, image or DNS setup cannot reach the network either way. "
            f"stderr: {control.stderr.decode('utf-8', 'replace')[:300]}"
        )


def detect_container_runtime(
    image: str, *, binary: str | None = None, verify: bool = True
) -> ContainerRuntime:
    """The runtime to use, with its isolation measured. Raises if there is none.

    Raising rather than returning `None` is the point of 4a's "degrading
    loudly": a caller that asked for containment and got a quiet `None` would
    run the candidate anyway.
    """
    candidates: tuple[str, ...] = (binary,) if binary else available_runtimes()
    if not candidates:
        raise SandboxUnavailableError(
            f"no container runtime available (looked for {', '.join(SUPPORTED_RUNTIMES)} with a "
            f"responsive daemon). Containment cannot be provided here; run with "
            f"NetworkPolicy.ACKNOWLEDGED_UNISOLATED and accept that the result records "
            f"network_isolated=False."
        )

    chosen = candidates[0]
    if verify:
        verify_network_isolation(chosen, image)
    return ContainerRuntime(binary=chosen, image=image, verified=verify)


def container_argv(
    argv: Sequence[str],
    *,
    runtime: ContainerRuntime,
    policy: SandboxPolicy,
    workdir: Path,
    name: str | None = None,
    interactive: bool = False,
    read_only_mounts: Mapping[str, str] | None = None,
) -> list[str]:
    """The full command line, built in one place so it can be asserted on.

    Every flag here is a control, and each is named in the test that pins it:

    - `--network none` is the whole point.
    - `--rm` so a candidate cannot leave state behind for the next one.
    - `--read-only` plus a writable mount of the workspace: filesystem
      confinement that `cwd` never provided.
    - `--cap-drop ALL` and `--security-opt no-new-privileges` because a
      candidate that can raise its own privileges inside the container has
      undone the boundary.
    - `--memory` / `--pids-limit` from the same policy fields the in-process
      path uses, so the two paths bound the same things.
    - `--env` is NOT passed through: the container starts from the image's
      environment, which is a stronger version of the env scrubbing the
      in-process path does by allowlist.

    `interactive` keeps stdin open, which a long-lived worker driven over a
    line protocol needs (`NodeWorkerSession`). It grants the container
    nothing — the pipe already existed for the in-process worker.

    `read_only_mounts` are exactly that: every one is appended `:ro`, asserted
    rather than trusted, so "the workspace is the only WRITABLE mount" stays
    true while an operator can still supply an image's dependencies from the
    host.
    """
    command = [
        runtime.binary,
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--workdir",
        WORKDIR_MOUNT,
        "--volume",
        f"{workdir.resolve()}:{WORKDIR_MOUNT}",
    ]
    if interactive:
        command.append("--interactive")
    for source, target in sorted((read_only_mounts or {}).items()):
        mount = f"{Path(source).resolve()}:{target}:ro"
        if not mount.endswith(":ro"):  # pragma: no cover - constructed above
            raise SandboxUnavailableError(f"read-only mount {mount} is not read-only")
        command += ["--volume", mount]
    if name:
        # A handle for the timeout path. Without one there is nothing to kill
        # but the client, and killing the client leaves the container
        # RUNNING — reproduced in this milestone's adversarial round, and the
        # same defect ADR 0093 found in the in-process path: the direct child
        # dies and the real work survives it.
        command += ["--name", name]
    if policy.max_address_space_bytes:
        command += ["--memory", str(policy.max_address_space_bytes)]
    if policy.max_processes:
        command += ["--pids-limit", str(policy.max_processes)]
    for key, value in sorted(policy.extra_env.items()):
        command += ["--env", f"{key}={value}"]
    command.append(runtime.image)
    command.extend(argv)
    return command


def run_containerized(
    argv: Sequence[str],
    *,
    workdir: Path,
    runtime: ContainerRuntime,
    policy: SandboxPolicy | None = None,
    name: str | None = None,
) -> SandboxResult:
    """Run `argv` inside a container and report what was ACTUALLY enforced.

    `network_isolated` and `filesystem_isolated` are `runtime.verified` and
    not `True` — an unverified runtime still runs, and still says so. The
    capability report is what a gate outcome is read against, and a report
    that overstates is the failure this module exists to end.
    """
    from aef.harness.sandbox import NetworkPolicy

    policy = policy or SandboxPolicy(network=NetworkPolicy.ACKNOWLEDGED_UNISOLATED)
    workdir = workdir.resolve()
    if not workdir.is_dir():
        raise SandboxUnavailableError(f"sandbox workdir {workdir} does not exist")

    # A caller may name its container so it can ask the daemon about exactly
    # that one afterwards (the leak test does); the default stays unique.
    name = name or f"aef-gate-{uuid4().hex[:16]}"
    command = container_argv(argv, runtime=runtime, policy=policy, workdir=workdir, name=name)

    started = time.monotonic()
    timed_out = False
    try:
        completed = _run(command, timeout_s=policy.timeout_s)
        returncode, out, err = completed.returncode, completed.stdout, completed.stderr
    except SandboxUnavailableError as exc:
        # A timeout reaches here as an unavailability, because `_run` cannot
        # tell "the image is missing" from "the work took too long". Reported
        # as a timeout when the message says so, so a gate does not read a
        # slow candidate as a broken harness.
        if "timed out" not in str(exc):
            raise
        _force_remove(runtime.binary, name)
        timed_out = True
        returncode = -1
        out, err = b"", str(exc).encode()

    return SandboxResult(
        returncode=returncode,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        duration_s=time.monotonic() - started,
        capabilities=SandboxCapabilities(
            env_scrubbed=True,
            cwd_confined=True,
            timeout_enforced=True,
            rlimits_applied=probe_rlimits(policy),
            # MEASURED, not declared. False when nobody ran the probe.
            network_isolated=runtime.verified,
            filesystem_isolated=runtime.verified,
        ),
    )
