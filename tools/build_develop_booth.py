#!/usr/bin/env python
"""Pre-registered develop-vs-retrieve Booth round (spec section 6).

For N prompt seeds: B = the retrieved pick verbatim (answer_my_loop); A = the
same pick developed (develop_my_loop with pin_source). Moves cycle evenly
across the 8-move roster. Pairs where A == B (no-op transform) are skipped.
Ears OFF — this round is measurement-only on its own promotion track.

Usage:
  .venv/bin/python tools/build_develop_booth.py [--n 16] [--seed 7] [--dry-run]
Then: build_booth_page.py -> vote -> ingest_booth_votes.py as any round.
"""
import argparse
import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from ingest_booth_votes import booth_round_digest, notes_sha256  # noqa: E402
from rank_pairwise import MANIFEST, PREDICTIONS  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "analyze"))
from corpus_keys import phrase_lookup  # noqa: E402


def _is_index_id(index_lookup, pick_id) -> bool:
    """True when pick_id names a phrase in the prebuilt phrase lookup."""
    return bool(pick_id) and pick_id in index_lookup

# 3 slow-burn flavours + 5 motivic ops — deterministic roster order (spec §6).
_DEVELOP_ROSTER = (
    "slow-burn:reveal",
    "slow-burn:accent",
    "slow-burn:add",
    "motivic:reharm",
    "motivic:fragment",
    "motivic:invert",
    "motivic:retro",
    "motivic:walk",
)
_PICK_SEED = 0  # fixed retrieval seed for answer_my_loop (B side) — only prompt/move rotation uses --seed
# pass 2 = full-blend accent / two-thirds adds / _thin_notes-equal reveal; walk gets :walk2
_DEVELOP_SEED = 2  # develop_my_loop seed only — pin_source keeps the pick identical


def _notes_equal(a, b):
    return notes_sha256(a) == notes_sha256(b)


def _resolved_move(sampled_move, result):
    return result.get("develop_move") or sampled_move


def build_pairs(index_phrases, n, seed, core_mod):
    index_lookup = phrase_lookup(index_phrases)
    lead = [p for p in index_phrases if p.get("role") == "lead" and p.get("notes")]
    lead.sort(key=lambda p: p.get("id", ""))
    if not lead:
        return [], {}
    step = max(len(lead) // max(n, 1), 1)
    prompts = [lead[(seed + i * step) % len(lead)] for i in range(n)]
    pairs = []
    move_counts = {m: 0 for m in _DEVELOP_ROSTER}
    for ordinal, prompt in enumerate(prompts):
        move = _DEVELOP_ROSTER[(ordinal + seed) % len(_DEVELOP_ROSTER)]
        b = core_mod.answer_my_loop(
            prompt["notes"], transform=None, seed=_PICK_SEED,
        )
        pick_id = (b.get("source_phrase_ids") or [None])[0]
        assert pick_id, "B_phrase_id must be a real pick id"
        assert _is_index_id(index_lookup, pick_id), \
            "B_phrase_id must be a real phrase index id"
        a = core_mod.develop_my_loop(
            prompt["notes"], move=move, key=b.get("key"), seed=_DEVELOP_SEED,
            pin_source=pick_id,
        )
        if _notes_equal(a.get("answer_notes") or [], b.get("answer_notes") or []):
            continue
        resolved = _resolved_move(move, a)
        pairs.append({
            "prompt_id": prompt.get("id"),
            "move": move,
            "resolved_move": resolved,
            "pick_id": pick_id,
            "a_notes": a.get("answer_notes") or [],
            "b_notes": b.get("answer_notes") or [],
            "tempo": b.get("tempo") or prompt.get("tempo") or 120.0,
        })
        move_counts[move] += 1
    return pairs, move_counts


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--n", type=int, default=16)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import core

    idx = json.load(open(os.path.join(ROOT, "store/phrase_index.json")))
    phrases = idx if isinstance(idx, list) else idx.get("phrases", [])
    pairs, move_counts = build_pairs(phrases, args.n, args.seed, core)
    if len(pairs) < 5:
        sys.exit("only %d surviving develop pairs (A != B) — not enough to judge; "
                 "raise --n or pick prompts/moves with more disagreement" % len(pairs))

    print("develop A/B round: %d pairs (developed side shuffled per pair, blind to the voter)"
          % len(pairs))
    print("per-move counts: %s" % ", ".join(
        "%s=%d" % (m, move_counts[m]) for m in _DEVELOP_ROSTER if move_counts[m]))
    zero_moves = [m for m in _DEVELOP_ROSTER if move_counts[m] == 0]
    if zero_moves:
        print("WARNING: ZERO surviving pairs for move(s): %s — roster gap; "
              "raise --n or check corpus/transform disagreement" % ", ".join(zero_moves))
    dev_a_count = dev_b_count = 0
    for i, p in enumerate(pairs, 1):
        develop_id = "develop:%s:%s" % (p["pick_id"], p["resolved_move"])
        develop_on_a = random.Random("develop-side:%s:%d" % (args.seed, i - 1)).random() < 0.5
        if develop_on_a:
            dev_a_count += 1
            side_tag = "dev=A"
            print("  pair %02d: %s  A=%s  B=%s  move=%s"
                  % (i, side_tag, develop_id, p["pick_id"], p["resolved_move"]))
        else:
            dev_b_count += 1
            side_tag = "dev=B"
            print("  pair %02d: %s  A=%s  B=%s  move=%s"
                  % (i, side_tag, p["pick_id"], develop_id, p["resolved_move"]))
    print("develop side summary: dev=A=%d dev=B=%d" % (dev_a_count, dev_b_count))
    if args.dry_run:
        # Nothing has been written yet — no MIDI, no manifest, no sidecar. The
        # round_id/digest can't be shown here: it's bound to the exported MIDI
        # bytes (booth_round_digest hashes each clip's on-disk sha256), which a
        # dry run must not create. Keep the export loop below this gate.
        print("dry run — nothing written.")
        print("promotion: needs 3 consecutive qualifying develop rounds (the ingest "
              "report tracks N/3 on the develop track only; taste and ears streaks "
              "are untouched).")
        return

    print("warning: replaces the in-flight Booth round — overwrites %s and %s"
          % (os.path.relpath(MANIFEST, ROOT), os.path.relpath(PREDICTIONS, ROOT)))

    manifest, preds = [], []
    for i, p in enumerate(pairs, 1):
        develop_id = "develop:%s:%s" % (p["pick_id"], p["resolved_move"])
        develop_on_a = random.Random("develop-side:%s:%d" % (args.seed, i - 1)).random() < 0.5
        if develop_on_a:
            a_phrase_id, b_phrase_id = develop_id, p["pick_id"]
            a_notes, b_notes = p["a_notes"], p["b_notes"]
        else:
            a_phrase_id, b_phrase_id = p["pick_id"], develop_id
            a_notes, b_notes = p["b_notes"], p["a_notes"]
        a_res = core.export_midi(a_notes, "develop_%02dA.mid" % i,
                                 tempo=float(p["tempo"]))
        b_res = core.export_midi(b_notes, "develop_%02dB.mid" % i,
                                 tempo=float(p["tempo"]))
        row = {
            "pair_id": i,
            "A_phrase_id": a_phrase_id,
            "B_phrase_id": b_phrase_id,
            "selection_mode": "develop_ab",
            "A_notes_sha256": notes_sha256(a_notes),
            "B_notes_sha256": notes_sha256(b_notes),
            "A_midi": a_res.get("path") if isinstance(a_res, dict) else "",
            "B_midi": b_res.get("path") if isinstance(b_res, dict) else "",
        }
        manifest.append(row)
        preds.append({
            "pair_id": i,
            "A_phrase_id": a_phrase_id,
            "B_phrase_id": b_phrase_id,
            "selection_mode": "develop_ab",
            "predicted_winner": develop_id,
            "A_notes": a_notes,
            "B_notes": b_notes,
        })

    sidecar = {"model": {"kind": "develop_ab"}, "pairs": preds}
    digest = booth_round_digest(manifest, sidecar)
    sidecar["artifact_digest"] = digest
    sidecar["round_id"] = "booth_" + digest[:12]

    print("develop A/B round %s: %d pairs written" % (sidecar["round_id"], len(manifest)))
    import core
    core._save_json_atomic(MANIFEST, manifest, indent=1)
    core._save_json_atomic(PREDICTIONS, sidecar, indent=1)
    print("wrote %s + %s — next: build_booth_page.py, vote blind, ingest."
          % (os.path.relpath(MANIFEST, ROOT), os.path.relpath(PREDICTIONS, ROOT)))
    print("promotion: needs 3 consecutive qualifying develop rounds (the ingest report "
          "tracks N/3 on the develop track only; taste and ears streaks are untouched).")


if __name__ == "__main__":
    main()
