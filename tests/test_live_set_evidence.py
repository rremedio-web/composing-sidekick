"""Set evidence sidecar: indexed audio-ref card shape + synthetic real-path Live Sets."""
from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import audio_ref
from tools import live_set_reader
from tools.live_set_reader import read_live_set
from tests.live_set_fixtures import live_set_xml


class SetEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.refs = self.root / "audio_refs"
        self.out = self.refs / "ref-fixture"
        self.out.mkdir(parents=True)
        self.index = self.root / "audio_ref_index.json"
        self._write_index("other")
        self.set_file = self.root / "fixture.als"
        self.set_file.write_bytes(gzip.compress(live_set_xml(8)))
        self.sidecar = self.out / "set_analysis.json"

    def _write_index(self, ownership, *, out_dir=True):
        # Indexed audio-ref card shape (store/audio_ref_index.json "refs" row).
        card = {"ref_id": "r-fixture", "title": "Fixture", "identity": "Fixture",
                "ownership": ownership, "tags": []}
        if ownership is None:
            del card["ownership"]
        if out_dir:
            card["out_dir"] = str(self.out)
        self.index.write_text(json.dumps({"refs": {"r-fixture": card}}))

    def _attach(self, set_file=None, **kwargs):
        return live_set_reader.attach_set_evidence(
            "r-fixture", set_file or self.set_file, store_dir=self.root,
            index_path=self.index, **kwargs)

    def test_attach_and_read_only_summary_exposure(self):
        result = self._attach()
        self.assertEqual(result["evidence"], "set_file")
        self.assertEqual(result["ownership"], "other")
        self.assertEqual(result["version_match"], "unconfirmed")
        self.assertEqual(result["container_sha256"], live_set_reader._sha256(self.set_file))
        self.assertEqual(result["source_sha256"],
                         read_live_set(live_set_xml(8))[0]["xml_sha256"])
        self.assertNotIn("xml_sha256", result)
        self.assertEqual(json.loads(self.sidecar.read_text()), result)
        with mock.patch.object(audio_ref, "ensure_out_dir_contained", return_value=self.out):
            card = audio_ref.get_ref_card("r-fixture", index_path=self.index)
            (row,) = audio_ref.find_audio_refs(k=1, index_path=self.index)
        expected = {"available": True, "scenes": ["Intro", "Main", ""],
                    "loop_lengths_bars": [2.0, 3.0, 5.0, 16.0], "sidechain_count": 2,
                    "return_count": 1, "version_match": "unconfirmed"}
        self.assertEqual(card["set_evidence"], expected)
        self.assertEqual(row["set_evidence"], expected)

    def test_same_set_in_other_containers_is_the_same_source(self):
        self._attach()
        for name, data in (("fixture.xml", live_set_xml(8)),
                           ("fixture.alp", gzip.compress(b"pack" + live_set_xml(8) + b"end"))):
            with self.subTest(container=name):
                other = self.root / name
                other.write_bytes(data)
                result = self._attach(other)  # no --force needed: same parsed Set
                self.assertEqual(result["container_sha256"], live_set_reader._sha256(other))

    def test_attach_records_owner_confirmed_and_same_sha_reattach_keeps_it(self):
        self.assertEqual(self._attach(same_version_confirmed_by_owner=True)["version_match"],
                         "owner_confirmed")
        self.assertEqual(self._attach()["version_match"], "owner_confirmed")
        different = self.root / "other.als"
        different.write_bytes(gzip.compress(live_set_xml(8, tempo=90)))
        self.assertEqual(self._attach(different, force=True)["version_match"], "unconfirmed")

    def test_attach_refuses_ownership_the_owner_did_not_set(self):
        for ownership in (None, "unknown", "his"):
            with self.subTest(ownership=ownership):
                self._write_index(ownership)
                with self.assertRaisesRegex(ValueError, "ownership") as ctx:
                    self._attach()
                if ownership != "his":
                    self.assertIn("--ownership other", str(ctx.exception))
                    self.assertIn("tag_audio_ref", str(ctx.exception))
                self.assertFalse(self.sidecar.exists())

    def test_attach_refuses_a_different_source_without_force(self):
        self._attach()
        different = self.root / "other.als"
        different.write_bytes(gzip.compress(live_set_xml(8, tempo=90)))
        with self.assertRaisesRegex(ValueError, "--force"):
            self._attach(different)
        self.assertEqual(json.loads(self.sidecar.read_text())["tempo"], 100.0)

    def test_failed_attach_leaves_no_directory_or_partial_file(self):
        self._write_index("other", out_dir=False)
        target_dir = self.refs / "r-fixture"
        broken = self.root / "broken.als"
        broken.write_bytes(gzip.compress(live_set_xml(8))[:40])
        with self.assertRaises(ValueError):
            self._attach(broken)
        self.assertFalse(target_dir.exists())
        with mock.patch.object(audio_ref, "write_json_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self._attach()
        self.assertFalse(target_dir.exists())
        self._attach()
        self.assertEqual([p.name for p in target_dir.iterdir()], ["set_analysis.json"])

    def test_hooks_find_the_sidecar_for_cards_without_out_dir(self):
        self._write_index("other", out_dir=False)
        self._attach()
        self.assertTrue((self.refs / "r-fixture" / "set_analysis.json").is_file())
        card = audio_ref.get_ref_card("r-fixture", index_path=self.index)
        (row,) = audio_ref.find_audio_refs(k=1, index_path=self.index)
        self.assertEqual(card["set_evidence"]["sidechain_count"], 2)
        self.assertEqual(row["set_evidence"]["return_count"], 1)

    def test_compare_card_layers_with_set_matrix_is_diagnostic(self):
        # Card shape follows the verified v3 audio-ref record; Set shape follows
        # the reader output produced by a synthetic Ableton LiveSet.
        card = {
            "ref_id": "r-fixture", "ownership": "other", "listen_verified": True,
            "sections": [
                {"t": "00:00", "label": "Open", "layers_in": ["kick", "noise"], "layers_out": []},
                {"t": "00:32", "label": "Break", "layers_in": [], "layers_out": ["bass"]},
            ],
            "layers": [{"role": "drums", "timbre": "kick", "first_t": "00:00"},
                       {"role": "bass", "timbre": "sub bass", "first_t": "00:16"}],
        }
        set_data = {
            "scenes": ["Open", "Break", "End"],
            "version_match": "owner_confirmed",
            "tracks": [{"id": "1", "name": "Kick 1"}, {"id": "2", "name": "Sub Bass"},
                       {"id": "3", "name": "Guitar"}, {"id": "4", "name": "Piano"},
                       {"id": "5", "name": "custom rising"}],
            # Reader shape: scene_matrix keyed by track id. Track 4 has no
            # clips, so it is not a layer of this Set.
            "scene_matrix": {"1": {"name": "Kick 1", "label": "Kick 1", "loop_bars": [2, 2, None]},
                             "2": {"name": "Sub Bass", "label": "Sub Bass", "loop_bars": [None, 8, None]},
                             "3": {"name": "Guitar", "label": "Guitar", "loop_bars": [None, None, 4]},
                             "4": {"name": "Piano", "label": "Piano", "loop_bars": [None, None, None]},
                             "5": {"name": "custom rising", "label": "custom rising",
                                   "loop_bars": [None, 11, None]}},
        }
        result = live_set_reader.compare_card_to_set(card, set_data)
        self.assertEqual(result["label"], "DIAGNOSTIC ONLY")
        # role "drums" and timbre "kick" are read separately: both match.
        self.assertEqual(result["layers"]["matched"], ["bass", "drums", "kick"])
        self.assertEqual(result["layers"]["card_only"], ["noise"])
        # "custom" is not "tom", "rising" is not "sing": no role, kept as named.
        self.assertEqual(result["layers"]["set_only"], ["custom rising", "guitar"])
        self.assertEqual(result["section_count"], {"card": 2, "set": 3, "compatible": False})
        self.assertFalse(result["timing_claims"])

    def test_unconfirmed_version_comparison_cannot_label_mistakes(self):
        card = {"listen_verified": True, "ownership": "other", "sections": [
            {"layers_in": ["breath/vocal"], "layers_out": []}],
            "layers": [{"role": "breath/vocal"}]}
        set_data = {"scenes": ["Scene 1", "Scene 2"], "tracks": [{"id": "1", "name": "Kick"}],
                    "scene_matrix": {"1": {"name": "Kick", "label": "Kick", "loop_bars": [4, None]}}, "version_match": "unconfirmed"}
        result = live_set_reader.compare_card_to_set(card, set_data)
        self.assertEqual(result["version_match"], "unconfirmed")
        self.assertIn("version unconfirmed", result["warning"].lower())
        self.assertEqual(result["layers"], {"matched": [], "card_only": [], "set_only": []})
        self.assertIsNone(result["section_count"]["compatible"])


    def test_role_matcher_tokenises_on_word_boundaries(self):
        roles = live_set_reader._layer_roles
        self.assertEqual(roles("rising"), set())
        self.assertEqual(roles("custom"), set())
        self.assertEqual(roles("GtrDrone1"), {"guitar", "noise"})
        self.assertEqual(roles("Toms"), {"drums"})
        self.assertEqual(roles("BreathTake2"), {"breath/vocal"})
        self.assertEqual(roles("singing voice"), {"breath/vocal"})
        self.assertEqual(roles("Hat Loop"), {"drums"})
        card_roles = live_set_reader._card_layer_roles(
            {"role": "texture", "timbre": "close breath"})
        self.assertEqual(card_roles, {"noise", "breath/vocal"})
        self.assertEqual(live_set_reader._card_layer_roles({"role": "lead", "timbre": "brass"}),
                         {"brass"})


class CompareStoreTests(unittest.TestCase):
    """compare honours --store: a non-default store is not refused for
    living outside the repo's store/audio_refs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "store"
        out = self.store / "audio_refs" / "r-fixture"
        out.mkdir(parents=True)
        # Verified v3 card as indexed (listen_verified, sections with layers_in/out).
        (self.store / "audio_ref_index.json").write_text(json.dumps({"refs": {"r-fixture": {
            "ref_id": "r-fixture", "title": "Fixture", "ownership": "other",
            "out_dir": str(out), "listen_verified": True, "tags": [],
            "sections": [{"t": "00:00", "label": "Open", "layers_in": ["guitar noise"],
                          "layers_out": []}],
            "layers": [{"role": "drums", "timbre": "kick"}]}}}))
        set_file = Path(self.tmp.name) / "fixture.als"
        set_file.write_bytes(gzip.compress(live_set_xml(12)))
        live_set_reader.attach_set_evidence("r-fixture", set_file, store_dir=self.store,
                                            same_version_confirmed_by_owner=True)

    def test_compare_reads_card_and_sidecar_from_the_given_store(self):
        result = live_set_reader.compare_ref_to_set("r-fixture", store_dir=self.store)
        self.assertEqual(result["version_match"], "owner_confirmed")
        self.assertEqual(result["section_count"], {"card": 1, "set": 3, "compatible": False})
        self.assertEqual(result["layers"]["card_only"], ["drums", "guitar", "kick", "noise"])
        self.assertEqual(result["layers"]["set_only"], ["track a", "track b", "track c"])
        self.assertEqual(live_set_reader.main(["compare", "r-fixture", "--store", str(self.store)]), 0)

    def test_compare_refuses_a_suspect_ref(self):
        # get_ref_card's suspect quarantine held before compare read the store directly.
        index = self.store / "audio_ref_index.json"
        data = json.loads(index.read_text())
        data["refs"]["r-fixture"].update(suspect=True, suspect_reason="wrong upload")
        index.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "suspect"):
            live_set_reader.compare_ref_to_set("r-fixture", store_dir=self.store)

    def test_compare_refuses_an_out_dir_outside_the_given_store(self):
        index = self.store / "audio_ref_index.json"
        data = json.loads(index.read_text())
        data["refs"]["r-fixture"]["out_dir"] = str(Path(self.tmp.name))
        index.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "outside"):
            live_set_reader.compare_ref_to_set("r-fixture", store_dir=self.store)


if __name__ == "__main__":
    unittest.main()
