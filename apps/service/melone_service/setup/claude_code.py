"""Adds/removes the melone entry in top-level mcpServers of ~/.claude.json.

Parses the whole file with json to preserve every other key untouched, and
rewrites with 2-space indent to match Claude Code's own format. The shared
backup/idempotency/write flow lives in common.ConfigEditor; this module only
supplies the JSON-specific primitives.
"""

import json
from collections.abc import MutableMapping
from pathlib import Path

from .common import (
    ConfigEditor,
    ConfigParseError,
    SetupResult,
    build_server_entry,
)

SERVER_KEY = "melone"
_SERVERS_KEY = "mcpServers"


def default_config_path() -> Path:
    return Path.home() / ".claude.json"


class _ClaudeCodeEditor(ConfigEditor[dict[str, object]]):
    server_key = SERVER_KEY

    def load(self, path: Path) -> dict[str, object]:
        # Never write after a parse failure — protects the user's config.
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ConfigParseError(
                f"Claude Code 설정 파싱 실패 ({path}): {error}"
            ) from error
        if not isinstance(config, dict):
            raise ConfigParseError(
                f"Claude Code 설정의 최상위가 객체가 아닙니다: {path}"
            )
        return config

    def empty(self) -> dict[str, object]:
        return {}

    def dump(self, doc: dict[str, object]) -> str:
        return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"

    def ensure_servers(self, doc: dict[str, object]) -> MutableMapping[str, object]:
        servers = doc.setdefault(_SERVERS_KEY, {})
        if not isinstance(servers, dict):
            # Treat unexpected structure as a parse failure instead of overwriting user data.
            raise ConfigParseError(
                f"Claude Code 설정의 {_SERVERS_KEY}가 객체가 아닙니다"
            )
        return servers

    def get_servers(
        self, doc: dict[str, object]
    ) -> MutableMapping[str, object] | None:
        servers = doc.get(_SERVERS_KEY)
        return servers if isinstance(servers, dict) else None

    def canonical_entry(
        self, melone_home: str | Path | None
    ) -> dict[str, object]:
        # Claude Code entries must declare the transport, hence the stdio type.
        return {"type": "stdio", **build_server_entry(melone_home=melone_home)}


_EDITOR = _ClaudeCodeEditor()


def detect(config_path: Path | None = None) -> bool:
    # Existence of ~/.claude.json marks a Claude Code user.
    return _resolve_path(config_path).is_file()


def is_enabled(config_path: Path | None = None) -> bool:
    return _EDITOR.is_enabled(_resolve_path(config_path))


def enable(
    config_path: Path | None = None,
    *,
    melone_home: str | Path | None = None,
) -> SetupResult:
    """Add the mcpServers.melone entry.

    Idempotent: returns changed=False when the entry is already identical,
    refreshes it when it differs (e.g. interpreter path changed), and creates
    the file when missing.
    """
    return _EDITOR.enable(_resolve_path(config_path), melone_home=melone_home)


def disable(config_path: Path | None = None) -> SetupResult:
    """Remove only the melone entry from mcpServers.

    Other server entries are never touched; a missing file or entry
    (idempotent re-call) returns changed=False without error.
    """
    return _EDITOR.disable(_resolve_path(config_path))


def _resolve_path(config_path: Path | None) -> Path:
    return default_config_path() if config_path is None else config_path
