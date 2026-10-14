"""B13 — continuity: piece identity apart from Set identity, piece-level
inheritance of owner intent, provenance-tagged piece records (statements,
proposals, reactions, observations) and the read-only piece brief.

No Live, no real store: every path is a temp dir (``piece_context._decision_path``
is patched, and the pieces/records stores are siblings of it).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from mcp_server import piece_context, piece_continuity

V2 = "/Users/x/Example Project/NOCTURNEv2.als"
V4 = "/Users/x/Example Project/NOCTURNEv4.als"
V5 = "/Users/x/Example Project/NOCTURNEv5.als"
LOOKALIKE = "/Users/x/Example Project/NOCTURNEv5 copy.als"
OTHER = "/Users/x/other/OTHERSONG.als"


SET_DIR = os.path.expanduser("~/Music/Example Project")


class _TempStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.decisions = os.path.join(self.temp.name, "piece_decisions.json")
        self.pieces = os.path.join(self.temp.name, "pieces.json")
        self.records = os.path.join(self.temp.name, "piece_records.json")
        patcher = mock.patch.object(piece_context, "_decision_path",
                                    lambda path=None: path or self.decisions)
        patcher.start()
        self.addCleanup(patcher.stop)

    def declare(self, set_key, piece="NOCTURNE", **kw):
        return piece_context.declare_piece_version(piece, set_key, confirm=True, **kw)

    def read(self, path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)


class TestPieceIdentity(_TempStore):
    """Rule 1: a Set belongs to a piece only when the owner confirmed it."""

    def test_declare_piece_version_preview_writes_nothing(self):
        out = piece_context.declare_piece_version("NOCTURNE", V2)
        self.assertEqual(out["status"], "preview")
        self.assertEqual(out["piece_id"], "nocturne")
        self.assertEqual(out["set_key"], V2)
        self.assertFalse(os.path.exists(self.pieces))
        self.assertIsNone(piece_context.resolve_piece(V2))

    def test_declare_piece_version_confirm_binds_set_to_piece_as_owner(self):
        out = self.declare(V2, notes_path="docs/pieces/NOCTURNE.md")
        self.assertEqual(out["status"], "confirmed")
        data = self.read(self.pieces)
        self.assertEqual(data["revision"], 1)
        (piece,) = data["pieces"]
        self.assertEqual(piece["piece_id"], "nocturne")
        self.assertEqual(piece["name"], "NOCTURNE")
        self.assertEqual(piece["notes_path"], "docs/pieces/NOCTURNE.md")
        (version,) = piece["set_keys"]
        self.assertEqual(version["set_key"], V2)
        self.assertEqual(version["confirmed_by"], "owner")
        self.assertIn("ts", version)
        resolved = piece_context.resolve_piece(V2)
        self.assertEqual(resolved["piece_id"], "nocturne")
        self.assertEqual(resolved["confirmed_versions"], [V2])

    def test_declaring_again_is_idempotent_and_a_second_version_joins(self):
        self.declare(V2)
        again = self.declare(V2)
        self.assertEqual(again["status"], "already_confirmed")
        self.declare(V5)
        data = self.read(self.pieces)
        (piece,) = data["pieces"]
        self.assertEqual([v["set_key"] for v in piece["set_keys"]], [V2, V5])
        self.assertEqual(piece_context.resolve_piece(V5)["confirmed_versions"], [V2, V5])

    def test_set_is_never_inferred_from_a_similar_filename(self):
        self.declare(V2)
        self.declare(V5)
        self.assertIsNone(piece_context.resolve_piece(LOOKALIKE))
        self.assertIsNone(piece_context.resolve_piece(V4))
        self.assertIsNone(piece_context.resolve_piece(V5.lower()))

    def test_a_set_belongs_to_exactly_one_piece(self):
        self.declare(V2)
        with self.assertRaisesRegex(ValueError, "nocturne"):
            piece_context.declare_piece_version("OTHERSONG", V2, confirm=True)
        (piece,) = self.read(self.pieces)["pieces"]
        self.assertEqual(piece["piece_id"], "nocturne")

    def test_current_decisions_inherits_intent_from_a_confirmed_sibling_version(self):
        piece_context.record_decision(V2, {"kind": "intent", "source": "owner",
                                           "goal": "keep the warmth", "preserve": ["sound"],
                                           "borrow": ["pacing", "layering"]})
        # before the owner confirms v5 as a version nothing is inherited
        self.assertEqual(piece_context.current_decisions(V5), {"revision": 0})
        self.declare(V2)
        self.declare(V5)
        current = piece_context.current_decisions(V5)
        self.assertEqual(current["goal"], "keep the warmth")
        self.assertEqual(current["preserve"], ["sound"])
        self.assertEqual(current["borrow"], ["pacing", "layering"])
        self.assertEqual(current["revision"], 0)          # v5 itself holds no rows
        self.assertEqual(current["piece"]["piece_id"], "nocturne")
        self.assertEqual(current["piece"]["confirmed_versions"], [V2, V5])
        self.assertEqual(current["inherited"]["goal"]["from"], V2)
        self.assertEqual(current["inherited"]["goal"]["source"], "owner")

    def test_version_specific_owner_record_overrides_the_inherited_one(self):
        self.declare(V2)
        self.declare(V5)
        piece_context.record_decision(V2, {"kind": "intent", "source": "owner",
                                           "goal": "v2 goal", "borrow": ["pacing"]})
        piece_context.record_decision(V5, {"kind": "intent", "source": "owner",
                                           "goal": "v5 goal"})
        current = piece_context.current_decisions(V5)
        self.assertEqual(current["goal"], "v5 goal")
        self.assertEqual(current["borrow"], ["pacing"])        # still inherited
        self.assertNotIn("goal", current["inherited"])
        self.assertEqual(current["inherited"]["borrow"]["from"], V2)
        self.assertEqual(current["revision"], 2)
        # v2 keeps its own view: nothing flows backwards from v5's later owner row
        self.assertEqual(piece_context.current_decisions(V2)["goal"], "v2 goal")

    def test_inherited_model_record_never_outranks_an_owner_record(self):
        self.declare(V2)
        self.declare(V5)
        piece_context.record_decision(V5, {"kind": "intent", "source": "model",
                                           "goal": "model guess on v5"})
        piece_context.record_decision(V2, {"kind": "intent", "source": "owner",
                                           "goal": "his words on v2"})
        self.assertEqual(piece_context.current_decisions(V5)["goal"], "his words on v2")
        # and a later model row on a sibling never beats an exact owner row
        piece_context.record_decision(V4, {"kind": "intent", "source": "model",
                                           "goal": "model guess on v4"})
        self.declare(V4)
        self.assertEqual(piece_context.current_decisions(V5)["goal"], "his words on v2")

    def test_inheritance_never_carries_track_ids_verdicts_or_proposal_ids(self):
        self.declare(V2)
        self.declare(V5)
        piece_context.record_decision(V2, {"kind": "intent", "source": "owner",
                                           "goal": "g", "protect_track_ids": ["2444"],
                                           "proposal_id": "p-v2", "ref_id": "ref-1",
                                           "verdict": "keep", "reason": "liked"})
        current = piece_context.current_decisions(V5)
        self.assertEqual(current["goal"], "g")
        self.assertEqual(current["ref_id"], "ref-1")
        for set_bound in ("protect_track_ids", "proposal_id", "verdict", "reason"):
            self.assertNotIn(set_bound, current)
        # an unrelated Set (never confirmed) sees nothing of the piece
        self.assertEqual(piece_context.current_decisions(OTHER), {"revision": 0})

    def test_no_pieces_file_keeps_the_old_exact_match_contract(self):
        piece_context.record_decision(V2, {"kind": "intent", "source": "owner", "goal": "g"})
        self.assertEqual(piece_context.current_decisions(V2), {"revision": 1, "goal": "g"})
        self.assertEqual(piece_context.current_decisions(V5), {"revision": 0})

    def test_explicit_paths_resolve_pieces_and_records_beside_that_file_never_the_default(self):
        # the default store binds v2+v5 and holds the v2 intent …
        self.declare(V2)
        self.declare(V5)
        piece_context.record_decision(V2, {"kind": "intent", "source": "owner", "goal": "g"})
        piece_continuity.record_statement("NOCTURNE", "Bassline is the bass", source="owner")
        # … but a caller naming another decisions file must see only that file's
        # siblings (none here): no inheritance leaks in from the default store
        other = os.path.join(self.temp.name, "elsewhere")
        os.makedirs(other)
        decisions = os.path.join(other, "piece_decisions.json")
        records = os.path.join(other, "piece_records.json")
        piece_context.record_decision(V2, {"kind": "intent", "source": "owner", "goal": "g2"},
                                      path=decisions)
        self.assertEqual(piece_context.current_decisions(V5, path=decisions), {"revision": 0})
        self.assertNotIn("piece", piece_context.current_decisions(V2, path=decisions))
        with self.assertRaisesRegex(ValueError, "unknown piece"):
            piece_continuity.record_statement("NOCTURNE", "x", source="owner", path=records)
        out = piece_continuity.piece_brief(V5, root=self.temp.name, path=records)
        self.assertIsNone(out["piece"])
        self.assertEqual(out["owner_statements"], [])
        self.assertFalse(os.path.exists(records))

    def test_server_tool_previews_confirms_and_reports_refusals(self):
        from mcp_server import server
        preview = server.declare_piece_version("NOCTURNE", V2)
        self.assertEqual(preview["status"], "preview")
        self.assertFalse(os.path.exists(self.pieces))
        done = server.declare_piece_version("NOCTURNE", V2, confirm=True,
                                            notes_path="docs/pieces/NOCTURNE.md")
        self.assertEqual(done["status"], "confirmed")
        self.assertEqual(done["notes_path"], "docs/pieces/NOCTURNE.md")
        refused = server.declare_piece_version("OTHERSONG", V2, confirm=True)
        self.assertFalse(refused["ok"])
        self.assertIn("exactly one piece", refused["error"])
        self.assertEqual(refused["writes"], 0)
        self.assertEqual(self.read(self.pieces)["revision"], 1)


class TestProvenance(_TempStore):
    """Rule 2: owner statements vs interpretations, never mixed."""

    def setUp(self):
        super().setUp()
        self.declare(V2)
        self.declare(V5)

    def test_statement_requires_a_known_source_and_a_quote(self):
        with self.assertRaisesRegex(ValueError, "source"):
            piece_continuity.record_statement("NOCTURNE", "Bassline is the bass", source="model")
        with self.assertRaisesRegex(ValueError, "quote"):
            piece_continuity.record_statement("NOCTURNE", "", source="owner")
        with self.assertRaisesRegex(ValueError, "declare_piece_version"):
            piece_continuity.record_statement("OTHERSONG", "x", source="owner")
        self.assertFalse(os.path.exists(self.records))

    def test_owner_statement_is_his_words_verbatim_at_piece_level(self):
        out = piece_continuity.record_statement("NOCTURNE", "Bassline is the bass",
                                                source="owner", topic="roles")
        self.assertTrue(out["record_id"].startswith("st-"))
        self.assertEqual(out["kind"], "statement")
        self.assertEqual(out["source"], "owner")
        self.assertEqual(out["quote"], "Bassline is the bass")
        self.assertEqual(out["piece_id"], "nocturne")
        self.assertIsNone(out["set_key"])
        data = self.read(self.records)
        self.assertEqual(data["revision"], 1)
        self.assertEqual(data["records"][0]["record_id"], out["record_id"])
        # the same words again are the same statement, not a second row
        again = piece_continuity.record_statement("NOCTURNE", "Bassline is the bass",
                                                  source="owner", topic="roles")
        self.assertEqual(again["record_id"], out["record_id"])
        self.assertEqual(len(self.read(self.records)["records"]), 1)

    def test_model_heard_and_measured_bind_to_the_bounce_and_set_they_came_from(self):
        with self.assertRaisesRegex(ValueError, "bound_to"):
            piece_continuity.record_statement("NOCTURNE", "the shaker is quiet",
                                              source="model_heard", dated="2026-09-30")
        with self.assertRaisesRegex(ValueError, "dated"):
            piece_continuity.record_statement("NOCTURNE", "the shaker is quiet",
                                              source="model_heard", set_key=V4)
        out = piece_continuity.record_statement(
            "NOCTURNE", "the shaker is barely audible under the pads", source="model_heard",
            set_key=V5, bound_to={"bounce": "example-mix_20240110-120000"}, dated="2026-09-30")
        self.assertEqual(out["bound_to"], {"bounce": "example-mix_20240110-120000", "set_key": V5})
        self.assertEqual(out["dated"], "2026-09-30")
        blocks = piece_continuity.statements_for("nocturne")
        self.assertEqual([r["quote"] for r in blocks["owner"]], [])
        self.assertEqual([r["source"] for r in blocks["model"]], ["model_heard"])

    def test_statement_blocks_keep_owner_and_model_apart(self):
        piece_continuity.record_statement("NOCTURNE", "the chorus starts at bar 33", source="owner")
        piece_continuity.record_statement("NOCTURNE", "loudest region 79–94",
                                          source="measured", set_key=V5, dated="2026-10-01")
        piece_continuity.record_statement("NOCTURNE", "the strip is a second peak",
                                          source="inference", set_key=V5, dated="2026-10-01")
        blocks = piece_continuity.statements_for("nocturne")
        self.assertEqual([r["quote"] for r in blocks["owner"]], ["the chorus starts at bar 33"])
        self.assertEqual([r["source"] for r in blocks["model"]], ["measured", "inference"])
        # a model statement bound to another Set is still the piece's, tagged
        for row in blocks["model"]:
            self.assertEqual(row["bound_to"]["set_key"], V5)


class TestProposals(_TempStore):
    """Rule 3: a proposal record — question, his gesture, the reference job,
    what stays, what varies, candidate handles, seeds."""

    def setUp(self):
        super().setUp()
        self.declare(V2)
        self.declare(V5)
        self.gesture = {"track_id": "1113", "clip_ids": ["2201"], "label": "Bassline 16ths 65–80"}
        self.job = {"ref": "Nolan", "job": "bells on their own grid", "evidence": "model_heard"}

    def propose(self, **kw):
        args = dict(piece="NOCTURNE", set_key=V5,
                    question="How can 65–80 keep its intensity while the foreground clears?",
                    source_gesture=self.gesture, reference_job=self.job,
                    preserve=["the synth 16ths"], vary=["drum density 65–80"],
                    candidates=[{"label": "drums sparse", "track_id": "2444", "clip_ids": ["2586"]},
                                {"label": "synths sparse", "track_id": "1113"}],
                    seeds=["seed=7", "tools/ideas/drift_sparse.py"], notes="one dimension")
        args.update(kw)
        return piece_continuity.record_proposal(**args)

    def test_record_proposal_returns_an_id_and_binds_piece_and_set(self):
        out = self.propose()
        self.assertTrue(out["proposal_id"].startswith("pr-"))
        self.assertEqual(out["kind"], "proposal")
        self.assertEqual(out["piece_id"], "nocturne")
        self.assertEqual(out["set_key"], V5)
        self.assertEqual(out["source"], "inference")
        self.assertEqual(out["reference_job"], self.job)
        self.assertEqual(out["preserve"], ["the synth 16ths"])
        self.assertEqual([c["label"] for c in out["candidates"]], ["drums sparse", "synths sparse"])
        self.assertEqual(out["seeds"], ["seed=7", "tools/ideas/drift_sparse.py"])
        self.assertEqual(piece_continuity.find_proposal(out["proposal_id"])["question"],
                         out["question"])
        self.assertIsNone(piece_continuity.find_proposal("pr-nope"))

    def test_record_proposal_refuses_a_set_the_owner_has_not_confirmed(self):
        with self.assertRaisesRegex(ValueError, "declare_piece_version"):
            self.propose(set_key=V4)
        with self.assertRaisesRegex(ValueError, "declare_piece_version"):
            self.propose(set_key=LOOKALIKE)
        self.assertFalse(os.path.exists(self.records))

    def test_record_proposal_validates_its_parts(self):
        with self.assertRaisesRegex(ValueError, "question"):
            self.propose(question="  ")
        with self.assertRaisesRegex(ValueError, "source_gesture"):
            self.propose(source_gesture={})
        with self.assertRaisesRegex(ValueError, "evidence"):
            self.propose(reference_job={"ref": "Nolan", "job": "bells", "evidence": "vibes"})
        with self.assertRaisesRegex(ValueError, "label"):
            self.propose(candidates=[{"track_id": "2444"}])
        self.assertFalse(os.path.exists(self.records))
        out = self.propose(reference_job=None, candidates=[], seeds=[])
        self.assertIsNone(out["reference_job"])

    def test_proposals_for_a_piece_list_every_version_tagged(self):
        a = self.propose()
        b = self.propose(set_key=V2, question="does 119–135 need an answer?")
        rows = piece_continuity.proposals_for("nocturne")
        self.assertEqual([r["proposal_id"] for r in rows], [a["proposal_id"], b["proposal_id"]])
        self.assertEqual([r["set_key"] for r in rows], [V5, V2])


class TestReactions(_TempStore):
    """Rule 4: his words verbatim, scoped; the model's reading stored apart;
    never a verdict."""

    def setUp(self):
        super().setUp()
        self.declare(V5)
        self.proposal = piece_continuity.record_proposal(
            "NOCTURNE", V5, "thin the pads at 65–80?",
            source_gesture={"track_id": "1113"},
            candidates=[{"label": "drums sparse", "track_id": "2444", "clip_ids": ["2586"]}])

    def test_record_reaction_stores_his_words_verbatim_with_scope(self):
        out = piece_continuity.record_reaction(
            "that works, maybe a touch brighter", "piece",
            piece="NOCTURNE", scope_detail="drums")
        self.assertTrue(out["record_id"].startswith("re-"))
        self.assertEqual(out["kind"], "reaction")
        self.assertEqual(out["source"], "owner")
        self.assertEqual(out["quote"], "that works, maybe a touch brighter")
        self.assertEqual(out["scope"], {"kind": "piece", "detail": "drums"})
        self.assertEqual(out["piece_id"], "nocturne")
        self.assertIsNone(out["proposal_id"])
        self.assertFalse(out["rule"])
        self.assertIsNone(out["interpretation"])

    def test_interpretation_is_stored_apart_and_never_promoted_to_a_rule(self):
        out = piece_continuity.record_reaction(
            "too busy here", "section", proposal_id=self.proposal["proposal_id"],
            scope_detail="65–80", interpretation="fewer drum attacks, keep the synth motion")
        self.assertEqual(out["interpretation"],
                         {"text": "fewer drum attacks, keep the synth motion", "source": "inference"})
        self.assertFalse(out["rule"])
        self.assertEqual(out["quote"], "too busy here")
        rule = piece_continuity.record_reaction(
            "no hats in this piece, ever", "piece", piece="NOCTURNE", rule=True)
        self.assertTrue(rule["rule"])
        self.assertEqual(rule["source"], "owner")

    def test_a_reaction_never_creates_a_verdict_or_touches_the_ledger(self):
        from mcp_server import ledger
        ledger_path = os.path.join(self.temp.name, "ledger.json")
        with mock.patch.object(ledger, "LEDGER_PATH", ledger_path):
            out = piece_continuity.record_reaction(
                "KEEP that one, drop the other", "candidate",
                proposal_id=self.proposal["proposal_id"], scope_detail="drums sparse")
        self.assertFalse(os.path.exists(ledger_path))
        for forbidden in ("verdict", "immediate_verdict", "terminal_state"):
            self.assertNotIn(forbidden, out)
        self.assertEqual(piece_context.current_decisions(V5).get("verdict"), None)
        decisions = piece_context._load_store(self.decisions, "decisions")
        self.assertEqual(decisions["decisions"], [])

    def test_reaction_needs_a_proposal_or_piece_and_a_known_scope(self):
        with self.assertRaisesRegex(ValueError, "proposal_id or piece"):
            piece_continuity.record_reaction("nice", "piece")
        with self.assertRaisesRegex(ValueError, "scope"):
            piece_continuity.record_reaction("nice", "vibe", piece="NOCTURNE")
        with self.assertRaisesRegex(ValueError, "scope_detail"):
            piece_continuity.record_reaction("nice", "section", piece="NOCTURNE")
        with self.assertRaisesRegex(ValueError, "words"):
            piece_continuity.record_reaction("  ", "piece", piece="NOCTURNE")
        with self.assertRaisesRegex(ValueError, "pr-nope"):
            piece_continuity.record_reaction("nice", "piece", proposal_id="pr-nope")
        with self.assertRaisesRegex(ValueError, "declare_piece_version"):
            piece_continuity.record_reaction("nice", "piece", piece="OTHERSONG")

    def test_reaction_on_a_proposal_inherits_its_piece_and_set(self):
        out = piece_continuity.record_reaction(
            "rhythm works, wrong sound", "candidate",
            proposal_id=self.proposal["proposal_id"], scope_detail="drums sparse")
        self.assertEqual(out["piece_id"], "nocturne")
        self.assertEqual(out["set_key"], V5)
        self.assertEqual(out["proposal_id"], self.proposal["proposal_id"])
        self.declare(OTHER, piece="OTHERSONG")
        with self.assertRaisesRegex(ValueError, "belongs to piece 'nocturne', not 'othersong'"):
            piece_continuity.record_reaction("x", "piece", proposal_id=self.proposal["proposal_id"],
                                             piece="OTHERSONG")
        (linked,) = piece_continuity.reactions_for(proposal_id=self.proposal["proposal_id"])
        self.assertEqual(linked["record_id"], out["record_id"])


class TestObservations(_TempStore):
    """Rule 5: a Set change seen on a read is observed, unjudged — never a verdict."""

    def test_record_observation_requires_a_snapshot_digest(self):
        with self.assertRaisesRegex(ValueError, "snapshot_digest"):
            piece_continuity.record_observation(V5, "track 2483 deleted", {})
        with self.assertRaisesRegex(ValueError, "what"):
            piece_continuity.record_observation(V5, "", {"snapshot_digest": "abc"})
        self.assertFalse(os.path.exists(self.records))

    def test_observation_is_observed_unjudged_and_bound_to_the_exact_set(self):
        self.declare(V5)
        out = piece_continuity.record_observation(
            V5, "SIDEKICK – peak line 1 space: track deleted",
            {"snapshot_digest": "d9f1", "track_id": "2483", "change": "track_deleted"})
        self.assertTrue(out["record_id"].startswith("ob-"))
        self.assertEqual(out["kind"], "observation")
        self.assertEqual(out["source"], "set_file")
        self.assertEqual(out["status"], "observed, unjudged")
        self.assertEqual(out["set_key"], V5)
        self.assertEqual(out["piece_id"], "nocturne")
        self.assertEqual(out["evidence"]["snapshot_digest"], "d9f1")
        for forbidden in ("verdict", "immediate_verdict", "terminal_state"):
            self.assertNotIn(forbidden, out)
        # an unconfirmed Set is observed too, with no piece
        other = piece_continuity.record_observation(OTHER, "clip moved", {"snapshot_digest": "e1"})
        self.assertIsNone(other["piece_id"])
        rows = piece_continuity.observations_for(V5)
        self.assertEqual([r["record_id"] for r in rows], [out["record_id"]])


class TestRecordToolsOnServer(_TempStore):
    """The MCP wrappers: refusals come back as {ok: False, error}, never raise;
    nothing is written on a refusal."""

    def test_record_tools_refuse_cleanly(self):
        from mcp_server import server
        out = server.record_proposal("NOCTURNE", V5, "q", source_gesture={"track_id": "1"})
        self.assertFalse(out["ok"])
        self.assertIn("declare_piece_version", out["error"])
        out = server.record_reaction("nice", "piece")
        self.assertFalse(out["ok"])
        out = server.record_observation(V5, "x", {})
        self.assertFalse(out["ok"])
        self.assertIn("snapshot_digest", out["error"])
        self.assertFalse(os.path.exists(self.records))
        self.declare(V5)
        out = server.record_proposal("NOCTURNE", V5, "q", source_gesture={"track_id": "1"})
        self.assertTrue(out["proposal_id"].startswith("pr-"))
        out = server.record_reaction("brighter", "piece", proposal_id=out["proposal_id"],
                                     scope_detail="drums", interpretation="more drum energy")
        self.assertEqual(out["quote"], "brighter")
        out = server.record_observation(V5, "track muted", {"snapshot_digest": "x1"})
        self.assertEqual(out["status"], "observed, unjudged")


class TestRound2(_TempStore):
    """Review round 2: exact-Set proposal links, corrupt stores degrade,
    unverified live reads, confirmed set_key on reactions, verbatim quotes."""

    def test_corrupt_records_or_pieces_degrade_the_brief_instead_of_raising(self):
        self.declare(V5)
        with open(self.records, "w") as handle:
            handle.write("{not json")
        out = piece_continuity.piece_brief(V5, root=self.temp.name)
        self.assertEqual(out["piece"]["piece_id"], "nocturne")
        self.assertIn("piece_records.json", " ".join(out["store_errors"]))
        self.assertEqual(out["proposals"], [])
        with open(self.pieces, "w") as handle:
            handle.write("[broken")
        out = piece_continuity.piece_brief(V5, root=self.temp.name)
        self.assertIsNone(out["piece"])
        self.assertIn("pieces.json", " ".join(out["store_errors"]))

    def test_corrupt_pieces_file_falls_back_to_set_only_decisions_with_a_warning(self):
        piece_context.record_decision(V5, {"kind": "intent", "source": "owner", "goal": "g"})
        with open(self.pieces, "w") as handle:
            handle.write("[broken")
        with self.assertWarnsRegex(RuntimeWarning, "pieces.json"):
            current = piece_context.current_decisions(V5)
        self.assertEqual(current, {"revision": 1, "goal": "g"})

    def test_live_snapshot_without_a_file_path_is_unverified(self):
        from mcp_server import moment, server
        self.declare(V5)

        async def fake(*_a, **_k):
            return {"set": {"name": "x"}, "identity": {}, "tracks": [], "sections": []}

        with mock.patch.object(moment, "read_piece_snapshot", fake), \
                mock.patch.object(server, "ROOT", self.temp.name):
            out = server.piece_brief(V5, live=True)
        self.assertEqual(out["latest_snapshot"]["status"], "live_unverified")

    def test_reaction_refuses_an_unconfirmed_set_key(self):
        self.declare(V5)
        proposal = piece_continuity.record_proposal("NOCTURNE", V5, "q",
                                                    source_gesture={"track_id": "1"})
        with self.assertRaisesRegex(ValueError, "declare_piece_version"):
            piece_continuity.record_reaction("x", "piece", proposal_id=proposal["proposal_id"],
                                             set_key=V4)
        with self.assertRaisesRegex(ValueError, "declare_piece_version"):
            piece_continuity.record_reaction("x", "piece", piece="NOCTURNE", set_key=V4)
        self.assertEqual(piece_continuity.reactions_for(piece_id="nocturne"), [])

    def test_owner_quotes_are_stored_exactly_as_given(self):
        self.declare(V5)
        st = piece_continuity.record_statement("NOCTURNE", "  Bassline is the bass ",
                                               source="owner")
        self.assertEqual(st["quote"], "  Bassline is the bass ")
        re = piece_continuity.record_reaction(" brighter\n", "piece", piece="NOCTURNE")
        self.assertEqual(re["quote"], " brighter\n")


class TestProposalLedgerLink(unittest.TestCase):
    """Rule 3, the link: register_arrangement_candidates items may carry a
    proposal_id; the ledger row stores it. Driven through the v4 Producer Pal
    fake from test_experiments_tools (no Live, temp ledger)."""

    def setUp(self):
        from contextlib import asynccontextmanager
        from mcp_server import ledger, pp_session
        from tests.test_experiments_tools import V4Device
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger_path = os.path.join(self.temp.name, "ledger.json")
        self.decisions = os.path.join(self.temp.name, "piece_decisions.json")
        for target, attr, value in ((ledger, "LEDGER_PATH", self.ledger_path),
                                    (piece_context, "_decision_path",
                                     lambda path=None: path or self.decisions)):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.device = V4Device()
        pp_session.set_live_process_incarnation_resolver(lambda: dict(self.device.incarnation))
        self.addCleanup(pp_session.set_live_process_incarnation_resolver, None)

        @asynccontextmanager
        async def fake_operation(*_a, **_k):
            yield self.device.pp_session()

        patcher = mock.patch.object(pp_session, "operation_session", fake_operation)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ledger = ledger
        self.set_path = self.device.file_path

    def rows(self):
        from mcp_server import experiments
        return experiments.all_rows(self.ledger.load(self.ledger_path))

    def test_registered_row_stores_the_proposal_id(self):
        from mcp_server import server
        piece_context.declare_piece_version("NOCTURNE", self.set_path, confirm=True)
        proposal = piece_continuity.record_proposal(
            "NOCTURNE", self.set_path, "does 119–135 need an answer?",
            source_gesture={"track_id": "2537", "clip_ids": ["2567"]},
            candidates=[{"label": "line 3", "track_id": "2537", "clip_ids": ["2567"]}])
        out = server.register_arrangement_candidates(
            items=[{"kind": "arrangement_experiment", "label": "line 3", "track_id": "2537",
                    "clip_ids": ["2567"], "proposal_id": proposal["proposal_id"]}],
            set_path=self.set_path, confirm=True)
        self.assertTrue(out["ok"], out)
        (registered,) = out["registered"]
        self.assertEqual(registered["proposal_id"], proposal["proposal_id"])
        (row,) = self.rows()
        self.assertEqual(row["proposal_id"], proposal["proposal_id"])
        self.assertEqual(row["set_identity"]["file_path"], self.set_path)

    def test_a_proposal_from_another_version_or_piece_is_refused_before_any_read(self):
        from mcp_server import server
        v2 = self.set_path.replace("v4.als", "v2.als")
        piece_context.declare_piece_version("NOCTURNE", v2, confirm=True)
        piece_context.declare_piece_version("NOCTURNE", self.set_path, confirm=True)
        piece_context.declare_piece_version("OTHERSONG", OTHER, confirm=True)
        for piece, key in (("NOCTURNE", v2), ("OTHERSONG", OTHER)):
            proposal = piece_continuity.record_proposal(piece, key, "q",
                                                        source_gesture={"track_id": "2537"})
            out = server.register_arrangement_candidates(
                items=[{"kind": "arrangement_experiment", "label": "line 3", "track_id": "2537",
                        "clip_ids": ["2567"], "proposal_id": proposal["proposal_id"]}],
                set_path=self.set_path, confirm=True)
            self.assertFalse(out["ok"])
            self.assertIn(key, out["error"])
            self.assertEqual(self.device.wire_calls, [])
            self.assertEqual(self.rows(), [])

    def test_an_unfiled_proposal_id_is_refused_before_any_read(self):
        from mcp_server import server
        out = server.register_arrangement_candidates(
            items=[{"kind": "arrangement_experiment", "label": "line 3", "track_id": "2537",
                    "clip_ids": ["2567"], "proposal_id": "pr-nope"}],
            set_path=self.set_path, confirm=True)
        self.assertFalse(out["ok"])
        self.assertIn("pr-nope", out["error"])
        self.assertEqual(self.device.wire_calls, [])
        self.assertEqual(self.rows(), [])

    def test_items_without_a_proposal_register_as_before(self):
        from mcp_server import server
        out = server.register_arrangement_candidates(
            items=[{"kind": "arrangement_experiment", "label": "line 3", "track_id": "2537",
                    "clip_ids": ["2567"]}], set_path=self.set_path, confirm=True)
        self.assertTrue(out["ok"], out)
        (row,) = self.rows()
        self.assertIsNone(row["proposal_id"])


NOTES_MD = """# NOCTURNE — piece notes (owner: example)

Set: `~/Documents/Example Project/NOCTURNEv5.als`.

## Analysis of v5 (2026-10-01)

| Bars | What plays |
|---|---|
| 65–80 | the bridge |

## Suggestions (2026-10-01) — status

| # | Suggestion | Status |
|---|---|---|
| 1 | Thin the pads at 65–80 | proposed |
| 5 | Drop the second synth | awaiting owner |

## Cuts and transitions (model transitions pass, 2026-10-01)

| Join | Gemini | Edit | Who | Status |
|---|---|---|---|---|
| 41 (1:52) | abrupt | a short fill before 41 | owner edit | proposed |
| 146+ | "ending" | ending still open | owner decision | open |

## Listens of the bounces (model-heard)
- something
"""


class TestPieceBrief(_TempStore):
    """Rule 6: piece_brief(set_path) — read-only, Live optional, structured +
    a short prose brief; provenance blocks kept apart."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.temp.name, "repo")
        os.makedirs(os.path.join(self.root, "docs", "pieces"))
        with open(os.path.join(self.root, "docs", "pieces", "NOCTURNE.md"), "w",
                  encoding="utf-8") as handle:
            handle.write(NOTES_MD)
        self.declare(V2, notes_path="docs/pieces/NOCTURNE.md")
        self.declare(V5)
        piece_context.record_decision(V2, {"kind": "intent", "source": "owner",
                                           "goal": "keep the warmth", "preserve": ["sound"],
                                           "borrow": ["pacing", "layering"],
                                           "protect_track_ids": ["21"]})
        piece_continuity.record_statement("NOCTURNE", "Bassline is the bass", source="owner",
                                          topic="roles")
        piece_continuity.record_statement(
            "NOCTURNE", "the shaker is barely audible", source="model_heard", set_key=V5,
            bound_to={"bounce": "example-mix_20240112-090000"}, dated="2026-10-01")
        self.proposal = piece_continuity.record_proposal(
            "NOCTURNE", V5, "thin the pads at 65–80?",
            source_gesture={"track_id": "1113", "clip_ids": ["2201"]},
            reference_job={"ref": "Nolan", "job": "bells on their own grid",
                           "evidence": "model_heard"},
            preserve=["the synth 16ths"], vary=["drum density"],
            candidates=[{"label": "drums sparse", "track_id": "2444", "clip_ids": ["2586"]}],
            seeds=["seed=7"])
        self.reaction = piece_continuity.record_reaction(
            "maybe a touch brighter", "piece", proposal_id=self.proposal["proposal_id"],
            scope_detail="drums", interpretation="more drum energy, same notes")
        self.observation = piece_continuity.record_observation(
            V5, "SIDEKICK – peak line 1 space: track deleted",
            {"snapshot_digest": "d9f1", "track_id": "2483"})

    def brief(self, set_key=V5, **kw):
        return piece_continuity.piece_brief(set_key, root=self.root, **kw)

    def store_state(self):
        state = {}
        for name in ("pieces.json", "piece_records.json", "piece_decisions.json"):
            full = os.path.join(self.temp.name, name)
            with open(full, "rb") as handle:
                state[name] = (os.stat(full).st_mtime_ns, handle.read())
        return state

    def test_brief_collects_the_piece_with_provenance_blocks_apart(self):
        out = self.brief()
        self.assertEqual(out["piece"]["piece_id"], "nocturne")
        self.assertEqual(out["piece"]["confirmed_versions"], [V2, V5])
        self.assertEqual(out["piece"]["this_version"], V5)
        self.assertEqual(out["intent"]["goal"], "keep the warmth")
        self.assertEqual(out["intent"]["inherited"]["goal"]["from"], V2)
        self.assertNotIn("protect_track_ids", out["intent"])
        self.assertEqual([r["quote"] for r in out["owner_statements"]], ["Bassline is the bass"])
        (heard,) = out["model_observations"]
        self.assertEqual(heard["source"], "model_heard")
        self.assertEqual(heard["dated"], "2026-10-01")
        self.assertEqual(heard["bound_to"]["bounce"], "example-mix_20240112-090000")
        (proposal,) = out["proposals"]
        self.assertEqual(proposal["proposal_id"], self.proposal["proposal_id"])
        (reaction,) = proposal["reactions"]
        self.assertEqual(reaction["quote"], "maybe a touch brighter")
        self.assertEqual(reaction["interpretation"]["source"], "inference")
        (ref,) = out["references"]
        self.assertEqual(ref, {"ref": "Nolan", "job": "bells on their own grid",
                               "evidence": "model_heard",
                               "proposal_id": self.proposal["proposal_id"]})
        (observed,) = out["observations"]
        self.assertEqual(observed["status"], "observed, unjudged")
        self.assertEqual(out["latest_snapshot"]["status"], "cached_from_observation")
        self.assertEqual(out["latest_snapshot"]["snapshot_digest"], "d9f1")
        self.assertIn("NOCTURNE", out["prose"])
        self.assertIn("keep the warmth", out["prose"])
        self.assertIn("observed, unjudged", out["prose"])
        self.assertEqual(out["writes"], 0)

    def test_brief_reads_the_notes_sections_with_their_status(self):
        out = self.brief()
        notes = out["notes"]
        self.assertEqual(notes["path"], "docs/pieces/NOCTURNE.md")
        suggestions = notes["sections"]["suggestions"]
        self.assertTrue(suggestions["heading"].startswith("Suggestions"))
        self.assertEqual([row["Status"] for row in suggestions["rows"]],
                         ["proposed", "awaiting owner"])
        cuts = notes["sections"]["cuts_and_transitions"]
        self.assertEqual([row["Join"] for row in cuts["rows"]], ["41 (1:52)", "146+"])
        self.assertEqual(cuts["rows"][1]["Status"], "open")
        open_questions = out["open_questions"]
        self.assertIn("suggestion #5: Drop the second synth (awaiting owner)",
                      open_questions)
        self.assertIn("join 146+: ending still open (open)", open_questions)

    def test_brief_without_notes_or_piece_says_so_and_still_lists_observations(self):
        os.remove(os.path.join(self.root, "docs", "pieces", "NOCTURNE.md"))
        out = self.brief()
        self.assertEqual(out["notes"]["status"], "missing")
        piece_continuity.record_observation(V4, "clip moved", {"snapshot_digest": "e1"})
        out = self.brief(V4)
        self.assertIsNone(out["piece"])
        self.assertIn("declare_piece_version", out["note"])
        self.assertEqual(out["intent"], {"revision": 0})
        self.assertEqual(out["owner_statements"], [])
        self.assertEqual([o["what"] for o in out["observations"]], ["clip moved"])
        self.assertIn("not a confirmed version", out["prose"])

    def test_server_brief_is_read_only_and_never_contacts_live_by_default(self):
        from mcp_server import moment, server
        before = self.store_state()

        async def never(*_a, **_k):
            raise AssertionError("Live was contacted")

        with mock.patch.object(moment, "read_piece_snapshot", never), \
                mock.patch.object(server, "ROOT", self.root):
            out = server.piece_brief(V5)
        self.assertEqual(out["piece"]["piece_id"], "nocturne")
        self.assertEqual(out["latest_snapshot"]["status"], "cached_from_observation")
        self.assertEqual(self.store_state(), before)

    def test_server_brief_live_true_summarises_the_snapshot_or_reports_the_failure(self):
        from mcp_server import moment, server
        snapshot = {"kind": "piece_snapshot",
                    "set": {"name": "NOCTURNEv5", "tempo": 85.0, "meter": "4/4", "bars": 183},
                    "identity": {"file_path": V5, "set_id": V5, "structural": {"digest": "s1"}},
                    "tracks": [{"id": "21", "name": "25-Bassline"}],
                    "sections": [{"bars": [1, 12]}, {"bars": [13, 40]}],
                    "muted_tracks": [{"track_id": "2515", "track": "SIDEKICK – peak line 2"}],
                    "unheard": [{"track": "ADH"}]}

        async def fake(*_a, **_k):
            return snapshot

        with mock.patch.object(moment, "read_piece_snapshot", fake), \
                mock.patch.object(server, "ROOT", self.root):
            out = server.piece_brief(V5, live=True)
        latest = out["latest_snapshot"]
        self.assertEqual(latest["status"], "live")
        self.assertEqual(latest["set"]["name"], "NOCTURNEv5")
        self.assertEqual(latest["tracks"], 1)
        self.assertEqual(latest["sections"], 2)
        self.assertEqual(latest["muted_tracks"], ["SIDEKICK – peak line 2"])
        self.assertEqual(latest["structural_digest"], "s1")
        self.assertEqual(latest["unheard"], 1)

        async def dead(*_a, **_k):
            raise ConnectionError("refused")

        with mock.patch.object(moment, "read_piece_snapshot", dead), \
                mock.patch.object(server, "ROOT", self.root):
            out = server.piece_brief(V5, live=True)
        self.assertEqual(out["latest_snapshot"]["status"], "unavailable")
        self.assertIn("refused", out["latest_snapshot"]["reason"])
        self.assertEqual(out["piece"]["piece_id"], "nocturne")
        # another Set open in Live: the read is reported as that Set's, never as this one's
        with mock.patch.object(moment, "read_piece_snapshot", fake), \
                mock.patch.object(server, "ROOT", self.root):
            out = server.piece_brief(V2, live=True)
        self.assertEqual(out["latest_snapshot"]["status"], "live_other_set")
        self.assertEqual(out["latest_snapshot"]["file_path"], V5)
        self.assertIn("NOCTURNEv5.als", out["latest_snapshot"]["note"])
        self.assertIn("another Set", out["prose"])


class TestDeclarePieceMigration(unittest.TestCase):
    """tools/declare_piece.py — the owner-run seed, tested on a temp store (never the real one)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = os.path.join(self.temp.name, "store")
        os.makedirs(self.store)
        # A frozen copy of an owner's v1 intent, so the inheritance assertion below
        # never depends on a live store.
        with open(os.path.join(self.store, "piece_decisions.json"), "w", encoding="utf-8") as handle:
            json.dump({"revision": 1, "decisions": [{
                "kind": "intent", "source": "owner",
                "goal": "Keep the warm tape sound and write a longer bridge.",
                "preserve": ["sound"], "borrow": ["pacing", "layering"],
                "reason": "stated by the owner 2024-01-05",
                "set_key": os.path.join(SET_DIR, "EXAMPLEv1.als"),
                "ts": "2024-01-05T10:00:00+00:00", "revision": 1}]}, handle)

    def run_script(self, *args):
        import io
        from contextlib import redirect_stdout
        from tools import declare_piece
        out = io.StringIO()
        with redirect_stdout(out):
            code = declare_piece.main(["--store", self.store, "--spec", declare_piece.EXAMPLE_SPEC, *args])
        return code, out.getvalue()

    def read(self, name):
        with open(os.path.join(self.store, name), encoding="utf-8") as handle:
            return json.load(handle)

    def test_import_and_dry_run_write_nothing_and_print_the_plan(self):
        from tools import declare_piece
        self.assertFalse(os.path.exists(os.path.join(self.store, "pieces.json")))
        code, text = self.run_script()
        self.assertEqual(code, 0)
        for version in (1, 2, 3):
            self.assertIn(os.path.join(SET_DIR, "EXAMPLEv%d.als")
                          % version, text)
        self.assertIn("--confirm", text)
        self.assertIn("the cello patch carries the low line", text)
        self.assertFalse(os.path.exists(os.path.join(self.store, "pieces.json")))
        self.assertFalse(os.path.exists(os.path.join(self.store, "piece_records.json")))
        

    def test_confirm_seeds_the_example_with_provenance_and_is_idempotent(self):
        code, text = self.run_script("--confirm")
        self.assertEqual(code, 0)
        (piece,) = self.read("pieces.json")["pieces"]
        self.assertEqual(piece["piece_id"], "example")
        self.assertEqual(piece["notes_path"], "docs/pieces/EXAMPLE.md")
        self.assertEqual([v["set_key"] for v in piece["set_keys"]], [
            os.path.join(SET_DIR, "EXAMPLEv%d.als") % n
            for n in (1, 2, 3)])
        self.assertTrue(all(v["confirmed_by"] == "owner" and v["via"] == "cli"
                            for v in piece["set_keys"]))
        records = self.read("piece_records.json")["records"]
        owner = [r for r in records if r["kind"] == "statement" and r["source"] == "owner"]
        self.assertEqual([r["quote"] for r in owner], [
            "the cello patch carries the low line", "the second piano is a sketch, ignore it",
            "the chorus starts at bar 33"])
        heard = [r for r in records if r["source"] == "model_heard"]
        self.assertEqual([(r["quote"], r["dated"], r["bound_to"]["bounce"]) for r in heard], [
            ("the shaker is barely audible under the pads", "2024-01-10", "example-mix_20240110-120000")])
        (measured,) = [r for r in records if r["source"] == "measured"]
        self.assertEqual(measured["dated"], "2024-01-12")
        self.assertEqual(measured["bound_to"]["bounce"], "example-mix_20240112-090000")
        self.assertIn("chorus", measured["evidence"]["loudness_db"])
        reactions = [r for r in records if r["kind"] == "reaction"]
        self.assertEqual([(r["quote"], r["scope"]) for r in reactions], [
            ("more space before the chorus", {"kind": "piece", "detail": "form"}),
            ("keep the cello dry", {"kind": "global", "detail": None})])
        self.assertTrue(all(r["source"] == "owner" and r["rule"] is False
                            and r["interpretation"] is None for r in reactions))
        self.assertIn("3 version", text)
        # the owner's existing v1 intent now resolves under v3, inherited
        decisions = os.path.join(self.store, "piece_decisions.json")
        current = piece_context.current_decisions(
            os.path.join(SET_DIR, "EXAMPLEv3.als"), path=decisions,
            pieces_path=os.path.join(self.store, "pieces.json"))
        self.assertIn("bridge", current["goal"])
        self.assertEqual(current["inherited"]["goal"]["from"],
                         os.path.join(SET_DIR, "EXAMPLEv1.als"))
        # a second run adds nothing
        before = (self.read("pieces.json"), self.read("piece_records.json"))
        code, text = self.run_script("--confirm")
        self.assertEqual(code, 0)
        self.assertEqual((self.read("pieces.json"), self.read("piece_records.json")), before)
        self.assertIn("nothing new", text)


if __name__ == "__main__":
    unittest.main()
