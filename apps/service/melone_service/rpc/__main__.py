"""Entry point for the packaged melone-daemon binary.

PyInstaller bundles this module as the binary's single entry, so it dispatches
on the first CLI argument:

- ``mcp``              — the MCP stdio server (what Claude Code and Codex launch);
- ``service``          — the collector service (what ``main.start_service`` spawns);
- ``permission-probe`` — print Accessibility status as JSON, then exit (a fresh
  process the daemon spawns to bypass AXIsProcessTrusted's per-process cache, so
  a newly granted permission is seen without a daemon restart);
- anything else, including no arguments — the RPC daemon the desktop talks to.

A frozen binary cannot honor ``python -m module``, so an explicit subcommand is
the only way to expose the secondary modes.
"""

import sys


def main() -> int:
    if sys.argv[1:2] == ["mcp"]:
        # Drop the dispatched subcommand so it is never handed to the MCP
        # server's own argv (defensive: FastMCP.run() ignores it today).
        sys.argv.pop(1)
        # Imported lazily so the RPC path never pays for the MCP server's deps.
        from melone_service.mcp.server import main as mcp_main

        mcp_main()
        return 0

    if sys.argv[1:2] == ["service"]:
        # Drop the dispatched subcommand so the collector sees a clean argv.
        sys.argv.pop(1)
        # Lazy import: melone_service.main pulls in fcntl (Unix-only) and the
        # collector deps, which the RPC and MCP paths must never require.
        from melone_service.main import main as service_main

        return service_main()

    if sys.argv[1:2] == ["permission-probe"]:
        sys.argv.pop(1)
        # Lazy import keeps the RPC/MCP startup paths free of this helper.
        from melone_service.permissions import run_permission_probe

        return run_permission_probe()

    from melone_service.rpc.server import main as rpc_main

    return rpc_main()


if __name__ == "__main__":
    sys.exit(main())
