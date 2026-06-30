from __future__ import annotations

import sys

from melone_service.asset.model import Asset
from melone_service.asset.resolver import ChainResolver
from melone_service.collectors.active_window import (
    ActiveWindowAPI,
    ActiveWindowSnapshot,
    MacOSActiveWindowAPI,
)
from melone_service.models import NormalizedEvent
from melone_service.pipeline.normalizer import normalize_event

# "지금 보고 있는 에셋"의 URI를 한 줄기로 수집.
CURRENT_ASSET_CHANGED = "current_asset_changed"


class CurrentAssetCollector:
    name = "current_asset"

    def __init__(
        self,
        *,
        resolver: ChainResolver,
        active_window_api: ActiveWindowAPI | None = None,
        platform_name: str | None = None,
    ) -> None:
        self.platform_name = sys.platform if platform_name is None else platform_name
        self.active_window_api = active_window_api or MacOSActiveWindowAPI(
            platform_name=self.platform_name
        )
        self.resolver = resolver
        self._last_key: tuple | None = None

    def poll(self) -> list[NormalizedEvent]:
        if self.platform_name != "darwin":
            return []

        snapshot = self.active_window_api.get_snapshot()
        if snapshot is None:
            return []

        asset = self.resolver.resolve(snapshot)

        # active_window와 같은 단위(app + window title)로 dedup해 윈도우 변화를 따라간다.
        key = (
            snapshot.app_identity,
            snapshot.window_title,
            None if asset is None else asset.identity(),
        )
        if key == self._last_key:
            return []
        self._last_key = key

        if asset is None:
            return []
        return [_asset_changed_event(snapshot, asset)]


def _asset_changed_event(snapshot: ActiveWindowSnapshot, asset: Asset) -> NormalizedEvent:
    metadata: dict[str, object] = {
        "kind": asset.kind,
        "asset_source": asset.source,
        "confidence": asset.confidence,
    }
    if asset.title is not None:
        metadata["title"] = asset.title
    if asset.candidates:
        metadata["candidates"] = list(asset.candidates)

    return normalize_event(
        CURRENT_ASSET_CHANGED,
        app=snapshot.app_context(),
        window=snapshot.window_context(),
        url=asset.uri,
        source="current_asset",
        metadata=metadata,
    )
