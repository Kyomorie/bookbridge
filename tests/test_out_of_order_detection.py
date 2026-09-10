"""Issue #426: detect_out_of_order_blocks — reports large runs of anchors that
`AlignmentService._filter_monotonic_lis` had to discard because they don't chain
onto the retained (longest strictly increasing) subsequence.

This is the signature of a book whose EPUB spine order doesn't match its
audiobook's narration order — Four Past Midnight, a Stephen King collection of
four novellas spined 2-4-3-1 but narrated in published order 1-2-3-4, so the LIS
filter can keep anchors from at most two of the four blocks and silently
discards the other two (51% of candidates on the real book). Detection is
diagnostics only: it never changes the map `_filter_monotonic_lis` produces.
"""

from typing import Dict, List

import pytest

from src.db.database_service import DatabaseService
from src.services.alignment_service import AlignmentService
from src.services.map_quality import (
    _OUT_OF_ORDER_MIN_BLOCK_ANCHORS,
    _OUT_OF_ORDER_MIN_BLOCK_FRACTION,
    detect_out_of_order_blocks,
)
from src.utils.polisher import Polisher


def _block_anchors(char_start: int, char_span: int, ts_start: float,
                   pace_chars_per_sec: float, anchor_count: int) -> List[Dict]:
    """`anchor_count` anchors evenly spaced across `char_span` chars, narrated
    at a constant `pace_chars_per_sec` starting at `ts_start`. Anchors are
    strictly increasing in both char and ts, matching what real n-gram anchors
    look like inside one contiguously-narrated block of text."""
    step = char_span / anchor_count
    anchors = []
    for i in range(anchor_count):
        char = char_start + round(i * step)
        ts = ts_start + (char - char_start) / pace_chars_per_sec
        anchors.append({"char": char, "ts": ts})
    return anchors


class TestFourPastMidnightPermutation:
    """The load-bearing test: a synthetic four-block permutation with the real
    Four Past Midnight shape. EPUB char order carries the novellas as
    Secret Window (rank 2), The Sun Dog (rank 4), Library Policeman (rank 3),
    The Langoliers (rank 1) — the book's real 2-4-3-1 spine order — while the
    audiobook narrates them contiguously in published order 1-2-3-4, at the
    paces measured on the live book (15.5 / 15.0 / 13.2 / 15.6 chars/sec)."""

    def setup_method(self):
        self.secret_window = _block_anchors(0, 3300, 320.51, 15.5, 66)          # rank 2
        self.sun_dog = _block_anchors(3300, 3300, 859.18, 15.0, 66)             # rank 4
        self.library_policeman = _block_anchors(6600, 4300, 533.42, 13.2, 86)   # rank 3
        self.langoliers = _block_anchors(10900, 5000, 0.0, 15.6, 100)           # rank 1
        self.anchors = (self.secret_window + self.sun_dog +
                        self.library_policeman + self.langoliers)
        self.total_chars = 15900

    def test_lis_can_only_keep_two_of_the_four_blocks(self):
        """Sanity check on the setup, not the detector: confirms the real LIS
        really does collapse this permutation to two blocks (Secret Window +
        Library Policeman — the longest compatible chain), the premise the
        rest of this test class rests on."""
        kept = AlignmentService._filter_monotonic_lis(self.anchors)
        assert kept == self.secret_window + self.library_policeman

    def test_reports_exactly_the_two_dropped_blocks(self):
        kept = AlignmentService._filter_monotonic_lis(self.anchors)
        blocks = detect_out_of_order_blocks(self.anchors, kept, self.total_chars)

        assert len(blocks) == 2
        # Sorted by descending char span: Langoliers' block is wider than Sun Dog's.
        langoliers_block, sun_dog_block = blocks

        assert langoliers_block["char_start"] == self.langoliers[0]["char"]
        assert langoliers_block["char_end"] == self.langoliers[-1]["char"]
        assert langoliers_block["ts_start"] == pytest.approx(self.langoliers[0]["ts"])
        assert langoliers_block["ts_end"] == pytest.approx(self.langoliers[-1]["ts"])
        assert langoliers_block["anchors"] == self.langoliers

        assert sun_dog_block["char_start"] == self.sun_dog[0]["char"]
        assert sun_dog_block["char_end"] == self.sun_dog[-1]["char"]
        assert sun_dog_block["ts_start"] == pytest.approx(self.sun_dog[0]["ts"])
        assert sun_dog_block["ts_end"] == pytest.approx(self.sun_dog[-1]["ts"])
        assert sun_dog_block["anchors"] == self.sun_dog

        # Neither retained block is reported.
        reported_anchor_ids = {id(a) for block in blocks for a in block["anchors"]}
        assert not any(id(a) in reported_anchor_ids for a in kept)


def test_ordinary_monotone_book_reports_no_blocks():
    """The most important negative case: a normally-ordered book, where the
    LIS keeps everything, must never be flagged."""
    anchors = [{"char": c, "ts": c / 15.0} for c in range(0, 10000, 20)]
    kept = AlignmentService._filter_monotonic_lis(anchors)
    assert kept == anchors
    assert detect_out_of_order_blocks(anchors, kept, total_chars=10000) == []


def test_scattered_noise_below_thresholds_reports_no_blocks():
    """A handful of scattered false anchors (ordinary matcher noise) gets
    dropped by the LIS same as a real out-of-order block would, but is far too
    small to be reported as one."""
    main = [{"char": c, "ts": c / 15.0} for c in range(0, 10000, 20)]
    noise = [{"char": 5001, "ts": 1.0}, {"char": 5002, "ts": 2.0}, {"char": 5003, "ts": 3.0}]
    anchors = sorted(main + noise, key=lambda a: a["char"])

    kept = AlignmentService._filter_monotonic_lis(anchors)
    assert len(anchors) - len(kept) == len(noise)
    assert detect_out_of_order_blocks(anchors, kept, total_chars=10000) == []


class TestThresholds:
    """A single dropped block is reported only when it clears BOTH the
    char-span fraction and the anchor-count floor; each threshold is tested
    independently by holding the other comfortably above its own minimum."""

    TOTAL_CHARS = 20000

    def _build(self, block_start: int, block_end: int, block_count: int):
        """A long monotone book with one out-of-order block spliced into the
        middle: everything before and after the block forms a single
        contiguous, strictly-increasing timeline (so the LIS keeps all of it
        and only ever considers dropping the spliced-in block)."""
        main_before = [{"char": c, "ts": c / 15.0} for c in range(0, block_start, 20)]
        # +1.0 keeps this strictly greater than main_before's last ts even
        # though the block sits at char block_start (bisect_left requires a
        # strict increase, so equal ts values would collide at the seam).
        resume_ts = main_before[-1]["ts"] + 1.0
        main_after = [{"char": c, "ts": resume_ts + (c - block_end) / 15.0}
                      for c in range(block_end, self.TOTAL_CHARS, 20)]
        step = (block_end - block_start) / block_count if block_count > 1 else 0
        block = [{"char": block_start + round(i * step), "ts": i * 0.1}
                 for i in range(block_count)]
        anchors = sorted(main_before + block + main_after, key=lambda a: a["char"])
        return anchors, main_before, main_after

    def test_block_above_both_thresholds_is_reported(self):
        span = int(_OUT_OF_ORDER_MIN_BLOCK_FRACTION * self.TOTAL_CHARS * 1.2)
        count = _OUT_OF_ORDER_MIN_BLOCK_ANCHORS + 10
        anchors, main_before, main_after = self._build(8000, 8000 + span, count)

        kept = AlignmentService._filter_monotonic_lis(anchors)
        assert kept == main_before + main_after

        blocks = detect_out_of_order_blocks(anchors, kept, self.TOTAL_CHARS)
        assert len(blocks) == 1
        assert blocks[0]["char_start"] == 8000
        assert len(blocks[0]["anchors"]) == count

    def test_same_block_below_anchor_count_threshold_is_not_reported(self):
        span = int(_OUT_OF_ORDER_MIN_BLOCK_FRACTION * self.TOTAL_CHARS * 1.2)
        count = _OUT_OF_ORDER_MIN_BLOCK_ANCHORS - 40
        assert count < _OUT_OF_ORDER_MIN_BLOCK_ANCHORS
        anchors, main_before, main_after = self._build(8000, 8000 + span, count)

        kept = AlignmentService._filter_monotonic_lis(anchors)
        assert kept == main_before + main_after
        assert detect_out_of_order_blocks(anchors, kept, self.TOTAL_CHARS) == []

    def test_same_block_below_char_span_threshold_is_not_reported(self):
        span = int(_OUT_OF_ORDER_MIN_BLOCK_FRACTION * self.TOTAL_CHARS * 0.5)
        assert span < _OUT_OF_ORDER_MIN_BLOCK_FRACTION * self.TOTAL_CHARS
        count = _OUT_OF_ORDER_MIN_BLOCK_ANCHORS + 10
        anchors, main_before, main_after = self._build(8000, 8000 + span, count)

        kept = AlignmentService._filter_monotonic_lis(anchors)
        assert kept == main_before + main_after
        assert detect_out_of_order_blocks(anchors, kept, self.TOTAL_CHARS) == []


class TestDegenerateInput:

    def test_empty_anchors_returns_empty(self):
        assert detect_out_of_order_blocks([], [], total_chars=0) == []
        assert detect_out_of_order_blocks([], [], total_chars=100) == []

    def test_zero_total_chars_returns_empty(self):
        anchors = [{"char": 0, "ts": 0.0}, {"char": 100, "ts": 5.0}]
        assert detect_out_of_order_blocks(anchors, [], total_chars=0) == []

    def test_negative_total_chars_returns_empty(self):
        anchors = [{"char": 0, "ts": 0.0}, {"char": 100, "ts": 5.0}]
        assert detect_out_of_order_blocks(anchors, [], total_chars=-5) == []

    def test_kept_equal_to_anchors_returns_empty(self):
        anchors = [{"char": c, "ts": c / 15.0} for c in range(0, 1000, 20)]
        assert detect_out_of_order_blocks(anchors, anchors, total_chars=1000) == []

    def test_does_not_raise_on_degenerate_input(self):
        detect_out_of_order_blocks(None or [], None or [], total_chars=0)


# --- AlignmentService-level: the WARNING actually fires at the LIS site ---

def _unique_words(prefix: str, count: int) -> List[str]:
    return [f"{prefix}{i:04d}" for i in range(count)]


def _segments_for(words: List[str], start_time: float, word_seconds: float = 0.5):
    """One-word transcript segments, each `word_seconds` long, starting at
    `start_time`. Returns (segments, next_start_time)."""
    segments = []
    t = start_time
    for word in words:
        segments.append({"start": t, "end": t + word_seconds, "text": word})
        t += word_seconds
    return segments, t


@pytest.fixture
def service(tmp_path):
    db = DatabaseService(str(tmp_path / "out_of_order.db"))
    try:
        yield AlignmentService(db, Polisher())
    finally:
        db.db_manager.close()


def test_permuted_book_logs_out_of_order_warning(service, caplog):
    # Four uniquely-worded blocks, book order a-b-c-d but narrated d-a-c-b —
    # the same 2-4-3-1-vs-1-2-3-4 shape as Four Past Midnight, expressed as
    # real transcript segments + ebook text run through the actual n-gram
    # anchor finder rather than hand-built anchor dicts.
    a, b, c, d = _unique_words("a", 75), _unique_words("b", 77), _unique_words("c", 97), _unique_words("d", 111)
    full_text = " ".join(a + b + c + d)

    segments = []
    t = 0.0
    for block in (d, a, c, b):
        block_segments, t = _segments_for(block, t)
        segments.extend(block_segments)

    with caplog.at_level("INFO", logger="src.services.alignment_service"):
        alignment_map, method = service._generate_alignment_map_with_method(
            segments, full_text, abs_id="four-past-midnight")

    assert method == "lexical"
    assert "⚠️ Alignment: EPUB and audio are out of order for four-past-midnight" in caplog.text
    assert "out of order" in caplog.text
    # The two existing LIS log lines are untouched by this addition.
    assert "Monotonic LIS filter" in caplog.text
    assert "Dropped" in caplog.text


def test_monotone_book_does_not_log_out_of_order_warning(service, caplog):
    a, b, c, d = _unique_words("a", 75), _unique_words("b", 77), _unique_words("c", 97), _unique_words("d", 111)
    full_text = " ".join(a + b + c + d)

    segments = []
    t = 0.0
    for block in (a, b, c, d):  # narrated in the same order as the book
        block_segments, t = _segments_for(block, t)
        segments.extend(block_segments)

    with caplog.at_level("INFO", logger="src.services.alignment_service"):
        alignment_map, method = service._generate_alignment_map_with_method(
            segments, full_text, abs_id="monotone-book")

    assert method == "lexical"
    assert "out of order" not in caplog.text
