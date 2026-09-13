"""``CommandHandle`` — E2B-compatible handle to a background command.

Returned by ``sandbox.commands.run(cmd, background=True)``. It talks to its
backend through a small polling protocol (``command_poll``/``command_kill``/
``command_stdin``), so any backend that tracks processes (the ``microvm`` agent
server does) gets real streaming, ``wait``, ``kill``, and stdin.
"""

from __future__ import annotations

import base64
import shlex
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, Protocol

from .errors import CommandExitException, GitAuthException, GitUpstreamException
from .types import (
    CommandResult,
    CompletionItem,
    EntryInfo,
    FilesystemEvent,
    LspSymbol,
    Position,
    PtySize,
    SandboxInfo,
)

_POLL_INTERVAL = 0.1


class _CommandController(Protocol):
    """The subset of a backend a :class:`CommandHandle` drives."""

    def command_poll(self, pid: int, stdout_off: int, stderr_off: int) -> dict[str, Any]: ...
    def command_kill(self, pid: int) -> bool: ...
    def command_stdin(self, pid: int, data: str | bytes) -> None: ...


class CommandHandle:
    """Handle to a background process (mirrors e2b ``CommandHandle``)."""

    def __init__(self, controller: _CommandController, pid: int, cmd: str) -> None:
        self._c = controller
        self.pid = pid
        self._cmd = cmd
        self._out_off = 0
        self._err_off = 0
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._exit_code: int | None = None
        self._disconnected = False

    # -- incremental drain -------------------------------------------------

    def _drain(self) -> tuple[str, str]:
        """Fetch output produced since the last poll; track exit status."""
        poll = self._c.command_poll(self.pid, self._out_off, self._err_off)
        out = poll.get("stdout", "") or ""
        err = poll.get("stderr", "") or ""
        if out:
            self._stdout.append(out)
            self._out_off += len(out)
        if err:
            self._stderr.append(err)
            self._err_off += len(err)
        if poll.get("exited"):
            self._exit_code = poll.get("exit_code", 0)
        return out, err

    # -- E2B surface -------------------------------------------------------

    def __iter__(self) -> Iterator[str]:
        """Yield stdout/stderr chunks as they arrive until the process exits."""
        while not self._disconnected:
            out, err = self._drain()
            if out:
                yield out
            if err:
                yield err
            if self._exit_code is not None:
                return
            if not out and not err:
                time.sleep(_POLL_INTERVAL)

    def wait(
        self,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> CommandResult:
        """Block until the command exits, returning a ``CommandResult``.

        Raises :class:`CommandExitException` on a non-zero exit, matching E2B.
        """
        while self._exit_code is None and not self._disconnected:
            out, err = self._drain()
            if out and on_stdout:
                on_stdout(out)
            if err and on_stderr:
                on_stderr(err)
            if self._exit_code is None and not out and not err:
                time.sleep(_POLL_INTERVAL)
        result = CommandResult(
            stdout="".join(self._stdout),
            stderr="".join(self._stderr),
            exit_code=self._exit_code or 0,
        )
        if result.exit_code != 0:
            raise CommandExitException(
                stdout=result.stdout, stderr=result.stderr, exit_code=result.exit_code
            )
        return result

    def kill(self) -> bool:
        return self._c.command_kill(self.pid)

    def send_stdin(self, data: str | bytes) -> None:
        self._c.command_stdin(self.pid, data)

    def disconnect(self) -> None:
        self._disconnected = True


class _PtyController(Protocol):
    """The subset of a backend a :class:`PtyHandle` drives."""

    def pty_poll(self, pid: int, offset: int) -> dict[str, Any]: ...
    def pty_stdin(self, pid: int, data: bytes) -> None: ...
    def pty_resize(self, pid: int, rows: int, cols: int) -> None: ...
    def pty_kill(self, pid: int) -> bool: ...


class PtyHandle:
    """Handle to an interactive pseudo-terminal (mirrors e2b ``pty.create``).

    Output is a single merged byte stream (a real TTY, not split stdout/stderr).
    Pass ``on_data`` to :meth:`Pty.create` and it is called with each chunk as it
    arrives (from a background poll thread); otherwise drain synchronously with
    :meth:`wait`. Feed input with :meth:`send_stdin`, adjust the window with
    :meth:`resize`, and stop it with :meth:`kill`.
    """

    def __init__(
        self,
        controller: _PtyController,
        pid: int,
        on_data: Callable[[bytes], None] | None = None,
    ) -> None:
        self._c = controller
        self.pid = pid
        self._on_data = on_data
        self._off = 0
        self._exit_code: int | None = None
        self._data = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if on_data is not None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _poll_once(self) -> bytes:
        poll = self._c.pty_poll(self.pid, self._off)
        raw = poll.get("data") or ""
        chunk = base64.b64decode(raw) if raw else b""
        if chunk:
            with self._lock:
                self._data += chunk
            self._off += len(chunk)
            if self._on_data is not None:
                self._on_data(chunk)
        if poll.get("exited"):
            self._exit_code = poll.get("exit_code") or 0
        return chunk

    def _loop(self) -> None:
        while not self._stop.is_set() and self._exit_code is None:
            if not self._poll_once():
                time.sleep(_POLL_INTERVAL)

    def wait(self) -> int:
        """Block until the terminal process exits; return its exit code."""
        if self._thread is not None:
            self._thread.join()
        else:
            while self._exit_code is None and not self._stop.is_set():
                if not self._poll_once():
                    time.sleep(_POLL_INTERVAL)
        return self._exit_code or 0

    def kill(self) -> bool:
        self._stop.set()
        return self._c.pty_kill(self.pid)

    def send_stdin(self, data: bytes) -> None:
        self._c.pty_stdin(self.pid, data)

    def resize(self, size: PtySize) -> None:
        self._c.pty_resize(self.pid, size.rows, size.cols)

    @property
    def data(self) -> bytes:
        """All terminal output seen so far."""
        with self._lock:
            return bytes(self._data)


class WatchHandle:
    """Handle to a directory watch (mirrors e2b ``WatchHandle``).

    RaitBox implements watching generically by polling ``files.list`` in a
    background thread and diffing entries, so it works on every backend. It
    detects ``create`` and ``remove`` events (content-change/chmod are not
    distinguished without server-side inotify).
    """

    def __init__(
        self,
        list_fn: Callable[[str], list[EntryInfo]],
        path: str,
        interval: float = 1.0,
    ) -> None:
        self._list_fn = list_fn
        self._path = path
        self._interval = interval
        self._events: list[FilesystemEvent] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._known = {e.name for e in self._safe_list()}
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _safe_list(self) -> list[EntryInfo]:
        try:
            return self._list_fn(self._path)
        except Exception:  # noqa: BLE001 - watching is best-effort
            return []

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            current = {e.name for e in self._safe_list()}
            created = current - self._known
            removed = self._known - current
            if created or removed:
                with self._lock:
                    self._events += [FilesystemEvent(n, "create") for n in sorted(created)]
                    self._events += [FilesystemEvent(n, "remove") for n in sorted(removed)]
                self._known = current

    def get_new_events(self) -> list[FilesystemEvent]:
        """Return (and clear) events observed since the last call."""
        with self._lock:
            events, self._events = self._events, []
        return events

    def stop(self) -> None:
        self._stop.set()


class SandboxPaginator:
    """Paginator returned by ``Sandbox.list`` (mirrors e2b ``SandboxPaginator``).

    RaitBox backends return all matches at once, so this wraps them into pages
    consumed via ``while p.has_next: p.next_items()``; it is also directly
    iterable.
    """

    def __init__(self, items: list[SandboxInfo], page_size: int | None = None) -> None:
        self._items = list(items)
        self._pos = 0
        self._page = page_size or max(len(self._items), 1)

    @property
    def has_next(self) -> bool:
        return self._pos < len(self._items)

    @property
    def next_token(self) -> str | None:
        return None

    def next_items(self) -> list[SandboxInfo]:
        chunk = self._items[self._pos : self._pos + self._page]
        self._pos += len(chunk)
        return chunk

    def __iter__(self) -> Iterator[SandboxInfo]:
        return iter(self._items)


class _LspController(Protocol):
    """The subset of a backend an :class:`LspServer` drives."""

    def lsp_start(self, language: str, path: str) -> str: ...
    def lsp_call(self, server_id: str, op: str, params: dict[str, Any]) -> Any: ...
    def lsp_stop(self, server_id: str) -> None: ...


class LspServer:
    """A language server scoped to a project (LSP over the sandbox).

    Created by ``sandbox.lsp.create_server(language, project_root)`` and backed
    by a real language-server process inside the sandbox (``pylsp`` for Python,
    ``typescript-language-server`` for JS/TS). Open files with :meth:`did_open`,
    then query :meth:`document_symbols` / :meth:`completions`. Call :meth:`stop`
    when done (or use it as a context manager).
    """

    def __init__(self, controller: _LspController, language: str, project_root: str) -> None:
        self._c = controller
        self.language = language
        self.project_root = project_root
        self._server_id: str | None = None

    def start(self) -> LspServer:
        """Spawn the language server and run the LSP initialize handshake."""
        if self._server_id is None:
            self._server_id = self._c.lsp_start(self.language, self.project_root)
        return self

    def stop(self) -> None:
        if self._server_id is not None:
            self._c.lsp_stop(self._server_id)
            self._server_id = None

    def _call(self, op: str, **params: Any) -> Any:
        if self._server_id is None:
            self.start()
        return self._c.lsp_call(self._server_id, op, params)  # type: ignore[arg-type]

    def did_open(self, path: str) -> None:
        """Notify the server a file is open (required before querying it)."""
        self._call("did_open", path=path)

    def did_close(self, path: str) -> None:
        self._call("did_close", path=path)

    def document_symbols(self, path: str) -> list[LspSymbol]:
        """Symbols (functions, classes, …) declared in ``path``."""
        raw = self._call("document_symbols", path=path) or []
        return [
            LspSymbol(name=s.get("name", ""), kind=s.get("kind", 0), location=s.get("location", s))
            for s in raw
        ]

    def workspace_symbols(self, query: str) -> list[LspSymbol]:
        """Symbols matching ``query`` across the project."""
        raw = self._call("workspace_symbols", query=query) or []
        return [
            LspSymbol(name=s.get("name", ""), kind=s.get("kind", 0), location=s.get("location", s))
            for s in raw
        ]

    def completions(self, path: str, position: Position | tuple[int, int]) -> list[CompletionItem]:
        """Completion candidates at ``position`` (a :class:`Position` or ``(line, char)``)."""
        line, char = (
            (position.line, position.character) if isinstance(position, Position) else position
        )
        raw = self._call("completions", path=path, line=line, character=char) or []
        return [
            CompletionItem(label=c.get("label", ""), kind=c.get("kind"), detail=c.get("detail"))
            for c in raw
        ]

    def __enter__(self) -> LspServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


class Git:
    """Best-effort ``sandbox.git`` namespace, implemented over the shell.

    Each method runs ``git`` inside the sandbox and raises
    :class:`GitUpstreamException` (or :class:`GitAuthException` for auth
    failures) on a non-zero exit, mirroring E2B's git error surface.
    """

    def __init__(self, runner: Any) -> None:
        # ``runner`` is a Backend (exposes ``run_command``).
        self._r = runner

    def _git(self, args: str, cwd: str | None = None) -> CommandResult:
        result = self._r.run_command(f"git {args}", cwd=cwd)
        if result.exit_code != 0:
            blob = (result.stderr or "") + (result.stdout or "")
            low = blob.lower()
            if "authentication" in low or "permission denied" in low or "could not read" in low:
                raise GitAuthException(blob.strip() or "git authentication failed")
            raise GitUpstreamException(blob.strip() or f"git {args} failed")
        return result

    def clone(self, url: str, path: str | None = None, branch: str | None = None) -> CommandResult:
        args = "clone"
        if branch:
            args += f" --branch {shlex.quote(branch)}"
        args += f" {shlex.quote(url)}"
        if path:
            args += f" {shlex.quote(path)}"
        return self._git(args)

    def checkout(self, ref: str, cwd: str | None = None) -> CommandResult:
        return self._git(f"checkout {shlex.quote(ref)}", cwd=cwd)

    def add(self, path: str = ".", cwd: str | None = None) -> CommandResult:
        return self._git(f"add {shlex.quote(path)}", cwd=cwd)

    def commit(self, message: str, cwd: str | None = None) -> CommandResult:
        return self._git(f"commit -m {shlex.quote(message)}", cwd=cwd)

    def pull(self, cwd: str | None = None) -> CommandResult:
        return self._git("pull", cwd=cwd)

    def push(self, cwd: str | None = None) -> CommandResult:
        return self._git("push", cwd=cwd)

    def status(self, cwd: str | None = None) -> CommandResult:
        return self._git("status --porcelain", cwd=cwd)

    def branch(self, cwd: str | None = None) -> CommandResult:
        return self._git("branch --show-current", cwd=cwd)
