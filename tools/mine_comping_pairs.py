"""Mine historical comping preferences from the materialized corpus.

Every arrangement (main-lane) clip that overlaps take-lane clips on the same track
records a judgment Ricardo already made: the main content beat those takes. This
emits winner-vs-loser pairs as a REVIEWABLE manifest — it does NOT touch the taste
model, ratings, or any store artifact. Undo = delete the manifest.

Guards learned from the .als probe (2026-07-02):
- a take identical to the main is the PROMOTED take, not a loser → dropped, its
  lane-mates remain losers;
- pairs need >= 50% overlap of the shorter clip;
- role is stamped per winner clip so lead pairs can be validated separately
  (the taste model is lead-scoped; nothing here trains anything yet).

Run: python3 tools/mine_comping_pairs.py
"""
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "ingest"))
sys.path.insert(0, os.path.join(ROOT, "analyze"))
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
from parse_als import from_dict  # noqa: E402
from roles import clip_role  # noqa: E402
import core  # noqa: E402

OUT = os.path.join(ROOT, "tools/ideas/comping_pairs.json")
MIN_OVERLAP_FRAC = 0.5
MIN_NOTES = 4
MAX_LOSERS_PER_WINNER = 6


def _enabled_note_key(notes):
    """Content fingerprint over enabled notes (pitch/start/duration rounded)."""
    return tuple(sorted((n.pitch, round(n.start, 4), round(n.duration, 4))
                        for n in notes if n.enabled))


def _note_dicts(notes):
    return [{"pitch": n.pitch, "start": n.start, "duration": n.duration,
             "velocity": n.velocity} for n in notes if n.enabled]


def mine_project(pj):
    p = from_dict(pj)
    by_track: dict[str, dict[str, list]] = {}
    for c in p.clips:
        if c.start is None or c.end is None or c.end <= c.start:
            continue
        bucket = by_track.setdefault(c.track, {"main": [], "take": []})
        if c.clip_source == "arrangement":
            bucket["main"].append(c)
        elif c.clip_source == "take_lane":
            bucket["take"].append(c)

    pairs = []
    for track, bucket in by_track.items():
        for m in bucket["main"]:
            m_notes = [n for n in m.notes if n.enabled]
            if len(m_notes) < MIN_NOTES:
                continue
            m_key = _enabled_note_key(m.notes)
            losers = []
            for t in bucket["take"]:
                t_notes = [n for n in t.notes if n.enabled]
                if len(t_notes) < MIN_NOTES:
                    continue
                ov = min(m.end, t.end) - max(m.start, t.start)
                if ov <= 0 or ov < MIN_OVERLAP_FRAC * min(m.end - m.start, t.end - t.start):
                    continue
                if _enabled_note_key(t.notes) == m_key:
                    continue                     # the promoted take itself — not a loser
                losers.append(t)
            if not losers:
                continue
            losers = losers[:MAX_LOSERS_PER_WINNER]
            role = clip_role(m.notes, m.is_drum)
            pairs.append({
                "track": track,
                "role": role,
                "is_drum": m.is_drum,
                "span_beats": [m.start, m.end],
                "winner": {"n_notes": len(m_notes), "notes": _note_dicts(m.notes)},
                "winner_is_reduction": any(len([n for n in t.notes if n.enabled])
                                           > 2 * len(m_notes) for t in losers),
                "losers": [{"n_notes": len([n for n in t.notes if n.enabled]),
                            "span_beats": [t.start, t.end],
                            "notes": _note_dicts(t.notes)} for t in losers],
            })
    return pairs


def main():
    proj_dir = os.path.join(ROOT, "store/projects")
    index = json.load(open(os.path.join(proj_dir, "_index.json")))
    with_takes = [e for e in index if e.get("note_source_counts", {}).get("take_lane")]
    print(f"scanning {len(with_takes)} / {len(index)} projects with take-lane notes...")

    all_pairs, per_project = [], Counter()
    by_role, reductions = Counter(), 0
    for e in with_takes:
        fp = os.path.join(proj_dir, f"{e['id']}.json")
        if not os.path.exists(fp):
            continue
        pairs = mine_project(json.load(open(fp)))
        for pr in pairs:
            pr["project"] = e["name"]
            pr["project_id"] = e["id"]
            by_role[pr["role"]] += 1
            reductions += bool(pr["winner_is_reduction"])
        per_project[e["name"]] = len(pairs)
        all_pairs.extend(pairs)

    n_ab = sum(len(p["losers"]) for p in all_pairs)
    manifest = {
        "schema_version": 1,
        "mined_at": "2026-07-02",
        "semantics": "winner = main-lane (comped/kept) clip; losers = overlapping take-lane "
                     "alternates on the same track. Historical preferences, NOT yet validated "
                     "as taste-model training data.",
        "n_winners": len(all_pairs),
        "n_ab_pairs": n_ab,
        "by_role": dict(by_role),
        "n_winner_is_reduction": reductions,
        "top_projects": per_project.most_common(10),
        "pairs": all_pairs,
    }
    core._save_json_atomic(OUT, manifest, indent=2)

    print(f"winners with >=1 losing take: {len(all_pairs)}  ->  A/B pairs: {n_ab}")
    print(f"by role: {dict(by_role)}   winner-is-reduction: {reductions}")
    print("top projects:", per_project.most_common(6))
    print(f"manifest: {os.path.relpath(OUT, ROOT)}")
    print("\nsample (eyeball these):")
    for pr in all_pairs[:4]:
        print(f"  [{pr['project']}] {pr['track']!r} role={pr['role']} "
              f"win={pr['winner']['n_notes']}n vs {len(pr['losers'])} takes "
              f"({[l['n_notes'] for l in pr['losers']]}n){' REDUCTION' if pr['winner_is_reduction'] else ''}")


if __name__ == "__main__":
    main()
