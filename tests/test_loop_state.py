"""Loop state on every push: hear_the_room, JAM writes, and the profiles closure."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from mcp_server import jam_lifecycle, ledger, live_write, server

NOTES = [{"pitch": 60, "start": 0, "duration": 1, "velocity": 80}]
SET_A = {"set_id": "/music/loop-state.als", "runtime_epoch": "run-a",
         "file_path": "/music/loop-state.als"}
IDLE = ledger.SESSION_IDLE_SECONDS


def _ref(clip_id):
    return {"clip_id": clip_id, "track_id": "jam", "track": "SIDEKICK JAM",
            "surface": "session", "path": "t3/s0", "answer_notes": NOTES}


def _blind_moment():
    return {"ears": "blind", "reason": "offline", "tempo": None,
            "tempo_world": None, "key": None, "key_estimate": None,
            "scale_pcs": None, "roles": {}, "unheard": [], "hidden": [],
            "context": {}, "assumptions": []}


def _live_moment():
    return {"ears": "live", "reason": None, "tempo": 124.0,
            "tempo_world": "straight", "key": "D minor",
            "key_estimate": "D minor", "scale_pcs": None,
            "roles": {"bass": {"n_notes": 4}}, "unheard": [], "hidden": [],
            "context": {}, "assumptions": []}


class LoopStateLedger:
    """Two nights of one Set: the first ended idle and unswept, the second open."""

    def setUp(self):
        # anchored to real now so a wall-clock sweep never reads as "late"
        self.t0 = time.time() - IDLE - 120
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger_path = os.path.join(self._tmp.name, "ledger.json")
        patcher = mock.patch.object(ledger, "LEDGER_PATH", self.ledger_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        # fix4 nit: no test inherits another's cached readiness probe
        server._READINESS_CACHE.update({"at": None, "result": None})
        self.addCleanup(server._READINESS_CACHE.update,
                        {"at": None, "result": None})
        with mock.patch.object(ledger, "_now", return_value=self.t0):
            self.night_one = ledger.record_write(
                clip_name="old answer", slot="3/0", source="catalogue",
                answer_notes=NOTES, set_identity=SET_A, clip_ref=_ref("clip-a"),
                path=self.ledger_path)
            ledger.record_verdict("keep", entry_id=self.night_one["id"],
                                  path=self.ledger_path)
        # seven idle hours later: night one is ended (never closed), night
        # two opens with the same Set still up
        with mock.patch.object(ledger, "_now",
                               return_value=self.t0 + IDLE + 60):
            self.night_two = ledger.record_write(
                clip_name="tonight", slot="3/1", source="catalogue",
                answer_notes=[{**NOTES[0], "pitch": 64}],
                set_identity=SET_A, clip_ref=_ref("clip-b"),
                path=self.ledger_path)
        sessions = ledger.load(self.ledger_path)["sessions"]
        self.open_session_id = sessions[-1]["id"]
        self.ended_session_id = sessions[0]["id"]
        assert sessions[0]["ended_at"] and not sessions[0]["closed_at"]

    def open_inventory(self, complete=True):
        return {"set_identity": SET_A, "complete": complete, "errors": [],
                "clips": [_ref("clip-b")]}


class HearTheRoomAutoCloseTests(LoopStateLedger,
                                unittest.IsolatedAsyncioTestCase):
    async def hear(self, *, moment=_live_moment(), inventory=None,
                   inventory_error=None):
        readiness = mock.AsyncMock(return_value={
            "ready": True, "live_api": True, "set_saved": True,
            "identity": SET_A, "reason": None})
        with mock.patch("mcp_server.moment.get_moment", return_value=moment), \
                mock.patch.object(live_write, "scoring_readiness", readiness), \
                mock.patch.object(live_write, "read_set_inventory",
                                  mock.AsyncMock(return_value=inventory)
                                  if inventory_error is None
                                  else mock.AsyncMock(side_effect=inventory_error)), \
                mock.patch.object(server, "_store_disk_state",
                                  return_value={"free_bytes": 10 ** 12}):
            result = await server.mcp.call_tool("hear_the_room", {})
        return json.loads(result[0].text)

    async def test_ended_session_of_open_set_is_auto_swept_open_one_never(self):
        payload = await self.hear(inventory=self.open_inventory())
        self.assertEqual([row["session_id"] for row in payload["auto_closed"]],
                         [self.ended_session_id])
        self.assertEqual(payload["auto_closed"][0]["rate_status"], "definitive")
        self.assertEqual(payload["ledger_status"]["session"]["id"],
                         self.open_session_id)
        sessions = ledger.load(self.ledger_path)["sessions"]
        self.assertIsNotNone(sessions[0]["closed_at"])
        self.assertIsNone(sessions[-1]["closed_at"], "the open night is his")

    async def test_inventory_failure_becomes_a_warning_not_an_error(self):
        payload = await self.hear(inventory_error=ConnectionError("bridge down"))
        self.assertNotIn("auto_closed", payload)
        self.assertIn("auto-close skipped", payload["ledger_status"]["warning"])
        self.assertIsNone(
            ledger.load(self.ledger_path)["sessions"][0]["closed_at"])

    async def test_identityless_inventory_sweeps_nothing(self):
        """fix3 item 5: an inventory without Set identity can prove no
        chain — nothing is auto-closed and nothing is reported closed."""
        payload = await self.hear(inventory={
            "set_identity": None, "complete": True, "errors": [],
            "clips": [_ref("clip-b")]})
        self.assertNotIn("auto_closed", payload)
        self.assertNotIn("auto_swept_partial", payload)
        self.assertNotIn("warning", payload["ledger_status"])
        self.assertIsNone(
            ledger.load(self.ledger_path)["sessions"][0]["closed_at"])

    async def test_partial_sweep_is_reported_as_swept_partial_not_closed(self):
        """fix3 item 5: an ended night whose auto-sweep comes back partial
        (incomplete inventory) is listed under auto_swept_partial and its
        session stays open — auto_closed means actually closed."""
        payload = await self.hear(inventory={
            "set_identity": SET_A, "complete": False, "errors": [],
            "clips": []})
        self.assertNotIn("auto_closed", payload)
        self.assertEqual([row["session_id"] for row in
                          payload["auto_swept_partial"]],
                         [self.ended_session_id])
        self.assertEqual(payload["auto_swept_partial"][0]["rate_status"],
                         "partial")
        self.assertIsNone(
            ledger.load(self.ledger_path)["sessions"][0]["closed_at"])

    async def test_blind_moment_never_touches_live(self):
        calls = []

        async def forbidden(*_args, **_kwargs):
            calls.append(True)
            raise AssertionError("Live must not be read while blind")

        with mock.patch("mcp_server.moment.get_moment",
                        return_value=_blind_moment()), \
                mock.patch.object(live_write, "scoring_readiness",
                                  mock.AsyncMock(return_value={
                                      "ready": False, "reason": "offline"})), \
                mock.patch.object(live_write, "read_set_inventory", forbidden), \
                mock.patch.object(server, "_store_disk_state",
                                  return_value={"free_bytes": 10 ** 12}):
            payload = json.loads((await server.mcp.call_tool(
                "hear_the_room", {}))[0].text)
        self.assertEqual(calls, [])
        self.assertNotIn("auto_closed", payload)
        self.assertEqual(payload["ledger_status"]["unswept"][0]["session_id"],
                         self.ended_session_id)
        # tonight's unjudged write outranks the ended night's sweep
        self.assertIn("await judgment",
                      payload["ledger_status"]["next_action"])


class LedgerStatusTests(LoopStateLedger, unittest.TestCase):
    def test_jam_write_result_carries_ledger_status(self):
        target = {}
        entry = jam_lifecycle.stamp_verified_write(
            target, clip_name="jam write", slot="3/2", source="catalogue",
            role="lead", gear=2, notes=NOTES, ledger_path=self.ledger_path,
            live_receipt={"slot": "3/2", "track": "SIDEKICK JAM",
                          "set_identity": SET_A, "clip_ref": _ref("clip-c")})
        self.assertIsNotNone(entry)
        status = target["ledger_status"]
        self.assertEqual(status["session"]["id"], self.open_session_id)
        self.assertEqual(status["session"]["state"], "open")
        self.assertEqual(status["session"]["writes"], 2)
        self.assertEqual(status["session"]["pending_judgment"], 2)
        self.assertEqual(status["session"]["scoreable_writes"], 2)
        self.assertEqual(status["unswept"][0]["keep_rows_unresolved"], 1)

    def test_ledger_status_shape_is_live_free(self):
        status = ledger.ledger_status(path=self.ledger_path,
                                      now=self.t0 + IDLE + 120)
        self.assertEqual(status["session"]["id"], self.open_session_id)
        self.assertEqual(status["unswept"][0]["session_id"], self.ended_session_id)
        self.assertEqual(status["unswept"][0]["set_id"], SET_A["set_id"])
        self.assertEqual(status["unswept"][0]["keep_rows_unresolved"], 1)
        # tonight's unjudged write outranks every close advice
        self.assertIn("await judgment", status["next_action"])
        self.assertEqual(status["accounting_failures"], [])

    def test_idle_unended_night_still_gets_close_advice(self):
        """fix4 R2: the normal morning-after state — one night, idle past
        the window, never ended (no newer write), everything judged — must
        still advise the sweep (repro: repro2/p2.py case a2)."""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "l.json")
            with mock.patch.object(ledger, "_now", return_value=self.t0):
                entry = ledger.record_write(
                    clip_name="only-night", slot="3/0", source="catalogue",
                    answer_notes=NOTES, set_identity=SET_A,
                    clip_ref=_ref("only-night"), path=path)
            with mock.patch.object(ledger, "_now", return_value=self.t0 + 10):
                ledger.record_verdict("keep", entry_id=entry["id"], path=path)
            status = ledger.ledger_status(path, now=self.t0 + 3 * IDLE)
            self.assertIsNone(status["session"])  # nothing live
            self.assertEqual(len(status["unswept"]), 1)
            self.assertIsNone(status["unswept"][0]["ended_at"])
            self.assertIn("close_session()", status["next_action"])

    def test_next_action_orders_judging_first_then_attest_then_close(self):
        """fix3 item 4: judging tonight's pending writes comes before any
        close advice; attest advice stays but never outranks it; close
        advice targets ENDED sessions only."""
        now = self.t0 + IDLE + 120
        # all three at once: open pending + legacy attest rows + an ended
        # unswept night -> judging tonight wins
        with mock.patch.object(ledger, "_now", return_value=now):
            idless = ledger.record_write(clip_name="idless", slot="3/9",
                                         source="catalogue",
                                         path=self.ledger_path)
        status = ledger.ledger_status(path=self.ledger_path, now=now + 10)
        self.assertIn("await judgment", status["next_action"])

        # judging everything settles the open session: attest advice next
        with mock.patch.object(ledger, "_now", return_value=now + 20):
            ledger.record_verdict("keep", entry_id=self.night_two["id"],
                                  path=self.ledger_path)
            ledger.record_verdict("keep", entry_id=idless["id"],
                                  path=self.ledger_path)
        status = ledger.ledger_status(path=self.ledger_path, now=now + 30)
        self.assertIn("attest or retire", status["next_action"])

        # the legacy row retired: the ended night's close advice is last
        ledger.attest(self.open_session_id, {idless["id"]: "retire"},
                      confirm=True, path=self.ledger_path)
        status = ledger.ledger_status(path=self.ledger_path, now=now + 60)
        self.assertIn("close_session()", status["next_action"])
        self.assertIn(self.ended_session_id, status["next_action"])

    def test_close_advice_targets_ended_sessions_only(self):
        """An open, not-ended session with nothing pending gets no close
        nag — its night has not ended; the reminder waits for the sweep
        that its own ending will justify."""
        now = self.t0 + IDLE + 120
        with mock.patch.object(ledger, "_now", return_value=now):
            ledger.record_verdict("keep", entry_id=self.night_two["id"],
                                  path=self.ledger_path)
        # a fresh open session with a judged row, no ended nights pending
        status = ledger.ledger_status(path=self.ledger_path, now=now + 10)
        self.assertIn("close_session()", status["next_action"])  # ended night one
        # now sweep night one too: only the open session remains unswept
        ledger.close_session(
            {"set_identity": SET_A, "complete": True, "errors": [],
             "clips": [_ref("clip-b")]},
            session_id=self.ended_session_id, path=self.ledger_path)
        status = ledger.ledger_status(path=self.ledger_path, now=now + 20)
        self.assertIsNone(status["next_action"], status)


class HearTheRoomCostTests(unittest.IsolatedAsyncioTestCase):
    """fix3 item 6: hearing the room must not pay for Live round trips it
    cannot use. Full sink isolation — the ledger is a temp copy."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger_path = os.path.join(self._tmp.name, "ledger.json")
        patcher = mock.patch.object(ledger, "LEDGER_PATH", self.ledger_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        server._READINESS_CACHE.update({"at": None, "result": None})
        self.addCleanup(server._READINESS_CACHE.update,
                        {"at": None, "result": None})
        self.readiness = mock.AsyncMock(return_value={
            "ready": True, "live_api": True, "set_saved": True,
            "identity": SET_A, "reason": None})
        self.inventory = mock.AsyncMock(return_value={
            "set_identity": SET_A, "complete": True, "errors": [],
            "clips": [_ref("clip-b")]})

    async def hear(self, moment):
        with mock.patch("mcp_server.moment.get_moment",
                        return_value=moment), \
                mock.patch.object(live_write, "scoring_readiness",
                                  self.readiness), \
                mock.patch.object(live_write, "read_set_inventory",
                                  self.inventory), \
                mock.patch.object(server, "_store_disk_state",
                                  return_value={"free_bytes": 10 ** 12}):
            return json.loads((await server.mcp.call_tool(
                "hear_the_room", {}))[0].text)

    def write(self, name, *, identity, slot, now):
        with mock.patch.object(ledger, "_now", return_value=now):
            return ledger.record_write(
                clip_name=name, slot=slot, source="catalogue",
                answer_notes=NOTES, set_identity=identity,
                clip_ref=_ref(name), path=self.ledger_path)

    async def test_chainless_ended_night_costs_no_inventory_read(self):
        """An ended legacy night with no Set chain can never match the
        inventory: no Live read happens for it."""
        idle = ledger.SESSION_IDLE_SECONDS
        base = time.time() - idle - 120
        self.write("legacy", identity=None, slot="3/0", now=base)
        self.write("tonight", identity=SET_A, slot="3/1", now=base + idle + 60)
        sessions = ledger.load(self.ledger_path)["sessions"]
        self.assertEqual(len(sessions), 2)
        self.assertTrue(sessions[0].get("ended_at"))
        payload = await self.hear(_live_moment())
        self.inventory.assert_not_awaited()
        self.assertNotIn("auto_closed", payload)

    async def test_ended_night_of_another_set_costs_no_inventory_read(self):
        """fix4 item 6: having a chain is not enough — the ended night's
        chain must CONTAIN the Set the ledger last saw. An old night of a
        different Set (chain perfectly non-empty) costs no Live read."""
        idle = ledger.SESSION_IDLE_SECONDS
        base = time.time() - idle - 120
        set_b = {"set_id": "/music/other.als", "runtime_epoch": "rb",
                 "file_path": "/music/other.als"}
        self.write("old-set-night", identity=SET_A, slot="3/0", now=base)
        self.write("tonight", identity=set_b, slot="3/1",
                   now=base + idle + 60)
        sessions = ledger.load(self.ledger_path)["sessions"]
        self.assertEqual(len(sessions), 2)
        self.assertTrue(sessions[0].get("ended_at"))
        # the ledger's current Set is tonight's, not the old night's
        self.assertEqual(ledger.last_known_set_id(ledger.load(self.ledger_path)),
                         set_b["set_id"])
        payload = await self.hear(_live_moment())
        self.inventory.assert_not_awaited()
        self.assertNotIn("auto_closed", payload)
        self.assertIsNone(ledger.load(self.ledger_path)["sessions"][0]
                          .get("closed_at"))

    async def test_blind_room_skips_the_readiness_probe(self):
        await self.hear(_blind_moment())
        self.readiness.assert_not_awaited()

    async def test_readiness_probe_is_cached_for_sixty_seconds(self):
        first = await self.hear(_live_moment())
        second = await self.hear(_live_moment())
        self.assertEqual(self.readiness.await_count, 1)
        self.assertEqual(second["scoring_ready"], first["scoring_ready"])
        # a fresh cache pays for exactly one more probe
        server._READINESS_CACHE.update({"at": None, "result": None})
        await self.hear(_live_moment())
        self.assertEqual(self.readiness.await_count, 2)

    async def test_readiness_cache_is_keyed_by_set(self):
        """fix4 item 6: switching Sets invalidates the cached probe."""
        idle = ledger.SESSION_IDLE_SECONDS
        base = time.time() - 120
        set_b = {"set_id": "/music/other.als", "runtime_epoch": "rb",
                 "file_path": "/music/other.als"}
        self.write("on-a", identity=SET_A, slot="3/0", now=base)
        await self.hear(_live_moment())
        self.assertEqual(self.readiness.await_count, 1)
        # a write to another Set makes it the ledger's current Set
        self.write("on-b", identity=set_b, slot="3/1", now=base + idle + 60)
        await self.hear(_live_moment())
        self.assertEqual(self.readiness.await_count, 2)
        # ...and the new Set's result is now the cached one
        await self.hear(_live_moment())
        self.assertEqual(self.readiness.await_count, 2)

    async def test_failed_readiness_probe_is_not_cached(self):
        """fix4 item 6: a probe that raises is returned but never cached —
        the next hearing tries again."""
        ok = {"ready": True, "live_api": True, "set_saved": True,
              "identity": SET_A, "reason": None}
        self.readiness.side_effect = [OSError("bridge hiccup"), ok]
        first = await self.hear(_live_moment())
        self.assertIn("probe failed", first["scoring_ready"]["reason"])
        second = await self.hear(_live_moment())
        self.assertTrue(second["scoring_ready"]["ready"])
        self.assertEqual(self.readiness.await_count, 2)


class DiskAndClosureTests(unittest.IsolatedAsyncioTestCase):
    """fix3 item 10: even the "pure" hear_the_room tests never read the
    real ledger — the loop state rides on every result, so the ledger is a
    temp copy here too."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(
            ledger, "LEDGER_PATH", os.path.join(tmp.name, "ledger.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        server._READINESS_CACHE.update({"at": None, "result": None})
        self.addCleanup(server._READINESS_CACHE.update,
                        {"at": None, "result": None})
        # a live session in the temp ledger: the loop state must name IT,
        # never this worktree's store copy (isolation guard)
        self.isolated_session_id = ledger.record_write(
            clip_name="isolated", slot="3/0", source="catalogue",
            answer_notes=NOTES,
            set_identity={"set_id": "disk-test", "runtime_epoch": "r1",
                          "file_path": None},
            clip_ref=_ref("disk-test"), path=ledger.LEDGER_PATH)["session_id"]

    async def test_low_disk_is_a_loud_warning_on_hear_the_room(self):
        import collections
        usage = collections.namedtuple("usage", "total used free")(
            256 * 1024 * 1024, 106 * 1024 * 1024, 150 * 1024 * 1024)
        with mock.patch("mcp_server.moment.get_moment",
                        return_value=_blind_moment()), \
                mock.patch.object(live_write, "scoring_readiness",
                                  mock.AsyncMock(return_value={
                                      "ready": False, "reason": "offline"})), \
                mock.patch("shutil.disk_usage", return_value=usage):
            payload = json.loads((await server.mcp.call_tool(
                "hear_the_room", {}))[0].text)
        self.assertIn("free space now", payload["disk"]["warning"])
        # isolation guard: the loop state came from this test's temp ledger,
        # not this worktree's store copy
        self.assertEqual(payload["ledger_status"]["session"]["id"],
                         self.isolated_session_id)

    async def test_profiles_description_closure_still_holds(self):
        from mcp_server import profiles
        profiles.validate_server_description_closure(server.mcp)


if __name__ == "__main__":
    unittest.main()
