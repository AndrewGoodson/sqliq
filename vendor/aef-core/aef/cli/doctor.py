"""`aef doctor` — sanity-check an AEF-adopted (or freshly-initialized)
repo's setup: Python version, presence of CLAUDE.md, and validity of every
`aef.yaml` it can find.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from aef.config import AgentConfig, AgentConfigError, load_agent_config
from aef.harness.preflight import model_calls_are_visible
from aef.harness.zones import DEFAULT_AGENT_ROOT, discover_graph_files


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str
    # "error" checks fail the overall `aef doctor` (exit 1) when not ok;
    # "advisory" checks are warnings only — surfaced to the user but never
    # flipping the exit code, for semantically-degenerate-but-valid config
    # (review Finding 4) that pydantic can't judge intent on.
    level: str = "error"


def _adapter_check(adapter: Path) -> DoctorCheck:
    """Does `aef_adapter.py` at least parse and expose `build_graph`?

    Parsed, not imported: importing an adopter's shim would execute whatever
    their legacy entrypoint does at module scope, and a diagnostic must not
    have side effects. Parsing catches the case that shipped — a file that is
    not valid Python — without running anything.
    """
    try:
        source = adapter.read_text()
        tree = ast.parse(source, filename=str(adapter))
    except UnicodeDecodeError as exc:
        return DoctorCheck("adapter_importable", False, f"{adapter} is not UTF-8: {exc}")
    except SyntaxError as exc:
        return DoctorCheck("adapter_importable", False, f"{adapter} does not parse: {exc}")
    except OSError as exc:  # pragma: no cover - unreadable file
        return DoctorCheck("adapter_importable", False, f"{adapter} is unreadable: {exc}")

    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    if "build_graph" not in names:
        return DoctorCheck(
            "adapter_importable",
            False,
            f"{adapter} defines no top-level build_graph(); nothing can load it",
        )

    still_a_stub = "wire your existing entrypoint into this node" in source
    return DoctorCheck(
        "adapter_importable",
        not still_a_stub,
        f"{adapter} parses and defines build_graph()"
        + (" — still the generated stub, not wired yet" if still_a_stub else ""),
        level="advisory" if still_a_stub else "info",
    )


def _link_on_the_way_to(target_dir: Path, path: Path) -> Path | None:
    """The first symlink between `target_dir` and `path`, or `None`.

    The same walk `aef adopt`'s writer does, and deliberately so: adopt refuses
    a path that IS a link or is UNDER one, so a diagnostic that only looked at
    the leaf would prescribe a fix adopt declines to perform for a parent
    directory link (ADR 0168, R7).
    """
    if path.is_symlink():
        return path
    parent = path.parent
    while parent != target_dir and parent != parent.parent:
        if parent.is_symlink():
            return parent
        parent = parent.parent
    return None


def _entry_file_fix(target_dir: Path, entry_file: Path, name: str) -> str:
    """What to actually DO about an entry file with no aef block.

    REPRODUCED (ADR 0168, R7). A repo whose `AGENTS.md` and `CLAUDE.md` are both
    symlinks into `docs/` — an ordinary cross-tool arrangement — gets:

        $ aef adopt --dir .
        skipped .../AGENTS.md (a symlink, or under one — adoption never writes through a link)
        skipped .../CLAUDE.md (a symlink, or under one — adoption never writes through a link)
        $ aef doctor --dir .
        [WARN] entry_file_points_at_the_guide:AGENTS.md: ... fix: re-run `aef adopt --dir .`

    which skips it again. `is_file()` follows the link, so the check reads the
    target's bytes, finds no block, and prescribes the one command that is
    guaranteed not to add one. The only advice on offer was a loop.

    Reading THROUGH the link stays right — a link whose target carries the block
    does reach the agent, and that case passes. What was wrong was the fix.
    """
    link = _link_on_the_way_to(target_dir, entry_file)
    if link is None:
        return (
            "re-run `aef adopt --dir .`, which appends a block between "
            "`<!-- aef:begin -->` and `<!-- aef:end -->` and leaves every other byte "
            "of the file alone"
        )
    try:
        resolved = entry_file.resolve()
        shown: str | Path = resolved.relative_to(target_dir)
    except (OSError, ValueError):  # outside the repo, or a broken link
        shown = entry_file.resolve(strict=False)
    via = "" if link == entry_file else f" (via the symlinked directory {link.name}/)"
    return (
        f"{name} is a SYMLINK{via} to {shown}, and `aef adopt` never writes through a "
        f"link — it reports `a symlink, or under one` and skips, so re-running it will "
        f"not add the block and telling you to is a loop. Do one of: add the block to "
        f"the TARGET ({shown}) — appending `<!-- aef:begin -->` … `<!-- aef:end -->` "
        f"with a pointer to AGENT_INTEGRATION.md, which is what adopt would have "
        f"written — or replace the link with a real file that carries the target's "
        f"content plus the block. Either way the agent reading {name} sees the "
        f"contract; adopt will then leave it alone."
    )


def _graph_entries(
    target_dir: Path,
    agent_path: str | None,
    agent_root: str = DEFAULT_AGENT_ROOT,
) -> list[str]:
    """Which files are "the configured graph" for the model-call advisory.

    Keyed on the graph, not on migrate's output file. The advisory was written
    against the fixture the K1 increment had in hand — an adopter who ran
    `aef migrate` — and migrate's output is the artefact of a *different*
    command. The documented adoption path is `aef adopt`, wire
    `aef_adapter.py`, write nodes under `<agent_root>/**`; a repo that followed
    it to the letter produced no migrated file at all, so the advisory could
    not fire for the only path the docs describe (reproduced, ADR 0141).

    An explicit `--agent-path` wins outright — it is the same flag
    `aef loop doctor` takes and it means the owner has said which file it is.
    Otherwise the walk is `aef.harness.zones.discover_graph_files`, shared with
    the loop's own preflight so that the two cannot disagree about which files
    exist.

    **Erratum on ADR 0149.** The docstring this replaces claimed that deriving
    every path from `DEFAULT_AGENT_ROOT` meant the list "cannot leave this list
    naming a directory nothing writes to". That held while `aef migrate` had
    one writer. ADR 0152 added a second — one graph per prompt agent at
    `<agent root>/migrated/<module>/graph.py` — and the glob here was
    `<agent root>/*/graph.py`, one level. Derivation kept the *root* correct
    and said nothing about the *depth*, so on the pilot clone doctor listed two
    entries against nine graphs on disk and passed obligation 6 on the one file
    that makes no model call. The invariant is now enforced by running the real
    `run_migrate` into this function
    (`tests/cli/test_doctor_discovery.py::test_doctor_lists_every_graph_migrate_wrote`),
    not by a shared constant.

    `agent_root` is the repo's own — `aef doctor --agent-root .claude/agents`
    for a repo that widened Zone A. Without it, a widened repo's sixteen Zone A
    files went unmentioned while doctor reported on two Zone C ones.
    """
    if agent_path:
        return [agent_path]
    return discover_graph_files(target_dir, agent_root=agent_root)


def run_doctor(
    target_dir: Path,
    *,
    agent_path: str | None = None,
    agent_root: str = DEFAULT_AGENT_ROOT,
) -> list[DoctorCheck]:
    target_dir = target_dir.resolve()
    checks: list[DoctorCheck] = []

    checks.append(
        DoctorCheck(
            "python_version",
            sys.version_info >= (3, 11),
            f"{sys.version_info.major}.{sys.version_info.minor} (need >=3.11)",
        )
    )

    # An `aef init` project legitimately has no CLAUDE.md — `init` does not
    # write one. Hard-failing that left a pristine `aef init` repo at exit 1
    # with no fix but to hand-write the file, while AGENT_INTEGRATION.md says
    # "fix any [FAIL]". Advisory when the repo looks init-shaped, an error
    # when it looks adopted (ADR 0079).
    claude_md = target_dir / "CLAUDE.md"
    adapter = target_dir / "aef_adapter.py"
    adoption_markers = (
        target_dir / "AEF_MIGRATION_CHECKLIST.md",
        target_dir / "AGENT_INTEGRATION.md",
        target_dir / "AUTONOMY.md",
        target_dir / "LOOP.md",
    )
    looks_adopted = (
        adapter.exists()
        or adapter.is_symlink()
        or any(marker.exists() for marker in adoption_markers)
    )
    claude_md_detail = (
        str(claude_md)
        if claude_md.exists()
        else f"{claude_md} not found — run `aef adopt` to generate one for an existing repo, "
        f"or add your own if this is a fresh `aef init`-based project"
    )
    checks.append(
        DoctorCheck(
            "claude_md_present",
            claude_md.exists(),
            claude_md_detail,
            level="error" if looks_adopted else "advisory",
        )
    )

    # ADR 0153. `aef adopt` used to SKIP an entry file that already existed,
    # so a repo with its own `AGENTS.md` (or `CLAUDE.md`) was adopted with the
    # scaffold contract in files its agents never open: measured on a real
    # 8-agent repo, `grep -c AEF AGENTS.md` returned 0 after a clean adopt.
    # Adopt now appends a marker block instead — but a repo adopted before
    # that, or one whose block was deleted, still has the gap, and nothing
    # told anyone. Advisory: an adopter may legitimately point their agents
    # somewhere else, as long as they know they have.
    if looks_adopted:
        for name in ("CLAUDE.md", "AGENTS.md"):
            entry_file = target_dir / name
            if not entry_file.is_file():
                continue
            try:
                text = entry_file.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            # A block adopt wrote carries a signed begin marker since ADR 0172
            # (`<!-- aef:begin sha256=… -->`); the bare form is an adopter's prose.
            points_at_the_guide = (
                re.search(r"<!-- aef:begin sha256=[0-9a-f]{16} -->", text) is not None
                or "AGENT_INTEGRATION.md" in text
            )
            checks.append(
                DoctorCheck(
                    f"entry_file_points_at_the_guide:{name}",
                    points_at_the_guide,
                    f"{entry_file} carries the aef block"
                    if points_at_the_guide
                    else f"{entry_file} is what your coding agent reads and it names neither the "
                    f"aef marker block nor AGENT_INTEGRATION.md — the scaffold contract "
                    f"never reaches the agent. fix: "
                    + _entry_file_fix(target_dir, entry_file, name),
                    level="info" if points_at_the_guide else "advisory",
                )
            )

    # F4: `aef adopt` promises doctor "confirms the config and IMPORTS are
    # wired correctly" and doctor never imported anything — so a syntactically
    # invalid `aef_adapter.py` passed clean.
    if adapter.is_file():
        checks.append(_adapter_check(adapter))
    elif looks_adopted:
        checks.append(
            DoctorCheck(
                "adapter_present",
                False,
                f"{adapter} is not a file — restore it or rerun `aef adopt`",
            )
        )

    # ADR 0137. `aef doctor` reported this repo green: `aef migrate` had
    # wrapped a function that builds its own `anthropic.Anthropic()`, so the
    # model call reached no `Services.model_provider`, the recorder captured
    # no `RecordedCall`, and the gates could only replay it by calling the
    # vendor live or scoring it 0. Nothing anywhere said so.
    #
    # Advisory, not an error: `aef doctor` checks that a setup is coherent,
    # while readiness to be gated is `aef loop doctor`'s question. An adopter
    # who has not routed their calls yet is mid-migration, not broken.
    for entry in _graph_entries(target_dir, agent_path, agent_root):
        visible, detail, fix = model_calls_are_visible(target_dir, entry)
        checks.append(
            DoctorCheck(
                f"model_calls_visible:{entry}",
                visible,
                detail
                + (
                    ""
                    if visible
                    else ". It bypasses the policy engine and the fallback chain, and the "
                    f"gates cannot replay a call the recorder never saw. fix: {fix}"
                ),
                level="info" if visible else "advisory",
            )
        )

    config_candidates = sorted(
        {*target_dir.glob("aef.yaml"), *target_dir.glob("agents/*/aef.yaml")}
    )
    if not config_candidates:
        checks.append(
            DoctorCheck("agent_config", False, "no aef.yaml found — run `aef adopt` or `aef init`")
        )
    for path in config_candidates:
        try:
            config = load_agent_config(path)
        except AgentConfigError as exc:
            checks.append(DoctorCheck(f"agent_config:{path}", False, str(exc)))
        else:
            checks.append(DoctorCheck(f"agent_config:{path}", True, "valid"))
            checks.extend(_config_advisories(path, config))

    return checks


def _config_advisories(path: Path, config: AgentConfig) -> list[DoctorCheck]:
    """Warnings for config that validates but is semantically degenerate —
    review Finding 4. Advisory level only: `aef doctor` stays exit-0 unless
    something is actually broken. Not schema rejections, because a user may
    genuinely want e.g. two same-vendor endpoints, which pydantic can't
    judge."""
    out: list[DoctorCheck] = []
    primary = config.model_provider.impl if config.model_provider is not None else None
    fallback = config.model_provider.fallback if config.model_provider is not None else []

    if primary in fallback:
        out.append(
            DoctorCheck(
                f"advisory:{path}:fallback_same_as_primary",
                False,
                f"model_provider.fallback lists the primary impl {primary!r} — a same-vendor "
                f"fallback fails identically on a vendor outage; consider a different provider",
                level="advisory",
            )
        )
    if len(fallback) != len(set(fallback)):
        dupes = sorted({impl for impl in fallback if fallback.count(impl) > 1})
        out.append(
            DoctorCheck(
                f"advisory:{path}:fallback_duplicates",
                False,
                f"model_provider.fallback has duplicate entries {dupes} — each is tried in "
                f"order, so duplicates add no resilience",
                level="advisory",
            )
        )
    # `.startswith("TODO")` as well as empty: `aef adopt` writes
    # `objectives: "TODO: describe this agent's objective..."`, which is
    # non-empty — so the check built to catch "the agent has no stated
    # purpose" could not see the one string that ships by default (ADR 0079).
    objectives = config.objectives.strip()
    if not objectives or objectives.upper().startswith("TODO"):
        out.append(
            DoctorCheck(
                f"advisory:{path}:empty_objectives",
                False,
                "objectives is empty or still the generated TODO — the agent has no stated "
                "purpose; fill it in",
                level="advisory",
            )
        )
    return out
