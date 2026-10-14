"""B11 round 3: a device moved to another track is not gone (unknown,
device_moved:<track>); closing another Set's night by id touches nothing.
Helpers and the V4Device fake come from the two round-1 modules."""
from __future__ import annotations

import unittest

from mcp_server import experiments, ledger
from tests.pp_fakes import on_both_versions
from tests.test_experiments_rows import (IDENTITY, SET_PATH, _LedgerCase, _live, _loc,
                                         _peak_evidence, _track)
from tests import test_experiments_tools as _tools

server = _tools.server


class DeviceMovedTests(_LedgerCase):
    """Round-3 small 1: a device that now sits on another track is not gone."""

    def test_moved_device_is_unknown_with_the_track_named(self):
        evidence = _peak_evidence()
        evidence["track_devices"] = {"21": [{"id": "2593", "name": "SIDEKICK duck ← drums",
                                             "type": "audio-effect: Compressor", "path": "t24/d4"}]}
        evidence["devices"] = {"2593": {"device_id": "2593", "id": "2593", "type": "audio-effect: Compressor",
                                        "name": "SIDEKICK duck ← drums",
                                        "path": "t24/d4", "params": {"Threshold": -12}}}
        cand = {"kind": experiments.EFFECT_KIND, "label": "duck on Bassline", "track_id": "21",
                "device_ids": ["2593"], "group": "sidechain", "move": "fx:device", "journal": None}
        row, why = experiments.verify_candidate(cand, evidence, expected_set_path=SET_PATH)
        (written,) = experiments.register([row], path=self.ledger_path)
        experiments.judge(written["id"], "keep", reason="ok", path=self.ledger_path)
        moved = _peak_evidence()
        moved["tracks"]["1113"] = _track("1113", "32-Omnisphere", 31)
        moved["track_devices"] = {"21": [{"id": "210", "name": "Bassline", "type": "instrument", "path": "t24/d0"}]}
        moved["devices"] = {"2593": {"device_id": "2593", "id": "2593", "type": "audio-effect: Compressor",
                                        "name": "SIDEKICK duck ← drums",
                                     "path": "t31/d3", "params": {"Threshold": -12}}}
        report = experiments.sweep(moved, path=self.ledger_path)
        row = experiments.find_row(ledger.load(self.ledger_path), written["id"])
        self.assertIsNone(row["terminal_state"])
        self.assertEqual(row["observation"]["reason"], "device_moved:32-Omnisphere")
        self.assertEqual(report["effect_trials"]["unknown"], 1)
        self.assertEqual(report["effect_trials"]["removed"], 0)


@on_both_versions
class Round3ToolTests(unittest.TestCase):
    """The V4Device loop (setUp and helpers borrowed, tests not inherited),
    for the round-3 items only."""
    setUp = _tools.ExperimentToolTests.setUp
    journal = _tools.ExperimentToolTests.journal
    rows = _tools.ExperimentToolTests.rows
    register_peak = _tools.ExperimentToolTests.register_peak

    def test_a_device_moved_to_another_track_is_unknown_not_removed(self):
        out = server.register_arrangement_candidates(journal_path=self.sidechain,
                                                     set_path=SET_PATH, confirm=True)
        ids = {c["label"]: c["entry_id"] for c in out["registered"]}
        server.judge_answer("keep", entry_id=ids["SIDEKICK duck \u2190 drums on 25-Bassline"],
                            reason="ok")
        self.device.move_device("2593", "21", "1113")
        close = server.close_session()
        line = close["effect_trials"]
        self.assertEqual((line["removed"], line["unknown"], line["resolved_judged"]), (0, 1, 0))
        row = experiments.find_row(ledger.load(self.ledger_path),
                                   ids["SIDEKICK duck \u2190 drums on 25-Bassline"])
        self.assertIsNone(row["terminal_state"])
        self.assertEqual(row["observation"]["reason"], "device_moved:32-Omnisphere")

    def test_closing_another_sets_night_by_id_touches_nothing(self):
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 2 rotation"], reason="2")
        night = experiments.find_row(ledger.load(self.ledger_path),
                                     ids["peak line 2 rotation"])["session_id"]
        self.device.file_path = "/Users/x/other/OTHER.als"
        out = server.close_session(session_id=night)
        self.assertFalse(out["ok"])
        self.assertFalse(out["experiments_closed"])
        self.assertIn("chain", out["experiments_refused"])
        self.assertEqual(out["arrangement"]["rows"], 0)
        for row in self.rows():
            self.assertIsNone(row["observation"])
            self.assertIsNone(row["terminal_state"])


if __name__ == "__main__":
    unittest.main()
