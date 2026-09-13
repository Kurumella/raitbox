"""MCP tools: sandbox operations map to Sandbox calls; registration wiring."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from raitbox.mcp import RaitboxTools


class FakeExecution:
    class _Logs:
        stdout: ClassVar[list[str]] = ["hi\n"]
        stderr: ClassVar[list[str]] = []

    logs = _Logs()
    text = "hi"
    error = None


class FakeSandbox:
    _n = 0

    def __init__(self, **kwargs: Any) -> None:
        FakeSandbox._n += 1
        self.sandbox_id = f"sbx-{FakeSandbox._n}"
        self.kwargs = kwargs
        self.killed = False
        self.files = self
        self.commands = self
        self._files: dict[str, str] = {}

    # run_code / commands / files, all on one object for the fake
    def run_code(self, code: str, language: str = "python"):
        return FakeExecution()

    def run(self, cmd: str):
        from raitbox.core.types import CommandResult

        return CommandResult(stdout=f"ran {cmd}", stderr="", exit_code=0)

    def write(self, path: str, content: str):
        from raitbox.core.types import WriteInfo

        self._files[path] = content
        return WriteInfo(name=path.rsplit("/", 1)[-1], type="file", path=path)

    def read(self, path: str):
        return self._files[path]

    def list(self, path: str = ""):
        from raitbox.core.types import EntryInfo

        return [EntryInfo(name=p, type="file", path=p) for p in self._files]

    def kill(self):
        self.killed = True
        return True


@pytest.fixture
def tools():
    FakeSandbox._n = 0
    return RaitboxTools(sandbox_factory=lambda **kw: FakeSandbox(**kw), backend="microvm")


def test_create_applies_defaults_and_tracks(tools):
    sid = tools.create_sandbox()
    assert sid == "sbx-1"
    assert tools.list_sandboxes() == ["sbx-1"]


def test_run_code_and_command(tools):
    sid = tools.create_sandbox()
    assert tools.run_code(sid, "print('hi')") == {
        "stdout": "hi\n",
        "stderr": "",
        "text": "hi",
        "error": None,
    }
    assert tools.run_command(sid, "ls")["stdout"] == "ran ls"


def test_files_roundtrip(tools):
    sid = tools.create_sandbox()
    tools.files_write(sid, "/a.txt", "data")
    assert tools.files_read(sid, "/a.txt") == "data"
    assert [e["path"] for e in tools.files_list(sid)] == ["/a.txt"]


def test_command_nonzero_exit_returns_result(tools, monkeypatch):
    sid = tools.create_sandbox()
    sbx = tools._get(sid)

    def _raise(cmd):
        from raitbox.core.errors import CommandExitException

        raise CommandExitException(stdout="", stderr="boom", exit_code=2)

    monkeypatch.setattr(sbx, "run", _raise)
    assert tools.run_command(sid, "false") == {"stdout": "", "stderr": "boom", "exit_code": 2}


def test_kill_and_unknown_id(tools):
    sid = tools.create_sandbox()
    assert tools.kill_sandbox(sid) is True
    assert tools.kill_sandbox(sid) is False
    with pytest.raises(ValueError, match="unknown sandbox_id"):
        tools.run_code("nope", "x")


def test_register_collects_all_tools():
    registered: list[str] = []

    class FakeServer:
        def tool(self, name: str):
            registered.append(name)
            return lambda fn: fn

    RaitboxTools(sandbox_factory=lambda **kw: FakeSandbox(**kw)).register(FakeServer())
    assert set(registered) == {
        "create_sandbox",
        "run_code",
        "run_command",
        "files_write",
        "files_read",
        "files_list",
        "list_sandboxes",
        "kill_sandbox",
    }


def test_build_server_needs_mcp_or_builds():
    pytest.importorskip("mcp")
    from raitbox.mcp import build_server

    server = build_server(name="raitbox-test")
    assert server is not None
