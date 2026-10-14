"""Arrangement intelligence — learns how Ricardo builds songs from timeline clips.

Reads the materialized corpus (store/projects/*.json). Detects which projects
are arranged (vs loop/Session sketches), builds per-bar track-density curves
and sections, then aggregates arrangement patterns across the corpus.

Writes store/structure.json. The Arrangement section of STYLE.md is rendered by
style_profile.py (single owner — this module does NOT touch STYLE.md).

Run AFTER ingest/materialize.py:
    python3 analyze/structure.py
    python3 analyze/structure.py --demo
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
OUT_PATH = os.path.join(ROOT, "store", "structure.json")

sys.path.insert(0, os.path.join(ROOT, "ingest"))
from parse_als import clip_source_of, is_blocked_project, is_timeline_clip_source  # noqa: E402

BEATS_PER_BAR = 4
SECTION_BUCKETS = (1, 2, 4, 8, 16, 32, 64)
ENERGY_POINTS = 16
BREAKDOWN_DIP_RATIO = 0.55


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


def timeline_clips(data: dict) -> list[dict]:
    """Clips placed on the arrangement timeline (start/end in beats)."""
    out = []
    for c in data.get("clips", []):
        if not is_timeline_clip_source(clip_source_of(c)):
            continue
        start, end = c.get("start"), c.get("end")
        if start is not None and end is not None and end > start:
            out.append(c)
    return out


def is_arranged(data: dict) -> bool:
    """True when the project looks like a timeline arrangement, not a loop sketch."""
    clips = timeline_clips(data)
    if len(clips) < 4:
        return False
    starts = {c["start"] for c in clips}
    if len(starts) < 3:
        return False
    span = max(c["end"] for c in clips) - min(c["start"] for c in clips)
    return span >= 32


def _n_bars(clips: list[dict]) -> int:
    if not clips:
        return 0
    max_end = max(c["end"] for c in clips)
    return max(1, int(math.ceil(max_end / BEATS_PER_BAR)))


def trim_density_curve(curve: list[int], max_gap: int = 32) -> tuple[list[int], int]:
    """Drop leading zeros and truncate before long silent gaps.

    Returns (trimmed curve, first_bar_index) so callers slice bar-track-sets from
    the same window — sections and density always align.
    """
    if not curve:
        return [], 0

    start = 0
    while start < len(curve) and curve[start] == 0:
        start += 1
    window = curve[start:]
    if not window:
        return [], start

    end = len(window)
    gap = 0
    for i, d in enumerate(window):
        if d == 0:
            gap += 1
            if gap >= max_gap:
                end = i - gap + 1
                break
        else:
            gap = 0
    while end > 0 and window[end - 1] == 0:
        end -= 1
    return window[:end], start


def bar_density_curve(clips: list[dict]) -> list[int]:
    """Per-bar count of distinct active track names."""
    n = _n_bars(clips)
    curve = []
    for b in range(n):
        bar_start = b * BEATS_PER_BAR
        bar_end = (b + 1) * BEATS_PER_BAR
        active = {c["track"] for c in clips
                  if c["start"] < bar_end and c["end"] > bar_start}
        curve.append(len(active))
    return curve


def _bar_track_sets(clips: list[dict], n_bars: int | None = None) -> list[frozenset[str]]:
    n = n_bars if n_bars is not None else _n_bars(clips)
    sets = []
    for b in range(n):
        bar_start = b * BEATS_PER_BAR
        bar_end = (b + 1) * BEATS_PER_BAR
        active = {c["track"] for c in clips
                  if c["start"] < bar_end and c["end"] > bar_start}
        sets.append(frozenset(active))
    return sets


def _merge_short_fragments(sections: list[dict]) -> list[dict]:
    """Merge <2-bar sections into the previous section (or next if first)."""
    if not sections:
        return sections
    merged: list[dict] = []
    for sec in sections:
        if sec["length_bars"] < 2 and merged:
            merged[-1]["length_bars"] += sec["length_bars"]
        elif sec["length_bars"] < 2 and not merged:
            merged.append(dict(sec))
        else:
            merged.append(dict(sec))
    # First section still a 1-bar orphan → absorb into the next.
    if len(merged) >= 2 and merged[0]["length_bars"] < 2:
        merged[1]["length_bars"] += merged[0]["length_bars"]
        merged.pop(0)
    return merged


def sections_from_bar_sets(bar_sets: list[frozenset[str]]) -> list[dict]:
    """Consecutive bars with the same active-track set → sections."""
    if not bar_sets:
        return []

    raw: list[dict] = []
    current = bar_sets[0]
    length = 1
    for i in range(1, len(bar_sets)):
        if bar_sets[i] == current:
            length += 1
        else:
            raw.append({
                "length_bars": length,
                "density": len(current),
                "tracks": sorted(current),
            })
            current = bar_sets[i]
            length = 1
    raw.append({
        "length_bars": length,
        "density": len(current),
        "tracks": sorted(current),
    })
    return _merge_short_fragments(raw)


def sections_from_clips(
    clips: list[dict],
    density: list[int] | None = None,
    bar_offset: int = 0,
) -> list[dict]:
    """Build sections from a density-aligned bar window."""
    if density is None:
        density, bar_offset = trim_density_curve(bar_density_curve(clips))
    n = len(density)
    if n == 0:
        return []
    raw_sets = _bar_track_sets(clips, n_bars=bar_offset + n)
    bar_sets = raw_sets[bar_offset:bar_offset + n]
    return sections_from_bar_sets(bar_sets)


def bucket_section_length(bars: int) -> int:
    """Bucket a section length into {1,2,4,8,16,32,64}."""
    for b in SECTION_BUCKETS:
        if bars <= b:
            return b
    return 64


def resample(values: list[float], n: int = ENERGY_POINTS) -> list[float]:
    """Linear resample to n points."""
    if not values:
        return [0.0] * n
    if len(values) == 1:
        return [float(values[0])] * n
    out = []
    for i in range(n):
        pos = i * (len(values) - 1) / (n - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, len(values) - 1)
        frac = pos - lo
        out.append(values[lo] * (1 - frac) + values[hi] * frac)
    return out


def normalize_to_peak(values: list[float]) -> list[float]:
    peak = max(values) if values else 0.0
    if peak <= 0:
        return [0.0] * len(values)
    return [v / peak for v in values]


def has_breakdown(density: list[int]) -> bool:
    """Density dips to ≤55% of peak after the peak, then rises again."""
    if len(density) < 4:
        return False
    peak = max(density)
    if peak <= 0:
        return False
    peak_idx = density.index(peak)
    after = density[peak_idx + 1:]
    if len(after) < 2:
        return False
    min_val = min(after)
    if min_val > BREAKDOWN_DIP_RATIO * peak:
        return False
    min_rel = peak_idx + 1 + after.index(min_val)
    tail = density[min_rel + 1:]
    if not tail:
        return False
    return max(tail) > min_val * 1.1


def analyze_project(data: dict, project_id: str, name: str) -> dict:
    clips = timeline_clips(data)
    arranged = is_arranged(data)
    tempo = data.get("tempo") or 120.0
    raw_density = bar_density_curve(clips) if arranged else []
    density, bar_offset = trim_density_curve(raw_density) if arranged else ([], 0)
    n_bars = len(density)
    sections = sections_from_clips(clips, density, bar_offset) if arranged else []

    return {
        "id": project_id,
        "name": name,
        "arranged": arranged,
        "tempo": tempo,
        "n_timeline_clips": len(clips),
        "n_bars": n_bars,
        "bar_offset": bar_offset,
        "density_curve": density,
        "sections": sections,
        "minutes": round(n_bars * BEATS_PER_BAR / tempo, 2) if arranged and tempo else None,
    }


def aggregate(projects: list[dict]) -> dict:
    arranged = [p for p in projects if p["arranged"]]
    loop = [p for p in projects if not p["arranged"]]

    length_hist = Counter()
    sections_per_song = []
    total_bars = 0
    breakdown_count = 0
    energy_samples: list[list[float]] = []
    start_densities: list[float] = []
    peak_densities: list[float] = []

    for p in arranged:
        for sec in p["sections"]:
            length_hist[bucket_section_length(sec["length_bars"])] += 1
        sections_per_song.append(len(p["sections"]))
        total_bars += p["n_bars"]
        if has_breakdown(p["density_curve"]):
            breakdown_count += 1
        norm = normalize_to_peak([float(d) for d in p["density_curve"]])
        energy_samples.append(resample(norm))
        if p["density_curve"]:
            start_densities.append(p["density_curve"][0] / max(p["density_curve"]))
            peak_densities.append(1.0)

    n_arr = len(arranged)
    avg_energy = []
    if energy_samples:
        for i in range(ENERGY_POINTS):
            avg_energy.append(round(
                sum(s[i] for s in energy_samples) / len(energy_samples), 3))

    tempos = [p["tempo"] for p in arranged if p["tempo"]]
    avg_bars = total_bars / n_arr if n_arr else 0
    per_song_minutes = [p["minutes"] for p in arranged if p["minutes"] is not None]
    avg_minutes = sum(per_song_minutes) / len(per_song_minutes) if per_song_minutes else 0

    sec_dist = Counter(sections_per_song)

    return {
        "section_length_histogram": {str(k): length_hist.get(k, 0) for k in SECTION_BUCKETS},
        "sections_per_song": {
            "mean": round(sum(sections_per_song) / n_arr, 1) if n_arr else 0,
            "distribution": sorted(sec_dist.items()),
        },
        "song_length": {
            "avg_bars": round(avg_bars, 1),
            "avg_minutes": round(avg_minutes, 2),
            "total_bars": total_bars,
        },
        "energy_arc": {
            "points": avg_energy,
            "avg_start_density": round(sum(start_densities) / len(start_densities), 3)
            if start_densities else 0,
            "avg_peak_density_normalized": round(sum(peak_densities) / len(peak_densities), 3)
            if peak_densities else 0,
        },
        "breakdown_pct": round(100 * breakdown_count / n_arr, 1) if n_arr else 0,
        "n_breakdown_songs": breakdown_count,
    }


def render_energy_arc_ascii(points: list[float], width: int = 48, height: int = 8) -> list[str]:
    """ASCII sparkline of the energy arc (0–1 normalized)."""
    if not points:
        return ["  (no data)"]
    lines = [f"  Energy arc ({len(points)}-point avg, normalized):"]
    for row in range(height, 0, -1):
        threshold = row / height
        chars = []
        for v in points:
            chars.append("█" if v >= threshold else " ")
        lines.append(f"  {row:>2} │{''.join(chars).rstrip()}")
    labels = "     └" + "─" * width
    tick = "      start" + " " * (width - 18) + "end"
    lines.append(labels[:width + 8])
    lines.append(tick[:width + 14])
    return lines


def format_section_map(project: dict) -> list[str]:
    """Human-readable section map for one project."""
    lines = [
        f"  {project['name']} ({project['id']})",
        f"    {project['n_bars']} bars · {project['n_timeline_clips']} timeline clips · "
        f"{len(project['sections'])} sections · {project['minutes']} min @ {project['tempo']} BPM",
    ]
    bar_cursor = 0
    offset = project.get("bar_offset", 0)
    for i, sec in enumerate(project["sections"], 1):
        b0 = offset + bar_cursor
        b1 = offset + bar_cursor + sec["length_bars"]
        n_tracks = len(sec["tracks"])
        lines.append(
            f"    §{i:>2}  bars {b0:>4}–{b1:<4}  ({sec['length_bars']:>2} bars)  "
            f"density={sec['density']}  tracks={n_tracks}"
        )
        bar_cursor += sec["length_bars"]
    return lines


def build_narrative(agg: dict, n_arranged: int, n_loop: int) -> list[str]:
    s = []
    s.append(f"**{n_arranged}** of your projects read as **arranged songs** on the timeline; "
             f"**{n_loop}** look like loops or Session sketches (few clips or short span).")

    hist = agg["section_length_histogram"]
    top_lens = sorted(((int(k), v) for k, v in hist.items() if v), key=lambda x: -x[1])[:3]
    if top_lens:
        lens_str = ", ".join(f"**{b}**-bar sections ({c}×)" for b, c in top_lens)
        s.append(f"Your sections most often land on {lens_str} — typical pop/EDM block sizes.")

    mean_sec = agg["sections_per_song"]["mean"]
    avg_min = agg["song_length"]["avg_minutes"]
    s.append(f"Arranged songs average **~{mean_sec:.0f} sections** over **~{avg_min:.1f} minutes** "
             f"({agg['song_length']['avg_bars']:.0f} bars).")

    bp = agg["breakdown_pct"]
    if bp >= 40:
        s.append(f"**{bp:.0f}%** of arranged songs show a **breakdown** pattern — density drops "
                 f"to ~half the peak mid-song, then builds back up.")
    elif bp >= 15:
        s.append(f"About **{bp:.0f}%** of songs dip into a quieter breakdown section before "
                 f"building energy again.")
    else:
        s.append(f"Breakdowns are uncommon ({bp:.0f}% of songs) — you tend to **sustain or "
                 f"layer up** rather than strip back mid-track.")

    arc = agg["energy_arc"]["points"]
    if arc:
        start, mid, end = arc[0], arc[len(arc) // 2], arc[-1]
        if end > start + 0.15:
            s.append("Typical **energy arc**: starts sparse, **builds toward the end** — "
                     "a crescendo arrangement habit.")
        elif start > end + 0.15:
            s.append("Typical **energy arc**: **front-loaded** — dense opening, thinning out later.")
        else:
            s.append("Typical **energy arc**: **plateau-ish** — track count stays fairly steady "
                     "once the arrangement gets going.")

    return s


# Hand-known arranged project for ground-truth verification (content not at bar 0).
GT_KNOWN = {
    "m83v2__5e166e05": {
        "bar_offset": 56,
        "n_bars": 37,
        "min_peak_density": 2,
    },
}


def _misaligned_sections(project: dict) -> bool:
    """True when trim/section bug leaves real content as all-empty sections."""
    sections = project["sections"]
    curve = project["density_curve"]
    if not sections or not curve:
        return False
    peak = max(curve)
    if peak <= 0:
        return False
    if all(s["density"] == 0 for s in sections):
        return True
    if len(sections) == 1 and sections[0]["density"] == 0:
        return True
    return False


def ground_truth_check(project: dict) -> list[str]:
    """Sanity-check an arranged project — asserts that CAN fail if misaligned."""
    lines = ["Ground-truth check:"]
    peak = max(project["density_curve"]) if project["density_curve"] else 0
    zero_sections = [s for s in project["sections"] if s["density"] == 0]
    lines.append(f"  Peak bar density: {peak}")
    lines.append(f"  Zero-density sections: {len(zero_sections)}")
    aligned = not _misaligned_sections(project)
    lines.append(f"  Trim/section alignment: {'PASS' if aligned else 'FAIL'}")

    curve_matches = (
        len(project["density_curve"]) == project["n_bars"]
        and all(s["density"] == len(s["tracks"]) for s in project["sections"])
    )
    lines.append(f"  Section density matches track count: {'PASS' if curve_matches else 'FAIL'}")

    known = GT_KNOWN.get(project["id"])
    if known:
        off_ok = project.get("bar_offset") == known["bar_offset"]
        bars_ok = project["n_bars"] == known["n_bars"]
        peak_ok = peak >= known["min_peak_density"]
        lines.append(f"  Known project {project['name']}: bar_offset={project.get('bar_offset')} "
                     f"(expect {known['bar_offset']}) → {'PASS' if off_ok else 'FAIL'}")
        lines.append(f"  Known n_bars={project['n_bars']} (expect {known['n_bars']}) → "
                     f"{'PASS' if bars_ok else 'FAIL'}")
        lines.append(f"  Known peak density ≥{known['min_peak_density']} → "
                     f"{'PASS' if peak_ok else 'FAIL'}")

    lengths = [s["length_bars"] for s in project["sections"]]
    lines.append(f"  Section lengths: {lengths}")
    all_ok = aligned and curve_matches
    if known:
        all_ok = all_ok and off_ok and bars_ok and peak_ok
    lines.append(f"  Overall: {'PASS' if all_ok else 'FAIL'}")
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--demo", action="store_true",
                    help="Print example section maps + ASCII energy arc")
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
    print(f"Arrangement analysis over {len(index)} projects (local corpus)...")

    projects = []
    for i, r in enumerate(index, 1):
        with open(os.path.join(PROJ_DIR, f"{r['id']}.json")) as fh:
            data = json.load(fh)
        projects.append(analyze_project(data, r["id"], r.get("name", r["id"])))
        if i % 50 == 0 or i == len(index):
            print(f"  {i}/{len(index)}")

    n_arranged = sum(1 for p in projects if p["arranged"])
    n_loop = len(projects) - n_arranged
    agg = aggregate(projects)

    # Strip per-bar curves from JSON output (large); keep section summaries per arranged project.
    arranged_summaries = []
    for p in projects:
        if not p["arranged"]:
            continue
        arranged_summaries.append({
            "id": p["id"],
            "name": p["name"],
            "n_bars": p["n_bars"],
            "n_sections": len(p["sections"]),
            "minutes": p["minutes"],
            "tempo": p["tempo"],
            "section_lengths": [s["length_bars"] for s in p["sections"]],
            "has_breakdown": has_breakdown(p["density_curve"]),
        })

    narrative = build_narrative(agg, n_arranged, n_loop)
    arc_ascii = render_energy_arc_ascii(agg["energy_arc"]["points"])

    out = {
        "meta": build_meta(len(index)),
        "n_projects": len(projects),
        "n_arranged": n_arranged,
        "n_loop": n_loop,
        "aggregate": agg,
        "narrative": narrative,
        "energy_arc_ascii": arc_ascii,
        "arranged_projects": arranged_summaries,
    }
    _save_json_atomic(OUT_PATH, out, indent=2)

    print(f"\n{n_arranged} arranged · {n_loop} loop/sketch · {len(projects)} total")
    print("Section-length histogram:", agg["section_length_histogram"])
    print(f"Sections/song mean: {agg['sections_per_song']['mean']}")
    print(f"Avg song length: {agg['song_length']['avg_minutes']} min "
          f"({agg['song_length']['avg_bars']} bars)")
    print(f"Breakdown pattern: {agg['breakdown_pct']}% ({agg['n_breakdown_songs']} songs)")
    print("\n".join(arc_ascii))
    print("  → store/structure.json (consumed by style_profile.py)")

    # Ground-truth: hand-known offset project + one auto-picked mid-sized song.
    arranged = [p for p in projects if p["arranged"]]
    gt_ids = ["m83v2__5e166e05"]
    gt_projects = [p for p in arranged if p["id"] in gt_ids]
    arranged.sort(key=lambda p: (len(p["sections"]), -p["n_bars"]))
    auto = next((p for p in arranged if 8 <= len(p["sections"]) <= 24
                 and 64 <= p["n_bars"] <= 512), None)
    if auto and auto["id"] not in gt_ids:
        gt_projects.append(auto)
    for gt in gt_projects:
        print()
        print("\n".join(ground_truth_check(gt)))
        print("\n".join(format_section_map(gt)))

    if args.demo:
        print("\n--- demo: 3 example arranged projects ---")
        demo_picks = sorted(arranged, key=lambda p: -len(p["sections"]))[:3]
        for p in demo_picks:
            print()
            print("\n".join(format_section_map(p)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
