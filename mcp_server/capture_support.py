"""Lower-level, fail-closed support for the Ears capture lifecycle.

This module deliberately owns no MCP registration and no Producer Pal policy.  It
contains the small deterministic pieces that Task 5 can compose: time-signature
duration, an injected SIDEKICK BRIDGE transport, cleanup receipts, and local PCM
inspection.  Error messages are intentionally path-free because this module's
results can cross an MCP boundary later.
"""
from __future__ import annotations

import math
import os
import re
import struct
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator


STATES = frozenset(("recording", "ready", "finalized", "cancelled", "failed"))
SUPPORTED_DENOMINATORS = frozenset((1, 2, 4, 8, 16, 32))
MAX_BARS = 1024
MAX_TEMPO = 1000.0
MAX_DURATION_SECONDS = 24.0 * 60.0 * 60.0
MAX_AUDIO_BYTES = 512 * 1024 * 1024
MAX_AUDIO_FRAMES = 20_000_000
MAX_DECODE_SAMPLES = 60_000_000
MAX_CAPTURE_HISTORY = 64
_AUDIO_SUFFIXES = frozenset((".wav", ".aif", ".aiff"))


class CaptureError(RuntimeError):
    """A path-free capture contract failure."""


class CaptureTimeout(TimeoutError):
    """Timeout whose cleanup attempt remains inspectable by the caller."""

    def __init__(self, message: str, cleanup_receipt: Any = None):
        super().__init__(message)
        self.cleanup_receipt = cleanup_receipt


def _number(name: str, value: Any, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    value_f = float(value)
    if not math.isfinite(value_f) or value_f <= 0:
        raise ValueError(f"{name} must be finite and positive")
    if integer and value_f != int(value_f):
        raise ValueError(f"{name} must be an integer")
    return int(value_f) if integer else value_f


def capture_duration_seconds(
    bars: Any, numerator: Any, denominator: Any, tempo: Any
) -> float:
    """Return exact capture seconds for a validated time signature.

    A bar has ``numerator`` beats and each beat is a quarter-note fraction
    ``4 / denominator``.  Bounds are deliberately conservative so a malformed
    caller cannot schedule an unexpectedly large Live task.
    """
    b = _number("bars", bars, integer=True)
    n = _number("numerator", numerator, integer=True)
    d = _number("denominator", denominator, integer=True)
    t = _number("tempo", tempo)
    if b > MAX_BARS or n > 64 or d not in SUPPORTED_DENOMINATORS or t > MAX_TEMPO:
        raise ValueError("capture timing is unreasonable or uses an unsupported denominator")
    seconds = b * n * (4.0 / d) * (60.0 / t)
    if not math.isfinite(seconds) or seconds <= 0 or seconds > MAX_DURATION_SECONDS:
        raise ValueError("capture duration is unreasonable")
    return seconds


# Common names make the pure helper easy to discover without creating another owner.
calculate_capture_duration = capture_duration_seconds
capture_duration = capture_duration_seconds
capture_seconds = capture_duration_seconds


def _safe_error(exc: BaseException) -> str:
    # Do not echo paths or arbitrary transport payloads in a bridge-facing error.
    # Even a one-component filename can identify a project, so use a fixed
    # message for every non-contract exception.
    if isinstance(exc, CaptureError):
        text = str(exc)
        if text and all(ch.isalnum() or ch in " _-" for ch in text):
            return text[:120]
    return "capture operation failed"


def _safe_receipt(value: Any) -> Any:
    """Keep cleanup receipts useful while preventing path-bearing values."""
    if isinstance(value, str):
        if "/" in value or "\\" in value or "://" in value:
            return "redacted"
        return value[:240]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            text = str(key)
            if "/" in text or "\\" in text or "://" in text:
                text = "redacted_key"
            else:
                text = text[:80]
            out[text] = _safe_receipt(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_safe_receipt(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "redacted"


def _reply_result(reply: Any) -> dict[str, Any]:
    """Validate the existing envelope_bridge.send_cmds reply shape."""
    if not isinstance(reply, list) or len(reply) != 1 or not isinstance(reply[0], dict):
        raise CaptureError("malformed bridge response")
    envelope = reply[0]
    if envelope.get("ok") is not True:
        raise CaptureError("bridge operation failed")
    results = envelope.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise CaptureError("malformed bridge response")
    result = results[0]
    if "error" in result:
        raise CaptureError("bridge operation failed")
    return result


class CaptureBridgeClient:
    """Injected-transport client for the four SIDEKICK BRIDGE capture ops."""

    def __init__(
        self,
        transport: Callable[[list[list[dict[str, Any]]]], Any],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval_s: float = 0.05,
        timeout_s: float = 30.0,
    ):
        if not callable(transport):
            raise TypeError("transport must be callable")
        if (isinstance(poll_interval_s, bool) or not isinstance(poll_interval_s, (int, float))
                or not math.isfinite(float(poll_interval_s)) or poll_interval_s <= 0):
            raise ValueError("poll_interval_s must be positive")
        if isinstance(timeout_s, bool) or timeout_s <= 0 or not math.isfinite(float(timeout_s)):
            raise ValueError("timeout_s must be finite and positive")
        self.transport = transport
        self.clock = clock
        self.sleep = sleep
        self.poll_interval_s = float(poll_interval_s)
        self.timeout_s = float(timeout_s)
        self._capture: dict[str, Any] | None = None
        self._records: dict[str, dict[str, Any]] = {}
        self._record_order: list[str] = []

    def _remember(self, capture_id: str, record: dict[str, Any]) -> None:
        if capture_id not in self._records:
            self._record_order.append(capture_id)
        self._records[capture_id] = record
        current_id = self._capture.get("capture_id") if self._capture else None
        while len(self._record_order) > MAX_CAPTURE_HISTORY:
            victim = self._record_order[0]
            if victim == current_id:
                self._record_order = self._record_order[1:] + [victim]
                continue
            self._record_order.pop(0)
            self._records.pop(victim, None)

    @staticmethod
    def _capture_id(value: Any) -> str:
        if (not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", value)):
            raise ValueError("capture_id must be a path-free opaque token")
        return value

    @staticmethod
    def _target(track: Any, scene: Any) -> tuple[int, int]:
        def index(name: str, value: Any) -> int:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
            return value
        t = index("target_track", track)
        s = index("target_scene", scene)
        if t < 0 or s < 0:  # defensive, index() already establishes this
            raise ValueError("target track and scene must be non-negative")
        return t, s

    @staticmethod
    def _duration_ms(value: Any) -> int:
        d = _number("duration_ms", value, integer=True)
        if d > int(MAX_DURATION_SECONDS * 1000):
            raise ValueError("duration_ms is unreasonable")
        return int(d)

    def _call(self, op: dict[str, Any]) -> dict[str, Any]:
        try:
            return _reply_result(self.transport([[op]]))
        except CaptureError as exc:
            raise CaptureError(_safe_error(exc)) from None
        except Exception as exc:  # transport details never cross the boundary
            raise CaptureError(_safe_error(exc)) from None

    @staticmethod
    def _clean_cleanup(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise CaptureError("malformed cleanup receipt")
        out: dict[str, Any] = {}
        allowed_keys = {"task_cancelled", "stopped", "deleted", "arm_restored",
                        "task_cancel_error", "stop_error", "delete_error",
                        "arm_restore_error"}
        for key, item in value.items():
            if key not in allowed_keys:
                raise CaptureError("malformed cleanup receipt")
            if isinstance(item, bool):
                out[key] = item
            elif isinstance(item, str) and all(ch.isalnum() or ch in " _-" for ch in item):
                out[key] = item[:120]
        return out

    def _clean_result(self, result: dict[str, Any], capture_id: str, *,
                      operation: str) -> dict[str, Any]:
        if result.get("capture_id") != capture_id:
            raise CaptureError("bridge capture identity mismatch")
        state = result.get("state")
        if state not in STATES:
            raise CaptureError("malformed bridge state")
        expected_fields = {
            "status": {"capture_id", "state"},
            "finalize": {"capture_id", "state", "path", "clip_id", "cleanup"},
            "cancel": {"capture_id", "state", "cleanup"},
        }.get(operation, set())
        if set(result) - expected_fields or not expected_fields.issubset(result):
            raise CaptureError("unexpected bridge fields")
        prior_record = self._records.get(capture_id)
        if prior_record is None:
            if operation != "cancel" or state not in {"cancelled", "failed"}:
                raise CaptureError("unknown local capture")
            return {"capture_id": capture_id, "state": state,
                    "cleanup": self._clean_cleanup(result.get("cleanup", {}))}
        prior = prior_record.get("state") if prior_record else None
        allowed = {
            "status": {"recording": {"recording", "ready", "failed", "cancelled"},
                        "ready": {"ready", "failed", "cancelled"},
                        "finalized": {"finalized"}, "cancelled": {"cancelled"}, "failed": {"failed"}},
            "finalize": {"ready": {"finalized"}},
            "cancel": {"recording": {"cancelled", "failed"}, "ready": {"cancelled", "failed"},
                       "failed": {"failed", "cancelled"}, "cancelled": {"cancelled"}},
        }
        if prior and state not in allowed.get(operation, {}).get(prior, set()):
            raise CaptureError("illegal bridge state transition")
        if operation == "finalize":
            path = result.get("path")
            clip_id = result.get("clip_id")
            if not isinstance(path, str) or not re.fullmatch(r"t[0-9]+/s[0-9]+", path):
                raise CaptureError("malformed capture path")
            if isinstance(clip_id, bool) or not isinstance(clip_id, int) or clip_id <= 0:
                raise CaptureError("malformed capture clip")
            return {"capture_id": capture_id, "path": path, "clip_id": clip_id,
                    "state": state, "cleanup": self._clean_cleanup(result.get("cleanup", {}))}
        out = {"capture_id": capture_id, "state": state}
        if operation == "cancel":
            out["cleanup"] = self._clean_cleanup(result.get("cleanup", {}))
        return out

    def start(self, capture_id: str, target_track: int, target_scene: int,
              duration_ms: int, **kwargs: Any) -> dict[str, Any]:
        if kwargs:
            forbidden = next(iter(kwargs))
            raise ValueError(f"unsupported capture start field: {forbidden}")
        cid = self._capture_id(capture_id)
        track, scene = self._target(target_track, target_scene)
        duration = self._duration_ms(duration_ms)
        requested = {"capture_id": cid, "track_index": track,
                     "scene_index": scene, "duration_ms": duration}
        if self._capture is not None and self._capture.get("state") == "recording":
            prior = self._capture
            if all(prior.get(k) == v for k, v in requested.items()):
                return dict(prior)
            raise CaptureError("another capture is active")
        if cid in self._records:
            prior = self._records[cid]
            if all(prior.get(k) == v for k, v in requested.items()):
                return dict(prior)
            raise CaptureError("capture_id or target was already used")
        result = self._call({"op": "capture_start", **requested})
        if (set(result) - {"capture_id", "state"} or
                result.get("capture_id") != cid or result.get("state") != "recording"):
            raise CaptureError("bridge did not enter recording state")
        self._capture = {**requested, "capture_id": cid, "state": "recording"}
        self._remember(cid, self._capture)
        return dict(self._capture)

    def status(self, capture_id: str) -> dict[str, Any]:
        cid = self._capture_id(capture_id)
        result = self._call({"op": "capture_status", "capture_id": cid})
        result = self._clean_result(result, cid, operation="status")
        if self._capture and self._capture.get("capture_id") == cid:
            self._capture.update(result)
        if cid in self._records:
            self._records[cid].update(result)
        return result

    def wait_ready(self, capture_id: str, timeout_s: float | None = None) -> dict[str, Any]:
        cid = self._capture_id(capture_id)
        timeout = self.timeout_s if timeout_s is None else float(_number("timeout_s", timeout_s))
        deadline = self.clock() + timeout
        while True:
            result = self.status(cid)
            state = result["state"]
            if state == "ready":
                return result
            if state in ("failed", "cancelled", "finalized"):
                raise CaptureError(f"capture is {state}")
            if self.clock() >= deadline:
                try:
                    receipt = self.cancel(cid)
                except Exception as exc:  # preserve timeout as the primary failure
                    receipt = {"state": "cleanup_failed", "error": _safe_error(exc)}
                raise CaptureTimeout("capture readiness timed out", receipt) from None
            self.sleep(min(self.poll_interval_s, max(0.0, deadline - self.clock())))

    def finalize(self, capture_id: str) -> dict[str, Any]:
        cid = self._capture_id(capture_id)
        current = self.status(cid)  # first observe: never claim while timer is pending
        if current["state"] != "ready":
            raise CaptureError("capture is not ready")
        result = self._clean_result(
            self._call({"op": "capture_finalize", "capture_id": cid}), cid,
            operation="finalize")
        if self._capture is not None and self._capture.get("capture_id") == cid:
            self._capture.update(result)
        self._remember(cid, dict(self._records.get(cid, {}), **result))
        return result

    def cancel(self, capture_id: str) -> dict[str, Any]:
        cid = self._capture_id(capture_id)
        result = self._clean_result(
            self._call({"op": "capture_cancel", "capture_id": cid}), cid,
            operation="cancel")
        if self._capture is not None and self._capture.get("capture_id") == cid:
            self._capture.update(result)
        self._remember(cid, dict(self._records.get(cid, {}), **result))
        return result


BridgeCaptureClient = CaptureBridgeClient
CaptureClient = CaptureBridgeClient


class CleanupTransaction:
    """Small idempotent LIFO cleanup stack with non-masking receipts."""

    def __init__(self):
        self._actions: list[tuple[str, Callable[[], Any]]] = []
        self._closed = False
        self._receipt: dict[str, Any] | None = None

    def register(self, action: Callable[[], Any], label: str | None = None) -> "CleanupTransaction":
        if self._closed:
            raise RuntimeError("cleanup transaction is closed")
        if not callable(action):
            raise TypeError("cleanup action must be callable")
        candidate = str(label) if label is not None else ""
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", candidate):
            candidate = f"action_{len(self._actions) + 1}"
        self._actions.append((candidate, action))
        return self

    add = register

    def close(self) -> dict[str, Any]:
        if self._closed:
            return dict(self._receipt or {"closed": True, "actions": []})
        receipts = []
        for label, action in reversed(self._actions):
            try:
                value = action()
                receipts.append({"label": label, "ok": True, "receipt": _safe_receipt(value)})
            except Exception as exc:  # each cleanup is best effort
                receipts.append({"label": label, "ok": False, "error": _safe_error(exc)})
        self._closed = True
        self._receipt = {"closed": True, "actions": receipts}
        return dict(self._receipt)

    def __enter__(self) -> "CleanupTransaction":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False


CleanupStack = CleanupTransaction


class AudioValidationError(ValueError):
    pass


def _reject_audio_path(path: Any, roots: Iterable[Any]) -> Path:
    if not isinstance(path, (str, os.PathLike)):
        raise AudioValidationError("audio file is invalid")
    raw = os.fspath(path)
    if not raw or raw.startswith(("\\\\", "//")) or "://" in raw:
        raise AudioValidationError("audio file is invalid")
    candidate = Path(raw)
    if ".." in candidate.parts:
        raise AudioValidationError("audio file path traversal is not allowed")
    if candidate.suffix.lower() not in _AUDIO_SUFFIXES:
        raise AudioValidationError("unsupported audio format")
    try:
        resolved = candidate.resolve(strict=True)
        root_iter = [roots] if isinstance(roots, (str, os.PathLike)) else roots
        root_paths = [Path(r).resolve(strict=True) for r in root_iter]
    except (OSError, RuntimeError, ValueError):
        raise AudioValidationError("audio file is unavailable") from None
    if not root_paths:
        raise AudioValidationError("no recording root supplied")
    if any(not root.is_dir() for root in root_paths):
        raise AudioValidationError("recording root is not a directory")
    if not any(resolved == root or root in resolved.parents for root in root_paths):
        raise AudioValidationError("audio file is outside recording roots")
    return resolved


def _read_chunks(blob: bytes, offset: int, endian: str, fourcc: bytes) -> tuple[memoryview, int]:
    pos = offset
    while pos + 8 <= len(blob):
        tag = blob[pos:pos + 4]
        size = struct.unpack_from(endian + "I", blob, pos + 4)[0]
        pos += 8
        end = pos + size
        if end > len(blob):
            raise AudioValidationError("corrupt audio header")
        if tag == fourcc:
            return memoryview(blob)[pos:end], end + (size & 1)
        pos = end + (size & 1)
    raise AudioValidationError("audio data is missing")


def _parse_wav(blob: bytes) -> tuple[int, int, int, memoryview, str, bool]:
    if len(blob) < 12 or blob[:4] != b"RIFF" or blob[8:12] != b"WAVE":
        raise AudioValidationError("corrupt WAV header")
    if struct.unpack_from("<I", blob, 4)[0] + 8 != len(blob):
        raise AudioValidationError("corrupt WAV container size")
    fmt, _ = _read_chunks(blob, 12, "<", b"fmt ")
    data, _ = _read_chunks(blob, 12, "<", b"data")
    if len(fmt) < 16:
        raise AudioValidationError("corrupt WAV format")
    code, channels, rate, byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", fmt)
    if code != 1:
        raise AudioValidationError("only PCM audio is supported")
    if bits not in (8, 16, 24, 32) or block_align != channels * ((bits + 7) // 8) or byte_rate != rate * block_align:
        raise AudioValidationError("unsupported PCM dimensions")
    return channels, rate, bits, data, "<", False


def _parse_aiff(blob: bytes) -> tuple[int, int, int, memoryview, str, bool]:
    if len(blob) < 12 or blob[:4] != b"FORM" or blob[8:12] not in (b"AIFF", b"AIFC"):
        raise AudioValidationError("corrupt AIFF header")
    if struct.unpack_from(">I", blob, 4)[0] + 8 != len(blob):
        raise AudioValidationError("corrupt AIFF container size")
    comm, _ = _read_chunks(blob, 12, ">", b"COMM")
    ssnd, _ = _read_chunks(blob, 12, ">", b"SSND")
    if len(comm) < 18 or blob[8:12] == b"AIFC":
        raise AudioValidationError("only uncompressed AIFF PCM is supported")
    channels, frames, bits = struct.unpack_from(">HIH", comm)
    # AIFF stores an 80-bit extended float sample rate.  Decode the common
    # integer rates without relying on platform audio libraries.
    exponent = int.from_bytes(comm[8:10], "big") & 0x7FFF
    mantissa = int.from_bytes(comm[10:18], "big")
    rate = int(round((mantissa / float(1 << 63)) * (2.0 ** (exponent - 16383))))
    if len(ssnd) < 8:
        raise AudioValidationError("corrupt AIFF data")
    if channels <= 0 or bits not in (8, 16, 24, 32):
        raise AudioValidationError("unsupported PCM dimensions")
    offset = struct.unpack_from(">I", ssnd, 0)[0]
    if offset > len(ssnd) - 8:
        raise AudioValidationError("corrupt AIFF data")
    payload = ssnd[8 + offset:]
    width = (bits + 7) // 8
    if width <= 0 or len(payload) % (channels * width) or frames != len(payload) // (channels * width):
        raise AudioValidationError("AIFF frame count does not match audio data")
    return channels, rate, bits, payload, ">", True


def _decode_samples(channels: int, rate: int, bits: int, data: memoryview, endian: str,
                    signed_8: bool = False) -> Iterator[float]:
    if channels <= 0 or channels > 64 or rate <= 0 or rate > 384000 or bits not in (8, 16, 24, 32):
        raise AudioValidationError("unsupported PCM dimensions")
    width = (bits + 7) // 8
    block = channels * width
    if not data or len(data) % block:
        raise AudioValidationError("empty or truncated audio data")
    frames = len(data) // block
    if frames > MAX_AUDIO_FRAMES or frames * channels > MAX_DECODE_SAMPLES:
        raise AudioValidationError("audio decode is too large")
    denom = float(1 << (bits - 1))
    for pos in range(0, len(data), block):
        vals = []
        for ch in range(channels):
            raw = data[pos + ch * width:pos + (ch + 1) * width]
            if bits == 8:
                value = int.from_bytes(raw, "big" if endian == ">" else "little", signed=signed_8)
                if not signed_8:
                    value -= 128
            else:
                value = int.from_bytes(raw, "little" if endian == "<" else "big", signed=True)
            vals.append(value / denom)
        yield sum(vals) / channels


def analyze_captured_audio(path: Any, recording_roots: Iterable[Any], *,
                           max_bytes: int = MAX_AUDIO_BYTES) -> dict[str, Any]:
    """Validate and summarize a local WAV/AIFF without exposing its path."""
    resolved = _reject_audio_path(path, recording_roots)
    try:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise AudioValidationError("audio file size limit is invalid")
        effective_limit = min(max_bytes, MAX_AUDIO_BYTES)
        with resolved.open("rb") as source:
            blob = source.read(effective_limit + 1)
        if not blob or len(blob) > effective_limit:
            raise AudioValidationError("audio file size is invalid")
        if resolved.suffix.lower() == ".wav":
            channels, rate, bits, data, endian, signed_8 = _parse_wav(blob)
        else:
            channels, rate, bits, data, endian, signed_8 = _parse_aiff(blob)
        samples = _decode_samples(channels, rate, bits, data, endian, signed_8)
    except AudioValidationError:
        raise
    except (OSError, struct.error, OverflowError, ValueError):
        raise AudioValidationError("audio file could not be decoded") from None
    sample_count = 0
    peak = 0.0
    sum_squares = 0.0
    non_silent_count = 0
    for sample in samples:
        sample_count += 1
        magnitude = abs(sample)
        peak = max(peak, magnitude)
        sum_squares += sample * sample
        if magnitude > 1e-4:
            non_silent_count += 1
    if not sample_count:
        raise AudioValidationError("audio file contains no frames")
    rms = math.sqrt(sum_squares / sample_count)
    non_silent = non_silent_count / sample_count
    return {
        "duration_s": sample_count / float(rate),
        "sample_rate": rate,
        "channels": channels,
        "peak": round(peak, 9),
        "rms": round(rms, 9),
        "non_silent_fraction": round(non_silent, 9),
        "classification": "signal" if non_silent > 0 else "silent",
    }


validate_captured_audio = analyze_captured_audio
inspect_captured_audio = analyze_captured_audio
analyze_audio_file = analyze_captured_audio
validate_audio_file = analyze_captured_audio


def run_selftest() -> None:
    assert capture_duration_seconds(1, 4, 4, 120) == 2.0
    assert capture_duration_seconds(1, 3, 4, 120) == 1.5
    assert capture_duration_seconds(1, 5, 4, 120) == 2.5
    for bad in ((True, 4, 4, 120), (1.5, 4, 4, 120), (1, 4, 3, 120),
                (1, 4, 4, 0), (1, 4, 4, float("nan"))):
        try:
            capture_duration_seconds(*bad)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid timing accepted")
    calls = []
    def tx(batches):
        calls.append(batches)
        op = batches[0][0]["op"]
        state = "recording" if op == "capture_start" else ("ready" if op == "capture_status" else "finalized")
        return [{"ok": True, "results": [{"state": state, "capture_id": "x"}]}]
    c = CaptureBridgeClient(tx, clock=lambda: 0.0, sleep=lambda _x: None)
    c.start("x", 1, 2, 100)
    assert c.wait_ready("x")["state"] == "ready"
    assert CleanupTransaction().close()["closed"] is True
