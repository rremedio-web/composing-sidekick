"""Unit tests for ingest/parse_als.py."""
from __future__ import annotations

import math
import unittest
import xml.etree.ElementTree as ET

import sys
import os

# Insert repo root to allow imports from ingest/
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ingest.parse_als import Note, from_dict, parse_notes, to_dict


class TestParseNotes(unittest.TestCase):
    """Test parse_notes() directly with in-code XML."""

    def _build_clip_xml(self, key_tracks_xml: str) -> ET.Element:
        """Helper to build a minimal clip element with KeyTracks."""
        xml = f"""<Clip>
            <Name>TestClip</Name>
            <CurrentStart>0.0</CurrentStart>
            <CurrentEnd>4.0</CurrentEnd>
            {key_tracks_xml}
        </Clip>"""
        return ET.fromstring(xml)

    def test_valid_notes_parse_correctly(self):
        """Valid notes parse with correct pitch/start/duration/velocity/enabled, sorted by start."""
        key_tracks_xml = """
        <KeyTrack>
            <MidiKey Value="60"/>
            <MidiNoteEvent Time="0.0" Duration="0.5" Velocity="100" IsEnabled="true"/>
            <MidiNoteEvent Time="1.0" Duration="0.5" Velocity="100" IsEnabled="true"/>
        </KeyTrack>
        <KeyTrack>
            <MidiKey Value="64"/>
            <MidiNoteEvent Time="0.5" Duration="0.5" Velocity="100" IsEnabled="true"/>
        </KeyTrack>
        """
        clip_el = self._build_clip_xml(key_tracks_xml)
        notes, skip_detail = parse_notes(clip_el)

        self.assertEqual(len(notes), 3)
        self.assertEqual(skip_detail, {})

        # Check first note
        self.assertEqual(notes[0].pitch, 60)
        self.assertEqual(notes[0].start, 0.0)
        self.assertEqual(notes[0].duration, 0.5)
        self.assertEqual(notes[0].velocity, 100)
        self.assertTrue(notes[0].enabled)

        # Check second note
        self.assertEqual(notes[1].pitch, 64)
        self.assertEqual(notes[1].start, 0.5)
        self.assertEqual(notes[1].duration, 0.5)
        self.assertEqual(notes[1].velocity, 100)
        self.assertTrue(notes[1].enabled)

        # Check third note
        self.assertEqual(notes[2].pitch, 60)
        self.assertEqual(notes[2].start, 1.0)
        self.assertEqual(notes[2].duration, 0.5)
        self.assertEqual(notes[2].velocity, 100)
        self.assertTrue(notes[2].enabled)

        # Verify sorting by start, then pitch
        self.assertEqual([n.pitch for n in notes], [60, 64, 60])

    def test_malformed_pitch_counts_in_skip_detail(self):
        """Malformed pitch KeyTrack counted in skip_detail['malformed_pitch'] and skipped."""
        key_tracks_xml = """
        <KeyTrack>
            <MidiKey Value="60"/>
            <MidiNoteEvent Time="0.0" Duration="0.5" Velocity="100" IsEnabled="true"/>
        </KeyTrack>
        <KeyTrack>
            <MidiKey Value="not_a_number"/>
            <MidiNoteEvent Time="1.0" Duration="0.5" Velocity="100" IsEnabled="true"/>
        </KeyTrack>
        """
        clip_el = self._build_clip_xml(key_tracks_xml)
        notes, skip_detail = parse_notes(clip_el)

        self.assertEqual(len(notes), 1)
        self.assertEqual(skip_detail["malformed_pitch"], 1)
        self.assertEqual(notes[0].pitch, 60)

    def test_malformed_pitch_counts_each_discarded_event(self):
        """Each MidiNoteEvent under a pitch-invalid KeyTrack counts as malformed_pitch."""
        key_tracks_xml = """
        <KeyTrack>
            <MidiKey Value="not_a_number"/>
            <MidiNoteEvent Time="0.0" Duration="0.5" Velocity="100" IsEnabled="true"/>
            <MidiNoteEvent Time="1.0" Duration="0.5" Velocity="100" IsEnabled="true"/>
            <MidiNoteEvent Time="2.0" Duration="0.5" Velocity="100" IsEnabled="true"/>
        </KeyTrack>
        """
        clip_el = self._build_clip_xml(key_tracks_xml)
        notes, skip_detail = parse_notes(clip_el)

        self.assertEqual(len(notes), 0)
        self.assertEqual(skip_detail["malformed_pitch"], 3)
        self.assertEqual(sum(skip_detail.values()), 3)

    def test_missing_pitch_counts_each_discarded_event(self):
        """KeyTrack without MidiKey: each descendant event counts as missing_pitch."""
        key_tracks_xml = """
        <KeyTrack>
            <MidiNoteEvent Time="0.0" Duration="0.5" Velocity="100" IsEnabled="true"/>
            <MidiNoteEvent Time="1.0" Duration="0.5" Velocity="100" IsEnabled="true"/>
        </KeyTrack>
        """
        clip_el = self._build_clip_xml(key_tracks_xml)
        notes, skip_detail = parse_notes(clip_el)

        self.assertEqual(len(notes), 0)
        self.assertEqual(skip_detail["missing_pitch"], 2)
        self.assertEqual(sum(skip_detail.values()), 2)

    def test_pitch_invalid_key_track_with_no_events_adds_zero(self):
        """Pitch-invalid KeyTracks with zero child events contribute zero skips."""
        key_tracks_xml = """
        <KeyTrack>
            <MidiKey Value="not_a_number"/>
        </KeyTrack>
        <KeyTrack>
        </KeyTrack>
        """
        clip_el = self._build_clip_xml(key_tracks_xml)
        notes, skip_detail = parse_notes(clip_el)

        self.assertEqual(len(notes), 0)
        self.assertEqual(skip_detail, {})

    def test_non_positive_duration_counts_under_non_positive_duration(self):
        """Non-positive/NaN duration counted under skip_detail['non_positive_duration']."""
        key_tracks_xml = """
        <KeyTrack>
            <MidiKey Value="60"/>
            <MidiNoteEvent Time="0.0" Duration="0.0" Velocity="100" IsEnabled="true"/>
            <MidiNoteEvent Time="1.0" Duration="-0.5" Velocity="100" IsEnabled="true"/>
            <MidiNoteEvent Time="2.0" Duration="NaN" Velocity="100" IsEnabled="true"/>
        </KeyTrack>
        """
        clip_el = self._build_clip_xml(key_tracks_xml)
        notes, skip_detail = parse_notes(clip_el)

        self.assertEqual(len(notes), 0)
        self.assertEqual(skip_detail["non_positive_duration"], 3)

    def test_bad_velocity_counts_under_bad_velocity(self):
        """Bad velocity counted under skip_detail['bad_velocity']."""
        key_tracks_xml = """
        <KeyTrack>
            <MidiKey Value="60"/>
            <MidiNoteEvent Time="0.0" Duration="0.5" Velocity="not_a_number" IsEnabled="true"/>
            <MidiNoteEvent Time="1.0" Duration="0.5" Velocity="0" IsEnabled="true"/>
            <MidiNoteEvent Time="2.0" Duration="0.5" Velocity="128" IsEnabled="true"/>
        </KeyTrack>
        """
        clip_el = self._build_clip_xml(key_tracks_xml)
        notes, skip_detail = parse_notes(clip_el)

        # Velocity="0" and Velocity="128" are valid (int(float("0")) = 0, int(float("128")) = 128)
        # Only "not_a_number" should be bad
        self.assertEqual(len(notes), 2)
        self.assertEqual(skip_detail["bad_velocity"], 1)

    def test_unparsable_time_counts_under_unparsable_note(self):
        """Unparsable Time counted under skip_detail['unparsable_note']."""
        key_tracks_xml = """
        <KeyTrack>
            <MidiKey Value="60"/>
            <MidiNoteEvent Time="not_a_number" Duration="0.5" Velocity="100" IsEnabled="true"/>
            <MidiNoteEvent Time="0.0" Duration="0.5" Velocity="100" IsEnabled="true"/>
        </KeyTrack>
        """
        clip_el = self._build_clip_xml(key_tracks_xml)
        notes, skip_detail = parse_notes(clip_el)

        self.assertEqual(len(notes), 1)
        self.assertEqual(skip_detail["unparsable_note"], 1)
        self.assertEqual(notes[0].pitch, 60)
        self.assertEqual(notes[0].start, 0.0)

    def test_skip_counts_aggregate_across_multiple_key_tracks(self):
        """Skip counts aggregate across KeyTracks — one failure mode each (parser short-circuits)."""
        key_tracks_xml = """
        <KeyTrack>
            <MidiKey Value="60"/>
            <MidiNoteEvent Time="0.0" Duration="0.5" Velocity="100" IsEnabled="true"/>
        </KeyTrack>
        <KeyTrack>
            <MidiKey Value="not_a_number"/>
            <MidiNoteEvent Time="1.0" Duration="0.5" Velocity="100" IsEnabled="true"/>
        </KeyTrack>
        <KeyTrack>
            <MidiKey Value="64"/>
            <MidiNoteEvent Time="2.0" Duration="0.0" Velocity="100" IsEnabled="true"/>
        </KeyTrack>
        <KeyTrack>
            <MidiKey Value="67"/>
            <MidiNoteEvent Time="3.0" Duration="0.5" Velocity="not_a_number" IsEnabled="true"/>
        </KeyTrack>
        <KeyTrack>
            <MidiKey Value="69"/>
            <MidiNoteEvent Time="not_a_number" Duration="0.5" Velocity="100" IsEnabled="true"/>
        </KeyTrack>
        """
        clip_el = self._build_clip_xml(key_tracks_xml)
        notes, skip_detail = parse_notes(clip_el)

        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].pitch, 60)
        self.assertEqual(skip_detail["malformed_pitch"], 1)
        self.assertEqual(skip_detail["non_positive_duration"], 1)
        self.assertEqual(skip_detail["bad_velocity"], 1)
        self.assertEqual(skip_detail["unparsable_note"], 1)
        self.assertEqual(sum(skip_detail.values()), 4)

    def test_five_discarded_events_report_exactly_five_skips(self):
        """Five discarded MIDI events must yield notes_skipped == 5 at clip boundary."""
        key_tracks_xml = """
        <KeyTrack>
            <MidiKey Value="not_a_number"/>
            <MidiNoteEvent Time="0.0" Duration="0.5" Velocity="100" IsEnabled="true"/>
            <MidiNoteEvent Time="1.0" Duration="0.5" Velocity="100" IsEnabled="true"/>
        </KeyTrack>
        <KeyTrack>
            <MidiNoteEvent Time="2.0" Duration="0.5" Velocity="100" IsEnabled="true"/>
            <MidiNoteEvent Time="3.0" Duration="0.5" Velocity="100" IsEnabled="true"/>
        </KeyTrack>
        <KeyTrack>
            <MidiKey Value="60"/>
            <MidiNoteEvent Time="4.0" Duration="0.0" Velocity="100" IsEnabled="true"/>
        </KeyTrack>
        """
        clip_el = self._build_clip_xml(key_tracks_xml)
        notes, skip_detail = parse_notes(clip_el)

        self.assertEqual(len(notes), 0)
        self.assertEqual(skip_detail["malformed_pitch"], 2)
        self.assertEqual(skip_detail["missing_pitch"], 2)
        self.assertEqual(skip_detail["non_positive_duration"], 1)
        self.assertEqual(sum(skip_detail.values()), 5)

    def test_multiple_valid_notes_per_key_track(self):
        """Multiple valid notes in same KeyTrack parse correctly."""
        key_tracks_xml = """
        <KeyTrack>
            <MidiKey Value="60"/>
            <MidiNoteEvent Time="0.0" Duration="0.25" Velocity="100" IsEnabled="true"/>
            <MidiNoteEvent Time="0.25" Duration="0.25" Velocity="100" IsEnabled="true"/>
            <MidiNoteEvent Time="0.5" Duration="0.25" Velocity="100" IsEnabled="true"/>
            <MidiNoteEvent Time="0.75" Duration="0.25" Velocity="100" IsEnabled="true"/>
        </KeyTrack>
        """
        clip_el = self._build_clip_xml(key_tracks_xml)
        notes, skip_detail = parse_notes(clip_el)

        self.assertEqual(len(notes), 4)
        self.assertEqual(skip_detail, {})
        self.assertEqual([n.pitch for n in notes], [60, 60, 60, 60])
        self.assertEqual([n.start for n in notes], [0.0, 0.25, 0.5, 0.75])


class TestSkipRoundTrip(unittest.TestCase):
    """Skip counters survive to_dict → from_dict materialization boundary."""

    def test_from_dict_preserves_notes_skipped_and_skip_detail(self):
        data = {
            "path": "/tmp/test.als",
            "tempo": 120.0,
            "track_count": 1,
            "midi_track_names": ["Lead"],
            "audio_track_names": [],
            "clip_count": 1,
            "total_notes": 0,
            "audio_clip_count": 0,
            "notes_skipped": 5,
            "skip_detail": {
                "malformed_pitch": 2,
                "missing_pitch": 2,
                "non_positive_duration": 1,
            },
            "clips": [{
                "name": "clip",
                "track": "Lead",
                "start": 0.0,
                "end": 4.0,
                "note_count": 0,
                "is_drum": False,
                "clip_source": "arrangement",
                "notes": [],
            }],
        }
        project = from_dict(data)
        self.assertEqual(project.notes_skipped, 5)
        self.assertEqual(project.skip_detail, data["skip_detail"])
        self.assertEqual(project.notes_skipped, sum(project.skip_detail.values()))

    def test_to_dict_from_dict_round_trip_preserves_skip_counters(self):
        original = {
            "path": "/tmp/roundtrip.als",
            "tempo": 128.0,
            "track_count": 1,
            "midi_track_names": ["Bass"],
            "audio_track_names": [],
            "clip_count": 1,
            "total_notes": 1,
            "audio_clip_count": 0,
            "notes_skipped": 3,
            "skip_detail": {"malformed_pitch": 3},
            "clips": [{
                "name": "bass",
                "track": "Bass",
                "start": 0.0,
                "end": 8.0,
                "note_count": 1,
                "is_drum": False,
                "clip_source": "session",
                "notes": [{
                    "pitch": 36,
                    "start": 0.0,
                    "duration": 0.5,
                    "velocity": 100,
                    "enabled": True,
                }],
            }],
        }
        project = from_dict(original)
        restored = from_dict(to_dict(project))
        self.assertEqual(restored.notes_skipped, 3)
        self.assertEqual(restored.skip_detail, {"malformed_pitch": 3})
        self.assertEqual(restored.notes_skipped, sum(restored.skip_detail.values()))


if __name__ == "__main__":
    unittest.main()
