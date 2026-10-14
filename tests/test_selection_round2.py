"""Round-two selection review regressions."""
import unittest
from unittest import mock
from tests._requires import requires_catalog


class RatingVectorFeedbackTests(unittest.TestCase):
    def test_unmark_and_soft_demote_records_are_not_down_votes(self):
        from mcp_server import taste_bridge as TB
        notes = [{"pitch": 60, "start": 0, "duration": 1,
                  "velocity": 90}]
        ratings = [
            {"rating": -1, "label": "ungold", "notes": notes},
            {"rating": 0, "label": "served-not-kept", "notes": notes},
        ]
        with mock.patch.object(TB, "_phrases", return_value=[]):
            ups, downs = TB._rating_vectors(ratings, "harmonic")
        self.assertEqual(ups, [])
        self.assertEqual(downs, [])


class ComposeBriefParserTests(unittest.TestCase):
    def test_lowercase_tonic(self):
        from mcp_server.core import _parse_brief_hints
        self.assertEqual(_parse_brief_hints("c minor lead")["key"],
                         "C minor")

    def test_lowercase_a_requires_full_mode(self):
        from mcp_server.core import _parse_brief_hints
        self.assertEqual(_parse_brief_hints("a minor lead")["key"],
                         "A minor")
        self.assertIsNone(_parse_brief_hints("I Am tired")["key"])

    def test_drum_and_bass_names_genre_not_role(self):
        from mcp_server.core import _parse_brief_hints
        for request in ("drum and bass", "drum & bass", "dnb"):
            with self.subTest(request=request):
                self.assertIsNone(_parse_brief_hints(request)["role"])


class ReasonNegationTests(unittest.TestCase):
    def test_negation_must_be_immediately_before_word(self):
        from mcp_server.ears import reason_bucket_dim
        self.assertEqual(reason_bucket_dim("No, too static"), "contour")
        self.assertEqual(reason_bucket_dim("no. static"), "contour")
        self.assertIsNone(reason_bucket_dim("it's not really static"))
        self.assertIsNone(reason_bucket_dim("not static"))


class EarsOffDemotionScopeTests(unittest.TestCase):
    def test_previous_sessions_reason_demotions_do_not_move_todays_picks(self):
        import json
        import tempfile
        from pathlib import Path
        from mcp_server import core as C
        from mcp_server import ears, ledger
        ranked = [{"id": "flat", "notes": []},
                  {"id": "rise", "notes": []}]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ears.json"
            path.write_text(json.dumps({
                "demoted": ["contour:flat"],
                "reason_demoted": ["contour:flat"],
                "ledger_session_id": "last-night"}))
            with (mock.patch.object(ledger, "load", return_value={}),
                  mock.patch.object(ledger, "_live_sessions",
                                    return_value=[{"id": "tonight",
                                                   "last_activity": 9}]),
                  mock.patch.object(ledger, "_now", return_value=10),
                  mock.patch.object(ears, "candidate_buckets",
                                    side_effect=[{"contour": "flat"},
                                                 {"contour": "rising"}])):
                moved = C._demote_session_buckets(
                    ranked, lambda hit, *_args: hit["notes"], 0, 60, 60,
                    session_path=str(path))
        self.assertEqual([hit["id"] for hit in moved], ["flat", "rise"])

    def test_new_ledger_session_does_not_inherit_old_ears_buckets(self):
        import json
        import tempfile
        from pathlib import Path
        from mcp_server import ears
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "ears.json")
            Path(path).write_text(json.dumps({
                "streaks": {"contour:flat": 3},
                "demoted": ["contour:flat"],
                "reason_demoted": ["contour:flat"],
                "ledger_session_id": "last-night"}))
            def save(state, target):
                Path(target).write_text(json.dumps(state))
            with mock.patch.object(ears, "note_verdict", return_value=[]), \
                    mock.patch.object(ears, "save_session", side_effect=save):
                ears.record_verdict(
                    {"notes": []}, kept=False, path=path,
                    ledger_session_id="tonight")
            state = ears.load_session(path)
        self.assertNotIn("contour:flat", state["reason_demoted"])
        self.assertEqual(state["ledger_session_id"], "tonight")

@requires_catalog
class ServedNotKeptClearTests(unittest.TestCase):
    def test_moved_drop_does_not_clear_soft_demotion(self):
        import tempfile
        from pathlib import Path
        from mcp_server import core_shared, taste_bridge as TB
        notes = [{"pitch": 60, "start": 0, "duration": 1,
                  "velocity": 90}]
        with tempfile.TemporaryDirectory() as td:
            old_path = core_shared._ratings_path() if core_shared._RATINGS_PATH_OVERRIDE else None
            core_shared.set_ratings_path(Path(td) / "ratings.json")
            try:
                C = __import__("mcp_server.core", fromlist=["rate"])
                C.rate(rating=0, label="served-not-kept",
                       phrase_id="src|demote|w0", notes=notes)
                C.rate(rating=-1, label="dropped",
                       phrase_id="src|demote|w0", notes=notes,
                       move="surgery:thin")
                self.assertIn("src|demote|w0",
                              TB.served_not_kept_identities())
            finally:
                core_shared.set_ratings_path(old_path)


class SeedStampSizeTests(unittest.TestCase):
    def test_seed_notes_are_not_persisted_or_snapshotted(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        from mcp_server import ledger
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "ledger.json")
            with mock.patch.object(ledger, "LEDGER_PATH", path):
                seed = [{"pitch": 60, "start": 0, "duration": 1,
                         "velocity": 90}]
                entry = ledger.record_write(
                    clip_name="one", slot="3/0", source="catalogue",
                    source_phrase_id="src|seed|w0", answer_notes=seed,
                    seed_notes=seed, path=path)
                ledger.record_write(
                    clip_name="two", slot="3/1", source="catalogue",
                    source_phrase_id="src|seed|w0", answer_notes=seed,
                    seed_notes=seed, revision_of=entry["id"], path=path)
            saved = json.loads(Path(path).read_text())
            row = saved["entries"][0]
            self.assertNotIn("seed_notes", row)
            self.assertNotIn("seed_notes",
                             row["revision_history"][0]["snapshot"])
