from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal, Protocol

from melone_service.models import NormalizedEvent
from melone_service.pipeline.activity import ACTIVITY_EVENT_TYPES
from melone_service.pipeline.context_pages import (
    ContextPage,
    ContextUnit,
    RankedContextPage,
    build_context_units,
    normalize_context_page,
)


TransitionGraph = dict[str, dict[str, float]]
_SegmentEventIndex = dict[int, tuple[NormalizedEvent, ...]]
_RankGranularity = Literal["app", "app_window", "detail"]
SESSION_GAP_SECONDS = 30 * 60
_ACTIVITY_EVENT_TYPES = frozenset(ACTIVITY_EVENT_TYPES)

ENGAGEMENT_MODEL_VERSION = "segment_engagement_v1"
ATTRIBUTION_MODEL_VERSION = "destination_engagement_v1"

ENGAGEMENT_BASE_VISIT_SCORE = 1.0
ENGAGEMENT_DURATION_SECONDS_PER_POINT = 30.0
ENGAGEMENT_DURATION_CAP_SECONDS = 600.0
ENGAGEMENT_KEY_COUNT_CAP = 200.0
ENGAGEMENT_KEY_WEIGHT = 0.05
ENGAGEMENT_CLIPBOARD_WEIGHT = 1.0
ENGAGEMENT_CLICK_WEIGHT = 0.8
ENGAGEMENT_SCROLL_WEIGHT = 0.25
ENGAGEMENT_DRAG_WEIGHT = 0.6
ENGAGEMENT_MOVE_COUNT_CAP = 500.0
ENGAGEMENT_MOVE_WEIGHT = 0.01
ENGAGEMENT_EDGE_MIN_WEIGHT = 1.0
ENGAGEMENT_EDGE_MAX_WEIGHT = 30.0


@dataclass(frozen=True)
class ContextRankWeights:
    app: float = 0.2
    app_window: float = 0.3
    detail: float = 0.5


DEFAULT_CONTEXT_RANK_WEIGHTS = ContextRankWeights()


@dataclass(frozen=True)
class _VisitSegment:
    index: int
    unit: ContextUnit
    page: ContextPage
    started_at: datetime
    ended_at: datetime


@dataclass(frozen=True)
class _EngagementComponent:
    name: str
    score: float


@dataclass(frozen=True)
class _EngagementBreakdown:
    total: float
    components: tuple[_EngagementComponent, ...]


@dataclass(frozen=True)
class _ContextEngagement:
    source_key: str
    retrieval_locator: str | None
    engagement: _EngagementBreakdown


_BASE_VISIT_ENGAGEMENT = _EngagementBreakdown(
    total=ENGAGEMENT_BASE_VISIT_SCORE,
    components=(
        _EngagementComponent(
            name="base_visit",
            score=ENGAGEMENT_BASE_VISIT_SCORE,
        ),
    ),
)


class _SegmentEngagementScorer(Protocol):
    def __call__(
        self,
        segment: _VisitSegment,
        segment_events: Sequence[NormalizedEvent],
    ) -> _EngagementComponent:
        ...


class _ContextPageProjector(Protocol):
    def __call__(self, unit: ContextUnit) -> ContextPage:
        ...


@dataclass(frozen=True)
class _DurationEngagementScorer:
    name: str = "duration"

    def __call__(
        self,
        segment: _VisitSegment,
        segment_events: Sequence[NormalizedEvent],
    ) -> _EngagementComponent:
        duration_seconds = max(
            (segment.ended_at - segment.started_at).total_seconds(),
            0.0,
        )
        score = (
            min(duration_seconds, ENGAGEMENT_DURATION_CAP_SECONDS)
            / ENGAGEMENT_DURATION_SECONDS_PER_POINT
        )
        return _EngagementComponent(name=self.name, score=score)


@dataclass(frozen=True)
class _KeyboardEngagementScorer:
    name: str = "keyboard"

    def __call__(
        self,
        segment: _VisitSegment,
        segment_events: Sequence[NormalizedEvent],
    ) -> _EngagementComponent:
        total_key_count = sum(
            _metadata_number(event, "key_count")
            for event in _activity_events(segment_events)
            if event.type == "keyboard_burst"
        )
        score = min(total_key_count, ENGAGEMENT_KEY_COUNT_CAP) * ENGAGEMENT_KEY_WEIGHT
        return _EngagementComponent(name=self.name, score=score)


@dataclass(frozen=True)
class _ClipboardEngagementScorer:
    name: str = "clipboard"

    def __call__(
        self,
        segment: _VisitSegment,
        segment_events: Sequence[NormalizedEvent],
    ) -> _EngagementComponent:
        shortcut_count = sum(
            1
            for event in _activity_events(segment_events)
            if event.type == "clipboard_shortcut"
        )
        return _EngagementComponent(
            name=self.name,
            score=shortcut_count * ENGAGEMENT_CLIPBOARD_WEIGHT,
        )


@dataclass(frozen=True)
class _MouseEngagementScorer:
    name: str = "mouse"

    def __call__(
        self,
        segment: _VisitSegment,
        segment_events: Sequence[NormalizedEvent],
    ) -> _EngagementComponent:
        mouse_events = tuple(
            event
            for event in _activity_events(segment_events)
            if event.type == "mouse_activity"
        )
        click_count = _metadata_total(mouse_events, "click_count")
        scroll_count = _metadata_total(mouse_events, "scroll_count")
        drag_count = _metadata_total(mouse_events, "drag_count")
        move_count = min(
            _metadata_total(mouse_events, "move_count"),
            ENGAGEMENT_MOVE_COUNT_CAP,
        )
        score = (
            click_count * ENGAGEMENT_CLICK_WEIGHT
            + scroll_count * ENGAGEMENT_SCROLL_WEIGHT
            + drag_count * ENGAGEMENT_DRAG_WEIGHT
            + move_count * ENGAGEMENT_MOVE_WEIGHT
        )
        return _EngagementComponent(name=self.name, score=score)


_DEFAULT_ENGAGEMENT_SCORERS: tuple[_SegmentEngagementScorer, ...] = (
    _DurationEngagementScorer(),
    _KeyboardEngagementScorer(),
    _ClipboardEngagementScorer(),
    _MouseEngagementScorer(),
)


@dataclass(frozen=True)
class _ContextPathEntry:
    source_key: str
    retrieval_locator: str | None
    engagement: _EngagementBreakdown

    @property
    def identity(self) -> tuple[str, str | None]:
        return self.source_key, self.retrieval_locator


class _EdgeAttributionStrategy(Protocol):
    def __call__(
        self,
        source: _ContextPathEntry,
        destination: _ContextPathEntry,
    ) -> float:
        ...


@dataclass(frozen=True)
class _DestinationEngagementAttribution:
    def __call__(
        self,
        source: _ContextPathEntry,
        destination: _ContextPathEntry,
    ) -> float:
        return _edge_weight(destination.engagement.total)


_DEFAULT_EDGE_ATTRIBUTION_STRATEGY: _EdgeAttributionStrategy = (
    _DestinationEngagementAttribution()
)


def rank_contexts(
    events: Sequence[NormalizedEvent],
    *,
    limit: int | None = None,
    show_hidden: bool = False,
    rank_weights: ContextRankWeights = DEFAULT_CONTEXT_RANK_WEIGHTS,
) -> list[RankedContextPage]:
    units = build_context_units(events)
    pages_by_source_key: dict[str, ContextPage] = {}
    visits_by_source_key: dict[str, int] = {}
    locators_by_source_key: dict[str, list[str]] = {}

    for unit in units:
        page = normalize_context_page(unit)
        source_key = page.source_key
        pages_by_source_key.setdefault(source_key, page)
        visits_by_source_key[source_key] = visits_by_source_key.get(source_key, 0) + 1
        if page.retrieval_locator is not None:
            locators = locators_by_source_key.setdefault(source_key, [])
            if page.retrieval_locator not in locators:
                locators.append(page.retrieval_locator)

    scores = _combined_rank_scores(
        units,
        events=events,
        rank_weights=rank_weights,
    )
    ranked_pages = [
        RankedContextPage(
            page=page,
            score=scores.get(source_key, 0.0),
            visits=visits_by_source_key[source_key],
            retrieval_locators=tuple(locators_by_source_key.get(source_key, ())),
        )
        for source_key, page in pages_by_source_key.items()
        if show_hidden or page.rankable
    ]
    ranked_pages.sort(
        key=lambda ranked_page: (
            -ranked_page.score,
            -ranked_page.visits,
            ranked_page.page.label,
        )
    )

    if limit is not None:
        return ranked_pages[: max(limit, 0)]
    return ranked_pages


def build_transition_graph(
    units: Sequence[ContextUnit],
    events: Sequence[NormalizedEvent] = (),
    *,
    page_projector: _ContextPageProjector | None = None,
) -> TransitionGraph:
    graph: TransitionGraph = {}
    path: list[_ContextPathEntry] = []
    previous_segment: _VisitSegment | None = None
    segments = _build_visit_segments(units, events, page_projector=page_projector)
    segment_engagements = _build_graph_segment_engagements(segments, events)

    for segment in segments:
        if previous_segment is not None and _has_session_gap(
            previous_segment, segment
        ):
            _add_path_edges(graph, path, _DEFAULT_EDGE_ATTRIBUTION_STRATEGY)
            path = []

        page = segment.page
        if page.boundary:
            _add_path_edges(graph, path, _DEFAULT_EDGE_ATTRIBUTION_STRATEGY)
            path = []
            previous_segment = segment
            continue

        previous_segment = segment
        if page.bridge:
            continue

        path_entry = _ContextPathEntry(
            source_key=page.source_key,
            retrieval_locator=page.retrieval_locator,
            engagement=segment_engagements[segment.index],
        )
        if not path or path[-1].identity != path_entry.identity:
            path.append(path_entry)

    _add_path_edges(graph, path, _DEFAULT_EDGE_ATTRIBUTION_STRATEGY)
    return graph


def build_context_engagements(
    units: Sequence[ContextUnit],
    *,
    events: Sequence[NormalizedEvent] = (),
    engagement_scorers: Sequence[_SegmentEngagementScorer] | None = None,
) -> list[_ContextEngagement]:
    segments = _build_visit_segments(units, events)
    segment_events = _index_events_by_segment(segments, events)
    segment_engagements = _build_segment_engagements(
        segments,
        segment_events,
        engagement_scorers,
    )

    return [
        _ContextEngagement(
            source_key=segment.page.source_key,
            retrieval_locator=segment.page.retrieval_locator,
            engagement=segment_engagements[segment.index],
        )
        for segment in segments
    ]


def _combined_rank_scores(
    units: Sequence[ContextUnit],
    *,
    events: Sequence[NormalizedEvent],
    rank_weights: ContextRankWeights,
) -> dict[str, float]:
    weights = _normalized_rank_weights(rank_weights)
    scores: dict[str, float] = {}

    for granularity, weight in (
        ("app", weights.app),
        ("app_window", weights.app_window),
        ("detail", weights.detail),
    ):
        if weight <= 0:
            continue

        granularity_scores = _rank_scores_for_granularity(
            units,
            events=events,
            granularity=granularity,
        )
        for source_key, score in granularity_scores.items():
            scores[source_key] = scores.get(source_key, 0.0) + (score * weight)

    return scores


def _rank_scores_for_granularity(
    units: Sequence[ContextUnit],
    *,
    events: Sequence[NormalizedEvent],
    granularity: _RankGranularity,
) -> dict[str, float]:
    projector = _rank_page_projector(granularity)
    graph = build_transition_graph(units, events=events, page_projector=projector)
    projected_scores = page_rank(graph)
    allocation_visits: dict[str, dict[str, int]] = {}

    # Coarse app/window scores are shared by many detailed pages, so distribute
    # each projected node score instead of copying it onto every child page.
    for unit in units:
        source_page = normalize_context_page(unit)
        projected_page = projector(unit)
        if projected_page.boundary or projected_page.bridge:
            continue

        visits = allocation_visits.setdefault(projected_page.source_key, {})
        visits[source_page.source_key] = visits.get(source_page.source_key, 0) + 1

    scores: dict[str, float] = {}
    for projected_key, projected_score in projected_scores.items():
        visits = allocation_visits.get(projected_key, {})
        total_visits = sum(visits.values())
        if total_visits <= 0:
            continue

        for source_key, visit_count in visits.items():
            scores[source_key] = (
                scores.get(source_key, 0.0)
                + projected_score * visit_count / total_visits
            )

    return scores


def _normalized_rank_weights(rank_weights: ContextRankWeights) -> ContextRankWeights:
    weights = (rank_weights.app, rank_weights.app_window, rank_weights.detail)
    if any(not math.isfinite(weight) or weight < 0 for weight in weights):
        raise ValueError("rank weights must be finite non-negative numbers")

    total = sum(weights)
    if total <= 0:
        raise ValueError("at least one rank weight must be positive")

    return ContextRankWeights(
        app=rank_weights.app / total,
        app_window=rank_weights.app_window / total,
        detail=rank_weights.detail / total,
    )


def _rank_page_projector(granularity: _RankGranularity) -> _ContextPageProjector:
    def project(unit: ContextUnit) -> ContextPage:
        return _project_rank_page(unit, granularity)

    return project


def _project_rank_page(
    unit: ContextUnit,
    granularity: _RankGranularity,
) -> ContextPage:
    detail_page = normalize_context_page(unit)
    if granularity == "detail":
        projected_page = replace(
            detail_page,
            source_key=_detail_rank_source_key(unit, detail_page),
        )
    elif granularity == "app_window":
        projected_page = normalize_context_page(replace(unit, url=None))
    else:
        projected_page = normalize_context_page(
            replace(unit, window_title=None, url=None)
        )

    return replace(
        projected_page,
        rankable=detail_page.rankable,
        bridge=detail_page.bridge,
        boundary=detail_page.boundary,
    )


def _detail_rank_source_key(unit: ContextUnit, detail_page: ContextPage) -> str:
    app_page = normalize_context_page(replace(unit, window_title=None, url=None))
    window_page = normalize_context_page(replace(unit, url=None))
    locator = detail_page.retrieval_locator or detail_page.source_key
    return f"detail:{app_page.source_key}:{window_page.source_key}:{locator}"


def _build_graph_segment_engagements(
    segments: Sequence[_VisitSegment],
    events: Sequence[NormalizedEvent],
) -> dict[int, _EngagementBreakdown]:
    if not events:
        return {segment.index: _BASE_VISIT_ENGAGEMENT for segment in segments}

    segment_events = _index_events_by_segment(segments, events)
    return _build_segment_engagements(segments, segment_events)


def _build_visit_segments(
    units: Sequence[ContextUnit],
    events: Sequence[NormalizedEvent] = (),
    *,
    page_projector: _ContextPageProjector | None = None,
) -> list[_VisitSegment]:
    observation_end = _observation_end(events)
    segments: list[_VisitSegment] = []
    project_page = (
        normalize_context_page if page_projector is None else page_projector
    )

    for index, unit in enumerate(units):
        started_at = _parse_timestamp(unit.started_at)
        segments.append(
            _VisitSegment(
                index=index,
                unit=unit,
                page=project_page(unit),
                started_at=started_at,
                ended_at=_unit_effective_end(
                    unit,
                    observation_end,
                    started_at=started_at,
                ),
            )
        )

    return segments


def _observation_end(events: Sequence[NormalizedEvent]) -> datetime | None:
    return max((_parse_timestamp(event.timestamp) for event in events), default=None)


def _unit_effective_end(
    unit: ContextUnit,
    observation_end: datetime | None,
    *,
    started_at: datetime | None = None,
) -> datetime:
    started_at = started_at or _parse_timestamp(unit.started_at)
    ended_at = (
        _parse_timestamp(unit.ended_at)
        if unit.ended_at is not None
        else observation_end
    )

    if ended_at is None or ended_at < started_at:
        return started_at
    return ended_at


def _index_events_by_segment(
    segments: Sequence[_VisitSegment],
    events: Sequence[NormalizedEvent],
) -> _SegmentEventIndex:
    segment_events: dict[int, list[NormalizedEvent]] = {
        segment.index: [] for segment in segments
    }
    if not segments:
        return {}

    ordered_events = sorted(
        events,
        key=lambda event: (_parse_timestamp(event.timestamp), event.id),
    )

    # Segments come from `_build_visit_segments` in non-decreasing started_at
    # order with non-overlapping [started_at, ended_at) intervals. The segment
    # containing an event is therefore the last one whose start is <= the event
    # time; bisect that boundary instead of scanning every segment per event.
    # (Was O(events x segments); the per-event scan dominated context-rank cost.)
    starts = [segment.started_at for segment in segments]
    for event in ordered_events:
        event_timestamp = _parse_timestamp(event.timestamp)
        position = bisect_right(starts, event_timestamp) - 1
        if position >= 0 and event_timestamp < segments[position].ended_at:
            segment_events[segments[position].index].append(event)

    return {
        segment_index: tuple(event_group)
        for segment_index, event_group in segment_events.items()
    }


def _build_segment_engagements(
    segments: Sequence[_VisitSegment],
    segment_events: _SegmentEventIndex,
    engagement_scorers: Sequence[_SegmentEngagementScorer] | None = None,
) -> dict[int, _EngagementBreakdown]:
    engagements: dict[int, _EngagementBreakdown] = {}
    scorers = (
        _DEFAULT_ENGAGEMENT_SCORERS
        if engagement_scorers is None
        else engagement_scorers
    )

    for segment in segments:
        events = segment_events.get(segment.index, ())
        components = tuple(scorer(segment, events) for scorer in scorers)
        engagements[segment.index] = _EngagementBreakdown(
            total=sum(component.score for component in components),
            components=components,
        )

    return engagements


def _activity_events(
    segment_events: Sequence[NormalizedEvent],
) -> tuple[NormalizedEvent, ...]:
    return tuple(
        event for event in segment_events if event.type in _ACTIVITY_EVENT_TYPES
    )


def _metadata_total(events: Sequence[NormalizedEvent], key: str) -> float:
    return sum(_metadata_number(event, key) for event in events)


def _metadata_number(event: NormalizedEvent, key: str) -> float:
    value = event.metadata.get(key, 0)
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        number = float(value)
    else:
        try:
            number = float(str(value))
        except (TypeError, ValueError):
            return 0.0

    if not math.isfinite(number) or number < 0:
        return 0.0
    return number


def page_rank(
    graph: TransitionGraph,
    *,
    alpha: float = 0.85,
    iterations: int = 100,
    tolerance: float = 1e-8,
) -> dict[str, float]:
    nodes = _graph_nodes(graph)
    if not nodes:
        return {}

    node_count = len(nodes)
    ranks = dict.fromkeys(nodes, 1.0 / node_count)
    outgoing_totals = {
        node: sum(weight for weight in graph.get(node, {}).values() if weight > 0)
        for node in nodes
    }

    for _ in range(iterations):
        next_ranks = dict.fromkeys(nodes, (1.0 - alpha) / node_count)
        dangling_score = sum(
            ranks[node] for node, total in outgoing_totals.items() if total <= 0
        )
        dangling_share = alpha * dangling_score / node_count

        for node in nodes:
            next_ranks[node] += dangling_share

        for from_node, to_edges in graph.items():
            total = outgoing_totals.get(from_node, 0.0)
            if total <= 0:
                continue

            for to_node, weight in to_edges.items():
                if weight > 0:
                    next_ranks[to_node] += alpha * ranks[from_node] * weight / total

        delta = sum(abs(next_ranks[node] - ranks[node]) for node in nodes)
        ranks = next_ranks
        if delta <= tolerance:
            break

    return ranks


def _graph_nodes(graph: TransitionGraph) -> list[str]:
    nodes = set(graph)
    has_edge = False

    for to_edges in graph.values():
        for to_node, weight in to_edges.items():
            if weight > 0:
                has_edge = True
                nodes.add(to_node)

    if not has_edge:
        return []

    return sorted(nodes)


def _has_session_gap(
    previous_segment: _VisitSegment,
    segment: _VisitSegment,
) -> bool:
    # Units come from sparse events, so consecutive start times are the
    # observable signal for a break in activity.
    gap_seconds = (segment.started_at - previous_segment.started_at).total_seconds()
    return gap_seconds > SESSION_GAP_SECONDS


def _add_path_edges(
    graph: TransitionGraph,
    path: Sequence[_ContextPathEntry],
    attribution_strategy: _EdgeAttributionStrategy,
) -> None:
    for source, destination in zip(path, path[1:]):
        weight = attribution_strategy(source, destination)
        edges = graph.setdefault(source.source_key, {})
        edges[destination.source_key] = (
            edges.get(destination.source_key, 0.0) + weight
        )


def _edge_weight(score: float) -> float:
    if not math.isfinite(score):
        return ENGAGEMENT_EDGE_MIN_WEIGHT
    return min(
        max(score, ENGAGEMENT_EDGE_MIN_WEIGHT),
        ENGAGEMENT_EDGE_MAX_WEIGHT,
    )


def _parse_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)
