"""Blind audition harness to test the alive_score ranker against your EAR.

The pool A/B couldn't validate the ranker (clip_source != aliveness). This
generates the data that can: a spread of real leads for you to rate blind,
then an AUC of alive_score vs your gold/meh calls.

  build (no args): pick 12 non-mechanical leads spanning the alive_score range,
                   export listenable MIDI, write a BLIND manifest (scores hidden
                   in a locked sidecar so rating isn't biased).
  --score:         after you fill in each "rating" (gold/passable/meh/skip),
                   compute AUC = P(a kept clip outranks a meh under alive_score).

AMENDMENT (2026-07-03, decided BEFORE any r2b rating was compared against the
locked scores): ratings are now 3-tier (gold = "genuinely ME", passable =
"works, wouldn't chase it", meh). The pre-registered positive class widens to
kept = gold + passable — the semantic quantity ("clips his ear kept" vs meh)
is unchanged. The original gold-only AUC is still printed for r1 comparability.

Run: .venv/bin/python tools/rank_audition.py
     # ...listen to exports/audition_NN.mid, set ratings in the manifest...
     .venv/bin/python tools/rank_audition.py --score
"""
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
from boringness import phrase_to_degrees, alive_score, mechanical_reason, stratified_sample, auc_separation

MANIFEST = os.path.join(ROOT, "tools/ideas/rank_audition_manifest.json")
SCORES = os.path.join(ROOT, "tools/ideas/rank_audition_scores.json")
MAJOR = {0, 2, 4, 5, 7, 9, 11}
MINOR = {0, 2, 3, 5, 7, 8, 10}
N_PICKS = 12


def _tonic_pc(notes):
    c = Counter(n["pitch"] % 12 for n in notes)
    top = max(c.values())
    return min(pc for pc, v in c.items() if v == top)


def _scale_for(degrees):
    c = Counter(degrees)
    return MINOR if c.get(3, 0) > c.get(4, 0) else MAJOR


def build():
    agg = json.load(open(os.path.join(ROOT, "store/style_profile.json")))["aggregate"]
    grammar = agg.get("melodic_grammar_by_role", {}).get("lead") or agg["melodic_grammar"]
    idx = json.load(open(os.path.join(ROOT, "store/phrase_index.json")))
    phrases = idx if isinstance(idx, list) else idx.get("phrases", [])

    scored = []
    for p in phrases:
        if p.get("role") != "lead":
            continue
        notes = p.get("notes", [])
        if len(notes) < 4:               # need enough to be worth auditioning
            continue
        degs = phrase_to_degrees([{"pitch": n["pitch"], "start": n["start"]} for n in notes],
                                 _tonic_pc(notes))
        if len(degs) < 4 or mechanical_reason(degs, grammar) is not None:
            continue
        a = alive_score(degs, grammar, _scale_for(degs))
        scored.append((a, p))

    picks = stratified_sample(scored, N_PICKS)

    try:
        import core
        export = core.export_midi
    except Exception as e:                # pragma: no cover - env guard
        export = None
        print("warning: MIDI export unavailable (%s); manifest still written" % e)

    manifest, locked = [], {}
    for i, (a, p) in enumerate(picks, 1):
        notes = p["notes"]
        midi_file = None
        if export is not None:
            try:
                res = export(notes, "audition_%02d.mid" % i, tempo=float(p.get("tempo", 120.0)))
                midi_file = res.get("path") if isinstance(res, dict) else "audition_%02d.mid" % i
            except Exception as e:
                print("warning: export failed for %s (%s)" % (p.get("id"), e))
        manifest.append({
            "blind_id": i,
            "phrase_id": p.get("id"),
            "midi_file": midi_file,
            "n_notes": len(notes),
            "pitch_preview": [n["pitch"] for n in notes[:16]],
            "rating": "",                 # YOU fill: gold | meh | skip
        })
        locked[p.get("id")] = a

    import core
    core._save_json_atomic(MANIFEST, manifest, indent=1)
    core._save_json_atomic(SCORES, locked, indent=1)

    midi_dir = os.path.dirname(manifest[0]["midi_file"]) if manifest and manifest[0]["midi_file"] else "(export unavailable)"
    print("built blind audition of %d leads (alive_score hidden)." % len(manifest))
    print("  manifest:  %s   <- set each 'rating' to gold/meh/skip" % os.path.relpath(MANIFEST, ROOT))
    print("  midi:      %s/audition_01.mid .. audition_%02d.mid" % (midi_dir, len(manifest)))
    print("  scores:    %s   (locked; do not peek before rating)" % os.path.relpath(SCORES, ROOT))
    print("\nListen, rate blind, then: .venv/bin/python tools/rank_audition.py --score")


def score():
    manifest = json.load(open(MANIFEST))
    locked = json.load(open(SCORES))
    gold, passable, meh = [], [], []
    for it in manifest:
        r = (it.get("rating") or "").strip().lower()
        a = locked.get(it["phrase_id"])
        if a is None:
            continue
        if r == "gold":
            gold.append(a)
        elif r == "passable":
            passable.append(a)
        elif r == "meh":
            meh.append(a)
    kept = gold + passable
    print("rated: %d gold, %d passable, %d meh" % (len(gold), len(passable), len(meh)))
    if not kept or not meh:
        print("need at least one kept (gold/passable) AND one meh to compute separation. Rate more.")
        return
    auc = auc_separation(kept, meh)
    print("alive_score AUC (kept vs meh) = %.2f  (1.0 perfect, 0.5 chance, <0.5 inverted)" % auc)
    if gold and meh:
        print("  gold-only AUC = %.2f  (original r1 pre-registration, for comparability)"
              % auc_separation(gold, meh))
    print("  kept mean alive=%.2f   meh mean alive=%.2f"
          % (sum(kept) / len(kept), sum(meh) / len(meh)))
    if auc >= 0.7:
        print("  -> alive_score tracks your ear. Worth pursuing as a ranker.")
    elif auc <= 0.3:
        print("  -> inverted: your ear likes the LOW-surprise ones. Interesting.")
    else:
        print("  -> no real separation. The ranker idea stays dead; trust the ear.")


if __name__ == "__main__":
    score() if "--score" in sys.argv else build()
