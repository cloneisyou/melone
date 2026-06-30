"""Safeguards shared by the Claude Code/Codex config editors.

Server entry builder, pre-edit backup, and atomic writes live here so both
editors follow the same protection rules.
"""

import os
import shutil
import sys
import tempfile
from abc import ABC, abstractmethod
from collections.abc import MutableMapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generic, TypeVar


class ConfigParseError(Exception):
    """Raised when a config file cannot be interpreted.

    Signals the never-write-on-parse-failure rule; exposed as its own class
    so the RPC daemon can map it to CONFIG_PARSE_ERROR (-32003).
    """


@dataclass(frozen=True)
class SetupResult:
    changed: bool
    enabled: bool
    config_path: Path
    backup_path: Path | None = None


BACKUP_INFIX = ".melone-bak-"
BACKUP_KEEP_COUNT = 3


def build_server_entry(*, melone_home: str | Path | None = None) -> dict[str, object]:
    """Build the Melone MCP server entry to register in config files.

    Pinning command to sys.executable guarantees the interpreter that has
    melone_service installed, independent of PATH or venv activation.

    In a packaged build sys.executable is the frozen melone-daemon binary, which
    is single-entry and cannot honor ``-m module``; it dispatches on argv
    instead, so register the ``mcp`` subcommand. In dev sys.executable is a real
    interpreter, so ``-m melone_service.mcp`` is used.

    The dev path must not ``resolve()`` the executable: a venv's python is a
    symlink to the base interpreter, and resolving it escapes the venv, losing
    melone_service from site-packages. The frozen binary is a real file, so it
    is resolved to canonicalize any symlinked install path.
    """
    if getattr(sys, "frozen", False):
        command = str(Path(sys.executable).resolve())
        args = ["mcp"]
    else:
        command = str(Path(sys.executable))
        args = ["-m", "melone_service.mcp"]
    entry: dict[str, object] = {"command": command, "args": args}
    if melone_home is not None:
        # Include env only when a MELONE_HOME override is active.
        entry["env"] = {"MELONE_HOME": str(melone_home)}
    return entry


def create_backup(config_path: Path) -> Path | None:
    """Back up the original next to it before editing, keeping the latest 3.

    Enables manual recovery if an edit corrupts the user's config. Returns
    None when the original does not exist (new file, nothing to back up).
    """
    if not config_path.is_file():
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = config_path.with_name(f"{config_path.name}{BACKUP_INFIX}{timestamp}")
    counter = 0
    while backup_path.exists():
        # Never overwrite, even if two backups land in the same microsecond.
        counter += 1
        backup_path = config_path.with_name(
            f"{config_path.name}{BACKUP_INFIX}{timestamp}-{counter}"
        )

    shutil.copy2(config_path, backup_path)
    _prune_backups(config_path)
    return backup_path


def _prune_backups(config_path: Path) -> None:
    # Backup names sort by timestamp, so the oldest come first.
    backups = sorted(config_path.parent.glob(f"{config_path.name}{BACKUP_INFIX}*"))
    for stale_backup in backups[:-BACKUP_KEEP_COUNT]:
        stale_backup.unlink()


def atomic_write_text(path: Path, content: str) -> None:
    """Write to a temp file in the same directory, then swap via os.replace.

    A crash mid-write never leaves a half-written config behind. The temp
    file sits in the same directory because os.replace is only atomic within
    a single volume.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


Doc = TypeVar("Doc")


class ConfigEditor(ABC, Generic[Doc]):
    """Template for editing one MCP host's config file (Claude Code / Codex).

    The enable/disable/is_enabled flow — backup, idempotency, write ordering,
    SetupResult shaping, and the never-write-on-parse-failure rule — lives here
    once. Subclasses supply only the format-specific primitives (load, dump,
    servers-container access, entry shape). `Doc` is the parsed document type
    (a plain dict for JSON, a TOMLDocument for tomlkit).
    """

    #: Key under the servers table that holds the Melone entry ("melone").
    server_key: str

    @abstractmethod
    def load(self, path: Path) -> Doc:
        """Parse an existing file, raising ConfigParseError on bad input."""

    @abstractmethod
    def empty(self) -> Doc:
        """A fresh empty document for a config file that does not exist yet."""

    @abstractmethod
    def dump(self, doc: Doc) -> str:
        """Serialize the document back to file text."""

    @abstractmethod
    def ensure_servers(self, doc: Doc) -> MutableMapping[str, object]:
        """Return the servers container, creating it if absent.

        Raises ConfigParseError when the existing value is the wrong type, so
        unexpected structure never gets silently overwritten.
        """

    @abstractmethod
    def get_servers(self, doc: Doc) -> MutableMapping[str, object] | None:
        """Return the servers container, or None when absent/wrong type."""

    @abstractmethod
    def canonical_entry(self, melone_home: str | Path | None) -> dict[str, object]:
        """The desired entry as a plain dict — the idempotency comparison form."""

    def to_stored(self, entry: dict[str, object]) -> object:
        """Convert the canonical entry to the value stored in the document."""
        return entry

    def from_stored(self, existing: object) -> object:
        """Convert a stored value back to the canonical (plain) form."""
        return existing

    def is_enabled(self, path: Path) -> bool:
        if not path.is_file():
            return False
        servers = self.get_servers(self.load(path))
        return servers is not None and self.server_key in servers

    def enable(self, path: Path, *, melone_home: str | Path | None) -> SetupResult:
        """Add or refresh the Melone server entry. Idempotent."""
        doc = self.load(path) if path.is_file() else self.empty()
        servers = self.ensure_servers(doc)
        entry = self.canonical_entry(melone_home)
        existing = servers.get(self.server_key)
        if existing is not None and self.from_stored(existing) == entry:
            return SetupResult(changed=False, enabled=True, config_path=path)

        servers[self.server_key] = self.to_stored(entry)
        backup_path = create_backup(path)
        atomic_write_text(path, self.dump(doc))
        return SetupResult(
            changed=True, enabled=True, config_path=path, backup_path=backup_path
        )

    def disable(self, path: Path) -> SetupResult:
        """Remove only the Melone entry; other servers are never touched."""
        if not path.is_file():
            return SetupResult(changed=False, enabled=False, config_path=path)

        doc = self.load(path)
        servers = self.get_servers(doc)
        if servers is None or self.server_key not in servers:
            return SetupResult(changed=False, enabled=False, config_path=path)

        del servers[self.server_key]
        backup_path = create_backup(path)
        atomic_write_text(path, self.dump(doc))
        return SetupResult(
            changed=True, enabled=False, config_path=path, backup_path=backup_path
        )
