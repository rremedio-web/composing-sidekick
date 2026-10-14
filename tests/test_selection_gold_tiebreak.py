"""B3 item 1: gold is a tie-breaker, not an attractor.

The +0.05 gold bonus applies only when the gold's cosine is within 0.02 of
the rank-1 cosine (or >= 0.9 absolute); at most one gold sits in a pick
window. Synthetic pools throughout: no Live, no real store.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from mcp_server import core_shared
from tests._requires import requires_catalog
from mcp_server.core import (
    _GOLD_BONUS,
    _GOLD_TIEBREAK_ABSOLUTE,
    _GOLD_TIEBREAK_WINDOW,
    _rank_with_gold_tiebreak,
    _single_gold_window,
)


def _hit(cid, cos, gold=False):
    return {"id": cid, "cos": cos, "_gold": gold, "vec": [cos]}


class TestGoldTiebreakConstants(unittest.TestCase):
    def test_window_and_floor(self):
        self.assertEqual(_GOLD_TIEBREAK_WINDOW, 0.02)
        self.assertEqual(_GOLD_TIEBREAK_ABSOLUTE, 0.9)
        self.assertEqual(_GOLD_BONUS, 0.05)


class TestRankWithGoldTiebreak(unittest.TestCase):
    def test_far_gold_gets_no_bonus(self):
        """Gold 0.03 below rank-1 (and under 0.9) must not jump the queue."""
        hits = [_hit("p0", 0.88), _hit("g1", 0.85, gold=True),
                _hit("p1", 0.80)]
        ranked = _rank_with_gold_tiebreak(hits, seed_vec=[1.0])
        self.assertEqual([p["id"] for p in ranked], ["p0", "g1", "p1"])

    def test_near_gold_wins_the_tie(self):
        """Gold within 0.02 of rank-1 keeps the old +0.05 lift."""
        hits = [_hit("p0", 0.88), _hit("g1", 0.865, gold=True),
                _hit("p1", 0.80)]
        ranked = _rank_with_gold_tiebreak(hits, seed_vec=[1.0])
        self.assertEqual(ranked[0]["id"], "g1")

    def test_strong_gold_absolute_floor(self):
        """A gold at >= 0.9 absolute earns the bonus even past the window."""
        hits = [_hit("p0", 0.94), _hit("g1", 0.91, gold=True),
                _hit("p1", 0.90)]
        ranked = _rank_with_gold_tiebreak(hits, seed_vec=[1.0])
        self.assertEqual(ranked[0]["id"], "g1")

    def test_non_gold_order_untouched(self):
        hits = [_hit("a", 0.5), _hit("b", 0.7), _hit("c", 0.6)]
        ranked = _rank_with_gold_tiebreak(hits, seed_vec=[1.0])
        self.assertEqual([p["id"] for p in ranked], ["b", "c", "a"])

    def test_exact_tie_breaks_to_gold(self):
        """An exact cosine tie is what the tie-breaker is for: gold first."""
        hits = [_hit("p0", 0.88), _hit("g1", 0.88, gold=True)]
        ranked = _rank_with_gold_tiebreak(hits, seed_vec=[1.0])
        self.assertEqual([p["id"] for p in ranked], ["g1", "p0"])


class TestSingleGoldWindow(unittest.TestCase):
    def test_second_gold_slides_below_window(self):
        ranked = [_hit("g1", 0.99, gold=True), _hit("p0", 0.95),
                  _hit("g2", 0.94, gold=True), _hit("p1", 0.90)]
        out = _single_gold_window(ranked, 3)
        self.assertEqual([p["id"] for p in out[:3]], ["g1", "p0", "p1"])
        self.assertEqual(out[3]["id"], "g2")

    def test_single_gold_window_untouched(self):
        ranked = [_hit("g1", 0.99, gold=True), _hit("p0", 0.95)]
        self.assertEqual(_single_gold_window(ranked, 3), ranked)

    def test_empty_and_small(self):
        self.assertEqual(_single_gold_window([], 3), [])
        ranked = [_hit("g1", 0.9, gold=True)]
        self.assertEqual(_single_gold_window(ranked, 3), ranked)


@requires_catalog
class TestGoldTiebreakEndToEnd(unittest.TestCase):
    """A gold 0.03 behind rank-1 must not take the pick slot anymore."""

    def setUp(self):
        from mcp_server import core as C
        from mcp_server import taste_bridge as TB
        from build_index import cosine, phrase_vector
        self.C = C
        self._tb = TB
        self._cosine = cosine
        self._phrase_vector = phrase_vector
        self._td = tempfile.TemporaryDirectory()
        self._prev_ratings = (
            core_shared._ratings_path()
            if core_shared._RATINGS_PATH_OVERRIDE else None)
        core_shared.set_ratings_path(
            os.path.join(self._td.name, "ratings.json"))
        self._prev_pool = TB._PHRASE_POOL_OVERRIDE
        # seed: 8-note bass line across two bars
        self.seed_notes = [
            {"pitch": 33 + s, "start": float(s) * 0.5,
             "duration": 0.5, "velocity": 90}
            for s in (0, 0, 3, 5, 7, 5, 3, 2, 0, 0, 5, 7)][:8]
        seed_vec = phrase_vector(
            [{"pitch": n["pitch"], "start": n["start"],
              "duration": n["duration"], "velocity": n["velocity"]}
             for n in self.seed_notes])
        # rank-1 candidate: small rhythmic displacement (cos ~0.9)
        p0_notes = [dict(n, start=n["start"] + (0.25 if i % 3 == 0 else 0.0))
                    for i, n in enumerate(self.seed_notes)]
        p0_cos = cosine(seed_vec, phrase_vector(p0_notes))
        # gold candidate: search deterministic variants for a cosine in the
        # (0.02, 0.05] gap band below rank-1 and under 0.9 absolute
        gold_notes = None
        for shift in (1, 2, 4, 5, 6, -1, -2, 3, 7, -3):
            cand = [dict(n, pitch=n["pitch"] + (shift if i % 2 == 0 else 0))
                    for i, n in enumerate(self.seed_notes)]
            c = cosine(seed_vec, phrase_vector(cand))
            gap = p0_cos - c
            if 0.02 < gap <= 0.05 and c < 0.9:
                gold_notes = cand
                break
        self.assertIsNotNone(
            gold_notes,
            "fixture needs a gold in the (0.02, 0.05] gap band (p0=%0.3f)" % p0_cos)
        self.gold_cos = cosine(seed_vec, phrase_vector(gold_notes))
        self.p0_cos = p0_cos

        def rec(pid, notes, role="bass"):
            nd = [{"pitch": n["pitch"], "start": n["start"],
                   "duration": n["duration"], "velocity": n["velocity"]}
                  for n in notes]
            return {"id": pid, "project": "synth", "track": "t",
                    "role": role, "clip_source": "clip",
                    "kind": "harmonic", "tempo": 120.0,
                    "n_notes": len(nd), "notes": nd,
                    "vec": phrase_vector(nd)}
        TB._PHRASE_POOL_OVERRIDE = [
            rec("p0", p0_notes), rec("g1", gold_notes),
            rec("p1", [dict(n, pitch=n["pitch"] + 12)
                       for n in self.seed_notes]),
        ]
        C.rate(rating=1, label="gold", phrase_id="g1",
               notes=gold_notes, role="bass")

    def tearDown(self):
        self._tb._PHRASE_POOL_OVERRIDE = self._prev_pool
        core_shared.set_ratings_path(self._prev_ratings)
        self._td.cleanup()

    def test_pick_is_rank1_not_far_gold(self):
        ans = self.C.answer_my_loop(
            self.seed_notes, role="bass", seed=0,
            use_ears=False, humanize=False)
        self.assertFalse(ans.get("no_fit"), ans.get("assumptions"))
        self.assertEqual(ans["source_phrase_ids"], ["p0"])
        self.assertFalse(ans["source_provenance"]["gold"])


if __name__ == "__main__":
    unittest.main()
