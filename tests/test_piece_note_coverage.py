"""Note coverage: Producer Pal reads MIDI notes only in [-L, 2L) (batch8 1.3 review).

The truth is the Live API capture in fixtures/pp/2.2.0-rc1/piece-notes (every
arrangement MIDI clip of NOCTURNEv2: loop/marker properties and all notes),
replayed by PieceDevice(live_api=True). tests.piece_notes_truth is an
independent model of what each clip plays, built from that capture alone.
No Live, no network, no store.
"""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

from mcp_server import clip_playback as cp
from mcp_server import moment
from mcp_server import piece_snapshot as ps
from mcp_server import pp_session as pp
from tests.piece_notes_truth import arrangement_events, audible_set, load_truth
from tests.pp_fakes import V23, PieceDevice, on_both_versions


def setUpModule():
    pp.set_live_process_incarnation_resolver(
        lambda: {"pid": 4242, "start_time": "Sat Sep 26 10:00:00 2026"})


def tearDownModule():
    pp.set_live_process_incarnation_resolver(None)


def snapshot(device, **kwargs):
    with patch.object(pp, "operation_session", device.operation()):
        return asyncio.run(moment.read_piece_snapshot(**kwargs))


def hear(device, bar):
    with patch.object(pp, "operation_session", device.operation()):
        return pp.run_coro_blocking(moment.read_moment_live(
            where="arrangement", bar=bar, max_tracks=32, session=device.pp_session()))


def track(snap, name, kind=None):
    found = [t for t in snap["tracks"] if t["name"] == name and (kind is None or t["type"] == kind)]
    assert len(found) == 1, (name, found)
    return found[0]


def clips_by_id(snap):
    return {c["id"]: (t, c) for t in snap["tracks"] for c in t["arrangement_clips"]}


def _q(value):
    return round(value, 4)


# --- the pure model --------------------------------------------------------------

def live_frame(**props):
    base = {"looping": 1, "loop_start": 0, "loop_end": 16, "start_marker": 0,
            "end_marker": 16, "start_time": 0, "end_time": 32,
            "signature_numerator": 4, "signature_denominator": 4, "muted": 0}
    base.update(props)
    return cp.frame_from_live(base)


@on_both_versions
class PlaybackModelTests(unittest.TestCase):
    def test_coverage_is_the_played_region_against_the_read_window(self):
        pp_clip = {"start": "1|1", "end": "5|1", "length": "4bar", "looping": True,
                   "timeSignature": "4/4"}
        self.assertEqual(cp.coverage(cp.frame_from_pp(pp_clip)), "full")
        self.assertEqual(cp.coverage(cp.frame_from_pp({**pp_clip, "start": "7|1",
                                                       "end": "11|1"})), "partial")
        # Bassline at 33|1: loop 88-104 in a 16-beat clip -> nothing of it is read
        self.assertEqual(cp.coverage(cp.frame_from_pp({**pp_clip, "start": "23|1",
                                                       "end": "27|1"})), "unseen")
        self.assertEqual(cp.coverage(live_frame(loop_start=88, loop_end=104,
                                                start_marker=88)), "full")

    def test_start_marker_sets_the_first_pass_offset(self):
        frame = live_frame(start_marker=8)      # loop 0-16, first pass from 8
        notes = [cp.note_from_live({"pitch": 60, "start_time": 0, "duration": 1,
                                    "velocity": 90}),
                 cp.note_from_live({"pitch": 62, "start_time": 8, "duration": 1,
                                    "velocity": 90})]
        self.assertEqual([(e["start"], e["pitch"]) for e in cp.events(frame, notes)],
                         [(0.0, 62), (8.0, 60), (16.0, 62), (24.0, 60)])

    def test_start_marker_before_the_loop_plays_its_lead_in_once(self):
        frame = live_frame(loop_start=4, loop_end=8, start_marker=0, end_time=12)
        notes = [cp.note_from_live({"pitch": p, "start_time": t, "duration": 0.5,
                                    "velocity": 90}) for p, t in ((50, 0), (60, 4))]
        self.assertEqual([(e["start"], e["pitch"]) for e in cp.events(frame, notes)],
                         [(0.0, 50), (4.0, 60), (8.0, 60)])
        self.assertEqual(len(cp.split(frame, notes, quarters=True)["audible"]), 2)

    def test_muted_notes_never_sound(self):
        frame = live_frame()
        notes = [cp.note_from_live({"pitch": 60, "start_time": 0, "duration": 1,
                                    "velocity": 90, "mute": 1}),
                 cp.note_from_live({"pitch": 64, "start_time": 1, "duration": 1,
                                    "velocity": 90, "mute": 0})]
        split = cp.split(frame, notes, quarters=True)
        self.assertEqual([n["pitch"] for n in split["audible"]], [64])
        self.assertEqual([n["pitch"] for n in split["muted"]], [60])
        self.assertEqual({e["pitch"] for e in cp.events(frame, notes)}, {64})

    def test_a_muted_clip_plays_no_events(self):
        notes = [cp.note_from_live({"pitch": 60, "start_time": 0, "duration": 1,
                                    "velocity": 90})]
        self.assertEqual(len(cp.events(live_frame(), notes)), 2)
        self.assertEqual(cp.events(live_frame(muted=1), notes), [])

    def test_durations_stop_at_the_loop_wrap(self):
        # a 3-beat note at 14 in a 0-16 loop sounds 2 beats, then the loop restarts
        frame = live_frame(end_time=32)
        notes = [cp.note_from_live({"pitch": 60, "start_time": 14, "duration": 3,
                                    "velocity": 90})]
        self.assertEqual([(e["start"], e["duration"]) for e in cp.events(frame, notes)],
                         [(14.0, 2.0), (30.0, 2.0)])
        # a clip that does not loop stops at its end marker
        once = live_frame(looping=0, end_marker=15, end_time=32)
        self.assertEqual([(e["start"], e["duration"]) for e in cp.events(once, notes)],
                         [(14.0, 1.0)])

    def test_producer_pal_first_start_is_the_start_marker(self):
        clip = {"start": "1|1", "end": "5|1", "length": "4bar", "looping": True,
                "timeSignature": "4/4", "arrangementStart": "1|1",
                "arrangementLength": "4bar"}
        assumed = cp.frame_from_pp(clip)
        self.assertEqual((assumed["start_marker"], assumed["start_marker_known"]),
                         (0.0, False))
        rotated = cp.frame_from_pp({**clip, "firstStart": "3|1"})
        self.assertEqual((rotated["start_marker"], rotated["start_marker_known"]),
                         (8.0, True))
        notes = [{"pitch": 60, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 90},
                 {"pitch": 62, "start_beats": 8.0, "duration_beats": 1.0, "velocity": 90}]
        self.assertEqual([(e["start"], e["pitch"]) for e in
                          cp.events(rotated, notes, quarters=False)][:2],
                         [(0.0, 62), (8.0, 60)])
        # firstStart is in the clip's meter: 1|4 of 6/8 is 3 eighths
        eighths = cp.frame_from_pp({**clip, "timeSignature": "6/8", "end": "3|1",
                                    "firstStart": "1|4"})
        self.assertEqual(eighths["start_marker"], 1.5)
        self.assertTrue(pp.split_audible_notes({**clip, "firstStart": "3|1"}, notes)
                        ["start_marker_known"])
        self.assertFalse(pp.split_audible_notes(clip, notes)["start_marker_known"])

    def test_eighth_note_clip_meter_compares_like_with_like(self):
        # 6/8 clip, brace 1|4 .. 2|1 = quarters 1.5 .. 3.0. Producer Pal's note t is
        # in eighths: t=3 is quarter 1.5 (plays), t=1 is quarter 0.5 (before the brace).
        clip = {"start": "1|4", "end": "2|1", "length": "n3/8", "looping": True,
                "timeSignature": "6/8"}
        notes = [{"pitch": 60, "start_beats": 3.0, "duration_beats": 1.0, "velocity": 90},
                 {"pitch": 62, "start_beats": 1.0, "duration_beats": 1.0, "velocity": 90}]
        split = pp.split_audible_notes(clip, notes)
        self.assertEqual([n["pitch"] for n in split["audible"]], [60])
        self.assertEqual([n["pitch"] for n in split["hidden"]], [62])
        self.assertEqual((split["loop_start_beats"], split["loop_end_beats"]), (1.5, 3.0))

    def test_split_audible_notes_never_calls_unseen_notes_hidden(self):
        clip = {"start": "23|1", "end": "27|1", "length": "4bar", "looping": True,
                "timeSignature": "4/4"}
        notes = [{"pitch": 39, "start_beats": 19.0, "duration_beats": 1.0, "velocity": 21}]
        split = pp.split_audible_notes(clip, notes)
        self.assertTrue(split["unread"])
        self.assertEqual(split["coverage"], "unseen")
        self.assertEqual((split["audible"], split["hidden"], split["n_hidden"]), ([], [], 0))
        self.assertIn("[-L, 2L)", split["unread_reason"])
        # a Live API read of the same clip is complete
        live = {"frame": cp.frame_from_live({"looping": 1, "loop_start": 88, "loop_end": 104,
                                             "start_marker": 88, "end_marker": 108,
                                             "start_time": 128, "end_time": 144}),
                "notes": [cp.note_from_live({"pitch": 55, "start_time": 88, "duration": 8,
                                             "velocity": 90})]}
        split = pp.split_audible_notes(clip, notes, live=live)
        self.assertFalse(split["unread"])
        self.assertEqual([n["pitch"] for n in split["audible"]], [55])


# --- the snapshot with the Live API: every note read --------------------------------

@on_both_versions
class LiveApiNotesSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.truth = load_truth()
        cls.device = PieceDevice(live_api=True)
        cls.snap = snapshot(cls.device)
        cls.clips = clips_by_id(cls.snap)

    def test_every_arrangement_midi_clip_is_read_through_the_live_api(self):
        paths = [args["path"] for name, args in self.device.calls
                 if name == "ppal-live-api" and args["path"].startswith("id ")]
        self.assertEqual(len(paths), 126)
        self.assertEqual(set(paths), {"id " + cid for cid in self.truth})
        self.assertEqual(self.device.live_refusals, [])
        # one request per clip: 12 properties + the notes call, as captured
        request = next(args for name, args in self.device.calls
                       if name == "ppal-live-api" and args["path"] == "id 1016")
        self.assertEqual(request["operations"], self.device.clip_live["1016"]["ops"])

    def test_bassline_plays_from_bar_33(self):
        _, clip = self.clips["1016"]
        self.assertEqual((clip["notes_source"], clip["notes_status"]), ("live_api", "read"))
        self.assertEqual(clip["coverage"], "unseen")     # what Producer Pal alone would see
        self.assertEqual((clip["notes_audible"], clip["notes_hidden"]), (2, 17))
        events = ps.clip_events(clip)
        self.assertEqual([(ps.bar_beat(e["start"], 4, 4), e["pitch"]) for e in events],
                         [("33|4.25", 55), ("35|1", 41)])

    def test_the_low_drone_plays_from_bar_1(self):
        heart = track(self.snap, "Low Drone")["arrangement_clips"][0]
        events = ps.clip_events(heart)
        self.assertTrue(events)
        self.assertEqual(ps.bar_beat(events[0]["start"], 4, 4).split("|")[0], "1")

    def test_all_126_clips_agree_with_the_independent_model(self):
        agree_set = agree_events = 0
        disagree = []
        for cid, truth in self.truth.items():
            _, clip = self.clips[cid]
            mine = sorted((_q(n["start_beats"]), n["pitch"]) for n in clip["audible_notes"])
            events = sorted({(_q(e["start"]), e["pitch"]) for e in ps.clip_events(clip)})
            agree_set += mine == audible_set(truth)
            agree_events += events == arrangement_events(truth)
            if mine != audible_set(truth) or events != arrangement_events(truth):
                disagree.append(cid)
        self.assertEqual((agree_set, agree_events), (126, 126), disagree)

    def test_source_integrity_covers_every_track_and_sees_unseen_notes(self):
        integrity = self.snap["source_integrity"]
        self.assertEqual(integrity["blind"], [])
        self.assertEqual(len(integrity["digests"]), 32)
        device = PieceDevice(live_api=True)
        bassline_id = track(self.snap, "20-Bassline")["id"]
        device.clip_live["1016"]["notes"][-2]["pitch"] = 54   # inside the unseen loop
        edited = snapshot(device)["source_integrity"]["digests"]
        self.assertNotEqual(edited[bassline_id], integrity["digests"][bassline_id])

    def test_source_integrity_sees_probability_and_velocity_detail(self):
        bassline_id = track(self.snap, "20-Bassline")["id"]
        base = self.snap["source_integrity"]["digests"][bassline_id]
        for key, value in (("probability", 0.5), ("velocity_deviation", 12.0),
                           ("release_velocity", 90.0)):
            device = PieceDevice(live_api=True)
            device.clip_live["1016"]["notes"][0][key] = value
            edited = snapshot(device)["source_integrity"]["digests"][bassline_id]
            self.assertNotEqual(edited, base, key)

    def test_a_failed_live_read_of_a_fully_seen_clip_says_it_fell_back(self):
        device = PieceDevice(live_api=True)
        full_id = next(cid for cid, (t, c) in self.clips.items()
                       if c["kind"] == "midi" and c["coverage"] == "full")
        del device.clip_live[full_id]          # the Live API read of it errors
        snap = snapshot(device)
        _, clip = clips_by_id(snap)[full_id]
        self.assertEqual((clip["notes_status"], clip["notes_source"]),
                         ("read", "producer_pal_window"))
        self.assertEqual(snap["notes_read"]["producer_pal_window_clips"], 1)
        self.assertTrue(any("producer_pal_window" in item or "fell back" in item
                            for item in snap["uncertain"]))

    def test_a_live_read_that_disagrees_with_producer_pal_is_a_changed_set(self):
        device = PieceDevice(live_api=True)
        original = device.clip_live["1016"]["values"]["start_time"]

        def moved(d):
            d.clip_live["1016"]["values"]["start_time"] = original + 4
        for number in range(1, 400):
            device.before_call[number] = moved
        with self.assertRaisesRegex(ps.SetChangedWhileReading, "1016"):
            snapshot(device)


# --- without the Live API: unread, and a typed blind list ---------------------------

@on_both_versions
class NoLiveApiCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.truth = load_truth()
        cls.snap = snapshot(PieceDevice())
        cls.clips = clips_by_id(cls.snap)

    def test_36_clips_are_not_fully_read_and_are_unread(self):
        coverage = {cid: self.clips[cid][1]["coverage"] for cid in self.truth}
        counts = {kind: list(coverage.values()).count(kind)
                  for kind in ("full", "partial", "unseen")}
        self.assertEqual(counts, {"full": 90, "partial": 22, "unseen": 14})
        for cid, kind in coverage.items():
            clip = self.clips[cid][1]
            if kind == "full":
                self.assertEqual(clip["notes_status"], "read")
                self.assertEqual(sorted((_q(n["start_beats"]), n["pitch"])
                                        for n in clip["audible_notes"]),
                                 audible_set(self.truth[cid]), cid)
            else:
                self.assertEqual(clip["notes_status"], "unread", cid)
                self.assertIsNone(clip["notes_audible"])
                self.assertIsNone(clip["notes_hidden"])
                self.assertIsNone(clip["audible_notes"])
                self.assertEqual(ps.clip_events(clip), [])

    def test_uncertain_names_the_read_window_not_a_note_frame(self):
        text = " ".join(self.snap["uncertain"])
        self.assertIn("[-L, 2L)", text)
        self.assertIn("36 placed MIDI clips", text)
        self.assertNotIn("note frame", text)

    def test_source_integrity_blind_list_refuses_to_vouch(self):
        integrity = self.snap["source_integrity"]
        blind = {item["track"]: item for item in integrity["blind"]}
        self.assertIn("20-Bassline", blind)
        self.assertIn("Low Drone", blind)
        self.assertEqual(blind["20-Bassline"]["reason"], "notes_unread")
        self.assertIn("1016", [c["clip_id"] for c in blind["20-Bassline"]["clips"]])
        for item in integrity["blind"]:
            self.assertNotIn(item["track_id"], integrity["digests"])
        self.assertEqual(sum(len(item["clips"]) for item in integrity["blind"]), 36)


# --- hear_the_room on the same Set ---------------------------------------------------

@on_both_versions
class HearTheRoomCoverageTests(unittest.TestCase):
    def test_bar_33_without_the_live_api_calls_bassline_unread_not_hidden(self):
        m = hear(PieceDevice(narrow_reads=True), 33)
        self.assertEqual(m["ears"], "live", m.get("reason"))
        self.assertNotIn("20-Bassline", [c["track"] for c in m["hidden"]])
        unread = {c["track"]: c for c in m["unread"]}
        self.assertIn("20-Bassline", unread)
        self.assertIn("[-L, 2L)", unread["20-Bassline"]["reason"])
        self.assertNotIn("20-Bassline", m["heard_tracks"])
        self.assertTrue(any("not read" in a and "20-Bassline" in a for a in m["assumptions"]))

    def test_bar_33_with_the_live_api_hears_bassline(self):
        m = hear(PieceDevice(live_api=True, narrow_reads=True), 33)
        self.assertEqual(m["ears"], "live", m.get("reason"))
        self.assertIn("20-Bassline", m["heard_tracks"])
        self.assertEqual(m["unread"], [])
        card = next(c for c in m["hidden"] if c["track"] == "20-Bassline")
        self.assertEqual(card["n_hidden"], 17)       # really behind the brace: seen

    def test_bar_33_default_hears_every_regular_track(self):
        """round-2 review: the 12-track default hid Bassline (track 19)."""
        device = PieceDevice(live_api=True, narrow_reads=True)
        with patch.object(pp, "operation_session", device.operation()):
            m = pp.run_coro_blocking(moment.read_moment_live(
                where="arrangement", bar=33, session=device.pp_session()))
        for name in ("20-Bassline", "Synth A", "Grain Pad"):
            self.assertIn(name, m["heard_tracks"])
        self.assertEqual(m["context"]["tracks_scanned"], 32)
        self.assertFalse(any("read the first" in a for a in m["assumptions"]))
        device = PieceDevice(live_api=True, narrow_reads=True)
        with patch.object(pp, "operation_session", device.operation()):
            capped = pp.run_coro_blocking(moment.read_moment_live(
                where="arrangement", bar=33, max_tracks=12, session=device.pp_session()))
        self.assertEqual(capped["context"]["tracks_scanned"], 12)
        self.assertNotIn("20-Bassline", capped["heard_tracks"])
        self.assertTrue(any("read the first 12" in a for a in capped["assumptions"]))

    def test_bar_33_bass_stays_busy_with_the_live_api(self):
        """round-2 review: Live-API notes keep clip-content time (Bassline plays
        88-104), so measuring density from beat 0 diluted the bass to sparse."""
        for live_api in (False, True):
            device = PieceDevice(live_api=live_api, narrow_reads=True)
            with patch.object(pp, "operation_session", device.operation()):
                m = pp.run_coro_blocking(moment.read_moment_live(
                    where="arrangement", bar=33, session=device.pp_session()))
            self.assertEqual(m["roles"]["bass"]["density"], "busy", live_api)

    @staticmethod
    def _heard_density(clip, notes):
        heard, hidden, unread = [], [], []
        moment._file_split(pp.split_audible_notes(clip, notes), track="Lead", track_index=0,
                           slot=0, clip_name="", heard=heard, hidden=hidden, unread=unread)
        return moment.build_moment(heard, 120)["roles"]

    def test_density_is_measured_over_the_played_region(self):
        # loop 16-32 (inside [-16, 32)): 24 notes in 16 beats = 1.5 per beat
        clip = {"start": "5|1", "end": "9|1", "length": "4bar", "looping": True,
                "timeSignature": "4/4"}
        notes = [{"pitch": 60, "start_beats": 16 + i * 0.5, "duration_beats": 0.25,
                  "velocity": 90} for i in range(24)]
        self.assertEqual(self._heard_density(clip, notes)["lead"]["density"], "mid")

    def test_density_counts_quarter_beats_in_an_eighth_note_meter(self):
        # 6/8, two bars = 6 quarters; Producer Pal's t is in eighths. 24 notes
        # in 6 quarters = 4 per beat (busy), not 24 per 12 eighths (mid).
        clip = {"start": "1|1", "end": "3|1", "length": "2bar", "looping": True,
                "timeSignature": "6/8"}
        notes = [{"pitch": 60 + k, "start_beats": float(i), "duration_beats": 1.0,
                  "velocity": 90} for i in range(12) for k in (0, 7)]
        self.assertEqual(self._heard_density(clip, notes)["lead"]["density"], "busy")

    def test_bar_1_hears_the_low_drone_only_with_the_live_api(self):
        off = hear(PieceDevice(narrow_reads=True), 1)
        self.assertIn("Low Drone", [c["track"] for c in off["unread"]])
        self.assertNotIn("Low Drone", [c["track"] for c in off["hidden"]])
        on = hear(PieceDevice(live_api=True, narrow_reads=True), 1)
        self.assertIn("Low Drone", on["heard_tracks"])


# --- coverage before presence: an empty-looking MIDI read is not an empty clip ------

FAR_CLIP = {"id": "c9", "type": "midi", "name": "Far", "start": "23|1", "end": "27|1",
            "length": "4bar", "looping": True, "timeSignature": "4/4",
            "arrangementStart": "33|1", "arrangementLength": "4bar"}
FAR_PROPS = {"name": "Far", "loop_start": 88, "loop_end": 104, "start_marker": 88,
             "end_marker": 104, "looping": 1, "start_time": 128, "end_time": 144,
             "signature_numerator": 4, "signature_denominator": 4, "is_midi_clip": 1,
             "muted": 0}


def _json(result):
    return pp.PPResponse(json.dumps({"result": result}), result, [])


def far_room(where, live_api):
    """A one-track Set whose only clip plays 88-104 of a 16-beat clip: Producer
    Pal's [-L, 2L) window returns it with no notes at all."""
    from tests.test_moment import FakePPSession

    def call(name, args):
        if name == "ppal-read-live-set":
            return _json({"tempo": 120.0, "timeSignature": "4/4", "isPlaying": True,
                          "regularTrackCount": 1,
                          "tracks": [{"trackIndex": 0, "name": "Keys", "type": "midi"}]})
        if name == "ppal-read-track":
            if where == "arrangement":
                return _json({"trackIndex": 0, "type": "midi",
                              "arrangementClips": [dict(FAR_CLIP)]})
            return _json({"trackIndex": 0, "playingSlotIndex": 0})
        if name == "ppal-read-clip":
            return _json(dict(FAR_CLIP))
        if name == "ppal-live-api":
            path = args["path"]
            if path == "id c9":
                results = [{"operation": op, "result": (
                    FAR_PROPS[op["property"]] if op["type"] == "getProperty" else json.dumps(
                        {"notes": [{"pitch": 55, "start_time": 88, "duration": 4,
                                    "velocity": 90, "mute": 0}]}))}
                    for op in args["operations"]]
            else:
                values = {"is_playing": True, "current_song_time": 128.0, "mute": False,
                          "back_to_arranger": True,
                          "playing_slot_index": 0 if where == "session" else -1}
                results = [{"operation": op, "result": values[op["property"]]}
                           for op in args["operations"]]
            return _json({"id": "1", "path": path, "results": results})
        raise AssertionError(name)

    session = FakePPSession(capabilities={"live_api": live_api})
    session.responses = {name: call for name in (
        "ppal-read-live-set", "ppal-read-track", "ppal-read-clip", "ppal-live-api")}
    return pp.run_coro_blocking(moment.read_moment_live(
        where=where, bar=33 if where == "arrangement" else None, session=session))


@on_both_versions
class CoverageBeforePresenceTests(unittest.TestCase):
    """round-2 review: a MIDI clip whose coverage is not full and that comes
    back with notes [] was classified "empty" and silently dropped."""

    def test_classifier_calls_a_window_miss_unread_not_empty(self):
        self.assertEqual(pp.classify_session_clip(_json(dict(FAR_CLIP)), []), "unread")
        near = dict(FAR_CLIP, start="1|1", end="5|1")
        self.assertEqual(pp.classify_session_clip(_json(near), []), "empty")

    def test_classifier_on_the_captured_read_clip_shape(self):
        from tests.pp_fakes import load_fixture, response_text
        text = response_text(load_fixture("legacy/legacy_read_clip_path.json")["response"])
        self.assertEqual(pp.classify_session_clip(pp.parse_tool_response(text), []), "empty")
        record = dict(json.loads(text), start="23|1", end="27|1", length="4bar",
                      looping=True)                 # + the timing include's fields
        self.assertEqual(pp.classify_session_clip(
            pp.parse_tool_response(json.dumps(record)), []), "unread")

    def test_without_the_live_api_the_clip_is_unread(self):
        for where in ("session", "arrangement"):
            m = far_room(where, live_api=False)
            self.assertEqual(m["ears"], "live", m.get("reason"))
            self.assertEqual([c["track"] for c in m["unread"]], ["Keys"], where)
            self.assertEqual(m["roles"], {}, where)
            self.assertTrue(any("not read" in a for a in m["assumptions"]), where)

    def test_with_the_live_api_the_clip_is_read_whole(self):
        for where in ("session", "arrangement"):
            m = far_room(where, live_api=True)
            self.assertEqual(m["unread"], [], where)
            self.assertIn("Keys", m["heard_tracks"], where)


# --- the other Producer Pal note readers ---------------------------------------------

BASSLINE_PP = {"id": "1016", "arrangementStart": "33|1", "arrangementLength": "4bar",
              "start": "23|1", "end": "27|1", "length": "4bar", "looping": True,
              "timeSignature": "4/4",
              "notes": "[{p:40,t:6.5,d:0.5,v:90},{p:45,t:2.25,d:0.25,v:70}]"}


def bassline_live():
    truth = load_truth()["1016"]
    return {"clip_id": "1016", "props": truth["props"],
            "frame": cp.frame_from_live(truth["props"]),
            "notes": [cp.note_from_live(n) for n in truth["notes"]]}


@on_both_versions
class OtherReaderCoverageTests(unittest.TestCase):
    def live(self, clips, meter="4/4"):
        return {"set_name": "NOCTURNEv2", "tempo": 90, "meter": meter,
                "beats_per_bar": 4.0 if meter == "4/4" else 3.0,
                "tracks": [{"track_index": 19, "name": "20-Bassline", "type": "midi",
                            "arrangement_clips": clips}]}

    def test_finisher_never_counts_unseen_notes_as_hidden(self):
        from mcp_server import finisher
        project = finisher.project_from_live_tracks(self.live([dict(BASSLINE_PP)]))
        self.assertEqual(project["skip_detail"]["hidden_past_loop"], 0)
        self.assertEqual(project["unread_clips"], 1)
        self.assertTrue(project["clips"][0]["unread"])
        self.assertEqual(project["clips"][0]["note_count"], 0)
        self.assertTrue(any("[-L, 2L)" in a for a in project["assumptions"]))

    def test_finisher_uses_the_live_api_read_when_there(self):
        from mcp_server import finisher
        project = finisher.project_from_live_tracks(
            self.live([dict(BASSLINE_PP, live=bassline_live())]))
        clip = project["clips"][0]
        self.assertEqual(sorted(n["pitch"] for n in clip["notes"]), [41, 55])
        self.assertEqual(project["skip_detail"]["hidden_past_loop"], 17)
        self.assertEqual(project["unread_clips"], 0)
        failed = finisher.project_from_live_tracks(
            self.live([dict(BASSLINE_PP, live={"clip_id": "1016", "error": "boom"})]))
        self.assertTrue(failed["clips"][0]["unread"])
        self.assertEqual(failed["skip_detail"]["hidden_past_loop"], 0)

    def test_finisher_playback_prefers_the_live_api_frame(self):
        from mcp_server import finisher
        live = bassline_live()
        live["frame"] = dict(live["frame"], start_marker=92.0, muted=True)
        # Producer Pal's own view (no firstStart, clip active) must not win.
        clip = finisher.project_from_live_tracks(
            self.live([dict(BASSLINE_PP, live=live)]))["clips"][0]
        self.assertEqual(clip["start_marker"], 92.0)
        self.assertTrue(clip["disabled"])
        self.assertEqual(clip["loop_start"], 88.0)

    def test_finisher_first_start_is_read_in_the_clip_meter(self):
        from mcp_server import finisher
        clip = {"arrangementStart": "1|1", "arrangementLength": "2bar", "start": "1|1",
                "end": "3|1", "firstStart": "1|4", "length": "2bar", "looping": True,
                "timeSignature": "6/8", "notes": "[{p:60,t:0,d:1,v:90}]"}
        rec = finisher.project_from_live_tracks(self.live([clip], meter="6/8"))["clips"][0]
        self.assertEqual(rec["start_marker"], 1.5)     # 3 eighths, not 3 quarters

    def test_finisher_places_bar_beat_in_the_song_meter(self):
        from mcp_server import finisher
        clip = {"arrangementStart": "2|4", "arrangementLength": "1bar", "start": "1|1",
                "end": "2|1", "length": "1bar", "looping": True, "timeSignature": "6/8",
                "notes": "[{p:60,t:0,d:1,v:90}]"}
        project = finisher.project_from_live_tracks(self.live([clip], meter="6/8"))
        self.assertEqual(project["clips"][0]["start"], 4.5)     # bar 2 (3.0) + 3 eighths

    def test_clip_read_result_is_unread_outside_the_window(self):
        from mcp_server import live_write
        notes = pp.extract_notes(pp.PPResponse(json.dumps({"result": BASSLINE_PP}),
                                               BASSLINE_PP, []))
        out = live_write._clip_read_result({"source": "arrangement"}, dict(BASSLINE_PP),
                                           notes, "")
        self.assertTrue(out["unread"])
        self.assertEqual((out["notes"], out["hidden_notes"], out["n_hidden"]), ([], [], 0))
        self.assertIn("[-L, 2L)", out["unread_reason"])
        read = live_write._clip_read_result({"source": "arrangement"}, dict(BASSLINE_PP),
                                            notes, "", live=bassline_live())
        self.assertFalse(read["unread"])
        self.assertEqual(sorted(n["pitch"] for n in read["notes"]), [41, 55])
        self.assertEqual(read["n_hidden"], 17)

    def test_select_arrangement_clips_reads_the_song_meters_beat(self):
        # 6/8: 1|3 is quarter 1.0 (not 2.0); the clip covers 1.0-2.5, so it does not
        # reach bar 2's downbeat at 3.0. Reading the beat as a quarter put it at 2.0-3.5.
        clips = [{"id": "a", "arrangementStart": "1|3", "arrangementLength": "n3/8"}]
        self.assertEqual(moment.select_arrangement_clips(clips, 2, 3.0, meter="6/8"), [])
        clips = [{"id": "b", "arrangementStart": "1|5", "arrangementLength": "n3/8"}]
        self.assertEqual(moment.select_arrangement_clips(clips, 2, 3.0, meter="6/8"), clips)


@on_both_versions
class FinisherUnreadClipTests(unittest.TestCase):
    """round-2 review: read_structure skipped note_count == 0, so an unread clip
    (notes unknown) looked like silence — a false silent_gap [94, 100]."""

    def diagnose(self, live_api):
        import tempfile
        from mcp_server import finisher, live_write, piece_context
        device = PieceDevice(live_api=live_api, narrow_reads=True)
        with patch.object(pp, "operation_session", device.operation()):
            tracks = asyncio.run(live_write.read_arrangement_tracks())
        project = finisher.project_from_live_tracks(tracks)
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(piece_context, "_decision_path", return_value=tmp + "/d.json"):
            return finisher.whats_missing(project_dict=project)

    def test_unread_clips_occupy_their_bars(self):
        off, on = self.diagnose(False), self.diagnose(True)
        gaps = lambda rep: [f["bars"] for f in rep["findings"] if f["detector"] == "silent_gap"]
        self.assertEqual(gaps(on), [[85, 92]])
        self.assertEqual(gaps(off), [[85, 92]])            # no false [94, 100]
        self.assertEqual(on["suppressed"], [])

    def test_note_based_findings_over_unread_bars_are_withheld_and_said(self):
        off = self.diagnose(False)
        self.assertTrue(off["suppressed"])
        for item in off["suppressed"]:
            self.assertIn("unread", item["reason"])
        self.assertTrue(any("withheld" in a for a in off["assumptions"]))

    def test_synthetic_unread_middle_is_not_a_hole(self):
        from mcp_server import finisher

        def clip(start_bar, end_bar, unread=False):
            notes = [] if unread else [
                {"pitch": 48 + (i % 5), "start": i * 0.5, "duration": 0.25,
                 "velocity": 80 + i % 7, "enabled": True} for i in range(16)]
            rec = {"name": "", "track": "Keys", "start": (start_bar - 1) * 4.0,
                   "end": (end_bar - 1) * 4.0, "note_count": len(notes), "is_drum": False,
                   "clip_source": "arrangement", "notes": notes}
            if unread:
                rec.update(unread=True, unread_reason="outside [-L, 2L)")
            return rec
        project = {"path": "live:x", "tempo": 120, "track_count": 1,
                   "midi_track_names": ["Keys"], "audio_track_names": [],
                   "clip_count": 3, "total_notes": 32, "clips": [
                       clip(1, 9), clip(9, 17, unread=True), clip(17, 25)]}
        reading = finisher.read_structure(project)
        self.assertEqual(reading["unread_bars"], list(range(9, 17)))
        self.assertTrue(all(b["tracks"] for b in reading["bars"]))
        self.assertTrue(reading["bars"][8]["density_unknown"])
        findings = finisher.run_detectors(reading)
        self.assertNotIn("silent_gap", [f["detector"] for f in findings])
        for f in findings:
            bars = f.get("bars")
            if f["detector"] in finisher.NOTE_DETECTORS and bars:
                self.assertTrue(bars[1] < 9 or bars[0] > 16, f)


@on_both_versions
class FinisherComparisonSpanTests(unittest.TestCase):
    def test_drop_without_contrast_is_withheld_when_the_section_before_is_unread(self):
        from unittest import mock
        from mcp_server import finisher
        finding = {"detector": "drop_without_contrast", "severity": "medium",
                   "bars": [61, 68], "context_bars": [59, 68], "evidence": "x"}
        with mock.patch.object(finisher, "_run_detectors", return_value=[dict(finding)]):
            reading = {"unread_bars": [59]}
            self.assertEqual(finisher.run_detectors(reading), [])
            self.assertEqual(reading["suppressed"][0]["detector"], "drop_without_contrast")
            self.assertEqual(finisher.run_detectors({"unread_bars": [40]}), [finding])


@on_both_versions
class ClipReadConsumerTests(unittest.TestCase):
    """round-2 review: a clip whose notes were not read must say why, not
    'select a MIDI clip' (it IS a MIDI clip, selected)."""

    UNREAD = {"source": "selected_clip", "read_error": None, "notes": [], "n_notes": 0,
              "unread": True, "coverage": "unseen",
              "unread_reason": "Producer Pal returns a MIDI clip's notes only inside "
                               "[-L, 2L) of clip-content time; this clip plays 88-104"}

    def test_tools_name_the_unread_reason(self):
        from unittest import mock
        from mcp_server import live_write, server
        calls = {
            "develop_my_loop": lambda: server.develop_my_loop(),
            "answer_my_loop": lambda: server.answer_my_loop(),
            "three_endings": lambda: server.three_endings(slot="0/0"),
            "create_idea_pack_from_live_clip": lambda: server.create_idea_pack_from_live_clip(
                use_current_selection=True),
        }
        with mock.patch.object(live_write, "read_current_clip",
                               mock.AsyncMock(return_value=dict(self.UNREAD))), \
                mock.patch.object(live_write, "read_clip",
                                  mock.AsyncMock(return_value=dict(self.UNREAD))):
            for name, call in calls.items():
                with self.assertRaises(ValueError, msg=name) as ctx:
                    call()
                self.assertIn("[-L, 2L)", str(ctx.exception), name)
                self.assertNotIn("select a MIDI clip", str(ctx.exception), name)


if __name__ == "__main__":
    unittest.main()
