"""Quarantine pass: flag mechanical 'leads' (rolls/trills) across the index.

Read-only over store/phrase_index.json. Writes a manifest you can eyeball;
it does NOT mutate the index. Undo = delete the manifest.

Run: .venv/bin/python tools/quarantine_rolls.py
"""
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
from boringness import phrase_to_degrees, mechanical_reason
import core
OUT = os.path.join(ROOT, "tools/ideas/lead_roll_quarantine.json")


def _tonic_pc(notes):
    counts = Counter(n["pitch"] % 12 for n in notes)
    top = max(counts.values())
    return min(pc for pc, c in counts.items() if c == top)


def main():
    agg = json.load(open(os.path.join(ROOT, "store/style_profile.json")))["aggregate"]
    grammar = agg.get("melodic_grammar_by_role", {}).get("lead") or agg["melodic_grammar"]
    idx = json.load(open(os.path.join(ROOT, "store/phrase_index.json")))
    phrases = idx if isinstance(idx, list) else idx.get("phrases", [])

    items, scanned = [], 0
    by_source, by_reason = Counter(), Counter()
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
        scanned += 1
        reason = mechanical_reason(degs, grammar)
        if reason is None:
            continue
        src = p.get("clip_source", "?")
        by_source[src] += 1
        by_reason[reason] += 1
        items.append({
            "phrase_id": p.get("id"),
            "project": p.get("project"),
            "clip_source": src,
            "n_notes": len(notes),
            "distinct_degrees": len(set(degs)),
            "reason": reason,
            "pitch_preview": [n["pitch"] for n in notes[:16]],
        })

    manifest = {
        "schema_version": 1,
        "scanned_leads": scanned,
        "quarantined": len(items),
        "by_source": dict(by_source),
        "by_reason": dict(by_reason),
        "items": items,
    }
    core._save_json_atomic(OUT, manifest, indent=1)

    print("scanned %d lead phrases -> quarantined %d (%.1f%%)"
          % (scanned, len(items), 100.0 * len(items) / max(scanned, 1)))
    print("by clip_source:", dict(by_source))
    print("by reason:     ", dict(by_reason))
    print("manifest:", os.path.relpath(OUT, ROOT))
    print("\nsample evictions (confirm these are junk by ear/eye):")
    for reason in ("mono_pitch", "predictable"):
        ex = [it for it in items if it["reason"] == reason][:3]
        for it in ex:
            print("  [%s] %-28s n=%-3d distinct=%d  pitches=%s"
                  % (reason, it["phrase_id"], it["n_notes"],
                     it["distinct_degrees"], it["pitch_preview"][:8]))


if __name__ == "__main__":
    main()
