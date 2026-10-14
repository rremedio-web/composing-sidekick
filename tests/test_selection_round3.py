"""B3 selection round-3 regression tests."""
import json
import tempfile
import unittest
from pathlib import Path

from mcp_server import core as _core  # noqa: F401 — installs bare-import alias
from mcp_server import ears


class UnboundVerdictSessionTests(unittest.TestCase):
    NOTES = [
        {"pitch": 72, "start": 0.0, "duration": 0.5, "velocity": 90},
        {"pitch": 72, "start": 1.0, "duration": 0.5, "velocity": 85},
        {"pitch": 72, "start": 2.0, "duration": 1.0, "velocity": 90},
        {"pitch": 72, "start": 3.0, "duration": 1.0, "velocity": 88},
    ]

    def test_unbound_verdicts_preserve_bound_session_memory(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "ears.json")
            ears.record_verdict({"notes": self.NOTES}, False, path=path,
                                reason="too static", ledger_session_id="tonight")
            ears.record_verdict({"notes": self.NOTES}, False, path=path,
                                ledger_session_id=None)
            ears.record_verdict({"notes": self.NOTES}, False, path=path,
                                reason="too static", ledger_session_id="tonight")
            state = ears.load_session(path)
            self.assertIn("contour:flat", state["reason_demoted"])

    def test_unbound_keep_does_not_clear_existing_demotion(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "ears.json")
            ears.save_session({"streaks": {"contour:flat": 3},
                               "demoted": ["contour:flat"],
                               "reason_demoted": ["contour:flat"],
                               "ledger_session_id": "tonight"}, path)
            ears.record_verdict({"notes": self.NOTES}, True, path=path,
                                ledger_session_id=None)
            state = ears.load_session(path)
            self.assertIn("contour:flat", state["reason_demoted"])


class ComposeBriefKeyHintTests(unittest.TestCase):
    def test_skips_invalid_early_key_candidates(self):
        from mcp_server import core
        self.assertEqual(core._parse_brief_hints(
            "I am thinking C minor lead")["key"], "C minor")
        self.assertEqual(core._parse_brief_hints(
            "I Am feeling it, give me a C minor lead")["key"], "C minor")

    def test_key_regex_comment_describes_lowercase_a_ambiguity(self):
        import inspect
        from mcp_server import core
        source = inspect.getsource(core)
        self.assertIn(
            'lowercase "a" is ambiguous with the article', source)


class ComposeBriefGenreGuardTests(unittest.TestCase):
    def test_strips_drum_and_bass_genre_before_role_matching(self):
        from mcp_server import core
        self.assertEqual(core._parse_brief_hints("dnb lead")["role"], "lead")
        self.assertEqual(core._parse_brief_hints("drum n bass lead")["role"], "lead")
        self.assertEqual(core._parse_brief_hints("drum 'n' bass lead")["role"], "lead")
        self.assertEqual(core._parse_brief_hints(
            "a bass at 170 bpm drum and bass style")["role"], "bass")
        self.assertEqual(core._parse_brief_hints(
            "give me drums for drum and bass")["role"], "drum")
        self.assertIsNone(core._parse_brief_hints(
            "G major, drum and bass")["role"])
        self.assertIsNone(core._parse_brief_hints("d&b")["role"])


class ReasonNegationRound3Tests(unittest.TestCase):
    def test_hyphen_and_single_softener_preserve_negation(self):
        from mcp_server.ears import reason_bucket_dim
        for reason in ("not-static", "doesn't feel static",
                       "it is not so static"):
            with self.subTest(reason=reason):
                self.assertIsNone(reason_bucket_dim(reason))
        self.assertEqual(reason_bucket_dim("No, too static"), "contour")


class ReasonDemotionVoteTests(unittest.TestCase):
    def test_prior_reason_vote_marks_bucket_when_later_vote_crosses(self):
        from mcp_server import ears
        notes = UnboundVerdictSessionTests.NOTES
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "ears.json")
            ears.record_verdict({"notes": notes}, False, path=path,
                                reason="too static", ledger_session_id="night")
            ears.record_verdict({"notes": notes}, False, path=path,
                                ledger_session_id="night")
            state = ears.load_session(path)
            self.assertIn("contour:flat", state["reason_demoted"])


class OutcomeTimestampAndRetryMarkerTests(unittest.TestCase):
    def test_newer_z_rating_beats_plus_zero_terminal_stamp(self):
        from unittest import mock
        from mcp_server import server
        entry = {"id": "e-time", "terminal_state": "drop",
                 "immediate_verdict": "keep", "source_phrase_id": "p1",
                 "terminal_at": "2026-09-25T10:00:00.900000Z",
                 "answer_notes": [{"pitch": 60}]}
        with mock.patch.object(server, "_outcome_op_applied", return_value=False), \
                mock.patch("taste_bridge._load_ratings_file", return_value={
                    "ratings": [{"phrase_id": "p1",
                                 "created_at": "2026-09-25T10:00:00.950000+00:00"}]}), \
                mock.patch("ledger.set_outcome_op_pending"), \
                mock.patch.object(server, "_apply_outcome_rating_ops") as apply:
            server._close_outcome_ratings(
                {"entries": [{"entry_id": "e-time"}]}, {"e-time": entry},
                {"e-time": None})
        apply.assert_not_called()

    def test_notes_only_row_does_not_match_unrelated_notes_only_rating(self):
        from unittest import mock
        from mcp_server import server
        entry = {"id": "e-notes", "terminal_state": "abandoned",
                 "source_phrase_id": None, "answer_notes": [{"pitch": 60}],
                 "terminal_at": "2026-09-25T10:00:00Z"}
        with mock.patch.object(server, "_outcome_op_applied", return_value=False), \
                mock.patch("taste_bridge._load_ratings_file", return_value={
                    "ratings": [{"phrase_id": None, "entry_id": "other",
                                 "created_at": "2026-09-25T10:00:01Z"}]}), \
                mock.patch("ledger.set_outcome_op_pending"), \
                mock.patch.object(server, "_apply_outcome_rating_ops",
                                  return_value=[]) as apply:
            server._close_outcome_ratings(
                {"entries": [{"entry_id": "e-notes"}]}, {"e-notes": entry},
                {"e-notes": None})
        apply.assert_called_once()

    def test_retry_marker_write_failure_is_reported_not_raised(self):
        from unittest import mock
        from mcp_server import server
        entry = {"id": "e-marker", "terminal_state": "abandoned",
                 "answer_notes": [{"pitch": 60}], "terminal_at": "2026-09-25T10:00:00Z"}
        with mock.patch.object(server, "_outcome_op_applied", return_value=False), \
                mock.patch("taste_bridge._load_ratings_file", return_value={"ratings": []}), \
                mock.patch("ledger.set_outcome_op_pending",
                           side_effect=OSError("ledger locked")), \
                mock.patch.object(server, "_apply_outcome_rating_ops",
                                  return_value=[{"error": "ratings locked"}]):
            outcomes, _ = server._close_outcome_ratings(
                {"entries": [{"entry_id": "e-marker"}]}, {"e-marker": entry},
                {"e-marker": None})
        self.assertTrue(any("retry marker" in str(item).lower()
                            for item in outcomes), outcomes)

    def test_pending_marker_write_skips_unchanged_value(self):
        from unittest import mock
        from mcp_server import ledger
        with mock.patch.object(ledger, "LEDGER_PATH", "unused-ledger"), \
                mock.patch.object(ledger, "_load_for_mutation", return_value={
                    "entries": [{"id": "e-marker", "outcome_op_pending": True}]}), \
                mock.patch.object(ledger, "_save") as save:
            ledger.set_outcome_op_pending("e-marker", True)
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
