"""AgentCore Code Interpreter backend + Sandbox facade tests (no AWS)."""

from __future__ import annotations

import pytest

from raitbox import CommandExitException, NotSupportedError, Sandbox
from raitbox.backends.agentcore_ci import AgentCoreCodeInterpreterBackend
from tests.conftest import FakeCIClient


def make_sandbox(client=None):
    client = client or FakeCIClient()
    backend = AgentCoreCodeInterpreterBackend.open(
        template=None,
        timeout=300,
        envs=None,
        metadata=None,
        region_name=None,
        profile_name=None,
        client=client,
    )
    return Sandbox(_backend=backend), client


def test_run_code_restores_trailing_newline():
    sbx, _ = make_sandbox()
    execution = sbx.run_code("print('hello from sandbox')")
    assert execution.error is None
    assert execution.logs.stdout == ["hello from sandbox\n"]
    assert execution.text == "hello from sandbox"


def test_run_code_error_parses_exception():
    sbx, _ = make_sandbox()
    execution = sbx.run_code("1/0")
    assert execution.error is not None
    assert execution.error.name == "ZeroDivisionError"
    assert "division by zero" in execution.error.value


def test_command_pty_merge_exposed_as_stdout():
    sbx, _ = make_sandbox()
    result = sbx.commands.run("echo ok")
    assert result.exit_code == 0
    assert result.stdout == "ok\r\n"


def test_command_nonzero_raises():
    sbx, _ = make_sandbox()
    with pytest.raises(CommandExitException) as ei:
        sbx.commands.run("false")
    assert ei.value.exit_code == 3


def test_files_roundtrip_text_and_bytes():
    sbx, _ = make_sandbox()
    sbx.files.write("a/b.txt", "content")
    assert sbx.files.read("a/b.txt") == "content"
    sbx.files.write("a/data.bin", b"\x00\x01\x02")
    assert sbx.files.read("a/data.bin", format="bytes") == bytearray(b"\x00\x01\x02")
    assert sbx.files.exists("a/b.txt")
    names = {e.name for e in sbx.files.list("a")}
    assert {"b.txt", "data.bin"} <= names
    sbx.files.remove("a/b.txt")
    assert not sbx.files.exists("a/b.txt")


def test_get_host_not_supported():
    sbx, _ = make_sandbox()
    with pytest.raises(NotSupportedError):
        sbx.get_host(8080)


def test_kill_stops_session_and_is_idempotent():
    sbx, client = make_sandbox()
    assert sbx.kill() is True
    assert client.stopped == [sbx.sandbox_id]
    assert sbx.kill() is False


def test_context_manager_kills():
    client = FakeCIClient()
    backend = AgentCoreCodeInterpreterBackend.open(
        template=None,
        timeout=300,
        envs=None,
        metadata=None,
        region_name=None,
        profile_name=None,
        client=client,
    )
    with Sandbox(_backend=backend) as sbx:
        sid = sbx.sandbox_id
    assert client.stopped == [sid]


def test_envs_exported_on_open():
    client = FakeCIClient()
    AgentCoreCodeInterpreterBackend.open(
        template=None,
        timeout=300,
        envs={"FOO": "bar"},
        metadata=None,
        region_name=None,
        profile_name=None,
        client=client,
    )
    # env export runs an executeCode call; no files were written by it.
    assert client.files == {}
