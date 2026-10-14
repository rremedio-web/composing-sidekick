"""The gearbox: six ways to answer Ricardo's loop, from pure recall to composition.

Gear 1 RECALL     — his nearest real phrase, verbatim (rebased only). Zero invention.
Gear 2 RECONTEXT  — answer_my_loop's transposed, voice-led answer (the default).
Gear 3 DEVELOP    — gear 2, then ONE named surgery on his notes (motif surgery).
Gear 4 GROW       — a composed continuation that sprouts from the seed (one rare move).
Gear 5 COMPOSE    — returns a CONTRACT, not notes: the LLM composes in-session
                    (sombra-brain loaded), then validate_composed() gates it with the
                    critics before anything is written.
Gear 6 COVER      — like 5, but the contract carries the seed's BONES (contour +
                    harmonic skeleton) and a target direction: self-translation.

Silence is legal at every gear: sparse, rest-heavy answers pass validation as long
as what remains is in contract. New module by design (core.py stops growing);
imports core one-way, never the reverse.
"""
from __future__ import annotations

import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402
from corpus_keys import phrase_lookup  # noqa: E402  (pid/alias/provenance id resolution)

# One module universe for the legacy bare import and the package import.  Keep
# this before callers can import and retain ``gears`` from either path.
for _alias in ("gears", "mcp_server.gears"):
    sys.modules.setdefault(_alias, sys.modules[__name__])

GEAR_NAMES = {1: "recall", 2: "recontext", 3: "develop", 4: "grow",
              5: "compose", 6: "cover"}

SURGERIES = ("ghost_answer", "bass_shadow", "broken_rhythm",
             "ending_variation", "octave_displacement")

# director axes: every gear 5/6 contract can carry an aesthetic target on each,
# -1.0 .. +1.0 (0 = his center of gravity). The LLM interprets; validation floors
# stay identical — axes steer taste, never bypass the critics.
DIRECTOR_AXES = ("faithful_alien", "sparse_maximal", "song_club",
                 "human_machine", "romantic_brutal", "clean_damaged",
                 "darker_brighter")

# validation floors for composed material (gears 5-6)
MIN_STYLE_SCORE = 0.45
MIN_IN_KEY_FRAC = 0.75
MAX_REGISTER_DRIFT = 14          # semitones from the seed median
MIN_DISTINCT_VELOCITIES = 3      # flat dynamics are an automatic reject


def _seed_context(seed_notes: list[dict], key: str | None):
    nd = core._normalize_phrase(core._note_dicts(seed_notes))
    if len(nd) < 3:
        raise ValueError("seed too short — play at least ~3 notes")
    if key:
        key_str = key
    else:
        pc_hist = [0.0] * 12
        for n in nd:
            pc_hist[n["pitch"] % 12] += 1
        t, m, _ = core.estimate_key(pc_hist)
        key_str = f"{t} {m}"
    median = sorted(n["pitch"] for n in nd)[len(nd) // 2]
    bars = max(1, math.ceil(core._phrase_span(nd) / core.BEATS_PER_BAR))
    return nd, key_str, median, bars


# --- gear 3 surgeries: his notes, reshaped (velocity-conscious by construction) ---

def _ghost_answer(nd: list[dict]) -> list[dict]:
    """The phrase echoes itself an octave up, half a bar late, at half strength."""
    ghost = [{**n, "pitch": min(127, n["pitch"] + 12),
              "start": n["start"] + 2.0,
              "velocity": max(1, int(n["velocity"] * 0.55))} for n in nd]
    return sorted(nd + ghost, key=lambda n: (n["start"], n["pitch"]))


def _bass_shadow(nd: list[dict]) -> list[dict]:
    """Strong-beat notes only, dropped into the bass register — the line's shadow."""
    strong = [n for n in nd if abs((n["start"] % 1.0)) < 1e-6]
    if len(strong) < 2:
        strong = nd[::2]
    return [{**n, "pitch": max(24, n["pitch"] - 24),
             "duration": max(n["duration"], 0.5)} for n in strong]


def _broken_rhythm(nd: list[dict], rng: random.Random) -> list[dict]:
    """Same pitches, rhythm broken: offbeat onsets displaced by a 16th, seeded."""
    out = []
    for n in nd:
        m = dict(n)
        if abs(m["start"] % 1.0) > 1e-6 and rng.random() < 0.6:
            m["start"] = max(0.0, m["start"] + rng.choice((-0.25, 0.25)))
        out.append(m)
    return sorted(out, key=lambda n: (n["start"], n["pitch"]))


def _ending_variation(nd: list[dict], key_str: str) -> list[dict]:
    """His dark landing: force the final note onto b7→1 territory (ends on the tonic)."""
    if not nd:
        return []
    tonic, _mode = core._parse_key_string(key_str)
    tonic_idx = core.PITCH_NAMES.index(tonic)
    out = [dict(n) for n in nd]
    last = max(range(len(out)), key=lambda i: (out[i]["start"], out[i]["pitch"]))
    p = out[last]["pitch"]
    candidates = [c for c in range(p - 11, p + 12) if (c - tonic_idx) % 12 == 0]
    out[last]["pitch"] = min(candidates, key=lambda c: abs(c - p))
    out[last]["duration"] = max(out[last]["duration"], 1.0)
    return out


def apply_surgery(nd: list[dict], name: str, key_str: str, seed: int = 0) -> list[dict]:
    rng = random.Random(f"surgery:{name}:{seed}")
    if name == "ghost_answer":
        return _ghost_answer(nd)
    if name == "bass_shadow":
        return _bass_shadow(nd)
    if name == "broken_rhythm":
        return _broken_rhythm(nd, rng)
    if name == "ending_variation":
        return _ending_variation(nd, key_str)
    if name == "octave_displacement":
        return core._octave_variant(nd, 12)
    raise ValueError(f"surgery must be one of {SURGERIES}, got {name!r}")


def _ears_regate_final(ans: dict, *, role: str = "lead", use_ears: bool | None = None,
                       direction: str | None = None, moment: dict | None = None,
                       relax: str | None = None) -> dict:
    """Re-admit FINAL answer_notes after gear transforms.

    Gears 1 and 3 historically gated an intermediate (transposed / pre-surgery)
    then returned different notes while still claiming ears=live. The room must
    see the notes that would be written — fail closed on empty survivors.
    """
    if ans.get("no_fit"):
        return ans
    want = use_ears if use_ears is not None else core._ears_default()
    if not want:
        return ans
    import ears as _ears
    import moment as _moment_mod
    m = moment if moment is not None else _moment_mod.get_moment()
    notes = core._note_dicts(ans.get("answer_notes") or [])
    if not notes:
        return ans
    resolved = None
    if direction:
        resolved = core._EARS_DIRECTION_ALIASES.get(str(direction).lower())
    cand = {
        "id": (ans.get("source_phrase_ids") or ["final"])[0],
        "notes": notes,
        "role": role,
        "key": ans.get("key"),
        "tempo": ans.get("tempo"),
    }
    served = _ears.serve([cand], m, role=role, direction=resolved, relax=relax)
    ans["ears"] = served["ears"]
    ans["gate_report"] = served["gate_report"]
    prior = list(ans.get("announcements") or [])
    fresh = list(served.get("announcements") or [])
    # keep prior gear announcements; append final-gate notes
    merged = prior + [a for a in fresh if a not in prior]
    ans["announcements"] = merged
    ans["ears_status"] = _ears.status_line(m, served["gate_report"], fresh)
    if served["ears"] == "live" and not served["served"]:
        ans["no_fit"] = True
        ans["answer_notes"] = []
        ans["transforms_applied"] = list(ans.get("transforms_applied") or []) + [
            "ears:refused_final"
        ]
    return ans


# --- the gearbox ---------------------------------------------------------------

def answer_with_gear(seed_notes: list[dict], gear: int = 2, key: str | None = None,
                     tempo: float | None = None, surgery: str | None = None,
                     direction: str | None = None, axes: dict | None = None,
                     seed: int = 0, *, use_ears: bool | None = None,
                     moment: dict | None = None, role: str = "lead",
                     relax: str | None = None) -> dict:
    """Route an answer through the chosen gear. Gears 1-4 return notes; gears 5-6
    return a CONTRACT for in-session LLM composition (validate with
    validate_composed before writing). Deterministic where machines make notes.
    use_ears/moment/relax ride through to answer_my_loop (gears 1-3) for
    room-aware serving; off by default, so ears-off answers stay byte-identical.
    ``role`` ('lead' or 'bass') picks the bank and the register the answer
    lives in — a bass riff is answered as a bass line."""
    if gear not in GEAR_NAMES:
        raise ValueError(f"gear must be 1-6, got {gear}")
    if gear >= 4:
        import rituals
        active_until = rituals.no_new_notes_active()
        if active_until:
            raise ValueError(
                f"NO_NEW_NOTES is active until {active_until}: gears 4-6 refuse "
                "(the track already contains its solution). Gears 1-3 work; "
                "no_new_notes(0) lifts the mode early.")
    nd, key_str, median, bars = _seed_context(seed_notes, key)
    key_from_live = False
    if gear >= 5 and key is None and moment and moment.get("scale_pcs") and moment.get("key"):
        # the Set's own scale outranks a four-note key guess for the contract
        key_str = str(moment["key"])
        key_from_live = True

    if gear == 1:
        ans = core.answer_my_loop(seed_notes, key=key_str, tempo=tempo,
                                  transform=None, seed=seed, use_ears=use_ears,
                                  direction=direction, moment=moment,
                                  role=role, relax=relax)
        if ans.get("no_fit"):
            ans["gear"] = 1
            return ans
        # recall = the retrieved phrase VERBATIM: strip recontext by re-fetching
        # the source phrase's raw notes
        pid = ans["source_phrase_ids"][0]
        src = phrase_lookup(core._phrases()).get(pid)
        if src:
            raw = sorted(core._note_dicts(src["notes"]), key=lambda n: (n["start"], n["pitch"]))
            t0 = raw[0]["start"] if raw else 0.0
            ans["answer_notes"] = core._notes_to_mcp(
                [{**n, "start": n["start"] - t0} for n in raw])
            ans["transforms_applied"] = ["verbatim"]
        ans = _ears_regate_final(ans, role=role, use_ears=use_ears,
                                 direction=direction, moment=moment, relax=relax)
        ans["gear"] = 1
        return ans

    if gear == 2:
        ans = core.answer_my_loop(seed_notes, key=key_str, tempo=tempo, seed=seed,
                                  use_ears=use_ears, direction=direction, moment=moment,
                                  role=role, relax=relax)
        ans["gear"] = 2
        return ans

    if gear == 3:
        chosen = surgery or random.Random(f"gear3:{seed}").choice(SURGERIES)
        ans = core.answer_my_loop(seed_notes, key=key_str, tempo=tempo, seed=seed,
                                  use_ears=use_ears, direction=direction, moment=moment,
                                  role=role, relax=relax,
                                  move=f"surgery:{chosen}")
        if ans.get("no_fit"):
            ans["gear"] = 3
            return ans
        cut = apply_surgery(core._note_dicts(ans["answer_notes"]), chosen, key_str, seed)
        ans["answer_notes"] = core._notes_to_mcp(
            sorted(cut, key=lambda n: (n["start"], n["pitch"])))
        ans["transforms_applied"] = ans.get("transforms_applied", []) + [f"surgery:{chosen}"]
        ans = _ears_regate_final(ans, role=role, use_ears=use_ears,
                                 direction=direction, moment=moment, relax=relax)
        ans["gear"] = 3
        return ans

    if gear == 4:
        if role != "lead":
            raise ValueError("gear 4 GROW composes a lead continuation — role must be 'lead'")
        grown = core.grow_my_loop(seed_notes, n=1, key=key_str,
                                  tempo=tempo or 120.0, seed=seed,
                                  use_ears=use_ears, moment=moment)
        sug = grown["suggestions"][0]
        cont = core._note_dicts(sug["continuation"])
        t0 = min((n["start"] for n in cont), default=0.0)
        return {
            "gear": 4,
            "answer_notes": core._notes_to_mcp([{**n, "start": n["start"] - t0} for n in cont]),
            "source_phrase_ids": [],
            "transforms_applied": ["grow:" + sug["the_move"]],
            "provenance_note": sug["why"],
            "key": key_str, "tempo": tempo or 120.0,
            "bars": max(1, math.ceil(core._phrase_span(cont) / core.BEATS_PER_BAR)),
            "assumptions": [f"gear 4 continuation composed by the growth engine "
                            f"({sug['dimension']})"],
            # Honesty: grow gates backing (F3b) but not the composed continuation —
            # do not imply ears=live on notes the room never admitted.
            "ears": "not_gated",
            "announcements": [
                "ears: not_gated (gear 4 continuation — backing may be gated; "
                "continuation is composed, not re-admitted)"
            ],
        }

    # gears 5-6: contracts for the LLM tier
    bad_axes = set(axes or {}) - set(DIRECTOR_AXES)
    if bad_axes:
        raise ValueError(f"unknown director axes {sorted(bad_axes)}; "
                         f"valid: {DIRECTOR_AXES}")
    contract = {
        "gear": gear,
        "mode": GEAR_NAMES[gear],
        "key": key_str,
        "bars": bars,
        "axes": {k: max(-1.0, min(1.0, float(v))) for k, v in (axes or {}).items()},
        "register": {"median": median, "max_drift_semitones": MAX_REGISTER_DRIFT},
        "role": role,
        "tempo": tempo,
        "validation": {"min_style_score": MIN_STYLE_SCORE,
                       "min_in_key_frac": MIN_IN_KEY_FRAC,
                       "min_distinct_velocities": MIN_DISTINCT_VELOCITIES},
        "silence_is_legal": True,
        "instructions": ("load the sombra-brain skill; compose in HIS grammar; then call "
                         "write_composed_to_jam(notes, contract, name, confirm=False) — "
                         "it runs validate_composed and only writes what passes"),
    }
    if moment and moment.get("scale_pcs"):
        # the room's scale (Live's own setting) is the in-key set the critics use
        contract["scale_pcs"] = sorted(int(p) % 12 for p in moment["scale_pcs"])
    if key_from_live:
        contract["assumptions"] = ["key %s from Live's scale setting" % key_str]
    if gear == 6:
        sky = {}
        for n in nd:
            slot = round(n["start"] * 2) / 2
            if slot not in sky or n["pitch"] > sky[slot]:
                sky[slot] = n["pitch"]
        contract["bones"] = {
            "contour": [sky[k] for k in sorted(sky)],
            "onset_halfbeats": [k for k in sorted(sky)],
            "direction_target": direction or "darker",
        }
        contract["direction_exemplars"] = _direction_exemplars(direction or "darker")
        contract["instructions"] = ("COVER: keep the bones (contour shape + onset "
                                    "skeleton), re-speak everything else in the target "
                                    "direction's language; then validate_composed")
    return contract


_DIRECTION_POLES = {
    "darker": ("darker_brighter", "neg_pole"), "brighter": ("darker_brighter", "pos_pole"),
    "club": ("song_club", "pos_pole"), "clubbier": ("song_club", "pos_pole"),
    "song": ("song_club", "neg_pole"),
    "sparse": ("sparse_maximal", "neg_pole"), "sparser": ("sparse_maximal", "neg_pole"),
    "maximal": ("sparse_maximal", "pos_pole"),
}


def _direction_exemplars(direction: str) -> list[str]:
    """COVER translation targets: his own projects at the requested pole
    (store/directions.json, regenerable; empty list when absent or unmapped —
    'weirder' and the runtime axes stay LLM-interpreted)."""
    axis_pole = _DIRECTION_POLES.get(str(direction).lower())
    if not axis_pole:
        return []
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "store", "directions.json")
    try:
        import json
        with open(path) as fh:
            ex = json.load(fh)["exemplars"]
        return ex[axis_pole[0]][axis_pole[1]]
    except (OSError, KeyError, ValueError):
        return []


def judge_answer(verdict: str, source_phrase_id: str | None = None,
                 answer_notes: list[dict] | None = None,
                 reason: str | None = None, *,
                 seed_notes: list[dict] | None = None,
                 key: str | None = None,
                 role: str | None = None,
                 idempotency_key: str | None = None,
                 entry_id: str | None = None,
                 _idempotency_key: str | None = None,
                 ears_session_path: str | None = None,
                 develop_session_path: str | None = None,
                 ledger_path: str | None = None,
                 source: str | None = None) -> dict:
    """KEEP/DROP, the ratings half (the live clip-delete half lives in live_write and
    is confirm-gated at the server). KEEP golds the SOURCE phrase (index notes when
    the id resolves, else the answer's notes). DROP records an explicit negative
    with the reason — the 'learn why' the taste loop needs. Never touches taste
    training directly; ratings stay the append-only ledger.

    Also the ears' session-memory WRITER (spec §4): every verdict buckets the
    answer's notes and feeds ears.note_verdict, so 3 straight same-bucket DROPs
    demote that bucket for the rest of the session — the ONLY place that
    happens, since the ears' serving path only ever READS session state. A
    DROP reason naming 'static'/'jumpy'/'wrong register' votes that bucket
    twice. A DROP of a moved answer bans only the (source, move) pairing.
    ears_session_path overrides the real session file for isolated callers
    (selftests); production callers leave it at the real default."""
    verdict = str(verdict).strip().lower()
    if verdict not in ("keep", "drop"):
        raise ValueError("verdict must be 'keep' or 'drop'")
    if idempotency_key is not None and _idempotency_key is not None \
            and idempotency_key != _idempotency_key:
        raise ValueError("conflicting idempotency keys")
    idempotency_key = idempotency_key or _idempotency_key
    if idempotency_key is None and entry_id:
        idempotency_key = f"judge:{entry_id}:{verdict}"
    import ledger as _ledger
    known_entry = None
    if entry_id:
        known_entry = next((entry for entry in _ledger.load(ledger_path).get("entries", [])
                            if entry.get("id") == entry_id), None)
    event_id = idempotency_key if idempotency_key is not None else (
        f"judge:{entry_id}:{verdict}" if known_entry is not None else None)
    existing_event = (_ledger.judgment_event(event_id, path=ledger_path)
                      if event_id is not None else None)
    stored_payload = (existing_event or {}).get("payload") or {}
    judged_snapshot = ((known_entry or {}).get("judged_snapshot")
                       if known_entry and known_entry.get("immediate_verdict") is not None
                       else None)
    def _contradicts(field: str, supplied, stored) -> bool:
        """Whether a supplied judgment field contradicts the verified write.
        Roles compare through learning_role: synonyms fold ('drums' IS
        'drum', 'percussion' IS 'drum') and the 'unknown' placeholder —
        supplied or stored — compares as no role at all."""
        if field == "role":
            supplied = _ledger.learning_role(supplied)
            stored = _ledger.learning_role(stored)
        if supplied is None or stored is None:
            return False
        if field == "source_phrase_id":
            # a rebuild may have moved the stored id to the pid while the
            # caller (e.g. the pedal listener) still holds the old alias
            return not _ledger._same_phrase_id(supplied, stored)
        return supplied != stored

    authoritative = dict(known_entry or {})
    if judged_snapshot:
        authoritative.update(judged_snapshot)
        # Keep the legacy validation block below pointed at the same immutable
        # view; the live row may now contain a later revision.
        known_entry = dict(known_entry)
        known_entry.update(judged_snapshot)
        for field, supplied in (("role", role), ("key", key),
                                ("source_phrase_id", source_phrase_id),
                                ("source", source)):
            if _contradicts(field, supplied, authoritative.get(field)):
                raise ValueError("judgment %s contradicts the verified write" % field)
        if answer_notes is not None and authoritative.get("answer_notes") is not None and \
                _ledger._normalize_answer_notes(answer_notes) != authoritative["answer_notes"]:
            raise ValueError("judgment answer_notes contradicts the verified write")
        answer_notes = authoritative.get("answer_notes")
        source_phrase_id = authoritative.get("source_phrase_id")
        # a real stored role wins (the check above made them agree); the
        # 'unknown' placeholder never does — a supplied role beats it,
        # exactly as on the unjudged path
        stored_role = _ledger.learning_role(authoritative.get("role"))
        role = stored_role if stored_role is not None else _ledger.learning_role(role)
        key = authoritative.get("key")
        source = authoritative.get("source")
    elif known_entry is not None:
        if answer_notes is None:
            answer_notes = known_entry.get("answer_notes")
        if role is None:
            role = _ledger.learning_role(known_entry.get("role"))
        if key is None:
            key = known_entry.get("key")
        if source_phrase_id is None:
            source_phrase_id = known_entry.get("source_phrase_id")
        if source is None:
            source = known_entry.get("source")
    # The heard move rides from the ledger row (stamped at write time), not
    # from the caller: a DROP of a moved answer bans the (source, move)
    # pairing, never the untouched source. Rows stamped before moves existed
    # carry None and keep the global ban.
    move = None
    if judged_snapshot:
        move = authoritative.get("move")
    elif known_entry is not None:
        move = known_entry.get("move")
    # Replays may omit optional arguments, but an explicitly changed argument
    # remains part of the event fingerprint and is rejected by begin_judgment.
    if stored_payload.get("reason") is not None and reason is None:
        reason = stored_payload["reason"]
    if stored_payload.get("seed_notes") is not None and seed_notes is None:
        seed_notes = stored_payload["seed_notes"]
    if stored_payload.get("role") is not None and role is None:
        role = stored_payload["role"]
    if stored_payload.get("key") is not None and key is None:
        key = stored_payload["key"]
    if stored_payload.get("source") is not None and source is None:
        source = stored_payload["source"]
    if stored_payload.get("source_phrase_id") is not None and source_phrase_id is None:
        source_phrase_id = stored_payload["source_phrase_id"]
    if stored_payload.get("answer_notes") is not None and answer_notes is None:
        answer_notes = stored_payload["answer_notes"]
    if stored_payload.get("move") is not None and move is None:
        move = stored_payload["move"]

    # F4: capture what the ears actually HEARD (the caller's answer_notes) before
    # any source resolution — session memory must learn the heard answer, not the
    # resolved source phrase (which the rating separately golds).
    heard_notes = answer_notes
    notes = answer_notes
    legacy_source = False
    if source_phrase_id:
        src = phrase_lookup(core._phrases()).get(source_phrase_id)
        if src:
            # Preserve the serving role when the caller omitted it.  In
            # particular, a legacy bass source must not be silently recast as
            # lead merely because the old caller had no explicit role field.
            if role is None:
                role = src.get("role")
            # A legacy caller may only have the source id.  It is safe to use
            # that source as the heard material, but make the provenance explicit
            # instead of pretending it was a verified write snapshot.
            if not notes:
                notes = src["notes"]
                legacy_source = True
    if not notes:
        raise ValueError("need source_phrase_id resolvable in the index, or answer_notes")
    if heard_notes is None:
        # only a source id was given: the heard material IS that source phrase
        heard_notes = notes

    if known_entry is not None:
        if known_entry.get("immediate_verdict") is not None and \
                known_entry.get("immediate_verdict") != verdict:
            raise ValueError("judgment verdict contradicts the existing immutable verdict")
        if answer_notes is not None and known_entry.get("answer_notes") is not None:
            if _ledger._normalize_answer_notes(answer_notes) != known_entry["answer_notes"]:
                raise ValueError("judgment answer_notes contradict the verified write")
        for field, supplied in (("role", role), ("key", key),
                                ("source_phrase_id", source_phrase_id),
                                ("source", source)):
            if _contradicts(field, supplied, known_entry.get(field)):
                raise ValueError("judgment %s contradicts the verified write" % field)
    event_payload = {
        "entry_id": entry_id, "verdict": verdict,
        "source_phrase_id": source_phrase_id, "answer_notes": core._note_dicts(notes),
        "heard_notes": core._note_dicts(heard_notes), "reason": reason,
        "role": role, "key": key, "source": source, "move": move,
        "seed_notes": (core._note_dicts(seed_notes) if seed_notes is not None else None),
    }
    event_fingerprint = _ledger._event_fingerprint(event_payload)
    event = None
    if event_id is not None:
        event = _ledger.begin_judgment(event_id, event_payload,
                                       entry_id=entry_id, path=ledger_path)
        # After a phrase-id migration the retry's own digest differs from the
        # one the sinks saved; begin_judgment matched it alias-aware and hands
        # back the stored digest, which is the one the sinks must compare.
        event_fingerprint = event.get("fingerprint") or event_fingerprint
        if event.get("status") == "complete" and event.get("result"):
            return dict(event["result"])

    def _mark_failed(step: str, exc: Exception) -> None:
        if event_id:
            try:
                _ledger.mark_judgment_step(
                    event_id, step, status="failed",
                    error=f"{type(exc).__name__}: {exc}", path=ledger_path)
            except Exception:
                pass

    import ears  # lazy: keeps gears.py importable without the moment/Live deps
    session_path = ears_session_path or ears.SESSION_PATH
    ledger_session_id = (known_entry or {}).get("session_id")
    if known_entry is None:
        try:
            live_sessions = _ledger._live_sessions(
                _ledger.load(ledger_path), _ledger._now())
            if live_sessions:
                ledger_session_id = max(
                    live_sessions,
                    key=lambda session: session.get("last_activity", 0)
                ).get("id")
        except Exception:
            ledger_session_id = None
    # F5: do the COMPLETE ears session read-modify-write BEFORE the rating persists.
    # A corrupt session file then blocks the verdict with nothing written, so a
    # retry can't duplicate a rating that already landed. F9a: under a lock.
    ears_step = (event or {}).get("steps", {}).get("ears")
    if ears_step and ears_step.get("status") == "applied":
        ears_announcements = list(ears_step.get("result") or [])
    else:
        try:
            ears_announcements = ears.record_verdict(
                {"notes": core._note_dicts(heard_notes)}, kept=(verdict == "keep"),
                path=session_path, event_id=event_id,
                conflict_fingerprint=event_fingerprint, reason=reason,
                ledger_session_id=ledger_session_id)
        except Exception as exc:
            _mark_failed("ears", exc)
            raise
        if event_id:
            _ledger.mark_judgment_step(event_id, "ears", result=ears_announcements,
                                       path=ledger_path)

    import develop_session  # lazy: sticky develop joins the pre-rating section
    dev_path = develop_session_path or develop_session.STATE_PATH
    dev_step = (event or {}).get("steps", {}).get("develop")
    if dev_step and dev_step.get("status") == "applied":
        dev_state = dev_step.get("result") or {}
    elif verdict == "keep" and source_phrase_id and seed_notes is not None:
        try:
            dev_state = develop_session.record_keep(
                source_phrase_id, seed_notes, key, path=dev_path,
                event_id=event_id, conflict_fingerprint=event_fingerprint)
        except Exception as exc:
            _mark_failed("develop", exc)
            raise
        if event_id:
            _ledger.mark_judgment_step(event_id, "develop", result=dev_state,
                                       path=ledger_path)
    elif verdict == "drop":
        try:
            dev_state = develop_session.record_drop(
                path=dev_path, event_id=event_id,
                conflict_fingerprint=event_fingerprint)
        except Exception as exc:
            _mark_failed("develop", exc)
            raise
        if event_id:
            _ledger.mark_judgment_step(event_id, "develop", result=dev_state,
                                       path=ledger_path)
    else:
        dev_state = develop_session.load_state(dev_path)
        if event_id:
            _ledger.mark_judgment_step(event_id, "develop", result=dev_state,
                                       path=ledger_path)

    rating_step = (event or {}).get("steps", {}).get("rating")
    if rating_step and rating_step.get("status") == "applied":
        rec = {"record": rating_step.get("result") or {}}
        result = ({"verdict": "keep", "golded": source_phrase_id or "answer notes",
                   "record": rec["record"]}
                  if verdict == "keep" else
                  {"verdict": "drop", "record": rec["record"],
                   "note": "negative logged; clip deletion is the server's confirm-gated half"})
    elif verdict == "keep":
        try:
            rec = core.rate(rating=1, label="gold", phrase_id=source_phrase_id,
                            notes=notes, comment=reason, role=role, key=key,
                            idempotency_key=idempotency_key,
                            entry_id=entry_id,
                            gear=(known_entry or {}).get("gear"),
                            source=source)
            if move:
                core.rate(rating=1, label="kept", phrase_id=source_phrase_id,
                          notes=notes, comment=reason, role=role, key=key,
                          idempotency_key=(idempotency_key + ":pairing-keep"
                                           if idempotency_key else None),
                          entry_id=entry_id, move=move)
        except Exception as exc:
            _mark_failed("rating", exc)
            raise
        if event_id:
            _ledger.mark_judgment_step(event_id, "rating", result=rec["record"],
                                       path=ledger_path)
        result = {"verdict": "keep", "golded": source_phrase_id or "answer notes",
                  "record": rec["record"]}
        if rec.get("taste_retrained"):
            # a failed post-persistence retrain is reported stale, never lost
            result["taste_retrained"] = rec["taste_retrained"]
    else:
        try:
            rec = core.rate(rating=-1, label="dropped", phrase_id=source_phrase_id,
                            notes=notes, comment=reason, role=role, key=key,
                            idempotency_key=idempotency_key, move=move)
        except Exception as exc:
            _mark_failed("rating", exc)
            raise
        if event_id:
            _ledger.mark_judgment_step(event_id, "rating", result=rec["record"],
                                       path=ledger_path)
        result = {"verdict": "drop", "record": rec["record"],
                  "note": "negative logged; clip deletion is the server's confirm-gated half"}
        if rec.get("taste_retrained"):
            result["taste_retrained"] = rec["taste_retrained"]

    if legacy_source:
        result["legacy_source"] = source_phrase_id
        result["note"] = (
            "heard notes came from legacy source_phrase_id %s; pass answer_notes "
            "for an exact verified-write snapshot" % source_phrase_id)
    if entry_id:
        result["entry_id"] = entry_id

    result["ears_announcements"] = ears_announcements
    if dev_state.get("sticky") and dev_state.get("hold"):
        _hold = dev_state["hold"]
        import develop_session as _ds
        _move = _ds.current_move(dev_state)
        result["sticky_develop"] = (
            "hold on — holding your idea, pass %d (%s)"
            % (_hold["pass"], _move))
    if event_id:
        result["event_id"] = event_id
        _ledger.complete_judgment(event_id, result, path=ledger_path)
    return result


def validate_composed(notes: list[dict], contract: dict, *,
                      role: str | None = None) -> dict:
    """The critics' gate for LLM-composed material (gears 5-6). Returns
    {passes, checks, score} — nothing should be written unless passes is True.
    ``role`` picks the grammar the style score is taken under (default: the
    contract's role, else lead)."""
    role = role or contract.get("role") or "lead"
    nd = core._note_dicts(notes)
    checks: dict[str, bool] = {}
    if not nd:
        return {"passes": False, "checks": {"non_empty": False},
                "reason": "no notes (if silence is the answer, write a sparse phrase, "
                          "not an empty one)"}
    key_str = contract["key"]
    tonic, mode = core._parse_key_string(key_str)
    scale_pcs = contract.get("scale_pcs")
    scale = (set(int(p) % 12 for p in scale_pcs) if scale_pcs
             else core.diatonic_set(core.PITCH_NAMES.index(tonic), mode))
    # The thresholds are the SERVER'S, not the caller's. A contract arrives from
    # whoever is composing, so reading limits out of it let the thing being judged
    # set its own pass mark — the selftest zeroed min_style_score, which is exactly
    # how easy that was. A contract may still TIGHTEN a limit, never loosen one.
    _v = contract.get("validation") or {}
    min_in_key = max(MIN_IN_KEY_FRAC, float(_v.get("min_in_key_frac", 0.0)))
    min_vels = max(MIN_DISTINCT_VELOCITIES, int(_v.get("min_distinct_velocities", 0)))
    min_style = max(MIN_STYLE_SCORE, float(_v.get("min_style_score", 0.0)))
    max_drift = min(MAX_REGISTER_DRIFT,
                    float((contract.get("register") or {}).get("max_drift_semitones",
                                                               MAX_REGISTER_DRIFT)))

    in_key = sum(1 for n in nd if (n["pitch"] % 12) in scale) / len(nd)
    checks["in_key"] = in_key >= min_in_key

    median = sorted(n["pitch"] for n in nd)[len(nd) // 2]
    checks["register"] = abs(median - contract["register"]["median"]) <= max_drift

    span = core._phrase_span(nd)
    checks["length"] = span <= contract["bars"] * core.BEATS_PER_BAR + 1e-6

    vels = {n["velocity"] for n in nd}
    checks["dynamics"] = len(vels) >= min_vels or len(nd) < 4

    sc = core.score_phrase(notes, role=role, key=key_str, scale_pcs=scale_pcs)
    checks["style_score"] = sc["overall"] >= min_style

    return {"passes": all(checks.values()), "checks": checks,
            "style_score": round(sc["overall"], 3),
            "in_key_frac": round(in_key, 3),
            "role": role,
            "thresholds": {"min_in_key_frac": min_in_key,
                           "min_distinct_velocities": min_vels,
                           "min_style_score": min_style,
                           "max_drift_semitones": max_drift}}
