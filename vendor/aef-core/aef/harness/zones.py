"""Write-scope zones — the boundary that makes self-coding survivable.

The arithmetic (ADR 0044, ADR 0045 §3): **if an agent can modify what judges
it, the judgement carries no information.** No approval policy prevents that;
only write-scope does. So the repo is partitioned three ways:

| Zone | Contents | Agent-writable |
|---|---|---|
| **A** | the agent root (`agents/**` by default) | yes |
| **B** | the harness — gate code, corpus, eval suite, CI workflow | **never** |
| **C** | everything else, including all of `aef/**` | no |

A diff touching **Zone B is a security event**, not a rejected proposal —
the distinction drives the halt criteria in `05-approval-policy.md` §6.

Deny-by-default is the operating rule, matching `PolicyEngine`
(constraint #6): a path this module cannot *prove* is Zone A comes back
non-writable. Every malformed, ambiguous, or hostile path lands on the deny
side, and does so as a security event, because none of them can arise from
a well-formed `git diff --name-only`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# Zone B is deliberately NOT configurable. There is no constructor argument
# below that shrinks it, because a configurable judge is not a judge — the
# adopting repo may say where its agents live, never where its gates live.
_ZONE_B_ROOTS: tuple[tuple[str, ...], ...] = (
    ("aef", "harness"),
    ("tests", "harness"),
    (".github", "workflows"),
    ("corpus",),
    ("evals",),
)

DEFAULT_AGENT_ROOT = "agents"

# THE default agent module, and the only place in `aef/` allowed to name a
# file under the agent root (ADR 0149; `tests/harness/test_default_agent_path.py`
# enforces it).
#
# It must be **what `aef migrate` actually writes**, because every `--agent-path`
# default in the CLI and in `harness.loop.cycle()` is read by an adopted repo
# that has run `adopt` then `migrate` and nothing else. It used to be
# `agents/demo/graph.py` — aef-core's OWN fixture directory, which exists in no
# adopted repo — so `aef loop doctor` printed a `bless` fix line that could not
# be run and `aef loop cycle` exited **0** with `no agent source at
# agents/demo/graph.py`: ADR 0139's signature "silently inert" failure, reached
# from the documented defaults (reproduced, ADR 0149).
#
# It lives HERE, beside the root it is built from, and not in
# `aef.cli.migrate` where `DEFAULT_MIGRATED_OUT` used to own it, because
# `aef/harness/loop.py` needs it and the harness does not import the CLI.
# `aef.cli.migrate.DEFAULT_MIGRATED_OUT` is now an alias for this constant, so
# the writer and the default cannot drift apart again.
DEFAULT_AGENT_PATH = f"{DEFAULT_AGENT_ROOT}/migrated/graph.py"

# The two graph files that live OUTSIDE the agent root, named here for the
# same reason `DEFAULT_AGENT_PATH` is: `discover_graph_files` below is shared
# with `aef/cli/doctor.py`, and the harness does not import the CLI, so a copy
# of either string in `aef.cli.migrate` would be the ADR 0091 drift shape
# again. `aef.cli.migrate.LEGACY_MIGRATED_OUT` is now an alias for the second.
#
# `ADAPTER_SHIM` is what `aef adopt` writes and the documented adoption path
# ends at; `LEGACY_AGENT_PATH` is where `aef migrate` wrote before ADR 0143,
# kept ONLY so that a repo migrated by an older `aef` does not silently drop
# out of discovery the day the default moved.
ADAPTER_SHIM = "aef_adapter.py"
LEGACY_AGENT_PATH = "aef_migrated.py"

# The `SKILL.md` that `aef adopt` writes into the repo it is adopting: the
# per-model-release re-audit (ADR 0111). It is aef's OWN output, not part of
# the adopter's prompt surface, and every count of "how many skills does this
# repo have" must exclude it or the answer changes the moment adoption runs.
#
# It lives HERE for the same reason `DEFAULT_AGENT_PATH` does: two CLI modules
# need it, `aef/cli/adopt.py` already imports `aef/cli/migrate.py`, and the
# reverse import is a hard cycle — reproduced (ADR 0176), both directions:
#
#     ImportError: cannot import name '_ADOPT_SKILL_PATH' from partially
#     initialized module 'aef.cli.adopt' (most likely due to a circular import)
#
# `aef/harness/zones.py` is what both of them already import, and the harness
# imports neither, so this is the one place the string can be derived from.
ADOPT_SKILL_PATH = ".claude/skills/new-model-check/SKILL.md"


def discover_graph_files(root: Path, *, agent_root: str = DEFAULT_AGENT_ROOT) -> list[str]:
    """Every file in `root` that is "a graph this repo runs", repo-relative.

    ONE function, because there were two answers and both were wrong in the
    same direction. `aef doctor` globbed `<agent root>/*/graph.py` — one level
    — while ADR 0152's `aef migrate` writes `<agent root>/migrated/<module>/
    graph.py`, which is two. Reproduced on the pilot clone with **nine**
    graphs on disk: doctor's model-call advisory scanned two entries
    (`aef_adapter.py` and `agents/migrated/graph.py`, the call-site stub whose
    `build_graph()` raises `NotImplementedError`), passed obligation 6 on a
    file that makes no model call at all, and never opened the eight graphs
    that do.

    So the search is `rglob`, not `glob`, and it is deliberately shaped like
    `discover_prompt_agents`' own recursion — which was itself measured
    against the Claude Code CLI rather than assumed (ADR 0152 §2). A depth
    limit here is a promise about a layout `migrate` is free to change; the
    filename is the contract.

    The three named entries are outside the agent root and cannot be found by
    walking it: the adapter shim, `DEFAULT_AGENT_PATH` (which `--agent-root`
    does NOT move — `aef migrate --agent-root .claude/agents` still writes the
    call-site graph to `agents/migrated/graph.py`, so widening the root must
    not make it invisible), and the pre-0143 legacy path.

    Order is stable and de-duplicated: named entries first in adoption order,
    then the walk, sorted. Nothing is filtered on content — whether a graph
    file is *interesting* is the caller's question, and a file this function
    hides is a file no diagnostic can report on.
    """
    entries: list[str] = []

    def add(relative: str) -> None:
        if relative not in entries:
            entries.append(relative)

    for name in (ADAPTER_SHIM, DEFAULT_AGENT_PATH, LEGACY_AGENT_PATH):
        if (root / name).is_file():
            add(name)

    base = root / Path(agent_root) if agent_root else root
    if base.is_dir():
        for graph in sorted(base.rglob("graph.py")):
            if not graph.is_file():  # pragma: no cover - a directory named graph.py
                continue
            try:
                add(graph.relative_to(root).as_posix())
            except ValueError:  # pragma: no cover - agent_root escaping the repo
                continue
    return entries


class Zone(StrEnum):
    A = "A"  # agent-writable
    B = "B"  # the harness — a diff here is a security event
    C = "C"  # core; not agent-writable


@dataclass(frozen=True)
class ZonePolicy:
    """Per-repo configuration. Only the *agent* root is configurable.

    Note this config lives in Zone C and gates read it from the base ref
    (see `trust.py`), so a candidate cannot widen its own zone by editing
    it. Zone B precedence below holds independently of that, as defence in
    depth: even `agent_root=""` leaves the harness in Zone B.
    """

    agent_root: str = DEFAULT_AGENT_ROOT

    @property
    def agent_segments(self) -> tuple[str, ...]:
        return tuple(s for s in self.agent_root.split("/") if s)


@dataclass(frozen=True)
class PathVerdict:
    path: str
    zone: Zone
    allowed: bool
    reason: str
    security_event: bool = False


@dataclass(frozen=True)
class ZoneVerdict:
    verdicts: tuple[PathVerdict, ...]

    @property
    def allowed(self) -> bool:
        return all(v.allowed for v in self.verdicts)

    @property
    def rejected(self) -> tuple[PathVerdict, ...]:
        return tuple(v for v in self.verdicts if not v.allowed)

    @property
    def security_events(self) -> tuple[PathVerdict, ...]:
        return tuple(v for v in self.verdicts if v.security_event)


def _segments(raw: str) -> tuple[tuple[str, ...] | None, str]:
    """Split a repo-relative POSIX path into segments, or explain why it is
    not one. Returns `(segments, reason)`; `segments` is `None` iff the path
    is malformed, in which case `reason` says how."""
    path = raw.strip()
    if not path:
        return None, "empty path"
    if "\x00" in path:
        return None, "null byte in path"
    if "\\" in path:
        # git emits forward slashes. A backslash is either a literal
        # filename character or a hand-built Windows path; this classifier
        # vouches for neither.
        return None, "backslash separator; expected a POSIX repo-relative path"
    if path.startswith("/"):
        return None, "absolute path; diff paths are repo-relative"

    parts = [p for p in path.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None, "path traversal ('..') is never resolved, only refused"
    if not parts:
        return None, "path resolves to the repo root, not a file"
    return tuple(parts), ""


def segment_refusal(name: str) -> str:
    """`""` if `name` is exactly one safe path segment, else why it is not.

    The same deny-by-default rule `_segments` already applies to diff paths,
    exposed for the other places a caller-supplied string is joined onto a
    directory. `aef/harness/archive.py` builds `root / graph_id` and a
    `graph_id` is whatever a persona's `name:` frontmatter says: `../escape`
    wrote `state/escape/v000001/` — one level ABOVE the archive root it was
    handed, with the archive root left empty (reproduced, ADR 0168).

    Normalisation is refused rather than applied. `./x` and `x` name the same
    file, and a checker that silently accepted the first would let two spellings
    of one graph id disagree about which directory they mean; the caller is told
    to pass the segment it means.
    """
    segments, reason = _segments(name)
    if segments is None:
        return reason
    if segments != (name,):
        return f"{name!r} is not a single path segment (normalises to {'/'.join(segments)!r})"
    return ""


def _under(segments: tuple[str, ...], root: tuple[str, ...]) -> bool:
    """True iff `segments` names a file strictly beneath `root`.

    Segment-wise and case-sensitive on purpose. A string prefix test would
    put `agentsfoo/x.py` in Zone A; a case-insensitive test would accept
    `AGENTS/x.py`, which on a case-insensitive macOS filesystem is the same
    file on disk while git treats it as a different path. Deny the variant
    rather than guess which one the filesystem will hand back.
    """
    return len(segments) > len(root) and segments[: len(root)] == root


def inspect_path(path: str, policy: ZonePolicy | None = None) -> PathVerdict:
    policy = policy or ZonePolicy()
    segments, reason = _segments(path)

    if segments is None:
        # Malformed paths cannot come out of a well-formed `git diff`, so
        # they are treated as probing rather than as an ordinary rejection.
        return PathVerdict(
            path=path, zone=Zone.C, allowed=False, reason=f"{path!r}: {reason}", security_event=True
        )

    # Zone B is checked FIRST and unconditionally: no agent-root
    # configuration can move a gate out of the harness.
    for root in _ZONE_B_ROOTS:
        if _under(segments, root):
            return PathVerdict(
                path=path,
                zone=Zone.B,
                allowed=False,
                reason=(
                    f"{path}: Zone B (harness) — {'/'.join(root)}/** judges the candidate and is "
                    f"never agent-writable — this is a security event, not a proposal"
                ),
                security_event=True,
            )

    if _under(segments, policy.agent_segments):
        return PathVerdict(
            path=path, zone=Zone.A, allowed=True, reason=f"{path}: Zone A (agent-writable)"
        )

    return PathVerdict(
        path=path,
        zone=Zone.C,
        allowed=False,
        reason=(
            f"{path}: Zone C (core) — not under the agent root "
            f"{policy.agent_root or '<repo root>'!r}; only Zone A is agent-writable"
        ),
    )


def classify_path(path: str, policy: ZonePolicy | None = None) -> Zone:
    return inspect_path(path, policy).zone


def enforce_zones(
    paths: list[str] | tuple[str, ...], policy: ZonePolicy | None = None
) -> ZoneVerdict:
    """Classify every path in a candidate diff. The candidate is allowed
    only if *every* path is Zone A — one violation rejects the whole diff,
    since a candidate is merged or not merged as a unit."""
    return ZoneVerdict(verdicts=tuple(inspect_path(p, policy) for p in paths))
