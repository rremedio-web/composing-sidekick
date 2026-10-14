"""Controller regressions for interrupted and repeated public judgments."""
import json
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from mcp_server import develop_session, ears, gears, ledger

NOTES = [{"pitch": 60, "start": 0, "duration": 1, "velocity": 80}]
IDENTITY = {"set_id": "/tmp/controller.als", "file_path": "/tmp/controller.als", "runtime_epoch": "live-1"}


class JudgmentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.ledger_path = str(self.root / "ledger.json")
        self.stack.enter_context(mock.patch.object(ledger, "LEDGER_PATH", self.ledger_path))
        self.stack.enter_context(mock.patch.object(ears, "SESSION_PATH", str(self.root / "ears.json")))
        self.stack.enter_context(mock.patch.object(develop_session, "STATE_PATH", str(self.root / "develop.json")))
        self.stack.enter_context(mock.patch.object(gears.core, "_phrases", return_value=[]))
        # Alias resolution reads the build's id map; keep it inside the temp store.
        self.stack.enter_context(mock.patch.object(
            ledger, "ID_MAP_PATH", str(self.root / "phrase_id_map.json")))
        ledger._ID_MAP_CACHE = None
        self.addCleanup(setattr, ledger, "_ID_MAP_CACHE", None)
        old_path = gears.core._ratings_path()
        gears.core.set_ratings_path(str(self.root / "ratings.json"))
        self.addCleanup(gears.core.set_ratings_path, old_path)

    def write(self, name):
        return ledger.record_write(clip_name=name, slot="3/0", source="catalogue", answer_notes=NOTES,
                                   set_identity=IDENTITY,
                                   clip_ref={"clip_id": name, "track_id": "jam", "track": "SIDEKICK JAM",
                                             "surface": "session", "path": "t3/s0", "answer_notes": NOTES})

    def test_retry_after_rating_acknowledgement_completes_without_relearning(self):
        entry = self.write("one")
        with mock.patch.object(ledger, "complete_judgment", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                gears.judge_answer("drop", answer_notes=NOTES, entry_id=entry["id"])
        before = {p.name: p.read_bytes() for p in self.root.glob("*.json") if p.name != "ledger.json"}
        result = gears.judge_answer("drop", answer_notes=NOTES, entry_id=entry["id"])
        self.assertEqual(result["verdict"], "drop")
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.glob("*.json") if p.name != "ledger.json"})

    def test_retry_survives_phrase_id_migration_after_rating_failure(self):
        # Shape from ledger.record_write and gears.judge_answer's persisted
        # judgment event; all paths and sink failures stay inside this temp store.
        from analyze.corpus_keys import migrate_stored_ids
        from mcp_server import core, server

        entry = ledger.record_write(
            clip_name="migrating", slot="3/0", source="catalogue",
            source_phrase_id="old|clip|w0", answer_notes=NOTES,
            set_identity=IDENTITY,
            clip_ref={"clip_id": "migrating", "track_id": "jam",
                      "track": "SIDEKICK JAM", "surface": "session",
                      "path": "t3/s0", "answer_notes": NOTES})
        with mock.patch.object(core, "rate", side_effect=OSError("ratings disk full")):
            failed = server.judge_answer("drop", entry_id=entry["id"])
        self.assertIn("learning_failure", failed)

        id_map_path = str(self.root / "phrase_id_map.json")
        Path(id_map_path).write_text(json.dumps({"map": {"old|clip|w0": "p-migrated"}}))
        with mock.patch.object(ledger, "ID_MAP_PATH", id_map_path):
            ledger._ID_MAP_CACHE = None
            ratings_path = gears.core._ratings_path()
            migrate_stored_ids(ratings_path, self.ledger_path,
                               {"old|clip|w0": "p-migrated"}, dry_run=False,
                               backup=False)
            result = server.judge_answer("drop", entry_id=entry["id"])
            self.assertEqual(result["verdict"], "drop")
            self.assertEqual(result["record"]["phrase_id"], "p-migrated")
            with self.assertRaises(ValueError):
                server.judge_answer("keep", entry_id=entry["id"])

        row = next(e for e in ledger.load(self.ledger_path)["entries"]
                   if e["id"] == entry["id"])
        self.assertEqual(row["immediate_verdict"], "drop")
        ratings = json.loads(Path(ratings_path).read_text())["ratings"]
        self.assertEqual(len(ratings), 1)
        state = ledger.load(self.ledger_path)
        self.assertEqual(len(state["judgment_events"]), 1)
        self.assertEqual(set(state["judgment_events"][0]["steps"]),
                         {"ears", "develop", "rating", "ledger"})
        self.assertTrue(all(step["status"] == "applied"
                            for step in state["judgment_events"][0]["steps"].values()))
        ears_state = json.loads((self.root / "ears.json").read_text())
        develop_state = json.loads((self.root / "develop.json").read_text())
        self.assertEqual(len(ears_state["judgment_events"]), 1)
        self.assertEqual(len(develop_state["judgment_events"]), 1)

    def test_seed_change_on_same_event_is_rejected(self):
        gears.judge_answer("keep", answer_notes=NOTES, seed_notes=NOTES, idempotency_key="seed-test")
        with self.assertRaises(ValueError):
            gears.judge_answer("keep", answer_notes=NOTES,
                               seed_notes=[{**NOTES[0], "pitch": 65}], idempotency_key="seed-test")

    def _migrate(self, id_map):
        """Run the real migration with the id map the build would ship."""
        from analyze.corpus_keys import migrate_stored_ids
        id_map_path = str(self.root / "phrase_id_map.json")
        Path(id_map_path).write_text(json.dumps({"map": id_map}))
        ledger._ID_MAP_CACHE = None
        migrate_stored_ids(gears.core._ratings_path(), self.ledger_path, id_map,
                           dry_run=False, backup=False,
                           develop_session_path=str(self.root / "develop.json"))

    def _interrupted_after_sink(self, sink, *, verdict="drop", move=None,
                                seed=False, chain=False, retry_old_id=False):
        """Crash after a sink's own atomic save but before the journal's
        ``applied`` ack, migrate the phrase id, then retry. Scenario from the
        round-3 review probe p1_sinks.py; ledger/clip_ref shapes are the ones
        ledger.record_write persists for a SIDEKICK JAM session write."""
        from mcp_server import core, server
        name = "migrate-%s-%s-%s-%s-%s-%s" % (sink, verdict, move, seed, chain, retry_old_id)
        clip_ref = {"clip_id": name, "track_id": "jam", "track": "SIDEKICK JAM",
                    "surface": "session", "path": "t3/s0", "answer_notes": NOTES}
        entry = ledger.record_write(
            clip_name=name, slot="3/0", source="catalogue",
            source_phrase_id="old|clip|w0", answer_notes=NOTES,
            set_identity=IDENTITY, move=move, clip_ref=clip_ref)
        if seed:
            develop_session._locked_update(lambda s: {**s, "sticky": True}, None)
        kwargs = {"seed_notes": NOTES} if seed else {}
        real_mark = ledger.mark_judgment_step

        def crash(event_id, step, **kw):
            if step == sink and kw.get("status", "applied") == "applied":
                raise OSError("crash after %s sink saved" % step)
            return real_mark(event_id, step, **kw)

        with mock.patch.object(ledger, "mark_judgment_step", side_effect=crash):
            first = server.judge_answer(verdict, entry_id=entry["id"], **kwargs)
        self.assertIn("crash after %s sink saved" % sink, first["learning_failure"]["error"])
        if chain:
            self._migrate({"old|clip|w0": "p-mid"})
            self._migrate({"old|clip|w0": "p-new", "p-mid": "p-new"})
        else:
            self._migrate({"old|clip|w0": "p-new"})
        if retry_old_id:
            kwargs["source_phrase_id"] = "old|clip|w0"
        retry = server.judge_answer(verdict, entry_id=entry["id"], **kwargs)
        again = server.judge_answer(verdict, entry_id=entry["id"], **kwargs)
        return entry, first, retry, again

    def _effects(self, entry_id):
        state = ledger.load(self.ledger_path)
        events = [e for e in state["judgment_events"] if e.get("entry_id") == entry_id]
        ratings = json.loads((self.root / "ratings.json").read_text())["ratings"] \
            if (self.root / "ratings.json").exists() else []
        ears_events = json.loads((self.root / "ears.json").read_text()).get(
            "judgment_events", {})
        dev = json.loads((self.root / "develop.json").read_text())
        mine = [r for r in ratings if r.get("entry_id") == entry_id
                or str(r.get("idempotency_key") or "").startswith("judge:" + entry_id)]
        return events, mine, ears_events, dev

    def test_retry_after_each_sink_ack_survives_id_migration(self):
        cases = [
            ("ears", dict()),
            ("develop", dict()),
            ("rating", dict()),
            ("rating", dict(verdict="keep", move="invert")),
            ("develop", dict(verdict="keep", seed=True)),
            ("rating", dict(chain=True)),
            ("ears", dict(chain=True)),
            ("develop", dict(retry_old_id=True)),
            ("rating", dict(retry_old_id=True)),
        ]
        with mock.patch.object(gears.core, "_retrain_taste",
                               side_effect=RuntimeError("no taste model in a temp store")):
            for sink, kw in cases:
                with self.subTest(sink=sink, **kw):
                    entry, first, retry, again = self._interrupted_after_sink(sink, **kw)
                    self.assertNotIn("learning_failure", retry)
                    self.assertNotIn("learning_failure", again)
                    self.assertEqual(retry["verdict"], kw.get("verdict", "drop"))
                    events, ratings, ears_events, dev = self._effects(entry["id"])
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]["status"], "complete")
                    expected = 2 if kw.get("move") else 1
                    self.assertEqual(len(ratings), expected)
                    self.assertTrue(all(r["phrase_id"] == "p-new" for r in ratings))
                    self.assertEqual(sum(1 for k in ears_events
                                         if k == events[0]["event_id"]), 1)
                    # develop records an event for DROP and for a seeded KEEP;
                    # a seedless KEEP only reads the hold state.
                    develop_writes = kw.get("verdict", "drop") == "drop" or kw.get("seed")
                    self.assertEqual(events[0]["event_id"] in dev.get("judgment_events", {}),
                                     bool(develop_writes))
                    if kw.get("seed"):
                        self.assertEqual(dev["hold"]["source_phrase_id"], "p-new")
                        self.assertEqual(dev["hold"]["pass"], 0)
                    develop_session._locked_update(
                        lambda s: {**s, "sticky": False, "hold": None}, None)

    def test_migrated_retry_still_refuses_genuine_conflicts(self):
        """Round-3 control probe (r2_control.py): alias tolerance must not
        widen into accepting a different verdict, notes or phrase."""
        from mcp_server import core, server
        entry = ledger.record_write(
            clip_name="c", slot="3/0", source="catalogue", source_phrase_id="old|clip|w0",
            answer_notes=NOTES, set_identity=IDENTITY,
            clip_ref={"clip_id": "c", "track_id": "jam", "track": "SIDEKICK JAM",
                      "surface": "session", "path": "t3/s0", "answer_notes": NOTES})
        ledger.record_write(
            clip_name="o", slot="3/1", source="catalogue", source_phrase_id="other|clip|w0",
            answer_notes=NOTES, set_identity=IDENTITY,
            clip_ref={"clip_id": "o", "track_id": "jam", "track": "SIDEKICK JAM",
                      "surface": "session", "path": "t3/s1", "answer_notes": NOTES})
        with mock.patch.object(core, "rate", side_effect=OSError("ratings unavailable")):
            server.judge_answer("drop", entry_id=entry["id"])
        self._migrate({"old|clip|w0": "p-new"})
        for label, verdict, kw in (
                ("notes", "drop", {"answer_notes": [{**NOTES[0], "pitch": 61}]}),
                ("phrase", "drop", {"source_phrase_id": "other|clip|w0"}),
                ("verdict", "keep", {})):
            with self.subTest(conflict=label):
                with self.assertRaises(ValueError):
                    server.judge_answer(verdict, entry_id=entry["id"], **kw)

    def test_journal_retry_matches_stored_digest_across_alias(self):
        """begin_judgment accepts only a phrase-alias difference and hands
        back the ORIGINAL digest the sinks already hold."""
        payload = {"entry_id": "e-1", "verdict": "drop", "source_phrase_id": "old|clip|w0",
                   "answer_notes": NOTES, "reason": None}
        first = ledger.begin_judgment("judge:e-1:drop", payload, entry_id="e-1")
        self._migrate({"old|clip|w0": "p-new"})
        stored = ledger.load(self.ledger_path)["judgment_events"][0]
        self.assertEqual(stored["fingerprint"], first["fingerprint"])
        self.assertEqual(stored["payload"]["source_phrase_id"], "p-new")
        again = ledger.begin_judgment("judge:e-1:drop",
                                      {**payload, "source_phrase_id": "p-new"}, entry_id="e-1")
        self.assertEqual(again["fingerprint"], first["fingerprint"])
        for bad in ({"source_phrase_id": "p-other"}, {"reason": "too static"},
                    {"answer_notes": [{**NOTES[0], "pitch": 61}]}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ledger.begin_judgment("judge:e-1:drop", {**payload, **bad}, entry_id="e-1")

    def test_rating_idempotency_accepts_alias_only(self):
        """core.rate: a replay whose only difference is a migrated phrase id
        matches the stored hash; other content still raises."""
        from mcp_server import core
        with mock.patch.object(core, "_retrain_taste", side_effect=RuntimeError("no model")):
            core.rate(rating=-1, label="dropped", phrase_id="old|clip|w0", notes=NOTES,
                      idempotency_key="judge:e-9:drop")
            self._migrate({"old|clip|w0": "p-new"})
            out = core.rate(rating=-1, label="dropped", phrase_id="p-new", notes=NOTES,
                            idempotency_key="judge:e-9:drop")
            self.assertTrue(out["idempotent"])
            out = core.rate(rating=-1, label="dropped", phrase_id="old|clip|w0", notes=NOTES,
                            idempotency_key="judge:e-9:drop")
            self.assertTrue(out["idempotent"])
            for bad in ({"phrase_id": "p-other"}, {"comment": "different"}):
                with self.subTest(bad=bad):
                    with self.assertRaisesRegex(ValueError, "different content"):
                        core.rate(**{"rating": -1, "label": "dropped", "phrase_id": "p-new",
                                     "notes": NOTES, "idempotency_key": "judge:e-9:drop", **bad})

    def test_explicit_old_phrase_alias_is_valid_after_migration(self):
        from analyze.corpus_keys import migrate_stored_ids
        from mcp_server import core, server
        entry = ledger.record_write(
            clip_name="old-alias", slot="3/0", source="catalogue",
            source_phrase_id="old|clip|w0", answer_notes=NOTES,
            set_identity=IDENTITY)
        with mock.patch.object(core, "rate", side_effect=OSError("ratings unavailable")):
            server.judge_answer("drop", entry_id=entry["id"],
                                source_phrase_id="old|clip|w0")
        id_map = {"old|clip|w0": "p-migrated"}
        id_map_path = str(self.root / "phrase_id_map.json")
        Path(id_map_path).write_text(json.dumps({"map": id_map}))
        with mock.patch.object(ledger, "ID_MAP_PATH", id_map_path):
            ledger._ID_MAP_CACHE = None
            migrate_stored_ids(gears.core._ratings_path(), self.ledger_path,
                               id_map, dry_run=False, backup=False)
            self.assertEqual(server.judge_answer(
                "drop", entry_id=entry["id"],
                source_phrase_id="old|clip|w0")["verdict"], "drop")

    def test_bare_replay_does_not_judge_older_pending_entry(self):
        from mcp_server import server
        older = self.write("older")
        newest = self.write("newest")
        first = server.judge_answer("drop")
        second = server.judge_answer("drop")
        self.assertEqual(first["entry_id"], newest["id"])
        self.assertNotIn("replay", first)
        # The retry inside the replay window is an explicit replay of the
        # same cursor, never a fresh judgment of the older pending row.
        self.assertEqual(second["entry_id"], newest["id"])
        self.assertTrue(second["replay"])
        row = next(e for e in ledger.load()["entries"] if e["id"] == older["id"])
        self.assertIsNone(row["immediate_verdict"])

    def test_failed_delete_receipt_remains_retryable(self):
        from mcp_server import server, live_write
        entry = self.write("delete-retry")
        with mock.patch.object(live_write, "delete_jam_clip", new=mock.AsyncMock(
                side_effect=[{"deleted": False, "reason": "readback unavailable"}, {"deleted": True}])) as delete:
            first = server.judge_answer("drop", entry_id=entry["id"], confirm=True)
            second = server.judge_answer("drop", entry_id=entry["id"], confirm=True)
        self.assertFalse(first["clip_delete"]["deleted"])
        self.assertTrue(second["clip_delete"]["deleted"])
        self.assertEqual(delete.await_count, 2)

    def test_pedal_verdict_stamps_its_entry_not_newer_clip_at_same_slot(self):
        from mcp_server import sombra_listener
        older = self.write("pedal-owned")
        newer = self.write("other-write")
        state = {"last_answer_slot": "3/0", "last_answer_entry_id": older["id"],
                 "last_answer_notes": NOTES, "last_answer_source": None,
                 "last_answer_seed": None, "last_answer_key": None,
                 "last_answer_clip_ref": older["clip_ref"], "last_answer_set_identity": IDENTITY}
        with mock.patch.dict(sombra_listener.STATE, state, clear=True):
            result = sombra_listener._execute({"tool": "judge_answer", "tool_args": {
                "verdict": "drop", "slot": "3/0", "confirm": False}})
        self.assertEqual(result["ledger"]["entry_id"], older["id"])
        rows = {e["id"]: e for e in ledger.load()["entries"]}
        self.assertEqual(rows[older["id"]]["immediate_verdict"], "drop")
        self.assertIsNone(rows[newer["id"]]["immediate_verdict"])

    def test_unknown_explicit_entry_has_no_learning_effects(self):
        from mcp_server import server
        with self.assertRaises(ValueError):
            server.judge_answer("drop", answer_notes=NOTES, entry_id="missing")
        self.assertFalse((self.root / "ratings.json").exists())
        self.assertFalse((self.root / "ears.json").exists())

    def test_public_keep_survives_retrain_failure_end_to_end(self):
        """End to end through the public judgment tool: a taste retrain crash
        after the gold rating persists must not read as a failed judgment —
        the ledger verdict lands, exactly one gold rating exists, and the
        identical retry replays it."""
        from mcp_server import core, server

        entry = self.write("keep-me")
        with mock.patch.object(core, "_retrain_taste",
                               side_effect=RuntimeError("model exploded")):
            first = server.judge_answer("keep")
            second = server.judge_answer("keep")
        self.assertEqual(first["verdict"], "keep")
        self.assertEqual(first["ledger"]["entry_id"], entry["id"])
        self.assertEqual(second["ledger"]["entry_id"], entry["id"])
        self.assertEqual(second["record"]["id"], first["record"]["id"])
        rows = ledger.load()["entries"]
        self.assertEqual(rows[0]["immediate_verdict"], "keep")
        with open(self.root / "ratings.json") as handle:
            ratings = json.load(handle)["ratings"]
        self.assertEqual(len(ratings), 1)
        self.assertEqual(ratings[0]["label"], "gold")

    def test_pedal_answer_surfaces_unscoreable_write_in_log(self):
        """A pedal answer whose receipt can never be scored at session close
        is logged as unscoreable — the operator sees it on the device, not
        only deep inside a chat result."""
        from mcp_server import core, live_write, sombra_listener

        receipt = {"slot": "3/0", "track": "SIDEKICK JAM",
                   "identity_status": "identity_error"}
        logs = []
        ans = {"answer_notes": NOTES, "tempo": 120.0, "source_phrase_ids": ["p1"]}

        async def fake_write(_notes, *, clip_name, tempo):
            return dict(receipt)

        with (mock.patch.dict(sombra_listener.STATE, {}, clear=True),
              mock.patch.object(sombra_listener, "_selection_notes",
                                return_value=NOTES),
              mock.patch.object(sombra_listener, "_log", new=logs.append),
              mock.patch.object(core, "answer_my_loop", return_value=ans),
              mock.patch.object(live_write, "write_answer_to_jam_track",
                                new=fake_write)):
            result = sombra_listener._execute({"tool": "answer_my_loop",
                                               "tool_args": {"gear": 2,
                                                             "write": True}})
        self.assertEqual(result["slot"], "3/0")
        unscoreable = [entry for entry in logs if "unscoreable" in entry]
        self.assertEqual(len(unscoreable), 1)
        self.assertIn("cannot be scored", unscoreable[0]["unscoreable"])
