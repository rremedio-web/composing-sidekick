"""B3 item 6: session survival feeds selection.

ledger.outcome_rating_ops(entry) maps a definitively closed row to rating
ops — keep+absent un-golds, abandoned demotes soft (back, not banned),
keeps/drops/unjudged need nothing (judge KEEP golded already, incl. adopted
keeps). Ops apply once per entry under idempotency key 'outcome:<entry_id>'.
A later 'dropped' label un-golds. Synthetic entries/files throughout.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from mcp_server import core_shared
from mcp_server import core as C
from mcp_server import taste_bridge as TB
from mcp_server import ledger
from build_index import phrase_vector
from tests._requires import requires_catalog

NOTES = [
    {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90},
    {"pitch": 64, "start": 0.5, "duration": 0.5, "velocity": 85},
    {"pitch": 67, "start": 1.0, "duration": 1.0, "velocity": 90},
    {"pitch": 72, "start": 2.0, "duration": 2.0, "velocity": 88},
]
OTHER = [
    {"pitch": 45, "start": 0.0, "duration": 1.0, "velocity": 90},
    {"pitch": 52, "start": 1.0, "duration": 1.0, "velocity": 85},
    {"pitch": 50, "start": 2.0, "duration": 1.0, "velocity": 90},
    {"pitch": 48, "start": 3.0, "duration": 1.0, "velocity": 88},
]


def _entry(eid="e-1", verdict=None, state=None, pid="src|x|w0",
           notes=None):
    return {"id": eid, "session_id": "s-1", "immediate_verdict": verdict,
            "terminal_state": state, "source_phrase_id": pid,
            "answer_notes": notes if notes is not None else NOTES,
            "role": "lead", "gear": 2, "source": "catalogue"}


class OutcomeOpsTests(unittest.TestCase):
    def test_keep_absent_ungolds(self):
        ops = ledger.outcome_rating_ops(
            _entry(verdict="keep", state="drop"))
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["action"], "ungold")
        self.assertEqual(ops[0]["phrase_id"], "src|x|w0")

    def test_abandoned_demotes_soft(self):
        ops = ledger.outcome_rating_ops(_entry(state="abandoned"))
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["action"], "demote")

    def test_keep_drop_unjudged_need_nothing(self):
        self.assertEqual(
            ledger.outcome_rating_ops(_entry(verdict="keep", state="keep")), [])
        self.assertEqual(
            ledger.outcome_rating_ops(_entry(verdict="drop", state="drop")), [])
        self.assertEqual(ledger.outcome_rating_ops(_entry(state="unjudged")), [])
        self.assertEqual(ledger.outcome_rating_ops(_entry()), [])

    def test_no_identity_no_op(self):
        entry = _entry(verdict="keep", state="drop")
        entry["source_phrase_id"] = None
        entry["answer_notes"] = None
        self.assertEqual(ledger.outcome_rating_ops(entry), [])

    def test_pure_no_reads_no_writes(self):
        entry = _entry(verdict="keep", state="drop")
        before = dict(entry)
        ledger.outcome_rating_ops(entry)
        self.assertEqual(entry, before)


@requires_catalog
class ApplyOnceTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        prev = (core_shared._ratings_path()
                if core_shared._RATINGS_PATH_OVERRIDE else None)
        core_shared.set_ratings_path(
            Path(self._td.name) / "ratings.json")
        self.addCleanup(core_shared.set_ratings_path, prev)
        C.rate(rating=1, label="gold", phrase_id="src|x|w0", notes=NOTES)
        self.assertTrue(any(g.get("phrase_id") == "src|x|w0"
                            for g in C.get_gold_phrases()))

    def _apply(self, entry):
        from mcp_server import server as _server
        return _server._apply_outcome_rating_ops(entry)

    def test_keep_absent_applies_ungold_once(self):
        entry = _entry(verdict="keep", state="drop")
        first = self._apply(entry)
        self.assertEqual(len(first), 1)
        self.assertFalse(any(g.get("phrase_id") == "src|x|w0"
                             for g in C.get_gold_phrases()))
        second = self._apply(entry)
        self.assertEqual(len(second), 1)
        self.assertTrue(second[0].get("idempotent"))
        import json
        with open(Path(self._td.name) / "ratings.json") as handle:
            ratings = json.load(handle)["ratings"]
        self.assertEqual(
            sum(1 for r in ratings
                if r.get("idempotency_key") == "outcome:e-1"), 1)

    def test_abandoned_applies_demote(self):
        entry = _entry(state="abandoned")
        applied = self._apply(entry)
        self.assertEqual(len(applied), 1)
        import json
        with open(Path(self._td.name) / "ratings.json") as handle:
            ratings = json.load(handle)["ratings"]
        demotes = [r for r in ratings
                   if r.get("label") == "served-not-kept"]
        self.assertEqual(len(demotes), 1)
        self.assertEqual(demotes[0]["rating"], 0)
        # soft: not a reject, still servable
        self.assertEqual(TB.rejected_phrases(), [])


@requires_catalog
class DroppedUngoldsTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        prev = (core_shared._ratings_path()
                if core_shared._RATINGS_PATH_OVERRIDE else None)
        core_shared.set_ratings_path(
            Path(self._td.name) / "ratings.json")
        self.addCleanup(core_shared.set_ratings_path, prev)

    def test_later_dropped_label_ungolds(self):
        C.rate(rating=1, label="gold", phrase_id="g1", notes=NOTES)
        self.assertEqual(len(C.get_gold_phrases()), 1)
        C.rate(rating=-1, label="dropped", phrase_id="g1", notes=NOTES)
        self.assertEqual(C.get_gold_phrases(), [])

    def test_plain_drop_without_gold_changes_nothing(self):
        C.rate(rating=-1, label="dropped", phrase_id="g1", notes=NOTES)
        self.assertEqual(C.get_gold_phrases(), [])
        self.assertEqual(len(TB.rejected_phrases()), 1)


@requires_catalog
class DemoteConsumerTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        prev = (core_shared._ratings_path()
                if core_shared._RATINGS_PATH_OVERRIDE else None)
        core_shared.set_ratings_path(
            Path(self._td.name) / "ratings.json")
        self.addCleanup(core_shared.set_ratings_path, prev)
        self._prev_pool = TB._PHRASE_POOL_OVERRIDE
        self.addCleanup(setattr, TB, "_PHRASE_POOL_OVERRIDE", self._prev_pool)

        def rec(pid, notes):
            nd = [dict(n) for n in notes]
            return {"id": pid, "project": "synth", "track": "t",
                    "role": "lead", "clip_source": "clip",
                    "kind": "harmonic", "tempo": 120.0,
                    "n_notes": len(nd), "notes": nd,
                    "vec": phrase_vector(nd)}
        TB._PHRASE_POOL_OVERRIDE = [rec("top|x|w0", NOTES),
                                    rec("second|x|w0", OTHER)]
        # near seed: under the 0.97 near-dup cut yet near enough to rank top
        from build_index import cosine as _cos
        top_vec = phrase_vector(NOTES)
        self.seed = None
        for tweak in ((1, 1), (3, -1), (1, 1, 3, -1), (0, 2), (2, 1)):
            cand = [dict(n) for n in NOTES]
            for i, delta in zip(tweak[::2], tweak[1::2]):
                cand[i]["pitch"] += delta
            if 0.90 <= _cos(top_vec, phrase_vector(cand)) < 0.96:
                self.seed = cand
                break
        self.assertIsNotNone(self.seed, "fixture needs a near seed")
        first = C.answer_my_loop(self.seed, role="lead", seed=0,
                                 use_ears=False, humanize=False)
        self.assertEqual(first["source_phrase_ids"], ["top|x|w0"])

    def test_demote_moves_source_to_back_not_out(self):
        C.rate(rating=0, label="served-not-kept",
               phrase_id="top|x|w0", notes=NOTES)
        ans = C.answer_my_loop(self.seed, role="lead", seed=0,
                               use_ears=False, humanize=False)
        # the demoted source yields the slot ...
        self.assertEqual(ans["source_phrase_ids"], ["second|x|w0"])
        # ... but is NOT banned: seed 1 walks the list and still finds it
        again = C.answer_my_loop(self.seed, role="lead", seed=1,
                                 use_ears=False, humanize=False)
        self.assertEqual(again["source_phrase_ids"], ["top|x|w0"])
        survivors, _ = TB.reject_cull(
            [p for p in TB._phrases() if p["id"] == "top|x|w0"])
        self.assertEqual([p["id"] for p in survivors], ["top|x|w0"])


if __name__ == "__main__":
    unittest.main()
