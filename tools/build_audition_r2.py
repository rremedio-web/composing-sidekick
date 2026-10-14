"""Build solo-audition ROUND 2 — picked BY his ear's data, not by scorer spread.

Round 1 was a stratified alive_score spread (a scorer experiment — half the deck
was expected to bore him; it did, and the scorer landed AUC 0.15, inverted).
Round 2 optimizes for GOLD DENSITY instead:
  8 = nearest cosine neighbors of his 2 golds (4 each, max 2 per project),
  3 = taste-model top picks (model now full-weight, exam acc 0.902),
  1 = wildcard from the take-lane pool (exploration never dies).
Excludes round-1 phrases, the golds themselves, junk, blocklisted projects.
Overwrites rank_audition_manifest.json (r1 archived as rank_audition_r1_rated.json),
re-exports audition_NN.mid, re-locks alive scores so --score can run again on r2.

Run: .venv/bin/python tools/build_audition_r2.py
"""
import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
sys.path.insert(0, os.path.join(ROOT, "ingest"))
import core  # noqa: E402
from boringness import phrase_to_degrees, alive_score, mechanical_reason  # noqa: E402
from parse_als import is_blocked_project  # noqa: E402

MANIFEST = os.path.join(ROOT, "tools/ideas/rank_audition_manifest.json")
SCORES = os.path.join(ROOT, "tools/ideas/rank_audition_scores.json")
R1 = os.path.join(ROOT, "tools/ideas/rank_audition_r1_rated.json")
N_NEIGHBORS_PER_GOLD = 4
N_TASTE_TOP = 3
N_WILDCARD = 1


def _tonic_pc(notes):
    from collections import Counter
    counts = Counter(n["pitch"] % 12 for n in notes)
    top = max(counts.values())
    return min(pc for pc, c in counts.items() if c == top)


def main():
    agg = json.load(open(os.path.join(ROOT, "store/style_profile.json")))["aggregate"]
    grammar = agg.get("melodic_grammar_by_role", {}).get("lead") or agg["melodic_grammar"]
    r1_ids = {it["phrase_id"] for it in json.load(open(R1))}
    golds = core.get_gold_phrases()
    assert golds, "no golds — round 2 selection needs the r1 golds ingested first"
    gold_ids = {g["phrase_id"] for g in golds}
    phrases = core._phrases()
    by_id = {p["id"]: p for p in phrases}

    # Ableton LESSON sets that slipped past the blocklist (found by r2 selection —
    # round-3 blocklist candidates, his call; excluded from auditions either way)
    suspects = {"Audio Effects", "Controlling Live"}

    def eligible(p):
        if p.get("role") != "lead" or len(p.get("notes", [])) < 4:
            return False
        if p["id"] in r1_ids or p["id"] in gold_ids:
            return False
        if is_blocked_project(p.get("project", "")) or p.get("project") in suspects:
            return False
        degs = phrase_to_degrees(
            [{"pitch": n["pitch"], "start": n["start"]} for n in p["notes"]],
            _tonic_pc(p["notes"]))
        return len(degs) >= 4 and mechanical_reason(degs, grammar) is None

    candidates = [p for p in phrases if eligible(p)]
    picked, picked_ids = [], set()

    def take(p, why):
        if p["id"] not in picked_ids:
            picked.append((p, why))
            picked_ids.add(p["id"])

    # 1) gold neighborhoods: cosine on the built phrase vectors. RHYME, don't clone:
    # sibling project versions carry the identical phrase at cos 1.0 — auditioning
    # those re-plays the gold and teaches nothing. Cap sim at 0.98 vs the gold AND
    # vs everything already picked, one per project.
    MAX_SIM = 0.98
    for g in sorted(golds, key=lambda r: r["phrase_id"]):
        gvec = by_id[g["phrase_id"]]["vec"]
        sims = sorted(((core._l2_cosine(gvec, p["vec"]), p) for p in candidates
                       if p["id"] not in picked_ids),
                      key=lambda sp: -sp[0])
        per_proj, taken = {}, 0
        for sim, p in sims:
            if taken >= N_NEIGHBORS_PER_GOLD:
                break
            if sim > MAX_SIM or per_proj.get(p["project"], 0) >= 1:
                continue
            if any(core._l2_cosine(p["vec"], q["vec"]) > MAX_SIM for q, _ in picked):
                continue
            take(p, f"neighbor_of:{g['phrase_id']} (cos {sim:.2f})")
            per_proj[p["project"]] = per_proj.get(p["project"], 0) + 1
            taken += 1

    # 2) taste-model top picks (distinct projects)
    scored = sorted(((core._taste_score(p["notes"], role="lead"), p) for p in candidates
                     if p["id"] not in picked_ids), key=lambda sp: -sp[0])
    used_proj = set()
    for ts, p in scored:
        if sum(1 for _, w in picked if w.startswith("taste_top")) >= N_TASTE_TOP:
            break
        if p["project"] in used_proj:
            continue
        take(p, f"taste_top ({ts:+.2f})")
        used_proj.add(p["project"])

    # 3) wildcard from the take-lane pool
    lanes = [p for p in candidates
             if p.get("clip_source") == "take_lane" and p["id"] not in picked_ids]
    if lanes:
        take(random.Random(7).choice(lanes), "wildcard_take_lane")

    rng = random.Random(11)
    rng.shuffle(picked)
    manifest, locked = [], {}
    for i, (p, why) in enumerate(picked[:12], 1):
        res = core.export_midi(p["notes"], "audition_%02d.mid" % i,
                               tempo=float(p.get("tempo") or 120.0))
        degs = phrase_to_degrees(
            [{"pitch": n["pitch"], "start": n["start"]} for n in p["notes"]],
            _tonic_pc(p["notes"]))
        scale = sorted({d % 12 for d in degs})
        locked[p["id"]] = alive_score(degs, grammar, scale)
        manifest.append({
            "blind_id": i, "phrase_id": p["id"],
            "midi_file": res.get("path") if isinstance(res, dict) else None,
            "n_notes": len(p["notes"]),
            "pitch_preview": [n["pitch"] for n in p["notes"][:16]],
            "picked_by": why, "round": 2, "rating": "",
        })
    import core
    core._save_json_atomic(MANIFEST, manifest, indent=1)
    core._save_json_atomic(SCORES, locked, indent=1)
    print(f"round 2: {len(manifest)} leads (from {len(candidates)} eligible)")
    for it in manifest:
        print(f"  {it['blind_id']:2d}  {it['phrase_id']:34s} <- {it['picked_by']}")


if __name__ == "__main__":
    main()
