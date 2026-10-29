"""Run the Node tests for the Max for Live bridge script and the idea-pack panel
(skips without node)."""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "needs node to run tests/test_sidekick_bridge_capture.mjs")
class TestSidekickBridgeJs(unittest.TestCase):
    def test_bridge_capture_and_policy(self):
        proc = subprocess.run(
            [NODE, os.path.join(ROOT, "tests", "test_sidekick_bridge_capture.mjs")],
            capture_output=True, text=True, timeout=60, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("sidekick bridge capture tests: OK", proc.stdout)


@unittest.skipUnless(NODE, "needs node to run tools/ideapack_panel.test.mjs")
class TestIdeaPackPanelJs(unittest.TestCase):
    def test_panel_rating_contract_checks(self):
        proc = subprocess.run(
            [NODE, os.path.join(ROOT, "tools", "ideapack_panel.test.mjs")],
            capture_output=True, text=True, timeout=60, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PASS", proc.stdout)


if __name__ == "__main__":
    unittest.main()
