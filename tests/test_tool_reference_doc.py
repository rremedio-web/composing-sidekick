"""The tool reference table in config/claude_mcp.md and the profile counts in the
README and claude_mcp.md match the registry in mcp_server/profiles.py."""
from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mcp_server import profiles  # noqa: E402

TOOL_DOC = os.path.join(ROOT, "config", "claude_mcp.md")
README = os.path.join(ROOT, "README.md")

# Profiles a tool can be listed under; "all" holds every tool by definition.
NAMED_PROFILES = ("compose", "session", "reference", "debug")
CELL_ALIASES = {"ref": "reference"}

ROW = re.compile(r"^\| `([a-z_]+)` \| ([^|]+) \|")
COUNT_ROW = re.compile(r"^\| `([a-z]+)`(?: \(default\))? \| (\d+) \|")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _tools_section(text: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index("## Tools exposed")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("## "))
    return lines[start:end]


def _table_rows(section: list[str]) -> list[tuple[int, str, str]]:
    return [(i, m.group(1), m.group(2)) for i, line in enumerate(section)
            if (m := ROW.match(line))]


def _cell_profiles(cell: str) -> set[str]:
    names = set(re.findall(r"`([a-z]+)`", cell))
    if names == {"all"}:
        return set(NAMED_PROFILES)
    return {CELL_ALIASES.get(n, n) for n in names}


def _expected_profiles(tool: str) -> set[str]:
    return {p for p in NAMED_PROFILES if tool in profiles.get_profile_tools(p)}


class TestToolReferenceTable(unittest.TestCase):
    def setUp(self):
        self.rows = _table_rows(_tools_section(_read(TOOL_DOC)))

    def test_every_registered_tool_has_exactly_one_row(self):
        names = [name for _, name, _ in self.rows]
        missing = sorted(set(profiles.ALL_TOOLS) - set(names))
        dupes = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual(missing, [], "tools missing from the table")
        self.assertEqual(dupes, [], "tools listed more than once")

    def test_every_row_names_a_registered_tool(self):
        unknown = sorted({name for _, name, _ in self.rows} - set(profiles.ALL_TOOLS))
        self.assertEqual(unknown, [])

    def test_row_profiles_match_the_registry(self):
        wrong = {}
        for _, name, cell in self.rows:
            if name not in profiles.ALL_TOOLS:
                continue
            listed, expected = _cell_profiles(cell), _expected_profiles(name)
            if listed != expected:
                wrong[name] = {"listed": sorted(listed), "registry": sorted(expected)}
        self.assertEqual(wrong, {})

    def test_table_has_no_blank_line_inside(self):
        # A blank line ends a Markdown table; the rows after it render as raw text.
        first, last = self.rows[0][0], self.rows[-1][0]
        self.assertEqual(last - first + 1, len(self.rows))


class TestProfileCounts(unittest.TestCase):
    def _counts(self, path: str) -> dict[str, int]:
        return {m.group(1): int(m.group(2)) for line in _read(path).splitlines()
                if (m := COUNT_ROW.match(line))}

    def test_profile_counts_match_the_registry(self):
        expected = {p: len(profiles.get_profile_tools(p))
                    for p in (*NAMED_PROFILES, "all")}
        for path in (README, TOOL_DOC):
            with self.subTest(doc=os.path.relpath(path, ROOT)):
                self.assertEqual(self._counts(path), expected)

    def test_readme_total_matches_the_registry(self):
        self.assertIn("The server has %d tools." % len(profiles.ALL_TOOLS), _read(README))


if __name__ == "__main__":
    unittest.main()
