"""F10: reject_cull never silently reintroduces what Ricardo dropped.

A small pool must not reverse his verdict to fill itself: exact rejects stay
excluded even when fewer than ``floor`` survivors remain — even when none
remain — and the suppression evidence always rides along in the same
(survivors, suppressed) tuple shape.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from mcp_server import core_shared
from mcp_server.core import rate
from mcp_server.taste_bridge import reject_cull, rejected_phrases
from tests._requires import requires_catalog

NOTES_A = [{"pitch": 60, "start": 0, "duration": 1, "velocity": 80}]
NOTES_B = [{"pitch": 64, "start": 0, "duration": 1, "velocity": 80}]
NOTES_C = [{"pitch": 67, "start": 0, "duration": 1, "velocity": 80}]


@requires_catalog
class TestRejectCullKeepsVerdicts(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._prev = core_shared._ratings_path() if core_shared._RATINGS_PATH_OVERRIDE else None
        core_shared.set_ratings_path(os.path.join(self._td.name, "ratings.json"))

    def tearDown(self):
        core_shared.set_ratings_path(self._prev)
        self._td.cleanup()

    def _drop(self, pid, notes):
        rate(-1, phrase_id=pid, label="dropped", notes=notes)

    def test_exact_reject_excluded_below_floor_with_truthful_metadata(self):
        """The audited repro: 3-pool with one exact DROP serves 2, names 1."""
        self._drop("pA", NOTES_A)
        pool = [{"id": "pA"}, {"id": "pB"}, {"id": "pC"}]
        survivors, suppressed = reject_cull(pool)
        self.assertEqual([p["id"] for p in survivors], ["pB", "pC"])
        self.assertEqual(len(suppressed), 1)
        self.assertEqual(suppressed[0]["id"], "pA")
        self.assertEqual(suppressed[0]["tier"], "dropped_id")

    def test_all_rejected_yields_empty_survivors_not_silent_restore(self):
        self._drop("pA", NOTES_A)
        self._drop("pB", NOTES_B)
        survivors, suppressed = reject_cull([{"id": "pA"}, {"id": "pB"}])
        self.assertEqual(survivors, [])
        self.assertEqual({s["id"] for s in suppressed}, {"pA", "pB"})
        self.assertTrue(all(s["tier"] == "dropped_id" for s in suppressed))

    def test_near_duplicate_suppressed_and_named(self):
        self._drop("orig", NOTES_A)
        vec = next(r["vec"] for r in rejected_phrases() if r["identity"] == "orig")
        self.assertIsNotNone(vec)
        pool = [{"id": "cover", "vec": list(vec)}, {"id": "fresh"}]
        survivors, suppressed = reject_cull(pool)
        self.assertEqual([p["id"] for p in survivors], ["fresh"])
        self.assertEqual(len(suppressed), 1)
        self.assertEqual(suppressed[0]["tier"], "near_dropped")

    def test_no_rejects_passes_pool_through(self):
        pool = [{"id": "pA"}, {"id": "pB"}]
        survivors, suppressed = reject_cull(pool)
        self.assertEqual(survivors, pool)
        self.assertIsNot(survivors, pool)
        self.assertEqual(suppressed, [])

    def test_empty_pool_stays_empty(self):
        self._drop("pA", NOTES_A)
        self.assertEqual(reject_cull([]), ([], []))

    def test_survivors_keep_pool_order_and_cull_is_pure(self):
        self._drop("pB", NOTES_B)
        pool = [{"id": "pA"}, {"id": "pB"}, {"id": "pC"}]
        first = reject_cull(pool)
        second = reject_cull(pool)
        self.assertEqual([p["id"] for p in first[0]], ["pA", "pC"])
        self.assertEqual(first, second)
        self.assertEqual([p["id"] for p in pool], ["pA", "pB", "pC"])


@requires_catalog
class TestExhaustedPoolServingRefusal(unittest.TestCase):
    """Serving level: answer_my_loop refuses no_fit when rejects exhaust the pool.

    Regression for the dead fail-open block: reject_cull now returns [] when
    every candidate was dropped, so the serving path must return the existing
    no_fit shape (not crash on ranked[seed % ...], not serve a reject).
    Uses a controlled bank + retrieval so the real catalog cannot refill the
    pool behind the cull's back.
    """

    def setUp(self):
        from unittest import mock

        from build_index import phrase_vector

        from mcp_server import core as _core

        self._core = _core
        self._td = tempfile.TemporaryDirectory()
        self._prev = core_shared._ratings_path() if core_shared._RATINGS_PATH_OVERRIDE else None
        core_shared.set_ratings_path(os.path.join(self._td.name, "ratings.json"))

        def _ph(pid, pitches, starts, vels):
            n = [{"pitch": p, "start": s, "duration": 0.45, "velocity": v}
                 for p, s, v in zip(pitches, starts, vels)]
            return {"id": pid, "project": "p", "role": "lead",
                    "kind": "harmonic", "clip_source": "session", "tempo": 122.0,
                    "n_notes": len(n), "notes": n, "vec": phrase_vector(n)}

        self._pool = [
            _ph("pA|c0", [62, 65, 69, 67, 65, 64], [0.0, .5, 1.0, 1.5, 2.0, 3.0],
                [91, 63, 77, 84, 58, 96]),
            _ph("pB|c0", [67, 70, 74, 72, 70, 65], [0.0, .5, 1.0, 2.0, 2.5, 3.0],
                [88, 70, 82, 74, 60, 90]),
            _ph("pC|c0", [60, 63, 67, 70, 63, 60], [0.0, .75, 1.5, 2.25, 3.0, 3.5],
                [70, 80, 75, 85, 65, 92]),
        ]
        self._seed = [{"pitch": p, "start": s, "duration": 0.45, "velocity": 90}
                      for p, s in zip([62, 65, 69, 67, 65, 62],
                                      [0.0, .5, 1.0, 1.5, 2.0, 3.0])]
        _core.set_phrase_pool_for_test(list(self._pool))
        self._bank_patch = mock.patch.object(
            _core, "_answer_bank", return_value=(list(self._pool), 0, 0))
        self._search_patch = mock.patch.object(
            _core, "search", return_value=list(self._pool))
        self._bank_patch.start()
        self._search_patch.start()

    def tearDown(self):
        self._search_patch.stop()
        self._bank_patch.stop()
        self._core.set_phrase_pool_for_test(None)
        core_shared.set_ratings_path(self._prev)
        self._td.cleanup()

    def _drop_all(self):
        for p in self._pool:
            rate(-1, phrase_id=p["id"], label="dropped", notes=p["notes"])

    def _ratings_hash(self):
        p = os.path.join(self._td.name, "ratings.json")
        with open(p, "rb") as fh:
            return fh.read()

    def test_all_rejected_serves_no_fit_ears_off(self):
        self._drop_all()
        before = self._ratings_hash()
        r = self._core.answer_my_loop(self._seed, key="D minor", seed=0,
                                      use_ears=False)
        self.assertTrue(r.get("no_fit"), r)
        self.assertEqual(r["answer_notes"], [])
        self.assertEqual(r["role"], "lead")
        self.assertEqual(r["key"], "D minor")
        self.assertIn("bars", r)
        self.assertEqual(r["rejects"]["pool_after"], 0)
        self.assertEqual(r["rejects"]["pool_before"], len(self._pool))
        self.assertEqual(r["bank_stats"]["n_rejected"], len(self._pool))
        self.assertFalse(any("serving anyway" in a for a in r["assumptions"]))
        self.assertTrue(any("dropped" in a for a in r["assumptions"]), r["assumptions"])
        # the serving path itself writes nothing (ratings only change via rate())
        self.assertEqual(self._ratings_hash(), before)

    def test_all_rejected_no_fit_with_ears_and_ref(self):
        self._drop_all()
        r = self._core.answer_my_loop(self._seed, key="D minor", seed=0,
                                      use_ears=True, ref={"tempo": 122.0})
        self.assertTrue(r.get("no_fit"), r)
        self.assertEqual(r["answer_notes"], [])
        self.assertEqual(r["rejects"]["pool_after"], 0)

    def test_reduced_pool_serves_allowed_candidate(self):
        for p in self._pool[:2]:
            rate(-1, phrase_id=p["id"], label="dropped", notes=p["notes"])
        r = self._core.answer_my_loop(self._seed, key="D minor", seed=0,
                                      use_ears=False)
        self.assertNotIn("no_fit", r, r)
        self.assertEqual(r["source_phrase_ids"], ["pC|c0"])
        self.assertTrue(r["answer_notes"])
        self.assertEqual(len(r["rejects"]["suppressed"]), 2)


if __name__ == "__main__":
    unittest.main()
