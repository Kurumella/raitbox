"""Sandbox operations exposed over MCP, and the server that registers them.

``RaitboxTools`` owns a small registry of live sandboxes keyed by id and turns
each MCP tool call into a ``Sandbox`` operation. It is deliberately independent
of the MCP framework so it can be unit-tested directly; ``build_server`` binds
the methods to a ``FastMCP`` instance.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class RaitboxTools:
    """Stateful set of sandbox tools for an MCP session.

    ``create_defaults`` (e.g. ``backend=``, ``template=``, ``region_name=``) are
    applied to every ``create_sandbox`` call. Pass ``sandbox_factory`` to inject
    a fake in tests; it defaults to ``Sandbox.create``.
    """

    def __init__(
        self,
        *,
        sandbox_factory: Callable[..., Any] | None = None,
        **create_defaults: Any,
    ) -> None:
        self._factory = sandbox_factory
        self._defaults = create_defaults
        self._sandboxes: dict[str, Any] = {}

    def _create(self, **kwargs: Any) -> Any:
        if self._factory is not None:
            return self._factory(**kwargs)
        from ..client.sync import Sandbox

        return Sandbox.create(**kwargs)

    def _get(self, sandbox_id: str) -> Any:
        try:
            return self._sandboxes[sandbox_id]
        except KeyError:
            raise ValueError(f"unknown sandbox_id {sandbox_id!r}") from None

    # -- tools -------------------------------------------------------------

    def create_sandbox(self, template: str | None = None, backend: str | None = None) -> str:
        """Create a sandbox and return its id."""
        kwargs = {**self._defaults}
        if template is not None:
            kwargs["template"] = template
        if backend is not None:
            kwargs["backend"] = backend
        sbx = self._create(**kwargs)
        self._sandboxes[sbx.sandbox_id] = sbx
        return sbx.sandbox_id

    def run_code(self, sandbox_id: str, code: str, language: str = "python") -> dict[str, Any]:
        """Run code in the sandbox kernel; returns stdout/stderr/error."""
        ex = self._get(sandbox_id).run_code(code, language=language)
        return {
            "stdout": "".join(ex.logs.stdout),
            "stderr": "".join(ex.logs.stderr),
            "text": ex.text,
            "error": ex.error.name if ex.error else None,
        }

    def run_command(self, sandbox_id: str, cmd: str) -> dict[str, Any]:
        """Run a shell command; returns stdout/stderr/exit_code."""
        from ..core.errors import CommandExitException

        try:
            res = self._get(sandbox_id).commands.run(cmd)
        except CommandExitException as e:
            return {"stdout": e.stdout, "stderr": e.stderr, "exit_code": e.exit_code}
        return {"stdout": res.stdout, "stderr": res.stderr, "exit_code": res.exit_code}

    def files_write(self, sandbox_id: str, path: str, content: str) -> dict[str, Any]:
        info = self._get(sandbox_id).files.write(path, content)
        return {"path": info.path, "name": info.name}

    def files_read(self, sandbox_id: str, path: str) -> str:
        return str(self._get(sandbox_id).files.read(path))

    def files_list(self, sandbox_id: str, path: str = "") -> list[dict[str, str]]:
        return [
            {"name": e.name, "type": e.type, "path": e.path}
            for e in self._get(sandbox_id).files.list(path)
        ]

    def list_sandboxes(self) -> list[str]:
        return list(self._sandboxes)

    def kill_sandbox(self, sandbox_id: str) -> bool:
        sbx = self._sandboxes.pop(sandbox_id, None)
        if sbx is None:
            return False
        sbx.kill()
        return True

    # -- registration ------------------------------------------------------

    def register(self, server: Any) -> None:
        """Register every tool method on a FastMCP-style ``server`` via ``@tool``."""
        for name in (
            "create_sandbox",
            "run_code",
            "run_command",
            "files_write",
            "files_read",
            "files_list",
            "list_sandboxes",
            "kill_sandbox",
        ):
            server.tool(name=name)(getattr(self, name))


def build_server(name: str = "raitbox", **create_defaults: Any) -> Any:
    """Build a ``FastMCP`` server exposing the RaitBox sandbox tools."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(name)
    RaitboxTools(**create_defaults).register(server)
    return server
