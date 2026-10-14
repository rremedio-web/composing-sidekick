#!/usr/bin/env python
"""Pre-registered ears-vs-current Booth round (spec section 6).

For N prompt seeds: A = the ears-gated pick, B = today's pick, same pool.
Pairs where they agree are skipped (nothing to learn). --silence-pairs adds
flag-gated diagnostic pairs (A = the pick, B = nothing) — diagnostic only,
never part of promotion.

Usage:
  .venv/bin/python tools/check_ears.py --save tools/ideas/moment_snapshot.json
  .venv/bin/python tools/build_ears_booth.py --moment tools/ideas/moment_snapshot.json \
      [--n 12] [--seed 7] [--silence-pairs 2] [--dry-run]
Then: build_booth_page.py -> vote -> ingest_booth_votes.py as any round.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from ingest_booth_votes import booth_round_digest, notes_sha256  # noqa: E402
from rank_pairwise import MANIFEST, PREDICTIONS  # noqa: E402


def build_pairs(index_phrases, m, n, seed, ears_mod, core_mod):
    lead = [p for p in index_phrases if p.get("role") == "lead" and p.get("notes")]
    lead.sort(key=lambda p: p.get("id", ""))
    step = max(len(lead) // max(n, 1), 1)
    prompts = [lead[(seed + i * step) % len(lead)] for i in range(n)]
    pairs = []
    for prompt in prompts:
        pool = core_mod.find_phrases(seed_notes=prompt["notes"], role="lead",
                                     k=24, prefer_liked=True)
        if not pool:
            continue
        current = pool[0]
        served = ears_mod.serve(pool, m, role="lead")["served"]
        if not served:
            continue
        eared = served[0]
        if eared.get("id") == current.get("id"):
            continue  # nothing to learn from an identical pick
        pairs.append((eared, current))
    return pairs


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--moment", required=True,
                    help="moment snapshot from check_ears --save (a REAL room)")
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--silence-pairs", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import core
    import ears

    m = json.load(open(args.moment))
    if m.get("ears") != "live":
        sys.exit("the saved moment is blind — capture one with Live up; "
                 "a blind round would test nothing")

    idx = json.load(open(os.path.join(ROOT, "store/phrase_index.json")))
    phrases = idx if isinstance(idx, list) else idx.get("phrases", [])
    ab = build_pairs(phrases, m, args.n, args.seed, ears, core)
    if len(ab) < 5:
        sys.exit("only %d disagreeing pairs — not enough to judge; raise --n "
                 "or capture a moment with more happening in it" % len(ab))

    print("ears A/B round: %d pairs (A = ears pick, blind to the voter)" % len(ab))
    if args.silence_pairs:
        print("note: %d silence pairs requested — diagnostic only, "
              "add after the core round is judged" % args.silence_pairs)
    if args.dry_run:
        # Nothing has been written yet — no MIDI, no manifest, no sidecar. The
        # round_id/digest can't be shown here: it's bound to the exported MIDI
        # bytes (booth_round_digest hashes each clip's on-disk sha256), which a
        # dry run must not create. Keep the export loop below this gate.
        print("dry run — nothing written.")
        return

    manifest, preds = [], []
    for i, (a, b) in enumerate(ab, 1):
        a_res = core.export_midi(a["notes"], "ears_%02dA.mid" % i,
                                 tempo=float(a.get("tempo") or m["tempo"]))
        b_res = core.export_midi(b["notes"], "ears_%02dB.mid" % i,
                                 tempo=float(b.get("tempo") or m["tempo"]))
        row = {"pair_id": i, "A_phrase_id": a["id"], "B_phrase_id": b["id"],
               "selection_mode": "ears_ab",
               "A_notes_sha256": notes_sha256(a["notes"]),  # F6: bind frozen notes
               "B_notes_sha256": notes_sha256(b["notes"]),
               "A_midi": a_res.get("path") if isinstance(a_res, dict) else "",
               "B_midi": b_res.get("path") if isinstance(b_res, dict) else ""}
        manifest.append(row)
        preds.append({"pair_id": i, "A_phrase_id": a["id"],
                      "B_phrase_id": b["id"], "selection_mode": "ears_ab",
                      "predicted_winner": a["id"]})

    sidecar = {"model": {"kind": "ears_ab", "moment_digest": m.get("digest")},
               "pairs": preds}
    digest = booth_round_digest(manifest, sidecar)
    sidecar["artifact_digest"] = digest
    sidecar["round_id"] = "booth_" + digest[:12]

    print("ears A/B round %s: %d pairs written" % (sidecar["round_id"], len(manifest)))
    import core
    core._save_json_atomic(MANIFEST, manifest, indent=1)
    core._save_json_atomic(PREDICTIONS, sidecar, indent=1)
    print("wrote %s + %s — next: build_booth_page.py, vote blind, ingest."
          % (os.path.relpath(MANIFEST, ROOT), os.path.relpath(PREDICTIONS, ROOT)))
    print("promotion: needs 3 consecutive qualifying ears rounds (the ingest report "
          "tracks N/3); when reached, follow docs/EARS_PROMOTION.md before flipping "
          "store/ears_default.json — an explicit human act, never automatic.")


if __name__ == "__main__":
    main()
