"""B12 — judgment-loop accounting and evidence seams (five-day review
findings 1–5, `.scratch/review-5d-20260930/review.md`). Each fake here is
the review's reproduction (`probes.json`), fixed:

1. effect evidence is bound to the returned track, device and parent
   (``effect_wrong_parent_and_id``);
2. the arrangement rate is withheld while judged rows are unresolved
   (``rate_with_unresolved_keeps``);
3. an unchanged restart is an observation on the closed row, never a second
   judged row (``identical_restart_double_count``);
4. renaming the SIDEKICK track never manufactures ``on_his_originals``
   (``rename_is_reported_as_original_adoption``);
5. parameter evidence degrades loudly (``unrecognized_params_accepted``,
   ``device_parameter_read_failure_at_close``).

No Live, no real store: temp ledger, temp piece decisions, the V4Device fake
(both builds) and hand-built evidence envelopes.
"""
from __future__ import annotations

import unittest

from mcp_server import experiments, ledger
from tests.pp_fakes import error_response, json_response, on_both_versions
from tests.test_experiments_rows import (SET_PATH, _LedgerCase, _peak_evidence)
from tests import test_experiments_tools as _tools

server = _tools.server

DUCK = {"id": "2593", "name": "SIDEKICK duck ← drums", "type": "audio-effect: Compressor",
        "path": "t24/d4"}
BASSLINE = {"id": "210", "name": "Bassline", "type": "instrument: Bassline", "path": "t24/d0"}


def _duck_read(**over):
    read = {"device_id": "2593", "id": "2593", "name": DUCK["name"], "type": DUCK["type"],
            "path": DUCK["path"], "params": {"Threshold": -12, "S/C On": "On"},
            "params_coverage": "full", "params_error": None}
    read.update(over)
    return read


def _duck_evidence(*, listed=(BASSLINE, DUCK), read=None):
    evidence = _peak_evidence()
    evidence["track_devices"] = {"21": [dict(d) for d in listed]}
    evidence["devices"] = {"2593": _duck_read() if read is None else read}
    return evidence


def _duck_candidate():
    return {"kind": experiments.EFFECT_KIND, "label": "duck on Bassline", "track_id": "21",
            "device_ids": ["2593"], "group": "sidechain", "move": "fx:device", "journal": None}


# --- finding 1: effect evidence is bound to the returned objects ------------------

class EffectBindingTests(_LedgerCase):
    """The listing must put the device under the track's own path and the
    by-id read must name that very device (id, type, parent); anything else
    is a contradiction — registration refused, close unknown."""

    def verify(self, evidence):
        return experiments.verify_candidate(_duck_candidate(), evidence, expected_set_path=SET_PATH)

    def test_a_bound_device_still_registers(self):
        row, why = self.verify(_duck_evidence())
        self.assertIsNone(why, why)
        (device,) = row["devices"]
        self.assertEqual((device["device_id"], device["type"], device["path"]),
                         ("2593", DUCK["type"], "t24/d4"))
        self.assertEqual(device["params_coverage"], "full")

    def test_device_listed_under_another_track_path_is_refused(self):
        row, why = self.verify(_duck_evidence(listed=(BASSLINE, dict(DUCK, path="t99/d0"))))
        self.assertIsNone(row)
        self.assertIn("evidence_mismatch:device_path", why)
        self.assertIn("t99/d0", why)

    def test_read_answering_another_id_is_refused(self):
        row, why = self.verify(_duck_evidence(read=_duck_read(id="888", mismatch=True,
                                                               error="evidence_mismatch:device_id")))
        self.assertIsNone(row)
        self.assertIn("evidence_mismatch:device_id", why)
        row, why = self.verify(_duck_evidence(read=_duck_read(id="888")))
        self.assertIsNone(row)
        self.assertIn("evidence_mismatch:device_id", why)

    def test_read_of_another_type_or_parent_is_refused(self):
        row, why = self.verify(_duck_evidence(read=_duck_read(type="audio-effect: Utility")))
        self.assertIsNone(row)
        self.assertIn("evidence_mismatch:device_type", why)
        row, why = self.verify(_duck_evidence(read=_duck_read(path="t99/d0")))
        self.assertIsNone(row)
        self.assertIn("evidence_mismatch:device_parent", why)
        row, why = self.verify(_duck_evidence(read=_duck_read(type=None)))
        self.assertIsNone(row)
        self.assertIn("evidence_mismatch:device_type", why)

    def test_unread_listing_names_the_mismatch(self):
        evidence = _duck_evidence()
        evidence["track_devices"] = {"21": None}
        evidence["track_device_errors"] = {"21": "evidence_mismatch:track_id (asked for id 21, "
                                                 "Live answered 999)"}
        row, why = self.verify(evidence)
        self.assertIsNone(row)
        self.assertIn("evidence_mismatch:track_id", why)

    def close_state(self, evidence):
        row, why = self.verify(_duck_evidence())
        self.assertIsNone(why, why)
        (written,) = experiments.register([row], path=self.ledger_path)
        experiments.judge(written["id"], "keep", reason="ok", path=self.ledger_path)
        report = experiments.sweep(evidence, path=self.ledger_path)
        stored = experiments.find_row(ledger.load(self.ledger_path), written["id"])
        return stored["terminal_state"], stored["observation"], report["effect_trials"]

    def test_close_is_unknown_when_the_listing_or_read_contradicts(self):
        cases = {
            "evidence_mismatch:track_id": None,
            "evidence_mismatch:device_path": _duck_evidence(listed=(BASSLINE, dict(DUCK, path="t99/d0"))),
            "evidence_mismatch:device_id": _duck_evidence(read=_duck_read(id="888", mismatch=True,
                                                                          error="evidence_mismatch:device_id")),
            "evidence_mismatch:device_type": _duck_evidence(read=_duck_read(type="audio-effect: Utility")),
            "evidence_mismatch:device_parent": _duck_evidence(read=_duck_read(path="t31/d3")),
        }
        unread = _duck_evidence()
        unread["track_devices"] = {"21": None}
        unread["track_device_errors"] = {"21": "evidence_mismatch:track_id (asked for id 21, "
                                               "Live answered 999)"}
        cases["evidence_mismatch:track_id"] = unread
        for expected, evidence in cases.items():
            with self.subTest(expected):
                self.setUp()
                state, obs, line = self.close_state(evidence)
                self.assertIsNone(state)
                self.assertEqual(obs["reason"], expected)
                self.assertEqual((line["kept"], line["removed"], line["unknown"]), (0, 0, 1))

    def test_listing_of_another_type_under_the_same_id_is_unknown(self):
        state, obs, line = self.close_state(
            _duck_evidence(listed=(BASSLINE, dict(DUCK, type="audio-effect: Utility")),
                           read=_duck_read(type="audio-effect: Utility")))
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "evidence_mismatch:device_type")

    def test_absent_device_whose_read_contradicts_is_unknown_not_removed(self):
        state, obs, line = self.close_state(
            _duck_evidence(listed=(BASSLINE,), read=_duck_read(id="888", mismatch=True,
                                                               error="evidence_mismatch:device_id")))
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "evidence_mismatch:device_id")
        self.assertEqual(line["removed"], 0)


@on_both_versions
class EffectBindingToolTests(unittest.TestCase):
    """The review's ``effect_wrong_parent_and_id`` fake through the reader:
    the track read answers another id/path and the device read another id."""
    setUp = _tools.ExperimentToolTests.setUp
    journal = _tools.ExperimentToolTests.journal
    rows = _tools.ExperimentToolTests.rows

    def wrong_parent_and_id(self):
        original = self.device.handlers["ppal-read-track"]

        def handler(args):
            if args.get("id") == "21" and args.get("include") == ["devices"]:
                return json_response({"id": "999", "path": "t99", "name": "OTHER TRACK",
                                      "devices": [{"id": "2593", "path": "t99/d0",
                                                   "name": "SIDEKICK duck ← drums",
                                                   "type": "audio-effect: Compressor"}]})
            return original(args)
        self.device.handlers["ppal-read-track"] = handler
        self.device.handlers["ppal-read-device"] = lambda args: json_response(
            {"id": "888", "path": "t99/d0", "name": "unrelated",
             "parameters": [{"name": "Device On", "value": "Off"}]})

    def wrong_device_only(self):
        self.device.handlers["ppal-read-device"] = lambda args: json_response(
            {"id": "888", "path": "t24/d4", "name": "SIDEKICK duck ← drums",
             "type": "audio-effect: Compressor",
             "parameters": [{"name": "Device On", "value": "Off"}]})

    def test_wrong_parent_and_id_registers_nothing(self):
        self.wrong_parent_and_id()
        out = server.register_arrangement_candidates(
            items=[{"kind": "effect_trial", "label": "wrong parent", "track_id": "21",
                    "device_ids": ["2593"]}], set_path=SET_PATH, confirm=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["registered"], [])
        (refused,) = out["refused"]
        self.assertIn("evidence_mismatch:track_id", refused["reason"])
        self.assertIn("999", refused["reason"])
        self.assertEqual(self.rows(), [])

    def test_track_reply_at_another_path_registers_nothing(self):
        original = self.device.handlers["ppal-read-track"]

        def handler(args):
            reply = original(args)
            if args.get("id") == "21" and args.get("include") == ["devices"]:
                payload = reply["content"][0]["json"]
                payload["trackIndex"] = 99           # id 21, but the reply is t99
                payload["devices"] = [dict(d, path="t99/d%d" % i)
                                      for i, d in enumerate(payload["devices"])]
            return reply
        self.device.handlers["ppal-read-track"] = handler
        out = server.register_arrangement_candidates(
            items=[{"kind": "effect_trial", "label": "wrong path", "track_id": "21",
                    "device_ids": ["2593"]}], set_path=SET_PATH, confirm=True)
        self.assertEqual(out["registered"], [])
        (refused,) = out["refused"]
        self.assertIn("evidence_mismatch:track_path", refused["reason"])
        self.assertIn("t99", refused["reason"])

    def test_wrong_device_id_registers_nothing(self):
        self.wrong_device_only()
        out = server.register_arrangement_candidates(journal_path=self.sidechain,
                                                     set_path=SET_PATH, confirm=True)
        self.assertEqual(out["registered"], [])
        for refused in out["refused"]:
            self.assertIn("evidence_mismatch:device_id", refused["reason"])
            self.assertIn("888", refused["reason"])

    def test_wrong_parent_and_id_at_close_is_unknown_never_kept(self):
        out = server.register_arrangement_candidates(journal_path=self.sidechain,
                                                     set_path=SET_PATH, confirm=True)
        ids = {c["label"]: c["entry_id"] for c in out["registered"]}
        self.assertEqual(len(ids), 2)
        for entry_id in ids.values():
            server.judge_answer("keep", entry_id=entry_id, reason="ok")
        self.wrong_parent_and_id()
        close = server.close_session()
        line = close["effect_trials"]
        self.assertEqual((line["kept"], line["removed"], line["unknown"]), (0, 0, 2))
        self.assertIsNone(line["kept_rate"])
        whys = {r["label"]: r["observation"]["reason"] for r in self.rows()}
        self.assertEqual(whys["SIDEKICK duck ← drums on 25-Bassline"], "evidence_mismatch:track_id")
        self.assertEqual(whys["SIDEKICK duck ← drums on 32-Omnisphere"], "evidence_mismatch:device_id")
        self.assertTrue(all(r["terminal_state"] is None for r in self.rows()))
        self.assertTrue(any("evidence_mismatch:track_id" in e
                            for e in close["experiment_evidence_errors"]))


# --- finding 2: the rate is withheld while judged rows are unresolved -----------

class RateCoverageTests(_LedgerCase):
    """Three KEEPs, one resolved: the definitive rate is withheld, and the
    report says how many verdicts it covers."""

    def three_keeps(self):
        evidence = _peak_evidence()
        rows = self.register_peak(labels=("peak line 1 space", "peak line 2 rotation"),
                                  evidence=evidence)
        for row in rows:
            experiments.judge(row["id"], "keep", reason="yes", path=self.ledger_path)
        return {r["label"]: r["id"] for r in rows}

    def test_unresolved_judgments_withhold_the_rate(self):
        self.three_keeps()
        evidence = _peak_evidence(muted_2515=False)
        from tests.test_experiments_rows import _live
        evidence["clips"]["2566"] = _live("2566", 69, 71, 73, 76)     # altered → unknown
        report = experiments.sweep(evidence, path=self.ledger_path)
        arrangement = report["arrangement"]
        self.assertEqual((arrangement["adopted"], arrangement["unknown"]), (1, 1))
        self.assertEqual(arrangement["judged_total"], 2)
        self.assertEqual(arrangement["resolved_judged"], 1)
        self.assertNotIn("judged", arrangement)
        self.assertIsNone(arrangement["survival_rate"])
        self.assertEqual(arrangement["rate_status"], "partial")
        self.assertEqual(arrangement["coverage"], {"resolved_judged": 1, "judged_total": 2})
        self.assertIn("withheld", arrangement["warning"])
        self.assertIn("1/2", arrangement["warning"])

    def test_every_judgment_resolved_is_definitive_but_thin(self):
        self.three_keeps()
        report = experiments.sweep(_peak_evidence(muted_2515=False), path=self.ledger_path)
        arrangement = report["arrangement"]
        self.assertEqual(arrangement["rate_status"], "definitive")
        self.assertEqual(arrangement["survival_rate"], 1.0)
        self.assertEqual(arrangement["coverage"], {"resolved_judged": 2, "judged_total": 2})
        self.assertIn("only 2 judged", arrangement["warning"])
        self.assertIn("too thin", arrangement["warning"])

    def test_nothing_judged_is_undefined_not_zero(self):
        self.register_peak()
        report = experiments.sweep(_peak_evidence(), path=self.ledger_path)
        arrangement = report["arrangement"]
        self.assertEqual((arrangement["judged_total"], arrangement["resolved_judged"]), (0, 0))
        self.assertIsNone(arrangement["survival_rate"])
        self.assertEqual(arrangement["rate_status"], "definitive")
        self.assertIn("undefined, not 0", arrangement["warning"])
        empty = experiments.report(ledger.load(self.ledger_path), session_ids=[])
        self.assertNotIn("warning", empty["arrangement"])
        self.assertEqual(empty["effect_trials"]["judged_total"], 0)

    def test_an_unknown_never_turns_into_a_drop(self):
        ids = self.three_keeps()
        evidence = _peak_evidence(muted_2515=False)
        from tests.test_experiments_rows import _live
        evidence["clips"]["2566"] = _live("2566", 69, 71, 73, 76)
        report = experiments.sweep(evidence, path=self.ledger_path)
        self.assertEqual((report["arrangement"]["drop"], report["arrangement"]["not_adopted"]), (0, 0))
        row = experiments.find_row(ledger.load(self.ledger_path), ids["peak line 2 rotation"])
        self.assertIsNone(row["terminal_state"])


@on_both_versions
class RateCoverageToolTests(unittest.TestCase):
    """The review's ``rate_with_unresolved_keeps`` fake: three KEEPs, two
    contents altered before close."""
    setUp = _tools.ExperimentToolTests.setUp
    journal = _tools.ExperimentToolTests.journal
    rows = _tools.ExperimentToolTests.rows
    register_peak = _tools.ExperimentToolTests.register_peak

    def test_two_altered_keeps_withhold_the_rate(self):
        ids = self.register_peak()
        for entry_id in ids.values():
            server.judge_answer("keep", entry_id=entry_id)
        self.device.replace_notes("2566", (1, 2, 3))
        self.device.replace_notes("2567", (4, 5, 6))
        close = server.close_session()
        arrangement = close["arrangement"]
        self.assertEqual((arrangement["adopted"], arrangement["unknown"]), (1, 2))
        self.assertEqual(arrangement["judged_total"], 3)
        self.assertEqual(arrangement["resolved_judged"], 1)
        self.assertIsNone(arrangement["survival_rate"])
        self.assertEqual(arrangement["rate_status"], "partial")
        self.assertEqual(arrangement["coverage"], {"resolved_judged": 1, "judged_total": 3})
        self.assertIn("2 judged", arrangement["warning"])
        # session_report carries the same accounting
        import taste_bridge
        from unittest import mock
        with mock.patch.object(taste_bridge, "rejected_phrases", return_value=[]):
            report = server.session_report()
        self.assertEqual(report["arrangement"]["rate_status"], "partial")
        self.assertIsNone(report["arrangement"]["survival_rate"])


# --- finding 3: an unchanged restart is an observation, never a second row -------

class UnchangedRestartTests(_LedgerCase):
    """The review's ``identical_restart_double_count`` replay: KEEP, adopted
    close; the same content registered again in another Live run."""

    def rotation(self, track_id="2515", clip_ids=("2566",)):
        return {"kind": experiments.ARRANGEMENT_KIND, "label": "rotation", "track_id": track_id,
                "clip_ids": list(clip_ids), "group": "v4-peak-compose", "move": "arr:variant",
                "journal": None}

    def close_adopted(self):
        row, why = experiments.verify_candidate(self.rotation(), _peak_evidence(),
                                                expected_set_path=SET_PATH)
        (first,) = experiments.register([row], path=self.ledger_path)
        experiments.judge(first["id"], "keep", reason="2", path=self.ledger_path)
        experiments.sweep(_peak_evidence(muted_2515=False), path=self.ledger_path)
        return first

    def test_same_content_in_another_run_is_observed_on_the_closed_row(self):
        from tests.test_experiments_rows import EPOCH_B, _peak_evidence_after_restart
        first = self.close_adopted()
        evidence = _peak_evidence_after_restart(muted_2515=False)
        row, why = experiments.verify_candidate(self.rotation("3515", ("3566",)), evidence,
                                                expected_set_path=SET_PATH)
        self.assertIsNone(why, why)
        (out,) = experiments.register([row], path=self.ledger_path)
        self.assertEqual(out["registration"], "observed")
        self.assertEqual(out["id"], first["id"])
        self.assertEqual(out["state"], "adopted")
        self.assertEqual(out["differs"], "epoch")
        self.assertIn("observation", out["reason"])
        self.assertNotIn("new_round_of", out)
        data = ledger.load(self.ledger_path)
        self.assertEqual(len(experiments.all_rows(data)), 1)
        stored = experiments.find_row(data, first["id"])
        (observation,) = stored["survival_observations"]
        self.assertEqual(observation["epoch"], EPOCH_B)
        self.assertEqual(observation["track_id"], "3515")
        self.assertEqual(observation["ids"], ["3566"])
        self.assertFalse(observation["track_muted"])
        self.assertTrue(observation["at"].endswith("Z"))
        self.assertEqual(observation["differs"], "epoch")
        # nothing about the closed row changed; the parent is still the one row in the score
        self.assertEqual(stored["terminal_state"], "adopted")
        report = experiments.report(data, [s["id"] for s in data["sessions"]])
        arrangement = report["arrangement"]
        self.assertEqual((arrangement["rows"], arrangement["judged_total"],
                          arrangement["resolved_judged"], arrangement["survived"]), (1, 1, 1, 1))
        # judging the observation is judging the closed row: refused, named
        with self.assertRaisesRegex(ValueError, "already closed as adopted"):
            experiments.judge(first["id"], "keep", reason="same", path=self.ledger_path)

    def test_same_content_recreated_in_the_same_run_is_observed_too(self):
        from tests.test_experiments_rows import _live, _loc
        first = self.close_adopted()
        evidence = _peak_evidence(muted_2515=False, clips_2515=())
        evidence["clip_locations"]["90066"] = _loc("90066", "2515", "SIDEKICK – peak line 2 rotation",
                                                   "t41[119|1]")
        evidence["clips"]["90066"] = _live("90066", 69, 71, 73, 74, track_index=41)
        row, why = experiments.verify_candidate(self.rotation("2515", ("90066",)), evidence,
                                                expected_set_path=SET_PATH)
        self.assertIsNone(why, why)
        (out,) = experiments.register([row], path=self.ledger_path)
        self.assertEqual(out["registration"], "observed")
        self.assertEqual(out["differs"], "ids")
        self.assertEqual(len(experiments.all_rows(ledger.load(self.ledger_path))), 1)

    def test_changed_content_is_still_a_new_round(self):
        from tests.test_experiments_rows import _live
        first = self.close_adopted()
        evidence = _peak_evidence()
        evidence["clips"]["2566"] = _live("2566", 69, 71, 73, 76)
        row, why = experiments.verify_candidate(self.rotation(), evidence, expected_set_path=SET_PATH)
        (out,) = experiments.register([row], path=self.ledger_path)
        self.assertEqual(out["registration"], "new")
        self.assertEqual(out["new_round_of"], {"entry_id": first["id"], "state": "adopted",
                                               "closed_at": out["new_round_of"]["closed_at"],
                                               "differs": "content"})
        self.assertEqual(len(experiments.all_rows(ledger.load(self.ledger_path))), 2)

    def test_legacy_epoch_only_children_never_count_beside_their_parent(self):
        from tests.test_experiments_rows import _peak_evidence_after_restart
        first = self.close_adopted()
        # a child written by the old code: a second judged row for the same material
        data = ledger.load(self.ledger_path)
        parent = experiments.find_row(data, first["id"])
        child = dict(parent, id="arr-legacychild", new_round_of={
            "entry_id": first["id"], "state": "adopted", "closed_at": parent["terminal_at"],
            "differs": "epoch"})
        child["set_identity"] = dict(parent["set_identity"], runtime_epoch="root-b|5151|restart")
        child["track"] = dict(parent["track"], id="3515")
        child["clips"] = [dict(c, clip_id="3566") for c in parent["clips"]]
        data["arrangement_experiments"].append(child)
        data["sessions"].append(dict(data["sessions"][-1], id="s-second"))
        child["session_id"] = "s-second"
        ledger._save(data, self.ledger_path)
        data = ledger.load(self.ledger_path)
        both = experiments.report(data, session_ids=[parent["session_id"], "s-second"])
        arrangement = both["arrangement"]
        self.assertEqual((arrangement["rows"], arrangement["resolved_judged"], arrangement["survived"]),
                         (1, 1, 1))
        self.assertEqual(arrangement["excluded_unchanged_rounds"],
                         [{"entry_id": "arr-legacychild", "of": first["id"], "differs": "epoch",
                           "parent_in_report": True}])
        self.assertEqual([e["entry_id"] for e in arrangement["entries"]], [first["id"]])
        # the child's own night, without the parent: still never counted alone
        alone = experiments.report(data, session_ids=["s-second"])
        self.assertEqual((alone["arrangement"]["rows"], alone["arrangement"]["judged_total"]), (0, 0))
        self.assertEqual(alone["arrangement"]["excluded_unchanged_rounds"],
                         [{"entry_id": "arr-legacychild", "of": first["id"], "differs": "epoch",
                           "parent_in_report": False}])


@on_both_versions
class UnchangedRestartToolTests(unittest.TestCase):
    setUp = _tools.ExperimentToolTests.setUp
    journal = _tools.ExperimentToolTests.journal
    rows = _tools.ExperimentToolTests.rows
    register_peak = _tools.ExperimentToolTests.register_peak

    def test_reopened_set_with_the_same_line_is_observed_not_counted_twice(self):
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 2 rotation"], reason="2")
        self.device.set_muted("2515", False)
        first = server.close_session()
        self.assertEqual(first["arrangement"]["adopted"], 1)
        self.device.restart_live(renumber=True)
        out = server.register_arrangement_candidates(
            items=[{"kind": "arrangement_experiment", "label": "rotation again",
                    "track_id": "3515", "clip_ids": ["3566"]}], set_path=SET_PATH, confirm=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["registered"], [])
        self.assertFalse(out["written"])
        (observed,) = out["observed"]
        self.assertEqual(observed["entry_id"], ids["peak line 2 rotation"])
        self.assertEqual(observed["state"], "adopted")
        self.assertEqual(observed["observation"]["track_id"], "3515")
        self.assertFalse(observed["already_recorded"])
        self.assertIn("observation", out["note"])
        again = server.register_arrangement_candidates(
            items=[{"kind": "arrangement_experiment", "label": "rotation again",
                    "track_id": "3515", "clip_ids": ["3566"]}], set_path=SET_PATH, confirm=True)
        self.assertTrue(again["observed"][0]["already_recorded"])
        self.assertIn("already recorded", again["note"])
        self.assertEqual(len(self.rows()), 3)
        with self.assertRaisesRegex(ValueError, "already closed"):
            server.judge_answer("keep", entry_id=ids["peak line 2 rotation"], reason="same")
        import taste_bridge
        from unittest import mock
        with mock.patch.object(taste_bridge, "rejected_phrases", return_value=[]):
            report = server.session_report()
        self.assertEqual((report["arrangement"]["rows"], report["arrangement"]["resolved_judged"],
                          report["arrangement"]["survived"]), (3, 1, 1))


# --- finding 4: on_his_originals is decided by track identity, never by name ------

class TrackIdentityTests(_LedgerCase):
    """The review's ``rename_is_reported_as_original_adoption`` fake and its
    converse: the registered track keeps the SIDEKICK classes whatever it is
    called; his track is his whatever it is called."""

    def keep_line_1(self, evidence=None):
        rows = self.register_peak(labels=("peak line 1 space", "peak line 2 rotation"),
                                  evidence=evidence)
        one = next(r for r in rows if r["label"] == "peak line 1 space")
        experiments.judge(one["id"], "keep", reason="1", path=self.ledger_path)
        return {r["label"]: r["id"] for r in rows}

    def state_of(self, entry_id):
        row = experiments.find_row(ledger.load(self.ledger_path), entry_id)
        return row["terminal_state"], row["observation"]

    def test_renamed_sidekick_track_is_still_the_sidekick_track(self):
        ids = self.keep_line_1()
        evidence = _peak_evidence()
        evidence["tracks"]["2483"]["name"] = "My renamed candidate"
        report = experiments.sweep(evidence, path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 1 space"])
        self.assertEqual(state, "adopted")
        self.assertEqual(obs["reason"], "still_audible_on_sidekick")
        self.assertEqual(obs["sidekick_track_renamed"],
                         {"from": "SIDEKICK – peak line 1 space", "to": "My renamed candidate"})
        self.assertEqual(report["arrangement"]["adopted_by"],
                         {"on_his_originals": 0, "unmuted_since_registration": 0,
                          "still_audible_on_sidekick": 1})

    def test_a_sidekick_named_destination_never_grants_his_originals(self):
        # a name never grants the stronger class: without a created-tracks
        # record, a SIDEKICK-named destination outside the ledger's provenance
        # is treated as the sidekick's (the conservative reading of the gap)
        from tests.test_experiments_rows import _live, _loc
        ids = self.keep_line_1()
        evidence = _peak_evidence()
        evidence["tracks"]["21"]["name"] = "SIDEKICK Bassline"
        evidence["clip_locations"]["2565"] = _loc("2565", "21", "SIDEKICK Bassline", "t24[119|1]")
        evidence["clips"]["2565"] = _live("2565", 81, 83, 85, track_index=24)
        experiments.sweep(evidence, path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 1 space"])
        self.assertEqual((state, obs["reason"]), ("adopted", "still_audible_on_sidekick"))
        self.assertEqual(obs["on_sidekick_named_track"], {"2565": "21"})
        self.assertNotIn("sidekick_track_renamed", obs)

    def test_his_plainly_named_track_grants_his_originals(self):
        from tests.test_experiments_rows import _live, _loc
        ids = self.keep_line_1()
        evidence = _peak_evidence()
        evidence["clip_locations"]["2565"] = _loc("2565", "21", "25-Bassline", "t24[119|1]")
        evidence["clips"]["2565"] = _live("2565", 81, 83, 85, track_index=24)
        experiments.sweep(evidence, path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 1 space"])
        self.assertEqual((state, obs["reason"]), ("adopted", "on_his_originals"))

    def test_moved_onto_another_sidekick_track_is_not_his_originals(self):
        from tests.test_experiments_rows import _live, _loc
        ids = self.keep_line_1()          # line 2's origin track 2515 is the sidekick's too
        evidence = _peak_evidence(muted_2515=False)
        evidence["clip_locations"]["2565"] = _loc("2565", "2515", "SIDEKICK – peak line 2 rotation",
                                                  "t41[151|1]")
        evidence["clips"]["2565"] = _live("2565", 81, 83, 85, start=600.0, track_index=41)
        experiments.sweep(evidence, path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 1 space"])
        self.assertEqual((state, obs["reason"]), ("adopted", "still_audible_on_sidekick"))
        self.assertEqual(obs["on_sidekick_track"], {"2565": "2515"})

    def test_moved_onto_the_jam_track_of_this_run_is_not_his_originals(self):
        from tests.test_experiments_rows import IDENTITY, _live, _loc, _track
        ledger.record_write(
            clip_name="answer", slot="3/0", source="catalogue",
            answer_notes=[{"pitch": 60, "start": 0, "duration": 1, "velocity": 80}],
            set_identity=IDENTITY,
            clip_ref={"clip_id": "c1", "track_id": "777", "track": "SIDEKICK JAM",
                      "surface": "session", "path": "t3/s0"},
            path=self.ledger_path)
        ids = self.keep_line_1()
        evidence = _peak_evidence()
        evidence["tracks"]["777"] = _track("777", "JAM (renamed)", 3)
        evidence["clip_locations"]["2565"] = _loc("2565", "777", "JAM (renamed)", "t3[119|1]")
        evidence["clips"]["2565"] = _live("2565", 81, 83, 85, track_index=3)
        experiments.sweep(evidence, path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 1 space"])
        self.assertEqual((state, obs["reason"]), ("adopted", "still_audible_on_sidekick"))
        self.assertEqual(obs["on_sidekick_track"], {"2565": "777"})

    def test_another_run_never_yields_his_originals(self):
        from tests.test_experiments_rows import (EPOCH_B, _live, _loc, _track, _evidence,
                                                 IDENTITY)
        # registered on a track without the SIDEKICK prefix: the name proves nothing
        evidence = _peak_evidence()
        evidence["tracks"]["2483"]["name"] = "My line"
        evidence["clip_locations"]["2565"]["track"] = "My line"
        ids = self.keep_line_1(evidence=evidence)
        after = _evidence(
            tracks={"3483": _track("3483", "My line", 40)},
            locations={"3565": _loc("3565", "3483", "My line", "t40[119|1]")},
            clips={"3565": _live("3565", 81, 83, 85)},
            identity=dict(IDENTITY, runtime_epoch=EPOCH_B))
        experiments.sweep(after, path=self.ledger_path)
        state, obs = self.state_of(ids["peak line 1 space"])
        self.assertEqual((state, obs["reason"]), ("adopted", "still_audible_on_sidekick"))
        self.assertFalse(obs["same_epoch"])

    def test_the_origin_track_is_stamped_and_survives_a_cross_run_rebind(self):
        from tests.test_experiments_rows import EPOCH_B, IDENTITY, _peak_evidence_after_restart
        ids = self.keep_line_1()
        row = experiments.find_row(ledger.load(self.ledger_path), ids["peak line 2 rotation"])
        self.assertEqual(row["origin_track"], {"id": "2515", "name": "SIDEKICK – peak line 2 rotation",
                                               "epoch": IDENTITY["runtime_epoch"], "sidekick": True})
        cand = {"kind": experiments.ARRANGEMENT_KIND, "label": "peak line 2 rotation",
                "track_id": "3515", "clip_ids": ["3566"], "group": "v4-peak-compose",
                "move": "arr:variant", "journal": None}
        verified, why = experiments.verify_candidate(cand, _peak_evidence_after_restart(),
                                                     expected_set_path=SET_PATH)
        (out,) = experiments.register([verified], path=self.ledger_path)
        self.assertEqual(out["registration"], "rebound")
        row = experiments.find_row(ledger.load(self.ledger_path), ids["peak line 2 rotation"])
        self.assertEqual(row["origin_track"], {"id": "3515", "name": "SIDEKICK – peak line 2 rotation",
                                               "epoch": EPOCH_B, "sidekick": True})


@on_both_versions
class TrackIdentityToolTests(unittest.TestCase):
    setUp = _tools.ExperimentToolTests.setUp
    journal = _tools.ExperimentToolTests.journal
    rows = _tools.ExperimentToolTests.rows
    register_peak = _tools.ExperimentToolTests.register_peak

    def test_renaming_the_sidekick_track_is_not_adoption_on_his_originals(self):
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 1 space"])
        self.device.track_by_id("2483")["name"] = "My renamed candidate"
        close = server.close_session()
        arrangement = close["arrangement"]
        self.assertEqual(arrangement["adopted"], 1)
        self.assertEqual(arrangement["adopted_by"]["on_his_originals"], 0)
        self.assertEqual(arrangement["adopted_by"]["still_audible_on_sidekick"], 1)
        entry = next(e for e in arrangement["entries"] if e["entry_id"] == ids["peak line 1 space"])
        self.assertEqual(entry["why"], "still_audible_on_sidekick")
        row = experiments.find_row(ledger.load(self.ledger_path), ids["peak line 1 space"])
        self.assertEqual(row["observation"]["sidekick_track_renamed"]["to"], "My renamed candidate")

    def test_his_track_renamed_with_the_prefix_withholds_his_originals(self):
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 1 space"])
        self.device.track_by_id("21")["name"] = "SIDEKICK Bassline"
        self.device.move_clip_to_track("2565", 24)
        close = server.close_session()
        self.assertEqual(close["arrangement"]["adopted_by"],
                         {"on_his_originals": 0, "unmuted_since_registration": 0,
                          "still_audible_on_sidekick": 1})
        # plainly named, the same move is his originals
        self.setUp()
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 1 space"])
        self.device.move_clip_to_track("2565", 24)
        close = server.close_session()
        self.assertEqual(close["arrangement"]["adopted_by"]["on_his_originals"], 1)


# --- finding 5: parameter evidence degrades loudly, never silently ----------------

class DeviceParamsShapeTests(unittest.TestCase):
    def params(self, reply):
        from mcp_server import live_write
        return live_write._device_params(reply)

    def test_list_of_name_value_records_is_full(self):
        out = self.params({"parameters": [{"name": "Threshold", "value": -12},
                                          {"name": "S/C On", "value": "On"}]})
        self.assertEqual(out, {"params": {"Threshold": -12, "S/C On": "On"},
                               "coverage": "full", "error": None})
        self.assertEqual(self.params({"params": [{"name": "A", "value": 1}]})["coverage"], "full")

    def test_dict_shaped_parameters_are_none_with_the_reason(self):
        out = self.params({"parameters": {"Threshold": -99}})
        self.assertEqual((out["params"], out["coverage"]), ({}, "none"))
        self.assertIn("dict", out["error"])
        self.assertIn("expected a list", out["error"])

    def test_missing_or_empty_parameters_are_none(self):
        self.assertEqual(self.params({"id": "2593"})["coverage"], "none")
        self.assertIn("missing", self.params({"id": "2593"})["error"])
        self.assertEqual(self.params({"parameters": []})["coverage"], "none")
        self.assertEqual(self.params("nonsense")["coverage"], "none")

    def test_unreadable_items_make_it_partial(self):
        out = self.params({"parameters": [{"name": "Threshold", "value": -12}, "junk", {"value": 1}]})
        self.assertEqual((out["params"], out["coverage"]), ({"Threshold": -12}, "partial"))
        self.assertIn("2 of 3", out["error"])
        self.assertEqual(self.params({"parameters": ["junk"]})["coverage"], "none")


class ParamsCoverageTests(_LedgerCase):
    def register(self, read):
        row, why = experiments.verify_candidate(_duck_candidate(), _duck_evidence(read=read),
                                                expected_set_path=SET_PATH)
        self.assertIsNone(why, why)
        (written,) = experiments.register([row], path=self.ledger_path)
        experiments.judge(written["id"], "keep", reason="ok", path=self.ledger_path)
        return written["id"]

    def close(self, entry_id, read):
        report = experiments.sweep(_duck_evidence(read=read), path=self.ledger_path)
        row = experiments.find_row(ledger.load(self.ledger_path), entry_id)
        return row["terminal_state"], row["observation"], report["effect_trials"]

    def test_full_reads_both_sides_are_compared(self):
        entry_id = self.register(_duck_read())
        state, obs, line = self.close(entry_id, _duck_read())
        self.assertEqual(state, "kept")
        self.assertEqual((obs["params_compared"], obs["params_coverage"], obs["params_changed"]),
                         (True, "full", []))
        self.assertIsNone(obs["params_error"])
        self.assertEqual((line["kept_params_unchanged"], line["kept_params_changed"],
                          line["kept_params_uncompared"]), (1, 0, 0))
        self.assertIn("present", line["note"])
        self.assertIn("audible", line["note"])
        (entry,) = line["entries"]
        self.assertEqual((entry["params_compared"], entry["params_coverage"]), (True, "full"))

    def test_changed_params_are_a_separate_fact(self):
        entry_id = self.register(_duck_read())
        state, obs, line = self.close(entry_id, _duck_read(params={"Threshold": -20, "S/C On": "On"}))
        self.assertEqual(state, "kept")
        self.assertEqual(obs["params_changed"], ["2593"])
        self.assertEqual((line["kept"], line["kept_params_changed"]), (1, 1))

    def test_a_failed_read_at_close_keeps_presence_but_not_the_comparison(self):
        entry_id = self.register(_duck_read())
        state, obs, line = self.close(entry_id, {"device_id": "2593",
                                                 "error": "PPToolError: read timed out"})
        self.assertEqual(state, "kept")
        self.assertFalse(obs["params_compared"])
        self.assertEqual(obs["params_coverage"], "none")
        self.assertEqual(obs["params_uncompared"], ["2593"])
        self.assertIn("read timed out", obs["params_error"][0])
        self.assertEqual((line["kept"], line["kept_params_uncompared"]), (1, 1))
        (entry,) = line["entries"]
        self.assertEqual((entry["params_compared"], entry["params_coverage"]), (False, "none"))
        self.assertIn("read timed out", entry["params_error"][0])

    def test_partial_coverage_on_either_side_is_never_compared(self):
        entry_id = self.register(_duck_read())
        state, obs, line = self.close(entry_id, _duck_read(params={"Threshold": -12},
                                                           params_coverage="partial",
                                                           params_error="1 of 2 parameter item(s) "
                                                                        "are not {name, value} records"))
        self.assertEqual(state, "kept")
        self.assertEqual((obs["params_compared"], obs["params_coverage"]), (False, "partial"))
        self.assertIn("1 of 2", obs["params_error"][0])
        # registered with nothing readable: presence binds, parameters never compare
        self.setUp()
        entry_id = self.register(_duck_read(params={}, params_coverage="none",
                                            params_error="parameters is dict, expected a list"))
        row = experiments.find_row(ledger.load(self.ledger_path), entry_id)
        self.assertEqual(row["devices"][0]["params_coverage"], "none")
        state, obs, line = self.close(entry_id, _duck_read())
        self.assertEqual(state, "kept")
        self.assertEqual((obs["params_compared"], obs["params_coverage"]), (False, "full"))
        self.assertTrue(any("registered with none" in e for e in obs["params_error"]))


@on_both_versions
class ParamsCoverageToolTests(unittest.TestCase):
    """The review's ``unrecognized_params_accepted`` (a dict-shaped
    `parameters`, on a reply that otherwise binds) and
    ``device_parameter_read_failure_at_close`` fakes."""
    setUp = _tools.ExperimentToolTests.setUp
    journal = _tools.ExperimentToolTests.journal
    rows = _tools.ExperimentToolTests.rows

    def bound_but_dict_params(self, args):
        did = args["id"]
        owner = next(tid for tid, devs in self.device.devices.items()
                     if any(d["id"] == did for d in devs))
        entry = next(d for d in self.device.devices[owner] if d["id"] == did)
        return json_response({"id": did, "name": entry["name"], "path": entry["path"],
                              "type": entry["type"], "parameters": {"Threshold": -99}})

    def register_and_keep(self):
        out = server.register_arrangement_candidates(journal_path=self.sidechain,
                                                     set_path=SET_PATH, confirm=True)
        self.assertTrue(out["ok"], out)
        for row in out["registered"]:
            server.judge_answer("keep", entry_id=row["entry_id"])
        return out

    def test_dict_shaped_parameters_register_with_no_coverage_and_never_compare(self):
        self.device.handlers["ppal-read-device"] = self.bound_but_dict_params
        out = self.register_and_keep()
        self.assertEqual(len(out["registered"]), 2)
        for row in out["registered"]:
            self.assertEqual(row["params"], {row["device_ids"][0]: {}})
            self.assertEqual(row["params_coverage"], {row["device_ids"][0]: "none"})
        self.assertIn("parameter coverage", out["note"])
        close = server.close_session()
        line = close["effect_trials"]
        self.assertEqual((line["kept"], line["unknown"]), (2, 0))
        self.assertEqual((line["kept_params_unchanged"], line["kept_params_uncompared"]), (0, 2))
        for entry in line["entries"]:
            self.assertFalse(entry["params_compared"])
            self.assertEqual(entry["params_coverage"], "none")
            self.assertTrue(any("expected a list" in e for e in entry["params_error"]))

    def test_read_failure_at_close_is_reported_never_compared(self):
        self.register_and_keep()
        self.device.handlers["ppal-read-device"] = lambda args: error_response("read timed out")
        close = server.close_session()
        line = close["effect_trials"]
        self.assertEqual((line["kept"], line["unknown"], line["resolved_judged"]), (2, 0, 2))
        self.assertEqual(line["kept_params_uncompared"], 2)
        self.assertEqual(line["kept_params_unchanged"], 0)
        for row in self.rows():
            self.assertEqual(row["terminal_state"], "kept")
            self.assertFalse(row["observation"]["params_compared"])
            self.assertEqual(row["observation"]["params_coverage"], "none")
            self.assertTrue(any("read timed out" in e for e in row["observation"]["params_error"]))
        self.assertIn("audible", line["note"])


# --- round 2, B1: absence is proven, never inferred from a silent listing ---------

class DeviceAbsenceTests(_LedgerCase):
    """A device missing from the bound listing is gone only when Live itself
    says its id does not exist (or answers the by-id read with no object);
    any other by-id read — an error, a clip-shaped object, a device on a
    return or in a rack — leaves the row unknown with the reason."""

    def keep_duck(self):
        row, why = experiments.verify_candidate(_duck_candidate(), _duck_evidence(),
                                                expected_set_path=SET_PATH)
        self.assertIsNone(why, why)
        (written,) = experiments.register([row], path=self.ledger_path)
        experiments.judge(written["id"], "keep", reason="ok", path=self.ledger_path)
        return written["id"]

    def absent(self, read, **tracks):
        from tests.test_experiments_rows import _track
        evidence = _duck_evidence(listed=(BASSLINE,), read=read)
        for tid, (name, index) in tracks.items():
            evidence["tracks"][tid] = _track(tid, name, index)
        return evidence

    def close(self, entry_id, evidence):
        report = experiments.sweep(evidence, path=self.ledger_path)
        row = experiments.find_row(ledger.load(self.ledger_path), entry_id)
        return row["terminal_state"], row["observation"], report["effect_trials"]

    def test_lives_own_does_not_exist_proves_absence(self):
        entry_id = self.keep_duck()
        state, obs, line = self.close(entry_id, self.absent(
            {"device_id": "2593", "missing": True,
             "error": 'PPToolError: Error: id "2593" does not exist'}))
        self.assertEqual((state, obs["reason"]), ("removed", "devices_gone"))
        self.assertEqual((line["removed"], line["kept_rate"]), (1, 0.0))

    def test_a_result_less_read_never_proves_absence(self):
        # round 3: a reply naming no object is exactly a degraded read's shape
        entry_id = self.keep_duck()
        state, obs, line = self.close(entry_id, self.absent(
            {"device_id": "2593", "no_object": True, "error": "no device object in the reply"}))
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "device_read_failed")
        self.assertEqual((line["removed"], line["unknown"]), (0, 1))

    def test_an_id_recycled_to_another_device_elsewhere_is_recycled_not_moved(self):
        entry_id = self.keep_duck()
        state, obs, _ = self.close(entry_id, self.absent(
            _duck_read(path="t31/d3", type="audio-effect: Utility", name="Utility"),
            **{"1113": ("32-Omnisphere", 31)}))
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "id_recycled:t31/d3")
        self.setUp()
        entry_id = self.keep_duck()          # same type, another name: not this device
        state, obs, _ = self.close(entry_id, self.absent(
            _duck_read(path="t31/d3", name="his own compressor"), **{"1113": ("32-Omnisphere", 31)}))
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "id_recycled:t31/d3")
        self.setUp()
        entry_id = self.keep_duck()          # same type and name elsewhere: moved
        state, obs, _ = self.close(entry_id, self.absent(
            _duck_read(path="t31/d3"), **{"1113": ("32-Omnisphere", 31)}))
        self.assertEqual(obs["reason"], "device_moved:32-Omnisphere")

    def test_a_failed_read_leaves_absence_unknown(self):
        entry_id = self.keep_duck()
        state, obs, line = self.close(entry_id, self.absent(
            {"device_id": "2593", "error": "PPToolError: read timed out"}))
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "device_read_failed")
        self.assertIn("read timed out", obs["detail"])
        self.assertEqual((line["removed"], line["unknown"], line["kept_rate"]), (0, 1, None))
        # no read at all is unread, never gone
        self.setUp()
        entry_id = self.keep_duck()
        evidence = self.absent({"device_id": "2593", "error": "x"})
        evidence["devices"] = {}
        state, obs, _ = self.close(entry_id, evidence)
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "device_unread")

    def test_an_id_recycled_to_a_clip_is_unknown_not_removed(self):
        entry_id = self.keep_duck()
        state, obs, line = self.close(entry_id, self.absent(
            {"device_id": "2593", "id": "2593", "path": "t41/arr0"}))
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "id_recycled:t41/arr0")
        self.assertEqual(line["removed"], 0)
        # a device-shaped path without a type is not a device either
        self.setUp()
        entry_id = self.keep_duck()
        state, obs, _ = self.close(entry_id, self.absent(
            {"device_id": "2593", "id": "2593", "path": "t31/d3", "name": "?"}))
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "id_recycled:t31/d3")

    def test_a_device_on_a_return_or_in_a_rack_is_moved_not_removed(self):
        entry_id = self.keep_duck()
        state, obs, line = self.close(entry_id, self.absent(_duck_read(path="rt0/d0")))
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "device_moved:rt0/d0")
        self.assertEqual(line["removed"], 0)
        self.setUp()
        entry_id = self.keep_duck()
        state, obs, _ = self.close(entry_id, self.absent(
            _duck_read(path="t31/d3/c0/d1"), **{"1113": ("32-Omnisphere", 31)}))
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "device_moved:32-Omnisphere")
        self.assertEqual(obs["moved"], {"2593": "32-Omnisphere"})

    def test_unknown_absence_resolves_on_a_later_close(self):
        entry_id = self.keep_duck()
        state, obs, _ = self.close(entry_id, self.absent(
            {"device_id": "2593", "error": "PPToolError: read timed out"}))
        self.assertIsNone(state)
        state, obs, _ = self.close(entry_id, _duck_evidence())      # the device is back, read fine
        self.assertEqual(state, "kept")
        # unjudged + proven gone is abandoned; unjudged + unproven is not
        self.setUp()
        row, why = experiments.verify_candidate(_duck_candidate(), _duck_evidence(),
                                                expected_set_path=SET_PATH)
        (written,) = experiments.register([row], path=self.ledger_path)
        state, obs, _ = self.close(written["id"], self.absent(
            {"device_id": "2593", "error": "PPToolError: read timed out"}))
        self.assertIsNone(state)
        state, obs, _ = self.close(written["id"], self.absent(
            {"device_id": "2593", "missing": True,
             "error": 'PPToolError: Error: id "2593" does not exist'}))
        self.assertEqual(state, "abandoned")


@on_both_versions
class DeviceAbsenceToolTests(unittest.TestCase):
    """The round-2 fakes through the reader: moved + timed-out read, an id
    recycled to a clip, a compressor on a return, and a true deletion."""
    setUp = _tools.ExperimentToolTests.setUp
    journal = _tools.ExperimentToolTests.journal
    rows = _tools.ExperimentToolTests.rows

    def keep_bassline(self):
        out = server.register_arrangement_candidates(journal_path=self.sidechain,
                                                     set_path=SET_PATH, confirm=True)
        ids = {c["label"]: c["entry_id"] for c in out["registered"]}
        server.judge_answer("keep", entry_id=ids["SIDEKICK duck ← drums on 25-Bassline"], reason="ok")
        server.judge_answer("keep", entry_id=ids["SIDEKICK duck ← drums on 32-Omnisphere"], reason="ok")
        return ids["SIDEKICK duck ← drums on 25-Bassline"]

    def state(self, entry_id):
        row = experiments.find_row(ledger.load(self.ledger_path), entry_id)
        return row["terminal_state"], row["observation"]["reason"]

    def test_moved_with_a_timed_out_read_is_unknown_not_removed(self):
        entry_id = self.keep_bassline()
        self.device.move_device("2593", "21", "1113")
        self.device.handlers["ppal-read-device"] = lambda args: error_response("read timed out")
        close = server.close_session()
        line = close["effect_trials"]
        # Omnisphere's is still listed: kept, parameters uncompared; Bassline's is unknown
        self.assertEqual((line["removed"], line["unknown"], line["kept"],
                          line["kept_params_uncompared"]), (0, 1, 1, 1))
        self.assertEqual(self.state(entry_id), (None, "device_read_failed"))

    def test_an_id_recycled_to_a_clip_is_unknown_not_removed(self):
        entry_id = self.keep_bassline()
        self.device.remove_device("21", "2593")
        original = self.device.handlers["ppal-read-device"]
        self.device.handlers["ppal-read-device"] = lambda args: (
            json_response({"id": "2593", "path": "t41/arr0"}) if args.get("id") == "2593"
            else original(args))
        close = server.close_session()
        self.assertEqual(close["effect_trials"]["removed"], 0)
        self.assertEqual(self.state(entry_id), (None, "id_recycled:t41/arr0"))

    def test_a_compressor_on_a_return_is_moved_not_removed(self):
        entry_id = self.keep_bassline()
        self.device.remove_device("21", "2593")
        original = self.device.handlers["ppal-read-device"]
        self.device.handlers["ppal-read-device"] = lambda args: (
            json_response({"id": "2593", "name": "SIDEKICK duck ← drums", "path": "rt0/d0",
                           "type": "audio-effect: Compressor",
                           "parameters": [{"name": "Device On", "value": "On"}]})
            if args.get("id") == "2593" else original(args))
        close = server.close_session()
        self.assertEqual(close["effect_trials"]["removed"], 0)
        self.assertEqual(self.state(entry_id), (None, "device_moved:rt0/d0"))
        # put it back on Bassline: the later close resolves it kept
        self.device.handlers["ppal-read-device"] = original
        self.device.devices["21"].append(dict(DUCK))
        close = server.close_session()
        self.assertEqual(self.state(entry_id), ("kept", "present_on_his_track"))

    def test_degraded_read_shapes_never_prove_absence(self):
        # round 3: warning-only, {"result": null, warnings}, {} + truncated, bare {}, null, []
        shapes = {
            "warning_only": {"content": [{"text": "WARNING: read timed out"}], "is_error": False},
            "null_result_with_warning": json_response({"result": None,
                                                       "warnings": ["WARNING: read timed out"]}),
            "empty_plus_truncated": {"content": [{"json": {}}, {"text": "WARNING: response truncated"}],
                                     "is_error": False},
            "bare_empty": json_response({}),
            "null": json_response(None),
            "list": json_response([]),
        }
        for name, reply in shapes.items():
            with self.subTest(name):
                self.setUp()
                entry_id = self.keep_bassline()
                self.device.move_device("2593", "21", "1113")      # it still exists
                original = self.device.handlers["ppal-read-device"]
                self.device.handlers["ppal-read-device"] = lambda args, reply=reply, original=original: (
                    reply if args.get("id") == "2593" else original(args))
                close = server.close_session()
                line = close["effect_trials"]
                self.assertEqual((line["removed"], line["unknown"]), (0, 1), name)
                self.assertEqual(self.state(entry_id), (None, "device_read_failed"), name)
                self.assertIsNone(line["kept_rate"])

    def test_a_warned_read_of_a_present_device_leaves_its_params_uncompared(self):
        entry_id = self.keep_bassline()
        original = self.device.handlers["ppal-read-device"]
        self.device.handlers["ppal-read-device"] = lambda args: (
            {**original(args), "content": original(args)["content"]
             + [{"text": "WARNING: read timed out"}]} if args.get("id") == "2593" else original(args))
        close = server.close_session()
        line = close["effect_trials"]
        self.assertEqual((line["kept"], line["kept_params_uncompared"]), (2, 1))
        row = experiments.find_row(ledger.load(self.ledger_path), entry_id)
        self.assertEqual(row["observation"]["params_coverage"], "none")
        self.assertTrue(any("timed out" in e for e in row["observation"]["params_error"]))

    def test_an_id_recycled_to_another_device_is_unknown_not_moved(self):
        entry_id = self.keep_bassline()
        self.device.remove_device("21", "2593")
        self.device.devices["1113"].append({"id": "2593", "name": "Utility",
                                            "type": "audio-effect: Utility", "path": "t31/d3"})
        self.device.device_params["2593"] = [{"name": "Device On", "value": "On"}]
        close = server.close_session()
        self.assertEqual(close["effect_trials"]["removed"], 0)
        self.assertEqual(self.state(entry_id), (None, "id_recycled:t31/d3"))

    def test_a_deleted_device_is_still_removed(self):
        entry_id = self.keep_bassline()
        self.device.remove_device("21", "2593")          # the fake answers "does not exist"
        close = server.close_session()
        self.assertEqual((close["effect_trials"]["removed"], close["effect_trials"]["kept"]), (1, 1))
        self.assertEqual(self.state(entry_id), ("removed", "devices_gone"))


# --- round 2, B2: a re-proposal after a non-survived close is a new round --------

class ReproposalTests(_LedgerCase):
    def line_2(self, track_id="2515", clip_ids=("2566",), label="peak line 2 rotation"):
        return {"kind": experiments.ARRANGEMENT_KIND, "label": label, "track_id": track_id,
                "clip_ids": list(clip_ids), "group": "v4-peak-compose", "move": "arr:variant",
                "journal": None}

    def registered(self, cand, evidence):
        row, why = experiments.verify_candidate(cand, evidence, expected_set_path=SET_PATH)
        self.assertIsNone(why, why)
        (out,) = experiments.register([row], path=self.ledger_path)
        return out

    def recreated(self, clip_id="9566", track_id="2515", muted=True, name=None):
        from tests.test_experiments_rows import _live, _loc
        evidence = _peak_evidence(clips_2515=(), muted_2515=muted)
        track_name = name or evidence["tracks"][track_id]["name"]
        evidence["tracks"][track_id]["name"] = track_name
        index = evidence["tracks"][track_id]["index"]
        evidence["clip_locations"][clip_id] = _loc(clip_id, track_id, track_name, "t%d[119|1]" % index)
        evidence["clips"][clip_id] = _live(clip_id, 69, 71, 73, 74, track_index=index)
        return evidence

    def test_abandoned_then_the_same_notes_again_is_a_judgeable_round(self):
        first = self.registered(self.line_2(), _peak_evidence())
        experiments.sweep(_peak_evidence(clips_2515=()), path=self.ledger_path)
        self.assertEqual(experiments.find_row(ledger.load(self.ledger_path), first["id"])
                         ["terminal_state"], "abandoned")
        again = self.registered(self.line_2(clip_ids=("9566",)), self.recreated())
        self.assertEqual(again["registration"], "new")
        self.assertNotEqual(again["id"], first["id"])
        self.assertEqual(again["new_round_of"]["differs"], "re-proposed_after_abandoned")
        self.assertEqual(again["new_round_of"]["entry_id"], first["id"])
        out = experiments.judge(again["id"], "keep", reason="now yes", path=self.ledger_path)
        self.assertEqual(out["verdict"], "keep")
        report = experiments.sweep(self.recreated(), path=self.ledger_path)
        self.assertEqual((report["arrangement"]["judged_total"], report["arrangement"]["staged_kept"]),
                         (1, 1))
        self.assertEqual(report["arrangement"]["excluded_unchanged_rounds"], [])
        self.assertEqual(experiments.find_row(ledger.load(self.ledger_path), first["id"])
                         .get("survival_observations"), None)

    def test_dropped_then_proposed_again_is_a_new_round_even_unchanged(self):
        first = self.registered(self.line_2(), _peak_evidence())
        experiments.judge(first["id"], "drop", reason="no", path=self.ledger_path)
        experiments.sweep(_peak_evidence(), path=self.ledger_path)
        again = self.registered(self.line_2(), _peak_evidence())        # the very same clip
        self.assertEqual(again["registration"], "new")
        self.assertEqual(again["new_round_of"]["differs"], "re-proposed_after_drop")
        experiments.judge(again["id"], "keep", reason="changed my mind", path=self.ledger_path)
        report = experiments.sweep(_peak_evidence(muted_2515=False), path=self.ledger_path)
        self.assertEqual((report["arrangement"]["drop"], report["arrangement"]["adopted"]), (1, 1))

    def test_the_latest_verdict_on_the_material_decides_not_the_oldest(self):
        # drop → re-proposed → KEEP → adopted: the identical clip a third time
        # meets the ADOPTED row (newest), not the old drop — refused, no chain
        first = self.registered(self.line_2(), _peak_evidence())
        experiments.judge(first["id"], "drop", reason="no", path=self.ledger_path)
        experiments.sweep(_peak_evidence(), path=self.ledger_path)
        second = self.registered(self.line_2(), _peak_evidence())
        self.assertEqual(second["new_round_of"]["differs"], "re-proposed_after_drop")
        experiments.judge(second["id"], "keep", reason="yes", path=self.ledger_path)
        experiments.sweep(_peak_evidence(muted_2515=False), path=self.ledger_path)
        third = self.registered(self.line_2(), _peak_evidence())
        self.assertEqual(third["registration"], "refused")
        self.assertIn(second["id"], third["reason"])
        self.assertIn("nothing changed since", third["reason"])
        self.assertEqual(len(experiments.all_rows(ledger.load(self.ledger_path))), 2)
        # and after a restart the same content is an observation on the adopted row
        from tests.test_experiments_rows import _peak_evidence_after_restart
        again = self.registered(self.line_2(track_id="3515", clip_ids=("3566",)),
                                _peak_evidence_after_restart(muted_2515=False))
        self.assertEqual((again["registration"], again["id"]), ("observed", second["id"]))

    def test_not_adopted_then_back_is_a_new_round(self):
        first = self.registered(self.line_2(), _peak_evidence())
        experiments.judge(first["id"], "keep", reason="2", path=self.ledger_path)
        experiments.sweep(_peak_evidence(clips_2515=()), path=self.ledger_path)
        again = self.registered(self.line_2(clip_ids=("9566",)), self.recreated())
        self.assertEqual(again["new_round_of"]["differs"], "re-proposed_after_not_adopted")

    def test_a_copy_on_his_track_is_always_registrable(self):
        first = self.registered(self.line_2(), _peak_evidence())
        experiments.judge(first["id"], "keep", reason="2", path=self.ledger_path)
        experiments.sweep(_peak_evidence(muted_2515=False), path=self.ledger_path)     # adopted
        copy = self.registered(self.line_2(track_id="21", clip_ids=("9566",), label="rotation on Bassline"),
                               self.recreated(track_id="21"))
        self.assertEqual(copy["registration"], "new")
        self.assertEqual(copy["new_round_of"]["differs"], "copy_on_his_track")
        experiments.judge(copy["id"], "keep", reason="mine now", path=self.ledger_path)
        report = experiments.sweep(self.recreated(track_id="21"), path=self.ledger_path)
        self.assertEqual(report["arrangement"]["adopted_by"]["on_his_originals"], 1)
        # the same copy registered again in this run is that row, not a third
        third = self.registered(self.line_2(track_id="21", clip_ids=("9566",), label="rotation on Bassline"),
                                self.recreated(track_id="21"))
        self.assertEqual(third["registration"], "refused")
        self.assertIn("nothing changed since", third["reason"])

    def test_survived_and_seen_again_on_the_sidekick_track_stays_an_observation(self):
        first = self.registered(self.line_2(), _peak_evidence())
        experiments.judge(first["id"], "keep", reason="2", path=self.ledger_path)
        experiments.sweep(_peak_evidence(muted_2515=False), path=self.ledger_path)
        again = self.registered(self.line_2(clip_ids=("9566",)), self.recreated(muted=False))
        self.assertEqual(again["registration"], "observed")
        with self.assertRaisesRegex(ValueError, "already in the score"):
            experiments.judge(first["id"], "keep", reason="again", path=self.ledger_path)

    def test_observations_are_idempotent(self):
        first = self.registered(self.line_2(), _peak_evidence())
        experiments.judge(first["id"], "keep", reason="2", path=self.ledger_path)
        experiments.sweep(_peak_evidence(muted_2515=False), path=self.ledger_path)
        one = self.registered(self.line_2(clip_ids=("9566",)), self.recreated(muted=False))
        two = self.registered(self.line_2(clip_ids=("9566",)), self.recreated(muted=False))
        self.assertEqual((one["registration"], two["registration"]), ("observed", "observed"))
        self.assertFalse(one["already_recorded"])
        self.assertTrue(two["already_recorded"])
        row = experiments.find_row(ledger.load(self.ledger_path), first["id"])
        self.assertEqual(len(row["survival_observations"]), 1)
        self.assertEqual(len(row["survival_observations"][0]["digest"]), 64)
        # read-only: what a preview would say
        verified, _ = experiments.verify_candidate(self.line_2(clip_ids=("9566",)),
                                                   self.recreated(muted=False),
                                                   expected_set_path=SET_PATH)
        (would,) = experiments.observed_on([verified], path=self.ledger_path)
        self.assertEqual((would["entry_id"], would["state"]), (first["id"], "adopted"))
        self.assertEqual(len(experiments.all_rows(ledger.load(self.ledger_path))), 1)


@on_both_versions
class ReproposalToolTests(unittest.TestCase):
    setUp = _tools.ExperimentToolTests.setUp
    journal = _tools.ExperimentToolTests.journal
    rows = _tools.ExperimentToolTests.rows
    register_peak = _tools.ExperimentToolTests.register_peak

    def test_abandoned_line_re_added_is_a_new_round_he_can_keep(self):
        ids = self.register_peak()
        self.device.remove_clip("2566")
        first = server.close_session()
        self.assertEqual(first["arrangement"]["abandoned"], 1)
        with self.assertRaisesRegex(ValueError, "re-proposal"):
            server.judge_answer("keep", entry_id=ids["peak line 2 rotation"])
        self.device.add_clip(41, "9566", "2 rotation", 119, (69, 71, 73, 74))
        out = server.register_arrangement_candidates(
            items=[{"kind": "arrangement_experiment", "label": "rotation, again",
                    "track_id": "2515", "clip_ids": ["9566"]}], set_path=SET_PATH, confirm=True)
        (again,) = out["registered"]
        self.assertFalse(again["already_registered"])
        self.assertEqual(again["new_round_of"]["differs"], "re-proposed_after_abandoned")
        self.assertEqual(out["observed"], [])
        server.judge_answer("keep", entry_id=again["entry_id"], reason="yes")
        self.device.set_muted("2515", False)
        close = server.close_session()
        self.assertEqual((close["arrangement"]["judged_total"], close["arrangement"]["adopted"]), (1, 1))

    def test_preview_says_it_would_be_an_observation(self):
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 2 rotation"], reason="2")
        self.device.set_muted("2515", False)
        server.close_session()
        self.device.restart_live(renumber=True)
        preview = server.register_arrangement_candidates(
            items=[{"kind": "arrangement_experiment", "label": "rotation again",
                    "track_id": "3515", "clip_ids": ["3566"]}], set_path=SET_PATH)
        (candidate,) = preview["verified"]
        self.assertEqual(candidate["would_observe"]["entry_id"], ids["peak line 2 rotation"])
        self.assertIn("would be recorded as an observation on", preview["note"])
        self.assertEqual(len(self.rows()), 3)
        with self.assertRaisesRegex(ValueError, "already in the score"):
            server.judge_answer("keep", entry_id=ids["peak line 2 rotation"])


# --- item 6: the docs say what the code does ------------------------------------

class DocsTests(unittest.TestCase):
    def read(self, *parts):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, *parts), encoding="utf-8") as fh:
            return fh.read()

    @staticmethod
    def flat(text):
        return " ".join(text.split())

    def test_mcp_reference_carries_the_accounting_contract(self):
        text = self.read("config", "claude_mcp.md")
        section = self.flat(text.split("### Arrangement experiments and effect trials", 1)[1]
                            .split("\n## ", 1)[0])
        for phrase in ("rate_status", "judged_total", "resolved_judged", "coverage",
                       "never turn an unknown into a drop", "origin_track", "sidekick_track_renamed",
                       "survival_observations", "excluded_unchanged_rounds", "evidence_mismatch",
                       "params_coverage", "kept_params_uncompared",
                       "never that ducking works",
                       # round 2
                       "Gone is proven, never inferred", "device_read_failed", "id_recycled",
                       "only Live's own \"id … does not exist\" for that id proves absence",
                       "a degraded read is never Live's word",
                       "re-proposed_after_<state>", "copy_on_his_track", "already_recorded",
                       "on_sidekick_named_track", "not beside its parent and not alone"):
            self.assertIn(phrase, section, phrase)

    def test_playbook_carries_coverage_observation_and_the_effect_route(self):
        text = self.read("docs", "CURSOR_CHAT_PLAYBOOK.md")
        candidates = self.flat(text.split("## Arrangement candidates", 1)[1].split("\n## ", 1)[0])
        for phrase in ("Read the coverage before the rate", "rate_status: definitive",
                       "Observation vs round", "never a second judged row",
                       "a new round he can judge", "a DROP last week never forbids a KEEP tonight",
                       "Kept ≠ audible ducking", "never report \"ducking works\"",
                       "only when Live itself says the id no longer exists",
                       "only Live's does-not-exist proves absence",
                       "a timed-out or empty read"):
            self.assertIn(phrase, candidates, phrase)
        self.assertNotIn("with no object", self.flat(self.read("config", "claude_mcp.md")))
        self.assertNotIn("there is nothing to re-judge", candidates)
        after = self.flat(text.split("## After an answer", 1)[1].split("\n## ", 1)[0])
        for phrase in ("register_arrangement_candidates", "judge_answer('keep'|'drop', entry_id=",
                       "close_session()", "observed", "presence, not a heard effect"):
            self.assertIn(phrase, after, phrase)


if __name__ == "__main__":
    unittest.main()
