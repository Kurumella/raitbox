"""``python -m raitbox.mcp`` — run the RaitBox MCP server over stdio.

Defaults for created sandboxes come from the environment / AWS credential chain
(honoring ``RAITBOX_BACKEND``); pass ``--backend`` to override.
"""

from __future__ import annotations

import argparse

from .tools import build_server


def main() -> None:
    p = argparse.ArgumentParser(description="RaitBox MCP server (stdio)")
    p.add_argument("--backend", help="Default backend for created sandboxes")
    p.add_argument("--template", help="Default template/image for created sandboxes")
    p.add_argument("--region", dest="region_name", help="AWS region")
    args = {k: v for k, v in vars(p.parse_args()).items() if v is not None}
    server = build_server(**args)
    server.run()


if __name__ == "__main__":
    main()
