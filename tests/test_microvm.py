"""Lambda MicroVM backend tests (fake boto3 client + in-memory HTTP)."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from raitbox import Sandbox
from raitbox.backends import microvm as microvm_mod
from raitbox.backends.microvm import MicroVMBackend

IMAGE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:raitbox"


class FakeMicroVMClient:
    def __init__(self) -> None:
        self.terminated: list[str] = []
        self.tokens_minted = 0

    def run_microvm(self, **kw: Any):
        self.last_run = kw
        return {
            "microvmId": "mv-abc",
            "endpoint": "mv-abc.lambda-microvm.us-east-1.on.aws",
            "state": "PENDING",
        }

    def get_microvm(self, **kw: Any):
        return {
            "microvmId": kw["microvmIdentifier"],
            "state": "RUNNING",
            "endpoint": "mv-abc.lambda-microvm.us-east-1.on.aws",
            "imageIdentifier": IMAGE_ARN,
        }

    def create_microvm_auth_token(self, **kw: Any):
        self.tokens_minted += 1
        return {"authToken": {"X-aws-proxy-auth": "tok-123"}}

    def terminate_microvm(self, **kw: Any):
        self.terminated.append(kw["microvmIdentifier"])
        return {}


class _Resp:
    def __init__(self, data: dict) -> None:
        self._data = data
        self.status_code = 200

    def json(self) -> dict:
        return self._data


class FakeHttp:
    """In-memory stand-in for MicroVMHttpClient backed by a dict filesystem."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        self.fs: dict[str, bytes] = {}

    def get(self, path: str, **kw: Any) -> _Resp:
        return _Resp({"ok": True})

    def post(self, path: str, json: dict | None = None, **kw: Any) -> _Resp:
        json = json or {}
        if path == "/run_code":
            return _Resp({"stdout": "42\n", "stderr": "", "results": ["42"], "error": None})
        if path == "/run_command":
            return _Resp({"stdout": "hi\n", "stderr": "", "exit_code": 0})
        if path == "/files/write":
            data = base64.b64decode(json["b64"]) if "b64" in json else json.get("text", "").encode()
            self.fs[json["path"]] = data
            return _Resp({"ok": True})
        if path == "/files/read":
            if json["path"] not in self.fs:
                return _Resp({"not_found": True})
            raw = self.fs[json["path"]]
            if json.get("format") == "bytes":
                return _Resp({"b64": base64.b64encode(raw).decode()})
            return _Resp({"text": raw.decode()})
        if path == "/files/list":
            entries = [{"name": p.rsplit("/", 1)[-1], "type": "file", "path": p} for p in self.fs]
            return _Resp({"entries": entries})
        if path == "/files/remove":
            self.fs.pop(json["path"], None)
            return _Resp({"ok": True})
        return _Resp({"ok": True})


@pytest.fixture
def patched_http(monkeypatch):
    monkeypatch.setattr(microvm_mod, "MicroVMHttpClient", FakeHttp)


def make_sandbox():
    client = FakeMicroVMClient()
    backend = MicroVMBackend.open(
        template=IMAGE_ARN,
        timeout=600,
        envs={"FOO": "bar"},
        metadata=None,
        region_name=None,
        profile_name=None,
        client=client,
    )
    return Sandbox(_backend=backend), client


def test_open_launches_and_polls(patched_http):
    sbx, client = make_sandbox()
    assert sbx.sandbox_id == "mv-abc"
    assert client.last_run["imageIdentifier"] == IMAGE_ARN
    assert client.last_run["idlePolicy"]["autoResumeEnabled"] is True


def test_requires_template():
    from raitbox.core.errors import TemplateException

    with pytest.raises(TemplateException):
        MicroVMBackend.open(
            template=None,
            timeout=300,
            envs=None,
            metadata=None,
            region_name=None,
            profile_name=None,
            client=FakeMicroVMClient(),
        )


def test_run_code(patched_http):
    sbx, _ = make_sandbox()
    execution = sbx.run_code("print(6*7)")
    assert execution.error is None
    assert execution.logs.stdout == ["42\n"]


def test_run_command(patched_http):
    sbx, _ = make_sandbox()
    assert sbx.commands.run("echo hi").stdout == "hi\n"


def test_files_roundtrip(patched_http):
    sbx, _ = make_sandbox()
    sbx.files.write("/work/a.txt", "content")
    assert sbx.files.read("/work/a.txt") == "content"
    sbx.files.write("/work/d.bin", b"\x00\x01")
    assert sbx.files.read("/work/d.bin", format="bytes") == bytearray(b"\x00\x01")
    sbx.files.remove("/work/a.txt")
    assert not sbx.files.exists("/work/a.txt")


def test_get_host_supported(patched_http):
    sbx, _ = make_sandbox()
    assert sbx.get_host(8080) == "mv-abc.lambda-microvm.us-east-1.on.aws"


def test_kill_terminates(patched_http):
    sbx, client = make_sandbox()
    sbx.kill()
    assert client.terminated == ["mv-abc"]
