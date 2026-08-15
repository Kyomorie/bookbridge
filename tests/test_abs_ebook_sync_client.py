import json
import unittest
from unittest.mock import MagicMock, patch
from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
from src.db.models import Book
from src.sync_clients.sync_client_interface import UpdateProgressRequest, LocatorResult

class TestABSEbookSyncClient(unittest.TestCase):

    def setUp(self):
        self.mock_abs_client = MagicMock()
        self.mock_ebook_parser = MagicMock()
        self.client = ABSEbookSyncClient(self.mock_abs_client, self.mock_ebook_parser)
        self.book = Book(abs_id="test-book-id", ebook_filename="test.epub")

    def test_get_service_state_success(self):
        self.mock_abs_client.get_progress_with_status.return_value = (
            {
                'ebookProgress': 0.5,
                'ebookLocation': 'epubcfi(/6/14!/4/2/1:0)'
            }, 200,
        )
        state = self.client.get_service_state(self.book, None)
        self.assertIsNotNone(state)
        self.assertEqual(state.current['pct'], 0.5)

    # --- target precedence --------------------------------------------------

    def test_target_prefers_abs_ebook_item_id(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    abs_ebook_item_id="ebook-42")
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"ebookProgress": 0.3, "ebookLocation": "cfi1"}, 200,
        )
        state = self.client.get_service_state(book, None)
        self.assertIsNotNone(state)
        self.assertEqual(state.current['pct'], 0.3)
        self.mock_abs_client.get_progress_with_status.assert_called_once_with("ebook-42")

    def test_target_falls_back_to_ebook_source_id_when_abs(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    ebook_source="ABS", ebook_source_id="abs-ebook-99")
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"ebookProgress": 0.6, "ebookLocation": "cfi2"}, 200,
        )
        state = self.client.get_service_state(book, None)
        self.assertIsNotNone(state)
        self.mock_abs_client.get_progress_with_status.assert_called_once_with("abs-ebook-99")

    def test_target_ignores_ebook_source_id_when_not_abs(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    ebook_source="BookLore", ebook_source_id="bl-42")
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"ebookProgress": 0.4, "ebookLocation": "cfi3"}, 200,
        )
        state = self.client.get_service_state(book, None)
        self.assertIsNotNone(state)
        self.mock_abs_client.get_progress_with_status.assert_called_once_with("audio-1")

    # --- separate-item zero reset target ------------------------------------

    def test_update_progress_uses_abs_ebook_item_id_for_nonzero(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    abs_ebook_item_id="ebook-42")
        locator = LocatorResult(percentage=0.75, cfi="epubcfi(/6/20!/4:0)")
        request = UpdateProgressRequest(locator_result=locator)
        self.mock_abs_client.update_ebook_progress.return_value = True
        with patch("src.services.write_tracker.record_write"):
            self.client.update_progress(book, request)
        self.mock_abs_client.update_ebook_progress.assert_called_with(
            "ebook-42", 0.75, "epubcfi(/6/20!/4:0)"
        )

    def test_update_progress_zero_reset_uses_abs_ebook_item_id(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    abs_ebook_item_id="ebook-42")
        locator = LocatorResult(percentage=0.0, cfi="")
        request = UpdateProgressRequest(locator_result=locator)
        self.mock_abs_client.update_ebook_progress.return_value = True
        with patch("src.services.write_tracker.record_write"):
            self.client.update_progress(book, request)
        self.mock_abs_client.update_ebook_progress.assert_called_with(
            "ebook-42", 0, ""
        )

    # --- explicit unopened 404 -> 0% state ----------------------------------

    def test_explicit_404_returns_zero_state_when_abs_ebook_item_id(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    abs_ebook_item_id="ebook-42")
        self.mock_abs_client.get_progress_with_status.return_value = (None, 404)
        state = self.client.get_service_state(book, None)
        self.assertIsNotNone(state)
        self.assertEqual(state.current['pct'], 0.0)
        self.assertEqual(state.current['cfi'], "")

    def test_explicit_404_returns_zero_state_when_ebook_source_abs(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    ebook_source="ABS", ebook_source_id="abs-ebook-99")
        self.mock_abs_client.get_progress_with_status.return_value = (None, 404)
        state = self.client.get_service_state(book, None)
        self.assertIsNotNone(state)
        self.assertEqual(state.current['pct'], 0.0)

    # --- explicit 200-without-ebookProgress -> 0% state ---------------------

    def test_explicit_200_without_ep_returns_zero_state(self):
        """Combined item: audio progress exists but ebook is unopened."""
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    abs_ebook_item_id="ebook-42")
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"progress": 0.3}, 200,  # no ebookProgress key
        )
        state = self.client.get_service_state(book, None)
        self.assertIsNotNone(state)
        self.assertEqual(state.current['pct'], 0.0)
        self.assertEqual(state.current['cfi'], "")

    # --- 500 / exception status -> None -------------------------------------

    def test_500_returns_none(self):
        book = Book(abs_id="audio-1", ebook_filename="test.epub",
                    abs_ebook_item_id="ebook-42")
        self.mock_abs_client.get_progress_with_status.return_value = (None, 500)
        state = self.client.get_service_state(book, None)
        self.assertIsNone(state)

    # --- non-explicit audio-only missing ebook progress -> None -------------

    def test_non_explicit_missing_ebook_progress_returns_none(self):
        """Audio-only mapping with no explicit ABS ebook — must not reset."""
        book = Book(abs_id="audio-1", ebook_filename="test.epub")
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"progress": 0.3}, 200,  # no ebookProgress key, non-explicit
        )
        state = self.client.get_service_state(book, None)
        self.assertIsNone(state)

    # --- existing regression tests ------------------------------------------

    def test_update_progress_success(self):
        locator = LocatorResult(percentage=0.75, cfi="epubcfi(/6/20!/4:0)")
        request = UpdateProgressRequest(locator_result=locator)
        self.mock_abs_client.update_ebook_progress.return_value = True
        with patch("src.services.write_tracker.record_write") as mock_record_write:
            self.client.update_progress(self.book, request)
        self.mock_abs_client.update_ebook_progress.assert_called_with(
            "test-book-id", 0.75, "epubcfi(/6/20!/4:0)"
        )
        mock_record_write.assert_called_once_with("ABS_Ebook", "test-book-id")

    def test_threshold_is_percent_scaled(self):
        self.assertEqual(self.client.delta_abs_thresh, 0.01)

    def test_update_progress_does_not_record_write_on_failure(self):
        locator = LocatorResult(percentage=0.75, cfi="epubcfi(/6/20!/4:0)")
        request = UpdateProgressRequest(locator_result=locator)
        self.mock_abs_client.update_ebook_progress.return_value = False

        with patch("src.services.write_tracker.record_write") as mock_record_write:
            self.client.update_progress(self.book, request)

        mock_record_write.assert_not_called()

    def test_participates_in_both_audiobook_and_ebook_modes(self):
        self.assertEqual(
            self.client.get_supported_sync_types(), {'audiobook', 'ebook'}
        )

    def test_get_service_state_none_when_item_has_no_ebook_progress(self):
        self.mock_abs_client.get_progress_with_status.return_value = (
            {'progress': 0.3}, 200,
        )
        self.assertIsNone(self.client.get_service_state(self.book, None))

if __name__ == '__main__':
    unittest.main()


class TestABSReadiumLocator(unittest.TestCase):
    """Issue #359 — ABS stores whatever its reader produced in `ebookLocation`.

    The web reader (epub.js) writes an `epubcfi(...)` string; the mobile apps
    (Readium) write a JSON locator. Treating the JSON as a CFI logged
    `Error resolving CFI->index ... Unsupported parsed CFI type: tuple` every sync
    cycle and left the position resolvable only as a percentage.
    """

    # Verbatim from the issue #359 report.
    REPORTED_LOCATION = (
        '{"href":"OEBPS/Text/part0014.xhtml",'
        '"locations":{"position":78,"progression":0},'
        '"title":"Chapter 10","type":"application/xhtml+xml"}'
    )

    def setUp(self):
        self.mock_abs_client = MagicMock()
        self.mock_ebook_parser = MagicMock()
        self.client = ABSEbookSyncClient(self.mock_abs_client, self.mock_ebook_parser)
        self.book = Book(abs_id="dcc", ebook_filename="dcc.epub")

    def _state_for(self, location):
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"ebookProgress": 0.201571, "ebookLocation": location}, 200,
        )
        return self.client.get_service_state(self.book, None)

    def test_readium_locator_is_not_stored_as_a_cfi(self):
        state = self._state_for(self.REPORTED_LOCATION)
        self.assertEqual(state.current["cfi"], "")
        self.assertNotIn("{", str(state.current.get("cfi")))

    def test_readium_locator_exposes_href_and_chapter_progress(self):
        """These are the keys sync_manager normalization resolves positions from."""
        state = self._state_for(self.REPORTED_LOCATION)
        self.assertEqual(state.current["href"], "OEBPS/Text/part0014.xhtml")
        self.assertEqual(state.current["chapter_progress"], 0.0)
        self.assertEqual(state.current["position"], 78)
        self.assertAlmostEqual(state.current["pct"], 0.201571)

    def test_readium_locator_carrying_a_partial_cfi_keeps_it(self):
        state = self._state_for(
            '{"href":"OEBPS/Text/ch1.xhtml",'
            '"locations":{"progression":0.5,"partialCfi":"epubcfi(/4/2/1:7)"}}'
        )
        self.assertEqual(state.current["cfi"], "epubcfi(/4/2/1:7)")
        self.assertEqual(state.current["href"], "OEBPS/Text/ch1.xhtml")

    def test_plain_cfi_behaviour_is_unchanged(self):
        state = self._state_for("epubcfi(/6/14!/4/2/1:0)")
        self.assertEqual(state.current["cfi"], "epubcfi(/6/14!/4/2/1:0)")
        self.assertNotIn("href", state.current)
        self.assertNotIn("chapter_progress", state.current)

    def test_empty_location_behaviour_is_unchanged(self):
        state = self._state_for("")
        self.assertEqual(state.current["cfi"], "")
        self.assertNotIn("href", state.current)

    def test_text_extraction_falls_back_to_href_not_percentage(self):
        state = self._state_for(self.REPORTED_LOCATION)
        self.mock_ebook_parser.resolve_locator_id.return_value = "chapter ten text"

        text = self.client.get_text_from_current_state(self.book, state)

        self.assertEqual(text, "chapter ten text")
        self.mock_ebook_parser.resolve_locator_id.assert_called_once_with(
            "dcc.epub", "OEBPS/Text/part0014.xhtml", None,
        )
        self.mock_ebook_parser.get_text_at_percentage.assert_not_called()

    def test_text_extraction_still_falls_back_to_percentage_when_href_fails(self):
        state = self._state_for(self.REPORTED_LOCATION)
        self.mock_ebook_parser.resolve_locator_id.return_value = None
        self.mock_ebook_parser.get_text_at_percentage.return_value = "pct text"

        self.assertEqual(self.client.get_text_from_current_state(self.book, state), "pct text")


class TestCfiGuards(unittest.TestCase):
    """A non-CFI locator must not reach the CFI parser (issue #359 log spam)."""

    def setUp(self):
        from src.utils.ebook_utils import EbookParser
        self.parser = EbookParser.__new__(EbookParser)

    def test_resolve_cfi_to_index_skips_json_locators(self):
        self.assertIsNone(
            self.parser.resolve_cfi_to_index("book.epub", TestABSReadiumLocator.REPORTED_LOCATION)
        )

    def test_get_text_around_cfi_skips_json_locators(self):
        self.assertIsNone(
            self.parser.get_text_around_cfi("book.epub", TestABSReadiumLocator.REPORTED_LOCATION)
        )


class TestABSWritesMatchingLocatorShape(unittest.TestCase):
    """#359 follow-up: the reporter reads only in the ABS mobile app.

    `ebookLocation` is opaque to ABS — whatever a reader writes is handed back.
    Always writing a CFI leaves a Readium-based reader with nothing it can
    resolve, so the write mirrors whatever shape is already stored.
    """

    READIUM = ('{"href":"OEBPS/Text/part0014.xhtml","locations":'
               '{"position":78,"progression":0.336}}')

    def setUp(self):
        self.mock_abs_client = MagicMock()
        self.mock_ebook_parser = MagicMock()
        self.client = ABSEbookSyncClient(self.mock_abs_client, self.mock_ebook_parser)
        self.book = Book(abs_id="dcc", ebook_filename="dcc.epub")
        self.mock_abs_client.update_ebook_progress.return_value = True

    def _push(self, existing_location, **locator_kwargs):
        self.mock_abs_client.get_progress_with_status.return_value = (
            {"ebookProgress": 0.2, "ebookLocation": existing_location}, 200,
        )
        kwargs = {
            "percentage": 0.35,
            "cfi": "epubcfi(/6/32!/4/2/4/46/6:0)",
            "href": "OEBPS/Text/part0014.xhtml",
            "chapter_progress": 0.336,
        }
        kwargs.update(locator_kwargs)
        self.client.update_progress(self.book, UpdateProgressRequest(LocatorResult(**kwargs)))
        return self.mock_abs_client.update_ebook_progress.call_args[0][2]

    def test_readium_reader_gets_a_readium_locator(self):
        written = self._push(self.READIUM)
        payload = json.loads(written)
        self.assertEqual(payload["href"], "OEBPS/Text/part0014.xhtml")
        self.assertAlmostEqual(payload["locations"]["progression"], 0.336)
        self.assertAlmostEqual(payload["locations"]["totalProgression"], 0.35)
        # The CFI rides along so a CFI-capable reader can still resolve it.
        self.assertEqual(payload["locations"]["partialCfi"], "epubcfi(/6/32!/4/2/4/46/6:0)")

    def test_cfi_reader_still_gets_a_cfi(self):
        self.assertEqual(self._push("epubcfi(/6/14!/4/2/1:0)"), "epubcfi(/6/32!/4/2/4/46/6:0)")

    def test_unread_book_still_gets_a_cfi(self):
        self.assertEqual(self._push(""), "epubcfi(/6/32!/4/2/4/46/6:0)")
        self.assertEqual(self._push(None), "epubcfi(/6/32!/4/2/4/46/6:0)")

    def test_readium_reader_without_an_href_falls_back_to_cfi(self):
        self.assertEqual(
            self._push(self.READIUM, href=None), "epubcfi(/6/32!/4/2/4/46/6:0)")

    def test_probe_failure_falls_back_to_cfi(self):
        self.mock_abs_client.get_progress_with_status.side_effect = RuntimeError("boom")
        self.client.update_progress(self.book, UpdateProgressRequest(LocatorResult(
            percentage=0.35, cfi="epubcfi(/6/32!/4/2/4/46/6:0)",
            href="OEBPS/Text/part0014.xhtml", chapter_progress=0.336)))
        self.assertEqual(
            self.mock_abs_client.update_ebook_progress.call_args[0][2],
            "epubcfi(/6/32!/4/2/4/46/6:0)")

    def test_round_trips_through_the_parser(self):
        """What we write must be what get_service_state can read back."""
        from src.utils.ebook_utils import parse_readium_locator
        parsed = parse_readium_locator(self._push(self.READIUM))
        self.assertEqual(parsed["href"], "OEBPS/Text/part0014.xhtml")
        self.assertAlmostEqual(parsed["chapter_progress"], 0.336)
        self.assertEqual(parsed["cfi"], "epubcfi(/6/32!/4/2/4/46/6:0)")

    def test_reset_to_zero_is_unchanged(self):
        self.client.update_progress(self.book, UpdateProgressRequest(LocatorResult(percentage=0)))
        self.mock_abs_client.update_ebook_progress.assert_called_with("dcc", 0, "")
