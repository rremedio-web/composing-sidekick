"""Read-only motif scan — does Ricardo have a recurring melodic-cell vocabulary?

Reduces every pitched ('harmonic') phrase in the index to its top-voice melody,
converts pitches to SCALE DEGREES relative to that project's tonic, and counts the
most frequent 3- and 4-note cells across the corpus. Also counts key-independent
INTERVAL shapes as a cross-check. The headline number is CONCENTRATION: what share
of all 4-note cells the top 20 cover — high = a real motif vocabulary (motif
injection is viable), low/flat = diffuse (it won't help).

Read-only: reads store/ only, writes nothing. Run: python3 analyze/motif_scan.py
"""
from __future__ import annotations

import collections
import json
import os

STORE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "store")

_PC = {"C": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3, "E": 4, "FB": 4,
       "F": 5, "E#": 5, "F#": 6, "GB": 6, "G": 7, "G#": 8, "AB": 8, "A": 9,
       "A#": 10, "BB": 10, "B": 11, "CB": 11}
_DEG = {0: "1", 1: "b2", 2: "2", 3: "b3", 4: "3", 5: "4", 6: "b5",
        7: "5", 8: "b6", 9: "6", 10: "b7", 11: "7"}
LEAD_MIN_MEDIAN = 55  # ~G3: bias toward lead/melody range, not sub-bass


def tonic_pc(key: str) -> int | None:
    name = key.rsplit(" ", 1)[0].strip().upper()
    return _PC.get(name)


def skyline(notes: list[dict]) -> list[int]:
    """Top-voice pitch per onset (notes grouped to the nearest 16th), in time order."""
    by_onset: dict[float, int] = {}
    for n in notes:
        start = float(n.get("start_beats", n.get("start", 0.0)))
        slot = round(start * 4) / 4.0
        pitch = int(n["pitch"])
        if slot not in by_onset or pitch > by_onset[slot]:
            by_onset[slot] = pitch
    return [by_onset[s] for s in sorted(by_onset)]


def ngrams(seq: list, n: int):
    for i in range(len(seq) - n + 1):
        yield tuple(seq[i:i + n])


def main() -> None:
    idx = json.load(open(os.path.join(STORE, "phrase_index.json")))
    phrases = idx["phrases"] if isinstance(idx, dict) else idx
    sp = json.load(open(os.path.join(STORE, "style_profile.json")))
    key_of = {p["name"]: p["key"] for p in sp.get("projects", [])
              if p.get("has_harmonic") and p.get("key")}

    deg_counts = {3: collections.Counter(), 4: collections.Counter()}
    deg_support = {3: collections.defaultdict(set), 4: collections.defaultdict(set)}
    iv_counts = {3: collections.Counter(), 4: collections.Counter()}
    iv_support = {3: collections.defaultdict(set), 4: collections.defaultdict(set)}
    used = no_key = short = static = 0

    for ph in phrases:
        if ph.get("kind") != "harmonic":
            continue
        key = key_of.get(ph.get("project"))
        if not key:
            no_key += 1
            continue
        tpc = tonic_pc(key)
        if tpc is None:
            no_key += 1
            continue
        line = skyline(ph.get("notes", []))
        if len(line) < 4:
            short += 1
            continue
        median = sorted(line)[len(line) // 2]
        if median < LEAD_MIN_MEDIAN:   # focus on lead/melodic range
            continue
        intervals = [b - a for a, b in zip(line, line[1:])]
        moving = sum(1 for d in intervals if d != 0)
        # melodic filter: kill pads/drones (need real motion + variety)
        if len(set(line)) < 4 or moving / len(intervals) < 0.5:
            static += 1
            continue
        used += 1
        proj = ph.get("project")
        degrees = [(p - tpc) % 12 for p in line]
        for n in (3, 4):
            for g in ngrams(degrees, n):
                deg_counts[n][g] += 1
                deg_support[n][g].add(proj)
            for g in ngrams(intervals, n):
                iv_counts[n][g] += 1
                iv_support[n][g].add(proj)

    def label_deg(g):
        return "-".join(_DEG[d] for d in g)

    def label_iv(g):
        return " ".join(f"{'+' if d > 0 else ''}{d}" for d in g)

    print(f"phrases used (melodic, lead-range): {used} | skipped: "
          f"no-key {no_key}, too-short {short}, static/pad {static}")

    for n in (4, 3):
        tot = sum(deg_counts[n].values())
        distinct = len(deg_counts[n])
        cover = sum(c for _, c in deg_counts[n].most_common(20)) / tot * 100 if tot else 0
        print(f"\n===== SCALE-DEGREE {n}-note cells — RAW COUNT "
              f"(distinct={distinct}, total={tot}, top20 covers {cover:.1f}%) =====")
        for g, c in deg_counts[n].most_common(8):
            print(f"  {label_deg(g):<18} {c:>5}x  {c/tot*100:4.1f}%  "
                  f"in {len(deg_support[n][g]):>3} projects")
        by_support = sorted(
            (g for g, c in deg_counts[n].items() if c >= 20),
            key=lambda g: (len(deg_support[n][g]), deg_counts[n][g]), reverse=True)
        print(f"  -- ranked by CROSS-SONG SUPPORT (loop-proof signature) --")
        for g in by_support[:10]:
            print(f"  {label_deg(g):<18} in {len(deg_support[n][g]):>3} songs "
                  f"({deg_counts[n][g]}x)")

    n = 3
    by_support = sorted(
        (g for g, c in iv_counts[n].items() if c >= 20),
        key=lambda g: (len(iv_support[n][g]), iv_counts[n][g]), reverse=True)
    print(f"\n===== INTERVAL {n}-step shapes — CROSS-SONG SUPPORT (key-independent) =====")
    for g in by_support[:12]:
        print(f"  {label_iv(g):<14} in {len(iv_support[n][g]):>3} songs "
              f"({iv_counts[n][g]}x)")


if __name__ == "__main__":
    main()
