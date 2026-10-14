"""Phrase-curve library: HIS automation gestures, normalized and retrievable.

store/automation.json holds 1,149 raw envelopes — whole-song rides. This module
segments them into gesture-sized chunks (empirically: gap-splitting at 16 beats
yields ~700 volume gestures, median ~10 beats), normalizes per family, classifies
shape, dedupes into a small template library (store/phrase_curves.json), and picks/
renders curves deterministically for the envelope writer.

Pure + stdlib; loads only its own artifact. Value normalization per family:
  volume : .als linear gain (1.0 = 0 dB, max ≈ 1.995 = +6 dB) → dB clamp[-60,+6] → (dB+60)/66
  send   : already 0..1
  cutoff : AutoFilter semitone units, observed span exactly 20..135 → (v-20)/115
  pan    : -1..1 → (v+1)/2
Raw values are NEVER written to the Live API — render_curve maps a template into an
explicit [lo, hi] the caller chooses (see DEFAULT_WRITE_RANGES for the safe caps).
"""
from __future__ import annotations

import json
import math
import os
import random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CURVES_JSON = os.path.join(ROOT, "store", "phrase_curves.json")

GAP_SPLIT_BEATS = 16.0
MIN_POINTS, MAX_POINTS = 3, 48
MIN_SPAN, MAX_SPAN = 2.0, 64.0
REVERSAL_MIN = 0.15
RESAMPLE_K = 16
DEDUPE_DIST = 0.08
PER_BUCKET_CAP = 6
DUR_BUCKETS = (1, 2, 4, 8)          # bars
MIN_DEPTH = 0.05                    # family-normalized; flatter segments aren't gestures

FAMILIES = {
    "volume": ("Mixer", "Volume"),
    "send": ("TrackSendHolder", "Send"),
    "cutoff": ("AutoFilter", "Cutoff"),
    "pan": ("Mixer", "Pan"),
}

CLASSES = ("rise", "fall", "arc", "dip", "wiggle")
CHARACTER_TO_CLASS = {"swell": "rise", "fade": "fall", "arc": "arc",
                      "dip": "dip", "wiggle": "wiggle"}
FAMILY_DEFAULT_CLASS = {"volume": "rise", "send": "arc", "cutoff": "arc", "pan": "wiggle"}

# Write-side amplitude caps (shape-true, amplitude-safe v1). volume/send are absolute
# Live-API parameter values (mixer volume 0.85 ≈ 0 dB — never boost above unity);
# cutoff/pan are FRACTIONS of the live parameter's own [min, max] read at write time.
DEFAULT_WRITE_RANGES = {
    "volume": (0.0, 0.85),
    "send": (0.0, 0.6),
    "cutoff": (0.2, 1.0),
    "pan": (0.25, 0.75),
}

_LIB_CACHE: dict | None = None


def family_of(target: str) -> str | None:
    """Family for an automation target path — matched on its LAST TWO components."""
    parts = [p for p in str(target or "").split("/") if p]
    if len(parts) < 2:
        return None
    tail = (parts[-2], parts[-1])
    for fam, match in FAMILIES.items():
        if tail == match:
            return fam
    return None


def normalize_value(family: str, v: float) -> float:
    if family == "volume":
        db = 20.0 * math.log10(max(float(v), 1e-3))
        db = max(-60.0, min(6.0, db))
        return (db + 60.0) / 66.0
    if family == "send":
        return max(0.0, min(1.0, float(v)))
    if family == "cutoff":
        return max(0.0, min(1.0, (float(v) - 20.0) / 115.0))
    if family == "pan":
        return max(0.0, min(1.0, (float(v) + 1.0) / 2.0))
    raise ValueError(f"unknown family {family!r}")


def _split_on_gaps(events: list[list[float]]) -> list[list[list[float]]]:
    chunks, cur = [], []
    for ev in events:
        if cur and ev[0] - cur[-1][0] > GAP_SPLIT_BEATS:
            chunks.append(cur)
            cur = []
        cur.append(ev)
    if cur:
        chunks.append(cur)
    return chunks


def _split_long_chunk(chunk: list[list[float]], family: str) -> list[list[list[float]]]:
    """Chunks longer than MAX_SPAN split at big internal reversals; arcs stay whole."""
    span = chunk[-1][0] - chunk[0][0]
    if span <= MAX_SPAN or len(chunk) < 5:
        return [chunk]
    norm = [normalize_value(family, v) for _, v in chunk]
    cuts = []
    for i in range(1, len(chunk) - 1):
        prev_dir = norm[i] - norm[i - 1]
        next_dir = norm[i + 1] - norm[i]
        if prev_dir * next_dir < 0:
            left = max(abs(norm[i] - norm[j]) for j in range(max(0, i - 8), i))
            right = max(abs(norm[i] - norm[j]) for j in range(i + 1, min(len(chunk), i + 9)))
            if min(left, right) >= REVERSAL_MIN:
                cuts.append(i)
    if not cuts:
        return [chunk]
    out, start = [], 0
    for c in cuts:
        out.append(chunk[start:c + 1])
        start = c
    out.append(chunk[start:])
    return [p for p in out if len(p) >= 2]


def segment_envelope(events: list[list[float]], family: str) -> list[list[list[float]]]:
    """Raw [[beat, value]] → gesture-sized segments (sorted, gap- and reversal-split,
    size/span filtered)."""
    if not events:
        return []
    events = sorted(events, key=lambda e: (e[0], e[1]))
    out = []
    for chunk in _split_on_gaps(events):
        for seg in _split_long_chunk(chunk, family):
            n = len(seg)
            span = seg[-1][0] - seg[0][0]
            if MIN_POINTS <= n <= MAX_POINTS and MIN_SPAN <= span <= MAX_SPAN:
                out.append(seg)
    return out


def _resample(t01: list[float], v01: list[float], k: int) -> list[float]:
    """Linear resample of a (t01, v01) polyline at k evenly spaced times."""
    out = []
    j = 0
    for i in range(k):
        t = i / (k - 1)
        while j < len(t01) - 2 and t01[j + 1] < t:
            j += 1
        t0, t1 = t01[j], t01[j + 1]
        v0, v1 = v01[j], v01[j + 1]
        out.append(v0 if t1 <= t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0))
    return out


def classify_shape(shape: list[float]) -> str:
    net = shape[-1] - shape[0]
    imax = max(range(len(shape)), key=lambda i: shape[i])
    imin = min(range(len(shape)), key=lambda i: shape[i])
    reversals = 0
    last_dir = 0.0
    for a, b in zip(shape, shape[1:]):
        d = b - a
        if abs(d) < 0.02:
            continue
        if last_dir and d * last_dir < 0:
            reversals += 1
        last_dir = d
    if reversals >= 3:
        return "wiggle"
    frac_max = imax / (len(shape) - 1)
    frac_min = imin / (len(shape) - 1)
    if net >= 0.4:
        return "rise"
    if net <= -0.4:
        return "fall"
    if 0.2 < frac_max < 0.8 and shape[imax] - max(shape[0], shape[-1]) >= 0.3:
        return "arc"
    if 0.2 < frac_min < 0.8 and min(shape[0], shape[-1]) - shape[imin] >= 0.3:
        return "dip"
    if net >= 0.15:
        return "rise"
    if net <= -0.15:
        return "fall"
    return "wiggle"


def _template_from_segment(seg: list[list[float]], family: str, provenance: dict) -> dict | None:
    t0, t1 = seg[0][0], seg[-1][0]
    span = t1 - t0
    fam_vals = [normalize_value(family, v) for _, v in seg]
    vmin, vmax = min(fam_vals), max(fam_vals)
    depth = vmax - vmin
    if depth < MIN_DEPTH:
        return None
    t01 = [(t - t0) / span for t, _ in seg]
    v01 = [(v - vmin) / depth for v in fam_vals]
    shape = [round(x, 4) for x in _resample(t01, v01, RESAMPLE_K)]
    bars = span / 4.0
    bucket = min(DUR_BUCKETS, key=lambda b: abs(b - bars))
    return {
        "family": family,
        "class": classify_shape(shape),
        "dur_bucket": bucket,
        "points": [[round(t, 4), round(v, 4)] for t, v in zip(t01, v01)],
        "shape16": shape,
        "depth": round(depth, 4),
        "start_level": round(fam_vals[0], 4),
        "end_level": round(fam_vals[-1], 4),
        "provenance": {**provenance, "t0_beats": round(t0, 3), "span_beats": round(span, 3)},
    }


def _shape_dist(a: list[float], b: list[float]) -> float:
    return (sum((x - y) ** 2 for x, y in zip(a, b)) / len(a)) ** 0.5


def build_curve_library(automation: dict) -> dict:
    """automation.json → deduped template library. Deterministic: fixed iteration
    order, greedy medoid keep (first-seen wins), per-(family,class,bucket) cap."""
    kept: dict[tuple, list[dict]] = {}
    n_segments = 0
    projects = sorted(automation.get("projects", []), key=lambda p: str(p.get("id")))
    for proj in projects:
        for tr in sorted(proj.get("automation_tracks", []), key=lambda t: str(t.get("track"))):
            for env in sorted(tr.get("envelopes", []), key=lambda e: str(e.get("target"))):
                fam = family_of(env.get("target", ""))
                if fam is None:
                    continue
                prov = {"project": proj.get("name"), "track": tr.get("track"),
                        "target": env.get("target")}
                for seg in segment_envelope(env.get("events", []), fam):
                    tpl = _template_from_segment(seg, fam, prov)
                    if tpl is None:
                        continue
                    n_segments += 1
                    key = (tpl["family"], tpl["class"], tpl["dur_bucket"])
                    bucket = kept.setdefault(key, [])
                    if len(bucket) >= PER_BUCKET_CAP:
                        continue
                    if all(_shape_dist(tpl["shape16"], k["shape16"]) > DEDUPE_DIST
                           for k in bucket):
                        bucket.append(tpl)
    templates = [t for _, b in sorted(kept.items()) for t in b]
    return {
        "schema_version": 1,
        "built_from": "store/automation.json",
        "n_gesture_segments": n_segments,
        "n_templates": len(templates),
        "by_bucket": {f"{k[0]}/{k[1]}/{k[2]}bar": len(v) for k, v in sorted(kept.items())},
        "templates": templates,
    }


def _library() -> dict:
    global _LIB_CACHE
    if _LIB_CACHE is None:
        if not os.path.exists(CURVES_JSON):
            raise ValueError("store/phrase_curves.json missing — run tools/build_phrase_curves.py")
        with open(CURVES_JSON) as fh:
            _LIB_CACHE = json.load(fh)
    return _LIB_CACHE


def set_library_for_test(lib: dict | None) -> None:
    global _LIB_CACHE
    _LIB_CACHE = lib


def pick_curve(family: str, bars: int, seed: int = 0, character: str | None = None,
               curves: dict | None = None) -> dict:
    """Deterministically pick a template: family + (character→class | family default),
    nearest duration bucket, falling back to the class's other buckets, then any class
    in the family. Raises with the available inventory when the family has nothing."""
    if family not in FAMILIES:
        raise ValueError(f"family must be one of {sorted(FAMILIES)}, got {family!r}")
    if character is not None and character not in CHARACTER_TO_CLASS:
        raise ValueError(f"character must be one of {sorted(CHARACTER_TO_CLASS)}")
    lib = curves or _library()
    fam_templates = [t for t in lib.get("templates", []) if t["family"] == family]
    if not fam_templates:
        raise ValueError(f"no {family!r} templates in the library "
                         f"(have: {sorted({t['family'] for t in lib.get('templates', [])})})")
    cls = CHARACTER_TO_CLASS[character] if character else FAMILY_DEFAULT_CLASS[family]
    in_class = [t for t in fam_templates if t["class"] == cls] or fam_templates
    want = min(DUR_BUCKETS, key=lambda b: abs(b - max(1, bars)))
    best_bucket = min({t["dur_bucket"] for t in in_class}, key=lambda b: abs(b - want))
    cands = sorted((t for t in in_class if t["dur_bucket"] == best_bucket),
                   key=lambda t: (t["provenance"]["project"], t["provenance"]["track"],
                                  t["provenance"]["t0_beats"]))
    rng = random.Random(f"curve:{seed}:{family}:{bars}:{character}")
    return rng.choice(cands)


def render_curve(template: dict, bars: int, lo: float, hi: float,
                 beats_per_bar: float = 4.0, max_steps: int = 64) -> list[list[float]]:
    """Template → explicit [[beat, value]] over `bars`, values mapped into [lo, hi].
    Densified to ~8 steps/bar (staircase smoothing for insert_step writes)."""
    if hi < lo:
        lo, hi = hi, lo
    total = max(1, int(bars)) * beats_per_bar
    steps = min(max_steps, max(2, int(8 * max(1, int(bars)))))
    t01 = [p[0] for p in template["points"]]
    v01 = [p[1] for p in template["points"]]
    vals = _resample(t01, v01, steps)
    out = []
    for i, v in enumerate(vals):
        beat = round(total * i / (steps - 1), 4)
        out.append([beat, round(lo + (hi - lo) * max(0.0, min(1.0, v)), 5)])
    return out
