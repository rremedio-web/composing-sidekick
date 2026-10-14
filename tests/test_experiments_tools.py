"""B11 — the MCP loop: register_arrangement_candidates → judge_answer → close_session
→ session_report, driven through a Producer Pal fake shaped like the v4 Set
(both builds; the 2.3 twin validates every request against the captured
2.3.0 schemas and answers in 2.3 shapes).

No Live, no real store: temp ledger, temp piece decisions, temp journal
copies. The fake holds the ids the journals name (tracks 2444/2483/2515/2537,
clips 2565–2567 and 2584–2591, devices 2593/2617 on owner tracks 21/1113).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from unittest import mock

from mcp_server import experiments, ledger, live_write, piece_context, pp_session, profiles, server
from tests.pp_fakes import SchemaStrictSession, error_response, json_response, on_both_versions
from tests.test_experiments_rows import IMPACT_JOURNAL, PEAK_JOURNAL, SIDECHAIN_JOURNAL

SET_PATH = "/Users/x/Example Project/NOCTURNEv4.als"
ROOT_ID = "live-root-v4"
INCARNATION = {"pid": 4242, "start_time": "Tue Sep 29 20:00:00 2026"}
LIVE_PROPS = ("name", "loop_start", "loop_end", "start_marker", "end_marker", "looping",
              "start_time", "end_time", "signature_numerator", "signature_denominator",
              "is_midi_clip", "muted")


def _pp_notes(pitches):
    return "[" + ",".join("{p:%d,t:%d,d:0.5,v:90}" % (p, i) for i, p in enumerate(pitches)) + "]"


def _live_notes(pitches):
    return [{"note_id": i + 1, "pitch": p, "start_time": float(i), "duration": 0.5,
             "velocity": 90.0, "mute": 0, "probability": 1.0, "velocity_deviation": 0.0,
             "release_velocity": 0.0} for i, p in enumerate(pitches)]


class V4Device(SchemaStrictSession):
    """The NOCTURNEv4 Set as the journals left it, kept in 2.2 terms
    (the harness reshapes replies for the 2.3 twin). Read-only: every
    handler answers reads; there is no write handler at all."""

    def __init__(self, version=None):
        super().__init__(version)
        self.file_path = SET_PATH
        self.root_id = ROOT_ID
        self.incarnation = dict(INCARNATION)   # what the epoch resolver reports
        self.tracks = []            # 2.2 overview records
        self.arrangement = {}       # trackIndex -> [2.2 arrangement clip records]
        self.clip_live = {}         # clip id -> {"values", "notes", "path"}
        self.devices = {}           # track id -> [{id, name, type, path}]
        self.device_params = {}     # device id -> [{name, value}]
        self.handlers = {
            "ppal-live-api": self._live_api,
            "ppal-read-live-set": self._read_live_set,
            "ppal-read-track": self._read_track,
            "ppal-read-device": self._read_device,
        }
        self.add_track("2", "PP", 0)
        self.add_track("21", "25-Bassline", 24, devices=[
            ("210", "Bassline", "instrument: Bassline"),
            ("2593", "SIDEKICK duck ← drums", "audio-effect: Compressor")])
        self.add_track("1113", "32-Omnisphere", 31, devices=[
            ("1853", "Omnisphere", "instrument: Omnisphere"),
            ("2617", "SIDEKICK duck ← drums", "audio-effect: Compressor")])
        self.add_track("2444", "SIDEKICK – drums, whole track (impact)", 10)
        self.add_track("2483", "SIDEKICK – peak line 1 space", 40)
        self.add_track("2515", "SIDEKICK – peak line 2 rotation", 41, muted=True)
        self.add_track("2537", "SIDEKICK – peak line 3 climb", 42, muted=True)
        for cid, name, start_bar, pitches in (("2584", "weight", 41, (36, 38)),
                                              ("2591", "riff", 55, (36, 36, 38)),
                                              ("2586", "drift", 79, (36,))):
            self.add_clip(10, cid, name, start_bar, pitches)
        self.add_clip(40, "2565", "1 space", 119, (81, 83, 85))
        self.add_clip(41, "2566", "2 rotation", 119, (69, 71, 73, 74))
        self.add_clip(42, "2567", "3 climb", 119, (90, 92))
        for did in ("2593", "2617"):
            self.device_params[did] = [{"name": "Device On", "value": "On"},
                                       {"name": "Threshold", "value": -12},
                                       {"name": "S/C On", "value": "On"}]

    # --- Set builders -------------------------------------------------------
    def add_track(self, tid, name, index, muted=False, devices=()):
        record = {"id": tid, "name": name, "trackIndex": index, "type": "midi",
                  "arrangementClipCount": 0, "sessionClipCount": 0,
                  "deviceCount": len(devices)}
        if muted:
            record["state"] = "muted"
        self.tracks.append(record)
        self.devices[tid] = [{"id": did, "name": dname, "type": dtype,
                              "path": "t%d/d%d" % (index, i)}
                             for i, (did, dname, dtype) in enumerate(devices)]
        return record

    def track_by_id(self, tid):
        return next(t for t in self.tracks if t["id"] == tid)

    def add_clip(self, index, cid, name, start_bar, pitches, muted=False):
        record = {"id": cid, "name": name, "arrangementStart": "%d|1" % start_bar,
                  "arrangementLength": "4bar", "start": "1|1", "end": "5|1",
                  "length": "4bar", "looping": True, "timeSignature": "4/4",
                  "notes": _pp_notes(pitches)}
        self.arrangement.setdefault(index, []).append(record)
        track = next(t for t in self.tracks if t["trackIndex"] == index)
        track["arrangementClipCount"] = len(self.arrangement[index])
        start = (start_bar - 1) * 4.0
        self.clip_live[cid] = {
            "path": "live_set tracks %d arrangement_clips %d" % (index, len(self.arrangement[index]) - 1),
            "values": {"name": name, "loop_start": 0.0, "loop_end": 16.0, "start_marker": 0.0,
                       "end_marker": 16.0, "looping": 1, "start_time": start,
                       "end_time": start + 16.0, "signature_numerator": 4,
                       "signature_denominator": 4, "is_midi_clip": 1, "muted": 1 if muted else 0},
            "notes": _live_notes(pitches)}
        return record

    def remove_clip(self, cid):
        for index, clips in self.arrangement.items():
            for clip in list(clips):
                if clip["id"] == cid:
                    clips.remove(clip)
                    next(t for t in self.tracks if t["trackIndex"] == index)["arrangementClipCount"] = len(clips)
        self.clip_live.pop(cid, None)

    def move_clip_to_track(self, cid, index):
        moved = None
        for clips in self.arrangement.values():
            for clip in list(clips):
                if clip["id"] == cid:
                    clips.remove(clip)
                    moved = clip
        self.arrangement.setdefault(index, []).append(moved)
        self.clip_live[cid]["path"] = "live_set tracks %d arrangement_clips %d" % (
            index, len(self.arrangement[index]) - 1)     # Live's path follows the object
        for track in self.tracks:
            track["arrangementClipCount"] = len(self.arrangement.get(track["trackIndex"], []))

    def set_muted(self, tid, muted):
        track = self.track_by_id(tid)
        if muted:
            track["state"] = "muted"
        else:
            track.pop("state", None)

    def replace_notes(self, cid, pitches):
        self.clip_live[cid]["notes"] = _live_notes(pitches)
        for clips in self.arrangement.values():
            for clip in clips:
                if clip["id"] == cid:
                    clip["notes"] = _pp_notes(pitches)

    def remove_device(self, tid, did):
        self.devices[tid] = [d for d in self.devices[tid] if d["id"] != did]

    def recycle_id(self, cid, as_kind, on_tid="21"):
        """Live reuses object ids within a run: the clip ``cid`` is deleted and
        the next object created — a device on ``on_tid`` or a new track —
        gets its id (observed live 2026-09-30: a deleted Utility's 2641 came
        back as a duplicated track's id)."""
        self.remove_clip(cid)
        if as_kind == "device":
            index = self.track_by_id(on_tid)["trackIndex"]
            self.devices.setdefault(on_tid, []).append(
                {"id": cid, "name": "Utility", "type": "audio-effect: Utility",
                 "path": "t%d/d%d" % (index, len(self.devices[on_tid]))})
            self.device_params[cid] = [{"name": "Device On", "value": "On"}]
        elif as_kind == "track":
            self.add_track(cid, "%s-Recycled" % cid, max(t["trackIndex"] for t in self.tracks) + 1)
        else:
            raise AssertionError(as_kind)

    def _object_path(self, oid):
        """The Live object path an id names now: a clip, a device, a track, or None."""
        for track in self.tracks:
            if track["id"] == oid:
                return "live_set tracks %d" % track["trackIndex"]
        for tid, devs in self.devices.items():
            for n, dev in enumerate(devs):
                if dev["id"] == oid:
                    return "live_set tracks %d devices %d" % (self.track_by_id(tid)["trackIndex"], n)
        return None

    def move_device(self, did, from_tid, to_tid):
        entry = next(d for d in self.devices[from_tid] if d["id"] == did)
        self.devices[from_tid] = [d for d in self.devices[from_tid] if d["id"] != did]
        index = self.track_by_id(to_tid)["trackIndex"]
        self.devices.setdefault(to_tid, []).append(
            dict(entry, path="t%d/d%d" % (index, len(self.devices[to_tid]))))

    def add_group(self, gid, name, index, members, muted=False, soloed=False):
        """A group track (isGroup) whose ``members`` (track ids) carry groupId."""
        record = {"id": gid, "name": name, "trackIndex": index, "type": "group",
                  "isGroup": True, "arrangementClipCount": 0, "sessionClipCount": 0,
                  "deviceCount": 0}
        if muted:
            record["state"] = "muted"
        elif soloed:
            record["state"] = "soloed"
        self.tracks.append(record)
        for tid in members:
            track = self.track_by_id(tid)
            track["groupId"] = gid
            track["isGroupMember"] = True
        return record

    def restart_live(self, renumber=True):
        """Live quit and reopened the same .als: a new process incarnation and
        Live root (a new runtime epoch); every object id is renumbered unless
        ``renumber`` is False (the coincidence the resolver must not trust)."""
        self.incarnation = {"pid": self.incarnation["pid"] + 1000,
                            "start_time": "Wed Sep 30 09:00:00 2026"}
        self.root_id = self.root_id + "-restart"
        if not renumber:
            return
        def new(old):
            return str(int(old) + 1000)
        for track in self.tracks:
            track["id"] = new(track["id"])
            if track.get("groupId"):
                track["groupId"] = new(track["groupId"])
        for clips in self.arrangement.values():
            for clip in clips:
                clip["id"] = new(clip["id"])
        self.clip_live = {new(cid): live for cid, live in self.clip_live.items()}
        self.devices = {new(tid): [dict(d, id=new(d["id"])) for d in devs]
                        for tid, devs in self.devices.items()}
        self.device_params = {new(did): params for did, params in self.device_params.items()}

    # --- handlers ---------------------------------------------------------------
    def _live_api(self, args):
        path = args.get("path")
        if path == "live_set":
            results = []
            for operation in args["operations"]:
                if operation.get("property") != "file_path":
                    raise AssertionError("live_set operation %r not modelled" % operation)
                results.append({"operation": {"property": "file_path", "type": "getProperty"},
                                "result": self.file_path})
            return json_response({"id": self.root_id, "path": "live_set", "results": results})
        if isinstance(path, str) and path.startswith("id "):
            live = self.clip_live.get(path[3:])
            if live is None:
                other = self._object_path(path[3:])
                if other is None:
                    return error_response("Error: id \"%s\" does not exist" % path[3:])
                # a recycled id: Live answers for the object that holds it now
                # (its own properties; a clip property or method yields nothing)
                results = []
                for operation in args["operations"]:
                    if operation.get("type") == "getProperty":
                        results.append({"operation": {"property": operation["property"],
                                                      "type": "getProperty"},
                                        "result": ("Utility" if operation["property"] == "name"
                                                   else None)})
                    else:
                        results.append({"operation": dict(operation), "result": None})
                return json_response({"id": path[3:], "path": other, "results": results})
            results = []
            for operation in args["operations"]:
                if operation.get("type") == "getProperty" and operation.get("property") in LIVE_PROPS:
                    results.append({"operation": {"property": operation["property"],
                                                  "type": "getProperty"},
                                    "result": live["values"][operation["property"]]})
                elif operation.get("method") == "get_notes_extended":
                    pitch, span, start, length = operation["args"]
                    notes = [dict(n) for n in live["notes"]
                             if pitch <= n["pitch"] < pitch + span and start <= n["start_time"] < start + length]
                    results.append({"operation": {"args": list(operation["args"]),
                                                  "method": "get_notes_extended", "type": "call"},
                                    "result": json.dumps({"notes": notes})})
                else:
                    raise AssertionError("clip operation %r not modelled" % operation)
            return json_response({"id": path[3:], "path": live["path"], "results": results})
        return error_response("Error: live-api path %r not modelled" % (path,))

    def _read_live_set(self, args):
        payload = {"name": "NOCTURNEv4", "tempo": 92, "timeSignature": "4/4",
                   "scale": "C Major", "regularTrackCount": len(self.tracks),
                   "returnTrackCount": 0, "sceneCount": 8}
        if "tracks" in (args.get("include") or []):
            payload["tracks"] = [dict(t) for t in sorted(self.tracks, key=lambda t: t["trackIndex"])]
            payload["returnTracks"] = []
            payload["masterTrack"] = {"name": "Main", "type": "master"}
        return json_response(payload)

    def _read_track(self, args):
        include = args.get("include") or []
        if "id" in args:
            track = next((t for t in self.tracks if t["id"] == args["id"]), None)
            if track is None:
                return error_response("Error executing tool 'ppal-read-track': readTrack "
                                      "failed: id \"%s\" does not exist" % args["id"])
        else:
            track = next((t for t in self.tracks if t["trackIndex"] == args.get("trackIndex")), None)
            if track is None:
                return error_response("Error executing tool 'ppal-read-track': readTrack "
                                      "failed: trackIndex %s does not exist" % args.get("trackIndex"))
        out = {key: track[key] for key in ("id", "name", "trackIndex", "type",
                                           "deviceCount", "arrangementClipCount")}
        if "state" in track:
            out["state"] = track["state"]
        if "session-clips" in include:
            out["sessionClips"] = []
        if "arrangement-clips" in include:
            out["arrangementClips"] = [
                {k: v for k, v in clip.items() if k != "notes" or "notes" in include}
                for clip in self.arrangement.get(track["trackIndex"], [])]
        if "devices" in include:
            out["devices"] = [dict(d) for d in self.devices.get(track["id"], [])]
        return json_response(out)

    def _read_device(self, args):
        did = args.get("id")
        params = self.device_params.get(did)
        owner = next((tid for tid, devs in self.devices.items() if any(d["id"] == did for d in devs)), None)
        if params is None or owner is None:
            return error_response("Error: id \"%s\" does not exist" % did)
        entry = next(d for d in self.devices[owner] if d["id"] == did)
        # reply shape: the 2026-09-29 live read (sc_step5: `parameters` [{name, value}]);
        # no capture of ppal-read-device exists in the fixtures
        return json_response({"id": did, "name": entry["name"], "path": entry["path"],
                              "type": entry["type"],
                              "parameters": [dict(p) for p in params]})

    def pp_session(self):
        handshake = self.handshake_for({"live_api": True, "ppal-live-api": True})
        return pp_session.PPSession(self, handshake)


@on_both_versions
class ExperimentToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger_path = os.path.join(self._tmp.name, "ledger.json")
        self.decisions_path = os.path.join(self._tmp.name, "piece_decisions.json")
        patcher = mock.patch.object(ledger, "LEDGER_PATH", self.ledger_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(piece_context, "_decision_path",
                                    lambda path=None: path or self.decisions_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.device = V4Device()
        pp_session.set_live_process_incarnation_resolver(lambda: dict(self.device.incarnation))
        self.addCleanup(pp_session.set_live_process_incarnation_resolver, None)

        @asynccontextmanager
        async def fake_operation(*_a, **_k):
            yield self.device.pp_session()

        patcher = mock.patch.object(pp_session, "operation_session", fake_operation)
        patcher.start()
        self.addCleanup(patcher.stop)
        # journal copies under the temp dir — never the .scratch originals
        self.peak = self.journal("v4-peak-compose.json", PEAK_JOURNAL)
        self.impact = self.journal("impact-drums.json", IMPACT_JOURNAL)
        self.sidechain = self.journal("sidechain.json", SIDECHAIN_JOURNAL)

    def journal(self, name, data):
        path = os.path.join(self._tmp.name, name)
        with open(path, "w") as fh:
            json.dump(data, fh)
        return path

    def rows(self):
        return experiments.all_rows(ledger.load(self.ledger_path))

    # --- register -------------------------------------------------------------
    def test_preview_verifies_against_the_set_and_writes_nothing(self):
        out = server.register_arrangement_candidates(journal_path=self.peak)
        self.assertTrue(out["ok"])
        self.assertFalse(out["written"])
        self.assertEqual(out["writes"], 0)
        verified = {c["label"]: c for c in out["verified"]}
        self.assertEqual(set(verified), {"peak line 1 space", "peak line 2 rotation",
                                         "peak line 3 climb"})
        self.assertEqual(verified["peak line 2 rotation"]["track"]["muted"], True)
        self.assertEqual(verified["peak line 1 space"]["track"]["muted"], False)
        (refused,) = out["refused"]
        self.assertEqual(refused["label"], "peak drums (Ritual cell)")
        self.assertIn("2564", refused["reason"])
        self.assertEqual(self.rows(), [])
        self.assertFalse(os.path.exists(self.ledger_path))
        self.assertIn("confirm=True", out["note"])
        self.assertTrue(all(name.startswith("ppal-read") or name == "ppal-live-api"
                            for name, _ in self.device.wire_calls))

    def test_confirm_registers_rows_read_from_the_track_not_the_journal(self):
        out = server.register_arrangement_candidates(journal_path=self.impact,
                                                     set_path=SET_PATH, confirm=True)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["written"])
        (entry,) = out["registered"]
        self.assertEqual(entry["label"], "drums, whole track (impact)")
        self.assertEqual(entry["clip_ids"], ["2584", "2591", "2586"])
        self.assertNotIn("2585", entry["clip_ids"])
        (row,) = self.rows()
        self.assertEqual(row["id"], entry["entry_id"])
        self.assertTrue(row["recovered"])
        self.assertEqual(row["set_identity"]["file_path"], SET_PATH)
        self.assertEqual(row["set_identity"]["set_id"], SET_PATH)
        self.assertIn(ROOT_ID, row["set_identity"]["runtime_epoch"])
        self.assertEqual([c["n_notes"] for c in row["clips"]], [2, 3, 1])
        self.assertEqual(row["clips"][1]["arr_start"], 216.0)
        self.assertEqual(ledger.load(self.ledger_path)["entries"], [])
        self.assertIn("judge_answer", out["note"])

    def test_effect_trials_register_on_his_tracks_with_read_params(self):
        out = server.register_arrangement_candidates(journal_path=self.sidechain,
                                                     set_path=SET_PATH, confirm=True)
        self.assertTrue(out["ok"], out)
        rows = sorted(self.rows(), key=lambda r: r["track"]["id"])
        self.assertEqual([r["kind"] for r in rows], [experiments.EFFECT_KIND] * 2)
        self.assertEqual([r["track"]["name"] for r in rows], ["32-Omnisphere", "25-Bassline"])
        self.assertFalse(any(r["track"]["sidekick"] for r in rows))
        self.assertEqual(rows[1]["devices"][0]["params"],
                         {"Device On": "On", "Threshold": -12, "S/C On": "On"})
        self.assertEqual(ledger.load(self.ledger_path)["arrangement_experiments"], [])
        self.assertEqual(ledger.load(self.ledger_path)["entries"], [])

    def test_confirming_the_same_journal_twice_registers_once(self):
        first = server.register_arrangement_candidates(journal_path=self.peak, confirm=True)
        second = server.register_arrangement_candidates(journal_path=self.peak, confirm=True)
        self.assertEqual(len(self.rows()), 3)
        self.assertTrue(all(c["already_registered"] for c in second["registered"]))
        self.assertFalse(any(c["already_registered"] for c in first["registered"]))
        self.assertEqual(sorted(c["entry_id"] for c in second["registered"]),
                         sorted(c["entry_id"] for c in first["registered"]))
        self.assertIn("already registered", second["note"])
        # a second label for clip 2565 is the same row, not a second one
        out = server.register_arrangement_candidates(
            items=[{"kind": "arrangement_experiment", "label": "the airy one",
                    "track_id": "2483", "clip_ids": ["2565"]}], set_path=SET_PATH, confirm=True)
        self.assertEqual(len(self.rows()), 3)
        (entry,) = out["registered"]
        self.assertTrue(entry["already_registered"])
        self.assertEqual(entry["label"], "peak line 1 space")

    def test_ambiguous_label_is_refused_and_never_reaches_the_phrase_path(self):
        ids = self.register_peak()
        data = ledger.load(self.ledger_path)
        for row in data["arrangement_experiments"]:
            row["label"] = "twin"
        ledger._save(data, self.ledger_path)
        import gears
        with mock.patch.object(gears, "judge_answer",
                               side_effect=AssertionError("phrase learning ran")) as learn:
            with self.assertRaisesRegex(ValueError, "ambiguous") as ctx:
                server.judge_answer("keep", entry_id="twin", reason="x")
        learn.assert_not_called()
        for entry_id in ids.values():
            self.assertIn(entry_id, str(ctx.exception))

    def test_refuses_when_another_set_is_open(self):
        self.device.file_path = "/Users/x/other/NOCTURNEv3.als"
        out = server.register_arrangement_candidates(journal_path=self.peak, confirm=True)
        self.assertFalse(out["ok"])
        self.assertIn("NOCTURNEv3.als", out["error"])
        self.assertEqual(self.rows(), [])

    def test_journal_without_a_set_needs_set_path(self):
        out = server.register_arrangement_candidates(journal_path=self.impact, confirm=True)
        self.assertFalse(out["ok"])
        self.assertIn("set_path", out["error"])
        self.assertEqual(self.device.wire_calls, [])

    def test_items_are_verified_like_a_journal(self):
        items = [{"kind": "arrangement_experiment", "label": "line 3", "track_id": "2537",
                  "clip_ids": ["2567"]},
                 {"kind": "arrangement_experiment", "label": "ghost", "track_id": "2537",
                  "clip_ids": ["2599"]}]
        out = server.register_arrangement_candidates(items=items, set_path=SET_PATH, confirm=True)
        self.assertTrue(out["ok"])
        self.assertEqual([c["label"] for c in out["registered"]], ["line 3"])
        self.assertEqual([c["label"] for c in out["refused"]], ["ghost"])

    def test_live_unreachable_is_reported_not_written(self):
        @asynccontextmanager
        async def dead(*_a, **_k):
            raise ConnectionError("refused")
            yield  # pragma: no cover

        with mock.patch.object(pp_session, "operation_session", dead):
            out = server.register_arrangement_candidates(journal_path=self.peak, confirm=True)
        self.assertFalse(out["ok"])
        self.assertIn("refused", out["error"])
        self.assertEqual(self.rows(), [])

    # --- judge ----------------------------------------------------------------
    def register_peak(self):
        out = server.register_arrangement_candidates(journal_path=self.peak, confirm=True)
        self.assertTrue(out["ok"], out)
        return {c["label"]: c["entry_id"] for c in out["registered"]}

    def test_judge_answer_routes_before_note_learning_and_never_bans(self):
        ids = self.register_peak()
        import gears
        import jam_lifecycle
        with mock.patch.object(gears, "judge_answer",
                               side_effect=AssertionError("phrase learning ran")) as learn, \
                mock.patch.object(jam_lifecycle, "record_judgment",
                                  side_effect=AssertionError("phrase judgment ran")) as record, \
                mock.patch.object(server, "_record_ref_tally_for_source",
                                  side_effect=AssertionError("ref tally ran")) as tally:
            out = server.judge_answer("drop", entry_id=ids["peak line 2 rotation"],
                                      reason="too busy")
        self.assertEqual(out["routed"], "arrangement_feedback")
        self.assertEqual(out["verdict"], "drop")
        self.assertEqual(out["entry_id"], ids["peak line 2 rotation"])
        learn.assert_not_called()
        record.assert_not_called()
        tally.assert_not_called()
        self.assertEqual(sorted(s["label"] for s in out["siblings_unjudged"]),
                         ["peak line 1 space", "peak line 3 climb"])
        row = experiments.find_row(ledger.load(self.ledger_path), ids["peak line 2 rotation"])
        self.assertEqual(row["immediate_verdict"], "drop")
        with open(self.decisions_path) as fh:
            (record_,) = json.load(fh)["decisions"]
        self.assertEqual((record_["kind"], record_["source"], record_["verdict"]),
                         ("arrangement_feedback", "model", "drop"))
        self.assertEqual(record_["set_key"], SET_PATH)

    def test_a_phrase_entry_id_takes_the_phrase_path_even_when_a_label_matches_it(self):
        ids = self.register_peak()
        phrase = ledger.record_write(
            clip_name="answer", slot="3/0", source="catalogue",
            answer_notes=[{"pitch": 60, "start": 0, "duration": 1, "velocity": 80}],
            set_identity={"set_id": SET_PATH, "runtime_epoch": "x", "file_path": SET_PATH},
            clip_ref={"clip_id": "c1", "track_id": "jam", "track": "SIDEKICK JAM",
                      "surface": "session", "path": "t3/s0"},
            path=self.ledger_path)
        data = ledger.load(self.ledger_path)
        data["arrangement_experiments"][0]["label"] = phrase["id"]
        ledger._save(data, self.ledger_path)
        import gears
        with mock.patch.object(gears, "judge_answer", return_value={"verdict": "keep"}) as learn:
            out = server.judge_answer("keep", entry_id=phrase["id"])
        learn.assert_called_once()
        self.assertNotIn("routed", out)
        self.assertEqual(out["resolved_from"]["entry_id"], phrase["id"])
        self.assertTrue(all(r["immediate_verdict"] is None for r in self.rows()))

    def test_judge_by_label_and_phrase_ids_still_take_the_phrase_path(self):
        ids = self.register_peak()
        out = server.judge_answer("keep", entry_id="peak line 1 space", reason="yes")
        self.assertEqual(out["entry_id"], ids["peak line 1 space"])
        with self.assertRaisesRegex(ValueError, "unknown ledger entry"):
            server.judge_answer("keep", entry_id="e-nothere")

    # --- close ----------------------------------------------------------------
    def test_close_session_verifies_the_intended_change_and_reports_the_subtotal(self):
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 2 rotation"], reason="2")
        server.judge_answer("keep", entry_id=ids["peak line 3 climb"], reason="also")
        server.judge_answer("drop", entry_id=ids["peak line 1 space"], reason="flat")
        self.device.set_muted("2515", False)       # he unmuted the rotation
        out = server.close_session()
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["judged"], 0)          # the phrase denominator is untouched
        arrangement = out["arrangement"]
        self.assertEqual((arrangement["adopted"], arrangement["staged_kept"],
                          arrangement["drop"], arrangement["not_adopted"]), (1, 1, 1, 0))
        self.assertEqual(arrangement["resolved_judged"], 3)
        self.assertEqual(arrangement["survived"], 2)
        self.assertEqual(arrangement["survival_rate"], 0.667)
        self.assertEqual(out["effect_trials"]["rows"], 0)
        states = {r["label"]: r["terminal_state"] for r in self.rows()}
        self.assertEqual(states, {"peak line 1 space": "drop", "peak line 2 rotation": "adopted",
                                  "peak line 3 climb": "staged_kept"})

    def test_close_not_adopted_when_the_kept_variant_was_deleted(self):
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 2 rotation"], reason="2")
        self.device.remove_clip("2566")
        out = server.close_session()
        self.assertEqual(out["arrangement"]["not_adopted"], 1)
        self.assertEqual(out["arrangement"]["survival_rate"], 0.0)

    def test_close_adopted_when_the_line_moved_onto_his_track(self):
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 2 rotation"], reason="2")
        self.device.move_clip_to_track("2566", 24)   # onto Bassline
        out = server.close_session()
        entry = next(e for e in out["arrangement"]["entries"]
                     if e["entry_id"] == ids["peak line 2 rotation"])
        self.assertEqual((entry["state"], entry["why"]), ("adopted", "on_his_originals"))

    def test_close_leaves_unknown_when_the_content_changed_or_the_read_is_partial(self):
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 2 rotation"], reason="2")
        self.device.replace_notes("2566", (69, 71, 73, 76))
        out = server.close_session()
        self.assertEqual(out["arrangement"]["unknown"], 1)
        self.assertEqual(out["arrangement"]["resolved_judged"], 0)
        self.assertIsNone(out["arrangement"]["survival_rate"])
        row = experiments.find_row(ledger.load(self.ledger_path), ids["peak line 2 rotation"])
        self.assertIsNone(row["terminal_state"])
        self.assertEqual(row["observation"]["reason"], "content_changed")
        # a partial device read (timeout warning) keeps every row unresolved too
        self.device.replace_notes("2566", (69, 71, 73, 74))
        self.device.device_warnings = ["WARNING: read timed out after 5000ms"]
        out = server.close_session()
        self.assertFalse(out["inventory_coverage"])
        self.assertEqual(out["arrangement"]["unknown"], 3)   # the two unjudged rows too
        self.assertEqual(out["arrangement"]["resolved_judged"], 0)
        for label in ("peak line 1 space", "peak line 2 rotation", "peak line 3 climb"):
            row = experiments.find_row(ledger.load(self.ledger_path), ids[label])
            self.assertIsNone(row["terminal_state"])
            self.assertEqual(row["observation"]["reason"], "inventory_incomplete")

    def test_close_effect_trials_on_their_own_line(self):
        out = server.register_arrangement_candidates(journal_path=self.sidechain,
                                                     set_path=SET_PATH, confirm=True)
        ids = {c["label"]: c["entry_id"] for c in out["registered"]}
        server.judge_answer("keep", entry_id=ids["SIDEKICK duck ← drums on 25-Bassline"],
                            reason="ok for compressor")
        server.judge_answer("keep", entry_id=ids["SIDEKICK duck ← drums on 32-Omnisphere"],
                            reason="ok for compressor")
        self.device.remove_device("1113", "2617")
        self.device.device_params["2593"][1] = {"name": "Threshold", "value": -20}
        out = server.close_session()
        line = out["effect_trials"]
        self.assertEqual((line["kept"], line["removed"], line["resolved_judged"]), (1, 1, 2))
        self.assertEqual(line["kept_rate"], 0.5)
        self.assertEqual(out["arrangement"]["rows"], 0)
        kept = next(r for r in self.rows() if r["terminal_state"] == "kept")
        self.assertEqual(kept["observation"]["params_changed"], ["2593"])

    # --- Live restart (blocker 1) ---------------------------------------------
    def register_all(self):
        ids = self.register_peak()
        out = server.register_arrangement_candidates(journal_path=self.impact,
                                                     set_path=SET_PATH, confirm=True)
        ids.update({c["label"]: c["entry_id"] for c in out["registered"]})
        out = server.register_arrangement_candidates(journal_path=self.sidechain,
                                                     set_path=SET_PATH, confirm=True)
        ids.update({c["label"]: c["entry_id"] for c in out["registered"]})
        return ids

    def test_live_restart_with_renumbered_ids_scores_by_content_not_by_absence(self):
        ids = self.register_all()
        server.judge_answer("keep", entry_id=ids["peak line 1 space"], reason="1")
        server.judge_answer("keep", entry_id=ids["SIDEKICK duck \u2190 drums on 25-Bassline"],
                            reason="ok")
        self.device.restart_live(renumber=True)
        out = server.close_session()
        arrangement = out["arrangement"]
        self.assertEqual((arrangement["not_adopted"], arrangement["abandoned"]), (0, 0))
        self.assertEqual(arrangement["adopted"], 1)
        self.assertEqual(arrangement["survival_rate"], 1.0)
        states = {r["label"]: (r["terminal_state"], r["observation"]["state"]) for r in self.rows()}
        self.assertEqual(states["peak line 1 space"], ("adopted", "adopted"))
        self.assertEqual(states["peak line 2 rotation"], (None, "unjudged"))
        self.assertEqual(states["drums, whole track (impact)"], (None, "unjudged"))
        self.assertEqual(states["SIDEKICK duck \u2190 drums on 25-Bassline"], ("kept", "kept"))
        one = experiments.find_row(ledger.load(self.ledger_path), ids["peak line 1 space"])
        self.assertEqual(one["observation"]["rebound"], {"2565": "3565"})
        self.assertFalse(one["observation"]["same_epoch"])

    def test_live_restart_with_kept_ids_still_checks_content(self):
        ids = self.register_all()
        server.judge_answer("keep", entry_id=ids["peak line 1 space"], reason="1")
        self.device.restart_live(renumber=False)
        self.device.replace_notes("2565", (81, 83, 86))
        out = server.close_session()
        self.assertEqual(out["arrangement"]["adopted"], 0)
        self.assertIsNone(out["arrangement"]["survival_rate"])
        one = experiments.find_row(ledger.load(self.ledger_path), ids["peak line 1 space"])
        self.assertIsNone(one["terminal_state"])
        self.assertEqual(one["observation"]["reason"], "cross_epoch_unresolved")

    def test_live_restart_then_the_material_is_gone_stays_unknown(self):
        ids = self.register_all()
        server.judge_answer("keep", entry_id=ids["peak line 1 space"], reason="1")
        self.device.restart_live(renumber=True)
        self.device.remove_clip("3565")
        out = server.close_session()
        self.assertEqual(out["arrangement"]["not_adopted"], 0)
        self.assertEqual(out["arrangement"]["unknown"], 1)

    def test_muted_group_parent_reads_as_silent(self):
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 1 space"], reason="1")
        self.device.add_group("g900", "PEAK LINES", 39, ["2483"], muted=True)
        out = server.close_session()
        self.assertEqual(out["arrangement"]["staged_kept"], 1)
        self.assertEqual(out["arrangement"]["adopted"], 0)

    def line_1_entry(self, out, ids):
        return next(e for e in out["arrangement"]["entries"]
                    if e["entry_id"] == ids["peak line 1 space"])

    def test_an_unrelated_solo_reads_as_silent_never_adopted(self):
        # two-month review finding 6: KEEP line 1, solo Bassline, close — the
        # close read adopted=1 / still_audible_on_sidekick=1
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 1 space"], reason="1")
        self.device.track_by_id("21")["state"] = "soloed"
        out = server.close_session()
        self.assertEqual((out["arrangement"]["adopted"], out["arrangement"]["staged_kept"]), (0, 1))
        self.assertEqual(out["arrangement"]["adopted_by"]["still_audible_on_sidekick"], 0)
        entry = self.line_1_entry(out, ids)
        self.assertEqual((entry["state"], entry["why"], entry["audibility"]),
                         ("staged_kept", "present_but_silent", "silent_by_solo"))

    def test_a_soloed_group_parent_keeps_its_member_audible(self):
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 1 space"], reason="1")
        self.device.add_group("g900", "PEAK LINES", 39, ["2483"], soloed=True)
        out = server.close_session()
        self.assertEqual((out["arrangement"]["adopted"], out["arrangement"]["staged_kept"]), (1, 0))
        self.assertEqual(self.line_1_entry(out, ids)["audibility"], "audible")

    def test_a_soloed_group_elsewhere_silences_the_line(self):
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 1 space"], reason="1")
        self.device.add_group("g901", "SYNTHS", 38, ["21"], soloed=True)
        out = server.close_session()
        self.assertEqual((out["arrangement"]["adopted"], out["arrangement"]["staged_kept"]), (0, 1))
        self.assertEqual(self.line_1_entry(out, ids)["audibility"], "silent_by_solo")

    def test_a_parent_group_the_overview_lacks_is_never_adopted(self):
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 1 space"], reason="1")
        self.device.track_by_id("2483")["groupId"] = "g-unlisted"   # a parent the read never lists
        out = server.close_session()
        self.assertEqual((out["arrangement"]["adopted"], out["arrangement"]["staged_kept"]), (0, 1))
        entry = self.line_1_entry(out, ids)
        self.assertEqual((entry["why"], entry["audibility"]), ("present_but_silent", "parent_unknown"))

    # --- scope and retry (items 4 + 5) -------------------------------------------
    def phrase_row_tonight(self):
        epoch = pp_session.build_runtime_epoch(self.device.root_id, self.device.incarnation)
        return ledger.record_write(
            clip_name="answer", slot="3/0", source="catalogue",
            answer_notes=[{"pitch": 60, "start": 0, "duration": 1, "velocity": 80}],
            set_identity={"set_id": SET_PATH, "runtime_epoch": epoch, "file_path": SET_PATH},
            clip_ref={"clip_id": "c-gone", "track_id": "jam", "track": "SIDEKICK JAM",
                      "surface": "session", "path": "t3/s0"},
            path=self.ledger_path)

    def test_a_plain_close_re_resolves_unknown_rows_of_a_sealed_mixed_night(self):
        phrase = self.phrase_row_tonight()
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 2 rotation"], reason="2")
        self.assertEqual(experiments.find_row(ledger.load(self.ledger_path),
                                              ids["peak line 2 rotation"])["session_id"],
                         phrase["session_id"])
        self.device.replace_notes("2566", (69, 71, 73, 76))    # content changed → unknown
        first = server.close_session()
        self.assertTrue(first["ok"])
        self.assertEqual(first["rate_status"], "definitive")   # the phrase night is sealed
        self.assertEqual(first["arrangement"]["unknown"], 1)
        # he restores the line and closes again, plainly: the phrase close has
        # nothing open, the experiment rows still re-resolve
        self.device.replace_notes("2566", (69, 71, 73, 74))
        self.device.set_muted("2515", False)
        second = server.close_session()
        self.assertFalse(second["ok"])
        self.assertTrue(second["experiments_closed"])
        self.assertEqual(second["arrangement"]["adopted"], 1)
        self.assertEqual(second["experiment_sessions"], [phrase["session_id"]])
        row = experiments.find_row(ledger.load(self.ledger_path), ids["peak line 2 rotation"])
        self.assertEqual(row["terminal_state"], "adopted")

    def test_the_phrase_close_keeps_its_own_session_ids_beside_the_experiment_ones(self):
        # night S0: a phrase row (its clip gone → definitive) + a KEEP whose
        # content changed (unknown); night S1: a new phrase row
        s0_phrase = self.phrase_row_tonight()
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 2 rotation"], reason="2")
        self.device.replace_notes("2566", (69, 71, 73, 76))
        first = server.close_session()
        self.assertEqual(first["rate_status"], "definitive")
        s1_phrase = self.phrase_row_tonight()
        self.assertNotEqual(s1_phrase["session_id"], s0_phrase["session_id"])
        self.device.replace_notes("2566", (69, 71, 73, 74))
        self.device.set_muted("2515", False)
        second = server.close_session()
        self.assertTrue(second["ok"])
        self.assertEqual(second["session_ids"], [s1_phrase["session_id"]])
        self.assertEqual(second["experiment_sessions"],
                         sorted([s0_phrase["session_id"], s1_phrase["session_id"]]))
        self.assertEqual(second["arrangement"]["adopted"], 1)

    # --- round 3 ------------------------------------------------------------------
    def test_recreated_material_rebinds_the_pending_row_and_closes_by_the_new_id(self):
        ids = self.register_peak()
        self.device.remove_clip("2565")
        self.device.add_clip(40, "90001", "1 space", 119, (81, 83, 85))
        # the journal still names 2565: that candidate is refused (not in the Set) …
        stale = server.register_arrangement_candidates(journal_path=self.peak, confirm=True)
        self.assertIn("2565", next(r["reason"] for r in stale["refused"]
                                   if r["label"] == "peak line 1 space"))
        # … the recreated clip, named as it now is, rebinds the pending row
        out = server.register_arrangement_candidates(
            items=[{"kind": "arrangement_experiment", "label": "peak line 1 space",
                    "track_id": "2483", "clip_ids": ["90001"]}], set_path=SET_PATH, confirm=True)
        (line_1,) = out["registered"]
        self.assertEqual(line_1["entry_id"], ids["peak line 1 space"])
        self.assertTrue(line_1["already_registered"])
        self.assertEqual(line_1["rebound"], {"2565": "90001"})
        self.assertEqual(line_1["clip_ids"], ["90001"])
        self.assertEqual(len(self.rows()), 3)
        server.judge_answer("keep", entry_id=ids["peak line 1 space"], reason="1")
        close = server.close_session()
        self.assertEqual(close["arrangement"]["not_adopted"], 0)
        self.assertEqual(close["arrangement"]["adopted"], 1)
        row = experiments.find_row(ledger.load(self.ledger_path), ids["peak line 1 space"])
        self.assertEqual(row["observation"]["reason"], "still_audible_on_sidekick")

    def test_recreated_material_under_a_judged_row_is_refused(self):
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 1 space"], reason="1")
        self.device.remove_clip("2565")
        self.device.add_clip(40, "90001", "1 space", 119, (81, 83, 85))
        out = server.register_arrangement_candidates(
            items=[{"kind": "arrangement_experiment", "label": "peak line 1 space",
                    "track_id": "2483", "clip_ids": ["90001"]}], set_path=SET_PATH, confirm=True)
        (refused,) = out["refused"]
        self.assertEqual(out["registered"], [])
        self.assertIn(ids["peak line 1 space"], refused["reason"])
        self.assertEqual(len(self.rows()), 3)
        row = experiments.find_row(ledger.load(self.ledger_path), ids["peak line 1 space"])
        self.assertEqual([c["clip_id"] for c in row["clips"]], ["2565"])

    def test_closing_another_set_never_touches_this_nights_rows(self):
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 2 rotation"], reason="2")
        self.device.file_path = "/Users/x/other/OTHER.als"
        out = server.close_session()
        self.assertFalse(out["ok"])
        self.assertFalse(out["experiments_closed"])
        self.assertEqual(out["arrangement"]["rows"], 0)
        for row in self.rows():
            self.assertIsNone(row["observation"])
            self.assertIsNone(row["terminal_state"])

    def test_session_report_carries_the_subtotal_and_the_pending_advice(self):
        ids = self.register_peak()
        import taste_bridge
        with mock.patch.object(taste_bridge, "rejected_phrases", return_value=[]):
            report = server.session_report()   # the reject cull needs the corpus; not this
        self.assertEqual(report["arrangement"]["rows"], 3)
        self.assertEqual(report["arrangement"]["unjudged"], 3)
        self.assertEqual(report["effect_trials"]["rows"], 0)
        self.assertTrue(any("3 arrangement/effect candidate(s) await judgment" in line
                            and ids["peak line 2 rotation"] in line
                            for line in report["advice"]))
        self.assertEqual(report["scoreboard"]["verified"]["judged"], 0)


class ToolSurfaceTests(unittest.TestCase):
    def test_tool_is_registered_in_the_composing_profiles_and_documented(self):
        for profile in ("compose", "session", "all"):
            self.assertIn("register_arrangement_candidates", profiles.get_profile_tools(profile))
        self.assertNotIn("register_arrangement_candidates", profiles.get_profile_tools("debug"))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config", "claude_mcp.md")) as fh:
            table = fh.read()
        row = next(line for line in table.splitlines()
                   if line.startswith("| `register_arrangement_candidates` |"))
        self.assertIn("`compose`", row)
        self.assertIn("`session`", row)
        with open(os.path.join(root, "docs", "CURSOR_CHAT_PLAYBOOK.md")) as fh:
            playbook = fh.read()
        self.assertIn("## Arrangement candidates", playbook)
        section = playbook.split("## Arrangement candidates", 1)[1].split("\n## ", 1)[0]
        for step in ("register_arrangement_candidates", "1/2/3", "judge_answer", "close_session"):
            self.assertIn(step, section)


if __name__ == "__main__":
    unittest.main()
