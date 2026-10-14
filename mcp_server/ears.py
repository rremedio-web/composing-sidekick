"""The ears — admission gates, direction modifiers, session memory, serve().

Design: ears context layer (docs/history/plans/2026-07-10-ears-context-layer.md).
Gates FILTER, they never score; survivors keep their order so the caller's
existing taste ranking stays the only ranker. Session memory demotes, never
bans, and never writes to the taste model.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys

try:
    import moment as _moment
except ModuleNotFoundError:  # package entrypoint without mcp_server on sys.path
    from mcp_server import moment as _moment

# Ears is imported both by the bare MCP adapter path and by package tests.  Do
# not allow those forms to create separate session-memory state.
for _alias in ("ears", "mcp_server.ears"):
    sys.modules.setdefault(_alias, sys.modules[__name__])

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION_PATH = os.path.join(ROOT, "tools", "ideas", "ears_session.json")

GATE_ORDER = ("tempo_world", "key", "register", "density", "role")
_DENSITY_ORDER = ("sparse", "mid", "busy")
# When the room carries Live's own scale (moment["scale_pcs"]), a candidate whose
# key name does not match still passes the key gate if this fraction of its
# notes sits inside that scale — the Set's scale outranks a project's key label.
KEY_GATE_MIN_IN_SCALE = 0.75


def _validate_relax(relax):
    """A caller may relax ONE named gate, or 'auto' (the nearest_relaxation
    offer). Anything else fails loud."""
    if relax is None:
        return None
    if relax == "auto" or relax in GATE_ORDER:
        return relax
    raise ValueError("unknown gate %r — one of %s, or 'auto'"
                     % (relax, "/".join(GATE_ORDER)))

DIRECTIONS = {
    # gate MODIFIERS only, never scores (spec section 3; device-locked words)
    "darker": {"density_max": "mid", "minor_only": True},
    "clubbier": {"tempo_world": "club", "density_min": "mid"},
    "sparser": {"density_max": "sparse"},
    "weirder": {"skip_gates": ("density", "register")},
}

# Moment states that carry enough context to run the gates. "live" is Ableton,
# heard. "reference" is a described track he is chasing — honest about not being
# the room, so status_line never claims an empty Set nobody looked at.
GATED_EARS_STATES = ("live", "reference")

_MOD_KEYS = ("tempo_world", "minor_only", "density_max", "density_min", "skip_gates")


def _resolve_mods(direction):
    """direction -> mods dict. Accepts a known direction NAME or an ad-hoc mods
    dict (e.g. computed from an audio reference). Unknown words still fail loud.

    Ad-hoc mods may NOT set ``tempo_world``: a named direction is his explicit
    word, but an inferred context (a reference) must never silently veto the
    tempo world of the room he is actually playing in. The room is a fact; the
    reference is an aspiration about material he isn't hearing yet.
    """
    if direction is None:
        return {}
    if isinstance(direction, dict):
        unknown = sorted(set(direction) - set(_MOD_KEYS))
        if unknown:
            raise ValueError("unknown gate modifiers %s — one of %s"
                             % (unknown, "/".join(_MOD_KEYS)))
        if "tempo_world" in direction:
            raise ValueError(
                "ad-hoc mods may not set tempo_world — the room's tempo world "
                "wins over an inferred context; use a named direction instead")
        return dict(direction)
    if direction not in DIRECTIONS:
        raise ValueError("unknown direction %r — one of %s"
                         % (direction, "/".join(sorted(DIRECTIONS))))
    return dict(DIRECTIONS[direction])


def _direction_label(direction):
    if isinstance(direction, dict):
        return "reference(%s)" % ",".join("%s=%s" % (k, v)
                                          for k, v in sorted(direction.items())) or "reference"
    return direction


def _key_compatible(moment_key, cand_key, minor_only=False):
    if not moment_key or not cand_key:
        return not minor_only or "major" not in str(cand_key).lower()
    import core as _core
    try:
        mt, mm = _core._parse_key_string(str(moment_key))
        ct, cm = _core._parse_key_string(str(cand_key))
    except Exception:
        return True  # unparseable keys stay admitted — reported, not dropped
    if minor_only and cm == "major":
        return False
    mpc = _core.PITCH_NAMES.index(mt)
    cpc = _core.PITCH_NAMES.index(ct)
    diff = (cpc - mpc) % 12
    if diff == 0:
        return True  # same tonic; mode borrowing is his idiom
    if {mm, cm} == {"minor", "major"} and diff in (3, 9):
        return True  # relative pair
    return mm == cm and diff in (5, 7)  # the i<->iv / v neighborhood


def _gate_passes(gate, cand, m, role, mods, notes):
    if gate == "tempo_world":
        want = mods.get("tempo_world") or m.get("tempo_world")
        if want in (None, "other"):
            return True
        ct = cand.get("tempo")
        if ct is None:
            if "tempo-less candidates admitted" not in notes:
                notes.append("tempo-less candidates admitted")
            return True
        return _moment.tempo_world(ct) == want
    if gate == "key":
        minor_only = bool(mods.get("minor_only"))
        if _key_compatible(m.get("key"), cand.get("key"), minor_only=minor_only):
            return True
        # Live's scale, when the room carries it, is the Set's own truth: a
        # candidate whose notes live inside it passes even under a foreign key
        # label (union semantics — never stricter than the name check above).
        pcs = m.get("scale_pcs")
        cand_notes = cand.get("notes") or []
        if pcs and cand_notes:
            if minor_only and "major" in str(cand.get("key") or "").lower():
                return False
            inside = sum(1 for n in cand_notes if int(n["pitch"]) % 12 in set(pcs))
            return inside / len(cand_notes) >= KEY_GATE_MIN_IN_SCALE
        return False
    if gate == "register":
        band = _moment.register_band(cand.get("notes") or [])
        if band is None:
            return True
        target = cand.get("role") or role
        for r, info in (m.get("roles") or {}).items():
            if r != target and info.get("register_band") == band:
                return False
        return True
    if gate == "density":
        band = _moment.density_band(cand.get("notes") or [])
        dmax, dmin = mods.get("density_max"), mods.get("density_min")
        if dmax and _DENSITY_ORDER.index(band) > _DENSITY_ORDER.index(dmax):
            return False
        if dmin and _DENSITY_ORDER.index(band) < _DENSITY_ORDER.index(dmin):
            return False
        room = m.get("roles") or {}
        busiest = max((_DENSITY_ORDER.index(i.get("density", "sparse"))
                       for i in room.values()), default=0)
        if busiest >= 2:  # busy room wants complement, not competition
            return band != "busy"
        return True
    if gate == "role":
        cr = cand.get("role")
        if cr is None or cr == role:
            return True
        return cr not in set(m.get("roles") or {})
    raise ValueError("unknown gate %r" % gate)


def _run_gates(cands, m, role, mods, disabled=()):
    skip = set(mods.get("skip_gates", ()))
    drops, notes = [], []
    for gate in GATE_ORDER:
        if gate in disabled:
            drops.append({"gate": gate, "n": 0, "relaxed": True})
            continue
        if gate in skip:
            drops.append({"gate": gate, "n": 0, "skipped": True})
            continue
        keep = [c for c in cands if _gate_passes(gate, c, m, role, mods, notes)]
        drops.append({"gate": gate, "n": len(cands) - len(keep)})
        cands = keep
    return cands, drops, notes


def admit(candidates, m, *, role=None, direction=None, relax=None):
    """The admission ladder. Returns (survivors, gate_report). Never relaxes
    silently: an empty result carries a nearest_relaxation offer instead —
    and ``relax`` is how the caller says yes: one gate name disables that
    gate outright; 'auto' accepts the nearest_relaxation offer only when the
    normal run comes back empty. Either way the report says ``relaxed``."""
    mods = _resolve_mods(direction)
    relax = _validate_relax(relax)
    if m.get("ears") not in GATED_EARS_STATES:
        raise ValueError("admit() needs a live or reference moment — "
                         "serve() handles blind loudly")

    pool = list(candidates)
    import core as _core
    normalized_pool = []
    original_by_normalized_id = {}
    for candidate in pool:
        normalized = dict(candidate)
        if "notes" in normalized:
            normalized["notes"] = _core._note_dicts(normalized.get("notes") or [])
        normalized_pool.append(normalized)
        original_by_normalized_id[id(normalized)] = candidate
    disabled = (relax,) if relax and relax != "auto" else ()
    survivors, drops, notes = _run_gates(normalized_pool, m, role, mods, disabled=disabled)
    survivors = [original_by_normalized_id[id(candidate)] for candidate in survivors]
    report = {"pool": len(pool), "direction": _direction_label(direction),
              "dropped": drops, "survived": len(survivors), "notes": notes,
              "relaxed": relax if disabled else None}
    if not survivors and pool:
        offer_gate = None
        for gate in reversed(GATE_ORDER):
            if gate in disabled:
                continue
            retry, rdrops, rnotes = _run_gates(normalized_pool, m, role, mods,
                                               disabled=disabled + (gate,))
            if retry:
                offer_gate = gate
                if relax == "auto":
                    survivors = [original_by_normalized_id[id(c)] for c in retry]
                    report.update({"dropped": rdrops, "survived": len(survivors),
                                   "notes": rnotes, "relaxed": gate,
                                   "auto_relaxed": True})
                else:
                    report["nearest_relaxation"] = {
                        "gate": gate,
                        "offer": "nothing fits — relaxing the %s gate would admit %d; "
                                 "want it?" % (gate, len(retry)),
                    }
                break
        if offer_gate is None:
            report["nearest_relaxation"] = {
                "gate": None,
                "offer": "nothing fits, and no single gate is responsible — "
                         "this pool doesn't belong in this room",
            }
    return survivors, report


DEMOTE_AFTER = 3

_BUCKET_PHRASES = {
    "density": {"sparse": "sparse lines", "mid": "mid-density lines",
                "busy": "busy lines"},
    "register": {"low": "the low register", "mid": "the mid register",
                 "high": "the high register"},
    "contour": {"rising": "rising lines", "falling": "falling lines",
                "flat": "static lines"},
}


def _bucket_phrase(bkey):
    dim, _, value = bkey.partition(":")
    return _BUCKET_PHRASES.get(dim, {}).get(value, bkey)


def contour(notes):
    pitched = sorted((n for n in notes or [] if "pitch" in n),
                     key=lambda n: float(n["start"]))
    if len(pitched) < 2:
        return "flat"
    net = int(pitched[-1]["pitch"]) - int(pitched[0]["pitch"])
    if net > 1:
        return "rising"
    if net < -1:
        return "falling"
    return "flat"


def candidate_buckets(cand):
    # normalize to build_index shape first: a candidate may arrive MCP-shaped
    # (start_beats/duration_beats) and the band/contour helpers index n["start"]
    # (F1). _note_dicts accepts both shapes and is a no-op on already-normal notes.
    import core as _core
    notes = _core._note_dicts(cand.get("notes") or [])
    return {"density": _moment.density_band(notes),
            "register": _moment.register_band(notes) or "mid",
            "contour": contour(notes)}


def load_session(path=SESSION_PATH):
    if not os.path.exists(path):
        return {"streaks": {}, "demoted": [], "reason_demoted": [],
                "reason_voted": []}
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (ValueError, OSError) as exc:
        # fail closed: a corrupt session file blocks the verdict loudly (F5) —
        # name the one-gesture recovery so the caller can act, never silently
        # reset the counters or (worse) let a rating persist past a failed write.
        raise ValueError(
            "ears session file is corrupt — say 'reset ears' to clear it (%s)" % exc
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            "ears session file has an invalid shape — say 'reset ears' to clear it")
    data.setdefault("streaks", {})
    data.setdefault("demoted", [])
    data.setdefault("reason_demoted", [])
    data.setdefault("reason_voted", [])
    return data


# A DROP reason names the dimension he heard ("too static" is about the
# line shape, "wrong register" about the floor); the heard notes name the
# value. The named bucket gets one extra vote, so two named DROPs demote
# where three unnamed ones were needed. Session-scoped like every streak,
# and it lands whether or not the ears gates ran — the verdict writer is
# the only session-memory writer either way.
_REASON_BUCKET_DIMS = (
    ("wrong register", "register"),
    ("static", "contour"),
    ("jumpy", "contour"),
)


# A negation just before the word ("not static", "isn't jumpy") is not a
# vote for that bucket — only an unnegated mention moves the needle.
_NEGATION_RE = re.compile(
    r"(?:\bnot\b|\bnever\b|\bno\b|n['\u2019]t)[\s-]+"
    r"(?:(?:so|too|really|very|that|feel|feels|sound|sounds)\s+)?$",
    re.IGNORECASE)


def reason_bucket_dim(reason: str | None) -> str | None:
    """Ears-bucket dimension a DROP reason word addresses, if any.

    Word-boundary match, so "ecstatic" never votes the contour bucket; a
    negated mention ("not static", "isn't jumpy") is not a vote either."""
    if not reason:
        return None
    lowered = str(reason).lower()
    for word, dim in _REASON_BUCKET_DIMS:
        for match in re.finditer(r"\b%s\b" % re.escape(word), lowered):
            prefix = re.split(r"[,.;!?]", lowered[:match.start()])[-1]
            if not _NEGATION_RE.search(prefix):
                return dim
    return None


def note_verdict(state, cand, kept, reason=None):
    """Session memory: 3 straight rejects in a bucket demote it (never ban).
    Returns only NEW announcements; never touches the taste model."""
    announcements = []
    buckets = candidate_buckets(cand)
    named_dim = reason_bucket_dim(reason) if not kept else None
    for dim, value in buckets.items():
        bkey = "%s:%s" % (dim, value)
        if kept:
            state["streaks"][bkey] = 0
            continue
        votes = 2 if dim == named_dim else 1
        if dim == named_dim and bkey not in state["reason_voted"]:
            state["reason_voted"].append(bkey)
        state["streaks"][bkey] = state["streaks"].get(bkey, 0) + votes
        if state["streaks"][bkey] >= DEMOTE_AFTER and bkey not in state["demoted"]:
            state["demoted"].append(bkey)
            announcements.append("easing off %s — 'reset ears' clears it"
                                 % _BUCKET_PHRASES[dim][value])
        # Reason-word demotions are tracked apart: ears-off serving reads only these.
        if (state["streaks"][bkey] >= DEMOTE_AFTER and
                bkey in state["reason_voted"] and
                bkey not in state["reason_demoted"]):
            state["reason_demoted"].append(bkey)
    return announcements


def save_session(state, path=SESSION_PATH):
    import core as _core
    _core._save_json_atomic(path, state)


def _event_fingerprint(cand, kept, conflict_fingerprint=None):
    if conflict_fingerprint:
        return str(conflict_fingerprint)
    payload = {"cand": cand, "kept": bool(kept)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


def record_verdict(cand, kept, path=SESSION_PATH, *, event_id=None,
                   conflict_fingerprint=None, reason=None,
                   ledger_session_id=None):
    """The ONLY production writer of session memory (spec §4): load → note_verdict
    → save, all under an fcntl flock so two concurrent verdicts can't race the
    bucket counters (F9a). serve() only ever READS session state, never writes.
    Returns the new demotion announcements. A corrupt session file raises out of
    load_session (fail-closed) before anything is written. ``reason`` carries a
    DROP's own words ('too static' etc.) into the named bucket — this lands
    whether or not the ears gates ran, since the writer never gates."""
    import fcntl
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path + ".lock", "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            state = load_session(path)
            stored_session_id = state.get("ledger_session_id")
            if (ledger_session_id is not None and
                    stored_session_id is not None and
                    stored_session_id != ledger_session_id):
                state["streaks"] = {}
                state["demoted"] = []
                state["reason_demoted"] = []
                state["reason_voted"] = []
            if ledger_session_id is not None:
                state["ledger_session_id"] = ledger_session_id
            fingerprint = _event_fingerprint(cand, kept, conflict_fingerprint)
            if event_id:
                events = state.setdefault("judgment_events", {})
                existing = events.get(event_id)
                if existing is not None:
                    if existing.get("fingerprint") != fingerprint:
                        raise ValueError("conflicting retry for ears event %s" % event_id)
                    return list(existing.get("announcements") or [])
            announcements = note_verdict(state, cand, kept, reason=reason)
            if event_id:
                state.setdefault("judgment_events", {})[event_id] = {
                    "fingerprint": fingerprint, "announcements": list(announcements),
                }
            save_session(state, path)
            return announcements
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def reset_session(path=SESSION_PATH):
    if os.path.exists(path):
        os.unlink(path)
    return "ears reset — session demotions cleared"


def reset_streaks(path=None):
    """Scope DROP streaks to the ledger session: clear streaks + demotions
    when a night definitively closes, keeping the judgment_events replay
    guard (a retried old verdict must still replay, not double-vote).
    Returns True when state was cleared, False when no file exists."""
    path = path or SESSION_PATH
    if not os.path.exists(path):
        return False
    state = load_session(path)
    state["streaks"] = {}
    state["demoted"] = []
    state["reason_demoted"] = []
    state["reason_voted"] = []
    state.pop("ledger_session_id", None)
    save_session(state, path)
    return True


def serve(candidates, m, *, role=None, direction=None, session_path=SESSION_PATH,
          relax=None):
    """The chokepoint (spec section 5). Blind bypasses gates LOUDLY; survivors
    keep their order for the caller's existing taste ranking; demoted buckets
    go to the back — demoted, never banned. ``relax`` (a gate name or 'auto')
    is the caller accepting a relaxation; it is always announced."""
    # validate the direction (and the relax word) BEFORE the blind short-circuit:
    # an unknown word like "moist" must fail loud even when the room can't be
    # heard, never fall open into an ungated serve (F8a). admit() re-checks.
    _resolve_mods(direction)
    _validate_relax(relax)
    if m.get("ears") not in GATED_EARS_STATES:
        return {"served": list(candidates), "ears": "blind",
                "gate_report": {"pool": len(candidates),
                                "survived": len(candidates),
                                "dropped": [], "skipped": "blind"},
                "announcements": ["no ears — serving context-blind (%s)"
                                  % m.get("reason", "Live unreachable")]}
    survivors, report = admit(candidates, m, role=role, direction=direction,
                              relax=relax)
    state = load_session(session_path)
    demoted = set(state.get("demoted", []))
    announcements = []
    if report.get("relaxed"):
        announcements.append(
            "%s gate relaxed by caller%s"
            % (report["relaxed"],
               " (auto: nearest relaxation)" if report.get("auto_relaxed") else ""))
    if demoted and survivors:
        front, back = [], []
        for c in survivors:
            hit = {"%s:%s" % (d, v)
                   for d, v in candidate_buckets(c).items()} & demoted
            (back if hit else front).append(c)
        if back:
            announcements.append("easing off %s — 'reset ears' clears it"
                                 % ", ".join(_bucket_phrase(b) for b in sorted(demoted)))
        survivors = front + back
    if not survivors and report.get("nearest_relaxation"):
        announcements.append(report["nearest_relaxation"]["offer"])
    return {"served": survivors, "ears": m.get("ears"), "gate_report": report,
            "announcements": announcements}


def status_line(m, gate_report=None, announcements=()):
    """The one canonical ears line — same words on the device, in chat, on
    pages. State only, never identity: no track or project names ever."""
    if m.get("ears") == "reference":
        # NEVER borrow the live wording here. A reference moment has no room, so
        # printing the room summary would put "empty room" — a claim about his
        # Ableton Set that nobody looked at — on the device chassis.
        parts = ["steering by reference — not listening to your set"]
        if m.get("tempo"):
            parts.append("ref ~%g" % m["tempo"])
        if gate_report and "survived" in gate_report:
            parts.append("fit: %d→%d" % (gate_report.get("pool", 0),
                                         gate_report["survived"]))
        parts.extend(announcements)
        return " | ".join(parts)
    if m.get("ears") != "live":
        return "no ears — serving context-blind"
    head = " · ".join([
        ("%g" % m["tempo"]) if m.get("tempo") else "?",
        m.get("key") or "key ?",
        m.get("tempo_world") or "?",
    ])
    roles = m.get("roles") or {}
    unheard = m.get("unheard") or []
    unread = m.get("unread") or []
    if roles:
        room = " · ".join("%s %s" % ("drums" if r == "drum" else r,
                                     i.get("density", "?"))
                          for r, i in sorted(roles.items()))
    elif unheard or unread:
        # Audio (or ambiguous) material, or a MIDI clip whose notes were not
        # read, is present — never call that an empty room.
        room = "no midi heard"
    else:
        room = "empty room"
    parts = [head, room]
    for cards, word in ((unheard, "unheard"), (unread, "unread")):
        if cards:
            n = len(cards)
            parts.append("%d clip%s %s" % (n, "" if n == 1 else "s", word))
    ctx = m.get("context") or {}
    if isinstance(ctx.get("tracks_total"), int):
        parts.append("heard %d/%d tracks" % (
            ctx.get("tracks_heard", 0), ctx["tracks_total"]))
    if ctx.get("transport_playing") is False:
        parts.append("transport stopped")
    if gate_report and "survived" in gate_report:
        parts.append("fit: %d→%d" % (gate_report.get("pool", 0),
                                     gate_report["survived"]))
    parts.extend(announcements)
    return " | ".join(parts)
