"""fix2 item 1 — a legacy identity-less night must never swallow tonight's Set.

Scenario (from .scratch/batch1/repro/real/merge.py, on the real 2026-09-02
ledger shape): after attesting A and B, a verdict on a stale legacy row used
to bump the dead night's last_activity, tonight's first write then joined it,
the night adopted tonight's Set identity, and close_session sealed the merged
night "definitive" while the unattested rows C and R vanished from advice.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mcp_server import ledger

TODAY = {"set_id": "/Users/r/Today.als", "runtime_epoch": "rt-today",
         "file_path": "/Users/r/Today.als"}
LEGACY_LAST_ACTIVITY = 1_788_387_757.9  # the real 2026-09-02 night
NOTES = [{"pitch": p, "start": i * 0.5, "duration": 0.25, "velocity": 90}
         for i, p in enumerate([60, 63, 67, 70, 72, 75, 77, 79])]


def legacy_ledger() -> dict:
    """The real 09-02 night: one session, five identity-less rows, A/B judged."""
    rows = []
    for i, (eid, name, verdict) in enumerate((
            ("e-8d2661103a", "Block A", "keep"),
            ("e-d6d2701dcd", "Block B", "keep"),
            ("e-45b0eae50f", "C counter 69", None),
            ("e-b4c8e1ab59", "R recall trauma 69", None),
            ("e-2b9c347a87", "Block D", None))):
        rows.append({
            "id": eid, "session_id": "s-3a9760393f",
            "written_at": "2026-09-02T21:52:52Z", "track": "SIDEKICK JAM",
            "slot": "31/%d" % i, "clip_name": name, "source": "catalogue",
            "immediate_verdict": verdict, "terminal_state": None,
            **({"judged_at": "2026-09-02T22:10:00Z"} if verdict else {}),
        })
    return {
        "version": 1,
        "sessions": [{"id": "s-3a9760393f",
                      "opened_at": "2026-09-02T21:52:52Z",
                      "last_activity": LEGACY_LAST_ACTIVITY,
                      "closed_at": None}],
        "entries": rows,
    }


class LegacyNightBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "ledger.json")
        Path(self.path).write_text(json.dumps(legacy_ledger()))
        # "now" is weeks after the legacy night: it is idle, effectively ended
        self.now = LEGACY_LAST_ACTIVITY + 20 * 24 * 3600

    def session(self, sid="s-3a9760393f"):
        return next(s for s in ledger.load(self.path)["sessions"]
                    if s["id"] == sid)

    def test_bare_judgment_before_tonights_write_refuses(self):
        binding = ledger.resolve_judgment_target(
            ledger.load(self.path), "drop", now=self.now)
        self.assertTrue(binding["refused"])
        self.assertIn("no active session", binding["error"])
        # the legacy rows are all identity-less — judged or not, only
        # attestation settles them, so the refusal lists them separately
        # from verdict-pending rows
        self.assertEqual(binding["pending_ids"], [])
        self.assertEqual(sorted(binding["attest_only"]),
                         sorted(["e-8d2661103a", "e-d6d2701dcd", "e-45b0eae50f",
                                 "e-b4c8e1ab59", "e-2b9c347a87"]))

    def test_stale_row_verdict_never_resurrects_the_night(self):
        with mock.patch.object(ledger, "_now", return_value=self.now):
            stamped = ledger.record_verdict("drop", entry_id="e-2b9c347a87",
                                            path=self.path)
        self.assertEqual(stamped["immediate_verdict"], "drop")
        self.assertEqual(self.session()["last_activity"], LEGACY_LAST_ACTIVITY)

    def test_tonights_write_opens_its_own_session_and_legacy_adopts_nothing(self):
        ref = {"clip_id": "new1", "track_id": "tj", "track": "SIDEKICK JAM",
               "surface": "session", "path": "5/0", "answer_notes": NOTES}
        with mock.patch.object(ledger, "_now", return_value=self.now + 60):
            entry = ledger.record_write(
                clip_name="new", slot="5/0", source="catalogue", gear=5,
                set_identity=TODAY, clip_ref=ref, answer_notes=NOTES,
                path=self.path)
            ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
        self.assertNotEqual(entry["session_id"], "s-3a9760393f")
        legacy = self.session()
        self.assertIsNone(legacy.get("set_identity"))
        self.assertIsNotNone(legacy.get("ended_at"),
                             "the dead night is ended, not resurrected")

    def test_merge_scenario_keeps_c_and_r_advised(self):
        """The full merge.py flow: attest A,B; judge D; write tonight; close."""
        ledger.attest("s-3a9760393f",
                      {"e-8d2661103a": "adopted", "e-d6d2701dcd": "adopted"},
                      confirm=True, path=self.path)
        with mock.patch.object(ledger, "_now", return_value=self.now):
            ledger.record_verdict("drop", entry_id="e-2b9c347a87",
                                  path=self.path)
        ref = {"clip_id": "new1", "track_id": "tj", "track": "SIDEKICK JAM",
               "surface": "session", "path": "5/0", "answer_notes": NOTES}
        with mock.patch.object(ledger, "_now", return_value=self.now + 60):
            entry = ledger.record_write(
                clip_name="new", slot="5/0", source="catalogue", gear=5,
                set_identity=TODAY, clip_ref=ref, answer_notes=NOTES,
                path=self.path)
            ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
        with mock.patch.object(ledger, "_now", return_value=self.now + 600):
            report = ledger.close_session(
                {"set_identity": TODAY, "complete": True, "errors": [],
                 "clips": [dict(ref, name="new")]}, path=self.path)

        # tonight is its own definitive night; the legacy night is untouched
        self.assertNotIn("s-3a9760393f", report["session_ids"])
        legacy = self.session()
        self.assertIsNone(legacy.get("closed_at"))
        self.assertIsNone(legacy.get("set_identity"))
        # C and R are still waiting for the owner's word
        advice = ledger.summary(self.path)["advice"]
        self.assertTrue(any("s-3a9760393f" in item and "attest or retire" in item
                            for item in advice),
                        "unattested rows must stay in advice: %r" % (advice,))


class SweepCannotCloseUnattestedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "ledger.json")
        self.identity = {"set_id": "set-mixed", "runtime_epoch": "run-1",
                         "file_path": None}
        self.t0 = time.time() - 3600

    def _write(self, name, clip_id, *, identity, notes):
        ref = {"clip_id": clip_id, "track_id": "tj", "track": "SIDEKICK JAM",
               "surface": "session", "path": "3/0", "answer_notes": notes}
        with mock.patch.object(ledger, "_now", return_value=self.t0):
            entry = ledger.record_write(
                clip_name=name, slot="3/0", source="catalogue", gear=5,
                set_identity=identity, clip_ref=ref, answer_notes=notes,
                path=self.path)
            ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
            return entry

    def test_sweep_never_closes_a_night_with_unattested_identityless_rows(self):
        """A mid-night identity read failure leaves an identity-less row
        beside scoreable ones: the sweep scores what it can but the night
        stays open until the owner attests the orphan."""
        notes = [{"pitch": 60 + i, "start": 0.5 * i, "duration": 0.25,
                  "velocity": 90} for i in range(8)]
        scored = self._write("scored", "clip-scored", identity=self.identity,
                             notes=notes)
        orphan = self._write("orphan", "clip-orphan", identity=None,
                             notes=[dict(n, pitch=n["pitch"] + 12)
                                    for n in notes])
        report = ledger.close_session(
            {"set_identity": self.identity, "complete": True, "errors": [],
             "clips": [dict(scored["clip_ref"], name="scored")]},
            path=self.path)
        self.assertEqual(report["rate_status"], "partial")
        self.assertEqual(report["unscoreable"], {"rows": 1, "judged": 1})
        session = ledger.load(self.path)["sessions"][0]
        self.assertIsNone(session.get("closed_at"))
        self.assertTrue(any("attest or retire" in item
                            for item in ledger.summary(self.path)["advice"]))

    def test_summary_advises_unattested_rows_even_in_closed_sessions(self):
        """History (or an old bug) may have closed a night around unattested
        identity-less rows; the advice must not go quiet for them."""
        scored = self._write("scored", "clip-scored", identity=self.identity,
                             notes=[{"pitch": 60 + i, "start": 0.5 * i,
                                     "duration": 0.25, "velocity": 90}
                                    for i in range(8)])
        orphan = self._write("orphan", "clip-orphan", identity=None,
                             notes=[{"pitch": 72 + i, "start": 0.5 * i,
                                     "duration": 0.25, "velocity": 90}
                                    for i in range(8)])
        ledger.close_session(
            {"set_identity": self.identity, "complete": True, "errors": [],
             "clips": [dict(scored["clip_ref"], name="scored")]},
            path=self.path)
        # simulate the pre-fix state: the night got closed around the orphan
        data = ledger.load(self.path)
        data["sessions"][0]["closed_at"] = "2026-09-01T00:00:00.000000Z"
        data["sessions"][0]["closed_by"] = "sweep"
        data["sessions"][0]["rate_status"] = "definitive"
        with open(self.path, "w") as handle:
            json.dump(data, handle)
        advice = ledger.summary(self.path)["advice"]
        self.assertTrue(any("attest or retire" in item and "identity-less" in item
                            for item in advice),
                        "closed nights keep nagging too: %r" % (advice,))


if __name__ == "__main__":
    unittest.main()
