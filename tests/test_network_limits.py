"""Network limits: egress is deny-by-default; internet_access is an explicit guard."""

from __future__ import annotations

from typing import Any

import pytest

from raitbox import InvalidArgumentException, Sandbox
from raitbox.backends import microvm as microvm_mod

IMAGE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:raitbox"


class FakeMV:
    def __init__(self) -> None:
        self.run_params: dict[str, Any] = {}

    def run_microvm(self, **kw: Any):
        self.run_params = kw
        return {"microvmId": "mv-1", "endpoint": "mv-1.on.aws", "state": "PENDING"}

    def get_microvm(self, **kw: Any):
        return {"microvmId": kw["microvmIdentifier"], "state": "RUNNING", "endpoint": "mv-1.on.aws"}

    def create_microvm_auth_token(self, **kw: Any):
        return {"authToken": {"X-aws-proxy-auth": "tok"}}

    def terminate_microvm(self, **kw: Any): ...


def _make(monkeypatch, **open_kwargs):
    monkeypatch.setattr(
        microvm_mod,
        "MicroVMHttpClient",
        lambda *a, **k: type("H", (), {"get": lambda s, p, **k: None})(),
    )
    client = FakeMV()
    Sandbox(
        backend="microvm",
        template=IMAGE,
        region_name="us-west-2",
        client=client,
        **open_kwargs,
    )
    return client


def test_default_has_no_egress_connectors(monkeypatch):
    client = _make(monkeypatch)
    assert "egressNetworkConnectors" not in client.run_params  # deny-by-default


def test_internet_access_false_forces_empty_egress(monkeypatch):
    # Even if a connector was supplied, internet_access=False wins (locks it down).
    client = _make(monkeypatch, internet_access=False, egress_connectors=["egr-1"])
    assert "egressNetworkConnectors" not in client.run_params


def test_internet_access_true_requires_connector(monkeypatch):
    monkeypatch.setattr(
        microvm_mod,
        "MicroVMHttpClient",
        lambda *a, **k: type("H", (), {"get": lambda s, p, **k: None})(),
    )
    with pytest.raises(InvalidArgumentException, match="egress_connectors"):
        Sandbox(
            backend="microvm",
            template=IMAGE,
            region_name="us-west-2",
            client=FakeMV(),
            internet_access=True,
        )


def test_internet_access_true_with_connector_passes_through(monkeypatch):
    client = _make(monkeypatch, internet_access=True, egress_connectors=["egr-1"])
    assert client.run_params["egressNetworkConnectors"] == ["egr-1"]
