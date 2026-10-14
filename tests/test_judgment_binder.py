"""The one judgment binder: every verdict lands on a row it named, or refuses."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from mcp_server import ledger

NOTES = [{"pitch": 60, "start": 0, "duration": 1, "velocity": 80}]
OTHER_NOTES = [{"pitch": 64, "start": 0, "duration": 1, "velocity": 80}]
IDENTITY = {"set_id": "binder-set", "runtime_epoch": "run-1",
            "file_path": "/tmp/binder.als"}
T0 = 1_800_000_000.0  # arbitrary fixed epoch; tests pass `now` explicitly


class BinderFixture:
    """Shared helpers: a temp ledger plus quick writes."""

    def setup_ledger(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "ledger.json")
        self._original = ledger.LEDGER_PATH
        ledger.LEDGER_PATH = self.path
        self.addCleanup(setattr, ledger, "LEDGER_PATH", self._original)

    def write(self, name, *, slot="3/0", notes=NOTES, now=T0, clip_id=None,
              identity=IDENTITY, source_phrase_id=None):
        with mock.patch.object(ledger, "_now", return_value=now):
            return ledger.record_write(
                clip_name=name, slot=slot, source="catalogue",
                answer_notes=notes, source_phrase_id=source_phrase_id,
                set_identity=identity,
                clip_ref={"clip_id": clip_id or name, "track_id": "jam",
                          "track": "SIDEKICK JAM", "surface": "session",
                          "path": "t3/s0", "answer_notes": notes},
                path=self.path)

    def data(self):
        return ledger.load(self.path)


class CanonicalSlotTests(unittest.TestCase):
    def test_legacy_spellings_fold_to_the_clip_path_form(self):
        self.assertEqual(ledger.canonical_slot("31/0"), "t31/s0")
        self.assertEqual(ledger.canonical_slot("session:31/0"), "t31/s0")
        self.assertEqual(ledger.canonical_slot("t31/s0"), "t31/s0")
        self.assertEqual(ledger.canonical_slot(" 31/0 "), "t31/s0")

    def test_path_forms_pass_through(self):
        self.assertEqual(ledger.canonical_slot("t9"), "t9")
        self.assertEqual(ledger.canonical_slot("t9/l2"), "t9/l2")
        self.assertIsNone(ledger.canonical_slot(None))

    def test_record_write_stores_canonical_slots(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "ledger.json")
            entry = ledger.record_write(clip_name="a", slot="31/0",
                                        source="catalogue", path=path)
            self.assertEqual(entry["slot"], "t31/s0")
            revised = ledger.record_write(clip_name="b", slot="session:31/1",
                                          source="catalogue", path=path,
                                          revision_of=entry["id"])
            self.assertEqual(revised["slot"], "t31/s1")


class ResolveJudgmentTargetTests(BinderFixture, unittest.TestCase):
    def setUp(self):
        self.setup_ledger()

    def test_bare_refuses_when_only_a_stale_session_exists(self):
        entry = self.write("three-weeks")
        data = self.data()
        data["sessions"][0]["last_activity"] = T0 - 3 * 7 * 24 * 3600
        binding = ledger.resolve_judgment_target(
            data, "drop", now=T0)
        self.assertFalse(binding["bound"])
        self.assertTrue(binding["refused"])
        self.assertIn("no active session", binding["error"])
        self.assertEqual(binding["pending_ids"], [entry["id"]])

    def test_bare_replays_judged_cursor_inside_fifteen_minutes(self):
        entry = self.write("cursor")
        with mock.patch.object(ledger, "_now", return_value=T0 + 60):
            ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
        inside = ledger.resolve_judgment_target(
            self.data(), "keep", now=T0 + 60 + 14 * 60)
        self.assertTrue(inside["bound"])
        self.assertTrue(inside["replay"])
        self.assertEqual(inside["entry"]["id"], entry["id"])
        outside = ledger.resolve_judgment_target(
            self.data(), "keep", now=T0 + 60 + 16 * 60)
        self.assertFalse(outside["bound"])
        self.assertTrue(outside["refused"])
        self.assertIn("already judged", outside["error"])

    def test_bare_replay_refuses_on_a_different_verdict(self):
        entry = self.write("cursor")
        with mock.patch.object(ledger, "_now", return_value=T0 + 60):
            ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
        binding = ledger.resolve_judgment_target(
            self.data(), "drop", now=T0 + 60 + 60)
        self.assertTrue(binding["refused"])

    def test_legacy_and_canonical_slot_spellings_bind_the_same_row(self):
        entry = self.write("slot-row", slot="31/0")
        by_legacy = ledger.resolve_judgment_target(
            self.data(), "keep", slot="31/0", now=T0 + 30)
        by_path = ledger.resolve_judgment_target(
            self.data(), "keep", slot="t31/s0", now=T0 + 30)
        self.assertTrue(by_legacy["bound"])
        self.assertTrue(by_path["bound"])
        self.assertEqual(by_legacy["entry"]["id"], entry["id"])
        self.assertEqual(by_path["entry"]["id"], entry["id"])

    def test_entry_id_with_wrong_slot_is_refused(self):
        entry = self.write("owned", slot="3/0")
        binding = ledger.resolve_judgment_target(
            self.data(), "drop", entry_id=entry["id"], slot="3/9", now=T0 + 30)
        self.assertTrue(binding["refused"])
        self.assertIn("does not match", binding["error"])

    def test_entry_id_unknown_is_refused(self):
        binding = ledger.resolve_judgment_target(
            self.data(), "drop", entry_id="e-missing", now=T0 + 30)
        self.assertTrue(binding["refused"])
        self.assertIn("unknown ledger entry", binding["error"])

    def test_slot_only_across_two_live_sessions_is_ambiguous(self):
        first = self.write("a", slot="3/0", now=T0,
                           identity={"set_id": "set-a", "runtime_epoch": "r1",
                                     "file_path": "/tmp/a.als"})
        second = self.write("b", slot="3/0", now=T0 + 60,
                            identity={"set_id": "set-b", "runtime_epoch": "r2",
                                      "file_path": "/tmp/b.als"})
        self.assertNotEqual(first["session_id"], second["session_id"])
        data = self.data()
        # A hand-opened pair of live sessions sharing a slot spelling.
        for session in data["sessions"]:
            session["ended_at"] = None
            session["ended_by"] = None
            session["last_activity"] = T0 + 120
        binding = ledger.resolve_judgment_target(data, "drop", slot="3/0",
                                                 now=T0 + 121)
        self.assertTrue(binding["refused"])
        self.assertIn("ambiguous", binding["error"])
        self.assertIn(first["id"], binding["error"])
        self.assertIn(second["id"], binding["error"])

    def test_late_explicit_entry_id_is_flagged(self):
        entry = self.write("sealed")
        ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
        ledger.close_session(
            {"set_identity": IDENTITY, "complete": True, "errors": [],
             "clips": [entry["clip_ref"]]}, path=self.path)
        binding = ledger.resolve_judgment_target(
            self.data(), "drop", entry_id=entry["id"], now=T0 + 120)
        self.assertTrue(binding["bound"])
        self.assertTrue(binding["late"])

    def test_answer_notes_matches_content_then_replays_judged(self):
        pending = self.write("pending", notes=NOTES, now=T0)
        judged = self.write("judged", notes=OTHER_NOTES, now=T0 + 10)
        with mock.patch.object(ledger, "_now", return_value=T0 + 20):
            ledger.record_verdict("drop", entry_id=judged["id"], path=self.path)
        to_pending = ledger.resolve_judgment_target(
            self.data(), "keep", answer_notes=NOTES, now=T0 + 30)
        self.assertTrue(to_pending["bound"])
        self.assertFalse(to_pending["replay"])
        self.assertEqual(to_pending["entry"]["id"], pending["id"])
        replay = ledger.resolve_judgment_target(
            self.data(), "drop", answer_notes=OTHER_NOTES, now=T0 + 30)
        self.assertTrue(replay["bound"])
        self.assertTrue(replay["replay"])
        self.assertEqual(replay["entry"]["id"], judged["id"])

    def test_answer_notes_no_match_is_learning_only(self):
        self.write("pending", now=T0)
        binding = ledger.resolve_judgment_target(
            self.data(), "keep",
            answer_notes=[{"pitch": 72, "start": 0, "duration": 1,
                           "velocity": 80}], now=T0 + 30)
        self.assertFalse(binding["bound"])
        self.assertTrue(binding["learning_only"])

    def test_answer_notes_ambiguity_is_refused(self):
        self.write("one", notes=NOTES, now=T0)
        self.write("two", notes=NOTES, now=T0 + 10)
        binding = ledger.resolve_judgment_target(
            self.data(), "keep", answer_notes=NOTES, now=T0 + 30)
        self.assertTrue(binding["refused"])
        self.assertIn("ambiguous", binding["error"])


class BinderSettledRowTests(BinderFixture, unittest.TestCase):
    """fix3 item 2: attested/retired/superseded rows are never re-judged,
    idle nights score as late, and refusals separate attest-only rows."""

    def setUp(self):
        self.setup_ledger()

    def write_idless(self, name, slot="3/0", notes=NOTES):
        with mock.patch.object(ledger, "_now", return_value=T0):
            return ledger.record_write(
                clip_name=name, slot=slot, source="catalogue",
                answer_notes=notes, path=self.path)

    def test_attested_row_cannot_be_rejudged_by_entry_id(self):
        """The attested-C re-judge from the real ledger: an explicit entry id
        on an attested row must refuse, not quietly restamp the row and
        change the attested stratum after the fact."""
        entry = self.write_idless("c-counter")
        ledger.attest(entry["session_id"], {entry["id"]: "present"},
                      confirm=True, path=self.path)
        binding = ledger.resolve_judgment_target(
            self.data(), "drop", entry_id=entry["id"], now=T0 + 60)
        self.assertTrue(binding["refused"])
        self.assertIn("attested", binding["error"])
        row = next(e for e in ledger.load(self.path)["entries"]
                   if e["id"] == entry["id"])
        self.assertIsNone(row["immediate_verdict"])

    def test_retired_and_superseded_rows_are_refused_too(self):
        retired = self.write_idless("retired-row")
        ledger.attest(retired["session_id"], {retired["id"]: "retire"},
                      confirm=True, path=self.path)
        binding = ledger.resolve_judgment_target(
            self.data(), "keep", entry_id=retired["id"], now=T0 + 60)
        self.assertTrue(binding["refused"])
        self.assertIn("retired", binding["error"])

        # superseded rows (a shape revisions may produce): the judgment
        # belongs to the replacement, never the superseded row
        superseded = self.write_idless("revised-row", slot="3/1",
                                       notes=OTHER_NOTES)
        data = self.data()
        data["entries"][-1]["superseded_by"] = "e-replacement"
        with open(self.path, "w") as handle:
            json.dump(data, handle)
        binding = ledger.resolve_judgment_target(
            self.data(), "keep", entry_id=superseded["id"], now=T0 + 60)
        self.assertTrue(binding["refused"])
        self.assertIn("superseded", binding["error"])

    def test_late_only_when_the_night_was_already_sealed(self):
        """fix4 R3: late is ONE rule, the scorer's — the night's evidence
        was sealed (a complete sweep ran, or the night closed) before the
        verdict landed. An ended or long-idle but never-swept night is NOT
        late: its verdict still scores normally."""
        entry = self.write("idle-night")
        idle = ledger.resolve_judgment_target(
            self.data(), "keep", entry_id=entry["id"],
            now=T0 + ledger.SESSION_IDLE_SECONDS + 60)
        self.assertTrue(idle["bound"])
        self.assertFalse(idle["late"])

        swept = ledger.close_session(
            {"set_identity": IDENTITY, "complete": True, "errors": [],
             "clips": [entry["clip_ref"]]}, path=self.path)
        self.assertEqual(swept["rate_status"], "definitive")
        sealed = ledger.resolve_judgment_target(
            self.data(), "keep", entry_id=entry["id"],
            now=T0 + ledger.SESSION_IDLE_SECONDS + 120)
        self.assertTrue(sealed["bound"])
        self.assertTrue(sealed["late"])

    def test_bare_refusal_lists_identityless_rows_as_attest_only(self):
        self.write_idless("idless")
        binding = ledger.resolve_judgment_target(
            self.data(), "drop", now=T0 + 30 * 24 * 3600)
        self.assertTrue(binding["refused"])
        self.assertIn("no active session", binding["error"])
        self.assertEqual(binding["pending_ids"], [])
        self.assertEqual(len(binding["attest_only"]), 1)
        # fix4 nit: with nothing judgable left, the wording says what those
        # rows actually need — attest, not judgment
        self.assertIn("need attest, not judgment", binding["error"])

    def test_bare_no_cursor_fallback_honors_the_replay_window(self):
        """A legacy session without cursor fields falls back to its newest
        judgable row; once that row is judged, the fallback replays only
        inside the window like a real cursor."""
        entry = self.write("fallback-row")
        with mock.patch.object(ledger, "_now", return_value=T0 + 60):
            ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
        data = self.data()
        for session in data["sessions"]:
            session.pop("last_served_entry_id", None)
            session.pop("last_judgment_target_entry_id", None)
        inside = ledger.resolve_judgment_target(
            data, "keep", now=T0 + 60 + 5 * 60)
        self.assertTrue(inside["bound"])
        self.assertTrue(inside["replay"])
        outside = ledger.resolve_judgment_target(
            data, "keep", now=T0 + 60 + 20 * 60)
        self.assertTrue(outside["refused"])
        self.assertIn("already judged", outside["error"])

    def test_source_and_slot_refuses_settled_and_stale_replays(self):
        settled = self.write_idless("src-row")
        # a sibling row keeps the identity-less night open (attest closes
        # it only once every row is answered)
        self.write_idless("sibling-row", slot="3/1", notes=OTHER_NOTES)
        data = self.data()
        data["entries"][0]["source_phrase_id"] = "pX"
        with open(self.path, "w") as handle:
            json.dump(data, handle)
        # attested: naming it by source+slot must refuse, not silently bind
        ledger.attest(settled["session_id"], {settled["id"]: "absent"},
                      confirm=True, path=self.path)
        binding = ledger.resolve_judgment_target(
            self.data(), "drop", source_phrase_id="pX", slot="3/0", now=T0 + 60)
        self.assertTrue(binding["refused"])
        self.assertIn("attested", binding["error"])

        judged = self.write_idless("judged-row", slot="3/1", notes=OTHER_NOTES)
        data = self.data()
        data["entries"][-1]["source_phrase_id"] = "pY"
        with open(self.path, "w") as handle:
            json.dump(data, handle)
        with mock.patch.object(ledger, "_now", return_value=T0 + 100):
            ledger.record_verdict("keep", entry_id=judged["id"], path=self.path)
        inside = ledger.resolve_judgment_target(
            self.data(), "keep", source_phrase_id="pY", slot="3/1",
            now=T0 + 100 + 5 * 60)
        self.assertTrue(inside["bound"])
        self.assertTrue(inside["replay"])
        outside = ledger.resolve_judgment_target(
            self.data(), "keep", source_phrase_id="pY", slot="3/1",
            now=T0 + 100 + 20 * 60)
        self.assertTrue(outside["refused"])
        self.assertIn("entry_id", outside["error"])


class RecordVerdictStrictnessTests(BinderFixture, unittest.TestCase):
    def setUp(self):
        self.setup_ledger()

    def test_slot_mismatch_with_entry_id_raises(self):
        entry = self.write("row", slot="3/0")
        with self.assertRaisesRegex(ValueError, "does not match"):
            ledger.record_verdict("drop", entry_id=entry["id"], slot="3/9",
                                  path=self.path)

    def test_slot_only_outside_the_active_session_records_nothing(self):
        entry = self.write("row", slot="3/0")
        # The night went quiet seven hours ago: idle, so not active, so a
        # slot-only verdict has no scope and stamps nothing.
        with mock.patch.object(ledger, "_now", return_value=T0 + 7 * 3600):
            self.assertIsNone(ledger.record_verdict("drop", slot="3/0",
                                                    path=self.path))
        row = next(e for e in ledger.load(self.path)["entries"]
                   if e["id"] == entry["id"])
        self.assertIsNone(row["immediate_verdict"])

    def test_slot_only_with_two_pending_rows_records_nothing(self):
        self.write("one", slot="3/0", notes=NOTES, now=T0)
        self.write("two", slot="3/0", notes=OTHER_NOTES, now=T0 + 10)
        self.assertIsNone(ledger.record_verdict("drop", slot="3/0",
                                                path=self.path))
        rows = ledger.load(self.path)["entries"]
        self.assertTrue(all(r["immediate_verdict"] is None for r in rows))

    def test_slot_only_binds_unique_pending_row_after_one_is_judged(self):
        first = self.write("one", slot="3/0", notes=NOTES, now=T0)
        second = self.write("two", slot="3/0", notes=OTHER_NOTES, now=T0 + 10)
        with mock.patch.object(ledger, "_now", return_value=T0 + 20):
            stamped = ledger.record_verdict("drop", entry_id=second["id"],
                                            path=self.path)
        self.assertEqual(stamped["id"], second["id"])
        with mock.patch.object(ledger, "_now", return_value=T0 + 30):
            stamped = ledger.record_verdict("drop", slot="3/0", path=self.path)
        self.assertEqual(stamped["id"], first["id"])

    def test_bare_call_without_identity_records_nothing(self):
        self.write("row", slot="3/0")
        self.assertIsNone(ledger.record_verdict("drop", path=self.path))


class JudgeAnswerMCPBoundaryTests(unittest.TestCase):
    """The binder's refusals must survive the MCP tool boundary."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger_path = os.path.join(self._tmp.name, "ledger.json")
        patches = [
            mock.patch.object(ledger, "LEDGER_PATH", self.ledger_path),
        ]
        from mcp_server import develop_session, ears, gears
        patches.extend([
            mock.patch.object(ears, "SESSION_PATH",
                              os.path.join(self._tmp.name, "ears.json")),
            mock.patch.object(develop_session, "STATE_PATH",
                              os.path.join(self._tmp.name, "develop.json")),
            mock.patch.object(gears.core, "_phrases", return_value=[]),
        ])
        old_ratings = gears.core._ratings_path()
        gears.core.set_ratings_path(os.path.join(self._tmp.name, "ratings.json"))
        self.addCleanup(gears.core.set_ratings_path, old_ratings)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.server = None

    def _server(self):
        if self.server is None:
            from mcp_server import server
            self.server = server
        return self.server

    def test_bare_judgment_refusal_lists_pending_ids_through_mcp(self):
        import asyncio

        server = self._server()
        entry = ledger.record_write(
            clip_name="stale", slot="3/0", source="catalogue",
            answer_notes=NOTES, set_identity=IDENTITY,
            clip_ref={"clip_id": "stale", "track_id": "jam",
                      "track": "SIDEKICK JAM", "surface": "session",
                      "path": "t3/s0", "answer_notes": NOTES},
            path=self.ledger_path)
        data = ledger.load(self.ledger_path)
        data["sessions"][0]["last_activity"] = 1.0  # decades idle
        with open(self.ledger_path, "w") as handle:
            json.dump(data, handle)
        with self.assertRaisesRegex(Exception, "no active session"):
            asyncio.run(server.mcp.call_tool("judge_answer", {"verdict": "drop"}))
        # The refusal named the pending row the operator can judge explicitly.
        with self.assertRaisesRegex(Exception, entry["id"]):
            asyncio.run(server.mcp.call_tool("judge_answer", {"verdict": "drop"}))

    def test_explicit_entry_judges_through_mcp(self):
        import asyncio

        server = self._server()
        entry = ledger.record_write(
            clip_name="named", slot="3/0", source="catalogue",
            answer_notes=NOTES, set_identity=IDENTITY,
            clip_ref={"clip_id": "named", "track_id": "jam",
                      "track": "SIDEKICK JAM", "surface": "session",
                      "path": "t3/s0", "answer_notes": NOTES},
            path=self.ledger_path)
        result = asyncio.run(server.mcp.call_tool(
            "judge_answer", {"verdict": "keep", "entry_id": entry["id"]}))
        payload = json.loads(result[0].text)
        self.assertEqual(payload["verdict"], "keep")
        self.assertEqual(payload["ledger"]["entry_id"], entry["id"])


if __name__ == "__main__":
    unittest.main()
