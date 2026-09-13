"""CommandHandle behavior against a scripted controller."""

from __future__ import annotations

import pytest

from raitbox import CommandExitException
from raitbox.core.handles import CommandHandle


class ScriptedController:
    """Returns a sequence of poll payloads, then a terminal one."""

    def __init__(self, polls: list[dict]) -> None:
        self._polls = list(polls)
        self.killed = False
        self.stdin: list = []

    def command_poll(self, pid, stdout_off, stderr_off):
        payload = self._polls.pop(0) if self._polls else {"exited": True, "exit_code": 0}
        # honor offsets so the handle only accumulates the new slice
        return {
            "stdout": (payload.get("stdout", "") or "")[stdout_off:]
            if payload.get("_abs")
            else payload.get("stdout", ""),
            "stderr": payload.get("stderr", ""),
            "exited": payload.get("exited", False),
            "exit_code": payload.get("exit_code"),
        }

    def command_kill(self, pid):
        self.killed = True
        return True

    def command_stdin(self, pid, data):
        self.stdin.append(data)


def test_wait_aggregates_and_returns_result():
    c = ScriptedController(
        [
            {"stdout": "hello "},
            {"stdout": "world\n"},
            {"exited": True, "exit_code": 0},
        ]
    )
    h = CommandHandle(c, pid=1, cmd="echo")
    result = h.wait()
    assert result.stdout == "hello world\n"
    assert result.exit_code == 0


def test_wait_raises_on_nonzero():
    c = ScriptedController(
        [
            {"stderr": "boom\n"},
            {"exited": True, "exit_code": 2},
        ]
    )
    h = CommandHandle(c, pid=1, cmd="x")
    with pytest.raises(CommandExitException) as ei:
        h.wait()
    assert ei.value.exit_code == 2
    assert "boom" in ei.value.stderr


def test_wait_invokes_callbacks():
    c = ScriptedController([{"stdout": "a"}, {"exited": True, "exit_code": 0}])
    chunks: list[str] = []
    CommandHandle(c, 1, "x").wait(on_stdout=chunks.append)
    assert chunks == ["a"]


def test_iteration_yields_chunks():
    c = ScriptedController(
        [
            {"stdout": "one\n"},
            {"stdout": "two\n"},
            {"exited": True, "exit_code": 0},
        ]
    )
    assert list(CommandHandle(c, 1, "x")) == ["one\n", "two\n"]


def test_kill_and_stdin():
    c = ScriptedController([])
    h = CommandHandle(c, 1, "x")
    assert h.kill() is True and c.killed
    h.send_stdin("data\n")
    assert c.stdin == ["data\n"]
