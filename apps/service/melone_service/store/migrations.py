import sqlite3
from dataclasses import dataclass
from pathlib import Path


OCR_FTS_TOKENIZER = "trigram"
# FTS5 trigram works for mixed Korean/English screen text without an external
# tokenizer, which keeps the MVP searchable on a plain SQLite install.


@dataclass(frozen=True)
class Migration:
    # 스키마 변경 한 건을 버전, 이름, SQL 본문으로 묶어 관리합니다.
    version: int
    name: str
    sql: str


@dataclass(frozen=True)
class MigrationStatus:
    # CLI status와 초기화 로직에서 현재/최신/대기 버전을 함께 전달합니다.
    current_version: int
    latest_version: int
    pending_versions: tuple[int, ...]

    @property
    def is_current(self) -> bool:
        # 대기 중인 마이그레이션이 없으면 DB 스키마가 최신입니다.
        return not self.pending_versions


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="create_mvp_schema",
        sql="""
        CREATE TABLE IF NOT EXISTS events (
          id TEXT PRIMARY KEY,
          timestamp TEXT NOT NULL,
          type TEXT NOT NULL,
          app_name TEXT,
          bundle_id TEXT,
          pid INTEGER,
          window_title TEXT,
          url TEXT,
          source TEXT NOT NULL,
          metadata_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_events_timestamp
          ON events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_events_type_timestamp
          ON events(type, timestamp);

        CREATE TABLE IF NOT EXISTS screenshots (
          id TEXT PRIMARY KEY,
          event_id TEXT NOT NULL,
          timestamp TEXT NOT NULL,
          app_name TEXT,
          window_title TEXT,
          url TEXT,
          image_path TEXT NOT NULL,
          ocr_text TEXT,
          summary TEXT,
          importance_score REAL NOT NULL DEFAULT 0,
          FOREIGN KEY (event_id) REFERENCES events(id)
        );

        CREATE INDEX IF NOT EXISTS idx_screenshots_timestamp
          ON screenshots(timestamp);
        CREATE INDEX IF NOT EXISTS idx_screenshots_event_id
          ON screenshots(event_id);

        CREATE TABLE IF NOT EXISTS sessions (
          id TEXT PRIMARY KEY,
          started_at TEXT NOT NULL,
          ended_at TEXT,
          title TEXT,
          summary TEXT,
          apps_json TEXT NOT NULL,
          topics_json TEXT NOT NULL,
          event_ids_json TEXT NOT NULL,
          confidence REAL NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_started_at
          ON sessions(started_at);
        CREATE INDEX IF NOT EXISTS idx_sessions_ended_at
          ON sessions(ended_at);
        """,
    ),
    Migration(
        version=2,
        name="add_screen_search_schema",
        sql=f"""
        CREATE TABLE IF NOT EXISTS screen_sessions (
          id TEXT PRIMARY KEY,
          source_key TEXT NOT NULL,
          retrieval_locator TEXT NOT NULL,
          app_name TEXT,
          bundle_id TEXT,
          window_title TEXT,
          url TEXT,
          started_at TEXT NOT NULL,
          ended_at TEXT,
          status TEXT NOT NULL DEFAULT 'open'
            CHECK (status IN ('open', 'closed', 'finalized')),
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
          updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );

        CREATE INDEX IF NOT EXISTS idx_screen_sessions_source_key
          ON screen_sessions(source_key);
        CREATE INDEX IF NOT EXISTS idx_screen_sessions_retrieval_locator
          ON screen_sessions(retrieval_locator);
        CREATE INDEX IF NOT EXISTS idx_screen_sessions_status_started_at
          ON screen_sessions(status, started_at);

        CREATE TABLE IF NOT EXISTS screen_frames (
          id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          captured_at TEXT NOT NULL,
          image_path TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          perceptual_hash TEXT,
          diff_score REAL,
          width INTEGER NOT NULL CHECK (width >= 0),
          height INTEGER NOT NULL CHECK (height >= 0),
          status TEXT NOT NULL DEFAULT 'captured'
            CHECK (status IN ('captured', 'selected', 'skipped')),
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
          FOREIGN KEY (session_id) REFERENCES screen_sessions(id) ON DELETE CASCADE,
          UNIQUE (session_id, sha256)
        );

        CREATE INDEX IF NOT EXISTS idx_screen_frames_session_captured_at
          ON screen_frames(session_id, captured_at);
        CREATE INDEX IF NOT EXISTS idx_screen_frames_sha256
          ON screen_frames(sha256);

        CREATE TABLE IF NOT EXISTS vlm_jobs (
          id TEXT PRIMARY KEY,
          type TEXT NOT NULL
            CHECK (type IN ('session_finalize', 'frame_ocr', 'crop_ocr', 'index_refresh')),
          target_id TEXT NOT NULL,
          session_id TEXT,
          frame_id TEXT,
          source_key TEXT,
          retrieval_locator TEXT,
          priority INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'running', 'done', 'retryable_failed', 'dead')),
          attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
          next_run_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
          locked_at TEXT,
          last_error TEXT,
          metadata_json TEXT NOT NULL DEFAULT '{{}}',
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
          updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
          FOREIGN KEY (session_id) REFERENCES screen_sessions(id) ON DELETE CASCADE,
          FOREIGN KEY (frame_id) REFERENCES screen_frames(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_vlm_jobs_due
          ON vlm_jobs(status, next_run_at, priority, created_at);
        CREATE INDEX IF NOT EXISTS idx_vlm_jobs_session_id
          ON vlm_jobs(session_id);
        CREATE INDEX IF NOT EXISTS idx_vlm_jobs_frame_id
          ON vlm_jobs(frame_id);
        CREATE INDEX IF NOT EXISTS idx_vlm_jobs_source_key
          ON vlm_jobs(source_key);
        CREATE INDEX IF NOT EXISTS idx_vlm_jobs_retrieval_locator
          ON vlm_jobs(retrieval_locator);

        CREATE TABLE IF NOT EXISTS ocr_chunks (
          id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          frame_id TEXT NOT NULL,
          source_key TEXT NOT NULL,
          retrieval_locator TEXT NOT NULL,
          app_name TEXT,
          window_title TEXT,
          url TEXT,
          crop_bbox_json TEXT,
          text TEXT NOT NULL,
          text_hash TEXT NOT NULL,
          provider TEXT,
          model TEXT,
          latency_ms INTEGER,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
          FOREIGN KEY (session_id) REFERENCES screen_sessions(id) ON DELETE CASCADE,
          FOREIGN KEY (frame_id) REFERENCES screen_frames(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_ocr_chunks_session_id
          ON ocr_chunks(session_id);
        CREATE INDEX IF NOT EXISTS idx_ocr_chunks_frame_id
          ON ocr_chunks(frame_id);
        CREATE INDEX IF NOT EXISTS idx_ocr_chunks_source_key
          ON ocr_chunks(source_key);
        CREATE INDEX IF NOT EXISTS idx_ocr_chunks_retrieval_locator
          ON ocr_chunks(retrieval_locator);
        CREATE INDEX IF NOT EXISTS idx_ocr_chunks_text_hash
          ON ocr_chunks(text_hash);

        CREATE VIRTUAL TABLE IF NOT EXISTS ocr_chunks_fts USING fts5(
          chunk_id UNINDEXED,
          source_key UNINDEXED,
          retrieval_locator UNINDEXED,
          title,
          app_name,
          text,
          tokenize='{OCR_FTS_TOKENIZER}'
        );

        CREATE TABLE IF NOT EXISTS context_rank_scores (
          source_key TEXT PRIMARY KEY,
          score REAL NOT NULL,
          visits INTEGER NOT NULL DEFAULT 0 CHECK (visits >= 0),
          retrieval_locators_json TEXT NOT NULL DEFAULT '[]',
          computed_at TEXT NOT NULL,
          model_version TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
          updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );

        CREATE INDEX IF NOT EXISTS idx_context_rank_scores_computed_at
          ON context_rank_scores(computed_at);

        DROP TABLE IF EXISTS screenshots;
        DROP TABLE IF EXISTS sessions;
        """,
    ),
    Migration(
        version=3,
        name="add_screen_frame_image_retention",
        sql="""
        ALTER TABLE screen_frames
          ADD COLUMN image_retention_state TEXT NOT NULL DEFAULT 'retained'
            CHECK (
              image_retention_state IN (
                'retained',
                'retained_for_ocr',
                'retained_for_retry',
                'retained_after_dead_job',
                'delete_pending_after_indexing',
                'deleted_after_indexing',
                'missing_after_indexing',
                'delete_failed_after_indexing'
              )
            );

        ALTER TABLE screen_frames
          ADD COLUMN image_retention_updated_at TEXT;
        """,
    ),
    Migration(
        version=4,
        name="rename_vlm_jobs_to_ocr_jobs",
        sql="""
        ALTER TABLE vlm_jobs RENAME TO ocr_jobs;

        DROP INDEX IF EXISTS idx_vlm_jobs_due;
        DROP INDEX IF EXISTS idx_vlm_jobs_session_id;
        DROP INDEX IF EXISTS idx_vlm_jobs_frame_id;
        DROP INDEX IF EXISTS idx_vlm_jobs_source_key;
        DROP INDEX IF EXISTS idx_vlm_jobs_retrieval_locator;

        CREATE INDEX IF NOT EXISTS idx_ocr_jobs_due
          ON ocr_jobs(status, next_run_at, priority, created_at);
        CREATE INDEX IF NOT EXISTS idx_ocr_jobs_session_id
          ON ocr_jobs(session_id);
        CREATE INDEX IF NOT EXISTS idx_ocr_jobs_frame_id
          ON ocr_jobs(frame_id);
        CREATE INDEX IF NOT EXISTS idx_ocr_jobs_source_key
          ON ocr_jobs(source_key);
        CREATE INDEX IF NOT EXISTS idx_ocr_jobs_retrieval_locator
          ON ocr_jobs(retrieval_locator);
        """,
    ),
    Migration(
        version=5,
        name="add_ocr_chunk_embedding_cache",
        sql="""
        CREATE TABLE IF NOT EXISTS ocr_chunk_embeddings (
          chunk_id TEXT NOT NULL,
          model TEXT NOT NULL,
          dimension INTEGER NOT NULL CHECK (dimension > 0),
          text_hash TEXT NOT NULL,
          embedding BLOB NOT NULL,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
          updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
          PRIMARY KEY (chunk_id, model, dimension),
          FOREIGN KEY (chunk_id) REFERENCES ocr_chunks(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_ocr_chunk_embeddings_model_dimension
          ON ocr_chunk_embeddings(model, dimension);
        CREATE INDEX IF NOT EXISTS idx_ocr_chunk_embeddings_text_hash
          ON ocr_chunk_embeddings(text_hash);
        """,
    ),
)


def latest_version() -> int:
    # 등록된 마이그레이션이 없을 때도 0을 반환해 초기 상태를 표현합니다.
    return max((migration.version for migration in MIGRATIONS), default=0)


def ensure_migration_table(connection: sqlite3.Connection) -> None:
    # 어떤 스키마가 적용됐는지 기록하는 내부 테이블을 보장합니다.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )


def applied_versions(connection: sqlite3.Connection) -> set[int]:
    # 이미 적용된 버전을 set으로 반환해 중복 실행을 막습니다.
    ensure_migration_table(connection)
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {int(row[0]) for row in rows}


def get_migration_status(connection: sqlite3.Connection) -> MigrationStatus:
    # 현재 DB 상태를 계산해 CLI와 초기화 코드가 같은 기준으로 판단하게 합니다.
    applied = applied_versions(connection)
    migration_versions = tuple(migration.version for migration in MIGRATIONS)
    pending = tuple(version for version in migration_versions if version not in applied)
    current = max(applied, default=0)
    return MigrationStatus(
        current_version=current,
        latest_version=latest_version(),
        pending_versions=pending,
    )


def read_applied_version(database_path: Path) -> int:
    """Read the latest applied migration version without writing.

    get_migration_status guarantees creation of the schema_migrations table
    (a write), so polling status paths read the table directly over a
    read-only connection instead. Returns 0 when the DB file or table is
    missing, or the file is not a readable SQLite database (corruption).
    """
    # Imported lazily: store.db imports this module at top level.
    from melone_service.store.db import connect_readonly

    if not database_path.exists():
        return 0

    try:
        connection = connect_readonly(database_path)
    except sqlite3.Error:
        return 0
    try:
        row = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
    except sqlite3.Error:
        return 0
    finally:
        connection.close()
    return int(row[0]) if row is not None and row[0] is not None else 0


def run_migrations(connection: sqlite3.Connection) -> MigrationStatus:
    # 아직 적용되지 않은 마이그레이션만 트랜잭션 단위로 실행합니다.
    ensure_migration_table(connection)
    applied = applied_versions(connection)

    for migration in MIGRATIONS:
        if migration.version in applied:
            continue

        with connection:
            connection.executescript(migration.sql)
            connection.execute(
                """
                INSERT INTO schema_migrations (version, name)
                VALUES (?, ?)
                """,
                (migration.version, migration.name),
            )

    return get_migration_status(connection)
