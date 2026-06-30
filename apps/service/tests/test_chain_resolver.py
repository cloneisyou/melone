from melone_service.asset.model import Asset, AssetPermissionError
from melone_service.asset.resolver import ChainResolver
from melone_service.collectors.active_window import ActiveWindowSnapshot


def _snapshot(bundle_id="com.x"):
    return ActiveWindowSnapshot(
        app_name="App", bundle_id=bundle_id, pid=1, window_title="W"
    )


class _FakeResolver:
    def __init__(self, source, *, handles=True, result=None, raises=None):
        self.source = source
        self._handles = handles
        self._result = result
        self._raises = raises
        self.calls = 0

    def handles(self, snapshot):
        return self._handles

    def resolve(self, snapshot):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._result


def test_returns_first_non_none_and_short_circuits():
    first = Asset("local_file", "file:///a", "first")
    r1 = _FakeResolver("first", result=first)
    r2 = _FakeResolver("second", result=Asset("web_url", "https://b", "second"))
    assert ChainResolver([r1, r2]).resolve(_snapshot()) is first
    assert r2.calls == 0


def test_skips_resolvers_that_do_not_handle():
    r1 = _FakeResolver("first", handles=False, result=Asset("web_url", "https://a", "f"))
    chosen = Asset("web_url", "https://b", "second")
    r2 = _FakeResolver("second", result=chosen)
    assert ChainResolver([r1, r2]).resolve(_snapshot()) is chosen
    assert r1.calls == 0


def test_continues_past_none():
    chosen = Asset("web_url", "https://b", "second")
    chain = ChainResolver([_FakeResolver("first", result=None), _FakeResolver("second", result=chosen)])
    assert chain.resolve(_snapshot()) is chosen


def test_returns_none_when_nothing_resolves():
    assert ChainResolver([_FakeResolver("a", result=None)]).resolve(_snapshot()) is None


def test_permission_block_is_logged_once_then_chain_continues():
    logs = []
    r1 = _FakeResolver("first", raises=AssetPermissionError("automation", source="first"))
    chosen = Asset("web_url", "https://b", "second")
    r2 = _FakeResolver("second", result=chosen)
    chain = ChainResolver([r1, r2], log=logs.append)
    snap = _snapshot()
    assert chain.resolve(snap) is chosen
    assert chain.resolve(snap) is chosen
    assert len(logs) == 1
    assert "first" in logs[0]


def test_permission_block_relogs_after_recovery():
    logs = []
    seq = [AssetPermissionError("automation", source="flaky"), None, AssetPermissionError("automation", source="flaky")]

    class _Flaky:
        source = "flaky"

        def __init__(self):
            self.i = 0

        def handles(self, snapshot):
            return True

        def resolve(self, snapshot):
            value = seq[self.i]
            self.i += 1
            if isinstance(value, Exception):
                raise value
            return value

    chain = ChainResolver([_Flaky()], log=logs.append)
    snap = _snapshot()
    chain.resolve(snap)  # blocked -> log
    chain.resolve(snap)  # recovered -> clears block
    chain.resolve(snap)  # blocked again -> log
    assert len(logs) == 2
