"""B3 item 2: composed golds carry provenance.

KEEP rating records carry entry_id/gear/source; _gold_bank_records labels
index-less golds clip_source='gold_composed'; gears 1/2 disclose that in
source_provenance; the JAM stamp never calls them 'catalogue'. Synthetic
pools/files throughout.
"""
from __future__ import annotations

import json
import unittest
from contextlib import ExitStack
from pathlib import Path
import tempfile
from unittest import mock

from mcp_server import develop_session, ears, gears, ledger
from tests._requires import requires_catalog

NOTES = [
    {"pitch": 36, "start": 0.0, "duration": 0.5, "velocity": 90},
    {"pitch": 36, "start": 0.5, "duration": 0.5, "velocity": 80},
    {"pitch": 43, "start": 1.0, "duration": 1.0, "velocity": 95},
    {"pitch": 41, "start": 2.0, "duration": 1.0, "velocity": 85},
    {"pitch": 36, "start": 3.0, "duration": 1.0, "velocity": 90},
]
IDENTITY = {"set_id": "composed-gold", "runtime_epoch": "run-1",
            "file_path": "/tmp/composed-gold.als"}


class ComposedGoldFixture(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(mock.patch.object(
            ledger, "LEDGER_PATH", str(self.root / "ledger.json")))
        self.stack.enter_context(mock.patch.object(
            ears, "SESSION_PATH", str(self.root / "ears.json")))
        self.stack.enter_context(mock.patch.object(
            develop_session, "STATE_PATH", str(self.root / "develop.json")))
        self.stack.enter_context(mock.patch.object(
            gears.core, "_phrases", return_value=[]))
        old_ratings = gears.core._ratings_path()
        gears.core.set_ratings_path(str(self.root / "ratings.json"))
        self.addCleanup(gears.core.set_ratings_path, old_ratings)

    def write(self, gear=5, source="catalogue"):
        return ledger.record_write(
            clip_name="row", slot="3/0", source=source, gear=gear,
            answer_notes=NOTES, set_identity=IDENTITY,
            clip_ref={"clip_id": "row", "track_id": "jam",
                      "track": "SIDEKICK JAM", "surface": "session",
                      "path": "t3/s0", "answer_notes": NOTES},
            path=str(self.root / "ledger.json"))

    def ratings(self):
        with open(self.root / "ratings.json") as handle:
            return json.load(handle)["ratings"]


class KeepProvenanceTests(ComposedGoldFixture):
    def test_keep_record_carries_entry_gear_source(self):
        entry = self.write(gear=5)
        res = gears.judge_answer("keep", entry_id=entry["id"])
        rec = res["record"]
        self.assertEqual(rec["entry_id"], entry["id"])
        self.assertEqual(rec["gear"], 5)
        self.assertEqual(rec["source"], "catalogue")
        stored = self.ratings()[-1]
        self.assertEqual(stored["entry_id"], entry["id"])
        self.assertEqual(stored["gear"], 5)
        self.assertEqual(stored["source"], "catalogue")


@requires_catalog
class ComposedGoldBankTests(ComposedGoldFixture):
    def _gold_composed(self):
        gears.core.rate(rating=1, label="gold", notes=NOTES, role="bass")

    def test_index_less_gold_labelled_gold_composed(self):
        self._gold_composed()
        bank = gears.core._gold_bank_records(role="bass")
        self.assertEqual(len(bank), 1)
        self.assertEqual(bank[0]["clip_source"], "gold_composed")
        self.assertTrue(bank[0]["_gold"])

    def _near_seed(self):
        """A seed near the gold but below search's 0.97 near-dup cut."""
        from build_index import cosine, phrase_vector
        gold_vec = phrase_vector(NOTES)
        for shift in (2, -2, 3, -3, 4, 5, 1, -1, 7):
            cand = [dict(n, pitch=n["pitch"] + (shift if i % 2 == 0 else 0))
                    for i, n in enumerate(NOTES)]
            if cosine(gold_vec, phrase_vector(cand)) < 0.97:
                return cand
        raise AssertionError("fixture needs a seed below the near-dup cut")

    def test_answer_provenance_discloses_composed(self):
        """Gear 2 serving the composed gold names it in source_provenance."""
        self._gold_composed()
        ans = gears.core.answer_my_loop(
            self._near_seed(), role="bass", seed=0,
            use_ears=False, humanize=False)
        self.assertFalse(ans.get("no_fit"), ans.get("assumptions"))
        prov = ans["source_provenance"]
        self.assertTrue(prov["gold"])
        self.assertEqual(prov["clip_source"], "gold_composed")

    def test_gear1_provenance_discloses_composed(self):
        """Gear 1 (verbatim recall) keeps the composed disclosure too."""
        self._gold_composed()
        ans = gears.answer_with_gear(
            self._near_seed(), gear=1, role="bass", use_ears=False)
        self.assertFalse(ans.get("no_fit"), ans.get("assumptions"))
        self.assertEqual(
            ans["source_provenance"]["clip_source"], "gold_composed")


class StampSourceTests(unittest.TestCase):
    def test_composed_pick_never_stamps_catalogue(self):
        from mcp_server.server import _stamp_source_for_answer
        ans = {"source_provenance": {"clip_source": "gold_composed",
                                     "gold": True}}
        self.assertEqual(
            _stamp_source_for_answer(ans, None, False), "gold_composed")

    def test_plain_pick_stamps_catalogue(self):
        from mcp_server.server import _stamp_source_for_answer
        ans = {"source_provenance": {"clip_source": "clip", "gold": False}}
        self.assertEqual(
            _stamp_source_for_answer(ans, None, False), "catalogue")

    def test_ref_drove_stamps_ref(self):
        from mcp_server.server import _stamp_source_for_answer
        ans = {"source_provenance": {"clip_source": "clip", "gold": False}}
        self.assertEqual(
            _stamp_source_for_answer(ans, "yt-x", True), "ref:yt-x")


if __name__ == "__main__":
    unittest.main()
