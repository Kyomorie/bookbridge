"""CTC chapter search as the chunking prior (docs/PLAN_CTC_CHAPTER_SEARCH.md, Phase 2-3).

A long book with no usable prior used to refuse CTC ("no chunking prior") and wait
for a Whisper transcript. With CTC_CHAPTER_SEARCH on, `align_forced_and_store`
runs the model once, locates each spine chapter in the greedy decode, and hands
`align` the resulting (boundaries, text_range, exclude_spans) plus the emissions.

Only the model is faked: the emission is a one-hot log-prob tensor spelling the
narrated chapters, so the real greedy decode, chapter search, gates and wiring run.
"""
import os
import random
from typing import Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

torch = pytest.importorskip("torch")

from src.db.database_service import DatabaseService
from src.services.alignment_service import AlignmentService
from src.services.ctc_search import ChapterSearchResult, build_query
from src.utils.polisher import Polisher
from tests.test_ctc_search import _make_prose

_CHARS = "abcdefghijklmnopqrstuvwxyz'"
ID_TO_CHAR = {i + 1: ch for i, ch in enumerate(_CHARS)}  # 0 is blank
CHAR_TO_ID = {ch: i for i, ch in ID_TO_CHAR.items()}
SPF = 0.02
FRAMES_PER_CHAR = 3
SILENCE_FRAMES = 200


def _book(sizes: List[int], seed: int = 7) -> Tuple[str, List[Dict]]:
    rng = random.Random(seed)
    texts = [_make_prose(rng, n) for n in sizes]
    full = "\n\n".join(texts)
    chapters, cursor = [], 0
    for t in texts:
        chapters.append({"start": cursor, "end": cursor + len(t)})
        cursor += len(t) + 2
    return full, chapters


def _emission(full: str, chapters: List[Dict], narration_order: List[int]):
    """One-hot emission narrating `chapters` in `narration_order`; also returns
    each chapter's first frame."""
    ids: List[int] = []
    first_frame: Dict[int, int] = {}
    for ci in narration_order:
        query, _ = build_query(full, chapters[ci]["start"], chapters[ci]["end"])
        first_frame[ci] = len(ids)
        for ch in query:
            ids += [CHAR_TO_ID[ch], 0, 0]
        ids += [0] * SILENCE_FRAMES
    emission = torch.full((1, len(ids), len(_CHARS) + 1), -10.0)
    emission[0, torch.arange(len(ids)), torch.tensor(ids)] = 0.0
    return emission, first_frame


@pytest.fixture
def service(tmp_path):
    db = DatabaseService(str(tmp_path / "search.db"))
    try:
        yield AlignmentService(db, Polisher())
    finally:
        db.db_manager.close()


@pytest.fixture
def search_on():
    old = os.environ.get("CTC_CHAPTER_SEARCH")
    os.environ["CTC_CHAPTER_SEARCH"] = "true"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("CTC_CHAPTER_SEARCH", None)
        else:
            os.environ["CTC_CHAPTER_SEARCH"] = old


def _run(service, full, chapters, emission, *, single_pass=False, audio_duration=10_000.0):
    """align_forced_and_store with only the model faked; returns (ok, align kwargs, emissions_for mock)."""
    calls: Dict = {}

    def fake_align(self, audio_path, text, **kwargs):
        calls.update(kwargs)
        return [{"char": c, "ts": c / 15.0} for c in range(0, len(text) + 1, 200)]

    with patch("src.utils.forced_aligner.ForcedAligner.is_available", return_value=True), \
         patch("src.utils.forced_aligner.ForcedAligner.emissions_for", return_value=(emission, SPF)) as emit, \
         patch("src.utils.forced_aligner.ForcedAligner.ctc_vocab", return_value=(0, ID_TO_CHAR)), \
         patch("src.utils.forced_aligner.ForcedAligner.can_single_pass", return_value=single_pass), \
         patch("src.utils.forced_aligner.ForcedAligner.align", autospec=True, side_effect=fake_align):
        ok = service.align_forced_and_store("book-1", ["/a.m4b"], full, spine_chapters=chapters,
                                            audio_duration=audio_duration)
    return ok, calls, emit


def test_in_order_book_aligns_against_a_search_prior(service, search_on):
    full, chapters = _book([3000, 3500, 2800])
    emission, first_frame = _emission(full, chapters, [0, 1, 2])

    ok, calls, emit = _run(service, full, chapters, emission)

    assert ok is True
    emit.assert_called_once()
    assert calls["precomputed"][0] is emission
    boundaries = calls["boundaries"]
    assert len(boundaries) >= 3
    assert all(b["char"] < c["char"] and b["ts"] <= c["ts"] for b, c in zip(boundaries, boundaries[1:]))
    assert calls["text_range"] == (chapters[0]["start"], chapters[2]["end"])
    assert calls["exclude_spans"] == []
    # Each anchor sits at its true narration time.
    for b in boundaries:
        ci = next(i for i, c in enumerate(chapters) if c["start"] <= b["char"] < c["end"])
        query_index = len(build_query(full, chapters[ci]["start"], b["char"])[0])
        truth = (first_frame[ci] + FRAMES_PER_CHAR * query_index) * SPF
        assert abs(b["ts"] - truth) < 0.1, (b, truth)
    assert service.database_service.get_alignment_method("book-1") == "ctc"


@pytest.mark.parametrize("value", ["true", "on"])
def test_setting_spellings_enable_the_search(service, value):
    old = os.environ.get("CTC_CHAPTER_SEARCH")
    os.environ["CTC_CHAPTER_SEARCH"] = value
    try:
        full, chapters = _book([3000, 3000])
        emission, _ = _emission(full, chapters, [0, 1])
        ok, _calls, emit = _run(service, full, chapters, emission)
    finally:
        if old is None:
            os.environ.pop("CTC_CHAPTER_SEARCH", None)
        else:
            os.environ["CTC_CHAPTER_SEARCH"] = old
    assert ok is True
    emit.assert_called_once()


def test_setting_off_keeps_the_no_prior_refusal(service):
    os.environ.pop("CTC_CHAPTER_SEARCH", None)
    full, chapters = _book([3000, 3000])
    emission, _ = _emission(full, chapters, [0, 1])

    ok, calls, emit = _run(service, full, chapters, emission)

    assert ok is False
    emit.assert_not_called()
    assert calls == {}


def test_a_book_that_fits_one_pass_does_not_search(service, search_on):
    full, chapters = _book([3000, 3000])
    emission, _ = _emission(full, chapters, [0, 1])

    ok, calls, emit = _run(service, full, chapters, emission, single_pass=True)

    emit.assert_not_called()
    assert calls["boundaries"] is None


def test_audio_of_another_book_falls_back_to_the_transcript_path(service, search_on, caplog):
    full, chapters = _book([3000, 3000, 3000], seed=7)
    other, other_chapters = _book([3000, 3000, 3000], seed=99)
    emission, _ = _emission(other, other_chapters, [0, 1, 2])

    ok, calls, _emit = _run(service, full, chapters, emission)

    assert ok is False
    assert calls == {}
    assert "may not be this book" in caplog.text


def test_substantial_chapters_out_of_spine_order_fall_back(service, search_on, caplog):
    full, chapters = _book([3000, 3000, 3000])
    emission, _ = _emission(full, chapters, [2, 0, 1])

    ok, calls, _emit = _run(service, full, chapters, emission)

    assert ok is False
    assert calls == {}
    assert "out of spine order" in caplog.text


def _result(i, start, end, found, frame=None, anchors=()):
    return ChapterSearchResult(
        chapter_index=i, start_char=start, end_char=end, found=found,
        start_frame=frame, end_frame=None if frame is None else frame + 100,
        confidence=0.9 if found else None, anchors=list(anchors),
    )


def test_a_short_chapter_found_out_of_order_is_dropped_not_trusted(service, search_on, caplog):
    """Measured on a real book: an 80-char chapter whose text recurs later was
    placed an hour late. It must not send an in-order book to Whisper, and it
    must not be anchored where it was wrongly found."""
    full, chapters = _book([3000, 80, 3000])
    emission, _ = _emission(full, chapters, [0, 1, 2])
    a, s, b = chapters
    crafted = [
        _result(0, a["start"], a["end"], True, 0, [(a["start"] + 100, 50)]),
        _result(1, s["start"], s["end"], True, 90_000, [(s["start"] + 10, 90_010)]),
        _result(2, b["start"], b["end"], True, 10_000, [(b["start"] + 100, 10_050)]),
    ]
    with patch("src.services.ctc_search.search_chapters", return_value=crafted):
        ok, calls, _emit = _run(service, full, chapters, emission)

    assert ok is True
    assert "dropping short chapter" in caplog.text
    assert [bd["char"] for bd in calls["boundaries"]] == [a["start"] + 100, b["start"] + 100]
    assert calls["exclude_spans"] == [(s["start"], s["end"])]
