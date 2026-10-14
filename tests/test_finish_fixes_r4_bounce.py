"""Batch 5 final review: bounce length floor, no-copy duration reads, and
accurate refusals for RF64 / empty WAV files."""
import os
import struct
import unittest
import wave

from mcp_server import rituals
from tests.test_finish_fixes_r3 import _BounceLedgerBase


def _wav_bytes(frames: int, rate: int = 8000) -> bytes:
    # Shape of a 16-bit mono PCM RIFF/WAVE exporter file.
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    data = b"\0\0" * frames
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(data)) + data)
    return b"RIFF" + struct.pack("<I", len(body)) + body


class BounceLengthTests(_BounceLedgerBase):
    def test_bounce_shorter_than_one_second_is_refused(self):
        path = self.write("short.wav", _wav_bytes(4000))           # 0.5 s
        with self.assertRaisesRegex(ValueError, "bounce too short"):
            rituals.confirm_bounce("Song", path)

    def test_one_second_bounce_is_accepted(self):
        path = self.write("ok.wav", _wav_bytes(8000))
        self.assertEqual(rituals.confirm_bounce("Song", path)["duration_s"], 1.0)

    def test_header_only_wav_says_it_has_no_audio(self):
        path = self.write("silent-header.wav", _wav_bytes(0))
        with self.assertRaisesRegex(ValueError, "no audio data"):
            rituals.confirm_bounce("Song", path)

    def test_empty_file_says_it_is_empty(self):
        path = self.write("empty.wav", b"")
        with self.assertRaisesRegex(ValueError, "bounce file is empty"):
            rituals.confirm_bounce("Song", path)

    def test_rf64_wav_is_named_as_unsupported(self):
        body = b"WAVE" + b"ds64" + struct.pack("<I", 28) + b"\0" * 28
        path = self.write("big.wav", b"RF64" + b"\xff\xff\xff\xff" + body)
        with self.assertRaisesRegex(ValueError, "RF64"):
            rituals.confirm_bounce("Song", path)


class _SliceSpy:
    """A read-only buffer that records the largest slice anyone takes."""

    def __init__(self, data: bytes):
        self.data, self.largest = data, 0

    def __len__(self):
        return len(self.data)

    def __getitem__(self, key):
        chunk = self.data[key]
        if isinstance(key, slice):
            self.largest = max(self.largest, len(chunk))
        return chunk


class NoCopyDurationTests(unittest.TestCase):
    def test_wave_duration_never_copies_the_data_chunk(self):
        spy = _SliceSpy(_wav_bytes(80000))                          # 160 KB data
        self.assertEqual(rituals._wave_duration(spy), 10.0)
        self.assertLess(spy.largest, 64)

    def test_aiff_duration_never_copies_the_sound_chunk(self):
        rate = struct.pack(">H", 16398) + struct.pack(">Q", 8000 << 48)
        comm = struct.pack(">hIh", 1, 80000, 16) + rate
        sound = struct.pack(">II", 0, 0) + b"\0" * 160000
        body = (b"AIFF" + b"COMM" + struct.pack(">I", len(comm)) + comm
                + b"SSND" + struct.pack(">I", len(sound)) + sound)
        spy = _SliceSpy(b"FORM" + struct.pack(">I", len(body)) + body)
        self.assertEqual(rituals._aiff_duration(spy), 10.0)
        self.assertLess(spy.largest, 64)

    def test_real_wave_module_file_still_reads(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "w.wav")
            with wave.open(path, "wb") as out:
                out.setnchannels(2); out.setsampwidth(3); out.setframerate(44100)
                out.writeframes(b"\0" * 6 * 44100 * 2)
            with open(path, "rb") as fh:
                self.assertAlmostEqual(rituals._wave_duration(fh.read()), 2.0)


if __name__ == "__main__":
    unittest.main()
