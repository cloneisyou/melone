"""Adds/removes the [mcp_servers.melone] table in ~/.codex/config.toml.

Uses the tomlkit round-trip parser because users manage this file by hand:
comments, whitespace, and other [mcp_servers.*] entries must be preserved. The
shared backup/idempotency/write flow lives in common.ConfigEditor; this module
only supplies the TOML-specific primitives.
"""

from collections.abc import MutableMapping
from pathlib import Path

import tomlkit
from tomlkit.exceptions import ParseError

from .common import (
    ConfigEditor,
    ConfigParseError,
    SetupResult,
    build_server_entry,
)

SERVER_KEY = "melone"
_SERVERS_KEY = "mcp_servers"


def default_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


class _CodexEditor(ConfigEditor[tomlkit.TOMLDocument]):
    server_key = SERVER_KEY

    def load(self, path: Path) -> tomlkit.TOMLDocument:
        # Never write after a parse failure — protects the user's config.
        try:
            return tomlkit.parse(path.read_text(encoding="utf-8"))
        except (ParseError, UnicodeDecodeError) as error:
            raise ConfigParseError(
                f"Codex 설정 파싱 실패 ({path}): {error}"
            ) from error

    def empty(self) -> tomlkit.TOMLDocument:
        return tomlkit.document()

    def dump(self, doc: tomlkit.TOMLDocument) -> str:
        return tomlkit.dumps(doc)

    def ensure_servers(
        self, doc: tomlkit.TOMLDocument
    ) -> MutableMapping[str, object]:
        servers = doc.get(_SERVERS_KEY)
        if servers is None:
            # A super table writes [mcp_servers.melone] without an empty [mcp_servers] header.
            servers = tomlkit.table(is_super_table=True)
            doc[_SERVERS_KEY] = servers
            return servers
        if not isinstance(servers, MutableMapping):
            # Treat unexpected structure as a parse failure instead of overwriting user data.
            raise ConfigParseError(
                f"Codex 설정의 {_SERVERS_KEY}가 테이블이 아닙니다"
            )
        return servers

    def get_servers(
        self, doc: tomlkit.TOMLDocument
    ) -> MutableMapping[str, object] | None:
        servers = doc.get(_SERVERS_KEY)
        return servers if isinstance(servers, MutableMapping) else None

    def canonical_entry(
        self, melone_home: str | Path | None
    ) -> dict[str, object]:
        return build_server_entry(melone_home=melone_home)

    def to_stored(self, entry: dict[str, object]) -> object:
        return _server_table(entry)

    def from_stored(self, existing: object) -> object:
        # Unwrap tomlkit containers to plain Python values for comparison.
        unwrap = getattr(existing, "unwrap", None)
        return unwrap() if callable(unwrap) else existing


_EDITOR = _CodexEditor()


def detect(config_path: Path | None = None) -> bool:
    # A ~/.codex/ directory marks a Codex user even before config.toml exists,
    # so check the parent directory rather than the file.
    return _resolve_path(config_path).parent.is_dir()


def is_enabled(config_path: Path | None = None) -> bool:
    return _EDITOR.is_enabled(_resolve_path(config_path))


def enable(
    config_path: Path | None = None,
    *,
    melone_home: str | Path | None = None,
) -> SetupResult:
    """Add the [mcp_servers.melone] table.

    Idempotent: returns changed=False when the entry is already identical,
    refreshes it when it differs, and creates the file when missing.
    """
    return _EDITOR.enable(_resolve_path(config_path), melone_home=melone_home)


def disable(config_path: Path | None = None) -> SetupResult:
    """Remove only the [mcp_servers.melone] table.

    Other [mcp_servers.*] entries and comments are never touched; a missing
    file or entry (idempotent re-call) returns changed=False without error.
    """
    return _EDITOR.disable(_resolve_path(config_path))


def _resolve_path(config_path: Path | None) -> Path:
    return default_config_path() if config_path is None else config_path


def _server_table(entry: dict[str, object]) -> tomlkit.items.Table:
    # An inline table keeps env on one line, avoiding an [mcp_servers.melone.env] header.
    table = tomlkit.table()
    table["command"] = entry["command"]
    table["args"] = entry["args"]
    env = entry.get("env")
    if env is not None:
        env_table = tomlkit.inline_table()
        env_table.update(env)
        table["env"] = env_table
    return table
