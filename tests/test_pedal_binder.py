"""The SOMBRA pedal judges through the same ledger binder as chat."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

from mcp_server import ledger, sombra_listener

NOTES = [{"pitch": 60, "start": 0, "duration": 1, "velocity": 80}]
OTHER_NOTES = [{"pitch": 67, "start": 0, "duration": 1, "velocity": 80}]
IDENTITY = {"set_id": "pedal-set", "runtime_epoch": "run-1",
            "file_path": "/tmp/pedal.als"}


def _clip_ref(clip_id, notes=NOTES):
    return {"clip_id": clip_id, "track_id": "jam", "track": "SIDEKICK JAM",
            "surface": "session", "path": "t3/s0", "answer_notes": notes}


class PedalBinderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger_path = os.path.join(self._tmp.name, "ledger.json")
        patches = [mock.patch.object(ledger, "LEDGER_PATH", self.ledger_path)]
        for name in ("ears.SESSION_PATH", "develop_session.STATE_PATH"):
            module, attr = name.split(".")
            import importlib
            patches.append(mock.patch.object(
                importlib.import_module("mcp_server." + module), attr,
                os.path.join(self._tmp.name, "%s.json" % attr)))
        from mcp_server import gears
        old_ratings = gears.core._ratings_path()
        gears.core.set_ratings_path(os.path.join(self._tmp.name, "ratings.json"))
        self.addCleanup(gears.core.set_ratings_path, old_ratings)
        patches.append(mock.patch.object(gears.core, "_phrases", return_value=[]))
        self.gears = gears
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.state = {"last_answer_slot": None, "last_answer_entry_id": None,
                      "last_answer_notes": None, "last_answer_source": None,
                      "last_answer_seed": None, "last_answer_key": None,
                      "last_answer_clip_ref": None,
                      "last_answer_set_identity": None}
        self.patched_state = mock.patch.dict(sombra_listener.STATE, self.state,
                                             clear=True)
        self.patched_state.start()
        self.addCleanup(self.patched_state.stop)

    def write(self, name, *, notes=NOTES, slot="3/0", clip_id=None):
        return ledger.record_write(
            clip_name=name, slot=slot, source="catalogue",
            answer_notes=notes, set_identity=IDENTITY,
            clip_ref=_clip_ref(clip_id or name, notes), path=self.ledger_path)

    def judge(self, verdict="keep", **toolArgs):
        return sombra_listener._execute(
            {"tool": "judge_answer",
             "tool_args": {"verdict": verdict, **toolArgs}})

    def test_bound_entry_id_stamps_only_that_row(self):
        older = self.write("older")
        newer = self.write("newer", notes=OTHER_NOTES)
        sombra_listener.STATE["last_answer_entry_id"] = older["id"]
        sombra_listener.STATE["last_answer_notes"] = NOTES
        sombra_listener.STATE["last_answer_slot"] = "3/0"
        result = self.judge()
        self.assertEqual(result["ledger"]["entry_id"], older["id"])
        rows = {e["id"]: e for e in ledger.load(self.ledger_path)["entries"]}
        self.assertEqual(rows[older["id"]]["immediate_verdict"], "keep")
        self.assertIsNone(rows[newer["id"]]["immediate_verdict"])

    def test_no_entry_id_and_two_pending_rows_in_slot_is_ambiguous(self):
        # Two unjudged rows carry the remembered notes in the same slot:
        # the binder refuses, the refusal lands in accounting_failures, and
        # no ledger verdict is stamped.
        self.write("one")
        self.write("two", clip_id="two")
        sombra_listener.STATE["last_answer_notes"] = NOTES
        sombra_listener.STATE["last_answer_slot"] = "3/0"
        with mock.patch.object(self.gears, "judge_answer") as judge:
            result = self.judge(verdict="drop", slot="3/0", confirm=True)
        judge.assert_not_called()
        self.assertIn("ambiguous", result["ledger_error"])
        failures = sombra_listener.status()["accounting_failures"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["stage"], "binding")
        self.assertIn("ambiguous", failures[0]["message"])
        rows = ledger.load(self.ledger_path)["entries"]
        self.assertTrue(all(row["immediate_verdict"] is None for row in rows))

    def test_stamp_failure_is_visible_in_status(self):
        """fix4 R4: a stamp exception is reported, never escapes to the
        dispatcher — it lands in accounting_failures where status() sees
        it, the press returns ledger_error, and no learning runs."""
        entry = self.write("stamped")
        sombra_listener.STATE["last_answer_entry_id"] = entry["id"]
        sombra_listener.STATE["last_answer_notes"] = NOTES
        with mock.patch.object(
                ledger, "record_verdict",
                side_effect=RuntimeError("ledger unwritable")):
            result = self.judge()
        self.assertIn("ledger unwritable", result["ledger_error"])
        failures = sombra_listener.status()["accounting_failures"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["stage"], "stamp")
        self.assertIn("ledger unwritable", failures[0]["message"])
        # the verdict never landed, so nothing was learned either
        self.assertFalse(os.path.exists(
            os.path.join(self._tmp.name, "ratings.json")))

    def test_answer_resets_previous_entry_id(self):
        stale = self.write("stale")
        sombra_listener.STATE["last_answer_entry_id"] = stale["id"]
        sombra_listener.STATE["last_answer_notes"] = NOTES
        receipt = {"slot": "3/1", "track": "SIDEKICK JAM",
                   "clip_ref": _clip_ref("ans-2", OTHER_NOTES),
                   "set_identity": IDENTITY}
        answer = {"answer_notes": OTHER_NOTES, "tempo": 120.0,
                  "source_phrase_ids": ["p2"], "key": "D minor"}

        async def fake_write(_notes, *, clip_name, tempo):
            return dict(receipt)

        from mcp_server import core, live_write
        with mock.patch.object(sombra_listener, "_selection_notes",
                               return_value=NOTES), \
                mock.patch.object(core, "answer_my_loop", return_value=answer), \
                mock.patch.object(live_write, "write_answer_to_jam_track",
                                  new=fake_write):
            sombra_listener._execute({"tool": "answer_my_loop",
                                      "tool_args": {"gear": 2, "write": True}})
        # The new answer's row is the bound one, not the stale id.
        newest = ledger.load(self.ledger_path)["entries"][-1]
        self.assertNotEqual(newest["id"], stale["id"])
        self.assertEqual(sombra_listener.STATE["last_answer_entry_id"],
                         newest["id"])
        # ...and a KEEP with no explicit identity binds that fresh row.
        sombra_listener.STATE["last_answer_notes"] = OTHER_NOTES
        result = self.judge()
        self.assertEqual(result["ledger"]["entry_id"], newest["id"])


class PedalFailureGatingTests(unittest.TestCase):
    """fix3 item 1: the pedal's contradictions surface before stamping and
    its learning failures only exist when a verdict actually landed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger_path = os.path.join(self._tmp.name, "ledger.json")
        from mcp_server import develop_session, ears, gears
        self.gears = gears
        patches = [
            mock.patch.object(ledger, "LEDGER_PATH", self.ledger_path),
            mock.patch.object(ears, "SESSION_PATH",
                              os.path.join(self._tmp.name, "ears.json")),
            mock.patch.object(develop_session, "STATE_PATH",
                              os.path.join(self._tmp.name, "develop.json")),
            mock.patch.object(gears.core, "_phrases", return_value=[]),
        ]
        old_ratings = gears.core._ratings_path()
        gears.core.set_ratings_path(os.path.join(self._tmp.name, "ratings.json"))
        self.addCleanup(gears.core.set_ratings_path, old_ratings)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.state = {"last_answer_slot": None, "last_answer_entry_id": None,
                      "last_answer_notes": None, "last_answer_source": None,
                      "last_answer_seed": None, "last_answer_key": None,
                      "last_answer_clip_ref": None,
                      "last_answer_set_identity": None}
        patched = mock.patch.dict(sombra_listener.STATE, self.state, clear=True)
        patched.start()
        self.addCleanup(patched.stop)

    def write(self, name, notes=NOTES):
        return ledger.record_write(
            clip_name=name, slot="3/0", source="catalogue",
            answer_notes=notes, set_identity=IDENTITY,
            clip_ref={"clip_id": name, "track_id": "jam",
                      "track": "SIDEKICK JAM", "surface": "session",
                      "path": "t3/s0", "answer_notes": notes},
            path=self.ledger_path)

    def press(self, verdict, **tool_args):
        return sombra_listener._execute(
            {"tool": "judge_answer",
             "tool_args": {"verdict": verdict, **tool_args}})

    def test_conflicting_verdict_press_raises_before_any_stamping(self):
        entry = self.write("judged")
        with mock.patch.object(ledger, "_now", return_value=time.time()):
            ledger.record_verdict("keep", entry_id=entry["id"],
                                  path=self.ledger_path)
        sombra_listener.STATE["last_answer_entry_id"] = entry["id"]
        sombra_listener.STATE["last_answer_notes"] = NOTES
        with mock.patch.object(self.gears, "judge_answer") as learning:
            with self.assertRaisesRegex(ValueError, "contradicts"):
                self.press("drop")
        learning.assert_not_called()
        row = next(e for e in ledger.load(self.ledger_path)["entries"]
                   if e["id"] == entry["id"])
        self.assertEqual(row["immediate_verdict"], "keep")

    def test_conflicting_notes_press_raises_before_any_stamping(self):
        entry = self.write("revised")
        sombra_listener.STATE["last_answer_entry_id"] = entry["id"]
        # the remembered answer is NOT what the row now carries
        sombra_listener.STATE["last_answer_notes"] = OTHER_NOTES
        with mock.patch.object(self.gears, "judge_answer") as learning:
            with self.assertRaisesRegex(ValueError, "answer_notes contradict"):
                self.press("keep")
        learning.assert_not_called()
        row = next(e for e in ledger.load(self.ledger_path)["entries"]
                   if e["id"] == entry["id"])
        self.assertIsNone(row["immediate_verdict"])

    def test_record_verdict_conflict_is_not_swallowed(self):
        """A record_verdict conflict is reported on the press and in
        accounting_failures — visible, never silently absorbed."""
        entry = self.write("row")
        sombra_listener.STATE["last_answer_entry_id"] = entry["id"]
        sombra_listener.STATE["last_answer_notes"] = NOTES
        with mock.patch.object(
                ledger, "record_verdict",
                side_effect=ValueError("conflicting verdict for ledger entry")):
            result = self.press("keep")
        self.assertIn("conflicting verdict", result["ledger_error"])
        failures = sombra_listener.status()["accounting_failures"]
        self.assertTrue(any("conflicting verdict" in failure["message"]
                            for failure in failures))
        self.assertIsNone(
            next(e for e in ledger.load(self.ledger_path)["entries"]
                 if e["id"] == entry["id"])["immediate_verdict"])

    def test_unbound_press_error_is_not_a_learning_failure(self):
        """Nothing binds (the press's slot matches no row, no remembered
        notes or entry id) and learning has no material: the input error
        surfaces — no verdict was ever claimed to stand."""
        self.write("row", notes=NOTES)
        with self.assertRaisesRegex(ValueError, "need source_phrase_id"):
            self.press("keep", slot="9/9")
        rows = ledger.load(self.ledger_path)["entries"]
        self.assertTrue(all(row["immediate_verdict"] is None for row in rows))


if __name__ == "__main__":
    unittest.main()
