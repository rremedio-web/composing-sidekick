"""Close-session failure honesty and unscoreable surgery edits."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from mcp_server import ledger, live_write, pp_session, server

NOTES = [{"pitch": 60, "start": 0, "duration": 1, "velocity": 80}]


class CloseSessionErrorMappingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger_path = os.path.join(self._tmp.name, "ledger.json")
        patcher = mock.patch.object(ledger, "LEDGER_PATH", self.ledger_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        # A pending scoreable row makes close_session reach for the inventory.
        ledger.record_write(
            clip_name="unswept", slot="3/0", source="catalogue",
            answer_notes=NOTES,
            set_identity={"set_id": "err-set", "runtime_epoch": "r1",
                          "file_path": None},
            clip_ref={"clip_id": "c1", "track_id": "jam",
                      "track": "SIDEKICK JAM", "surface": "session",
                      "path": "t3/s0", "answer_notes": NOTES},
            path=self.ledger_path)

    def close_with(self, exc):
        with mock.patch.object(live_write, "read_set_inventory",
                               side_effect=exc):
            return server.close_session()

    def assert_left_open(self, out, fragment):
        self.assertFalse(out["ok"])
        self.assertIn(fragment, out["note"])
        self.assertIn("left OPEN", out["note"])

    def test_attest_survives_a_refused_sweep_with_its_report(self):
        """fix2 item 6: when close_session(attest=…, confirm=True) saves the
        attestation and THEN cannot sweep, the attest report must come back
        beside the failure — the owner's word was recorded either way."""
        # one identity-less row to attest, plus the scoreable row from setUp
        idless = ledger.record_write(
            clip_name="legacy", slot="3/1", source="catalogue",
            path=self.ledger_path)
        session_id = ledger.load(self.ledger_path)["sessions"][0]["id"]
        with mock.patch.object(live_write, "read_set_inventory",
                               side_effect=ConnectionError("refused")):
            out = server.close_session(
                session_id=session_id, attest={idless["id"]: "present"},
                confirm=True)
        self.assertFalse(out["ok"])
        self.assertIn("Producer Pal unreachable", out["note"])
        self.assertTrue(out["attest_applied"])
        report = out["attest_report"]
        self.assertTrue(report["ok"])
        self.assertEqual(report["attested"]["counts"]["unjudged"], 1)
        row = next(e for e in ledger.load(self.ledger_path)["entries"]
                   if e["id"] == idless["id"])
        self.assertEqual(row["attestation"]["fact"], "present")
        self.assertIsNone(ledger.load(self.ledger_path)["sessions"][0]
                          .get("closed_at"))

    def test_live_set_identity_error_names_the_real_reason(self):
        self.assert_left_open(
            self.close_with(live_write.LiveSetIdentityError(
                "live_set identity", 0, None, detail="ppal-live-api missing")),
            "Live Set identity is unavailable")

    def test_connection_error_is_producer_pal_unreachable(self):
        self.assert_left_open(self.close_with(ConnectionError("refused")),
                              "Producer Pal unreachable")

    def test_os_error_is_producer_pal_unreachable(self):
        self.assert_left_open(self.close_with(OSError("down")),
                              "Producer Pal unreachable")

    def test_protocol_error_is_a_version_mismatch(self):
        self.assert_left_open(
            self.close_with(pp_session.PPProtocolError("handshake rejected")),
            "protocol/version mismatch")

    def test_unknown_exception_reports_its_type(self):
        self.assert_left_open(self.close_with(RuntimeError("boom")),
                              "unexpected RuntimeError")


class SurgeryUnscoreableTests(unittest.TestCase):
    def test_identity_less_verified_edit_is_flagged_unscoreable(self):
        def verified_without_identity(path, bar, surgery, confirm=False):
            return {"status": "verified", "path": path,
                    "answer_notes": surgery["notes"]}

        with mock.patch.object(server, "_run_async",
                               side_effect=lambda value: value), \
                mock.patch("mcp_server.clip_mutation.surgery_clip",
                           new=verified_without_identity):
            result = server.surgery_clip("t1/s0", 1, {"notes": NOTES},
                                         confirm=True)
        self.assertEqual(result["status"], "verified")
        self.assertFalse(result["scoreable"])
        self.assertIn("cannot be scored", result["scoreable_reason"])

    def test_tracked_edit_keeps_stamp_path(self):
        with tempfile.TemporaryDirectory() as td:
            ledger_path = os.path.join(td, "ledger.json")
            identity = {"set_id": "surg-set", "runtime_epoch": "r1",
                        "file_path": None}
            entry = ledger.record_write(
                clip_name="loop", slot="t1/s0", source="catalogue",
                answer_notes=NOTES, set_identity=identity,
                clip_ref={"clip_id": "clip-1", "track_id": "t1",
                          "track": "SIDEKICK AUD", "surface": "session",
                          "path": "t1/s0", "answer_notes": NOTES},
                path=ledger_path)
            prior_ref = dict(entry["clip_ref"])

            def verified(path, bar, surgery, confirm=False):
                return {"status": "verified", "path": path,
                        "prior_clip_ref": prior_ref, "set_identity": identity,
                        "answer_notes": surgery["notes"],
                        "clip_ref": {**prior_ref, "answer_notes": NOTES}}

            with mock.patch.object(ledger, "LEDGER_PATH", ledger_path), \
                    mock.patch.object(server, "_run_async",
                                      side_effect=lambda value: value), \
                    mock.patch("mcp_server.clip_mutation.surgery_clip",
                               new=verified):
                result = server.surgery_clip("t1/s0", 1, {"notes": NOTES},
                                             confirm=True)
            self.assertEqual(result["ledger_entry_id"], entry["id"])
            self.assertNotIn("scoreable_reason", result)
            rows = ledger.load(ledger_path)["entries"]
            self.assertEqual(rows[0]["revision"], 2)


if __name__ == "__main__":
    unittest.main()
