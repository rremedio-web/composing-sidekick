"""music21 harmonic enrichment — names Ricardo's chords & progressions.

Reads the materialized corpus (store/projects/*.json). For each project it
estimates the key, slides a beat-window to collect the pitch-classes sounding
together (capturing harmony formed by overlapping monophonic parts), then uses
music21 to name each chord and assign a Roman numeral relative to the key.

Aggregates the corpus into: most-used chord qualities, most-used Roman numerals,
and most common chord progressions. Writes store/harmony.json and inserts a
"## Harmony" section into store/STYLE.md (idempotent).

Run AFTER ingest/materialize.py. Uses music21 from the venv:
    .venv/bin/python analyze/harmony.py
    .venv/bin/python analyze/harmony.py --limit 40
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJ_DIR = os.path.join(ROOT, "store", "projects")

sys.path.insert(0, os.path.join(ROOT, "ingest"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_als import clip_source_of, is_blocked_project, is_main_clip_source  # noqa: E402
from style_profile import estimate_key, PITCH_NAMES  # noqa: E402

from music21 import chord as m21chord, roman, key as m21key  # noqa: E402


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


def build_meta(n_projects: int, schema_version: int = 1) -> dict:
    return {
        "schema_version": schema_version,
        "built_at": _iso_utc(),
        "code_rev": _git_rev(),
        "corpus": {"n_projects": int(n_projects)},
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


def harmonic_notes(data: dict):
    # Sidekick output tracks are not his playing (flag, plus the track-name
    # fallback for materialized clips that predate the flag — as in motifs).
    return [n for c in data["clips"]
            if is_main_clip_source(clip_source_of(c)) and not c["is_drum"]
            and not c.get("is_sidekick")
            and not str(c.get("track", "")).strip().upper().startswith("SIDEKICK")
            for n in c["notes"] if n.get("enabled", True)]


def chord_windows(notes, max_span=16):
    """Map each integer beat → set of pitch-classes sounding during that beat."""
    windows = {}
    for n in notes:
        start = n["start"]
        end = start + min(n["duration"], max_span)
        b0, b1 = int(math.floor(start)), int(math.floor(end - 1e-9))
        for b in range(b0, max(b0, b1) + 1):
            windows.setdefault(b, set()).add(n["pitch"] % 12)
    return windows


_CACHE = {}


def name_chord(pcs, tonic, mode):
    """(common name, roman figure) for a pitch-class set in a key. Cached."""
    ckey = (frozenset(pcs), tonic, mode)
    if ckey in _CACHE:
        return _CACHE[ckey]
    try:
        c = m21chord.Chord(sorted(pcs))
        common = c.commonName
    except Exception:
        common = None
    figure = None
    try:
        rn = roman.romanNumeralFromChord(m21chord.Chord(sorted(pcs)), m21key.Key(tonic, mode))
        figure = rn.romanNumeralAlone if rn.romanNumeralAlone else rn.figure
    except Exception:
        figure = None
    _CACHE[ckey] = (common, figure)
    return common, figure


def analyze_project(data: dict):
    notes = harmonic_notes(data)
    if len(notes) < 8:
        return None
    pc_hist = [0.0] * 12
    for n in notes:
        pc_hist[n["pitch"] % 12] += 1
    tonic, mode, _ = estimate_key(pc_hist)

    windows = chord_windows(notes)
    qualities, figures, seq = Counter(), Counter(), []
    for b in sorted(windows):
        pcs = windows[b]
        if len(pcs) < 3:          # need a real chord, not a dyad/single note
            continue
        common, figure = name_chord(pcs, tonic, mode)
        if common:
            qualities[common] += 1
        if figure:
            figures[figure] += 1
            if not seq or seq[-1] != figure:   # collapse repeats
                seq.append(figure)

    progressions = Counter()
    for a, b in zip(seq, seq[1:]):
        progressions[f"{a}→{b}"] += 1
    for a, b, c in zip(seq, seq[1:], seq[2:]):
        progressions[f"{a}→{b}→{c}"] += 1

    return {"key": f"{tonic} {mode}", "qualities": qualities,
            "figures": figures, "progressions": progressions, "n_chords": sum(qualities.values())}


# Plain-language gloss for common Roman-numeral figures.
_FIGURE_GLOSS = {
    "i": "i (minor home)", "I": "I (major home)", "ii": "ii (minor supertonic)",
    "iio": "ii° (diminished)", "II": "II", "iii": "iii", "III": "♭III (relative major)",
    "iv": "iv (minor subdominant)", "IV": "IV (major subdominant)",
    "v": "v (minor dominant)", "V": "V (major dominant)", "vi": "vi", "VI": "♭VI",
    "vii": "vii", "VII": "♭VII", "viio": "vii° (leading-tone)",
}


def _quality_buckets(qualities):
    """Group music21's chord names into readable buckets (triads / 7ths / open)."""
    b = Counter()
    for name, c in qualities.items():
        low = name.lower()
        if "triad" in low and "quartal" not in low and "whole-tone" not in low:
            b["clean triads"] += c
        elif "seventh" in low:
            b["seventh chords"] += c
        else:
            b["open / ambiguous voicings"] += c
    return b


def build_narrative(qualities, figures, progressions):
    s = []
    topf = figures.most_common(6)
    if topf:
        glossed = ", ".join(_FIGURE_GLOSS.get(f, f) for f, _ in topf)
        s.append(f"Your harmony centres on **{glossed}** — the chords your progressions keep returning to.")
    topp = [p for p, _ in progressions.most_common(5)]
    if topp:
        s.append("Progressions you reach for again and again: " + "; ".join(f"`{p}`" for p in topp) + ".")
    # Characterise the overall flavour from the dominant figures.
    fset = {f for f, _ in figures.most_common(4)}
    if {"i", "iv"} <= fset:
        s.append("That **i↔iv** centre of gravity is a *plagal, minor, 'falling-home'* sound — "
                 "moodier and modal rather than bright major-key pop.")
    buckets = _quality_buckets(qualities)
    btot = sum(buckets.values()) or 1
    mix = ", ".join(f"{k} ({100*c/btot:.0f}%)" for k, c in buckets.most_common())
    s.append(f"Voicing texture: {mix}. (Lots of open/ambiguous voicings reflects your mostly "
             "**single-line, melodic** writing — harmony implied by overlapping parts more than stacked chords.)")
    return s


def insert_into_style_md(path, lines):
    """Insert/replace a '## Harmony' block just before the 'Notes for the sidekick' section."""
    block = ["## Harmony — your chord language", ""] + [f"- {l}" for l in lines] + [""]
    if not os.path.exists(path):
        return
    with open(path) as fh:
        text = fh.read()
    # strip any prior Harmony block
    marker_notes = "## Notes for the sidekick"
    if "## Harmony — your chord language" in text:
        head = text.split("## Harmony — your chord language")[0]
        tail = text[text.index(marker_notes):] if marker_notes in text else ""
        text = head + "\n".join(block) + "\n---\n" + tail
    elif marker_notes in text:
        head, tail = text.split(marker_notes, 1)
        text = head + "\n".join(block) + "\n---\n" + marker_notes + tail
    else:
        text = text + "\n" + "\n".join(block)
    _write_text_atomic(path, text)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    with open(os.path.join(PROJ_DIR, "_index.json")) as fh:
        index = json.load(fh)
    index = [r for r in index if r["harmonic_notes"] > 0
             and not is_blocked_project(r["name"])]
    if args.limit:
        index = index[:args.limit]
    print(f"Harmonic analysis over {len(index)} projects (music21)...")

    qualities, figures, progressions, analyzed = Counter(), Counter(), Counter(), 0
    for i, r in enumerate(index, 1):
        with open(os.path.join(PROJ_DIR, f"{r['id']}.json")) as fh:
            data = json.load(fh)
        res = analyze_project(data)
        if res:
            qualities.update(res["qualities"])
            figures.update(res["figures"])
            progressions.update(res["progressions"])
            analyzed += 1
        if i % 25 == 0 or i == len(index):
            print(f"  {i}/{len(index)}  (cache={len(_CACHE)})")

    out = {
        "meta": build_meta(len(index)),
        "n_projects": analyzed,
        "total_chords": sum(qualities.values()),
        "chord_qualities": qualities.most_common(12),
        "roman_numerals": figures.most_common(15),
        "progressions": progressions.most_common(20),
        "narrative": build_narrative(qualities, figures, progressions),
    }
    _save_json_atomic(os.path.join(ROOT, "store", "harmony.json"), out, indent=2)

    print(f"\nAnalyzed {analyzed} projects, {out['total_chords']:,} chord events.")
    print("  (STYLE.md Harmony section is now rendered by style_profile.py from harmony.json)")
    print("Top chord qualities:", qualities.most_common(5))
    print("Top Roman numerals :", figures.most_common(8))
    print("Top progressions   :", progressions.most_common(6))
    print("  → store/harmony.json (consumed by style_profile.py)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
