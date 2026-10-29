"""Run each module's own `--selftest` entry point so CI catches it rotting.

Each one runs in a subprocess: tools/mix_witness.py's selftest replaces
`shutil.which` for the whole process, which must not leak into other tests.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FFMPEG = shutil.which("ffmpeg")

# (path, needs ffmpeg)
SELFTESTS = (
    ("mcp_server/session_ears.py", True),
    ("mcp_server/mix_trials.py", True),
    ("mcp_server/mix_contracts.py", False),
    ("tools/mix_witness.py", True),
    ("tools/mix_dsp.py", True),
    ("tools/mix_check.py", False),
)


class TestModuleSelftests(unittest.TestCase):
    def test_each_module_selftest_passes(self):
        for rel, needs_ffmpeg in SELFTESTS:
            with self.subTest(module=rel):
                if needs_ffmpeg and not FFMPEG:
                    self.skipTest("needs ffmpeg for %s --selftest" % rel)
                proc = subprocess.run(
                    [sys.executable, os.path.join(ROOT, rel), "--selftest"],
                    capture_output=True, text=True, timeout=300, cwd=ROOT)
                self.assertEqual(
                    proc.returncode, 0,
                    "%s --selftest exited %d\n--- stdout ---\n%s\n--- stderr ---\n%s"
                    % (rel, proc.returncode, proc.stdout[-4000:], proc.stderr[-4000:]))


if __name__ == "__main__":
    unittest.main()
