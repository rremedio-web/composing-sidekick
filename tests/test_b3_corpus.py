"""Lane B3 CORPUS — regression tests for a phrase index that reflects his playing.

Items 1-6 of .scratch/batch3/brief_corpus.md. All fixtures synthetic (in-code
.als XML, generated note structures); nothing touches Live or the network.
"""
from __future__ import annotations

import gzip
import os
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ingest.parse_als import Clip, Note, from_dict, parse_als, to_dict  # noqa: E402
from tests._requires import requires_catalog


def _key_track_xml(pitch, times):
    events = "\n".join(
        f'<MidiNoteEvent Time="{t}" Duration="0.5" Velocity="100" IsEnabled="true"/>'
        for t in times
    )
    return f"""<KeyTrack><MidiKey Value="{pitch}"/>{events}</KeyTrack>"""


def _session_clip_xml(name, key_tracks, loop_start=0, loop_end=8,
                      loop_on="true", out_marker=8, current_start=0,
                      current_end=None, start_relative=0):
    ce = loop_end if current_end is None else current_end
    return f"""<MidiClip>
        <Name Value="{name}"/>
        <CurrentStart Value="{current_start}"/><CurrentEnd Value="{ce}"/>
        <Loop><LoopStart Value="{loop_start}"/><LoopEnd Value="{loop_end}"/>
        <StartRelative Value="{start_relative}"/><LoopOn Value="{loop_on}"/>
        <OutMarker Value="{out_marker}"/><HiddenLoopStart Value="0"/><HiddenLoopEnd Value="{loop_end}"/></Loop>
        <Notes>{key_tracks}</Notes>
    </MidiClip>"""


def _track_xml(track_name, clip_xml):
    return f"""<MidiTrack><Name><EffectiveName Value="{track_name}"/></Name>
      <DeviceChain><MainSequencer><ClipSlotList><ClipSlot><ClipSlot>
      <Value>{clip_xml}</Value>
      </ClipSlot></ClipSlot></ClipSlotList></MainSequencer></DeviceChain></MidiTrack>"""


def _arrangement_track_xml(track_name, clip_xml):
    return f"""<MidiTrack><Name><EffectiveName Value="{track_name}"/></Name>
      <DeviceChain><MainSequencer>
      <ClipTimeable><ArrangerAutomation><Events>{clip_xml}</Events>
      </ArrangerAutomation></ClipTimeable>
      </MainSequencer></DeviceChain></MidiTrack>"""


def _take_lane_track_xml(track_name, clip_xml):
    return f"""<MidiTrack><Name><EffectiveName Value="{track_name}"/></Name>
      <TakeLanes><TakeLanes><TakeLane><ClipAutomation><Events>{clip_xml}</Events>
      </ClipAutomation></TakeLane></TakeLanes></TakeLanes></MidiTrack>"""


def _write_als(tracks_xml):
    xml = ("<Ableton><LiveSet><Tracks>" + "".join(tracks_xml)
           + "</Tracks></LiveSet></Ableton>")
    tmp = tempfile.NamedTemporaryFile(suffix=".als", delete=False)
    tmp.close()
    with gzip.open(tmp.name, "wb") as fh:
        fh.write(xml.encode("utf-8"))
    return tmp.name


def _eight_notes(pitch=64):
    return [Note(pitch=pitch, start=float(t), duration=0.5, velocity=100 - t)
            for t in range(8)]


class TestSidekickFilter(unittest.TestCase):
    """Item 1: SIDEKICK tracks are tagged at parse time and skipped downstream."""

    def test_parse_tags_sidekick_track_clips(self):
        from ingest.parse_als import is_sidekick_track
        path = _write_als([
            _track_xml("SIDEKICK JAM", _session_clip_xml(
                "jam", _key_track_xml(64, [0, 1, 2, 3]))),
            _track_xml("Lead", _session_clip_xml(
                "lead", _key_track_xml(64, [0, 1, 2, 3]))),
        ])
        try:
            project = parse_als(path)
        finally:
            os.unlink(path)
        self.assertTrue(is_sidekick_track("SIDEKICK JAM"))
        self.assertTrue(is_sidekick_track("sidekick booth"))
        self.assertFalse(is_sidekick_track("Lead"))
        by_track = {c.track: c for c in project.clips}
        self.assertTrue(by_track["SIDEKICK JAM"].is_sidekick)
        self.assertFalse(by_track["Lead"].is_sidekick)

    def test_sidekick_flag_survives_materialization_round_trip(self):
        clip = Clip(name="jam", track="SIDEKICK JAM", start=0.0, end=8.0,
                    note_count=8, is_sidekick=True, notes=_eight_notes())
        project = from_dict(to_dict(__import__("ingest.parse_als", fromlist=["Project"]).Project(
            path="/tmp/x.als", tempo=120.0, track_count=1,
            midi_track_names=["SIDEKICK JAM"], audio_track_names=[],
            clip_count=1, total_notes=8, clips=[clip])))
        self.assertTrue(project.clips[0].is_sidekick)

    def test_build_index_skips_sidekick_clips(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from build_index import iter_phrases
        data = {
            "path": "/tmp/mix.als", "tempo": 120.0,
            "clips": [
                {"name": "jam", "track": "SIDEKICK JAM", "is_drum": False,
                 "clip_source": "session", "is_sidekick": True,
                 "notes": [{"pitch": 64, "start": float(t), "duration": 0.5,
                            "velocity": 100, "enabled": True} for t in range(8)]},
                {"name": "lead", "track": "Lead", "is_drum": False,
                 "clip_source": "session", "is_sidekick": False,
                 "notes": [{"pitch": 64, "start": float(t), "duration": 0.5,
                            "velocity": 100, "enabled": True} for t in range(8)]},
            ],
        }
        phrases = list(iter_phrases(data))
        self.assertTrue(phrases)
        self.assertTrue(all(p["track"] != "SIDEKICK JAM" for p in phrases))

    def test_style_profile_ignores_sidekick_clips(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from ingest.parse_als import Project
        from style_profile import analyze_project
        sidekick = Clip(name="jam", track="SIDEKICK JAM", start=0.0, end=8.0,
                        note_count=8, is_sidekick=True, notes=_eight_notes(64))
        lead = Clip(name="lead", track="Lead", start=0.0, end=8.0,
                    note_count=8, notes=_eight_notes(72))
        project = Project(path="/tmp/mix.als", tempo=120.0, track_count=2,
                          midi_track_names=["SIDEKICK JAM", "Lead"],
                          audio_track_names=[], clip_count=2, total_notes=16,
                          clips=[sidekick, lead])
        rec = analyze_project(project)
        # Only the real lead line informs harmony: mean pitch must be the lead's.
        self.assertEqual(rec["pitch_mean"], 72.0)

    def test_harmony_ignores_sidekick_clips(self):
        # Item 12: harmony skips SIDEKICK clips too — flagged ones and
        # legacy ones that only carry the track name.
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from harmony import harmonic_notes
        data = {
            "clips": [
                {"clip_source": "session", "is_drum": False,
                 "is_sidekick": True, "track": "SIDEKICK JAM",
                 "notes": [{"pitch": 60, "start": 0.0, "duration": 1.0,
                            "velocity": 100, "enabled": True}]},
                {"clip_source": "session", "is_drum": False,
                 "track": "SIDEKICK BOOTH",  # legacy: no flag, name only
                 "notes": [{"pitch": 62, "start": 0.0, "duration": 1.0,
                            "velocity": 100, "enabled": True}]},
                {"clip_source": "session", "is_drum": False,
                 "is_sidekick": False, "track": "Lead",
                 "notes": [{"pitch": 64, "start": 0.0, "duration": 1.0,
                            "velocity": 100, "enabled": True}]},
            ],
        }
        self.assertEqual([n["pitch"] for n in harmonic_notes(data)], [64])


class TestAudibleRegions(unittest.TestCase):
    """Item 2: parse_als reads the clip Loop brace; build_index windows it."""

    def test_parse_reads_loop_brace(self):
        path = _write_als([
            _track_xml("Lead", _session_clip_xml(
                "brace", _key_track_xml(64, [0, 4, 8, 9, 10, 11]),
                loop_start=8, loop_end=16, out_marker=16)),
        ])
        try:
            project = parse_als(path)
        finally:
            os.unlink(path)
        clip = project.clips[0]
        self.assertEqual((clip.loop_start, clip.loop_end), (8.0, 16.0))
        self.assertTrue(clip.loop_on)
        self.assertEqual((clip.audible_start, clip.audible_end), (8.0, 16.0))

    def test_loop_off_uses_start_end_markers(self):
        # Fix item 6 deliberately changes this (was: audible = (0, OutMarker)):
        # with the loop off, LoopStart/LoopEnd hold the start/end markers, so
        # audible = [LoopStart, min(LoopEnd, LoopStart + timeline)). Shape
        # mirrors fado2.als (ls=2.25 le=9.45 om=25.45, timeline 7.2).
        path = _write_als([
            _track_xml("Lead", _session_clip_xml(
                "noloop", _key_track_xml(64, [0, 1, 2, 3, 4, 5, 6, 7]),
                loop_start=2.25, loop_end=9.45,
                loop_on="false", out_marker=25.45, current_end=7.2)),
        ])
        try:
            project = parse_als(path)
        finally:
            os.unlink(path)
        clip = project.clips[0]
        self.assertFalse(clip.loop_on)
        self.assertAlmostEqual(clip.audible_start, 2.25)
        self.assertAlmostEqual(clip.audible_end, 9.45)

    def test_loop_off_clamps_to_timeline(self):
        # audible = [LoopStart, min(LoopEnd, LoopStart + timeline)).
        path = _write_als([
            _track_xml("Lead", _session_clip_xml(
                "noloop", _key_track_xml(64, [0, 1, 2, 3]),
                loop_start=2, loop_end=10,
                loop_on="false", out_marker=10, current_end=7)),
        ])
        try:
            project = parse_als(path)
        finally:
            os.unlink(path)
        clip = project.clips[0]
        self.assertEqual((clip.audible_start, clip.audible_end), (2.0, 9.0))

    def test_loop_on_arrangement_clamps_to_one_brace_pass(self):
        # An arrangement clip plays loop repeats, but the index takes one
        # pass of the brace: clamp to the timeline length.
        path = _write_als([
            _arrangement_track_xml("Lead", _session_clip_xml(
                "arr", _key_track_xml(64, [0, 1, 2, 3, 4, 5, 6, 7]),
                loop_start=0, loop_end=16, loop_on="true", out_marker=16,
                current_start=100, current_end=108)),
        ])
        try:
            project = parse_als(path)
        finally:
            os.unlink(path)
        clip = project.clips[0]
        self.assertEqual(clip.clip_source, "arrangement")
        self.assertTrue(clip.loop_on)
        self.assertEqual((clip.audible_start, clip.audible_end), (0.0, 8.0))

    def test_loop_on_arrangement_starts_at_the_start_marker(self):
        # Real shape (1_5DEZ): ls=144 le=240 sr=32, 64 beats on the timeline.
        # The pass ends exactly at the brace end — no wrap: [176, 240).
        path = _write_als([
            _arrangement_track_xml("Lead", _session_clip_xml(
                "arr", _key_track_xml(64, [150, 180, 200]),
                loop_start=144, loop_end=240, loop_on="true", out_marker=240,
                current_start=0, current_end=64, start_relative=32)),
        ])
        try:
            project = parse_als(path)
        finally:
            os.unlink(path)
        clip = project.clips[0]
        self.assertEqual((clip.audible_start, clip.audible_end), (176.0, 240.0))

    def test_loop_on_arrangement_wrap_uses_whole_brace(self):
        path = _write_als([
            _arrangement_track_xml("Lead", _session_clip_xml(
                "arr", _key_track_xml(64, [0, 8, 16]),
                loop_start=0, loop_end=16, loop_on="true", out_marker=16,
                current_start=0, current_end=88, start_relative=8)),
        ])
        try:
            project = parse_als(path)
        finally:
            os.unlink(path)
        clip = project.clips[0]
        self.assertEqual((clip.audible_start, clip.audible_end), (0.0, 16.0))

    def test_loop_on_take_lane_clamps_to_timeline(self):
        # Take lanes sit on the arrangement timeline: clamp like arrangement.
        path = _write_als([
            _take_lane_track_xml("Lead", _session_clip_xml(
                "take", _key_track_xml(64, [0, 1, 2, 3, 12]),
                loop_start=0, loop_end=16, loop_on="true", out_marker=16,
                current_start=40, current_end=48)),
        ])
        try:
            project = parse_als(path)
        finally:
            os.unlink(path)
        clip = project.clips[0]
        self.assertEqual(clip.clip_source, "take_lane")
        self.assertEqual((clip.audible_start, clip.audible_end), (0.0, 8.0))

    def test_session_loop_on_ignores_start_relative(self):
        # A session clip cycles the whole brace; StartRelative only offsets
        # where the first cycle begins.
        path = _write_als([
            _track_xml("Lead", _session_clip_xml(
                "sess", _key_track_xml(64, [0, 1, 2, 3]),
                loop_start=0, loop_end=8, loop_on="true", out_marker=8,
                start_relative=4)),
        ])
        try:
            project = parse_als(path)
        finally:
            os.unlink(path)
        clip = project.clips[0]
        self.assertEqual((clip.audible_start, clip.audible_end), (0.0, 8.0))

    def test_windows_start_at_audible_start(self):
        # Windows open at audible_start, not floor(lo/8)*8: with notes 0..15
        # but an audible region starting at beat 2, the first window is 2.
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from build_index import iter_phrases
        notes = [{"pitch": 64, "start": float(t), "duration": 0.5,
                  "velocity": 100, "enabled": True} for t in range(16)]
        data = {
            "path": "/tmp/brace.als", "tempo": 120.0,
            "clips": [
                {"name": "brace", "track": "Lead", "is_drum": False,
                 "clip_source": "session", "is_sidekick": False,
                 "loop_start": 2.0, "loop_end": 10.0, "loop_on": False,
                 "audible_start": 2.0, "audible_end": 10.0,
                 "notes": notes},
            ],
        }
        phrases = list(iter_phrases(data))
        self.assertTrue(phrases)
        self.assertEqual(phrases[0]["window"], 2)
        self.assertTrue(all(n["start"] >= 0.0 for p in phrases for n in p["notes"]))
        self.assertEqual(min(n["start"] for p in phrases for n in p["notes"]), 0.0)

    def test_build_index_windows_only_the_audible_region(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from build_index import iter_phrases
        notes = [{"pitch": 64, "start": float(t), "duration": 0.5,
                  "velocity": 100, "enabled": True} for t in range(16)]
        data = {
            "path": "/tmp/brace.als", "tempo": 120.0,
            "clips": [
                {"name": "brace", "track": "Lead", "is_drum": False,
                 "clip_source": "session", "is_sidekick": False,
                 "loop_start": 8.0, "loop_end": 16.0, "loop_on": True,
                 "audible_start": 8.0, "audible_end": 16.0,
                 "notes": notes},
            ],
        }
        phrases = list(iter_phrases(data))
        self.assertTrue(phrases)
        for p in phrases:
            # Fix item 2: the window lives on the alias now (id is the pid).
            starts = [n["start"] + int(p["alias"].split("|w")[-1]) for n in p["notes"]]
            self.assertTrue(all(s >= 8.0 for s in starts),
                            f"cropped notes leaked into {p['id']}")
            self.assertTrue(p["audible"])


class TestWrappedPassPlaysTwoSlices(unittest.TestCase):
    """48h review P3: a short arrangement pass that wraps plays [S, LoopEnd)
    then [LoopStart, ...) — never the middle of the brace. Asserted through
    the real parser AND iter_phrases, not only _parse_loop."""

    def _parse(self, notes_at, *, sr, length, ls=0, le=16):
        path = _write_als([
            _arrangement_track_xml("Lead", _session_clip_xml(
                "arr", _key_track_xml(64, notes_at),
                loop_start=ls, loop_end=le, loop_on="true", out_marker=le,
                current_start=0, current_end=length, start_relative=sr)),
        ])
        try:
            return parse_als(path)
        finally:
            os.unlink(path)

    def _phrases(self, project):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from build_index import iter_phrases
        from dataclasses import asdict
        data = asdict(project)
        data["path"] = "/tmp/wrap.als"
        return list(iter_phrases(data))

    def test_note_on_before_single_audible_start_is_not_triggered(self):
        # ALS fixture shape comes from _session_clip_xml / parse_als.
        project = self._parse(list(range(16)), sr=0, length=16, ls=4, le=16)
        from dataclasses import asdict
        data = asdict(project)
        data["path"] = "/tmp/single-region.als"
        data["clips"][0]["audible_regions"] = None
        data["clips"][0]["notes"][3].update(pitch=50, start=3.5, duration=2.0)
        from build_index import iter_phrases
        phrases = list(iter_phrases(data))
        starts = [int(p["alias"].split("|w")[-1]) + n["start"]
                  for p in phrases for n in p["notes"]]
        self.assertTrue(starts)
        self.assertGreaterEqual(min(starts), 4.0)
        self.assertFalse(any(n["pitch"] == 50 for p in phrases for n in p["notes"]))

    def test_note_on_before_wrap_head_region_is_not_triggered(self):
        # ALS fixture shape comes from _session_clip_xml / parse_als.
        project = self._parse(list(range(16)), sr=14, length=4, ls=4, le=16)
        from dataclasses import asdict
        data = asdict(project)
        data["path"] = "/tmp/wrap.als"
        data["clips"][0]["notes"][3].update(pitch=50, start=3.5, duration=2.0)
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from build_index import iter_phrases
        phrases = list(iter_phrases(data))
        starts = [int(p["alias"].split("|w")[-1]) + n["start"]
                  for p in phrases for n in p["notes"]]
        self.assertTrue(starts)
        self.assertFalse(any(0.0 <= start < 4.0 for start in starts))
        self.assertFalse(any(n["pitch"] == 50 for p in phrases for n in p["notes"]))

    def test_note_sustained_across_eight_beat_window_is_kept_in_both(self):
        # Note/clip shape mirrors _key_track_xml parsed by parse_als.
        project = self._parse(list(range(16)), sr=0, length=16, ls=0, le=16)
        from dataclasses import asdict
        data = asdict(project)
        data["path"] = "/tmp/window-boundary.als"
        data["clips"][0]["audible_regions"] = None
        data["clips"][0]["audible_start"] = None
        data["clips"][0]["audible_end"] = None
        data["clips"][0]["notes"][7].update(pitch=50, start=7.5, duration=1.0)
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from build_index import iter_phrases
        phrases = list(iter_phrases(data))
        windows = {p["window"] for p in phrases
                   if any(n["pitch"] == 50 for n in p["notes"])}
        self.assertEqual(windows, {0, 8})

    def test_regions_for_each_pass_shape(self):
        cases = [
            # (sr, pass length) -> regions / single range
            ((14, 4), [[14.0, 16.0], [0.0, 2.0]], (0.0, 16.0)),   # reviewer case
            ((8, 9), [[8.0, 16.0], [0.0, 1.0]], (0.0, 16.0)),     # partial wrap
            ((8, 8), None, (8.0, 16.0)),                          # exact end
            ((8, 16), None, (0.0, 16.0)),                         # full pass
            ((8, 88), None, (0.0, 16.0)),                         # many passes
            ((4, 4), None, (4.0, 8.0)),                           # no wrap
        ]
        for (sr, length), regions, single in cases:
            with self.subTest(sr=sr, length=length):
                clip = self._parse([1], sr=sr, length=length).clips[0]
                self.assertEqual(clip.audible_regions, regions)
                self.assertEqual((clip.audible_start, clip.audible_end), single)

    def test_unheard_middle_is_never_indexed(self):
        # notes only at beats 4-7: none of them plays in a [14,16)+[0,2) pass
        project = self._parse([4, 5, 6, 7], sr=14, length=4)
        self.assertEqual(self._phrases(project), [])

    def test_played_sequence_is_tail_then_head(self):
        # notes at 14, 15 (tail) and 0, 1 (head) play as one 4-note phrase
        project = self._parse([0, 1, 14, 15], sr=14, length=4)
        phrases = self._phrases(project)
        self.assertEqual(len(phrases), 1)
        self.assertEqual([n["start"] for n in phrases[0]["notes"]],
                         [0.0, 1.0, 2.0, 3.0])


class TestContentAddressedIds(unittest.TestCase):
    """Item 3: pids are content-addressed; lookups resolve pid first, alias after."""

    def _notes(self, shift=0):
        return [{"pitch": 64 + (i % 3), "start": float(i + shift),
                 "duration": 0.5, "velocity": 100} for i in range(6)]

    def test_pid_stable_and_window_scoped(self):
        # Fix item 1 (orchestrator design decision) deliberately changes this:
        # pid = digest(role + canonical notes) only — window/clip identity no
        # longer enter the digest, so the same phrase windows share one pid.
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import phrase_pid
        a = phrase_pid(self._notes(), role="lead")
        b = phrase_pid(self._notes(), role="lead")
        d = phrase_pid(self._notes(shift=5), role="lead")
        e = phrase_pid(self._notes(), role="bass")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("p") and len(a) == 13)
        self.assertNotEqual(a, d)
        self.assertNotEqual(a, e)

    def test_iter_phrases_carries_pid_and_alias(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from build_index import iter_phrases
        data = {
            "path": "/tmp/song.als", "tempo": 120.0,
            "clips": [
                {"name": "take", "track": "Lead", "is_drum": False,
                 "clip_source": "session", "is_sidekick": False,
                 "loop_start": None, "loop_end": None, "loop_on": True,
                 "audible_start": None, "audible_end": None,
                 "notes": [{"pitch": 64, "start": float(t), "duration": 0.5,
                            "velocity": 100, "enabled": True} for t in range(8)]},
            ],
        }
        phrases = list(iter_phrases(data))
        self.assertTrue(phrases)
        for p in phrases:
            self.assertTrue(p["pid"].startswith("p"))
            # Fix item 2: the record id IS the pid; the positional id is the alias.
            self.assertEqual(p["id"], p["pid"])
            self.assertIn("|c", p["alias"])

    def test_duplicate_pids_fail_the_build_check(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import check_duplicate_pids
        dup = {"pid": "pabc", "alias": "s|c0|w0", "id": "s|c0|w0"}
        with self.assertRaises(ValueError):
            check_duplicate_pids([dict(dup), dict(dup, alias="s|c1|w0")])

    def test_lookup_resolves_pid_first_then_alias(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import ids_match, phrase_lookup
        phrases = [{"pid": "p111", "alias": "s|c0|w0", "id": "s|c0|w0"},
                   {"pid": "p222", "alias": "s|c1|w0", "id": "s|c1|w0"}]
        idx = phrase_lookup(phrases)
        self.assertIs(idx["p111"], phrases[0])
        self.assertIs(idx["s|c1|w0"], phrases[1])
        self.assertTrue(ids_match("p111", phrases[0]))
        self.assertTrue(ids_match("s|c0|w0", phrases[0]))
        self.assertFalse(ids_match("s|c9|w9", phrases[0]))

    def test_migration_rewrites_stored_ids_with_backup(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import migrate_stored_ids
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        ratings_path = os.path.join(tmp, "ratings.json")
        ledger_path = os.path.join(tmp, "session_ledger.json")
        import json
        with open(ratings_path, "w") as fh:
            json.dump({"ratings": [
                {"phrase_id": "s|c0|w0", "rating": 1},
                {"phrase_id": "notes:xyz", "rating": -1}]}, fh)
        with open(ledger_path, "w") as fh:
            json.dump({"sessions": [{"entries": [
                {"source_phrase_id": "s|c0|w0"},
                {"source_phrase_id": "s|other|w0"}]}]}, fh)
        id_map = {"s|c0|w0": "p111"}
        dry = migrate_stored_ids(ratings_path, ledger_path, id_map, dry_run=True)
        self.assertEqual(dry["ratings_rewritten"], 1)
        self.assertEqual(dry["ledger_rewritten"], 1)
        with open(ratings_path) as fh:  # dry run writes nothing
            self.assertIn("s|c0|w0", fh.read())
        real = migrate_stored_ids(ratings_path, ledger_path, id_map, dry_run=False)
        self.assertEqual(real["ratings_rewritten"], 1)
        backups = [name for name in os.listdir(tmp)
                   if name.startswith("ratings.json.") and name.endswith(".bak")]
        self.assertEqual(len(backups), 1)
        with open(ratings_path) as fh:
            ratings = json.load(fh)["ratings"]
        self.assertEqual(ratings[0]["phrase_id"], "p111")
        self.assertEqual(ratings[1]["phrase_id"], "notes:xyz")
        with open(ledger_path) as fh:
            entries = json.load(fh)["sessions"][0]["entries"]
        self.assertEqual(entries[0]["source_phrase_id"], "p111")

    def test_migration_keeps_only_three_timestamped_backups(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import _timestamped_backup
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "ratings.json")
        with open(path, "w") as fh:
            fh.write("current")
        for i in range(5):
            with open(path, "w") as fh:
                fh.write(str(i))
            _timestamped_backup(path)
        backups = [name for name in os.listdir(tmp)
                   if name.startswith("ratings.json.") and name.endswith(".bak")]
        self.assertEqual(len(backups), 3)

    def test_timestamped_backup_pruning_preserves_one_shot_backup(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import _timestamped_backup
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "session_ledger.json")
        with open(path, "w") as fh:
            fh.write("current")
        legacy = path + ".bak"
        with open(legacy, "w") as fh:
            fh.write("one-shot backup")
        for i in range(3):
            with open(path, "w") as fh:
                fh.write(str(i))
            _timestamped_backup(path)
        with open(path, "w") as fh:
            fh.write("newest")
        _timestamped_backup(path)
        self.assertTrue(os.path.exists(legacy))
        timestamped = [name for name in os.listdir(tmp)
                       if name.startswith("session_ledger.json.")
                       and name.endswith(".bak") and name != "session_ledger.json.bak"]
        self.assertEqual(len(timestamped), 3)

    def test_migration_skips_backup_when_no_ids_are_rewritten(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import migrate_stored_ids
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        ratings_path = os.path.join(tmp, "ratings.json")
        ledger_path = os.path.join(tmp, "session_ledger.json")
        with open(ratings_path, "w") as fh:
            fh.write('{"ratings": [{"phrase_id": "already-current"}]}')
        with open(ledger_path, "w") as fh:
            fh.write('{"sessions": [{"entries": []}]}')
        report = migrate_stored_ids(ratings_path, ledger_path,
                                    {"old-id": "new-id"}, dry_run=False)
        self.assertEqual(report["ratings_rewritten"], 0)
        self.assertEqual(report["ledger_rewritten"], 0)
        backups = [name for name in os.listdir(tmp) if name.endswith(".bak")]
        self.assertEqual(backups, [])


class TestDedupeProvenance(unittest.TestCase):
    """Item 4: one record per distinct note-set, family-weighted stats."""

    def test_song_family_strips_version_suffixes(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import song_family
        self.assertEqual(song_family("demosong10"), "demosong")
        self.assertEqual(song_family("NOCTURNEv2"), "nocturne")
        self.assertEqual(song_family("ballad"), "ballad")
        self.assertEqual(song_family("sessiondemo2"), "sessiondemo")
        self.assertEqual(song_family("707"), "707")  # never strip to empty
        self.assertEqual(song_family("song 2026-08-16"), "song")

    def test_dedupe_phrases_keeps_one_record_with_provenance(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import dedupe_phrases
        base = {"role": "lead", "kind": "harmonic", "window": 0,
                "notes": [{"pitch": 60 + i, "start": float(i),
                           "duration": 0.5, "velocity": 100} for i in range(4)]}
        phrases = [dict(base, pid=f"p{i}", alias=f"s{i}|c0|w0",
                        project=f"s{i}", track="Lead") for i in range(3)]
        phrases.append(dict(base, pid="p9", alias="other|c0|w0",
                            project="other", track="Bass",
                            notes=[{"pitch": 40, "start": 0.0,
                                    "duration": 1.0, "velocity": 90}],
                            role="bass"))
        deduped, remap = dedupe_phrases(phrases)
        self.assertEqual(len(deduped), 2)
        lead = next(p for p in deduped if p["role"] == "lead")
        self.assertEqual(len(lead["provenance"]), 3)
        self.assertEqual({e["project"] for e in lead["provenance"]},
                         {"s0", "s1", "s2"})
        self.assertEqual(remap["s1|c0|w0"], "p0")  # collapsed → kept pid

    def _style_rec(self, name, t1):
        return {"name": name, "tempo": 120.0, "has_harmonic": True,
                "key": "C minor", "harmonic_notes": 10, "drum_notes": 0,
                "harmonic_groove": [0] * 16,
                "drum_groove": [0] * 16, "intervals": {}, "durations": [],
                "pitch_hist": [0] * 128, "transitions_1": t1,
                "transitions_2": {}, "degree_counts": {"0": 1},
                "transitions_1_by_role": {}, "transitions_2_by_role": {},
                "degree_counts_by_role": {}, "scale_conformity": 1.0,
                "avg_polyphony": 1.0, "velocity_mean": 100.0,
                "pitch_min": 60, "pitch_max": 72}

    def test_grammar_caps_version_family_duplicates(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from style_profile import aggregate
        projects = [self._style_rec("demosong2", {"0->7": 100}),
                    self._style_rec("demosong10", {"0->7": 100}),
                    self._style_rec("ballad", {"0->5": 1})]
        grammar = aggregate(projects)["melodic_grammar"]
        # One family, one vote: 100/(100+1), not 200/(200+1).
        self.assertEqual(grammar["order_1"]["0"]["7"], round(100 / 101, 4))

    def _motif_store(self, tmp, spec):
        """spec: list of (project, line) → temp STORE with index + profile."""
        import json
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        phrases = []
        for project, line in spec:
            notes = [{"pitch": p, "start": float(i), "duration": 0.5,
                      "velocity": 100} for i, p in enumerate(line)]
            phrases.append({"id": f"{project}|c0|w0", "pid": f"p{len(phrases)}",
                            "alias": f"{project}|c0|w0", "project": project,
                            "track": "Lead", "kind": "harmonic",
                            "notes": notes})
        with open(os.path.join(tmp, "phrase_index.json"), "w") as fh:
            json.dump({"phrases": phrases}, fh)
        with open(os.path.join(tmp, "style_profile.json"), "w") as fh:
            json.dump({"projects": [
                {"name": p, "key": "C minor", "has_harmonic": True}
                for p in {p for p, _ in spec}]}, fh)

    def test_motifs_count_distinct_lines_and_family_support(self):
        import json
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        import motifs as _motifs
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        # Same 3-gram (3,5,7) carried by 30 DISTINCT lines across 2 families.
        spec = []
        for i in range(30):
            proj = ["alpha", "alpha2", "beta"][i % 3]
            spec.append((proj, [60 + i, 63, 65, 67, 69, 67]))
        self._motif_store(tmp, spec)
        old_store, _motifs.STORE = _motifs.STORE, tmp
        try:
            cells = _motifs.build()["cells"]
        finally:
            _motifs.STORE = old_store
        hit = next(c for c in cells if c["degrees"] == [3, 5, 7])
        self.assertEqual(hit["count"], 30)
        self.assertEqual(hit["support"], 2)  # families, not .als files

    def test_motifs_support_counts_provenance_families(self):
        # Item 7: one deduped record whose provenance spans 3 song families
        # gives its cells support 3 (survivor project alone would give 1).
        import json
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        import motifs as _motifs
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        line = [72, 74, 76, 79, 81, 79]
        notes = [{"pitch": p, "start": float(i), "duration": 0.5,
                  "velocity": 100} for i, p in enumerate(line)]
        phrases = [{
            "id": "pid-alpha", "pid": "pid-alpha",
            "alias": "alpha|c0|w0", "project": "alpha",
            "track": "Lead", "kind": "harmonic", "notes": notes,
            "provenance": [
                {"alias": "alpha|c0|w0", "project": "alpha",
                 "track": "Lead", "clip": "a", "window": 0},
                {"alias": "beta|c2|w8", "project": "beta",
                 "track": "Lead", "clip": "b", "window": 8},
                {"alias": "gamma|c1|w0", "project": "gamma",
                 "track": "Keys", "clip": "c", "window": 0},
            ],
        }]
        with open(os.path.join(tmp, "phrase_index.json"), "w") as fh:
            json.dump({"phrases": phrases}, fh)
        with open(os.path.join(tmp, "style_profile.json"), "w") as fh:
            json.dump({"projects": [
                {"name": "alpha", "key": "C minor", "has_harmonic": True},
                {"name": "beta", "key": "D minor", "has_harmonic": True},
                {"name": "gamma", "key": "E minor", "has_harmonic": True}]}, fh)
        old_store, _motifs.STORE = _motifs.STORE, tmp
        old_min_count, _motifs.MIN_COUNT = _motifs.MIN_COUNT, 1
        old_min_support, _motifs.MIN_SUPPORT = _motifs.MIN_SUPPORT, 1
        try:
            cells = _motifs.build()["cells"]
        finally:
            _motifs.STORE = old_store
            _motifs.MIN_COUNT = old_min_count
            _motifs.MIN_SUPPORT = old_min_support
        hit = next(c for c in cells if c["degrees"] == [0, 2, 4])
        self.assertEqual(hit["count"], 1)  # alpha's own tonic
        self.assertEqual(hit["support"], 1)
        self.assertTrue(any(c["degrees"] == [10, 0, 2] for c in cells))  # beta
        self.assertTrue(any(c["degrees"] == [8, 10, 0] for c in cells))  # gamma

    def test_motifs_ignores_save_as_duplicates(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        import motifs as _motifs
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        # The identical line saved 25x across one version family: one vote.
        spec = [(["demosong2", "demosong3", "demosong10"][i % 3],
                 [72, 74, 76, 77, 79, 77]) for i in range(25)]
        self._motif_store(tmp, spec)
        old_store, _motifs.STORE = _motifs.STORE, tmp
        try:
            cells = _motifs.build()["cells"]
        finally:
            _motifs.STORE = old_store
        self.assertFalse(any(c["degrees"] == [0, 2, 4, 5] for c in cells))

    def test_taste_negatives_count_distinct_phrases_once(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import dedupe_pool
        notes = lambda p: [{"pitch": p + i, "start": float(i),
                            "duration": 0.5, "velocity": 100} for i in range(4)]
        pool = [{"role": "lead", "kind": "harmonic", "notes": notes(60),
                 "id": f"dup|c{i}|w0"} for i in range(5)]
        pool.append({"role": "lead", "kind": "harmonic", "notes": notes(72),
                     "id": "other|c0|w0"})
        self.assertEqual(len(dedupe_pool(pool)), 2)


class TestSafeRebuild(unittest.TestCase):
    """Item 5: rebuild refuses on missing roots/shrink; dry-run by default."""

    def _store(self, tmp, n_projects=10, n_phrases=100):
        import json
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        root_a = os.path.join(tmp, "driveA")
        os.makedirs(root_a)
        rows = [{"path": os.path.join(root_a, f"s{i}.als"), "name": f"s{i}",
                 "error": None} for i in range(n_projects)]
        with open(os.path.join(tmp, "catalog_manifest.json"), "w") as fh:
            json.dump(rows, fh)
        with open(os.path.join(tmp, "phrase_index.json"), "w") as fh:
            json.dump({"meta": {}, "count": n_phrases, "phrases": []}, fh)
        return root_a

    def test_missing_root_refused(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from rebuild_corpus import check_roots_mounted
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        root_a = self._store(tmp)
        # The previous manifest drew from a drive that is not mounted now.
        gone = os.path.join(tmp, "unmounted-drive")
        prev = [os.path.join(root_a, "s0.als"), os.path.join(gone, "sea.als")]
        missing = check_roots_mounted([root_a, gone], prev)
        self.assertEqual(missing, [gone])
        self.assertEqual(check_roots_mounted([root_a], prev), [])

    def test_shrink_refused_without_flag(self):
        # Item 8 deliberately replaces --allow-shrink with the two separate
        # flags; the phrase side gates on n_windows now.
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from rebuild_corpus import check_shrink
        before = {"projects": 100, "phrases": 1000, "n_windows": 1000}
        after = {"projects": 93, "phrases": 1000, "n_windows": 1000}
        self.assertIsNotNone(check_shrink(before, after))
        after = {"projects": 100, "phrases": 1000, "n_windows": 940}
        self.assertIsNotNone(check_shrink(before, after))
        after = {"projects": 96, "phrases": 960, "n_windows": 960}
        self.assertIsNone(check_shrink(before, after))
        after = {"projects": 50, "phrases": 500, "n_windows": 500}
        self.assertIsNone(check_shrink(
            before, after, allow_project_shrink=True,
            allow_phrase_shrink=True))

    def test_shrink_flags_are_independent(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from rebuild_corpus import check_shrink
        before = {"projects": 100, "phrases": 1000, "n_windows": 1000}
        shrunk = {"projects": 50, "phrases": 500, "n_windows": 500}
        # One flag does not excuse the other side.
        self.assertIsNotNone(check_shrink(
            before, shrunk, allow_project_shrink=True))
        self.assertIsNotNone(check_shrink(
            before, shrunk, allow_phrase_shrink=True))

    def test_phrase_gate_uses_windows_not_deduped_count(self):
        # A dedupe improvement collapses the phrase count without losing any
        # indexed music: windows hold steady, so no refusal.
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from rebuild_corpus import check_shrink, index_counts
        before = {"projects": 100, "phrases": 1000, "n_windows": 1000}
        after = {"projects": 100, "phrases": 120, "n_windows": 1000}
        self.assertIsNone(check_shrink(before, after))

    def test_index_counts_falls_back_to_phrases_without_stamp(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from rebuild_corpus import index_counts
        old = {"meta": {}, "count": 300, "phrases": []}
        self.assertEqual(index_counts(old)["n_windows"], 300)
        new = {"meta": {"corpus": {"n_windows": 30000}}, "count": 3000,
               "phrases": []}
        self.assertEqual(index_counts(new)["n_windows"], 30000)

    def test_index_counts_reads_what_build_meta_stamps(self):
        # Round trip: the gate must see the stamp build_index writes.
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from build_index import build_meta
        from rebuild_corpus import index_counts
        idx = {"meta": build_meta(2992, n_windows=30086), "count": 2992,
               "phrases": []}
        self.assertEqual(index_counts(idx)["n_windows"], 30086)

    def test_build_meta_stamps_n_windows(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from build_index import build_meta
        meta = build_meta(3, n_windows=150)
        self.assertEqual(meta["corpus"]["n_windows"], 150)
        self.assertNotIn("n_windows",
                         build_meta(3)["corpus"])  # unstamped old shape

    def test_dry_run_writes_nothing(self):
        import json
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        root_a = self._store(tmp)
        calls = []
        import rebuild_corpus as _rb
        out_manifest = os.path.join(tmp, "catalog_manifest.json")

        def fake_run(name, script, args):
            calls.append(name)
            out = args[args.index("--out") + 1]
            rows = [{"path": os.path.join(root_a, f"s{i}.als"),
                     "name": f"s{i}", "error": None} for i in range(10)]
            with open(out, "w") as fh:
                json.dump(rows, fh)
            return 0

        rc = _rb.main(["--store", tmp, "--roots", root_a,
                       "--manifest", out_manifest],
                      run_stage=fake_run)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["scan"])  # scan-to-tmp only, nothing applied
        with open(out_manifest) as fh:  # previous manifest untouched
            self.assertEqual(len(json.load(fh)), 10)

    def test_migration_preview_derives_map_from_candidate_index(self):
        import json
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import phrase_pid
        from rebuild_corpus import preview_id_map
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        notes = [{"pitch": 64 + i, "start": float(i), "duration": 0.5}
                 for i in range(4)]
        old = {"id": "song|c2|w0", "alias": "song|c2|w0",
               "role": "lead", "notes": notes}
        pid = phrase_pid(notes, role="lead")
        with open(os.path.join(tmp, "phrase_index.json"), "w") as fh:
            json.dump({"phrases": [old]}, fh)
        with open(os.path.join(tmp, "phrase_id_map.json"), "w") as fh:
            json.dump({"map": {}, "ambiguous": {}, "gone": {}}, fh)
        candidate = os.path.join(tmp, "candidate.json")
        with open(candidate, "w") as fh:
            json.dump({"phrases": [{"id": pid, "pid": pid,
                                    "alias": "song|c3|w0", "role": "lead",
                                    "notes": notes}]}, fh)
        result = preview_id_map(tmp, candidate)
        self.assertEqual(result["map"]["song|c2|w0"], pid)
        self.assertFalse(os.path.exists(os.path.join(tmp, "phrase_id_map.preview.json")))

    def test_commit_runs_stages_in_order(self):
        import json
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        root_a = self._store(tmp)
        import rebuild_corpus as _rb
        calls = []
        out_manifest = os.path.join(tmp, "catalog_manifest.json")

        def fake_run(name, script, args):
            calls.append(name)
            if name == "scan":
                out = args[args.index("--out") + 1]
                rows = [{"path": os.path.join(root_a, f"s{i}.als"),
                         "name": f"s{i}", "error": None} for i in range(10)]
                with open(out, "w") as fh:
                    json.dump(rows, fh)
            if name == "build_index":
                with open(os.path.join(tmp, "phrase_index.json"), "w") as fh:
                    json.dump({"meta": {}, "count": 100, "phrases": []}, fh)
            return 0

        rc = _rb.main(["--store", tmp, "--roots", root_a,
                       "--manifest", out_manifest, "--commit"],
                      run_stage=fake_run)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["scan", "materialize", "harmony", "structure",
                                 "style_profile", "build_index", "motifs",
                                 "directions"])

    def test_commit_migrates_stored_positional_ids(self):
        import json
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        root_a = self._store(tmp)
        map_path = os.path.join(tmp, "phrase_id_map.json")
        with open(map_path, "w") as fh:
            json.dump({"map": {"old|c0|w0": "p-right"}, "ambiguous": {}}, fh)
        ratings = os.path.join(tmp, "ratings.json")
        ledger = os.path.join(tmp, "session_ledger.json")
        with open(ratings, "w") as fh:
            json.dump({"ratings": [{"phrase_id": "old|c0|w0"}]}, fh)
        with open(ledger, "w") as fh:
            json.dump({"sessions": [{"entries": [
                {"source_phrase_id": "old|c0|w0"}]}]}, fh)
        import rebuild_corpus as _rb

        def fake_run(name, script, args):
            if name == "scan":
                with open(args[args.index("--out") + 1], "w") as fh:
                    json.dump([{"path": os.path.join(root_a, f"s{i}.als"),
                                "name": f"s{i}", "error": None}
                               for i in range(10)], fh)
            return 0

        rc = _rb.main(["--store", tmp, "--roots", root_a,
                       "--manifest", os.path.join(tmp, "catalog_manifest.json"),
                       "--commit", "--allow-phrase-shrink"], run_stage=fake_run)
        self.assertEqual(rc, 0)
        with open(ratings) as fh:
            self.assertEqual(json.load(fh)["ratings"][0]["phrase_id"], "p-right")
        with open(ledger) as fh:
            self.assertEqual(json.load(fh)["sessions"][0]["entries"][0]
                             ["source_phrase_id"], "p-right")


    def test_map_first_resolution_ambiguity_and_cumulative_entries(self):
        import json
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import phrase_lookup, write_id_map
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "phrase_id_map.json")
        with open(path, "w") as fh:
            json.dump({"map": {"first|c1|w0": "p-right"},
                       "ambiguous": {"amb|c0|w0": ["p1", "p2"]}}, fh)
        right = {"pid": "p-right", "id": "p-right", "alias": "new|c0|w0",
                 "notes": []}
        wrong = {"pid": "p-wrong", "id": "p-wrong", "alias": "first|c1|w0",
                 "notes": []}
        lookup = phrase_lookup([wrong, right], id_map={
            "map": {"first|c1|w0": "p-right"},
            "ambiguous": {"amb|c0|w0": ["p1", "p2"]}})
        self.assertIs(lookup["first|c1|w0"], right)
        self.assertNotIn("amb|c0|w0", lookup)
        notes = [{"pitch": 60, "start": 0, "duration": 1}]
        old = [{"id": "second|c0|w0", "alias": "second|c0|w0",
                "role": "lead", "notes": notes}]
        new = [{"pid": __import__("corpus_keys").phrase_pid(notes, role="lead")}]
        write_id_map(old, new, path)
        with open(path) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["map"]["first|c1|w0"], "p-right")
        self.assertIn("second|c0|w0", saved["map"])
        self.assertIn("amb|c0|w0", saved["ambiguous"])

    def test_stage_raise_restores_store_byte_identical(self):
        # Item 9: a raising stage restores the store byte-identical (single
        # files AND projects/) and the original error propagates.
        import json
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        root_a = self._store(tmp)
        projects = os.path.join(tmp, "projects")
        os.makedirs(projects)
        for name in ("_index.json", "p1.json"):
            with open(os.path.join(projects, name), "w") as fh:
                json.dump({"seed": name}, fh)
        import rebuild_corpus as _rb
        out_manifest = os.path.join(tmp, "catalog_manifest.json")

        def boom_run(name, script, args):
            if name == "scan":
                out = args[args.index("--out") + 1]
                rows = [{"path": os.path.join(root_a, f"s{i}.als"),
                         "name": f"s{i}", "error": None} for i in range(10)]
                with open(out, "w") as fh:
                    json.dump(rows, fh)
                return 0
            if name == "materialize":
                with open(os.path.join(tmp, "phrase_index.json"), "w") as fh:
                    fh.write("HALF-WRITTEN")
                with open(os.path.join(projects, "p1.json"), "w") as fh:
                    fh.write("HALF-WRITTEN")
                raise RuntimeError("materialize blew up")
            return 0  # pragma: no cover

        before = {}
        for dirpath, _dirnames, filenames in os.walk(tmp):
            for fn in filenames:
                path = os.path.join(dirpath, fn)
                with open(path, "rb") as fh:
                    before[os.path.relpath(path, tmp)] = fh.read()
        with self.assertRaises(RuntimeError):
            _rb.main(["--store", tmp, "--roots", root_a,
                      "--manifest", out_manifest, "--commit",
                      "--allow-project-shrink", "--allow-phrase-shrink"],
                     run_stage=boom_run)
        after = {}
        for dirpath, _dirnames, filenames in os.walk(tmp):
            for fn in filenames:
                path = os.path.join(dirpath, fn)
                with open(path, "rb") as fh:
                    after[os.path.relpath(path, tmp)] = fh.read()
        self.assertEqual(after, before)
        leftovers = [p for p in after if ".rebuild-bak" in p]
        self.assertEqual(leftovers, [])

    def test_commit_refuses_non_default_store(self):
        # Item 9: the stage scripts hardcode the default store, so a real
        # --commit elsewhere is refused before anything runs.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self._store(tmp)
        import rebuild_corpus as _rb
        out_manifest = os.path.join(tmp, "catalog_manifest.json")
        with open(out_manifest, "rb") as fh:
            manifest_before = fh.read()
        rc = _rb.main(["--store", tmp, "--commit"])  # default runner
        self.assertEqual(rc, 2)
        with open(out_manifest, "rb") as fh:
            self.assertEqual(fh.read(), manifest_before)

    def test_main_refuses_missing_root(self):
        # Item 14: a configured root that is not there refuses before any
        # stage runs (find_als would silently skip it → partial corpus).
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        root_a = self._store(tmp)
        import rebuild_corpus as _rb
        calls = []
        out_manifest = os.path.join(tmp, "catalog_manifest.json")

        def fake_run(name, script, args):
            calls.append(name)
            return 0

        rc = _rb.main(["--store", tmp, "--roots", root_a,
                       os.path.join(tmp, "unmounted-drive"),
                       "--manifest", out_manifest],
                      run_stage=fake_run)
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])

    def test_stage_failure_restores_previous_artifacts(self):
        # Item 14: a stage returning nonzero restores the single files AND
        # the materialized projects/ tree; no backups linger.
        import json
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        root_a = self._store(tmp)
        projects = os.path.join(tmp, "projects")
        os.makedirs(projects)
        with open(os.path.join(projects, "p1.json"), "w") as fh:
            json.dump({"seed": "p1"}, fh)
        import rebuild_corpus as _rb
        out_manifest = os.path.join(tmp, "catalog_manifest.json")

        def fail_run(name, script, args):
            if name == "scan":
                out = args[args.index("--out") + 1]
                rows = [{"path": os.path.join(root_a, f"s{i}.als"),
                         "name": f"s{i}", "error": None} for i in range(10)]
                with open(out, "w") as fh:
                    json.dump(rows, fh)
                return 0
            if name == "materialize":
                with open(os.path.join(tmp, "phrase_index.json"), "w") as fh:
                    fh.write("HALF-WRITTEN")
                with open(os.path.join(projects, "p1.json"), "w") as fh:
                    fh.write("HALF-WRITTEN")
                return 1
            return 0  # pragma: no cover

        def _snapshot():
            snap = {}
            for dirpath, _dirnames, filenames in os.walk(tmp):
                for fn in filenames:
                    path = os.path.join(dirpath, fn)
                    with open(path, "rb") as fh:
                        snap[os.path.relpath(path, tmp)] = fh.read()
            return snap

        before = _snapshot()
        rc = _rb.main(["--store", tmp, "--roots", root_a,
                       "--manifest", out_manifest, "--commit",
                       "--allow-project-shrink", "--allow-phrase-shrink"],
                      run_stage=fail_run)
        self.assertEqual(rc, 1)
        self.assertEqual(_snapshot(), before)

    def test_stage_failure_preserves_concurrent_drop(self):
        import json
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        root_a = self._store(tmp)
        ratings = os.path.join(tmp, "ratings.json")
        with open(ratings, "w") as fh:
            json.dump({"ratings": []}, fh)
        import rebuild_corpus as _rb

        def stage(name, script, args):
            if name == "scan":
                with open(args[args.index("--out") + 1], "w") as fh:
                    json.dump([{"path": os.path.join(root_a, f"s{i}.als"),
                                "name": f"s{i}", "error": None}
                               for i in range(10)], fh)
            elif name == "materialize":
                with open(ratings, "w") as fh:
                    json.dump({"ratings": [{"phrase_id": "p-drop",
                                             "rating": "drop"}]}, fh)
            elif name == "harmony":
                return 1
            return 0

        rc = _rb.main(["--store", tmp, "--roots", root_a,
                       "--manifest", os.path.join(tmp, "catalog_manifest.json"),
                       "--commit", "--allow-project-shrink", "--allow-phrase-shrink"],
                      run_stage=stage)
        self.assertEqual(rc, 1)
        with open(ratings) as fh:
            self.assertEqual(json.load(fh)["ratings"][0]["phrase_id"], "p-drop")

    def test_shrink_refusal_removes_new_id_map(self):
        import json
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        root_a = self._store(tmp)
        map_path = os.path.join(tmp, "phrase_id_map.json")
        import rebuild_corpus as _rb

        def stage(name, script, args):
            if name == "scan":
                with open(args[args.index("--out") + 1], "w") as fh:
                    json.dump([{"path": os.path.join(root_a, f"s{i}.als"),
                                "name": f"s{i}", "error": None}
                               for i in range(10)], fh)
            elif name == "build_index":
                with open(map_path, "w") as fh:
                    json.dump({"map": {}, "ambiguous": {}}, fh)
                with open(os.path.join(tmp, "phrase_index.json"), "w") as fh:
                    json.dump({"count": 1, "phrases": [], "meta": {}}, fh)
            return 0

        rc = _rb.main(["--store", tmp, "--roots", root_a,
                       "--manifest", os.path.join(tmp, "catalog_manifest.json"),
                       "--commit", "--allow-project-shrink"], run_stage=stage)
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(map_path))

    def test_build_meta_stamps_manifest_time(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from build_index import build_meta
        meta = build_meta(3, manifest_time="2026-09-01T00:00:00+00:00")
        self.assertEqual(meta["corpus"]["manifest_time"], "2026-09-01T00:00:00+00:00")


class TestCorpusStaleness(unittest.TestCase):
    """Item 6: session_report notes a stale corpus and an unindexed open Set."""

    def _store(self, tmp, built_at, manifest_paths=()):
        import json
        with open(os.path.join(tmp, "phrase_index.json"), "w") as fh:
            json.dump({"meta": {"built_at": built_at,
                                "corpus": {"n_projects": 1}},
                       "count": 0, "phrases": []}, fh)
        with open(os.path.join(tmp, "catalog_manifest.json"), "w") as fh:
            json.dump([{"path": p, "name": "s", "error": None}
                       for p in manifest_paths], fh)

    def test_old_corpus_is_noted(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import corpus_staleness
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self._store(tmp, "2026-01-01T00:00:00+00:00")
        notes = corpus_staleness(tmp, now="2026-09-25T00:00:00+00:00")
        self.assertTrue(any("6 weeks" in n for n in notes))

    def test_fresh_corpus_is_silent(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import corpus_staleness
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self._store(tmp, "2026-09-01T00:00:00+00:00",
                     manifest_paths=["/music/open.als"])
        self.assertEqual(corpus_staleness(tmp, current_set_path="/music/open.als",
                                          now="2026-09-25T00:00:00+00:00"), [])

    def test_open_set_missing_from_manifest_is_noted(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import corpus_staleness
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self._store(tmp, "2026-09-01T00:00:00+00:00",
                     manifest_paths=["/music/other.als"])
        notes = corpus_staleness(tmp, current_set_path="/music/open.als",
                                 now="2026-09-25T00:00:00+00:00")
        self.assertTrue(any("not in" in n for n in notes))

    def test_staleness_prefers_manifest_time(self):
        # Item 13: age comes from the catalog scan the build saw; a fresh
        # build over an old scan still reads stale.
        import json
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import corpus_staleness
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "phrase_index.json"), "w") as fh:
            json.dump({"meta": {"built_at": "2026-09-20T00:00:00+00:00",
                                "corpus": {"n_projects": 1,
                                           "manifest_time":
                                           "2026-01-01T00:00:00+00:00"}},
                       "count": 0, "phrases": []}, fh)
        notes = corpus_staleness(tmp, now="2026-09-25T00:00:00+00:00")
        self.assertTrue(any("6 weeks" in n for n in notes))

    def test_staleness_reads_index_once_per_mtime(self):
        # Item 13: repeat calls don't re-parse the index while its mtime
        # holds (the real file is tens of MB).
        from unittest import mock
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        import corpus_keys as _ck
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self._store(tmp, "2026-01-01T00:00:00+00:00")
        orig = _ck._read_index_meta
        reads = []

        def counting(path):
            reads.append(path)
            return orig(path)

        with mock.patch.object(_ck, "_read_index_meta",
                               side_effect=counting):
            _ck.corpus_staleness(tmp, now="2026-09-25T00:00:00+00:00")
            _ck.corpus_staleness(tmp, now="2026-09-25T00:00:00+00:00")
        self.assertEqual(len(reads), 1)

    def test_missing_corpus_files_fail_silent(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import corpus_staleness
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertEqual(corpus_staleness(tmp), [])

    def test_session_report_carries_corpus_advice(self):
        from unittest import mock
        from mcp_server import ledger, server, taste_bridge
        with mock.patch.object(ledger, "summary", return_value={}), \
                mock.patch.object(ledger, "load", return_value={}), \
                mock.patch.object(server, "_store_disk_state", return_value={}), \
                mock.patch.object(taste_bridge, "rejected_phrases", return_value=[]), \
                mock.patch("corpus_keys.corpus_staleness",
                           return_value=["corpus built 90 days ago"]) as stale:
            out = server.session_report()
        self.assertTrue(stale.called)
        self.assertIn("corpus built 90 days ago", out.get("advice", []))


class TestPidContentScheme(unittest.TestCase):
    """Fix item 1: pid = digest(role + canonical notes) only, unique by
    construction after dedupe; the duplicate-pid check runs after dedupe."""

    def _notes(self, shift=0):
        return [{"pitch": 64 + (i % 3), "start": float(i + shift),
                 "duration": 0.5, "velocity": 100} for i in range(6)]

    def test_pid_ignores_project_track_clip_window(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import phrase_pid
        a = phrase_pid(self._notes(), role="lead")
        # Same musical phrase wherever he played it: same pid regardless of
        # project, track, clip or window identity inputs (which no longer exist).
        self.assertTrue(a.startswith("p") and len(a) == 13)
        self.assertEqual(a, phrase_pid(self._notes(), role="lead"))
        self.assertNotEqual(a, phrase_pid(self._notes(), role="bass"))
        self.assertNotEqual(a, phrase_pid(self._notes(shift=5), role="lead"))

    def test_same_notes_same_name_clips_fold_instead_of_raising(self):
        # Two same-notes clips on one track (today the pre-dedupe
        # duplicate-pid check raises ValueError — 59/191 real projects hit
        # it — because the pid carries the clip name).
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from build_index import iter_phrases
        from corpus_keys import check_duplicate_pids, dedupe_phrases
        notes = [{"pitch": 64, "start": float(t), "duration": 0.5,
                  "velocity": 100, "enabled": True} for t in range(8)]
        data = {
            "path": "/tmp/dup.als", "tempo": 120.0,
            "clips": [
                {"name": "take", "track": "Lead", "is_drum": False,
                 "clip_source": "session", "is_sidekick": False,
                 "loop_start": None, "loop_end": None, "loop_on": True,
                 "audible_start": None, "audible_end": None,
                 "notes": notes},
                {"name": "take2", "track": "Lead", "is_drum": False,
                 "clip_source": "session", "is_sidekick": False,
                 "loop_start": None, "loop_end": None, "loop_on": True,
                 "audible_start": None, "audible_end": None,
                 "notes": [dict(n) for n in notes]},
            ],
        }
        raw = list(iter_phrases(data))
        self.assertEqual(len(raw), 2)
        self.assertEqual(raw[0]["pid"], raw[1]["pid"])
        deduped, _ = dedupe_phrases(raw)
        check_duplicate_pids(deduped)  # must not raise after dedupe
        self.assertEqual(len(deduped), 1)
        self.assertEqual(len(deduped[0]["provenance"]), 2)


class TestPidAsRecordId(unittest.TestCase):
    """Fix item 2: the record id IS the pid; alias = survivor's positional id;
    every provenance entry keeps its alias; project stays on the record. Id
    parsers use the record's project, falling back to rsplit for legacy ids."""

    def _data(self):
        notes = [{"pitch": 64, "start": float(t), "duration": 0.5,
                  "velocity": 100, "enabled": True} for t in range(8)]
        return {
            "path": "/tmp/survivor.als", "tempo": 120.0,
            "clips": [
                {"name": "take", "track": "Lead", "is_drum": False,
                 "clip_source": "session", "is_sidekick": False,
                 "loop_start": None, "loop_end": None, "loop_on": True,
                 "audible_start": None, "audible_end": None,
                 "notes": notes},
                {"name": "take2", "track": "Lead", "is_drum": False,
                 "clip_source": "session", "is_sidekick": False,
                 "loop_start": None, "loop_end": None, "loop_on": True,
                 "audible_start": None, "audible_end": None,
                 "notes": [dict(n) for n in notes]},
            ],
        }

    def test_record_id_is_pid_with_survivor_alias(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from build_index import iter_phrases
        from corpus_keys import dedupe_phrases
        raw = list(iter_phrases(self._data()))
        self.assertEqual(len(raw), 2)
        for p in raw:
            self.assertEqual(p["id"], p["pid"])
            self.assertIn("|c", p["alias"])
            self.assertEqual(p["project"], "survivor")
        deduped, _ = dedupe_phrases(raw)
        self.assertEqual(len(deduped), 1)
        keep = deduped[0]
        self.assertEqual(keep["id"], keep["pid"])
        self.assertEqual(keep["alias"], raw[0]["alias"])  # survivor's positional id
        self.assertEqual(keep["project"], "survivor")
        self.assertEqual({e["alias"] for e in keep["provenance"]},
                         {raw[0]["alias"], raw[1]["alias"]})

    def test_booth_gold_sources_use_record_project(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from ingest_booth_votes import validate_booth_gold_sources
        # A pid carries no project segment: the blocked project must come
        # from the record, not from parsing the id.
        by_id = {"pabcdef012345": {"pid": "pabcdef012345",
                                  "id": "pabcdef012345",
                                  "alias": "BLOCKED|c0|w0",
                                  "project": "BLOCKED"}}
        with self.assertRaises(ValueError):
            validate_booth_gold_sources(["pabcdef012345"], by_id,
                                        lambda p: p == "BLOCKED")

    def test_booth_plan_uses_record_project(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from ingest_booth_votes import plan_booth_ingest
        by_id = {"pabcdef012345": {"pid": "pabcdef012345",
                                  "id": "pabcdef012345",
                                  "alias": "BLOCKED|c0|w0",
                                  "project": "BLOCKED"}}
        manifest = [{"pair_id": 1, "A_phrase_id": "pabcdef012345",
                     "B_phrase_id": "pokother00000",
                     "winner": "A", "selection_mode": "random"}]
        plan = plan_booth_ingest(manifest, None, lambda p: p == "BLOCKED",
                                 {"pabcdef012345", "pokother00000"},
                                 phrase_by_id=by_id)
        self.assertEqual(plan["dropped_blocked"], [1])

    def test_audition_plan_uses_record_project(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from ingest_audition_votes import plan_ingest
        by_id = {"pabcdef012345": {"pid": "pabcdef012345",
                                  "id": "pabcdef012345",
                                  "alias": "BLOCKED|c0|w0",
                                  "project": "BLOCKED"}}
        manifest = [{"blind_id": 1, "phrase_id": "pabcdef012345",
                     "round": "r9"}]
        plan = plan_ingest(manifest, {1: "gold"}, lambda p: p == "BLOCKED",
                           {"pabcdef012345"}, "r9", phrase_by_id=by_id)
        self.assertEqual(plan["dropped"], ["pabcdef012345"])
        self.assertEqual(plan["rates"], [])


@requires_catalog
class TestIdMapFromPreviousBuild(unittest.TestCase):
    """Fix item 3: the id map is built from the PREVIOUS phrase_index vs the
    new build (old alias -> pid(role, old notes) when that pid survives);
    aliases shared by old records with different content are excluded and
    listed under ambiguous."""

    def _rec(self, alias, base, role="lead"):
        notes = [{"pitch": base + (i % 3), "start": float(i),
                  "duration": 0.5, "velocity": 100} for i in range(6)]
        return {"id": alias, "alias": alias,
                "project": "song", "role": role, "notes": notes}

    def test_renumbered_alias_migrates_to_right_phrase(self):
        import json
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import phrase_pid, write_id_map
        # Old build: two phrases. New build after inserting a clip in front:
        # same note-sets, shifted aliases, plus the new clip's phrase.
        old = [self._rec("song|c0|w0", 64), self._rec("song|c1|w0", 70)]
        new_notes_a = old[0]["notes"]
        new_notes_b = old[1]["notes"]
        new = [dict(self._rec("song|c1|w0", 64), notes=new_notes_a),
               dict(self._rec("song|c2|w0", 70), notes=new_notes_b)]
        for r in new:
            r["pid"] = r["id"] = phrase_pid(r["notes"], role=r["role"])
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        result = write_id_map(old, new, os.path.join(tmp, "phrase_id_map.json"))
        # The gold stored under the old alias follows the CONTENT, not the slot.
        self.assertEqual(result["map"]["song|c0|w0"],
                         phrase_pid(new_notes_a, role="lead"))
        self.assertEqual(result["map"]["song|c1|w0"],
                         phrase_pid(new_notes_b, role="lead"))
        self.assertEqual(result["ambiguous"], {})
        with open(os.path.join(tmp, "phrase_id_map.json")) as fh:
            self.assertEqual(json.load(fh), {"map": result["map"],
                                              "ambiguous": {}, "gone": {}})

    def test_ambiguous_alias_excluded_and_listed(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import phrase_pid, write_id_map
        old = [self._rec("song|c0|w0", 64), self._rec("song|c0|w0", 70)]
        new = [dict(self._rec("song|c0|w0", 64), notes=old[0]["notes"])]
        for r in new:
            r["pid"] = r["id"] = phrase_pid(r["notes"], role=r["role"])
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        result = write_id_map(old, new, os.path.join(tmp, "phrase_id_map.json"))
        self.assertNotIn("song|c0|w0", result["map"])
        self.assertEqual(sorted(result["ambiguous"]["song|c0|w0"]),
                         sorted([phrase_pid(old[0]["notes"], role="lead"),
                                 phrase_pid(old[1]["notes"], role="lead")]))

    def test_vanished_content_not_mapped(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import phrase_pid, write_id_map
        old = [self._rec("song|c9|w0", 80)]
        new = [dict(self._rec("song|c0|w0", 64), notes=self._rec("x", 64)["notes"])]
        for r in new:
            r["pid"] = r["id"] = phrase_pid(r["notes"], role=r["role"])
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        result = write_id_map(old, new, os.path.join(tmp, "phrase_id_map.json"))
        self.assertEqual(result["map"], {})
        self.assertEqual(result["ambiguous"], {})
        from corpus_keys import expand_ids, phrase_lookup
        gone_pid = phrase_pid(old[0]["notes"], role="lead")
        self.assertEqual(result["gone"]["song|c9|w0"], gone_pid)
        current = {"pid": "p-current", "id": "p-current",
                   "alias": "song|c9|w0", "notes": new[0]["notes"]}
        lookup = phrase_lookup([current], id_map=result)
        self.assertNotIn("song|c9|w0", lookup)
        self.assertNotIn("song|c9|w0", expand_ids(["song|c9|w0"], [current],
                                                   id_map=result))

    def test_gone_alias_absent_from_lookup_but_suppressed_during_expansion(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import expand_ids, phrase_lookup
        phrase = {"pid": "p-current", "id": "p-current", "alias": "song|c0|w0"}
        id_map = {"map": {"song|c9|w0": "p-vanished"}, "ambiguous": {},
                  "gone": {"song|c9|w0": "p-vanished"}}
        self.assertNotIn("song|c9|w0", phrase_lookup([phrase], id_map=id_map))
        self.assertNotIn("song|c9|w0", expand_ids(["song|c9|w0"], [phrase],
                                                  id_map=id_map))

    def test_gone_alias_ratings_are_safe_for_consumers_and_answer(self):
        from unittest import mock
        from mcp_server import core, taste_bridge
        import corpus_keys
        from build_index import phrase_vector

        gone = "song|c9|w0"
        notes = [{"pitch": 60 + (i % 3), "start": float(i),
                  "duration": 0.5, "velocity": 90} for i in range(8)]
        phrase = {"id": "p-current", "pid": "p-current", "alias": "song|c0|w0",
                  "role": "lead", "kind": "harmonic", "project": "song",
                  "clip_source": "session", "tempo": 120.0, "notes": notes,
                  "vec": phrase_vector(notes)}
        id_map = {"map": {gone: "p-vanished"}, "ambiguous": {},
                  "gone": {gone: "p-vanished"}}
        ratings = [
            {"id": 1, "phrase_id": gone, "rating": 1, "label": "gold",
             "role": "lead", "notes": notes},
            {"id": 2, "phrase_id": gone, "rating": -1, "label": "drop",
             "notes": notes},
        ]
        with mock.patch.object(corpus_keys, "_read_default_id_map", return_value=id_map), \
                mock.patch.object(core, "_phrases", return_value=[phrase]), \
                mock.patch.object(taste_bridge, "_phrases", return_value=[phrase]), \
                mock.patch.object(core, "get_gold_phrases", return_value=[ratings[0]]), \
                mock.patch.object(taste_bridge, "_load_ratings_file",
                                  return_value={"ratings": ratings}), \
                mock.patch.object(core, "_answer_bank",
                                  return_value=([phrase], 0, 0)), \
                mock.patch.object(core, "search", return_value=[phrase]):
            gold_records = core._gold_bank_records()
            self.assertEqual(len(gold_records), 1)
            self.assertEqual(gold_records[0]["id"], gone)
            self.assertEqual(len(taste_bridge.rejected_phrases()), 1)
            self.assertEqual(len(taste_bridge._rating_vectors(ratings, "harmonic")[0]), 1)
            result = core.answer_my_loop(notes, key="C major", use_ears=False)
        self.assertTrue(result.get("no_fit"), result)
        self.assertEqual(result["rejects"]["pool_after"], 0)

    def test_cumulative_map_keeps_legacy_alias_after_noop_rebuild(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import phrase_pid, write_id_map
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "phrase_id_map.json")
        notes_b = self._rec("b", 70)["notes"]
        pid_b = phrase_pid(notes_b, role="lead")
        legacy = [{"id": "s|c1|w0", "role": "lead", "notes": notes_b}]
        write_id_map(legacy, [{"pid": pid_b}], path)
        # A modern index record is not a fresh legacy alias observation.
        modern = [{"id": pid_b, "pid": pid_b, "alias": "s|c0|w0",
                   "role": "lead", "notes": notes_b}]
        result = write_id_map(modern, [dict(modern[0])], path)
        self.assertEqual(result["map"]["s|c1|w0"], pid_b)
        self.assertEqual(result["ambiguous"], {})

    def test_noop_rebuild_preserves_ambiguous_candidates_and_file_bytes(self):
        import json
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import write_id_map
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "phrase_id_map.json")
        original = ('{\n  "map": {"old": "p1"},\n'
                    '  "ambiguous": {"collision": ["p2", "p3"]},\n'
                    '  "gone": {}\n}\n')
        with open(path, "w") as fh:
            fh.write(original)

        result = write_id_map([], [{"pid": "p1"}], path)

        self.assertEqual(result["ambiguous"], {"collision": ["p2", "p3"]})
        with open(path) as fh:
            self.assertEqual(fh.read(), original)


class TestProvenanceAliasResolution(unittest.TestCase):
    """Fix item 4: phrase_lookup/expand_ids index pid, id, alias AND every
    provenance alias (first wins, deterministic order). A rating stored under
    a folded duplicate's alias resolves to the kept record."""

    def test_folded_alias_resolves(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import dedupe_phrases, expand_ids, ids_match, phrase_lookup
        base = {"role": "lead", "kind": "harmonic", "window": 0,
                "notes": [{"pitch": 60 + i, "start": float(i),
                           "duration": 0.5, "velocity": 100} for i in range(4)]}
        raw = [dict(base, pid="p0", id="p0", alias="s0|c0|w0",
                    project="s0", track="Lead", clip="take"),
               dict(base, pid="p0", id="p0", alias="s1|c0|w0",
                    project="s1", track="Lead", clip="take")]
        deduped, _ = dedupe_phrases(raw)
        self.assertEqual(len(deduped), 1)
        keep = deduped[0]
        idx = phrase_lookup(deduped)
        # The folded duplicate's alias resolves to the kept record...
        self.assertIs(idx["s1|c0|w0"], keep)
        self.assertIs(idx["s0|c0|w0"], keep)
        self.assertIs(idx["p0"], keep)
        self.assertTrue(ids_match("s1|c0|w0", keep))
        # ...so a rating stored under it expands to the pid.
        self.assertIn("p0", expand_ids(["s1|c0|w0"], deduped))

    def test_first_wins_in_index_order(self):
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import phrase_lookup
        a = {"pid": "pa", "id": "pa", "alias": "shared|c0|w0",
             "provenance": [{"alias": "shared|c0|w0"}]}
        b = {"pid": "pb", "id": "pb", "alias": "shared|c0|w0",
             "provenance": [{"alias": "shared|c0|w0"}]}
        idx = phrase_lookup([a, b])
        self.assertIs(idx["shared|c0|w0"], a)
        self.assertIs(idx["pb"], b)


class TestSourceIdResolution(unittest.TestCase):
    """Fix item 5: gears (recall + judge) and develop_session resolve source
    ids through phrase_lookup / the id map — a folded duplicate's alias names
    the kept phrase. migrate_stored_ids also rewrites develop_session.json."""

    def _kept(self):
        notes = [{"pitch": 64 + (i % 3), "start": float(i),
                  "duration": 0.5, "velocity": 100} for i in range(6)]
        return {"pid": "pkept0000001", "id": "pkept0000001",
                "alias": "song|c0|w0", "project": "song", "track": "Lead",
                "clip": "take", "role": "lead", "kind": "harmonic",
                "clip_source": "session", "tempo": 120.0,
                "n_notes": len(notes), "notes": notes,
                "vec": [0.0] * 44,
                "provenance": [{"alias": "song|c0|w0", "project": "song"},
                               {"alias": "song|c1|w0", "project": "song"}]}

    def test_gear1_verbatim_recall_through_folded_alias(self):
        from unittest import mock
        sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
        from mcp_server import gears as _gears
        kept = self._kept()
        folded = "song|c1|w0"
        seed = [{"pitch": 60 + i, "start": float(i) * 0.5,
                 "duration": 0.5, "velocity": 90} for i in range(4)]
        served = {"source_phrase_ids": [folded], "key": "C minor", "tempo": 120.0}
        with mock.patch.object(_gears.core, "answer_my_loop",
                               return_value=dict(served)), \
                mock.patch.object(_gears.core, "_phrases",
                                  return_value=[kept]):
            ans = _gears.answer_with_gear(seed, gear=1, key="C minor",
                                          use_ears=False)
        # The recall is the retrieved phrase VERBATIM, even though the served
        # id was the folded alias rather than the record id.
        self.assertEqual(ans.get("transforms_applied"), ["verbatim"])
        raw = sorted(kept["notes"], key=lambda n: (n["start"], n["pitch"]))
        t0 = raw[0]["start"]
        want = [(n["pitch"], round(n["start"] - t0, 4)) for n in raw]
        got = [(n["pitch"], round(n["start_beats"], 4))
               for n in ans["answer_notes"]]
        self.assertEqual(got, want)

    def test_judge_answer_resolves_folded_alias(self):
        from unittest import mock
        sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
        from mcp_server import develop_session as _ds
        from mcp_server import ears as _ears
        from mcp_server import gears as _gears
        from mcp_server import ledger as _ledger
        kept = self._kept()
        folded = "song|c1|w0"
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        ratings_path = os.path.join(tmp, "ratings.json")
        old_ratings = _gears.core._ratings_path()
        _gears.core.set_ratings_path(ratings_path)
        self.addCleanup(_gears.core.set_ratings_path, old_ratings)
        with mock.patch.object(_gears.core, "_phrases", return_value=[kept]):
            res = _gears.judge_answer(
                "drop", source_phrase_id=folded,
                seed_notes=[{"pitch": 64, "start": 0.0,
                             "duration": 0.5, "velocity": 90}],
                key="C minor", reason="test",
                ears_session_path=os.path.join(tmp, "ears.json"),
                develop_session_path=os.path.join(tmp, "develop.json"),
                ledger_path=os.path.join(tmp, "ledger.json"))
        self.assertEqual(res["verdict"], "drop")

    def test_develop_hold_matches_across_migration(self):
        from unittest import mock
        sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
        from mcp_server import develop_session as _ds
        from mcp_server import ledger as _ledger
        import json
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        map_path = os.path.join(tmp, "phrase_id_map.json")
        with open(map_path, "w") as fh:
            json.dump({"song|c0|w0": "pkept0000001"}, fh)
        state_path = os.path.join(tmp, "develop_session.json")
        old_cache = _ledger._ID_MAP_CACHE
        _ledger._ID_MAP_CACHE = None
        try:
            with mock.patch.object(_ledger, "ID_MAP_PATH", map_path):
                _ds.set_sticky(True, path=state_path)
                seed = [{"pitch": 64, "start": 0.0, "duration": 0.5,
                         "velocity": 90}]
                _ds.record_keep("song|c0|w0", seed, "C minor", path=state_path)
                # Same phrase under its migrated pid extends the hold...
                state = _ds.record_keep("pkept0000001", seed, "C minor",
                                        path=state_path)
                self.assertEqual(state["hold"]["pass"], 1)
                # ...while a genuinely different phrase restarts it.
                state = _ds.record_keep("pother0000002", seed, "C minor",
                                        path=state_path)
                self.assertEqual(state["hold"]["pass"], 0)
                self.assertEqual(state["hold"]["source_phrase_id"],
                                 "pother0000002")
        finally:
            _ledger._ID_MAP_CACHE = old_cache

    def test_migrate_rewrites_develop_session(self):
        import json
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import migrate_stored_ids
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        ratings_path = os.path.join(tmp, "ratings.json")
        ledger_path = os.path.join(tmp, "session_ledger.json")
        dev_path = os.path.join(tmp, "develop_session.json")
        with open(ratings_path, "w") as fh:
            json.dump({"ratings": []}, fh)
        with open(ledger_path, "w") as fh:
            json.dump({"sessions": []}, fh)
        with open(dev_path, "w") as fh:
            json.dump({"sticky": True,
                       "hold": {"source_phrase_id": "song|c0|w0",
                                "seed_notes": [], "key": "C minor",
                                "pass": 2, "move_cursor": 0}}, fh)
        report = migrate_stored_ids(ratings_path, ledger_path,
                                    {"song|c0|w0": "pkept0000001"},
                                    dry_run=False,
                                    develop_session_path=dev_path)
        self.assertEqual(report["develop_session_rewritten"], 1)
        with open(dev_path) as fh:
            hold = json.load(fh)["hold"]
        self.assertEqual(hold["source_phrase_id"], "pkept0000001")
        self.assertEqual(hold["pass"], 2)  # only the id moves

    def test_migration_restores_backups_on_late_failure(self):
        # Item 10: ratings migrates, then the ledger write fails → ratings
        # comes back byte-identical from the start-of-run backup; the error
        # propagates; no half-written file or temp survives.
        import json
        from unittest import mock
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        import corpus_keys as _ck
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        ratings_path = os.path.join(tmp, "ratings.json")
        ledger_path = os.path.join(tmp, "session_ledger.json")
        with open(ratings_path, "w") as fh:
            json.dump({"ratings": [{"phrase_id": "s|c0|w0",
                                    "rating": 1}]}, fh)
        with open(ledger_path, "w") as fh:
            json.dump({"sessions": [{"entries": [
                {"source_phrase_id": "s|c0|w0"}]}]}, fh)
        with open(ratings_path, "rb") as fh:
            ratings_before = fh.read()
        with open(ledger_path, "rb") as fh:
            ledger_before = fh.read()
        orig_atomic = _ck._write_json_atomic
        calls = []

        def flaky_atomic(path, data):
            calls.append(path)
            if len(calls) > 1:
                raise RuntimeError("disk blew up mid-migration")
            return orig_atomic(path, data)

        with mock.patch.object(_ck, "_write_json_atomic",
                               side_effect=flaky_atomic):
            with self.assertRaises(RuntimeError):
                _ck.migrate_stored_ids(ratings_path, ledger_path,
                                       {"s|c0|w0": "p111"}, dry_run=False)
        with open(ratings_path, "rb") as fh:  # restored byte-identical
            self.assertEqual(fh.read(), ratings_before)
        with open(ledger_path, "rb") as fh:
            self.assertEqual(fh.read(), ledger_before)
        leftovers = [fn for fn in os.listdir(tmp)
                     if fn.endswith(".tmp") or ".rebuild-bak" in fn]
        self.assertEqual(leftovers, [])

    def test_migration_holds_ledger_lock_on_write(self):
        # Item 10: while the ledger lands, its .lock file is LOCK_EX-held
        # (same convention as ledger._lock) — a concurrent writer blocks.
        import fcntl
        import json
        from unittest import mock
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        import corpus_keys as _ck
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        ratings_path = os.path.join(tmp, "ratings.json")
        ledger_path = os.path.join(tmp, "session_ledger.json")
        with open(ratings_path, "w") as fh:
            json.dump({"ratings": []}, fh)
        with open(ledger_path, "w") as fh:
            json.dump({"sessions": [{"entries": [
                {"source_phrase_id": "s|c0|w0"}]}]}, fh)
        orig_atomic = _ck._write_json_atomic
        held = []

        def spy_atomic(path, data):
            if path == ledger_path:
                with open(ledger_path + ".lock", "a+") as fh:
                    try:
                        fcntl.flock(fh.fileno(),
                                    fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        held.append(True)  # someone else holds it: us
                    else:
                        held.append(False)
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return orig_atomic(path, data)

        with mock.patch.object(_ck, "_write_json_atomic",
                               side_effect=spy_atomic):
            report = _ck.migrate_stored_ids(ratings_path, ledger_path,
                                            {"s|c0|w0": "p111"},
                                            dry_run=False)
        self.assertEqual(report["ledger_rewritten"], 1)
        self.assertEqual(held, [True])

    def test_migration_holds_ratings_and_develop_locks_and_keeps_old_backups(self):
        import fcntl
        import json
        from unittest import mock
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        import corpus_keys as _ck
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        ratings = os.path.join(tmp, "ratings.json")
        ledger = os.path.join(tmp, "ledger.json")
        develop = os.path.join(tmp, "develop_session.json")
        with open(ratings, "w") as fh:
            json.dump({"ratings": [{"phrase_id": "s|c0|w0"}]}, fh)
        with open(ledger, "w") as fh:
            json.dump({}, fh)
        with open(develop, "w") as fh:
            json.dump({"hold": {"source_phrase_id": "s|c0|w0"}}, fh)
        legacy_backup = ratings + ".bak"
        with open(legacy_backup, "w") as fh:
            fh.write("keep this older backup")
        orig_atomic = _ck._write_json_atomic
        observed = []

        def spy_atomic(path, data):
            if path in (ratings, develop):
                with open(path + ".lock", "a+") as lock:
                    try:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        observed.append(path)
                    else:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            return orig_atomic(path, data)

        with mock.patch.object(_ck, "_write_json_atomic", side_effect=spy_atomic):
            _ck.migrate_stored_ids(ratings, ledger, {"s|c0|w0": "p-new"},
                                   dry_run=False, develop_session_path=develop)
        self.assertEqual(set(observed), {ratings, develop})
        with open(legacy_backup) as fh:
            self.assertEqual(fh.read(), "keep this older backup")
        saved = [name for name in os.listdir(tmp)
                 if name.startswith("ratings.json.") and name.endswith(".bak")
                 and name != "ratings.json.bak"]
        self.assertEqual(len(saved), 1)


class TestLedgerPidResolution(unittest.TestCase):
    """Item 3: the judgment binder matches pids against old aliases."""

    def test_same_phrase_id_across_migration(self):
        # Item 11 deliberately keys the cache on the map file's mtime, so
        # this test drives a real map file instead of injecting the cache.
        import json
        from unittest import mock
        from mcp_server import ledger as _ledger
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        map_path = os.path.join(tmp, "phrase_id_map.json")
        with open(map_path, "w") as fh:
            json.dump({"song|c0|w0": "p111"}, fh)
        old_cache, old_mtime = _ledger._ID_MAP_CACHE, _ledger._ID_MAP_MTIME
        _ledger._ID_MAP_CACHE, _ledger._ID_MAP_MTIME = None, None
        try:
            with mock.patch.object(_ledger, "ID_MAP_PATH", map_path):
                self.assertTrue(_ledger._same_phrase_id("song|c0|w0", "p111"))
                self.assertTrue(_ledger._same_phrase_id("p111", "p111"))
                self.assertFalse(_ledger._same_phrase_id("song|c0|w0", "p222"))
                self.assertFalse(_ledger._same_phrase_id(None, "p111"))
        finally:
            _ledger._ID_MAP_CACHE, _ledger._ID_MAP_MTIME = old_cache, old_mtime


class TestPairwiseAliasResolution(unittest.TestCase):
    def test_old_booth_repeat_probe_ids_resolve_to_current_records(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import phrase_lookup
        from rank_pairwise import _past_booth_pairs
        phrases = [
            {"id": "p-win", "pid": "p-win", "alias": "song|c1|w0"},
            {"id": "p-lose", "pid": "p-lose", "alias": "song|c2|w0"},
        ]
        lookup = phrase_lookup(phrases, id_map={
            "map": {"song|c0|w0": "p-win", "song|c1|w0": "p-lose"},
            "ambiguous": {}})
        rounds = [{"reason": "booth pair repeat", "best": 0, "worst": 1,
                   "candidates": [{"id": "song|c0|w0"},
                                  {"id": "song|c1|w0"}]}]
        self.assertEqual(_past_booth_pairs(rounds, lookup),
                         [("p-win", "p-lose", 0)])

    def test_migration_rewrites_candidate_round_ids(self):
        import json
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import migrate_stored_ids
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        ratings = os.path.join(tmp, "ratings.json")
        ledger = os.path.join(tmp, "ledger.json")
        with open(ratings, "w") as fh:
            json.dump({"candidate_rounds": [{"candidates": [
                {"id": "song|c0|w0"}, {"id": "song|c1|w0"}]}]}, fh)
        with open(ledger, "w") as fh:
            json.dump({}, fh)
        migrate_stored_ids(ratings, ledger,
                           {"song|c0|w0": "p-win", "song|c1|w0": "p-lose"},
                           dry_run=False)
        with open(ratings) as fh:
            ids = [c["id"] for c in json.load(fh)["candidate_rounds"][0]["candidates"]]
        self.assertEqual(ids, ["p-win", "p-lose"])

    def test_id_map_cache_refreshes_after_rewrite(self):
        # Item 11: a fresh build's map takes effect without a cache reset —
        # the rewrite bumps the mtime, which is the cache key.
        import json
        from unittest import mock
        from mcp_server import ledger as _ledger
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        map_path = os.path.join(tmp, "phrase_id_map.json")
        with open(map_path, "w") as fh:
            json.dump({"song|c0|w0": "p111"}, fh)
        old_cache, old_mtime = _ledger._ID_MAP_CACHE, _ledger._ID_MAP_MTIME
        _ledger._ID_MAP_CACHE, _ledger._ID_MAP_MTIME = None, None
        try:
            with mock.patch.object(_ledger, "ID_MAP_PATH", map_path):
                self.assertTrue(_ledger._same_phrase_id("song|c0|w0", "p111"))
                with open(map_path, "w") as fh:
                    json.dump({"song|c0|w0": "p222"}, fh)
                bump = os.stat(map_path).st_mtime + 2.0
                os.utime(map_path, (bump, bump))
                self.assertTrue(_ledger._same_phrase_id("song|c0|w0", "p222"))
                self.assertFalse(_ledger._same_phrase_id("song|c0|w0", "p111"))
        finally:
            _ledger._ID_MAP_CACHE, _ledger._ID_MAP_MTIME = old_cache, old_mtime


if __name__ == "__main__":
    unittest.main()
