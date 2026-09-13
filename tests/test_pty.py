"""Interactive PTY: handle draining, backend guard, and a real in-VM roundtrip."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from raitbox import NotSupportedError, PtySize, Sandbox
from raitbox.backends import microvm as microvm_mod
from raitbox.core.handles import PtyHandle
from tests.conftest import FakeCIClient

IMAGE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:raitbox"


class FakePtyBackend:
    """Serves a fixed output stream, then reports exit; records stdin/resize/kill."""

    def __init__(self, output: bytes) -> None:
        self._out = output
        self.stdin: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self.killed: list[int] = []

    def pty_poll(self, pid: int, offset: int) -> dict[str, Any]:
        chunk = self._out[offset:]
        return {
            "data": base64.b64encode(chunk).decode(),
            "exited": True,
            "exit_code": 0,
        }

    def pty_stdin(self, pid: int, data: bytes) -> None:
        self.stdin.append(data)

    def pty_resize(self, pid: int, rows: int, cols: int) -> None:
        self.resizes.append((rows, cols))

    def pty_kill(self, pid: int) -> bool:
        self.killed.append(pid)
        return True


def test_pty_handle_sync_drain_and_controls():
    b = FakePtyBackend(b"hello\r\n")
    h = PtyHandle(b, pid=7)
    assert h.wait() == 0
    assert h.data == b"hello\r\n"

    h.send_stdin(b"ls\n")
    h.resize(PtySize(rows=40, cols=100))
    assert h.kill() is True
    assert b.stdin == [b"ls\n"]
    assert b.resizes == [(40, 100)]
    assert b.killed == [7]


def test_pty_handle_on_data_callback():
    b = FakePtyBackend(b"streamed")
    seen: list[bytes] = []
    h = PtyHandle(b, pid=1, on_data=seen.append)
    assert h.wait() == 0
    assert b"".join(seen) == b"streamed"


def test_pty_not_supported_on_ci():
    sbx = Sandbox(backend="agentcore-ci", client=FakeCIClient())
    with pytest.raises(NotSupportedError):
        sbx.pty.create(PtySize(rows=24, cols=80))


def test_microvm_pty_calls_go_over_http():
    class FakeHttp:
        def __init__(self) -> None:
            self.posts: list[tuple[str, dict]] = []

        def post(self, path: str, json: dict | None = None, **kw: Any):
            self.posts.append((path, json or {}))
            body = {
                "/pty/start": {"pid": 42},
                "/pty/stdin": {"ok": True},
                "/pty/resize": {"ok": True},
                "/pty/kill": {"ok": True},
            }[path]
            return type("R", (), {"json": lambda self: body})()

        def get(self, *a: Any, **k: Any):
            return None

    http = FakeHttp()
    b = microvm_mod.MicroVMBackend(
        client=object(),
        microvm_id="mv-1",
        endpoint="mv-1.on.aws",
        template=IMAGE,
        http=http,
        timeout=300,
    )
    assert b.pty_start(rows=24, cols=80) == 42
    b.pty_stdin(42, b"echo hi\n")
    b.pty_resize(42, 30, 120)
    assert b.pty_kill(42) is True
    # stdin was base64-encoded on the wire.
    stdin_call = next(p for p in http.posts if p[0] == "/pty/stdin")
    assert base64.b64decode(stdin_call[1]["data"]) == b"echo hi\n"


def test_agent_server_pty_real_roundtrip():
    pytest.importorskip("fastapi")
    import time

    from raitbox.agent_server import server as srv_mod

    # A real pty running `echo` — no HTTP layer, exercise the session directly.
    sess = srv_mod._PtySession(rows=24, cols=80, cwd=None, envs={}, cmd="echo raitbox-pty")
    deadline = time.time() + 10
    out = b""
    while time.time() < deadline:
        data, exited, code = sess.read_from(len(out))
        out += data
        if exited:
            break
        time.sleep(0.05)
    sess.kill()
    assert b"raitbox-pty" in out
    assert exited and code == 0
