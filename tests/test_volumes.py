"""Volumes: registration, mount plumbing, and backend guards."""

from __future__ import annotations

import json
from typing import Any

import pytest

from raitbox import InvalidArgumentException, NotSupportedError, Sandbox, Volume, VolumeMount
from raitbox.backends import microvm as microvm_mod
from tests.conftest import FakeCIClient

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


class FakeEFS:
    def describe_file_systems(self):
        return {"FileSystems": [{"FileSystemId": "fs-123", "Name": "shared"}]}


def test_create_volume_requires_a_resource():
    with pytest.raises(InvalidArgumentException):
        Sandbox.create_volume("v")
    vol = Sandbox.create_volume("v", file_system_id="fs-123")
    assert isinstance(vol, Volume) and vol.id == "fs-123" and vol.kind == "efs"


def test_list_volumes_with_fake_efs():
    vols = Sandbox.list_volumes(client=FakeEFS())
    assert [v.id for v in vols] == ["fs-123"]


def test_volume_mounts_rejected_on_non_microvm():
    with pytest.raises(NotSupportedError):
        Sandbox(
            backend="agentcore-ci",
            volume_mounts=[VolumeMount("fs-123", "/data")],
            client=FakeCIClient(),
        )


def test_volume_mounts_go_into_run_hook_payload(monkeypatch):
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
        volume_mounts=[VolumeMount("fs-123", "/data", subpath="team")],
    )
    payload = json.loads(client.run_params["runHookPayload"])
    assert payload["mounts"][0] == {
        "file_system_id": "fs-123",
        "mount_path": "/data",
        "subpath": "team",
        "region": "us-west-2",
    }


def test_run_hook_tolerates_mount_payload():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from raitbox.agent_server.server import app

    r = TestClient(app).post(
        "/aws/lambda-microvms/runtime/v1/run",
        json={
            "microvmId": "mv-x",
            "runHookPayload": json.dumps(
                {
                    "mounts": [
                        {"file_system_id": "fs-9", "mount_path": "/mnt/x", "region": "us-west-2"}
                    ]
                }
            ),
        },
    )
    assert r.json()["status"] == "running"  # mount is best-effort; never fails the hook
