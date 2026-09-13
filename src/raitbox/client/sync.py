"""E2B-compatible synchronous ``Sandbox`` facade.

Delegates to a pluggable :class:`~raitbox.backends.base.Backend`. The public
surface (methods, kwargs, return types) matches ``e2b`` / ``e2b_code_interpreter``
so existing E2B code runs by changing only the import.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..backends import get_backend
from ..backends.base import Backend
from ..core import events as events_mod
from ..core import session_pool as registry
from ..core.errors import CommandExitException, NotSupportedError
from ..core.handles import (
    CommandHandle,
    Git,
    LspServer,
    PtyHandle,
    SandboxPaginator,
    WatchHandle,
)
from ..core.observability import span
from ..core.settings import resolve_backend
from ..core.types import (
    CommandResult,
    Context,
    EntryInfo,
    Execution,
    PreviewLink,
    ProcessInfo,
    PtySize,
    SandboxInfo,
    Volume,
    VolumeMount,
    WriteInfo,
)

DEFAULT_TIMEOUT = 300  # seconds; mirrors E2B's default sandbox timeout

# The E2B API defines ``.list()`` methods on Sandbox/files/commands, which would
# shadow the builtin ``list`` in return annotations; alias it to keep both.
_list = list


class Sandbox:
    """E2B-compatible sandbox.

    Usage identical to ``e2b_code_interpreter``::

        with Sandbox() as sbx:
            print(sbx.run_code("print('hi')").logs.stdout)

    Args:
        template: Backend-specific template/image identifier.
        timeout: Sandbox idle timeout in seconds.
        metadata: Arbitrary key/value labels.
        envs: Environment variables exported into the sandbox.
        api_key: Accepted for E2B signature compatibility and IGNORED
            (RaitBox authenticates via the AWS credential chain).
        backend: One of ``agentcore-ci`` (default), ``agentcore-runtime``,
            ``microvm``; overrides ``RAITBOX_BACKEND``.
        region_name / profile_name: Passed through to the boto3 session.
    """

    def __init__(
        self,
        template: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        metadata: dict[str, str] | None = None,
        envs: dict[str, str] | None = None,
        api_key: str | None = None,  # ignored, E2B compat
        *,
        backend: str | None = None,
        region_name: str | None = None,
        profile_name: str | None = None,
        secrets: dict[str, str] | None = None,
        volume_mounts: list[VolumeMount] | None = None,
        _backend: Backend | None = None,
        **kwargs: Any,
    ) -> None:
        if _backend is not None:
            self._backend = _backend
        else:
            if secrets:
                # Resolve secret references and merge into envs (envs win on clash).
                from ..core.secrets import resolve_secrets

                resolved_secrets = resolve_secrets(secrets, region_name, profile_name)
                envs = {**resolved_secrets, **(envs or {})}
            backend_name = resolve_backend(backend)
            if volume_mounts and backend_name != "microvm":
                raise NotSupportedError(
                    f"{backend_name}: volume_mounts require the microvm backend (EFS)."
                )
            backend_cls = get_backend(backend_name)
            with span("raitbox.sandbox.create", **{"raitbox.backend": backend_name}):
                self._backend = backend_cls.open(
                    template=template,
                    timeout=timeout,
                    envs=envs,
                    metadata=metadata,
                    region_name=region_name,
                    profile_name=profile_name,
                    volume_mounts=volume_mounts,
                    **kwargs,
                )
        self.sandbox_id = self._backend.sandbox_id
        registry.register(self.sandbox_id, self._safe_kill)
        self.files = Filesystem(self._backend)
        self.commands = Commands(self._backend)
        self.pty = Pty(self._backend)
        self.git = Git(self._backend)
        self.lsp = Lsp(self._backend)
        self.computer_use = ComputerUse(self._backend)
        self._contexts: dict[str, Context] = {}
        events_mod.emit(
            "sandbox.created",
            {"sandbox_id": self.sandbox_id, "backend": self._backend.name},
        )

    # -- construction ------------------------------------------------------

    @classmethod
    def create(cls, template: str | None = None, **kwargs: Any) -> Sandbox:
        """E2B v2 constructor: ``Sandbox.create(...)``."""
        return cls(template=template, **kwargs)

    @classmethod
    def connect(
        cls,
        sandbox_id: str,
        *,
        backend: str | None = None,
        region_name: str | None = None,
        profile_name: str | None = None,
        **kwargs: Any,
    ) -> Sandbox:
        """Reconnect to an existing sandbox by id."""
        backend_cls = get_backend(resolve_backend(backend))
        b = backend_cls.attach(
            sandbox_id,
            region_name=region_name,
            profile_name=profile_name,
            **kwargs,
        )
        return cls(_backend=b)

    @staticmethod
    def list(
        query: Any = None,
        limit: int | None = None,
        next_token: str | None = None,
        *,
        backend: str | None = None,
        region_name: str | None = None,
        **kwargs: Any,
    ) -> SandboxPaginator:
        """List running sandboxes (mirrors e2b ``Sandbox.list`` → paginator).

        Supported on backends that can enumerate sessions (``microvm``); others
        raise ``NotSupportedError``. Returns a :class:`SandboxPaginator`
        (``has_next`` / ``next_items()``) that is also directly iterable.
        """
        backend_cls = get_backend(resolve_backend(backend))
        items = backend_cls.list_running(region_name=region_name, **kwargs)
        return SandboxPaginator(items, page_size=limit)

    # -- volumes (EFS / S3 access points; bring-your-own resource) ---------

    @staticmethod
    def create_volume(
        name: str,
        *,
        kind: str = "efs",
        file_system_id: str | None = None,
        access_point: str | None = None,
        **kwargs: Any,
    ) -> Volume:
        """Register an existing EFS file system / S3 access point as a volume.

        RaitBox mounts existing storage (it does not provision EFS/S3); pass
        ``file_system_id`` (EFS) or ``access_point``. Attach via
        ``Sandbox.create(volume_mounts=[VolumeMount(volume.id, "/data")])``.
        """
        from ..core.errors import InvalidArgumentException

        vid = file_system_id or access_point
        if not vid:
            raise InvalidArgumentException(
                "create_volume needs file_system_id (EFS) or access_point (S3)."
            )
        return Volume(id=vid, name=name, kind=kind)

    @staticmethod
    def list_volumes(
        *,
        region_name: str | None = None,
        profile_name: str | None = None,
        client: Any = None,
        **kwargs: Any,
    ) -> _list[Volume]:
        """List EFS file systems usable as volumes."""
        if client is None:
            import boto3

            client = boto3.Session(profile_name=profile_name, region_name=region_name).client("efs")
        return [
            Volume(id=f["FileSystemId"], name=f.get("Name") or f["FileSystemId"], kind="efs")
            for f in client.describe_file_systems().get("FileSystems", [])
        ]

    # -- lifecycle ---------------------------------------------------------
    # Each of kill/set_timeout/is_running/get_info works BOTH as an instance
    # method (``sbx.kill()``) and, like E2B, as a static method taking an id
    # (``Sandbox.kill("sbx-id")``) — in the static form ``self`` binds to the id.

    def kill(self, *, request_timeout: float | None = None, **kwargs: Any) -> bool:
        """Stop a sandbox. ``sbx.kill()`` or static ``Sandbox.kill(sandbox_id)``."""
        if isinstance(self, str):  # static form: Sandbox.kill("sbx-id")
            return Sandbox.connect(self, **kwargs).kill()
        killed = self._backend.kill()
        registry.unregister(self.sandbox_id)
        events_mod.emit(
            "sandbox.killed",
            {"sandbox_id": self.sandbox_id, "backend": self._backend.name},
        )
        return killed

    def _safe_kill(self) -> None:
        try:
            self._backend.kill()
        finally:
            registry.unregister(self.sandbox_id)

    def set_timeout(self, timeout: int = DEFAULT_TIMEOUT, **kwargs: Any) -> None:
        """``sbx.set_timeout(t)`` or static ``Sandbox.set_timeout(sandbox_id, t)``."""
        if isinstance(self, str):  # static form
            Sandbox.connect(self, **kwargs).set_timeout(timeout)
            return
        self._backend.set_timeout(timeout)

    def is_running(self, **kwargs: Any) -> bool:
        if isinstance(self, str):
            return Sandbox.connect(self, **kwargs).is_running()
        return self._backend.is_running()

    def get_info(self, **kwargs: Any) -> SandboxInfo:
        if isinstance(self, str):
            return Sandbox.connect(self, **kwargs).get_info()
        return self._backend.get_info()

    def get_host(self, port: int) -> str:
        return self._backend.get_host(port)

    def get_preview_link(self, port: int, *, expires_in_minutes: int = 30) -> PreviewLink:
        """A reachable URL + auth headers for a port in the sandbox (microvm)."""
        return self._backend.preview_link(port, expires_in_minutes)

    def get_terminal_link(
        self,
        port: int = 7681,
        *,
        command: str | None = "ttyd -p {port} -W bash",
        expires_in_minutes: int = 30,
    ) -> PreviewLink:
        """A browser-reachable web terminal into the sandbox (microvm; extension).

        Launches a web-terminal server in the background (``command``, with
        ``{port}`` substituted — defaults to ``ttyd``) and returns a
        :class:`PreviewLink` to it. Pass ``command=None`` if a terminal server is
        already running on ``port``. The same pattern exposes any human-facing
        server (e.g. a VNC/noVNC desktop): run it, then ``get_preview_link(port)``.
        """
        if command:
            self.commands.run(command.format(port=port), background=True)
        return self._backend.preview_link(port, expires_in_minutes)

    def get_metrics(self, start: Any = None, end: Any = None, **kwargs: Any) -> _list:
        """Resource metrics (mirrors e2b ``get_metrics``); empty if unsupported."""
        return []

    # -- E2B methods with no AWS equivalent (present so callers get a clear
    #    NotSupportedError instead of AttributeError) ------------------------

    def fork(self, **kwargs: Any) -> Sandbox:
        raise NotSupportedError(
            "fork() has no AWS equivalent; use pause()/resume() to snapshot files."
        )

    def update_network(self, **kwargs: Any) -> None:
        raise NotSupportedError("update_network() is not supported")

    def get_mcp_token(self, **kwargs: Any) -> str:
        raise NotSupportedError("get_mcp_token() is not supported")

    def beta_pause(self, **kwargs: Any) -> str:
        return self.pause(**kwargs)

    def create_snapshot(self, **kwargs: Any) -> str:
        raise NotSupportedError("create_snapshot() is not supported; use pause().")

    @staticmethod
    def list_snapshots(**kwargs: Any) -> _list:
        raise NotSupportedError("list_snapshots() is not supported")

    @staticmethod
    def delete_snapshot(snapshot_id: str, **kwargs: Any) -> None:
        raise NotSupportedError("delete_snapshot() is not supported")

    def open_shell(self, *, session_id: str | None = None, shell_id: str | None = None):
        """Open an interactive shell (async, ``agentcore-runtime`` only).

        Returns an async context manager; use it from async code or
        ``AsyncSandbox.open_shell``.
        """
        opener = getattr(self._backend, "open_shell", None)
        if opener is None:
            from ..core.errors import NotSupportedError

            raise NotSupportedError(f"{self._backend.name}: interactive shell not supported")
        return opener(session_id=session_id, shell_id=shell_id)

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.kill()

    # -- code execution ----------------------------------------------------

    def run_code(
        self,
        code: str,
        language: str | None = None,
        context: Context | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        on_result: Callable[[Any], None] | None = None,
        on_error: Callable[[Any], None] | None = None,
        envs: dict[str, str] | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
        **kwargs: Any,
    ) -> Execution:
        """Execute code in the sandbox kernel (E2B ``run_code``).

        Pass either ``language`` or a ``context`` from ``create_code_context``
        (they are mutually exclusive, matching E2B). ``on_stdout``/``on_stderr``/
        ``on_result``/``on_error`` callbacks are invoked as in E2B.
        """
        if context is not None and language is not None:
            from ..core.errors import InvalidArgumentException

            raise InvalidArgumentException("pass either language or context, not both")
        lang = (context.language if context else language) or "python"
        with span(
            "raitbox.sandbox.run_code",
            **{
                "raitbox.backend": self._backend.name,
                "raitbox.sandbox_id": self.sandbox_id,
                "raitbox.language": lang,
            },
        ):
            execution = self._backend.run_code(
                code,
                language=lang,
                context=context,
                envs=envs,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
        if on_result:
            for result in execution.results:
                on_result(result)
        if on_error and execution.error is not None:
            on_error(execution.error)
        return execution

    def create_code_context(
        self, cwd: str | None = None, language: str = "python", **kwargs: Any
    ) -> Context:
        """Create an isolated execution context (E2B ``create_code_context``)."""
        ctx = self._backend.create_code_context(cwd=cwd, language=language)
        self._contexts[ctx.id] = ctx
        return ctx

    def list_code_contexts(self, **kwargs: Any) -> _list[Context]:
        return list(self._contexts.values())

    def restart_code_context(self, context: Context, **kwargs: Any) -> None:
        self._backend.restart_code_context(context)

    def remove_code_context(self, context: Context, **kwargs: Any) -> None:
        self._contexts.pop(context.id, None)
        self._backend.remove_code_context(context)

    # -- pause / resume (S3 file snapshot) ---------------------------------

    def pause(self, bucket: str | None = None) -> str:
        """Snapshot files to S3 and stop the sandbox; returns the id for resume.

        Files survive; in-memory/kernel state does not.
        """
        from ..core.persistence import save_snapshot

        sandbox_id = save_snapshot(self._backend, bucket=bucket)
        self.kill()
        return sandbox_id

    @classmethod
    def resume(
        cls,
        sandbox_id: str,
        bucket: str | None = None,
        *,
        backend: str | None = None,
        **kwargs: Any,
    ) -> Sandbox:
        """Start a fresh sandbox and restore files from an S3 snapshot."""
        from ..core.persistence import load_snapshot

        sbx = cls.create(backend=backend, **kwargs)
        load_snapshot(sbx._backend, sandbox_id, bucket=bucket)
        return sbx


class Filesystem:
    """Mirrors ``sandbox.files``."""

    def __init__(self, backend: Backend) -> None:
        self._b = backend

    def read(self, path: str, format: str = "text", **kwargs: Any) -> str | bytearray:
        return self._b.file_read(path, format=format)

    def write(
        self, path: str | list[dict], data: str | bytes | None = None, **kwargs: Any
    ) -> WriteInfo | _list[WriteInfo]:
        # E2B overload: write(path, data) or write([{path, data}, ...]).
        if isinstance(path, list):
            return self.write_files(path)
        return self._b.file_write(path, data if data is not None else "")

    def write_files(self, files: list[dict], **kwargs: Any) -> _list[WriteInfo]:
        return [self._b.file_write(f["path"], f["data"]) for f in files]

    def list(self, path: str = "", depth: int = 1, **kwargs: Any) -> _list[EntryInfo]:
        return self._b.file_list(path, depth=depth)

    def exists(self, path: str, **kwargs: Any) -> bool:
        return self._b.file_exists(path)

    def get_info(self, path: str, **kwargs: Any) -> EntryInfo:
        return self._b.file_get_info(path)

    def remove(self, path: str, **kwargs: Any) -> None:
        self._b.file_remove(path)

    def rename(self, old_path: str, new_path: str, **kwargs: Any) -> EntryInfo:
        return self._b.file_rename(old_path, new_path)

    def make_dir(self, path: str, **kwargs: Any) -> bool:
        return self._b.file_make_dir(path)

    def watch_dir(self, path: str, **kwargs: Any):
        """Watch a directory for changes (mirrors e2b ``files.watch_dir``).

        Returns a :class:`~raitbox.core.handles.WatchHandle`; call
        ``get_new_events()`` to poll and ``stop()`` when done. Implemented by
        polling ``files.list`` (works on all backends).
        """
        return WatchHandle(self._b.file_list, path)


class Commands:
    """Mirrors ``sandbox.commands``."""

    def __init__(self, backend: Backend) -> None:
        self._b = backend

    def run(
        self,
        cmd: str,
        background: bool = False,
        envs: dict[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        stdin: bool | None = None,
        timeout: float | None = 60,
        request_timeout: float | None = None,
        **kwargs: Any,
    ) -> CommandResult | CommandHandle:
        """Run a shell command.

        Foreground (default) returns a :class:`CommandResult` and raises
        :class:`CommandExitException` on a non-zero exit (E2B behavior).
        ``background=True`` returns a :class:`CommandHandle` (backends that
        track processes, e.g. ``microvm``).
        """
        if background:
            pid = self._b.command_start(cmd, envs=envs, cwd=cwd, user=user)
            return CommandHandle(self._b, pid, cmd)
        with span("raitbox.sandbox.run_command", **{"raitbox.backend": self._b.name}):
            result = self._b.run_command(
                cmd,
                envs=envs,
                cwd=cwd,
                user=user,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
                timeout=timeout,
            )
        if result.exit_code != 0:
            raise CommandExitException(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                error=result.error,
            )
        return result

    def list(self, **kwargs: Any) -> _list[ProcessInfo]:
        return self._b.command_list()

    def kill(self, pid: int, **kwargs: Any) -> bool:
        return self._b.command_kill(pid)

    def send_stdin(self, pid: int, data: str | bytes, **kwargs: Any) -> None:
        self._b.command_stdin(pid, data)

    def connect(self, pid: int, **kwargs: Any) -> CommandHandle:
        return CommandHandle(self._b, pid, cmd="")


class _Mouse:
    def __init__(self, backend: Backend) -> None:
        self._b = backend

    def move(self, x: int, y: int) -> Any:
        return self._b.computer_use("mouse.move", {"x": x, "y": y})

    def click(
        self, x: int | None = None, y: int | None = None, button: str = "left", double: bool = False
    ) -> Any:
        code = {"left": 1, "middle": 2, "right": 3}.get(button, 1)
        params: dict[str, Any] = {"button": code, "double": double}
        if x is not None and y is not None:
            params["x"], params["y"] = x, y
        return self._b.computer_use("mouse.click", params)

    def scroll(self, direction: str = "up", amount: int = 1) -> Any:
        return self._b.computer_use("mouse.scroll", {"direction": direction, "amount": amount})

    def get_position(self) -> Any:
        return self._b.computer_use("mouse.position", {})


class _Keyboard:
    def __init__(self, backend: Backend) -> None:
        self._b = backend

    def type(self, text: str) -> Any:
        return self._b.computer_use("keyboard.type", {"text": text})

    def press(self, key: str, modifiers: list[str] | None = None) -> Any:
        return self._b.computer_use("keyboard.press", {"key": key, "modifiers": modifiers or []})

    def hotkey(self, *keys: str) -> Any:
        *mods, key = keys
        return self._b.computer_use("keyboard.press", {"key": key, "modifiers": list(mods)})


class _Screenshot:
    def __init__(self, backend: Backend) -> None:
        self._b = backend

    def take(self) -> bytes:
        """Capture the desktop and return PNG bytes."""
        import base64 as _b64

        res = self._b.computer_use("screenshot", {}) or {}
        return _b64.b64decode(res.get("png", ""))


class _Display:
    def __init__(self, backend: Backend) -> None:
        self._b = backend

    def get_info(self) -> Any:
        return self._b.computer_use("display.info", {})


class ComputerUse:
    """``sandbox.computer_use`` — drive a virtual desktop (microvm; extension).

    Grouped API: ``mouse`` (move/click/scroll/get_position), ``keyboard``
    (type/press/hotkey), ``screenshot.take()``, ``display.get_info()``. Call
    ``start()`` first to bring up the virtual display (needs a desktop stack —
    Xvfb + xdotool + a capture tool — in the image). ``microvm`` only.
    """

    def __init__(self, backend: Backend) -> None:
        self._b = backend
        self.mouse = _Mouse(backend)
        self.keyboard = _Keyboard(backend)
        self.screenshot = _Screenshot(backend)
        self.display = _Display(backend)

    def start(self) -> Any:
        return self._b.computer_use("start", {})

    def stop(self) -> Any:
        return self._b.computer_use("stop", {})

    def get_status(self) -> Any:
        return self._b.computer_use("status", {})


class Lsp:
    """``sandbox.lsp`` — create language servers scoped to a project (extension).

    Additive (no E2B equivalent); ``microvm`` only. Use
    ``create_server(language, project_root)`` to start a project-scoped server.
    """

    def __init__(self, backend: Backend) -> None:
        self._b = backend

    def create_server(self, language: str, project_root: str = "/", **kwargs: Any) -> LspServer:
        """Create (but do not yet start) an :class:`LspServer` for ``language``."""
        return LspServer(self._b, language, project_root)


class Pty:
    """Mirrors ``sandbox.pty`` (interactive terminal).

    Backed by a real pseudo-terminal in the sandbox (``microvm`` only). Other
    backends raise ``NotSupportedError`` (``agentcore-runtime`` offers an async
    ``open_shell`` instead).
    """

    def __init__(self, backend: Backend) -> None:
        self._b = backend

    def create(
        self,
        size: PtySize,
        on_data: Callable[[bytes], None] | None = None,
        *,
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
        user: str | None = None,
        cmd: str | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> PtyHandle:
        """Start an interactive terminal; returns a :class:`PtyHandle`."""
        pid = self._b.pty_start(rows=size.rows, cols=size.cols, cwd=cwd, envs=envs, cmd=cmd)
        return PtyHandle(self._b, pid, on_data)

    def send_stdin(self, pid: int, data: bytes, **kwargs: Any) -> None:
        self._b.pty_stdin(pid, data)

    def resize(self, pid: int, size: PtySize, **kwargs: Any) -> None:
        self._b.pty_resize(pid, size.rows, size.cols)

    def kill(self, pid: int, **kwargs: Any) -> bool:
        return self._b.pty_kill(pid)
