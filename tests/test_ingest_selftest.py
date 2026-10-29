"""Run ingest/selftest_ingest.py (synthetic ingest checks) so CI catches it rotting."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestIngestSelftest(unittest.TestCase):
    def test_ingest_selftest_passes(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "ingest", "selftest_ingest.py")],
            capture_output=True, text=True, timeout=120, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stdout[-4000:] + proc.stderr[-4000:])
        self.assertIn("ingest selftest passed", proc.stdout)


if __name__ == "__main__":
    unittest.main()
