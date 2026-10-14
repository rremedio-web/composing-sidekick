"""B3 item 9 smalls: taste scope label, ears streak scope, finisher map,
junk floor. Synthetic pools/files throughout (one Live-mocked close)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcp_server import core_shared
from mcp_server import core as C
from mcp_server import ears, ledger, live_write, profiles, finisher
from mcp_server import server
from boringness import is_mechanical, mechanical_reason

NOTES = [
    {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90},
    {"pitch": 64, "start": 0.5, "duration": 0.5, "velocity": 85},
    {"pitch": 67, "start": 1.0, "duration": 1.0, "velocity": 90},
    {"pitch": 72, "start": 2.0, "duration": 2.0, "velocity": 88},
]
IDENTITY = {"set_id": "smalls-set", "runtime_epoch": "run-1",
            "file_path": "/tmp/smalls.als"}


class TasteScopeTests(unittest.TestCase):
    def test_summary_labels_diagnostic_scope(self):
        summary = C._taste_model_summary()
        self.assertEqual(summary["steering"],
                         "diagnostic — steers compose_from_profile only")


class EarsStreakScopeTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.path = str(Path(self._td.name) / "ears.json")
        with open(self.path, "w") as handle:
            json.dump({"streaks": {"density:busy": 2, "contour:flat": 1},
                       "demoted": ["density:busy"],
                       "judgment_events": {"e-old": {"fingerprint": "x"}}},
                      handle)

    def test_reset_streaks_keeps_event_guard(self):
        self.assertTrue(ears.reset_streaks(self.path))
        state = ears.load_session(self.path)
        self.assertEqual(state["streaks"], {})
        self.assertEqual(state["demoted"], [])
        self.assertIn("e-old", state.get("judgment_events", {}))

    def test_reset_streaks_missing_file_is_noop(self):
        self.assertFalse(ears.reset_streaks(
            str(Path(self._td.name) / "absent.json")))


class DefinitiveCloseResetsStreaksTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ledger_path = str(Path(self._td.name) / "ledger.json")
        self.ears_path = str(Path(self._td.name) / "ears.json")
        self._patches = [
            mock.patch.object(ledger, "LEDGER_PATH", self.ledger_path),
            mock.patch.object(ears, "SESSION_PATH", self.ears_path),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        prev = (core_shared._ratings_path()
                if core_shared._RATINGS_PATH_OVERRIDE else None)
        core_shared.set_ratings_path(
            str(Path(self._td.name) / "ratings.json"))
        self.addCleanup(core_shared.set_ratings_path, prev)
        entry = ledger.record_write(
            clip_name="row", slot="3/0", source="catalogue",
            answer_notes=NOTES, set_identity=IDENTITY,
            clip_ref={"clip_id": "c1", "track_id": "jam",
                      "track": "SIDEKICK JAM", "surface": "session",
                      "path": "t3/s0", "answer_notes": NOTES},
            path=self.ledger_path)
        ledger.record_verdict("drop", entry_id=entry["id"],
                              path=self.ledger_path)
        # end the night so the all-drop sweep is definitive
        with open(self.ledger_path) as handle:
            data = json.load(handle)
        now = ledger._now()
        data["sessions"][0]["ended_at"] = ledger._iso(now)
        data["sessions"][0]["last_activity"] = now - 10
        with open(self.ledger_path, "w") as handle:
            json.dump(data, handle)
        with open(self.ears_path, "w") as handle:
            json.dump({"streaks": {"density:busy": 2},
                       "demoted": ["density:busy"]}, handle)

    def test_definitive_close_resets_drop_streaks(self):
        inventory = {"complete": True, "set_identity": IDENTITY,
                     "clips": [], "errors": []}
        with mock.patch.object(server, "_run_async",
                               side_effect=lambda v: v), \
                mock.patch.object(live_write, "read_set_inventory",
                                  new=lambda: inventory):
            out = server.close_session()
        self.assertTrue(out.get("ok"), out)
        rows = ledger.load(self.ledger_path)["entries"]
        self.assertEqual(rows[0]["terminal_state"], "drop")
        state = ears.load_session(self.ears_path)
        self.assertEqual(state["streaks"], {})
        self.assertEqual(state["demoted"], [])
        self.assertTrue(out.get("ears_reset"))


class FinisherMapTests(unittest.TestCase):
    def test_commands_name_compose_profile_tools(self):
        tools = set(profiles.COMPOSE_TOOLS)
        for detector, (_i, _v, _s, command) in finisher._PROPOSAL_MAP.items():
            if command is None:
                continue
            self.assertIn(command["tool"], tools,
                          "%s proposes %r, not in the compose profile"
                          % (detector, command["tool"]))

    def test_uncommitted_ending_proposes_three_endings_with_last_section_seed(self):
        # Shape emitted by run_detectors for an uncommitted ending.
        finding = {"detector": "uncommitted_ending", "severity": "high",
                   "bars": [9, 12],
                   "evidence": "the final bars hold ≥80% of median density and then stop"}
        project = {
            "path": "/tmp/ending.als", "tempo": 120, "track_count": 1,
            "midi_track_names": ["Lead"], "audio_track_names": [],
            "clip_count": 2, "total_notes": 3,
            "clips": [
                # Shape emitted by ingest.parse_als for an arrangement MIDI clip.
                {"name": "earlier", "track": "Lead", "start": 0, "end": 40,
                 "note_count": 1, "clip_source": "arrangement",
                 # Looped 8-beat brace over a 40-beat pass: its fifth pass
                 # (beat 32) plays inside the final bars and IS in the seed.
                 "loop_start": 0, "loop_end": 8, "loop_on": True,
                 "audible_start": 0, "audible_end": 8, "audible_regions": None,
                 "start_marker": 0,
                 "notes": [{"pitch": 60, "start": 0, "duration": 1,
                            "velocity": 90, "enabled": True}]},
                {"name": "ending", "track": "Lead", "start": 32, "end": 48,
                 "note_count": 2, "clip_source": "arrangement",
                 # parse_als note times are clip-content beats. This parsed ALS
                 # shape has a non-zero start marker at beat 560.
                 "loop_start": 560, "loop_end": 568, "loop_on": False,
                 "audible_start": 560, "audible_end": 568, "audible_regions": None,
                 "notes": [{"pitch": 64, "start": 560, "duration": 1,
                            "velocity": 90, "enabled": True},
                           {"pitch": 67, "start": 564, "duration": 1,
                            "velocity": 90, "enabled": True}]},
            ],
        }
        proposal = finisher.propose([finding], project_dict=project)[0]
        self.assertEqual(proposal["command"]["tool"], "three_endings")
        self.assertEqual(proposal["command"]["args"]["seed_notes"], [
            {"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 90,
             "enabled": True},
            {"pitch": 64, "start": 0.0, "duration": 1.0, "velocity": 90,
             "enabled": True},
            {"pitch": 67, "start": 4.0, "duration": 1.0, "velocity": 90,
             "enabled": True},
        ])
        result = server.three_endings(**proposal["command"]["args"])
        self.assertEqual([ending["name"] for ending in result["endings"]],
                         ["decay", "subtraction", "hard_stop"])
        self.assertTrue(all(ending["notes"] for ending in result["endings"]))
        self.assertIn("last-section", proposal["strategy"])

    def test_uncommitted_ending_without_overlapping_midi_keeps_instruction(self):
        finding = {"detector": "uncommitted_ending", "severity": "ask",
                   "bars": [9, 12], "evidence": "MIDI only"}
        project = {"clips": []}
        proposal = finisher.propose([finding], project_dict=project)[0]
        self.assertIsNone(proposal["command"])
        self.assertIn("select the project's last-section MIDI clip", proposal["strategy"])


class JunkFloorTests(unittest.TestCase):
    def test_period_two_over_eight_is_mechanical(self):
        degs = [0, 7] * 5  # exact trill, 10 onsets
        self.assertTrue(is_mechanical(degs, {"unigram": {"0": 0.5, "7": 0.5}}))
        self.assertEqual(
            mechanical_reason(degs, {"unigram": {"0": 0.5, "7": 0.5}}),
            "periodic")

    def test_period_three_not_flagged_by_period_arm(self):
        degs = [0, 4, 7] * 4  # 12 onsets, period 3
        # a grammar that finds every degree surprising: the surprise arm
        # stays quiet, so only the period arm could fire (and must not)
        grammar = {"unigram": {"0": 0.01, "4": 0.01, "7": 0.01}}
        self.assertFalse(is_mechanical(degs, grammar))
        self.assertIsNone(mechanical_reason(degs, grammar))

    def test_short_period_two_not_flagged(self):
        degs = [0, 7] * 3  # 6 onsets: below the onset floor
        grammar = {"unigram": {"0": 0.01, "7": 0.01}}
        self.assertFalse(is_mechanical(degs, grammar))

    def test_bank_tonic_prefers_project_key(self):
        saved = dict(core_shared._CACHE)
        core_shared._CACHE["project_keys"] = {"synth": "D minor"}
        self.addCleanup(self._restore_keys, saved)
        nd = [{"pitch": 62, "start": 0.0, "duration": 0.5, "velocity": 90},
              {"pitch": 64, "start": 0.5, "duration": 0.5, "velocity": 90}]
        # D minor -> tonic pc 2, not the phrase's own pitch-class mode (0)
        self.assertEqual(
            C._bank_tonic_pc({"project": "synth"}, nd), 2)
        self.assertEqual(
            C._bank_tonic_pc({"project": "unknown-proj"}, nd),
            C._phrase_tonic_pc(nd))

    def _restore_keys(self, saved):
        core_shared._CACHE.pop("project_keys", None)
        for k, v in saved.items():
            core_shared._CACHE[k] = v


if __name__ == "__main__":
    unittest.main()
