"""Build a style profile of Ricardo's MIDI catalog — pure stdlib, full corpus.

Reads the materialized corpus (store/projects/_index.json and one JSON per
project, so run ingest/materialize.py first) and, for every project with MIDI notes,
computes corpus-wide statistics that describe his compositional voice. Drum /
percussion tracks are separated out: they inform GROOVE but never the harmonic
analysis (key, melody, register), since percussion has no harmonic pitch.

Output is written two ways:
  - store/STYLE.md          — leads with a PLAIN-LANGUAGE description (no jargon),
                              then the data behind it. Hand-editable.
  - store/style_profile.json — machine-readable, per-project + aggregate.

No third-party deps — runs on system Python.

Usage:
    python3 analyze/style_profile.py
    python3 analyze/style_profile.py --limit 50
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ingest"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_als import from_dict, is_blocked_project, is_main_clip_source  # noqa: E402
from corpus_keys import song_family  # noqa: E402
from roles import clip_role  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJ_DIR = os.path.join(ROOT, "store", "projects")
HARMONY_JSON = os.path.join(ROOT, "store", "harmony.json")
STRUCTURE_JSON = os.path.join(ROOT, "store", "structure.json")
NOTES_MD = os.path.join(ROOT, "store", "NOTES.md")
PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _git_rev() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        ).strip()
        return out or "unknown"
    except Exception:
        return "unknown"


def _iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_meta(n_projects: int, schema_version: int = 1,
               manifest_time: str | None = None) -> dict:
    corpus = {"n_projects": int(n_projects)}
    if manifest_time is not None:
        corpus["manifest_time"] = manifest_time  # catalog scan this build saw
    return {
        "schema_version": schema_version,
        "built_at": _iso_utc(),
        "code_rev": _git_rev(),
        "corpus": corpus,
    }


def _save_json_atomic(path: str, data: dict, indent: int | None = 2) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(fd, "w") as fh:
            if indent is not None:
                json.dump(data, fh, indent=indent)
            else:
                json.dump(data, fh)
            fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _write_text_atomic(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise

KK_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
KK_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


# --- key finding ------------------------------------------------------------

def _pearson(a, b):
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((x - mb) ** 2 for x in b))
    return num / (da * db) if da and db else 0.0


def estimate_key(pc_hist):
    best = (None, None, -2.0)
    for tonic in range(12):
        rotated = pc_hist[tonic:] + pc_hist[:tonic]
        for mode, profile in (("major", KK_MAJOR), ("minor", KK_MINOR)):
            corr = _pearson(rotated, profile)
            if corr > best[2]:
                best = (PITCH_NAMES[tonic], mode, corr)
    return best


MODE_STEPS = {
    "major": [0, 2, 4, 5, 7, 9, 11],
    "ionian": [0, 2, 4, 5, 7, 9, 11],
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "aeolian": [0, 2, 3, 5, 7, 8, 10],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "phrygian": [0, 1, 3, 5, 7, 8, 10],
    "harmonic minor": [0, 2, 3, 5, 7, 8, 11],
    "bhairav": [0, 1, 4, 5, 7, 8, 11],
}
KNOWN_MODES = frozenset(MODE_STEPS)


def diatonic_set(tonic_idx, mode):
    """Pitch classes of ``mode`` on ``tonic_idx``. Unknown mode names fall back
    to natural minor (the historical behaviour); callers that care check
    KNOWN_MODES first."""
    steps = MODE_STEPS.get(str(mode).strip().lower(), MODE_STEPS["minor"])
    return {(tonic_idx + s) % 12 for s in steps}


def _std(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


# --- per-project feature extraction -----------------------------------------

def _groove(notes):
    g = [0] * 16
    for n in notes:
        g[int(round((n.start % 4.0) * 4)) % 16] += 1
    return g


def _skyline_intervals(clips):
    """Top-voice melodic intervals, so chord tones don't masquerade as melody."""
    intervals = Counter()
    for c in clips:
        top = {}
        for n in c.notes:
            if not n.enabled:
                continue
            slot = round(n.start * 4)
            if slot not in top or n.pitch > top[slot].pitch:
                top[slot] = n
        seq = [top[k] for k in sorted(top)]
        for a, b in zip(seq, seq[1:]):
            iv = max(-24, min(24, b.pitch - a.pitch))
            if iv != 0:
                intervals[iv] += 1
    return intervals


def _dna_accumulate(clips, role_of, vel_slots, timing_acc):
    """Per-role velocity-contour + micro-timing sums (the humanize-like-me DNA).

    Velocity only counts clips that are velocity-VARIED — a clip drawn at one velocity
    is mouse input, not his hands, and would collapse the contour toward defaults.
    Timing deviation is measured against the 16th grid and clamped to ±0.125 beats."""
    for c in clips:
        enabled = [n for n in c.notes if n.enabled]
        if not enabled:
            continue
        role = role_of(c)
        varied = len({n.velocity for n in enabled}) > 1
        va = vel_slots[role]
        ta = timing_acc[role]
        for n in enabled:
            if varied:
                slot = int(round((n.start % 4.0) * 4)) % 16
                va["sum"][slot] += n.velocity
                va["sumsq"][slot] += n.velocity * n.velocity
                va["n"][slot] += 1
            dev = max(-0.125, min(0.125, n.start - round(n.start * 4) / 4.0))
            ta["n"] += 1
            if abs(dev) > 1e-6:
                ta["n_offgrid"] += 1
            ta["sum_dev"] += dev
            ta["sumsq_dev"] += dev * dev


def analyze_project(p):
    main_clips = [c for c in p.clips
                  if is_main_clip_source(c.clip_source) and not c.is_sidekick]
    harm_clips = [c for c in main_clips if not c.is_drum]
    drum_clips = [c for c in main_clips if c.is_drum]
    harm = [n for c in harm_clips for n in c.notes if n.enabled]
    drum = [n for c in drum_clips for n in c.notes if n.enabled]
    if not harm and not drum:
        return None

    rec = {
        "name": os.path.splitext(os.path.basename(p.path))[0],
        "tempo": p.tempo,
        "harmonic_notes": len(harm),
        "drum_notes": len(drum),
        "has_harmonic": bool(harm),
        "drum_groove": _groove(drum) if drum else [0] * 16,
        "harmonic_groove": _groove(harm) if harm else [0] * 16,
    }

    if harm:
        pc_hist = [0.0] * 12
        pitch_hist = [0] * 128
        for n in harm:
            pc_hist[n.pitch % 12] += 1
            if 0 <= n.pitch < 128:
                pitch_hist[n.pitch] += 1
        tonic, mode, corr = estimate_key(pc_hist)
        tonic_idx = PITCH_NAMES.index(tonic)
        dia = diatonic_set(tonic_idx, mode)
        in_scale = sum(pc_hist[i] for i in dia)
        velocities = [n.velocity for n in harm]
        pitches = [n.pitch for n in harm]
        poly = []
        for c in harm_clips:
            poly.extend(Counter(round(n.start * 4) for n in c.notes if n.enabled).values())
        span = max(n.start + n.duration for n in harm) - min(n.start for n in harm)

        t1_count = Counter()
        t2_count = Counter()
        deg_count = Counter()
        t1_by_role: dict = defaultdict(Counter)
        t2_by_role: dict = defaultdict(Counter)
        deg_by_role: dict = defaultdict(Counter)
        for c in harm_clips:
            role = clip_role(c.notes, c.is_drum)
            top = {}
            for n in c.notes:
                if not n.enabled:
                    continue
                slot = round(n.start * 4)
                if slot not in top or n.pitch > top[slot].pitch:
                    top[slot] = n
            seq = [top[k].pitch for k in sorted(top)]
            if not seq:
                continue
            degrees = [(pitch - tonic_idx) % 12 for pitch in seq]
            for deg in degrees:
                deg_count[deg] += 1
                deg_by_role[role][deg] += 1
            for a, b in zip(degrees, degrees[1:]):
                t1_count[f"{a}->{b}"] += 1
                t1_by_role[role][f"{a}->{b}"] += 1
            for a, b, c_note in zip(degrees, degrees[1:], degrees[2:]):
                t2_count[f"{a},{b}->{c_note}"] += 1
                t2_by_role[role][f"{a},{b}->{c_note}"] += 1

        rec.update({
            "key": f"{tonic} {mode}",
            "key_confidence": round(corr, 3),
            "scale_conformity": round(in_scale / sum(pc_hist), 3) if sum(pc_hist) else 0.0,
            "pitch_min": min(pitches), "pitch_max": max(pitches),
            "pitch_mean": round(sum(pitches) / len(pitches), 1),
            "velocity_mean": round(sum(velocities) / len(velocities), 1),
            "velocity_std": round(_std(velocities), 1),
            "avg_polyphony": round(sum(poly) / len(poly), 2) if poly else 0.0,
            "density_notes_per_beat": round(len(harm) / span, 2) if span > 0 else 0.0,
            "intervals": {str(k): v for k, v in _skyline_intervals(harm_clips).items()},
            "durations": [round(n.duration, 3) for n in harm[:5000]],
            "pitch_hist": pitch_hist,
            "transitions_1": dict(t1_count),
            "transitions_2": dict(t2_count),
            "degree_counts": {str(k): v for k, v in deg_count.items()},
            "transitions_1_by_role": {r: dict(c) for r, c in t1_by_role.items()},
            "transitions_2_by_role": {r: dict(c) for r, c in t2_by_role.items()},
            "degree_counts_by_role": {r: {str(k): v for k, v in c.items()} for r, c in deg_by_role.items()},
        })

    vel_slots = defaultdict(lambda: {"sum": [0.0] * 16, "sumsq": [0.0] * 16, "n": [0] * 16})
    timing_acc = defaultdict(lambda: {"n": 0, "n_offgrid": 0, "sum_dev": 0.0, "sumsq_dev": 0.0})
    _dna_accumulate(harm_clips, lambda c: clip_role(c.notes, c.is_drum), vel_slots, timing_acc)
    _dna_accumulate(drum_clips, lambda c: "drum", vel_slots, timing_acc)
    if timing_acc:
        rec["velocity_slots_by_role"] = dict(vel_slots)
        rec["timing_dev_by_role"] = dict(timing_acc)
    return rec


# --- corpus aggregation -----------------------------------------------------

_IV_NAME = {0: "unison", 1: "m2", 2: "M2", 3: "m3", 4: "M3", 5: "P4", 6: "tritone",
            7: "P5", 8: "m6", 9: "M6", 10: "m7", 11: "M7", 12: "octave"}


def _note_name(midi):
    """MIDI number → name like 'C4' (middle C = 60)."""
    if midi is None:
        return "?"
    return f"{PITCH_NAMES[midi % 12]}{midi // 12 - 1}"


def _pct_from_hist(hist, pct):
    """Pitch (MIDI number) at the given percentile of a 128-bin histogram."""
    total = sum(hist)
    if not total:
        return None
    target = total * pct / 100.0
    run = 0
    for pitch, count in enumerate(hist):
        run += count
        if run >= target:
            return pitch
    return 127


def _dur_bucket(d):
    for label, val in [("1/16", 0.25), ("1/8", 0.5), ("1/4", 1.0), ("1/2", 2.0), ("whole", 4.0)]:
        if d <= val * 1.25:
            return label
    return ">whole"


def _tempo_bins(tempos):
    bins = Counter()
    for t in tempos:
        lo = int(t // 10 * 10)
        bins[f"{lo}-{lo+9}"] += 1
    return bins.most_common(6)


def _interval_summary(intervals):
    total = sum(intervals.values()) or 1
    out = []
    for iv, count in intervals.most_common(10):
        name = _IV_NAME.get(abs(iv), f"{abs(iv)}st")
        out.append({"semitones": iv, "name": f"{name} {'up' if iv > 0 else 'down'}",
                    "pct": round(100 * count / total, 1)})
    return out


def _grammar_from_counts(t1_global, t2_global, deg_global):
    """Build {unigram, order_1, order_2} probability dicts from degree-transition
    counts — shared by the pooled grammar and each per-role grammar."""
    total_deg = sum(deg_global.values())
    unigram = ({str(k): round(v / total_deg, 4) for k, v in deg_global.items()}
               if total_deg > 0 else {})

    t1_by_src: dict = {}
    for key, count in t1_global.items():
        src_str, dst_str = key.split("->")
        t1_by_src.setdefault(int(src_str), {})[int(dst_str)] = count
    order_1 = {}
    for src, dsts in t1_by_src.items():
        tot = sum(dsts.values())
        if tot > 0:
            order_1[str(src)] = {str(d): round(c / tot, 4) for d, c in dsts.items()}

    t2_by_src: dict = {}
    for key, count in t2_global.items():
        src_str, dst_str = key.split("->")
        t2_by_src.setdefault(src_str, {})[int(dst_str)] = count
    order_2 = {}
    for src_str, dsts in t2_by_src.items():
        tot = sum(dsts.values())
        if tot > 0:
            order_2[src_str] = {str(d): round(c / tot, 4) for d, c in dsts.items()}

    return {"unigram": unigram, "order_1": order_1, "order_2": order_2}


def _family_capped(projects: list[dict], field: str) -> Counter:
    """Merge per-project {move: count} counters so each song family casts one
    vote: its richest member's count per move. Version-saves of one song can
    no longer outvote distinct songs in the grammar."""
    per_family: dict[str, list[dict]] = {}
    for p in projects:
        per_family.setdefault(song_family(p.get("name", "")), []).append(
            p.get(field, {}) or {})
    merged = Counter()
    for counters in per_family.values():
        for move in set().union(*[set(c) for c in counters]) if counters else ():
            merged[move] += max(c.get(move, 0) for c in counters)
    return merged


def _family_capped_by_role(projects: list[dict], field: str) -> dict:
    """Per-role variant of _family_capped ({role: {move: count}})."""
    roles = set()
    for p in projects:
        roles.update((p.get(field, {}) or {}).keys())
    return {r: _family_capped(
        [{"name": p.get("name", ""),
          field: (p.get(field, {}) or {}).get(r, {})} for p in projects],
        field) for r in roles}


def aggregate(projects):
    harm_projects = [p for p in projects if p["has_harmonic"]]
    keys = Counter(p["key"] for p in harm_projects)
    modes = Counter(p["key"].split()[1] for p in harm_projects)
    tonics = Counter(p["key"].split()[0] for p in harm_projects)
    tempos = [p["tempo"] for p in projects if p["tempo"]]

    intervals, hgroove, dgroove, durations = Counter(), [0] * 16, [0] * 16, Counter()
    for p in projects:
        for i in range(16):
            hgroove[i] += p["harmonic_groove"][i]
            dgroove[i] += p["drum_groove"][i]
    corpus_pitch = [0] * 128
    for p in harm_projects:
        for k, v in p["intervals"].items():
            intervals[int(k)] += v
        for d in p["durations"]:
            durations[_dur_bucket(d)] += 1
        for i, v in enumerate(p.get("pitch_hist", [])):
            corpus_pitch[i] += v

    # Grammar counts one vote per song family (not per .als file): version
    # duplicates of a song collapse to their richest member's counts.
    t1_global = _family_capped(harm_projects, "transitions_1")
    t2_global = _family_capped(harm_projects, "transitions_2")
    deg_global = Counter({int(k): v for k, v in
                          _family_capped(harm_projects, "degree_counts").items()})
    roles_t1 = {r: c for r, c in
                _family_capped_by_role(harm_projects, "transitions_1_by_role").items()}
    roles_t2 = {r: c for r, c in
                _family_capped_by_role(harm_projects, "transitions_2_by_role").items()}
    roles_deg = {r: Counter({int(k): v for k, v in c.items()}) for r, c in
                 _family_capped_by_role(harm_projects, "degree_counts_by_role").items()}

    melodic_grammar = _grammar_from_counts(t1_global, t2_global, deg_global)
    melodic_grammar_by_role = {
        r: _grammar_from_counts(roles_t1[r], roles_t2[r], roles_deg[r])
        for r in (set(roles_t1) | set(roles_deg))
    }

    # humanize-like-me DNA: merge per-project sums, emit mean/std per 16th slot per role.
    # Roles with < 500 usable notes are omitted — humanize then falls back to identity
    # rather than learning a thin contour.
    vel_roles: dict = defaultdict(lambda: {"sum": [0.0] * 16, "sumsq": [0.0] * 16, "n": [0] * 16})
    tim_roles: dict = defaultdict(lambda: {"n": 0, "n_offgrid": 0, "sum_dev": 0.0, "sumsq_dev": 0.0})
    for p in projects:
        for r, v in p.get("velocity_slots_by_role", {}).items():
            acc = vel_roles[r]
            for i in range(16):
                acc["sum"][i] += v["sum"][i]
                acc["sumsq"][i] += v["sumsq"][i]
                acc["n"][i] += v["n"][i]
        for r, t in p.get("timing_dev_by_role", {}).items():
            acc = tim_roles[r]
            for k in ("n", "n_offgrid", "sum_dev", "sumsq_dev"):
                acc[k] += t[k]

    velocity_by_role = {}
    for r, acc in vel_roles.items():
        n_total = sum(acc["n"])
        if n_total < 500:
            continue
        overall_mean = sum(acc["sum"]) / n_total
        slot_mean, slot_std = [], []
        for i in range(16):
            if acc["n"][i]:
                m = acc["sum"][i] / acc["n"][i]
                var = max(0.0, acc["sumsq"][i] / acc["n"][i] - m * m)
                slot_mean.append(round(m, 2))
                slot_std.append(round(var ** 0.5, 2))
            else:   # empty slot: target the overall mean, never 0
                slot_mean.append(round(overall_mean, 2))
                slot_std.append(0.0)
        overall_var = max(0.0, sum(acc["sumsq"]) / n_total - overall_mean ** 2)
        velocity_by_role[r] = {
            "slot_mean": slot_mean, "slot_std": slot_std, "n_notes": n_total,
            "mean": round(overall_mean, 2), "std": round(overall_var ** 0.5, 2),
        }

    timing_by_role = {}
    for r, acc in tim_roles.items():
        if acc["n"] < 500:
            continue
        m = acc["sum_dev"] / acc["n"]
        var = max(0.0, acc["sumsq_dev"] / acc["n"] - m * m)
        timing_by_role[r] = {
            "dev_mean_beats": round(m, 5), "dev_std_beats": round(var ** 0.5, 5),
            "offgrid_frac": round(acc["n_offgrid"] / acc["n"], 4), "n_notes": acc["n"],
        }

    iv_total = sum(intervals.values()) or 1
    melody_shape = {
        "pct_steps": round(100 * sum(v for k, v in intervals.items() if 1 <= abs(k) <= 2) / iv_total, 1),
        "pct_leaps": round(100 * sum(v for k, v in intervals.items() if abs(k) >= 5) / iv_total, 1),
        "pct_down": round(100 * sum(v for k, v in intervals.items() if k < 0) / iv_total, 1),
        "pct_up": round(100 * sum(v for k, v in intervals.items() if k > 0) / iv_total, 1),
    }

    return {
        "n_projects": len(projects),
        "n_harmonic_projects": len(harm_projects),
        "n_drum_only": sum(1 for p in projects if not p["has_harmonic"]),
        "total_harmonic_notes": sum(p["harmonic_notes"] for p in projects),
        "total_drum_notes": sum(p["drum_notes"] for p in projects),
        "key_distribution": keys.most_common(),
        "mode_distribution": modes.most_common(),
        "tonic_distribution": tonics.most_common(),
        "tempo": {"min": min(tempos) if tempos else None, "max": max(tempos) if tempos else None,
                  "mean": round(sum(tempos) / len(tempos), 1) if tempos else None,
                  "common_ranges": _tempo_bins(tempos)},
        "top_melodic_intervals": _interval_summary(intervals),
        "melody_shape": melody_shape,
        "harmonic_groove": hgroove,
        "drum_groove": dgroove,
        "duration_distribution": durations.most_common(),
        "avg_scale_conformity": round(sum(p["scale_conformity"] for p in harm_projects) / len(harm_projects), 3) if harm_projects else 0,
        "avg_polyphony": round(sum(p["avg_polyphony"] for p in harm_projects) / len(harm_projects), 2) if harm_projects else 0,
        "avg_velocity": round(sum(p["velocity_mean"] for p in harm_projects) / len(harm_projects), 1) if harm_projects else 0,
        "pitch_range": [min(p["pitch_min"] for p in harm_projects), max(p["pitch_max"] for p in harm_projects)] if harm_projects else [None, None],
        "pitch_register": [_pct_from_hist(corpus_pitch, 5), _pct_from_hist(corpus_pitch, 95)],
        "pitch_median": _pct_from_hist(corpus_pitch, 50),
        "melodic_grammar": melodic_grammar,
        "melodic_grammar_by_role": melodic_grammar_by_role,
        "velocity_by_role": velocity_by_role,
        "timing_by_role": timing_by_role,
    }


# --- plain-language narrative ----------------------------------------------

def build_narrative(agg):
    """Translate the numbers into plain English — no theory background assumed."""
    s = []

    # Mood / key feel
    md = dict(agg["mode_distribution"])
    minor, major = md.get("minor", 0), md.get("major", 0)
    tot = minor + major or 1
    pmin = minor / tot
    if pmin >= 0.6:
        mood = "You lean strongly toward **minor keys** — the moodier, darker-sounding side of music."
    elif pmin <= 0.4:
        mood = "You lean toward **major keys** — the brighter, more open-sounding side of music."
    else:
        mood = "You split fairly evenly between **minor keys** (moodier, darker) and **major** (brighter), with a slight pull toward minor."
    top_tonic = agg["tonic_distribution"][0][0] if agg["tonic_distribution"] else "C"
    s.append(f"{mood} More than any other, your music tends to call **{top_tonic}** home.")

    # In-key vs chromatic
    sc = agg["avg_scale_conformity"]
    if sc >= 0.9:
        s.append(f"You almost always stay **in key** (~{sc*100:.0f}% of notes fit the scale), so things sound melodic and grounded.")
    elif sc >= 0.8:
        s.append(f"You mostly stay **in key** (~{sc*100:.0f}% of notes), with a regular sprinkle of out-of-key notes for colour and tension.")
    else:
        s.append(f"You use a lot of **out-of-key notes** (only ~{sc*100:.0f}% sit in the scale) — a more chromatic, restless, experimental flavour.")

    # Tempo
    t = agg["tempo"]
    bands = agg["tempo"]["common_ranges"]
    fast = sum(c for r, c in bands if int(r.split("-")[0]) >= 110)
    slow = sum(c for r, c in bands if int(r.split("-")[0]) < 100)
    if fast and slow:
        s.append(f"You work in **two speed zones**: a lot of up-tempo, danceable material (around 120–130 BPM) and a slower, moodier set (roughly 70–95 BPM). Average ~{t['mean']:.0f} BPM.")
    else:
        s.append(f"Your tempos centre around **{t['mean']:.0f} BPM**, ranging {t['min']:.0f}–{t['max']:.0f}.")

    # Melody shape
    ms = agg["melody_shape"]
    direction = "**fall (drift downward)** more than they climb" if ms["pct_down"] > ms["pct_up"] + 3 else \
                "**rise** more than they fall" if ms["pct_up"] > ms["pct_down"] + 3 else "move up and down fairly evenly"
    if ms["pct_steps"] > ms["pct_leaps"]:
        s.append(f"Your melodies move mostly in **small steps** (neighbouring notes) rather than big jumps, and they tend to {direction} — a smooth, singable, gentle melodic feel.")
    else:
        s.append(f"Your melodies favour **bigger leaps** between notes, and they tend to {direction} — a more angular, dramatic melodic feel.")

    # Texture
    ap = agg["avg_polyphony"]
    if ap < 1.5:
        s.append("You mostly write **one note at a time** — melodic lines and sequences rather than thick stacked chords.")
    elif ap < 2.5:
        s.append("You mix **single-note lines with some chords** — a balance of melody and harmony.")
    else:
        s.append("You write in **thick chords** — a dense, harmony-driven texture.")

    # Rhythm feel from duration mix + groove
    dd = dict(agg["duration_distribution"])
    if dd and max(dd, key=dd.get) == "1/16":
        s.append("Rhythmically you're **busy and driving** — lots of fast 16th-note movement, which gives a sequenced, electronic energy.")

    # Drums
    if agg["total_drum_notes"] > 0:
        s.append(f"A big part of your output is **rhythm/percussion** ({agg['total_drum_notes']:,} drum notes across the catalog, separate from the harmony above).")

    return s


# --- output -----------------------------------------------------------------

def _render_groove(groove, label):
    total = sum(groove) or 1
    out = [f"  ({label})"]
    for i, v in enumerate(groove):
        beat = "●" if i % 4 == 0 else "·"
        pct = 100 * v / total
        out.append(f"  {beat} 16th {i+1:>2}: {'█' * int(pct/2)} {pct:.1f}%")
    return out


def _harmony_section():
    """Render the Harmony section from store/harmony.json, or nothing if absent."""
    if not os.path.exists(HARMONY_JSON):
        return []
    with open(HARMONY_JSON) as fh:
        h = json.load(fh)
    lines = ["---", "", "## Harmony — your chord language", ""]
    for n in h.get("narrative", []):
        lines.append(f"- {n}")
    rns = ", ".join(f"{f} ({c})" for f, c in h.get("roman_numerals", [])[:8])
    if rns:
        lines.append(f"- Most-used chords (scale degrees): {rns}")
    progs = "; ".join(f"`{p}` ({c})" for p, c in h.get("progressions", [])[:6])
    if progs:
        lines.append(f"- Top progressions: {progs}")
    lines.append("")
    return lines


def _structure_section():
    """Render the Arrangement section from store/structure.json, or nothing if absent."""
    if not os.path.exists(STRUCTURE_JSON):
        return []
    with open(STRUCTURE_JSON) as fh:
        st = json.load(fh)
    agg = st.get("aggregate", {})
    lines = ["---", "", "## Arrangement — how you build songs", ""]
    for n in st.get("narrative", []):
        lines.append(f"- {n}")
    hist = agg.get("section_length_histogram", {})
    hist_str = ", ".join(f"{k} bars ({hist[k]})" for k in sorted(hist, key=int) if hist.get(k))
    if hist_str:
        lines.append(f"- Section-length histogram: {hist_str}")
    sps = agg.get("sections_per_song", {})
    if sps.get("mean"):
        lines.append(f"- Sections per song: mean **{sps['mean']}**")
    sl = agg.get("song_length", {})
    if sl.get("avg_minutes"):
        lines.append(f"- Avg arranged song: **{sl['avg_minutes']} min** ({sl['avg_bars']} bars)")
    if agg.get("breakdown_pct") is not None:
        lines.append(f"- Breakdown pattern (dip mid-song): **{agg['breakdown_pct']}%** of arranged songs")
    arc = st.get("energy_arc_ascii") or []
    if arc:
        lines.append("")
        lines.append("```")
        lines.extend(arc)
        lines.append("```")
    lines.append("")
    return lines


def _notes_section():
    """Ricardo's hand-written notes — persisted in store/NOTES.md so they SURVIVE
    STYLE.md regeneration (style_profile rewrites STYLE.md; NOTES.md is never touched).
    Created with a template on first run."""
    if not os.path.exists(NOTES_MD):
        with open(NOTES_MD, "w") as fh:
            fh.write(
                "_Describe your music in your own words: the feel you go for, artists\n"
                "you're inspired by, things you always/never do, what a track of yours\n"
                "should sound like. The sidekick reads this first._\n"
                "\n_(This file is YOURS — edit it freely; STYLE.md regeneration never\n"
                "overwrites it.)_\n"
            )
    with open(NOTES_MD) as fh:
        body = fh.read().strip()
    return ["---", "## Notes for the sidekick (write your own — this matters most)", "", body, ""]


def write_style_md(agg, path):
    L = [
        "# Ricardo — Style Profile",
        "",
        "> Auto-generated from your MIDI catalog. **This file is meant to be edited.**",
        "> Fix anything that doesn't ring true, and add your own notes at the bottom —",
        "> your words become part of how the sidekick understands you.",
        "",
        f"Built from **{agg['n_harmonic_projects']} melodic/harmonic projects** "
        f"(+{agg['n_drum_only']} drum-only) · "
        f"**{agg['total_harmonic_notes']:,} melodic notes**, {agg['total_drum_notes']:,} drum notes.",
        "",
        "## In plain words",
        "",
    ]
    for line in build_narrative(agg):
        L.append(f"- {line}")
    L += [
        "",
        "---",
        "## The data behind it",
        "",
        "### Tonality",
        "- Keys (moodier *minor* vs brighter *major*): " + ", ".join(f"{m} ({c})" for m, c in agg["mode_distribution"]),
        "- Most-used keys: " + ", ".join(f"{k} ({c})" for k, c in agg["key_distribution"][:8]),
        "- Home notes (tonics): " + ", ".join(f"{t} ({c})" for t, c in agg["tonic_distribution"][:6]),
        f"- In-key rate: **{agg['avg_scale_conformity']*100:.0f}%**",
        "",
        "### Tempo",
        f"- {agg['tempo']['min']:.0f}–{agg['tempo']['max']:.0f} BPM, mean **{agg['tempo']['mean']:.0f}**",
        "- Common bands: " + ", ".join(f"{r} ({c})" for r, c in agg["tempo"]["common_ranges"]),
        "",
        "### Melody",
        f"- Small steps {agg['melody_shape']['pct_steps']}% vs big leaps {agg['melody_shape']['pct_leaps']}%; "
        f"downward {agg['melody_shape']['pct_down']}% vs upward {agg['melody_shape']['pct_up']}%",
        "- Favourite moves: " + ", ".join(f"{iv['name']} {iv['pct']}%" for iv in agg["top_melodic_intervals"][:6]),
        "",
        "### Groove — where notes land in a 4/4 bar",
        "```",
        *_render_groove(agg["harmonic_groove"], "melodic/harmonic parts"),
        "",
        *_render_groove(agg["drum_groove"], "drums/percussion"),
        "```",
        "",
        "### Texture & dynamics",
        f"- Avg notes per hit (chord density): **{agg['avg_polyphony']}**",
        f"- Avg velocity (loudness): **{agg['avg_velocity']}** / 127",
        f"- Typical register: **{_note_name(agg['pitch_register'][0])}–{_note_name(agg['pitch_register'][1])}** "
        f"(middle 90%), centred near {_note_name(agg['pitch_median'])}",
        "- Note-length mix: " + ", ".join(f"{d} ({c})" for d, c in agg["duration_distribution"]),
        "",
    ]
    L += _harmony_section()
    L += _structure_section()
    L += _notes_section()
    _write_text_atomic(path, "\n".join(L) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    index_path = os.path.join(PROJ_DIR, "_index.json")
    if not os.path.exists(index_path):
        print("No materialized corpus. Run: python3 ingest/materialize.py first.", file=sys.stderr)
        return 1
    with open(index_path) as fh:
        index = json.load(fh)
    index = [r for r in index if not is_blocked_project(r["name"])]
    if args.limit:
        index = index[:args.limit]
    print(f"Analyzing {len(index)} projects from local corpus...")

    analyzed = []
    for i, r in enumerate(index, 1):
        with open(os.path.join(PROJ_DIR, f"{r['id']}.json")) as fh:
            proj = from_dict(json.load(fh))
        res = analyze_project(proj)
        if res:
            analyzed.append(res)
        if i % 40 == 0 or i == len(index):
            print(f"  {i}/{len(index)}")

    agg = aggregate(analyzed)
    out = {
        "meta": build_meta(len(index)),
        "aggregate": agg,
        "projects": analyzed,
    }
    _save_json_atomic(os.path.join(ROOT, "store", "style_profile.json"), out, indent=2)
    write_style_md(agg, os.path.join(ROOT, "store", "STYLE.md"))

    print(f"\n{agg['n_harmonic_projects']} harmonic + {agg['n_drum_only']} drum-only projects.")
    print(f"Harmonic notes: {agg['total_harmonic_notes']:,}  Drum notes: {agg['total_drum_notes']:,}")
    print("  → store/STYLE.md  &  store/style_profile.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
