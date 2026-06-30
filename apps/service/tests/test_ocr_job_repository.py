from melone_service.store.db import connect, initialize_database
from melone_service.store.ocr_jobs import OcrJobRepository
from melone_service.ocr import OcrTimeoutError, OcrUnavailableError


NOW = "2026-06-09T06:00:00.000Z"
RETRY_AT = "2026-06-09T06:01:00.000Z"
LATER = "2026-06-09T06:02:00.000Z"
FUTURE = "2026-06-09T06:03:00.000Z"


def test_create_pending_job_persists_worker_contract_fields(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)

    connection = connect(database_path)
    try:
        repository = OcrJobRepository(connection)
        _insert_screen_session_and_frame(connection)

        job = repository.create_pending_job(
            job_id="ocr_job_1",
            job_type="frame_ocr",
            target_id="screen_frame_1",
            session_id="screen_session_1",
            frame_id="screen_frame_1",
            source_key="github:repo:cloneisyou/melone",
            retrieval_locator="url:https://github.com/cloneisyou/melone/pulls",
            priority=7,
            next_run_at=RETRY_AT,
            metadata={"reason": "keyframe"},
            now=NOW,
        )

        assert job.id == "ocr_job_1"
        assert job.job_type == "frame_ocr"
        assert job.target_id == "screen_frame_1"
        assert job.status == "pending"
        assert job.attempts == 0
        assert job.next_run_at == RETRY_AT
        assert job.locked_at is None
        assert job.last_error is None
        assert job.metadata == {"reason": "keyframe"}
        assert repository.count_jobs(status="pending") == 1
    finally:
        connection.close()


def _insert_screen_session_and_frame(connection):
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
                NOW,
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
                NOW,
                "/tmp/frame.png",
                "abc123",
                1280,
                720,
            ),
        )


def test_only_one_repository_instance_locks_a_pending_due_job(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)

    first_connection = connect(database_path)
    second_connection = connect(database_path)
    try:
        first_repository = OcrJobRepository(first_connection)
        second_repository = OcrJobRepository(second_connection)
        first_repository.create_pending_job(
            job_id="ocr_job_1",
            job_type="frame_ocr",
            target_id="screen_frame_1",
            now=NOW,
        )

        first_lock = first_repository.lock_due_job(now=NOW)
        second_lock = second_repository.lock_due_job(now=NOW)
        stored_job = second_repository.get_job("ocr_job_1")

        assert first_lock is not None
        assert first_lock.id == "ocr_job_1"
        assert first_lock.status == "running"
        assert first_lock.locked_at == NOW
        assert second_lock is None
        assert stored_job is not None
        assert stored_job.status == "running"
    finally:
        first_connection.close()
        second_connection.close()


def test_done_transition_clears_lock_and_error(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)

    connection = connect(database_path)
    try:
        repository = OcrJobRepository(connection)
        repository.create_pending_job(
            job_id="ocr_job_1",
            job_type="frame_ocr",
            target_id="screen_frame_1",
            now=NOW,
        )
        assert repository.lock_due_job(now=NOW) is not None

        done_job = repository.mark_done("ocr_job_1", now=LATER)

        assert done_job is not None
        assert done_job.status == "done"
        assert done_job.attempts == 0
        assert done_job.locked_at is None
        assert done_job.last_error is None
        assert done_job.updated_at == LATER
        assert repository.lock_due_job(now=FUTURE) is None
    finally:
        connection.close()


def test_retryable_failure_updates_attempts_next_run_and_last_error(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)

    connection = connect(database_path)
    try:
        repository = OcrJobRepository(connection)
        repository.create_pending_job(
            job_id="ocr_job_1",
            job_type="frame_ocr",
            target_id="screen_frame_1",
            now=NOW,
        )
        assert repository.lock_due_job(now=NOW) is not None

        failed_job = repository.mark_retryable_failure(
            "ocr_job_1",
            error=OcrUnavailableError("vLLM is unavailable"),
            now=NOW,
            next_run_at=RETRY_AT,
            max_attempts=3,
        )

        assert failed_job is not None
        assert failed_job.status == "retryable_failed"
        assert failed_job.attempts == 1
        assert failed_job.next_run_at == RETRY_AT
        assert failed_job.locked_at is None
        assert failed_job.last_error == "OcrUnavailableError: vLLM is unavailable"
    finally:
        connection.close()


def test_retryable_failure_moves_job_to_dead_after_max_attempts(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)

    connection = connect(database_path)
    try:
        repository = OcrJobRepository(connection)
        repository.create_pending_job(
            job_id="ocr_job_1",
            job_type="frame_ocr",
            target_id="screen_frame_1",
            now=NOW,
        )
        assert repository.lock_due_job(now=NOW) is not None
        first_failure = repository.mark_retryable_failure(
            "ocr_job_1",
            error=OcrTimeoutError("timed out"),
            now=NOW,
            next_run_at=RETRY_AT,
            max_attempts=2,
        )
        assert first_failure is not None
        assert first_failure.status == "retryable_failed"
        assert first_failure.attempts == 1

        assert repository.lock_due_job(now=RETRY_AT) is not None
        dead_job = repository.mark_retryable_failure(
            "ocr_job_1",
            error=OcrTimeoutError("timed out again"),
            now=LATER,
            next_run_at=FUTURE,
            max_attempts=2,
        )

        assert dead_job is not None
        assert dead_job.status == "dead"
        assert dead_job.attempts == 2
        assert dead_job.next_run_at == LATER
        assert dead_job.locked_at is None
        assert dead_job.last_error == "OcrTimeoutError: timed out again"
        assert repository.lock_due_job(now=FUTURE) is None
    finally:
        connection.close()
