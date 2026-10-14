import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import audio_ref as ar
from tests._stage_passthrough import passthrough_listenable_staging


def _card(identity="heard"):
    return {
        "contract_version": ar.CARD_CONTRACT_VERSION,
        "identity": identity,
        "tempo": {"grid_bpm": 100.0, "feel_bpm": 50.0, "confidence": "med"},
        "meter": {"beats_per_bar": 4, "half_time": True},
        "swing": {"pct": 0.0, "confidence": "high"},
        "density": "sparse",
        "register": {"band": "low", "pitch_span": "C1-G2"},
        "harmony": {"modal_center": "E minor", "pitch_classes": ["E"],
                    "interval_content": ["P5"], "harmonic_rhythm_bars": 4.0,
                    "confidence": "med"},
        "melody": {"phrase_length_bars": 2.0, "contour": "static",
                   "call_response": "none", "motif_note_count": 2,
                   "repetition": "verbatim"},
        "drums": {"grid": [{"instrument": "kick", "steps": [0]}],
                  "confidence": "high"},
        "sections": [{"t": "00:01", "label": "entry", "event": "enter",
                      "density": "sparse", "register": "low", "harmony": None}],
        "imitation": {"do": ["hold the root"], "dont": ["busy the pocket"]},
        "uncertain": [],
    }


def _v3_card(identity="heard"):
    return {
        "contract_version": ar.V3_CONTRACT_VERSION,
        "identity": identity,
        "tempo": {"grid_bpm": 100.0, "feel_bpm": 50.0, "confidence": "high",
                  "evidence_t": None},
        "meter": {"beats_per_bar": 4, "half_time": True},
        "swing": {"pct": 0.0, "confidence": "high", "evidence_t": None},
        "density": "sparse",
        "register": {"band": "low", "pitch_span": "C1-G2"},
        "harmony": {"modal_center": "E minor", "pitch_classes": ["E"],
                    "confidence": "high", "evidence_t": None},
        "melody": {"phrase_length_bars": 2.0, "repetition": "verbatim"},
        "drums": {"grid": [{"instrument": "kick", "steps": [0]}],
                  "confidence": "high", "evidence_t": None},
        "sections": [{"t": "00:01", "label": "entry", "energy": 2,
                      "layers_in": ["kick"], "layers_out": [],
                      "transition_in": None}],
        "layers": [{"role": "drums", "timbre": "dry kick",
                    "first_t": "00:01"}],
        "low_end": {"kick_bass_relation": "kick sits above the bass",
                    "sidechain_audible": False},
        "space": "tight room",
        "the_trick": {"what": "filter opens at the drop", "t": "00:30"},
        "imitation": {"do": [{"rule": "hold the root", "t": None,
                              "ableton_move": "one MIDI clip, no quantize",
                              "why": "anchors the pocket"}],
                      "dont": []},
        "uncertain": [],
    }


class RelistenV2ListenerTests(unittest.TestCase):
    def test_schema_ownership_is_operator_only(self):
        self.assertNotIn("ownership", ar.V2_SCHEMA["properties"])
        card = _card()
        ar.validate_card_v2(card)
        card["ownership"] = "his"
        with self.assertRaises(ValueError):
            ar.validate_card_v2(card)

    def test_v2_production_path_uses_staged_json_and_atomic_card(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wav = root / "source.wav"
            wav.write_bytes(b"RIFFfake")
            out = root / "ref"
            out.mkdir()
            captured = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = cmd
                if "--add-dir" in cmd:
                    captured["entries"] = sorted(os.listdir(cmd[cmd.index("--add-dir") + 1]))
                return ar.subprocess.CompletedProcess(cmd, 0, json.dumps(_card()), "")

            with passthrough_listenable_staging(), \
                 mock.patch.object(ar, "_which", return_value="agy"), \
                 mock.patch.object(ar, "_run", side_effect=fake_run):
                result = ar.run_agy_describe_v2(
                    wav=wav, focus="full", prompt_extra=None, duration_s=1, timeout_s=61,
                    model="gemini-3.8-flash-high"
                )
            self.assertTrue(result["ok"])
            self.assertEqual(captured["entries"], ["source.wav"])
            self.assertIn("--json-schema", captured["cmd"])
            self.assertEqual(
                captured["cmd"][captured["cmd"].index("--print-timeout") + 1], "2m"
            )
            ar._persist_v2_artifacts(out, result["card"])
            self.assertEqual(ar.card_from_out_dir(out)["identity"], "heard")

    def test_analyze_audio_ref_reaches_v3_listener(self):
        # New listens extract v3 (X4 flipped the default; v2 stays readable
        # via run_agy_describe_v2, covered by its own tests).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "refs" / "new"
            out.mkdir(parents=True)
            (out / "source.wav").write_bytes(b"RIFFfake")
            (out / "meta.json").write_text(json.dumps({"source": "old", "tags": ["project:x"]}))
            captured = {}
            convs = root / "convs"
            def fake_run(cmd, **kwargs):
                if "--add-dir" in cmd:
                    captured["cmd"] = cmd
                    captured["entries"] = sorted(os.listdir(cmd[cmd.index("--add-dir") + 1]))
                    # B3: the fake agent leaves view evidence for the staged
                    # path, and the envelope carries the conversation id.
                    from tests.test_agy_listen_verification import (
                        _staged_path_from_prompt, plant_listen_evidence)
                    plant_listen_evidence(
                        convs, "conv-v3",
                        _staged_path_from_prompt(cmd[cmd.index("--print") + 1]))
                    payload = dict(_v3_card())
                    payload["conversation_id"] = "conv-v3"
                    payload["usage"] = {"input_tokens": 100}
                    return ar.subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
                return ar.subprocess.CompletedProcess(cmd, 0, "", "")
            old_root, old_index = ar.OUT_ROOT, ar.INDEX_PATH
            ar.OUT_ROOT, ar.INDEX_PATH = root / "refs", root / "index.json"
            try:
                with passthrough_listenable_staging(), \
                     mock.patch.object(ar, "_which", return_value="agy"), \
                     mock.patch.object(ar, "_run", side_effect=fake_run), \
                     mock.patch.object(ar, "ffprobe_info", return_value={"duration_s": 1}), \
                     mock.patch.object(ar, "_version_first_line", return_value=None):
                    result = ar.analyze_audio_ref(from_dir=str(out), modes=["describe"],
                                                  model="model-v2", tags=["batch:b1"],
                                                  conversations_dir=convs)
                self.assertEqual(captured["entries"], ["source.wav"])
                self.assertIn("--json-schema", captured["cmd"])
                self.assertTrue(result["mode_ok"]["describe"])
                self.assertEqual(result["contract_version"], ar.V3_CONTRACT_VERSION)
                self.assertTrue((out / "card.json").is_file())
                saved = json.loads((root / "index.json").read_text())["refs"]["new"]
                self.assertEqual(saved["identity"], "heard")
                self.assertEqual(saved["tags"], ["project:x", "batch:b1"])
                self.assertEqual(json.loads((out / "meta.json").read_text())["contract_version"], ar.V3_CONTRACT_VERSION)
            finally:
                ar.OUT_ROOT, ar.INDEX_PATH = old_root, old_index

    def test_stale_and_invalid_failures_keep_last_good_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "ref"
            out.mkdir()
            (out / "source.wav").write_bytes(b"RIFFfake")
            good = _card("LAST GOOD")
            ar._persist_v2_artifacts(out, good)
            old_card = (out / "card.json").read_bytes()
            old_text = (out / "describe.txt").read_bytes()
            meta = {"source": "old", "source_kind": "file", "agy_model": "old-model",
                    "ownership": "his", "features": {"duration_s": 2}}
            (out / "meta.json").write_text(json.dumps(meta))
            previous = ar.OUT_ROOT
            ar.OUT_ROOT = out.parent
            try:
                with mock.patch.object(ar, "run_agy_describe_v3", return_value={
                "ok": False, "error": "invalid v3 card", "model": "new-model",
                "timeout_s": 60, "argv": ["agy"],
                }), mock.patch.object(ar, "ffprobe_info", return_value={"duration_s": 2}), \
                     mock.patch.object(ar, "collect_tool_versions", return_value={}):
                    result = ar.analyze_audio_ref(from_dir=str(out), modes=["describe"],
                                                  model="new-model", timeout_s=60)
            finally:
                ar.OUT_ROOT = previous
            self.assertFalse(result["ok"])
            self.assertIsNone(result["describe_excerpt"])
            self.assertIsNone(result.get("structure"))
            self.assertEqual(result["agy_model"], "old-model")
            self.assertEqual((out / "card.json").read_bytes(), old_card)
            self.assertEqual((out / "describe.txt").read_bytes(), old_text)
            saved = json.loads((out / "meta.json").read_text())
            self.assertEqual(saved["analysis_attempts"][-1]["model"], "new-model")
            self.assertEqual(ar.card_from_out_dir(out)["identity"], "LAST GOOD")

    def test_windowed_from_dir_reanalyze_keeps_previous_measured(self):
        # A start/end re-analyze slices the file: file-zero DSP would
        # describe the slice, so the previous full-track measurement rides
        # through instead of becoming None.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            refs = root / "refs"
            refs.mkdir()
            out = refs / "r-w"
            out.mkdir()
            (out / "source.wav").write_bytes(b"RIFFfake")
            prev_measured = {"energy_per_bar": [1.0, 2.0],
                             "energy_bar_start_s": [0.0, 1.5],
                             "bar_len_s": 1.5, "tempo_bpm": 100.0}
            (out / "meta.json").write_text(json.dumps({
                "source": "file", "source_kind": "file",
                "measured": prev_measured}))
            old_root, old_index = ar.OUT_ROOT, ar.INDEX_PATH
            ar.OUT_ROOT, ar.INDEX_PATH = refs, root / "index.json"
            try:
                with mock.patch.object(ar, "ffprobe_info",
                                       return_value={"duration_s": 30}):
                    result = ar.analyze_audio_ref(
                        from_dir=str(out), modes=["features"],
                        start="00:10", end="00:20")
            finally:
                ar.OUT_ROOT, ar.INDEX_PATH = old_root, old_index
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["measured"], prev_measured)
            saved = json.loads((out / "meta.json").read_text())
            self.assertEqual(saved["measured"], prev_measured)

    def _make_relisten_public_fixture(self, root):
        refs = root / "refs"
        refs.mkdir()
        out = refs / "ref"
        out.mkdir()
        (out / "source.wav").write_bytes(b"RIFFfake")
        old_card = _card("OLD")
        ar._persist_v2_artifacts(out, old_card)
        (out / "meta.json").write_text(json.dumps({
            "source": "old", "source_kind": "file", "agy_model": "old-model",
            "ownership": "his", "tags": ["project:old"]
        }))
        index = root / "index.json"
        index.write_text(json.dumps({"refs": {"ref": {
            "ref_id": "ref", "out_dir": str(out), "identity": "OLD",
            "ownership": "his", "tags": ["project:old"]
        }}}))
        return refs, out, index

    def _run_public_relisten(self, out, index, failure, modes=None):
        captured = {}
        # B3: analyze verifies every listen against the run's conversation
        # record, so the fake agent plants its view evidence for the staged
        # path it was told to open (parsed out of the prompt directive).
        convs = out.parent / "convs"

        def fake_run(cmd, **kwargs):
            if "--add-dir" in cmd:
                captured["entries"] = sorted(os.listdir(cmd[cmd.index("--add-dir") + 1]))
                from tests.test_agy_listen_verification import (
                    _staged_path_from_prompt, plant_listen_evidence)
                plant_listen_evidence(
                    convs, "conv-relisten",
                    _staged_path_from_prompt(cmd[cmd.index("--print") + 1]))
                payload = dict(_v3_card("NEW"))
                payload["conversation_id"] = "conv-relisten"
                payload["usage"] = {"input_tokens": 100}
                return ar.subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
            return ar.subprocess.CompletedProcess(cmd, 0, "", "")

        old_root, old_index = ar.OUT_ROOT, ar.INDEX_PATH
        ar.OUT_ROOT, ar.INDEX_PATH = out.parent, index
        try:
            with passthrough_listenable_staging(), \
                 mock.patch.object(ar, "_which", return_value="agy"), \
                 mock.patch.object(ar, "_run", side_effect=fake_run), \
                 mock.patch.object(ar, "ffprobe_info", return_value={"duration_s": 1}), \
                 mock.patch.object(ar, "_version_first_line", return_value=None), \
                 failure:
                result = ar.analyze_audio_ref(
                    from_dir=str(out), modes=modes or ["describe"], model="new-model",
                    conversations_dir=convs,
                )
        finally:
            ar.OUT_ROOT, ar.INDEX_PATH = old_root, old_index
        return result, captured

    def test_relisten_public_path_rolls_back_on_metadata_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _refs, out, index = self._make_relisten_public_fixture(root)
            before = {name: (out / name).read_bytes() for name in
                      ("card.json", "describe.txt", "meta.json")}
            before["index.json"] = index.read_bytes()
            original_write = ar.write_json_atomic

            def fail_meta(path, data):
                if Path(path).name == "meta.json":
                    raise OSError("injected metadata publication failure")
                return original_write(path, data)

            result, captured = self._run_public_relisten(
                out, index, mock.patch.object(ar, "write_json_atomic", side_effect=fail_meta)
            )
            self.assertFalse(result["ok"])
            self.assertEqual(captured["entries"], ["source.wav"])
            self.assertEqual(index.read_bytes(), before["index.json"])
            for name, payload in before.items():
                if name != "index.json":
                    self.assertEqual((out / name).read_bytes(), payload)

    def test_rejected_relisten_keeps_validated_card_and_index(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _refs, out, index = self._make_relisten_public_fixture(root)
            old_card = (out / "card.json").read_bytes()
            old_describe = (out / "describe.txt").read_bytes()
            old_index = index.read_bytes()
            unverified = _v3_card("UNVERIFIED")
            unverified["ownership"] = "model supplied"
            rejected = {
                "ok": False, "card": unverified,
                "listen_rejected": {"reason": "no_listen"},
                "error": "listen rejected: no_listen",
            }
            old_root, old_index_path = ar.OUT_ROOT, ar.INDEX_PATH
            ar.OUT_ROOT, ar.INDEX_PATH = out.parent, index
            try:
                with mock.patch.object(ar, "ffprobe_info", return_value={"duration_s": 1}), \
                     mock.patch.object(ar, "collect_tool_versions", return_value={}), \
                     mock.patch.object(ar, "run_agy_describe_v3", return_value=rejected):
                    result = ar.analyze_audio_ref(from_dir=str(out), modes=["describe"])
            finally:
                ar.OUT_ROOT, ar.INDEX_PATH = old_root, old_index_path

            self.assertFalse(result["mode_ok"]["describe"])
            self.assertEqual((out / "card.json").read_bytes(), old_card)
            self.assertEqual((out / "describe.txt").read_bytes(), old_describe)
            self.assertEqual(index.read_bytes(), old_index)
            saved_unverified = json.loads((out / "card.unverified.json").read_text())
            self.assertEqual(saved_unverified["identity"], "UNVERIFIED")
            self.assertNotIn("ownership", saved_unverified)
            self.assertTrue((out / "describe.unverified.txt").is_file())

    def test_rejected_relisten_leaves_canonical_provenance_untouched(self):
        # Rejected-relisten scenario from the round-2 review probe
        # (/tmp/cs-rev-b4-2/rejected_leak.py): the previous meta carries a
        # verified listen, and the rejected run brings an unverified card
        # whose grid (173 BPM) disagrees with measurement. Canonical
        # conversation_id / listen_verification / reconciliation / hashes
        # must stay the previous run's; the rejected values live only
        # under meta["listen_rejected"]; the index uncertain view must
        # never cite the unverified card.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _refs, out, index = self._make_relisten_public_fixture(root)
            prev_meta = json.loads((out / "meta.json").read_text())
            prev_meta.update({
                "listen_verified": True,
                "conversation_id": "conv-good",
                "listen_verification": {"verified": True, "ok": True},
                # Shape of the record reconcile_measured writes: per-claim
                # status with measured vs card values.
                "reconciliation": {
                    "focus": "full",
                    "tempo": {"status": "agree", "measured_bpm": 100.0,
                              "card_grid_bpm": 100.0, "card_feel_bpm": 50.0,
                              "uses_bpm": 100.0, "note": None},
                },
                "hashes": {"source_sha256": "s",
                           "card_sha256": "c", "describe_sha256": "d"},
            })
            (out / "meta.json").write_text(json.dumps(prev_meta))
            unverified = _v3_card("UNVERIFIED")
            unverified["tempo"]["grid_bpm"] = 173.0
            rejected = {
                "ok": False, "card": unverified,
                "listen_rejected": {"reason": "no_listen",
                                    "conversation_id": "conv-bad"},
                "error": "listen rejected: no_listen",
                "conversation_id": "conv-bad",
                "listen_verification": {"verified": False, "ok": False,
                                        "reason": "no_listen"},
                "reconciliation": {
                    "focus": "full",
                    "tempo": {"status": "disagree", "measured_bpm": 120,
                              "card_grid_bpm": 173.0, "card_feel_bpm": 86.5,
                              "uses_bpm": 120, "note": None},
                },
            }
            old_root, old_index_path = ar.OUT_ROOT, ar.INDEX_PATH
            ar.OUT_ROOT, ar.INDEX_PATH = out.parent, index
            try:
                with mock.patch.object(ar, "ffprobe_info",
                                       return_value={"duration_s": 1}), \
                     mock.patch.object(ar, "collect_tool_versions",
                                       return_value={}), \
                     mock.patch.object(ar, "run_agy_describe_v3",
                                       return_value=rejected):
                    result = ar.analyze_audio_ref(from_dir=str(out),
                                                  modes=["describe"])
            finally:
                ar.OUT_ROOT, ar.INDEX_PATH = old_root, old_index_path
            self.assertFalse(result["mode_ok"]["describe"])
            saved = json.loads((out / "meta.json").read_text())
            self.assertTrue(saved["listen_verified"])
            self.assertEqual(saved["conversation_id"], "conv-good")
            self.assertEqual(saved["listen_verification"],
                             {"verified": True, "ok": True})
            self.assertEqual(saved["reconciliation"],
                             prev_meta["reconciliation"])
            self.assertEqual(saved["hashes"]["card_sha256"], "c")
            self.assertEqual(saved["hashes"]["describe_sha256"], "d")
            kept = saved["listen_rejected"]
            self.assertEqual(kept["reason"], "no_listen")
            self.assertEqual(kept["conversation_id"], "conv-bad")
            self.assertEqual(kept["listen_verification"],
                             {"verified": False, "ok": False,
                              "reason": "no_listen"})
            self.assertEqual(kept["reconciliation"],
                             rejected["reconciliation"])
            row = ar.card_from_out_dir(out, saved)
            self.assertEqual(row["identity"], "OLD")
            self.assertNotIn("173", json.dumps(row.get("uncertain") or []))
            indexed = json.loads(index.read_text())["refs"]["ref"]
            self.assertEqual(indexed["identity"], "OLD")
            self.assertNotIn("173", json.dumps(indexed.get("uncertain") or []))

    def test_rejected_relisten_sidecars_join_rollback_snapshot(self):
        # The unverified sidecars are part of the relisten publication
        # unit: a storage failure after they land must restore their
        # exact prior bytes (present or absent), like the canonical
        # card/describe pair.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _refs, out, index = self._make_relisten_public_fixture(root)
            (out / "card.unverified.json").write_bytes(b'{"old": true}\n')
            (out / "describe.unverified.txt").write_bytes(b"old\n")
            original_write = ar.write_json_atomic

            def fail_meta(path, data):
                if Path(path).name == "meta.json":
                    raise OSError("injected metadata publication failure")
                return original_write(path, data)

            unverified = _v3_card("UNVERIFIED")
            rejected = {
                "ok": False, "card": unverified,
                "listen_rejected": {"reason": "no_listen"},
                "error": "listen rejected: no_listen",
            }
            old_root, old_index_path = ar.OUT_ROOT, ar.INDEX_PATH
            ar.OUT_ROOT, ar.INDEX_PATH = out.parent, index
            try:
                with mock.patch.object(ar, "ffprobe_info",
                                       return_value={"duration_s": 1}), \
                     mock.patch.object(ar, "collect_tool_versions",
                                       return_value={}), \
                     mock.patch.object(ar, "run_agy_describe_v3",
                                       return_value=rejected), \
                     mock.patch.object(ar, "write_json_atomic",
                                       side_effect=fail_meta):
                    result = ar.analyze_audio_ref(from_dir=str(out),
                                                  modes=["describe"])
            finally:
                ar.OUT_ROOT, ar.INDEX_PATH = old_root, old_index_path
            self.assertFalse(result["ok"])
            self.assertEqual((out / "card.unverified.json").read_bytes(),
                             b'{"old": true}\n')
            self.assertEqual((out / "describe.unverified.txt").read_bytes(),
                             b"old\n")

    def test_relisten_public_path_rolls_back_on_index_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _refs, out, index = self._make_relisten_public_fixture(root)
            before = {name: (out / name).read_bytes() for name in
                      ("card.json", "describe.txt", "meta.json")}
            before_index = index.read_bytes()

            def fail_index(*_args, **_kwargs):
                raise OSError("injected index publication failure")

            result, _captured = self._run_public_relisten(
                out, index, mock.patch.object(ar, "save_index", side_effect=fail_index)
            )
            self.assertFalse(result["ok"])
            self.assertEqual(index.read_bytes(), before_index)
            for name, payload in before.items():
                self.assertEqual((out / name).read_bytes(), payload)

    def test_relisten_index_failure_rolls_back_features_atomically(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _refs, out, index = self._make_relisten_public_fixture(root)
            features = out / "features.json"
            features.write_bytes(b'{"old": true}\n')
            before = {
                name: (out / name).read_bytes()
                for name in ("card.json", "describe.txt", "features.json", "meta.json")
            }
            before_index = index.read_bytes()

            def fail_index(*_args, **_kwargs):
                raise OSError("injected index publication failure")

            result, _captured = self._run_public_relisten(
                out, index, mock.patch.object(ar, "save_index", side_effect=fail_index),
                modes=["describe", "features"]
            )
            self.assertFalse(result["ok"])
            self.assertEqual(index.read_bytes(), before_index)
            for name, payload in before.items():
                self.assertEqual((out / name).read_bytes(), payload)

    def test_invalid_card_reindex_keeps_previous_card_and_reports_error(self):
        with tempfile.TemporaryDirectory() as td:
            root, index = Path(td) / "refs", Path(td) / "index.json"
            root.mkdir(); out = root / "ref"; out.mkdir()
            (out / "source.wav").write_bytes(b"wav")
            (out / "card.json").write_text("{not-json")
            index.write_text(json.dumps({"refs": {"ref": {"ref_id": "ref",
                "identity": "LAST GOOD", "ownership": "his"}}}))
            old_root = ar.OUT_ROOT; ar.OUT_ROOT = root
            try:
                report = ar.reindex_audio_refs(root=root, index_path=index)
            finally:
                ar.OUT_ROOT = old_root
            self.assertFalse(report["ok"])
            self.assertEqual(json.loads(index.read_text())["refs"]["ref"]["identity"], "LAST GOOD")
            self.assertEqual(report["invalid_cards"][0]["ref_id"], "ref")
            with self.assertRaises(ValueError):
                ar.card_from_out_dir(out)

    def _make_reindex_transaction_fixture(self, root):
        refs, index = root / "refs", root / "index.json"
        refs.mkdir()
        out = refs / "ref"
        out.mkdir()
        (out / "source.wav").write_bytes(b"wav")
        (out / "describe.txt").write_text(
            "# Identity\nA real old brief with enough content to remain valid.\n"
            "\n# Groove\n- sparse pocket\n"
        )
        (out / "meta.json").write_text(json.dumps({"ownership": "his", "old": True}))
        index.write_text(json.dumps({"version": 1, "refs": {"ref": {
            "ref_id": "ref", "out_dir": str(out), "ownership": "other",
            "identity": "OLD", "tags": ["old"]
        }}}))
        return refs, out, index

    def test_reindex_rolls_back_metadata_and_index_on_metadata_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            refs, out, index = self._make_reindex_transaction_fixture(root)
            before_meta, before_index = (out / "meta.json").read_bytes(), index.read_bytes()
            original_write = ar.write_json_atomic

            def fail_meta(path, data):
                if Path(path).name == "meta.json":
                    raise OSError("injected reindex metadata failure")
                return original_write(path, data)

            old_root = ar.OUT_ROOT
            ar.OUT_ROOT = refs
            try:
                with mock.patch.object(ar, "write_json_atomic", side_effect=fail_meta):
                    with self.assertRaises(OSError):
                        ar.reindex_audio_refs(root=refs, index_path=index)
            finally:
                ar.OUT_ROOT = old_root
            self.assertEqual((out / "meta.json").read_bytes(), before_meta)
            self.assertEqual(index.read_bytes(), before_index)

    def test_reindex_rolls_back_metadata_and_index_on_index_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            refs, out, index = self._make_reindex_transaction_fixture(root)
            before_meta, before_index = (out / "meta.json").read_bytes(), index.read_bytes()

            old_root = ar.OUT_ROOT
            ar.OUT_ROOT = refs
            try:
                with mock.patch.object(ar, "save_index", side_effect=OSError("injected reindex index failure")):
                    with self.assertRaises(OSError):
                        ar.reindex_audio_refs(root=refs, index_path=index)
            finally:
                ar.OUT_ROOT = old_root
            self.assertEqual((out / "meta.json").read_bytes(), before_meta)
            self.assertEqual(index.read_bytes(), before_index)

    def test_reindex_rolls_back_new_metadata_file_on_index_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            refs, out, index = self._make_reindex_transaction_fixture(root)
            (out / "meta.json").unlink()
            before_index = index.read_bytes()
            old_root = ar.OUT_ROOT
            ar.OUT_ROOT = refs
            try:
                with mock.patch.object(ar, "save_index", side_effect=OSError("injected index failure")):
                    with self.assertRaises(OSError):
                        ar.reindex_audio_refs(root=refs, index_path=index)
            finally:
                ar.OUT_ROOT = old_root
            self.assertFalse((out / "meta.json").exists())
            self.assertEqual(index.read_bytes(), before_index)

    def test_ownership_setter_and_reindex_preserve_operator_value(self):
        with tempfile.TemporaryDirectory() as td:
            root, index = Path(td) / "refs", Path(td) / "index.json"
            root.mkdir()
            out = root / "parent"
            out.mkdir()
            (out / "source.wav").write_bytes(b"RIFFfake")
            (out / "describe.txt").write_text("# Identity\nparent\n\n# Groove\n- sparse\n")
            (out / "meta.json").write_text(json.dumps({"ownership": "his", "tags": ["mine"]}))
            old = {"version": 1, "refs": {"parent": {"ref_id": "parent",
                    "ownership": "his", "tags": ["mine"]}}}
            index.write_text(json.dumps(old))
            previous = ar.OUT_ROOT
            try:
                ar.OUT_ROOT = root
                report = ar.reindex_audio_refs(root=root, index_path=index)
            finally:
                ar.OUT_ROOT = previous
            card = json.loads(index.read_text())["refs"]["parent"]
            self.assertEqual(card["ownership"], "his")
            self.assertEqual(json.loads((out / "meta.json").read_text())["ownership"], "his")
            self.assertEqual(report["count"], 1)

    def test_operator_ownership_setter_updates_index_and_meta(self):
        with tempfile.TemporaryDirectory() as td:
            root, index = Path(td) / "refs", Path(td) / "index.json"
            root.mkdir()
            out = root / "ref"; out.mkdir()
            (out / "source.wav").write_bytes(b"RIFFfake")
            (out / "meta.json").write_text(json.dumps({"tags": ["old"]}))
            index.write_text(json.dumps({"refs": {"ref": {"ref_id": "ref",
                "out_dir": str(out), "tags": ["old"], "ownership": "other"}}}))
            previous = ar.OUT_ROOT
            ar.OUT_ROOT = root
            try:
                result = ar.tag_audio_ref("ref", ownership="his", index_path=index)
            finally:
                ar.OUT_ROOT = previous
            self.assertEqual(result["ownership"], "his")
            self.assertEqual(json.loads(index.read_text())["refs"]["ref"]["ownership"], "his")
            self.assertEqual(json.loads((out / "meta.json").read_text())["ownership"], "his")

    def test_parent_tags_and_ownership_for_all_null_parent_moment_cases(self):
        with tempfile.TemporaryDirectory() as td:
            root, index = Path(td) / "refs", Path(td) / "index.json"
            root.mkdir()
            parent_id = "yt-parent_20260903-010101"
            parent = root / parent_id
            parent.mkdir()
            (parent / "source.wav").write_bytes(b"RIFFfake")
            (parent / "describe.txt").write_text("# Identity\nparent\n")
            (parent / "meta.json").write_text(json.dumps({"ownership": "his",
                "tags": ["project:mine", "keep"]}))
            refs = {parent_id: {"ref_id": parent_id, "ownership": "his",
                    "tags": ["project:mine", "keep"]}}
            child_ids = [f"{parent_id}_m{i}_20260903-020{i:03d}" for i in range(11)]
            for cid in child_ids:
                child = root / cid
                child.mkdir()
                (child / "source.wav").write_bytes(b"RIFFfake")
                (child / "describe.txt").write_text("# Identity\nmoment\n")
                (child / "meta.json").write_text(json.dumps({"parent_ref_id": None,
                    "tags": ["moment"], "ownership": "unknown"}))
                refs[cid] = {"ref_id": cid, "parent_ref_id": None,
                             "ownership": "unknown", "tags": ["moment"]}
            index.write_text(json.dumps({"version": 1, "refs": refs}))
            previous = ar.OUT_ROOT
            try:
                ar.OUT_ROOT = root
                ar.reindex_audio_refs(root=root, index_path=index)
            finally:
                ar.OUT_ROOT = previous
            fresh = json.loads(index.read_text())["refs"]
            for cid in child_ids:
                self.assertEqual(fresh[cid]["ownership"], "his")
                self.assertEqual(fresh[cid]["parent_ref_id"], parent_id)
                self.assertEqual(fresh[cid]["tags"], ["moment"])

    def test_tag_only_child_is_not_parent_inferred(self):
        with tempfile.TemporaryDirectory() as td:
            root, index = Path(td) / "refs", Path(td) / "index.json"
            root.mkdir(); parent = root / "p"; parent.mkdir()
            child = root / "unrelated"; child.mkdir()
            for out in (parent, child):
                (out / "source.wav").write_bytes(b"wav")
                (out / "describe.txt").write_text("# Identity\nx\n")
            (parent / "meta.json").write_text(json.dumps({"ownership": "his", "tags": ["parent"]}))
            (child / "meta.json").write_text(json.dumps({"parent_ref_id": None,
                "ownership": "other", "tags": ["moment"]}))
            index.write_text(json.dumps({"refs": {"p": {"ref_id": "p", "ownership": "his",
                "tags": ["parent"]}, "unrelated": {"ref_id": "unrelated",
                "ownership": "other", "tags": ["moment"]}}}))
            old_root = ar.OUT_ROOT; ar.OUT_ROOT = root
            try:
                ar.reindex_audio_refs(root=root, index_path=index)
            finally:
                ar.OUT_ROOT = old_root
            self.assertEqual(json.loads(index.read_text())["refs"]["unrelated"]["ownership"], "other")
            self.assertEqual(json.loads(index.read_text())["refs"]["unrelated"]["tags"], ["moment"])

    @unittest.skipUnless((Path(__file__).parents[1] / "tools/data/audio_ref_ownership.json").exists(),
                         "needs the local ref-ownership registry (owner data, not in the repo)")
    def test_restore_ownership_uses_exact_registry_shape(self):
        registry = json.loads((Path(__file__).parents[1] / "tools/data/audio_ref_ownership.json").read_text())
        with tempfile.TemporaryDirectory() as td:
            root, index = Path(td) / "refs", Path(td) / "index.json"
            root.mkdir()
            his = [x["ref_id"] for x in registry["his_parents"]] + registry["his_moment_children"]
            ids = his + [f"other-{i}" for i in range(registry["counts"]["other"])]
            refs = {}
            for rid in ids:
                out = root / rid; out.mkdir()
                (out / "meta.json").write_text("{}")
                refs[rid] = {"ref_id": rid, "out_dir": str(out), "ownership": "unknown"}
            index.write_text(json.dumps({"version": 1, "refs": refs}))
            old_root = ar.OUT_ROOT; ar.OUT_ROOT = root
            try:
                result = ar.restore_ownership(registry_path=Path(__file__).parents[1] / "tools/data/audio_ref_ownership.json",
                                              index_path=index)
            finally:
                ar.OUT_ROOT = old_root
            self.assertEqual((result["his"], result["other"], result["total"]), (32, 152, 184))
            fresh = json.loads(index.read_text())["refs"]
            self.assertTrue(all(fresh[rid]["ownership"] == "his" for rid in his))
            self.assertTrue(all(fresh[rid]["ownership"] == "other" for rid in ids if rid not in his))
            self.assertTrue(all(json.loads((root / rid / "meta.json").read_text())["ownership"] == fresh[rid]["ownership"] for rid in ids))

    def test_restore_malformed_meta_fails_before_any_write(self):
        registry = {"counts": {"total": 1, "his": 1, "other": 0},
                    "his_parents": [{"ref_id": "p"}], "his_moment_children": []}
        with tempfile.TemporaryDirectory() as td:
            root, index, reg = Path(td) / "refs", Path(td) / "index.json", Path(td) / "r.json"
            root.mkdir(); out = root / "p"; out.mkdir()
            meta = out / "meta.json"; meta.write_text("{bad")
            index.write_text(json.dumps({"refs": {"p": {"ref_id": "p", "out_dir": str(out), "ownership": "other"}}}))
            reg.write_text(json.dumps(registry))
            old_root = ar.OUT_ROOT; ar.OUT_ROOT = root
            try:
                with self.assertRaises(Exception):
                    ar.restore_ownership(registry_path=reg, index_path=index)
            finally:
                ar.OUT_ROOT = old_root
            self.assertEqual(meta.read_text(), "{bad")
            self.assertEqual(json.loads(index.read_text())["refs"]["p"]["ownership"], "other")

    def test_restore_missing_out_dir_fails_before_any_write(self):
        registry = {"counts": {"total": 1, "his": 1, "other": 0},
                    "his_parents": [{"ref_id": "p"}], "his_moment_children": []}
        with tempfile.TemporaryDirectory() as td:
            index, reg = Path(td) / "index.json", Path(td) / "r.json"
            index.write_text(json.dumps({"refs": {"p": {
                "ref_id": "p", "ownership": "other"
            }}}))
            reg.write_text(json.dumps(registry))
            before = index.read_bytes()
            with self.assertRaisesRegex(ValueError, "missing out_dir"):
                ar.restore_ownership(registry_path=reg, index_path=index)
            self.assertEqual(index.read_bytes(), before)

    def test_restore_duplicate_resolved_targets_fails_before_any_write(self):
        registry = {"counts": {"total": 2, "his": 1, "other": 1},
                    "his_parents": [{"ref_id": "p"}], "his_moment_children": []}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "shared"
            out.mkdir()
            meta = out / "meta.json"
            meta.write_text(json.dumps({"ownership": "other"}))
            index, reg = root / "index.json", root / "r.json"
            index.write_text(json.dumps({"refs": {
                "p": {"ref_id": "p", "out_dir": str(out), "ownership": "other"},
                "o": {"ref_id": "o", "out_dir": str(out), "ownership": "his"},
            }}))
            reg.write_text(json.dumps(registry))
            before_index, before_meta = index.read_bytes(), meta.read_bytes()
            old_root = ar.OUT_ROOT
            ar.OUT_ROOT = root
            try:
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    ar.restore_ownership(registry_path=reg, index_path=index)
            finally:
                ar.OUT_ROOT = old_root
            self.assertEqual(index.read_bytes(), before_index)
            self.assertEqual(meta.read_bytes(), before_meta)

    def test_restore_mid_write_rolls_back_previous_metadata(self):
        registry = {"counts": {"total": 2, "his": 1, "other": 1},
                    "his_parents": [{"ref_id": "p"}], "his_moment_children": []}
        with tempfile.TemporaryDirectory() as td:
            root, index, reg = Path(td) / "refs", Path(td) / "index.json", Path(td) / "r.json"
            root.mkdir(); refs = {}
            for rid in ("p", "o"):
                out = root / rid; out.mkdir(); (out / "meta.json").write_text(json.dumps({"old": rid}))
                refs[rid] = {"ref_id": rid, "out_dir": str(out), "ownership": "unknown"}
            index.write_text(json.dumps({"refs": refs})); reg.write_text(json.dumps(registry))
            original_meta = {rid: (root / rid / "meta.json").read_bytes() for rid in refs}
            original_index = index.read_bytes()
            original_write = ar.write_json_atomic
            calls = [0]
            def fail_second(path, data):
                calls[0] += 1
                if calls[0] == 2:
                    raise OSError("injected metadata failure")
                return original_write(path, data)
            old_root = ar.OUT_ROOT; ar.OUT_ROOT = root
            try:
                with mock.patch.object(ar, "write_json_atomic", side_effect=fail_second):
                    with self.assertRaises(OSError):
                        ar.restore_ownership(registry_path=reg, index_path=index)
            finally:
                ar.OUT_ROOT = old_root
            self.assertEqual(index.read_bytes(), original_index)
            for rid, before in original_meta.items():
                self.assertEqual((root / rid / "meta.json").read_bytes(), before)

    def test_parent_tag_recovers_owner_without_changing_child_tags(self):
        with tempfile.TemporaryDirectory() as td:
            root, index = Path(td) / "refs", Path(td) / "index.json"
            root.mkdir()
            for rid in ("p", "child-not-inferable"):
                out = root / rid; out.mkdir(); (out / "source.wav").write_bytes(b"wav")
                (out / "describe.txt").write_text("# Identity\nx\n")
            (root / "p" / "meta.json").write_text(json.dumps({"ownership": "his"}))
            (root / "child-not-inferable" / "meta.json").write_text(json.dumps({
                "parent_ref_id": None, "ownership": "other", "tags": ["parent:p", "moment"]}))
            index.write_text(json.dumps({"refs": {
                "p": {"ref_id": "p", "ownership": "his"},
                "child-not-inferable": {"ref_id": "child-not-inferable", "ownership": "other",
                                         "tags": ["parent:p", "moment"]}}}))
            old_root = ar.OUT_ROOT; ar.OUT_ROOT = root
            try:
                ar.reindex_audio_refs(root=root, index_path=index)
            finally:
                ar.OUT_ROOT = old_root
            child = json.loads(index.read_text())["refs"]["child-not-inferable"]
            self.assertEqual(child["ownership"], "his")
            self.assertEqual(child["tags"], ["parent:p", "moment"])

    def test_new_moment_clip_emits_only_provenance_tags(self):
        import shutil
        with tempfile.TemporaryDirectory() as td:
            root, index = Path(td) / "refs", Path(td) / "index.json"
            root.mkdir()
            parent = root / "parent"
            parent.mkdir()
            shutil.copyfile(
                Path(__file__).parent / "fixtures/audio_ref/longarc_v3.json",
                parent / "card.json")
            (parent / "source.wav").write_bytes(b"wav")
            (parent / "describe.txt").write_text(
                "# Identity\nparent\n\n# Moments\n- 00:05 – motif\n"
            )
            index.write_text(json.dumps({"refs": {"parent": {
                "ref_id": "parent", "out_dir": str(parent), "title": "Parent",
                "duration_s": 30, "tags": ["batch:relisten", "project:mine", "verdict:keep"],
                "linked_projects": ["project-x"]
            }}}))

            def fake_ffmpeg(_src, dst, _start, _end, **kwargs):
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(b"wav")

            old_root = ar.OUT_ROOT
            ar.OUT_ROOT = root
            try:
                with mock.patch.object(ar, "ffmpeg_to_wav", side_effect=fake_ffmpeg):
                    result = ar.clip_moments(
                        "parent", modes=[], index_path=index, max_moments=1
                    )
            finally:
                ar.OUT_ROOT = old_root

            self.assertEqual(len(result["children"]), 1)
            child_id = result["children"][0]["ref_id"]
            child = json.loads(index.read_text())["refs"][child_id]
            self.assertEqual(child["tags"], ["moment", "parent:parent"])
            self.assertEqual(child["linked_projects"], ["project-x"])
            child_meta = json.loads((root / child_id / "meta.json").read_text())
            self.assertEqual(child_meta["tags"], ["moment", "parent:parent"])

    def _make_clip_fixture(self, root):
        import shutil
        refs, index = root / "refs", root / "index.json"
        refs.mkdir()
        parent = refs / "parent"
        parent.mkdir()
        shutil.copyfile(
            Path(__file__).parent / "fixtures/audio_ref/longarc_v3.json",
            parent / "card.json")
        (parent / "source.wav").write_bytes(b"wav")
        (parent / "describe.txt").write_text(
            "# Identity\nparent\n\n# Moments\n- 00:05 – motif\n"
        )
        index.write_text(json.dumps({"refs": {"parent": {
            "ref_id": "parent", "out_dir": str(parent), "title": "Parent",
            "duration_s": 30, "tags": ["batch:relisten", "project:mine"],
            "linked_projects": ["project-x"]
        }}}))
        return refs, index

    def test_new_moment_clip_failure_cleans_child_and_fails_function(self):
        for failure_kind in ("describe", "features", "metadata", "index"):
            with self.subTest(failure_kind=failure_kind), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                refs, index = self._make_clip_fixture(root)

                def fake_ffmpeg(_src, dst, _start, _end, **kwargs):
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(b"wav")

                # Keep the original atomic text writer available to the
                # selective fault injector without making a real subprocess.
                original_text = ar._write_text_atomic
                def selective_text(path, text):
                    if failure_kind == "describe" and Path(path).name == "describe.txt":
                        raise OSError("injected describe write failure")
                    if failure_kind == "features" and Path(path).name == "features.json":
                        raise OSError("injected features write failure")
                    return original_text(path, text)

                original_json = ar.write_json_atomic
                def selective_json(path, data):
                    if failure_kind == "metadata" and Path(path).name == "meta.json":
                        raise OSError("injected metadata write failure")
                    return original_json(path, data)

                def fail_index(*_args, **_kwargs):
                    raise OSError("injected index write failure")

                old_root = ar.OUT_ROOT
                ar.OUT_ROOT = refs
                try:
                    text_patch = mock.patch.object(ar, "_write_text_atomic", side_effect=selective_text)
                    json_patch = mock.patch.object(ar, "write_json_atomic", side_effect=selective_json)
                    index_patch = mock.patch.object(ar, "save_index", side_effect=fail_index)
                    with mock.patch.object(ar, "ffmpeg_to_wav", side_effect=fake_ffmpeg), \
                         text_patch, json_patch, index_patch:
                        result = ar.clip_moments(
                            "parent", modes=["features"] if failure_kind == "features" else [],
                            index_path=index, max_moments=1
                        )
                finally:
                    ar.OUT_ROOT = old_root
                self.assertFalse(result["ok"])
                self.assertEqual(len(result["children"]), 1)
                self.assertFalse(Path(result["children"][0]["out_dir"]).exists())

    def test_backup_captures_card_json(self):
        with tempfile.TemporaryDirectory() as td:
            root, index, dest = Path(td) / "refs", Path(td) / "index.json", Path(td) / "backups"
            root.mkdir(); dest.mkdir()
            out = root / "ref"; out.mkdir()
            (out / "meta.json").write_text("{}")
            (out / "describe.txt").write_text("brief")
            (out / "card.json").write_text(json.dumps(_card()))
            index.write_text(json.dumps({"refs": {}}))
            result = ar.backup_overlays(dest_dir=dest, root=root, index_path=index)
            self.assertEqual(result["file_count"], 4)
            import tarfile
            with tarfile.open(result["archive"]) as tar:
                self.assertTrue(any(name.endswith("/card.json") for name in tar.getnames()))
            self.assertIn(str(dest), result["restore"])

    def test_batch_timeout_precedence_item_manifest_function(self):
        with tempfile.TemporaryDirectory() as td:
            old_batch = ar.BATCH_ROOT
            ar.BATCH_ROOT = Path(td) / "batches"
            seen = []

            def fake_analyze(**kwargs):
                seen.append(kwargs["timeout_s"])
                return {"ok": True, "partial": False, "ref_id": "r", "out_dir": td,
                        "focus": "full", "warnings": [], "agy_model": "m",
                        "timeout_s": kwargs["timeout_s"], "contract_version": ar.CARD_CONTRACT_VERSION,
                        "hashes": {"source_sha256": "abc"},
                        "evidence": {"staged_audio_only": True, "argv": ["agy"]}}

            manifest = {"batch_id": "timeout-test", "defaults": {"timeout_s": 240},
                        "items": [{"source": "a", "timeout_s": 61}, {"source": "b"}]}
            try:
                with mock.patch.object(ar, "analyze_audio_ref", side_effect=fake_analyze):
                    report = ar.run_batch(manifest, resume=False, timeout_s=120)
            finally:
                ar.BATCH_ROOT = old_batch
            self.assertTrue(report["ok"])
            self.assertEqual(seen, [61, 240])
            self.assertEqual(report["results"][0]["agy_model"], "m")
            self.assertEqual(report["results"][0]["contract_version"], ar.CARD_CONTRACT_VERSION)
            self.assertEqual(len(report["manifest_sha256"]), 64)

    def test_resume_manifest_or_overlay_change_fails_closed_before_write(self):
        with tempfile.TemporaryDirectory() as td:
            old_batch = ar.BATCH_ROOT
            ar.BATCH_ROOT = Path(td) / "batches"
            manifest = {"batch_id": "resume-guard", "defaults": {"tags": ["old"]},
                        "items": [{"source": "a"}]}
            ar.BATCH_ROOT.mkdir()
            report_path = ar.BATCH_ROOT / "resume-guard.json"
            report_path.write_text(json.dumps({"manifest_sha256": ar.batch_manifest_hash(manifest),
                                               "results": []}))
            before = report_path.read_bytes()
            changed = {"batch_id": "resume-guard", "defaults": {"tags": ["new"]},
                       "items": [{"source": "a"}]}
            try:
                with self.assertRaises(ValueError):
                    ar.run_batch(changed, resume=True)
            finally:
                ar.BATCH_ROOT = old_batch
            self.assertEqual(report_path.read_bytes(), before)

    def test_mcp_reindex_backs_up_and_ownership_wrapper(self):
        from mcp_server import server
        fake = mock.Mock()
        fake.backup_overlays.return_value = {"ok": True, "archive": "a.tar.gz"}
        fake.reindex_audio_refs.return_value = {"ok": True, "count": 1}
        fake.restore_ownership.return_value = {"ok": True, "total": 1}
        with mock.patch.object(server, "_audio_ref_mod", return_value=fake):
            result = server.reindex_audio_refs()
            restored = server.restore_audio_ref_ownership()
        fake.backup_overlays.assert_called_once_with()
        self.assertEqual(result["overlay_backup"]["archive"], "a.tar.gz")
        self.assertEqual(restored["total"], 1)


if __name__ == "__main__":
    unittest.main()
