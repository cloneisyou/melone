"""JSON-RPC method handlers and dispatch table.

Handlers take a params dict and return a JSON-serializable result or raise
RpcError, so pytest can call them without the stdio loop. The daemon stays
stateless: DB connections open and close per request, nothing is cached
(prevents memory growth in a long-lived process).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import traceback
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import timedelta

from melone_service import __version__
from melone_service.config import MELONE_HOME_ENV, load_config
from melone_service.models import utc_now, utc_timestamp
from melone_service.recording import clear_paused, is_paused, set_paused
from melone_service.queries import (
    DEFAULT_RANK_LIMIT,
    DEFAULT_RANK_SINCE_MINUTES,
    DEFAULT_SCENE_PREVIEW_LIMIT,
    DEFAULT_TIMELINE_LIMIT,
    DEFAULT_TIMELINE_SINCE_MINUTES,
    activity_thresholds,
    get_context_graph,
    get_current_context,
    DEFAULT_SCENE_TIMELINE_LIMIT,
    get_ranked_contexts,
    get_scene_timeline,
    get_storage_stats,
    get_timeline,
    list_scene_previews,
    open_event_repository,
    sample_event,
    search_contexts,
    seed_demo_events,
)
from melone_service.rpc.errors import (
    CONFIG_PARSE_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    NOT_SUPPORTED_ON_PLATFORM,
    SERVICE_ERROR,
    RpcError,
)
from melone_service.screen_text_status import build_screen_text_status
from melone_service.setup import ConfigParseError, claude_code, codex, skill
from melone_service.settings import (
    app_settings_path,
    update_screen_text_settings,
)
from melone_service.store.events import EventRepository
from melone_service.store.migrations import read_applied_version

# The error vocabulary lives in rpc/errors.py — import codes from there directly
# rather than re-exporting them here.
__all__ = ["RpcError", "HANDLERS", "dispatch"]

# Upper bounds for client-supplied integers — large values only waste work or
# overflow timedelta, so reject them at the boundary.
MAX_SINCE_MINUTES = 5_256_000  # 10 years
MAX_LIMIT = 10_000

# rank/timeline defaults come from queries (shared with the MCP tools). Graph
# and search defaults are RPC-only: the graph view draws more nodes than rank,
# and the search UI uses a tighter window than the recall-biased MCP search.
DEFAULT_GRAPH_LIMIT = 60
DEFAULT_SEARCH_SINCE_MINUTES = 120
DEFAULT_SEARCH_LIMIT = 5

_SETUP_TARGETS = {"claude-code": claude_code, "codex": codex}


def dispatch(method: str, params: object) -> object:
    """Look up the handler by method name and map domain errors to RPC errors."""
    handler = HANDLERS.get(method)
    if handler is None:
        raise RpcError(
            METHOD_NOT_FOUND, "METHOD_NOT_FOUND", f"알 수 없는 메서드: {method}"
        )
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise RpcError(
            INVALID_PARAMS, "INVALID_PARAMS", "params는 객체여야 합니다"
        )

    try:
        return handler(params)
    except RpcError:
        raise
    except ConfigParseError as error:
        # Propagate the never-write-on-parse-failure signal from setup as-is.
        raise RpcError(CONFIG_PARSE_ERROR, "CONFIG_PARSE_ERROR", str(error)) from error
    except OverflowError as error:
        # Safety net for out-of-range integers that slip past validation.
        raise RpcError(
            INVALID_PARAMS, "INVALID_PARAMS", "파라미터가 허용 범위를 벗어났습니다"
        ) from error
    except sqlite3.Error as error:
        # DB errors (e.g. lock contention with the collector) map to SERVICE_ERROR.
        # error.data stays generic; the raw exception goes to stderr only.
        traceback.print_exc(file=sys.stderr)
        raise RpcError(
            SERVICE_ERROR, "SERVICE_ERROR", "데이터베이스 조회 중 오류가 발생했습니다"
        ) from error


def ping(params: dict[str, object]) -> dict[str, object]:
    # Electron handshake health check — version diagnoses shell/daemon mismatch.
    return {"version": __version__}


def service_status(params: dict[str, object]) -> dict[str, object]:
    """Report service status without writing.

    Unlike CLI status, never records permission events — Electron polls this
    periodically and inserting per call would pile up garbage events.
    """
    config = load_config()
    running = False
    pid: int | None = None
    if sys.platform == "darwin":
        # main.py depends on fcntl and fails to import on Windows — darwin-only lazy import.
        from melone_service import main

        state = main.get_process_state(config)
        running = state.is_running
        pid = state.pid

    # check_permission_status returns an "unsupported" snapshot off darwin,
    # so the response keeps the same shape on every platform.
    from melone_service import permissions

    snapshot = permissions.check_permission_status()
    return {
        "platform": sys.platform,
        "collectorsSupported": sys.platform == "darwin",
        "running": running,
        "paused": is_paused(config.pause_flag_path),
        "pid": pid,
        "dbPath": str(config.database_path),
        "migrationVersion": read_applied_version(config.database_path),
        "permissions": _permissions_payload(snapshot),
    }


def service_start(params: dict[str, object]) -> dict[str, object]:
    # Starting the collector is macOS-only (collectors and the fcntl lock).
    _require_darwin("service.start")
    from melone_service import main

    try:
        result = main.start_service()
    except (RuntimeError, OSError) as error:
        raise RpcError(SERVICE_ERROR, "SERVICE_ERROR", str(error)) from error
    return {"started": result.started, "pid": result.pid}


def service_stop(params: dict[str, object]) -> dict[str, object]:
    _require_darwin("service.stop")
    from melone_service import main

    try:
        result = main.stop_service()
    except (RuntimeError, OSError) as error:
        raise RpcError(SERVICE_ERROR, "SERVICE_ERROR", str(error)) from error
    return {"stopped": result.stopped}


def service_pause(params: dict[str, object]) -> dict[str, object]:
    # Pauses collection without stopping the daemon. Pure flag file, so it works
    # (and is testable) on every platform; the collector loop honors it on macOS.
    set_paused(load_config().pause_flag_path)
    return {"paused": True}


def service_resume(params: dict[str, object]) -> dict[str, object]:
    clear_paused(load_config().pause_flag_path)
    return {"paused": False}


def screen_text_status(params: dict[str, object]) -> dict[str, object]:
    return build_screen_text_status(load_config())


def screen_text_update_settings(params: dict[str, object]) -> dict[str, object]:
    enabled = params.get("enabled")
    if not isinstance(enabled, bool):
        raise RpcError(
            INVALID_PARAMS,
            "INVALID_PARAMS",
            f"enabled는 boolean이어야 합니다: {enabled!r}",
        )

    config = load_config()
    settings_path = config.settings_path or app_settings_path(config.data_dir)
    update_screen_text_settings(settings_path, enabled=enabled)
    return build_screen_text_status(load_config())


def context_current(params: dict[str, object]) -> dict[str, object]:
    config = load_config()
    with open_event_repository(config.database_path) as repository:
        return get_current_context(
            repository, thresholds=activity_thresholds(config)
        )


def context_rank(params: dict[str, object]) -> list[dict[str, object]]:
    # Result shape [{score, visits, kind, label}] per the docs/prod/desktop-plan.md method table.
    since, limit = _time_window(
        params,
        default_since=DEFAULT_RANK_SINCE_MINUTES,
        default_limit=DEFAULT_RANK_LIMIT,
    )
    with _open_repository() as repository:
        return get_ranked_contexts(repository, since=since, limit=limit)


def context_graph(params: dict[str, object]) -> dict[str, object]:
    # {nodes, edges, totalNodes} for the Memory graph view.
    since, limit = _time_window(
        params,
        default_since=DEFAULT_RANK_SINCE_MINUTES,
        default_limit=DEFAULT_GRAPH_LIMIT,
    )
    with _open_repository() as repository:
        return get_context_graph(repository, since=since, limit=limit)


def context_search(params: dict[str, object]) -> dict[str, object]:
    query = params.get("query")
    if not isinstance(query, str) or not query.strip():
        raise RpcError(
            INVALID_PARAMS,
            "INVALID_PARAMS",
            f"query는 비어 있지 않은 문자열이어야 합니다: {query!r}",
        )
    since, limit = _time_window(
        params,
        default_since=DEFAULT_SEARCH_SINCE_MINUTES,
        default_limit=DEFAULT_SEARCH_LIMIT,
    )
    config = load_config()
    with open_event_repository(config.database_path) as repository:
        return search_contexts(
            repository,
            query=query,
            since=since,
            limit=limit,
            include_images=True,
            config=config,
        )


def context_timeline(params: dict[str, object]) -> list[dict[str, object]]:
    since, limit = _time_window(
        params,
        default_since=DEFAULT_TIMELINE_SINCE_MINUTES,
        default_limit=DEFAULT_TIMELINE_LIMIT,
    )
    with _open_repository() as repository:
        return get_timeline(repository, since=since, limit=limit)


def screen_previews(params: dict[str, object]) -> dict[str, object]:
    # {previews: [...]} for the home-page scene gallery. Read-only, like search.
    limit = _positive_int(
        params, "limit", default=DEFAULT_SCENE_PREVIEW_LIMIT, maximum=MAX_LIMIT
    )
    with _open_repository() as repository:
        return list_scene_previews(repository, limit=limit)


def scene_timeline(params: dict[str, object]) -> dict[str, object]:
    # {scenes: [...]} for the timeline view (sticks + scene details + logs).
    # `before` is a started_at cursor for infinite scroll into older scenes.
    limit = _positive_int(
        params, "limit", default=DEFAULT_SCENE_TIMELINE_LIMIT, maximum=MAX_LIMIT
    )
    before = params.get("before")
    if before is not None and not isinstance(before, str):
        raise RpcError(INVALID_PARAMS, "INVALID_PARAMS", "before는 문자열이어야 합니다")
    with _open_repository() as repository:
        return get_scene_timeline(repository, before=before, limit=limit)


def storage_stats(params: dict[str, object]) -> dict[str, object]:
    # {databaseBytes, screenshotBytes, ..., totalBytes, counts} for the Stats page.
    config = load_config()
    with open_event_repository(config.database_path) as repository:
        return get_storage_stats(repository, config=config)


def mcp_status(params: dict[str, object]) -> dict[str, object]:
    """Return detection/registration status for both targets.

    Policy: a broken config for one target must not hide the other, so the
    call never fails with -32003 as a whole; the broken target is marked with
    enabled=null and error="parse_error" (mcp.enable/disable still raise -32003).
    """
    return {
        "claudeCode": _target_status(claude_code),
        "codex": _target_status(codex),
    }


def mcp_enable(params: dict[str, object]) -> dict[str, object]:
    module = _setup_target(params)
    # Pass the MELONE_HOME override through so the registered MCP server
    # reads the same DB.
    result = module.enable(melone_home=os.environ.get(MELONE_HOME_ENV) or None)
    _sync_skill(params, enable=True)
    return _setup_result(result)


def mcp_disable(params: dict[str, object]) -> dict[str, object]:
    module = _setup_target(params)
    result = module.disable()
    _sync_skill(params, enable=False)
    return _setup_result(result)


def _sync_skill(params: dict[str, object], *, enable: bool) -> None:
    """Install/remove the bundled `/melone` skill alongside the MCP entry.

    Best-effort: the target was already validated by _setup_target, and a
    filesystem hiccup writing the skill file must never fail (or undo) the MCP
    toggle itself.
    """
    target = params.get("target")
    if not isinstance(target, str):
        return
    try:
        path = skill.default_skill_path(target)
    except KeyError:
        return
    try:
        if enable:
            skill.install_skill(path)
        else:
            skill.uninstall_skill(path)
    except OSError:
        pass


def events_add_sample(params: dict[str, object]) -> dict[str, object]:
    # Seeds a sample event so the query views can be exercised on Windows dev setups.
    event = sample_event()
    with _open_repository() as repository:
        repository.insert(event)
    return {"eventId": event.id}


def events_seed_demo(params: dict[str, object]) -> dict[str, object]:
    # Seeds a transition sequence so graph/rank/search populate without collectors.
    with _open_repository() as repository:
        inserted = seed_demo_events(repository)
    return {"inserted": inserted}


HANDLERS: dict[str, Callable[[dict[str, object]], object]] = {
    "app.ping": ping,
    "service.status": service_status,
    "service.start": service_start,
    "service.stop": service_stop,
    "service.pause": service_pause,
    "service.resume": service_resume,
    "screenText.status": screen_text_status,
    "screenText.updateSettings": screen_text_update_settings,
    "context.current": context_current,
    "context.rank": context_rank,
    "context.graph": context_graph,
    "context.search": context_search,
    "context.timeline": context_timeline,
    "screen.previews": screen_previews,
    "scene.timeline": scene_timeline,
    "storage.stats": storage_stats,
    "mcp.status": mcp_status,
    "mcp.enable": mcp_enable,
    "mcp.disable": mcp_disable,
    "events.addSample": events_add_sample,
    "events.seedDemo": events_seed_demo,
}


def _require_darwin(method: str) -> None:
    if sys.platform != "darwin":
        raise RpcError(
            NOT_SUPPORTED_ON_PLATFORM,
            "NOT_SUPPORTED_ON_PLATFORM",
            f"{method}는 macOS에서만 지원됩니다 (현재: {sys.platform})",
        )


def _positive_int(
    params: dict[str, object], key: str, *, default: int, maximum: int
) -> int:
    # bool is a subtype of int; reject it explicitly so true cannot pass as 1.
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RpcError(
            INVALID_PARAMS, "INVALID_PARAMS", f"{key}는 양의 정수여야 합니다: {value!r}"
        )
    if value > maximum:
        raise RpcError(
            INVALID_PARAMS,
            "INVALID_PARAMS",
            f"{key}는 {maximum} 이하여야 합니다: {value!r}",
        )
    return value


def _time_window(
    params: dict[str, object], *, default_since: int, default_limit: int
) -> tuple[str, int]:
    # Shared sinceMinutes/limit validation for the context.* handlers.
    since_minutes = _positive_int(
        params, "sinceMinutes", default=default_since, maximum=MAX_SINCE_MINUTES
    )
    limit = _positive_int(params, "limit", default=default_limit, maximum=MAX_LIMIT)
    since = utc_timestamp(utc_now() - timedelta(minutes=since_minutes))
    return since, limit


def _open_repository() -> AbstractContextManager[EventRepository]:
    # Resolves the configured DB path and hands back the repository context manager.
    return open_event_repository(load_config().database_path)


def _permissions_payload(snapshot) -> dict[str, object]:
    # Same content as snapshot.to_metadata() but camelCase on the RPC wire.
    metadata = snapshot.to_metadata()
    return {
        "permissions": metadata["permissions"],
        "collectors": metadata["collectors"],
        "missingRequiredPermissions": metadata["missing_required_permissions"],
    }


def _setup_target(params: dict[str, object]):
    target = params.get("target")
    module = _SETUP_TARGETS.get(target) if isinstance(target, str) else None
    if module is None:
        expected = ", ".join(sorted(_SETUP_TARGETS))
        raise RpcError(
            INVALID_PARAMS,
            "INVALID_PARAMS",
            f"target은 {expected} 중 하나여야 합니다: {target!r}",
        )
    return module


def _target_status(module) -> dict[str, object]:
    # detect() only checks existence and is safe on broken files; is_enabled()
    # parses, so absorb ConfigParseError per target (mcp_status policy).
    status: dict[str, object] = {
        "detected": module.detect(),
        "configPath": str(module.default_config_path()),
    }
    try:
        status["enabled"] = module.is_enabled()
    except ConfigParseError:
        status["enabled"] = None
        status["error"] = "parse_error"
    return status


def _setup_result(result) -> dict[str, object]:
    return {
        "enabled": result.enabled,
        "backupPath": (
            str(result.backup_path) if result.backup_path is not None else None
        ),
    }
