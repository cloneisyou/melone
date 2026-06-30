from melone_service.pipeline.screen_sessions import ScreenContext, ScreenSessionizer
from melone_service.store.db import connect, initialize_database
from melone_service.store.screen import ScreenRepository
from melone_service.store.ocr_jobs import OcrJobRepository


NOW = "2026-06-09T06:00:00.000Z"
LATER = "2026-06-09T06:01:00.000Z"
FUTURE = "2026-06-09T06:02:00.000Z"


def test_repeated_input_for_same_url_reuses_one_open_session(tmp_path):
    connection = _connection(tmp_path)
    try:
        screen_repository = ScreenRepository(connection)
        sessionizer = _sessionizer(connection)

        first_session = sessionizer.accept_latest_context(
            _context(
                timestamp=NOW,
                window_title="Docs",
                url="https://example.com/docs?utm_source=newsletter#intro",
            )
        )
        second_session = sessionizer.accept_latest_context(
            _context(
                timestamp=LATER,
                window_title="Updated Docs",
                url="https://example.com/docs",
            )
        )

        assert second_session.id == first_session.id
        assert second_session.status == "open"
        assert second_session.started_at == NOW
        assert second_session.window_title == "Updated Docs"
        assert screen_repository.count_sessions() == 1
        assert _count_jobs(connection, job_type="session_finalize") == 0
    finally:
        connection.close()


def test_url_change_closes_previous_session_and_opens_new_one(tmp_path):
    connection = _connection(tmp_path)
    try:
        screen_repository = ScreenRepository(connection)
        sessionizer = _sessionizer(connection)

        first_session = sessionizer.accept_latest_context(
            _context(
                timestamp=NOW,
                window_title="Docs",
                url="https://example.com/docs",
            )
        )
        second_session = sessionizer.accept_latest_context(
            _context(
                timestamp=LATER,
                window_title="Blog",
                url="https://example.com/blog",
            )
        )

        stored_first_session = screen_repository.get_session(first_session.id)
        assert stored_first_session is not None
        assert stored_first_session.status == "closed"
        assert stored_first_session.ended_at == LATER
        assert second_session.id != first_session.id
        assert second_session.status == "open"
        assert screen_repository.count_sessions() == 2

        finalize_jobs = _jobs(connection, job_type="session_finalize")
        assert len(finalize_jobs) == 1
        assert finalize_jobs[0]["target_id"] == first_session.id
        assert finalize_jobs[0]["session_id"] == first_session.id
        assert finalize_jobs[0]["source_key"] == first_session.source_key
        assert finalize_jobs[0]["retrieval_locator"] == first_session.retrieval_locator
    finally:
        connection.close()


def test_url_less_contexts_use_app_window_and_app_fallback_locators(tmp_path):
    connection = _connection(tmp_path)
    try:
        sessionizer = _sessionizer(connection)

        app_window_session = sessionizer.accept_latest_context(
            _context(
                timestamp=NOW,
                app_name="Cursor",
                bundle_id="com.todesktop.230313mzl4w4u92",
                window_title="  melone    status   ",
                url=None,
            )
        )
        app_session = sessionizer.accept_latest_context(
            _context(
                timestamp=LATER,
                app_name="Google Chrome",
                bundle_id="com.google.Chrome",
                window_title="New Tab",
                url=None,
            )
        )

        assert app_window_session.source_key == "app_window:cursor:melone status"
        assert app_window_session.retrieval_locator == (
            "app_window:cursor:melone status"
        )
        assert app_session.source_key == "app:google chrome"
        assert app_session.retrieval_locator == "app:google chrome"
    finally:
        connection.close()


def test_github_grouped_urls_keep_source_key_and_locator_distinct(tmp_path):
    connection = _connection(tmp_path)
    try:
        screen_repository = ScreenRepository(connection)
        sessionizer = _sessionizer(connection)

        pull_session = sessionizer.accept_latest_context(
            _context(
                timestamp=NOW,
                window_title="Pull request - melone",
                url="https://github.com/cloneisyou/melone/pull/1",
            )
        )
        issue_session = sessionizer.accept_latest_context(
            _context(
                timestamp=LATER,
                window_title="Issue - melone",
                url="https://github.com/cloneisyou/melone/issues/3",
            )
        )

        assert pull_session.source_key == "github:repo:cloneisyou/melone"
        assert issue_session.source_key == "github:repo:cloneisyou/melone"
        assert pull_session.retrieval_locator == (
            "url:https://github.com/cloneisyou/melone/pull/1"
        )
        assert issue_session.retrieval_locator == (
            "url:https://github.com/cloneisyou/melone/issues/3"
        )
        assert pull_session.retrieval_locator != issue_session.retrieval_locator
        assert screen_repository.count_sessions() == 2
        assert _count_jobs(connection, job_type="session_finalize") == 1
    finally:
        connection.close()


def test_closing_current_session_creates_exactly_one_finalize_job(tmp_path):
    connection = _connection(tmp_path)
    try:
        sessionizer = _sessionizer(connection)
        session = sessionizer.accept_latest_context(
            _context(
                timestamp=NOW,
                window_title="Docs",
                url="https://example.com/docs",
            )
        )

        closed_session = sessionizer.close_current_session(ended_at=LATER)
        second_close = sessionizer.close_current_session(ended_at=FUTURE)

        assert closed_session is not None
        assert closed_session.id == session.id
        assert closed_session.status == "closed"
        assert closed_session.ended_at == LATER
        assert second_close is None

        finalize_jobs = _jobs(connection, job_type="session_finalize")
        assert len(finalize_jobs) == 1
        assert finalize_jobs[0]["target_id"] == session.id
        assert finalize_jobs[0]["next_run_at"] == LATER
        assert finalize_jobs[0]["metadata_json"] == (
            '{"closed_at": "2026-06-09T06:01:00.000Z"}'
        )
    finally:
        connection.close()


def _connection(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)
    return connect(database_path)


def _sessionizer(connection):
    return ScreenSessionizer(
        screen_repository=ScreenRepository(connection),
        job_repository=OcrJobRepository(connection),
    )


def _context(
    *,
    timestamp,
    app_name="Google Chrome",
    bundle_id="com.google.Chrome",
    window_title,
    url,
):
    return ScreenContext(
        app_name=app_name,
        bundle_id=bundle_id,
        window_title=window_title,
        url=url,
        timestamp=timestamp,
    )


def _jobs(connection, *, job_type):
    return connection.execute(
        """
        SELECT *
        FROM ocr_jobs
        WHERE type = ?
        ORDER BY created_at, id
        """,
        (job_type,),
    ).fetchall()


def _count_jobs(connection, *, job_type):
    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM ocr_jobs
        WHERE type = ?
        """,
        (job_type,),
    ).fetchone()
    return int(row[0])
