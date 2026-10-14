"""Batch 5 item 7: owner intent questions for MIDI-only diagnoses."""
import os
import tempfile
import unittest
from unittest import mock

from mcp_server import finisher, piece_context


class FinisherIntentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.decisions_path = os.path.join(self.temp.name, "decisions.json")
        self.project = {"path": "/music/Tonight.als", "tempo": 120, "clips": [
            # Shape from a materialized parse_als arrangement clip.
            {"name": "Pad phrase", "track": "Pad", "start": 0, "end": 128,
             "note_count": 1, "is_drum": False, "clip_source": "arrangement",
             "notes": [{"pitch": 60, "start": 0, "duration": 1,
                        "velocity": 90, "enabled": True}]},
        ]}
        # Shape returned by finisher.read_structure for a materialized arrangement.
        self.reading = {
            "tempo": 120, "total_bars": 32,
            "bars": [{"density": 1.0, "tracks": frozenset({"Pad"}),
                      "roles": ["pad"]} for _ in range(32)],
            "sections": [{"start_bar": 1, "end_bar": 32, "bars": 32,
                          "density": 1.0, "signature": ("Pad",)}],
            "timeline": [], "comping_debris": 0, "tracks": ["Pad"],
        }
        self.read_patch = mock.patch.object(finisher, "read_structure", return_value=self.reading)
        self.read_patch.start()
        self.addCleanup(self.read_patch.stop)
        self.decision_patch = mock.patch.object(
            piece_context, "_decision_path", return_value=self.decisions_path)
        self.decision_patch.start()
        self.addCleanup(self.decision_patch.stop)

    def test_midi_only_findings_are_questions_with_limited_evidence(self):
        result = finisher.whats_missing(project_dict=self.project)
        by_detector = {row["detector"]: row for row in result["findings"]}
        for detector in ("uncommitted_ending", "flat_energy_run", "b_section_missing"):
            self.assertEqual(by_detector[detector]["severity"], "ask")
            self.assertIn("is this intended?", by_detector[detector]["evidence"])
            self.assertIn("MIDI only", by_detector[detector]["evidence"])
            self.assertNotRegex(by_detector[detector]["evidence"],
                                r"defect|never composed|missing a B")
        self.assertIn("hard stop / held texture / one section by design",
                      by_detector["uncommitted_ending"]["evidence"])

    def test_owner_intent_suppresses_same_finding_until_material_changes(self):
        first = finisher.whats_missing(project_dict=self.project,
                                       intended_findings=["uncommitted_ending"])
        self.assertNotIn("uncommitted_ending", {f["detector"] for f in first["findings"]})
        with open(self.decisions_path, encoding="utf-8") as handle:
            import json
            record = json.load(handle)["decisions"][-1]
        self.assertEqual(record["kind"], "protect")
        self.assertEqual(record["source"], "model_on_request")
        self.assertEqual(record["finding"], "uncommitted_ending")

        again = finisher.whats_missing(project_dict=self.project)
        self.assertNotIn("uncommitted_ending", {f["detector"] for f in again["findings"]})
        self.project["clips"][0]["notes"][0]["pitch"] = 62
        changed = finisher.whats_missing(project_dict=self.project)
        self.assertIn("uncommitted_ending", {f["detector"] for f in changed["findings"]})

    def test_b_section_intent_silences_linked_findings_and_proposals(self):
        self.reading["total_bars"] = 64
        self.reading["bars"] = self.reading["bars"] * 2
        self.reading["sections"] = [{"start_bar": 1, "end_bar": 64, "bars": 64,
                                      "density": 1.0, "signature": ("Pad",)}]
        # Round 3: the link is per instance. An intended loop_with_ambitions span
        # also silences b_section_missing; b_section_missing names no span, so it
        # silences only itself.
        result = finisher.whats_missing(project_dict=self.project,
                                        intended_findings=["loop_with_ambitions"])
        self.assertNotIn("loop_with_ambitions", {f["detector"] for f in result["findings"]})
        self.assertNotIn("b_section_missing", {f["detector"] for f in result["findings"]})
        self.assertNotIn("make_b_section", {p["intervention"] for p in result["proposals"]})

        os.unlink(self.decisions_path)
        result = finisher.whats_missing(project_dict=self.project,
                                        intended_findings=["b_section_missing"])
        self.assertNotIn("b_section_missing", {f["detector"] for f in result["findings"]})
        self.assertIn("loop_with_ambitions", {f["detector"] for f in result["findings"]})

    def test_repeated_detector_intent_silences_each_finding_instance(self):
        self.reading["total_bars"] = 48
        self.reading["bars"] = ([{"density": 1.0, "tracks": frozenset({"Pad"}),
                                  "roles": ["pad"]} for _ in range(16)]
                                 + [{"density": 0.1, "tracks": frozenset({"Pad"}),
                                     "roles": ["pad"]} for _ in range(16)]
                                 + [{"density": (0.1 if i % 2 else 1.0),
                                     "tracks": frozenset({"Pad"}), "roles": ["pad"]}
                                    for i in range(16)])
        self.reading["sections"] = [{"start_bar": 1, "end_bar": 48, "bars": 48,
                                      "density": 1.0, "signature": ("Pad",)}]
        self.project["clips"] = [
            {"name": "first", "start": 0, "end": 64, "track": "Pad"},
            {"name": "second", "start": 64, "end": 128, "track": "Pad"},
        ]

        # Round 3: a bare name that fired twice is refused; name the instances.
        with self.assertRaisesRegex(ValueError, "flat_energy_run@1-16"):
            finisher.whats_missing(project_dict=self.project,
                                   intended_findings=["flat_energy_run"])
        result = finisher.whats_missing(
            project_dict=self.project,
            intended_findings=["flat_energy_run@1-16", {"detector": "flat_energy_run",
                                                        "bars": [17, 32]}])
        self.assertNotIn("flat_energy_run", {f["detector"] for f in result["findings"]})
        with open(self.decisions_path, encoding="utf-8") as handle:
            import json
            decisions = json.load(handle)["decisions"]
        self.assertEqual(sum(row["finding"] == "flat_energy_run" for row in decisions), 2)

        again = finisher.whats_missing(project_dict=self.project)
        self.assertNotIn("flat_energy_run", {f["detector"] for f in again["findings"]})

    def test_intent_must_name_a_finding_raised_in_this_diagnosis(self):
        self.reading["total_bars"] = 16
        self.reading["bars"] = self.reading["bars"][:16]
        self.reading["sections"] = [{"start_bar": 1, "end_bar": 8, "bars": 8,
                                      "density": 1.0, "signature": ("Pad",)},
                                     {"start_bar": 9, "end_bar": 16, "bars": 8,
                                      "density": 1.0, "signature": ("Pad",)}]
        with self.assertRaisesRegex(ValueError, "not raised"):
            finisher.whats_missing(project_dict=self.project,
                                   intended_findings=["loop_with_ambitions"])


if __name__ == "__main__":
    unittest.main()
