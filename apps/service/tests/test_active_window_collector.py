from melone_service.collectors.active_window import (
    ACTIVE_APP_CHANGED,
    ACTIVE_APP_SNAPSHOT,
    WINDOW_LAYER_KEY,
    WINDOW_NAME_KEY,
    WINDOW_NUMBER_KEY,
    WINDOW_OWNER_NAME_KEY,
    WINDOW_OWNER_PID_KEY,
    WINDOW_TITLE_CHANGED,
    ActiveWindowCollector,
    ActiveWindowSnapshot,
    MacOSActiveWindowAPI,
)


def test_active_window_collector_records_initial_snapshot_and_changes():
    api = _FakeActiveWindowAPI(
        [
            ActiveWindowSnapshot(
                app_name="Cursor",
                bundle_id="com.todesktop.230313mzl4w4u92",
                pid=101,
                window_title="melone",
                window_number=10,
            ),
            ActiveWindowSnapshot(
                app_name="Cursor",
                bundle_id="com.todesktop.230313mzl4w4u92",
                pid=101,
                window_title="melone",
                window_number=10,
            ),
            ActiveWindowSnapshot(
                app_name="Cursor",
                bundle_id="com.todesktop.230313mzl4w4u92",
                pid=101,
                window_title="implement-plan.md",
                window_number=11,
            ),
            ActiveWindowSnapshot(
                app_name="Safari",
                bundle_id="com.apple.Safari",
                pid=202,
                window_title="Melone Docs",
                window_number=20,
            ),
        ]
    )
    collector = ActiveWindowCollector(api=api, platform_name="darwin")

    initial_events = collector.poll()
    unchanged_events = collector.poll()
    title_events = collector.poll()
    app_events = collector.poll()

    assert [event.type for event in initial_events] == [ACTIVE_APP_SNAPSHOT]
    assert initial_events[0].metadata["reason"] == "initial"
    assert unchanged_events == []
    assert [event.type for event in title_events] == [
        ACTIVE_APP_SNAPSHOT,
        WINDOW_TITLE_CHANGED,
    ]
    assert title_events[0].metadata["window_title_changed"] is True
    assert title_events[1].metadata["previous_window_title"] == "melone"
    assert [event.type for event in app_events] == [
        ACTIVE_APP_SNAPSHOT,
        ACTIVE_APP_CHANGED,
        WINDOW_TITLE_CHANGED,
    ]
    assert app_events[1].app_name == "Safari"
    assert app_events[1].metadata["previous_app"]["name"] == "Cursor"


def test_active_window_collector_is_unsupported_off_macos():
    api = _FakeActiveWindowAPI(
        [ActiveWindowSnapshot(app_name="Cursor", bundle_id="app", pid=101)]
    )
    collector = ActiveWindowCollector(api=api, platform_name="linux")

    assert collector.poll() == []
    assert api.calls == 0


def test_macos_active_window_api_maps_frontmost_window_info():
    apps = {
        101: _FakeApplication(
            name="ChatGPT",
            bundle_id="com.openai.chat",
            pid=101,
        ),
        202: _FakeApplication(
            name="Terminal",
            bundle_id="com.apple.Terminal",
            pid=202,
        ),
    }
    windows = [
        {
            WINDOW_OWNER_PID_KEY: 101,
            WINDOW_LAYER_KEY: 0,
            WINDOW_NUMBER_KEY: 10,
            WINDOW_OWNER_NAME_KEY: "ChatGPT",
            WINDOW_NAME_KEY: "",
        },
        {
            WINDOW_OWNER_PID_KEY: 101,
            WINDOW_LAYER_KEY: 0,
            WINDOW_NUMBER_KEY: 11,
            WINDOW_OWNER_NAME_KEY: "ChatGPT",
            WINDOW_NAME_KEY: "ChatGPT",
        },
        {
            WINDOW_OWNER_PID_KEY: 202,
            WINDOW_LAYER_KEY: 0,
            WINDOW_NUMBER_KEY: 20,
            WINDOW_OWNER_NAME_KEY: "Terminal",
            WINDOW_NAME_KEY: "melone",
        },
    ]
    api = MacOSActiveWindowAPI(
        platform_name="darwin",
        running_application_resolver=apps.get,
        window_info_reader=lambda: windows,
    )

    snapshot = api.get_snapshot()

    assert snapshot == ActiveWindowSnapshot(
        app_name="ChatGPT",
        bundle_id="com.openai.chat",
        pid=101,
        window_title="ChatGPT",
        window_number=11,
        window_owner_name="ChatGPT",
    )


class _FakeActiveWindowAPI:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.index = 0
        self.calls = 0

    def get_snapshot(self):
        self.calls += 1
        snapshot = self.snapshots[self.index]
        if self.index < len(self.snapshots) - 1:
            self.index += 1
        return snapshot


class _FakeApplication:
    def __init__(self, *, name, bundle_id, pid):
        self.name = name
        self.bundle_id = bundle_id
        self.pid = pid

    def localizedName(self):
        return self.name

    def bundleIdentifier(self):
        return self.bundle_id

    def processIdentifier(self):
        return self.pid
