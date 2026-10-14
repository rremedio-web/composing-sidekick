"""Diagnostic (NOT a unit test): junk-gate + in-key ranker on the real corpus.

Run: .venv/bin/python tools/boringness_report.py

Two questions:
  A. Does is_mechanical() evict the dead rolls? (count flagged per clip_source)
  B. Does alive_score() (surprise gated to in-key notes) finally rank
     session > take_lane, where raw mean_surprise did not?

Reads the real corpus, so it stays separate from the tested boringness module.
Tonic = most-common pitch class; mode = minor if the 3rd is minor, else major
(crude, directional only).
"""
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from boringness import phrase_to_degrees, mean_surprise, is_mechanical, alive_score

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAJOR = {0, 2, 4, 5, 7, 9, 11}
MINOR = {0, 2, 3, 5, 7, 8, 10}


def _tonic_pc(notes):
    counts = Counter(n["pitch"] % 12 for n in notes)
    top = max(counts.values())
    return min(pc for pc, c in counts.items() if c == top)


def _scale_for(degrees):
    """Minor if the minor 3rd (deg 3) outweighs the major 3rd (deg 4)."""
    c = Counter(degrees)
    return MINOR if c.get(3, 0) > c.get(4, 0) else MAJOR


def _summary(name, values):
    if not values:
        print("  %-14s n=0" % name)
        return
    vs = sorted(values)
    n = len(vs)
    print("  %-14s n=%-5d mean=%.2f  median=%.2f  p10=%.2f  p90=%.2f"
          % (name, n, sum(vs) / n, vs[n // 2], vs[int(n * 0.10)], vs[min(n - 1, int(n * 0.90))]))


def main():
    agg = json.load(open(os.path.join(ROOT, "store/style_profile.json")))["aggregate"]
    grammar = agg.get("melodic_grammar_by_role", {}).get("lead") or agg["melodic_grammar"]

    idx = json.load(open(os.path.join(ROOT, "store/phrase_index.json")))
    phrases = idx if isinstance(idx, list) else idx.get("phrases", [])

    raw, alive, alive_clean, mech, total = {}, {}, {}, Counter(), Counter()
    for p in phrases:
        if p.get("role") != "lead":
            continue
        notes = p.get("notes", [])
        if len(notes) < 2:
            continue
        degs = phrase_to_degrees([{"pitch": n["pitch"], "start": n["start"]} for n in notes],
                                 _tonic_pc(notes))
        if len(degs) < 2:
            continue
        src = p.get("clip_source", "?")
        total[src] += 1
        a = alive_score(degs, grammar, _scale_for(degs))
        raw.setdefault(src, []).append(mean_surprise(degs, grammar))
        alive.setdefault(src, []).append(a)
        if is_mechanical(degs, grammar):
            mech[src] += 1
        else:
            alive_clean.setdefault(src, []).append(a)

    order = ["session", "arrangement", "take_lane"]

    print("=== A. junk-gate: is_mechanical() flagged per pool ===")
    for src in order:
        if total[src]:
            print("  %-14s %5d / %-5d  (%.1f%% mechanical)"
                  % (src, mech[src], total[src], 100.0 * mech[src] / total[src]))

    print("\n=== B1. raw mean_surprise (the FAILED ranker, for reference) ===")
    for src in order:
        _summary(src, raw.get(src, []))

    print("\n=== B2. alive_score = in-key-gated surprise (the new ranker) ===")
    for src in order:
        _summary(src, alive.get(src, []))

    print("\n=== B3. alive_score after dropping is_mechanical() junk ===")
    for src in order:
        _summary(src, alive_clean.get(src, []))


if __name__ == "__main__":
    main()
