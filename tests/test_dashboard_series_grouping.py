"""
Unit tests for dashboard series grouping logic and series metadata extraction.
"""

import sys
import os
import unittest
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault('DATA_DIR', 'test_data')
os.environ.setdefault('BOOKS_DIR', 'test_data')


def _make_mapping(abs_id="test", series_name=None, series_sequence=None,
                  display_title="Title", display_author="Author",
                  unified_progress=0.0, cover_url=None, last_sync_unix=0.0):
    return {
        "abs_id": abs_id,
        "display_title": display_title,
        "display_author": display_author,
        "unified_progress": unified_progress,
        "series_name": series_name,
        "series_sequence": series_sequence,
        "cover_url": cover_url,
        "last_sync_unix": last_sync_unix,
        "status": "active",
        "sync_mode": "audiobook",
    }


class TestSeriesGroupingLayout(unittest.TestCase):

    def test_expanded_visible_series_does_not_stretch_neighboring_cards(self):
        source = (Path(__file__).parent.parent / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        selector = (
            "body:not(.series-grouping-off) "
            ".book-grid:has(> .series-group.expanded:not(.hidden))"
        )

        start = source.index(selector)
        self.assertIn("align-items: start", source[start:source.index("}", start)])


class TestSeriesGrouping(unittest.TestCase):

    def setUp(self):
        from src.web_server import (
            _group_dashboard_mappings_by_series,
            _extract_series_from_abs_metadata,
            _extract_series_from_booklore_metadata,
        )
        self.group = _group_dashboard_mappings_by_series
        self.extract_abs = _extract_series_from_abs_metadata
        self.extract_bl = _extract_series_from_booklore_metadata

    def test_singleton_series_renders_as_flat_card(self):
        flat = [_make_mapping(abs_id="a", series_name="Solo", series_sequence=1.0)]
        result = self.group(flat)
        self.assertEqual(len(result), 1)
        self.assertNotEqual(result[0].get("is_series_group"), True)

    def test_book_with_no_series_stays_flat(self):
        flat = [_make_mapping(abs_id="a", series_name=None)]
        result = self.group(flat)
        self.assertNotEqual(result[0].get("is_series_group"), True)

    def test_two_books_same_series_groups(self):
        flat = [
            _make_mapping(abs_id="a", series_name="Imperial Radch", series_sequence=1.0,
                          display_title="Ancillary Justice", unified_progress=100),
            _make_mapping(abs_id="b", series_name="Imperial Radch", series_sequence=2.0,
                          display_title="Ancillary Sword", unified_progress=64),
        ]
        result = self.group(flat)
        self.assertEqual(len(result), 1)
        g = result[0]
        self.assertTrue(g["is_series_group"])
        self.assertEqual(g["child_count"], 2)
        self.assertEqual(g["finished_count"], 1)
        self.assertEqual(g["section_bucket"], "not_started")
        self.assertEqual(g["next_book"]["display_title"], "Ancillary Sword")

    def test_all_finished_group_lands_in_finished(self):
        flat = [
            _make_mapping(abs_id="a", series_name="Done", series_sequence=1.0, unified_progress=100),
            _make_mapping(abs_id="b", series_name="Done", series_sequence=2.0, unified_progress=100),
        ]
        result = self.group(flat)
        self.assertEqual(result[0]["section_bucket"], "finished")

    def test_all_not_started_group_lands_in_not_started(self):
        flat = [
            _make_mapping(abs_id="a", series_name="NS", series_sequence=1.0, unified_progress=0),
            _make_mapping(abs_id="b", series_name="NS", series_sequence=2.0, unified_progress=0),
        ]
        result = self.group(flat)
        self.assertEqual(result[0]["section_bucket"], "not_started")

    def test_children_sort_by_sequence_floats(self):
        flat = [
            _make_mapping(abs_id="c", series_name="X", series_sequence=2.0, display_title="Two"),
            _make_mapping(abs_id="a", series_name="X", series_sequence=1.0, display_title="One"),
            _make_mapping(abs_id="b", series_name="X", series_sequence=1.5, display_title="One-half"),
        ]
        result = self.group(flat)
        titles = [c["display_title"] for c in result[0]["children"]]
        self.assertEqual(titles, ["One", "One-half", "Two"])

    def test_children_sort_none_sequence_last(self):
        flat = [
            _make_mapping(abs_id="b", series_name="Y", series_sequence=None, display_title="Unknown"),
            _make_mapping(abs_id="a", series_name="Y", series_sequence=1.0, display_title="First"),
        ]
        result = self.group(flat)
        titles = [c["display_title"] for c in result[0]["children"]]
        self.assertEqual(titles, ["First", "Unknown"])

    def test_series_name_normalization_groups_case_variants(self):
        flat = [
            _make_mapping(abs_id="a", series_name="The Expanse", series_sequence=1.0),
            _make_mapping(abs_id="b", series_name="THE  EXPANSE", series_sequence=2.0),
        ]
        result = self.group(flat)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["child_count"], 2)

    def test_series_group_preserves_most_common_author(self):
        flat = [
            _make_mapping(abs_id="a", series_name="S", series_sequence=1.0, display_author="James S.A. Corey"),
            _make_mapping(abs_id="b", series_name="S", series_sequence=2.0, display_author="James S.A. Corey"),
            _make_mapping(abs_id="c", series_name="S", series_sequence=3.0, display_author="Other"),
        ]
        result = self.group(flat)
        self.assertEqual(result[0]["series_author"], "James S.A. Corey")

    def test_avg_progress_computed(self):
        flat = [
            _make_mapping(abs_id="a", series_name="P", series_sequence=1.0, unified_progress=50.0),
            _make_mapping(abs_id="b", series_name="P", series_sequence=2.0, unified_progress=0.0),
        ]
        result = self.group(flat)
        self.assertAlmostEqual(result[0]["avg_progress"], 25.0)

    def test_last_sync_unix_is_max_of_children(self):
        flat = [
            _make_mapping(abs_id="a", series_name="Q", series_sequence=1.0, last_sync_unix=1000.0),
            _make_mapping(abs_id="b", series_name="Q", series_sequence=2.0, last_sync_unix=5000.0),
        ]
        result = self.group(flat)
        self.assertEqual(result[0]["last_sync_unix"], 5000.0)

    def test_stack_cover_urls_up_to_three(self):
        flat = [
            _make_mapping(abs_id="a", series_name="C", series_sequence=1.0, cover_url="u1"),
            _make_mapping(abs_id="b", series_name="C", series_sequence=2.0, cover_url="u2"),
            _make_mapping(abs_id="c", series_name="C", series_sequence=3.0, cover_url="u3"),
            _make_mapping(abs_id="d", series_name="C", series_sequence=4.0, cover_url="u4"),
        ]
        result = self.group(flat)
        self.assertEqual(result[0]["stack_cover_urls"], ["u1", "u2", "u3"])

    def test_dom_id_is_safe_slug(self):
        flat = [
            _make_mapping(abs_id="a", series_name="The Expanse!", series_sequence=1.0),
            _make_mapping(abs_id="b", series_name="The Expanse!", series_sequence=2.0),
        ]
        result = self.group(flat)
        dom_id = result[0]["dom_id"]
        self.assertTrue(dom_id.startswith("series-"))
        self.assertNotIn("!", dom_id)

    def test_mixed_series_and_flat_preserved(self):
        flat = [
            _make_mapping(abs_id="solo", series_name=None, display_title="Lone Wolf"),
            _make_mapping(abs_id="s1", series_name="Trio", series_sequence=1.0, display_title="One"),
            _make_mapping(abs_id="s2", series_name="Trio", series_sequence=2.0, display_title="Two"),
        ]
        result = self.group(flat)
        self.assertEqual(len(result), 2)
        types = [r.get("is_series_group", False) for r in result]
        self.assertIn(False, types)
        self.assertIn(True, types)


class TestExtractSeriesFromAbsMetadata(unittest.TestCase):

    def setUp(self):
        from src.web_server import _extract_series_from_abs_metadata
        self.extract = _extract_series_from_abs_metadata

    def test_standard_series_array(self):
        name, seq = self.extract({"series": [{"name": "Foundation", "sequence": "1.5"}]})
        self.assertEqual(name, "Foundation")
        self.assertAlmostEqual(seq, 1.5)

    def test_missing_sequence(self):
        name, seq = self.extract({"series": [{"name": "Foundation"}]})
        self.assertEqual(name, "Foundation")
        self.assertIsNone(seq)

    def test_seriesname_flat_field(self):
        name, seq = self.extract({"seriesName": "Dune"})
        self.assertEqual(name, "Dune")
        self.assertIsNone(seq)

    def test_empty_series_array_falls_back_to_seriesname(self):
        name, seq = self.extract({"series": [], "seriesName": "Fallback"})
        self.assertEqual(name, "Fallback")

    def test_none_input(self):
        name, seq = self.extract(None)
        self.assertIsNone(name)
        self.assertIsNone(seq)

    def test_empty_dict(self):
        name, seq = self.extract({})
        self.assertIsNone(name)
        self.assertIsNone(seq)

    def test_invalid_sequence_returns_none(self):
        name, seq = self.extract({"series": [{"name": "S", "sequence": "not-a-number"}]})
        self.assertEqual(name, "S")
        self.assertIsNone(seq)

    def test_integer_sequence(self):
        name, seq = self.extract({"series": [{"name": "X", "sequence": "3"}]})
        self.assertAlmostEqual(seq, 3.0)

    def test_blank_series_name_returns_none(self):
        name, seq = self.extract({"series": [{"name": "   ", "sequence": "1"}]})
        self.assertIsNone(name)


class TestExtractSeriesFromBookloreMetadata(unittest.TestCase):

    def setUp(self):
        from src.web_server import _extract_series_from_booklore_metadata
        self.extract = _extract_series_from_booklore_metadata

    def test_standard_booklore_metadata(self):
        raw = {"metadata": {"seriesName": "Wheel of Time", "seriesNumber": "1"}}
        name, seq = self.extract(raw)
        self.assertEqual(name, "Wheel of Time")
        self.assertAlmostEqual(seq, 1.0)

    def test_flat_booklore_response(self):
        raw = {"seriesName": "Mistborn", "seriesNumber": "2"}
        name, seq = self.extract(raw)
        self.assertEqual(name, "Mistborn")
        self.assertAlmostEqual(seq, 2.0)

    def test_none_input(self):
        name, seq = self.extract(None)
        self.assertIsNone(name)
        self.assertIsNone(seq)

    def test_missing_series_name(self):
        name, seq = self.extract({"metadata": {"title": "Something"}})
        self.assertIsNone(name)


class TestMigrationHasSeriesColumns(unittest.TestCase):

    def test_series_columns_exist_after_migrations(self):
        """Migration smoke test: after upgrading to head, series columns must exist."""
        import tempfile
        from alembic.config import Config
        from alembic import command
        import sqlalchemy as sa

        tmp = tempfile.mkdtemp()
        try:
            db_path = str(Path(tmp) / "test.db")
            db_url = f"sqlite:///{db_path}"

            alembic_cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
            alembic_cfg.set_main_option("sqlalchemy.url", db_url)

            command.upgrade(alembic_cfg, "head")

            engine = sa.create_engine(db_url)
            try:
                with engine.connect() as conn:
                    inspector = sa.inspect(conn)
                    col_names = {c["name"] for c in inspector.get_columns("books")}
            finally:
                engine.dispose()

            self.assertIn("series_name", col_names, "series_name column missing after migration")
            self.assertIn("series_sequence", col_names, "series_sequence column missing after migration")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
