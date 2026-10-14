"""Lane B2 AUDIOREF-V3 regression tests (R1..R8, X1..X7). Synthetic only."""
from __future__ import annotations

import unittest
from unittest import mock

from mcp_server import core as _core
from mcp_server import server as _server
from tools import audio_ref as _ar
from tests._stage_passthrough import passthrough_listenable_staging
from tests._requires import requires_store


def _four_on_floor_card():
    return {
        "ref_id": "r-four",
        "identity": "Test Four",
        "ownership": "his",
        "structure": {
            "feel_bpm": 120.0, "grid_bpm": 120.0, "density": "high",
        },
        "drums": {"grid": [{"instrument": "kick", "steps": [0, 4, 8, 12]}]},
    }


class TestR1CardGridBeforeProse(unittest.TestCase):
    def test_card_grid_to_notes_four_on_floor(self):
        notes, ok = _ar.card_grid_to_notes(_four_on_floor_card())
        self.assertTrue(ok)
        self.assertEqual(
            sorted(n["start"] for n in notes if n["pitch"] == 36),
            [0.0, 1.0, 2.0, 3.0],
        )

    def test_beat_from_ref_uses_structured_grid(self):
        card = _four_on_floor_card()
        seen: dict = {}
        fake_res = {"notes": [], "tempo": 120.0, "source_phrase_ids": ["x"],
                    "assumptions": []}

        def _fake_beat(grid_notes, **kw):
            # R2 runs a second unsteered retrieval (empty grid); the steered
            # call comes first, so keep it.
            if "grid_notes" not in seen:
                seen["grid_notes"] = list(grid_notes or [])
            return dict(fake_res)

        with mock.patch.object(
            _server, "_audio_ref_mod", return_value=_ar
        ), mock.patch.object(
            _ar, "get_ref_card", return_value=card
        ), mock.patch.object(
            _core, "beat_from_grid", side_effect=_fake_beat
        ):
            res = _server.beat_from_ref("r-four", bars=1, seed=0)
        kicks = sorted(
            n["start"] for n in seen.get("grid_notes", []) if n["pitch"] == 36
        )
        self.assertEqual(kicks, [0.0, 1.0, 2.0, 3.0])
        self.assertEqual(res["ref"]["grid_source"], "card_grid")


class TestReindexUsesValidatedCardContract(unittest.TestCase):
    def test_moments_come_from_the_validated_card_not_a_stale_describe(self):
        import shutil
        import tempfile
        from pathlib import Path

        # The synthetic v2 card (tests/fixtures/audio_ref/make_synthetic.py) has
        # eight sections; describe.txt stands in for the stale render of a later
        # unverified attempt.
        fixture = Path(__file__).parent / "fixtures/audio_ref/longarc_v2_stale_meta"
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            shutil.copyfile(fixture / "card.json", out / "card.json")
            (out / "describe.txt").write_text(
                "# Identity\nstale\n\n# Moments\n- 00:05 – unverified stale moment\n",
                encoding="utf-8")
            times = [m["t"] for m in _ar._moment_timestamps(out)]
        # the v2 card's own eight sections, never the stale 00:05
        self.assertEqual(times, [0.0, 80.0, 170.0, 250.0, 330.0, 420.0, 510.0, 580.0])
        self.assertNotIn(5.0, times)

    def test_apply_audio_ref_uses_longarc_card_over_stale_describe(self):
        import json
        import shutil
        import tempfile
        from pathlib import Path

        # Synthetic v3 card fixture (B minor); describe.txt represents an older
        # stale render and deliberately disagrees about identity and key.
        fixture = Path(__file__).parent / "fixtures/audio_ref/longarc_v3.json"
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "longarc"
            out.mkdir()
            shutil.copyfile(fixture, out / "card.json")
            (out / "describe.txt").write_text(
                "# Identity\nStale track in D minor\n\n"
                "# Imitation rules\n- Do: Stay in D natural minor\n",
                encoding="utf-8")
            (out / "ref.mid").write_bytes(b"")
            idx = Path(td) / "index.json"
            _ar.save_index({"version": 1, "refs": {"longarc": {
                "ref_id": "longarc", "out_dir": str(out),
                "identity": "stale D minor", "imitation_do": ["Stay in D natural minor"],
            }}}, idx)

            old_root = _ar.OUT_ROOT
            _ar.OUT_ROOT = out.parent
            try:
                result = _ar.apply_audio_ref(
                    "longarc", include_midi=False, index_path=idx)
            finally:
                _ar.OUT_ROOT = old_root
            self.assertIn("B minor", result["identity"])
            self.assertNotIn("D natural minor", " ".join(result["imitation_do"]))

    def test_clip_moments_children_use_longarc_card_rules_over_stale_describe(self):
        import json
        import shutil
        import tempfile
        from pathlib import Path

        # Same synthetic card; the sibling describe is stale prose.
        fixture = Path(__file__).parent / "fixtures/audio_ref/longarc_v3.json"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "refs"
            root.mkdir()
            parent = root / "longarc"
            parent.mkdir()
            shutil.copyfile(fixture, parent / "card.json")
            (parent / "source.wav").write_bytes(b"wav")
            (parent / "describe.txt").write_text(
                "# Identity\nStale D minor\n\n"
                "# Imitation rules\n- Do: Stay in D natural minor\n",
                encoding="utf-8")
            index = Path(td) / "index.json"
            index.write_text(json.dumps({"refs": {"longarc": {
                "ref_id": "longarc", "out_dir": str(parent), "title": "Longarc",
                "duration_s": 200,
            }}}))

            def fake_ffmpeg(_src, dst, _start, _end, **kwargs):
                dst.write_bytes(b"wav")

            old_root = _ar.OUT_ROOT
            _ar.OUT_ROOT = root
            try:
                with mock.patch.object(_ar, "ffmpeg_to_wav", side_effect=fake_ffmpeg):
                    result = _ar.clip_moments(
                        "longarc", modes=[], index_path=index, max_moments=1)
            finally:
                _ar.OUT_ROOT = old_root
            child = Path(result["children"][0]["out_dir"])
            child_text = (child / "describe.txt").read_text(encoding="utf-8")
            self.assertNotIn("D natural minor", child_text)
            self.assertIn("B minor", child_text)

    def test_legacy_describe_keeps_its_moments_but_never_reads_sections(self):
        import tempfile
        from pathlib import Path

        # Legacy prose refs (no card.json, ~130 of the real 185) keep their
        # # Moments list for browsing; a # Sections heading is a v3 render.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "describe.txt").write_text(
                "# Identity\nlegacy\n\n# Moments\n- 00:02 – hit\n- 00:08 – swell\n",
                encoding="utf-8")
            self.assertEqual([m["t"] for m in _ar._moment_timestamps(out)], [2.0, 8.0])
            (out / "describe.txt").write_text(
                "# Identity\nstale v3\n\n# Sections\n- 00:05 – stale\n", encoding="utf-8")
            self.assertEqual(_ar._moment_timestamps(out), [])

    def test_reindex_replaces_stale_v3_index_from_longarc_v2_card(self):
        import json
        import shutil
        import tempfile
        from pathlib import Path

        # Synthetic fixture pair: card.json is v2 while meta.json records a later
        # v3 analysis attempt.
        fixture = Path(__file__).parent / "fixtures/audio_ref/longarc_v2_stale_meta"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "audio_refs"
            root.mkdir()
            out = root / "longarc-ref"
            out.mkdir()
            shutil.copyfile(fixture / "card.json", out / "card.json")
            (out / "meta.json").write_bytes((fixture / "meta.json").read_bytes())
            (out / "source.wav").write_bytes(b"")
            idx = Path(td) / "index.json"
            stale = {
                "ref_id": "longarc-ref", "out_dir": str(out),
                "contract_version": _ar.V3_CONTRACT_VERSION,
                "sections": [{"t": "00:00", "label": "stale v3 section"}],
            }
            _ar.save_index({"version": 1, "refs": {"longarc-ref": stale}}, idx)
            orig_root, orig_index = _ar.OUT_ROOT, _ar.INDEX_PATH
            _ar.OUT_ROOT, _ar.INDEX_PATH = root, idx
            try:
                _ar.upsert_index_card(dict(stale), path=idx)
                indexed = _ar.load_index(idx)["refs"]["longarc-ref"]
                self.assertEqual(indexed["contract_version"],
                                 _ar.CARD_CONTRACT_VERSION)
                self.assertEqual(indexed["sections"],
                                 json.loads((out / "card.json").read_text(
                                     encoding="utf-8"))["sections"])
                _ar._reindex_audio_refs_impl(root=root, index_path=idx)
                row = _ar.load_index(idx)["refs"]["longarc-ref"]
                card = json.loads(
                    (out / "card.json").read_text(encoding="utf-8"))
                self.assertEqual(row["contract_version"],
                                 card["contract_version"])
                self.assertEqual(row["sections"], card["sections"])
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = orig_root, orig_index

    def test_beat_from_ref_unwraps_index_shaped_cards(self):
        # Real index cards nest the filed card under "card" (card_from_v2 /
        # card_from_v3); beat_from_ref must read the grid from the inner card,
        # not misparse the rendered prose as beats.
        from tests.test_relisten_v2_listener import (
            _card as _v2_card, _v3_card)

        index_cards = []
        for inner, wrap, rid in (
                (_v2_card(), _ar.card_from_v2, "r2-idx"),
                (_v3_card(), _ar.card_from_v3, "r3-idx")):
            inner["drums"]["grid"] = [
                {"instrument": "kick", "steps": [0, 4, 8, 12]}]
            built = wrap(inner, ref_id=rid, out_dir="/tmp/" + rid)
            # beat_from_ref answers only his-owned refs.
            built["ownership"] = "his"
            index_cards.append(built)
        for index_card in index_cards:
            seen: dict = {}
            fake_res = {"notes": [], "tempo": 120.0,
                        "source_phrase_ids": ["x"], "assumptions": []}

            def _fake_beat(grid_notes, **kw):
                if "grid_notes" not in seen:
                    seen["grid_notes"] = list(grid_notes or [])
                return dict(fake_res)

            with mock.patch.object(
                _server, "_audio_ref_mod", return_value=_ar
            ), mock.patch.object(
                _ar, "get_ref_card", return_value=index_card
            ), mock.patch.object(
                _core, "beat_from_grid", side_effect=_fake_beat
            ):
                res = _server.beat_from_ref(
                    index_card["ref_id"], bars=1, seed=0)
            kicks = sorted(
                n["start"] for n in seen.get("grid_notes", [])
                if n["pitch"] == 36
            )
            self.assertEqual(kicks, [0.0, 1.0, 2.0, 3.0],
                             index_card["ref_id"])
            self.assertEqual(res["ref"]["grid_source"], "card_grid",
                             index_card["ref_id"])


class TestR5MomentChildrenInheritParentTempo(unittest.TestCase):
    def test_longarc_children_transcribe_at_parent_measured_tempo(self):
        import json
        import shutil
        import tempfile
        from pathlib import Path

        # The synthetic measured block (tests/fixtures/audio_ref/longarc_measured.json,
        # tempo_bpm 142.0) outranks the caller's 120 BPM guess; the card says
        # grid 142, feel 71.
        fixture = Path(__file__).parent / "fixtures/audio_ref/longarc_v3.json"
        measured_fixture = (
            Path(__file__).parent / "fixtures/audio_ref/longarc_measured.json")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "refs"
            root.mkdir()
            parent = root / "longarc"
            parent.mkdir()
            shutil.copyfile(fixture, parent / "card.json")
            (parent / "source.wav").write_bytes(b"wav")
            (parent / "describe.txt").write_text(
                "# Identity\nLongarc\n", encoding="utf-8")
            measured = json.loads(
                measured_fixture.read_text(encoding="utf-8"))
            (parent / "meta.json").write_text(
                json.dumps({"measured": measured}), encoding="utf-8")
            index = Path(td) / "index.json"
            index.write_text(json.dumps({"refs": {"longarc": {
                "ref_id": "longarc", "out_dir": str(parent), "title": "Longarc",
                "duration_s": measured["duration_s"],
            }}}))

            seen: dict = {}

            def fake_ffmpeg(_src, dst, _start, _end, **kwargs):
                dst.write_bytes(b"wav")

            def fake_bp(_wav, midi_path, tempo=120.0):
                seen.setdefault("tempos", []).append(tempo)
                Path(midi_path).write_bytes(b"mid")
                return {"note_count": 0}

            old_root = _ar.OUT_ROOT
            _ar.OUT_ROOT = root
            try:
                with mock.patch.object(
                    _ar, "ffmpeg_to_wav", side_effect=fake_ffmpeg
                ), mock.patch.object(
                    _ar, "run_basic_pitch", side_effect=fake_bp
                ):
                    result = _ar.clip_moments(
                        "longarc", modes=["midi", "features"],
                        index_path=index, max_moments=1)
            finally:
                _ar.OUT_ROOT = old_root
            self.assertTrue(result["children"])
            self.assertTrue(result["children"][0]["ok"])
            self.assertTrue(seen.get("tempos"))
            for tempo in seen["tempos"]:
                self.assertAlmostEqual(tempo, 142.0, delta=1.0)
                self.assertNotAlmostEqual(tempo, 120.0, delta=5.0)

    def test_no_measurement_falls_back_to_card_grid_then_guess(self):
        # Pure unit: the card grid carries the song when the parent was
        # never measured; the caller's guess is the last resort.
        grid_card = {
            "structure": {"grid_bpm": 100.0, "feel_bpm": None},
            "card": {"tempo": {"grid_bpm": 100.0, "feel_bpm": None}},
        }
        self.assertEqual(
            _ar._moment_transcribe_tempo(grid_card, {}, 120.0), 100.0)
        self.assertEqual(_ar._moment_transcribe_tempo({}, {}, 120.0), 120.0)
        # A half-time measurement resolves up to the card grid's octave.
        card144 = {
            "structure": {"grid_bpm": 144.0, "feel_bpm": 72.0},
            "card": {"tempo": {"grid_bpm": 144.0, "feel_bpm": 72.0}},
        }
        self.assertAlmostEqual(
            _ar._moment_transcribe_tempo(
                card144, {"measured": {"tempo_bpm": 71.9}}, 120.0),
            143.8, delta=0.2)


def _steer():
    return {
        "ref_id": "r-1", "identity": "Ref", "mods": {"density": "x"},
        "tempo": 120.0, "register": "mid", "unmapped": [],
        "drum_grid": None, "measured_drum_grid": None,
        "ricardo_note": None, "contract_version": "audio-ref-card-v2",
        "brief": {},
    }


_SEED = [
    {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90},
    {"pitch": 62, "start": 0.5, "duration": 0.5, "velocity": 90},
    {"pitch": 64, "start": 1.0, "duration": 1.0, "velocity": 90},
]


class TestR2HonestRefStamping(unittest.TestCase):
    def _run_answer(self, *, steered_ids, unsteered_ids, ownership="his"):
        fake_ar = mock.Mock()
        fake_ar.ref_steering.return_value = _steer()
        fake_ar.get_ref_card.return_value = {"ref_id": "r-1",
                                             "ownership": ownership}
        fake_ar.normalize_ownership.side_effect = _ar.normalize_ownership
        fake_ar.ref_brief.return_value = {}

        def _fake_core_answer(**kw):
            ids = steered_ids if kw.get("ref") is not None else unsteered_ids
            ans = {
                "answer_notes": list(_SEED), "tempo": 120.0,
                "source_phrase_ids": list(ids), "role": "lead",
                "assumptions": [],
            }
            if kw.get("ref") is not None:
                ans["ref"] = {"steering": "gates", "unmapped": [],
                              "identity": "Ref",
                              "contract_version": "audio-ref-card-v2",
                              "brief": {}}
            return ans

        stamped: dict = {}

        def _fake_write(target, **kw):
            stamped.update({k: kw.get(k) for k in ("source", "ref_prose")})
            return {"slot": "0/0"}

        with mock.patch.object(_server, "_audio_ref_mod",
                               return_value=fake_ar), mock.patch.object(
            _core, "answer_my_loop", side_effect=_fake_core_answer
        ), mock.patch.object(
            _core, "_fold_persisted_session_takes", side_effect=lambda c: c
        ), mock.patch("jam_lifecycle.write_answer_to_jam",
                      side_effect=_fake_write):
            out = _server.answer_my_loop(seed_notes=list(_SEED), seed=0,
                                         ref_id="r-1", write=True)
        return out, stamped

    def test_other_owned_ref_writes_and_stamps_honestly(self):
        # A reference never refuses an answer (CLAUDE.md): answer_my_loop
        # answers with his OWN phrases, and a ref of ANY ownership steers
        # which of them are picked — so the steered write lands on JAM
        # normally, and the ledger stamp stays honest about what drove
        # the pick. Ownership only gates apply_audio_ref(write=True),
        # which pastes the ref's own transcribed notes.
        for ownership in ("other", None):
            with self.subTest(ownership=ownership, pick="changed"):
                out, stamped = self._run_answer(
                    steered_ids=["p-steered"], unsteered_ids=["p-plain"],
                    ownership=ownership)
                self.assertTrue(out["ref"]["changed_pick"])
                self.assertNotIn("live_write_skipped", out)
                self.assertEqual(stamped["source"], "ref:r-1")
                self.assertIsNone(stamped["ref_prose"])
            with self.subTest(ownership=ownership, pick="unchanged"):
                out, stamped = self._run_answer(
                    steered_ids=["p-same"], unsteered_ids=["p-same"],
                    ownership=ownership)
                self.assertFalse(out["ref"]["changed_pick"])
                self.assertNotIn("live_write_skipped", out)
                self.assertEqual(stamped["source"], "catalogue")
                self.assertEqual(stamped["ref_prose"], "r-1")

    def test_beat_from_ref_any_ownership_steers_his_phrases(self):
        # Fix2 item 1: a reference never refuses an answer. Any ownership
        # (his/other/unknown/missing) steers retrieval from HIS drum
        # phrases via the ref's grid; the ref's own grid notes never land
        # as the beat — the returned notes come from his phrase.
        for ownership in ("his", "other", "unknown", None):
            card = _four_on_floor_card()
            if ownership is None:
                del card["ownership"]
            else:
                card["ownership"] = ownership
            seen: dict = {}

            def _fake_beat(grid_notes, **kw):
                # R2 runs a second unsteered retrieval (empty grid); the
                # steered call comes first, so keep it.
                if "grid_notes" not in seen:
                    seen["grid_notes"] = list(grid_notes or [])
                return {"notes": [{"pitch": 36, "start": 0.0}],
                        "tempo": 120.0,
                        "source_phrase_ids": ["drum|his|clip"],
                        "assumptions": []}

            with self.subTest(ownership=ownership):
                with mock.patch.object(
                    _server, "_audio_ref_mod", return_value=_ar
                ), mock.patch.object(
                    _ar, "get_ref_card", return_value=card
                ), mock.patch.object(
                    _core, "beat_from_grid", side_effect=_fake_beat
                ):
                    res = _server.beat_from_ref(
                        "r-four", bars=1, seed=0)
            # The ref's grid drove retrieval (four-on-floor kicks as query).
            kicks = sorted(
                n["start"] for n in seen.get("grid_notes", [])
                if n["pitch"] == 36)
            self.assertEqual(kicks, [0.0, 1.0, 2.0, 3.0])
            self.assertEqual(res["ref"]["grid_source"], "card_grid")
            # Provenance is his phrase, never the ref's own grid notes.
            self.assertEqual(res["source_phrase_ids"], ["drum|his|clip"])

    def test_changed_pick_stamps_ref(self):
        out, stamped = self._run_answer(steered_ids=["p-steered"],
                                        unsteered_ids=["p-plain"])
        self.assertTrue(out["ref"]["changed_pick"])
        self.assertEqual(stamped["source"], "ref:r-1")
        self.assertIsNone(stamped["ref_prose"])

    def test_unchanged_pick_stamps_catalogue(self):
        out, stamped = self._run_answer(steered_ids=["p-same"],
                                        unsteered_ids=["p-same"])
        self.assertFalse(out["ref"]["changed_pick"])
        self.assertEqual(stamped["source"], "catalogue")
        self.assertEqual(stamped["ref_prose"], "r-1")

    def test_beat_changed_and_unchanged(self):
        for steered_ids, unsteered_ids, want_source, want_prose, want_changed in (
            (["d-steered"], ["d-plain"], "ref:r-1", None, True),
            (["d-same"], ["d-same"], "catalogue", "r-1", False),
        ):
            fake_ar = mock.Mock()
            fake_ar.get_ref_card.return_value = {
                "ref_id": "r-1", "ownership": "his",
                "structure": {"density": "medium"}}
            fake_ar.normalize_ownership.side_effect = (
                _ar.normalize_ownership)
            fake_ar.ref_steering.return_value = _steer()
            fake_ar.measured_drum_grid_to_notes.return_value = ([], {})
            fake_ar.card_grid_to_notes.return_value = ([], False)
            fake_ar.drum_grid_to_notes.return_value = ([], {})

            def _fake_beat(grid_notes, **kw):
                ids = (steered_ids if kw.get("density") is not None
                       else unsteered_ids)
                return {"notes": [], "tempo": 120.0,
                        "source_phrase_ids": list(ids), "assumptions": []}

            stamped: dict = {}

            def _fake_write(target, **kw):
                stamped.update({k: kw.get(k) for k in ("source", "ref_prose")})
                return {"slot": "0/0"}

            with mock.patch.object(_server, "_audio_ref_mod",
                                   return_value=fake_ar), mock.patch.object(
                _core, "beat_from_grid", side_effect=_fake_beat
            ), mock.patch("jam_lifecycle.write_answer_to_jam",
                          side_effect=_fake_write):
                res = _server.beat_from_ref("r-1", seed=0, write=True)
            self.assertEqual(res["ref"]["changed_pick"], want_changed)
            self.assertEqual(stamped["source"], want_source)
            self.assertEqual(stamped["ref_prose"], want_prose)


def _r3_card(ref_id="r-j", **extra):
    card = {
        "ref_id": ref_id, "identity": "J %s" % ref_id, "title": "T %s" % ref_id,
        "describe_excerpt": "", "tags": [], "imitation_do": [], "source": "",
        "structure": {}, "verdict": "unset",
    }
    card.update(extra)
    return card


_R3_NOTES = [{"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 90}]
_R3_IDENTITY = {"set_id": "set-J", "runtime_epoch": "run-j",
                "file_path": "/tmp/j.als"}
_R3_CLIP = {"clip_id": "clip-j", "track_id": "track-j",
            "track": "SIDEKICK JAM", "surface": "session", "path": "3/0"}


class TestR3JudgmentLoopPerRef(unittest.TestCase):
    def test_tally_shelves_dropped_ref(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            idx = Path(td) / "index.json"
            orig_root = _ar.OUT_ROOT
            _ar.OUT_ROOT = Path(td) / "audio_refs"
            try:
                _ar.save_index({"version": 1, "refs": {
                    "r-j": _r3_card(), "r-k": _r3_card("r-k")}}, idx)
                self.assertEqual(
                    [c["ref_id"] for c in _ar.find_audio_refs(k=5, index_path=idx)],
                    ["r-j", "r-k"],
                )
                _ar.record_ref_judgment("r-j", "dropped", index_path=idx)
                # One drop only ranks down: still visible, below the clean ref.
                got = _ar.find_audio_refs(k=5, index_path=idx)
                self.assertEqual([c["ref_id"] for c in got], ["r-k", "r-j"])
                _ar.record_ref_judgment("r-j", "dropped", index_path=idx)
                # Deliberate change (review fix): two drops shelve the ref
                # for ANSWERS, but search keeps matching it — the tally
                # only ranks it down, last.
                self.assertEqual(
                    [c["ref_id"] for c in _ar.find_audio_refs(k=5, index_path=idx)],
                    ["r-k", "r-j"],
                )
                with self.assertRaises(ValueError):
                    _ar.record_ref_judgment("r-j", "meh", index_path=idx)
                with self.assertRaises(KeyError):
                    _ar.record_ref_judgment("r-ghost", "kept", index_path=idx)
            finally:
                _ar.OUT_ROOT = orig_root

    def test_judge_answer_bumps_tally(self):
        import tempfile

        from mcp_server import gears, ledger
        import mcp_server.develop_session as develop_session
        import mcp_server.ears as ears

        with tempfile.TemporaryDirectory() as td:
            import os

            ledger_path = os.path.join(td, "ledger.json")
            ratings_path = os.path.join(td, "ratings.json")
            ears_path = os.path.join(td, "ears.json")
            develop_path = os.path.join(td, "develop.json")
            idx_path = os.path.join(td, "index.json")
            from pathlib import Path

            _ar.save_index({"version": 1, "refs": {"r-j": _r3_card()}},
                           Path(idx_path))
            entry = ledger.record_write(
                clip_name="refjam", slot="3/0", source="ref:r-j",
                role="lead", answer_notes=_R3_NOTES, path=ledger_path)
            orig_ledger = ledger.LEDGER_PATH
            orig_ratings = gears.core._ratings_path()
            orig_ears = ears.SESSION_PATH
            orig_develop = develop_session.STATE_PATH
            orig_index = _ar.INDEX_PATH
            orig_root = _ar.OUT_ROOT
            try:
                _ar.OUT_ROOT = Path(td) / "audio_refs"
                ledger.LEDGER_PATH = ledger_path
                gears.core.set_ratings_path(ratings_path)
                ears.SESSION_PATH = ears_path
                develop_session.STATE_PATH = develop_path
                _ar.INDEX_PATH = Path(idx_path)
                with mock.patch.object(gears.core, "_phrases",
                                       return_value=[]), mock.patch.object(
                    ears, "record_verdict", return_value=[]
                ), mock.patch.object(
                    develop_session, "load_state", return_value={}
                ), mock.patch.object(
                    develop_session, "record_drop", return_value={}
                ), mock.patch.object(
                    gears.core, "rate",
                    return_value={"record": {"id": 1}}):
                    res = _server.judge_answer("drop", entry_id=entry["id"],
                                               confirm=False)
                self.assertEqual(res["ref_tally"]["tally"], {
                    "kept": 0, "dropped": 1, "survived": 0})
                self.assertEqual(
                    _ar.ref_tally_of(
                        _ar.load_index(Path(idx_path))["refs"]["r-j"]),
                    {"kept": 0, "dropped": 1, "survived": 0})
                # Catalogue rows never touch the ref index.
                entry2 = ledger.record_write(
                    clip_name="plain", slot="3/1", source="catalogue",
                    role="lead", answer_notes=_R3_NOTES, path=ledger_path)
                with mock.patch.object(gears.core, "_phrases",
                                       return_value=[]), mock.patch.object(
                    ears, "record_verdict", return_value=[]
                ), mock.patch.object(
                    develop_session, "load_state", return_value={}
                ), mock.patch.object(
                    develop_session, "record_drop", return_value={}
                ), mock.patch.object(
                    gears.core, "rate",
                    return_value={"record": {"id": 2}}):
                    res2 = _server.judge_answer("keep", entry_id=entry2["id"],
                                                confirm=False)
                self.assertNotIn("ref_tally", res2)
            finally:
                ledger.LEDGER_PATH = orig_ledger
                _ar.OUT_ROOT = orig_root
                gears.core.set_ratings_path(orig_ratings)
                ears.SESSION_PATH = orig_ears
                develop_session.STATE_PATH = orig_develop
                _ar.INDEX_PATH = orig_index

    def test_close_session_bumps_survived(self):
        import tempfile

        from mcp_server import ledger, live_write

        with tempfile.TemporaryDirectory() as td:
            import os

            ledger_path = os.path.join(td, "ledger.json")
            idx_path = os.path.join(td, "index.json")
            from pathlib import Path

            _ar.save_index({"version": 1, "refs": {"r-j": _r3_card()}},
                           Path(idx_path))
            wrote = ledger.record_write(
                clip_name="refjam", slot="3/0", source="ref:r-j",
                role="lead", set_identity=dict(_R3_IDENTITY),
                clip_ref=dict(_R3_CLIP), answer_notes=_R3_NOTES,
                path=ledger_path)
            # A kept clip that is still in the Set survives the sweep.
            ledger.record_verdict("keep", entry_id=wrote["id"],
                                  path=ledger_path)

            async def _inv():
                return {"set_identity": dict(_R3_IDENTITY), "complete": True,
                        "clips": [dict(_R3_CLIP)], "errors": []}

            orig_ledger = ledger.LEDGER_PATH
            orig_index = _ar.INDEX_PATH
            orig_root = _ar.OUT_ROOT
            try:
                _ar.OUT_ROOT = Path(td) / "audio_refs"
                ledger.LEDGER_PATH = ledger_path
                _ar.INDEX_PATH = Path(idx_path)
                with mock.patch.object(live_write, "read_set_inventory",
                                       side_effect=_inv):
                    res = _server.close_session()
                self.assertTrue(res.get("ok"))
                tallies = {t["ref_id"]: t for t in res.get("ref_tallies", [])}
                self.assertEqual(tallies["r-j"]["tally"], {
                    "kept": 0, "dropped": 0, "survived": 1})
                # A cached re-close replays the report without double counting.
                with mock.patch.object(live_write, "read_set_inventory",
                                       side_effect=_inv):
                    res2 = _server.close_session()
                self.assertNotIn("ref_tallies", res2)
                self.assertEqual(
                    _ar.ref_tally_of(
                        _ar.load_index(Path(idx_path))["refs"]["r-j"]),
                    {"kept": 0, "dropped": 0, "survived": 1})
            finally:
                ledger.LEDGER_PATH = orig_ledger
                _ar.OUT_ROOT = orig_root
                _ar.INDEX_PATH = orig_index

    def test_entry_id_dedups_record_ref_judgment(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            idx = Path(td) / "index.json"
            orig_root = _ar.OUT_ROOT
            _ar.OUT_ROOT = Path(td) / "audio_refs"
            try:
                _ar.save_index({"version": 1, "refs": {"r-j": _r3_card()}},
                               idx)
                _ar.record_ref_judgment("r-j", "dropped", index_path=idx,
                                        entry_id="e-1")
                _ar.record_ref_judgment("r-j", "dropped", index_path=idx,
                                        entry_id="e-1")
                card = _ar.load_index(idx)["refs"]["r-j"]
                self.assertEqual(_ar.ref_tally_of(card),
                                 {"kept": 0, "dropped": 1, "survived": 0})
                self.assertEqual(_ar.ref_tallied_entries_of(card),
                                 {"e-1": "dropped"})
                # Calls without an entry id keep the legacy always-bump path.
                _ar.record_ref_judgment("r-j", "dropped", index_path=idx)
                card = _ar.load_index(idx)["refs"]["r-j"]
                self.assertEqual(_ar.ref_tally_of(card),
                                 {"kept": 0, "dropped": 2, "survived": 0})
            finally:
                _ar.OUT_ROOT = orig_root

    def test_judge_then_sweep_counts_once(self):
        import tempfile

        from mcp_server import gears, ledger, live_write
        import mcp_server.develop_session as develop_session
        import mcp_server.ears as ears

        with tempfile.TemporaryDirectory() as td:
            import os

            ledger_path = os.path.join(td, "ledger.json")
            ratings_path = os.path.join(td, "ratings.json")
            ears_path = os.path.join(td, "ears.json")
            develop_path = os.path.join(td, "develop.json")
            idx_path = os.path.join(td, "index.json")
            from pathlib import Path

            _ar.save_index({"version": 1, "refs": {"r-j": _r3_card()}},
                           Path(idx_path))
            entry = ledger.record_write(
                clip_name="refjam", slot="3/0", source="ref:r-j",
                role="lead", set_identity=dict(_R3_IDENTITY),
                clip_ref=dict(_R3_CLIP), answer_notes=_R3_NOTES,
                path=ledger_path)
            orig_ledger = ledger.LEDGER_PATH
            orig_ratings = gears.core._ratings_path()
            orig_ears = ears.SESSION_PATH
            orig_develop = develop_session.STATE_PATH
            orig_index = _ar.INDEX_PATH
            orig_root = _ar.OUT_ROOT
            try:
                _ar.OUT_ROOT = Path(td) / "audio_refs"
                ledger.LEDGER_PATH = ledger_path
                gears.core.set_ratings_path(ratings_path)
                ears.SESSION_PATH = ears_path
                develop_session.STATE_PATH = develop_path
                _ar.INDEX_PATH = Path(idx_path)

                def _judge():
                    with mock.patch.object(gears.core, "_phrases",
                                           return_value=[]), mock.patch.object(
                        ears, "record_verdict", return_value=[]
                    ), mock.patch.object(
                        develop_session, "load_state", return_value={}
                    ), mock.patch.object(
                        develop_session, "record_drop", return_value={}
                    ), mock.patch.object(
                        gears.core, "rate",
                        return_value={"record": {"id": 1}}):
                        return _server.judge_answer(
                            "drop", entry_id=entry["id"], confirm=False)

                res = _judge()
                self.assertEqual(res["ref_tally"]["tally"],
                                 {"kept": 0, "dropped": 1, "survived": 0})

                async def _inv_absent():
                    return {"set_identity": dict(_R3_IDENTITY),
                            "complete": True, "clips": [], "errors": []}

                with mock.patch.object(live_write, "read_set_inventory",
                                       side_effect=_inv_absent):
                    swept = _server.close_session()
                self.assertTrue(swept.get("ok"))
                # The sweep saw the same row settle terminally but the judge
                # path already counted it: still exactly one drop.
                card = _ar.load_index(Path(idx_path))["refs"]["r-j"]
                self.assertEqual(_ar.ref_tally_of(card),
                                 {"kept": 0, "dropped": 1, "survived": 0})
                self.assertEqual(_ar.ref_tallied_entries_of(card),
                                 {entry["id"]: "dropped"})
            finally:
                ledger.LEDGER_PATH = orig_ledger
                _ar.OUT_ROOT = orig_root
                gears.core.set_ratings_path(orig_ratings)
                ears.SESSION_PATH = orig_ears
                develop_session.STATE_PATH = orig_develop
                _ar.INDEX_PATH = orig_index

    def test_repeated_partial_close_counts_nothing_then_once(self):
        import tempfile

        from mcp_server import ledger, live_write

        with tempfile.TemporaryDirectory() as td:
            import os

            ledger_path = os.path.join(td, "ledger.json")
            idx_path = os.path.join(td, "index.json")
            from pathlib import Path

            _ar.save_index({"version": 1, "refs": {"r-j": _r3_card()}},
                           Path(idx_path))
            wrote = ledger.record_write(
                clip_name="refjam", slot="3/0", source="ref:r-j",
                role="lead", set_identity=dict(_R3_IDENTITY),
                clip_ref=dict(_R3_CLIP), answer_notes=_R3_NOTES,
                path=ledger_path)
            ledger.record_verdict("keep", entry_id=wrote["id"],
                                  path=ledger_path)

            async def _inv_partial():
                return {"set_identity": dict(_R3_IDENTITY),
                        "complete": False, "clips": [dict(_R3_CLIP)],
                        "errors": []}

            async def _inv_full():
                return {"set_identity": dict(_R3_IDENTITY), "complete": True,
                        "clips": [dict(_R3_CLIP)], "errors": []}

            orig_ledger = ledger.LEDGER_PATH
            orig_index = _ar.INDEX_PATH
            orig_root = _ar.OUT_ROOT
            try:
                _ar.OUT_ROOT = Path(td) / "audio_refs"
                ledger.LEDGER_PATH = ledger_path
                _ar.INDEX_PATH = Path(idx_path)

                def _close(inv):
                    with mock.patch.object(live_write, "read_set_inventory",
                                           side_effect=inv):
                        return _server.close_session()

                for _ in range(2):
                    partial = _close(_inv_partial)
                    self.assertTrue(partial.get("ok"))
                    self.assertNotIn("ref_tallies", partial)
                card = _ar.load_index(Path(idx_path))["refs"]["r-j"]
                self.assertEqual(_ar.ref_tally_of(card),
                                 {"kept": 0, "dropped": 0, "survived": 0})
                full = _close(_inv_full)
                self.assertTrue(full.get("ok"))
                tallies = {t["ref_id"]: t
                           for t in full.get("ref_tallies", [])}
                self.assertEqual(tallies["r-j"]["tally"],
                                 {"kept": 0, "dropped": 0, "survived": 1})
                # A cached re-close replays without counting again.
                again = _close(_inv_full)
                self.assertNotIn("ref_tallies", again)
                card = _ar.load_index(Path(idx_path))["refs"]["r-j"]
                self.assertEqual(_ar.ref_tally_of(card),
                                 {"kept": 0, "dropped": 0, "survived": 1})
                self.assertEqual(_ar.ref_tallied_entries_of(card),
                                 {wrote["id"]: "survived"})
            finally:
                ledger.LEDGER_PATH = orig_ledger
                _ar.OUT_ROOT = orig_root
                _ar.INDEX_PATH = orig_index

    def test_judge_retry_after_accounting_failure_counts_once(self):
        import tempfile

        from mcp_server import gears, ledger
        import mcp_server.develop_session as develop_session
        import mcp_server.ears as ears

        with tempfile.TemporaryDirectory() as td:
            import os

            ledger_path = os.path.join(td, "ledger.json")
            ratings_path = os.path.join(td, "ratings.json")
            ears_path = os.path.join(td, "ears.json")
            develop_path = os.path.join(td, "develop.json")
            idx_path = os.path.join(td, "index.json")
            from pathlib import Path

            _ar.save_index({"version": 1, "refs": {"r-j": _r3_card()}},
                           Path(idx_path))
            entry = ledger.record_write(
                clip_name="refjam", slot="3/0", source="ref:r-j",
                role="lead", answer_notes=_R3_NOTES, path=ledger_path)
            real_record_verdict = ledger.record_verdict
            calls: list = []
            fail_all = {"on": True}

            def _flaky(verdict, **kw):
                calls.append(kw.get("entry_id"))
                if fail_all["on"]:
                    raise RuntimeError("boom")
                return real_record_verdict(verdict, **kw)

            orig_ledger = ledger.LEDGER_PATH
            orig_ratings = gears.core._ratings_path()
            orig_ears = ears.SESSION_PATH
            orig_develop = develop_session.STATE_PATH
            orig_index = _ar.INDEX_PATH
            orig_root = _ar.OUT_ROOT
            try:
                _ar.OUT_ROOT = Path(td) / "audio_refs"
                ledger.LEDGER_PATH = ledger_path
                gears.core.set_ratings_path(ratings_path)
                ears.SESSION_PATH = ears_path
                develop_session.STATE_PATH = develop_path
                _ar.INDEX_PATH = Path(idx_path)

                def _judge():
                    with mock.patch.object(gears.core, "_phrases",
                                           return_value=[]), mock.patch.object(
                        ears, "record_verdict", return_value=[]
                    ), mock.patch.object(
                        develop_session, "load_state", return_value={}
                    ), mock.patch.object(
                        develop_session, "record_drop", return_value={}
                    ), mock.patch.object(
                        gears.core, "rate",
                        return_value={"record": {"id": 1}}), mock.patch.object(
                        ledger, "record_verdict", side_effect=_flaky):
                        return _server.judge_answer(
                            "drop", entry_id=entry["id"], confirm=False)

                # Verdict-first retries a one-shot stamp failure in the same
                # call, so the failure is sustained to reach the retryable
                # accounting path with no tally.
                failed = _judge()
                self.assertNotIn("ref_tally", failed)
                self.assertEqual(
                    failed.get("accounting_failure", {}).get("status"),
                    "retryable")
                card = _ar.load_index(Path(idx_path))["refs"]["r-j"]
                self.assertEqual(_ar.ref_tally_of(card),
                                 {"kept": 0, "dropped": 0, "survived": 0})
                # Recovery: the retry lands the verdict, counted once.
                fail_all["on"] = False
                landed = _judge()
                self.assertEqual(landed["ref_tally"]["tally"],
                                 {"kept": 0, "dropped": 1, "survived": 0})
                # A same-verdict repeat is a no-op for the tally.
                _judge()
                card = _ar.load_index(Path(idx_path))["refs"]["r-j"]
                self.assertEqual(_ar.ref_tally_of(card),
                                 {"kept": 0, "dropped": 1, "survived": 0})
                self.assertEqual(_ar.ref_tallied_entries_of(card),
                                 {entry["id"]: "dropped"})
            finally:
                ledger.LEDGER_PATH = orig_ledger
                _ar.OUT_ROOT = orig_root
                gears.core.set_ratings_path(orig_ratings)
                ears.SESSION_PATH = orig_ears
                develop_session.STATE_PATH = orig_develop
                _ar.INDEX_PATH = orig_index

    def test_judge_transient_stamp_failure_tallies_once(self):
        # Verdict-first: a one-shot record_verdict failure is retried by
        # record_judgment inside the same call, so the verdict lands
        # immediately and the per-ref tally counts it exactly once.
        import tempfile

        from mcp_server import gears, ledger
        import mcp_server.develop_session as develop_session
        import mcp_server.ears as ears

        with tempfile.TemporaryDirectory() as td:
            import os

            ledger_path = os.path.join(td, "ledger.json")
            ratings_path = os.path.join(td, "ratings.json")
            ears_path = os.path.join(td, "ears.json")
            develop_path = os.path.join(td, "develop.json")
            idx_path = os.path.join(td, "index.json")
            from pathlib import Path

            _ar.save_index({"version": 1, "refs": {"r-j": _r3_card()}},
                           Path(idx_path))
            entry = ledger.record_write(
                clip_name="refjam", slot="3/0", source="ref:r-j",
                role="lead", answer_notes=_R3_NOTES, path=ledger_path)
            real_record_verdict = ledger.record_verdict
            attempts: list = []

            def _fail_once(verdict, **kw):
                attempts.append(kw.get("entry_id"))
                if len(attempts) == 1:
                    raise RuntimeError("boom")
                return real_record_verdict(verdict, **kw)

            orig_ledger = ledger.LEDGER_PATH
            orig_ratings = gears.core._ratings_path()
            orig_ears = ears.SESSION_PATH
            orig_develop = develop_session.STATE_PATH
            orig_index = _ar.INDEX_PATH
            orig_root = _ar.OUT_ROOT
            try:
                _ar.OUT_ROOT = Path(td) / "audio_refs"
                ledger.LEDGER_PATH = ledger_path
                gears.core.set_ratings_path(ratings_path)
                ears.SESSION_PATH = ears_path
                develop_session.STATE_PATH = develop_path
                _ar.INDEX_PATH = Path(idx_path)

                def _judge():
                    with mock.patch.object(gears.core, "_phrases",
                                           return_value=[]), mock.patch.object(
                        ears, "record_verdict", return_value=[]
                    ), mock.patch.object(
                        develop_session, "load_state", return_value={}
                    ), mock.patch.object(
                        develop_session, "record_drop", return_value={}
                    ), mock.patch.object(
                        gears.core, "rate",
                        return_value={"record": {"id": 1}}), mock.patch.object(
                        ledger, "record_verdict", side_effect=_fail_once):
                        return _server.judge_answer(
                            "drop", entry_id=entry["id"], confirm=False)

                res = _judge()
                self.assertNotIn("accounting_failure", res)
                self.assertEqual(res["ref_tally"]["tally"],
                                 {"kept": 0, "dropped": 1, "survived": 0})
                # A same-verdict repeat is a no-op for the tally.
                _judge()
                card = _ar.load_index(Path(idx_path))["refs"]["r-j"]
                self.assertEqual(_ar.ref_tally_of(card),
                                 {"kept": 0, "dropped": 1, "survived": 0})
                self.assertEqual(_ar.ref_tallied_entries_of(card),
                                 {entry["id"]: "dropped"})
            finally:
                ledger.LEDGER_PATH = orig_ledger
                _ar.OUT_ROOT = orig_root
                gears.core.set_ratings_path(orig_ratings)
                ears.SESSION_PATH = orig_ears
                develop_session.STATE_PATH = orig_develop
                _ar.INDEX_PATH = orig_index

    def test_attest_settles_ref_row_once(self):
        import tempfile

        from mcp_server import ledger

        with tempfile.TemporaryDirectory() as td:
            import os

            ledger_path = os.path.join(td, "ledger.json")
            idx_path = os.path.join(td, "index.json")
            from pathlib import Path

            _ar.save_index({"version": 1, "refs": {"r-j": _r3_card()}},
                           Path(idx_path))
            # Identity-less night: no sweep can score it, only attestation.
            entry = ledger.record_write(
                clip_name="refjam", slot="3/0", source="ref:r-j",
                role="lead", answer_notes=_R3_NOTES, path=ledger_path)
            ledger.record_verdict("drop", entry_id=entry["id"],
                                  path=ledger_path)
            orig_ledger = ledger.LEDGER_PATH
            orig_index = _ar.INDEX_PATH
            orig_root = _ar.OUT_ROOT
            try:
                _ar.OUT_ROOT = Path(td) / "audio_refs"
                ledger.LEDGER_PATH = ledger_path
                _ar.INDEX_PATH = Path(idx_path)
                # A preview writes nothing and tallies nothing.
                preview = _server.close_session(
                    session_id=entry["session_id"],
                    attest={entry["id"]: "absent"}, confirm=False)
                self.assertNotIn("ref_tallies", preview)
                card = _ar.load_index(Path(idx_path))["refs"]["r-j"]
                self.assertEqual(_ar.ref_tally_of(card),
                                 {"kept": 0, "dropped": 0, "survived": 0})
                settled = _server.close_session(
                    session_id=entry["session_id"],
                    attest={entry["id"]: "absent"}, confirm=True)
                tallies = {t["ref_id"]: t
                           for t in settled.get("ref_tallies", [])}
                self.assertEqual(tallies["r-j"]["tally"],
                                 {"kept": 0, "dropped": 1, "survived": 0})
                # Re-sending the identical attestation settles nothing new.
                settled2 = _server.close_session(
                    session_id=entry["session_id"],
                    attest={entry["id"]: "absent"}, confirm=True)
                card = _ar.load_index(Path(idx_path))["refs"]["r-j"]
                self.assertEqual(_ar.ref_tally_of(card),
                                 {"kept": 0, "dropped": 1, "survived": 0})
                self.assertEqual(_ar.ref_tallied_entries_of(card),
                                 {entry["id"]: "dropped"})
            finally:
                ledger.LEDGER_PATH = orig_ledger
                _ar.OUT_ROOT = orig_root
                _ar.INDEX_PATH = orig_index


class TestR4MomentSlicesExcludedByDefault(unittest.TestCase):
    def test_child_hidden_unless_explicit(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            idx = Path(td) / "index.json"
            parent = _r3_card("r-parent")
            child = _r3_card("r-parent_m1_20260101-000000")
            child["parent_ref_id"] = "r-parent"
            child["describe_excerpt"] = "stale v1 words about the slice"
            _ar.save_index({"version": 1,
                            "refs": {"r-parent": parent,
                                     child["ref_id"]: child}}, idx)
            default = [c["ref_id"] for c in _ar.find_audio_refs(
                k=5, index_path=idx)]
            self.assertEqual(default, ["r-parent"])
            # Explicit moments still collapse by default: one track, one
            # row. The slice itself stays reachable by direct ref_id
            # (get_ref_card/steering).
            # Deliberate change (review fix): include_moments shows every
            # row instead of collapsing.
            explicit = [c["ref_id"] for c in _ar.find_audio_refs(
                k=5, index_path=idx, include_moments=True)]
            self.assertEqual(explicit,
                             ["r-parent", "r-parent_m1_20260101-000000"])
            self.assertEqual(
                _ar.get_ref_card(child["ref_id"],
                                 index_path=idx)["ref_id"], child["ref_id"])


def _r5_card(ref_id, *, title, identity="", ownership="other", struct=None,
              excerpt="", dos=(), note=None, sections=(), uncertain=(),
              parent=None):
    card = _r3_card(ref_id)
    card.update({
        "title": title, "identity": identity or title, "ownership": ownership,
        "structure": dict(struct or {}), "describe_excerpt": excerpt,
        "imitation_do": list(dos),
        "sections": [{"t": "00:10", "label": label} for label in sections],
        "uncertain": list(uncertain),
    })
    if note is not None:
        card["ricardo_note"] = note
    if parent is not None:
        card["parent_ref_id"] = parent
    return card


class TestR5FindPrecision(unittest.TestCase):
    def _index(self, td, cards):
        from pathlib import Path

        idx = Path(td) / "index.json"
        _ar.save_index({"version": 1, "refs": cards}, idx)
        return idx

    def test_key_and_word_queries_are_precise(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            idx = self._index(td, {
                "r-doom": _r5_card(
                    "r-doom", title="Doom Loop",
                    struct={"modal_center": "D minor", "grid_bpm": 90.0,
                            "density": "low", "register": "low"},
                    excerpt="slow heavy groove"),
                "r-dmaj": _r5_card(
                    "r-dmaj", title="D Major Lift",
                    struct={"modal_center": "D major", "grid_bpm": 100.0,
                            "density": "medium", "register": "mid"},
                    excerpt="bright turnaround"),
                "r-moto": _r5_card(
                    "r-moto", title="Motorik",
                    struct={"modal_center": "C major", "grid_bpm": 120.0,
                            "density": "high", "register": "mid"},
                    excerpt="kick on 1 driving eighth-note pulse"),
                "r-hiss": _r5_card(
                    "r-hiss", title="Field Hiss",
                    excerpt="a synthesizer that drains slowly"),
                "r-drone": _r5_card(
                    "r-drone", title="Static Field",
                    note="static drone texture"),
                "r-pad": _r5_card(
                    "r-pad", title="Pad Wash",
                    excerpt="a droning synth pad that never lands"),
            })
            self.assertEqual(
                [c["ref_id"] for c in _ar.find_audio_refs(
                    query="D minor", k=10, index_path=idx)],
                ["r-doom", "r-dmaj"],
            )
            self.assertEqual(
                [c["ref_id"] for c in _ar.find_audio_refs(
                    query="Dm", k=10, index_path=idx)],
                ["r-doom", "r-dmaj"],
            )
            self.assertEqual(
                [c["ref_id"] for c in _ar.find_audio_refs(
                    query="drone", k=10, index_path=idx)],
                ["r-drone"],
            )

    def test_title_outranks_excerpt(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            idx = self._index(td, {
                "r-title": _r5_card("r-title", title="Drone Psalm"),
                "r-note": _r5_card("r-note", title="Unrelated",
                                   note="has a drone section"),
            })
            got = [c["ref_id"] for c in _ar.find_audio_refs(
                query="drone", k=10, index_path=idx)]
            self.assertEqual(got, ["r-title", "r-note"])

    def test_children_collapse_into_parent_once(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            idx = self._index(td, {
                "r-ap": _r5_card("r-ap", title="Author & Punisher live set"),
                "r-ap_m1_20260101-000000": _r5_card(
                    "r-ap_m1_20260101-000000",
                    title="Author & Punisher slice one", parent="r-ap"),
                "r-ap_m2_20260101-000000": _r5_card(
                    "r-ap_m2_20260101-000000",
                    title="Author & Punisher slice two", parent="r-ap"),
            })
            self.assertEqual(
                [c["ref_id"] for c in _ar.find_audio_refs(
                    query="Author & Punisher", k=10, index_path=idx)],
                ["r-ap"],
            )
            # Review fix round 1, item 10 refines this: with
            # include_moments a group still collapses when its parent
            # matched the query — slices show only when the parent did
            # not match.
            self.assertEqual(
                [c["ref_id"] for c in _ar.find_audio_refs(
                    query="Author & Punisher", k=10, index_path=idx,
                    include_moments=True)],
                ["r-ap"],
            )
            self.assertEqual(
                [c["ref_id"] for c in _ar.find_audio_refs(
                    query="slice", k=10, index_path=idx,
                    include_moments=True)],
                ["r-ap_m1_20260101-000000",
                 "r-ap_m2_20260101-000000"],
            )

    def test_orphan_moments_collapse_into_parent(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            idx = self._index(td, {
                "r-ap": _r5_card("r-ap", title="Author & Punisher live set"),
                # No explicit parent_ref_id: legacy orphans resolve by id.
                "r-ap_m1_20260101-000000": _r5_card(
                    "r-ap_m1_20260101-000000",
                    title="Author & Punisher slice one"),
                "r-ap_m2_20260101-000000": _r5_card(
                    "r-ap_m2_20260101-000000",
                    title="Author & Punisher slice two"),
            })
            self.assertEqual(
                [c["ref_id"] for c in _ar.find_audio_refs(
                    query="Author & Punisher", k=10, index_path=idx)],
                ["r-ap"],
            )
            # Item 10: the parent matched, so the group collapses even
            # with moments requested; a slice-only query shows the
            # orphans standing alone.
            self.assertEqual(
                [c["ref_id"] for c in _ar.find_audio_refs(
                    query="Author & Punisher", k=10, index_path=idx,
                    include_moments=True)],
                ["r-ap"],
            )
            self.assertEqual(
                [c["ref_id"] for c in _ar.find_audio_refs(
                    query="slice", k=10, index_path=idx,
                    include_moments=True)],
                ["r-ap_m1_20260101-000000",
                 "r-ap_m2_20260101-000000"],
            )

    def test_collapse_serves_parent_own_score(self):
        # Item 10: a collapsing group emits the parent's own row scored
        # with the parent's own points — never the max child score, and
        # never the moment's row.
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            idx = self._index(td, {
                "r-p": _r5_card("r-p", title="Doom Loop"),
                "r-p_m1_20260101-000000": _r5_card(
                    "r-p_m1_20260101-000000", title="Doom slice",
                    excerpt="doom heavy", note="doom drone"),
                "r-r": _r5_card("r-r", title="Doom Gloom",
                                excerpt="doom haze"),
            })
            got = _ar.find_audio_refs(query="doom", k=10, index_path=idx,
                                      detail=True)
            # Parent own 4 < rival 5 < orphan 7: the group ranks by the
            # parent's 4, below the rival — but the row is the parent.
            self.assertEqual([c["ref_id"] for c in got], ["r-r", "r-p"])
            self.assertNotIn("parent_ref_id", got[1])

    def test_key_phrase_matches_modal_center(self):
        self.assertEqual(
            _ar._find_key_phrase("D minor"),
            ((2, "minor"), {"d", "minor"}))
        self.assertEqual(
            _ar._find_key_phrase("in Bb major please"),
            ((10, "major"), {"b", "bb", "major"}))
        self.assertEqual(
            _ar._find_key_phrase("Dm")[0], (2, "minor"))
        self.assertEqual(
            _ar._find_key_phrase("F#m")[0], (6, "minor"))
        # English "am" is not A minor; plain words are not keys.
        self.assertEqual(_ar._find_key_phrase("I am sad"), (None, set()))
        self.assertEqual(_ar._find_key_phrase("drone"), (None, set()))
        self.assertEqual(_ar._find_key_phrase("minor"), (None, set()))

    def test_ownership_filter(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            idx = self._index(td, {
                "r-his": _r5_card("r-his", title="Natureza riff",
                                  ownership="his"),
                "r-other": _r5_card("r-other", title="Alien riff",
                                    ownership="other"),
            })
            self.assertEqual(
                [c["ref_id"] for c in _ar.find_audio_refs(
                    k=10, index_path=idx, ownership="his")],
                ["r-his"],
            )
            self.assertEqual(
                [c["ref_id"] for c in _ar.find_audio_refs(
                    k=10, index_path=idx, ownership="other")],
                ["r-other"],
            )
            with self.assertRaises(ValueError):
                _ar.find_audio_refs(k=10, index_path=idx, ownership="theirs")

    def test_compact_view_by_default_detail_on_request(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            idx = self._index(td, {
                "r-full": _r5_card(
                    "r-full", title="Full Card", ownership="his",
                    struct={"grid_bpm": 100.0, "density": "medium",
                            "register": "mid"},
                    dos=["one", "two", "three", "four", "five"],
                    sections=["drop", "swell"], uncertain=["tempo"]),
            })
            (compact,) = _ar.find_audio_refs(k=5, index_path=idx)
            self.assertEqual(
                sorted(compact),
                sorted(["ref_id", "title", "ownership", "identity",
                        "ricardo_note", "dos",
                        "tempo", "density", "register", "sections",
                        "uncertain", "suspect", "suspect_reason",
                        "listen_verified", "has_measured",
                        "contract_version"]),
            )
            self.assertFalse(compact["suspect"])
            # B3: cards predating the flag read as unverified, never unknown.
            self.assertFalse(compact["listen_verified"])
            self.assertIsNone(compact["suspect_reason"])
            self.assertEqual(compact["ref_id"], "r-full")
            self.assertIsNone(compact["ricardo_note"])
            self.assertEqual(compact["dos"], ["one", "two", "three"])
            self.assertEqual(compact["tempo"], 100.0)
            self.assertEqual(compact["sections"], ["drop", "swell"])
            (full,) = _ar.find_audio_refs(k=5, index_path=idx, detail=True)
            self.assertEqual(full["ref_id"], "r-full")
            self.assertEqual(len(full["imitation_do"]), 5)


class TestR6BriefReframing(unittest.TestCase):
    def test_key_timbre_rules_rewritten_others_verbatim(self):
        card = _r3_card("r-b")
        card.update({
            "imitation_do": ["Stay in D minor throughout",
                             "Use a distorted guitar swell",
                             "Leave space in bar 4"],
            "imitation_dont": ["Modulate to F# major for lift"],
        })
        brief = _ar.ref_brief(card)
        centre, timbre, plain = brief["imitation_do"]
        self.assertIn("ref's centre is D minor", centre)
        self.assertIn("your loop stays in its own key", centre)
        # The rewrite states the centre as information: the original
        # command is never quoted (the LLM would obey the quote).
        self.assertNotIn("Stay in D minor", centre)
        self.assertNotIn("(from:", centre)
        self.assertTrue(timbre.startswith("sound design, not notes:"))
        self.assertIn("guitar", timbre)
        self.assertEqual(plain, "Leave space in bar 4")
        (dont,) = brief["imitation_dont"]
        self.assertIn("ref's centre is F# major", dont)
        self.assertIn("your loop stays in its own key", dont)
        self.assertNotIn("(from:", dont)

    def test_centre_word_pitch_and_progression_rewritten(self):
        cases = {
            "Keep the F drone static throughout":
                "ref's centre is F;",
            "Do not modulate away from the tonal center of C":
                "ref's centre is C;",
            "anchor with a deep sub-bass drone (Bb) while upper voices move":
                "ref's centre is Bb;",
            "Alternate harmonic centers between E and D every four bars":
                "ref's centre is E, D;",
            "Anchor the tonality around D with distorted acid basslines":
                "ref's centre is D;",
            "Keep bass root static on C with short moves to D# and F":
                "ref's centre is C;",
            "Write a 4-bar Dm \u2013 Bb \u2013 F \u2013 C chord loop":
                "ref's centre is Dm \u2013 Bb \u2013 F \u2013 C;",
        }
        for rule, start in cases.items():
            with self.subTest(rule=rule):
                out = _ar._reframe_rule(rule)
                self.assertTrue(out.startswith(start), out)
                self.assertIn("your loop stays in its own key", out)
                self.assertNotIn("(from:", out)
                self.assertNotIn(rule, out)
        # No pitch content, no command: verbatim survivors.
        for rule in (
                "Do not use a standard backbeat snare or clap on beats 2 "
                "and 4.",
                "Anchor the song around a dramatic contrast between quiet "
                "spoken balladry and noise.",
                "Anchor the track in a 74 BPM tribal rhythm.",
                "change root chord harmony \u2014 keep a single static "
                "modal drone."):
            with self.subTest(rule=rule):
                self.assertEqual(_ar._reframe_rule(rule), rule)

    def test_arrangement_rule_not_marked_sound_design(self):
        rule = ("construct a short miniature arc where density builds to a "
                "peak at ~01:17 before collapsing into ambient reverb "
                "decay.")
        self.assertEqual(_ar._reframe_rule(rule), rule)
        # A rig choice still marks.
        rig = _ar._reframe_rule("drench the snare in plate reverb")
        self.assertTrue(rig.startswith("sound design, not notes:"))

    def test_compact_dos_reframed(self):
        card = _r3_card("r-c")
        card.update({
            "imitation_do": ["Stay in D minor throughout",
                             "Leave space in bar 4"],
        })
        compact = _ar.compact_ref_view(card)
        self.assertIn("ref's centre is D minor", compact["dos"][0])
        self.assertNotIn("(from:", compact["dos"][0])
        self.assertEqual(compact["dos"][1], "Leave space in bar 4")

    def test_sections_uncertain_survive_the_cut(self):
        card = _r3_card("r-b")
        card.update({
            "describe_excerpt": "x" * 2000,
            "sections": [{"t": "00:30", "label": "drop"}],
            "uncertain": ["tempo unclear"],
        })
        brief = _ar.ref_brief(card)
        self.assertEqual(brief["sections"], ["00:30 - drop"])
        self.assertEqual(brief["uncertain"], ["tempo unclear"])
        keys = list(brief)
        self.assertLess(keys.index("sections"), keys.index("excerpt"))
        self.assertLess(keys.index("uncertain"), keys.index("excerpt"))
        head = "\n".join(brief["sections"] + brief["uncertain"])
        self.assertLessEqual(len(head) + len(brief["excerpt"]), 1600 + len(" [...]"))
        self.assertTrue(brief["excerpt"].endswith("[...]"))


class TestR7OtherTranscriptionStaysPreview(unittest.TestCase):
    def _run_apply(self, *, ownership, write=True):
        pkg = {
            "ok": True, "ref_id": "r-x", "role": "lead",
            "notes": [dict(_SEED[0])],
            "note_count": 1, "tempo": 120.0, "clip_name": "ref x",
            "card": {"ownership": ownership},
        }
        fake_ar = mock.Mock()
        fake_ar.apply_audio_ref.return_value = pkg
        fake_ar.normalize_ownership.side_effect = _ar.normalize_ownership
        stamped: dict = {}

        def _fake_write(target, **kw):
            stamped.update(kw)
            return {"slot": "0/0"}

        def _boom(target, **kw):
            raise AssertionError("another artist's notes must never reach JAM")

        with mock.patch.object(_server, "_audio_ref_mod",
                               return_value=fake_ar), mock.patch(
            "jam_lifecycle.write_answer_to_jam",
            side_effect=_fake_write if ownership == "his" else _boom):
            out = _server.apply_audio_ref("r-x", role="lead", write=write)
        return out, stamped

    def test_other_owned_write_stays_preview(self):
        out, _ = self._run_apply(ownership="other")
        self.assertTrue(out.get("write_skipped"))
        self.assertIsNone(out.get("live_write"))
        self.assertIn("answer_my_loop", out.get("write_reason") or "")
        self.assertIn("r-x", out.get("write_reason") or "")

    def test_unknown_owned_write_stays_preview(self):
        out, _ = self._run_apply(ownership="unknown")
        self.assertTrue(out.get("write_skipped"))

    def test_his_owned_write_lands(self):
        out, stamped = self._run_apply(ownership="his")
        self.assertNotIn("write_skipped", out)
        self.assertEqual(stamped.get("source"), "ref:r-x")

    def test_preview_needs_no_ownership(self):
        out, _ = self._run_apply(ownership="other", write=False)
        self.assertNotIn("write_skipped", out)


@requires_store("audio_refs")
class TestR8SuspectFlag(unittest.TestCase):
    def _seed(self, td):
        from pathlib import Path

        idx = Path(td) / "index.json"
        orig_root, orig_index = _ar.OUT_ROOT, _ar.INDEX_PATH
        _ar.OUT_ROOT = Path(td) / "audio_refs"
        _ar.INDEX_PATH = idx
        _ar.save_index({"version": 1, "refs": {"r-s": _r3_card("r-s")}}, idx)
        return idx, orig_root, orig_index

    def test_mark_excludes_and_clear_restores(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            idx, orig_root, orig_index = self._seed(td)
            try:
                card = _ar.mark_ref_suspect("r-s", "templated boilerplate",
                                            index_path=idx)
                self.assertTrue(card["suspect"])
                self.assertEqual(card["suspect_reason"],
                                 "templated boilerplate")
                self.assertEqual(
                    [c["ref_id"] for c in _ar.find_audio_refs(
                        k=5, index_path=idx)], [])
                self.assertEqual(
                    [c["ref_id"] for c in _ar.find_audio_refs(
                        k=5, index_path=idx, include_suspect=True)],
                    ["r-s"])
                with self.assertRaises(ValueError) as ctx:
                    _ar.get_ref_card("r-s", index_path=idx)
                self.assertIn("templated boilerplate", str(ctx.exception))
                # Steering refuses through the server chokepoint too.
                with self.assertRaises(ValueError):
                    _server.beat_from_ref("r-s", seed=0)
                cleared = _ar.clear_ref_suspect("r-s", index_path=idx)
                self.assertNotIn("suspect", cleared)
                self.assertNotIn("suspect_reason", cleared)
                self.assertEqual(
                    [c["ref_id"] for c in _ar.find_audio_refs(
                        k=5, index_path=idx)], ["r-s"])
                _ar.get_ref_card("r-s", index_path=idx)
                with self.assertRaises(ValueError):
                    _ar.mark_ref_suspect("r-s", "  ", index_path=idx)
                with self.assertRaises(KeyError):
                    _ar.mark_ref_suspect("r-ghost", "x", index_path=idx)
                with self.assertRaises(KeyError):
                    _ar.clear_ref_suspect("r-ghost", index_path=idx)
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = orig_root, orig_index

    def test_clear_rebuilds_stale_row_from_disk_card(self):
        # Clearing suspect rebuilds the index row from the validated
        # on-disk card (via card_from_out_dir), so a stale row — e.g.
        # the superseded D-minor text — cannot resurface.
        import json
        import tempfile
        from pathlib import Path

        # v2 card shape mirrored from the validated v2 contract exercised
        # in tests/test_relisten_v2_listener.py::_card.
        on_disk = {
            "contract_version": _ar.CARD_CONTRACT_VERSION,
            "identity": "on-disk identity",
            "tempo": {"grid_bpm": 100.0, "feel_bpm": 50.0,
                      "confidence": "med"},
            "meter": {"beats_per_bar": 4, "half_time": True},
            "swing": {"pct": 0.0, "confidence": "high"},
            "density": "sparse",
            "register": {"band": "low", "pitch_span": "C1-G2"},
            "harmony": {"modal_center": "E minor", "pitch_classes": ["E"],
                        "interval_content": ["P5"],
                        "harmonic_rhythm_bars": 4.0, "confidence": "med"},
            "melody": {"phrase_length_bars": 2.0, "contour": "static",
                       "call_response": "none", "motif_note_count": 2,
                       "repetition": "verbatim"},
            "drums": {"grid": [{"instrument": "kick", "steps": [0]}],
                      "confidence": "high"},
            "sections": [{"t": "00:01", "label": "entry", "event": "enter",
                          "density": "sparse", "register": "low",
                          "harmony": None}],
            "imitation": {"do": ["hold the root"],
                          "dont": ["busy the pocket"]},
            "uncertain": [],
        }
        _ar.validate_card(on_disk)
        with tempfile.TemporaryDirectory() as td:
            idx = Path(td) / "index.json"
            orig_root, orig_index = _ar.OUT_ROOT, _ar.INDEX_PATH
            _ar.OUT_ROOT = Path(td) / "audio_refs"
            try:
                out = _ar.OUT_ROOT / "r-s"
                out.mkdir(parents=True)
                (out / "card.json").write_text(json.dumps(on_disk))
                (out / "meta.json").write_text(json.dumps({
                    "source": "file", "source_kind": "file",
                    "suspect": True,
                    "suspect_reason": "templated boilerplate"}))
                stale = _r3_card("r-s")
                stale.update({
                    "out_dir": str(out), "identity": "STALE D-minor text",
                    "suspect": True,
                    "suspect_reason": "templated boilerplate"})
                _ar.save_index({"version": 1, "refs": {"r-s": stale}}, idx)
                cleared = _ar.clear_ref_suspect("r-s", index_path=idx)
                self.assertEqual(cleared["identity"], "on-disk identity")
                self.assertNotIn("STALE", json.dumps(cleared))
                self.assertNotIn("suspect", cleared)
                self.assertNotIn("suspect_reason", cleared)
                saved = _ar.load_index(idx)["refs"]["r-s"]
                self.assertEqual(saved["identity"], "on-disk identity")
                meta = json.loads((out / "meta.json").read_text())
                self.assertNotIn("suspect", meta)
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = orig_root, orig_index

    def test_shelved_matches_but_never_answers_suspect_hidden(self):
        # R8: the ledger tally only ranks search down (matching ignores
        # it); suspect leaves default search; BOTH never answer.
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            idx, orig_root, orig_index = self._seed(td)
            data = _ar.load_index(idx)
            data["refs"]["r-s"].update({"title": "Doom Loop",
                                        "ricardo_note": "a keeper"})
            extra = {"r-g": _r3_card("r-g")}
            extra["r-g"].update({"title": "Doom Gloom"})
            data["refs"].update(extra)
            _ar.save_index(data, idx)
            try:
                doom = lambda **kw: [c["ref_id"] for c in _ar.find_audio_refs(
                    query="doom", k=10, index_path=idx, **kw)]
                self.assertEqual(doom(), ["r-s", "r-g"])
                # Two drops, no keep: shelved — still matches, ranked down.
                _ar.record_ref_judgment("r-s", "dropped", index_path=idx)
                _ar.record_ref_judgment("r-s", "dropped", index_path=idx)
                self.assertTrue(_ar.ref_is_shelved(
                    _ar.load_index(idx)["refs"]["r-s"]))
                self.assertEqual(doom(), ["r-g", "r-s"])
                # ... but never answers, steered or unsteered.
                with self.assertRaises(ValueError):
                    _ar.get_ref_card("r-s", index_path=idx)
                # Suspect leaves default search entirely.
                _ar.mark_ref_suspect("r-s", "templated boilerplate",
                                     index_path=idx)
                self.assertEqual(doom(), ["r-g"])
                self.assertEqual(
                    {c["ref_id"] for c in _ar.find_audio_refs(
                        query="doom", k=10, index_path=idx,
                        include_suspect=True)},
                    {"r-s", "r-g"})
                flagged = [c for c in _ar.find_audio_refs(
                    query="doom", k=10, index_path=idx,
                    include_suspect=True) if c["ref_id"] == "r-s"][0]
                self.assertTrue(flagged["suspect"])
                self.assertEqual(flagged["suspect_reason"],
                                 "templated boilerplate")
                # Clearing suspect still leaves the shelf: still no answers.
                _ar.clear_ref_suspect("r-s", index_path=idx)
                with self.assertRaises(ValueError):
                    _ar.get_ref_card("r-s", index_path=idx)
                # A KEEP lifts the shelf: answers again.
                _ar.record_ref_judgment("r-s", "kept", index_path=idx)
                self.assertFalse(_ar.ref_is_shelved(
                    _ar.load_index(idx)["refs"]["r-s"]))
                self.assertEqual(
                    _ar.get_ref_card("r-s", index_path=idx)["ref_id"], "r-s")
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = orig_root, orig_index

    def test_suspect_parent_hides_orphan_group(self):
        # An orphan that matches must not drag its suspect parent back
        # into default search, even with include_moments. Triage remains
        # available through include_suspect.
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            idx, orig_root, orig_index = self._seed(td)
            data = _ar.load_index(idx)
            data["refs"]["r-s"].update({"title": "Doom Loop"})
            data["refs"]["r-s_m1_20260101-000000"] = _r3_card(
                "r-s_m1_20260101-000000")
            data["refs"]["r-s_m1_20260101-000000"].update(
                {"title": "Doom Loop slice one"})
            _ar.save_index(data, idx)
            try:
                doom = lambda **kw: [c["ref_id"] for c in _ar.find_audio_refs(
                    query="doom", k=10, index_path=idx, **kw)]
                self.assertEqual(doom(), ["r-s"])
                _ar.mark_ref_suspect("r-s", "templated boilerplate",
                                     index_path=idx)
                self.assertEqual(doom(), [])
                self.assertEqual(doom(include_moments=True), [])
                flagged = doom(include_suspect=True)
                self.assertEqual(flagged, ["r-s"])
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = orig_root, orig_index

    def test_shelved_ref_never_answers_through_server(self):
        # R8 end to end: beat_from_ref refuses a shelved ref (the same
        # chokepoint guards the unsteered retrieval — it resolves the card
        # first).
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            idx = Path(td) / "index.json"
            _ar.save_index({"version": 1, "refs": {"r-j": _r3_card("r-j")}},
                           idx)
            _ar.record_ref_judgment("r-j", "dropped", index_path=idx)
            _ar.record_ref_judgment("r-j", "dropped", index_path=idx)
            orig_index = _ar.INDEX_PATH
            _ar.INDEX_PATH = idx
            try:
                with mock.patch.object(
                        _server, "_audio_ref_mod", return_value=_ar):
                    with self.assertRaises(ValueError) as ctx:
                        _server.beat_from_ref("r-j", bars=1, seed=0)
                self.assertIn("shelved", str(ctx.exception))
            finally:
                _ar.INDEX_PATH = orig_index

    def test_verdict_and_links_files_never_enter_index(self):
        # M5/M7 overlays stay physical files: a stray verdict.json or
        # links.json in a ref dir never loads into the index card and never
        # affects matching.
        import json as _json
        import tempfile
        from pathlib import Path

        from tests.test_relisten_v2_listener import _card as _v2_card

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "audio_refs"
            root.mkdir()
            out = root / "r-file"
            out.mkdir()
            (out / "card.json").write_text(
                _json.dumps(_v2_card()), encoding="utf-8")
            (out / "meta.json").write_text(_json.dumps({
                "source": "f", "source_kind": "file",
                "modes": ["describe"], "verdict": "keep"}), encoding="utf-8")
            (out / "verdict.json").write_text(_json.dumps(
                {"verdict": "drop", "reason": "M5 file"}), encoding="utf-8")
            (out / "links.json").write_text(_json.dumps(
                {"links": [{"to": "r-other"}]}), encoding="utf-8")
            idx = Path(td) / "index.json"
            orig_root, orig_index = _ar.OUT_ROOT, _ar.INDEX_PATH
            _ar.OUT_ROOT, _ar.INDEX_PATH = root, idx
            try:
                _ar.save_index({"version": 1, "refs": {}}, idx)
                _ar._reindex_audio_refs_impl(root=root, index_path=idx)
                card = _ar.load_index(idx)["refs"]["r-file"]
                # meta.json wins; the stray files leak nothing.
                self.assertEqual(card.get("verdict"), "keep")
                self.assertNotIn("links", card)
                self.assertNotIn("M5 file", _json.dumps(card))
                hits = _ar.find_audio_refs(query="r-file", k=5,
                                           index_path=idx, detail=True)
                self.assertEqual([c["ref_id"] for c in hits], ["r-file"])
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = orig_root, orig_index

    def test_cli_mark_and_clear(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            idx, orig_root, orig_index = self._seed(td)
            try:
                self.assertEqual(
                    _ar.main(["--mark-suspect", "r-s", "key contradiction"]),
                    0)
                self.assertTrue(
                    _ar.load_index(idx)["refs"]["r-s"]["suspect"])
                self.assertEqual(
                    _ar.main(["--clear-suspect", "r-s"]), 0)
                self.assertNotIn(
                    "suspect", _ar.load_index(idx)["refs"]["r-s"])
                self.assertEqual(
                    _ar.main(["--clear-suspect", "r-ghost"]), 1)
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = orig_root, orig_index

    def test_reindex_preserves_suspect_and_tally(self):
        import json as _json
        import tempfile
        from pathlib import Path

        from tests.test_relisten_v2_listener import _card as _v2_card

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "audio_refs"
            root.mkdir()
            out = root / "r-keep"
            out.mkdir()
            (out / "card.json").write_text(
                _json.dumps(_v2_card()), encoding="utf-8")
            (out / "meta.json").write_text(
                _json.dumps({"listener_mode": "isolated",
                             "exact_listener_model": "gemini-test-1"}),
                encoding="utf-8")
            idx = Path(td) / "index.json"
            orig_root, orig_index = _ar.OUT_ROOT, _ar.INDEX_PATH
            _ar.OUT_ROOT, _ar.INDEX_PATH = root, idx
            try:
                _ar.save_index({"version": 1, "refs": {}}, idx)
                rep = _ar._reindex_audio_refs_impl(root=root,
                                                   index_path=idx)
                self.assertEqual(rep["count"], 1)
                _ar.mark_ref_suspect("r-keep", "templated boilerplate",
                                     index_path=idx)
                _ar.record_ref_judgment("r-keep", "dropped",
                                        index_path=idx, entry_id="e-9")
                # A rebuild from card.json + meta.json keeps the overlays.
                _ar._reindex_audio_refs_impl(root=root, index_path=idx)
                after = _ar.load_index(idx)["refs"]["r-keep"]
                self.assertTrue(after["suspect"])
                self.assertEqual(after["suspect_reason"],
                                 "templated boilerplate")
                self.assertEqual(_ar.ref_tally_of(after),
                                 {"kept": 0, "dropped": 1, "survived": 0})
                self.assertEqual(_ar.ref_tallied_entries_of(after),
                                 {"e-9": "dropped"})
                self.assertEqual(after["listener_mode"], "isolated")
                self.assertEqual(after["exact_listener_model"],
                                 "gemini-test-1")
                # Overlays that live only in the index (meta predates them)
                # still carry over.
                meta = _json.loads(
                    (out / "meta.json").read_text(encoding="utf-8"))
                for key in ("suspect", "suspect_reason", "judgment_tally",
                            "tallied_entries"):
                    meta.pop(key, None)
                (out / "meta.json").write_text(_json.dumps(meta),
                                               encoding="utf-8")
                _ar._reindex_audio_refs_impl(root=root, index_path=idx)
                again = _ar.load_index(idx)["refs"]["r-keep"]
                self.assertTrue(again["suspect"])
                self.assertEqual(_ar.ref_tally_of(again),
                                 {"kept": 0, "dropped": 1, "survived": 0})
                self.assertEqual(_ar.ref_tallied_entries_of(again),
                                 {"e-9": "dropped"})
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = orig_root, orig_index


class TestX1LeanListenerHarness(unittest.TestCase):
    def _envelope(self, extra_usage=None, version="v2"):
        if version == "v3":
            from tests.test_relisten_v2_listener import _v3_card

            payload = _v3_card()
        else:
            from tests.test_relisten_v2_listener import _card as _v2_card

            payload = _v2_card()
        payload["usage"] = {
            "input_tokens": 15000, "output_tokens": 800,
            "thinking_tokens": 100, "cache_read_tokens": 4300000,
        }
        if extra_usage:
            payload["usage"].update(extra_usage)
        payload["num_turns"] = 1
        payload["duration_seconds"] = 212.5
        return payload

    def test_one_structured_turn_records_usage(self):
        import json as _json
        import os as _os
        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as td:
            wav = _Path(td) / "source.wav"
            wav.write_bytes(b"RIFFfake")
            captured: dict = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = list(cmd)
                captured["env"] = dict(kwargs.get("env") or {})
                schema_path = cmd[cmd.index("--json-schema") + 1]
                captured["schema"] = _json.loads(_Path(schema_path).read_text())
                captured["schema_exists_after"] = None  # checked post-run
                captured["schema_path"] = schema_path
                if "--add-dir" in cmd:
                    staged = _Path(cmd[cmd.index("--add-dir") + 1])
                    captured["entries"] = sorted(_os.listdir(staged))
                return _ar.subprocess.CompletedProcess(
                    cmd, 0, _json.dumps(self._envelope()), "")

            with passthrough_listenable_staging(), \
                 mock.patch.object(_ar, "_which", return_value="agy"), \
                 mock.patch.object(_ar, "_run", side_effect=fake_run), \
                 mock.patch.dict(_os.environ,
                                 {"X1_SECRET": "must-not-cross",
                                  "AGY_ECHO": "allowed-prefix"}):
                result = _ar.run_agy_describe_v2(
                    wav=wav, focus="full", prompt_extra=None, duration_s=60,
                    timeout_s=61, model="gemini-3.8-flash-high")
            self.assertTrue(result["ok"])
            # One structured print turn, schema as a file beside the audio.
            self.assertIn("--print", captured["cmd"])
            self.assertEqual(
                captured["cmd"][captured["cmd"].index("--output-format") + 1],
                "json")
            self.assertTrue(captured["schema_path"].endswith(".json"))
            self.assertEqual(
                captured["schema"]["properties"]["contract_version"]["const"],
                _ar.CARD_CONTRACT_VERSION)
            self.assertFalse(_Path(captured["schema_path"]).exists())
            # Staged isolation kept: the listener sees only the wav.
            self.assertEqual(captured["entries"], ["source.wav"])
            self.assertIn("--sandbox", captured["cmd"])
            # Env allowlist (M3): identical to session ears — unrelated
            # secrets AND credential prefixes stay out, PATH in.
            self.assertNotIn("X1_SECRET", captured["env"])
            self.assertIn("PATH", captured["env"])
            self.assertNotIn("AGY_ECHO", captured["env"])
            # Usage lands on the result for meta.json listen_usage.
            self.assertEqual(result["usage"]["input_tokens"], 15000)
            self.assertEqual(result["usage"]["cache_read_tokens"], 4300000)
            self.assertEqual(result["usage"]["num_turns"], 1)
            self.assertEqual(result["usage"]["duration_seconds"], 212.5)

    def test_usage_parsing_never_fails_a_card(self):
        self.assertEqual(_ar._agy_usage_from_stdout(""), {})
        self.assertEqual(_ar._agy_usage_from_stdout("not json at all {{{"),
                         {})
        # A bare card with no envelope still validates downstream.
        from tests.test_relisten_v2_listener import _card as _v2_card
        import json as _json

        self.assertEqual(
            _ar._agy_usage_from_stdout(_json.dumps(_v2_card())), {})

    def test_listen_usage_persists_to_meta(self):
        import json as _json
        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as td:
            root = _Path(td)
            out = root / "refs" / "new"
            out.mkdir(parents=True)
            (out / "source.wav").write_bytes(b"RIFFfake")
            convs = root / "convs"

            def fake_run(cmd, **kwargs):
                if "--add-dir" in cmd:
                    # B3: the fake agent leaves view evidence for the
                    # staged path, and the envelope carries the id.
                    from tests.test_agy_listen_verification import (
                        _staged_path_from_prompt, plant_listen_evidence)
                    plant_listen_evidence(
                        convs, "conv-usage",
                        _staged_path_from_prompt(
                            cmd[cmd.index("--print") + 1]))
                    payload = self._envelope(version="v3")
                    payload["conversation_id"] = "conv-usage"
                    # A within-cap tally: input + cache_read must fit 250k.
                    payload["usage"] = {"input_tokens": 15000,
                                        "output_tokens": 800,
                                        "cache_read_tokens": 4000}
                    return _ar.subprocess.CompletedProcess(
                        cmd, 0, _json.dumps(payload), "")
                return _ar.subprocess.CompletedProcess(cmd, 0, "", "")

            old_root, old_index = _ar.OUT_ROOT, _ar.INDEX_PATH
            _ar.OUT_ROOT, _ar.INDEX_PATH = root / "refs", root / "index.json"
            try:
                with passthrough_listenable_staging(), \
                     mock.patch.object(_ar, "_which",
                                       return_value="agy"), \
                     mock.patch.object(_ar, "_run",
                                       side_effect=fake_run), \
                     mock.patch.object(_ar, "ffprobe_info",
                                       return_value={"duration_s": 60}), \
                     mock.patch.object(_ar, "_version_first_line",
                                       return_value=None):
                    result = _ar.analyze_audio_ref(
                        from_dir=str(out), modes=["describe"],
                        model="model-v2", conversations_dir=convs)
                self.assertTrue(result["mode_ok"]["describe"])
                saved = _json.loads((out / "meta.json").read_text())
                self.assertEqual(saved["listen_usage"]["input_tokens"], 15000)
                self.assertEqual(saved["listen_usage"]["num_turns"], 1)
                self.assertEqual(saved["conversation_id"], "conv-usage")
                self.assertIsNone(saved["listen_rejected"])
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = old_root, old_index


class TestX2BatchRobustness(unittest.TestCase):
    def test_classify_listen_failure(self):
        self.assertEqual(
            _ar.classify_listen_failure(
                {"error": "RESOURCE_EXHAUSTED: quota exceeded"}), "quota")
        self.assertEqual(
            _ar.classify_listen_failure({"error": "HTTP 429 too many"}),
            "quota")
        self.assertEqual(
            _ar.classify_listen_failure({"error": "agy timeout"}), "timeout")
        self.assertEqual(
            _ar.classify_listen_failure(
                {"error": "invalid v2 card: agy returned prose, not JSON: .."}),
            "invalid_json")
        self.assertEqual(
            _ar.classify_listen_failure(
                {"error": "invalid v2 card: audio-ref-card-v2 rejected at "
                          "card.tempo: null is not allowed here"}),
            "validation")
        self.assertEqual(_ar.classify_listen_failure(None), "other")
        self.assertEqual(
            _ar.classify_listen_failure({"error": "yt-dlp blew up"}), "other")

    def test_repair_retry_feeds_validator_back(self):
        import json as _json
        import tempfile
        from pathlib import Path as _Path

        from tests.test_relisten_v2_listener import _card as _v2_card

        with tempfile.TemporaryDirectory() as td:
            wav = _Path(td) / "source.wav"
            wav.write_bytes(b"RIFFfake")
            calls: list = []

            def fake_run(cmd, **kwargs):
                calls.append(list(cmd))
                if len(calls) == 1:
                    body = "Here is the analysis: the track is slow."
                else:
                    body = _v2_card()
                    body["usage"] = {"input_tokens": 10}
                return _ar.subprocess.CompletedProcess(
                    cmd, 0, body if isinstance(body, str)
                    else _json.dumps(body), "")

            with passthrough_listenable_staging(), \
                 mock.patch.object(_ar, "_which",
                                   return_value="agy"), \
                 mock.patch.object(_ar, "_run", side_effect=fake_run):
                result = _ar.run_agy_describe_v2(
                    wav=wav, focus="full", prompt_extra=None, duration_s=60,
                    timeout_s=61, model="gemini-3.8-flash-high")
            self.assertTrue(result["ok"])
            self.assertEqual(result["attempts"], 2)
            self.assertEqual(len(calls), 2)
            # The validator message goes back into the second prompt.
            self.assertIn("prose", calls[1][calls[1].index("--print") + 1])

    def test_batch_stops_on_quota(self):
        import tempfile
        from pathlib import Path as _Path

        calls: list = []

        def fake_analyze(**kw):
            calls.append(kw.get("source"))
            if kw.get("source") == "a":
                return {"ok": True, "partial": False, "ref_id": "r-a",
                        "out_dir": "/tmp/a", "error": None,
                        "listen_usage": {"input_tokens": 100,
                                         "output_tokens": 10},
                        "focus": "full", "agy_model": "m",
                        "timeout_s": 600,
                        "contract_version": _ar.CARD_CONTRACT_VERSION,
                        "hashes": None, "evidence": None}
            if kw.get("source") == "b":
                return {"ok": False, "partial": False, "ref_id": None,
                        "out_dir": "/tmp/b", "error": None,
                        "describe_result": {
                            "ok": False,
                            "error": "RESOURCE_EXHAUSTED: quota exceeded",
                        },
                        "listen_usage": None, "focus": "full",
                        "agy_model": "m", "timeout_s": 600,
                        "contract_version": None, "hashes": None,
                        "evidence": None}
            raise AssertionError("batch must stop before item c")

        with tempfile.TemporaryDirectory() as td:
            manifest = {"batch_id": "x2q",
                        "items": [{"source": "a"}, {"source": "b"},
                                  {"source": "c"}]}
            with mock.patch.object(_ar, "BATCH_ROOT", _Path(td)), \
                 mock.patch.object(_ar, "analyze_audio_ref",
                                   side_effect=fake_analyze):
                report = _ar.run_batch(manifest, resume=False)
            self.assertFalse(report["complete"])
            self.assertEqual(report["stopped_reason"], "quota")
            rows = {r["source"]: r for r in report["results"]}
            self.assertEqual(sorted(rows), ["a", "b"])
            self.assertEqual(rows["b"]["failure_kind"], "quota")
            self.assertTrue(rows["b"]["retry_in_place"])
            self.assertEqual(report["resume_from"], rows["b"]["resume_key"])
            self.assertEqual(report["listen_tokens"]["input_tokens"], 100)
            self.assertEqual(report["listen_tokens"]["items_with_usage"], 1)


def _write_wav(path, samples, sr=22050):
    import struct
    import wave

    clamped = [max(-32768, min(32767, int(s * 32767))) for s in samples]
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sr)
        writer.writeframes(struct.pack("<%dh" % len(clamped), *clamped))


def _click_track(sr=22050, bpm=120.0, seconds=8.0):
    import numpy as np

    n = int(sr * seconds)
    out = np.zeros(n)
    period = int(sr * 60.0 / bpm)
    decay = np.exp(-np.arange(int(sr * 0.01)) / (sr * 0.002))
    for start in range(0, n, period):
        stop = min(n, start + len(decay))
        out[start:stop] += decay[: stop - start] * 0.9
    return out


def _sine_track(sr=22050, freq=440.0, seconds=5.0, amp=0.5):
    import numpy as np

    t = np.arange(int(sr * seconds)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).tolist()


class TestX3MeasureFirst(unittest.TestCase):
    def test_click_track_tempo_and_grid(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "clicks.wav"
            _write_wav(wav, _click_track(bpm=120.0, seconds=8.0))
            measured = _ar.measure_audio(wav)
            self.assertIsNotNone(measured["tempo_bpm"])
            self.assertAlmostEqual(measured["tempo_bpm"], 120.0, delta=1.5)
            gaps = [b - a for a, b in
                    zip(measured["beat_times_s"], measured["beat_times_s"][1:])]
            self.assertTrue(gaps)
            for gap in gaps:
                self.assertAlmostEqual(gap, 0.5, delta=0.02)
            self.assertTrue(measured["energy_per_bar"])
            self.assertAlmostEqual(measured["bar_len_s"], 2.0, delta=0.05)

    def test_novelty_boundary_on_level_jump(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "twolevel.wav"
            _write_wav(wav, _sine_track(seconds=5.0, amp=0.15)
                       + _sine_track(freq=440.0, seconds=5.0, amp=0.8))
            measured = _ar.measure_audio(wav)
            self.assertTrue(any(abs(b - 5.0) <= 1.6
                                for b in measured["novelty_boundaries_s"]),
                            measured["novelty_boundaries_s"])

    def test_chroma_key_names_concert_a(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "a440.wav"
            _write_wav(wav, _sine_track(freq=440.0, seconds=5.0))
            measured = _ar.measure_audio(wav)
            key = (measured["chroma_key"] or {}).get("key") or ""
            self.assertEqual(key.split()[0], "A")

    def test_silence_measures_nothing(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "silent.wav"
            _write_wav(wav, [0.0] * 22050)
            measured = _ar.measure_audio(wav)
            self.assertIsNone(measured["tempo_bpm"])
            self.assertEqual(measured["novelty_boundaries_s"], [])
            self.assertIsNone(measured["chroma_key"]["key"])

    def test_reconciler_marks_and_flags(self):
        card = {"tempo": {"grid_bpm": 120.0, "feel_bpm": 60.0,
                          "confidence": "med"},
                "harmony": {"modal_center": "E minor"},
                "sections": [{"t": "00:30"}, {"t": "01:00"}]}
        measured = {"tempo_bpm": 120.0,
                    "chroma_key": {"key": "E minor", "strength": 0.4},
                    "novelty_boundaries_s": [30.0],
                    "energy_per_bar": [0.1, 0.4], "bar_len_s": 2.0}
        rec = _ar.reconcile_measured(measured, card)
        self.assertEqual(rec["tempo"]["status"], "agree")
        self.assertEqual(rec["tempo"]["uses_bpm"], 120.0)
        self.assertEqual(rec["key"]["status"], "agree")
        self.assertFalse(rec["key"]["flagged"])
        self.assertEqual(rec["sections"]["status"], "agree")
        self.assertEqual(rec["energy_per_bar"], [0.1, 0.4])

        disagree = _ar.reconcile_measured(
            measured,
            {"tempo": {"grid_bpm": 90.0, "feel_bpm": 90.0},
             "harmony": {"modal_center": "C major"},
             "sections": [{"t": "00:10"}] * 6})
        self.assertEqual(disagree["tempo"]["status"], "disagree")
        # Measured tempo wins even in disagreement.
        self.assertEqual(disagree["tempo"]["uses_bpm"], 120.0)
        self.assertEqual(disagree["key"]["status"], "disagree")
        self.assertTrue(disagree["key"]["flagged"])
        self.assertEqual(disagree["key"]["measured"], "E minor")
        self.assertEqual(disagree["key"]["card"], "C major")

        blind = _ar.reconcile_measured({}, card)
        self.assertEqual(
            (blind["tempo"]["status"], blind["key"]["status"],
             blind["sections"]["status"]),
            ("model_only", "model_only", "model_only"))

    def test_key_agrees_on_tonic_with_unknown_mode(self):
        def _key(measured_key, card_center):
            rec = _ar.reconcile_measured(
                {"tempo_bpm": 120.0,
                 "chroma_key": {"key": measured_key, "strength": 0.5},
                 "novelty_boundaries_s": [], "energy_per_bar": [],
                 "bar_len_s": 2.0},
                {"tempo": {"grid_bpm": 120.0},
                 "harmony": {"modal_center": card_center},
                 "sections": []})
            return rec["key"]

        # Same tonic, mode missing on one side: agree, mode unknown.
        agree = _key("D minor", "D")
        self.assertEqual(agree["status"], "agree")
        self.assertFalse(agree["flagged"])
        self.assertIsNotNone(agree["note"])
        agree2 = _key("D", "D minor")
        self.assertEqual(agree2["status"], "agree")
        # Same pitch class spelled both ways, both modes known and equal.
        self.assertEqual(_key("Bb major", "A# major")["status"], "agree")
        # Same tonic, both modes known and different: disagree.
        dd = _key("D minor", "D major")
        self.assertEqual(dd["status"], "disagree")
        self.assertTrue(dd["flagged"])
        # Different tonics: disagree.
        self.assertEqual(_key("D minor", "F major")["status"], "disagree")
        # Exact match stays a clean agree.
        clean = _key("E minor", "E minor")
        self.assertEqual(clean["status"], "agree")
        self.assertIsNone(clean["note"])

    def test_tempo_octave_agreement(self):
        def _tempo(measured_bpm, grid_bpm):
            rec = _ar.reconcile_measured(
                {"tempo_bpm": measured_bpm,
                 "chroma_key": {"key": None, "strength": 0.0},
                 "novelty_boundaries_s": [], "energy_per_bar": [],
                 "bar_len_s": 2.0},
                {"tempo": {"grid_bpm": grid_bpm}, "sections": []})
            return rec["tempo"]

        self.assertEqual(_tempo(140.0, 140.0)["status"], "agree")
        self.assertIsNone(_tempo(140.0, 140.0)["note"])
        half = _tempo(70.0, 140.0)
        self.assertEqual(half["status"], "agree")
        self.assertEqual(half["note"], "octave-equivalent")
        self.assertEqual(_tempo(280.5, 140.0)["status"], "agree")
        self.assertEqual(_tempo(100.0, 138.0)["status"], "disagree")

    def test_disagreements_surface_in_uncertain_view(self):
        import json
        import tempfile
        from pathlib import Path

        from tests.test_relisten_v2_listener import _card as _v2_card

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "r-flag"
            out.mkdir()
            inner = _v2_card()
            inner["tempo"] = {"grid_bpm": 120.0, "feel_bpm": 120.0,
                              "confidence": "high"}
            inner["harmony"] = {"modal_center": "E minor",
                                "pitch_classes": ["E"],
                                "interval_content": ["P5"],
                                "harmonic_rhythm_bars": 4.0,
                                "confidence": "high"}
            inner["uncertain"] = ["kick grid blurred by reverb"]
            (out / "card.json").write_text(json.dumps(inner))
            measured = {"tempo_bpm": 90.0,
                        "chroma_key": {"key": "C major", "strength": 0.8},
                        "novelty_boundaries_s": [], "energy_per_bar": [],
                        "bar_len_s": 2.0}
            rec = _ar.reconcile_measured(measured, inner)
            self.assertEqual(rec["tempo"]["status"], "disagree")
            self.assertEqual(rec["key"]["status"], "disagree")
            (out / "meta.json").write_text(json.dumps({
                "source": "f", "source_kind": "file",
                "reconciliation": rec}))
            card = _ar.card_from_out_dir(out)
            # Flag, never override: heard values stand, flags join uncertain.
            self.assertEqual(
                (card["card"]["tempo"] or {}).get("grid_bpm"), 120.0)
            self.assertEqual(
                (card["card"]["harmony"] or {}).get("modal_center"),
                "E minor")
            flags = [u for u in card["uncertain"]
                     if "disagreement" in u]
            self.assertEqual(len(flags), 2)
            self.assertIn("kick grid blurred by reverb", card["uncertain"])
            brief = _ar.ref_brief(card)
            self.assertTrue(any("tempo disagreement" in u
                                for u in brief["uncertain"]))
            self.assertTrue(any("key disagreement" in u
                                for u in brief["uncertain"]))
            compact = _ar.compact_ref_view(card)
            self.assertTrue(any("disagreement" in u
                                for u in compact["uncertain"]))
            # Rebuilding from the same meta never duplicates the flags.
            again = _ar.card_from_out_dir(out)
            self.assertEqual(again["uncertain"], card["uncertain"])

    def test_full_prompt_holds_back_boundaries_tempo_and_key(self):
        import json as _json
        import tempfile
        from pathlib import Path as _Path

        from tests.test_relisten_v2_listener import _card as _v2_card

        with tempfile.TemporaryDirectory() as td:
            wav = _Path(td) / "source.wav"
            wav.write_bytes(b"RIFFfake")
            prompts: list = []

            def fake_run(cmd, **kwargs):
                prompts.append(cmd[cmd.index("--print") + 1])
                body = _v2_card()
                body["usage"] = {}
                return _ar.subprocess.CompletedProcess(
                    cmd, 0, _json.dumps(body), "")

            measured = {"tempo_bpm": 100.0,
                        "novelty_boundaries_s": [30.0, 90.0],
                        "chroma_key": {"key": "E minor", "strength": 0.4}}
            with passthrough_listenable_staging(), \
                 mock.patch.object(_ar, "_which",
                                   return_value="agy"), \
                 mock.patch.object(_ar, "_run", side_effect=fake_run):
                result = _ar.run_agy_describe_v2(
                    wav=wav, focus="full", prompt_extra=None, duration_s=125,
                    timeout_s=61, model="gemini-3.8-flash-high",
                    measured=measured)
            self.assertTrue(result["ok"])
            (prompt,) = prompts
            self.assertNotIn("Loudness change points", prompt)
            self.assertNotIn("00:30", prompt)
            # Blind: neither the measured tempo nor the key may leak in.
            self.assertNotIn("100", prompt)
            self.assertNotIn("minor", prompt.lower())
            rec = result["reconciliation"]
            self.assertEqual(rec["tempo"]["status"], "agree")
            self.assertEqual(rec["key"]["status"], "agree")


class TestX4CardSchemaV3(unittest.TestCase):
    def _cards(self):
        from tests.test_relisten_v2_listener import (
            _card as _v2_card, _v3_card)
        return _v2_card(), _v3_card()

    def test_v3_validates_and_dispatch_reads_both(self):
        v2, v3 = self._cards()
        self.assertIs(_ar.validate_card_v3(v3), v3)
        self.assertIs(_ar.validate_card(v3), v3)
        self.assertIs(_ar.validate_card(v2), v2)
        with self.assertRaises(ValueError):
            _ar.validate_card_v3(v2)
        with self.assertRaises(ValueError):
            _ar.validate_card({"contract_version": "audio-ref-card-v9"})

    def test_v3_rejects_bad_shapes(self):
        _, v3 = self._cards()
        import copy

        too_many = copy.deepcopy(v3)
        too_many["sections"] = [dict(too_many["sections"][0])] * 17
        with self.assertRaises(ValueError):
            _ar.validate_card_v3(too_many)

        bad_energy = copy.deepcopy(v3)
        bad_energy["sections"][0]["energy"] = 6
        with self.assertRaises(ValueError):
            _ar.validate_card_v3(bad_energy)

        bad_role = copy.deepcopy(v3)
        bad_role["layers"][0]["role"] = "violin"
        with self.assertRaises(ValueError):
            _ar.validate_card_v3(bad_role)

        stale_melody = copy.deepcopy(v3)
        stale_melody["melody"]["contour"] = "static"
        with self.assertRaises(ValueError):
            _ar.validate_card_v3(stale_melody)

    def test_v3_uncertain_must_address_low_confidence(self):
        _, v3 = self._cards()
        import copy

        soft = copy.deepcopy(v3)
        soft["tempo"]["confidence"] = "med"
        soft["harmony"]["confidence"] = "low"
        soft["drums"]["confidence"] = "low"
        with self.assertRaises(ValueError):
            _ar.validate_card_v3(soft)
        soft["uncertain"] = ["tempo drifts in the intro",
                             "key could be relative major",
                             "kick grid blurred by reverb"]
        self.assertIs(_ar.validate_card_v3(soft), soft)

    def test_v3_prompt_names_enums_bans_generic_no_examples(self):
        prompt = _ar.DESCRIBE_PROMPT_V3
        for phrase in ("sparse, medium or dense", "low, med or high",
                       "drums, bass, pad, lead, texture or fx",
                       "stay in key, no swing",
                       "Return exactly one JSON object"):
            self.assertIn(phrase, prompt)
        for word in ("68", "sludge", "ostinato", "Aeolian", "0% swing",
                     "snare on 3", "half-time", "hard-quantize", "2-bar",
                     "crash-ride"):
            self.assertNotIn(word, prompt)
        # Renders with the same fields the harness supplies.
        rendered = prompt.format(name="x.wav", duration_s=60,
                                 duration_mmss="01:00", focus="", prompt_extra="",
                                 measured_note="")
        self.assertIn("x.wav", rendered)

    def test_v3_listen_uses_v3_schema_and_prompt(self):
        import json as _json
        import tempfile
        from pathlib import Path as _Path

        from tests.test_relisten_v2_listener import _v3_card

        with tempfile.TemporaryDirectory() as td:
            wav = _Path(td) / "source.wav"
            wav.write_bytes(b"RIFFfake")
            captured: dict = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = list(cmd)
                schema_path = cmd[cmd.index("--json-schema") + 1]
                captured["schema"] = _json.loads(
                    _Path(schema_path).read_text())
                captured["prompt"] = cmd[cmd.index("--print") + 1]
                body = _v3_card()
                body["usage"] = {}
                return _ar.subprocess.CompletedProcess(
                    cmd, 0, _json.dumps(body), "")

            with passthrough_listenable_staging(), \
                 mock.patch.object(_ar, "_which",
                                   return_value="agy"), \
                 mock.patch.object(_ar, "_run", side_effect=fake_run):
                result = _ar.run_agy_describe_v3(
                    wav=wav, focus="full", prompt_extra=None, duration_s=60,
                    timeout_s=61, model="gemini-3.8-flash-high",
                    measured={"novelty_boundaries_s": []})
            self.assertTrue(result["ok"])
            self.assertEqual(
                captured["schema"]["properties"]["contract_version"]["const"],
                _ar.V3_CONTRACT_VERSION)
            self.assertIn("Allowed values, exactly", captured["prompt"])

    def test_consumers_read_v2_and_v3(self):
        v2, v3 = self._cards()
        s2, s3 = _ar.structure_from_card(v2), _ar.structure_from_card(v3)
        self.assertEqual(set(s2), set(s3))
        self.assertEqual(s3["grid_bpm"], 100.0)
        n2, ok2 = _ar.card_grid_to_notes(v2)
        n3, ok3 = _ar.card_grid_to_notes(v3)
        self.assertTrue(ok2 and ok3)
        self.assertEqual([n["start"] for n in n2],
                         [n["start"] for n in n3])
        i2 = _ar.card_from_v2(v2, ref_id="r2", out_dir="/tmp/r2")
        i3 = _ar.card_from_v3(v3, ref_id="r3", out_dir="/tmp/r3")
        self.assertEqual(set(i2), set(i3))
        self.assertEqual(i3["contract_version"], _ar.V3_CONTRACT_VERSION)
        self.assertEqual(i2["imitation_do"], i3["imitation_do"])
        b2, b3 = _ar.ref_brief(i2), _ar.ref_brief(i3)
        self.assertEqual(b2["imitation_do"], b3["imitation_do"])
        st2 = _ar.ref_steering(i2)
        st3 = _ar.ref_steering(i3)
        for key in ("mods", "tempo", "register", "drum_grid", "usable"):
            self.assertEqual(st2[key], st3[key], key)
        self.assertEqual(_ar.ref_to_mods(i2)[0].get("density"),
                         _ar.ref_to_mods(i3)[0].get("density"))


class TestX5FocusRespectingReconciler(unittest.TestCase):
    def _measured_card(self):
        measured = {"tempo_bpm": 120.0,
                    "chroma_key": {"key": "E minor", "strength": 0.4},
                    "novelty_boundaries_s": [30.0],
                    "energy_per_bar": [0.1, 0.4], "bar_len_s": 2.0}
        card = {"tempo": {"grid_bpm": 120.0, "feel_bpm": 60.0,
                          "confidence": "high"},
                "swing": {"pct": 10.0, "confidence": "med"},
                "harmony": {"modal_center": "E minor",
                            "pitch_classes": ["E", "G", "B"],
                            "confidence": "high"},
                "melody": {"phrase_length_bars": 2.0,
                           "repetition": "verbatim"},
                "register": {"band": "mid", "pitch_span": "E2-E4"},
                "drums": {"grid": [{"instrument": "kick", "steps": [0]}],
                          "confidence": "high"},
                "sections": [{"t": "00:30"}, {"t": "01:00"}]}
        return measured, card

    def test_focus_keeps_only_its_claims(self):
        measured, card = self._measured_card()
        groove = _ar.reconcile_measured(measured, card, focus="groove")
        self.assertEqual(
            sorted(groove),
            sorted(["focus", "tempo", "swing", "drums", "sections",
                    "energy_per_bar", "bar_len_s"]))
        self.assertEqual(groove["tempo"]["status"], "agree")
        self.assertEqual(groove["swing"]["status"], "model_only")
        self.assertEqual(groove["swing"]["card_pct"], 10.0)
        self.assertEqual(groove["drums"]["card_instruments"], ["kick"])

        harmony = _ar.reconcile_measured(measured, card, focus="harmony")
        self.assertEqual(sorted(harmony),
                         ["focus", "key", "midipitch", "register"])
        self.assertEqual(harmony["key"]["status"], "agree")
        self.assertEqual(harmony["register"]["card_band"], "mid")

        melody = _ar.reconcile_measured(measured, card, focus="melody")
        self.assertEqual(sorted(melody), ["focus", "phrase", "range"])
        self.assertEqual(melody["phrase"]["card_phrase_length_bars"], 2.0)
        self.assertEqual(melody["range"]["card_pitch_span"], "E2-E4")

        full = _ar.reconcile_measured(measured, card)
        self.assertEqual(full["focus"], "full")
        for claim in ("tempo", "swing", "drums", "key", "midipitch",
                      "register", "phrase", "range", "sections"):
            self.assertIn(claim, full)
        with self.assertRaises(ValueError):
            _ar.reconcile_measured(measured, card, focus="composition")

    def test_overlays_are_v3_keyed_and_distinct(self):
        modes = _ar.FOCUS_MODES
        self.assertEqual(set(_ar.FOCUS_OVERLAYS), set(modes))
        self.assertEqual(_ar.FOCUS_OVERLAYS["full"], "")
        self.assertIn("drum grid", _ar.FOCUS_OVERLAYS["groove"])
        self.assertIn("Ableton moves", _ar.FOCUS_OVERLAYS["groove"])
        self.assertIn("modal center", _ar.FOCUS_OVERLAYS["harmony"])
        self.assertIn("low-end", _ar.FOCUS_OVERLAYS["harmony"])
        self.assertIn("phrase", _ar.FOCUS_OVERLAYS["melody"])
        self.assertNotEqual(_ar.FOCUS_OVERLAYS["groove"],
                            _ar.FOCUS_OVERLAYS["harmony"])

    def test_focus_single_source(self):
        modes = _ar.FOCUS_MODES
        self.assertEqual(tuple(modes),
                         ("full", "groove", "harmony", "melody"))
        for mode, intent in modes.items():
            self.assertTrue(intent, mode)
        # CLI derives its choices from the modes.
        parser = _ar.cli_parser()
        focus_action = next(a for a in parser._actions
                            if "--focus" in a.option_strings)
        self.assertEqual(set(focus_action.choices), set(modes))
        # Server + profile copy names exactly these modes, nothing retired.
        self.assertIn("full|groove|harmony|melody",
                      _server.analyze_audio_ref.__doc__)
        from mcp_server import profiles as _profiles

        self.assertIn("full|groove|harmony|melody",
                      _profiles.PROFILE_INSTRUCTIONS["reference"])
        self.assertNotIn("composition",
                         _server.analyze_audio_ref.__doc__)
        # normalize_focus enforces the same set.
        self.assertEqual(_ar.normalize_focus("harmony"), "harmony")
        with self.assertRaises(ValueError):
            _ar.normalize_focus("composition")


def _make_midi(path, notes, is_drum=True):
    """notes: [(pitch, start_s, end_s)]. Writes a .mid via pretty_midi."""
    import pretty_midi

    pm = pretty_midi.PrettyMIDI()
    inst = pretty_midi.Instrument(program=0, is_drum=is_drum)
    for pitch, start, end in notes:
        inst.notes.append(pretty_midi.Note(
            velocity=100, pitch=pitch, start=start, end=end))
    pm.instruments.append(inst)
    pm.write(str(path))
    return path


class TestX6StemsPerStemMidi(unittest.TestCase):
    def test_midi_lanes_and_phase_steps(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            mid = Path(td) / "drums.mid"
            notes = ([(36, t, t + 0.1) for t in (0.0, 0.5, 1.0, 1.5)]
                     + [(38, t, t + 0.1) for t in (0.5, 1.5)])
            _make_midi(mid, notes)
            lanes = _ar.midi_drum_lanes(mid, 120.0)
            self.assertEqual(
                [(lane["instrument"], lane["steps"]) for lane in lanes],
                [("kick", [0, 4, 8, 12]), ("snare", [4, 12])])
            self.assertEqual(_ar.midi_onset_phase_steps(mid, 120.0),
                             [0, 4, 8, 12])
            self.assertIsNone(_ar.midi_drum_lanes(mid, 0.0))
            self.assertIsNone(
                _ar.midi_drum_lanes(Path(td) / "missing.mid", 120.0))
            # Non-GM pitches map to no lane.
            pitched = Path(td) / "pitched.mid"
            _make_midi(pitched, [(60, 0.0, 0.5)], is_drum=False)
            self.assertIsNone(_ar.midi_drum_lanes(pitched, 120.0))

    def test_long_jittered_offset_loop_resolves_to_card_grid(self):
        # Review fix: 3-min 138 BPM four-on-the-floor with jitter, a
        # downbeat offset and slightly-wrong (even half-time) measured
        # tempos still yields kick {0,4,8,12} once resolved against the
        # card tempo; one-off fills wash out at the 50%-of-bars bar.
        import random
        import tempfile
        from pathlib import Path

        import pretty_midi

        bpm = 138.0
        beat = 60.0 / bpm
        offset = 0.37
        random.seed(1)
        pm = pretty_midi.PrettyMIDI()
        inst = pretty_midi.Instrument(program=0, is_drum=True)
        for b in range(int(180 / beat)):
            t = offset + b * beat + random.gauss(0, 0.008)
            inst.notes.append(pretty_midi.Note(100, 36, t, t + 0.05))
            inst.notes.append(pretty_midi.Note(
                70, 42, t + beat / 2, t + beat / 2 + 0.03))
        inst.notes.append(pretty_midi.Note(90, 49, 50.0, 50.2))
        pm.instruments.append(inst)
        with tempfile.TemporaryDirectory() as td:
            mid = Path(td) / "drums.mid"
            pm.write(str(mid))
            for tb in (138.0, 137.4, 138.9, 69.44):
                lanes = _ar.midi_drum_lanes(
                    mid, tb, card_tempo_bpm=bpm)
                by_inst = {lane["instrument"]: lane["steps"]
                           for lane in lanes}
                self.assertEqual(by_inst.get("kick"), [0, 4, 8, 12], tb)
                self.assertNotIn("crash", by_inst, tb)
            steps = _ar.midi_onset_phase_steps(
                mid, 69.44, card_tempo_bpm=bpm)
            for step in (0, 4, 8, 12):
                self.assertIn(step, steps)

    def test_resolve_tempo_octave(self):
        self.assertEqual(_ar.resolve_tempo_octave(137.4, 138.0), 138.0)
        self.assertEqual(_ar.resolve_tempo_octave(138.9, 138.0), 138.0)
        self.assertEqual(_ar.resolve_tempo_octave(69.44, 138.0), 138.0)
        self.assertEqual(_ar.resolve_tempo_octave(276.5, 138.0), 138.0)
        # No agreement: the measurement stands.
        self.assertEqual(_ar.resolve_tempo_octave(100.0, 138.0), 100.0)
        # No card: the measurement stands; unusable: None.
        self.assertEqual(_ar.resolve_tempo_octave(137.4, None), 137.4)
        self.assertIsNone(_ar.resolve_tempo_octave(None, 138.0))
        self.assertIsNone(_ar.resolve_tempo_octave(0.0, 138.0))

    def test_stems_winner_grids_and_tempo(self):
        import tempfile
        from pathlib import Path

        from tests.test_relisten_v2_listener import _v3_card

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "refs" / "song"
            (out / "stems").mkdir(parents=True)
            (out / "source.wav").write_bytes(b"RIFFfake")
            (out / "meta.json").write_text('{"source": "old"}')
            # Caller stems: tiny real wavs (ffmpeg decodes them on ingest).
            _write_wav(root / "drums.wav", [0.0] * 2205)
            _write_wav(root / "bass.wav", [0.0] * 2205)
            drums_notes = ([(36, t, t + 0.1)
                            for t in [i * 0.5 for i in range(8)]]
                           + [(38, t, t + 0.1)
                              for t in [0.5 + i for i in range(4)]])
            bass_notes = [(40, 0.0, 0.4), (43, 0.5, 0.9),
                          (45, 1.0, 1.4), (43, 1.5, 1.9)]
            tempos: list = []

            def fake_basic_pitch(wav, midi_path, tempo=120.0):
                tempos.append(tempo)
                role = Path(midi_path).stem
                if role == "drums":
                    _make_midi(midi_path, drums_notes)
                    count = len(drums_notes)
                else:
                    _make_midi(midi_path, bass_notes, is_drum=False)
                    count = len(bass_notes)
                return {"note_count": count, "pitch_min": 36,
                        "pitch_max": 45, "model_path_used": "fake"}

            heard = _v3_card()
            heard["drums"]["grid"] = []

            def fake_describe(**kwargs):
                return {"ok": True, "card": heard, "argv": ["agy"],
                        "usage": {}, "reconciliation": {"focus": "full"}}

            old_root, old_index = _ar.OUT_ROOT, _ar.INDEX_PATH
            _ar.OUT_ROOT, _ar.INDEX_PATH = root / "refs", root / "index.json"
            try:
                with mock.patch.object(_ar, "run_basic_pitch",
                                       side_effect=fake_basic_pitch), \
                     mock.patch.object(_ar, "run_agy_describe_v3",
                                       side_effect=fake_describe), \
                     mock.patch.object(_ar, "measure_audio",
                                       return_value={"tempo_bpm": 111.0}), \
                     mock.patch.object(_ar, "ffprobe_info",
                                       return_value={"duration_s": 4}), \
                     mock.patch.object(_ar, "collect_tool_versions",
                                       return_value={}):
                    result = _ar.analyze_audio_ref(
                        from_dir=str(out), modes=["midi", "describe"],
                        stem_files={"drums": str(root / "drums.wav"),
                                    "bass": str(root / "bass.wav")})
                # Basic Pitch ran per stem at the MEASURED tempo.
                self.assertEqual(sorted(tempos), [111.0, 111.0])
                # Per-stem midis kept; the MELODIC stem with the most notes
                # wins ref.mid (deliberate M5 change: drums never wins, so
                # melodic consumers never read drum hits as pitched lines).
                self.assertTrue((out / "stems" / "drums.mid").is_file())
                self.assertTrue((out / "stems" / "bass.mid").is_file())
                self.assertEqual(result["winning_stem"], "bass")
                self.assertEqual(
                    (out / "ref.mid").read_bytes(),
                    (out / "stems" / "bass.mid").read_bytes())
                sixteenth = 60.0 / 111.0 / 4.0
                want_steps = sorted({
                    int(round(t / sixteenth)) % 16
                    for t in [i * 0.5 for i in range(8)]
                    + [0.5 + i for i in range(4)]})
                grid = result["measured_drum_grid"]
                self.assertEqual(grid["method"], "stem_midi_onsets")
                self.assertEqual(grid["steps"], want_steps)
                # Deliberate change (review fix): MIDI-derived grids never
                # enter the model's card — the heard null stays null and
                # the stem evidence lives in measured_drum_grid only.
                saved = _ar.load_index(root / "index.json")["refs"]["song"]
                self.assertFalse((saved["card"]["drums"] or {}).get("grid"))
                self.assertIsNone(saved["structure"]["drum_grid"])
                self.assertFalse(any("filled from drums stem midi" in w
                                     for w in result["warnings"]))
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = old_root, old_index


@requires_store("audio_refs")
class TestX7Provenance(unittest.TestCase):
    def test_normalize_listener_mode(self):
        self.assertIsNone(_ar.normalize_listener_mode(None))
        self.assertIsNone(_ar.normalize_listener_mode("  "))
        self.assertEqual(_ar.normalize_listener_mode("interactive"),
                         "interactive")
        self.assertEqual(_ar.normalize_listener_mode("ISOLATED"), "isolated")
        with self.assertRaises(ValueError):
            _ar.normalize_listener_mode("supervised")

    def _sandbox(self, td: str):
        from pathlib import Path

        root = Path(td)
        _write_wav(root / "in.wav", [0.0] * 2205)
        old_root, old_index = _ar.OUT_ROOT, _ar.INDEX_PATH
        _ar.OUT_ROOT, _ar.INDEX_PATH = root / "refs", root / "index.json"
        return root, old_root, old_index

    def test_analyze_midi_only_gets_no_mode(self):
        # Deliberate change (review fix): runs with no listener stamp no
        # mode — the old code stamped these interactive by default.
        import json
        import tempfile
        from pathlib import Path

        def fake_basic_pitch(wav, midi_path, tempo=120.0):
            _make_midi(midi_path, [(36, 0.0, 0.1)])
            return {"note_count": 1, "model_path_used": "fake"}

        with tempfile.TemporaryDirectory() as td:
            root, old_root, old_index = self._sandbox(td)
            try:
                with mock.patch.object(_ar, "run_basic_pitch",
                                       side_effect=fake_basic_pitch), \
                     mock.patch.object(_ar, "measure_audio",
                                       return_value={"tempo_bpm": 100.0}):
                    result = _ar.analyze_audio_ref(
                        str(root / "in.wav"), modes=["midi", "features"])
                self.assertIsNone(result["listener_mode"])
                # No listener ran (midi-only), so no model to name.
                self.assertIsNone(result["exact_listener_model"])
                meta = json.loads(
                    (Path(result["wav_path"]).parent / "meta.json")
                    .read_text(encoding="utf-8"))
                self.assertIsNone(meta["listener_mode"])
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = old_root, old_index

    def test_analyze_pipeline_listen_is_isolated_unless_overridden(self):
        # Deliberate change (review fix): every pipeline per-track listen —
        # CLI, MCP or batch — is isolated. The old CLI/MCP = interactive
        # default is gone; only an explicit operator override wins.
        import tempfile

        from tests.test_relisten_v2_listener import _v3_card

        def fake_describe(**kwargs):
            return {"ok": True, "card": _v3_card(), "argv": ["agy"],
                    "usage": {}, "reconciliation": {"focus": "full"}}

        def fake_basic_pitch(wav, midi_path, tempo=120.0):
            _make_midi(midi_path, [(36, 0.0, 0.1)])
            return {"note_count": 1, "model_path_used": "fake"}

        with tempfile.TemporaryDirectory() as td:
            root, old_root, old_index = self._sandbox(td)
            try:
                with mock.patch.object(_ar, "run_agy_describe_v3",
                                       side_effect=fake_describe), \
                     mock.patch.object(_ar, "run_basic_pitch",
                                       side_effect=fake_basic_pitch), \
                     mock.patch.object(_ar, "measure_audio",
                                       return_value={"tempo_bpm": 100.0}), \
                     mock.patch.object(_ar, "ffprobe_info",
                                       return_value={"duration_s": 4}), \
                     mock.patch.object(_ar, "collect_tool_versions",
                                       return_value={}):
                    plain = _ar.analyze_audio_ref(
                        str(root / "in.wav"), modes=["describe"])
                    self.assertEqual(plain["listener_mode"], "isolated")
                    batched = _ar.analyze_audio_ref(
                        str(root / "in.wav"), modes=["describe"],
                        batch_id="b1")
                    self.assertEqual(batched["listener_mode"], "isolated")
                    forced = _ar.analyze_audio_ref(
                        str(root / "in.wav"), modes=["describe"],
                        batch_id="b1", listener_mode="interactive")
                    self.assertEqual(forced["listener_mode"], "interactive")
                    # A midi-only batch run has no listener: no mode at all.
                    midionly = _ar.analyze_audio_ref(
                        str(root / "in.wav"), modes=["midi"],
                        batch_id="b1")
                    self.assertIsNone(midionly["listener_mode"])
                    with self.assertRaises(ValueError):
                        _ar.analyze_audio_ref(
                            str(root / "in.wav"), modes=["describe"],
                            listener_mode="watched")
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = old_root, old_index

    def test_describe_is_isolated_and_relisten_resets_mode(self):
        # Deliberate change (review fix): a re-listen through the pipeline
        # is isolated — the old mode carry-forward is gone — and a
        # listener-less re-listen clears the mode. The model carry stays.
        import json
        import tempfile
        from pathlib import Path

        from tests.test_relisten_v2_listener import _v3_card

        def fake_describe(**kwargs):
            return {"ok": True, "card": _v3_card(), "argv": ["agy"],
                    "usage": {}, "reconciliation": {"focus": "full"}}

        def fake_basic_pitch(wav, midi_path, tempo=120.0):
            _make_midi(midi_path, [(36, 0.0, 0.1)])
            return {"note_count": 1, "model_path_used": "fake"}

        with tempfile.TemporaryDirectory() as td:
            root, old_root, old_index = self._sandbox(td)
            try:
                with mock.patch.object(_ar, "run_agy_describe_v3",
                                       side_effect=fake_describe), \
                     mock.patch.object(_ar, "run_basic_pitch",
                                       side_effect=fake_basic_pitch), \
                     mock.patch.object(_ar, "measure_audio",
                                       return_value={"tempo_bpm": 100.0}), \
                     mock.patch.object(_ar, "ffprobe_info",
                                       return_value={"duration_s": 4}), \
                     mock.patch.object(_ar, "collect_tool_versions",
                                       return_value={}):
                    result = _ar.analyze_audio_ref(
                        str(root / "in.wav"), modes=["describe"],
                        model="gemini-probe-1")
                    self.assertEqual(result["listener_mode"], "isolated")
                    self.assertEqual(result["exact_listener_model"],
                                     "gemini-probe-1")
                    out_dir = Path(result["wav_path"]).parent
                    # Simulate a legacy stamp, then re-listen with a
                    # listener: the pipeline mode wins, it is not carried.
                    meta = json.loads(
                        (out_dir / "meta.json").read_text(encoding="utf-8"))
                    meta["listener_mode"] = "interactive"
                    (out_dir / "meta.json").write_text(json.dumps(meta))
                    relistened = _ar.analyze_audio_ref(
                        None, from_dir=str(out_dir), modes=["describe"],
                        model="gemini-probe-1")
                    self.assertEqual(relistened["listener_mode"], "isolated")
                    # A listener-less re-listen keeps the model but no mode.
                    second = _ar.analyze_audio_ref(
                        None, from_dir=str(out_dir), modes=["midi"])
                self.assertIsNone(second["listener_mode"])
                self.assertEqual(second["exact_listener_model"],
                                 "gemini-probe-1")
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = old_root, old_index

    def test_card_from_out_dir_carries_provenance(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "song"
            out.mkdir()
            (out / "meta.json").write_text(json.dumps({
                "source": "f", "source_kind": "file",
                "modes": ["describe"],
                "listener_mode": "isolated",
                "exact_listener_model": "gemini-batch-2",
            }))
            card = _ar.card_from_out_dir(out)
            self.assertEqual(card["listener_mode"], "isolated")
            self.assertEqual(card["exact_listener_model"], "gemini-batch-2")

    def test_upsert_preserves_stamped_provenance(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            index = Path(td) / "index.json"
            _ar.save_index({"version": 1, "updated_at": None, "refs": {
                "r": {"ref_id": "r", "listener_mode": "interactive",
                      "exact_listener_model": "gemini-old"}}}, index)
            fresh = {"ref_id": "r", "out_dir": str(Path(td) / "r"),
                     "tags": [], "linked_projects": []}
            kept = _ar.upsert_index_card(dict(fresh), path=index)
            self.assertEqual(kept["listener_mode"], "interactive")
            self.assertEqual(kept["exact_listener_model"], "gemini-old")

    def test_stamp_provenance_dry_run_then_apply(self):
        # Deliberate change (review fix): only v2 card.json dirs with no
        # pipeline-listen evidence (agy argv) and no parent_ref_id are
        # legacy chat-heard cards. A bare batch_id does NOT exclude (batch
        # runs stamp it for non-listen work too); pipeline argv, moment
        # slices and midi-only dirs are never stamped interactive.
        import json
        import tempfile
        from pathlib import Path

        from tests.test_relisten_v2_listener import _card as _v2_card

        def _dir(name, meta, card=True):
            out = Path(td) / "refs" / name
            out.mkdir(parents=True)
            (out / "source.wav").write_bytes(b"RIFFfake")
            if card:
                (out / "card.json").write_text(json.dumps(_v2_card()))
            (out / "meta.json").write_text(json.dumps(meta))

        with tempfile.TemporaryDirectory() as td:
            _dir("heard", {"source": "f", "source_kind": "file",
                           "modes": ["describe", "midi"],
                           "agy_model": "gemini-past"})
            _dir("pipeline", {"source": "f", "source_kind": "file",
                              "modes": ["describe"], "batch_id": "b-gate",
                              "evidence": {"listener": "agy",
                                           "argv": ["agy", "-p"]}})
            _dir("batched", {"source": "f", "source_kind": "file",
                             "modes": ["describe"], "batch_id": "b1"})
            _dir("moment", {"source": "f", "source_kind": "moment_clip",
                            "parent_ref_id": "heard"})
            _dir("midionly", {"source": "f", "source_kind": "file",
                              "modes": ["midi"]}, card=False)
            _dir("stamped", {"source": "f", "source_kind": "file",
                             "modes": ["describe"],
                             "listener_mode": "isolated"})
            root = Path(td) / "refs"
            index = Path(td) / "index.json"
            _ar.save_index({"version": 1, "updated_at": None, "refs": {
                "heard": {"ref_id": "heard"}}}, index)
            dry = _ar.stamp_provenance(root=root, index_path=index)
            self.assertFalse(dry["applied"])
            self.assertEqual(dry["stamped"], ["batched", "heard"])
            self.assertEqual(dry["model_backfilled"], ["heard"])
            self.assertEqual(dry["already"], 1)
            self.assertEqual(dry["skipped_no_listener"], 1)
            self.assertEqual(dry["skipped_pipeline"], 2)
            # Dry run writes nothing.
            meta = json.loads((root / "heard" / "meta.json").read_text())
            self.assertNotIn("listener_mode", meta)
            applied = _ar.stamp_provenance(
                root=root, index_path=index, apply=True)
            self.assertTrue(applied["applied"])
            meta = json.loads((root / "heard" / "meta.json").read_text())
            self.assertEqual(meta["listener_mode"], "interactive")
            self.assertEqual(meta["exact_listener_model"], "gemini-past")
            card = _ar.load_index(index)["refs"]["heard"]
            self.assertEqual(card["listener_mode"], "interactive")
            self.assertEqual(card["exact_listener_model"], "gemini-past")

    def test_stamp_cli_flags(self):
        import tempfile

        p = _ar.cli_parser()
        args = p.parse_args(["--stamp-provenance"])
        self.assertTrue(args.stamp_provenance)
        self.assertFalse(args.stamp_apply)
        with tempfile.TemporaryDirectory() as td:
            from pathlib import Path

            old_root, old_index = _ar.OUT_ROOT, _ar.INDEX_PATH
            _ar.OUT_ROOT, _ar.INDEX_PATH = (Path(td) / "refs",
                                            Path(td) / "index.json")
            try:
                self.assertEqual(_ar.main(["--stamp-provenance"]), 0)
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = old_root, old_index


def _x5_sections():
    return [
        {"t": "00:20", "label": "verse", "energy": 3,
         "layers_in": ["soft pad"], "layers_out": [],
         "transition_in": None},
        {"t": "01:40", "label": "drop", "energy": 5,
         "layers_in": ["kick", "snare"], "layers_out": [],
         "transition_in": "build"},
        {"t": "02:40", "label": "break", "energy": 4,
         "layers_in": ["warm keys"], "layers_out": ["kick"],
         "transition_in": "breakdown"},
    ]


class TestX5PassBWindows(unittest.TestCase):
    def test_select_peak_windows_centered_and_bounded(self):
        sections = [
            {"t": "00:20", "label": "a", "energy": 5,
             "layers_in": ["kick"], "layers_out": [],
             "transition_in": None},
            {"t": "01:30", "label": "b", "energy": 5,
             "layers_in": ["kick"], "layers_out": [],
             "transition_in": None},
            {"t": "02:20", "label": "c", "energy": 4,
             "layers_in": ["kick"], "layers_out": [],
             "transition_in": None},
        ]
        wins = _ar.select_pass_b_windows(sections, 180.0, focus="full")
        self.assertEqual(len(wins), 3)
        for start, end in wins:
            self.assertGreaterEqual(start, 0.0)
            self.assertLessEqual(end, 180.0)
            self.assertGreaterEqual(end - start, 30.0)
            self.assertLessEqual(end - start, 45.0)
        # Energy order: the two energy-5 peaks first (by time), then energy 4.
        self.assertAlmostEqual(wins[0][0], 0.0)
        self.assertAlmostEqual((wins[1][0] + wins[1][1]) / 2, 90.0)
        self.assertAlmostEqual((wins[2][0] + wins[2][1]) / 2, 140.0)
        # Non-overlapping.
        for (s1, e1), (s2, e2) in zip(wins, wins[1:]):
            self.assertGreaterEqual(s2, e1)

    def test_select_windows_empty_sections_falls_back_to_spread(self):
        wins = _ar.select_pass_b_windows([], 150.0, focus="full")
        self.assertEqual(len(wins), 3)
        for start, end in wins:
            self.assertGreaterEqual(start, 0.0)
            self.assertLessEqual(end, 150.0)
            self.assertGreaterEqual(end - start, 30.0)
        for (s1, e1), (s2, e2) in zip(wins, wins[1:]):
            self.assertGreaterEqual(s2, e1)

    def test_select_windows_short_file_returns_none(self):
        self.assertEqual(
            _ar.select_pass_b_windows(_x5_sections(), 20.0), [])

    def test_focus_steers_which_peak_comes_first(self):
        # Harmony prefers the pitched sections (pad/keys) over the louder
        # drums peak; groove ranks by energy alone. The drums peak at 00:50
        # overlaps the pad verse window, so exactly one of them survives.
        sections = [
            {"t": "00:20", "label": "verse", "energy": 3,
             "layers_in": ["soft pad"], "layers_out": [],
             "transition_in": None},
            {"t": "00:50", "label": "drop", "energy": 5,
             "layers_in": ["kick", "snare"], "layers_out": [],
             "transition_in": "build"},
            {"t": "02:40", "label": "break", "energy": 4,
             "layers_in": ["warm keys"], "layers_out": ["kick"],
             "transition_in": "breakdown"},
        ]
        harmony = _ar.select_pass_b_windows(
            sections, 200.0, focus="harmony")
        groove = _ar.select_pass_b_windows(
            sections, 200.0, focus="groove")
        self.assertAlmostEqual(harmony[0][0], 0.0)   # pad verse kept
        self.assertAlmostEqual(groove[0][0], 27.5)   # drums peak kept
        self.assertNotIn([0.0, 45.0], [list(w) for w in groove])

    def test_overlays_name_v3_keys(self):
        self.assertIn("grid_bpm", _ar.FOCUS_OVERLAYS["groove"])
        self.assertIn("pitch_classes", _ar.FOCUS_OVERLAYS["harmony"])
        self.assertIn("phrase_length_bars", _ar.FOCUS_OVERLAYS["melody"])

    def test_pass_b_off_by_default(self):
        import inspect

        self.assertIs(
            inspect.signature(_ar.analyze_audio_ref).parameters[
                "pass_b"].default, False)

    def test_run_cuts_listens_and_merges_with_fake_runner(self):
        import tempfile
        import wave
        from pathlib import Path

        import numpy as _np

        from tests.test_relisten_v2_listener import _v3_card

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wav = root / "source.wav"
            sr = 22050
            n = sr * 200
            t = _np.arange(n) / sr
            audio = (0.3 * _np.sin(2 * _np.pi * 220.0 * t) * 32767).astype(
                _np.int16)
            with wave.open(str(wav), "wb") as fh:
                fh.setnchannels(1)
                fh.setsampwidth(2)
                fh.setframerate(sr)
                fh.writeframes(audio.tobytes())

            card = _v3_card()
            card["sections"] = _x5_sections()
            calls = []

            def _fake_listen(*, wav, prompt_extra, focus, duration_s,
                             timeout_s, model, **_verify_kw):
                calls.append({"wav": wav, "duration_s": duration_s,
                              "prompt_extra": prompt_extra, "focus": focus})
                return {"ok": True, "card": _v3_card(),
                        "usage": {"input_tokens": 10}}

            out = _ar.run_pass_b_windows(
                wav=wav, v3_card=card, duration_s=200.0, focus="full",
                out_dir=root, model="fake-model",
                cut_fn=_ar.ffmpeg_to_wav, listen_fn=_fake_listen)
            wins = out["windows"]
            self.assertEqual(len(wins), 3)
            self.assertTrue(all(w["ok"] for w in wins))
            for w in wins:
                self.assertTrue(Path(w["wav"]).is_file())
                _ar.validate_card_v3(w["card"])
                self.assertIn("groove", w["focus_aspect"])
            # The cuts are real ffmpeg slices of the synthetic source.
            self.assertEqual(len(calls), 3)
            for c in calls:
                self.assertGreaterEqual(c["duration_s"], 30.0)
                self.assertIn("THIS excerpt", c["prompt_extra"])
            # Usage merges across the three window turns.
            self.assertEqual(out["usage"].get("input_tokens"), 30)

    def test_window_listen_failure_is_an_entry_not_a_crash(self):
        import tempfile
        from pathlib import Path

        from tests.test_relisten_v2_listener import _v3_card

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            card = _v3_card()
            card["sections"] = _x5_sections()

            def _fake_listen(*, wav, prompt_extra, focus, duration_s,
                             timeout_s, model, **_verify_kw):
                if "window2" in str(wav):
                    return {"ok": False, "error": "quota"}
                return {"ok": True, "card": _v3_card(), "usage": {}}

            def _fake_cut(src, dst, start, end):
                Path(dst).write_bytes(b"RIFFfake")

            out = _ar.run_pass_b_windows(
                wav=root / "source.wav", v3_card=card, duration_s=200.0,
                out_dir=root, cut_fn=_fake_cut, listen_fn=_fake_listen)
            wins = out["windows"]
            self.assertEqual([w["ok"] for w in wins],
                             [True, False, True])
            self.assertIn("quota", wins[1]["error"])

    def test_windows_ride_meta_into_index_and_reindex(self):
        import tempfile
        from pathlib import Path

        from tests.test_relisten_v2_listener import _v3_card

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            heard = root / "r-win"
            heard.mkdir()
            (heard / "card.json").write_text(
                __import__("json").dumps(_v3_card()), encoding="utf-8")
            meta = {"source": "x", "source_kind": "file", "focus": "full",
                    "windows": [{"window": 1, "start_s": 0.0, "end_s": 40.0,
                                 "ok": True, "card": _v3_card()}]}
            (heard / "meta.json").write_text(
                __import__("json").dumps(meta), encoding="utf-8")
            card = _ar.card_from_out_dir(heard, dict(meta))
            self.assertEqual(len(card["windows"]), 1)


def _m2_envelope(response: str) -> str:
    import json as _json

    return _json.dumps({
        "conversation_id": "conv-1",
        "status": "ok",
        "response": response,
        "duration_seconds": 12.5,
        "num_turns": 1,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    })


def _m2_card() -> dict:
    return {"contract_version": _ar.V3_CONTRACT_VERSION, "identity": "X"}


def _agy_json_result(stdout: str) -> dict:
    return _ar._agy_json_result(stdout)


class TestM2EnvelopeParsing(unittest.TestCase):
    def test_bare_card_response_in_envelope(self):
        import json as _json

        card = _agy_json_result(_m2_envelope(_json.dumps(_m2_card())))
        self.assertEqual(card["identity"], "X")
        self.assertNotIn("conversation_id", card)

    def test_fenced_card_response_in_envelope(self):
        import json as _json

        card = _agy_json_result(
            _m2_envelope("here:\n```json\n%s\n```" % _json.dumps(_m2_card())))
        self.assertEqual(card["identity"], "X")

    def test_prose_wrapped_card_response_in_envelope(self):
        import json as _json

        card = _agy_json_result(
            _m2_envelope("My analysis:\n%s\nHope this helps."
                         % _json.dumps(_m2_card())))
        self.assertEqual(card["identity"], "X")

    def test_stray_fence_in_response_does_not_eat_the_card(self):
        # A ``` emphasis fence in the response prose must not win over the
        # whole-stdout envelope parse: the card after it still reads.
        import json as _json

        response = ("Note: ``` listen on headphones ```\n%s"
                    % _json.dumps(_m2_card()))
        card = _agy_json_result(_m2_envelope(response))
        self.assertEqual(card["identity"], "X")

    def test_usage_from_indented_prose_wrapped_envelope(self):
        import json as _json

        envelope = _json.loads(_m2_envelope("{}"))
        stdout = "Result:\n%s\nDone." % _json.dumps(envelope, indent=4)
        usage = _ar._agy_usage_from_stdout(stdout)
        self.assertEqual(usage.get("input_tokens"), 100)
        self.assertEqual(usage.get("output_tokens"), 50)
        self.assertEqual(usage.get("duration_seconds"), 12.5)
        self.assertEqual(usage.get("num_turns"), 1)

    def test_usage_never_raises_on_garbage(self):
        self.assertEqual(_ar._agy_usage_from_stdout("oops, no json here"), {})
        self.assertEqual(_ar._agy_usage_from_stdout(""), {})

    def test_prose_stdout_still_raises(self):
        with self.assertRaises(ValueError):
            _agy_json_result("just commentary, no braces at all")


def _real_agy_envelope() -> str:
    # Fix2 item 2: the exact shape a REAL agy call returned
    # (agy -p ... --output-format json --json-schema <file> --sandbox):
    # the schema-shaped answer rides in structured_output, the same JSON
    # again as a string in response (with agy narration keys beside it),
    # and the tally in usage + top-level turn keys.
    import json as _json

    inner_answer = "{\n  \"answer\": \"OK\"\n}"
    return _json.dumps({
        "conversation_id": "conv-real-1",
        "status": "SUCCESS",
        "response": _json.dumps({
            "answer": inner_answer,
            "toolAction": "Returning JSON response",
            "toolSummary": "Return JSON response",
        }) + "\n",
        "duration_seconds": 4.46,
        "num_turns": 1,
        "structured_output": {"answer": inner_answer},
        "json_schema": {"type": "object"},
        "usage": {"input_tokens": 14504, "output_tokens": 583,
                  "thinking_tokens": 541, "cache_read_tokens": 0,
                  "total_tokens": 15087},
    })


class TestRealAgyEnvelope(unittest.TestCase):
    def test_structured_output_wins_and_parses_once(self):
        # structured_output is preferred over response; the JSON string
        # nested in the string-typed field parses exactly once; agy
        # narration keys never leak into the card.
        card = _agy_json_result(_real_agy_envelope())
        self.assertEqual(card, {"answer": {"answer": "OK"}})

    def test_response_fallback_strips_narration(self):
        # Without structured_output the response string still reads, with
        # the harness narration stripped (the nested JSON string itself
        # stays a string — only the structured path parses it once).
        import json as _json

        envelope = _json.loads(_real_agy_envelope())
        del envelope["structured_output"]
        card = _agy_json_result(_json.dumps(envelope))
        self.assertEqual(card["answer"], "{\n  \"answer\": \"OK\"\n}")
        self.assertNotIn("toolAction", card)
        self.assertNotIn("toolSummary", card)
        self.assertNotIn("status", card)

    def test_plain_strings_pass_through_structured_output(self):
        import json as _json

        envelope = _json.loads(_real_agy_envelope())
        envelope["structured_output"] = {
            "identity": "X", "notes": "plain prose, not json",
            "drums": {"grid": []},
        }
        card = _agy_json_result(_json.dumps(envelope))
        self.assertEqual(card["identity"], "X")
        self.assertEqual(card["notes"], "plain prose, not json")
        self.assertEqual(card["drums"], {"grid": []})

    def test_usage_from_real_envelope(self):
        usage = _ar._agy_usage_from_stdout(_real_agy_envelope())
        self.assertEqual(usage.get("input_tokens"), 14504)
        self.assertEqual(usage.get("output_tokens"), 583)
        self.assertEqual(usage.get("thinking_tokens"), 541)
        self.assertEqual(usage.get("cache_read_tokens"), 0)
        self.assertEqual(usage.get("total_tokens"), 15087)
        self.assertEqual(usage.get("duration_seconds"), 4.46)
        self.assertEqual(usage.get("num_turns"), 1)
        self.assertEqual(usage.get("status"), "SUCCESS")


class TestM3ListenerHardening(unittest.TestCase):
    def test_skip_permissions_is_a_constant_default_off(self):
        self.assertIs(_ar.AGY_SKIP_PERMISSIONS, False)
        cmd = _ar._agy_cmd_for("gemini-3.8-flash-medium", "/tmp", "hi")
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        with mock.patch.object(_ar, "AGY_SKIP_PERMISSIONS", True):
            toggled = _ar._agy_cmd_for(
                "gemini-3.8-flash-medium", "/tmp", "hi")
        self.assertIn("--dangerously-skip-permissions", toggled)

    def test_card_listen_sandbox_ignores_env_override(self):
        with mock.patch.dict("os.environ", {"AUDIO_REF_AGY_SANDBOX": "0"}):
            cmd = _ar._agy_cmd_for("gemini-3.8-flash-medium", "/tmp", "hi")
        self.assertIn("--sandbox", cmd)

    def test_legacy_describe_hardened_too(self):
        import tempfile
        from pathlib import Path

        captured = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["env"] = kwargs.get("env")
            return type("Proc", (), {"returncode": 0, "stdout": "",
                                    "stderr": ""})()

        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "source.wav"
            wav.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
            with passthrough_listenable_staging(), \
                 mock.patch.dict(
                "os.environ", {"AUDIO_REF_AGY_SANDBOX": "0",
                               "GEMINI_API_KEY": "secret"}), \
                    mock.patch.object(_ar, "_which",
                                      return_value="/fake/agy"), \
                    mock.patch.object(_ar, "_run", side_effect=_fake_run):
                _ar.run_agy_describe(
                    wav=wav, describe_path=Path(td) / "describe.txt",
                    prompt_extra=None, focus="full", duration_s=10.0,
                    source_label=None, timeout_s=600, model="m",
                    allow_unverified_listen=True)
        self.assertIn("--sandbox", captured["cmd"])
        self.assertNotIn("--dangerously-skip-permissions", captured["cmd"])
        self.assertNotIn("GEMINI_API_KEY", captured["env"] or {})

    def test_listen_env_matches_session_ears_allowlist(self):
        from mcp_server import session_ears as _se

        self.assertEqual(set(_ar._AGY_ENV_ALLOW),
                         set(_se._AGY_ENV_ALLOW))
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "s3cr3t",
                                            "GOOGLE_API_KEY": "s3cr3t",
                                            "UNRELATED_X": "1",
                                            "PATH": "/bin"}):
            env = _ar._agy_listen_env()
        self.assertNotIn("GEMINI_API_KEY", env)
        self.assertNotIn("GOOGLE_API_KEY", env)
        self.assertNotIn("UNRELATED_X", env)
        self.assertEqual(env.get("PATH"), "/bin")

    def test_multi_turn_listen_warns(self):
        warnings: list = []
        _ar._note_listener_turns({"num_turns": 3}, warnings)
        self.assertEqual(len(warnings), 1)
        self.assertIn("3 turns", warnings[0])
        _ar._note_listener_turns({"num_turns": 1}, warnings)
        _ar._note_listener_turns({}, warnings)
        _ar._note_listener_turns(None, warnings)
        self.assertEqual(len(warnings), 1)

    def test_num_turns_recorded_in_merged_usage(self):
        merged = _ar._merge_usages([{"num_turns": 1, "input_tokens": 5},
                                    {"num_turns": 2, "input_tokens": 7}])
        self.assertEqual(merged["num_turns"], 3)
        self.assertEqual(merged["input_tokens"], 12)


class TestM5MeasuredMidi(unittest.TestCase):
    def test_note_features_dropped_rough_tempo(self):
        feats = _ar.note_features([(0.0, 0.5, 60, 90, 0)])
        self.assertNotIn("rough_tempo_bpm", feats)
        self.assertEqual(feats["note_count"], 1)
        self.assertNotIn("rough_tempo_bpm",
                         _ar.note_features([]))

    def test_nonstem_bp_uses_measured_octave_resolved_tempo(self):
        import tempfile
        from pathlib import Path

        tempos: list = []

        def fake_bp(wav, midi_path, tempo=120.0):
            tempos.append(tempo)
            _make_midi(midi_path, [(60, 0.0, 0.4)])
            return {"note_count": 1, "pitch_min": 60,
                    "pitch_max": 60, "model_path_used": "fake"}

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_wav(root / "in.wav", [0.0] * 2205)
            old_root, old_index = _ar.OUT_ROOT, _ar.INDEX_PATH
            _ar.OUT_ROOT, _ar.INDEX_PATH = root / "refs", root / "index.json"
            try:
                with mock.patch.object(_ar, "run_basic_pitch",
                                       side_effect=fake_bp), \
                     mock.patch.object(_ar, "measure_audio",
                                       return_value={"tempo_bpm": 138.0}):
                    result = _ar.analyze_audio_ref(
                        str(root / "in.wav"), modes=["midi"],
                        midi_tempo=120.0)
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = old_root, old_index
        # 138 measured vs 120 anchor: no octave agreement, measured wins
        # (pre-fix code passed the 120 guess straight through).
        self.assertEqual(tempos, [138.0])
        self.assertEqual(result["measured"]["tempo_bpm"], 138.0)

    def test_windowed_analyze_skips_measure_audio(self):
        # Review fix (minors): a start/end window describes a clip slice, so
        # file-zero-based DSP (tempo, loudness windows) must not run on it.
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_wav(root / "in.wav", [0.0] * 2205)
            old_root, old_index = _ar.OUT_ROOT, _ar.INDEX_PATH
            _ar.OUT_ROOT, _ar.INDEX_PATH = root / "refs", root / "index.json"
            try:
                with mock.patch.object(_ar, "measure_audio") as m_measure, \
                     mock.patch.object(_ar, "ffprobe_info",
                                       return_value={"duration_s": 4}), \
                     mock.patch.object(_ar, "collect_tool_versions",
                                       return_value={}):
                    result = _ar.analyze_audio_ref(
                        str(root / "in.wav"), modes=["features"],
                        start="0:00", end="0:01")
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = old_root, old_index
        m_measure.assert_not_called()
        self.assertIsNone(result["measured"])

    def test_full_analyze_still_measures(self):
        # Guard for the windowed skip above: without start/end the local
        # DSP measurement still runs.
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_wav(root / "in.wav", [0.0] * 2205)
            old_root, old_index = _ar.OUT_ROOT, _ar.INDEX_PATH
            _ar.OUT_ROOT, _ar.INDEX_PATH = root / "refs", root / "index.json"
            try:
                with mock.patch.object(_ar, "measure_audio",
                                       return_value={"tempo_bpm": 100.0}) as m_measure, \
                     mock.patch.object(_ar, "ffprobe_info",
                                       return_value={"duration_s": 4}), \
                     mock.patch.object(_ar, "collect_tool_versions",
                                       return_value={}):
                    result = _ar.analyze_audio_ref(
                        str(root / "in.wav"), modes=["features"])
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = old_root, old_index
        m_measure.assert_called_once()
        self.assertEqual(result["measured"], {"tempo_bpm": 100.0})

    def test_melodic_stem_wins_ref_mid_drums_kept_separately(self):
        import tempfile
        from pathlib import Path

        def fake_bp(wav, midi_path, tempo=120.0):
            role = Path(midi_path).stem
            if role == "drums":
                _make_midi(midi_path,
                           [(36, t, t + 0.05)
                            for t in (i * 0.25 for i in range(12))])
                count = 12
            else:
                _make_midi(midi_path,
                           [(40, 0.0, 0.4), (43, 0.5, 0.9),
                            (45, 1.0, 1.4), (43, 1.5, 1.9)],
                           is_drum=False)
                count = 4
            return {"note_count": count, "pitch_min": 36,
                    "pitch_max": 45, "model_path_used": "fake"}

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_wav(root / "in.wav", [0.0] * 2205)
            _write_wav(root / "drums.wav", [0.0] * 2205)
            _write_wav(root / "bass.wav", [0.0] * 2205)
            old_root, old_index = _ar.OUT_ROOT, _ar.INDEX_PATH
            _ar.OUT_ROOT, _ar.INDEX_PATH = root / "refs", root / "index.json"
            try:
                with mock.patch.object(_ar, "run_basic_pitch",
                                       side_effect=fake_bp):
                    result = _ar.analyze_audio_ref(
                        str(root / "in.wav"), modes=["midi"],
                        stem_files={"drums": str(root / "drums.wav"),
                                    "bass": str(root / "bass.wav")})
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = old_root, old_index
            out = Path(result["out_dir"])
            # The drums stem has 3x the notes but must NOT become ref.mid.
            self.assertEqual(result["winning_stem"], "bass")
            self.assertEqual((out / "ref.mid").read_bytes(),
                             (out / "stems" / "bass.mid").read_bytes())
            self.assertTrue((out / "stems" / "drums.mid").is_file())
            self.assertIn("drums", result["stem_midi"])
            # The drums stem still feeds the measured grid.
            self.assertEqual(result["measured_drum_grid"]["method"],
                             "stem_midi_onsets")

    def test_midi_pitch_classes_from_synthetic_mid(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            mid = Path(td) / "x.mid"
            _make_midi(mid, [(60, 0.0, 0.4), (64, 0.5, 0.9),
                             (67, 1.0, 1.4), (72, 1.5, 1.9)],
                       is_drum=False)
            self.assertEqual(_ar.midi_pitch_classes(mid), ["C", "E", "G"])
        self.assertIsNone(_ar.midi_pitch_classes(Path(td) / "missing.mid"))

    def test_reconciler_mid_pitch_cross_check(self):
        from tests.test_relisten_v2_listener import _v3_card

        card = _v3_card()
        card["harmony"]["pitch_classes"] = ["C", "E", "G"]
        agree = _ar.reconcile_measured(
            {"tempo_bpm": 100.0}, card, focus="harmony",
            midi_pitch_classes=["C", "E", "G", "A"])
        self.assertEqual(agree["midipitch"]["status"], "agree")
        clash = _ar.reconcile_measured(
            {"tempo_bpm": 100.0}, card, focus="harmony",
            midi_pitch_classes=["F#", "C#"])
        self.assertEqual(clash["midipitch"]["status"], "disagree")
        self.assertIn("C", clash["midipitch"]["note"])
        bare = _ar.reconcile_measured(
            {"tempo_bpm": 100.0}, card, focus="harmony",
            midi_pitch_classes=None)
        self.assertEqual(bare["midipitch"]["status"], "model_only")
        # Groove claims say nothing about pitch.
        groove = _ar.reconcile_measured(
            {"tempo_bpm": 100.0}, card, focus="groove",
            midi_pitch_classes=["C", "E", "G"])
        self.assertNotIn("midipitch", groove)


class TestBatchSummaryShape(unittest.TestCase):
    """Item 15: the X2 summary shape keeps every field block 80 asserts."""

    def test_partial_row_exact_shape(self):
        self.assertEqual(
            _ar.summarize_batch_results([{"ok": False, "partial": True}]),
            {"ok": False, "partial": True, "ok_count": 0, "fail_count": 1,
             "listen_rejected_count": 0,
             "listen_tokens": {"items_with_usage": 0}})

    def test_listen_tokens_total_present_rows(self):
        out = _ar.summarize_batch_results([
            {"ok": True, "partial": False,
             "listen_usage": {"input_tokens": 10, "output_tokens": 5}},
            {"ok": False, "partial": True},
        ])
        self.assertTrue(out["partial"])
        self.assertEqual(out["ok_count"], 1)
        self.assertEqual(out["fail_count"], 1)
        self.assertEqual(out["listen_tokens"]["items_with_usage"], 1)
        self.assertEqual(out["listen_tokens"]["input_tokens"], 10)


@requires_store("audio_refs")
class TestItem16Low(unittest.TestCase):
    def test_compact_row_uses_ref_id_and_keeps_note(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            idx = Path(td) / "index.json"
            _ar.save_index({"version": 1, "refs": {
                "r-n": _r5_card("r-n", title="N", note="steal the snare"),
            }}, idx)
            (row,) = _ar.find_audio_refs(k=5, index_path=idx)
        self.assertEqual(row["ref_id"], "r-n")
        self.assertNotIn("id", row)
        self.assertEqual(row["ricardo_note"], "steal the snare")

    def test_find_docstring_matches_compact_view(self):
        import inspect

        doc = inspect.getdoc(_server.find_audio_refs) or ""
        self.assertIn("ref_id", doc)
        self.assertIn("ricardo_note", doc)
        self.assertNotIn("(id, title", doc)

    def test_record_judgment_writes_under_index_lock(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            idx = Path(td) / "index.json"
            _ar.save_index({"version": 1, "refs": {
                "r-j": _r3_card(),
            }}, idx)
            _ar.record_ref_judgment("r-j", "kept", index_path=idx)
            self.assertTrue(Path(str(idx) + ".lock").is_file())
            card = _ar.load_index(idx)["refs"]["r-j"]
            self.assertEqual(_ar.ref_tally_of(card)["kept"], 1)


if __name__ == "__main__":
    unittest.main()
