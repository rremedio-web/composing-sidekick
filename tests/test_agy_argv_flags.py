"""Lane B3 AGY-LISTENER item 5: argv flag probing + -low model slugs.

agy gains flags over time; the listener must not blindly pass flags an
older agy rejects. ``--disable-slash-commands`` is added to argv only
when ``agy --help`` lists it (probed once per process, fail-open).
The ``-low`` effort slug must stay choosable via config (env / --model)
without gaining a duplicate --effort. Synthetic only: no agy calls —
the probe itself is faked.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import audio_ref as _ar
from tools import mix_witness as _mw
from tests._stage_passthrough import passthrough_listenable_staging


class AgvFlagProbeCase(unittest.TestCase):
    MODULES = ()

    def setUp(self):
        for mod in self.MODULES:
            mod._agy_help_cache.clear()

    def tearDown(self):
        for mod in self.MODULES:
            mod._agy_help_cache.clear()


class TestDisableSlashCommands(AgvFlagProbeCase):
    MODULES = (_ar,)

    def test_flag_added_when_help_lists_it(self):
        with mock.patch.object(
                _ar, "_agy_supports_flag", return_value=True) as probe:
            cmd = _ar._agy_cmd_for("gemini-3.8-flash-medium", "/tmp/staged",
                                   "listen")
        self.assertIn("--disable-slash-commands", cmd)
        probe.assert_called_once_with(cmd[0], "--disable-slash-commands")

    def test_flag_omitted_when_help_lacks_it(self):
        with mock.patch.object(
                _ar, "_agy_supports_flag", return_value=False):
            cmd = _ar._agy_cmd_for("gemini-3.8-flash-medium", "/tmp/staged",
                                   "listen")
        self.assertNotIn("--disable-slash-commands", cmd)

    def test_probe_reads_help_text_once_and_caches(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return _ar.subprocess.CompletedProcess(
                argv, 0, "  --disable-slash-commands  drop / commands\n", "")

        with mock.patch.object(_ar.subprocess, "run",
                               side_effect=fake_run):
            self.assertTrue(_ar._agy_supports_flag("/fake/agy",
                                                   "--disable-slash-commands"))
            self.assertTrue(_ar._agy_supports_flag("/fake/agy",
                                                   "--disable-slash-commands"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ["/fake/agy", "--help"])

    def test_probe_missing_flag_and_missing_binary(self):
        def fake_run(argv, **kwargs):
            return _ar.subprocess.CompletedProcess(argv, 0, "  --print\n", "")

        with mock.patch.object(_ar.subprocess, "run",
                               side_effect=fake_run):
            self.assertFalse(_ar._agy_supports_flag("/fake/agy",
                                                    "--disable-slash-commands"))
        with mock.patch.object(_ar.subprocess, "run",
                               side_effect=OSError("no agy")):
            self.assertFalse(_ar._agy_supports_flag("/gone/agy",
                                                    "--disable-slash-commands"))

    def test_probe_uses_scrubbed_environment_and_temporary_cwd(self):
        from mcp_server import session_ears
        modules = (_ar, session_ears, _mw)
        with mock.patch.dict(os.environ, {"UNTRUSTED_PROBE_TEST": "secret"}):
            for index, module in enumerate(modules):
                module._agy_help_cache.clear()
                captured = {}

                def fake_run(argv, **kwargs):
                    captured.update(kwargs)
                    captured["cwd_exists_during_probe"] = Path(kwargs["cwd"]).is_dir()
                    return module.subprocess.CompletedProcess(argv, 0,
                                                               "--print", "")

                with mock.patch.object(module.subprocess, "run",
                                       side_effect=fake_run):
                    self.assertFalse(module._agy_supports_flag(
                        "/fake/agy-%d" % index, "--missing"))
                self.assertTrue(captured["cwd_exists_during_probe"])
                self.assertNotEqual(captured["cwd"], str(Path.cwd()))
                self.assertNotIn("UNTRUSTED_PROBE_TEST", captured["env"])
                env_builder = getattr(module, "_agy_subprocess_env",
                                      getattr(module, "_agy_listen_env", None))
                self.assertEqual(captured["env"], env_builder())
                module._agy_help_cache.clear()


class TestTakeAndWitnessFlags(AgvFlagProbeCase):
    MODULES = ()

    def test_take_cmd_honours_probe(self):
        from mcp_server import session_ears

        session_ears._agy_help_cache.clear()
        self.addCleanup(session_ears._agy_help_cache.clear)
        take = {"take_id": "t1", "track": "Vox", "clip_name": "take 1"}
        with mock.patch.object(session_ears, "_agy_supports_flag",
                               return_value=True):
            cmd = session_ears._build_agy_cmd(
                agy="/fake/agy", audio_dir=Path("/tmp/staged"),
                audio_name="take.wav", take=take, focus="full",
                model="gemini-3.8-flash-medium", timeout_s=300,
                output_path=Path("/tmp/staged/take_card.json"))
        self.assertIn("--disable-slash-commands", cmd)
        with mock.patch.object(session_ears, "_agy_supports_flag",
                               return_value=False):
            cmd = session_ears._build_agy_cmd(
                agy="/fake/agy", audio_dir=Path("/tmp/staged"),
                audio_name="take.wav", take=take, focus="full",
                model="gemini-3.8-flash-medium", timeout_s=300,
                output_path=Path("/tmp/staged/take_card.json"))
        self.assertNotIn("--disable-slash-commands", cmd)

    def test_witness_cmd_honours_probe(self):
        _mw._agy_help_cache.clear()
        self.addCleanup(_mw._agy_help_cache.clear)
        with mock.patch.object(_mw, "_agy_supports_flag", return_value=True):
            cmd = _mw._build_agy_cmd(
                agy="/fake/agy", witness_path=Path("/tmp/staged/witness.txt"),
                audio_dir=Path("/tmp/staged"), audio_manifest="- master: m.wav",
                target="", model="gemini-3.8-flash-medium", timeout_s=300)
        self.assertIn("--disable-slash-commands", cmd)
        with mock.patch.object(_mw, "_agy_supports_flag", return_value=False):
            cmd = _mw._build_agy_cmd(
                agy="/fake/agy", witness_path=Path("/tmp/staged/witness.txt"),
                audio_dir=Path("/tmp/staged"), audio_manifest="- master: m.wav",
                target="", model="gemini-3.8-flash-medium", timeout_s=300)
        self.assertNotIn("--disable-slash-commands", cmd)


class TestLowModelSlug(unittest.TestCase):
    def test_low_slug_via_env_used_verbatim_without_effort(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "source.wav"
            wav.write_bytes(b"RIFFfake")
            captured: dict = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = list(cmd)
                return _ar.subprocess.CompletedProcess(cmd, 1, "", "nope")

            with passthrough_listenable_staging(), \
                 mock.patch.object(_ar, "_which", return_value="agy"), \
                 mock.patch.object(_ar, "_run", side_effect=fake_run), \
                 mock.patch.dict(os.environ,
                                 {"AUDIO_REF_MODEL": "gemini-3.8-flash-low"}):
                _ar.run_agy_describe_v3(
                    wav=wav, focus="full", prompt_extra=None, duration_s=60,
                    timeout_s=61)
            cmd = captured["cmd"]
            self.assertEqual(
                cmd[cmd.index("--model") + 1], "gemini-3.8-flash-low")
            self.assertNotIn("--effort", cmd)

    def test_low_slug_via_explicit_model(self):
        cmd = _ar._agy_cmd_for("gemini-3.8-flash-low", "/tmp/staged", "hi")
        self.assertIn("gemini-3.8-flash-low", cmd)
        self.assertNotIn("--effort", cmd)


if __name__ == "__main__":
    unittest.main()
