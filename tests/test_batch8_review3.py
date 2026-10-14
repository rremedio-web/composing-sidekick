"""batch8 final review (round 3) — ports of the probes in
.scratch/batch8/review3-probes/ (sc_fail, status, six8, loopend, seeds, intent).
Fakes and temporary state only: no Live, no network, no store."""
import asyncio
import contextlib
import copy
import os
import tempfile
import unittest
from unittest.mock import patch

from mcp_server import clip_playback as cp
from mcp_server import ears
from mcp_server import finisher
from mcp_server import moment
from mcp_server import piece_snapshot as ps
from mcp_server import pp_session as pp
from tests.pp_fakes import V23, PieceDevice, on_both_versions


def setUpModule():
    pp.set_live_process_incarnation_resolver(
        lambda: {"pid": 4242, "start_time": "Sat Sep 26 10:00:00 2026"})


def tearDownModule():
    pp.set_live_process_incarnation_resolver(None)


def hear(device, bar, max_tracks):
    with patch.object(pp, "operation_session", device.operation()):
        return asyncio.run(moment.read_moment_live(max_tracks=max_tracks,
                                                   where="arrangement", bar=bar))


@on_both_versions
class StatusLineUnreadTests(unittest.TestCase):
    """status.py: an unread MIDI clip is never an 'empty room'."""

    def test_unread_only_room_is_no_midi_heard(self):
        m = hear(PieceDevice(live_api=False, narrow_reads=True), 1, 2)
        self.assertEqual([u["track"] for u in m["unread"]], ["Low Drone"])
        line = ears.status_line(m)
        self.assertNotIn("empty room", line)
        self.assertIn("no midi heard", line)
        self.assertIn("1 clip unread", line)

    def test_heard_room_also_names_the_unread_clip(self):
        m = hear(PieceDevice(live_api=False, narrow_reads=True), 1, 32)
        line = ears.status_line(m)
        self.assertIn("lead sparse", line)
        self.assertIn("1 clip unread", line)

    def test_plural_and_unheard_coexist(self):
        m = {"ears": "live", "tempo": 90, "key": "C major", "tempo_world": "downtempo",
             "roles": {}, "unheard": [{"track": "a"}],
             "unread": [{"track": "b"}, {"track": "c"}], "context": {}}
        line = ears.status_line(m)
        self.assertIn("no midi heard", line)
        self.assertIn("1 clip unheard", line)
        self.assertIn("2 clips unread", line)


def piece_snapshot(device):
    with patch.object(pp, "operation_session", device.operation()):
        return asyncio.run(moment.read_piece_snapshot())


def n_sidechain_unknown(snap):
    return sum(1 for t in snap["tracks"] for h in t["hazards"]
               if h["type"] == "sidechain_unknown")


def keeps_uncertain_line(snap):
    return any(item.startswith("sidechains unknown") for item in snap["uncertain"])


@on_both_versions
class SidechainFailClosedTests(unittest.TestCase):
    """sc_fail.py: a malformed saved-Set read must not strip the
    sidechain_unknown hazards (staging would then trust an unread Set)."""

    @classmethod
    def setUpClass(cls):
        cls.snap = piece_snapshot(PieceDevice())
        cls.before = n_sidechain_unknown(cls.snap)

    def test_the_unread_snapshot_carries_the_hazards(self):
        self.assertGreater(self.before, 0)
        self.assertTrue(keeps_uncertain_line(self.snap))

    def test_malformed_reader_output_leaves_the_snapshot_untouched(self):
        for bad in ([{"tracks": [None], "sidechains": []}],
                    [{"tracks": [], "sidechains": ["x"]}],
                    [{"tracks": [], "returns": [None], "sidechains": []}],
                    [{"tracks": [], "sidechains": [{"source_track": "Drum Map",
                                                    "target_track": ["unhashable"]}]}]):
            with self.subTest(bad=bad):
                snap = copy.deepcopy(self.snap)
                with self.assertRaises(Exception):
                    ps.sidechains_from_reader(snap, bad, file_path="/x.als")
                self.assertEqual(snap, self.snap)
                self.assertEqual(n_sidechain_unknown(snap), self.before)
                self.assertTrue(keeps_uncertain_line(snap))

    def test_an_unmatched_edge_is_read_not_unknown(self):
        snap = copy.deepcopy(self.snap)
        ps.sidechains_from_reader(snap, [{"tracks": [], "sidechains": [
            {"source_track": "Low Drone", "target_track": "Nope"}]}], file_path="/x.als")
        self.assertEqual(snap["sidechains"]["status"], "from_saved_als")
        self.assertEqual(n_sidechain_unknown(snap), 0)
        self.assertFalse(keeps_uncertain_line(snap))
        self.assertEqual([(u["source"], u["target"]) for u in snap["sidechains"]["unmatched"]],
                         [("Low Drone", "Nope")])
        dark = [t for t in snap["tracks"] if t["name"] == "Low Drone"][0]
        self.assertIn("sidechain_unmatched", [h["type"] for h in dark["hazards"]])

    def test_read_piece_snapshot_keeps_the_hazards_when_the_reader_output_is_bad(self):
        import tools.live_set_reader as reader
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Song.als")
            with open(path, "wb") as handle:
                handle.write(b"placeholder")
            device = PieceDevice(live_api=True)
            device.live_api_values["file_path"] = path
            with patch.object(reader, "read_live_set",
                              return_value=[{"tracks": [None], "sidechains": ["x"]}]):
                snap = piece_snapshot(device)
        self.assertEqual(snap["sidechains"]["status"], "unknown")
        self.assertIn("unreadable", snap["sidechains"]["reason"])
        self.assertGreater(n_sidechain_unknown(snap), 0)
        self.assertTrue(keeps_uncertain_line(snap))


SIX8 = {"id": "7", "arrangementStart": "1|1", "arrangementLength": "4bar", "start": "1|1",
        "end": "3|1", "length": "2bar", "looping": True, "timeSignature": "6/8",
        "notes": "[{p:60,t:0,d:1,v:80},{p:72,t:6,d:1,v:80}]"}
SIX8_EXPECTED = [(60, 0.0), (72, 3.0), (60, 6.0), (72, 9.0), (60, 12.0), (72, 15.0)]


def six8_live(clip):
    return {"file_path": "/x/S.als", "tempo": 120, "identity_verified": True,
            "meter": "4/4", "beats_per_bar": 4,
            "tracks": [{"name": "Lead", "type": "midi", "arrangement_clips": [clip]}]}


def seeds(project, bars):
    return [(n["pitch"], n["start"]) for n in finisher._ending_seed_notes(project, bars)]


@on_both_versions
class LiveNotesInQuarterBeatsTests(unittest.TestCase):
    """six8.py: project='live' notes are quarter beats (like parse_als), so a
    6/8 clip seeds three_endings with what it plays."""

    def test_producer_pal_notes_of_a_6_8_clip_are_scaled_to_quarters(self):
        project = finisher.project_from_live_tracks(six8_live(dict(SIX8)))
        clip = project["clips"][0]
        self.assertEqual([(n["pitch"], n["start"], n["duration"]) for n in clip["notes"]],
                         [(60, 0.0, 0.5), (72, 3.0, 0.5)])
        self.assertEqual(clip["playback_brace"]["loop_end"], 6.0)
        self.assertEqual(seeds(project, [1, 4]), SIX8_EXPECTED)

    def test_live_api_notes_of_a_6_8_clip_stay_in_quarters(self):
        props = {"looping": 1, "loop_start": 0, "loop_end": 6, "start_marker": 0,
                 "end_marker": 6, "start_time": 0, "end_time": 16,
                 "signature_numerator": 6, "signature_denominator": 8, "muted": 0}
        live = {"clip_id": "7", "props": props, "frame": cp.frame_from_live(props),
                "notes": [cp.note_from_live({"pitch": 60, "start_time": 0, "duration": 0.5,
                                             "velocity": 80}),
                          cp.note_from_live({"pitch": 72, "start_time": 3, "duration": 0.5,
                                             "velocity": 80})]}
        project = finisher.project_from_live_tracks(six8_live(dict(SIX8, live=live)))
        clip = project["clips"][0]
        self.assertEqual([(n["pitch"], n["start"], n["duration"]) for n in clip["notes"]],
                         [(60, 0.0, 0.5), (72, 3.0, 0.5)])
        self.assertEqual(seeds(project, [1, 4]), SIX8_EXPECTED)

    def test_4_4_notes_are_unchanged(self):
        clip = dict(SIX8, timeSignature="4/4", end="5|1", length="4bar",
                    arrangementLength="8bar",
                    notes="[{p:60,t:0,d:1,v:80},{p:72,t:6,d:1,v:80}]")
        notes = finisher.project_from_live_tracks(six8_live(clip))["clips"][0]["notes"]
        self.assertEqual([(n["pitch"], n["start"], n["duration"]) for n in notes],
                         [(60, 0.0, 1.0), (72, 6.0, 1.0)])


def read_arrangement(live_api):
    from mcp_server import live_write
    device = PieceDevice(live_api=live_api, narrow_reads=True)
    with patch.object(pp, "operation_session", device.operation()):
        return asyncio.run(live_write.read_arrangement_tracks())


@on_both_versions
class UnreadSpanSeedsTests(unittest.TestCase):
    """seeds.py: a span holding an unread clip ships no seed notes (its notes
    are unknown, so the rest is not what plays there)."""

    @classmethod
    def setUpClass(cls):
        cls.project = finisher.project_from_live_tracks(read_arrangement(False))

    def overlaps_unread(self, bars):
        lo, hi = (bars[0] - 1) * 4, bars[1] * 4
        return any(c.get("unread") and c.get("clip_source") == "arrangement"
                   and c["start"] < hi and c["end"] > lo for c in self.project["clips"])

    def test_every_window_with_an_unread_clip_seeds_nothing(self):
        self.assertGreater(self.project["unread_clips"], 0)
        windows = [(101, 108)] + [(b, b + 7) for b in range(1, 101, 4)]
        unread_windows = [w for w in windows if self.overlaps_unread(w)]
        self.assertTrue(unread_windows)
        for window in unread_windows:
            with self.subTest(window=window):
                self.assertEqual(finisher._ending_seed_notes(self.project, list(window)), [])

    def test_a_single_unread_clip_blanks_a_read_neighbour(self):
        read = {"track": "A", "start": 0.0, "end": 16.0, "clip_source": "arrangement",
                "loop_start": 0.0, "loop_end": 16.0, "start_marker": 0.0,
                "notes": [{"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 90}]}
        unread = dict(read, track="B", notes=[], unread=True)
        self.assertTrue(finisher._ending_seed_notes({"clips": [read]}, [1, 4]))
        self.assertEqual(finisher._ending_seed_notes({"clips": [read, unread]}, [1, 4]), [])
        # an unread clip outside the span does not blank it
        far = dict(unread, start=64.0, end=80.0)
        self.assertTrue(finisher._ending_seed_notes({"clips": [read, far]}, [1, 4]))


@on_both_versions
class UnreadErrorMessageTests(unittest.TestCase):
    """The unread error names the right fix: turning the Live API on only
    helps when it was off."""

    def test_live_api_off_says_turn_it_on(self):
        from mcp_server import live_write
        read = {"notes": [], "unread": True, "notes_source": "producer_pal",
                "unread_reason": "the loop brace lies outside [-L, 2L)"}
        with self.assertRaisesRegex(ValueError, "turn on ppal-live-api") as caught:
            live_write.notes_or_unread_error(read, "missing")
        self.assertIn("[-L, 2L)", str(caught.exception))

    def test_a_failed_live_api_read_does_not_say_turn_it_on(self):
        from mcp_server import live_write
        read = {"notes": [], "unread": True, "notes_source": "live_api",
                "unread_reason": "the Live API read of this clip failed (boom)"}
        with self.assertRaises(ValueError) as caught:
            live_write.notes_or_unread_error(read, "missing")
        text = str(caught.exception)
        self.assertNotIn("turn on", text)
        self.assertIn("Live API read failed", text)
        self.assertIn("boom", text)
        self.assertIn("seed_notes", text)

    def test_notes_or_missing_unchanged(self):
        from mcp_server import live_write
        self.assertEqual(live_write.notes_or_unread_error({"notes": [1]}, "m"), [1])
        with self.assertRaisesRegex(ValueError, "^m$"):
            live_write.notes_or_unread_error({"notes": []}, "m")


@on_both_versions
class LiveLoopLengthIntentTests(unittest.TestCase):
    """loopend.py: a live loop_end change changes what plays, so an intended
    finding over it re-asks; a default brace (one pass: loop length = the
    clip's arrangement span) adds no key and hashes as before."""

    def setUp(self):
        from mcp_server import piece_context
        from tests.test_review12h import _live_tracks
        self.temp = tempfile.TemporaryDirectory(prefix="b8r3-loopend-")
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(piece_context, "_decision_path",
                               return_value=os.path.join(self.temp.name, "d.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        path = os.path.join(self.temp.name, "Song.als")
        self.record = lambda **kw: finisher.project_from_live_tracks(_live_tracks(path, **kw))
        self.tracks = lambda **kw: _live_tracks(path, **kw)

    @staticmethod
    def raised(project):
        return [f["detector"] for f in finisher.whats_missing(project_dict=project)["findings"]]

    def test_loop_end_change_re_asks(self):
        base = self.record()
        self.assertIn("uncommitted_ending", self.raised(base))
        finisher.whats_missing(project_dict=base, intended_findings="uncommitted_ending")
        self.assertNotIn("uncommitted_ending", self.raised(self.record()))
        self.assertIn("uncommitted_ending", self.raised(self.record(end="6|1")))

    def test_default_brace_adds_no_key(self):
        one_pass = self.tracks()
        one_pass["tracks"][0]["arrangement_clips"][0]["arrangementLength"] = "4bar"
        clip = finisher.project_from_live_tracks(one_pass)["clips"][0]
        self.assertEqual(finisher._playback_keys(clip), ({}, False))
        looped = self.record()["clips"][0]
        self.assertEqual(finisher._playback_keys(looped)[0], {"loop_length": 16.0})

    def test_default_brace_loop_end_change_re_asks(self):
        def project(end):
            tracks = self.tracks(end=end)
            tracks["tracks"][0]["arrangement_clips"][0]["arrangementLength"] = "4bar"
            return finisher.project_from_live_tracks(tracks)
        prints = lambda p: finisher._intent_fingerprints(p, "uncommitted_ending", [1, 4])
        default, longer = prints(project("5|1")), prints(project("6|1"))
        self.assertEqual(finisher._playback_keys(project("5|1")["clips"][0]), ({}, False))
        self.assertNotEqual(default[0], longer[0])
        # the phase-free fingerprint (rows recorded while playback was
        # unknown) never carries the loop length
        self.assertEqual(default[1], longer[1])

    def test_loop_off_end_marker_change_changes_the_fingerprint(self):
        def project(end):
            tracks = self.tracks(end=end)
            clip = tracks["tracks"][0]["arrangement_clips"][0]
            clip.update(looping=False, arrangementLength="4bar")
            return finisher.project_from_live_tracks(tracks)
        default = project("5|1")
        self.assertEqual(finisher._playback_keys(default["clips"][0]), ({}, False))
        shorter = project("4|1")
        self.assertEqual(finisher._playback_keys(shorter["clips"][0])[0],
                         {"played_length": 12.0})
        prints = lambda p: finisher._intent_fingerprints(p, "uncommitted_ending", [1, 4])[0]
        self.assertNotEqual(prints(default), prints(shorter))


FULL_TRACK_INCLUDE = ["arrangement-clips", "notes", "timing", "sample", "devices",
                      "routings", "mixer"]


def track_includes(device):
    return [sorted(args.get("include") or []) for name, args in device.calls
            if name == "ppal-read-track" and args.get("trackType") in (None, "regular")]


def counts(device):
    names = [name for name, _ in device.calls]
    return {name: names.count(name) for name in sorted(set(names))}


@on_both_versions
class NotesFreeTrackReadsTests(unittest.TestCase):
    """perf: with the Live API on, every MIDI clip's notes come from the Live
    API, so the track reads skip Producer Pal's notes; what is heard / read
    does not change."""

    def hear(self, live_api, full_notes=False):
        device = PieceDevice(live_api=live_api, narrow_reads=True)
        forced = (patch.object(moment, "_arrangement_track_include",
                               return_value=["arrangement-clips", "notes", "timing"])
                  if full_notes else contextlib.nullcontext())
        with forced:
            m = hear(device, 33, 32)
        return m, device

    def test_hear_the_room_skips_track_notes_with_the_live_api(self):
        m, device = self.hear(True)
        self.assertEqual(set(map(tuple, track_includes(device))), {("arrangement-clips", "timing")})
        self.assertEqual(counts(device), {"ppal-live-api": 42, "ppal-read-live-set": 1,
                                          "ppal-read-track": 32})
        baseline, _ = self.hear(True, full_notes=True)
        for key in ("roles", "unheard", "hidden", "unread", "digest"):
            self.assertEqual(m[key], baseline[key], key)

    def test_hear_the_room_keeps_track_notes_without_the_live_api(self):
        _, device = self.hear(False)
        self.assertEqual(set(map(tuple, track_includes(device))),
                         {("arrangement-clips", "notes", "timing")})

    def snap(self, device, full_notes=False):
        if full_notes:
            with patch.object(moment, "_piece_track_include",
                              return_value=list(FULL_TRACK_INCLUDE)):
                return piece_snapshot(device)
        return piece_snapshot(device)

    def test_snapshot_skips_track_notes_with_the_live_api(self):
        device = PieceDevice(live_api=True)
        snap = self.snap(device)
        includes = track_includes(device)
        notes_free = sorted(i for i in FULL_TRACK_INCLUDE if i != "notes")
        # every regular track once without notes; the one track with a MIDI
        # take-lane clip (Live API reads only placed clips) once more with them
        self.assertEqual(includes.count(notes_free), 32)
        self.assertEqual(includes.count(sorted(FULL_TRACK_INCLUDE)), 1)
        baseline = self.snap(PieceDevice(live_api=True), full_notes=True)
        for key in ("tracks", "source_integrity", "notes_read", "uncertain", "hazard_tracks"):
            self.assertEqual(snap[key], baseline[key], key)

    def test_a_failed_live_read_re_reads_that_track_with_notes(self):
        probe = piece_snapshot(PieceDevice(live_api=True))
        full_id = next(c["id"] for t in probe["tracks"] for c in t["arrangement_clips"]
                       if c["kind"] == "midi" and c["coverage"] == "full"
                       and not t["take_lanes"])
        device = PieceDevice(live_api=True)
        del device.clip_live[full_id]
        snap = self.snap(device)
        clip = [c for t in snap["tracks"] for c in t["arrangement_clips"]
                if c["id"] == full_id][0]
        self.assertEqual((clip["notes_status"], clip["notes_source"]),
                         ("read", "producer_pal_window"))
        self.assertTrue(clip["notes"])
        self.assertEqual(track_includes(device).count(sorted(FULL_TRACK_INCLUDE)), 2)

    def test_snapshot_keeps_track_notes_without_the_live_api(self):
        device = PieceDevice(live_api=False)
        piece_snapshot(device)
        self.assertEqual(set(map(tuple, track_includes(device))),
                         {tuple(sorted(FULL_TRACK_INCLUDE))})


def fixture_project(live_api=True, mutate=None):
    from mcp_server import live_write
    device = PieceDevice(live_api=live_api, narrow_reads=True)
    if mutate:
        mutate(device)
    with patch.object(pp, "operation_session", device.operation()):
        tracks = asyncio.run(live_write.read_arrangement_tracks())
    return finisher.project_from_live_tracks(tracks)


@on_both_versions
class IntentOnTheFixtureTests(unittest.TestCase):
    """intent.py: an intended uncommitted_ending on the captured Set re-asks
    exactly when what plays in its span changes."""

    @classmethod
    def setUpClass(cls):
        device = PieceDevice(live_api=True)
        def hides_a_note(clip):
            v = clip["values"]
            return any(not v["loop_start"] <= n["start_time"] < v["loop_end"]
                       for n in clip["notes"])
        # a clip of the last section with a note behind its brace
        cls.cid = sorted(cid for cid, clip in device.clip_live.items()
                         if 400 <= clip["values"].get("start_time", 0) < 432
                         and hides_a_note(clip))[0]

    def setUp(self):
        from mcp_server import piece_context
        self.temp = tempfile.TemporaryDirectory(prefix="b8r3-intent-")
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(piece_context, "_decision_path",
                               return_value=os.path.join(self.temp.name, "d.json"))
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def report(project):
        return finisher.whats_missing(project_dict=project)

    def ending_raised(self, project):
        return any(f["detector"] == "uncommitted_ending"
                   for f in self.report(project)["findings"])

    def values(self, device):
        return device.clip_live[self.cid]["values"]

    def test_intent_re_asks_when_what_plays_changes(self):
        base = fixture_project()
        found = [f for f in self.report(base)["findings"]
                 if f["detector"] == "uncommitted_ending"]
        self.assertEqual([f["bars"] for f in found], [[101, 108]])
        out = finisher.whats_missing(project_dict=base,
                                     intended_findings="uncommitted_ending")
        self.assertEqual(out["intent_recorded"][0]["status"], "recorded")
        self.assertFalse(self.ending_raised(base))
        self.assertFalse(self.ending_raised(fixture_project()))

        def rotate(device):
            v = self.values(device)
            v["start_marker"] = v["loop_start"] + (v["loop_end"] - v["loop_start"]) / 2

        def mute(device):
            self.values(device)["muted"] = True

        def longer_loop(device):
            self.values(device)["loop_end"] += 4

        def playhead(device):
            device.set_playhead(40.0)

        def hidden_note(device):
            clip = device.clip_live[self.cid]
            v = clip["values"]
            note = next(n for n in clip["notes"]
                        if not v["loop_start"] <= n["start_time"] < v["loop_end"])
            note["velocity"] = max(1, note["velocity"] - 7)

        for label, mutate, raised in (("rotated start marker", rotate, True),
                                      ("clip muted", mute, True),
                                      ("loop_end + 4", longer_loop, True),
                                      ("playhead moved", playhead, False),
                                      ("hidden note edited", hidden_note, False)):
            with self.subTest(label):
                self.assertEqual(self.ending_raised(fixture_project(mutate=mutate)), raised)

    def test_live_api_off_neither_raises_nor_suppresses_the_ending(self):
        # its bars hold unread clips: they count as occupied (notes unknown),
        # so the ending is not claimed at all
        report = self.report(fixture_project(live_api=False))
        self.assertNotIn("uncommitted_ending", [f["detector"] for f in report["findings"]])
        self.assertNotIn("uncommitted_ending", [f["detector"] for f in report["suppressed"]])

    def test_live_api_seed_notes_match_the_independent_model(self):
        from tests.piece_notes_truth import arrangement_events, load_truth
        proposal = [p for p in self.report(fixture_project())["proposals"]
                    if p["detector"] == "uncommitted_ending"][0]
        self.assertEqual(proposal["bars"], [101, 108])
        seeds = proposal["command"]["args"]["seed_notes"]
        got = {(round(n["start"], 4), n["pitch"]) for n in seeds}
        lo, hi = 400.0, 432.0
        expected = {(round(pos - lo, 4), pitch)
                    for clip in load_truth().values() if not clip["props"].get("muted")
                    for pos, pitch in arrangement_events(clip) if lo <= pos < hi}
        self.assertTrue(expected)
        self.assertEqual(expected - got, set())
        # anything else is a note sounding into the span from before it
        self.assertTrue(all(start == 0.0 for start, _ in got - expected), got - expected)


if __name__ == "__main__":
    unittest.main()
