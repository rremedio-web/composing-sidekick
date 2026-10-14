"""The Moment — a read-only snapshot of the room.

Design: ears context layer (docs/history/plans/2026-07-10-ears-context-layer.md).
Pure builders here so selftests can feed synthetic rooms; the Producer Pal
reader injects into build_moment(). ears is "live" or "blind" — NEVER guessed.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys

# One module universe for the bare import and the package import (F4).
for _alias in ("moment", "mcp_server.moment"):
    sys.modules.setdefault(_alias, sys.modules[__name__])

_TEMPO_RE = re.compile(r"""(?:"tempo"|'tempo'|tempo)\s*:\s*([-+]?\d+(?:\.\d+)?)""")
_STRUCTURED_TEMPO_KEYS = ("tempo", "bpm")

# Half-open bands. His measured worlds are ~75 and ~120; 92-100 is not "between
# worlds" for him, it is slow music played a little faster, so downtempo runs up
# to (not including) 100 and club takes over from there.
TEMPO_WORLDS = (("downtempo", 60.0, 100.0), ("club", 100.0, 141.0))
DENSITY_BANDS = (("sparse", 0.0, 1.0), ("mid", 1.0, 3.0), ("busy", 3.0, float("inf")))
REGISTER_BANDS = (("low", 0, 48), ("mid", 48, 72), ("high", 72, 128))
_ROLE_KEYWORDS = (
    ("drum", ("drum", "kick", "perc", "hat", "snare", "808", "batida")),
    ("bass", ("bass", "sub", "baixo")),
    ("harmony", ("pad", "chord", "keys", "harmon", "string")),
    ("lead", ("lead", "melod", "arp", "voz", "vox")),
)


def _extract_finite_tempo(val) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return f if math.isfinite(f) and f > 0 else None
    if isinstance(val, str):
        try:
            f = float(val.strip())
            return f if math.isfinite(f) and f > 0 else None
        except (ValueError, TypeError):
            return None
    return None


def parse_tempo(overview) -> float | None:
    """Read a finite fractional JSON number tempo (e.g. 99.5, 120.25).

    Rejects bool, NaN, Infinity, negative values, and non-numeric objects.
    Extracts from structured dict / PPResponse first, parses JSON text next,
    and explicitly falls back to regex search across legacy text.
    Never relies on int-only truncation fallback causing blind room (F7).
    """
    if overview is None or isinstance(overview, bool):
        return None

    # 1. Direct dict check
    if isinstance(overview, dict):
        for key in _STRUCTURED_TEMPO_KEYS:
            t = _extract_finite_tempo(overview.get(key))
            if t is not None:
                return t
        res = overview.get("result")
        if isinstance(res, dict):
            for key in _STRUCTURED_TEMPO_KEYS:
                t = _extract_finite_tempo(res.get(key))
                if t is not None:
                    return t

    # 2. PPResponse or object with .result attribute
    if hasattr(overview, "result") and isinstance(getattr(overview, "result"), dict):
        res = getattr(overview, "result")
        for key in _STRUCTURED_TEMPO_KEYS:
            t = _extract_finite_tempo(res.get(key))
            if t is not None:
                return t

    # 3. String: try parsing JSON first
    raw = str(overview)
    stripped = raw.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or (stripped.startswith("[") and stripped.endswith("]")):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                for key in _STRUCTURED_TEMPO_KEYS:
                    t = _extract_finite_tempo(parsed.get(key))
                    if t is not None:
                        return t
                res = parsed.get("result")
                if isinstance(res, dict):
                    for key in _STRUCTURED_TEMPO_KEYS:
                        t = _extract_finite_tempo(res.get(key))
                        if t is not None:
                            return t
        except Exception:
            pass

    # 4. Explicit legacy regex fallback
    m = _TEMPO_RE.search(raw)
    if m:
        t = _extract_finite_tempo(m.group(1))
        if t is not None:
            return t

    return None


def tempo_world(tempo) -> str:
    t = float(tempo)
    for name, lo, hi in TEMPO_WORLDS:
        if lo <= t < hi:
            return name
    return "other"


def density_band(notes, span: float | None = None) -> str:
    """Notes per beat over ``span`` beats (default: from 0 to the last note end)."""
    if not notes:
        return "sparse"
    if span is None:
        span = max(n["start"] + n.get("duration", 0.0) for n in notes)
    per_beat = len(notes) / max(span, 1.0)
    for name, lo, hi in DENSITY_BANDS:
        if lo <= per_beat < hi:
            return name
    return "busy"


def register_range(notes):
    pitches = [int(n["pitch"]) for n in notes]
    return (min(pitches), max(pitches)) if pitches else None


def register_band(notes):
    if not notes:
        return None
    median = sorted(int(n["pitch"]) for n in notes)[len(notes) // 2]
    for name, lo, hi in REGISTER_BANDS:
        if lo <= median < hi:
            return name
    return "high"


def infer_role(track_name, notes) -> str:
    low = str(track_name or "").lower()
    for role, words in _ROLE_KEYWORDS:
        if any(w in low for w in words):
            return role
    if not notes:
        return "lead"
    if register_band(notes) == "low":
        return "bass"
    starts = {}
    for n in notes:
        key = round(float(n["start"]), 3)
        starts[key] = starts.get(key, 0) + 1
    poly = sum(1 for c in starts.values() if c > 1) / max(len(starts), 1)
    return "harmony" if poly > 0.4 else "lead"


def _normalize_unheard(unheard) -> list:
    """Stable list of unheard clip cards for fingerprint + moment payload."""
    out = []
    for u in unheard or []:
        if not isinstance(u, dict):
            continue
        kind = u.get("kind") or "unknown"
        if kind not in ("audio", "unknown"):
            kind = "unknown"
        out.append({
            "track": str(u.get("track") or ""),
            "track_index": int(u["track_index"]) if u.get("track_index") is not None else None,
            "slot": int(u["slot"]) if u.get("slot") is not None else 0,
            "kind": kind,
            "clip_name": str(u.get("clip_name") or ""),
        })
    out.sort(key=lambda c: (c["track_index"] is None, c["track_index"] or 0,
                            c["slot"], c["track"], c["clip_name"], c["kind"]))
    return out


def _same_or_relative(key_a: str, key_b: str) -> bool:
    """Same tonic (mode borrowing is his idiom) or a relative major/minor pair."""
    import core as _core
    try:
        ta, ma = _core._parse_key_string(key_a)
        tb, mb = _core._parse_key_string(key_b)
    except Exception:
        return False
    diff = (_core.PITCH_NAMES.index(tb) - _core.PITCH_NAMES.index(ta)) % 12
    if diff == 0:
        return True
    return {ma, mb} == {"minor", "major"} and diff in (3, 9)


_LIVE_DEFAULT_SCALE = ("c major", [0, 2, 4, 5, 7, 9, 11])


def build_moment(tracks, tempo, unheard=None, scale=None, context=None) -> dict:
    """tracks: [{"name": str, "notes": [{"pitch","start","duration","velocity"}]}]

    ``unheard``: optional list of named-but-unheard session clips (audio or
    ambiguous). Names are conversational evidence only — never instrument
    identity. Empty unheard is omitted from the digest fingerprint so existing
    MIDI-only rooms keep stable digests; a nonempty list is always fingerprinted.

    ``scale``: Live's own scale setting as ("<Tonic> <mode>", pitch classes) —
    when present it IS the room's key (``key``), with the pitch classes on
    ``scale_pcs``; the heard estimate stays beside it as ``key_estimate`` and
    the assumptions say which won. Without it ``key`` is the estimate, as ever.

    ``context``: optional dict carrying active playing slots, transport status,
    playhead position, meter, and overrides. Included in the digest fingerprint
    so context changes update cache identity and prevent stale room reads.

    Every track's notes are normalized through core._note_dicts first: real
    Producer Pal reads arrive MCP-shaped (start_beats/duration_beats), and the
    band/register helpers below index n["start"]/n["duration"] — without the
    normalization a nonempty live room raised KeyError and get_moment declared
    blind (F1). _note_dicts accepts BOTH shapes, so the twin note-shapes yield
    an identical moment.
    """
    import core as _core  # lazy: no cycle at import time
    by_role, spans = {}, {}
    for t in tracks:
        notes = _core._note_dicts(t.get("notes") or [])
        role = infer_role(t.get("name", ""), notes)
        by_role.setdefault(role, []).extend(notes)
        # a heard clip carries its played-region length (``span``); otherwise
        # the span runs from 0 to its last note end, as before
        span = t.get("span")
        if span is None:
            span = max((n["start"] + n.get("duration", 0.0) for n in notes), default=0.0)
        spans[role] = max(spans.get(role, 0.0), float(span))

    roles = {}
    pc_hist = [0.0] * 12
    for role, notes in sorted(by_role.items()):
        roles[role] = {
            "n_notes": len(notes),
            "density": density_band(notes, spans.get(role)),
            "register": register_range(notes),
            "register_band": register_band(notes),
        }
        if role != "drum":
            for n in notes:
                pc_hist[int(n["pitch"]) % 12] += 1.0

    if any(pc_hist):
        tonic, mode, _ = _core.estimate_key(pc_hist)
        key_estimate = "%s %s" % (tonic, mode)
        key_note = "key %s heard from the playing clips" % key_estimate
    else:
        key_estimate, key_note = None, "no pitched material playing — key unknown"

    key, scale_pcs = key_estimate, None
    assumptions = []
    scale_key = scale[0] if isinstance(scale, (tuple, list)) and len(scale) == 2 else None
    if scale_key:
        key = str(scale_key)
        pcs = scale[1]
        try:
            s_tonic, s_mode = _core._parse_key_string(key)
        except ValueError:
            s_tonic, s_mode = None, None
        if pcs:
            scale_pcs = sorted({int(p) % 12 for p in pcs})
        elif s_tonic is not None:
            scale_pcs = sorted(_core.diatonic_set(_core.PITCH_NAMES.index(s_tonic), s_mode))
        assumptions.append("key %s from Live's scale setting" % key)
        if key_estimate and not _same_or_relative(key_estimate, key):
            assumptions.append(
                "the playing clips lean %s — check the Set's scale if that's not "
                "what you hear" % key_estimate)
        if key.lower() == _LIVE_DEFAULT_SCALE[0] and scale_pcs in (None, _LIVE_DEFAULT_SCALE[1]):
            assumptions.append(
                "C Major is Live's default scale — confirm it was set on purpose")
        if s_mode is not None and s_mode not in _core.KNOWN_MODES:
            assumptions.append(
                "scale '%s' is outside the sidekick's mode table — exact pitches "
                "from Live still apply" % s_mode)
    else:
        assumptions.append(key_note)

    unheard_cards = _normalize_unheard(unheard)
    if unheard_cards:
        n = len(unheard_cards)
        assumptions.append(
            "%d clip%s in this Set and I cannot hear %s — ask what %s doing "
            "before structure advice; do not infer musical content from the name"
            % (n, "" if n == 1 else "s",
               "it" if n == 1 else "them",
               "it is" if n == 1 else "they are"))
        if any(c["kind"] == "unknown" for c in unheard_cards):
            assumptions.append(
                "at least one present clip has uncertain kind (not clearly audio) "
                "— treat as unheard, not as empty")

    fp_obj = {"tempo": float(tempo), "roles": roles, "key": key}
    if unheard_cards:
        fp_obj["unheard"] = unheard_cards
    if scale_key:
        fp_obj["scale_pcs"] = scale_pcs
    if context:
        fp_obj["context"] = context
    fingerprint = json.dumps(fp_obj, sort_keys=True, separators=(",", ":"))
    return {
        "ears": "live",
        "tempo": float(tempo),
        "tempo_world": tempo_world(tempo),
        "key": key,
        "key_estimate": key_estimate,
        "scale_pcs": scale_pcs,
        "assumptions": assumptions,
        "roles": roles,
        "unheard": unheard_cards,
        "hidden": [],
        "unread": [],
        "heard_tracks": sorted({str(t.get("name", "")) for t in tracks}),
        "context": context if context is not None else {},
        "digest": hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12],
    }


def blind_moment(reason) -> dict:
    return {"ears": "blind", "reason": str(reason), "tempo": None,
            "tempo_world": None, "key": None, "key_estimate": None,
            "scale_pcs": None, "roles": {},
            "unheard": [], "hidden": [], "unread": [], "heard_tracks": [],
            "context": {},
            "assumptions": ["no ears — serving context-blind"]}


def parse_meter(sig_raw: Any) -> tuple[str, float]:
    """Validate and parse a meter string (e.g. '4/4', '3/4', '6/8').

    Invalid, malformed, non-positive, or non-power-of-two denominator meters
    raise ValueError. Never silently count 4/4.
    """
    if not isinstance(sig_raw, str) or "/" not in sig_raw:
        raise ValueError("invalid meter: %r" % (sig_raw,))
    parts = sig_raw.strip().split("/")
    if len(parts) != 2:
        raise ValueError("invalid meter: %r" % (sig_raw,))
    try:
        num = int(parts[0].strip())
        den = int(parts[1].strip())
    except (ValueError, TypeError):
        raise ValueError("invalid meter: %r" % (sig_raw,))
    if num <= 0 or den <= 0:
        raise ValueError("invalid meter: %r" % (sig_raw,))
    if (den & (den - 1)) != 0:
        raise ValueError("invalid meter: %r" % (sig_raw,))
    bpb = num * 4.0 / den
    return f"{num}/{den}", bpb


def select_arrangement_clips(clips, bar, beats_per_bar: float = 4.0,
                             meter: str | None = None) -> list[dict]:
    """The arrangement clips (Producer Pal read-track listing entries) whose
    [arrangementStart, arrangementStart + arrangementLength) contains ``bar``
    (1-based). Entries without a readable start/length are skipped. With the
    song ``meter`` ("6/8") the beat of arrangementStart is the meter's beat."""
    if not isinstance(beats_per_bar, (int, float)) or not math.isfinite(beats_per_bar) or beats_per_bar <= 0:
        raise ValueError("beats_per_bar must be a positive finite number")
    import pp_codec
    import pp_session
    import clip_playback
    parsed = clip_playback.parse_signature(meter)
    at = (int(bar) - 1) * beats_per_bar
    out = []
    for c in clips or []:
        if not isinstance(c, dict):
            continue
        # 2.2 arrangementStart, 2.3 the position in the clip path 't3[5|1]'
        start_s, length_s = pp_session.arrangement_start_of(c), c.get("arrangementLength")
        if not start_s or not length_s:
            continue
        try:
            start = (clip_playback.bar_beat_quarters(str(start_s), *parsed) if parsed
                     else pp_codec.bar_beat_to_beats(str(start_s), beats_per_bar))
            length = pp_codec.pp_duration_to_beats(str(length_s), beats_per_bar)
        except (ValueError, TypeError):
            continue
        if start <= at < start + length:
            out.append(c)
    return out


def _unheard_card_from_clip(clip: dict, track_index: int, kind: str,
                            track_names: dict | None = None) -> dict:
    """Build one unheard card. Track name comes from the overview map (PP2
    clip reads do not carry trackName); clip name comes from the clip raw."""
    import pp_codec  # adapter boundary: pure wire parse, not live_write
    raw = clip.get("raw") or ""
    names = track_names or {}
    track = names.get(track_index) or ("track %d" % track_index)
    clip_name = clip.get("name") or pp_codec.find_str(raw, "name", None) or ""
    return {
        "track": track,
        "track_index": track_index,
        "slot": int(clip.get("slot", 0)),
        "kind": kind,
        "clip_name": clip_name,
    }


# A MIDI clip with notes, or one whose notes Producer Pal's [-L, 2L) window
# could not return ("unread"): read it through the Live API when on, else the
# split files it as unread. Never "empty".
_READ_PRESENCES = ("heard", "unread")


def _arrangement_track_include(live_api: bool) -> list[str]:
    """hear_the_room's arrangement track listing. With the Live API on every
    MIDI clip's notes are read whole through it (a failed read is unread), so
    the listing skips Producer Pal's notes."""
    return (["arrangement-clips", "timing"] if live_api
            else ["arrangement-clips", "notes", "timing"])


def _file_split(split: dict, *, track: str, track_index: int, slot: int, clip_name: str,
                heard: list, hidden: list, unread: list) -> None:
    """File one clip's split: unread (notes not read — neither heard nor
    hidden), or heard with the notes behind its brace as a hidden card."""
    if split.get("unread"):
        unread.append({"track": track, "track_index": track_index, "slot": slot,
                       "clip_name": clip_name, "coverage": split.get("coverage"),
                       "reason": split.get("unread_reason")})
        return
    heard.append(_heard_track(track, split))
    if split.get("n_hidden"):
        hidden.append({
            "track": track, "track_index": track_index, "slot": slot,
            "clip_name": clip_name,
            "n_hidden": int(split["n_hidden"]),
            "hidden_bars": split.get("hidden_span_bars"),
            "loop_end_beats": split.get("loop_end_beats"),
        })


def _heard_track(track: str, split: dict) -> dict:
    """One heard clip for build_moment: its audible notes in QUARTER beats
    measured from the start of its played region, with that region's length
    as ``span`` — so density is notes per beat of what plays, whether the notes
    came from the Live API (clip-content time: Bassline plays 88-104) or from
    Producer Pal (clip-meter beats: eighths in 6/8)."""
    scale = 4.0 / float(split.get("notes_den") or 4)
    region = split.get("played_region")
    origin = float(region[0]) if region else 0.0
    notes = []
    for n in split["audible"]:
        start = n.get("start", n.get("start_beats", n.get("t", 0.0)))
        dur = n.get("duration", n.get("duration_beats", n.get("d", 0.0)))
        notes.append({"pitch": n.get("pitch", n.get("p")),
                      "start": float(start or 0.0) * scale - origin,
                      "duration": float(dur or 0.0) * scale,
                      "velocity": n.get("velocity", n.get("v", 100))})
    out = {"name": track, "notes": notes}
    if region:
        out["span"] = float(region[1]) - origin
    return out


async def _live_clip_or_none(session, clip_id):
    """The complete Live API read of a clip when ppal-live-api is on (it may
    carry an ``error``: the clip is then unread), else None."""
    if clip_id is None or not session.capabilities.get("live_api"):
        return None
    import live_write
    return await live_write.read_clip_live(session, clip_id)


def _unread_live_failure(live: dict, track: str, track_index: int, slot: int,
                         clip_name: str) -> dict:
    return {"track": track, "track_index": track_index, "slot": slot, "clip_name": clip_name,
            "coverage": None,
            "reason": "the Live API read of this clip failed (%s); Producer Pal's own "
                      "read covers only [-L, 2L) of the clip, so its notes were not read"
                      % live.get("error")}


def _unread_assumptions(m: dict, unread: list) -> None:
    for card in unread:
        m["assumptions"].append(
            "clip %r on track %r: notes not read — %s" % (
                card["clip_name"], card["track"], card["reason"]))


async def read_moment_live(max_tracks: int | None = None, where: str = "session",
                           bar: int | None = None, session: Any = None) -> dict:
    """Read the room through Producer Pal. Raises on any bridge failure so the
    caller converts it into a DECLARED blind moment — no silent guessing.

    where='session' (default) scans actual playing Session slots (not row 0).
    where='arrangement' hears clips at current transport position or explicit ``bar``.
    Tracks overriding arrangement with playing Session clips are honestly read as overrides.
    Stopped transport or unknown active context is described honestly without row-0 substitution.
    """
    import live_write  # lazy: keeps moment importable without the MCP deps
    import pp_session
    import pp_codec
    import config as _config

    if where not in ("session", "arrangement"):
        raise ValueError("where must be 'session' or 'arrangement', got %r" % (where,))

    if session is None:
        async with pp_session.operation_session(live_write.PP_URL) as s:
            return await read_moment_live(max_tracks=max_tracks, where=where, bar=bar, session=s)

    overview = await session.call("ppal-read-live-set", {"include": ["tracks"]})
    tempo = parse_tempo(overview)
    if tempo is None:
        raise RuntimeError("live-set overview carries no tempo")

    scale = None
    if _config.MOMENT_TRUST_LIVE_SCALE:
        scale_key, scale_pcs = live_write.scale_from_overview(overview)
        if scale_key:
            scale = (scale_key, scale_pcs)

    ov_res = pp_session._result(overview) if hasattr(pp_session, "_result") else None
    ov_res = ov_res if isinstance(ov_res, dict) else {}
    sig_raw = ov_res.get("timeSignature")
    if sig_raw is None:
        sig_raw = live_write._find_str(overview, "timeSignature", None)
    if sig_raw is None:
        raise ValueError("live-set overview carries no timeSignature; cannot guess meter")
    sig, bpb = parse_meter(sig_raw)

    is_playing = None
    current_song_time = None
    if "isPlaying" in ov_res or "is_playing" in ov_res:
        raw_p = ov_res.get("isPlaying", ov_res.get("is_playing"))
        if isinstance(raw_p, (bool, int)):
            is_playing = bool(raw_p)
    if "currentSongTime" in ov_res or "current_song_time" in ov_res:
        raw_t = ov_res.get("currentSongTime", ov_res.get("current_song_time"))
        if isinstance(raw_t, (int, float)) and not isinstance(raw_t, bool) and math.isfinite(float(raw_t)):
            current_song_time = float(raw_t)

    if session.capabilities.get("live_api"):
        try:
            ops = [
                {"type": "getProperty", "property": "is_playing"},
                {"type": "getProperty", "property": "current_song_time"},
            ]
            receipts = await live_write._live_api_results(session, "live_set", ops)
            if len(receipts) >= 2:
                r_play = receipts[0].get("result")
                if isinstance(r_play, (bool, int)):
                    is_playing = bool(r_play)
                r_time = receipts[1].get("result")
                if isinstance(r_time, (int, float)) and not isinstance(r_time, bool) and math.isfinite(float(r_time)):
                    current_song_time = float(r_time)
        except Exception:
            pass

    total_tracks = live_write._regular_track_count(overview)
    track_names = live_write.track_names_by_index(overview)
    # A stopped Arrangement still hears the playhead bar: the clips under the
    # song-time cursor are real context, not a silent room. Every other
    # stopped view keeps the empty room (deliberate — never substitute).
    can_derive_arrangement_bar = (
        where == "arrangement"
        and (bar is not None or current_song_time is not None))
    if is_playing is False and not can_derive_arrangement_bar:
        context_meta = {
            "where": where,
            "transport_playing": False,
            "current_song_time": current_song_time,
            "meter": sig,
            "beats_per_bar": bpb,
            "active_slots": {},
            "overrides": [],
            "tracks_heard": 0,
            "tracks_scanned": 0,
            "tracks_total": total_tracks,
        }
        m0 = build_moment([], tempo, scale=scale, context=context_meta)
        m0["assumptions"].append("transport is stopped — no clips currently playing; not substituting session row 0")
        if sig != "4/4":
            m0["assumptions"].append("song meter %s (%.1f beats per bar)" % (sig, bpb))
        return m0
    if max_tracks is None:
        max_tracks = (_config.ARRANGEMENT_MAX_TRACKS if where == "arrangement"
                      else _config.MOMENT_MAX_TRACKS)
    n_tracks = min(total_tracks, max_tracks)
    if n_tracks == 0:
        context_meta = {
            "where": where,
            "transport_playing": is_playing,
            "current_song_time": current_song_time,
            "meter": sig,
            "beats_per_bar": bpb,
            "active_slots": {},
            "overrides": [],
            "tracks_heard": 0,
            "tracks_scanned": 0,
            "tracks_total": total_tracks,
        }
        m0 = build_moment([], tempo, scale=scale, context=context_meta)
        m0["assumptions"].append("no regular tracks in the overview")
        return m0

    if where == "arrangement":
        target_bar = None
        if bar is not None:
            target_bar = int(bar)
            bar_source = "explicit"
        elif current_song_time is not None:
            target_bar = int(current_song_time // bpb) + 1
            bar_source = "transport"
        else:
            raise ValueError("arrangement needs bar=N — transport playhead unavailable")

        tracks_a: list[dict] = []
        unheard_a: list[dict] = []
        hidden_a: list[dict] = []
        unread_a: list[dict] = []
        overrides: list[str] = []
        assumptions_a: list[str] = []

        for idx in range(n_tracks):
            track_name = track_names.get(idx) or ("track %d" % idx)
            is_muted = False
            is_override = False
            session_slot = None

            if session.capabilities.get("live_api"):
                try:
                    ops = [
                        {"type": "getProperty", "property": "mute"},
                        {"type": "getProperty", "property": "back_to_arranger"},
                        {"type": "getProperty", "property": "playing_slot_index"},
                    ]
                    rcpts = await live_write._live_api_results(session, "live_set tracks %d" % idx, ops)
                    if len(rcpts) >= 3:
                        is_muted = bool(rcpts[0].get("result"))
                        bta = rcpts[1].get("result")
                        psi = rcpts[2].get("result")
                        if (bta is False or bta == 0) and isinstance(psi, int) and psi >= 0:
                            is_override = True
                            session_slot = psi
                except Exception:
                    pass

            if is_muted:
                assumptions_a.append("track %r is muted — omitted from arrangement context" % track_name)
                continue

            if is_override and session_slot is not None:
                overrides.append(track_name)
                try:
                    raw_clip = await session.call("ppal-read-clip", {
                        **pp_session.clip_location_args(idx, session_slot),
                        "include": ["notes", "timing"],
                    })
                    clip_res = pp_session.clip_record(raw_clip) if isinstance(raw_clip, pp_session.PPResponse) else {}
                    if isinstance(clip_res, dict) and "timeSignature" not in clip_res:
                        clip_res["timeSignature"] = sig
                    notes = pp_session.extract_notes(raw_clip) if hasattr(pp_session, "extract_notes") else []
                    if not notes:
                        notes = live_write._extract_notes(raw_clip) or []
                    presence = live_write.classify_session_clip(raw_clip, notes)
                    live = (await _live_clip_or_none(session, clip_res.get("id"))
                            if presence in _READ_PRESENCES and isinstance(clip_res, dict)
                            else None)
                    if live is not None and live.get("error"):
                        unread_a.append(_unread_live_failure(
                            live, track_name, idx, session_slot, str(clip_res.get("name") or "")))
                    elif presence in _READ_PRESENCES:
                        split = pp_session.split_audible_notes(clip_res, notes, live=live)
                        _file_split(split, track=track_name, track_index=idx, slot=session_slot,
                                    clip_name=str(clip_res.get("name") or ""),
                                    heard=tracks_a, hidden=hidden_a, unread=unread_a)
                    elif presence in ("unheard_audio", "unheard_unknown"):
                        unheard_a.append({
                            "track": track_name, "track_index": idx, "slot": session_slot,
                            "kind": "audio" if presence == "unheard_audio" else "unknown",
                            "clip_name": clip_res.get("name") or "",
                        })
                    assumptions_a.append("track %r has active Session clip override (slot %d) playing over arrangement" % (track_name, session_slot))
                except Exception:
                    pass
                continue

            try:
                t_listing = await session.call("ppal-read-track", pp_session.requests_for(
                    session).read_track(idx, include=_arrangement_track_include(
                        bool(session.capabilities.get("live_api")))))
                t_res = pp_session._result(t_listing)
                track_type = t_res.get("type") if isinstance(t_res, dict) else None
                clips_raw = pp_session._collection(t_res, ("arrangementClips", "arrangement-clips", "arrangement_clips"))
                matching = select_arrangement_clips(clips_raw, target_bar, beats_per_bar=bpb,
                                                    meter=sig)
                for clip in matching:
                    cid = clip.get("id")
                    cname = str(clip.get("name") or "")
                    if track_type == "audio" or clip.get("type") == "audio":
                        unheard_a.append({
                            "track": track_name, "track_index": idx, "slot": 0,
                            "kind": "audio", "clip_name": cname,
                        })
                        continue
                    live = await _live_clip_or_none(session, cid)
                    if live is not None:
                        # The Live API reads the whole clip; Producer Pal's read
                        # would stop at [-L, 2L). A failed read is unread, never
                        # the Producer Pal window.
                        if live.get("error"):
                            unread_a.append(_unread_live_failure(live, track_name, idx, 0, cname))
                        else:
                            frame = live["frame"]
                            split = pp_session.split_audible_notes(
                                {"timeSignature": "%d/%d" % (frame["num"], frame["den"])}, [],
                                live=live)
                            if split["audible"] or split["hidden"]:
                                _file_split(split, track=track_name, track_index=idx, slot=0,
                                            clip_name=cname, heard=tracks_a, hidden=hidden_a,
                                            unread=unread_a)
                        continue
                    craw = await session.call("ppal-read-clip", {
                        **pp_session.clip_locator(clip_id=cid),
                        "include": ["notes", "timing"]})
                    c_notes = pp_session.extract_notes(craw) if hasattr(pp_session, "extract_notes") else []
                    if not c_notes:
                        c_notes = live_write._extract_notes(craw) or []
                    clip_rec = pp_session.arrangement_clip_record(craw, clip_index=0) if hasattr(pp_session, "arrangement_clip_record") else clip
                    clip_rec = clip_rec if isinstance(clip_rec, dict) else clip
                    if isinstance(clip_rec, dict) and "timeSignature" not in clip_rec:
                        clip_rec["timeSignature"] = sig
                    presence = live_write.classify_session_clip(craw, c_notes)
                    if presence in _READ_PRESENCES:
                        split = pp_session.split_audible_notes(clip_rec, c_notes)
                        _file_split(split, track=track_name, track_index=idx, slot=0,
                                    clip_name=cname, heard=tracks_a, hidden=hidden_a,
                                    unread=unread_a)
                    elif presence in ("unheard_audio", "unheard_unknown"):
                        unheard_a.append({
                            "track": track_name, "track_index": idx, "slot": 0,
                            "kind": "audio" if presence == "unheard_audio" else "unknown",
                            "clip_name": cname,
                        })
            except Exception:
                pass

        context_meta = {
            "where": "arrangement",
            "bar": target_bar,
            "bar_source": bar_source,
            "transport_playing": is_playing,
            "current_song_time": current_song_time,
            "meter": sig,
            "beats_per_bar": bpb,
            "active_slots": {},
            "overrides": overrides,
            "tracks_heard": len(tracks_a),
            "tracks_scanned": n_tracks,
            "tracks_total": total_tracks,
        }
        m = build_moment(tracks_a, tempo, unheard=unheard_a, scale=scale, context=context_meta)
        m["hidden"] = hidden_a
        m["unread"] = unread_a
        _unread_assumptions(m, unread_a)
        for card in hidden_a:
            bars_span = card.get("hidden_bars") or [None, None]
            m["assumptions"].append(
                "clip %r on track %r: %d note%s past the loop brace (bars %s–%s) — not playing"
                % (card["clip_name"], card["track"], card["n_hidden"],
                   "" if card["n_hidden"] == 1 else "s", bars_span[0], bars_span[1]))
        m["assumptions"].append(
            "arrangement clips at bar %d (%s) across %d track%s (%d clip%s heard)"
            % (target_bar, bar_source, n_tracks, "" if n_tracks == 1 else "s",
               len(tracks_a), "" if len(tracks_a) == 1 else "s"))
        if is_playing is False:
            m["assumptions"].append(
                "transport is stopped — bar %d comes from the playhead, nothing is playing" % target_bar)
        if sig != "4/4":
            m["assumptions"].append("song meter %s (%.1f beats per bar)" % (sig, bpb))
        if total_tracks > max_tracks:
            m["assumptions"].append("set has %d regular tracks; read the first %d only" % (total_tracks, max_tracks))
        m["assumptions"].extend(assumptions_a)
        return m

    # where == "session"
    tracks_s: list[dict] = []
    unheard_s: list[dict] = []
    hidden_s: list[dict] = []
    unread_s: list[dict] = []
    active_slots: dict[int, int] = {}
    muted_tracks: list[str] = []

    for idx in range(n_tracks):
        track_name = track_names.get(idx) or ("track %d" % idx)
        is_muted = False
        playing_slot = None

        if session.capabilities.get("live_api"):
            try:
                t_ops = [
                    {"type": "getProperty", "property": "mute"},
                    {"type": "getProperty", "property": "playing_slot_index"},
                ]
                t_receipts = await live_write._live_api_results(session, "live_set tracks %d" % idx, t_ops)
                if len(t_receipts) >= 2:
                    is_muted = bool(t_receipts[0].get("result"))
                    ps_val = t_receipts[1].get("result")
                    if isinstance(ps_val, int) and ps_val >= 0:
                        playing_slot = ps_val
            except Exception:
                pass

        if playing_slot is None:
            try:
                t_listing = await session.call("ppal-read-track", pp_session.requests_for(
                    session).read_track(idx, include=["session-clips"]))
                t_res = pp_session._result(t_listing)
                if isinstance(t_res, dict):
                    if not is_muted and (t_res.get("mute") or t_res.get("isMuted")):
                        is_muted = True
                    psi = t_res.get("playingSlotIndex", t_res.get("playing_slot_index"))
                    if isinstance(psi, int) and psi >= 0:
                        playing_slot = psi
                    if playing_slot is None:
                        s_clips = pp_session._collection(t_res, ("sessionClips", "session-clips", "session_clips"))
                        for sc in s_clips:
                            if isinstance(sc, dict) and (sc.get("isPlaying") is True or sc.get("is_playing") is True):
                                slot_str = pp_codec.clip_session_slot(sc)
                                if slot_str and "/" in slot_str:
                                    try:
                                        playing_slot = int(slot_str.split("/", 1)[1])
                                        break
                                    except ValueError:
                                        pass
            except Exception:
                pass

        if is_muted:
            muted_tracks.append(track_name)
            continue

        if playing_slot is None:
            continue

        active_slots[idx] = playing_slot
        try:
            raw_clip = await session.call("ppal-read-clip", {
                **pp_session.clip_locator(track=idx, scene=playing_slot),
                "include": ["notes", "timing"],
            })
            clip_res = pp_session.clip_record(raw_clip) if isinstance(raw_clip, pp_session.PPResponse) else {}
            if isinstance(clip_res, dict) and "timeSignature" not in clip_res:
                clip_res["timeSignature"] = sig
            all_notes = pp_session.extract_notes(raw_clip) if hasattr(pp_session, "extract_notes") else []
            if not all_notes:
                all_notes = live_write._extract_notes(raw_clip) or []

            if isinstance(clip_res, dict) and (clip_res.get("muted") or clip_res.get("isMuted")):
                continue

            presence = live_write.classify_session_clip(raw_clip, all_notes)
            live = (await _live_clip_or_none(session, clip_res.get("id"))
                    if presence in _READ_PRESENCES and isinstance(clip_res, dict) else None)
            if live is not None and live.get("error"):
                unread_s.append(_unread_live_failure(
                    live, track_name, idx, playing_slot, str(clip_res.get("name") or "")))
            elif presence in _READ_PRESENCES:
                split = pp_session.split_audible_notes(clip_res, all_notes, live=live)
                _file_split(split, track=track_name, track_index=idx, slot=playing_slot,
                            clip_name=str(clip_res.get("name") or ""),
                            heard=tracks_s, hidden=hidden_s, unread=unread_s)
            elif presence == "unheard_audio":
                unheard_s.append(_unheard_card_from_clip(
                    {"raw": raw_clip, "slot": playing_slot, "name": clip_res.get("name") if isinstance(clip_res, dict) else ""},
                    idx, "audio", track_names
                ))
            elif presence == "unheard_unknown":
                unheard_s.append(_unheard_card_from_clip(
                    {"raw": raw_clip, "slot": playing_slot, "name": clip_res.get("name") if isinstance(clip_res, dict) else ""},
                    idx, "unknown", track_names
                ))
        except Exception as exc:
            if live_write.is_empty_slot_error(exc):
                continue
            raise

    context_meta = {
        "where": "session",
        "transport_playing": is_playing,
        "current_song_time": current_song_time,
        "meter": sig,
        "beats_per_bar": bpb,
        "active_slots": {str(k): v for k, v in active_slots.items()},
        "overrides": [],
        "tracks_heard": len(tracks_s),
        "tracks_scanned": n_tracks,
        "tracks_total": total_tracks,
    }
    m = build_moment(tracks_s, tempo, unheard=unheard_s, scale=scale, context=context_meta)
    m["hidden"] = hidden_s
    m["unread"] = unread_s
    _unread_assumptions(m, unread_s)
    for card in hidden_s:
        bars_span = card.get("hidden_bars") or [None, None]
        m["assumptions"].append(
            "clip %r on track %r: %d note%s past the loop brace (bars %s–%s) — not "
            "playing; density and key were read from the audible notes only"
            % (card["clip_name"], card["track"], card["n_hidden"],
               "" if card["n_hidden"] == 1 else "s", bars_span[0], bars_span[1]))

    if not active_slots:
        m["assumptions"].append(
            "no active Session slots currently playing (or unknown active context); not substituting session row 0")
    else:
        m["assumptions"].append(
            "heard %d playing Session slot%s across %d regular track%s"
            % (len(tracks_s), "" if len(tracks_s) == 1 else "s", n_tracks, "" if n_tracks == 1 else "s"))

    if muted_tracks:
        m["assumptions"].append(
            "%d track%s muted (%s) — omitted from audible context"
            % (len(muted_tracks), "" if len(muted_tracks) == 1 else "s", ", ".join(muted_tracks[:3])))
    if sig != "4/4":
        m["assumptions"].append("song meter %s (%.1f beats per bar)" % (sig, bpb))
    if total_tracks > max_tracks:
        m["assumptions"].append("set has %d regular tracks; heard/named the first %d only" % (total_tracks, max_tracks))
    return m


def get_moment(where: str = "session", bar: int | None = None, session: Any = None) -> dict:
    """Sync entry: the live room, or a declared-blind moment. Never raises."""
    try:
        from mcp_server import pp_session as _pp_session
        return _pp_session.run_coro_blocking(
            read_moment_live(where=where, bar=bar, session=session))
    except Exception as exc:
        return blind_moment("%s: %s" % (type(exc).__name__, exc))


# --- piece snapshot (plan batch8 1.3) -------------------------------------------

PIECE_OVERVIEW_INCLUDE = ["tracks", "routings", "mixer", "locators"]
PIECE_TRACK_INCLUDE = ["arrangement-clips", "notes", "timing", "sample",
                       "devices", "routings", "mixer"]


def _piece_track_include(live_api: bool) -> list[str]:
    """The snapshot's track read. With the Live API on, every placed MIDI
    clip's notes are re-read whole through it, so Producer Pal's [-L, 2L)
    notes are not fetched (a track that still needs them is re-read with
    them, see _piece_collect)."""
    return [i for i in PIECE_TRACK_INCLUDE if i != "notes"] if live_api \
        else list(PIECE_TRACK_INCLUDE)


def _needs_producer_pal_notes(read: dict, clip_reads: dict) -> bool:
    """A track read without notes whose MIDI material the Live API did not
    read whole: a placed clip without an id or whose Live API read failed
    (Producer Pal's window is its fallback), or a take-lane clip (only placed
    clips are read through the Live API)."""
    if not isinstance(read, dict) or read.get("type") == "audio":
        return False

    def midi(clip):
        return (isinstance(clip, dict) and clip.get("type") != "audio"
                and "sampleFile" not in clip)
    for clip in read.get("arrangementClips") or []:
        if midi(clip):
            live = clip_reads.get(str(clip.get("id"))) if clip.get("id") is not None else None
            if not isinstance(live, dict) or live.get("error"):
                return True
    return any(midi(clip) for lane in read.get("takeLanes") or [] if isinstance(lane, dict)
               for clip in lane.get("clips") or [])
PIECE_RETURN_INCLUDE = ["devices", "routings", "mixer"]
PIECE_MASTER_INCLUDE = ["devices", "mixer"]
PIECE_TRANSPORT_PROPERTIES = ("current_song_time", "is_playing", "loop",
                              "loop_start", "loop_length")


def _piece_result(response, label: str) -> dict:
    import pp_session
    from mcp_server import piece_snapshot as _ps
    issues = pp_session.response_warnings_mean_incomplete(response)
    if issues:
        raise _ps.PieceSnapshotError("%s read incomplete: %s" % (label, "; ".join(issues)))
    result = pp_session._result(response)
    if not isinstance(result, dict):
        raise _ps.PieceSnapshotError("%s read is malformed" % label)
    return result


async def _piece_identity(session) -> tuple[dict, dict]:
    """(identity, overview): structural fingerprint always; Live API identity when on."""
    import live_write
    from mcp_server import piece_snapshot as _ps
    live = None
    if session.capabilities.get("live_api"):
        found = await live_write.read_set_identity(session)
        live = {key: found.get(key) for key in ("set_id", "runtime_epoch", "file_path",
                                                "live_root_id")}
    overview = _piece_result(await session.call(
        "ppal-read-live-set", {"include": list(PIECE_OVERVIEW_INCLUDE)}), "Set overview")
    return {"structural": _ps.structural_fingerprint(overview), "live": live}, overview


async def _piece_collect(session, overview: dict) -> dict:
    import live_write
    import pp_session
    requests = pp_session.requests_for(session)
    track_reads = {}
    track_include = _piece_track_include(bool(session.capabilities.get("live_api")))
    for track in overview.get("tracks") or []:
        index = pp_session.track_index_of(track)      # 2.2 trackIndex, 2.3 path 't3'
        if index is None:
            continue
        track_reads[index] = _piece_result(await session.call(
            "ppal-read-track", requests.read_track(index, include=track_include)),
            "track %d" % index)
    return_reads = {}
    for track in overview.get("returnTracks") or []:
        index = pp_session.return_index_of(track)     # 2.2 returnTrackIndex, 2.3 'rt0'
        if index is None:
            continue
        return_reads[index] = _piece_result(await session.call(
            "ppal-read-track", requests.read_track(
                index, kind="return", include=PIECE_RETURN_INCLUDE)), "return %d" % index)
    master_read = None
    if pp_session.main_track_of(overview) is not None:  # 2.2 masterTrack, 2.3 mainTrack
        master_read = _piece_result(await session.call(
            "ppal-read-track", requests.read_track(kind="main", include=PIECE_MASTER_INCLUDE)),
            "main track")
    clip_reads, notes_reason = None, None
    if session.capabilities.get("live_api"):
        # Producer Pal's track read carries notes only inside [-L, 2L): re-read
        # every placed MIDI clip whole (one ppal-live-api request per clip).
        clip_reads = {}
        for read in track_reads.values():
            if read.get("type") == "audio":
                continue
            for clip in read.get("arrangementClips") or []:
                if (isinstance(clip, dict) and clip.get("id") is not None
                        and clip.get("type") != "audio" and "sampleFile" not in clip):
                    clip_reads[str(clip["id"])] = await live_write.read_clip_live(
                        session, clip["id"])
        if "notes" not in track_include:
            for index, read in list(track_reads.items()):
                if _needs_producer_pal_notes(read, clip_reads):
                    track_reads[index] = _piece_result(await session.call(
                        "ppal-read-track", requests.read_track(
                            index, include=PIECE_TRACK_INCLUDE)),
                        "track %d" % index)
    else:
        notes_reason = ("ppal-live-api is off for this Set in Producer Pal, so notes outside "
                        "that window cannot be read")
    selection = pp_session._result(await session.call("ppal-select", {}))
    transport, reason = None, None
    if session.capabilities.get("live_api"):
        receipts = await live_write._live_api_results(session, "live_set", [
            {"type": "getProperty", "property": prop} for prop in PIECE_TRANSPORT_PROPERTIES])
        transport = {receipt.get("operation", {}).get("property", prop): receipt.get("result")
                     for prop, receipt in zip(PIECE_TRANSPORT_PROPERTIES, receipts)}
    else:
        reason = ("playhead and loop brace need the Live API (ppal-live-api is off "
                  "for this Set in Producer Pal)")
    return {"track_reads": track_reads, "return_reads": return_reads,
            "master_read": master_read, "selection": selection,
            "transport": transport, "transport_reason": reason,
            "clip_reads": clip_reads, "notes_reason": notes_reason}


async def read_piece_snapshot(session: Any = None, *, read_saved_set: bool = True) -> dict:
    """Read-only structural snapshot of the open Arrangement (tracks, clips,
    sections, layers, routing, transport, selection, digests).

    One operation_session. Set identity is read before and after the
    collection — the Live API identity (Set file + Live root/process) when
    ppal-live-api is on, and always a structural fingerprint of the overview
    (track ids/names/order/grouping/clip counts, tempo, meter, locators; weaker:
    it cannot tell two same-shaped Sets apart or see a clip moved mid-read).
    A mismatch, or a per-track read that is not the track the overview listed,
    re-collects once; a second mismatch raises ``SetChangedWhileReading``
    ("the Set changed while reading"). Never writes to Live or the store.
    The pure builder is ``piece_snapshot.build_piece_snapshot``.
    """
    import live_write
    import pp_session
    from mcp_server import piece_snapshot as _ps

    if session is None:
        async with pp_session.operation_session(live_write.PP_URL) as owned:
            return await read_piece_snapshot(owned, read_saved_set=read_saved_set)

    reasons: list[str] = []
    for attempt in (1, 2):
        before, overview = await _piece_identity(session)
        raw = await _piece_collect(session, overview)
        after, _ = await _piece_identity(session)
        attempt_reasons = (_ps.identity_mismatch(before, after)
                           + _ps.reads_consistent(overview, raw["track_reads"])
                           + _ps.live_clips_consistent(overview, raw["track_reads"],
                                                       raw["clip_reads"]))
        if not attempt_reasons:
            break
        reasons.extend("attempt %d: %s" % (attempt, item) for item in attempt_reasons)
    else:
        raise _ps.SetChangedWhileReading(
            "%s (twice) — %s" % (_ps.SET_CHANGED_MESSAGE, "; ".join(reasons)))

    live = before["live"]
    identity = {
        "basis": "live_api" if live else "structural",
        "verified": bool(live),
        "set_id": live.get("set_id") if live else None,
        "file_path": live.get("file_path") if live else None,
        "runtime_epoch": live.get("runtime_epoch") if live else None,
        "structural_digest": before["structural"]["digest"],
        "attempts": attempt,
        "retried_because": reasons,
        "note": (None if live else
                 "Live API off: identity is a structural fingerprint of the overview "
                 "(weaker — cannot tell two same-shaped Sets apart); the Set file path "
                 "is unknown"),
    }
    snapshot = _ps.build_piece_snapshot(overview, identity=identity, **raw)
    path = identity["file_path"]
    if read_saved_set and path:
        import os
        if os.path.isfile(path):
            try:
                import tools.live_set_reader as _reader
            except ModuleNotFoundError:
                _reader = None
            if _reader is not None:
                try:
                    _ps.sidechains_from_reader(snapshot, _reader.read_live_set(path),
                                               file_path=path)
                except Exception as exc:  # the .als is optional enrichment
                    snapshot["sidechains"] = {"status": "unknown", "file_path": path,
                                              "reason": "saved .als unreadable: %s" % exc}
        else:
            snapshot["sidechains"] = {"status": "unknown", "file_path": path,
                                      "reason": "the saved .als is not on this disk"}
    return snapshot
