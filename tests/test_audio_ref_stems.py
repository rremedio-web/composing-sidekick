from __future__ import annotations

import inspect
import json
import math
import os
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mcp_server import ledger, live_write, server
from tools import audio_ref
from tests._stage_passthrough import passthrough_listenable_staging


def write_pulses(path: Path, onsets: list[float], *, duration: float = 2.0,
                 sample_rate: int = 22050) -> None:
    samples = [0] * int(duration * sample_rate)
    width = max(8, sample_rate // 100)
    for onset in onsets:
        start = int(onset * sample_rate)
        for index in range(start, min(start + width, len(samples))):
            samples[index] = 24000
    payload = bytearray()
    for sample in samples:
        payload.extend(int(sample).to_bytes(2, "little", signed=True))
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(bytes(payload))


class TestMeasuredStemGrid(unittest.TestCase):
    def test_energy_flux_is_deterministic_snapped_and_suppressed(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "drums.wav"
            write_pulses(wav, [0.25, 0.50, 0.52, 1.0, 1.5])
            first = audio_ref.measure_drums_grid(wav, 120)
            second = audio_ref.measure_drums_grid(wav, 120)
        self.assertEqual(first, second)
        self.assertIsNotNone(first)
        self.assertEqual(first["method"], "mono_energy_flux")
        self.assertEqual(first["grid"], "sixteenth")
        self.assertTrue(first["evidence_only"])
        self.assertFalse(first["transcription"])
        self.assertEqual(first["steps"], [2, 4, 8, 12])
        self.assertEqual(len(first["steps"]), len(set(first["steps"])))
        self.assertLessEqual(first["accepted_onsets"], 4)
        self.assertTrue(all(0 <= step < 16 for step in first["steps"]))

    def test_invalid_tempo_or_grid_falls_back_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "drums.wav"
            write_pulses(wav, [0.25, 0.5])
            self.assertIsNone(audio_ref.measure_drums_grid(wav, math.nan))
        notes, understood = audio_ref.measured_drum_grid_to_notes({
            "evidence_only": True, "tempo_bpm": 120, "steps": [0, 4, 4, 8],
            "method": "mono_energy_flux",
        })
        self.assertEqual([note["start"] for note in notes], [0, 1, 2])
        self.assertEqual(understood["source"], "measured_drums_stem")
        self.assertEqual(audio_ref.measured_drum_grid_to_notes(
            {"evidence_only": True, "tempo_bpm": 120, "steps": [16]})[0], [])
        self.assertEqual(audio_ref.measured_drum_grid_to_notes(
            {"evidence_only": True, "tempo_bpm": 0, "steps": [0]})[0], [])


class TestExplicitStemIngestion(unittest.TestCase):
    def test_analysis_copies_explicit_stems_without_source_path_leak(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "mix-source.wav"
            drums = root / "private-drums-name.wav"
            bass = root / "private-bass-name.wav"
            write_pulses(source, [0.25])
            write_pulses(drums, [0.25, 0.5, 1.0, 1.5])
            write_pulses(bass, [0.4])
            out_root = root / "artifacts"
            index_path = root / "index.json"

            def copy_wav(src, dst, _start, _end, **kwargs):
                shutil.copyfile(src, dst)

            with (mock.patch.object(audio_ref, "OUT_ROOT", out_root),
                  mock.patch.object(audio_ref, "INDEX_PATH", index_path),
                  mock.patch.object(audio_ref, "ffmpeg_to_wav", side_effect=copy_wav),
                  mock.patch.object(audio_ref, "ffprobe_info", return_value={
                      "duration_s": 2.0, "sample_rate": 22050,
                      "channels": 1, "codec": "pcm_s16le",
                  }),
                  mock.patch.object(audio_ref, "collect_tool_versions", return_value={})):
                result = audio_ref.analyze_audio_ref(
                    str(source), modes=["features"], midi_tempo=120,
                    stem_files={"drums": str(drums), "bass": str(bass)})

            out_dir = Path(result["out_dir"])
            self.assertTrue((out_dir / "stems" / "drums.wav").is_file())
            self.assertTrue((out_dir / "stems" / "bass.wav").is_file())
            self.assertTrue((out_dir / "measured_drum_grid.json").is_file())
            self.assertIsNotNone(result["measured_drum_grid"])
            meta_text = (out_dir / "meta.json").read_text(encoding="utf-8")
            self.assertNotIn(str(drums), meta_text)
            self.assertNotIn(str(bass), meta_text)
            self.assertEqual(
                {item["file"] for item in result["stem_artifacts"]},
                {"stems/drums.wav", "stems/bass.wav"})
            card = audio_ref.card_from_out_dir(out_dir)
            self.assertEqual(card["measured_drum_grid"], result["measured_drum_grid"])
            self.assertTrue(audio_ref.structure_is_usable(card))

    def test_stem_paths_and_roles_are_explicit_and_validated(self):
        with self.assertRaisesRegex(ValueError, "explicit local"):
            audio_ref._validate_stem_files({"drums": r"\\server\share\drums.wav"})
        with self.assertRaisesRegex(ValueError, "roles"):
            audio_ref._validate_stem_files({"../drums": "/tmp/nope.wav"})
        with self.assertRaisesRegex(ValueError, "roles"):
            audio_ref._validate_stem_files({1: "/tmp/nope.wav"})
        with self.assertRaisesRegex(ValueError, "readable"):
            audio_ref._validate_stem_files({"drums": "/tmp/does-not-exist.wav"})


class TestBeatGridPreference(unittest.TestCase):
    def test_measured_grid_precedes_prose_and_invalid_grid_falls_back(self):
        captured = []

        def beat_from_grid(notes, **kwargs):
            captured.append(notes)
            return {"notes": [], "tempo": kwargs.get("tempo") or 120,
                    "assumptions": []}

        measured = {
            "version": 1, "method": "mono_energy_flux", "tempo_bpm": 120,
            "grid": "sixteenth", "steps": [1, 5, 9, 13],
            "evidence_only": True, "transcription": False,
        }
        fake = SimpleNamespace(
            get_ref_card=lambda _rid: {"ownership": "his",
                                       "structure": {"density": "low"}},
            normalize_ownership=audio_ref.normalize_ownership,
            ref_steering=lambda _card: {
                "identity": "Ref", "tempo": 120,
                "measured_drum_grid": measured,
                "drum_grid": "kick on 1", "ricardo_note": None,
            },
            measured_drum_grid_to_notes=audio_ref.measured_drum_grid_to_notes,
            card_grid_to_notes=audio_ref.card_grid_to_notes,
            drum_grid_to_notes=lambda _text: self.fail("prose fallback used despite measured grid"),
        )
        with (mock.patch.object(server, "_audio_ref_mod", return_value=fake),
              mock.patch.object(server._core, "beat_from_grid", side_effect=beat_from_grid)):
            result = server.beat_from_ref("ref", humanize=False)
        self.assertEqual(result["ref"]["grid_source"], "measured_drums_stem")
        # captured[0] is the steered call; captured[1] is R2's unsteered
        # honesty check (empty grid), not the pick.
        self.assertEqual([note["start"] for note in captured[0]], [0.25, 1.25, 2.25, 3.25])

        fake.measured_drum_grid_to_notes = lambda _grid: ([], {})
        fake.drum_grid_to_notes = lambda _text: ([{
            "pitch": 36, "start": 0, "duration": 0.25, "velocity": 100}],
            {"source": "prose"})
        with (mock.patch.object(server, "_audio_ref_mod", return_value=fake),
              mock.patch.object(server._core, "beat_from_grid", side_effect=beat_from_grid)):
            result = server.beat_from_ref("ref", humanize=False)
        self.assertEqual(result["ref"]["grid_source"], "prose_drum_grid")

    def test_public_signature_exposes_stem_mapping(self):
        self.assertIn("stem_files", inspect.signature(server.analyze_audio_ref).parameters)


LOW_MONO_NOTES = [
    {"pitch": 36, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 80},
    {"pitch": 38, "start_beats": 0.5, "duration_beats": 0.5, "velocity": 80},
    {"pitch": 36, "start_beats": 1.0, "duration_beats": 0.5, "velocity": 80},
]
HIGH_MONO_NOTES = [
    {"pitch": 72, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 80},
    {"pitch": 74, "start_beats": 0.5, "duration_beats": 0.5, "velocity": 80},
    {"pitch": 72, "start_beats": 1.0, "duration_beats": 0.5, "velocity": 80},
]


class TestApplyAudioRefRoleStamping(unittest.TestCase):
    """The reference write must stamp the role the written notes actually carry.

    tools.apply_audio_ref does NOT resolve role="auto" — pkg["role"] just
    echoes the normalized argument — so the server call site classifies the
    notes it actually wrote; "drums" maps to the catalogue's "drum"; an
    explicit role is kept as given."""

    def _stamped_row(self, *, role_arg: str = "auto", notes: list) -> dict:
        # shaped like a real apply_audio_ref package: role == normalized arg
        pkg = {
            "ok": True, "ref_id": "yt-ref", "role": role_arg,
            "notes": notes,
            "note_count": len(notes), "tempo": 120.0, "clip_name": "ref test",
            # his-owned: anyone else's transcription stays a preview (R7)
            "card": {"ownership": "his"},
        }
        receipt = {"slot": "3/0", "track": "SIDEKICK JAM"}
        fake = SimpleNamespace(
            apply_audio_ref=lambda *_args, **_kwargs: pkg,
            normalize_ownership=audio_ref.normalize_ownership,
        )
        with tempfile.TemporaryDirectory() as td:
            ledger_path = os.path.join(td, "ledger.json")
            with (mock.patch.object(ledger, "LEDGER_PATH", ledger_path),
                  mock.patch.object(server, "_audio_ref_mod", return_value=fake),
                  mock.patch.object(live_write, "write_answer_to_jam_track",
                                    new=lambda notes, *, clip_name, tempo: receipt),
                  mock.patch.object(server, "_run_async", side_effect=lambda v: v)):
                result = server.apply_audio_ref("yt-ref", role=role_arg, write=True)
            rows = ledger.load(ledger_path)["entries"]
        self.assertEqual(result["live_write"], receipt)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_auto_low_monophonic_notes_stamp_bass(self):
        row = self._stamped_row(role_arg="auto", notes=LOW_MONO_NOTES)
        self.assertEqual(row["role"], "bass")
        self.assertEqual(row["source"], "ref:yt-ref")

    def test_auto_high_monophonic_notes_stamp_lead(self):
        self.assertEqual(
            self._stamped_row(role_arg="auto", notes=HIGH_MONO_NOTES)["role"], "lead")

    def test_drums_maps_to_catalogue_drum(self):
        self.assertEqual(
            self._stamped_row(role_arg="drums", notes=LOW_MONO_NOTES)["role"], "drum")

    def test_explicit_pad_is_kept_even_when_notes_suggest_bass(self):
        self.assertEqual(
            self._stamped_row(role_arg="pad", notes=LOW_MONO_NOTES)["role"], "pad")


class TestAgyDescribeEffortSuffix(unittest.TestCase):
    """Unit tests for the Antigravity effort-suffix heuristic in run_agy_describe."""

    def _capture_cmd(self, model):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "source.wav"
            wav.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
            describe_path = Path(td) / "describe.txt"

            captured: list[list[str]] = []

            def fake_runner(cmd, **kwargs):
                captured.append(list(cmd))
                return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with passthrough_listenable_staging(), \
                 mock.patch.object(audio_ref, "_which", return_value="/fake/agy"), \
                 mock.patch.object(audio_ref, "_run", side_effect=fake_runner):
                audio_ref.run_agy_describe(
                    wav=wav,
                    describe_path=describe_path,
                    prompt_extra=None,
                    focus="full",
                    duration_s=10.0,
                    source_label=None,
                    timeout_s=600,
                    model=model,
                    allow_unverified_listen=True,
                )
        return captured[0]

    def test_plain_model_includes_effort_high(self):
        cmd = self._capture_cmd("gemini-3.7-flash")
        self.assertIn("--effort", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")

    def test_high_suffix_excludes_effort(self):
        self.assertNotIn("--effort", self._capture_cmd("gemini-3.7-flash-high"))

    def test_medium_suffix_excludes_effort(self):
        self.assertNotIn("--effort", self._capture_cmd("gemini-3.7-flash-medium"))

    def test_low_suffix_excludes_effort(self):
        self.assertNotIn("--effort", self._capture_cmd("gemini-3.7-flash-low"))

    def test_explicit_plain_name_includes_effort_high(self):
        cmd = self._capture_cmd("claude-sonnet-4-6")
        self.assertIn("--effort", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")


if __name__ == "__main__":
    unittest.main()
