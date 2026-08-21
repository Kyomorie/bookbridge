"""Regression coverage for the reading-position-preview hardening.

Three defects, two of them observed on a live dashboard:

- a write-only tracker was selectable as the preview source, rendering a source
  line like ``hardcover · 9.5% · Approximate`` off a percentage that is only an
  echo of what BookBridge last pushed there;
- ``ABSEbook`` was collapsed into ``abs``, which merged it with the audio state
  row and routed an ebook position into the audio-alignment branch;
- the excerpt trimmed its marker-facing edges, deleting the space the position
  sits on so a word boundary rendered as ``several|notches``.
"""
import logging
from types import SimpleNamespace

from src.services.reading_position_preview import build_reading_position_preview

TEXT = "He earned him several notches in Victor's estimation and it showed. " * 12


class FakeParser:
    def __init__(self, text=TEXT):
        self.text = text
        self.xpath_result = None
        self.cfi_result = None

    def resolve_book_path(self, filename):
        return filename

    def extract_text_and_map(self, _path):
        return self.text, [{"start": 0, "end": len(self.text)}]

    def resolve_xpath_to_index(self, _filename, _xpath):
        return self.xpath_result

    def resolve_cfi_to_index(self, _filename, _cfi):
        return self.cfi_result


class FakeAlignment:
    def __init__(self, result=4321):
        self.result = result
        self.calls = []

    def get_char_for_time(self, abs_id, timestamp):
        self.calls.append((abs_id, timestamp))
        return self.result


def _book(**kwargs):
    values = {
        "abs_id": "book-1",
        "original_ebook_filename": "book.epub",
        "ebook_filename": "book.epub",
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def _state(client, percentage=0.25, last_updated=100.0, **kwargs):
    values = {
        "client_name": client,
        "percentage": percentage,
        "timestamp": None,
        "xpath": None,
        "cfi": None,
        "last_updated": last_updated,
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def _build(states, last_leader=None, parser=None, alignment=None):
    return build_reading_position_preview(
        book=_book(),
        states=states,
        last_leader=last_leader,
        ebook_parser=parser or FakeParser(),
        alignment_service=alignment,
    )


# --------------------------------------------------------------- trackers


def test_a_tracker_is_never_chosen_even_when_it_is_the_newest_state():
    """Observed live as 'hardcover · 9.5% · Approximate'.

    Every client that syncs gets a State row stamped with the same cycle time,
    so the newest-state fallback could tie-break onto a write-only tracker.
    """
    result = _build([
        _state("kosync", percentage=0.42, last_updated=100.0),
        _state("hardcover", percentage=0.095, last_updated=999.0),
    ])

    assert result["source"] == "KoSync"
    assert result["percentage"] == 42.0


def test_a_tracker_named_as_last_leader_is_still_not_chosen():
    result = _build(
        [
            _state("kosync", percentage=0.42),
            _state("storygraph", percentage=0.095, last_updated=999.0),
        ],
        last_leader="StoryGraph",
    )

    assert result["source"] == "KoSync"


def test_tracker_only_state_yields_unavailable_rather_than_a_tracker_source():
    result = _build([_state("hardcover", percentage=0.095)])

    assert result["status"] == "unavailable"
    assert result["source"] == "BookBridge"


# --------------------------------------------------------------- ABSEbook


def test_absebook_is_not_merged_into_the_abs_audio_state():
    alignment = FakeAlignment()
    result = _build(
        [
            _state("ABS", percentage=0.80, timestamp=3600.0, last_updated=999.0),
            _state("ABSEbook", percentage=0.20, last_updated=100.0),
        ],
        last_leader="ABSEbook",
        alignment=alignment,
    )

    # The ebook state is its own row and must be the one previewed...
    assert result["source"] == "Audiobookshelf (ebook)"
    assert result["percentage"] == 20.0
    # ...and it must not be pushed through the audio alignment map.
    assert alignment.calls == []


def test_abs_audio_state_still_maps_through_the_alignment():
    alignment = FakeAlignment(result=1500)
    result = _build(
        [_state("ABS", percentage=0.80, timestamp=3600.0)],
        last_leader="ABS",
        alignment=alignment,
    )

    assert result["source"] == "Audiobookshelf"
    assert result["confidence"] == "Mapped · audio alignment"
    assert alignment.calls == [("book-1", 3600.0)]


# ------------------------------------------------------------- whitespace


def test_the_space_the_marker_sits_on_is_kept():
    """'several notches' must not render as 'several|notches'."""
    parser = FakeParser()
    boundary = parser.text.index("notches")
    assert parser.text[boundary - 1] == " "
    parser.xpath_result = boundary

    result = _build([_state("kosync", xpath="/body/DocFragment[2]/body/p[1]/text().0")],
                    parser=parser)

    assert result["confidence"] == "Exact · XPath"
    assert result["before"].endswith("several ")
    assert result["after"].startswith("notches")


def test_outer_edges_are_still_trimmed():
    parser = FakeParser(text="   " + TEXT + "   ")
    parser.xpath_result = 400

    result = _build([_state("kosync", xpath="/body/DocFragment[2]/body/p[1]/text().0")],
                    parser=parser)

    assert not result["before"].startswith(" ")
    assert not result["after"].endswith(" ")


# ----------------------------------------------------------------- logging


def test_an_unreadable_ebook_is_logged_rather_than_silently_swallowed(caplog):
    class ExplodingParser(FakeParser):
        def extract_text_and_map(self, _path):
            raise OSError("corrupt epub")

    with caplog.at_level(logging.WARNING, logger="src.services.reading_position_preview"):
        result = _build([_state("kosync")], parser=ExplodingParser())

    assert result["status"] == "unavailable"
    assert any("could not read ebook text" in r.message for r in caplog.records)
    assert any(r.exc_info for r in caplog.records)


def test_a_failing_alignment_lookup_is_logged(caplog):
    class ExplodingAlignment:
        def get_char_for_time(self, abs_id, timestamp):
            raise RuntimeError("no alignment map")

    with caplog.at_level(logging.WARNING, logger="src.services.reading_position_preview"):
        result = _build(
            [_state("ABS", percentage=0.5, timestamp=3600.0)],
            last_leader="ABS",
            alignment=ExplodingAlignment(),
        )

    # Falls back to the percentage estimate rather than failing outright.
    assert result["status"] == "approximate"
    assert any("audio alignment lookup failed" in r.message for r in caplog.records)
    assert any(r.exc_info for r in caplog.records)
