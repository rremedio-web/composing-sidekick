"""Feed Ricardo's SOLO audition votes into the ledger + taste model — honestly.

The 3-tier page exports {"ratings":[{"blind_id":N,"rating":"gold|passable|meh"}]}.
Semantics (2026-07-03, after his "gold meant passable" clarification):
  gold     -> core.rate(1, label='gold')      permanent anchor; guarded pseudo-pair retrain
  passable -> core.rate(1, label='passable')  ledger-only upvote; NEVER touches the gold set
  meh      -> no rate() call                  lives as preference-round loser + rated archive
Gold PROBES (manifest picked_by == "gold_probe": existing anchors re-auditioned blind):
  gold     -> reaffirming gold record (harmless)
  passable -> rate(1, label='ungold')  demoted but the upvote stays  + a passable record
  meh      -> rate(0, label='ungold')  demoted outright
Preference rounds (every clip heard blind under identical conditions — valid
comparisons whatever the labels mean):
  per gold:     rate_candidates([gold] + passables + mehs, best=0)
  per passable: rate_candidates([passable] + mehs, best=0)
Blocklisted projects are dropped, same rule as the booth ingester. The rated
manifest is archived to tools/ideas/rank_audition_<round>_rated.json; an
existing archive refuses to be overwritten (double-ingest guard).

Legacy 2-value vote files (r1-era, gold/meh only): pass --legacy-label passable
to reinterpret their "gold" clicks. Refused for files with native "passable"
votes — no guessing.

Run: .venv/bin/python tools/ingest_audition_votes.py \
       --votes tools/ideas/audition_votes_r2b.json --round r2b [--dry-run]
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
sys.path.insert(0, os.path.join(ROOT, "ingest"))

DEFAULT_MANIFEST = os.path.join(ROOT, "tools/ideas/rank_audition_manifest.json")
INGEST_JOURNAL = os.path.join(ROOT, "store/ingest_journal.json")
TIERS = ("gold", "passable", "meh")


def validate_audition_manifest_round(manifest, expected_round="", allow_roundless=False):
    """Return one normalized manifest round, rejecting mixed or mislabeled items."""
    if not isinstance(manifest, list) or not manifest:
        raise ValueError("audition manifest must be a non-empty list")
    rounds = []
    for item in manifest:
        if not isinstance(item, dict):
            raise ValueError("audition manifest entries must be objects")
        raw_round = str(item.get("round") or "").strip()
        rounds.append(
            raw_round if raw_round.startswith("r") else ("r" + raw_round if raw_round else "")
        )
    stated_rounds = {value for value in rounds if value}
    if stated_rounds and (len(stated_rounds) != 1 or any(not value for value in rounds)):
        raise ValueError("audition manifest mixes round identities")
    manifest_round = next(iter(stated_rounds), "")
    if expected_round and not manifest_round and not allow_roundless:
        raise ValueError("roundless audition manifest requires explicit legacy mode")
    if expected_round and manifest_round and manifest_round != expected_round:
        raise ValueError(
            "audition manifest round %r does not match expected round %r"
            % (manifest_round, expected_round)
        )
    return manifest_round


def validate_audition_vote_payload(page, manifest, expected_round="", require_complete=False,
                                   allow_roundless_manifest=False,
                                   allow_roundless_payload=False):
    """Validate a partial autosave or complete solo-audition vote payload."""
    if not isinstance(page, dict):
        raise ValueError("audition votes payload must be an object")
    if not isinstance(manifest, list) or not manifest:
        raise ValueError("audition manifest must be a non-empty list")

    expected_ids = set()
    for item in manifest:
        if (not isinstance(item, dict) or type(item.get("blind_id")) is not int
                or item["blind_id"] <= 0):
            raise ValueError("audition manifest contains an invalid blind_id")
        blind_id = item["blind_id"]
        if blind_id in expected_ids:
            raise ValueError("audition manifest contains duplicate blind IDs")
        expected_ids.add(blind_id)

    validate_audition_manifest_round(
        manifest, expected_round, allow_roundless=allow_roundless_manifest,
    )

    if expected_round:
        actual_round = str(page.get("round") or "").strip()
        if actual_round != expected_round and not (
                allow_roundless_payload and not actual_round):
            raise ValueError(
                "audition votes round %r does not match expected round %r"
                % (actual_round, expected_round)
            )

    rows = page.get("ratings")
    if not isinstance(rows, list):
        raise ValueError("audition votes payload must contain a ratings list")
    by_id = {}
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("blind_id")) is not int
                or row["blind_id"] <= 0):
            raise ValueError("each audition vote needs an integer blind_id")
        blind_id = row["blind_id"]
        if blind_id not in expected_ids:
            raise ValueError("audition votes contain unknown blind_id %r" % blind_id)
        if blind_id in by_id:
            raise ValueError("audition votes contain duplicate blind_id %r" % blind_id)
        raw_rating = row.get("rating", "")
        if raw_rating is None:
            raw_rating = ""
        if not isinstance(raw_rating, str):
            raise ValueError("audition rating for blind %r must be text" % blind_id)
        rating = raw_rating.strip().lower()
        if rating == "skip":
            rating = ""
        if rating not in ("",) + TIERS:
            raise ValueError("audition rating must be gold, passable, meh, or skip")
        by_id[blind_id] = rating

    if require_complete and set(by_id) != expected_ids:
        missing = sorted(expected_ids - set(by_id))
        raise ValueError("audition votes are incomplete; missing blind IDs %s" % missing)
    return by_id


def _project_of(phrase_id, phrase_by_id=None):
    """Project owning a phrase id: the record's project (lookup), falling
    back to parsing only for legacy positional ids (``proj|c0|w0``)."""
    if phrase_by_id:
        rec = phrase_by_id.get(phrase_id)
        if isinstance(rec, dict) and rec.get("project"):
            return rec["project"]
    text = str(phrase_id)
    return text.rsplit("|", 2)[0] if "|" in text else text


def plan_ingest(manifest, votes, blocked_fn, known_ids, round_tag, legacy_label=None,
                phrase_by_id=None):
    """Pure planner: no I/O, no core import — selftestable.

    manifest: list of {blind_id, phrase_id, picked_by?, ...}
    votes:    {blind_id: "gold"|"passable"|"meh"|""|None}
    blocked_fn(project) -> bool;  known_ids: phrase_ids present in the index.
    Returns {"rates", "rounds", "manifest_ratings", "dropped", "counts"}.
    """
    if legacy_label and any((v or "").strip().lower() == "passable" for v in votes.values()):
        raise ValueError("--legacy-label given but the votes file already has native "
                         "'passable' votes — refusing to reinterpret")
    tiers = {t: [] for t in TIERS}
    manifest_ratings, dropped = {}, []
    for it in manifest:
        tier = (votes.get(it["blind_id"]) or "").strip().lower()
        if tier == "gold" and legacy_label:
            tier = legacy_label
        if tier not in TIERS:
            continue
        manifest_ratings[it["blind_id"]] = tier
        project = _project_of(it["phrase_id"], phrase_by_id)
        if blocked_fn(project) or it["phrase_id"] not in known_ids:
            dropped.append(it["phrase_id"])
            continue
        tiers[tier].append(it)

    def is_probe(it):
        return it.get("picked_by") == "gold_probe"

    rates = []
    for it in tiers["gold"]:
        note = " (blind probe reaffirmed)" if is_probe(it) else ""
        rates.append({"blind_id": it["blind_id"], "phrase_id": it["phrase_id"],
                      "rating": 1, "label": "gold",
                      "comment": "solo audition %s blind %d%s" % (round_tag, it["blind_id"], note)})
    for it in tiers["passable"]:
        if is_probe(it):
            rates.append({"blind_id": it["blind_id"], "phrase_id": it["phrase_id"],
                          "rating": 1, "label": "ungold",
                          "comment": "blind probe %s: voted passable — demoted from gold"
                                     % round_tag})
        rates.append({"blind_id": it["blind_id"], "phrase_id": it["phrase_id"],
                      "rating": 1, "label": "passable",
                      "comment": "solo audition %s blind %d (passable)"
                                 % (round_tag, it["blind_id"])})
    for it in tiers["meh"]:
        if is_probe(it):
            rates.append({"blind_id": it["blind_id"], "phrase_id": it["phrase_id"],
                          "rating": 0, "label": "ungold",
                          "comment": "blind probe %s: voted meh — demoted from gold"
                                     % round_tag})

    rounds = []
    for w in tiers["gold"]:
        losers = tiers["passable"] + tiers["meh"]
        if losers:
            rounds.append({"winner": w["phrase_id"], "blind_id": w["blind_id"],
                           "losers": [l["phrase_id"] for l in losers],
                           "reason": "solo audition %s: gold blind %d over %d others (blind)"
                                     % (round_tag, w["blind_id"], len(losers))})
    for w in tiers["passable"]:
        if tiers["meh"]:
            rounds.append({"winner": w["phrase_id"], "blind_id": w["blind_id"],
                           "losers": [l["phrase_id"] for l in tiers["meh"]],
                           "reason": "solo audition %s: passable blind %d over %d mehs (blind)"
                                     % (round_tag, w["blind_id"], len(tiers["meh"]))})
    return {"rates": rates, "rounds": rounds, "manifest_ratings": manifest_ratings,
            "dropped": dropped,
            "counts": {t: len(v) for t, v in tiers.items()}}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--votes", required=True, help="votes JSON exported by the page")
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST)
    ap.add_argument("--round", required=True, dest="round_tag",
                    help="round tag, e.g. r2b — threaded into comments + archive name")
    ap.add_argument("--legacy-label", choices=("passable", "gold"), default=None,
                    help="reinterpret a legacy 2-value file's 'gold' votes as this tier")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import core  # noqa: E402  (deferred: heavy import)
    from parse_als import is_blocked_project  # noqa: E402

    manifest = json.load(open(args.manifest))
    votes_payload = json.load(open(args.votes))
    payload_round = (votes_payload.get("round") or "").strip()
    if args.legacy_label and payload_round and payload_round != args.round_tag:
        sys.exit("votes file is from round %r but --round is %r — refusing to "
                 "mix rounds" % (payload_round, args.round_tag))
    expected_round = args.round_tag
    try:
        votes = validate_audition_vote_payload(
            votes_payload, manifest, expected_round, require_complete=True,
            allow_roundless_manifest=bool(args.legacy_label),
            allow_roundless_payload=bool(args.legacy_label),
        )
    except ValueError as exc:
        sys.exit("invalid audition votes payload: %s" % exc)
    idx = json.load(open(os.path.join(ROOT, "store/phrase_index.json")))
    phrases = idx if isinstance(idx, list) else idx.get("phrases", [])
    sys.path.insert(0, os.path.join(ROOT, "analyze"))
    from corpus_keys import phrase_lookup
    by_id = phrase_lookup(phrases)  # pid, id, alias (and provenance aliases)

    plan = plan_ingest(manifest, votes, is_blocked_project, set(by_id), args.round_tag,
                       legacy_label=args.legacy_label, phrase_by_id=by_id)

    archive = os.path.join(ROOT, "tools/ideas/rank_audition_%s_rated.json" % args.round_tag)
    terminal_archive_exists = os.path.exists(archive)

    print("plan for round %s: %d gold, %d passable, %d meh"
          % (args.round_tag, plan["counts"]["gold"], plan["counts"]["passable"],
             plan["counts"]["meh"]))
    for r in plan["rates"]:
        print("  rate(%d, label=%r)  %s   -- %s"
              % (r["rating"], r["label"], r["phrase_id"], r["comment"]))
    for rd in plan["rounds"]:
        print("  round: %s > %d losers  -- %s"
              % (rd["winner"], len(rd["losers"]), rd["reason"]))
    if plan["dropped"]:
        print("  dropped (blocklist/missing): %s" % plan["dropped"])
    if args.dry_run:
        print("dry run — nothing written.")
        return

    from ingest_journal import claim_submission, complete_submission, peek_submission
    submission_scope = "audition:%s" % args.round_tag
    submission_payload = {
        "round": args.round_tag,
        "legacy_label": args.legacy_label,
        "manifest": [[item["blind_id"], item["phrase_id"], item.get("round", "")]
                     for item in manifest],
        "votes": [[blind_id, votes[blind_id]] for blind_id in sorted(votes)],
    }
    if terminal_archive_exists:
        # peek, never claim: an aborting branch must not persist a pending claim
        # that a second identical run would then "recover" as a completed ingest
        try:
            journal_status = peek_submission(
                INGEST_JOURNAL, submission_scope, submission_payload,
            )
        except ValueError as exc:
            sys.exit("audition ingest retry conflict: %s" % exc)
        if journal_status == "pending":
            complete_submission(INGEST_JOURNAL, submission_scope)
            print("recovered completed audition submission from its archive")
            return
        if journal_status == "complete":
            sys.exit("audition ingest submission is already complete")
        sys.exit("audition archive exists without matching journal history")
    try:
        journal_status = claim_submission(
            INGEST_JOURNAL, submission_scope, submission_payload,
        )
    except ValueError as exc:
        sys.exit("audition ingest retry conflict: %s" % exc)
    if journal_status == "complete":
        sys.exit("audition journal is complete but its archive is missing")

    before = core._taste_model_summary()
    action_indexes = {}
    for r in plan["rates"]:
        action_index = action_indexes.get(r["blind_id"], 0)
        action_indexes[r["blind_id"]] = action_index + 1
        entry = by_id[r["phrase_id"]]
        res = core.rate(r["rating"], label=r["label"], phrase_id=r["phrase_id"],
                        notes=entry["notes"], comment=r["comment"],
                        idempotency_key="audition:%s:rating:%s:%s" % (
                            args.round_tag, r["blind_id"], action_index,
                        ))
        if r["label"] == "passable":
            assert "taste_retrained" not in res, "passable must be ledger-only"
            print("PASSABLE %s — ledger-only, gold set untouched" % r["phrase_id"])
        elif r["label"] == "ungold":
            print("UNGOLD   %s — demoted (blind probe)" % r["phrase_id"])
        else:
            t = res.get("taste_retrained", {})
            print("GOLD     %s -> gold pairs %s, exam acc %s"
                  % (r["phrase_id"], t.get("n_gold_pairs"), t.get("train_accuracy")))
    for rd in plan["rounds"]:
        cands = [{"id": pid, "role": by_id[pid].get("role", "lead"), "source": "lead",
                  "notes": by_id[pid]["notes"]}
                 for pid in [rd["winner"]] + rd["losers"]]
        core.rate_candidates(
            cands, best=0, reason=rd["reason"],
            idempotency_key="audition:%s:preference:%s" % (
                args.round_tag, rd["blind_id"],
            ),
        )

    for it in manifest:
        if it["blind_id"] in plan["manifest_ratings"]:
            it["rating"] = plan["manifest_ratings"][it["blind_id"]]
    core._save_json_atomic(args.manifest, manifest)
    core._save_json_atomic(archive, manifest)
    complete_submission(INGEST_JOURNAL, submission_scope)

    after = core._taste_model_summary()
    golds_now = core.get_gold_phrases()
    print("\ntaste: %s -> %s pairs (+%s gold pseudo-pairs) · weight %s -> %s · exam acc %s"
          % (before["n_pairs"], after["n_pairs"], after.get("n_gold_pairs", 0),
             before["weight"], after["weight"], after.get("train_accuracy")))
    print("golds now: %d · archive: %s"
          % (len(golds_now), os.path.relpath(archive, ROOT)))
    if not golds_now:
        print("NOTE: gold shelf is EMPTY — answer_my_loop runs with n_gold=0 (graceful);")
        print("      gold-seeded audition builds are blocked until a real G lands.")
    print("next: .venv/bin/python tools/rank_audition.py --score")


if __name__ == "__main__":
    main()
