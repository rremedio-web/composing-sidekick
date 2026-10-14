"""B3 item 4: re-roll variety + session memory.

The pick window dedupes by project|clip; seed k walks the deduped ranked
list instead of cycling mod 3; source ids already served in the live ledger
session for a near-identical seed are skipped (unless pinned/kept) and named
in assumptions. Synthetic pools/files throughout.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from mcp_server import core_shared
from mcp_server import core as C
from mcp_server import taste_bridge as TB
from mcp_server import ledger
from build_index import cosine, phrase_vector
from tests._requires import requires_catalog


def _line(shifts, base=60):
    starts = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]
    return [{"pitch": base + s, "start": t, "duration": 0.5, "velocity": 90}
            for s, t in zip(shifts, starts)]


def _rec(pid, notes, project="synth"):
    nd = [dict(n) for n in notes]
    return {"id": pid, "project": project, "track": "t",
            "role": "lead", "clip_source": "clip",
            "kind": "harmonic", "tempo": 120.0,
            "n_notes": len(nd), "notes": nd, "vec": phrase_vector(nd)}


class ClipKeyTests(unittest.TestCase):
    def test_window_suffix_stripped(self):
        self.assertEqual(C._clip_key("storm4|c148|w88"), "storm4|c148")

    def test_plain_ids_kept_whole(self):
        self.assertEqual(C._clip_key("gold:abc123"), "gold:abc123")
        self.assertIsNone(C._clip_key(None))


class DedupeTests(unittest.TestCase):
    def test_one_window_per_clip_keeps_order(self):
        ranked = [_rec("p|c1|w0", _line([0, 2, 4, 5, 7, 9])),
                  _rec("p|c1|w1", _line([0, 2, 4, 5, 7, 9])),
                  _rec("p|c2|w0", _line([0, 1, 2, 3, 4, 5]))]
        out = C._dedupe_ranked_by_clip(ranked)
        self.assertEqual([p["id"] for p in out], ["p|c1|w0", "p|c2|w0"])

    def test_empty_stays_empty(self):
        self.assertEqual(C._dedupe_ranked_by_clip([]), [])


class RerollWalkFixture(unittest.TestCase):
    VARIANTS = ([0, 2, 4, 5, 7, 9], [0, 3, 5, 7, 10, 12],
                [2, 5, 7, 9, 12, 14], [0, 2, 3, 7, 8, 12],
                [5, 7, 9, 12, 14, 16], [1, 4, 6, 8, 11, 13],
                [3, 5, 8, 10, 12, 15])

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        prev = (core_shared._ratings_path()
                if core_shared._RATINGS_PATH_OVERRIDE else None)
        core_shared.set_ratings_path(
            os.path.join(self._td.name, "ratings.json"))
        self.addCleanup(core_shared.set_ratings_path, prev)
        self._prev_pool = TB._PHRASE_POOL_OVERRIDE
        self.addCleanup(setattr, TB, "_PHRASE_POOL_OVERRIDE", self._prev_pool)
        self.seed = _line([0, 2, 4, 7, 9, 12])
        seed_vec = phrase_vector(self.seed)
        pool = []
        for i, shifts in enumerate(self.VARIANTS):
            notes = _line(shifts)
            c = cosine(seed_vec, phrase_vector(notes))
            self.assertLess(
                c, 0.97, "variant %d too near the seed (%0.3f)" % (i, c))
            pool.append(_rec("synth|c%d|w0" % i, notes))
        TB._PHRASE_POOL_OVERRIDE = pool

    def picks(self, seed_notes=None, **kw):
        return [C.answer_my_loop(seed_notes or self.seed, role="lead",
                                 seed=s, use_ears=False, humanize=False,
                                 **kw)["source_phrase_ids"][0]
                for s in range(6)]


@requires_catalog
class WalkTests(RerollWalkFixture):
    def test_seeds_walk_distinct_clips(self):
        """Seeds 0..5 serve distinct clips (today: seed 3 replays seed 0)."""
        ids = self.picks()
        self.assertEqual(len(set(ids)), 6)

    def test_reroll_differs_from_first(self):
        ids = self.picks()
        self.assertNotEqual(ids[3], ids[0])


@requires_catalog
class ServedSkipFixture(RerollWalkFixture):
    def setUp(self):
        super().setUp()
        self.ledger_path = os.path.join(self._td.name, "ledger.json")
        patcher = mock.patch.object(ledger, "LEDGER_PATH", self.ledger_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        # live session serving the seed-0 pick, answered from this seed
        first = C.answer_my_loop(self.seed, role="lead", seed=0,
                                 use_ears=False, humanize=False)
        self.served_id = first["source_phrase_ids"][0]
        now = ledger._now()
        data = {"version": ledger.LEDGER_VERSION, "sessions": [{
            "id": "s-live", "opened_at": ledger._iso(now),
            "last_activity": now, "closed_at": None, "closed_by": None,
            "ended_at": None, "ended_by": None, "rate_status": None,
            "alias_kind": None, "mode": "compose", "set_identity": None,
            "set_aliases": [], "runtime_epochs": []}],
            "entries": [{
                "id": "e-1", "session_id": "s-live",
                "source_phrase_id": self.served_id,
                "answer_notes": first["answer_notes"],
                # seed memory is seed-to-seed: the stamped seed fingerprint,
                # not the (transposed) answer notes
                "seed_notes": ledger._normalize_answer_notes(self.seed),
                "seed_fingerprint": ledger.seed_fingerprint(self.seed),
                "immediate_verdict": None}],
            "adoptions": [], "judgment_events": []}
        with open(self.ledger_path, "w") as handle:
            json.dump(data, handle)

    def test_served_source_skipped_and_named(self):
        ans = C.answer_my_loop(self.seed, role="lead", seed=0,
                               use_ears=False, humanize=False)
        self.assertNotEqual(ans["source_phrase_ids"][0], self.served_id)
        self.assertTrue(any("already served" in a
                            for a in ans["assumptions"]),
                        ans["assumptions"])

    def test_pinned_source_replays(self):
        ans = C._answer_worker(
            self.seed, key=None, tempo=None, bars=None, seed=0,
            humanize=False, transform_fn=None,
            prospective_fn=C._prospective_answer_notes,
            use_ears=False, direction=None, moment=None,
            pin_source=self.served_id, role="lead")
        self.assertEqual(ans["source_phrase_ids"], [self.served_id])

    def test_kept_source_stays_eligible(self):
        with open(self.ledger_path) as handle:
            data = json.load(handle)
        data["entries"][0]["immediate_verdict"] = "keep"
        with open(self.ledger_path, "w") as handle:
            json.dump(data, handle)
        ans = C.answer_my_loop(self.seed, role="lead", seed=0,
                               use_ears=False, humanize=False)
        self.assertEqual(ans["source_phrase_ids"][0], self.served_id)

    def test_different_seed_no_skip(self):
        other = [{"pitch": p, "start": t, "duration": 0.75, "velocity": 90}
                 for p, t in zip([40, 45, 52, 57, 64, 69],
                                 [0.0, 0.75, 1.0, 2.0, 2.5, 3.5])]
        self.assertLess(
            cosine(phrase_vector(self.seed), phrase_vector(other)), 0.9)
        ans = C.answer_my_loop(other, role="lead", seed=0,
                               use_ears=False, humanize=False)
        self.assertFalse(any("already served" in a
                             for a in ans.get("assumptions", [])))


if __name__ == "__main__":
    unittest.main()
