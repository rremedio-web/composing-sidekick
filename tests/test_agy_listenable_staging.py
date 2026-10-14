"""Item 2 (round-1 review): every agy listen stages a transcoded plain-PCM copy.

Real agy conversation records show the listener can only open a plain
16-bit PCM WAV (fmt tag 1): raw session takes (24-bit/float
WAVE_FORMAT_EXTENSIBLE) and mp3s fail with step_type 17 "unsupported
mime type". These tests stage a synthetic WAVE_FORMAT_EXTENSIBLE input
(built with struct, no ffmpeg needed to generate it) and require the
staged output to be plain PCM. They fail on the old code, which
hard-linked/copied the raw file into the staged dir.
"""

import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools import audio_ref

ffmpeg_to_wav = audio_ref.ffmpeg_to_wav
staged_audio_only = audio_ref.staged_audio_only
stage_listenable_copy = getattr(audio_ref, "stage_listenable_copy", None)

FFMPEG = shutil.which("ffmpeg")


def write_extensible_wav(path: Path, *, sample_rate=48000, seconds=1,
                         channels=1) -> None:
    """Write a 24-bit WAVE_FORMAT_EXTENSIBLE wav (a raw-take stand-in)."""
    n_frames = sample_rate * seconds
    pcm_data = b"\x00\x00\x00" * n_frames * channels
    channel_mask = 0x4 if channels == 1 else 0x3  # front-center / front L+R
    # PCM subformat GUID {00000001-0000-0010-8000-00AA00389B71}.
    subformat = struct.pack("<IHH8s", 1, 0, 0x10, bytes([0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71]))
    fmt = struct.pack(
        "<HHIIHHH",
        0xFFFE,  # WAVE_FORMAT_EXTENSIBLE
        channels,
        sample_rate,
        sample_rate * 3 * channels,
        3 * channels,  # block align
        24,  # bits per sample
        22,  # cbSize
    ) + struct.pack("<HI", 24, channel_mask) + subformat
    with open(path, "wb") as fh:
        fh.write(b"RIFF")
        fh.write(struct.pack("<I", 36 + len(fmt) + 8 + len(pcm_data)))
        fh.write(b"WAVE")
        fh.write(b"fmt ")
        fh.write(struct.pack("<I", len(fmt)))
        fh.write(fmt)
        fh.write(b"data")
        fh.write(struct.pack("<I", len(pcm_data)))
        fh.write(pcm_data)


def read_wav_format(path: Path) -> tuple[int, int, int]:
    """Return (format tag, channels, bits per sample) from the fmt chunk."""
    raw = path.read_bytes()
    pos = raw.index(b"fmt ")
    return (
        struct.unpack_from("<H", raw, pos + 8)[0],
        struct.unpack_from("<H", raw, pos + 10)[0],
        struct.unpack_from("<H", raw, pos + 22)[0],
    )


def require_ffmpeg(test: unittest.TestCase) -> None:
    if not FFMPEG:
        test.skipTest("ffmpeg not on PATH")


class ListenableStagingTest(unittest.TestCase):
    def test_stage_listenable_copy_produces_plain_pcm(self) -> None:
        require_ffmpeg(self)
        self.assertIsNotNone(stage_listenable_copy)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "raw-take.wav"
            write_extensible_wav(src)
            self.assertEqual(read_wav_format(src), (0xFFFE, 1, 24))  # fixture sanity
            dst = Path(tmp) / "staged.wav"
            stage_listenable_copy(src, dst)
            self.assertEqual(read_wav_format(dst), (1, 1, 16))

    def test_stage_listenable_copy_keeps_stereo_channels(self) -> None:
        require_ffmpeg(self)
        self.assertIsNotNone(stage_listenable_copy)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "raw-stereo.wav"
            write_extensible_wav(src, channels=2)
            self.assertEqual(read_wav_format(src), (0xFFFE, 2, 24))  # fixture sanity
            dst = Path(tmp) / "staged.wav"
            stage_listenable_copy(src, dst)
            self.assertEqual(read_wav_format(dst), (1, 2, 16))

    def test_stage_listenable_copy_caps_six_channels_to_stereo(self) -> None:
        require_ffmpeg(self)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "six-channel.wav"
            write_extensible_wav(src, channels=6)
            dst = Path(tmp) / "staged.wav"
            stage_listenable_copy(src, dst)
            self.assertEqual(read_wav_format(dst), (1, 2, 16))

    def test_ffmpeg_to_wav_requests_pcm_s16le_with_timeout(self) -> None:
        captured: dict = {}

        def fake_run(cmd: list[str], **kwargs):  # noqa: ANN001, ANN202
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            Path(cmd[-1]).touch()
            return subprocess.CompletedProcess(cmd, 0, "", "")

        real_run = audio_ref._run
        audio_ref._run = fake_run  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ffmpeg_to_wav(Path(tmp) / "in.wav", Path(tmp) / "out.wav",
                              None, None, timeout_s=30)
        finally:
            audio_ref._run = real_run  # type: ignore[assignment]
        cmd = captured["cmd"]
        self.assertIn("pcm_s16le", cmd)
        self.assertEqual(captured["kwargs"].get("timeout"), 30)

    def test_staged_audio_only_transcodes_instead_of_linking(self) -> None:
        require_ffmpeg(self)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "source.wav"
            write_extensible_wav(src)
            with staged_audio_only(str(src)) as staged:
                self.assertEqual(read_wav_format(Path(staged)), (1, 1, 16))
                self.assertEqual(Path(staged).name, "source.wav")

    def test_mix_witness_stages_plain_pcm_wav(self) -> None:
        require_ffmpeg(self)
        from tools.mix_witness import _stage_audio_inputs

        with tempfile.TemporaryDirectory() as tmp:
            master = Path(tmp) / "master-take.wav"
            write_extensible_wav(master)
            staging = Path(tmp) / "staging"
            staging.mkdir()
            manifest = _stage_audio_inputs(str(master), {}, staging)
            self.assertIn("master.wav", manifest)
            self.assertEqual(read_wav_format(staging / "master.wav"), (1, 1, 16))

    def test_session_ears_take_stages_plain_pcm_wav(self) -> None:
        require_ffmpeg(self)
        from mcp_server.session_ears import _stage_audio

        with tempfile.TemporaryDirectory() as tmp:
            take_path = Path(tmp) / "take24.wav"
            write_extensible_wav(take_path)
            staging = Path(tmp) / "staging"
            staging.mkdir()
            take = {"take_id": "take-1", "path": str(take_path)}
            staged, _duration = _stage_audio(take, staging)
            self.assertEqual(staged.name, "take.wav")
            self.assertEqual(read_wav_format(staged), (1, 1, 16))


if __name__ == "__main__":
    unittest.main()
