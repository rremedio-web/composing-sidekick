"""The step-6 sweep: truth table, definitive rate, sticky presence (ledger).

The truth table is the metric definition — verdict at evidence x presence
evidence — and the definitive rule says exactly when keep_rate may exist.
Unjudged material never inflates it: verdict-less rows are listed, never
counted, and identity-less rows are unscoreable by any sweep.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import ledger


IDENTITY = {"set_id": "set-A", "runtime_epoch": "run-1", "file_path": "/tmp/a.als"}
OTHER_EPOCH = {"set_id": "set-A", "runtime_epoch": "run-2", "file_path": "/tmp/a.als"}
UNSAVED = {"set_id": "unsaved:run-1", "runtime_epoch": "run-1", "file_path": None}
SAVED = {"set_id": "/music/a.als", "runtime_epoch": "run-1", "file_path": "/music/a.als"}
REF = {"clip_id": "clip-1", "track_id": "track-1", "track": "SIDEKICK JAM",
       "surface": "session", "path": "3/0"}
NOTES = [{"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 90},
         {"pitch": 64, "start": 1.0, "duration": 0.5, "velocity": 80}]
OTHER_NOTES = [{"pitch": 67, "start": 0.0, "duration": 0.5, "velocity": 70},
               {"pitch": 71, "start": 2.0, "duration": 0.5, "velocity": 70}]

T0 = 1_000_000.0

TRUTH_TABLE = [
    # (verdict, presence outcome, expected state)
    ("keep", "present", "keep"),
    ("keep", "jam_copy", "keep"),
    ("keep", "absent", "drop"),
    ("keep", "unresolved", None),
    ("drop", "present", "drop"),
    ("drop", "jam_copy", "drop"),
    ("drop", "absent", "drop"),
    ("drop", "unresolved", "drop"),
    (None, "present", "unjudged"),
    (None, "jam_copy", "unjudged"),
    (None, "absent", "abandoned"),
    (None, "unresolved", None),
]


def clip(clip_id="clip-1", notes=NOTES, name="idea"):
    return {"clip_id": clip_id, "track_id": "track-1", "track": "SIDEKICK JAM",
            "surface": "session", "path": "4/0", "name": name,
            "answer_notes": [dict(n) for n in notes]}


class LedgerSweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "ledger.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, verdict=None, identity=IDENTITY, clip_id="clip-1",
              notes=NOTES, now=T0, slot="3/0", name="idea", path=None):
        path = path or self.path
        ref = {**REF, "clip_id": clip_id}
        if notes is not None:
            ref["answer_notes"] = [dict(n) for n in notes]
        with patch.object(ledger, "_now", return_value=now):
            entry = ledger.record_write(
                clip_name=name, slot=slot, source="catalogue", role="lead", gear=5,
                set_identity=identity, clip_ref=ref,
                answer_notes=[dict(n) for n in notes] if notes is not None else None,
                path=path)
        if verdict is not None:
            with patch.object(ledger, "_now", return_value=now + 1):
                ledger.record_verdict(verdict, entry_id=entry["id"], path=path)
        return entry

    def sweep(self, identity=IDENTITY, clips=(), complete=True, now=T0 + 120,
              session_id=None, path=None):
        with patch.object(ledger, "_now", return_value=now):
            return ledger.close_session(
                {"set_identity": identity, "complete": complete, "errors": [],
                 "clips": list(clips)},
                session_id=session_id, path=path or self.path)

    def row(self, entry_id, path=None):
        return next(e for e in ledger.load(path or self.path)["entries"]
                    if e["id"] == entry_id)

    def fresh_path(self, *parts):
        return str(Path(self.tmp.name) / ("l-%s.json" % "-".join(str(p) for p in parts)))

    def test_truth_table_verdict_x_presence(self):
        for verdict, outcome, want in TRUTH_TABLE:
            with self.subTest(verdict=verdict, outcome=outcome):
                path = self.fresh_path(verdict, outcome)
                entry = self.write(verdict=verdict, path=path)
                if outcome == "jam_copy":
                    # jam_copy presence is written by adoption scanning; inject
                    # it as a first sweep would, then sweep: sticky, scored as
                    # present.
                    data = ledger.load(path)
                    row = next(e for e in data["entries"] if e["id"] == entry["id"])
                    row["presence"] = {"state": "jam_copy",
                                       "match": "unique_content_context",
                                       "swept_at": ledger._iso(T0 + 60),
                                       "matched_clips": [], "adoption": None}
                    Path(path).write_text(json.dumps(data))
                if outcome == "present":
                    result = self.sweep(clips=[clip()], path=path)
                elif outcome == "absent":
                    result = self.sweep(clips=[], path=path)
                elif outcome == "unresolved":
                    result = self.sweep(identity=OTHER_EPOCH,
                                        clips=[clip("copy-a"), clip("copy-b")],
                                        path=path)
                else:
                    result = self.sweep(clips=[clip()], path=path)
                row = self.row(entry["id"], path=path)
                self.assertEqual(row.get("terminal_state"), want)
                if outcome in ("present", "absent"):
                    self.assertEqual(row["presence"]["state"],
                                     "present" if outcome == "present" else "absent")
                if verdict == "keep" and outcome == "unresolved":
                    self.assertIsNone(result["keep_rate"])
                    self.assertEqual(result["rate_status"], "partial")
                    self.assertEqual(result["unresolved"][0]["reason"],
                                     "ambiguous_content_context")
                elif verdict is None and outcome == "unresolved":
                    # listed, but it never withholds the rate
                    self.assertEqual(result["rate_status"], "definitive")
                    self.assertIsNone(result["keep_rate"])  # nothing judged
                    self.assertEqual(len(result["unresolved"]), 1)
                else:
                    self.assertEqual(result["rate_status"], "definitive",
                                     result)

    def test_unresolved_verdictless_row_does_not_withhold_rate(self):
        kept = self.write(verdict="keep")
        idless_notes = self.write(name="no notes", slot="3/1", clip_id="clip-2",
                                  notes=None)
        self.assertIsNotNone(idless_notes)
        result = self.sweep(identity=OTHER_EPOCH, clips=[clip("new-id")])
        self.assertEqual(result["rate_status"], "definitive")
        self.assertEqual(result["counts"]["keep"], 1)
        self.assertEqual(result["keep_rate"], 1.0)
        self.assertEqual([r["clip_name"] for r in result["unresolved"]], ["no notes"])
        self.assertEqual(self.row(kept["id"])["terminal_state"], "keep")
        # the harsher reading stays withheld while a row could still resolve
        self.assertIsNone(result["keep_rate_if_abandoned_counted_as_drop"])

    def test_identityless_keep_is_unscoreable_and_blocks_the_seal(self):
        """fix2 item 1(c): an unattested identity-less row never enters the
        rate, but the night cannot be sealed around it — the sweep scores
        what it can and the session stays open until the owner attests or
        retires the ghost, so the reminder cannot go quiet."""
        tracked = self.write(verdict="keep")
        with patch.object(ledger, "_now", return_value=T0 + 5):
            ghost = ledger.record_write(clip_name="ghost", slot="3/9",
                                        source="catalogue", path=self.path)
            judged = ledger.record_verdict("keep", entry_id=ghost["id"],
                                           path=self.path)
        self.assertIsNone(ghost["set_identity"])
        self.assertIsNone(judged.get("late"))
        result = self.sweep(clips=[clip()])
        self.assertEqual(result["rate_status"], "partial")
        self.assertIsNone(result["keep_rate"])
        # the verified keep is scored and bounds the rate on its own
        self.assertEqual(result["keep_rate_bounds"], [1.0, 1.0])
        # the ghost KEEP is judged but unscoreable: counted in the
        # unscoreable stratum, never inside the verified numbers
        self.assertEqual(result["unscoreable"], {"rows": 1, "judged": 1})
        self.assertEqual(result["unresolved"], [])
        session = ledger.load(self.path)["sessions"][-1]
        self.assertIsNone(session.get("closed_at"))
        self.assertTrue(any("attest or retire" in item for item in
                            ledger.summary(self.path)["advice"]))
        self.assertIsNone(self.row(tracked["id"])["presence"].get("adoption"))
        self.assertIsNone(self.row(ghost["id"]).get("terminal_state"))

    def test_unresolved_keep_withholds_rate_and_reports_bounds(self):
        self.write(verdict="keep", slot="3/0", clip_id="clip-1", notes=NOTES)
        self.write(verdict="drop", name="other", slot="3/1", clip_id="clip-2",
                   notes=OTHER_NOTES)
        result = self.sweep(identity=OTHER_EPOCH,
                            clips=[clip("dup-a"), clip("dup-b"),
                                   clip("drop-clip", OTHER_NOTES, name="other")])
        self.assertEqual(result["rate_status"], "partial")
        self.assertIsNone(result["keep_rate"])
        # summed-counts interval: k=0, j=1, p=1 -> [0/2, 1/2]
        self.assertEqual(result["keep_rate_bounds"], [0.0, 0.5])
        self.assertEqual(result["counts"], {"keep": 0, "drop": 1,
                                            "abandoned": 0, "unjudged": 0})
        self.assertTrue(all(s.get("closed_at") is None
                            for s in ledger.load(self.path)["sessions"]))
        self.assertIn("withheld", result["warning"])

    def test_all_drop_ended_night_is_definitive_on_partial_inventory(self):
        """fix2 item 2: a DROP resolves by his word alone, so an ENDED night
        whose scoreable rows are all DROP verdicts is definitive even when
        the inventory came back partial (repro: correct/x1.py item13 — the
        night was ended by a set change before anyone could sweep it)."""
        self.write(verdict="drop", clip_id="clip-d1", name="d1",
                   notes=OTHER_NOTES)
        # a write to another Set ends tonight's night (set_change)
        other = {"set_id": "set-B", "runtime_epoch": "run-b", "file_path": None}
        self.write(verdict=None, identity=other, clip_id="clip-b1",
                   name="b1", notes=NOTES, now=T0 + 100)
        result = self.sweep(identity=IDENTITY, clips=(), complete=False,
                            now=T0 + 200)
        self.assertEqual(result["rate_status"], "definitive", result)
        self.assertEqual(result["counts"], {"keep": 0, "drop": 1,
                                            "abandoned": 0, "unjudged": 0})
        self.assertEqual(result["keep_rate"], 0.0)
        ended = ledger.load(self.path)["sessions"][0]
        self.assertIsNotNone(ended.get("closed_at"))

    def test_partial_inventory_stays_partial_for_keeps_or_unjudged(self):
        """The all-drop alternative is narrow: a KEEP needs presence
        evidence and an unjudged row needs the inventory, so a partial read
        keeps their night open for a real sweep."""
        self.write(verdict="drop", clip_id="clip-d1", name="d1",
                   notes=OTHER_NOTES, slot="3/0")
        self.write(verdict="keep", clip_id="clip-k1", name="k1",
                   notes=NOTES, slot="3/1")
        result = self.sweep(clips=(), complete=False)
        self.assertEqual(result["rate_status"], "partial")
        self.assertIsNone(ledger.load(self.path)["sessions"][0].get("closed_at"))

        path = self.fresh_path("unjudged")
        self.write(verdict="drop", clip_id="clip-d1", name="d1",
                   notes=OTHER_NOTES, path=path)
        self.write(verdict=None, clip_id="clip-u1", name="u1",
                   notes=NOTES, slot="3/1", path=path)
        # end the night by moving to another Set, then sweep partially
        self.write(verdict=None, identity=OTHER_EPOCH, clip_id="clip-b1",
                   name="b1", notes=NOTES, now=T0 + 100, path=path)
        result = self.sweep(clips=(), complete=False, path=path)
        self.assertEqual(result["rate_status"], "partial", result)
        self.assertIsNone(ledger.load(path)["sessions"][0].get("closed_at"))

    def test_partial_inventory_records_coverage_only(self):
        # A bare list is incomplete evidence: it records coverage but never
        # ends, seals, or scores the night. A later KEEP is not late, the
        # next write still joins, and the next real close counts it.
        entry = self.write(verdict=None)
        partial = ledger.close_session([], path=self.path)
        session = ledger.load(self.path)["sessions"][0]
        self.assertIsNone(session.get("swept_at"))
        self.assertIsNone(session.get("ended_at"))
        self.assertIsNone(session.get("swept_complete"))
        self.assertEqual(session["sweep_coverage"]["complete"], False)
        with patch.object(ledger, "_now", return_value=T0 + 600):
            judged = ledger.record_verdict("keep", entry_id=entry["id"],
                                           path=self.path)
        self.assertFalse(judged.get("late"), "nothing sealed the night yet")
        with patch.object(ledger, "_now", return_value=T0 + 610):
            later = self.write(name="later", clip_id="clip-2", slot="3/1",
                               notes=OTHER_NOTES, path=self.path)
        self.assertEqual(later["session_id"], entry["session_id"])
        result = self.sweep(clips=[clip(), clip("clip-2", OTHER_NOTES)],
                            now=T0 + 700)
        self.assertEqual(result["rate_status"], "definitive")
        self.assertEqual(result["counts"]["keep"], 1)
        self.assertEqual(result["counts"]["unjudged"], 1)
        self.assertFalse(self.row(entry["id"]).get("late"))
        self.assertEqual(self.row(entry["id"])["terminal_state"], "keep")

    def test_every_close_report_carries_coverage_complete(self):
        # Whatever an inventory could prove, its close report always states
        # whether the coverage behind it was complete: False for a partial
        # inventory or a night no inventory can sweep (identity-less), True
        # for a complete one — and still True when a sealed night's report
        # is recomputed by a later close.
        with self.subTest("identity-less night, bare list"):
            path = self.fresh_path("cov-idless")
            with patch.object(ledger, "_now", return_value=T0):
                ledger.record_write(clip_name="x", slot="1/1", source="catalogue",
                                    path=path)
            report = ledger.close_session([], path=path)
            self.assertTrue(report["ok"])
            self.assertIs(report["coverage"]["complete"], False)
            self.assertIn("inventory_errors", report["coverage"])
            self.assertIn("observed_clips", report["coverage"])
            session = ledger.load(path)["sessions"][-1]
            self.assertIsNone(session.get("closed_by"))
            self.assertIsNone(session.get("ended_at"))
        with self.subTest("partial inventory over an identity-bearing night"):
            path = self.fresh_path("cov-partial")
            self.write(path=path)
            report = self.sweep(complete=False, clips=[clip()], path=path)
            self.assertTrue(report["ok"])
            self.assertIs(report["coverage"]["complete"], False)
            self.assertEqual(
                ledger.load(path)["sessions"][0]["sweep_coverage"]["complete"],
                False)
        with self.subTest("complete inventory"):
            path = self.fresh_path("cov-complete")
            self.write(verdict="keep", path=path)
            report = self.sweep(clips=[clip()], path=path)
            self.assertIs(report["coverage"]["complete"], True)
            closed = ledger.load(path)["sessions"][0]
            self.assertEqual(closed["closed_by"], "sweep")
        with self.subTest("recomputed report of a sealed night"):
            again = ledger.close_session(
                {"set_identity": IDENTITY, "complete": True, "errors": [],
                 "clips": []}, session_id=closed["id"], path=path)
            self.assertIs(again["coverage"]["complete"], True)

    def test_all_drop_night_is_definitive(self):
        # Every verified verdict row is a drop: the night is definitive,
        # every drop lands terminal, and the scoreboard counts them.
        self.write(verdict="drop", slot="3/0", clip_id="clip-1", notes=NOTES)
        self.write(name="other", verdict="drop", slot="3/1", clip_id="clip-2",
                   notes=OTHER_NOTES)
        result = self.sweep(clips=[])
        self.assertEqual(result["rate_status"], "definitive")
        self.assertEqual(result["counts"]["drop"], 2)
        self.assertEqual(result["judged"], 2)
        scoreboard = ledger.summary(path=self.path)["scoreboard"]
        self.assertEqual(scoreboard["verified"]["judged"], 2)
        self.assertEqual(scoreboard["verified"]["keep_rate"], 0.0)

    def test_contentless_runtime_id_match_is_unconfirmed_across_sets(self):
        # A clip-id match with no content on either side proves nothing once
        # the Set has an alias: without content_equal it stays unconfirmed.
        unsaved = {"set_id": "unsaved:run-1", "runtime_epoch": "run-1",
                   "file_path": None}
        saved = {"set_id": "/music/a.als", "runtime_epoch": "run-1",
                 "file_path": "/music/a.als"}
        ref = {"clip_id": "x", "track_id": "t1", "track": "SIDEKICK JAM",
               "surface": "session", "path": "3/0"}
        with patch.object(ledger, "_now", return_value=T0):
            entry = ledger.record_write(
                clip_name="idea", slot="3/0", source="catalogue", gear=5,
                set_identity=unsaved, clip_ref=dict(ref), path=self.path)
            ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
            ledger.record_write(
                clip_name="later", slot="3/1", source="catalogue", gear=5,
                set_identity=saved,
                clip_ref={**ref, "clip_id": "y"}, path=self.path)
        result = ledger.close_session(
            {"set_identity": saved, "complete": True, "errors": [],
             "clips": [{"clip_id": "x", "track_id": "t1", "track": "SIDEKICK JAM",
                        "surface": "session", "path": "3/0", "name": "x"}]},
            path=self.path)
        row = self.row(entry["id"])
        self.assertEqual(row.get("resolution_reason"), "alias_unconfirmed")
        self.assertIsNone(row.get("terminal_state"))
        self.assertEqual(result["rate_status"], "partial")

    def test_explicit_sweep_with_foreign_set_is_refused(self):
        entry = self.write(verdict="keep")
        session_id = entry["session_id"]
        foreign = {"set_id": "set-B", "runtime_epoch": "run-9", "file_path": None}
        result = ledger.close_session(
            {"set_identity": foreign, "complete": True, "errors": [], "clips": []},
            session_id=session_id, path=self.path)
        self.assertFalse(result["ok"])
        session = ledger.load(self.path)["sessions"][0]
        self.assertIsNone(session.get("swept_at"))
        self.assertIsNone(session.get("swept_complete"))
        self.assertIsNone(session.get("ended_at"))
        self.assertIsNone(self.row(entry["id"]).get("provisional_state"))

    def test_retired_only_session_never_turns_definitive(self):
        # A night whose only scoreable row was retired has no verified row
        # left to score: later complete sweeps record coverage but stay open.
        entry = self.write(verdict="keep", slot="3/0", clip_id="clip-1", notes=NOTES)
        first = self.sweep(identity=OTHER_EPOCH,
                           clips=[clip("dup-a"), clip("dup-b")])
        self.assertEqual(first["rate_status"], "partial")
        session_id = entry["session_id"]
        ledger.attest(session_id, {entry["id"]: "retire"}, confirm=True,
                      path=self.path)
        second = self.sweep(identity=OTHER_EPOCH,
                            clips=[clip("dup-a"), clip("dup-b")])
        self.assertEqual(second["rate_status"], "partial")
        session = ledger.load(self.path)["sessions"][0]
        self.assertIsNone(session.get("closed_at"))

    # NOTE: test_partial_sweep_then_verdict_is_late_and_stays_late was
    # removed on purpose: a partial inventory no longer seals the night, so
    # a verdict after one is not late (see
    # test_partial_inventory_records_coverage_only). Late now means a
    # verdict after the session's sealing complete sweep, covered by
    # test_verdict_on_closed_session_is_late_and_changes_nothing.

    def test_verdict_half_a_second_after_sealing_is_late(self):
        # Lateness compares at sub-second precision: half a second after
        # the sealing sweep is already late, even with the night still open.
        entry = self.write(verdict=None, slot="3/0", clip_id="c1",
                           notes=NOTES)
        self.write(name="ambiguous", slot="3/1", clip_id="clip-2",
                   notes=OTHER_NOTES, verdict="keep")
        self.sweep(clips=[clip("c1"), clip("amb-1", OTHER_NOTES),
                           clip("amb-2", OTHER_NOTES)], now=T0 + 120)
        session = ledger.load(self.path)["sessions"][0]
        self.assertIsNotNone(session.get("swept_at"))
        self.assertIsNone(session.get("closed_at"))
        with patch.object(ledger, "_now", return_value=T0 + 120.5):
            judged = ledger.record_verdict("keep", entry_id=entry["id"],
                                           path=self.path)
        self.assertTrue(judged.get("late"))

    def test_verdict_on_closed_session_is_late_and_changes_nothing(self):
        entry = self.write(verdict=None)
        self.sweep(clips=[clip()], now=T0 + 10)
        closed = ledger.load(self.path)["sessions"][0]
        self.assertEqual(closed["closed_by"], "sweep")
        with patch.object(ledger, "_now", return_value=T0 + 600):
            judged = ledger.record_verdict("keep", entry_id=entry["id"],
                                           path=self.path)
        self.assertTrue(judged.get("late"))
        self.assertEqual(self.row(entry["id"])["terminal_state"], "unjudged")
        report = self.sweep(session_id=closed["id"], now=T0 + 700)
        self.assertEqual(report["late_verdicts"]["counts"], {"keep": 1})
        self.assertEqual(report["counts"]["unjudged"], 1)

    def test_write_after_complete_sweep_opens_new_session(self):
        path = self.fresh_path("swept")
        first = self.write(path=path)
        self.sweep(clips=[], now=T0 + 10, path=path)
        old = ledger.load(path)["sessions"][0]
        self.assertIsNotNone(old["ended_at"])
        self.assertIsNotNone(old["closed_at"])
        # an ended session is never joined again — not by the same Set…
        with patch.object(ledger, "_now", return_value=T0 + 20):
            second = self.write(name="later", clip_id="clip-2", slot="3/1",
                                path=path)
        self.assertNotEqual(second["session_id"], first["session_id"])
        # …and not by an identity-less write either: once that new session is
        # itself swept, even an identity-less write must open another one
        self.sweep(clips=[], now=T0 + 30, path=path)
        ended = [s for s in ledger.load(path)["sessions"] if s["ended_at"]]
        self.assertEqual(len(ended), 2)
        with patch.object(ledger, "_now", return_value=T0 + 40):
            third = ledger.record_write(clip_name="idless", slot="3/2",
                                        source="catalogue", path=path)
        self.assertNotEqual(third["session_id"], second["session_id"])

    def test_aliased_sweep_without_confirmation_never_fabricates_drops(self):
        first = self.write(verdict="keep", identity=UNSAVED, slot="3/0")
        second = self.write(verdict="keep", identity=SAVED, clip_id="clip-2",
                            slot="3/1", now=T0 + 5)
        # both JAM clips gone, nothing confirmed by id+content
        result = self.sweep(identity=SAVED, clips=[], now=T0 + 60)
        self.assertEqual([r["reason"] for r in result["unresolved"]],
                         ["alias_unconfirmed", "alias_unconfirmed"])
        self.assertIsNone(result["keep_rate"])
        rows = {e["id"]: e for e in ledger.load(self.path)["entries"]}
        self.assertIsNone(rows[first["id"]].get("terminal_state"))
        self.assertIsNone(rows[second["id"]].get("terminal_state"))

    def test_aliased_sweep_with_one_confirmed_row_scores_absences(self):
        self.write(verdict="keep", identity=UNSAVED, slot="3/0")
        saved = self.write(verdict="keep", identity=SAVED, clip_id="clip-2",
                           slot="3/1", now=T0 + 5)
        # the saved-Set write is confirmed present by id AND content, so the
        # unsaved write's verified absence may score
        result = self.sweep(identity=SAVED, clips=[clip("clip-2")], now=T0 + 60)
        self.assertEqual(result["rate_status"], "definitive")
        self.assertEqual(result["counts"], {"keep": 1, "drop": 1,
                                            "abandoned": 0, "unjudged": 0})
        self.assertEqual(self.row(saved["id"])["terminal_state"], "keep")

    def test_bounds_come_from_summed_counts(self):
        # keep1/drop1/pending1 -> [k/(j+p), (k+p)/(j+p)] = [1/3, 2/3].
        pending_notes = [{"pitch": 72, "start": 0.0, "duration": 1.0, "velocity": 90},
                         {"pitch": 76, "start": 1.0, "duration": 0.5, "velocity": 80}]
        self.write(verdict="keep", slot="3/0", clip_id="clip-1", notes=NOTES)
        self.write(verdict="keep", name="pending", slot="3/1", clip_id="clip-2",
                   notes=pending_notes)
        self.write(verdict="drop", name="other", slot="3/2", clip_id="clip-3",
                   notes=OTHER_NOTES)
        result = self.sweep(identity=OTHER_EPOCH,
                            clips=[clip("a"), clip("b1", pending_notes),
                                   clip("b2", pending_notes),
                                   clip("drop-clip", OTHER_NOTES, name="other")])
        self.assertEqual(result["rate_status"], "partial")
        self.assertEqual(result["counts"]["keep"], 1)
        self.assertEqual(result["judged"], 2)
        self.assertEqual(result["keep_rate_bounds"], [round(1 / 3, 3), round(2 / 3, 3)])

    def test_bounds_two_keep_one_drop_one_pending(self):
        # keep2/drop1/pending1 -> [0.5, 0.75].
        pending_notes = [{"pitch": 72, "start": 0.0, "duration": 1.0, "velocity": 90},
                         {"pitch": 76, "start": 1.0, "duration": 0.5, "velocity": 80}]
        extra_keep = [{"pitch": 79, "start": 0.0, "duration": 1.0, "velocity": 90},
                      {"pitch": 81, "start": 1.0, "duration": 0.5, "velocity": 80}]
        self.write(verdict="keep", slot="3/0", clip_id="clip-1", notes=NOTES)
        self.write(verdict="keep", name="second", slot="3/1", clip_id="clip-2",
                   notes=extra_keep)
        self.write(verdict="keep", name="pending", slot="3/2", clip_id="clip-3",
                   notes=pending_notes)
        self.write(verdict="drop", name="other", slot="3/3", clip_id="clip-4",
                   notes=OTHER_NOTES)
        result = self.sweep(identity=OTHER_EPOCH,
                            clips=[clip("a"), clip("d", extra_keep),
                                   clip("b1", pending_notes), clip("b2", pending_notes),
                                   clip("drop-clip", OTHER_NOTES, name="other")])
        self.assertEqual(result["rate_status"], "partial")
        self.assertEqual(result["judged"], 3)
        self.assertEqual(result["keep_rate_bounds"], [0.5, 0.75])

    def test_bounds_merge_sessions_from_summed_counts(self):
        # Two partial nights merge as [k/(j+p), (k+p)/(j+p)] over the sums,
        # never by averaging per-session rates.
        pending_b = [{"pitch": 72, "start": 0.0, "duration": 1.0, "velocity": 90},
                     {"pitch": 76, "start": 1.0, "duration": 0.5, "velocity": 80}]
        pending_e = [{"pitch": 55, "start": 0.0, "duration": 1.0, "velocity": 90},
                     {"pitch": 57, "start": 1.0, "duration": 0.5, "velocity": 80}]
        extra_keep = [{"pitch": 79, "start": 0.0, "duration": 1.0, "velocity": 90},
                      {"pitch": 81, "start": 1.0, "duration": 0.5, "velocity": 80}]
        self.write(verdict="keep", slot="3/0", clip_id="clip-1", notes=NOTES, now=T0)
        self.write(verdict="keep", name="pending-b", slot="3/1", clip_id="clip-2",
                   notes=pending_b, now=T0 + 2)
        first = self.sweep(identity=OTHER_EPOCH,
                           clips=[clip("a"), clip("b1", pending_b),
                                  clip("b2", pending_b)], now=T0 + 120)
        self.assertEqual(first["rate_status"], "partial")
        # an idle gap opens a second night in the same Set
        self.write(verdict="drop", name="other", slot="3/0", clip_id="clip-9",
                   notes=OTHER_NOTES, now=T0 + 8 * 3600)
        self.write(verdict="keep", name="pending-e", slot="3/1", clip_id="clip-8",
                   notes=pending_e, now=T0 + 8 * 3600 + 2)
        self.write(verdict="keep", name="extra", slot="3/2", clip_id="clip-7",
                   notes=extra_keep, now=T0 + 8 * 3600 + 4)
        merged = self.sweep(identity=OTHER_EPOCH,
                            clips=[clip("a"), clip("b1", pending_b),
                                   clip("b2", pending_b),
                                   clip("drop-9", OTHER_NOTES, name="other"),
                                   clip("e1", pending_e), clip("e2", pending_e),
                                   clip("d", extra_keep)],
                            now=T0 + 8 * 3600 + 120)
        self.assertEqual(merged["rate_status"], "partial")
        # k=2 (NOTES, extra_keep), j=3 (+drop), p=2 (pending_b, pending_e)
        self.assertEqual(merged["counts"]["keep"], 2)
        self.assertEqual(merged["judged"], 3)
        self.assertEqual(merged["keep_rate_bounds"], [round(2 / 5, 3), round(4 / 5, 3)])

    def test_second_sweep_never_overwrites_sticky_presence(self):
        kept = self.write(verdict="keep", notes=NOTES, slot="3/0")
        self.write(name="ambiguous", slot="3/1", clip_id="clip-2",
                   notes=OTHER_NOTES, verdict="keep")
        first = self.sweep(identity=OTHER_EPOCH,
                           clips=[clip("a1"), clip("amb-1", OTHER_NOTES),
                                  clip("amb-2", OTHER_NOTES)],
                           now=T0 + 60)
        self.assertEqual(first["rate_status"], "partial")
        presence = self.row(kept["id"])["presence"]
        self.assertEqual(presence["state"], "present")
        # the clip is gone by the second sweep — presence is sticky anyway
        second = self.sweep(identity=OTHER_EPOCH,
                            clips=[clip("amb-1", OTHER_NOTES),
                                   clip("amb-2", OTHER_NOTES)],
                            now=T0 + 120)
        sticky = self.row(kept["id"])["presence"]
        self.assertEqual(sticky["state"], "present")
        self.assertEqual(sticky["swept_at"], presence["swept_at"])
        self.assertEqual(self.row(kept["id"])["provisional_state"], "keep")
        self.assertEqual(second["rate_status"], "partial")

    def test_write_presence_never_overwrites_sticky_evidence(self):
        # _write_presence is the second line of defense for sticky
        # presence: only the first complete sweep writes it, so a call
        # on a row that already carries presence changes nothing.
        entry = {"id": "e1",
                 "presence": {"state": "present", "match": "runtime_id",
                              "swept_at": "then",
                              "matched_clips": [{"clip_id": "x"}],
                              "adoption": None}}
        before = json.loads(json.dumps(entry["presence"]))
        ledger._write_presence(entry, "absent", "runtime_id_absent", [],
                               [], None, "now")
        self.assertEqual(entry["presence"], before)

    def test_unsaved_nights_sticky_claims_survive_the_first_save(self):
        """fix2 item 7: a night swept while the Set was unsaved keeps its
        clips reserved once the same Live run saves. The unsaved id and the
        saved path are the same Set — night 1's sticky claim on its kept
        clip must stop night 2's adoption scan from claiming that clip and
        keeping two nights with it."""
        unsaved = {"set_id": "unsaved:run-1", "runtime_epoch": "run-1",
                   "file_path": None}
        saved = {"set_id": "/music/a.als", "runtime_epoch": "run-1",
                 "file_path": "/music/a.als"}
        distinctive = [
            {"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 90},
            {"pitch": 62, "start": 1.0, "duration": 0.5, "velocity": 80},
            {"pitch": 64, "start": 1.5, "duration": 0.5, "velocity": 80},
            {"pitch": 65, "start": 3.0, "duration": 0.5, "velocity": 80},
            {"pitch": 67, "start": 4.0, "duration": 0.5, "velocity": 80},
            {"pitch": 69, "start": 6.0, "duration": 0.5, "velocity": 80}]

        def ref(clip_id):
            return {**REF, "clip_id": clip_id,
                    "answer_notes": [dict(n) for n in distinctive]}

        def present(clip_id):
            return {"clip_id": clip_id, "track_id": REF["track_id"],
                    "track": "SIDEKICK JAM", "surface": "session",
                    "path": "9/0", "name": clip_id,
                    "answer_notes": [dict(n) for n in distinctive]}

        night_one = self.write(verdict="keep", identity=unsaved, notes=distinctive,
                               clip_id="c1", name="night-one")
        first = self.sweep(identity=unsaved, clips=[present("c1")])
        self.assertEqual(first["rate_status"], "definitive")
        self.assertEqual(self.row(night_one["id"])["presence"]["state"],
                         "present")
        self.assertIsNotNone(
            ledger.load(self.path)["sessions"][0].get("closed_at"))

        # he saves the Set (same runtime); night 2 rewrites the same answer
        # and deletes its own clip — night 1's kept clip is still there
        with patch.object(ledger, "_now", return_value=T0 + 200):
            night_two = ledger.record_write(
                clip_name="night-two", slot="3/1", source="catalogue",
                role="lead", gear=5, set_identity=saved, clip_ref=ref("c2"),
                answer_notes=[dict(n) for n in distinctive], path=self.path)
            ledger.record_verdict("keep", entry_id=night_two["id"],
                                  path=self.path)
        second = self.sweep(identity=saved, clips=[present("c1")],
                            now=T0 + 400)
        self.assertEqual(second["adoption"]["adopted"], 0, second)
        row_two = self.row(night_two["id"])
        self.assertNotEqual(row_two.get("terminal_state"), "keep")
        self.assertEqual(row_two.get("terminal_state"), "drop",
                         "his KEEP with the clip gone and the copy already "
                         "owned by night 1 is a drop, not a second keep")
        # night 1's sticky evidence is untouched
        self.assertEqual(self.row(night_one["id"])["presence"]["state"],
                         "present")

    def test_foreign_set_sticky_claim_never_reserves_this_sweep(self):
        """fix2 item 4 (r5a): sticky presence belongs to the Set it was
        proven in. Set A's sealed night claims a BASS clip by content; Set
        B's later night writes its own clip with the same content on the
        same track. A's history must not reserve B's clip out from under
        B's own row — without the chain filter at the sticky reservation
        this very sweep would score B's KEEP as a verified DROP."""
        set_b = {"set_id": "set-B", "runtime_epoch": "run-b", "file_path": None}

        def bass_ref(clip_id):
            return {"clip_id": clip_id, "track_id": "bass-1", "track": "BASS",
                    "surface": "session", "path": "1/0",
                    "answer_notes": [dict(n) for n in NOTES]}

        def bass_clip(clip_id, name):
            return {"clip_id": clip_id, "track_id": "bass-1", "track": "BASS",
                    "surface": "session", "path": "9/0", "name": name,
                    "answer_notes": [dict(n) for n in NOTES]}

        with patch.object(ledger, "_now", return_value=T0):
            entry_a = ledger.record_write(
                clip_name="a-night", slot="1/0", source="catalogue",
                role="lead", gear=5, set_identity=IDENTITY,
                clip_ref=bass_ref("c1"),
                answer_notes=[dict(n) for n in NOTES], path=self.path)
        first = self.sweep(clips=[bass_clip("c1", "his-a")])
        self.assertEqual(self.row(entry_a["id"])["presence"]["state"],
                         "present")

        with patch.object(ledger, "_now", return_value=T0 + 200):
            entry_b = ledger.record_write(
                clip_name="b-night", slot="1/0", source="catalogue",
                role="lead", gear=5, set_identity=set_b,
                clip_ref=bass_ref("c2"),
                answer_notes=[dict(n) for n in NOTES], path=self.path)
            ledger.record_verdict("keep", entry_id=entry_b["id"], path=self.path)
        second = self.sweep(identity=set_b, clips=[bass_clip("c2", "his-b")])
        row_b = self.row(entry_b["id"])
        self.assertEqual(row_b.get("presence", {}).get("state"), "present",
                         second)
        self.assertEqual(row_b["terminal_state"], "keep")
        # A's sticky evidence is untouched by B's sweep
        self.assertEqual(self.row(entry_a["id"])["presence"]["state"],
                         "present")


if __name__ == "__main__":
    unittest.main()
