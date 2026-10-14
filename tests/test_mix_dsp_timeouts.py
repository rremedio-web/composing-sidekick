"""Subprocess deadlines on audio measurement and provenance calls."""
import subprocess
import unittest
from unittest import mock

from tools import audio_ref, mix_dsp


class TestToolTimeouts(unittest.TestCase):
    def test_mix_dsp_runner_has_default_deadline(self):
        completed = subprocess.CompletedProcess([], 0, "ok", "")
        with mock.patch.object(mix_dsp.subprocess, "run", return_value=completed) as run:
            mix_dsp._run(["ffmpeg"])
        self.assertEqual(run.call_args.kwargs["timeout"], 300)

    def test_mix_dsp_runner_wraps_timeout(self):
        timeout = subprocess.TimeoutExpired(["ffmpeg"], 300)
        with mock.patch.object(mix_dsp.subprocess, "run", side_effect=timeout):
            with self.assertRaises(mix_dsp.MixDspError):
                mix_dsp._run(["ffmpeg"])

    def test_version_timeout_is_best_effort(self):
        with mock.patch.object(audio_ref, "_run", side_effect=subprocess.TimeoutExpired("ffmpeg", 120)):
            self.assertIsNone(audio_ref._version_first_line(["ffmpeg", "-version"]))

    def test_collect_tool_versions_omits_timed_out_tools(self):
        with mock.patch.object(audio_ref, "_which", side_effect=lambda name: "/fake/" + name), \
             mock.patch.object(audio_ref, "_version_first_line",
                               side_effect=subprocess.TimeoutExpired("tool", 120)):
            versions = audio_ref.collect_tool_versions()
        self.assertNotIn("ffmpeg", versions)
        self.assertNotIn("ffprobe", versions)


if __name__ == "__main__":
    unittest.main()
