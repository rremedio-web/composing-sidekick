"""Unit tests for analyze/roles.py."""
from __future__ import annotations

import unittest

import sys
import os

# Insert repo root to allow imports from analyze/
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from analyze import roles


class TestClipRoleReport(unittest.TestCase):
    """Test clip_role_report() for each role class."""

    def test_pad_role(self):
        """pad: avg_polyphony >= 2.2."""
        notes = [
            {"pitch": 60, "start_beats": 0.0, "duration_beats": 0.25, "enabled": True},
            {"pitch": 62, "start_beats": 0.0, "duration_beats": 0.25, "enabled": True},
            {"pitch": 64, "start_beats": 0.0, "duration_beats": 0.25, "enabled": True},
            {"pitch": 65, "start_beats": 0.0, "duration_beats": 0.25, "enabled": True},
        ]
        report = roles.clip_role_report(notes)
        self.assertEqual(report["role"], "pad")
        self.assertGreaterEqual(report["confidence"], 0.5)

    def test_bass_role(self):
        """bass: median pitch < 48."""
        notes = [
            {"pitch": 36, "start_beats": 0.0, "duration_beats": 0.5, "enabled": True},
            {"pitch": 38, "start_beats": 0.5, "duration_beats": 0.5, "enabled": True},
            {"pitch": 40, "start_beats": 1.0, "duration_beats": 0.5, "enabled": True},
        ]
        report = roles.clip_role_report(notes)
        self.assertEqual(report["role"], "bass")
        self.assertGreaterEqual(report["confidence"], 0.5)

    def test_lead_role(self):
        """lead: median pitch >= 55."""
        notes = [
            {"pitch": 60, "start_beats": 0.0, "duration_beats": 0.5, "enabled": True},
            {"pitch": 62, "start_beats": 0.5, "duration_beats": 0.5, "enabled": True},
            {"pitch": 64, "start_beats": 1.0, "duration_beats": 0.5, "enabled": True},
            {"pitch": 67, "start_beats": 1.5, "duration_beats": 0.5, "enabled": True},
        ]
        report = roles.clip_role_report(notes)
        self.assertEqual(report["role"], "lead")
        self.assertGreaterEqual(report["confidence"], 0.5)

    def test_other_role(self):
        """other: ambiguous register [48..54] with low polyphony."""
        notes = [
            {"pitch": 50, "start_beats": 0.0, "duration_beats": 0.5, "enabled": True},
            {"pitch": 52, "start_beats": 0.5, "duration_beats": 0.5, "enabled": True},
        ]
        report = roles.clip_role_report(notes)
        self.assertEqual(report["role"], "other")
        self.assertGreaterEqual(report["confidence"], 0.3)

    def test_drum_role(self):
        """drum: is_drum=True."""
        notes = [
            {"pitch": 36, "start_beats": 0.0, "duration_beats": 0.25, "enabled": True},
        ]
        report = roles.clip_role_report(notes, is_drum=True)
        self.assertEqual(report["role"], "drum")
        self.assertEqual(report["confidence"], 1.0)

    def test_few_notes(self):
        """other: fewer than 2 enabled notes."""
        notes = [{"pitch": 60, "start_beats": 0.0, "duration_beats": 0.5, "enabled": True}]
        report = roles.clip_role_report(notes)
        self.assertEqual(report["role"], "other")
        self.assertEqual(report["confidence"], 1.0)

    def test_disabled_notes(self):
        """other: disabled notes excluded."""
        notes = [
            {"pitch": 60, "start_beats": 0.0, "duration_beats": 0.5, "enabled": False},
            {"pitch": 62, "start_beats": 0.5, "duration_beats": 0.5, "enabled": True},
        ]
        report = roles.clip_role_report(notes)
        self.assertEqual(report["role"], "other")


class TestClipRole(unittest.TestCase):
    """Test clip_role() convenience wrapper."""

    def test_pad(self):
        notes = [
            {"pitch": 60, "start_beats": 0.0, "duration_beats": 0.25, "enabled": True},
            {"pitch": 62, "start_beats": 0.0, "duration_beats": 0.25, "enabled": True},
            {"pitch": 64, "start_beats": 0.0, "duration_beats": 0.25, "enabled": True},
        ]
        self.assertEqual(roles.clip_role(notes), "pad")

    def test_bass(self):
        notes = [
            {"pitch": 36, "start_beats": 0.0, "duration_beats": 0.5, "enabled": True},
            {"pitch": 38, "start_beats": 0.5, "duration_beats": 0.5, "enabled": True},
        ]
        self.assertEqual(roles.clip_role(notes), "bass")

    def test_lead(self):
        notes = [
            {"pitch": 60, "start_beats": 0.0, "duration_beats": 0.5, "enabled": True},
            {"pitch": 62, "start_beats": 0.5, "duration_beats": 0.5, "enabled": True},
            {"pitch": 64, "start_beats": 1.0, "duration_beats": 0.5, "enabled": True},
        ]
        self.assertEqual(roles.clip_role(notes), "lead")

    def test_other(self):
        notes = [
            {"pitch": 50, "start_beats": 0.0, "duration_beats": 0.5, "enabled": True},
            {"pitch": 52, "start_beats": 0.5, "duration_beats": 0.5, "enabled": True},
        ]
        self.assertEqual(roles.clip_role(notes), "other")


class TestThresholds(unittest.TestCase):
    """Test that threshold constants match documented values."""

    def test_pad_min_polyphony(self):
        self.assertEqual(roles.ROLE_THRESHOLDS["pad_min_polyphony"], 2.2)

    def test_bass_max_pitch(self):
        self.assertEqual(roles.ROLE_THRESHOLDS["bass_max_pitch"], 48)

    def test_lead_min_pitch(self):
        self.assertEqual(roles.ROLE_THRESHOLDS["lead_min_pitch"], 55)


if __name__ == "__main__":
    unittest.main()
