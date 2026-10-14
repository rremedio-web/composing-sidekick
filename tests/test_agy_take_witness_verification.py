"""Round-1 item 4: take and witness listens carry proof + token cap.

Same gate as card listens, applied after each agy call: the run must
show a view_file step on the staged audio within the token cap. A
rejected take listen raises a listen-rejected error naming the reason
and is never persisted as a take card; a rejected witness is skipped
fail-open with a listen-rejected warning and no witness card.
Synthetic only: fake runners, fixture conversation DBs, temp dirs.
"""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcp_server import session_ears
from tools import mix_witness as _mw
from tests._stage_passthrough import passthrough_listenable_staging
from tests.test_agy_listen_verification import (
    _staged_path_from_prompt,
    plant_deaf_evidence,
    plant_listen_evidence,
)
from tests.test_session_ears import _wav_bytes

_WITNESS_SAMPLE = """# Audible observations
kick reads forward

# Masking hypotheses
pad may cover bass body

# Preserve
keep snare bite

# Candidate checks
try slightly lower pad level

# Cannot determine
room tone not represented
"""


def _envelope_stdout(conversation_id: str, usage: dict | None = None) -> str:
    return json.dumps({
        "conversation_id": conversation_id,
        "usage": usage if usage is not None else {"input_tokens": 100},
    })


def _take_inventory(root: Path):
    session_ears._reset_registry_for_tests()
    trusted = root / "trusted"
    trusted.mkdir(exist_ok=True)
    (trusted / "take.wav").write_bytes(_wav_bytes())
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({
        "session_id": "proof-session",
        "takes": [{"track": "Vox", "clip_name": "take 1",
                   "path": str(trusted / "take.wav")}],
    }), encoding="utf-8")
    env = session_ears._inventory_from_manifest(manifest, trusted)
    return env, env["takes"][0]["take_id"]


def _take_card_for(prompt: str) -> dict:
    take_id = __import__("re").search(r"- take_id: ([^\n]+)", prompt).group(1)
    return {
        "take_id": take_id, "track": "Vox", "clip_name": "take 1",
        "role_guess": "lead", "vibe": "dry close vocal", "register": None,
        "density": "sparse", "pitch_claims": [], "timestamps": ["00:00"],
        "contract_version": session_ears.CONTRACT_VERSION,
    }


def _supported_kwargs(fn, **kw):
    """Drop proof kwargs the function under test does not accept yet, so
    the rejection tests fail behaviorally on code without the gate
    (a card is returned / persisted) instead of erroring on signature."""
    params = inspect.signature(fn).parameters
    return {k: v for k, v in kw.items() if k in params}


class TestTakeListenVerification(unittest.TestCase):
    def _listen(self, root: Path, convs: Path, fake_run):
        env, take_id = _take_inventory(root)
        try:
            with passthrough_listenable_staging(), \
                    mock.patch.object(session_ears, "_which",
                                      return_value="/fake/agy"):
                return session_ears.listen_to_takes(
                    env["inventory_id"], [take_id], runner=fake_run,
                    store_root=root / "store",
                    **_supported_kwargs(
                        session_ears.listen_to_takes,
                        conversations_dir=convs))
        finally:
            session_ears._reset_registry_for_tests()

    def _cards_path(self, root: Path) -> Path:
        # Cards live under a Set-keyed dir (dead-paths lane); any persisted
        # take_cards.json under the store counts.
        found = sorted((root / "store" / "session_ears").glob("*/take_cards.json"))
        return found[0] if found else (root / "store" / "session_ears"
                                       / "none" / "take_cards.json")

    def test_deaf_take_is_an_error_and_persists_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            root, convs = Path(td), Path(td) / "convs"
            convs.mkdir()

            def fake_run(cmd, **kwargs):
                staged = _staged_path_from_prompt(
                    cmd[cmd.index("--print") + 1])
                plant_deaf_evidence(convs, "conv-take-deaf")
                output = Path(__import__("re").search(
                    r"write the same JSON object to:\n([^\n]+)",
                    cmd[-1]).group(1))
                output.write_text(
                    json.dumps(_take_card_for(cmd[-1])), encoding="utf-8")
                assert staged
                return type("Proc", (), {
                    "returncode": 0,
                    "stdout": _envelope_stdout("conv-take-deaf"),
                    "stderr": ""})()

            # A rejected take is an errors entry, never a raise or a card.
            heard = self._listen(root, convs, fake_run)
            self.assertEqual(heard["cards"], [])
            self.assertEqual(len(heard["errors"]), 1)
            self.assertIn("no_listen", heard["errors"][0]["reason"])
            self.assertFalse(self._cards_path(root).is_file(),
                             "a rejected take must never persist a card")
            self.assertFalse(self._cards_path(root).is_file(),
                             "a rejected take must never persist a card")

    def test_over_budget_take_is_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root, convs = Path(td), Path(td) / "convs"
            convs.mkdir()

            def fake_run(cmd, **kwargs):
                staged = _staged_path_from_prompt(
                    cmd[cmd.index("--print") + 1])
                plant_listen_evidence(convs, "conv-take-rich", staged)
                output = Path(__import__("re").search(
                    r"write the same JSON object to:\n([^\n]+)",
                    cmd[-1]).group(1))
                output.write_text(
                    json.dumps(_take_card_for(cmd[-1])), encoding="utf-8")
                return type("Proc", (), {
                    "returncode": 0,
                    "stdout": _envelope_stdout(
                        "conv-take-rich", {"input_tokens": 300000}),
                    "stderr": ""})()

            # A rejected take is an errors entry, never a raise or a card.
            heard = self._listen(root, convs, fake_run)
            self.assertEqual(heard["cards"], [])
            self.assertEqual(len(heard["errors"]), 1)
            self.assertIn("over_budget", heard["errors"][0]["reason"])
            self.assertFalse(self._cards_path(root).is_file(),
                             "a rejected take must never persist a card")

    def test_unverifiable_take_is_an_error_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root, convs = Path(td), Path(td) / "convs"
            convs.mkdir()

            def fake_run(cmd, **kwargs):
                output = Path(__import__("re").search(
                    r"write the same JSON object to:\n([^\n]+)",
                    cmd[-1]).group(1))
                output.write_text(
                    json.dumps(_take_card_for(cmd[-1])), encoding="utf-8")
                return type("Proc", (), {
                    "returncode": 0, "stdout": "", "stderr": ""})()

            # A rejected take is an errors entry, never a raise or a card.
            heard = self._listen(root, convs, fake_run)
            self.assertEqual(heard["cards"], [])
            self.assertEqual(len(heard["errors"]), 1)
            self.assertIn("unverifiable", heard["errors"][0]["reason"])
            self.assertFalse(self._cards_path(root).is_file(),
                             "a rejected take must never persist a card")

    def test_verified_take_returns_card(self):
        with tempfile.TemporaryDirectory() as td:
            root, convs = Path(td), Path(td) / "convs"
            convs.mkdir()

            def fake_run(cmd, **kwargs):
                staged = _staged_path_from_prompt(
                    cmd[cmd.index("--print") + 1])
                plant_listen_evidence(convs, "conv-take-ok", staged)
                output = Path(__import__("re").search(
                    r"write the same JSON object to:\n([^\n]+)",
                    cmd[-1]).group(1))
                output.write_text(
                    json.dumps(_take_card_for(cmd[-1])), encoding="utf-8")
                return type("Proc", (), {
                    "returncode": 0,
                    "stdout": _envelope_stdout("conv-take-ok"),
                    "stderr": ""})()

            heard = self._listen(root, convs, fake_run)
            self.assertEqual(heard["errors"], [])
            self.assertEqual(len(heard["cards"]), 1)
            self.assertTrue(self._cards_path(root).is_file())


class TestWitnessVerification(unittest.TestCase):
    def _witness(self, root: Path, convs: Path, fake_run, trial_id: str):
        master = root / "master.wav"
        master.write_bytes(b"RIFFfake")
        with passthrough_listenable_staging(), \
                mock.patch.object(_mw, "_which", return_value="/fake/agy"):
            return _mw.run_mix_listener(
                master_path=str(master), group_paths={},
                trial_id=trial_id, listener=True, runner=fake_run,
                **_supported_kwargs(
                    _mw.run_mix_listener, conversations_dir=convs))

    def test_deaf_witness_skipped_with_reason_no_card(self):
        with tempfile.TemporaryDirectory() as td:
            root, convs = Path(td), Path(td) / "convs"
            convs.mkdir()

            def fake_run(cmd, **kwargs):
                prompt = cmd[-1]
                witness_path = Path(__import__("re").search(
                    r"absolute path \(create/overwrite\): (.+)",
                    prompt).group(1).strip())
                witness_path.write_text(_WITNESS_SAMPLE, encoding="utf-8")
                plant_deaf_evidence(convs, "conv-wit-deaf")
                return type("Proc", (), {
                    "returncode": 0,
                    "stdout": _envelope_stdout("conv-wit-deaf"),
                    "stderr": ""})()

            result = self._witness(root, convs, fake_run, "wit-deaf")
            self.assertFalse(result["ok"])
            self.assertNotIn("witness", result)
            self.assertIn("no_listen", result["warning"])
            self.assertEqual(result["listen_rejected"]["reason"], "no_listen")

    def test_verified_witness_returns_card(self):
        with tempfile.TemporaryDirectory() as td:
            root, convs = Path(td), Path(td) / "convs"
            convs.mkdir()

            def fake_run(cmd, **kwargs):
                prompt = cmd[-1]
                staged = _staged_path_from_prompt(
                    cmd[cmd.index("--print") + 1])
                plant_listen_evidence(convs, "conv-wit-ok", staged)
                witness_path = Path(__import__("re").search(
                    r"absolute path \(create/overwrite\): (.+)",
                    prompt).group(1).strip())
                witness_path.write_text(_WITNESS_SAMPLE, encoding="utf-8")
                return type("Proc", (), {
                    "returncode": 0,
                    "stdout": _envelope_stdout("conv-wit-ok"),
                    "stderr": ""})()

            result = self._witness(root, convs, fake_run, "wit-ok")
            self.assertTrue(result["ok"])
            self.assertIn("witness", result)


if __name__ == "__main__":
    unittest.main()
