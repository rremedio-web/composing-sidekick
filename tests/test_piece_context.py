"""Tests for arrangement layer observations on materialized project records."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from mcp_server.piece_context import (
    current_decisions, layer_matrix, precedent_habits, record_decision,
    resolve_precedent,
)


FIXTURES = Path(__file__).parent / "fixtures" / "piece_context"


class TestLayerMatrix(unittest.TestCase):
    def test_song_reports_thin_and_delayed_entry_habits(self):
        # Synthetic Project shape (anonymized catalog structure: generic names, scrambled pitches).
        project = json.loads((FIXTURES / "song-a.json").read_text())
        matrix = layer_matrix(
            project["clips"], beats_per_bar=4,
            audio_track_names=project["audio_track_names"],
            audio_clip_count=project["audio_clip_count"],
        )

        habits = precedent_habits(matrix)
        kinds = {habit["kind"] for habit in habits}
        self.assertIn("thin_before_return", kinds)
        delayed = next(habit for habit in habits if habit["kind"] == "delayed_entry")
        self.assertEqual(delayed["bars"], 36)
        self.assertTrue(all(habit["partial"] for habit in habits))

    def test_song_matrix_discloses_unseen_audio_arrangement(self):
        # Synthetic Project shape (anonymized catalog structure: generic names, scrambled pitches).
        project = json.loads((FIXTURES / "song-a.json").read_text())
        matrix = layer_matrix(
            project["clips"], beats_per_bar=4,
            audio_track_names=project["audio_track_names"],
            audio_clip_count=project["audio_clip_count"],
        )
        self.assertEqual(matrix["basis"], "midi_layers")
        self.assertEqual(matrix["audio_unseen"], {"tracks": 7, "clips": 21})

    def test_session_only_project_reports_no_arrangement(self):
        # Synthetic Clip shape (anonymized catalog structure: generic names, scrambled pitches).
        project = json.loads((FIXTURES / "session-only.json").read_text())
        matrix = layer_matrix(project["clips"], beats_per_bar=4)
        self.assertEqual(matrix, {
            "no_arrangement": True,
            "message": "No arrangement clips are available; this project is session-only.",
        })
        self.assertEqual(precedent_habits(matrix), [])

    def test_missing_audio_metadata_warns_and_marks_habits_partial(self):
        clips = [{"track": "A", "start": 0, "end": 4, "clip_source": "arrangement"}]
        matrix = layer_matrix(clips, beats_per_bar=4)
        self.assertEqual(matrix["audio_unseen"], "unknown")
        self.assertTrue(matrix["warning"])
        self.assertTrue(all(habit["partial"] for habit in precedent_habits(matrix)))

    def test_materialized_song_fixtures_have_verified_layer_counts(self):
        # Synthetic shapes (anonymized catalog structure).
        for filename, expected_bars, expected in (
            ("song-a.json", 81, {35: 0, 36: 1, 42: 2, 46: 3, 50: 3, 60: 3, 80: 1}),
            ("song-b.json", 82, {35: 0, 36: 1, 42: 2, 46: 3, 50: 3, 60: 3, 80: 2, 81: 1}),
        ):
            with self.subTest(filename=filename):
                project = json.loads((FIXTURES / filename).read_text())
                matrix = layer_matrix(project["clips"], beats_per_bar=4)
                self.assertEqual(matrix["bars"], expected_bars)
                for bar, count in expected.items():
                    self.assertEqual(matrix["layers_per_bar"][bar], count, bar)
                self.assertEqual(matrix["evidence"], "observed")

    def test_layers_count_tracks_once_ignore_empty_and_take_lane(self):
        # Shape mirrors materialized project clip fields; take_lane represents an alternate.
        clips = [
            {"track": "A", "start": 0, "end": 9, "clip_source": "arrangement"},
            {"track": "A", "start": 4, "end": 8, "clip_source": "arrangement"},
            {"track": "B", "start": 4, "end": 8, "clip_source": "arrangement"},
            {"track": "C", "start": 0, "end": 8, "clip_source": "take_lane"},
            {"track": "D", "start": 8, "end": 8, "clip_source": "arrangement"},
        ]
        matrix = layer_matrix(clips, beats_per_bar=4)
        self.assertEqual(matrix["bars"], 3)
        self.assertEqual(matrix["layers_per_bar"], [1, 2, 1])
        self.assertEqual(matrix["tracks_per_bar"], [["A"], ["A", "B"], ["A"]])
        self.assertEqual(matrix["entries"], [{"bar": 0, "track": "A"}, {"bar": 1, "track": "B"}])
        self.assertEqual(matrix["exits"], [{"bar": 2, "track": "B"}, {"bar": 3, "track": "A"}])

    def test_layer_change_sections_and_habits_are_deterministic(self):
        clips = [
            {"track": "A", "start": 0, "end": 16, "clip_source": "arrangement"},
            {"track": "B", "start": 0, "end": 4, "clip_source": "arrangement"},
            {"track": "C", "start": 12, "end": 16, "clip_source": "arrangement"},
        ]
        matrix = layer_matrix(clips, beats_per_bar=4)
        self.assertEqual(matrix["layers_per_bar"], [2, 1, 1, 2])
        self.assertEqual(matrix["sections_guess"], [
            {"bar_start": 0, "bar_end": 1, "layer_mean": 2.0, "status": "inferred"},
            {"bar_start": 1, "bar_end": 3, "layer_mean": 1.0, "status": "inferred"},
            {"bar_start": 3, "bar_end": 4, "layer_mean": 2.0, "status": "inferred"},
        ])
        self.assertIn({"kind": "thin_before_return", "bars": 2,
                       "bar_start": 1, "layers": 1,
                       "detail": "drop to 1 layers for 2 bars at bar 1 before returning to 2 layers",
                       "status": "observed", "partial": True}, precedent_habits(matrix))

    def test_thin_habits_are_bounded_and_located(self):
        matrix = {"layers_per_bar": [3] + [2] * 4 + [3] + [2] * 17 + [3],
                  "audio_unseen": {"tracks": 0, "clips": 0}}
        habits = precedent_habits(matrix)
        thin = next(habit for habit in habits if habit["kind"] == "thin_before_return")
        self.assertEqual((thin["bars"], thin["bar_start"], thin["layers"]), (4, 1, 2))
        self.assertIn("drop to 2 layers for 4 bars at bar 1", thin["detail"])
        self.assertLessEqual(thin["bars"], 16)

    def test_long_valley_is_not_a_thin_habit_and_strip_is_bounded(self):
        counts = [3] + [2] * 40
        habits = precedent_habits({"layers_per_bar": counts,
                                   "audio_unseen": {"tracks": 0, "clips": 0}})
        self.assertFalse(any(habit["kind"] == "thin_before_return" for habit in habits))
        self.assertFalse(any(habit["kind"] == "strip_ending" for habit in habits))

    def test_late_peak_habit_requires_a_late_sustained_layer_rise(self):
        # Shape mirrors layer_matrix's layers_per_bar output (one count per bar).
        for counts in ([0] * 16, [2] * 16, [3] * 8 + [1] * 8,
                       [1] * 15 + [5], [1] * 10 + [3] * 6):
            with self.subTest(counts=counts):
                habits = precedent_habits({"layers_per_bar": counts})
                self.assertFalse(any(habit["kind"] == "late_peak" for habit in habits))

        habits = precedent_habits({"layers_per_bar": [1] * 20 + [3] * 8})
        peak = next(habit for habit in habits if habit["kind"] == "late_peak")
        self.assertEqual(peak["bars"], 20)

    def _late_peak_bars(self, counts):
        # Shape mirrors layer_matrix's layers_per_bar output (one count per bar).
        return [habit["bars"] for habit in precedent_habits({"layers_per_bar": counts})
                if habit["kind"] == "late_peak"]

    def test_rising_layer_plateau_from_mid_piece_is_not_a_late_peak(self):
        # Round-3 probe r2_peak: layers 2 -> 4 at bar 20 of 32, then 4 -> 5
        # at bar 26. The 90% onset lands at bar 24 (75%) but the rise began
        # at bar 20 (62.5%).
        self.assertEqual(self._late_peak_bars([2] * 20 + [4] * 6 + [5] * 6), [])
        self.assertEqual(self._late_peak_bars([2] * 20 + [4] * 12), [])

    def test_genuine_late_layer_rise_is_still_a_late_peak(self):
        self.assertEqual(self._late_peak_bars([2] * 24 + [4] * 8), [24])
        self.assertEqual(self._late_peak_bars([2] * 29 + [4] * 3), [28])
        self.assertEqual(self._late_peak_bars([1] * 24 + [2] * 8), [24])

    def test_missing_layer_counts_read_as_silent_bars(self):
        self.assertEqual(self._late_peak_bars([1] * 28 + [None] * 4), [])
        self.assertEqual(self._late_peak_bars([None] * 24 + [3] * 8), [])
        self.assertEqual(self._late_peak_bars([1] * 22 + [None] * 2 + [3] * 8), [24])

    def test_slow_layer_crescendo_is_not_a_late_peak(self):
        habits = precedent_habits({"layers_per_bar": list(range(1, 17))})
        self.assertFalse(any(habit["kind"] == "late_peak" for habit in habits))


class TestPrecedentResolution(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).parent / "fixtures" / "piece_context"
        self.index_path = self.directory / "resolution_index.json"

    def test_duplicate_name_returns_candidate_paths(self):
        # Index entries mirror store/projects/_index.json; project files mirror its id.json records.
        result = resolve_precedent("industrial", index_path=self.index_path)
        self.assertEqual(result, {"ambiguous": [
            "/Projects/industrial-a.als", "/Projects/industrial-b.als",
        ]})

    def test_exact_path_and_unique_name_load_the_indexed_project(self):
        # Synthetic Project shape (anonymized catalog structure: generic names, scrambled pitches).
        by_path = resolve_precedent("/Projects/solo.als", index_path=self.index_path)
        by_name = resolve_precedent("solo", index_path=self.index_path)
        expected = json.loads((self.directory / "solo-id.json").read_text())
        self.assertEqual(by_path, {"project": expected})
        self.assertEqual(by_name, {"project": expected})

    def test_missing_precedent_is_reported(self):
        self.assertEqual(resolve_precedent("unknown", index_path=self.index_path),
                         {"missing": "unknown"})

    def test_path_match_is_realpath_and_case_insensitive(self):
        result = resolve_precedent("/projects/SOLO.ALS", index_path=self.index_path)
        self.assertIn("project", result)


class TestPieceDecisions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "piece_decisions.json"
        self.key = "/Projects/song.als"

    def tearDown(self):
        self.temp.cleanup()

    def test_owner_decision_beats_later_model_for_same_field(self):
        record_decision(self.key, {"kind": "intent", "source": "owner",
                                   "goal": "patient", "borrow": ["pacing"]}, path=self.path)
        record_decision(self.key, {"kind": "intent", "source": "model",
                                   "goal": "urgent", "borrow": ["density"]}, path=self.path)
        current = current_decisions(self.key, path=self.path)
        self.assertEqual(current["goal"], "patient")
        self.assertEqual(current["borrow"], ["pacing"])

    def test_latest_owner_wins_and_revision_increments(self):
        first = record_decision(self.key, {"kind": "intent", "source": "owner",
                                           "goal": "patient"}, path=self.path)
        second = record_decision(self.key, {"kind": "intent", "source": "owner",
                                            "goal": "driving"}, path=self.path)
        self.assertEqual(first["revision"], 1)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(current_decisions(self.key, path=self.path)["goal"], "driving")

    def test_feedback_verdict_and_reason_do_not_resolve_as_intent(self):
        record_decision(self.key, {"kind": "arrangement_feedback", "source": "model",
                                   "verdict": "KEEP", "reason": "more patient"}, path=self.path)
        self.assertEqual(current_decisions(self.key, path=self.path), {"revision": 1})

    def test_unknown_direction_values_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "borrow"):
            record_decision(self.key, {"kind": "intent", "source": "owner",
                                       "borrow": ["tempo"]}, path=self.path)

    def test_concurrent_writers_keep_both_decisions(self):
        barrier = threading.Barrier(3)
        errors = []

        def write(goal):
            try:
                barrier.wait()
                record_decision(self.key, {"kind": "intent", "source": "owner",
                                           "goal": goal}, path=self.path)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(goal,))
                   for goal in ("one", "two")]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        data = json.loads(self.path.read_text())
        records = [row for row in data["decisions"] if row["set_key"] == self.key]
        self.assertEqual(len(records), 2)
        self.assertEqual(sorted(row["revision"] for row in records), [1, 2])


if __name__ == "__main__":
    unittest.main()
