import pytest

from melone_service.asset.model import AssetPermissionError
from melone_service.asset.resolvers.browser import (
    AppleEventsDeniedError,
    BrowserURIResolver,
    _browser_spec_for_snapshot,
    _is_firefox_family,
    build_browser_url_script,
)
from melone_service.collectors.active_window import ActiveWindowSnapshot


def _snapshot(
    *, app_name="Google Chrome", bundle_id="com.google.Chrome", window_title="Example"
):
    return ActiveWindowSnapshot(
        app_name=app_name,
        bundle_id=bundle_id,
        pid=123,
        window_title=window_title,
        window_number=456,
    )


class _Runner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, script):
        self.calls.append(script)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _Clock:
    def __init__(self, values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0)


@pytest.mark.parametrize(
    ("bundle_id", "style", "fragment"),
    [
        ("com.google.Chrome", "chromium", "URL of active tab"),
        ("com.brave.Browser", "chromium", "URL of active tab"),
        ("company.thebrowser.Browser", "chromium", "URL of active tab"),
        ("com.microsoft.Edge", "chromium", "URL of active tab"),
        ("com.apple.Safari", "safari", "URL of current tab"),
        ("com.kagi.kagimacOS", "safari", "URL of current tab"),
    ],
)
def test_browser_mapping_selects_lookup_script(bundle_id, style, fragment):
    snapshot = _snapshot(app_name="Browser", bundle_id=bundle_id)
    spec = _browser_spec_for_snapshot(snapshot)

    assert spec is not None
    assert spec.script_style == style
    script = build_browser_url_script(snapshot, spec)
    assert f'tell application id "{bundle_id}"' in script
    assert fragment in script


def test_firefox_family_is_unsupported():
    resolver = BrowserURIResolver(script_runner=_Runner(["https://x"]))
    snapshot = _snapshot(app_name="Firefox", bundle_id="org.mozilla.firefox")

    assert resolver.handles(snapshot) is False
    assert resolver.resolve(snapshot) is None
    assert _is_firefox_family(_snapshot(app_name="Firefox", bundle_id=None))


def test_resolves_url_to_web_asset():
    resolver = BrowserURIResolver(script_runner=lambda script: "https://example.com/a")
    asset = resolver.resolve(_snapshot(window_title="Example"))

    assert asset.kind == "web_url"
    assert asset.uri == "https://example.com/a"
    assert asset.title == "Example"
    assert asset.source == "browser"


def test_ttl_cache_avoids_rerunning_script():
    runner = _Runner(["https://a", "https://b"])
    resolver = BrowserURIResolver(
        script_runner=runner, monotonic=_Clock([0.0, 0.5, 1.5])
    )
    snapshot = _snapshot()

    assert resolver.resolve(snapshot).uri == "https://a"  # t=0 runs
    assert resolver.resolve(snapshot).uri == "https://a"  # t=0.5 cached
    assert resolver.resolve(snapshot).uri == "https://b"  # t=1.5 re-runs
    assert len(runner.calls) == 2


def test_apple_events_denied_raises_permission_error():
    runner = _Runner([AppleEventsDeniedError("Not authorized ... (-1743)")])
    resolver = BrowserURIResolver(script_runner=runner)

    with pytest.raises(AssetPermissionError) as exc_info:
        resolver.resolve(_snapshot())
    assert exc_info.value.permission == "automation"
    assert exc_info.value.source == "browser"


def test_missing_url_is_quiet():
    resolver = BrowserURIResolver(script_runner=lambda script: "  ")
    assert resolver.resolve(_snapshot()) is None


def test_file_url_tab_is_classified_as_local():
    resolver = BrowserURIResolver(script_runner=lambda script: "file:///Users/me/page.html")
    asset = resolver.resolve(_snapshot())
    assert asset.kind == "local_file"
    assert asset.uri == "file:///Users/me/page.html"
