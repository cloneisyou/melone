from melone_service.store.context_rank import ContextRankRepository
from melone_service.store.db import connect, initialize_database
from melone_service.store.ocr import OcrChunkRepository
from melone_service.store.screen import ScreenRepository
from melone_service.store.ocr_jobs import OcrJobRepository


def test_screen_search_repository_skeletons_count_rows(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)

    connection = connect(database_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO screen_sessions (
                  id,
                  source_key,
                  retrieval_locator,
                  app_name,
                  window_title,
                  started_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "screen_session_1",
                    "github:repo:cloneisyou/melone",
                    "url:https://github.com/cloneisyou/melone/pulls",
                    "Google Chrome",
                    "Pull requests - melone",
                    "2026-06-09T06:00:00.000Z",
                ),
            )
            connection.execute(
                """
                INSERT INTO screen_frames (
                  id,
                  session_id,
                  captured_at,
                  image_path,
                  sha256,
                  width,
                  height
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "screen_frame_1",
                    "screen_session_1",
                    "2026-06-09T06:00:01.000Z",
                    "/tmp/screen.png",
                    "abc123",
                    1280,
                    720,
                ),
            )
            connection.execute(
                """
                INSERT INTO ocr_jobs (
                  id,
                  type,
                  target_id,
                  session_id,
                  frame_id,
                  source_key,
                  retrieval_locator
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ocr_job_1",
                    "frame_ocr",
                    "screen_frame_1",
                    "screen_session_1",
                    "screen_frame_1",
                    "github:repo:cloneisyou/melone",
                    "url:https://github.com/cloneisyou/melone/pulls",
                ),
            )
            connection.execute(
                """
                INSERT INTO ocr_chunks (
                  id,
                  session_id,
                  frame_id,
                  source_key,
                  retrieval_locator,
                  app_name,
                  window_title,
                  text,
                  text_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ocr_chunk_1",
                    "screen_session_1",
                    "screen_frame_1",
                    "github:repo:cloneisyou/melone",
                    "url:https://github.com/cloneisyou/melone/pulls",
                    "Google Chrome",
                    "Pull requests - melone",
                    "review screen search schema",
                    "text_hash_1",
                ),
            )
            connection.execute(
                """
                INSERT INTO ocr_chunks_fts (
                  chunk_id,
                  source_key,
                  retrieval_locator,
                  title,
                  app_name,
                  text
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "ocr_chunk_1",
                    "github:repo:cloneisyou/melone",
                    "url:https://github.com/cloneisyou/melone/pulls",
                    "Pull requests - melone",
                    "Google Chrome",
                    "review screen search schema",
                ),
            )
            connection.execute(
                """
                INSERT INTO context_rank_scores (
                  source_key,
                  score,
                  visits,
                  retrieval_locators_json,
                  computed_at,
                  model_version
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "github:repo:cloneisyou/melone",
                    0.8,
                    3,
                    '["url:https://github.com/cloneisyou/melone/pulls"]',
                    "2026-06-09T06:05:00.000Z",
                    "test_model_v1",
                ),
            )

        screen_repository = ScreenRepository(connection)
        job_repository = OcrJobRepository(connection)
        ocr_repository = OcrChunkRepository(connection)
        context_rank_repository = ContextRankRepository(connection)

        assert screen_repository.count_sessions() == 1
        assert screen_repository.count_frames() == 1
        assert job_repository.count_jobs() == 1
        assert job_repository.count_jobs(status="pending") == 1
        assert ocr_repository.count_chunks() == 1
        assert ocr_repository.count_fts_rows() == 1
        assert context_rank_repository.count_scores() == 1
    finally:
        connection.close()
