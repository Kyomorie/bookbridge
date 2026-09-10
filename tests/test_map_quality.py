"""Issue #426: MapQuality scoring — pure functions with no DB/service dependency.

score_map's density_spread axis is the metric this phase adds: existing gap-fraction
and anchor-count checks can pass while an interpolated map still has a wildly uneven
chars/sec profile (e.g. a slow prologue vs a fast recap chapter, the real Four Past
Midnight profile that motivated this metric). density_spread slices by equal ANCHOR
COUNT (not equal char width) and uses a median-relative ratio (not a p90/p10
percentile ratio) — both corrections were made after validating against the live
map corpus; see the two dedicated tests below that fail against the earlier,
wrong implementations.
"""

import unittest
from typing import Dict, List

from src.services.map_quality import MapQuality, is_regression, score_map

_ANCHORS_PER_GROUP = 10


def _dense_map(span_chars: int, char_step: int, rate_chars_per_sec: float) -> List[Dict]:
    """An evenly-paced synthetic map: constant chars/sec throughout."""
    return [{"char": c, "ts": c / rate_chars_per_sec}
            for c in range(0, span_chars + 1, char_step)]


def _grouped_map(chars_per_group: List[int], rates: List[float]) -> List[Dict]:
    """Build a map of len(chars_per_group) * _ANCHORS_PER_GROUP anchors, split into
    equal-anchor-count groups (one per `chars_per_group`/`rates` entry). Group i
    covers chars_per_group[i] chars at rates[i] chars/sec, with anchors spaced
    evenly in char (and, within the group, in time) across the group."""
    points: List[Dict] = []
    char_cursor = 0.0
    ts_cursor = 0.0
    for chars, rate in zip(chars_per_group, rates):
        char_step = chars / _ANCHORS_PER_GROUP
        ts_step = char_step / rate
        for _ in range(_ANCHORS_PER_GROUP):
            char_cursor += char_step
            ts_cursor += ts_step
            points.append({"char": round(char_cursor), "ts": ts_cursor})
    return points


class TestScoreMapDegenerateInput(unittest.TestCase):

    def test_none_returns_zero_score_without_raising(self):
        quality = score_map(None)
        self.assertEqual(quality.score, 0.0)
        self.assertEqual(quality.max_gap_fraction, 1.0)

    def test_empty_list_returns_zero_score_without_raising(self):
        quality = score_map([])
        self.assertEqual(quality.score, 0.0)
        self.assertEqual(quality.max_gap_fraction, 1.0)

    def test_single_point_returns_zero_score_without_raising(self):
        quality = score_map([{"char": 10, "ts": 1.0}])
        self.assertEqual(quality.score, 0.0)
        self.assertEqual(quality.max_gap_fraction, 1.0)


class TestScoreMapQualitativeCases(unittest.TestCase):

    def test_dense_evenly_paced_map_scores_near_one(self):
        amap = _dense_map(100000, 100, 100.0)
        quality = score_map(amap)
        self.assertEqual(quality.anchors, 1001)
        self.assertLess(quality.max_gap_fraction, 0.01)
        self.assertGreater(quality.score, 0.95)

    def test_two_point_degenerate_map_scores_near_zero(self):
        amap = [{"char": 0, "ts": 0.0}, {"char": 100000, "ts": 1000.0}]
        quality = score_map(amap)
        self.assertEqual(quality.max_gap_fraction, 1.0)
        self.assertLess(quality.anchor_density, 1.0)
        self.assertLess(quality.score, 0.15)

    def test_huge_interpolated_gap_scores_materially_lower(self):
        base = _dense_map(100000, 100, 100.0)
        # Drop every interior anchor in a 40,000-char stretch — one huge
        # interpolated gap — while leaving the rest of the map untouched.
        gapped = [point for point in base if not (40000 < point["char"] < 80000)]

        before = score_map(base)
        after = score_map(gapped)

        self.assertAlmostEqual(after.max_gap_fraction, 0.4, places=3)
        self.assertLess(after.score, before.score - 0.25)


class TestDensitySpreadDiscriminates(unittest.TestCase):
    """The whole point of this phase: two maps with identical anchor count and
    identical max_gap_fraction must still be told apart by density_spread, and
    that difference must move the final score.

    Both maps below share the exact same (deliberately non-uniform) anchor char
    layout — a sparse first anchor-group spanning a disproportionately large
    char range, a dense last anchor-group spanning a disproportionately small
    one, baseline in between — modeled on the real Four Past Midnight anchor
    distribution ("the first 10% of anchors cover 124k chars but 38,000
    seconds"). Sharing the char layout is what makes anchor count and
    max_gap_fraction come out identical by construction: a genuinely
    evenly-char-spaced map is provably the unique minimum-gap-fraction layout
    for a given anchor count (max >= mean, equality only when uniform), so a
    map with any non-uniform layout can never tie a uniform one on
    max_gap_fraction. Only the per-group PACE (chars/sec) differs between the
    two maps: constant throughout the "even" map, anomalously slow/fast in two
    groups of the "uneven" one.
    """

    def test_even_pace_scores_higher_than_uneven_pace(self):
        # First group is anchor-sparse (50,000 chars), last is anchor-dense
        # (500 chars), 18 baseline groups make up the rest — same layout for
        # both maps, 20 groups x 10 anchors = 200 anchors, span 100,000 chars.
        chars_per_group = [50000] + [2750] * 18 + [500]
        self.assertEqual(sum(chars_per_group), 100000)

        even_rates = [15.0] * 20
        # Same shape as the real Four Past Midnight profile: one slow
        # anchor-decile (~3 c/s), one fast one (~59 c/s), ~15 c/s elsewhere.
        uneven_rates = [3.0] + [15.0] * 18 + [59.0]

        even_map = _grouped_map(chars_per_group, even_rates)
        uneven_map = _grouped_map(chars_per_group, uneven_rates)

        even_quality = score_map(even_map)
        uneven_quality = score_map(uneven_map)

        # Same char layout in both maps, so these two axes must be identical —
        # only density_spread (and therefore score) may differ.
        self.assertEqual(even_quality.anchors, uneven_quality.anchors)
        self.assertAlmostEqual(even_quality.max_gap_fraction, uneven_quality.max_gap_fraction, places=9)

        self.assertLess(even_quality.density_spread, uneven_quality.density_spread)
        self.assertGreater(even_quality.score, uneven_quality.score)

    def test_density_spread_detects_a_single_anomalous_slice(self):
        """A lone slow anchor-decile out of 20 must still move density_spread —
        this is exactly what p90/p10 percentile trimming missed (the metric's
        second wrong turn): with only one outlier, int(0.1*20)==2 and
        min(int(0.9*20),19)==18 both land on a normal neighbor, not the
        anomaly, because trimming discards the two most extreme slices at each
        end. A median-relative ratio stays robust to a single anomalous slice."""
        baseline_rate, slow_rate = 15.0, 3.0
        rates = [slow_rate] + [baseline_rate] * 19
        amap = _grouped_map([100] * 20, rates)

        quality = score_map(amap)

        self.assertAlmostEqual(quality.density_spread, baseline_rate / slow_rate, places=2)


class TestBackwardsFraction(unittest.TestCase):

    def test_nonzero_when_timestamps_regress_partway_through(self):
        amap = [{"char": 0, "ts": 0.0}, {"char": 100, "ts": 1.0}, {"char": 200, "ts": 2.0},
                {"char": 300, "ts": 1.5}, {"char": 400, "ts": 3.0}]
        quality = score_map(amap)
        self.assertAlmostEqual(quality.backwards_fraction, 0.25)

    def test_zero_when_timestamps_are_monotonic(self):
        amap = [{"char": 0, "ts": 0.0}, {"char": 100, "ts": 1.0}, {"char": 200, "ts": 2.0},
                {"char": 300, "ts": 2.5}, {"char": 400, "ts": 3.0}]
        quality = score_map(amap)
        self.assertEqual(quality.backwards_fraction, 0.0)


class TestExcludeSpans(unittest.TestCase):
    """Mirrors tests/test_ctc_unnarrated_spans.py's
    test_gap_fraction_subtracts_clipped_union_from_gap_and_extent."""

    def test_exclude_spans_removes_excluded_chars_from_gap_computation(self):
        points = [{"char": c, "ts": c / 10} for c in [0, 100, 200, 800, 900, 1000]]
        quality = score_map(points, exclude_spans=[(200, 800)])
        self.assertAlmostEqual(quality.max_gap_fraction, 0.25)

    def test_overlapping_exclude_spans_collapse_to_their_union(self):
        points = [{"char": c, "ts": c / 10} for c in [0, 100, 200, 800, 900, 1000]]
        quality = score_map(points, exclude_spans=[(500, 800), (200, 600), (300, 500)])
        self.assertAlmostEqual(quality.max_gap_fraction, 0.25)


class TestIsRegression(unittest.TestCase):

    def _quality(self, score: float) -> MapQuality:
        return MapQuality(anchors=10, span_chars=1000, max_gap_fraction=0.1,
                           anchor_density=5.0, density_spread=2.0,
                           backwards_fraction=0.0, score=score)

    def test_equal_scores_are_not_a_regression(self):
        # This is the case that protects the CTC upgrade path: a healthy CTC
        # challenger and a healthy lexical incumbent routinely tie on this
        # score (both look structurally perfect), and that tie must NOT be
        # treated as a regression or CTC could never replace lexical again.
        incumbent, challenger = self._quality(0.996), self._quality(0.996)
        self.assertFalse(is_regression(incumbent, challenger))

    def test_challenger_below_margin_is_a_regression(self):
        incumbent, challenger = self._quality(0.90), self._quality(0.87)
        self.assertTrue(is_regression(incumbent, challenger, margin=0.02))

    def test_challenger_within_margin_is_not_a_regression(self):
        incumbent, challenger = self._quality(0.90), self._quality(0.89)
        self.assertFalse(is_regression(incumbent, challenger, margin=0.02))

    def test_challenger_better_than_incumbent_is_never_a_regression(self):
        incumbent, challenger = self._quality(0.80), self._quality(0.95)
        self.assertFalse(is_regression(incumbent, challenger))

    def test_default_margin_is_used_when_omitted(self):
        incumbent, challenger = self._quality(0.90), self._quality(0.87)
        self.assertTrue(is_regression(incumbent, challenger))


if __name__ == "__main__":
    unittest.main()
