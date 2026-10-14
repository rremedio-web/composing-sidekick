import tempfile
import unittest
from pathlib import Path

from mcp_server import session_ears
from tools import mix_witness


class AgyPromptDirectiveTests(unittest.TestCase):
    def test_take_prompt_allows_required_output_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "take_card.json"
            cmd = session_ears._build_agy_cmd(
                agy="/fake/agy", audio_dir=root, audio_name="take.wav",
                take={"take_id": "t", "track": "Vox", "clip_name": "take"},
                focus="full", model="gemini-3.8-flash-medium", timeout_s=120,
                output_path=output)
            prompt = cmd[cmd.index("--print") + 1]
            self.assertTrue(prompt.startswith(
                "Your first tool action is view_file on " + str(root / "take.wav")))
            self.assertIn("write the same JSON object to:\n" + str(output), prompt)
            self.assertNotIn("first and only tool action", prompt)

    def test_mix_prompt_requires_each_file_and_allows_witness_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "master.wav").write_bytes(b"")
            (root / "group-01-drums.wav").write_bytes(b"")
            witness = root / "witness.txt"
            cmd = mix_witness._build_agy_cmd(
                agy="/fake/agy", witness_path=witness, audio_dir=root,
                audio_manifest="- master: master.wav\n- group drums: group-01-drums.wav",
                target="", model="gemini-3.8-flash-medium", timeout_s=120)
            prompt = cmd[cmd.index("--print") + 1]
            self.assertTrue(prompt.startswith(
                "Your first tool actions are view_file on each of: "))
            self.assertIn(str(root / "master.wav"), prompt)
            self.assertIn(str(root / "group-01-drums.wav"), prompt)
            self.assertIn("Write ONLY to this absolute path", prompt)
            self.assertIn(str(witness), prompt)
            self.assertNotIn("first and only tool action", prompt)


if __name__ == "__main__":
    unittest.main()
