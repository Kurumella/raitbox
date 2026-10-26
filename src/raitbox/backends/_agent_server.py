"""Backends that talk to the RaitBox agent server (``raitbox.agent_server``) over HTTP.

The agent server exposes one JSON-over-HTTP contract (``/run_code``, ``/run_command``,
``/files/*``, ``/commands/*``, ``/pty/*``, ``/lsp/*``). Backends that run it (microvm, dunefield)
share this implementation and differ only in lifecycle and in the transport (``AgentHttp``) that
authenticates and routes each request.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any, Protocol

import requests

from ..core.errors import FileNotFoundException
from ..core.types import (
    CommandResult,
    EntryInfo,
    Execution,
    ExecutionError,
    Logs,
    Result,
    WriteInfo,
)
from .base import Backend


class AgentHttp(Protocol):
    """Transport to one sandbox's agent server; raises RaitBox errors for non-2xx responses."""

    def post(self, path: str, json: Any = None, **kw: Any) -> requests.Response: ...

    def get(self, path: str, **kw: Any) -> requests.Response: ...


class AgentServerBackend(Backend):
    """Code, commands, files, PTY and LSP through the agent server. Subclasses set ``_http``."""

    _http: AgentHttp

    # -- language server (LSP) ---------------------------------------------

    def lsp_start(self, language: str, path: str) -> str:
        data = self._http.post("/lsp/start", json={"language": language, "path": path}).json()
        return data["server_id"]

    def lsp_call(self, server_id: str, op: str, params: dict[str, Any]) -> Any:
        return (
            self._http.post("/lsp/call", json={"server_id": server_id, "op": op, "params": params})
            .json()
            .get("result")
        )

    def lsp_stop(self, server_id: str) -> None:
        self._http.post("/lsp/stop", json={"server_id": server_id})

    # -- interactive PTY ---------------------------------------------------

    def pty_start(
        self,
        *,
        rows: int,
        cols: int,
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
        cmd: str | None = None,
    ) -> int:
        data = self._http.post(
            "/pty/start",
            json={"rows": rows, "cols": cols, "cwd": cwd, "envs": envs or {}, "cmd": cmd},
        ).json()
        return data["pid"]

    def pty_poll(self, pid: int, offset: int) -> dict[str, Any]:
        return self._http.post("/pty/poll", json={"pid": pid, "offset": offset}).json()

    def pty_stdin(self, pid: int, data: bytes) -> None:
        raw = data if isinstance(data, bytes) else str(data).encode()
        self._http.post("/pty/stdin", json={"pid": pid, "data": base64.b64encode(raw).decode()})

    def pty_resize(self, pid: int, rows: int, cols: int) -> None:
        self._http.post("/pty/resize", json={"pid": pid, "rows": rows, "cols": cols})

    def pty_kill(self, pid: int) -> bool:
        return self._http.post("/pty/kill", json={"pid": pid}).json().get("ok", False)

    # -- code execution ----------------------------------------------------

    def run_code(
        self,
        code: str,
        language: str = "python",
        envs: dict[str, str] | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> Execution:
        data = self._http.post(
            "/run_code", json={"code": code, "language": language, "envs": envs or {}}
        ).json()
        stdout = data.get("stdout", "")
        stderr = data.get("stderr", "")
        if stdout and on_stdout:
            on_stdout(stdout)
        if stderr and on_stderr:
            on_stderr(stderr)
        logs = Logs(
            stdout=[stdout] if stdout else [],
            stderr=[stderr] if stderr else [],
        )
        err = data.get("error")
        if err:
            return Execution(
                results=[],
                logs=logs,
                error=ExecutionError(
                    name=err.get("name", "Error"),
                    value=err.get("value", ""),
                    traceback=err.get("traceback", ""),
                ),
            )
        results = [
            Result(text=r, is_main_result=(i == 0)) for i, r in enumerate(data.get("results", []))
        ]
        return Execution(results=results, logs=logs, error=None)

    def run_command(
        self,
        cmd: str,
        *,
        envs: dict[str, str] | None = None,
        cwd: str | None = None,
        user: str | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        data = self._http.post(
            "/run_command",
            json={"cmd": cmd, "envs": envs or {}, "cwd": cwd, "timeout": timeout},
            timeout=(timeout or 300) + 10,
        ).json()
        stdout = data.get("stdout", "")
        stderr = data.get("stderr", "")
        if stdout and on_stdout:
            on_stdout(stdout)
        if stderr and on_stderr:
            on_stderr(stderr)
        return CommandResult(stdout=stdout, stderr=stderr, exit_code=data.get("exit_code", 0))

    # -- filesystem --------------------------------------------------------

    def file_write(self, path: str, data: str | bytes) -> WriteInfo:
        if isinstance(data, str):
            payload = {"path": path, "text": data}
        else:
            payload = {"path": path, "b64": base64.b64encode(data).decode()}
        self._http.post("/files/write", json=payload)
        name = path.rstrip("/").rpartition("/")[2]
        return WriteInfo(name=name, type="file", path=path)

    def file_read(self, path: str, format: str = "text") -> str | bytearray:
        resp = self._http.post("/files/read", json={"path": path, "format": format})
        data = resp.json()
        if data.get("not_found"):
            raise FileNotFoundException(f"No such file: {path!r}")
        if "b64" in data:
            raw = base64.b64decode(data["b64"])
            return raw.decode() if format == "text" else bytearray(raw)
        text = data.get("text", "")
        return text if format == "text" else bytearray(text.encode())

    def file_list(self, path: str = "", depth: int = 1) -> list[EntryInfo]:
        data = self._http.post("/files/list", json={"path": path, "depth": depth}).json()
        return [
            EntryInfo(name=e["name"], type=e["type"], path=e["path"])
            for e in data.get("entries", [])
        ]

    def file_remove(self, path: str) -> None:
        self._http.post("/files/remove", json={"path": path})

    def file_make_dir(self, path: str) -> bool:
        return self._http.post("/files/make_dir", json={"path": path}).json().get("ok", False)

    def file_rename(self, old_path: str, new_path: str) -> EntryInfo:
        self._http.post("/files/rename", json={"old_path": old_path, "new_path": new_path})
        name = new_path.rstrip("/").rpartition("/")[2]
        return EntryInfo(name=name, type="file", path=new_path)

    # -- background commands -----------------------------------------------

    def command_start(
        self,
        cmd: str,
        *,
        envs: dict[str, str] | None = None,
        cwd: str | None = None,
        user: str | None = None,
    ) -> int:
        data = self._http.post(
            "/commands/start", json={"cmd": cmd, "envs": envs or {}, "cwd": cwd}
        ).json()
        return data["pid"]

    def command_poll(self, pid: int, stdout_off: int, stderr_off: int) -> dict[str, Any]:
        return self._http.post(
            "/commands/poll",
            json={"pid": pid, "stdout_off": stdout_off, "stderr_off": stderr_off},
        ).json()

    def command_kill(self, pid: int) -> bool:
        return self._http.post("/commands/kill", json={"pid": pid}).json().get("ok", False)

    def command_stdin(self, pid: int, data: str | bytes) -> None:
        text = data.decode() if isinstance(data, bytes) else data
        self._http.post("/commands/stdin", json={"pid": pid, "data": text})

    def command_list(self) -> list:
        from ..core.types import ProcessInfo

        data = self._http.post("/commands/list").json()
        return [ProcessInfo(pid=p["pid"], cmd=p["cmd"]) for p in data.get("processes", [])]
