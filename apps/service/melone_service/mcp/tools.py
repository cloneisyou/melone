from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import timedelta

from melone_service import queries
from melone_service.config import ServiceConfig, load_config
from melone_service.models import utc_now, utc_timestamp
from melone_service.queries import (
    DEFAULT_RANK_LIMIT,
    DEFAULT_RANK_SINCE_MINUTES,
    DEFAULT_TIMELINE_LIMIT,
    DEFAULT_TIMELINE_SINCE_MINUTES,
)
from melone_service.store.events import EventRepository


# Tool implementations kept separate from the FastMCP decorators so pytest
# can call them directly without starting a server. Rank/timeline defaults are
# re-exported from queries (shared with the RPC handlers) and stay importable
# here because server.py reads them as the MCP tool schema defaults.
__all__ = [
    "DEFAULT_RANK_SINCE_MINUTES",
    "DEFAULT_RANK_LIMIT",
    "DEFAULT_TIMELINE_SINCE_MINUTES",
    "DEFAULT_TIMELINE_LIMIT",
    "DEFAULT_SEARCH_SINCE_MINUTES",
    "DEFAULT_SEARCH_LIMIT",
    "get_current_context",
    "rank_contexts",
    "search_contexts",
    "get_timeline",
]

# Agent searches are often recall ("that doc from yesterday"), so the default
# window is 24h — intentionally longer than rank's and not shared with the RPC
# search handler, which serves a tighter UI window.
DEFAULT_SEARCH_SINCE_MINUTES = 1440
DEFAULT_SEARCH_LIMIT = 5


def get_current_context() -> dict[str, object]:
    return _query(
        lambda repository, config: queries.get_current_context(
            repository, thresholds=queries.activity_thresholds(config)
        )
    )


def rank_contexts(
    since_minutes: int = DEFAULT_RANK_SINCE_MINUTES,
    limit: int = DEFAULT_RANK_LIMIT,
    show_hidden: bool = False,
) -> dict[str, object]:
    return _query(
        lambda repository, _config: {
            "contexts": queries.get_ranked_contexts(
                repository,
                since=_since_timestamp(since_minutes),
                limit=limit,
                show_hidden=show_hidden,
            )
        },
        empty={"contexts": []},
    )


def search_contexts(
    query: str,
    limit: int = DEFAULT_SEARCH_LIMIT,
    since_minutes: int = DEFAULT_SEARCH_SINCE_MINUTES,
) -> dict[str, object]:
    return _query(
        lambda repository, config: queries.search_contexts(
            repository,
            query=query,
            since=_since_timestamp(since_minutes),
            limit=limit,
            config=config,
        ),
        empty={"results": [], "episodes": []},
    )


def get_timeline(
    since_minutes: int = DEFAULT_TIMELINE_SINCE_MINUTES,
    limit: int = DEFAULT_TIMELINE_LIMIT,
) -> dict[str, object]:
    return _query(
        lambda repository, _config: {
            "events": queries.get_timeline(
                repository,
                since=_since_timestamp(since_minutes),
                limit=limit,
            )
        },
        empty={"events": []},
    )


def _query(
    run: Callable[[EventRepository, ServiceConfig], dict[str, object]],
    *,
    empty: dict[str, object] | None = None,
) -> dict[str, object]:
    """Open a read-only repository, run the query, and shape the MCP payload.

    A missing/locked DB returns an "unavailable" guidance payload (merged with
    `empty` so list fields stay present) instead of raising — a crash in the
    MCP host's child process is hard to debug.
    """
    config = load_config()
    try:
        with queries.open_readonly_event_repository(
            config.database_path
        ) as repository:
            result = run(repository, config)
    except (FileNotFoundError, sqlite3.Error) as error:
        return {**_unavailable_result(config, error), **(empty or {})}

    return {"available": True, **result}


def _since_timestamp(since_minutes: int) -> str:
    return utc_timestamp(utc_now() - timedelta(minutes=since_minutes))


def _unavailable_result(
    config: ServiceConfig,
    error: Exception,
) -> dict[str, object]:
    # A crash in the MCP host's child process is hard to debug, so missing-DB
    # and schema errors become a guidance payload instead of an exception.
    return {
        "available": False,
        "reason": (
            "Melone activity database is not ready"
            f" ({type(error).__name__}: {error})."
            f" Expected at {config.database_path}."
            " Start the Melone service to begin collecting events."
        ),
    }
