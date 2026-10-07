"""Do the things an adopter must supply actually exist?

Each obligation was discovered by an adopter (or by me, in this repo) being
stuck, one command at a time, in the worst possible order: run the loop, get
a refusal, fix one thing, get the next refusal. This reports all of them at
once with the command that fixes each.

`bless` lives here too, because LOOP.md obligation 5 told owners to archive a
blessed baseline and **no command existed to do it** — the obligation was
literally unmeetable and G5 refused every candidate forever (ADR 0073).

The sixth obligation, *model calls visible*, was added by ADR 0137 and is the
only one an adopter cannot discover by being stuck: a node that constructs its
own vendor client works perfectly, `aef doctor` reports green, and the failure
arrives much later as a gate that either makes live calls or scores the
candidate 0 — because the call was invisible to the recorder, so the scenario
carries no cassette to replay.

**These obligations are ADVISORY, and this file used to say otherwise.** Only
`aef loop doctor` reads `Preflight.ready`; `cycle`, `gate` and `run` never
have. ADR 0137 §2 claimed "`Preflight.ready` is false while it stands, so the
gates refuse", and `render()` closed with "the gates refuse for lack of
evidence" — both false, and reproduced: `loop doctor` exits 1 with obligation
6 red and `loop cycle` then proposes and gates the same repo. What is true is
narrower and is now what the text says: obligations 1, 2 and 5 are enforced
later by the gates themselves (G2/G3 refuse an empty corpus, G5 refuses
without a blessed baseline, a graph nothing routes to reflect records no
failure memory so the proposer never proposes), while 3, 4 and 6 are not
enforced anywhere and the loop will run without them. `cmd_cycle`/`cmd_gate`
print the unmet ones so the surface stops saying nothing. See ADR 0141 for
why they were not made blocking.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from aef.harness import archive, ledger
from aef.harness.candidate import ESCAPE_MODES, MODE_NAMES
from aef.harness.corpus import Expected, load_corpus
from aef.harness.git import GitError, GitRepo
from aef.harness.vendor_scan import MODEL_SDK_ROOTS, VendorImport, scan_file
from aef.harness.zones import DEFAULT_AGENT_ROOT, discover_graph_files


class BlessError(RuntimeError):
    pass


@dataclass(frozen=True)
class Obligation:
    name: str
    met: bool
    detail: str
    fix: str


@dataclass(frozen=True)
class Preflight:
    obligations: tuple[Obligation, ...]

    @property
    def ready(self) -> bool:
        return all(o.met for o in self.obligations)

    @property
    def unmet(self) -> tuple[Obligation, ...]:
        return tuple(o for o in self.obligations if not o.met)

    def render(self) -> str:
        width = max(len(o.name) for o in self.obligations)
        count = len(self.obligations)
        lines = [f"Loop readiness — {count} things you must supply", ""]
        for o in self.obligations:
            lines.append(f"  [{'OK' if o.met else '--'}] {o.name:<{width}}  {o.detail}")
            if not o.met:
                lines.append(f"       fix: {o.fix}")
        lines.append("")
        # This used to close with "the gates refuse for lack of evidence,
        # which is correct behaviour and not a bug." Nothing but this command
        # reads `ready`, so that sentence described a control that does not
        # exist: `loop doctor` exits 1 and `loop cycle` then gates the same
        # repo anyway (ADR 0141). Say what is true instead.
        lines.append(
            f"All {count} green — the loop can gate a candidate on real evidence."
            if self.ready
            else "These are ADVISORY and this command is the only thing that reads them: "
            "`aef loop cycle` and `aef loop gate` will still run, and will print the "
            "unmet ones. Some enforce themselves later — G2/G3 refuse an empty corpus "
            "and G5 refuses without a blessed baseline — but a missing halt channel or "
            "an invisible model call stops nothing, which is why they are listed here."
        )
        return "\n".join(lines)


def _reasoning_factory_names(tree: ast.Module) -> set[str]:
    """Names bound by `from aef.reasoning... import make_*_node`.

    The set is deliberately narrow: only `make_*_node` factories, only from
    `aef.reasoning`, and the local binding name (so `import make_x_node as f`
    is followed). Anything else that happens to take a `route=` keyword is
    somebody's own function and this file vouches for nothing about it.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not node.module or not node.module.startswith("aef.reasoning"):
            continue
        for alias in node.names:
            if alias.name.startswith("make_") and alias.name.endswith("_node"):
                names.add(alias.asname or alias.name)
    return names


def _reflect_is_routed_to(agent_source: Path) -> tuple[bool, str]:
    """A reflect node must exist AND something must ROUTE to it.

    Checking only that the node exists is not enough, and this is the trap
    that has now caught two people: an `Edge` to a reflect node does not wire
    it. Routing is chosen by node code, so a work node returning `END` never
    reaches reflect however the edges are drawn (ADR 0070).

    **Two shapes count, because there are two shapes.**

    1. A hand-written node — a three-argument `(state, ctx, services)`
       function whose `Return` carries the reflect node's id.
    2. A node built by an `aef.reasoning` factory with `route="reflect"`.
       `aef migrate` generates exactly this for a prompt agent (ADR 0152):
       `make_prompt_agent_node(agent_file=..., route="reflect")`, with the
       closure living in `aef/reasoning/prompt_agent.py` and no node function
       in the generated module at all.

    Shape 2 was invisible here until ADR 0167, so obligation 2 was
    **permanently red on every graph `aef migrate` writes for a prompt-file
    repo** — while the graph routed correctly. Reproduced: `loop doctor` on a
    freshly migrated `agents/migrated/marlin_accela/graph.py` printed
    `[--] reflect node routed to  a reflect node exists but nothing routes to
    it` and exited 1, and executing that same graph with a stub provider gave
    the trace `['prompt_agent', 'reflect', 'consolidate']`.

    The factory that BUILDS the reflect node is excluded: `make_reflect_node`
    routing to `"reflect"` would be a self-loop, not something arriving at it.
    """
    if not agent_source.is_file():
        return False, f"no agent source at {agent_source}"
    source = agent_source.read_text()
    if "make_reflect_node" not in source:
        return False, "no reflect node in the graph"

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return False, f"agent source does not parse: {exc}"

    # Find the id the reflect node is constructed with, defaulting to the
    # factory's own default.
    reflect_id = "reflect"
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "make_reflect_node"
        ):
            for kw in node.keywords:
                if kw.arg == "node_id" and isinstance(kw.value, ast.Constant):
                    reflect_id = str(kw.value.value)

    # Only Return statements inside NODE functions count. Scanning every
    # return in the module false-positived on `Edge(to_node="reflect")` inside
    # build_graph's return — the detector passed the exact trap it exists to
    # catch, and only a planted-fault test revealed it.
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        args = [a.arg for a in fn.args.args]
        if len(args) != 3:
            continue  # not (state, ctx, services)
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and node.value is not None:
                for literal in ast.walk(node.value):
                    if isinstance(literal, ast.Constant) and literal.value == reflect_id:
                        return True, f"{fn.name}() returns {reflect_id!r} as its Route"

    # Shape 2: a factory node built with `route="reflect"`. The route is the
    # node's whole control flow — `make_prompt_agent_node` returns exactly
    # this constant as its `Route` — so a keyword naming the reflect id is
    # the same evidence a `return delta, "reflect"` is.
    factories = _reasoning_factory_names(tree)
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
            continue
        callee = call.func.id
        if callee not in factories or callee == "make_reflect_node":
            continue
        for kw in call.keywords:
            if (
                kw.arg == "route"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value == reflect_id
            ):
                return True, f"{callee}(route={reflect_id!r}) builds a node that routes to it"

    return (
        False,
        "a reflect node exists but nothing routes to it — an Edge does not wire it, and no "
        "aef.reasoning factory in this module was given route='reflect' either",
    )


@dataclass(frozen=True)
class AgentSource:
    """Which file the graph obligations should actually read, and why.

    `--agent-path` is ONE flag with TWO meanings. Under `--proposer
    rule_based_prompt` it names the PERSONA `.md` the proposer edits (ADR
    0157, and that is what its own help text says); everywhere else it names
    the Python module that builds the graph. The obligations here only ever
    understood the second meaning, so on the documented prompt-proposer
    invocation they read a markdown file as Python:

        $ aef loop doctor ... --agent-root .claude/agents \\
              --agent-path .claude/agents/accela.md
          [--] reflect node routed to  no reflect node in the graph
               fix: add make_reflect_node() to your graph AND make a node
                    `return delta, 'reflect'` ...
          [OK] model calls visible     1 graph scanned, none reaches a model
                                       SDK the harness cannot see

    Both lines are wrong about a correctly generated graph. The first told
    the reader to add a node `aef migrate` had already written and routed;
    the second reported a clean bill of health for a repo with `import
    anthropic` planted in the generated graph — the ADR 0167/0168 false pass
    again, one flag over (ADR 0178).

    `problem` non-empty means the two obligations are unmet **with that
    sentence as the reason**, rather than with a Python answer about a file
    that is not Python.
    """

    path: str
    persona: str = ""
    problem: str = ""

    @property
    def from_persona(self) -> bool:
        return bool(self.persona)


def resolve_agent_source(
    repo_root: Path,
    agent_path: str,
    *,
    agent_root: str = DEFAULT_AGENT_ROOT,
) -> AgentSource:
    """A persona `.md` resolved to the graph `aef migrate` generated for it.

    The mapping is **imported, not re-derived**: `discover_prompt_agents` is
    the one function that decides which persona becomes which module, with
    the sanitiser (`marlin-accela` -> `marlin_accela`), the keyword and
    leading-digit prefixes and the collision disambiguation ADR 0152
    specifies. A second spelling of that rule here would be the ADR 0149
    shape — two answers to one question, drifting the first time either
    gains a case — and this file already lost a whole obligation to exactly
    that (ADR 0167's C<->D finding).

    Anything not ending in `.md` is returned unchanged, so every existing
    caller is unaffected. A `.md` that no persona discovery claims is
    reported as a problem rather than parsed: it is neither a graph nor a
    persona, and "no reflect node in the graph" is not what is wrong with it.
    """
    if not agent_path.lower().endswith((".md", ".toml")):
        return AgentSource(path=agent_path)

    from aef.cli.migrate import discover_prompt_agents
    from aef.reasoning.prompt_agent import DEFAULT_PROMPT_AGENT_DIR

    wanted = PurePosixPath(agent_path).as_posix()
    for site in discover_prompt_agents(repo_root, agent_root=agent_root):
        if PurePosixPath(site.source).as_posix() != wanted:
            continue
        graph = site.out_relative
        if (repo_root / graph).is_file():
            return AgentSource(path=graph, persona=wanted)
        # A repo may keep the GENERATED graphs at the default root and widen
        # Zone A only for the personas (ADR 0157's own reproduction, and the
        # form ADR 0158's acceptance test runs): `aef migrate` at the default
        # root, the loop at `--agent-root .claude/agents`. The persona is the
        # same file either way; look for its graph where the default root
        # would have put it before declaring it missing.
        if agent_root != DEFAULT_AGENT_ROOT:
            for default_site in discover_prompt_agents(repo_root, agent_root=DEFAULT_AGENT_ROOT):
                if PurePosixPath(default_site.source).as_posix() != wanted:
                    continue
                default_graph = default_site.out_relative
                if (repo_root / default_graph).is_file():
                    return AgentSource(path=default_graph, persona=wanted)
        return AgentSource(
            path=graph,
            persona=wanted,
            problem=(
                f"persona {wanted} has no generated graph — `aef migrate` would write it "
                f"to {graph} and nothing is there. Nothing was read, so nothing is claimed "
                f"about the graph's wiring or its model calls"
            ),
        )

    return AgentSource(
        path=wanted,
        problem=(
            f"{wanted} is a markdown file, and no persona under {DEFAULT_PROMPT_AGENT_DIR} "
            f"has that path — so it is neither a graph module nor a persona this can resolve "
            f"to one. --agent-path takes the module that builds your graph; with --proposer "
            f"rule_based_prompt it takes the persona `.md` instead (ADR 0157)"
        ),
    )


_MIGRATE_FIX = (
    "run `aef migrate --dir . --agent-root {root}` to generate the graph for this persona, "
    "or pass --agent-path naming the module that builds your graph. `--agent-path` means the "
    "persona only for --proposer rule_based_prompt (ADR 0157); these obligations are always "
    "about the GRAPH, and resolve the persona to it (ADR 0178)."
)


def _module_candidates(repo_root: Path, dotted: str) -> list[Path]:
    """Where an absolute `import a.b.c` could live inside this repo.

    Repo-root-relative, because that is where `aef run <module>` and the node
    body `aef migrate` generates (`from src.my_agent import run_agent`) both
    resolve from: the adopter's repo root is on `sys.path`. `a.b` is also
    tried as `a/b.py` when written `from a import b`, since that form names a
    module as often as it names a symbol.
    """
    parts = dotted.split(".")
    return [
        repo_root.joinpath(*parts).with_suffix(".py"),
        repo_root.joinpath(*parts, "__init__.py"),
    ]


def _reachable_modules(repo_root: Path, entry: Path) -> list[Path]:
    """Every in-repo `.py` file the entry file reaches by import, transitively.

    Only files that resolve inside `repo_root` are followed. A third-party
    import is not the adopter's code and is not this obligation's business —
    `anthropic` itself is expected to import `anthropic`.

    Bounded by the visited set; a circular import terminates.
    """
    entry = entry.resolve()
    seen: set[Path] = {entry}
    queue: list[Path] = [entry]
    order: list[Path] = [entry]

    while queue:
        current = queue.pop()
        try:
            tree = ast.parse(current.read_text(encoding="utf-8"), filename=str(current))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        dotted_names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                dotted_names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                dotted_names.append(node.module)
                # `from a.b import c` — `c` may itself be a module.
                dotted_names.extend(f"{node.module}.{a.name}" for a in node.names)
        for dotted in dotted_names:
            for candidate in _module_candidates(repo_root, dotted):
                resolved = candidate.resolve()
                if resolved in seen or not candidate.is_file():
                    continue
                seen.add(resolved)
                order.append(resolved)
                queue.append(resolved)
    return order


_UNROUTED_MARKER = "UNROUTED wrapper for "
_WRAPS_MARKER = "Wraps `"
_NOT_ROUTED_MARKER = "Not routed because "


def _migrate_refusals(entry: Path) -> dict[str, str]:
    """`{dotted module: why migrate refused to route it}`, read from the graph.

    `aef migrate` writes the decision into every generated node's own
    docstring — `UNROUTED wrapper for \\`src.my_agent.run_agent\\`` followed by
    `Not routed because <reason>`. Parsing that back is how this file tells
    "migrate has not looked at this yet" from "migrate looked and refused",
    which are the two cases whose remedies are completely different.

    Read from the docstring rather than by importing `aef.cli.migrate`: the
    harness does not import the CLI, and the generated file is the artefact
    the adopter actually has in front of them.
    """
    try:
        tree = ast.parse(entry.read_text(encoding="utf-8"), filename=str(entry))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return {}
    refusals: dict[str, str] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        doc = ast.get_docstring(fn) or ""
        if not doc.startswith(_UNROUTED_MARKER):
            continue
        # The dotted name follows `Wraps \`...\``. It used to sit on the first
        # line, and this parser read it from there — then ADR 0140 rewrote the
        # docstring and every refusal silently stopped being parsed, because
        # two modules shared a format and nothing compared them (ADR 0091, in
        # the file that exists to stop an adopter reading a fix that does not
        # fix anything). `test_the_generated_docstring_is_a_contract...` runs
        # the real `aef migrate` and reads its output back through here.
        _, _, after = doc.partition(_WRAPS_MARKER)
        dotted = after.split("`", 1)[0] if after else ""
        if not dotted:
            continue
        module = dotted.rsplit(".", 1)[0] if "." in dotted else dotted
        _, _, tail = doc.partition(_NOT_ROUTED_MARKER)
        reason = " ".join(tail.split("\n\n", 1)[0].split()).rstrip(".")
        refusals[module] = reason
    return refusals


def _dotted_from(repo_root: Path, path: Path) -> str:
    try:
        rel = path.relative_to(repo_root.resolve())
    except ValueError:  # pragma: no cover - reachability keeps them inside the repo
        return path.stem
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


_GENERIC_FIX = (
    "run `aef migrate --dir . --force` and READ ITS REPORT. Where it can, it rewrites the "
    "node to call `services.require_model_provider().complete(...)` instead of a client the "
    "node builds itself. Where it cannot it prints `NOT routed because ...` and generates a "
    "wrapper that leaves this obligation red — re-running it then changes nothing, and the "
    "remedy becomes the hand-routing one this message will name once migrate has said so."
)


def _hand_routing_fix(module: str, reason: str) -> str:
    return (
        f"`aef migrate --dir . --force` will NOT fix this and will loop: it already refused to "
        f"route {module} ({reason}), and regenerating produces the same unrouted wrapper. Two "
        f"edits, both yours. (1) Rewrite that node's body to call "
        f"`services.require_model_provider().complete(...)` instead of calling your function, "
        f"re-expressing whatever the refusal reason names. (2) DELETE the "
        f"`from {module} import ...` line from the node — {module} stays in the graph's "
        f"reachable set while that import stands, so the obligation stays red even after the "
        f"body is routed. Neither step is optional and neither is automatic."
    )


def model_calls_are_visible(repo_root: Path, agent_path: str) -> tuple[bool, str, str]:
    """`(visible, detail, fix)` — no module reachable from the graph may import
    a **model** SDK.

    This is constraint #3 pointed at the adopter instead of at this repo. The
    scanner is the one `tests/test_vendor_isolation.py` has run since Phase 0
    (`aef/harness/vendor_scan.py`); the difference is where it is aimed and
    **which list it is aimed with**.

    `MODEL_SDK_ROOTS`, not `VENDOR_TOP_LEVEL_MODULES`. The first version of
    this function inherited `scan_file`'s constraint-#3 default, so an adopter
    whose graph reached a module doing `import psycopg2` was permanently
    blocked and told to route their Postgres connection through
    `require_model_provider().complete(...)`. Fourteen of the nineteen names
    in the constraint #3 list are not model SDKs; ADR 0137's "it over-reports
    nothing" was false for all fourteen (ADR 0141). Constraint #3 still uses
    the full list where it belongs — inside this repo, in
    `tests/test_vendor_isolation.py`.

    A node that builds its own client is not a style problem. It bypasses the
    policy engine, the fallback chain and the harness login, and — the part
    that costs the adopter a whole loop — it is invisible to
    `aef/harness/recorder.py`, so its scenario carries **no** `RecordedCall`.
    Replay with `on_miss="fail"` then has nothing to serve and nothing to
    refuse: the node reaches the vendor live, or fails for want of a
    credential and scores 0. Both were reproduced (ADR 0137).
    """
    entry = (repo_root / agent_path).resolve()
    if not entry.is_file():
        return (
            False,
            f"no agent source at {entry} — nothing to scan",
            f"point --agent-path at the module that builds your graph; {agent_path!r} is "
            f"not a file under {repo_root}",
        )

    modules = _reachable_modules(repo_root, entry)
    found: list[VendorImport] = []
    for module in modules:
        found.extend(scan_file(module, roots=MODEL_SDK_ROOTS))
    if not found:
        return (
            True,
            f"{len(modules)} reachable module(s), none imports a model SDK",
            "",
        )

    first = found[0]
    try:
        where = first.path.relative_to(repo_root.resolve())
    except ValueError:  # pragma: no cover - reachability keeps them inside the repo
        where = first.path
    extra = f" (+{len(found) - 1} more)" if len(found) > 1 else ""
    detail = f"{where}:{first.lineno} imports {first.module}{extra} — the harness cannot see it"

    # WHICH fix, and this is the whole point of ADR 0141's R4. Telling an
    # adopter to re-run migrate when migrate has already refused this exact
    # function is a loop: the same wrapper is regenerated and the obligation
    # is red again, with the same message.
    refusals = _migrate_refusals(entry)
    dotted = _dotted_from(repo_root, first.path)
    reason = refusals.get(dotted)
    consequence = (
        " A node with its own client bypasses the policy engine and the fallback chain, "
        "and the recorder captures no RecordedCall for it — so the gates replay nothing "
        "and either call the vendor live or score it 0."
    )
    fix = _hand_routing_fix(dotted, reason) if reason is not None else _GENERIC_FIX
    return False, detail, fix + consequence


def preflight(
    *,
    repo_root: Path,
    state_root: Path,
    corpus_root: Path,
    agent_path: str,
    graph_id: str,
    halt_channel_configured: bool,
    observations: Path,
    agent_root: str = DEFAULT_AGENT_ROOT,
    scan_all_graphs: bool = False,
) -> Preflight:
    """The obligations, reported against ONE Zone A tree.

    `agent_root` defaults rather than being required because every existing
    caller passed the default implicitly; obligations 5 and 6 read it, the
    first to answer a question nothing could answer before — *is the archived
    baseline a baseline of the tree this loop is measuring?* — and the second
    to know where the graphs are (ADR 0167).

    `scan_all_graphs` widens obligation 6 from the single `agent_path` to
    every graph `discover_graph_files` finds. The CLI passes it when
    `--agent-path` was left at its default, because then the path is this
    package's guess rather than the owner's answer, and a diagnostic that
    reports on one file out of nine is how ADR 0168's false pass happened.
    Default `False`, so a caller that names a path still gets an answer about
    that path. It is forced on when `agent_path` was a PERSONA, because the
    graph is then this package's derivation from the persona rather than the
    owner's answer — the same reason the CLI passes it for the defaulted path
    (ADR 0178).

    `agent_path` may be a persona `.md`: `--proposer rule_based_prompt` reads
    it as one (ADR 0157) and it is one flag. `resolve_agent_source` maps it to
    the generated graph so obligations 2 and 6 report on Python rather than on
    markdown; the proposer is untouched and still gets the persona.
    """
    checks: list[Obligation] = []
    source = resolve_agent_source(repo_root, agent_path, agent_root=agent_root)
    migrate_fix = _MIGRATE_FIX.format(root=agent_root)

    # 1 — corpus with at least one tripwire
    try:
        corpus = load_corpus(corpus_root) if corpus_root.is_dir() else None
        scenarios = corpus.scenarios if corpus else ()
        tripwires = [s for s in scenarios if s.expected is Expected.MUST_FAIL]
        checks.append(
            Obligation(
                name="corpus + tripwire",
                met=bool(scenarios) and bool(tripwires),
                detail=f"{len(scenarios)} scenario(s), {len(tripwires)} tripwire(s)",
                fix=(
                    "aef loop record <module> --corpus corpus --scenario-id tripwire-1 "
                    "--objective '<a task beyond this agent>' --split validation "
                    "--expected must_fail --working-memory '{\"difficulty\": 99}'. "
                    "Without a tripwire the gates cannot detect reward hacking (ADR 0060)."
                ),
            )
        )
    except Exception as exc:  # noqa: BLE001 - a broken corpus is an unmet obligation
        checks.append(
            Obligation("corpus + tripwire", False, f"corpus unreadable: {exc}", "fix the corpus")
        )

    # 2 — a reflect node something routes to
    #
    # It read `--agent-path` as Python. Under `--proposer rule_based_prompt`
    # that flag names a persona `.md`, so this printed `no reflect node in the
    # graph` — with a fix telling the reader to add a node `aef migrate` had
    # already written and routed — on every prompt-proposer cycle, forever
    # (reproduced, ADR 0178).
    if source.problem:
        routed, why, reflect_fix = False, source.problem, migrate_fix
    else:
        routed, why = _reflect_is_routed_to(repo_root / source.path)
        if source.from_persona:
            why = f"{source.path}: {why}"
        reflect_fix = (
            "add make_reflect_node() to your graph AND make a node "
            "`return delta, 'reflect'` — an Edge alone does not route (ADR 0070)"
        )
    checks.append(
        Obligation(
            name="reflect node routed to",
            met=routed,
            detail=why,
            fix=reflect_fix,
        )
    )

    # 3 — observations
    count = len(observations.read_text().splitlines()) if observations.is_file() else 0
    checks.append(
        Obligation(
            name="observations",
            met=count > 0,
            detail=f"{count} recorded run(s) at {observations}",
            fix="pass --observations from your production runs, then "
            "`aef loop monitor --observations <path>`",
        )
    )

    # 4 — halt channel
    checks.append(
        Obligation(
            name="halt channel",
            met=halt_channel_configured,
            detail="configured" if halt_channel_configured else "none — a halt would tell nobody",
            fix="commit halt_channel.argv in aef.yaml (or --config), or set "
            "AEF_HALT_WEBHOOK in your environment (never in this repo)",
        )
    )

    # 5 — blessed baseline, OF THE TREE THIS LOOP IS ACTUALLY MEASURING
    versions = archive.versions(state_root / "archive", graph_id)
    root_flag = f" --agent-root {agent_root}" if agent_root != DEFAULT_AGENT_ROOT else ""
    # The GRAPH, never the persona: under the default root a persona lives
    # outside the tree `bless` archives, so a fix line naming it is a command
    # that refuses ("... is NOT inside the tree this would archive").
    bless_fix = (
        f"aef loop bless --repo . --state {state_root} --agent-path {source.path}{root_flag}"
        f" --graph-id {graph_id}"
    )
    blessed_root = ""
    if versions:
        try:
            blessed_root = archive.load_entry(
                state_root / "archive", graph_id, versions[-1]
            ).agent_root
        except archive.ArchiveError:  # pragma: no cover - a broken entry is its own alarm
            blessed_root = ""
    # "" means the entry predates ADR 0167 and recorded no root. It is NOT
    # treated as a mismatch: a baseline blessed before the field existed is
    # not evidence of disagreement, and failing every one of them would be a
    # control that fires on the ordinary case.
    root_mismatch = bool(versions) and blessed_root not in ("", agent_root)
    if root_mismatch:
        detail = (
            f"{len(versions)} archived version(s), but v{versions[-1]} was blessed with "
            f"--agent-root {blessed_root!r} and this loop is running under {agent_root!r} — "
            f"G5 would measure every candidate's drift between two different trees"
        )
    else:
        recorded = f"of {blessed_root!r}" if blessed_root else "(root not recorded)"
        detail = (
            f"{len(versions)} archived version(s) {recorded}"
            if versions
            else "0 archived version(s)"
        )
    checks.append(
        Obligation(
            name="blessed baseline",
            met=bool(versions) and not root_mismatch,
            detail=(
                f"{detail}; archive namespace --graph-id {graph_id} (use it on every loop command)"
            ),
            fix=(
                f"the baseline and this invocation must name the SAME Zone A tree. Either "
                f"re-run with --agent-root {blessed_root!r}, or start a new graph id and "
                f"bless it under {agent_root!r} — re-blessing the same graph is refused, "
                f"because the drift budget is measured against the baseline and silently "
                f"replacing it resets that budget without anyone deciding to (ADR 0053)."
                if root_mismatch
                else bless_fix
            ),
        )
    )

    # 6 — every model call reaches the harness (ADR 0137, corrected by 0141,
    # widened past one file by 0167)
    #
    # It scanned ONE path, defaulted by the CLI, so on a prompt-file repo with
    # eight generated graphs it answered about `agents/migrated/graph.py` —
    # the call-site stub whose `build_graph()` raises `NotImplementedError` and
    # which reaches no model at all — and reported the obligation MET while the
    # eight graphs that do call a model were never opened. Reproduced: a model
    # SDK import planted in one generated graph, `visible=True  1 reachable
    # module(s), none imports a model SDK`. Exactly the false pass ADR 0168
    # fixed in `aef doctor`, in the other diagnostic, sharing the discovery
    # function 0168 added rather than a second answer to the same question.
    #
    # And it read a persona `.md` as a graph: `model_calls_are_visible`
    # found the file, reached no imports out of it and reported `1 graph
    # scanned, none reaches a model SDK the harness cannot see` while an
    # `import anthropic` planted in the generated graph went unopened — with
    # the wide scan disabled, because `--agent-path` was not at its default
    # (reproduced, ADR 0178). A persona resolves to its generated graph, and
    # the graph it resolves to is this package's derivation rather than the
    # owner's answer, so the wide scan applies for the same reason it applies
    # to the defaulted path.
    if source.problem:
        checks.append(
            Obligation(
                name="model calls visible",
                met=False,
                detail=source.problem,
                fix=migrate_fix,
            )
        )
        return Preflight(obligations=tuple(checks))

    targets = [source.path]
    if scan_all_graphs or source.from_persona:
        targets = discover_graph_files(repo_root, agent_root=agent_root) or [source.path]
    invisible: list[tuple[str, str, str]] = []
    for target in targets:
        ok, why, how = model_calls_are_visible(repo_root, target)
        if not ok:
            invisible.append((target, why, how))
    if invisible:
        first_path, first_detail, first_fix = invisible[0]
        more = f" (+{len(invisible) - 1} more graph(s))" if len(invisible) > 1 else ""
        detail = f"{first_path}: {first_detail}{more}"
        fix = first_fix
    else:
        plural = "graph" if len(targets) == 1 else "graphs"
        detail = f"{len(targets)} {plural} scanned, none reaches a model SDK the harness cannot see"
        fix = ""
    checks.append(Obligation(name="model calls visible", met=not invisible, detail=detail, fix=fix))

    return Preflight(obligations=tuple(checks))


def _zone_a_files(repo: GitRepo, ref: str, agent_root: str) -> dict[str, bytes]:
    """Every file under the Zone A root **as of `ref`**, keyed by repo path.

    Read from git, not from the working tree, for two reasons. It is the same
    source `_candidate_files` reads, so G5's two inputs describe the same tree
    the same way — reading one side from disk let untracked build output
    (a `__pycache__` the candidate had committed, in the run that found this)
    appear on one side only and charged 0.430 drift for a two-line change.
    And a baseline blessed from a dirty working tree records a state that
    exists nowhere in history, so nothing could ever be compared against it
    reproducibly (ADR 0074).
    """
    paths = repo.list_tree(ref, agent_root)
    return {p: repo.run_bytes("show", f"{ref}:{p}") for p in sorted(paths)}


def _zone_a_escapes(repo: GitRepo, ref: str, agent_root: str) -> dict[str, str]:
    """`{path: mode}` for every Zone A entry that is a symlink or a gitlink.

    The same `ESCAPE_MODES` `candidate.check_modes` denies with
    `security_event=True` — imported, never re-listed, because two lists of
    what counts as an escape drifting apart is the ADR 0091 shape and this
    pair had already drifted: a candidate that ADDS a Zone A symlink is a
    security event, while `bless` accepted one as the baseline (ADR 0149).

    Read with `ls-tree -r` rather than through `list_tree`, which asks only
    for names. The mode is the whole question here: for `120000` git's blob is
    the **link target string**, so `_zone_a_files` archives 16 bytes of
    `../real/graph.py` and the baseline contains the agent by name and none of
    it by content.
    """
    try:
        out = repo.run("ls-tree", "-r", "-z", ref, "--", agent_root)
    except GitError:  # pragma: no cover - `bless` has already read this tree
        return {}
    escapes: dict[str, str] = {}
    for record in out.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        mode = meta.split(" ", 1)[0]
        if mode in ESCAPE_MODES:
            escapes[path] = mode
    return escapes


def _normalise(path: str) -> str:
    """One spelling for one file, so `./agents/x.py` and `agents/x.py` are the
    same answer. Git reports the second; a person types either."""
    return PurePosixPath(path.replace("\\", "/")).as_posix().removeprefix("./")


def bless(
    *,
    repo_root: Path,
    state_root: Path,
    agent_path: str,
    graph_id: str,
    at: datetime,
    note: str = "",
    agent_root: str = DEFAULT_AGENT_ROOT,
    ref: str = "HEAD",
) -> archive.ArchiveEntry:
    """Archive the current Zone A state as the owner-blessed baseline.

    **The whole Zone A tree, not one file.** G5 measures drift as
    `structural_drift(baseline, candidate)`, which unions the two key sets;
    archiving a single file while the candidate side describes the whole tree
    made every other Zone A file read as deleted, and the *first* candidate
    after a blessing was rejected for drift it had not caused (ADR 0074).
    Baseline and candidate must describe the same tree or the metric is
    measuring the difference between two questions.

    Refuses when one already exists. Rebaselining is owner-only and
    rate-limited by G5 (ADR 0053), and a `bless` that silently replaced the
    baseline would route around that — the drift budget is measured against
    this, so overwriting it resets drift to zero without anyone deciding to.

    **And refuses a `state_root` inside the repository**, which it did not
    until ADR 0167. `harness.loop._preflight` has refused that since ADR 0090
    — the ledger and archive swept into a candidate's own commit by `git add
    -A` make the audit trail part of what it audits — but `bless` does not go
    through `_preflight`, so `aef loop bless --state <repo>/state` printed
    "blessed agents/demo/graph.py as baseline v1" and left `archive/` and
    `ledger.jsonl` inside the working tree, while `aef loop cycle` with the
    same `--state` refused (reproduced). The baseline is the one artefact that
    must sit outside the candidate's reach: under a widened `--agent-root` it
    could otherwise end up archived into its own next baseline.

    The check is here as well as in the CLI for L6's reason: `bless` is
    importable, and a control that only binds when argparse is involved does
    not bind on the path a library caller takes.
    """
    from aef.harness.loop import LoopConfig, LoopPaths, _check_state_is_outside_the_repo

    # The SAME function the driver calls, not a second copy of its predicate.
    _check_state_is_outside_the_repo(
        LoopConfig(repo=GitRepo(root=repo_root), paths=LoopPaths(root=state_root))
    )

    existing = archive.versions(state_root / "archive", graph_id)
    if existing:
        raise BlessError(
            f"graph {graph_id!r} already has {len(existing)} archived version(s); a baseline "
            f"exists. Rebaselining is a separate, rate-limited owner decision (G5, ADR 0053) "
            f"— blessing again here would reset the drift budget without anyone choosing to."
        )

    repo = GitRepo(root=repo_root)
    if not repo.path_exists_at(ref, agent_path):
        raise BlessError(
            f"no agent source at {agent_path} in {ref}; nothing to bless. If you have just "
            f"written it, commit it first — the baseline is read from git so that what was "
            f"blessed is a state the gates can actually compare against."
        )

    files = _zone_a_files(repo, ref, agent_root)
    if not files:
        raise BlessError(f"no files under {agent_root!r} at {ref}; nothing to bless")

    # Two different questions, and until ADR 0147 this function asked the
    # first and answered the second. `path_exists_at` above asks "does the
    # agent exist?"; `_zone_a_files` asks "what will be archived?" — and
    # nothing checked that the answer to the second contains the subject of
    # the first. Reproduced against an adopted repo: `bless --agent-path
    # aef_migrated.py` printed `blessed aef_migrated.py as baseline v1` while
    # archiving one file, `agents/README.md` (ADR 0139's second reported
    # defect).
    #
    # The consequence is not cosmetic. The `blessed baseline` obligation goes
    # green on evidence unrelated to the agent, and G5 then measures every
    # candidate's structural drift against a baseline that never contained
    # the file the candidate changes — so the first real proposal is charged
    # for the whole agent as an addition. Refusing here is the only place the
    # two paths are both in hand.
    if _normalise(agent_path) not in {_normalise(p) for p in files}:
        shown = ", ".join(sorted(files)[:3]) + ("..." if len(files) > 3 else "")
        raise BlessError(
            f"{agent_path} exists at {ref} but is NOT inside the tree this would archive: "
            f"{agent_root!r} at {ref} holds {len(files)} file(s) ({shown}). A baseline is "
            f"the whole Zone A tree, and G5 measures every candidate's drift against it, so "
            f"blessing here would report {agent_path} as blessed while archiving a tree that "
            f"does not contain it. Move the agent under {agent_root!r} (Zone A is the only "
            f"place the loop may propose changes), or pass --agent-root naming the tree "
            f"{agent_path} actually lives in."
        )

    # A symlink PASSES the containment check above, because both sides come
    # from `git ls-tree` and a link is a tree entry like any other — ADR 0147
    # named exactly this case as untested and it is now reproduced (ADR 0149):
    # `bless --agent-path agents/graph.py` on a repo where that path is a link
    # to `../real/graph.py` printed `blessed agents/graph.py as baseline v1`
    # and archived a 16-byte file whose entire content is the string
    # `../real/graph.py`. The baseline then contains the agent by NAME and
    # none of it by CONTENT, so G5 measures every candidate's drift against a
    # tree that never held the code, and any edit to the real file is drift of
    # zero.
    #
    # `candidate.check_modes` already refuses these on the candidate side as a
    # SECURITY EVENT rather than a rejection; the asymmetry — deny it landing,
    # accept it as the thing everything is measured against — was the seam.
    # Same `ESCAPE_MODES`, not a second list.
    escapes = _zone_a_escapes(repo, ref, agent_root)
    if escapes:
        shown = ", ".join(
            f"{p} ({MODE_NAMES.get(m, f'mode {m}')})" for p, m in sorted(escapes.items())[:3]
        ) + ("..." if len(escapes) > 3 else "")
        raise BlessError(
            f"{agent_root!r} at {ref} holds {len(escapes)} entr(ies) git records as a symlink "
            f"or submodule, not a regular file: {shown}. A baseline archives what `git show` "
            f"returns, and for those that is the LINK TARGET — so blessing here would record "
            f"the agent by name and none of it by content, and G5 would measure every "
            f"candidate's drift against a tree that never held the code. `aef loop gate` "
            f"already refuses a candidate that ADDS one of these, as a security event rather "
            f"than a rejection. Replace it with the real file under {agent_root!r}, or pass "
            f"--agent-root naming the tree the real file lives in."
        )

    entry = archive.record(
        state_root / "archive",
        graph_id,
        files=files,
        base_sha="0" * 40,
        head_sha="0" * 40,
        recorded_at=at,
        notes=note or "owner-blessed baseline",
        # WHICH tree this is the baseline of. Both sides of G5's drift metric
        # must describe the same tree, and until ADR 0167 the entry recorded
        # only the files — so a baseline blessed under `--agent-root
        # .claude/agents` and a cycle run at the default root produced a
        # union of two disjoint key sets and `cumulative drift: 1.000`,
        # reproduced end to end. Recording it lets preflight say so by name
        # instead of leaving it to be read off a rejection.
        agent_root=agent_root,
    )
    ledger.append(
        state_root,
        kind=ledger.EventKind.BLESSED,
        at=at,
        proposal_id=f"baseline@{graph_id}",
        summary="owner-blessed baseline archived",
        detail={"archive_version": entry.version, "blessed": True},
    )
    return entry
