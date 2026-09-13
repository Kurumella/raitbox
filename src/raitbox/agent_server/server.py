"""RaitBox agent server — runs INSIDE a Lambda MicroVM image.

Exposes the HTTP contract the ``microvm`` backend calls (``/run_code``,
``/run_command``, ``/commands/*``, ``/files/*``, ``/health``) plus the required
Lambda MicroVM lifecycle hooks under ``/aws/lambda-microvms/runtime/v1/``.

Endpoints are synchronous ``def`` handlers: FastAPI runs them in a threadpool,
so the blocking subprocess/filesystem work here never stalls the event loop.
Request bodies are validated by Pydantic models (and documented in the
auto-generated OpenAPI schema).

Snapshot safety: all MicroVMs restored from one image version share identical
memory, so per-VM identity and entropy MUST be generated in the ``/run`` hook,
never at import/build time (see AWS MicroVMs snapshot guidance).

Run locally / in the image::

    uvicorn raitbox.agent_server.server:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import io
import json
import os
import pty as ptylib
import secrets
import shutil
import struct
import subprocess
import termios
import threading
import time
import traceback
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI
    from pydantic import BaseModel
except ImportError as e:  # pragma: no cover - only needed inside the image
    raise ImportError(
        "raitbox.agent_server requires the 'server' extra: pip install 'raitbox[server]'"
    ) from e

app = FastAPI(title="RaitBox agent server", version="0.1.0")

HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1"

# Per-VM identity, (re)generated in the /run hook for snapshot safety.
_STATE: dict[str, Any] = {"microvm_id": None, "instance_secret": None}

# Persistent namespace for stateful run_code (Jupyter-like), per E2B semantics.
_KERNEL_GLOBALS: dict[str, Any] = {"__name__": "__main__"}


# -- request models --------------------------------------------------------


class RunCodeReq(BaseModel):
    code: str = ""
    language: str = "python"
    envs: dict[str, str] = {}


class RunCommandReq(BaseModel):
    cmd: str = ""
    envs: dict[str, str] = {}
    cwd: str | None = None
    timeout: float | None = None


class StartCommandReq(BaseModel):
    cmd: str = ""
    envs: dict[str, str] = {}
    cwd: str | None = None


class PollReq(BaseModel):
    pid: int
    stdout_off: int = 0
    stderr_off: int = 0


class PidReq(BaseModel):
    pid: int


class StdinReq(BaseModel):
    pid: int
    data: str = ""


class FileWriteReq(BaseModel):
    path: str
    text: str | None = None
    b64: str | None = None


class FileReadReq(BaseModel):
    path: str
    format: str = "text"


class FileListReq(BaseModel):
    path: str = ""
    depth: int = 1


class PathReq(BaseModel):
    path: str


class RenameReq(BaseModel):
    old_path: str
    new_path: str


class RunHookReq(BaseModel):
    microvmId: str | None = None
    runHookPayload: str | None = None


class LspStartReq(BaseModel):
    language: str = "python"
    path: str = "/"


class LspCallReq(BaseModel):
    server_id: str
    op: str
    params: dict[str, Any] = {}


class LspStopReq(BaseModel):
    server_id: str


class PtyStartReq(BaseModel):
    rows: int = 24
    cols: int = 80
    cwd: str | None = None
    envs: dict[str, str] = {}
    cmd: str | None = None


class PtyPollReq(BaseModel):
    pid: int
    offset: int = 0


class PtyStdinReq(BaseModel):
    pid: int
    data: str = ""  # base64


class PtyResizeReq(BaseModel):
    pid: int
    rows: int
    cols: int


class ComputerReq(BaseModel):
    action: str
    params: dict[str, Any] = {}


# -- health ---------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "microvm_id": _STATE["microvm_id"]}


# -- code execution --------------------------------------------------------


def _error(exc_name: str, value: str, tb: str = "") -> dict[str, str]:
    return {"name": exc_name, "value": value, "traceback": tb}


@app.post("/run_code")
def run_code(req: RunCodeReq) -> dict[str, Any]:
    envs = {str(k): str(v) for k, v in req.envs.items()}
    if req.language != "python":
        interp = {"javascript": "node", "typescript": "ts-node"}.get(req.language)
        if interp is None:
            return {
                "stdout": "",
                "stderr": f"unsupported language: {req.language}",
                "results": [],
                "error": _error("ValueError", req.language),
            }
        proc = subprocess.run(
            [interp, "-e", req.code],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, **envs},
        )
        err = None if proc.returncode == 0 else _error("Error", proc.stderr, proc.stderr)
        return {"stdout": proc.stdout, "stderr": proc.stderr, "results": [], "error": err}

    # Apply per-call env into the kernel so the executed code sees it.
    os.environ.update(envs)
    buf = io.StringIO()
    error = None
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            compiled = compile(req.code, "<cell>", "exec")
            exec(compiled, _KERNEL_GLOBALS)  # noqa: S102 - the VM sandbox IS the boundary
    except Exception as exc:  # noqa: BLE001 - report any user error E2B-style
        error = _error(type(exc).__name__, str(exc), traceback.format_exc())
    stdout = buf.getvalue()
    return {
        "stdout": stdout,
        "stderr": "" if error is None else stdout,
        "results": [],
        "error": error,
    }


@app.post("/run_command")
def run_command(req: RunCommandReq) -> dict[str, Any]:
    env = {**os.environ, **{str(k): str(v) for k, v in req.envs.items()}}
    try:
        proc = subprocess.run(
            ["/bin/bash", "-c", req.cmd],
            capture_output=True,
            text=True,
            cwd=req.cwd or None,
            env=env,
            timeout=req.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"command timed out after {req.timeout}s", "exit_code": 124}
    return {"stdout": proc.stdout, "stderr": proc.stderr, "exit_code": proc.returncode}


# -- background commands ---------------------------------------------------


class _Process:
    """A tracked background process with buffered, thread-safe output."""

    def __init__(self, cmd: str, env: dict[str, str], cwd: str | None) -> None:
        self.cmd = cmd
        self.proc = subprocess.Popen(
            ["/bin/bash", "-c", cmd],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=cwd or None,
            env=env,
        )
        self._lock = threading.Lock()
        self._out = ""
        self._err = ""
        self._readers = [
            threading.Thread(target=self._pump, args=(self.proc.stdout, "out"), daemon=True),
            threading.Thread(target=self._pump, args=(self.proc.stderr, "err"), daemon=True),
        ]
        for t in self._readers:
            t.start()

    def _pump(self, pipe: Any, which: str) -> None:
        for line in iter(pipe.readline, ""):
            with self._lock:
                if which == "out":
                    self._out += line
                else:
                    self._err += line
        pipe.close()

    def poll(self, out_off: int, err_off: int) -> dict[str, Any]:
        with self._lock:
            out, err = self._out[out_off:], self._err[err_off:]
        rc = self.proc.poll()
        exited = rc is not None and not any(t.is_alive() for t in self._readers)
        return {"stdout": out, "stderr": err, "exited": exited, "exit_code": rc if exited else None}

    def send_stdin(self, data: str) -> None:
        if self.proc.stdin:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def kill(self) -> None:
        self.proc.kill()


_PROCS: dict[int, _Process] = {}


@app.post("/commands/start")
def commands_start(req: StartCommandReq) -> dict[str, Any]:
    env = {**os.environ, **{str(k): str(v) for k, v in req.envs.items()}}
    p = _Process(req.cmd, env, req.cwd)
    _PROCS[p.proc.pid] = p
    return {"pid": p.proc.pid}


@app.post("/commands/poll")
def commands_poll(req: PollReq) -> dict[str, Any]:
    p = _PROCS.get(req.pid)
    if p is None:
        return {"stdout": "", "stderr": "", "exited": True, "exit_code": None}
    return p.poll(req.stdout_off, req.stderr_off)


@app.post("/commands/stdin")
def commands_stdin(req: StdinReq) -> dict[str, Any]:
    p = _PROCS.get(req.pid)
    if p is not None:
        p.send_stdin(req.data)
    return {"ok": p is not None}


@app.post("/commands/kill")
def commands_kill(req: PidReq) -> dict[str, Any]:
    p = _PROCS.get(req.pid)
    if p is not None:
        p.kill()
    return {"ok": p is not None}


@app.post("/commands/list")
def commands_list() -> dict[str, Any]:
    procs = [{"pid": pid, "cmd": p.cmd} for pid, p in _PROCS.items() if p.proc.poll() is None]
    return {"processes": procs}


# -- filesystem ------------------------------------------------------------


@app.post("/files/write")
def files_write(req: FileWriteReq) -> dict[str, Any]:
    path = Path(req.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if req.b64 is not None:
        path.write_bytes(base64.b64decode(req.b64))
    else:
        path.write_text(req.text or "")
    return {"ok": True, "path": str(path)}


@app.post("/files/read")
def files_read(req: FileReadReq) -> dict[str, Any]:
    path = Path(req.path)
    if not path.exists():
        return {"not_found": True}
    if req.format == "text":
        try:
            return {"text": path.read_text()}
        except UnicodeDecodeError:
            return {"b64": base64.b64encode(path.read_bytes()).decode()}
    return {"b64": base64.b64encode(path.read_bytes()).decode()}


@app.post("/files/list")
def files_list(req: FileListReq) -> dict[str, Any]:
    base = Path(req.path or ".")
    entries = []
    if base.exists():
        for child in sorted(base.iterdir()):
            entries.append(
                {
                    "name": child.name,
                    "type": "dir" if child.is_dir() else "file",
                    "path": str(child),
                }
            )
    return {"entries": entries}


@app.post("/files/remove")
def files_remove(req: PathReq) -> dict[str, Any]:
    path = Path(req.path)
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink()
    return {"ok": True}


@app.post("/files/make_dir")
def files_make_dir(req: PathReq) -> dict[str, Any]:
    Path(req.path).mkdir(parents=True, exist_ok=True)
    return {"ok": True}


@app.post("/files/rename")
def files_rename(req: RenameReq) -> dict[str, Any]:
    Path(req.old_path).rename(req.new_path)
    return {"ok": True}


# -- language server (LSP) -------------------------------------------------
# A minimal LSP client: spawn a language server, speak JSON-RPC over stdio with
# Content-Length framing, and expose a few semantic operations. Sessions persist
# across HTTP calls in ``_LSP``, keyed by an opaque server id.


def _uri(path: str) -> str:
    return f"file://{path}"


# Invoke pylsp via ``python -m`` so it works even when the console script is not
# on PATH (pip may not install it into the image's bin dir).
_LSP_SERVERS = {
    "python": ["python", "-m", "pylsp"],
    "py": ["python", "-m", "pylsp"],
    "typescript": ["typescript-language-server", "--stdio"],
    "javascript": ["typescript-language-server", "--stdio"],
    "ts": ["typescript-language-server", "--stdio"],
    "js": ["typescript-language-server", "--stdio"],
}
_LANG_IDS = {"py": "python", "ts": "typescript", "js": "javascript"}


class _LspSession:
    """One language-server subprocess speaking LSP JSON-RPC over stdio."""

    def __init__(self, language: str, root: str) -> None:
        cmd = _LSP_SERVERS.get(language.lower())
        if cmd is None:
            raise ValueError(f"no language server configured for {language!r}")
        self.language_id = _LANG_IDS.get(language.lower(), language.lower())
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        self._id = 0
        self._lock = threading.Lock()
        self.request(
            "initialize", {"processId": os.getpid(), "rootUri": _uri(root), "capabilities": {}}
        )
        self.notify("initialized", {})

    def _write(self, msg: dict) -> None:
        body = json.dumps(msg).encode()
        stdin = self.proc.stdin
        if stdin is None:
            return
        stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        stdin.flush()

    def _read(self) -> dict | None:
        stdout = self.proc.stdout
        if stdout is None:
            return None
        length = 0
        while True:
            line = stdout.readline()
            if not line:
                return None
            text = line.decode("utf-8", "replace").strip()
            if text == "":
                break
            key, _, val = text.partition(":")
            if key.strip().lower() == "content-length":
                length = int(val.strip())
        raw = stdout.read(length) if length else b""
        return json.loads(raw) if raw else None

    def request(self, method: str, params: dict, timeout: float = 15.0) -> Any:
        with self._lock:
            self._id += 1
            rid = self._id
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            deadline = time.time() + timeout
            while time.time() < deadline:
                msg = self._read()
                if msg is None:
                    return None
                if msg.get("id") == rid and ("result" in msg or "error" in msg):
                    return msg.get("result")
            return None

    def notify(self, method: str, params: dict) -> None:
        with self._lock:
            self._write({"jsonrpc": "2.0", "method": method, "params": params})

    # -- semantic ops ------------------------------------------------------

    def did_open(self, path: str) -> None:
        text = Path(path).read_text() if Path(path).exists() else ""
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": _uri(path),
                    "languageId": self.language_id,
                    "version": 1,
                    "text": text,
                }
            },
        )

    def did_close(self, path: str) -> None:
        self.notify("textDocument/didClose", {"textDocument": {"uri": _uri(path)}})

    def document_symbols(self, path: str) -> list:
        return (
            self.request("textDocument/documentSymbol", {"textDocument": {"uri": _uri(path)}}) or []
        )

    def workspace_symbols(self, query: str) -> list:
        return self.request("workspace/symbol", {"query": query}) or []

    def completions(self, path: str, line: int, character: int) -> list:
        res = self.request(
            "textDocument/completion",
            {
                "textDocument": {"uri": _uri(path)},
                "position": {"line": line, "character": character},
            },
        )
        if isinstance(res, dict):
            return res.get("items", [])
        return res or []

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.notify("shutdown", {})
            self.proc.terminate()


_LSP: dict[str, _LspSession] = {}


@app.post("/lsp/start")
def lsp_start(req: LspStartReq) -> dict[str, Any]:
    sid = secrets.token_hex(8)
    _LSP[sid] = _LspSession(req.language, req.path)
    return {"server_id": sid}


@app.post("/lsp/call")
def lsp_call(req: LspCallReq) -> dict[str, Any]:
    session = _LSP.get(req.server_id)
    if session is None:
        return {"result": None, "error": "unknown server_id"}
    op = getattr(session, req.op, None)
    if op is None:
        return {"result": None, "error": f"unknown op {req.op!r}"}
    return {"result": op(**req.params)}


@app.post("/lsp/stop")
def lsp_stop(req: LspStopReq) -> dict[str, Any]:
    session = _LSP.pop(req.server_id, None)
    if session is not None:
        session.stop()
    return {"ok": session is not None}


# -- interactive PTY -------------------------------------------------------
# A real pseudo-terminal: spawn a shell attached to a pty, stream its merged
# output into a growing buffer, feed stdin, and resize the window via ioctl.
# Sessions persist across HTTP calls in ``_PTYS``, keyed by the child pid.


class _PtySession:
    """A shell running under a pseudo-terminal, with a buffered output stream."""

    def __init__(
        self, rows: int, cols: int, cwd: str | None, envs: dict[str, str], cmd: str | None
    ) -> None:
        self.master_fd, slave_fd = ptylib.openpty()
        env = {**os.environ, **{str(k): str(v) for k, v in envs.items()}}
        argv = ["/bin/bash", "-lc", cmd] if cmd else ["/bin/bash", "-l"]
        self.proc = subprocess.Popen(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd or None,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave_fd)
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._eof = False
        self.resize(rows, cols)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while True:
            try:
                data = os.read(self.master_fd, 65536)
            except OSError:
                break
            if not data:
                break
            with self._lock:
                self._buf += data
        self._eof = True

    def read_from(self, offset: int) -> tuple[bytes, bool, int | None]:
        with self._lock:
            data = bytes(self._buf[offset:])
        rc = self.proc.poll()
        exited = self._eof and rc is not None
        return data, exited, (rc if exited else None)

    def write(self, data: bytes) -> None:
        os.write(self.master_fd, data)

    def resize(self, rows: int, cols: int) -> None:
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def kill(self) -> None:
        with contextlib.suppress(Exception):
            self.proc.terminate()
        with contextlib.suppress(Exception):
            os.close(self.master_fd)


_PTYS: dict[int, _PtySession] = {}


@app.post("/pty/start")
def pty_start(req: PtyStartReq) -> dict[str, Any]:
    session = _PtySession(req.rows, req.cols, req.cwd, req.envs, req.cmd)
    _PTYS[session.proc.pid] = session
    return {"pid": session.proc.pid}


@app.post("/pty/poll")
def pty_poll(req: PtyPollReq) -> dict[str, Any]:
    session = _PTYS.get(req.pid)
    if session is None:
        return {"data": "", "exited": True, "exit_code": None}
    data, exited, code = session.read_from(req.offset)
    return {"data": base64.b64encode(data).decode(), "exited": exited, "exit_code": code}


@app.post("/pty/stdin")
def pty_stdin(req: PtyStdinReq) -> dict[str, Any]:
    session = _PTYS.get(req.pid)
    if session is not None:
        session.write(base64.b64decode(req.data) if req.data else b"")
    return {"ok": session is not None}


@app.post("/pty/resize")
def pty_resize(req: PtyResizeReq) -> dict[str, Any]:
    session = _PTYS.get(req.pid)
    if session is not None:
        session.resize(req.rows, req.cols)
    return {"ok": session is not None}


@app.post("/pty/kill")
def pty_kill(req: PidReq) -> dict[str, Any]:
    session = _PTYS.pop(req.pid, None)
    if session is not None:
        session.kill()
    return {"ok": session is not None}


# -- computer use (desktop automation) ------------------------------------
# Drives a virtual X display with xdotool (input) and scrot or ImageMagick
# `import` (capture). The
# desktop stack (Xvfb + a window manager) must be present in the image; ``start``
# launches Xvfb on DISPLAY if it is not already running. All ops are best-effort
# and return structured results; the dotted action name selects the operation.


class _ComputerUse:
    DISPLAY = ":99"

    def __init__(self) -> None:
        self._xvfb: subprocess.Popen | None = None

    def _env(self) -> dict[str, str]:
        return {**os.environ, "DISPLAY": self.DISPLAY}

    def _run(self, *args: str) -> str:
        proc = subprocess.run(
            list(args), capture_output=True, text=True, env=self._env(), check=False, timeout=30
        )
        return proc.stdout.strip()

    def start(self) -> dict[str, Any]:
        if self._xvfb is None or self._xvfb.poll() is not None:
            self._xvfb = subprocess.Popen(
                ["Xvfb", self.DISPLAY, "-screen", "0", "1280x800x24"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1.0)
        return {"running": True, "display": self.DISPLAY}

    def stop(self) -> dict[str, Any]:
        if self._xvfb is not None:
            with contextlib.suppress(Exception):
                self._xvfb.terminate()
            self._xvfb = None
        return {"running": False}

    def status(self) -> dict[str, Any]:
        running = self._xvfb is not None and self._xvfb.poll() is None
        return {"running": running, "display": self.DISPLAY}

    def screenshot(self, params: dict) -> dict[str, Any]:
        out = Path("/tmp/raitbox-shot.png")  # noqa: S108 - ephemeral capture path
        # Prefer scrot; fall back to ImageMagick `import` (scrot is not packaged
        # for every distro/arch, e.g. AL2023 aarch64, where ImageMagick is).
        if shutil.which("scrot"):
            self._run("scrot", "-o", str(out))
        elif shutil.which("import"):
            self._run("import", "-window", "root", str(out))
        else:
            return {"png": "", "error": "no screenshot tool (install scrot or ImageMagick)"}
        data = out.read_bytes() if out.exists() else b""
        return {"png": base64.b64encode(data).decode()}

    def dispatch(self, action: str, params: dict) -> Any:
        if action == "start":
            return self.start()
        if action == "stop":
            return self.stop()
        if action == "status":
            return self.status()
        if action in ("screenshot", "screenshot.take"):
            return self.screenshot(params)
        if action == "mouse.move":
            self._run("xdotool", "mousemove", str(params["x"]), str(params["y"]))
            return {"x": params["x"], "y": params["y"]}
        if action == "mouse.click":
            if "x" in params and "y" in params:
                self._run("xdotool", "mousemove", str(params["x"]), str(params["y"]))
            button = str(params.get("button", 1))
            repeat = "2" if params.get("double") else "1"
            self._run("xdotool", "click", "--repeat", repeat, button)
            return {"ok": True}
        if action == "mouse.scroll":
            button = "4" if params.get("direction", "up") == "up" else "5"
            for _ in range(int(params.get("amount", 1))):
                self._run("xdotool", "click", button)
            return {"ok": True}
        if action == "mouse.position":
            loc = self._run("xdotool", "getmouselocation", "--shell")
            vals = dict(line.split("=", 1) for line in loc.splitlines() if "=" in line)
            return {"x": int(vals.get("X", 0)), "y": int(vals.get("Y", 0))}
        if action == "keyboard.type":
            self._run("xdotool", "type", "--", str(params.get("text", "")))
            return {"ok": True}
        if action == "keyboard.press":
            keys = "+".join([*params.get("modifiers", []), params["key"]])
            self._run("xdotool", "key", keys)
            return {"ok": True}
        if action == "display.info":
            size = self._run("xdotool", "getdisplaygeometry")
            w, _, h = size.partition(" ")
            return {"width": int(w or 0), "height": int(h or 0), "display": self.DISPLAY}
        raise ValueError(f"unknown computer-use action {action!r}")


_COMPUTER = _ComputerUse()


@app.post("/computer")
def computer(req: ComputerReq) -> dict[str, Any]:
    return {"result": _COMPUTER.dispatch(req.action, req.params)}


# -- Lambda MicroVM lifecycle hooks ---------------------------------------


@app.post(f"{HOOK_PREFIX}/ready")
def hook_ready() -> dict[str, Any]:
    # 200 = safe to snapshot. Everything the app needs is already imported.
    return {"status": "ready"}


@app.post(f"{HOOK_PREFIX}/validate")
def hook_validate() -> dict[str, Any]:
    return {"status": "ok"}


def _mount_volume(m: dict) -> None:
    """Best-effort EFS mount inside the VM (needs a VPC egress connector + nfs)."""
    path = m.get("mount_path")
    fs = m.get("file_system_id")
    if not path or not fs:
        return
    src = f"{fs}.efs.{m.get('region') or ''}.amazonaws.com:/{(m.get('subpath') or '').lstrip('/')}"
    Path(path).mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["mount", "-t", "nfs4", "-o", "nfsvers=4.1", src, path],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@app.post(f"{HOOK_PREFIX}/run")
def hook_run(req: RunHookReq) -> dict[str, Any]:
    # Snapshot safety: derive unique identity/entropy HERE, not at build time.
    _STATE["microvm_id"] = req.microvmId
    _STATE["instance_secret"] = secrets.token_hex(32)
    if req.runHookPayload:
        try:
            payload = json.loads(req.runHookPayload)
        except ValueError:
            payload = {}
        for k, v in (payload.get("envs") or {}).items():
            os.environ[str(k)] = str(v)
        for m in payload.get("mounts") or []:
            with contextlib.suppress(Exception):  # mounting is best-effort
                _mount_volume(m)
    return {"status": "running"}


@app.post(f"{HOOK_PREFIX}/suspend")
def hook_suspend() -> dict[str, Any]:
    return {"status": "suspended"}


@app.post(f"{HOOK_PREFIX}/resume")
def hook_resume() -> dict[str, Any]:
    # Re-establish any custom network connections here if added later.
    return {"status": "resumed"}


@app.post(f"{HOOK_PREFIX}/terminate")
def hook_terminate() -> dict[str, Any]:
    return {"status": "terminated"}
