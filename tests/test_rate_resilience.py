"""Post-persistence retrain failures must never un-record a judgment.

rate()/rate_candidates() save the rating/round FIRST and retrain taste SECOND.
When _retrain_taste raised, callers (judge_answer especially) saw an error for
a judgment that was already persisted — the ledger verdict never completed and
retries could not heal it. These tests pin the contract: the rating stands,
taste_retrained reports stale instead of raising, and an identical no-key
candidate round replays rather than recording twice.
"""
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from mcp_server import core, taste_bridge
from tests._requires import requires_catalog

NOTES = [{"pitch": 62, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 80},
         {"pitch": 65, "start_beats": 0.5, "duration_beats": 0.5, "velocity": 85},
         {"pitch": 67, "start_beats": 1.0, "duration_beats": 0.5, "velocity": 90}]
STATIC = [{"pitch": 62, "start_beats": j * 0.5, "duration_beats": 0.5,
           "velocity": 80} for j in range(3)]


@requires_catalog
class RateRetrainResilienceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        old_path = core._ratings_path()
        core.set_ratings_path(str(self.root / "ratings.json"))
        self.addCleanup(core.set_ratings_path, old_path)

    def ratings(self) -> dict:
        return core._load_ratings_file()

    def test_gold_rating_survives_retrain_failure(self):
        with mock.patch.object(core, "_retrain_taste",
                               side_effect=RuntimeError("model exploded")):
            res = core.rate(1, label="gold", notes=NOTES, key="D minor")
        self.assertTrue(res["ok"])
        self.assertTrue(res["taste_retrained"]["stale"])
        self.assertEqual(res["taste_retrained"]["error"],
                         "RuntimeError: model exploded")
        ratings = self.ratings()["ratings"]
        self.assertEqual(len(ratings), 1)
        self.assertEqual(ratings[0]["label"], "gold")

    def test_gold_retry_with_key_is_idempotent_after_retrain_failure(self):
        with mock.patch.object(core, "_retrain_taste",
                               side_effect=RuntimeError("model exploded")):
            first = core.rate(1, label="gold", notes=NOTES, key="D minor",
                              idempotency_key="resilience:gold:1")
            second = core.rate(1, label="gold", notes=NOTES, key="D minor",
                               idempotency_key="resilience:gold:1")
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertTrue(second["idempotent"])
        # the replay branch retrains too — its failure also reports stale
        self.assertTrue(second["taste_retrained"]["stale"])
        self.assertEqual(len(self.ratings()["ratings"]), 1)

    def test_identical_no_key_rounds_record_once(self):
        with mock.patch.object(taste_bridge, "_retrain_taste",
                               side_effect=RuntimeError("model exploded")):
            first = core.rate_candidates([NOTES, STATIC], best=0, worst=1,
                                         key="D minor")
            second = core.rate_candidates([NOTES, STATIC], best=0, worst=1,
                                          key="D minor")
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(len(self.ratings()["candidate_rounds"]), 1)
        # no last good model exists → counts are honestly None, not stale zeros
        self.assertIsNone(first["rounds"])
        self.assertIsNone(first["pairs"])
        self.assertIsNone(first["train_accuracy"])
        self.assertTrue(first["taste_retrained"]["stale"])

    def test_retrain_failure_reports_last_good_model_counts(self):
        last_good = {"weights": [0.1] * len(taste_bridge._TASTE_FEATURE_NAMES),
                     "feature_names": list(taste_bridge._TASTE_FEATURE_NAMES),
                     "n_pairs": 4, "n_rounds": 2, "n_gold_pairs": 0,
                     "train_accuracy": 0.75}
        (self.root / "ratings.taste_model.json").write_text(json.dumps(last_good))
        with mock.patch.object(taste_bridge, "_retrain_taste",
                               side_effect=RuntimeError("model exploded")):
            res = core.rate_candidates([NOTES, STATIC], best=0, worst=1,
                                       key="D minor")
        self.assertTrue(res["ok"])
        self.assertEqual(res["rounds"], 2)
        self.assertEqual(res["pairs"], 4)
        self.assertEqual(res["train_accuracy"], 0.75)
        self.assertTrue(res["taste_retrained"]["stale"])
        self.assertIn("RuntimeError", res["taste_retrained"]["error"])

    def test_distinct_no_key_rounds_still_record(self):
        with mock.patch.object(taste_bridge, "_retrain_taste",
                               side_effect=RuntimeError("model exploded")):
            core.rate_candidates([NOTES, STATIC], best=0, worst=1, key="D minor")
            core.rate_candidates([NOTES, STATIC], best=1, worst=0, key="D minor")
        self.assertEqual(len(self.ratings()["candidate_rounds"]), 2)

    def test_replay_reports_the_stored_round_key(self):
        first = core.rate_candidates([NOTES, STATIC], best=0, worst=1,
                                     key="D minor")
        second = core.rate_candidates([NOTES, STATIC], best=0, worst=1,
                                      key="D minor")
        self.assertTrue(second["idempotent"])
        # the replayed round reports the same key identity as a fresh one
        self.assertEqual(second["key"], "D minor")
        self.assertEqual(second["key_source"], "caller")
        self.assertEqual(first["key"], second["key"])
        self.assertEqual(first["key_source"], second["key_source"])
        self.assertEqual(len(self.ratings()["candidate_rounds"]), 1)

    def test_weight_read_failure_after_good_retrain_reports_weight_error(self):
        """A _taste_weight() failure after a successful retrain must not read
        as a failed retrain: the round stands with weight 0.0 plus
        taste_weight_error, and no stale report."""
        with mock.patch.object(taste_bridge, "_taste_weight",
                               side_effect=RuntimeError("weight exploded")):
            res = core.rate_candidates([NOTES, STATIC], best=0, worst=1,
                                       key="D minor")
            replay = core.rate_candidates([NOTES, STATIC], best=0, worst=1,
                                          key="D minor")
        self.assertTrue(res["ok"])
        self.assertEqual(res["taste_weight"], 0.0)
        self.assertIn("taste_weight_error", res)
        self.assertIn("RuntimeError", res["taste_weight_error"])
        self.assertNotIn("taste_retrained", res)
        # the idempotent replay reads the weight through the same guard
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["taste_weight"], 0.0)
        self.assertIn("RuntimeError", replay["taste_weight_error"])
        self.assertNotIn("taste_retrained", replay)

    def test_round_and_replay_survive_unreadable_model_file(self):
        """retrain fails AND the persisted model file is unreadable: the round
        still stands and taste_weight reports 0.0 instead of raising after
        the round was persisted."""
        (self.root / "ratings.taste_model.json").write_text("{corrupt model")
        with mock.patch.object(taste_bridge, "_retrain_taste",
                               side_effect=RuntimeError("model exploded")):
            first = core.rate_candidates([NOTES, STATIC], best=0, worst=1,
                                         key="D minor")
            second = core.rate_candidates([NOTES, STATIC], best=0, worst=1,
                                          key="D minor")
        self.assertTrue(first["ok"])
        self.assertEqual(first["taste_weight"], 0.0)
        self.assertTrue(first["taste_retrained"]["stale"])
        self.assertTrue(second["ok"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["taste_weight"], 0.0)
        self.assertEqual(len(self.ratings()["candidate_rounds"]), 1)


if __name__ == "__main__":
    unittest.main()
