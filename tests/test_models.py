"""Tests for models, config, and the session registry."""

from __future__ import annotations

import pytest

from raitbox import Execution, Logs, Result
from raitbox.core.settings import resolve_backend
from raitbox.core.types import CommandResult


def test_execution_text_returns_main_result():
    ex = Execution(results=[Result(text="hi", is_main_result=True)])
    assert ex.text == "hi"


def test_execution_text_none_when_no_main():
    ex = Execution(results=[Result(text="x", is_main_result=False)])
    assert ex.text is None


def test_logs_default_empty():
    assert Logs().stdout == [] and Logs().stderr == []


def test_command_result_fields():
    r = CommandResult(stdout="o", stderr="e", exit_code=1)
    assert (r.stdout, r.stderr, r.exit_code, r.error) == ("o", "e", 1, None)


@pytest.mark.parametrize(
    "given,expected",
    [
        (None, "agentcore-ci"),
        ("agentcore", "agentcore-ci"),
        ("code-interpreter", "agentcore-ci"),
        ("agentcore-runtime", "agentcore-runtime"),
        ("runtime", "agentcore-runtime"),
        ("microvm", "microvm"),
        ("lambda-microvm", "microvm"),
    ],
)
def test_resolve_backend_aliases(given, expected, monkeypatch):
    monkeypatch.delenv("RAITBOX_BACKEND", raising=False)
    assert resolve_backend(given) == expected


def test_resolve_backend_env_override(monkeypatch):
    monkeypatch.setenv("RAITBOX_BACKEND", "microvm")
    assert resolve_backend(None) == "microvm"


def test_resolve_backend_invalid():
    with pytest.raises(ValueError):
        resolve_backend("nope")


def test_registry_max_sessions(monkeypatch):
    monkeypatch.setenv("RAITBOX_MAX_SESSIONS", "1")
    from raitbox.core import session_pool as registry

    registry._live.clear()
    registry.register("s1", lambda: None)
    with pytest.raises(RuntimeError):
        registry.register("s2", lambda: None)
    registry.unregister("s1")
    registry.register("s2", lambda: None)
    registry.unregister("s2")
