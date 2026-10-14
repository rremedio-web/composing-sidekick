"""Lane B3 AGY-LISTENER item 4: 3m full-track / 2m window timeouts.

The pilot's deaf loop burned quota for far too long per turn. Full
tracks now get --print-timeout 3m and Pass B windows 2m, both
configurable (timeout_s / window_timeout_s, CLI --timeout-s /
--window-timeout-s). Synthetic only: fake runners, temp dirs.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import audio_ref as _ar
from tests.test_agy_listen_verification import (
    _staged_path_from_prompt,
    plant_listen_evidence,
)
from tests.test_agy_workspace_isolation import _v3_envelope
from tests._stage_passthrough import passthrough_listenable_staging


def _verified_payload(conversation_id="conv-t"):
    payload = _v3_envelope()
    payload["conversation_id"] = conversation_id
    payload["usage"] = {"input_tokens": 100}
    return payload


class TestListenTimeouts(unittest.TestCase):
    def test_full_track_print_timeout_defaults_to_3m(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "source.wav"
            wav.write_bytes(b"RIFFfake")
            convs = Path(td) / "convs"
            captured: dict = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = list(cmd)
                plant_listen_evidence(
                    convs, "conv-t",
                    _staged_path_from_prompt(
                        cmd[cmd.index("--print") + 1]))
                return _ar.subprocess.CompletedProcess(
                    cmd, 0, json.dumps(_verified_payload()), "")

            with passthrough_listenable_staging(), \
                 mock.patch.object(_ar, "_which", return_value="agy"), \
                    mock.patch.object(_ar, "_run", side_effect=fake_run):
                result = _ar.run_agy_describe_v3(
                    wav=wav, focus="full", prompt_extra=None, duration_s=60,
                    model="gemini-3.8-flash-high", verify_listen=True,
                    conversations_dir=convs)
            self.assertTrue(result["ok"])
            self.assertEqual(
                captured["cmd"][captured["cmd"].index("--print-timeout") + 1],
                "3m")

    def test_window_print_timeout_defaults_to_2m(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "source.wav"
            wav.write_bytes(b"RIFFfake")
            convs = Path(td) / "convs"
            seen: list[str] = []

            def fake_cut(src, dst, start_s, end_s):
                shutil.copyfile(src, dst)

            def fake_run(cmd, **kwargs):
                seen.append(
                    cmd[cmd.index("--print-timeout") + 1])
                plant_listen_evidence(
                    convs, "conv-w",
                    _staged_path_from_prompt(
                        cmd[cmd.index("--print") + 1]))
                return _ar.subprocess.CompletedProcess(
                    cmd, 0, json.dumps(_verified_payload("conv-w")), "")

            card = dict(_v3_envelope())
            card["sections"] = [
                {"t": "00:10", "energy": 5, "label": "drop",
                 "layers_in": [], "layers_out": []},
            ]
            with passthrough_listenable_staging(), \
                 mock.patch.object(_ar, "_which", return_value="agy"), \
                    mock.patch.object(_ar, "_run", side_effect=fake_run):
                res = _ar.run_pass_b_windows(
                    wav=wav, v3_card=card, duration_s=200.0, focus="full",
                    out_dir=Path(td) / "ref", model="m", cut_fn=fake_cut,
                    verify_listen=True, conversations_dir=convs)
            self.assertTrue(any(w.get("ok") for w in res["windows"]))
            self.assertTrue(seen)
            self.assertTrue(all(t == "2m" for t in seen))

    def test_analyze_window_timeout_follows_tighter_knob(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "refs" / "new"
            out.mkdir(parents=True)
            (out / "source.wav").write_bytes(b"RIFFfake")
            convs = root / "convs"
            forwarded: dict = {}

            def fake_run(cmd, **kwargs):
                if "--add-dir" in cmd:
                    plant_listen_evidence(
                        convs, "conv-a",
                        _staged_path_from_prompt(
                            cmd[cmd.index("--print") + 1]))
                    return _ar.subprocess.CompletedProcess(
                        cmd, 0, json.dumps(_verified_payload("conv-a")), "")
                return _ar.subprocess.CompletedProcess(cmd, 0, "", "")

            def fake_windows(**kw):
                forwarded.update(kw)
                return {"windows": [], "usage": {}}

            old_root, old_index = _ar.OUT_ROOT, _ar.INDEX_PATH
            _ar.OUT_ROOT, _ar.INDEX_PATH = root / "refs", root / "index.json"
            try:
                with passthrough_listenable_staging(), \
                     mock.patch.object(_ar, "_which",
                                       return_value="agy"), \
                     mock.patch.object(_ar, "_run",
                                       side_effect=fake_run), \
                     mock.patch.object(_ar, "ffprobe_info",
                                       return_value={"duration_s": 200}), \
                     mock.patch.object(_ar, "_version_first_line",
                                       return_value=None), \
                     mock.patch.object(_ar, "run_pass_b_windows",
                                       side_effect=fake_windows):
                    result = _ar.analyze_audio_ref(
                        from_dir=str(out), modes=["describe"], pass_b=True,
                        conversations_dir=convs)
                self.assertTrue(result["mode_ok"]["describe"])
                # Default full-track 180 s, windows capped at 120 s.
                self.assertEqual(result["timeout_s"], 180)
                self.assertEqual(forwarded["timeout_s"], 120)

                with passthrough_listenable_staging(), \
                     mock.patch.object(_ar, "_which",
                                       return_value="agy"), \
                     mock.patch.object(_ar, "_run",
                                       side_effect=fake_run), \
                     mock.patch.object(_ar, "ffprobe_info",
                                       return_value={"duration_s": 200}), \
                     mock.patch.object(_ar, "_version_first_line",
                                       return_value=None), \
                     mock.patch.object(_ar, "run_pass_b_windows",
                                       side_effect=fake_windows):
                    _ar.analyze_audio_ref(
                        from_dir=str(out), modes=["describe"], pass_b=True,
                        timeout_s=60, window_timeout_s=300,
                        conversations_dir=convs)
                self.assertEqual(forwarded["timeout_s"], 300)
            finally:
                _ar.OUT_ROOT, _ar.INDEX_PATH = old_root, old_index

    def test_cli_timeout_defaults(self):
        args = _ar.cli_parser().parse_args([])
        self.assertEqual(args.timeout_s, 180)
        self.assertIsNone(args.window_timeout_s)


if __name__ == "__main__":
    unittest.main()
