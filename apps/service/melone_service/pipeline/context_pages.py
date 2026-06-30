from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
import re
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from melone_service.models import NormalizedEvent


ContextPageKind = Literal["url", "app_window", "app"]
ContextPageSource = Literal["url", "window_title", "app"]
CONTEXT_GRAPH_EVENT_TYPES = (
    "active_app_snapshot",
    "current_asset_changed",
)


@dataclass(frozen=True)
class ContextUnit:
    # sparse event stream에서 복원한 하나의 시간 구간입니다.
    app_name: str | None
    bundle_id: str | None
    window_title: str | None
    url: str | None
    started_at: str
    ended_at: str | None
    evidence_event_ids: list[str]


@dataclass(frozen=True)
class ContextPage:
    # PageRank는 coarse source 단위로 계산하고, retrieval은 더 정확한 locator를 씁니다.
    source_key: str
    retrieval_locator: str | None
    kind: ContextPageKind
    label: str
    app_name: str | None
    window_title: str | None
    url: str | None
    rankable: bool
    bridge: bool
    boundary: bool
    source: ContextPageSource

    @property
    def key(self) -> str:
        # 기존 호출부와 외부 사용자가 PageRank key 의미로 계속 접근할 수 있게 둡니다.
        return self.source_key


@dataclass(frozen=True)
class RankedContextPage:
    # ranking 결과와 표시용 page 정보를 함께 담습니다.
    page: ContextPage
    score: float
    visits: int
    retrieval_locators: tuple[str, ...] = ()


def build_context_units(events: Sequence[NormalizedEvent]) -> list[ContextUnit]:
    ordered_events = sorted(
        (event for event in events if event.type in CONTEXT_GRAPH_EVENT_TYPES),
        key=lambda event: (event.timestamp, event.id),
    )

    units: list[ContextUnit] = []
    current_unit: ContextUnit | None = None
    current_key: str | None = None
    app_name: str | None = None
    bundle_id: str | None = None
    window_title: str | None = None
    url: str | None = None
    # 마지막 URL이 속한 browser window state를 기억해, 같은 창의 snapshot에서
    # URL을 잃지 않게 한다. (snapshot마다 url=None으로 리셋하면 하나의 브라우징이
    # url / app_window 두 노드로 쪼개져 PageRank와 visits가 오염된다.)
    browser_url: str | None = None
    browser_bundle_id: str | None = None
    browser_window_title: str | None = None

    for event in ordered_events:
        if event.type == "active_app_snapshot":
            app_name = event.app_name
            bundle_id = event.bundle_id
            window_title = event.window_title
            # Snapshot은 URL을 증명하지 못하지만, 같은 browser window로 돌아온
            # 경우에는 마지막 URL을 유지해 browser context가 쪼개지지 않게 한다.
            same_browser_window = (
                bundle_id == browser_bundle_id
                and window_title == browser_window_title
            )
            url = browser_url if same_browser_window else None
        elif event.type == "current_asset_changed":
            if not event.url:
                # 해석 가능한 URI가 없는 asset(예: 매칭 안 된 터미널) — 랭킹에서 제외.
                continue
            app_name = event.app_name or app_name
            bundle_id = event.bundle_id or bundle_id
            window_title = event.window_title or window_title
            url = event.url
            browser_url = event.url
            browser_bundle_id = bundle_id
            browser_window_title = window_title

        next_unit = ContextUnit(
            app_name=app_name,
            bundle_id=bundle_id,
            window_title=window_title,
            url=url,
            started_at=event.timestamp,
            ended_at=None,
            evidence_event_ids=[event.id],
        )
        next_page = normalize_context_page(next_unit)
        next_key = next_page.retrieval_locator or next_page.source_key

        if current_unit is None:
            current_unit = next_unit
            current_key = next_key
            continue

        if next_key == current_key:
            current_unit = replace(
                current_unit,
                evidence_event_ids=[*current_unit.evidence_event_ids, event.id],
            )
            continue

        units.append(replace(current_unit, ended_at=event.timestamp))
        current_unit = next_unit
        current_key = next_key

    if current_unit is not None:
        units.append(current_unit)

    return units


TRACKING_QUERY_NAMES = frozenset({"fbclid", "gclid"})
LOW_SIGNAL_TITLES = frozenset({"new tab", "untitled", "새 탭", "제목 없음"})
LOW_SIGNAL_URLS = frozenset({"chrome://newtab", "chrome://newtab/"})
GITHUB_NON_REPO_PATH_PREFIXES = frozenset(
    {
        "about",
        "apps",
        "blog",
        "codespaces",
        "collections",
        "customer-stories",
        "enterprise",
        "events",
        "explore",
        "features",
        "github-copilot",
        "issues",
        "login",
        "marketplace",
        "new",
        "notifications",
        "orgs",
        "pricing",
        "pulls",
        "search",
        "settings",
        "sponsors",
        "topics",
        "trending",
    }
)


def normalize_context_page(unit: ContextUnit) -> ContextPage:
    # normalize_context_page reads only these three unit fields, and the same
    # (app, window, url) combination recurs heavily — across the three ranking
    # granularities and across repeated visits to the same page. Cache on the
    # field tuple so a rank pass normalizes each distinct combination once
    # instead of ~20x (95k calls collapse to the few hundred distinct contexts).
    return _normalize_context_page_fields(
        unit.app_name,
        unit.window_title,
        unit.url,
    )


# Pure function of its three arguments, so caching is always correct. The bound
# comfortably covers a rank window's distinct (app, window, url) set (~hundreds);
# a heavier long-lived working set just evicts LRU and recomputes — no staleness.
@lru_cache(maxsize=4096)
def _normalize_context_page_fields(
    app_name: str | None,
    window_title: str | None,
    url: str | None,
) -> ContextPage:
    if url and url.strip():
        normalized_url = _normalize_url(url)
        retrieval_locator = _url_retrieval_locator(normalized_url)
        low_signal_url = _is_low_signal_url(normalized_url)
        # Prefer the page/conversation title for the label, while keeping the
        # retrieval locator URL-based so source-level ranking does not erase page detail.
        title = _clean_text(window_title)
        display = normalized_url if not title or _is_low_signal_title(title) else title
        return _context_page(
            source_key=_url_source_key(normalized_url),
            retrieval_locator=retrieval_locator,
            kind="url",
            label=_label(app_name, display),
            app_name=app_name,
            window_title=window_title,
            url=url,
            rankable=not low_signal_url,
            bridge=low_signal_url,
            source="url",
        )

    title = _clean_text(window_title)
    cleaned_app_name = _clean_text(app_name) or "Unknown App"
    low_signal_title = _is_low_signal_title(title)

    if title and not low_signal_title:
        source_key = f"app_window:{_key_part(cleaned_app_name)}:{title}"
        return _context_page(
            source_key=source_key,
            retrieval_locator=source_key,
            kind="app_window",
            label=f"{cleaned_app_name} | {title}",
            app_name=app_name,
            window_title=window_title,
            url=url,
            rankable=True,
            bridge=False,
            source="window_title",
        )

    source_key = f"app:{_key_part(cleaned_app_name)}"
    return _context_page(
        source_key=source_key,
        retrieval_locator=source_key,
        kind="app",
        label=cleaned_app_name,
        app_name=app_name,
        window_title=window_title,
        url=url,
        rankable=not low_signal_title,
        bridge=low_signal_title,
        source="app",
    )


def _context_page(
    *,
    source_key: str,
    retrieval_locator: str | None,
    kind: ContextPageKind,
    label: str,
    app_name: str | None,
    window_title: str | None,
    url: str | None,
    rankable: bool,
    bridge: bool,
    source: ContextPageSource,
) -> ContextPage:
    return ContextPage(
        source_key=source_key,
        retrieval_locator=retrieval_locator,
        kind=kind,
        label=label,
        app_name=app_name,
        window_title=window_title,
        url=url,
        rankable=rankable,
        bridge=bridge,
        boundary=False,
        source=source,
    )


def _url_retrieval_locator(normalized_url: str) -> str:
    return f"url:{normalized_url}"


def _url_source_key(normalized_url: str) -> str:
    github_source_key = _github_repo_source_key(normalized_url)
    if github_source_key is not None:
        return github_source_key
    return _url_retrieval_locator(normalized_url)


def _github_repo_source_key(normalized_url: str) -> str | None:
    parsed = urlsplit(normalized_url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if _host_without_www(parsed.netloc) != "github.com":
        return None

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 2:
        return None

    owner, repo = path_parts[0], path_parts[1]
    if owner.casefold() in GITHUB_NON_REPO_PATH_PREFIXES:
        return None
    return f"github:repo:{owner.casefold()}/{repo.casefold()}"


def _host_without_www(host: str) -> str:
    lowered_host = host.casefold()
    if lowered_host.startswith("www."):
        return lowered_host[4:]
    return lowered_host


def _normalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    path = _remove_trailing_slash(parsed.path)
    query = urlencode(
        [
            (name, value)
            for name, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not _is_tracking_query(name)
        ]
    )

    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            query,
            "",
        )
    )


def _remove_trailing_slash(path: str) -> str:
    if path == "/":
        return ""
    if path.endswith("/"):
        return path[:-1]
    return path


def _is_tracking_query(name: str) -> bool:
    lower_name = name.lower()
    return lower_name in TRACKING_QUERY_NAMES or lower_name.startswith("utm_")


def _label(app_name: str | None, value: str) -> str:
    clean_app_name = _clean_text(app_name)
    if clean_app_name:
        return f"{clean_app_name} | {value}"
    return value


def _key_part(value: str) -> str:
    return value.lower()


def _clean_text(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _is_low_signal_title(title: str) -> bool:
    if not title:
        return False
    return title.casefold() in LOW_SIGNAL_TITLES


def _is_low_signal_url(url: str) -> bool:
    return url.casefold() in LOW_SIGNAL_URLS
