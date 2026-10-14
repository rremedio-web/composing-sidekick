"""Live Set reader on synthetic Sets with the element paths of real Live 8/11/12 files."""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.live_set_fixtures import live_set_xml, preset_xml
from tools.live_set_reader import read_live_set

REPO = Path(__file__).resolve().parents[1]


def _by_id(items):
    return {item["id"]: item for item in items}


def _device(track, dtype):
    return next(d for d in track["devices"] if d["type"] == dtype)


class RealPathReaderTests(unittest.TestCase):
    def test_transport_scenes_and_names_per_live_version(self):
        for version, creator in ((8, "Ableton Live 8.2.1"), (11, "Ableton Live 11.3.25"),
                                 (12, "Ableton Live 12.4.3")):
            with self.subTest(version=version):
                result = read_live_set(live_set_xml(version))[0]
                self.assertEqual(result["creator"], creator)
                self.assertEqual(result["tempo"], 100.0)
                self.assertEqual(result["time_signature"], {"numerator": 4, "denominator": 4})
                self.assertEqual(result["scenes"], ["Intro", "Main", ""])
                self.assertEqual(result["locators"], [{"time_beats": 16.0, "bar": 5.0, "name": "Mark"}])
                tracks = _by_id(result["tracks"])
                self.assertEqual([t["name"] for t in result["tracks"]],
                                 ["Group A", "Track A", "Track A", "Track B", "Track C"])
                self.assertEqual(tracks["10"]["kind"], "group")
                self.assertEqual(tracks["20"]["kind"], "midi")
                self.assertEqual(tracks["30"]["kind"], "audio")
                self.assertEqual((tracks["20"]["group_id"], tracks["20"]["group"]), ("10", "Group A"))
                self.assertIsNone(tracks["30"]["group_id"])
                self.assertIsNone(tracks["30"]["group"])

    def test_session_matrix_keyed_by_track_id_with_bars_from_signature(self):
        for version in (8, 11, 12):
            with self.subTest(version=version):
                result = read_live_set(live_set_xml(version))[0]
                matrix = result["scene_matrix"]
                self.assertEqual(sorted(matrix), ["20", "21", "30", "40"])
                self.assertEqual(matrix["20"]["loop_bars"], [2.0, 2.0, None])
                self.assertEqual(matrix["21"]["loop_bars"], [None, None, 5.0])
                self.assertEqual(matrix["30"]["loop_bars"], [3.0, None, None])
                self.assertEqual(matrix["40"]["loop_bars"], [None, 16.0, None])
                # Same display name, two tracks: the label disambiguates by id.
                self.assertEqual(matrix["20"]["name"], "Track A")
                self.assertEqual((matrix["20"]["label"], matrix["21"]["label"]),
                                 ("Track A [20]", "Track A [21]"))
                self.assertEqual(matrix["30"]["label"], "Track B")
                self.assertEqual(result["loop_lengths_bars"], [2.0, 3.0, 5.0, 16.0])
        odd = read_live_set(live_set_xml(12, time_signature=303))[0]  # 7/8
        self.assertEqual(odd["time_signature"], {"numerator": 7, "denominator": 8})
        self.assertEqual(odd["scene_matrix"]["20"]["loop_bars"][0], round(8 / 3.5, 4))

    def test_clip_fields_are_typed_and_named(self):
        for version in (8, 11, 12):
            with self.subTest(version=version):
                tracks = _by_id(read_live_set(live_set_xml(version))[0]["tracks"])
                (clip,) = tracks["30"]["session"]
                self.assertEqual(clip["scene_index"], 0)
                self.assertEqual(clip["clip_name"], "Air")
                self.assertIs(clip["loop_on"], True)
                self.assertEqual((clip["warp_mode"], clip["warp_mode_id"]), ("complex", 4))
                self.assertAlmostEqual(clip["gain"], 0.1, places=6)
                self.assertAlmostEqual(clip["gain_db"], -20.0, places=2)
                (other,) = tracks["40"]["session"]
                self.assertIs(other["loop_on"], False)
                self.assertEqual(other["warp_mode"], "beats")
                self.assertEqual(tracks["30"]["arrangement"],
                                 [{"start_bar": 1.0, "end_bar": 9.0, "name": "Air"}])
                self.assertIsNone(tracks["20"]["session"][0]["gain"])

    def test_mixer_volume_sends_and_track_on(self):
        for version in (8, 11, 12):
            with self.subTest(version=version):
                result = read_live_set(live_set_xml(version))[0]
                tracks = _by_id(result["tracks"])
                self.assertEqual(tracks["20"]["sends"],
                                 [{"return_id": "50", "return": "Return A", "value": 0.5,
                                   "db": round(20 * __import__("math").log10(0.5), 2)}])
                self.assertEqual(tracks["30"]["sends"][0]["value"], 1.0)
                self.assertAlmostEqual(tracks["30"]["volume"], 0.5)
                self.assertAlmostEqual(tracks["30"]["volume_db"], -6.02, places=2)
                self.assertIs(tracks["30"]["on"], True)
                self.assertIs(tracks["40"]["on"], False)
                # The send floor is -inf in Live; JSON gets None, never Infinity.
                self.assertIsNone(tracks["10"]["sends"][0]["db"])

    def test_device_params_from_real_parameter_names(self):
        for version in (8, 11, 12):
            with self.subTest(version=version):
                tracks = _by_id(read_live_set(live_set_xml(version))[0]["tracks"])
                erosion = _device(tracks["20"], "Erosion")
                self.assertEqual(erosion["params"], {"mode": 0, "frequency": 5000.0, "amplitude": 100.0})
                self.assertIs(erosion["on"], True)
                self.assertEqual(_device(tracks["20"], "AutoFilter")["params"],
                                 {"filter_type": 0, "cutoff": 60.0, "resonance": 0.3})
                # BeatRepeat carries a parameter called Gate; it is not a Gate device.
                self.assertEqual([d["type"] for d in tracks["21"]["devices"]], ["BeatRepeat"])
                self.assertEqual(tracks["21"]["devices"][0]["params"],
                                 {"chance": 0.5, "interval": 4, "grid": 8, "gate": 4})
                comp = _device(tracks["30"], "Compressor2")
                self.assertEqual(comp["params"]["ratio"], "inf")
                self.assertAlmostEqual(comp["params"]["threshold_db"], -26.02, places=2)
                self.assertEqual(comp["params"]["makeup_db"], 6.0)
                gate = _device(tracks["40"], "Gate")
                self.assertIs(gate["params"]["flip"], True)
                self.assertAlmostEqual(gate["params"]["threshold_db"], -26.02, places=2)
                self.assertEqual(gate["params"]["hold"], 20.0)
                inner = _device(tracks["40"], "Compressor2")
                self.assertEqual(inner["rack_path"], ["AudioEffectGroupDevice:Rack"])
                plugin = [d for d in tracks["40"]["devices"] if d.get("plugin")]
                self.assertEqual(plugin[0]["plugin"],
                                 {8: "Plugin V", 11: "Plugin A", 12: "Plugin V3"}[version])

    def test_returns_share_device_and_mixer_extraction(self):
        for version in (8, 11, 12):
            with self.subTest(version=version):
                (ret,) = read_live_set(live_set_xml(version))[0]["returns"]
                self.assertEqual((ret["id"], ret["name"], ret["kind"]), ("50", "Return A", "return"))
                self.assertIs(ret["on"], True)
                comp = _device(ret, "Compressor2")["params"]
                self.assertAlmostEqual(comp["threshold_db"], -20.0, places=2)
                self.assertAlmostEqual(comp["ratio"], 4.0, places=3)
                self.assertAlmostEqual(comp["makeup_db"], -6.0, places=4)
                self.assertAlmostEqual(comp["attack"], 1.0, places=4)
                self.assertAlmostEqual(comp["release"], 50.0, places=4)
                reverb = _device(ret, "Reverb")
                self.assertIs(reverb["on"], False)
                self.assertEqual(reverb["params"], {"decay": 1000.0, "size": 50.0, "dry_wet": 0.5})

    def test_sidechains_resolve_by_id_after_all_tracks_including_racks(self):
        for version in (8, 11, 12):
            with self.subTest(version=version):
                edges = read_live_set(live_set_xml(version))[0]["sidechains"]
                self.assertEqual(edges, [
                    {"target_track_id": "30", "target_track": "Track B", "device": "Compressor2",
                     "device_on": True, "in_rack": False, "source_track_id": "40", "source_track": "Track C",
                     "source_tap": "PostFxOut"},
                    {"target_track_id": "40", "target_track": "Track C", "device": "Compressor2",
                     "device_on": True, "in_rack": True, "source_track_id": "21", "source_track": "Track A",
                     "source_tap": "PostFxOut"},
                ])

    def test_gzip_als_and_pack_keep_only_live_sets(self):
        payload = live_set_xml(8)
        with tempfile.TemporaryDirectory() as tmp:
            als = Path(tmp) / "test.als"
            als.write_bytes(gzip.compress(payload))
            self.assertEqual(len(read_live_set(als)), 1)
            alp = Path(tmp) / "test.alp"
            alp.write_bytes(gzip.compress(b"junk" + payload + b"\x00junk"
                                          + preset_xml() + b"junk" + payload))
            docs = read_live_set(alp)
            self.assertEqual(len(docs), 2)
            self.assertEqual(docs[0]["xml_sha256"], docs[1]["xml_sha256"])
            only_preset = Path(tmp) / "preset.alp"
            only_preset.write_bytes(gzip.compress(b"junk" + preset_xml()))
            with self.assertRaisesRegex(ValueError, "no Ableton Live Set"):
                read_live_set(only_preset)

    def test_parsed_xml_sha_matches_across_containers(self):
        payload = live_set_xml(12)
        raw = read_live_set(payload)[0]["xml_sha256"]
        packed = read_live_set(gzip.compress(b"head" + payload + b"tail"))[0]["xml_sha256"]
        self.assertEqual(raw, packed)
        self.assertNotEqual(raw, read_live_set(live_set_xml(12, tempo=90))[0]["xml_sha256"])

    def test_broken_containers_raise_value_error(self):
        full = gzip.compress(live_set_xml(8))
        for label, data in (("truncated gzip", full[: len(full) // 2]),
                            ("header only", full[:2]),
                            ("corrupt deflate", full[:12] + b"\xff" * 40 + full[52:])):
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    read_live_set(data)


class CommandLineTests(unittest.TestCase):
    """The CLI runs as documented: ``python tools/live_set_reader.py …`` from the repo."""

    def _run(self, *args, cwd=REPO):
        env = dict(os.environ, PYTHONPATH="", PRODUCER_PAL_URL="http://127.0.0.1:9/mcp")
        return subprocess.run([sys.executable, str(REPO / "tools" / "live_set_reader.py"), *args],
                              cwd=cwd, env=env, capture_output=True, text=True, timeout=60)

    def test_reads_a_set_and_prints_the_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            als = Path(tmp) / "set.als"
            als.write_bytes(gzip.compress(live_set_xml(11)))
            out = Path(tmp) / "out.json"
            proc = self._run(str(als), "--json", str(out), cwd=tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Track A [21]", proc.stdout)
            self.assertIn("sidechain: Track B [30] Compressor2 <- Track C [40]", proc.stdout)
            self.assertEqual(json.loads(out.read_text())["tempo"], 100.0)

    def test_broken_files_exit_2_with_a_clean_message(self):
        full = gzip.compress(live_set_xml(8))
        with tempfile.TemporaryDirectory() as tmp:
            cases = {"truncated.als": full[: len(full) // 2], "tiny.als": full[:2],
                     "preset.alp": gzip.compress(preset_xml()), "text.xml": b"not xml"}
            for name, data in cases.items():
                with self.subTest(name=name):
                    path = Path(tmp) / name
                    path.write_bytes(data)
                    proc = self._run(str(path), cwd=tmp)
                    self.assertEqual(proc.returncode, 2)
                    self.assertTrue(proc.stderr.startswith("live_set_reader: "), proc.stderr)
                    self.assertNotIn("Traceback", proc.stderr)

    def test_attach_and_compare_subcommands_import_from_the_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store"
            (store / "audio_refs").mkdir(parents=True)
            (store / "audio_ref_index.json").write_text(json.dumps({"refs": {}}))
            als = Path(tmp) / "set.als"
            als.write_bytes(gzip.compress(live_set_xml(12)))
            proc = self._run("attach", "missing-ref", str(als), "--store", str(store), cwd=tmp)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("unknown ref_id", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            proc = self._run("compare", "missing-ref", "--store", str(store), cwd=tmp)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()
