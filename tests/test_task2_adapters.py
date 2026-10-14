from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from mcp_server import ears, gears, jam_lifecycle, ledger


NOTES = [{"pitch": 60, "start_beats": 0, "duration_beats": 1, "velocity": 80}]
IDENTITY = {"set_id": "set-a", "runtime_epoch": "run-a", "file_path": "/tmp/a.als"}
REF = {"clip_id": "clip-a", "track_id": "jam-a", "track": "SIDEKICK JAM",
       "surface": "session", "path": "t3/s0", "answer_notes": NOTES}


class Task2AdapterTests(unittest.TestCase):
    def test_verified_receipt_is_accounted_and_revision_keeps_one_row(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "ledger.json")
            receipt = {"slot": "3/0", "set_identity": IDENTITY, "clip_ref": REF}
            target = {}
            first = jam_lifecycle.stamp_verified_write(
                target, clip_name="idea", slot="3/0", source="catalogue",
                role="lead", gear=2, notes=NOTES, live_receipt=receipt,
                ledger_path=path)
            self.assertEqual(first["clip_ref"]["clip_id"], "clip-a")
            self.assertTrue(target["scoreable"])
            self.assertNotIn("scoreable_reason", target)
            revised_ref = {**REF, "clip_id": "clip-b", "answer_notes": NOTES}
            revised = ledger.record_write(
                clip_name="idea", slot="3/0", source="catalogue", role="lead", gear=2,
                answer_notes=NOTES, set_identity=IDENTITY, clip_ref=revised_ref,
                revision_of=first["id"], path=path)
            self.assertEqual(revised["id"], first["id"])
            self.assertEqual(len(ledger.load(path)["entries"]), 1)

    def test_identity_error_receipt_stamps_unscoreable_write(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "ledger.json")
            receipt = {"slot": "3/0", "set_identity": IDENTITY, "clip_ref": REF,
                       "identity_status": "identity_error"}
            target = {}
            entry = jam_lifecycle.stamp_verified_write(
                target, clip_name="idea", slot="3/0", source="catalogue",
                role="lead", gear=2, notes=NOTES, live_receipt=receipt,
                ledger_path=path)
            # the entry is still recorded — only its scorability is flagged
            self.assertIsNotNone(entry)
            self.assertIn("ledger", target)
            self.assertFalse(target["scoreable"])
            self.assertIn("Producer Pal 2.2", target["scoreable_reason"])
            self.assertEqual(len(ledger.load(path)["entries"]), 1)

    def test_unscoreable_reason_names_the_actual_cause(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "ledger.json")
            readback_cases = (
                # identity read OK, but no verified clip_ref
                {"slot": "3/0", "set_identity": IDENTITY},
                # identity read OK, clip readback unverified
                {"slot": "3/0", "set_identity": IDENTITY, "clip_ref": REF,
                 "identity_status": "accounting_error"},
                # accounting_error stays a readback problem even on a receipt
                # that never carried identity
                {"slot": "3/0", "clip_ref": REF, "identity_status": "accounting_error"},
            )
            for receipt in readback_cases:
                with self.subTest(receipt=receipt):
                    target = {}
                    entry = jam_lifecycle.stamp_verified_write(
                        target, clip_name="idea", slot="3/0", source="catalogue",
                        role="lead", gear=2, notes=NOTES, live_receipt=receipt,
                        ledger_path=path)
                    self.assertIsNotNone(entry)
                    self.assertFalse(target["scoreable"])
                    self.assertIn("clip readback could not be verified",
                                  target["scoreable_reason"])
                    self.assertIn("cannot be scored", target["scoreable_reason"])
                    self.assertNotIn("Producer Pal 2.2", target["scoreable_reason"])
            identity_cases = (
                # no Set identity at all
                {"slot": "3/0"},
                # the identity read itself failed
                {"slot": "3/0", "set_identity": IDENTITY, "clip_ref": REF,
                 "identity_status": "identity_error"},
            )
            for receipt in identity_cases:
                with self.subTest(receipt=receipt):
                    target = {}
                    entry = jam_lifecycle.stamp_verified_write(
                        target, clip_name="idea", slot="3/0", source="catalogue",
                        role="lead", gear=2, notes=NOTES, live_receipt=receipt,
                        ledger_path=path)
                    self.assertIsNotNone(entry)
                    self.assertFalse(target["scoreable"])
                    self.assertIn("Producer Pal 2.2", target["scoreable_reason"])
                    self.assertIn("cannot be scored", target["scoreable_reason"])
                    self.assertNotIn("clip readback", target["scoreable_reason"])
            self.assertEqual(len(ledger.load(path)["entries"]), 5)

    def test_same_event_replay_updates_ears_and_rating_once(self):
        with tempfile.TemporaryDirectory() as td:
            ledger_path = os.path.join(td, "ledger.json")
            ratings_path = os.path.join(td, "ratings.json")
            ears_path = os.path.join(td, "ears.json")
            develop_path = os.path.join(td, "develop.json")
            entry = ledger.record_write(
                clip_name="idea", slot="3/0", source="catalogue", answer_notes=NOTES,
                set_identity=IDENTITY, clip_ref=REF, path=ledger_path)
            old_ratings = gears.core._ratings_path()
            try:
                gears.core.set_ratings_path(ratings_path)
                with mock.patch.object(gears.core, "_phrases", return_value=[]):
                    first = gears.judge_answer(
                        "drop", answer_notes=NOTES, entry_id=entry["id"], role="lead",
                        ears_session_path=ears_path, develop_session_path=develop_path,
                        ledger_path=ledger_path)
                    second = gears.judge_answer(
                        "drop", answer_notes=NOTES, entry_id=entry["id"], role="lead",
                        ears_session_path=ears_path, develop_session_path=develop_path,
                        ledger_path=ledger_path)
            finally:
                gears.core.set_ratings_path(old_ratings)
            self.assertEqual(first["record"]["id"], second["record"]["id"])
            with open(ratings_path) as handle:
                self.assertEqual(len(json.load(handle)["ratings"]), 1)
            with open(ears_path) as handle:
                self.assertEqual(sum(json.load(handle)["streaks"].values()), 3)
            ledger.record_verdict(
                "drop", entry_id=entry["id"],
                event_id="judge:%s:drop" % entry["id"], path=ledger_path)
            event = ledger.judgment_event(
                "judge:%s:drop" % entry["id"], path=ledger_path)
            self.assertEqual(event["status"], "complete")

    def test_drop_callback_requires_verified_delete(self):
        callbacks = []
        deletes = []
        result = {"verdict": "drop"}
        jam_lifecycle.record_judgment(
            result, slot="3/0", confirm=True, entry_id="e1",
            record_verdict_fn=lambda *args, **kwargs: {"id": "e1"},
            clip_ref=REF, set_identity=IDENTITY,
            delete_jam_clip_fn=lambda slot, **kwargs: (
                deletes.append((slot, kwargs)) or {"deleted": False}),
            on_deleted=lambda: callbacks.append(True),
        )
        self.assertEqual(len(deletes), 1)
        self.assertEqual(callbacks, [])

    def test_observation_refresh_is_stable_and_does_not_rewrite_terminal_state(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "ledger.json")
            entry = ledger.record_write(
                clip_name="idea", slot="3/0", source="catalogue", answer_notes=NOTES,
                set_identity=IDENTITY, clip_ref=REF, path=path)
            ledger.record_verdict("keep", entry_id=entry["id"], path=path)
            ledger.close_session({"set_identity": IDENTITY, "complete": True,
                                  "clips": [REF], "errors": []}, path=path)
            current = {**IDENTITY, "runtime_epoch": "run-b"}
            destination = {**REF, "clip_id": "clip-b", "path": "t4/s0", "name": "idea"}
            inventory = {"set_identity": current, "complete": True,
                         "clips": [destination], "errors": [],
                         "observed_session_id": "observed-1"}
            first = ledger.refresh_observations(inventory, path=path)
            second = ledger.refresh_observations(inventory, path=path)
            self.assertEqual(first["recorded"], 1)
            self.assertEqual(second["recorded"], 0)
            self.assertEqual(ledger.load(path)["entries"][0]["terminal_state"], "keep")


class Gear4WriteNamesClipTests(unittest.TestCase):
    def test_gear4_write_names_clip_without_source(self):
        from pathlib import Path

        from mcp_server import server, session_ears

        answer = {
            "gear": 4,
            "answer_notes": [
                {"pitch": 62, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 80},
                {"pitch": 64, "start_beats": 0.5, "duration_beats": 0.5, "velocity": 78},
            ],
            "source_phrase_ids": [],
            "tempo": 120.0,
            "role": "lead",
        }
        calls = []
        fn = getattr(server.answer_my_loop, "fn", server.answer_my_loop)
        with tempfile.TemporaryDirectory() as ears_tmp, \
                mock.patch.object(session_ears, "STORE_ROOT", Path(ears_tmp)), \
                mock.patch.object(
                    gears, "answer_with_gear", return_value=answer), \
                mock.patch.object(
                    jam_lifecycle, "write_answer_to_jam",
                    side_effect=lambda *args, **kwargs: calls.append((args, kwargs)) or {}):
            fn(seed_notes=NOTES, gear=4, write=True, seed=7)
        self.assertEqual(len(calls), 1)
        clip_name = calls[0][1]["clip_name"]
        self.assertIn("g4", clip_name)


if __name__ == "__main__":
    unittest.main()
