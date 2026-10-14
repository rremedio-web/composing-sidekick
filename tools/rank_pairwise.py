"""Pairwise A/B harness: test melodic-aliveness scorers against Ricardo's EAR.

Humans are surgical at "which of these two is more alive?" and useless at
absolute scores. So we judge clips pairwise, then ask each scorer: did you rank
the winner above the loser? The scorer that best predicts the ear wins.

  build (no args): pick clips spread across resolve_score, export listenable MIDI,
                   write a BLIND pair manifest (scores locked in a sidecar).
  --score:         after you fill each pair's "winner" (A/B/tie/skip), print
                   pairwise agreement for resolve vs alive vs mean_surprise,
                   plus an Elo ranking of the clips.

Pair selection (2026-07-03): of the 18 pairs, 11 are UNCERTAIN — the taste
model's closest calls (|score_A − score_B| minimal), where his vote teaches it
most; 5 are RANDOM — the unbiased eval stream the EAR-vs-MODEL scoreboard is
computed on; 2 are REPEAT probes replayed from past rounds (test-retest
consistency; never retrained at ingest). The page-visible manifest carries
only `selection_mode`; taste scores, P(A) and predicted winners live in the
LOCKED sidecar tools/ideas/pairwise_predictions.json — do not peek before
voting. Determinism contract: given fixed taste_model.json + phrase_index.json
+ style_profile.json + ratings.json, build() reproduces byte-identical output.

Run: .venv/bin/python tools/rank_pairwise.py
     # ...listen to the pair_NN MIDI, set winners in the manifest...
     .venv/bin/python tools/rank_pairwise.py --score
"""
import hashlib
import json
import os
import random
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
from boringness import (phrase_to_degrees, mean_surprise, alive_score, resolve_score,
                        mechanical_reason, stratified_sample, pairwise_agreement, elo_ratings)
import taste  # noqa: E402  (pure math: sigmoid for P(A beats B))

MANIFEST = os.path.join(ROOT, "tools/ideas/pairwise_manifest.json")
SCORES = os.path.join(ROOT, "tools/ideas/pairwise_scores.json")
PREDICTIONS = os.path.join(ROOT, "tools/ideas/pairwise_predictions.json")
MAJOR = {0, 2, 4, 5, 7, 9, 11}
MINOR = {0, 2, 3, 5, 7, 8, 10}
# taste deliberately NOT in SCORERS: 11/18 pairs are uncertainty-SELECTED coin-flips,
# so all-pair agreement would be systematically depressed and misleading. The
# random-only scoreboard (ingest_booth_votes.py) is the one honest taste-vs-ear number.
SCORERS = ("resolve", "alive", "mean_surprise")
N_CLIPS = 12
N_PAIRS = 18
N_UNCERTAIN = 11
N_RANDOM = 5
N_REPEAT = 2
MAX_DEGREE = 4
SEED = 7


def _tonic_pc(notes):
    c = Counter(n["pitch"] % 12 for n in notes)
    top = max(c.values())
    return min(pc for pc, v in c.items() if v == top)


def _scale_for(degrees):
    c = Counter(degrees)
    return MINOR if c.get(3, 0) > c.get(4, 0) else MAJOR


def _metrics_for(p, grammar):
    notes = p.get("notes", [])
    if len(notes) < 2:
        return None
    try:
        degs = phrase_to_degrees([{"pitch": n["pitch"], "start": n["start"]} for n in notes],
                                 _tonic_pc(notes))
        if not degs:
            return None
        return {"resolve": resolve_score(degs, grammar),
                "alive": alive_score(degs, grammar, _scale_for(degs)),
                "mean_surprise": mean_surprise(degs, grammar)}
    except Exception:
        return None


def _past_booth_pairs(rounds, index_by_id):
    """Resolve durable repeat-probe ids to current phrase record ids."""
    pairs = []
    for i, row in enumerate(rounds or []):
        candidates = row.get("candidates", []) if isinstance(row, dict) else []
        if not str(row.get("reason", "")).startswith("booth pair") or len(candidates) != 2:
            continue
        win = index_by_id.get(candidates[row.get("best", 0)].get("id"))
        lose = index_by_id.get(candidates[row.get("worst", 1)].get("id"))
        if win and lose:
            pairs.append((win.get("id"), lose.get("id"), i))
    return pairs


def select_pairs(clip_ids, taste_by_id, past_pairs, seed,
                 n_uncertain=N_UNCERTAIN, n_random=N_RANDOM, n_repeat=N_REPEAT,
                 max_degree=MAX_DEGREE):
    """Pure pair chooser — no I/O, no core import (selftest 38 runs it synthetically).

    clip_ids:    this round's clip phrase_ids.
    taste_by_id: {phrase_id: taste scalar}. Missing/flat scores => cold-start
                 fallback: NO uncertain pairs (uncertainty over a flat model is
                 id-order garbage) — everything non-repeat goes random.
    past_pairs:  [(winner_id, loser_id, order_tag)] from prior booth rounds;
                 the newest become repeat probes (test-retest), NEVER retrained.
    Returns (pairs, meta): pairs = [{a_id, b_id, selection_mode, a_first,
    repeat_of}], total always n_uncertain+n_random+n_repeat (shortfalls in
    uncertain/repeat are backfilled with randoms); meta flags fallbacks.
    """
    rnd = random.Random(seed)
    ids = list(clip_ids)

    def norm(a, b):
        return (a, b) if a <= b else (b, a)

    allpairs = [(ids[i], ids[j]) for i in range(len(ids)) for j in range(i + 1, len(ids))]
    degree = {i: 0 for i in ids}
    chosen, chosen_keys = [], set()

    vals = [taste_by_id.get(i) for i in ids]
    flat = (not ids or any(v is None for v in vals)
            or len({round(v, 9) for v in vals}) <= 1)
    meta = {"flat_model": flat, "relaxed": False}

    n_unc_done = 0
    if not flat:
        ranked = sorted(allpairs, key=lambda ab: (
            round(abs(taste_by_id[ab[0]] - taste_by_id[ab[1]]), 6), ab[0], ab[1]))
        for cap in (3, max_degree):          # relax the degree cap only if greedy stalls
            for a, b in ranked:
                if n_unc_done >= n_uncertain:
                    break
                if norm(a, b) in chosen_keys or degree[a] >= cap or degree[b] >= cap:
                    continue
                meta["relaxed"] = meta["relaxed"] or cap > 3
                chosen.append({"a_id": a, "b_id": b, "selection_mode": "uncertain",
                               "repeat_of": None})
                chosen_keys.add(norm(a, b))
                degree[a] += 1
                degree[b] += 1
                n_unc_done += 1
            if n_unc_done >= n_uncertain:
                break

    repeats = []
    for win, lose, _tag in sorted(past_pairs, key=lambda t: t[2], reverse=True):
        if len(repeats) >= n_repeat:
            break
        key = norm(win, lose)
        if key in chosen_keys:
            continue
        chosen_keys.add(key)
        repeats.append({"a_id": key[0], "b_id": key[1], "selection_mode": "repeat",
                        "repeat_of": {"past_winner": win}})

    n_rand = (n_uncertain - n_unc_done) + n_random + (n_repeat - len(repeats))
    pool = [ab for ab in allpairs if norm(*ab) not in chosen_keys]
    rnd.shuffle(pool)
    n_rand_done = 0
    for a, b in pool:
        if n_rand_done >= n_rand:
            break
        if degree[a] >= max_degree or degree[b] >= max_degree:
            continue
        chosen.append({"a_id": a, "b_id": b, "selection_mode": "random",
                       "repeat_of": None})
        chosen_keys.add(norm(a, b))
        degree[a] += 1
        degree[b] += 1
        n_rand_done += 1

    final = chosen + repeats
    rnd.shuffle(final)
    for p in final:
        p["a_first"] = rnd.random() < 0.5
    meta.update(n_uncertain=n_unc_done, n_random=n_rand_done, n_repeat=len(repeats))
    return final, meta


def manifest_rows(pairs, midi_by_id, notes_sha_by_id=None):
    """Page-visible rows. BLINDNESS: exactly one model-adjacent key leaks in —
    selection_mode. Scores/P(A)/predicted winners belong ONLY in the locked
    predictions sidecar (selftest 38 asserts this key-set). *_notes_sha256 (F6)
    binds the frozen note content — not model-derived, added only when supplied."""
    notes_sha_by_id = notes_sha_by_id or {}
    rows = []
    for n, pr in enumerate(pairs, 1):
        a, b = (pr["a_id"], pr["b_id"]) if pr["a_first"] else (pr["b_id"], pr["a_id"])
        row = {"pair_id": n,
               "A_phrase_id": a, "A_midi": midi_by_id.get(a),
               "B_phrase_id": b, "B_midi": midi_by_id.get(b),
               "winner": "",                 # YOU fill: A | B | tie | skip
               "selection_mode": pr["selection_mode"]}
        for side, pid in (("A", a), ("B", b)):
            sha = notes_sha_by_id.get(pid)
            if sha is not None:
                row["%s_notes_sha256" % side] = sha
        rows.append(row)
    return rows


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
        if len(notes) < 4:
            continue
        degs = phrase_to_degrees([{"pitch": n["pitch"], "start": n["start"]} for n in notes],
                                 _tonic_pc(notes))
        if len(degs) < 4 or mechanical_reason(degs, grammar) is not None:
            continue
        metrics = {
            "resolve": resolve_score(degs, grammar),
            "alive": alive_score(degs, grammar, _scale_for(degs)),
            "mean_surprise": mean_surprise(degs, grammar),
        }
        scored.append((metrics["resolve"], (p, metrics)))

    # Spread the audition across the NEW metric so the pairs actually exercise it.
    picks = stratified_sample(scored, N_CLIPS)   # returns sorted low -> high resolve

    # archive a voted manifest before clobbering it (mirror the audition pattern)
    if os.path.exists(MANIFEST):
        old = json.load(open(MANIFEST))
        if any((m.get("winner") or "").strip() for m in old):
            n = 1
            while os.path.exists(os.path.join(ROOT, "tools/ideas/pairwise_r%d_rated.json" % n)):
                n += 1
            arch = os.path.join(ROOT, "tools/ideas/pairwise_r%d_rated.json" % n)
            import core
            core._save_json_atomic(arch, old, indent=1)
            print("archived voted manifest -> %s" % os.path.relpath(arch, ROOT))

    core_mod, export = None, None
    try:
        import core as core_mod
        export = core_mod.export_midi
    except Exception as e:                        # pragma: no cover - env guard
        print("warning: core unavailable (%s); no MIDI export, no taste predictions" % e)

    from corpus_keys import phrase_lookup
    index_by_id = phrase_lookup(phrases)
    pick_ids = [p.get("id") for _, (p, _m) in picks]

    # taste scores for uncertainty pairing — cold-start guard: a flat/absent model
    # makes every |delta| zero and "uncertain" degenerates to id-order garbage.
    taste_by_id, model_meta = {}, {}
    if core_mod is not None:
        try:
            model = core_mod._taste_model()
            w = model.get("weights") or []
            if any(w) and core_mod._taste_weight() > 0.0:
                for pid in pick_ids:
                    taste_by_id[pid] = core_mod._taste_score(index_by_id[pid]["notes"],
                                                             role="lead")
                model_meta = {"n_pairs": model.get("n_pairs"),
                              "n_rounds": model.get("n_rounds"),
                              "weights_md5": hashlib.md5(json.dumps(w).encode()).hexdigest()}
            else:
                print("cold-start: taste model flat/ramping — all non-repeat pairs go random")
        except Exception as e:
            print("warning: taste unavailable (%s) — all non-repeat pairs go random" % e)

    # repeat-probe source: candidate_rounds is durable; voted manifests get overwritten
    past_pairs = []
    if core_mod is not None:
        try:
            past_pairs = _past_booth_pairs(
                core_mod._load_ratings_file().get("candidate_rounds", []),
                index_by_id)
        except Exception as e:
            print("warning: past booth pairs unavailable (%s) — no repeat probes" % e)

    pairs, meta = select_pairs(pick_ids, taste_by_id, past_pairs, SEED)
    if meta["flat_model"] and taste_by_id:
        print("note: taste scores flat across picks — uncertainty pairing skipped")
    if meta["relaxed"]:
        print("note: uncertain degree cap relaxed to %d (greedy stalled at 3)" % MAX_DEGREE)

    # export MIDI + lock scorer metrics for every clip in a pair (repeat probes
    # can pull clips from outside this round's 12 picks)
    clip_ids = pick_ids + sorted({c for pr in pairs for c in (pr["a_id"], pr["b_id"])}
                                 - set(pick_ids))
    midi_by_id, locked = {}, {}
    for i, pid in enumerate(clip_ids, 1):
        p = index_by_id.get(pid)
        if p is None:
            continue
        if export is not None:
            try:
                res = export(p["notes"], "pair_%02d.mid" % i,
                             tempo=float(p.get("tempo") or 120.0))
                midi_by_id[pid] = res.get("path") if isinstance(res, dict) else "pair_%02d.mid" % i
            except Exception as e:
                print("warning: export failed for %s (%s)" % (pid, e))
        m = _metrics_for(p, grammar)
        if m is not None:
            locked[pid] = m

    # F6: bind each pair to its frozen note content. core is optional in this
    # tool (env guard above); only hash when it's importable.
    notes_sha_by_id = {}
    if core_mod is not None:
        from ingest_booth_votes import notes_sha256
        for pid in clip_ids:
            p = index_by_id.get(pid)
            if p is not None and p.get("notes") is not None:
                notes_sha_by_id[pid] = notes_sha256(p["notes"])
    else:
        print("warning: core unavailable — round frozen WITHOUT note-content hashes (permanently grandfathered)")
    manifest = manifest_rows(pairs, midi_by_id, notes_sha_by_id)

    import core
    core._save_json_atomic(MANIFEST, manifest, indent=1)
    core._save_json_atomic(SCORES, locked, indent=1)

    # predictions sidecar: ALWAYS written (round_id + repeat provenance live here);
    # taste columns only when the model was usable. LOCKED — do not peek.
    if model_meta:
        for pid in clip_ids:
            if pid not in taste_by_id and pid in index_by_id:
                taste_by_id[pid] = core_mod._taste_score(index_by_id[pid]["notes"],
                                                         role="lead")
    rows = []
    for row, pr in zip(manifest, pairs):
        rec = {"pair_id": row["pair_id"], "selection_mode": row["selection_mode"],
               "A_phrase_id": row["A_phrase_id"], "B_phrase_id": row["B_phrase_id"],
               "repeat_of": pr["repeat_of"]}
        ta, tb = taste_by_id.get(row["A_phrase_id"]), taste_by_id.get(row["B_phrase_id"])
        if model_meta and ta is not None and tb is not None:
            rec.update(taste_A=ta, taste_B=tb, p_A=taste.sigmoid(ta - tb),
                       predicted_winner=row["A_phrase_id"] if ta >= tb else row["B_phrase_id"])
        rows.append(rec)
    from ingest_booth_votes import booth_round_digest
    predictions = {"model": model_meta, "pairs": rows}
    artifact_digest = booth_round_digest(manifest, predictions)
    round_id = "booth_" + artifact_digest[:12]
    predictions.update(round_id=round_id, artifact_digest=artifact_digest)
    core._save_json_atomic(PREDICTIONS, predictions, indent=1)

    n_modes = Counter(r["selection_mode"] for r in manifest)
    midi_dir = os.path.dirname(next(iter(midi_by_id.values()), "")) or "(export unavailable)"
    print("built %d blind pairs over %d clips (%s)."
          % (len(manifest), len(clip_ids),
             ", ".join("%d %s" % (n_modes[k], k)
                       for k in ("uncertain", "random", "repeat") if n_modes.get(k))))
    print("  round_id:  %s" % round_id)
    print("  manifest:  %s   <- set each 'winner' to A/B/tie/skip" % os.path.relpath(MANIFEST, ROOT))
    print("  midi:      %s/pair_01.mid .. pair_%02d.mid" % (midi_dir, len(clip_ids)))
    print("  scores:    %s   (locked; do not peek before judging)" % os.path.relpath(SCORES, ROOT))
    print("  predicted: %s   (locked; do not peek before judging)" % os.path.relpath(PREDICTIONS, ROOT))
    print("\nJudge each A vs B by ear, then: .venv/bin/python tools/ingest_booth_votes.py "
          "--votes tools/ideas/booth_votes.json")


def score():
    manifest = json.load(open(MANIFEST))
    locked = json.load(open(SCORES))
    paired = {k: [] for k in SCORERS}
    results = []
    for it in manifest:
        w = (it.get("winner") or "").strip().upper()
        if w == "A":
            win, los = it["A_phrase_id"], it["B_phrase_id"]
        elif w == "B":
            win, los = it["B_phrase_id"], it["A_phrase_id"]
        else:
            continue
        if win not in locked or los not in locked:
            continue
        results.append((win, los))
        for k in SCORERS:
            paired[k].append((locked[win][k], locked[los][k]))

    print("decided pairs: %d" % len(results))
    if not results:
        print("no winners set yet. Fill 'winner' (A/B) in the manifest, then re-run --score.")
        return

    print("\nscorer agreement with your ear (0.5 = chance, 1.0 = perfect):")
    ranked = sorted(SCORERS, key=lambda k: pairwise_agreement(paired[k]), reverse=True)
    for k in ranked:
        print("  %-14s %.2f" % (k, pairwise_agreement(paired[k])))

    best = ranked[0]
    if best == "resolve" and pairwise_agreement(paired["resolve"]) >= 0.7:
        print("\n-> resolve_score wins and clears 0.7: aliveness IS surprise-that-resolves. Pursue it.")
    elif pairwise_agreement(paired[ranked[0]]) < 0.6:
        print("\n-> nothing clears 0.6: no scorer tracks your ear. Trust the ear; the metric stays a side tool.")
    else:
        print("\n-> '%s' leads. Suggestive, not conclusive at this N -- run another round of pairs." % best)

    elo = elo_ratings(results)
    print("\nclip Elo ranking (high = your ear preferred it) vs resolve_score:")
    for pid in sorted(elo, key=lambda p: elo[p], reverse=True):
        print("  %-28s elo=%-7.1f resolve=%.2f" % (pid, elo[pid], locked[pid]["resolve"]))


if __name__ == "__main__":
    score() if "--score" in sys.argv else build()
