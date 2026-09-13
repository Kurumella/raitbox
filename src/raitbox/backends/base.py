"""Backend abstraction.

A ``Backend`` encapsulates one execution substrate (AgentCore Code Interpreter,
AgentCore Runtime, or Lambda MicroVM). The E2B-compatible ``Sandbox`` facade
holds a backend and forwards calls to it, so all AWS specifics live here.

Backends are constructed via the ``open`` classmethod (which provisions/starts
the underlying session) or ``attach`` (which binds to an existing one, for
``Sandbox.connect``). Methods that a given substrate cannot support raise
``NotSupportedError`` with an actionable message.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..core.errors import NotSupportedError
from ..core.types import (
    CommandResult,
    EntryInfo,
    Execution,
    SandboxInfo,
    WriteInfo,
)

if TYPE_CHECKING:
    from ..core.types import Context, PreviewLink

OutputCallback = Callable[[str], None]


class Backend(ABC):
    """Common interface every RaitBox backend implements."""

    #: Canonical backend name (matches the registry key).
    name: str = "base"

    # -- construction ------------------------------------------------------

    @classmethod
    @abstractmethod
    def open(
        cls,
        *,
        template: str | None,
        timeout: int,
        envs: dict[str, str] | None,
        metadata: dict[str, str] | None,
        region_name: str | None,
        profile_name: str | None,
        client: Any | None = None,
        **kwargs: Any,
    ) -> Backend:
        """Provision and start a new sandbox session."""

    @classmethod
    def attach(
        cls,
        sandbox_id: str,
        *,
        region_name: str | None = None,
        profile_name: str | None = None,
        client: Any | None = None,
        **kwargs: Any,
    ) -> Backend:
        """Bind to an existing sandbox (for ``Sandbox.connect``)."""
        raise NotSupportedError(f"{cls.name}: connecting to an existing sandbox is not supported")

    @classmethod
    def list_running(
        cls,
        *,
        region_name: str | None = None,
        profile_name: str | None = None,
        client: Any | None = None,
        **kwargs: Any,
    ) -> list[SandboxInfo]:
        """List running sandboxes (for ``Sandbox.list``)."""
        raise NotSupportedError(f"{cls.name}: listing sandboxes is not supported")

    # -- lifecycle ---------------------------------------------------------

    @property
    @abstractmethod
    def sandbox_id(self) -> str: ...

    @abstractmethod
    def kill(self) -> bool: ...

    def set_timeout(self, timeout: int) -> None:
        raise NotSupportedError(f"{self.name}: set_timeout is not supported")

    def is_running(self) -> bool:
        return True

    def get_info(self) -> SandboxInfo:
        return SandboxInfo(sandbox_id=self.sandbox_id)

    def get_host(self, port: int) -> str:
        raise NotSupportedError(
            f"{self.name}: get_host() is not available on this backend. "
            "Retrieve artifacts with sandbox.files.read()."
        )

    def preview_link(self, port: int, expires_in_minutes: int = 30) -> PreviewLink:
        raise NotSupportedError(
            f"{self.name}: preview links need an inbound network path (microvm backend)."
        )

    # -- language server (LSP) ---------------------------------------------
    # A small generic protocol drives the LspServer handle: start a server,
    # dispatch a named op, stop it. Backends that can run a language server
    # process (microvm) override these; the default is unsupported.

    def lsp_start(self, language: str, path: str) -> str:
        raise NotSupportedError(
            f"{self.name}: language servers need an OS process (microvm backend)."
        )

    def lsp_call(self, server_id: str, op: str, params: dict[str, Any]) -> Any:
        raise NotSupportedError(f"{self.name}: LSP is not supported")

    def lsp_stop(self, server_id: str) -> None:
        raise NotSupportedError(f"{self.name}: LSP is not supported")

    # -- execution ---------------------------------------------------------

    def run_code(self, code: str, language: str = "python", **kwargs: Any) -> Execution:
        raise NotSupportedError(f"{self.name}: run_code is not supported")

    def create_code_context(self, cwd: str | None = None, language: str = "python") -> Context:
        """Create an execution context. Default: a fresh id, no real isolation."""
        import uuid

        from ..core.types import Context

        return Context(id=uuid.uuid4().hex, language=language, cwd=cwd)

    def restart_code_context(self, context: Context) -> None:  # noqa: B027 - optional hook
        """Reset a context's state (default: no-op; backends may override)."""

    def remove_code_context(self, context: Context) -> None:  # noqa: B027 - optional hook
        """Drop a context (default: no-op; backends may override)."""

    @abstractmethod
    def run_command(
        self,
        cmd: str,
        *,
        envs: dict[str, str] | None = None,
        cwd: str | None = None,
        user: str | None = None,
        on_stdout: OutputCallback | None = None,
        on_stderr: OutputCallback | None = None,
        timeout: float | None = None,
    ) -> CommandResult: ...

    # -- background commands (drive CommandHandle) -------------------------
    # Backends that track processes override these; the default is unsupported.

    def command_start(
        self,
        cmd: str,
        *,
        envs: dict[str, str] | None = None,
        cwd: str | None = None,
        user: str | None = None,
    ) -> int:
        raise NotSupportedError(f"{self.name}: background commands are not supported")

    def command_poll(self, pid: int, stdout_off: int, stderr_off: int) -> dict[str, Any]:
        raise NotSupportedError(f"{self.name}: background commands are not supported")

    def command_kill(self, pid: int) -> bool:
        raise NotSupportedError(f"{self.name}: commands.kill is not supported")

    def command_stdin(self, pid: int, data: str | bytes) -> None:
        raise NotSupportedError(f"{self.name}: commands.send_stdin is not supported")

    def command_list(self) -> list:
        raise NotSupportedError(f"{self.name}: commands.list is not supported")

    # -- interactive PTY (drive PtyHandle) ---------------------------------
    # Backends with a real TTY (microvm) override these; the default is
    # unsupported (agentcore-runtime offers an async ``open_shell`` instead).

    _PTY_MSG = (
        "interactive pty needs a real TTY (microvm backend); "
        "agentcore-runtime offers an async open_shell()."
    )

    def pty_start(
        self,
        *,
        rows: int,
        cols: int,
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
        cmd: str | None = None,
    ) -> int:
        raise NotSupportedError(f"{self.name}: {self._PTY_MSG}")

    def pty_poll(self, pid: int, offset: int) -> dict[str, Any]:
        raise NotSupportedError(f"{self.name}: {self._PTY_MSG}")

    def pty_stdin(self, pid: int, data: bytes) -> None:
        raise NotSupportedError(f"{self.name}: {self._PTY_MSG}")

    def pty_resize(self, pid: int, rows: int, cols: int) -> None:
        raise NotSupportedError(f"{self.name}: {self._PTY_MSG}")

    def pty_kill(self, pid: int) -> bool:
        raise NotSupportedError(f"{self.name}: {self._PTY_MSG}")

    # -- computer use (desktop automation) ---------------------------------
    # One generic dispatch drives the whole ``computer_use`` namespace; the
    # action name is dotted (``mouse.move``, ``keyboard.type``, ``screenshot``,
    # ``display.info``, ``start``/``stop``/``status``). microvm overrides this.

    def computer_use(self, action: str, params: dict[str, Any]) -> Any:
        raise NotSupportedError(
            f"{self.name}: computer use needs a graphical desktop (microvm backend)."
        )

    # -- filesystem --------------------------------------------------------

    @abstractmethod
    def file_read(self, path: str, format: str = "text") -> str | bytearray: ...

    @abstractmethod
    def file_write(self, path: str, data: str | bytes) -> WriteInfo: ...

    @abstractmethod
    def file_list(self, path: str = "", depth: int = 1) -> list[EntryInfo]: ...

    @abstractmethod
    def file_remove(self, path: str) -> None: ...

    def file_exists(self, path: str) -> bool:
        parent, _, name = path.rstrip("/").rpartition("/")
        return any(e.name == name for e in self.file_list(parent))

    def file_rename(self, old_path: str, new_path: str) -> EntryInfo:
        raise NotSupportedError(f"{self.name}: files.rename is not supported")

    def file_make_dir(self, path: str) -> bool:
        raise NotSupportedError(f"{self.name}: files.make_dir is not supported")

    def file_get_info(self, path: str) -> EntryInfo:
        parent, _, name = path.rstrip("/").rpartition("/")
        for e in self.file_list(parent):
            if e.name == name:
                return e
        from ..core.errors import FileNotFoundException

        raise FileNotFoundException(f"No such file: {path!r}")
