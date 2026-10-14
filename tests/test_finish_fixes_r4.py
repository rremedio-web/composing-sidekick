"""Batch 5 finish-fixes round 4 (final review) regression tests."""
import gzip
import os
import tempfile
import unittest

from ingest import parse_als
from mcp_server import finisher


def _midi_clip(start, end, loop_start, loop_end, start_relative, notes,
               loop_on=True, disabled=False, name="c"):
    """One arrangement <MidiClip> in the shape Live 11/12 saves it."""
    keytracks = "".join(
        '<KeyTrack Id="%d"><Notes><MidiNoteEvent Time="%s" Duration="%s" '
        'Velocity="100" IsEnabled="true" /></Notes><MidiKey Value="%d" /></KeyTrack>'
        % (i, time, duration, pitch) for i, (pitch, time, duration) in enumerate(notes))
    return (
        '<MidiClip Id="0" Time="%s"><CurrentStart Value="%s" /><CurrentEnd Value="%s" />'
        '<Loop><LoopStart Value="%s" /><LoopEnd Value="%s" /><StartRelative Value="%s" />'
        '<LoopOn Value="%s" /><OutMarker Value="%s" /><HiddenLoopStart Value="0" />'
        '<HiddenLoopEnd Value="%s" /></Loop><Name Value="%s" />'
        '<Disabled Value="%s" /><Notes><KeyTracks>%s</KeyTracks></Notes></MidiClip>'
        % (start, start, end, loop_start, loop_end, start_relative,
           "true" if loop_on else "false", loop_end, loop_end, name,
           "true" if disabled else "false", keytracks))


def _als(tracks):
    """A minimal gzipped Live Set: tracks = [(track name, [clip xml, ...])]."""
    body = "".join(
        '<MidiTrack Id="%d"><Name><EffectiveName Value="%s" /><UserName Value="%s" />'
        '</Name><DeviceChain><MainSequencer><ClipTimeable><ArrangerAutomation><Events>'
        '%s</Events></ArrangerAutomation></ClipTimeable></MainSequencer></DeviceChain>'
        '</MidiTrack>' % (i, name, name, "".join(clips))
        for i, (name, clips) in enumerate(tracks))
    return ('<?xml version="1.0" encoding="UTF-8"?><Ableton MajorVersion="5">'
            '<LiveSet><Tracks>%s</Tracks><MasterTrack><DeviceChain><Mixer><Tempo>'
            '<Manual Value="120" /></Tempo></Mixer></DeviceChain></MasterTrack>'
            '</LiveSet></Ableton>' % body).encode()


class _AlsCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def parse(self, tracks) -> dict:
        path = os.path.join(self.temp.name, "set.als")
        with gzip.open(path, "wb") as fh:
            fh.write(_als(tracks))
        return parse_als.to_dict(parse_als.parse_als(path))


# eight distinct pitches, one per beat of an 8-beat brace: non-periodic inside it
_BRACE_NOTES = [(60 + beat, float(beat), 0.5) for beat in range(8)]


class DisabledClipSeedTests(_AlsCase):
    def test_disabled_flag_is_parsed_and_old_records_load_enabled(self):
        project = self.parse([("Keys", [_midi_clip(0, 8, 0, 8, 0, _BRACE_NOTES,
                                                   disabled=True)])])
        self.assertIs(project["clips"][0]["disabled"], True)
        legacy = dict(project, clips=[{k: v for k, v in project["clips"][0].items()
                                       if k != "disabled"}])
        self.assertIs(parse_als.from_dict(legacy).clips[0].disabled, False)

    def test_deactivated_clip_never_seeds_the_ending(self):
        project = self.parse([
            ("Keys", [_midi_clip(0, 8, 0, 8, 0, [(60, 0.0, 0.5)])]),
            ("Muted", [_midi_clip(0, 8, 0, 8, 0, [(40, 0.0, 0.5)], disabled=True)]),
        ])
        seeds = finisher._ending_seed_notes(project, [1, 2])
        self.assertEqual([n["pitch"] for n in seeds], [60])

    def test_corpus_ingestion_still_keeps_deactivated_clips(self):
        project = self.parse([("Muted", [_midi_clip(0, 8, 0, 8, 0, [(40, 0.0, 0.5)],
                                                    disabled=True)])])
        self.assertEqual(project["clip_count"], 1)
        self.assertEqual(project["total_notes"], 1)


class StartMarkerSeedTests(_AlsCase):
    """A looped arrangement clip whose pass is longer than the brace still starts
    at its start marker (LoopStart + StartRelative), then repeats the brace."""

    def test_long_pass_seeds_from_the_start_marker_end_to_end(self):
        project = self.parse([("Keys", [_midi_clip(0, 32, 0, 8, 4, _BRACE_NOTES)])])
        clip = project["clips"][0]
        # existing audible semantics unchanged: a >= one-brace pass is the brace
        self.assertEqual((clip["audible_start"], clip["audible_end"]), (0.0, 8.0))
        self.assertIsNone(clip["audible_regions"])
        self.assertEqual(clip["start_marker"], 4.0)
        seeds = finisher._ending_seed_notes(project, [1, 8])
        # Live plays content beat (4 + t) mod 8 at arrangement beat t
        expected = [(float(t), 60 + (4 + t) % 8, 0.5) for t in range(32)]
        self.assertEqual([(n["start"], n["pitch"], n["duration"]) for n in seeds], expected)

    def test_start_marker_field_bounds(self):
        cases = (("zero", 0, True, 2.0), ("inside", 3, True, 5.0),
                 ("equal to brace", 8, True, 2.0), ("past brace", 9, True, None),
                 ("negative", -1, True, None), ("loop off", 3, False, None))
        for label, rel, loop_on, want in cases:
            with self.subTest(label):
                project = self.parse([("Keys", [_midi_clip(0, 32, 2, 10, rel,
                                                           _BRACE_NOTES, loop_on=loop_on)])])
                self.assertEqual(project["clips"][0]["start_marker"], want)

    def test_old_records_without_start_marker_keep_the_instruction(self):
        project = self.parse([("Keys", [_midi_clip(0, 32, 0, 8, 4, _BRACE_NOTES)])])
        finding = {"detector": "uncommitted_ending", "severity": "ask", "bars": [7, 8],
                   "evidence": "MIDI only"}
        for source in ("arrangement", "legacy"):
            with self.subTest(source):
                clip = {k: v for k, v in project["clips"][0].items() if k != "start_marker"}
                clip["clip_source"] = source
                old = dict(project, clips=[clip])
                self.assertIsNone(parse_als.from_dict(old).clips[0].start_marker)
                self.assertEqual(finisher._ending_seed_notes(old, [7, 8]), [])
                proposal = finisher.propose([finding], project_dict=old)[0]
                self.assertIsNone(proposal["command"])

    def test_out_of_range_start_marker_keeps_the_instruction(self):
        project = self.parse([("Keys", [_midi_clip(0, 32, 0, 8, 9, _BRACE_NOTES)])])
        self.assertIsNone(project["clips"][0]["start_marker"])
        self.assertEqual(finisher._ending_seed_notes(project, [1, 8]), [])

    def test_new_raw_fields_leave_intent_fingerprints_alone(self):
        from unittest import mock
        from mcp_server import piece_context
        decisions = os.path.join(self.temp.name, "piece_decisions.json")
        project = self.parse([("Keys", [_midi_clip(0, 32, 0, 8, 4, _BRACE_NOTES)])])
        project["path"] = "/music/Song.als"
        old = dict(project, clips=[{k: v for k, v in c.items()
                                    if k not in ("start_marker", "disabled")}
                                   for c in project["clips"]])
        with mock.patch.object(piece_context, "_decision_path", return_value=decisions):
            raised = [f["detector"] for f in finisher.whats_missing(project_dict=old)["findings"]]
            self.assertIn("uncommitted_ending", raised)
            finisher.whats_missing(project_dict=old, intended_findings=["uncommitted_ending"])
            again = finisher.whats_missing(project_dict=project)
        self.assertNotIn("uncommitted_ending", [f["detector"] for f in again["findings"]])


if __name__ == "__main__":
    unittest.main()
