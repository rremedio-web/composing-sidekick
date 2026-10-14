from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FinishingDocsTests(unittest.TestCase):
    def test_version_courage_is_described_as_version_family_comparison(self):
        docs = (ROOT / "config/claude_mcp.md").read_text()
        row = next(line for line in docs.splitlines() if "`version_courage`" in line)
        self.assertIn("version-family comparison", row.lower())
        self.assertNotIn("Snapshot current session state", row)

    def test_playbook_has_loops_to_finished_track_flow(self):
        docs = (ROOT / "docs/CURSOR_CHAT_PLAYBOOK.md").read_text()
        self.assertIn("## Loops → finished track", docs)
        flow = docs.split("## Loops → finished track", 1)[1].split("\n## ", 1)[0]
        tools = [
            "bury_or_marry", "version_courage", "whats_missing('live')",
            "lay_the_song", "no_new_notes", "three_endings",
            "one_hour_close", "confirm_bounce", "close_session",
        ]
        positions = [flow.index(tool) for tool in tools]
        self.assertEqual(positions, sorted(positions))

    def test_finishing_guidance_preserves_intent_and_scope(self):
        diagnosis = (ROOT / ".claude/skills/sombra-brain/references/diagnosis.md").read_text().lower()
        for stale in ("usually holes", "#1 finisher tell", "never learned a b"):
            self.assertNotIn(stale, diagnosis)
        finisher = (ROOT / "mcp_server/finisher.py").read_text()
        docstring = finisher.split("def whats_missing(", 1)[1].split("\n    if project_dict", 1)[0]
        self.assertIn("intended_findings", docstring)
        self.assertIn("only when Ricardo explicitly said", docstring)

        playbook = (ROOT / "docs/CURSOR_CHAT_PLAYBOOK.md").read_text()
        flow = playbook.split("## Loops → finished track", 1)[1].split("\n## ", 1)[0]
        self.assertIn("compares the members of a version family", flow.lower())

        mcp = (ROOT / "config/claude_mcp.md").read_text()
        self.assertIn("`confirm_bounce`", mcp)
        self.assertIn("`declare_finishing_state`", mcp)
        self.assertIn("`complete_draft` only", mcp.lower())
        self.assertIn("declare_finishing.py", mcp)


if __name__ == "__main__":
    unittest.main()
