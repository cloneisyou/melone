from __future__ import annotations

from melone_service.asset.model import Asset, AssetKind, AssetPermissionError
from melone_service.asset.resolver import ChainResolver, URIResolver
from melone_service.asset.resolvers.agent import AgentURIResolver
from melone_service.asset.resolvers.agent_sessions import default_collectors
from melone_service.asset.resolvers.browser import (
    FIREFOX_FAMILY_BUNDLE_IDS,
    SUPPORTED_BROWSERS_BY_BUNDLE_ID,
    BrowserURIResolver,
)
from melone_service.asset.resolvers.document import DocumentURIResolver

__all__ = [
    "Asset",
    "AssetKind",
    "AssetPermissionError",
    "ChainResolver",
    "URIResolver",
    "build_default_resolver",
]


def build_default_resolver() -> ChainResolver:
    # 순서 = specificity(local 우선). "로컬 있으면 로컬, 없으면 URL"을 순서로 표현.
    return ChainResolver(
        [
            AgentURIResolver(),     # local/web · 터미널 세션 파일 OR 데스크톱 AI 앱 채팅 URL
            DocumentURIResolver(    # local     · 범용 문서 앱(AXDocument)
                exclude_bundle_ids=_resolver_owned_bundle_ids(),
            ),
            BrowserURIResolver(),   # web       · 일반 브라우저 활성 탭 URL
        ]
    )


def _resolver_owned_bundle_ids() -> frozenset[str]:
    # agent/browser가 담당하는 앱. Document는 이들을 건드리지 않는다.
    owned: set[str] = set(SUPPORTED_BROWSERS_BY_BUNDLE_ID)
    owned |= FIREFOX_FAMILY_BUNDLE_IDS
    for collector in default_collectors():
        owned |= set(collector.bundle_ids)
    return frozenset(owned)
