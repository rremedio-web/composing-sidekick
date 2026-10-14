import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import ledger


IDENTITY = {"set_id": "set-A", "runtime_epoch": "run-1", "file_path": "/tmp/a.als"}
REF = {"clip_id": "clip-1", "track_id": "track-1", "track": "SIDEKICK JAM",
       "surface": "session", "path": "3/0"}


def inventory(identity=IDENTITY, clips=(), complete=True, errors=()):
    return {"set_identity": identity, "complete": complete,
            "clips": list(clips), "errors": list(errors)}


def clip(ref=REF, **extra):
    value = dict(ref)
    value.update(extra)
    return value


class LedgerIdentityTests(unittest.TestCase):
    def test_runtime_id_survives_move_to_another_track_and_surface(self):
        entry = self.write()
        ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
        moved = clip(track_id="song-track", track="Song", surface="arrangement", path="t8")
        result = ledger.close_session(inventory(clips=[moved]), path=self.path)
        self.assertEqual(result["counts"]["keep"], 1)
        self.assertEqual(result["counts"]["drop"], 0)

    def test_duplicate_runtime_id_at_different_locations_is_incomplete(self):
        entry = self.write()
        ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
        duplicate = clip(track_id="other", track="Other", path="t9/s0")
        result = ledger.close_session(inventory(clips=[REF, duplicate]), path=self.path)
        self.assertFalse(result["coverage"]["complete"])
        self.assertIsNone(result["keep_rate"])

    def test_revision_after_final_close_preserves_terminal_history(self):
        entry = self.write()
        ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
        original = ledger.close_session(inventory(clips=[REF]), path=self.path)
        revised = self.write(revision_of=entry["id"], clip_name="edited later")
        self.assertEqual(revised["terminal_state"], "keep")
        again = ledger.close_session(inventory(clips=[]), session_id=entry["session_id"], path=self.path)
        self.assertEqual(original, again)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "ledger.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, **kwargs):
        ref = kwargs.pop("clip_ref", REF)
        return ledger.record_write(clip_name=kwargs.pop("clip_name", "idea"),
                                   slot=kwargs.pop("slot", "3/0"), source="catalogue",
                                   set_identity=IDENTITY, clip_ref=ref,
                                   answer_notes=[{"pitch": 60, "start": 0, "duration": 1,
                                                  "velocity": 90, "probability": 0.5,
                                                  "release_velocity": 22}],
                                   path=self.path, **kwargs)

    def test_migrates_legacy_without_assigning_current_identity(self):
        original = json.dumps({"version": 1, "sessions": [],
                               "entries": [{"id": "old", "clip_name": "x"}]})
        Path(self.path).write_text(original)
        data = ledger.load(self.path)
        self.assertEqual(data["version"], 3)
        self.assertIsNone(data["entries"][0]["set_identity"])
        self.assertEqual(data["entries"][0]["identity_status"], "unknown")
        self.assertEqual(Path(self.path).read_text(), original)
        ledger.open_session(path=self.path)
        self.assertEqual(json.loads(Path(self.path).read_text())["version"], 3)
        self.assertEqual(Path(self.path + ".bak").read_text(), original)

    def test_future_schema_is_rejected_without_downgrade(self):
        original = json.dumps({"version": 99, "sessions": [], "entries": []})
        Path(self.path).write_text(original)
        with self.assertRaisesRegex(ValueError, "newer than supported"):
            ledger.load(self.path)
        self.assertEqual(Path(self.path).read_text(), original)

    def test_reused_slot_and_stale_id_do_not_match_replacement(self):
        entry = self.write()
        ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
        result = ledger.close_session(inventory(clips=[clip({**REF, "clip_id": "replacement"})]),
                                      path=self.path)
        self.assertEqual(result["counts"]["drop"], 1)
        self.assertEqual(result["entries"][0]["match"], "runtime_id_absent")
        self.assertEqual(result["keep_rate"], 0.0)

    def test_shared_clip_collision_leaves_all_claims_unresolved(self):
        first = self.write(slot="3/0")
        second = self.write(slot="3/1")
        ledger.record_verdict("keep", entry_id=first["id"], path=self.path)
        ledger.record_verdict("keep", entry_id=second["id"], path=self.path)
        result = ledger.close_session(inventory(clips=[REF]), path=self.path)
        self.assertEqual(result["counts"]["keep"], 0)
        self.assertEqual(result["counts"]["drop"], 0)
        self.assertEqual({row["reason"] for row in result["unresolved"]},
                         {"shared_clip_collision"})
        self.assertIsNone(json.loads(Path(self.path).read_text())["sessions"][0]["closed_at"])

    def test_partial_inventory_and_bare_list_never_declare_absence(self):
        entry = self.write()
        ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
        for evidence in ([], inventory(complete=False, errors=["track omitted"])):
            result = ledger.close_session(evidence, path=self.path)
            self.assertEqual(result["counts"]["drop"], 0)
            self.assertEqual(len(result["unresolved"]), 1)
            if result["session_id"]:
                ledger.reopen_session(result["session_id"], path=self.path)

    def test_reopen_then_clear_match_and_repeated_report_are_stable(self):
        entry = self.write()
        ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
        partial = ledger.close_session([], path=self.path)
        ledger.reopen_session(partial["session_id"], path=self.path)
        complete = ledger.close_session(inventory(clips=[REF]), path=self.path)
        repeated = ledger.close_session(complete, session_id=complete["session_id"], path=self.path)
        self.assertEqual(complete["counts"], repeated["counts"])
        self.assertEqual(complete["entries"], repeated["entries"])
        self.assertEqual(complete["keep_rate"], 1.0)

    def test_partial_close_aggregates_prior_resolution_and_remains_retryable(self):
        first = self.write(clip_ref=REF)
        second = self.write(clip_name="second", clip_ref={**REF, "clip_id": "clip-2"})
        ledger.record_verdict("keep", entry_id=first["id"], path=self.path)
        partial = ledger.close_session(inventory(clips=[REF], complete=False), path=self.path)
        self.assertEqual(partial["counts"], {key: 0 for key in ledger.TERMINAL_STATES})
        self.assertEqual(len(partial["unresolved"]), 2)
        complete = ledger.close_session(inventory(clips=[REF, {**REF, "clip_id": "clip-2",
                                                               "name": "second"}]), path=self.path)
        self.assertEqual(complete["counts"]["keep"], 1)
        self.assertEqual(complete["counts"]["unjudged"], 1)
        self.assertEqual(complete["judged"], 1)

    def test_cross_runtime_unique_content_context_can_reconcile_once(self):
        entry = self.write()
        ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
        current = {**IDENTITY, "runtime_epoch": "run-2"}
        notes = entry["answer_notes"]
        destination = {"clip_id": "new-id", "track_id": "track-1", "track": "SIDEKICK JAM",
                       "surface": "session", "path": "4/0", "name": "idea",
                       "answer_notes": notes}
        result = ledger.close_session(inventory(current, [destination]), path=self.path)
        self.assertEqual(result["counts"]["keep"], 1)
        self.assertEqual(result["entries"][0]["match"], "unique_content_context")

    def test_fingerprint_ignores_runtime_note_ids_and_rejects_missing_cross_epoch_content(self):
        notes_a = [{"pitch": 60, "start": 0, "duration": 1, "velocity": 90, "note_id": 11}]
        notes_b = [{"pitch": 60, "start": 0, "duration": 1, "velocity": 90, "note_id": 99}]
        first = ledger.record_write(clip_name="ids", slot="1/0", source="catalogue",
                                    set_identity=IDENTITY, clip_ref={**REF, "name": "ids",
                                                                      "answer_notes": notes_a},
                                    answer_notes=notes_a, path=self.path)
        self.assertEqual(first["fingerprint"], ledger._clip_ref(
            {**REF, "name": "ids", "answer_notes": notes_b})["fingerprint"])
        ledger.record_verdict("keep", entry_id=first["id"], path=self.path)
        current = {**IDENTITY, "runtime_epoch": "run-2"}
        no_content = {**REF, "clip_id": "new", "name": "ids"}
        result = ledger.close_session(inventory(current, [no_content]), path=self.path)
        self.assertIn(result["unresolved"][0]["reason"],
                      {"cross_runtime_content_unavailable", "cross_runtime_unresolved"})

    def test_cross_set_is_unresolved_and_identity_starts_new_session(self):
        a = self.write()
        other = {**IDENTITY, "set_id": "set-B", "runtime_epoch": "run-b"}
        b = ledger.record_write(clip_name="other", slot="3/0", source="catalogue",
                               set_identity=other, clip_ref={**REF, "clip_id": "clip-b"},
                               path=self.path)
        data = ledger.load(self.path)
        self.assertNotEqual(a["session_id"], b["session_id"])
        result = ledger.close_session(inventory(other, [dict({**REF, "clip_id": "clip-b"})]),
                                      session_id=b["session_id"], path=self.path)
        self.assertEqual(result["counts"]["abandoned"], 0)
        self.assertEqual(result["unresolved"], [])

    def test_explicit_adoption_survives_lane_promotion(self):
        entry = self.write()
        destination = {**REF, "clip_id": "arr-1", "track_id": "lane-1",
                       "surface": "arrangement", "path": "lane/0"}
        linked = ledger.adopt_entry(entry["id"], destination, set_identity=IDENTITY,
                                    path=self.path)
        ledger.adopt_entry(entry["id"], destination, set_identity=IDENTITY,
                           path=self.path)
        result = ledger.close_session(inventory(clips=[destination]), path=self.path)
        self.assertEqual(result["counts"]["unjudged"], 1)
        self.assertEqual(result["entries"][0]["match"], "explicit_adoption")
        self.assertEqual(linked["adoption_links"][0]["destination_ref"]["clip_id"], "arr-1")
        self.assertEqual(len(ledger.load(self.path)["adoptions"]), 1)

    def test_revisions_share_suggestion_and_do_not_duplicate_denominator(self):
        original = self.write()
        ledger.record_verdict("keep", entry_id=original["id"], path=self.path)
        revision = self.write(revision_of=original["id"], clip_ref={**REF, "clip_id": "clip-2"})
        self.assertEqual(revision["id"], original["id"])
        self.assertEqual(revision["suggestion_id"], original["suggestion_id"])
        result = ledger.close_session(inventory(clips=[{**REF, "clip_id": "clip-2"}]), path=self.path)
        self.assertEqual(result["judged"], 1)
        self.assertEqual(result["counts"]["keep"], 1)
        stored = ledger.load(self.path)["entries"][0]
        self.assertEqual(len(stored["revision_history"]), 1)
        self.assertEqual(stored["judged_snapshot"]["clip_ref"]["clip_id"], "clip-1")
        self.assertEqual(stored["immediate_verdict"], "keep")

    def test_expressive_notes_and_observations_are_preserved(self):
        entry = self.write()
        stored = ledger.load(self.path)["entries"][0]
        self.assertEqual(stored["answer_notes"][0]["release_velocity"], 22)
        ledger.record_observation(entry["id"], observation="next_session", present=True,
                                  set_identity=IDENTITY, clip_ref=REF, path=self.path)
        ledger.record_observation(entry["id"], observation="next_session", present=True,
                                  set_identity=IDENTITY, clip_ref=REF, path=self.path)
        self.assertEqual(ledger.observation_report(self.path)["by_type"]["next_session"]["present"], 1)
        with self.assertRaisesRegex(ValueError, "different Set"):
            ledger.record_observation(entry["id"], observation="next_session", present=True,
                                      set_identity={**IDENTITY, "set_id": "other"}, clip_ref=REF,
                                      path=self.path)

    def test_mode_mixed_is_reported_and_idle_boundary_splits(self):
        with patch.object(ledger, "_now", side_effect=[1000.0, 1001.0, 1002.0]):
            first = ledger.open_session("catalogue", set_identity=IDENTITY, path=self.path)
            second = ledger.open_session("ref", set_identity=IDENTITY, path=self.path)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["mode"], "mixed")
        with patch.object(ledger, "_now", return_value=1000.0 + ledger.SESSION_IDLE_SECONDS + 2):
            third = ledger.open_session("ref", set_identity=IDENTITY, path=self.path)
        self.assertNotEqual(second["id"], third["id"])


if __name__ == "__main__":
    unittest.main()
