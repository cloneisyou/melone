import hashlib
import io
import json
import sqlite3
import subprocess
import sys
from datetime import timedelta

import pytest

from melone_service import __version__
from melone_service.models import utc_now, utc_timestamp
from melone_service.pipeline.normalizer import normalize_event
from melone_service.queries import open_event_repository
from melone_service.rpc import errors, methods, server
from melone_service.rpc.errors import RpcError
from melone_service.rpc.methods import dispatch
from melone_service.setup import claude_code, codex, skill
from melone_service.settings import load_app_settings
from melone_service.store.ocr import OcrChunkRepository
from melone_service.store.screen import ScreenRepository


@pytest.fixture
def melone_home(tmp_path, monkeypatch):
    # RPC handlers resolve paths via load_config(), so inject the test DB with MELONE_HOME.
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def setup_paths(tmp_path, monkeypatch):
    # setup modules fall back to default_config_path() when config_path=None,
    # so replace the function to keep tests away from real user config files.
    claude_path = tmp_path / ".claude.json"
    codex_path = tmp_path / ".codex" / "config.toml"
    monkeypatch.setattr(claude_code, "default_config_path", lambda: claude_path)
    monkeypatch.setattr(codex, "default_config_path", lambda: codex_path)
    # mcp.enable/disable also installs the bundled skill — keep it off real home.
    claude_skill = tmp_path / ".claude" / "skills" / "melone" / "SKILL.md"
    codex_skill = tmp_path / ".codex" / "skills" / "melone" / "SKILL.md"
    monkeypatch.setattr(
        skill,
        "default_skill_path",
        lambda target: claude_skill if target == "claude-code" else codex_skill,
    )
    return claude_path, codex_path


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


def _count_events(database_path):
    with open_event_repository(database_path) as repository:
        return len(repository.list())


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


# --- shared dispatch contract ---


def test_dispatch_unknown_method_raises_method_not_found():
    with pytest.raises(RpcError) as excinfo:
        dispatch("no.such.method", {})

    assert excinfo.value.code == errors.METHOD_NOT_FOUND


def test_dispatch_rejects_non_object_params():
    with pytest.raises(RpcError) as excinfo:
        dispatch("app.ping", [1, 2])

    assert excinfo.value.code == errors.INVALID_PARAMS


def test_dispatch_treats_missing_params_as_empty_object():
    assert dispatch("app.ping", None) == {"version": __version__}


# --- daemon entry dispatch ---


def test_entry_runs_rpc_daemon_by_default(monkeypatch):
    from melone_service.rpc import __main__ as entry

    called = []
    monkeypatch.setattr(entry.sys, "argv", ["melone-daemon"])
    monkeypatch.setattr(server, "main", lambda: called.append("rpc") or 0)

    assert entry.main() == 0
    assert called == ["rpc"]


def test_entry_runs_mcp_server_on_mcp_arg(monkeypatch):
    from melone_service.mcp import server as mcp_server
    from melone_service.rpc import __main__ as entry

    called = []
    monkeypatch.setattr(entry.sys, "argv", ["melone-daemon", "mcp"])
    monkeypatch.setattr(mcp_server, "main", lambda: called.append(list(entry.sys.argv)))
    monkeypatch.setattr(server, "main", lambda: called.append("rpc") or 0)

    assert entry.main() == 0
    # The dispatched subcommand is consumed before the MCP server sees argv.
    assert called == [["melone-daemon"]]


def test_entry_runs_service_on_service_arg(monkeypatch):
    import types

    from melone_service.rpc import __main__ as entry

    called = []
    # melone_service.main imports fcntl (Unix-only); inject a stand-in so the
    # lazy `from melone_service.main import main` resolves to it on any platform.
    fake_main = types.ModuleType("melone_service.main")
    fake_main.main = lambda: called.append(list(entry.sys.argv)) or 0
    monkeypatch.setitem(sys.modules, "melone_service.main", fake_main)
    monkeypatch.setattr(entry.sys, "argv", ["melone-daemon", "service"])

    assert entry.main() == 0
    # The dispatched subcommand is consumed before the collector sees argv.
    assert called == [["melone-daemon"]]


@pytest.mark.skipif(sys.platform != "darwin", reason="main.py imports fcntl (Unix-only)")
def test_service_command_uses_subcommand_when_frozen(monkeypatch):
    from melone_service import main as service_main

    # A frozen binary dispatches on the "service" subcommand, never `-m`.
    monkeypatch.setattr(service_main.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        service_main.sys, "executable", "/Applications/Melone.app/Contents/Resources/melone-daemon"
    )

    assert service_main._service_command() == [
        "/Applications/Melone.app/Contents/Resources/melone-daemon",
        "service",
    ]


@pytest.mark.skipif(sys.platform != "darwin", reason="main.py imports fcntl (Unix-only)")
def test_service_command_uses_module_in_dev(monkeypatch):
    from melone_service import main as service_main

    monkeypatch.delattr(service_main.sys, "frozen", raising=False)
    monkeypatch.setattr(service_main.sys, "executable", "/usr/bin/python3")

    assert service_main._service_command() == [
        "/usr/bin/python3",
        "-m",
        "melone_service.main",
    ]


# --- app.ping ---


def test_app_ping_returns_package_version():
    result = dispatch("app.ping", {})

    assert result == {"version": __version__}
    assert json.dumps(result)


# --- service.status ---


def test_service_status_reports_platform_and_db(melone_home):
    result = dispatch("service.status", {})

    assert result["platform"] == sys.platform
    assert result["collectorsSupported"] is (sys.platform == "darwin")
    assert result["dbPath"] == str(_database_path(melone_home))
    # Wire contract: RPC payload uses camelCase for the missing-permissions key.
    assert set(result["permissions"]) == {
        "permissions",
        "collectors",
        "missingRequiredPermissions",
    }


def test_service_status_does_not_create_database(melone_home):
    # Read-only contract: polling must not even trigger migrations.
    result = dispatch("service.status", {})

    assert result["migrationVersion"] == 0
    assert not _database_path(melone_home).exists()


def test_service_status_is_read_only(melone_home):
    with open_event_repository(_database_path(melone_home)) as repository:
        repository.insert(_context_event("Cursor", "methods.py", utc_now(), 5))
    before = _count_events(_database_path(melone_home))

    result = dispatch("service.status", {})

    # Unlike CLI status, RPC status must not insert permission events.
    assert _count_events(_database_path(melone_home)) == before
    assert result["migrationVersion"] == 5


def test_service_status_survives_corrupted_database(melone_home):
    # A corrupted DB file must degrade to version 0, not fail the status poll.
    _database_path(melone_home).write_bytes(b"\xffnot a sqlite database\x00" * 16)

    result = dispatch("service.status", {})

    assert result["migrationVersion"] == 0


@pytest.mark.skipif(sys.platform == "darwin", reason="exercises the non-darwin branch")
def test_service_status_reports_not_running_off_macos(melone_home):
    result = dispatch("service.status", {})

    assert result["running"] is False
    assert result["pid"] is None
    permission_statuses = {
        check["status"] for check in result["permissions"]["permissions"].values()
    }
    assert permission_statuses == {"unsupported"}


# --- service.pause / service.resume ---


def test_service_status_reports_not_paused_by_default(melone_home):
    assert dispatch("service.status", {})["paused"] is False


def test_pause_then_resume_toggles_status(melone_home):
    assert dispatch("service.pause", {}) == {"paused": True}
    assert dispatch("service.status", {})["paused"] is True

    assert dispatch("service.resume", {}) == {"paused": False}
    assert dispatch("service.status", {})["paused"] is False


def test_pause_and_resume_are_idempotent(melone_home):
    dispatch("service.pause", {})
    assert dispatch("service.pause", {}) == {"paused": True}

    dispatch("service.resume", {})
    # Resuming when not paused must not raise.
    assert dispatch("service.resume", {}) == {"paused": False}


# --- screenText.status / screenText.updateSettings ---


def test_screen_text_status_defaults_off_without_writing_settings_file(melone_home):
    result = dispatch("screenText.status", {})

    assert result["state"] == "off"
    assert result["reason"] == "disabled"
    assert result["settings"] == {"enabled": False, "retainScreenshots": False}
    assert result["enabled"] is False
    assert result["effectiveEnabled"] is False
    assert result["screenshotCollectorEnabled"] is False
    assert result["workersEnabled"] is False
    assert result["developmentOverrides"] == {
        "screenshotCollector": False,
        "workers": False,
    }
    assert result["requiredPermissions"] == []
    assert result["backlogCount"] == 0
    assert result["lastError"] is None
    assert not (melone_home / "settings.json").exists()


def test_screen_text_update_settings_persists_across_status_calls(melone_home):
    enabled = dispatch("screenText.updateSettings", {"enabled": True})
    status = dispatch("screenText.status", {})

    assert enabled["state"] != "off"
    assert enabled["settings"] == {"enabled": True, "retainScreenshots": False}
    assert enabled["enabled"] is True
    assert enabled["effectiveEnabled"] is True
    assert enabled["screenshotCollectorEnabled"] is True
    assert enabled["workersEnabled"] is True
    assert status["settings"] == enabled["settings"]
    assert status["enabled"] is True
    assert status["effectiveEnabled"] is True
    assert load_app_settings(melone_home / "settings.json").screen_text.enabled is True


def test_screen_text_update_settings_can_disable_existing_setting(melone_home):
    dispatch("screenText.updateSettings", {"enabled": True})

    disabled = dispatch("screenText.updateSettings", {"enabled": False})

    assert disabled["state"] == "off"
    assert disabled["settings"] == {"enabled": False, "retainScreenshots": False}
    assert disabled["effectiveEnabled"] is False
    assert load_app_settings(melone_home / "settings.json").screen_text.enabled is False


def test_screen_text_status_reports_development_env_overrides(melone_home, monkeypatch):
    monkeypatch.setenv("MELONE_SCREENSHOT_COLLECTOR_ENABLED", "true")
    monkeypatch.setenv("MELONE_SCREEN_SEARCH_WORKERS_ENABLED", "true")

    result = dispatch("screenText.status", {})

    assert result["state"] != "off"
    assert result["settings"] == {"enabled": False, "retainScreenshots": False}
    assert result["effectiveEnabled"] is True
    assert result["screenshotCollectorEnabled"] is True
    assert result["workersEnabled"] is True
    assert result["developmentOverrides"] == {
        "screenshotCollector": True,
        "workers": True,
    }


@pytest.mark.parametrize("params", [{}, {"enabled": "yes"}, {"enabled": 1}])
def test_screen_text_update_settings_rejects_invalid_enabled(melone_home, params):
    with pytest.raises(RpcError) as excinfo:
        dispatch("screenText.updateSettings", params)

    assert excinfo.value.code == errors.INVALID_PARAMS


# --- service.start / service.stop ---


@pytest.mark.skipif(sys.platform == "darwin", reason="exercises the non-darwin branch")
def test_service_start_not_supported_off_macos():
    with pytest.raises(RpcError) as excinfo:
        dispatch("service.start", {})

    assert excinfo.value.code == errors.NOT_SUPPORTED_ON_PLATFORM


@pytest.mark.skipif(sys.platform == "darwin", reason="exercises the non-darwin branch")
def test_service_stop_not_supported_off_macos():
    with pytest.raises(RpcError) as excinfo:
        dispatch("service.stop", {})

    assert excinfo.value.code == errors.NOT_SUPPORTED_ON_PLATFORM


# --- context.current / context.rank ---


def test_context_current_returns_latest_context(melone_home):
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

    result = dispatch("context.current", {})

    assert result == {
        "app": "Safari",
        "window": "Melone Docs",
        "url": "https://example.com/melone",
        "activity": "active",
    }
    assert json.dumps(result)


def test_context_rank_returns_ranked_list(melone_home):
    now = utc_now()
    with open_event_repository(_database_path(melone_home)) as repository:
        repository.insert(_context_event("Cursor", "methods.py - melone", now, 20))
        repository.insert(_context_event("Slack", "dev - Clone - Slack", now, 10))

    result = dispatch("context.rank", {})

    assert isinstance(result, list)
    assert len(result) == 2
    for item in result:
        assert set(item) == {"score", "visits", "kind", "label"}
    assert json.dumps(result)


def test_context_rank_applies_since_and_limit(melone_home):
    now = utc_now()
    with open_event_repository(_database_path(melone_home)) as repository:
        repository.insert(_context_event("Old App", "stale", now, 3 * 3600))
        repository.insert(_context_event("Cursor", "A", now, 30))
        repository.insert(_context_event("Slack", "B", now, 20))

    result = dispatch("context.rank", {"sinceMinutes": 60, "limit": 1})

    assert len(result) == 1
    assert "Old App" not in result[0]["label"]


@pytest.mark.parametrize(
    "params",
    [
        {"sinceMinutes": "abc"},
        {"sinceMinutes": 0},
        {"sinceMinutes": True},
        {"sinceMinutes": 10**18},
        {"limit": -1},
        {"limit": 10**18},
    ],
)
def test_context_rank_rejects_invalid_params(melone_home, params):
    with pytest.raises(RpcError) as excinfo:
        dispatch("context.rank", params)

    assert excinfo.value.code == errors.INVALID_PARAMS


def test_dispatch_maps_overflow_to_invalid_params(melone_home, monkeypatch):
    # Safety net: even if the bound check is bypassed, the timedelta
    # OverflowError must surface as INVALID_PARAMS, not INTERNAL_ERROR.
    monkeypatch.setattr(methods, "MAX_SINCE_MINUTES", 10**19)

    with pytest.raises(RpcError) as excinfo:
        dispatch("context.rank", {"sinceMinutes": 10**18})

    assert excinfo.value.code == errors.INVALID_PARAMS


def test_dispatch_hides_sqlite_error_details(melone_home, monkeypatch):
    # SERVICE_ERROR data must stay generic; raw DB errors go to stderr only.
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("secret: /Users/x/melone.sqlite is locked")

    monkeypatch.setattr(methods, "get_ranked_contexts", boom)

    with pytest.raises(RpcError) as excinfo:
        dispatch("context.rank", {})

    assert excinfo.value.code == errors.SERVICE_ERROR
    assert excinfo.value.data == "데이터베이스 조회 중 오류가 발생했습니다"


# --- context.graph ---


def test_context_graph_returns_nodes_and_edges(melone_home):
    now = utc_now()
    with open_event_repository(_database_path(melone_home)) as repository:
        repository.insert(_context_event("Cursor", "methods.py", now, 30))
        repository.insert(_context_event("Slack", "dev", now, 20))
        repository.insert(_context_event("Cursor", "methods.py", now, 10))

    result = dispatch("context.graph", {})

    assert set(result) == {"nodes", "edges", "totalNodes"}
    assert result["totalNodes"] == 2
    for node in result["nodes"]:
        assert set(node) == {"key", "kind", "label", "score", "visits"}
    node_keys = {node["key"] for node in result["nodes"]}
    assert node_keys == {"app_window:cursor:methods.py", "app_window:slack:dev"}
    assert len(result["edges"]) == 2  # Cursor<->Slack transitions both ways
    for edge in result["edges"]:
        assert set(edge) == {"source", "target", "weight"}
        assert {edge["source"], edge["target"]} <= node_keys
    assert json.dumps(result)


def test_context_graph_applies_since_and_limit(melone_home):
    now = utc_now()
    with open_event_repository(_database_path(melone_home)) as repository:
        repository.insert(_context_event("Old App", "stale", now, 3 * 3600))
        repository.insert(_context_event("Cursor", "A", now, 30))
        repository.insert(_context_event("Slack", "B", now, 20))

    result = dispatch("context.graph", {"sinceMinutes": 60, "limit": 1})

    assert result["totalNodes"] == 2  # sinceMinutes excludes Old App entirely
    assert len(result["nodes"]) == 1
    assert "Old App" not in result["nodes"][0]["label"]
    assert result["edges"] == []  # no edge survives when one endpoint is trimmed


@pytest.mark.parametrize(
    "params",
    [
        {"sinceMinutes": "abc"},
        {"sinceMinutes": 0},
        {"sinceMinutes": True},
        {"sinceMinutes": 10**18},
        {"limit": -1},
        {"limit": True},
    ],
)
def test_context_graph_rejects_invalid_params(melone_home, params):
    with pytest.raises(RpcError) as excinfo:
        dispatch("context.graph", params)

    assert excinfo.value.code == errors.INVALID_PARAMS


# --- context.search ---


def test_context_search_returns_results_and_episodes(melone_home):
    now = utc_now()
    with open_event_repository(_database_path(melone_home)) as repository:
        repository.insert(_context_event("Cursor", "melone search", now, 20))
        repository.insert(_context_event("Slack", "general", now, 10))

    result = dispatch("context.search", {"query": "MELONE"})

    assert set(result) == {"results", "episodes"}
    assert [item["label"] for item in result["results"]] == ["Cursor | melone search"]
    for item in result["results"]:
        assert set(item) == {
            "key", "kind", "label", "uri", "score", "visits", "lastSeenAt",
        }
    assert [episode["window"] for episode in result["episodes"]] == ["melone search"]
    assert json.dumps(result)


def test_context_search_returns_ocr_matches_for_desktop(melone_home):
    now = utc_now()
    with open_event_repository(_database_path(melone_home)) as repository:
        _seed_ocr_chunk(
            repository.connection,
            chunk_id="ocr_chunk_desktop",
            session_id="screen_session_desktop",
            frame_id="screen_frame_desktop",
            source_key="url:https://example.com/desktop",
            retrieval_locator="url:https://example.com/desktop",
            text="desktop search should find this OCR invoice",
            started_at=utc_timestamp(now - timedelta(seconds=30)),
            ended_at=utc_timestamp(now - timedelta(seconds=20)),
        )

    result = dispatch("context.search", {"query": "invoice"})

    assert result["results"][0]["matchSource"] == "ocr"
    assert result["results"][0]["uri"] == "https://example.com/desktop"
    assert "invoice" in result["results"][0]["snippet"]
    assert result["episodes"][0]["matchSource"] == "ocr"
    assert "invoice" in result["episodes"][0]["snippet"]
    assert json.dumps(result)


def test_context_search_applies_since_and_limit(melone_home):
    now = utc_now()
    with open_event_repository(_database_path(melone_home)) as repository:
        repository.insert(_context_event("Old App", "doc stale", now, 3 * 3600))
        repository.insert(_context_event("Cursor", "doc a", now, 30))
        repository.insert(_context_event("Slack", "doc b", now, 20))

    result = dispatch("context.search", {"query": "doc", "sinceMinutes": 60, "limit": 1})

    assert len(result["results"]) == 1
    assert "Old App" not in result["results"][0]["label"]
    assert all("Old App" != episode["app"] for episode in result["episodes"])


@pytest.mark.parametrize(
    "params",
    [
        {},  # missing query
        {"query": ""},
        {"query": "   "},
        {"query": 5},
        {"query": "doc", "sinceMinutes": 0},
        {"query": "doc", "sinceMinutes": 10**18},
        {"query": "doc", "limit": True},
    ],
)
def test_context_search_rejects_invalid_params(melone_home, params):
    with pytest.raises(RpcError) as excinfo:
        dispatch("context.search", params)

    assert excinfo.value.code == errors.INVALID_PARAMS


# --- context.timeline ---


def test_context_timeline_returns_recent_events_in_order(melone_home):
    now = utc_now()
    with open_event_repository(_database_path(melone_home)) as repository:
        repository.insert(_context_event("Old App", "stale", now, 3 * 3600))
        repository.insert(_context_event("Cursor", "melone", now, 10))
        repository.insert(_context_event("Slack", "dev", now, 5))

    result = dispatch("context.timeline", {})

    # Default sinceMinutes=60, so the 3-hour-old event is excluded.
    assert [item["app"] for item in result] == ["Cursor", "Slack"]
    for item in result:
        assert set(item) == {"timestamp", "type", "app", "window", "url"}
    assert json.dumps(result)


def test_context_timeline_applies_limit(melone_home):
    now = utc_now()
    with open_event_repository(_database_path(melone_home)) as repository:
        for seconds in range(3):
            repository.insert(_context_event("Cursor", f"W{seconds}", now, seconds))

    result = dispatch("context.timeline", {"limit": 2})

    assert len(result) == 2


@pytest.mark.parametrize(
    "params",
    [
        {"sinceMinutes": "abc"},
        {"sinceMinutes": 0},
        {"sinceMinutes": True},
        {"sinceMinutes": 10**18},
        {"limit": -1},
        {"limit": True},
    ],
)
def test_context_timeline_rejects_invalid_params(melone_home, params):
    with pytest.raises(RpcError) as excinfo:
        dispatch("context.timeline", params)

    assert excinfo.value.code == errors.INVALID_PARAMS


# --- events.addSample ---


def test_events_add_sample_inserts_event(melone_home):
    result = dispatch("events.addSample", {})

    with open_event_repository(_database_path(melone_home)) as repository:
        events = repository.list()
    assert len(events) == 1
    assert result == {"eventId": events[0].id}


def test_events_seed_demo_populates_the_graph(melone_home):
    result = dispatch("events.seedDemo", {})

    assert result["inserted"] > 0
    # The seeded transitions must produce a non-empty graph and ranking.
    graph = dispatch("context.graph", {"sinceMinutes": 120, "limit": 60})
    assert len(graph["nodes"]) > 1
    assert len(graph["edges"]) > 0
    assert len(dispatch("context.rank", {"sinceMinutes": 120, "limit": 10})) > 0


# --- mcp.status / mcp.enable / mcp.disable ---


def test_mcp_status_reports_both_targets(setup_paths):
    claude_path, codex_path = setup_paths

    result = dispatch("mcp.status", {})

    assert result["claudeCode"] == {
        "detected": False,
        "enabled": False,
        "configPath": str(claude_path),
    }
    assert result["codex"]["detected"] is False
    assert result["codex"]["configPath"] == str(codex_path)


def test_mcp_enable_and_disable_roundtrip(setup_paths):
    claude_path, _ = setup_paths

    enabled = dispatch("mcp.enable", {"target": "claude-code"})
    status = dispatch("mcp.status", {})
    disabled = dispatch("mcp.disable", {"target": "claude-code"})

    assert enabled == {"enabled": True, "backupPath": None}  # new file, no backup
    assert status["claudeCode"]["detected"] is True
    assert status["claudeCode"]["enabled"] is True
    assert disabled["enabled"] is False
    assert disabled["backupPath"] is not None
    config = json.loads(claude_path.read_text(encoding="utf-8"))
    assert "melone" not in config.get("mcpServers", {})


def test_mcp_enable_codex_writes_toml(setup_paths):
    _, codex_path = setup_paths

    result = dispatch("mcp.enable", {"target": "codex"})

    assert result["enabled"] is True
    assert "[mcp_servers.melone]" in codex_path.read_text(encoding="utf-8")


def test_mcp_enable_disable_syncs_skill(setup_paths):
    # Enabling installs the bundled /melone skill; disabling removes it.
    skill_path = skill.default_skill_path("claude-code")

    dispatch("mcp.enable", {"target": "claude-code"})
    assert skill_path.is_file()
    assert "name: melone" in skill_path.read_text(encoding="utf-8")

    dispatch("mcp.disable", {"target": "claude-code"})
    assert skill_path.is_file() is False


def test_mcp_enable_survives_skill_write_failure(setup_paths, monkeypatch):
    # A skill-install hiccup must not fail the MCP toggle itself.
    def boom(_path):
        raise OSError("disk full")

    monkeypatch.setattr(skill, "install_skill", boom)
    result = dispatch("mcp.enable", {"target": "claude-code"})
    assert result["enabled"] is True


@pytest.mark.parametrize("params", [{}, {"target": "vscode"}, {"target": 3}])
def test_mcp_enable_rejects_unknown_target(setup_paths, params):
    with pytest.raises(RpcError) as excinfo:
        dispatch("mcp.enable", params)

    assert excinfo.value.code == errors.INVALID_PARAMS


def test_mcp_status_marks_broken_config_per_target(setup_paths):
    claude_path, _ = setup_paths
    claude_path.write_text("{ not json", encoding="utf-8")

    result = dispatch("mcp.status", {})

    # One broken target must not fail the whole call; only that target is marked.
    assert result["claudeCode"] == {
        "detected": True,
        "enabled": None,
        "error": "parse_error",
        "configPath": str(claude_path),
    }
    assert result["codex"]["enabled"] is False


def test_mcp_enable_broken_config_raises_parse_error(setup_paths):
    claude_path, _ = setup_paths
    claude_path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(RpcError) as excinfo:
        dispatch("mcp.enable", {"target": "claude-code"})

    assert excinfo.value.code == errors.CONFIG_PARSE_ERROR
    # The never-write-on-parse-failure safeguard must hold through RPC as well.
    assert claude_path.read_text(encoding="utf-8") == "{ not json"


# --- server line handling / loop ---


def test_handle_line_returns_result_response():
    response = json.loads(
        server.handle_line('{"jsonrpc":"2.0","id":7,"method":"app.ping"}')
    )

    assert response == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"version": __version__},
    }


def test_handle_line_parse_error_uses_null_id():
    response = json.loads(server.handle_line("{ broken json"))

    assert response["id"] is None
    assert response["error"]["code"] == errors.PARSE_ERROR


def test_handle_line_unknown_method_echoes_id():
    response = json.loads(
        server.handle_line('{"jsonrpc":"2.0","id":9,"method":"nope"}')
    )

    assert response["id"] == 9
    assert response["error"]["code"] == errors.METHOD_NOT_FOUND


def test_handle_line_skips_blank_line():
    assert server.handle_line("\n") is None


def test_handle_request_rejects_non_object_request():
    response = server.handle_request(["not", "a", "request"])

    assert response["id"] is None
    assert response["error"]["code"] == errors.INVALID_REQUEST


def test_handle_request_converts_unexpected_exception(monkeypatch):
    # A handler bug must become an INTERNAL_ERROR response, not kill the daemon.
    def boom(method, params):
        raise ValueError("secret internal detail")

    monkeypatch.setattr(server, "dispatch", boom)

    response = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "app.ping"})

    assert response["error"]["code"] == errors.INTERNAL_ERROR
    # The raw exception text must never reach the wire (stderr only).
    assert response["error"]["data"] == "요청 처리 중 내부 오류가 발생했습니다"


def test_handle_request_responds_to_notification():
    # Requests without an id (notifications) also get a response — the single
    # self-client (Electron shell) reads one reply per line it writes.
    response = server.handle_request({"jsonrpc": "2.0", "method": "app.ping"})

    assert response["id"] is None
    assert response["result"] == {"version": __version__}


def test_handle_line_converts_unserializable_result(monkeypatch):
    # json.dumps failure on a handler result must keep the request id and
    # come back as INTERNAL_ERROR instead of escaping the loop.
    monkeypatch.setitem(methods.HANDLERS, "app.ping", lambda params: object())

    response = json.loads(
        server.handle_line('{"jsonrpc":"2.0","id":11,"method":"app.ping"}')
    )

    assert response["id"] == 11
    assert response["error"]["code"] == errors.INTERNAL_ERROR
    assert response["error"]["data"] == "요청 처리 중 내부 오류가 발생했습니다"


def test_serve_survives_unserializable_result(monkeypatch, melone_home):
    # The daemon must keep serving subsequent requests after a bad handler result.
    monkeypatch.setitem(methods.HANDLERS, "bad.method", lambda params: {"p": object()})
    stdin = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"bad.method"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"app.ping"}\n'
    )
    stdout = io.StringIO()

    server.serve(stdin, stdout)

    lines = stdout.getvalue().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["error"]["code"] == errors.INTERNAL_ERROR
    assert json.loads(lines[1])["result"] == {"version": __version__}


def test_serve_responds_per_line_and_exits_on_eof(melone_home):
    stdin = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"app.ping"}\n'
        "not json\n"
    )
    stdout = io.StringIO()

    server.serve(stdin, stdout)  # hangs the test if serve does not return on EOF

    lines = stdout.getvalue().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["result"] == {"version": __version__}
    assert json.loads(lines[1])["error"]["code"] == errors.PARSE_ERROR


def test_dispatch_permission_probe_emits_json(monkeypatch, capsys):
    # The "permission-probe" subcommand routes to run_permission_probe, which
    # prints the Accessibility status as JSON and exits 0. Off macOS the
    # framework load fails and it reports "denied" (a clean, parseable result).
    from melone_service.rpc.__main__ import main as dispatch_main

    monkeypatch.setattr(sys, "argv", ["melone-daemon", "permission-probe"])
    assert dispatch_main() == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["accessibility"] in {"granted", "denied"}


def test_serve_stops_collector_on_eof(monkeypatch):
    # On daemon exit the collector it spawned must be stopped, or it orphans and
    # keeps the SQLite DB locked across the next launch/update.
    calls: list[bool] = []
    monkeypatch.setattr(server, "_stop_collector_on_exit", lambda: calls.append(True))

    server.serve(io.StringIO(""), io.StringIO())

    assert calls == [True]


def test_serve_stops_collector_after_handling_requests(monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(server, "_stop_collector_on_exit", lambda: calls.append(True))
    stdout = io.StringIO()

    server.serve(io.StringIO('{"jsonrpc":"2.0","id":1,"method":"app.ping"}\n'), stdout)

    assert json.loads(stdout.getvalue().splitlines()[0])["result"] == {"version": __version__}
    assert calls == [True]


def test_stop_collector_on_exit_is_noop_off_darwin(monkeypatch):
    # Off macOS there is no collector and main.py cannot import (needs fcntl):
    # the cleanup must return without touching anything.
    monkeypatch.setattr(server.sys, "platform", "win32")
    server._stop_collector_on_exit()  # must not raise


def test_rpc_modules_import_without_main_module():
    # main.py cannot be imported on Windows (fcntl), so verify in an isolated
    # process that rpc never pulls it in through any path.
    code = (
        "import sys; "
        "import melone_service.rpc.server, melone_service.rpc.methods; "
        "assert 'melone_service.main' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
