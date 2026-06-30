import sqlite3
from pathlib import Path

from .migrations import MigrationStatus, run_migrations


SQLITE_BUSY_TIMEOUT_MS = 5000
# Lets reads wait out lock contention while the collector writes at a 1s cadence.
READONLY_BUSY_TIMEOUT_MS = 2000


def connect(database_path: Path) -> sqlite3.Connection:
    # 모든 DB 진입점에서 같은 SQLite 옵션과 row 형식을 쓰도록 연결을 표준화합니다.
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def connect_readonly(database_path: Path) -> sqlite3.Connection:
    # Read-only so query-only entry points (e.g. MCP) can never mutate the
    # collector DB. A missing file raises sqlite3.OperationalError by design.
    connection = sqlite3.connect(
        f"file:{database_path.as_posix()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {READONLY_BUSY_TIMEOUT_MS}")
    return connection


def initialize_database(database_path: Path) -> MigrationStatus:
    # 서비스나 CLI가 DB를 쓰기 전에 필요한 스키마를 최신 상태로 맞춥니다.
    connection = connect(database_path)
    try:
        return run_migrations(connection)
    finally:
        connection.close()
