import math
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import capture_support
from mcp_server.capture_support import (
    AudioValidationError,
    CaptureBridgeClient,
    CaptureError,
    CaptureTimeout,
    CleanupTransaction,
    analyze_captured_audio,
    capture_duration_seconds,
)


def wav(samples, channels=1, bits=16, rate=100):
    width = (bits + 7) // 8
    raw = b""
    for x in samples:
        if bits == 8:
            raw += bytes((int(x) + 128,) )
        else:
            raw += int(x).to_bytes(width, "little", signed=True)
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * channels * bits // 8,
                      channels * bits // 8, bits)
    body = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(raw)) + raw
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body


def aiff_stereo_16():
    # 44.1 kHz IEEE-754 80-bit extended representation used by AIFF.
    comm = struct.pack(">HIH", 2, 2, 16) + bytes.fromhex("400eac44000000000000")
    raw = struct.pack(">hhhh", 32767, -32768, -32768, 32767)
    ssnd = struct.pack(">II", 0, 0) + raw
    body = b"COMM" + struct.pack(">I", len(comm)) + comm + b"SSND" + struct.pack(">I", len(ssnd)) + ssnd
    return b"FORM" + struct.pack(">I", len(body) + 4) + b"AIFF" + body


def aiff_pcm(values, bits=16, channels=1, frames=None):
    width = (bits + 7) // 8
    frames = len(values) // channels if frames is None else frames
    raw = b"".join(int(x).to_bytes(width, "big", signed=True) for x in values)
    comm = struct.pack(">HIH", channels, frames, bits) + bytes.fromhex("400eac44000000000000")
    ssnd = struct.pack(">II", 0, 0) + raw
    body = b"COMM" + struct.pack(">I", len(comm)) + comm + b"SSND" + struct.pack(">I", len(ssnd)) + ssnd
    return b"FORM" + struct.pack(">I", len(body) + 4) + b"AIFF" + body


class TestTiming(unittest.TestCase):
    def test_exact_signatures(self):
        self.assertEqual(capture_duration_seconds(1, 4, 4, 120), 2.0)
        self.assertEqual(capture_duration_seconds(1, 3, 4, 120), 1.5)
        self.assertEqual(capture_duration_seconds(1, 5, 4, 120), 2.5)

    def test_rejects_bad_values(self):
        for args in ((True, 4, 4, 120), (1.5, 4, 4, 120), (1, 4, 3, 120),
                     (1, 4, 4, 0), (1, 4, 4, math.inf), (1, 4, 4, 120.0)):
            if args == (1, 4, 4, 120.0):
                continue
            with self.assertRaises(ValueError):
                capture_duration_seconds(*args)


class TestBridgeClient(unittest.TestCase):
    def test_contract_and_retry(self):
        calls = []
        def transport(batches):
            calls.append(batches)
            op = batches[0][0]["op"]
            state = "recording" if op == "capture_start" else ("ready" if op == "capture_status" else "finalized")
            result = {"capture_id": "opaque", "state": state}
            if op == "capture_finalize":
                result.update(path="t0/s2", clip_id=1, cleanup={})
            return [{"ok": True, "results": [result]}]

        c = CaptureBridgeClient(transport, clock=lambda: 0.0, sleep=lambda _: None)
        self.assertEqual(c.start("opaque", 0, 2, 100)["state"], "recording")
        self.assertEqual(c.start("opaque", 0, 2, 100)["state"], "recording")
        self.assertEqual(c.wait_ready("opaque")["state"], "ready")
        self.assertEqual(c.finalize("opaque")["state"], "finalized")
        self.assertEqual([x[0][0]["op"] for x in calls], ["capture_start", "capture_status", "capture_status", "capture_finalize"])

    def test_conflict_and_malformed_fail_closed(self):
        def transport(batches):
            return [{"ok": True, "results": [{"capture_id": "one", "state": "recording"}]}]
        c = CaptureBridgeClient(transport)
        c.start("one", 0, 0, 100)
        with self.assertRaises(CaptureError):
            c.start("two", 0, 0, 100)
        with self.assertRaises(ValueError):
            c.start("one", 0, 0, 100, source_track=1)

    def test_timeout_keeps_cleanup_receipt(self):
        now = [0.0]
        ops = []

        def transport(batches):
            op = batches[0][0]["op"]
            ops.append(op)
            state = "cancelled" if op == "capture_cancel" else "recording"
            result = {"capture_id": "x", "state": state}
            if op == "capture_cancel":
                result["cleanup"] = {"stopped": True}
            return [{"ok": True, "results": [result]}]

        def sleep(dt):
            now[0] += dt
        c = CaptureBridgeClient(transport, clock=lambda: now[0], sleep=sleep, poll_interval_s=0.2, timeout_s=0.3)
        c.start("x", 0, 0, 100)
        with self.assertRaises(CaptureTimeout) as ctx:
            c.wait_ready("x")
        self.assertEqual(ctx.exception.cleanup_receipt["state"], "cancelled")
        self.assertEqual(ops[-1], "capture_cancel")

    def test_sequential_captures_are_allowed_but_old_id_conflicts(self):
        def transport(batches):
            op = batches[0][0]["op"]
            cid = batches[0][0]["capture_id"]
            state = {"capture_start": "recording", "capture_status": "ready",
                     "capture_finalize": "finalized"}[op]
            result = {"capture_id": cid, "state": state}
            if op == "capture_finalize":
                result.update(path="t0/s0", clip_id=1, cleanup={})
            return [{"ok": True, "results": [result]}]
        c = CaptureBridgeClient(transport)
        c.start("one", 0, 0, 100)
        c.finalize("one")
        self.assertEqual(c.start("two", 0, 1, 100)["capture_id"], "two")
        with self.assertRaises(CaptureError):
            c.start("one", 0, 1, 100)

    def test_hostile_responses_and_lost_start_cancel_recovery(self):
        mode = ["mismatch"]

        def transport(batches):
            op = batches[0][0]["op"]
            cid = batches[0][0]["capture_id"]
            if mode[0] == "mismatch":
                return [{"ok": True, "results": [{"capture_id": "other", "state": "recording"}]}]
            if mode[0] == "extra":
                return [{"ok": True, "results": [{"capture_id": cid, "state": "recording", "path": "/secret/project"}]}]
            if mode[0] == "lost" and op == "capture_start":
                raise TimeoutError("/secret/project")
            if op == "capture_cancel":
                return [{"ok": True, "results": [{"capture_id": cid, "state": "cancelled", "cleanup": {"stopped": True}}]}]
            return [{"ok": True, "results": [{"capture_id": cid, "state": "recording"}]}]

        c = CaptureBridgeClient(transport)
        with self.assertRaises(CaptureError):
            c.start("one", 0, 0, 100)
        mode[0] = "extra"
        with self.assertRaises(CaptureError):
            c.start("one", 0, 0, 100)
        mode[0] = "lost"
        with self.assertRaises(CaptureError):
            c.start("lost", 0, 0, 100)
        self.assertEqual(c.cancel("lost")["state"], "cancelled")
        with self.assertRaises(CaptureError):
            c.status("lost")

    def test_illegal_regression_terminal_cancel_and_bad_cleanup(self):
        phase = ["start"]

        def transport(batches):
            op = batches[0][0]["op"]
            cid = batches[0][0]["capture_id"]
            if op == "capture_start":
                return [{"ok": True, "results": [{"capture_id": cid, "state": "recording"}]}]
            if phase[0] == "regress":
                return [{"ok": True, "results": [{"capture_id": cid, "state": "recording"}]}]
            if op == "capture_status":
                return [{"ok": True, "results": [{"capture_id": cid, "state": "ready"}]}]
            if op == "capture_finalize":
                return [{"ok": True, "results": [{"capture_id": cid, "state": "finalized", "path": "t0/s0", "clip_id": 1, "cleanup": {}}]}]
            return [{"ok": True, "results": [{"capture_id": cid, "state": "cancelled", "cleanup": []}]}]

        c = CaptureBridgeClient(transport)
        c.start("one", 0, 0, 100)
        c.status("one")
        phase[0] = "regress"
        with self.assertRaises(CaptureError):
            c.status("one")
        phase[0] = "start"
        c.finalize("one")
        with self.assertRaises(CaptureError):
            c.cancel("one")
        c2 = CaptureBridgeClient(transport)
        c2.start("bad", 0, 0, 100)
        with self.assertRaises(CaptureError):
            c2.cancel("bad")

    def test_history_is_bounded(self):
        def transport(batches):
            op = batches[0][0]["op"]
            cid = batches[0][0]["capture_id"]
            state = "cancelled" if op == "capture_cancel" else "recording"
            result = {"capture_id": cid, "state": state}
            if op == "capture_cancel":
                result["cleanup"] = {}
            return [{"ok": True, "results": [result]}]

        with patch.object(capture_support, "MAX_CAPTURE_HISTORY", 2):
            c = CaptureBridgeClient(transport)
            for index in range(4):
                cid = f"take{index}"
                c.start(cid, 0, index, 100)
                c.cancel(cid)
            self.assertLessEqual(len(c._records), 2)

    def test_nonfinite_poll_interval_and_nonpositive_clip_id_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ValueError):
                CaptureBridgeClient(lambda _: [], poll_interval_s=value)
        def transport(batches):
            op = batches[0][0]["op"]
            cid = batches[0][0]["capture_id"]
            if op == "capture_start":
                state = "recording"
            elif op == "capture_status":
                state = "ready"
            else:
                state = "finalized"
            result = {"capture_id": cid, "state": state}
            if op == "capture_finalize":
                result.update(path="t0/s0", clip_id=0, cleanup={})
            return [{"ok": True, "results": [result]}]
        c = CaptureBridgeClient(transport)
        c.start("zero", 0, 0, 100)
        with self.assertRaises(CaptureError):
            c.finalize("zero")


class TestCleanup(unittest.TestCase):
    def test_lifo_partial_failure_and_idempotence(self):
        order = []

        def bad():
            order.append("bad")
            raise RuntimeError("/secret/project")

        tx = CleanupTransaction().register(lambda: (order.append("a"), "/secret/path"), "a").register(bad, "b")
        first = tx.close()
        self.assertEqual(order, ["bad", "a"])
        self.assertFalse(first["actions"][0]["ok"])
        self.assertEqual(first["actions"][1]["receipt"][1], "redacted")
        self.assertEqual(tx.close(), first)

    def test_nested_path_receipt_keys_are_redacted(self):
        tx = CleanupTransaction().register(
            lambda: {"/secret/project": {"nested\\path": "/secret/file.wav"}}, "safe")
        receipt = tx.close()
        self.assertNotIn("/secret/project", str(receipt))
        self.assertNotIn("nested\\path", str(receipt))
        self.assertNotIn("/secret/file.wav", str(receipt))

    def test_exceptional_context_does_not_mask_primary(self):
        with self.assertRaises(KeyError):
            with CleanupTransaction().register(lambda: (_ for _ in ()).throw(RuntimeError("cleanup"))):
                raise KeyError("primary")


class TestAudio(unittest.TestCase):
    def test_metrics_silence_and_redaction(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Samples" / "Recorded"
            root.mkdir(parents=True)
            path = root / "take.wav"
            path.write_bytes(wav([0, 32767, -32768, 0]))
            card = analyze_captured_audio(path, [root])
            self.assertEqual(card["sample_rate"], 100)
            self.assertEqual(card["channels"], 1)
            self.assertGreater(card["peak"], 0.99)
            self.assertEqual(card["classification"], "signal")
            silent = root / "silent.wav"
            silent.write_bytes(wav([0, 0, 0]))
            self.assertEqual(analyze_captured_audio(silent, [root])["classification"], "silent")
            self.assertNotIn(str(path), str(card))

    def test_supported_integer_widths_and_multichannel_downmix(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixtures = {
                8: [0, 127, -128],
                16: [0, 32767, -32768],
                24: [0, 8388607, -8388608],
                32: [0, 2147483647, -2147483648],
            }
            for bits, values in fixtures.items():
                path = root / f"{bits}.wav"
                path.write_bytes(wav(values, bits=bits))
                card = analyze_captured_audio(path, [root])
                self.assertEqual(card["channels"], 1)
                self.assertLessEqual(card["peak"], 1.0)
            stereo = root / "stereo.wav"
            stereo.write_bytes(wav([32767, -32768, -32768, 32767], channels=2))
            self.assertEqual(analyze_captured_audio(stereo, [root])["channels"], 2)
            aiff = root / "take.aiff"
            aiff.write_bytes(aiff_stereo_16())
            card = analyze_captured_audio(aiff, [root])
            self.assertEqual((card["sample_rate"], card["channels"]), (44100, 2))
            for bits, values in ((8, [-128, 127]), (16, [-32768, 32767]),
                                 (24, [-8388608, 8388607]), (32, [-2147483648, 2147483647])):
                path = root / f"{bits}.aiff"
                path.write_bytes(aiff_pcm(values, bits=bits))
                self.assertEqual(analyze_captured_audio(path, [root])["sample_rate"], 44100)
            three = root / "three.wav"
            three.write_bytes(wav([32767, 0, -32768, -32768, 0, 32767], channels=3))
            self.assertEqual(analyze_captured_audio(three, [root])["channels"], 3)

    def test_frame_and_sample_decode_limits_and_aiff_frame_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "take.wav"
            path.write_bytes(wav([0, 0, 0]))
            with patch.object(capture_support, "MAX_AUDIO_FRAMES", 2):
                with self.assertRaises(AudioValidationError):
                    analyze_captured_audio(path, [root])
            with patch.object(capture_support, "MAX_DECODE_SAMPLES", 2):
                with self.assertRaises(AudioValidationError):
                    analyze_captured_audio(path, [root])
            mismatch = root / "mismatch.aiff"
            mismatch.write_bytes(aiff_pcm([0, 0], frames=3))
            with self.assertRaises(AudioValidationError):
                analyze_captured_audio(mismatch, [root])

    def test_bounded_read_and_recording_root_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "take.wav"
            valid = wav([0])
            path.write_bytes(valid)
            with self.assertRaises(AudioValidationError):
                analyze_captured_audio(path, [path])
            class OverflowReader:
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    return False
                def read(self, limit):
                    self.limit = limit
                    return valid + b"overflow"
            reader = OverflowReader()
            with patch.object(Path, "open", return_value=reader):
                with self.assertRaises(AudioValidationError):
                    analyze_captured_audio(path, [root], max_bytes=len(valid))
            self.assertEqual(reader.limit, len(valid) + 1)

    def test_pcm_data_view_does_not_copy_container_blob(self):
        blob = wav([0, 32767])
        _channels, _rate, _bits, data, _endian, _signed = capture_support._parse_wav(blob)
        self.assertIs(data.obj, blob)

    def test_unsupported_format_width_and_limits_are_redacted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bad = root / "bad.mp3"
            bad.write_bytes(b"not audio")
            with self.assertRaises(AudioValidationError) as ctx:
                analyze_captured_audio(bad, [root])
            self.assertNotIn(str(bad), str(ctx.exception))
            bad_wav = root / "bad.wav"
            bad_wav.write_bytes(wav([0], bits=16)[:24] + b"x")
            with self.assertRaises(AudioValidationError):
                analyze_captured_audio(bad_wav, [root])
            unsupported = root / "unsupported.wav"
            unsupported.write_bytes(wav([0], bits=12))
            with self.assertRaises(AudioValidationError):
                analyze_captured_audio(unsupported, [root])
            huge = root / "huge.wav"
            huge.write_bytes(wav([0]) + b"x" * 100)
            with self.assertRaises(AudioValidationError):
                analyze_captured_audio(huge, [root], max_bytes=10)

    def test_containment_traversal_symlink_and_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            root.mkdir()
            path = root / "take.wav"
            path.write_bytes(wav([0]))
            with self.assertRaises(AudioValidationError):
                analyze_captured_audio(root / ".." / "root" / "take.wav", [root])
            outside = Path(td) / "outside.wav"
            outside.write_bytes(wav([0]))
            link = root / "link.wav"
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("symlinks unavailable")
            with self.assertRaises(AudioValidationError):
                analyze_captured_audio(link, [root])
            for unc in (r"\\server\share\take.wav", "//server/share/take.wav"):
                with self.assertRaises(AudioValidationError):
                    analyze_captured_audio(unc, [root])
            path.write_bytes(b"RIFFbad")
            with self.assertRaises(AudioValidationError):
                analyze_captured_audio(path, [root])


if __name__ == "__main__":
    unittest.main()
