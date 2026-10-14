"""B11 — arrangement experiments / effect trials: journals, verification,
terminal-state contract, judgment routing and the separate subtotal.

Pure module tests: no Live, no real store (temp ledger + temp piece
decisions). The evidence envelopes here are hand-built in the shape
``live_write.read_experiment_evidence`` returns; the tool-level tests drive
that reader through a 2.3-shaped fake.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from mcp_server import experiments, ledger, piece_context

SET_PATH = "/Users/x/Example Project/NOCTURNEv4.als"
IDENTITY = {"set_id": SET_PATH, "runtime_epoch": "root-a|4242|start", "file_path": SET_PATH}


def _notes(*pitches):
    return [{"note_id": i + 1, "pitch": p, "start_time": float(i), "duration": 0.5,
             "velocity": 90.0, "mute": 0, "probability": 1.0, "velocity_deviation": 0.0,
             "release_velocity": 0.0} for i, p in enumerate(pitches)]


_TRACK_INDEX_OF = {"2565": 40, "2566": 41, "2567": 42, "2584": 10, "90001": 40,
                   "3565": 40, "3566": 41, "3599": 41, "2599": 41, "90": 5}


def _live(clip_id, *pitches, start=472.0, length=96.0, muted=False, name="x", track_index=None):
    if track_index is None:
        track_index = _TRACK_INDEX_OF.get(str(clip_id), 0)
    from mcp_server import clip_playback
    props = {"name": name, "loop_start": 0.0, "loop_end": length, "start_marker": 0.0,
             "end_marker": length, "looping": True, "start_time": start,
             "end_time": start + length, "signature_numerator": 4,
             "signature_denominator": 4, "is_midi_clip": True, "muted": muted}
    return {"clip_id": clip_id, "props": props, "frame": clip_playback.frame_from_live(props),
            "notes": [clip_playback.note_from_live(n) for n in _notes(*pitches)],
            "object_path": "live_set tracks %d arrangement_clips 0" % track_index}


def _evidence(*, tracks, locations, clips, track_devices=None, devices=None,
              identity=IDENTITY, complete=True, errors=()):
    return {"set_identity": dict(identity) if identity else None, "complete": complete,
            "errors": list(errors), "tracks": tracks, "clip_locations": locations,
            "clips": clips, "track_devices": track_devices or {}, "devices": devices or {}}


def _track(tid, name, index, muted=False):
    return {"id": tid, "name": name, "index": index, "muted": muted, "soloed": False}


def _group(gid, name, muted=False, soloed=False):
    return {"id": gid, "name": name, "index": None, "muted": muted, "soloed": soloed,
            "group_id": None}


def _loc(cid, tid, name, path):
    return {"track_id": tid, "track": name, "surface": "arrangement", "path": path}


PEAK_JOURNAL = {
    "set": SET_PATH,
    "tracks": {
        "drums": {"id": "2444", "readback": ["SIDEKICK – peak drums (Ritual cell)", 0]},
        "1 space": {"id": "2483", "path": "t40", "readback": ["SIDEKICK – peak line 1 space", 0]},
        "2 rotation": {"id": "2515", "path": "t41", "readback": ["SIDEKICK – peak line 2 rotation", 1]},
        "3 climb": {"id": "2537", "path": "t42", "readback": ["SIDEKICK – peak line 3 climb", 1]},
    },
    "clips": {
        "drums": {"id": "2564", "path": "t10[118|1]", "noteCount": 173},
        "1 space": {"id": "2565", "path": "t40[119|1]", "noteCount": 16, "existing": True},
        "2 rotation": {"id": "2566", "path": "t41[119|1]", "noteCount": 59},
        "3 climb": {"id": "2567", "path": "t42[119|1]", "noteCount": 15},
    },
    "paths_after": {"2444": ["t10", "SIDEKICK – peak drums (Ritual cell)"]},
    "brace": [1, 464, 108, 0],
    "note": "refer to tracks by id",
    "composition": {"variants": {}},
}

IMPACT_JOURNAL = {
    "track": "2444",
    "deleted": ["2570", "2571"],
    "new": {
        "weight": {"id": "2584", "path": "t10[41|1]", "noteCount": 17},
        "riff": {"id": "2591", "path": "t10[55|1]", "noteCount": 46},
        "drift": {"id": "2586", "path": "t10[79|1]", "noteCount": 9},
    },
    "gone_before_write": ["2574"],
    # deliberately STALE: the riff clip is 2591, not 2585 (the real journal
    # carried this before the riff re-lock) — registration must never read it
    "final_clips": [["2584", "t10[41|1]"], ["2585", "t10[55|1]"], ["2586", "t10[79|1]"]],
    "track_state": ["SIDEKICK – drums, whole track (impact)", 0],
}

SIDECHAIN_JOURNAL = {
    "before": {"21": {"path": "t24", "name": "25-Bassline", "devices": [["210", None, "instrument: Bassline"]]},
               "1113": {"path": "t31", "name": "32-Omnisphere", "devices": []}},
    "made": {"21": {"id": "2593", "path": "t24/d4"}, "1113": {"id": "2617", "path": "t31/d3"}},
    "after_devices": {"21": [["210", None], ["2593", "SIDEKICK duck ← drums"]],
                      "1113": [["1853", None], ["2617", "SIDEKICK duck ← drums"]]},
    "params": {"2593": {"Threshold": -12}, "2617": {"Threshold": -12}},
    "routing_status": "UNSET",
}


class JournalParsingTests(unittest.TestCase):
    def test_peak_journal_parses_every_variant_by_track_and_clip_id(self):
        parsed = experiments.parse_journal(PEAK_JOURNAL, journal="v4-peak-compose.json")
        self.assertEqual(parsed["set_path"], SET_PATH)
        by_label = {c["label"]: c for c in parsed["items"]}
        self.assertEqual(set(by_label), {"peak drums (Ritual cell)", "peak line 1 space",
                                         "peak line 2 rotation", "peak line 3 climb"})
        self.assertEqual(by_label["peak line 2 rotation"]["track_id"], "2515")
        self.assertEqual(by_label["peak line 2 rotation"]["clip_ids"], ["2566"])
        self.assertEqual(by_label["peak line 2 rotation"]["kind"], experiments.ARRANGEMENT_KIND)
        self.assertEqual(by_label["peak line 2 rotation"]["group"], "v4-peak-compose")
        self.assertEqual(by_label["peak line 2 rotation"]["move"], "arr:variant")

    def test_impact_journal_takes_clip_ids_from_the_write_receipts_never_final_clips(self):
        parsed = experiments.parse_journal(IMPACT_JOURNAL, journal="impact-drums.json")
        self.assertIsNone(parsed["set_path"])
        (item,) = parsed["items"]
        self.assertEqual(item["track_id"], "2444")
        self.assertEqual(item["clip_ids"], ["2584", "2591", "2586"])
        self.assertNotIn("2585", item["clip_ids"])
        self.assertEqual(item["label"], "drums, whole track (impact)")
        self.assertEqual(item["move"], "arr:whole_track")

    def test_sidechain_journal_yields_one_effect_trial_per_device(self):
        parsed = experiments.parse_journal(SIDECHAIN_JOURNAL, journal="sidechain.json")
        items = sorted(parsed["items"], key=lambda c: c["track_id"])
        self.assertEqual([c["kind"] for c in items], [experiments.EFFECT_KIND] * 2)
        self.assertEqual([(c["track_id"], c["device_ids"]) for c in items],
                         [("1113", ["2617"]), ("21", ["2593"])])
        self.assertEqual(items[0]["label"], "SIDEKICK duck ← drums on 32-Omnisphere")
        self.assertEqual(items[0]["move"], "fx:device")

    def test_unknown_journal_shape_is_refused(self):
        with self.assertRaisesRegex(ValueError, "journal shape"):
            experiments.parse_journal({"hello": 1}, journal="x.json")


def _missing(cid):
    """What the reader records for an id Live says does not exist."""
    return {"clip_id": cid, "error": 'PPToolError: Error: id "%s" does not exist' % cid,
            "missing": True}


def _peak_evidence(*, muted_2515=True, clips_2515=("2566",), identity=IDENTITY, complete=True,
                   errors=(), gone=()):
    tracks = {"2483": _track("2483", "SIDEKICK – peak line 1 space", 40),
              "2515": _track("2515", "SIDEKICK – peak line 2 rotation", 41, muted=muted_2515),
              "2444": _track("2444", "SIDEKICK – drums, whole track (impact)", 10),
              "21": _track("21", "25-Bassline", 24)}
    locations = {"2565": _loc("2565", "2483", tracks["2483"]["name"], "t40[119|1]"),
                 "2584": _loc("2584", "2444", tracks["2444"]["name"], "t10[41|1]")}
    clips = {"2565": _live("2565", 81, 83, 85), "2584": _live("2584", 36, 38, start=160.0)}
    for cid in clips_2515:
        locations[cid] = _loc(cid, "2515", tracks["2515"]["name"], "t41[119|1]")
        clips[cid] = _live(cid, 69, 71, 73, 74)
    for cid in set(gone) | ({"2566"} - set(clips_2515)):
        clips[cid] = _missing(cid)      # deleted, and its id not reused
    return _evidence(tracks=tracks, locations=locations, clips=clips, identity=identity,
                     complete=complete, errors=errors)


class VerificationTests(unittest.TestCase):
    def cand(self, label="peak line 2 rotation", track_id="2515", clip_ids=("2566",)):
        return {"kind": experiments.ARRANGEMENT_KIND, "label": label, "track_id": track_id,
                "clip_ids": list(clip_ids), "group": "v4-peak-compose", "move": "arr:variant",
                "journal": "v4-peak-compose.json"}

    def test_verified_candidate_carries_digest_placement_mute_and_identity(self):
        row, why = experiments.verify_candidate(self.cand(), _peak_evidence(),
                                                expected_set_path=SET_PATH)
        self.assertIsNone(why)
        self.assertEqual(row["kind"], experiments.ARRANGEMENT_KIND)
        self.assertEqual(row["set_identity"], IDENTITY)
        self.assertEqual(row["piece"], SET_PATH)
        self.assertEqual(row["track"], {"id": "2515", "name": "SIDEKICK – peak line 2 rotation",
                                        "index": 41, "muted": True, "sidekick": True})
        (clip,) = row["clips"]
        self.assertEqual(clip["clip_id"], "2566")
        self.assertEqual(clip["n_notes"], 4)
        self.assertEqual((clip["arr_start"], clip["arr_end"]), (472.0, 568.0))
        self.assertFalse(clip["muted"])
        self.assertEqual(len(clip["digest"]), 64)
        self.assertTrue(row["recovered"])
        self.assertEqual(row["source"], "recovered")

    def test_digest_ignores_placement_and_mutes_but_not_notes(self):
        same = experiments.content_digest(_live("a", 60, 62, start=0.0, muted=True))
        self.assertEqual(same, experiments.content_digest(_live("b", 60, 62, start=99.0)))
        self.assertNotEqual(same, experiments.content_digest(_live("c", 60, 63, start=0.0)))

    def test_refuses_when_the_open_set_is_another_file(self):
        other = dict(IDENTITY, set_id="/elsewhere.als", file_path="/elsewhere.als")
        row, why = experiments.verify_candidate(self.cand(), _peak_evidence(identity=other),
                                                expected_set_path=SET_PATH)
        self.assertIsNone(row)
        self.assertIn("/elsewhere.als", why)
        self.assertIn(SET_PATH, why)

    def test_refuses_on_incomplete_inventory(self):
        row, why = experiments.verify_candidate(
            self.cand(), _peak_evidence(complete=False, errors=["track 3 read failed: boom"]),
            expected_set_path=SET_PATH)
        self.assertIsNone(row)
        self.assertIn("incomplete", why)
        self.assertIn("boom", why)

    def test_refuses_a_clip_the_track_no_longer_holds(self):
        # the journal says 2564 on the drums track; the Set holds 2584 there now
        row, why = experiments.verify_candidate(
            self.cand("peak drums (Ritual cell)", "2444", ["2564"]), _peak_evidence(),
            expected_set_path=SET_PATH)
        self.assertIsNone(row)
        self.assertIn("2564", why)
        self.assertIn("not in the Set", why)

    def test_refuses_a_clip_that_sits_on_another_track(self):
        row, why = experiments.verify_candidate(
            self.cand("x", "2515", ["2565"]), _peak_evidence(), expected_set_path=SET_PATH)
        self.assertIsNone(row)
        self.assertIn("2565", why)
        self.assertIn("2483", why)

    def test_refuses_an_unknown_track(self):
        row, why = experiments.verify_candidate(
            self.cand("x", "9999", ["2566"]), _peak_evidence(), expected_set_path=SET_PATH)
        self.assertIsNone(row)
        self.assertIn("9999", why)

    def test_refuses_when_a_clip_read_failed(self):
        evidence = _peak_evidence()
        evidence["clips"]["2566"] = {"clip_id": "2566", "error": "ValueError: incomplete"}
        row, why = experiments.verify_candidate(self.cand(), evidence, expected_set_path=SET_PATH)
        self.assertIsNone(row)
        self.assertIn("incomplete", why)

    def test_extra_clips_on_the_track_are_reported_not_registered(self):
        evidence = _peak_evidence(clips_2515=("2566", "2599"))
        row, why = experiments.verify_candidate(self.cand(), evidence, expected_set_path=SET_PATH)
        self.assertIsNone(why)
        self.assertEqual([c["clip_id"] for c in row["clips"]], ["2566"])
        self.assertEqual(row["unregistered_on_track"], ["2599"])

    def test_effect_trial_verified_on_an_owner_track_with_params(self):
        evidence = _peak_evidence()
        evidence["track_devices"] = {"21": [{"id": "210", "name": "Bassline", "type": "instrument", "path": "t24/d0"},
                                            {"id": "2593", "name": "SIDEKICK duck ← drums",
                                             "type": "audio-effect: Compressor", "path": "t24/d4"}]}
        evidence["devices"] = {"2593": {"device_id": "2593", "id": "2593", "type": "audio-effect: Compressor",
                                        "name": "SIDEKICK duck ← drums",
                                        "path": "t24/d4", "params": {"Threshold": -12, "S/C On": "On"}}}
        cand = {"kind": experiments.EFFECT_KIND, "label": "SIDEKICK duck ← drums on 25-Bassline",
                "track_id": "21", "device_ids": ["2593"], "group": "sidechain",
                "move": "fx:device", "journal": "sidechain.json"}
        row, why = experiments.verify_candidate(cand, evidence, expected_set_path=SET_PATH)
        self.assertIsNone(why)
        self.assertEqual(row["kind"], experiments.EFFECT_KIND)
        self.assertFalse(row["track"]["sidekick"])
        (device,) = row["devices"]
        self.assertEqual(device["device_id"], "2593")
        self.assertEqual(device["params"]["Threshold"], -12)
        self.assertEqual(len(device["params_digest"]), 64)

    def test_effect_trial_refuses_a_device_missing_from_the_track(self):
        evidence = _peak_evidence()
        evidence["track_devices"] = {"21": [{"id": "210", "name": "Bassline", "type": "instrument", "path": "t24/d0"}]}
        cand = {"kind": experiments.EFFECT_KIND, "label": "x", "track_id": "21",
                "device_ids": ["2593"], "group": "sidechain", "move": "fx:device", "journal": None}
        row, why = experiments.verify_candidate(cand, evidence, expected_set_path=SET_PATH)
        self.assertIsNone(row)
        self.assertIn("2593", why)


class _LedgerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger_path = os.path.join(self._tmp.name, "ledger.json")
        self.decisions_path = os.path.join(self._tmp.name, "piece_decisions.json")
        for target, value in ((ledger, "LEDGER_PATH"),):
            patcher = mock.patch.object(target, value, self.ledger_path)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(piece_context, "_decision_path",
                                    lambda path=None: path or self.decisions_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def register_peak(self, labels=("peak line 1 space", "peak line 2 rotation"),
                      evidence=None):
        evidence = evidence or _peak_evidence()
        parsed = experiments.parse_journal(PEAK_JOURNAL, journal="v4-peak-compose.json")
        rows = []
        for cand in parsed["items"]:
            if cand["label"] not in labels:
                continue
            row, why = experiments.verify_candidate(cand, evidence, expected_set_path=SET_PATH)
            self.assertIsNone(why, why)
            rows.append(row)
        return experiments.register(rows, path=self.ledger_path)

    def decisions(self):
        try:
            with open(self.decisions_path) as fh:
                return json.load(fh)["decisions"]
        except FileNotFoundError:
            return []


class RegistrationLedgerTests(_LedgerCase):
    def test_rows_land_outside_the_phrase_entries_with_ids_and_a_session(self):
        written = self.register_peak()
        data = ledger.load(self.ledger_path)
        self.assertEqual(data["entries"], [])
        self.assertEqual(len(data["arrangement_experiments"]), 2)
        self.assertEqual(data["effect_trials"], [])
        ids = [row["id"] for row in written]
        self.assertTrue(all(i.startswith("arr-") for i in ids))
        session = data["sessions"][-1]
        self.assertEqual({row["session_id"] for row in written}, {session["id"]})
        self.assertEqual(session["set_identity"], IDENTITY)
        self.assertEqual(ledger.summary(self.ledger_path)["entries"], 0)
        self.assertEqual(ledger.summary(self.ledger_path)["judged"], 0)

    def test_registration_after_the_legacy_identity_less_night_opens_a_new_session(self):
        # the 2026-09-02 night: identity-less rows, long idle
        ledger.record_write(clip_name="legacy", slot="3/0", source="catalogue",
                            path=self.ledger_path)
        data = ledger.load(self.ledger_path)
        legacy = data["sessions"][-1]
        legacy["last_activity"] -= 30 * 24 * 3600
        ledger._save(data, self.ledger_path)
        written = self.register_peak()
        data = ledger.load(self.ledger_path)
        self.assertEqual(len(data["sessions"]), 2)
        self.assertNotEqual(written[0]["session_id"], legacy["id"])
        self.assertEqual(data["sessions"][0]["ended_by"], "idle")

    def test_effect_rows_are_a_separate_record(self):
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
        data = ledger.load(self.ledger_path)
        self.assertTrue(written["id"].startswith("fx-"))
        self.assertEqual(data["arrangement_experiments"], [])
        self.assertEqual([r["id"] for r in data["effect_trials"]], [written["id"]])
        self.assertEqual(data["entries"], [])


class JudgmentTests(_LedgerCase):
    def test_keep_stamps_verdict_reason_time_and_intent_revision(self):
        one, two = self.register_peak()
        out = experiments.judge(two["id"], "keep", reason="the rotation breathes",
                                path=self.ledger_path)
        self.assertEqual(out["verdict"], "keep")
        self.assertEqual(out["entry_id"], two["id"])
        self.assertEqual(out["label"], "peak line 2 rotation")
        row = experiments.find_row(ledger.load(self.ledger_path), two["id"])
        self.assertEqual(row["immediate_verdict"], "keep")
        self.assertEqual(row["reason"], "the rotation breathes")
        self.assertEqual(row["intent_revision"], "the rotation breathes")
        self.assertTrue(row["judged_at"].endswith("Z"))
        self.assertIsNone(row["terminal_state"])

    def test_selecting_one_variant_judges_that_variant_only_and_says_so(self):
        one, two = self.register_peak()
        out = experiments.judge(two["id"], "keep", reason="2", path=self.ledger_path)
        self.assertEqual(out["siblings_unjudged"], [{"entry_id": one["id"],
                                                     "label": "peak line 1 space"}])
        self.assertIn("stay unjudged", out["note"])
        row = experiments.find_row(ledger.load(self.ledger_path), one["id"])
        self.assertIsNone(row["immediate_verdict"])

    def test_feedback_is_model_level_evidence_in_piece_decisions(self):
        one, two = self.register_peak()
        experiments.judge(two["id"], "drop", reason="too busy", path=self.ledger_path)
        (record,) = self.decisions()
        self.assertEqual(record["kind"], "arrangement_feedback")
        self.assertEqual(record["source"], "model")
        self.assertEqual(record["set_key"], SET_PATH)
        self.assertEqual(record["verdict"], "drop")
        self.assertEqual(record["reason"], "too busy")
        self.assertEqual(record["label"], "peak line 2 rotation")
        self.assertEqual(record["entry_id"], two["id"])
        self.assertEqual(record["experiment"], experiments.ARRANGEMENT_KIND)
        # owner corrections outrank it: a model verdict never becomes the Set's
        # verdict, and an owner intent record wins any field it shares
        piece_context.record_decision(SET_PATH, {"kind": "intent", "source": "owner",
                                                 "goal": "keep the peak sparse"},
                                      path=self.decisions_path)
        current = piece_context.current_decisions(SET_PATH, path=self.decisions_path)
        self.assertEqual(current["goal"], "keep the peak sparse")
        self.assertNotIn("verdict", current)

    def test_judge_by_exact_label(self):
        one, two = self.register_peak()
        out = experiments.judge("peak line 1 space", "keep", reason=None, path=self.ledger_path)
        self.assertEqual(out["entry_id"], one["id"])

    def test_unknown_handle_returns_none_so_the_phrase_path_runs(self):
        self.register_peak()
        self.assertIsNone(experiments.judge("e-nothere", "keep", reason=None,
                                            path=self.ledger_path))

    def test_conflicting_verdict_is_refused_and_same_verdict_replays(self):
        one, two = self.register_peak()
        experiments.judge(two["id"], "keep", reason="a", path=self.ledger_path)
        with self.assertRaisesRegex(ValueError, "contradicts"):
            experiments.judge(two["id"], "drop", reason="b", path=self.ledger_path)
        again = experiments.judge(two["id"], "keep", reason="a", path=self.ledger_path)
        self.assertTrue(again["replay"])
        self.assertEqual(len(self.decisions()), 1)

    def test_verdict_after_a_terminal_state_is_refused(self):
        one, two = self.register_peak()
        experiments.judge(two["id"], "keep", reason="a", path=self.ledger_path)
        experiments.sweep(_peak_evidence(muted_2515=False), path=self.ledger_path)
        with self.assertRaisesRegex(ValueError, "adopted"):
            experiments.judge(two["id"], "keep", reason="a", path=self.ledger_path)


class TerminalContractTests(_LedgerCase):
    """The written-down contract (experiments.py docstring), one test per row."""

    def keep(self, *labels):
        rows = self.register_peak()
        for row in rows:
            if row["label"] in labels:
                experiments.judge(row["id"], "keep", reason="yes", path=self.ledger_path)
        return {row["label"]: row["id"] for row in rows}

    def state_of(self, entry_id):
        row = experiments.find_row(ledger.load(self.ledger_path), entry_id)
        return row["terminal_state"], row["observation"]

    def test_adopted_when_unmuted_on_the_sidekick_track_and_unchanged(self):
        ids = self.keep("peak line 2 rotation")
        experiments.sweep(_peak_evidence(muted_2515=False), path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 2 rotation"])
        self.assertEqual(state, "adopted")
        self.assertTrue(obs["audible"])
        self.assertTrue(obs["unmute_observed"])
        self.assertEqual(obs["reason"], "unmuted_since_registration")

    def test_staged_kept_when_still_muted(self):
        ids = self.keep("peak line 2 rotation")
        experiments.sweep(_peak_evidence(muted_2515=True), path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 2 rotation"])
        self.assertEqual(state, "staged_kept")
        self.assertFalse(obs["audible"])

    def test_staged_kept_when_the_clip_itself_is_deactivated(self):
        ids = self.keep("peak line 1 space")
        evidence = _peak_evidence()
        evidence["clips"]["2565"] = _live("2565", 81, 83, 85, muted=True)
        experiments.sweep(evidence, path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 1 space"])
        self.assertEqual(state, "staged_kept")

    def test_adopted_when_the_clip_now_lives_on_his_track(self):
        ids = self.keep("peak line 2 rotation")
        evidence = _peak_evidence(muted_2515=True)
        evidence["clip_locations"]["2566"] = _loc("2566", "21", "25-Bassline", "t24[119|1]")
        evidence["clips"]["2566"] = _live("2566", 69, 71, 73, 74, track_index=24)
        experiments.sweep(evidence, path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 2 rotation"])
        self.assertEqual(state, "adopted")
        self.assertEqual(obs["reason"], "on_his_originals")
        self.assertEqual(obs["where"], {"2566": "21"})

    def test_not_adopted_when_every_clip_is_gone(self):
        ids = self.keep("peak line 2 rotation")
        experiments.sweep(_peak_evidence(clips_2515=()), path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 2 rotation"])
        self.assertEqual(state, "not_adopted")
        self.assertEqual(obs["absent"], ["2566"])

    def test_not_adopted_when_the_track_is_gone(self):
        ids = self.keep("peak line 2 rotation")
        evidence = _peak_evidence(clips_2515=())
        del evidence["tracks"]["2515"]
        experiments.sweep(evidence, path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 2 rotation"])
        self.assertEqual(state, "not_adopted")
        self.assertEqual(obs["reason"], "track_gone")

    def test_unknown_on_identity_mismatch(self):
        ids = self.keep("peak line 2 rotation")
        other = dict(IDENTITY, set_id="/other.als", file_path="/other.als")
        # another Set's evidence never reaches this night by default …
        experiments.sweep(_peak_evidence(identity=other), path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 2 rotation"])
        self.assertIsNone(state)
        self.assertIsNone(obs)
        # … and named explicitly it can only say "not this Set"
        session_id = ledger.load(self.ledger_path)["sessions"][-1]["id"]
        experiments.sweep(_peak_evidence(identity=other), session_ids=[session_id],
                          path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 2 rotation"])
        self.assertIsNone(state)
        self.assertEqual(obs["state"], "unknown")
        self.assertEqual(obs["reason"], "identity_mismatch")

    def test_unknown_on_incomplete_inventory(self):
        ids = self.keep("peak line 2 rotation")
        experiments.sweep(_peak_evidence(complete=False, errors=["track 4 read failed"]),
                          path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 2 rotation"])
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "inventory_incomplete")

    def test_unknown_on_partial_presence(self):
        evidence = _peak_evidence(clips_2515=("2566", "2599"))
        parsed = experiments.parse_journal(PEAK_JOURNAL, journal="j.json")
        cand = next(c for c in parsed["items"] if c["track_id"] == "2515")
        cand["clip_ids"] = ["2566", "2599"]
        row, why = experiments.verify_candidate(cand, evidence, expected_set_path=SET_PATH)
        (written,) = experiments.register([row], path=self.ledger_path)
        experiments.judge(written["id"], "keep", reason="x", path=self.ledger_path)
        experiments.sweep(_peak_evidence(clips_2515=("2566",), gone=("2599",)),
                          path=self.ledger_path)
        state, obs = self.state_of(written["id"])
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "partial_presence")
        self.assertEqual(obs["absent"], ["2599"])

    def test_unknown_when_the_content_changed(self):
        ids = self.keep("peak line 2 rotation")
        evidence = _peak_evidence(muted_2515=False)
        evidence["clips"]["2566"] = _live("2566", 69, 71, 73, 75)
        experiments.sweep(evidence, path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 2 rotation"])
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "content_changed")
        self.assertEqual(obs["changed"], ["2566"])

    def test_a_moved_clip_still_counts_and_is_noted(self):
        ids = self.keep("peak line 2 rotation")
        evidence = _peak_evidence(muted_2515=False)
        evidence["clips"]["2566"] = _live("2566", 69, 71, 73, 74, start=480.0)
        experiments.sweep(evidence, path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 2 rotation"])
        self.assertEqual(state, "adopted")
        self.assertEqual(obs["moved"], ["2566"])

    def test_drop_is_terminal_drop_even_when_present_and_audible(self):
        rows = self.register_peak()
        two = next(r for r in rows if r["label"] == "peak line 2 rotation")
        experiments.judge(two["id"], "drop", reason="no", path=self.ledger_path)
        experiments.sweep(_peak_evidence(muted_2515=False), path=self.ledger_path)
        state, obs = self.state_of(two["id"])
        self.assertEqual(state, "drop")
        self.assertEqual(obs["reason"], "owner_drop")

    def test_unjudged_present_stays_pending_and_unjudged_absent_is_abandoned(self):
        ids = self.keep()  # nothing judged
        experiments.sweep(_peak_evidence(clips_2515=()), path=self.ledger_path)
        self.assertEqual(self.state_of(ids["peak line 1 space"])[0], None)
        self.assertEqual(self.state_of(ids["peak line 1 space"])[1]["state"], "unjudged")
        self.assertEqual(self.state_of(ids["peak line 2 rotation"])[0], "abandoned")

    def test_a_later_complete_close_resolves_what_an_incomplete_one_left_unknown(self):
        ids = self.keep("peak line 2 rotation")
        experiments.sweep(_peak_evidence(complete=False, errors=["x"]), path=self.ledger_path)
        self.assertIsNone(self.state_of(ids["peak line 2 rotation"])[0])
        experiments.sweep(_peak_evidence(muted_2515=False), path=self.ledger_path)
        self.assertEqual(self.state_of(ids["peak line 2 rotation"])[0], "adopted")

    def test_identity_through_the_session_alias_chain_still_resolves(self):
        ids = self.keep("peak line 2 rotation")
        data = ledger.load(self.ledger_path)
        data["sessions"][-1]["set_aliases"].append({"set_id": "/saved-as.als",
                                                    "alias_kind": "save_as", "at": "x"})
        ledger._save(data, self.ledger_path)
        aliased = dict(IDENTITY, set_id="/saved-as.als", file_path="/saved-as.als")
        experiments.sweep(_peak_evidence(muted_2515=False, identity=aliased),
                          path=self.ledger_path)
        self.assertEqual(self.state_of(ids["peak line 2 rotation"])[0], "adopted")


class AudibilityTests(_LedgerCase):
    """Two-month review (2026-10-02) finding 6: ``adopted`` means audible, not
    merely unmuted. Effective audibility = not muted, no muted ancestor, AND
    (no track soloed in the Set, or this track / an ancestor is the one
    soloed); a parent group the evidence lacks proves nothing, nor does a
    track state the captures never showed. ``observation.audibility`` says
    which."""

    def keep_line_2(self):
        rows = self.register_peak()
        two = next(r for r in rows if r["label"] == "peak line 2 rotation")
        experiments.judge(two["id"], "keep", reason="2", path=self.ledger_path)
        return two["id"]

    def close_with(self, evidence):
        entry_id = self.keep_line_2()
        experiments.sweep(evidence, path=self.ledger_path)
        row = experiments.find_row(ledger.load(self.ledger_path), entry_id)
        obs = row["observation"]
        return row["terminal_state"], obs["audible"], obs["audibility"], obs["reason"]

    def test_unmuted_with_no_solo_in_the_set_is_audible(self):
        # registered muted (register_peak's default evidence), unmuted since
        self.assertEqual(self.close_with(_peak_evidence(muted_2515=False)),
                         ("adopted", True, "audible", "unmuted_since_registration"))

    def test_an_unrelated_solo_silences_the_kept_line(self):
        # the review's probe: KEEP the line, solo Bassline, close — it read
        # adopted / still_audible_on_sidekick while nothing of it could be heard
        evidence = _peak_evidence(muted_2515=False)
        evidence["tracks"]["21"]["soloed"] = True
        self.assertEqual(self.close_with(evidence),
                         ("staged_kept", False, "silent_by_solo", "present_but_silent"))

    def test_the_track_itself_soloed_is_audible(self):
        evidence = _peak_evidence(muted_2515=False)
        evidence["tracks"]["21"]["soloed"] = True
        evidence["tracks"]["2515"]["soloed"] = True
        self.assertEqual(self.close_with(evidence)[:3], ("adopted", True, "audible"))

    def test_a_soloed_parent_group_keeps_its_member_audible(self):
        evidence = _peak_evidence(muted_2515=False)
        evidence["tracks"]["2515"]["group_id"] = "g1"
        evidence["groups"] = {"g1": _group("g1", "PEAK", soloed=True)}
        self.assertEqual(self.close_with(evidence)[:3], ("adopted", True, "audible"))

    def test_a_soloed_sibling_in_the_group_silences_the_member(self):
        evidence = _peak_evidence(muted_2515=False)
        evidence["groups"] = {"g1": _group("g1", "PEAK")}
        for tid in ("2515", "2483"):
            evidence["tracks"][tid]["group_id"] = "g1"
        evidence["tracks"]["2483"]["soloed"] = True
        self.assertEqual(self.close_with(evidence)[:3], ("staged_kept", False, "silent_by_solo"))

    def test_a_soloed_group_elsewhere_silences_it_too(self):
        evidence = _peak_evidence(muted_2515=False)
        evidence["tracks"]["21"]["group_id"] = "g2"
        evidence["groups"] = {"g2": _group("g2", "SYNTHS", soloed=True)}
        self.assertEqual(self.close_with(evidence)[:3], ("staged_kept", False, "silent_by_solo"))

    def test_a_parent_group_the_evidence_lacks_is_never_audible(self):
        # the review's probe: group_id g1 with no group read — it read adopted
        evidence = _peak_evidence(muted_2515=False)
        evidence["tracks"]["2515"]["group_id"] = "g1"
        self.assertEqual(self.close_with(evidence),
                         ("staged_kept", False, "parent_unknown", "present_but_silent"))

    def test_a_missing_parent_outranks_the_track_being_soloed(self):
        # the parent could be muted: its own solo proves nothing
        evidence = _peak_evidence(muted_2515=False)
        evidence["tracks"]["2515"]["group_id"] = "g1"
        evidence["tracks"]["2515"]["soloed"] = True
        self.assertEqual(self.close_with(evidence)[:3], ("staged_kept", False, "parent_unknown"))

    def test_a_muted_parent_group_says_silent_by_mute(self):
        evidence = _peak_evidence(muted_2515=False)
        evidence["tracks"]["2515"]["group_id"] = "g1"
        evidence["groups"] = {"g1": _group("g1", "PEAK", muted=True)}
        self.assertEqual(self.close_with(evidence)[:3], ("staged_kept", False, "silent_by_mute"))

    def test_a_mute_outranks_a_missing_parent_and_a_solo(self):
        evidence = _peak_evidence(muted_2515=True)
        evidence["tracks"]["2515"]["group_id"] = "g1"
        evidence["tracks"]["21"]["soloed"] = True
        self.assertEqual(self.close_with(evidence)[:3], ("staged_kept", False, "silent_by_mute"))

    def test_a_deactivated_clip_is_silent_by_mute(self):
        evidence = _peak_evidence(muted_2515=False)
        evidence["clips"]["2566"] = _live("2566", 69, 71, 73, 74, muted=True)
        self.assertEqual(self.close_with(evidence)[:3], ("staged_kept", False, "silent_by_mute"))

    def test_a_track_state_the_captures_never_showed_is_never_audible(self):
        # every Producer Pal capture reports "muted" only (the reader derives
        # "soloed"); any other state on ANY track could hide a solo
        evidence = _peak_evidence(muted_2515=False)
        evidence["tracks"]["21"]["state"] = "muted-via-solo"
        self.assertEqual(self.close_with(evidence)[:3], ("staged_kept", False, "state_unknown"))

    def test_unjudged_rows_and_the_report_carry_the_audibility(self):
        self.register_peak()
        evidence = _peak_evidence(muted_2515=False)
        evidence["tracks"]["21"]["soloed"] = True
        out = experiments.sweep(evidence, path=self.ledger_path)
        for row in experiments.all_rows(ledger.load(self.ledger_path)):
            self.assertEqual(row["observation"]["audibility"], "silent_by_solo")
            self.assertIsNone(row["terminal_state"])
        self.assertEqual({e["audibility"] for e in out["arrangement"]["entries"]},
                         {"silent_by_solo"})


class EffectTrialContractTests(_LedgerCase):
    def register_duck(self, present=True, params=None):
        evidence = _peak_evidence()
        devices = [{"id": "210", "name": "Bassline", "type": "instrument", "path": "t24/d0"}]
        if present:
            devices.append({"id": "2593", "name": "SIDEKICK duck ← drums",
                            "type": "audio-effect: Compressor", "path": "t24/d4"})
        evidence["track_devices"] = {"21": devices}
        # a gone device's by-id read fails with Live's own "does not exist";
        # a read still placing it on the track would contradict the listing
        evidence["devices"] = {"2593": {"device_id": "2593", "id": "2593", "type": "audio-effect: Compressor",
                                        "name": "SIDEKICK duck ← drums",
                                        "path": "t24/d4",
                                        "params": params or {"Threshold": -12, "S/C On": "On"}}
                               if present else
                               {"device_id": "2593", "missing": True,
                                "error": 'PPToolError: Error: id "2593" does not exist'}}
        return evidence

    def setUp(self):
        super().setUp()
        cand = {"kind": experiments.EFFECT_KIND, "label": "duck on Bassline", "track_id": "21",
                "device_ids": ["2593"], "group": "sidechain", "move": "fx:device", "journal": None}
        row, why = experiments.verify_candidate(cand, self.register_duck(),
                                                expected_set_path=SET_PATH)
        (self.row,) = experiments.register([row], path=self.ledger_path)

    def state(self):
        row = experiments.find_row(ledger.load(self.ledger_path), self.row["id"])
        return row["terminal_state"], row["observation"]

    def test_kept_when_present_after_keep_with_param_changes_noted(self):
        experiments.judge(self.row["id"], "keep", reason="ok", path=self.ledger_path)
        experiments.sweep(self.register_duck(params={"Threshold": -18, "S/C On": "On"}),
                          path=self.ledger_path)
        state, obs = self.state()
        self.assertEqual(state, "kept")
        self.assertEqual(obs["params_changed"], ["2593"])

    def test_removed_when_the_device_is_gone_after_keep(self):
        experiments.judge(self.row["id"], "keep", reason="ok", path=self.ledger_path)
        experiments.sweep(self.register_duck(present=False), path=self.ledger_path)
        self.assertEqual(self.state()[0], "removed")

    def test_drop_is_terminal_drop(self):
        experiments.judge(self.row["id"], "drop", reason="no", path=self.ledger_path)
        experiments.sweep(self.register_duck(), path=self.ledger_path)
        self.assertEqual(self.state()[0], "drop")

    def test_unknown_when_his_track_is_gone_or_devices_unread(self):
        experiments.judge(self.row["id"], "keep", reason="ok", path=self.ledger_path)
        evidence = self.register_duck()
        evidence["track_devices"] = {"21": None}
        experiments.sweep(evidence, path=self.ledger_path)
        self.assertIsNone(self.state()[0])
        self.assertEqual(self.state()[1]["reason"], "devices_unread")

    def test_unjudged_absent_is_abandoned(self):
        experiments.sweep(self.register_duck(present=False), path=self.ledger_path)
        self.assertEqual(self.state()[0], "abandoned")


class ReportTests(_LedgerCase):
    def test_subtotal_is_survived_over_judged_and_separate_from_the_phrase_rate(self):
        evidence = _peak_evidence()
        evidence["tracks"]["2537"] = _track("2537", "SIDEKICK – peak line 3 climb", 42, True)
        evidence["clip_locations"]["2567"] = _loc("2567", "2537", "SIDEKICK – peak line 3 climb", "t42[119|1]")
        evidence["clips"]["2567"] = _live("2567", 90, 92)
        rows = self.register_peak(labels=("peak line 1 space", "peak line 2 rotation",
                                         "peak line 3 climb"), evidence=evidence)
        by_label = {r["label"]: r for r in rows}
        experiments.judge(by_label["peak line 1 space"]["id"], "keep", reason="a",
                          path=self.ledger_path)
        experiments.judge(by_label["peak line 2 rotation"]["id"], "drop", reason="b",
                          path=self.ledger_path)
        report = experiments.sweep(evidence, path=self.ledger_path)
        arrangement = report["arrangement"]
        self.assertEqual(arrangement["resolved_judged"], 2)
        self.assertEqual(arrangement["survived"], 1)
        self.assertEqual(arrangement["adopted"], 1)
        self.assertEqual(arrangement["drop"], 1)
        self.assertEqual(arrangement["unjudged"], 1)
        self.assertEqual(arrangement["survival_rate"], 0.5)
        self.assertEqual(report["effect_trials"]["rows"], 0)
        self.assertIsNone(report["effect_trials"]["kept_rate"])
        # the phrase scoreboard never sees these rows
        summary = ledger.summary(self.ledger_path)
        self.assertEqual(summary["scoreboard"]["verified"]["judged"], 0)
        self.assertEqual(summary["entries"], 0)

    def test_advice_names_pending_candidates(self):
        rows = self.register_peak()
        advice = experiments.advice(ledger.load(self.ledger_path))
        self.assertEqual(len(advice), 1)
        self.assertIn("2 arrangement/effect candidate(s) await judgment", advice[0])
        self.assertIn(rows[0]["id"], advice[0])
        experiments.judge(rows[0]["id"], "keep", reason="a", path=self.ledger_path)
        advice = experiments.advice(ledger.load(self.ledger_path))
        self.assertTrue(any("await close_session()" in line for line in advice))


if __name__ == "__main__":
    unittest.main()


EPOCH_B = "root-b|5151|restart"


def _peak_evidence_after_restart(*, renumber=True, muted_2515=True, content=(69, 71, 73, 74),
                                 extra_copy=False, name_2515="SIDEKICK – peak line 2 rotation"):
    """The same Set after a Live restart: a new runtime epoch, ids renumbered
    (or, by coincidence, kept) — content is what survives."""
    identity = dict(IDENTITY, runtime_epoch=EPOCH_B)
    t1, t2, c1, c2 = ("3483", "3515", "3565", "3566") if renumber else ("2483", "2515", "2565", "2566")
    tracks = {t1: _track(t1, "SIDEKICK – peak line 1 space", 40),
              t2: _track(t2, name_2515, 41, muted=muted_2515),
              "21": _track("21", "25-Bassline", 24)}
    locations = {c1: _loc(c1, t1, tracks[t1]["name"], "t40[119|1]"),
                 c2: _loc(c2, t2, tracks[t2]["name"], "t41[119|1]")}
    clips = {c1: _live(c1, 81, 83, 85), c2: _live(c2, *content)}
    if extra_copy:
        locations["3599"] = _loc("3599", t2, tracks[t2]["name"], "t41[151|1]")
        clips["3599"] = _live("3599", *content, start=600.0)
    return _evidence(tracks=tracks, locations=locations, clips=clips, identity=identity)


class CrossEpochTests(_LedgerCase):
    """Blocker 1: after a Live restart ids mean nothing. Survival is settled by
    content on the same-named track, or not at all — never by id absence."""

    def keep_line_2(self):
        rows = self.register_peak()
        two = next(r for r in rows if r["label"] == "peak line 2 rotation")
        experiments.judge(two["id"], "keep", reason="2", path=self.ledger_path)
        return {r["label"]: r["id"] for r in rows}

    def state_of(self, entry_id):
        row = experiments.find_row(ledger.load(self.ledger_path), entry_id)
        return row["terminal_state"], row["observation"]

    def test_renumbered_ids_rebind_by_content_on_the_same_named_track(self):
        ids = self.keep_line_2()
        experiments.sweep(_peak_evidence_after_restart(muted_2515=False), path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 2 rotation"])
        self.assertEqual(state, "adopted")
        self.assertEqual(obs["rebound"], {"2566": "3566"})
        self.assertFalse(obs["same_epoch"])
        self.assertEqual(obs["reason"], "unmuted_since_registration")
        # the unjudged line 1 was found too and stays pending, never abandoned
        state, obs = self.state_of(ids["peak line 1 space"])
        self.assertIsNone(state)
        self.assertEqual(obs["state"], "unjudged")

    def test_kept_ids_after_a_restart_still_need_the_content_to_match(self):
        ids = self.keep_line_2()
        experiments.sweep(_peak_evidence_after_restart(renumber=False, muted_2515=False,
                                                       content=(69, 71, 73, 75)),
                          path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 2 rotation"])
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "cross_epoch_unresolved")

    def test_no_content_match_is_unknown_never_not_adopted(self):
        ids = self.keep_line_2()
        evidence = _peak_evidence_after_restart()
        del evidence["clip_locations"]["3566"]
        del evidence["clips"]["3566"]
        report = experiments.sweep(evidence, path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 2 rotation"])
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "cross_epoch_unresolved")
        self.assertEqual(report["arrangement"]["not_adopted"], 0)
        self.assertEqual(report["arrangement"]["abandoned"], 0)
        self.assertIsNone(report["arrangement"]["survival_rate"])

    def test_two_content_matches_are_ambiguous(self):
        ids = self.keep_line_2()
        experiments.sweep(_peak_evidence_after_restart(extra_copy=True), path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 2 rotation"])
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "cross_epoch_ambiguous")

    def test_same_content_on_a_renamed_track_is_not_a_match(self):
        ids = self.keep_line_2()
        experiments.sweep(_peak_evidence_after_restart(name_2515="rotation (mine now)"),
                          path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 2 rotation"])
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "cross_epoch_unresolved")

    def test_a_muted_group_parent_silences_its_child(self):
        ids = self.keep_line_2()
        evidence = _peak_evidence(muted_2515=False)
        evidence["groups"] = {"g1": {"id": "g1", "name": "PEAK", "muted": True, "group_id": None}}
        evidence["tracks"]["2515"]["group_id"] = "g1"
        experiments.sweep(evidence, path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 2 rotation"])
        self.assertEqual(state, "staged_kept")
        self.assertFalse(obs["audible"])


class CrossEpochEffectTests(_LedgerCase):
    def setUp(self):
        super().setUp()
        evidence = _peak_evidence()
        evidence["track_devices"] = {"21": [{"id": "2593", "name": "SIDEKICK duck ← drums",
                                             "type": "audio-effect: Compressor", "path": "t24/d4"}]}
        evidence["devices"] = {"2593": {"device_id": "2593", "id": "2593", "type": "audio-effect: Compressor",
                                        "name": "SIDEKICK duck ← drums",
                                        "path": "t24/d4", "params": {"Threshold": -12}}}
        cand = {"kind": experiments.EFFECT_KIND, "label": "duck on Bassline", "track_id": "21",
                "device_ids": ["2593"], "group": "sidechain", "move": "fx:device", "journal": None}
        row, why = experiments.verify_candidate(cand, evidence, expected_set_path=SET_PATH)
        (self.row,) = experiments.register([row], path=self.ledger_path)
        experiments.judge(self.row["id"], "keep", reason="ok", path=self.ledger_path)

    def evidence(self, *, devices):
        identity = dict(IDENTITY, runtime_epoch=EPOCH_B)
        tracks = {"1021": _track("1021", "25-Bassline", 24)}
        return _evidence(tracks=tracks, locations={}, clips={}, identity=identity,
                         track_devices={"1021": devices})

    def state(self):
        row = experiments.find_row(ledger.load(self.ledger_path), self.row["id"])
        return row["terminal_state"], row["observation"]

    def test_device_rebinds_by_name_and_type_on_the_same_named_track(self):
        experiments.sweep(self.evidence(devices=[
            {"id": "3593", "name": "SIDEKICK duck ← drums", "type": "audio-effect: Compressor",
             "path": "t24/d4"}]), path=self.ledger_path)
        state, obs = self.state()
        self.assertEqual(state, "kept")
        self.assertEqual(obs["rebound"], {"2593": "3593"})
        self.assertFalse(obs["params_compared"])

    def test_device_absence_across_epochs_is_unknown_not_removed(self):
        experiments.sweep(self.evidence(devices=[
            {"id": "1210", "name": "Bassline", "type": "instrument: Bassline", "path": "t24/d0"}]),
            path=self.ledger_path)
        state, obs = self.state()
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "cross_epoch_unresolved")


class DedupeTests(_LedgerCase):
    """Blocker 2: one clip, one pending row; a handle that names two rows is
    refused, never guessed."""

    def verified_peak(self, labels, evidence=None):
        evidence = evidence or _peak_evidence()
        parsed = experiments.parse_journal(PEAK_JOURNAL, journal="v4-peak-compose.json")
        rows = []
        for cand in parsed["items"]:
            if cand["label"] in labels:
                row, why = experiments.verify_candidate(cand, evidence, expected_set_path=SET_PATH)
                self.assertIsNone(why, why)
                rows.append(row)
        return rows

    def test_registering_the_same_journal_twice_returns_the_existing_rows(self):
        first = experiments.register(self.verified_peak(("peak line 1 space", "peak line 2 rotation")),
                                     path=self.ledger_path)
        second = experiments.register(self.verified_peak(("peak line 1 space", "peak line 2 rotation")),
                                      path=self.ledger_path)
        self.assertEqual([r["registration"] for r in first], ["new", "new"])
        self.assertEqual([r["registration"] for r in second], ["existing", "existing"])
        self.assertEqual([r["id"] for r in second], [r["id"] for r in first])
        self.assertEqual(len(experiments.all_rows(ledger.load(self.ledger_path))), 2)

    def test_a_second_label_for_the_same_clip_is_the_same_row(self):
        (first,) = experiments.register(self.verified_peak(("peak line 1 space",)),
                                        path=self.ledger_path)
        (row,) = self.verified_peak(("peak line 1 space",))
        row["label"] = "the airy one"
        (second,) = experiments.register([row], path=self.ledger_path)
        self.assertEqual(second["registration"], "existing")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["label"], "peak line 1 space")
        self.assertEqual(len(experiments.all_rows(ledger.load(self.ledger_path))), 1)

    def test_an_overlapping_candidate_is_refused_naming_the_holding_row(self):
        (first,) = experiments.register(self.verified_peak(("peak line 2 rotation",)),
                                        path=self.ledger_path)
        evidence = _peak_evidence(clips_2515=("2566", "2599"))
        parsed = experiments.parse_journal(PEAK_JOURNAL, journal="j.json")
        cand = next(c for c in parsed["items"] if c["track_id"] == "2515")
        cand["clip_ids"] = ["2566", "2599"]
        cand["label"] = "whole rotation track"
        row, why = experiments.verify_candidate(cand, evidence, expected_set_path=SET_PATH)
        (out,) = experiments.register([row], path=self.ledger_path)
        self.assertEqual(out["registration"], "refused")
        self.assertIn("2566", out["reason"])
        self.assertIn(first["id"], out["reason"])
        self.assertEqual(len(experiments.all_rows(ledger.load(self.ledger_path))), 1)

    def test_the_same_material_registered_after_a_restart_is_the_same_row(self):
        (first,) = experiments.register(self.verified_peak(("peak line 2 rotation",)),
                                        path=self.ledger_path)
        evidence = _peak_evidence_after_restart()    # new epoch, ids renumbered, same content
        cand = {"kind": experiments.ARRANGEMENT_KIND, "label": "peak line 2 rotation",
                "track_id": "3515", "clip_ids": ["3566"], "group": "v4-peak-compose",
                "move": "arr:variant", "journal": None}
        row, why = experiments.verify_candidate(cand, evidence, expected_set_path=SET_PATH)
        self.assertIsNone(why, why)
        (out,) = experiments.register([row], path=self.ledger_path)
        self.assertEqual(out["registration"], "rebound")   # the same row, on its new ids
        self.assertEqual(out["id"], first["id"])
        self.assertEqual(len(experiments.all_rows(ledger.load(self.ledger_path))), 1)

    def test_a_closed_row_blocks_only_its_own_repeat(self):
        (first,) = experiments.register(self.verified_peak(("peak line 2 rotation",)),
                                        path=self.ledger_path)
        experiments.judge(first["id"], "keep", reason="a", path=self.ledger_path)
        experiments.sweep(_peak_evidence(muted_2515=False), path=self.ledger_path)
        # nothing changed since the close: already in the score, refused
        (same,) = experiments.register(self.verified_peak(("peak line 2 rotation",)),
                                       path=self.ledger_path)
        self.assertEqual(same["registration"], "refused")
        self.assertIn("nothing changed since", same["reason"])
        # changed material: a new round
        evidence = _peak_evidence()
        evidence["clips"]["2566"] = _live("2566", 69, 71, 73, 76)
        (again,) = experiments.register(self.verified_peak(("peak line 2 rotation",), evidence),
                                        path=self.ledger_path)
        self.assertEqual(again["registration"], "new")
        self.assertNotEqual(again["id"], first["id"])
        self.assertEqual(again["new_round_of"]["entry_id"], first["id"])

    def test_a_label_naming_two_pending_rows_is_refused_with_their_ids(self):
        rows = experiments.register(self.verified_peak(("peak line 1 space", "peak line 2 rotation")),
                                    path=self.ledger_path)
        data = ledger.load(self.ledger_path)
        for row in data["arrangement_experiments"]:
            row["label"] = "twin"
        ledger._save(data, self.ledger_path)
        with self.assertRaisesRegex(ValueError, "ambiguous") as ctx:
            experiments.judge("twin", "keep", reason=None, path=self.ledger_path)
        for row in rows:
            self.assertIn(row["id"], str(ctx.exception))
        # by id it still binds
        out = experiments.judge(rows[0]["id"], "keep", reason=None, path=self.ledger_path)
        self.assertEqual(out["entry_id"], rows[0]["id"])

    def test_labels_match_exactly_after_strip_never_by_case(self):
        (first,) = experiments.register(self.verified_peak(("peak line 1 space",)),
                                        path=self.ledger_path)
        self.assertIsNone(experiments.find_row(ledger.load(self.ledger_path), "PEAK LINE 1 SPACE"))
        self.assertEqual(experiments.find_row(ledger.load(self.ledger_path), " peak line 1 space ")["id"],
                         first["id"])


class LabelHijackTests(_LedgerCase):
    """Blocker 3: a handle naming a phrase entry is that phrase's, always."""

    def phrase_row(self):
        return ledger.record_write(
            clip_name="answer", slot="3/0", source="catalogue",
            answer_notes=[{"pitch": 60, "start": 0, "duration": 1, "velocity": 80}],
            set_identity=IDENTITY,
            clip_ref={"clip_id": "c1", "track_id": "jam", "track": "SIDEKICK JAM",
                      "surface": "session", "path": "t3/s0"},
            path=self.ledger_path)

    def test_a_label_equal_to_a_phrase_entry_id_never_captures_its_verdict(self):
        phrase = self.phrase_row()
        (row,) = self.register_peak(labels=("peak line 1 space",))
        data = ledger.load(self.ledger_path)
        data["arrangement_experiments"][0]["label"] = phrase["id"]
        ledger._save(data, self.ledger_path)
        self.assertIsNone(experiments.judge(phrase["id"], "keep", reason=None,
                                            path=self.ledger_path))
        again = experiments.find_row(ledger.load(self.ledger_path), row["id"])
        self.assertIsNone(again["immediate_verdict"])

    def test_registration_refuses_a_label_equal_to_any_ledger_id(self):
        phrase = self.phrase_row()
        (first,) = self.register_peak(labels=("peak line 1 space",))
        session_id = ledger.load(self.ledger_path)["sessions"][-1]["id"]
        parsed = experiments.parse_journal(PEAK_JOURNAL, journal="j.json")
        for taken in (phrase["id"], first["id"], session_id):
            cand = next(c for c in parsed["items"] if c["track_id"] == "2515")
            cand = dict(cand, label=taken)
            row, why = experiments.verify_candidate(cand, _peak_evidence(), expected_set_path=SET_PATH)
            (out,) = experiments.register([row], path=self.ledger_path)
            self.assertEqual(out["registration"], "refused", taken)
            self.assertIn("ledger id", out["reason"])
        self.assertEqual(len(experiments.all_rows(ledger.load(self.ledger_path))), 1)


class FeedbackRetryTests(_LedgerCase):
    """Item 8: a feedback write that failed is retried by the re-press."""

    def test_replay_retries_an_unrecorded_feedback_write(self):
        (row,) = self.register_peak(labels=("peak line 2 rotation",))
        real = piece_context.record_decision
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("disk full")
            return real(*args, **kwargs)

        with mock.patch.object(piece_context, "record_decision", side_effect=flaky):
            first = experiments.judge(row["id"], "keep", reason="a", path=self.ledger_path)
            self.assertFalse(first["feedback_recorded"])
            self.assertFalse(experiments.find_row(ledger.load(self.ledger_path), row["id"])
                             ["feedback_recorded"])
            self.assertEqual(self.decisions(), [])
            second = experiments.judge(row["id"], "keep", reason="a", path=self.ledger_path)
        self.assertTrue(second["replay"])
        self.assertTrue(second["feedback_recorded"])
        self.assertTrue(experiments.find_row(ledger.load(self.ledger_path), row["id"])
                        ["feedback_recorded"])
        self.assertEqual(len(self.decisions()), 1)
        # a third press changes nothing
        third = experiments.judge(row["id"], "keep", reason="a", path=self.ledger_path)
        self.assertTrue(third["feedback_recorded"])
        self.assertEqual(len(self.decisions()), 1)


OTHER_SET = "/Users/x/other/OTHER.als"
OTHER_IDENTITY = {"set_id": OTHER_SET, "runtime_epoch": "root-o|7|start", "file_path": OTHER_SET}


class SessionScopeTests(_LedgerCase):
    """Item 4: one close scores one Set's night(s); another Set's rows are
    never rewritten, and the report totals only what was swept."""

    def register_other_set(self):
        tracks = {"9": _track("9", "SIDEKICK – other line", 5)}
        locations = {"90": _loc("90", "9", tracks["9"]["name"], "t5[1|1]")}
        evidence = _evidence(tracks=tracks, locations=locations, clips={"90": _live("90", 50)},
                             identity=OTHER_IDENTITY)
        cand = {"kind": experiments.ARRANGEMENT_KIND, "label": "other line", "track_id": "9",
                "clip_ids": ["90"], "group": "other", "move": "arr:variant", "journal": None}
        row, why = experiments.verify_candidate(cand, evidence, expected_set_path=OTHER_SET)
        self.assertIsNone(why, why)
        (out,) = experiments.register([row], path=self.ledger_path)
        return out

    def test_sweep_defaults_to_the_evidence_sets_sessions_only(self):
        peak = self.register_peak(labels=("peak line 2 rotation",))
        other = self.register_other_set()
        self.assertNotEqual(peak[0]["session_id"], other["session_id"])
        experiments.judge(peak[0]["id"], "keep", reason="a", path=self.ledger_path)
        experiments.judge(other["id"], "keep", reason="b", path=self.ledger_path)
        report = experiments.sweep(_peak_evidence(muted_2515=False), path=self.ledger_path)
        data = ledger.load(self.ledger_path)
        self.assertEqual(experiments.find_row(data, peak[0]["id"])["terminal_state"], "adopted")
        untouched = experiments.find_row(data, other["id"])
        self.assertIsNone(untouched["terminal_state"])
        self.assertIsNone(untouched["observation"])
        self.assertEqual(report["arrangement"]["rows"], 1)
        self.assertEqual(report["session_ids"], [peak[0]["session_id"]])

    def test_an_explicit_session_is_the_only_one_swept(self):
        peak = self.register_peak(labels=("peak line 2 rotation",))
        other = self.register_other_set()
        experiments.judge(peak[0]["id"], "keep", reason="a", path=self.ledger_path)
        report = experiments.sweep(_peak_evidence(muted_2515=False),
                                   session_ids=[other["session_id"]], path=self.ledger_path)
        data = ledger.load(self.ledger_path)
        self.assertIsNone(experiments.find_row(data, peak[0]["id"])["observation"])
        self.assertEqual(experiments.find_row(data, other["id"])["observation"]["reason"],
                         "identity_mismatch")
        self.assertEqual(report["arrangement"]["rows"], 1)
        self.assertEqual(report["arrangement"]["unknown"], 1)

    def test_report_totals_only_the_named_sessions(self):
        peak = self.register_peak(labels=("peak line 2 rotation",))
        other = self.register_other_set()
        both = experiments.report(ledger.load(self.ledger_path),
                                  session_ids=[peak[0]["session_id"], other["session_id"]])
        one = experiments.report(ledger.load(self.ledger_path), session_ids=[other["session_id"]])
        self.assertEqual((both["arrangement"]["rows"], one["arrangement"]["rows"]), (2, 1))


class AdoptedByTests(_LedgerCase):
    """Item 6: "adopted" is reported by why — his originals (PLAN §1.6's
    class), an unmute since registration, or merely still audible on the
    SIDEKICK track it was written to."""

    def test_subtotal_splits_adopted_by_why(self):
        evidence = _peak_evidence(muted_2515=True)
        evidence["tracks"]["2537"] = _track("2537", "SIDEKICK – peak line 3 climb", 42, True)
        evidence["clip_locations"]["2567"] = _loc("2567", "2537", "SIDEKICK – peak line 3 climb", "t42[119|1]")
        evidence["clips"]["2567"] = _live("2567", 90, 92)
        rows = self.register_peak(labels=("peak line 1 space", "peak line 2 rotation",
                                         "peak line 3 climb"), evidence=evidence)
        for row in rows:
            experiments.judge(row["id"], "keep", reason="yes", path=self.ledger_path)
        close = _peak_evidence(muted_2515=False)            # line 2 unmuted since
        close["tracks"]["2537"] = _track("2537", "SIDEKICK – peak line 3 climb", 42, True)
        close["clip_locations"]["2567"] = _loc("2567", "21", "25-Bassline", "t24[119|1]")  # line 3 on his track
        close["clips"]["2567"] = _live("2567", 90, 92, track_index=24)
        report = experiments.sweep(close, path=self.ledger_path)
        arrangement = report["arrangement"]
        self.assertEqual(arrangement["adopted"], 3)
        self.assertEqual(arrangement["adopted_by"], {"on_his_originals": 1,
                                                     "unmuted_since_registration": 1,
                                                     "still_audible_on_sidekick": 1})
        whys = {e["label"]: e["why"] for e in arrangement["entries"]}
        self.assertEqual(whys["peak line 1 space"], "still_audible_on_sidekick")
        self.assertEqual(whys["peak line 2 rotation"], "unmuted_since_registration")
        self.assertEqual(whys["peak line 3 climb"], "on_his_originals")


class SameRunRebindTests(_LedgerCase):
    """Round-3 blocker: a content match must never hand back a row whose ids
    the current read shows are gone. Same run, ids disjoint → the pending
    unjudged row is rebound to the ids just verified; a judged row refuses."""

    def line_1(self, evidence=None):
        (row,) = self.register_peak(labels=("peak line 1 space",), evidence=evidence)
        return row

    def recreated(self):
        """2565 deleted and written again with identical content as 90001."""
        evidence = _peak_evidence()
        del evidence["clip_locations"]["2565"]
        del evidence["clips"]["2565"]
        evidence["clip_locations"]["90001"] = _loc("90001", "2483", "SIDEKICK – peak line 1 space",
                                                   "t40[119|1]")
        evidence["clips"]["90001"] = _live("90001", 81, 83, 85)
        return evidence

    def verified_line_1(self, evidence):
        parsed = experiments.parse_journal(PEAK_JOURNAL, journal="v4-peak-compose.json")
        cand = dict(next(c for c in parsed["items"] if c["track_id"] == "2483"), clip_ids=["90001"])
        row, why = experiments.verify_candidate(cand, evidence, expected_set_path=SET_PATH)
        self.assertIsNone(why, why)
        return row

    def test_a_pending_unjudged_row_is_rebound_to_the_verified_ids(self):
        first = self.line_1()
        (out,) = experiments.register([self.verified_line_1(self.recreated())], path=self.ledger_path)
        self.assertEqual(out["registration"], "rebound")
        self.assertEqual(out["id"], first["id"])
        self.assertEqual(out["rebound"], {"2565": "90001"})
        row = experiments.find_row(ledger.load(self.ledger_path), first["id"])
        self.assertEqual([c["clip_id"] for c in row["clips"]], ["90001"])
        (record,) = row["rebinds"]
        self.assertEqual((record["from_ids"], record["to_ids"]), (["2565"], ["90001"]))
        self.assertTrue(record["at"].endswith("Z"))
        self.assertEqual(len(experiments.all_rows(ledger.load(self.ledger_path))), 1)
        # judged after the rebind, the same-run close finds it by its new id
        experiments.judge(first["id"], "keep", reason="a", path=self.ledger_path)
        experiments.sweep(self.recreated(), path=self.ledger_path)
        row = experiments.find_row(ledger.load(self.ledger_path), first["id"])
        self.assertEqual(row["terminal_state"], "adopted")

    def test_a_judged_pending_row_refuses_and_names_itself(self):
        first = self.line_1()
        experiments.judge(first["id"], "keep", reason="a", path=self.ledger_path)
        (out,) = experiments.register([self.verified_line_1(self.recreated())], path=self.ledger_path)
        self.assertEqual(out["registration"], "refused")
        self.assertIn(first["id"], out["reason"])
        self.assertIn("2565", out["reason"])
        row = experiments.find_row(ledger.load(self.ledger_path), first["id"])
        self.assertEqual([c["clip_id"] for c in row["clips"]], ["2565"])
        self.assertEqual(len(experiments.all_rows(ledger.load(self.ledger_path))), 1)

    def test_a_new_run_rebinds_the_row_to_the_new_epoch_and_ids(self):
        first = self.line_1()
        evidence = _peak_evidence_after_restart()
        cand = {"kind": experiments.ARRANGEMENT_KIND, "label": "peak line 1 space",
                "track_id": "3483", "clip_ids": ["3565"], "group": "v4-peak-compose",
                "move": "arr:variant", "journal": None}
        row, why = experiments.verify_candidate(cand, evidence, expected_set_path=SET_PATH)
        (out,) = experiments.register([row], path=self.ledger_path)
        self.assertEqual(out["registration"], "rebound")
        self.assertEqual(out["id"], first["id"])
        stored = experiments.find_row(ledger.load(self.ledger_path), first["id"])
        self.assertEqual(stored["set_identity"]["runtime_epoch"], EPOCH_B)
        self.assertEqual(stored["track"]["id"], "3483")
        self.assertEqual([c["clip_id"] for c in stored["clips"]], ["3565"])
        self.assertEqual(stored["rebinds"][0]["from_epoch"], IDENTITY["runtime_epoch"])
