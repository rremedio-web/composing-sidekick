"""Batch 5 final review: intended_findings input forms, truncated live reads,
and protect rows that keep out of the Set-level decision fields."""
import asyncio
import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from unittest import mock

from mcp_server import finisher, piece_context


class _IntentCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.decisions = os.path.join(self.temp.name, "piece_decisions.json")
        patcher = mock.patch.object(piece_context, "_decision_path", return_value=self.decisions)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Shape emitted by run_detectors for an ending that just stops.
        self.finding = {"detector": "uncommitted_ending", "severity": "ask", "bars": [9, 12],
                        "evidence": "the final bars hold density and then stop"}
        detectors = mock.patch.object(finisher, "run_detectors",
                                      side_effect=lambda _r: [dict(self.finding)])
        detectors.start()
        self.addCleanup(detectors.stop)
        # Shape of a materialized parse_als project for a saved Set.
        self.project = {"path": "/music/Song.als", "tempo": 120, "track_count": 1,
                        "midi_track_names": ["Lead"], "audio_track_names": [],
                        "clip_count": 1, "total_notes": 1, "clips": [
                            {"name": "a", "track": "Lead", "start": 32, "end": 48,
                             "note_count": 1, "clip_source": "arrangement",
                             "notes": [{"pitch": 60, "start": 0, "duration": 1,
                                        "velocity": 90, "enabled": True}]}]}

    def rows(self):
        if not os.path.exists(self.decisions):
            return []
        with open(self.decisions) as fh:
            return json.load(fh)["decisions"]

    def detectors_left(self, project=None):
        result = finisher.whats_missing(project_dict=json.loads(json.dumps(project or self.project)))
        return [f["detector"] for f in result["findings"]]


class BareStringIntentTests(_IntentCase):
    def test_bare_string_is_one_entry_not_a_list_of_characters(self):
        result = finisher.whats_missing(project_dict=self.project,
                                        intended_findings="uncommitted_ending")
        self.assertEqual(result["intent_recorded"], [
            {"detector": "uncommitted_ending", "bars": [9, 12], "status": "recorded"}])
        self.assertEqual(len(self.rows()), 1)
        self.assertNotIn("uncommitted_ending", self.detectors_left())

    def test_server_tool_wraps_a_bare_string_too(self):
        from mcp_server import server
        with mock.patch.object(finisher, "resolve_project", return_value=self.project):
            result = server.whats_missing("Song", intended_findings="uncommitted_ending@9-12")
        self.assertEqual([r["status"] for r in result["intent_recorded"]], ["recorded"])


class ProtectRowFieldTests(_IntentCase):
    def test_new_protect_rows_do_not_use_the_verdict_decision_field(self):
        finisher.whats_missing(project_dict=self.project, intended_findings=["uncommitted_ending"])
        row = self.rows()[0]
        self.assertNotIn("verdict", row)
        self.assertEqual(row["finding_status"], "intended")
        self.assertNotIn("verdict", piece_context.current_decisions("/music/Song.als"))

    def test_legacy_verdict_rows_still_silence_and_do_not_leak(self):
        finisher.whats_missing(project_dict=self.project, intended_findings=["uncommitted_ending"])
        with open(self.decisions) as fh:
            data = json.load(fh)
        legacy = data["decisions"][0]
        legacy.pop("finding_status")
        legacy["verdict"] = "intended"          # the row shape batch-5 r3 wrote
        with open(self.decisions, "w") as fh:
            json.dump(data, fh)
        self.assertNotIn("uncommitted_ending", self.detectors_left())
        again = finisher.whats_missing(project_dict=self.project,
                                       intended_findings=["uncommitted_ending"])
        self.assertEqual(again["intent_recorded"][0]["status"], "already_intended")
        self.assertNotIn("verdict", piece_context.current_decisions("/music/Song.als"))

    def test_a_real_verdict_decision_still_resolves(self):
        finisher.whats_missing(project_dict=self.project, intended_findings=["uncommitted_ending"])
        piece_context.record_decision("/music/Song.als", {"kind": "intent", "source": "owner",
                                                          "verdict": "keep"})
        self.assertEqual(piece_context.current_decisions("/music/Song.als")["verdict"], "keep")


class TruncatedLiveReadTests(_IntentCase):
    def test_intent_is_refused_when_the_live_read_was_truncated(self):
        truncated = dict(self.project, read_truncated={"read": 2, "total": 40})
        with self.assertRaisesRegex(ValueError, r"40 regular tracks.*covered only 2.*max_tracks of at least 40"):
            finisher.whats_missing(project_dict=truncated,
                                   intended_findings=["uncommitted_ending"])
        self.assertEqual(self.rows(), [])
        # the plain (truncated) diagnosis still runs
        self.assertIn("uncommitted_ending", self.detectors_left(truncated))

    def test_live_read_reports_truncation_into_the_project(self):
        from mcp_server import live_write, pp_session as pp

        def resp(obj):
            return pp.PPResponse(json.dumps({"result": obj}), obj, [])

        # Shape of a PP 2.2 live-set overview with three regular tracks.
        overview = {"name": "Tonight", "tempo": 120, "tracks": [
            {"trackIndex": i, "name": "T%d" % i, "type": "midi"} for i in range(3)]}

        class Session:
            async def call(self, name, args=None):
                if name == "ppal-live-api":
                    raise RuntimeError("Producer Pal tool error: Live API access is disabled")
                if name == "ppal-read-live-set":
                    return resp(overview)
                if name == "ppal-read-track":
                    return resp({"trackIndex": args["trackIndex"], "arrangementClips": []})
                raise AssertionError(name)

        @asynccontextmanager
        async def operation(*_args, **_kwargs):
            yield Session()

        with mock.patch.object(pp, "operation_session", operation):
            capped = asyncio.run(live_write.read_arrangement_tracks(2))
            full = asyncio.run(live_write.read_arrangement_tracks(3))
        self.assertEqual(capped["truncated"], {"read": 2, "total": 3})
        self.assertIsNone(full["truncated"])
        self.assertEqual(finisher.project_from_live_tracks(capped)["read_truncated"],
                         {"read": 2, "total": 3})
        self.assertNotIn("read_truncated", finisher.project_from_live_tracks(full))


if __name__ == "__main__":
    unittest.main()
