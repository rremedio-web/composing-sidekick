"""Round-1 item 3: only a verified-but-malformed card may be repaired.

A repair retry spends a second full listener turn. When attempt 1 never
listened (no_listen), blew the token cap (over_budget), or has no
readable record (unverifiable), a second turn cannot fix the card — the
harness must verify attempt 1 first and return the listen rejection
without repairing. Only a verified-but-malformed card earns the repair.
Synthetic only: fake runners, fixture conversation DBs, temp dirs.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import audio_ref as _ar
from tests._stage_passthrough import passthrough_listenable_staging
from tests.test_agy_listen_verification import (
    _staged_path_from_prompt,
    plant_deaf_evidence,
    plant_listen_evidence,
)
from tests.test_agy_workspace_isolation import _v3_envelope


def _malformed_stdout(conversation_id: str) -> str:
    """Stdout that fails card validation but carries an envelope id."""
    return json.dumps({
        "conversation_id": conversation_id,
        "usage": {"input_tokens": 100},
        "note": "prose, not a card",
    })


def _valid_stdout(conversation_id: str) -> str:
    payload = _v3_envelope()
    payload["conversation_id"] = conversation_id
    payload["usage"] = {"input_tokens": 100}
    return json.dumps(payload)


class TestRepairRetryGatedOnVerification(unittest.TestCase):
    def _run_v3(self, td: Path, convs: Path, fake_run):
        wav = td / "source.wav"
        wav.write_bytes(b"RIFFfake")
        with passthrough_listenable_staging(), \
                mock.patch.object(_ar, "_which", return_value="agy"), \
                mock.patch.object(_ar, "_run", side_effect=fake_run):
            return _ar.run_agy_describe_v3(
                wav=wav, focus="full", prompt_extra=None, duration_s=60,
                timeout_s=61, model="gemini-3.8-flash-high",
                verify_listen=True, conversations_dir=convs)

    def test_deaf_malformed_card_is_rejected_without_repair(self):
        with tempfile.TemporaryDirectory() as td:
            root, convs = Path(td), Path(td) / "convs"
            convs.mkdir()
            calls: list = []

            def fake_run(cmd, **kwargs):
                calls.append(list(cmd))
                staged = _staged_path_from_prompt(
                    cmd[cmd.index("--print") + 1])
                plant_deaf_evidence(convs, "conv-deaf")
                return _ar.subprocess.CompletedProcess(
                    cmd, 0, _malformed_stdout("conv-deaf"), "")

            result = self._run_v3(root, convs, fake_run)
            self.assertFalse(result["ok"])
            self.assertEqual(len(calls), 1, "a deaf run must not be repaired")
            self.assertEqual(result["attempts"], 1)
            self.assertIn("no_listen", result["error"])
            self.assertEqual(result["listen_rejected"]["reason"], "no_listen")
            self.assertEqual(result["listen_rejected"]["conversation_id"],
                             "conv-deaf")

    def test_unverifiable_malformed_card_is_rejected_without_repair(self):
        with tempfile.TemporaryDirectory() as td:
            root, convs = Path(td), Path(td) / "convs"
            convs.mkdir()
            calls: list = []

            def fake_run(cmd, **kwargs):
                calls.append(list(cmd))
                # No record planted: the id has no readable conversation.
                return _ar.subprocess.CompletedProcess(
                    cmd, 0, _malformed_stdout("conv-ghost"), "")

            result = self._run_v3(root, convs, fake_run)
            self.assertFalse(result["ok"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(result["attempts"], 1)
            self.assertIn("unverifiable", result["error"])
            self.assertEqual(result["listen_rejected"]["reason"],
                             "unverifiable")

    def test_over_budget_malformed_card_is_rejected_without_repair(self):
        with tempfile.TemporaryDirectory() as td:
            root, convs = Path(td), Path(td) / "convs"
            convs.mkdir()
            calls: list = []

            def fake_run(cmd, **kwargs):
                calls.append(list(cmd))
                staged = _staged_path_from_prompt(
                    cmd[cmd.index("--print") + 1])
                plant_listen_evidence(convs, "conv-rich", staged)
                return _ar.subprocess.CompletedProcess(
                    cmd, 0, json.dumps({
                        "conversation_id": "conv-rich",
                        "usage": {"input_tokens": 300000},
                        "note": "prose, not a card",
                    }), "")

            result = self._run_v3(root, convs, fake_run)
            self.assertFalse(result["ok"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(result["attempts"], 1)
            self.assertIn("over_budget", result["error"])
            self.assertEqual(result["listen_rejected"]["reason"],
                             "over_budget")

    def test_verified_malformed_card_is_repaired(self):
        with tempfile.TemporaryDirectory() as td:
            root, convs = Path(td), Path(td) / "convs"
            convs.mkdir()
            calls: list = []

            def fake_run(cmd, **kwargs):
                calls.append(list(cmd))
                staged = _staged_path_from_prompt(
                    cmd[cmd.index("--print") + 1])
                if len(calls) == 1:
                    plant_listen_evidence(convs, "conv-r1", staged)
                    return _ar.subprocess.CompletedProcess(
                        cmd, 0, _malformed_stdout("conv-r1"), "")
                plant_listen_evidence(convs, "conv-r2", staged)
                return _ar.subprocess.CompletedProcess(
                    cmd, 0, _valid_stdout("conv-r2"), "")

            result = self._run_v3(root, convs, fake_run)
            self.assertTrue(result["ok"])
            self.assertEqual(len(calls), 2)
            self.assertEqual(result["attempts"], 2)
            self.assertEqual(result["conversation_id"], "conv-r2")


if __name__ == "__main__":
    unittest.main()
