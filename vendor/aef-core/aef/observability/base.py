"""`Tracer`/`Span` — the interface the kernel emits telemetry through.

The kernel never imports OpenTelemetry directly (constraint #3 keeps even
observability's own vendor SDK out of `kernel/`); it calls `Tracer` here.
`otel_tracer.py` (this package) adapts to the real OTel SDK. `InMemoryTracer`
is a real, dependency-free backend used in tests and examples.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class Span(ABC):
    @abstractmethod
    def set_attribute(self, key: str, value: Any) -> None:
        raise NotImplementedError

    @abstractmethod
    def record_exception(self, exc: BaseException) -> None:
        raise NotImplementedError

    @abstractmethod
    def end(self) -> None:
        raise NotImplementedError


class Tracer(ABC):
    @abstractmethod
    def start_span(self, name: str, attributes: dict[str, Any] | None = None) -> Span:
        raise NotImplementedError

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[Span]:
        s = self.start_span(name, attributes)
        try:
            yield s
        except BaseException as exc:
            s.record_exception(exc)
            raise
        finally:
            s.end()
