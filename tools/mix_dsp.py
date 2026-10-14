"""Deterministic local FFmpeg mix evidence — measurements only, never a taste score.

Public API:
  probe_audio(path) -> dict
  analyze_audio(path, label) -> dict
  analyze_mix(master_path, group_paths) -> dict
  main() — --selftest and JSON CLI

Evidence for analyze_mix_pass; no mixer proposals, network, Live, or store writes.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# Ensure repo root is importable when invoked as tools/mix_dsp.py
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp_server.mix_contracts import (  # noqa: E402
    ALLOWED_AUDIO_EXTENSIONS,
    CONTRACT_VERSION,
    validate_groups,
    validate_local_audio_path,
)

TOOL_NAME = "mix_dsp"
TOOL_VERSION = 1
DURATION_DRIFT_LIMIT_S = 0.050
DURATION_DRIFT_EPSILON_S = 1e-9

BANDS: tuple[tuple[str, int, int], ...] = (
    ("20_120", 20, 120),
    ("120_500", 120, 500),
    ("500_5000", 500, 5000),
    ("5000_20000", 5000, 20000),
)

_ENV_C = {"LC_ALL": "C", "LANG": "C"}


class MixDspError(ValueError):
    """Actionable measurement / validation failure (fail closed)."""


def _which(name: str) -> str | None:
    return shutil.which(name)


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    kwargs.setdefault("timeout", 300)
    env = dict(os.environ)
    env.update(_ENV_C)
    try:
        return subprocess.run(
            cmd,
            check=False,
            text=True,
            capture_output=True,
            env=env,
            **kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        raise MixDspError("subprocess timed out: %s" % exc) from exc


def _ffmpeg() -> str:
    path = _which("ffmpeg")
    if not path:
        raise MixDspError("ffmpeg not found on PATH")
    return path


def _ffprobe() -> str:
    path = _which("ffprobe")
    if not path:
        raise MixDspError("ffprobe not found on PATH")
    return path


def _version_first_line(cmd: list[str]) -> str:
    proc = _run(cmd)
    line = (proc.stdout or proc.stderr or "").splitlines()
    return line[0].strip() if line else ""


def _ffmpeg_provenance() -> dict[str, str]:
    ff = _ffmpeg()
    fp = _ffprobe()
    return {
        "tool": TOOL_NAME,
        "tool_version": str(TOOL_VERSION),
        "contract_version": str(CONTRACT_VERSION),
        "ffmpeg_path": ff,
        "ffprobe_path": fp,
        "ffmpeg_version": _version_first_line([ff, "-version"]),
        "ffprobe_version": _version_first_line([fp, "-version"]),
    }


def _require_regular_file(path: str) -> Path:
    p = Path(path)
    if not p.exists():
        raise MixDspError("audio file does not exist: %s" % path)
    if not p.is_file():
        raise MixDspError("audio path is not a regular file: %s" % path)
    return p


def _parse_finite(value: Any, *, file: str, measurement: str) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError("bool")
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise MixDspError(
            "non-finite or unparsable measurement %r for file %s (%s)"
            % (measurement, file, value)
        ) from exc
    if math.isnan(num) or math.isinf(num):
        raise MixDspError(
            "non-finite measurement %r for file %s (%s)" % (measurement, file, value)
        )
    return num


def _fmt_num(value: float) -> str:
    """Stable decimal for evidence_refs (trim trailing zeros, keep precision)."""
    if abs(value) >= 1000 or (abs(value) > 0 and abs(value) < 1e-4):
        text = "%.6g" % value
    else:
        text = "%.6f" % value
        text = text.rstrip("0").rstrip(".") if "." in text else text
    return text


# ---------------------------------------------------------------------------
# Parsers (official-shaped strings; unit-tested in _selftest)
# ---------------------------------------------------------------------------

_EBUR_I = re.compile(r"^\s*I:\s*([-+]?\d+(?:\.\d+)?)\s*LUFS\s*$", re.M)
_EBUR_LRA = re.compile(r"^\s*LRA:\s*([-+]?\d+(?:\.\d+)?)\s*LU\s*$", re.M)
_EBUR_TRUE_PEAK = re.compile(
    r"True peak:\s*\n\s*Peak:\s*([-+]?\d+(?:\.\d+)?)\s*dBFS", re.M
)
_EBUR_SAMPLE_PEAK = re.compile(
    r"Sample peak:\s*\n\s*Peak:\s*([-+]?\d+(?:\.\d+)?)\s*dBFS", re.M
)
# Fallback: last FTPK / TPK from continuous lines when Summary lacks peaks
_EBUR_FTPK_LINE = re.compile(
    r"FTPK:\s*([-+]?\d+(?:\.\d+)?)\s+([-+]?\d+(?:\.\d+)?)\s*dBFS"
)
_EBUR_TPK_LINE = re.compile(
    r"TPK:\s*([-+]?\d+(?:\.\d+)?)\s+([-+]?\d+(?:\.\d+)?)\s*dBFS"
)


def parse_ebur128(stderr: str, *, file: str) -> dict[str, Any]:
    """Parse ebur128 Summary block from ffmpeg stderr."""
    if "Summary:" not in stderr:
        raise MixDspError(
            "ebur128 summary missing for file %s (measurement integrated_lufs)" % file
        )
    summary = stderr.rsplit("Summary:", 1)[1]
    m_i = _EBUR_I.search(summary)
    m_lra = _EBUR_LRA.search(summary)
    if not m_i:
        raise MixDspError(
            "missing measurement integrated_lufs for file %s" % file
        )
    if not m_lra:
        raise MixDspError("missing measurement loudness_range_lu for file %s" % file)
    out: dict[str, Any] = {
        "integrated_lufs": _parse_finite(m_i.group(1), file=file, measurement="integrated_lufs"),
        "loudness_range_lu": _parse_finite(
            m_lra.group(1), file=file, measurement="loudness_range_lu"
        ),
    }
    m_tp = _EBUR_TRUE_PEAK.search(summary)
    if m_tp:
        out["true_peak_dbfs"] = _parse_finite(
            m_tp.group(1), file=file, measurement="true_peak_dbfs"
        )
    else:
        # continuous-line fallback: max of last TPK pair
        matches = list(_EBUR_TPK_LINE.finditer(stderr))
        if matches:
            a = float(matches[-1].group(1))
            b = float(matches[-1].group(2))
            out["true_peak_dbfs"] = _parse_finite(
                max(a, b), file=file, measurement="true_peak_dbfs"
            )
    m_sp = _EBUR_SAMPLE_PEAK.search(summary)
    if m_sp:
        out["sample_peak_dbfs"] = _parse_finite(
            m_sp.group(1), file=file, measurement="sample_peak_dbfs"
        )
    else:
        matches = list(_EBUR_FTPK_LINE.finditer(stderr))
        if matches:
            a = float(matches[-1].group(1))
            b = float(matches[-1].group(2))
            out["sample_peak_dbfs"] = _parse_finite(
                max(a, b), file=file, measurement="sample_peak_dbfs"
            )
    return out


_ASTATS_OVERALL = re.compile(r"\]\s*Overall\s*$", re.M)
_ASTATS_KV = re.compile(
    r"\]\s*(Peak level dB|RMS level dB|Crest factor):\s*([-+]?\d+(?:\.\d+)?|-?inf)\s*$",
    re.M,
)
_ASTATS_CHANNEL = re.compile(r"\]\s*Channel:\s*(\d+)\s*$", re.M)


def parse_astats(stderr: str, *, file: str) -> dict[str, Any]:
    """Parse astats per-channel + Overall RMS/peak/crest from ffmpeg stderr."""
    channels: dict[str, dict[str, float]] = {}
    current: dict[str, float] | None = None
    current_key: str | None = None
    overall: dict[str, float] = {}

    for line in stderr.splitlines():
        ch = _ASTATS_CHANNEL.search(line)
        if ch:
            current_key = "ch%s" % ch.group(1)
            current = {}
            channels[current_key] = current
            continue
        if _ASTATS_OVERALL.search(line):
            current_key = "overall"
            current = overall
            continue
        kv = _ASTATS_KV.search(line)
        if not kv or current is None:
            continue
        key_raw, raw_val = kv.group(1), kv.group(2)
        if raw_val in ("inf", "-inf", "+inf"):
            # near-silent / empty: treat as missing required overall later
            continue
        name = {
            "Peak level dB": "peak_db",
            "RMS level dB": "rms_db",
            "Crest factor": "crest_factor",
        }[key_raw]
        current[name] = _parse_finite(raw_val, file=file, measurement=name)

    if "rms_db" not in overall:
        raise MixDspError("missing measurement rms_db for file %s" % file)
    if "peak_db" not in overall:
        raise MixDspError("missing measurement peak_db for file %s" % file)
    # Crest factor is omitted from Overall by some FFmpeg builds; derive if needed
    if "crest_factor" not in overall:
        # Prefer channel crest if uniform, else leave as derived from peak/rms linear
        crest_vals = [
            ch["crest_factor"] for ch in channels.values() if "crest_factor" in ch
        ]
        if crest_vals:
            overall["crest_factor"] = sum(crest_vals) / len(crest_vals)
        else:
            # crest ≈ 10^((peak_db - rms_db)/20)
            peak = overall["peak_db"]
            rms = overall["rms_db"]
            overall["crest_factor"] = _parse_finite(
                10 ** ((peak - rms) / 20.0),
                file=file,
                measurement="crest_factor",
            )

    out: dict[str, Any] = {
        "rms_db": overall["rms_db"],
        "peak_db": overall["peak_db"],
        "crest_factor": overall["crest_factor"],
    }
    if channels:
        out["per_channel"] = {
            k: dict(sorted(v.items())) for k, v in sorted(channels.items())
        }
    return out


# FFmpeg 8 aphasemeter emits per-frame lavfi.aphasemeter.phase via ametadata;
# there is no integrated mean — average all finite frame values.
_APHASE_METADATA = re.compile(
    r"lavfi\.aphasemeter\.phase\s*=\s*([-+]?\d+(?:\.\d+)?)",
    re.I,
)


def parse_aphasemeter(text: str, *, file: str, channels: int) -> dict[str, Any]:
    """Parse stereo phase/correlation; mono → unavailable with reason."""
    if channels < 2:
        return {
            "correlation": None,
            "available": False,
            "reason": "mono input; stereo correlation requires at least 2 channels",
        }
    values: list[float] = []
    for match in _APHASE_METADATA.finditer(text):
        values.append(
            _parse_finite(match.group(1), file=file, measurement="stereo_correlation")
        )
    if values:
        mean = sum(values) / float(len(values))
        return {
            "correlation": _parse_finite(
                mean, file=file, measurement="stereo_correlation"
            ),
            "available": True,
            "reason": None,
            "frames_averaged": len(values),
        }
    # Fail soft for stereo when filter emits no phase metadata — evidence, not crash
    return {
        "correlation": None,
        "available": False,
        "reason": (
            "aphasemeter produced no parseable lavfi.aphasemeter.phase frames for "
            "file %s; use aphasemeter=video=0 + ametadata=print" % file
        ),
    }


def parse_band_rms(stderr: str, *, file: str, band: str) -> float:
    """Extract Overall RMS level dB from a band-limited astats run."""
    # Reuse overall parser path
    stats = parse_astats(stderr, file="%s[%s]" % (file, band))
    return float(stats["rms_db"])


# ---------------------------------------------------------------------------
# FFmpeg invocations
# ---------------------------------------------------------------------------


def probe_audio(path: str) -> dict[str, Any]:
    """ffprobe JSON → validated audio stream metadata."""
    validated = validate_local_audio_path(path)
    p = _require_regular_file(validated)
    fp = _ffprobe()
    proc = _run(
        [
            fp,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(p),
        ]
    )
    if proc.returncode != 0:
        raise MixDspError(
            "ffprobe failed for file %s: %s"
            % (validated, (proc.stderr or proc.stdout or "").strip() or "unknown error")
        )
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MixDspError("ffprobe JSON unparsable for file %s" % validated) from exc

    streams = payload.get("streams") or []
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if len(audio_streams) != 1:
        raise MixDspError(
            "file %s must contain exactly one audio stream (found %d)"
            % (validated, len(audio_streams))
        )
    stream = audio_streams[0]
    fmt = payload.get("format") or {}

    sample_rate = _parse_finite(
        stream.get("sample_rate"), file=validated, measurement="sample_rate"
    )
    channels = _parse_finite(
        stream.get("channels"), file=validated, measurement="channels"
    )
    if channels != int(channels) or int(channels) < 1:
        raise MixDspError(
            "non-finite or invalid measurement channels for file %s (%s)"
            % (validated, stream.get("channels"))
        )
    channels_i = int(channels)

    duration_raw = stream.get("duration")
    if duration_raw is None:
        duration_raw = fmt.get("duration")
    duration = _parse_finite(duration_raw, file=validated, measurement="duration")
    if duration <= 0:
        raise MixDspError(
            "duration must be positive for file %s (got %s)" % (validated, duration)
        )
    if sample_rate <= 0:
        raise MixDspError(
            "sample_rate must be positive for file %s (got %s)" % (validated, sample_rate)
        )

    ext = validated.lower()
    if not any(ext.endswith(s) for s in ALLOWED_AUDIO_EXTENSIONS):
        raise MixDspError("unsupported extension for file %s" % validated)

    return {
        "path": str(p.resolve()),
        "format_name": fmt.get("format_name"),
        "codec_name": stream.get("codec_name"),
        "sample_rate": int(sample_rate),
        "channels": channels_i,
        "duration": duration,
        "bit_rate": fmt.get("bit_rate"),
    }


def _run_filter(path: str, af: str) -> str:
    ff = _ffmpeg()
    proc = _run(
        [
            ff,
            "-hide_banner",
            "-nostats",
            "-i",
            path,
            "-af",
            af,
            "-f",
            "null",
            "-",
        ]
    )
    # ebur128/astats write to stderr; non-zero may still have useful output
    text = (proc.stderr or "") + "\n" + (proc.stdout or "")
    if proc.returncode != 0 and "Summary:" not in text and "Overall" not in text:
        raise MixDspError(
            "ffmpeg filter failed for file %s: %s"
            % (path, text.strip()[-500:] or "exit %d" % proc.returncode)
        )
    return text


def _run_aphasemeter(path: str, channels: int) -> str:
    """Run aphasemeter with ametadata dump (FFmpeg 8 has no stderr phase summary)."""
    if channels < 2:
        return ""
    # video=0 keeps audio-only; phasing=1 enables mono/OOP detection side-channel;
    # ametadata=print:file=- writes lavfi.aphasemeter.phase to stdout.
    for af in (
        "aphasemeter=video=0:phasing=1,ametadata=print:file=-",
        "aphasemeter=video=0,ametadata=print:file=-",
    ):
        try:
            text = _run_filter(path, af)
        except MixDspError:
            continue
        if "lavfi.aphasemeter.phase=" in text:
            return text
        if text.strip():
            return text
    return ""


def _band_filter(lo: int, hi: int) -> str:
    # highpass then lowpass; measure RMS of band-limited signal
    return "highpass=f=%d,lowpass=f=%d,astats=metadata=1:reset=0" % (lo, hi)


def analyze_audio(path: str, label: str) -> dict[str, Any]:
    """Per-file measurement card (ebur128, astats, bands, correlation)."""
    if not isinstance(label, str) or not label.strip():
        raise MixDspError("label must be a nonblank string")
    label = label.strip()
    probe = probe_audio(path)
    validated = validate_local_audio_path(path)
    file_path = str(Path(validated).resolve())

    # peak=true+sample → True peak + Sample peak sections when the build supports both
    ebur_text = _run_filter(validated, "ebur128=peak=true+sample")
    ebur = parse_ebur128(ebur_text, file=file_path)

    astats_text = _run_filter(validated, "astats=metadata=1:reset=0")
    astats = parse_astats(astats_text, file=file_path)

    bands: dict[str, float] = {}
    for band_name, lo, hi in BANDS:
        band_text = _run_filter(validated, _band_filter(lo, hi))
        bands[band_name] = parse_band_rms(band_text, file=file_path, band=band_name)

    phase_text = _run_aphasemeter(validated, probe["channels"])
    correlation = parse_aphasemeter(
        phase_text, file=file_path, channels=probe["channels"]
    )

    evidence_refs: list[str] = []
    evidence_refs.append(
        "lufs:%s:%s" % (label, _fmt_num(ebur["integrated_lufs"]))
    )
    evidence_refs.append(
        "lra:%s:%s" % (label, _fmt_num(ebur["loudness_range_lu"]))
    )
    if "true_peak_dbfs" in ebur:
        evidence_refs.append(
            "true_peak:%s:%s" % (label, _fmt_num(ebur["true_peak_dbfs"]))
        )
    if "sample_peak_dbfs" in ebur:
        evidence_refs.append(
            "sample_peak:%s:%s" % (label, _fmt_num(ebur["sample_peak_dbfs"]))
        )
    evidence_refs.append("rms:%s:%s" % (label, _fmt_num(astats["rms_db"])))
    evidence_refs.append("peak:%s:%s" % (label, _fmt_num(astats["peak_db"])))
    evidence_refs.append(
        "crest:%s:%s" % (label, _fmt_num(astats["crest_factor"]))
    )
    for band_name, _lo, _hi in BANDS:
        evidence_refs.append(
            "band_%s:%s:%s" % (band_name, label, _fmt_num(bands[band_name]))
        )
    if correlation.get("available") and correlation.get("correlation") is not None:
        evidence_refs.append(
            "corr:%s:%s" % (label, _fmt_num(float(correlation["correlation"])))
        )
    else:
        evidence_refs.append("corr:%s:unavailable" % label)

    card: dict[str, Any] = {
        "label": label,
        "path": file_path,
        "probe": probe,
        "ebur128": dict(sorted(ebur.items())),
        "astats": {
            "rms_db": astats["rms_db"],
            "peak_db": astats["peak_db"],
            "crest_factor": astats["crest_factor"],
        },
        "bands_rms_db": {k: bands[k] for k, _, __ in ((b[0], b[1], b[2]) for b in BANDS)},
        "stereo_correlation": correlation,
        "evidence_refs": evidence_refs,
    }
    if "per_channel" in astats:
        card["astats"]["per_channel"] = astats["per_channel"]
    return card


def _overlap_indicators(
    cards: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pairwise same-band descriptive overlap evidence (not a score)."""
    labels = sorted(cards.keys())
    out: list[dict[str, Any]] = []
    for i, a in enumerate(labels):
        for b in labels[i + 1 :]:
            bands_a = cards[a]["bands_rms_db"]
            bands_b = cards[b]["bands_rms_db"]
            for band_name, _, __ in BANDS:
                ra = bands_a[band_name]
                rb = bands_b[band_name]
                # Descriptive: both bands have measurable energy and absolute
                # difference — never labeled good/bad.
                delta = abs(ra - rb)
                out.append(
                    {
                        "labels": [a, b],
                        "band": band_name,
                        "rms_db": {a: ra, b: rb},
                        "abs_delta_db": delta,
                        "description": (
                            "same-band energy comparison for %s and %s in %s Hz: "
                            "%s dB vs %s dB (abs delta %s dB)"
                            % (
                                a,
                                b,
                                band_name.replace("_", "-"),
                                _fmt_num(ra),
                                _fmt_num(rb),
                                _fmt_num(delta),
                            )
                        ),
                    }
                )
    return out


def analyze_mix(master_path: str, group_paths: dict[str, str]) -> dict[str, Any]:
    """Mix-level evidence card: master + groups, refs, overlap indicators."""
    master_validated = validate_local_audio_path(master_path)
    groups_validated = validate_groups(group_paths)
    _require_regular_file(master_validated)
    for gpath in groups_validated.values():
        _require_regular_file(gpath)

    master_card = analyze_audio(master_validated, "master")
    master_sr = master_card["probe"]["sample_rate"]
    master_channels = master_card["probe"]["channels"]
    master_dur = master_card["probe"]["duration"]

    group_cards: dict[str, dict[str, Any]] = {}
    # Stable key order
    for name in sorted(groups_validated.keys()):
        gpath = groups_validated[name]
        card = analyze_audio(gpath, name)
        g_sr = card["probe"]["sample_rate"]
        if g_sr != master_sr:
            raise MixDspError(
                "sample rate mismatch: master=%d Hz, group %s=%d Hz (file %s)"
                % (master_sr, name, g_sr, card["path"])
            )
        g_channels = card["probe"]["channels"]
        if g_channels != master_channels:
            raise MixDspError(
                "channel mismatch: master=%d channels, group %s=%d channels (file %s)"
                % (master_channels, name, g_channels, card["path"])
            )
        g_dur = card["probe"]["duration"]
        drift = abs(g_dur - master_dur)
        if drift > DURATION_DRIFT_LIMIT_S + DURATION_DRIFT_EPSILON_S:
            raise MixDspError(
                "duration drift %.6f s exceeds %.3f s limit for group %s "
                "(master=%.6f s, group=%.6f s, file %s)"
                % (
                    drift,
                    DURATION_DRIFT_LIMIT_S,
                    name,
                    master_dur,
                    g_dur,
                    card["path"],
                )
            )
        card["duration_drift_s"] = drift
        group_cards[name] = card

    all_cards = {"master": master_card, **group_cards}
    evidence_refs: list[str] = []
    for label in ["master"] + sorted(group_cards.keys()):
        evidence_refs.extend(all_cards[label]["evidence_refs"])

    overlap = _overlap_indicators(all_cards)

    for item in overlap:
        a, b = item["labels"]
        band = item["band"]
        evidence_refs.append(
            "overlap:%s+%s:%s:delta_db=%s"
            % (a, b, band, _fmt_num(item["abs_delta_db"]))
        )

    result: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "master": master_card,
        "groups": {k: group_cards[k] for k in sorted(group_cards.keys())},
        "duration_drift_s": {
            name: group_cards[name]["duration_drift_s"]
            for name in sorted(group_cards.keys())
        },
        "band_overlap": overlap,
        "evidence_refs": evidence_refs,
        "probe": {
            "master": master_card["probe"],
            "groups": {
                name: group_cards[name]["probe"]
                for name in sorted(group_cards.keys())
            },
        },
        "provenance": _ffmpeg_provenance(),
    }
    # Explicit: never emit a scalar mix-quality score key
    assert "score" not in result
    assert "mix_score" not in result
    assert "quality" not in result
    return result


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------

_SAMPLE_EBUR = """\
[Parsed_ebur128_0 @ 0x0] Summary:

  Integrated loudness:
    I:         -21.8 LUFS
    Threshold: -31.8 LUFS

  Loudness range:
    LRA:         0.0 LU
    Threshold:   0.0 LUFS
    LRA low:     0.0 LUFS
    LRA high:    0.0 LUFS

  True peak:
    Peak:      -21.1 dBFS
"""

_SAMPLE_ASTATS = """\
[Parsed_astats_0 @ 0x0] Channel: 1
[Parsed_astats_0 @ 0x0] Peak level dB: -21.072762
[Parsed_astats_0 @ 0x0] RMS level dB: -24.084172
[Parsed_astats_0 @ 0x0] Crest factor: 1.414394
[Parsed_astats_0 @ 0x0] Channel: 2
[Parsed_astats_0 @ 0x0] Peak level dB: -21.072762
[Parsed_astats_0 @ 0x0] RMS level dB: -24.084172
[Parsed_astats_0 @ 0x0] Crest factor: 1.414394
[Parsed_astats_0 @ 0x0] Overall
[Parsed_astats_0 @ 0x0] Peak level dB: -21.072762
[Parsed_astats_0 @ 0x0] RMS level dB: -24.084172
[Parsed_astats_0 @ 0x0] Number of samples: 44100
"""

_SAMPLE_APHASE = """\
frame:0  pts:0
lavfi.aphasemeter.phase=0.982000
frame:1  pts:1024
lavfi.aphasemeter.phase=0.981500
"""


def _write_sine(
    path: Path,
    *,
    duration: float,
    sample_rate: int,
    channels: int,
    frequency: float = 440.0,
    amplitude: float = 0.2,
) -> None:
    ff = _ffmpeg()
    # lavfi sine is mono; pan/channels via -ac
    cmd = [
        ff,
        "-hide_banner",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=%s:duration=%s:sample_rate=%d:beep_factor=0"
        % (frequency, duration, sample_rate),
        "-filter:a",
        "volume=%s" % amplitude,
        "-ac",
        str(channels),
        "-c:a",
        "pcm_s16le",
        str(path),
    ]
    proc = _run(cmd)
    if proc.returncode != 0 or not path.is_file():
        raise AssertionError(
            "failed to synthesize %s: %s" % (path, proc.stderr[-400:])
        )


def _write_silence(path: Path, *, duration: float, sample_rate: int, channels: int) -> None:
    ff = _ffmpeg()
    layout = "mono" if channels == 1 else "stereo"
    cmd = [
        ff,
        "-hide_banner",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=%d:cl=%s:d=%s" % (sample_rate, layout, duration),
        "-c:a",
        "pcm_s16le",
        str(path),
    ]
    proc = _run(cmd)
    if proc.returncode != 0 or not path.is_file():
        raise AssertionError(
            "failed to synthesize silence %s: %s" % (path, proc.stderr[-400:])
        )


def _selftest() -> None:
    # --- parser unit tests on official-shaped captured strings ---
    ebur = parse_ebur128(_SAMPLE_EBUR, file="sample.wav")
    assert ebur["integrated_lufs"] == -21.8
    assert ebur["loudness_range_lu"] == 0.0
    assert ebur["true_peak_dbfs"] == -21.1

    ast = parse_astats(_SAMPLE_ASTATS, file="sample.wav")
    assert abs(ast["rms_db"] - (-24.084172)) < 1e-6
    assert abs(ast["peak_db"] - (-21.072762)) < 1e-6
    assert "crest_factor" in ast
    assert "per_channel" in ast
    assert "ch1" in ast["per_channel"]

    phase = parse_aphasemeter(_SAMPLE_APHASE, file="sample.wav", channels=2)
    assert phase["available"] is True
    # mean of 0.982000 and 0.981500
    assert abs(float(phase["correlation"]) - 0.98175) < 1e-6
    assert phase.get("frames_averaged") == 2

    mono_phase = parse_aphasemeter("", file="mono.wav", channels=1)
    assert mono_phase["available"] is False
    assert mono_phase["correlation"] is None
    assert "mono" in (mono_phase["reason"] or "").lower()

    # non-finite rejection
    try:
        parse_ebur128(
            "Summary:\n  Integrated loudness:\n    I:         nan LUFS\n"
            "  Loudness range:\n    LRA:         0.0 LU\n",
            file="bad.wav",
        )
        raise AssertionError("expected MixDspError for nan lufs")
    except MixDspError as exc:
        assert "bad.wav" in str(exc)
        assert "integrated_lufs" in str(exc) or "non-finite" in str(exc)

    # URL / missing-file rejection
    try:
        probe_audio("https://example.com/a.wav")
        raise AssertionError("expected rejection of URL")
    except ValueError:
        pass
    try:
        probe_audio("/tmp/mix_dsp_definitely_missing_zz99.wav")
        raise AssertionError("expected missing-file rejection")
    except MixDspError as exc:
        assert "does not exist" in str(exc)

    # --- synthetic end-to-end ---
    with tempfile.TemporaryDirectory(prefix="mix_dsp_selftest_") as tmp:
        tmp_path = Path(tmp)
        master = tmp_path / "master.wav"
        drums = tmp_path / "drums.wav"
        bass = tmp_path / "bass.wav"
        mono_m = tmp_path / "mono_master.wav"
        mono_a = tmp_path / "mono_a.wav"
        mono_b = tmp_path / "mono_b.wav"
        mono_group = tmp_path / "mono_group.wav"
        bad_sr = tmp_path / "bad_sr.wav"
        boundary_dur = tmp_path / "boundary_dur.wav"
        over_dur = tmp_path / "over_dur.wav"
        bad_dur = tmp_path / "bad_dur.wav"
        near_silent = tmp_path / "near_silent.wav"

        _write_sine(master, duration=0.5, sample_rate=44100, channels=2, frequency=220)
        _write_sine(drums, duration=0.5, sample_rate=44100, channels=2, frequency=110)
        _write_sine(bass, duration=0.5, sample_rate=44100, channels=2, frequency=55)
        _write_sine(mono_m, duration=0.4, sample_rate=44100, channels=1, frequency=330)
        _write_sine(mono_a, duration=0.4, sample_rate=44100, channels=1, frequency=200)
        _write_sine(mono_b, duration=0.4, sample_rate=44100, channels=1, frequency=150)
        _write_sine(mono_group, duration=0.5, sample_rate=44100, channels=1, frequency=180)
        _write_sine(bad_sr, duration=0.5, sample_rate=48000, channels=2, frequency=220)
        _write_sine(boundary_dur, duration=0.55, sample_rate=44100, channels=2, frequency=220)
        _write_sine(over_dur, duration=0.551, sample_rate=44100, channels=2, frequency=220)
        _write_sine(bad_dur, duration=0.6, sample_rate=44100, channels=2, frequency=220)
        _write_silence(near_silent, duration=0.3, sample_rate=44100, channels=2)

        # probe happy path
        probe = probe_audio(str(master))
        assert probe["sample_rate"] == 44100
        assert probe["channels"] == 2
        assert abs(probe["duration"] - 0.5) < 0.02

        # aligned stereo master + 2 stems
        mix = analyze_mix(
            str(master),
            {"drums": str(drums), "bass": str(bass)},
        )
        assert mix["contract_version"] == CONTRACT_VERSION
        assert "master" in mix
        assert set(mix["groups"].keys()) == {"bass", "drums"}
        assert "evidence_refs" in mix and mix["evidence_refs"]
        assert "band_overlap" in mix and mix["band_overlap"]
        assert "provenance" in mix
        assert "ffmpeg_version" in mix["provenance"]
        assert "score" not in mix
        assert "mix_score" not in mix
        assert "quality" not in json.dumps(mix)
        # bands present as measured values
        for band_name, _, __ in BANDS:
            assert band_name in mix["master"]["bands_rms_db"]
            assert isinstance(mix["master"]["bands_rms_db"][band_name], float)
        # drift recorded and within limit
        for name, drift in mix["duration_drift_s"].items():
            assert drift <= DURATION_DRIFT_LIMIT_S

        # evidence stability (same input → same refs aside from absolute paths)
        mix2 = analyze_mix(
            str(master),
            {"drums": str(drums), "bass": str(bass)},
        )
        assert mix["evidence_refs"] == mix2["evidence_refs"]
        assert mix["master"]["ebur128"] == mix2["master"]["ebur128"]
        assert mix["master"]["astats"]["rms_db"] == mix2["master"]["astats"]["rms_db"]
        assert mix["band_overlap"] == mix2["band_overlap"]

        # mono → correlation unavailable
        mono_card = analyze_audio(str(mono_m), "mono_master")
        assert mono_card["stereo_correlation"]["available"] is False
        assert mono_card["stereo_correlation"]["correlation"] is None
        assert "mono" in mono_card["stereo_correlation"]["reason"].lower()
        assert any(r.endswith(":unavailable") for r in mono_card["evidence_refs"] if r.startswith("corr:"))

        mono_mix = analyze_mix(
            str(mono_m),
            {"a": str(mono_a), "b": str(mono_b)},
        )
        assert mono_mix["master"]["stereo_correlation"]["available"] is False

        # sample-rate mismatch
        try:
            analyze_mix(str(master), {"drums": str(drums), "bad": str(bad_sr)})
            raise AssertionError("expected sample-rate mismatch")
        except MixDspError as exc:
            assert "sample rate" in str(exc).lower()

        # All exports in one evidence pass must share a channel layout.
        try:
            analyze_mix(str(master), {"drums": str(drums), "mono": str(mono_group)})
            raise AssertionError("expected channel mismatch")
        except MixDspError as exc:
            assert "channel" in str(exc).lower()

        # Exactly 50 ms is accepted; >50 ms remains fail-closed.
        boundary_mix = analyze_mix(
            str(master), {"drums": str(drums), "boundary": str(boundary_dur)}
        )
        assert boundary_mix["duration_drift_s"]["boundary"] <= DURATION_DRIFT_LIMIT_S + 1e-9
        try:
            analyze_mix(str(master), {"drums": str(drums), "over": str(over_dur)})
            raise AssertionError("expected duration mismatch")
        except MixDspError as exc:
            assert "exceeds 0.050 s limit" in str(exc)

        # Larger duration mismatch
        try:
            analyze_mix(str(master), {"drums": str(drums), "bad": str(bad_dur)})
            raise AssertionError("expected duration mismatch")
        except MixDspError as exc:
            assert "duration drift" in str(exc).lower()

        # silent / near-silent: must not invent scores; either measurements or
        # actionable missing-metric error naming file+measurement
        try:
            silent_card = analyze_audio(str(near_silent), "near_silent")
            assert "rms_db" in silent_card["astats"]
            assert "evidence_refs" in silent_card
            assert "score" not in silent_card
        except MixDspError as exc:
            msg = str(exc)
            assert "near_silent" in msg or str(near_silent) in msg
            assert any(
                token in msg
                for token in ("rms_db", "integrated_lufs", "peak_db", "measurement")
            )

        # real synthetic end-to-end filter run already covered by analyze_mix above
        # (ebur128 + astats + bands exercised)

        # overlap indicators name labels + band, no taste words
        for item in mix["band_overlap"]:
            assert "labels" in item and len(item["labels"]) == 2
            assert "band" in item
            blob = json.dumps(item).lower()
            for banned in ("bad", "good", "score", "quality"):
                # allow "band" etc.; check whole-word-ish
                assert '"%s"' % banned not in blob


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deterministic local FFmpeg mix evidence (no taste score)."
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="run synthetic selftest (no network/Live/fixtures)",
    )
    parser.add_argument("--probe", metavar="PATH", help="probe one audio file")
    parser.add_argument(
        "--analyze",
        metavar="PATH",
        help="analyze one audio file (requires --label)",
    )
    parser.add_argument("--label", default="track", help="label for --analyze")
    parser.add_argument("--master", metavar="PATH", help="master path for --mix")
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="group stem NAME=PATH (repeat 2–4 times with --master)",
    )
    args = parser.parse_args()

    if args.selftest:
        _selftest()
        print("=== mix_dsp selftest passed ===")
        return

    if args.probe:
        print(json.dumps(probe_audio(args.probe), indent=2, sort_keys=True))
        return

    if args.analyze:
        print(
            json.dumps(
                analyze_audio(args.analyze, args.label),
                indent=2,
                sort_keys=True,
            )
        )
        return

    if args.master:
        groups: dict[str, str] = {}
        for item in args.group:
            if "=" not in item:
                raise SystemExit("group must be NAME=PATH, got %r" % item)
            name, path = item.split("=", 1)
            groups[name] = path
        print(
            json.dumps(
                analyze_mix(args.master, groups),
                indent=2,
                sort_keys=True,
            )
        )
        return

    parser.print_help()


if __name__ == "__main__":
    main()
