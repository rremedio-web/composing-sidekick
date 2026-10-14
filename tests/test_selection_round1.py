"""SELECTION lane round-1 review fixes (fix1_selection.md), items 1-12.

Synthetic pools throughout: no Live, no real store, no network.
"""
from __future__ import annotations

import unittest

from mcp_server.core import _single_gold_window
from tests._requires import requires_catalog


def _hit(cid, gold=False):
    return {"id": cid, "_gold": gold, "vec": [0.5]}


class TestItem1OutOfWindowGoldKeepsPosition(unittest.TestCase):
    def test_tail_gold_not_lifted_above_tail_plain(self):
        ranked = [_hit("p%d" % i) for i in range(10)]
        ranked.append(_hit("g", gold=True))
        ranked.extend([_hit("p10"), _hit("p11")])
        out = _single_gold_window(ranked, 3)
        self.assertEqual([p["id"] for p in out].index("g"), 10)

    def test_tail_order_preserved_with_bumped_head_gold(self):
        ranked = [_hit("g1", gold=True), _hit("g2", gold=True),
                  _hit("p0"), _hit("p1"), _hit("g3", gold=True),
                  _hit("p2")]
        out = _single_gold_window(ranked, 3)
        ids = [p["id"] for p in out]
        # one gold in the window; bumped g2 just below the refilled window;
        # tail gold g3 keeps its relative place after p1
        self.assertEqual(ids[:3].count("g1") + ids[:3].count("g2")
                         + ids[:3].count("g3"), 1)
        self.assertLess(ids.index("p1"), ids.index("g3"))
        self.assertLess(ids.index("g3"), ids.index("p2"))


@requires_catalog
class TestItem2OutcomeUngoldNotAReject(unittest.TestCase):
    """keep+absent un-golds without banning: the source must leave the gold
    set but never enter the reject fold."""

    def setUp(self):
        import os
        import tempfile
        from pathlib import Path
        from mcp_server import core_shared
        from mcp_server import core as C
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        prev = (core_shared._ratings_path()
                if core_shared._RATINGS_PATH_OVERRIDE else None)
        core_shared.set_ratings_path(Path(self._td.name) / "ratings.json")
        self.addCleanup(core_shared.set_ratings_path, prev)
        self.C = C
        from build_index import phrase_vector
        self._phrase_vector = phrase_vector
        self.notes = [
            {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90},
            {"pitch": 64, "start": 0.5, "duration": 0.5, "velocity": 85},
            {"pitch": 67, "start": 1.0, "duration": 1.0, "velocity": 90},
            {"pitch": 72, "start": 2.0, "duration": 2.0, "velocity": 88},
        ]
        self.pid = "src|x|w0"
        C.rate(rating=1, label="gold", phrase_id=self.pid, notes=self.notes)

    def test_keep_absent_neither_gold_nor_rejected(self):
        from mcp_server import server as S
        from mcp_server import taste_bridge as TB
        entry = {"id": "e-1", "session_id": "s-1",
                 "immediate_verdict": "keep", "terminal_state": "drop",
                 "source_phrase_id": self.pid, "answer_notes": self.notes,
                 "role": "lead", "gear": 2, "source": "catalogue"}
        applied = S._apply_outcome_rating_ops(entry)
        self.assertEqual(len(applied), 1)
        self.assertFalse(any(g.get("phrase_id") == self.pid
                             for g in self.C.get_gold_phrases()))
        self.assertFalse(any(r.get("phrase_id") == self.pid
                             for r in TB.rejected_phrases()))
        pool = [{"id": self.pid,
                 "vec": self._phrase_vector(self.C._note_dicts(self.notes))}]
        _surv, suppressed = TB.reject_cull(pool)
        self.assertEqual(suppressed, [])
        import json
        from pathlib import Path
        from mcp_server import core_shared
        with open(Path(core_shared._ratings_path())) as handle:
            ratings = json.load(handle)["ratings"]
        ungolds = [r for r in ratings
                   if r.get("idempotency_key") == "outcome:e-1"]
        self.assertEqual(len(ungolds), 1)
        self.assertEqual(str(ungolds[0].get("label")).lower(), "ungold")
        self.assertEqual(ungolds[0].get("entry_id"), "e-1")


class TestItem3SeedFingerprintStampAndSkip(unittest.TestCase):
    """Session memory is seed-to-seed: the JAM write stamps the seed
    fingerprint; the skip compares incoming seed against it. Legacy rows
    without a stamp are ignored."""

    SEED = [
        {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90},
        {"pitch": 62, "start": 0.5, "duration": 0.5, "velocity": 90},
        {"pitch": 64, "start": 1.0, "duration": 0.5, "velocity": 90},
        {"pitch": 67, "start": 1.5, "duration": 0.5, "velocity": 90},
        {"pitch": 69, "start": 2.0, "duration": 0.5, "velocity": 90},
        {"pitch": 72, "start": 2.5, "duration": 1.0, "velocity": 90},
    ]
    OTHER = [
        {"pitch": 40, "start": 0.0, "duration": 0.75, "velocity": 90},
        {"pitch": 45, "start": 0.75, "duration": 0.75, "velocity": 90},
        {"pitch": 52, "start": 1.5, "duration": 0.75, "velocity": 90},
        {"pitch": 57, "start": 2.25, "duration": 0.75, "velocity": 90},
        {"pitch": 64, "start": 3.0, "duration": 0.75, "velocity": 90},
        {"pitch": 69, "start": 3.75, "duration": 0.75, "velocity": 90},
    ]

    def setUp(self):
        import tempfile
        from unittest import mock
        from mcp_server import ledger
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        import os
        patcher = mock.patch.object(
            ledger, "LEDGER_PATH", os.path.join(self._td.name, "ledger.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ledger = ledger

    def test_record_write_stamps_seed(self):
        entry = self.ledger.record_write(
            clip_name="c", slot="3/0", source="catalogue",
            source_phrase_id="synth|c0|w0", answer_notes=list(self.SEED),
            seed_notes=list(self.SEED))
        self.assertEqual(entry["seed_fingerprint"],
                         self.ledger.seed_fingerprint(self.SEED))
        self.assertNotIn("seed_notes", entry)
        self.assertEqual(len(entry["seed_vector"]), 44)

    def test_skip_hits_stamped_seed_ignores_legacy_row(self):
        from mcp_server import core as C
        from build_index import phrase_vector
        self.ledger.record_write(
            clip_name="c", slot="3/0", source="catalogue",
            source_phrase_id="synth|c0|w0", answer_notes=list(self.SEED),
            seed_notes=list(self.SEED))
        # legacy row: served but never stamped with a seed
        self.ledger.record_write(
            clip_name="d", slot="3/1", source="catalogue",
            source_phrase_id="synth|c1|w0", answer_notes=list(self.SEED))
        skip = C._served_skip_ids(phrase_vector(self.SEED), list(self.SEED))
        self.assertIn("synth|c0|w0", skip)
        self.assertNotIn("synth|c1|w0", skip)

    def test_different_seed_no_skip(self):
        from mcp_server import core as C
        from build_index import cosine, phrase_vector
        self.assertLess(cosine(phrase_vector(self.SEED),
                               phrase_vector(self.OTHER)), 0.9)
        self.ledger.record_write(
            clip_name="c", slot="3/0", source="catalogue",
            source_phrase_id="synth|c0|w0", answer_notes=list(self.SEED),
            seed_notes=list(self.SEED))
        skip = C._served_skip_ids(phrase_vector(self.OTHER), list(self.OTHER))
        self.assertEqual(skip, set())


@requires_catalog
class TestItem4DemoteLatestFold(unittest.TestCase):
    """served-not-kept is a latest-record state: a later gold/keep clears it,
    and the neutral demote never shadows an earlier DROP in the reject fold."""

    NOTES_A = [
        {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90},
        {"pitch": 64, "start": 0.5, "duration": 0.5, "velocity": 85},
        {"pitch": 67, "start": 1.0, "duration": 1.0, "velocity": 90},
    ]
    NOTES_B = [
        {"pitch": 48, "start": 0.0, "duration": 1.0, "velocity": 90},
        {"pitch": 52, "start": 1.0, "duration": 1.0, "velocity": 85},
        {"pitch": 55, "start": 2.0, "duration": 1.0, "velocity": 90},
    ]

    def setUp(self):
        import tempfile
        from pathlib import Path
        from mcp_server import core_shared
        from mcp_server import core as C
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        prev = (core_shared._ratings_path()
                if core_shared._RATINGS_PATH_OVERRIDE else None)
        core_shared.set_ratings_path(Path(self._td.name) / "ratings.json")
        self.addCleanup(core_shared.set_ratings_path, prev)
        self.C = C

    def test_later_gold_clears_demote(self):
        from mcp_server import taste_bridge as TB
        self.C.rate(rating=0, label=TB.SERVED_NOT_KEPT_LABEL,
                    phrase_id="synth|ca|w0", notes=self.NOTES_A)
        self.assertIn("synth|ca|w0", TB.served_not_kept_identities())
        self.C.rate(rating=1, label="gold",
                    phrase_id="synth|ca|w0", notes=self.NOTES_A)
        self.assertNotIn("synth|ca|w0", TB.served_not_kept_identities())

    def test_demote_never_shadows_earlier_drop(self):
        from mcp_server import taste_bridge as TB
        self.C.rate(rating=-1, label="dropped",
                    phrase_id="synth|cb|w0", notes=self.NOTES_B)
        self.assertTrue(any(r.get("phrase_id") == "synth|cb|w0"
                            for r in TB.rejected_phrases()))
        self.C.rate(rating=0, label=TB.SERVED_NOT_KEPT_LABEL,
                    phrase_id="synth|cb|w0", notes=self.NOTES_B)
        self.assertTrue(any(r.get("phrase_id") == "synth|cb|w0"
                            for r in TB.rejected_phrases()),
                        "a neutral demote must not lift an earlier DROP")


class TestItem5ReasonWordMatching(unittest.TestCase):
    def test_plain_words_map(self):
        from mcp_server import ears
        self.assertEqual(ears.reason_bucket_dim("too static"), "contour")
        self.assertEqual(ears.reason_bucket_dim("too jumpy"), "contour")
        self.assertEqual(ears.reason_bucket_dim("wrong register"), "register")

    def test_substring_inside_word_does_not_match(self):
        from mcp_server import ears
        self.assertIsNone(ears.reason_bucket_dim("ecstatic grooves"))
        self.assertIsNone(ears.reason_bucket_dim("the registerX preset"))

    def test_negated_forms_do_not_vote(self):
        from mcp_server import ears
        self.assertIsNone(ears.reason_bucket_dim("not static"))
        self.assertIsNone(ears.reason_bucket_dim("isn't jumpy"))
        self.assertIsNone(ears.reason_bucket_dim("not wrong register"))
        self.assertIsNone(ears.reason_bucket_dim(None))
        self.assertIsNone(ears.reason_bucket_dim(""))



def _isolate_ledger(test):
    """_demote_session_buckets only applies when ears.json names the CURRENT live
    ledger session; without this the real store/session_ledger.json decides (a
    live composing session on his machine switched the demotion off)."""
    import importlib
    import os
    from unittest import mock
    path = os.path.join(test._td.name, "session_ledger.json")
    for name in ("ledger", "mcp_server.ledger"):
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        patcher = mock.patch.object(module, "LEDGER_PATH", path)
        patcher.start()
        test.addCleanup(patcher.stop)


class TestItem5EarsOffDemotion(unittest.TestCase):
    FLAT = [
        {"pitch": 72, "start": 0.0, "duration": 0.5, "velocity": 90},
        {"pitch": 72, "start": 1.0, "duration": 0.5, "velocity": 85},
        {"pitch": 72, "start": 2.0, "duration": 1.0, "velocity": 90},
        {"pitch": 72, "start": 3.0, "duration": 1.0, "velocity": 88},
    ]
    RISING = [
        {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90},
        {"pitch": 64, "start": 0.5, "duration": 0.5, "velocity": 85},
        {"pitch": 67, "start": 1.0, "duration": 0.5, "velocity": 90},
        {"pitch": 72, "start": 1.5, "duration": 0.5, "velocity": 88},
        {"pitch": 76, "start": 2.0, "duration": 1.0, "velocity": 90},
    ]

    def setUp(self):
        import json
        import tempfile
        import os
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        _isolate_ledger(self)
        self.session_path = os.path.join(self._td.name, "ears.json")
        with open(self.session_path, "w") as handle:
            json.dump({"streaks": {"contour:flat": 3},
                       "demoted": ["contour:flat"],
                       "reason_demoted": ["contour:flat"]}, handle)

    def _ranked(self):
        return [{"id": "flat-one", "notes": [dict(n) for n in self.FLAT]},
                {"id": "rise-one", "notes": [dict(n) for n in self.RISING]}]

    def test_demoted_bucket_slides_back_never_banned(self):
        from unittest import mock
        from mcp_server import ears
        from mcp_server import core as C
        with mock.patch.object(ears, "SESSION_PATH", self.session_path):
            out = C._demote_session_buckets(
                self._ranked(), lambda h, *_a: h["notes"], 0, 72, 70)
        self.assertEqual([h["id"] for h in out], ["rise-one", "flat-one"])

    def test_no_demotions_keeps_order(self):
        import json
        from unittest import mock
        from mcp_server import ears
        from mcp_server import core as C
        with open(self.session_path, "w") as handle:
            json.dump({"streaks": {}, "demoted": []}, handle)
        with mock.patch.object(ears, "SESSION_PATH", self.session_path):
            out = C._demote_session_buckets(
                self._ranked(), lambda h, *_a: h["notes"], 0, 72, 70)
        self.assertEqual([h["id"] for h in out], ["flat-one", "rise-one"])

    def test_corrupt_session_serves_untouched(self):
        from unittest import mock
        from mcp_server import ears
        from mcp_server import core as C
        with open(self.session_path, "w") as handle:
            handle.write("{corrupt")
        with mock.patch.object(ears, "SESSION_PATH", self.session_path):
            out = C._demote_session_buckets(
                self._ranked(), lambda h, *_a: h["notes"], 0, 72, 70)
        self.assertEqual([h["id"] for h in out], ["flat-one", "rise-one"])


@requires_catalog
class TestItem5EarsOffAnswerPath(unittest.TestCase):
    """End to end: with ears off, a reason-demoted bucket still slides to
    the back of the answer bank instead of taking the pick slot."""

    FLAT = [60, 67, 64, 72, 65, 60]
    RISE = [60, 62, 64, 65, 67, 69]

    def _notes(self, pitches):
        return [{"pitch": p, "start": 0.5 * i, "duration": 0.5,
                 "velocity": 90} for i, p in enumerate(pitches)]

    def _rec(self, pid, pitches):
        from build_index import phrase_vector
        nd = self._notes(pitches)
        return {"id": pid, "project": "synth", "track": "t",
                "role": "lead", "clip_source": "clip",
                "kind": "harmonic", "tempo": 120.0,
                "n_notes": len(nd), "notes": nd, "vec": phrase_vector(nd)}

    def setUp(self):
        import json
        import tempfile
        import os
        from unittest import mock
        from mcp_server import core_shared
        from mcp_server import core as C
        from mcp_server import taste_bridge as TB
        from mcp_server import ears
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        prev = (core_shared._ratings_path()
                if core_shared._RATINGS_PATH_OVERRIDE else None)
        core_shared.set_ratings_path(
            os.path.join(self._td.name, "ratings.json"))
        self.addCleanup(core_shared.set_ratings_path, prev)
        self._prev_pool = TB._PHRASE_POOL_OVERRIDE
        self.addCleanup(setattr, TB, "_PHRASE_POOL_OVERRIDE", self._prev_pool)
        TB._PHRASE_POOL_OVERRIDE = [
            self._rec("synth|flat|w0", self.FLAT),
            self._rec("synth|rise|w0", self.RISE)]
        self.session_path = os.path.join(self._td.name, "ears.json")
        with open(self.session_path, "w") as handle:
            json.dump({"streaks": {}, "demoted": []}, handle)
        patcher = mock.patch.object(ears, "SESSION_PATH", self.session_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        _isolate_ledger(self)
        self.C = C
        # near the flat phrase (0.961: survives the diversify gate) but
        # nearer to it than to the rise, so rank-1 is the flat phrase
        self.seed = self._notes([60, 67, 64, 72, 65, 62])

    def test_ears_off_demoted_bucket_loses_pick_slot(self):
        import json
        control = self.C.answer_my_loop(
            self.seed, role="lead", seed=0, use_ears=False, humanize=False)
        self.assertEqual(control["source_phrase_ids"], ["synth|flat|w0"])
        # demote the winner's (transpose-stable) contour bucket
        with open(self.session_path, "w") as handle:
            json.dump({"streaks": {"contour:flat": 3},
                       "demoted": ["contour:flat"],
                       "reason_demoted": ["contour:flat"]}, handle)
        moved = self.C.answer_my_loop(
            self.seed, role="lead", seed=0, use_ears=False, humanize=False)
        self.assertEqual(moved["source_phrase_ids"], ["synth|rise|w0"])
        self.assertTrue(any("session memory" in a
                            for a in moved["assumptions"]),
                        moved["assumptions"])


@requires_catalog
class TestItem6MovedDropKeepsGold(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from mcp_server import core_shared
        from mcp_server import core as C
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        prev = (core_shared._ratings_path()
                if core_shared._RATINGS_PATH_OVERRIDE else None)
        core_shared.set_ratings_path(Path(self._td.name) / "ratings.json")
        self.addCleanup(core_shared.set_ratings_path, prev)
        self.C = C
        self.notes = [
            {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90},
            {"pitch": 64, "start": 0.5, "duration": 0.5, "velocity": 85},
            {"pitch": 67, "start": 1.0, "duration": 1.0, "velocity": 90},
        ]

    def test_moved_drop_does_not_ungold_source(self):
        from mcp_server import taste_bridge as TB
        self.C.rate(rating=1, label="gold", phrase_id="synth|m6|w0",
                    notes=self.notes)
        self.assertTrue(any(g.get("phrase_id") == "synth|m6|w0"
                            for g in TB.get_gold_phrases()))
        self.C.rate(rating=-1, label="dropped", phrase_id="synth|m6|w0",
                    notes=self.notes, move="surgery:thin")
        self.assertTrue(any(g.get("phrase_id") == "synth|m6|w0"
                            for g in TB.get_gold_phrases()),
                        "a moved DROP bans the pairing, not the source's gold")

    def test_plain_drop_still_ungolds(self):
        from mcp_server import taste_bridge as TB
        self.C.rate(rating=1, label="gold", phrase_id="synth|m6b|w0",
                    notes=self.notes)
        self.C.rate(rating=-1, label="dropped", phrase_id="synth|m6b|w0",
                    notes=self.notes)
        self.assertFalse(any(g.get("phrase_id") == "synth|m6b|w0"
                             for g in TB.get_gold_phrases()))


@requires_catalog
class TestItem7GlobalBanSticks(unittest.TestCase):
    NOTES = [
        {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90},
        {"pitch": 64, "start": 0.5, "duration": 0.5, "velocity": 85},
        {"pitch": 67, "start": 1.0, "duration": 1.0, "velocity": 90},
        {"pitch": 72, "start": 2.0, "duration": 1.0, "velocity": 88},
    ]

    def setUp(self):
        import tempfile
        from pathlib import Path
        from mcp_server import core_shared
        from mcp_server import core as C
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        prev = (core_shared._ratings_path()
                if core_shared._RATINGS_PATH_OVERRIDE else None)
        core_shared.set_ratings_path(Path(self._td.name) / "ratings.json")
        self.addCleanup(core_shared.set_ratings_path, prev)
        self.C = C

    def _vec(self):
        from build_index import phrase_vector
        return phrase_vector(self.C._note_dicts(self.NOTES))

    def test_global_ban_survives_later_moved_drop(self):
        from mcp_server import taste_bridge as TB
        self.C.rate(rating=-1, label="dropped", phrase_id="synth|m7|w0",
                    notes=self.NOTES)
        self.C.rate(rating=-1, label="dropped", phrase_id="synth|m7|w0",
                    notes=self.NOTES, move="surgery:thin")
        recs = [r for r in TB.rejected_phrases()
                if r.get("phrase_id") == "synth|m7|w0"]
        # one entry per banned pairing (48h review P2): the global ban and
        # the moved one are independent — neither narrows the other
        self.assertEqual(sorted((r.get("move") or "") for r in recs),
                         ["", "surgery:thin"])
        pool = [{"id": "synth|m7|w0", "vec": self._vec()}]
        _surv, suppressed = TB.reject_cull(pool)
        self.assertEqual([s["id"] for s in suppressed], ["synth|m7|w0"])
        _surv, suppressed = TB.reject_cull(pool, move="other:move")
        self.assertEqual([s["id"] for s in suppressed], ["synth|m7|w0"])

    def test_moved_reject_near_dup_keeps_pairing(self):
        from mcp_server import taste_bridge as TB
        self.C.rate(rating=-1, label="dropped", phrase_id="synth|m7b|w0",
                    notes=self.NOTES, move="surgery:thin")
        near = [{"id": "synth|other|w0", "vec": self._vec()}]
        _surv, suppressed = TB.reject_cull(near, move="surgery:thin")
        self.assertEqual([s["id"] for s in suppressed], ["synth|other|w0"])
        _surv, suppressed = TB.reject_cull(near, move="other:move")
        self.assertEqual(suppressed, [])
        _surv, suppressed = TB.reject_cull(near)
        self.assertEqual(suppressed, [])

    def test_positive_clears_sticky_global_ban_before_moved_drop(self):
        from mcp_server import taste_bridge as TB
        self.C.rate(rating=-1, label="dropped", phrase_id="synth|m7c|w0",
                    notes=self.NOTES)
        self.C.rate(rating=1, label="gold", phrase_id="synth|m7c|w0",
                    notes=self.NOTES)
        self.C.rate(rating=-1, label="dropped", phrase_id="synth|m7c|w0",
                    notes=self.NOTES, move="surgery:thin")
        rec = next(r for r in TB.rejected_phrases()
                   if r.get("phrase_id") == "synth|m7c|w0")
        self.assertEqual(rec.get("move"), "surgery:thin")
        pool = [{"id": "synth|m7c|w0", "vec": self._vec()}]
        _surv, suppressed = TB.reject_cull(pool)
        self.assertEqual(suppressed, [])

    def test_like_clears_sticky_global_ban_before_moved_drop(self):
        from mcp_server import taste_bridge as TB
        self.C.rate(rating=-1, label="dropped", phrase_id="synth|m7d|w0",
                    notes=self.NOTES)
        self.C.rate(rating=1, label="like", phrase_id="synth|m7d|w0",
                    notes=self.NOTES)
        self.C.rate(rating=-1, label="dropped", phrase_id="synth|m7d|w0",
                    notes=self.NOTES, move="surgery:thin")
        rec = next(r for r in TB.rejected_phrases()
                   if r.get("phrase_id") == "synth|m7d|w0")
        self.assertEqual(rec.get("move"), "surgery:thin")


class TestItem8ComposedStratum(unittest.TestCase):
    def _entry(self, eid, source, verdict="keep", state="keep"):
        return {"id": eid, "session_id": "s-1", "source": source,
                "role": "lead", "gear": 2,
                "immediate_verdict": verdict, "terminal_state": state,
                "set_identity": {"set_id": "set-1"}}

    def test_source_stratum_mapping(self):
        from mcp_server import ledger
        self.assertEqual(ledger._source_stratum("gold_composed"), "composed")
        self.assertEqual(ledger._source_stratum("catalogue"), "catalogue")
        self.assertEqual(ledger._source_stratum("ref:abc"), "ref")
        self.assertEqual(ledger._source_stratum(None), "catalogue")

    def test_session_report_stratifies_composed(self):
        from mcp_server import ledger
        data = {"entries": [
            self._entry("e-1", "gold_composed"),
            self._entry("e-2", "catalogue"),
            self._entry("e-3", "ref:xyz"),
        ]}
        rep = ledger._session_report(data, {"id": "s-1"})
        self.assertIn("composed|lead|gear2", rep["strata"])
        self.assertEqual(rep["strata"]["composed|lead|gear2"]["keep"], 1)
        self.assertIn("catalogue|lead|gear2", rep["strata"])
        self.assertIn("ref|lead|gear2", rep["strata"])
        self.assertNotIn("gold_composed", str(rep["strata"]))

    def test_attested_composed_row_stratified(self):
        from mcp_server import ledger
        entry = self._entry("e-9", "gold_composed")
        entry["attestation"] = {"fact": "present", "note": "owner word",
                                "attested_at": "2026-01-01T00:00:00Z"}
        data = {"entries": [entry]}
        rep = ledger._session_report(data, {"id": "s-1"})
        self.assertIn("composed|lead|gear2", rep["attested"]["strata"])


class TestItem9BriefParsing(unittest.TestCase):
    def test_abbreviated_keys(self):
        from mcp_server import core as C
        self.assertEqual(C._parse_brief_hints("a loop in Dm")["key"],
                         "D minor")
        self.assertEqual(C._parse_brief_hints("F# maj idea")["key"],
                         "F# major")
        self.assertEqual(C._parse_brief_hints("a lead in A minor")["key"],
                         "A minor")

    def test_bare_mode_hints_nothing(self):
        from mcp_server import core as C
        self.assertIsNone(C._parse_brief_hints("a sad minor loop")["key"])
        # Fix2 accepts lowercase a as a tonic when directly followed by a
        # full mode word; this used to be treated as an article.
        self.assertEqual(C._parse_brief_hints("a major statement")["key"],
                         "A major")
        self.assertIsNone(C._parse_brief_hints("surprise me")["key"])

    def test_drum_words_win_over_bass(self):
        from mcp_server import core as C
        self.assertEqual(C._parse_brief_hints("bass drum groove")["role"],
                         "drum")
        self.assertEqual(
            C._parse_brief_hints("drums with a bassline")["role"], "drum")
        self.assertEqual(C._parse_brief_hints("a dark bass line")["role"],
                         "bass")


@requires_catalog
class TestItem9BriefSeedWindow(unittest.TestCase):
    def setUp(self):
        from mcp_server import taste_bridge as TB
        from build_index import phrase_vector
        self._prev_pool = TB._PHRASE_POOL_OVERRIDE
        self.addCleanup(setattr, TB, "_PHRASE_POOL_OVERRIDE", self._prev_pool)
        pool = []
        for i in range(8):
            notes = [{"pitch": 60 + ((i * 3 + j * 2) % 12),
                      "start": 0.5 * j, "duration": 0.5, "velocity": 90}
                     for j in range(6)]
            pool.append({"id": "lead%d" % i, "project": "synth", "track": "t",
                         "role": "lead", "clip_source": "clip",
                         "kind": "harmonic", "tempo": 120.0,
                         "n_notes": len(notes), "notes": notes,
                         "vec": phrase_vector(notes)})
        TB._PHRASE_POOL_OVERRIDE = pool
        self.TB = TB

    def test_seed_rotates_selection_not_just_order(self):
        from mcp_server import core as C
        first = C.compose_brief("surprise me", seed=0)["example_phrases"]
        third = C.compose_brief("surprise me", seed=2)["example_phrases"]
        self.assertEqual(len(first), 4)
        self.assertEqual(len(third), 4)
        self.assertNotEqual({p["id"] for p in first},
                            {p["id"] for p in third})

    def test_seed_window_deterministic(self):
        from mcp_server import core as C
        again = C.compose_brief("surprise me", seed=2)["example_phrases"]
        third = C.compose_brief("surprise me", seed=2)["example_phrases"]
        self.assertEqual([p["id"] for p in again],
                         [p["id"] for p in third])

    def test_small_pool_falls_back_to_rotation(self):
        from mcp_server import core as C
        self.assertEqual(
            [p["id"] for p in C._seed_window(
                [{"id": "a"}, {"id": "b"}], 1, 4)], ["b", "a"])


@requires_catalog
class TestItem10PickDepthFixture(unittest.TestCase):
    VARIANTS = ([0, 2, 4, 5, 7, 9], [0, 3, 5, 7, 10, 12],
                [2, 5, 7, 9, 12, 14], [0, 2, 3, 7, 8, 12],
                [5, 7, 9, 12, 14, 16], [1, 4, 6, 8, 11, 13],
                [3, 5, 8, 10, 12, 15], [0, 4, 7, 11, 14, 17],
                [2, 4, 9, 12, 16, 19], [1, 5, 9, 12, 14, 18])

    def setUp(self):
        import os
        import tempfile
        from mcp_server import core_shared
        from mcp_server import core as C
        from mcp_server import taste_bridge as TB
        from build_index import phrase_vector
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        prev = (core_shared._ratings_path()
                if core_shared._RATINGS_PATH_OVERRIDE else None)
        core_shared.set_ratings_path(
            os.path.join(self._td.name, "ratings.json"))
        self.addCleanup(core_shared.set_ratings_path, prev)
        self._prev_pool = TB._PHRASE_POOL_OVERRIDE
        self.addCleanup(setattr, TB, "_PHRASE_POOL_OVERRIDE", self._prev_pool)
        pool = []
        for i, shifts in enumerate(self.VARIANTS):
            notes = [{"pitch": 60 + s, "start": 0.5 * j, "duration": 0.5,
                      "velocity": 90} for j, s in enumerate(shifts)]
            pool.append({"id": "synth|c%d|w0" % i, "project": "synth",
                         "track": "t", "role": "lead", "clip_source": "clip",
                         "kind": "harmonic", "tempo": 120.0,
                         "n_notes": len(notes), "notes": notes,
                         "vec": phrase_vector(notes)})
        TB._PHRASE_POOL_OVERRIDE = pool
        self.C = C
        self.seed = [{"pitch": 60 + s, "start": 0.5 * j, "duration": 0.5,
                      "velocity": 90} for j, s in enumerate([0, 2, 4, 7, 9, 12])]

    def _picks(self, n, **kw):
        return [self.C.answer_my_loop(self.seed, role="lead", seed=s,
                                      use_ears=False, humanize=False,
                                      **kw)["source_phrase_ids"][0]
                for s in range(n)]

    def test_no_ref_wraps_at_six(self):
        ids = self._picks(7)
        self.assertEqual(len(set(ids[:6])), 6)
        self.assertEqual(ids[6], ids[0])

    def test_ref_wraps_at_eight(self):
        ref = {"ref_id": "r1", "tempo": 120.0, "register": None,
               "mods": None, "density": None, "unmapped": []}
        ids = self._picks(9, ref=ref)
        self.assertEqual(len(set(ids[:8])), 8)
        self.assertEqual(ids[8], ids[0])


@requires_catalog
class TestItem11CloseOutcomes(unittest.TestCase):
    NOTES = [
        {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90},
        {"pitch": 64, "start": 0.5, "duration": 0.5, "velocity": 85},
        {"pitch": 67, "start": 1.0, "duration": 1.0, "velocity": 90},
        {"pitch": 72, "start": 2.0, "duration": 1.0, "velocity": 88},
    ]
    IDENTITY = {"set_id": "set-1", "runtime_epoch": "run-1"}

    def setUp(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        from mcp_server import core_shared
        from mcp_server import core as C
        from mcp_server import ledger
        from mcp_server import server as S
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ledger_path = str(Path(self._td.name) / "ledger.json")
        self.ears_path = str(Path(self._td.name) / "ears.json")
        self._patches = [
            mock.patch.object(ledger, "LEDGER_PATH", self.ledger_path),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        prev = (core_shared._ratings_path()
                if core_shared._RATINGS_PATH_OVERRIDE else None)
        core_shared.set_ratings_path(
            str(Path(self._td.name) / "ratings.json"))
        self.addCleanup(core_shared.set_ratings_path, prev)
        self.C = C
        self.ledger = ledger
        self.server = S
        with open(self.ears_path, "w") as handle:
            json.dump({"streaks": {"density:busy": 2},
                       "demoted": ["density:busy"]}, handle)

    def _write(self, clip, verdict=None, source="catalogue", pid="synth|c11|w0"):
        entry = self.ledger.record_write(
            clip_name=clip, slot="3/0", source=source,
            source_phrase_id=pid, answer_notes=list(self.NOTES),
            set_identity=dict(self.IDENTITY),
            clip_ref={"clip_id": clip, "track_id": "jam",
                      "track": "SIDEKICK JAM", "surface": "session",
                      "path": "t3/s0", "answer_notes": list(self.NOTES)},
            path=self.ledger_path)
        if verdict is not None:
            self.ledger.record_verdict(verdict, entry_id=entry["id"],
                                       path=self.ledger_path)
        return entry

    def _close(self, clips, session_id=None):
        from unittest import mock
        from mcp_server import live_write
        inventory = {"complete": True, "set_identity": dict(self.IDENTITY),
                     "clips": clips, "errors": []}
        with mock.patch.object(self.server, "_run_async",
                               side_effect=lambda v: v), \
                mock.patch.object(live_write, "read_set_inventory",
                                  new=lambda: inventory):
            return self.server.close_session(session_id=session_id)

    def _end_night(self):
        import json
        from mcp_server import ledger
        with open(self.ledger_path) as handle:
            data = json.load(handle)
        now = ledger._now()
        for s in data["sessions"]:
            s["ended_at"] = ledger._iso(now)
            s["last_activity"] = now - 10
        with open(self.ledger_path, "w") as handle:
            json.dump(data, handle)

    def test_failed_outcome_op_without_pending_marker_is_not_retried(self):
        import json
        from mcp_server import taste_bridge as TB
        self.C.rate(rating=1, label="gold", phrase_id="synth|c11|w0",
                    notes=self.NOTES)
        # A pre-branch terminal row has no retry marker and must not be
        # retroactively rated by a later close.
        entry = self._write("gone", verdict="keep")
        with open(self.ledger_path) as handle:
            data = json.load(handle)
        for e in data["entries"]:
            if e["id"] == entry["id"]:
                e["terminal_state"] = "drop"
        with open(self.ledger_path, "w") as handle:
            json.dump(data, handle)
        self._end_night()
        out = self._close([])
        self.assertTrue(out.get("ok"), out)
        applied_ids = [r["entry_id"]
                       for r in out.get("outcome_ratings", [])]
        self.assertNotIn(entry["id"], applied_ids)
        self.assertTrue(any(g.get("phrase_id") == "synth|c11|w0"
                            for g in TB.get_gold_phrases()))

    def test_applied_outcome_op_not_reapplied(self):
        from mcp_server import server as S
        entry = self._write("gone", verdict="keep")
        S._apply_outcome_rating_ops(
            {**entry, "terminal_state": "drop",
             "immediate_verdict": "keep"})
        self.assertTrue(S._outcome_op_applied(entry["id"]))
        self._end_night()
        out = self._close([])
        self.assertTrue(out.get("ok"), out)
        applied_ids = [r["entry_id"]
                       for r in out.get("outcome_ratings", [])]
        self.assertNotIn(entry["id"], applied_ids)

    def test_sealed_night_later_gold_survives_reclose(self):
        from unittest import mock
        from mcp_server import taste_bridge as TB
        self.C.rate(rating=1, label="gold", phrase_id="synth|c11|w0",
                    notes=self.NOTES)
        entry = self._write("gone", verdict="keep")
        self._end_night()
        with mock.patch.object(self.server, "_apply_outcome_rating_ops",
                               return_value=[{"error": "sink down"}]):
            first = self._close([])
        self.assertTrue(first.get("ok"), first)
        self.assertTrue(self.ledger.load(self.ledger_path)["entries"][0].get(
            "outcome_op_pending"))
        self.C.rate(rating=1, label="gold", phrase_id="synth|c11|w0",
                    notes=self.NOTES)
        second = self._close([], session_id=entry["session_id"])
        self.assertTrue(second.get("ok"), second)
        self.assertTrue(any(g.get("phrase_id") == "synth|c11|w0"
                            for g in TB.get_gold_phrases()))
        self.assertNotIn(entry["id"], [r["entry_id"]
                                       for r in second.get("outcome_ratings", [])])
        self.assertNotIn("outcome_op_pending",
                         self.ledger.load(self.ledger_path)["entries"][0])

    def test_pending_marker_retries_failed_outcome_op(self):
        import json
        from mcp_server import taste_bridge as TB
        self.C.rate(rating=1, label="gold", phrase_id="synth|c11|w0",
                    notes=self.NOTES)
        entry = self._write("gone", verdict="keep")
        with open(self.ledger_path) as handle:
            data = json.load(handle)
        for e in data["entries"]:
            if e["id"] == entry["id"]:
                e["terminal_state"] = "drop"
                e["outcome_op_pending"] = True
        with open(self.ledger_path, "w") as handle:
            json.dump(data, handle)
        self._end_night()
        out = self._close([])
        self.assertTrue(out.get("ok"), out)
        self.assertIn(entry["id"], [r["entry_id"]
                                    for r in out.get("outcome_ratings", [])])
        self.assertFalse(any(g.get("phrase_id") == "synth|c11|w0"
                             for g in TB.get_gold_phrases()))
        saved = self.ledger.load(self.ledger_path)
        self.assertNotIn("outcome_op_pending", saved["entries"][0])

    def test_attested_row_without_confirm_time_op_is_not_reclosed(self):
        import json
        from mcp_server import taste_bridge as TB
        self.C.rate(rating=1, label="gold", phrase_id="synth|c11|w0",
                    notes=self.NOTES)
        entry = self._write("ghost", verdict="keep")
        with open(self.ledger_path) as handle:
            data = json.load(handle)
        for e in data["entries"]:
            if e["id"] == entry["id"]:
                e["attestation"] = {
                    "fact": "absent", "note": "owner word",
                    "attested_at": "2026-01-01T00:00:00Z"}
        with open(self.ledger_path, "w") as handle:
            json.dump(data, handle)
        self._end_night()
        out = self._close([])
        self.assertTrue(out.get("ok"), out)
        applied_ids = [r["entry_id"]
                       for r in out.get("outcome_ratings", [])]
        self.assertNotIn(entry["id"], applied_ids)
        self.assertTrue(any(g.get("phrase_id") == "synth|c11|w0"
                            for g in TB.get_gold_phrases()))

    def test_confirmed_attestation_applies_outcome_immediately(self):
        from mcp_server import taste_bridge as TB
        entry = self.ledger.record_write(
            clip_name="legacy", slot=None, source="catalogue",
            source_phrase_id="synth|c11|w0", answer_notes=self.NOTES,
            path=self.ledger_path)
        self.ledger.record_verdict("keep", entry_id=entry["id"],
                                   path=self.ledger_path)
        self.C.rate(rating=1, label="gold", phrase_id="synth|c11|w0",
                    notes=self.NOTES)
        out = self.server.close_session(
            session_id=entry["session_id"],
            attest={entry["id"]: "absent"}, confirm=True)
        self.assertTrue(out.get("ok"), out)
        self.assertIn(entry["id"], [r["entry_id"]
                                    for r in out.get("outcome_ratings", [])])
        self.assertFalse(any(g.get("phrase_id") == "synth|c11|w0"
                             for g in TB.get_gold_phrases()))

    def test_reset_skipped_while_another_session_live(self):
        from unittest import mock
        from mcp_server import ears
        from mcp_server import ledger
        self._write("gone", verdict="drop")
        self._end_night()
        # a second night is still live (open, recent, nothing to sweep)
        ledger.open_session(path=self.ledger_path)
        out = self._close([])
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(out.get("ears_reset_skipped"))
        with mock.patch.object(ears, "SESSION_PATH", self.ears_path):
            state = ears.load_session(self.ears_path)
        self.assertEqual(state["demoted"], ["density:busy"])


class TestItem12JunkFloorFlag(unittest.TestCase):
    """The project-key tonic + period rule ride behind JUNK_FLOOR_V2
    (default OFF): with the flag off the bank gates exactly like base."""

    GRAMMAR = {"unigram": {"0": 0.05, "7": 0.05, "2": 0.45, "4": 0.45}}

    def _trill(self, project="synth"):
        notes = [{"pitch": p, "start": 0.5 * i, "duration": 0.5,
                  "velocity": 90}
                 for i, p in enumerate([60, 67] * 5)]
        return {"id": "synth|trill|w0", "project": project, "track": "t",
                "role": "lead", "clip_source": "clip", "kind": "harmonic",
                "tempo": 120.0, "n_notes": len(notes), "notes": notes}

    def setUp(self):
        from mcp_server import core_shared
        self._saved_keys = dict(core_shared._CACHE)
        core_shared._CACHE["project_keys"] = {"synth": "C major"}
        self.addCleanup(self._restore_cache)

    def _restore_cache(self):
        from mcp_server import core_shared
        core_shared._CACHE.pop("project_keys", None)
        for k, v in self._saved_keys.items():
            core_shared._CACHE[k] = v

    def test_flag_off_matches_base(self):
        from unittest import mock
        from mcp_server import core as C
        from boringness import is_mechanical, phrase_to_degrees
        rec = self._trill()
        nd = C._note_dicts(rec["notes"])
        degs = phrase_to_degrees(
            [{"pitch": n["pitch"], "start": n["start"]} for n in nd],
            C._phrase_tonic_pc(nd))
        base_gated = len(degs) >= 2 and is_mechanical(
            degs, self.GRAMMAR, period_rule=False)
        self.assertFalse(base_gated, "fixture must be base-clean")
        self.assertFalse(C.JUNK_FLOOR_V2, "flag ships OFF")
        self.assertEqual(C._junk_gate(rec, nd, self.GRAMMAR), base_gated)
        with mock.patch.object(C, "JUNK_FLOOR_V2", True):
            self.assertTrue(C._junk_gate(rec, nd, self.GRAMMAR))

    def test_report_lists_flips_for_ear_check(self):
        from mcp_server import core as C
        rep = C.junk_floor_v2_report(pool=[self._trill()],
                                     grammar=self.GRAMMAR)
        self.assertIn("synth|trill|w0", rep["newly_gated"])
        self.assertEqual(rep["newly_ungated"], [])
        self.assertIn("synth|trill|w0", rep["periodic"])
        self.assertFalse(rep["flag"])
