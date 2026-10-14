"""Focused tests for ingest/scan_catalog.py CLI reporting."""
from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "ingest"))

from ingest import scan_catalog  # noqa: E402


class TestScanCatalogReportOutput(unittest.TestCase):
    """Catalog report must use real newlines, not escaped literal backslash-n."""

    def _run_report(self, rows: list[dict]) -> str:
        buf = io.StringIO()
        with patch.object(scan_catalog, "find_als", return_value=["/tmp/a.als"]), \
             patch.object(scan_catalog, "summarize", side_effect=rows), \
             patch.object(scan_catalog.os, "makedirs"), \
             patch("builtins.open", unittest.mock.mock_open()), \
             redirect_stdout(buf):
            scan_catalog.main(["--roots", "/tmp/empty", "--out", "/tmp/manifest.json"])
        return buf.getvalue()

    def test_report_uses_real_newlines_not_literal_backslash_n(self):
        rows = [{
            "path": "/tmp/a.als",
            "name": "ProjectA",
            "tempo": 120.0,
            "midi_tracks": 1,
            "audio_tracks": 0,
            "midi_clips": 1,
            "notes": 10,
            "main_notes": 10,
            "clip_source_counts": {"arrangement": 1},
            "note_source_counts": {"arrangement": 10},
            "audio_clips": 0,
            "notes_skipped": 2,
            "skip_detail": {"malformed_pitch": 2},
            "error": None,
        }]
        output = self._run_report(rows)

        self.assertNotIn("\\n", output)
        self.assertIn("===== CATALOG REPORT =====", output)
        self.assertIn("Projects with skipped notes:", output)
        self.assertIn("ProjectA: 2 skipped", output)
        lines = output.splitlines()
        self.assertGreater(len(lines), 5)
        self.assertTrue(any("CATALOG REPORT" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
