from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from melone_service.pipeline import context_graph
from melone_service.pipeline.context_graph import (
    SESSION_GAP_SECONDS,
    build_context_engagements,
    build_transition_graph,
    page_rank,
    rank_contexts,
)
from melone_service.pipeline.normalizer import normalize_event
from melone_service.pipeline.context_pages import ContextUnit
from melone_service.models import utc_timestamp


NOW = datetime(2026, 6, 9, 6, 0, 0, tzinfo=timezone.utc)
APP_A_KEY = "app_window:test app:A"
APP_B_KEY = "app_window:test app:B"
APP_C_KEY = "app_window:test app:C"


def test_build_transition_graph_compresses_consecutive_duplicates():
    units = [
        _unit("A", seconds=0),
        _unit("A", seconds=1),
        _unit("A", seconds=2),
        _unit("B", seconds=3),
    ]

    graph = build_transition_graph(units)

    assert graph == {APP_A_KEY: {APP_B_KEY: 1.0}}


def test_build_transition_graph_connects_across_bridge_pages():
    units = [
        _unit("A", seconds=0),
        _unit("New Tab", app_name="Google Chrome", seconds=1),
        _unit("B", seconds=2),
    ]

    graph = build_transition_graph(units)

    assert graph == {APP_A_KEY: {APP_B_KEY: 1.0}}


def test_build_transition_graph_splits_at_boundary_pages(monkeypatch):
    original_normalize_context_page = context_graph.normalize_context_page

    def normalize_with_boundary(unit):
        page = original_normalize_context_page(unit)
        if unit.window_title == "Boundary":
            return replace(page, boundary=True)
        return page

    monkeypatch.setattr(context_graph, "normalize_context_page", normalize_with_boundary)
    units = [
        _unit("A", seconds=0),
        _unit("Boundary", seconds=1),
        _unit("B", seconds=2),
    ]

    graph = build_transition_graph(units)

    assert graph == {}


def test_build_transition_graph_splits_when_session_gap_exceeds_limit():
    units = [
        _unit("A", seconds=0),
        _unit("B", seconds=SESSION_GAP_SECONDS + 1),
    ]

    graph = build_transition_graph(units)

    assert graph == {}


def test_build_visit_segments_uses_observation_end_for_open_last_unit():
    unit = _unit("A", seconds=0)
    events = [
        _event("Test App", "A", seconds=0),
        _activity_event("keyboard_burst", seconds=45),
    ]

    segments = context_graph._build_visit_segments([unit], events)

    assert len(segments) == 1
    assert segments[0].started_at == NOW
    assert segments[0].ended_at == NOW + timedelta(seconds=45)
    assert segments[0].page.source_key == APP_A_KEY


def test_build_visit_segments_clamps_missing_or_early_observation_end_to_start():
    unit = _unit("A", seconds=10)
    early_event = _activity_event("keyboard_burst", seconds=5)

    missing_end_segments = context_graph._build_visit_segments([unit])
    early_end_segments = context_graph._build_visit_segments([unit], [early_event])

    assert missing_end_segments[0].ended_at == missing_end_segments[0].started_at
    assert early_end_segments[0].ended_at == early_end_segments[0].started_at


def test_index_events_by_segment_assigns_events_in_time_order():
    units = [
        replace(
            _unit("A", seconds=0),
            ended_at=utc_timestamp(NOW + timedelta(seconds=10)),
        ),
        replace(
            _unit("B", seconds=10),
            ended_at=utc_timestamp(NOW + timedelta(seconds=20)),
        ),
    ]
    events = [
        _activity_event("keyboard_burst", seconds=12),
        _activity_event("keyboard_burst", seconds=5),
        _event("Test App", "A", seconds=2),
        _event("Test App", "B", seconds=11),
    ]

    segments = context_graph._build_visit_segments(units, events)
    index = context_graph._index_events_by_segment(segments, events)

    assert [event.id for event in index[0]] == ["evt_2", "evt_activity_5"]
    assert [event.id for event in index[1]] == ["evt_11", "evt_activity_12"]


def test_index_events_by_segment_sorts_same_timestamp_by_event_id():
    unit = replace(
        _unit("A", seconds=0),
        ended_at=utc_timestamp(NOW + timedelta(seconds=10)),
    )
    context_event = _event("Test App", "A", seconds=5)
    activity_event = _activity_event("keyboard_burst", seconds=5)

    segments = context_graph._build_visit_segments(
        [unit],
        [activity_event, context_event],
    )
    index = context_graph._index_events_by_segment(
        segments,
        [activity_event, context_event],
    )

    assert [event.id for event in index[0]] == ["evt_5", "evt_activity_5"]


def test_index_events_by_segment_matches_linear_reference_scan():
    # The bisect implementation must produce identical assignments to the
    # original O(events x segments) linear scan over many random events.
    import random

    rng = random.Random(20260609)
    units = [
        replace(
            _unit(f"W{i}", seconds=i * 10),
            ended_at=utc_timestamp(NOW + timedelta(seconds=(i + 1) * 10)),
        )
        for i in range(50)
    ]
    seconds = rng.sample(range(0, 500), 400)  # unique → distinct event ids
    events = [_activity_event("keyboard_burst", seconds=s) for s in seconds]
    segments = context_graph._build_visit_segments(units, events)

    def reference(segs, evs):
        groups = {segment.index: [] for segment in segs}
        ordered = sorted(
            evs,
            key=lambda event: (
                context_graph._parse_timestamp(event.timestamp),
                event.id,
            ),
        )
        for event in ordered:
            timestamp = context_graph._parse_timestamp(event.timestamp)
            for segment in segs:
                if segment.started_at <= timestamp < segment.ended_at:
                    groups[segment.index].append(event)
                    break
        return {index: tuple(group) for index, group in groups.items()}

    new_index = context_graph._index_events_by_segment(segments, events)
    expected = reference(segments, events)

    assert {k: [e.id for e in v] for k, v in new_index.items()} == {
        k: [e.id for e in v] for k, v in expected.items()
    }


def test_index_events_by_segment_assigns_boundary_timestamp_to_next_segment():
    units = [
        replace(
            _unit("A", seconds=0),
            ended_at=utc_timestamp(NOW + timedelta(seconds=10)),
        ),
        replace(
            _unit("B", seconds=10),
            ended_at=utc_timestamp(NOW + timedelta(seconds=20)),
        ),
    ]
    boundary_event = _activity_event("keyboard_burst", seconds=10)

    segments = context_graph._build_visit_segments(units, [boundary_event])
    index = context_graph._index_events_by_segment(segments, [boundary_event])

    assert index[0] == ()
    assert [event.id for event in index[1]] == ["evt_activity_10"]


def test_index_events_by_segment_uses_activity_to_extend_open_last_segment():
    unit = _unit("A", seconds=0)
    segment_event = _activity_event("keyboard_burst", seconds=30)
    observation_end_event = _activity_event("mouse_activity", seconds=45)

    segments = context_graph._build_visit_segments(
        [unit],
        [segment_event, observation_end_event],
    )
    index = context_graph._index_events_by_segment(
        segments,
        [segment_event, observation_end_event],
    )

    assert segments[0].ended_at == NOW + timedelta(seconds=45)
    assert [event.id for event in index[0]] == ["evt_activity_30"]


def test_build_segment_engagements_scores_duration_with_cap():
    units = [
        replace(_unit("A", seconds=0), ended_at=utc_timestamp(NOW)),
        replace(
            _unit("B", seconds=60),
            ended_at=utc_timestamp(NOW + timedelta(seconds=90)),
        ),
        replace(
            _unit("C", seconds=120),
            ended_at=utc_timestamp(NOW + timedelta(seconds=840)),
        ),
    ]

    segments = context_graph._build_visit_segments(units)
    engagements = context_graph._build_segment_engagements(segments, {})

    assert engagements[0].total == pytest.approx(0.0)
    assert engagements[1].total == pytest.approx(1.0)
    assert engagements[2].total == pytest.approx(20.0)


def test_duration_engagement_component_name_is_stable():
    unit = replace(
        _unit("A", seconds=0),
        ended_at=utc_timestamp(NOW + timedelta(seconds=30)),
    )

    segments = context_graph._build_visit_segments([unit])
    engagements = context_graph._build_segment_engagements(segments, {})

    assert [component.name for component in engagements[0].components] == [
        "duration",
        "keyboard",
        "clipboard",
        "mouse",
    ]
    assert engagements[0].components[0].score == pytest.approx(1.0)


def test_build_segment_engagements_preserves_scorer_registry_order():
    unit = replace(
        _unit("A", seconds=0),
        ended_at=utc_timestamp(NOW + timedelta(seconds=10)),
    )
    event = _activity_event("keyboard_burst", seconds=5)

    def first_scorer(segment, segment_events):
        assert [event.id for event in segment_events] == ["evt_activity_5"]
        return context_graph._EngagementComponent(name="first", score=2.0)

    def second_scorer(segment, segment_events):
        return context_graph._EngagementComponent(name="second", score=3.5)

    segments = context_graph._build_visit_segments([unit], [event])
    index = context_graph._index_events_by_segment(segments, [event])
    engagements = context_graph._build_segment_engagements(
        segments,
        index,
        (first_scorer, second_scorer),
    )

    assert [component.name for component in engagements[0].components] == [
        "first",
        "second",
    ]
    assert engagements[0].total == pytest.approx(5.5)


def test_keyboard_engagement_scores_key_count_with_cap():
    unit = replace(
        _unit("A", seconds=0),
        ended_at=utc_timestamp(NOW + timedelta(seconds=10)),
    )
    event = _activity_event(
        "keyboard_burst",
        seconds=5,
        metadata={"key_count": 250, "raw_text": "not read by scorer"},
    )

    segments = context_graph._build_visit_segments([unit], [event])
    index = context_graph._index_events_by_segment(segments, [event])
    engagements = context_graph._build_segment_engagements(segments, index)
    components = _engagement_components_by_name(engagements[0])

    assert components["keyboard"].score == pytest.approx(10.0)


def test_clipboard_engagement_scores_shortcut_count():
    unit = replace(
        _unit("A", seconds=0),
        ended_at=utc_timestamp(NOW + timedelta(seconds=10)),
    )
    events = [
        _activity_event("clipboard_shortcut", seconds=2),
        _activity_event("clipboard_shortcut", seconds=4),
        _activity_event("clipboard_shortcut", seconds=6),
    ]

    segments = context_graph._build_visit_segments([unit], events)
    index = context_graph._index_events_by_segment(segments, events)
    engagements = context_graph._build_segment_engagements(segments, index)
    components = _engagement_components_by_name(engagements[0])

    assert components["clipboard"].score == pytest.approx(3.0)


def test_mouse_engagement_scores_activity_counts_with_move_cap():
    unit = replace(
        _unit("A", seconds=0),
        ended_at=utc_timestamp(NOW + timedelta(seconds=10)),
    )
    event = _activity_event(
        "mouse_activity",
        seconds=5,
        metadata={
            "click_count": 2,
            "scroll_count": 4,
            "drag_count": 3,
            "move_count": 600,
        },
    )

    segments = context_graph._build_visit_segments([unit], [event])
    index = context_graph._index_events_by_segment(segments, [event])
    engagements = context_graph._build_segment_engagements(segments, index)
    components = _engagement_components_by_name(engagements[0])

    assert components["mouse"].score == pytest.approx(9.4)


def test_engagement_scorers_treat_invalid_metadata_as_zero():
    unit = replace(
        _unit("A", seconds=0),
        ended_at=utc_timestamp(NOW + timedelta(seconds=10)),
    )
    events = [
        _activity_event(
            "keyboard_burst",
            seconds=2,
            metadata={"key_count": "not-a-number"},
        ),
        _activity_event(
            "mouse_activity",
            seconds=4,
            metadata={
                "click_count": None,
                "scroll_count": "fast",
                "drag_count": [],
                "move_count": -10,
            },
        ),
    ]

    segments = context_graph._build_visit_segments([unit], events)
    index = context_graph._index_events_by_segment(segments, events)
    engagements = context_graph._build_segment_engagements(segments, index)
    components = _engagement_components_by_name(engagements[0])

    assert components["keyboard"].score == pytest.approx(0.0)
    assert components["mouse"].score == pytest.approx(0.0)


def test_build_context_engagements_exposes_segment_breakdown():
    unit = _closed_unit("A", start_seconds=0, end_seconds=30)
    events = [
        _activity_event(
            "keyboard_burst",
            seconds=5,
            metadata={"key_count": 20},
        ),
        _activity_event(
            "mouse_activity",
            seconds=10,
            metadata={
                "click_count": 1,
                "scroll_count": 1,
                "move_count": 10,
            },
        ),
    ]

    context_engagements = build_context_engagements([unit], events=events)

    assert len(context_engagements) == 1
    assert context_engagements[0].source_key == APP_A_KEY
    assert context_engagements[0].retrieval_locator == APP_A_KEY
    components = _engagement_components_by_name(context_engagements[0].engagement)
    assert components["duration"].score == pytest.approx(1.0)
    assert components["keyboard"].score == pytest.approx(1.0)
    assert components["mouse"].score == pytest.approx(1.15)


def test_build_context_engagements_total_matches_component_scores():
    unit = _closed_unit("A", start_seconds=0, end_seconds=60)
    events = [
        _activity_event(
            "keyboard_burst",
            seconds=5,
            metadata={"key_count": 10},
        ),
        _activity_event(
            "mouse_activity",
            seconds=10,
            metadata={"click_count": 1},
        ),
    ]

    context_engagement = build_context_engagements([unit], events=events)[0]

    component_total = sum(
        component.score for component in context_engagement.engagement.components
    )
    assert context_engagement.engagement.total == pytest.approx(component_total)


def test_destination_engagement_attribution_uses_destination_score():
    strategy = context_graph._DestinationEngagementAttribution()

    weight = strategy(
        _path_entry(APP_A_KEY, engagement_score=12.0),
        _path_entry(APP_B_KEY, engagement_score=7.5),
    )

    assert weight == pytest.approx(7.5)


def test_destination_engagement_attribution_ignores_source_score():
    strategy = context_graph._DestinationEngagementAttribution()

    weight = strategy(
        _path_entry(APP_A_KEY, engagement_score=29.0),
        _path_entry(APP_B_KEY, engagement_score=2.0),
    )

    assert weight == pytest.approx(2.0)


def test_edge_weight_clamps_minimum_and_maximum():
    assert context_graph._edge_weight(0.0) == pytest.approx(1.0)
    assert context_graph._edge_weight(0.5) == pytest.approx(1.0)
    assert context_graph._edge_weight(30.0) == pytest.approx(30.0)
    assert context_graph._edge_weight(35.0) == pytest.approx(30.0)


def test_add_path_edges_uses_attribution_strategy_weight():
    graph = {}
    path = [
        _path_entry(APP_A_KEY, engagement_score=25.0),
        _path_entry(APP_B_KEY, engagement_score=4.0),
    ]

    context_graph._add_path_edges(
        graph,
        path,
        context_graph._DestinationEngagementAttribution(),
    )

    assert graph == {APP_A_KEY: {APP_B_KEY: 4.0}}


def test_build_transition_graph_accumulates_repeated_transition_weight():
    units = [
        _unit("A", seconds=0),
        _unit("B", seconds=1),
        _unit("A", seconds=2),
        _unit("B", seconds=3),
    ]

    graph = build_transition_graph(units)

    assert graph == {
        APP_A_KEY: {APP_B_KEY: 2.0},
        APP_B_KEY: {APP_A_KEY: 1.0},
    }


def test_build_transition_graph_keeps_same_source_different_locator_transition():
    units = [
        _url_unit("https://github.com/cloneisyou/melone/pull/1", seconds=0),
        _url_unit("https://github.com/cloneisyou/melone/issues/3", seconds=1),
    ]

    graph = build_transition_graph(units)

    assert graph == {
        "github:repo:cloneisyou/melone": {
            "github:repo:cloneisyou/melone": 1.0
        }
    }


def test_build_transition_graph_weights_transition_by_destination_duration():
    units = [
        _closed_unit("A", start_seconds=0, end_seconds=10),
        _closed_unit("B", start_seconds=10, end_seconds=70),
    ]

    graph = build_transition_graph(
        units,
        events=[
            _event("Test App", "A", seconds=0),
            _event("Test App", "B", seconds=10),
        ],
    )

    assert graph[APP_A_KEY][APP_B_KEY] == pytest.approx(2.0)


def test_build_transition_graph_weights_transition_by_destination_keyboard():
    units = [
        _closed_unit("A", start_seconds=0, end_seconds=10),
        _closed_unit("B", start_seconds=10, end_seconds=20),
    ]

    graph = build_transition_graph(
        units,
        events=[
            _event("Test App", "A", seconds=0),
            _event("Test App", "B", seconds=10),
            _activity_event(
                "keyboard_burst",
                seconds=15,
                metadata={"key_count": 20},
            ),
        ],
    )

    assert graph[APP_A_KEY][APP_B_KEY] == pytest.approx((10 / 30) + 1.0)


def test_build_transition_graph_weights_transition_by_destination_mouse():
    units = [
        _closed_unit("A", start_seconds=0, end_seconds=10),
        _closed_unit("B", start_seconds=10, end_seconds=20),
    ]

    graph = build_transition_graph(
        units,
        events=[
            _event("Test App", "A", seconds=0),
            _event("Test App", "B", seconds=10),
            _activity_event(
                "mouse_activity",
                seconds=15,
                metadata={
                    "click_count": 2,
                    "scroll_count": 4,
                    "drag_count": 1,
                    "move_count": 20,
                },
            ),
        ],
    )

    assert graph[APP_A_KEY][APP_B_KEY] == pytest.approx((10 / 30) + 3.4)


def test_build_transition_graph_does_not_use_source_activity_for_edge_weight():
    units = [
        _closed_unit("A", start_seconds=0, end_seconds=10),
        _closed_unit("B", start_seconds=10, end_seconds=20),
    ]

    graph = build_transition_graph(
        units,
        events=[
            _event("Test App", "A", seconds=0),
            _activity_event(
                "keyboard_burst",
                seconds=5,
                metadata={"key_count": 200},
            ),
            _event("Test App", "B", seconds=10),
        ],
    )

    assert graph[APP_A_KEY][APP_B_KEY] == pytest.approx(1.0)


def test_build_transition_graph_without_events_keeps_unweighted_transitions():
    units = [
        _closed_unit("A", start_seconds=0, end_seconds=10),
        _closed_unit("B", start_seconds=10, end_seconds=70),
    ]

    graph = build_transition_graph(units)

    assert graph == {APP_A_KEY: {APP_B_KEY: 1.0}}


def test_build_transition_graph_bridge_keeps_destination_engagement_weight():
    units = [
        _closed_unit("A", start_seconds=0, end_seconds=10),
        _closed_unit(
            "New Tab",
            app_name="Google Chrome",
            start_seconds=10,
            end_seconds=20,
        ),
        _closed_unit("B", start_seconds=20, end_seconds=80),
    ]

    graph = build_transition_graph(
        units,
        events=[
            _event("Test App", "A", seconds=0),
            _event("Google Chrome", "New Tab", seconds=10),
            _event("Test App", "B", seconds=20),
            _activity_event(
                "keyboard_burst",
                seconds=30,
                metadata={"key_count": 40},
            ),
        ],
    )

    assert graph == {APP_A_KEY: {APP_B_KEY: 4.0}}


def test_build_transition_graph_boundary_splits_weighted_path(monkeypatch):
    original_normalize_context_page = context_graph.normalize_context_page

    def normalize_with_boundary(unit):
        page = original_normalize_context_page(unit)
        if unit.window_title == "Boundary":
            return replace(page, boundary=True)
        return page

    monkeypatch.setattr(context_graph, "normalize_context_page", normalize_with_boundary)
    units = [
        _closed_unit("A", start_seconds=0, end_seconds=10),
        _closed_unit("Boundary", start_seconds=10, end_seconds=20),
        _closed_unit("B", start_seconds=20, end_seconds=80),
    ]

    graph = build_transition_graph(
        units,
        events=[
            _event("Test App", "A", seconds=0),
            _event("Test App", "Boundary", seconds=10),
            _event("Test App", "B", seconds=20),
            _activity_event(
                "keyboard_burst",
                seconds=30,
                metadata={"key_count": 200},
            ),
        ],
    )

    assert graph == {}


def test_build_transition_graph_session_gap_splits_weighted_path():
    units = [
        _closed_unit("A", start_seconds=0, end_seconds=10),
        _closed_unit(
            "B",
            start_seconds=SESSION_GAP_SECONDS + 1,
            end_seconds=SESSION_GAP_SECONDS + 61,
        ),
    ]

    graph = build_transition_graph(
        units,
        events=[
            _event("Test App", "A", seconds=0),
            _event("Test App", "B", seconds=SESSION_GAP_SECONDS + 1),
            _activity_event(
                "keyboard_burst",
                seconds=SESSION_GAP_SECONDS + 30,
                metadata={"key_count": 200},
            ),
        ],
    )

    assert graph == {}


def test_build_transition_graph_same_source_different_locator_self_edge_is_weighted():
    units = [
        replace(
            _url_unit("https://github.com/cloneisyou/melone/pull/1", seconds=0),
            ended_at=utc_timestamp(NOW + timedelta(seconds=10)),
        ),
        replace(
            _url_unit("https://github.com/cloneisyou/melone/issues/3", seconds=10),
            ended_at=utc_timestamp(NOW + timedelta(seconds=70)),
        ),
    ]

    graph = build_transition_graph(
        units,
        events=[
            _url_event(
                "Pull request - melone",
                "https://github.com/cloneisyou/melone/pull/1",
                seconds=0,
            ),
            _url_event(
                "Issue - melone",
                "https://github.com/cloneisyou/melone/issues/3",
                seconds=10,
            ),
            _activity_event(
                "keyboard_burst",
                seconds=30,
                metadata={"key_count": 20},
            ),
        ],
    )

    assert graph == {
        "github:repo:cloneisyou/melone": {
            "github:repo:cloneisyou/melone": 3.0
        }
    }


def test_build_transition_graph_weighted_edges_change_page_rank_distribution():
    units = [
        _closed_unit("A", start_seconds=0, end_seconds=10),
        _closed_unit("B", start_seconds=10, end_seconds=20),
        _closed_unit("A", start_seconds=20, end_seconds=30),
        _closed_unit("C", start_seconds=30, end_seconds=60),
    ]

    graph = build_transition_graph(
        units,
        events=[
            _event("Test App", "A", seconds=0),
            _event("Test App", "B", seconds=10),
            _event("Test App", "A", seconds=20),
            _event("Test App", "C", seconds=30),
            _activity_event(
                "keyboard_burst",
                seconds=40,
                metadata={"key_count": 200},
            ),
        ],
    )
    ranking = page_rank(graph)

    assert graph[APP_A_KEY][APP_C_KEY] > graph[APP_A_KEY][APP_B_KEY]
    assert ranking[APP_C_KEY] > ranking[APP_B_KEY]


def test_page_rank_returns_empty_ranking_for_empty_graph():
    assert page_rank({}) == {}
    assert page_rank({"A": {}}) == {}


def test_page_rank_scores_simple_chain_deterministically():
    ranking = page_rank({"A": {"B": 1.0}, "B": {"C": 1.0}})

    assert ranking == pytest.approx(
        {
            "A": 0.18441678,
            "B": 0.34117105,
            "C": 0.47441217,
        }
    )


def test_page_rank_preserves_score_sum_with_dangling_node():
    ranking = page_rank({"A": {"B": 1.0}})

    assert set(ranking) == {"A", "B"}
    assert sum(ranking.values()) == pytest.approx(1.0)
    assert ranking["B"] > ranking["A"]


def test_page_rank_reflects_weighted_edges():
    ranking = page_rank(
        {
            "A": {"B": 9.0, "C": 1.0},
            "B": {"A": 1.0},
            "C": {"A": 1.0},
        }
    )

    assert sum(ranking.values()) == pytest.approx(1.0)
    assert ranking["B"] > ranking["C"]


def test_rank_contexts_creates_ranked_contexts_from_events():
    first_event = _event("Cursor", "context_graph.py - melone", seconds=0)
    second_event = _event("Slack", "dev - Clone - Slack", seconds=1)

    ranked_pages = rank_contexts([first_event, second_event])

    assert [ranked_page.page.label for ranked_page in ranked_pages] == [
        "Slack | dev - Clone - Slack",
        "Cursor | context_graph.py - melone",
    ]
    assert all(ranked_page.score > 0 for ranked_page in ranked_pages)
    assert [ranked_page.visits for ranked_page in ranked_pages] == [1, 1]


def test_rank_contexts_excludes_hidden_pages_by_default():
    events = [
        _event("Cursor", "A", seconds=0),
        _event("Google Chrome", "New Tab", seconds=1),
        _event("Cursor", "B", seconds=2),
    ]

    ranked_pages = rank_contexts(events)

    assert [ranked_page.page.label for ranked_page in ranked_pages] == [
        "Cursor | B",
        "Cursor | A",
    ]
    assert all(ranked_page.page.rankable for ranked_page in ranked_pages)


def test_rank_contexts_includes_hidden_pages_when_requested():
    events = [
        _event("Cursor", "A", seconds=0),
        _event("Google Chrome", "New Tab", seconds=1),
        _event("Cursor", "B", seconds=2),
    ]

    ranked_pages = rank_contexts(events, show_hidden=True)

    hidden_pages = [
        ranked_page
        for ranked_page in ranked_pages
        if ranked_page.page.source_key == "app:google chrome"
    ]
    assert len(hidden_pages) == 1
    assert hidden_pages[0].page.rankable is False
    assert hidden_pages[0].page.bridge is True
    assert hidden_pages[0].visits == 1


def test_rank_contexts_applies_limit():
    events = [
        _event("Cursor", "A", seconds=0),
        _event("Cursor", "B", seconds=1),
        _event("Cursor", "C", seconds=2),
    ]

    ranked_pages = rank_contexts(events, limit=2)

    assert len(ranked_pages) == 2


def test_rank_contexts_counts_page_visits_from_units():
    events = [
        _event("Cursor", "A", seconds=0),
        _event("Cursor", "B", seconds=1),
        _event("Cursor", "A", seconds=2),
    ]

    ranked_pages = rank_contexts(events)
    visits_by_key = {
        ranked_page.page.source_key: ranked_page.visits for ranked_page in ranked_pages
    }

    assert visits_by_key["app_window:cursor:A"] == 2
    assert visits_by_key["app_window:cursor:B"] == 1


def test_rank_contexts_uses_activity_weighted_transitions():
    events = [
        _event("Test App", "A", seconds=0),
        _event("Test App", "B", seconds=10),
        _event("Test App", "A", seconds=20),
        _event("Test App", "C", seconds=30),
        _activity_event(
            "keyboard_burst",
            seconds=35,
            metadata={"key_count": 200},
        ),
        _activity_event(
            "mouse_activity",
            seconds=40,
            metadata={"move_count": 0},
        ),
    ]

    ranked_pages = rank_contexts(events)
    ranked_by_key = {ranked.page.source_key: ranked for ranked in ranked_pages}

    assert ranked_by_key[APP_C_KEY].score > ranked_by_key[APP_B_KEY].score


def test_rank_contexts_groups_github_pages_by_repo_source():
    events = [
        _event("Cursor", "context_graph.py - melone", seconds=0),
        _url_event(
            "Pull request - melone",
            "https://github.com/cloneisyou/melone/pull/1",
            seconds=1,
        ),
        _url_event(
            "Issue - melone",
            "https://github.com/cloneisyou/melone/issues/3",
            seconds=2,
        ),
        _event("Slack", "dev - Clone - Slack", seconds=3),
    ]

    ranked_pages = rank_contexts(events)
    ranked_by_source = {ranked.page.source_key: ranked for ranked in ranked_pages}

    github_page = ranked_by_source["github:repo:cloneisyou/melone"]
    assert github_page.page.kind == "url"
    assert github_page.score > 0
    assert github_page.visits == 2
    assert github_page.retrieval_locators == (
        "url:https://github.com/cloneisyou/melone/pull/1",
        "url:https://github.com/cloneisyou/melone/issues/3",
    )


def test_rank_contexts_ranks_agent_conversation_as_url_context():
    events = [
        _event("Cursor", "context_graph.py - melone", seconds=0),
        _agent_event("https://chatgpt.com/c/abc", app_name="ChatGPT", seconds=1),
        _event("Cursor", "context_graph.py - melone", seconds=2),
    ]

    ranked_pages = rank_contexts(events)
    ranked_by_key = {ranked.page.source_key: ranked for ranked in ranked_pages}

    agent_page = ranked_by_key["url:https://chatgpt.com/c/abc"]
    assert agent_page.page.kind == "url"
    assert agent_page.page.rankable is True
    assert agent_page.score > 0
    assert agent_page.visits == 1


def test_rank_contexts_skips_agent_conversation_without_url():
    events = [
        _event("Cursor", "A", seconds=0),
        _agent_event(None, seconds=1),
        _event("Cursor", "B", seconds=2),
    ]

    ranked_pages = rank_contexts(events)

    assert all(ranked.page.kind != "url" for ranked in ranked_pages)


def test_rank_contexts_blends_app_window_and_detail_page_rank(monkeypatch):
    events = [
        _event("Cursor", "A", seconds=0),
        _event("Cursor", "B", seconds=1),
        _event("Slack", "C", seconds=2),
    ]
    seen_node_sets = []

    def fake_page_rank(graph):
        nodes = context_graph._graph_nodes(graph)
        seen_node_sets.append(frozenset(nodes))

        if set(nodes) == {"app:cursor", "app:slack"}:
            return {"app:cursor": 0.8, "app:slack": 0.2}

        if set(nodes) == {
            "app_window:cursor:A",
            "app_window:cursor:B",
            "app_window:slack:C",
        }:
            return {
                "app_window:cursor:A": 0.1,
                "app_window:cursor:B": 0.2,
                "app_window:slack:C": 0.7,
            }

        return {
            _detail_node(nodes, "app_window:cursor:A"): 0.3,
            _detail_node(nodes, "app_window:cursor:B"): 0.4,
            _detail_node(nodes, "app_window:slack:C"): 0.3,
        }

    monkeypatch.setattr(context_graph, "page_rank", fake_page_rank)

    ranked_pages = rank_contexts(
        events,
        rank_weights=context_graph.ContextRankWeights(
            app=2.0,
            app_window=3.0,
            detail=5.0,
        ),
    )
    scores_by_source = {
        ranked.page.source_key: ranked.score for ranked in ranked_pages
    }

    assert len(seen_node_sets) == 3
    assert scores_by_source["app_window:cursor:A"] == pytest.approx(0.26)
    assert scores_by_source["app_window:cursor:B"] == pytest.approx(0.34)
    assert scores_by_source["app_window:slack:C"] == pytest.approx(0.40)


def test_detail_rank_source_key_includes_app_window_and_locator():
    unit = _url_unit("https://example.com/docs", seconds=0)

    page = context_graph._project_rank_page(unit, "detail")

    assert page.source_key == (
        "detail:app:google chrome:"
        "app_window:google chrome:melone:"
        "url:https://example.com/docs"
    )


def _agent_event(url, *, app_name=None, seconds):
    return normalize_event(
        "current_asset_changed",
        event_id=f"evt_{seconds}",
        timestamp=NOW + timedelta(seconds=seconds),
        app={"name": app_name} if app_name else None,
        url=url,
        source="test",
    )


def _url_event(window_title: str, url: str, *, seconds: int):
    return normalize_event(
        "current_asset_changed",
        event_id=f"evt_url_{seconds}",
        timestamp=NOW + timedelta(seconds=seconds),
        app={"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
        window={"title": window_title},
        url=url,
        source="test",
    )


def _url_unit(url: str, *, seconds: int) -> ContextUnit:
    return ContextUnit(
        app_name="Google Chrome",
        bundle_id="com.google.Chrome",
        window_title="melone",
        url=url,
        started_at=utc_timestamp(NOW + timedelta(seconds=seconds)),
        ended_at=None,
        evidence_event_ids=[f"evt_url_{seconds}"],
    )


def _unit(
    window_title: str,
    *,
    app_name: str = "Test App",
    seconds: int,
) -> ContextUnit:
    return ContextUnit(
        app_name=app_name,
        bundle_id=None,
        window_title=window_title,
        url=None,
        started_at=utc_timestamp(NOW + timedelta(seconds=seconds)),
        ended_at=None,
        evidence_event_ids=[f"evt_{seconds}"],
    )


def _closed_unit(
    window_title: str,
    *,
    start_seconds: int,
    end_seconds: int,
    app_name: str = "Test App",
) -> ContextUnit:
    return replace(
        _unit(window_title, app_name=app_name, seconds=start_seconds),
        ended_at=utc_timestamp(NOW + timedelta(seconds=end_seconds)),
    )


def _event(app_name: str, window_title: str, *, seconds: int):
    return normalize_event(
        "active_app_snapshot",
        event_id=f"evt_{seconds}",
        timestamp=NOW + timedelta(seconds=seconds),
        app={"name": app_name},
        window={"title": window_title},
        source="test",
    )


def _activity_event(event_type: str, *, seconds: int, metadata=None):
    return normalize_event(
        event_type,
        event_id=f"evt_activity_{seconds}",
        timestamp=NOW + timedelta(seconds=seconds),
        source="test",
        metadata=metadata,
    )


def _engagement_components_by_name(engagement):
    return {component.name: component for component in engagement.components}


def _path_entry(source_key, *, engagement_score):
    return context_graph._ContextPathEntry(
        source_key=source_key,
        retrieval_locator=source_key,
        engagement=context_graph._EngagementBreakdown(
            total=engagement_score,
            components=(
                context_graph._EngagementComponent(
                    name="test",
                    score=engagement_score,
                ),
            ),
        ),
    )


def _detail_node(nodes, source_key):
    return next(node for node in nodes if node.endswith(f":{source_key}"))
