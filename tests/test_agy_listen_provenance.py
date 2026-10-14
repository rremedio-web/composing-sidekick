"""Lane B3 AGY-LISTENER item 6: card provenance.

Cards written by a verified listen get ``listen_verified: true`` +
conversation_id in meta.json; the flag rides into the index card and
both search views. Legacy cards that never ran a verifiable listen —
including --stamp-provenance's interactive chat-heard cards — read as
``listen_verified: false`` everywhere. Synthetic only: temp dirs,
fake runners.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import audio_ref as _ar
from tests.test_agy_listen_verification import (
    _staged_path_from_prompt,
    plant_listen_evidence,
)
from tests.test_agy_workspace_isolation import _v3_envelope
from tests._stage_passthrough import passthrough_listenable_staging
from tests.test_audio_ref_sequence import FIXTURES, load_json


def _verified_payload(conversation_id="conv-prov"):
    payload = _v3_envelope()
    payload["conversation_id"] = conversation_id
    payload["usage"] = {"input_tokens": 100}
    return payload


def _analyze_from_dir(root: Path, convs: Path, plant: bool,
                      runner_failure: bool = False,
                      previous_verified: bool = False):
    out = root / "refs" / "new"
    out.mkdir(parents=True)
    (out / "source.wav").write_bytes(b"RIFFfake")
    (out / "meta.json").write_text(json.dumps({
        "source": "old", "conversation_id": "conv-previous",
        "listen_verified": previous_verified}))

    def fake_run(cmd, **kwargs):
        if "--add-dir" in cmd:
            if runner_failure:
                return _ar.subprocess.CompletedProcess(
                    cmd, 1, "", "temporary listener failure")
            if plant:
                plant_listen_evidence(
                    convs, "conv-prov",
                    _staged_path_from_prompt(
                        cmd[cmd.index("--print") + 1]))
            return _ar.subprocess.CompletedProcess(
                cmd, 0, json.dumps(_verified_payload()), "")
        return _ar.subprocess.CompletedProcess(cmd, 0, "", "")

    old_root, old_index = _ar.OUT_ROOT, _ar.INDEX_PATH
    _ar.OUT_ROOT, _ar.INDEX_PATH = root / "refs", root / "index.json"
    try:
        with passthrough_listenable_staging(), \
             mock.patch.object(_ar, "_which", return_value="agy"), \
             mock.patch.object(_ar, "_run", side_effect=fake_run), \
             mock.patch.object(_ar, "ffprobe_info",
                               return_value={"duration_s": 60}), \
             mock.patch.object(_ar, "_version_first_line",
                               return_value=None):
            return _ar.analyze_audio_ref(
                from_dir=str(out), modes=["describe"],
                conversations_dir=convs)
    finally:
        _ar.OUT_ROOT, _ar.INDEX_PATH = old_root, old_index


class TestFreshListenProvenance(unittest.TestCase):
    def test_verified_listen_stamps_meta_index_and_search(self):
        with tempfile.TemporaryDirectory() as td:
            root, convs = Path(td), Path(td) / "convs"
            convs.mkdir()
            result = _analyze_from_dir(root, convs, plant=True)
            self.assertTrue(result["mode_ok"]["describe"])
            out = Path(result["out_dir"])
            saved = json.loads((out / "meta.json").read_text())
            self.assertTrue(saved["listen_verified"])
            self.assertEqual(saved["conversation_id"], "conv-prov")
            card = _ar.card_from_out_dir(out, saved)
            self.assertTrue(card["listen_verified"])
            row = _ar.compact_ref_view(card)
            self.assertTrue(row["listen_verified"])
            found = _ar.find_audio_refs(
                None, index_path=root / "index.json")
            self.assertTrue(found)
            self.assertTrue(all(r["listen_verified"] for r in found))

    def test_unverified_card_and_measurement_survive_without_view_proof(self):
        with tempfile.TemporaryDirectory() as td:
            root, convs = Path(td), Path(td) / "convs"
            convs.mkdir()
            measured = load_json(FIXTURES / "longarc_measured.json")
            with mock.patch.object(_ar, "measure_audio",
                                   return_value=measured):
                result = _analyze_from_dir(root, convs, plant=False)
            self.assertFalse(result["mode_ok"]["describe"])
            saved = json.loads(
                (Path(result["out_dir"]) / "meta.json").read_text())
            self.assertFalse(saved["listen_verified"])
            self.assertEqual(saved["measured"], measured)
            self.assertEqual(set(saved["measured"]), set(measured))
            # R4 (fix2 item 5): a rejected re-listen leaves canonical
            # provenance untouched — the previous (here empty) values are
            # carried, and the rejected run's proof rides only under
            # listen_rejected.
            self.assertIsNone(saved["listen_verification"])
            self.assertIsNone(saved["reconciliation"])
            self.assertEqual(saved["conversation_id"], "conv-previous")
            self.assertIsNotNone(saved["listen_rejected"])
            self.assertEqual(saved["listen_rejected"]["conversation_id"],
                             "conv-prov")
            self.assertFalse(
                saved["listen_rejected"]["listen_verification"]["verified"])
            self.assertIsNotNone(
                saved["listen_rejected"]["reconciliation"])
            out = Path(result["out_dir"])
            self.assertFalse((out / "card.json").exists())
            card = json.loads((out / "card.unverified.json").read_text())
            self.assertEqual(card["contract_version"],
                             _ar.V3_CONTRACT_VERSION)
            row = _ar.compact_ref_view(_ar.card_from_out_dir(
                out, saved))
            self.assertFalse(row["listen_verified"])
            self.assertTrue(row["has_measured"])
            self.assertNotEqual(row.get("contract_version"),
                                _ar.V3_CONTRACT_VERSION)

    def test_failed_non_rejected_relisten_keeps_previous_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            root, convs = Path(td), Path(td) / "convs"
            convs.mkdir()
            result = _analyze_from_dir(root, convs, plant=False,
                                       runner_failure=True,
                                       previous_verified=True)
            self.assertFalse(result["mode_ok"]["describe"])
            saved = json.loads((Path(result["out_dir"]) / "meta.json").read_text())
            self.assertIsNone(saved["listen_rejected"])
            self.assertEqual(saved["conversation_id"], "conv-previous")
            self.assertIs(saved["listen_verified"], True)


class TestLegacyProvenance(unittest.TestCase):
    def test_legacy_card_without_flag_reads_unverified(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "refs" / "old"
            out.mkdir(parents=True)
            (out / "source.wav").write_bytes(b"RIFFfake")
            (out / "meta.json").write_text(json.dumps({
                "source": "f", "source_kind": "file",
                "listener_mode": "interactive",
            }))
            card = _ar.card_from_out_dir(out)
            self.assertFalse(card["listen_verified"])
            self.assertFalse(
                _ar.compact_ref_view(card)["listen_verified"])

    def test_stamp_provenance_writes_unverified_false(self):
        from tests.test_relisten_v2_listener import _card as _v2_card

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "refs"
            heard = root / "heard"
            heard.mkdir(parents=True)
            (heard / "source.wav").write_bytes(b"RIFFfake")
            (heard / "card.json").write_text(json.dumps(_v2_card()))
            (heard / "meta.json").write_text(json.dumps({
                "source": "f", "source_kind": "file",
                "modes": ["describe", "midi"], "agy_model": "gemini-past"}))
            index = Path(td) / "index.json"
            index.write_text(json.dumps({
                "version": 1,
                "refs": {"heard": {"ref_id": "heard"}}}))
            dry = _ar.stamp_provenance(root=root, index_path=index)
            self.assertEqual(dry["stamped"], ["heard"])
            # Dry run writes nothing.
            self.assertNotIn(
                "listen_verified",
                json.loads((heard / "meta.json").read_text()))
            applied = _ar.stamp_provenance(
                root=root, index_path=index, apply=True)
            self.assertEqual(applied["stamped"], ["heard"])
            saved = json.loads((heard / "meta.json").read_text())
            self.assertEqual(saved["listener_mode"], "interactive")
            self.assertFalse(saved["listen_verified"])
            indexed = json.loads(index.read_text())["refs"]["heard"]
            self.assertEqual(indexed["listener_mode"], "interactive")
            self.assertFalse(indexed["listen_verified"])

    def test_upsert_preserves_provenance_overlay(self):
        with tempfile.TemporaryDirectory() as td:
            index = Path(td) / "index.json"
            _ar.save_index({"version": 1, "updated_at": None, "refs": {
                "r": {"ref_id": "r", "listen_verified": True}}}, index)
            fresh = {"ref_id": "r", "out_dir": str(Path(td) / "r"),
                     "tags": [], "linked_projects": []}
            with mock.patch.object(_ar, "OUT_ROOT", Path(td)):
                kept = _ar.upsert_index_card(dict(fresh), path=index)
            self.assertTrue(kept["listen_verified"])


if __name__ == "__main__":
    unittest.main()
