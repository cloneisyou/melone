import base64
import io
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image

from .config import ACTIVITY_EVENT_LIMIT, ServiceConfig
from .models import NormalizedEvent, utc_now, utc_timestamp
from .pipeline.activity import (
    ACTIVITY_EVENT_TYPES,
    ActivityThresholds,
    classify_activity_state,
)
from .pipeline.context_graph import (
    build_transition_graph,
    page_rank,
    rank_contexts,
)
from .pipeline.context_pages import (
    CONTEXT_GRAPH_EVENT_TYPES,
    ContextPage,
    ContextUnit,
    build_context_units,
    normalize_context_page,
)
from .pipeline.normalizer import normalize_event
from .search import (
    ScreenSearchResult,
    ScreenSearchService,
    SemanticSearchCandidateProvider,
    normalize_context_scores,
)
from .store.context_rank import ContextRankRepository
from .store.db import connect, connect_readonly
from .store.events import DEFAULT_EVENT_LIMIT, EventRepository
from .store.migrations import run_migrations
from .store.ocr import OcrChunkRepository
from .store.screen import ScenePreview, ScreenRepository, ScreenSession
from .store.search import ScreenSearchRepository


# Query layer shared by CLI/MCP/RPC. Must not import main.py (fcntl) so it
# stays usable on Windows.

CONTEXT_EVENT_TYPES = (
    "active_app_snapshot",
    "active_app_changed",
    "window_title_changed",
    "current_asset_changed",
)
CONTEXT_RANKING_EVENT_LIMIT = DEFAULT_EVENT_LIMIT
# Ranking feeds graph events AND activity events so the pipeline can weight by
# engagement (dwell time), matching the CLI's CONTEXT_RANKING_EVENT_TYPES.
CONTEXT_RANKING_EVENT_TYPES = (*CONTEXT_GRAPH_EVENT_TYPES, *ACTIVITY_EVENT_TYPES)
# Max episodes returned with search results — part of the renderer contract.
SEARCH_EPISODE_LIMIT = 10

# Default query windows shared by the MCP tools and the RPC handlers so the two
# callers cannot drift. Search defaults are intentionally NOT shared — MCP biases
# toward recall (24h) while the RPC-backed UI shows a tighter window — so each
# caller keeps its own search defaults.
DEFAULT_RANK_SINCE_MINUTES = 120
DEFAULT_RANK_LIMIT = 10
DEFAULT_TIMELINE_SINCE_MINUTES = 60
DEFAULT_TIMELINE_LIMIT = 100


def activity_thresholds(config: ServiceConfig) -> ActivityThresholds:
    """Build the activity-classification thresholds from a ServiceConfig.

    Shared by get_current_context callers (MCP tools, RPC handlers) so the
    threshold wiring lives next to the query that consumes it.
    """
    return ActivityThresholds(
        active_window_seconds=config.activity_active_window_seconds,
        idle_timeout_seconds=config.idle_timeout_seconds,
    )


@contextmanager
def open_event_repository(database_path: Path) -> Iterator[EventRepository]:
    connection = connect(database_path)
    try:
        run_migrations(connection)
        yield EventRepository(connection)
    finally:
        connection.close()


@contextmanager
def open_readonly_event_repository(
    database_path: Path,
) -> Iterator[EventRepository]:
    """Open an EventRepository on a read-only connection (no migrations).

    Raises FileNotFoundError when database_path is missing — read-only mode
    cannot create the file, and callers must handle the empty-DB case.
    """
    if not database_path.exists():
        raise FileNotFoundError(f"Melone database not found: {database_path}")

    connection = connect_readonly(database_path)
    try:
        yield EventRepository(connection)
    finally:
        connection.close()


def get_current_context(
    repository: EventRepository,
    *,
    thresholds: ActivityThresholds,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return the latest app/window/URL context and activity state."""
    reference_time = utc_now() if now is None else now
    since = utc_timestamp(
        reference_time - timedelta(seconds=thresholds.idle_timeout_seconds)
    )

    context_event = repository.latest_by_types(CONTEXT_EVENT_TYPES)
    activity_events = repository.list_by_types(
        ACTIVITY_EVENT_TYPES,
        since=since,
        limit=ACTIVITY_EVENT_LIMIT,
    )
    activity_state = classify_activity_state(
        activity_events,
        thresholds=thresholds,
        now=reference_time,
    )
    return {
        "app": context_event.app_name if context_event else None,
        "window": context_event.window_title if context_event else None,
        "url": context_event.url if context_event else None,
        "activity": activity_state,
    }


def get_ranked_contexts(
    repository: EventRepository,
    *,
    since: str | None = None,
    limit: int | None = None,
    show_hidden: bool = False,
) -> list[dict[str, object]]:
    """Return stored context events compressed into a PageRank ranking."""
    events = repository.list_by_types(
        CONTEXT_RANKING_EVENT_TYPES,
        since=since,
        limit=CONTEXT_RANKING_EVENT_LIMIT,
    )
    ranked_contexts = rank_contexts(events, limit=limit, show_hidden=show_hidden)
    return [
        {
            "score": ranked_context.score,
            "visits": ranked_context.visits,
            "kind": ranked_context.page.kind,
            "label": ranked_context.page.label,
        }
        for ranked_context in ranked_contexts
    ]


def get_context_graph(
    repository: EventRepository,
    *,
    since: str | None = None,
    limit: int = 60,
) -> dict[str, object]:
    """Build the transition graph for the desktop Memory graph view.

    Same pipeline as rank_contexts but also exposes edges. The schema
    {nodes, edges, totalNodes} is a renderer contract — do not rename fields.
    """
    index = _build_context_index(repository, since=since)
    pages_by_key = index.pages_by_key
    graph = index.graph

    # Besides graph nodes (bridges already excluded by the graph builder),
    # include rankable standalone nodes that have no transitions.
    node_keys = {
        to_key for to_edges in graph.values() for to_key in to_edges
    } | set(graph)
    node_keys |= {key for key, page in pages_by_key.items() if page.rankable}

    nodes = [
        {
            "key": key,
            "kind": pages_by_key[key].kind,
            "label": pages_by_key[key].label,
            "score": index.scores.get(key, 0.0),
            "visits": index.visits_by_key.get(key, 0),
        }
        for key in node_keys
    ]
    # Score desc, then label, for deterministic output.
    nodes.sort(key=lambda node: (-node["score"], node["label"]))

    total_nodes = len(nodes)
    kept_nodes = nodes[: max(limit, 0)]
    kept_keys = {node["key"] for node in kept_nodes}

    # Drop edges touching nodes trimmed by limit — the renderer cannot draw them.
    edges = [
        {"source": from_key, "target": to_key, "weight": weight}
        for from_key, to_edges in graph.items()
        for to_key, weight in to_edges.items()
        if from_key in kept_keys and to_key in kept_keys
    ]
    edges.sort(key=lambda edge: (edge["source"], edge["target"]))

    return {"nodes": kept_nodes, "edges": edges, "totalNodes": total_nodes}


def search_contexts(
    repository: EventRepository,
    *,
    query: str,
    since: str | None = None,
    limit: int = 5,
    include_images: bool = False,
    config: ServiceConfig | None = None,
    semantic_candidate_provider: SemanticSearchCandidateProvider | None = None,
) -> dict[str, object]:
    """Find top contexts or screen text matching a keyword, with matching episodes.

    The schema {results: [{key, kind, label, uri, score, visits, lastSeenAt,
    matchSource?, snippet?}], episodes: [{startedAt, endedAt, app, window,
    url, matchSource?, snippet?}]} is shared by the search UI and MCP agents
    — do not rename fields.

    include_images is opt-in (the desktop UI only): when set, each result gains
    an `image` base64 thumbnail of a representative retained screenshot, or None.
    MCP callers leave it off so agent responses stay text-only.
    """
    needle = query.strip().casefold()
    if not needle:
        # A blank query is indistinguishable from a list-all request; reject it.
        raise ValueError("query must not be blank")

    index = _build_context_index(repository, since=since)

    # lastSeenAt must be a non-null timestamp (contract), so an in-progress
    # final unit without ended_at falls back to started_at.
    last_seen_by_key: dict[str, str] = {}
    for unit, page in index.unit_pages:
        last_seen_by_key[page.key] = unit.ended_at or unit.started_at

    search_scores = _context_search_scores(index)
    matched = [
        _context_search_result(
            key=key,
            page=page,
            score=search_scores.get(key, 0.0),
            visits=index.visits_by_key.get(key, 0),
            last_seen_at=last_seen_by_key[key],
        )
        for key, page in index.pages_by_key.items()
        if _matches_query(page, needle)
    ]

    episodes = [
        {
            "startedAt": unit.started_at,
            "endedAt": unit.ended_at,
            "app": unit.app_name,
            "window": unit.window_title,
            "url": unit.url,
        }
        for unit, page in reversed(index.unit_pages)
        if _matches_query(page, needle)
    ]
    ocr_matches = _screen_ocr_matches(
        repository,
        query=query,
        since=since,
        limit=max(limit, SEARCH_EPISODE_LIMIT),
        context_rank_scores=search_scores,
        config=config,
        semantic_candidate_provider=semantic_candidate_provider,
    )
    ocr_results = [
        _screen_ocr_search_result(
            result,
            visits=index.visits_by_key.get(result.source_key, 0),
        )
        for result in ocr_matches[: max(limit, 0)]
    ]
    matched = _merge_search_results(matched, ocr_results)
    episodes = [
        *episodes,
        *[
            {
                "startedAt": result.started_at,
                "endedAt": result.ended_at,
                "app": result.app_name,
                "window": result.window_title,
                "url": result.url,
                "matchSource": "ocr",
                "snippet": result.preview,
            }
            for result in ocr_matches[:SEARCH_EPISODE_LIMIT]
        ],
    ]
    episodes.sort(key=lambda episode: str(episode["startedAt"]), reverse=True)

    results = matched[: max(limit, 0)]
    if include_images:
        _attach_result_images(repository, results)

    return {
        "results": results,
        "episodes": episodes[:SEARCH_EPISODE_LIMIT],
    }


def _attach_result_images(
    repository: EventRepository, results: list[dict[str, object]]
) -> None:
    # Map each result to a representative retained screenshot via its source_key
    # (the result `key`), attaching a thumbnail data URL or None.
    screen_repository = ScreenRepository(repository.connection)
    for result in results:
        preview = screen_repository.get_scene_preview_for_source_key(
            str(result["key"])
        )
        result["image"] = (
            _thumbnail_data_url(Path(preview.image_path))
            if preview is not None
            else None
        )


@dataclass(frozen=True)
class _ContextIndex:
    # Ranking inputs computed once from context events, shared by
    # get_context_graph and search_contexts to avoid duplicate pipeline runs.
    unit_pages: list[tuple[ContextUnit, ContextPage]]
    pages_by_key: dict[str, ContextPage]
    visits_by_key: dict[str, int]
    graph: dict[str, dict[str, float]]
    scores: dict[str, float]


def _build_context_index(
    repository: EventRepository,
    *,
    since: str | None = None,
) -> _ContextIndex:
    # Fetch graph + activity events; build_context_units filters to the graph
    # types internally, while build_transition_graph uses the activity events
    # for engagement (dwell-time) edge weighting.
    events = repository.list_by_types(
        CONTEXT_RANKING_EVENT_TYPES,
        since=since,
        limit=CONTEXT_RANKING_EVENT_LIMIT,
    )
    units = build_context_units(events)

    unit_pages: list[tuple[ContextUnit, ContextPage]] = []
    pages_by_key: dict[str, ContextPage] = {}
    visits_by_key: dict[str, int] = {}
    for unit in units:
        page = normalize_context_page(unit)
        unit_pages.append((unit, page))
        pages_by_key.setdefault(page.key, page)
        visits_by_key[page.key] = visits_by_key.get(page.key, 0) + 1

    graph = build_transition_graph(units, events)
    return _ContextIndex(
        unit_pages=unit_pages,
        pages_by_key=pages_by_key,
        visits_by_key=visits_by_key,
        graph=graph,
        scores=page_rank(graph),
    )


def _matches_query(page: ContextPage, needle: str) -> bool:
    # Bridges (e.g. New Tab) are hidden from ranking; hide them from search too.
    if not page.rankable:
        return False
    if needle in page.label.casefold():
        return True
    return bool(page.url) and needle in page.url.casefold()


def _context_search_scores(index: _ContextIndex) -> dict[str, float]:
    return normalize_context_scores(
        {
            key: index.scores.get(key, 0.0)
            for key, page in index.pages_by_key.items()
            if page.rankable
        }
    )


def _context_search_result(
    *,
    key: str,
    page: ContextPage,
    score: float,
    visits: int,
    last_seen_at: str,
) -> dict[str, object]:
    return {
        "key": key,
        "kind": page.kind,
        "label": page.label,
        # retrieval_locator ("url:<normalized>") is the precise per-URL locator;
        # source_key may be coarser (e.g. a GitHub repo groups many URLs).
        "uri": (
            (page.retrieval_locator or "").removeprefix("url:")
            if page.kind == "url"
            else None
        ),
        "score": score,
        "visits": visits,
        "lastSeenAt": last_seen_at,
    }


def _screen_ocr_matches(
    repository: EventRepository,
    *,
    query: str,
    since: str | None,
    limit: int,
    context_rank_scores: dict[str, float],
    config: ServiceConfig | None = None,
    semantic_candidate_provider: SemanticSearchCandidateProvider | None = None,
) -> list[ScreenSearchResult]:
    if limit <= 0:
        return []

    semantic_provider = semantic_candidate_provider
    if semantic_provider is None and config is not None:
        semantic_provider = build_semantic_candidate_provider(
            repository.connection,
            config,
        )

    return ScreenSearchService(
        ScreenSearchRepository(repository.connection),
        ContextRankRepository(repository.connection),
        context_rank_overrides=context_rank_scores,
        semantic_candidate_provider=semantic_provider,
    ).search(query, limit=limit, since=since)


def build_semantic_candidate_provider(
    connection,
    config: ServiceConfig,
) -> SemanticSearchCandidateProvider | None:
    if not config.semantic_search_enabled:
        return None

    from .embeddings.sentence_transformers import (
        get_sentence_transformer_embedding_model,
    )
    from .search import EmbeddingSemanticSearchProvider, SqliteExactVectorIndex
    from .store.embeddings import EmbeddingRepository

    embedding_repository = EmbeddingRepository(connection)
    if (
        embedding_repository.count_current_chunk_embeddings(
            model=config.embedding_model,
            dimension=config.embedding_dimension,
        )
        == 0
    ):
        return None

    return EmbeddingSemanticSearchProvider(
        model=get_sentence_transformer_embedding_model(config),
        vector_index=SqliteExactVectorIndex(embedding_repository),
        candidate_limit=config.semantic_search_candidate_limit,
    )


def _screen_ocr_search_result(
    result: ScreenSearchResult,
    *,
    visits: int,
) -> dict[str, object]:
    return {
        "key": result.source_key,
        "kind": _screen_ocr_result_kind(result),
        "label": _screen_ocr_result_label(result),
        "uri": _screen_ocr_result_uri(result),
        "score": result.final_score,
        "visits": visits,
        "lastSeenAt": result.ended_at or result.started_at,
        "matchSource": "ocr",
        "snippet": result.preview,
    }


def _merge_search_results(
    context_results: list[dict[str, object]],
    ocr_results: list[dict[str, object]],
) -> list[dict[str, object]]:
    by_key = {str(result["key"]): result for result in context_results}

    for ocr_result in ocr_results:
        key = str(ocr_result["key"])
        context_result = by_key.get(key)
        if context_result is None:
            by_key[key] = ocr_result
            continue

        by_key[key] = {
            **context_result,
            "score": max(float(context_result["score"]), float(ocr_result["score"])),
            "lastSeenAt": max(
                str(context_result["lastSeenAt"]),
                str(ocr_result["lastSeenAt"]),
            ),
            "matchSource": "context+ocr",
            "snippet": ocr_result["snippet"],
        }

    merged = list(by_key.values())
    merged.sort(key=lambda result: (-float(result["score"]), str(result["label"])))
    return merged


def _screen_ocr_result_kind(result: ScreenSearchResult) -> str:
    if _screen_ocr_result_uri(result) is not None:
        return "url"
    if result.window_title:
        return "app_window"
    return "app"


def _screen_ocr_result_label(result: ScreenSearchResult) -> str:
    if result.app_name and result.window_title:
        return f"{result.app_name} | {result.window_title}"
    if result.window_title:
        return result.window_title
    if result.app_name:
        return result.app_name
    if result.url:
        return result.url
    return result.source_key


def _screen_ocr_result_uri(result: ScreenSearchResult) -> str | None:
    if result.url:
        return result.url
    if result.retrieval_locator and result.retrieval_locator.startswith("url:"):
        return result.retrieval_locator.removeprefix("url:")
    return None


DEFAULT_SCENE_PREVIEW_LIMIT = 12
_SCENE_THUMBNAIL_MAX_EDGE = 480
_SCENE_THUMBNAIL_QUALITY = 70

# Frame images are immutable, so cache thumbnails by path. Bounds the cost of
# re-rendering the same screenshots across repeated polls and search keystrokes.
_THUMBNAIL_CACHE: "OrderedDict[str, str]" = OrderedDict()
_THUMBNAIL_CACHE_MAX = 256


def list_scene_previews(
    repository: EventRepository,
    *,
    limit: int = DEFAULT_SCENE_PREVIEW_LIMIT,
) -> dict[str, object]:
    """Recent scenes with their first retained screenshot, for the home page.

    The schema {previews: [{key, frameId, label, appName, windowTitle, url,
    kind, capturedAt, lastSeenAt, image}]} is the renderer contract. `image`
    is a base64 JPEG data URL of a downscaled thumbnail. Scenes whose PNG is
    missing or unreadable are skipped so the row only shows openable previews.
    """
    screen_repository = ScreenRepository(repository.connection)
    previews = screen_repository.list_top_scene_previews(limit=max(limit, 0))

    items: list[dict[str, object]] = []
    for preview in previews:
        image = _thumbnail_data_url(Path(preview.image_path))
        if image is None:
            continue
        items.append(
            {
                "key": preview.session_id,
                "frameId": preview.frame_id,
                "label": _scene_preview_label(preview),
                "appName": preview.app_name,
                "windowTitle": preview.window_title,
                "url": preview.url,
                "kind": _scene_preview_kind(preview),
                "capturedAt": preview.captured_at,
                "lastSeenAt": preview.ended_at or preview.started_at,
                "image": image,
            }
        )
    return {"previews": items}


def _scene_preview_label(preview: ScenePreview) -> str:
    detail = preview.url or preview.window_title
    if preview.app_name and detail:
        return f"{preview.app_name} | {detail}"
    if preview.app_name:
        return preview.app_name
    if detail:
        return detail
    return preview.session_id


def _scene_preview_kind(preview: ScenePreview) -> str:
    if preview.url:
        return "url"
    if preview.window_title:
        return "app_window"
    return "app"


def _thumbnail_data_url(image_path: Path) -> str | None:
    key = str(image_path)
    cached = _THUMBNAIL_CACHE.get(key)
    if cached is not None:
        _THUMBNAIL_CACHE.move_to_end(key)
        return cached

    if not image_path.is_file():
        return None
    try:
        with Image.open(image_path) as image:
            thumbnail = image.convert("RGB")
            thumbnail.thumbnail(
                (_SCENE_THUMBNAIL_MAX_EDGE, _SCENE_THUMBNAIL_MAX_EDGE)
            )
            buffer = io.BytesIO()
            thumbnail.save(
                buffer, format="JPEG", quality=_SCENE_THUMBNAIL_QUALITY
            )
    except (OSError, ValueError):
        return None
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    data_url = f"data:image/jpeg;base64,{encoded}"

    _THUMBNAIL_CACHE[key] = data_url
    _THUMBNAIL_CACHE.move_to_end(key)
    if len(_THUMBNAIL_CACHE) > _THUMBNAIL_CACHE_MAX:
        _THUMBNAIL_CACHE.popitem(last=False)
    return data_url


DEFAULT_SCENE_TIMELINE_LIMIT = 30
_SCENE_TIMELINE_LOG_LIMIT = 100


def get_scene_timeline(
    repository: EventRepository,
    *,
    before: str | None = None,
    limit: int = DEFAULT_SCENE_TIMELINE_LIMIT,
) -> dict[str, object]:
    """Recent scenes (sessions) for the timeline view, most-recent first.

    Each scene carries a keyframe thumbnail (or null), its OCR-shot count, and
    the raw events ("logs") that fall in its [startedAt, endedAt] window. The
    renderer draws one tall stick per scene plus a short stick per extra log.
    `before` is a started_at cursor for infinite scroll into older scenes.
    Schema: {scenes: [{id, label, kind, appName, windowTitle, url, file,
    startedAt, endedAt, image, ocrShots, recordCount, logs:[{timestamp, type,
    app, window, url}]}]}.
    """
    screen_repository = ScreenRepository(repository.connection)
    ocr_repository = OcrChunkRepository(repository.connection)
    sessions = screen_repository.list_recent_sessions(before=before, limit=max(limit, 0))

    scenes: list[dict[str, object]] = []
    for session in sessions:
        keyframe = screen_repository.get_session_keyframe(session.id)
        image = (
            _thumbnail_data_url(Path(keyframe.image_path))
            if keyframe is not None
            else None
        )
        events = repository.list(
            since=session.started_at,
            until=session.ended_at,
            limit=_SCENE_TIMELINE_LOG_LIMIT,
        )
        logs = [
            {
                "timestamp": event.timestamp,
                "type": event.type,
                "app": event.app_name,
                "window": event.window_title,
                "url": event.url,
            }
            for event in events
        ]
        scenes.append(
            {
                "id": session.id,
                "label": _session_label(session),
                "kind": _session_kind(session),
                "appName": session.app_name,
                "windowTitle": session.window_title,
                "url": session.url,
                "file": session.url,
                "startedAt": session.started_at,
                "endedAt": session.ended_at,
                "image": image,
                "ocrShots": ocr_repository.count_for_session(session.id),
                "recordCount": max(len(logs), 1),
                "logs": logs,
            }
        )
    return {"scenes": scenes}


def _session_label(session: ScreenSession) -> str:
    detail = session.window_title or session.url
    if session.app_name and detail:
        return f"{session.app_name} | {detail}"
    if session.app_name:
        return session.app_name
    if detail:
        return detail
    return session.source_key


def _session_kind(session: ScreenSession) -> str:
    if session.url:
        return "url"
    if session.window_title:
        return "app_window"
    return "app"


def get_storage_stats(
    repository: EventRepository, *, config: ServiceConfig
) -> dict[str, object]:
    """Local storage footprint for the Stats page.

    Returns byte sizes (database incl. WAL/SHM, screenshots, logs, total) and
    row counts (sessions, frames, retained screenshots, indexed text chunks).
    """
    database_bytes = _path_group_bytes(
        [
            config.database_path,
            config.database_path.with_name(config.database_path.name + "-wal"),
            config.database_path.with_name(config.database_path.name + "-shm"),
        ]
    )
    screenshot_bytes, screenshot_count = _dir_bytes_and_count(config.screenshots_dir)
    log_bytes, _ = _dir_bytes_and_count(config.logs_dir)

    connection = repository.connection
    sessions = int(connection.execute("SELECT COUNT(*) FROM screen_sessions").fetchone()[0])
    frames = int(connection.execute("SELECT COUNT(*) FROM screen_frames").fetchone()[0])
    retained = int(
        connection.execute(
            "SELECT COUNT(*) FROM screen_frames WHERE image_retention_state = ?",
            ("retained",),
        ).fetchone()[0]
    )
    chunks = int(connection.execute("SELECT COUNT(*) FROM ocr_chunks").fetchone()[0])
    # Scenes that have a retained keyframe vs scenes that produced OCR text —
    # coverage denominators for the Stats bars (both over total scenes).
    scenes_captured = int(
        connection.execute(
            """
            SELECT COUNT(DISTINCT session_id) FROM screen_frames
            WHERE status = 'selected' AND image_retention_state = ?
            """,
            ("retained",),
        ).fetchone()[0]
    )
    scenes_with_ocr = int(
        connection.execute(
            "SELECT COUNT(DISTINCT session_id) FROM ocr_chunks"
        ).fetchone()[0]
    )

    return {
        "databaseBytes": database_bytes,
        "screenshotBytes": screenshot_bytes,
        "screenshotCount": screenshot_count,
        "logBytes": log_bytes,
        "totalBytes": database_bytes + screenshot_bytes + log_bytes,
        "sessions": sessions,
        "frames": frames,
        "retainedScreenshots": retained,
        "indexedChunks": chunks,
        "scenesCaptured": scenes_captured,
        "scenesWithOcr": scenes_with_ocr,
    }


def _path_group_bytes(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _dir_bytes_and_count(directory: Path) -> tuple[int, int]:
    total = 0
    count = 0
    if not directory.is_dir():
        return (0, 0)
    for path in directory.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
                count += 1
        except OSError:
            continue
    return (total, count)


def get_timeline(
    repository: EventRepository,
    *,
    since: str | None = None,
    limit: int = DEFAULT_EVENT_LIMIT,
) -> list[dict[str, object]]:
    """Return a chronological activity timeline as JSON-serializable dicts."""
    events = repository.list(since=since, limit=limit)
    return [
        {
            "timestamp": event.timestamp,
            "type": event.type,
            "app": event.app_name,
            "window": event.window_title,
            "url": event.url,
        }
        for event in events
    ]


def sample_event() -> NormalizedEvent:
    # Development-only event for verifying storage and output flows locally.
    return normalize_event(
        "active_app_changed",
        app={
            "name": "Sample App",
            "bundle_id": "com.example.SampleApp",
            "pid": 12345,
        },
        window={"title": "Sample Window"},
        url="https://example.com/work?q=private#section",
        source="sample",
        metadata={"sample": True},
    )


# App/window/URL transitions that form a small but meaningful graph: Cursor hubs
# between a PR and docs, with a Slack detour. Marked source="demo".
_DEMO_STEPS: tuple[tuple[str, dict[str, object], dict[str, str], str | None], ...] = (
    ("active_app_snapshot", {"name": "Cursor"}, {"title": "queries.py"}, None),
    (
        "current_asset_changed",
        {"name": "Chrome"},
        {"title": "PR #214"},
        "https://github.com/org/repo/pull/214",
    ),
    ("active_app_snapshot", {"name": "Cursor"}, {"title": "queries.py"}, None),
    (
        "current_asset_changed",
        {"name": "Chrome"},
        {"title": "sqlite3 docs"},
        "https://docs.python.org/3/library/sqlite3.html",
    ),
    ("active_app_snapshot", {"name": "Slack"}, {"title": "#daily-scrum"}, None),
    ("active_app_snapshot", {"name": "Cursor"}, {"title": "queries.py"}, None),
    (
        "current_asset_changed",
        {"name": "Chrome"},
        {"title": "PR #214"},
        "https://github.com/org/repo/pull/214",
    ),
)


def seed_demo_events(
    repository: EventRepository, *, now: datetime | None = None
) -> int:
    """Insert a demo transition sequence so the graph/rank/search/timeline views
    are populated on devices that do not collect (e.g. Windows dev setups).

    Timestamps land in the recent past (within the default query windows) and a
    couple of minutes apart, so the events form one continuous session.
    """
    base = (now or utc_now()) - timedelta(minutes=len(_DEMO_STEPS) * 2 + 5)
    for index, (event_type, app, window, url) in enumerate(_DEMO_STEPS):
        event = normalize_event(
            event_type,
            app=app,
            window=window,
            url=url,
            source="demo",
            metadata={"demo": True},
        )
        timestamp = utc_timestamp(base + timedelta(minutes=index * 2))
        repository.insert(replace(event, timestamp=timestamp))
    return len(_DEMO_STEPS)
