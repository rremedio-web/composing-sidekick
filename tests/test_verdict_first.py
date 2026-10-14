"""Verdict first: the ledger row lands before any learning sink can fail."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from mcp_server import develop_session, ears, gears, ledger

NOTES = [{"pitch": 60, "start": 0, "duration": 1, "velocity": 80}]
IDENTITY = {"set_id": "verdict-first", "runtime_epoch": "run-1",
            "file_path": "/tmp/verdict-first.als"}


class VerdictFirstFixture(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.ledger_path = str(self.root / "ledger.json")
        self.stack.enter_context(mock.patch.object(ledger, "LEDGER_PATH",
                                                   self.ledger_path))
        self.stack.enter_context(mock.patch.object(
            ears, "SESSION_PATH", str(self.root / "ears.json")))
        self.stack.enter_context(mock.patch.object(
            develop_session, "STATE_PATH", str(self.root / "develop.json")))
        self.stack.enter_context(mock.patch.object(gears.core, "_phrases",
                                                   return_value=[]))
        old_ratings = gears.core._ratings_path()
        gears.core.set_ratings_path(str(self.root / "ratings.json"))
        self.addCleanup(gears.core.set_ratings_path, old_ratings)

    def write(self, name="row"):
        return ledger.record_write(
            clip_name=name, slot="3/0", source="catalogue",
            answer_notes=NOTES, set_identity=IDENTITY,
            clip_ref={"clip_id": name, "track_id": "jam",
                      "track": "SIDEKICK JAM", "surface": "session",
                      "path": "t3/s0", "answer_notes": NOTES},
            path=self.ledger_path)

    def row(self, entry_id):
        return next(e for e in ledger.load(self.ledger_path)["entries"]
                    if e["id"] == entry_id)

    def ratings(self):
        with open(self.root / "ratings.json") as handle:
            return json.load(handle)["ratings"]


class CanonicalRoleTests(VerdictFirstFixture):
    def test_drum_synonyms_and_absent_roles_fold(self):
        for given, want in (("percussion", "drum"), ("perc", "drum"),
                            ("Drums", "drum"), ("Bass", "bass"),
                            (None, "unknown"), ("", "unknown"), ("auto", "unknown")):
            entry = ledger.record_write(clip_name="r-%s" % given, slot="3/%d" %
                                        (len(ledger.load(self.ledger_path)["entries"]),),
                                        source="catalogue", role=given,
                                        path=self.ledger_path)
            self.assertEqual(entry["role"], want, given)


class LearningRoleTests(VerdictFirstFixture):
    """fix3 item 3 + fix4 R1: the stored 'unknown' placeholder is
    bookkeeping, never a role the taste model should trust as
    authoritative — and never a contradiction against a supplied role."""

    def _write_with_role(self, name, stored_role):
        return ledger.record_write(
            clip_name=name, slot="3/0", source="catalogue",
            role=stored_role, answer_notes=NOTES, set_identity=IDENTITY,
            clip_ref={"clip_id": name, "track_id": "jam",
                      "track": "SIDEKICK JAM", "surface": "session",
                      "path": "t3/s0", "answer_notes": NOTES},
            path=self.ledger_path)

    def test_unknown_placeholder_never_reaches_learning(self):
        from mcp_server import server

        entry = self._write_with_role("unclassified", None)
        self.assertEqual(entry["role"], "unknown")
        res = server.judge_answer("keep", entry_id=entry["id"])
        # None lets core.rate infer from register instead of trusting the
        # placeholder as a stored role
        self.assertIsNone(res["record"]["role"])

    def test_supplied_role_beats_an_unknown_stored_role(self):
        from mcp_server import server

        entry = self._write_with_role("unclassified", None)
        self.assertEqual(entry["role"], "unknown")
        res = server.judge_answer("keep", entry_id=entry["id"], role="lead")
        self.assertNotIn("learning_failure", res)
        self.assertEqual(res["record"]["role"], "lead")

    def test_supplied_drum_synonym_matches_stored_drum(self):
        from mcp_server import server

        entry = self.write()
        data = ledger.load(self.ledger_path)
        next(e for e in data["entries"] if e["id"] == entry["id"])["role"] = "drum"
        with open(self.ledger_path, "w") as handle:
            json.dump(data, handle)
        res = server.judge_answer("keep", entry_id=entry["id"], role="drums")
        self.assertEqual(res["record"]["role"], "drum")

    def test_legacy_noncanonical_stored_role(self):
        """A legacy row stored 'percussion' before canonicalization: a
        supplied 'drums' is the same role, and no supplied role learns the
        canonical 'drum' — neither may become a contradiction."""
        from mcp_server import server

        entry = self._write_with_role("legacy", "percussion")
        data = ledger.load(self.ledger_path)
        next(e for e in data["entries"] if e["id"] == entry["id"])["role"] = "percussion"
        with open(self.ledger_path, "w") as handle:
            json.dump(data, handle)
        supplied = server.judge_answer("keep", entry_id=entry["id"], role="drums")
        self.assertNotIn("learning_failure", supplied)
        self.assertEqual(supplied["record"]["role"], "drum")
        # ...and a retry with no role at all replays cleanly
        again = server.judge_answer("keep", entry_id=entry["id"])
        self.assertNotIn("learning_failure", again)
        self.assertEqual(again["record"]["role"], "drum")


class VerdictBeforeLearningTests(VerdictFirstFixture):
    def test_retrain_failure_keeps_verdict_and_marks_rating_stale(self):
        from mcp_server import server

        entry = self.write()
        with mock.patch.object(gears.core, "_retrain_taste",
                               side_effect=RuntimeError("model exploded")):
            res = server.judge_answer("keep", entry_id=entry["id"])
        self.assertEqual(res["verdict"], "keep")
        self.assertEqual(res["ledger"]["entry_id"], entry["id"])
        self.assertEqual(self.row(entry["id"])["immediate_verdict"], "keep")
        self.assertEqual(len(self.ratings()), 1)
        self.assertEqual(self.ratings()[0]["label"], "gold")
        # the rating persisted; the model is reported stale, not retrained
        self.assertTrue(res["taste_retrained"]["stale"])
        self.assertIn("model exploded", res["taste_retrained"]["error"])

    def test_ears_failure_leaves_verdict_with_learning_failure(self):
        from mcp_server import server

        entry = self.write()
        with mock.patch.object(ears, "record_verdict",
                               side_effect=OSError("ears session unreadable")):
            res = server.judge_answer("keep", entry_id=entry["id"])
        self.assertEqual(res["verdict"], "keep")
        failure = res["learning_failure"]
        self.assertEqual(failure["stage"], "ears")
        self.assertIn("ears session unreadable", failure["error"])
        self.assertIn(entry["id"], failure["retry"])
        # the verdict stands and no rating landed from the failed pass
        self.assertEqual(self.row(entry["id"])["immediate_verdict"], "keep")
        self.assertFalse((self.root / "ratings.json").exists())

    def test_retry_after_learning_failure_completes_the_journal(self):
        from mcp_server import server

        entry = self.write()
        with mock.patch.object(ears, "record_verdict",
                               side_effect=OSError("ears session unreadable")):
            first = server.judge_answer("drop", entry_id=entry["id"])
        self.assertIn("learning_failure", first)
        event = ledger.judgment_event("judge:%s:drop" % entry["id"],
                                      path=self.ledger_path)
        # the failed step is journaled so the retry resumes instead of redoing
        self.assertEqual(event["steps"]["ears"]["status"], "failed")
        second = server.judge_answer("drop", entry_id=entry["id"])
        self.assertNotIn("learning_failure", second)
        self.assertEqual(second["verdict"], "drop")
        event = ledger.judgment_event("judge:%s:drop" % entry["id"],
                                      path=self.ledger_path)
        self.assertEqual(event["status"], "complete")
        self.assertEqual(len(self.ratings()), 1)
        self.assertEqual(self.row(entry["id"])["immediate_verdict"], "drop")


class LearningFailureGatingTests(VerdictFirstFixture):
    """fix3 item 1: 'the ledger verdict stands' is only honest when one landed."""

    def test_unbound_judgment_error_is_not_a_learning_failure(self):
        """judge_answer(verdict='drop', slot=…) with nothing bound must
        surface the input error, not claim a verdict that never landed."""
        from mcp_server import server

        self.write()
        with self.assertRaisesRegex(ValueError, "need source_phrase_id"):
            server.judge_answer("drop", slot="9/9")
        # nothing was judged, nothing learned
        rows = ledger.load(self.ledger_path)["entries"]
        self.assertTrue(all(row["immediate_verdict"] is None for row in rows))
        self.assertFalse((self.root / "ratings.json").exists())

    def test_learning_failure_requires_a_landed_verdict(self):
        """The pre-verdict stamp failed AND learning failed: no verdict
        exists, so the learning error re-raises instead of degrading."""
        from mcp_server import server

        entry = self.write()
        with mock.patch.object(ledger, "record_verdict",
                               side_effect=RuntimeError("ledger down")), \
                mock.patch.object(gears, "judge_answer",
                                  side_effect=ValueError("sink boom")):
            with self.assertRaisesRegex(ValueError, "sink boom"):
                server.judge_answer("keep", entry_id=entry["id"])
        self.assertIsNone(self.row(entry["id"])["immediate_verdict"])
        self.assertFalse((self.root / "ratings.json").exists())

    def test_already_judged_replay_sink_failure_is_a_learning_failure(self):
        """A retry of an already-judged row whose sink still fails: the
        verdict landed on the first pass, so learning_failure is honest."""
        from mcp_server import server

        entry = self.write()
        with mock.patch.object(ears, "record_verdict",
                               side_effect=OSError("ears unreadable")):
            first = server.judge_answer("keep", entry_id=entry["id"])
            self.assertIn("learning_failure", first)
            retry = server.judge_answer("keep", entry_id=entry["id"])
        self.assertEqual(retry["verdict"], "keep")
        self.assertEqual(retry["learning_failure"]["stage"], "ears")
        self.assertEqual(self.row(entry["id"])["immediate_verdict"], "keep")
        self.assertFalse((self.root / "ratings.json").exists())

    def test_no_material_judgment_skips_learning_with_a_note(self):
        """fix4 nit: a row with no answer notes and no resolvable source
        phrase has nothing to learn from — the verdict lands and learning
        is skipped with a clear note, not a retry-forever failure."""
        from mcp_server import server

        entry = ledger.record_write(
            clip_name="no-material", slot="3/0", source="catalogue",
            set_identity=IDENTITY,
            clip_ref={"clip_id": "no-material", "track_id": "jam",
                      "track": "SIDEKICK JAM", "surface": "session",
                      "path": "t3/s0"},
            path=self.ledger_path)
        res = server.judge_answer("keep", entry_id=entry["id"])
        self.assertNotIn("learning_failure", res)
        self.assertIn("nothing to learn", res["note"])
        self.assertEqual(res["ledger"]["entry_id"], entry["id"])
        self.assertEqual(self.row(entry["id"])["immediate_verdict"], "keep")
        self.assertFalse((self.root / "ratings.json").exists())
        # a retry is the same verdict-only judgment, not a failure loop
        retry = server.judge_answer("keep", entry_id=entry["id"])
        self.assertNotIn("learning_failure", retry)
        self.assertIn("nothing to learn", retry["note"])

    def test_late_binding_surfaces_in_the_result(self):
        """fix3 item 7 + fix4 R3: a verdict on an already-SEALED night
        scores late and says so; an idle-but-unswept night scores normally
        and says nothing (repro: repro2/p2.py case b)."""
        from mcp_server import server

        # a night that went idle hours ago, everything real-time from here
        t0 = time.time() - ledger.SESSION_IDLE_SECONDS - 600
        with mock.patch.object(ledger, "_now", return_value=t0):
            first = ledger.record_write(
                clip_name="idle-night", slot="3/0", source="catalogue",
                answer_notes=[{**NOTES[0], "pitch": 62}], set_identity=IDENTITY,
                clip_ref={"clip_id": "idle", "track_id": "jam",
                          "track": "SIDEKICK JAM", "surface": "session",
                          "path": "t3/s0",
                          "answer_notes": [{**NOTES[0], "pitch": 62}]},
                path=self.ledger_path)
            second = ledger.record_write(
                clip_name="sealed-night", slot="3/1", source="catalogue",
                answer_notes=NOTES, set_identity=IDENTITY,
                clip_ref={"clip_id": "sealed", "track_id": "jam",
                          "track": "SIDEKICK JAM", "surface": "session",
                          "path": "t3/s1", "answer_notes": NOTES},
                path=self.ledger_path)

        idle = server.judge_answer("drop", entry_id=first["id"])
        self.assertNotIn("late", idle)
        # ...and it scores normally: no late_verdicts at the close
        report = ledger.close_session(
            {"set_identity": IDENTITY, "complete": True, "errors": [],
             "clips": [dict(first["clip_ref"], name="idle")]},
            path=self.ledger_path)
        self.assertEqual(report["counts"]["drop"], 1)
        self.assertEqual(report["late_verdicts"]["counts"], {})

        # the sealed night, by contrast, is late and says so
        sealed = server.judge_answer("drop", entry_id=second["id"])
        self.assertTrue(sealed["late"])


if __name__ == "__main__":
    unittest.main()
