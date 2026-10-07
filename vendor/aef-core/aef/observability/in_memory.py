"""Dependency-free `Tracer` used in tests, examples, and as the DI default
when no real OTel exporter is configured."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from aef.observability.base import Span, Tracer


@dataclass
class RecordedSpan:
    name: str
    attributes: dict[str, Any]
    exceptions: list[BaseException] = field(default_factory=list)
    ended: bool = False


class InMemorySpan(Span):
    def __init__(self, recorded: RecordedSpan) -> None:
        self._recorded = recorded

    def set_attribute(self, key: str, value: Any) -> None:
        self._recorded.attributes[key] = deepcopy(value)

    def record_exception(self, exc: BaseException) -> None:
        self._recorded.exceptions.append(exc)

    def end(self) -> None:
        self._recorded.ended = True


class InMemoryTracer(Tracer):
    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []

    def start_span(self, name: str, attributes: dict[str, Any] | None = None) -> Span:
        recorded = RecordedSpan(name=name, attributes=deepcopy(attributes or {}))
        self.spans.append(recorded)
        return InMemorySpan(recorded)
