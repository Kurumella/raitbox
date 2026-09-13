"""Interactive shell for the agentcore-runtime backend (fake shell client)."""

from __future__ import annotations

from typing import Any

from raitbox import AsyncSandbox, Sandbox
from raitbox.backends.agentcore_runtime import AgentCoreRuntimeBackend

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/demo"


class _Frame:
    def __init__(self, channel: str, text: str) -> None:
        self.channel = channel
        self.text = text


class FakeShell:
    def __init__(self) -> None:
        self.shell_id = "sh-1"
        self.reconnected = False
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def __aiter__(self):
        yield _Frame("STDOUT", "hello\n")
        yield _Frame("STDOUT", "world\n")


class _ShellCM:
    def __init__(self, shell: FakeShell) -> None:
        self._shell = shell

    async def __aenter__(self) -> FakeShell:
        return self._shell

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeShellClient:
    def __init__(self) -> None:
        self.shell = FakeShell()
        self.opened_with: dict[str, Any] = {}

    def open_shell(self, arn: str, session_id=None, shell_id=None):
        self.opened_with = {"arn": arn, "session_id": session_id, "shell_id": shell_id}
        return _ShellCM(self.shell)


class DummyRuntimeClient:
    def stop_runtime_session(self, **kw: Any):
        return {}


def _async_sandbox(shell_client):
    backend = AgentCoreRuntimeBackend.open(
        template=RUNTIME_ARN,
        timeout=300,
        envs=None,
        metadata=None,
        region_name="us-west-2",
        profile_name=None,
        client=DummyRuntimeClient(),
        shell_client=shell_client,
    )
    return AsyncSandbox(Sandbox(_backend=backend))


async def test_open_shell_streams_frames():
    shell_client = FakeShellClient()
    sbx = _async_sandbox(shell_client)
    lines: list[str] = []
    async with sbx.open_shell() as shell:
        await shell.send("echo hello\n")
        async for frame in shell:
            lines.append(frame.text)
    assert lines == ["hello\n", "world\n"]
    assert shell_client.shell.sent == ["echo hello\n"]
    # session id (>=33 chars) is passed through to pin the microVM.
    assert len(shell_client.opened_with["session_id"]) >= 33


def test_open_shell_without_sdk_raises(monkeypatch):
    from raitbox.core.errors import NotSupportedError

    backend = AgentCoreRuntimeBackend.open(
        template=RUNTIME_ARN,
        timeout=300,
        envs=None,
        metadata=None,
        region_name=None,
        profile_name=None,
        client=DummyRuntimeClient(),
    )
    # No shell_client and no bedrock-agentcore installed -> NotSupportedError.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("bedrock_agentcore"):
            raise ImportError("not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    import pytest

    with pytest.raises(NotSupportedError):
        backend.open_shell()
