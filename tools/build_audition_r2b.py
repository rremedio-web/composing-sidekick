"""Round 2b: the 12 unvoted round-2 clips + the current gold anchors slipped in
BLIND as probes, all re-shuffled and re-numbered.

Why: his r2 votes were lost, and his r1 "gold" clicks turned out to mean merely
"passable" — so before the two r1 golds keep steering retrieval and round
seeding, they must survive a blind re-listen. Voted G on the 3-tier page they
stay anchors; voted P/M the ingester demotes them (rate label='ungold').

Re-numbering blind_id 1..14 deliberately scrambles any memory of the morning
listen. Probe positions live only in the manifest (picked_by == "gold_probe")
— treat the manifest like the locked scores file: do not peek before voting.

Run: .venv/bin/python tools/build_audition_r2b.py
     # listen to exports/audition_01..14.mid in Live, vote on audition_vote.html
     .venv/bin/python tools/ingest_audition_votes.py \
         --votes tools/ideas/audition_votes_r2b.json --round r2b
"""
import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
from boringness import phrase_to_degrees, alive_score  # noqa: E402
from rank_audition import _tonic_pc, _scale_for  # noqa: E402
import core  # noqa: E402
import build_audition_page  # noqa: E402

MANIFEST = os.path.join(ROOT, "tools/ideas/rank_audition_manifest.json")
SCORES = os.path.join(ROOT, "tools/ideas/rank_audition_scores.json")
SEED = 23


def _grammar():
    agg = json.load(open(os.path.join(ROOT, "store/style_profile.json")))["aggregate"]
    return agg.get("melodic_grammar_by_role", {}).get("lead") or agg["melodic_grammar"]


def _alive_or_none(notes, grammar):
    try:
        degs = phrase_to_degrees([{"pitch": n["pitch"], "start": n["start"]} for n in notes],
                                 _tonic_pc(notes))
        return alive_score(degs, grammar, _scale_for(degs)) if degs else None
    except Exception:
        return None


def main():
    manifest = json.load(open(MANIFEST))
    assert all(not (m.get("rating") or "").strip() for m in manifest), \
        "manifest already carries votes — refusing to rebuild over a rated round"
    assert any(m.get("round") == 2 for m in manifest), \
        "expected the unvoted round-2 manifest (run build_audition_r2.py first)"

    idx = json.load(open(os.path.join(ROOT, "store/phrase_index.json")))
    phrases = idx if isinstance(idx, list) else idx.get("phrases", [])
    by_id = {p.get("id"): p for p in phrases}

    golds = core.get_gold_phrases()
    assert golds, "no gold anchors to probe — nothing to do"
    in_deck = {m["phrase_id"] for m in manifest}
    deck = [{"phrase_id": m["phrase_id"], "n_notes": m["n_notes"],
             "pitch_preview": m["pitch_preview"], "picked_by": m.get("picked_by", "")}
            for m in manifest]
    for g in golds:
        pid = g.get("phrase_id")
        if pid in in_deck:
            continue
        p = by_id.get(pid)
        assert p is not None, "gold %s missing from the phrase index" % pid
        deck.append({"phrase_id": pid, "n_notes": len(p["notes"]),
                     "pitch_preview": [n["pitch"] for n in p["notes"][:16]],
                     "picked_by": "gold_probe"})

    rnd = random.Random(SEED)
    rnd.shuffle(deck)

    grammar = _grammar()
    out, locked = [], {}
    for i, d in enumerate(deck, 1):
        p = by_id.get(d["phrase_id"])
        assert p is not None, "deck phrase %s missing from the phrase index" % d["phrase_id"]
        res = core.export_midi(p["notes"], "audition_%02d.mid" % i,
                               tempo=float(p.get("tempo") or 120.0))
        midi = res.get("path") if isinstance(res, dict) else "audition_%02d.mid" % i
        out.append({"blind_id": i, "phrase_id": d["phrase_id"], "midi_file": midi,
                    "n_notes": d["n_notes"], "pitch_preview": d["pitch_preview"],
                    "picked_by": d["picked_by"], "round": "2b", "rating": ""})
        locked[d["phrase_id"]] = _alive_or_none(p["notes"], grammar)

    core._save_json_atomic(MANIFEST, out, indent=1)
    core._save_json_atomic(SCORES, locked, indent=1)
    build_audition_page.main()

    n_probe = sum(1 for d in deck if d["picked_by"] == "gold_probe")
    print("round 2b: %d clips (%d round-2 + %d gold probes), order scrambled, "
          "blind_ids reassigned." % (len(out), len(out) - n_probe, n_probe))
    print("  probe positions withheld — don't open the manifest before voting.")
    print("  midi:  store/exports/audition_01.mid .. audition_%02d.mid" % len(out))
    print("  page:  tools/ideas/audition_vote.html  (G gold / P passable / M meh)")


if __name__ == "__main__":
    main()
