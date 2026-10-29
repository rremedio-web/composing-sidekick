"""The core tools' schemas tell the client what each parameter means and
whether the tool writes (MCP ToolAnnotations)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CORE_TOOLS = (
    "get_style", "find_phrases", "hear_the_room", "listen_to_takes",
    "grow_my_loop", "answer_my_loop", "develop_my_loop", "judge_answer",
    "score_phrase", "export_midi", "write_midi_clip", "write_composed_to_jam",
    "close_session", "session_report",
)
READ_ONLY = {"get_style", "find_phrases", "grow_my_loop", "develop_my_loop",
             "score_phrase"}
# may delete or overwrite: judge_answer(confirm) deletes a JAM clip on DROP,
# export_midi replaces an existing .mid, write_midi_clip(replace) a clip
DESTRUCTIVE = {"judge_answer", "export_midi", "write_midi_clip"}

# A fresh interpreter with the 'all' profile, so the result never depends on
# which profile (or reloaded server) the rest of the suite left behind.
_DUMP = """
import json, sys
sys.path.insert(0, %r)
from mcp_server import server
out = {}
for t in server.mcp._tool_manager.list_tools():
    out[t.name] = {"parameters": t.parameters, "description": t.description,
                   "annotations": t.annotations.model_dump() if t.annotations else None}
print(json.dumps(out))
""" % ROOT


def _tools():
    env = dict(os.environ, SIDEKICK_MCP_PROFILE="all")
    proc = subprocess.run([sys.executable, "-c", _DUMP], capture_output=True,
                          text=True, timeout=120, cwd=ROOT, env=env)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    return {name: types.SimpleNamespace(
                parameters=t["parameters"], description=t["description"],
                annotations=(types.SimpleNamespace(**t["annotations"])
                             if t["annotations"] else None))
            for name, t in data.items()}


class TestCoreToolSchemas(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = _tools()

    def test_every_core_parameter_has_a_description(self):
        for name in CORE_TOOLS:
            props = self.tools[name].parameters.get("properties", {})
            for param, schema in props.items():
                with self.subTest(tool=name, param=param):
                    self.assertTrue(schema.get("description", "").strip())

    def test_annotations_match_what_the_tool_does(self):
        for name in CORE_TOOLS:
            ann = self.tools[name].annotations
            with self.subTest(tool=name):
                self.assertIsNotNone(ann)
                self.assertIs(ann.readOnlyHint, name in READ_ONLY)
                if name not in READ_ONLY:
                    self.assertIs(ann.destructiveHint, name in DESTRUCTIVE)
                # only listen_to_takes sends data outside (audio to the listener)
                self.assertIs(ann.openWorldHint, name == "listen_to_takes")

    def test_hear_the_room_does_not_claim_read_only(self):
        doc = self.tools["hear_the_room"].description
        self.assertNotIn("Read-only;", doc)
        self.assertIn("Not read-only", doc)

    def test_write_midi_clip_has_no_dead_parameters(self):
        props = self.tools["write_midi_clip"].parameters["properties"]
        for dead in ("source", "role", "gear", "metadata"):
            self.assertNotIn(dead, props)


if __name__ == "__main__":
    unittest.main()
