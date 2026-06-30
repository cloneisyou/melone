import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import timedelta

import pytest

from melone_service.models import utc_now, utc_timestamp
from melone_service.pipeline.activity import ActivityThresholds
from melone_service.pipeline.normalizer import normalize_event
from melone_service.queries import (
    SEARCH_EPISODE_LIMIT,
    build_semantic_candidate_provider,
    get_context_graph,
    get_current_context,
    get_ranked_contexts,
    get_timeline,
    open_event_repository,
    open_readonly_event_repository,
    sample_event,
    search_contexts,
)
from melone_service.config import load_config
from melone_service.embeddings import FakeEmbeddingModel
from melone_service.search.vector_index import SemanticSearchCandidate
from melone_service.store.context_rank import ContextRankRepository, ContextRankScore
from melone_service.store.db import READONLY_BUSY_TIMEOUT_MS
from melone_service.store.embeddings import EmbeddingRepository
from melone_service.store.ocr import OcrChunkRepository
from melone_service.store.screen import ScreenRepository


THRESHOLDS = ActivityThresholds(active_window_seconds=30, idle_timeout_seconds=300)


def test_open_event_repository_runs_migrations_and_closes_connection(tmp_path):
    database_path = tmp_path / "melone.sqlite"

    with open_event_repository(database_path) as repository:
        repository.insert(sample_event())
        events = repository.list()

    assert database_path.is_file()
    assert len(events) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        repository.connection.execute("SELECT 1")


def _write_png(path, *, color=(12, 34, 56)):
    from PIL import Image

    Image.new("RGB", (8, 6), color).save(path, format="PNG")
    return path


def test_list_scene_previews_returns_first_retained_frame_per_scene(tmp_path):
    from melone_service.queries import list_scene_previews
    from melone_service.store.screen import (
        IMAGE_RETENTION_DELETED_AFTER_INDEXING,
        IMAGE_RETENTION_RETAINED,
    )

    database_path = tmp_path / "melone.sqlite"
    earlier = utc_timestamp(utc_now() - timedelta(minutes=10))
    later = utc_timestamp(utc_now() - timedelta(minutes=5))
    latest = utc_timestamp(utc_now() - timedelta(minutes=1))

    with open_event_repository(database_path) as repository:
        screen = ScreenRepository(repository.connection)
        screen.create_session(
            session_id="scene_a",
            source_key="url:https://a.example",
            retrieval_locator="url:https://a.example",
            app_name="Chrome",
            window_title="A page",
            url="https://a.example",
            started_at=earlier,
            now=earlier,
        )
        screen.create_session(
            session_id="scene_b",
            source_key="app:cmux",
            retrieval_locator="app:cmux",
            app_name="cmux",
            window_title="~ / clone_corp",
            started_at=later,
            now=later,
        )
        screen.create_session(
            session_id="scene_c",
            source_key="app:gone",
            retrieval_locator="app:gone",
            app_name="Gone",
            started_at=latest,
            now=latest,
        )

        # scene_a: earliest selected frame was deleted; the later selected frame
        # is the retained preview — proves the retention filter is applied.
        screen.insert_frame(
            frame_id="a_dropped",
            session_id="scene_a",
            captured_at=earlier,
            image_path=str(_write_png(tmp_path / "a_dropped.png")),
            sha256="a-dropped",
            width=8,
            height=6,
        )
        screen.insert_frame(
            frame_id="a_preview",
            session_id="scene_a",
            captured_at=later,
            image_path=str(_write_png(tmp_path / "a_preview.png")),
            sha256="a-preview",
            width=8,
            height=6,
        )
        screen.mark_frame_status("a_dropped", status="selected")
        screen.mark_frame_status("a_preview", status="selected")
        screen.mark_frame_image_retention(
            "a_dropped", state=IMAGE_RETENTION_DELETED_AFTER_INDEXING
        )
        screen.mark_frame_image_retention(
            "a_preview", state=IMAGE_RETENTION_RETAINED
        )

        # scene_b: a retained, selected preview frame with a real file on disk.
        screen.insert_frame(
            frame_id="b_preview",
            session_id="scene_b",
            captured_at=later,
            image_path=str(_write_png(tmp_path / "b_preview.png")),
            sha256="b-preview",
            width=8,
            height=6,
        )
        screen.mark_frame_status("b_preview", status="selected")
        screen.mark_frame_image_retention(
            "b_preview", state=IMAGE_RETENTION_RETAINED
        )

        # scene_c: retained + selected, but the PNG is missing — must be skipped.
        missing = tmp_path / "c_missing.png"
        screen.insert_frame(
            frame_id="c_preview",
            session_id="scene_c",
            captured_at=latest,
            image_path=str(missing),
            sha256="c-preview",
            width=8,
            height=6,
        )
        screen.mark_frame_status("c_preview", status="selected")
        screen.mark_frame_image_retention(
            "c_preview", state=IMAGE_RETENTION_RETAINED
        )

        result = list_scene_previews(repository, limit=12)

    previews = result["previews"]
    # Newest-first, scene_c skipped (missing file).
    assert [item["key"] for item in previews] == ["scene_b", "scene_a"]
    assert previews[0]["label"] == "cmux | ~ / clone_corp"
    assert previews[1]["label"] == "Chrome | https://a.example"
    assert previews[1]["frameId"] == "a_preview"
    assert previews[0]["kind"] == "app_window"
    assert previews[1]["kind"] == "url"
    for item in previews:
        assert str(item["image"]).startswith("data:image/jpeg;base64,")


def _insert_retained_scene(screen, tmp_path, *, session_id, source_key, app_name, when):
    screen.create_session(
        session_id=session_id,
        source_key=source_key,
        retrieval_locator=source_key,
        app_name=app_name,
        started_at=when,
        now=when,
    )
    frame_id = f"{session_id}_frame"
    screen.insert_frame(
        frame_id=frame_id,
        session_id=session_id,
        captured_at=when,
        image_path=str(_write_png(tmp_path / f"{session_id}.png")),
        sha256=f"{session_id}-sha",
        width=8,
        height=6,
    )
    screen.mark_frame_status(frame_id, status="selected")
    from melone_service.store.screen import IMAGE_RETENTION_RETAINED

    screen.mark_frame_image_retention(frame_id, state=IMAGE_RETENTION_RETAINED)


def test_list_scene_previews_orders_by_context_rank(tmp_path):
    from melone_service.queries import list_scene_previews

    database_path = tmp_path / "melone.sqlite"
    older = utc_timestamp(utc_now() - timedelta(minutes=20))
    newer = utc_timestamp(utc_now() - timedelta(minutes=2))

    with open_event_repository(database_path) as repository:
        screen = ScreenRepository(repository.connection)
        # Low-ranked but most recent; high-ranked but older.
        _insert_retained_scene(
            screen, tmp_path, session_id="scene_low", source_key="app:low",
            app_name="Low", when=newer,
        )
        _insert_retained_scene(
            screen, tmp_path, session_id="scene_high", source_key="app:high",
            app_name="High", when=older,
        )
        repository.connection.executemany(
            """
            INSERT INTO context_rank_scores
              (source_key, score, visits, retrieval_locators_json,
               computed_at, model_version)
            VALUES (?, ?, ?, '[]', ?, 'test')
            """,
            [("app:low", 0.1, 1, newer), ("app:high", 0.9, 1, newer)],
        )
        repository.connection.commit()

        previews = list_scene_previews(repository, limit=12)["previews"]

    # Rank wins over recency: the high-ranked context comes first.
    assert [item["key"] for item in previews] == ["scene_high", "scene_low"]


def test_search_contexts_attaches_thumbnails_only_when_requested(tmp_path):
    from melone_service.queries import search_contexts
    from melone_service.store.ocr import OcrChunkRepository
    from melone_service.store.screen import IMAGE_RETENTION_RETAINED

    database_path = tmp_path / "melone.sqlite"
    ts = utc_timestamp(utc_now() - timedelta(minutes=2))

    with open_event_repository(database_path) as repository:
        screen = ScreenRepository(repository.connection)
        screen.create_session(
            session_id="scene_s",
            source_key="app:cmux",
            retrieval_locator="app:cmux",
            app_name="cmux",
            window_title="~ / clone_corp",
            started_at=ts,
            now=ts,
        )
        screen.insert_frame(
            frame_id="s_frame",
            session_id="scene_s",
            captured_at=ts,
            image_path=str(_write_png(tmp_path / "s.png")),
            sha256="s-sha",
            width=8,
            height=6,
        )
        screen.mark_frame_status("s_frame", status="selected")
        screen.mark_frame_image_retention("s_frame", state=IMAGE_RETENTION_RETAINED)
        OcrChunkRepository(repository.connection).insert_chunk_with_fts(
            session_id="scene_s",
            frame_id="s_frame",
            source_key="app:cmux",
            retrieval_locator="app:cmux",
            app_name="cmux",
            window_title="~ / clone_corp",
            url=None,
            crop_bbox_json=None,
            text="clone corp project notes",
            text_hash="hash-1",
            provider="mock",
            model=None,
            latency_ms=1,
            created_at=ts,
        )

        with_images = search_contexts(repository, query="clone", include_images=True)
        without = search_contexts(repository, query="clone", include_images=False)

    assert with_images["results"], "expected an OCR match for 'clone'"
    top = with_images["results"][0]
    assert top["key"] == "app:cmux"
    assert str(top["image"]).startswith("data:image/jpeg;base64,")
    # MCP / default path stays image-free.
    assert "image" not in without["results"][0]


def test_search_thumbnail_falls_back_to_same_app_screenshot(tmp_path):
    from melone_service.store.screen import (
        IMAGE_RETENTION_RETAINED,
        ScreenRepository,
    )

    database_path = tmp_path / "melone.sqlite"
    older = utc_timestamp(utc_now() - timedelta(minutes=20))
    newer = utc_timestamp(utc_now() - timedelta(minutes=2))

    with open_event_repository(database_path) as repository:
        screen = ScreenRepository(repository.connection)
        # A Slack session WITH a retained screenshot.
        screen.create_session(
            session_id="slack_framed",
            source_key="app_window:slack:General",
            retrieval_locator="app_window:slack:General",
            app_name="Slack",
            bundle_id="com.tinyspeck.slackmacgap",
            window_title="General",
            started_at=older,
            now=older,
        )
        screen.insert_frame(
            frame_id="slack_frame",
            session_id="slack_framed",
            captured_at=older,
            image_path=str(_write_png(tmp_path / "slack.png")),
            sha256="slack-sha",
            width=8,
            height=6,
        )
        screen.mark_frame_status("slack_frame", status="selected")
        screen.mark_frame_image_retention("slack_frame", state=IMAGE_RETENTION_RETAINED)

        # A different Slack context (brief visit) with NO frame of its own.
        screen.create_session(
            session_id="slack_frameless",
            source_key="app_window:slack:Threads - Clone - Slack",
            retrieval_locator="app_window:slack:Threads - Clone - Slack",
            app_name="Slack",
            bundle_id="com.tinyspeck.slackmacgap",
            window_title="Threads - Clone - Slack",
            started_at=newer,
            now=newer,
        )

        preview = screen.get_scene_preview_for_source_key(
            "app_window:slack:Threads - Clone - Slack"
        )

    # Falls back to the other Slack session's retained screenshot.
    assert preview is not None
    assert preview.frame_id == "slack_frame"


def test_get_scene_timeline_groups_events_and_keyframe(tmp_path):
    from melone_service.queries import get_scene_timeline
    from melone_service.store.screen import IMAGE_RETENTION_RETAINED, ScreenRepository

    database_path = tmp_path / "melone.sqlite"
    start_dt = utc_now() - timedelta(minutes=10)
    mid_dt = utc_now() - timedelta(minutes=9)
    start = utc_timestamp(start_dt)
    end = utc_timestamp(utc_now() - timedelta(minutes=8))

    with open_event_repository(database_path) as repository:
        # Two events inside the scene's window become the logs / sticks.
        for ts in (start_dt, mid_dt):
            repository.insert(
                normalize_event(
                    "current_asset_changed",
                    timestamp=ts,
                    app={"name": "Code"},
                    window={"title": "__init__.py — melone-mvp"},
                    url="file:///Users/x/.codex/init.py",
                )
            )
        screen = ScreenRepository(repository.connection)
        session = screen.create_session(
            session_id="scene_tl",
            source_key="app_window:code:__init__.py — melone-mvp",
            retrieval_locator="app_window:code:__init__.py — melone-mvp",
            app_name="Code",
            window_title="__init__.py — melone-mvp",
            url="file:///Users/x/.codex/init.py",
            started_at=start,
            now=start,
        )
        screen.close_session(session.id, ended_at=end)
        # mark_session_finalized via repository
        repository.connection.execute(
            "UPDATE screen_sessions SET status='finalized' WHERE id=?", (session.id,)
        )
        repository.connection.commit()
        screen.insert_frame(
            frame_id="tl_frame",
            session_id="scene_tl",
            captured_at=start,
            image_path=str(_write_png(tmp_path / "tl.png")),
            sha256="tl-sha",
            width=8,
            height=6,
        )
        screen.mark_frame_status("tl_frame", status="selected")
        screen.mark_frame_image_retention("tl_frame", state=IMAGE_RETENTION_RETAINED)

        result = get_scene_timeline(repository, limit=80)

    scenes = result["scenes"]
    assert len(scenes) == 1
    scene = scenes[0]
    assert scene["id"] == "scene_tl"
    assert scene["label"] == "Code | __init__.py — melone-mvp"
    assert scene["kind"] == "url"
    assert scene["recordCount"] == 2
    assert len(scene["logs"]) == 2
    assert str(scene["image"]).startswith("data:image/jpeg;base64,")


def test_get_storage_stats_reports_sizes_and_counts(tmp_path):
    from melone_service.config import load_config
    from melone_service.queries import get_storage_stats
    from melone_service.store.screen import IMAGE_RETENTION_RETAINED, ScreenRepository

    config = load_config({"MELONE_HOME": str(tmp_path)})
    ts = utc_timestamp(utc_now() - timedelta(minutes=1))

    with open_event_repository(config.database_path) as repository:
        screen = ScreenRepository(repository.connection)
        screen.create_session(
            session_id="scene_x",
            source_key="app:cmux",
            retrieval_locator="app:cmux",
            app_name="cmux",
            started_at=ts,
            now=ts,
        )
        png = config.screenshots_dir / "scene_x" / "frame.png"
        png.parent.mkdir(parents=True, exist_ok=True)
        _write_png(png)
        screen.insert_frame(
            frame_id="x_frame",
            session_id="scene_x",
            captured_at=ts,
            image_path=str(png),
            sha256="x-sha",
            width=8,
            height=6,
        )
        screen.mark_frame_status("x_frame", status="selected")
        screen.mark_frame_image_retention("x_frame", state=IMAGE_RETENTION_RETAINED)

        stats = get_storage_stats(repository, config=config)

    assert stats["sessions"] == 1
    assert stats["frames"] == 1
    assert stats["retainedScreenshots"] == 1
    assert stats["scenesCaptured"] == 1
    assert stats["scenesWithOcr"] == 0
    assert stats["screenshotCount"] == 1
    assert stats["screenshotBytes"] > 0
    assert stats["databaseBytes"] > 0
    assert stats["totalBytes"] >= stats["databaseBytes"] + stats["screenshotBytes"]


def test_get_current_context_returns_latest_context_and_activity(tmp_path):
    now = utc_now()
    database_path = tmp_path / "melone.sqlite"

    with open_event_repository(database_path) as repository:
        repository.insert(
            normalize_event(
                "current_asset_changed",
                timestamp=now - timedelta(seconds=10),
                app={"name": "Safari", "bundle_id": "com.apple.Safari", "pid": 123},
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
        context = get_current_context(repository, thresholds=THRESHOLDS, now=now)

    assert context == {
        "app": "Safari",
        "window": "Melone Docs",
        "url": "https://example.com/melone",
        "activity": "active",
    }
    assert json.dumps(context)


def test_get_current_context_returns_idle_for_empty_database(tmp_path):
    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        context = get_current_context(repository, thresholds=THRESHOLDS)

    assert context == {"app": None, "window": None, "url": None, "activity": "idle"}


def test_get_ranked_contexts_returns_serializable_entries(tmp_path):
    now = utc_now()

    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        repository.insert(_context_event("Cursor", "queries.py - melone", now, 0))
        repository.insert(_context_event("Slack", "dev - Clone - Slack", now, 1))
        ranked_contexts = get_ranked_contexts(repository)

    assert len(ranked_contexts) == 2
    labels = [item["label"] for item in ranked_contexts]
    assert "Cursor | queries.py - melone" in labels
    assert "Slack | dev - Clone - Slack" in labels
    for item in ranked_contexts:
        assert set(item) == {"score", "visits", "kind", "label"}
    assert json.dumps(ranked_contexts)


def test_get_ranked_contexts_show_hidden_includes_bridge_context(tmp_path):
    now = utc_now()

    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        repository.insert(_context_event("Cursor", "A", now, 0))
        repository.insert(_context_event("Google Chrome", "New Tab", now, 1))
        repository.insert(_context_event("Cursor", "B", now, 2))
        default_labels = [
            item["label"] for item in get_ranked_contexts(repository)
        ]
        hidden_labels = [
            item["label"]
            for item in get_ranked_contexts(repository, show_hidden=True)
        ]

    assert "Google Chrome" not in default_labels
    assert "Google Chrome" in hidden_labels


def test_get_ranked_contexts_limit_restricts_entries(tmp_path):
    now = utc_now()

    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        repository.insert(_context_event("Cursor", "A", now, 0))
        repository.insert(_context_event("Cursor", "B", now, 1))
        repository.insert(_context_event("Cursor", "C", now, 2))
        ranked_contexts = get_ranked_contexts(repository, limit=2)

    assert len(ranked_contexts) == 2


def test_get_context_graph_returns_nodes_edges_and_visits(tmp_path):
    now = utc_now()
    cursor_key = "app_window:cursor:queries.py"
    url_key = "url:https://docs.example.com/guide"

    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        # Cursor -> browser URL -> Cursor transitions create edges both ways.
        repository.insert(_context_event("Cursor", "queries.py", now, 30))
        repository.insert(_url_event("https://docs.example.com/guide", now, 20))
        repository.insert(_context_event("Cursor", "queries.py", now, 10))
        graph = get_context_graph(repository)

    assert set(graph) == {"nodes", "edges", "totalNodes"}
    assert graph["totalNodes"] == 2
    nodes_by_key = {node["key"]: node for node in graph["nodes"]}
    assert set(nodes_by_key) == {cursor_key, url_key}
    for node in graph["nodes"]:
        assert set(node) == {"key", "kind", "label", "score", "visits"}
        assert node["score"] > 0
    assert nodes_by_key[cursor_key]["kind"] == "app_window"
    assert nodes_by_key[cursor_key]["label"] == "Cursor | queries.py"
    assert nodes_by_key[cursor_key]["visits"] == 2
    assert nodes_by_key[url_key]["kind"] == "url"
    assert nodes_by_key[url_key]["visits"] == 1
    assert graph["edges"] == [
        {"source": cursor_key, "target": url_key, "weight": 1.0},
        {"source": url_key, "target": cursor_key, "weight": 1.0},
    ]
    assert json.dumps(graph)


def test_get_context_graph_includes_standalone_node_excludes_bridge(tmp_path):
    now = utc_now()

    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        # New Tab is a bridge: dropped from nodes, compressed into an A->B edge.
        repository.insert(_context_event("Cursor", "A", now, 40))
        repository.insert(_context_event("Google Chrome", "New Tab", now, 30))
        repository.insert(_context_event("Cursor", "B", now, 20))
        graph = get_context_graph(repository)

    labels = [node["label"] for node in graph["nodes"]]
    assert "Google Chrome" not in labels
    assert set(labels) == {"Cursor | A", "Cursor | B"}
    assert graph["edges"] == [
        {"source": "app_window:cursor:A", "target": "app_window:cursor:B", "weight": 1.0}
    ]

    with open_event_repository(tmp_path / "standalone.sqlite") as repository:
        # A standalone node with no transitions still appears, with score 0.
        repository.insert(_context_event("Cursor", "solo", now, 10))
        standalone = get_context_graph(repository)

    assert standalone["totalNodes"] == 1
    assert standalone["edges"] == []
    assert standalone["nodes"][0]["score"] == 0.0
    assert standalone["nodes"][0]["visits"] == 1


def test_get_context_graph_limit_keeps_top_nodes_and_their_edges(tmp_path):
    now = utc_now()

    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        # Repeated A<->B visits push the A and B scores clearly above C.
        sequence = ["A", "B", "A", "B", "A", "C"]
        for index, title in enumerate(sequence):
            repository.insert(
                _context_event("Cursor", title, now, 60 - index * 10)
            )
        graph = get_context_graph(repository, limit=2)

    assert graph["totalNodes"] == 3
    labels = [node["label"] for node in graph["nodes"]]
    assert labels == ["Cursor | A", "Cursor | B"]  # sorted by descending score
    kept_keys = {node["key"] for node in graph["nodes"]}
    assert graph["edges"] == [
        {"source": "app_window:cursor:A", "target": "app_window:cursor:B", "weight": 2.0},
        {"source": "app_window:cursor:B", "target": "app_window:cursor:A", "weight": 2.0},
    ]
    for edge in graph["edges"]:
        assert {edge["source"], edge["target"]} <= kept_keys


def test_get_context_graph_returns_empty_shape_for_empty_database(tmp_path):
    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        graph = get_context_graph(repository)

    assert graph == {"nodes": [], "edges": [], "totalNodes": 0}
    assert json.dumps(graph)


def test_get_timeline_returns_events_in_timestamp_order(tmp_path):
    now = utc_now()

    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        repository.insert(_context_event("Slack", "dev", now, 5))
        repository.insert(_context_event("Cursor", "melone", now, 10))
        timeline = get_timeline(repository)

    assert [item["app"] for item in timeline] == ["Cursor", "Slack"]
    for item in timeline:
        assert set(item) == {"timestamp", "type", "app", "window", "url"}
    assert json.dumps(timeline)


def test_search_contexts_matches_label_and_url_case_insensitive(tmp_path):
    now = utc_now()

    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        repository.insert(_context_event("Cursor", "MELONE queries", now, 30))
        repository.insert(_url_event("https://docs.example.com/Melone/guide", now, 20))
        repository.insert(_context_event("Slack", "general", now, 10))
        found = search_contexts(repository, query="melone")

    labels = [item["label"] for item in found["results"]]
    assert "Cursor | MELONE queries" in labels  # label match, case-insensitive
    # main labels URL pages by title, so the docs page is matched on its url, not its label.
    # _normalize_url lowercases the host only; the path case ("Melone") is preserved.
    url_uris = [item["uri"] for item in found["results"] if item["kind"] == "url"]
    assert "https://docs.example.com/Melone/guide" in url_uris  # url match
    assert all("Slack" not in label for label in labels)
    for item in found["results"]:
        assert set(item) == {
            "key", "kind", "label", "uri", "score", "visits", "lastSeenAt",
        }
    assert json.dumps(found)


def test_search_contexts_includes_ocr_text_matches(tmp_path):
    now = utc_now()
    url = "https://example.com/ocr-doc"

    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        repository.insert(_url_event(url, now, 30))
        _seed_ocr_chunk(
            repository.connection,
            chunk_id="ocr_chunk_invoice",
            session_id="screen_session_invoice",
            frame_id="screen_frame_invoice",
            source_key="url:https://example.com/ocr-doc",
            retrieval_locator="url:https://example.com/ocr-doc",
            text="Quarterly invoice approval checklist",
            started_at=utc_timestamp(now - timedelta(seconds=30)),
            ended_at=utc_timestamp(now - timedelta(seconds=20)),
        )
        found = search_contexts(repository, query="invoice")

    assert len(found["results"]) == 1
    result = found["results"][0]
    assert result["key"] == "url:https://example.com/ocr-doc"
    assert result["kind"] == "url"
    assert result["uri"] == url
    assert result["visits"] == 1
    assert result["matchSource"] == "ocr"
    assert "invoice" in str(result["snippet"]).casefold()

    assert found["episodes"][0]["matchSource"] == "ocr"
    assert "invoice" in str(found["episodes"][0]["snippet"]).casefold()
    assert found["episodes"][0]["startedAt"] == utc_timestamp(
        now - timedelta(seconds=30)
    )
    assert found["episodes"][0]["endedAt"] == utc_timestamp(
        now - timedelta(seconds=20)
    )
    assert json.dumps(found)


def test_search_contexts_semantic_ocr_match_keeps_response_schema(tmp_path):
    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        found = search_contexts(
            repository,
            query="which renewal did customer success approve",
            semantic_candidate_provider=_FakeSemanticProvider(
                _semantic_candidate(
                    "ocr_chunk_semantic_query",
                    text=(
                        "Project Phoenix renewal approval notes from the "
                        "customer success review."
                    ),
                    embedding_similarity=0.8,
                    embedding_relevance=0.9,
                )
            ),
            limit=1,
        )

    assert len(found["results"]) == 1
    result = found["results"][0]
    assert set(result) == {
        "key",
        "kind",
        "label",
        "uri",
        "score",
        "visits",
        "lastSeenAt",
        "matchSource",
        "snippet",
    }
    assert result["matchSource"] == "ocr"
    assert result["uri"] == "https://example.com/ocr_chunk_semantic_query"
    assert "Project Phoenix" in str(result["snippet"])
    assert set(found["episodes"][0]) == {
        "startedAt",
        "endedAt",
        "app",
        "window",
        "url",
        "matchSource",
        "snippet",
    }
    assert json.dumps(found)


def test_semantic_provider_skips_empty_embedding_cache(tmp_path):
    config = load_config(
        {
            "MELONE_HOME": str(tmp_path),
            "MELONE_SEMANTIC_SEARCH_ENABLED": "true",
            "MELONE_EMBEDDING_MODEL": "test-embedding-model",
            "MELONE_EMBEDDING_DIMENSION": "128",
        }
    )
    now = utc_now()

    with open_event_repository(config.database_path) as repository:
        _seed_ocr_chunk(
            repository.connection,
            chunk_id="ocr_chunk_no_embedding",
            session_id="screen_session_no_embedding",
            frame_id="screen_frame_no_embedding",
            source_key="url:https://example.com/no-embedding",
            retrieval_locator="url:https://example.com/no-embedding",
            text="Project Phoenix renewal notes",
            started_at=utc_timestamp(now - timedelta(seconds=30)),
        )

        assert build_semantic_candidate_provider(repository.connection, config) is None


def test_search_contexts_uses_configured_semantic_embedding_index(
    monkeypatch,
    tmp_path,
):
    config = load_config(
        {
            "MELONE_HOME": str(tmp_path),
            "MELONE_SEMANTIC_SEARCH_ENABLED": "true",
            "MELONE_EMBEDDING_MODEL": "test-embedding-model",
            "MELONE_EMBEDDING_DIMENSION": "128",
        }
    )
    model = FakeEmbeddingModel(
        model="test-embedding-model",
        dimension=128,
        query_vectors={"who approved the renewal": [1.0, *([0.0] * 127)]},
        document_vectors={
            "Project Phoenix contract acceptance notes": [1.0, *([0.0] * 127)]
        },
    )
    monkeypatch.setattr(
        "melone_service.embeddings.sentence_transformers."
        "get_sentence_transformer_embedding_model",
        lambda _config: model,
    )
    now = utc_now()

    with open_event_repository(config.database_path) as repository:
        _seed_ocr_chunk(
            repository.connection,
            chunk_id="ocr_chunk_semantic_index",
            session_id="screen_session_semantic_index",
            frame_id="screen_frame_semantic_index",
            source_key="url:https://example.com/semantic-index",
            retrieval_locator="url:https://example.com/semantic-index",
            text="Project Phoenix contract acceptance notes",
            started_at=utc_timestamp(now - timedelta(seconds=30)),
        )
        chunk = OcrChunkRepository(repository.connection).list_chunks()[0]
        EmbeddingRepository(repository.connection).upsert_chunk_embedding(
            chunk_id=chunk.id,
            model=config.embedding_model,
            dimension=config.embedding_dimension,
            text_hash=chunk.text_hash,
            embedding=model.encode_document(chunk.text),
        )
        repository.connection.commit()

        found = search_contexts(
            repository,
            query="who approved the renewal",
            config=config,
            limit=1,
        )

    assert [result["key"] for result in found["results"]] == [
        "url:https://example.com/semantic-index"
    ]
    assert found["results"][0]["matchSource"] == "ocr"
    assert "Project Phoenix" in str(found["results"][0]["snippet"])
    assert model.query_calls == ["who approved the renewal"]


def test_search_contexts_uses_request_local_pagerank_for_ocr_order(tmp_path):
    now = utc_now()
    high_url = "https://example.com/high-context"
    low_url = "https://example.com/low-context"
    other_url = "https://example.com/other-context"

    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        for index, url in enumerate((low_url, high_url, other_url, high_url)):
            repository.insert(_url_event(url, now, 50 - index * 10))

        # The cache intentionally says the opposite. search_contexts should use
        # the fresh PageRank scores it already computed for this request.
        _seed_context_score(repository.connection, f"url:{low_url}", 1.0)
        _seed_context_score(repository.connection, f"url:{high_url}", 0.0)
        for url in (low_url, high_url):
            _seed_ocr_chunk(
                repository.connection,
                chunk_id=f"ocr_chunk_{url.rsplit('/', 1)[-1]}",
                session_id=f"screen_session_{url.rsplit('/', 1)[-1]}",
                frame_id=f"screen_frame_{url.rsplit('/', 1)[-1]}",
                source_key=f"url:{url}",
                retrieval_locator=f"url:{url}",
                text="Quarterly invoice approval checklist",
                started_at=utc_timestamp(now - timedelta(seconds=5)),
            )

        found = search_contexts(repository, query="invoice", limit=2)

    assert [result["key"] for result in found["results"]] == [
        f"url:{high_url}",
        f"url:{low_url}",
    ]
    assert found["results"][0]["score"] > found["results"][1]["score"]
    assert all(result["matchSource"] == "ocr" for result in found["results"])


def test_search_contexts_excludes_bridge_pages(tmp_path):
    now = utc_now()

    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        repository.insert(_context_event("Cursor", "tab manager", now, 30))
        repository.insert(_context_event("Google Chrome", "New Tab", now, 20))
        found = search_contexts(repository, query="tab")

    # New Tab is a bridge hidden from ranking, so search must exclude it too.
    assert [item["label"] for item in found["results"]] == ["Cursor | tab manager"]
    assert [episode["window"] for episode in found["episodes"]] == ["tab manager"]


def test_search_contexts_sorts_by_score_and_applies_limit(tmp_path):
    now = utc_now()

    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        # hub receives return transitions from both leaf and misc, scoring highest.
        sequence = ["doc hub", "doc leaf", "doc hub", "misc", "doc hub"]
        for index, title in enumerate(sequence):
            repository.insert(
                _context_event("Cursor", title, now, 50 - index * 10)
            )
        found = search_contexts(repository, query="doc")
        limited = search_contexts(repository, query="doc", limit=1)

    labels = [item["label"] for item in found["results"]]
    assert labels == ["Cursor | doc hub", "Cursor | doc leaf"]
    scores = [item["score"] for item in found["results"]]
    assert scores == sorted(scores, reverse=True)
    assert found["results"][0]["visits"] == 3
    assert [item["label"] for item in limited["results"]] == ["Cursor | doc hub"]


def test_search_contexts_breaks_score_ties_by_label(tmp_path):
    now = utc_now()

    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        # Gaps over the 30-minute session break leave no edges, tying scores at 0.
        repository.insert(_context_event("Cursor", "doc b", now, 3 * 3600))
        repository.insert(_context_event("Cursor", "doc c", now, 2 * 3600))
        repository.insert(_context_event("Cursor", "doc a", now, 1 * 3600))
        found = search_contexts(repository, query="doc")

    assert [item["label"] for item in found["results"]] == [
        "Cursor | doc a",
        "Cursor | doc b",
        "Cursor | doc c",
    ]
    assert all(item["score"] == 0.0 for item in found["results"])


def test_search_contexts_uri_is_normalized_url_only_for_url_kind(tmp_path):
    now = utc_now()

    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        repository.insert(
            _url_event("https://Docs.Example.com/guide/?utm_source=mail", now, 20)
        )
        repository.insert(_context_event("Cursor", "guide notes", now, 10))
        found = search_contexts(repository, query="guide")

    by_kind = {item["kind"]: item for item in found["results"]}
    assert by_kind["url"]["uri"] == "https://docs.example.com/guide"
    assert by_kind["app_window"]["uri"] is None


def test_search_contexts_last_seen_at_uses_unit_end_or_start(tmp_path):
    now = utc_now()

    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        repository.insert(_context_event("Cursor", "doc one", now, 30))
        repository.insert(_context_event("Slack", "general", now, 20))
        repository.insert(_context_event("Cursor", "doc two", now, 10))
        found = search_contexts(repository, query="doc")

    last_seen = {item["label"]: item["lastSeenAt"] for item in found["results"]}
    # The doc one unit ended at the next event (20s ago), so ended_at is used.
    assert last_seen["Cursor | doc one"] == utc_timestamp(now - timedelta(seconds=20))
    # The in-progress doc two unit has no ended_at and falls back to started_at.
    assert last_seen["Cursor | doc two"] == utc_timestamp(now - timedelta(seconds=10))


def test_search_contexts_episodes_are_newest_first_and_capped(tmp_path):
    now = utc_now()

    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        repository.insert(_context_event("Cursor", "doc one", now, 30))
        repository.insert(_context_event("Slack", "general", now, 20))
        repository.insert(_context_event("Cursor", "doc two", now, 10))
        found = search_contexts(repository, query="doc")

        # Consecutive events with the same key merge into one unit, so alternate
        # two titles to produce more matching units than SEARCH_EPISODE_LIMIT.
        for index in range(SEARCH_EPISODE_LIMIT + 2):
            repository.insert(
                _context_event("Cursor", f"doc {index % 2}", now, 600 - index * 10)
            )
        capped = search_contexts(repository, query="doc")

    episodes = found["episodes"]
    assert [episode["window"] for episode in episodes] == ["doc two", "doc one"]
    for episode in episodes:
        assert set(episode) == {"startedAt", "endedAt", "app", "window", "url"}
    started = [episode["startedAt"] for episode in episodes]
    assert started == sorted(started, reverse=True)
    assert episodes[0]["endedAt"] is None  # in-progress final unit
    assert episodes[1]["endedAt"] == utc_timestamp(now - timedelta(seconds=20))
    assert len(capped["episodes"]) == SEARCH_EPISODE_LIMIT


def test_search_contexts_returns_empty_shape_when_nothing_matches(tmp_path):
    now = utc_now()

    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        repository.insert(_context_event("Cursor", "doc one", now, 10))
        found = search_contexts(repository, query="없는검색어")

    assert found == {"results": [], "episodes": []}
    assert json.dumps(found)


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
def test_search_contexts_rejects_blank_query(tmp_path, query):
    with open_event_repository(tmp_path / "melone.sqlite") as repository:
        with pytest.raises(ValueError):
            search_contexts(repository, query=query)


def test_open_readonly_event_repository_reads_but_blocks_writes(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    with open_event_repository(database_path) as repository:
        repository.insert(sample_event())

    with open_readonly_event_repository(database_path) as repository:
        busy_timeout = repository.connection.execute(
            "PRAGMA busy_timeout"
        ).fetchone()[0]
        events = repository.list()
        with pytest.raises(sqlite3.OperationalError):
            repository.insert(sample_event())

    assert busy_timeout == READONLY_BUSY_TIMEOUT_MS
    assert len(events) == 1


def test_open_readonly_event_repository_requires_existing_database(tmp_path):
    missing_path = tmp_path / "missing.sqlite"

    with pytest.raises(FileNotFoundError):
        with open_readonly_event_repository(missing_path):
            pass

    assert not missing_path.exists()


def test_sample_event_describes_development_context():
    event = sample_event()

    assert event.type == "active_app_changed"
    assert event.app_name == "Sample App"
    assert event.window_title == "Sample Window"
    assert event.url == "https://example.com/work?q=private#section"
    assert event.source == "sample"


def test_queries_module_imports_without_main_module():
    # main.py cannot be imported on Windows (fcntl), so verify in an isolated
    # process that queries never pulls it in through any path.
    code = (
        "import sys; import melone_service.queries; "
        "assert 'melone_service.main' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def _context_event(app_name, window_title, now, seconds):
    return normalize_event(
        "active_app_snapshot",
        timestamp=now - timedelta(seconds=seconds),
        app={"name": app_name},
        window={"title": window_title},
        source="test",
    )


def _url_event(url, now, seconds):
    return normalize_event(
        "current_asset_changed",
        timestamp=now - timedelta(seconds=seconds),
        app={"name": "Chrome"},
        window={"title": "Docs"},
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
    screen_repository = ScreenRepository(connection)
    screen_repository.create_session(
        session_id=session_id,
        source_key=source_key,
        retrieval_locator=retrieval_locator,
        app_name="Chrome",
        bundle_id="com.google.Chrome",
        window_title="Docs",
        url=retrieval_locator.removeprefix("url:"),
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
        url=retrieval_locator.removeprefix("url:"),
        text=text,
        text_hash=hashlib.sha256(f"{chunk_id}:{text}".encode()).hexdigest(),
        created_at=ended_at or started_at,
    )
    connection.commit()


def _seed_context_score(connection, source_key, score):
    ContextRankRepository(connection).upsert_scores(
        [
            ContextRankScore(
                source_key=source_key,
                score=score,
                visits=1,
                retrieval_locators=(source_key,),
                computed_at=utc_timestamp(utc_now()),
                model_version="test_model_v1",
            )
        ]
    )


def _semantic_candidate(
    chunk_id,
    *,
    text,
    embedding_similarity,
    embedding_relevance,
):
    url = f"https://example.com/{chunk_id}"
    return SemanticSearchCandidate(
        chunk_id=chunk_id,
        session_id=f"screen_session_{chunk_id}",
        frame_id=f"screen_frame_{chunk_id}",
        source_key=f"url:{url}",
        retrieval_locator=f"url:{url}",
        app_name="Chrome",
        window_title="Docs",
        url=url,
        session_started_at="2026-06-09T06:00:00.000Z",
        session_ended_at=None,
        chunk_created_at="2026-06-09T06:00:00.000Z",
        text=text,
        embedding_similarity=embedding_similarity,
        embedding_relevance=embedding_relevance,
    )


class _FakeSemanticProvider:
    def __init__(self, *candidates):
        self.candidates = list(candidates)

    def search_candidates(self, query, *, limit, since=None):
        return self.candidates[:limit]
