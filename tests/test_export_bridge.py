"""Unit tests for mcp_server/export_bridge.py (Producer Pal payload builder, no socket)."""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mcp_server import core, core_shared, export_bridge  # noqa: E402
from tests.pp_fakes import V23, PATH_VERSIONS, on_both_versions, validate_call  # noqa: E402
from mcp_server import pp_session  # noqa: E402

NOTES = [
    {"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 96},
    {"pitch": 63, "start": 1.0, "duration": 1.0, "velocity": 90},
    {"pitch": 67, "start": 2.0, "duration": 2.0, "velocity": 84},
]


@on_both_versions
class TestSendToAbleton(unittest.TestCase):
    def test_handoff_reports_clip_length_token(self):
        # server.py's selftest asserts handoff["clip_length"] matches ^\d+bar$ —
        # slice A's bridge copy had silently dropped the key.
        handoff = export_bridge.send_to_ableton(
            NOTES, track=0, clip_name="unit", tempo=120, bar_length=2,
        )
        self.assertIn("clip_length", handoff)
        self.assertRegex(handoff["clip_length"], r"^\d+bar$")
        self.assertEqual(handoff["clip_length"], "2bar")
        # the same token is what ppal-create-clip receives as `length`
        create_clip = handoff["tool_calls"][-1]
        self.assertEqual(create_clip["tool"], "ppal-create-clip")
        self.assertEqual(create_clip["arguments"]["length"], handoff["clip_length"])

    def test_every_handoff_call_matches_the_captured_schemas(self):
        # Session slot, selected-track fallback and arrangement placement all
        # emit only argument names the build's captured schemas list.
        addressing = pp_session.addressing_for_version(self.PP_VERSION)   # 2.2 / 2.3 / 2.4
        variants = [
            dict(track=3, session_scene=2),
            dict(track=None),
            dict(track=4, arrangement_start="9|1"),
        ]
        for kwargs in variants:
            with self.subTest(**kwargs):
                handoff = export_bridge.send_to_ableton(
                    NOTES, clip_name="unit", tempo=120, addressing=addressing, **kwargs)
                validate_call(handoff["prerequisite"]["tool"],
                              handoff["prerequisite"]["arguments"])
                for call in handoff["tool_calls"]:
                    validate_call(call["tool"], call["arguments"])
        session = export_bridge.send_to_ableton(NOTES, track=3, session_scene=2,
                                                addressing=addressing)
        self.assertEqual(session["tool_calls"][-1]["arguments"]["path"], "t3/s2")
        arrangement = export_bridge.send_to_ableton(
            NOTES, track=4, arrangement_start="9|1", addressing=addressing)
        created = arrangement["tool_calls"][-1]["arguments"]
        if self.PP_VERSION in PATH_VERSIONS:   # v23/v23_create_clip_arrangement_path.json form (2.4: v24_create_arrangement_clip)
            self.assertEqual(created["path"], "t4[9|1]")
            self.assertNotIn("arrangementStart", created)
        else:                        # legacy/check_create_clip_arrangement.json form
            self.assertEqual((created["path"], created["arrangementStart"]), ("t4", "9|1"))
        with self.assertRaisesRegex(AssertionError, "unknown arguments.*arrangementStart"):
            validate_call("ppal-create-clip", export_bridge.send_to_ableton(
                NOTES, track=4, arrangement_start="9|1", addressing="2.2")["tool_calls"][-1][
                "arguments"], version=V23)

    def test_addressing_takes_the_version_ppal_connect_reports(self):
        # review minor: the operator passes what ppal-connect said ("2.3.0")
        for version, path, start in (("2.3.0", "t4[9|1]", None),
                                     ("2.2.0-rc1", "t4", "9|1")):
            with self.subTest(version=version):
                created = export_bridge.send_to_ableton(
                    NOTES, track=4, arrangement_start="9|1",
                    addressing=version)["tool_calls"][-1]["arguments"]
                self.assertEqual((created["path"], created.get("arrangementStart")),
                                 (path, start))
        with self.assertRaises(ValueError):
            export_bridge.send_to_ableton(NOTES, track=4, arrangement_start="9|1",
                                          addressing="latest")

    def test_core_facade_is_the_bridge_function(self):
        self.assertIs(core.send_to_ableton, export_bridge.send_to_ableton)


@on_both_versions
class TestSharedHelpers(unittest.TestCase):
    def test_clip_digest_has_one_owner(self):
        self.assertIs(core._clip_digest, core_shared._clip_digest)
        self.assertEqual(len(core_shared._clip_digest(NOTES)), 10)


if __name__ == "__main__":
    unittest.main()
