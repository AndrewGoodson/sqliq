"""The vendor-SDK import scanner, lifted out of the test that owned it.

## Why this module exists

Constraint #3 — vendor SDKs are imported only under `aef/providers/` and
`aef/services/*/adapters/` — has been enforced since Phase 0 by an AST scan
living inside `tests/test_vendor_isolation.py`. It worked, and it ran on every
push. But a test only ever scans **this** repo, and the constraint it encodes
is not a house rule: a node in an *adopted* repo that constructs its own
`anthropic.Anthropic()` bypasses the policy engine, the fallback chain and the
harness login exactly the way one in `aef/kernel/` would. Nothing ever looked.

So the scanner moves here, where `aef/harness/preflight.py` can point it at an
adopter's graph and `aef/cli/migrate.py` can share its list. The test imports
it back. There is one list of vendor module names and one scanner — a second
copy is the drift ADR 0091 is about, and this file exists to make a second
copy impossible rather than merely discouraged.

## Two lists, and the difference between them

`VENDOR_TOP_LEVEL_MODULES` answers *"is this a vendor SDK import?"* and is the
constraint #3 list, moved here verbatim. `MODEL_SDK_ROOTS` answers a narrower
question `aef/cli/migrate.py` asks — *"is this call a model call?"* — and is a
strict subset. They are separate because `opentelemetry` is a vendor SDK that
must not be imported in `aef/kernel/`, and is emphatically not a model call
site worth generating a node for. A test asserts the subset relation holds, so
the two cannot drift apart into two independent lists.

**Which list a caller wants is the caller's decision, so it is a parameter.**
`scan_*` defaults to `VENDOR_TOP_LEVEL_MODULES` — the constraint #3 question,
which is what this scanner was written to ask about *this* repo. ADR 0137
pointed it at an adopter's graph and inherited that default, so the sixth
preflight obligation answered the wrong question: `import psycopg2` in a
reachable module permanently blocked the adopter, with a fix telling them to
route a Postgres connection through `require_model_provider().complete(...)`.
Fourteen of the nineteen names are not model SDKs. The obligation now passes
`MODEL_SDK_ROOTS` explicitly and a test pins which caller passes which
(ADR 0141).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

# Every vendor/product SDK named or implied by the research report and
# blueprint as a pluggable backend. Not exhaustive by construction — extend
# this set whenever a new adapter is added under providers/ or
# services/*/adapters/.
VENDOR_TOP_LEVEL_MODULES = frozenset(
    {
        "anthropic",
        "openai",
        # `cohere` was in `migrate.py`'s model-SDK tuple and NOT in this list,
        # for as long as both existed: constraint #3 would not have caught
        # `import cohere` in `aef/kernel/`. Found by the subset test below on
        # its first run — the drift ADR 0091 describes, already present.
        "cohere",
        "mem0",
        "mem0ai",
        "neo4j",
        "falkordb",
        "memgraph",
        "temporalio",
        "psycopg",
        "psycopg2",
        "opentelemetry",
        "dspy",
        "gepa",
        "llmlingua",
        "ragas",
        "deepeval",
        "langfuse",
        "google",
    }
)

# The subset that is a *model* SDK: the roots `aef migrate` treats as evidence
# that a function wraps a model call. Kept here rather than in `migrate.py` so
# that adding a provider means editing one file.
#
# `google` rather than `google.generativeai`, which is what `migrate.py` carried
# before this module existed: it matched a dotted root against a top-level one
# (`"google.generativeai".split(".")[0]` is `"google"`, which was never in the
# tuple), so the entry could not fire. The cost of the correction is that a
# non-model `google.*` call inside a function can now be read as a call site;
# the benefit is that a Gemini client construction is seen at all.
MODEL_SDK_ROOTS = frozenset({"anthropic", "openai", "cohere", "mem0", "google"})

# Vendored trees that are not the adopter's own code.
SKIP_DIRS = frozenset({"venv", "node_modules", "build", "dist", "site-packages"})


@dataclass(frozen=True)
class VendorImport:
    """One vendor SDK import, with enough to name it in a message."""

    path: Path
    lineno: int
    module: str
    statement: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: {self.statement}"


def top_level_module(name: str) -> str:
    return name.split(".", 1)[0]


def scan_source(
    source: str, *, path: Path, roots: frozenset[str] = VENDOR_TOP_LEVEL_MODULES
) -> list[VendorImport]:
    """Every import of a module in `roots` in `source`, wherever it hides.

    `ast.walk` rather than a scan of module-level statements: a lazy import
    inside a function body, inside a `try/except ImportError`, or inside an
    `if TYPE_CHECKING:` block is still an import of a vendor SDK, and each of
    those shapes has its own regression test.

    `roots` defaults to the constraint #3 list because that is the question
    this scanner was written to ask. A caller asking the narrower *model* call
    question passes `MODEL_SDK_ROOTS` — see the module docstring for why the
    default was the wrong answer for the preflight obligation.
    """
    tree = ast.parse(source, filename=str(path))
    found: list[VendorImport] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = top_level_module(alias.name)
                if mod in roots:
                    found.append(VendorImport(path, node.lineno, mod, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            if node.module is None or node.level > 0:
                continue  # relative import, e.g. "from . import x" — always in-package
            mod = top_level_module(node.module)
            if mod in roots:
                found.append(VendorImport(path, node.lineno, mod, f"from {node.module} import ..."))
    return found


def scan_file(
    path: Path, *, roots: frozenset[str] = VENDOR_TOP_LEVEL_MODULES
) -> list[VendorImport]:
    """Imports in one file. Unreadable or unparseable is *not* a violation —
    it is a file this scanner cannot speak about, and reporting a syntax error
    as a vendor import would be a lie in the caller's message."""
    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    try:
        return scan_source(source, path=path, roots=roots)
    except SyntaxError:
        return []


def scan_tree(
    root: Path, *, roots: frozenset[str] = VENDOR_TOP_LEVEL_MODULES
) -> list[VendorImport]:
    """Imports under `root`, skipping vendored trees and dot-dirs.

    Paths are tested **relative to `root`**, not absolutely: a repo that itself
    lives under a dot-directory (`~/.local/src/app`, a git worktree under
    `.claude/`) would otherwise have every one of its files skipped and be
    reported clean. See the same fix in `aef/cli/migrate.py`.
    """
    found: list[VendorImport] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts):
            continue
        found.extend(scan_file(path, roots=roots))
    return found
