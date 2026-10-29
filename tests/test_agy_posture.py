"""The agy listener postures: what is shared and what differs on purpose.

Session ears passes --dangerously-skip-permissions because its listener writes
the take card to a file; audio-ref card listens only view the audio and answer
on stdout, so they do not. Both keep --sandbox and the same env allowlist.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mcp_server import session_ears as _se  # noqa: E402
from tools import audio_ref as _ar  # noqa: E402

SKIP = "--dangerously-skip-permissions"
TAKE = {"take_id": "tk1", "track": "Vox", "clip_name": "take 1"}


def _ears_cmd(output_path: Path) -> list[str]:
    with mock.patch.object(_se, "_agy_supports_flag", return_value=False):
        return _se._build_agy_cmd(
            agy="/fake/agy", audio_dir=Path("/tmp/stage"), audio_name="take.wav",
            take=TAKE, focus="full", model="gemini-3.8-flash-medium",
            timeout_s=300, output_path=output_path)


def _ref_cmd() -> list[str]:
    with mock.patch.object(_ar, "_agy_supports_flag", return_value=False):
        return _ar._agy_cmd_for("gemini-3.8-flash-medium", "/tmp/stage", "hi")


class TestAgyPosture(unittest.TestCase):
    def test_ears_skips_permissions_because_it_writes_a_card_file(self):
        out = Path("/tmp/stage/take_card.json")
        cmd = _ears_cmd(out)
        self.assertIs(_se.EARS_AGY_SKIP_PERMISSIONS, True)
        self.assertIn(SKIP, cmd)
        self.assertIn("--sandbox", cmd)
        # the reason for the flag: the prompt asks for a file write
        self.assertIn("write the same JSON object to:\n%s" % out, cmd[-1])

    def test_ears_flag_follows_the_constant(self):
        with mock.patch.object(_se, "EARS_AGY_SKIP_PERMISSIONS", False):
            cmd = _ears_cmd(Path("/tmp/stage/take_card.json"))
        self.assertNotIn(SKIP, cmd)
        self.assertIn("--sandbox", cmd)

    def test_audio_ref_card_listen_needs_no_write_and_no_flag(self):
        cmd = _ref_cmd()
        self.assertIs(_ar.AGY_SKIP_PERMISSIONS, False)
        self.assertNotIn(SKIP, cmd)
        self.assertIn("--sandbox", cmd)

    def test_shared_parts_of_the_posture(self):
        self.assertEqual(set(_ar._AGY_ENV_ALLOW), set(_se._AGY_ENV_ALLOW))

    def test_dead_sandbox_toggle_is_gone(self):
        # it was never called; --sandbox is unconditional in every listener
        self.assertFalse(hasattr(_se, "_agy_sandbox_enabled"))


if __name__ == "__main__":
    unittest.main()
