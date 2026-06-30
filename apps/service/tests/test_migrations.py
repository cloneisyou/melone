import sqlite3

from melone_service.store.db import SQLITE_BUSY_TIMEOUT_MS, connect, initialize_database
from melone_service.store.migrations import (
    MIGRATIONS,
    OCR_FTS_TOKENIZER,
    ensure_migration_table,
    read_applied_version,
    run_migrations,
)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row[0] for row in rows}


def _index_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
    ).fetchall()
    return {row[0] for row in rows}


def test_initialize_database_creates_mvp_schema(tmp_path):
    database_path = tmp_path / "melone.sqlite"

    status = initialize_database(database_path)

    assert database_path.is_file()
    assert status.current_version == 5
    assert status.latest_version == 5
    assert status.pending_versions == ()

    connection = sqlite3.connect(database_path)
    try:
        assert {
            "schema_migrations",
            "events",
            "screen_sessions",
            "screen_frames",
            "ocr_jobs",
            "ocr_chunks",
            "ocr_chunks_fts",
            "ocr_chunk_embeddings",
            "context_rank_scores",
        }.issubset(_table_names(connection))
        assert "screenshots" not in _table_names(connection)
        assert "sessions" not in _table_names(connection)
        assert {
            "idx_events_timestamp",
            "idx_events_type_timestamp",
            "idx_screen_sessions_source_key",
            "idx_screen_sessions_retrieval_locator",
            "idx_screen_sessions_status_started_at",
            "idx_screen_frames_session_captured_at",
            "idx_screen_frames_sha256",
            "idx_ocr_jobs_due",
            "idx_ocr_jobs_session_id",
            "idx_ocr_jobs_frame_id",
            "idx_ocr_jobs_source_key",
            "idx_ocr_jobs_retrieval_locator",
            "idx_ocr_chunks_session_id",
            "idx_ocr_chunks_frame_id",
            "idx_ocr_chunks_source_key",
            "idx_ocr_chunks_retrieval_locator",
            "idx_ocr_chunks_text_hash",
            "idx_ocr_chunk_embeddings_model_dimension",
            "idx_ocr_chunk_embeddings_text_hash",
            "idx_context_rank_scores_computed_at",
        }.issubset(_index_names(connection))

        event_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(events)")
        }
        assert {
            "id",
            "timestamp",
            "type",
            "app_name",
            "bundle_id",
            "pid",
            "window_title",
            "url",
            "source",
            "metadata_json",
        } == event_columns

        screen_session_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(screen_sessions)")
        }
        assert {
            "id",
            "source_key",
            "retrieval_locator",
            "app_name",
            "bundle_id",
            "window_title",
            "url",
            "started_at",
            "ended_at",
            "status",
            "created_at",
            "updated_at",
        } == screen_session_columns

        screen_frame_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(screen_frames)")
        }
        assert {
            "id",
            "session_id",
            "captured_at",
            "image_path",
            "sha256",
            "perceptual_hash",
            "diff_score",
            "width",
            "height",
            "status",
            "created_at",
            "image_retention_state",
            "image_retention_updated_at",
        } == screen_frame_columns

        ocr_job_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(ocr_jobs)")
        }
        assert {
            "id",
            "type",
            "target_id",
            "session_id",
            "frame_id",
            "source_key",
            "retrieval_locator",
            "priority",
            "status",
            "attempts",
            "next_run_at",
            "locked_at",
            "last_error",
            "metadata_json",
            "created_at",
            "updated_at",
        } == ocr_job_columns

        ocr_chunk_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(ocr_chunks)")
        }
        assert {
            "id",
            "session_id",
            "frame_id",
            "source_key",
            "retrieval_locator",
            "app_name",
            "window_title",
            "url",
            "crop_bbox_json",
            "text",
            "text_hash",
            "provider",
            "model",
            "latency_ms",
            "created_at",
        } == ocr_chunk_columns

        embedding_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(ocr_chunk_embeddings)")
        }
        assert {
            "chunk_id",
            "model",
            "dimension",
            "text_hash",
            "embedding",
            "created_at",
            "updated_at",
        } == embedding_columns

        embedding_primary_key = {
            row[1]: row[5]
            for row in connection.execute("PRAGMA table_info(ocr_chunk_embeddings)")
            if row[5] > 0
        }
        assert embedding_primary_key == {
            "chunk_id": 1,
            "model": 2,
            "dimension": 3,
        }

        embedding_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(ocr_chunk_embeddings)"
        ).fetchall()
        assert any(
            row[2] == "ocr_chunks"
            and row[3] == "chunk_id"
            and row[4] == "id"
            and row[6].upper() == "CASCADE"
            for row in embedding_foreign_keys
        )

        rank_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(context_rank_scores)")
        }
        assert {
            "source_key",
            "score",
            "visits",
            "retrieval_locators_json",
            "computed_at",
            "model_version",
            "created_at",
            "updated_at",
        } == rank_columns

        fts_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'ocr_chunks_fts'"
        ).fetchone()[0]
        assert "fts5" in fts_sql.lower()
        assert f"tokenize='{OCR_FTS_TOKENIZER}'" in fts_sql
    finally:
        connection.close()


def test_initialize_database_is_idempotent(tmp_path):
    database_path = tmp_path / "melone.sqlite"

    initialize_database(database_path)
    status = initialize_database(database_path)

    connection = sqlite3.connect(database_path)
    try:
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
    finally:
        connection.close()

    assert status.current_version == 5
    assert status.pending_versions == ()
    assert migration_count == 5


def test_migrating_v1_database_drops_legacy_placeholder_tables(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    _create_v1_database(database_path)

    connection = connect(database_path)
    try:
        status = run_migrations(connection)
        tables = _table_names(connection)
        event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        connection.close()

    assert status.current_version == 5
    assert status.pending_versions == ()
    assert "screenshots" not in tables
    assert "sessions" not in tables
    assert {
        "screen_sessions",
        "screen_frames",
        "ocr_jobs",
        "ocr_chunks",
        "ocr_chunks_fts",
        "ocr_chunk_embeddings",
        "context_rank_scores",
    }.issubset(tables)
    assert event_count == 1


def test_migrating_v3_database_renames_legacy_vlm_jobs_table(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    _create_v3_database(database_path)

    connection = connect(database_path)
    try:
        status = run_migrations(connection)
        tables = _table_names(connection)
        indexes = _index_names(connection)
        job_count = connection.execute("SELECT COUNT(*) FROM ocr_jobs").fetchone()[0]
    finally:
        connection.close()

    assert status.current_version == 5
    assert status.pending_versions == ()
    assert "ocr_jobs" in tables
    assert "vlm_jobs" not in tables
    assert "idx_ocr_jobs_due" in indexes
    assert "idx_vlm_jobs_due" not in indexes
    assert job_count == 1


def test_connect_applies_sqlite_runtime_pragmas(tmp_path):
    database_path = tmp_path / "melone.sqlite"

    connection = connect(database_path)
    try:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    finally:
        connection.close()

    assert journal_mode.lower() == "wal"
    assert busy_timeout == SQLITE_BUSY_TIMEOUT_MS
    assert foreign_keys == 1


def _create_v1_database(database_path):
    connection = sqlite3.connect(database_path)
    try:
        ensure_migration_table(connection)
        with connection:
            connection.executescript(MIGRATIONS[0].sql)
            connection.execute(
                """
                INSERT INTO schema_migrations (version, name)
                VALUES (?, ?)
                """,
                (MIGRATIONS[0].version, MIGRATIONS[0].name),
            )
            connection.execute(
                """
                INSERT INTO events (
                  id,
                  timestamp,
                  type,
                  source,
                  metadata_json
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "evt_v1",
                    "2026-06-09T06:00:00.000Z",
                    "active_app_changed",
                    "test",
                    "{}",
                ),
            )
            connection.execute(
                """
                INSERT INTO screenshots (
                  id,
                  event_id,
                  timestamp,
                  image_path
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    "shot_v1",
                    "evt_v1",
                    "2026-06-09T06:00:00.000Z",
                    "/tmp/shot.png",
                ),
            )
            connection.execute(
                """
                INSERT INTO sessions (
                  id,
                  started_at,
                  apps_json,
                  topics_json,
                  event_ids_json
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "session_v1",
                    "2026-06-09T06:00:00.000Z",
                    "[]",
                    "[]",
                    "[]",
                ),
            )
    finally:
        connection.close()


def _create_v3_database(database_path):
    connection = sqlite3.connect(database_path)
    try:
        ensure_migration_table(connection)
        with connection:
            for migration in MIGRATIONS[:3]:
                connection.executescript(migration.sql)
                connection.execute(
                    """
                    INSERT INTO schema_migrations (version, name)
                    VALUES (?, ?)
                    """,
                    (migration.version, migration.name),
                )
            connection.execute(
                """
                INSERT INTO vlm_jobs (
                  id,
                  type,
                  target_id,
                  metadata_json
                )
                VALUES (?, ?, ?, ?)
                """,
                ("ocr_job_legacy", "frame_ocr", "screen_frame_1", "{}"),
            )
    finally:
        connection.close()


def test_read_applied_version_returns_zero_for_missing_db(tmp_path):
    assert read_applied_version(tmp_path / "melone.sqlite") == 0


def test_read_applied_version_reads_latest_applied_version(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)

    assert read_applied_version(database_path) == 5


def test_read_applied_version_returns_zero_without_migration_table(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("CREATE TABLE unrelated (x INTEGER)")
        connection.commit()
    finally:
        connection.close()

    assert read_applied_version(database_path) == 0


def test_read_applied_version_returns_zero_for_corrupted_db(tmp_path):
    # Garbage bytes are not a SQLite database; the read must fall back to 0.
    database_path = tmp_path / "melone.sqlite"
    database_path.write_bytes(b"\xffgarbage bytes, not sqlite\x00" * 32)

    assert read_applied_version(database_path) == 0
