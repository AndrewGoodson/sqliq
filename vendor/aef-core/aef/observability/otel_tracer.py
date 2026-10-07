"""Real `Tracer` adapter backed by the OpenTelemetry SDK (report §17). This
module (and no other outside `providers/`/`services/*/adapters/`) is allowed
to import `opentelemetry` directly (constraint #3) — `kernel/` only ever
sees the vendor-neutral `Tracer` interface.

AEF does not configure a `TracerProvider` or exporter here: which exporter
to use (console, OTLP, Jaeger, ...) is a deployment decision. Callers build
their own `TracerProvider`, get an `opentelemetry.trace.Tracer` from it, and
hand it to `OtelTracer`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry.trace import Span as OtelSpanAPI
from opentelemetry.trace import Tracer as OtelTracerAPI
from opentelemetry.trace import get_tracer, set_span_in_context

from aef.observability.base import Span, Tracer


class OtelSpan(Span):
    def __init__(self, span: OtelSpanAPI) -> None:
        self._span = span

    def set_attribute(self, key: str, value: Any) -> None:
        self._span.set_attribute(key, value)

    def record_exception(self, exc: BaseException) -> None:
        # The real OTel SDK's Span.record_exception accepts BaseException
        # directly (confirmed via inspect.signature, not assumed) — no
        # Exception-only restriction to mirror here. The previous
        # isinstance(exc, Exception) guard silently dropped recording for
        # KeyboardInterrupt/SystemExit/etc., inconsistent with both the
        # real API and InMemorySpan (which has no such filter).
        self._span.record_exception(exc)

    def end(self) -> None:
        self._span.end()

    @property
    def raw(self) -> OtelSpanAPI:
        return self._span


class OtelTracer(Tracer):
    def __init__(
        self, otel_tracer: OtelTracerAPI | None = None, *, instrumentation_name: str = "aef"
    ) -> None:
        # `is not None`, not truthiness: an injected tracer whose type defines
        # __bool__/__len__ falsey would otherwise be silently discarded and spans
        # would go to the global tracer instead of the caller's. Same class as the
        # PolicyEngine/AnthropicProvider/InMemoryMemoryStore dependencies.
        self._tracer = otel_tracer if otel_tracer is not None else get_tracer(instrumentation_name)

    def start_span(self, name: str, attributes: dict[str, Any] | None = None) -> Span:
        span = self._tracer.start_span(name, attributes=attributes)
        return OtelSpan(span)

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[Span]:
        # `Tracer.span()`'s default implementation (aef/observability/base.py)
        # only calls `start_span()`, which the real OTel SDK never makes the
        # "current" span in its context — nested `tracer.span()` calls (e.g.
        # GraphExecutor's per-node span wrapping a nested emergent-routing
        # span) would export as unrelated root spans with no parent/child
        # relationship at all, silently discarding the nesting the code
        # visually has. Confirmed directly with a real TracerProvider +
        # InMemorySpanExporter before this fix: a span started inside
        # another's `with` block had `child.parent is None`. Explicitly
        # attaching/detaching the OTel context around the yield (the same
        # thing `start_as_current_span` does internally) is what
        # `start_span()` alone does not do. See docs/adr/0027.
        s = self.start_span(name, attributes)
        assert isinstance(s, OtelSpan)
        token = otel_context.attach(set_span_in_context(s.raw))
        try:
            yield s
        except BaseException as exc:
            s.record_exception(exc)
            raise
        finally:
            otel_context.detach(token)
            s.end()
