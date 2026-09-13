"""E2B-compatible ``AsyncSandbox``.

boto3 is synchronous, so async methods offload to threads with
``asyncio.to_thread`` — this preserves E2B's ``async with
AsyncSandbox.create() as sbx:`` usage without an aioboto3 dependency.
Concurrency across sandboxes works (each call runs in its own thread).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from ..core.types import EntryInfo, Execution, SandboxInfo, WriteInfo
from .sync import Sandbox

# ``.list()`` methods below shadow the builtin ``list`` in annotations; alias it.
_list = list


class AsyncFilesystem:
    def __init__(self, files) -> None:
        self._files = files

    async def read(self, path: str, format: str = "text", **kw: Any) -> str | bytearray:
        return await asyncio.to_thread(self._files.read, path, format)

    async def write(self, path: str, data: str | bytes, **kw: Any) -> WriteInfo:
        return await asyncio.to_thread(self._files.write, path, data)

    async def list(self, path: str = "", depth: int = 1, **kw: Any) -> _list[EntryInfo]:
        return await asyncio.to_thread(self._files.list, path, depth)

    async def exists(self, path: str, **kw: Any) -> bool:
        return await asyncio.to_thread(self._files.exists, path)

    async def get_info(self, path: str, **kw: Any) -> EntryInfo:
        return await asyncio.to_thread(self._files.get_info, path)

    async def remove(self, path: str, **kw: Any) -> None:
        return await asyncio.to_thread(self._files.remove, path)

    async def rename(self, old: str, new: str, **kw: Any) -> EntryInfo:
        return await asyncio.to_thread(self._files.rename, old, new)

    async def make_dir(self, path: str, **kw: Any) -> bool:
        return await asyncio.to_thread(self._files.make_dir, path)

    async def write_files(self, files: _list[dict], **kw: Any):
        return await asyncio.to_thread(self._files.write_files, files)

    def watch_dir(self, path: str, **kw: Any):
        # WatchHandle already polls on a background thread; no offload needed.
        return self._files.watch_dir(path)


class AsyncGit:
    def __init__(self, git) -> None:
        self._git = git

    async def clone(self, url: str, path: str | None = None, branch: str | None = None):
        return await asyncio.to_thread(self._git.clone, url, path, branch)

    async def checkout(self, ref: str, cwd: str | None = None):
        return await asyncio.to_thread(self._git.checkout, ref, cwd)

    async def add(self, path: str = ".", cwd: str | None = None):
        return await asyncio.to_thread(self._git.add, path, cwd)

    async def commit(self, message: str, cwd: str | None = None):
        return await asyncio.to_thread(self._git.commit, message, cwd)

    async def pull(self, cwd: str | None = None):
        return await asyncio.to_thread(self._git.pull, cwd)

    async def push(self, cwd: str | None = None):
        return await asyncio.to_thread(self._git.push, cwd)

    async def status(self, cwd: str | None = None):
        return await asyncio.to_thread(self._git.status, cwd)

    async def branch(self, cwd: str | None = None):
        return await asyncio.to_thread(self._git.branch, cwd)


class AsyncLspServer:
    """Async wrapper over :class:`~raitbox.core.handles.LspServer`."""

    def __init__(self, server) -> None:
        self._s = server

    @property
    def language(self) -> str:
        return self._s.language

    async def start(self) -> AsyncLspServer:
        await asyncio.to_thread(self._s.start)
        return self

    async def stop(self) -> None:
        return await asyncio.to_thread(self._s.stop)

    async def did_open(self, path: str) -> None:
        return await asyncio.to_thread(self._s.did_open, path)

    async def did_close(self, path: str) -> None:
        return await asyncio.to_thread(self._s.did_close, path)

    async def document_symbols(self, path: str):
        return await asyncio.to_thread(self._s.document_symbols, path)

    async def workspace_symbols(self, query: str):
        return await asyncio.to_thread(self._s.workspace_symbols, query)

    async def completions(self, path: str, position):
        return await asyncio.to_thread(self._s.completions, path, position)

    async def __aenter__(self) -> AsyncLspServer:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()


class AsyncLsp:
    def __init__(self, lsp) -> None:
        self._lsp = lsp

    def create_server(self, language: str, project_root: str = "/", **kw: Any) -> AsyncLspServer:
        return AsyncLspServer(self._lsp.create_server(language, project_root))


class _AsyncNS:
    """Offloads every public method of a sync namespace to a thread."""

    def __init__(self, ns) -> None:
        self.__ns = ns

    def __getattr__(self, name: str):
        attr = getattr(self.__ns, name)
        if not callable(attr):
            return attr

        async def _call(*a: Any, **k: Any):
            return await asyncio.to_thread(lambda: attr(*a, **k))

        return _call


class AsyncComputerUse:
    """Async wrapper over ``sandbox.computer_use`` (microvm only)."""

    def __init__(self, cu) -> None:
        self._cu = cu
        self.mouse = _AsyncNS(cu.mouse)
        self.keyboard = _AsyncNS(cu.keyboard)
        self.screenshot = _AsyncNS(cu.screenshot)
        self.display = _AsyncNS(cu.display)

    async def start(self, **k: Any):
        return await asyncio.to_thread(self._cu.start)

    async def stop(self, **k: Any):
        return await asyncio.to_thread(self._cu.stop)

    async def get_status(self, **k: Any):
        return await asyncio.to_thread(self._cu.get_status)


class AsyncPty:
    """Async wrapper over ``sandbox.pty`` (microvm only)."""

    def __init__(self, pty) -> None:
        self._pty = pty

    async def create(self, size, on_data=None, **kw: Any):
        # Returns a PtyHandle; its on_data streaming runs on its own thread.
        return await asyncio.to_thread(lambda: self._pty.create(size, on_data, **kw))

    async def send_stdin(self, pid: int, data: bytes, **kw: Any) -> None:
        return await asyncio.to_thread(self._pty.send_stdin, pid, data)

    async def resize(self, pid: int, size, **kw: Any) -> None:
        return await asyncio.to_thread(self._pty.resize, pid, size)

    async def kill(self, pid: int, **kw: Any) -> bool:
        return await asyncio.to_thread(self._pty.kill, pid)


class AsyncCommands:
    def __init__(self, commands) -> None:
        self._commands = commands

    async def run(self, cmd: str, **kwargs: Any):
        return await asyncio.to_thread(lambda: self._commands.run(cmd, **kwargs))

    async def list(self, **kwargs: Any):
        return await asyncio.to_thread(self._commands.list)

    async def kill(self, pid: int, **kwargs: Any) -> bool:
        return await asyncio.to_thread(self._commands.kill, pid)

    async def send_stdin(self, pid: int, data: str | bytes, **kwargs: Any) -> None:
        return await asyncio.to_thread(self._commands.send_stdin, pid, data)

    def connect(self, pid: int, **kwargs: Any):
        return self._commands.connect(pid)


class AsyncSandbox:
    """Async facade over :class:`Sandbox`. Build with ``await AsyncSandbox.create()``."""

    def __init__(self, _sandbox: Sandbox) -> None:
        self._sbx = _sandbox
        self.files = AsyncFilesystem(_sandbox.files)
        self.commands = AsyncCommands(_sandbox.commands)
        self.git = AsyncGit(_sandbox.git)
        self.lsp = AsyncLsp(_sandbox.lsp)
        self.pty = AsyncPty(_sandbox.pty)
        self.computer_use = AsyncComputerUse(_sandbox.computer_use)

    @property
    def sandbox_id(self) -> str:
        return self._sbx.sandbox_id

    @classmethod
    async def create(cls, template: str | None = None, **kwargs: Any) -> AsyncSandbox:
        sbx = await asyncio.to_thread(lambda: Sandbox.create(template=template, **kwargs))
        return cls(sbx)

    @classmethod
    async def connect(cls, sandbox_id: str, **kwargs: Any) -> AsyncSandbox:
        sbx = await asyncio.to_thread(lambda: Sandbox.connect(sandbox_id, **kwargs))
        return cls(sbx)

    async def run_code(
        self,
        code: str,
        language: str | None = None,
        context: Any = None,
        envs: dict[str, str] | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> Execution:
        return await asyncio.to_thread(
            lambda: self._sbx.run_code(
                code,
                language=language,
                context=context,
                envs=envs,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
        )

    async def create_code_context(self, cwd: str | None = None, language: str = "python"):
        return await asyncio.to_thread(self._sbx.create_code_context, cwd, language)

    async def list_code_contexts(self):
        return await asyncio.to_thread(self._sbx.list_code_contexts)

    async def restart_code_context(self, context) -> None:
        return await asyncio.to_thread(self._sbx.restart_code_context, context)

    async def remove_code_context(self, context) -> None:
        return await asyncio.to_thread(self._sbx.remove_code_context, context)

    @staticmethod
    def list(
        query: Any = None, limit: int | None = None, next_token: str | None = None, **kwargs: Any
    ):
        """Mirrors e2b: returns a paginator synchronously (not awaited)."""
        return Sandbox.list(query, limit, next_token, **kwargs)

    async def get_metrics(self, start: Any = None, end: Any = None, **kwargs: Any):
        return await asyncio.to_thread(self._sbx.get_metrics, start, end)

    async def fork(self, **kwargs: Any) -> Any:
        # Raises NotSupportedError on the sync side (no AWS equivalent).
        return await asyncio.to_thread(lambda: self._sbx.fork(**kwargs))

    async def create_snapshot(self, **kwargs: Any) -> str:
        return await asyncio.to_thread(lambda: self._sbx.create_snapshot(**kwargs))

    async def pause(self, bucket: str | None = None) -> str:
        return await asyncio.to_thread(self._sbx.pause, bucket)

    @classmethod
    async def resume(
        cls, sandbox_id: str, bucket: str | None = None, **kwargs: Any
    ) -> AsyncSandbox:
        sbx = await asyncio.to_thread(lambda: Sandbox.resume(sandbox_id, bucket, **kwargs))
        return cls(sbx)

    def open_shell(self, *, session_id: str | None = None, shell_id: str | None = None):
        """Open an interactive shell (``agentcore-runtime``). Async context manager."""
        return self._sbx.open_shell(session_id=session_id, shell_id=shell_id)

    async def kill(self) -> bool:
        return await asyncio.to_thread(self._sbx.kill)

    async def set_timeout(self, timeout: int) -> None:
        return await asyncio.to_thread(self._sbx.set_timeout, timeout)

    async def is_running(self) -> bool:
        return await asyncio.to_thread(self._sbx.is_running)

    async def get_info(self) -> SandboxInfo:
        return await asyncio.to_thread(self._sbx.get_info)

    def get_host(self, port: int) -> str:
        return self._sbx.get_host(port)

    async def get_preview_link(self, port: int, *, expires_in_minutes: int = 30):
        return await asyncio.to_thread(
            lambda: self._sbx.get_preview_link(port, expires_in_minutes=expires_in_minutes)
        )

    async def get_terminal_link(
        self,
        port: int = 7681,
        *,
        command: str | None = "ttyd -p {port} -W bash",
        expires_in_minutes: int = 30,
    ):
        return await asyncio.to_thread(
            lambda: self._sbx.get_terminal_link(
                port, command=command, expires_in_minutes=expires_in_minutes
            )
        )

    async def __aenter__(self) -> AsyncSandbox:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.kill()
