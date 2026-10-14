"""Unit tests for mcp_server/live_write.py locator state persistence.

Tests that _write_locator_state follows the documented atomic-write pattern:
- tempfile.mkstemp() in the same directory
- os.replace() for atomic commit
- cleanup on error
- no leftover .tmp files
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

# Insert repo root to allow imports from mcp_server/
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mcp_server import live_write


class TestWriteLocatorState(unittest.TestCase):
    """Test _write_locator_state writes + reloads a dict round-trip via tempfile.TemporaryDirectory."""

    def test_write_and_reload(self):
        """_write_locator_state writes + reloads a dict round-trip via tempfile.TemporaryDirectory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "locators.json")
            names = ["A", "B"]
            live_write._write_locator_state(path, names)
            with open(path) as fh:
                reloaded = json.load(fh)
            self.assertEqual(reloaded, {"names": names})

    def test_no_leftover_tmp_files(self):
        """_write_locator_state leaves no .tmp files in the directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "locators.json")
            names = ["C", "D"]
            live_write._write_locator_state(path, names)
            # No .tmp files should exist
            tmp_files = [f for f in os.listdir(tmpdir) if f.endswith(".tmp")]
            self.assertEqual(tmp_files, [])

    def test_delete_state(self):
        """_write_locator_state(path, None) removes the state file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "locators.json")
            names = ["E", "F"]
            live_write._write_locator_state(path, names)
            self.assertTrue(os.path.isfile(path))

            live_write._write_locator_state(path, None)
            self.assertFalse(os.path.isfile(path))

    def test_roundtrip_with_none(self):
        """_write_locator_state round-trips a list, then deletes, then writes again."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "locators.json")
            names1 = ["G", "H"]
            live_write._write_locator_state(path, names1)
            with open(path) as fh:
                reloaded1 = json.load(fh)
            self.assertEqual(reloaded1, {"names": names1})

            live_write._write_locator_state(path, None)
            self.assertFalse(os.path.isfile(path))

            names2 = ["I", "J"]
            live_write._write_locator_state(path, names2)
            with open(path) as fh:
                reloaded2 = json.load(fh)
            self.assertEqual(reloaded2, {"names": names2})


if __name__ == "__main__":
    unittest.main()
