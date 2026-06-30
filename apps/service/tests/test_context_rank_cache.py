from datetime import datetime, timedelta, timezone

import pytest

from melone_service.pipeline.context_rank_cache import (
    CONTEXT_RANK_CACHE_MODEL_VERSION,
    ContextRankCacheRefresher,
)
from melone_service.pipeline.normalizer import normalize_event
from melone_service.store.context_rank import (
    ContextRankRepository,
    stable_retrieval_locators_json,
)
from melone_service.store.db import connect, initialize_database
from melone_service.store.events import EventRepository


NOW = datetime(2026, 6, 9, 6, 0, 0, tzinfo=timezone.utc)


def test_refresh_context_rank_cache_creates_rows_from_fixture_events(tmp_path):
    connection = _connection(tmp_path)
    try:
        events = EventRepository(connection)
        events.insert(_context_event("Cursor", "context_graph.py - melone", 0))
        events.insert(
            _url_event(
                "Pull request - melone",
                "https://github.com/cloneisyou/melone/pull/1",
                1,
            )
        )
        events.insert(_activity_event("keyboard_burst", 2, metadata={"key_count": 6}))

        ranks = ContextRankRepository(connection)
        result = ContextRankCacheRefresher(events, ranks).refresh(now=NOW)
        score_count = ranks.count_scores()
        rows = ranks.list_scores()
    finally:
        connection.close()

    assert result.upserted_count == 2
    assert result.event_count == 3
    assert score_count == 2
    assert {row.source_key for row in rows} == {
        "app_window:cursor:context_graph.py - melone",
        "github:repo:cloneisyou/melone",
    }
    assert all(row.score > 0 for row in rows)
    assert all(row.computed_at == "2026-06-09T06:00:00.000Z" for row in rows)
    assert all(row.model_version == CONTEXT_RANK_CACHE_MODEL_VERSION for row in rows)


def test_refresh_context_rank_cache_upserts_existing_source_key(tmp_path):
    connection = _connection(tmp_path)
    try:
        events = EventRepository(connection)
        ranks = ContextRankRepository(connection)
        events.insert(
            _url_event(
                "Pull request - melone",
                "https://github.com/cloneisyou/melone/pull/1",
                0,
            )
        )
        ContextRankCacheRefresher(events, ranks).refresh(now=NOW)

        events.insert(
            _context_event(
                "Cursor",
                "context_rank_cache.py - melone",
                1,
            )
        )
        events.insert(
            _url_event(
                "Issue - melone",
                "https://github.com/cloneisyou/melone/issues/3",
                2,
            )
        )
        result = ContextRankCacheRefresher(events, ranks).refresh(now=NOW)
        score_count = ranks.count_scores()
        github_row = ranks.get_score("github:repo:cloneisyou/melone")
    finally:
        connection.close()

    assert result.upserted_count == 2
    assert score_count == 2
    assert github_row is not None
    assert github_row.visits == 2
    assert github_row.retrieval_locators == (
        "url:https://github.com/cloneisyou/melone/issues/3",
        "url:https://github.com/cloneisyou/melone/pull/1",
    )


def test_refresh_context_rank_cache_excludes_hidden_contexts_by_default(tmp_path):
    connection = _connection(tmp_path)
    try:
        events = EventRepository(connection)
        events.insert(_context_event("Cursor", "A", 0))
        events.insert(_context_event("Google Chrome", "New Tab", 1))
        events.insert(_context_event("Cursor", "B", 2))

        ranks = ContextRankRepository(connection)
        result = ContextRankCacheRefresher(events, ranks).refresh(now=NOW)
    finally:
        connection.close()

    assert result.upserted_count == 2
    assert "app:google chrome" not in {score.source_key for score in result.scores}


def test_refresh_context_rank_cache_uses_recent_event_window(tmp_path):
    connection = _connection(tmp_path)
    try:
        events = EventRepository(connection)
        events.insert(_context_event("Old App", "Old", 0))
        events.insert(_context_event("Cursor", "A", 1))
        events.insert(_context_event("Cursor", "B", 2))

        ranks = ContextRankRepository(connection)
        ContextRankCacheRefresher(events, ranks).refresh(
            event_limit=2,
            now=NOW,
        )
        source_keys = {row.source_key for row in ranks.list_scores()}
    finally:
        connection.close()

    assert source_keys == {
        "app_window:cursor:A",
        "app_window:cursor:B",
    }


def test_stable_retrieval_locators_json_sorts_and_dedupes():
    assert stable_retrieval_locators_json(
        (
            "url:https://example.com/b",
            "url:https://example.com/a",
            "url:https://example.com/b",
            "",
        )
    ) == '["url:https://example.com/a","url:https://example.com/b"]'


def test_refresh_context_rank_cache_rejects_invalid_event_limit(tmp_path):
    connection = _connection(tmp_path)
    try:
        refresher = ContextRankCacheRefresher(
            EventRepository(connection),
            ContextRankRepository(connection),
        )

        with pytest.raises(ValueError, match="event_limit"):
            refresher.refresh(event_limit=0, now=NOW)
    finally:
        connection.close()


def test_refresh_if_due_recomputes_on_first_run(tmp_path):
    connection = _connection(tmp_path)
    try:
        events = EventRepository(connection)
        events.insert(_context_event("Cursor", "A", 0))
        ranks = ContextRankRepository(connection)

        result = ContextRankCacheRefresher(events, ranks).refresh_if_due(
            min_interval_seconds=30.0,
            now=NOW,
        )
        score_count = ranks.count_scores()
    finally:
        connection.close()

    assert result.recomputed is True
    assert score_count == 1


def test_refresh_if_due_skips_when_no_new_events(tmp_path):
    connection = _connection(tmp_path)
    try:
        events = EventRepository(connection)
        events.insert(_context_event("Cursor", "A", 0))
        ranks = ContextRankRepository(connection)
        refresher = ContextRankCacheRefresher(events, ranks)
        refresher.refresh_if_due(min_interval_seconds=30.0, now=NOW)

        # Interval elapsed but no new relevant event arrived → skip.
        result = refresher.refresh_if_due(
            min_interval_seconds=30.0,
            now=NOW + timedelta(seconds=60),
        )
        rows = ranks.list_scores()
    finally:
        connection.close()

    assert result.recomputed is False
    assert result.upserted_count == 0
    # Existing cache is preserved, not cleared.
    assert {row.source_key for row in rows} == {"app_window:cursor:A"}


def test_refresh_if_due_skips_within_min_interval(tmp_path):
    connection = _connection(tmp_path)
    try:
        events = EventRepository(connection)
        events.insert(_context_event("Cursor", "A", 0))
        ranks = ContextRankRepository(connection)
        refresher = ContextRankCacheRefresher(events, ranks)
        refresher.refresh_if_due(min_interval_seconds=30.0, now=NOW)

        # New event exists, but the cadence floor has not elapsed → skip.
        events.insert(_context_event("Cursor", "B", 2))
        result = refresher.refresh_if_due(
            min_interval_seconds=30.0,
            now=NOW + timedelta(seconds=5),
        )
        source_keys = {row.source_key for row in ranks.list_scores()}
    finally:
        connection.close()

    assert result.recomputed is False
    assert source_keys == {"app_window:cursor:A"}


def test_refresh_if_due_recomputes_after_interval_with_new_events(tmp_path):
    connection = _connection(tmp_path)
    try:
        events = EventRepository(connection)
        events.insert(_context_event("Cursor", "A", 0))
        ranks = ContextRankRepository(connection)
        refresher = ContextRankCacheRefresher(events, ranks)
        refresher.refresh_if_due(min_interval_seconds=30.0, now=NOW)

        events.insert(_context_event("Cursor", "B", 40))
        result = refresher.refresh_if_due(
            min_interval_seconds=30.0,
            now=NOW + timedelta(seconds=60),
        )
        source_keys = {row.source_key for row in ranks.list_scores()}
    finally:
        connection.close()

    assert result.recomputed is True
    assert source_keys == {"app_window:cursor:A", "app_window:cursor:B"}


def test_refresh_if_due_first_run_matches_direct_refresh(tmp_path):
    # Gating must not change results: a first-run refresh_if_due produces the
    # same scores as a direct refresh on identical events.
    fixture = [
        _context_event("Cursor", "A", 0),
        _url_event("PR", "https://github.com/cloneisyou/melone/pull/1", 1),
        _context_event("Cursor", "B", 2),
    ]

    gated = _connection(tmp_path / "gated")
    direct = _connection(tmp_path / "direct")
    try:
        for event in fixture:
            EventRepository(gated).insert(event)
            EventRepository(direct).insert(event)

        gated_ranks = ContextRankRepository(gated)
        direct_ranks = ContextRankRepository(direct)
        ContextRankCacheRefresher(
            EventRepository(gated), gated_ranks
        ).refresh_if_due(min_interval_seconds=30.0, now=NOW)
        ContextRankCacheRefresher(
            EventRepository(direct), direct_ranks
        ).refresh(now=NOW)

        gated_scores = {row.source_key: row.score for row in gated_ranks.list_scores()}
        direct_scores = {
            row.source_key: row.score for row in direct_ranks.list_scores()
        }
    finally:
        gated.close()
        direct.close()

    assert gated_scores == direct_scores


def _connection(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)
    return connect(database_path)


def _context_event(app_name: str, window_title: str, seconds: int):
    return normalize_event(
        "active_app_snapshot",
        event_id=f"evt_context_{seconds}",
        timestamp=NOW.replace(second=seconds),
        app={"name": app_name},
        window={"title": window_title},
        source="test",
    )


def _url_event(window_title: str, url: str, seconds: int):
    return normalize_event(
        "current_asset_changed",
        event_id=f"evt_url_{seconds}",
        timestamp=NOW.replace(second=seconds),
        app={"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
        window={"title": window_title},
        url=url,
        source="test",
    )


def _activity_event(event_type: str, seconds: int, *, metadata=None):
    return normalize_event(
        event_type,
        event_id=f"evt_activity_{seconds}",
        timestamp=NOW.replace(second=seconds),
        source="test",
        metadata=metadata,
    )
