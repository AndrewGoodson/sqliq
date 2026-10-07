"""G1 — does the candidate build?

Runs, in the sandbox, against the **post-merge workspace** (base ref +
Zone A overlay, `workspace.py`): every configured build command must succeed.
The default set mirrors this repo's green bar minus the formatter, which
rejects on cosmetics rather than correctness.

Two things this gate deliberately does **not** do, both corrected from the
superseded spec after the three-reviewer audit (04 §1.9):

- **No reachability check.** Routing is chosen by node code, not authorised
  by edges: `_resolve_route` returns the `Route` the node supplied
  (`executor.py`). `END` therefore never appears in an `Edge`, so
  END-reachability is undecidable from the edge set — and node reachability
  produces false positives, because `fallback_node_id` targets bypass edge
  resolution entirely (ADR 0036), making a correctly-wired fallback-only node
  look like an orphan.
- **No graph loading of its own.** `Graph.compile()` runs as part of the
  candidate's own test suite, where it belongs.

The suite must pass **as of the candidate**, which is the point: an agent
cannot delete the test that fails, because the tests are Zone B and come from
the base ref.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aef.harness.gates.base import Gate, GateContext, GateOutcome, GateResult
from aef.harness.sandbox import NetworkPolicy, SandboxPolicy, run_sandboxed
from aef.harness.workspace import build_candidate_workspace

# Deliberately NOT aef-core's own green bar. `mypy --strict aef` runs against
# the ADOPTING repo, which has no `aef/` directory, so it failed every
# candidate in every adopting repo — forever, and silently, since the failure
# looks like an ordinary gate rejection (ADR 0069). A repo's build command is
# repo-specific; the only safe default is the one every Python repo shares.
DEFAULT_BUILD_COMMANDS: tuple[tuple[str, ...], ...] = (("python", "-m", "pytest", "-q"),)


@dataclass(frozen=True)
class G1Builds(Gate):
    id: str = "G1"
    commands: tuple[tuple[str, ...], ...] = field(default_factory=lambda: DEFAULT_BUILD_COMMANDS)

    def run(self, ctx: GateContext) -> GateResult:
        # `workspace-G1`, not `workspace`. G2 materialises its own tree from
        # the same `ctx.workdir` when the cohort could not be built, and
        # `trust._prepare_empty_destination` refuses a non-empty destination —
        # so with one shared name the second gate to run raised
        # `TrustBoundaryError` and could never judge the candidate at all
        # (ADR 0170 defect 1; seen as a red herring in ADR 0148 and reported
        # in ADR 0157).
        #
        # A per-gate directory rather than sharing G1's tree, because the
        # emptiness rule protects something real: a workspace must be exactly
        # base-ref + Zone A overlay, and THIS gate has just run build commands
        # in its copy. Those commands come from configuration and execute
        # candidate code; whatever they wrote (caches, artefacts, anything)
        # would otherwise be in the tree G2 re-executes the corpus against.
        workspace = build_candidate_workspace(
            ctx.repo, ctx.verdict.diff, ctx.workdir / f"workspace-{self.id}", ctx.zone_policy
        )
        policy = ctx.sandbox_policy or SandboxPolicy(network=NetworkPolicy.ACKNOWLEDGED_UNISOLATED)

        evidence: list[str] = []
        for command in self.commands:
            result = run_sandboxed(command, workdir=workspace, policy=policy)
            label = " ".join(command)
            if result.ok:
                evidence.append(f"{label}: ok")
                continue

            detail = "timed out" if result.timed_out else f"exit {result.returncode}"
            tail = (result.stderr or result.stdout).strip().splitlines()[-20:]
            return GateResult(
                gate=self.id,
                outcome=GateOutcome.FAIL,
                reason=f"build command failed ({detail}): {label}",
                evidence=(*evidence, f"{label}: {detail}", *tail),
            )

        return GateResult(
            gate=self.id,
            outcome=GateOutcome.PASS,
            reason=f"{len(self.commands)} build command(s) succeeded against the merged workspace",
            evidence=tuple(evidence),
        )
