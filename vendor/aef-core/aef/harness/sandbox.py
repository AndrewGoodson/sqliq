"""Sandboxed execution of agent-authored code.

With self-coding, the harness runs code the agent wrote during validation
(04 §2.5: "a precondition, not a nicety"). This module provides what a
single process genuinely *can* enforce, and — this is the important part —
**declares what it cannot**.

Enforced here, really:
  - environment scrubbed to an explicit allowlist (no credentials inherited)
  - working directory confined to a caller-supplied scratch dir
  - wall-clock timeout, with the whole process group killed
  - POSIX resource limits (address space, file size, processes, core dumps)

**Not** enforced here, and not pretended:
  - **network egress.** Blocking it requires a network namespace, a
    firewall, or a container. A process cannot revoke its own connectivity.
  - **filesystem confinement.** `cwd` is a default, not a jail; nothing
    stops an absolute path.
  - **an exact child environment.** The OS loader injects variables of its
    own after `execve` (macOS adds `__CF_USER_TEXT_ENCODING` to every
    process — verified, not assumed). `env_scrubbed` therefore means
    "nothing is inherited from the parent", which is the property that
    actually protects credentials, and not "the child's environment equals
    the allowlist".

So the default policy **refuses to run** unless isolation has been attested
by the layer that can actually provide it (the CI container). Running
unisolated requires saying so explicitly, and it is recorded on the result.
That is deliberately inconvenient: a sandbox that silently provides less
than it claims is worse than none, because the claim is what gets trusted.

Attestation is passed in, not read from the environment, so it is testable
and so the value comes from Zone B configuration rather than from anything
a candidate can set.
"""

from __future__ import annotations

import os
import resource
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

DEFAULT_ENV_ALLOWLIST = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ"})

HARNESS_LOGIN_ENV = frozenset({"USER"})
"""What a harness CLI needs to find the operator's login — **measured**, not
guessed, and deliberately not in `DEFAULT_ENV_ALLOWLIST`.

The default allowlist exists so that *no credential is inherited* (this
module's own docstring), and `claude -p` under it answers
`Not logged in · Please run /login` — which is the allowlist working, not
failing. Adding this set is therefore a widening of the blast radius and
never a typo correction: with it, code the candidate wrote runs in a process
that can spend the operator's quota. It is applied only where an owner has
said so in `aef.yaml` (`gates.live_model_calls: true`, ADR 0181).

The contents are one variable because that is what the measurement found, on
macOS with `claude` 2.1.x (ADR 0181, four probes of the exact argv
`ClaudeCodeProvider` builds):

  - allowlist as shipped               -> `is_error: true`, `Not logged in`
  - allowlist + `LOGNAME`              -> `is_error: true`, `Not logged in`
  - allowlist + `USER`                 -> `is_error: false`, `OK`
  - allowlist minus `HOME`, + `USER`   -> `is_error: false`, `OK`
  - `PATH` + `USER` only               -> `is_error: false`, `OK`

So it is `USER` specifically: not `HOME` (the credential is not read out of
the config directory), and not `LOGNAME` (the other conventional spelling of
the same fact does not substitute). A harness whose login needs more than
this on some other platform will fail the same visible way — a provider error
naming the CLI's own message — rather than silently.
"""

_GIB = 1024**3
_MIB = 1024**2


class NetworkPolicy(StrEnum):
    REQUIRE_ISOLATED = "require_isolated"
    ACKNOWLEDGED_UNISOLATED = "acknowledged_unisolated"


class SandboxUnavailableError(RuntimeError):
    """The requested isolation cannot be provided here. Deliberately fatal:
    degrading silently is how a sandbox becomes decorative."""


@dataclass(frozen=True)
class SandboxCapabilities:
    """What this run actually got. Recorded on every result so a gate
    outcome can never be read without knowing the conditions it ran under."""

    env_scrubbed: bool
    cwd_confined: bool
    timeout_enforced: bool
    rlimits_applied: tuple[str, ...]
    network_isolated: bool
    filesystem_isolated: bool = False  # container-level only; never true in-process


@dataclass(frozen=True)
class SandboxPolicy:
    timeout_s: float = 120.0
    max_address_space_bytes: int = 2 * _GIB
    max_file_size_bytes: int = 64 * _MIB
    # `RLIMIT_NPROC` is a PER-UID total, not a per-run allowance. 256 read as
    # "this run may spawn 256 processes" and means "this user may have 256
    # processes in total" — so on any host where the operator already has
    # more (a developer laptop: 538 when this was measured), every build
    # command that forks fails with `BlockingIOError: Resource temporarily
    # unavailable`, which G1 reports as an ordinary build failure. ADR 0069's
    # shape: a harness default that rejects every candidate in the adopting
    # environment, silently, and is invisible in a container (ADR 0093).
    #
    # `None` means "do not set it", which is what a limit nobody can choose
    # correctly should default to. An operator who wants a ceiling sets one
    # knowing it counts their own shell.
    max_processes: int | None = None
    env_allowlist: frozenset[str] = field(default_factory=lambda: DEFAULT_ENV_ALLOWLIST)
    extra_env: Mapping[str, str] = field(default_factory=dict)
    network: NetworkPolicy = NetworkPolicy.REQUIRE_ISOLATED
    network_isolation_attested: bool = False
    # An image name, not a `ContainerRuntime`: a plain string keeps this
    # module free of any import of `container.py`, which imports this one.
    # When set, `run_sandboxed` executes inside a container that ACTUALLY
    # blocks egress, and the attestation stops being something a caller
    # asserts and becomes something a probe measured (ADR 0102).
    container_image: str | None = None

    def __post_init__(self) -> None:
        if self.timeout_s <= 0:
            raise ValueError("SandboxPolicy.timeout_s must be positive")
        if (
            self.network is NetworkPolicy.REQUIRE_ISOLATED
            and not self.network_isolation_attested
            and self.container_image is None
        ):
            # Not raised lazily at run() time: a policy that can never run is
            # a configuration error, and finding out at the first gate
            # invocation is finding out too late.
            raise SandboxUnavailableError(
                "network isolation is required but not attested. A process cannot revoke "
                "its own network access — isolation must come from a container or network "
                "namespace, whose runner sets network_isolation_attested=True. To run "
                "without it (local development), set "
                "network=NetworkPolicy.ACKNOWLEDGED_UNISOLATED explicitly; the result will "
                "record network_isolated=False."
            )


@dataclass(frozen=True)
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float
    capabilities: SandboxCapabilities

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def _scrubbed_env(policy: SandboxPolicy) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in policy.env_allowlist}
    env.update(policy.extra_env)
    return env


def _apply_rlimits(policy: SandboxPolicy) -> tuple[str, ...]:
    """Best-effort, and honest about it: macOS ignores or rejects some
    limits that Linux honours, so the applied set is *reported* rather than
    assumed. A caller that needs a hard guarantee reads `capabilities`."""
    applied: list[str] = []
    wanted = [
        ("RLIMIT_FSIZE", getattr(resource, "RLIMIT_FSIZE", None), policy.max_file_size_bytes),
        *(
            [("RLIMIT_NPROC", getattr(resource, "RLIMIT_NPROC", None), policy.max_processes)]
            if policy.max_processes is not None
            else []
        ),
        ("RLIMIT_CORE", getattr(resource, "RLIMIT_CORE", None), 0),
        ("RLIMIT_AS", getattr(resource, "RLIMIT_AS", None), policy.max_address_space_bytes),
    ]
    for name, which, value in wanted:
        if which is None:
            continue
        try:
            soft, hard = resource.getrlimit(which)
            ceiling = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(which, (ceiling, hard))
        except (ValueError, OSError):
            continue
        applied.append(name)
    return tuple(applied)


def probe_rlimits(policy: SandboxPolicy | None = None) -> tuple[str, ...]:
    """Which limits this platform will actually accept, without running
    anything. Used to populate `capabilities` truthfully."""
    policy = policy or SandboxPolicy(network=NetworkPolicy.ACKNOWLEDGED_UNISOLATED)
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child process
        os._exit(len(_apply_rlimits(policy)))
    _, status = os.waitpid(pid, 0)
    count = os.waitstatus_to_exitcode(status)
    names = ("RLIMIT_FSIZE", "RLIMIT_NPROC", "RLIMIT_CORE", "RLIMIT_AS")
    return names[: max(count, 0)]


def _kill_process_group(pid: int) -> None:
    """SIGKILL the timed-out child's whole process group.

    Best-effort: the group may already be gone, and on a platform without
    `killpg` there is nothing to do. Reported as unenforced rather than
    pretended — `timeout_enforced` still means the wall clock was applied to
    the direct child, which is what it always meant.
    """
    if not hasattr(os, "killpg"):
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        return


def child_preexec(policy: SandboxPolicy) -> Callable[[], None]:
    """The confinement a sandboxed child gets, as a reusable callable.

    Exported because the node worker needs exactly this and a second copy
    would drift — which is the defect ADR 0091 named. `setsid()` puts the
    child in its own process group so a timeout can kill its descendants too;
    the rlimits are best-effort and reported rather than assumed.
    """

    def _preexec() -> None:  # pragma: no cover - runs in the forked child
        os.setsid()
        _apply_rlimits(policy)

    return _preexec


def scrubbed_env(policy: SandboxPolicy) -> dict[str, str]:
    """The child's environment. Public for the same reason as above."""
    return _scrubbed_env(policy)


def with_harness_login(policy: SandboxPolicy) -> SandboxPolicy:
    """`policy`, widened by exactly `HARNESS_LOGIN_ENV` and nothing else.

    A named function rather than an inline `|` at the one call site, because
    the widening is the whole security decision of ADR 0181 and a reader
    grepping for "how does a credential reach the worker" should land on a
    docstring rather than on a set literal. The returned policy is a copy:
    `SandboxPolicy` is frozen, so no caller's policy is mutated into a
    credential-carrying one behind its back.
    """
    return replace(policy, env_allowlist=policy.env_allowlist | HARNESS_LOGIN_ENV)


def kill_process_group(pid: int) -> None:
    """SIGKILL a child's whole process group. Public for the same reason."""
    _kill_process_group(pid)


def run_sandboxed(
    argv: Sequence[str],
    *,
    workdir: Path,
    policy: SandboxPolicy | None = None,
) -> SandboxResult:
    """Run `argv` under `policy`, in `workdir`, and report what was enforced.

    Dispatches to the container path when the policy names an image. Imported
    lazily so `container.py` can import this module without a cycle, and so a
    repo with no container runtime never pays for the import.
    """
    policy = policy or SandboxPolicy()
    if policy.container_image is not None:
        from aef.harness.container import detect_container_runtime, run_containerized

        runtime = detect_container_runtime(policy.container_image)
        return run_containerized(argv, workdir=workdir, runtime=runtime, policy=policy)

    workdir = workdir.resolve()
    if not workdir.is_dir():
        raise SandboxUnavailableError(f"sandbox workdir {workdir} does not exist")

    started = time.monotonic()
    timed_out = False
    try:
        # `Popen`, not `subprocess.run`, so the pid is in hand when the
        # timeout fires. `run(timeout=...)` calls `Popen.kill()` — the DIRECT
        # CHILD only — and `_preexec` calls `setsid()`, so descendants sit in
        # a group nothing ever signalled: a grandchild outlived the timeout
        # by six seconds and touched a marker file after the gate reported
        # finished (ADR 0093). Two comments in this module asserted the group
        # was killed; neither was true.
        proc = subprocess.Popen(
            list(argv),
            cwd=str(workdir),
            env=_scrubbed_env(policy),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=child_preexec(policy),
        )
        try:
            out, err = proc.communicate(timeout=policy.timeout_s)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            _kill_process_group(proc.pid)
            out, err = proc.communicate()
            timed_out = True
            returncode = -1
    except OSError as exc:
        timed_out = False
        returncode = -1
        out, err = b"", str(exc).encode()
    duration = time.monotonic() - started

    return SandboxResult(
        returncode=returncode,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        duration_s=duration,
        capabilities=SandboxCapabilities(
            env_scrubbed=True,
            cwd_confined=True,
            timeout_enforced=True,
            rlimits_applied=probe_rlimits(policy),
            network_isolated=policy.network_isolation_attested,
        ),
    )
