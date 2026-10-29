#!/usr/bin/env python3
"""Audio reference pipeline: describe (agy/Gemini) + features + audio→MIDI.

Accepts a local audio file OR a YouTube URL. Writes artifacts under
store/audio_refs/<stem>_<timestamp>/ and upserts store/audio_ref_index.json.

  source.wav     working audio (fetched/converted)
  features.json  ffprobe + note-derived stats
  describe.txt   listening brief via agy (Gemini multimodal)
  ref.mid        Basic Pitch transcription (weak on pure drums)
  meta.json      run metadata / warnings

Examples:
  .venv/bin/python tools/audio_ref.py ~/Desktop/ref.wav --focus groove
  .venv/bin/python tools/audio_ref.py 'https://youtu.be/...' \\
      --start 5:30 --end 8:30 --focus melody --prompt-extra 'your note'
  .venv/bin/python tools/audio_ref.py --from-dir store/audio_refs/yt-... --modes describe --focus groove
  .venv/bin/python tools/audio_ref.py --batch refs.json
  .venv/bin/python tools/audio_ref.py --reindex
  .venv/bin/python tools/audio_ref.py --offload-sources /Volumes/<ext>/audioref-sources [--confirm]
  .venv/bin/python tools/audio_ref.py --restore-sources [--confirm]

Deps (venv): basic-pitch, yt-dlp; pin setuptools<81 for resampy/pkg_resources.
System: ffmpeg, ffprobe, agy (Antigravity CLI) on PATH.
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import os
import re
import contextlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ``server.py`` uses the legacy bare ``audio_ref`` import while tests and the
# package surface use ``tools.audio_ref``.  They must share patched globals,
# caches, and overlay paths.
for _alias in ("audio_ref", "tools.audio_ref"):
    sys.modules.setdefault(_alias, sys.modules[__name__])

ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "store" / "audio_refs"
INDEX_PATH = ROOT / "store" / "audio_ref_index.json"
BATCH_ROOT = OUT_ROOT / "batches"
YOUTUBE_HOSTS = (
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
)
DEFAULT_MODES = ("describe", "midi", "features")
# Focus modes: the SINGLE source. The mode list, the per-mode intent, the
# prompt overlays, normalize_focus, the CLI --focus choices, session-ears
# validation and the reconciler's claim sets all derive from these two
# objects — there is no parallel list. (composition was retired: v3 has no
# composition-only keys, and the reconciler maps groove/harmony/melody.)
FOCUS_MODES: dict[str, str] = {
    "full": "the whole card; every claim reconciled",
    "groove": "the pocket: tempo, swing, drum grid, sections and their energies",
    "harmony": "tonality and voicing: modal center, pitch span, register",
    "melody": "lines: phrase length, repetition, range",
}
FOCUS_CHOICES = tuple(FOCUS_MODES)
DEFAULT_AGY_MODEL = "gemini-3.8-flash-medium"

# --- audio-ref-card-v2: schema-enforced listening ------------------------------
# v1 asked a model to write markdown to a path and then scraped it with regexes.
# Two failures came from that: on 2026-08-27 a newer model simply declined to
# write the file (5/5 failed, and the stale card kept reporting success), and the
# prompt's few-shot EXAMPLE was copied instead of listened to — "0% swing" in
# 57/59 briefs, 13 tracks reported at the example's 68 BPM including ones that
# are not. A JSON schema constrains SHAPE without handing the model CONTENT.

CARD_CONTRACT_VERSION = "audio-ref-card-v2"

_ENUM_DENSITY = ["sparse", "medium", "dense"]      # aligned with session-ears take-cards
_ENUM_BAND = ["low", "mid", "high"]
_ENUM_CONF = ["low", "med", "high"]
_ENUM_EVENT = ["enter", "exit", "drop", "build", "breakdown", "texture_change", "other"]
_ENUM_CONTOUR = ["static", "rising", "falling", "arch", "zigzag"]
_ENUM_CALL = ["none", "call_and_response", "answer_only"]
_ENUM_REPEAT = ["verbatim", "varied", "through_composed", "continuous", "periodic"]
_ENUM_DRUM = ["kick", "snare", "hat", "openhat", "ride", "crash", "tom", "clap",
              "rim", "perc"]
_MMSS_PATTERN = r"^[0-9]{1,2}:[0-5][0-9]$"


def _obj(props: dict, **kw) -> dict:
    """An object node that is CLOSED and whose every key is required.

    Optionality is expressed as a nullable TYPE, never as an absent key: a model
    that cannot hear something must say so on purpose rather than go quiet."""
    node = {"type": "object", "properties": props,
            "required": sorted(props), "additionalProperties": False}
    node.update(kw)
    return node


def _nullable(*types: str) -> list[str]:
    return list(types) + ["null"]


def _nullable_enum(values: list[str]) -> dict:
    """A nullable enumerated string.

    Measured 2026-09-03: agy REJECTS a schema whose enum contains null
    ("Agent execution terminated due to error", 0.7s, before listening) while
    accepting every other construct here. So the wire schema carries the type
    only; validate_card_v2 enforces the enum, and the prompt names the values.
    Enforcement was always ours anyway — the same probe showed --json-schema is
    a hint, not a guarantee: the model returned extra keys of its own."""
    return {"type": _nullable("string"), "x-enum": values}


V2_SCHEMA: dict = _obj({
    "contract_version": {"const": CARD_CONTRACT_VERSION},
    "identity": {"type": "string", "maxLength": 160},
    "tempo": _obj({
        "grid_bpm": {"type": _nullable("number")},
        "feel_bpm": {"type": _nullable("number")},
        "confidence": {"enum": _ENUM_CONF},
    }),
    "meter": _obj({
        "beats_per_bar": {"type": _nullable("integer")},
        "half_time": {"type": _nullable("boolean")},
    }),
    "swing": _obj({
        "pct": {"type": _nullable("number")},
        "confidence": {"enum": _ENUM_CONF},
    }),
    "density": {"enum": _ENUM_DENSITY},
    "register": _obj({
        "band": _nullable_enum(_ENUM_BAND),
        "pitch_span": {"type": _nullable("string")},
    }),
    "harmony": _obj({
        "modal_center": {"type": _nullable("string")},
        "pitch_classes": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "interval_content": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "harmonic_rhythm_bars": {"type": _nullable("number")},
        "confidence": {"enum": _ENUM_CONF},
    }),
    "melody": _obj({
        "phrase_length_bars": {"type": _nullable("number")},
        "contour": _nullable_enum(_ENUM_CONTOUR),
        "call_response": _nullable_enum(_ENUM_CALL),
        "motif_note_count": {"type": _nullable("integer")},
        "repetition": _nullable_enum(_ENUM_REPEAT),
    }),
    "drums": _obj({
        "grid": {"type": "array", "maxItems": 10, "items": _obj({
            "instrument": {"enum": _ENUM_DRUM},
            "steps": {"type": "array", "maxItems": 16,
                      "items": {"type": "integer", "minimum": 0, "maximum": 15}},
        })},
        "confidence": {"enum": _ENUM_CONF},
    }),
    "sections": {"type": "array", "minItems": 1, "maxItems": 8, "items": _obj({
        "t": {"type": "string", "pattern": _MMSS_PATTERN},
        "label": {"type": "string", "maxLength": 90},
        "event": {"enum": _ENUM_EVENT},
        "density": _nullable_enum(_ENUM_DENSITY),
        "register": _nullable_enum(_ENUM_BAND),
        "harmony": {"type": _nullable("string")},
    })},
    "imitation": _obj({
        "do": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "string"}},
        "dont": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
    }),
    "uncertain": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
})

V3_CONTRACT_VERSION = "audio-ref-card-v3"

_ENUM_LAYER_ROLE = ["drums", "bass", "pad", "lead", "texture", "fx"]

_V3_EVIDENCE_T = {"type": _nullable("string"), "pattern": _MMSS_PATTERN}

_V3_RULE = _obj({
    "rule": {"type": "string"},
    "t": {"type": _nullable("string"), "pattern": _MMSS_PATTERN},
    "ableton_move": {"type": _nullable("string"), "maxLength": 160},
    "why": {"type": _nullable("string"), "maxLength": 160},
})

V3_SCHEMA: dict = _obj({
    "contract_version": {"const": V3_CONTRACT_VERSION},
    "identity": {"type": "string", "maxLength": 160},
    "tempo": _obj({
        "grid_bpm": {"type": _nullable("number")},
        "feel_bpm": {"type": _nullable("number")},
        "confidence": {"enum": _ENUM_CONF},
        "evidence_t": _V3_EVIDENCE_T,
    }),
    "meter": _obj({
        "beats_per_bar": {"type": _nullable("integer")},
        "half_time": {"type": _nullable("boolean")},
    }),
    "swing": _obj({
        "pct": {"type": _nullable("number")},
        "confidence": {"enum": _ENUM_CONF},
        "evidence_t": _V3_EVIDENCE_T,
    }),
    "density": {"enum": _ENUM_DENSITY},
    "register": _obj({
        "band": _nullable_enum(_ENUM_BAND),
        "pitch_span": {"type": _nullable("string")},
    }),
    "harmony": _obj({
        "modal_center": {"type": _nullable("string")},
        "pitch_classes": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "confidence": {"enum": _ENUM_CONF},
        "evidence_t": _V3_EVIDENCE_T,
    }),
    "melody": _obj({
        "phrase_length_bars": {"type": _nullable("number")},
        "repetition": _nullable_enum(_ENUM_REPEAT),
    }),
    "drums": _obj({
        "grid": {"type": "array", "maxItems": 10, "items": _obj({
            "instrument": {"enum": _ENUM_DRUM},
            "steps": {"type": "array", "maxItems": 16,
                      "items": {"type": "integer", "minimum": 0, "maximum": 15}},
        })},
        "confidence": {"enum": _ENUM_CONF},
        "evidence_t": _V3_EVIDENCE_T,
    }),
    "sections": {"type": "array", "minItems": 1, "maxItems": 16, "items": _obj({
        "t": {"type": "string", "pattern": _MMSS_PATTERN},
        "label": {"type": "string", "maxLength": 90},
        "energy": {"type": "integer", "minimum": 1, "maximum": 5},
        "layers_in": {"type": "array", "maxItems": 8,
                      "items": {"type": "string", "maxLength": 40}},
        "layers_out": {"type": "array", "maxItems": 8,
                       "items": {"type": "string", "maxLength": 40}},
        "transition_in": {"type": _nullable("string"), "maxLength": 60},
    })},
    "layers": {"type": "array", "maxItems": 8, "items": _obj({
        "role": {"enum": _ENUM_LAYER_ROLE},
        "timbre": {"type": "string", "maxLength": 80},
        "first_t": {"type": "string", "pattern": _MMSS_PATTERN},
    })},
    "low_end": _obj({
        "kick_bass_relation": {"type": _nullable("string"), "maxLength": 160},
        "sidechain_audible": {"type": _nullable("boolean")},
    }),
    "space": {"type": _nullable("string"), "maxLength": 240},
    "the_trick": {"type": ["object", "null"],
                  "properties": {
                      "what": {"type": "string", "maxLength": 160},
                      "t": {"type": "string", "pattern": _MMSS_PATTERN},
                  },
                  "required": ["t", "what"], "additionalProperties": False},
    "imitation": _obj({
        "do": {"type": "array", "maxItems": 8, "items": _V3_RULE},
        "dont": {"type": "array", "maxItems": 6, "items": _V3_RULE},
    }),
    "uncertain": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
})

# No musical content anywhere. The only literal is the MM:SS placeholder, which
# cannot be copied as a claim about the music.
DESCRIBE_PROMPT_V2 = """You are transcribing what you hear in one audio file into structured data.

File: {name}
Length: {duration_s:.0f} seconds ({duration_mmss}).

Rules:
- Describe ONLY what you hear in THIS file. Never what a track of this kind usually does.
- If a statement of yours would be equally true of most records in this genre, it is
  probably not something you heard. Listen again, or use null.
- Prefer null over a guess, and an empty list over an invented one. Say what you could
  not determine in "uncertain".
- Every timestamp must be inside the file's length, written as MM:SS.
- Pitch classes use letter names. Intervals use m2/M2/m3/M3/P4/TT/P5/m6/M6/m7/M7/P8.
- Drum steps are 0-15 within one bar of sixteenths.
- "imitation.do" and "imitation.dont" are instructions to a composer working on a
  different piece: what to reproduce, and what would break it.

Return exactly one JSON object matching the provided schema. No prose, no markdown,
no commentary.{focus}{prompt_extra}{measured_note}"""

# v3 prompt: the allowed enums are spelled out (the wire schema cannot carry
# them), generic rules are banned by name, and there are no content examples
# to copy — only the MM:SS placeholder, which cannot be a musical claim.
DESCRIBE_PROMPT_V3 = """You are transcribing what you hear in one audio file into structured data.

File: {name}
Length: {duration_s:.0f} seconds ({duration_mmss}).
This is the ONLY audio you have. If you cannot hear the described thing, use
null — a null is honest, a guess contaminates every track that steers off
this card.
{focus}{prompt_extra}{measured_note}
Allowed values, exactly: density is sparse, medium or dense; register band is
low, mid or high; every confidence is low, med or high; section energy is an
integer 1-5; layer role is drums, bass, pad, lead, texture or fx; drum
instruments are kick, snare, hat, openhat, ride, crash, tom, clap, rim or
perc. Melody repetition is verbatim, varied, through_composed, continuous,
periodic or null. Times are M:SS. Nothing else is allowed where an enum applies.
Sections: up to 16, each with its time, a short label, an energy 1-5, which
layers enter and leave, and how the section arrives. Layers: each voice with
its role, its timbre in plain words, and where it first appears. Low end: how
kick and bass relate, and whether sidechain pumping is audible. Space: the
room and width only when supported by the supplied audio; null is allowed.
Never infer stereo width from mono audio. The trick: an identifiable audible move —
what it is and when it happens, or null when there is none.
Imitation rules are optional application proposals, not facts about the artist's
equipment or rules for the composer's music. Leave do and dont empty for an
observation-only request or when no supported proposal follows. Otherwise each
one names its moment, a possible Ableton move, and why it might work.
A rule that would apply to any track in
this genre — stay in key, no swing — is not allowed. Write only rules this
exact recording earns.
Every confidence below high must say what is uncertain in the uncertain list:
which tempo, which key, which grid.
Return exactly one JSON object matching the provided schema. No prose, no
markdown, no commentary."""

FOCUS_OVERLAYS: dict[str, str] = {
    "full": "",
    "groove": (
        "FOCUS=groove (composer chose this). Bias hard toward the pocket: "
        "tempo.grid_bpm vs feel_bpm, swing.pct, the drum grid (drums.grid) "
        "instruments and their steps, sections and their energies and "
        "transitions, and imitation rules with concrete Ableton moves for "
        "the groove. Keep Identity short; harmony.modal_center and melody "
        "lines only if they frame the pocket.\n"
    ),
    "harmony": (
        "FOCUS=harmony (composer chose this). Emphasize tonality and voicing: "
        "harmony.modal_center and pitch_classes (the modal center and its "
        "pitch classes), register.band and pitch_span, pad and keys layer "
        "timbres with their first_t entry times, the low-end kick/bass "
        "relation and space. Groove and lines only as they serve the harmony.\n"
    ),
    "melody": (
        "FOCUS=melody (composer chose this). Emphasize lines: "
        "melody.phrase_length_bars and repetition, register.band and range, "
        "lead layer timbre and first_t appearance, the_trick when it is "
        "melodic. Keep the groove brief unless it frames the lines.\n"
    ),
}
# X5 Pass B: optional second listen over 2-3 short windows cut from the
# Pass A section energy peaks (or the focus). Each window is heard for
# groove + drum grid + sound design and merged into the card as windows[].
PASS_B_MIN_S = 30.0
PASS_B_MAX_S = 45.0
PASS_B_MAX_WINDOWS = 3
PASS_B_WINDOW_OVERLAY = (
    "PASS B window {i} of {n} ({start_mmss}-{end_mmss} of the full track). "
    "This file is ONLY that excerpt: transcribe the groove, drum grid and "
    "sound design of THIS excerpt — tempo feel vs grid, swing, drums.grid "
    "steps, layers entering here, low_end, space, the_trick when it happens "
    "inside this window. Timestamps are relative to THIS file (00:00 = "
    "excerpt start), never the full track.\n"
)
_PASS_B_FOCUS_ASPECT = "groove+drums+sound-design"
# Window placement for a pitched focus prefers sections whose label/layers
# name a pitched voice; energy still orders within each group.
_PASS_B_PITCHED_WORDS = ("pad", "keys", "lead", "bass", "arp", "piano",
                         "synth", "strings", "vocal", "choir", "guitar",
                         "organ", "brass")

# Gemini multimodal tips baked in (Google AI docs + Applied AI audio recipes):
# - clear constraints + few-shot output shape
# - step-by-step: listen → reason briefly → write file
# - MM:SS timestamps only; clip duration hard-bound
# - music/non-speech is weaker than speech — prefer concrete imitation rules
# - Antigravity slugs encode reasoning effort in the model name (-high/-medium/-low)
#   and duplicate --effort flags are rejected, so we only add --effort high when
#   the model name has no suffix.
DESCRIBE_PROMPT = """You are a music-reference analyst for an electronic composer in Ableton.

TASK (do in order):
1) Listen to the entire audio file{source_hint}.
2) Mentally note groove, melody/harmony, texture, and 3–5 copyable moments.
3) Write ONLY to this absolute path (create/overwrite): {describe_path}
4) Reply with exactly: OK wrote {describe_path}

CLIP CONSTRAINT (hard):
- File length = {duration_s:.1f}s = {duration_mmss}.
- # Moments times are relative to THIS file (00:00 = start).
- Format every moment time as MM:SS (examples: 00:12, 01:45).
- NEVER invent a moment after {duration_mmss}. If unsure, omit it.

Output format — use EXACTLY these markdown headings (few-shot shape):

# Identity
Post-metal half-time sludge; mechanical ride pulse.

# Groove
- Kick/snare: kick on 1, snare on 3 (half-time); occasional 4-and pickup.
- Density: sparse; 0% swing; ~68 BPM feel.

# Harmony / melody
- C# minor/Aeolian drone; 2-bar 3–4 note ostinato (m3, P4).

# Texture / production
- Distorted wall vs clean chorus arps; long snare room tail.

# Imitation rules
- Do: snare only on beat 3 at ~68 BPM; hard-quantize.
- Do: leave space for snare reverb tail.
- Don't: add swing or busy snare fills.
- Don't: fast chord changes — keep static modal center.

# Moments
- 00:15 – drums enter, half-time pocket locks.
- 01:30 – crash-ride wash replaces closed hats.

{prompt_extra}

Final checks before writing the file:
- Every Moments timestamp ≤ {duration_mmss}
- Imitation rules are concrete enough to write MIDI from
- Prefer audible facts over vibe adjectives
"""

_MOMENT_TS_RE = re.compile(
    r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2}(?:\.\d+)?)|(?P<m2>\d{1,3}):(?P<s2>\d{2}(?:\.\d+)?)"
)


def _which(name: str) -> str | None:
    return shutil.which(name)


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, text=True, capture_output=True, **kwargs)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_mmss(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m = int(seconds // 60)
    s = int(round(seconds - m * 60))
    if s == 60:
        m += 1
        s = 0
    return f"{m:02d}:{s:02d}"


def is_youtube_url(source: str) -> bool:
    s = source.strip()
    if not (s.startswith("http://") or s.startswith("https://")):
        return False
    host = urlparse(s).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in YOUTUBE_HOSTS)


def parse_time(value: str | None) -> float | None:
    """Parse seconds or mm:ss / hh:mm:ss → float seconds."""
    if value is None or value == "":
        return None
    value = value.strip()
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)
    parts = value.split(":")
    if not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts):
        raise ValueError(f"bad time: {value!r} (use seconds or mm:ss)")
    parts_f = [float(p) for p in parts]
    if len(parts_f) == 2:
        return parts_f[0] * 60 + parts_f[1]
    if len(parts_f) == 3:
        return parts_f[0] * 3600 + parts_f[1] * 60 + parts_f[2]
    raise ValueError(f"bad time: {value!r}")


def slugify(text: str, max_len: int = 48) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text or "ref")[:max_len]


def normalize_focus(focus: str | None) -> str:
    f = (focus or "full").strip().lower()
    if f not in FOCUS_CHOICES:
        raise ValueError(f"focus must be one of {FOCUS_CHOICES}, got {focus!r}")
    return f


def detect_source(source: str) -> tuple[str, str]:
    """Return (kind, resolved) where kind is 'youtube' or 'file'."""
    source = source.strip()
    if is_youtube_url(source):
        return "youtube", source
    if source.startswith("http://") or source.startswith("https://"):
        raise ValueError(
            f"unsupported URL host (v1 accepts YouTube only): {source}"
        )
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"audio file not found: {path}")
    return "file", str(path)


def make_out_dir(stem: str, root: Path | None = None) -> Path:
    """Create ``{stem}_{stamp}`` under root (default OUT_ROOT).

    On FileExistsError retry with ``stem-2``, ``stem-3``, … up to ``stem-10``,
    then raise.
    """
    root = OUT_ROOT if root is None else Path(root)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    last_err: OSError | None = None
    for n in range(1, 11):
        name = f"{stem}_{stamp}" if n == 1 else f"{stem}-{n}_{stamp}"
        out = root / name
        try:
            out.mkdir(parents=True, exist_ok=False)
            return out
        except FileExistsError as exc:
            last_err = exc
    raise FileExistsError(
        f"could not create unique out dir for stem={stem!r} under {root} "
        f"after 10 attempts"
    ) from last_err


def _pick_raw_download(out_dir: Path, requested: str) -> Path:
    """Pick yt-dlp output without depending on filesystem glob order.

    Prefer the exact ``requested`` path when it exists; otherwise accept exactly
    one ``yt_raw.*`` match; otherwise raise listing candidates (sorted).
    """
    req = Path(requested)
    if req.is_file():
        return req
    candidates = sorted(out_dir.glob("yt_raw.*"))
    if len(candidates) == 1:
        return candidates[0]
    raise RuntimeError(
        "yt-dlp finished but could not pick raw download "
        f"(requested={requested!r}, candidates={[str(c) for c in candidates]})"
    )


def _version_first_line(cmd: list[str]) -> str | None:
    try:
        proc = _run(cmd, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    text = (proc.stdout or proc.stderr or "").strip()
    if not text:
        return None
    return text.splitlines()[0].strip() or None


def _module_version(mod: Any) -> str | None:
    ver = getattr(mod, "__version__", None)
    if ver:
        return str(ver)
    nested = getattr(mod, "version", None)
    if nested is not None:
        ver = getattr(nested, "__version__", None)
        if ver:
            return str(ver)
    return None


def collect_tool_versions() -> dict[str, str]:
    """Best-effort tool versions for meta.json. Omit unavailable keys.

    Lazy-import safe: no top-level basic_pitch / yt_dlp imports.
    """
    out: dict[str, str] = {}

    ffmpeg = _which("ffmpeg")
    if ffmpeg:
        try:
            v = _version_first_line([ffmpeg, "-version"])
        except subprocess.TimeoutExpired:
            v = None
        if v:
            out["ffmpeg"] = v

    ffprobe = _which("ffprobe")
    if ffprobe:
        try:
            v = _version_first_line([ffprobe, "-version"])
        except subprocess.TimeoutExpired:
            v = None
        if v:
            out["ffprobe"] = v

    try:
        import yt_dlp as _yt_dlp

        v = _module_version(_yt_dlp)
        if not v:
            ytdlp_bin = _which("yt-dlp")
            if ytdlp_bin:
                v = _version_first_line([ytdlp_bin, "--version"])
        if v:
            out["yt_dlp"] = v
    except Exception:
        pass

    try:
        import basic_pitch as _basic_pitch

        v = _module_version(_basic_pitch)
        if v:
            out["basic_pitch"] = v
    except Exception:
        pass

    agy = _which("agy")
    if agy:
        try:
            v = _version_first_line([agy, "--version"])
        except subprocess.TimeoutExpired:
            v = None
        if v:
            out["agy"] = v

    return out


def _offload_record(out_dir: str | Path) -> dict[str, Any] | None:
    """The ref's ``source_offloaded`` record from meta.json, if any."""
    meta_path = Path(out_dir) / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    record = meta.get("source_offloaded")
    return record if isinstance(record, dict) else None


def _dir_is_filed_ref(out_dir: Path) -> bool:
    """Whether a directory is a filed reference, audio present or not.

    ONE predicate shared by the reindex gate and ``get_ref_card``'s disk
    fallback so the two can never disagree. ``source.wav`` or ``card.json``
    makes the ref; an offload record in meta.json keeps an offloaded ref
    filed; a meta.json alone does NOT — a failed download writes meta.json
    with ok:false and no wav, and indexing those files phantom refs.
    """
    return (
        (out_dir / "source.wav").is_file()
        or (out_dir / "card.json").is_file()
        or _offload_record(out_dir) is not None
    )


def _require_local_source(out_dir: str | Path) -> Path:
    """The ref's source.wav, or the clearest possible error when it is gone.

    An offloaded source is recoverable, so say where it went and how to get it
    back — a bare "missing source.wav" reads as corruption and invites a
    wasteful re-download that would duplicate the ref.
    """
    out_dir = Path(out_dir)
    wav = out_dir / "source.wav"
    if wav.is_file():
        return wav
    record = _offload_record(out_dir)
    if record:
        raise FileNotFoundError(
            f"source.wav offloaded to {record.get('path')}; mount the volume and "
            "run tools/audio_ref.py --restore-sources --confirm"
        )
    raise FileNotFoundError(f"missing source.wav in {out_dir}")


def resolve_from_dir(from_dir: str | Path) -> Path:
    p = Path(from_dir).expanduser()
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    else:
        p = p.resolve()
    _require_under_out_root(p, label="from_dir")
    if not p.is_dir():
        raise FileNotFoundError(f"from_dir not found: {p}")
    _require_local_source(p)
    return p


def _require_under_out_root(path: Path, *, label: str = "path") -> Path:
    """Resolve and require path is inside store/audio_refs/ (no traversal)."""
    root = OUT_ROOT.resolve()
    root.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must be under {root}, got {resolved}") from exc
    return resolved


def normalize_ref_id(ref_id: str) -> str:
    rid = (ref_id or "").strip()
    if not rid or "/" in rid or "\\" in rid or rid in (".", "..") or ".." in rid:
        raise ValueError(f"invalid ref_id: {ref_id!r}")
    return rid


def ref_dir_for_id(ref_id: str) -> Path:
    return _require_under_out_root(OUT_ROOT / normalize_ref_id(ref_id), label="ref_id")


def ensure_out_dir_contained(out_dir: str | Path) -> Path:
    return _require_under_out_root(Path(out_dir), label="out_dir")


def ffprobe_info(wav: Path, *, timeout_s: float | None = 30) -> dict[str, Any]:
    exe = _which("ffprobe")
    if not exe:
        return {"error": "ffprobe not found"}
    cmd = [
        exe, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(wav),
    ]
    proc = _run(cmd, timeout=timeout_s)
    if proc.returncode != 0:
        return {"error": proc.stderr.strip() or "ffprobe failed"}
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    fmt = data.get("format") or {}
    return {
        "duration_s": float(fmt.get("duration") or audio.get("duration") or 0) or None,
        "sample_rate": int(audio["sample_rate"]) if audio.get("sample_rate") else None,
        "channels": int(audio["channels"]) if audio.get("channels") else None,
        "codec": audio.get("codec_name"),
        "format": fmt.get("format_name"),
        "size_bytes": int(fmt["size"]) if fmt.get("size") else None,
    }


def ffmpeg_to_wav(src: Path, dst: Path, start: float | None, end: float | None, *,
                  channels: int | None = 1,
                  sample_rate: int | None = 22050,
                  timeout_s: float | None = 120) -> None:
    """Transcode audio into a plain 16-bit PCM wav.

    The explicit ``-c:a pcm_s16le`` (not ffmpeg's wav default) plus a
    ≤44100 Hz / ≤2-channel output keeps the header plain
    WAVE_FORMAT_PCM (fmt tag 1): raw session takes (24-bit/float
    WAVE_FORMAT_EXTENSIBLE) and mp3s are the exact inputs agy
    demonstrably cannot open (step_type 17 "unsupported mime type" in
    real conversation records). ``channels=None`` keeps ffmpeg's
    default channel mapping; the default mono 22050 Hz serves the
    measurement paths. ``timeout_s`` bounds the ffmpeg call.
    """
    exe = _which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg not found on PATH")
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(src)]
    if end is not None:
        if start is None:
            cmd += ["-t", str(end)]
        else:
            cmd += ["-t", str(max(0.0, end - start))]
    cmd += ["-c:a", "pcm_s16le"]
    if channels is not None:
        cmd += ["-ac", str(int(channels))]
    if sample_rate is not None:
        cmd += ["-ar", str(int(sample_rate))]
    cmd += [str(dst)]
    proc = _run(cmd, timeout=timeout_s)
    if proc.returncode != 0 or not dst.is_file():
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip() or proc.stdout}")


# Listener staging caps the sample rate at 44.1 kHz (plain-header
# range; anything higher only costs the listener turn quota).
_STAGE_MAX_SAMPLE_RATE = 44100
_STAGE_FFMPEG_TIMEOUT_S = 120


def _capped_stage_rate(src: str | Path) -> int:
    """The transcode rate for a listener copy: the source rate capped
    at 44.1 kHz. Falls back to the cap when ffprobe cannot read the
    source. Never raises."""
    try:
        rate = (ffprobe_info(Path(src)) or {}).get("sample_rate")
        if isinstance(rate, bool):
            return _STAGE_MAX_SAMPLE_RATE
        if isinstance(rate, (int, float)) and float(rate) > 0:
            return int(min(float(rate), _STAGE_MAX_SAMPLE_RATE))
    except Exception:
        pass
    return _STAGE_MAX_SAMPLE_RATE


def _probed_stage_channels(src: str | Path) -> int | None:
    """The staged channel count: the source's own channel count.

    Passed as an explicit ``-ac`` so ffmpeg normalizes the channel
    layout instead of carrying a session-take mask through: without it
    the wav muxer copies WAVE_FORMAT_EXTENSIBLE onto the output even
    at 16-bit (measured), which is the exact header agy demonstrably
    cannot open. ``None`` when ffprobe cannot read the source (the
    transcode then keeps ffmpeg's default mapping). Never raises.
    """
    try:
        channels = (ffprobe_info(Path(src), timeout_s=_STAGE_FFMPEG_TIMEOUT_S) or {}).get("channels")
        if isinstance(channels, bool):
            return None
        if isinstance(channels, (int, float)) and int(channels) > 0:
            return min(int(channels), 2)
    except Exception:
        pass
    return None


def stage_listenable_copy(src: str | Path, dst: str | Path,
                          start: float | None = None, end: float | None = None, *,
                          timeout_s: float | None = _STAGE_FFMPEG_TIMEOUT_S) -> Path:
    """Transcode audio into a plain listenable PCM copy for an agy listen.

    Every listener (card, Pass B windows via their ffmpeg cut, takes,
    witness) stages this instead of the raw file: channels kept, rate
    capped at 44.1 kHz, 16-bit PCM, no extensible header. The ffmpeg
    call is bounded by ``timeout_s``. Raises (ffmpeg missing/failed)
    rather than staging the raw file — an unlistenable input must fail
    loudly here, not as a wasted listener turn downstream.
    """
    out = Path(dst)
    ffmpeg_to_wav(Path(src), out, start, end,
                  channels=_probed_stage_channels(src),
                  sample_rate=_capped_stage_rate(src),
                  timeout_s=timeout_s)
    return out


def fetch_youtube(
    url: str, out_dir: Path, start: float | None, end: float | None
) -> tuple[Path, dict]:
    """Download audio with yt-dlp → source.wav (optionally time-clipped)."""
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("yt-dlp not installed in venv — pip install yt-dlp") from exc

    raw_tmpl = str(out_dir / "yt_raw.%(ext)s")
    ydl_opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": raw_tmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 120,
    }
    if start is not None and end is not None and end > start:
        ydl_opts["download_ranges"] = (
            lambda _info, _ydl: [{"start_time": start, "end_time": end}]
        )
        ydl_opts["force_keyframes_at_cuts"] = True
        clip_via_ffmpeg = False
    else:
        clip_via_ffmpeg = start is not None or end is not None

    meta: dict[str, Any] = {"url": url}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        meta["title"] = info.get("title")
        meta["id"] = info.get("id")
        meta["duration"] = info.get("duration")
        requested = ydl.prepare_filename(info)

    raw = _pick_raw_download(out_dir, requested)
    wav = out_dir / "source.wav"
    if clip_via_ffmpeg:
        stage_listenable_copy(raw, wav, start, end)
    else:
        stage_listenable_copy(raw, wav)
    try:
        raw.unlink()
    except OSError:
        pass
    return wav, meta


def prepare_local(
    path: str, out_dir: Path, start: float | None, end: float | None
) -> Path:
    wav = out_dir / "source.wav"
    stage_listenable_copy(Path(path), wav, start, end)
    return wav


def _validate_stem_files(stem_files: dict[str, str] | None) -> list[tuple[str, Path]]:
    """Validate caller-supplied stems without placing their paths in artifacts."""
    if stem_files is None:
        return []
    if not isinstance(stem_files, dict):
        raise ValueError("stem_files must be a role-to-path object")
    validated: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for raw_role, raw_path in stem_files.items():
        if not isinstance(raw_role, str):
            raise ValueError("stem roles must be strings")
        role = raw_role.strip().lower()
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", role) is None:
            raise ValueError("stem roles must use 1-32 lowercase letters, digits, _ or -")
        if role in seen:
            raise ValueError("stem roles must be unique")
        if not isinstance(raw_path, str) or not raw_path.strip() or raw_path.startswith("\\\\"):
            raise ValueError("each stem must be an explicit local file path")
        try:
            path = Path(raw_path).expanduser().resolve()
        except (OSError, ValueError) as exc:
            raise ValueError("stem %r is not a readable local file" % role) from exc
        if not path.is_file():
            raise ValueError("stem %r is not a readable local file" % role)
        seen.add(role)
        validated.append((role, path))
    return validated


def _ingest_stems(validated: list[tuple[str, Path]], out_dir: Path) -> list[dict[str, Any]]:
    """Normalize validated stems into the contained reference artifact."""
    if not validated:
        return []
    stem_dir = ensure_out_dir_contained(out_dir) / "stems"
    stem_dir.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[str, Path, Path]] = []
    try:
        for role, source_path in validated:
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{role}.", suffix=".wav", dir=str(stem_dir))
            os.close(fd)
            tmp_path = Path(tmp_name)
            try:
                ffmpeg_to_wav(source_path, tmp_path, None, None)
            except Exception as exc:
                try:
                    tmp_path.unlink()
                except FileNotFoundError:
                    pass
                raise ValueError("stem %r could not be decoded" % role) from exc
            staged.append((role, tmp_path, stem_dir / f"{role}.wav"))
        for _role, tmp_path, target in staged:
            os.replace(tmp_path, target)
        return [{
            "role": role,
            "file": f"stems/{role}.wav",
            "size_bytes": target.stat().st_size,
        } for role, _tmp, target in staged]
    finally:
        for _role, tmp_path, _target in staged:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def measure_drums_grid(wav_path: Path, tempo_bpm: float) -> dict[str, Any] | None:
    """Derive deterministic rhythmic-phase evidence from a mono PCM drums stem.

    The detector measures positive short-window energy changes, suppresses nearby
    duplicates, and snaps accepted onsets to sixteenth notes. It deliberately
    makes no claim about drum identity or transcription accuracy.
    """
    if (isinstance(tempo_bpm, bool) or not isinstance(tempo_bpm, (int, float))
            or not math.isfinite(float(tempo_bpm)) or not 20 <= float(tempo_bpm) <= 400):
        return None
    with wave.open(str(wav_path), "rb") as reader:
        if reader.getnchannels() != 1 or reader.getsampwidth() != 2:
            raise ValueError("drums artifact must be mono 16-bit PCM")
        sample_rate = reader.getframerate()
        if sample_rate <= 0:
            raise ValueError("drums artifact sample rate is invalid")
        window = max(128, min(2048, sample_rate // 40))
        energies: list[float] = []
        while True:
            raw = reader.readframes(window)
            if len(raw) < window:
                break
            frame = array.array("h")
            frame.frombytes(raw)
            if sys.byteorder != "little":
                frame.byteswap()
            energies.append(math.sqrt(
                sum(value * value for value in frame) / len(frame)))
    if len(energies) < 3:
        return None
    flux = [0.0] + [max(0.0, value - energies[i - 1])
                    for i, value in enumerate(energies[1:], start=1)]
    peak_flux = max(flux)
    if peak_flux <= 0:
        return None
    ordered = sorted(flux)
    median = ordered[len(ordered) // 2]
    deviations = sorted(abs(value - median) for value in flux)
    mad = deviations[len(deviations) // 2]
    threshold = max(median + 3.0 * mad, peak_flux * 0.12)
    candidates = [i for i in range(1, len(flux) - 1)
                  if flux[i] >= threshold and flux[i] >= flux[i - 1]
                  and flux[i] > flux[i + 1]]
    minimum_gap_s = max(0.05, (60.0 / float(tempo_bpm) / 4.0) * 0.4)
    minimum_gap_frames = max(1, round(minimum_gap_s * sample_rate / window))
    accepted: list[int] = []
    for index in sorted(candidates, key=lambda item: (-flux[item], item)):
        if all(abs(index - previous) > minimum_gap_frames for previous in accepted):
            accepted.append(index)
    accepted.sort()
    if not accepted:
        return None
    sixteenth_s = 60.0 / float(tempo_bpm) / 4.0
    absolute_steps = sorted({round((index * window / sample_rate) / sixteenth_s)
                             for index in accepted})
    phase_steps = sorted({step % 16 for step in absolute_steps})
    if not phase_steps:
        return None
    return {
        "version": 1,
        "method": "mono_energy_flux",
        "tempo_bpm": float(tempo_bpm),
        "grid": "sixteenth",
        "steps": phase_steps,
        "accepted_onsets": len(absolute_steps),
        "evidence_only": True,
        "transcription": False,
    }


def _read_midi_notes(mid_path: str | Path) -> list[tuple[float, int, int]]:
    """A stem midi -> [(start_s, pitch, velocity)]. pretty_midi, already in
    the venv via basic_pitch. Raises on unreadable."""
    import pretty_midi

    pm = pretty_midi.PrettyMIDI(str(mid_path))
    out: list[tuple[float, int, int]] = []
    for inst in pm.instruments:
        for note in inst.notes:
            out.append((float(note.start), int(note.pitch),
                        int(note.velocity)))
    return out


_MIDI_PC_NAMES = ("C", "C#", "D", "D#", "E", "F",
                  "F#", "G", "G#", "A", "A#", "B")
_MIDI_PC_FLAT_ALIAS = {"DB": "C#", "EB": "D#", "GB": "F#",
                       "AB": "G#", "BB": "A#"}


def normalize_pitch_class(name: Any) -> str | None:
    """A pitch-class spelling -> sharp name (C..B), or None when unusable."""
    if not isinstance(name, str):
        return None
    key = name.strip().upper()
    if key in _MIDI_PC_NAMES:
        return key
    return _MIDI_PC_FLAT_ALIAS.get(key)


def midi_pitch_classes(mid_path: str | Path) -> list[str] | None:
    """Pitch-class vocabulary of a MIDI file, sharp names, most notes first.

    M5: ref.mid is used ONLY as a pitch-class cross-check in the
    reconciler — this is that vocabulary. None when the file is
    unreadable; [] when it holds no notes."""
    import mido

    try:
        mid = mido.MidiFile(str(mid_path))
    except Exception:
        return None
    counts: dict[str, int] = {}
    first: dict[str, int] = {}
    order = 0
    for track in mid.tracks:
        for msg in track:
            if msg.type != "note_on" or msg.velocity == 0:
                continue
            name = _MIDI_PC_NAMES[int(msg.note) % 12]
            counts[name] = counts.get(name, 0) + 1
            if name not in first:
                first[name] = order
                order += 1
    return sorted(counts, key=lambda n: (-counts[n], first[n]))


def resolve_tempo_octave(measured_bpm: Any,
                         card_bpm: Any, tol: float = 0.04) -> float | None:
    """Octave-resolve a measured tempo against the card's heard tempo.

    When the measured tempo agrees within ``tol`` with the card tempo at
    any octave (card/2, card, 2·card), the CARD tempo is used exactly: a
    slightly-off measurement folded over minutes drifts whole steps, while
    the heard tempo does not, and a half/double-time measurement is an
    octave error on the same grid. No agreement (or no usable tempo on
    either side) leaves the measured tempo untouched; an unusable measured
    tempo returns None.
    """
    if (isinstance(measured_bpm, bool)
            or not isinstance(measured_bpm, (int, float))):
        return None
    measured = float(measured_bpm)
    if not math.isfinite(measured) or not 20 <= measured <= 400:
        return None
    try:
        card = float(card_bpm)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return measured
    if (isinstance(card_bpm, bool) or not math.isfinite(card)
            or card <= 0):
        return measured
    for target in (card, card / 2.0, card * 2.0):
        if abs(measured - target) / target <= tol:
            return card
    return measured


def _bp_tempo(measured_bpm: Any, fallback_tempo: Any) -> float:
    """Basic Pitch tempo: measured, octave-resolved against the fallback.

    M5: non-stem runs never transcribe at the caller's raw guess when a
    measurement exists — the measured tempo wins, snapped to the
    fallback's octave when they agree."""
    try:
        fallback = float(fallback_tempo)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        fallback = 120.0
    if not math.isfinite(fallback) or fallback <= 0:
        fallback = 120.0
    return resolve_tempo_octave(measured_bpm, fallback) or fallback


def _melodic_winner(
    stem_midi: dict[str, dict[str, Any]],
) -> str | None:
    """Winning stem role for ref.mid: most notes among MELODIC stems.

    M5: the drums stem never becomes ref.mid — melodic consumers
    (apply_audio_ref) would otherwise read drum hits as pitched lines.
    The drums midi stays in stem_midi for the measured grid; it just
    cannot win."""
    ranked = sorted(
        ((e.get("note_count") or 0, r)
         for r, e in (stem_midi or {}).items()
         if e.get("midi") and r != "drums"),
        reverse=True)
    return ranked[0][1] if ranked else None


def _phase_step_counts(notes: list[tuple[float, int, int]],
                       tempo_bpm: float) -> tuple[dict[tuple[int, int], int], int]:
    """Onsets -> ({(pitch, step): bars_hit}, n_bars).

    Steps are sixteenths folded % 16 relative to the first strong downbeat
    (the earliest onset at or above the median velocity — the kick, not a
    ghost hat or pickup — else the earliest onset). Each (pitch, step)
    counts once per bar it fires in, so a one-off fill washes out over a
    long file while the loop's own steps survive.
    """
    sixteenth_s = 60.0 / float(tempo_bpm) / 4.0
    bar_s = sixteenth_s * 16.0
    vels = sorted(vel for _, _, vel in notes)
    median_vel = vels[len(vels) // 2]
    strong = [start for start, _, vel in notes if vel >= median_vel]
    anchor = min(strong) if strong else min(start for start, _, _ in notes)
    span = max(start for start, _, _ in notes) - anchor
    n_bars = max(1, int(math.floor(span / bar_s)) + 1)
    hits: dict[tuple[int, int], set[int]] = {}
    for start, pitch, _ in notes:
        if start < anchor:
            continue
        step = int(round((start - anchor) / sixteenth_s)) % 16
        bar = int(math.floor((start - anchor) / bar_s))
        hits.setdefault((pitch, step), set()).add(bar)
    return {key: len(bars) for key, bars in hits.items()}, n_bars


def midi_onset_phase_steps(mid_path: str | Path,
                           tempo_bpm: float,
                           card_tempo_bpm: Any = None) -> list[int] | None:
    """All note onsets of one stem midi, snapped to sixteenth phase steps
    (0-15) at the octave-resolved tempo. Same step semantics as the
    audio-onset path, so either can feed meta measured_drum_grid. A step
    survives only when hit in at least half the bars."""
    tempo = resolve_tempo_octave(tempo_bpm, card_tempo_bpm) \
        if card_tempo_bpm is not None else tempo_bpm
    if (isinstance(tempo, bool) or not isinstance(tempo, (int, float))
            or not math.isfinite(float(tempo))
            or not 20 <= float(tempo) <= 400):
        return None
    try:
        notes = _read_midi_notes(mid_path)
    except Exception:
        return None
    if not notes:
        return None
    counts, n_bars = _phase_step_counts(notes, float(tempo))
    steps = sorted({step for (_, step), hit in counts.items()
                    if hit / n_bars >= 0.5})
    return steps or None


def midi_drum_lanes(mid_path: str | Path,
                    tempo_bpm: float,
                    card_tempo_bpm: Any = None) -> list[dict[str, Any]] | None:
    """A drums stem midi -> measured lanes [{instrument, steps}].

    Only General MIDI drum pitches map to a lane; anything else is not a
    drum claim. Phase-folded like the audio path and aligned to the first
    strong downbeat: a loop's bar, not the arrangement. A step survives
    only when hit in at least half the bars. These lanes are MEASURED
    evidence (measured_drum_grid) — they never go into the model's card.
    """
    tempo = resolve_tempo_octave(tempo_bpm, card_tempo_bpm) \
        if card_tempo_bpm is not None else tempo_bpm
    if (isinstance(tempo, bool) or not isinstance(tempo, (int, float))
            or not math.isfinite(float(tempo))
            or not 20 <= float(tempo) <= 400):
        return None
    try:
        notes = _read_midi_notes(mid_path)
    except Exception:
        return None
    if not notes:
        return None
    counts, n_bars = _phase_step_counts(notes, float(tempo))
    lanes: dict[str, set[int]] = {}
    for (pitch, step), hit in counts.items():
        if hit / n_bars < 0.5:
            continue
        name = _GM_PITCH_TO_DRUM.get(int(pitch))
        if name is None:
            continue
        lanes.setdefault(name, set()).add(step)
    out = [{"instrument": name, "steps": sorted(steps)}
           for name, steps in sorted(lanes.items())][:10]
    return out or None


def measured_drum_grid_to_notes(grid: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Convert measured phase evidence to the existing one-bar retrieval query."""
    understood = {"source": "measured_drums_stem", "method": None, "steps": []}
    if not isinstance(grid, dict) or grid.get("evidence_only") is not True:
        return [], understood
    steps = grid.get("steps")
    tempo = grid.get("tempo_bpm")
    if (not isinstance(steps, list) or not steps or isinstance(tempo, bool)
            or not isinstance(tempo, (int, float)) or not math.isfinite(float(tempo))
            or not 20 <= float(tempo) <= 400):
        return [], understood
    if any(isinstance(step, bool) or not isinstance(step, int) or not 0 <= step < 16
           for step in steps):
        return [], understood
    unique = sorted(set(steps))
    understood.update({"method": grid.get("method"), "steps": unique})
    return [{"pitch": 39, "start": step * 0.25, "duration": 0.25,
             "velocity": 90} for step in unique], understood


def note_features(note_events: list) -> dict[str, Any]:
    """note_events: list of (start, end, pitch, amp, bends).

    No tempo guess here (M5): the IOI-derived rough tempo folded octave
    errors into features, while Basic Pitch takes its tempo from
    measurement (octave-resolved). Count, span and pitch range only.
    """
    if not note_events:
        return {
            "note_count": 0,
            "pitch_min": None,
            "pitch_max": None,
        }
    pitches = [int(n[2]) for n in note_events]
    starts = sorted(float(n[0]) for n in note_events)
    return {
        "note_count": len(note_events),
        "pitch_min": min(pitches),
        "pitch_max": max(pitches),
        "duration_span_s": round(
            max(float(n[1]) for n in note_events) - min(starts), 3
        ),
    }


# Krumhansl major/minor profiles, C-rooted. Correlated against the measured
# chroma vector at all 12 rotations; the winner names the key candidate.
_KRUMHANSL_MAJOR = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39,
                    3.66, 2.29, 2.88)
_KRUMHANSL_MINOR = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98,
                    2.69, 3.34, 3.17)
_PC_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

_MEASURE_SR = 22050
_MEASURE_HOP = 512


def _read_mono_22050(wav: str | Path) -> tuple[Any, int, float]:
    """Audio file -> (mono float64 @22050, sr, duration_s). Raises on unreadable."""
    import numpy as np
    import soundfile as sf

    y, native = sf.read(str(wav), always_2d=False)
    y = np.asarray(y, dtype=np.float64)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if not y.size:
        raise ValueError("empty audio")
    if int(native) != _MEASURE_SR:
        from scipy import signal
        n = max(1, int(round(y.shape[0] * _MEASURE_SR / float(native))))
        y = np.asarray(signal.resample(y, n), dtype=np.float64)
    return y, _MEASURE_SR, y.shape[0] / float(_MEASURE_SR)


def _frame_rms(y: Any, hop: int) -> Any:
    """Per-hop RMS energy."""
    import numpy as np

    n = max(1, y.shape[0] // hop)
    frames = y[: n * hop].reshape(n, hop)
    return np.sqrt((frames ** 2).mean(axis=1) + 1e-12)


def _parabolic_peak(xs: Any, i: int) -> float:
    """Sub-frame peak offset for an autocorrelation maximum."""
    a, b, c = float(xs[i - 1]), float(xs[i]), float(xs[i + 1])
    denom = a - 2 * b + c
    if denom == 0:
        return float(i)
    return float(i) + 0.5 * (a - c) / denom


def _estimate_tempo(onset: Any, fps: float) -> float | None:
    """Onset-envelope autocorrelation in the 50–220 BPM band, parabolic peak."""
    import numpy as np

    o = np.asarray(onset, dtype=np.float64)
    o = o - o.mean()
    if not np.any(o) or o.shape[0] < 8:
        return None
    full = np.correlate(o, o, mode="full")[len(o) - 1:]
    if full[0] <= 0:
        return None
    full = full / full[0]
    lo = max(2, int(round(fps * 60.0 / 220.0)))
    hi = min(len(full) - 2, int(round(fps * 60.0 / 50.0)))
    if hi <= lo:
        return None
    band = full[lo:hi + 1]
    i = int(np.argmax(band)) + lo
    if i <= 0:
        return None
    # Octave preference: frame straddle can halve the true lag's
    # correlation (impulse onsets alternate across frame edges while even
    # multiples realign), so the peak often sits an octave low. Walk down
    # while the halved lag keeps half the peak: a sparse ballad's half-lag
    # aligns onsets with silence and stays put, while clicks on every beat
    # read as the grid, not half-time. Consumers (Basic Pitch tempo, the
    # reconciler) are octave-tolerant by design.
    peak = float(full[i])
    while True:
        j = int(round(i / 2.0))
        if j < lo or j < 1 or i <= 1:
            break
        if full[j] < 0.5 * peak:
            break
        if fps * 60.0 / j > 220.0:
            break
        i = j
    lag = _parabolic_peak(full, i)
    if lag <= 0:
        return None
    bpm = fps * 60.0 / lag
    return round(float(bpm), 2) if 50.0 <= bpm <= 220.0 else None


def _chroma_key(y: Any, sr: int) -> dict[str, Any]:
    """Krumhansl key candidate from summed STFT chroma. Root + mode + margin."""
    import numpy as np
    from scipy import signal

    seg = min(8192, max(2048, (len(y) // 64) | 1))
    _, _, spec = signal.stft(y, fs=sr, window="hann", nperseg=seg,
                             noverlap=seg * 3 // 4, boundary=None)
    mag = np.abs(spec)
    freqs = np.fft.rfftfreq(seg, 1.0 / sr)
    chroma = np.zeros(12)
    for k in range(1, len(freqs)):
        f = freqs[k]
        if f < 55 or f > 4200:
            continue
        pc = (int(round(12 * np.log2(f / 440.0))) + 9) % 12
        chroma[pc] += float((mag[k] ** 2).sum())
    if chroma.sum() <= 0:
        return {"key": None, "strength": 0.0}
    chroma = chroma / chroma.sum()
    best: tuple[float, str] = (-2.0, "")
    scored: list[float] = []
    for root in range(12):
        for mode, prof in (("major", _KRUMHANSL_MAJOR),
                           ("minor", _KRUMHANSL_MINOR)):
            template = np.array([prof[(i - root) % 12] for i in range(12)])
            t = template - template.mean()
            c = chroma - chroma.mean()
            denom = float(np.sqrt((t ** 2).sum() * (c ** 2).sum()))
            r = float((t * c).sum() / denom) if denom > 0 else 0.0
            scored.append(r)
            if r > best[0]:
                best = (r, "%s %s" % (_PC_NAMES[root], mode))
    scored.sort(reverse=True)
    margin = max(0.0, scored[0] - (scored[1] if len(scored) > 1 else 0.0))
    return {"key": best[1] or None, "strength": round(margin, 3)}


def measure_audio(wav: str | Path) -> dict[str, Any]:
    """Local DSP first listen: tempo + beat grid, per-bar RMS energy, novelty
    section boundaries, chroma key candidate. numpy/scipy/soundfile only.

    Raises on unreadable audio; silence yields Nones, never guesses. Times in
    seconds, energy linear RMS. Sections are LOUDNESS boundaries — the prompt
    offers them as change points, never as musical claims.
    """
    import numpy as np

    y, sr, duration_s = _read_mono_22050(wav)
    hop = _MEASURE_HOP
    fps = sr / hop
    rms = _frame_rms(y, hop)
    if float(rms.max()) <= 1e-9:
        return {"tempo_bpm": None, "beat_times_s": [], "beat_period_s": None,
                "bar_len_s": None, "energy_per_bar": [],
                "novelty_boundaries_s": [],
                "chroma_key": {"key": None, "strength": 0.0},
                "duration_s": round(duration_s, 3)}

    log_e = np.log1p(100.0 * rms)
    onset = np.maximum(0.0, np.diff(log_e, prepend=log_e[0]))
    tempo = _estimate_tempo(onset, fps)

    beat_times: list[float] = []
    period: float | None = None
    if tempo:
        period = 60.0 / tempo
        window = max(2, int(round(2 * period * fps)))
        phase_i = int(np.argmax(onset[:window]))
        phase = phase_i * hop / sr
        t = phase
        while t < duration_s:
            beat_times.append(round(t, 3))
            t += period

    bar_len = 4 * period if period else 4.0
    n_bars = max(1, int(duration_s / bar_len) + 1)
    energy: list[float] = []
    for b in range(n_bars):
        lo, hi = int(b * bar_len * fps), int(min(rms.shape[0], (b + 1) * bar_len * fps))
        if hi <= lo:
            break
        energy.append(round(float(rms[lo:hi].mean()), 6))
    energy_t = [round(b * bar_len, 3) for b in range(len(energy))]

    chunk_s = 1.5
    edges = np.arange(0.0, duration_s, chunk_s).tolist() + [duration_s]
    chunk_e = np.array([
        rms[int(edges[i] * fps):int(min(rms.shape[0], edges[i + 1] * fps))].mean()
        if int(edges[i + 1] * fps) > int(edges[i] * fps) else 0.0
        for i in range(len(edges) - 1)])
    novelty = np.abs(np.diff(np.log1p(100.0 * chunk_e)))
    boundaries: list[float] = []
    if novelty.size:
        # Adaptive, with an absolute floor: flat tracks must yield nothing,
        # a real level jump must clear even a two-chunk comparison.
        thresh = max(float(np.median(novelty) + 1.0 * novelty.std()), 0.35)
        min_gap = 12.0
        for i in range(len(novelty)):
            t = round(edges[i + 1], 3)
            if novelty[i] >= thresh and all(abs(t - b) >= min_gap for b in boundaries):
                boundaries.append(t)
                if len(boundaries) >= 16:
                    break

    try:
        key = _chroma_key(y, sr)
    except Exception:
        key = {"key": None, "strength": 0.0}
    return {
        "tempo_bpm": tempo,
        "beat_times_s": beat_times,
        "beat_period_s": round(period, 4) if period else None,
        "bar_len_s": round(bar_len, 4),
        "energy_per_bar": energy,
        "energy_bar_start_s": energy_t,
        "novelty_boundaries_s": boundaries,
        "chroma_key": key,
        "duration_s": round(duration_s, 3),
    }


def measured_prompt_note(measured: dict[str, Any] | None) -> str:
    """Measured boundaries as loudness points for a blind listen. The prompt
    may name where the LEVEL jumps, never what it means musically — and it
    never sees the measured tempo or key."""
    bounds = (measured or {}).get("novelty_boundaries_s") or []
    if not bounds:
        return ""
    pts = ", ".join(format_mmss(float(b)) for b in bounds[:8])
    return ("Loudness change points (NOT musical claims — a level jump is "
            f"not a section): {pts}.\n")


def _tempo_agree(measured_bpm: float, card_bpm: float) -> tuple[bool, str | None]:
    """Octave-aware tempo agreement: one rule shared with the grid builder.

    Agrees when the measurement is within ±4% of the card tempo at any
    octave (card/2, card, 2·card); only a non-card-octave match notes
    "octave-equivalent".
    """
    m, c = _num(measured_bpm), _num(card_bpm)
    if not m or not c:
        return False, None
    if resolve_tempo_octave(m, c, tol=0.04) != c:
        return False, None
    note = None if abs(m - c) / c <= 0.04 else "octave-equivalent"
    return True, note


_KEY_TONIC_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def _key_parts(key: Any) -> tuple[int | None, str | None]:
    """A key string -> (tonic pitch class, mode or None).

    "D minor" -> (2, "minor"); "D" or "Dm" -> (2, ...); an unrecognized
    mode word reads as unknown, not as disagreement. Unparseable -> (None,
    None), which the reconciler reports as model_only.
    """
    s = str(key or "").strip()
    if not s:
        return None, None
    m = re.match(r"^([A-Ga-g])\s*([#b\u266f\u266d]?)\s*(.*)$", s)
    if not m:
        return None, None
    tonic = m.group(1).upper()
    acc = m.group(2).replace("\u266f", "#").replace("\u266d", "b")
    pc = (_KEY_TONIC_PC[tonic] + (1 if acc == "#" else -1 if acc == "b" else 0)) % 12
    rest = m.group(3).strip().lower()
    mode: str | None = None
    if rest:
        if rest.startswith("min") or rest == "m":
            mode = "minor"
        elif rest.startswith("maj") or rest == "M":
            mode = "major"
    return pc, mode


def _keys_agree(key_m: Any, key_c: Any) -> tuple[str, str | None]:
    """Key agreement by pitch class + mode where known.

    Same tonic with both modes known and equal: agree. Same tonic with a
    mode missing on either side ("D minor" vs "D"): agree on the tonic,
    mode unknown. Anything else with both tonics known: disagree. A side
    with no usable tonic: model_only.
    """
    mt, mm = _key_parts(key_m)
    ct, cm = _key_parts(key_c)
    if mt is None or ct is None:
        return "model_only", None
    if mt == ct and (mm == cm or mm is None or cm is None):
        note = (None if mm == cm
                else "tonic agrees; mode unknown on one side")
        return "agree", note
    return "disagree", None


def _reconciliation_uncertain_flags(meta: dict[str, Any] | None) -> list[str]:
    """Stored reconciler disagreements as uncertain-view flags.

    Flag, never override: the card keeps its heard values and the brief /
    search uncertain view gains the disagreement. Idempotent strings, so a
    reindex that rebuilds from the same meta never duplicates them.
    """
    rec = (meta or {}).get("reconciliation") or {}
    if not isinstance(rec, dict):
        return []
    flags: list[str] = []
    tempo = rec.get("tempo") or {}
    if isinstance(tempo, dict) and tempo.get("status") == "disagree":
        m = _num(tempo.get("measured_bpm"))
        g = _num(tempo.get("card_grid_bpm"))
        f = _num(tempo.get("card_feel_bpm"))
        flags.append(
            "tempo disagreement: measured %s BPM vs card grid %s / feel %s"
            " — card value kept"
            % (("%g" % m) if m is not None else "?",
               ("%g" % g) if g is not None else "?",
               ("%g" % f) if f is not None else "?"))
    key = rec.get("key") or {}
    if isinstance(key, dict) and key.get("status") == "disagree":
        flags.append(
            "key disagreement: measured %s vs card %s — card value kept"
            % (key.get("measured") or "?", key.get("card") or "?"))
    return flags


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    import math
    value = float(value)
    return value if math.isfinite(value) else None


# Reconciler claim sets per focus: only the focused claims stay audible.
# A claim the local DSP cannot check (swing, drum grid detail, register,
# phrase, range) still reports, as model_only with the card's value —
# audible, but marked as the model's word alone.
_FOCUS_CLAIMS: dict[str, tuple[str, ...]] = {
    "groove": ("tempo", "swing", "drums", "sections"),
    "harmony": ("key", "midipitch", "register"),
    "melody": ("phrase", "range"),
    "full": ("tempo", "swing", "drums", "key", "midipitch", "register",
             "phrase", "range", "sections"),
}


def reconcile_measured(measured: dict[str, Any] | None,
                       card: dict[str, Any] | None,
                       focus: str = "full",
                       midi_pitch_classes: list[str] | None = None
                       ) -> dict[str, Any]:
    """Measured DSP vs the model's card, claim by claim. Pure.

    Each claim reads agree | disagree | model_only, where model_only means
    no independent check was possible (no measurement, or the card left the
    field empty). Only the FOCUSED claims are returned — a groove listen
    says nothing about the key, a harmony listen nothing about the grid.
    The reconciled tempo and the energy curve come from measurement; a key
    disagreement is flagged with both values reported — never silently
    resolved. ref.mid joins as a pitch-class cross-check (M5): the
    transcription supports the card's pitch vocabulary, or the claim says
    which classes it never heard.
    """
    focus_n = normalize_focus(focus)
    measured = measured or {}
    card = card or {}
    tempo_m = _num(measured.get("tempo_bpm"))
    ctempo = card.get("tempo") or {}
    card_rates = [b for b in (_num(ctempo.get("grid_bpm")),
                             _num(ctempo.get("feel_bpm"))) if b]
    if tempo_m and card_rates:
        hits = [_tempo_agree(tempo_m, b) for b in card_rates]
        agree = next((h for h in hits if h[0]), None)
        tempo_status = "agree" if agree else "disagree"
        tempo_note = agree[1] if agree else None
    else:
        tempo_status, tempo_note = "model_only", None
    uses_bpm = tempo_m or (card_rates[0] if card_rates else None)

    key_m = (measured.get("chroma_key") or {}).get("key")
    key_c = (card.get("harmony") or {}).get("modal_center")
    if key_m and key_c:
        key_status, key_note = _keys_agree(key_m, key_c)
    else:
        key_status, key_note = "model_only", None

    bounds = measured.get("novelty_boundaries_s")
    card_sections = card.get("sections") or []
    if not isinstance(bounds, list) or not measured:
        sections_status = "model_only"
        segments: int | None = None
    else:
        segments = len(bounds) + 1
        sections_status = ("agree" if abs(len(card_sections) - segments) <= 1
                           else "disagree")

    cswing = card.get("swing") or {}
    cdrums = card.get("drums") or {}
    creg = card.get("register") or {}
    cmel = card.get("melody") or {}
    charmony = card.get("harmony") or {}
    mid_raw = ([normalize_pitch_class(p) for p in (midi_pitch_classes or [])]
               if midi_pitch_classes is not None else None)
    mid_pcs = sorted({p for p in (mid_raw or []) if p}) if mid_raw else mid_raw
    card_pcs = [p for p in (normalize_pitch_class(p)
                            for p in (charmony.get("pitch_classes") or []))
                if p]
    if mid_pcs is not None and card_pcs:
        mid_set = set(mid_pcs)
        overlap = [p for p in card_pcs if p in mid_set]
        recall = len(overlap) / len(card_pcs)
        if recall >= 0.5:
            midi_status: str = "agree"
            midi_note: str | None = (
                "ref.mid covers %d/%d card pitch classes (%s)"
                % (len(overlap), len(card_pcs), ",".join(overlap)))
        else:
            midi_status = "disagree"
            missing = [p for p in card_pcs if p not in mid_set]
            midi_note = ("ref.mid never plays %s (card claims %s; heard %s)"
                         % (",".join(missing), ",".join(card_pcs),
                            ",".join(mid_pcs) if mid_pcs else "nothing"))
    else:
        midi_status, midi_note, overlap = "model_only", None, []
    claims: dict[str, dict[str, Any]] = {
        "tempo": {"status": tempo_status,
                  "measured_bpm": tempo_m,
                  "card_grid_bpm": _num(ctempo.get("grid_bpm")),
                  "card_feel_bpm": _num(ctempo.get("feel_bpm")),
                  "uses_bpm": uses_bpm,
                  "note": tempo_note},
        "swing": {"status": "model_only",
                  "card_pct": _num(cswing.get("pct")),
                  "card_confidence": cswing.get("confidence")},
        "drums": {"status": "model_only",
                  "card_instruments": sorted({
                      str(lane.get("instrument") or "")
                      for lane in cdrums.get("grid") or []} - {""}),
                  "card_confidence": cdrums.get("confidence")},
        "key": {"status": key_status,
                "measured": key_m,
                "card": key_c,
                "note": key_note,
                "flagged": key_status == "disagree"},
        "midipitch": {"status": midi_status,
                      "mid_pitch_classes": mid_pcs,
                      "card_pitch_classes": card_pcs,
                      "overlap": overlap,
                      "note": midi_note,
                      "flagged": midi_status == "disagree"},
        "register": {"status": "model_only",
                     "card_band": creg.get("band"),
                     "card_pitch_span": creg.get("pitch_span")},
        "phrase": {"status": "model_only",
                   "card_phrase_length_bars": _num(cmel.get("phrase_length_bars")),
                   "card_repetition": cmel.get("repetition")},
        "range": {"status": "model_only",
                  "card_pitch_span": creg.get("pitch_span")},
        "sections": {"status": sections_status,
                     "measured_segments": segments,
                     "card_sections": len(card_sections)},
    }
    out: dict[str, Any] = {"focus": focus_n}
    for name in _FOCUS_CLAIMS[focus_n]:
        out[name] = claims[name]
    if focus_n in ("groove", "full"):
        out["energy_per_bar"] = measured.get("energy_per_bar")
        out["bar_len_s"] = measured.get("bar_len_s")
    return out


def run_basic_pitch(wav: Path, midi_path: Path, tempo: float = 120.0) -> dict[str, Any]:
    from basic_pitch import ICASSP_2022_MODEL_PATH
    from basic_pitch.inference import predict

    model_path = Path(ICASSP_2022_MODEL_PATH)
    onnx = model_path.parent / "nmp.onnx"
    if onnx.is_file():
        model_path = onnx

    _model_out, midi_data, note_events = predict(
        str(wav),
        str(model_path),
        midi_tempo=tempo,
    )
    midi_data.write(str(midi_path))
    stats = note_features(list(note_events))
    stats["model_path_used"] = str(model_path)
    return stats


def _moment_seconds(match: re.Match[str]) -> float:
    if match.group("h") is not None:
        return (
            int(match.group("h")) * 3600
            + int(match.group("m")) * 60
            + float(match.group("s"))
        )
    return int(match.group("m2")) * 60 + float(match.group("s2"))


def extract_moment_timestamps(text: str) -> list[float]:
    """Seconds for timestamps found under # Moments (best-effort)."""
    lines = text.splitlines()
    in_moments = False
    stamps: list[float] = []
    for line in lines:
        if line.startswith("# "):
            in_moments = line.strip().lower() == "# moments"
            continue
        if not in_moments:
            continue
        m = _MOMENT_TS_RE.search(line)
        if m:
            stamps.append(_moment_seconds(m))
    return stamps


def validate_moments(
    text: str, duration_s: float, slack_s: float = 2.0
) -> dict[str, Any]:
    stamps = extract_moment_timestamps(text)
    if not stamps:
        return {
            "ok": True,
            "stamp_count": 0,
            "out_of_range": [],
            "warning": "no moment timestamps found",
        }
    limit = float(duration_s) + slack_s
    bad = [s for s in stamps if s > limit]
    return {
        "ok": len(bad) < len(stamps),  # soft-fail only if ALL out of range
        "all_out_of_range": bool(bad) and len(bad) == len(stamps),
        "stamp_count": len(stamps),
        "out_of_range": [round(s, 2) for s in bad],
        "duration_s": duration_s,
    }


def _section_body(text: str, heading: str) -> str:
    lines = text.splitlines()
    want = heading.strip().lower()
    capturing = False
    body: list[str] = []
    for line in lines:
        if line.startswith("# "):
            if capturing:
                break
            capturing = line.strip().lower() == want
            continue
        if capturing:
            body.append(line)
    return "\n".join(body).strip()


def _bullets(section: str, want_dont: bool = False) -> list[str]:
    out: list[str] = []
    for line in section.splitlines():
        s = line.strip()
        if not s.startswith(("-", "*")):
            # also accept "Do: ..." / "Don't: ..." without a dash
            low0 = s.lower()
            if not (low0.startswith("do:") or low0.startswith("don't:") or low0.startswith("do not:")):
                continue
            s = "- " + s
        body = s.lstrip("-* ").strip()
        low = body.lower()
        if low in ("do:", "don't:", "do not:", "do", "don't"):
            continue
        is_dont = low.startswith(("don't", "do not", "dont"))
        is_do = low.startswith("do:") or (low.startswith("do ") and not is_dont)
        if want_dont:
            if not is_dont:
                continue
        else:
            if is_dont:
                continue
            # keep plain bullets under Do section as do-rules
        # strip leading Do:/Don't:
        for prefix in ("don't:", "do not:", "dont:", "do:"):
            if low.startswith(prefix):
                body = body[len(prefix):].strip()
                break
        if body:
            out.append(body)
    return out[:8]


def parse_describe_card_fields(text: str) -> dict[str, Any]:
    identity = _section_body(text, "# Identity").splitlines()
    identity_line = identity[0].strip() if identity else ""
    rules = _section_body(text, "# Imitation rules")
    do_list = _bullets(rules, want_dont=False)
    dont_list = _bullets(rules, want_dont=True)
    if not dont_list:
        # fallback: lines that start with Don't
        dont_list = [
            s.lstrip("-* ").strip()
            for s in rules.splitlines()
            if s.strip().lower().startswith(("- don't", "* don't", "- do not"))
        ][:8]
    return {
        "identity": identity_line,
        "imitation_do": do_list,
        "imitation_dont": dont_list,
        "describe_excerpt": text[:600],
    }


_BPM_RE = re.compile(
    r"(?:~|≈|about\s+|around\s+)?(\d{2,3})\s*(?:bpm|BPM)\b",
    re.I,
)
_SWING_PCT_RE = re.compile(r"(\d{1,3})\s*%\s*swing", re.I)
_MODAL_RE = re.compile(
    r"\b([A-G](?:#|b)?)\s+"
    r"(Phrygian|Aeolian|Dorian|Mixolydian|Lydian|Ionian|Locrian|minor|major|"
    r"min|maj)\b",
    re.I,
)


def parse_describe_structure(text: str) -> dict[str, Any]:
    """Best-effort structured fields from an existing describe.txt (no re-listen).

    The haystack includes ``# Imitation rules`` (Do-lines only). Moment children
    written by ``clip_moments`` deliberately empty Identity/Groove/Harmony
    ("Parent pocket — see imitation rules") and carry every concrete fact under
    the rules heading, so parsing the first three sections alone left 114 of 185
    refs with no usable structure at all. Don't-lines stay OUT: "Don't: add
    swing" must never be read as a description of the track.
    """
    identity = _section_body(text, "# Identity")
    groove = _section_body(text, "# Groove")
    harmony = _section_body(text, "# Harmony / melody")
    do_rules = "\n".join(_bullets(_section_body(text, "# Imitation rules"),
                                  want_dont=False))
    hay = "\n".join([identity, groove, harmony, do_rules])
    low = hay.lower()

    # Feel vs grid tempo are DIFFERENT numbers and get used for different jobs.
    # A brief saying "~52 BPM (104 BPM half-time) feel" means both: 52 is the
    # pulse he'd tap, 104 is the grid the notes sit on. Anything arithmetic
    # (bar counts, tempo-matched retrieval) must use the grid tempo — using the
    # feel tempo would halve a song's sections. Prose keeps the feel.
    feel_bpm: float | None = None
    grid_bpm: float | None = None
    bpms = [float(m.group(1)) for m in _BPM_RE.finditer(hay)]
    if bpms:
        feel_bpm = min(bpms)
        grid_bpm = max(bpms)

    swing_pct: float | None = None
    sm = _SWING_PCT_RE.search(hay)
    if sm:
        swing_pct = float(sm.group(1))
    elif "0% swing" in low or "straight grid" in low or "no swing" in low:
        swing_pct = 0.0
    elif re.search(r"\bswung\b|\bswing\b", low):
        swing_pct = None  # mentioned but unquantified

    density: str | None = None
    if re.search(
        r"\blow(?:-|\s)?density\b|\blow\b[^.\n]{0,24}\bdensity\b|\bsparse\b|\bminimal dens",
        low,
    ):
        density = "low"
    elif re.search(r"\bhigh(?:-|\s)?density\b|\bbusy\b|\bdense\b", low):
        density = "high"
    elif re.search(r"\bmedium(?:-|\s)?density\b|\bmoderate dens", low):
        density = "medium"

    modal_center: str | None = None
    mm = _MODAL_RE.search(hay)
    if mm:
        modal_center = f"{mm.group(1)} {mm.group(2)}"
    else:
        m2 = re.search(
            r"\b([A-G](?:#|b)?)\s+(?:Phrygian|Aeolian|Dorian|minor|major)\b",
            identity,
            re.I,
        )
        if m2:
            modal_center = m2.group(0)

    register: str | None = None
    if re.search(r"\blow[- ]register\b|\bsub-bass\b|\bbass register\b", low):
        register = "low"
    elif re.search(r"\bhigh[- ]register\b|\bupper register\b|\btop[- ]end\b", low):
        register = "high"
    elif re.search(r"\bmid[- ]register\b|\bmiddle register\b", low):
        register = "mid"

    # Groove bullets first; fall back to the Do-rules, which is where moment
    # children keep their only percussion description.
    drum_lines: list[str] = []
    for source in (groove, do_rules):
        for line in source.splitlines():
            s = line.strip().lstrip("-* ").strip()
            if not s:
                continue
            low_s = s.lower()
            if any(k in low_s for k in
                   ("kick", "snare", "hat", "hi-hat", "ride", "drum", "tom",
                    "clap", "rim", "perc")):
                if s not in drum_lines:
                    drum_lines.append(s)
            if len(drum_lines) >= 2:
                break
        if drum_lines:
            break
    drum_grid = " | ".join(drum_lines) if drum_lines else None

    return {
        "feel_bpm": feel_bpm,
        "grid_bpm": grid_bpm,
        "swing_pct": swing_pct,
        "density": density,
        "modal_center": modal_center,
        "register": register,
        "drum_grid": drum_grid,
    }


def extract_moment_lines(text: str, *, include_sections: bool = True) -> list[dict[str, Any]]:
    """Moments bullets with optional timestamp (seconds) + label text.

    ``include_sections`` also reads a v3 render's ``# Sections``; callers that
    cannot vouch the describe matches a validated card pass False."""
    lines = text.splitlines()
    in_moments = False
    headings = ("# moments", "# sections") if include_sections else ("# moments",)
    out: list[dict[str, Any]] = []
    for line in lines:
        if line.startswith("# "):
            in_moments = line.strip().lower() in headings
            continue
        if not in_moments:
            continue
        s = line.strip()
        if not s.startswith(("-", "*")):
            continue
        body = s.lstrip("-* ").strip()
        if not body:
            continue
        m = _MOMENT_TS_RE.search(body)
        t = _moment_seconds(m) if m else None
        out.append({"t": t, "text": body})
    return out


VERDICT_CHOICES = ("keep", "drop", "unset")
OWNERSHIP_CHOICES = ("his", "other", "unknown")


def normalize_ownership(ownership: str | None) -> str:
    """Normalize the operator-owned authorship field.

    Ownership is deliberately not model output.  It is an operator assertion,
    and ``unknown`` is the safe value when a legacy artifact has no assertion.
    """
    if ownership is None or str(ownership).strip() == "":
        return "unknown"
    value = str(ownership).strip().lower()
    if value not in OWNERSHIP_CHOICES:
        raise ValueError(
            f"ownership must be one of {OWNERSHIP_CHOICES}, got {ownership!r}"
        )
    return value


def _inferred_parent_ref_id(ref_id: str | None) -> str | None:
    """Infer a moment's parent from its stable ``<parent>_mN_<stamp>`` id."""
    rid = str(ref_id or "")
    match = re.match(r"^(.+)_m\d+_\d{8}-\d{6}$", rid)
    return match.group(1) if match else None


def _parent_ref_id(card: dict[str, Any], refs: dict[str, Any]) -> str | None:
    """Resolve parent in contract order: explicit, parent tag, stable id."""
    explicit = card.get("parent_ref_id")
    if explicit and explicit in refs:
        return str(explicit)
    for tag in card.get("tags") or []:
        text = str(tag)
        if text.startswith("parent:") and text[7:] in refs:
            return text[7:]
    inferred = _inferred_parent_ref_id(card.get("ref_id"))
    return inferred if inferred in refs else None


def _suspect_source(card: dict[str, Any], refs: dict[str, Any]) -> dict | None:
    """A moment inherits its parent's quarantine; its own flag still wins."""
    if card.get("suspect") is True:
        return card
    parent = refs.get(_parent_ref_id(card, refs)) or {}
    return parent if parent.get("suspect") is True else None


def _file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def normalize_verdict(verdict: str | None) -> str:
    if verdict is None or str(verdict).strip() == "":
        return "unset"
    v = str(verdict).strip().lower()
    if v not in VERDICT_CHOICES:
        raise ValueError(f"verdict must be one of {VERDICT_CHOICES}, got {verdict!r}")
    return v


# X7: how the listener turn ran. interactive = heard inside the legacy
# shared multi-track Gemini chat conversation (stamped after the fact by
# --stamp-provenance, never assigned by the pipeline); isolated = any card
# produced by the pipeline's per-track listener (CLI, MCP or batch). Runs
# with no listener (moments, midi-only) carry no mode.
LISTENER_MODES = ("interactive", "isolated")


def normalize_listener_mode(listener_mode: str | None) -> str | None:
    """Normalize the listener provenance field. None stays None (a ref that
    never ran a listener has no mode to claim); anything else must be exact."""
    if listener_mode is None or str(listener_mode).strip() == "":
        return None
    value = str(listener_mode).strip().lower()
    if value not in LISTENER_MODES:
        raise ValueError(
            f"listener_mode must be one of {LISTENER_MODES}, got {listener_mode!r}"
        )
    return value


def load_index(path: Path | None = None) -> dict[str, Any]:
    path = path or INDEX_PATH
    if not path.is_file():
        return {"version": 1, "updated_at": None, "refs": {}}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except OSError as exc:
        raise RuntimeError(f"cannot read audio_ref index {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"corrupt audio_ref index {path}: {exc}. "
            "Fix or delete the file, then run --reindex (will not auto-wipe)."
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"corrupt audio_ref index {path}: root must be an object")
    refs = data.get("refs")
    if refs is None:
        data["refs"] = {}
    elif not isinstance(refs, dict):
        raise RuntimeError(f"corrupt audio_ref index {path}: 'refs' must be an object")
    data.setdefault("version", 1)
    return data


def write_json_atomic(path: Path, data: Any) -> Path:
    """Serialize to a sibling temp file, then os.replace — readers see the old
    file or the new one, never a truncated one. Used for the index AND for batch
    reports: a half-written report silently disables --resume (the JSONDecodeError
    is swallowed), which is how a crashed batch turns into duplicate refs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, default=str) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
    return path


def save_index(data: dict[str, Any], path: Path | None = None) -> Path:
    path = path or INDEX_PATH
    data["version"] = int(data.get("version") or 1)
    data["updated_at"] = _utc_now_iso()
    return write_json_atomic(path, data)


def _index_lock(path: Path | None = None):
    """Exclusive advisory lock for an index read-modify-write.

    Same <path>.lock + flock pattern as the ledger: concurrent tally
    writers (judge + sweep) serialize instead of losing counts on a
    stale read. POSIX-only; without fcntl the write proceeds unlocked.
    """
    @contextlib.contextmanager
    def _cm():
        lock_path = str(path or INDEX_PATH) + ".lock"
        try:
            import fcntl
        except ImportError:
            yield
            return
        os.makedirs(os.path.dirname(os.path.abspath(lock_path)),
                    exist_ok=True)
        with open(lock_path, "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return _cm()


def card_from_out_dir(out_dir: Path, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    meta = meta or {}
    if not meta and (out_dir / "meta.json").is_file():
        try:
            meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        if not isinstance(meta, dict):
            # Tolerate corrupt metadata the way an unreadable file already is:
            # one bad meta.json must not break the whole reindex.
            meta = {}
    describe_path = out_dir / "describe.txt"
    # A validated card (v3 or v2) is the canonical representation when it is
    # present and valid.  A malformed/partial card is never allowed to replace
    # a good rendered card; callers can then retain the previous index entry
    # on a failed attempt.
    v2_path = out_dir / "card.json"
    heard_card: dict[str, Any] | None = None
    if v2_path.is_file():
        try:
            candidate = json.loads(v2_path.read_text(encoding="utf-8"))
            heard_card = validate_card(candidate)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid card.json in {out_dir}: {exc}") from exc
    if heard_card is not None:
        yt = meta.get("youtube") or {}
        build = (card_from_v3 if heard_card.get("contract_version")
                 == V3_CONTRACT_VERSION else card_from_v2)
        base = build(
            heard_card,
            ref_id=out_dir.name,
            out_dir=str(out_dir),
            source=meta.get("source"),
            source_kind=meta.get("source_kind"),
            title=yt.get("title") or meta.get("title"),
            start=meta.get("start"),
            end=meta.get("end"),
            duration_s=(meta.get("features") or {}).get("duration_s")
            or meta.get("duration_s"),
            focus=meta.get("focus") or "full",
            prompt_extra=(meta.get("prompt_extra") or "")[:240] or None,
            model=meta.get("agy_model"),
            created_at=meta.get("created_at") or _utc_now_iso(),
            tags=list(meta.get("tags") or []),
            linked_projects=list(meta.get("linked_projects") or []),
            batch_id=meta.get("batch_id"),
            ricardo_note=meta.get("ricardo_note"),
            verdict=normalize_verdict(meta.get("verdict")),
            verdict_at=meta.get("verdict_at"),
            parent_ref_id=meta.get("parent_ref_id"),
            moment_index=meta.get("moment_index"),
            moment_t=meta.get("moment_t"),
            stem_artifacts=meta.get("stem_artifacts"),
            measured_drum_grid=meta.get("measured_drum_grid"),
            ownership=normalize_ownership(meta.get("ownership")),
            listener_mode=normalize_listener_mode(meta.get("listener_mode")),
            exact_listener_model=meta.get("exact_listener_model"),
        )
        # Operator overlays ride in meta.json beside the heard card (same
        # keys as the describe-only branch below): a rebuild must not drop
        # the tally, the suspect flag or the counted entry ids.
        base["judgment_tally"] = ref_tally_of(meta)
        if isinstance(meta.get("tallied_entries"), dict) \
                and meta["tallied_entries"]:
            base["tallied_entries"] = {
                str(eid): str(outcome)
                for eid, outcome in meta["tallied_entries"].items()}
        base["suspect"] = meta.get("suspect") is True
        base["suspect_reason"] = (str(meta.get("suspect_reason") or "").strip()
                                  or None)
        # B3 provenance: only an explicit True counts as verified — every
        # legacy card without the flag reads as unverified, never unknown.
        base["listen_verified"] = meta.get("listen_verified") is True
        base["has_measured"] = isinstance(meta.get("measured"), dict) \
            and bool(meta.get("measured"))
        # X5 Pass B windows ride beside the heard card, like the overlays
        # above: a rebuild must not drop them.
        base["windows"] = (list(meta["windows"])
                           if isinstance(meta.get("windows"), list) else None)
        # Reconciler disagreements surface in the uncertain view (flag, never
        # override): the brief and the compact search row read them here.
        rec_flags = _reconciliation_uncertain_flags(meta)
        if rec_flags:
            seen = {str(u) for u in base.get("uncertain") or []}
            base["uncertain"] = (list(base.get("uncertain") or [])
                                 + [f for f in rec_flags if f not in seen])
        base["card_path"] = str(v2_path)
        return base
    fields: dict[str, Any] = {
        "identity": "",
        "imitation_do": [],
        "imitation_dont": [],
        "describe_excerpt": "",
    }
    structure: dict[str, Any] = {}
    if describe_path.is_file():
        try:
            describe_text = describe_path.read_text(encoding="utf-8")
            fields.update(parse_describe_card_fields(describe_text))
            structure = parse_describe_structure(describe_text)
        except OSError:
            pass
    if not structure and isinstance(meta.get("structure"), dict):
        structure = dict(meta["structure"])
    yt = meta.get("youtube") or {}
    tags = list(meta.get("tags") or [])
    linked = list(meta.get("linked_projects") or [])
    note = meta.get("ricardo_note")
    if note is not None:
        note = str(note).strip() or None
    verdict = normalize_verdict(meta.get("verdict"))
    return {
        "ref_id": out_dir.name,
        "out_dir": str(out_dir),
        "source": meta.get("source"),
        "source_kind": meta.get("source_kind"),
        "title": yt.get("title") or meta.get("title"),
        "start": meta.get("start"),
        "end": meta.get("end"),
        "duration_s": (meta.get("features") or {}).get("duration_s")
        or meta.get("duration_s"),
        "focus": meta.get("focus") or "full",
        "prompt_extra": (meta.get("prompt_extra") or "")[:240] or None,
        "model": meta.get("agy_model"),
        "created_at": meta.get("created_at") or _utc_now_iso(),
        "identity": fields["identity"],
        "tags": tags,
        "imitation_do": fields["imitation_do"],
        "imitation_dont": fields["imitation_dont"],
        "describe_excerpt": fields["describe_excerpt"],
        "describe_path": str(describe_path) if describe_path.is_file() else None,
        "midi_path": str(out_dir / "ref.mid") if (out_dir / "ref.mid").is_file() else None,
        "linked_projects": linked,
        "batch_id": meta.get("batch_id"),
        "moments_validation": meta.get("moments_validation"),
        "structure": structure or None,
        "ricardo_note": note,
        "verdict": verdict,
        "verdict_at": meta.get("verdict_at"),
        "parent_ref_id": meta.get("parent_ref_id"),
        "moment_index": meta.get("moment_index"),
        "moment_t": meta.get("moment_t"),
        "stem_artifacts": meta.get("stem_artifacts"),
        "measured_drum_grid": meta.get("measured_drum_grid"),
        "judgment_tally": ref_tally_of(meta),
        "suspect": meta.get("suspect") is True,
        "suspect_reason": (str(meta.get("suspect_reason") or "").strip()
                           or None),
        "uncertain": _reconciliation_uncertain_flags(meta) or None,
        "ownership": normalize_ownership(meta.get("ownership")),
        "contract_version": meta.get("contract_version"),
        "listener_mode": normalize_listener_mode(meta.get("listener_mode")),
        "listen_verified": meta.get("listen_verified") is True,
        "has_measured": isinstance(meta.get("measured"), dict)
        and bool(meta.get("measured")),
        "exact_listener_model": meta.get("exact_listener_model"),
        "windows": (list(meta["windows"])
                    if isinstance(meta.get("windows"), list) else None),
        "card_path": str(v2_path) if v2_path.is_file() else None,
    }


def upsert_index_card(card: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    path = path or INDEX_PATH
    data = load_index(path)
    ref_id = card["ref_id"]
    prev = data["refs"].get(ref_id) or {}
    # Preserve tags / linked_projects / overlay if new card omits them
    if not card.get("tags") and prev.get("tags"):
        card["tags"] = list(prev["tags"])
    if not card.get("linked_projects") and prev.get("linked_projects"):
        card["linked_projects"] = list(prev["linked_projects"])
    if not card.get("ricardo_note") and prev.get("ricardo_note"):
        card["ricardo_note"] = prev["ricardo_note"]
    if (not card.get("verdict") or card.get("verdict") == "unset") and prev.get("verdict"):
        card["verdict"] = prev["verdict"]
        if prev.get("verdict_at") and not card.get("verdict_at"):
            card["verdict_at"] = prev["verdict_at"]
    # The judgment tally is an operator overlay, not listener output. Fresh
    # cards carry zeros (card_from_out_dir normalizes); keep the running
    # counts across a reindex instead of resetting them.
    if (not any(ref_tally_of(card).values()) and prev.get("judgment_tally")):
        card["judgment_tally"] = dict(prev["judgment_tally"])
    if not card.get("tallied_entries") and prev.get("tallied_entries"):
        card["tallied_entries"] = dict(prev["tallied_entries"])
    # The suspect flag is an operator overlay too: a re-listen rebuilds the
    # card from audio + meta, and meta carries the flag (mark_ref_suspect
    # writes it there). Only fall back to the previous index entry when the
    # rebuilt card has no flag of its own.
    if card.get("suspect") is not True and prev.get("suspect") is True:
        card["suspect"] = True
        if prev.get("suspect_reason") and not card.get("suspect_reason"):
            card["suspect_reason"] = prev["suspect_reason"]
    # Authorship is an operator overlay, never model output.  Keep it when an
    # analysis card omits it, and derive moment ownership from the parent when a
    # legacy child has no explicit parent_ref_id.
    current_owner = normalize_ownership(card.get("ownership"))
    previous_owner = normalize_ownership(prev.get("ownership"))
    if current_owner == "unknown" and previous_owner != "unknown":
        current_owner = previous_owner
    card["ref_id"] = ref_id
    parent_id = _parent_ref_id(card, data["refs"])
    parent = data["refs"].get(parent_id) if parent_id else None
    if parent_id:
        card["parent_ref_id"] = parent_id
    if parent:
        parent_owner = normalize_ownership(parent.get("ownership"))
        # Parent ownership is authoritative for moments; child tags remain
        # exactly the operator supplied tags.
        current_owner = parent_owner
    card["ownership"] = current_owner
    # X7 provenance is stamped, never heard: a rebuild from a meta.json that
    # predates the stamp must not drop it (same rule as the other overlays).
    if card.get("listener_mode") is None and prev.get("listener_mode"):
        card["listener_mode"] = prev["listener_mode"]
    if card.get("exact_listener_model") is None and prev.get(
            "exact_listener_model"):
        card["exact_listener_model"] = prev["exact_listener_model"]
    # Preserve a stamped value only when a caller omitted provenance
    # entirely. card_from_out_dir supplies explicit False for legacy meta,
    # so a fresh unverified listen cannot inherit an old True stamp.
    if card.get("listen_verified") is None and prev.get(
            "listen_verified") is not None:
        card["listen_verified"] = prev["listen_verified"]
    # The card artifact is the source of truth for its schema and section
    # shape. Analysis metadata can describe a newer attempted contract while
    # the last successfully validated card on disk is still the prior version.
    try:
        artifact_dir = ensure_out_dir_contained(
            Path(card.get("out_dir") or ref_dir_for_id(ref_id)))
    except ValueError:
        artifact_dir = None
    artifact_path = artifact_dir / "card.json" if artifact_dir else None
    if artifact_path is not None and artifact_path.is_file():
        try:
            artifact = validate_card(json.loads(
                artifact_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid card.json in {artifact_dir}: {exc}") from exc
        card["contract_version"] = artifact["contract_version"]
        card["sections"] = list(artifact.get("sections") or [])
    data["refs"][ref_id] = card
    # Keep the operator field beside the artifact so a reindex can recover it.
    try:
        out = ensure_out_dir_contained(Path(card.get("out_dir") or ref_dir_for_id(ref_id)))
    except ValueError:
        out = None
    if out is not None and out.is_dir():
        meta_path = out / "meta.json"
        meta: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        meta["ownership"] = current_owner
        if parent_id:
            meta["parent_ref_id"] = parent_id
        write_json_atomic(meta_path, meta)
    save_index(data, path)
    return card


def reindex_audio_refs(root: Path | None = None, index_path: Path | None = None) -> dict[str, Any]:
    root = root or OUT_ROOT
    index_path = index_path or INDEX_PATH
    # Include absent candidates too: a reindex may create metadata for a card
    # whose operator overlay lives only in the index, and rollback must remove
    # that newly created file.
    metadata_paths = [
        child / "meta.json"
        for child in root.iterdir()
        if child.is_dir() and child.name != "batches"
    ] if root.is_dir() else []
    snapshot = _snapshot_bytes([*metadata_paths, index_path])
    try:
        return _reindex_audio_refs_impl(root=root, index_path=index_path)
    except Exception:
        _restore_bytes(snapshot)
        raise


def _reindex_audio_refs_impl(root: Path | None = None, index_path: Path | None = None) -> dict[str, Any]:
    root = root or OUT_ROOT
    index_path = index_path or INDEX_PATH
    prev = load_index(index_path)
    fresh: dict[str, Any] = {"version": 1, "updated_at": None, "refs": {}}
    added = 0
    dropped = 0
    invalid_cards: list[dict[str, str]] = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name == "batches":
                continue
            # ONE shared notion of a filed ref (see _dir_is_filed_ref): an
            # offloaded source.wav must not drop its ref from the index, and a
            # failed-fetch meta.json must not create a phantom one.
            if not _dir_is_filed_ref(child):
                continue
            try:
                ensure_out_dir_contained(child)
            except ValueError:
                continue
            try:
                card = card_from_out_dir(child)
            except ValueError as exc:
                old = (prev.get("refs") or {}).get(child.name)
                if old:
                    # A malformed v2 card is not permission to downgrade the
                    # index to v1. Keep the last known-good card and report it.
                    fresh["refs"][child.name] = old
                invalid_cards.append({"ref_id": child.name, "error": str(exc)})
                continue
            old = (prev.get("refs") or {}).get(card["ref_id"]) or {}
            if old.get("tags") and not card.get("tags"):
                card["tags"] = list(old["tags"])
            if old.get("linked_projects") and not card.get("linked_projects"):
                card["linked_projects"] = list(old["linked_projects"])
            if old.get("ricardo_note") and not card.get("ricardo_note"):
                card["ricardo_note"] = old["ricardo_note"]
            if old.get("verdict") and (
                not card.get("verdict") or card.get("verdict") == "unset"
            ):
                card["verdict"] = old["verdict"]
                card["verdict_at"] = old.get("verdict_at") or card.get("verdict_at")
            if old.get("ownership") and normalize_ownership(old.get("ownership")) != "unknown":
                card["ownership"] = normalize_ownership(old["ownership"])
            # Operator overlays that live only in the previous index entry
            # (meta.json predates them) carry over — same rule as
            # upsert_index_card, so a reindex never resets operator state.
            if (not any(ref_tally_of(card).values())
                    and old.get("judgment_tally")):
                card["judgment_tally"] = dict(old["judgment_tally"])
            if not card.get("tallied_entries") and old.get("tallied_entries"):
                card["tallied_entries"] = dict(old["tallied_entries"])
            if card.get("suspect") is not True and old.get("suspect") is True:
                card["suspect"] = True
                if old.get("suspect_reason") and not card.get("suspect_reason"):
                    card["suspect_reason"] = old["suspect_reason"]
            if card.get("listener_mode") is None and old.get("listener_mode"):
                card["listener_mode"] = old["listener_mode"]
            if card.get("exact_listener_model") is None and old.get(
                    "exact_listener_model"):
                card["exact_listener_model"] = old["exact_listener_model"]
            # persist parsed structure onto meta for next time
            meta_path = child / "meta.json"
            if card.get("structure") and meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    meta = None
                if isinstance(meta, dict):
                    meta["structure"] = card["structure"]
                    write_json_atomic(meta_path, meta)
            if meta_path.is_file() or card.get("ownership") != "unknown":
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8")) \
                        if meta_path.is_file() else {}
                except (OSError, json.JSONDecodeError):
                    meta = None
                if isinstance(meta, dict):
                    meta["ownership"] = normalize_ownership(card.get("ownership"))
                    write_json_atomic(meta_path, meta)
            fresh["refs"][card["ref_id"]] = card
            added += 1
    # Legacy moment metadata has eleven null parent_ref_id values.  Resolve the
    # deterministic id prefix before inheriting ownership/tags/structure.
    for card in fresh["refs"].values():
        parent_id = _parent_ref_id(card, fresh["refs"])
        parent = fresh["refs"].get(parent_id) if parent_id else None
        if not parent:
            continue
        card["parent_ref_id"] = parent_id
        card["ownership"] = normalize_ownership(parent.get("ownership"))
        # Persist the recovered field to the source metadata as well as index.
        meta_path = Path(str(card.get("out_dir") or "")) / "meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = None
            if isinstance(meta, dict):
                meta["ownership"] = card["ownership"]
                meta["parent_ref_id"] = parent_id
                write_json_atomic(meta_path, meta)
    inherited = _inherit_parent_structure(fresh["refs"])
    if prev.get("refs"):
        dropped = len(set(prev["refs"]) - set(fresh["refs"]))
    save_index(fresh, index_path)
    return {
        "ok": not bool(invalid_cards),
        "count": len(fresh["refs"]),
        "scanned": added,
        "dropped": dropped,
        "structure_inherited": inherited,
        "steerable": sum(1 for c in fresh["refs"].values() if structure_is_usable(c)),
        "index_path": str(index_path),
        "invalid_cards": invalid_cards,
    }


STEERING_FIELDS = ("feel_bpm", "grid_bpm", "density", "modal_center", "register",
                   "drum_grid")

# density word -> ears gate modifier. These are the ONLY fields a reference is
# allowed to gate on; see ref_steering() for why the others don't.
_REF_DENSITY_MODS: dict[str, dict[str, str]] = {
    "low": {"density_max": "sparse"},
    "medium": {"density_max": "mid"},
    "high": {"density_min": "mid"},
}


def ref_to_mods(card: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Reference card -> ears gate modifiers. PURE: no filesystem, no index read.

    Returns ``(mods, unmapped)``. ``mods`` is the same shape as an entry in
    ``ears.DIRECTIONS`` — a reference is a direction with more resolution, so it
    rides the channel the ears spec already reserves for "the energy he's
    chasing", not the channel that reports what Ableton is playing.

    Only ``density`` becomes a gate. ``unmapped`` names every field that was
    present but deliberately not gated, so a caller can say so out loud instead
    of implying the whole brief was applied.
    """
    s = (card or {}).get("structure") or {}
    mods: dict[str, str] = {}
    unmapped: list[str] = []

    density = (s.get("density") or "").strip().lower() or None
    if density in _REF_DENSITY_MODS:
        mods.update(_REF_DENSITY_MODS[density])
    elif density:
        unmapped.append("density %r is not one of low/medium/high" % density)

    if s.get("grid_bpm") or s.get("feel_bpm"):
        # NOT a gate: ears' tempo_world covers 60-92 and 100-140 and silently
        # admits everything in the gaps, so gating on it is non-monotone in
        # tempo. Tempo steers retrieval instead (a window has no dead zone).
        unmapped.append("tempo steers retrieval, not the gates")
    if s.get("register"):
        # NOT a gate: the register gate exists to keep two players off the same
        # band. A reference isn't a player. Register biases octave placement.
        unmapped.append("register biases octave placement, not the gates")
    if s.get("modal_center"):
        # NOT a gate and NOT a transposition target: forcing his answer into the
        # reference's key would drag it away from the loop he is actually
        # playing. Mode travels as prose for the composing LLM to use.
        unmapped.append("modal centre is prose only — it never re-keys your loop")
    if s.get("swing_pct") is not None:
        # Parsed and stored, but nothing in this codebase implements swing —
        # humanize_notes does velocity contour and bounded jitter, not swing.
        # Say so rather than let a stored field imply a feature.
        unmapped.append("swing is parsed but not implemented anywhere — prose only")
    return mods, unmapped


# General MIDI drum pitches, matching the role buckets analyze/build_index.py
# uses to build every drum phrase's 96-dim onset grid (kick 35/36 · snare 37-40 ·
# hats 42/44/46 · toms · cymbals · perc). 93.3% of his indexed drum notes land in
# 35-59, so GM is a safe assumption on both sides of the comparison.
_GM = {"kick": 36, "snare": 38, "hat": 42, "openhat": 46, "ride": 51,
       "crash": 49, "tom": 45, "clap": 39, "rim": 37, "perc": 39}
_DRUM_WORDS = (
    ("kick", ("kick", "bass drum", "bd", "808 boom", "sub-bass pulse", "sub bass pulse")),
    ("snare", ("snare", "sd", "backbeat hit", "impact/snare", "impact snare")),
    ("clap", ("clap", "handclap")),
    ("openhat", ("open hat", "open hi-hat", "open hihat")),
    ("hat", ("hat", "hi-hat", "hihat", "closed hat")),
    ("ride", ("ride",)),
    ("crash", ("crash", "cymbal wash")),
    ("tom", ("tom", "floor tom")),
    ("rim", ("rimshot", "rim shot", "cross-stick", "cross stick")),
    ("perc", ("perc", "shaker", "tambourine", "conga", "bongo")),
)
_BEAT_RE = re.compile(r"\bbeats?\s+([1-4])(?:\s*(?:and|&|,)\s*([1-4]))?", re.I)
_ON_N_RE = re.compile(r"\bon\s+(?:the\s+)?([1-4])(?:\s*(?:and|&|\+|,)\s*([1-4]))?\b", re.I)


def drum_grid_to_notes(drum_grid: str | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reference drum prose -> one bar of MIDI hits, plus what was understood.

    This is the whole point of the drum route. Tempo and density cannot express
    the loudest thing in "a beat like Godflesh": two sparse four-hit bars differ
    ONLY in where the hits land. His phrase index stores placement as a 6-role x
    16-step onset grid, so turning the brief's prose into hits on that same grid
    lets the ordinary cosine search do the work — and tempo behaviour falls out
    for free, without the tempo_world dead zones.

    Returns ``(notes, understood)``. ``understood`` is deliberately explicit:
    an unparsed grid must read as "I couldn't hear that", never as an empty beat.
    """
    text = (drum_grid or "").strip()
    understood: dict[str, Any] = {"phrases": [], "instruments": [], "half_time": False}
    if not text:
        return [], understood
    low = text.lower()
    slots: dict[str, set[int]] = {}

    def put(inst: str, *slot_list: int) -> None:
        slots.setdefault(inst, set()).update(s % 16 for s in slot_list)

    half_time = bool(re.search(r"\bhalf[- ]time\b", low))
    understood["half_time"] = half_time

    if re.search(r"\bfour[- ]on[- ]the[- ]floor\b|\b4[- ]on[- ]the[- ]floor\b|"
                 r"\bfour to the floor\b", low):
        put("kick", 0, 4, 8, 12)
        understood["phrases"].append("four-on-the-floor kick")

    # One clause routinely names two instruments ("16th hats with backbeat snare
    # on 2 and 4"), so scope each one explicitly instead of taking the first
    # match: positions AFTER an instrument belong to it, while a subdivision
    # modifier sits BEFORE it. Without this, "kick on 1 and snare on 3" gives the
    # kick both beats and the snare none.
    for clause in re.split(r"[;.]|\s\|\s|,(?=[^0-9])", low):
        # Briefs label their bullets ("Kick/snare: kick on 1, snare on 3"). The
        # label names instruments it does not place, so it steals the positions
        # that belong to the real mentions after it. Drop it — but only when the
        # remainder still names an instrument, so "Hats: 8ths" keeps its subject.
        head, sep, tail = clause.partition(":")
        if sep and len(head) <= 30 and any(
            w in tail for _n, words in _DRUM_WORDS for w in words
        ):
            clause = tail
        found: list[tuple[int, int, str]] = []
        for name, words in _DRUM_WORDS:
            for w in words:
                i = clause.find(w)
                if i >= 0:
                    found.append((i, i + len(w), name))
                    break
        if not found:
            continue
        found.sort()
        # drop an instrument whose match is contained in an earlier one
        # ("open hat" already consumed the "hat" inside it)
        scoped = [f for i, f in enumerate(found)
                  if not any(f[0] >= g[0] and f[1] <= g[1] for g in found[:i])]
        for idx, (start, end, inst) in enumerate(scoped):
            nxt = scoped[idx + 1][0] if idx + 1 < len(scoped) else len(clause)
            after = clause[start:nxt]
            prev_end = scoped[idx - 1][1] if idx else 0
            before = clause[prev_end:start]
            beats: set[int] = set()
            for m in list(_BEAT_RE.finditer(after)) + list(_ON_N_RE.finditer(after)):
                for g in m.groups():
                    if g:
                        beats.add(int(g))
            # A subdivision AFTER an instrument that already has explicit beats
            # belongs to the NEXT instrument, not this one: "snare click on 2 and
            # 4 with 16th-note hi-hat" was putting the snare on all 16 slots.
            # A wrong grid is worse than an empty one — it retrieves confidently
            # wrong material — so when beats are stated, only a modifier BEFORE
            # the instrument ("16th hats") may add a subdivision.
            window = before if beats else before + after
            if re.search(r"\b16ths?\b|\bsixteenth", window):
                put(inst, *range(16))
                understood["phrases"].append(f"{inst} 16ths")
            elif re.search(r"\b8ths?\b|\beighth", window):
                put(inst, *range(0, 16, 2))
                understood["phrases"].append(f"{inst} 8ths")
            elif re.search(r"\boffbeat\b|\bupbeat\b", window):
                put(inst, 2, 6, 10, 14)
                understood["phrases"].append(f"{inst} offbeats")
            elif re.search(r"\bquarter|\bevery beat\b|\bpulse\b", window) and not beats:
                put(inst, 0, 4, 8, 12)
                understood["phrases"].append(f"{inst} quarters")
            if beats:
                put(inst, *[(b - 1) * 4 for b in sorted(beats)])
                understood["phrases"].append(
                    "%s on %s" % (inst, "+".join(str(b) for b in sorted(beats))))

    if not slots and half_time:
        put("kick", 0)
        put("snare", 8)
        understood["phrases"].append("half-time skeleton (kick 1, snare 3)")
    if not slots:
        return [], understood

    understood["instruments"] = sorted(slots)
    notes: list[dict[str, Any]] = []
    for inst, ss in sorted(slots.items()):
        for s in sorted(ss):
            notes.append({"pitch": _GM[inst], "start": round(s * 0.25, 4),
                          "duration": 0.25,
                          "velocity": 100 if inst in ("kick", "snare") else 80})
    notes.sort(key=lambda n: (n["start"], n["pitch"]))
    return notes, understood


# --- the composer-facing brief -------------------------------------------------
# One owner for every sentence a reference says to the composing LLM. These were
# inline literals inside apply_audio_ref; the melodic path never called that tool,
# so the only actionable part of a reference (its Do/Don't rules) reached nobody.

REF_GUIDANCE = (
    "You (Claude) compose with this ref as steering — adapt, don't paste "
    "verbatim unless intentional. Prefer imitation_do/dont over raw Basic "
    "Pitch MIDI when they conflict. score_phrase your result; if write was "
    "requested, notes land on SIDEKICK JAM only."
)

REF_KEEP_DROP = (
    "KEEP if the jam clip captures the ref's pocket/motif feel under your "
    "sounds; DROP and regenerate with a tighter prompt_extra / different role "
    "if it fights the track. After KEEP/DROP call tag_audio_ref(note=…, verdict=keep|drop)."
)

REF_OVERLAY_PREFIX = "Ricardo's overlay (prefer over Gemini when they conflict): "

# sombra-brain law #1 ("his material first") restated at the exact boundary where
# a reference could violate it — in code, so it cannot drift from the tool.
REF_AUTHORITY = (
    "A machine's description of someone else's recording. It is a TARGET, not a "
    "source: nothing here is material. Ricardo's retrieved phrase is the material "
    "— these rules shape how it is placed, repeated, thinned and registered. If a "
    "rule can only be honoured by inventing notes that are not in his phrase, skip "
    "it and say which one you skipped."
)

# His own records are in this library too. For those the
# "aim at it, don't copy it" caution is simply wrong: it is his record, and he is
# entitled to quote it. Confirmed by him 2026-09-03.
REF_AUTHORITY_HIS = (
    "This is YOUR OWN record, not an influence. It is material, not just a target: "
    "quoting a figure from it verbatim is legitimate, and the usual 'adapt, don't "
    "paste' caution does not apply. Where this project also exists in the MIDI "
    "catalogue, prefer those real phrases over this description of them — a "
    "description is a machine's account of your music, the phrases are the music."
)

_REF_BRIEF_MAX_DO = 6
_REF_BRIEF_MAX_DONT = 4
_SOURCE_LABEL_MARKER = "Source label"


def _schema_fail(path: str, why: str,
                 contract: str = CARD_CONTRACT_VERSION) -> None:
    raise ValueError(f"{contract} rejected at {path}: {why}")


def _validate_node(value: Any, node: dict, path: str,
                   contract: str = CARD_CONTRACT_VERSION) -> None:
    """Validate against the subset of JSON Schema V2_SCHEMA uses. Rejects, never
    repairs — a card that guessed is worse than a card that said it could not tell."""
    if "const" in node:
        if value != node["const"]:
            _schema_fail(path, f"must be {node['const']!r}, got {value!r}",
                         contract)
        return
    if "enum" in node and "type" not in node:
        if value not in node["enum"]:
            _schema_fail(path, f"{value!r} not in {node['enum']}", contract)
        return
    types = node.get("type")
    types = [types] if isinstance(types, str) else list(types or [])
    if "enum" in node and value not in node["enum"]:
        _schema_fail(path, f"{value!r} not in {node['enum']}", contract)
    if value is None:
        if "null" not in types:
            _schema_fail(path, "null is not allowed here", contract)
        return
    # nullable enums travel as type-only on the wire (agy rejects null inside an
    # enum), so the constraint is enforced here instead
    if "x-enum" in node and value not in node["x-enum"]:
        _schema_fail(path, f"{value!r} not in {node['x-enum']}", contract)
    if "object" in types:
        if not isinstance(value, dict):
            _schema_fail(path, "expected an object", contract)
        props = node.get("properties", {})
        unknown = sorted(set(value) - set(props))
        if unknown:
            _schema_fail(path, f"unknown key(s) {unknown}", contract)
        missing = sorted(set(node.get("required", [])) - set(value))
        if missing:
            _schema_fail(path, f"missing key(s) {missing}", contract)
        for k, sub in props.items():
            _validate_node(value[k], sub, f"{path}.{k}", contract)
        return
    if "array" in types:
        if not isinstance(value, list):
            _schema_fail(path, "expected an array", contract)
        if len(value) < node.get("minItems", 0):
            _schema_fail(path, f"needs at least {node['minItems']} item(s)",
                         contract)
        if "maxItems" in node and len(value) > node["maxItems"]:
            _schema_fail(path, f"at most {node['maxItems']} item(s), got {len(value)}",
                         contract)
        for i, item in enumerate(value):
            _validate_node(item, node.get("items", {}), f"{path}[{i}]", contract)
        return
    if "integer" in types:
        if isinstance(value, bool) or not isinstance(value, int):
            _schema_fail(path, "expected an integer", contract)
    elif "number" in types:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _schema_fail(path, "expected a number", contract)
    elif "boolean" in types:
        if not isinstance(value, bool):
            _schema_fail(path, "expected a boolean", contract)
    elif "string" in types:
        if not isinstance(value, str):
            _schema_fail(path, "expected a string", contract)
        if "maxLength" in node and len(value) > node["maxLength"]:
            _schema_fail(path, f"longer than {node['maxLength']} characters",
                         contract)
        if "pattern" in node and not re.match(node["pattern"], value):
            _schema_fail(path, f"{value!r} does not match {node['pattern']}",
                         contract)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in node and value < node["minimum"]:
            _schema_fail(path, f"below minimum {node['minimum']}", contract)
        if "maximum" in node and value > node["maximum"]:
            _schema_fail(path, f"above maximum {node['maximum']}", contract)


def validate_card_v2(obj: Any) -> dict:
    """Hard validation of a v2 card. Unknown keys are rejected, not ignored."""
    _validate_node(obj, V2_SCHEMA, "card")
    return obj


def validate_card_v3(obj: Any) -> dict:
    """Hard validation of a v3 card: closed schema plus the cross-claim rule.

    A claim below high confidence must be named in uncertain — tempo words
    for tempo, key words for key, grid words for the drum grid. Low
    confidence the model will not even name is a guess wearing a hedge.
    """
    _validate_node(obj, V3_SCHEMA, "card", V3_CONTRACT_VERSION)
    uncertain = [str(u).lower() for u in (obj.get("uncertain") or [])]

    def _named(*words: str) -> bool:
        return any(w in entry for entry in uncertain for w in words)

    if (obj.get("tempo") or {}).get("confidence") in ("low", "med") and not _named(
            "tempo", "bpm", "beat", "pulse", "grid"):
        _schema_fail("card.uncertain",
                     "tempo confidence is below high but nothing names it",
                     V3_CONTRACT_VERSION)
    if (obj.get("harmony") or {}).get("confidence") in ("low", "med") and not _named(
            "key", "modal", "minor", "major", "tonal", "pitch", "chord",
            "harmony", "tune", "tuning"):
        _schema_fail("card.uncertain",
                     "key confidence is below high but nothing names it",
                     V3_CONTRACT_VERSION)
    if (obj.get("drums") or {}).get("confidence") in ("low", "med") and not _named(
            "drum", "grid", "kick", "snare", "hat", "beat", "rhythm",
            "groove", "percussion", "pulse"):
        _schema_fail("card.uncertain",
                     "drum-grid confidence is below high but nothing names it",
                     V3_CONTRACT_VERSION)
    return obj


def validate_card(obj: Any) -> dict:
    """Version dispatch: v3 and v2 both read; anything else is rejected."""
    if not isinstance(obj, dict):
        raise ValueError(f"unknown contract_version: {obj!r}")
    contract = obj.get("contract_version")
    if contract == V3_CONTRACT_VERSION:
        return validate_card_v3(obj)
    if contract == CARD_CONTRACT_VERSION or (
            contract is None and "identity" in obj):
        # contract_version missing: a legacy v1-shaped dict reads as v2.
        return validate_card_v2(obj)
    raise ValueError(f"unknown contract_version: {contract!r}")


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)

# agy's own progress narration, not the model's answer
_AGY_ENVELOPE_KEYS = {"toolAction", "toolSummary", "conversation_id", "status",
                      "duration_seconds", "duration_ms", "num_turns",
                      "json_schema", "error", "usage", "model", "stop_reason",
                      "input_tokens", "output_tokens", "thinking_tokens",
                      "cached_tokens", "cache_read_tokens",
                      "cache_creation_tokens", "total_tokens"}


def _loads_first_json(text: str) -> Any:
    """First JSON object out of listener stdout, by decreasing strictness.

    Whole text first (the documented agy envelope, or a bare card), then a
    ```json fence, then the outermost brace span. The fence is only a
    fallback: searching it first lets a stray emphasis fence in the prose
    eat the envelope that actually carries the card."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = _JSON_FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except ValueError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("agy returned prose, not JSON: %s" % text[:160])
    return json.loads(text[start:end + 1])


def _agy_json_result(stdout: str) -> dict:
    """The card out of agy stdout. Tolerates a bare object, a ```json fence, and
    an envelope carrying the JSON as a string (fenced or prose-wrapped inside
    the response). A real agy turn also carries the schema-shaped answer in
    ``structured_output`` — that object wins over ``response``. Raises on
    prose — a describe that came back as commentary is a failure, never a
    partial success."""
    text = (stdout or "").strip()
    if not text:
        raise ValueError("agy returned nothing")
    obj = _loads_first_json(text)
    if isinstance(obj, dict):
        structured = obj.get("structured_output")
        if isinstance(structured, dict) and structured:
            # The model may still nest a JSON *string* inside a string-typed
            # field, so parse each object-shaped value once. The card
            # schema's fields are objects; plain strings never start with
            # a brace/bracket and pass through untouched.
            card = {}
            for key, value in structured.items():
                if (isinstance(value, str)
                        and value.strip()[:1] in ("{", "[")
                        and key not in _AGY_ENVELOPE_KEYS):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        pass
                card[key] = value
            return {k: v for k, v in card.items()
                    if k not in _AGY_ENVELOPE_KEYS}
        if isinstance(structured, str) and structured.strip():
            return _agy_json_result(structured)
    for key in ("result", "response", "text", "content"):
        if isinstance(obj, dict) and key in obj and isinstance(obj[key], str) and obj[key].strip():
            if key not in V2_SCHEMA["properties"]:
                return _agy_json_result(obj[key])
    if not isinstance(obj, dict):
        raise ValueError("agy returned a %s, not an object" % type(obj).__name__)
    # agy narrates its own progress into the object it returns. Those keys are the
    # harness talking, not the model's answer, so drop them rather than let them
    # trip the unknown-key rejection that exists to catch the model inventing fields.
    return {k: v for k, v in obj.items() if k not in _AGY_ENVELOPE_KEYS}


_V2_DENSITY_TO_STRUCTURE = {"sparse": "low", "medium": "medium", "dense": "high"}
_DRUM_GM_PITCH = {"kick": 36, "snare": 38, "hat": 42, "openhat": 46, "ride": 51,
                  "crash": 49, "tom": 45, "clap": 39, "rim": 37, "perc": 54}

# X6: inverse map for per-stem Basic Pitch winners (drums only). Names match
# _DRUM_GM_PITCH keys so measured grids round-trip through card_grid_to_notes.
_GM_PITCH_TO_DRUM: dict[int, str] = {
    pitch: name for name, pitch in _DRUM_GM_PITCH.items()
}


def structure_from_card(card: dict[str, Any]) -> dict[str, Any]:
    """A v2 or v3 card → the SAME seven-key structure v1 produced, plus what
    v1 could not carry. v3 drops the unconsumed melody/harmony sub-fields but
    keeps every key this reads, so no branch is needed. Keeps every existing
    consumer working and the steering contract version-blind."""
    tempo = card.get("tempo") or {}
    swing = card.get("swing") or {}
    reg = card.get("register") or {}
    harm = card.get("harmony") or {}
    mel = card.get("melody") or {}
    drums = (card.get("drums") or {}).get("grid") or []
    grid_prose = " | ".join(
        "%s on %s" % (d["instrument"], ", ".join(str(s) for s in d.get("steps") or []))
        for d in drums if d.get("steps")) or None
    return {
        "feel_bpm": tempo.get("feel_bpm"),
        "grid_bpm": tempo.get("grid_bpm") or tempo.get("feel_bpm"),
        "swing_pct": swing.get("pct"),
        "density": _V2_DENSITY_TO_STRUCTURE.get(str(card.get("density") or "")),
        "modal_center": harm.get("modal_center"),
        "register": reg.get("band"),
        "drum_grid": grid_prose,
        # what v1 had no field for
        "drum_steps": drums,
        "phrase_length_bars": mel.get("phrase_length_bars"),
        "pitch_classes": harm.get("pitch_classes") or [],
        "interval_content": harm.get("interval_content") or [],
    }


def card_grid_to_notes(card: dict[str, Any]) -> tuple[list[dict], bool]:
    """The card's STRUCTURED drum grid → one bar of GM notes. Replaces prose
    parsing, which filed 'sidechain compression triggered by the kick' as a
    drum pattern because it contained the word kick. An empty grid invents
    nothing."""
    out: list[dict] = []
    for lane in (card.get("drums") or {}).get("grid") or []:
        pitch = _DRUM_GM_PITCH.get(str(lane.get("instrument") or ""))
        if pitch is None:
            continue
        for step in lane.get("steps") or []:
            out.append({"pitch": pitch, "start": float(step) * 0.25,
                        "duration": 0.25, "velocity": 100})
    out.sort(key=lambda n: (n["start"], n["pitch"]))
    return out, bool(out)


def render_describe_v2(card: dict[str, Any]) -> str:
    """The human-readable describe, RENDERED from the validated card. Prose and
    data can no longer disagree, and the injected source-label line can never
    reach it."""
    s = structure_from_card(card)
    harm = card.get("harmony") or {}
    mel = card.get("melody") or {}
    bits = [f"# Identity\n{card.get('identity') or ''}", ""]
    groove = []
    if s["drum_grid"]:
        groove.append(f"- Kick/snare: {s['drum_grid']}.")
    tempo_txt = ", ".join(
        x for x in (f"~{s['feel_bpm']:g} BPM feel" if s["feel_bpm"] else None,
                    f"{s['grid_bpm']:g} BPM grid" if s["grid_bpm"] else None) if x)
    groove.append("- Density: %s; %s swing; %s." % (
        card.get("density") or "?",
        ("%g%%" % s["swing_pct"]) if s["swing_pct"] is not None else "unstated",
        tempo_txt or "tempo unstated"))
    bits += ["# Groove", *groove, ""]
    hm = []
    if harm.get("modal_center"):
        hm.append(f"- {harm['modal_center']}.")
    if harm.get("pitch_classes"):
        hm.append("- Pitch classes: %s." % ", ".join(harm["pitch_classes"]))
    if harm.get("interval_content"):
        hm.append("- Intervals: %s." % ", ".join(harm["interval_content"]))
    if mel.get("phrase_length_bars"):
        hm.append("- Phrase length: %g bars (%s)." % (
            mel["phrase_length_bars"], mel.get("repetition") or "unstated"))
    bits += ["# Harmony / melody", *(hm or ["- not determinable from this file."]), ""]
    bits += ["# Imitation rules"]
    bits += [f"- Do: {d}" for d in (card.get("imitation") or {}).get("do") or []]
    bits += [f"- Don't: {d}" for d in (card.get("imitation") or {}).get("dont") or []]
    bits += ["", "# Moments"]
    bits += ["- %s – %s" % (sec.get("t"), sec.get("label"))
             for sec in card.get("sections") or []]
    if card.get("uncertain"):
        bits += ["", "# Uncertain"] + ["- %s" % u for u in card["uncertain"]]
    return "\n".join(bits).strip() + "\n"


def render_describe_v3(card: dict[str, Any]) -> str:
    """The human-readable describe, RENDERED from a validated v3 card. Same
    no-disagreement guarantee as v2, plus the v3 fields: section energies,
    layers, low end, space and the trick."""
    s = structure_from_card(card)
    harm = card.get("harmony") or {}
    mel = card.get("melody") or {}
    low = card.get("low_end") or {}
    bits = [f"# Identity\n{card.get('identity') or ''}", ""]
    groove = []
    if s["drum_grid"]:
        groove.append(f"- Kick/snare: {s['drum_grid']}.")
    tempo_txt = ", ".join(
        x for x in (f"~{s['feel_bpm']:g} BPM feel" if s["feel_bpm"] else None,
                    f"{s['grid_bpm']:g} BPM grid" if s["grid_bpm"] else None) if x)
    groove.append("- Density: %s; %s swing; %s." % (
        card.get("density") or "?",
        ("%g%%" % s["swing_pct"]) if s["swing_pct"] is not None else "unstated",
        tempo_txt or "tempo unstated"))
    bits += ["# Groove", *groove, ""]
    hm = []
    if harm.get("modal_center"):
        hm.append(f"- {harm['modal_center']}.")
    if harm.get("pitch_classes"):
        hm.append("- Pitch classes: %s." % ", ".join(harm["pitch_classes"]))
    if mel.get("phrase_length_bars"):
        hm.append("- Phrase length: %g bars (%s)." % (
            mel["phrase_length_bars"], mel.get("repetition") or "unstated"))
    bits += ["# Harmony / melody", *(hm or ["- not determinable from this file."]), ""]
    layers = []
    for layer in card.get("layers") or []:
        layers.append("- %s (%s), first at %s." % (
            layer.get("role"), layer.get("timbre"), layer.get("first_t")))
    bits += ["# Layers", *(layers or ["- none isolated."]), ""]
    le = []
    if low.get("kick_bass_relation"):
        le.append(f"- Kick vs bass: {low['kick_bass_relation']}.")
    if low.get("sidechain_audible") is not None:
        le.append("- Sidechain pumping: %s." % (
            "audible" if low["sidechain_audible"] else "not audible"))
    if card.get("space"):
        le.append(f"- Space: {card['space']}.")
    bits += ["# Low end / space", *(le or ["- not determinable from this file."]), ""]
    trick = card.get("the_trick") or {}
    if trick.get("what"):
        bits += ["# The trick", f"- {trick['what']} ({trick.get('t')}).", ""]
    bits += ["# Imitation rules"]
    for d in (card.get("imitation") or {}).get("do") or []:
        line = f"- Do: {d.get('rule')}"
        if d.get("ableton_move"):
            line += f" [{d['ableton_move']}]"
        bits.append(line)
    for d in (card.get("imitation") or {}).get("dont") or []:
        line = f"- Don't: {d.get('rule')}"
        if d.get("ableton_move"):
            line += f" [{d['ableton_move']}]"
        bits.append(line)
    bits += ["", "# Sections"]
    bits += ["- %s – %s (energy %s)" % (sec.get("t"), sec.get("label"),
                                        sec.get("energy"))
             for sec in card.get("sections") or []]
    if card.get("uncertain"):
        bits += ["", "# Uncertain"] + ["- %s" % u for u in card["uncertain"]]
    return "\n".join(bits).strip() + "\n"


def card_from_v3(card: dict[str, Any], *, ref_id: str, out_dir: str,
                 **extra: Any) -> dict[str, Any]:
    """A v3 card → the SAME index-card shape v2 produces.

    Imitation objects flatten to their rule text so every downstream reader
    (brief, steering, search) works version-blind; the full v3 card rides
    along under "card" for the fields only v3 has.
    """
    imit = card.get("imitation") or {}
    out = {
        "ref_id": ref_id,
        "out_dir": out_dir,
        "contract_version": V3_CONTRACT_VERSION,
        "identity": card.get("identity"),
        "ownership": card.get("ownership") or "unknown",
        "imitation_do": [str(d.get("rule")) for d in (imit.get("do") or [])],
        "imitation_dont": [str(d.get("rule")) for d in (imit.get("dont") or [])],
        "describe_excerpt": render_describe_v3(card)[:1600],
        "structure": structure_from_card(card),
        "sections": list(card.get("sections") or []),
        "uncertain": list(card.get("uncertain") or []),
        "card": card,
    }
    out.update(extra)
    return out


def card_from_v2(card: dict[str, Any], *, ref_id: str, out_dir: str,
                 **extra: Any) -> dict[str, Any]:
    """A v2 card → the index-card shape the rest of the system reads."""
    imit = card.get("imitation") or {}
    out = {
        "ref_id": ref_id,
        "out_dir": out_dir,
        "contract_version": CARD_CONTRACT_VERSION,
        "identity": card.get("identity"),
        "ownership": card.get("ownership") or "unknown",
        "imitation_do": list(imit.get("do") or []),
        "imitation_dont": list(imit.get("dont") or []),
        "describe_excerpt": render_describe_v2(card)[:1600],
        "structure": structure_from_card(card),
        "sections": list(card.get("sections") or []),
        "uncertain": list(card.get("uncertain") or []),
        "card": card,
    }
    out.update(extra)
    return out


@contextlib.contextmanager
def staged_audio_only(wav: str):
    """Expose the audio and NOTHING else to the listener.

    --add-dir hands the model a whole directory. On a FIRST analysis that
    directory held only the wav, but on a RE-listen it also holds the previous
    describe.txt and meta.json — the very answer we are trying to replace.
    Measured 2026-09-03: gemini-3.8-flash-medium returned the old contaminated
    description character-for-character (identity, 68 BPM feel, 136 grid, C#
    minor) rather than listening. A model cannot copy what it cannot see.

    The staged file is an ffmpeg-transcoded plain PCM copy, never the raw
    file: raw session takes are the exact inputs agy demonstrably cannot
    open (step_type 17 "unsupported mime type" in real records)."""
    tmp = tempfile.mkdtemp(prefix="audioref-listen-")
    try:
        if not Path(wav).is_file():
            # An offloaded ref must fail with the recovery command, not a raw
            # os.link errno the operator cannot act on.
            _require_local_source(Path(wav).parent)
        staged = os.path.join(tmp, Path(wav).stem + ".wav")
        stage_listenable_copy(wav, staged)
        yield staged
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _listen_directive(staged_audio_path: str) -> str:
    """The prompt's first sentence: the exact listening procedure.

    Without this sentence the agent treats the card task as repo research
    (2026-09-25 pilot: 53 tool requests, 0 wav view_file calls). The
    absolute staged path leaves no room for "which file" exploration;
    the second sentence bans every other tool action. Pass B windows
    share this harness, so their prompts carry it too."""
    return (
        "Your first and only tool action is view_file on %s. Do not run "
        "commands, list directories, search, or read any other file."
        % staged_audio_path
    )


def _resolve_model(model: str | None) -> str:
    """explicit argument → AUDIO_REF_MODEL → default (session_ears and
    mix_witness honour their own env overrides the same way)."""
    return model or os.environ.get("AUDIO_REF_MODEL") or DEFAULT_AGY_MODEL


# M3 listener hardening: the auto-approve flag is a single constant,
# default OFF. A no-tool structured print turn should not need it; the
# orchestrator verifies that with a real call — flip to True only if that
# verification fails. Session ears keeps it ON (session_ears.
# EARS_AGY_SKIP_PERMISSIONS) because its listener writes a card file; this
# listener only views the audio and answers on stdout.
AGY_SKIP_PERMISSIONS = False


def _agy_cmd_for(model: str, add_dir: str, prompt: str, *, timeout_s: int = 600,
                 schema: dict | str | None = None) -> list[str]:
    """The agy invocation for a schema-enforced card read from stdout.

    One structured print turn: ``--print`` + ``--output-format json`` +
    ``--json-schema``. ``schema`` is a schema-FILE path in production (argv
    stays short); a dict is still accepted and inlined. The only ``--add-dir``
    is the staged-audio directory, and ``--sandbox`` is unconditional for
    card listens — the audio is untrusted input, and no environment
    override can lift that. The listener gets the audio and nothing else.
    """
    cmd = [_which("agy") or "agy", "--model", model]
    # Antigravity slugs encode effort in the name; a duplicate --effort is rejected
    if not any(model.endswith(f"-{e}") for e in ("low", "medium", "high")):
        cmd.extend(["--effort", "high"])
    cmd.extend(["--print-timeout",
                f"{max(1, math.ceil(float(timeout_s) / 60.0))}m"])
    if AGY_SKIP_PERMISSIONS:
        cmd.append("--dangerously-skip-permissions")
    cmd.append("--sandbox")
    if _agy_supports_flag(cmd[0], "--disable-slash-commands"):
        cmd.append("--disable-slash-commands")
    cmd.extend(["--add-dir", add_dir, "--output-format", "json"])
    if schema is not None:
        cmd.extend(["--json-schema", schema if isinstance(schema, str)
                    else json.dumps(schema)])
    cmd.extend(["--print", prompt])
    return cmd


# The agy subprocess inherits a minimal environment. This allowlist is
# IDENTICAL to session_ears._AGY_ENV_ALLOW (pinned by an equality test) —
# one list, no prefixes. In particular GEMINI_*/GOOGLE_* never cross,
# exactly like session ears; agy authenticates via keychain/config under
# HOME. The operator's unrelated secrets, PYTHONPATH and shell
# customizations never cross into the listener turn.
# Duplicated, not imported: `python3 tools/audio_ref.py` runs with tools/
# on sys.path, where mcp_server is not importable.
_AGY_ENV_ALLOW = (
    "PATH", "HOME", "USER", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL",
    "LC_CTYPE", "SYSTEMROOT", "COMSPEC", "PATHEXT",
)


def _agy_listen_env() -> dict[str, str]:
    """Allowlisted environment for one agy listener turn."""
    return {k: v for k, v in os.environ.items() if k in _AGY_ENV_ALLOW}


# `agy --help` probe cache: agy gains flags over time, and passing a flag
# an older agy rejects fails the whole listen. Probed once per process;
# any failure reads as unsupported (fail-open — the flag is hygiene, the
# sandbox is the guarantee). Tests clear this between cases.
_agy_help_cache: dict[str, bool] = {}


def _agy_supports_flag(agy: str, flag: str) -> bool:
    """Whether ``agy --help`` lists a flag. Never raises, never touches
    the network — --help is local usage text."""
    key = "%s\x00%s" % (agy, flag)
    if key not in _agy_help_cache:
        try:
            with tempfile.TemporaryDirectory(prefix="agy-help-probe-") as cwd:
                proc = subprocess.run(
                    [agy, "--help"], check=False, text=True,
                    capture_output=True, timeout=30, cwd=cwd,
                    env=_agy_listen_env())
            text = (proc.stdout or "") + (proc.stderr or "")
            _agy_help_cache[key] = flag in text
        except (OSError, subprocess.SubprocessError):
            _agy_help_cache[key] = False
    return _agy_help_cache[key]


def _note_listener_turns(usage: dict[str, Any] | None,
                         warnings: list[str]) -> None:
    """Warn when the listener needed more than one turn.

    One structured print turn is the contract; extra turns cost quota, so
    num_turns > 1 lands in the meta warnings where the operator will see
    it. num_turns itself is recorded in listen_usage by _merge_usages."""
    try:
        turns = int((usage or {}).get("num_turns") or 0)
    except (TypeError, ValueError):
        return
    if turns > 1:
        warnings.append(
            "listener took %d turns (>1) — one structured print turn is "
            "the contract; extra turns cost quota" % turns)


# A listen failure, classified for the batch report. The report carries the
# class — not the last 500 bytes of stdout — so a resume decision is one
# comparison: quota stops the batch, validation gets one repair retry,
# anything else just records.
_QUOTA_RE = re.compile(
    r"RESOURCE_EXHAUSTED|\bquota\b|\b429\b|rate.limit|rate_limit",
    re.I,
)


def classify_listen_failure(describe_result: dict[str, Any] | None) -> str:
    """One of quota | timeout | invalid_json | validation | other.

    ``describe_result`` is run_agy_describe_v2's out dict (or anything with
    an error/tails shape). Anything unrecognized is "other", never silent.
    """
    res = describe_result or {}
    if res.get("listen_rejected"):
        return "listen_rejected"
    text = " ".join(str(res.get(k) or "")
                    for k in ("error", "stderr_tail", "stdout_tail"))
    if _QUOTA_RE.search(text):
        return "quota"
    if "timeout" in text.lower():
        return "timeout"
    err = str(res.get("error") or "")
    if "invalid v2 card" in err or "invalid v3 card" in err:
        # The validator rejects with "rejected at <path>"; anything else is
        # the model not returning JSON at all (prose, empty, wrong type).
        return "validation" if "rejected at" in err else "invalid_json"
    return "other"


# Usage fields the harness records from agy's JSON envelope. agy narrates its
# own progress into the object it returns; the card parser drops those keys,
# but the token/cost tally is kept here for meta.json `listen_usage`.
_AGY_USAGE_KEYS = frozenset({
    "input_tokens", "output_tokens", "thinking_tokens", "cached_tokens",
    "cache_read_tokens", "cache_creation_tokens", "total_tokens",
    "duration_seconds", "duration_ms", "num_turns", "model", "stop_reason",
    "status",
})


def _agy_usage_from_stdout(stdout: str) -> dict[str, Any]:
    """Best-effort usage envelope out of agy stdout. Never raises: usage is
    accounting, and a missing envelope must not fail a good card. Handles
    indented envelopes wrapped in surrounding prose via the brace span."""
    try:
        text = (stdout or "").strip()
        if not text:
            return {}
        try:
            obj = _loads_first_json(text)
        except ValueError:
            return {}
    except AttributeError:
        return {}
    if not isinstance(obj, dict):
        return {}
    usage: dict[str, Any] = {}
    nested = obj.get("usage")
    if isinstance(nested, dict):
        for key, value in nested.items():
            if key in _AGY_USAGE_KEYS and not isinstance(value, (dict, list)):
                usage[key] = value
    for key in sorted(_AGY_USAGE_KEYS):
        if key not in usage and key in obj and not isinstance(obj[key], (dict, list)):
            usage[key] = obj[key]
    return usage


_USAGE_SUM_KEYS = ("input_tokens", "output_tokens", "thinking_tokens",
                   "cached_tokens", "cache_read_tokens",
                   "cache_creation_tokens", "total_tokens",
                   "duration_seconds", "duration_ms", "num_turns")


def _merge_usages(usages: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum per-attempt tallies into one listen tally. Token counts and turns
    add; labels (model, stop reason) take the last attempt's."""
    merged: dict[str, Any] = {}
    for attempt in usages:
        for key in _USAGE_SUM_KEYS:
            value = attempt.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            merged[key] = merged.get(key, 0) + value
    for attempt in usages:
        for key in ("model", "stop_reason", "status"):
            if key in attempt:
                merged[key] = attempt[key]
    return merged


# B3 proof-of-listening: a card is only persisted when the agy run behind
# it demonstrably opened the staged audio. The 2026-09-25 pilot proved a
# returned card is no evidence of a listen (3.46M tokens, 0 wav views).
# Caps guard the other failure mode (an expensive non-listen).
DEFAULT_LISTEN_TOKEN_CAP = 250000
DEFAULT_WINDOW_TOKEN_CAP = 150000


def agy_conversations_dir(explicit: str | Path | None = None) -> Path:
    """Where agy conversation records live. Explicit argument wins, then
    AGY_CONVERSATIONS_DIR, then agy's own default. Injectable for tests."""
    if explicit is not None and str(explicit).strip():
        return Path(str(explicit)).expanduser()
    env = os.environ.get("AGY_CONVERSATIONS_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".gemini" / "antigravity-cli" / "conversations"


def agy_print_text(stdout: str) -> str:
    """The model's answer text out of an ``--output-format json`` envelope.

    Every listener runs with ``--output-format json`` so the envelope carries
    the conversation_id the proof check needs; the answer itself sits in the
    envelope's result/response text (or structured_output). Plain stdout that
    is not an envelope passes through unchanged."""
    raw = (stdout or "").strip()
    try:
        obj = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(obj, dict):
        for key in ("result", "response", "text", "content"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        structured = obj.get("structured_output")
        if isinstance(structured, dict) and structured:
            return json.dumps(structured)
        if isinstance(structured, str) and structured.strip():
            return structured.strip()
    return raw


def _agy_conversation_id_from_stdout(stdout: str) -> str | None:
    """The conversation_id out of agy's JSON envelope. None when stdout
    carries no envelope id — then the run has no readable record and any
    verification against it is 'unverifiable'. Never raises."""
    try:
        obj = _loads_first_json((stdout or "").strip())
    except (ValueError, AttributeError):
        return None
    if not isinstance(obj, dict):
        return None
    cid = obj.get("conversation_id")
    if isinstance(cid, str) and cid.strip():
        return cid.strip()
    return None


def _agy_conversation_db_path(conversation_id: str,
                              conversations_dir: str | Path | None) -> Path | None:
    """The <id>.db record for a run, or None when no file matches. A
    prefix glob tolerates agy recording under a longer sibling name;
    an unreadable directory is 'no file', never an exception."""
    try:
        root = agy_conversations_dir(conversations_dir)
        exact = root / f"{conversation_id}.db"
        if exact.is_file():
            return exact
        hits = sorted(root.glob(f"{conversation_id}*.db"))
        return hits[0] if hits else None
    except OSError:
        return None


def _agy_usage_from_conversation(conversation_id: str | None,
                                conversations_dir: str | Path | None
                                ) -> dict[str, Any]:
    """Usage extraction is unavailable: agy's gen_metadata is protobuf, not JSON."""
    return {}


# Real agy record shapes (sqlite table `steps`): a planner step
# (step_type 15) is only the tool CALL — bytes like
# `\tview_file\x1a..{"AbsolutePath":"<path>","toolAction":..}` — while
# a success result (step_type 132, status 3) carries the heard bytes
# ("The following is the entire, complete content of the requested
# file." + an audio/* mime or a .tempmediaStorage/media_<n> reference).
# A failure result (step_type 17, "invalid tool call ... unsupported
# mime type audio/wave") proves the opposite: the file never reached
# the model. Proof of a listen is therefore a RESULT record, never a
# call: planner text, thinking that mentions view_file, and
# run_command/ffprobe touching the file never count.
_AGY_VIEW_PLANNER_STEP = 15
_AGY_VIEW_RESULT_STEP = 132
_AGY_VIEW_RESULT_STATUS = 3
_AGY_VIEW_FAILURE_STEP = 17


def _step_shows_wav_view(metadata: bytes, step_payload: bytes,
                         staged_audio_path: str) -> bool:
    """One RESULT payload holds a successful view_file of the staged audio.

    Operationalized defensively on raw bytes (the record is protobuf
    without a committed schema): the payload must name the tool
    (``view_file``), quote the staged file as an exact params path
    (``"AbsolutePath":"<exact staged path>"`` — exact, with the closing
    quote, so a longer sibling path never matches), and carry a media
    marker (an ``audio/`` mime or a ``tempmediaStorage`` reference).
    The caller gates this on step_type 132 / status 3; standalone it
    answers only "does this payload read as a successful view".
    Returns False on anything unexpected: absence of proof is not
    proof of a listen."""
    try:
        meta = bytes(metadata or b"")
        payload = bytes(step_payload or b"")
        if not meta or not payload or not staged_audio_path:
            return False
        if b"view_file" not in meta or b"run_command" in meta:
            return False
        paths = re.findall(rb'"AbsolutePath"\s*:\s*"([^"\\]*)"', meta)
        if not any(os.path.realpath(path.decode("utf-8", "ignore"))
                   == os.path.realpath(staged_audio_path) for path in paths):
            return False
        lowered = payload.lower()
        return b"audio/" in lowered or b"tempmediastorage" in lowered
    except (TypeError, AttributeError):
        return False


def _step_rejects_wav_view(cells: list[bytes], staged_audio_path: str) -> bool:
    """One payload records a failed view_file of the staged audio.

    The agent asked but the file never reached the model ("invalid
    tool call ... unsupported mime type audio/wave" — seen in real
    records for raw 24-bit/float session takes and mp3s). The caller
    gates this on step_type 17. Returns False on anything unexpected.
    """
    try:
        if not cells or not staged_audio_path:
            return False
        for cell in cells:
            raw = bytes(cell)
            paths = re.findall(rb'"AbsolutePath"\s*:\s*"([^"\\]*)"', raw)
            if (any(os.path.realpath(path.decode("utf-8", "ignore"))
                    == os.path.realpath(staged_audio_path) for path in paths)
                    and (b"invalid tool call" in raw.lower()
                         or b"unsupported mime" in raw.lower())):
                return True
        return False
    except (TypeError, AttributeError):
        return False


def conversation_shows_wav_view(conversation_id: str,
                                staged_audio_path: str, *,
                                conversations_dir: str | Path | None = None) -> bool:
    """True iff the conversation record for a run shows a view_file RESULT
    on the staged audio path. False covers every unverifiable case too
    (missing DB, corrupt file, no step table, no step_type/status
    columns to tell a call from a result) — callers map those to
    reason 'unverifiable' via verify_agy_listen. A step_type 17 mime
    failure for the path vetoes: even beside a success record the run
    is rejected. Read-only; never raises."""
    try:
        db_path = _agy_conversation_db_path(conversation_id,
                                            conversations_dir)
        if db_path is None:
            return False
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            tables = {row[0] for row in
                      con.execute("select name from sqlite_master "
                                  "where type='table'")}
            if "steps" not in tables:
                return False
            cols = [row[1] for row in con.execute("PRAGMA table_info(steps)")]
            if "step_type" not in cols or "status" not in cols:
                # Without the call/result discriminator a view_file
                # mention cannot prove a listen — fail closed.
                return False
            blobs = [c for c in cols
                     if c not in ("idx", "step_type", "status",
                                  "has_subtrajectory", "step_format")]
            if not blobs:
                return False
            query = "select step_type, status, %s from steps" % ", ".join(
                f'"{c}"' for c in blobs)
            heard = False
            for row in con.execute(query):
                try:
                    step_type = int(row[0])
                    status = int(row[1])
                except (TypeError, ValueError):
                    continue
                cells = [bytes(cell) for cell in row[2:]
                         if isinstance(cell, (bytes, bytearray)) and cell]
                if not cells:
                    continue
                if _step_rejects_wav_view(cells, staged_audio_path):
                    return False
                metadata = bytes(row[2 + blobs.index("metadata")]) if "metadata" in blobs and row[2 + blobs.index("metadata")] else b""
                payload = bytes(row[2 + blobs.index("step_payload")]) if "step_payload" in blobs and row[2 + blobs.index("step_payload")] else b""
                if (step_type in (_AGY_VIEW_RESULT_STEP, 8)
                        and status == _AGY_VIEW_RESULT_STATUS
                        and _step_shows_wav_view(metadata, payload,
                                                 staged_audio_path)):
                    heard = True
            return heard
        finally:
            con.close()
    except (sqlite3.Error, OSError, ValueError):
        return False


def verify_agy_listen(*, conversation_id: str | None,
                      staged_audio_path: str,
                      usage: dict[str, Any] | None = None,
                      token_cap: int = DEFAULT_LISTEN_TOKEN_CAP,
                      conversations_dir: str | Path | None = None,
                      allow_unverified: bool = False) -> dict[str, Any]:
    """Proof-of-listening + cost guard for one agy run. Never raises.

    Returns ``{"ok", "verified", "reason", "conversation_id",
    "token_cap", "usage", ...}``. ``ok`` gates persistence; ``verified``
    stamps provenance. Reasons: None (verified), 'no_listen' (record
    read, no wav view), 'over_budget' (cap exceeded), 'unverifiable'
    (no record to check — fail closed unless ``allow_unverified``).
    Missing usage figures cannot prove or disprove the cap, so they
    pass the cost check as ``usage_missing`` (usage is accounting, and
    a missing envelope must not fail a card that demonstrably listened).
    """
    use = dict(usage or {})
    if not any(isinstance(use.get(k), (int, float))
               and not isinstance(use.get(k), bool)
               for k in ("input_tokens", "cache_read_tokens")):
        db_usage = _agy_usage_from_conversation(conversation_id,
                                                conversations_dir)
        for key, value in db_usage.items():
            use.setdefault(key, value)
    record: dict[str, Any] = {
        "ok": False,
        "verified": False,
        "reason": None,
        "conversation_id": conversation_id,
        "staged_audio": os.path.basename(staged_audio_path or ""),
        "token_cap": int(token_cap),
        "usage": use,
    }
    spent = 0
    known = False
    for key in ("input_tokens", "cache_read_tokens"):
        value = use.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            spent += value
            known = True
    if known and spent > int(token_cap):
        record["reason"] = "over_budget"
        record["spent_tokens"] = spent
        return record
    if not known:
        record["usage_missing"] = True
    else:
        record["spent_tokens"] = spent
    if not conversation_id or not str(conversation_id).strip():
        record["reason"] = "unverifiable"
        record["ok"] = bool(allow_unverified)
        return record
    if not conversation_shows_wav_view(str(conversation_id),
                                       staged_audio_path,
                                       conversations_dir=conversations_dir):
        # No view step, or no readable record at all: indistinguishable
        # from the pilot's deaf run, so both map fail-closed here; the
        # harness cannot tell them apart and must not guess.
        record["reason"] = "unverifiable"
        db_path = _agy_conversation_db_path(str(conversation_id),
                                            conversations_dir)
        if db_path is not None:
            record["reason"] = "no_listen"
        record["ok"] = bool(allow_unverified) and record["reason"] == "unverifiable"
        return record
    record["ok"] = True
    record["verified"] = True
    return record


def _attempt_verified_for_repair(out: dict[str, Any], stdout: str,
                                   staged_audio_path: str,
                                   listen_token_cap: int | None,
                                   conversations_dir: str | Path | None,
                                   allow_unverified: bool) -> bool:
    """Proof-check a malformed-card attempt before any repair retry.

    A second turn cannot fix a run that never listened, so the attempt
    goes through ``verify_agy_listen`` first: only a verified run earns
    the repair. A rejected run (no_listen / over_budget / unverifiable)
    is recorded on ``out`` as a listen rejection and the caller returns
    it without repairing. An allowed-but-unverified run keeps its
    validation error, also without a repair. Never raises.
    """
    conversation_id = _agy_conversation_id_from_stdout(stdout)
    out["conversation_id"] = conversation_id
    check = verify_agy_listen(
        conversation_id=conversation_id,
        staged_audio_path=staged_audio_path,
        usage=out.get("usage"),
        token_cap=(int(listen_token_cap)
                   if listen_token_cap else
                   DEFAULT_LISTEN_TOKEN_CAP),
        conversations_dir=conversations_dir,
        allow_unverified=allow_unverified,
    )
    out["listen_verification"] = check
    if check.get("verified"):
        return True
    if not check["ok"]:
        out.update({
            "ok": False,
            "error": "listen rejected: %s" % check["reason"],
            "listen_rejected": {
                "reason": check["reason"],
                "usage": check["usage"],
                "conversation_id": conversation_id,
            },
        })
    return False


def contract_version_of(card: dict[str, Any]) -> str:
    return str((card or {}).get("contract_version") or "audio-ref-card-v1")


def _strip_source_label(text: str | None) -> str | None:
    """Drop the injected 'Source label (for Identity context only): <url>' line.

    It is prompt scaffolding that got persisted into describe.txt, so it sits in
    the excerpt and in the search haystack as if it were something heard."""
    if not text:
        return None
    kept = [ln for ln in str(text).splitlines()
            if _SOURCE_LABEL_MARKER not in ln]
    out = "\n".join(kept).strip()
    return out or None


# A rule naming a key centre reads as a transposition order unless it is
# defused: the reference never re-keys his loop, so the centre travels as
# information ("theirs is X") with the loop's own key left alone.
_REF_CENTRE_RE = re.compile(
    r"\b([A-G][#b]?\s+(?:major|minor))\b"
    r"|\b(dorian|phrygian|lydian|mixolydian|aeolian|ionian|locrian)\b",
    re.I,
)

# Pitch names next to centre-words: the v2 cards state centres as
# "static E pedal root", "tonal center of C", "low D drone". Bare A-G must
# be uppercase (an article "a" is not pitch A); marked forms (E1, Bb, D#)
# match either case. Full word match only, so "BPM" never reads as pitch B.
_REF_PITCH_FULL_RE = re.compile(
    r"[A-G](?![A-Za-z])|[A-Ga-g][#b\u266f\u266d]\d?|[A-Ga-g]\d")
_REF_KEYCMD_WORDS = frozenset(
    "root roots rooted centre centres centered centred center centers "
    "tonal tonality key keys drone drones pedal pedals modulate "
    "modulates modulated modulating modulation anchor anchors anchored "
    "chord chords".split())
_REF_WORD_RE = re.compile(r"[A-Za-z0-9#\u266f\u266d]+")

# Chord symbols and progressions ("Dm - Bb - F - C"): two or more chord
# tokens joined by dashes, slashes, commas or pipes.
_REF_CHORD_BIT = r"[A-G][#b\u266f\u266d]?(?:maj|min|dim|aug|sus\d*|add\d*|m(?![a-z]))?"
_REF_PROG_RE = re.compile(
    r"\b([A-G][#b\u266f\u266d]?(?:maj|min|dim|aug|sus\d*|add\d*|m(?![a-z]))?"
    r"(?:\s*[-\u2013\u2014/,|]\s*" + _REF_CHORD_BIT + r")+)\b")


def _keycmd_centres(text: str) -> list[str]:
    """Pitch names the rule anchors to a centre-word, in textual order.

    Tier 1 is a pitch within two words of a centre-word ("F# root
    drone"); with none, tier 2 takes pitches within six ("root static on
    C"). Pitches adjacent to an included one join the chain ("D# / G#").
    Melodic stabs far from any centre-word ("m2 (G) stabs") stay out, and
    so does the article "a".
    """
    words = [m.group(0) for m in _REF_WORD_RE.finditer(text)]
    pitch_idx = [i for i, w in enumerate(words)
                 if _REF_PITCH_FULL_RE.fullmatch(w)]
    key_idx = [i for i, w in enumerate(words)
               if w.lower() in _REF_KEYCMD_WORDS]
    if not pitch_idx or not key_idx:
        return []
    near = {p for p in pitch_idx for k in key_idx if abs(p - k) <= 2}
    if not near:
        near = {p for p in pitch_idx for k in key_idx if abs(p - k) <= 6}
    changed = True
    while changed:
        changed = False
        for p in pitch_idx:
            if p not in near and any(abs(p - q) <= 2 for q in near):
                near.add(p)
                changed = True
    return [words[p] for p in sorted(near)][:4]

# Unambiguous instrument/effect words. Deliberately narrow: "room tails" or a
# "root pedal" are arrangement/texture, not a rig, and marking them would
# mislabel note choices as sound design. A rule that also names note content
# (riff, line, pattern, …) keeps its notes reading.
_REF_TIMBRE_RE = re.compile(
    r"\b(guitars?|pianos?|synths?|synthesizers?|strings?|brass|organs?|"
    r"rhodes|wurlitzer|mellotron|808|909|drum machines?|reverbs?|delays?|"
    r"distortion|fuzz|overdrive|chorus(es)?|flangers?|phaser|filters?|eq|"
    r"compressors?|timbres?|tones?|mix|production|samples?|patch(es)?|"
    r"presets?|amps?|amplifiers?|cabinets?|tape|vinyl|bitcrush|wavetable)\b",
    re.I,
)
_REF_NOTECONTENT_RE = re.compile(
    r"\b(melod\w+|harmon\w+|rhythm|groove|groovy|pattern|riff|lines?|"
    r"notes?|chords?|progressions?|basslines?|motifs?|phrases?|"
    r"arps?|arpeggios?|beats?)\b",
    re.I,
)
# Arrangement/shape words: a rule about form that happens to mention an
# effect word ("density builds to a peak ... into ambient reverb decay")
# is arrangement, not a rig choice, and must not be marked sound design.
_REF_ARRANGE_RE = re.compile(
    r"\b(arrange\w*|arcs?|sections?|structures?|builds?|buildups?|peaks?|"
    r"collaps\w+|density|intros?|outros?|verses?|chorus(es)?|breakdowns?|"
    r"movements?|bars?|form)\b",
    re.I,
)

_REF_BRIEF_BUDGET = 1600


def _reframe_rule(rule: str) -> str:
    """One imitation rule, reframed so it cannot contradict the gearbox.

    A key/mode command, a pitch anchored to a centre-word, or a chord
    progression becomes the ref's centre plus an explicit stay-in-your-key
    — stated as information, never by quoting the original command (the
    LLM would obey the quote). A timbre/instrument rule is marked sound
    design so it shapes sounds, never note choices — unless it is really
    about arrangement or note content. Anything else passes through
    verbatim.
    """
    text = str(rule)
    prog = _REF_PROG_RE.search(text)
    if prog:
        return ("ref's centre is %s; your loop stays in its own key"
                % prog.group(1).strip())
    centre = _REF_CENTRE_RE.search(text)
    if centre:
        heard = next(g for g in centre.groups() if g)
        return ("ref's centre is %s; your loop stays in its own key"
                % heard.strip())
    centres = _keycmd_centres(text)
    if centres:
        return ("ref's centre is %s; your loop stays in its own key"
                % ", ".join(centres))
    if (_REF_TIMBRE_RE.search(text)
            and not _REF_NOTECONTENT_RE.search(text)
            and not _REF_ARRANGE_RE.search(text)):
        return "sound design, not notes: %s" % text.strip()
    return text


def ref_brief(card_or_steer: dict[str, Any]) -> dict[str, Any]:
    """What a reference SAYS, for the composing LLM. Pure; no filesystem.

    Caps are deliberately tighter than apply_audio_ref's eight: an answer is one
    phrase, and six imperatives already exceed what four bars can honour.
    Sections and uncertain travel ahead of the excerpt under a shared
    1600-char budget, so the cut takes the excerpt's tail — never them."""
    card = card_or_steer or {}
    note = card.get("ricardo_note") or None
    do = [_reframe_rule(x) for x in (card.get("imitation_do") or [])][:_REF_BRIEF_MAX_DO]
    dont = [_reframe_rule(x) for x in (card.get("imitation_dont") or [])][:_REF_BRIEF_MAX_DONT]
    sections = ["%s - %s" % (s.get("t"), s.get("label"))
                for s in (card.get("sections") or []) if isinstance(s, dict)]
    uncertain = [str(u) for u in (card.get("uncertain") or [])]
    head_len = sum(len(x) + 1 for x in sections + uncertain)
    full_excerpt = _strip_source_label(card.get("describe_excerpt")) or ""
    room = max(0, _REF_BRIEF_BUDGET - head_len)
    excerpt = full_excerpt[:room].rstrip()
    if len(full_excerpt) > room:
        excerpt = (excerpt + " [...]").strip()
    owner = str(card.get("ownership") or "unknown").lower()
    return {
        "identity": card.get("identity") or None,
        "ownership": owner,
        "imitation_do": do,
        "imitation_dont": dont,
        "sections": sections,
        "uncertain": uncertain,
        "excerpt": excerpt or None,
        "ricardo_note": (REF_OVERLAY_PREFIX + note) if note else None,
        "authority": REF_AUTHORITY_HIS if owner == "his" else REF_AUTHORITY,
        "guidance": REF_GUIDANCE,
        "heard_by": {
            "model": card.get("model"),
            "listened_at": card.get("created_at"),
            "contract": contract_version_of(card),
        },
    }


def reference_sequence(card: dict[str, Any], meta: dict[str, Any],
                      owner_notes: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return timestamped reference facts with their evidence provenance.

    Loudness windows are fixed 4-beat spans from file time zero. They do not
    establish a musical downbeat. Structural move thresholds are deliberately
    phrase-sized: thinning needs four windows at or below 70% of the preceding
    eight-window median and recovery within four windows; sustained peaks and
    measured endings also need four windows. Delayed entries require typed
    layer evidence and are never inferred from loudness alone.
    """
    card, meta = card or {}, meta or {}
    verified = meta.get("listen_verified") is True
    measured = meta.get("measured")
    has_measured = isinstance(measured, dict) and bool(measured)
    owner_notes = owner_notes or {}
    ref_id = meta.get("ref_id") or card.get("ref_id")
    tempo = card.get("tempo") or {}
    measured_bpm = measured.get("tempo_bpm") if has_measured else None
    beat_times = measured.get("beat_times_s") if has_measured else None
    valid_beats = ([float(t) for t in beat_times
                    if isinstance(t, (int, float)) and not isinstance(t, bool)
                    and math.isfinite(float(t))]
                   if isinstance(beat_times, list) else [])
    downbeat = owner_notes.get("downbeat_s")
    aligned = (isinstance(downbeat, (int, float)) and not isinstance(downbeat, bool)
               and downbeat >= 0 and bool(valid_beats))
    meter = card.get("meter") or {}
    beats_per_bar = meter.get("beats_per_bar")
    if (isinstance(beats_per_bar, bool) or not isinstance(beats_per_bar, int)
            or beats_per_bar <= 0):
        beats_per_bar = 4
        meter_assumption = "meter assumption: 4/4"
    else:
        meter_assumption = f"meter assumption: {beats_per_bar}/4 from reference card"

    def ref_bar(time_s: float) -> int:
        downbeat_index = min(range(len(valid_beats)),
                             key=lambda i: abs(valid_beats[i] - float(downbeat)))
        beat_index = min(range(len(valid_beats)),
                         key=lambda i: abs(valid_beats[i] - time_s))
        return (beat_index - downbeat_index) // beats_per_bar + 1
    duration = meta.get("duration_s")
    if duration is None:
        duration = (meta.get("features") or {}).get("duration_s")
    sections = []
    raw_sections = card.get("sections") or []
    for raw in raw_sections:
        if not isinstance(raw, dict):
            continue
        t = raw.get("t")
        try:
            t_s = parse_time(t)
        except (TypeError, ValueError):
            continue
        if t_s is None:
            continue
        row = {"t": t, "t_s": t_s}
        if verified:
            for key in ("label", "energy", "layers_in", "layers_out",
                        "transition_in", "event", "density"):
                if key in raw and raw[key] is not None:
                    row[key] = raw[key]
            row["evidence"] = "model_heard"
            row["status"] = "observed"
        else:
            row["evidence"] = "model_unverified"
            row["status"] = "text_only"
        sections.append(row)
    # Owner timestamps are aligned only when they land on a card section
    # boundary (within 2 seconds); this keeps free-form times from becoming
    # structural claims about an unrelated section.
    def align_owner_time(value):
        try:
            time_s = parse_time(value)
        except (TypeError, ValueError):
            return None
        if time_s is None or not sections:
            return None
        nearest = min(sections, key=lambda section: abs(section["t_s"] - time_s))
        return nearest if abs(nearest["t_s"] - time_s) <= 2.0 else None

    owner_confirmations = []
    for value in owner_notes.get("confirmed_sections") or []:
        section = align_owner_time(value)
        if section:
            section["evidence"] = "owner_confirmed"
            section["status"] = "observed"
            if aligned:
                section["ref_bar"] = ref_bar(section["t_s"])
                section["ref_bar_evidence"] = "owner_confirmed"
            owner_confirmations.append({"kind": "section", "t": section["t"],
                                        "t_s": section["t_s"], "evidence": "owner_confirmed",
                                        "status": "observed"})
    aligned_layer_changes = []
    for change in owner_notes.get("confirmed_layer_changes") or []:
        if not isinstance(change, dict):
            continue
        section = align_owner_time(change.get("t"))
        if not section:
            continue
        layers_out = change.get("layers_out") or []
        layers_in = change.get("layers_in") or []
        if not layers_out and not layers_in:
            # An aligned confirmation that names no change is not evidence.
            continue
        aligned_change = {"t": section["t"], "t_s": section["t_s"],
                          "layers_out": layers_out,
                          "layers_in": layers_in,
                          "evidence": "owner_confirmed", "status": "observed"}
        aligned_layer_changes.append(aligned_change)
        owner_confirmations.append({"kind": "layer_change", **aligned_change})
    warnings = []
    if not verified:
        warnings.append("model-heard facts omitted: listen_verified is not true")
    if not sections:
        warnings.append("no timestamped sections in card")
    if owner_notes.get("downbeat_s") is not None and not aligned:
        warnings.append("downbeat alignment unavailable: confirmed downbeat and measured beat grid required")
    # An aligned confirmation that names no layer change is not evidence
    # (skipped above) — but it did align, so it must not count as a miss.
    requested_owner_facts = list(
        owner_notes.get("confirmed_sections") or []) + [
            change for change in
            owner_notes.get("confirmed_layer_changes") or []
            if not (isinstance(change, dict)
                    and not change.get("layers_out")
                    and not change.get("layers_in"))]
    if requested_owner_facts and len(owner_confirmations) < len(requested_owner_facts):
        warnings.append("one or more owner confirmations could not align to a card section within 2 seconds")

    loudness = None
    values = measured.get("energy_per_bar") if has_measured else None
    bar_len = measured.get("bar_len_s") if has_measured else None
    starts = measured.get("energy_bar_start_s") if has_measured else None
    if isinstance(values, list) and values and isinstance(bar_len, (int, float)) and bar_len > 0:
        windows = (starts if isinstance(starts, list) and len(starts) == len(values)
                   else [i * float(bar_len) for i in range(len(values))])
        loudness = {"windows_s": windows, "values": values,
                    "note": "loudness per fixed 4-beat window from 0:00 (not downbeat-aligned)",
                    "evidence": "measured"}
    moves = []
    if loudness:
        loudness["status"] = "observed"
        vals = values
        numeric = [v for v in vals if isinstance(v, (int, float))
                   and not isinstance(v, bool) and math.isfinite(float(v))]
        if numeric:
            median = sorted(numeric)[len(numeric) // 2]
        else:
            median = 0
        vals = [float(v) if isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(float(v)) else 0.0 for v in vals]
        # Loudness cannot identify an instrument entering. Only a later,
        # evidenced layers_in role absent from every earlier section can do
        # so: a role that returns after having been present is not delayed.
        owner_layers_by_t: dict[str, dict[str, list]] = {}
        for change in aligned_layer_changes:
            bucket = owner_layers_by_t.setdefault(
                change["t"], {"layers_in": [], "layers_out": []})
            bucket["layers_in"].extend(change.get("layers_in") or [])
            bucket["layers_out"].extend(change.get("layers_out") or [])
        first_heard: dict[str, float] = {}
        for layer in card.get("layers") or []:
            if not isinstance(layer, dict) or not isinstance(layer.get("role"), str):
                continue
            try:
                first_s = parse_time(layer.get("first_t")) \
                    if isinstance(layer.get("first_t"), str) else None
            except ValueError:
                continue
            if first_s is None:
                continue
            known = first_heard.get(layer["role"])
            first_heard[layer["role"]] = first_s if known is None else min(known, first_s)
        for section_index, section in enumerate(sections[1:], start=1):
            entering = section.get("layers_in") or []
            active_so_far = set()
            for earlier in sections[:section_index]:
                # Each earlier section contributes its net layer presence;
                # a role that left and returns was still present earlier.
                active_so_far.update(set(earlier.get("layers_in") or [])
                                     - set(earlier.get("layers_out") or []))
                owner_earlier = owner_layers_by_t.get(earlier["t"])
                if owner_earlier:
                    active_so_far.update(set(owner_earlier["layers_in"])
                                         - set(owner_earlier["layers_out"]))
            for role, first_s in first_heard.items():
                if first_s < section["t_s"]:
                    active_so_far.add(role)
            added = [role for role in entering if role not in active_so_far]
            owner_change = next((change for change in aligned_layer_changes
                                 if change["t"] == section["t"] and change["layers_in"]), None)
            if owner_change:
                owner_added = [role for role in owner_change["layers_in"]
                               if role not in active_so_far]
                if owner_added:
                    moves.append({"kind": "delayed_entry", "t_s": section["t_s"],
                                  "layers": owner_added, "evidence": ["owner_confirmed"],
                                  "status": "inferred"})
            elif verified and added:
                moves.append({"kind": "delayed_entry", "t_s": section["t_s"],
                              "layers": added, "evidence": ["model_heard"],
                              "status": "inferred"})

        # A thinning is a sustained low run against the local eight-window
        # median: every run window at or below 0.70 x baseline, four or more
        # windows, with a recovery to 0.90 x baseline within four windows.
        # A phrase wobble (a window or two) never qualifies on its own.
        i = 8
        while i < len(vals) - 4:
            ordered = sorted(vals[i - 8:i])
            pre_level = (ordered[3] + ordered[4]) / 2
            if pre_level <= 0 or vals[i] > pre_level * 0.70:
                i += 1
                continue
            end = i
            while end + 1 < len(vals) and vals[end + 1] <= pre_level * 0.70:
                end += 1
            recovery = next((j for j in range(end + 1, min(len(vals), end + 5))
                             if vals[j] >= pre_level * 0.90), None)
            if end - i + 1 >= 4 and recovery is not None:
                t_s = windows[i]
                return_t_s = windows[recovery]
                prior_sections = [s for s in sections if s["t_s"] <= t_s]
                sec = prior_sections[-1] if prior_sections else None
                span = float(windows[end]) + float(bar_len) - float(windows[i])
                section_end = duration or (t_s + span)
                next_section = None
                if sec:
                    following = [s for s in sections if s["t_s"] > sec["t_s"]]
                    if following:
                        next_section = min(following, key=lambda s: s["t_s"])
                        section_end = next_section["t_s"]
                # Layer changes authorize only a measured dip at that boundary:
                # layers_out starts the dip; layers_in ends it at the return.
                out_at_boundary = bool(
                    sec and abs(t_s - sec["t_s"]) <= 5.0
                )
                return_at_boundary = bool(
                    sec and next_section and sec["t_s"] <= t_s < section_end
                    and abs(t_s + span - next_section["t_s"]) <= 5.0
                )
                owner_change = next((change for change in aligned_layer_changes
                                     if sec and change["t"] == sec["t"]
                                     and change["layers_out"] and out_at_boundary), None)
                owner_return = next((change for change in aligned_layer_changes
                                     if return_at_boundary and next_section
                                     and change["t"] == next_section["t"]
                                     and change["layers_in"]), None)
                model_layer_change = bool(
                    verified and sec and (
                        (out_at_boundary and sec.get("layers_out"))
                        or (return_at_boundary and next_section.get("layers_in"))
                    )
                )
                layer_change = bool(model_layer_change or owner_change or owner_return)
                support = ("owner_confirmed" if owner_change or owner_return else "model_heard")
                moves.append({"kind": "thin_before_return", "t_s": t_s,
                              "evidence": ["measured"],
                              "span_s": span, "return_t_s": return_t_s,
                              "span_fraction_of_section": span / max(1, section_end - (sec["t_s"] if sec else t_s)),
                              **({"section_t": sec["t"]} if sec else {}),
                              "supported_by": (["measured", support] if layer_change else ["measured"]),
                              "status": "inferred",
                              "authorizes_layer_removal": layer_change,
                              **({} if layer_change else {"missing_evidence":
                                 "verified layer-change evidence or owner confirmation required"})})
            i = max(i + 1, end + 1)

        # Compare four-window means. Locate the first window reaching 90% of
        # the maximum (the onset of a plateau), then require 3/4 windows above
        # a 1.5x baseline; a single spike cannot establish a late peak. The
        # position test applies where the rise BEGAN: walk back from the
        # onset over the contiguous windows already at 1.5x baseline, so a
        # plateau that starts mid-track and keeps drifting up is not "late".
        # Same rule as piece_context.precedent_habits on MIDI layer counts.
        duration_value = float(duration) if isinstance(duration, (int, float)) else (
            float(windows[-1]) + float(bar_len))
        if len(vals) >= 4 and duration_value > 0:
            spans = [(sum(vals[i:i + 4]) / 4, i) for i in range(len(vals) - 3)]
            peak_mean = max(mean for mean, _ in spans)
            peak_start = next(i for mean, i in spans if mean >= peak_mean * 0.9)
            prior = sorted(vals[:peak_start])
            track = sorted(vals)
            if prior and track:
                prior_median = prior[len(prior) // 2]
                track_median = track[len(track) // 2]
                baseline = max(prior_median, track_median)
                sustained = sum(value >= baseline * 1.5 for value in vals[peak_start:peak_start + 4])
                rise_start = peak_start
                while rise_start > 0 and vals[rise_start - 1] >= baseline * 1.5:
                    rise_start -= 1
                if (float(windows[rise_start]) > duration_value * 0.70
                        and peak_mean > 1e-9
                        and baseline > 0 and sustained >= 3):
                    moves.append({"kind": "late_peak", "t_s": windows[rise_start],
                                  "span_windows": 4, "evidence": ["measured"],
                                  "status": "inferred"})

        # A measured strip ending is a quiet plateau ahead of the final
        # fade: first strip the trailing monotone decay to silence, then
        # require at least four windows at or below 0.60 x the median
        # before it. The fade alone never establishes one.
        if len(vals) >= 6:
            ordered_all = sorted(vals)
            overall_median = ordered_all[len(ordered_all) // 2]
            fade_start = len(vals)
            if overall_median > 0 and vals[-1] <= overall_median * 0.10:
                fade_start = len(vals) - 1
                while (fade_start > 0
                       and vals[fade_start - 1] >= vals[fade_start]):
                    fade_start -= 1
            body = vals[:fade_start]
            for start in range(1, len(body) - 3):
                before = sorted(body[:start])
                baseline_before = before[len(before) // 2] if before else 0
                tail = body[start:]
                if (len(tail) >= 4 and baseline_before > 0
                        and all(value <= baseline_before * 0.60 for value in tail)):
                    moves.append({"kind": "strip_ending", "t_s": windows[start],
                                  "span_windows": len(tail), "evidence": ["measured"],
                                  "status": "inferred"})
                    break
        # Heard or owner layers_out on the final section still counts as a
        # strip ending even when the measured tail is only the final fade.
        if sections:
            final = sections[-1]
            owner_out = next((change for change in aligned_layer_changes
                              if change["t"] == final["t"]
                              and change["layers_out"]), None)
            if owner_out:
                moves.append({"kind": "strip_ending", "t_s": final["t_s"],
                              "layers": list(owner_out["layers_out"]),
                              "evidence": ["owner_confirmed"],
                              "status": "inferred"})
            elif verified and final.get("layers_out"):
                moves.append({"kind": "strip_ending", "t_s": final["t_s"],
                              "layers": list(final["layers_out"]),
                              "evidence": ["model_heard"],
                              "status": "inferred"})
    return {
        "ref_id": ref_id,
        "contract_version": contract_version_of(card),
        "evidence_summary": {"delivery_verified": verified, "measured": has_measured,
                             "model_heard": verified,
                             "owner_confirmed": bool(owner_confirmations)},
        "warnings": warnings, "owner_confirmations": owner_confirmations, "assumptions": [
            "measured energy windows use file time zero and are not downbeat-aligned",
            meter_assumption],
        "tempo": {"grid_bpm": tempo.get("grid_bpm"),
                  "grid_evidence": "model_heard" if verified else "model_unverified",
                  "feel_bpm": tempo.get("feel_bpm"),
                  "feel_evidence": "model_heard" if verified else "model_unverified",
                  "measured_bpm": measured_bpm,
                  "measured_bpm_evidence": "measured" if has_measured else None,
                  "status": "observed"},
        "duration_s": duration, "sections": sections,
        "loudness": loudness, "moves": moves,
        "alignment": {"ref_bar_available": bool(aligned),
                      "reason": "owner-confirmed downbeat and measured beat grid" if aligned
                      else "no confirmed downbeat"},
        **({"the_trick": dict(card["the_trick"], evidence="model_heard",
                              status="observed")}
           if verified and isinstance(card.get("the_trick"), dict) else {}),
        **({"rules": [{"rule": rule.get("rule"), "t": rule.get("t"),
                       "t_s": parse_time(rule.get("t")) if rule.get("t") else None,
                       "ableton_move": rule.get("ableton_move"), "why": rule.get("why"),
                       "evidence": "model_heard", "status": "observed"}
                      for rule in (card.get("imitation") or {}).get("do", [])
                      if isinstance(rule, dict)]}
           if verified and isinstance((card.get("imitation") or {}).get("do"), list) else {}),
    }


def ref_steering(card: dict[str, Any]) -> dict[str, Any]:
    """The full steering bundle a composing call needs from one reference.

    Splits by MECHANISM, not by field: ``mods`` filter (auditable), ``tempo``
    narrows retrieval, ``register`` biases realization, ``mode_hint``/``prose``
    inform the LLM. Nothing here ranks — this project has blind-falsified every
    aggregate scorer it has tried, so a reference is never allowed to become one.
    """
    card = card or {}
    s = card.get("structure") or {}
    measured = card.get("measured_drum_grid") or {}
    measured_tempo = measured.get("tempo_bpm")
    if (isinstance(measured_tempo, bool)
            or not isinstance(measured_tempo, (int, float))
            or not math.isfinite(float(measured_tempo))
            or not 20 <= float(measured_tempo) <= 400):
        measured_tempo = None
    mods, unmapped = ref_to_mods(card)
    return {
        "ref_id": card.get("ref_id"),
        "identity": card.get("identity") or None,
        "mods": mods,
        # grid tempo for anything arithmetic; feel tempo is what he'd tap
        "tempo": (s.get("grid_bpm") or s.get("feel_bpm")
                  or measured_tempo),
        "feel_tempo": s.get("feel_bpm"),
        "register": s.get("register"),
        "mode_hint": s.get("modal_center"),
        "drum_grid": s.get("drum_grid"),
        "measured_drum_grid": measured or None,
        "ricardo_note": card.get("ricardo_note"),
        "usable": structure_is_usable(card),
        "unmapped": unmapped,
        "inherited_from": card.get("structure_from"),
        "contract_version": contract_version_of(card),
        # what the reference SAYS, so the melodic path stops dropping it
        "brief": ref_brief(card),
    }


def structure_is_usable(card: dict[str, Any]) -> bool:
    """True when a card's structure carries at least one field that can steer."""
    s = card.get("structure") or {}
    return (any(s.get(k) is not None for k in STEERING_FIELDS)
            or bool((card.get("measured_drum_grid") or {}).get("steps")))


def _inherit_parent_structure(refs: dict[str, Any]) -> int:
    """Moment children with no readable structure of their own borrow the parent's.

    A 45-second cut of a track has the parent's tempo, mode and register by
    construction — it IS that track. Without this a moment clip is unsteerable
    even when its parent is fully described. Marked ``structure_from`` so the
    provenance never silently reads as first-hand.
    """
    filled = 0
    for card in refs.values():
        parent_id = card.get("parent_ref_id")
        if not parent_id or structure_is_usable(card):
            continue
        parent = refs.get(parent_id)
        if not parent or not structure_is_usable(parent):
            continue
        card["structure"] = dict(parent["structure"])
        card["structure_from"] = parent_id
        filled += 1
    return filled


def backup_overlays(
    dest_dir: Path | None = None,
    root: Path | None = None,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Snapshot the judgment layer: index + per-ref metadata and cards.

    The .wav files are re-downloadable; Ricardo's ``ricardo_note`` / ``verdict``
    overlays and the Gemini briefs are not — and today they live in exactly one
    place each. This writes a small tar.gz (no audio, no MIDI) that restores with
    the returned ``tar xzf <file> -C <anchor>`` command. It guards against the realistic accident (a bad
    reindex, an overwritten meta.json), NOT against disk loss — store/ is
    gitignored, so copy the archive somewhere else to cover that.
    """
    import tarfile

    root = root or OUT_ROOT
    index_path = index_path or INDEX_PATH
    dest_dir = Path(dest_dir) if dest_dir else (ROOT / "store" / "backups")
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = dest_dir / f"audio_ref_overlays-{stamp}.tar.gz"

    targets: list[Path] = []
    n_verdicts = 0
    n_notes = 0
    if index_path.is_file():
        targets.append(index_path.resolve())
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name in ("batches", "backups"):
                continue
            for name in ("meta.json", "describe.txt", "card.json"):
                f = child / name
                if f.is_file():
                    targets.append(f.resolve())
            meta_f = child / "meta.json"
            if meta_f.is_file():
                try:
                    meta = json.loads(meta_f.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(meta, dict):
                    # A hand-mangled meta.json is still archived above; only
                    # the counting must not crash the whole backup on it.
                    continue
                try:
                    if normalize_verdict(meta.get("verdict")) != "unset":
                        n_verdicts += 1
                except ValueError:
                    # An unknown verdict word is a judgment we cannot parse,
                    # not a reason to refuse the snapshot.
                    pass
                if meta.get("ricardo_note"):
                    n_notes += 1

    # Anchor arcnames at the deepest directory containing everything — ROOT in
    # production, a temp dir under test. Never assume the caller's paths sit
    # under this checkout.
    if targets:
        anchor = Path(os.path.commonpath([str(t.parent) for t in targets]))
    else:
        anchor = ROOT
    with tarfile.open(archive, "w:gz") as tar:
        for t in targets:
            tar.add(t, arcname=str(t.relative_to(anchor)))
    return {
        "ok": True,
        "archive": str(archive),
        "file_count": len(targets),
        "size_bytes": archive.stat().st_size,
        "verdicts_captured": n_verdicts,
        "notes_captured": n_notes,
        "anchor": str(anchor),
        "restore": f"tar xzf {archive} -C {anchor}",
    }


def restore_ownership(
    registry_path: Path | None = None,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Restore the operator ownership registry by exact ref_id.

    This intentionally refuses partial/mismatched libraries: guessing from a
    parent prefix here would risk silently mislabeling a reference.
    """
    registry_path = Path(registry_path) if registry_path else ROOT / "tools" / "data" / "audio_ref_ownership.json"
    index_path = index_path or INDEX_PATH
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    data = load_index(index_path)
    refs = data.get("refs") or {}
    parents = [str(x.get("ref_id")) for x in registry.get("his_parents") or []]
    children = [str(x) for x in registry.get("his_moment_children") or []]
    his_ids = parents + children
    counts = registry.get("counts") or {}
    expected_total = int(counts.get("total", 0))
    expected_his = int(counts.get("his", len(his_ids)))
    expected_other = int(counts.get("other", expected_total - expected_his))
    if (len(refs) != expected_total or len(his_ids) != expected_his
            or expected_total != expected_his + expected_other
            or expected_other != expected_total - len(his_ids)
            or len(set(his_ids)) != len(his_ids)
            or any(rid not in refs for rid in his_ids)):
        raise ValueError(
            "ownership registry/index mismatch: "
            f"index={len(refs)} expected={expected_total}, "
            f"his={len(his_ids)} expected={expected_his}, "
            f"other={expected_other}"
        )
    # Every registry/index ref must have a concrete metadata target.  Skipping
    # an index entry would make the restore look successful while leaving the
    # ownership overlay only partially applied.
    resolved_dirs: set[Path] = set()
    resolved_meta: set[Path] = set()
    for rid, card in refs.items():
        if not isinstance(card, dict) or not card.get("out_dir"):
            raise ValueError(f"ownership restore target missing out_dir: {rid}")
        out_dir = ensure_out_dir_contained(Path(card["out_dir"]))
        if not out_dir.is_dir():
            raise ValueError(f"ownership restore target directory missing: {out_dir}")
        out_dir_resolved = out_dir.resolve()
        meta_resolved = (out_dir / "meta.json").resolve()
        if out_dir_resolved in resolved_dirs or meta_resolved in resolved_meta:
            raise ValueError(
                f"duplicate ownership restore target: {out_dir_resolved}"
            )
        resolved_dirs.add(out_dir_resolved)
        resolved_meta.add(meta_resolved)
    his_set = set(his_ids)
    for rid, card in refs.items():
        card["ownership"] = "his" if rid in his_set else "other"
    target_meta: list[tuple[dict[str, Any], Path]] = []
    original_meta: dict[Path, bytes | None] = {}
    parsed_meta: dict[Path, dict[str, Any]] = {}
    for card in refs.values():
        out_dir = ensure_out_dir_contained(Path(card["out_dir"]))
        meta_path = out_dir / "meta.json"
        original_meta[meta_path] = meta_path.read_bytes() if meta_path.is_file() else None
        parsed = json.loads(original_meta[meta_path].decode("utf-8")) \
            if original_meta[meta_path] is not None else {}
        if not isinstance(parsed, dict):
            raise ValueError(f"malformed ownership target metadata: {meta_path}")
        parsed_meta[meta_path] = parsed
        target_meta.append((card, meta_path))
    original_index = Path(index_path).read_bytes() if Path(index_path).is_file() else None
    # Persist metadata before the index; every target was parsed above, before
    # any mutation. Roll back all prior atomic writes if a later one fails.
    try:
        for card, meta_path in target_meta:
            meta = parsed_meta[meta_path]
            meta["ownership"] = card["ownership"]
            write_json_atomic(meta_path, meta)
        save_index(data, index_path)
    except Exception:
        for meta_path, original in original_meta.items():
            if original is None:
                try:
                    meta_path.unlink()
                except FileNotFoundError:
                    pass
            else:
                _write_bytes_atomic(meta_path, original)
        if original_index is None:
            try:
                Path(index_path).unlink()
            except FileNotFoundError:
                pass
        else:
            _write_bytes_atomic(Path(index_path), original_index)
        raise
    return {"ok": True, "total": len(refs), "his": len(his_set),
            "other": len(refs) - len(his_set), "index_path": str(index_path),
            "registry_path": str(registry_path)}


def enrich_structure(
    root: Path | None = None, index_path: Path | None = None
) -> dict[str, Any]:
    """Re-parse describe.txt structure into meta + index (no re-listen)."""
    return reindex_audio_refs(root=root, index_path=index_path)


def _mount_point_of(path: Path) -> Path:
    """Walk up from path to its mount point (patchable so tests can fake volumes)."""
    cur = Path(path)
    while not os.path.ismount(cur):
        if cur == cur.parent:
            return cur
        cur = cur.parent
    return cur


def _device_of(path: str | Path) -> int:
    """st_dev of a path (patchable so tests can fake a second volume)."""
    return os.stat(path).st_dev


def _require_external_dest(dest: str | Path, root: Path | None = None) -> Path:
    """An offload destination on a mounted, non-root volume — or refuse.

    Offloading to the same (nearly full) internal disk would be worse than
    useless, and a destination whose volume is not mounted silently "succeeds"
    into a directory a later mount would shadow. Refuse before touching a ref.
    The mount-point walk alone is not enough on macOS: /System/Volumes/Data
    is a mount point distinct from / yet the same internal disk, so also
    refuse when the destination shares a device with the refs root. The
    device is probed on the destination itself — or its nearest existing
    ancestor when it does not exist yet — because a volume's mount root
    lives inside /Volumes on the internal disk: the PARENT's device would
    falsely read as internal.
    """
    root = Path(root) if root else OUT_ROOT
    dest = Path(dest).expanduser()
    if not dest.is_absolute():
        raise ValueError(f"offload destination must be an absolute path: {dest}")
    if not dest.parent.is_dir():
        raise ValueError(
            f"offload destination volume is not mounted: {dest.parent} does not exist"
        )
    mount = _mount_point_of(dest)
    if mount == Path(os.sep):
        raise ValueError(
            f"refusing to offload to the root volume: {dest} is on {mount}"
        )
    probe = dest
    while not probe.exists():
        probe = probe.parent
    if _device_of(probe) == _device_of(root):
        raise ValueError(
            f"refusing to offload onto the store's own device: {dest} shares "
            f"a device with {root}"
        )
    return dest


def _offload_candidate_dirs(root: Path) -> list[Path]:
    """Ref dirs under root worth considering for offload/restore, sorted."""
    if not root.is_dir():
        return []
    children: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name == "batches":
            continue
        try:
            children.append(ensure_out_dir_contained(child))
        except ValueError:
            continue
    return children


def _fsync_file(path: Path) -> None:
    """Flush a written file to stable storage before it is trusted.

    On darwin a plain fsync can stop at the cache, so ask for F_FULLFSYNC
    (fallback: ``os.fsync``) — the only local copy is unlinked right after
    this copy verifies. Opened read-only: fsync and F_FULLFSYNC both work
    on read-only fds, and the file (or the mode-preserving copy2 temp)
    may be write-protected.
    """
    import fcntl

    fd = os.open(path, os.O_RDONLY)
    try:
        if sys.platform == "darwin":
            try:
                fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            except (AttributeError, OSError):
                os.fsync(fd)
        else:
            os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    """Make a directory entry durable — called AFTER a rename, so the
    published name (not the temp entry) is what survives a crash."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def offload_sources(
    dest: str | Path,
    *,
    confirm: bool = False,
    root: Path | None = None,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Copy re-downloadable source.wav files to an external volume, reversibly.

    Dry run by default: the plan is reported and nothing moves. With
    ``confirm`` each copy lands on a destination TEMP name first and is
    sha256-verified before ``os.replace`` publishes it, so a corrupt or
    interrupted copy never sits at the destination name. A ref whose record
    says it was already offloaded but whose local wav hashes differently (an
    interrupted restore left a truncated file) fails instead of overwriting
    the good external copy with the truncated one — even when a re-listen
    has since stamped the changed local file as analyzed. Refs without
    meta.json (where the record would live) are skipped and reported, never
    invented.
    """
    root = root or OUT_ROOT
    index_path = index_path or INDEX_PATH
    dest_dir = _require_external_dest(dest, root=root)

    plan: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for child in _offload_candidate_dirs(root):
        wav = child / "source.wav"
        meta_path = child / "meta.json"
        has_meta = meta_path.is_file()
        if not wav.is_file():
            # Only report artifact-bearing dirs; a bare dir was never a ref.
            if _offload_record(child):
                skipped.append({"ref_id": child.name, "reason": "already offloaded"})
            elif has_meta or (child / "card.json").is_file():
                skipped.append({"ref_id": child.name, "reason": "no local source.wav"})
            continue
        if not has_meta:
            skipped.append({"ref_id": child.name, "reason": "no meta.json"})
            continue
        # Parse the metadata here so the confirm phase works from the plan: the
        # offload record and the analyzed source hash decide below whether a
        # local wav may overwrite its external copy. Unusable metadata skips
        # the ref — failing LATER would be mid-run, after other refs moved.
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            skipped.append({"ref_id": child.name,
                            "reason": f"unreadable meta.json: {type(exc).__name__}: {exc}"})
            continue
        if not isinstance(meta, dict):
            skipped.append({"ref_id": child.name, "reason": "meta.json is not an object"})
            continue
        record = meta.get("source_offloaded")
        record = record if isinstance(record, dict) else None
        item: dict[str, Any] = {
            "ref_id": child.name,
            "bytes": wav.stat().st_size,
            "dest": str(dest_dir / child.name / "source.wav"),
        }
        if record:
            # A ref with a record AND a local wav (crash between record and
            # unlink, or an interrupted restore) converges only when the local
            # file still hashes to what the record says. A later re-listen
            # stamping the changed local file as analyzed changes nothing:
            # the external copy is the record's bytes, never whatever the
            # local file became.
            item["recorded_sha256"] = record.get("sha256")
        plan.append(item)

    if not confirm:
        return {
            "ok": True,
            "dry_run": True,
            "dest": str(dest_dir),
            "plan": plan,
            "skipped": skipped,
            "skipped_count": len(skipped),
            "total_bytes": sum(item["bytes"] for item in plan),
        }

    # Fail closed before touching a ref: a corrupt index aborts here — not
    # mid-run after some refs already moved — and the baseline below (the ids
    # plus the exact index bytes) is what the post-offload reindex is held to.
    baseline_index = load_index(index_path)
    index_snapshot = _snapshot_bytes([index_path])
    refs_before = set(baseline_index.get("refs") or {})

    offloaded: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    bytes_freed = 0
    for item in plan:
        child = root / item["ref_id"]
        wav = child / "source.wav"
        target = Path(item["dest"])
        meta_path = child / "meta.json"
        try:
            src_sha = _file_sha256(wav)
            if not src_sha:
                failed.append({"ref_id": item["ref_id"],
                               "reason": "cannot hash local source.wav"})
                continue
            recorded = item.get("recorded_sha256")
            if recorded and src_sha != recorded:
                # Never os.replace over the external copy (whose sha equals
                # the record) with a source that differs from it.
                failed.append({
                    "ref_id": item["ref_id"],
                    "reason": "local source.wav sha256 differs from the offload "
                              "record (interrupted restore?); run --restore-sources "
                              "--confirm to move this file aside as "
                              "source.wav.mismatch-<UTC stamp> and restore the "
                              "recorded copy",
                })
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.parent / f".{target.name}.copying"
            try:
                shutil.copy2(wav, tmp)
                # Durability before the only local copy goes away: flush the
                # temp file, verify it, publish — and only THEN make the
                # published name durable. The directory fsync must follow the
                # rename: fsyncing the temp's entry first would leave a
                # durable temp name but a non-durable destination name.
                _fsync_file(tmp)
                if _file_sha256(tmp) != src_sha:
                    # Never leave a corrupt copy on the destination: the temp
                    # is unlinked below and any previous copy stands untouched.
                    failed.append({"ref_id": item["ref_id"],
                                   "reason": "sha256 mismatch after copy"})
                    continue
                os.replace(tmp, target)
                _fsync_dir(target.parent)
            finally:
                tmp.unlink(missing_ok=True)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["source_offloaded"] = {
                "path": str(target),
                "sha256": src_sha,
                "bytes": item["bytes"],
                "at": _utc_now_iso(),
            }
            write_json_atomic(meta_path, meta)
            # The record must be durable before the local wav goes too: a
            # crash after the unlink with an unpersisted record would orphan
            # the external copy.
            _fsync_dir(meta_path.parent)
            wav.unlink()
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            failed.append({"ref_id": item["ref_id"], "reason": f"{type(exc).__name__}: {exc}"})
            continue
        offloaded.append({**item, "sha256": src_sha})
        bytes_freed += item["bytes"]

    reindex: dict[str, Any] | None = None
    if offloaded:
        # The index must survive the offload unchanged; verify via the normal
        # reindex path (never a direct index write from here). The invariant
        # is enforced, not just logged: a reindex that drops refs restores the
        # pre-offload index bytes and fails the run.
        try:
            count_after = reindex_audio_refs(root=root, index_path=index_path)["count"]
            refs_after = set(load_index(index_path).get("refs") or {})
            missing = sorted(refs_before - refs_after)
            reindex = {
                "refs_before": len(refs_before),
                "refs_after": count_after,
                "equal": not missing,
            }
            if missing:
                _restore_bytes(index_snapshot)
                reindex["missing_refs"] = missing
        except Exception as exc:
            # Report, don't raise: the offloaded/failed lists are the record
            # of what actually moved and must still reach the operator.
            reindex = {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": not failed and (reindex is None or reindex.get("equal") is True),
        "dry_run": False,
        "dest": str(dest_dir),
        "offloaded": offloaded,
        "offloaded_count": len(offloaded),
        "skipped": skipped,
        "skipped_count": len(skipped),
        "failed": failed,
        "failed_count": len(failed),
        "bytes_freed": bytes_freed,
        "reindex": reindex,
    }


def restore_sources(
    *,
    confirm: bool = False,
    root: Path | None = None,
) -> dict[str, Any]:
    """Copy offloaded source.wav files back from their external volume.

    Dry run by default. With ``confirm`` the external copy is verified against
    the recorded sha256, then copied to a sibling temp file
    (``.source.wav.restoring``) which is verified again BEFORE ``os.replace``
    publishes it as ``source.wav`` — every later path trusts that filename, so
    a partial write must never appear under it. The temp is unlinked on every
    failure path. A local wav that hashes differently from the record (an
    interrupted restore, or a re-listen over a changed file) is moved aside
    as ``source.wav.mismatch-<UTC stamp>`` — never deleted — and reported as
    ``moved_aside``; only a local wav still matching the record is skipped.
    Only after the restore is the ``source_offloaded`` record removed from
    meta.json. The external copy is never deleted — re-offload is the reverse
    operation.
    """
    root = root or OUT_ROOT

    plan: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for child in _offload_candidate_dirs(root):
        record = _offload_record(child)
        if not record:
            continue
        ref_id = child.name
        recorded_path = record.get("path")
        recorded_sha = record.get("sha256")
        if not recorded_path or not recorded_sha:
            skipped.append({"ref_id": ref_id, "reason": "incomplete offload record"})
            continue
        moved_aside: str | None = None
        if (child / "source.wav").is_file():
            if _file_sha256(child / "source.wav") == recorded_sha:
                skipped.append({"ref_id": ref_id, "reason": "local source.wav already present"})
                continue
            # A mismatched local wav was a dead end: offload refuses it and
            # restore used to skip it, so the good copy was unreachable.
            # Move it aside — never delete — and restore the recorded bytes.
            moved_aside = ("source.wav.mismatch-"
                           + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        if not Path(recorded_path).is_file():
            skipped.append({"ref_id": ref_id, "reason": f"offload copy not reachable: {recorded_path}"})
            continue
        recorded_bytes = record.get("bytes")
        if (recorded_bytes is None
                or isinstance(recorded_bytes, bool)
                or not isinstance(recorded_bytes, (int, float))
                or not math.isfinite(float(recorded_bytes))):
            # A missing, hand-edited or non-finite size cannot be trusted for
            # accounting — the external file's real size is the truth.
            bytes_count = Path(recorded_path).stat().st_size
        else:
            bytes_count = int(recorded_bytes)
        plan.append({
            "ref_id": ref_id,
            "bytes": bytes_count,
            "path": str(recorded_path),
            "recorded_sha": recorded_sha,
            "moved_aside": moved_aside,
        })

    if not confirm:
        return {
            "ok": True,
            "dry_run": True,
            "plan": plan,
            "skipped": skipped,
            "skipped_count": len(skipped),
            "total_bytes": sum(item["bytes"] for item in plan),
        }

    restored: list[dict[str, Any]] = []
    bytes_restored = 0
    for item in plan:
        child = root / item["ref_id"]
        wav = child / "source.wav"
        meta_path = child / "meta.json"
        external = Path(item["path"])
        tmp = child / ".source.wav.restoring"
        try:
            ext_sha = _file_sha256(external)
            if not ext_sha or ext_sha != item.get("recorded_sha"):
                failed.append({"ref_id": item["ref_id"], "reason": "sha256 mismatch against offload record"})
                continue
            shutil.copy2(external, tmp)
            _fsync_file(tmp)  # durable before it becomes source.wav
            if _file_sha256(tmp) != ext_sha:
                failed.append({"ref_id": item["ref_id"], "reason": "sha256 mismatch after copy back"})
                continue
            aside = item.get("moved_aside")
            if aside:
                # Only now that the good copy is verified on disk does the
                # mismatched local wav move aside — preserved, never deleted.
                os.replace(wav, child / aside)
            os.replace(tmp, wav)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                raise TypeError("meta.json is not an object")
            meta.pop("source_offloaded", None)
            write_json_atomic(meta_path, meta)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            failed.append({"ref_id": item["ref_id"], "reason": f"{type(exc).__name__}: {exc}"})
            continue
        finally:
            tmp.unlink(missing_ok=True)
        restored.append(item)
        bytes_restored += item["bytes"]
    return {
        "ok": not failed,
        "dry_run": False,
        "restored": restored,
        "restored_count": len(restored),
        "skipped": skipped,
        "skipped_count": len(skipped),
        "failed": failed,
        "failed_count": len(failed),
        "bytes_restored": bytes_restored,
    }


_FIND_WORD_RE = re.compile(r"[a-z0-9]+")


def _find_tier_words(*texts: Any) -> set[str]:
    """Whole words of one search tier. Word-boundary matching, so 'drone'
    no longer matches every card whose excerpt merely contains those letters
    inside longer words, and one-letter noise ('&', '1') never becomes a
    token. Single-letter NOTE names (a-g) survive: they are the key-phrase
    half of queries like 'D minor'."""
    out: set[str] = set()
    for text in texts:
        if text is None:
            continue
        out.update(w for w in _FIND_WORD_RE.findall(str(text).lower())
                   if len(w) >= 2 or w in "abcdefg")
    return out


_KEY_QUERY_LONG_RE = re.compile(
    r"\b([A-Ga-g])\s*([#b\u266f\u266d]?)\s*(major|minor|maj|min)\b",
    re.IGNORECASE)
# Chord-symbol shorthand ("Dm", "F#m"): the note letter must be uppercase
# so English "am" never reads as A minor.
_KEY_QUERY_SHORT_RE = re.compile(r"\b([A-G][#b\u266f\u266d]?)m\b")


def _find_key_phrase(query: str) -> tuple[tuple[int, str | None] | None, set[str]]:
    """A '<note> major|minor' (or '<note>m') query -> ((tonic pc, mode),
    consumed word tokens).

    The phrase is matched against structure.modal_center as ONE unit (see
    find_audio_refs), so its words leave the word-token set — 'D minor'
    must not also score every card that merely says 'minor'.
    """
    m = _KEY_QUERY_LONG_RE.search(query or "")
    if m:
        letter, acc, mode_word = m.group(1).upper(), m.group(2), m.group(3)
        mode = "minor" if mode_word.lower().startswith("min") else "major"
    else:
        m = _KEY_QUERY_SHORT_RE.search(query or "")
        if not m:
            return None, set()
        letter, acc, mode = m.group(1)[0].upper(), m.group(1)[1:], "minor"
        # "Dm" survives tier-splitting as one word ("dm"); "F#m" splits.
        consumed_extra = {(m.group(1) + "m").lower()}
        pc = (_KEY_TONIC_PC[letter]
              + (1 if acc == "#" else -1 if acc == "b" else 0)) % 12
        return (pc, mode), ({letter.lower(), "m"} | consumed_extra)
    acc = acc.replace("\u266f", "#").replace("\u266d", "b")
    pc = (_KEY_TONIC_PC[letter]
          + (1 if acc == "#" else -1 if acc == "b" else 0)) % 12
    consumed = {letter.lower()}
    if acc:
        consumed.add((letter + acc).lower())
    consumed.add(m.group(3).lower() if m.lastindex == 3 else "m")
    return (pc, mode), consumed


def compact_ref_view(card: dict[str, Any],
                     audio_refs_root: Path | None = None) -> dict[str, Any]:
    """The default find_audio_refs row: enough to choose a ref, not the card.
    ``detail=True`` returns the full card instead. ``audio_refs_root`` is
    where a card without out_dir keeps its Set evidence (default OUT_ROOT)."""
    struct = card.get("structure") or {}
    sections = card.get("sections") or []
    from tools.live_set_reader import _set_evidence_summary

    result = {
        "ref_id": card.get("ref_id"),
        "title": card.get("title"),
        "ownership": normalize_ownership(card.get("ownership")),
        "identity": card.get("identity"),
        "ricardo_note": card.get("ricardo_note"),
        # Same reframe as the brief: a key command must not reach the
        # composing LLM verbatim from either view.
        "dos": [_reframe_rule(str(x))
                for x in (card.get("imitation_do") or [])][:3],
        # R8: search exposes the quarantine state — flag AND reason — so a
        # cleared-then-reviewed row shows why it vanished from results.
        "suspect": bool(card.get("suspect")),
        "suspect_reason": card.get("suspect_reason"),
        # B3: unverified legacy cards read as False here, never unknown.
        "listen_verified": card.get("listen_verified") is True,
        "has_measured": card.get("has_measured") is True,
        "contract_version": card.get("contract_version"),
        "tempo": struct.get("grid_bpm") or struct.get("feel_bpm"),
        "density": struct.get("density"),
        "register": struct.get("register"),
        "sections": [str(s.get("label") or "") for s in sections
                     if isinstance(s, dict)],
        "uncertain": list(card.get("uncertain") or []),
    }
    set_evidence = _set_evidence_summary(card, audio_refs_root)
    if set_evidence is not None:
        result["set_evidence"] = set_evidence
    return result


def find_audio_refs(
    query: str | None = None,
    focus: str | None = None,
    tag: str | None = None,
    project: str | None = None,
    verdict: str | None = None,
    ownership: str | None = None,
    k: int = 5,
    index_path: Path | None = None,
    include_moments: bool = False,
    include_suspect: bool = False,
    detail: bool = False,
) -> list[dict[str, Any]]:
    data = load_index(index_path)
    refs = list((data.get("refs") or {}).values())
    by_id = {str(c.get("ref_id")): c for c in refs if c.get("ref_id")}
    focus_f = focus.strip().lower() if focus else None
    tag_f = tag.strip().lower() if tag else None
    project_f = project.strip().lower() if project else None
    verdict_f = normalize_verdict(verdict) if verdict else None
    if verdict_f == "unset" and verdict is None:
        verdict_f = None
    ownership_f = normalize_ownership(ownership) if ownership else None
    if ownership_f == "unknown" and not ownership:
        ownership_f = None
    tokens = _find_tier_words(query) if (query or "").strip() else set()
    # A key query is one phrase against modal_center, not word tokens.
    key_phrase, consumed = _find_key_phrase(query or "")
    tokens -= consumed

    scored: list[tuple[int, dict[str, Any]]] = []
    # Refs the query itself matched (key/token points, not the empty-query
    # base score): a matching parent absorbs its moment group even with
    # include_moments, while slices of an unmatched parent still show.
    matched_ids: set[str] = set()
    for card in refs:
        if focus_f and (card.get("focus") or "full").lower() != focus_f:
            continue
        tags = [str(t).lower() for t in (card.get("tags") or [])]
        if tag_f and tag_f not in tags:
            continue
        linked = [str(p).lower() for p in (card.get("linked_projects") or [])]
        if project_f and project_f not in linked:
            continue
        if verdict_f and normalize_verdict(card.get("verdict")) != verdict_f:
            continue
        if ownership_f and normalize_ownership(card.get("ownership")) != ownership_f:
            continue
        tally = ref_tally_of(card)
        if card.get("parent_ref_id") and not include_moments:
            # A moment slice carries its parent's track-level brief in v1
            # words; serving it by default steers composing off a stale
            # paraphrase. The parent stays discoverable, and the slice
            # itself is one explicit flag (or ref_id) away.
            continue
        suspect = _suspect_source(card, by_id)
        if suspect is not None:
            if not include_suspect:
                continue
            if suspect is not card:
                card = {**card, "suspect": True, "suspect_reason":
                        f"parent {suspect.get('ref_id')} is suspect: "
                        f"{suspect.get('suspect_reason') or 'no reason recorded'}"}
        struct = card.get("structure") or {}
        struct_bits = " ".join(str(v) for v in struct.values() if v is not None)
        # Tiers, not one haystack: what the ref IS (title/identity) outranks
        # what was written about it, so a title match beats an excerpt match.
        hi = _find_tier_words(card.get("title"), card.get("identity"))
        mid = _find_tier_words(" ".join(tags),
                               " ".join(str(x) for x in (card.get("imitation_do") or [])),
                               card.get("source"), card.get("ref_id"),
                               card.get("ricardo_note"))
        lo = _find_tier_words(card.get("describe_excerpt"), struct_bits)
        score = 0
        key_points = 0
        if key_phrase is not None:
            # One phrase against modal_center, tonic + mode with high
            # weight: an exact centre outranks everything, a tonic-only
            # centre still counts. A key query shows keyed refs only.
            want_pc, want_mode = key_phrase
            got_pc, got_mode = _key_parts(struct.get("modal_center"))
            if got_pc is not None and got_pc == want_pc:
                if got_mode is not None and want_mode is not None \
                        and got_mode != want_mode:
                    key_points = 4
                else:
                    key_points = 8
            if key_points == 0:
                continue
            score += key_points
        token_points = 0
        if tokens:
            for t in tokens:
                if t in hi:
                    token_points += 4
                if t in mid:
                    token_points += 2
                if t in lo:
                    token_points += 1
            if token_points == 0 and key_points == 0:
                continue
            score += token_points
        elif key_phrase is None:
            score = 1
        if key_points or token_points:
            matched_ids.add(str(card.get("ref_id")))
        if card.get("ricardo_note"):
            score += 1
        if normalize_verdict(card.get("verdict")) == "keep":
            score += 1
        # The judgment loop ranks down: each drop costs a point, so a
        # once-dropped ref still surfaces below its never-judged peers.
        score -= tally["dropped"]
        scored.append((score, card))
    # Moment slices collapse into their parent: one track, one row, and the
    # row served is always the parent's own, with the parent's own score
    # when the parent matched. Orphan slices without an explicit
    # parent_ref_id resolve through the same helper as the reindex
    # (explicit, parent: tag, stable id). With include_moments a group
    # still collapses when its parent matched the query; slices of an
    # unmatched parent stand alone.
    collapsed: dict[str, tuple[int, dict[str, Any]]] = {}

    def _place(key: str, score: int, emit: dict[str, Any],
               is_parent_own: bool) -> None:
        cur = collapsed.get(key)
        if cur is None:
            collapsed[key] = (score, emit)
        elif is_parent_own:
            collapsed[key] = (score, emit)
        elif str(cur[1].get("ref_id")) != key and score > cur[0]:
            collapsed[key] = (score, emit)

    for score, card in scored:
        rid = str(card.get("ref_id"))
        parent_id = _parent_ref_id(card, by_id)
        if parent_id and str(parent_id) in by_id:
            parent = by_id[str(parent_id)]
            if include_moments:
                if str(parent_id) in matched_ids:
                    # The parent matched: the group collapses into its
                    # own row even with moments requested.
                    continue
                _place(rid, score, card, True)
            else:
                if parent.get("suspect") is True and not include_suspect:
                    # Quarantined track: a matching orphan must not drag
                    # its suspect parent back into default search — the
                    # whole group stays out until cleared or explicitly
                    # asked for.
                    continue
                _place(str(parent_id), score, parent, rid == str(parent_id))
        else:
            _place(rid, score, card, True)
    ranked = sorted(collapsed.values(),
                    key=lambda x: (-x[0], str(x[1].get("created_at") or ""),
                                   str(x[1].get("ref_id"))))
    chosen = [c for _, c in ranked[: max(1, int(k))]]
    if detail:
        return chosen
    refs_root = Path(index_path).parent / "audio_refs" if index_path else None
    return [compact_ref_view(c, refs_root) for c in chosen]


_REF_TALLY_OUTCOMES = ("kept", "dropped", "survived")


def ref_tally_of(card: dict[str, Any] | None) -> dict[str, int]:
    """A ref's judgment tally, normalized. Pure; missing keys read as zero."""
    raw = (card or {}).get("judgment_tally") or {}
    out: dict[str, int] = {}
    for key in _REF_TALLY_OUTCOMES:
        try:
            out[key] = max(0, int(raw.get(key, 0)))
        except (TypeError, ValueError):
            out[key] = 0
    return out


def ref_is_shelved(card: dict[str, Any] | None) -> bool:
    """True when a ref has been dropped twice with no keep to balance it.

    The judgment loop's only automatic consequence is on ANSWERS: a shelved
    ref never answers (get_ref_card refuses it, steered or unsteered) until
    a KEEP lifts the shelf. Search keeps matching it — the tally only ranks
    it down — so the ledger never hides a ref from query matching."""
    tally = ref_tally_of(card)
    return tally["dropped"] >= 2 and tally["kept"] == 0


def ref_tallied_entries_of(card: dict[str, Any] | None) -> dict[str, str]:
    """Ledger entry ids already counted in a ref's tally, mapped to outcome.

    Pure; missing or malformed reads as empty. The tally counts each ledger
    row exactly once no matter which path (judge, sweep, attest) resolves it
    first — later paths see the id here and skip.
    """
    raw = (card or {}).get("tallied_entries") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(eid): str(outcome) for eid, outcome in raw.items()}


def record_ref_judgment(
    ref_id: str,
    outcome: str,
    index_path: Path | None = None,
    entry_id: str | None = None,
) -> dict[str, Any]:
    """Bump a ref's ``judgment_tally`` for one judgment-loop event.

    ``outcome`` is one of kept (judge_answer KEEP), dropped (judge_answer DROP
    or a sweep terminal drop with no prior DROP), survived (sweep terminal
    keep). Goes through the same index + meta.json write path as the other
    operator overlays, so the tally survives a reindex that keeps overlays.

    ``entry_id`` is the ledger row being counted: a row already present in
    the card's ``tallied_entries`` is returned unchanged, so a judge-time
    verdict, a later sweep, an attestation, or a retried call each count the
    same row exactly once. Calls without an entry id always bump (the legacy
    operator path).
    """
    outcome = str(outcome).strip().lower()
    if outcome not in _REF_TALLY_OUTCOMES:
        raise ValueError(f"outcome must be one of {_REF_TALLY_OUTCOMES}")
    rid = normalize_ref_id(ref_id)
    # The tally read-modify-write runs under the index lock so concurrent
    # judge/sweep writers serialize instead of losing counts on a stale
    # read; the write itself stays on the atomic save_index helper.
    with _index_lock(index_path):
        data = load_index(index_path)
        card = data["refs"].get(rid)
        if not card:
            raise KeyError(f"unknown ref_id: {rid}")
        tallied = ref_tallied_entries_of(card)
        if entry_id is not None and str(entry_id) in tallied:
            return card
        tally = ref_tally_of(card)
        tally[outcome] += 1
        card["judgment_tally"] = tally
        if entry_id is not None:
            tallied[str(entry_id)] = outcome
            card["tallied_entries"] = tallied
        data["refs"][rid] = card
        try:
            out = ensure_out_dir_contained(Path(card.get("out_dir") or ref_dir_for_id(rid)))
        except ValueError:
            out = None
        if out is not None and out.is_dir():
            meta_path = out / "meta.json"
            meta: dict[str, Any] = {}
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    meta = {}
            meta["judgment_tally"] = tally
            if entry_id is not None:
                meta["tallied_entries"] = tallied
            write_json_atomic(meta_path, meta)
        save_index(data, index_path)
    return card


def mark_ref_suspect(
    ref_id: str,
    reason: str,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Flag a card as suspect: out of steering and default search.

    Nothing is set by any automated step — the owner marks cards while
    triaging (e.g. templated v2 boilerplate, key contradictions) and clears
    them after review. Goes through the same index + meta.json write path as
    the other operator overlays, so the flag survives a reindex.
    """
    reason_s = str(reason or "").strip()
    if not reason_s:
        raise ValueError("a suspect mark needs a reason")
    rid = normalize_ref_id(ref_id)
    data = load_index(index_path)
    card = data["refs"].get(rid)
    if not card:
        raise KeyError(f"unknown ref_id: {rid}")
    card["suspect"] = True
    card["suspect_reason"] = reason_s
    data["refs"][rid] = card
    try:
        out = ensure_out_dir_contained(Path(card.get("out_dir") or ref_dir_for_id(rid)))
    except ValueError:
        out = None
    if out is not None and out.is_dir():
        meta_path = out / "meta.json"
        meta: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        meta["suspect"] = True
        meta["suspect_reason"] = reason_s
        write_json_atomic(meta_path, meta)
    save_index(data, index_path)
    return card


def clear_ref_suspect(
    ref_id: str,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Clear a suspect flag after owner review. Never automated."""
    rid = normalize_ref_id(ref_id)
    data = load_index(index_path)
    card = data["refs"].get(rid)
    if not card:
        raise KeyError(f"unknown ref_id: {rid}")
    try:
        out = ensure_out_dir_contained(Path(card.get("out_dir") or ref_dir_for_id(rid)))
    except ValueError:
        out = None
    meta: dict[str, Any] | None = None
    if out is not None and out.is_dir():
        meta_path = out / "meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
            if isinstance(meta, dict):
                meta.pop("suspect", None)
                meta.pop("suspect_reason", None)
                write_json_atomic(meta_path, meta)
            else:
                meta = None
    if out is not None and out.is_dir():
        # Rebuild the index row from the validated on-disk card so a stale
        # row (e.g. superseded prose) cannot resurface once cleared.
        try:
            card = card_from_out_dir(out, meta)
        except ValueError:
            card = dict(card)
    else:
        card = dict(card)
    card.pop("suspect", None)
    card.pop("suspect_reason", None)
    data["refs"][rid] = card
    save_index(data, index_path)
    return card


def stamp_provenance(
    root: Path | None = None,
    index_path: Path | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """X7 one-off: stamp the legacy chat-heard cards ``listener_mode``.

    ``interactive`` is ONLY for cards heard inside the shared multi-track
    Gemini chat conversation. Those are exactly the v2 card.json dirs with
    no pipeline-listen evidence in meta.json: no agy argv (the per-track
    listener records its argv under evidence) and no parent_ref_id (moment
    slices never ran a listener). A bare batch_id does NOT exclude: batch
    runs stamp it for non-listen work too, and half the legacy cards carry
    one with no argv, no attempts and no model. Anything already stamped
    is left alone; dirs without a v2 card.json (midi-only, failed fetches)
    never had a listener and are skipped. ``exact_listener_model`` is backfilled
    from the meta.json ``agy_model`` the run already recorded (evidence,
    not invention); refs whose model was never recorded keep no model
    rather than a guessed one. Dry run by default; apply=True writes
    meta.json files and the index through the same atomic path as the other
    overlays. Never run on the real store here — the orchestrator runs it.
    """
    root = root or OUT_ROOT
    index_path = index_path or INDEX_PATH
    data = load_index(index_path)
    stamped: list[str] = []
    model_backfilled: list[str] = []
    already = 0
    skipped_no_listener = 0
    skipped_pipeline = 0
    for child in sorted(root.iterdir()) if root.is_dir() else []:
        if not child.is_dir() or child.name == "batches":
            continue
        if not _dir_is_filed_ref(child):
            continue
        meta_path = child / "meta.json"
        try:
            meta = (json.loads(meta_path.read_text(encoding="utf-8"))
                    if meta_path.is_file() else {})
        except (OSError, json.JSONDecodeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if normalize_listener_mode(meta.get("listener_mode")) is not None:
            already += 1
            continue
        contract = None
        card_path = child / "card.json"
        if card_path.is_file():
            try:
                heard = json.loads(card_path.read_text(encoding="utf-8"))
                contract = heard.get("contract_version") \
                    if isinstance(heard, dict) else None
            except (OSError, json.JSONDecodeError):
                contract = None
        if contract != CARD_CONTRACT_VERSION:
            skipped_no_listener += 1
            continue
        evidence = meta.get("evidence") or {}
        if not isinstance(evidence, dict):
            evidence = {}
        if evidence.get("argv") or meta.get("parent_ref_id"):
            skipped_pipeline += 1
            continue
        stamped.append(child.name)
        model = meta.get("exact_listener_model") or meta.get("agy_model")
        if model:
            model_backfilled.append(child.name)
        if not apply:
            continue
        meta["listener_mode"] = "interactive"
        if model and not meta.get("exact_listener_model"):
            meta["exact_listener_model"] = model
        # B3: a chat-heard legacy card was never proof-verified — stamp
        # that explicitly so search treats it as unverified, not unknown.
        meta["listen_verified"] = False
        write_json_atomic(meta_path, meta)
        card = data["refs"].get(child.name)
        if card is not None:
            card["listener_mode"] = "interactive"
            if model and not card.get("exact_listener_model"):
                card["exact_listener_model"] = model
            card["listen_verified"] = False
            data["refs"][child.name] = card
    report = {
        "ok": True,
        "applied": bool(apply),
        "stamped": stamped,
        "stamped_count": len(stamped),
        "model_backfilled": model_backfilled,
        "already": already,
        "skipped_no_listener": skipped_no_listener,
        "skipped_pipeline": skipped_pipeline,
    }
    if apply:
        save_index(data, index_path)
    return report


def tag_audio_ref(
    ref_id: str,
    tags: list[str] | None = None,
    project: str | None = None,
    note: str | None = None,
    verdict: str | None = None,
    ownership: str | None = None,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Tag / link project / attach operator overlays.

    ``ownership`` is explicitly operator-set (his|other|unknown); a listening
    model is never permitted to infer or overwrite it.
    """
    rid = normalize_ref_id(ref_id)
    data = load_index(index_path)
    card = data["refs"].get(rid)
    if not card:
        raise KeyError(f"unknown ref_id: {rid}")
    existing = [str(t) for t in (card.get("tags") or [])]
    for t in tags or []:
        t = str(t).strip()
        if t and t not in existing:
            existing.append(t)
    card["tags"] = existing
    if project:
        linked = [str(p) for p in (card.get("linked_projects") or [])]
        if project not in linked:
            linked.append(project)
        card["linked_projects"] = linked
    if note is not None:
        note_s = str(note).strip()
        card["ricardo_note"] = note_s or None
    if verdict is not None:
        card["verdict"] = normalize_verdict(verdict)
        card["verdict_at"] = _utc_now_iso()
    if ownership is not None:
        card["ownership"] = normalize_ownership(ownership)
    data["refs"][rid] = card
    # also patch meta.json if present and contained
    try:
        out = ensure_out_dir_contained(Path(card.get("out_dir") or ref_dir_for_id(rid)))
    except ValueError:
        out = None
    if out is not None and out.is_dir():
        meta_path = out / "meta.json"
        meta: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        meta["tags"] = existing
        meta["linked_projects"] = list(card.get("linked_projects") or [])
        if "ricardo_note" in card:
            meta["ricardo_note"] = card.get("ricardo_note")
        if verdict is not None:
            meta["verdict"] = card.get("verdict")
            meta["verdict_at"] = card.get("verdict_at")
        if ownership is not None:
            meta["ownership"] = card.get("ownership")
        write_json_atomic(meta_path, meta)
    save_index(data, index_path)
    return card


_DESCRIBE_VERSIONS: dict[str, dict[str, Any]] = {
    "v2": {"prompt": DESCRIBE_PROMPT_V2, "schema": V2_SCHEMA,
           "validate": validate_card_v2, "contract": CARD_CONTRACT_VERSION},
    "v3": {"prompt": DESCRIBE_PROMPT_V3, "schema": V3_SCHEMA,
           "validate": validate_card_v3, "contract": V3_CONTRACT_VERSION},
}


def run_agy_describe_v2(
    *,
    wav: Path,
    prompt_extra: str | None,
    focus: str = "full",
    duration_s: float | None = None,
    timeout_s: int = 180,
    model: str | None = None,
    measured: dict[str, Any] | None = None,
    verify_listen: bool = False,
    listen_token_cap: int | None = None,
    conversations_dir: str | Path | None = None,
    allow_unverified_listen: bool = False,
) -> dict[str, Any]:
    """Listen to one staged wav and return a validated v2 card. Kept for
    re-reading old cards; new listens use v3."""
    return _run_agy_describe(
        version="v2", wav=wav, prompt_extra=prompt_extra, focus=focus,
        duration_s=duration_s, timeout_s=timeout_s, model=model,
        measured=measured, verify_listen=verify_listen,
        listen_token_cap=listen_token_cap,
        conversations_dir=conversations_dir,
        allow_unverified_listen=allow_unverified_listen)


def run_agy_describe_v3(
    *,
    wav: Path,
    prompt_extra: str | None,
    focus: str = "full",
    duration_s: float | None = None,
    timeout_s: int = 180,
    model: str | None = None,
    measured: dict[str, Any] | None = None,
    midi_pitch_classes: list[str] | None = None,
    verify_listen: bool = False,
    listen_token_cap: int | None = None,
    conversations_dir: str | Path | None = None,
    allow_unverified_listen: bool = False,
) -> dict[str, Any]:
    """Listen to one staged wav and return a validated v3 card. See
    run_agy_describe_v2 for the harness contract; the version only selects
    prompt, schema and validator."""
    return _run_agy_describe(
        version="v3", wav=wav, prompt_extra=prompt_extra, focus=focus,
        duration_s=duration_s, timeout_s=timeout_s, model=model,
        measured=measured, midi_pitch_classes=midi_pitch_classes,
        verify_listen=verify_listen, listen_token_cap=listen_token_cap,
        conversations_dir=conversations_dir,
        allow_unverified_listen=allow_unverified_listen)


def _run_agy_describe(
    *,
    version: str,
    wav: Path,
    prompt_extra: str | None,
    focus: str = "full",
    duration_s: float | None = None,
    timeout_s: int = 180,
    model: str | None = None,
    measured: dict[str, Any] | None = None,
    midi_pitch_classes: list[str] | None = None,
    verify_listen: bool = False,
    listen_token_cap: int | None = None,
    conversations_dir: str | Path | None = None,
    allow_unverified_listen: bool = False,
) -> dict[str, Any]:
    """One staged listen for either card version. Shared harness: single
    structured print turn, schema file, allowlisted env, staged-audio-only,
    one repair retry, blind prompt, usage tally, reconciliation.

    The model receives a temporary directory containing only the audio.  It
    returns JSON on stdout; artifact persistence is owned by ``analyze`` so a
    failed attempt cannot overwrite the last good card or rendered brief.
    Full focus receives no measured change points; other focuses retain labelled
    level hints, never measured tempo or key. The returned card is compared with
    measurement (agree/disagree/model_only per field) after the listen.
    agy runs with cwd set to the staged directory (the audio and nothing
    else) so the repo can never become its workspace.
    With ``verify_listen`` the validated card is held back until the agy
    conversation record for the run shows a view_file step on the staged
    audio and the token tally fits ``listen_token_cap`` (fail closed, no
    automatic retry); the rejection rides in ``listen_rejected``.
    """
    spec = _DESCRIBE_VERSIONS[version]
    agy = _which("agy")
    if not agy:
        return {"ok": False, "error": "agy not found on PATH"}
    effective_model = _resolve_model(model).strip() or DEFAULT_AGY_MODEL
    focus_n = normalize_focus(focus)
    dur = float(duration_s) if duration_s else None
    if dur is None or dur <= 0:
        info = ffprobe_info(wav)
        dur = float(info.get("duration_s") or 0) or 1.0
    if measured is None:
        try:
            measured = measure_audio(wav)
        except Exception:
            measured = {}
    extra = FOCUS_OVERLAYS.get(focus_n) or ""
    if prompt_extra and prompt_extra.strip():
        extra += f"Composer note (honor this): {prompt_extra.strip()}\n"
    base_prompt = spec["prompt"].format(
        name=wav.name,
        duration_s=dur,
        duration_mmss=format_mmss(dur),
        focus=extra,
        prompt_extra="",
        measured_note=measured_prompt_note(measured) if focus_n != "full" else "",
    )
    started = datetime.now(timezone.utc).isoformat()
    # The schema travels as a FILE, not argv: --json-schema takes a path, and
    # a 3 KB inline schema on every command line is noise next to the prompt.
    # The file lives outside the staged directory — the listener sees audio.
    schema_fd, schema_path = tempfile.mkstemp(prefix="audioref-schema-",
                                              suffix=".json")
    usages: list[dict[str, Any]] = []
    out: dict[str, Any] = {
        "ok": False,
        "model": effective_model,
        "timeout_s": int(timeout_s),
        "started_at": started,
    }
    try:
        with os.fdopen(schema_fd, "w", encoding="utf-8") as fh:
            json.dump(spec["schema"], fh)
        hint: str | None = None
        for attempt in (1, 2):
            with staged_audio_only(str(wav)) as staged:
                staged_dir = str(Path(staged).parent)
                # This is the supplied file's format, not a musical inference.
                # Unknown channel count must not invite a width guess either.
                try:
                    with wave.open(staged, "rb") as audio_input:
                        channels = audio_input.getnchannels()
                except (OSError, EOFError, wave.Error):
                    channels = None
                channel_note = ("Input is mono; stereo width cannot be inferred."
                                if channels == 1 else
                                "Input has two channels; report width only if heard."
                                if channels == 2 else
                                "Input channel count unknown; do not infer stereo width.")
                prompt = "%s\n\n%s" % (_listen_directive(staged),
                                       channel_note + "\n" + base_prompt)
                if hint:
                    prompt += ("\nYour previous output was rejected by the card "
                               f"validator: {hint}\nReturn exactly one JSON object "
                               "matching the schema. No prose, no markdown, no "
                               "commentary.")
                cmd = _agy_cmd_for(
                    effective_model, staged_dir, prompt,
                    timeout_s=int(timeout_s), schema=schema_path,
                )
                try:
                    proc = _run(cmd, timeout=int(timeout_s) + 60,
                                env=_agy_listen_env(), cwd=staged_dir)
                except subprocess.TimeoutExpired:
                    out.update({
                        "ok": False, "error": "agy timeout",
                        "argv": cmd, "attempts": attempt,
                        "usage": _merge_usages(usages),
                    })
                    return out
            out.update({
                "ok": False,
                "argv": cmd,
                "returncode": proc.returncode,
                "stdout_tail": (proc.stdout or "")[-500:],
                "stderr_tail": (proc.stderr or "")[-500:],
                "attempts": attempt,
            })
            if proc.returncode != 0:
                out["error"] = (proc.stderr or proc.stdout
                                or "agy failed").strip()[-500:]
                out["usage"] = _merge_usages(usages)
                return out
            usages.append(_agy_usage_from_stdout(proc.stdout or ""))
            try:
                parsed = _agy_json_result(proc.stdout or "")
                spec["validate"](parsed)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                out["error"] = f"invalid {version} card: {exc}"
                out["usage"] = _merge_usages(usages)
                # One repair retry, and only for a verified-but-malformed
                # answer: the validator message goes back into the prompt.
                # Quota and timeouts never retry here — the batch decides
                # that. A second turn cannot fix a run that never
                # listened, so attempt 1 is proof-checked first and a
                # no_listen / over_budget / unverifiable run is returned
                # as a listen rejection, never repaired.
                if attempt == 1 and classify_listen_failure(out) in (
                        "validation", "invalid_json"):
                    if verify_listen and not _attempt_verified_for_repair(
                            out, proc.stdout or "", staged,
                            listen_token_cap, conversations_dir,
                            allow_unverified_listen):
                        return out
                    hint = str(exc)
                    continue
                return out
            out["usage"] = _merge_usages(usages)
            if verify_listen:
                # Proof-of-listening + cost guard (B3): a validated card
                # is still held back when the run never opened the staged
                # audio or blew the token cap. Deliberately outside the
                # repair loop above — a rejected listen is never retried
                # here; the batch/operator decides that.
                conversation_id = _agy_conversation_id_from_stdout(
                    proc.stdout or "")
                out["conversation_id"] = conversation_id
                check = verify_agy_listen(
                    conversation_id=conversation_id,
                    staged_audio_path=staged,
                    usage=out["usage"],
                    token_cap=(int(listen_token_cap)
                               if listen_token_cap else
                               DEFAULT_LISTEN_TOKEN_CAP),
                    conversations_dir=conversations_dir,
                    allow_unverified=allow_unverified_listen,
                )
                out["listen_verification"] = check
                out["usage"] = check.get("usage") or out["usage"]
                out["usage_missing"] = bool(check.get("usage_missing"))
                if not check["ok"]:
                    out.update({
                        # Proof is independent evidence about delivery, not
                        # whether the model returned a valid card. Keep the
                        # card available for browsing while marking it
                        # unverified in the persisted provenance.
                        "ok": False,
                        "error": "listen rejected: %s" % check["reason"],
                        "card": parsed,
                        "reconciliation": reconcile_measured(
                            measured, parsed, focus=focus_n,
                            midi_pitch_classes=midi_pitch_classes),
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "listen_rejected": {
                            "reason": check["reason"],
                            "usage": check["usage"],
                            "conversation_id": conversation_id,
                        },
                    })
                    return out
            out["ok"] = True
            out["card"] = parsed
            out["reconciliation"] = reconcile_measured(
                measured, parsed, focus=focus_n,
                midi_pitch_classes=midi_pitch_classes)
            out["finished_at"] = datetime.now(timezone.utc).isoformat()
            return out
    finally:
        try:
            os.unlink(schema_path)
        except OSError:
            pass
    out["usage"] = _merge_usages(usages)
    return out


def _pass_b_section_hits_pitched(section: dict[str, Any]) -> bool:
    text = " ".join(
        [str(section.get("label") or "")]
        + [str(x) for x in (section.get("layers_in") or [])]
        + [str(x) for x in (section.get("layers_out") or [])]
    ).lower()
    return any(w in text for w in _PASS_B_PITCHED_WORDS)


def select_pass_b_windows(
    sections: list[dict[str, Any]] | None,
    duration_s: float,
    focus: str = "full",
) -> list[tuple[float, float]]:
    """Pick up to 3 non-overlapping 30-45 s windows for Pass B.

    Candidates are the Pass A section starts, ranked by energy (a pitched
    focus — harmony/melody — prefers sections naming a pitched voice
    first, energy still ordering within each group). Each window is
    centered on its section, clamped into the file. Shortfalls — peaks
    that cluster, or no sections at all — are filled from the largest
    empty gaps, so a long track still yields its windows. Files under
    30 s get none.
    """
    try:
        duration = float(duration_s)
    except (TypeError, ValueError):
        return []
    if duration < PASS_B_MIN_S:
        return []
    length = min(PASS_B_MAX_S, max(PASS_B_MIN_S, duration / 2.5))
    if duration >= 100.0:
        max_n = PASS_B_MAX_WINDOWS
    elif duration >= 60.0:
        max_n = 2
    else:
        max_n = 1
    try:
        focus_n = normalize_focus(focus)
    except ValueError:
        focus_n = "full"
    cands: list[tuple[int, float, bool]] = []
    for sec in (sections or []):
        if not isinstance(sec, dict):
            continue
        t_s = parse_time(sec.get("t"))
        if t_s is None:
            continue
        try:
            energy = int(sec.get("energy") or 0)
        except (TypeError, ValueError):
            energy = 0
        cands.append((energy, float(t_s),
                      _pass_b_section_hits_pitched(sec)))
    if focus_n in ("harmony", "melody"):
        cands.sort(key=lambda c: (not c[2], -c[0], c[1]))
    else:
        cands.sort(key=lambda c: (-c[0], c[1]))

    def _window_at(center: float) -> tuple[float, float]:
        start = min(max(center - length / 2.0, 0.0),
                    max(duration - length, 0.0))
        return (round(start, 1), round(start + length, 1))

    accepted: list[tuple[float, float]] = []

    def _overlaps(win: tuple[float, float]) -> bool:
        return any(win[0] < e and s < win[1] for s, e in accepted)

    for _energy, t_s, _pitched in cands:
        if len(accepted) >= max_n:
            break
        win = _window_at(min(max(t_s, 0.0), duration))
        if not _overlaps(win):
            accepted.append(win)
    while len(accepted) < max_n:
        spans = sorted(accepted)
        gaps: list[tuple[float, float]] = []
        prev = 0.0
        for s, e in spans:
            gaps.append((prev, s))
            prev = e
        gaps.append((prev, duration))
        gaps = [(a, b) for a, b in gaps if b - a >= length]
        if not gaps:
            break
        a, b = max(gaps, key=lambda g: (g[1] - g[0], -g[0]))
        accepted.append(_window_at((a + b) / 2.0))
    return sorted(accepted)


def run_pass_b_windows(
    *,
    wav: str | Path,
    v3_card: dict[str, Any] | None,
    duration_s: float,
    focus: str = "full",
    out_dir: str | Path,
    model: str | None = None,
    timeout_s: int = 120,
    cut_fn: Any = None,
    listen_fn: Any = None,
    verify_listen: bool = False,
    listen_token_cap: int | None = None,
    conversations_dir: str | Path | None = None,
    allow_unverified_listen: bool = False,
) -> dict[str, Any]:
    """Cut Pass B windows and listen to each (groove/drum grid/sound design).

    cut_fn defaults to stage_listenable_copy, listen_fn to run_agy_describe_v3 —
    tests inject fakes for both (a fake listen_fn must tolerate the
    verify_*/conversations_* keyword arguments the harness takes). A
    failed cut or listen becomes an ``ok: False`` entry, never an
    exception: Pass B is enrichment, and a deaf window must not sink the
    Pass A card. Returns ``{"windows": [...], "usage": merged}``.
    With ``verify_listen`` each window is proof-checked against the
    window token cap (DEFAULT_WINDOW_TOKEN_CAP unless overridden); a
    rejected window is an ``ok: False`` entry carrying the rejection.
    """
    cut = cut_fn or stage_listenable_copy
    listen = listen_fn or run_agy_describe_v3
    try:
        focus_n = normalize_focus(focus)
    except ValueError:
        focus_n = "full"
    selections = select_pass_b_windows(
        (v3_card or {}).get("sections"), duration_s, focus=focus_n)
    windows_dir = Path(out_dir) / "windows"
    try:
        windows_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    entries: list[dict[str, Any]] = []
    usages: list[dict[str, Any]] = []
    total = len(selections)
    for i, (start_s, end_s) in enumerate(selections, 1):
        entry: dict[str, Any] = {
            "window": i,
            "start_s": start_s,
            "end_s": end_s,
            "start": format_mmss(start_s),
            "end": format_mmss(end_s),
            "focus_aspect": _PASS_B_FOCUS_ASPECT,
            "ok": False,
        }
        wpath = windows_dir / f"window{i}.wav"
        try:
            cut(Path(wav), wpath, float(start_s), float(end_s))
        except Exception as exc:
            entry["error"] = f"cut failed: {type(exc).__name__}: {exc}"
            entries.append(entry)
            continue
        entry["wav"] = str(wpath)
        overlay = PASS_B_WINDOW_OVERLAY.format(
            i=i, n=total, start_mmss=entry["start"],
            end_mmss=entry["end"])
        try:
            res = listen(
                wav=wpath, prompt_extra=overlay, focus="full",
                duration_s=float(end_s) - float(start_s),
                timeout_s=int(timeout_s), model=model,
                verify_listen=verify_listen,
                listen_token_cap=(int(listen_token_cap)
                                  if listen_token_cap else
                                  DEFAULT_WINDOW_TOKEN_CAP),
                conversations_dir=conversations_dir,
                allow_unverified_listen=allow_unverified_listen)
        except Exception as exc:
            entry["error"] = (f"listen failed: {type(exc).__name__}: {exc}")
            entries.append(entry)
            continue
        if isinstance(res, dict) and isinstance(res.get("usage"), dict):
            usages.append(res["usage"])
        if not isinstance(res, dict) or not res.get("ok"):
            entry["error"] = str(
                (res or {}).get("error") or "window listen failed")
            entries.append(entry)
            continue
        card = res.get("card")
        try:
            validate_card_v3(card)
        except (TypeError, ValueError) as exc:
            entry["error"] = f"invalid window card: {exc}"
            entries.append(entry)
            continue
        entry["card"] = card
        entry["reconciliation"] = res.get("reconciliation")
        entry["usage"] = res.get("usage")
        entry["ok"] = True
        try:
            write_json_atomic(windows_dir / f"window{i}.card.json", card)
        except OSError:
            pass
        entries.append(entry)
    return {"windows": entries, "usage": _merge_usages(usages)}


def run_agy_describe(
    *,
    wav: Path,
    describe_path: Path,
    prompt_extra: str | None,
    focus: str = "full",
    duration_s: float | None = None,
    source_label: str | None = None,
    timeout_s: int = 180,
    model: str | None = None,
    listen_token_cap: int | None = None,
    conversations_dir: str | Path | None = None,
    allow_unverified_listen: bool = False,
) -> dict[str, Any]:
    """Always listen via local wav + --add-dir (YouTube URL is metadata only)."""
    agy = _which("agy")
    if not agy:
        return {"ok": False, "error": "agy not found on PATH"}

    model = (model or DEFAULT_AGY_MODEL).strip() or DEFAULT_AGY_MODEL
    focus = normalize_focus(focus)
    dur = float(duration_s) if duration_s else None
    if dur is None or dur <= 0:
        info = ffprobe_info(wav)
        dur = float(info.get("duration_s") or 0) or 1.0

    parts: list[str] = []
    overlay = FOCUS_OVERLAYS.get(focus) or ""
    if overlay:
        parts.append(overlay)
    if prompt_extra and prompt_extra.strip():
        parts.append(f"Composer note (honor this): {prompt_extra.strip()}\n")
    if source_label:
        parts.append(f"Source label (for Identity context only): {source_label}\n")
    parts.append(f"Listen to the audio file named {wav.name} in the workspace.")
    extra = "".join(parts)

    prompt = DESCRIBE_PROMPT.format(
        source_hint=f" in file {wav.name}",
        describe_path=str(describe_path),
        duration_s=dur,
        duration_mmss=format_mmss(dur),
        prompt_extra=extra,
    )
    cmd = [
        agy,
        "--model",
        model,
    ]
    if not any(model.endswith(f"-{e}") for e in ("low", "medium", "high")):
        cmd.extend(["--effort", "high"])
    cmd.extend([
        "--print-timeout",
        f"{max(1, math.ceil(float(timeout_s) / 60.0))}m",
    ])
    if AGY_SKIP_PERMISSIONS:
        cmd.append("--dangerously-skip-permissions")
    # The audio is UNTRUSTED input (a stranger's YouTube upload) fed to an
    # agent. --sandbox keeps terminal access restricted so a prompt-injection
    # in the recording can't turn "describe this track" into shell execution.
    # Unconditional for card listens (M3) — no environment override.
    # Workspace isolation (B3): the listener runs with cwd set to a staged
    # directory holding ONLY the wav — never wav.parent, which on a
    # re-listen also holds the previous describe.txt and meta.json the
    # model would otherwise copy instead of listening.
    with staged_audio_only(str(wav)) as staged:
        staged_dir = str(Path(staged).parent)
        prompt = "%s\n\n%s" % (_listen_directive(staged), prompt)
        cmd.append("--sandbox")
        cmd += [
            "--add-dir",
            staged_dir,
            # the envelope carries the conversation_id the proof check reads
            "--output-format",
            "json",
            "--print",
            prompt,
        ]
        try:
            proc = _run(cmd, timeout=timeout_s + 60,
                        env=_agy_listen_env(), cwd=staged_dir)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "agy timeout"}
        stdout = proc.stdout or ""
        conversation_id = _agy_conversation_id_from_stdout(stdout)
        verification = verify_agy_listen(
            conversation_id=conversation_id,
            staged_audio_path=staged,
            usage=_agy_usage_from_stdout(stdout),
            token_cap=(int(listen_token_cap) if listen_token_cap
                       else DEFAULT_LISTEN_TOKEN_CAP),
            conversations_dir=conversations_dir,
            allow_unverified=allow_unverified_listen)
        if not verification["ok"]:
            return {
                "ok": False,
                "error": "listen rejected: %s" % verification["reason"],
                "conversation_id": conversation_id,
                "listen_rejected": {
                    "reason": verification["reason"],
                    "usage": verification["usage"],
                    "conversation_id": conversation_id,
                },
            }
        # A cwd-confined agent writes describe.txt into its workspace
        # rather than the absolute out path: carry a valid staged write
        # home so the listen is not lost.
        staged_describe = Path(staged_dir) / "describe.txt"
        if (not _describe_looks_valid(describe_path)
                and _describe_looks_valid(staged_describe)):
            try:
                describe_path.write_text(
                    staged_describe.read_text(encoding="utf-8"),
                    encoding="utf-8")
            except OSError:
                pass

    result: dict[str, Any] = {
        "ok": describe_path.is_file() and _describe_looks_valid(describe_path),
        "conversation_id": conversation_id,
        "listen_verification": verification,
        "usage_missing": bool(verification.get("usage_missing")),
        "usage": verification.get("usage"),
        "returncode": proc.returncode,
        "stdout_tail": (proc.stdout or "")[-500:],
        "stderr_tail": (proc.stderr or "")[-500:],
        "focus": focus,
        "duration_s": dur,
    }
    if (
        not result["ok"]
        and proc.stdout
        and _describe_text_valid(proc.stdout)
    ):
        describe_path.write_text(proc.stdout.strip() + "\n", encoding="utf-8")
        result["ok"] = True
        result["recovered_from_stdout"] = True

    if result["ok"] and describe_path.is_file():
        text = describe_path.read_text(encoding="utf-8")
        mv = validate_moments(text, dur)
        result["moments_validation"] = mv
        if mv.get("all_out_of_range"):
            result["ok"] = False
            result["error"] = (
                "all # Moments timestamps exceed clip duration "
                f"({format_mmss(dur)})"
            )
        elif mv.get("out_of_range"):
            result["moments_warning"] = (
                f"out-of-range moment timestamps: {mv['out_of_range']}"
            )

    if not result["ok"]:
        result.setdefault(
            "error",
            "agy did not write a valid describe.txt (# Identity required)",
        )
    return result


def _describe_text_valid(text: str) -> bool:
    t = text.lstrip()
    return "# Identity" in t and len(t) > 120


def _describe_looks_valid(path: Path) -> bool:
    try:
        return path.is_file() and _describe_text_valid(path.read_text(encoding="utf-8"))
    except OSError:
        return False


def _write_text_atomic(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return path


def _write_bytes_atomic(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return path


def _snapshot_bytes(paths: list[Path]) -> dict[Path, bytes | None]:
    """Capture exact file bytes, including whether each file was absent."""
    snapshot: dict[Path, bytes | None] = {}
    for raw_path in paths:
        path = Path(raw_path)
        snapshot[path] = path.read_bytes() if path.is_file() else None
    return snapshot


def _restore_bytes(snapshot: dict[Path, bytes | None]) -> None:
    """Restore a file snapshot without exposing partially written JSON."""
    for path, original in snapshot.items():
        if original is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            _write_bytes_atomic(path, original)


def _persist_v2_artifacts(out_dir: Path, card: dict[str, Any]) -> dict[str, str | None]:
    """Atomically persist a validated v2 card and its deterministic rendering."""
    return _persist_card_artifacts(out_dir, card, version="v2")


def _persist_card_artifacts(out_dir: Path, card: dict[str, Any], *,
                            version: str,
                            suffix: str = "") -> dict[str, str | None]:
    """Atomically persist a validated card and its deterministic rendering."""
    render = render_describe_v3 if version == "v3" else render_describe_v2
    card_path = out_dir / f"card{suffix}.json"
    describe_path = out_dir / f"describe{suffix}.txt"
    old_card = card_path.read_bytes() if card_path.is_file() else None
    old_describe = describe_path.read_bytes() if describe_path.is_file() else None
    try:
        write_json_atomic(card_path, card)
        _write_text_atomic(describe_path, render(card))
    except Exception:
        # Keep the pair coherent if the second atomic replace fails.
        if old_card is None:
            try:
                card_path.unlink()
            except FileNotFoundError:
                pass
        else:
            _write_bytes_atomic(card_path, old_card)
        if old_describe is None:
            try:
                describe_path.unlink()
            except FileNotFoundError:
                pass
        else:
            _write_bytes_atomic(describe_path, old_describe)
        raise
    return {
        "card_sha256": _file_sha256(card_path),
        "describe_sha256": _file_sha256(describe_path),
    }


def analyze_audio_ref(
    source: str | None = None,
    modes: list[str] | None = None,
    prompt_extra: str | None = None,
    start: str | None = None,
    end: str | None = None,
    midi_tempo: float = 120.0,
    model: str | None = None,
    focus: str | None = "full",
    from_dir: str | None = None,
    tags: list[str] | None = None,
    batch_id: str | None = None,
    linked_projects: list[str] | None = None,
    stem_files: dict[str, str] | None = None,
    timeout_s: int = 180,
    ownership: str | None = None,
    listener_mode: str | None = None,
    pass_b: bool = False,
    verify_listen: bool = True,
    allow_unverified_listen: bool = False,
    listen_token_cap: int | None = None,
    window_token_cap: int | None = None,
    window_timeout_s: int | None = None,
    conversations_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Library entry used by CLI and MCP.

    ``verify_listen`` (default on) holds back any card whose agy run
    fails proof-of-listening or the token cap: the card is not
    persisted, ``listen_rejected`` lands in meta.json, and nothing
    retries automatically. ``allow_unverified_listen`` is the explicit
    operator override for runs with no readable conversation record.
    """
    modes = list(modes if modes is not None else DEFAULT_MODES)
    if not modes:
        raise ValueError("modes must be non-empty (describe, midi, and/or features)")
    unknown = [m for m in modes if m not in DEFAULT_MODES]
    if unknown:
        raise ValueError(f"unknown modes: {unknown}")
    focus_n = normalize_focus(focus)
    timeout_s = int(timeout_s)
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    agy_model = _resolve_model(model).strip() or DEFAULT_AGY_MODEL
    validated_stems = _validate_stem_files(stem_files)

    start_s = parse_time(start)
    end_s = parse_time(end)
    if start_s is not None and end_s is not None and end_s <= start_s:
        raise ValueError("--end must be after --start")

    warnings: list[str] = []
    mode_ok: dict[str, bool] = {m: False for m in modes}
    yt_meta: dict[str, Any] = {}
    created_at = _utc_now_iso()
    tag_list = [str(t).strip() for t in (tags or []) if str(t).strip()]
    linked_list = [str(p).strip() for p in (linked_projects or []) if str(p).strip()]

    prev_meta: dict[str, Any] = {}
    if from_dir:
        out_dir = resolve_from_dir(from_dir)
        wav = out_dir / "source.wav"
        if (out_dir / "meta.json").is_file():
            try:
                prev_meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                prev_meta = {}
        resolved = str(source or prev_meta.get("source") or wav)
        kind = str(prev_meta.get("source_kind") or ("youtube" if is_youtube_url(resolved) else "file"))
        yt_meta = prev_meta.get("youtube") or {}
        if start_s is None:
            start_s = prev_meta.get("start")
        if end_s is None:
            end_s = prev_meta.get("end")
        # Batch tags are additive.  In particular, adding batch:<id> must not
        # erase the project's existing tags on a from_dir re-listen.
        try:
            indexed_card = load_index().get("refs", {}).get(out_dir.name, {})
        except (OSError, RuntimeError):
            indexed_card = {}
        existing_tags = list(prev_meta.get("tags") or indexed_card.get("tags") or [])
        existing_linked = list(prev_meta.get("linked_projects")
                               or indexed_card.get("linked_projects") or [])
        tag_list = existing_tags + [t for t in tag_list if t not in existing_tags]
        linked_list = existing_linked + [p for p in linked_list if p not in existing_linked]
        if batch_id is None:
            batch_id = prev_meta.get("batch_id")
        # Preserve Ricardo's overlay — re-analyze must not erase keep/drop notes.
        carried_note = prev_meta.get("ricardo_note")
        carried_verdict = prev_meta.get("verdict")
        carried_verdict_at = prev_meta.get("verdict_at")
        # X7 provenance: interactive belongs ONLY to cards heard inside the
        # legacy shared-chat conversation (stamped by --stamp-provenance, never
        # here). Every pipeline per-track listen — CLI, MCP or batch — is
        # isolated, and a re-listen never carries the previous run's mode. A
        # run with no listener (no describe mode) gets no mode at all. An
        # explicit argument still wins as an operator override.
        carried_listener_mode = normalize_listener_mode(listener_mode)
        if carried_listener_mode is None and "describe" in modes:
            carried_listener_mode = "isolated"
        carried_ownership = normalize_ownership(
            ownership if ownership is not None else prev_meta.get("ownership")
        )
        if ownership is None and carried_ownership == "unknown":
            # Older metadata predates the operator field; recover it from the
            # index before a re-listen (the index is the other live copy).
            try:
                indexed = load_index().get("refs", {}).get(out_dir.name, {})
                carried_ownership = normalize_ownership(indexed.get("ownership"))
            except (OSError, RuntimeError, ValueError):
                pass
    else:
        carried_note = carried_verdict = carried_verdict_at = None
        carried_listener_mode = normalize_listener_mode(listener_mode)
        if carried_listener_mode is None and "describe" in modes:
            carried_listener_mode = "isolated"
        carried_ownership = normalize_ownership(ownership)
        if not source:
            raise ValueError("source is required unless from_dir is set")
        kind, resolved = detect_source(source)
        if kind == "youtube":
            stem = "yt"
            m = re.search(r"(?:v=|/shorts/|youtu\.be/)([\w-]{6,})", resolved)
            if m:
                stem = f"yt-{m.group(1)}"
        else:
            stem = slugify(Path(resolved).stem)
        out_dir = make_out_dir(stem)
        try:
            if kind == "youtube":
                wav, yt_meta = fetch_youtube(resolved, out_dir, start_s, end_s)
            else:
                wav = prepare_local(resolved, out_dir, start_s, end_s)
        except Exception as exc:
            meta = {
                "ok": False,
                "partial": False,
                "error": f"{type(exc).__name__}: {exc}",
                "source": resolved,
                "source_kind": kind,
                "out_dir": str(out_dir),
                "mode_ok": mode_ok,
                "focus": focus_n,
                "tags": tag_list,
                "batch_id": batch_id,
                "created_at": created_at,
            }
            write_json_atomic(out_dir / "meta.json", meta)
            return meta

    # A re-listen updates a four-file publication unit.  Keep the exact prior
    # bytes until both metadata and index have committed; a storage failure
    # must not leave a new card paired with an old index (or vice versa).
    analysis_paths = [
        out_dir / "card.json",
        out_dir / "describe.txt",
        out_dir / "card.unverified.json",
        out_dir / "describe.unverified.txt",
        out_dir / "features.json",
        out_dir / "ref.mid",
        out_dir / "measured_drum_grid.json",
        out_dir / "meta.json",
        INDEX_PATH,
    ]
    if validated_stems:
        analysis_paths.extend(
            out_dir / "stems" / f"{role}.wav" for role, _path in validated_stems
        )
    analysis_snapshot = _snapshot_bytes(analysis_paths)

    if validated_stems:
        stem_artifacts = _ingest_stems(validated_stems, out_dir)
    elif from_dir and isinstance(prev_meta.get("stem_artifacts"), list):
        stem_artifacts = list(prev_meta["stem_artifacts"])
    else:
        stem_artifacts = []

    features: dict[str, Any] = {"source": resolved, "source_kind": kind}
    midi_path = out_dir / "ref.mid"
    describe_path = out_dir / "describe.txt"
    describe_result = None
    v2_card: dict[str, Any] | None = None
    card_artifact_hashes: dict[str, str | None] = {}
    moments_validation = None
    model_path_used: str | None = None
    tool_versions = collect_tool_versions()

    if "features" in modes or "midi" in modes or "describe" in modes:
        features.update(ffprobe_info(wav))

    # Measure first: local DSP runs BEFORE Basic Pitch (per-stem tempo,
    # and the measured octave-resolved tempo for non-stem runs) and before
    # the model listens (loudness points + reconciliation).
    measured: dict[str, Any] = {}
    if start_s is not None or end_s is not None:
        # A start/end window is a clip slice: file-zero-based DSP (tempo,
        # loudness windows) would describe the slice as the whole track.
        warnings.append("windowed analyze (start/end): measure_audio skipped")
        if from_dir and isinstance(prev_meta.get("measured"), dict):
            # A windowed re-analyze keeps the previous full-track
            # measurement instead of dropping it to None.
            measured = dict(prev_meta["measured"])
    elif wav.is_file():
        try:
            measured = measure_audio(wav)
        except Exception as exc:
            warnings.append(f"measure failed: {type(exc).__name__}: {exc}")

    stem_midi: dict[str, dict[str, Any]] = {}
    winning_stem: str | None = None
    if "midi" in modes:
        if validated_stems:
            # One Basic Pitch run per stem at the MEASURED tempo, not the
            # caller's guess. Per-stem midis are kept; the melodic stem
            # with the most notes wins ref.mid (never drums).
            stem_tempo = (measured.get("tempo_bpm") if measured
                          else None) or midi_tempo
            for item in stem_artifacts:
                role = item.get("role")
                stem_wav = out_dir / str(item.get("file"))
                stem_mid = out_dir / "stems" / f"{role}.mid"
                try:
                    stats = run_basic_pitch(stem_wav, stem_mid, tempo=stem_tempo)
                    model_path_used = stats.pop("model_path_used", None)
                    entry = {"midi": f"stems/{role}.mid",
                             "note_count": int(stats.get("note_count") or 0),
                             "stats": {k: v for k, v in stats.items()}}
                    item["midi"] = entry["midi"]
                    item["note_count"] = entry["note_count"]
                    if stem_mid.is_file():
                        stem_midi[str(role)] = entry
                    else:
                        entry["error"] = "stem mid was not written"
                        stem_midi[str(role)] = entry
                        warnings.append(f"midi failed for stem {role!r}: "
                                        "stem mid was not written")
                except Exception as exc:
                    stem_midi[str(role)] = {"midi": None, "note_count": 0,
                                            "error": f"{type(exc).__name__}: {exc}"}
                    warnings.append(f"midi failed for stem {role!r}: "
                                    f"{type(exc).__name__}: {exc}")
            # M5: ref.mid goes to the melodic stem with the most notes —
            # never the drums stem, whose hits are not pitched lines. The
            # drums midi stays in stem_midi for the measured grid.
            winning_stem = _melodic_winner(stem_midi)
            if winning_stem is not None:
                try:
                    shutil.copy2(out_dir / str(stem_midi[winning_stem]["midi"]),
                                 midi_path)
                except OSError as exc:
                    warnings.append(f"midi failed: winner copy failed: {exc}")
                    winning_stem = None
            if winning_stem is not None and midi_path.is_file():
                features.update(stem_midi[winning_stem].get("stats") or {})
                mode_ok["midi"] = True
                warnings.append(
                    "Basic Pitch is weak on pure drums/percussion; "
                    "lean on describe.txt for drum refs."
                )
            elif any(e.get("midi") for e in stem_midi.values()):
                warnings.append(
                    "midi: only the drums stem produced a midi — kept "
                    "separately as stems/drums.mid for the measured grid; "
                    "ref.mid needs a melodic stem so melodic consumers "
                    "never read drum hits as pitched lines"
                )
            else:
                warnings.append("midi failed: no stem produced a midi")
        else:
            try:
                # M5: transcribe at the measured tempo (octave-resolved
                # against the caller's guess), never the raw guess.
                bp_tempo = _bp_tempo((measured or {}).get("tempo_bpm"),
                                     midi_tempo)
                midi_stats = run_basic_pitch(wav, midi_path, tempo=bp_tempo)
                model_path_used = midi_stats.pop("model_path_used", None)
                features.update(midi_stats)
                if midi_path.is_file():
                    mode_ok["midi"] = True
                    warnings.append(
                        "Basic Pitch is weak on pure drums/percussion; "
                        "lean on describe.txt for drum refs."
                    )
                else:
                    warnings.append("midi failed: ref.mid was not written")
            except Exception as exc:
                warnings.append(f"midi failed: {type(exc).__name__}: {exc}")

    if "features" in modes:
        try:
            _write_text_atomic(
                out_dir / "features.json",
                json.dumps(features, indent=2) + "\n",
            )
            mode_ok["features"] = (out_dir / "features.json").is_file()
            if not mode_ok["features"]:
                warnings.append("features failed: features.json was not written")
        except Exception as exc:
            warnings.append(f"features failed: {type(exc).__name__}: {exc}")
    elif (out_dir / "features.json").is_file() and from_dir:
        try:
            prev_feat = json.loads((out_dir / "features.json").read_text(encoding="utf-8"))
            for k, v in prev_feat.items():
                features.setdefault(k, v)
        except (OSError, json.JSONDecodeError):
            pass

    listen_rejected: dict[str, Any] | None = None
    if "describe" in modes:
        # M5: ref.mid joins the listen as a pitch-class cross-check only.
        # The midi block above already ran, so read the vocabulary here.
        mid_pcs: list[str] | None = None
        if midi_path.is_file():
            try:
                mid_pcs = midi_pitch_classes(midi_path)
            except Exception:
                mid_pcs = None
        try:
            describe_result = run_agy_describe_v3(
                wav=wav,
                prompt_extra=prompt_extra,
                focus=focus_n,
                duration_s=features.get("duration_s"),
                timeout_s=timeout_s,
                model=model,
                measured=measured,
                midi_pitch_classes=mid_pcs,
                verify_listen=verify_listen,
                listen_token_cap=listen_token_cap,
                conversations_dir=conversations_dir,
                allow_unverified_listen=allow_unverified_listen,
            )
            if describe_result.get("ok"):
                v3_card = dict(describe_result["card"])
                # Ownership is an operator overlay, so never accept a model key
                # even if an old/fake listener returns one.
                v3_card.pop("ownership", None)
                # MIDI-derived grids never go into the model's card: a null
                # heard grid stays null, and the measured lanes below carry
                # the stem evidence as measured_drum_grid only.
                card_artifact_hashes = _persist_card_artifacts(
                    out_dir, v3_card, version="v3")
                mode_ok["describe"] = True
                moments_validation = {
                    "ok": True,
                    "stamp_count": len(v3_card.get("sections") or []),
                    "duration_s": features.get("duration_s"),
                }
            else:
                mode_ok["describe"] = False
                # Keep a rejected listen beside the canonical validated
                # artifacts; it must not change what indexing or steering sees.
                unverified_card = describe_result.get("card")
                if (isinstance(unverified_card, dict)
                        and describe_result.get("listen_rejected")):
                    unverified_card = dict(unverified_card)
                    unverified_card.pop("ownership", None)
                    _persist_card_artifacts(
                        out_dir, unverified_card, version="v3",
                        suffix=".unverified")
            if not mode_ok["describe"]:
                warnings.append(
                    f"describe failed: {describe_result.get('error') or describe_result}"
                )
        except Exception as exc:
            warnings.append(f"describe failed: {type(exc).__name__}: {exc}")
            describe_result = {"ok": False, "error": str(exc)}
            mode_ok["describe"] = False
        # A rejected proof remains separate from the usable model card and
        # local DSP measurement; it is recorded for browsing, never retried.
        # The rejected run's own conversation id, verification record and
        # reconciliation ride here — the canonical meta keys below carry
        # the previous run's values instead, so the unverified card can
        # never leak into the index row's uncertain view.
        listen_rejected = (describe_result or {}).get("listen_rejected")
        if isinstance(listen_rejected, dict):
            listen_rejected = dict(listen_rejected)
            for _key in ("conversation_id", "listen_verification",
                         "reconciliation"):
                _value = (describe_result or {}).get(_key)
                if _value is not None:
                    listen_rejected.setdefault(_key, _value)
        if listen_rejected:
            warnings.append(
                "listen proof unavailable (%s): card persisted unverified; "
                "re-run the listen when appropriate, it is never retried automatically"
                % (listen_rejected.get("reason") or "unverified"))

    # Never read stale prose as the result of a failed describe attempt. On
    # success it is the deterministic rendering we just committed atomically.
    excerpt = ""
    if mode_ok.get("describe") and describe_path.is_file():
        excerpt = describe_path.read_text(encoding="utf-8")[:800]

    all_ok = all(mode_ok[m] for m in modes)
    any_ok = any(mode_ok[m] for m in modes)
    result = {
        "ok": all_ok,
        "partial": (not all_ok) and any_ok,
        "mode_ok": mode_ok,
        "out_dir": str(out_dir),
        "ref_id": out_dir.name,
        "source": resolved,
        "source_kind": kind,
        "modes": modes,
        "focus": focus_n,
        "prompt_extra": prompt_extra,
        "tags": tag_list,
        "linked_projects": linked_list,
        "batch_id": batch_id,
        "wav_path": str(wav),
        "features_path": str(out_dir / "features.json")
        if (out_dir / "features.json").is_file()
        else None,
        "describe_path": str(describe_path)
        if mode_ok.get("describe") and describe_path.is_file() else None,
        "midi_path": str(midi_path) if midi_path.is_file() else None,
        "features": features if features else None,
        "describe_excerpt": excerpt or None,
        "describe_result": describe_result,
        "moments_validation": moments_validation,
        "youtube": yt_meta or None,
        "start": start_s,
        "end": end_s,
        # A failed attempt must not stamp its requested model over the last good
        # data.  The attempt itself is recorded below, additively.
        "agy_model": agy_model if mode_ok.get("describe") else (
            prev_meta.get("agy_model") if from_dir else None
        ),
        # X7 provenance: isolated names the pipeline per-track listen that
        # produced this card (interactive is stamped only by
        # --stamp-provenance for the legacy chat-heard cards, never here).
        # A run with no listener leaves no mode — unlike the model, which
        # keeps the agy_model carry rule so a failed attempt must not stamp
        # over the last good data.
        "listener_mode": carried_listener_mode,
        "exact_listener_model": (
            agy_model if mode_ok.get("describe") else (
                prev_meta.get("exact_listener_model")
                or prev_meta.get("agy_model") if from_dir else None
            )
        ),
        "timeout_s": timeout_s,
        "contract_version": V3_CONTRACT_VERSION if mode_ok.get("describe") else (
            prev_meta.get("contract_version") if from_dir else None
        ),
        # A rejected re-listen keeps the previous run's card hashes: the
        # canonical card.json/describe.txt are untouched, so their hashes
        # stay valid; the rejected run's card is fingerprinted only in the
        # sidecar files it wrote.
        "hashes": {
            "source_sha256": _file_sha256(wav),
            **card_artifact_hashes,
        } if mode_ok.get("describe") else {
            "source_sha256": _file_sha256(wav),
            "card_sha256": (prev_meta.get("hashes") or {}).get("card_sha256")
            if from_dir else None,
            "describe_sha256": (prev_meta.get("hashes") or {}).get(
                "describe_sha256") if from_dir else None,
        },
        "evidence": {
            "listener": "agy" if "describe" in modes else None,
            "staged_audio_only": bool("describe" in modes),
            "v3": bool(mode_ok.get("describe")),
            "argv": (describe_result or {}).get("argv"),
        },
        # One structured turn's token/cost tally. A failed re-listen keeps
        # the previous tally; a fresh run starts empty, never inherits.
        # num_turns rides along (M3): more than one turn warns below.
        "listen_usage": ((describe_result or {}).get("usage")
                         if mode_ok.get("describe")
                         else (prev_meta.get("listen_usage")
                               if from_dir else None)),
        # Delivery proof is separate from the model card. A successful
        # run stamps its own conversation id and verification record; a
        # rejected re-listen keeps the previous run's — the rejected run's
        # values ride only under listen_rejected above, so the canonical
        # row never cites the unverified card.
        "conversation_id": (
            (describe_result or {}).get("conversation_id")
            if mode_ok.get("describe")
            else (prev_meta.get("conversation_id") if from_dir else None)
        ),
        "listen_rejected": listen_rejected,
        "listen_verification": (
            (describe_result or {}).get("listen_verification")
            if mode_ok.get("describe")
            else (prev_meta.get("listen_verification")
                  if from_dir else None)),
        "usage_missing": bool((describe_result or {}).get("usage_missing")),
        # B3 provenance: True only from verify_agy_listen's verification
        # record. A re-listen that ran no listener keeps the previous value;
        # everything else is unverified, never unknown.
        "listen_verified": (
            bool(((describe_result or {}).get("listen_verification") or {})
                 .get("verified"))
            if mode_ok.get("describe")
            else (bool(prev_meta.get("listen_verified"))
                  if from_dir else False)),
        # Local measurement first, model second, reconciler after. On a
        # failed describe the measurement still lands (it is independent of
        # the model), so a later re-listen can reconcile without re-measuring.
        "measured": measured or None,
        # The reconciler compares measurement against the model's card; a
        # rejected run's reconciliation describes the unverified card, so
        # the canonical key keeps the previous run's (like listen_verified).
        "reconciliation": (
            (describe_result or {}).get("reconciliation")
            if mode_ok.get("describe")
            else (prev_meta.get("reconciliation") if from_dir else None)),
        "created_at": created_at,
        "warnings": warnings,
        "tool_versions": tool_versions,
    }
    if mode_ok.get("describe") and describe_path.is_file():
        try:
            result["structure"] = parse_describe_structure(
                describe_path.read_text(encoding="utf-8")
            )
        except OSError:
            result["structure"] = None
    measured_grid = None
    if validated_stems:
        drums = next((item for item in stem_artifacts
                      if item.get("role") == "drums"), None)
        if drums is not None:
            structure = result.get("structure") or {}
            card_tempo = (structure.get("grid_bpm")
                          or structure.get("feel_bpm"))
            raw_tempo = ((measured.get("tempo_bpm") if measured else None)
                         or card_tempo or midi_tempo)
            grid_tempo = resolve_tempo_octave(raw_tempo, card_tempo)
            drums_mid = (stem_midi.get("drums") or {}).get("midi")
            if drums_mid and grid_tempo is not None:
                steps = midi_onset_phase_steps(out_dir / str(drums_mid),
                                               grid_tempo)
                if steps:
                    measured_grid = {
                        "version": 1,
                        "method": "stem_midi_onsets",
                        "tempo_bpm": float(grid_tempo),
                        "grid": "sixteenth",
                        "steps": steps,
                        "accepted_onsets": len(steps),
                        "evidence_only": True,
                        "transcription": False,
                    }
            if measured_grid is None:
                try:
                    measured_grid = measure_drums_grid(
                        out_dir / drums["file"], grid_tempo)
                except (OSError, ValueError, wave.Error):
                    warnings.append("drums stem onset analysis failed")
            if measured_grid is None:
                warnings.append(
                    "drums stem did not yield a valid tempo-snapped onset grid")
    elif from_dir and isinstance(prev_meta.get("measured_drum_grid"), dict):
        measured_grid = dict(prev_meta["measured_drum_grid"])
    if not validated_stems and from_dir:
        if not stem_midi and isinstance(prev_meta.get("stem_midi"), dict):
            stem_midi = dict(prev_meta["stem_midi"])
        if winning_stem is None and prev_meta.get("winning_stem"):
            winning_stem = prev_meta.get("winning_stem")
    result["stem_artifacts"] = stem_artifacts or None
    result["stem_midi"] = stem_midi or None
    result["winning_stem"] = winning_stem
    result["measured_drum_grid"] = measured_grid
    # X5 Pass B (opt-in): after a successful Pass A, cut 2-3 windows from
    # the section energy peaks (or the focus) and hear each for groove /
    # drum grid / sound design. Enrichment only — a deaf window is an
    # entry, never a failed run.
    pass_b_windows: list[dict[str, Any]] | None = None
    if pass_b and mode_ok.get("describe"):
        try:
            # Windows are 30-45 s excerpts: 2m by default, tighter when
            # the whole run is on a tighter budget, explicit knob wins.
            window_timeout = (int(window_timeout_s) if window_timeout_s
                              else min(int(timeout_s), 120))
            _pass_b = run_pass_b_windows(
                wav=wav,
                v3_card=(describe_result or {}).get("card"),
                duration_s=((features or {}).get("duration_s") or 0.0),
                focus=focus_n,
                out_dir=out_dir,
                model=model,
                timeout_s=window_timeout,
                verify_listen=verify_listen,
                listen_token_cap=window_token_cap,
                conversations_dir=conversations_dir,
                allow_unverified_listen=allow_unverified_listen,
            )
        except Exception as exc:
            warnings.append(f"pass_b failed: {type(exc).__name__}: {exc}")
            _pass_b = {"windows": [], "usage": {}}
        pass_b_windows = list(_pass_b.get("windows") or [])
        result["pass_b_usage"] = _pass_b.get("usage") or {}
        for _w in pass_b_windows:
            if not _w.get("ok"):
                warnings.append("pass_b window %s: %s"
                                % (_w.get("window"), _w.get("error")))
    elif from_dir and isinstance(prev_meta.get("windows"), list):
        # A re-listen without Pass B keeps the previous run's windows.
        pass_b_windows = list(prev_meta["windows"])
    result["windows"] = pass_b_windows
    if measured_grid is not None:
        write_json_atomic(out_dir / "measured_drum_grid.json", measured_grid)
    if model_path_used is not None:
        result["model_path_used"] = model_path_used
    if carried_note is not None:
        result["ricardo_note"] = carried_note
    if carried_verdict is not None:
        result["verdict"] = carried_verdict
    if carried_verdict_at is not None:
        result["verdict_at"] = carried_verdict_at
    result["ownership"] = carried_ownership
    # M3: a multi-turn listen costs quota — say so in the meta warnings.
    _note_listener_turns(result.get("listen_usage"), warnings)
    prior_attempts = list(prev_meta.get("analysis_attempts") or []) if from_dir else []
    if "describe" in modes:
        attempt = {
            "ok": bool(mode_ok.get("describe")),
            "model": agy_model,
            "timeout_s": timeout_s,
            "contract_version": V3_CONTRACT_VERSION,
            "evidence": (describe_result or {}).get("argv"),
            "error": (describe_result or {}).get("error"),
            "started_at": (describe_result or {}).get("started_at"),
            "finished_at": (describe_result or {}).get("finished_at"),
        }
        result["analysis_attempts"] = prior_attempts + [attempt]
    elif prior_attempts:
        result["analysis_attempts"] = prior_attempts
    if from_dir:
        # meta.json is rewritten wholesale below; re-read it just before the
        # write and carry any offload record forward (crash between record
        # and unlink, or a manual restore that kept it) — dropping it here
        # would orphan the external copy.
        record = _offload_record(out_dir)
        if record:
            result["source_offloaded"] = record
    try:
        write_json_atomic(out_dir / "meta.json", result)
        # Index whenever audio exists — describe-less midi/features refs must
        # be findable. On a from_dir failure, leave the previous index card
        # untouched: failed JSON must not be re-parsed as a successful reading.
        if wav.is_file() and (
            "describe" not in modes or mode_ok.get("describe") or not from_dir
        ):
            upsert_index_card(card_from_out_dir(out_dir, result))
    except Exception as exc:
        _restore_bytes(analysis_snapshot)
        result["ok"] = False
        result["partial"] = False
        result["error"] = f"artifact publication failed: {type(exc).__name__}: {exc}"
        result["warnings"] = warnings + [result["error"]]
        result["mode_ok"] = dict(mode_ok)
        if "describe" in result["mode_ok"]:
            result["mode_ok"]["describe"] = False
        result["describe_path"] = None
        result["describe_excerpt"] = None
    return result


def batch_item_key(item: dict[str, Any], defaults: dict[str, Any] | None = None) -> str:
    """Stable resume key for the fully merged effective analysis config.

    Includes prompt_extra / model / midi_tempo so changing the listen brief or
    transcription tempo does not silently reuse a prior success under resume=True.
    """
    defaults = defaults or {}
    src = item.get("source")
    from_dir = item.get("from_dir")
    focus = item.get("focus", defaults.get("focus", "full"))
    start = item.get("start", defaults.get("start"))
    end = item.get("end", defaults.get("end"))
    modes_raw = item.get("modes", defaults.get("modes", "describe,midi,features"))
    if isinstance(modes_raw, (list, tuple)):
        modes = ",".join(str(m).strip() for m in modes_raw if str(m).strip())
    else:
        modes = ",".join(m.strip() for m in str(modes_raw).split(",") if m.strip())
    prompt_extra = item.get("prompt_extra", defaults.get("prompt_extra"))
    model = item.get("model", defaults.get("model"))
    effective_model = _resolve_model(model)
    midi_tempo = item.get("midi_tempo", defaults.get("midi_tempo", 120.0))
    tags = []
    for value in list(defaults.get("tags") or []) + list(item.get("tags") or []):
        text = str(value).strip()
        if text and text not in tags:
            tags.append(text)
    linked = []
    for value in list(defaults.get("linked_projects") or []) + list(item.get("linked_projects") or []):
        text = str(value).strip()
        if text and text not in linked:
            linked.append(text)
    try:
        tempo_s = f"{float(midi_tempo):g}"
    except (TypeError, ValueError):
        tempo_s = str(midi_tempo)
    return "|".join(
        [
            f"src={src or ''}",
            f"from={from_dir or ''}",
            f"start={start if start is not None else ''}",
            f"end={end if end is not None else ''}",
            f"focus={focus or 'full'}",
            f"modes={modes}",
            f"prompt={prompt_extra or ''}",
            f"model={effective_model}",
            f"tempo={tempo_s}",
            f"tags={json.dumps(tags, separators=(',', ':'))}",
            f"linked_projects={json.dumps(linked, separators=(',', ':'))}",
        ]
    )


def batch_manifest_hash(manifest: dict[str, Any], timeout_s: int = 600) -> str:
    """Stable hash of the effective batch configuration for forensic resumes."""
    payload = {"manifest": manifest, "function_timeout_s": int(timeout_s)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def new_batch_id(prefix: str = "batch") -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return slugify(f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}")


def load_batch_manifest(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    # Plain URL/path list
    stripped = raw.strip()
    if not stripped.startswith("{") and not stripped.startswith("["):
        items = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            items.append({"source": line})
        return {"batch_id": new_batch_id(slugify(path.stem) or "batch"), "defaults": {}, "items": items}

    data = json.loads(raw)
    if isinstance(data, list):
        return {"batch_id": new_batch_id(slugify(path.stem) or "batch"), "defaults": {}, "items": data}
    if not isinstance(data, dict) or "items" not in data:
        raise ValueError("batch JSON must be a list or object with 'items'")
    if not data.get("batch_id"):
        data["batch_id"] = new_batch_id(slugify(path.stem) or "batch")
    data.setdefault("defaults", {})
    return data


def run_batch(
    manifest: dict[str, Any] | str | Path,
    *,
    resume: bool = True,
    timeout_s: int = 180,
    allow_unverified_listen: bool = False,
    listen_token_cap: int | None = None,
    conversations_dir: str | Path | None = None,
    retry_rejected: bool = False,
) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        manifest = load_batch_manifest(Path(manifest))
    else:
        manifest = dict(manifest)
        if not manifest.get("batch_id"):
            manifest["batch_id"] = new_batch_id("batch")
    batch_id = slugify(str(manifest.get("batch_id") or new_batch_id("batch")))
    defaults = dict(manifest.get("defaults") or {})
    items = list(manifest.get("items") or [])
    if not items:
        raise ValueError("batch has no items")

    BATCH_ROOT.mkdir(parents=True, exist_ok=True)
    manifest_sha256 = batch_manifest_hash(manifest, timeout_s)
    report_path = BATCH_ROOT / f"{batch_id}.json"
    if resume and report_path.is_file():
        try:
            existing_report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot verify existing batch report: {exc}") from exc
        previous_hash = existing_report.get("manifest_sha256") or existing_report.get("config_sha256")
        legacy_hashes = {batch_manifest_hash(manifest, legacy_timeout)
                         for legacy_timeout in (600,)}
        if (previous_hash and previous_hash != manifest_sha256
                and previous_hash not in legacy_hashes):
            raise ValueError(
                "batch manifest_sha256 differs from existing report; refusing resume"
            )
    prev_items: dict[str, Any] = {}
    prev_rejected: dict[str, Any] = {}
    prev_partial: dict[str, str] = {}
    if resume and report_path.is_file():
        try:
            prev = json.loads(report_path.read_text(encoding="utf-8"))
            for row in prev.get("results") or []:
                key = str(row.get("resume_key") or "")
                if not key and row.get("source"):
                    # legacy reports: source-only key (narrower than composite)
                    key = str(row.get("source"))
                if not key:
                    continue
                if row.get("ok"):
                    prev_items[key] = row
                    # Reports written before timeout was removed from the
                    # semantic resume key retain one timeout_s segment.
                    prev_items[re.sub(r"\|timeout_s=[^|]*\|?", "", key)] = row
                elif row.get("listen_rejected"):
                    prev_rejected[key] = row
                    prev_rejected[re.sub(r"\|timeout_s=[^|]*\|?", "", key)] = row
                    if retry_rejected and row.get("out_dir"):
                        part_dir = Path(str(row["out_dir"]))
                        if ((part_dir / "source.wav").is_file()
                                or _offload_record(part_dir)):
                            prev_partial[key] = str(part_dir)
                            prev_partial[re.sub(r"\|timeout_s=[^|]*\|?", "", key)] = str(part_dir)
                elif ((((row.get("partial") or row.get("retry_in_place"))
                        and (retry_rejected or not row.get("listen_rejected")))
                       or (retry_rejected and row.get("listen_rejected")))
                      and row.get("out_dir")):
                    # A partial or retry_in_place row already has fetched
                    # audio on disk. Re-running it from scratch re-downloads
                    # AND re-listens into a NEW timestamped folder — a
                    # duplicate library entry for one track plus a wasted
                    # agy call. Retry in place instead, but only when the
                    # audio really survived (from_dir demands source.wav).
                    # An OFFLOADED source counts as survived too: the
                    # in-place retry then fails fast with the restore hint
                    # instead of re-downloading a duplicate ref.
                    part_dir = Path(str(row["out_dir"]))
                    if (part_dir / "source.wav").is_file() or _offload_record(part_dir):
                        prev_partial[key] = str(part_dir)
                        prev_partial[re.sub(r"\|timeout_s=[^|]*\|?", "", key)] = str(part_dir)
        except (OSError, json.JSONDecodeError):
            pass

    results: list[dict[str, Any]] = []
    stopped: dict[str, str] | None = None
    listen_rejected_streak = 0
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            item = {"source": str(item)}
        src = item.get("source")
        key_defaults = dict(defaults)
        key_defaults.setdefault("timeout_s", timeout_s)
        key = batch_item_key(item, key_defaults)
        if resume and not retry_rejected and key in prev_rejected:
            results.append({**prev_rejected[key], "resume_key": key,
                            "skipped_resume": True})
            continue
        if resume and key in prev_items:
            results.append({**prev_items[key], "resume_key": key, "skipped_resume": True})
            continue
        from_dir = item.get("from_dir")
        retried_partial = False
        if resume and not from_dir and key in prev_partial:
            from_dir = prev_partial[key]
            retried_partial = True

        focus = item.get("focus", defaults.get("focus", "full"))
        modes_raw = item.get("modes", defaults.get("modes", "describe,midi,features"))
        if isinstance(modes_raw, str):
            mode_list = [m.strip() for m in modes_raw.split(",") if m.strip()]
        else:
            mode_list = list(modes_raw)
        tags = list(defaults.get("tags") or []) + list(item.get("tags") or [])
        if batch_id:
            bid_tag = f"batch:{batch_id}"
            if bid_tag not in tags:
                tags.append(bid_tag)
        try:
            item_timeout = int(item.get("timeout_s", defaults.get("timeout_s", timeout_s)))
            row = analyze_audio_ref(
                source=src,
                from_dir=from_dir,
                modes=mode_list,
                prompt_extra=item.get("prompt_extra", defaults.get("prompt_extra")),
                start=item.get("start", defaults.get("start")),
                end=item.get("end", defaults.get("end")),
                midi_tempo=float(item.get("midi_tempo", defaults.get("midi_tempo", 120.0))),
                model=item.get("model", defaults.get("model")),
                focus=focus,
                tags=tags,
                batch_id=batch_id,
                linked_projects=list(item.get("linked_projects") or defaults.get("linked_projects") or []),
                timeout_s=item_timeout,
                allow_unverified_listen=allow_unverified_listen,
                listen_token_cap=listen_token_cap,
                conversations_dir=conversations_dir,
            )
            kind = None
            if not row.get("ok"):
                detail = dict(row.get("describe_result") or {})
                detail.setdefault("error", row.get("error"))
                kind = classify_listen_failure(detail)
            entry = {
                    "ok": bool(row.get("ok")),
                    "partial": bool(row.get("partial")),
                    "resume_key": key,
                    "source": src,
                    "from_dir": from_dir,
                    "retried_partial": retried_partial or None,
                    "start": item.get("start", defaults.get("start")),
                    "end": item.get("end", defaults.get("end")),
                    "ref_id": row.get("ref_id"),
                    "out_dir": row.get("out_dir"),
                    "error": row.get("error"),
                    "failure_kind": kind,
                    "warnings": row.get("warnings"),
                    "focus": row.get("focus"),
                    "agy_model": row.get("agy_model"),
                    "timeout_s": row.get("timeout_s"),
                    "contract_version": row.get("contract_version"),
                    "hashes": row.get("hashes"),
                    "evidence": row.get("evidence"),
                    "listen_usage": row.get("listen_usage"),
                    "conversation_id": row.get("conversation_id"),
                    "listen_rejected": row.get("listen_rejected"),
                    "usage_missing": row.get("usage_missing"),
                }
            if kind == "quota" and entry.get("out_dir"):
                # The fetched audio survived; a resume retries this row in
                # place (the resume reader re-verifies source.wav first).
                entry["retry_in_place"] = True
            results.append(entry)
            if kind == "quota" and not entry.get("ok"):
                # Quota is batch-fatal: every further listen fails the same
                # way and burns quota. Stop with the resume key, not a tail.
                stopped = {"reason": "quota", "key": key}
                listen_rejected_streak = 0
            elif kind == "listen_rejected" and not entry.get("ok"):
                listen_rejected_streak += 1
                stopped = ({"reason": "listen_rejected_streak", "key": key}
                           if listen_rejected_streak >= 2 else None)
            else:
                listen_rejected_streak = 0
                stopped = None
        except Exception as exc:
            row = {
                "ok": False,
                "resume_key": key,
                "source": src,
                "from_dir": from_dir,
                "retried_partial": retried_partial or None,
                "error": f"{type(exc).__name__}: {exc}",
                "failure_kind": classify_listen_failure(
                    {"error": f"{type(exc).__name__}: {exc}"}),
                "agy_model": _resolve_model(item.get("model", defaults.get("model"))),
                "timeout_s": item.get("timeout_s", defaults.get("timeout_s", timeout_s)),
                "contract_version": CARD_CONTRACT_VERSION,
                "hashes": None,
                "evidence": None,
                "listen_usage": None,
            }
            if retried_partial:
                # The partial dir still holds the fetched audio; keep the row
                # retryable with its out_dir so the NEXT --resume also retries
                # in place instead of re-downloading a duplicate ref. A
                # separate marker, not partial: the batch-level partial flag
                # keeps meaning "produced usable output this run".
                row["retry_in_place"] = True
                row["out_dir"] = from_dir
            results.append(row)
            if row.get("failure_kind") == "quota":
                stopped = {"reason": "quota", "key": key}
                listen_rejected_streak = 0
            elif row.get("failure_kind") == "listen_rejected":
                listen_rejected_streak += 1
                stopped = ({"reason": "listen_rejected_streak", "key": key}
                           if listen_rejected_streak >= 2 else None)
            else:
                listen_rejected_streak = 0
                stopped = None

        # Interim checkpoint after every item: complete=False so an interrupted
        # batch never reads as a finished one just because its processed rows
        # happened to all pass.
        write_json_atomic(report_path, {
            **summarize_batch_results(results),
            "batch_id": batch_id,
            "complete": False,
            "stopped_reason": (stopped or {}).get("reason"),
            "resume_from": (stopped or {}).get("key"),
            "items_total": len(items),
            "manifest_sha256": manifest_sha256,
            "config_sha256": manifest_sha256,
            "updated_at": _utc_now_iso(),
            "results": results,
        })
        if stopped is not None:
            break

    report = {
        **summarize_batch_results(results),
        "batch_id": batch_id,
        "complete": stopped is None,
        "stopped_reason": (stopped or {}).get("reason"),
        "resume_from": (stopped or {}).get("key"),
        "items_total": len(items),
        "manifest_sha256": manifest_sha256,
        "config_sha256": manifest_sha256,
        "updated_at": _utc_now_iso(),
        "report_path": str(report_path),
        "results": results,
    }
    write_json_atomic(report_path, report)
    return report


def summarize_batch_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Batch ok/partial contract: all-partial ≠ total failure.

    ``partial`` is true when any row is ok or partial and not every row is fully ok.
    ``listen_tokens`` totals the per-item listener tallies that are present.
    """
    all_ok = bool(results) and all(r.get("ok") for r in results)
    any_usable = any((r.get("ok") or r.get("partial"))
                     and not r.get("listen_rejected") for r in results)
    totals: dict[str, Any] = {"items_with_usage": 0}
    for row in results:
        usage = row.get("listen_usage") or {}
        if not isinstance(usage, dict) or not usage:
            continue
        totals["items_with_usage"] += 1
        for key in ("input_tokens", "output_tokens", "thinking_tokens",
                    "cache_read_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    return {
        "ok": all_ok,
        "partial": any_usable and not all_ok,
        "ok_count": sum(1 for r in results if r.get("ok")),
        "fail_count": sum(1 for r in results if not r.get("ok")),
        "listen_rejected_count": sum(
            1 for r in results
            if r.get("failure_kind") == "listen_rejected"
            or r.get("listen_rejected")),
        "listen_tokens": totals,
    }


ROLE_PITCH_RANGES: dict[str, tuple[int, int]] = {
    "auto": (0, 127),
    "drums": (24, 60),
    "bass": (24, 52),
    "pad": (48, 76),
    "lead": (55, 88),
}


def midi_file_to_notes(
    path: Path | str,
    *,
    tempo_bpm: float | None = None,
    max_bars: float | None = None,
    pitch_min: int = 0,
    pitch_max: int = 127,
) -> dict[str, Any]:
    """Read a Standard MIDI file → note dicts {pitch,start,duration,velocity} in beats."""
    import mido

    mid = mido.MidiFile(str(path))
    tpb = mid.ticks_per_beat or 480
    tempo_us = 500000
    for track in mid.tracks:
        for msg in track:
            if msg.type == "set_tempo":
                tempo_us = msg.tempo
                break
        else:
            continue
        break
    bpm = float(tempo_bpm) if tempo_bpm else (60_000_000.0 / float(tempo_us))

    # absolute-tick note spans across all tracks
    open_notes: dict[tuple[int, int], list[tuple[int, int]]] = {}
    finished: list[dict[str, Any]] = []
    for ti, track in enumerate(mid.tracks):
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                open_notes.setdefault((ti, msg.note), []).append((abs_tick, msg.velocity))
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                stack = open_notes.get((ti, msg.note))
                if not stack:
                    continue
                start_tick, vel = stack.pop()
                dur_ticks = max(1, abs_tick - start_tick)
                pitch = int(msg.note)
                if pitch < pitch_min or pitch > pitch_max:
                    continue
                start_beats = start_tick / tpb
                dur_beats = dur_ticks / tpb
                if max_bars is not None and start_beats >= float(max_bars) * 4.0:
                    continue
                if max_bars is not None:
                    limit = float(max_bars) * 4.0
                    if start_beats + dur_beats > limit:
                        dur_beats = max(0.05, limit - start_beats)
                finished.append(
                    {
                        "pitch": pitch,
                        "start": round(start_beats, 6),
                        "duration": round(dur_beats, 6),
                        "velocity": int(vel),
                    }
                )

    finished.sort(key=lambda n: (n["start"], n["pitch"]))
    return {
        "notes": finished,
        "tempo_bpm": round(bpm, 3),
        "ticks_per_beat": tpb,
        "note_count": len(finished),
    }


def get_ref_card(ref_id: str, index_path: Path | None = None) -> dict[str, Any]:
    rid = normalize_ref_id(ref_id)
    data = load_index(index_path)
    card = (data.get("refs") or {}).get(rid)
    if not card:
        # Apply the same policy to a filed card missing from the search index.
        out = ref_dir_for_id(rid)
        if out.is_dir() and _dir_is_filed_ref(out):
            card = card_from_out_dir(out)
    if card:
        suspect = _suspect_source(card, data.get("refs") or {})
        if suspect is not None:
            # The single steering chokepoint: answer_my_loop, beat_from_ref
            # and apply all resolve through here, so a suspect ref cannot
            # steer until the owner clears it. Fail closed with the reason.
            raise ValueError(
                "ref %s is marked suspect (%s%s) — steering refused; "
                "triage with --mark-suspect/--clear-suspect" % (
                    rid, (f"parent {suspect.get('ref_id')} is suspect: "
                          if suspect is not card else ""),
                    suspect.get("suspect_reason") or "no reason recorded"))
        if ref_is_shelved(card):
            # R8 quarantine: a dropped ref never answers (steered or
            # unsteered — both resolve here) until a KEEP lifts the shelf.
            # Search still matches it; only answers refuse.
            raise ValueError(
                "ref %s is shelved (%d drops, no keeps) — answers refused; "
                "judge it KEEP to lift the shelf" % (
                    rid, ref_tally_of(card)["dropped"]))
        out_raw = card.get("out_dir")
        if out_raw:
            try:
                out = ensure_out_dir_contained(Path(out_raw))
            except ValueError as exc:
                raise KeyError(f"ref_id {rid} points outside audio_refs: {exc}") from exc
            if not out.is_dir():
                raise KeyError(f"ref_id {rid} out_dir missing on disk: {out}")
        from tools.live_set_reader import _set_evidence_summary
        set_evidence = _set_evidence_summary(
            card, Path(index_path).parent / "audio_refs" if index_path else None)
        if set_evidence is not None:
            card = dict(card)
            card["set_evidence"] = set_evidence
        return card
    raise KeyError(f"unknown ref_id: {rid}")


def apply_audio_ref(
    ref_id: str,
    *,
    role: str = "auto",
    bars: float | None = 8,
    tempo: float | None = None,
    include_describe: bool = True,
    include_midi: bool = True,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Load a filed ref into a composing handoff package (no Ableton write).

    Returns describe text, imitation rules, optional MIDI notes filtered by role,
    and KEEP/DROP guidance for SIDEKICK JAM / compose_brief.
    """
    role_n = (role or "auto").strip().lower()
    if role_n not in ROLE_PITCH_RANGES:
        raise ValueError(f"role must be one of {tuple(ROLE_PITCH_RANGES)}, got {role!r}")

    rid = normalize_ref_id(ref_id)
    card = get_ref_card(rid, index_path=index_path)
    out_dir = ensure_out_dir_contained(Path(card.get("out_dir") or ref_dir_for_id(rid)))
    describe_path = out_dir / "describe.txt"
    midi_path = out_dir / "ref.mid"

    canonical: dict[str, Any] | None = None
    canonical_path = out_dir / "card.json"
    if canonical_path.is_file():
        try:
            canonical = validate_card(json.loads(
                canonical_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid validated card.json in {out_dir}: {exc}") from exc
        builder = (card_from_v3 if canonical.get("contract_version") == V3_CONTRACT_VERSION
                   else card_from_v2)
        card = {**card, **builder(canonical, ref_id=rid, out_dir=str(out_dir))}

    describe_text = ""
    warnings: list[str] = []
    if include_describe and canonical is None and describe_path.is_file():
        describe_text = describe_path.read_text(encoding="utf-8")
    elif include_describe and canonical is None:
        warnings.append("describe.txt missing")
    fields = parse_describe_card_fields(describe_text) if describe_text else {
        "identity": card.get("identity") or "",
        "imitation_do": card.get("imitation_do") or [],
        "imitation_dont": card.get("imitation_dont") or [],
        "describe_excerpt": card.get("describe_excerpt") or "",
    }

    pitch_min, pitch_max = ROLE_PITCH_RANGES[role_n]
    midi_info: dict[str, Any] | None = None
    notes: list[dict[str, Any]] = []
    if include_midi:
        if midi_path.is_file():
            midi_info = midi_file_to_notes(
                midi_path,
                tempo_bpm=tempo,
                max_bars=bars,
                pitch_min=pitch_min,
                pitch_max=pitch_max,
            )
            notes = list(midi_info["notes"])
            if role_n == "drums":
                warnings.append(
                    "Basic Pitch is weak on pure drums — treat MIDI as a sketch; "
                    "lean on describe imitation rules for the pocket."
                )
            if not notes:
                warnings.append(
                    f"no MIDI notes in pitch band for role={role_n} "
                    f"({pitch_min}-{pitch_max}); use describe rules to compose."
                )
        else:
            warnings.append("ref.mid missing — describe-only handoff")

    bpm = tempo or (midi_info or {}).get("tempo_bpm") or 120.0
    clip_name = f"ref {slugify(card.get('identity') or rid, max_len=28)} {role_n}"
    has_brief = bool(describe_text.strip()) or bool(fields.get("imitation_do"))
    has_notes = bool(notes)
    ok = has_brief or has_notes

    ricardo_note = card.get("ricardo_note")
    guidance = REF_GUIDANCE
    if ricardo_note:
        guidance = f"{REF_OVERLAY_PREFIX}{ricardo_note}. " + guidance

    return {
        "ok": ok,
        "ref_id": rid,
        "role": role_n,
        "card": card,
        "out_dir": str(out_dir),
        "describe_path": str(describe_path) if describe_path.is_file() else None,
        "describe": describe_text or None,
        "identity": fields.get("identity"),
        "imitation_do": fields.get("imitation_do") or [],
        "imitation_dont": fields.get("imitation_dont") or [],
        "structure": card.get("structure"),
        "ricardo_note": ricardo_note,
        "verdict": normalize_verdict(card.get("verdict")),
        "notes": notes,
        "note_count": len(notes),
        "tempo": bpm,
        "bars": bars,
        "midi_path": str(midi_path) if midi_path.is_file() else None,
        "clip_name": clip_name,
        "guidance": guidance,
        "keep_drop": REF_KEEP_DROP,
        "warnings": warnings,
    }


def _moment_transcribe_tempo(card: dict[str, Any] | None,
                             parent_meta: dict[str, Any] | None,
                             midi_tempo: float = 120.0) -> float:
    """Tempo a moment child transcribes at: the parent's measured tempo.

    Moment children inherit the parent's tempo context — the parent meta's
    measured.tempo_bpm, with the octave closest to the card's grid
    (grid_bpm, else feel_bpm) winning when the card names one. Without a
    usable measurement the card's grid carries the song; without either,
    the caller's guess. Deterministic. The index row never carries a tempo
    of its own, so reading card["tempo"] always fell back to 120."""
    card = card or {}
    struct = card.get("structure") or {}
    heard_tempo = (card.get("card") or {}).get("tempo") or {}
    grid = _num(struct.get("grid_bpm")) or _num(heard_tempo.get("grid_bpm"))
    feel = _num(struct.get("feel_bpm")) or _num(heard_tempo.get("feel_bpm"))
    measured = None
    raw = (parent_meta or {}).get("measured")
    if isinstance(raw, dict):
        measured = _num(raw.get("tempo_bpm"))
    if measured and 20 <= measured <= 400:
        ref = grid or feel
        if ref and ref > 0:
            return min((measured, measured / 2.0, measured * 2.0),
                       key=lambda c: abs(c - ref))
        return measured
    if grid and grid > 0:
        return grid
    try:
        guess = float(midi_tempo)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 120.0
    return guess if math.isfinite(guess) and guess > 0 else 120.0


def _moment_timestamps(parent: Path) -> list[dict[str, Any]]:
    """The timestamps a ref's moments may be cut at.

    A validated card.json (v2 or v3) is the only source when present: a
    describe.txt beside it may be a stale render of an unverified listen.
    Without a card (legacy prose describe) its own # Moments list is used,
    never a # Sections heading, which only v3 renders emit."""
    describe_path = parent / "describe.txt"
    canonical_path = parent / "card.json"
    stamped: list[dict[str, Any]] = []
    if canonical_path.is_file():
        # A validated card (v2 or v3) is the only source of its moments: a
        # describe.txt beside it may be a stale render of an unverified listen.
        try:
            canonical = validate_card(json.loads(canonical_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid validated card.json in {parent}: {exc}") from exc
        for section in canonical.get("sections") or []:
            match = _MOMENT_TS_RE.search(str(section.get("t") or ""))
            if match:
                stamped.append({
                    "t": _moment_seconds(match),
                    "text": str(section.get("label") or "section"),
                })
    else:
        # Legacy prose describe (no card contract): its own # Moments list only,
        # never a # Sections heading, which only v3 renders emit.
        legacy_text = (describe_path.read_text(encoding="utf-8")
                       if describe_path.is_file() else "")
        stamped = [m for m in extract_moment_lines(legacy_text, include_sections=False)
                   if m.get("t") is not None]
    return stamped


def clip_moments(
    ref_id: str,
    *,
    window_s: float = 45.0,
    pad_s: float = 5.0,
    modes: list[str] | None = None,
    midi_tempo: float = 120.0,
    index_path: Path | None = None,
    max_moments: int = 8,
) -> dict[str, Any]:
    """Cut jam-sized mini-refs from parent # Moments timestamps (no agy)."""
    modes = list(modes if modes is not None else ["midi", "features"])
    rid = normalize_ref_id(ref_id)
    card = get_ref_card(rid, index_path=index_path)
    parent = ensure_out_dir_contained(Path(card.get("out_dir") or ref_dir_for_id(rid)))
    wav = _require_local_source(parent)
    describe_path = parent / "describe.txt"
    canonical_path = parent / "card.json"
    if canonical_path.is_file():
        try:
            canonical = validate_card(json.loads(
                canonical_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid validated card.json in {parent}: {exc}") from exc
        builder = (card_from_v3 if canonical.get("contract_version") == V3_CONTRACT_VERSION
                   else card_from_v2)
        card = {**card, **builder(canonical, ref_id=rid, out_dir=str(parent))}
    parent_meta: dict[str, Any] = {}
    if (parent / "meta.json").is_file():
        try:
            loaded_meta = json.loads(
                (parent / "meta.json").read_text(encoding="utf-8"))
            if isinstance(loaded_meta, dict):
                parent_meta = loaded_meta
        except (OSError, json.JSONDecodeError):
            parent_meta = {}
    stamped = _moment_timestamps(parent)
    if not stamped:
        return {
            "ok": False,
            "partial": False,
            "parent_ref_id": rid,
            "error": "no moment timestamps under # Moments",
            "children": [],
        }

    dur = float(
        (card.get("duration_s") or 0)
        or ((card.get("structure") or {}).get("duration_s") or 0)
        or 0
    )
    if dur <= 0 and (parent / "features.json").is_file():
        try:
            feat = json.loads((parent / "features.json").read_text(encoding="utf-8"))
            dur = float(feat.get("duration_s") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            dur = 0.0
    if dur <= 0:
        dur = 1e9  # allow cuts; ffmpeg clamps via short output

    describe_text = (describe_path.read_text(encoding="utf-8")
                     if describe_path.is_file() else "")
    if canonical_path.is_file():
        # A validated card is canonical; the adjacent rendered prose may be stale.
        fields = {
            "imitation_do": list(card.get("imitation_do") or []),
            "imitation_dont": list(card.get("imitation_dont") or []),
        }
    else:
        fields = parse_describe_card_fields(describe_text)
    parent_linked = list(card.get("linked_projects") or [])
    children: list[dict[str, Any]] = []
    errors: list[str] = []

    for i, mom in enumerate(stamped[: max(1, int(max_moments))]):
        t = float(mom["t"])
        if dur < 1e8 and t > dur + 2.0:
            errors.append(f"moment {i} t={t:.1f} beyond duration {dur:.1f}")
            continue
        start = max(0.0, t - float(pad_s))
        end = start + float(window_s)
        if dur < 1e8:
            end = min(end, dur)
        if end <= start + 1.0:
            errors.append(f"moment {i} window too short after clamp")
            continue

        child_dir = make_out_dir(f"{rid}_m{i}", root=OUT_ROOT)
        child_created = True

        def discard_child() -> None:
            if child_created:
                shutil.rmtree(child_dir, ignore_errors=True)

        def append_child_failure(message: str) -> None:
            errors.append(message)
            children.append({
                "ok": False,
                "partial": False,
                "ref_id": child_dir.name,
                "moment_index": i,
                "moment_t": t,
                "start": start,
                "end": end,
                "out_dir": str(child_dir),
                "mode_ok": {m: False for m in modes},
                "warnings": [message],
            })

        try:
            stage_listenable_copy(wav, child_dir / "source.wav", start, end)
        except Exception as exc:
            discard_child()
            append_child_failure(f"moment {i} ffmpeg: {type(exc).__name__}: {exc}")
            continue

        child_describe = (
            f"# Identity\n"
            f"Moment from {card.get('title') or rid}: {mom['text']}\n\n"
            f"# Groove\n"
            f"- Parent pocket — see imitation rules.\n\n"
            f"# Harmony / melody\n"
            f"- See parent ref `{rid}`.\n\n"
            f"# Texture / production\n"
            f"- Clip window {format_mmss(start)}–{format_mmss(end)} of parent.\n\n"
            f"# Imitation rules\n"
            + "".join(f"- Do: {x}\n" for x in (fields.get("imitation_do") or [])[:4])
            + "".join(f"- Don't: {x}\n" for x in (fields.get("imitation_dont") or [])[:3])
            + f"\n# Moments\n- 00:00 – {mom['text']}\n"
        )
        try:
            _write_text_atomic(child_dir / "describe.txt", child_describe)
        except Exception as exc:
            discard_child()
            append_child_failure(
                f"moment {i} describe: {type(exc).__name__}: {exc}"
            )
            continue

        # New moment clips get only their own provenance.  Parent batch,
        # verdict, and project tags belong to the parent; linked_projects is
        # carried separately under its explicit inheritance contract.
        child_tags = ["moment", f"parent:{rid}"]

        mode_ok: dict[str, bool] = {m: False for m in modes}
        features: dict[str, Any] = {}
        warnings: list[str] = []
        midi_path = child_dir / "ref.mid"
        if "features" in modes or "midi" in modes:
            try:
                # light duration via ffprobe through features path if available
                if "midi" in modes:
                    # Moment children inherit the parent's tempo context;
                    # full-track DSP is measured once by analyze_audio_ref,
                    # never per window.
                    stats = run_basic_pitch(
                        child_dir / "source.wav", midi_path,
                        tempo=_moment_transcribe_tempo(
                            card, parent_meta, midi_tempo),
                    )
                    features.update({k: stats[k] for k in stats if k != "model_path_used"})
                    mode_ok["midi"] = midi_path.is_file()
                if "features" in modes:
                    # reuse wav stats from midi run or probe
                    if not features.get("duration_s"):
                        features["duration_s"] = end - start
                    features.setdefault("source", card.get("source"))
                    features.setdefault("source_kind", "moment_clip")
                    _write_text_atomic(
                        child_dir / "features.json",
                        json.dumps(features, indent=2, default=str) + "\n",
                    )
                    mode_ok["features"] = True
            except Exception as exc:
                warnings.append(f"{type(exc).__name__}: {exc}")
                mode_ok["midi"] = False
                if "features" in modes and not (child_dir / "features.json").is_file():
                    features = {
                        "duration_s": end - start,
                        "source": card.get("source"),
                        "source_kind": "moment_clip",
                    }
                    try:
                        _write_text_atomic(
                            child_dir / "features.json",
                            json.dumps(features, indent=2, default=str) + "\n",
                        )
                    except Exception as fallback_exc:
                        discard_child()
                        append_child_failure(
                            f"moment {i} features: "
                            f"{type(fallback_exc).__name__}: {fallback_exc}"
                        )
                        continue
                    mode_ok["features"] = True

        structure = parse_describe_structure(child_describe)
        all_ok = bool(modes) and all(bool(mode_ok.get(m)) for m in modes)
        any_ok = any(bool(mode_ok.get(m)) for m in modes)
        meta = {
            "ok": all_ok,
            "partial": (not all_ok) and any_ok,
            "ref_id": child_dir.name,
            "out_dir": str(child_dir),
            "parent_ref_id": rid,
            "moment_index": i,
            "moment_t": t,
            "moment_text": mom["text"],
            "source": card.get("source"),
            "source_kind": "moment_clip",
            "title": f"{card.get('title') or rid} · moment {i}",
            "start": start,
            "end": end,
            "focus": card.get("focus") or "full",
            "tags": child_tags,
            "linked_projects": parent_linked,
            "batch_id": card.get("batch_id"),
            "structure": structure,
            "features": features or None,
            "created_at": _utc_now_iso(),
            "warnings": warnings,
            "mode_ok": mode_ok,
        }
        try:
            write_json_atomic(child_dir / "meta.json", meta)
        except Exception as exc:
            discard_child()
            append_child_failure(f"moment {i} metadata: {type(exc).__name__}: {exc}")
            continue
        try:
            upsert_index_card(card_from_out_dir(child_dir, meta), path=index_path)
        except Exception as exc:
            discard_child()
            append_child_failure(f"moment {i} index upsert: {type(exc).__name__}: {exc}")
            continue
        children.append(
            {
                "ok": all_ok,
                "partial": (not all_ok) and any_ok,
                "ref_id": child_dir.name,
                "moment_index": i,
                "moment_t": t,
                "start": start,
                "end": end,
                "out_dir": str(child_dir),
                "mode_ok": dict(mode_ok),
                "warnings": warnings,
            }
        )

    any_child = bool(children)
    # Clipping errors (out-of-range timestamps, ffmpeg fails) are not success —
    # usable children plus dropped moments ⇒ partial, never ok=true.
    clip_errors = bool(errors)
    all_children_ok = (
        any_child and all(c.get("ok") for c in children) and not clip_errors
    )
    any_child_usable = any(c.get("ok") or c.get("partial") for c in children)
    return {
        "ok": all_children_ok,
        "partial": any_child_usable and not all_children_ok,
        "parent_ref_id": rid,
        "child_count": len(children),
        "children": children,
        "errors": errors or None,
    }


def cli_parser() -> argparse.ArgumentParser:
    """The analyze CLI parser. --focus choices derive from FOCUS_MODES."""
    p = argparse.ArgumentParser(
        description="Analyze audio/YouTube ref → describe.txt + features + ref.mid + index"
    )
    p.add_argument("source", nargs="?", default=None, help="Local audio path OR YouTube URL")
    p.add_argument(
        "--modes",
        default="describe,midi,features",
        help="Comma list: describe,midi,features (default: all)",
    )
    p.add_argument("--prompt-extra", default=None, help="Composer one-liner for describe")
    p.add_argument(
        "--focus",
        default="full",
        choices=FOCUS_CHOICES,
        help="Describe emphasis preset (you choose; default full)",
    )
    p.add_argument("--start", default=None, help="Clip start (seconds or mm:ss)")
    p.add_argument("--end", default=None, help="Clip end (seconds or mm:ss)")
    p.add_argument("--midi-tempo", type=float, default=120.0)
    p.add_argument(
        "--model",
        default=None,
        help=f"agy/Gemini model for describe (default: {DEFAULT_AGY_MODEL})",
    )
    p.add_argument("--timeout-s", type=int, default=180,
                   help="agy listener timeout in seconds (default: 180)")
    p.add_argument("--window-timeout-s", type=int, default=None,
                   help="agy timeout per Pass B window in seconds "
                        "(default: min(--timeout-s, 120))")
    p.add_argument("--pass-b", action="store_true",
                   help="After Pass A, cut 2-3 windows from the section "
                        "energy peaks and hear each for groove/drum "
                        "grid/sound design (merged as windows[])")
    p.add_argument("--allow-unverified-listen", action="store_true",
                   help="Persist a card even when its agy conversation "
                        "record cannot be read (fail-open override; the "
                        "card stays unverified)")
    p.add_argument("--listen-token-cap", type=int, default=None,
                   help="Max input+cache-read tokens for a full-track "
                        f"listen (default: {DEFAULT_LISTEN_TOKEN_CAP})")
    p.add_argument("--window-token-cap", type=int, default=None,
                   help="Max input+cache-read tokens per Pass B window "
                        f"(default: {DEFAULT_WINDOW_TOKEN_CAP})")
    p.add_argument("--from-dir", default=None, help="Reuse existing audio_refs folder")
    p.add_argument("--tags", default=None, help="Comma tags for index card")
    p.add_argument("--batch", default=None, help="Batch manifest JSON/JSONL/URL-list path")
    p.add_argument("--retry-rejected", action="store_true",
                   help="Explicitly retry batch rows rejected by listen verification")
    p.add_argument("--reindex", action="store_true", help="Rebuild audio_ref_index.json")
    p.add_argument("--restore-ownership", action="store_true",
                   help="Restore exact operator ownership registry into index/meta")
    p.add_argument(
        "--backup-overlays",
        action="store_true",
        help="Snapshot index + per-ref meta.json/describe.txt/card.json to store/backups/ (no audio)",
    )
    p.add_argument(
        "--enrich-structure",
        action="store_true",
        help="Re-parse describe.txt structure into meta+index (alias of --reindex)",
    )
    p.add_argument("--apply", default=None, help="Load ref_id into compose handoff package")
    p.add_argument("--apply-role", default="auto", help="Pitch band for --apply (auto|drums|bass|pad|lead)")
    p.add_argument("--apply-bars", type=float, default=8.0)
    p.add_argument("--find", default=None, help="Query index and print matches")
    p.add_argument("--find-focus", default=None, help="Filter --find by focus")
    p.add_argument("--find-tag", default=None, help="Filter --find by tag")
    p.add_argument("--find-verdict", default=None, help="Filter --find by verdict keep|drop|unset")
    p.add_argument("--find-k", type=int, default=5)
    p.add_argument("--mark-suspect", nargs=2, metavar=("REF_ID", "REASON"),
                   default=None,
                   help="Flag a ref suspect (out of steering/search until cleared)")
    p.add_argument("--clear-suspect", default=None, metavar="REF_ID",
                   help="Clear a suspect flag after owner review")
    p.add_argument(
        "--stamp-provenance",
        action="store_true",
        help="Dry run: list legacy v2 refs that would gain "
             "listener_mode interactive",
    )
    p.add_argument(
        "--stamp-apply",
        action="store_true",
        help="Write listener_mode interactive to unstamped legacy v2 refs",
    )
    p.add_argument("--note-ref", default=None, help="ref_id to attach Ricardo note/verdict")
    p.add_argument("--note", default=None, help="Ricardo overlay note (with --note-ref)")
    p.add_argument("--verdict", default=None, help="keep|drop|unset (with --note-ref)")
    p.add_argument("--project", default=None, help="Project tag (with --note-ref)")
    p.add_argument("--ownership", choices=OWNERSHIP_CHOICES,
                   default=None, help="operator authorship (with --note-ref)")
    p.add_argument(
        "--clip-moments",
        default=None,
        help="Parent ref_id → cut moment mini-refs (no agy)",
    )
    p.add_argument("--clip-window", type=float, default=45.0)
    p.add_argument("--clip-pad", type=float, default=5.0)
    p.add_argument(
        "--clip-modes",
        default="midi,features",
        help="Modes for --clip-moments (default midi,features; no describe/agy)",
    )
    p.add_argument(
        "--offload-sources",
        default=None,
        metavar="DEST",
        help="Copy ref source.wav files to DEST on an external volume "
             "(dry run; add --confirm to move and free the space)",
    )
    p.add_argument(
        "--restore-sources",
        action="store_true",
        help="Copy offloaded source.wav files back from their external volume "
             "(dry run; add --confirm to restore)",
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Execute --offload-sources / --restore-sources (default is a dry run)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    p = cli_parser()
    args = p.parse_args(argv)

    if args.confirm and args.offload_sources is None and not args.restore_sources:
        p.error("--confirm requires --offload-sources or --restore-sources")

    if args.backup_overlays:
        print(json.dumps(backup_overlays(), indent=2, default=str))
        return 0

    if args.restore_ownership:
        try:
            print(json.dumps(restore_ownership(), indent=2, default=str))
            return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
                  file=sys.stderr)
            return 1

    if args.offload_sources is not None or args.restore_sources:
        if args.offload_sources is not None and args.restore_sources:
            p.error("--offload-sources and --restore-sources are mutually exclusive")
        if args.offload_sources is not None:
            # Refuse an unusable destination BEFORE the overlay backup: the
            # snapshot is only worth taking for a run that can proceed.
            try:
                _require_external_dest(args.offload_sources)
            except (OSError, ValueError) as exc:
                print(
                    json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
                    file=sys.stderr,
                )
                return 1
        if args.confirm:
            # Same guard as --reindex: a confirmed offload/restore rewrites
            # meta.json per ref — back the judgment layer up first. A backup
            # that cannot be taken refuses the whole run: proceeding would
            # rewrite the only copy of the overlays with no snapshot.
            try:
                bk = backup_overlays()
                print(json.dumps({"overlay_backup": bk["archive"]}), file=sys.stderr)
            except Exception as exc:
                print(json.dumps({"overlay_backup_failed": str(exc)}), file=sys.stderr)
                return 1
        try:
            if args.offload_sources is not None:
                result = offload_sources(args.offload_sources, confirm=args.confirm)
            else:
                result = restore_sources(confirm=args.confirm)
        except (OSError, ValueError, RuntimeError) as exc:
            print(
                json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok") else 1

    if args.reindex or args.enrich_structure:
        # Back the judgments up FIRST: reindex rewrites every card, and a bad
        # scan is exactly the accident that would eat the overlays.
        try:
            bk = backup_overlays()
            print(json.dumps({"overlay_backup": bk["archive"]}), file=sys.stderr)
        except OSError as exc:
            print(json.dumps({"overlay_backup_failed": str(exc)}), file=sys.stderr)
        print(json.dumps(enrich_structure(), indent=2))
        return 0

    if args.note_ref:
        try:
            tag_list = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
            result = tag_audio_ref(
                args.note_ref,
                tags=tag_list or None,
                project=args.project,
                note=args.note,
                verdict=args.verdict,
                ownership=args.ownership,
            )
        except Exception as exc:
            print(
                json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.mark_suspect:
        try:
            result = mark_ref_suspect(args.mark_suspect[0], args.mark_suspect[1])
        except Exception as exc:
            print(
                json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.clear_suspect:
        try:
            result = clear_ref_suspect(args.clear_suspect)
        except Exception as exc:
            print(
                json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.stamp_provenance or args.stamp_apply:
        try:
            result = stamp_provenance(apply=args.stamp_apply)
        except Exception as exc:
            print(
                json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok") else 1

    if args.clip_moments:
        try:
            modes = [m.strip() for m in args.clip_modes.split(",") if m.strip()]
            result = clip_moments(
                args.clip_moments,
                window_s=args.clip_window,
                pad_s=args.clip_pad,
                modes=modes,
                midi_tempo=args.midi_tempo,
            )
        except Exception as exc:
            print(
                json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok") else 1

    if args.apply:
        try:
            result = apply_audio_ref(
                args.apply, role=args.apply_role, bars=args.apply_bars
            )
        except Exception as exc:
            print(
                json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok") else 1

    if args.find is not None or args.find_tag or args.find_focus or args.find_verdict:
        hits = find_audio_refs(
            query=args.find,
            focus=args.find_focus,
            tag=args.find_tag,
            verdict=args.find_verdict,
            k=args.find_k,
        )
        print(json.dumps(hits, indent=2, default=str))
        return 0

    if args.batch:
        try:
            result = run_batch(
                args.batch, timeout_s=args.timeout_s,
                allow_unverified_listen=args.allow_unverified_listen,
                listen_token_cap=args.listen_token_cap,
                retry_rejected=args.retry_rejected,
            )
        except Exception as exc:
            print(
                json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok") else 1

    if not args.source and not args.from_dir:
        p.error(
            "source or --from-dir or --batch or --reindex or --find or "
            "--note-ref or --clip-moments or --stamp-provenance required"
        )

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    if not modes:
        print(
            json.dumps({"ok": False, "error": "modes must be non-empty"}),
            file=sys.stderr,
        )
        return 1
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    try:
        result = analyze_audio_ref(
            args.source,
            modes=modes,
            prompt_extra=args.prompt_extra,
            start=args.start,
            end=args.end,
            midi_tempo=args.midi_tempo,
            model=args.model,
            focus=args.focus,
            from_dir=args.from_dir,
            tags=tags or None,
            timeout_s=args.timeout_s,
            pass_b=args.pass_b,
            allow_unverified_listen=args.allow_unverified_listen,
            listen_token_cap=args.listen_token_cap,
            window_token_cap=args.window_token_cap,
            window_timeout_s=args.window_timeout_s,
        )
    except Exception as exc:
        print(
            json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
