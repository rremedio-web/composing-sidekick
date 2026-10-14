"""B11 round 4: a closed row with nothing changed is not a new experiment;
dedupe keys on ids regardless of track; Live recycles object ids within a
run, so a by-id read is "present" only as a clip on the expected track.
Helpers and the V4Device fake come from the round-1 modules."""
from __future__ import annotations

import unittest

from mcp_server import experiments, ledger
from tests.pp_fakes import on_both_versions
from tests.test_experiments_rows import (EPOCH_B, IDENTITY, PEAK_JOURNAL, SET_PATH, _LedgerCase,
                                         _live, _loc, _peak_evidence,
                                         _peak_evidence_after_restart, _track)
from tests import test_experiments_tools as _tools

server = _tools.server


def _verified(cand, evidence):
    row, why = experiments.verify_candidate(cand, evidence, expected_set_path=SET_PATH)
    assert why is None, why
    return row


def _line_2(track_id="2515", clip_ids=("2566",), label="peak line 2 rotation"):
    return {"kind": experiments.ARRANGEMENT_KIND, "label": label, "track_id": track_id,
            "clip_ids": list(clip_ids), "group": "v4-peak-compose", "move": "arr:variant",
            "journal": None}


class ClosedRowTests(_LedgerCase):
    """Item 1: the same ids and content as a CLOSED row is that experiment,
    already scored — refused; a changed digest or another run is new, and
    says which row it follows."""

    def close_line_2(self):
        (row,) = experiments.register([_verified(_line_2(), _peak_evidence())], path=self.ledger_path)
        experiments.judge(row["id"], "keep", reason="a", path=self.ledger_path)
        experiments.sweep(_peak_evidence(muted_2515=False), path=self.ledger_path)
        stored = experiments.find_row(ledger.load(self.ledger_path), row["id"])
        self.assertEqual(stored["terminal_state"], "adopted")
        return stored

    def test_nothing_changed_since_the_close_is_refused(self):
        closed = self.close_line_2()
        (out,) = experiments.register([_verified(_line_2(), _peak_evidence())], path=self.ledger_path)
        self.assertEqual(out["registration"], "refused")
        self.assertIn(closed["id"], out["reason"])
        self.assertIn("closed as adopted", out["reason"])
        self.assertIn(closed["terminal_at"], out["reason"])
        self.assertIn("nothing changed since", out["reason"])
        self.assertEqual(len(experiments.all_rows(ledger.load(self.ledger_path))), 1)

    def test_changed_content_is_a_new_round_that_names_the_closed_row(self):
        closed = self.close_line_2()
        evidence = _peak_evidence()
        evidence["clips"]["2566"] = _live("2566", 69, 71, 73, 75)
        (out,) = experiments.register([_verified(_line_2(), evidence)], path=self.ledger_path)
        self.assertEqual(out["registration"], "new")
        self.assertEqual(out["new_round_of"]["entry_id"], closed["id"])
        self.assertEqual(out["new_round_of"]["state"], "adopted")
        self.assertEqual(out["new_round_of"]["differs"], "content")
        self.assertEqual(len(experiments.all_rows(ledger.load(self.ledger_path))), 2)

    def test_another_run_with_the_same_content_is_an_observation_not_a_round(self):
        # B12 (review-5d finding 3): unchanged material in another Live run
        # is next-session presence on the closed row, never a second judged row
        closed = self.close_line_2()
        cand = _line_2(track_id="3515", clip_ids=("3566",))
        (out,) = experiments.register([_verified(cand, _peak_evidence_after_restart())],
                                      path=self.ledger_path)
        self.assertEqual(out["registration"], "observed")
        self.assertEqual(out["id"], closed["id"])
        self.assertEqual(out["differs"], "epoch")
        self.assertEqual(len(experiments.all_rows(ledger.load(self.ledger_path))), 1)


class TrackIndependentDedupeTests(_LedgerCase):
    """Item 2: one clip id, one row — whatever track it sits on now."""

    def moved_evidence(self):
        """2566 moved from the SIDEKICK rotation track onto Bassline (21)."""
        evidence = _peak_evidence()
        evidence["clip_locations"]["2566"] = _loc("2566", "21", "25-Bassline", "t24[119|1]")
        evidence["clips"]["2566"] = _live("2566", 69, 71, 73, 74, track_index=24)
        return evidence

    def test_the_same_clip_on_his_track_rebinds_the_pending_row_with_moved_to(self):
        (first,) = experiments.register([_verified(_line_2(), _peak_evidence())], path=self.ledger_path)
        (out,) = experiments.register([_verified(_line_2(track_id="21", label="rotation on Bassline"),
                                                 self.moved_evidence())], path=self.ledger_path)
        self.assertEqual(out["registration"], "rebound")
        self.assertEqual(out["id"], first["id"])
        self.assertEqual(out["moved_to"], {"from_track_id": "2515", "to_track_id": "21"})
        stored = experiments.find_row(ledger.load(self.ledger_path), first["id"])
        self.assertEqual(stored["track"]["id"], "21")
        self.assertFalse(stored["track"]["sidekick"])
        self.assertEqual(stored["label"], "peak line 2 rotation")
        self.assertEqual([c["clip_id"] for c in stored["clips"]], ["2566"])
        self.assertEqual(stored["rebinds"][0]["to_track_id"], "21")
        self.assertEqual(len(experiments.all_rows(ledger.load(self.ledger_path))), 1)
        experiments.judge(first["id"], "keep", reason="a", path=self.ledger_path)
        report = experiments.sweep(self.moved_evidence(), path=self.ledger_path)
        self.assertEqual(report["arrangement"]["resolved_judged"], 1)
        self.assertEqual(report["arrangement"]["adopted_by"]["on_his_originals"], 1)

    def test_a_judged_row_refuses_the_moved_registration_and_is_never_doubled(self):
        (first,) = experiments.register([_verified(_line_2(), _peak_evidence())], path=self.ledger_path)
        experiments.judge(first["id"], "keep", reason="a", path=self.ledger_path)
        (out,) = experiments.register([_verified(_line_2(track_id="21", label="rotation on Bassline"),
                                                 self.moved_evidence())], path=self.ledger_path)
        self.assertEqual(out["registration"], "refused")
        self.assertIn(first["id"], out["reason"])
        self.assertEqual(len(experiments.all_rows(ledger.load(self.ledger_path))), 1)
        report = experiments.sweep(self.moved_evidence(), path=self.ledger_path)
        self.assertEqual(report["arrangement"]["resolved_judged"], 1)

    def test_overlap_across_tracks_is_refused(self):
        experiments.register([_verified(_line_2(), _peak_evidence())], path=self.ledger_path)
        evidence = self.moved_evidence()
        evidence["clip_locations"]["2599"] = _loc("2599", "21", "25-Bassline", "t24[151|1]")
        evidence["clips"]["2599"] = _live("2599", 40, 41, track_index=24)
        (out,) = experiments.register([_verified(_line_2(track_id="21", clip_ids=("2566", "2599"),
                                                         label="both on Bassline"), evidence)],
                                      path=self.ledger_path)
        self.assertEqual(out["registration"], "refused")
        self.assertIn("2566", out["reason"])


class IdRecyclingTests(_LedgerCase):
    """Item 3 (observed live 2026-09-30): Live reuses object ids within a run,
    so an id read by itself proves nothing — "present" needs a clip on the
    expected track; anything else is unknown, never present or absent."""

    def keep_line_2(self):
        (row,) = experiments.register([_verified(_line_2(), _peak_evidence())], path=self.ledger_path)
        experiments.judge(row["id"], "keep", reason="a", path=self.ledger_path)
        return row["id"]

    def state(self, entry_id):
        row = experiments.find_row(ledger.load(self.ledger_path), entry_id)
        return row["terminal_state"], row["observation"]

    def test_present_id_whose_read_is_a_clip_elsewhere_is_unknown(self):
        entry_id = self.keep_line_2()
        evidence = _peak_evidence(muted_2515=False)
        evidence["clips"]["2566"] = _live("2566", 69, 71, 73, 74, track_index=24)  # not track 41
        experiments.sweep(evidence, path=self.ledger_path)
        state, obs = self.state(entry_id)
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "object_mismatch")

    def test_absent_id_now_naming_a_device_is_unknown_not_gone(self):
        entry_id = self.keep_line_2()
        evidence = _peak_evidence(clips_2515=())
        evidence["clips"]["2566"] = {"clip_id": "2566", "error": "id 2566 names live_set tracks 24 "
                                     "devices 4, not a clip on a track", "mismatch": True,
                                     "object_path": "live_set tracks 24 devices 4"}
        report = experiments.sweep(evidence, path=self.ledger_path)
        state, obs = self.state(entry_id)
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "id_recycled:live_set tracks 24 devices 4")
        self.assertEqual(report["arrangement"]["not_adopted"], 0)

    def test_absent_id_that_live_says_does_not_exist_is_gone(self):
        entry_id = self.keep_line_2()
        experiments.sweep(_peak_evidence(clips_2515=()), path=self.ledger_path)
        self.assertEqual(self.state(entry_id)[0], "not_adopted")

    def test_absent_id_with_any_other_read_failure_is_unknown(self):
        entry_id = self.keep_line_2()
        evidence = _peak_evidence(clips_2515=())
        evidence["clips"]["2566"] = {"clip_id": "2566", "error": "ConnectionError: reset"}
        experiments.sweep(evidence, path=self.ledger_path)
        state, obs = self.state(entry_id)
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "id_unreadable")

    def test_absent_id_still_readable_as_a_clip_is_unknown(self):
        entry_id = self.keep_line_2()
        evidence = _peak_evidence(clips_2515=())
        evidence["clips"]["2566"] = _live("2566", 69, 71, 73, 74)     # a clip the inventory lacks
        experiments.sweep(evidence, path=self.ledger_path)
        state, obs = self.state(entry_id)
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "inventory_disagrees")

    def test_registration_refuses_an_id_that_is_not_a_clip_on_its_track(self):
        evidence = _peak_evidence()
        evidence["clips"]["2566"] = _live("2566", 69, 71, 73, 74, track_index=24)
        row, why = experiments.verify_candidate(_line_2(), evidence, expected_set_path=SET_PATH)
        self.assertIsNone(row)
        self.assertIn("2566", why)
        self.assertIn("not a clip on track 2515", why)

    def test_cross_run_rebind_needs_a_clip_on_the_named_track(self):
        entry_id = self.keep_line_2()
        evidence = _peak_evidence_after_restart(muted_2515=False)
        evidence["clips"]["3566"] = _live("3566", 69, 71, 73, 74, track_index=24)
        experiments.sweep(evidence, path=self.ledger_path)
        state, obs = self.state(entry_id)
        self.assertIsNone(state)
        self.assertEqual(obs["reason"], "cross_epoch_unresolved")


@on_both_versions
class Round4ToolTests(unittest.TestCase):
    setUp = _tools.ExperimentToolTests.setUp
    journal = _tools.ExperimentToolTests.journal
    rows = _tools.ExperimentToolTests.rows
    register_peak = _tools.ExperimentToolTests.register_peak

    def test_re_registering_after_a_close_with_nothing_changed_is_refused(self):
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 2 rotation"], reason="2")
        self.device.set_muted("2515", False)
        first = server.close_session()
        self.assertEqual(first["arrangement"]["adopted"], 1)
        again = server.register_arrangement_candidates(journal_path=self.peak, confirm=True)
        refused = next(r for r in again["refused"] if r["label"] == "peak line 2 rotation")
        self.assertIn("closed as adopted", refused["reason"])
        self.assertIn(ids["peak line 2 rotation"], refused["reason"])
        # the two unjudged siblings are still pending and simply return
        self.assertEqual(len(self.rows()), 3)
        self.assertTrue(all(c["already_registered"] for c in again["registered"]))
        # a changed line is a new round, and says so
        self.device.replace_notes("2566", (69, 71, 73, 76))
        third = server.register_arrangement_candidates(journal_path=self.peak, confirm=True)
        line_2 = next(c for c in third["registered"] if c["label"] == "peak line 2 rotation")
        self.assertFalse(line_2["already_registered"])
        self.assertEqual(line_2["new_round_of"]["entry_id"], ids["peak line 2 rotation"])
        self.assertEqual(len(self.rows()), 4)
        server.judge_answer("keep", entry_id=line_2["entry_id"], reason="better")
        second = server.close_session()
        self.assertEqual(second["arrangement"]["resolved_judged"], 2)   # two rounds, two verdicts

    def test_registering_the_moved_clip_on_his_track_is_the_same_row(self):
        ids = self.register_peak()
        self.device.move_clip_to_track("2566", 24)     # onto Bassline
        out = server.register_arrangement_candidates(
            items=[{"kind": "arrangement_experiment", "label": "rotation on Bassline",
                    "track_id": "21", "clip_ids": ["2566"]}], set_path=SET_PATH, confirm=True)
        (entry,) = out["registered"]
        self.assertEqual(entry["entry_id"], ids["peak line 2 rotation"])
        self.assertTrue(entry["already_registered"])
        self.assertEqual(entry["moved_to"], {"from_track_id": "2515", "to_track_id": "21"})
        self.assertEqual(len(self.rows()), 3)
        server.judge_answer("keep", entry_id=ids["peak line 2 rotation"], reason="2")
        close = server.close_session()
        self.assertEqual(close["arrangement"]["resolved_judged"], 1)
        self.assertEqual(close["arrangement"]["adopted_by"]["on_his_originals"], 1)

    def test_a_deleted_clips_id_reused_by_a_device_or_track_is_unknown_at_close(self):
        for as_kind in ("device", "track"):
            with self.subTest(recycled_as=as_kind):
                self.setUp()
                ids = self.register_peak()
                server.judge_answer("keep", entry_id=ids["peak line 1 space"], reason="1")
                self.device.recycle_id("2565", as_kind)
                close = server.close_session()
                self.assertTrue(close["inventory_coverage"])
                self.assertEqual((close["arrangement"]["not_adopted"],
                                  close["arrangement"]["unknown"]), (0, 1))
                row = experiments.find_row(ledger.load(self.ledger_path), ids["peak line 1 space"])
                self.assertIsNone(row["terminal_state"])
                self.assertTrue(row["observation"]["reason"].startswith("id_recycled:live_set tracks"))
                # registering the id again is refused: it is not a clip in the Set
                again = server.register_arrangement_candidates(
                    items=[{"kind": "arrangement_experiment", "label": "ghost",
                            "track_id": "2483", "clip_ids": ["2565"]}], set_path=SET_PATH, confirm=True)
                self.assertEqual(again["registered"], [])
                self.assertIn("2565", again["refused"][0]["reason"])

    def test_a_truly_deleted_clip_is_still_not_adopted(self):
        ids = self.register_peak()
        server.judge_answer("keep", entry_id=ids["peak line 1 space"], reason="1")
        self.device.remove_clip("2565")
        close = server.close_session()
        self.assertEqual(close["arrangement"]["not_adopted"], 1)


if __name__ == "__main__":
    unittest.main()
