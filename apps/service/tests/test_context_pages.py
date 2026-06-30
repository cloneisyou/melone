from datetime import datetime, timedelta, timezone

from melone_service.pipeline.context_pages import (
    CONTEXT_GRAPH_EVENT_TYPES,
    ContextPage,
    ContextUnit,
    RankedContextPage,
    build_context_units,
    normalize_context_page,
)
from melone_service.pipeline.normalizer import normalize_event


NOW = datetime(2026, 6, 9, 6, 0, 0, tzinfo=timezone.utc)


def test_normalize_context_page_caches_on_identity_fields_only():
    # The memoized normalizer keys on (app_name, window_title, url). Fields it
    # does not read (bundle_id, timestamps, evidence) must not affect identity,
    # and a difference in a read field must produce a distinct page.
    def unit(**overrides):
        base = dict(
            app_name="Code",
            bundle_id="com.microsoft.VSCode",
            window_title="main.py",
            url=None,
            started_at="2026-06-09T06:00:00.000Z",
            ended_at=None,
            evidence_event_ids=["e"],
        )
        base.update(overrides)
        return ContextUnit(**base)

    # Same identity fields, different ignored fields → identical (cached) page.
    page = normalize_context_page(unit())
    same = normalize_context_page(
        unit(bundle_id="other", started_at="2026-06-09T07:00:00.000Z")
    )
    assert same is page  # served from cache, not recomputed

    # A difference in any identity field → a distinct page, no collision.
    assert normalize_context_page(unit(url="https://example.com")).key != page.key
    assert (
        normalize_context_page(unit(window_title="other.py")).source_key
        != page.source_key
    )
    assert normalize_context_page(unit(app_name="Cursor")).source_key != page.source_key


def test_context_unit_preserves_expected_fields():
    unit = ContextUnit(
        app_name="Google Chrome",
        bundle_id="com.google.Chrome",
        window_title="Pull requests - melone",
        url="https://github.com/cloneisyou/melone/pulls",
        started_at="2026-06-09T06:00:00.000Z",
        ended_at="2026-06-09T06:05:00.000Z",
        evidence_event_ids=["evt_1", "evt_2"],
    )

    assert unit.app_name == "Google Chrome"
    assert unit.bundle_id == "com.google.Chrome"
    assert unit.window_title == "Pull requests - melone"
    assert unit.url == "https://github.com/cloneisyou/melone/pulls"
    assert unit.started_at == "2026-06-09T06:00:00.000Z"
    assert unit.ended_at == "2026-06-09T06:05:00.000Z"
    assert unit.evidence_event_ids == ["evt_1", "evt_2"]


def test_context_page_preserves_original_context_for_llm_output():
    page = ContextPage(
        source_key="github:repo:cloneisyou/melone",
        retrieval_locator="url:https://github.com/cloneisyou/melone/pulls",
        kind="url",
        label="Google Chrome | https://github.com/cloneisyou/melone/pulls",
        app_name="Google Chrome",
        window_title="Pull requests - melone",
        url="https://github.com/cloneisyou/melone/pulls",
        rankable=True,
        bridge=False,
        boundary=False,
        source="url",
    )

    assert page.source_key == "github:repo:cloneisyou/melone"
    assert page.key == "github:repo:cloneisyou/melone"
    assert page.retrieval_locator == "url:https://github.com/cloneisyou/melone/pulls"
    assert page.kind == "url"
    assert page.label == "Google Chrome | https://github.com/cloneisyou/melone/pulls"
    assert page.app_name == "Google Chrome"
    assert page.window_title == "Pull requests - melone"
    assert page.url == "https://github.com/cloneisyou/melone/pulls"
    assert page.rankable is True
    assert page.bridge is False
    assert page.boundary is False
    assert page.source == "url"


def test_ranked_context_page_preserves_page_score_and_visits():
    page = ContextPage(
        source_key="app_window:cursor:melone",
        retrieval_locator="app_window:cursor:melone",
        kind="app_window",
        label="Cursor | melone",
        app_name="Cursor",
        window_title="melone",
        url=None,
        rankable=True,
        bridge=False,
        boundary=False,
        source="window_title",
    )

    ranked_page = RankedContextPage(
        page=page,
        score=0.42,
        visits=3,
        retrieval_locators=("app_window:cursor:melone",),
    )

    assert ranked_page.page == page
    assert ranked_page.score == 0.42
    assert ranked_page.visits == 3
    assert ranked_page.retrieval_locators == ("app_window:cursor:melone",)


def test_context_graph_event_types_only_include_sparse_context_inputs():
    assert CONTEXT_GRAPH_EVENT_TYPES == (
        "active_app_snapshot",
        "current_asset_changed",
    )


def test_build_context_units_ranks_desktop_agent_conversation_url():
    event = _event(
        "current_asset_changed",
        event_id="evt_1",
        app={"name": "ChatGPT"},
        url="https://chatgpt.com/c/abc",
    )

    units = build_context_units([event])

    assert len(units) == 1
    assert units[0].url == "https://chatgpt.com/c/abc"
    page = normalize_context_page(units[0])
    assert page.key == "url:https://chatgpt.com/c/abc"
    assert page.kind == "url"
    assert page.rankable is True


def test_build_context_units_keeps_stable_key_for_cli_agent_project_url():
    event = _event(
        "current_asset_changed",
        event_id="evt_1",
        url="agent://claude_code/work/repo",
    )

    units = build_context_units([event])

    assert len(units) == 1
    assert units[0].url == "agent://claude_code/work/repo"
    page = normalize_context_page(units[0])
    assert page.key == "url:agent://claude_code/work/repo"
    assert page.kind == "url"
    assert page.rankable is True


def test_build_context_units_skips_agent_conversation_without_url():
    event = _event(
        "current_asset_changed",
        event_id="evt_1",
        url=None,
    )

    assert build_context_units([event]) == []


def test_build_context_units_merges_agent_and_browser_events_on_same_url():
    browser_event = _event(
        "current_asset_changed",
        event_id="evt_1",
        seconds=0,
        app={"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
        window={"title": "ChatGPT"},
        url="https://chatgpt.com/c/abc",
    )
    agent_event = _event(
        "current_asset_changed",
        event_id="evt_2",
        seconds=1,
        app={"name": "ChatGPT"},
        url="https://chatgpt.com/c/abc",
    )

    units = build_context_units([browser_event, agent_event])

    assert len(units) == 1
    assert units[0].evidence_event_ids == ["evt_1", "evt_2"]
    assert normalize_context_page(units[0]).key == "url:https://chatgpt.com/c/abc"


def test_build_context_units_creates_unit_from_active_app_snapshot_only():
    event = _event(
        "active_app_snapshot",
        event_id="evt_1",
        app={"name": "Cursor", "bundle_id": "com.todesktop.230313mzl4w4u92"},
        window={"title": "context_pages.py - melone"},
    )

    units = build_context_units([event])

    assert units == [
        ContextUnit(
            app_name="Cursor",
            bundle_id="com.todesktop.230313mzl4w4u92",
            window_title="context_pages.py - melone",
            url=None,
            started_at=event.timestamp,
            ended_at=None,
            evidence_event_ids=["evt_1"],
        )
    ]


def test_build_context_units_uses_browser_url_state_when_url_changes():
    active_event = _event(
        "active_app_snapshot",
        event_id="evt_1",
        seconds=0,
        app={"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
        window={"title": "Search"},
    )
    url_event = _event(
        "current_asset_changed",
        event_id="evt_2",
        seconds=1,
        url="https://www.google.com/search?q=slack+api",
    )

    units = build_context_units([active_event, url_event])

    assert len(units) == 2
    assert units[0].ended_at == url_event.timestamp
    assert units[1].app_name == "Google Chrome"
    assert units[1].bundle_id == "com.google.Chrome"
    assert units[1].window_title == "Search"
    assert units[1].url == "https://www.google.com/search?q=slack+api"
    assert units[1].started_at == url_event.timestamp
    assert units[1].ended_at is None
    assert units[1].evidence_event_ids == ["evt_2"]


def test_build_context_units_keeps_url_on_snapshot_from_same_browser_window():
    url_event = _event(
        "current_asset_changed",
        event_id="evt_1",
        seconds=0,
        app={"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
        window={"title": "Pull requests - melone"},
        url="https://github.com/cloneisyou/melone/pulls",
    )
    snapshot_event = _event(
        "active_app_snapshot",
        event_id="evt_2",
        seconds=1,
        app={"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
        window={"title": "Pull requests - melone"},
    )

    units = build_context_units([url_event, snapshot_event])

    assert len(units) == 1
    assert units[0].url == "https://github.com/cloneisyou/melone/pulls"
    assert units[0].evidence_event_ids == ["evt_1", "evt_2"]


def test_build_context_units_restores_url_when_returning_to_browser_window():
    url_event = _event(
        "current_asset_changed",
        event_id="evt_1",
        seconds=0,
        app={"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
        window={"title": "Pull requests - melone"},
        url="https://github.com/cloneisyou/melone/pulls",
    )
    away_event = _event(
        "active_app_snapshot",
        event_id="evt_2",
        seconds=1,
        app={"name": "Slack", "bundle_id": "com.tinyspeck.slackmacgap"},
        window={"title": "dev - Clone - Slack"},
    )
    back_event = _event(
        "active_app_snapshot",
        event_id="evt_3",
        seconds=2,
        app={"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
        window={"title": "Pull requests - melone"},
    )

    units = build_context_units([url_event, away_event, back_event])

    assert len(units) == 3
    assert units[0].url == "https://github.com/cloneisyou/melone/pulls"
    assert units[1].app_name == "Slack"
    assert units[1].url is None
    assert units[2].url == "https://github.com/cloneisyou/melone/pulls"
    assert units[2].evidence_event_ids == ["evt_3"]


def test_build_context_units_drops_url_on_snapshot_from_other_browser_window():
    url_event = _event(
        "current_asset_changed",
        event_id="evt_1",
        seconds=0,
        app={"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
        window={"title": "Pull requests - melone"},
        url="https://github.com/cloneisyou/melone/pulls",
    )
    other_window_event = _event(
        "active_app_snapshot",
        event_id="evt_2",
        seconds=1,
        app={"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
        window={"title": "New Tab"},
    )

    units = build_context_units([url_event, other_window_event])

    assert len(units) == 2
    assert units[0].url == "https://github.com/cloneisyou/melone/pulls"
    assert units[1].url is None


def test_build_context_units_merges_events_that_keep_the_same_page_key():
    first_event = _event(
        "current_asset_changed",
        event_id="evt_1",
        seconds=0,
        app={"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
        window={"title": "Docs"},
        url="https://example.com/docs?utm_source=newsletter#intro",
    )
    second_event = _event(
        "current_asset_changed",
        event_id="evt_2",
        seconds=1,
        app={"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
        window={"title": "Docs"},
        url="https://example.com/docs?fbclid=abc",
    )

    units = build_context_units([first_event, second_event])

    assert len(units) == 1
    assert units[0].started_at == first_event.timestamp
    assert units[0].ended_at is None
    assert units[0].evidence_event_ids == ["evt_1", "evt_2"]


def test_build_context_units_keeps_distinct_github_locators_in_same_source():
    first_event = _event(
        "current_asset_changed",
        event_id="evt_1",
        seconds=0,
        app={"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
        window={"title": "Pull request - melone"},
        url="https://github.com/cloneisyou/melone/pull/1",
    )
    second_event = _event(
        "current_asset_changed",
        event_id="evt_2",
        seconds=1,
        app={"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
        window={"title": "Issue - melone"},
        url="https://github.com/cloneisyou/melone/issues/3",
    )

    units = build_context_units([first_event, second_event])
    pages = [normalize_context_page(unit) for unit in units]

    assert len(units) == 2
    assert [page.source_key for page in pages] == [
        "github:repo:cloneisyou/melone",
        "github:repo:cloneisyou/melone",
    ]
    assert [page.retrieval_locator for page in pages] == [
        "url:https://github.com/cloneisyou/melone/pull/1",
        "url:https://github.com/cloneisyou/melone/issues/3",
    ]


def test_build_context_units_closes_previous_unit_when_page_key_changes():
    first_event = _event(
        "active_app_snapshot",
        event_id="evt_1",
        seconds=0,
        app={"name": "Cursor", "bundle_id": "com.todesktop.230313mzl4w4u92"},
        window={"title": "context_pages.py - melone"},
    )
    second_event = _event(
        "active_app_snapshot",
        event_id="evt_2",
        seconds=10,
        app={"name": "Slack", "bundle_id": "com.tinyspeck.slackmacgap"},
        window={"title": "dev - Clone - Slack"},
    )

    units = build_context_units([first_event, second_event])

    assert len(units) == 2
    assert units[0].started_at == first_event.timestamp
    assert units[0].ended_at == second_event.timestamp
    assert units[1].started_at == second_event.timestamp
    assert units[1].ended_at is None


def test_build_context_units_sorts_events_by_timestamp_and_id():
    later_event = _event(
        "active_app_snapshot",
        event_id="evt_b",
        seconds=10,
        app={"name": "Slack", "bundle_id": "com.tinyspeck.slackmacgap"},
        window={"title": "dev - Clone - Slack"},
    )
    earlier_event = _event(
        "active_app_snapshot",
        event_id="evt_a",
        seconds=0,
        app={"name": "Cursor", "bundle_id": "com.todesktop.230313mzl4w4u92"},
        window={"title": "context_pages.py - melone"},
    )

    units = build_context_units([later_event, earlier_event])

    assert [unit.started_at for unit in units] == [
        earlier_event.timestamp,
        later_event.timestamp,
    ]
    assert units[0].ended_at == later_event.timestamp


def test_build_context_units_sorts_same_timestamp_events_by_id():
    second_event = _event(
        "active_app_snapshot",
        event_id="evt_b",
        app={"name": "Cursor", "bundle_id": "com.todesktop.230313mzl4w4u92"},
        window={"title": "context_pages.py - melone"},
    )
    first_event = _event(
        "active_app_snapshot",
        event_id="evt_a",
        app={"name": "Cursor", "bundle_id": "com.todesktop.230313mzl4w4u92"},
        window={"title": "context_pages.py - melone"},
    )

    units = build_context_units([second_event, first_event])

    assert len(units) == 1
    assert units[0].evidence_event_ids == ["evt_a", "evt_b"]


def test_normalize_context_page_uses_source_key_and_retrieval_locator():
    unit = ContextUnit(
        app_name="Google Chrome",
        bundle_id="com.google.Chrome",
        window_title="Pull requests - melone",
        url="https://github.com/cloneisyou/melone/pulls",
        started_at="2026-06-09T06:00:00.000Z",
        ended_at=None,
        evidence_event_ids=["evt_1"],
    )

    page = normalize_context_page(unit)

    assert page.source_key == "github:repo:cloneisyou/melone"
    assert page.key == "github:repo:cloneisyou/melone"
    assert page.retrieval_locator == "url:https://github.com/cloneisyou/melone/pulls"
    assert page.kind == "url"
    assert page.label == "Google Chrome | Pull requests - melone"
    assert page.source == "url"
    assert page.window_title == "Pull requests - melone"
    assert page.url == "https://github.com/cloneisyou/melone/pulls"


def test_normalize_context_page_groups_github_repo_urls_by_source():
    urls = [
        "https://github.com/cloneisyou/melone/pull/1",
        "https://github.com/cloneisyou/melone/issues/3",
        "https://github.com/cloneisyou/melone/pulls?page=2",
        "https://github.com/cloneisyou/melone/blob/main/README.md",
    ]

    pages = [
        normalize_context_page(
            ContextUnit(
                app_name="Google Chrome",
                bundle_id="com.google.Chrome",
                window_title="melone",
                url=url,
                started_at="2026-06-09T06:00:00.000Z",
                ended_at=None,
                evidence_event_ids=[f"evt_{index}"],
            )
        )
        for index, url in enumerate(urls)
    ]

    assert {page.source_key for page in pages} == {
        "github:repo:cloneisyou/melone"
    }
    assert [page.retrieval_locator for page in pages] == [
        "url:https://github.com/cloneisyou/melone/pull/1",
        "url:https://github.com/cloneisyou/melone/issues/3",
        "url:https://github.com/cloneisyou/melone/pulls?page=2",
        "url:https://github.com/cloneisyou/melone/blob/main/README.md",
    ]


def test_normalize_context_page_removes_url_fragment_and_tracking_query():
    unit = ContextUnit(
        app_name="Google Chrome",
        bundle_id="com.google.Chrome",
        window_title="Docs",
        url=(
            "HTTPS://Example.com/docs/?utm_source=newsletter&"
            "fbclid=abc&gclid=def#section"
        ),
        started_at="2026-06-09T06:00:00.000Z",
        ended_at=None,
        evidence_event_ids=["evt_1"],
    )

    page = normalize_context_page(unit)

    assert page.key == "url:https://example.com/docs"
    assert page.label == "Google Chrome | Docs"
    assert page.url == (
        "HTTPS://Example.com/docs/?utm_source=newsletter&"
        "fbclid=abc&gclid=def#section"
    )


def test_normalize_context_page_preserves_meaningful_query():
    unit = ContextUnit(
        app_name="Google Chrome",
        bundle_id="com.google.Chrome",
        window_title="Search",
        url="https://www.google.com/search?q=slack+api&page=2&utm_medium=paid#top",
        started_at="2026-06-09T06:00:00.000Z",
        ended_at=None,
        evidence_event_ids=["evt_1"],
    )

    page = normalize_context_page(unit)

    assert page.key == "url:https://www.google.com/search?q=slack+api&page=2"
    assert page.label == "Google Chrome | Search"


def test_normalize_context_page_stabilizes_window_title_whitespace():
    unit = ContextUnit(
        app_name="Cursor",
        bundle_id="com.todesktop.230313mzl4w4u92",
        window_title="  melone    status   ",
        url=None,
        started_at="2026-06-09T06:00:00.000Z",
        ended_at=None,
        evidence_event_ids=["evt_1"],
    )

    page = normalize_context_page(unit)

    assert page.key == "app_window:cursor:melone status"
    assert page.label == "Cursor | melone status"
    assert page.window_title == "  melone    status   "
    assert page.source == "window_title"


def test_normalize_context_page_treats_low_signal_title_as_hidden_bridge():
    unit = ContextUnit(
        app_name="Google Chrome",
        bundle_id="com.google.Chrome",
        window_title="New Tab",
        url=None,
        started_at="2026-06-09T06:00:00.000Z",
        ended_at=None,
        evidence_event_ids=["evt_1"],
    )

    page = normalize_context_page(unit)

    assert page.key == "app:google chrome"
    assert page.kind == "app"
    assert page.label == "Google Chrome"
    assert page.rankable is False
    assert page.bridge is True
    assert page.boundary is False
    assert page.source == "app"


def test_normalize_context_page_treats_chrome_newtab_url_as_hidden_bridge():
    unit = ContextUnit(
        app_name="Google Chrome",
        bundle_id="com.google.Chrome",
        window_title="New Tab",
        url="chrome://newtab/",
        started_at="2026-06-09T06:00:00.000Z",
        ended_at=None,
        evidence_event_ids=["evt_1"],
    )

    page = normalize_context_page(unit)

    assert page.key == "url:chrome://newtab"
    assert page.kind == "url"
    assert page.label == "Google Chrome | chrome://newtab"
    assert page.rankable is False
    assert page.bridge is True
    assert page.source == "url"


def test_normalize_context_page_keeps_app_window_labels_without_app_specific_parsers():
    units = [
        ContextUnit(
            app_name="Slack",
            bundle_id="com.tinyspeck.slackmacgap",
            window_title="dev - Clone - Slack",
            url=None,
            started_at="2026-06-09T06:00:00.000Z",
            ended_at=None,
            evidence_event_ids=["evt_1"],
        ),
        ContextUnit(
            app_name="Cursor",
            bundle_id="com.todesktop.230313mzl4w4u92",
            window_title="context_pages.py - melone",
            url=None,
            started_at="2026-06-09T06:01:00.000Z",
            ended_at=None,
            evidence_event_ids=["evt_2"],
        ),
        ContextUnit(
            app_name="Terminal",
            bundle_id="com.apple.Terminal",
            window_title="melone - pytest - 120x40",
            url=None,
            started_at="2026-06-09T06:02:00.000Z",
            ended_at=None,
            evidence_event_ids=["evt_3"],
        ),
    ]

    pages = [normalize_context_page(unit) for unit in units]

    assert [page.kind for page in pages] == ["app_window", "app_window", "app_window"]
    assert [page.label for page in pages] == [
        "Slack | dev - Clone - Slack",
        "Cursor | context_pages.py - melone",
        "Terminal | melone - pytest - 120x40",
    ]
    assert [page.source for page in pages] == [
        "window_title",
        "window_title",
        "window_title",
    ]


def _event(
    event_type,
    *,
    event_id,
    seconds=0,
    app=None,
    window=None,
    url=None,
):
    return normalize_event(
        event_type,
        event_id=event_id,
        timestamp=NOW + timedelta(seconds=seconds),
        app=app,
        window=window,
        url=url,
        source="test",
    )
