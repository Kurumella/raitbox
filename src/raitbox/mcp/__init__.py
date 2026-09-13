"""RaitBox MCP server — expose sandboxes to MCP clients as tools.

``RaitboxTools`` holds the sandbox operations (framework-agnostic, unit-tested);
``build_server`` wires them into an MCP ``FastMCP`` server. Run it with::

    python -m raitbox.mcp            # stdio transport (Claude Desktop, etc.)

Requires the ``mcp`` extra: ``pip install 'raitbox[mcp]'``.
"""

from __future__ import annotations

from .tools import RaitboxTools, build_server

__all__ = ["RaitboxTools", "build_server"]
