"""Regression coverage for finish-my-idea tool guidance."""
import unittest
from unittest.mock import patch

from mcp_server import core, profiles


class TestFinishMyIdeaGuidance(unittest.TestCase):
    def test_guidance_names_only_tools_exposed_by_compose(self):
        # Note shape from Producer Pal read_clip notes.
        seed_notes = [
            {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90},
        ]
        continuation = {
            "guidance": "Keep developing the seed motif.",
            "seed_summary": {"notes": 1},
        }
        structure = {"sections": []}
        with patch.object(core, "continue_clip", return_value=continuation), \
                patch.object(core, "critique", return_value={}), \
                patch.object(core, "suggest_structure", return_value=structure), \
                patch.object(core, "_fold_persisted_session_takes",
                             return_value=structure):
            result = core.finish_my_idea(seed_notes)

        guidance = " ".join((result["guidance"],
                             result["continuation_brief"]["guidance"]))
        exposed = profiles.get_profile_tools("compose")
        named_tools = {"arrange_song", "write_arrangement", "lay_the_song",
                       "three_endings"}
        self.assertTrue({"lay_the_song", "three_endings"} <= exposed)
        self.assertFalse((named_tools - exposed) & set(guidance.split()))
        self.assertIn("lay_the_song", guidance)
        self.assertIn("three_endings", guidance)
        self.assertIn("preview", guidance.lower())
        self.assertIn("confirm", guidance.lower())
