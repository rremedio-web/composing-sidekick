"""Ledger v3 sessions: aliases across saves, epochs across restarts (step 5).

A night survives first-save (unsaved:<epoch> -> path), Save-As (path ->
path) and Live restarts (same Set, new epoch). Different Sets and idle gaps
only END the previous session — closing (scoring) is the sweep's job alone.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import ledger


EPOCH_A = "epoch-a"
EPOCH_B = "epoch-b"
UNSAVED_A = {"set_id": "unsaved:epoch-a", "runtime_epoch": EPOCH_A,
             "file_path": None}
SAVED_A = {"set_id": "/music/a.als", "runtime_epoch": EPOCH_A,
           "file_path": "/music/a.als"}
SAVED_B = {"set_id": "/music/b.als", "runtime_epoch": EPOCH_B,
           "file_path": "/music/b.als"}
SAVED_A_OTHER_EPOCH = {"set_id": "/music/b.als", "runtime_epoch": EPOCH_A,
                       "file_path": "/music/b.als"}
SAVED_A_NEW_EPOCH = {"set_id": "/music/a.als", "runtime_epoch": EPOCH_B,
                     "file_path": "/music/a.als"}
SAVED_B_NEW_EPOCH = {"set_id": "/music/b.als", "runtime_epoch": EPOCH_B,
                     "file_path": "/music/b.als"}


def _session_set_ids(session):
    return ledger._session_chain(session)

T0 = 1_000_000.0


def ref(clip_id):
    return {"clip_id": clip_id, "track_id": "track-1", "track": "SIDEKICK JAM",
            "surface": "session", "path": "3/0"}


class LedgerSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "ledger.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, clip="idea", clip_id="clip-1", identity=SAVED_A, now=T0,
              **kwargs):
        with patch.object(ledger, "_now", return_value=now):
            return ledger.record_write(clip_name=clip, slot="3/0",
                                       source="catalogue",
                                       set_identity=identity,
                                       clip_ref=ref(clip_id), path=self.path,
                                       **kwargs)

    def sessions(self):
        return ledger.load(self.path)["sessions"]

    def test_v2_migrates_read_only_until_mutation_then_bak(self):
        original = json.dumps({
            "version": 2,
            "sessions": [{"id": "s-old", "opened_at": "2026-01-01T00:00:00Z",
                          "last_activity": 1000.0,
                          "closed_at": "1970-01-01T00:16:40Z",
                          "closed_by": "idle", "mode": None,
                          "set_identity": SAVED_A}],
            "entries": [{"id": "e-old", "session_id": "s-old",
                         "clip_name": "x", "terminal_state": None}],
            "adoptions": [], "observations": [], "judgment_events": []})
        Path(self.path).write_text(original)
        data = ledger.load(self.path)
        self.assertEqual(data["version"], 3)
        session = data["sessions"][0]
        self.assertEqual(session["set_aliases"], [])
        self.assertEqual(session["runtime_epochs"], [EPOCH_A])
        self.assertIsNone(session["rate_status"])
        # idle-"closed" with unresolved rows becomes ended, evidence untouched
        self.assertEqual(session["ended_at"], "1970-01-01T00:16:40Z")
        self.assertEqual(session["ended_by"], "idle")
        self.assertIsNone(session["closed_at"])
        self.assertEqual(session["closed_by"], "idle")
        self.assertIsNone(data["entries"][0]["terminal_state"])
        self.assertEqual(Path(self.path).read_text(), original)
        with patch.object(ledger, "_now", return_value=T0):
            ledger.open_session(path=self.path)
        self.assertEqual(Path(self.path + ".bak").read_text(), original)
        self.assertEqual(json.loads(Path(self.path).read_text())["version"], 3)

    def test_v2_idle_closed_fully_swept_session_stays_closed(self):
        original = json.dumps({
            "version": 2,
            "sessions": [{"id": "s-done", "opened_at": "2026-01-01T00:00:00Z",
                          "last_activity": 1000.0,
                          "closed_at": "1970-01-01T00:16:40Z",
                          "closed_by": "idle", "mode": None,
                          "set_identity": SAVED_A}],
            "entries": [{"id": "e-done", "session_id": "s-done",
                         "clip_name": "x", "terminal_state": "keep"}],
            "adoptions": [], "observations": [], "judgment_events": []})
        Path(self.path).write_text(original)
        session = ledger.load(self.path)["sessions"][0]
        self.assertEqual(session["closed_at"], "1970-01-01T00:16:40Z")
        self.assertIsNone(session["ended_at"])

    def test_v2_swept_session_without_open_rows_migrates_definitive(self):
        original = json.dumps({
            "version": 2,
            "sessions": [{"id": "s-v2", "opened_at": "2026-01-01T00:00:00Z",
                          "last_activity": 1000.0,
                          "closed_at": "2026-01-02T00:00:00Z",
                          "closed_by": "sweep", "mode": None,
                          "set_identity": SAVED_A},
                         {"id": "s-open", "opened_at": "2026-01-03T00:00:00Z",
                          "last_activity": 1000.0, "mode": None,
                          "set_identity": SAVED_A}],
            "entries": [{"id": "e-done", "session_id": "s-v2",
                         "clip_name": "x", "terminal_state": "keep",
                         "set_identity": SAVED_A},
                        {"id": "e-pending", "session_id": "s-open",
                         "clip_name": "y", "terminal_state": None}],
            "adoptions": [], "observations": [], "judgment_events": []})
        Path(self.path).write_text(original)
        data = ledger.load(self.path)
        by_id = {s["id"]: s for s in data["sessions"]}
        self.assertEqual(by_id["s-v2"]["rate_status"], "definitive")
        self.assertIsNone(by_id["s-open"].get("rate_status"))

    def test_scoreboard_counts_only_sessions_with_judged_rows(self):
        # A definitive session with nothing judged lends no weight: the
        # scoreboard (and the freeze rule) ignore it.
        original = json.dumps({
            "version": 2,
            "sessions": [{"id": "s-empty", "opened_at": "2026-01-01T00:00:00Z",
                          "last_activity": 1000.0,
                          "closed_at": "2026-01-02T00:00:00Z",
                          "closed_by": "sweep", "mode": None,
                          "rate_status": "definitive",
                          "set_identity": SAVED_A}],
            "entries": [{"id": "e-quiet", "session_id": "s-empty",
                         "clip_name": "x", "terminal_state": None}],
            "adoptions": [], "observations": [], "judgment_events": []})
        Path(self.path).write_text(original)
        scoreboard = ledger.summary(path=self.path)["scoreboard"]
        self.assertEqual(scoreboard["verified"]["definitive_sessions"], 0)
        self.assertEqual(scoreboard["verified"]["judged"], 0)
        self.assertFalse(ledger.summary(path=self.path)["freeze"]["met"])

    def test_scoreboard_never_counts_attested_rows_as_verified(self):
        # Verified and attested numbers are never combined: a row carrying
        # an attestation counts only in the attested stratum, even when it
        # also carries a terminal state in a definitive session. (No public
        # path writes both — attest refuses verified rows and sweeps skip
        # attested ones — so the state is built directly to pin the guard.)
        original = json.dumps({
            "version": 3,
            "sessions": [{"id": "s-mixed", "opened_at": "2026-01-01T00:00:00Z",
                          "last_activity": 1000.0,
                          "closed_at": "2026-01-02T00:00:00Z",
                          "closed_by": "sweep", "mode": None,
                          "rate_status": "definitive",
                          "set_identity": SAVED_A}],
            "entries": [{"id": "e-both", "session_id": "s-mixed",
                         "clip_name": "x", "set_identity": SAVED_A,
                         "immediate_verdict": "keep",
                         "terminal_state": "keep",
                         "attestation": {"fact": "present", "note": None,
                                         "attested_at": "2026-01-02T00:00:00Z"}}],
            "adoptions": [], "observations": [], "judgment_events": []})
        Path(self.path).write_text(original)
        scoreboard = ledger.summary(path=self.path)["scoreboard"]
        self.assertEqual(scoreboard["verified"]["judged"], 0)
        self.assertEqual(scoreboard["verified"]["keep"], 0)
        self.assertEqual(scoreboard["attested"]["judged"], 1)
        self.assertEqual(scoreboard["attested"]["keep"], 1)

    def test_newer_version_refused(self):
        original = json.dumps({"version": 99, "sessions": [], "entries": []})
        Path(self.path).write_text(original)
        with self.assertRaisesRegex(ValueError, "newer than supported"):
            ledger.load(self.path)
        self.assertEqual(Path(self.path).read_text(), original)

    def test_first_save_aliases_and_still_sweeps(self):
        notes = [{"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90},
                 {"pitch": 64, "start": 1.0, "duration": 0.5, "velocity": 90}]
        first = self.write(identity=UNSAVED_A, clip_id="clip-1",
                           answer_notes=[dict(n) for n in notes])
        second = self.write(clip="saved", clip_id="clip-2", identity=SAVED_A,
                            now=T0 + 60,
                            answer_notes=[dict(n) for n in notes])
        self.assertEqual(first["session_id"], second["session_id"])
        session = self.sessions()[0]
        self.assertEqual(session["set_identity"], UNSAVED_A)
        self.assertEqual(session["set_aliases"],
                         [{"set_id": "/music/a.als", "alias_kind": "first_save",
                           "at": ledger._iso(T0 + 60)}])
        self.assertEqual(session["alias_kind"], "first_save")
        ledger.record_verdict("keep", entry_id=first["id"], path=self.path)
        ledger.record_verdict("keep", entry_id=second["id"], path=self.path)
        result = ledger.close_session(
            {"set_identity": SAVED_A, "complete": True, "errors": [], "clips": [
                {**ref("clip-1"), "answer_notes": [dict(n) for n in notes]},
                {**ref("clip-2"), "answer_notes": [dict(n) for n in notes]}]},
            path=self.path)
        self.assertEqual(result["counts"]["keep"], 2)
        self.assertEqual(result["unresolved"], [])

    def test_save_as_aliases(self):
        first = self.write(identity=SAVED_A, clip_id="clip-1")
        second = self.write(clip="as", clip_id="clip-2",
                            identity=SAVED_A_OTHER_EPOCH, now=T0 + 60)
        self.assertEqual(first["session_id"], second["session_id"])
        session = self.sessions()[0]
        self.assertEqual(
            [(a["set_id"], a["alias_kind"]) for a in session["set_aliases"]],
            [("/music/b.als", "save_as")])
        self.assertEqual(session["alias_kind"], "save_as")

    def test_identity_less_write_joins_without_changing_identity(self):
        first = self.write(identity=SAVED_A, clip_id="clip-1")
        second = self.write(clip="idless", clip_id="clip-2", identity=None,
                            now=T0 + 60)
        self.assertEqual(first["session_id"], second["session_id"])
        session = self.sessions()[0]
        self.assertEqual(session["set_identity"], SAVED_A)
        self.assertEqual(session["set_aliases"], [])

    def test_session_with_identityless_rows_never_adopts_an_identity(self):
        """fix2 item 1(a): a night that already holds identity-less rows can
        never adopt a Set — no sweep could reconcile those rows against it —
        so the next identity-bearing write opens its own session instead of
        swallowing the legacy night into tonight's Set."""
        first = self.write(identity=None, clip_id="clip-1")
        session = self.sessions()[0]
        self.assertIsNone(session["set_identity"])
        second = self.write(clip="real", clip_id="clip-2", identity=SAVED_A,
                            now=T0 + 60)
        self.assertNotEqual(first["session_id"], second["session_id"])
        legacy, tonight = self.sessions()
        self.assertIsNone(legacy.get("set_identity"))
        self.assertIsNotNone(legacy.get("ended_at"))
        self.assertEqual(tonight["set_identity"], SAVED_A)

    def test_empty_identityless_session_still_adopts_first_identity(self):
        ledger.open_session(path=self.path)
        entry = self.write(identity=SAVED_A, clip_id="clip-1")
        session = self.sessions()[0]
        self.assertEqual(entry["session_id"], session["id"])
        self.assertEqual(session["set_identity"], SAVED_A)

    def test_same_set_new_epoch_continues(self):
        first = self.write(identity=SAVED_A, clip_id="clip-1")
        restarted = {**SAVED_A, "runtime_epoch": EPOCH_B}
        second = self.write(clip="restart", clip_id="clip-2",
                            identity=restarted, now=T0 + 60)
        self.assertEqual(first["session_id"], second["session_id"])
        session = self.sessions()[0]
        self.assertEqual(session["runtime_epochs"], [EPOCH_A, EPOCH_B])

    def test_first_save_then_restart_stays_one_session(self):
        self.write(identity=UNSAVED_A, clip_id="clip-1", now=T0)
        self.write(clip="saved", clip_id="clip-2", identity=SAVED_A, now=T0 + 10)
        restarted = self.write(clip="after-restart", clip_id="clip-3",
                               identity=SAVED_A_NEW_EPOCH, now=T0 + 20)
        sessions = self.sessions()
        self.assertEqual(len(sessions), 1)
        self.assertIn("epoch-b", sessions[0]["runtime_epochs"])
        self.assertEqual(restarted["session_id"], sessions[0]["id"])

    def test_save_as_then_restart_stays_one_session(self):
        self.write(identity=SAVED_A, clip_id="clip-1", now=T0)
        self.write(clip="save-as", clip_id="clip-2",
                   identity=SAVED_A_OTHER_EPOCH, now=T0 + 10)
        restarted = self.write(clip="after-restart", clip_id="clip-3",
                               identity=SAVED_B_NEW_EPOCH, now=T0 + 20)
        sessions = self.sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(restarted["session_id"], sessions[0]["id"])

    def test_restart_then_save_as_stays_one_session(self):
        self.write(identity=SAVED_A, clip_id="clip-1", now=T0)
        self.write(clip="restart", clip_id="clip-2", identity=SAVED_A_NEW_EPOCH,
                   now=T0 + 10)
        saved_as = self.write(clip="save-as", clip_id="clip-3",
                              identity=SAVED_B_NEW_EPOCH, now=T0 + 20)
        sessions = self.sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(saved_as["session_id"], sessions[0]["id"])
        self.assertIn("/music/b.als", _session_set_ids(sessions[0]))

    def test_ended_session_is_not_reported_as_open(self):
        # A sealed-but-open night is ended: neither summary.open_session nor
        # open_session_entries reports it as the live night, while the
        # sweep backlog still names it.
        pending_notes = [{"pitch": 67, "start": 0.0, "duration": 0.5,
                          "velocity": 90},
                         {"pitch": 71, "start": 1.0, "duration": 0.5,
                          "velocity": 90}]
        entry = self.write(clip_id="clip-1", identity=SAVED_A, now=T0)
        self.write(clip="pending", clip_id="clip-2", identity=SAVED_A,
                   answer_notes=[dict(n) for n in pending_notes], now=T0 + 2)
        pending2 = self.write(clip="pending2", clip_id="clip-9",
                              identity=SAVED_A,
                              answer_notes=[dict(n) for n in pending_notes],
                              now=T0 + 3)
        ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
        ledger.record_verdict("keep", entry_id=pending2["id"], path=self.path)
        awaitable = [dict(ref("clip-1"),
                          answer_notes=[{"pitch": 60, "start": 0.0,
                                         "duration": 0.5, "velocity": 90}]),
                     dict(ref("amb-1"), answer_notes=pending_notes),
                     dict(ref("amb-2"), answer_notes=pending_notes)]
        ledger.close_session(
            {"set_identity": SAVED_A, "complete": True, "errors": [],
             "clips": awaitable}, path=self.path)
        session = self.sessions()[0]
        self.assertIsNotNone(session.get("ended_at"))
        self.assertIsNone(session.get("closed_at"))
        summary = ledger.summary(path=self.path)
        self.assertIsNone(summary["open_session"])
        self.assertEqual(ledger.open_session_entries(ledger.load(self.path)), [])
        needing = ledger.sessions_needing_sweep(ledger.load(self.path))
        self.assertEqual([group["session"]["id"] for group in needing],
                         [session["id"]])

    def test_non_explicit_sweep_ignores_other_sets_night(self):
        # Set A's night ended when Set B opened; sweeping B's inventory
        # without a session id scores B alone — A's night keeps no seal,
        # no coverage, no states from evidence it cannot hear.
        self.write(clip="a1", clip_id="clip-a", identity=SAVED_A, now=T0)
        self.write(clip="b1", clip_id="clip-b", identity=SAVED_B, now=T0 + 60)
        previous, current = self.sessions()
        self.assertEqual(previous["ended_by"], "set_change")
        entries = ledger.load(self.path)["entries"]
        other = next(e for e in entries if e["clip_name"] == "b1")
        ledger.record_verdict("keep", entry_id=other["id"], path=self.path)
        result = ledger.close_session(
            {"set_identity": SAVED_B, "complete": True, "errors": [],
             "clips": [dict(ref("clip-b"),
                            answer_notes=[{"pitch": 60, "start": 0.0,
                                           "duration": 0.5, "velocity": 90}])]},
            path=self.path)
        previous, current = self.sessions()
        self.assertIsNone(previous.get("swept_at"))
        self.assertIsNone(previous.get("swept_complete"))
        self.assertNotIn("sweep_coverage", previous)
        self.assertEqual(result["session_ids"], [current["id"]])

    def test_different_set_ends_previous_without_closing(self):
        first = self.write(identity=SAVED_A, clip_id="clip-1")
        second = self.write(clip="other", clip_id="clip-9", identity=SAVED_B,
                            now=T0 + 60)
        self.assertNotEqual(first["session_id"], second["session_id"])
        previous, current = self.sessions()
        self.assertEqual(previous["ended_at"], ledger._iso(T0))
        self.assertEqual(previous["ended_by"], "set_change")
        self.assertIsNone(previous["closed_at"])
        self.assertIsNone(current["ended_at"])

    def test_idle_gap_ends_at_last_activity(self):
        first = self.write(identity=SAVED_A, clip_id="clip-1")
        second = self.write(clip="later", clip_id="clip-2", identity=SAVED_A,
                            now=T0 + ledger.SESSION_IDLE_SECONDS + 60)
        self.assertNotEqual(first["session_id"], second["session_id"])
        previous = self.sessions()[0]
        self.assertEqual(previous["ended_at"], ledger._iso(T0))
        self.assertEqual(previous["ended_by"], "idle")
        self.assertIsNone(previous["closed_at"])

    def test_record_verdict_bumps_last_activity(self):
        entry = self.write(identity=SAVED_A, clip_id="clip-1")
        self.assertEqual(self.sessions()[0]["last_activity"], T0)
        with patch.object(ledger, "_now", return_value=T0 + 100):
            ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
        self.assertEqual(self.sessions()[0]["last_activity"], T0 + 100)

    def test_bare_judgment_binds_only_the_live_session(self):
        # Set A night ended by Set B; once B is swept closed no live session
        # remains, so the bare-judgment binder refuses instead of reaching
        # back into A's ended night.
        self.write(clip="a1", clip_id="clip-a", identity=SAVED_A, now=T0)
        second = self.write(clip="b1", clip_id="clip-9", identity=SAVED_B,
                            now=T0 + 60)
        binding = ledger.resolve_judgment_target(ledger.load(self.path), "drop",
                                                 now=T0 + 90)
        self.assertTrue(binding["bound"])
        self.assertEqual(binding["entry"]["id"], second["id"])
        with patch.object(ledger, "_now", return_value=T0 + 120):
            ledger.record_verdict("drop", entry_id=second["id"], path=self.path)
            ledger.close_session(
                {"set_identity": SAVED_B, "complete": True, "errors": [],
                 "clips": []}, path=self.path)
        previous, current = self.sessions()
        self.assertEqual(previous["ended_by"], "set_change")
        self.assertIsNotNone(current["closed_at"])
        binding = ledger.resolve_judgment_target(ledger.load(self.path), "drop",
                                                 now=T0 + 130)
        self.assertTrue(binding["refused"])
        self.assertIn("no active session", binding["error"])

    def test_advice_lists_every_unswept_session(self):
        self.write(identity=SAVED_A, clip_id="clip-1")
        self.write(clip="other", clip_id="clip-9", identity=SAVED_B,
                   now=T0 + 60)
        summary = ledger.summary(path=self.path)
        self.assertEqual(summary["open_session_awaiting_sweep"], 2)
        self.assertEqual(len(summary["advice"]), 2)
        for item in summary["advice"]:
            self.assertIn("close_session()", item)
            self.assertIn("while this Set is still open", item)


if __name__ == "__main__":
    unittest.main()
