"""Tests for mcp_server/moment.py — truthful musical context and tempo parsing."""

from __future__ import annotations

import json
import math
import unittest

import mcp_server.ears as ears
import mcp_server.moment as moment
from mcp_server.pp_session import PPResponse
from tests.pp_fakes import V23, on_both_versions, schema_strict


@schema_strict
class FakePPSession:
    def __init__(self, responses=None, capabilities=None):
        self.responses = responses or {}
        self.capabilities = capabilities or {}
        self.calls = []

    async def call(self, name: str, args: dict | None = None) -> PPResponse:
        self.calls.append((name, args or {}))
        resp = self.responses.get(name)
        if callable(resp):
            return resp(name, args or {})
        if isinstance(resp, PPResponse):
            return resp
        if isinstance(resp, dict):
            return PPResponse(json.dumps({"result": resp}), resp, [])
        if isinstance(resp, str):
            return PPResponse(resp, json.loads(resp).get("result") if resp.startswith("{") else resp, [])
        raise RuntimeError(f"no response queued for {name}")


@on_both_versions
class TestParseTempo(unittest.TestCase):
    def test_fractional_json_tempos(self):
        self.assertEqual(moment.parse_tempo({"tempo": 99.5}), 99.5)
        self.assertEqual(moment.parse_tempo({"tempo": 120.25}), 120.25)
        self.assertEqual(moment.parse_tempo({"result": {"tempo": 99.5}}), 99.5)
        self.assertEqual(moment.parse_tempo({"result": {"tempo": 120.25}}), 120.25)

    def test_pp_response_tempo(self):
        resp = PPResponse(
            json.dumps({"result": {"tempo": 99.5}}),
            {"tempo": 99.5},
            [],
        )
        self.assertEqual(moment.parse_tempo(resp), 99.5)

    def test_string_json_tempo(self):
        raw = '{"result": {"tempo": 120.25}}'
        self.assertEqual(moment.parse_tempo(raw), 120.25)

    def test_legacy_text_tempo(self):
        self.assertEqual(moment.parse_tempo("tempo: 99.5"), 99.5)
        self.assertEqual(moment.parse_tempo('{"tempo": 120.25}'), 120.25)
        self.assertEqual(moment.parse_tempo("tempo: 120"), 120.0)

    def test_rejects_boolean(self):
        self.assertIsNone(moment.parse_tempo({"tempo": True}))
        self.assertIsNone(moment.parse_tempo({"tempo": False}))
        self.assertIsNone(moment.parse_tempo(True))
        self.assertIsNone(moment.parse_tempo(False))

    def test_rejects_nan_and_infinity(self):
        self.assertIsNone(moment.parse_tempo({"tempo": float("nan")}))
        self.assertIsNone(moment.parse_tempo({"tempo": float("inf")}))
        self.assertIsNone(moment.parse_tempo({"tempo": float("-inf")}))

    def test_rejects_negative_or_zero(self):
        self.assertIsNone(moment.parse_tempo({"tempo": 0}))
        self.assertIsNone(moment.parse_tempo({"tempo": -120.0}))

    def test_rejects_missing_or_none(self):
        self.assertIsNone(moment.parse_tempo(None))
        self.assertIsNone(moment.parse_tempo({}))
        self.assertIsNone(moment.parse_tempo({"tempo": None}))
        self.assertIsNone(moment.parse_tempo("no tempo here"))


@on_both_versions
class TestMomentShapeAndIdentity(unittest.TestCase):
    def test_context_alters_cache_identity_digest(self):
        tracks = [{"name": "Lead", "notes": [{"pitch": 60, "start": 0, "duration": 1, "velocity": 80}]}]
        m1 = moment.build_moment(tracks, 120.0, context={"where": "session", "active_slots": {"0": 1}})
        m2 = moment.build_moment(tracks, 120.0, context={"where": "session", "active_slots": {"0": 2}})
        self.assertNotEqual(m1["digest"], m2["digest"])
        self.assertIn("context", m1)
        self.assertEqual(m1["context"]["active_slots"], {"0": 1})

    def test_core_moment_shape_preserved(self):
        tracks = [{"name": "Lead", "notes": [{"pitch": 60, "start": 0, "duration": 1, "velocity": 80}]}]
        m = moment.build_moment(tracks, 120.0)
        expected_keys = {
            "ears", "tempo", "tempo_world", "key", "key_estimate", "scale_pcs",
            "assumptions", "roles", "unheard", "hidden", "context", "digest",
        }
        self.assertTrue(expected_keys.issubset(set(m.keys())))
        self.assertEqual(m["ears"], "live")
        self.assertEqual(m["tempo"], 120.0)

    def test_blind_moment_shape(self):
        b = moment.blind_moment("test reason")
        self.assertEqual(b["ears"], "blind")
        self.assertEqual(b["reason"], "test reason")
        self.assertIn("context", b)


@on_both_versions
class TestMomentPlayingContext(unittest.TestCase):
    def test_stopped_transport_does_not_substitute_row0(self):
        session = FakePPSession(
            responses={
                "ppal-read-live-set": {
                    "tempo": 120.0,
                    "timeSignature": "4/4",
                    "isPlaying": False,
                    "currentSongTime": 0.0,
                    "regularTrackCount": 1,
                    "tracks": [{"trackIndex": 0, "name": "Lead", "type": "midi"}],
                },
            }
        )
        m = moment.get_moment(where="session", session=session)
        self.assertEqual(m["ears"], "live")
        self.assertEqual(m["roles"], {})
        self.assertTrue(any("transport is stopped" in a for a in m["assumptions"]))
        self.assertFalse(m["context"]["transport_playing"])

    def test_playing_scene_not_zero(self):
        def mock_call(name, args):
            if name == "ppal-read-live-set":
                return PPResponse(json.dumps({"result": {
                    "tempo": 120.0,
                    "timeSignature": "4/4",
                    "isPlaying": True,
                    "regularTrackCount": 1,
                    "tracks": [{"trackIndex": 0, "name": "Lead", "type": "midi"}],
                }}), {
                    "tempo": 120.0,
                    "timeSignature": "4/4",
                    "isPlaying": True,
                    "regularTrackCount": 1,
                    "tracks": [{"trackIndex": 0, "name": "Lead", "type": "midi"}],
                }, [])
            if name == "ppal-read-track":
                return PPResponse(json.dumps({"result": {
                    "trackIndex": 0,
                    "playingSlotIndex": 2,
                }}), {"trackIndex": 0, "playingSlotIndex": 2}, [])
            if name == "ppal-read-clip":
                path = args.get("path")
                if path == "t0/s2":
                    return PPResponse(json.dumps({"result": {
                        "name": "Clip Scene 2",
                        "notes": [{"pitch": 64, "start": 0.0, "duration": 1.0, "velocity": 90}],
                        "start": "1|1",
                        "end": "2|1",
                    }}), {
                        "name": "Clip Scene 2",
                        "notes": [{"pitch": 64, "start": 0.0, "duration": 1.0, "velocity": 90}],
                        "start": "1|1",
                        "end": "2|1",
                    }, [])
                raise AssertionError(f"unexpected path read: {path}")
            raise AssertionError(f"unexpected call: {name}")

        session = FakePPSession()
        session.responses = {"ppal-read-live-set": mock_call, "ppal-read-track": mock_call, "ppal-read-clip": mock_call}
        m = moment.get_moment(where="session", session=session)
        self.assertEqual(m["ears"], "live")
        self.assertIn("lead", m["roles"])
        self.assertEqual(m["context"]["active_slots"], {"0": 2})

    def test_stopped_or_muted_track_omitted(self):
        session = FakePPSession(
            capabilities={"live_api": True},
            responses={
                "ppal-read-live-set": {
                    "tempo": 120.0,
                    "timeSignature": "4/4",
                    "regularTrackCount": 1,
                    "tracks": [{"trackIndex": 0, "name": "Lead", "type": "midi"}],
                },
                "ppal-live-api": lambda name, args: PPResponse(json.dumps({"result": {
                    "results": [
                        {"operation": {"type": "getProperty", "property": "mute"}, "result": True},
                        {"operation": {"type": "getProperty", "property": "playing_slot_index"}, "result": 1},
                    ] if args.get("path") == "live_set tracks 0" else [
                        {"operation": {"type": "getProperty", "property": "is_playing"}, "result": True},
                        {"operation": {"type": "getProperty", "property": "current_song_time"}, "result": 8.0},
                    ]
                }}), {"results": [
                    {"operation": {"type": "getProperty", "property": "mute"}, "result": True},
                    {"operation": {"type": "getProperty", "property": "playing_slot_index"}, "result": 1},
                ] if args.get("path") == "live_set tracks 0" else [
                    {"operation": {"type": "getProperty", "property": "is_playing"}, "result": True},
                    {"operation": {"type": "getProperty", "property": "current_song_time"}, "result": 8.0},
                ]}, []),
            }
        )
        m = moment.get_moment(where="session", session=session)
        self.assertEqual(m["roles"], {})
        self.assertTrue(any("muted" in a for a in m["assumptions"]))

    def test_session_override_when_reading_arrangement(self):
        def mock_call(name, args):
            if name == "ppal-read-live-set":
                return PPResponse(json.dumps({"result": {
                    "tempo": 120.0,
                    "timeSignature": "4/4",
                    "isPlaying": True,
                    "currentSongTime": 16.0,
                    "regularTrackCount": 1,
                    "tracks": [{"trackIndex": 0, "name": "Bass", "type": "midi"}],
                }}), {
                    "tempo": 120.0,
                    "timeSignature": "4/4",
                    "isPlaying": True,
                    "currentSongTime": 16.0,
                    "regularTrackCount": 1,
                    "tracks": [{"trackIndex": 0, "name": "Bass", "type": "midi"}],
                }, [])
            if name == "ppal-live-api":
                path = args.get("path")
                if path == "live_set":
                    results = [
                        {"operation": {"type": "getProperty", "property": "is_playing"}, "result": True},
                        {"operation": {"type": "getProperty", "property": "current_song_time"}, "result": 16.0},
                    ]
                else:
                    results = [
                        {"operation": {"type": "getProperty", "property": "mute"}, "result": False},
                        {"operation": {"type": "getProperty", "property": "back_to_arranger"}, "result": False},
                        {"operation": {"type": "getProperty", "property": "playing_slot_index"}, "result": 3},
                    ]
                return PPResponse(json.dumps({"result": {"results": results}}), {"results": results}, [])
            if name == "ppal-read-clip":
                self.assertEqual(args.get("path"), "t0/s3")
                return PPResponse(json.dumps({"result": {
                    "name": "Override Bass",
                    "notes": [{"pitch": 36, "start": 0.0, "duration": 1.0, "velocity": 100}],
                    "start": "1|1",
                    "end": "2|1",
                }}), {
                    "name": "Override Bass",
                    "notes": [{"pitch": 36, "start": 0.0, "duration": 1.0, "velocity": 100}],
                    "start": "1|1",
                    "end": "2|1",
                }, [])
            raise AssertionError(f"unexpected call: {name}")

        session = FakePPSession(capabilities={"live_api": True})
        session.responses = {"ppal-read-live-set": mock_call, "ppal-live-api": mock_call, "ppal-read-clip": mock_call}
        m = moment.get_moment(where="arrangement", bar=5, session=session)
        self.assertIn("Bass", m["context"]["overrides"])
        self.assertIn("bass", m["roles"])
        self.assertTrue(any("override (slot 3) playing over arrangement" in a for a in m["assumptions"]))

    def test_meter_3_4_and_loop_hidden_notes(self):
        def mock_call(name, args):
            if name == "ppal-read-live-set":
                return PPResponse(json.dumps({"result": {
                    "tempo": 120.0,
                    "timeSignature": "3/4",
                    "isPlaying": True,
                    "regularTrackCount": 1,
                    "tracks": [{"trackIndex": 0, "name": "Waltz", "type": "midi"}],
                }}), {
                    "tempo": 120.0,
                    "timeSignature": "3/4",
                    "isPlaying": True,
                    "regularTrackCount": 1,
                    "tracks": [{"trackIndex": 0, "name": "Waltz", "type": "midi"}],
                }, [])
            if name == "ppal-read-track":
                return PPResponse(json.dumps({"result": {
                    "trackIndex": 0,
                    "playingSlotIndex": 0,
                }}), {"trackIndex": 0, "playingSlotIndex": 0}, [])
            if name == "ppal-read-clip":
                return PPResponse(json.dumps({"result": {
                    "name": "Waltz Loop",
                    "timeSignature": "3/4",
                    "start": "1|1",
                    "end": "2|1",
                    "notes": [
                        {"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 80},
                        {"pitch": 64, "start": 1.5, "duration": 1.0, "velocity": 80},
                        {"pitch": 67, "start": 3.5, "duration": 1.0, "velocity": 80},
                    ],
                }}), {
                    "name": "Waltz Loop",
                    "timeSignature": "3/4",
                    "start": "1|1",
                    "end": "2|1",
                    "notes": [
                        {"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 80},
                        {"pitch": 64, "start": 1.5, "duration": 1.0, "velocity": 80},
                        {"pitch": 67, "start": 3.5, "duration": 1.0, "velocity": 80},
                    ],
                }, [])
            raise AssertionError(f"unexpected call: {name}")

        session = FakePPSession()
        session.responses = {"ppal-read-live-set": mock_call, "ppal-read-track": mock_call, "ppal-read-clip": mock_call}
        m = moment.get_moment(where="session", session=session)
        self.assertEqual(m["context"]["meter"], "3/4")
        self.assertEqual(m["context"]["beats_per_bar"], 3.0)
        self.assertEqual(len(m["hidden"]), 1)
        self.assertEqual(m["hidden"][0]["n_hidden"], 1)
        self.assertEqual(m["roles"]["lead"]["n_notes"], 2)


@on_both_versions
class TestParseMeter(unittest.TestCase):
    def test_valid_meters(self):
        self.assertEqual(moment.parse_meter("4/4"), ("4/4", 4.0))
        self.assertEqual(moment.parse_meter("3/4"), ("3/4", 3.0))
        self.assertEqual(moment.parse_meter("5/4"), ("5/4", 5.0))
        self.assertEqual(moment.parse_meter("6/8"), ("6/8", 3.0))
        self.assertEqual(moment.parse_meter("7/8"), ("7/8", 3.5))

    def test_invalid_meters_raise(self):
        invalid = [None, "", "4", "4/0", "0/4", "-4/4", "4/-4", "4/3", "4/5", "4/4/4", "abc", True, False]
        for val in invalid:
            with self.subTest(val=val):
                with self.assertRaises(ValueError):
                    moment.parse_meter(val)

    def test_missing_or_invalid_overview_meter_raises(self):
        import asyncio
        session = FakePPSession(
            responses={
                "ppal-read-live-set": {
                    "tempo": 120.0,
                    # missing timeSignature
                    "isPlaying": False,
                    "regularTrackCount": 1,
                    "tracks": [{"trackIndex": 0, "name": "Lead", "type": "midi"}],
                },
            }
        )
        with self.assertRaises(ValueError):
            asyncio.run(moment.read_moment_live(where="session", session=session))

        m = moment.get_moment(where="session", session=session)
        self.assertEqual(m["ears"], "blind")
        self.assertIn("timeSignature", m["reason"])


@on_both_versions
class TestStoppedArrangementHearsPlayheadBar(unittest.TestCase):
    """Item 7: a stopped Arrangement hears the playhead bar instead of an
    empty room; a stopped Session keeps the empty room (deliberate)."""

    def stopped_session(self, song_time=16.0):
        overview = {
            "tempo": 120.0,
            "timeSignature": "4/4",
            "isPlaying": False,
            "regularTrackCount": 1,
            "tracks": [{"trackIndex": 0, "name": "Lead", "type": "midi"}],
        }
        if song_time is not None:
            overview["currentSongTime"] = song_time
        return FakePPSession(responses={"ppal-read-live-set": overview})

    def stopped_arrangement_session(self):
        session = self.stopped_session()

        def mock_track(name, args):
            return PPResponse(json.dumps({"result": {
                "trackIndex": 0,
                "type": "midi",
                "arrangementClips": [{
                    "id": "clip-a",
                    "name": "Lead §1",
                    "arrangementStart": "5|1",
                    "arrangementLength": "4bar",
                }],
            }}), {
                "trackIndex": 0,
                "type": "midi",
                "arrangementClips": [{
                    "id": "clip-a",
                    "name": "Lead §1",
                    "arrangementStart": "5|1",
                    "arrangementLength": "4bar",
                }],
            }, [])

        def mock_clip(name, args):
            self.assertEqual(args.get("id"), "clip-a")
            return PPResponse(json.dumps({"result": {
                "name": "Lead §1",
                "notes": [
                    {"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 80},
                    {"pitch": 64, "start": 1.0, "duration": 1.0, "velocity": 80},
                ],
            }}), {
                "name": "Lead §1",
                "notes": [
                    {"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 80},
                    {"pitch": 64, "start": 1.0, "duration": 1.0, "velocity": 80},
                ],
            }, [])

        session.responses["ppal-read-track"] = mock_track
        session.responses["ppal-read-clip"] = mock_clip
        return session

    def test_stopped_arrangement_derives_bar_from_playhead(self):
        m = moment.get_moment(
            where="arrangement", session=self.stopped_arrangement_session())
        self.assertEqual(m["ears"], "live")
        self.assertEqual(m["context"]["bar"], 5)
        self.assertEqual(m["context"]["bar_source"], "transport")
        self.assertTrue(m["roles"], "playhead bar clips must be heard")
        self.assertTrue(any("transport is stopped" in a
                            for a in m["assumptions"]))
        line = ears.status_line(m)
        self.assertIn("heard 1/1 tracks", line)
        self.assertIn("transport stopped", line)
        self.assertNotIn("empty room", line)

    def test_stopped_session_keeps_empty_room_with_coverage(self):
        m = moment.get_moment(
            where="session", session=self.stopped_session())
        self.assertEqual(m["roles"], {})
        self.assertTrue(any("transport is stopped" in a
                            for a in m["assumptions"]))
        line = ears.status_line(m)
        self.assertIn("empty room", line)
        self.assertIn("heard 0/1 tracks", line)
        self.assertIn("transport stopped", line)

    def test_stopped_arrangement_without_playhead_stays_empty(self):
        session = self.stopped_session(song_time=None)
        m = moment.get_moment(where="arrangement", session=session)
        self.assertEqual(m["roles"], {})
        self.assertTrue(any("transport is stopped" in a
                            for a in m["assumptions"]))


if __name__ == "__main__":
    unittest.main()
