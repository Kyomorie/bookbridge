#!/usr/bin/env python3
"""ABS_EBOOK_LOCATOR_FORMAT: which shape the bridge writes to ABS ebookLocation.

Audiobookshelf keeps ONE `ebookLocation` shared by every reader, and the readers
disagree on its format. Measured on real devices 2026-08-16:

    format   | Audiobooth        | ABS official app | ABS web
    ---------|-------------------|------------------|--------
    CFI      | right chapter,    | exact            | reads it
             | ~5.4k chars early |                  |
    Readium  | exact             | COVER            | COVER

So a CFI is readable everywhere and a Readium locator strands the official clients
at the front of the book. 'cfi' is the default and matches `main`, which has always
written `locator.cfi` unconditionally; 'auto' is the dev-only mirroring behaviour.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.models import Book
from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
from src.sync_clients.sync_client_interface import (
    LocatorResult, ServiceState, UpdateProgressRequest,
)

KEY = 'ABS_EBOOK_LOCATOR_FORMAT'
CFI = "epubcfi(/6/32!/4/2/4/46/6:0)"


def _state(shape):
    return ServiceState(
        current={'pct': 0.2, 'cfi': ''}, previous_pct=0.2, delta=0.0, threshold=0.01,
        is_configured=True, locator_shape=shape,
        display=("ABS eBook", "{prev:.4%} -> {curr:.4%}"),
        value_formatter=lambda v: f"{v*100:.4f}%",
    )


class TestLocatorFormatSetting(unittest.TestCase):
    def setUp(self):
        self._original = os.environ.get(KEY)
        self.abs_client = MagicMock()
        self.abs_client.update_ebook_progress.return_value = True
        self.client = ABSEbookSyncClient(self.abs_client, MagicMock())
        self.book = Book(abs_id="dcc", abs_title="T", ebook_filename="dcc.epub")
        self.locator = LocatorResult(
            percentage=0.35, cfi=CFI,
            href="OEBPS/Text/part0014.xhtml", chapter_progress=0.336,
        )

    def tearDown(self):
        if self._original is None:
            os.environ.pop(KEY, None)
        else:
            os.environ[KEY] = self._original

    def _written(self, shape):
        self.client.update_progress(
            self.book, UpdateProgressRequest(self.locator, current_state=_state(shape))
        )
        return self.abs_client.update_ebook_progress.call_args[0][2]

    def test_default_is_cfi_even_when_reader_uses_readium(self):
        """The unset default must not strand the official ABS app at the cover."""
        os.environ.pop(KEY, None)
        self.assertEqual(self._written('readium'), CFI)

    def test_cfi_forces_a_cfi_for_a_readium_reader(self):
        os.environ[KEY] = 'cfi'
        self.assertEqual(self._written('readium'), CFI)

    def test_readium_forces_a_readium_locator_for_a_cfi_reader(self):
        os.environ[KEY] = 'readium'
        written = self._written('cfi')
        self.assertTrue(written.startswith('{'), f"expected Readium JSON, got {written!r}")
        self.assertEqual(json.loads(written)['href'], "OEBPS/Text/part0014.xhtml")

    def test_auto_mirrors_a_readium_reader(self):
        os.environ[KEY] = 'auto'
        written = self._written('readium')
        self.assertTrue(written.startswith('{'), f"expected Readium JSON, got {written!r}")

    def test_auto_mirrors_a_cfi_reader(self):
        os.environ[KEY] = 'auto'
        self.assertEqual(self._written('cfi'), CFI)

    def test_unknown_value_degrades_to_cfi(self):
        """A typo must fail safe, not strand readers on an unreadable shape."""
        os.environ[KEY] = 'epub-cfi-ish'
        self.assertEqual(self._written('readium'), CFI)

    def test_value_is_case_and_whitespace_tolerant(self):
        os.environ[KEY] = '  Readium  '
        self.assertTrue(self._written('cfi').startswith('{'))

    def test_read_per_call_so_the_ui_applies_without_restart(self):
        os.environ[KEY] = 'auto'
        self.assertTrue(self._written('readium').startswith('{'))
        os.environ[KEY] = 'cfi'
        self.assertEqual(self._written('readium'), CFI)

    def test_readium_falls_back_to_cfi_without_an_href(self):
        """build_readium_locator needs an href; no href must not lose the position."""
        os.environ[KEY] = 'readium'
        self.locator = LocatorResult(percentage=0.35, cfi=CFI)
        self.assertEqual(self._written('cfi'), CFI)

    def test_cfi_mode_never_probes_abs(self):
        """Forcing CFI makes the shape question moot; don't spend a round-trip on it."""
        os.environ[KEY] = 'cfi'
        self.client.update_progress(
            self.book, UpdateProgressRequest(self.locator, current_state=None)
        )
        self.abs_client.get_progress_with_status.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
