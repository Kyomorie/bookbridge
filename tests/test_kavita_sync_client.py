"""Kavita progress adapter behavior and locator normalization tests."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.sync_clients.kavita_sync_client import KavitaSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest


def _book(**overrides):
    values = {
        "abs_id": "book-1",
        "abs_title": "The Example",
        "ebook_source": "Kavita",
        "ebook_filename": "The Example.epub",
        "original_ebook_filename": None,
        "kosync_doc_id": "deadbeef",
        "sync_mode": "ebook",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_only_supports_kavita_mappings_with_a_document_hash():
    client = KavitaSyncClient(MagicMock(), MagicMock())
    assert client.supports_book(_book()) is True
    assert client.supports_book(_book(ebook_source="BookOrbit")) is False
    assert client.supports_book(_book(kosync_doc_id=None)) is False
    assert client.supports_book(_book(sync_mode="audiobook_only")) is False


def test_service_state_prefers_locator_derived_percentage_over_stale_remote_value():
    api = MagicMock()
    api.is_configured.return_value = True
    api.get_progress_with_metadata.return_value = (
        0.90,
        "/body/DocFragment[1]/body/p[1].0",
        {"timestamp": 1_700_000_000},
    )
    parser = MagicMock()
    parser.resolve_xpath_to_index.return_value = 50
    parser.resolve_book_path.return_value = "resolved.epub"
    parser.extract_text_and_map.return_value = ("x" * 200, {})
    client = KavitaSyncClient(api, parser)

    state = client.get_service_state(_book(), SimpleNamespace(percentage=0.10, xpath="old"))

    assert state.current["pct"] == 0.25
    assert state.current["_remote_pct"] == 0.90
    assert state.current["service_updated_at"] == 1_700_000_000
    assert state.delta == 0.15


def test_same_locator_does_not_manufacture_a_delta_from_stale_stored_percentage():
    xpath = "/body/DocFragment[1]/body/p[1].0"
    api = MagicMock()
    api.is_configured.return_value = True
    api.get_progress_with_metadata.return_value = (0.90, xpath, {})
    parser = MagicMock()
    parser.resolve_xpath_to_index.return_value = 50
    parser.resolve_book_path.return_value = "resolved.epub"
    parser.extract_text_and_map.return_value = ("x" * 200, {})
    client = KavitaSyncClient(api, parser)

    state = client.get_service_state(_book(), SimpleNamespace(percentage=0.90, xpath=xpath))

    assert state.current["pct"] == 0.25
    assert state.delta == 0.0


def test_zero_progress_uses_locator_kavita_accepts_and_records_own_write():
    api = MagicMock()
    api.update_progress.return_value = True
    client = KavitaSyncClient(api, MagicMock())
    request = UpdateProgressRequest(locator_result=LocatorResult(percentage=0.0))

    with patch("src.services.write_tracker.record_write") as record_write:
        result = client.update_progress(_book(), request)

    assert result.success is True
    assert result.updated_state == {
        "pct": 0.0,
        "xpath": "/body/DocFragment[1].0",
    }
    api.update_progress.assert_called_once_with(
        "deadbeef", 0.0, "/body/DocFragment[1].0"
    )
    record_write.assert_called_once_with("Kavita", "book-1", 0.0)
