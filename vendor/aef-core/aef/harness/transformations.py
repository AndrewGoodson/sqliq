"""The bounded catalogue a proposer may draw from.

Not code generation. A proposer applies NAMED operations, each of which is a
readable diff, individually revertible, and constrained by the memory record
that motivated it (ADR 0096).

**The catalogue is DERIVED from the gates, not proposed and then checked
against them.** ADR 0096 chose a transformation, argued it was safe, and
shipped it; `fallback_node_id` had been owner-only since ADR 0036/0039 and
the proposer's sole structural output was the one thing the gate set forbids
(ADR 0098). So the constraint now comes from the gate itself:
`_assert_controls_untouched` imports `OWNER_ONLY_FIELDS` from G4 and
compares before against after. Adding a field to that frozenset
automatically forbids the catalogue from emitting it — nobody has to
remember to.

The four properties every transformation must hold, each traceable to a
defect this program actually found:

1. The diff is human-readable and G0 sizes it.
2. The citation constrains the change — a proposal that cites a failure but
   would have made the same edit regardless is decoration.
3. One named operation, one coherent change.
4. **Nothing here may alter routing into a HITL-gated edge.** ADR 0089
   measured what that buys: a candidate that broke five scenarios and added
   `requires_human_approval=True` converted every regression into a G2 pass.
   This is now the OWNER_ONLY_FIELDS check's job; the HITL flags are a
   subset of that frozenset.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from aef.harness.gates.g4_separation import OWNER_ONLY_FIELDS, _raises_approval_required

# How many times a retried node attempts its work. Emitted as a module-level
# constant in the AGENT's source rather than inlined, so the change composes:
# the structural transformation creates the knob, and the existing numeric
# proposer can then tune it under the same gates. Deliberately un-prefixed so
# `find_constants` (which matches `^[A-Z][A-Z0-9_]*`) can see it.
RETRY_CONSTANT = "RETRY_ATTEMPTS"
DEFAULT_RETRY_ATTEMPTS = 3

# The one exception a retry must never absorb. Named here for the REFUSAL
# below; deliberately never emitted into agent source, because G4 treats
# agent code referencing it as a security event in its own right.
APPROVAL_ERROR = "HumanApprovalRequiredError"


class TransformationError(RuntimeError):
    """The transformation does not apply. Not an error in the candidate — a
    statement that this operation has nothing to offer here."""


@dataclass(frozen=True)
class Transformation:
    """One applied change, with the evidence that motivated it."""

    name: str
    target: str
    rationale: str
    source: str


def _callee_name(call: ast.Call) -> str | None:
    callee = call.func
    if isinstance(callee, ast.Name):
        return callee.id
    if isinstance(callee, ast.Attribute):
        return callee.attr
    return None


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node for node in ast.walk(tree) if isinstance(node, ast.Call) and _callee_name(node) == name
    ]


def _node_constructions(tree: ast.AST) -> dict[str, ast.Call]:
    """`Node(id="x", ...)` calls, keyed by the declared id."""
    out: dict[str, ast.Call] = {}
    for call in _calls_named(tree, "Node"):
        for kw in call.keywords:
            if kw.arg == "id" and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, str):
                    out[kw.value.value] = call
    return out


def _rendered(expr: ast.expr | None) -> str:
    """A stable, comparable rendering of an expression. `ast.unparse` rather
    than `.value`, so a non-literal identifier is still distinguishable
    instead of collapsing to a single placeholder."""
    return "?" if expr is None else ast.unparse(expr)


def _control_signature(source: str) -> list[tuple[str, str, str]]:
    """Every owner-only declaration in the file, as comparable tuples.

    Covers `Node(...)` and `Edge(...)` alike, keyed by the field names G4
    owns rather than a list maintained here. Compared before against after,
    so a transformation cannot introduce, remove or alter a safety
    declaration even by accident — checked on the RESULT rather than trusted
    from the operation, which is the shape ADR 0080 found hackable.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    out: list[tuple[str, str, str]] = []
    for kind in ("Node", "Edge"):
        for call in _calls_named(tree, kind):
            kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
            # Identify the declaration so a field MOVING between two nodes is
            # visible; comparing bare field names would call that unchanged.
            if kind == "Node":
                where = _rendered(kwargs.get("id"))
            else:
                where = f"{_rendered(kwargs.get('from_node'))}->{_rendered(kwargs.get('to_node'))}"
            for field in sorted(OWNER_ONLY_FIELDS):
                if field in kwargs:
                    out.append((f"{kind}:{where}", field, ast.unparse(kwargs[field])))
    return sorted(out)


def _assert_controls_untouched(before: str, after: str) -> None:
    """The whole catalogue's safety property, in one place.

    ADR 0098: the previous catalogue's only entry emitted `fallback_node_id`,
    and no test joined the proposer to the gate that forbids it. This check
    is that join, and it reads the field list FROM the gate.
    """
    if _control_signature(before) != _control_signature(after):
        raise TransformationError(
            "transformation altered an owner-only safety declaration "
            f"(one of {', '.join(sorted(OWNER_ONLY_FIELDS))}), which no transformation may "
            "do — G4 rejects agent-authored changes to these and would reject the proposal "
            "(ADR 0036/0039, 0089, 0098)"
        )


def _validate(before: str, after: str) -> None:
    if after == before:
        raise TransformationError("transformation changed nothing")
    try:
        ast.parse(after)
    except SyntaxError as exc:
        raise TransformationError(f"transformation produced invalid Python: {exc}") from exc
    _assert_controls_untouched(before, after)


def _kwarg(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _function_named(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.name == name:
            return stmt
    return None


def add_bounded_retry(
    *,
    source: str,
    failing_node: str,
    citation: str,
    attempts: int = DEFAULT_RETRY_ATTEMPTS,
) -> Transformation:
    """Wrap `failing_node`'s function body in a bounded retry loop.

    The answer to "this node intermittently raises" that actually **does the
    work again** rather than declaring the failure tolerable. That distinction
    is the whole reason this replaced `add_deterministic_fallback`: a fallback
    records that control continued, which scores identically to the work
    getting done and converted genuine regressions into passes (ADR 0080,
    0098). A retry that succeeds succeeded.

    Preconditions, all refusals rather than guesses:

    - The node must declare `deterministic=False`. A deterministic node that
      raises raises identically on every attempt, so retrying it burns time
      to reach the same failure. Flakiness is a property of non-determinism.
    - The function's last statement must be a `return`. Otherwise a body that
      falls out of the `try` without returning would silently execute N times
      instead of once — a behaviour change the diff does not show.
    - Neither the retry constant nor an existing retry may already be present.

    Note what this does NOT touch: no `Node(...)` kwarg, no `Edge(...)`, no
    routing. `_assert_controls_untouched` proves that on the result rather
    than taking this paragraph's word for it.
    """
    if attempts < 2:
        raise TransformationError(f"a retry of {attempts} attempt(s) is not a retry")

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise TransformationError(f"source does not parse: {exc}") from exc

    constructions = _node_constructions(tree)
    if failing_node not in constructions:
        raise TransformationError(
            f"no Node(id={failing_node!r}) in this source; the memory names a node this "
            f"file does not declare"
        )
    call = constructions[failing_node]

    declared = _kwarg(call, "deterministic")
    if not (isinstance(declared, ast.Constant) and declared.value is False):
        raise TransformationError(
            f"node {failing_node!r} does not declare deterministic=False, so a retry cannot "
            f"help: a deterministic node raises identically on every attempt"
        )

    # REPRODUCED in the adversarial round for this milestone: a body whose
    # first statement has an external effect executes that effect once per
    # attempt, so a retry turned one call into three before the failure
    # surfaced. No gate can see this — G2 and G3 measure outcomes, and
    # nothing in the corpus counts side effects.
    #
    # The node contract's answer is `idempotency_key_fn`: the executor
    # computes the key ONCE per node execution, so every attempt carries the
    # same key and a deduplicating consumer collapses them (ADR 0010). That
    # premise is exactly what makes a retry safe, so this requires the
    # declaration rather than assuming it — and refuses MUTATING outright,
    # where "the consumer dedupes" is not a premise anyone should rest an
    # unattended merge on.
    effects = _kwarg(call, "side_effects")
    rendered_effects = _rendered(effects) if effects is not None else "PURE"
    if "MUTATING" in rendered_effects.upper():
        raise TransformationError(
            f"node {failing_node!r} declares side_effects=mutating; retrying it would repeat "
            f"an irreversible write, and no gate can observe how many times a side effect ran"
        )
    if effects is not None and "PURE" not in rendered_effects.upper():
        if _kwarg(call, "idempotency_key_fn") is None:
            raise TransformationError(
                f"node {failing_node!r} is non-pure and declares no idempotency_key_fn, so "
                f"repeating its work is not known to be safe"
            )

    # An approval requirement must never be retried. REPRODUCED in this
    # milestone's adversarial round: a node raising
    # `HumanApprovalRequiredError` on one attempt and returning normally on
    # another had the requirement swallowed by `except Exception`, turning a
    # HITL gate into a silent pass — HARD-STOP #7's exact concern.
    #
    # The first fix was to emit `except HumanApprovalRequiredError: raise`,
    # and G4 rejected it: introducing that class into agent-authored code is
    # itself a security event ("whether a run stopped at a human-approval
    # gate is the kernel's finding to report, not the candidate's to
    # assert"). That is ADR 0098's mistake in miniature — a fix designed
    # against a constraint I had not read — caught this time by running the
    # gate rather than by reasoning about it.
    #
    # So the answer is a REFUSAL, not emitted code, and it asks the question
    # using G4's own scanner. In the normal path the executor raises approval
    # AFTER `fn` returns, where no retry can reach it; the only exposure is
    # agent code raising it directly, which is exactly what this detects.
    # Residual risk, stated rather than papered over: a node that raises it
    # from a helper in another module is not visible here.
    if _raises_approval_required(source):
        raise TransformationError(
            f"{failing_node!r}'s module raises {APPROVAL_ERROR}; retrying it could absorb an "
            f"approval requirement and turn a HITL gate into a silent pass"
        )

    fn_ref = _kwarg(call, "fn")
    if not isinstance(fn_ref, ast.Name):
        raise TransformationError(
            f"node {failing_node!r} does not bind `fn` to a module-level function this "
            f"transformation can rewrite"
        )
    fn = _function_named(tree, fn_ref.id)
    if fn is None:
        raise TransformationError(f"no module-level `def {fn_ref.id}` to wrap")

    if RETRY_CONSTANT in source:
        raise TransformationError(
            f"{RETRY_CONSTANT} is already present; re-retrying an already-retried node is a "
            f"different operation with different evidence"
        )
    if not isinstance(fn.body[-1], ast.Return):
        raise TransformationError(
            f"`{fn_ref.id}` does not end in a return, so wrapping it in a loop would change "
            f"how many times the body runs without the diff showing it"
        )

    # A docstring stays OUTSIDE the loop. Wrapped, it becomes a bare
    # expression statement and `fn.__doc__` silently becomes None — a
    # semantic change the diff does not advertise.
    body_stmts = fn.body
    if (
        len(body_stmts) > 1
        and isinstance(body_stmts[0], ast.Expr)
        and isinstance(body_stmts[0].value, ast.Constant)
        and isinstance(body_stmts[0].value.value, str)
    ):
        body_stmts = body_stmts[1:]

    lines = source.splitlines(keepends=True)
    first, last = body_stmts[0].lineno, body_stmts[-1].end_lineno
    if last is None:  # pragma: no cover - ast always sets this
        raise TransformationError("cannot locate the function body to wrap")
    indent = " " * fn.body[0].col_offset
    inner = " " * 8

    body = [(inner + line if line.strip() else line) for line in lines[first - 1 : last]]
    if not body[-1].endswith("\n"):
        body[-1] += "\n"

    wrapped = (
        [f"{indent}for _attempt in range({RETRY_CONSTANT}):\n", f"{indent}    try:\n"]
        + body
        + [
            f"{indent}    except Exception:\n",
            f"{indent}        if _attempt == {RETRY_CONSTANT} - 1:\n",
            f"{indent}            raise\n",
        ]
    )

    # The constant goes above the function it serves, at module level, where
    # `find_constants` can later tune it under the same gates.
    prologue = [f"{RETRY_CONSTANT} = {attempts}\n", "\n", "\n"]
    rewritten = "".join(
        lines[: fn.lineno - 1]
        + prologue
        + lines[fn.lineno - 1 : first - 1]
        + wrapped
        + lines[last:]
    )

    _validate(source, rewritten)
    return Transformation(
        name="add_bounded_retry",
        target=failing_node,
        rationale=(
            f"node {failing_node!r} raised in a recorded run ({citation}) and is declared "
            f"non-deterministic; retrying its work up to {RETRY_CONSTANT}={attempts} times "
            f"before propagating the failure"
        ),
        source=rewritten,
    )
