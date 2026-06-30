"""Registers/unregisters the Melone MCP server in AI agent config files.

Never imports main.py (fcntl), so this package works on Windows.
"""

from . import claude_code, codex, common, skill
from .common import ConfigParseError, SetupResult, build_server_entry

__all__ = [
    "ConfigParseError",
    "SetupResult",
    "build_server_entry",
    "claude_code",
    "codex",
    "common",
    "skill",
]
