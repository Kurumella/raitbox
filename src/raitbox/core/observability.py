"""Optional OpenTelemetry tracing for sandbox operations.

Tracing is **off by default** and adds no dependency: :func:`span` is a no-op
until you call :func:`configure` (or set ``RAITBOX_TRACING=1`` with
``opentelemetry-api`` installed). Once enabled, key ``Sandbox`` operations
(create, run_code, run_command, kill) emit spans tagged with the backend and
sandbox id, so existing code is instrumented without changes.

    from raitbox.core.observability import configure
    configure(service_name="my-agent")   # uses the global OTel tracer provider
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from typing import Any

# Module state in a dict so helpers mutate it without ``global`` reassignment.
_STATE: dict[str, Any] = {"enabled": False, "tracer": None}


def configure(*, service_name: str = "raitbox", tracer: Any = None, enabled: bool = True) -> bool:
    """Enable tracing. Returns whether it is active (needs OTel unless ``tracer`` given).

    Pass ``tracer`` to inject a specific tracer (or a fake in tests); otherwise
    the global OpenTelemetry tracer provider is used if the package is present.
    """
    if tracer is not None:
        _STATE["tracer"] = tracer
        _STATE["enabled"] = enabled
        return enabled
    try:
        from opentelemetry import trace
    except ImportError:
        _STATE["enabled"] = False
        return False
    _STATE["tracer"] = trace.get_tracer(service_name)
    _STATE["enabled"] = enabled
    return enabled


def _maybe_autoconfigure() -> None:
    if not _STATE["enabled"] and os.environ.get("RAITBOX_TRACING") in ("1", "true", "TRUE"):
        configure()


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Start a span for ``name`` if tracing is enabled; otherwise a no-op."""
    _maybe_autoconfigure()
    tracer = _STATE["tracer"]
    if not _STATE["enabled"] or tracer is None:
        yield None
        return
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                with contextlib.suppress(Exception):
                    current.set_attribute(key, value)
        yield current


def is_enabled() -> bool:
    return bool(_STATE["enabled"])


def reset() -> None:
    """Disable tracing and drop the tracer (mainly for tests)."""
    _STATE["enabled"] = False
    _STATE["tracer"] = None
