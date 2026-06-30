"""PyInstaller entry point for the bundled melone-daemon binary.

The desktop app and the MCP clients spawn this standalone executable (no system
Python). It MUST route through the argv dispatcher in rpc/__main__.py — the
packaged equivalent of `python -m melone_service.rpc` — so the `mcp` and
`service` subcommands work in the frozen build. Importing rpc.server.main
directly would bypass the dispatcher and silently run the RPC daemon for every
subcommand, breaking MCP registration and the collector spawn.
"""

import sys

from melone_service.rpc.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
