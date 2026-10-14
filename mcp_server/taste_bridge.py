"""Taste bridge: candidate rating, taste model, and gold phrase management.

This module contains all taste-related functions that were moved from core.py to
separate the taste layer from the rest of the generation pipeline. All moved
names remain accessible as core._name from the public API (server.py, selftest, etc.).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from operator import mul as _mul
from typing import Any

from mcp_server.core_shared import (
    _CACHE,
    _canonical_move,
    _l2_cosine,
    _load_artifact,
    _load_json,
    _load_ratings_file,
    _motifs_data,
    _note_dicts,
    _ratings_file_lock,
    _ratings_path,
    _taste_model_path,
    _save_json_atomic,
    _save_ratings_file,
    _save_ratings_file_unlocked,
    _validate_artifact_meta,
    _ALLOW_LEGACY_ARTIFACTS_WITHOUT_META,
    _EXPECTED_ARTIFACT_SCHEMA_VERSION,
    EXPORTS_DIR,
    HARMONY_JSON,
    INDEX_JSON,
    MOTIFS_JSON,
    NOTES_MD,
    ROOT,
    RATINGS_JSON,
    STORE,
    STYLE_JSON,
    STYLE_MD,
    STRUCTURE_JSON,
    TASTE_MODEL_JSON,
    set_ratings_path,
    _write_text_atomic,
    _clip_digest,
)

sys.path.insert(0, os.path.join(ROOT, "analyze"))

from build_index import (  # noqa: E402
    cosine,
    drum_vector,
    phrase_vector,
    search,
)
from corpus_keys import dedupe_pool, expand_ids, phrase_lookup  # noqa: E402
from style_profile import (  # noqa: E402
    PITCH_NAMES,
    diatonic_set,
    estimate_key,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import taste  # noqa: E402  (pure-Python preference model; never imports core)

sys.path.insert(0, os.path.join(ROOT, "tools"))
from boringness import is_mechanical, phrase_to_degrees  # noqa: E402  junk FLOOR only — never a score term


from config import (  # noqa: E402
    BEATS_PER_BAR,
    PP_URL,
    REJECT_NEAR_DUP_COSINE,
    ROLE_SOURCE,
    ROLE_THRESHOLDS,
    ROLES,
    SECTION_BUCKETS,
    _DENSITY_PARAMS,
    _DROP_ENERGY,
    _DRUM_DENSITY_TARGETS,
    _ROLE_ENERGY_THRESHOLD,
    _ROLE_LAYER_ORDER,
)

sys.path.insert(0, os.path.join(ROOT, "analyze"))
# score_phrase is imported lazily in _taste_features to avoid circular import

# One module universe for the bare import and the package import (F4). This
# claims only its own two names — never core aliases (only core.py may claim
# those). First-loaded wins via setdefault, so both import orders unify.
for _alias in ("taste_bridge", "mcp_server.taste_bridge"):
    sys.modules.setdefault(_alias, sys.modules[__name__])


# --- idempotency & gold phrases ---------------------------------------------

def _idempotency_fingerprint(payload: dict) -> str:
    """Canonical request identity used to reject conflicting retries."""
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _require_matching_idempotency(existing: dict, fingerprint: str, key: str) -> None:
    stored = existing.get("idempotency_fingerprint")
    if stored != fingerprint:
        detail = "has no verifiable fingerprint" if stored is None else "has different content"
        raise ValueError(f"idempotency key {key!r} {detail}")


def _rating_aliases(ratings: list[dict]) -> dict[str, str]:
    """Learn notes-digest -> phrase_id aliases from moveless ratings.

    When the phrase_id is in the index, the record's notes must be that
    phrase's own notes: a KEEP/DROP of a moved answer carries the heard notes,
    and aliasing those to the source would let a later notes-only rating of
    the heard clip ban or un-gold the untouched source. A phrase_id the index
    does not know (a composed gold, a phrase from a project no longer indexed)
    has nothing to compare against, so its record is the only word on it and
    teaches the alias as before. Records carrying a move never alias."""
    alias: dict[str, str] = {}
    by_id = phrase_lookup(_phrases())
    for rec in ratings:
        pid = rec.get("phrase_id")
        notes = rec.get("notes")
        if not pid or not notes or rec.get("move"):
            continue
        phrase = by_id.get(pid)
        if phrase is not None and phrase.get("notes"):
            if _clip_digest(notes) != _clip_digest(phrase["notes"]):
                continue
        alias["notes:" + _clip_digest(notes)] = pid
    return alias


_GOLD_UNMARK_LABELS = {"rejected", "ungold"}
# Gold-membership fold only (NOT the taste-retrain trigger): a later plain
# DROP of a golded source un-golds it — his word against his earlier word.
_GOLD_UNGOLD_LABELS = _GOLD_UNMARK_LABELS | {"dropped", "gold"}
# Soft survival demotion (ledger OUTCOME_DEMOTION_LABEL): served but not
# kept — moved to the back, never banned. Neutral by construction.
SERVED_NOT_KEPT_LABEL = "served-not-kept"


def _latest_rating_by_identity(ratings: list[dict], *,
                               labels: set[str] | None = None) -> dict[str, dict]:
    """Fold ratings to the LATEST record per phrase identity.

    Identity is ``phrase_id`` when present, else ``"notes:"+digest``; a
    moveless record matching the indexed phrase's own notes teaches the alias.
    ``labels`` restricts which records participate (gold membership only
    moves on gold/un-gold records, where a later plain DROP also un-golds);
    omit it to fold every rating.

    One fold, two readers — the gold set and the reject set. They disagreed
    before: _rating_vectors appends by SIGN across all records, so a phrase
    dropped and then re-approved lands in the up set AND the down set."""
    relevant: list[tuple[dict, str | None, str | None]] = []
    eligible = [r for r in ratings
                if labels is None or str(r.get("label") or "").lower() in labels]
    alias = _rating_aliases(eligible)
    for r in eligible:
        pid = r.get("phrase_id")
        digest = ("notes:" + _clip_digest(r["notes"])) if r.get("notes") else None
        relevant.append((r, pid, digest))
    latest: dict[str, dict] = {}
    for r, pid, digest in relevant:
        key = pid or (alias.get(digest, digest) if digest else None)
        if key is None:
            continue
        prev = latest.get(key)
        if prev is None or str(r.get("created_at", "")) >= str(prev.get("created_at", "")):
            latest[key] = r
    return latest


def get_gold_phrases() -> list[dict]:
    """The current 'gold' anchor set: real phrases Ricardo hand-picked as genuinely *him*.

    Only records that explicitly reference gold move membership: rate(rating=1, label='gold',
    notes=..., phrase_id=...) marks; a LATER rate(label='gold', rating<=0) or
    rate(label='rejected'/'ungold') on the same phrase un-golds. Ordinary likes/dislikes with
    other labels never touch the gold set. Identity is phrase_id when present, else a digest of
    the notes; a record carrying both teaches the alias so either identifier un-golds later.

    A moved DROP (a record carrying a move) never touches gold membership
    either way: it bans the (source, move) pairing, and the untouched source
    keeps its anchor."""
    ratings = [r for r in _load_ratings_file().get("ratings", [])
               if _canonical_move(r.get("move")) is None]
    latest = _latest_rating_by_identity(ratings, labels=_GOLD_UNGOLD_LABELS)
    return [r for r in latest.values()
            if (r.get("rating") or 0) > 0 and str(r.get("label") or "").lower() == "gold"
            and r.get("notes")]


def _is_soft_demotion(rec: dict) -> bool:
    """A neutral served-not-kept record: rating 0 under that label. Neither
    a verdict nor training signal — only a back-move for selection."""
    return (str(rec.get("label") or "").lower() == SERVED_NOT_KEPT_LABEL
            and (rec.get("rating") or 0) == 0)


def served_not_kept_identities() -> set[str]:
    """Identities carrying a soft survival demotion: phrase_id when the
    record has one, else the notes-digest identity. Latest record per
    identity wins — a later gold/keep clears the demotion. Consumers move
    these to the back — never ban them (neutral ratings never enter the
    reject set)."""
    ratings = _load_ratings_file().get("ratings", [])
    latest_demote = _latest_rating_by_identity(
        ratings, labels={SERVED_NOT_KEPT_LABEL})
    out: set[str] = set()
    for identity, rec in latest_demote.items():
        if not _is_soft_demotion(rec):
            continue
        rec_keys = _ban_lookup_keys(identity, rec)
        rec_index = max((i for i, candidate in enumerate(ratings)
                         if candidate is rec), default=-1)
        # Only a later positive judgment clears a survival demotion. A
        # moved DROP is about one transformation and cannot clear the
        # identity-level served-not-kept memory.
        cleared = any(
            (candidate.get("rating") or 0) > 0 and
            rec_keys & _ban_lookup_keys(
                candidate.get("phrase_id") or "", candidate)
            for candidate in ratings[rec_index + 1:])
        if cleared:
            continue
        out.add(identity)
        pid = rec.get("phrase_id")
        if pid:
            out.add(pid)
        if rec.get("notes"):
            try:
                out.add("notes:" + _clip_digest(rec["notes"]))
            except Exception:
                pass
    return out


def _demote_to_back(items: list[dict], demoted: set[str]) -> list[dict]:
    """Stable partition: demoted ids (or their notes-digest) slide behind
    everything else. Pure order move — nothing culled, no score touched."""
    if not demoted:
        return list(items)
    front: list[dict] = []
    back: list[dict] = []
    for it in items:
        key = it.get("id")
        digest = None
        if it.get("notes"):
            try:
                digest = "notes:" + _clip_digest(it["notes"])
            except Exception:
                digest = None
        (back if key in demoted or digest in demoted else front).append(it)
    return front + back


def _ban_lookup_keys(identity: str, rec: dict) -> set[str]:
    """Every key that can name this reject identity: the fold key, the
    phrase id, and the notes digest. Two folds can key the same phrase
    differently (alias learned in one but not the other), so all three
    must match."""
    keys = {identity}
    if rec.get("phrase_id"):
        keys.add(rec["phrase_id"])
    if rec.get("notes"):
        try:
            keys.add("notes:" + _clip_digest(rec["notes"]))
        except Exception:
            pass
    return keys


def rejected_phrases(kind: str = "harmonic") -> list[dict]:
    """Phrases whose LATEST rating is negative — the things he said no to.

    Un-golding is deliberately NOT a rejection: removing an anchor says "not one
    of my best", while a DROP says "don't show me this again". Conflating them
    would silently ban every phrase he ever demoted.

    Soft survival demotions never enter this fold at all: as the latest
    record they would shadow the earlier DROP they were never meant to
    lift (a demote is a back-move, not an un-DROP).

    Judgments are folded per (identity, move) pairing: the moveless record
    is its own pairing (the global ban), and each transformation he dropped
    is another. So dropping "thin" then "invert" bans both pairings; a
    later moved DROP never narrows a global ban; and only a later positive
    on the SAME pairing (or clear_rejects) lifts it — a KEEP of the plain
    source says nothing about the thinned version. One identity may
    therefore yield several entries, one per banned pairing."""
    ratings = [r for r in _load_ratings_file().get("ratings", [])
               if not _is_soft_demotion(r)]
    by_id = phrase_lookup(_phrases())  # pid first, then alias
    by_move: dict[str | None, list[dict]] = {}
    for r in ratings:
        by_move.setdefault(_canonical_move(r.get("move")), []).append(r)
    folded: list[tuple[str | None, str, dict]] = []
    for move in sorted(by_move, key=lambda m: (m is not None, m or "")):
        for identity, rec in _latest_rating_by_identity(
                by_move[move]).items():
            folded.append((move, identity, rec))
    out: list[dict] = []
    for move, identity, rec in sorted(folded, key=lambda t: (t[1], t[0] is not None, t[0] or "")):
        if (rec.get("rating") or 0) >= 0:
            continue
        if str(rec.get("label") or "").lower() in _GOLD_UNMARK_LABELS:
            continue
        vec = None
        pid = rec.get("phrase_id")
        if pid and pid in by_id and by_id[pid].get("kind") == kind:
            vec = by_id[pid]["vec"]
        elif rec.get("notes"):
            nd = _note_dicts(rec["notes"])
            if nd:
                vec = drum_vector(nd) if kind == "drum" else phrase_vector(nd)
        out.append({"identity": identity, "phrase_id": pid, "vec": vec,
                    # notes ride along so an undo can supersede a digest identity,
                    # which has no phrase_id to address it by
                    "notes": rec.get("notes"),
                    "created_at": rec.get("created_at"),
                    "comment": rec.get("comment"),
                    # the move that was actually rejected, if any: a moved
                    # DROP bans the (source, move) pairing, not the source
                    "move": move})
    return out


def reject_cull(pool: list[dict], *, kind: str = "harmonic",
                near_dup_cosine: float | None = None,
                floor: int = 3,
                move: str | None = None) -> tuple[list[dict], list[dict]]:
    """Remove what he already said no to. A GATE, not a score.

    Filters, never reorders: survivors keep their order so his existing ranking
    stays the only ranker (the ears doctrine, applied to rejects). Two named
    tiers — an exact identity match, and a near-duplicate, which is the only way
    to catch a DROP of a *composed* answer that carries no index id.

    ``move`` scopes moved DROPs to their pairing: a reject stored with a move
    culls a candidate only when the same move is being served — the untouched
    source stays servable, and a plain (moveless) DROP keeps the global ban.
    A moveless reject still culls under any move.

    Never reintroduces (F10): an exact verdict is never silently reversed to
    fill the pool — not even when fewer than ``floor`` survivors remain, not
    even when none remain. The returned ``suppressed`` list always names what
    was held back and why, so a caller that needs a last resort can offer it
    explicitly instead of serving a reject as if it were fresh. ``floor`` is
    retained in the signature for caller-side policy use; this gate itself no
    longer restores candidates to meet it."""
    if near_dup_cosine is None:
        near_dup_cosine = REJECT_NEAR_DUP_COSINE
    current = _canonical_move(move)
    rejects = rejected_phrases(kind=kind)
    if not rejects:
        return list(pool), []
    # Every form each reject is known by (pid + alias + provenance aliases),
    # mapped back to ALL its records: one phrase can carry a global ban plus
    # several banned (source, move) pairings, possibly stored under an old
    # alias. The pairing check below sees every one of them.
    recs_by_form: dict[str, list[dict]] = {}
    for r in rejects:
        forms: set = set()
        for key in ("identity", "phrase_id"):
            if r.get(key):
                forms |= expand_ids({r[key]}, pool)
        for form in forms:
            bucket = recs_by_form.setdefault(form, [])
            if all(existing is not r for existing in bucket):
                bucket.append(r)
    reject_vecs = [(r, r["vec"]) for r in rejects if r.get("vec")]

    def _pairing_bans(rec: dict) -> bool:
        """A moved reject bans only its own pairing being served now."""
        stored = rec.get("move")
        return stored is None or stored == current

    survivors: list[dict] = []
    suppressed: list[dict] = []
    for cand in pool:
        cid = cand.get("id")
        cand_ids = [i for i in (cid, cand.get("pid"), cand.get("alias")) if i]
        recs: list[dict] = []
        for form in cand_ids:
            for r in recs_by_form.get(form, ()):
                if all(existing is not r for existing in recs):
                    recs.append(r)
        if recs:
            banning = [r for r in recs if _pairing_bans(r)]
            if not banning:
                survivors.append(cand)   # only OTHER pairings were dropped
                continue
            # report the global ban when there is one, else the matching pairing
            rec = next((r for r in banning if r.get("move") is None), banning[0])
            suppressed.append({"id": cid, "tier": "dropped_id",
                               "dropped_at": rec.get("created_at"),
                               "reason": rec.get("comment"),
                               "move": rec.get("move")})
            continue
        cvec = cand.get("vec")
        hit = None
        if cvec and reject_vecs:
            for rec, rvec in reject_vecs:
                if cosine(cvec, rvec) >= near_dup_cosine and _pairing_bans(rec):
                    hit = rec
                    break
        if hit is not None:
            suppressed.append({"id": cid, "tier": "near_dropped",
                               "dropped_at": hit.get("created_at"),
                               "reason": hit.get("comment"),
                               "move": hit.get("move")})
            continue
        survivors.append(cand)

    return survivors, suppressed


# --- taste model: learn which candidate Ricardo would keep ------------------

_TASTE_FEATURE_NAMES = ["scale_fit", "interval_shape", "melodic_fit", "gesture_fit",
                        "groove", "register", "polyphony", "jumpiness", "staticness"]
_TASTE_MIN_PAIRS = 6      # below this → no-op (cold start)
_TASTE_FULL_PAIRS = 30    # at/above this → full taste weight
_CANDIDATE_SCHEMA_VERSION = 2   # bump when the rated-round shape changes
_CONTRAST_AXES = {"lead", "harmony", "groove", "arrangement", "mixed"}
_TRAINABLE_MODELS_BY_SCOPE = {
    "lead": "lead_taste",
    "harmony": "harmony_future",
    "groove": "none",
    "arrangement": "none",
    "mixed": "none",
}


def _ratings_file_hash(path: str | None = None) -> str | None:
    p = path or _ratings_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except Exception:
        return None


def _taste_model() -> dict:
    p = _taste_model_path()
    if not os.path.exists(p):
        return {}
    model = _load_json(p)
    ratings_p = _ratings_path()
    if os.path.exists(ratings_p):
        curr_hash = _ratings_file_hash(ratings_p)
        curr_rounds = len(_load_ratings_file().get("candidate_rounds", []))
        stored_hash = model.get("ratings_sha256")
        stored_rounds = model.get("n_rounds")
        if stored_hash is not None and curr_hash != stored_hash:
            model["stale"] = True
            model["stale_reason"] = "stale model, retrain needed (ratings content hash mismatch)"
        elif stored_rounds is not None and curr_rounds != stored_rounds:
            model["stale"] = True
            model["stale_reason"] = "stale model, retrain needed (candidate rounds count mismatch)"
    elif model.get("ratings_sha256") is not None or model.get("n_rounds"):
        # Orphan model (review finding 5, 2026-08-16): the ratings file it was
        # trained from is gone entirely. The model is untraceable to any data —
        # it must not silently steer with full weight.
        model["stale"] = True
        model["stale_reason"] = "orphan taste model: ratings.json is missing but the model records training provenance"
    return model


def _ears_default() -> bool:
    """The Booth-promoted default for use_ears. Absent file => OFF (spec 6).
    A malformed file fails loud, not with a raw exception (fix round 1)."""
    path = os.path.join(os.path.dirname(_taste_model_path()), "ears_default.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return False
    except json.JSONDecodeError as exc:
        raise ValueError(
            "store/ears_default.json is malformed — fix or delete the promotion flag"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            "store/ears_default.json is malformed — fix or delete the promotion flag")
    value = data.get("use_ears_default")
    if not isinstance(value, bool):
        # bool() would enable the ears on truthy junk — the string "false", a 1,
        # a stray list all read as True. The promotion flag must be a real boolean
        # or the file is malformed and fails loud (F11).
        raise ValueError(
            "store/ears_default.json is malformed — fix or delete the promotion flag")
    return value


_EARS_DIRECTION_ALIASES = {
    # the gearbox speaks a wider dialect; only these words bend the gates
    "darker": "darker", "clubbier": "clubbier", "club": "clubbier",
    "sparser": "sparser", "sparse": "sparser", "weirder": "weirder",
}


def _apply_ears(result: dict, pool_key: str, *, role, use_ears, direction, moment,
                relax=None):
    """Shared ears hook: filter result[pool_key] through ears.serve. With ears
    off this NEVER touches the result — byte-identical behavior is a contract
    (and a ``relax`` request with ears off fails loud: there is no gate to relax)."""
    if use_ears is None:
        use_ears = _ears_default()
    if not use_ears:
        if relax:
            raise ValueError("relax needs use_ears — there is no gate to relax context-blind")
        return result
    import ears as _ears
    import moment as _moment_mod
    m = moment if moment is not None else _moment_mod.get_moment()
    served = _ears.serve(result.get(pool_key) or [], m, role=role,
                         direction=direction, relax=relax)
    result[pool_key] = served["served"]
    result["ears"] = served["ears"]
    result["gate_report"] = served["gate_report"]
    result["announcements"] = served["announcements"]
    result["ears_status"] = _ears.status_line(m, served["gate_report"],
                                              served["announcements"])
    return result


def _serve_indexed(result: dict, wrapped: list[dict], *, role, direction, moment,
                   relax=None):
    """Ears hook for pools whose items aren't serve-shaped candidates (bare MCP
    note-lists, MCP-format notes): serve an index-carrying wrapper pool, copy
    the four ears keys onto ``result``, and return the surviving indices so the
    caller realigns every parallel array. Callers only invoke this with ears ON."""
    probe = {"pool": wrapped}
    _apply_ears(probe, "pool", role=role, use_ears=True, direction=direction,
                moment=moment, relax=relax)
    for k in ("ears", "gate_report", "announcements", "ears_status"):
        result[k] = probe[k]
    return [w["i"] for w in probe["pool"]]


def _gate_backing(result: dict, m: dict) -> None:
    """F3b: keep a suggestion's backing from colliding with the room. For each
    surviving suggestion carrying a ``backing`` dict, drop the part (pad => role
    "harmony", bass => role "bass") whose register_band lands on a DIFFERENT
    occupied role's band, or whose own role is already playing (no second
    bassline), and announce it. Reuses the moment the serve already resolved so
    the room is read once; callers invoke this only on the ears-on path, and the
    ``announcements`` key is touched only when a part is actually dropped."""
    import moment as _moment_mod
    roles = m.get("roles") or {}
    for sug in result.get("suggestions", []):
        backing = sug.get("backing")
        if not backing:
            continue
        for part, part_role in (("pad", "harmony"), ("bass", "bass")):
            notes = backing.get(part)
            if notes is None:
                continue
            band = _moment_mod.register_band(_note_dicts(notes))
            register_hit = band is not None and any(
                r != part_role and info.get("register_band") == band
                for r, info in roles.items())
            if register_hit or part_role in roles:
                del backing[part]
                result.setdefault("announcements", []).append(
                    "left out the backing %s — yours is playing" % part)
        if not backing:
            del sug["backing"]


_BUILT_AT_CACHE: dict[str, Any] = {"mtime": None, "value": None}


def _index_built_at() -> str | None:
    """Index build stamp for cache keys: a rebuilt corpus busts the memo.
    The stamp file is re-read only when its mtime moved — parsing 30k
    phrases per lookup would cost more than the memo saves."""
    try:
        mtime = os.path.getmtime(INDEX_JSON)
    except OSError:
        return None
    if _BUILT_AT_CACHE.get("mtime") != mtime:
        try:
            meta = _load_artifact(INDEX_JSON, "phrase_index.json").get("meta") or {}
            value = meta.get("built_at")
        except Exception:
            value = None
        _BUILT_AT_CACHE.update(mtime=mtime, value=value)
    return _BUILT_AT_CACHE.get("value")


# Unseeded diversity hits, keyed (role, key, tempo, kind, k, built_at).
# Real-index path only: test pool overrides bypass it, so synthetic pools
# can never read production hits (or poison them). Bounded: Tempo floats
# make keys near-unique per call, so an overgrown memo is dropped whole.
_RETRIEVE_CACHE: dict[tuple, list[dict]] = {}
_RETRIEVE_CACHE_CAP = 512


def _retrieve_phrases(seed_notes=None, kind=None, k=6, key=None, tempo=None,
                      prefer_liked=False, role=None) -> list[dict]:
    """The one retrieval body behind find_phrases and find_phrases_with_ears.

    ``prefer_liked`` reranks by his ratings HERE, before any ears gate: the gates
    only filter and keep order, so his taste stays the only ranker."""
    role = _normalize_role(role)
    if kind == "melodic":
        kind = "harmonic"
    if kind is None:
        kind = "drum" if role == "drum" else "harmonic"
    if kind not in ("harmonic", "drum"):
        raise ValueError("kind must be 'melodic'/'harmonic' or 'drum'")
    phrases = _phrases()
    if role:   # restrict the pool BEFORE diversity/search so we actually return k of that role
        phrases = _filter_phrases(phrases, None, None, role=role)
    query_vec: list[float] | None = None

    if seed_notes:
        notes = _note_dicts(seed_notes)
        query_vec = drum_vector(notes) if kind == "drum" else phrase_vector(notes)
        hits = search(phrases, query_vec, k=k * 3, kind=kind, diversify=True, max_sim=0.97)
        hits = _filter_phrases(hits, key, tempo)[:k]
        results = [_phrase_result(p, cosine(query_vec, p["vec"])) for p in hits]
    else:
        # key/tempo BEFORE diversity, not after. _diverse_exemplars takes no seed
        # and no query — it is a deterministic function of the whole corpus, so
        # filtering its output meant every unseeded call drew from the same ~9
        # phrases forever and a tempo ask returned whichever of those nine
        # happened to land in the ±20 BPM window (often none).
        pool = _filter_phrases(phrases, key, tempo) if (key or tempo) else phrases
        if _PHRASE_POOL_OVERRIDE is None:
            memo_key = (role, key, tempo, kind, k, _index_built_at())
            hits = _RETRIEVE_CACHE.get(memo_key)
            if hits is None:
                hits = _diverse_exemplars(pool, k * 3, kind)[:k]
                if len(_RETRIEVE_CACHE) >= _RETRIEVE_CACHE_CAP:
                    _RETRIEVE_CACHE.clear()
                _RETRIEVE_CACHE[memo_key] = hits
        else:
            hits = _diverse_exemplars(pool, k * 3, kind)[:k]
        results = [_phrase_result(p) for p in hits]

    if prefer_liked:
        results = _rerank_by_ratings(results, query_vec, kind)
    return results[:k]


def find_phrases_with_ears(seed_notes=None, kind=None, k=6, key=None, tempo=None,
                            prefer_liked=False, role=None, use_ears=None,
                            direction=None, moment=None, relax=None):
    """Find phrases from the index, optionally filtered by ears.serve.

    With ears OFF this is identical to find_phrases (no side effects). With ears ON,
    survivors land in out['phrases'] and the ears metadata (gate_report, announcements, ears_status) rides on the returned dict.

    The ``moment`` argument is optional; if not provided, the current moment is read.
    The ``direction`` argument is optional; if not provided, the default direction
    is used. The ``role`` argument is optional; if not provided, the default role
    is used. The ``use_ears`` argument is optional; if not provided, the default
    value is read from store/ears_default.json (or False if the file is missing).
    """
    results = _retrieve_phrases(seed_notes=seed_notes, kind=kind, k=k, key=key,
                                tempo=tempo, prefer_liked=prefer_liked, role=role)
    if use_ears is None:
        # with_ears serves by design (HEAD behavior); only an explicit
        # use_ears=False opts out. Callers that want default-resolution do it
        # themselves before calling (see the MCP find_phrases wrapper).
        use_ears = True
    out = {"phrases": results}
    _apply_ears(out, "phrases", role=_normalize_role(role), use_ears=use_ears,
                direction=direction, moment=moment, relax=relax)
    out["phrases"] = out["phrases"][:k]
    return out


def _taste_features(notes: list[dict], role: str | None = None,
                    key: str | None = None) -> list[float]:
    """7 score_phrase dims + 'jumpiness' (leap fraction) + 'staticness' (repeat
    fraction) — the axes Ricardo names in his reasons ('too jumpy', 'too static').
    role/key are threaded into score_phrase so train- and serve-time features use the
    same (known) tonal interpretation — a rated round stores its key, and selection
    passes role='lead' + the known key, so the two never disagree."""
    from mcp_server.core import score_phrase  # noqa: PLC0415  (deferred: core imports this bridge at module load)
    sc = score_phrase(notes, role=role, key=key)["scores"]
    nd = _note_dicts(notes)
    top: dict = {}
    for n in sorted(nd, key=lambda x: x["start"]):
        slot = round(n["start"] * 4)
        if slot not in top or n["pitch"] > top[slot]["pitch"]:
            top[slot] = n
    sky = [top[k]["pitch"] for k in sorted(top)]
    ivs = [b - a for a, b in zip(sky, sky[1:])]
    jumpy = sum(1 for d in ivs if abs(d) > 4) / len(ivs) if ivs else 0.0
    static = sum(1 for d in ivs if d == 0) / len(ivs) if ivs else 0.0
    return [sc.get(k, 0.0) for k in _TASTE_FEATURE_NAMES[:7]] + [jumpy, static]


def _candidate_notes(candidate: Any) -> list[dict]:
    if isinstance(candidate, dict):
        return candidate.get("notes") or []
    return candidate


def _candidate_role(candidate: Any, round_role: str | None = None) -> str:
    if isinstance(candidate, dict):
        return str(candidate.get("role") or round_role or "lead").lower()
    return str(round_role or "lead").lower()


def _candidate_key(candidate: Any, round_key: str | None = None) -> str | None:
    if isinstance(candidate, dict):
        return candidate.get("key") or round_key
    return round_key


def _candidate_source(candidate: Any) -> str:
    if isinstance(candidate, dict):
        return str(candidate.get("source") or candidate.get("role") or "candidate").lower()
    return "legacy"


def _normalise_contrast_axis(value: str | None, candidates: list[Any] | None = None) -> str:
    axis = str(value or "").strip().lower()
    if axis in _CONTRAST_AXES:
        return axis
    sources = {_candidate_source(c) for c in (candidates or [])}
    sources.discard("legacy")
    if len(sources) == 1:
        source = next(iter(sources))
        if source in {"lead", "harmony", "groove", "arrangement"}:
            return source
    return "mixed"


def _rating_scope_for_contrast(contrast_axis: str, rating_scope: str | None = None) -> str:
    scope = str(rating_scope or "").strip().lower()
    if scope in _CONTRAST_AXES:
        return scope
    return _normalise_contrast_axis(contrast_axis)


def _trainable_model_for_scope(rating_scope: str, trainable_model: str | None = None) -> str:
    model = str(trainable_model or "").strip().lower()
    if model:
        return model
    return _TRAINABLE_MODELS_BY_SCOPE.get(rating_scope, "none")


def _candidate_taste_trainable(candidate: Any, round_role: str | None = None) -> bool:
    """Only lead-note candidates train the current lead taste model.

    Harmony/arrangement candidates are preserved as rated material but segregated until
    a dedicated harmony/arrangement feature model exists. Legacy note-list rounds remain
    trainable under the historical default role='lead'."""
    if isinstance(candidate, dict) and candidate.get("taste_trainable") is False:
        return False
    source = _candidate_source(candidate)
    if source in {"harmony", "arrangement", "structure", "songplan"}:
        return False
    return _candidate_role(candidate, round_role) == "lead"


def _normalise_rating_candidate(
    candidate: Any,
    *,
    index: int,
    key: str | None,
    role: str | None,
) -> dict:
    if isinstance(candidate, dict):
        out = dict(candidate)
        notes = out.get("notes") or []
    else:
        notes = candidate
        out = {
            "id": f"candidate-{index + 1}",
            "role": role or "lead",
            "source": role or "lead",
            "key": key,
            "notes": notes,
        }

    out["id"] = str(out.get("id") or f"candidate-{index + 1}")
    out["role"] = str(out.get("role") or role or "lead").lower()
    out["source"] = str(out.get("source") or out["role"] or "candidate").lower()
    if out.get("key") is None:
        out["key"] = key
    out["notes"] = notes
    out["taste_trainable"] = _candidate_taste_trainable(out, role)
    if out["taste_trainable"]:
        out["features"] = _taste_features(
            notes,
            role=out["role"],
            key=out.get("key"),
        )
        out["feature_names"] = list(_TASTE_FEATURE_NAMES)
    return out


def _candidate_features(candidate: Any, fallback: list[float] | None,
                        round_role: str | None, round_key: str | None) -> list[float]:
    if isinstance(candidate, dict):
        stored = candidate.get("features")
        if (stored and candidate.get("feature_names") == list(_TASTE_FEATURE_NAMES)
                and len(stored) == len(_TASTE_FEATURE_NAMES)):
            return stored
    if fallback and len(fallback) == len(_TASTE_FEATURE_NAMES):
        return fallback
    return _taste_features(
        _candidate_notes(candidate),
        role=_candidate_role(candidate, round_role),
        key=_candidate_key(candidate, round_key),
    )


def _valid_candidate_index(value: Any, n_candidates: int) -> int | None:
    try:
        idx = int(value)
    except (TypeError, ValueError):
        return None
    return idx if 0 <= idx < n_candidates else None


def _role_rounds(rounds: list[dict], role: str = "lead") -> list[dict]:
    """Filter candidate rounds by their declared role (defaulting missing role to 'lead')."""
    return [r for r in rounds if r.get("role", "lead") == role]


def _taste_pairs_from_rounds(rounds: list[dict]) -> list[tuple[list[float], list[float]]]:
    """best>others and others>worst preference pairs from each rated round."""
    pairs: list[tuple[list[float], list[float]]] = []
    for rnd in rounds:
        if rnd.get("trainable_model") not in {None, "lead_taste"}:
            continue
        cands = rnd.get("candidates", [])
        if len(cands) < 2:
            continue
        stored = rnd.get("features")
        r_role, r_key = rnd.get("role"), rnd.get("key")
        stored_ok = (
            stored and len(stored) == len(cands)
            and rnd.get("feature_names") == list(_TASTE_FEATURE_NAMES)
        )
        trainable = [_candidate_taste_trainable(c, r_role) for c in cands]
        b = _valid_candidate_index(rnd.get("best"), len(cands))
        w = _valid_candidate_index(rnd.get("worst"), len(cands))

        feature_cache: dict[int, list[float]] = {}

        def feat(i: int) -> list[float]:
            if i not in feature_cache:
                feature_cache[i] = _candidate_features(
                    cands[i],
                    stored[i] if stored_ok else None,
                    r_role,
                    r_key,
                )
            return feature_cache[i]

        for i in range(len(cands)):
            if b is not None and i != b and trainable[b] and trainable[i]:
                pairs.append((feat(b), feat(i)))
            if w is not None and i != w and i != b and trainable[i] and trainable[w]:
                pairs.append((feat(i), feat(w)))
    return pairs


def _taste_pairs_from_golds(cap: int) -> list[tuple[list[float], list[float]]]:
    """Gold anchors as TRAINING signal: each gold is a preference vote — 'this beats
    my typical material' — expanded into (gold, sampled-negative) pseudo-pairs.

    Negatives are deterministic per gold (hash-strided over the same-kind/role pool,
    junk-proxy-filtered so we teach 'gold vs typical', not 'gold vs junk' — the junk
    floor already knows the latter). Capped at ``cap`` so pseudo-pairs never outnumber
    his real best/worst calls; golds do NOT count toward the cold-start ramp
    (_taste_weight) — they steer direction, real pairs earn confidence."""
    if cap <= 0:
        return []
    golds = get_gold_phrases()
    if not golds:
        return []
    index_phrases = _phrases()
    idx_by_id = phrase_lookup(index_phrases)  # pid first, then alias
    gold_ids = expand_ids(
        {g.get("phrase_id") for g in golds if g.get("phrase_id")},
        index_phrases)
    pools: dict[tuple[str, str | None], list[dict]] = {}

    def _negatives(kind: str, role: str | None) -> list[dict]:
        if (kind, role) not in pools:
            # Distinct phrases once: a Save-As ostinato must not fill the
            # negative pool with copies of itself (the index is deduped at
            # build; this also covers pre-dedupe indexes).
            pools[(kind, role)] = dedupe_pool([
                p for p in index_phrases
                if p.get("kind") == kind and (role is None or p.get("role") == role)
                and p.get("id") not in gold_ids and p.get("n_notes", 0) >= 4
                and len({n["pitch"] for n in p.get("notes") or []}) >= 3
            ])
        return pools[(kind, role)]

    per_gold = max(1, min(3, cap // len(golds)))
    pairs: list[tuple[list[float], list[float]]] = []
    for g in sorted(golds, key=lambda r: str(r.get("phrase_id") or "")):
        pid = g.get("phrase_id")
        entry = idx_by_id.get(pid) or {}
        # the gold's OWN role first: a bass keep must draw its negatives from
        # the bass pool, or the augmentation teaches "bass lines are good leads"
        # and the accuracy guard below throws the whole thing away (it did).
        kind = str(g.get("kind") or entry.get("kind") or "harmonic")
        role = str(g.get("role") or entry.get("role") or "lead").lower()
        pool = _negatives(kind, role) or _negatives(kind, None)
        if not pool:
            continue
        gf = _taste_features(g["notes"], role=role,
                             key=_project_keys().get(entry.get("project", "")))
        h = int(hashlib.md5(str(pid or _clip_digest(g["notes"])).encode()).hexdigest()[:8], 16)
        for i in range(per_gold):
            neg = pool[(h + i * 7919) % len(pool)]
            pairs.append((gf, _taste_features(
                neg["notes"], role=role, key=_project_keys().get(neg.get("project", "")))))
            if len(pairs) >= cap:
                return pairs
    return pairs


def _taste_fit(pairs: list, gold_pairs: list, dim: int) -> tuple[list[float], int]:
    """THE shipped fit: real pairs + gold pseudo-pairs, guarded — if the augmented
    weights score WORSE on the real pairs than pairs-only weights, the augmentation
    is refused wholesale. Returns (weights, n_gold_used). Factored out so the LOO
    quality gate can audit the same model class that actually ships."""
    weights = taste.train(pairs + gold_pairs, dim)
    n_gold_used = len(gold_pairs)
    if pairs and gold_pairs:
        pairs_only = taste.train(pairs, dim)
        if taste.accuracy(pairs, weights) < taste.accuracy(pairs, pairs_only):
            weights, n_gold_used = pairs_only, 0
    return weights, n_gold_used


def _taste_loo_accuracy(pairs: list, gold_pairs: list, dim: int) -> float:
    """Leave-one-out accuracy on real preference pairs (cheap at n≲50).

    Each fold refits through ``_taste_fit`` — the SAME pairs+guarded-gold path the
    served model uses. Cross-validating a pairs-only model would grade a model
    nobody serves, so the gate could pass or fail on the wrong thing.

    Caveat: gold pseudo-pairs are NOT held out (they derive from the gold ledger,
    not from this pair list), so LOO reads optimistically where a held-out pair's
    material also appears among the golds. It bounds quality, it doesn't certify it.
    """
    if not pairs:
        return 0.0
    correct = 0
    for i in range(len(pairs)):
        weights, _ = _taste_fit(pairs[:i] + pairs[i + 1:], gold_pairs, dim)
        if taste.accuracy([pairs[i]], weights) == 1.0:
            correct += 1
    return correct / len(pairs)


def _retrain_taste() -> dict:
    """Train on real best/worst pairs + gold pseudo-pairs, guarded: if the augmented
    weights score WORSE on the real pairs than pairs-only weights, the augmentation is
    refused wholesale (n_gold_pairs reports 0). His direct A>B calls are the exam;
    golds may steer, never break it. train_accuracy is measured on real pairs only."""
    ratings_p = _ratings_path()
    rounds = _load_ratings_file().get("candidate_rounds", [])
    pairs = _taste_pairs_from_rounds(rounds)
    gold_pairs = _taste_pairs_from_golds(max(len(pairs), _TASTE_MIN_PAIRS))
    dim = len(_TASTE_FEATURE_NAMES)
    weights, n_gold_used = _taste_fit(pairs, gold_pairs, dim)
    exam = pairs or gold_pairs
    model = {
        "weights": weights,
        "feature_names": _TASTE_FEATURE_NAMES,
        "n_pairs": len(pairs),
        "n_gold_pairs": n_gold_used,
        "n_rounds": len(rounds),
        "ratings_sha256": _ratings_file_hash(ratings_p),
        "train_accuracy": round(taste.accuracy(exam, weights), 3),
    }
    if len(pairs) >= _TASTE_MIN_PAIRS:
        model["loo_accuracy"] = round(_taste_loo_accuracy(pairs, gold_pairs, dim), 3)
    _save_json_atomic(_taste_model_path(), model)
    return model


def _stale_retrain_report(exc: Exception) -> dict:
    """taste_retrained payload for a FAILED post-persistence retrain. The
    rating/round is already saved at every call site, so the failure must not
    raise: the judgment stands and the model goes stale until the next
    successful retrain."""
    return {"stale": True, "error": f"{type(exc).__name__}: {exc}"}


def _last_good_round_counts() -> dict:
    """rounds/pairs/train_accuracy from the persisted (last good) model, or
    all-None when no readable model exists — the counts a caller is actually
    served by while a fresh retrain keeps failing."""
    try:
        model = _taste_model()
    except Exception:
        model = {}
    return {"rounds": model.get("n_rounds"), "pairs": model.get("n_pairs"),
            "train_accuracy": model.get("train_accuracy")}


def _taste_weight() -> float:
    """Cold-start blend: 0 below _TASTE_MIN_PAIRS, ramps to 1.0 by _TASTE_FULL_PAIRS,
    scaled by leave-one-out quality when available."""
    model = _taste_model()
    if model.get("stale"):
        return 0.0
    n = model.get("n_pairs", 0)
    if n < _TASTE_MIN_PAIRS:
        return 0.0
    count_ramp = min(1.0, (n - _TASTE_MIN_PAIRS) / max(1, _TASTE_FULL_PAIRS - _TASTE_MIN_PAIRS))
    loo = model.get("loo_accuracy")
    quality = 1.0 if loo is None else (0.0 if loo <= 0.5 else min(1.0, (loo - 0.5) * 2))
    return count_ramp * quality


def _taste_score(notes: list[dict], role: str | None = None,
                 key: str | None = None) -> float:
    model = _taste_model()
    if model.get("stale"):
        return 0.0
    return taste.score(_taste_features(notes, role=role, key=key),
                       model.get("weights", []))


def rate_candidates(candidates: list[Any], best: int,
                    worst: int | None = None, reason: str | None = None,
                    key: str | None = None, role: str = "lead",
                    contrast_axis: str | None = None,
                    rating_scope: str | None = None,
                    trainable_model: str | None = None,
                    idempotency_key: str | None = None) -> dict:
    """Record one best/worst judgment over candidate phrases and retrain the taste
    model. Each round adds best>others and others>worst preference pairs.

    Pass the round's ``key`` (e.g. the audition's reported key) and ``role`` so the
    taste features are computed under the SAME known tonal interpretation at train time
    as at serve time (selection). audition_leads returns both to echo straight back."""
    if (not candidates or isinstance(best, bool) or not isinstance(best, int)
            or not (0 <= best < len(candidates))):
        raise ValueError("need candidates and a valid 'best' index")
    if worst is not None:
        if (isinstance(worst, bool) or not isinstance(worst, int)
                or not (0 <= worst < len(candidates)) or worst == best):
            raise ValueError("'worst' must be a valid index different from 'best'")
    # A round with no key trains scale_fit under a per-candidate SELF-DETECTED
    # key while serving passes the session's real one. scale_fit carries the
    # model's largest weight, so that skew corrupts the most influential
    # feature — and all 13 rounds recorded before 2026-09-03 carry key: null.
    # Derive it, or refuse; never store null again.
    key_source = "caller"
    if key is None:
        _cand_keys = {str(c.get("key")) for c in candidates
                      if isinstance(c, dict) and c.get("key")}
        if len(_cand_keys) == 1:
            key = _cand_keys.pop()
            key_source = "candidates"
        else:
            raise ValueError(
                "this round has no key — audition_leads / suggest_growth / "
                "grow_my_loop all return 'key'; echo it back, or pass key='auto' "
                "to record the detected one. Rating without a key trains "
                "scale_fit under a different tonal interpretation than serving "
                "uses.")
    elif str(key).lower() == "auto":
        _pc = [0.0] * 12
        for c in candidates:
            for n in _candidate_notes(c) or []:
                _pc[int(n["pitch"]) % 12] += 1.0
        if not any(_pc):
            raise ValueError("key='auto' needs candidates with notes to detect from")
        _t, _m, _ = estimate_key(_pc)
        key = f"{_t} {_m}"
        key_source = "auto"
    stored_candidates = [
        _normalise_rating_candidate(c, index=i, key=key, role=role)
        for i, c in enumerate(candidates)
    ]
    round_contrast_axis = _normalise_contrast_axis(contrast_axis, stored_candidates)
    round_rating_scope = _rating_scope_for_contrast(round_contrast_axis, rating_scope)
    round_trainable_model = _trainable_model_for_scope(
        round_rating_scope,
        trainable_model,
    )
    normalized_reason = ((reason or "").strip().lower() or None)
    idempotency_fingerprint = _idempotency_fingerprint({
        "kind": "candidate_round",
        # fingerprint over normalized note shapes so a retry that re-reads the
        # same material (e.g. 1 vs 1.0 after a JSON round-trip) still matches
        "candidates": [
            {**c, "notes": _note_dicts(c.get("notes") or [])}
            for c in stored_candidates
        ],
        "best": int(best),
        "worst": int(worst) if worst is not None else None,
        "reason": normalized_reason,
        "key": key,
        "role": role,
        "contrast_axis": round_contrast_axis,
        "rating_scope": round_rating_scope,
        "trainable_model": round_trainable_model,
    })
    if idempotency_key is None:
        # No caller key: derive one from the round's own fingerprint so a
        # retried identical round replays the stored one instead of appending
        # a duplicate. Same key lookup, same fingerprint check as the explicit
        # path — not a second mechanism; only a materially different round
        # records anew.
        idempotency_key = f"round:{idempotency_fingerprint}"
    with _ratings_file_lock():
        data = _load_ratings_file()
        rounds = data.setdefault("candidate_rounds", [])
        if idempotency_key:
            existing = next(
                (item for item in rounds if item.get("idempotency_key") == idempotency_key),
                None,
            )
            if existing is not None:
                _require_matching_idempotency(
                    existing, idempotency_fingerprint, idempotency_key,
                )
                # the round is already stored; a failed retrain downgrades to
                # last-good counts + a stale report, never a raised error
                try:
                    model = _retrain_taste()
                    counts = {"rounds": model.get("n_rounds", len(rounds)),
                              "pairs": model.get("n_pairs", 0),
                              "train_accuracy": model.get("train_accuracy")}
                    stale = None
                except Exception as exc:
                    counts = _last_good_round_counts()
                    stale = _stale_retrain_report(exc)
                weight_error = None
                if stale is None:
                    try:
                        weight = round(_taste_weight(), 2)
                    except Exception as exc:
                        weight = 0.0
                        weight_error = "%s: %s" % (type(exc).__name__, exc)
                else:
                    weight = 0.0
                out = {
                    "ok": True,
                    "idempotent": True,
                    **counts,
                    "taste_weight": weight,
                    # the replay carries the same key identity as a fresh
                    # round so callers can echo it back either way
                    "key": existing.get("key"),
                    "key_source": existing.get("key_source"),
                    "contrast_axis": existing.get("contrast_axis"),
                    "rating_scope": existing.get("rating_scope"),
                    "trainable_model": existing.get("trainable_model"),
                }
                if stale is not None:
                    out["taste_retrained"] = stale
                if weight_error is not None:
                    out["taste_weight_error"] = weight_error
                return out
        rounds.append({
            "schema_version": _CANDIDATE_SCHEMA_VERSION,
            "candidates": stored_candidates,
            "best": int(best),
            "worst": (int(worst) if worst is not None else None),
            "reason": normalized_reason,
            "key": key,
            "key_source": key_source,
            "role": role,
            "contrast_axis": round_contrast_axis,
            "rating_scope": round_rating_scope,
            "trainable_model": round_trainable_model,
            # round-level copy keeps legacy tooling simple; candidate-level features
            # are the authoritative additive schema for new structured candidates.
            "features": [c.get("features") for c in stored_candidates],
            "feature_names": list(_TASTE_FEATURE_NAMES),
            "idempotency_key": idempotency_key,
            "idempotency_fingerprint": idempotency_fingerprint,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        _save_ratings_file_unlocked(data)
        # the round is persisted above; a retrain crash must not raise or a
        # retry would record the same round twice — the round stands, the
        # model reports stale until the next successful retrain
        try:
            model = _retrain_taste()
            counts = {"rounds": model["n_rounds"], "pairs": model["n_pairs"],
                      "train_accuracy": model["train_accuracy"]}
            stale = None
        except Exception as exc:
            counts = _last_good_round_counts()
            stale = _stale_retrain_report(exc)
        weight_error = None
        if stale is None:
            try:
                weight = round(_taste_weight(), 2)
            except Exception as exc:
                weight = 0.0
                weight_error = "%s: %s" % (type(exc).__name__, exc)
        else:
            weight = 0.0
    out = {"ok": True, **counts,
           "taste_weight": weight,
           # say which key this round was recorded under, and where it came from
           "key": key, "key_source": key_source,
           "contrast_axis": round_contrast_axis,
           "rating_scope": round_rating_scope,
           "trainable_model": round_trainable_model}
    if stale is not None:
        out["taste_retrained"] = stale
    if weight_error is not None:
        out["taste_weight_error"] = weight_error
    return out


def _normalize_rating(rating: int | str) -> int:
    if isinstance(rating, str):
        r = rating.strip().lower()
        if r in ("up", "like", "+1", "1"):
            return 1
        if r in ("down", "dislike", "-1"):
            return -1
        if r in ("0", "neutral", "meh"):
            return 0
        raise ValueError("rating string must be 'up', 'down', or 'neutral'")
    if rating not in (-1, 0, 1):
        raise ValueError("rating must be -1, 0, or 1 ('up'/'down')")
    return int(rating)


def _rating_vectors(ratings: list[dict], kind: str) -> tuple[list[list[float]], list[list[float]]]:
    """Collect phrase vectors from up/down ratings for re-ranking."""
    up_vecs: list[list[float]] = []
    down_vecs: list[list[float]] = []
    by_id = phrase_lookup(_phrases())  # pid first, then alias
    for rec in ratings:
        label = str(rec.get("label") or "").lower()
        if label in _GOLD_UNMARK_LABELS or _is_soft_demotion(rec):
            continue
        vec: list[float] | None = None
        pid = rec.get("phrase_id")
        if pid and pid in by_id:
            p = by_id[pid]
            if p["kind"] == kind:
                vec = p["vec"]
        elif rec.get("notes"):
            nd = _note_dicts(rec["notes"])
            if nd:
                vec = drum_vector(nd) if kind == "drum" else phrase_vector(nd)
        if vec is None:
            continue
        if rec["rating"] > 0:
            up_vecs.append(vec)
        elif rec["rating"] < 0:
            down_vecs.append(vec)
    return up_vecs, down_vecs


def _rerank_by_ratings(
    results: list[dict],
    query_vec: list[float] | None,
    kind: str,
    *,
    w_up: float = 0.4,
    w_down: float = 0.4,
    down_drop: float = 0.97,
    exact_id_boost: float = 0.1,
) -> list[dict]:
    """Re-rank find_phrases hits using stored up/down preferences.

    final = base_cosine(seed) + w_up * max_sim(cand, up_set) - w_down * max_sim(cand, down_set)
    Drop candidates whose max similarity to a down-rated vector exceeds down_drop.
    """
    ratings = _load_ratings_file().get("ratings", [])
    up_vecs, down_vecs = _rating_vectors(ratings, kind)
    if not up_vecs and not down_vecs:
        return results

    index_phrases = _phrases()
    by_id = phrase_lookup(index_phrases)  # pid first, then alias
    rated_up_ids = expand_ids(
        {rec["phrase_id"] for rec in ratings
         if rec.get("phrase_id") and rec["rating"] > 0},
        index_phrases)
    scored: list[tuple[float, dict]] = []
    for r in results:
        p = by_id.get(r["id"])
        if not p:
            scored.append((r.get("similarity") or 0.0, r))
            continue
        base = r.get("similarity")
        if base is None and query_vec is not None:
            base = cosine(query_vec, p["vec"])
        elif base is None:
            base = 0.5
        up_sim = max((cosine(p["vec"], uv) for uv in up_vecs), default=0.0)
        down_sim = max((cosine(p["vec"], dv) for dv in down_vecs), default=0.0)
        if down_sim > down_drop:
            continue
        final = base + w_up * up_sim - w_down * down_sim
        if r["id"] in rated_up_ids:
            final += exact_id_boost
        out = dict(r)
        out["similarity"] = round(final, 3)
        scored.append((final, out))

    scored.sort(key=lambda x: x[0], reverse=True)
    ordered = [r for _, r in scored]
    # survival memory rides the rerank too: tried-and-abandoned slides back,
    # never out (neutral demote records never enter the reject set)
    return _demote_to_back(ordered, served_not_kept_identities())


# The index stamps the singular 'drum'; arrangement code speaks 'drums'. Accept both at the
# retrieval boundary so find_phrases(role="drums") stops silently returning nothing.
_ROLE_ALIASES = {"drums": "drum", "percussion": "drum"}


def _normalize_role(role: str | None) -> str | None:
    if role is None:
        return None
    low = str(role).lower()
    return _ROLE_ALIASES.get(low, low)


def _filter_phrases(cands: list[dict], key: str | None, tempo: float | None,
                    role: str | None = None) -> list[dict]:
    role = _normalize_role(role)
    if role is not None:
        cands = [p for p in cands if p.get("role") == role]
    if tempo is not None:
        cands = [p for p in cands if p.get("tempo") and abs(p["tempo"] - tempo) <= 20]
    if key is not None:
        pk = _project_keys()
        key_norm = key.strip().lower()
        cands = [p for p in cands if pk.get(p["project"], "").lower() == key_norm]
    return cands


def _vec_dot(a: list[float], b: list[float]) -> float:
    """Dot product with C-level multiplies in left-to-right order — the same
    value sum(x * y for x, y in zip(a, b)) computes, at a fraction of the
    generator overhead. The sampler below must match the naive version bit
    for bit, so no fsum reordering here."""
    return sum(map(_mul, a, b))


def _diverse_exemplars(phrases: list[dict], k: int, kind: str) -> list[dict]:
    """Pick k varied phrases without a seed query (max-min vector distance).

    Incremental farthest-point sampling: a running minima array holds each
    candidate's min cosine to the picked set and is updated against the
    NEWEST pick only — O(k·n) cosines instead of O(k²·n). The minima fold
    the same set the naive version re-scans every round (min is
    order-independent) with the same iteration order and strict comparison,
    so the picks are identical, only ~10× faster."""
    cands = [p for p in phrases if p["kind"] == kind]
    if not cands:
        return []
    cands.sort(key=lambda p: p["n_notes"], reverse=True)
    out = [cands[len(cands) // 2]]
    vecs = [p["vec"] for p in cands]
    first_vec = out[0]["vec"]
    mins = [2.0] * len(cands)
    newest = 0
    for i, p in enumerate(cands):
        if p is out[0] or p == out[0]:
            newest = i
        else:
            mins[i] = _vec_dot(vecs[i], first_vec)
    mins[newest] = 3.0  # the pick itself never re-competes (cosines ≤ 1)
    picked = {newest}
    while len(out) < k:
        best, best_sim = None, 2.0
        for i, p in enumerate(cands):
            if i in picked or p in out:
                continue
            if mins[i] < best_sim:
                best_sim = mins[i]
                best = i
        if best is None:
            break
        out.append(cands[best])
        picked.add(best)
        mins[best] = 3.0
        newest_vec = vecs[best]
        for i, p in enumerate(cands):
            if i in picked or p in out:
                continue
            sim = _vec_dot(vecs[i], newest_vec)
            if sim < mins[i]:
                mins[i] = sim
    return out


def _phrase_result(p: dict, sim: float | None = None) -> dict:
    return {
        "id": p["id"],
        "project": p["project"],
        "track": p["track"],
        "role": p.get("role"),
        "clip_source": p.get("clip_source"),
        "kind": p["kind"],
        "tempo": p.get("tempo"),
        "n_notes": p["n_notes"],
        "similarity": round(sim, 3) if sim is not None else None,
        "notes": p["notes"],
    }


_PHRASE_POOL_OVERRIDE: list[dict] | None = None


def _phrases() -> list[dict]:
    if _PHRASE_POOL_OVERRIDE is not None:
        return _PHRASE_POOL_OVERRIDE
    if "phrases" not in _CACHE:
        _CACHE["phrases"] = _load_artifact(INDEX_JSON, "phrase_index.json")["phrases"]
    return _CACHE["phrases"]


def _project_keys() -> dict[str, str]:
    """Map project stem -> key string from style_profile.json."""
    if "project_keys" not in _CACHE:
        keys = {}
        for p in _load_artifact(STYLE_JSON, "style_profile.json").get("projects", []):
            if p.get("has_harmonic") and p.get("key"):
                keys[p["name"]] = p["key"]
        _CACHE["project_keys"] = keys
    return _CACHE["project_keys"]


def _taste_model_summary() -> dict:
    """Human-readable state of the learned taste model: how much it's steering and
    which features it has learned to reward/penalize. Surfaced so the taste loop is
    visible (it used to be invisible — get_ratings only showed legacy up/down votes)."""
    model = _taste_model()
    weights = model.get("weights", [])
    names = model.get("feature_names", _TASTE_FEATURE_NAMES)
    paired = sorted(zip(names, weights), key=lambda nw: -abs(nw[1]))
    n_pairs = model.get("n_pairs", 0)
    if model.get("stale"):
        status = "stale model, retrain needed"
    elif n_pairs < _TASTE_MIN_PAIRS:
        status = f"cold-start (no steering until {_TASTE_MIN_PAIRS} pairs)"
    elif _taste_weight() >= 1.0:
        status = "fully active"
    else:
        status = "ramping in"
    out = {
        "status": status,
        # the learned weights rank-blend nowhere else: audition/suggest/grow
        # REPORT taste_scores but select without them ("taste_applied": False)
        "steering": "diagnostic — steers compose_from_profile only",
        "weight": round(_taste_weight(), 3),
        "n_pairs": n_pairs,
        "n_gold_pairs": model.get("n_gold_pairs", 0),
        "n_rounds": model.get("n_rounds", 0),
        "train_accuracy": model.get("train_accuracy"),
        "feature_weights": [{"feature": n, "weight": round(w, 3)} for n, w in paired],
    }
    if model.get("stale"):
        out["stale"] = True
        out["stale_reason"] = model.get("stale_reason", "stale model, retrain needed")
    return out


def get_ratings() -> dict:
    """Summary of stored phrase ratings AND the active taste loop (candidate rounds +
    learned taste-model state). Both are surfaced so nothing about how the model is
    steering selection stays hidden."""
    data = _load_ratings_file()
    ratings = data.get("ratings", [])
    counts = {"up": 0, "down": 0, "neutral": 0}
    for r in ratings:
        if r["rating"] > 0:
            counts["up"] += 1
        elif r["rating"] < 0:
            counts["down"] += 1
        else:
            counts["neutral"] += 1
    recent = [
        {k: v for k, v in rec.items() if k != "notes"}
        for rec in ratings[-10:]
    ]
    rounds = data.get("candidate_rounds", [])
    recent_rounds = [
        {k: v for k, v in rnd.items() if k != "candidates"}
        for rnd in rounds[-10:]
    ]
    return {
        "total": len(ratings),
        "counts": counts,
        "recent": recent,
        "candidate_rounds": {
            "total": len(rounds),
            "recent": recent_rounds,
        },
        "taste_model": _taste_model_summary(),
    }
