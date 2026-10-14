from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from mcp_server import gears, jam_lifecycle, ledger
import mcp_server.develop_session as develop_session
import mcp_server.ears as ears


NOTES = [{"pitch": 60, "start_beats": 0.25,
          "duration_beats": 0.5, "velocity": 91}]


class TestJudgmentRepairs(unittest.TestCase):
    def test_verified_write_keeps_normalized_notes_and_controls(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "ledger.json")
            target = {"key": "D minor"}
            jam_lifecycle.stamp_verified_write(
                target, clip_name="answer", slot="1/1", source="catalogue",
                role="bass", gear=5, notes=NOTES, ledger_path=path,
            )
            row = ledger.load(path)["entries"][-1]
            self.assertEqual(row["answer_notes"],
                             [{"pitch": 60, "start": 0.25,
                               "duration": 0.5, "velocity": 91}])
            self.assertEqual(row["role"], "bass")
            self.assertEqual(row["key"], "D minor")

    def test_pending_resolver_is_exact_for_same_slot(self):
        """Slot-only binding demands ONE pending row (the judgment binder's
        rule): two unjudged rows sharing a slot refuse, and judging one by
        entry id is what makes the slot unique again."""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "ledger.json")
            first = ledger.record_write(
                clip_name="first", slot="1/1", source="catalogue",
                answer_notes=NOTES, path=path,
            )
            second = ledger.record_write(
                clip_name="second", slot="1/1", source="catalogue",
                answer_notes=[{**NOTES[0], "pitch": 64}], path=path,
            )
            data = ledger.load(path)
            binding = ledger.resolve_judgment_target(data, "drop", slot="1/1")
            self.assertTrue(binding["refused"])
            self.assertIn(first["id"], binding["error"])
            self.assertIn(second["id"], binding["error"])
            ledger.record_verdict("drop", entry_id=second["id"], path=path)
            binding = ledger.resolve_judgment_target(
                ledger.load(path), "drop", slot="1/1")
            self.assertTrue(binding["bound"])
            self.assertEqual(binding["entry"]["id"], first["id"])
            rows = ledger.load(path)["entries"]
            self.assertIsNone(rows[0]["immediate_verdict"])
            self.assertEqual(rows[1]["immediate_verdict"], "drop")
            self.assertEqual(first["slot"], second["slot"])

    def test_judgment_forwards_role_key_and_retries_idempotently(self):
        with tempfile.TemporaryDirectory() as td:
            ratings_path = os.path.join(td, "ratings.json")
            ledger_path = os.path.join(td, "ledger.json")
            calls = []
            with mock.patch.object(gears.core, "_phrases", return_value=[]), \
                 mock.patch.object(ears, "record_verdict", return_value=[]), \
                 mock.patch.object(develop_session, "load_state", return_value={}), \
                 mock.patch.object(develop_session, "record_drop", return_value={}), \
                 mock.patch.object(gears.core, "rate", side_effect=lambda **kw: (
                     calls.append(kw) or {"record": {"id": 1}})):
                first = gears.judge_answer(
                    "drop", answer_notes=NOTES, role="bass", key="D minor",
                    entry_id="e-test", ledger_path=ledger_path,
                )
                second = gears.judge_answer(
                    "drop", answer_notes=NOTES, role="bass", key="D minor",
                    entry_id="e-test", ledger_path=ledger_path,
                )
            self.assertEqual(first["entry_id"], "e-test")
            self.assertEqual(second["entry_id"], "e-test")
            self.assertEqual(calls[0]["role"], "bass")
            self.assertEqual(calls[0]["key"], "D minor")
            self.assertEqual(calls[0]["idempotency_key"], "judge:e-test:drop")

    def test_legacy_source_is_explicit_and_missing_material_fails_closed(self):
        source = {"id": "legacy", "notes": [{"pitch": 60, "start": 0,
                                               "duration": 1, "velocity": 80,
                                               "role": "bass"}],
                  "role": "bass"}
        rate_calls = []
        with mock.patch.object(gears.core, "_phrases", return_value=[source]), \
             mock.patch.object(ears, "record_verdict", return_value=[]), \
             mock.patch.object(develop_session, "load_state", return_value={}), \
             mock.patch.object(develop_session, "record_drop", return_value={}), \
             mock.patch.object(gears.core, "rate", side_effect=lambda **kw: (
                 rate_calls.append(kw) or {"record": {"id": 1}})):
            result = gears.judge_answer("drop", source_phrase_id="legacy")
        self.assertIn("legacy source_phrase_id", result["note"])
        self.assertEqual(rate_calls[0]["role"], "bass")
        with mock.patch.object(gears.core, "_phrases", return_value=[]):
            with self.assertRaisesRegex(ValueError, "answer_notes"):
                gears.judge_answer("drop", source_phrase_id="gone")

    def test_public_judge_binds_exact_snapshot_and_retries_real_rating(self):
        """The MCP wrapper must retry the same persisted core rating."""
        try:
            from mcp_server import server
        except ModuleNotFoundError as exc:
            self.skipTest("MCP dependency unavailable: %s" % exc)

        with tempfile.TemporaryDirectory() as td:
            ledger_path = os.path.join(td, "ledger.json")
            ratings_path = os.path.join(td, "ratings.json")
            ears_path = os.path.join(td, "ears.json")
            develop_path = os.path.join(td, "develop.json")
            first_notes = [{**NOTES[0], "pitch": 60}]
            intended_notes = [{**NOTES[0], "pitch": 64}]
            first = ledger.record_write(
                clip_name="wrong", slot="4/4", source="catalogue",
                role="lead", answer_notes=first_notes, path=ledger_path)
            intended = ledger.record_write(
                clip_name="intended", slot="4/4", source="catalogue",
                role="bass", key="D minor", answer_notes=intended_notes,
                path=ledger_path)
            original_record_verdict = ledger.record_verdict
            original_ledger_path = ledger.LEDGER_PATH
            original_ratings = gears.core._ratings_path()
            original_ears_path = ears.SESSION_PATH
            original_develop_path = develop_session.STATE_PATH
            try:
                ledger.LEDGER_PATH = ledger_path
                gears.core.set_ratings_path(ratings_path)
                ears.SESSION_PATH = ears_path
                develop_session.STATE_PATH = develop_path
                record_attempts = {"count": 0}
                def fail_once(*args, **kwargs):
                    if record_attempts["count"] == 0:
                        record_attempts["count"] += 1
                        raise RuntimeError("injected")
                    return original_record_verdict(*args, **kwargs)
                with mock.patch.object(gears.core, "_phrases", return_value=[]), \
                     mock.patch.object(ears, "record_verdict", return_value=[]), \
                     mock.patch.object(develop_session, "load_state", return_value={}), \
                     mock.patch.object(develop_session, "record_drop", return_value={}), \
                     mock.patch.object(ledger, "record_verdict",
                                       side_effect=fail_once):
                    first_result = server.judge_answer(
                        "drop", slot="4/4", answer_notes=intended_notes,
                        confirm=False)
                    second_result = server.judge_answer(
                        "drop", slot="4/4", answer_notes=intended_notes,
                        entry_id=intended["id"],
                        confirm=False)
                with open(ratings_path) as fh:
                    ratings = json.load(fh)["ratings"]
                self.assertEqual(len(ratings), 1)
                # Verdict-first: the one-shot record_verdict failure is retried
                # by record_judgment inside the same call, so the verdict lands
                # immediately and the rating is never doubled on retry.
                self.assertEqual(first_result["ledger"]["entry_id"], intended["id"])
                self.assertEqual(second_result["ledger"]["entry_id"], intended["id"])
                self.assertEqual(second_result["entry_id"], intended["id"])
                self.assertEqual(ratings[0]["id"], 1)
                self.assertEqual(ratings[0]["idempotency_key"],
                                 "judge:%s:drop" % intended["id"])
                self.assertEqual(ratings[0]["role"], "bass")
                rows = ledger.load(ledger_path)["entries"]
                self.assertIsNone(rows[0]["immediate_verdict"])
                self.assertEqual(rows[1]["immediate_verdict"], "drop")
                self.assertNotIn("lead", str(ratings[0]["role"]))
            finally:
                ledger.LEDGER_PATH = original_ledger_path
                gears.core.set_ratings_path(original_ratings)
                ears.SESSION_PATH = original_ears_path
                develop_session.STATE_PATH = original_develop_path

    def test_public_three_endings_stamps_returned_key_on_every_ending(self):
        try:
            from mcp_server import live_write, rituals, server
        except ModuleNotFoundError as exc:
            self.skipTest("MCP dependency unavailable: %s" % exc)

        with tempfile.TemporaryDirectory() as td:
            ledger_path = os.path.join(td, "ledger.json")
            original_ledger_path = ledger.LEDGER_PATH
            try:
                ledger.LEDGER_PATH = ledger_path
                endings = {
                    "key": "D minor", "tempo": 111.0,
                    # Shape returned by rituals.three_endings: each ending has
                    # a display name and normalized MCP notes.
                    "endings": [{"name": name, "notes": NOTES}
                                for name in ("decay", "subtraction", "hard_stop")],
                }
                async def write(notes, *, clip_name, tempo):
                    return {"slot": "t1/%d" % (len(ledger.load(ledger_path)["entries"])),
                            "track": "SIDEKICK JAM"}
                with mock.patch.object(rituals, "three_endings", return_value=endings), \
                     mock.patch.object(live_write, "write_answer_to_jam_track", side_effect=write):
                    result = server.three_endings(seed_notes=NOTES, write=True)
                self.assertEqual(len(result["live_writes"]), 3)
                rows = ledger.load(ledger_path)["entries"]
                self.assertEqual(len(rows), 3)
                self.assertEqual([row["key"] for row in rows], ["D minor"] * 3)
                self.assertEqual([row["source"] for row in rows],
                                 ["catalogue"] * 3)
                self.assertEqual([row["move"] for row in rows], [
                    "ending:decay", "ending:subtraction", "ending:hard_stop",
                ])
            finally:
                ledger.LEDGER_PATH = original_ledger_path

    def test_sombra_three_endings_stamps_returned_key_on_every_ending(self):
        from mcp_server import rituals, sombra_listener
        import live_write

        with tempfile.TemporaryDirectory() as td:
            ledger_path = os.path.join(td, "ledger.json")
            original_ledger_path = ledger.LEDGER_PATH
            original_selection = sombra_listener._selection_notes
            try:
                ledger.LEDGER_PATH = ledger_path
                endings = {
                    "key": "A minor",
                    "endings": [{"name": name, "notes": NOTES}
                                for name in ("decay", "subtraction", "hard_stop")],
                }
                async def write(notes, *, clip_name, tempo):
                    return {"slot": "t2/0", "track": "SIDEKICK JAM"}
                sombra_listener._selection_notes = lambda: NOTES
                with mock.patch.object(rituals, "three_endings", return_value=endings), \
                     mock.patch.object(live_write, "write_answer_to_jam_track", side_effect=write):
                    result = sombra_listener._execute({
                        "tool": "three_endings", "tool_args": {},
                    })
                self.assertEqual(len(result["slots"]), 3)
                rows = ledger.load(ledger_path)["entries"]
                self.assertEqual(len(rows), 3)
                self.assertEqual([row["key"] for row in rows], ["A minor"] * 3)
            finally:
                ledger.LEDGER_PATH = original_ledger_path
                sombra_listener._selection_notes = original_selection


if __name__ == "__main__":
    unittest.main()
