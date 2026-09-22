"""Tests for Phase 6a (docs/PLAN_READALONG_EPUB3_GENERATION.md): the dashboard
"Create read-along EPUB" action.

Covers: eligibility refusal (no CTC/lexical map, no BookOrbit audio source,
ebook-only mapping) returning a clear error rather than failing silently on
click; the request dispatches generation through the user-scoped
`_spawn_user_background` helper rather than a bare thread; the background
worker never mutates `Book.ebook_filename` / `Book.original_ebook_filename`
on any path (success, refusal, or exception); status polling reads the `Job`
row the same way `_record_forge_match_job` does for Forge & Match; and
removal is idempotent-safe when there is nothing to remove.

The delivery layer (`deliver_readalong_epub`, `resolve_audiobook_folder`,
`remove_readalong_epub`) is mocked throughout -- this file never exercises
the real BookOrbit API or a real EPUB build. Permission-check rejection
(claimed vs. unclaimed book across two real users) is covered separately in
tests/test_multiuser_auth.py, which already has the real-DatabaseService +
auth-enabled harness this needs.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from tests.test_webserver import MockContainer

_TEMPLATES = str(Path(__file__).parent.parent / "templates")
_STATIC = str(Path(__file__).parent.parent / "static")


def _make_book(**overrides):
    from src.db.models import Book
    fields = dict(
        abs_id="rl-book-1",
        abs_title="Read-along Test Book",
        ebook_filename="rl.epub",
        original_ebook_filename="rl.epub",
        audio_source="BookOrbit",
        audio_source_id="bo-1",
        sync_mode="audiobook",
        status="active",
    )
    fields.update(overrides)
    return Book(**fields)


class ReadalongEpubActionTestCase(unittest.TestCase):
    """LOGIN_DISABLED default (True) -- exercises eligibility/dispatch/status,
    not auth (see test_multiuser_auth.py for the permission-check test)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.temp_dir
        os.environ['BOOKS_DIR'] = self.temp_dir
        self._orig_template_dir = os.environ.get('TEMPLATE_DIR')
        self._orig_static_dir = os.environ.get('STATIC_DIR')
        os.environ['TEMPLATE_DIR'] = _TEMPLATES
        os.environ['STATIC_DIR'] = _STATIC

        self.mock_container = MockContainer()

        def mock_initialize_database(data_dir):
            return self.mock_container.mock_database_service

        import src.db.migration_utils
        self.original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = mock_initialize_database

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

        self.mock_database_service = self.mock_container.mock_database_service
        self.mock_bookorbit_client = self.mock_container.mock_bookorbit_client

        self.book = _make_book()
        self.mock_database_service.get_book.return_value = self.book
        self.mock_database_service.get_alignment_method.return_value = "ctc"
        self.mock_database_service.get_latest_job.return_value = None

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self.original_init_db
        if self._orig_template_dir is None:
            os.environ.pop('TEMPLATE_DIR', None)
        else:
            os.environ['TEMPLATE_DIR'] = self._orig_template_dir
        if self._orig_static_dir is None:
            os.environ.pop('STATIC_DIR', None)
        else:
            os.environ['STATIC_DIR'] = self._orig_static_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ---- eligibility refusals (surfaced, not a silent no-op on click) ----

    def test_book_not_found_returns_404(self):
        self.mock_database_service.get_book.return_value = None
        resp = self.client.post('/api/readalong-epub/does-not-exist')
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(resp.get_json()["success"])

    def test_refuses_ebook_only_mapping(self):
        self.book.sync_mode = "ebook_only"
        resp = self.client.post('/api/readalong-epub/rl-book-1')
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data["success"])
        self.assertIn("no audiobook", data["error"])

    def test_refuses_non_bookorbit_audio_source(self):
        self.book.audio_source = "ABS"
        resp = self.client.post('/api/readalong-epub/rl-book-1')
        self.assertEqual(resp.status_code, 400)
        self.assertIn("BookOrbit", resp.get_json()["error"])

    def test_refuses_when_alignment_method_is_coarse(self):
        """'linear'/'llm_anchor'/'storyteller*' are all real alignment
        methods build_readalong_epub would not itself refuse, but the plan
        restricts the UI action to 'ctc'/'lexical' (Sec. 0 -- resolution)."""
        for method in ("linear", "llm_anchor", "storyteller", "storyteller_linear", ""):
            with self.subTest(method=method):
                self.mock_database_service.get_alignment_method.return_value = method
                resp = self.client.post('/api/readalong-epub/rl-book-1')
                self.assertEqual(resp.status_code, 400)
                self.assertIn("alignment map", resp.get_json()["error"])

    def test_refuses_when_no_alignment_map_at_all(self):
        self.mock_database_service.get_alignment_method.return_value = None
        resp = self.client.post('/api/readalong-epub/rl-book-1')
        self.assertEqual(resp.status_code, 400)
        self.assertIn("alignment map", resp.get_json()["error"])

    def test_accepts_ctc_or_lexical(self):
        """Finding 4: 'lexical_timed' (measured word timings) is accepted
        alongside 'ctc'/'lexical' -- it was previously rejected here even
        though it is fine-grained enough, giving a disabled dashboard button
        and an HTTP 400 for books whose alignment pipeline emitted it."""
        for method in ("ctc", "lexical", "lexical_timed"):
            with self.subTest(method=method):
                self.mock_database_service.get_alignment_method.return_value = method
                with patch("src.web_server._spawn_user_background"):
                    resp = self.client.post('/api/readalong-epub/rl-book-1')
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.get_json()["success"])

    # ---- dispatch is user-scoped, not a bare thread ----------------------

    def test_eligible_request_dispatches_via_user_scoped_helper(self):
        import src.web_server as ws
        with patch("src.web_server._spawn_user_background") as mock_spawn:
            resp = self.client.post('/api/readalong-epub/rl-book-1')

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["status"], "queued")

        mock_spawn.assert_called_once()
        args, kwargs = mock_spawn.call_args
        self.assertIs(args[0], ws._readalong_epub_worker)
        self.assertEqual(args[1], "rl-book-1")
        # A Job row is recorded synchronously so the very first status poll
        # (which can race the background thread starting) already sees
        # "running" instead of "idle".
        self.mock_database_service.save_job.assert_called_once()
        saved_job = self.mock_database_service.save_job.call_args[0][0]
        self.assertEqual(saved_job.abs_id, "rl-book-1")
        self.assertEqual(saved_job.progress, 0.0)
        # Finding 2: tagged as a read-along job, not the undifferentiated
        # "alignment" kind, so a normal sync cycle's alignment-repair pass
        # can never mistake it for the alignment-build job it's meant to
        # promote and mark it falsely "done".
        self.assertEqual(saved_job.kind, ws.JOB_KIND_READALONG)

    def test_no_direct_thread_bypasses_user_scoping(self):
        """A naive `threading.Thread(target=...).start()` would run with
        whatever contextvars happen to be ambient on that new thread (none,
        per CLAUDE.md failure mode #5) instead of the triggering user's
        BookOrbit credentials. Patching threading.Thread and asserting it is
        never constructed directly by the handler pins that the route goes
        through _spawn_user_background instead."""
        with patch("src.web_server.threading.Thread") as mock_thread, \
             patch("src.web_server._spawn_user_background") as mock_spawn:
            self.client.post('/api/readalong-epub/rl-book-1')
        mock_thread.assert_not_called()
        mock_spawn.assert_called_once()

    # ---- the worker never mutates ebook_filename fields -------------------

    def _worker_clients(self):
        clients = MagicMock()
        clients.sync_clients = {"BookOrbit": Mock(), "BookOrbitAudio": Mock()}
        clients.bookorbit_client = self.mock_bookorbit_client
        return clients

    def test_worker_never_mutates_ebook_filename_on_success(self):
        import src.web_server as ws
        before_ebook = self.book.ebook_filename
        before_original = self.book.original_ebook_filename

        clients = self._worker_clients()
        with patch.object(ws, "uc", return_value=clients), \
             patch.object(ws, "deliver_readalong_epub") as mock_deliver:
            mock_deliver.return_value = Mock(confirmed=True, read_aloud_sync={"state": "enabled"})
            ws._readalong_epub_worker("rl-book-1")

        self.assertEqual(self.book.ebook_filename, before_ebook)
        self.assertEqual(self.book.original_ebook_filename, before_original)
        self.mock_database_service.update_latest_job.assert_called_once_with(
            "rl-book-1", kind=ws.JOB_KIND_READALONG, progress=1.0, last_error=None
        )

    def test_worker_never_mutates_ebook_filename_on_refusal(self):
        import src.web_server as ws
        before_ebook = self.book.ebook_filename
        before_original = self.book.original_ebook_filename

        clients = self._worker_clients()
        with patch.object(ws, "uc", return_value=clients), \
             patch.object(ws, "deliver_readalong_epub", return_value=None):
            ws._readalong_epub_worker("rl-book-1")

        self.assertEqual(self.book.ebook_filename, before_ebook)
        self.assertEqual(self.book.original_ebook_filename, before_original)
        args, kwargs = self.mock_database_service.update_latest_job.call_args
        self.assertEqual(args[0], "rl-book-1")
        self.assertIn("refused", kwargs.get("last_error", ""))

    def test_worker_never_mutates_ebook_filename_on_exception(self):
        import src.web_server as ws
        before_ebook = self.book.ebook_filename
        before_original = self.book.original_ebook_filename

        clients = self._worker_clients()
        with patch.object(ws, "uc", return_value=clients), \
             patch.object(ws, "deliver_readalong_epub", side_effect=RuntimeError("boom")):
            ws._readalong_epub_worker("rl-book-1")

        self.assertEqual(self.book.ebook_filename, before_ebook)
        self.assertEqual(self.book.original_ebook_filename, before_original)
        args, kwargs = self.mock_database_service.update_latest_job.call_args
        self.assertEqual(args[0], "rl-book-1")
        self.assertIn("boom", kwargs.get("last_error", ""))

    def test_worker_reports_unconfirmed_delivery(self):
        import src.web_server as ws
        clients = self._worker_clients()
        with patch.object(ws, "uc", return_value=clients), \
             patch.object(ws, "deliver_readalong_epub") as mock_deliver:
            mock_deliver.return_value = Mock(
                confirmed=False, read_aloud_sync={"state": "unavailable"}
            )
            ws._readalong_epub_worker("rl-book-1")

        args, kwargs = self.mock_database_service.update_latest_job.call_args
        self.assertEqual(args[0], "rl-book-1")
        self.assertEqual(kwargs.get("progress"), 1.0)
        self.assertIn("not confirmed", kwargs.get("last_error", ""))

    # ---- status polling ---------------------------------------------------

    def test_status_route_filters_by_readalong_kind(self):
        """Finding 2: the status poll must scope its `Job` lookup to
        `kind=JOB_KIND_READALONG` -- without it, a newer Forge & Match or
        alignment-repair job on the same book (sharing the same `jobs`
        table) could be read here as if it were this action's own status."""
        import src.web_server as ws
        self.mock_database_service.get_latest_job.return_value = None
        self.client.get('/api/readalong-epub/rl-book-1/status')
        self.mock_database_service.get_latest_job.assert_called_once_with(
            "rl-book-1", kind=ws.JOB_KIND_READALONG
        )

    def test_status_idle_when_no_job_exists(self):
        self.mock_database_service.get_latest_job.return_value = None
        resp = self.client.get('/api/readalong-epub/rl-book-1/status')
        self.assertEqual(resp.get_json()["state"], "idle")

    def test_status_running_while_job_incomplete(self):
        job = Mock(progress=0.0, last_error=None)
        self.mock_database_service.get_latest_job.return_value = job
        resp = self.client.get('/api/readalong-epub/rl-book-1/status')
        self.assertEqual(resp.get_json()["state"], "running")

    def test_status_done_when_progress_complete(self):
        job = Mock(progress=1.0, last_error=None)
        self.mock_database_service.get_latest_job.return_value = job
        resp = self.client.get('/api/readalong-epub/rl-book-1/status')
        self.assertEqual(resp.get_json()["state"], "done")

    def test_status_failed_surfaces_error(self):
        job = Mock(progress=0.0, last_error="something went wrong")
        self.mock_database_service.get_latest_job.return_value = job
        resp = self.client.get('/api/readalong-epub/rl-book-1/status')
        data = resp.get_json()
        self.assertEqual(data["state"], "failed")
        self.assertEqual(data["error"], "something went wrong")

    def test_status_book_not_found(self):
        self.mock_database_service.get_book.return_value = None
        resp = self.client.get('/api/readalong-epub/does-not-exist/status')
        self.assertEqual(resp.status_code, 404)

    # ---- removal is safe/idempotent when there is nothing to remove -------

    def test_remove_reports_nothing_when_not_bookorbit(self):
        self.book.audio_source = "ABS"
        resp = self.client.post('/api/readalong-epub/rl-book-1/remove')
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertFalse(data["removed"])

    def test_remove_reports_nothing_when_audio_entry_unresolvable(self):
        import src.web_server as ws
        clients = self._worker_clients()
        clients.sync_clients["BookOrbitAudio"].resolve_bookorbit_book_id.return_value = None
        with patch.object(ws, "uc", return_value=clients):
            resp = self.client.post('/api/readalong-epub/rl-book-1/remove')
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertFalse(data["removed"])

    def test_remove_reports_nothing_when_folder_unresolvable(self):
        import src.web_server as ws
        clients = self._worker_clients()
        clients.sync_clients["BookOrbitAudio"].resolve_bookorbit_book_id.return_value = "bo-audio-1"
        with patch.object(ws, "uc", return_value=clients), \
             patch.object(ws, "resolve_audiobook_folder", return_value=None):
            resp = self.client.post('/api/readalong-epub/rl-book-1/remove')
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertFalse(data["removed"])

    def test_remove_deletes_when_resolvable(self):
        import src.web_server as ws
        clients = self._worker_clients()
        clients.sync_clients["BookOrbitAudio"].resolve_bookorbit_book_id.return_value = "bo-audio-1"
        resolved = Mock(folder=Path(self.temp_dir))
        self.mock_container.mock_ebook_parser.resolve_book_path.return_value = str(
            Path(self.temp_dir) / "rl.epub"
        )

        with patch.object(ws, "uc", return_value=clients), \
             patch.object(ws, "resolve_audiobook_folder", return_value=resolved), \
             patch.object(ws, "_remove_readalong_epub_file", return_value=True) as mock_remove:
            resp = self.client.post('/api/readalong-epub/rl-book-1/remove')

        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(data["removed"])
        mock_remove.assert_called_once()
        # The path removal was asked for is derived from the source EPUB's
        # own stem sitting in the resolved audio folder (deliver_readalong_epub's
        # own deterministic-filename convention), not some independently
        # invented path.
        called_output_path = mock_remove.call_args[0][2]
        self.assertEqual(Path(called_output_path).parent, Path(self.temp_dir))

    def test_remove_reports_failure_when_service_fails(self):
        import src.web_server as ws
        clients = self._worker_clients()
        clients.sync_clients["BookOrbitAudio"].resolve_bookorbit_book_id.return_value = "bo-audio-1"
        resolved = Mock(folder=Path(self.temp_dir))
        self.mock_container.mock_ebook_parser.resolve_book_path.return_value = str(
            Path(self.temp_dir) / "rl.epub"
        )

        with patch.object(ws, "uc", return_value=clients), \
             patch.object(ws, "resolve_audiobook_folder", return_value=resolved), \
             patch.object(ws, "_remove_readalong_epub_file", return_value=False):
            resp = self.client.post('/api/readalong-epub/rl-book-1/remove')

        self.assertEqual(resp.status_code, 500)
        self.assertFalse(resp.get_json()["success"])

    def test_remove_book_not_found(self):
        self.mock_database_service.get_book.return_value = None
        resp = self.client.post('/api/readalong-epub/does-not-exist/remove')
        self.assertEqual(resp.status_code, 404)


class ReadalongDashboardMappingTestCase(unittest.TestCase):
    """Template sanity: the dashboard still renders, and the eligibility
    fields the template reads (readalong_eligible/readalong_ineligible_reason/
    readalong_audio_ok) are computed the way the action's own handler checks
    eligibility, so the button's disabled state agrees with what a click
    would actually do."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.temp_dir
        os.environ['BOOKS_DIR'] = self.temp_dir
        self._orig_template_dir = os.environ.get('TEMPLATE_DIR')
        self._orig_static_dir = os.environ.get('STATIC_DIR')
        os.environ['TEMPLATE_DIR'] = _TEMPLATES
        os.environ['STATIC_DIR'] = _STATIC

        self.mock_container = MockContainer()

        def mock_initialize_database(data_dir):
            return self.mock_container.mock_database_service

        import src.db.migration_utils
        self.original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = mock_initialize_database

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        self.mock_database_service = self.mock_container.mock_database_service

        # Same baseline defaults as tests/test_webserver.py's
        # CleanFlaskIntegrationTest -- the full dashboard render iterates
        # every one of these, so an unconfigured plain Mock() (not a real
        # list/dict) would raise TypeError deep inside _build_dashboard_mappings
        # long before this test's own assertions run.
        self.mock_database_service.get_all_books.return_value = []
        self.mock_database_service.get_all_states.return_value = []
        self.mock_database_service.get_all_hardcover_details.return_value = []
        self.mock_database_service.get_all_storygraph_details.return_value = []
        self.mock_database_service.get_all_pending_suggestions.return_value = []
        self.mock_database_service.get_all_reading_stats.return_value = {}
        self.mock_database_service.get_booklore_book.return_value = None
        self.mock_database_service.get_all_booklore_books.return_value = []
        self.mock_container.mock_abs_client.get_all_audiobooks.return_value = []
        self.mock_container.mock_abs_client.get_all_progress_raw.return_value = {}
        self.mock_container.mock_booklore_client.is_configured.return_value = False
        self.mock_container.mock_bookorbit_client.is_configured.return_value = False
        self.mock_container.mock_storygraph_client.is_configured.return_value = False
        self.mock_container.mock_storyteller_client.is_configured.return_value = False

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self.original_init_db
        if self._orig_template_dir is None:
            os.environ.pop('TEMPLATE_DIR', None)
        else:
            os.environ['TEMPLATE_DIR'] = self._orig_template_dir
        if self._orig_static_dir is None:
            os.environ.pop('STATIC_DIR', None)
        else:
            os.environ['STATIC_DIR'] = self._orig_static_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_dashboard_renders_with_eligible_and_ineligible_books(self):
        # Deliberately non-overlapping ids (neither is a substring of the
        # other) so the string search below can't cross-match.
        eligible = _make_book(abs_id="book-yes-elig", abs_title="Eligible Book")
        ineligible = _make_book(
            abs_id="book-no-audio", abs_title="Ineligible Book",
            audio_source="ABS", sync_mode="audiobook",
        )
        self.mock_database_service.get_all_books.return_value = [eligible, ineligible]
        self.mock_database_service.get_readalong_alignment_book_ids.return_value = {"book-yes-elig"}

        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Create read-along EPUB", html)
        # The ineligible book's button carries its disable reason as a title.
        self.assertIn("Read-along generation requires a BookOrbit audio source.", html)
        # The eligible book's button is present and not disabled -- anchor on
        # its exact onclick call (Jinja's |tojson renders double-quoted args
        # inside the single-quoted onclick attribute) so ordering/substring
        # collisions between the two cards can't affect the result.
        call_marker = 'generateReadalongEpub("book-yes-elig", this)'
        self.assertIn(call_marker, html)
        call_idx = html.index(call_marker)
        button_start = html.rindex("<button", 0, call_idx)
        button_snippet = html[button_start:call_idx]
        self.assertNotIn("disabled", button_snippet)


if __name__ == "__main__":
    unittest.main()
