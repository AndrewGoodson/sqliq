"""`evaluator.suites` -> `RuleBasedEvaluator.domain_gates`.

`domain_gates` is the pluggable per-agent surface report §16 describes, and
it is one of the five things the prime directive allows to differ per agent.
It has been a declared injection point with no production caller since Phase
0 — the shape ADR 0092 named: *a declared injection point with no production
caller is indistinguishable from a missing feature*. `evaluator.suites`
validated and was read by nothing.

**A suite is a `module:function` reference, not a bare name.** A bare name
needs a registry someone has registered into, which needs an import to have
already happened — invisible, order-dependent, and silent when it did not.
The same `module:function` shape the loop already uses for `--entrypoint`
resolves explicitly and fails loudly. An adopter writes:

```yaml
evaluator:
  suites:
    - "myrepo.gates:blast_radius_under_limit"
```

**Adding a suite can only make `passed` stricter.** `EvaluationRecord.passed`
is `task_completion >= 0.5 and all(domain_gates.values())`, so a gate can
refuse a run that would otherwise pass and can never rescue one that would
not. That is what makes wiring this safe to do without touching
`task_completion` semantics, which are fixed by ADR 0038.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from aef.config.schema import EvaluatorConfig
from aef.services.eval.rule_based import GateFn


class DomainGateError(RuntimeError):
    """A declared suite that cannot be resolved into a callable.

    Raised rather than skipped. A suite an owner wrote and the evaluator
    silently ignored is the exact failure this module exists to end, and
    replacing "ignored because unwired" with "ignored because unresolvable"
    would be no improvement at all.
    """


def _forbidden_prefixes() -> tuple[str, ...]:
    """The aef subpackages a suite may not name, READ FROM G0.

    A suite is arbitrary code the config points at, resolved and called
    during scoring. G0 already forbids agent code importing `aef.harness`
    and `aef.cli` — the judging apparatus — and `evaluator.suites` would
    otherwise be a second door into the same rooms, reached from a different
    direction. Found by this milestone's adversarial round: a suite of
    `aef.harness.loop:gate` resolved cleanly.

    Imported lazily and read from the gate rather than copied here.
    `aef.harness` imports `aef.config`, so a module-level import would be a
    cycle; a hand-maintained copy would be "drift between two lists nobody
    compares", which ADR 0091 named after it cost four defects.
    """
    from aef.harness.gates.g0_static_safety import FORBIDDEN_AEF_SUBPACKAGES

    return tuple(FORBIDDEN_AEF_SUBPACKAGES)


def _checked(reference: str, gate: Callable[..., Any]) -> GateFn:
    """Wrap a resolved gate so it must answer with a bool, and so a failure
    inside it names itself.

    Both from this milestone's adversarial round, both reproduced. A gate
    returning the string `"yes"` landed in `EvaluationRecord.domain_gates` —
    typed `dict[str, bool]` — as `'yes'`, and `passed` read it truthily, so
    any non-empty string passed and `""` or `0` failed, silently. A gate with
    a forgotten `return` produced `None` and failed every run for a reason
    nothing reported. A verdict a gate did not actually give is worse than no
    gate: `passed` is what an owner reads to decide whether to trust a run.
    """

    def checked(state: Any) -> bool:
        try:
            verdict = gate(state)
        except Exception as exc:
            raise DomainGateError(
                f"evaluator suite {reference!r} raised {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(verdict, bool):
            raise DomainGateError(
                f"evaluator suite {reference!r} returned {verdict!r} ({type(verdict).__name__}), "
                f"not a bool. A domain gate states a verdict; anything else is read for its "
                f"truthiness, so a forgotten return fails every run and a non-empty string "
                f"passes every one."
            )
        return verdict

    return checked


def _resolve(reference: str) -> Callable[..., Any]:
    if reference.count(":") != 1:
        raise DomainGateError(
            f"evaluator suite {reference!r} is not a 'module:function' reference. A bare "
            f"name has nothing to resolve against — write the import path explicitly, e.g. "
            f"'myrepo.gates:blast_radius_under_limit'."
        )
    module_path, _, attribute = reference.partition(":")
    if not module_path or not attribute:
        raise DomainGateError(f"evaluator suite {reference!r} names an empty module or function")

    for forbidden in _forbidden_prefixes():
        if module_path == forbidden or module_path.startswith(f"{forbidden}."):
            raise DomainGateError(
                f"evaluator suite {reference!r} names {forbidden!r}, which is the judging "
                f"apparatus. G0 forbids agent code importing it; a suite is code the config "
                f"points at and this gate runs, so the same rule applies here."
            )

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise DomainGateError(
            f"evaluator suite {reference!r}: cannot import {module_path!r} ({exc})"
        ) from exc

    gate = getattr(module, attribute, None)
    if gate is None:
        raise DomainGateError(
            f"evaluator suite {reference!r}: {module_path!r} has no attribute {attribute!r}"
        )
    if not callable(gate):
        raise DomainGateError(
            f"evaluator suite {reference!r}: {attribute!r} is not callable, so it cannot be "
            f"evaluated against a run's state"
        )
    return gate  # type: ignore[no-any-return]


def build_domain_gates(config: EvaluatorConfig) -> dict[str, GateFn]:
    """Resolve every declared suite, or raise naming the one that failed.

    Keyed by the reference itself, so the evaluator's output says which suite
    a `False` came from without the adopter having to invent a second name
    for the same thing.
    """
    gates: dict[str, GateFn] = {}
    for reference in config.suites:
        if reference in gates:
            raise DomainGateError(
                f"evaluator suite {reference!r} is declared twice; a duplicate would silently "
                f"collapse to one gate and the count an owner reads would be wrong"
            )
        gates[reference] = _checked(reference, _resolve(reference))
    return gates
