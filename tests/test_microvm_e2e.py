"""End-to-end MicroVM backend test: the backend drives a REAL agent server.

The HTTP transport is swapped for a FastAPI TestClient bound to
``raitbox.agent_server.server.app``, so run_code / run_command / files /
background commands all execute through the full stack the ``microvm`` backend
would use against a live VM — only the boto3 control calls are faked.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from raitbox import Sandbox
from raitbox.agent_server.server import app
from raitbox.backends import microvm as microvm_mod
from raitbox.backends.microvm import MicroVMBackend

IMAGE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:raitbox"


class FakeMicroVMClient:
    def __init__(self) -> None:
        self.terminated: list[str] = []

    def run_microvm(self, **kw: Any):
        return {"microvmId": "mv-e2e", "endpoint": "mv-e2e.on.aws", "state": "PENDING"}

    def get_microvm(self, **kw: Any):
        return {
            "microvmId": kw["microvmIdentifier"],
            "state": "RUNNING",
            "endpoint": "mv-e2e.on.aws",
        }

    def create_microvm_auth_token(self, **kw: Any):
        return {"authToken": {"X-aws-proxy-auth": "tok"}}

    def terminate_microvm(self, **kw: Any):
        self.terminated.append(kw["microvmIdentifier"])


class AgentServerHttp:
    """MicroVMHttpClient stand-in that proxies to the in-process agent server."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        self._c = TestClient(app)

    def get(self, path: str, **kw: Any):
        return self._c.get(path)

    def post(self, path: str, json: dict | None = None, **kw: Any):
        return self._c.post(path, json=json)


@pytest.fixture
def sandbox(monkeypatch):
    monkeypatch.setattr(microvm_mod, "MicroVMHttpClient", AgentServerHttp)
    backend = MicroVMBackend.open(
        template=IMAGE_ARN,
        timeout=600,
        envs=None,
        metadata=None,
        region_name=None,
        profile_name=None,
        client=FakeMicroVMClient(),
    )
    sbx = Sandbox(_backend=backend)
    yield sbx


def test_run_code_stateful(sandbox):
    sandbox.run_code("e2e_val = 21 * 2")
    assert sandbox.run_code("print(e2e_val)").logs.stdout == ["42\n"]


def test_run_command(sandbox):
    r = sandbox.commands.run("echo hello && echo world")
    assert r.exit_code == 0
    assert r.stdout.splitlines() == ["hello", "world"]


def test_files_roundtrip(sandbox, tmp_path):
    p = str(tmp_path / "sub" / "f.txt")
    sandbox.files.write(p, "content-here")
    assert sandbox.files.read(p) == "content-here"
    assert any(e.name == "f.txt" for e in sandbox.files.list(str(tmp_path / "sub")))


def test_background_command_full_lifecycle(sandbox, tmp_path):
    marker = str(tmp_path / "done")
    handle = sandbox.commands.run(
        f"for i in 1 2 3; do echo line$i; done; touch {marker}", background=True
    )
    assert handle.pid > 0
    result = handle.wait()
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["line1", "line2", "line3"]
    import os

    assert os.path.exists(marker)


def test_background_command_kill(sandbox):
    handle = sandbox.commands.run("sleep 30", background=True)
    listed = sandbox.commands.list()
    assert any(p.pid == handle.pid for p in listed)
    assert handle.kill() is True


def test_get_host(sandbox):
    assert sandbox.get_host(8080) == "mv-e2e.on.aws"
