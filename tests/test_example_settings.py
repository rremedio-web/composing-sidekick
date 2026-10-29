"""docs/examples/claude-settings.json must name real Producer Pal tools and
cover every one that changes the Live Set."""
from __future__ import annotations

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.pp_fakes import V22, V23, V24, schemas_for  # noqa: E402

PREFIX = "mcp__producer-pal__"
MUTATING = ("ppal-create-", "ppal-update-", "ppal-delete", "ppal-duplicate",
            "ppal-live-api")


class TestExampleClaudeSettings(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "docs", "examples", "claude-settings.json"),
                  encoding="utf-8") as fh:
            self.ask = json.load(fh)["permissions"]["ask"]

    def test_entries_are_real_tools_on_every_captured_build(self):
        for version in (V22, V23, V24):
            tools = set(schemas_for(version))
            for rule in self.ask:
                with self.subTest(version=version, rule=rule):
                    self.assertTrue(rule.startswith(PREFIX), rule)
                    self.assertIn(rule[len(PREFIX):], tools)

    def test_every_set_changing_tool_asks_first(self):
        for version in (V22, V23, V24):
            mutating = {name for name in schemas_for(version)
                        if name.startswith(MUTATING)}
            asked = {rule[len(PREFIX):] for rule in self.ask}
            self.assertEqual(mutating - asked, set(), version)


if __name__ == "__main__":
    unittest.main()
