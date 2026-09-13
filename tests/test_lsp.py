"""LSP: handle behavior, backend guard, and in-VM JSON-RPC framing."""

from __future__ import annotations

import json
from typing import Any

import pytest

from raitbox import CompletionItem, LspSymbol, NotSupportedError, Position, Sandbox
from raitbox.backends import microvm as microvm_mod
from raitbox.core.handles import LspServer
from tests.conftest import FakeCIClient

IMAGE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:raitbox"


class FakeLspBackend:
    """Records LSP calls and returns canned payloads."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.started: list[tuple[str, str]] = []
        self.stopped: list[str] = []

    def lsp_start(self, language: str, path: str) -> str:
        self.started.append((language, path))
        return "srv-1"

    def lsp_call(self, server_id: str, op: str, params: dict[str, Any]) -> Any:
        self.calls.append((op, params))
        if op == "document_symbols":
            return [{"name": "foo", "kind": 12, "location": {"uri": "file:///a.py"}}]
        if op == "workspace_symbols":
            return [{"name": "Bar", "kind": 5}]
        if op == "completions":
            return [{"label": "append", "kind": 2, "detail": "list.append"}]
        return None

    def lsp_stop(self, server_id: str) -> None:
        self.stopped.append(server_id)


def test_lsp_handle_lazy_start_and_queries():
    b = FakeLspBackend()
    srv = LspServer(b, "python", "/proj")
    assert b.started == []  # not started until first use

    syms = srv.document_symbols("/proj/a.py")
    assert b.started == [("python", "/proj")]  # lazily started
    assert syms == [LspSymbol(name="foo", kind=12, location={"uri": "file:///a.py"})]

    comps = srv.completions("/proj/a.py", Position(3, 5))
    assert comps == [CompletionItem(label="append", kind=2, detail="list.append")]
    # Position was flattened to line/character for the backend call.
    assert b.calls[-1] == ("completions", {"path": "/proj/a.py", "line": 3, "character": 5})

    ws = srv.workspace_symbols("Bar")
    assert ws[0].name == "Bar" and ws[0].kind == 5

    srv.stop()
    assert b.stopped == ["srv-1"]


def test_lsp_completions_accepts_tuple_position():
    b = FakeLspBackend()
    LspServer(b, "python", "/p").completions("/p/x.py", (1, 2))
    assert b.calls[-1][1] == {"path": "/p/x.py", "line": 1, "character": 2}


def test_lsp_context_manager_starts_and_stops():
    b = FakeLspBackend()
    with LspServer(b, "python", "/p") as srv:
        assert b.started == [("python", "/p")]
        srv.did_open("/p/x.py")
    assert b.stopped == ["srv-1"]


def test_lsp_not_supported_on_ci():
    sbx = Sandbox(backend="agentcore-ci", client=FakeCIClient())
    with pytest.raises(NotSupportedError):
        sbx.lsp.create_server("python", "/p").document_symbols("/p/x.py")


def test_microvm_lsp_calls_go_over_http(monkeypatch):
    class FakeHttp:
        def __init__(self) -> None:
            self.posts: list[tuple[str, dict]] = []

        def post(self, path: str, json: dict | None = None, **kw: Any):
            self.posts.append((path, json or {}))
            body = {
                "/lsp/start": {"server_id": "srv-9"},
                "/lsp/call": {"result": [{"name": "f", "kind": 12}]},
                "/lsp/stop": {"ok": True},
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
    assert b.lsp_start("python", "/p") == "srv-9"
    assert b.lsp_call("srv-9", "document_symbols", {"path": "/p/a.py"}) == [
        {"name": "f", "kind": 12}
    ]
    b.lsp_stop("srv-9")
    assert [p[0] for p in http.posts] == ["/lsp/start", "/lsp/call", "/lsp/stop"]


def test_agent_server_lsp_jsonrpc_roundtrip():
    pytest.importorskip("fastapi")
    from raitbox.agent_server import server as srv_mod

    # A fake language-server process: frames LSP responses on stdout.
    class FakeProc:
        def __init__(self) -> None:
            import io

            self.stdin = io.BytesIO()
            self._out = io.BytesIO()
            self.stdout = self._out

        def _frame(self, obj: dict) -> bytes:
            body = json.dumps(obj).encode()
            return f"Content-Length: {len(body)}\r\n\r\n".encode() + body

        def feed(self, obj: dict) -> None:
            pos = self._out.tell()
            self._out.seek(0, 2)
            self._out.write(self._frame(obj))
            self._out.seek(pos)

        def terminate(self) -> None: ...

    # Build a session without spawning a real process.
    sess = srv_mod._LspSession.__new__(srv_mod._LspSession)
    sess.language_id = "python"
    sess.proc = FakeProc()
    sess._id = 0
    import threading

    sess._lock = threading.Lock()

    # Queue the response the "server" returns for request id 1.
    sess.proc.feed({"jsonrpc": "2.0", "id": 1, "result": [{"name": "g", "kind": 12}]})
    result = sess.request("textDocument/documentSymbol", {"textDocument": {"uri": "file:///a.py"}})
    assert result == [{"name": "g", "kind": 12}]
    # The request we wrote was correctly Content-Length framed.
    assert b"Content-Length:" in sess.proc.stdin.getvalue()
