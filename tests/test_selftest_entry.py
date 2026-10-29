"""server.py --selftest is a thin entry into mcp_server/selftest.py, and a
clean clone (no catalogue) gets one clear line and a non-zero exit."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mcp_server import selftest  # noqa: E402

CATALOGUE_BUILT = not selftest.missing_catalogue()


class TestSelftestEntry(unittest.TestCase):
    def test_missing_catalogue_names_each_absent_file(self):
        with tempfile.TemporaryDirectory() as store:
            self.assertEqual(selftest.missing_catalogue(store),
                             list(selftest.REQUIRED_CATALOGUE))
            for name in selftest.REQUIRED_CATALOGUE:
                open(os.path.join(store, name), "w").close()
            self.assertEqual(selftest.missing_catalogue(store), [])

    def test_missing_catalogue_returns_2_before_touching_the_server(self):
        with mock.patch.object(selftest, "missing_catalogue",
                               return_value=["style_profile.json"]), \
                mock.patch.object(selftest, "_run_selftest") as body:
            self.assertEqual(selftest.run_selftest(object()), 2)
        body.assert_not_called()

    def test_server_keeps_only_a_thin_entry(self):
        with open(os.path.join(ROOT, "mcp_server", "server.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("def run_selftest", src)
        self.assertIn("_selftest.run_selftest(sys.modules[__name__])", src)

    @unittest.skipIf(CATALOGUE_BUILT, "catalogue is built; the full selftest is the check")
    def test_clean_clone_prints_one_line_and_exits_nonzero(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "mcp_server", "server.py"), "--selftest"],
            capture_output=True, text=True, timeout=300, cwd=ROOT)
        output = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 2, output)
        self.assertIn("no catalogue built", proc.stderr)
        self.assertNotIn("Traceback", output)
        self.assertEqual(proc.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
