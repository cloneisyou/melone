import pytest

from melone_service.asset.model import Asset


def test_asset_rejects_empty_uri():
    with pytest.raises(ValueError):
        Asset(kind="web_url", uri="", source="x")


def test_identity_is_kind_and_uri_only():
    a = Asset(kind="local_file", uri="file:///a", source="x")
    b = Asset(kind="local_file", uri="file:///a", source="y", title="t", confidence=0.5)
    assert a.identity() == b.identity() == ("local_file", "file:///a")
