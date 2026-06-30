"""JSON-RPC 2.0 stdio daemon package spawned by the Electron shell.

main.py (fcntl) is never imported at top level so the daemon runs on Windows;
service start/stop lazy-imports it inside darwin-only branches.
"""

# Do not import server.py here — importing the package must not trigger
# side effects such as building the dispatch table.
