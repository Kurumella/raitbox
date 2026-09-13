"""OpenTelemetry tracing: no-op by default, records spans once configured."""

from __future__ import annotations

from typing import Any

import pytest

from raitbox import Sandbox
from raitbox.core import observability as obs
from tests.conftest import FakeCIClient


class FakeSpan:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeTracer:
    def __init__(self) -> None:
        self.spans: list[FakeSpan] = []

    def start_as_current_span(self, name: str) -> FakeSpan:
        s = FakeSpan(name)
        self.spans.append(s)
        return s


@pytest.fixture(autouse=True)
def _reset_tracing():
    obs.reset()
    yield
    obs.reset()


def test_span_is_noop_by_default():
    assert obs.is_enabled() is False
    with obs.span("x", **{"a": 1}) as s:
        assert s is None  # nothing recorded, no error


def test_configure_with_injected_tracer_records():
    tracer = FakeTracer()
    assert obs.configure(tracer=tracer) is True
    with obs.span("op", **{"raitbox.backend": "ci", "skip": None}) as s:
        assert s is tracer.spans[0]
    assert tracer.spans[0].name == "op"
    # None-valued attributes are dropped.
    assert tracer.spans[0].attributes == {"raitbox.backend": "ci"}


def test_run_code_emits_span_when_enabled():
    tracer = FakeTracer()
    obs.configure(tracer=tracer)
    sbx = Sandbox(backend="agentcore-ci", client=FakeCIClient())
    sbx.run_code("print(1)")
    names = [s.name for s in tracer.spans]
    assert "raitbox.sandbox.create" in names
    assert "raitbox.sandbox.run_code" in names
    run = next(s for s in tracer.spans if s.name == "raitbox.sandbox.run_code")
    assert run.attributes["raitbox.backend"] == "agentcore-ci"


def test_env_autoconfigure(monkeypatch):
    monkeypatch.setenv("RAITBOX_TRACING", "1")
    tracer = FakeTracer()

    def _fake_configure(**_):
        obs._STATE["enabled"] = True
        obs._STATE["tracer"] = tracer
        return True

    # Simulate OTel being present: the env flag triggers configure() on first span.
    monkeypatch.setattr(obs, "configure", _fake_configure)
    with obs.span("y"):
        pass
    assert tracer.spans and tracer.spans[0].name == "y"
