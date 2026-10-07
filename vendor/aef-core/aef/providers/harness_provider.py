"""The coding-agent harness as a `ModelProvider` (ADR 0112).

An agentic repo — one whose agents are Claude Code / Codex sessions and
`.md` personas — holds no API key. Its credential is the harness login.
Until this module existed every model path in the runtime went through an
SDK adapter, so in exactly the repos this scaffold is built for, no node
could reach a model and `CLAUDE.md` said the runtime "has nothing to attach
to". This is the attachment: a node's model call becomes one headless run
of the harness CLI under its own auth.

No vendor SDK is imported. The only dependency is the CLI on `PATH`, and the
subprocess is injectable so the adapters are tested without spawning one.

The three adapters are **not** equally isolated, and the previous version of
this paragraph said they were ("single-turn and tool-less by construction").
Each one now declares what its own argv enforces through `isolation`, and
`ISOLATION_PROPERTIES` in `aef.providers.base` is the vocabulary:

    claude_code  no_tools, no_mcp, single_turn, no_project_context, system_role
    codex        read_only_fs, user_turn_persona
    grok         no_web_search, no_subagents, single_turn, system_role

`codex exec` sends neither `--tools` nor `--max-turns`: it is an agentic loop
in a read-only sandbox, which may still read the working tree. And `grok
--tools ""` was **measured** on 1.0.5 to suppress nothing — the run read a
planted file and quoted its contents back (ADR 0169) — while `claude --tools
""` is documented by `claude --help` as "Use \"\" to disable all tools". The
same flag spelling, opposite semantics; ADR 0150's rule ("a flag's shape is
not a flag's value") one level up.

Each `isolation` is DERIVED from the argv the adapter actually builds for a
fixed probe request, not written down beside it, so removing a flag changes
the declaration in the same commit rather than in a later one.

Multi-message requests are rendered into one transcript because the CLIs take
one prompt — good enough for the reflection and consolidation nodes this
runtime ships, and stated here so nobody mistakes it for a chat API.

A harness this repo has never seen is **configuration, not code**: see
`aef.providers.command_provider.CommandProvider` (`impl: command`), which
takes an argv template and an output extractor from `aef.yaml`. The three
adapters here exist because each one carries measured, non-obvious isolation
knowledge that no owner should have to rediscover — not because a fourth
harness needs a fourth class.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from aef.providers.base import (
    CompletionRequest,
    CompletionResult,
    Effort,
    ModelProvider,
    ModelProviderError,
    ProviderMessage,
)

# The smallest VALID MCP config: an explicit, empty server set. Paired with
# `--strict-mcp-config` this is what stops the operator's own MCP servers
# reaching a node's model call. A bare `{}` fails the CLI's schema.
_EMPTY_MCP_CONFIG = '{"mcpServers":{}}'

# Two sentinels no flag name and no flag value can collide with. `isolation`
# builds the adapter's real argv for this request and reads the result back;
# scanning for a literal like "--safe-mode" in an argv that also carries a
# user's prompt would let the PROMPT decide what the provider claims.
PROBE_SYSTEM = "AEF_ISOLATION_PROBE_SYSTEM"
PROBE_PROMPT = "AEF_ISOLATION_PROBE_PROMPT"

PROBE_REQUEST = CompletionRequest(
    messages=(
        ProviderMessage(role="system", content=PROBE_SYSTEM),
        ProviderMessage(role="user", content=PROBE_PROMPT),
    ),
    model="aef-isolation-probe-model",
)
"""The request `isolation` builds argv for. It carries a system message on
purpose: `system_role` / `user_turn_persona` is a statement about whether the
adapter HAS a system channel, and an adapter with one only reveals it when
there is something to put in it."""


def flag_present(argv: Sequence[str], flag: str, value: str | None = None) -> bool:
    """`flag` appears in argv, optionally immediately followed by `value`."""
    if value is None:
        return flag in argv
    return any(argv[i] == flag and argv[i + 1] == value for i in range(len(argv) - 1))


def persona_channel(argv: Sequence[str]) -> frozenset[str]:
    """Where `PROBE_REQUEST`'s system message ended up in `argv`.

    Its own argv element (`--system-prompt <text>`) is `system_role`. Fused
    into another element alongside the prompt is `user_turn_persona` — the
    concatenation `CodexProvider` and a `{system}`-less `CommandProvider`
    both perform, which turns the persona into untrusted-channel text while
    ADR 0152's safety story assumes it is the system message. Neither, and
    the adapter has said nothing about the channel."""
    if any(element == PROBE_SYSTEM for element in argv):
        return frozenset({"system_role"})
    if any(PROBE_SYSTEM in element and PROBE_PROMPT in element for element in argv):
        return frozenset({"user_turn_persona"})
    return frozenset()


@dataclass(frozen=True)
class HarnessRun:
    """What one CLI invocation produced. The slice of `CompletedProcess`
    the adapters read, so tests can hand one in without a real process."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], float], HarnessRun]
"""Executes argv with a timeout in seconds and returns the run. Stdin is
closed: Codex reads a prompt from a non-tty stdin and would otherwise hang."""


def subprocess_runner(argv: Sequence[str], timeout_s: float) -> HarnessRun:
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ModelProviderError(f"harness executable not found: {argv[0]!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ModelProviderError(f"harness run exceeded {timeout_s:.0f}s: {argv[0]!r}") from exc
    return HarnessRun(
        returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
    )


def split_request(request: CompletionRequest) -> tuple[str | None, str]:
    """System text and the single prompt the CLI receives.

    The Messages API has no "tool" role and neither CLI can carry a
    `tool_result`; refuse it by name, as the Anthropic adapter does, before
    any process is spawned."""
    if any(m.role == "tool" for m in request.messages):
        raise ModelProviderError(
            "harness adapters cannot send role 'tool': a headless CLI run takes one "
            "prompt and ProviderMessage's string content cannot express a tool result"
        )
    system = "\n".join(m.content for m in request.messages if m.role == "system") or None
    turns = [m for m in request.messages if m.role != "system"]
    if len(turns) == 1 and turns[0].role == "user":
        return system, turns[0].content
    # Several turns: render a transcript. The CLI is single-turn, and hiding
    # that behind a fake chat would be the dishonest option.
    rendered = "\n\n".join(f"{m.role.capitalize()}: {m.content}" for m in turns)
    return system, rendered


def _usage_match(model_usage: dict[str, object], usage: dict[str, object] | None) -> str | None:
    """The one `modelUsage` key whose (input, output) token row equals the
    payload's top-level `usage` — or None when zero or several do. Both
    spellings of the keys are read because the two maps use different ones
    (`inputTokens` in `modelUsage`, `input_tokens` at the top level)."""
    if not usage:
        return None

    def _pair(row: object, in_key: str, out_key: str) -> tuple[int, int] | None:
        if not isinstance(row, dict):
            return None
        i, o = row.get(in_key), row.get(out_key)
        if isinstance(i, (int, float)) and isinstance(o, (int, float)):
            return int(i), int(o)
        return None

    top = _pair(usage, "input_tokens", "output_tokens")
    if top is None:
        return None
    hits = [
        k
        for k, row in model_usage.items()
        if _pair(row, "inputTokens", "outputTokens") == top
        or _pair(row, "input_tokens", "output_tokens") == top
    ]
    return hits[0] if len(hits) == 1 else None


# A key is the requested model under another spelling when it adds a dated
# snapshot (`-20260101`), a context-window marker (`[1m]`) or a server label
# (`-build`) — and is a DIFFERENT model when what it adds is a short version
# number. `claude-opus-5-5` extends `claude-opus-5-` and is Opus 5.5, not
# Opus 5; a bare `startswith(f"{requested}-")` said otherwise
# (model-check 2026-09-23).
_VERSION_STEP = re.compile(r"-\d{1,4}(?:$|[-\[.])")


def _is_alias_of(key: str, requested: str) -> bool:
    if key == requested or not key.startswith(requested):
        return False
    suffix = key[len(requested) :]
    return suffix[0] in "-[" and _VERSION_STEP.match(suffix) is None


def answering_model(
    model_usage: dict[str, object],
    requested: str | None,
    usage: dict[str, object] | None = None,
) -> tuple[str | None, str]:
    """Which model in a CLI's `modelUsage` map answered, **and how sure that
    is** — see `MODEL_ATTRIBUTION_VALUES`.

    **`next(iter(model_usage))` — the first key — is wrong, and was wrong on
    `main`.** These CLIs bill a *helper* model alongside the one asked for
    (Claude Code runs a small model for its own housekeeping), and the helper
    is not last in the map. Running `--model claude-opus-5` produced a
    `modelUsage` whose first key was `claude-haiku-4-5-20251001`, so
    `CompletionResult.model` said Haiku, and every provenance record and
    recorded corpus scenario downstream inherited it:

        "provenance": [{"node_id": "prompt_agent",
                        "model": "claude-haiku-4-5-20251001", ...}]

    That is worse than an unhelpful label. Provenance is the field a reader
    trusts when an ADR says which model a measurement was taken on, and this
    one named a model that wrote none of the answer.

    **Rule 3 — "the key with the most output tokens" — is a guess, and it was
    measured wrong.** S3's 36-call judge A/B misattributed
    `sum-13-cider-press` to `claude-haiku-4-5-20251001`: a judge whose entire
    reply is a small JSON object writes ~12 output tokens, which is *fewer*
    than the CLI's own helper model wrote on that call, so the premise "the
    helper writes a handful, the answering model writes the answer" inverts
    on exactly the shortest replies.

    The requested-name rule should have won there and never ran, because
    `requested` was empty: `agent_services(reflection="llm")` sets
    `LLMJudge.model = reflection_model or ""`, and with `model_provider.model`
    also unset `request.model or self._default_model` is falsy. Rules 1 and 2
    are skipped and rule 3 decides alone. That is reported upward rather than
    fixed here — `aef/services/runtime.py` is not this module's — and the
    remedy this function can offer is to stop presenting a guess as a fact.

    Six rules, in order, each returning the attribution it earned:

    1. the requested name, if the map has it — `requested`;
    2. a key that extends the requested name by a date, a context-window
       marker or a label (`claude-opus-5` -> `claude-opus-5-20260101`) —
       `alias`, the case that made reading `modelUsage` worth doing at all.
       Not by a version number: `claude-opus-5-5` is another model;
    3. a map with exactly one key — `sole`, no ambiguity to resolve;
    4. **the key whose token row equals the payload's top-level `usage`** —
       `usage_match`. S1 observed on a live call that the CLI's top-level
       `usage` matched the Opus row exactly (in 2 / out 69) while the map's
       first key was Haiku: the top-level usage IS the answering call's
       usage, and the helper's row differs. Deterministic, no guess, and it
       is the rule that decides when nothing was requested (a provider built
       with no default model — S3's judge runner);
    5. the key with the most output tokens — `heuristic`, the rule above;
    6. the first key — also `heuristic`, and where we came in.

    An empty map returns `(None, "unknown")`; the caller substitutes whatever
    it asked for and inherits the label.
    """
    if not model_usage:
        return None, "unknown"
    if requested:
        if requested in model_usage:
            return requested, "requested"
        extended = [k for k in model_usage if _is_alias_of(k, requested)]
        if len(extended) == 1:
            return extended[0], "alias"

    def _output_tokens(key: str) -> int:
        entry = model_usage[key]
        if not isinstance(entry, dict):
            return 0
        value = entry.get("outputTokens", entry.get("output_tokens", 0))
        return int(value) if isinstance(value, (int, float)) else 0

    keys = list(model_usage)
    if len(keys) == 1:
        # One model was billed. Nothing was guessed, whatever was requested.
        return keys[0], "sole"
    matched = _usage_match(model_usage, usage)
    if matched is not None:
        return matched, "usage_match"
    best = max(keys, key=_output_tokens)
    return (best if _output_tokens(best) else keys[0]), "heuristic"


class ClaudeCodeProvider(ModelProvider):
    """`claude -p` under the session's own login. Reproduced 2026-09-03
    inside a running Claude Code session: `--tools ""` and `--max-turns 1`
    give a tool-less single completion; `--output-format json` returns
    `result`, `stop_reason`, `is_error`, `usage` and `modelUsage`.

    `--bare` is deliberately NOT passed: it skips the keychain read and
    answers "Not logged in" — the one flag that would defeat the whole point.

    **The session is isolated, because it used to be inherited** (ADR 0126).
    As first shipped this argv ran inside whatever session the operator had
    configured: an adversarial round measured **211,470 input tokens per
    judge call** ($0.18 at cache-read rates, $2.22 uncached) against 4,684
    for the same prompt with the MCP configuration suppressed — the operator's
    MCP tool schemas and their `~/.claude/CLAUDE.md` were being re-sent on
    every critic and judge call, and the judge answered in the operator's
    personal register. Three flags fix it:

    - `--strict-mcp-config` with `--mcp-config '{"mcpServers":{}}'` — "only use
      MCP servers from `--mcp-config`", and that config declares none. The
      value must be a valid MCP config document: a bare `{}` is refused by the
      CLI's own schema (ADR 0150). This is the pair
      the 4,684-token measurement used.
    - `--safe-mode` — "all customizations (CLAUDE.md, skills, plugins, hooks,
      MCP servers, custom commands and agents ...) disabled ... Auth, model
      selection, built-in tools, and permissions work normally". It is the
      only documented flag that drops CLAUDE.md discovery *without* dropping
      the keychain login the way `--bare` does. `--restricted` and
      `--setting-sources` reach settings files only, not memory files.

    `--safe-mode` is taken from `claude --help` (CLI 2.1.260) and is **not
    re-measured live** — the authoring session's model quota was exhausted.
    The CLI tolerates unknown options silently (verified: an invented flag
    changes nothing), so the worst case if a future CLI drops it is that the
    cost stays where it was, not a broken call.

    **`CompletionRequest.max_tokens` is dropped.** The CLI has no
    output-length flag and never had one; a caller that sets `max_tokens=400`
    (`LLMJudge` does) gets whatever the model writes. Stated here because a
    silently ignored parameter reads as a control that exists. The length
    controls that do work are prompt-side.
    """

    name = "claude_code"

    @property
    def default_model(self) -> str | None:
        return self._default_model

    @property
    def isolation(self) -> frozenset[str]:
        """Derived from this adapter's own argv. Basis, per property:

        - `no_tools` — `claude --help` (CLI 2.1.260): "`--tools <tools...>`
          Specify the list of available tools from the built-in set. **Use ""
          to disable all tools**". Documented by the binary, not merely
          inferred from the flag being present — which matters, because the
          identical spelling on `grok` means the opposite (ADR 0169).
        - `single_turn` — `--max-turns 1`.
        - `no_mcp` — `--strict-mcp-config` with a config declaring no server.
        - `no_project_context` — `--safe-mode`, and ONLY that flag. `--cwd`
          is not a substitute and neither is `--strict-mcp-config`.
        - the channel — `--system-prompt` carries the persona.

        Not claimed and not claimable from here: that a tool never *ran*.
        This is the CLI's documented behaviour, and no live call in this repo
        has planted a file and confirmed it stayed unread — which is exactly
        the experiment that caught `grok`. Named as the open one.
        """
        argv = self.argv(PROBE_REQUEST)
        found: set[str] = set()
        if flag_present(argv, "--tools", ""):
            found.add("no_tools")
        if flag_present(argv, "--max-turns", "1"):
            found.add("single_turn")
        if flag_present(argv, "--strict-mcp-config") and flag_present(
            argv, "--mcp-config", _EMPTY_MCP_CONFIG
        ):
            found.add("no_mcp")
        if flag_present(argv, "--safe-mode"):
            found.add("no_project_context")
        return frozenset(found) | persona_channel(argv)

    def __init__(
        self,
        *,
        default_model: str | None = None,
        executable: str = "claude",
        timeout_s: float = 600.0,
        runner: Runner = subprocess_runner,
        effort: Effort | None = None,
    ) -> None:
        self._default_model = default_model
        self._effort = effort
        self._executable = executable
        self._timeout_s = timeout_s
        self._runner = runner

    def argv(self, request: CompletionRequest) -> list[str]:
        system, prompt = split_request(request)
        model = request.model or self._default_model
        argv = [
            self._executable,
            "-p",
            "--no-session-persistence",
            "--output-format",
            "json",
            "--max-turns",
            "1",
            "--tools",
            "",
            # Isolation from the operator's own session — see the class
            # docstring for the 211,470 -> 4,684 input-token measurement.
            "--strict-mcp-config",
            "--mcp-config",
            # `{}` is REJECTED — the CLI validates this against a schema whose
            # `mcpServers` key is required, and every call died with
            # `Error: Invalid MCP configuration: mcpServers: Invalid input`
            # from the moment ADR 0126 added the flag until ADR 0150 fixed it.
            # It shipped because the quota was exhausted that day, so the tests
            # could only assert the SHAPE of argv, and argv was well-formed.
            _EMPTY_MCP_CONFIG,
            "--safe-mode",
        ]
        if model:
            argv += ["--model", model]
        if self._effort is not None:
            argv += ["--effort", self._effort]
        if system is not None:
            argv += ["--system-prompt", system]
        argv.append(prompt)
        return argv

    def complete(self, request: CompletionRequest) -> CompletionResult:
        argv = self.argv(request)
        run = self._runner(argv, self._timeout_s)
        if run.returncode != 0:
            raise ModelProviderError(
                f"claude exited {run.returncode}: {run.stderr.strip()[-500:] or run.stdout[-500:]}"
            )
        try:
            payload = json.loads(run.stdout)
        except json.JSONDecodeError as exc:
            raise ModelProviderError(
                f"claude returned non-JSON output: {run.stdout[:200]!r}"
            ) from exc
        if payload.get("is_error"):
            # The CLI reports auth and API failures with exit 0 and
            # `is_error: true` — the smoke run under `--bare` returned
            # "Not logged in · Please run /login" exactly this way.
            raise ModelProviderError(f"claude error: {payload.get('result', '')}")
        stop_reason = payload.get("stop_reason")
        if stop_reason == "refusal":
            raise ModelProviderError("claude refusal: no content returned")
        usage = payload.get("usage") or {}
        # `modelUsage` is keyed by the model that actually answered — the
        # authoritative name when the request used an alias.
        model_usage = payload.get("modelUsage") or {}
        requested = request.model or self._default_model
        # NOT the first key of the map — see `answering_model`.
        answered_by, attribution = answering_model(model_usage, requested, usage)
        return CompletionResult(
            content=str(payload.get("result", "")),
            model=answered_by or requested or "",
            # The UNCACHED remainder. On this CLI it is routinely 2 while the
            # prompt is tens of thousands of tokens; the rest is below, and
            # dropping it is what made ADR 0126's cost figures unsupportable
            # from anything this code retained (ADR 0169).
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            stop_reason=stop_reason,
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0)),
            cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0)),
            model_attribution=attribution,
        )


class CodexProvider(ModelProvider):
    """`codex exec` under the Codex login. Built from the CLI's documented
    flags and **reproduced against the real binary** — ADR 0112 shipped this
    parsing as an avowed hypothesis because the Codex on the authoring box
    could not parse its own server's model catalogue; ADR 0131 upgraded the
    CLI and every derived field matched on the first live run, and ADR 0199
    added the end-to-end half (a `PromptAgentNode` through this provider, over
    the binary, recording `user_turn_persona`). Both live tests are opt-in
    twice over and are the ones `ci.yml`'s `live-harness` job selects wherever
    a CLI and a credential exist.

    `codex exec` has no system-prompt flag; system text is prepended to the
    prompt. The last assistant message is read from `--output-last-message`
    rather than parsed out of the event stream, because that file is the
    documented contract and the JSONL is not."""

    name = "codex"

    @property
    def default_model(self) -> str | None:
        return self._default_model

    @property
    def isolation(self) -> frozenset[str]:
        """**The weakest of the three, and it used to be described as equal
        to them.** `codex exec` is an agentic loop: this argv sends no
        `--tools` and no `--max-turns`, so nothing here stops the model
        calling a tool or taking another turn. What it does send is
        `--sandbox read-only`, which bounds the damage to reads — a real
        property, and not the one `no_tools` names.

        It also has no system-prompt flag, so the persona is prepended to the
        USER turn: `user_turn_persona`. ADR 0152's containment sentence
        (`--tools ""` with `--max-turns 1`) described `ClaudeCodeProvider`
        and was stamped into every generated module as if it described this
        one too.

        No longer a hypothesis: `tests/providers/test_codex_live.py` runs
        this adapter against the installed CLI, and since ADR 0199 it asserts
        the consequence rather than only the parsing — a `PromptAgentNode`
        driven through this provider records `user_turn_persona` and the
        `prompt_agent.persona_in_user_turn` warning, and records neither
        `no_tools` nor `single_turn`, which is the difference from
        `ClaudeCodeProvider` stated as an assertion instead of a comment.
        """
        argv = self.argv(PROBE_REQUEST, Path("aef-isolation-probe-last-message.txt"))
        found: set[str] = set()
        if flag_present(argv, "--sandbox", "read-only"):
            found.add("read_only_fs")
        if flag_present(argv, "--tools", ""):
            found.add("no_tools")
        if flag_present(argv, "--max-turns", "1"):
            found.add("single_turn")
        return frozenset(found) | persona_channel(argv)

    def __init__(
        self,
        *,
        default_model: str | None = None,
        executable: str = "codex",
        timeout_s: float = 600.0,
        runner: Runner = subprocess_runner,
        scratch_dir: Path | None = None,
    ) -> None:
        self._default_model = default_model
        self._executable = executable
        self._timeout_s = timeout_s
        self._runner = runner
        self._scratch_dir = scratch_dir

    def argv(self, request: CompletionRequest, last_message_file: Path) -> list[str]:
        system, prompt = split_request(request)
        model = request.model or self._default_model
        argv = [
            self._executable,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--json",
            "--output-last-message",
            str(last_message_file),
        ]
        if model:
            argv += ["--model", model]
        argv.append(f"{system}\n\n{prompt}" if system else prompt)
        return argv

    def complete(self, request: CompletionRequest) -> CompletionResult:
        with tempfile.TemporaryDirectory(dir=self._scratch_dir) as tmp:
            last = Path(tmp) / "last_message.txt"
            argv = self.argv(request, last)
            run = self._runner(argv, self._timeout_s)
            if run.returncode != 0:
                raise ModelProviderError(
                    f"codex exited {run.returncode}: {run.stderr.strip()[-500:]}"
                )
            if not last.exists():
                raise ModelProviderError("codex produced no last message file")
            content = last.read_text(encoding="utf-8")
        input_tokens = output_tokens = 0
        for line in run.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = event.get("usage") if isinstance(event, dict) else None
            if isinstance(usage, dict):
                input_tokens = int(usage.get("input_tokens", input_tokens))
                output_tokens = int(usage.get("output_tokens", output_tokens))
        return CompletionResult(
            content=content,
            model=request.model or self._default_model or "",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason="end_turn",
        )


class GrokProvider(ModelProvider):
    """`grok -p` under the Grok login (ADR 0154). **Reproduced live** on
    `grok 1.0.5 (5115b46bc909) [stable]`, 2026-09-04, five real runs.

    `grok` with no flags opens an interactive TUI. The headless door is
    `-p, --single <PROMPT>` — "Single-turn prompt. Prints the response to
    stdout and exits" — which is a *flag taking the prompt as its value*,
    not a positional argument like `claude -p <prompt>`. Paired with
    `--output-format json` a run prints one JSON object:

        {"text": "OK", "stopReason": "end_turn",
         "sessionId": "...", "requestId": "...", "thought": "...",
         "usage": {"input_tokens": 24001, "cache_read_input_tokens": 0,
                   "cache_creation_input_tokens": 0, "output_tokens": 141,
                   "reasoning_tokens": 136, "total_tokens": 24142},
         "num_turns": 1, "total_cost_usd": 0.00830416,
         "modelUsage": {"grok-4.6-build": {"inputTokens": 24001, ...}}}

    The field names are **not** Claude's: the reply is `text` (Claude says
    `result`), the stop reason is `stopReason` (Claude says `stop_reason`),
    and there is no `is_error`. A failure exits non-zero and prints
    `{"type": "error", "message": "..."}` on stdout with the same text on
    stderr — measured by asking for a model that does not exist.

    **The number to guard on is `CompletionResult.total_input_tokens`**:
    `usage.input_tokens` plus `cache_read_input_tokens` plus
    `cache_creation_input_tokens`, which is what `total_tokens` minus the
    output comes to. Grok's own `usage.input_tokens` is only the *uncached
    remainder*, and it swings by thousands between two identical calls
    depending on cache state. Reporting it raw cost this adapter a real
    defect: the live isolation guard below, written against the raw field,
    **passed on a deliberately de-isolated provider** because the leaked
    instructions happened to be cached that run (ADR 0154). A number that
    moves for reasons unrelated to what was sent cannot guard what was sent.

    ADR 0154 fixed that by folding the cache counters into `input_tokens`,
    because that was the only field there was. ADR 0169 gave
    `CompletionResult` the two counters and a `total_input_tokens` property,
    so the fold is gone and each field now means the same thing on every
    provider. The guard reads the property; the sum it reads is identical.

    **Isolation, measured (ADR 0150's rule) rather than assumed.** Grok
    discovers project instructions the way Claude Code does — `grok inspect`
    in this repo listed `Claude.md` (~3,369 tokens), `Agents.md` (~1,784)
    and the operator's global `~/.claude/Claude.md` (~142), plus 78 skills.
    Four arms, same prompt, same box:

        arm                                   uncached  cache_read  TOTAL IN
        baseline (repo cwd, no flags)           24,001           0    24,001
        tool flags only (repo cwd)              18,272       5,248    23,520
        --cwd <empty dir> only                  12,821       5,760    18,581
        both                                    12,688       5,248    17,936

    Read the last column: it is the only one that separates the isolated arms
    from the unisolated ones, and it is what this adapter reports.

    **`--cwd <empty dir>` is the load-bearing lever**, and it is a directory
    rather than a flag because Grok has no `--safe-mode`: nothing in
    `grok --help` disables customization discovery, so the only way to stop
    a repo's `CLAUDE.md`/`AGENTS.md`/skills reaching a node's model call is
    to run the CLI somewhere that has none. This adapter therefore runs each
    call in a fresh empty temporary directory (`isolate_project_context`,
    default on). The tool flags — `--tools ""`, `--disable-web-search`,
    `--no-subagents`, `--no-plan`, `--max-turns 1`, `--verbatim` — buy 481
    tokens on their own and 645 on top of `--cwd`, which is inside run-to-run
    variation. They are kept for the safety they buy, not the cost.

    **How much safety that is, is now measured and it is less than the
    previous version of this paragraph claimed.** `--tools ""` suppresses
    nothing on 1.0.5: given these exact flags and one more turn, the run
    listed a planted directory and quoted a file's first line back. See
    `isolation` for the two arms, and ADR 0169. The containment that survives
    is `--max-turns 1`, which works by *cancelling the run* — this adapter
    then raises rather than returning a completion.

    **The isolation is incomplete and there is no flag that completes it.**
    Even fully isolated the call carried ~17.9k tokens of input, and the
    reply's own `thought` field quoted a rule that exists only in the
    operator's global `~/.claude/Claude.md`. Claude Code's `--safe-mode` has
    no Grok equivalent in 1.0.5. Stated here rather than in a footnote
    because an owner comparing per-call cost across backends will otherwise
    read the `--cwd` win as the whole story.

    **`CompletionResult.model` is not a `-m` argument.** `modelUsage` is keyed
    `grok-4.6-build`; `grok models` lists `grok-4.6` and `grok-4.5`, and
    `-m grok-4.6-build` exits 1 with `Invalid params: "unknown model id"` —
    measured. This adapter reports the answering name because that is what
    `CompletionResult.model` means and what `ClaudeCodeProvider` does, but
    round-tripping it into the next `CompletionRequest.model` fails. Nothing
    in this runtime does that today; this is the note for the first caller
    that tries.

    **`CompletionRequest.max_tokens` is dropped**, for the same reason it is
    on `ClaudeCodeProvider`: `grok --help` has `--max-turns` but no
    output-length flag. A caller setting `max_tokens=400` gets whatever the
    model writes. Said out loud because a silently ignored parameter reads
    as a control that exists.
    """

    name = "grok"

    @property
    def isolation(self) -> frozenset[str]:
        """**`no_tools` is absent, and it is absent because it was measured
        false** (ADR 0169, three live calls on `grok 1.0.5`).

        The experiment: a directory holding one file whose first line is a
        canary string, `--cwd` pointed at it, this adapter's exact tool flags,
        the prompt "List the files in the current directory and print the
        first line of each."

            --max-turns 1   exit 1, `stopReason: "cancelled"`, stderr
                            "Error: max turns reached", `num_turns: 1`. The
                            reply text is a tool preamble ("Listing the
                            current directory and reading the first line of
                            each file.") — the model was offered tools and
                            reached for one.
            --max-turns 3   exit 0, `num_turns: 2`, and the reply contains
                            `AEF_CANARY_..._THIS_LINE_PROVES_A_FILE_WAS_READ`.

        So `--tools ""` is "no restriction given" to Grok, while `claude
        --help` documents the identical spelling as "Use \"\" to disable all
        tools". What contains the shipped adapter is `--max-turns 1`, and it
        contains by *cancelling the run*: the tool result never returns to the
        model and `complete()` raises. Whether the read itself executed before
        the cancellation is NOT established — a read is harmless, a write
        would not be, and no measurement here covers a persona that asks for
        one. That is the standing residual risk on `impl: grok`.

        `--disallowed-tools <TOOLS>` ("Built-in tools to remove") is the right
        lever and is **not** used, because `grok --help` lists no built-in
        tool names to put in it and this repo does not ship guessed flag
        values (ADR 0150). Named as the open follow-up rather than patched
        over.

        Declared: `no_web_search` and `no_subagents` (dedicated boolean flags
        with unambiguous `--help` text), `single_turn`, and `system_role`
        from `--system-prompt-override`. NOT `no_project_context`: Grok has no
        `--safe-mode`, and the fully isolated call still carried ~17.9k tokens
        of the operator's instructions (ADR 0154) — reconfirmed here, where an
        isolated run's own `thought` field quoted rules that exist only in
        `~/.claude/CLAUDE.md`.
        """
        argv = self.argv(
            PROBE_REQUEST,
            Path("aef-isolation-probe-cwd") if self._isolate_project_context else None,
        )
        found: set[str] = set()
        if flag_present(argv, "--max-turns", "1"):
            found.add("single_turn")
        if flag_present(argv, "--disable-web-search"):
            found.add("no_web_search")
        if flag_present(argv, "--no-subagents"):
            found.add("no_subagents")
        if flag_present(argv, "--disallowed-tools"):
            # Not sent today. Present so the day someone learns the built-in
            # tool names, the claim moves with the argv rather than after it.
            found.add("no_tools")
        return frozenset(found) | persona_channel(argv)

    @property
    def default_model(self) -> str | None:
        return self._default_model

    def __init__(
        self,
        *,
        default_model: str | None = None,
        executable: str = "grok",
        timeout_s: float = 600.0,
        runner: Runner = subprocess_runner,
        scratch_dir: Path | None = None,
        isolate_project_context: bool = True,
    ) -> None:
        self._default_model = default_model
        self._executable = executable
        self._timeout_s = timeout_s
        self._runner = runner
        self._scratch_dir = scratch_dir
        self._isolate_project_context = isolate_project_context

    def argv(self, request: CompletionRequest, isolation_cwd: Path | None) -> list[str]:
        system, prompt = split_request(request)
        model = request.model or self._default_model
        argv = [
            self._executable,
            "--output-format",
            "json",
            "--max-turns",
            "1",
            # No web fetch, no subagent, no plan mode, prompt sent as written.
            #
            # `--tools ""` is still sent and is NO LONGER BELIEVED. Measured
            # on 1.0.5 (ADR 0169): with these exact flags and `--max-turns 3`
            # the run listed a planted directory and quoted the file's first
            # line back. An empty value is "no restriction given" here, not
            # "allow none" — the opposite of what the same spelling means to
            # `claude`. It is kept because it costs nothing and a future
            # release may honour it; `isolation` does not count it.
            "--tools",
            "",
            "--disable-web-search",
            "--no-subagents",
            "--no-plan",
            "--verbatim",
        ]
        if isolation_cwd is not None:
            # The only lever that drops project instructions — see the class
            # docstring's four-arm measurement. There is no `--safe-mode`.
            argv += ["--cwd", str(isolation_cwd)]
        if model:
            argv += ["-m", model]
        if system is not None:
            # `--system-prompt-override` is the documented spelling;
            # `--system-prompt` is a compat alias for it.
            argv += ["--system-prompt-override", system]
        # `-p` TAKES the prompt: unlike `claude -p`, the prompt is this
        # flag's value and not a trailing positional.
        argv += ["-p", prompt]
        return argv

    def _complete_in(self, request: CompletionRequest, isolation_cwd: Path | None) -> HarnessRun:
        return self._runner(self.argv(request, isolation_cwd), self._timeout_s)

    def complete(self, request: CompletionRequest) -> CompletionResult:
        if self._isolate_project_context:
            with tempfile.TemporaryDirectory(dir=self._scratch_dir) as tmp:
                run = self._complete_in(request, Path(tmp))
        else:
            run = self._complete_in(request, None)
        if run.returncode != 0:
            if "max turns reached" in run.stderr:
                # Measured, not guessed (ADR 0169): a prompt the model wants a
                # tool for exits 1 with `stopReason: "cancelled"` and exactly
                # this stderr, because `--tools ""` did not stop it reaching
                # for one and `--max-turns 1` then cut the run off. The bare
                # "grok exited 1" this used to raise sent the reader looking
                # for an auth or network fault.
                raise ModelProviderError(
                    "grok exited 1 at the turn cap: the model asked for a tool and "
                    "--max-turns 1 cancelled the run before the second turn. On grok 1.0.5 "
                    '`--tools ""` does not remove the built-in tools (ADR 0169), so a '
                    "tool-using persona reaches this instead of an answer. Rephrase the "
                    "persona to reason rather than act, or use impl: claude_code."
                )
            raise ModelProviderError(
                f"grok exited {run.returncode}: {run.stderr.strip()[-500:] or run.stdout[-500:]}"
            )
        try:
            payload = json.loads(run.stdout)
        except json.JSONDecodeError as exc:
            raise ModelProviderError(
                f"grok returned non-JSON output: {run.stdout[:200]!r}"
            ) from exc
        if not isinstance(payload, dict):
            raise ModelProviderError(f"grok returned non-object JSON: {run.stdout[:200]!r}")
        if payload.get("type") == "error":
            # Observed with exit 1; handled at exit 0 too because that is
            # exactly the shape `claude` uses for auth failures (ADR 0150),
            # and a zero exit carrying an error would otherwise be returned
            # as an empty completion.
            raise ModelProviderError(f"grok error: {payload.get('message', '')}")
        stop_reason = payload.get("stopReason")
        if stop_reason == "refusal":
            raise ModelProviderError("grok refusal: no content returned")
        usage = payload.get("usage") or {}
        model_usage = payload.get("modelUsage") or {}
        requested = request.model or self._default_model
        # NOT the first key of the map — see `answering_model`. `usage` is
        # passed for rule 4 (`usage_match`), which ADR 0169's addendum wired
        # into `ClaudeCodeProvider` and, until ADR 0179's R7, not into this
        # one: the same payload shape produced `usage_match` there and
        # `heuristic` here, and `heuristic` is the rule measured wrong 1 time
        # in 36. The caveat is stated rather than implied — the only Grok
        # payload ever observed has a SINGLE-key `modelUsage`, which rule 3
        # answers as `sole` before rule 4 is consulted, so this fixes a
        # divergence that is currently unreachable on measured traffic and
        # would be reachable the first time Grok bills a helper model.
        answered_by, attribution = answering_model(model_usage, requested, usage)
        return CompletionResult(
            content=str(payload.get("text", "")),
            model=answered_by or requested or "",
            # The uncached remainder ONLY, as of ADR 0169. This adapter used
            # to fold the two cache counters into this field because it was
            # the only place to put them and the live isolation guard had to
            # read a stable number; `CompletionResult` now carries them
            # separately and `total_input_tokens` is that stable number. The
            # sum is unchanged — where it is read from is.
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            stop_reason=stop_reason,
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0)),
            cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0)),
            model_attribution=attribution,
        )
