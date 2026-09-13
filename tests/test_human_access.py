"""Human web-terminal access: launches a terminal server + returns a preview link."""

from __future__ import annotations

from typing import Any

import pytest

from raitbox import NotSupportedError, PreviewLink, Sandbox
from raitbox.backends import microvm as microvm_mod
from tests.conftest import FakeCIClient

IMAGE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:raitbox"


class FakeMV:
    def run_microvm(self, **kw: Any):
        return {"microvmId": "mv-1", "endpoint": "mv-1.on.aws", "state": "PENDING"}

    def get_microvm(self, **kw: Any):
        return {"microvmId": kw["microvmIdentifier"], "state": "RUNNING", "endpoint": "mv-1.on.aws"}

    def create_microvm_auth_token(self, **kw: Any):
        return {"authToken": {"X-aws-proxy-auth": "tok"}}

    def terminate_microvm(self, **kw: Any): ...


def _microvm_sandbox(monkeypatch):
    started: list[str] = []

    class FakeHttp:
        def post(self, path: str, json: dict | None = None, **kw: Any):
            if path == "/commands/start":
                started.append((json or {}).get("cmd", ""))
                return type("R", (), {"json": lambda self: {"pid": 5}})()
            return type("R", (), {"json": lambda self: {}})()

        def get(self, *a: Any, **k: Any):
            return None

    monkeypatch.setattr(microvm_mod, "MicroVMHttpClient", lambda *a, **k: FakeHttp())
    sbx = Sandbox(backend="microvm", template=IMAGE, region_name="us-west-2", client=FakeMV())
    return sbx, started


def test_terminal_link_launches_server_and_returns_preview(monkeypatch):
    sbx, started = _microvm_sandbox(monkeypatch)
    link = sbx.get_terminal_link(7681)
    assert isinstance(link, PreviewLink) and link.port == 7681
    assert link.headers["X-aws-proxy-port"] == "7681"
    # The web-terminal server was launched in the background with {port} filled in.
    assert started == ["ttyd -p 7681 -W bash"]


def test_terminal_link_skips_launch_when_command_none(monkeypatch):
    sbx, started = _microvm_sandbox(monkeypatch)
    link = sbx.get_terminal_link(9000, command=None)
    assert link.port == 9000
    assert started == []  # nothing launched; server assumed already running


def test_terminal_link_not_supported_on_ci():
    sbx = Sandbox(backend="agentcore-ci", client=FakeCIClient())
    with pytest.raises(NotSupportedError):
        sbx.get_terminal_link(7681, command=None)
