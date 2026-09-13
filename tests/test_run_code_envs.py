"""`run_code(envs=...)` is honored on every backend (previously silently dropped)."""

from __future__ import annotations

from typing import Any

import pytest

from raitbox import Sandbox
from raitbox.backends import microvm as microvm_mod
from tests.conftest import FakeCIClient

IMAGE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:raitbox"


def test_agentcore_ci_run_code_seeds_env_preamble():
    client = FakeCIClient()
    sbx = Sandbox(backend="agentcore-ci", client=client)
    sbx.run_code("print('x')", envs={"API_KEY": "xyz"})
    code = client.code_args[-1]["code"]
    assert "_os.environ.update(" in code and "'API_KEY': 'xyz'" in code


def test_agentcore_runtime_run_code_env_reaches_python():
    from tests.test_agentcore_runtime import make_sandbox

    sbx, _ = make_sandbox()
    ex = sbx.run_code("import os; print(os.environ['API_KEY'])", envs={"API_KEY": "runtime-secret"})
    assert "runtime-secret" in "".join(ex.logs.stdout)


def test_microvm_backend_sends_envs_in_payload():
    captured = {}

    class FakeHttp:
        def post(self, path: str, json: dict | None = None, **kw: Any):
            captured["path"], captured["json"] = path, json or {}
            body = {"stdout": "", "stderr": "", "results": [], "error": None}
            return type("R", (), {"json": lambda self: body})()

        def get(self, *a: Any, **k: Any):
            return None

    b = microvm_mod.MicroVMBackend(
        client=object(),
        microvm_id="mv-1",
        endpoint="mv-1.on.aws",
        template=IMAGE,
        http=FakeHttp(),
        timeout=300,
    )
    b.run_code("print(1)", envs={"API_KEY": "v"})
    assert captured["path"] == "/run_code"
    assert captured["json"]["envs"] == {"API_KEY": "v"}


def test_microvm_agent_server_run_code_applies_env():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from raitbox.agent_server.server import app

    client = TestClient(app)
    # python path: code reads the injected env var.
    r = client.post(
        "/run_code",
        json={"code": "import os; print(os.environ['MYVAR'])", "envs": {"MYVAR": "hello-env"}},
    ).json()
    assert "hello-env" in r["stdout"] and r["error"] is None

    # non-python path: env passed to the interpreter subprocess.
    r2 = client.post(
        "/run_code",
        json={
            "code": "console.log(process.env.MYVAR)",
            "language": "javascript",
            "envs": {"MYVAR": "js-env"},
        },
    ).json()
    # node may be absent in the test env; only assert when it ran.
    if r2.get("error") is None and r2.get("stdout"):
        assert "js-env" in r2["stdout"]
