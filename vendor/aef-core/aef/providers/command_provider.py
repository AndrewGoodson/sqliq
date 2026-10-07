"""A harness this repo has never seen, as configuration rather than code
(ADR 0154).

`ClaudeCodeProvider`, `CodexProvider` and `GrokProvider` each exist because
they carry *measured* knowledge — which flag actually isolates the operator's
session, which field the reply lives in, which error arrives with exit 0 —
that no owner should have to rediscover. That is a poor reason to write a
fourth class for a fourth CLI, and a terrible reason to make an owner wait
for one.

`CommandProvider` takes an argv template and an output extractor from
`aef.yaml` (`model_provider.impl: command`) and runs any single-shot CLI that
prints a completion. **Copilot's CLI is configured this way by the owner when
it is installed** — it is not on this box, so this repo ships no guess about
its flags; the same is true of every harness released after this file was
written. Unlisted harnesses are configuration, not code.

Its sufficiency is not asserted, it is demonstrated: ADR 0154 reproduces the
`GrokProvider` result — same argv, same answer, same token count — from a
`CommandProvider` config alone, and pins a config that reproduces
`ClaudeCodeProvider`'s argv element for element.

## The template

```yaml
model_provider:
  impl: command
  model: grok-4.6-build
  command:
    argv: ["grok", "--output-format", "json", "--max-turns", "1",
           "--tools", "", "--disable-web-search", "--no-subagents",
           "--no-plan", "--verbatim", "--cwd", "/path/to/an/empty/dir",
           "{model}", "{system}", "-p", "{prompt}"]
    model_argv: ["-m", "{model}"]
    system_argv: ["--system-prompt-override", "{system}"]
    output: json_pointer
    output_pointer: "/text"
    usage_pointer: "/usage/input_tokens"
```

`{model}` and `{system}` in `argv` are **slots**: each marks where its
fragment is spliced in when the request carries that value, and collapses to
nothing when it does not, so no dangling flag is ever passed. They are slots
rather than an append-at-the-end because argv order is part of a CLI's
contract — `claude` wants every flag before its positional prompt.

The `--cwd` above is the one thing this config cannot do for itself:
`GrokProvider` creates a fresh empty directory per call because that is the
only lever that stops a repo's `CLAUDE.md` reaching the model (ADR 0154), and
a static template has no place to put a fresh one. An owner configuring Grok
through `CommandProvider` points it at a directory they made. That gap is the
argument for the three hand-written adapters, stated plainly instead of
implied.

## Security

**No shell, ever.** The template is a *list*, `subprocess.run` receives that
list, and `shell=True` appears nowhere in this module (asserted by a test
that reads this source). A placeholder is substituted only when it is an
entire argv element, so a prompt containing `; rm -rf /` or `$(whoami)`
becomes one argv element holding those characters verbatim — there is no
string for a shell to reparse, because there is no shell. The prompt may
instead be written to the child's stdin (`stdin: true`), which is the same
guarantee by a different door.

## Isolation is the OWNER'S ASSERTION here, and is recorded as one

Every other provider in this package derives `isolation` from argv it built
itself, or from an API call it makes itself. This one cannot: `--tools ""`
means "disable all tools" to `claude` and "no restriction given" to `grok`
(measured — ADR 0169), so reading flag semantics off an unknown binary's
template would be a guess dressed as evidence.

So the containment half of `isolation` comes from the owner:

```yaml
    isolation: [no_tools, single_turn, no_project_context]
```

It is **an assertion, not a measurement**, it is never verified against the
CLI, and `PromptAgentNode` writes it into the trace labelled as the owner's.
Declaring nothing is the default and is honest — an undeclared
`CommandProvider` claims no containment at all, which is what F4 found this
class silently doing while `aef migrate` stamped the opposite into every
generated module as fact.

The one half this class DOES derive is the persona's channel, because that is
its own behaviour rather than the CLI's: a template with a `{system}` slot
gets `system_role`, and one without gets `user_turn_persona` — the
concatenation below. `system_role`/`user_turn_persona` are therefore refused
in the owner's list; the template already answers that question.

## Two things this provider will not do silently

- **A system message is never dropped.** If the template declares no
  `{system}` placeholder and the request carries a system message, the system
  text is prepended to the prompt. Half of every reflection prompt in this
  runtime is its system message; sending the request without it would look
  like a working provider giving worse answers. Same rule ADR 0112 applied to
  `max_tokens`.
- **`max_tokens` is dropped**, as it is on all three harness adapters: a
  generic CLI has no output-length flag this template can name. A caller
  setting `max_tokens=400` gets whatever the model writes.

Token counts are reported only when `usage_pointer` names them. An absent
pointer yields 0, and 0 here means *not extracted*, not *free*.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence

from aef.providers.base import (
    CompletionRequest,
    CompletionResult,
    ModelProvider,
    ModelProviderError,
    validate_isolation,
)
from aef.providers.harness_provider import (
    PROBE_REQUEST,
    HarnessRun,
    persona_channel,
    split_request,
)

PROMPT_PLACEHOLDER = "{prompt}"
MODEL_PLACEHOLDER = "{model}"
SYSTEM_PLACEHOLDER = "{system}"

OUTPUT_MODES: frozenset[str] = frozenset({"stdout", "last_line", "json_pointer"})
"""How the reply is dug out of what the CLI printed.

- `stdout` — the whole of stdout, stripped of a trailing newline.
- `last_line` — the last non-empty line, for CLIs that log progress first.
- `json_pointer` — parse stdout as JSON and follow `output_pointer`
  (RFC 6901: `/text`, `/result`, `/choices/0/message/content`).
"""

CommandRunner = Callable[[Sequence[str], float, str | None], HarnessRun]
"""Executes argv with a timeout, optionally writing text to the child's
stdin. Distinct from `harness_provider.Runner`, which always closes stdin;
this one has to be able to feed it."""


def subprocess_command_runner(
    argv: Sequence[str], timeout_s: float, stdin_text: str | None
) -> HarnessRun:
    try:
        completed = subprocess.run(
            list(argv),  # a LIST: there is no shell, so there is nothing to quote
            capture_output=True,
            text=True,
            input=stdin_text,
            stdin=None if stdin_text is not None else subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ModelProviderError(f"command executable not found: {argv[0]!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ModelProviderError(f"command exceeded {timeout_s:.0f}s: {argv[0]!r}") from exc
    return HarnessRun(
        returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
    )


def _count_placeholder(template: Sequence[str], placeholder: str, where: str) -> int:
    """Exact-element occurrences, refusing an embedded one.

    `["--prompt={prompt}"]` would otherwise be sent to the model as the
    literal eight characters `{prompt}` — a template that validates, runs,
    and silently asks the model the wrong question."""
    embedded = [e for e in template if placeholder in e and e != placeholder]
    if embedded:
        raise ValueError(
            f"{where} embeds {placeholder} inside {embedded[0]!r}. A placeholder must be a "
            f"WHOLE argv element so the value is passed as exactly one argument — an "
            f"embedded one is never substituted and would be sent as literal text."
        )
    return sum(1 for e in template if e == placeholder)


def validate_template(
    argv: Sequence[str],
    *,
    model_argv: Sequence[str] = (),
    system_argv: Sequence[str] = (),
    stdin: bool = False,
) -> None:
    """Raises `ValueError` on any template that would run and be wrong.

    Called both by `CommandProvider.__init__` and by
    `aef.config.schema.CommandProviderConfig`, so a bad `aef.yaml` fails at
    load time and a bad hand-built provider fails at construction — one rule,
    two doors, no drift (ADR 0091).

    `{model}` and `{system}` in `argv` are **slots**, not values: each marks
    the position where `model_argv` / `system_argv` is spliced in when the
    request carries that value, and collapses to nothing when it does not.
    Slots rather than an append at the end, because argv ORDER is part of a
    CLI's contract — `claude` wants its flags before the positional prompt,
    and a template that could only append could never reproduce that."""
    if not argv:
        raise ValueError("command.argv is empty: there is no program to run")
    prompts = _count_placeholder(argv, PROMPT_PLACEHOLDER, "command.argv")
    if stdin:
        if prompts:
            raise ValueError(
                "command.argv names {prompt} while command.stdin is true. The prompt would "
                "be sent twice — once as an argument and once on stdin. Remove one."
            )
    elif prompts != 1:
        raise ValueError(
            f"command.argv must name {PROMPT_PLACEHOLDER} exactly once (found {prompts}), "
            f"or set command.stdin: true to send the prompt on stdin instead."
        )
    for fragment, placeholder, name in (
        (model_argv, MODEL_PLACEHOLDER, "model_argv"),
        (system_argv, SYSTEM_PLACEHOLDER, "system_argv"),
    ):
        slots = _count_placeholder(argv, placeholder, "command.argv")
        if slots > 1:
            raise ValueError(
                f"command.argv names the {placeholder} slot {slots} times; it marks ONE "
                f"position where command.{name} is spliced in."
            )
        if slots and not fragment:
            raise ValueError(
                f"command.argv names the {placeholder} slot but command.{name} is empty, so "
                f"the slot expands to nothing and the value never reaches the CLI."
            )
        if fragment and not slots:
            raise ValueError(
                f"command.{name} is set but command.argv names no {placeholder} slot, so it "
                f"would never be spliced in. A block that validates and is ignored reads as "
                f"a control that exists."
            )
        if not fragment:
            continue
        if _count_placeholder(fragment, PROMPT_PLACEHOLDER, f"command.{name}"):
            raise ValueError(
                f"command.{name} names {PROMPT_PLACEHOLDER}. That fragment is CONDITIONAL, "
                f"so the prompt would go unsent whenever the condition does not hold."
            )
        found = _count_placeholder(fragment, placeholder, f"command.{name}")
        if found != 1:
            raise ValueError(
                f"command.{name} must name {placeholder} exactly once (found {found}); "
                f"otherwise the fragment is spliced in and the value never reaches it."
            )


CHANNEL_PROPERTIES: frozenset[str] = frozenset({"system_role", "user_turn_persona"})


def validate_command_isolation(isolation: Sequence[str]) -> frozenset[str]:
    """The owner's containment assertion, checked for spelling and for reach.

    Called from `CommandProvider.__init__` and from
    `aef.config.schema.CommandProviderConfig`, the same one-rule-two-doors
    arrangement `validate_template` uses (ADR 0091).

    The channel properties are refused rather than accepted-and-overridden:
    whether the persona reaches a system channel is decided by the presence of
    a `{system}` slot in the template, which this class can read, so an owner
    asserting it is either agreeing with something already known or claiming
    something false. An error naming the template is more use than either."""
    names = validate_isolation(isolation, where="command.isolation")
    overreach = sorted(names & CHANNEL_PROPERTIES)
    if overreach:
        raise ValueError(
            f"command.isolation names {overreach}, which is not the owner's to assert: the "
            f"persona's channel is derived from whether command.argv has a {SYSTEM_PLACEHOLDER} "
            f"slot. Add or remove the slot instead."
        )
    return names


def resolve_pointer(document: object, pointer: str) -> object | None:
    """RFC 6901 JSON pointer, returning `None` for anything absent.

    `""` is the whole document. `~1` is `/` and `~0` is `~`, in that order —
    the escape rule people get backwards."""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ModelProviderError(
            f"JSON pointer {pointer!r} must start with '/' (or be '' for the whole document)"
        )
    current: object = document
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit():
            index = int(token)
            if index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current


class CommandProvider(ModelProvider):
    """One single-shot CLI, described entirely by `aef.yaml`. See the module
    docstring for the security properties and the two things it refuses to
    drop silently."""

    name = "command"

    @property
    def default_model(self) -> str | None:
        return self._default_model

    @property
    def asserted_isolation(self) -> frozenset[str]:
        """Exactly what the owner wrote in `command.isolation`, unverified.

        Kept separate from `isolation` so a reader — and the trace — can tell
        the asserted half from the derived half without re-deriving it."""
        return self._asserted_isolation

    @property
    def isolation(self) -> frozenset[str]:
        """The owner's assertion, plus the channel this class derives itself.

        The channel is read back off `build()` for a fixed probe request
        rather than from `self._system_argv` directly, so it is the argv that
        actually goes to the CLI that decides — the same evidence the three
        hand-written adapters use, and the reason `PROBE_REQUEST` carries
        sentinels no template can collide with."""
        argv, stdin_text = self.build(PROBE_REQUEST)
        scanned = [*argv, stdin_text] if stdin_text is not None else argv
        return self._asserted_isolation | persona_channel(scanned)

    def __init__(
        self,
        *,
        argv: Sequence[str],
        model_argv: Sequence[str] = (),
        system_argv: Sequence[str] = (),
        stdin: bool = False,
        output: str = "stdout",
        output_pointer: str | None = None,
        usage_pointer: str | None = None,
        output_usage_pointer: str | None = None,
        isolation: Sequence[str] = (),
        default_model: str | None = None,
        timeout_s: float = 600.0,
        runner: CommandRunner = subprocess_command_runner,
    ) -> None:
        validate_template(argv, model_argv=model_argv, system_argv=system_argv, stdin=stdin)
        self._asserted_isolation = validate_command_isolation(isolation)
        if output not in OUTPUT_MODES:
            raise ValueError(f"command.output={output!r} is not one of {sorted(OUTPUT_MODES)}")
        if output == "json_pointer" and output_pointer is None:
            raise ValueError(
                "command.output is 'json_pointer' but command.output_pointer is unset: "
                "there is nothing to follow, so every reply would be empty."
            )
        if output != "json_pointer" and output_pointer is not None:
            raise ValueError(
                f"command.output_pointer is set while command.output is {output!r}, where it "
                f"is never read. A field that validates and is ignored reads as a control "
                f"that exists; set output: json_pointer or drop the pointer."
            )
        self._argv = tuple(argv)
        self._model_argv = tuple(model_argv)
        self._system_argv = tuple(system_argv)
        self._stdin = stdin
        self._output = output
        self._output_pointer = output_pointer
        self._usage_pointer = usage_pointer
        self._output_usage_pointer = output_usage_pointer
        self._default_model = default_model
        self._timeout_s = timeout_s
        self._runner = runner

    # -- argv ---------------------------------------------------------------
    @staticmethod
    def _render(template: Sequence[str], placeholder: str, value: str) -> list[str]:
        return [value if element == placeholder else element for element in template]

    def build(self, request: CompletionRequest) -> tuple[list[str], str | None]:
        """The argv to run and the text to write to stdin (or `None`).

        Public because the proof that this template is sufficient is an argv
        comparison against `ClaudeCodeProvider.argv` (ADR 0154), and a
        comparison needs something to compare."""
        system, prompt = split_request(request)
        model = request.model or self._default_model
        if system is not None and not self._system_argv:
            # Never dropped. See the module docstring.
            prompt = f"{system}\n\n{prompt}"
            system = None
        argv: list[str] = []
        for element in self._argv:
            if element == PROMPT_PLACEHOLDER:
                # Reached only when `stdin` is false — `validate_template`
                # refuses a template that has both.
                argv.append(prompt)
            elif element == MODEL_PLACEHOLDER:
                if model:
                    argv += self._render(self._model_argv, MODEL_PLACEHOLDER, model)
            elif element == SYSTEM_PLACEHOLDER:
                if system is not None:
                    argv += self._render(self._system_argv, SYSTEM_PLACEHOLDER, system)
            else:
                argv.append(element)
        return argv, (prompt if self._stdin else None)

    # -- output -------------------------------------------------------------
    def _parse(self, stdout: str) -> object:
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ModelProviderError(f"command returned non-JSON output: {stdout[:200]!r}") from exc

    def _content(self, stdout: str) -> str:
        if self._output == "stdout":
            return stdout.rstrip("\n")
        if self._output == "last_line":
            lines = [line for line in stdout.splitlines() if line.strip()]
            if not lines:
                raise ModelProviderError("command printed nothing to extract a last line from")
            return lines[-1]
        assert self._output_pointer is not None  # guaranteed by __init__
        found = resolve_pointer(self._parse(stdout), self._output_pointer)
        if found is None:
            raise ModelProviderError(
                f"command output pointer {self._output_pointer!r} resolved to nothing in "
                f"{stdout[:200]!r}"
            )
        return found if isinstance(found, str) else json.dumps(found)

    def _usage(self, stdout: str) -> tuple[int, int]:
        if self._usage_pointer is None and self._output_usage_pointer is None:
            return 0, 0
        payload = self._parse(stdout)
        counts: list[int] = []
        for pointer in (self._usage_pointer, self._output_usage_pointer):
            found = None if pointer is None else resolve_pointer(payload, pointer)
            counts.append(int(found) if isinstance(found, (int, float)) else 0)
        return counts[0], counts[1]

    def complete(self, request: CompletionRequest) -> CompletionResult:
        argv, stdin_text = self.build(request)
        run = self._runner(argv, self._timeout_s, stdin_text)
        if run.returncode != 0:
            raise ModelProviderError(
                f"{argv[0]} exited {run.returncode}: "
                f"{run.stderr.strip()[-500:] or run.stdout[-500:]}"
            )
        input_tokens, output_tokens = self._usage(run.stdout)
        return CompletionResult(
            content=self._content(run.stdout),
            model=request.model or self._default_model or "",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            # No generic CLI reports one, and inventing "end_turn" would be a
            # claim about a run this provider cannot see inside.
            stop_reason=None,
        )
