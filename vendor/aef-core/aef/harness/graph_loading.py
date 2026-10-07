"""The ONE way anything in this repo turns a graph entrypoint into a module.

ADR 0177, erratum on ADR 0168. ADR 0168 §M4 taught `aef run` that an
entrypoint may be a **file path**, because ADR 0152 §4's opt-in —
`aef migrate --agent-root .claude/agents`, and the only way a persona becomes
Zone A — writes `.claude/agents/migrated/<module>/graph.py`, and no dotted
spelling of that path exists at all (`.claude` is not an identifier, and a
leading dot means "relative import" to `importlib`).

It taught **one** of the three loaders. `aef/harness/scenario_runner.py` and
`aef/harness/node_worker.py` each kept their own `importlib.import_module`,
so under the documented opt-in:

    $ aef loop score '.claude/agents/migrated/reviewer/graph.py:build_graph' \\
        --corpus corpus --splits train --config aef.yaml
    error: the 'package' argument is required to perform a relative import
    for '.claude/agents/migrated/reviewer/graph.py'
    EXIT=1

— exit 1 is `EXIT_REJECTED` — and through `aef loop cycle` it is worse than a
refusal, because the two sides of G2 used **different** loaders: the incumbent
is reconstructed from the recording (which `aef loop record` could load,
because `aef loop record` goes through `aef run`'s importer), while the
candidate runs in `node_worker`, which could not. An import error on one side
only is indistinguishable, downstream, from a behavioural regression:

    G2 outcome : fail
    G2 reason  : 1 previously-passing scenario(s) no longer pass (zero tolerance)

So every prompt candidate is rejected forever on the flag that exists to
propose prompt candidates, and two rejections halt the loop.

This module is the fix's first half: one function, in the harness, because the
harness may not import the CLI (`aef/harness/zones.py` carries the same
argument for `DEFAULT_AGENT_PATH`). `aef/cli/run.py` imports it and re-exports
the two names it published, so `from aef.cli.run import import_graph_module`
keeps working and there is still only one implementation.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

__all__ = [
    "DEFAULT_GRAPH_FACTORY",
    "GRAPH_REFERENCE_HELP",
    "ensure_cwd_importable",
    "import_graph_module",
    "looks_like_a_path",
    "split_entrypoint",
]

# The factory `aef migrate` writes and every generated graph exposes, so it is
# the one a reference that names no factory means.
DEFAULT_GRAPH_FACTORY = "build_graph"

# ONE sentence about "which graph", for every flag and argument in this repo
# that takes one. It lives HERE, beside the splitter that enforces it, rather
# than in `aef/cli/loop.py` where it started: `--entrypoint` is read by the
# harness (`scenario_runner`, `node_worker`), the harness may not import the
# CLI, and a second copy in the harness is the ADR 0149 shape — two answers to
# one question, drifting the first time either gains a case. `aef/cli/loop.py`
# re-exports it, so `from aef.cli.loop import GRAPH_REFERENCE_HELP` still works
# and there is still only one string.
GRAPH_REFERENCE_HELP = (
    "which graph, in any of three forms — a dotted module exposing "
    "build_graph() ('agents.mine.graph'); 'module:factory' "
    "('agents.mine.graph:build_graph'); or a path to the .py file that "
    "defines it ('.claude/agents/migrated/x/graph.py', optionally with "
    "':factory'). The file form is the one to use under a widened "
    "--agent-root, whose `.claude/agents/...` path has no importable dotted "
    "spelling — a leading dot means relative import (ADR 0168)."
)


def ensure_cwd_importable() -> None:
    """`aef` runs as an installed console script, whose `sys.path[0]` is the
    script's own directory (e.g. `.venv/bin`), NOT the caller's current
    directory — unlike `python script.py` or `python -m`, where the CWD is on
    `sys.path` automatically. Without this, `aef run agents.foo.graph` can
    never find a module `aef init` just scaffolded one directory below where
    you are standing: confirmed by actually running `aef init` then `aef run`
    end-to-end, not inferred from reading the code."""
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)


def looks_like_a_path(module_path: str) -> bool:
    """Is this a FILE to load rather than a dotted module name to import?

    A `.py` suffix or a separator; nothing else. Deliberately not "try the
    import and fall back", because a dotted import that fails for its own
    reason — a typo inside the module, a missing dependency — would then be
    retried as a filename, miss, and be reported as "no such file", hiding the
    real error behind a second one.
    """
    return module_path.endswith(".py") or "/" in module_path or "\\" in module_path


def split_entrypoint(entrypoint: str) -> tuple[str, str]:
    """`reference` -> (module-or-path, factory name). THE splitter.

    The factory defaults to `DEFAULT_GRAPH_FACTORY`, so all THREE documented
    forms resolve: a dotted module, `module:factory`, and a file path with or
    without `:factory`.

    A Windows drive letter is not a separator: the split is on the LAST colon
    and only when what follows is a Python identifier, so `a/b/graph.py`,
    `a/b/graph.py:make` and `C:\\x\\graph.py` all resolve the way they read. A
    half-written `module:` or `:factory` is refused by name rather than
    guessed at.

    **This used to demand both halves**, and refusing a reference with no
    colon made `--entrypoint` a FOURTH spelling of "which graph" (reproduced,
    ADR 0182):

        load_graph_reference   'agents.demo.graph'  ok
        load_graph             'agents.demo.graph'  REFUSED  EntrypointError:
            entrypoint must be '<module or file path>:<factory>'
        load_graph             'agents/demo/graph.py'  REFUSED (same)

    — so `aef loop cycle --module agents/demo/graph.py --entrypoint
    agents/demo/graph.py` accepted the first and refused the second inside one
    invocation. ADR 0176 fixed the five in-process callers by writing a second
    splitter in `aef/cli/loop.py`; this is that splitter, moved to the one
    place both the CLI and the harness can read it, so `scenario_runner` and
    `node_worker` — the two sides of G2 — cannot disagree about a spelling
    (ADR 0177's rule, applied to the split as well as the import).
    """
    module_path, sep, attribute = entrypoint.rpartition(":")
    if not sep:
        return entrypoint, DEFAULT_GRAPH_FACTORY
    if not module_path:
        raise ValueError(f"{entrypoint!r} names no module before the ':'. {GRAPH_REFERENCE_HELP}")
    if not attribute:
        raise ValueError(f"{entrypoint!r} ends in ':' and names no factory. {GRAPH_REFERENCE_HELP}")
    if not attribute.isidentifier():
        # Not a factory name: a colon inside a path. Treat the whole string as
        # the module/path rather than guessing, so the error the importer
        # raises is about the thing the user typed.
        return entrypoint, DEFAULT_GRAPH_FACTORY
    return module_path, attribute


def import_graph_module(module_path: str) -> ModuleType:
    """The one importer, for a dotted module name **or a file path**.

    The choice is made on the **spelling**, never by trying the import and
    falling back — see `looks_like_a_path`.

    `sys.modules` is keyed on a hash of the **resolved** path, because
    `aef migrate` names every generated file `graph.py`; the module is
    registered there before it is executed, which is what the import system
    does for a normal import and what a module importing itself (or a
    dataclass being pickled out of it) needs.
    """
    ensure_cwd_importable()
    if not looks_like_a_path(module_path):
        return importlib.import_module(module_path)

    path = Path(module_path)
    if not path.is_file():
        raise ValueError(
            f"{module_path!r} looks like a file path and there is no file there "
            f"(resolved to {path.resolve()}). Pass a dotted module name, or a path "
            f"to the .py file that defines build_graph()."
        )
    resolved = path.resolve()
    # Derived from the resolved path, so two graphs with the same basename in
    # different directories do not collide in `sys.modules` — `graph.py` is the
    # name `aef migrate` gives every single one of them.
    name = "aef_graph_" + hashlib.sha256(str(resolved).encode()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, resolved)
    if spec is None or spec.loader is None:  # pragma: no cover - unreadable file
        raise ValueError(f"cannot load a Python module from {resolved}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module
