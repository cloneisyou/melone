import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import timedelta

import anyio
import pytest

from melone_service.mcp import tools
from melone_service.models import utc_now, utc_timestamp
from melone_service.pipeline.normalizer import normalize_event
from melone_service.queries import open_event_repository
from melone_service.store.ocr import OcrChunkRepository
from melone_service.store.screen import ScreenRepository


@pytest.fixture
def melone_home(tmp_path, monkeypatch):
    # MCP tools resolve paths via load_config(), so inject the test DB with MELONE_HOME.
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))
    return tmp_path


def _database_path(melone_home):
    return melone_home / "melone.sqlite"


def _context_event(app_name, window_title, now, seconds, url=None):
    return normalize_event(
        "active_app_snapshot",
        timestamp=now - timedelta(seconds=seconds),
        app={"name": app_name},
        window={"title": window_title},
        url=url,
        source="test",
    )


def _seed_ocr_chunk(
    connection,
    *,
    chunk_id,
    session_id,
    frame_id,
    source_key,
    retrieval_locator,
    text,
    started_at,
    ended_at=None,
):
    url = retrieval_locator.removeprefix("url:")
    screen_repository = ScreenRepository(connection)
    screen_repository.create_session(
        session_id=session_id,
        source_key=source_key,
        retrieval_locator=retrieval_locator,
        app_name="Chrome",
        bundle_id="com.google.Chrome",
        window_title="Docs",
        url=url,
        started_at=started_at,
        now=started_at,
    )
    if ended_at is not None:
        screen_repository.close_session(
            session_id,
            ended_at=ended_at,
            now=ended_at,
        )
    screen_repository.insert_frame(
        frame_id=frame_id,
        session_id=session_id,
        captured_at=started_at,
        image_path=f"/tmp/{frame_id}.png",
        sha256=hashlib.sha256(frame_id.encode()).hexdigest(),
        width=1280,
        height=720,
    )
    OcrChunkRepository(connection).insert_chunk_with_fts(
        chunk_id=chunk_id,
        session_id=session_id,
        frame_id=frame_id,
        source_key=source_key,
        retrieval_locator=retrieval_locator,
        app_name="Chrome",
        window_title="Docs",
        url=url,
        text=text,
        text_hash=hashlib.sha256(f"{chunk_id}:{text}".encode()).hexdigest(),
        created_at=ended_at or started_at,
    )
    connection.commit()


def test_get_current_context_returns_latest_context_and_activity(melone_home):
    now = utc_now()
    with open_event_repository(_database_path(melone_home)) as repository:
        repository.insert(
            normalize_event(
                "current_asset_changed",
                timestamp=now - timedelta(seconds=10),
                app={"name": "Safari"},
                window={"title": "Melone Docs"},
                url="https://example.com/melone",
                source="test",
            )
        )
        repository.insert(
            normalize_event(
                "keyboard_burst",
                timestamp=now - timedelta(seconds=5),
                source="test",
                metadata={"key_count": 4},
            )
        )

    result = tools.get_current_context()

    assert result == {
        "available": True,
        "app": "Safari",
        "window": "Melone Docs",
        "url": "https://example.com/melone",
        "activity": "active",
    }
    assert json.dumps(result)


def test_rank_contexts_returns_ranked_entries(melone_home):
    now = utc_now()
    with open_event_repository(_database_path(melone_home)) as repository:
        repository.insert(_context_event("Cursor", "tools.py - melone", now, 20))
        repository.insert(_context_event("Slack", "dev - Clone - Slack", now, 10))

    result = tools.rank_contexts()

    assert result["available"] is True
    labels = [item["label"] for item in result["contexts"]]
    assert "Cursor | tools.py - melone" in labels
    assert "Slack | dev - Clone - Slack" in labels
    for item in result["contexts"]:
        assert set(item) == {"score", "visits", "kind", "label"}
    assert json.dumps(result)


def test_rank_contexts_since_minutes_excludes_old_events(melone_home):
    now = utc_now()
    with open_event_repository(_database_path(melone_home)) as repository:
        repository.insert(
            _context_event("Old App", "Old Window", now, 10 * 60 * 60)
        )
        repository.insert(_context_event("Cursor", "fresh work", now, 30))

    labels = [
        item["label"]
        for item in tools.rank_contexts(since_minutes=120)["contexts"]
    ]

    assert "Cursor | fresh work" in labels
    assert "Old App | Old Window" not in labels


def test_rank_contexts_show_hidden_includes_bridge_context(melone_home):
    now = utc_now()
    with open_event_repository(_database_path(melone_home)) as repository:
        repository.insert(_context_event("Cursor", "A", now, 30))
        repository.insert(_context_event("Google Chrome", "New Tab", now, 20))
        repository.insert(_context_event("Cursor", "B", now, 10))

    default_labels = [
        item["label"] for item in tools.rank_contexts()["contexts"]
    ]
    hidden_labels = [
        item["label"]
        for item in tools.rank_contexts(show_hidden=True)["contexts"]
    ]

    assert "Google Chrome" not in default_labels
    assert "Google Chrome" in hidden_labels


def test_search_contexts_returns_results_and_episodes(melone_home):
    now = utc_now()
    with open_event_repository(_database_path(melone_home)) as repository:
        # Default since_minutes=1440 (24h), so 10-hour-old work must be searchable.
        repository.insert(
            _context_event("Notion", "melone spec", now, 10 * 60 * 60)
        )
        repository.insert(_context_event("Cursor", "melone tools", now, 20))
        repository.insert(_context_event("Slack", "general", now, 10))

    result = tools.search_contexts(query="melone")

    assert result["available"] is True
    labels = [item["label"] for item in result["results"]]
    assert "Cursor | melone tools" in labels
    assert "Notion | melone spec" in labels
    for item in result["results"]:
        assert set(item) == {
            "key", "kind", "label", "uri", "score", "visits", "lastSeenAt",
        }
    windows = [episode["window"] for episode in result["episodes"]]
    assert windows == ["melone tools", "melone spec"]  # newest first
    assert json.dumps(result)


def test_search_contexts_returns_ocr_matches(melone_home):
    now = utc_now()
    with open_event_repository(_database_path(melone_home)) as repository:
        _seed_ocr_chunk(
            repository.connection,
            chunk_id="ocr_chunk_mcp",
            session_id="screen_session_mcp",
            frame_id="screen_frame_mcp",
            source_key="url:https://example.com/mcp",
            retrieval_locator="url:https://example.com/mcp",
            text="MCP agent should retrieve this OCR receipt",
            started_at=utc_timestamp(now - timedelta(seconds=30)),
            ended_at=utc_timestamp(now - timedelta(seconds=20)),
        )

    result = tools.search_contexts(query="receipt")

    assert result["available"] is True
    assert result["results"][0]["matchSource"] == "ocr"
    assert result["results"][0]["uri"] == "https://example.com/mcp"
    assert "receipt" in result["results"][0]["snippet"]
    assert result["episodes"][0]["matchSource"] == "ocr"
    assert "receipt" in result["episodes"][0]["snippet"]
    assert json.dumps(result)


def test_search_contexts_reports_unavailable_without_database(melone_home):
    result = tools.search_contexts(query="melone")

    assert result["available"] is False
    assert "melone.sqlite" in result["reason"]
    assert result["results"] == []
    assert result["episodes"] == []
    assert not _database_path(melone_home).exists()


def test_get_timeline_returns_events_in_timestamp_order(melone_home):
    now = utc_now()
    with open_event_repository(_database_path(melone_home)) as repository:
        repository.insert(_context_event("Slack", "dev", now, 5))
        repository.insert(_context_event("Cursor", "melone", now, 10))
        repository.insert(
            _context_event("Old App", "Old Window", now, 10 * 60 * 60)
        )

    result = tools.get_timeline(since_minutes=60)

    assert result["available"] is True
    assert [item["app"] for item in result["events"]] == ["Cursor", "Slack"]
    for item in result["events"]:
        assert set(item) == {"timestamp", "type", "app", "window", "url"}
    assert json.dumps(result)


def test_get_timeline_limit_restricts_entries(melone_home):
    now = utc_now()
    with open_event_repository(_database_path(melone_home)) as repository:
        for seconds in range(3):
            repository.insert(_context_event("Cursor", f"W{seconds}", now, seconds))

    result = tools.get_timeline(limit=2)

    assert len(result["events"]) == 2


def test_tools_report_unavailable_without_creating_database(melone_home):
    # Read-only guarantee: without a DB, tools must not crash or create the file.
    current = tools.get_current_context()
    ranked = tools.rank_contexts()
    timeline = tools.get_timeline()

    for result in (current, ranked, timeline):
        assert result["available"] is False
        assert "melone.sqlite" in result["reason"]
    assert ranked["contexts"] == []
    assert timeline["events"] == []
    assert not _database_path(melone_home).exists()


def test_tools_report_unavailable_for_database_without_schema(melone_home):
    # An empty DB file (no schema) raises sqlite3 errors that must become guidance.
    sqlite3.connect(_database_path(melone_home)).close()

    for result in (
        tools.get_current_context(),
        tools.rank_contexts(),
        tools.get_timeline(),
    ):
        assert result["available"] is False
        assert "OperationalError" in result["reason"]


def test_mcp_modules_import_without_main_module():
    # main.py cannot be imported on Windows (fcntl), so verify in an isolated
    # process that the MCP server never pulls it in through any path.
    code = (
        "import sys; "
        "import melone_service.mcp.server; import melone_service.mcp.tools; "
        "assert 'melone_service.main' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_search_contexts_tool_docstring_describes_semantic_fallback():
    from melone_service.mcp.server import search_contexts as tool

    description = tool.__doc__ or ""

    assert "natural-language" in description
    assert "BM25" in description
    assert "semantic screen-text" in description
    assert "falls back" in description
    assert 'semantic screen-text matches also report as "ocr"' in description


def test_mcp_server_handshake_and_tool_call_in_process(melone_home):
    # Verifies the initialize..tools/call round trip over an in-memory transport.
    from mcp.shared.memory import create_connected_server_and_client_session

    from melone_service.mcp.server import mcp as server

    async def scenario():
        async with create_connected_server_and_client_session(server) as session:
            listed = await session.list_tools()
            called = await session.call_tool("search_contexts", {"query": "melone"})
            return listed, called

    listed, called = anyio.run(scenario)

    assert {tool.name for tool in listed.tools} == {"search_contexts"}
    assert called.isError is False
    payload = json.loads(called.content[0].text)
    assert payload["available"] is False
    assert not _database_path(melone_home).exists()
