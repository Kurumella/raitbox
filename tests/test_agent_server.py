"""Tests for the in-VM agent server (FastAPI TestClient, no AWS/VM)."""

from __future__ import annotations

import base64

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from raitbox.agent_server.server import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_run_code_captures_stdout():
    r = client.post("/run_code", json={"code": "print(6*7)"})
    body = r.json()
    assert body["error"] is None
    assert body["stdout"].strip() == "42"


def test_run_code_stateful_kernel():
    client.post("/run_code", json={"code": "x = 10"})
    r = client.post("/run_code", json={"code": "print(x + 5)"})
    assert r.json()["stdout"].strip() == "15"


def test_run_code_error():
    r = client.post("/run_code", json={"code": "1/0"})
    body = r.json()
    assert body["error"]["name"] == "ZeroDivisionError"


def test_run_command():
    r = client.post("/run_command", json={"cmd": "echo hi"})
    body = r.json()
    assert body["exit_code"] == 0
    assert body["stdout"].strip() == "hi"


def test_run_command_nonzero():
    r = client.post("/run_command", json={"cmd": "exit 5"})
    assert r.json()["exit_code"] == 5


def test_files_text_roundtrip(tmp_path):
    p = str(tmp_path / "a.txt")
    assert client.post("/files/write", json={"path": p, "text": "hello"}).status_code == 200
    assert client.post("/files/read", json={"path": p}).json()["text"] == "hello"


def test_files_binary_roundtrip(tmp_path):
    p = str(tmp_path / "b.bin")
    b64 = base64.b64encode(b"\x00\xff").decode()
    client.post("/files/write", json={"path": p, "b64": b64})
    got = client.post("/files/read", json={"path": p, "format": "bytes"}).json()["b64"]
    assert base64.b64decode(got) == b"\x00\xff"


def test_files_list_and_remove(tmp_path):
    client.post("/files/write", json={"path": str(tmp_path / "x.txt"), "text": "1"})
    entries = client.post("/files/list", json={"path": str(tmp_path)}).json()["entries"]
    assert any(e["name"] == "x.txt" for e in entries)
    client.post("/files/remove", json={"path": str(tmp_path / "x.txt")})
    entries = client.post("/files/list", json={"path": str(tmp_path)}).json()["entries"]
    assert not any(e["name"] == "x.txt" for e in entries)


def test_read_missing_file():
    assert client.post("/files/read", json={"path": "/no/such/file"}).json()["not_found"] is True


def test_run_hook_generates_entropy():
    r = client.post(
        "/aws/lambda-microvms/runtime/v1/run",
        json={"microvmId": "mv-123", "runHookPayload": '{"envs": {"K": "V"}}'},
    )
    assert r.json()["status"] == "running"
    import os

    assert os.environ.get("K") == "V"
    assert client.get("/health").json()["microvm_id"] == "mv-123"
