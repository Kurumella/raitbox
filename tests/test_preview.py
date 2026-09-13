"""Preview links (microvm) — port-scoped, expiring token + URL."""

from __future__ import annotations

from typing import Any

import pytest

from raitbox import NotSupportedError, PreviewLink, Sandbox
from raitbox.backends.agentcore_ci import AgentCoreCodeInterpreterBackend
from raitbox.backends.microvm import MicroVMBackend
from tests.conftest import FakeCIClient

IMAGE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:raitbox"


class FakeMV:
    def __init__(self) -> None:
        self.token_calls: list[dict] = []

    def run_microvm(self, **kw: Any):
        return {"microvmId": "mv-1", "endpoint": "mv-1.on.aws", "state": "PENDING"}

    def get_microvm(self, **kw: Any):
        return {"microvmId": kw["microvmIdentifier"], "state": "RUNNING", "endpoint": "mv-1.on.aws"}

    def create_microvm_auth_token(self, **kw: Any):
        self.token_calls.append(kw)
        return {"authToken": {"X-aws-proxy-auth": "tok-xyz"}}

    def terminate_microvm(self, **kw: Any): ...


def _mv_sandbox(monkeypatch):
    from raitbox.backends import microvm as m

    monkeypatch.setattr(
        m, "MicroVMHttpClient", lambda *a, **k: type("H", (), {"get": lambda s, p, **k: None})()
    )
    b = MicroVMBackend.open(
        template=IMAGE,
        timeout=600,
        envs=None,
        metadata=None,
        region_name=None,
        profile_name=None,
        client=FakeMV(),
    )
    return Sandbox(_backend=b), b._client


def test_get_preview_link(monkeypatch):
    sbx, client = _mv_sandbox(monkeypatch)
    link = sbx.get_preview_link(8080, expires_in_minutes=15)
    assert isinstance(link, PreviewLink)
    assert link.url == "https://mv-1.on.aws"
    assert link.token == "tok-xyz"
    assert link.headers == {"X-aws-proxy-auth": "tok-xyz", "X-aws-proxy-port": "8080"}
    # token was minted scoped to that single port with the requested TTL
    call = client.token_calls[-1]
    assert call["allowedPorts"] == [{"port": 8080}]
    assert call["expirationInMinutes"] == 15


def test_preview_not_supported_on_ci():
    b = AgentCoreCodeInterpreterBackend.open(
        template=None,
        timeout=300,
        envs=None,
        metadata=None,
        region_name=None,
        profile_name=None,
        client=FakeCIClient(),
    )
    with pytest.raises(NotSupportedError):
        Sandbox(_backend=b).get_preview_link(8080)
