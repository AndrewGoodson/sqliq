"""`Tool` interface, `PolicyEngine`, and audit logging (constraint #6).

Security default is deny: a tool with no declared scopes is denied, a scope
not in the allowlist is denied, and any call whose risk exceeds the
configured threshold requires explicit human-in-the-loop approval rather
than executing. This module assumes prompt injection will sometimes
succeed and is designed for containment (least-privilege scopes, explicit
HITL gates, a full audit trail), not detection.
"""

from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    tool_name: str
    arguments: dict[str, Any]
    risk: float = 0.0  # 0..1 blast-radius estimate for this specific call

    def __post_init__(self) -> None:
        # `call.risk > threshold` silently evaluates to False for a NaN
        # risk (NaN compares False against everything) — a HITL gate this
        # module's own docstring calls the security-critical default-deny
        # mechanism would silently ALLOW instead of REQUIRE_HITL. Reproduced
        # directly. Guard at construction, the one place every ToolCall
        # passes through regardless of who built it.
        if not math.isfinite(self.risk):
            raise ValueError(f"ToolCall.risk must be finite (no inf/-inf/nan) — got {self.risk!r}")
        if not (0.0 <= self.risk <= 1.0):
            raise ValueError(f"ToolCall.risk must be within 0..1 — got {self.risk!r}")


class Tool(ABC):
    name: str
    required_scopes: tuple[str, ...] = ()

    @abstractmethod
    def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute the tool. Callers MUST have obtained `PolicyDecision.ALLOW`
        (or explicit HITL approval) from a `PolicyEngine` first — this method
        does not itself enforce policy."""
        raise NotImplementedError


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_HITL = "require_hitl"


@dataclass(frozen=True)
class PolicyResult:
    decision: PolicyDecision
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision is PolicyDecision.ALLOW


@dataclass(frozen=True)
class PolicyConfig:
    allowed_scopes: frozenset[str] = frozenset()
    forbidden_tool_names: frozenset[str] = frozenset()
    # Any call with risk > this threshold is routed to REQUIRE_HITL instead
    # of ALLOW, even if scopes check out. Default is effectively "never
    # auto-allow high risk": callers must configure this explicitly per
    # constraint #6 ("HITL approval above a configurable risk threshold").
    require_hitl_above_risk: float = 0.0

    def __post_init__(self) -> None:
        # Same bypass as ToolCall.risk (see its __post_init__): `risk >
        # threshold` is False for any risk when threshold is NaN, silently
        # disabling the HITL gate for every call regardless of risk.
        if not math.isfinite(self.require_hitl_above_risk):
            raise ValueError(
                f"PolicyConfig.require_hitl_above_risk must be finite (no inf/-inf/nan) — "
                f"got {self.require_hitl_above_risk!r}"
            )
        # ToolCall.risk is bounded 0..1, so a threshold >= 1.0 makes
        # `call.risk > threshold` unreachable — the max-risk call (1.0) would
        # auto-ALLOW instead of routing to REQUIRE_HITL, silently disabling
        # the gate for exactly the most dangerous call. The threshold must
        # stay in [0.0, 1.0) so the gate is always reachable (docs/adr/0035).
        # To hard-deny rather than gate, use forbidden_tool_names / scopes.
        if not (0.0 <= self.require_hitl_above_risk < 1.0):
            raise ValueError(
                f"PolicyConfig.require_hitl_above_risk must be within [0.0, 1.0) so the HITL "
                f"gate stays reachable (risk is capped at 1.0) — got "
                f"{self.require_hitl_above_risk!r}"
            )


@dataclass(frozen=True)
class AuditEntry:
    tool: str
    call: ToolCall
    result: PolicyResult
    ts: datetime


class AuditLogWriter(ABC):
    @abstractmethod
    def write(self, entry: AuditEntry) -> None:
        raise NotImplementedError


class InMemoryAuditLogWriter(AuditLogWriter):
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def write(self, entry: AuditEntry) -> None:
        # Frozen dataclasses do not make nested argument values immutable.
        # Store a point-in-time audit record, not an alias that the caller can
        # rewrite after policy evaluation.
        self.entries.append(deepcopy(entry))


class FileAuditLogWriter(AuditLogWriter):
    """Append-only JSONL. The audit trail that survives the process.

    `InMemoryAuditLogWriter` was the only implementation shipped, so every
    audit entry died with the interpreter that wrote it — and `roadmap.md`
    listed `AuditLogWriter` under Phase 1 DONE. The interface was done; an
    audit trail you cannot read after the fact is not one (ADR 0083).

    **Argument VALUES are redacted by default.** A tool call's arguments are
    where an API key, a bearer token or a customer record lives, and an audit
    log is exactly the file that gets shipped to a log aggregator, attached to
    a ticket, or read by someone debugging. The names are what makes the entry
    useful — *which* tool, *which* parameters, what the engine decided — and
    the values are what makes it dangerous. Pass `redact_arguments=False` only
    if you have decided the destination is as trusted as the arguments are
    sensitive.
    """

    REDACTED = "<redacted>"

    def __init__(self, path: Path, *, redact_arguments: bool = True) -> None:
        self.path = Path(path)
        self._redact = redact_arguments

    def write(self, entry: AuditEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self._payload(entry), sort_keys=True) + "\n")

    def _payload(self, entry: AuditEntry) -> dict[str, Any]:
        return {
            "ts": entry.ts.isoformat(),
            "tool": entry.tool,
            "decision": entry.result.decision.value,
            "reason": entry.result.reason,
            "call": {
                "tool_name": entry.call.tool_name,
                "risk": entry.call.risk,
                "arguments": self._arguments(entry.call.arguments),
            },
        }

    def _arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._redact:
            return {key: self.REDACTED for key in sorted(arguments)}
        return {key: _jsonable(arguments[key]) for key in sorted(arguments)}

    def read(self) -> list[dict[str, Any]]:
        """Entries as written. Malformed lines raise rather than being
        skipped — an audit log that silently drops records is worse than one
        that admits it is damaged."""
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for number, line in enumerate(self.path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{self.path}:{number}: malformed audit entry: {exc}") from exc
        return out


def _jsonable(value: Any) -> Any:
    """Never let an unserialisable argument lose the whole entry."""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)
    return value


class PolicyEngine:
    """Evaluates every tool call before execution. Deny-by-default: a tool
    must declare scopes, every declared scope must be explicitly allowlisted,
    and the call's risk must not exceed the configured HITL threshold."""

    def __init__(
        self,
        config: PolicyConfig | None = None,
        audit_log: AuditLogWriter | None = None,
        clock: Any = None,
    ) -> None:
        self._config = config if config is not None else PolicyConfig()
        self._audit_log = audit_log if audit_log is not None else InMemoryAuditLogWriter()
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))

    @property
    def audit_log(self) -> AuditLogWriter:
        """The writer every evaluation is recorded to.

        Public because it was not: constructed without an explicit writer, the
        default landed on a private attribute with no accessor, so entries
        were written and then unreachable (ADR 0083)."""
        return self._audit_log

    def evaluate(self, tool: Tool, call: ToolCall) -> PolicyResult:
        result = self._decide(tool, call)
        self._audit_log.write(
            AuditEntry(tool=tool.name, call=call, result=result, ts=self._clock())
        )
        return result

    def _decide(self, tool: Tool, call: ToolCall) -> PolicyResult:
        if call.tool_name != tool.name:
            # ToolCall.tool_name is the caller's declared intent (e.g. what
            # an LLM tool-call request named); `tool` is the actual object
            # about to be invoked. Nothing upstream guarantees these match —
            # a lookup bug, or a successful prompt injection steering which
            # Tool gets resolved, could silently evaluate policy against a
            # different tool than the one that runs. Given this module's own
            # threat model (contain injection, don't just detect it), that
            # mismatch must deny, not pass through unnoticed.
            return PolicyResult(
                PolicyDecision.DENY,
                f"tool_name mismatch: call declares {call.tool_name!r} but is being "
                f"evaluated against tool {tool.name!r}",
            )
        if tool.name in self._config.forbidden_tool_names:
            return PolicyResult(PolicyDecision.DENY, f"tool {tool.name!r} is explicitly forbidden")
        if not tool.required_scopes:
            return PolicyResult(PolicyDecision.DENY, "tool declares no scopes; default-deny")
        missing = set(tool.required_scopes) - self._config.allowed_scopes
        if missing:
            return PolicyResult(PolicyDecision.DENY, f"missing scopes: {sorted(missing)}")
        if call.risk > self._config.require_hitl_above_risk:
            threshold = self._config.require_hitl_above_risk
            return PolicyResult(
                PolicyDecision.REQUIRE_HITL,
                f"risk {call.risk} exceeds require_hitl_above_risk={threshold}",
            )
        return PolicyResult(PolicyDecision.ALLOW, "ok")


__all__ = [
    "AuditEntry",
    "AuditLogWriter",
    "InMemoryAuditLogWriter",
    "PolicyConfig",
    "PolicyDecision",
    "PolicyEngine",
    "PolicyResult",
    "Tool",
    "ToolCall",
]
