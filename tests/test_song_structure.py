from __future__ import annotations

import inspect
import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

from mcp_server import pp_session, server, song_structure
from tests.pp_fakes import schema_strict, on_both_versions
from tests._requires import requires_catalog


def response(result):
    raw = json.dumps({"result": result})
    return pp_session.PPResponse(raw, result, [])


@schema_strict
class FakeSession:
    def __init__(self, tracks):
        self.tracks = tracks
        self.calls = []

    async def call(self, name, args):
        self.calls.append((name, args))
        if name == "ppal-read-live-set":
            return response({"sceneCount": 2, "tempo": 111, "tracks": self.tracks})
        if name == "ppal-read-track":
            index = args["trackIndex"]
            # Session clips carry id + path (legacy/legacy_read_track.json).
            return response({"sessionClips": [{
                "id": "clip-%d" % index, "path": "t%d/s0" % index,
                "name": "Loop %d" % index,
                "type": "audio" if index % 2 else "midi"}]})
        raise AssertionError("unexpected mutation/tool %s" % name)


def operation(session):
    @asynccontextmanager
    async def factory():
        yield session
    return factory


@on_both_versions
@requires_catalog
class TestSongStructure(unittest.TestCase):
    def test_explicit_parts_allow_duplicate_roles_and_validate_exact_schema(self):
        parts = song_structure.validate_parts([
            {"part_id": "lead-a", "role": "lead", "source_path": "t0/s0"},
            {"part_id": "lead-b", "role": "lead", "source_path": "t1/s0"},
            {"part_id": "kit", "role": "drum", "source_path": "t2/s0"},
        ])
        self.assertEqual([p["role"] for p in parts], ["lead", "lead", "drums"])
        for bad in (
            [{"part_id": "a", "role": "lead", "source_path": "/tmp/x"}],
            [{"part_id": "a", "role": "voice", "source_path": "t0/s0"}],
            [{"part_id": "a", "role": "lead", "source_path": "t0/s0"},
             {"part_id": "a", "role": "bass", "source_path": "t1/s0"}],
            [{"part_id": "a", "role": "lead", "source_path": "t0/s0", "x": 1}],
        ):
            with self.assertRaises(ValueError):
                song_structure.validate_parts(bad)

    def test_scene_discovery_marks_unknown_and_ambiguous_without_guessing(self):
        tracks = [
            {"trackIndex": 0, "name": "Lead Synth"},
            {"trackIndex": 1, "name": "Mystery Voice"},
            {"trackIndex": 2, "name": "Lead Bass"},
            {"trackIndex": 3, "name": "Warm Pad"},
        ]
        session = FakeSession(tracks)
        found = server._run_async(song_structure.discover_scene_parts(0, operation(session)))
        self.assertEqual(found["parts"], [
            {"part_id": "track-0", "role": "lead", "source_path": "t0/s0"},
            {"part_id": "track-3", "role": "pad", "source_path": "t3/s0"},
        ])
        self.assertEqual([x["reason"] for x in found["unresolved"]],
                         ["unknown", "ambiguous"])
        self.assertEqual(found["tempo"], 111.0)
        self.assertTrue(all(name.startswith("ppal-read-") for name, _ in session.calls))

    def test_preview_is_zero_write_and_uses_real_structure_shapes(self):
        parts = [
            {"part_id": "lead-a", "role": "lead", "source_path": "t0/s0"},
            {"part_id": "lead-b", "role": "lead", "source_path": "t1/s0"},
            {"part_id": "bass", "role": "bass", "source_path": "t2/s0"},
        ]
        with patch("mcp_server.live_write.write_song_locators",
                   side_effect=AssertionError("preview wrote locators")):
            result = server.lay_the_song(parts=parts, target_minutes=2,
                                         locators=True, commit=True, confirm=False)
        self.assertEqual(result["part_resolution"], "resolved")
        self.assertFalse(result["locators_written"]["executed"])
        self.assertFalse(result["committed"]["executed"])
        self.assertTrue(result["intended_operations"]["locators"])
        placements = result["intended_operations"]["placements"]
        self.assertTrue(placements)
        self.assertEqual({p["part_id"] for p in placements if p["role"] == "lead"},
                         {"lead-a", "lead-b"})
        self.assertTrue(all(isinstance(s["bar_start"], int) for s in result["sections"]))
        self.assertTrue(all(1 <= s["density"] <= 10 for s in result["sections"]))

    def test_auto_preview_needs_explicit_parts_and_confirm_refuses(self):
        session = FakeSession([
            {"trackIndex": 0, "name": "Lead"},
            {"trackIndex": 1, "name": "Unknown Thing"},
        ])
        with patch.object(pp_session, "operation_session", operation(session)):
            preview = server.lay_the_song(scene_index=0, locators=False)
        self.assertEqual(preview["part_resolution"], "needs_parts")
        self.assertEqual(preview["tempo"], 111.0)
        self.assertEqual(preview["unresolved"][0]["source_path"], "t1/s0")
        self.assertEqual(preview["intended_operations"]["locators"], [])

        session = FakeSession([{ "trackIndex": 0, "name": "Unknown Thing"}])
        with patch.object(pp_session, "operation_session", operation(session)):
            with self.assertRaisesRegex(ValueError, "explicit parts"):
                server.lay_the_song(scene_index=0, confirm=True)

    def test_legacy_seed_preview_remains_offline_and_signature_exposes_new_contract(self):
        result = server.lay_the_song(
            seed_notes=[{"pitch": 60, "start_beats": 0,
                         "duration_beats": 1, "velocity": 80}],
            locators=False,
        )
        self.assertEqual(result["part_resolution"], "resolved")
        self.assertEqual(result["parts"], [])
        params = list(inspect.signature(server.lay_the_song).parameters)
        self.assertEqual(params[:8], ["scene_index", "parts", "target_minutes",
                                     "tempo", "seed", "locators", "commit", "confirm"])
        self.assertIn("slot", params)
        self.assertIn("seed_notes", params)


if __name__ == "__main__":
    unittest.main()
