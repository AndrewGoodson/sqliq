"""`aef adopt` — run inside an existing, unrelated repo to start migrating it
onto the AEF scaffold. This is the adoption path the scaffold exists for:
detect what's there today, generate the artifacts a future session (in that
repo, possibly a different coding-agent session with no other context) needs
to continue the migration, and never silently overwrite anything already
in the target repo.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from importlib import resources
from pathlib import Path

from aef.cli.adopt_loop import (
    render_agents_zone_readme,
    render_corpus_readme,
    render_first_day_md,
    render_loop_gate_workflow,
    render_loop_md,
    render_loop_monitor_workflow,
)
from aef.cli.adopt_offline import (
    OFFLINE_CHECKLIST,
    OFFLINE_CONFIG,
    OFFLINE_ENTRY,
    render_offline_guide,
)
from aef.cli.learning_prompt import EVIDENCE_LEARNING_PROTOCOL
from aef.cli.migrate import (
    DEFAULT_MIGRATED_OUT,
    discover_prompt_agents,
    discover_skills,
)
from aef.harness.zones import DEFAULT_AGENT_ROOT

_IGNORED_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "site-packages",
        ".mypy_cache",
        ".ruff_cache",
    }
)

_LANGGRAPH_RE = re.compile(r"^\s*(?:import|from)\s+langgraph\b", re.MULTILINE)
_CREWAI_RE = re.compile(r"^\s*(?:import|from)\s+crewai\b", re.MULTILINE)
_RAW_SDK_RE = re.compile(r"^\s*(?:import|from)\s+(?:openai|anthropic)\b", re.MULTILINE)

# Dependency-manifest signals use a looser word-boundary match (no
# import/from syntax to anchor on) — a freshly-scaffolded repo that has
# `langgraph` in requirements.txt/pyproject.toml but hasn't written much
# code yet is exactly the adoption-path moment `aef adopt` should still
# get right, not report as "none" until enough code accumulates.
_LANGGRAPH_MANIFEST_RE = re.compile(r"\blanggraph\b")
_CREWAI_MANIFEST_RE = re.compile(r"\bcrewai\b")
_RAW_SDK_MANIFEST_RE = re.compile(r"\b(?:openai|anthropic)\b")
_MANIFEST_GLOBS = ("requirements*.txt", "pyproject.toml", "Pipfile")

Framework = str  # one of "langgraph", "crewai", "raw_sdk", "prompt_files", "none"

# The `/new-model-check` skill `adopt` itself writes. Named once and used
# twice — where it is written and where prompt-file detection EXCLUDES it —
# because a scaffold that counts its own output as the adopter's agent surface
# reports a different number on every run, and the marker block interpolates
# that number into files it must be able to rewrite byte-identically.
_ADOPT_SKILL_PATH = ".claude/skills/new-model-check/SKILL.md"

# The marker pair. `<!-- ... -->` in markdown-ish files (`.md`, `.mdc`), `#`
# in `.gitignore`, because a marker the file's own syntax does not tolerate is
# a marker that breaks the file it is protecting.
#
# These are the BARE forms — the strings this scaffold's own documentation
# teaches an adopter, and therefore strings an adopter's prose can contain.
# What `aef adopt` actually writes is the SIGNED begin marker below, and only
# a signed pair is treated as adopt's (ADR 0172). A balanced bare pair in the
# adopter's own text used to make adopt delete everything between it while the
# report said "your bytes outside it are unchanged".
MD_MARKERS = ("<!-- aef:begin -->", "<!-- aef:end -->")
GITIGNORE_MARKERS = ("# aef:begin", "# aef:end")

# Hex characters of the sha256 kept in the begin marker. 16 is 64 bits: long
# enough that no prose contains one by accident (which is what the signature
# is FOR — authorship, not integrity), short enough to stay on one line.
_SIGNATURE_HEX = 16
_SIGNATURE_RE_BODY = rf" sha256=[0-9a-f]{{{_SIGNATURE_HEX}}}"


def _split_begin(begin: str) -> tuple[str, str]:
    """`<!-- aef:begin -->` -> `('<!-- aef:begin', ' -->')`; `# aef:begin` ->
    `('# aef:begin', '')`. The signature is inserted between the two halves so
    the marker stays syntactically legal in the file it lives in."""
    return (begin[:-4], " -->") if begin.endswith(" -->") else (begin, "")


def signed_begin(begin: str, body: str) -> str:
    """The begin marker `aef adopt` writes: the bare marker plus a sha256 of
    the block body it opens.

    `<!-- aef:begin sha256=1a2b3c4d5e6f7081 -->`

    This is the whole of how `apply_block` tells a pair IT wrote from a pair
    the adopter merely quoted. The digest is over the body, so re-rendering
    the same block reproduces the same marker byte-for-byte (idempotency), and
    a changed block gets a changed marker.
    """
    stem, tail = _split_begin(begin)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:_SIGNATURE_HEX]
    return f"{stem} sha256={digest}{tail}"


def _signed_begin_re(begin: str) -> re.Pattern[str]:
    stem, tail = _split_begin(begin)
    return re.compile(re.escape(stem) + _SIGNATURE_RE_BODY + re.escape(tail))


# What the FIRST non-blank line of a pre-signature (ADR 0153 "M2") block body
# looks like. Used only to decide whether a bare marker pair is an old block
# of adopt's — to be upgraded once, in place — or the adopter's prose, which
# is left alone. Both strings are things `render_aef_block_body` /
# `render_gitignore_block` actually emit, so this cannot drift into a guess.
_LEGACY_BODY_HEADS = ("## AEF scaffold (", "__pycache__/")

# `agents/migrated/graph.py` -> `agents/migrated/<agent>/graph.py`, the shape
# `aef migrate` writes one-graph-per-prompt-agent into (`<agent>` is the agent
# name as a Python module name; the graph's own `graph_id` keeps the name as
# written). Derived from migrate's own default rather than spelled out
# (ADR 0091's rule).
_PROMPT_AGENT_OUT_SHAPE = f"{DEFAULT_MIGRATED_OUT.rsplit('/', 1)[0]}/<agent>/graph.py"


def _framework_files(repo_root: Path, max_files: int) -> tuple[list[Path], list[Path]]:
    """Walk owned source once, pruning dependency trees before entering them.

    File budgets apply separately to code and manifests. Sorting keeps the
    sampled files stable across machines; links and special files cannot add
    external imports or block the scan waiting for a FIFO writer.
    """
    python_files: list[Path] = []
    manifests: list[Path] = []
    if max_files <= 0:
        return python_files, manifests
    for directory, subdirectories, filenames in os.walk(repo_root, followlinks=False):
        root = Path(directory)
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if name not in _IGNORED_DIR_NAMES and not (root / name).is_symlink()
        )
        for name in sorted(filenames):
            if name.endswith(".py") and len(python_files) < max_files:
                selected = python_files
            elif (
                any(fnmatchcase(name, pattern) for pattern in _MANIFEST_GLOBS)
                and len(manifests) < max_files
            ):
                selected = manifests
            else:
                continue
            path = root / name
            try:
                if stat.S_ISREG(path.lstat().st_mode):
                    selected.append(path)
            except OSError:
                continue
        if len(python_files) >= max_files and len(manifests) >= max_files:
            break
    return python_files, manifests


def _read_all(paths: list[Path]) -> str:
    parts: list[str] = []
    for path in paths:
        try:
            parts.append(path.read_bytes().decode("utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(parts)


@dataclass(frozen=True)
class PromptSurface:
    """What a repo whose agents are *prompt files* actually has.

    Every eligible repo surveyed for `UPGRADE_LOOP.md` had **zero** model-SDK
    call sites and between three and twenty-six agent prompts. `aef migrate`
    found nothing in any of them and `aef adopt` reported `none`, which is the
    label for "no orchestration code to migrate away from" — true, and
    useless, when the agents are `.md` files run by a coding-agent harness.
    """

    agents: int = 0
    skills: int = 0
    has_agents_md: bool = False
    has_codex_dir: bool = False
    has_copilot_instructions: bool = False
    cursor_rules: int = 0

    @property
    def total(self) -> int:
        """How many prompt-surface signals were found at all. Zero means the
        repo has no prompt agents and `prompt_files` must not be reported."""
        return (
            self.agents
            + self.skills
            + int(self.has_agents_md)
            + int(self.has_codex_dir)
            + int(self.has_copilot_instructions)
            + self.cursor_rules
        )

    def describe(self) -> str:
        """`8 agents, 5 skills, AGENTS.md, .codex` — the counts, in the order
        an owner would list them, with nothing named that is not there."""
        parts: list[str] = []
        if self.agents:
            parts.append(f"{self.agents} agent{'s' if self.agents != 1 else ''}")
        if self.skills:
            parts.append(f"{self.skills} skill{'s' if self.skills != 1 else ''}")
        if self.has_agents_md:
            parts.append("AGENTS.md")
        if self.has_codex_dir:
            parts.append(".codex")
        if self.has_copilot_instructions:
            parts.append(".github/copilot-instructions.md")
        if self.cursor_rules:
            parts.append(f"{self.cursor_rules} cursor rule{'s' if self.cursor_rules != 1 else ''}")
        return ", ".join(parts)


# The two headings only `render_claude_md` and `render_harness_pointer`
# produce. Used to tell a file `aef adopt` WROTE from one it merely appended a
# block to — a distinction the marker alone cannot make, and getting it wrong
# is what made the counts differ between run 1 and run 2 (the file was the
# adopter's before adopt touched it, and still is).
_GENERATED_HEADINGS = (" — AEF scaffold contract", " — agent instructions (aef-core)")


def _is_adopt_generated(path: Path) -> bool:
    """Was this whole file written by `aef adopt`, as opposed to being the
    adopter's file with an appended block?

    Counting adopt's own entry files as the repo's prompt surface would make a
    pristine repo detect as `none` on the first run and `prompt_files` on the
    second, purely from the files adopt had just written. Read errors answer
    "not ours" — the conservative direction, where the worst case is counting
    a file once.
    """
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    # A SIGNED marker, or a pre-signature block of adopt's — never a bare
    # marker on its own, because a file quoting `<!-- aef:begin -->` in its
    # prose is the adopter's and counting it as adopt's output would drop
    # their own entry file out of the prompt-surface count.
    #
    # The legacy arm is what keeps ADR 0153's "the detection line is identical
    # on both runs" true ACROSS the signature change: without it, a repo
    # already adopted under the M2 marker format would have adopt's own entry
    # files stop looking like adopt's for exactly one run, and the detection
    # line would gain two signals and then lose them again.
    if not carries_adopt_block(text, MD_MARKERS):
        return False
    heading = next(
        (line for line in text.splitlines() if line.startswith("# ")),
        "",
    )
    if any(heading.rstrip().endswith(suffix) for suffix in _GENERATED_HEADINGS):
        return True
    # Offline entry files have no model-profile heading. Recognize only the
    # complete generated wrapper: owner instructions before or after our
    # block must still count as the owner's prompt surface.
    span = resolve_block_span(text, MD_MARKERS)
    if span is None or span[0] == "append":
        return False
    _, start, stop = span
    prefix = text[:start].strip()
    if text[stop:].strip():
        return False
    return prefix in {"", "---\nalwaysApply: true\n---"} or (
        "\n" not in prefix
        and prefix.startswith("# ")
        and prefix.endswith(" — AEF offline integration")
    )


def detect_prompt_surface(repo_root: Path) -> PromptSurface:
    """Counts the prompt-file agent surface: `.claude/agents/*.md`,
    `.claude/skills/*/SKILL.md`, `AGENTS.md`, `.codex/`,
    `.github/copilot-instructions.md`, `.cursor/rules/*`.

    Files `aef adopt` itself writes are excluded — the `/new-model-check`
    skill by path, and the entry files by their marker block — so running
    adopt twice reports the same numbers both times.

    **The agents and skills are discovered by `aef migrate`'s own functions**,
    not by a second glob here (ADR 0172). They disagreed: `migrate` recurses
    (`.claude/agents/**/*.md`, `.grok/agents/**/*.md`, measured against the Claude Code CLI in ADR
    0152) and adopt globbed one level, so a repo with a single nested persona
    had adopt say `8 agents` in the detection line, the checklist AND the
    appended block while `aef migrate` wrote 9 graphs. One discovery, one
    number — the exclusion of adopt's own output is applied on top.
    """
    agents = len(discover_prompt_agents(repo_root))

    ours = (repo_root / _ADOPT_SKILL_PATH).resolve()
    skills = len([p for p in discover_skills(repo_root) if (repo_root / p).resolve() != ours])

    agents_md = repo_root / "AGENTS.md"
    copilot = repo_root / ".github" / "copilot-instructions.md"
    cursor_dir = repo_root / ".cursor" / "rules"
    cursor = (
        [p for p in sorted(cursor_dir.iterdir()) if p.is_file() and not _is_adopt_generated(p)]
        if cursor_dir.is_dir()
        else []
    )
    return PromptSurface(
        agents=agents,
        skills=skills,
        has_agents_md=agents_md.is_file() and not _is_adopt_generated(agents_md),
        has_codex_dir=(repo_root / ".codex").is_dir(),
        has_copilot_instructions=copilot.is_file() and not _is_adopt_generated(copilot),
        cursor_rules=len(cursor),
    )


def first_prompt_module(repo_root: Path) -> str | None:
    """The dotted module `aef migrate` will write for this repo's FIRST prompt
    agent, or `None` when there are none.

    Derived from migrate's own discovery, sanitiser and output path — adopt
    computes no agent-name-to-module rule of its own, because two rules is how
    a generated workflow ends up naming a module nothing writes (ADR 0091).
    """
    sites = discover_prompt_agents(repo_root)
    if not sites:
        return None
    return sites[0].out_relative.removesuffix(".py").replace("/", ".")


def describe_detection(framework: Framework, surface: PromptSurface) -> str:
    """The one line `aef adopt` prints first. The counts are reported whatever
    the label is: a repo can have both call sites and prompt agents, and the
    adopter needs to know about both."""
    detail = surface.describe()
    if framework == "prompt_files":
        return f"{framework} ({detail})"
    if detail:
        return f"{framework} (+ prompt files: {detail})"
    return framework


def _detect_code_framework(repo_root: Path, *, max_files: int = 2000) -> Framework:
    """Scans `.py` files AND dependency manifests (requirements*.txt,
    pyproject.toml, Pipfile — anywhere in the tree, for monorepos with the
    framework declared in a subdirectory) under `repo_root` for the agent
    framework in use today. Detection order is deliberate: a repo using
    LangGraph or CrewAI almost always also imports openai/anthropic
    underneath, so those two are checked first and raw_sdk only matches
    when neither is present — across both signal sources combined, not
    just imports."""
    py_files, manifest_files = _framework_files(repo_root, max_files)

    code_text = _read_all(py_files)
    manifest_text = _read_all(manifest_files)

    if _LANGGRAPH_RE.search(code_text) or _LANGGRAPH_MANIFEST_RE.search(manifest_text):
        return "langgraph"
    if _CREWAI_RE.search(code_text) or _CREWAI_MANIFEST_RE.search(manifest_text):
        return "crewai"
    if _RAW_SDK_RE.search(code_text) or _RAW_SDK_MANIFEST_RE.search(manifest_text):
        return "raw_sdk"
    return "none"


def detect_framework(repo_root: Path, *, max_files: int = 2000) -> Framework:
    """The framework label, across both kinds of agent this scaffold meets.

    **Precedence: a code/manifest signal wins over prompt files, and the
    reason is what the label is FOR.** It selects the per-framework migration
    notes and the convert-your-call-sites half of the checklist, and a repo
    with real LangGraph/CrewAI/SDK call sites still needs those — the call
    sites are the thing `aef migrate` can route losslessly and the thing that
    makes a model call invisible to the harness if it does not. Nothing is
    lost the other way: `describe_detection` reports the prompt counts under
    every label, and the "run `aef migrate`, it registers your N prompt
    agents" checklist step is emitted whenever N > 0. Choosing prompt_files
    first would hide the framework notes entirely, which is a real loss;
    choosing the code label first hides nothing.
    """
    framework = _detect_code_framework(repo_root, max_files=max_files)
    if framework != "none":
        return framework
    return "prompt_files" if detect_prompt_surface(repo_root).total else "none"


_FRAMEWORK_MIGRATION_NOTES: dict[Framework, str] = {
    "langgraph": (
        "Detected LangGraph. Your `StateGraph` nodes map closely onto AEF `Node`s: "
        "the biggest mechanical change is that a LangGraph node closes over whatever "
        "clients/config it wants, while an AEF node receives everything through "
        "`Services` (constraint #2) and returns `(StateDelta, Route)` instead of "
        "mutating/returning a raw state dict. Your `MemorySaver`/`SqliteSaver` "
        "checkpointer becomes an `aef.kernel.DurabilityBackend`; your conditional "
        "edges become `aef.kernel.Edge` with an explicit `condition`."
    ),
    "crewai": (
        "Detected CrewAI. If you're using Crews (role-based, non-deterministic "
        "routing), each role becomes an AEF `Node` and the crew's implicit "
        "coordination becomes explicit `Edge`s — this is the main shift, since AEF's "
        "default is deterministic hierarchical handoff (blueprint §12.2), not "
        "emergent role delegation. If you're using Flows (already deterministic, "
        "event-driven), the mapping to AEF `Node`/`Edge` is close to 1:1."
    ),
    "raw_sdk": (
        "Detected direct OpenAI/Anthropic SDK calls with no orchestration "
        "framework. This is actually the simplest migration: wrap each existing "
        "call site in a `Node` function, move the vendor client construction "
        "behind a `ModelProvider` adapter (constraint #3 — vendor SDK imports "
        "only live in `providers/`), and inject it via `Services` instead of "
        "constructing it inline."
    ),
    "prompt_files": (
        "Detected prompt-file agents — `.claude/agents/*.md`, skills, `AGENTS.md`, "
        "`.codex/` — and no model-SDK call site anywhere. That is the ordinary "
        "shape for a repo whose agents are run by a coding-agent harness rather "
        "than by an SDK, and it is the shape this label exists for: `none` used "
        "to be reported here, which is the label for 'no orchestration code to "
        "migrate away from' and told you nothing about the eight agents you do "
        "have. There is no call site to convert. The runtime attaches at the "
        "MODEL layer instead: `aef migrate` registers each agent file as its own "
        "graph, and the node runs that prompt as the system prompt of one "
        "harness call — `model_provider.impl: claude_code` uses the coding "
        "agent's own login, so no API key is involved. The agent's PROMPT runs; "
        "the agent's TOOLS do not. That is the safety property, not a gap."
    ),
    "none": (
        "No agent framework, raw model-SDK usage, or prompt-file agents detected. "
        "Start from `aef adopt`'s generated `aef.yaml` and build your first `Node` "
        "directly against the AEF kernel — there's no existing orchestration code "
        "to migrate away from."
    ),
}


def render_aef_block_body(
    repo_name: str, framework: Framework, surface: PromptSurface | None = None
) -> str:
    """The section `aef adopt` maintains inside an entry file it did not
    write. Bounded on purpose: the adopter's `CLAUDE.md`/`AGENTS.md` is theirs,
    and the contract lives in `AGENT_INTEGRATION.md`, so this block is a
    pointer plus the rules an agent must not be able to miss."""
    surface = surface or PromptSurface()
    prompt_line = ""
    if surface.agents:
        # The AGENT COUNT only, never `surface.describe()`. The other signals
        # include files `aef adopt` writes, so a block quoting them says
        # something different on the second run and the "re-running replaces
        # the block with the same bytes" promise is broken. `.claude/agents/`
        # is a directory adopt never writes into.
        n = surface.agents
        prompt_line = (
            f"- **Your agents are prompt files** ({n} across native agent directories). "
            f"There is no call "
            f"site to convert: `aef migrate` registers each supported Markdown or "
            f"Codex TOML persona as its "
            f"own four-node graph at `{_PROMPT_AGENT_OUT_SHAPE}`, and the `prompt_agent` "
            f"node runs that persona as one model call. The persona's `tools:` frontmatter "
            f"is parsed, reported and never obeyed; what else the call may do is the "
            f"provider's answer, differs per `model_provider.impl`, and every run records "
            f'the one it got in `working_memory["prompt_agent__containment"]`.\n'
        )
    return f"""## AEF scaffold ({repo_name}) — generated section

This repo is adopting **aef-core**, a runtime for agent graphs with a gated
self-rewiring loop. **Everything outside the `aef:begin`/`aef:end` markers is
yours** — `aef adopt` did not rewrite a byte of it, and re-running replaces
only this block. Delete the block and adopt will append a fresh one; keep it
and adopt will update it in place.

- **Read `AGENT_INTEGRATION.md` first** (ingest-and-start), then
  `FIRST_DAY.md` (the command sequence, with the real output of each step),
  `LOOP.md` (the gates and the zones) and `AUTONOMY.md` (what may never be
  automated).
- **Zone A is `{DEFAULT_AGENT_ROOT}/`** — the only tree the loop may propose
  changes to. A candidate touching anything else is rejected by G0 and the
  cycle exits 1.
{prompt_line}- Node contract, non-negotiable:
  `(AEFState, Context, Services) -> tuple[StateDelta, Route]`. Everything
  arrives via `Services` — no globals, no env reads, no self-constructed
  clients.
- Two always-on invariants: **two-plane determinism** (the kernel is pure
  bookkeeping; every model/nondeterministic call lives in a node declared
  `deterministic=False`) and **vendor isolation** (`anthropic`/`openai`/
  `mem0`/`neo4j` imports only in `aef/providers/` and
  `aef/services/*/adapters/`).
- Green bar before any change is done: `pytest -q` · `mypy --strict <pkg>` ·
  `ruff check .` · `ruff format --check <dirs>`. Reproduce first: construct
  the failing case and RUN it before writing a fix.
- **HARD-STOP — ask a human** for: a push to another repo or any external
  publish; enabling `aef/evolution/`, weakening the deny-by-default
  `PolicyEngine`, or removing a HITL gate; deleting or overwriting a user
  file; a breaking public-contract change you are unsure of. Nothing
  auto-merges: a candidate passing all six gates is escalated, never merged.

{EVIDENCE_LEARNING_PROTOCOL}"""


def _wrap_in_markers(body: str, markers: tuple[str, str]) -> str:
    begin, end = markers
    return f"{signed_begin(begin, body)}\n{body}\n{end}"


def render_aef_block(
    repo_name: str,
    framework: Framework,
    surface: PromptSurface | None = None,
    *,
    markers: tuple[str, str] = MD_MARKERS,
) -> str:
    return _wrap_in_markers(render_aef_block_body(repo_name, framework, surface), markers)


def _legacy_block_span(text: str, markers: tuple[str, str]) -> tuple[int, int] | None | str:
    """Where a **pre-signature** block of adopt's own lives in `text`.

    Returns the `(start, stop)` slice to replace, `None` when there is no such
    block, or the string `"ambiguous"` when more than one bare pair looks like
    one — which is refused rather than guessed at.

    A bare pair is adopt's only when the first non-blank line of its body is a
    line `render_aef_block_body`/`render_gitignore_block` emits. That is the
    discriminator R2 needed: this scaffold's own kit teaches adopters the
    literal marker strings, so a balanced bare pair is far more often a
    quotation than a block, and the quotation never carries the heading.
    """
    begin, end = markers
    found: list[tuple[int, int]] = []
    at = text.find(begin)
    while at != -1:
        closing = text.find(end, at + len(begin))
        if closing == -1:
            break
        body = text[at + len(begin) : closing]
        head = next((line for line in body.splitlines() if line.strip()), "")
        if head.strip().startswith(_LEGACY_BODY_HEADS):
            found.append((at, closing + len(end)))
        at = text.find(begin, closing + len(end))
    if not found:
        return None
    if len(found) > 1:
        return "ambiguous"
    return found[0]


def apply_block(text: str, block: str, markers: tuple[str, str]) -> str | None:
    """Append `block` to `text`, or replace the block already there.

    Returns the new text, or **None** when the file carries markers this
    cannot safely resolve — the caller then skips the file and says why.
    Guessing where someone else's block ends is how a never-overwrite tool
    overwrites.

    **Only a pair whose begin marker carries a signature is adopt's**
    (ADR 0172). `<!-- aef:begin -->` written by anyone else — in a sentence,
    inside backticks, in a code fence teaching an adopter what the markers are
    — is inert prose, and a *balanced* bare pair used to be the one shape that
    silently destroyed the text between it. The three refusals are therefore
    about the SIGNED marker: two signed begins, a signed begin with no end,
    and a bare pair that could be one of two pre-signature blocks.

    The bytes outside the marker pair are never touched: on a replace they are
    the literal slices either side of it, and on a first append the file's
    existing bytes are a prefix of the result. The only thing added outside
    the markers is the separator that puts the block on its own line, and it
    is added once — a second run finds the markers and replaces between them.
    """
    resolved = resolve_block_span(text, markers)
    if resolved is None:
        return None
    _, start, stop = resolved
    return _assemble(text, block, start, stop, "\n")


def _assemble(text: str, block: str, start: int, stop: int, newline: str) -> str:
    """`text` with `[start:stop]` replaced by `block`, in `newline` endings.

    `text[:start]` and `text[stop:]` are pasted back **verbatim** — never
    re-rendered — so a file with mixed endings keeps every line exactly as its
    author left it, and only the bytes adopt is adding take the file's
    dominant ending.
    """
    rendered = block.replace("\n", newline) if newline != "\n" else block
    separator = ""
    if start == len(text) and text != "":
        if not text.endswith(newline):
            separator = newline * 2
        elif not text.endswith(newline * 2):
            separator = newline
    trailer = newline if start == len(text) else ""
    return text[:start] + separator + rendered + trailer + text[stop:]


# What `resolve_block_span` decided. `replace` and `migrate` both rewrite an
# existing span; they are distinguished because a `migrate` is worth telling
# the adopter about once, and a `replace` is the ordinary case.
BlockAction = str  # "append" | "replace" | "migrate"


def resolve_block_span(text: str, markers: tuple[str, str]) -> tuple[BlockAction, int, int] | None:
    """Which slice of `text` the block occupies, or `None` to refuse.

    `(action, start, stop)` — everything in `text[:start]` and `text[stop:]`
    survives verbatim, and that is the property `_verify_preserved` asserts in
    code before anything is written. On an append, `start == stop == len(text)`
    and the whole file is the prefix.
    """
    _, end = markers
    signed = list(_signed_begin_re(markers[0]).finditer(text))
    if len(signed) > 1:
        return None
    if signed:
        opening = signed[0]
        closing = text.find(end, opening.end())
        if closing == -1:
            return None
        return ("replace", opening.start(), closing + len(end))

    legacy = _legacy_block_span(text, markers)
    if legacy == "ambiguous":
        return None
    if isinstance(legacy, tuple):
        return ("migrate", legacy[0], legacy[1])
    return ("append", len(text), len(text))


def carries_adopt_block(text: str, markers: tuple[str, str]) -> bool:
    """Does this file already carry a block `aef adopt` wrote — signed, or in
    the pre-signature format?

    Not `markers[0] in text`: that reads an adopter's *quotation* of the
    marker as a block, which in `.gitignore`'s case would append the bytecode
    patterns to a file that already covers them (ADR 0142's no-nagging rule).
    """
    if _signed_begin_re(markers[0]).search(text):
        return True
    return _legacy_block_span(text, markers) is not None


def dominant_newline(data: bytes) -> bytes:
    """The line ending this file is written in: `b"\\r\\n"` or `b"\\n"`.

    Counted, not guessed, because a mixed file has to get *an* answer and the
    majority ending is the one that keeps the diff smallest. A file with no
    newline at all gets `b"\\n"` — nothing is being preserved either way, and
    LF is what every generated file here uses.

    This exists because `Path.read_text()` translates `\\r\\n` to `\\n` and
    `Path.write_text()` writes `os.linesep` back. Adopt's marker logic was
    byte-exact on the *translated* text, so on a CRLF `AGENTS.md` every line
    of the adopter's file was rewritten — `git diff --stat` said
    `41 insertions(+), 5 deletions(-)` while the CLI printed "your bytes
    outside it are unchanged" (ADR 0172).
    """
    crlf = data.count(b"\r\n")
    return b"\r\n" if crlf and crlf * 2 >= data.count(b"\n") else b"\n"


def _verify_preserved(prefix: bytes, suffix: bytes, result: bytes) -> bool:
    """The never-destroy rule, executable.

    ADR 0153 argued that appending inside markers is not overwriting because
    "every pre-existing byte survives verbatim". That was an argument in a
    document; on a CRLF file it was false. This is the same claim as an
    assertion that runs before every write: the bytes before the block and the
    bytes after it are still there, at the same ends of the file, and together
    they account for every byte of the original that is not the old block.
    """
    return (
        result.startswith(prefix)
        and result.endswith(suffix)
        and len(result) >= len(prefix) + len(suffix)
    )


@dataclass(frozen=True)
class BlockWrite:
    """What `apply_block_bytes` decided, for a caller that has to report it."""

    data: bytes
    action: BlockAction


def apply_block_bytes(data: bytes, block: str, markers: tuple[str, str]) -> BlockWrite | None:
    """`apply_block`, on bytes, in the file's own line ending.

    Three things happen here that the text version cannot do:

    1. the file is decoded **without newline translation**, so `\\r\\n` is
       still `\\r\\n` when the marker search runs and when the result is
       written back;
    2. the block is rendered in the file's dominant line ending, so the added
       lines match the ones around them;
    3. the result is checked against the original bytes before it is returned
       — a violation returns `None` (refuse) rather than a written file.

    Raises `UnicodeDecodeError` on a file that is not UTF-8 text; the caller
    already skips those and says so.
    """
    text = data.decode("utf-8")
    resolved = resolve_block_span(text, markers)
    if resolved is None:
        return None
    action, start, stop = resolved
    newline = dominant_newline(data).decode("ascii")
    out = _assemble(text, block, start, stop, newline).encode("utf-8")
    if not _verify_preserved(text[:start].encode("utf-8"), text[stop:].encode("utf-8"), out):
        return None
    return BlockWrite(data=out, action=action)


def render_claude_md(
    framework: Framework, repo_name: str, surface: PromptSurface | None = None
) -> str:
    surface = surface or PromptSurface()
    return f"""# {repo_name} — AEF scaffold contract

This repo is being migrated onto AEF (Agent Engineering Foundation), a
repo-agnostic Agent Operating System scaffold. This file is written so any
coding agent (Claude, Codex, Cursor, GitHub Copilot, …) with **no other
context** can pick up the migration. `AGENTS.md` is an identical copy for
harnesses that read that filename; Copilot/Cursor entry files
(`.github/copilot-instructions.md`, `.cursor/rules/aef.mdc`) point here.

## What AEF is

AEF is a Python runtime for agent graphs. Only five things are allowed to
differ per agent: **Knowledge, Policies, Tools, Objectives, Evaluation
Metrics.**

Inherited, audited against the code: graph execution, checkpoint/replay,
memory, context retrieval, evaluation, rule-based reflection, observability,
OTel telemetry, and a deny-by-default security policy with HITL gates.

NOT inherited, stated because an earlier version of this text claimed
otherwise: **planning** and **knowledge-graph access** do not exist (deleted,
ADR 0101); **token optimization** does not exist; LLM-backed **reflection**
and **optimization** are typed interfaces raising `NotImplementedError`.

And nothing here is inherited *automatically*. `aef adopt` wrote docs and a
stub; it read none of your code. `aef migrate --dir .` generates node wrappers
for your real call sites, but wiring and semantics remain yours.

## Where converted nodes have to live: `{DEFAULT_AGENT_ROOT}/`

`{DEFAULT_AGENT_ROOT}/**` is **Zone A** — the only tree the self-rewiring loop
may propose changes to. Everything else is Zone C (not agent-writable) or
Zone B (the harness: `corpus/`, `.github/workflows/`, the gates), and a diff
touching Zone B is treated as a security event rather than a rejected
proposal.

`aef migrate --dir .` writes its generated graph to
`{DEFAULT_MIGRATED_OUT}` — inside Zone A — and its report names the zone of
whatever path it wrote. It did not always: until aef-core ADR 0143 it wrote
`aef_migrated.py` to the repo **root**, which is Zone C, and said nothing.
Measured, not assumed: a candidate touching a root-level graph is rejected
with `G0 rejected it: candidate touches paths outside Zone A` and the cycle
exits 1, and `aef loop bless` will archive a Zone A tree that does not
contain your agent at all. So if you pass `--out`, or you have a graph left
over from an older `aef migrate`, put it under `{DEFAULT_AGENT_ROOT}/`
(e.g. `{DEFAULT_AGENT_ROOT}/<name>/graph.py`) before running the loop.

The other half of Zone A hygiene is the generated `.gitignore`: bytecode
committed under `{DEFAULT_AGENT_ROOT}/` is charged against G5's drift budget —
0.4675 of 0.500 for a one-line candidate in the run that measured it. If you
already had a `.gitignore`, `aef adopt` **appended** `__pycache__/` and
`*.py[cod]` inside a `# aef:begin` / `# aef:end` block, and only when neither
pattern was already there. Every byte you had is untouched and outside the
block; delete the block if you ignore bytecode another way (aef-core ADR 0153).

## The node contract (non-negotiable)

Every node has this fixed signature:

    (AEFState, Context, Services) -> tuple[StateDelta, Route]

Nodes never construct their own clients, never read env vars, never reach
for globals. Everything arrives via `Services` (dependency injection).
See the `aef-core` package's `aef/kernel/contracts.py` for the exact types.

## Detected framework in this repo: `{describe_detection(framework, surface)}`

{_FRAMEWORK_MIGRATION_NOTES[framework]}

## Migration checklist

See `AEF_MIGRATION_CHECKLIST.md` (generated alongside this file) for the
prioritized, ordered list of concrete next steps, and **`FIRST_DAY.md`** for
the command sequence that follows it — `aef migrate` through `aef loop cycle`,
in order, with the real output of every step and what each one costs you.

## Config

`aef.yaml` (generated alongside this file) stubs the five per-agent fields.
Fill in `objectives`, `tools.allow`, `policies`, and `evaluator.suites`
before running anything against real credentials — the scaffold defaults
to `require_hitl_above_risk: 0.0`, meaning **any** positive-risk tool call
requires human approval until you explicitly raise that threshold.

## How work is verified here

**Reproduce first**: construct the failing case and RUN it before writing a
fix. The **green bar** — tests, type-check, lint — passes before every commit.
`AUTONOMY.md` carries the full contract; this line is here so a session that
reads only this file still has it.

## Where to look in aef-core

These paths are inside the `aef-core` package/repository, not this one — if
`aef-core` was installed via pip, find its on-disk location with
`python -c "import aef; print(aef.__path__[0])"`; `docs/` is only present
if you have the `aef-core` source checked out (it isn't bundled into the
pip package), so `docs/roadmap.md` below requires that checkout.

- `aef/kernel/` — graph engine, Node/Edge contracts, Services, checkpointing, replay
- `aef/state/` — the shared `AEFState` schema every agent uses
- `aef/providers/`, `aef/services/*/` — pluggable backends behind stable interfaces
- `aef/security/tool.py` — the policy engine every tool call goes through
- `docs/roadmap.md` (source checkout only) — what's implemented vs. stubbed, phase by phase

{render_aef_block(repo_name, framework, surface)}
"""


# The bytecode patterns, and the two families that each independently keep
# `.pyc` out of the tree. `__pycache__/` alone is sufficient on CPython 3
# (every `.pyc` lives in one) and so is `*.pyc`; an adopter who already has
# either is not nagged for the other. Written out as literal forms rather
# than matched with a glob engine because `.gitignore` semantics are git's,
# not `fnmatch`'s, and a half-right matcher that reports a false gap costs
# more trust than it saves.
_BYTECODE_DIR_FORMS = frozenset(
    {"__pycache__/", "__pycache__", "__pycache__/*", "__pycache__/**", "**/__pycache__/"}
)
_BYTECODE_FILE_FORMS = frozenset({"*.pyc", "*.py[cod]", "*.py[co]", "**/*.pyc", "*$py.class"})


def render_gitignore_block() -> str:
    """The two bytecode patterns, inside `# aef:begin`/`# aef:end`.

    The same bytes whether they are written into a fresh `.gitignore` or
    appended to one the adopter already had — one string, so the two paths
    cannot drift and a re-run of adopt on either is a byte-for-byte no-op.
    """
    return _wrap_in_markers("__pycache__/\n*.py[cod]", GITIGNORE_MARKERS)


_GITIGNORE_BODY = f"""# Generated by `aef adopt`. Add your own entries below; this file is written
# once, and only the `# aef:begin` / `# aef:end` block below is ever rewritten.
#
# The first two patterns are load-bearing for the self-rewiring loop, and the
# reason is measured rather than stylistic (ADR 0142). Bytecode committed
# under `{DEFAULT_AGENT_ROOT}/` — Zone A, the only tree the loop may propose
# changes to — is charged as drift by G5, because the baseline
# `aef loop bless` archived does not contain it. One `git add -A` before
# blessing put a ONE-LINE candidate at 0.4675 of the 0.500 drift budget,
# against 0.0238 for the same candidate with the bytecode excluded: 35 of the
# 36 differing lines were `.pyc`. Two consecutive drift rejections halt the
# loop, so that is two candidates from a halt caused by nothing your agent did.
{render_gitignore_block()}
*.so

# Tool caches. Not drift (they land outside `{DEFAULT_AGENT_ROOT}/`), just noise.
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/

# Virtualenvs.
.venv/
venv/

# NOT ignored, deliberately: `corpus/` and `.github/workflows/` are Zone B —
# the evidence and the gates. The loop reads them from git, so an ignored
# corpus is an empty one.
"""


def render_gitignore() -> str:
    return _GITIGNORE_BODY


# The directory is named as a RULE, not as a path. `aef adopt` runs BEFORE
# `aef migrate`, and `aef migrate --agent-root` is what decides where Zone A
# is, so adopt cannot know it — it can only say which directory the rule
# applies to. Naming `agents/` outright (which this string did, interpolated
# from `DEFAULT_AGENT_ROOT`) told an operator who widened the root about the
# wrong directory: reproduced on the pilot clone, where `.gitignore` covers no
# bytecode, one generated module was compiled, and `git add -A` tracked
# `.claude/agents/migrated/marlin_accela/__pycache__/graph.cpython-313.pyc` —
# Zone A content under a root this sentence did not mention (ADR 0168).
_DRIFT_COST = (
    f"Committed bytecode under your AGENT ROOT is Zone A content the loop never wrote, and "
    f"G5 charges it as drift: measured 0.4675 of a 0.500 budget for a one-line candidate, "
    f"against 0.0238 with the bytecode excluded (ADR 0142). The agent root is `"
    f"{DEFAULT_AGENT_ROOT}/` by default and whatever you pass to `aef migrate --agent-root` "
    f"otherwise (`.claude/agents/` for a prompt-file repo) — `aef adopt` runs before "
    f"`aef migrate` and cannot know which you will choose, so both patterns are "
    f"repo-wide and cover either."
)


def gitignore_covers_bytecode(text: str) -> bool:
    """Does this `.gitignore` already keep `.pyc` out of the tree?

    Comments are not rules — a naive `"__pycache__" in text` reads
    `# __pycache__/` as coverage, the detector failing in exactly the
    direction that costs the adopter a drift budget (ADR 0142).
    """
    lines = {
        line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")
    }
    return bool(lines & _BYTECODE_DIR_FORMS or lines & _BYTECODE_FILE_FORMS)


def gitignore_gaps(text: str) -> tuple[str, ...]:
    """What an existing `.gitignore` is missing, in the adopter's words —
    the FALLBACK message, for when `aef adopt` could not append the block
    itself (a symlink, a non-text file, markers it refuses to resolve).

    ADR 0142 used this for every existing `.gitignore`, because appending was
    read as overwriting-by-another-route. ADR 0153 separates the two: writing
    inside `# aef:begin`/`# aef:end` leaves every pre-existing byte untouched
    and is reversible by deleting the block, so it is not an overwrite — and
    a checklist line is the weakest control available for a cost measured at
    93.5% of a drift budget.
    """
    if gitignore_covers_bytecode(text):
        return ()
    return (
        f"Add `__pycache__/` and `*.py[cod]` to your existing `.gitignore` — `aef adopt` "
        f"could not append them for you and neither pattern is in it. {_DRIFT_COST}",
    )


def legacy_block_upgraded_note(paths: tuple[str, ...]) -> str:
    """The printed notice for a block written before the marker signature.

    A checklist item, past tense, exactly like `gitignore_appended_note()` —
    it reports what the tool did so the adopter can go and look, and it
    reaches `AEF_MIGRATION_CHECKLIST.md` on disk as well as the terminal.

    Migrating is the alternative to refusing, and this is why: the only other
    option was to treat an unsigned pair as prose, which would leave the old
    block in place and append a second one — two contradicting copies of the
    contract in the file the repo's agents read. The upgrade is done ONCE
    (the replacement is signed), it is announced, and it is reversible by
    deleting the block. It is applied only where the block's first line is
    one `aef adopt` itself emits, so a quotation is never mistaken for a
    block.
    """
    named = ", ".join(f"`{p}`" for p in paths)
    return (
        f"`aef adopt` upgraded a pre-signature `aef:begin`/`aef:end` block in {named} to the "
        f"signed form (`aef:begin sha256=...`). Only a SIGNED pair is adopt's now: a bare pair "
        f"in your own prose — this kit teaches the marker strings, so quoting them is ordinary "
        f"— is left alone instead of having everything between it replaced (aef-core ADR 0172). "
        f"Nothing outside the block moved; check the diff if you want to see that."
    )


def gitignore_appended_note() -> str:
    """What the checklist says once the block HAS been appended. Past tense on
    purpose: it reports what the tool did, so an adopter can go and look at it,
    rather than asking them to do something already done."""
    return (
        f"`aef adopt` appended `__pycache__/` and `*.py[cod]` to your existing `.gitignore`, "
        f"inside a `# aef:begin` / `# aef:end` block — every byte you had is untouched and "
        f"outside it, and re-running adopt replaces only that block. Delete the block if you "
        f"ignore bytecode another way. {_DRIFT_COST}"
    )


def prompt_agent_checklist_item(agents: int) -> str:
    """The step that replaces "convert your call sites" when the repo's agents
    are prompt files. There is nothing to convert; there is something to
    register."""
    count = f"{agents} prompt agent{'s' if agents != 1 else ''}" if agents else "your prompt agents"
    return (
        f"Run `aef migrate --dir .` — it registers {count} (`.claude/agents/**/*.md`, "
        f"`.grok/agents/**/*.md`, "
        f"`.codex/agents/**/*.toml`) as graphs, one four-node graph per agent at "
        f"`{_PROMPT_AGENT_OUT_SHAPE}` (`retrieve -> prompt_agent -> reflect -> consolidate "
        f"-> END`; the graph ID is the collision-safe agent name), inside Zone A. There is no "
        f"call site to convert: each `prompt_agent` node runs that agent's persona as one "
        f"model call, reading the file at execution time. The persona's `tools:` frontmatter "
        f"is parsed, reported and never obeyed — but WHAT ELSE the call may do is the "
        f"provider's answer and not migrate's, it differs per `model_provider.impl`, and "
        f"migrate's own report prints the measured table (aef-core ADR 0169). NOTE which "
        f"file the loop may then edit: the GRAPH is Zone A, the PERSONA file is Zone C by "
        f"default, so a candidate editing the prompt itself is rejected until you widen the "
        f"agent root — `aef migrate --agent-root ...` is opt-in per repo and its report says "
        f"what that adds to the loop's blast radius."
    )


#: What step 1 says when adopt could put its block in NO entry file at all.
#: There is nothing to read, so the step is the fix rather than an instruction
#: to open a file that does not carry the contract.
_NO_ENTRY_FILE_STEP = (
    "aef adopt could put its contract in NO entry file ({reason}) — so nothing below "
    "reached a file your coding agent reads. Fix that first: clear the obstruction and "
    "re-run `aef adopt`, or copy the `aef:begin`/`aef:end` block out of "
    "`.github/copilot-instructions.md`, which did get one, into a real CLAUDE.md or "
    "AGENTS.md by hand."
)


def entry_file_checklist_item(entry_files: Sequence[str], reason: str = "") -> str:
    """Step 1, named from the file(s) adopt ACTUALLY wrote or appended to.

    It used to be the literal `Read the generated CLAUDE.md`, unconditionally.
    `aef adopt` skips `CLAUDE.md` whenever it is a symlink — datamining's
    shape, and the right call, because `CLAUDE.md` and `AGENTS.md` there
    resolve to one inode and appending to both would put two blocks in one
    file. On datamining the link happens to point at `AGENTS.md`, which did
    get the block, so step 1 accidentally worked; pointed at `README.md` it
    sends the adopter to a file with no aef content at all (reproduced,
    F-M8-3, ADR 0187). `CLAUDE.md`'s own promise — "self-contained: a fresh
    coding-agent session in that other repo can pick up the migration from it
    alone" — is false for a file adopt never touched.

    So it is derived from the same `written`/`appended`/`skipped` result the
    report prints, at the one place that knows all three.
    """
    if not entry_files:
        return _NO_ENTRY_FILE_STEP.format(reason=reason or "every candidate was skipped")
    named = entry_files[0] if len(entry_files) == 1 else " and ".join(entry_files)
    plural = "" if len(entry_files) == 1 else " (owner instructions may differ)"
    return f"Read the generated {named}{plural} in full before writing any code."


def render_migration_checklist(
    framework: Framework,
    surface: PromptSurface | None = None,
    *,
    entry_files: Sequence[str] = ("CLAUDE.md",),
    entry_skip_reason: str = "",
) -> list[str]:
    common = [
        entry_file_checklist_item(entry_files, entry_skip_reason),
        "Fill in aef.yaml: objectives, tools.allow, policies, evaluator.suites.",
        (
            (
                "Define your first agent objective and implement its first graph; "
                "no existing agent was detected."
            )
            if framework == "none" and not (surface and surface.agents)
            else (
                "Identify your current entrypoint(s) or native prompt definitions "
                "that start an agent run."
            )
        ),
        # Derived from DEFAULT_AGENT_ROOT rather than spelled out, so a repo
        # that moves its agent root cannot be told the wrong directory. The
        # output path is migrate's default for a call site, and the
        # one-graph-per-agent shape when the repo's agents are prompt files —
        # both inside Zone A, which is what this item is about.
        f"Put every node you convert under `{DEFAULT_AGENT_ROOT}/` — Zone A, the only tree "
        f"the loop is allowed to propose changes to. `aef migrate` writes its generated "
        f"graph to "
        f"`{_PROMPT_AGENT_OUT_SHAPE if surface and surface.agents else DEFAULT_MIGRATED_OUT}`"
        f", which is inside Zone A, and names the zone of "
        f"the path in its report; anywhere else is Zone C and, measured, a candidate "
        f"touching it is rejected with `G0 rejected it: candidate touches paths outside "
        f"Zone A` and the cycle exits 1 (ADR 0142, ADR 0143).",
    ]
    by_framework: dict[Framework, list[str]] = {
        "langgraph": [
            "List your StateGraph's nodes; give each one a stable `id` and `version`.",
            "For each node, decide side_effects (pure/io/external_call/mutating) and "
            "deterministic (true only if identical input always produces identical output).",
            "Replace direct client construction (OpenAI/Anthropic clients, DB "
            "connections) inside nodes with a `Services` parameter.",
            "Convert conditional edges to `aef.kernel.Edge` with an explicit `condition`.",
            "Replace your checkpointer with an `aef.kernel.DurabilityBackend` "
            "(start with `InMemoryDurabilityBackend` or `FileDurabilityBackend`).",
            "Wire the aef_adapter.py shim's `run_via_aef()` to your old entrypoint "
            "and run both side by side until outputs match.",
        ],
        "crewai": [
            "List your Crew's roles/tasks or Flow's steps as candidate AEF Nodes.",
            "If using Crews: decide which handoffs should become deterministic "
            "Edges vs. remain an explicitly-logged emergent-routing exception.",
            "If using Flows: map each `@start`/`@listen` step to a Node/Edge pair roughly 1:1.",
            "Move any tool definitions behind `aef.security.tool.Tool` with "
            "declared scopes — CrewAI tools have no default-deny policy engine.",
            "Wire the aef_adapter.py shim to your old entrypoint and compare outputs.",
        ],
        "raw_sdk": [
            "Wrap your vendor client construction in an "
            "`aef.providers.base.ModelProvider` adapter.",
            "Convert each call site into a Node function receiving Services.model_provider.",
            "Add a second provider + FallbackProvider if you want vendor fallback.",
            "Wire the aef_adapter.py shim to your old entrypoint and compare outputs.",
        ],
        # The convert-your-call-sites half, for a repo that has no call sites.
        # `aef migrate` is the step, and what it produces is one graph per
        # agent file rather than one node per call site.
        "prompt_files": [
            prompt_agent_checklist_item(surface.agents if surface else 0),
            "Read `.claude/agents/*.md` and decide WHICH agents the loop should improve — "
            "one graph per agent means one loop target per agent, each with its own corpus, "
            "baseline and drift budget.",
            "Prove one harness call before the loop depends on it: `aef run "
            '<the generated module> --objective "..." --config aef.yaml --checkpoints-dir '
            ".aef-runs`. `model_provider.impl: claude_code` needs no API key.",
        ],
        "none": [
            "Write your first Node directly against aef.kernel — no legacy code to migrate.",
            "Start with a two-node graph (do-the-thing -> END) and grow it.",
        ],
    }
    tail = [
        "Add an Evaluator (start with aef.services.eval.rule_based.RuleBasedEvaluator).",
        "Run `aef doctor` to confirm the config and imports are wired correctly.",
        # The checklist used to end here, at a repo that can run one agent and
        # nothing else. FIRST_DAY.md is the rest of the day, in order, with
        # the real output of every command in it (ADR 0148).
        "Then read `FIRST_DAY.md` and run the sequence it documents: `aef migrate` -> "
        "`aef loop bootstrap --state <dir> --memory <file>` -> the tripwire line "
        "bootstrap prints -> `aef loop bless` -> `aef loop doctor` -> `aef loop cycle`. "
        "It is the only document that says what each step costs you and which failures "
        "exit 0 having done nothing.",
    ]
    steps = common + by_framework[framework] + tail
    # A repo can have BOTH call sites and prompt agents — the framework label
    # picks which migration notes apply (see `detect_framework`), it does not
    # decide which agents exist. So the prompt-agent step is emitted under
    # every label once there is a prompt agent to register.
    if surface and surface.agents and framework != "prompt_files":
        steps.insert(len(common), prompt_agent_checklist_item(surface.agents))
    return steps


_ADAPTER_SHIM_TEMPLATE = '''"""AEF adapter shim — generated by `aef adopt`.

Routes this repo's existing agent entrypoint through the AEF kernel without
requiring a rewrite. Replace the TODOs below with your actual entrypoint.
"""

from __future__ import annotations

from aef.kernel import END, Context, Graph, GraphExecutor, Node, Route, Services
from aef.services.runtime import agent_services
from aef.state import AEFState, StateDelta


def legacy_entrypoint_node(
    state: AEFState, ctx: Context, services: Services
) -> tuple[StateDelta, Route]:
    """TODO: call your existing entrypoint here. It currently detected as:
    framework = "{framework}"
    See AEF_MIGRATION_CHECKLIST.md for the per-framework migration steps.
    """
    # Returning END keeps this file lint-clean and importable before you
    # wire it — `aef doctor` imports it, and a template that fails its own
    # repo's `ruff check .` is a poor first impression (ADR 0079). Replace
    # the body; do not leave it returning END.
    raise NotImplementedError("wire your existing entrypoint into this node")
    return StateDelta(), END  # unreachable; keeps the imports honest


def build_graph() -> Graph:
    node = Node(
        id="legacy_entrypoint", version="0.1.0", fn=legacy_entrypoint_node, deterministic=False
    )
    return Graph(
        id="{repo_name}",
        version="0.1.0",
        nodes={{"legacy_entrypoint": node}},
        edges=[],
        entry_node="legacy_entrypoint",
    )


def run_via_aef(objective: str, *, services: Services | None = None) -> AEFState:
    import uuid

    state = AEFState(run_id=str(uuid.uuid4()), agent_id="{repo_name}", objective=objective)
    # `agent_services()`, NOT a bare `Services()`. A bare container configures
    # nothing, so the first node that calls `services.require_critic()` raises
    # `ServiceNotConfiguredError` — and the graph `aef migrate` generates is
    # wired `<call site> -> reflect -> consolidate -> END`, whose reflect node
    # requires critic/judge/memory and whose consolidate node requires
    # knowledge. So the documented next step (point this shim at your migrated
    # graph) raised on a bare container. `agent_services()` supplies working
    # defaults for all of them and is the same factory `aef run`, `aef loop
    # bootstrap` and every gate re-execution use, so this path and those
    # cannot silently diverge. `model_provider` stays None by default: pass
    # `services=` when your nodes call a model.
    executor = GraphExecutor(build_graph().compile(), services or agent_services())
    result = executor.run(state)
    return result.final_state
'''


def render_adapter_shim(framework: Framework, repo_name: str) -> str:
    return _ADAPTER_SHIM_TEMPLATE.format(framework=framework, repo_name=repo_name)


def render_aef_yaml(repo_name: str) -> str:
    return f"""# Generated by `aef adopt` for {repo_name}.
# Fill in the five fields allowed to differ per agent: objectives,
# policies, tools.allow, evaluator.suites, and memory/knowledge_graph.
#
# WHAT IS WIRED TODAY: `model_provider`, `policies`, `tools.allow`,
# `objectives` and `evaluator.suites`.
#   - `tools.allow` is a list of SCOPES, not tool names. The policy engine
#     gates on a tool's required_scopes, so an allowlist of names could not
#     authorise anything. Names are the DENY axis: `policies.forbid`.
#   - Empty `tools.allow` allows nothing. That is deny-by-default, on purpose.
#   - Pass `--config aef.yaml` to `aef run`, and `--config` to `aef loop
#     gate`/`cycle`/`bootstrap`, or the engine falls back to its own
#     deny-by-default. On `bootstrap` it is also what lets a model-calling
#     graph record its first corpus (aef-core ADR 0145).
#     The gate reads this file FROM THE BASE REF, so editing it on a
#     candidate branch cannot widen the rules that candidate is judged by.
#   - `objectives` is the default objective for `aef run --config`; an
#     explicit `--objective` still overrides it.
#   - `evaluator.suites` are DOMAIN GATES, applied by `aef run --observations`
#     and `aef eval --config`. Each entry is a `module:function` reference to
#     a callable taking the run's AEFState and returning bool. A gate can only
#     make a run FAIL that would otherwise pass; it can never rescue one.
#
# REFUSED, so it cannot be believed by mistake:
#   - `knowledge_graph` — no builder exists, so the block raises at load time
#     rather than validating a claim nothing honours (aef-core ADR 0100).
#   - `extends` — nothing resolves a base config, so any value but the
#     default `_base` is rejected (aef-core ADR 0084).

extends: _base

model_provider:
  # the coding agent's own login, no API key; or codex / grok / anthropic /
  # command — any CLI, from an argv template in a `command:` block (ADR 0154).
  # GitHub Copilot's CLI is `command`, configured by you when you install it:
  # this repo ships no guess about its flags (ADR 0150).
  impl: claude_code
  model: claude-opus-5-5  # a real current ID; claude-fable-5-1 for the hardest long-horizon work
  fallback: []
  # effort: high  # optional, claude_code/anthropic; unset = model default (5.5: medium)

memory:
  impl: in_memory

evaluator:
  suites: []

tools:
  allow: []

policies:
  require_hitl_above_risk: 0.0
  forbid: []

# How a SHADOW run of a loop candidate is contained (aef-core ADR 0161).
# Commented out because `auto` is already the default — uncomment only to say
# something different, and note that `auto` REFUSES rather than downgrading
# when no container runtime or no image is available. `image` has no default:
# it must carry your own `aef` and its dependencies.
# shadow:
#   containment: auto  # auto | fallback | off — ADR 0161
#   image: null        # required by `auto`; `fallback`/`off` are owner choices

# Whether the GATES may make live model calls (aef-core ADR 0181). Off, and
# stated rather than left absent, because turning it on is a real grant: the
# gates' sandbox worker is the one process that executes code an AGENT wrote,
# and with this true it inherits your harness login — so a candidate's code
# can spend your quota. Read FROM THE BASE REF, so a candidate cannot switch
# it on in its own branch. You need it to gate a prompt candidate at all: a
# changed prompt is a changed cassette key, so `aef loop cycle
# --cassette-miss live` is the only honest way to score one, and it is
# REFUSED by name while this is false rather than failing every scenario and
# reporting that as a verdict. Every `gated` ledger event records the value.
gates:
  live_model_calls: false

objectives: "TODO: describe this agent's objective in one or two sentences."

evolution:
  enabled: false
"""


def render_agent_integration_md(repo_name: str) -> str:
    return f"""# AEF Agent Integration — ingest & start here ({repo_name})

You are a coding agent (Claude, Codex, Cursor, GitHub Copilot, or any other)
in a repo adopting **aef-core**, a repo-agnostic Agent Operating System
scaffold. This file is self-contained:
read it top to bottom and you can install aef-core, wire your first node, and
safely run the self-improving loop with no other context.

**If you want the sequence rather than the reference, read `FIRST_DAY.md`
first** (generated alongside this file). It runs `adopt` -> `migrate` ->
`bootstrap` -> `bless` -> `doctor` -> `cycle` in order, with the real output
of every command, and it names the two `aef loop bootstrap` flags nothing
else here mentions.

{EVIDENCE_LEARNING_PROTOCOL}

## What you inherit (and what you don't)
aef-core gives every agent, for free: a deterministic graph kernel, shared
`AEFState`, checkpoint/replay durability, a deny-by-default security policy +
HITL gates, memory, OTel tracing, and an eval harness. **Only five things
differ per agent:** Knowledge, Policies, Tools, Objectives, Evaluation
Metrics. An agent-specific branch anywhere else means the abstraction is
wrong, not the agent.

Two always-on invariants:
- **Two-plane determinism.** The kernel is pure bookkeeping. Every
  LLM/nondeterministic call lives inside a node declared
  `deterministic=False`. Never call a model from the kernel.
- **Vendor isolation.** `anthropic`/`openai`/`mem0`/`neo4j` imports live ONLY
  in `aef/providers/` and `aef/services/*/adapters/`. CI fails otherwise.

## Start (5 steps)
1. `pip install aef-core` — then `pip install -e .` too, if this repo is
   itself installable. (Add the `anthropic`/`mem0` extras only when wiring
   those backends; the base package imports without them.)
2. `aef doctor` — confirms Python >=3.11, CLAUDE.md present, aef.yaml valid.
   Fix any `[FAIL]`; `[WARN]` advisories are optional.
3. Fill `aef.yaml`: objectives, tools.allow, policies, evaluator.suites.
4. Write one node with the fixed signature and wire it in `aef_adapter.py`:
   `(AEFState, Context, Services) -> tuple[StateDelta, Route]`. Nodes take
   everything via `Services` (dependency injection) — no globals, no env
   reads, no self-constructed clients.
5. Execute, score, and replay. **`--checkpoints-dir` is required on the
   run**, or the run is held in memory and discarded — `aef eval` then
   reports "no checkpoints found for run_id=..." and blames the run id
   rather than the missing flag:

   ```
   aef run <your.module> --objective "..." --checkpoints-dir .aef-runs
   aef eval  --checkpoints-dir .aef-runs --run-id <the id printed above>
   aef trace --checkpoints-dir .aef-runs --run-id <the id printed above>
   ```

   `aef trace` prints provenance, and provenance only exists if your nodes
   emit it: `StateDelta(provenance=[Provenance(...)])`. A graph that emits
   none replays empty and scores `cost_tokens=0`.

## The node contract (non-negotiable)
- Declare `deterministic: bool`. `True` means the replay engine WILL
  re-execute it and assert identical output — never declare it on anything
  that calls a model, clock, or RNG.
- Declare `side_effects` (`pure`/`io`/`external_call`/`mutating`). Anything
  non-pure REQUIRES an `idempotency_key_fn` (enforced by the `Node`
  constructor). Resume is at-least-once — your key is what makes a
  re-executed side effect safe; the kernel does not dedupe for you.

## Running the self-improving loop (autonomously, safely)
Verification, in one line so a session that reads only this file has it:
**reproduce first** — construct the failing case and RUN it before writing a
fix — and the green bar (tests, type-check, lint) passes before every commit.
See `AUTONOMY.md` (generated alongside this file) for the safety contract,
and aef-core's `docs/autonomy/self-improving-loop.md` for the full spec.
The loop in one line: **audit by adversarial construction -> reproduce
failing -> fix -> verify -> ADR -> commit -> repeat until a bounded work-list
is done.** Green bar every step:

    pytest -q                              # exit 5 = no tests collected, not a pass
    mypy --strict <your package>
    ruff check .
    ruff format --check <your source dirs>

A freshly adopted repo has no tests, so `pytest -q` exits 5 on day one. That
is the bar telling you the truth: write the first test before you rely on it,
and note the same code fails G1 if you pass `pytest -q` as `--build-command`.

"Self-learning" means writing reflections into memory (rule-based
critic/judge first). It does NOT mean self-modification: `aef/evolution/` is
gated by design and stays off.

## Guardrails you cannot route around
- Security is deny-by-default: a tool with no declared scopes is denied; any
  positive-risk call routes to REQUIRE_HITL until explicitly approved.
- Every state change is a `StateDelta`; state is append-mostly; `plan`
  REPLACES on set. Scores/tokens/budgets are range-validated. Durability
  writes are atomic and resume recovers past a torn checkpoint; HITL pauses
  are always resumable.

## Getting the loop's obligations green — run this as your task

`aef loop doctor` reports **six** obligations and prints the exact command
that fixes each. Work down its output.

**They are ADVISORY, not gating** (aef-core ADR 0141), and an earlier version
of this text said otherwise. `aef loop doctor` is the only thing that reads
them: `aef loop cycle` and `aef loop gate` run whatever it says, printing the
unmet ones first. Three enforce themselves later and correctly — G2/G3 refuse
an empty corpus, G5 refuses without a blessed baseline, and a graph nothing
routes to reflect records no failure memory, so the proposer never proposes —
and the other three (observations, halt channel, model calls visible) stop
nothing at all, which is exactly why they are listed. Obligation 3 cannot be
green on day one: it needs production runs you have not made yet. Do not wait
for six before running a cycle.

```
aef loop doctor --repo . --state ~/.aef-loop-state --corpus corpus \\
                --agent-path agents/<yours>/graph.py
```

1. **CORPUS + TRIPWIRE.**
   ```
   aef loop record <your.module> --corpus corpus --scenario-id s1 \\
     --objective "..." --split validation \\
     --working-memory '{{"difficulty": 5}}'
   ```
   Record scenarios that **fail** as well as ones that pass — a corpus where
   everything already passes cannot demonstrate an improvement.
   Then at least one tripwire:
   ```
   ... --scenario-id tripwire-1 --expected must_fail \\
       --working-memory '{{"difficulty": 99}}'
   ```
   It must be impossible **in principle**, not merely hard; `record` refuses
   the label if the agent completes the task. Without a tripwire the gates
   cannot detect reward hacking — a one-line change making an agent always
   report success passes every cheap gate, because they read the agent's own
   claim about itself.

2. **REFLECT NODE, ROUTED TO.** You need BOTH an edge and a route:
   ```python
   return delta, "reflect"                            # in your work node
   edges=[Edge(from_node="work", to_node="reflect")]  # in build_graph
   ```
   An Edge alone does not route; a route with no edge is refused by the
   executor. This catches everyone once.

3. **OBSERVATIONS.** Pass `--observations` from production runs. With no
   input every monitoring window reports unobserved, which correctly rolls
   every change back — monitoring with no input is an expensive way to revert.

4. **HALT CHANNEL.** A halt fails a CI job. If nobody watches that, nothing
   has told you.

5. **BLESSED BASELINE.** Commit first; the baseline is read from git.
   ```
   aef loop bless --repo . --state ~/.aef-loop-state \\
                  --agent-path agents/<yours>/graph.py
   ```
   A baseline is the whole Zone A tree as committed, and `bless` refuses when
   `--agent-path` is not inside it rather than reporting a baseline that does
   not contain your agent (aef-core ADR 0147).

6. **MODEL CALLS VISIBLE.** The only obligation you cannot discover by being
   stuck: a node that builds its own `anthropic.Anthropic()` runs fine and
   `aef doctor` is green, and the bill arrives at gate time. A call the
   harness never saw is not policy-checked, not covered by the fallback
   chain, and captures no `RecordedCall` — so the gates replay nothing and
   either reach your vendor live or score the candidate 0. `aef migrate`
   routes what it can route losslessly; where it refuses, `aef loop doctor`
   names the two hand edits (route the body, AND delete the import that keeps
   your module in the reachable set). `aef migrate --force` does not fix it
   and loops. See `FIRST_DAY.md` section 2.

### Then verify by RUNNING, not by reading

```
aef loop gate --repo . --state ~/.aef-loop-state --head <branch> \\
   --workdir "$(mktemp -d)/run" --corpus corpus \\
   --entrypoint <your.module>:build_graph \\
   --build-command "<your green bar>"
```

`--entrypoint` is **required** or G2 and G3 cannot execute your corpus and
refuse. `--build-command` is **your** green bar, not aef-core's; the default
`pytest -q` exits 5 in a repo with no tests and fails G1.

Confirm all six gates actually ran — read the ledger, not the summary
(`aef loop status` and the ledger's `gated` entry both show which gates ran).

Then **prove the containment works**: plant a one-line reward hack — make the
agent ignore its inputs and always report success — gate it, and confirm G2
rejects it as a SECURITY EVENT and the loop HALTS with exit 2. If it does
not, your tripwire is not a tripwire. Revert the hack afterwards.

Scheduled workflows are optional. If wanted, review their schedules and run
`aef adopt --dir . --profile model --with-workflows`, then edit
`.github/workflows/loop-gate.yml` and set `AEF_ENTRYPOINT` and
`AEF_BUILD_COMMAND` to your values. The generated ones are placeholders.

### Know what is and is not wired
`model_provider`, `policies` and `tools.allow` reach a run — but only when you
pass `--config aef.yaml` to `aef run`, and `--config` to `aef loop
gate`/`cycle`/`bootstrap`. Without the flag the engine falls back to its own
deny-by-default, which denies every tool call. On `bootstrap` it is also the
only way a model-calling graph can record its first corpus at all: the
cassette the gates replay from does not exist until something makes the call
once (aef-core ADR 0145).

`tools.allow` is a list of **scopes**, not tool names; `policies.forbid` is
the name-based deny axis. An empty `tools.allow` allows nothing, on purpose.

`objectives` and `evaluator.suites` now reach a run (aef-core ADR 0100):
`objectives` is the default for `aef run --config`, and each `evaluator.suites`
entry is a `module:function` reference resolved into a domain gate that can
only make a run fail, never pass. `knowledge_graph` and a non-default `extends`
are **refused at load time** rather than silently ignored — there is no builder
and no inheritance, and a field that validates while being read by nothing is
indistinguishable from a feature. The stub says which is which, field by field.

**Nothing merges automatically.** Tier-1 auto-merge is off and no flag, config
or environment variable enables it. A candidate passing all six gates is
escalated to a human. Do not try to route around this.

### Stop and ask
If you find yourself weakening a gate to make something pass, labelling an
achievable task `must_fail`, enabling auto-merge, or adding a secret to a
workflow — stop and ask. Those are the owner's calls, not yours.

### Report back
Which obligations are green, the six-gate ledger output, the reward-hack run
and its exit code, and **anything the docs told you to do that did not work**.
That last one is the most valuable thing you can send upstream: five defects
in aef-core were documentation instructing adopters to run commands the CLI
rejects, and they were only found by someone being the adopter.

## Where to look (inside the aef-core package, not necessarily this repo)
Locate an installed copy: `python -c "import aef; print(aef.__path__[0])"`.
`docs/` ships only in a source checkout.
- `aef/kernel/` — graph engine, Node/Edge, Services, checkpoint/replay
- `aef/state/` — the shared AEFState schema + migrations
- `aef/security/tool.py` — the policy engine every tool call passes through
- `docs/autonomy/self-improving-loop.md` — the full autonomy protocol
- `docs/adr/README.md` — every design decision, with rationale
"""


def render_autonomy_md(repo_name: str) -> str:
    return f"""# Autonomy contract for {repo_name} (inherited from aef-core)

This repo adopted aef-core, which is developed with an autonomous
self-improving loop. If you run that loop here, you inherit the SAME safety
contract. Full spec: aef-core `docs/autonomy/self-improving-loop.md`.

## Green bar (every step, all four must pass)

    pytest -q                              # exit 5 = no tests collected, not a pass
    mypy --strict <your package>
    ruff check .
    ruff format --check <your source dirs>

A freshly adopted repo has no tests, so `pytest -q` exits 5 on day one. That
is the bar telling you the truth: write the first test before you rely on it,
and note the same code fails G1 if you pass `pytest -q` as `--build-command`.

## Reproduce-first
Never write a fix before a test/command that reproduces the defect and fails
as reported. This is the single most load-bearing rule.

## HARD-STOP gates — the only things that require a human
Run unattended, but pause and ask a human for any of:
1. Any push to a repo other than this one, or any external publish (package
   upload, sending data off-box) beyond `git push` on this repo.
2. Enabling `aef/evolution/`, weakening the deny-by-default PolicyEngine, or
   removing/loosening a HITL approval gate.
3. Deleting or overwriting an existing user file.
4. A breaking public-contract change you are not confident about.

Everything else: decide and proceed.

## Running unattended — prompt blocks from the vendor migration guide
Added by `/new-model-check` (ships in `.claude/skills/`) against
`claude-fable-5-1`. A capable model still stops to describe the next step
or ask permission for one the request already covered; these blocks are
the guide's mitigation. The guide also says to KEEP any instruction to
test or check work before reporting — Reproduce-first and the green bar
stay. Re-run `/new-model-check` when the model changes.

> You are operating autonomously. The user is not watching in real time and cannot answer
> questions mid-task, so asking 'Want me to...?' or 'Shall I...?' will block the work. For
> reversible actions that follow from the original request, proceed without asking. Stop
> only for destructive actions or genuine scope changes the user must decide. Offering
> follow-ups after the task is done is fine; asking permission before doing the work is not.
>
> Exception: when the user is describing a problem, asking a question, or thinking out loud
> rather than requesting a change, the deliverable is your assessment. Report your findings
> and stop. Don't apply a fix until they ask for one.
>
> Before ending your turn, check your last paragraph. If it is a plan, an analysis, a
> question, a list of next steps, or a promise about work you have not done ('I'll...', 'let
> me know when...'), do that work now with tool calls. That includes retrying after errors
> and gathering missing information yourself. Do not stop because the context or session is
> long. End your turn only when the task is complete or you are blocked on input only the
> user can provide.
>
> Before running a command that changes system state (such as restarts, deletes, or config
> edits), check that the evidence actually supports that specific action. A signal that
> pattern-matches to a known failure may have a different cause.

The stops this repo adds to "destructive actions or genuine scope changes"
are exactly the HARD-STOP gates above.

> \\# Delivering work
> The user's request - or the plan they approved - sets the scope, and the scope is the
> deliverable: don't quietly narrow, widen, or swap it. Read ambiguity the way a careful
> colleague would: make routine judgment calls yourself, and check in only when different
> readings would lead to materially different work. If you see a real problem with the task
> as specified, say so in a sentence or two and keep building under stated assumptions; if
> the user hears the concern and reaffirms, that is their decision, so deliver the full
> request.
>
> If a question comes up partway, first do everything that doesn't depend on the answer;
> then state the assumption you made, or - when going ahead on a wrong guess would be unsafe
> or would make the work useless - put the question at the end of a turn that also delivers
> that progress. If one part turns out to be blocked, complete every other part in full and
> say exactly what you left out and why - the whole task is the deliverable, and scaling it
> down is the user's call, not yours. A step you have decided on is something to run, not to
> announce: describing the next step and ending the turn leaves it undone until the user
> replies.
>
> Keep changes to what the request needs. Something else you notice worth doing - cleanup or
> documentation the task didn't call for, a change to a file the task didn't require - is a
> suggestion to make at the end, not a change to make; actions clearly beyond what the ask
> implies, and risky or destructive ones, still need the user's go-ahead.

Scope and test coverage — the guide saw far fewer unrequested additions and
much less committed scratch-test code with no change in task success:

> If, while working or testing, you find a pre-existing bug, a performance concern, or
> behavior the task doesn't mention, don't fix, optimize or extend it in this change unless
> the requested behavior cannot work without it; report it as a follow-up in your summary.
> Where the task is ambiguous, implement the reading its wording and the surrounding code
> most directly support, state that assumption in your summary, and don't build for the
> other readings as well. Verify your work however you like; scratch scripts and quick
> checks need not be kept. Commit tests only where the task asks for them or this repository
> already keeps tests for this kind of change, sized like the neighboring test files -
> roughly one focused test per stated behavior - and don't turn scratch checks into
> additional permanent test files. This is about extras only: implement every behavior the
> task asks for, completely.

Targeted edits — the model is more likely than its predecessor to rewrite a
whole file where a small edit would do:

> The number of tokens used to edit files is best minimized, all else being equal.
> Therefore, when it will not affect the end result, try to surgically edit a file rather
> than rewrite the entire thing.

## Self-learning is bounded
"Self-learning" = writing reflections/critiques into memory. It does NOT mean
self-modification. `aef/evolution/` is disabled in code and stays that way —
that boundary is what makes unattended autonomy safe rather than reckless.

## Bounded, not open-ended
Every loop run starts from a finite work-list and STOPS when it is shipped.
Do not manufacture new findings to keep running. A fresh audit is a new,
deliberately-started loop.
"""


_POINTER_PREAMBLE = """This repo uses **aef-core**, a repo-agnostic Agent Operating System
scaffold. It works with any coding agent (Claude, Codex, Cursor, GitHub
Copilot, …) — the scaffold is plain Python + the `aef` CLI; only the entry
file each agent reads differs. `CLAUDE.md` / `AGENTS.md` hold the full
scaffold contract; the section below is the same block `aef adopt` maintains
in those files, so there is one contract and not four."""


def render_harness_pointer(
    repo_name: str, framework: Framework = "none", surface: PromptSurface | None = None
) -> str:
    """A thin, harness-neutral instructions file pointing at the canonical
    guide and inlining the safety contract — used for GitHub Copilot's
    `.github/copilot-instructions.md`.

    Body is `render_aef_block`, not a fourth copy of the same rules: when this
    file already exists in the adopter's repo, the block is what gets appended
    to it, and a pointer that says something different from the appended block
    is two contracts wearing one name.
    """
    block = render_aef_block(repo_name, framework, surface)
    return f"# {repo_name} — agent instructions (aef-core)\n\n{_POINTER_PREAMBLE}\n\n{block}\n"


def render_cursor_rule(
    repo_name: str, framework: Framework = "none", surface: PromptSurface | None = None
) -> str:
    """Cursor `.cursor/rules/*.mdc` — same pointer body, with the minimal
    frontmatter Cursor uses to always apply a rule."""
    frontmatter = "---\ndescription: aef-core scaffold contract\nalwaysApply: true\n---\n\n"
    return frontmatter + render_harness_pointer(repo_name, framework, surface)


@dataclass(frozen=True)
class AdoptResult:
    framework: Framework
    written_files: list[Path] = field(default_factory=list)
    skipped_files: list[Path] = field(default_factory=list)
    checklist: list[str] = field(default_factory=list)
    # The third verb (ADR 0153). A file that already existed and gained an
    # `aef:begin`/`aef:end` block is neither written (its own bytes are still
    # there) nor skipped (the adopter's agent now reads the contract), and
    # collapsing it into either is how the report stops describing what
    # happened.
    appended_files: list[Path] = field(default_factory=list)
    # The subset of `appended_files` whose block was written before the marker
    # signature existed and was upgraded in place (ADR 0172). Reported to the
    # adopter as a checklist note, because it is the one case where adopt
    # rewrote a block it can only *infer* it wrote.
    upgraded_blocks: list[Path] = field(default_factory=list)
    # Why each skip happened. `already exists` stays the default so callers
    # that never look up a reason print what they always printed.
    skip_reasons: dict[Path, str] = field(default_factory=dict)
    prompt_surface: PromptSurface = field(default_factory=PromptSurface)

    def detection(self) -> str:
        """The line `aef adopt` prints first, with the prompt-file counts."""
        return describe_detection(self.framework, self.prompt_surface)

    def skip_reason(self, path: Path) -> str:
        return self.skip_reasons.get(path, "already exists")


def render_new_model_check_skill() -> str:
    """The `/new-model-check` skill (docs/adr/0111): a per-model-release
    re-audit of every prompt and API surface. Shipped as package data rather
    than a Python string so this repo's own copy under `.claude/skills/` can
    be pinned byte-identical to what adopters receive. It carries no
    per-model facts — those are read from the bundled `claude-api` skill at
    run time, because a fact table here would rot the day the next model
    ships."""
    return (
        resources.files("aef.cli")
        .joinpath("templates/skills/new-model-check/SKILL.md")
        .read_bytes()
        .decode("utf-8")
    )


#: Why an entry file is skipped when its block is already the current one.
#: Named once because the checklist reads it back: a file skipped for THIS
#: reason does carry the contract, and step 1 must still name it (F-M8-3).
_BLOCK_ALREADY_CURRENT = "already carries the current aef block"

#: The five entry files ADR 0153 allows a block in, and the marker pair each
#: one uses. Named once so the pre-pass below cannot drift from the writes.
_BLOCK_FILES: tuple[tuple[str, tuple[str, str]], ...] = (
    ("CLAUDE.md", MD_MARKERS),
    ("AGENTS.md", MD_MARKERS),
    (".github/copilot-instructions.md", MD_MARKERS),
    (".cursor/rules/aef.mdc", MD_MARKERS),
    (".gitignore", GITIGNORE_MARKERS),
)


def _entry_files_with_legacy_blocks(target_dir: Path) -> tuple[str, ...]:
    """Which entry files carry a PRE-SIGNATURE block, read before anything is
    written. A pre-pass rather than a running tally because the checklist is
    rendered before three of the five files are touched, and a notice that
    depends on write order is a notice that is sometimes wrong."""
    found: list[str] = []
    for name, markers in _BLOCK_FILES:
        path = target_dir / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _signed_begin_re(markers[0]).search(text):
            continue
        if isinstance(_legacy_block_span(text, markers), tuple):
            found.append(name)
    return tuple(found)


def run_adopt(
    target_dir: Path,
    *,
    write_guard: Callable[[Path], None] | None = None,
    profile: str = "model",
    with_workflows: bool = False,
) -> AdoptResult:
    if profile not in {"offline", "model"}:
        raise ValueError("profile must be offline or model")
    if with_workflows and profile != "model":
        raise ValueError("workflows require the model profile")
    target_dir = target_dir.resolve()
    # BEFORE any write: the first thing adopt does is upgrade CLAUDE.md's block.
    legacy_blocks = _entry_files_with_legacy_blocks(target_dir)
    surface = detect_prompt_surface(target_dir)
    framework = detect_framework(target_dir)
    repo_name = target_dir.name

    written: list[Path] = []
    skipped: list[Path] = []
    appended: list[Path] = []
    migrated: list[Path] = []
    reasons: dict[Path, str] = {}

    def _skip(path: Path, reason: str) -> None:
        skipped.append(path)
        if reason != "already exists":
            reasons[path] = reason

    def _write_if_absent(
        relative_name: str,
        content: str,
        *,
        block: str | None = None,
        markers: tuple[str, str] = MD_MARKERS,
    ) -> None:
        """Write the file when it is absent; when it exists and `block` is
        given, maintain that block inside `markers` and touch nothing else.

        Appending inside markers is not overwriting, and ADR 0153 says why:
        the adopter's bytes are still there, unmodified and outside the block,
        and deleting the block restores the file exactly. Everything without a
        `block` keeps ADR 0034/0040's rule unchanged — skipped and reported.
        """
        path = target_dir / relative_name
        # ``Path.exists()`` is false for a dangling symlink, and normal file
        # writes follow symlinked parent directories.  Treat either shape as
        # an existing repository entry: adoption promises to add files inside
        # the target repo, never to follow its links and create files
        # elsewhere.
        parent = path.parent
        blocked_parent = False
        while parent != target_dir:
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                blocked_parent = True
                break
            parent = parent.parent
        if path.is_symlink() or blocked_parent:
            _skip(path, "a symlink, or under one — adoption never writes through a link")
            return
        if not path.exists():
            if write_guard is not None:
                write_guard(path)
            path.parent.mkdir(parents=True, exist_ok=True)  # for .github/, .cursor/rules/
            # `write_bytes`, not `write_text`: `write_text` writes `os.linesep`,
            # so every generated file in this scaffold would be CRLF on Windows
            # and its own byte-preservation tests would compare LF against it.
            path.write_bytes(content.encode("utf-8"))
            written.append(path)
            return
        if block is None:
            _skip(path, "already exists")
            return
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                _skip(path, "exists but is not a regular file")
                return
            if info.st_nlink > 1:
                _skip(path, "hardlink — adoption never modifies shared file contents")
                return
            existing = path.read_bytes()
        except OSError as exc:
            _skip(path, f"exists but is not readable as text ({type(exc).__name__})")
            return
        try:
            outcome = apply_block_bytes(existing, block, markers)
        except UnicodeDecodeError as exc:
            # Not text. Appending to bytes we cannot read is how a scaffold
            # corrupts an adopter's file; the skip says so.
            _skip(path, f"exists but is not readable as text ({type(exc).__name__})")
            return
        if outcome is None:
            _skip(
                path,
                f"carries an unbalanced or duplicated {markers[0]} / {markers[1]} pair — "
                f"refusing to guess which bytes are the block",
            )
            return
        if outcome.data == existing:
            _skip(path, _BLOCK_ALREADY_CURRENT)
            return
        if write_guard is not None:
            write_guard(path)
        path.write_bytes(outcome.data)
        appended.append(path)
        if outcome.action == "migrate":
            migrated.append(path)

    entry_block = render_aef_block(repo_name, framework, surface)
    claude_md = render_claude_md(framework, repo_name, surface)
    if profile == "offline":
        entry_block = _wrap_in_markers(OFFLINE_ENTRY, MD_MARKERS)
        claude_md = f"# {repo_name} — AEF offline integration\n\n{entry_block}\n"
    _write_if_absent("CLAUDE.md", claude_md, block=entry_block)
    _write_if_absent(
        "aef.yaml", OFFLINE_CONFIG if profile == "offline" else render_aef_yaml(repo_name)
    )
    _write_if_absent("aef_adapter.py", render_adapter_shim(framework, repo_name))

    # Zone A hygiene (ADR 0142, amended by ADR 0153). Handled BEFORE the
    # checklist, because what the checklist says depends on what happened
    # here: the two bytecode patterns are appended inside `# aef:begin` /
    # `# aef:end` when the existing file covers neither, and reported as a gap
    # the adopter must close only when appending was impossible.
    gitignore = target_dir / ".gitignore"
    existing_gitignore = ""
    if gitignore.is_file() and not gitignore.is_symlink():
        try:
            existing_gitignore = gitignore.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            existing_gitignore = ""
    covered = gitignore_covers_bytecode(existing_gitignore)
    _write_if_absent(
        ".gitignore",
        render_gitignore(),
        # A file that already keeps `.pyc` out needs nothing appended — and
        # nagging an adopter to add a pattern equivalent to one they have is
        # how generated advice stops being read (ADR 0142). `None` here means
        # "skip, as before"; the block form is only offered when it is needed,
        # or when adopt's own block is already in the file and has to stay
        # replaceable rather than duplicated.
        block=(
            None
            if covered and not carries_adopt_block(existing_gitignore, GITIGNORE_MARKERS)
            else render_gitignore_block()
        ),
        markers=GITIGNORE_MARKERS,
    )

    # Onboarding kit: the ingest-and-start guide plus the inlined autonomy
    # safety contract, so a new repo agent inherits both (see docs/adr/0034).
    _write_if_absent(
        "AGENT_INTEGRATION.md",
        render_offline_guide(repo_name)
        if profile == "offline"
        else render_agent_integration_md(repo_name),
    )
    _write_if_absent("AUTONOMY.md", render_autonomy_md(repo_name))

    # Cross-harness entry files (docs/adr/0040): every major coding-agent reads
    # a different instructions file. AGENTS.md carries the full contract
    # (Codex + the cross-tool convention); Copilot and Cursor get thin native
    # pointers into the canonical guide.
    #
    # These four are where ADR 0153 changed the rule, and `AGENTS.md` is the
    # measurement that forced it: on a real repo with eight prompt agents,
    # `aef adopt` wrote a `CLAUDE.md` the repo does not use and SKIPPED the
    # `AGENTS.md` it does — `grep -c AEF AGENTS.md` returned 0, so the
    # contract never reached the file that repo's agents actually read.
    _write_if_absent("AGENTS.md", claude_md, block=entry_block)
    _write_if_absent(
        "GROK.md",
        "# AEF portable guide (load explicitly)\n\n" + entry_block + "\n",
        block=entry_block,
    )
    _write_if_absent(
        ".github/copilot-instructions.md",
        entry_block
        if profile == "offline"
        else render_harness_pointer(repo_name, framework, surface),
        block=entry_block,
    )
    _write_if_absent(
        ".cursor/rules/aef.mdc",
        ("---\nalwaysApply: true\n---\n\n" + entry_block)
        if profile == "offline"
        else render_cursor_rule(repo_name, framework, surface),
        block=entry_block,
    )

    # HERE, and not before `CLAUDE.md`/`AGENTS.md` were handled, because step 1
    # of the checklist names the entry file adopt ACTUALLY wrote or appended to
    # — and until it moved, `CLAUDE.md` skipped as a symlink still produced
    # `1. Read the generated CLAUDE.md in full before writing any code.` as the
    # first thing a fresh session in that repo reads (F-M8-3, ADR 0187). The
    # `.gitignore` note two lines down already worked this way, for the same
    # reason spelled out above it: what the checklist says depends on what
    # happened. Nothing between here and there reads the checklist.
    entry_paths = [target_dir / name for name in ("CLAUDE.md", "AGENTS.md")]

    def _carries_contract(path: Path) -> bool:
        # Written, appended to, or skipped BECAUSE the block is already there
        # — a second `adopt` skips every entry file with that reason, and the
        # file it skipped is exactly the file the adopter should read.
        return path in written or path in appended or reasons.get(path) == _BLOCK_ALREADY_CURRENT

    entry_files = [p.name for p in entry_paths if _carries_contract(p)]
    entry_skip_reason = "; ".join(
        f"{p.name}: {reasons.get(p, 'already exists')}"
        for p in entry_paths
        if p in skipped and not _carries_contract(p)
    )
    checklist = render_migration_checklist(
        framework, surface, entry_files=tuple(entry_files), entry_skip_reason=entry_skip_reason
    )
    if profile == "offline":
        checklist = list(OFFLINE_CHECKLIST)
        if entry_skip_reason:
            checklist.append("Entry instructions could not be updated: " + entry_skip_reason)
    if legacy_blocks:
        checklist.append(legacy_block_upgraded_note(legacy_blocks))
    if gitignore in appended:
        checklist.append(gitignore_appended_note())
    elif gitignore in skipped and not covered:
        # Unreadable, a symlink, a directory, or markers we refuse to resolve:
        # the patterns cannot be shown to be there and could not be added, and
        # claiming otherwise is the one answer that costs a drift budget.
        checklist.extend(gitignore_gaps(existing_gitignore))
    _write_if_absent(
        "AEF_MIGRATION_CHECKLIST.md",
        "# AEF migration checklist\n\n" + "\n".join(f"- [ ] {item}" for item in checklist),
    )

    # The self-rewiring loop kit (ADR 0057/0058). LOOP.md leads with what does
    # NOT work yet: an adopting repo whose agents produce candidates against an
    # empty corpus sees every one rejected, and that reads as "the loop is
    # broken" rather than "the loop has nothing to judge against".
    if profile == "model":
        _write_if_absent("LOOP.md", render_loop_md(repo_name, surface.agents))
    # The sequence, in the order an adopter meets it (ADR 0148). Separate from
    # LOOP.md deliberately: LOOP.md says what the loop NEEDS, and needed a
    # reader who already knew when to run each command. Every command in it
    # was executed against a fresh adoption and its real output pasted.
    _write_if_absent(
        "FIRST_DAY.md",
        render_offline_guide(repo_name)
        if profile == "offline"
        else render_first_day_md(repo_name, surface.agents),
    )
    if profile == "model":
        _write_if_absent("agents/README.md", render_agents_zone_readme(repo_name))
        _write_if_absent("corpus/README.md", render_corpus_readme(repo_name))
    if with_workflows:
        _write_if_absent(".github/workflows/loop-gate.yml", render_loop_gate_workflow(repo_name))
    # The nightly cycle's `AEF_MODULE` must never default to the placeholder
    # `aef migrate` writes when it finds no call site — on a prompt-file repo
    # that module exists, raises, and the cycle's exit 1 reads as a healthy
    # rejection (ADR 0172). Where there are prompt agents, name the first
    # one's module: it is derived from migrate's own discovery and sanitiser,
    # so the workflow and `aef migrate` cannot disagree about it.
    if with_workflows:
        _write_if_absent(
            ".github/workflows/loop-monitor.yml",
            render_loop_monitor_workflow(repo_name, prompt_module=first_prompt_module(target_dir)),
        )

    # Per-model-release re-audit (docs/adr/0111). Without it an adopted
    # repo's prompts and call sites are checked against exactly one model:
    # whichever was current the day it adopted.
    if profile == "model":
        _write_if_absent(_ADOPT_SKILL_PATH, render_new_model_check_skill())

    return AdoptResult(
        framework=framework,
        written_files=written,
        skipped_files=skipped,
        checklist=checklist,
        appended_files=appended,
        upgraded_blocks=migrated,
        skip_reasons=reasons,
        prompt_surface=surface,
    )
