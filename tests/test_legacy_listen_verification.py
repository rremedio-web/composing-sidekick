import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._stage_passthrough import passthrough_listenable_staging
from tests.test_agy_listen_verification import (
    _staged_path_from_prompt,
    plant_listen_evidence,
)
from tools import audio_ref


class LegacyListenVerificationTests(unittest.TestCase):
    def _run(self, root: Path, convs: Path, proof: bool):
        wav = root / "source.wav"
        wav.write_bytes(b"RIFFfake")
        destination = root / "describe.txt"
        (root / "convs").mkdir(exist_ok=True)

        def fake_run(cmd, **kwargs):
            staged = _staged_path_from_prompt(cmd[cmd.index("--print") + 1])
            if proof:
                plant_listen_evidence(convs, "legacy-run", staged)
            (Path(kwargs["cwd"]) / "describe.txt").write_text(
                "# Identity\nA synthetic described loop with enough detail.\n" * 5)
            return audio_ref.subprocess.CompletedProcess(
                cmd, 0, json.dumps({"conversation_id": "legacy-run",
                                    "usage": {"input_tokens": 50}}), "")

        with passthrough_listenable_staging(), \
                mock.patch.object(audio_ref, "_which", return_value="/fake/agy"), \
                mock.patch.object(audio_ref, "_run", side_effect=fake_run):
            return audio_ref.run_agy_describe(
                wav=wav, describe_path=destination, prompt_extra=None,
                duration_s=10, timeout_s=30, conversations_dir=convs)

    def test_legacy_listener_rejects_without_result_proof(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = self._run(root, root / "convs", proof=False)
            self.assertFalse(result["ok"])
            self.assertEqual(result["listen_rejected"]["reason"],
                             "unverifiable")
            self.assertFalse((root / "describe.txt").exists())

    def test_legacy_listener_accepts_verified_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = self._run(root, root / "convs", proof=True)
            self.assertTrue(result["ok"])
            self.assertEqual(result["conversation_id"], "legacy-run")
            self.assertTrue(result["listen_verification"]["verified"])


if __name__ == "__main__":
    unittest.main()
