"""Lane B3 AGY-LISTENER item 2: exact listening procedure in the prompt.

Without an explicit first instruction the pilot agent treated the card
task as a repo-research task (53 tool requests, 0 wav view_file calls).
Every listen prompt must therefore lead with one sentence naming the
ABSOLUTE staged audio path:

  Your first tool action is view_file on <path>. Do not run
  commands, list directories, search, or read any other file.

Applies to the card listener (v2/v3 incl. Pass B windows, which share
the v3 harness), the legacy describe path, the session-ears take
listener, and the mix witness. Synthetic only: fake runners, temp dirs.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import audio_ref as _ar
from tools import mix_witness as _mw
from tests._stage_passthrough import passthrough_listenable_staging
from tests.test_agy_workspace_isolation import (
    _VALID_DESCRIBE,
    _v3_envelope,
)

_DIRECTIVE_PREFIXES = ("Your first tool action is view_file on ",
                       "Your first and only tool action is view_file on ")
_DIRECTIVE_SUFFIX = ("Do not run commands, list directories, search, "
                     "or read any other file.")


def _directive_path(prompt: str) -> str:
    lines = prompt.splitlines()
    assert lines, "prompt must not be empty"
    first = lines[0]
    multi_prefix = "Your first tool actions are view_file on each of: "
    if first.startswith(multi_prefix):
        rest = first[len(multi_prefix):]
        path = rest.split(",", 1)[0].split(". Do not run commands", 1)[0]
        assert os.path.isabs(path), "directive must name an absolute path"
        assert "Do not run commands" in first
        return path
    prefix = next((item for item in _DIRECTIVE_PREFIXES
                   if first.startswith(item)), None)
    assert prefix, \
        "prompt must lead with the listening procedure, got: %r" % first[:120]
    rest = first[len(prefix):]
    assert _DIRECTIVE_SUFFIX in rest, \
        "directive must forbid other tool actions, got: %r" % first[:200]
    path = rest.split(_DIRECTIVE_SUFFIX)[0].strip().rstrip(".")
    assert os.path.isabs(path), "directive must name an absolute path"
    return path


class TestCardListenDirective(unittest.TestCase):
    def test_v3_prompt_leads_with_absolute_staged_path(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "source.wav"
            wav.write_bytes(b"RIFFfake")
            captured: dict = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = list(cmd)
                return _ar.subprocess.CompletedProcess(
                    cmd, 0, json.dumps(_v3_envelope()), "")

            with passthrough_listenable_staging(), \
                    mock.patch.object(_ar, "_which", return_value="agy"), \
                    mock.patch.object(_ar, "_run", side_effect=fake_run):
                result = _ar.run_agy_describe_v3(
                    wav=wav, focus="full", prompt_extra=None, duration_s=60,
                    timeout_s=61, model="gemini-3.8-flash-high")
            self.assertTrue(result["ok"])
            prompt = captured["cmd"][captured["cmd"].index("--print") + 1]
            staged = _directive_path(prompt)
            add_dir = captured["cmd"][
                captured["cmd"].index("--add-dir") + 1]
            self.assertEqual(staged, os.path.join(add_dir, "source.wav"))
            # The rest of the v3 prompt is kept.
            self.assertIn("Return exactly one JSON object matching the "
                          "provided schema.", prompt)
            self.assertIn("Allowed values, exactly:", prompt)

    def test_legacy_describe_prompt_leads_with_absolute_staged_path(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "source.wav"
            wav.write_bytes(b"RIFFfake")
            describe_path = Path(td) / "describe.txt"
            captured: dict = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = list(cmd)
                agent_cwd = kwargs.get("cwd") or td
                (Path(agent_cwd) / "describe.txt").write_text(_VALID_DESCRIBE)
                return _ar.subprocess.CompletedProcess(cmd, 0, "", "")

            with passthrough_listenable_staging(), \
                    mock.patch.object(_ar, "_which", return_value="agy"), \
                    mock.patch.object(_ar, "_run", side_effect=fake_run):
                result = _ar.run_agy_describe(
                    wav=wav, describe_path=describe_path, prompt_extra=None,
                    focus="full", duration_s=60, timeout_s=61,
                    model="gemini-3.8-flash-high",
                    allow_unverified_listen=True)
            self.assertTrue(result["ok"])
            prompt = captured["cmd"][captured["cmd"].index("--print") + 1]
            staged = _directive_path(prompt)
            add_dir = captured["cmd"][
                captured["cmd"].index("--add-dir") + 1]
            self.assertEqual(staged, os.path.join(add_dir, "source.wav"))
            # The legacy task body is kept.
            self.assertIn("# Imitation rules", prompt)


class TestPassBWindowDirective(unittest.TestCase):
    def test_window_prompt_leads_with_absolute_staged_window_path(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "source.wav"
            wav.write_bytes(b"RIFFfake")
            card = {"sections": [
                {"t": "00:10", "energy": 5, "label": "drop",
                 "layers_in": [], "layers_out": []},
                {"t": "01:30", "energy": 4, "label": "break",
                 "layers_in": [], "layers_out": []},
                {"t": "02:30", "energy": 3, "label": "outro",
                 "layers_in": [], "layers_out": []},
            ]}
            prompts: list[str] = []

            def fake_cut(src, dst, start_s, end_s):
                shutil.copyfile(src, dst)

            def fake_run(cmd, **kwargs):
                prompts.append(cmd[cmd.index("--print") + 1])
                return _ar.subprocess.CompletedProcess(
                    cmd, 0, json.dumps(_v3_envelope()), "")

            with passthrough_listenable_staging(), \
                    mock.patch.object(_ar, "_which", return_value="agy"), \
                    mock.patch.object(_ar, "_run", side_effect=fake_run):
                res = _ar.run_pass_b_windows(
                    wav=wav, v3_card=card, duration_s=200.0, focus="full",
                    out_dir=Path(td) / "ref", model="gemini-3.8-flash-high",
                    timeout_s=61, cut_fn=fake_cut)
            heard = [w for w in res["windows"] if w.get("ok")]
            self.assertTrue(heard, "expected at least one heard window")
            self.assertTrue(prompts)
            for prompt in prompts:
                staged = _directive_path(prompt)
                self.assertTrue(
                    staged.endswith(".wav") and "window" in
                    os.path.basename(staged),
                    "window directive must name the staged window file, "
                    "got: %r" % staged)


class TestTakeListenDirective(unittest.TestCase):
    def test_take_prompt_leads_with_absolute_staged_take_path(self):
        from mcp_server import session_ears
        from tests.test_session_ears import _wav_bytes

        session_ears._reset_registry_for_tests()
        tmp = tempfile.TemporaryDirectory()
        try:
            root = Path(tmp.name)
            trusted = root / "trusted"
            trusted.mkdir()
            audio = trusted / "take.wav"
            audio.write_bytes(_wav_bytes())
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "session_id": "dir-session",
                "takes": [{"track": "Vox", "clip_name": "take 1",
                           "path": str(audio)}],
            }), encoding="utf-8")
            env = session_ears._inventory_from_manifest(manifest, trusted)
            take_id = env["takes"][0]["take_id"]
            captured: dict = {}

            def failing_runner(cmd, **kwargs):
                captured["cmd"] = list(cmd)
                captured["kwargs"] = dict(kwargs)
                raise OSError("simulated agy failure")

            with passthrough_listenable_staging(), \
                    mock.patch.object(session_ears, "_which",
                                      return_value="/fake/agy"):
                heard = session_ears.listen_to_takes(
                    env["inventory_id"], [take_id],
                    runner=failing_runner,
                    store_root=root / "store")
                self.assertEqual(heard["cards"], [])
                self.assertEqual(len(heard["errors"]), 1)
            prompt = captured["cmd"][-1]
            staged = _directive_path(prompt)
            add_dir = captured["cmd"][
                captured["cmd"].index("--add-dir") + 1]
            self.assertEqual(os.path.dirname(staged), add_dir)
            self.assertTrue(staged.endswith(".wav"))
        finally:
            session_ears._reset_registry_for_tests()
            tmp.cleanup()


class TestWitnessDirective(unittest.TestCase):
    def test_witness_proof_checks_master_and_every_group_stem(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            master = root / "master.wav"
            drums = root / "drums.wav"
            bass = root / "bass.wav"
            for path in (master, drums, bass):
                path.write_bytes(b"RIFFfake")

            def fake_run(cmd, **kwargs):
                text = ("# Audible observations\nclear\n"
                        "# Masking hypotheses\nnone\n# Preserve\nroughness\n"
                        "# Candidate checks\nlisten\n# Cannot determine\nroom\n")
                return _mw.subprocess.CompletedProcess(
                    cmd, 0, text, "")

            checked = []
            def verify(**kwargs):
                checked.append(Path(kwargs["staged_audio_path"]).name)
                return {"ok": True, "verified": True, "usage": {}}

            with passthrough_listenable_staging(), \
                    mock.patch.object(_mw, "_which", return_value="/fake/agy"), \
                    mock.patch.object(_mw, "_agy_supports_flag", return_value=False), \
                    mock.patch.object(_mw, "verify_agy_listen", side_effect=verify):
                result = _mw.run_mix_listener(
                    master_path=str(master), group_paths={
                        "drums": str(drums), "bass": str(bass)},
                    trial_id="proof-all", listener=True, runner=fake_run)
            self.assertTrue(result["ok"])
            self.assertEqual(checked, [
                "master.wav", "group-01-bass.wav", "group-02-drums.wav"])

    def test_witness_prompt_leads_with_absolute_staged_master_path(self):
        with tempfile.TemporaryDirectory() as td:
            master = Path(td) / "master.wav"
            master.write_bytes(b"RIFFfake")
            captured: dict = {}

            def failing_runner(cmd, **kwargs):
                captured["cmd"] = list(cmd)
                return _mw.subprocess.CompletedProcess(cmd, 1, "", "boom")

            with passthrough_listenable_staging(), \
                    mock.patch.object(_mw, "_which",
                                      return_value="/fake/agy"):
                _mw.run_mix_listener(
                    master_path=str(master), group_paths={},
                    trial_id="dir-trial", listener=True,
                    runner=failing_runner)
            prompt = captured["cmd"][-1]
            staged = _directive_path(prompt)
            add_dir = captured["cmd"][
                captured["cmd"].index("--add-dir") + 1]
            self.assertEqual(staged, os.path.join(add_dir, "master.wav"))


if __name__ == "__main__":
    unittest.main()
