from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from melone_service.collectors.active_window import ActiveWindowSnapshot

from ..model import Asset, AssetPermissionError, kind_for_uri

# browser_url.py의 활성 탭 URL 로직을 흡수한 web_url resolver.

DEFAULT_TTL_SECONDS = 1.0

BrowserScriptStyle = Literal["chromium", "safari"]
AppleScriptRunner = Callable[[str], str]


class AppleScriptError(RuntimeError):
    pass


class AppleEventsDeniedError(AppleScriptError):
    pass


@dataclass(frozen=True)
class BrowserSpec:
    display_name: str
    script_style: BrowserScriptStyle


SUPPORTED_BROWSERS_BY_BUNDLE_ID: dict[str, BrowserSpec] = {
    "com.google.Chrome": BrowserSpec("Google Chrome", "chromium"),
    "com.brave.Browser": BrowserSpec("Brave Browser", "chromium"),
    "company.thebrowser.Browser": BrowserSpec("Arc", "chromium"),
    "com.microsoft.Edge": BrowserSpec("Microsoft Edge", "chromium"),
    "com.apple.Safari": BrowserSpec("Safari", "safari"),
    "com.kagi.kagimacOS": BrowserSpec("Orion", "safari"),
}

SUPPORTED_BROWSERS_BY_NAME: dict[str, BrowserSpec] = {
    "arc": SUPPORTED_BROWSERS_BY_BUNDLE_ID["company.thebrowser.Browser"],
    "brave": SUPPORTED_BROWSERS_BY_BUNDLE_ID["com.brave.Browser"],
    "brave browser": SUPPORTED_BROWSERS_BY_BUNDLE_ID["com.brave.Browser"],
    "chrome": SUPPORTED_BROWSERS_BY_BUNDLE_ID["com.google.Chrome"],
    "google chrome": SUPPORTED_BROWSERS_BY_BUNDLE_ID["com.google.Chrome"],
    "microsoft edge": SUPPORTED_BROWSERS_BY_BUNDLE_ID["com.microsoft.Edge"],
    "orion": SUPPORTED_BROWSERS_BY_BUNDLE_ID["com.kagi.kagimacOS"],
    "safari": SUPPORTED_BROWSERS_BY_BUNDLE_ID["com.apple.Safari"],
}

FIREFOX_FAMILY_BUNDLE_IDS = {
    "org.mozilla.firefox",
    "org.mozilla.firefoxdeveloperedition",
    "org.mozilla.nightly",
    "org.mozilla.fennec",
    "org.mozilla.librewolf",
    "one.waterfoxproject.waterfox",
    "com.mullvad.browser",
}
FIREFOX_FAMILY_NAMES = {
    "firefox",
    "firefox developer edition",
    "firefox nightly",
    "librewolf",
    "mullvad browser",
    "waterfox",
}


@dataclass(frozen=True)
class _CacheEntry:
    key: tuple[str | int | None, ...]
    asset: Asset | None
    expires_at: float


class BrowserURIResolver:
    source = "browser"

    def __init__(
        self,
        *,
        script_runner: AppleScriptRunner | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._run_script = script_runner or OsaScriptRunner().run
        self._ttl_seconds = ttl_seconds
        self._monotonic = monotonic or time.monotonic
        self._cache: _CacheEntry | None = None

    def handles(self, snapshot: ActiveWindowSnapshot) -> bool:
        return _browser_spec_for_snapshot(snapshot) is not None

    def resolve(self, snapshot: ActiveWindowSnapshot) -> Asset | None:
        spec = _browser_spec_for_snapshot(snapshot)
        if spec is None:
            return None

        now = self._monotonic()
        key = _cache_key(snapshot)
        if self._cache is not None and self._cache.key == key and now < self._cache.expires_at:
            return self._cache.asset

        asset = self._lookup(snapshot, spec)
        # osascript는 무거우므로 TTL 동안 결과(None 포함)를 캐시해 매 poll 재실행을 막습니다.
        self._cache = _CacheEntry(key, asset, now + self._ttl_seconds)
        return asset

    def _lookup(self, snapshot: ActiveWindowSnapshot, spec: BrowserSpec) -> Asset | None:
        script = build_browser_url_script(snapshot, spec)
        if script is None:
            return None
        try:
            url = _optional_url(self._run_script(script))
        except AppleEventsDeniedError as exc:
            # 명시적 실패: 권한 거부는 None으로 덮지 않고 chain이 출력하도록 raise.
            raise AssetPermissionError(
                "automation",
                source=self.source,
                bundle_id=snapshot.bundle_id,
                detail=str(exc),
            ) from exc
        except AppleScriptError:
            return None
        if url is None:
            return None
        return Asset(
            kind=kind_for_uri(url),  # file:// 탭이면 local_file로(웹으로 오분류 방지)
            uri=url,
            source=self.source,
            title=snapshot.window_title,
        )


class OsaScriptRunner:
    def __init__(self, *, timeout_seconds: float = 2.0) -> None:
        self.timeout_seconds = timeout_seconds

    def run(self, script: str) -> str:
        try:
            completed = subprocess.run(
                ["osascript"],
                input=script,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AppleScriptError("osascript was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise AppleScriptError("osascript timed out") from exc

        if completed.returncode == 0:
            return completed.stdout.strip()

        error = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"osascript exited with code {completed.returncode}"
        )
        if _is_apple_events_denied(error):
            raise AppleEventsDeniedError(error)
        raise AppleScriptError(error)


def _browser_spec_for_snapshot(snapshot: ActiveWindowSnapshot) -> BrowserSpec | None:
    if _is_firefox_family(snapshot):
        return None
    if snapshot.bundle_id in SUPPORTED_BROWSERS_BY_BUNDLE_ID:
        return SUPPORTED_BROWSERS_BY_BUNDLE_ID[snapshot.bundle_id]
    app_name = _normalize_app_name(snapshot.app_name)
    if app_name is None:
        return None
    return SUPPORTED_BROWSERS_BY_NAME.get(app_name)


def _is_firefox_family(snapshot: ActiveWindowSnapshot) -> bool:
    if snapshot.bundle_id in FIREFOX_FAMILY_BUNDLE_IDS:
        return True
    return _normalize_app_name(snapshot.app_name) in FIREFOX_FAMILY_NAMES


def build_browser_url_script(
    snapshot: ActiveWindowSnapshot, spec: BrowserSpec
) -> str | None:
    target = _applescript_application_target(snapshot, spec)
    if spec.script_style == "chromium":
        return _chromium_url_script(target)
    return _safari_url_script(target)


def _chromium_url_script(target: str) -> str:
    return f"""
tell {target}
    if (count of windows) is 0 then return ""
    set activeWindow to front window
    if (count of tabs of activeWindow) is 0 then return ""
    return URL of active tab of activeWindow
end tell
""".strip()


def _safari_url_script(target: str) -> str:
    return f"""
tell {target}
    if (count of windows) is 0 then return ""
    set activeWindow to front window
    try
        return URL of current tab of activeWindow
    on error
        return ""
    end try
end tell
""".strip()


def _applescript_application_target(
    snapshot: ActiveWindowSnapshot, spec: BrowserSpec
) -> str:
    if snapshot.bundle_id:
        return f"application id {_applescript_string(snapshot.bundle_id)}"
    app_name = snapshot.app_name or spec.display_name
    return f"application {_applescript_string(app_name)}"


def _applescript_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _cache_key(snapshot: ActiveWindowSnapshot) -> tuple[str | int | None, ...]:
    return (
        snapshot.bundle_id,
        snapshot.pid,
        snapshot.app_name,
        snapshot.window_title,
        snapshot.window_number,
    )


def _optional_url(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_app_name(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(value.strip().lower().split())
    return text or None


def _is_apple_events_denied(error: str) -> bool:
    text = error.lower()
    return (
        "-1743" in text
        or "not authorized to send apple events" in text
        or "not authorised to send apple events" in text
        or "not allowed to send apple events" in text
        or "erraeeventnotpermitted" in text
    )
