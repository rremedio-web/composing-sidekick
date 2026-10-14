import asyncio
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from mcp_server import develop_session, ears, gears, jam_lifecycle, ledger
from tests._requires import requires_catalog


NOTES = [{"pitch": 60, "start": 0, "duration": 1, "velocity": 80}]
IDENTITY = {"set_id": "set-public", "runtime_epoch": "run-1",
            "file_path": "/tmp/public.als"}


@requires_catalog
class PublicLifecycleRepairTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.ledger_path = str(self.root / "ledger.json")
        self.stack.enter_context(mock.patch.object(ledger, "LEDGER_PATH", self.ledger_path))
        self.stack.enter_context(mock.patch.object(ears, "SESSION_PATH",
                                                   str(self.root / "ears.json")))
        self.stack.enter_context(mock.patch.object(develop_session, "STATE_PATH",
                                                   str(self.root / "develop.json")))
        old_ratings = gears.core._ratings_path()
        gears.core.set_ratings_path(str(self.root / "ratings.json"))
        self.addCleanup(gears.core.set_ratings_path, old_ratings)

    def test_write_midi_clip_refuses_jam(self):
        """SIDEKICK JAM is the stamped answer surface: write_midi_clip must
        refuse it before any Live call, with the stamped write paths named,
        and leave no ledger row behind."""
        from mcp_server import clip_mutation, server

        calls = []

        async def fake_write(*_args, **_kwargs):
            calls.append(True)
            return {"status": "verified"}

        with mock.patch.object(clip_mutation, "write_midi_clip",
                               side_effect=fake_write):
            with self.assertRaisesRegex(ValueError, "answer_my_loop") as raised:
                server.write_midi_clip("SIDEKICK JAM", 0, NOTES,
                                       name="nope", confirm=True)
            with self.assertRaisesRegex(ValueError, "write_composed_to_jam"):
                server.write_midi_clip("  sidekick jam ", 0, NOTES,
                                       name="nope", confirm=True)
        self.assertEqual(calls, [])
        self.assertEqual(ledger.load(self.ledger_path)["entries"], [])

    def test_public_drop_preview_then_confirm_deletes_exact_entry_once(self):
        from mcp_server import live_write, server

        entry = ledger.record_write(
            clip_name="drop-me", slot="3/0", source="catalogue",
            answer_notes=NOTES, set_identity=IDENTITY,
            clip_ref={"clip_id": "clip-drop", "track_id": "jam",
                      "track": "SIDEKICK JAM", "surface": "session",
                      "path": "t3/s0", "answer_notes": NOTES},
            path=self.ledger_path)
        deletes = []

        async def fake_delete(slot, **expected):
            deletes.append((slot, expected))
            return {"deleted": True, "clip_id": expected["expected_clip_id"]}

        with mock.patch.object(live_write, "delete_jam_clip", side_effect=fake_delete), \
                mock.patch.object(server, "_run_async",
                                  side_effect=lambda coro: asyncio.run(coro)), \
                mock.patch.object(gears.core, "_phrases", return_value=[]):
            preview = server.judge_answer("drop", answer_notes=NOTES, confirm=False)
            confirmed = server.judge_answer("drop", entry_id=entry["id"], confirm=True)

        # The ledger stores slots canonically ("3/0" → "t3/s0"); the delete
        # itself is by verified clip id, the slot is display only.
        self.assertEqual(preview["clip_delete"]["would_delete"], "t3/s0")
        self.assertEqual(confirmed["clip_delete"]["clip_id"], "clip-drop")
        self.assertEqual(len(deletes), 1)
        event = ledger.judgment_event("judge:%s:drop" % entry["id"],
                                      path=self.ledger_path)
        self.assertEqual(event["steps"]["deletion"]["status"], "applied")
        self.assertEqual(event["result"]["clip_delete"]["clip_id"], "clip-drop")

    def test_public_refresh_requires_later_runtime_and_uses_current_set(self):
        from mcp_server import live_write, server

        entry = ledger.record_write(
            clip_name="refresh-me", slot="3/0", source="catalogue",
            answer_notes=NOTES, set_identity=IDENTITY,
            clip_ref={"clip_id": "clip-refresh", "track_id": "jam",
                      "track": "SIDEKICK JAM", "surface": "session",
                      "path": "t3/s0", "answer_notes": NOTES},
            path=self.ledger_path)
        ledger.record_verdict("keep", entry_id=entry["id"], path=self.ledger_path)
        ledger.close_session({"set_identity": IDENTITY, "complete": True,
                              "clips": [entry["clip_ref"]], "errors": []},
                             path=self.ledger_path)
        immediate = {"set_identity": IDENTITY, "complete": True,
                     "clips": [entry["clip_ref"]], "errors": []}
        later = {**IDENTITY, "runtime_epoch": "run-2"}
        later_inventory = {"set_identity": later, "complete": True,
                           "clips": [{**entry["clip_ref"], "clip_id": "clip-new"}],
                           "errors": []}
        with mock.patch.object(live_write, "read_set_inventory", new=lambda: None), \
                mock.patch.object(server, "_run_async",
                                  side_effect=[immediate, later_inventory]):
            first = server.session_report(refresh=True)
            second = server.session_report(refresh=True)
        self.assertEqual(first["refresh"]["recorded"], 0)
        self.assertEqual(second["refresh"]["recorded"], 1)
        self.assertEqual(second["refresh"]["coverage"]["next_session_eligible"], True)


class LifecycleAccountingTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.ledger_path = os.path.join(self.root.name, "ledger.json")
        self.ledger_patch = mock.patch.object(ledger, "LEDGER_PATH", self.ledger_path)
        self.ledger_patch.start()
        self.addCleanup(self.ledger_patch.stop)

    def test_failed_ledger_verdict_with_confirm_never_calls_delete(self):
        deletes = []
        result = {"verdict": "drop"}
        jam_lifecycle.record_judgment(
            result, slot="3/0", confirm=True, entry_id="e1",
            record_verdict_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("ledger unavailable")),
            clip_ref={"clip_id": "c1", "track_id": "t1"},
            set_identity=IDENTITY,
            delete_jam_clip_fn=lambda *_args, **_kwargs: deletes.append(True),
        )
        self.assertEqual(deletes, [])
        self.assertEqual(result["accounting_failure"]["stage"], "ledger_verdict")

    def test_applied_deletion_receipt_is_not_replayed(self):
        event_id = "judge:e1:drop"
        path = self.ledger_path
        entry = ledger.record_write(clip_name="x", slot="3/0", source="catalogue",
                                    answer_notes=NOTES, path=path)
        ledger.begin_judgment(event_id, {"entry_id": entry["id"], "verdict": "drop"},
                              entry_id=entry["id"], path=path)
        deletes = []

        def delete(_slot, **_expected):
            deletes.append(True)
            return {"deleted": True, "clip_id": "c1"}

        kwargs = {"slot": "3/0", "confirm": True, "entry_id": entry["id"],
                  "event_id": event_id, "ledger_path": path,
                  "record_verdict_fn": lambda verdict, **kw: ledger.record_verdict(
                      verdict, path=path, **kw),
                  "clip_ref": {"clip_id": "c1", "track_id": "t1"},
                  "set_identity": IDENTITY, "delete_jam_clip_fn": delete}
        jam_lifecycle.record_judgment({"verdict": "drop"}, **kwargs)
        jam_lifecycle.record_judgment({"verdict": "drop"}, **kwargs)
        self.assertEqual(deletes, [True])

    def test_judgment_retry_after_revision_uses_original_heard_snapshot(self):
        original = ledger.record_write(
            clip_name="original", slot="3/0", source="catalogue",
            answer_notes=NOTES, set_identity=IDENTITY,
            clip_ref={"clip_id": "old", "track_id": "jam",
                      "track": "SIDEKICK JAM", "surface": "session",
                      "path": "t3/s0", "answer_notes": NOTES},
            path=self.ledger_path)
        ledger.record_verdict("keep", entry_id=original["id"], path=self.ledger_path)
        revised_notes = [{**NOTES[0], "pitch": 65}]
        ledger.record_write(
            clip_name="revised", slot="3/0", source="catalogue",
            answer_notes=revised_notes, set_identity=IDENTITY,
            clip_ref={"clip_id": "new", "track_id": "jam",
                      "track": "SIDEKICK JAM", "surface": "session",
                      "path": "t3/s0", "answer_notes": revised_notes},
            revision_of=original["id"], path=self.ledger_path)
        rated = []
        with mock.patch.object(gears.core, "_phrases", return_value=[]), \
                mock.patch.object(ears, "record_verdict", return_value=[]), \
                mock.patch.object(develop_session, "record_keep", return_value={}), \
                mock.patch.object(gears.core, "rate", side_effect=lambda **kw: (
                    rated.append(kw) or {"record": {"id": 1}})):
            gears.judge_answer("keep", entry_id=original["id"],
                              answer_notes=NOTES, seed_notes=NOTES,
                              ears_session_path=os.path.join(self.root.name,
                                                             "ears-revision.json"),
                              develop_session_path=os.path.join(self.root.name,
                                                                "develop-revision.json"),
                              ledger_path=self.ledger_path)
        self.assertEqual(rated[0]["notes"], NOTES)


class RefreshLedgerRepairTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.path = os.path.join(self.root.name, "ledger.json")

    def _write(self, name, identity, clip_id, notes=NOTES):
        return ledger.record_write(
            clip_name=name, slot="3/0", source="catalogue", answer_notes=notes,
            set_identity=identity,
            clip_ref={"clip_id": clip_id, "track_id": "jam",
                      "track": "SIDEKICK JAM", "surface": "session",
                      "path": "t3/s0", "answer_notes": notes},
            path=self.path)

    def _close(self, identity, clips):
        return ledger.close_session({"set_identity": identity, "complete": True,
                                     "clips": clips, "errors": []}, path=self.path)

    def test_immediate_refresh_is_not_next_session(self):
        entry = self._write("same", IDENTITY, "clip-same")
        self._close(IDENTITY, [entry["clip_ref"]])
        result = ledger.refresh_observations(
            {"set_identity": IDENTITY, "complete": True,
             "clips": [entry["clip_ref"]], "errors": []}, path=self.path)
        self.assertEqual(result["recorded"], 0)
        self.assertFalse(result["coverage"]["next_session_eligible"])
        self.assertEqual(ledger.observation_report(self.path)["observations"], 0)

    def test_refresh_uses_latest_closed_session_for_current_set(self):
        old = self._write("old-set", IDENTITY, "clip-old")
        self._close(IDENTITY, [old["clip_ref"]])
        other = {"set_id": "other", "runtime_epoch": "other-run", "file_path": "/tmp/o.als"}
        newer = self._write("other-set", other, "clip-other")
        self._close(other, [newer["clip_ref"]])
        current = {**IDENTITY, "runtime_epoch": "run-later"}
        result = ledger.refresh_observations(
            {"set_identity": current, "complete": True,
             "clips": [{**old["clip_ref"], "clip_id": "clip-moved"}], "errors": []},
            path=self.path)
        self.assertEqual(result["recorded"], 1)
        self.assertEqual(result["coverage"]["target_session_id"], old["session_id"])

    def test_duplicate_cross_runtime_claims_remain_unresolved(self):
        first = self._write("same", IDENTITY, "clip-first")
        second = self._write("same", IDENTITY, "clip-second")
        self._close(IDENTITY, [first["clip_ref"], second["clip_ref"]])
        current = {**IDENTITY, "runtime_epoch": "run-later"}
        shared = {**first["clip_ref"], "clip_id": "clip-new"}
        result = ledger.refresh_observations(
            {"set_identity": current, "complete": True, "clips": [shared], "errors": []},
            path=self.path)
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(result["coverage"]["unresolved"], 2)
        self.assertEqual(ledger.observation_report(self.path)["observations"], 0)

    def test_same_runtime_known_later_session_can_record_verified_absence(self):
        entry = self._write("absent", IDENTITY, "clip-absent")
        self._close(IDENTITY, [entry["clip_ref"]])
        result = ledger.refresh_observations(
            {"set_identity": IDENTITY, "observed_session_id": "later-session",
             "complete": True, "clips": [], "errors": []}, path=self.path)
        self.assertEqual(result["recorded"], 1)
        row = ledger.load(self.path)["observations"][0]
        self.assertFalse(row["present"])


if __name__ == "__main__":
    unittest.main()
