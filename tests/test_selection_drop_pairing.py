"""B3 item 5: DROP bans what was rejected.

A DROP of a moved answer stores the reject identity as (source_id, move)
and culls only that pairing; plain gear-1/2 DROPs keep the global ban.
Reason words ('static'/'jumpy'/'wrong register') demote the matching
ears bucket even with ears off. Synthetic pools/files throughout.
"""
from __future__ import annotations

import json
import unittest
from contextlib import ExitStack
from pathlib import Path
import tempfile
from unittest import mock

from mcp_server import core_shared
from mcp_server import develop_session, ears, gears, ledger
from mcp_server import core as C
from mcp_server import taste_bridge as TB
from mcp_server.core_shared import _canonical_move
from build_index import cosine, phrase_vector
from tests._requires import requires_catalog

SEED = [
    {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90},
    {"pitch": 62, "start": 0.5, "duration": 0.5, "velocity": 85},
    {"pitch": 64, "start": 1.0, "duration": 0.5, "velocity": 90},
    {"pitch": 67, "start": 1.5, "duration": 0.5, "velocity": 88},
    {"pitch": 69, "start": 2.0, "duration": 1.0, "velocity": 90},
    {"pitch": 72, "start": 3.0, "duration": 1.0, "velocity": 85},
]
ALT_A = [
    {"pitch": 48, "start": 0.0, "duration": 1.0, "velocity": 90},
    {"pitch": 55, "start": 1.0, "duration": 1.0, "velocity": 85},
    {"pitch": 52, "start": 2.0, "duration": 1.0, "velocity": 90},
    {"pitch": 50, "start": 3.0, "duration": 1.0, "velocity": 88},
]
ALT_B = [
    {"pitch": 74, "start": 0.0, "duration": 0.5, "velocity": 90},
    {"pitch": 71, "start": 0.5, "duration": 0.5, "velocity": 85},
    {"pitch": 69, "start": 1.0, "duration": 1.0, "velocity": 90},
    {"pitch": 66, "start": 2.0, "duration": 2.0, "velocity": 88},
]

IDENTITY = {"set_id": "drop-pairing", "runtime_epoch": "run-1",
            "file_path": "/tmp/drop-pairing.als"}


def _rec(pid, notes):
    nd = [dict(n) for n in notes]
    return {"id": pid, "project": "synth", "track": "t",
            "role": "lead", "clip_source": "clip",
            "kind": "harmonic", "tempo": 120.0,
            "n_notes": len(nd), "notes": nd, "vec": phrase_vector(nd)}


class CanonicalMoveTests(unittest.TestCase):
    def test_pass_walk_suffixes_stripped(self):
        self.assertEqual(_canonical_move("slow-burn:reveal:pass2"),
                         "slow-burn:reveal")
        self.assertEqual(_canonical_move("motivic:walk:walk3"), "motivic:walk")
        self.assertEqual(_canonical_move("surgery:bass_shadow"),
                         "surgery:bass_shadow")
        self.assertEqual(_canonical_move("fragment"), "fragment")
        self.assertIsNone(_canonical_move(None))


@requires_catalog
class PairingCullTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        prev = (core_shared._ratings_path()
                if core_shared._RATINGS_PATH_OVERRIDE else None)
        core_shared.set_ratings_path(
            Path(self._td.name) / "ratings.json")
        self.addCleanup(core_shared.set_ratings_path, prev)

    def _drop(self, pid, notes, move=None):
        C.rate(-1, label="dropped", phrase_id=pid, notes=notes, move=move)

    def test_moved_drop_culls_only_that_pairing(self):
        src = _rec("src", SEED)
        self._drop("src", SEED, move="motivic:fragment")
        # same pairing culled ...
        kept, _ = TB.reject_cull([src], move="motivic:fragment")
        self.assertEqual(kept, [])
        # ... a different move still serves the source ...
        kept, _ = TB.reject_cull([src], move="slow-burn:reveal")
        self.assertEqual([p["id"] for p in kept], ["src"])
        # ... and so does a plain (unmoved) serving
        kept, _ = TB.reject_cull([src])
        self.assertEqual([p["id"] for p in kept], ["src"])

    def test_plain_drop_keeps_global_ban(self):
        src = _rec("src", SEED)
        self._drop("src", SEED)
        for move in (None, "motivic:fragment", "slow-burn:reveal"):
            kept, suppressed = TB.reject_cull([src], move=move)
            self.assertEqual(kept, [], move)
            self.assertEqual(len(suppressed), 1)

    def test_successive_moved_drops_each_keep_their_pairing(self):
        # 48h review P2: dropping "thin" then "invert" must ban BOTH pairings.
        src = _rec("src", SEED)
        self._drop("src", SEED, move="surgery:thin")
        self._drop("src", SEED, move="surgery:invert")
        for move in ("surgery:thin", "surgery:invert"):
            kept, suppressed = TB.reject_cull([src], move=move)
            self.assertEqual(kept, [], move)
            self.assertEqual(suppressed[0]["move"], move)
        kept, _ = TB.reject_cull([src], move="slow-burn:reveal")
        self.assertEqual([p["id"] for p in kept], ["src"])
        kept, _ = TB.reject_cull([src])
        self.assertEqual([p["id"] for p in kept], ["src"])
        moves = sorted(r["move"] for r in TB.rejected_phrases()
                       if r["identity"] == "src")
        self.assertEqual(moves, ["surgery:invert", "surgery:thin"])

    def test_only_a_same_pairing_positive_lifts_a_moved_ban(self):
        src = _rec("src", SEED)
        self._drop("src", SEED, move="surgery:thin")
        self._drop("src", SEED, move="surgery:invert")
        # a KEEP/gold of the plain source says nothing about the thinned version
        C.rate(1, label="gold", phrase_id="src", notes=SEED)
        kept, _ = TB.reject_cull([src], move="surgery:thin")
        self.assertEqual(kept, [])
        # a positive on the same pairing lifts exactly that pairing
        C.rate(1, label="kept", phrase_id="src", notes=SEED, move="surgery:thin")
        kept, _ = TB.reject_cull([src], move="surgery:thin")
        self.assertEqual([p["id"] for p in kept], ["src"])
        kept, _ = TB.reject_cull([src], move="surgery:invert")
        self.assertEqual(kept, [])

    def test_global_ban_and_moved_drops_are_independent(self):
        src = _rec("src", SEED)
        self._drop("src", SEED)                         # global
        self._drop("src", SEED, move="surgery:thin")   # later moved DROP
        for move in (None, "surgery:thin", "slow-burn:reveal"):
            kept, _ = TB.reject_cull([src], move=move)
            self.assertEqual(kept, [], move)
        # lifting the global ban (plain positive) leaves the moved one standing
        C.rate(1, label="kept", phrase_id="src", notes=SEED)
        kept, _ = TB.reject_cull([src])
        self.assertEqual([p["id"] for p in kept], ["src"])
        kept, _ = TB.reject_cull([src], move="surgery:thin")
        self.assertEqual(kept, [])

    def test_clear_rejects_lifts_every_pairing(self):
        from mcp_server import server
        src = _rec("src", SEED)
        self._drop("src", SEED)
        self._drop("src", SEED, move="surgery:thin")
        self._drop("src", SEED, move="surgery:invert")
        self.assertEqual(len(TB.rejected_phrases()), 3)
        preview = server.clear_rejects(phrase_id="src")
        self.assertEqual(preview["n_rejected"], 1)
        self.assertEqual(preview["would_clear"], ["src"])   # distinct phrases
        self.assertEqual({(p["identity"], p["move"])
                          for p in preview["would_clear_pairings"]},
                         {("src", None), ("src", "surgery:thin"),
                          ("src", "surgery:invert")})
        server.clear_rejects(phrase_id="src", confirm=True)
        self.assertEqual(TB.rejected_phrases(), [])
        for move in (None, "surgery:thin", "surgery:invert"):
            kept, _ = TB.reject_cull([src], move=move)
            self.assertEqual([p["id"] for p in kept], ["src"], move)

    def test_rejected_phrases_surface_move(self):
        self._drop("src", SEED, move="surgery:bass_shadow")
        recs = [r for r in TB.rejected_phrases()
                if r["identity"] == "src"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["move"], "surgery:bass_shadow")

    def test_moved_keep_notes_never_alias_to_the_source(self):
        # gears.judge_answer writes a moved KEEP as a moveless gold record
        # carrying the heard transformed notes. A later notes-only DROP of
        # those notes must not un-gold or globally ban the source.
        root = Path(self._td.name)
        with mock.patch.object(TB, "_PHRASE_POOL_OVERRIDE", [_rec("src", SEED)]):
            entry = ledger.record_write(
                clip_name="row", slot="3/0", source="catalogue", gear=3,
                move="surgery:thin", source_phrase_id="src", answer_notes=ALT_A,
                set_identity=IDENTITY,
                clip_ref={"clip_id": "row", "track_id": "jam",
                          "track": "SIDEKICK JAM", "surface": "session",
                          "path": "t3/s0", "answer_notes": ALT_A},
                path=str(root / "ledger.json"))
            gears.judge_answer(
                "keep", entry_id=entry["id"], ledger_path=str(root / "ledger.json"),
                ears_session_path=str(root / "ears.json"),
                develop_session_path=str(root / "develop.json"))
            C.rate(-1, label="dropped", notes=ALT_A)
            # checked with src indexed, as a real source phrase is
            rejects = TB.rejected_phrases()
            self.assertNotIn(("src", None),
                             [(r["identity"], r["move"]) for r in rejects])
            self.assertTrue(any(r["identity"].startswith("notes:") for r in rejects))
            self.assertEqual([r.get("phrase_id") for r in TB.get_gold_phrases()], ["src"])

    def test_unindexed_phrase_id_still_teaches_its_alias(self):
        # A composed gold / a phrase no longer indexed has no own notes to
        # compare, so its record teaches the alias: a notes-only un-gold
        # reaches it (server selftest block 23 relies on this).
        with mock.patch.object(TB, "_PHRASE_POOL_OVERRIDE", [_rec("src", SEED)]):
            C.rate(1, label="gold", phrase_id="composed|1", notes=ALT_B)
            self.assertIn("composed|1",
                          [g.get("phrase_id") for g in TB.get_gold_phrases()])
            C.rate(-1, label="ungold", notes=ALT_B)
            self.assertNotIn("composed|1",
                             [g.get("phrase_id") for g in TB.get_gold_phrases()])

    def test_session_report_names_pairings_and_advises_distinct_phrases(self):
        from mcp_server import server
        two_pairings = [{"identity": "src", "move": None},
                        {"identity": "src", "move": "surgery:thin"}]
        with mock.patch.object(ledger, "summary", return_value={"advice": []}), \
                mock.patch.object(TB, "rejected_phrases", return_value=two_pairings), \
                mock.patch.object(server, "_store_disk_state", return_value={}), \
                mock.patch("corpus_keys.corpus_staleness", return_value=[]):
            # hermetic: the corpus-age advice depends on today's date and the
            # real store's index age; it is not what this test is about
            report = server.session_report()
        self.assertEqual(report["n_rejected"], 1)
        self.assertEqual(report["rejected_pairings"], two_pairings)
        self.assertNotIn("pairings", report)
        # other advice (e.g. the corpus-age warning once the index is over six
        # weeks old) may come first; the suppression advice must be present
        self.assertTrue(any("1 phrase(s) are suppressed" in a for a in report["advice"]),
                        report["advice"])

    def test_moved_judgment_does_not_alias_heard_notes_to_source(self):
        # gears.judge_answer writes phrase_id=src with the heard transformed
        # notes and the move. A later notes-only DROP of that material must
        # remain independent of the source's global ban.
        root = Path(self._td.name)
        with mock.patch.object(TB, "_PHRASE_POOL_OVERRIDE", [_rec("src", SEED)]):
            entry = ledger.record_write(
                clip_name="row", slot="3/0", source="catalogue", gear=3,
                move="surgery:thin", source_phrase_id="src", answer_notes=ALT_A,
                set_identity=IDENTITY,
                clip_ref={"clip_id": "row", "track_id": "jam",
                          "track": "SIDEKICK JAM", "surface": "session",
                          "path": "t3/s0", "answer_notes": ALT_A},
                path=str(root / "ledger.json"))
            gears.judge_answer(
                "drop", entry_id=entry["id"], ledger_path=str(root / "ledger.json"),
                ears_session_path=str(root / "ears.json"),
                develop_session_path=str(root / "develop.json"))
            C.rate(-1, label="dropped", notes=ALT_A)
        rejects = TB.rejected_phrases()
        self.assertNotIn(("src", None),
                         [(r["identity"], r["move"]) for r in rejects])

    def test_moveless_source_notes_still_alias_to_source(self):
        with mock.patch.object(TB, "_PHRASE_POOL_OVERRIDE", [_rec("src", SEED)]):
            self._assert_moveless_source_alias()

    def _assert_moveless_source_alias(self):
        C.rate(-1, label="dropped", phrase_id="src", notes=SEED)
        C.rate(-1, label="dropped", notes=SEED)
        rejects = TB.rejected_phrases()
        self.assertEqual([(r["identity"], r["move"]) for r in rejects],
                         [("src", None)])


@requires_catalog
class DropPairingEndToEnd(unittest.TestCase):
    """DROP a developed answer: the source stays servable unmoved."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(mock.patch.object(
            ledger, "LEDGER_PATH", str(self.root / "ledger.json")))
        self.stack.enter_context(mock.patch.object(
            ears, "SESSION_PATH", str(self.root / "ears.json")))
        self.stack.enter_context(mock.patch.object(
            develop_session, "STATE_PATH", str(self.root / "develop.json")))
        old_ratings = C._ratings_path()
        C.set_ratings_path(str(self.root / "ratings.json"))
        self.addCleanup(C.set_ratings_path, old_ratings)
        self._prev_pool = TB._PHRASE_POOL_OVERRIDE
        self.addCleanup(setattr, TB, "_PHRASE_POOL_OVERRIDE", self._prev_pool)
        TB._PHRASE_POOL_OVERRIDE = [_rec("src", SEED),
                                    _rec("altA", ALT_A),
                                    _rec("altB", ALT_B)]
        # near seed: below search's 0.97 near-dup cut (so src stays in the
        # pool) and below the served-skip threshold (so the stamped serving
        # below doesn't hide src), yet near enough to rank it first
        src_vec = phrase_vector(SEED)
        self.seed2 = None
        for tweak in ((1, 1), (3, -1), (1, 1, 3, -1), (0, 2), (4, -2),
                      (2, 1, 5, -1), (1, 2), (3, 2)):
            cand = [dict(n) for n in SEED]
            for i, delta in zip(tweak[::2], tweak[1::2]):
                cand[i]["pitch"] += delta
            c = cosine(src_vec, phrase_vector(cand))
            if 0.90 <= c < 0.96:
                self.seed2 = cand
                break
        self.assertIsNotNone(self.seed2, "fixture needs a near seed")
        first = C.answer_my_loop(self.seed2, role="lead", seed=0,
                                 use_ears=False, humanize=False)
        self.assertEqual(first["source_phrase_ids"], ["src"])

    def _write(self, move):
        return ledger.record_write(
            clip_name="row", slot="3/0", source="catalogue", gear=3,
            move=move, source_phrase_id="src", answer_notes=SEED,
            set_identity=IDENTITY,
            clip_ref={"clip_id": "row", "track_id": "jam",
                      "track": "SIDEKICK JAM", "surface": "session",
                      "path": "t3/s0", "answer_notes": SEED},
            path=str(self.root / "ledger.json"))

    def test_developed_drop_bans_pairing_only(self):
        entry = self._write("motivic:fragment")
        res = gears.judge_answer("drop", entry_id=entry["id"])
        self.assertEqual(res["record"]["move"], "motivic:fragment")
        # same pairing: source gone ...
        moved = C.develop_my_loop(self.seed2, move="motivic:fragment",
                                  use_ears=False)
        self.assertNotEqual(moved["source_phrase_ids"], ["src"])
        # plain answer: source still serves (the reported bug was a global ban)
        plain = C.answer_my_loop(self.seed2, role="lead", seed=0,
                                 use_ears=False, humanize=False)
        self.assertEqual(plain["source_phrase_ids"], ["src"])
        # other move: source still serves
        other = C.develop_my_loop(self.seed2, move="slow-burn:reveal",
                                  use_ears=False)
        self.assertEqual(other["source_phrase_ids"], ["src"])

    def test_later_keep_lifts_moved_drop_through_judgment_path(self):
        from mcp_server import server
        first = self._write("surgery:thin")
        dropped = server.judge_answer("drop", entry_id=first["id"])
        self.assertEqual(dropped["record"]["move"], "surgery:thin")
        self.assertEqual(TB.reject_cull([_rec("src", SEED)],
                                        move="surgery:thin")[0], [])

        second = self._write("surgery:thin")
        kept = server.judge_answer("keep", entry_id=second["id"])
        self.assertEqual(kept["verdict"], "keep")
        self.assertEqual(TB.reject_cull([_rec("src", SEED)],
                                        move="surgery:thin")[0],
                         [_rec("src", SEED)])
        rows = ledger.load(str(self.root / "ledger.json"))["entries"]
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id[first["id"]]["immediate_verdict"], "drop")
        self.assertEqual(by_id[second["id"]]["immediate_verdict"], "keep")
        gold = TB.get_gold_phrases()
        self.assertIn("src", {r.get("phrase_id") for r in gold})


class ReasonBucketTests(unittest.TestCase):
    FLAT = [
        {"pitch": 72, "start": 0.0, "duration": 0.5, "velocity": 90},
        {"pitch": 72, "start": 1.0, "duration": 0.5, "velocity": 85},
        {"pitch": 72, "start": 2.0, "duration": 1.0, "velocity": 90},
        {"pitch": 72, "start": 3.0, "duration": 1.0, "velocity": 88},
    ]
    HIGH = [
        {"pitch": 84, "start": 0.0, "duration": 0.5, "velocity": 90},
        {"pitch": 88, "start": 0.5, "duration": 0.5, "velocity": 85},
        {"pitch": 91, "start": 1.0, "duration": 1.0, "velocity": 90},
        {"pitch": 86, "start": 2.0, "duration": 1.0, "velocity": 88},
    ]

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.path = str(Path(self._td.name) / "ears.json")

    def _drop(self, notes, reason):
        return ears.record_verdict({"notes": notes}, kept=False,
                                   path=self.path, reason=reason)

    def test_static_adds_contour_vote(self):
        self._drop(self.FLAT, "too static")
        state = ears.load_session(self.path)
        self.assertEqual(state["streaks"].get("contour:flat"), 2)

    def test_no_reason_single_vote(self):
        self._drop(self.FLAT, None)
        state = ears.load_session(self.path)
        self.assertEqual(state["streaks"].get("contour:flat"), 1)

    def test_wrong_register_demotes_register(self):
        anns = self._drop(self.HIGH, "wrong register")
        state = ears.load_session(self.path)
        self.assertEqual(state["streaks"].get("register:high"), 2)
        anns = self._drop(self.HIGH, "wrong register")
        state = ears.load_session(self.path)
        self.assertIn("register:high", state["demoted"])
        self.assertTrue(any("high" in a for a in anns), anns)

    def test_jumpy_maps_to_contour(self):
        self._drop(self.HIGH, "too jumpy")
        state = ears.load_session(self.path)
        buckets = ears.candidate_buckets({"notes": self.HIGH})
        self.assertEqual(
            state["streaks"].get("contour:%s" % buckets["contour"]), 2)


class JudgeDocstringClosureTests(unittest.TestCase):
    def test_judge_answer_docstring_holds_closure(self):
        from mcp_server import profiles, server
        docstrings = {name: getattr(server, name).__doc__ or ""
                      for name in profiles.ALL_TOOLS}
        for p in profiles.VALID_PROFILES:
            if "judge_answer" not in profiles.get_profile_tools(p):
                continue
            violations = profiles.check_tool_description_closure(
                p, docstrings)
            self.assertEqual(
                [v for v in violations if v.startswith("Tool 'judge_answer'")],
                [], f"judge_answer closure violations in '{p}': {violations}")


if __name__ == "__main__":
    unittest.main()
