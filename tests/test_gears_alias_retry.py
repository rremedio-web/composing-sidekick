"""A direct gears.judge_answer retry (the pedal listener's path) that still
carries the pre-migration alias completes after a phrase-id migration."""
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from mcp_server import core, develop_session, ears, gears, ledger
from analyze.corpus_keys import migrate_stored_ids

NOTES = [{"pitch": 60, "start": 0, "duration": 1, "velocity": 80}]
IDENTITY = {"set_id": "/tmp/controller.als", "file_path": "/tmp/controller.als",
            "runtime_epoch": "live-1"}


class GearsAliasRetry(unittest.TestCase):
    def _run(self, crash_step, keep_map=True):
        with ExitStack() as st, tempfile.TemporaryDirectory() as d:
            root = Path(d)
            lp = str(root / "ledger.json")
            st.enter_context(mock.patch.object(ledger, "LEDGER_PATH", lp))
            st.enter_context(mock.patch.object(ears, "SESSION_PATH", str(root / "ears.json")))
            st.enter_context(mock.patch.object(develop_session, "STATE_PATH",
                                               str(root / "develop.json")))
            st.enter_context(mock.patch.object(gears.core, "_phrases", return_value=[]))
            st.enter_context(mock.patch.object(core, "_retrain_taste", return_value=None))
            idmap = root / "phrase_id_map.json"
            st.enter_context(mock.patch.object(ledger, "ID_MAP_PATH", str(idmap)))
            ledger._ID_MAP_CACHE = None
            old_ratings = core._ratings_path()
            core.set_ratings_path(str(root / "ratings.json"))
            st.callback(core.set_ratings_path, old_ratings)
            st.callback(setattr, ledger, "_ID_MAP_CACHE", None)
            entry = ledger.record_write(
                clip_name="m", slot="3/0", source="catalogue", source_phrase_id="old|c|w0",
                answer_notes=NOTES, set_identity=IDENTITY,
                clip_ref={"clip_id": "m", "track_id": "jam", "track": "SIDEKICK JAM",
                          "surface": "session", "path": "t3/s0", "answer_notes": NOTES})
            ledger.record_verdict("drop", entry_id=entry["id"], path=lp)
            real = ledger.mark_judgment_step

            def boom(eid, step, **kw):
                if step == crash_step and kw.get("status", "applied") == "applied":
                    raise OSError("crash")
                return real(eid, step, **kw)

            kw = dict(source_phrase_id="old|c|w0", answer_notes=NOTES, entry_id=entry["id"],
                      idempotency_key="press-1", ledger_path=lp)
            with mock.patch.object(ledger, "mark_judgment_step", side_effect=boom):
                with self.assertRaises(OSError):
                    gears.judge_answer("drop", **kw)
            mapping = {"old|c|w0": "p-new"}
            idmap.write_text(json.dumps({"map": mapping}))
            ledger._ID_MAP_CACHE = None
            migrate_stored_ids(core._ratings_path(), lp, mapping, dry_run=False, backup=False)
            if not keep_map:
                idmap.unlink()
                ledger._ID_MAP_CACHE = None
                with self.assertRaisesRegex(ValueError, "contradicts"):
                    gears.judge_answer("drop", **kw)
                return None
            result = gears.judge_answer("drop", **kw)
            events = ledger.load(lp).get("judgment_events", [])
            stored = json.loads(Path(core._ratings_path()).read_text())
            rows = stored.get("ratings", stored) if isinstance(stored, dict) else stored
            return result, events, len(rows)

    def test_old_alias_retry_completes_after_migration(self):
        for step in ("ears", "rating"):
            with self.subTest(step=step):
                result, events, ratings = self._run(step)
                self.assertIsNone(result.get("learning_failure"))
                self.assertEqual(len(events), 1)
                steps = events[0]["steps"]
                # a DROP's clip deletion is the server's confirm-gated half, so
                # the event stays open here; every learning step applied once
                self.assertEqual({k: v["status"] for k, v in steps.items()},
                                 {"ears": "applied", "develop": "applied",
                                  "rating": "applied"})
                self.assertEqual(result["record"]["phrase_id"], "p-new")
                self.assertEqual(ratings, 1)

    def test_without_the_id_map_the_old_alias_is_refused(self):
        self._run("ears", keep_map=False)


if __name__ == "__main__":
    unittest.main()
