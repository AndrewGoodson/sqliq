"""`aef migrate` — generate node wrappers for a repo's real LLM call sites.

## Why this exists

`aef adopt` writes documentation, a config template, and a stub whose body is
`raise NotImplementedError`. It reads none of the adopting repo's code. For a
long time the scaffold's own pitch claimed agents "inherit ... without
rebuilding any of it per agent", which was the reverse of true: step 5 of the
checklist adopt generates is *"Convert each call site into a Node function"*,
by hand, for every call site.

This command does the mechanical half of that conversion and, more importantly,
**names the half it cannot do**.

## What it finds, and why that unit

Not raw SDK calls. The useful seam is the adopting repo's **own function that
wraps the vendor SDK** — `call_llm_with_backend`, `ask_claude`, whatever it is
called locally — because that function is where the repo has already put its
retries, budgets, backend selection and logging. Wrapping the raw
`client.messages.create` underneath it would bypass all of that and stand up a
second, dumber path beside a hardened one.

So: a function is a **call site** if its body contains a vendor SDK call. One
generated node per such function.

## Two node forms, and which one you get (ADR 0137)

Wrapping the adopter's function preserves everything that function does — and
leaves the model call **invisible to the harness**. A node that calls a
function that builds its own `anthropic.Anthropic()` never touches
`Services.model_provider`, so the policy engine does not see the call, the
fallback chain cannot cover it, the harness login cannot pay for it, and
`aef loop record` captures no `RecordedCall`. The scenario then carries no
cassette, and replay with `on_miss="fail"` has nothing to serve: the gate
reaches the vendor live, or fails for want of a credential and scores 0.

So this command generates one of two forms, decides which by reading the
function, and **says which it chose and why** — for every site, in the report
and in the generated docstring:

- **routed** — the node calls `services.require_model_provider().complete(...)`
  and does *not* call your function. Generated only when the function is a
  thin direct wrapper: undecorated, constructing an unconfigured client
  itself, making exactly one completion call with no loop, no `try`, no
  stream, no `**kwargs` and no other calls — **and passing only keywords
  `CompletionRequest` carries verbatim**.
- **unrouted** — today's wrapper, which calls your function, plus a warning in
  its docstring saying the call is invisible and what that costs.

**The falsification clause, honoured rather than argued.** If the function has
retries, a backend router, a loop, a stream, or does anything else at all,
routing through `ModelProvider.complete()` would silently drop it — a
single-shot, non-streaming call is strictly less than what that function does.
That is a trade, and a generator is the wrong place to make one on the
adopter's behalf. Every such site gets the **unrouted** form with the warning,
and the report names the specific thing that would have been lost. The
conservative direction is deliberate: a wrapper that is honestly labelled
invisible is recoverable; a rewrite that quietly dropped a retry policy is
found in production.

**And the clause covers the request, not only the control flow (ADR 0140).**
Its first version enumerated retries, streams and backends and never asked
whether the *request* survives translation. `CompletionRequest` has five
fields. A call passing `system=`, `tools=` or `stop_sequences=` was routed,
and there is no field for any of them — so "yours to re-express" named a place
that does not exist, while the generated docstring said "there is nothing here
for routing to lose". (`ProviderMessage` carries a `system` role that both
adapters fold in, so a system prompt is *representable*; synthesising one to
pair with the substituted objective is a semantic decision, which is why this
command refuses rather than guesses.) The routable set of keywords is read off
`dataclasses.fields(CompletionRequest)`, so a field added to the request type
widens it on the same commit and a second hardcoded list cannot drift from it.

## What it refuses to pretend

Three shapes are found and **skipped, with the reason recorded**, because
generating something plausible for them would be worse than generating nothing:

- **async functions** — `ModelProvider.complete()` and the node contract are
  sync. A generated wrapper would need an event loop the caller may not have.
- **methods** — they need an instance, and this command cannot know how the
  repo constructs one.
- **generators** — they yield rather than return; a node returns once.

Every skip is reported. A migration tool that silently emits nodes for a third
of the call sites and says nothing about the rest is how you end up believing a
repo is migrated when it is not.

## Where it writes, and what it wires (ADR 0143)

Two things this command got wrong for its whole life, both measured by running
the harness's own code against its own output:

- **It wrote to the repo root, which is Zone C.** `inspect_candidate` on a
  commit whose only changed path was the generated file returned
  `allowed: False ... Zone C (core) — not under the agent root 'agents'`, so
  the one file `aef migrate` exists to produce was the one file the loop could
  never propose a change to. The default is now `DEFAULT_MIGRATED_OUT`, inside
  Zone A, `--out` moves it, and `report()` names the zone of whatever path it
  wrote — because the failure mode is otherwise discovered at the *end* of a
  cycle rather than at the moment of writing.
- **The graph it generated had one node routed to `END`.** Nothing wrote
  failure memory, so the proposer had no evidence, so `aef loop cycle` exited
  0 with `no admissible failure memory: no candidate this cycle`. The
  generated `build_graph()` now wires `<call site> -> reflect -> consolidate
  -> END`, the same shape `agents/summary/graph.py` uses.
"""

from __future__ import annotations

import ast
import json
import keyword
import os
import re
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from pathlib import Path

from aef.harness.vendor_scan import MODEL_SDK_ROOTS, SKIP_DIRS
from aef.harness.zones import (
    ADOPT_SKILL_PATH,
    DEFAULT_AGENT_PATH,
    DEFAULT_AGENT_ROOT,
    LEGACY_AGENT_PATH,
    ZonePolicy,
    inspect_path,
    segment_refusal,
)
from aef.providers.base import CompletionRequest
from aef.reasoning.prompt_agent import (
    DEFAULT_PROMPT_AGENT_DIR,
    MIGRATED_DIR_NAME,
    NATIVE_PROMPT_AGENT_DIRS,
    PromptAgentDefinition,
    PromptAgentError,
    load_agent_file,
)

# Where the generated graph lands, repo-relative (ADR 0143). Derived from
# `DEFAULT_AGENT_ROOT` rather than spelled out: a second literal `agents` here
# is the drift ADR 0091 is about, and the previous default — the repo ROOT —
# was the drift's most expensive form. The root is Zone C, the one tree the
# loop is structurally forbidden to propose changes to, so every candidate
# touching the file this command generated was rejected by G0 before it was
# read.
#
# It is now an ALIAS for `zones.DEFAULT_AGENT_PATH`, not a second derivation
# (ADR 0149). ADR 0143 made this the single place that answered "where does
# the generated graph land?", but the loop's `--agent-path` default answered
# "where is the agent?" separately and still said `agents/demo/graph.py` —
# aef-core's own fixture directory. Two constants for one fact, and the
# adopter's repo satisfied only one of them. One name, one string, both
# questions.
#
# Import this constant; do not re-derive it. `aef/cli/doctor.py` and the
# `aef adopt` templates all read it from here.
DEFAULT_MIGRATED_OUT = DEFAULT_AGENT_PATH

# The pre-ADR-0143 default. NEVER written any more — it is here so that
# `aef doctor` still discovers the graph in a repo migrated by an older
# version, which would otherwise silently drop out of the model-call
# advisory the day this default moved.
#
# An ALIAS, for the same reason `DEFAULT_MIGRATED_OUT` is one: discovery moved
# into `aef.harness.zones` so that `aef doctor` and the loop's own preflight
# share it, and the harness does not import the CLI (ADR 0168).
LEGACY_MIGRATED_OUT = LEGACY_AGENT_PATH

# The keywords a routed node can actually carry, derived from the request type
# **itself** rather than from a second hand-written list.
#
# ADR 0140. The routable predicate used to judge control flow only — no loop,
# no try, no stream — and never asked whether the *request* survives the
# translation. A call passing `system=`, `tools=` or `stop_sequences=` was
# routed, and `CompletionRequest` has no field for any of them, so they were
# dropped with nowhere to put them back. Reading the field names off the
# dataclass means a field added to `CompletionRequest` tomorrow widens this
# predicate on the same commit; a hardcoded copy here is the drift ADR 0091
# is about, and it is the drift that produced this defect.
_REQUEST_FIELDS: frozenset[str] = frozenset(f.name for f in fields(CompletionRequest)) | {
    "messages"
}

# Same source, in declaration order, minus the one the generated node supplies
# from `state.objective` rather than from the call site.
_CARRIED_FIELDS: tuple[str, ...] = tuple(
    f.name for f in fields(CompletionRequest) if f.name != "messages"
)

# Vendor SDK entry points. A function whose body touches one of these is
# wrapping a model call, whatever it happens to be named locally.
#
# The roots come from `aef/harness/vendor_scan.py`, which is also what
# `tests/test_vendor_isolation.py` and the preflight obligation scan with. A
# second list of vendor module names here is the drift ADR 0091 is about.
_VENDOR_METHODS = (
    "messages.create",
    "completions.create",
    "chat.completions.create",
    "generate_content",
)


def _skip(rel: Path) -> bool:
    """Skip vendored trees and every dot-directory.

    Dot-directories are excluded wholesale rather than enumerated. The first
    run of this command against a real repo reported four call sites, and
    *two* of them were duplicate copies living in `.codex/worktrees/` — git
    worktrees of the same files. An enumerated denylist would have needed
    `.codex` added by hand, and then the next tool's cache dir after that.

    **The path is tested relative to the scan root.** It was tested absolutely
    until ADR 0137, which meant a repo that itself lived under any
    dot-directory — `~/.local/src/app`, a git worktree under `.claude/`, a
    checkout in `.build/` — had *every* file skipped and was reported as
    `scanned 0 Python file(s) ... 0 call site(s)`, exit 0. The tool said the
    repo had no model calls because it had refused to look at the repo.
    """
    return any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts)


@dataclass(frozen=True)
class CallSite:
    """One function that wraps a vendor call, and which form it gets."""

    module: str
    function: str
    lineno: int
    evidence: str
    # ADR 0137. `routed` is the choice; `form_reason` is why, and is carried
    # into both the report and the generated docstring so the adopter reads
    # the same sentence in both places.
    routed: bool = False
    form_reason: str = ""
    model: str | None = None
    max_tokens: int | None = None
    # Every keyword the call site passed that `CompletionRequest` can carry,
    # already rendered as source, in the request type's own field order. A
    # routed node emits exactly these and nothing else — if the call passed
    # anything this tuple cannot hold, it is not routed at all (ADR 0140).
    request_args: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Skipped:
    """A call site found and deliberately NOT wrapped."""

    module: str
    function: str
    lineno: int
    reason: str


@dataclass
class MigrateResult:
    sites: list[CallSite] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    scanned_files: int = 0
    written: Path | None = None
    # Set when `--force` overwrote a file that differed from what migrate
    # would have generated — i.e. one somebody edited (ADR 0140).
    backup: Path | None = None
    # The output path as the ZONE CLASSIFIER sees it: repo-relative, POSIX,
    # or `None` when it lands outside the repo entirely. `report()` names the
    # zone of the path it wrote from this, so the adopter is told at the
    # moment of writing whether the loop may ever touch the file (ADR 0143).
    out_relative: str | None = None
    # ADR 0152. Prompt-file agents — `.claude/agents/*.md` — discovered, and
    # the graphs written for them. A separate list from `sites` because they
    # are a different kind of thing found a different way: `sites` come from an
    # AST scan of Python, these from a directory the harness convention names.
    prompt_agents: list[PromptAgentSite] = field(default_factory=list)
    prompt_written: list[Path] = field(default_factory=list)
    prompt_backups: list[Path] = field(default_factory=list)
    # Prompt graphs NOT written because a file was already there (no --force).
    prompt_existing: list[str] = field(default_factory=list)
    # `SKILL.md` files seen and deliberately not migrated — see
    # `discover_skills` for why. Reported, never silently dropped.
    skills_seen: list[str] = field(default_factory=list)
    # The subset of `skills_seen` that `aef adopt` ITSELF wrote. Named as a
    # field rather than recomputed in `report()` because the answer depends on
    # the repo root, and a report is rendered from the result alone. Empty by
    # default, so a hand-built result counts exactly what it used to.
    skills_own: list[str] = field(default_factory=list)
    # The Zone A root the prompt graphs were written under, and the directory
    # the personas were read from. Both are reported, because together they
    # decide whether the loop may propose a change to the persona itself.
    agent_root: str = DEFAULT_AGENT_ROOT
    prompt_agents_dir: str = DEFAULT_PROMPT_AGENT_DIR

    @property
    def total_found(self) -> int:
        return len(self.sites) + len(self.skipped)

    @property
    def routed(self) -> list[CallSite]:
        return [s for s in self.sites if s.routed]

    @property
    def unrouted(self) -> list[CallSite]:
        return [s for s in self.sites if not s.routed]


def _dotted(node: ast.AST) -> str:
    """Render an attribute chain (`a.b.c`) so it can be matched as text."""
    parts: list[str] = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _is_completion_call(name: str) -> bool:
    return any(name.endswith(method) for method in _VENDOR_METHODS)


def _vendor_evidence(fn: ast.AST) -> str | None:
    """The vendor construct this function's body touches, or None."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if not name:
                continue
            root = name.split(".", 1)[0]
            if root in MODEL_SDK_ROOTS:
                return name
            if _is_completion_call(name):
                return name
    return None


@dataclass(frozen=True)
class _Routing:
    """The routed/unrouted decision for one function, and its reason."""

    routed: bool
    reason: str
    model: str | None = None
    max_tokens: int | None = None
    request_args: tuple[tuple[str, str], ...] = ()


def _render_literal(node: ast.AST) -> str | None:
    """The call site's literal value as source a generated node can carry, or
    `None` if it is not a literal this command will reproduce verbatim.

    "Verbatim" is the bar, not "close enough". A `max_tokens=MAX` routed as
    `max_tokens=<the request type's default>` is the same silent substitution
    as dropping `system=`, one step quieter.
    """
    if not isinstance(node, ast.Constant):
        return None
    value = node.value
    if isinstance(value, bool):
        return repr(value)
    if isinstance(value, str):
        # json.dumps, not repr: double quotes and ASCII-safe escapes, both of
        # which are valid Python and match the rest of the generated file.
        return json.dumps(value)
    if isinstance(value, int | float):
        return repr(value)
    return None


def _decorator_names(fn: ast.AST) -> list[str]:
    decorators = getattr(fn, "decorator_list", [])
    names = []
    for dec in decorators:
        rendered = _dotted(dec.func) if isinstance(dec, ast.Call) else _dotted(dec)
        names.append(rendered or "<expression>")
    return names


def _routing_decision(fn: ast.AST, *, direct: bool) -> _Routing:
    """Can this function's model call be routed through `Services` without
    losing something the function already does?

    Every `False` below names a specific thing routing would have dropped.
    That is the point: the falsification clause in ADR 0137 says a routed form
    that loses retries or streaming is a silent rewrite, so the answer when
    anything at all is in the way is the unrouted form plus a warning.

    ADR 0140 widened "anything at all" in four directions, each reproduced
    against a routed node that dropped something real: the request's own
    keywords (`system=`, `tools=`, ...), decorators, `**kwargs` forwarding,
    and a client constructed with arguments. ADR 0137's clause enumerated
    retries, streams and backends — control flow — and never asked whether the
    **request** survives translation into `CompletionRequest`.
    """
    if not direct:
        return _Routing(
            False,
            "it wraps another function that reaches the SDK — whatever retries, budgets "
            "and backend order live down there would be dropped by routing",
        )

    # A decorator is not visible in the body, and this analysis reads bodies.
    # REPRODUCED: a bare `@retry` (an `ast.Name`, not an `ast.Call`) slipped
    # past every rule below — including the "body also calls retry()" one,
    # which only ever caught `@retry(...)` by accident, because a *called*
    # decorator leaves a `Call` node in `decorator_list` for `ast.walk` to
    # find. Retries were dropped, silently, for the bare form.
    decorators = _decorator_names(fn)
    if decorators:
        shown = ", ".join(f"@{name}" for name in decorators)
        return _Routing(
            False,
            f"it is decorated ({shown}) — a decorator can wrap the call in retry, caching, "
            f"rate limiting or tracing that the body does not show, and routing keeps only "
            f"the body",
        )

    calls = [node for node in ast.walk(fn) if isinstance(node, ast.Call)]
    names = [(node, _dotted(node.func)) for node in calls]
    named = [(node, name) for node, name in names if name]

    constructions = [
        (node, name) for node, name in named if name.split(".", 1)[0] in MODEL_SDK_ROOTS
    ]
    if not constructions:
        return _Routing(
            False,
            "the client is injected or global rather than built here — migrate cannot see "
            "which vendor or which model this call uses",
        )

    for node in ast.walk(fn):
        if isinstance(node, ast.Try):
            return _Routing(
                False,
                "its body catches exceptions — a retry or fallback policy that a single "
                "complete() call would silently drop",
            )
        if isinstance(node, ast.For | ast.AsyncFor | ast.While):
            return _Routing(
                False,
                "its body loops — a retry, backoff or pagination policy that a single "
                "complete() call would silently drop",
            )

    # A client built with arguments is a configured client. REPRODUCED with
    # the corporate-gateway shape —
    # `anthropic.Anthropic(base_url="https://llm-gateway.corp/v1",
    # timeout=120.0, max_retries=8)` — which routed happily, so the generated
    # node quietly reached a different endpoint, on a different credential,
    # billed to a different account, with the SDK's own retry policy gone.
    # None of that is expressible through `Services.model_provider`, which
    # resolves its own endpoint and key.
    for node, name in constructions:
        parts = [f"{kw.arg}=" for kw in node.keywords if kw.arg is not None]
        if any(kw.arg is None for kw in node.keywords):
            parts.append("**kwargs")
        if node.args:
            parts.append("positional argument(s)")
        if parts:
            return _Routing(
                False,
                f"it configures its own client — {name}({', '.join(parts)}) sets an endpoint, "
                f"credential, timeout or retry policy that Services.model_provider resolves "
                f"for itself, so routing would send this call somewhere else",
            )

    if any(name.endswith(".stream") or name.endswith(".stream_async") for _, name in named):
        return _Routing(
            False,
            "it streams its reply — ModelProvider.complete() returns once, so routing "
            "would change the shape of the answer your caller reads",
        )

    unknown = sorted(
        {
            name
            for _, name in named
            if name.split(".", 1)[0] not in MODEL_SDK_ROOTS and not _is_completion_call(name)
        }
    )
    if unknown:
        return _Routing(
            False,
            f"its body also calls {unknown[0]}() — it does more than make the call, and "
            f"migrate cannot tell what routing would drop",
        )

    completions = [node for node, name in named if _is_completion_call(name)]
    if not completions:
        return _Routing(
            False,
            "its body constructs a client but makes no completion call migrate recognises, "
            "so there is no request to rebuild",
        )
    if len(completions) > 1:
        return _Routing(
            False,
            f"its body makes {len(completions)} completion calls — a node returns once, and "
            f"which of them is the answer is a semantic decision",
        )

    call = completions[0]

    # REPRODUCED: `client.messages.create(..., **kwargs)` routed, because
    # `ast.keyword` with `arg=None` was filtered out one line below and the
    # rest of the call looked thin. Whatever a caller passes at runtime —
    # `stream=True`, `system=`, `tools=` — is then dropped without trace, and
    # the stream check above is defeated by a caller rather than by the code.
    forwarded = []
    if any(isinstance(arg, ast.Starred) for arg in call.args):
        forwarded.append("*args")
    if any(kw.arg is None for kw in call.keywords):
        forwarded.append("**kwargs")
    if forwarded:
        return _Routing(
            False,
            f"it forwards {' and '.join(forwarded)} into the SDK call — migrate cannot see "
            f"what a caller passes, so a caller supplying stream=, system= or tools= at "
            f"runtime would have it dropped without trace",
        )

    kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}

    stream = kwargs.get("stream")
    if stream is not None and not (isinstance(stream, ast.Constant) and stream.value is False):
        return _Routing(
            False,
            "it passes stream= to the SDK — ModelProvider.complete() returns once, so "
            "routing would change the shape of the answer your caller reads",
        )

    # THE REQUEST ITSELF. Everything above this line judges control flow;
    # ADR 0137 stopped here and called the result "nothing to lose". A call
    # passing `system="You are a claims adjuster. NEVER approve a payout above
    # $5,000."` alongside `tools=` and `stop_sequences=` was routed, and
    # `CompletionRequest` has no field for any of the three — so the generated
    # docstring's "yours to re-express" named a place that does not exist.
    # (`ProviderMessage` does carry a `system` ROLE, which both adapters fold
    # into the vendor's system parameter — but this node replaces the whole
    # message list with `state.objective`, so pairing a literal system prompt
    # with a substituted user turn is a semantic decision, not plumbing.)
    #
    # `stream` is excluded because it has its own refusal above; the only
    # value that reaches here is a literal `stream=False`, which routing
    # preserves exactly rather than drops.
    inexpressible = sorted(
        {name for name in kwargs if name not in _REQUEST_FIELDS and name != "stream"}
    )
    if inexpressible:
        # `system=` is named on its own: it is not a tuning knob, it is the
        # instruction the adopter's behaviour depends on.
        if "system" in inexpressible:
            others = [f"{name}=" for name in inexpressible if name != "system"]
            also = f" (and {', '.join(others)})" if others else ""
            return _Routing(
                False,
                f"it passes system= to the SDK{also} — a system prompt is an instruction your "
                f"code relies on, and CompletionRequest has no system field; carrying it would "
                f"mean synthesising a system-role message to pair with the objective this node "
                f"substitutes, which is a semantic decision migrate will not make for you",
            )
        listed = ", ".join(f"{name}=" for name in inexpressible)
        them = "them" if len(inexpressible) > 1 else "it"
        return _Routing(
            False,
            f"it passes {listed} to the SDK, and CompletionRequest cannot express {them} — "
            f"routing would drop {them} with nowhere to re-express {them}",
        )

    model = kwargs.get("model")
    if not (isinstance(model, ast.Constant) and isinstance(model.value, str)):
        return _Routing(
            False,
            "the model id is not a literal at this call — migrate will not guess which "
            "model your code asks for",
        )

    # Expressible is necessary and not sufficient: the value has to be one this
    # command can reproduce verbatim. `max_tokens=MAX` used to be routed as no
    # max_tokens at all, which silently substituted the request type's default.
    request_args: list[tuple[str, str]] = []
    for name in _CARRIED_FIELDS:
        value = kwargs.get(name)
        if value is None:
            continue
        rendered = _render_literal(value)
        if rendered is None:
            return _Routing(
                False,
                f"its {name}= is not a literal at this call, so routing would have to guess "
                f"the value or drop it — and CompletionRequest.{name} has a default that "
                f"would silently take its place",
            )
        request_args.append((name, rendered))

    max_tokens_node = kwargs.get("max_tokens")
    max_tokens = (
        max_tokens_node.value
        if isinstance(max_tokens_node, ast.Constant) and isinstance(max_tokens_node.value, int)
        else None
    )
    return _Routing(
        True,
        "its body builds an unconfigured client and makes exactly one completion call — no "
        "decorator, no loop, no try/except, no stream, no **kwargs, and every keyword it "
        "passes is one CompletionRequest carries verbatim",
        model=model.value,
        max_tokens=max_tokens,
        request_args=tuple(request_args),
    )


def _scan_module(path: Path, root: Path) -> tuple[list[CallSite], list[Skipped]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        # Unreadable or not Python-3-parseable. Not a call site, and not worth
        # failing the whole scan over — but do not pretend it was scanned.
        return [], []

    rel = path.relative_to(root)
    module = ".".join(rel.with_suffix("").parts)
    sites: list[CallSite] = []
    skipped: list[Skipped] = []

    # Methods are recorded as skipped rather than ignored: "we saw it and
    # cannot wrap it" is information; silence is not.
    method_lines = {
        child.lineno
        for cls in ast.walk(tree)
        if isinstance(cls, ast.ClassDef)
        for child in cls.body
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
    }

    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]

    # Prefer the OUTERMOST wrapper, not the function holding the raw SDK call.
    #
    # Reproduced on a real repo: the first version of this scan wrapped
    # `research._llm_client._call_api` — the private function that constructs
    # `anthropic.Anthropic()` — while the repo's actual seam is
    # `call_llm_with_backend`, the public function above it carrying the
    # retries, daily budgets and backend order. Wrapping the inner one bypasses
    # every one of those, which is exactly what this module's docstring says not
    # to do. The docstring was right and the code did the opposite.
    #
    # So: find functions that reach a vendor call transitively within the
    # module, then keep only those nothing else in the module calls.
    direct = {fn.name: ev for fn in functions if (ev := _vendor_evidence(fn)) is not None}
    if not direct:
        return [], []

    calls: dict[str, set[str]] = {}
    for fn in functions:
        named: set[str] = set()
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Call):
                target = _dotted(sub.func)
                if target:
                    named.add(target.split(".")[-1])
        calls[fn.name] = named

    reaching = dict(direct)
    for _ in range(len(functions)):  # bounded; converges well before this
        grown = False
        for name, targets in calls.items():
            if name in reaching:
                continue
            hit = targets & reaching.keys()
            if hit:
                reaching[name] = f"{next(iter(sorted(hit)))} (transitively)"
                grown = True
        if not grown:
            break

    called_by_a_reacher = {
        target for name, targets in calls.items() if name in reaching for target in targets
    }
    outermost = {n: ev for n, ev in reaching.items() if n not in called_by_a_reacher}
    chosen = outermost or direct

    for node in functions:
        if node.name not in chosen:
            continue
        evidence = chosen[node.name]
        if isinstance(node, ast.AsyncFunctionDef):
            skipped.append(
                Skipped(
                    module,
                    node.name,
                    node.lineno,
                    "async — the node contract and ModelProvider.complete() are sync",
                )
            )
        elif node.lineno in method_lines:
            skipped.append(
                Skipped(
                    module,
                    node.name,
                    node.lineno,
                    "method — needs an instance this command cannot construct",
                )
            )
        elif any(isinstance(sub, ast.Yield | ast.YieldFrom) for sub in ast.walk(node)):
            skipped.append(
                Skipped(
                    module,
                    node.name,
                    node.lineno,
                    "generator — yields repeatedly; a node returns once",
                )
            )
        else:
            routing = _routing_decision(node, direct=node.name in direct)
            sites.append(
                CallSite(
                    module,
                    node.name,
                    node.lineno,
                    evidence,
                    routed=routing.routed,
                    form_reason=routing.reason,
                    model=routing.model,
                    max_tokens=routing.max_tokens,
                    request_args=routing.request_args,
                )
            )

    return sites, skipped


def scan(root: Path) -> MigrateResult:
    """Find every function in `root` whose body wraps a vendor SDK call."""
    result = MigrateResult()
    paths: list[Path] = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        # Prune before traversal: filtering rglob's results still enumerates
        # every dependency in .venv/node_modules in large adopting repos.
        dirs[:] = sorted(
            name
            for name in dirs
            if not _skip((base / name).relative_to(root)) and not (base / name).is_symlink()
        )
        paths.extend(
            base / name
            for name in files
            if name.endswith(".py") and not _skip((base / name).relative_to(root))
        )
    for path in sorted(paths):
        if path.is_symlink():
            result.skipped.append(
                Skipped(
                    ".".join(path.relative_to(root).with_suffix("").parts),
                    "<module>",
                    1,
                    "symlink Python file — source must remain inside the selected repository",
                )
            )
            continue
        if not path.is_file():
            continue
        result.scanned_files += 1
        sites, skipped = _scan_module(path, root)
        result.sites.extend(sites)
        result.skipped.extend(skipped)
    return result


DEFAULT_SKILLS_DIR = ".claude/skills"


@dataclass(frozen=True)
class PromptAgentSite:
    """One `.claude/agents/*.md` persona, and the graph generated for it."""

    definition: PromptAgentDefinition
    # Repo-relative POSIX path of the persona file.
    source: str
    # The sanitised directory name the graph lands in, and therefore the last
    # importable component of `aef run agents.migrated.<module>.graph`.
    module: str
    # Repo-relative POSIX path of the generated graph.
    out_relative: str
    # Why the persona's own `name:` could not be used as the graph id, or `""`
    # when it could. A graph id is JOINED ONTO A DIRECTORY by
    # `aef/harness/archive.py`, so it must be one safe path segment; the
    # persona name is whatever a markdown file's frontmatter says.
    unsafe_name_reason: str = ""
    graph_id_override: str = ""

    @property
    def graph_id(self) -> str:
        """The graph's id — the persona's name when that is a safe path
        segment, and the sanitised module name when it is not.

        ADR 0168. `graph_id` reaches `archive._graph_dir` as `root / graph_id`,
        and a persona whose frontmatter said `name: ../escape` produced
        `graph_id='../escape'` printed verbatim in this report as the value to
        hand `aef loop bless` — which then wrote `state/escape/v000001/` one
        level ABOVE the archive root and left the archive root empty
        (reproduced). Archive refuses such an id now; this stops one being
        minted in the first place, which is the half that keeps the agent
        migrated instead of failing later.

        The `module` is used rather than a freshly sanitised name because it is
        already collision-disambiguated across this run and is the directory
        the operator can see on disk. `AGENT_NAME` in the generated module is
        untouched: the persona's own name is what the model is told it is, and
        only the id that becomes a directory has to be a path segment.
        """
        if self.graph_id_override:
            return self.graph_id_override
        if self.unsafe_name_reason:
            return self.module
        return self.definition.name

    @property
    def dotted(self) -> str:
        return self.out_relative.removesuffix(".py").replace("/", ".")

    @property
    def importable(self) -> bool:
        """Is `dotted` a name `importlib` could actually resolve?

        Every component has to be an identifier and not a keyword. Under the
        default root it always is. Under `--agent-root .claude/agents` —
        ADR 0152 §4's opt-in, and the only way to put a persona in Zone A —
        the first component is `.claude`, and no dotted spelling of that path
        exists.
        """
        parts = self.dotted.split(".")
        return bool(parts) and all(
            part.isidentifier() and not keyword.iskeyword(part) for part in parts
        )

    @property
    def run_target(self) -> str:
        """What to put after `aef run`, and it must be RUNNABLE.

        ADR 0168, erratum on ADR 0152. The report printed `aef run
        {self.dotted}` unconditionally, so under the widened root it printed

            aef run .claude.agents.migrated.marlin_accela.graph

        which exits 1 with `the 'package' argument is required to perform a
        relative import` — a leading dot is a relative import to `importlib`.
        `aef run` (and `aef loop record`) now take a FILE PATH as well, which is
        the spelling that works for any root an adopter may choose, so that is
        what is printed when the dotted form cannot be imported.
        """
        return self.dotted if self.importable else self.out_relative


def _module_name(name: str) -> str:
    """A persona name as an importable module component.

    `marlin-accela` is not a module. Lowercased, every run of non-identifier
    characters folded to one `_`, and a leading digit or a Python keyword
    prefixed — because `aef run agents.migrated.<this>.graph` has to import
    it, and a directory named `2fa` or `class` cannot be imported at all.
    """
    slug = re.sub(r"[^0-9a-zA-Z_]+", "_", name.strip().lower()).strip("_")
    if not slug:
        slug = "agent"
    if slug[0].isdigit() or keyword.iskeyword(slug) or keyword.issoftkeyword(slug):
        slug = f"agent_{slug}"
    return slug


# The generated prompt-agent graph's node order, as the report prints it. A
# test asserts the rendered module declares exactly these nodes in this order,
# so the report cannot describe a three-node graph while writing a four-node
# one (ADR 0183 found exactly that).
PROMPT_AGENT_WIRING = "retrieve -> prompt_agent -> reflect -> consolidate -> END"


def _existing_prompt_graphs(
    root: Path, agent_root: str
) -> tuple[dict[str, tuple[str, str]], set[str], set[str]]:
    """Recover prior persona identities without importing any adopter code.

    A new earlier-sorting persona must not steal an existing graph path. The
    generated module already records its source and graph id as literals;
    retain that mapping even when another persona has the same name.
    """
    base = root / agent_root / MIGRATED_DIR_NAME
    if any(
        part.is_symlink()
        for part in (base, *base.parents)
        if part != root and part.is_relative_to(root)
    ):
        raise PromptAgentError(f"refusing symlink migrated graph directory: {base}")
    previous: dict[str, tuple[str, str]] = {}
    used: set[str] = set()
    graph_ids: set[str] = set()
    for path in sorted(base.glob("*/graph.py")):
        if path.is_symlink() or path.parent.is_symlink():
            raise PromptAgentError(f"refusing symlink migrated graph: {path}")
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError):
            continue
        constants: dict[str, str] = {}
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                constants[node.targets[0].id] = node.value.value
        source, graph_id = constants.get("AGENT_FILE"), constants.get("GRAPH_ID")
        module = path.parent.name
        if source and graph_id and not segment_refusal(graph_id) and module == _module_name(module):
            previous.setdefault(source, (module, graph_id))
            # Earlier versions could duplicate one source under several
            # paths. Preserve all occupied identities, including those we
            # no longer select as that persona's canonical mapping.
            used.add(module)
            graph_ids.add(graph_id)
    return previous, used, graph_ids


def discover_prompt_agents(
    root: Path,
    *,
    agents_dir: str = DEFAULT_PROMPT_AGENT_DIR,
    agent_root: str = DEFAULT_AGENT_ROOT,
) -> list[PromptAgentSite]:
    """Every persona file under `agents_dir`, one graph path each.

    **Recursive**, because the Claude Code CLI is: a scratch repo holding
    `.claude/agents/probe-one.md` and `.claude/agents/sub/probe-two.md` had
    the CLI list *both* (`claude -p --agent <unknown>` names every agent it
    found, and is rejected before any model call — so this was measured, not
    assumed). A flat glob would have migrated some of an adopter's agents and
    silently left the organised ones behind.

    Its own output tree is excluded by name: `migrate` under the agents
    directory is what this command writes when the adopter widens Zone A to
    `.claude/agents`, and a second run must not read its own graphs.

    Nothing here is filtered on "looks like an agent". A `.md` under
    `.claude/agents` is one, by the convention that directory *is*; a file
    with no frontmatter gets its filename as its name (see
    `aef.reasoning.prompt_agent`), which is what the harness does too.
    """
    directories = (
        NATIVE_PROMPT_AGENT_DIRS if agents_dir == DEFAULT_PROMPT_AGENT_DIR else (agents_dir,)
    )
    definitions: list[tuple[str, PromptAgentDefinition]] = []
    for directory in directories:
        base = root / directory
        if any(
            part.is_symlink()
            for part in (base, *base.parents)
            if part != root and part.is_relative_to(root)
        ):
            raise PromptAgentError(f"refusing symlink agent directory: {directory}")
        if not base.is_dir():
            continue
        suffix = ".toml" if directory == ".codex/agents" else ".md"
        for path in sorted(base.rglob(f"*{suffix}")):
            rel = path.relative_to(root).as_posix()
            if MIGRATED_DIR_NAME in path.relative_to(base).parts[:-1]:
                continue
            if any(
                part.is_symlink()
                for part in (path, *path.parents)
                if part != root and part.is_relative_to(root)
            ):
                raise PromptAgentError(f"refusing symlink agent file: {rel}")
            if not path.is_file():
                raise PromptAgentError(f"refusing nonregular agent file: {rel}")
            definitions.append((rel, load_agent_file(path, source=rel)))

    sites: list[PromptAgentSite] = []
    previous, used, graph_ids = _existing_prompt_graphs(root, agent_root)
    reserved = {_module_name(definition.name) for _, definition in definitions}
    reserved.update(definition.name for _, definition in definitions)
    for rel, definition in definitions:
        reason = segment_refusal(definition.name)
        if rel in previous:
            module, graph_id = previous[rel]
        else:
            stem = _module_name(definition.name)
            module = stem
            index = 1
            while module in used or module in graph_ids or (module != stem and module in reserved):
                index += 1
                module = f"{stem}_{index}"
            graph_id = module if reason or definition.name in graph_ids else definition.name
        used.add(module)
        graph_ids.add(graph_id)
        sites.append(
            PromptAgentSite(
                definition=definition,
                source=rel,
                module=module,
                out_relative=f"{agent_root}/{MIGRATED_DIR_NAME}/{module}/graph.py",
                unsafe_name_reason=reason,
                graph_id_override=graph_id if graph_id != definition.name else "",
            )
        )
    return sites


def discover_skills(root: Path, *, skills_dir: str = DEFAULT_SKILLS_DIR) -> list[str]:
    """`SKILL.md` paths found, **and deliberately not migrated** (ADR 0152).

    A skill is not an agent, and running one as a persona would produce a
    convincing wrong thing:

    - A `SKILL.md` body is *instructions injected into a session already in
      progress* when its description matches. It presumes the session's own
      task, tools and files. A persona body is the whole of who the agent is.
    - Skills routinely reference bundled material — `references/*.md`,
      scripts, templates — that a **tool-less single completion cannot open**
      (the safety property `make_prompt_agent_node` documents). A graph built
      from `SKILL.md` would send an instruction sheet stripped of the half it
      points at, and return an answer that looks like an agent's.
    - A skill has no objective of its own, so `state.objective` — the user
      turn every generated graph sends — has nothing to be about.

    So they are *counted and named* in the report rather than skipped
    silently, which is the same rule the call-site scanner follows for the
    functions it declines to wrap.

    **`aef adopt`'s own `SKILL.md` is still returned here and is NOT the
    adopter's** — see `discover_adopter_skills`, which is the count the report
    prints. This function stays the raw listing because `aef migrate`'s job is
    to name every file it declined to migrate, including aef's own.
    """
    base = root / Path(skills_dir)
    if not base.is_dir():
        return []
    # `<skills>/<name>/SKILL.md`, one level, which is the convention exactly.
    # `rglob` found ten on the pilot instead of six: two were fixtures *inside*
    # a skill's own test corpus. Counting those would have made the report's
    # first number wrong about the repo it was describing.
    return sorted(p.relative_to(root).as_posix() for p in base.glob("*/SKILL.md"))


def is_adopt_skill(root: Path, relative_path: str) -> bool:
    """Is this the `SKILL.md` `aef adopt` wrote, rather than the adopter's?

    THE RULE, and it is `aef/cli/adopt.py`'s, derived from the one string in
    `aef.harness.zones` rather than re-spelled here (ADR 0149). Resolved on
    both sides so a symlinked skills directory answers the same way adopt's
    own exclusion does.
    """
    ours = (root / ADOPT_SKILL_PATH).resolve()
    return (root / relative_path).resolve() == ours


def discover_adopter_skills(root: Path, *, skills_dir: str = DEFAULT_SKILLS_DIR) -> list[str]:
    """`discover_skills` minus aef's own output — the adopter's skill surface.

    THE DEFECT (H1's finding 1, ADR 0172 D4; reproduced in ADR 0176). Adopt
    applied this exclusion and migrate did not, so on the pilot clone the same
    tree measured **5 from adopt and 6 from migrate** — and after `aef adopt`
    the sixth was the `new-model-check/SKILL.md` adopt had just written. A
    scaffold that counts its own output as the adopter's surface reports a
    different number on every run.
    """
    return [p for p in discover_skills(root, skills_dir=skills_dir) if not is_adopt_skill(root, p)]


_PROMPT_GRAPH = '''"""{headline}

Source of truth:

    {source}

This module does not copy the persona; it names it, and
`make_prompt_agent_node` reads that file **at execution time**. So an edit to
the `.md` changes what the next run sends, with no regeneration step in
between — which is what makes it worth putting the `.md` in Zone A.

CONTAINMENT DEPENDS ON `model_provider.impl`, AND THIS RUN RECORDS WHICH ONE
IT GOT. The persona body becomes the `system` message of one
`CompletionRequest` and `state.objective` is the user turn. What happens next
is the provider's, not this module's (ADR 0169):

  claude_code  tool-suppressed by its own argv: `--tools ""` (its `--help`:
               'Use "" to disable all tools'), `--max-turns 1`, `--safe-mode`,
               and `--strict-mcp-config` with an empty server set. The persona
               goes in `--system-prompt`.
  grok         `--max-turns 1`, `--disable-web-search`, `--no-subagents`;
               persona in `--system-prompt-override`. Its `--tools ""` is
               MEASURED to suppress nothing — given one more turn the same
               argv read a planted file and quoted it back — so what contains
               a tool-using persona is the turn cap cancelling the run, and
               the provider raises instead of answering. It has no
               `--safe-mode`: ~17.9k tokens of project instructions still
               reach the call.
  codex        `--sandbox read-only` and nothing that suppresses tools: no
               `--tools`, no `--max-turns`. It has no system-prompt flag, so
               the persona is prepended to the USER turn.
  command      enforces exactly what your argv template says and nothing else.
               Its `isolation:` list is YOUR assertion, recorded unverified;
               with no `{{system}}` slot the persona goes in the USER turn.
  anthropic    an API call sent with no tools parameter; persona in `system=`.

The frontmatter `tools:` key is read and never honoured under any of
them.{unhonoured}

WHERE TO LOOK: every run writes the provider's declared isolation set and the
persona's channel to `state.working_memory["prompt_agent__containment"]`, and
adds a `prompt_agent.persona_in_user_turn` warning under that record's
`warning` key when the persona went out in the user turn. It is a containment
FACT about the provider you installed, not an error against the run, so it
does not zero the task metric and never becomes a bullet in your persona
(ADR 0179). `aef.reasoning.prompt_agent.containment_warnings(state)` reads
them back. Per-run evidence beats this comment.

WIRED `retrieve -> prompt_agent -> reflect -> consolidate -> END`, a cycle
with both halves:

  retrieve     asks `Services.retriever` for lessons this agent's earlier runs
               produced and puts them on `state.retrieved_context`. The prompt
               agent renders them into the USER turn, after the objective —
               the persona stays the system message and is never modified at
               runtime. With nothing retrieved the request is byte-identical
               to one made with no retrieve node at all.
  reflect      the only thing that writes the failure memory the self-rewiring
               loop's proposer reads, and the thing that records WHICH lessons
               were in context when the run went the way it went. Route the
               first node to `END` instead and the loop does not break, it
               goes silent — `aef loop cycle` exits 0 with `no admissible
               failure memory: no candidate this cycle`, every cycle (ADR
               0139/0143). Drop the retrieve node and the loop still runs, but
               `retrieved_signatures` is `[]` on every record forever and
               every lesson's helpful/harmful tally is a constant 0/0 (ADR
               0179, R6).
  consolidate  folds repeated records into `Services.knowledge`, which is what
               the retrieve node reads on the next run.

Running it needs `retriever`, `critic`, `judge`, `memory` and `knowledge` on
`Services`; `aef.services.runtime.agent_services()` supplies all five, so `aef
run --config`, `aef loop bootstrap` and the gates are unaffected. A hand-built
bare `Services(model_provider=...)` is not.

Regenerate with `aef migrate --dir .`; it never overwrites without --force.
"""

from __future__ import annotations

from aef.kernel import END, Edge, Graph
from aef.reasoning.nodes import make_consolidate_node, make_reflect_node, make_retrieve_node
from aef.reasoning.prompt_agent import make_prompt_agent_node

AGENT_NAME = {agent_name_literal}
AGENT_FILE = {source_literal}
# The graph's id is the persona's name whenever that name is one safe path
# segment. It is JOINED ONTO A DIRECTORY — `aef/harness/archive.py` builds
# `<archive root>/<graph id>/v000001/` — so a name that is not (`../escape`,
# `a/b`) is replaced here by the sanitised module name, and `aef migrate`'s
# report says which name was refused and why (ADR 0168).
GRAPH_ID = {graph_id_literal}


def build_graph() -> Graph:
    return Graph(
        id=GRAPH_ID,
        version="0.1.0",
        nodes={{
            # Before the work, not after: the lessons have to be in context
            # when the persona answers, or they are a record of learning
            # rather than an instance of it (ADR 0118/0179).
            "retrieve": make_retrieve_node(route="prompt_agent"),
            "prompt_agent": make_prompt_agent_node(
                agent_file=AGENT_FILE,
                agent_name=AGENT_NAME,
                # Repo-relative resolution falls back to walking up from here,
                # so the graph still finds its persona when the gates run it
                # from a materialised candidate workspace.
                module_file=__file__,
                route="reflect",
            ),
            "reflect": make_reflect_node(route="consolidate"),
            "consolidate": make_consolidate_node(route=END),
        }},
        edges=[
            Edge(from_node="retrieve", to_node="prompt_agent"),
            Edge(from_node="prompt_agent", to_node="reflect"),
            Edge(from_node="reflect", to_node="consolidate"),
        ],
        entry_node="retrieve",
    )
'''


def render_prompt_agent(site: PromptAgentSite, repo_name: str) -> str:
    """The generated graph module for one persona."""
    unhonoured = ""
    if site.definition.unhonoured_keys:
        keys = ", ".join(f"`{k}`" for k in site.definition.unhonoured_keys)
        unhonoured = _wrap(f"Frontmatter keys read and NOT acted on: {keys}.", indent="")
        unhonoured = f"\n\n{unhonoured}"
    # Wrapped at 97, not 100: the opening `"""` sits on the same line, and a
    # repo name plus a persona name is enough to push it over on its own —
    # `test_the_generated_graph_passes_the_repos_own_ruff` caught seven E501s
    # here on names no longer than the pilot's.
    headline = textwrap.fill(
        f"Generated by `aef migrate` for {repo_name} — the prompt agent `{site.definition.name}`.",
        width=97,
    )
    return _PROMPT_GRAPH.format(
        headline=headline,
        source=site.source,
        # `json.dumps` rather than `!r`: it emits double quotes, which is what
        # the adopter's own `ruff format` run will want, and a generated file
        # that fails the lint of the repo it lands in is a chore handed over
        # rather than work done (the same rule `_key_fn_kwarg` follows).
        agent_name_literal=json.dumps(site.definition.name),
        graph_id_literal=json.dumps(site.graph_id),
        source_literal=json.dumps(site.source),
        unhonoured=unhonoured,
    )


_NO_SITES_BODY = """
# No wrappable call site was found. See the command's report for what was
# skipped and why — an empty file here is a finding, not a failure.


def build_graph() -> Graph:
    raise NotImplementedError(
        "aef migrate found no wrappable call site in this repo. Nothing was "
        "generated, deliberately, rather than emitting a graph that does nothing."
    )
"""


_DOCSTRING = """
One node per call site found — a function whose body touches a vendor SDK.

TWO FORMS, AND THIS FILE SAYS WHICH IT CHOSE. A node marked ROUTED asks
`services.require_model_provider()` and does NOT call your function: the call
is then visible to the policy engine, the fallback chain and the harness
cassette, and whatever CONTROL FLOW your function had around the call is yours
to re-express. Its REQUEST is carried, not re-expressed: a call is routed only
when every keyword it passed is one `CompletionRequest` carries verbatim, and
those keywords are reproduced in the node body. A node marked UNROUTED calls
your function unchanged — nothing is lost, and the model call stays invisible
to the harness, which is stated in that node's own docstring rather than left
to be discovered at gate time.

EVERY NODE HERE DECLARES `side_effects=SideEffect.EXTERNAL_CALL`, because
every one of them reaches a model. See `_idempotency_key` below for what the
key that declaration requires does and does not buy you.

AND EVERY NODE ROUTES TO `reflect`, WHICH IS WHAT MAKES THIS REPO LEARN.
`build_graph()` wires `<call site> -> reflect -> consolidate -> END`. The
reflect node is the only thing in the runtime that writes failure memory, and
failure memory is the only evidence the self-rewiring loop's proposer will
act on. Route a node back to `END` instead and nothing raises: `aef loop
cycle` just exits **0** with `no admissible failure memory: no candidate this
cycle`, every cycle, which reads like success. See `build_graph`'s own
docstring for what the tail costs a caller who builds `Services` by hand.

WHAT THIS FILE IS NOT: a finished migration. Each node below passes the
objective through as a single prompt and stores the result. If your function
takes other arguments, or its result needs shaping into `StateDelta`, that is
yours to write — this file gets the plumbing and the imports right, not the
semantics. The routed/unrouted choice is likewise a reading of your code, not
a judgement about it: re-route an UNROUTED node by hand once you have decided
what its retries and backend selection should become.

Regenerate with `aef migrate --dir .`; it never overwrites without --force.
\"\"\"

"""


def _header(result: MigrateResult, repo_name: str) -> str:
    """The module docstring and imports.

    Imports are conditional on which forms were actually generated. An unused
    `from typing import Any` is an `F401` in the adopter's own lint run, and a
    generated file that fails the lint of the repo it lands in is a chore
    handed over rather than work done.
    """
    docstring = f'"""Generated by `aef migrate` for {repo_name}. Review before use.\n' + _DOCSTRING

    imports = ["from __future__ import annotations", ""]
    if not result.sites:
        # The refusal body defines `build_graph()` and nothing else, so every
        # other name would be unused. This imported all eight of them.
        return docstring + "\n".join(imports + ["from aef.kernel import Graph"]) + "\n"
    imports += ["from collections.abc import Callable"]
    if result.unrouted:
        imports += ["from typing import Any"]
    imports += [
        "",
        "from aef.kernel import END, Context, Edge, Graph, Node, Route, Services, SideEffect",
    ]
    if result.routed:
        imports += ["from aef.providers.base import CompletionRequest, ProviderMessage"]
    imports += [
        "from aef.reasoning.nodes import make_consolidate_node, make_reflect_node",
        "from aef.state import AEFState, StateDelta",
    ]

    return docstring + "\n".join(imports) + "\n"


_KEY_FN = '''

def _idempotency_key(node_id: str) -> Callable[[AEFState], str]:
    """The key the executor computes ONCE per node execution (ADR 0010).

    Every node in this file declares `side_effects=SideEffect.EXTERNAL_CALL`,
    because every one of them reaches a model — and the node contract requires
    an `idempotency_key_fn` from anything that is not pure. Before ADR 0140
    these nodes declared nothing, so they defaulted to PURE: a live model call
    labelled as having no effect on the world.

    This generator supplies the key rather than leaving the file unbuildable,
    and this docstring is what you are owed in exchange for a choice made on
    your behalf:

    - The key is stable across the attempts of ONE execution and differs
      between executions, which is what makes a repeat traceable to its cause.
    - It does NOT make repeating the call free. `ModelProvider.complete()`
      accepts no idempotency key, so nothing downstream deduplicates on this
      one: a second attempt is a second billed call returning a different
      answer. `aef`'s bounded-retry transformation reads the declaration on
      each node below, and will now propose retrying it — decide whether that
      is acceptable for your model spend before accepting such a candidate.
    """
    return lambda state: f"{node_id}:{state.run_id}:{state.checkpoint_seq}"
'''


def _key_fn_kwarg(node_id: str, indent: str) -> str:
    """The `idempotency_key_fn=` line, wrapped if the node id is long enough
    to push it past 100 columns. A generated file that fails the lint of the
    repo it lands in is a chore handed over, and node ids are derived from the
    adopter's module paths — `src_services_llm_client__call_with_backend` is
    an ordinary one and is 44 characters.
    """
    one_line = f'{indent}idempotency_key_fn=_idempotency_key("{node_id}"),'
    if len(one_line) <= 100:
        return one_line
    return f'{indent}idempotency_key_fn=_idempotency_key(\n{indent}    "{node_id}"\n{indent}),'


def _edge_line(node_id: str, indent: str) -> str:
    """The `Edge(from_node=..., to_node="reflect")` line, wrapped when the node
    id pushes it past 100 columns.

    Same reason as `_key_fn_kwarg`, and found the same way: the generated-file
    ruff test failed at 125 columns on `src/services/llm/
    anthropic_backend_client.py` + `call_llm_with_backend_and_budget`, a node
    id this repo already had a regression test for.
    """
    one_line = f'{indent}Edge(from_node="{node_id}", to_node="reflect"),'
    if len(one_line) <= 100:
        return one_line
    return (
        f"{indent}Edge(\n"
        f'{indent}    from_node="{node_id}",\n'
        f'{indent}    to_node="reflect",\n'
        f"{indent}),"
    )


def _wrap(text: str, *, indent: str = "    ") -> str:
    """Reflow a reason into the generated docstring inside 100 columns.

    The reasons are whole sentences on purpose — an adopter reads them in the
    report and in the node they landed on — and one of them is 171 characters.
    A generated file that fails the lint of the repo it lands in is a chore
    handed over, so the width is enforced here rather than hoped for.
    """
    return textwrap.fill(text, width=100, initial_indent=indent, subsequent_indent=indent).lstrip()


def _render_routed(site: CallSite, node_id: str) -> str:
    # Every keyword the call site passed, verbatim — not a chosen subset. The
    # predicate refuses to route at all unless this tuple is the whole of it.
    carried = "".join(f"\n            {name}={value}," for name, value in site.request_args)
    reason = _wrap(f"Routed because {site.form_reason}.")
    replaces = _wrap(f"Replaces `{site.module}.{site.function}` (line {site.lineno}).")
    bypassed = _wrap(
        f"`{site.module}.{site.function}` IS NOT CALLED by this node — it is bypassed."
    )
    return f'''

def {node_id}(
    state: AEFState, ctx: Context, services: Services
) -> tuple[StateDelta, Route]:
    """ROUTED node for one of this repo's model call sites.

    {replaces}
    Detected by: `{site.evidence}`
    {reason}

    {bypassed}
    Its control flow around the call (retries, budget accounting, backend
    selection, logging) is now yours to re-express here. Its REQUEST is not:
    migrate routes a call only when every keyword it passed is one
    `CompletionRequest` carries verbatim, and the keywords it passed are
    reproduced below. A call carrying anything else — `system=`, `tools=`,
    `stop_sequences=` — is never routed at all, rather than routed without it
    (ADR 0140).

    What you get for the bypass is a call the harness can see: the policy
    engine gates it, the fallback chain covers it, the harness login pays for
    it, and `aef loop record` captures it so the gates can replay the scenario
    without a credential.
    """
    result = services.require_model_provider().complete(
        CompletionRequest(
            messages=(ProviderMessage(role="user", content=state.objective),),{carried}
        )
    )
    key = "{node_id}"
    return StateDelta(working_memory={{key: result.content}}), "reflect"
'''


def _render_unrouted(site: CallSite, node_id: str) -> str:
    reason = _wrap(f"Not routed because {site.form_reason}.")
    wraps = _wrap(f"Wraps `{site.module}.{site.function}` (line {site.lineno}), unchanged.")
    # A deep module path plus a long function name pushes a one-line import
    # past 100 columns, and the adopter's own ruff run is where that lands.
    import_line = f"    from {site.module} import {site.function}"
    if len(import_line) > 100:
        import_line = f"    from {site.module} import (\n        {site.function},\n    )"
    return f'''

def {node_id}(
    state: AEFState, ctx: Context, services: Services
) -> tuple[StateDelta, Route]:
    """UNROUTED wrapper for one of this repo's model call sites.

    {wraps}
    Detected by: `{site.evidence}`
    {reason}

    WARNING — this node calls your function, and your function reaches the
    vendor SDK itself. That call does NOT pass through
    `Services.model_provider`, so the policy engine never sees it, the
    fallback chain cannot cover it, and it still needs your own API key.
    `aef loop record` captures no RecordedCall for it either, so any scenario
    recorded from this node carries an empty cassette and replay with
    on_miss="fail" has nothing to serve — the gates will reach the vendor live
    or score the scenario 0.

    Routing it is a trade, not a free win: see the reason above for what
    `complete()` would have dropped. Decide it deliberately, then rewrite this
    body to call `services.require_model_provider().complete(...)`.
    """
{import_line}

    result: Any = {site.function}(state.objective)
    key = "{node_id}"
    return StateDelta(working_memory={{key: result}}), "reflect"
'''


def node_id_for(site: CallSite) -> str:
    """The generated node's id for one call site.

    One function, two readers: `render` writes the graph and `report` tells
    the adopter which node is the entry and which are unreachable. They used
    to be one expression inlined in `render`, so `report` could not name a
    node at all — and a report that cannot name the entry node is how three
    call sites became one reachable node and two dead ones, silently
    (ADR 0149).
    """
    return f"{site.module.replace('.', '_')}__{site.function}"


def render(result: MigrateResult, repo_name: str) -> str:
    """The generated module. One node per call site, in one of two forms."""
    header = _header(result, repo_name)
    if not result.sites:
        return header + _NO_SITES_BODY

    body = [header, _KEY_FN]
    node_ids: list[str] = []
    for site in result.sites:
        node_id = node_id_for(site)
        node_ids.append(node_id)
        body.append(
            _render_routed(site, node_id) if site.routed else _render_unrouted(site, node_id)
        )

    # ADR 0140: `side_effects` was omitted entirely, so every generated node
    # defaulted to `SideEffect.PURE` — a live model call declared to have no
    # effect on the world. `add_bounded_retry` reads that declaration from
    # source before wrapping a node in a 3-attempt retry, and its own comment
    # says it "requires the declaration rather than assuming it"; the
    # generator had never written one, so the guard was asking a question
    # nobody had answered and getting the safest-sounding wrong answer.
    listed = "\n".join(
        f'            "{n}": Node(\n'
        f'                id="{n}",\n'
        f'                version="0.1.0",\n'
        f"                fn={n},\n"
        f"                deterministic=False,\n"
        f"                side_effects=SideEffect.EXTERNAL_CALL,\n"
        f"{_key_fn_kwarg(n, '                ')}\n"
        f"            ),"
        for n in node_ids
    )
    reflect_edges = "\n".join(_edge_line(n, "            ") for n in node_ids)
    body.append(f'''

def build_graph() -> Graph:
    """One node per call site, wired `<call site> -> reflect -> consolidate`.

    THE REFLECT/CONSOLIDATE TAIL IS WHAT MAKES THIS REPO LEARN. `make_reflect_node`
    writes a `MemoryRecord(kind="failure"|"success")` for every run; the
    self-rewiring loop's proposer reads exactly those records and nothing else.
    Delete the tail, or change a node's route back to `END`, and the loop does
    not break loudly — it goes SILENT: `aef loop cycle` exits **0** saying
    `no admissible failure memory: no candidate this cycle`, every cycle,
    forever, which reads as success (measured; aef-core ADR 0139 and 0143).
    `make_consolidate_node` then folds repeated records into
    `Services.knowledge` so a lesson seen in two runs survives as a lesson.

    WHAT THIS COSTS YOUR CALLER, stated because it is a real change. The
    node's own return value is untouched: the call site's answer is still in
    `state.working_memory["<node id>"]` when the run ends. But the run now
    needs four more services — `critic`, `judge` and `memory` for reflect,
    `knowledge` for consolidate. `aef.services.runtime.agent_services()`
    supplies all four by default, so `aef run`, `aef loop bootstrap` and the
    gates are unaffected. Hand-building a bare `Services(model_provider=...)`
    and executing this graph now raises `ServiceNotConfiguredError` where it
    previously ran — use `agent_services(...)`, or drop the tail knowing what
    the paragraph above says it costs.

    Only the ENTRY node is reached automatically. With more than one call site
    the others are declared, routed and unreachable until you add the edges
    that order them — how your call sites compose is a semantic decision this
    command has no basis to make.
    """
    return Graph(
        id="{repo_name}",
        version="0.1.0",
        nodes={{
{listed}
            "reflect": make_reflect_node(route="consolidate"),
            "consolidate": make_consolidate_node(route=END),
        }},
        edges=[
{reflect_edges}
            Edge(from_node="reflect", to_node="consolidate"),
        ],
        entry_node="{node_ids[0]}",
    )
''')
    return "".join(body)


def _backup_path(out: Path) -> Path:
    """A `.bak` beside the file that never destroys an earlier one.

    Overwriting `<generated>.py.bak` on a second `--force` would lose the
    first round of edits to save the second, which is the same failure one
    step along.
    """
    candidate = out.with_suffix(out.suffix + ".bak")
    counter = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = out.with_suffix(f"{out.suffix}.bak.{counter}")
        counter += 1
    return candidate


def _resolve_out(root: Path, out: str | Path | None) -> tuple[Path, str | None]:
    """The absolute output path, plus how the zone classifier will see it.

    An absolute `--out` is honoured as given; a relative one is resolved
    against the repo root, which is what makes the default land in Zone A.
    The second element is the repo-relative POSIX form, or `None` when the
    path is outside the repo — a path the zone classifier has no opinion on,
    which `report()` says rather than guessing.
    """
    raw = DEFAULT_MIGRATED_OUT if out is None else out
    path = Path(raw)
    absolute = path if path.is_absolute() else root / path
    absolute = Path(absolute)
    try:
        relative: str | None = absolute.resolve().relative_to(root).as_posix()
    except ValueError:
        relative = None
    return absolute, relative


def _write_prompt_agents(
    root: Path,
    result: MigrateResult,
    *,
    force: bool,
    write_guard: Callable[[Path], None] | None = None,
) -> None:
    """Write graphs; preserve edited wrappers before an explicit forced update."""
    for site in result.prompt_agents:
        target = root / Path(site.out_relative)
        if write_guard is not None:
            write_guard(target)
        if target.exists() and not force:
            result.prompt_existing.append(site.out_relative)
            continue
        rendered = render_prompt_agent(site, root.name).encode("utf-8")
        if target.exists() and target.read_bytes() != rendered:
            backup = _backup_path(target)
            if write_guard is not None:
                write_guard(backup)
            # Preserve encoding and newlines, including owner code that is
            # not valid UTF-8. Do not overwrite any prior backup.
            with backup.open("xb") as stream:
                stream.write(target.read_bytes())
            result.prompt_backups.append(backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(rendered)
        result.prompt_written.append(target)


def run_migrate(
    target_dir: Path,
    *,
    force: bool = False,
    write: bool = True,
    out: str | Path | None = None,
    agent_root: str = DEFAULT_AGENT_ROOT,
    prompt_agents_dir: str = DEFAULT_PROMPT_AGENT_DIR,
    write_guard: Callable[[Path], None] | None = None,
) -> MigrateResult:
    """Scan `target_dir` and write the generated graph to `out`.

    `out` is repo-relative unless absolute, and defaults to
    `DEFAULT_MIGRATED_OUT` — **inside Zone A**, because the previous default
    was the repo root and the repo root is Zone C. Reproduced before it was
    changed: a candidate whose only changed path was the generated file came
    back from the harness's own `inspect_candidate` as
    `allowed: False ... Zone C (core) — not under the agent root 'agents'`,
    so the one file this command exists to produce was the one file the loop
    could never improve (ADR 0143).

    Parent directories are created. The never-overwrite rule and `--force`'s
    backup (ADR 0140) apply to whatever path `out` names.
    """
    root = target_dir.resolve()
    result = scan(root)
    result.agent_root = agent_root
    result.prompt_agents_dir = prompt_agents_dir
    result.prompt_agents = discover_prompt_agents(
        root, agents_dir=prompt_agents_dir, agent_root=agent_root
    )
    result.skills_seen = discover_skills(root)
    # aef's own `new-model-check` skill is separated HERE, at the one place
    # that knows the repo root, so the report can name it and not count it.
    result.skills_own = [p for p in result.skills_seen if is_adopt_skill(root, p)]
    if not write:
        return result

    # Written BEFORE the early return below, because the call-site graph
    # already existing is not a reason to leave eight personas unmigrated —
    # and on every repo this command was built for there are no call sites at
    # all, so an early return here would make the whole feature unreachable.
    _write_prompt_agents(root, result, force=force, write_guard=write_guard)

    target, result.out_relative = _resolve_out(root, out)
    if write_guard is not None:
        write_guard(target)
    if target.exists() and not force:
        # Same rule as `aef adopt`: never overwrite. A generated file the
        # operator has since edited is the expensive thing to lose.
        return result

    rendered = render(result, root.name)

    # ADR 0140. `--force` used to discard hand edits silently and exit 0 —
    # while `aef loop doctor`'s fix string for the "model calls visible"
    # obligation printed `aef migrate --dir . --force` as the recommended
    # next command. The comment three lines above named the edited file as
    # "the expensive thing to lose" and then `--force` lost it. Compare
    # against what migrate WOULD generate: identical means there is nothing
    # to preserve, different means somebody changed it.
    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            existing = None
        if existing is not None and existing != rendered:
            backup = _backup_path(target)
            if write_guard is not None:
                write_guard(backup)
            backup.write_text(existing, encoding="utf-8")
            result.backup = backup

    # The default output is two directories deep now, and neither exists in a
    # freshly adopted repo (`aef adopt` writes `agents/README.md` but nothing
    # under it). Created here rather than required of the adopter.
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    result.written = target
    return result


def _zone_note(result: MigrateResult) -> list[str]:
    """Which zone the written path is in **under the policy the loop will run**,
    and what that costs.

    ADR 0143. This command wrote to Zone C for its whole life and its report
    said nothing about it, so the fact only surfaced later — as
    `G0 rejected it: candidate touches paths outside Zone A` at the end of a
    cycle, or as a `bless` that archived a Zone A tree containing none of the
    agent. The zone is a property of the path, so it is answered by the
    classifier the gate itself uses rather than by a string comparison here.

    ADR 0168 — and this is the whole reason it takes the result rather than a
    path. It classified with the DEFAULT `ZonePolicy` and hardcoded
    `DEFAULT_AGENT_ROOT` in its own string, so one run of

        aef migrate --dir . --agent-root .claude/agents

    printed `Zone A (agents/**)` for `agents/migrated/graph.py` twelve lines
    above a BLAST RADIUS block saying `Zone A is '.claude/agents'` — while the
    classifier the gates actually use says that file is Zone **C** under the
    widened root. Two sentences in one report, disagreeing, with the wrong one
    stated first and the gate agreeing with neither.

    The call-site graph does NOT move with `--agent-root` (`--out` moves it),
    so landing outside a widened root is the ORDINARY case rather than a
    mistake — and the note says so in words, and names the `--agent-path`
    values that ARE inside the root, because the next command the operator
    types takes one of them.
    """
    relative = result.out_relative
    if relative is None:
        return [
            "OUTSIDE the repo — the zone classifier only judges repo-relative paths, "
            "so nothing here can tell you whether the loop may touch this file"
        ]
    policy = ZonePolicy(agent_root=result.agent_root)
    verdict = inspect_path(relative, policy)
    if verdict.zone.value == "A":
        return [
            f"Zone A ({result.agent_root}/**) — agent-writable, the only tree the "
            f"self-rewiring loop may propose changes to"
        ]
    if verdict.zone.value == "B":
        return [
            "Zone B (the harness) — a candidate touching this is a SECURITY EVENT, not a "
            "rejected proposal. Write the graph somewhere else."
        ]

    lines = [
        f"Zone C under the agent root this run used ({result.agent_root!r}) — NOT "
        f"agent-writable. A candidate touching this file is rejected with "
        f"`G0 rejected it: candidate touches paths outside Zone A`, and `aef loop bless` "
        f"will archive a Zone A tree that does not contain it."
    ]
    if result.agent_root == DEFAULT_AGENT_ROOT:
        lines.append(
            f"  Pass `--out {DEFAULT_MIGRATED_OUT}` (the default) to put it inside Zone A."
        )
        return lines

    # A widened root. `--out` is what moves this file, and the graphs that ARE
    # inside the root are the ones every `aef loop --agent-path` should name.
    inside = [
        site.out_relative
        for site in result.prompt_agents
        if inspect_path(site.out_relative, policy).zone.value == "A"
    ]
    lines += [
        f"  You widened the agent root to {result.agent_root!r}, and this file is not under",
        "  it — `--agent-root` moves the PROMPT AGENT graphs, `--out` moves this one. That",
        "  is expected, not a mistake: it is the call-site graph, and this repo's agents are",
        "  elsewhere. Move it too with",
        f"    aef migrate --dir . --agent-root {result.agent_root} "
        f"--out {result.agent_root}/{MIGRATED_DIR_NAME}/graph.py",
        "  or leave it where it is and point the loop at a graph that IS inside the root:",
    ]
    if inside:
        lines += [f"    --agent-path {out}" for out in inside]
    else:
        lines.append(
            f"    (none — no graph was written under {result.agent_root!r} this run, so "
            f"`aef loop` has nothing inside Zone A to gate)"
        )
    return lines


def _blast_radius(result: MigrateResult) -> list[str]:
    """What this run added to the loop's write scope, in words (ADR 0152).

    Widening Zone A is a scope decision, not a default, so the report says
    out loud which of the two states the repo is in — and the sentence is
    computed from the classifier the gates themselves use, not from a string
    comparison here.
    """
    policy = ZonePolicy(agent_root=result.agent_root)
    personas = [(site.source, inspect_path(site.source, policy)) for site in result.prompt_agents]
    persona, verdict = personas[0]
    all_inside = all(v.zone.value == "A" for _, v in personas)
    some_inside = any(v.zone.value == "A" for _, v in personas)
    lines = [
        "",
        "BLAST RADIUS — what the self-rewiring loop may now propose changes to.",
        f"  Zone A is {result.agent_root!r}. The PROMPT AGENT graphs are inside it.",
    ]
    # "The generated graphs are inside it" was false for one of them whenever
    # the root was widened: `--agent-root` moves the prompt-agent graphs and
    # `--out` moves the call-site graph, so under `--agent-root .claude/agents`
    # the call-site graph stays at `agents/migrated/graph.py` and is Zone C.
    # Said here as well as beside the file, because the two claims sat twelve
    # lines apart and contradicted each other (ADR 0168).
    if result.out_relative is not None:
        call_site_zone = inspect_path(result.out_relative, policy).zone.value
        if call_site_zone != "A":
            lines.append(
                f"  The CALL-SITE graph is NOT ({result.out_relative} is Zone "
                f"{call_site_zone}) — `--out` moves that one, not `--agent-root`."
            )
    lines += [f"  {path} is Zone {v.zone.value}." for path, v in personas]
    if all_inside:
        lines += [
            f"  The PERSONA FILES are inside it too ({persona} is Zone A).",
            "  This is the widened setting, and it is the point: a proposer can edit the",
            "  prompt an agent runs on, and `aef loop bless` archives a baseline that",
            "  contains the prompt rather than only the wrapper around it.",
            f"  It also means a candidate may rewrite any file under {result.agent_root!r} —",
            "  including every persona your harness loads. Pass the SAME --agent-root to",
            "  every `aef loop` command (bless, gate, cycle, doctor), or bless and the",
            "  gate describe different trees and G5's drift metric is measured against",
            "  a baseline of something else.",
        ]
    else:
        lines += [
            "  Only some persona files are inside Zone A."
            if some_inside
            else f"  The PERSONA FILES are NOT ({persona} is Zone {verdict.zone.value}).",
            "  A candidate editing an outside persona is rejected by G0.",
            "  Widening the tree an agent may rewrite is an explicit owner scope decision.",
            "  Select one persona directory for a learning run; do not widen to the repo root.",
        ]
        roots = sorted(
            {
                next(
                    (root for root in NATIVE_PROMPT_AGENT_DIRS if path.startswith(root + "/")),
                    result.prompt_agents_dir,
                )
                for path, v in personas
                if v.zone.value != "A"
            }
        )
        for root in roots:
            lines += [
                f"    aef migrate --dir . --agent-root {root}",
                f"  Pass the SAME --agent-root {root} to every loop command.",
            ]
    return lines


def _prompt_agent_lines(result: MigrateResult) -> list[str]:
    lines = [
        "",
        f"found {len(result.prompt_agents)} prompt agent(s) under "
        + (
            ", ".join(NATIVE_PROMPT_AGENT_DIRS)
            if result.prompt_agents_dir == DEFAULT_PROMPT_AGENT_DIR
            else result.prompt_agents_dir
        ),
    ]
    if not result.prompt_agents:
        lines.append(
            "  none — this repo's agents are not markdown personas in that directory, "
            "or it does not exist"
        )
        return lines
    for site in result.prompt_agents:
        lines.append(f"  AGENT    {site.definition.name}  ({site.source})")
        lines.append(f"            -> {site.out_relative}")
        lines.append(
            f'            -> aef run {site.run_target} --objective "..." --config aef.yaml'
        )
        if not site.importable:
            lines.append(
                f"            (a file path, not {site.dotted!r}: no dotted module name exists under"
            )
            lines.append(f"            {result.agent_root!r}, and `aef run` takes either form)")
        lines.append(f"            graph_id={site.graph_id!r}, wired {PROMPT_AGENT_WIRING}")
        if site.unsafe_name_reason:
            lines += [
                f"            NAME REFUSED as a graph id: {site.unsafe_name_reason}.",
                "            A graph id is joined onto a directory "
                "(`<archive root>/<graph id>/v000001/`),",
                f"            so {site.graph_id!r} is used instead. The persona keeps its own "
                f"name for",
                "            the model call; pass the id above to `aef loop bless --graph-id`,",
                "            and rename the persona if you want the two to match.",
            ]
        if site.definition.unhonoured_keys:
            lines.append(
                "            frontmatter read and NOT honoured: "
                + ", ".join(site.definition.unhonoured_keys)
            )
    lines += [
        "",
        "CONTAINMENT DEPENDS ON model_provider.impl, and each run records the one it got.",
        "Each persona body becomes the system message of one completion; what that",
        "completion may do is the provider's answer, not this command's (ADR 0169):",
        '  claude_code  --tools "" (documented as "disable all tools"), --max-turns 1,',
        "               --safe-mode, empty strict MCP config; persona in --system-prompt.",
        '  grok         --tools "" is MEASURED to suppress nothing on 1.0.5 — the same argv',
        "               read a planted file with one more turn allowed. --max-turns 1",
        "               cancels such a run and the provider raises. No --safe-mode.",
        "  codex        --sandbox read-only, no --tools, no --max-turns, no system flag —",
        "               so the persona goes in the USER turn.",
        "  command      whatever your argv template says; its isolation: list is YOUR",
        "               unverified assertion, and with no {system} slot the persona goes",
        "               in the USER turn.",
        "  anthropic    no tools parameter is sent; persona in system=.",
        "The frontmatter tools: key is read and never honoured under any of them. Every run",
        "writes the provider's isolation set and the persona's channel to",
        'working_memory["prompt_agent__containment"], and appends a',
        "prompt_agent.persona_in_user_turn error when it was the user turn.",
    ]
    if result.prompt_written:
        lines += ["", f"wrote {len(result.prompt_written)} prompt agent graph(s):"]
        lines += [f"  {p}" for p in result.prompt_written]
    if result.prompt_backups:
        lines += ["", "preserved edited prompt graph(s) before --force:"]
        lines += [f"  {p}" for p in result.prompt_backups]
    if result.prompt_existing:
        lines += [
            "",
            f"{len(result.prompt_existing)} prompt agent graph(s) already existed and were "
            "NOT overwritten (pass --force):",
        ]
        lines += [f"  {p}" for p in result.prompt_existing]
    lines += _blast_radius(result)
    return lines


def _skill_lines(result: MigrateResult) -> list[str]:
    if not result.skills_seen:
        return []
    # The LISTING names every skill, aef's own included and marked. Dropping
    # it from the listing would make `aef migrate` silent about a file it
    # declined to migrate, which is the one thing this block exists not to be.
    #
    # The HEADER counts the rows under it, and then says how that total splits.
    # It used to print only the adopter's subtotal — ADR 0172's D4, so that the
    # number stops changing the moment adoption runs, and ADR 0176's reason
    # that counting aef's own made adopt say 5 and migrate say 6 about one
    # tree. Both reasons survive here: the stable adopter subtotal is still
    # stated, by name. What did not survive is a header whose number was not
    # the number of rows it headed — `found 4 skill(s)` over five rows on
    # keystone, `found 6` over seven on datamining, reproduced at two-over-three
    # on a scratch repo (F-M8-2, ADR 0187). A report that is wrong about itself
    # teaches the reader to stop counting.
    ours = [p for p in result.skills_seen if p in set(result.skills_own)]
    yours = len(result.skills_seen) - len(ours)
    split = f" ({yours} yours + {len(ours)} aef's own)" if ours else ""
    lines = [
        "",
        f"found {len(result.skills_seen)} skill(s) and did NOT migrate any of them{split}:",
    ]
    lines += [
        f"  SKILL    {p}" + ("   (aef's own — not yours)" if p in ours else "")
        for p in result.skills_seen
    ]
    lines += [
        "  A skill is not an agent. Its body is instructions injected into a session",
        "  already in progress, it presumes that session's task and tools, and it",
        "  routinely points at bundled files a tool-less completion cannot open — so a",
        "  graph built from SKILL.md would send an instruction sheet stripped of half",
        "  its content and return something that looks like an agent's answer. It also",
        "  has no objective of its own, and the objective is the user turn every",
        "  generated graph sends. Named here rather than skipped in silence.",
    ]
    return lines


def report(result: MigrateResult) -> str:
    lines = [
        f"scanned {result.scanned_files} Python file(s)",
        f"found {result.total_found} call site(s): "
        f"{len(result.sites)} wrapped, {len(result.skipped)} skipped",
    ]
    if result.sites:
        lines.append(
            f"  of the wrapped: {len(result.routed)} routed through "
            f"Services.model_provider, {len(result.unrouted)} still calling your function"
        )
    lines.append("")
    for site in result.sites:
        label = "ROUTED " if site.routed else "WRAPPED"
        lines.append(f"  {label}  {site.module}.{site.function}:{site.lineno}  ({site.evidence})")
        if site.routed:
            lines.append(
                f"            -> services.require_model_provider().complete(model={site.model!r})"
            )
            lines.append(f"            routed because {site.form_reason}")
            lines.append(
                f"            {site.module}.{site.function} is now BYPASSED — its retries "
                f"and backend selection are yours to re-express"
            )
        else:
            lines.append(f"            -> calls {site.module}.{site.function}, unchanged")
            lines.append(f"            NOT routed because {site.form_reason}")
            lines.append(
                "            the model call stays INVISIBLE to the harness: no policy "
                "check, no fallback, no RecordedCall to replay"
            )
    for skip in result.skipped:
        lines.append(f"  SKIPPED  {skip.module}.{skip.function}:{skip.lineno}  — {skip.reason}")
    if result.backup is not None:
        existing_name = result.out_relative or (
            result.written.name if result.written is not None else "the generated file"
        )
        lines += [
            "",
            f"your existing {existing_name} DIFFERED from what migrate generates —",
            f"it was backed up to {result.backup} before being overwritten.",
            "If you had hand-edited it (re-expressed a system prompt, finished a",
            "node body), that work is in the backup and not in the new file.",
        ]
    if result.written is not None:
        lines += ["", f"wrote {result.written}"]
        lines += [f"  {note}" for note in _zone_note(result)]
    elif result.sites or result.skipped:
        lines += ["", "nothing written (file exists; pass --force to overwrite)"]
    lines += [
        "",
        "This generated the PLUMBING, not the semantics. Every wrapper passes",
        "state.objective as a single prompt and stores the raw result; if your",
        "function takes more than that, the node body is yours to finish.",
    ]
    if result.sites:
        entry = node_id_for(result.sites[0])
        unreached = [node_id_for(s) for s in result.sites[1:]]
        lines += [
            "",
            f"build_graph() wires {entry} -> reflect -> consolidate -> END.",
            "That tail is what makes this repo LEARN: the reflect node writes the failure",
            "memory the self-rewiring loop's proposer reads, and nothing else does.",
            "Route a node back to END and the loop does not break — it goes silent,",
            "exiting 0 with `no admissible failure memory: no candidate this cycle`.",
            "Executing the graph now needs critic/judge/memory/knowledge on Services;",
            "`aef.services.runtime.agent_services()` supplies all four by default.",
        ]
        # The report used to say "wires <call site> -> reflect -> consolidate",
        # singular, for any number of call sites. With three it generated three
        # nodes, made ONE of them the entry, and said nothing: the executor
        # emits no warning for a declared-but-unreachable node, and
        # `classify()` builds `node_path` from the trace, so G2 never sees them
        # either. The adopter's first sign was a corpus that pinned a third of
        # their agent (reproduced, ADR 0149).
        if unreached:
            lines += [
                "",
                f"ONLY {entry} RUNS. The other {len(unreached)} generated node(s) are",
                "declared, routed to reflect, and UNREACHABLE — a graph has one entry",
                "and nothing reaches them from it:",
            ]
            lines += [f"  UNREACHED  {n}" for n in unreached]
            lines += [
                "",
                "This is the half of the migration that is yours. How your call sites",
                "compose — sequence, branch, or one entry that never calls the others —",
                "is a semantic decision, and nothing in your source says which; the",
                "entry above is simply the first call site found. Nothing warns you",
                "later: the executor runs an unreachable node zero times without",
                "complaining, and the gates score only the path the trace took, so a",
                "corpus recorded now pins the entry node alone. Edit build_graph()'s",
                "routes — a node returns the id of the next one — until every node you",
                "want executed is on the path.",
            ]
    if result.unrouted:
        lines += [
            "",
            "Every UNROUTED node above leaves its model call invisible to the",
            "harness. `aef loop doctor` reports that as an unmet obligation, and",
            "it is not a false alarm: the gates cannot replay a call they never",
            "saw. Routing one by hand is a trade — read the reason it was not",
            "routed automatically before you make it.",
        ]
    lines += _prompt_agent_lines(result)
    lines += _skill_lines(result)
    return "\n".join(lines)
