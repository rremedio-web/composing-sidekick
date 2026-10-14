"""Feed Ricardo's booth votes into the taste model — on clean ground only.

Takes the decided pairs from tools/ideas/pairwise_manifest.json (winners filled
by the voting page, or applied here via --votes), DROPS any pair touching a
blocklisted pack/demo project, and records each surviving NON-REPEAT pair as a
two-candidate rated round via core.rate_candidates (winner=best, loser=worst).

Selection-mode semantics (2026-07-03; ears_ab added 2026-07-10):
  repeat    -> NEVER trains (it would duplicate a past preference pair);
               scored as test-retest consistency vs the original winner.
  random    -> trains, AND counts toward the EAR-vs-MODEL scoreboard
               (uncertainty-selected pairs are ~coin-flips by construction).
  ears_ab   -> trains, AND counts identically to random in that same scoreboard —
               A = the ears-gated pick, B = today's pick; agreement against the
               frozen predicted_winner (the ears pick) IS the promotion metric for
               tools/build_ears_booth.py rounds.
  develop_ab -> NEVER trains (measurement-only); counts agreement on its own
               develop promotion track. One side is the developed pick (synthetic
               id, notes frozen in the sidecar); the other is the retrieved pick
               (always real). The developed side shuffles per pair.
  uncertain -> trains; its agreement is printed as a diagnostic only.
Agreement compares his winner against the PRE-VOTE frozen predicted_winner in
tools/ideas/pairwise_predictions.json — never recomputed after retraining.

store/scoreboard.json carries the pre-registered promotion rule and one entry
per round (idempotency: a round_id already present refuses to re-ingest).
Nothing anywhere reads "promotable" to change behavior — flipping the rule's
status is a deliberate human act.

Run: .venv/bin/python tools/ingest_booth_votes.py \
       --votes tools/ideas/booth_votes.json [--dry-run] [--ingest-golds]
"""
import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
sys.path.insert(0, os.path.join(ROOT, "ingest"))

MANIFEST = os.path.join(ROOT, "tools/ideas/pairwise_manifest.json")
PREDICTIONS = os.path.join(ROOT, "tools/ideas/pairwise_predictions.json")
SCOREBOARD = os.path.join(ROOT, "store/scoreboard.json")
INGEST_JOURNAL = os.path.join(ROOT, "store/ingest_journal.json")

PROMOTION_RULE = {
    "text": ("The taste model may gain any pre-filtering or ranking role only after "
             "agreement >= 0.7 on >= 5 decided RANDOM pairs in 3 consecutive booth "
             "rounds. Until then it is a side signal: predictions stay locked in "
             "sidecars, never entering selection, scoring, or page display for the "
             "current round. Repeat pairs never train; uncertain pairs never score."),
    "min_agreement": 0.7,
    "min_random_pairs": 5,
    "consecutive_rounds": 3,
    "registered_at": "2026-07-03",
    "status": "side_signal",
}


def notes_sha256(notes):
    """Content hash binding a manifest pair to the exact notes it was frozen with
    (F6). Uses core._note_dicts so both note shapes hash identically — the digest
    freezes exported MIDI bytes, but staging and training reload notes from the
    MUTABLE phrase index by ID, so the round must also bind the note content."""
    import core  # noqa: E402  (deferred: heavy import)
    canonical = json.dumps(core._note_dicts(notes), sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def booth_round_digest(manifest, predictions):
    """Canonical digest of every artifact that can affect Booth interpretation."""
    manifest_rows = []
    for item in manifest:
        row = {key: item.get(key) for key in
               ("pair_id", "A_phrase_id", "B_phrase_id", "selection_mode")}
        # F6: bind the frozen note content when present. CRITICAL — absent fields
        # must NOT enter the canonical row, so every pre-existing round's digest is
        # byte-identical (old manifests carry no *_notes_sha256 keys).
        for key in ("A_notes_sha256", "B_notes_sha256"):
            if item.get(key) is not None:
                row[key] = item[key]
        for side in ("A", "B"):
            path = item.get("%s_midi" % side)
            playback = {"ref": os.path.basename(path) if path else None, "sha256": None}
            if path:
                try:
                    with open(path, "rb") as file:
                        playback["sha256"] = hashlib.sha256(file.read()).hexdigest()
                except OSError:
                    playback["sha256"] = "missing"
            row["%s_playback" % side] = playback
        manifest_rows.append(row)
    prediction_rows = [dict(item) for item in (predictions or {}).get("pairs", [])]
    payload = {
        "manifest": sorted(manifest_rows, key=lambda item: item.get("pair_id", 0)),
        "model": (predictions or {}).get("model") or {},
        "predictions": sorted(prediction_rows, key=lambda item: item.get("pair_id", 0)),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def verify_booth_note_bindings(manifest, by_id, predictions=None):
    """F6: refuse loudly if any pair's frozen note hash no longer matches the live
    phrase index — a rebuilt index would otherwise stage/train notes different from
    the ones the round was frozen. Absent hash (pre-F6 rounds) => grandfathered, skipped.
    Missing phrases are left to the existing missing-phrase handling at each site.

    develop_ab: BOTH sides verify against digest-bound sidecar notes — A is the
    developed pick (synthetic id); B is the transposed answer material Ricardo
    judged, not the raw index phrase."""
    sidecar_a_notes = {}
    sidecar_by_pair = {}
    for pair in (predictions or {}).get("pairs") or []:
        pair_id = pair.get("pair_id")
        if pair_id is not None:
            sidecar_by_pair[pair_id] = pair
        a_id = pair.get("A_phrase_id")
        if a_id and str(a_id).startswith("develop:") and pair.get("A_notes") is not None:
            sidecar_a_notes[a_id] = pair["A_notes"]
    for item in manifest:
        is_develop = item.get("selection_mode") == "develop_ab"
        sidecar_pair = sidecar_by_pair.get(item.get("pair_id")) if is_develop else None
        for side in ("A", "B"):
            expected = item.get("%s_notes_sha256" % side)
            if expected is None:
                continue
            if is_develop:
                frozen = (sidecar_pair or {}).get("%s_notes" % side)
                if frozen is None:
                    raise ValueError(
                        "develop pair %s side %s missing frozen %s_notes in sidecar"
                        % (item.get("pair_id"), side, side))
                if notes_sha256(frozen) != expected:
                    raise ValueError(
                        "develop %s notes for pair %s no longer match the frozen sidecar "
                        "(side %s) — rebuild the round"
                        % (side, item.get("pair_id"), side))
                continue
            pid = item.get("%s_phrase_id" % side)
            phrase = by_id.get(pid)
            if phrase is None or phrase.get("notes") is None:
                if side == "A" and str(pid or "").startswith("develop:"):
                    frozen = sidecar_a_notes.get(pid)
                    if frozen is None:
                        raise ValueError(
                            "develop pair %s side A missing frozen A_notes in sidecar"
                            % item.get("pair_id"))
                    if notes_sha256(frozen) != expected:
                        raise ValueError(
                            "developed A notes for %s no longer match the frozen sidecar "
                            "(pair %s, side A) — rebuild the round" % (pid, item.get("pair_id")))
                continue
            if notes_sha256(phrase["notes"]) != expected:
                raise ValueError(
                    "phrase %s changed since the round was frozen — rebuild the round "
                    "(pair %s, side %s)" % (pid, item.get("pair_id"), side))


def validate_booth_round_artifacts(manifest, predictions):
    """Bind a modern Booth manifest to the exact frozen prediction sidecar."""
    if not isinstance(manifest, list) or not manifest:
        raise ValueError("Booth manifest must be a non-empty list")

    modern_flags = ["selection_mode" in item for item in manifest if isinstance(item, dict)]
    if len(modern_flags) != len(manifest) or any(modern_flags) != all(modern_flags):
        raise ValueError("Booth manifest mixes legacy and prediction-backed pairs")

    def indexed(rows, label):
        result = {}
        for item in rows:
            if (not isinstance(item, dict) or type(item.get("pair_id")) is not int
                    or item["pair_id"] <= 0):
                raise ValueError("%s contains an invalid pair_id" % label)
            pair_id = item["pair_id"]
            if pair_id in result:
                raise ValueError("%s contains duplicate pair_id %r" % (label, pair_id))
            result[pair_id] = item
        return result

    manifest_by_id = indexed(manifest, "Booth manifest")

    legacy = not any(modern_flags)
    if legacy:
        if predictions is not None:
            raise ValueError("legacy Booth manifest must not use a prediction sidecar")
        return ""
    if not isinstance(predictions, dict):
        raise ValueError("prediction-backed Booth manifest requires a sidecar")

    round_id = str(predictions.get("round_id") or "").strip()
    pairs = predictions.get("pairs")
    if not round_id or not isinstance(pairs, list):
        raise ValueError("Booth prediction sidecar needs round_id and pairs")
    expected_digest = booth_round_digest(manifest, predictions)
    if predictions.get("artifact_digest") != expected_digest:
        raise ValueError("Booth prediction sidecar digest does not match its contents")
    if round_id != "booth_" + expected_digest[:12]:
        raise ValueError("Booth round_id does not match the frozen artifact digest")

    prediction_by_id = indexed(pairs, "Booth prediction sidecar")
    if set(manifest_by_id) != set(prediction_by_id):
        raise ValueError("Booth manifest and prediction pair IDs do not match")

    # F7a: ears_ab and develop_ab are two mutually-exclusive whole-round classes.
    # If ANY pair is class C the round must be ALL class C AND model.kind == C;
    # the two classes never mix. This kills contamination paths at the gate.
    modes = {item.get("selection_mode") for item in manifest}
    model_kind = (predictions.get("model") or {}).get("kind")
    for kind in ("ears_ab", "develop_ab"):
        if kind in modes or model_kind == kind:
            if modes != {kind}:
                raise ValueError(
                    "a %s Booth round must have every pair in %s mode "
                    "(mixed selection modes: %s)"
                    % (kind, kind, sorted(m for m in modes if m)))
            if model_kind != kind:
                raise ValueError(
                    "%s pairs require the sidecar model.kind == %r — "
                    "this round is mislabeled (model.kind=%r)" % (kind, kind, model_kind))

    for pair_id, item in manifest_by_id.items():
        prediction = prediction_by_id[pair_id]
        for key in ("A_phrase_id", "B_phrase_id", "selection_mode"):
            if item.get(key) != prediction.get(key):
                raise ValueError("Booth pair %r disagrees on %s" % (pair_id, key))
        if item.get("selection_mode") not in (
                "random", "uncertain", "repeat", "ears_ab", "develop_ab"):
            raise ValueError("Booth pair %r has an invalid selection_mode" % pair_id)
        expected_phrases = {item["A_phrase_id"], item["B_phrase_id"]}
        predicted_winner = prediction.get("predicted_winner")
        if predicted_winner is None and predictions.get("model"):
            raise ValueError("Booth pair %r is missing a model prediction" % pair_id)
        if predicted_winner is not None and predicted_winner not in expected_phrases:
            raise ValueError("Booth pair %r has an invalid predicted_winner" % pair_id)
        if item.get("selection_mode") == "repeat":
            past_winner = (prediction.get("repeat_of") or {}).get("past_winner")
            if past_winner not in expected_phrases:
                raise ValueError("Booth repeat pair %r has an invalid past_winner" % pair_id)
    return round_id


def validate_booth_manifest_winners(manifest):
    """The no---votes legacy path reads winners straight from the manifest; they
    must obey the same grammar as a page export instead of silently skipping."""
    for item in manifest:
        raw_winner = item.get("winner", "")
        if raw_winner is None:
            raw_winner = ""
        if not isinstance(raw_winner, str):
            raise ValueError(
                "Booth manifest winner for pair %r must be text" % item.get("pair_id")
            )
        winner = raw_winner.strip()
        if winner.lower() in ("", "skip", "tie") or winner.upper() in ("A", "B"):
            continue
        raise ValueError(
            "Booth manifest winner for pair %r must be A, B, tie, or skip"
            % item.get("pair_id")
        )


def validate_booth_golds(golds, manifest, votes_by_pair):
    """Return unique gold phrase IDs that were actually chosen as A/B winners."""
    if golds is None:
        golds = []
    if not isinstance(golds, list):
        raise ValueError("Booth golds must be a list")
    normalized = []
    seen = set()
    for phrase_id in golds:
        if not isinstance(phrase_id, str) or not phrase_id.strip():
            raise ValueError("each Booth gold must be a phrase ID")
        phrase_id = phrase_id.strip()
        if phrase_id in seen:
            raise ValueError("Booth golds contain duplicates")
        seen.add(phrase_id)
        normalized.append(phrase_id)

    eligible = set()
    manifest_by_pair = {item["pair_id"]: item for item in manifest}
    for pair_id, winner in votes_by_pair.items():
        item = manifest_by_pair[pair_id]
        if winner == "A":
            eligible.add(item["A_phrase_id"])
        elif winner == "B":
            eligible.add(item["B_phrase_id"])
    for phrase_id in normalized:
        if str(phrase_id).startswith("develop:"):
            raise ValueError(
                "Booth gold %r is a developed side — developed ids can never become "
                "gold anchors" % phrase_id)
        if phrase_id not in eligible:
            raise ValueError("Booth gold %r was not an A/B winner" % phrase_id)
    return normalized


def _project_of(phrase_id, phrase_by_id=None):
    """Project owning a phrase id: the record's project (lookup), falling
    back to parsing only for legacy positional ids (``proj|c0|w0``). Pids
    carry no project segment, so without a record there is nothing to parse.
    """
    if phrase_by_id:
        rec = phrase_by_id.get(phrase_id)
        if isinstance(rec, dict) and rec.get("project"):
            return rec["project"]
    text = str(phrase_id)
    return text.rsplit("|", 2)[0] if "|" in text else text


def validate_booth_gold_sources(golds, phrase_by_id, blocked_fn):
    """Preflight gold sources before any taste-ledger write occurs."""
    for phrase_id in golds:
        if phrase_id not in phrase_by_id:
            raise ValueError("Booth gold %s is missing from the phrase index" % phrase_id)
        if blocked_fn(_project_of(phrase_id, phrase_by_id)):
            raise ValueError("Booth gold %s belongs to a blocked project" % phrase_id)
    return list(golds)


def validate_booth_vote_payload(page, manifest, expected_round="", require_complete=False):
    """Validate and normalize a Booth page payload without touching persistent state.

    Autosave calls may contain a valid subset of the round. Final ingestion must pass
    ``require_complete=True`` so a duplicated or missing pair can never become an
    irreversible scoreboard entry.
    """
    if not isinstance(page, dict):
        raise ValueError("Booth votes payload must be an object")
    if not isinstance(manifest, list):
        raise ValueError("Booth manifest must be a list")

    expected_ids = set()
    for item in manifest:
        if (not isinstance(item, dict) or type(item.get("pair_id")) is not int
                or item["pair_id"] <= 0):
            raise ValueError("Booth manifest contains an invalid pair_id")
        pair_id = item["pair_id"]
        if pair_id in expected_ids:
            raise ValueError("Booth manifest contains duplicate pair IDs")
        expected_ids.add(pair_id)

    if expected_round:
        actual_round = str(page.get("round_id") or "").strip()
        if actual_round != expected_round:
            raise ValueError(
                "Booth votes round %r does not match expected round %r"
                % (actual_round, expected_round)
            )

    rows = page.get("votes")
    if not isinstance(rows, list):
        raise ValueError("Booth votes payload must contain a votes list")

    by_pair = {}
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("pair_id")) is not int
                or row["pair_id"] <= 0):
            raise ValueError("each Booth vote needs an integer pair_id")
        pair_id = row["pair_id"]
        if pair_id not in expected_ids:
            raise ValueError("Booth votes contain unknown pair_id %r" % pair_id)
        if pair_id in by_pair:
            raise ValueError("Booth votes contain duplicate pair_id %r" % pair_id)

        raw_winner = row.get("winner", "")
        if raw_winner is None:
            raw_winner = ""
        if not isinstance(raw_winner, str):
            raise ValueError("Booth winner for pair %r must be text" % pair_id)
        winner = raw_winner.strip()
        if winner.lower() in ("", "skip"):
            winner = ""
        elif winner.lower() == "tie":
            winner = "tie"
        elif winner.upper() in ("A", "B"):
            winner = winner.upper()
        else:
            raise ValueError("Booth winner for pair %r must be A, B, tie, or skip" % pair_id)
        by_pair[pair_id] = winner

    if require_complete and set(by_pair) != expected_ids:
        missing = sorted(expected_ids - set(by_pair))
        raise ValueError("Booth votes are incomplete; missing pair IDs %s" % missing)
    validate_booth_golds(page.get("golds", []), manifest, by_pair)
    return by_pair


def plan_booth_ingest(manifest, predictions, blocked_fn, index_ids,
                      phrase_by_id=None):
    """Pure planner — no I/O, no core import (selftest 39 runs it synthetically).

    manifest: pairs with 'winner' filled (A/B/tie/'' ); predictions: the locked
    sidecar dict or None; blocked_fn(project)->bool; index_ids: known phrase_ids.
    Returns {"train","repeats","scoreboard","dropped_blocked","dropped_missing","legacy"}.
    """
    pred_by_pair = {p["pair_id"]: p for p in (predictions or {}).get("pairs", [])}
    legacy = bool(manifest) and all("selection_mode" not in it for it in manifest)
    train, repeats, dropped_blocked, dropped_missing = [], [], [], []
    n_random = n_agree = n_uncertain = n_unc_agree = n_tie_or_skip_random = 0
    for it in manifest:
        w = (it.get("winner") or "").strip().upper()
        mode = it.get("selection_mode", "legacy")
        pr = pred_by_pair.get(it.get("pair_id"), {})
        if w not in ("A", "B"):
            if mode in ("random", "ears_ab", "develop_ab"):
                n_tie_or_skip_random += 1
            continue
        win_id = it["A_phrase_id"] if w == "A" else it["B_phrase_id"]
        los_id = it["B_phrase_id"] if w == "A" else it["A_phrase_id"]
        if mode == "repeat":
            past = pr.get("repeat_of") or {}
            repeats.append({"pair_id": it["pair_id"], "current_winner": win_id,
                            "past_winner": past.get("past_winner"),
                            "consistent": win_id == past.get("past_winner")})
            continue                      # repeats NEVER train
        projs = [_project_of(i, phrase_by_id) for i in (win_id, los_id)]
        if any(blocked_fn(p) for p in projs):
            dropped_blocked.append(it["pair_id"])
            continue                      # contaminated ground: no training, no scoreboard
        if win_id not in index_ids or los_id not in index_ids:
            dropped_missing.append(it["pair_id"])
            continue
        predicted = pr.get("predicted_winner")
        if predicted:
            if mode in ("random", "ears_ab", "develop_ab"):
                n_random += 1
                n_agree += win_id == predicted
            elif mode == "uncertain":
                n_uncertain += 1
                n_unc_agree += win_id == predicted
        if mode != "develop_ab":
            train.append({"pair_id": it["pair_id"], "win_id": win_id, "los_id": los_id})
    n_rep_decided = len(repeats)
    scoreboard = {
        "n_random": n_random, "n_agree": n_agree,
        "agreement": (n_agree / n_random) if n_random else None,
        "n_uncertain": n_uncertain, "n_uncertain_agree": n_unc_agree,
        "n_repeat": n_rep_decided,
        "repeat_consistency": (sum(1 for r in repeats if r["consistent"]) / n_rep_decided)
                              if n_rep_decided else None,
        "n_tie_or_skip_random": n_tie_or_skip_random,
    }
    return {"train": train, "repeats": repeats, "scoreboard": scoreboard,
            "dropped_blocked": dropped_blocked, "dropped_missing": dropped_missing,
            "legacy": legacy}


def _develop_move_from_pair(item):
    a_id = str(item.get("A_phrase_id") or "")
    b_id = str(item.get("B_phrase_id") or "")
    if a_id.startswith("develop:"):
        prefix = "develop:%s:" % b_id
        if not a_id.startswith(prefix):
            return None
        return a_id[len(prefix):]
    if b_id.startswith("develop:"):
        prefix = "develop:%s:" % a_id
        if not b_id.startswith(prefix):
            return None
        return b_id[len(prefix):]
    return None


def derive_develop_per_move(manifest, predictions):
    """Per-move decided/agree counts for a develop_ab round (diagnostic only)."""
    pred_by_pair = {p["pair_id"]: p for p in (predictions or {}).get("pairs", [])}
    per_move = {}
    for item in manifest:
        winner = (item.get("winner") or "").strip().upper()
        if winner not in ("A", "B"):
            continue
        move = _develop_move_from_pair(item)
        a_id = item.get("A_phrase_id")
        b_id = item.get("B_phrase_id")
        if not move:
            continue
        win_id = a_id if winner == "A" else b_id
        predicted = pred_by_pair.get(item.get("pair_id"), {}).get("predicted_winner")
        bucket = per_move.setdefault(move, {"decided": 0, "agree": 0})
        bucket["decided"] += 1
        if predicted and win_id == predicted:
            bucket["agree"] += 1
    return per_move


def accumulate_develop_per_move(rounds):
    """Sum per_move diagnostics across all develop_ab scoreboard rounds."""
    acc = {}
    for rnd in rounds:
        if rnd.get("kind") != "develop_ab":
            continue
        for move, stats in (rnd.get("per_move") or {}).items():
            bucket = acc.setdefault(move, {"decided": 0, "agree": 0})
            bucket["decided"] += stats.get("decided", 0)
            bucket["agree"] += stats.get("agree", 0)
    return acc


def promotion_status(rounds, rule):
    """Trailing streak of qualifying TASTE rounds (>=min pairs AND >=min agreement).

    ears_ab and develop_ab rounds are separate promotion tracks and must never
    advance or break the taste-model streak — they are skipped as if they were
    not in the history at all. Absent kind (every round before ears_ab existed)
    counts as taste.
    """
    streak = 0
    for r in reversed(rounds):
        if r.get("kind") not in (None, "taste"):
            continue
        if (r.get("n_random", 0) >= rule["min_random_pairs"]
                and (r.get("agreement") or 0.0) >= rule["min_agreement"]):
            streak += 1
        else:
            break
    return {"qualifying_streak": streak,
            "promotable": streak >= rule["consecutive_rounds"]}


def develop_promotion_status(rounds, rule):
    """Trailing streak of qualifying DEVELOP rounds — mirror of ears_promotion_status
    filtered TO kind == "develop_ab". Non-develop rounds are transparent."""
    streak = 0
    for r in reversed(rounds):
        if r.get("kind") != "develop_ab":
            continue
        if (r.get("n_random", 0) >= rule["min_random_pairs"]
                and (r.get("agreement") or 0.0) >= rule["min_agreement"]):
            streak += 1
        else:
            break
    return {"qualifying_streak": streak,
            "promotable": streak >= rule["consecutive_rounds"]}


def ears_promotion_status(rounds, rule):
    """Trailing streak of qualifying EARS rounds — the mirror of promotion_status
    filtered TO kind == "ears_ab" (F7b). The registered rule requires
    consecutive_rounds qualifying rounds; a single qualifying round is NOT
    promotion-ready. Non-ears rounds are transparent (skipped), matching how
    promotion_status treats ears rounds on the taste track."""
    streak = 0
    for r in reversed(rounds):
        if r.get("kind") != "ears_ab":
            continue
        if (r.get("n_random", 0) >= rule["min_random_pairs"]
                and (r.get("agreement") or 0.0) >= rule["min_agreement"]):
            streak += 1
        else:
            break
    return {"qualifying_streak": streak,
            "promotable": streak >= rule["consecutive_rounds"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--votes", default=None,
                    help="page export {votes:[{pair_id,winner}],golds:[...]} to apply first")
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--predictions", default=PREDICTIONS)
    ap.add_argument("--ingest-golds", action="store_true",
                    help="rate the page's golds as REAL gold anchors (default: just list them)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import core  # noqa: E402  (deferred: heavy import)
    from parse_als import is_blocked_project  # noqa: E402

    manifest = json.load(open(args.manifest))
    predictions = json.load(open(args.predictions)) if os.path.exists(args.predictions) else None
    page_golds = []
    try:
        current_round = validate_booth_round_artifacts(manifest, predictions)
    except ValueError as exc:
        sys.exit("invalid Booth round artifacts: %s" % exc)
    if predictions and not args.votes:
        sys.exit("predictions-backed Booth rounds require a complete --votes payload")
    if args.votes:
        page = json.load(open(args.votes))
        try:
            by_pair = validate_booth_vote_payload(
                page,
                manifest,
                current_round,
                require_complete=True,
            )
        except ValueError as exc:
            sys.exit("invalid Booth votes payload: %s" % exc)
        page_golds = validate_booth_golds(page.get("golds", []), manifest, by_pair)
        for it in manifest:
            it["winner"] = by_pair[it["pair_id"]]
    else:
        try:
            validate_booth_manifest_winners(manifest)
        except ValueError as exc:
            sys.exit("invalid Booth manifest winners: %s" % exc)

    idx = json.load(open(os.path.join(ROOT, "store/phrase_index.json")))
    phrases = idx if isinstance(idx, list) else idx.get("phrases", [])
    sys.path.insert(0, os.path.join(ROOT, "analyze"))
    from corpus_keys import phrase_lookup
    by_id = phrase_lookup(phrases)  # pid, id, alias (and provenance aliases)
    try:
        verify_booth_note_bindings(manifest, by_id, predictions)  # F6
    except ValueError as exc:
        sys.exit(str(exc))
    if args.ingest_golds:
        try:
            validate_booth_gold_sources(page_golds, by_id, is_blocked_project)
        except ValueError as exc:
            sys.exit("invalid Booth golds: %s" % exc)

    develop_ids = set()
    for p in (predictions or {}).get("pairs", []):
        for side in ("A", "B"):
            pid = p.get("%s_phrase_id" % side)
            if str(pid or "").startswith("develop:"):
                develop_ids.add(pid)
    plan = plan_booth_ingest(
        manifest, predictions, is_blocked_project, set(by_id) | develop_ids,
        phrase_by_id=by_id)
    round_id = current_round or "booth_legacy"

    sb = json.load(open(SCOREBOARD)) if os.path.exists(SCOREBOARD) else \
        {"promotion_rule": PROMOTION_RULE, "rounds": []}
    terminal_round_exists = any(r.get("round_id") == round_id for r in sb["rounds"])
    if predictions and predictions.get("model", {}).get("weights_md5"):
        try:
            import hashlib
            cur = json.load(open(os.path.join(ROOT, "store/taste_model.json")))
            cur_md5 = hashlib.md5(json.dumps(cur.get("weights", [])).encode()).hexdigest()
            if cur_md5 != predictions["model"]["weights_md5"]:
                print("note: taste model changed since predictions were locked "
                      "(fine — agreement uses the pre-vote snapshot)")
        except Exception:
            pass

    s = plan["scoreboard"]
    print("round %s: %d trainable pairs, %d repeats, dropped %s blocked / %s missing"
          % (round_id, len(plan["train"]), len(plan["repeats"]),
             plan["dropped_blocked"] or "0", plan["dropped_missing"] or "0"))
    if s["n_random"]:
        print("EAR vs MODEL (random pairs only): %d/%d agree (%.2f)"
              % (s["n_agree"], s["n_random"], s["agreement"]))
    if s["n_uncertain"]:
        print("  uncertain-pair agreement %d/%d — diagnostic only "
              "(~coin-flips by construction, not the promotion metric)"
              % (s["n_uncertain_agree"], s["n_uncertain"]))
    if s["n_repeat"]:
        print("  repeat consistency: %.2f over %d probes (test-retest of the ear)"
              % (s["repeat_consistency"], s["n_repeat"]))
    if page_golds:
        verb = "rating as REAL gold anchors" if args.ingest_golds else \
            "NOT rated (re-run with --ingest-golds if these are genuinely ME)"
        print("page golds: %s — %s" % (page_golds, verb))
    if args.dry_run:
        print("dry run — nothing written.")
        return

    from ingest_journal import claim_submission, complete_submission, peek_submission
    legacy_identity = hashlib.sha256(json.dumps(
        [{key: item.get(key) for key in ("pair_id", "A_phrase_id", "B_phrase_id")}
         for item in manifest],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()[:12]
    ingest_identity = current_round or "legacy:" + legacy_identity
    submission_scope = "booth:%s" % ingest_identity
    submission_payload = {
        "round_id": round_id,
        "artifact_digest": (predictions or {}).get("artifact_digest"),
        "votes": [[item["pair_id"], item.get("winner", "")] for item in manifest],
        "golds": page_golds,
        "ingest_golds": bool(args.ingest_golds),
    }
    if terminal_round_exists:
        # peek, never claim: an aborting branch must not persist a pending claim
        # that a second identical run would then "recover" as a completed ingest
        try:
            journal_status = peek_submission(
                INGEST_JOURNAL, submission_scope, submission_payload,
            )
        except ValueError as exc:
            sys.exit("Booth ingest retry conflict: %s" % exc)
        if journal_status == "pending":
            complete_submission(INGEST_JOURNAL, submission_scope)
            print("recovered completed Booth submission from its scoreboard entry")
            return
        if journal_status == "complete":
            sys.exit("Booth ingest submission is already complete")
        sys.exit("Booth scoreboard exists without matching journal history")
    try:
        journal_status = claim_submission(
            INGEST_JOURNAL, submission_scope, submission_payload,
        )
    except ValueError as exc:
        sys.exit("Booth ingest retry conflict: %s" % exc)
    if journal_status == "complete":
        # legacy / sidecar-less rounds never write a scoreboard row, so for them
        # the journal itself is the terminal artifact
        if plan["legacy"] or not predictions:
            sys.exit("Booth ingest submission is already complete")
        sys.exit("Booth journal is complete but its scoreboard entry is missing")

    before = core._taste_model_summary()
    for t in plan["train"]:
        win, los = by_id[t["win_id"]], by_id[t["los_id"]]
        cands = [{"id": t["win_id"], "role": win.get("role", "lead"), "source": "lead",
                  "notes": win["notes"]},
                 {"id": t["los_id"], "role": los.get("role", "lead"), "source": "lead",
                  "notes": los["notes"]}]
        core.rate_candidates(cands, best=0, worst=1,
                             reason="booth pair %s (blind, %s)"
                                    % (t["pair_id"], ingest_identity),
                             idempotency_key="booth:%s:pair:%s" % (
                                 ingest_identity, t["pair_id"],
                             ))
    if args.ingest_golds:
        for pid in page_golds:
            p = by_id.get(pid)
            core.rate(1, label="gold", phrase_id=pid, notes=p["notes"],
                      comment="booth gold (%s)" % ingest_identity,
                      idempotency_key="booth:%s:gold:%s" % (ingest_identity, pid))

    core._save_json_atomic(args.manifest, manifest)
    if not plan["legacy"] and predictions:
        import datetime
        round_kind = (predictions.get("model") or {}).get("kind") or "taste"
        round_entry = {"round_id": round_id,
                       "date": datetime.date.today().isoformat(),
                       "kind": round_kind,
                       **{k: s[k] for k in ("n_random", "n_agree", "agreement",
                                            "n_uncertain", "n_repeat",
                                            "repeat_consistency")},
                       "model_weights_md5": predictions.get("model", {})
                                                       .get("weights_md5")}
        if round_kind == "develop_ab":
            round_entry["per_move"] = derive_develop_per_move(manifest, predictions)
        sb["rounds"].append(round_entry)
        core._save_json_atomic(SCOREBOARD, sb)
        st = promotion_status(sb["rounds"], sb["promotion_rule"])
        print("promotion status: streak %d/%d%s"
              % (st["qualifying_streak"], sb["promotion_rule"]["consecutive_rounds"],
                 " — PROMOTABLE (a human may now flip the rule's status)"
                 if st["promotable"] else ""))
        if round_kind == "ears_ab":
            rule = sb["promotion_rule"]
            this_round_qualifies = (s["n_random"] >= rule["min_random_pairs"]
                                    and (s["agreement"] or 0.0) >= rule["min_agreement"])
            # F7b: the registered rule requires 3 CONSECUTIVE qualifying ears
            # rounds — a single qualifying round is not promotion-ready. Report the
            # trailing ears streak, never claim readiness below consecutive_rounds.
            ears_st = ears_promotion_status(sb["rounds"], rule)
            print("ears promotion metric: %d/%d agree (%.2f) — this round %s"
                  % (s["n_agree"], s["n_random"], s["agreement"] or 0.0,
                     "qualifies" if this_round_qualifies else "does not qualify"))
            print("ears round streak: %d/%d consecutive qualifying%s "
                  "(diagnostic for the ears track only; taste streak above is untouched)"
                  % (ears_st["qualifying_streak"], rule["consecutive_rounds"],
                     " — EARS PROMOTABLE (a human may now flip store/ears_default.json)"
                     if ears_st["promotable"] else ""))
        if round_kind == "develop_ab":
            rule = sb["promotion_rule"]
            this_round_qualifies = (s["n_random"] >= rule["min_random_pairs"]
                                    and (s["agreement"] or 0.0) >= rule["min_agreement"])
            dev_st = develop_promotion_status(sb["rounds"], rule)
            print("develop promotion metric: %d/%d agree (%.2f) — this round %s"
                  % (s["n_agree"], s["n_random"], s["agreement"] or 0.0,
                     "qualifies" if this_round_qualifies else "does not qualify"))
            print("develop round streak: %d/%d consecutive qualifying%s "
                  "(diagnostic for the develop track only; taste/ears streaks untouched)"
                  % (dev_st["qualifying_streak"], rule["consecutive_rounds"],
                     " — DEVELOP TRACK READY (aggregate metric met; human culls moves)"
                     if dev_st["promotable"] else ""))
            acc_pm = accumulate_develop_per_move(sb["rounds"])
            if acc_pm:
                print("develop per-move (accumulated across develop rounds):")
                for move in sorted(acc_pm):
                    stats = acc_pm[move]
                    thin = " [thin evidence]" if stats["decided"] < 5 else ""
                    agr = (stats["agree"] / stats["decided"]) if stats["decided"] else 0.0
                    print("  %-18s %d/%d agree (%.2f)%s"
                          % (move, stats["agree"], stats["decided"], agr, thin))
    else:
        print("legacy manifest / no predictions sidecar — scoreboard skipped")

    complete_submission(INGEST_JOURNAL, submission_scope)

    after = core._taste_model_summary()
    print("taste: %s -> %s pairs · weight %s -> %s · train acc %s"
          % (before["n_pairs"], after["n_pairs"], before["weight"], after["weight"],
             after.get("train_accuracy")))
    for fw in after.get("feature_weights", [])[:5]:
        print("  %-15s %+.2f" % (fw["feature"], fw["weight"]))


if __name__ == "__main__":
    main()
