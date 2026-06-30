import pytest

from melone_service.asset.model import AssetPermissionError
from melone_service.asset.resolvers.document import AXDocumentResult, DocumentURIResolver
from melone_service.collectors.active_window import ActiveWindowSnapshot


def _snapshot(*, pid=123, bundle_id="com.apple.Preview"):
    return ActiveWindowSnapshot(
        app_name="Preview", bundle_id=bundle_id, pid=pid, window_title="doc"
    )


def _resolver(result):
    return DocumentURIResolver(ax_document_reader=lambda pid: result)


def test_file_url_becomes_local_asset():
    asset = _resolver(AXDocumentResult(0, "file:///Users/me/a.pdf")).resolve(_snapshot())
    assert asset.kind == "local_file"
    assert asset.uri == "file:///Users/me/a.pdf"
    assert asset.source == "ax_document"


def test_posix_path_is_converted_to_file_uri():
    asset = _resolver(AXDocumentResult(0, "/Users/me/a.txt")).resolve(_snapshot())
    assert asset.uri == "file:///Users/me/a.txt"


def test_accessibility_disabled_raises_permission_error():
    with pytest.raises(AssetPermissionError) as exc_info:
        _resolver(AXDocumentResult(-25211, None)).resolve(_snapshot())
    assert exc_info.value.permission == "accessibility"


@pytest.mark.parametrize(
    "result",
    [
        AXDocumentResult(-25212, None),  # no value (unsaved window)
        AXDocumentResult(-25205, None),  # attribute unsupported (no document concept)
        AXDocumentResult(0, ""),  # empty value
        AXDocumentResult(0, "Untitled"),  # not a file path/url
    ],
)
def test_no_document_returns_none(result):
    assert _resolver(result).resolve(_snapshot()) is None


def test_handles_requires_pid():
    resolver = _resolver(AXDocumentResult(0, "file:///x"))
    assert resolver.handles(_snapshot(pid=1)) is True
    assert resolver.handles(_snapshot(pid=None)) is False


def test_does_not_handle_excluded_bundles():
    # 다른 resolver가 담당하는 앱(예: Terminal)은 Document가 건드리지 않습니다.
    resolver = DocumentURIResolver(
        ax_document_reader=lambda pid: AXDocumentResult(0, "file:///cwd/"),
        exclude_bundle_ids=frozenset({"com.apple.Terminal"}),
    )
    assert resolver.handles(_snapshot(bundle_id="com.apple.Terminal")) is False
    assert resolver.handles(_snapshot(bundle_id="com.apple.Preview")) is True


def test_default_resolver_excludes_terminals_and_browsers():
    from melone_service.asset import build_default_resolver

    document = next(
        r for r in build_default_resolver()._resolvers if r.source == "ax_document"
    )
    assert document.handles(_snapshot(bundle_id="com.apple.Terminal")) is False
    assert document.handles(_snapshot(bundle_id="com.google.Chrome")) is False
    assert document.handles(_snapshot(bundle_id="com.apple.Preview")) is True
