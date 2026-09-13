"""AgentCore Runtime backend tests (no AWS)."""

from __future__ import annotations

import pytest

from raitbox import CommandExitException, InvalidArgumentException, Sandbox
from raitbox.backends.agentcore_runtime import AgentCoreRuntimeBackend

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/demo"


class FakeRuntimeClient:
    """Fake ``bedrock-agentcore`` runtime data-plane client.

    Executes the (base64-wrapped) bash command locally so file helpers work.
    """

    def __init__(self) -> None:
        self.stopped: list[str] = []
        self.sessions: list[str] = []

    def invoke_agent_runtime_command(self, **kw: Any):
        self.sessions.append(kw["runtimeSessionId"])
        body = kw["body"]
        import subprocess

        # body["command"] is: /bin/bash -c '<full>'
        proc = subprocess.run(body["command"], shell=True, capture_output=True, text=True)
        stream = [{"chunk": {"contentStart": {}}}]
        if proc.stdout:
            stream.append({"chunk": {"contentDelta": {"stdout": proc.stdout}}})
        if proc.stderr:
            stream.append({"chunk": {"contentDelta": {"stderr": proc.stderr}}})
        stream.append(
            {"chunk": {"contentStop": {"exitCode": proc.returncode, "status": "COMPLETED"}}}
        )
        return {"stream": stream}

    def stop_runtime_session(self, **kw: Any):
        self.stopped.append(kw["runtimeSessionId"])
        return {}


from typing import Any  # noqa: E402


def make_sandbox(tmp_env=None):
    client = FakeRuntimeClient()
    backend = AgentCoreRuntimeBackend.open(
        template=RUNTIME_ARN,
        timeout=300,
        envs=tmp_env,
        metadata=None,
        region_name=None,
        profile_name=None,
        client=client,
    )
    return Sandbox(_backend=backend), client


def test_requires_runtime_arn():
    with pytest.raises(InvalidArgumentException):
        AgentCoreRuntimeBackend.open(
            template=None,
            timeout=300,
            envs=None,
            metadata=None,
            region_name=None,
            profile_name=None,
            client=FakeRuntimeClient(),
        )


def test_session_id_is_at_least_33_chars():
    sbx, _ = make_sandbox()
    assert len(sbx.sandbox_id) >= 33


def test_run_command_streams_stdout_and_exit_code():
    sbx, _ = make_sandbox()
    chunks: list[str] = []
    result = sbx.commands.run("echo hello", on_stdout=chunks.append)
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert "".join(chunks).strip() == "hello"


def test_run_command_nonzero_raises():
    sbx, _ = make_sandbox()
    with pytest.raises(CommandExitException) as ei:
        sbx.commands.run("exit 7")
    assert ei.value.exit_code == 7


def test_files_roundtrip_over_shell(tmp_path):
    sbx, _ = make_sandbox()
    p = str(tmp_path / "sub" / "f.txt")
    sbx.files.write(p, "payload")
    assert sbx.files.read(p) == "payload"
    entries = sbx.files.list(str(tmp_path / "sub"))
    assert any(e.name == "f.txt" for e in entries)
    sbx.files.remove(p)
    assert not sbx.files.exists(p)


def test_binary_files_roundtrip(tmp_path):
    sbx, _ = make_sandbox()
    p = str(tmp_path / "data.bin")
    sbx.files.write(p, b"\x00\x01\x99")
    assert sbx.files.read(p, format="bytes") == bytearray(b"\x00\x01\x99")


def test_run_code_python(tmp_path):
    sbx, _ = make_sandbox()
    execution = sbx.run_code("print(6*7)")
    assert execution.error is None
    assert "42" in "".join(execution.logs.stdout)


def test_kill_stops_session():
    sbx, client = make_sandbox()
    sbx.kill()
    assert client.stopped == [sbx.sandbox_id]


def test_kill_tolerates_never_started_session():
    """AgentCore creates the session lazily; killing an unused one 404s and
    must be treated as already-stopped (verified live 2026)."""

    class NotFoundClient(FakeRuntimeClient):
        def stop_runtime_session(self, **kw: Any):
            raise RuntimeError(
                "ResourceNotFoundException: Session ... not found or has been terminated"
            )

    backend = AgentCoreRuntimeBackend.open(
        template=RUNTIME_ARN,
        timeout=300,
        envs=None,
        metadata=None,
        region_name=None,
        profile_name=None,
        client=NotFoundClient(),
    )
    sbx = Sandbox(_backend=backend)
    assert sbx.kill() is True  # does not raise
    assert sbx.kill() is False


def test_envs_available_to_commands(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sbx, _ = make_sandbox(tmp_env={"MY_VAR": "xyz"})
    result = sbx.commands.run("echo $MY_VAR")
    assert result.stdout.strip() == "xyz"
