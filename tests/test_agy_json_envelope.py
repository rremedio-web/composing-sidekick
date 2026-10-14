"""Every agy listener runs with --output-format json (merge review, B1).

The proof-of-listening check reads the conversation_id (and the token cap reads
usage) from agy's JSON envelope on stdout. Plain ``--print`` output carries no
envelope, so without the flag every real take / witness listen would be
rejected as ``unverifiable``. These tests pin the flag in each argv and prove a
real-shaped envelope-only stdout (card inside ``result``, no output file)
yields a verified card end to end, including through capture_loop.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcp_server import session_ears
from tests._stage_passthrough import passthrough_listenable_staging
from tests.test_agy_listen_verification import (
    _staged_path_from_prompt, plant_listen_evidence)
from tests.test_agy_take_witness_verification import _take_inventory
from tools import audio_ref, mix_witness


def _card_for(prompt: str) -> dict:
    take_id = re.search(r"- take_id: ([^\n]+)", prompt).group(1)
    track = re.search(r"- track: ([^\n]+)", prompt).group(1)
    clip = re.search(r"- clip_name: ([^\n]+)", prompt).group(1)
    return {
        "take_id": take_id, "track": track, "clip_name": clip,
        "role_guess": "lead", "vibe": "dry close vocal", "register": None,
        "density": "sparse", "pitch_claims": [], "timestamps": ["00:00"],
        "contract_version": session_ears.CONTRACT_VERSION,
    }


def _envelope_runner(convs: Path, *, prove: bool = True):
    """A real-shaped agy --output-format json turn: the card is ONLY in the
    envelope's result text (no output file); proof planted unless deaf."""
    def run(cmd, **kwargs):
        prompt = cmd[cmd.index("--print") + 1]
        cid = "conv-envelope"
        if prove:
            plant_listen_evidence(convs, cid, _staged_path_from_prompt(prompt))
        stdout = json.dumps({
            "conversation_id": cid,
            "result": json.dumps(_card_for(prompt)),
            "usage": {"input_tokens": 1200, "cache_read_tokens": 0},
            "num_turns": 2,
        })
        return type("Proc", (), {"returncode": 0, "stdout": stdout,
                                 "stderr": ""})()
    return run


class TestArgvCarriesJsonOutput(unittest.TestCase):
    def test_take_argv_requests_json_envelope(self):
        cmd = session_ears._build_agy_cmd(
            agy="agy", audio_dir=Path("/tmp/x"), audio_name="take.wav",
            take={"take_id": "t", "track": "Vox", "clip_name": "c",
                  "analyzed_start_s": 0.0},
            focus="full", model="gemini-3.8-flash-medium", timeout_s=120,
            output_path=Path("/tmp/x/take_card.json"))
        i = cmd.index("--output-format")
        self.assertEqual(cmd[i + 1], "json")
        self.assertLess(i, cmd.index("--print"))

    def test_witness_argv_requests_json_envelope(self):
        cmd = mix_witness._build_agy_cmd(
            agy="agy", witness_path=Path("/tmp/x/witness.txt"),
            audio_dir=Path("/tmp/x"), audio_manifest="master.wav",
            target="", model="gemini-3.8-flash-medium", timeout_s=120)
        i = cmd.index("--output-format")
        self.assertEqual(cmd[i + 1], "json")
        self.assertLess(i, cmd.index("--print"))


class TestPrintText(unittest.TestCase):
    def test_envelope_result_is_unwrapped(self):
        self.assertEqual(audio_ref.agy_print_text(
            json.dumps({"conversation_id": "c", "result": "# Heading\nbody"})),
            "# Heading\nbody")

    def test_structured_output_is_unwrapped(self):
        self.assertEqual(json.loads(audio_ref.agy_print_text(
            json.dumps({"conversation_id": "c",
                        "structured_output": {"a": 1}}))), {"a": 1})

    def test_plain_stdout_passes_through(self):
        self.assertEqual(audio_ref.agy_print_text("# Audible\nok\n"),
                         "# Audible\nok")
        card = {"take_id": "t", "vibe": "x"}
        self.assertEqual(json.loads(audio_ref.agy_print_text(
            json.dumps(card))), card)


class TestEnvelopeOnlyTakeListen(unittest.TestCase):
    def _listen(self, root, convs, runner):
        env, take_id = _take_inventory(root)
        try:
            with passthrough_listenable_staging(), \
                    mock.patch.object(session_ears, "_which",
                                      return_value="/fake/agy"):
                return session_ears.listen_to_takes(
                    env["inventory_id"], [take_id], runner=runner,
                    store_root=root / "store", conversations_dir=convs)
        finally:
            session_ears._reset_registry_for_tests()

    def test_verified_envelope_only_listen_yields_card(self):
        with tempfile.TemporaryDirectory() as td:
            root, convs = Path(td), Path(td) / "convs"
            convs.mkdir()
            heard = self._listen(root, convs, _envelope_runner(convs))
            self.assertEqual(heard["errors"], [])
            self.assertEqual(len(heard["cards"]), 1)
            self.assertIs(heard["cards"][0]["listen_verified"], True)

    def test_unverified_override_card_is_stamped_unverified(self):
        with tempfile.TemporaryDirectory() as td:
            root, convs = Path(td), Path(td) / "convs"
            convs.mkdir()
            env, take_id = _take_inventory(root)
            try:
                with passthrough_listenable_staging(), \
                        mock.patch.object(session_ears, "_which",
                                          return_value="/fake/agy"):
                    heard = session_ears.listen_to_takes(
                        env["inventory_id"], [take_id],
                        runner=_envelope_runner(convs, prove=False),
                        store_root=root / "store", conversations_dir=convs,
                        allow_unverified_listen=True)
            finally:
                session_ears._reset_registry_for_tests()
            # no record at all → unverifiable; the override accepts it but the
            # card must say it was not verified
            self.assertEqual(len(heard["cards"]), 1)
            self.assertIs(heard["cards"][0]["listen_verified"], False)


if __name__ == "__main__":
    unittest.main()
