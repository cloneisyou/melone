import hashlib
from datetime import timedelta

from melone_service.cli import build_parser, main
from melone_service.asset.resolvers.agent_sessions import AgentConversation
from melone_service.models import utc_now
from melone_service.pipeline.normalizer import normalize_event
from melone_service.runtime_config import RUNTIME_PARAMETERS
from melone_service.store.db import connect, initialize_database
from melone_service.store.embeddings import EmbeddingRepository
from melone_service.store.events import EventRepository
from melone_service.store.ocr import OcrChunkRepository
from melone_service.store.screen import ScreenRepository


def test_status_command(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))

    exit_code = main(["status"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Melone service" in captured.out
    assert "status: not running" in captured.out
    assert f"data directory: {tmp_path}" in captured.out
    assert f"db path: {tmp_path / 'melone.sqlite'}" in captured.out
    assert "db connection: ok" in captured.out
    assert "migration version: 5/5" in captured.out
    assert "pending migrations: 0" in captured.out
    assert "activity active window: 30s" in captured.out
    assert "permissions:" in captured.out
    assert "  accessibility:" in captured.out
    assert "collectors:" in captured.out
    assert "  active_window:" in captured.out
    assert (tmp_path / "melone.sqlite").is_file()

    events_exit_code = main(
        ["events", "--since", "10m", "--type", "permission_status_changed"]
    )
    events_output = capsys.readouterr().out

    assert events_exit_code == 0
    assert "permission_status" in events_output


def test_status_command_reports_semantic_embedding_coverage(
    capsys,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))
    monkeypatch.setenv("MELONE_SEMANTIC_SEARCH_ENABLED", "true")
    monkeypatch.setenv("MELONE_EMBEDDING_MODEL", "cli-test-model")
    monkeypatch.setenv("MELONE_EMBEDDING_DIMENSION", "128")
    _insert_screen_text_embedding_fixture(tmp_path / "melone.sqlite")

    exit_code = main(["status"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "semantic search: enabled" in output
    assert "embedding model: cli-test-model (128d)" in output
    assert "embedding cache coverage: 1/1 chunks (100.0%)" in output


def test_events_add_sample_and_list_command(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))

    add_exit_code = main(["events", "add-sample"])
    add_output = capsys.readouterr().out
    list_exit_code = main(["events", "--since", "30m", "--type", "active_app_changed"])
    list_output = capsys.readouterr().out

    assert add_exit_code == 0
    assert "added sample event: evt_" in add_output
    assert list_exit_code == 0
    assert "timestamp" in list_output
    assert "active_app_changed" in list_output
    assert "Sample App" in list_output
    assert "https://example.com/work?q=private#section" in list_output


def test_timeline_command_lists_sample_event(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))

    main(["events", "add-sample"])
    capsys.readouterr()
    exit_code = main(["timeline", "--since", "30m"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "time" in output
    assert "active_app_changed" in output
    assert (
        "Sample App | Sample Window | https://example.com/work?q=private#section"
        in output
    )


def test_context_command_shows_latest_context_and_activity(
    capsys,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)
    now = utc_now()

    connection = connect(database_path)
    try:
        repository = EventRepository(connection)
        repository.insert(
            normalize_event(
                "current_asset_changed",
                timestamp=now - timedelta(seconds=10),
                app={
                    "name": "Safari",
                    "bundle_id": "com.apple.Safari",
                    "pid": 123,
                },
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
    finally:
        connection.close()

    exit_code = main(["context"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Melone context" in output
    assert "app: Safari" in output
    assert "window: Melone Docs" in output
    assert "url: https://example.com/melone" in output
    assert "activity: active" in output


def test_contexts_command_is_registered():
    args = build_parser().parse_args(["contexts"])

    assert args.command == "contexts"
    assert callable(args.handler)


def test_context_rank_cache_command_is_registered():
    args = build_parser().parse_args(["context-rank-cache", "refresh"])

    assert args.command == "context-rank-cache"
    assert args.cache_action == "refresh"
    assert callable(args.handler)


def test_config_command_is_registered():
    args = build_parser().parse_args(["config", "doctor", "--desktop"])

    assert args.command == "config"
    assert args.config_action == "doctor"
    assert args.desktop is True
    assert callable(args.handler)


def test_config_list_prints_runtime_catalog(capsys):
    exit_code = main(["config", "list", "--scope", "developer"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "MELONE_HOME" in output
    assert "MELONE_SCREENSHOT_COLLECTOR_ENABLED" in output
    assert "MELONE_GOOGLE_CLIENT_SECRET" not in output


def test_config_doctor_desktop_summarizes_launch_inputs(
    capsys,
    monkeypatch,
    tmp_path,
):
    for parameter in RUNTIME_PARAMETERS:
        monkeypatch.delenv(parameter.name, raising=False)
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))
    monkeypatch.setattr("melone_service.config.load_env_file", lambda: False)

    exit_code = main(["config", "doctor", "--desktop"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Melone config doctor" in output
    assert "normal desktop launch: no env required" in output
    assert f"data directory: {tmp_path}" in output
    assert "Screen Text Search: off" in output
    assert "OCR provider: apple_vision" in output
    assert "MELONE_HOME:" in output
    assert "desktop integrations:" in output
    assert "Google sign-in: disabled" in output
    assert "Python override: auto" in output


def test_config_doctor_reports_legacy_ocr_env_with_current_name(
    capsys,
    monkeypatch,
    tmp_path,
):
    for parameter in RUNTIME_PARAMETERS:
        monkeypatch.delenv(parameter.name, raising=False)
        for alias in parameter.aliases:
            monkeypatch.delenv(alias, raising=False)
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))
    monkeypatch.setenv("MELONE_VLM_PROVIDER", "local_vllm")
    monkeypatch.setattr("melone_service.config.load_env_file", lambda: False)

    exit_code = main(["config", "doctor"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "OCR provider: local_vllm" in output
    assert "MELONE_OCR_PROVIDER: local_vllm" in output
    assert "MELONE_VLM_PROVIDER:" not in output


def test_contexts_command_prints_ranked_context_table(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))
    now = utc_now()
    _insert_events(
        tmp_path / "melone.sqlite",
        [
            _context_event("Cursor", "context_graph.py - melone", now, seconds=0),
            _context_event("Slack", "dev - Clone - Slack", now, seconds=1),
        ],
    )

    exit_code = main(["contexts", "--since", "30m"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "score" in output
    assert "visits" in output
    assert "kind" in output
    assert "context" in output
    assert "url" in output
    assert "Cursor | context_graph.py - melone" in output
    assert "Slack | dev - Clone - Slack" in output


def test_context_rank_cache_command_refreshes_and_prints_scores(
    capsys,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))
    now = utc_now()
    _insert_events(
        tmp_path / "melone.sqlite",
        [
            _context_event("Cursor", "context_rank_cache.py - melone", now, seconds=0),
            _browser_url_event(
                "Google Chrome",
                "Pull request - melone",
                "https://github.com/cloneisyou/melone/pull/1",
                now,
                seconds=1,
            ),
        ],
    )

    exit_code = main(["context-rank-cache", "refresh", "--event-limit", "10"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "refreshed context rank cache: 2 row(s)" in output
    assert "events considered: 2" in output
    assert "model_version: context_rank_cache_v1:" in output
    assert "app_window:cursor:context_rank_cache.py - melone" in output
    assert "github:repo:cloneisyou/melone" in output
    assert "url:https://github.com/cloneisyou/melone/pull/1" in output


def test_contexts_command_passes_activity_events_to_ranking(
    capsys,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))
    now = utc_now()
    seen_event_types = []

    def fake_rank_contexts(events, *, limit=None, show_hidden=False):
        seen_event_types.extend(event.type for event in events)
        return []

    monkeypatch.setattr("melone_service.cli.rank_contexts", fake_rank_contexts)
    _insert_events(
        tmp_path / "melone.sqlite",
        [
            _context_event("Cursor", "context_graph.py - melone", now, seconds=0),
            _activity_event(
                "keyboard_burst",
                now,
                seconds=1,
                metadata={"key_count": 12},
            ),
        ],
    )

    exit_code = main(["contexts"])
    capsys.readouterr()

    assert exit_code == 0
    assert "active_app_snapshot" in seen_event_types
    assert "keyboard_burst" in seen_event_types


def test_contexts_command_prints_url_column(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))
    now = utc_now()
    _insert_events(
        tmp_path / "melone.sqlite",
        [
            _browser_url_event(
                "Safari",
                "Melone Docs",
                "https://example.com/melone",
                now,
                seconds=0,
            ),
            _context_event("Cursor", "context_graph.py - melone", now, seconds=1),
        ],
    )

    exit_code = main(["contexts", "--since", "30m"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Melone Docs" in output
    assert "https://example.com/melone" in output


def test_contexts_command_limit_restricts_output_rows(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))
    now = utc_now()
    _insert_events(
        tmp_path / "melone.sqlite",
        [
            _context_event("Cursor", "A", now, seconds=0),
            _context_event("Cursor", "B", now, seconds=1),
            _context_event("Cursor", "C", now, seconds=2),
        ],
    )

    exit_code = main(["contexts", "--since", "30m", "--limit", "2"])
    lines = capsys.readouterr().out.strip().splitlines()

    assert exit_code == 0
    assert len(lines) == 3
    assert lines[0].startswith("score")


def test_contexts_command_show_hidden_includes_hidden_context(
    capsys,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))
    now = utc_now()
    _insert_events(
        tmp_path / "melone.sqlite",
        [
            _context_event("Cursor", "A", now, seconds=0),
            _context_event("Google Chrome", "New Tab", now, seconds=1),
            _context_event("Cursor", "B", now, seconds=2),
        ],
    )

    default_exit_code = main(["contexts", "--since", "30m"])
    default_output = capsys.readouterr().out
    hidden_exit_code = main(["contexts", "--since", "30m", "--show-hidden"])
    hidden_output = capsys.readouterr().out

    assert default_exit_code == 0
    assert hidden_exit_code == 0
    assert "Google Chrome" not in default_output
    assert "Google Chrome" in hidden_output


def test_contexts_command_prints_empty_message(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))

    exit_code = main(["contexts"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert output == "No contexts found.\n"


def _fake_terminal_sessions():
    candidates = [
        {
            "connector": "codex_cli",
            "conversation_id": "conv-one",
            "cwd": "/home/me/project-a",
            "updated_at": 2.0,
            "url": "agent://codex_cli/home/me/project-a",
        },
        {
            "connector": "claude_code",
            "conversation_id": "conv-two",
            "cwd": "/home/me/project-b",
            "updated_at": 1.0,
            "url": "agent://claude_code/home/me/project-b",
        },
    ]
    return AgentConversation(
        conversation_id="conv-one",
        url=candidates[0]["url"],
        kind="session",
        candidates=candidates,
    )


def test_agent_sessions_lists_candidates(capsys, monkeypatch):
    # The command reads live; mock the live enumeration.
    monkeypatch.setattr(
        "melone_service.cli.current_terminal_sessions", _fake_terminal_sessions
    )

    exit_code = main(["agent-sessions"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "conv-one" in output
    assert "conv-two" in output
    assert "agent://codex_cli/home/me/project-a" in output
    assert "*" in output  # the best-guess (active) row is marked


def test_agent_sessions_cwd_filters_to_single_candidate(capsys, monkeypatch):
    monkeypatch.setattr(
        "melone_service.cli.current_terminal_sessions", _fake_terminal_sessions
    )

    exit_code = main(["agent-sessions", "--cwd", "/home/me/project-b"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "conv-two" in output
    assert "conv-one" not in output


def _insert_events(database_path, events):
    initialize_database(database_path)
    connection = connect(database_path)
    try:
        repository = EventRepository(connection)
        for event in events:
            repository.insert(event)
    finally:
        connection.close()


def _insert_screen_text_embedding_fixture(database_path):
    initialize_database(database_path)
    connection = connect(database_path)
    try:
        screen_repository = ScreenRepository(connection)
        screen_repository.create_session(
            session_id="screen_session_cli_status",
            source_key="url:https://example.com/status",
            retrieval_locator="url:https://example.com/status",
            app_name="Safari",
            bundle_id="com.apple.Safari",
            window_title="Status",
            url="https://example.com/status",
        )
        screen_repository.insert_frame(
            frame_id="screen_frame_cli_status",
            session_id="screen_session_cli_status",
            captured_at=None,
            image_path="/tmp/screen_frame_cli_status.png",
            sha256=hashlib.sha256(b"screen_frame_cli_status").hexdigest(),
            width=1280,
            height=720,
        )
        text = "screen text status fixture"
        chunk = OcrChunkRepository(connection).insert_chunk_with_fts(
            chunk_id="ocr_chunk_cli_status",
            session_id="screen_session_cli_status",
            frame_id="screen_frame_cli_status",
            source_key="url:https://example.com/status",
            retrieval_locator="url:https://example.com/status",
            text=text,
            text_hash=hashlib.sha256(text.encode()).hexdigest(),
            provider="mock",
            model="mock-ocr",
        )
        EmbeddingRepository(connection).upsert_chunk_embedding(
            chunk_id=chunk.id,
            model="cli-test-model",
            dimension=128,
            text_hash=chunk.text_hash,
            embedding=[1.0, *([0.0] * 127)],
        )
        connection.commit()
    finally:
        connection.close()


def _context_event(app_name, window_title, now, *, seconds):
    return normalize_event(
        "active_app_snapshot",
        timestamp=now - timedelta(seconds=seconds),
        app={"name": app_name},
        window={"title": window_title},
        source="test",
    )


def _browser_url_event(app_name, window_title, url, now, *, seconds):
    return normalize_event(
        "current_asset_changed",
        timestamp=now - timedelta(seconds=seconds),
        app={"name": app_name},
        window={"title": window_title},
        url=url,
        source="test",
    )


def _activity_event(event_type, now, *, seconds, metadata=None):
    return normalize_event(
        event_type,
        timestamp=now - timedelta(seconds=seconds),
        source="test",
        metadata=metadata,
    )
