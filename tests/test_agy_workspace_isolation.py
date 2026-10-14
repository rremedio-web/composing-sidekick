"""Lane B3 AGY-LISTENER item 1: every agy listen runs with cwd=<staged dir>.

The 2026-09-25 pilot showed agy inheriting the repo as its workspace
(workspaceDirs=[<repo>, /tmp/audioref-listen-*]) because _run() never set
cwd. The agent then read the repo instead of the wav. Every listener —
card listens (v2/v3 incl. Pass B windows, which share the v3 harness),
the older run_agy_describe path, the session-ears take listener, and the
mix witness — must run agy with cwd set to the staged-audio directory
that contains ONLY the audio file, and --add-dir must name that same
directory. Synthetic only: fake runners, temp dirs, never agy/Live.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import audio_ref as _ar
from tools import mix_witness as _mw
from tests._stage_passthrough import passthrough_listenable_staging

REPO_ROOT = os.getcwd()

_VALID_DESCRIBE = (
    "# Identity\n"
    "A test drone that is heard, not read, with enough words here.\n"
    "\n"
    "# Groove\n"
    "- Sparse pulse.\n"
    "\n"
    "# Harmony / melody\n"
    "- One note.\n"
    "\n"
    "# Texture / production\n"
    "- Dry.\n"
    "\n"
    "# Imitation rules\n"
    "- Do: listen first.\n"
    "- Don't: read the repo.\n"
    "\n"
    "# Moments\n"
    "- 00:05 – the only moment.\n"
)


def _v3_envelope():
    from tests.test_relisten_v2_listener import _v3_card

    payload = _v3_card()
    payload["usage"] = {
        "input_tokens": 15000, "output_tokens": 800,
        "thinking_tokens": 100, "cache_read_tokens": 4000,
    }
    payload["num_turns"] = 1
    payload["duration_seconds"] = 12.5
    return payload


def _assert_isolated_listen(testcase, cmd, kwargs, wav_parent):
    """cwd and --add-dir are the staged dir; the repo/out dir never appears."""
    add_dir = Path(cmd[cmd.index("--add-dir") + 1])
    cwd = kwargs.get("cwd")
    testcase.assertIsNotNone(cwd, "agy must run with cwd=<staged dir>")
    testcase.assertEqual(
        Path(cwd), add_dir,
        "agy cwd and --add-dir must be the same staged directory")
    testcase.assertNotEqual(
        Path(cwd), Path(wav_parent),
        "agy must not run with the source/out dir as its workspace")
    testcase.assertNotEqual(os.path.realpath(cwd), REPO_ROOT)
    for element in cmd:
        testcase.assertNotIn(
            REPO_ROOT, str(element),
            "repo path must never appear in agy argv")
    testcase.assertFalse(
        str(cwd).startswith(REPO_ROOT + os.sep),
        "agy cwd must never sit inside the repo")


class TestCardListenerWorkspaceIsolation(unittest.TestCase):
    def test_v3_listen_runs_with_cwd_staged_dir(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "out"
            out_dir.mkdir()
            # Stale re-listen contaminants: must never reach the listener.
            (out_dir / "describe.txt").write_text(_VALID_DESCRIBE)
            (out_dir / "meta.json").write_text("{}")
            wav = out_dir / "source.wav"
            wav.write_bytes(b"RIFFfake")
            captured: dict = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = list(cmd)
                captured["kwargs"] = dict(kwargs)
                staged = Path(cmd[cmd.index("--add-dir") + 1])
                captured["entries"] = sorted(os.listdir(staged))
                return _ar.subprocess.CompletedProcess(
                    cmd, 0, json.dumps(_v3_envelope()), "")

            with passthrough_listenable_staging(), \
                    mock.patch.object(_ar, "_which", return_value="agy"), \
                    mock.patch.object(_ar, "_run", side_effect=fake_run):
                result = _ar.run_agy_describe_v3(
                    wav=wav, focus="full", prompt_extra=None, duration_s=60,
                    timeout_s=61, model="gemini-3.8-flash-high")
            self.assertTrue(result["ok"])
            _assert_isolated_listen(
                self, captured["cmd"], captured["kwargs"], out_dir)
            self.assertEqual(captured["entries"], ["source.wav"])

    def test_legacy_describe_runs_with_cwd_staged_dir(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "out"
            out_dir.mkdir()
            (out_dir / "describe.txt").write_text("stale card the model must not see\n" * 10)
            (out_dir / "meta.json").write_text("{}")
            wav = out_dir / "source.wav"
            wav.write_bytes(b"RIFFfake")
            describe_path = out_dir / "describe.txt"
            if describe_path.is_file():
                describe_path.unlink()  # fresh listen, stale file removed
            captured: dict = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = list(cmd)
                captured["kwargs"] = dict(kwargs)
                staged = Path(cmd[cmd.index("--add-dir") + 1])
                captured["entries"] = sorted(os.listdir(staged))
                # The agent is confined to cwd: it writes describe.txt there.
                agent_cwd = kwargs.get("cwd") or out_dir
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
            _assert_isolated_listen(
                self, captured["cmd"], captured["kwargs"], out_dir)
            self.assertEqual(captured["entries"], ["source.wav"])


class TestTakeListenerWorkspaceIsolation(unittest.TestCase):
    def test_take_listen_runs_with_cwd_staging_dir(self):
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
                "session_id": "iso-session",
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
            _assert_isolated_listen(
                self, captured["cmd"], captured["kwargs"], trusted)
        finally:
            session_ears._reset_registry_for_tests()
            tmp.cleanup()


class TestMixWitnessWorkspaceIsolation(unittest.TestCase):
    def test_witness_runs_with_cwd_audio_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            master = root / "master.wav"
            master.write_bytes(b"RIFFfake")
            captured: dict = {}

            def failing_runner(cmd, **kwargs):
                captured["cmd"] = list(cmd)
                captured["kwargs"] = dict(kwargs)
                return _mw.subprocess.CompletedProcess(cmd, 1, "", "boom")

            with passthrough_listenable_staging(), \
                    mock.patch.object(_mw, "_which", return_value="/fake/agy"):
                _mw.run_mix_listener(
                    master_path=str(master), group_paths={},
                    trial_id="iso-trial", listener=True,
                    runner=failing_runner)
            _assert_isolated_listen(
                self, captured["cmd"], captured["kwargs"], root)


if __name__ == "__main__":
    unittest.main()
