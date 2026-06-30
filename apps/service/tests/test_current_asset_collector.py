from melone_service.asset.model import Asset
from melone_service.collectors.active_window import ActiveWindowSnapshot
from melone_service.collectors.current_asset import (
    CURRENT_ASSET_CHANGED,
    CurrentAssetCollector,
)


def _snapshot(*, bundle_id="com.x", window_title="W"):
    return ActiveWindowSnapshot(
        app_name="App", bundle_id=bundle_id, pid=1, window_title=window_title
    )


class _FakeAPI:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.index = 0

    def get_snapshot(self):
        snapshot = self.snapshots[self.index]
        if self.index < len(self.snapshots) - 1:
            self.index += 1
        return snapshot


class _FakeResolver:
    def __init__(self, assets):
        self.assets = list(assets)
        self.index = 0

    def resolve(self, snapshot):
        asset = self.assets[self.index]
        if self.index < len(self.assets) - 1:
            self.index += 1
        return asset


def _collector(snapshots, assets, *, platform_name="darwin"):
    return CurrentAssetCollector(
        resolver=_FakeResolver(assets),
        active_window_api=_FakeAPI(snapshots),
        platform_name=platform_name,
    )


def test_non_darwin_returns_empty():
    asset = Asset("web_url", "https://a", "x")
    assert _collector([_snapshot()], [asset], platform_name="linux").poll() == []


def test_no_snapshot_returns_empty():
    assert _collector([None], [Asset("web_url", "https://a", "x")]).poll() == []


def test_no_asset_returns_empty():
    assert _collector([_snapshot()], [None]).poll() == []


def test_emits_current_asset_changed_with_uri_and_metadata():
    asset = Asset(
        kind="local_file",
        uri="file:///a.pdf",
        source="ax_document",
        title="a",
        confidence=0.9,
    )
    events = _collector([_snapshot()], [asset]).poll()

    assert len(events) == 1
    event = events[0]
    assert event.type == CURRENT_ASSET_CHANGED
    assert event.source == "current_asset"
    assert event.url == "file:///a.pdf"
    assert event.metadata["kind"] == "local_file"
    assert event.metadata["asset_source"] == "ax_document"
    assert event.metadata["title"] == "a"
    assert event.metadata["confidence"] == 0.9


def test_dedup_emits_only_on_identity_change():
    same_a = Asset("web_url", "https://a", "browser")
    same_a_again = Asset("web_url", "https://a", "browser", title="changed")
    different = Asset("web_url", "https://b", "browser")
    collector = _collector(
        [_snapshot(), _snapshot(), _snapshot()], [same_a, same_a_again, different]
    )

    assert len(collector.poll()) == 1
    assert collector.poll() == []  # same app + same URI -> no event
    assert len(collector.poll()) == 1  # URI changed -> event


def test_emits_per_app_visit_even_when_asset_repeats():
    asset = Asset("local_file", "file:///s.jsonl", "claude_code")
    # 같은 세션 파일을 Code와 cmux에서 봄 -> 앱마다 이벤트가 나와야 한다.
    collector = CurrentAssetCollector(
        resolver=_FakeResolver([asset, asset]),
        active_window_api=_FakeAPI(
            [
                _snapshot(bundle_id="com.microsoft.VSCode"),
                _snapshot(bundle_id="com.cmuxterm.app"),
            ]
        ),
        platform_name="darwin",
    )

    assert len(collector.poll()) == 1  # Code
    assert len(collector.poll()) == 1  # cmux, same URI but different app -> still emits


def test_emits_on_window_title_change_even_when_asset_repeats():
    asset = Asset("local_file", "file:///s.jsonl", "claude_code")
    # 같은 앱·같은 세션 파일이지만 창 제목(편집 중 파일)이 바뀌면 active_window처럼 다시 방출.
    collector = CurrentAssetCollector(
        resolver=_FakeResolver([asset, asset]),
        active_window_api=_FakeAPI(
            [
                _snapshot(bundle_id="com.microsoft.VSCode", window_title="base.py — x"),
                _snapshot(bundle_id="com.microsoft.VSCode", window_title="model.py — x"),
            ]
        ),
        platform_name="darwin",
    )

    assert len(collector.poll()) == 1  # base.py
    assert len(collector.poll()) == 1  # model.py: same app+asset, new title -> emits


def test_revisiting_app_after_no_asset_reemits():
    asset = Asset("web_url", "https://a", "browser")
    collector = CurrentAssetCollector(
        resolver=_FakeResolver([asset, None, asset]),
        active_window_api=_FakeAPI(
            [
                _snapshot(bundle_id="com.apple.Safari"),
                _snapshot(bundle_id="com.apple.finder"),
                _snapshot(bundle_id="com.apple.Safari"),
            ]
        ),
        platform_name="darwin",
    )

    assert len(collector.poll()) == 1  # Safari -> emit
    assert collector.poll() == []  # Finder -> no asset (recorded, no event)
    assert len(collector.poll()) == 1  # back to Safari -> re-emits
