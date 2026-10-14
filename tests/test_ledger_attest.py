"""Attest / retire: the owner's word, in its own stratum (step 7).

Legacy nights written before identity existed can never be scored by a
sweep. Attestation lets the owner settle them — feeding resolve_terminal
with the row's EXISTING verdict, never creating one — and the attested
numbers never mix with the verified ones.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mcp_server import ledger


def legacy_ledger():
    """Synthetic v1 fixture shaped like the real 2026-09-02 night: five rows,
    two KEEP verdicts, no set_identity / clip_ref / answer_notes, slots 31/x."""
    return {
        "version": 1,
        "sessions": [{"id": "s-legacy", "opened_at": "2026-09-02T21:52:52Z",
                      "last_activity": 1789940000.0, "closed_at": None}],
        "entries": [
            {"id": "e-1", "session_id": "s-legacy", "clip_name": "A", "slot": "31/0",
             "source": "catalogue", "immediate_verdict": "keep", "terminal_state": None},
            {"id": "e-2", "session_id": "s-legacy", "clip_name": "B", "slot": "31/1",
             "source": "catalogue", "immediate_verdict": "keep", "terminal_state": None},
            {"id": "e-3", "session_id": "s-legacy", "clip_name": "C", "slot": "31/2",
             "source": "catalogue", "immediate_verdict": None, "terminal_state": None},
            {"id": "e-4", "session_id": "s-legacy", "clip_name": "D", "slot": "31/3",
             "source": "catalogue", "immediate_verdict": None, "terminal_state": None},
            {"id": "e-5", "session_id": "s-legacy", "clip_name": "E", "slot": "31/4",
             "source": "catalogue", "immediate_verdict": None, "terminal_state": None},
        ],
    }


class AttestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "ledger.json")
        Path(self.path).write_text(json.dumps(legacy_ledger()))

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, entry_id, path=None):
        return next(e for e in ledger.load(path or self.path)["entries"]
                    if e["id"] == entry_id)

    def test_preview_writes_nothing(self):
        original = Path(self.path).read_bytes()
        result = ledger.attest("s-legacy", {"e-1": "adopted"}, confirm=False,
                               path=self.path)
        self.assertTrue(result["ok"])
        self.assertEqual(Path(self.path).read_bytes(), original)
        self.assertIsNone(self.row("e-1").get("attestation"))

    def test_confirm_attests_legacy_night_in_its_own_stratum(self):
        result = ledger.attest(
            "s-legacy",
            {"e-1": "adopted", "e-2": {"fact": "adopted", "note": "in the song"},
             "e-3": "present", "e-4": "absent", "e-5": "retire"},
            confirm=True, path=self.path)
        # the verified stratum stays empty — the sweep can prove nothing here
        self.assertIsNone(result["keep_rate"])
        self.assertEqual(result["counts"], {key: 0 for key in ledger.TERMINAL_STATES})
        self.assertEqual(result["retired"], 1)
        self.assertEqual(result["unscoreable"], {"rows": 0, "judged": 0})
        # the attested stratum follows the truth table with the EXISTING verdict
        self.assertEqual(result["attested"]["counts"],
                         {"keep": 2, "drop": 0, "abandoned": 1, "unjudged": 1})
        self.assertEqual(result["attested"]["judged"], 2)
        self.assertEqual(result["attested"]["keep_rate"], 1.0)
        by_id = {row["entry_id"]: row for row in result["attested"]["entries"]}
        self.assertEqual(by_id["e-1"]["state"], "keep")
        self.assertEqual(by_id["e-1"]["fact"], "adopted")
        self.assertEqual(by_id["e-2"]["note"], "in the song")
        self.assertEqual(by_id["e-3"]["state"], "unjudged")
        self.assertEqual(by_id["e-4"]["state"], "abandoned")
        self.assertNotIn("e-5", by_id)
        # an attested night stops nagging for a sweep it can never get
        summary = ledger.summary(path=self.path)
        self.assertEqual(summary["open_session_awaiting_sweep"], 0)
        self.assertEqual(summary["advice"], [])
        self.assertEqual(summary["scoreboard"]["attested"],
                         {"attested_sessions": 1, "judged": 2, "keep": 2,
                          "keep_rate": 1.0})
        self.assertEqual(summary["scoreboard"]["verified"],
                         {"definitive_sessions": 0, "judged": 0, "keep": 0,
                          "keep_rate": None})
        self.assertFalse(summary["freeze"]["met"])
        self.assertIn("3 definitive sessions", summary["freeze"]["rule"])

    def test_legacy_night_stays_open_until_every_row_answered(self):
        # Partial attest: the identity-less night stays open and keeps
        # nagging for the remaining rows.
        ledger.attest("s-legacy", {"e-1": "adopted", "e-2": "adopted"},
                      confirm=True, path=self.path)
        session = ledger.load(self.path)["sessions"][0]
        self.assertIsNone(session.get("closed_at"))
        advice = ledger.summary(path=self.path)["advice"]
        self.assertTrue(any("s-legacy" in item and "attest or retire" in item
                            for item in advice),
                        "remaining rows stay in the advice: %r" % (advice,))
        # No sweep can close it either: with an explicit id a foreign Set is
        # refused, and without one there is no chain to match.
        foreign = {"set_id": "set-A", "runtime_epoch": "run-1", "file_path": None}
        refused = ledger.close_session(
            {"set_identity": foreign, "complete": True, "errors": [], "clips": []},
            session_id="s-legacy", path=self.path)
        self.assertFalse(refused["ok"])
        session = ledger.load(self.path)["sessions"][0]
        self.assertIsNone(session.get("closed_at"))
        self.assertIsNone(session.get("swept_at"))
        ledger.close_session([], path=self.path)
        self.assertIsNone(ledger.load(self.path)["sessions"][0].get("swept_at"))
        # Answering every remaining row closes it as attested: the verified
        # scoreboard stays empty (judged 0) while the attested one counts them.
        result = ledger.attest(
            "s-legacy",
            {"e-3": "present", "e-4": "absent", "e-5": "retire"},
            confirm=True, path=self.path)
        session = ledger.load(self.path)["sessions"][0]
        self.assertIsNotNone(session.get("closed_at"))
        self.assertEqual(session.get("closed_by"), "attested")
        self.assertEqual(result["attested"]["counts"],
                         {"keep": 2, "drop": 0, "abandoned": 1, "unjudged": 1})
        self.assertEqual(result["attested"]["judged"], 2)
        scoreboard = ledger.summary(path=self.path)["scoreboard"]
        self.assertEqual(scoreboard["verified"]["judged"], 0)
        self.assertEqual(scoreboard["attested"]["judged"], 2)
        self.assertEqual(scoreboard["attested"]["keep"], 2)
        self.assertEqual(ledger.summary(path=self.path)["advice"], [])

    def test_attest_before_any_sweep_is_refused(self):
        # A scoreable row is still sweepable before the first complete
        # sweep: the owner's word cannot pre-empt the evidence.
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "l.json")
            identity = {"set_id": "set-A", "runtime_epoch": "run-1", "file_path": None}
            ref = {"clip_id": "c1", "track_id": "t1", "track": "SIDEKICK JAM",
                   "surface": "session", "path": "3/0"}
            entry = ledger.record_write(
                clip_name="idea", slot="3/0", source="catalogue", gear=5,
                set_identity=identity, clip_ref=ref, path=path)
            ledger.record_verdict("keep", entry_id=entry["id"], path=path)
            session_id = ledger.load(path)["sessions"][0]["id"]
            with self.assertRaisesRegex(ValueError, "sweep"):
                ledger.attest(session_id, {entry["id"]: "present"},
                              confirm=True, path=path)

    def test_freeze_needs_three_scored_nights(self):
        # Eight judged rows across two nights is not a freeze: the rule
        # needs three definitive sessions.
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "l.json")
            identity = {"set_id": "set-A", "runtime_epoch": "run-1", "file_path": None}
            for night in range(2):
                for index in range(4):
                    entry = ledger.record_write(
                        clip_name="k%d%d" % (night, index), slot="3/%d" % index,
                        source="catalogue", gear=5, set_identity=identity,
                        clip_ref={"clip_id": "c%d%d" % (night, index),
                                  "track_id": "t1", "track": "SIDEKICK JAM",
                                  "surface": "session",
                                  "path": "3/%d" % index}, path=path)
                    ledger.record_verdict("keep", entry_id=entry["id"], path=path)
                ledger.close_session(
                    {"set_identity": identity, "complete": True, "errors": [],
                     "clips": []}, path=path)   # keeps absent -> drops: rate 0.0
                identity = {**identity, "set_id": "set-B%d" % night}
            summary = ledger.summary(path=path)
            self.assertEqual(summary["scoreboard"]["verified"]["judged"], 8)
            self.assertEqual(summary["scoreboard"]["verified"]["definitive_sessions"], 2)
            self.assertFalse(summary["freeze"]["met"])

    def test_identical_attest_is_idempotent(self):
        # Re-sending the owner's word changes nothing and raises nothing.
        first = ledger.attest("s-legacy", {"e-3": "present"}, confirm=True,
                              path=self.path)
        second = ledger.attest("s-legacy", {"e-3": "present"}, confirm=True,
                               path=self.path)
        self.assertEqual(second["attested"]["judged"], first["attested"]["judged"])
        self.assertEqual(self.row("e-3", path=self.path)["attestation"]["fact"],
                         "present")
        with self.assertRaisesRegex(ValueError, "immutable"):
            ledger.attest("s-legacy", {"e-3": "absent"}, confirm=True,
                          path=self.path)

    def test_attestation_never_creates_a_verdict(self):
        ledger.attest("s-legacy", {"e-3": "absent"}, confirm=True, path=self.path)
        row = self.row("e-3")
        self.assertIsNone(row["immediate_verdict"])
        self.assertEqual(row["attestation"]["fact"], "absent")
        with self.assertRaisesRegex(ValueError, "immutable"):
            ledger.attest("s-legacy", {"e-3": "present"}, confirm=True, path=self.path)

    def test_attesting_verified_row_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "l.json")
            identity = {"set_id": "set-A", "runtime_epoch": "run-1", "file_path": None}
            entry = ledger.record_write(
                clip_name="kept", slot="3/0", source="catalogue", gear=5,
                set_identity=identity,
                clip_ref={"clip_id": "c1", "track_id": "t1", "track": "SIDEKICK JAM",
                          "surface": "session", "path": "3/0"}, path=path)
            ledger.record_verdict("keep", entry_id=entry["id"], path=path)
            ledger.close_session(
                {"set_identity": identity, "complete": True, "errors": [],
                 "clips": [{"clip_id": "c1", "track_id": "t1", "track": "SIDEKICK JAM",
                            "surface": "session", "path": "3/0"}]}, path=path)
            with self.assertRaisesRegex(ValueError, "verified"):
                ledger.attest(ledger.load(path)["sessions"][0]["id"],
                              {entry["id"]: "present"}, confirm=True, path=path)

    def test_attest_and_retire_refuse_a_verified_drop(self):
        # A provisional DROP written by a complete sweep is verified evidence:
        # neither attest nor retire may remove or override it.
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "l.json")
            identity = {"set_id": "set-A", "runtime_epoch": "run-1", "file_path": None}
            other = {"set_id": "set-A", "runtime_epoch": "run-2", "file_path": None}
            keep_notes = [{"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90},
                          {"pitch": 62, "start": 1.0, "duration": 0.5, "velocity": 90},
                          {"pitch": 64, "start": 2.0, "duration": 0.5, "velocity": 90},
                          {"pitch": 65, "start": 3.0, "duration": 0.5, "velocity": 90}]
            drop_notes = [{"pitch": 61, "start": 0.0, "duration": 0.5, "velocity": 90},
                          {"pitch": 63, "start": 1.0, "duration": 0.5, "velocity": 90},
                          {"pitch": 66, "start": 2.0, "duration": 0.5, "velocity": 90},
                          {"pitch": 68, "start": 3.0, "duration": 0.5, "velocity": 90}]
            pending_notes = [{"pitch": 70, "start": 0.0, "duration": 0.5, "velocity": 90},
                             {"pitch": 72, "start": 1.0, "duration": 0.5, "velocity": 90},
                             {"pitch": 74, "start": 2.0, "duration": 0.5, "velocity": 90},
                             {"pitch": 75, "start": 3.0, "duration": 0.5, "velocity": 90}]

            def write(clip_id, notes, verdict):
                ref = {"clip_id": clip_id, "track_id": "t1", "track": "SIDEKICK JAM",
                       "surface": "session", "path": "3/0",
                       "answer_notes": [dict(n) for n in notes]}
                entry = ledger.record_write(
                    clip_name=clip_id, slot="3/0", source="catalogue", gear=5,
                    set_identity=identity, clip_ref=ref,
                    answer_notes=[dict(n) for n in notes], path=path)
                ledger.record_verdict(verdict, entry_id=entry["id"], path=path)
                return entry

            kept = write("c1", keep_notes, "keep")
            dropped = write("c2", drop_notes, "drop")
            pending = write("c3", pending_notes, "keep")
            session_id = ledger.load(path)["sessions"][0]["id"]
            before = ledger.close_session(
                {"set_identity": other, "complete": True, "errors": [],
                 "clips": [{"clip_id": "c1", "track_id": "t1", "track": "SIDEKICK JAM",
                            "surface": "session", "path": "3/0",
                            "answer_notes": [dict(n) for n in keep_notes]}]},
                path=path)
            drop_row = next(e for e in ledger.load(path)["entries"]
                            if e["id"] == dropped["id"])
            # verified DROP with no presence clip: provisional, still verified
            self.assertEqual(drop_row.get("provisional_state"), "drop")
            self.assertIsNone(drop_row.get("presence"))
            with self.assertRaisesRegex(ValueError, "verified"):
                ledger.attest(session_id, {dropped["id"]: "retire"},
                              confirm=True, path=path)
            with self.assertRaisesRegex(ValueError, "verified"):
                ledger.attest(session_id, {dropped["id"]: "present"},
                              confirm=True, path=path)
            after = ledger.close_session(
                {"set_identity": other, "complete": True, "errors": [],
                 "clips": [{"clip_id": "c1", "track_id": "t1", "track": "SIDEKICK JAM",
                            "surface": "session", "path": "3/0",
                            "answer_notes": [dict(n) for n in keep_notes]}]},
                path=path)
            self.assertEqual(after["counts"], before["counts"])
            self.assertEqual(after["judged"], before["judged"])
            # a pending row with no verified state may still retire
            ledger.attest(session_id, {pending["id"]: "retire"},
                          confirm=True, path=path)
            self.assertTrue(self.row(pending["id"], path=path).get("retired"))

    def test_attest_refuses_scoreable_drop_with_open_candidates(self):
        # A scoreable DROP row stays refused even when its only state is an
        # open adoption question — his word already settled it.
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "l.json")
            identity = {"set_id": "set-A", "runtime_epoch": "run-1", "file_path": None}
            ref = {"clip_id": "c1", "track_id": "t1", "track": "SIDEKICK JAM",
                   "surface": "session", "path": "3/0"}
            entry = ledger.record_write(
                clip_name="dropped", slot="3/0", source="catalogue", gear=5,
                set_identity=identity, clip_ref=ref, path=path)
            ledger.record_verdict("drop", entry_id=entry["id"], path=path)
            session_id = ledger.load(path)["sessions"][0]["id"]
            data = ledger.load(path)
            for row in data["entries"]:
                if row["id"] == entry["id"]:
                    row["adoption_candidates"] = [
                        {"clip_id": "copy-1", "track": "BASS",
                         "tier": "contained_copy", "transposition": 0}]
            Path(path).write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "verified"):
                ledger.attest(session_id, {entry["id"]: "present"},
                              confirm=True, path=path)

    def test_not_adopted_declines_candidate_and_row_stays_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "l.json")
            identity = {"set_id": "set-A", "runtime_epoch": "run-1", "file_path": None}
            entry = ledger.record_write(
                clip_name="kept", slot="3/0", source="catalogue", gear=5,
                set_identity=identity,
                clip_ref={"clip_id": "c1", "track_id": "t1", "track": "SIDEKICK JAM",
                          "surface": "session", "path": "3/0"}, path=path)
            ledger.record_verdict("keep", entry_id=entry["id"], path=path)
            ledger.close_session(
                {"set_identity": identity, "complete": True, "errors": [],
                 "clips": []}, path=path)
            row = self.row(entry["id"], path=path)
            # a complete sweep verified the clip absent; an adoption candidate
            # was recorded against it (as the adoption scan writes them,
            # entry-level, since step 8)
            self.assertEqual(row["presence"]["state"], "absent")
            candidates = [{"clip_id": "copy-1", "track": "BASS",
                           "tier": "near_copy", "transposition": 0}]
            data = ledger.load(path)
            data["entries"] = [{**e, "adoption_candidates": candidates}
                               if e["id"] == entry["id"] else e
                               for e in data["entries"]]
            Path(path).write_text(json.dumps(data))
            result = ledger.attest(ledger.load(path)["sessions"][0]["id"],
                                   {entry["id"]: "not_adopted"},
                                   confirm=True, path=path)
            row = self.row(entry["id"], path=path)
            self.assertEqual(row["terminal_state"], "drop")  # verified, unchanged
            self.assertIsNone(row["presence"]["adoption"])
            self.assertNotIn("adoption_candidates", row)
            self.assertEqual(row["attestation"]["fact"], "not_adopted")
            # not_adopted never lands in the attested stratum
            self.assertEqual(result["attested"]["judged"], 0)
            self.assertEqual(result["counts"]["drop"], 1)

    def test_scoreboard_and_freeze_need_verified_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "l.json")
            identity = {"set_id": "set-A", "runtime_epoch": "run-1", "file_path": None}
            for night in range(3):
                for index in range(3):
                    entry = ledger.record_write(
                        clip_name="k%d%d" % (night, index), slot="3/%d" % index,
                        source="catalogue", gear=5, set_identity=identity,
                        clip_ref={"clip_id": "c%d%d" % (night, index), "track_id": "t1",
                                  "track": "SIDEKICK JAM", "surface": "session",
                                  "path": "3/%d" % index}, path=path)
                    ledger.record_verdict("keep", entry_id=entry["id"], path=path)
                ledger.close_session(
                    {"set_identity": identity, "complete": True, "errors": [],
                     "clips": []}, path=path)   # keeps absent -> drops: rate 0.0
                # a different Set per night so each sweep closes its own session
                identity = {**identity, "set_id": "set-B%d" % night}
            summary = ledger.summary(path=path)
            self.assertEqual(summary["scoreboard"]["verified"],
                             {"definitive_sessions": 3, "judged": 9, "keep": 0,
                              "keep_rate": 0.0})
            self.assertTrue(summary["freeze"]["met"])


class AttestMCPBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_attestation_through_mcp_needs_no_live(self):
        from mcp_server import live_write, server

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ledger.json")
            Path(path).write_text(json.dumps(legacy_ledger()))
            with patch.object(ledger, "LEDGER_PATH", path), \
                    patch.object(live_write, "read_set_inventory",
                                 AsyncMock(side_effect=ConnectionError("Live down"))):
                result = await server.mcp.call_tool(
                    "close_session", {"session_id": "s-legacy",
                                      "attest": {"e-1": "adopted", "e-2": "adopted",
                                                 "e-3": "present", "e-4": "absent",
                                                 "e-5": "retire"},
                                      "confirm": True})
            envelope = json.loads(result[0].text)
            self.assertTrue(envelope["ok"], envelope)
            self.assertEqual(envelope["attested"]["keep_rate"], 1.0)
            self.assertIsNone(envelope["keep_rate"])

    async def test_partial_attest_persists_when_live_is_down(self):
        # Rows remain on an identity-less night and Live is unreachable:
        # the attestation is still written and returned without Live contact.
        from mcp_server import live_write, server

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ledger.json")
            Path(path).write_text(json.dumps(legacy_ledger()))
            with patch.object(ledger, "LEDGER_PATH", path), \
                    patch.object(live_write, "read_set_inventory",
                                 AsyncMock(side_effect=ConnectionError("Live down"))):
                result = await server.mcp.call_tool(
                    "close_session", {"session_id": "s-legacy",
                                      "attest": {"e-1": "adopted"},
                                      "confirm": True})
            envelope = json.loads(result[0].text)
            self.assertTrue(envelope["ok"], envelope)
            rows = {e["id"]: e
                    for e in ledger.load(path)["entries"]}
            self.assertEqual(rows["e-1"]["attestation"]["fact"], "adopted")
            self.assertIsNone(ledger.load(path)["sessions"][0].get("closed_at"))
            advice = ledger.summary(path=path)["advice"]
            self.assertTrue(any("attest or retire" in item for item in advice))

    async def test_attest_without_session_id_is_a_structured_error(self):
        from mcp_server import server

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ledger.json")
            Path(path).write_text(json.dumps(legacy_ledger()))
            with patch.object(ledger, "LEDGER_PATH", path):
                result = await server.mcp.call_tool(
                    "close_session", {"attest": {"e-1": "adopted"},
                                      "confirm": True})
            envelope = json.loads(result[0].text)
            self.assertFalse(envelope["ok"])
            self.assertIn("session id", envelope["error"])


if __name__ == "__main__":
    unittest.main()
