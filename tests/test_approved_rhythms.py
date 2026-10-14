"""Behavioral tests for portable approved-rhythm delivery (Task 3)."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mcp_server import approved_rhythms, core
from mcp_server.core_shared import ROOT as REPO_ROOT
from tests._requires import requires_catalog


def _hermetic_session_ears(testcase):
    """Point session_ears at an empty temp store.

    compose_brief folds persisted Set-take cards, which otherwise reads the
    real store/session_ears and attempts a Live identity read whenever a
    card is fresh. An empty temp STORE_ROOT short-circuits before any of
    that, whatever the developer's checkout holds.
    """
    from pathlib import Path

    from mcp_server import session_ears
    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    patcher = mock.patch.object(session_ears, "STORE_ROOT", Path(tmp.name))
    patcher.start()
    testcase.addCleanup(patcher.stop)


def _compact_pattern() -> dict:
    return {
        "id": "probe_pattern",
        "title": "Probe Pattern",
        "description": "A test rhythm",
        "midi_asset": "assets/approved_rhythms/probe_pattern.mid",
        "bpm_spec": {
            "ideal_bpm": 72,
            "bpm_range": "68-82",
            "feel": "steady pulse",
        },
    }


def _dump_fixture(tmpdir: str, patterns: list[dict], *, version: int = 3) -> str:
    path = os.path.join(tmpdir, "approved_rhythms.json")
    assets = os.path.join(tmpdir, "assets", "approved_rhythms")
    os.makedirs(assets, exist_ok=True)
    for pat in patterns:
        midi_name = os.path.basename(pat["midi_asset"])
        with open(os.path.join(assets, midi_name), "wb") as fh:
            fh.write(b"MThd\x00\x00\x00\x06\x00\x00\x00\x00\x00\x00")
    payload = {
        "version": version,
        "curator": "test",
        "updated_at": "2026-08-23T00:00:00+00:00",
        "bpm": 72,
        "aesthetic": "test",
        "patterns": patterns,
    }
    with open(path, "w") as fh:
        json.dump(payload, fh)
    return path


class TestApprovedRhythmsLoader(unittest.TestCase):
    def setUp(self):
        approved_rhythms.reset_loader_overrides()

    def tearDown(self):
        approved_rhythms.reset_loader_overrides()
        core_shared_cache = __import__("mcp_server.core_shared", fromlist=["_CACHE"])._CACHE
        core_shared_cache.pop("approved_rhythms", None)

    def test_rejects_absolute_midi_asset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pat = _compact_pattern()
            pat["midi_asset"] = os.path.join(REPO_ROOT, "assets/approved_rhythms/probe_pattern.mid")
            path = _dump_fixture(tmpdir, [pat])
            with self.assertRaisesRegex(ValueError, "absolute"):
                approved_rhythms.load_approved_rhythms(json_path=path, repo_root=tmpdir)

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pat = _compact_pattern()
            pat["midi_asset"] = "assets/approved_rhythms/../secret.mid"
            path = _dump_fixture(tmpdir, [pat])
            with self.assertRaisesRegex(ValueError, "traversal|outside"):
                approved_rhythms.load_approved_rhythms(json_path=path, repo_root=tmpdir)

    def test_missing_default_library_is_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(approved_rhythms, "APPROVED_RHYTHMS_JSON",
                                   os.path.join(tmpdir, "approved_rhythms.json")):
                self.assertEqual(approved_rhythms.load_approved_rhythms(), [])

    def test_missing_explicit_library_still_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises((OSError, ValueError)):
                approved_rhythms.load_approved_rhythms(
                    json_path=os.path.join(tmpdir, "absent.json"), repo_root=tmpdir)

    def test_rejects_missing_asset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pat = _compact_pattern()
            path = os.path.join(tmpdir, "approved_rhythms.json")
            with open(path, "w") as fh:
                json.dump(
                    {
                        "version": 3,
                        "patterns": [pat],
                    },
                    fh,
                )
            with self.assertRaisesRegex(ValueError, "missing"):
                approved_rhythms.load_approved_rhythms(json_path=path, repo_root=tmpdir)

    def test_rejects_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pat = _compact_pattern()
            path = _dump_fixture(tmpdir, [pat, dict(pat)])
            with self.assertRaisesRegex(ValueError, "duplicate"):
                approved_rhythms.load_approved_rhythms(json_path=path, repo_root=tmpdir)

    def test_rejects_malformed_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pat = _compact_pattern()
            del pat["title"]
            path = _dump_fixture(tmpdir, [pat])
            with self.assertRaisesRegex(ValueError, "title"):
                approved_rhythms.load_approved_rhythms(json_path=path, repo_root=tmpdir)


class TestApprovedRhythmsStrictValidation(unittest.TestCase):
    def setUp(self):
        approved_rhythms.reset_loader_overrides()

    def tearDown(self):
        approved_rhythms.reset_loader_overrides()
        __import__("mcp_server.core_shared", fromlist=["_CACHE"])._CACHE.pop("approved_rhythms", None)

    def _load(self, tmpdir: str, pat: dict):
        path = _dump_fixture(tmpdir, [pat])
        return approved_rhythms.load_approved_rhythms(json_path=path, repo_root=tmpdir)

    def test_rejects_numeric_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pat = _compact_pattern()
            pat["id"] = 42
            with self.assertRaisesRegex(ValueError, "id"):
                self._load(tmpdir, pat)

    def test_rejects_null_title(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pat = _compact_pattern()
            pat["title"] = None
            with self.assertRaisesRegex(ValueError, "title"):
                self._load(tmpdir, pat)

    def test_rejects_empty_description(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pat = _compact_pattern()
            pat["description"] = ""
            with self.assertRaisesRegex(ValueError, "description"):
                self._load(tmpdir, pat)

    def test_rejects_non_object_bpm_spec(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pat = _compact_pattern()
            pat["bpm_spec"] = "68-82"
            with self.assertRaisesRegex(ValueError, "bpm_spec"):
                self._load(tmpdir, pat)

    def test_rejects_bool_ideal_bpm(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pat = _compact_pattern()
            pat["bpm_spec"]["ideal_bpm"] = True
            with self.assertRaisesRegex(ValueError, "ideal_bpm"):
                self._load(tmpdir, pat)

    def test_rejects_nonnumeric_ideal_bpm(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pat = _compact_pattern()
            pat["bpm_spec"]["ideal_bpm"] = "seventy-two"
            with self.assertRaisesRegex(ValueError, "ideal_bpm"):
                self._load(tmpdir, pat)

    def test_rejects_ideal_bpm_below_range(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pat = _compact_pattern()
            pat["bpm_spec"]["ideal_bpm"] = 19
            with self.assertRaisesRegex(ValueError, "ideal_bpm"):
                self._load(tmpdir, pat)

    def test_rejects_ideal_bpm_above_range(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pat = _compact_pattern()
            pat["bpm_spec"]["ideal_bpm"] = 301
            with self.assertRaisesRegex(ValueError, "ideal_bpm"):
                self._load(tmpdir, pat)

    def test_rejects_bool_bpm_range_component(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pat = _compact_pattern()
            pat["bpm_spec"]["bpm_range"] = "68-True"
            with self.assertRaisesRegex(ValueError, "bpm_range"):
                self._load(tmpdir, pat)

    def test_rejects_bpm_range_out_of_bounds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pat = _compact_pattern()
            pat["bpm_spec"]["bpm_range"] = "10-82"
            with self.assertRaisesRegex(ValueError, "bpm_range"):
                self._load(tmpdir, pat)

    def test_rejects_ideal_bpm_outside_declared_range(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pat = _compact_pattern()
            pat["bpm_spec"]["ideal_bpm"] = 90
            pat["bpm_spec"]["bpm_range"] = "68-82"
            with self.assertRaisesRegex(ValueError, "bpm_range|ideal_bpm"):
                self._load(tmpdir, pat)

    def test_rejects_empty_feel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pat = _compact_pattern()
            pat["bpm_spec"]["feel"] = ""
            with self.assertRaisesRegex(ValueError, "feel"):
                self._load(tmpdir, pat)

    def test_rejects_symlink_asset_pointing_outside_repo(self):
        import shutil

        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside)
        with tempfile.TemporaryDirectory() as tmpdir:
            outside_file = os.path.join(outside, "escape.mid")
            with open(outside_file, "wb") as fh:
                fh.write(b"MThd\x00\x00\x00\x06\x00\x00\x00\x00\x00\x00")
            assets = os.path.join(tmpdir, "assets", "approved_rhythms")
            os.makedirs(assets)
            os.symlink(outside_file, os.path.join(assets, "probe_pattern.mid"))
            path = os.path.join(tmpdir, "approved_rhythms.json")
            with open(path, "w") as fh:
                json.dump({"version": 3, "patterns": [_compact_pattern()]}, fh)
            with self.assertRaisesRegex(ValueError, "outside|symlink|regular"):
                approved_rhythms.load_approved_rhythms(json_path=path, repo_root=tmpdir)


@requires_catalog
class TestGetStyleApprovedRhythms(unittest.TestCase):
    def tearDown(self):
        approved_rhythms.reset_loader_overrides()
        __import__("mcp_server.core_shared", fromlist=["_CACHE"])._CACHE.pop("approved_rhythms", None)

    def test_structured_exposes_compact_metadata(self):
        style = core.get_style()
        rhythms = style["structured"]["approved_rhythms"]
        self.assertEqual(len(rhythms), 15)
        sample = rhythms[0]
        self.assertEqual(
            set(sample.keys()),
            {"id", "title", "description", "midi_asset", "bpm_spec"},
        )
        self.assertIn("ideal_bpm", sample["bpm_spec"])
        self.assertRegex(sample["midi_asset"], r"^assets/approved_rhythms/.+\.mid$")


@requires_catalog
class TestComposeBriefApprovedRhythms(unittest.TestCase):
    def setUp(self):
        _hermetic_session_ears(self)

    def tearDown(self):
        approved_rhythms.reset_loader_overrides()
        __import__("mcp_server.core_shared", fromlist=["_CACHE"])._CACHE.pop("approved_rhythms", None)

    def test_brief_names_each_approved_rhythm(self):
        brief = core.compose_brief("dark 6 minute song with breakdown")
        rhythms = brief["approved_rhythms"]
        self.assertEqual(len(rhythms), 15)
        titles = {r["title"] for r in rhythms}
        self.assertIn("1. Tectonic 12/8 Hemiola", titles)
        self.assertIn("Mid-Tempo: 100 Gnawa Trance", titles)

    def test_brief_rhythms_independent_of_style_excerpt(self):
        long_md = "x" * 5000 + "\n# Approved rhythms live here but are past the excerpt cut."
        with mock.patch("mcp_server.core._read_style_md", return_value=long_md):
            brief = core.compose_brief("dark industrial song")
        self.assertEqual(len(brief["style_md_excerpt"]), 4000)
        self.assertEqual(len(brief["approved_rhythms"]), 15)
        joined = json.dumps(brief["approved_rhythms"])
        self.assertNotIn("Approved rhythms live here", joined)


@requires_catalog
class TestModelFacingHygiene(unittest.TestCase):
    def setUp(self):
        _hermetic_session_ears(self)

    def tearDown(self):
        approved_rhythms.reset_loader_overrides()
        __import__("mcp_server.core_shared", fromlist=["_CACHE"])._CACHE.pop("approved_rhythms", None)

    def test_no_absolute_paths_or_raw_pp_in_style_and_brief(self):
        style = core.get_style()
        brief = core.compose_brief("industrial doom")
        blob = json.dumps({"style": style["structured"], "brief": brief})
        self.assertNotRegex(blob, re.escape(REPO_ROOT))
        self.assertNotIn("raw_producer_pal_response", blob)
        self.assertNotIn("ppal-", blob.lower())
        self.assertNotIn("slot", json.dumps(brief["approved_rhythms"]))


class TestProtectedProbesUnchanged(unittest.TestCase):
    PROBE_DIR = "/private/tmp/task3-probes"

    def _load_probe(self, name: str):
        with open(os.path.join(self.PROBE_DIR, f"{name}.json")) as fh:
            return json.load(fh)

    def test_find_phrases_unchanged(self):
        if not os.path.isdir(self.PROBE_DIR):
            self.skipTest("baseline probes not captured")
        self.assertEqual(core.find_phrases(kind="harmonic", k=4), self._load_probe("find_phrases"))

    def test_compose_from_profile_unchanged(self):
        if not os.path.isdir(self.PROBE_DIR):
            self.skipTest("baseline probes not captured")
        out = core.compose_from_profile(bars=4, key="D minor", seed=7, density="mid")
        self.assertEqual(out, self._load_probe("compose_from_profile"))

    def test_develop_idea_unchanged(self):
        if not os.path.isdir(self.PROBE_DIR):
            self.skipTest("baseline probes not captured")
        seed = [
            {"pitch": 62, "start": 0.0, "duration": 0.5, "velocity": 90},
            {"pitch": 65, "start": 0.5, "duration": 0.5, "velocity": 85},
            {"pitch": 69, "start": 1.0, "duration": 1.0, "velocity": 95},
        ]
        self.assertEqual(
            core.develop_idea(seed_notes=seed, tempo=120.0),
            self._load_probe("develop_idea"),
        )

    def test_send_to_ableton_unchanged(self):
        if not os.path.isdir(self.PROBE_DIR):
            self.skipTest("baseline probes not captured")
        sample_notes = [
            {"pitch": 60, "start": 0.0, "duration": 0.25, "velocity": 100},
            {"pitch": 64, "start": 0.25, "duration": 0.25, "velocity": 90},
            {"pitch": 67, "start": 0.5, "duration": 0.5, "velocity": 95},
            {"pitch": 72, "start": 1.0, "duration": 0.5, "velocity": 110},
        ]
        out = core.send_to_ableton(sample_notes, track=0, clip_name="selftest_sketch", tempo=120.0)
        self.assertEqual(out, self._load_probe("send_to_ableton"))


if __name__ == "__main__":
    unittest.main()
