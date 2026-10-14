"""Piece snapshot: a read-only structural picture of the open Arrangement.

Pure builders only — no Producer Pal, no store, no clock. The Producer Pal
reader is ``moment.read_piece_snapshot``; it collects raw responses and hands
them to :func:`build_piece_snapshot`. Everything a later step relies on
(sections, layer counts, routing hazards, the two digests) is computed here so
it can be tested with plain dicts.

Conventions (read these before consuming the snapshot):

* Beats are quarter-note beats from the song start (0-based), like Live's API.
* Bars are 1-based positions on Live's ruler. A clip placed at 33|1 for 4 bars
  has ``start_bar`` 33 and ``end_bar`` 37 (where it stops, as Live shows it).
  Sections carry ``bars: [first, last]`` — both inclusive.
* ``layer_matrix`` is the raw output of ``piece_context.layer_matrix`` — the same
  pure function precedents use — so its ``bars``/``entries``/``exits``/
  ``sections_guess`` are ZERO-based. The friendly ``layers`` view is 1-based.
* Audio clips are named and located, never heard.
* MIDI notes: Producer Pal returns a clip's notes only inside [-L, 2L) of
  clip-content time. Each MIDI clip carries ``coverage`` (full | partial |
  unseen: how much of its played region that window saw) and ``notes_source``.
  With the Live API every arrangement MIDI clip is re-read whole through
  ``ppal-live-api`` (``notes_source`` ``live_api``); without it a clip whose
  coverage is not full is ``notes_status: "unread"`` — its notes are neither
  audible nor hidden — and its track is in ``source_integrity.blind``.
  Clip ``notes`` / ``audible_notes`` are in clip-content QUARTER beats
  (Producer Pal's clip-meter ``t`` is converted); :func:`clip_events` places
  the audible ones on the song timeline.
* Sidechains live only in the saved .als. Until it is read
  (``sidechains.status`` ``from_saved_als``) every track of his carries a
  ``sidechain_unknown`` hazard; after it is read, edits since the last save
  are still invisible.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from mcp_server import clip_playback as cp
from mcp_server import pp_codec, pp_session
from mcp_server.arrangement_exec import _position_free
from mcp_server.piece_context import layer_matrix


SIDEKICK_PREFIX = "SIDEKICK"
SET_CHANGED_MESSAGE = "the Set changed while reading"
# A section boundary needs this many of his layers to enter or leave together.
SECTION_TURNOVER = 2
# Inferred sections shorter than this merge into the previous one (silence excepted).
SECTION_MIN_BARS = 2
_SAMPLE_KEYS = ("sampleFile", "gainDb", "pitchShift", "sampleLength", "sampleRate",
                "warping", "warpMode")
_MAIN_OUTPUTS = {"main", "master"}
_NO_OUTPUT = {"no output"}
_SENDS_ONLY = {"sends only"}
# Live's own names for its hardware / computer ports: the only true externals.
# Any other unmatched name (a renamed port, a device, a deleted track) is unknown.
_EXTERNAL_INPUTS = {"ext. in", "all ins", "computer keyboard"}
_EXTERNAL_OUTPUTS = {"ext. out"}
_RESAMPLING = {"resampling"}
_ROUTING_KEYS = ("inputRoutingType", "inputRoutingChannel",
                 "outputRoutingType", "outputRoutingChannel")
# Not source content: mute/arm/monitor are flipped by auditions, routing is
# checked by freshness, names and indices move.
_SOURCE_EXCLUDED_TRACK_KEYS = frozenset(_ROUTING_KEYS) | {
    "name", "state", "isArmed", "monitoringState", "sessionClipCount",
    "arrangementClipCount", "trackIndex", "path", "color"}   # path: the 2.3 index
# Placement, not content: where a clip sits and how long it is laid out. Names
# are labels, not material (renames never count, as in the ledger's key).
_SOURCE_EXCLUDED_CLIP_KEYS = frozenset({
    "id", "name", "arrangementStart", "arrangementLength", "path", "trackIndex", "color"})


class SetChangedWhileReading(RuntimeError):
    """The Set's identity or structure differed before and after collection."""


class PieceSnapshotError(RuntimeError):
    """A read the snapshot depends on is missing or malformed."""


# --- small pure helpers --------------------------------------------------------

def meter_of(signature: Any) -> tuple[str, int, int, float]:
    """(signature, numerator, denominator, quarter beats per bar); refuses bad meters."""
    if not isinstance(signature, str) or "/" not in signature:
        raise PieceSnapshotError("the Set reports no time signature; cannot place bars")
    try:
        num, den = (int(part.strip()) for part in signature.split("/", 1))
    except ValueError:
        raise PieceSnapshotError("unreadable time signature %r" % signature) from None
    if num <= 0 or den <= 0 or den & (den - 1):
        raise PieceSnapshotError("unreadable time signature %r" % signature)
    return "%d/%d" % (num, den), num, den, num * 4.0 / den


def position_beats(bar_beat: str, num: int, den: int) -> float:
    """Producer Pal bar|beat (song meter; the beat is the meter's beat) → quarter beats."""
    bar_s, beat_s = str(bar_beat).split("|", 1)
    return (int(bar_s) - 1) * num * 4.0 / den + (float(beat_s) - 1) * 4.0 / den


def bar_beat(beats: float, num: int, den: int) -> str:
    """Quarter beats → bar|beat in the song meter (inverse of position_beats)."""
    bpb = num * 4.0 / den
    beats = max(0.0, float(beats))
    bar = int(math.floor(beats / bpb + 1e-9))
    beat = (beats - bar * bpb) * den / 4.0 + 1
    beat = round(beat, 4)
    return "%d|%s" % (bar + 1, int(beat) if beat == int(beat) else beat)


def _bar_of(beats: float, bpb: float) -> float:
    value = beats / bpb + 1
    return int(round(value)) if abs(value - round(value)) < 1e-9 else round(value, 4)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) else None
    return None


# --- identity ------------------------------------------------------------------

def structural_fingerprint(overview: dict) -> dict:
    """A structural fingerprint of the ``ppal-read-live-set`` overview.

    Weaker than Live-API identity: it cannot tell two Sets of the same shape
    apart, and it does not see a clip moved without changing a track's clip
    count. It does see tracks added/removed/renamed/reordered, grouping,
    input/output routing, arrangement clip counts, tempo, meter, returns, the
    main track and locators. Mute/solo state and monitoring are left out on
    purpose: they are what an audition flips, and a flip during a long read
    must not refuse the snapshot — :func:`proposal_freshness` covers them.
    """
    if not isinstance(overview, dict) or not isinstance(overview.get("tracks"), list):
        raise PieceSnapshotError("the Set overview carries no track list")
    # Positions through pp_codec's readers: the same Set digests the same on
    # 2.2 (trackIndex, masterTrack) and 2.3 (path 't3', mainTrack).
    tracks = [[t.get("id"), t.get("name"), pp_codec.track_index_of(t), t.get("type"),
               bool(t.get("isGroup")), t.get("groupId"), t.get("arrangementClipCount"),
               [_name(t.get(key)) for key in _ROUTING_KEYS]]
              for t in overview["tracks"] if isinstance(t, dict)]
    returns = [[t.get("id"), t.get("name")] for t in overview.get("returnTracks") or []
               if isinstance(t, dict)]
    master = pp_codec.main_track_of(overview) or {}
    locators = [[item.get("name"), item.get("time")] for item in overview.get("locators") or []
                if isinstance(item, dict)]
    fields = {"name": overview.get("name"), "tempo": overview.get("tempo"),
              "time_signature": overview.get("timeSignature"), "tracks": tracks,
              "returns": returns, "master": master.get("id"), "locators": locators}
    return {"basis": "structural", "digest": _digest(fields), "track_count": len(tracks)}


def identity_mismatch(before: dict, after: dict) -> list[str]:
    """Why two identity reads differ ([] when they agree)."""
    reasons = []
    if (before.get("structural") or {}).get("digest") != (after.get("structural") or {}).get("digest"):
        reasons.append("track list, grouping, routing, clip counts, tempo, meter, returns "
                       "or locators changed")
    live_before, live_after = before.get("live"), after.get("live")
    if (live_before is None) != (live_after is None):
        reasons.append("Live API identity became unavailable mid-read")
    elif live_before is not None:
        for key in ("set_id", "runtime_epoch"):
            if live_before.get(key) != live_after.get(key):
                reasons.append("Live Set identity (%s) changed" % key)
    return reasons


def reads_consistent(overview: dict, track_reads: dict) -> list[str]:
    """Each per-track read must be the track the overview listed at that index."""
    problems = []
    listed = {pp_codec.track_index_of(t): t for t in overview.get("tracks") or []
              if isinstance(t, dict)}
    for index, track in listed.items():
        read = track_reads.get(index)
        if not isinstance(read, dict):
            problems.append("track %s was not read" % index)
        elif read.get("id") != track.get("id"):
            problems.append("track %s is %r in the overview but %r when read"
                            % (index, track.get("id"), read.get("id")))
    return problems


# --- clips ---------------------------------------------------------------------

def _clip_region(clip: dict) -> dict:
    """Clip content region (loop brace) as Producer Pal reports it: bar|beat in
    the clip's own meter, and quarter beats of clip-content time."""
    signature = clip.get("timeSignature")
    meter = cp.parse_signature(signature)
    region = {"looping": clip.get("looping") if isinstance(clip.get("looping"), bool) else None,
              "start": clip.get("start"), "end": clip.get("end"),
              "start_beats": None, "end_beats": None, "clip_meter": signature}
    if meter is not None:
        for key in ("start", "end"):
            value = clip.get(key)
            if isinstance(value, str) and "|" in value:
                try:
                    region[key + "_beats"] = cp.bar_beat_quarters(value, *meter)
                except (TypeError, ValueError):
                    pass
    return region


def _pp_notes_quarters(notes: list[dict], den: int) -> list[dict]:
    scale = 4.0 / den
    return [{"pitch": n["pitch"], "start_beats": n["start_beats"] * scale,
             "duration_beats": n["duration_beats"] * scale, "velocity": n["velocity"],
             "mute": False} for n in notes]


_FRAME_KEYS = ("looping", "loop_start", "loop_end", "start_marker", "end_marker",
               "start_marker_known", "arr_start", "arr_end", "source")


def _midi_notes(record: dict, raw: dict, *, num: int, den: int, live: dict | None) -> None:
    """Fill a MIDI clip record's notes, coverage and playback from the Producer
    Pal read and, when present, the complete Live API read of the same clip."""
    pp_notes = []
    if isinstance(raw.get("notes"), str) and raw["notes"].strip():
        try:
            pp_notes = pp_codec.midi_json_to_notes(raw["notes"])
        except ValueError as exc:
            raise PieceSnapshotError("clip %r notes unreadable: %s" % (raw.get("id"), exc)) from None
    song = "%d/%d" % (num, den)
    pp_frame = cp.frame_from_pp(raw, song_signature=song)
    clip_den = pp_frame["den"] if pp_frame else den
    live_ok = isinstance(live, dict) and not live.get("error") and live.get("frame")
    if live_ok:
        frame = dict(live["frame"])
        notes = [dict(n) for n in live["notes"]]
        coverage = cp.coverage({**frame, "source": "producer_pal"})
    else:
        frame = pp_frame
        notes = _pp_notes_quarters(pp_notes, clip_den)
        coverage = cp.coverage(frame) if frame else None
    record["notes"] = notes
    live_failed = isinstance(live, dict) and not live_ok
    # a failed Live API read leaves Producer Pal's [-L, 2L) window: complete
    # for what plays when coverage is full, blind to anything outside it
    record["notes_source"] = ("live_api" if live_ok else
                              "producer_pal_window" if live_failed else "producer_pal")
    record["coverage"] = coverage
    record["live_error"] = live.get("error") if isinstance(live, dict) else None
    if frame is None:       # no brace reported: every note plays, nothing to place
        record.update(notes_status="read", unread_reason=None, audible_notes=notes,
                      notes_audible=len(notes), notes_hidden=0, notes_muted=0, playback=None)
        return
    split = cp.split(frame, notes, quarters=True)
    playback = {key: frame.get(key) for key in _FRAME_KEYS}
    playback.update(played_region=split["played_region"], read_window=split["read_window"])
    record["playback"] = playback
    if split["unread"]:
        reason = split["unread_reason"]
        if record["live_error"]:
            reason += "; the Live API read failed: %s" % record["live_error"]
        record.update(notes_status="unread", unread_reason=reason, audible_notes=None,
                      notes_audible=None, notes_hidden=None, notes_muted=None)
        return
    record.update(notes_status="read", unread_reason=None, audible_notes=split["audible"],
                  notes_audible=len(split["audible"]), notes_hidden=len(split["hidden"]),
                  notes_muted=len(split["muted"]))


def clip_events(clip: dict) -> list[dict]:
    """A MIDI clip record's audible notes placed on the song timeline:
    ``[{pitch, start, duration, velocity, clip_start}]`` in song quarter beats
    (first pass from the start marker, loop repeats, cut at the clip's
    arrangement end). ``[]`` for unread clips, audio clips and take-lane clips
    without placement; ``[]`` for a muted clip (it plays nothing, as the layer
    count already says)."""
    if clip.get("kind") != "midi" or clip.get("notes_status") != "read" \
            or not clip.get("playback") or clip.get("muted"):
        return []
    return cp.events(clip["playback"], clip.get("audible_notes") or [], quarters=True)


def clip_record(raw: dict, *, num: int, den: int, audio: bool, live: dict | None = None) -> dict:
    """One arrangement clip: placement in beats/bars, loop region, notes or sample.

    ``live``: the complete ppal-live-api read of this clip (live_write.read_clip_live)."""
    bpb = num * 4.0 / den
    # 2.2 arrangementStart, 2.3 the position in the clip path 't3[5|1]'
    start_s, length_s = pp_codec.arrangement_start_of(raw), raw.get("arrangementLength")
    try:
        start = position_beats(start_s, num, den)
        length = pp_codec.pp_duration_to_beats(str(length_s), bpb)
    except (AttributeError, TypeError, ValueError):
        raise PieceSnapshotError("arrangement clip %r has no readable position"
                                 % raw.get("id")) from None
    if not math.isfinite(start) or not math.isfinite(length) or length <= 0:
        raise PieceSnapshotError("arrangement clip %r has no readable length" % raw.get("id"))
    end = start + length
    is_audio = audio or raw.get("type") == "audio" or "sampleFile" in raw
    record = {
        "id": raw.get("id"),
        "name": raw.get("name") if isinstance(raw.get("name"), str) else "",
        "kind": "audio" if is_audio else "midi",
        "start": start_s, "length": length_s,
        "start_beats": start, "end_beats": end,
        "start_bar": _bar_of(start, bpb), "end_bar": _bar_of(end, bpb),
        "bar_aligned": (abs(start / bpb - round(start / bpb)) < 1e-9
                        and abs(end / bpb - round(end / bpb)) < 1e-9),
        "sub_bar": length < bpb - 1e-9,
        "region": _clip_region(raw),
        "muted": raw.get("state") == "muted" or raw.get("muted") is True,
    }
    if is_audio:
        record["heard"] = False
        sample = {key: raw[key] for key in _SAMPLE_KEYS if key in raw}
        record["sample"] = sample or None
        return record
    _midi_notes(record, raw, num=num, den=den, live=live)
    return record


def live_clips_consistent(overview: dict, track_reads: dict, clip_reads: dict) -> list[str]:
    """A Live API clip read must be the clip Producer Pal placed: same start and
    end on the song timeline, and a MIDI clip. Anything else means the Set
    changed between the two reads."""
    if not clip_reads:
        return []
    try:
        _, num, den, bpb = meter_of(overview.get("timeSignature"))
    except PieceSnapshotError:
        return []
    placed = {}
    for read in track_reads.values():
        for clip in (read or {}).get("arrangementClips") or []:
            if isinstance(clip, dict):
                placed[str(clip.get("id"))] = clip
    problems = []
    for clip_id, live in sorted(clip_reads.items()):
        if not isinstance(live, dict) or live.get("error"):
            continue
        clip = placed.get(str(clip_id))
        props = live.get("props") or {}
        if clip is None:
            problems.append("clip %s was read through the Live API but is not placed" % clip_id)
            continue
        try:
            start = position_beats(pp_codec.arrangement_start_of(clip), num, den)
            end = start + pp_codec.pp_duration_to_beats(str(clip["arrangementLength"]), bpb)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        frame = live["frame"]
        if (frame["arr_start"] is None or abs(frame["arr_start"] - start) > 1e-3
                or frame["arr_end"] is None or abs(frame["arr_end"] - end) > 1e-3):
            problems.append("clip %s sits at %s-%s in Producer Pal's read but %s-%s through "
                            "the Live API" % (clip_id, start, end, frame["arr_start"],
                                              frame["arr_end"]))
        elif not props.get("is_midi_clip"):
            problems.append("clip %s is not a MIDI clip through the Live API" % clip_id)
    return problems


# --- routing -------------------------------------------------------------------

def _route(kind: str, target: str | None = None, target_ids: list | None = None) -> dict:
    return {"kind": kind, "target": target, "target_ids": list(target_ids or [])}


def classify_routing(track: dict, by_name: dict, groups: dict,
                     returns: dict | None = None) -> dict:
    """Output/input routing as main | group | track | return | resampling |
    sends_only | none | external | unknown | ambiguous.

    A routing that names another track (MIDI out to a synth track, audio in
    from a track) is a hazard for any experiment that mutes either side. A
    name matching several tracks is ``ambiguous`` — never guessed. ``returns``
    maps return-track names to ids (``return``); Resampling hears the main
    mix. Only Live's own port names (Ext. In/Out, All Ins, Computer Keyboard)
    are ``external``; any other unmatched name is ``unknown``.
    """
    own_id = track.get("id")
    returns = returns or {}

    def classify(name, *, output):
        if not isinstance(name, str):
            return _route("unknown")
        low = name.strip().casefold()
        if not low:
            return _route("unknown", name)
        if output and low in _MAIN_OUTPUTS:
            return _route("main", name)
        if output and low in _NO_OUTPUT:
            return _route("none", name)
        if output and low in _SENDS_ONLY:
            return _route("sends_only", name)
        if not output and low in _RESAMPLING:
            return _route("resampling", name)
        group_id = track.get("groupId")
        if output and group_id and groups.get(group_id) == name:
            return _route("group", name, [group_id])
        ids = [tid for tid in by_name.get(name, []) if tid != own_id]
        if len(ids) == 1:
            return _route("track", name, ids)
        if len(ids) > 1:
            return _route("ambiguous", name, ids)
        if name in returns:
            return _route("return", name, [returns[name]])
        if low in (_EXTERNAL_OUTPUTS if output else _EXTERNAL_INPUTS):
            return _route("external", name)
        return _route("unknown", name)

    has_output = "outputRoutingType" in track
    has_input = "inputRoutingType" in track
    return {
        "output": classify(_name(track.get("outputRoutingType")), output=True)
        if has_output else None,
        "output_channel": _name(track.get("outputRoutingChannel")),
        "input": classify(_name(track.get("inputRoutingType")), output=False)
        if has_input else None,
        "input_channel": _name(track.get("inputRoutingChannel")),
        "monitoring": track.get("monitoringState"),
    }


# --- tracks --------------------------------------------------------------------

def _track_type(read: dict, listed: dict) -> str:
    if read.get("isGroup") or listed.get("isGroup"):
        return "group"
    kind = read.get("type") or listed.get("type")
    return kind if kind in ("midi", "audio", "return", "master") else "unknown"


def _devices(read: dict) -> list[str]:
    out = []
    for device in read.get("devices") or []:
        if isinstance(device, dict):
            kind = device.get("type") or device.get("name") or ""
            out.append(str(kind))
    return out


def _sends(read: dict) -> list[dict]:
    return [{"return": send.get("return"), "gain_db": send.get("gainDb")}
            for send in read.get("sends") or [] if isinstance(send, dict)]


def track_record(read: dict, listed: dict, *, num: int, den: int, by_name: dict,
                 groups: dict, clip_reads: dict | None = None,
                 returns: dict | None = None) -> dict:
    kind = _track_type(read, listed)
    name = read.get("name") if isinstance(read.get("name"), str) else listed.get("name")
    state = read.get("state", listed.get("state"))
    excluded = None
    if isinstance(name, str) and name.upper().startswith(SIDEKICK_PREFIX):
        excluded = "sidekick track (ours, not his material)"
    elif read.get("hasProducerPalDevice") or listed.get("hasProducerPalDevice"):
        excluded = "hosts the Producer Pal device"
    elif kind == "group":
        excluded = "group track (its members carry the material; counted once)"
    group_id = read.get("groupId", listed.get("groupId"))
    audio = kind == "audio"
    clips_raw = read.get("arrangementClips")
    if clips_raw is None:
        clips_raw = []
    if not isinstance(clips_raw, list):
        raise PieceSnapshotError("track %r arrangement clips are malformed" % name)
    clip_reads = clip_reads or {}
    clips = [clip_record(c, num=num, den=den, audio=audio,
                         live=clip_reads.get(str(c.get("id"))))
             for c in clips_raw if isinstance(c, dict)]
    clips.sort(key=lambda c: (c["start_beats"], c["end_beats"], str(c["id"])))
    lanes = []
    for lane in read.get("takeLanes") or []:
        if isinstance(lane, dict):
            lanes.append({"name": lane.get("name"), "path": lane.get("path"),
                          "clips": [clip_record(c, num=num, den=den, audio=audio)
                                    for c in lane.get("clips") or [] if isinstance(c, dict)]})
    muted = state == "muted"
    record = {
        "id": read.get("id", listed.get("id")),
        "index": (pp_codec.track_index_of(read) if pp_codec.track_index_of(read) is not None
                  else pp_codec.track_index_of(listed)),
        "name": name,
        "type": kind,
        "group_id": group_id,
        "group": groups.get(group_id) if group_id else None,
        "state": state,
        "muted": muted,
        "soloed": state == "soloed",
        "armed": bool(read.get("isArmed", listed.get("isArmed", False))),
        "instrument": read.get("instrument", listed.get("instrument")),
        "devices": _devices(read),
        "routing": classify_routing({**listed, **read}, by_name, groups, returns),
        "sends": _sends(read),
        "mixer": {"gain_db": read.get("gainDb"), "pan": read.get("pan")},
        "his_material": excluded is None,
        "excluded_reason": excluded,
        "in_layers": excluded is None and not muted,
        "muted_by_group": None,
        "arrangement_clips": clips,
        "take_lanes": lanes,
        "feeds": [],
        "fed_by": [],
        "hazards": [],
    }
    return record


def _mute_group_members(tracks: list[dict]) -> None:
    """A member of a muted group (at any nesting depth) is silent: still his
    material, never a layer (``muted_by_group`` names the muted group)."""
    by_id = {t["id"]: t for t in tracks}
    for track in tracks:
        seen, group_id = set(), track.get("group_id")
        while group_id and group_id not in seen and group_id in by_id:
            seen.add(group_id)
            group = by_id[group_id]
            if group["muted"]:
                track["muted_by_group"] = group["name"]
                track["in_layers"] = False
                break
            group_id = group.get("group_id")


def _link_feeds(tracks: list[dict]) -> None:
    by_id = {t["id"]: t for t in tracks}
    for track in tracks:
        output = (track["routing"] or {}).get("output") or {}
        if output.get("kind") == "track":
            for target in output["target_ids"]:
                if target in by_id:
                    track["feeds"].append({"track_id": target, "track": by_id[target]["name"],
                                           "via": "output"})
                    by_id[target]["fed_by"].append({"track_id": track["id"],
                                                    "track": track["name"], "via": "output"})
        source = (track["routing"] or {}).get("input") or {}
        if source.get("kind") == "track":
            for origin in source["target_ids"]:
                if origin in by_id:
                    by_id[origin]["feeds"].append({"track_id": track["id"],
                                                   "track": track["name"], "via": "input"})
                    track["fed_by"].append({"track_id": origin, "track": by_id[origin]["name"],
                                            "via": "input"})


def _hazard(kind: str, **fields) -> dict:
    return {"type": kind, **fields}


def _sort_hazards(track: dict) -> None:
    track["hazards"].sort(key=lambda h: (h["type"], str(h.get("track_id")), _canonical(h)))


def link_hazards(tracks: list[dict]) -> None:
    """Typed per-track hazards: muting one of these tracks in an audition would
    change more than the experiment (another track hears it, or what it routes
    is unknown), so a staging step must refuse or exclude it — never pass.

    * ``output_unknown`` / ``input_unknown``: the routing is unnamed, unread or
      a name that is neither a track, a return nor one of Live's own ports —
      e.g. a MIDI track sending to a hardware port whose audio returns on an
      unknown track (a group track has no input, so its input is not judged).
    * ``input_resampling`` / ``input_from_return``: the track hears the main mix
      or a return (which hears every send): muting another track changes it.
    * ``output_ambiguous`` / ``input_ambiguous``: the name matches several tracks.
    * ``feeds_track`` / ``fed_by_track``: a track-to-track edge (``via`` output,
      incl. MIDI out to a synth track, or ``via`` input).
    * ``external_instrument_return``: a MIDI track sending to an unknown/external
      port and an audio track of the same name monitoring an external input —
      the synth the MIDI plays comes back on the audio track.
    * ``sidechain_unknown``: on every track of his while the sidechains are not
      read from the saved .als (see :func:`mark_sidechains_unknown`); replaced by
      the real edges by :func:`attach_sidechains`.
    Sidechain edges are added by :func:`attach_sidechains` when the saved .als
    is read. Even then, sidechain edits made since the last save are invisible.
    """
    for track in tracks:
        routing = track.get("routing") or {}
        output, source = routing.get("output"), routing.get("input")
        hazards = track["hazards"]
        if output is None or output.get("kind") == "unknown":
            hazards.append(_hazard("output_unknown",
                                   routing=(output or {}).get("target")))
        elif output.get("kind") == "ambiguous":
            hazards.append(_hazard("output_ambiguous", routing=output.get("target"),
                                   candidates=list(output.get("target_ids") or [])))
        if track.get("type") != "group":
            if source is None or source.get("kind") == "unknown":
                hazards.append(_hazard("input_unknown", routing=(source or {}).get("target")))
            elif source.get("kind") == "ambiguous":
                hazards.append(_hazard("input_ambiguous", routing=source.get("target"),
                                       candidates=list(source.get("target_ids") or [])))
            elif source.get("kind") == "resampling":
                hazards.append(_hazard("input_resampling", routing=source.get("target")))
            elif source.get("kind") == "return":
                hazards.append(_hazard("input_from_return", routing=source.get("target"),
                                       return_ids=list(source.get("target_ids") or [])))
        for edge in track["feeds"]:
            hazards.append(_hazard("feeds_track", track_id=edge["track_id"],
                                   track=edge["track"], via=edge["via"]))
        for edge in track["fed_by"]:
            hazards.append(_hazard("fed_by_track", track_id=edge["track_id"],
                                   track=edge["track"], via=edge["via"]))
    by_name: dict = {}
    for track in tracks:
        by_name.setdefault(str(track.get("name") or "").strip().casefold(), []).append(track)
    for track in tracks:
        routing = track.get("routing") or {}
        output = routing.get("output") or {}
        if track.get("type") != "midi" or output.get("kind") not in ("unknown", "external"):
            continue
        for other in by_name.get(str(track.get("name") or "").strip().casefold(), []):
            other_routing = other.get("routing") or {}
            if (other is track or other.get("type") != "audio"
                    or (other_routing.get("input") or {}).get("kind") != "external"
                    or other_routing.get("monitoring") != "in"):
                continue
            track["hazards"].append(_hazard("external_instrument_return",
                                            track_id=other["id"], track=other["name"],
                                            role="midi_to_hardware"))
            other["hazards"].append(_hazard("external_instrument_return",
                                            track_id=track["id"], track=track["name"],
                                            role="hardware_return"))
    for track in tracks:
        _sort_hazards(track)


SIDECHAINS_UNKNOWN_REASON = ("sidechain sources are only in the saved .als; the Set file "
                             "path needs the Live API")


def mark_sidechains_unknown(tracks: list[dict]) -> None:
    """Who ducks whom is unread: muting any track of his may change another,
    so each carries a ``sidechain_unknown`` hazard (staging refuses it)."""
    for track in tracks:
        if track.get("his_material") and not any(
                h["type"] == "sidechain_unknown" for h in track["hazards"]):
            track["hazards"].append(_hazard("sidechain_unknown"))
            _sort_hazards(track)


def hazard_tracks(tracks: list[dict]) -> list[dict]:
    return [{"track_id": t["id"], "track": t["name"],
             "types": sorted({h["type"] for h in t["hazards"]})}
            for t in tracks if t["hazards"]]


# --- sections ------------------------------------------------------------------

def infer_sections(tracks_per_bar: list, *, beats_per_bar: float,
                   turnover: int = SECTION_TURNOVER,
                   min_bars: int = SECTION_MIN_BARS) -> list[dict]:
    """Clip-boundary blocks from which layers are active per bar.

    Rule: a block starts at bar 1; a new block starts at a bar where at least
    ``turnover`` of his layers enter or leave together (the symmetric
    difference of the active-track sets of that bar and the bar before), or
    where the texture goes to or comes back from silence. A non-silence
    boundary closer than ``min_bars`` to the previous one is dropped (one
    track flickering in and out never makes a section). Every block is
    ``inferred`` — no locator says so.
    """
    sets = [set(bar) for bar in tracks_per_bar]
    if not sets:
        return []
    boundaries = [0]
    for bar in range(1, len(sets)):
        silence_edge = (not sets[bar]) != (not sets[bar - 1])
        changed = len(sets[bar] ^ sets[bar - 1]) >= turnover
        if silence_edge or (changed and bar - boundaries[-1] >= min_bars):
            boundaries.append(bar)
    boundaries.append(len(sets))
    out = []
    for left, right in zip(boundaries, boundaries[1:]):
        out.append({"bar_index_start": left, "bar_index_end": right,
                    "start_beats": left * beats_per_bar, "end_beats": right * beats_per_bar,
                    "status": "inferred", "name": None})
    return out


def split_locators(locators: list[dict], *, num: int, den: int,
                   song_end_beats: float) -> tuple[list[dict], list[dict]]:
    """``(usable, ignored)``: a locator at or after the song's end (where his
    material ends; an old end marker kept after shortening the song) can
    start no section, so it is ignored — each ignored one says where it is."""
    usable, ignored = [], []
    for item in locators:
        try:
            beats = position_beats(item["time"], num, den)
        except (KeyError, TypeError, ValueError):
            raise PieceSnapshotError("locator %r has no readable time" % (item,)) from None
        if beats < song_end_beats - 1e-9:
            usable.append(item)
        else:
            ignored.append({"name": str(item.get("name") or ""), "time": item["time"],
                            "why": "at or after the song's end"})
    return usable, ignored


def locator_sections(locators: list[dict], *, num: int, den: int,
                     song_end_beats: float) -> list[dict]:
    """Sections between consecutive locators (the last runs to the song end).

    Material before the first locator becomes its own leading section.
    Locators at or past the song end give no section (split_locators), and
    no section ever ends after the song.
    """
    locators, _ = split_locators(locators, num=num, den=den, song_end_beats=song_end_beats)
    points = sorted((position_beats(item["time"], num, den), str(item.get("name") or ""))
                    for item in locators)
    end = song_end_beats
    out = []
    if points and points[0][0] > 0:
        out.append({"start_beats": 0.0, "end_beats": points[0][0], "status": "locator",
                    "name": "before %s" % points[0][1]})
    for (start, name), following in zip(points, points[1:] + [(end, None)]):
        if following[0] > start:
            out.append({"start_beats": start, "end_beats": following[0],
                        "status": "locator", "name": name})
    return out


def _section_view(section: dict, index: int, counts: list, audio_counts: list,
                  sets: list, names: dict, bpb: float) -> dict:
    first = int(math.floor(section["start_beats"] / bpb + 1e-9))
    last = int(math.ceil(section["end_beats"] / bpb - 1e-9))  # exclusive bar index
    window = range(first, min(last, len(counts)))
    layer_values = [counts[i] for i in window]
    audio_values = [audio_counts[i] for i in window if i < len(audio_counts)]
    active = sorted({tid for i in window for tid in sets[i]},
                    key=lambda tid: str(names.get(tid, tid)))
    before = sets[first - 1] if 0 < first <= len(sets) else set()
    at = sets[first] if first < len(sets) else set()
    return {
        "index": index,
        "name": section.get("name") or "bars %d-%d" % (first + 1, last),
        "status": section["status"],
        "bars": [first + 1, last],
        "start_beats": section["start_beats"],
        "end_beats": section["end_beats"],
        "layers_min": min(layer_values) if layer_values else 0,
        "layers_max": max(layer_values) if layer_values else 0,
        "layers_mean": round(sum(layer_values) / len(layer_values), 3) if layer_values else 0.0,
        "audio_layers_max": max(audio_values) if audio_values else 0,
        "silent": not any(layer_values) and not any(audio_values),
        "tracks": [{"track_id": tid, "track": names.get(tid)} for tid in active],
        "enters": [{"track_id": tid, "track": names.get(tid)}
                   for tid in sorted(at - before, key=lambda t: str(names.get(t, t)))],
        "leaves": [{"track_id": tid, "track": names.get(tid)}
                   for tid in sorted(before - at, key=lambda t: str(names.get(t, t)))],
    }


# --- digests -------------------------------------------------------------------

def _source_clip(clip: Any) -> Any:
    if not isinstance(clip, dict):
        return clip
    return {key: value for key, value in clip.items() if key not in _SOURCE_EXCLUDED_CLIP_KEYS}


def _live_clip_content(live: dict) -> dict:
    """Position-free content of a complete Live API clip read."""
    props = live.get("props") or {}
    notes = sorted([round(n["start_beats"], 6), n["pitch"], round(n["duration_beats"], 6),
                    n["velocity"], bool(n.get("mute"))]
                   + [n.get(key) for key in cp.LIVE_NOTE_DETAIL]
                   for n in live.get("notes") or [])
    timing = {key: props.get(key) for key in (
        "loop_start", "loop_end", "start_marker", "end_marker", "looping",
        "signature_numerator", "signature_denominator", "muted")}
    return {"notes": notes, "timing": timing}


def source_integrity(track_read: dict, clip_reads: dict | None = None) -> str:
    """Position-free digest of one track's own material, from its raw read.

    Covers the stable track id, every arrangement clip's content (notes, loop
    region, looping, clip meter, audio sample fields; not clip names), take-lane
    clips, devices
    and mixer levels (gain, pan, sends) — normalized with arrangement_exec's
    ``_position_free`` like the staging fingerprint. It deliberately ignores
    where clips sit (arrangementStart/Length, clip ids), the track's index,
    name, mute/arm/monitor state and routing: moving a clip or muting the track
    for an audition is not a change to his material. Placement is what
    :func:`proposal_freshness` watches.

    ``clip_reads`` (clip id -> complete Live API read) replaces a clip's
    Producer Pal notes — which stop at [-L, 2L) — with every note and the real
    loop/start/end markers. A track whose notes were not all read gets no
    digest at all (see :func:`build_piece_snapshot`'s ``source_integrity.blind``).
    """
    if not isinstance(track_read, dict):
        raise PieceSnapshotError("track read is malformed")
    clip_reads = clip_reads or {}
    track_read = dict(track_read)
    clips = []
    for clip in track_read.get("arrangementClips") or []:
        live = clip_reads.get(str(clip.get("id"))) if isinstance(clip, dict) else None
        if isinstance(live, dict) and not live.get("error") and live.get("frame"):
            clip = {key: value for key, value in clip.items() if key != "notes"}
            clip["live_content"] = _live_clip_content(live)
        clips.append(clip)
    if "arrangementClips" in track_read:
        track_read["arrangementClips"] = clips
    value = _position_free(track_read)
    value = {key: item for key, item in value.items() if key not in _SOURCE_EXCLUDED_TRACK_KEYS}
    if isinstance(value.get("sends"), list):
        # 2.3 adds each send's return track id (returnId): identity, not material
        value["sends"] = [{k: v for k, v in send.items() if k != "returnId"}
                          if isinstance(send, dict) else send for send in value["sends"]]
    value["arrangementClips"] = sorted(
        (_source_clip(c) for c in value.get("arrangementClips") or []), key=_canonical)
    if "takeLanes" in value:
        lanes = []
        for lane in value["takeLanes"] or []:
            if isinstance(lane, dict):
                lane = dict(lane)
                lane["clips"] = sorted((_source_clip(c) for c in lane.get("clips") or []),
                                       key=_canonical)
            lanes.append(lane)
        value["takeLanes"] = lanes
    return _digest(value)


def source_integrity_map(tracks: list[dict], track_reads: dict, clip_reads: dict) -> dict:
    """``{"digests": {track id: digest}, "blind": [...]}``.

    A track with any placed MIDI clip whose notes were not read completely is
    BLIND: it has no digest (so nothing can vouch that his material is
    unchanged) and is listed with the unread clips and why. Take-lane clips are
    alternates, never played: they do not make a track blind.
    """
    digests, blind = {}, []
    for track in tracks:
        unread = [{"clip_id": c["id"], "start": c["start"], "coverage": c.get("coverage"),
                   "reason": c.get("unread_reason")}
                  for c in track["arrangement_clips"]
                  if c["kind"] == "midi" and c.get("notes_status") == "unread"]
        if unread:
            blind.append({"track_id": track["id"], "track": track["name"],
                          "reason": "notes_unread", "clips": unread})
            continue
        digests[track["id"]] = source_integrity(track_reads[track["index"]], clip_reads)
    return {"digests": digests, "blind": blind,
            "rule": "no digest for a track whose placed MIDI notes were not all read "
                    "(Producer Pal reads only [-L, 2L); the Live API reads every note)"}


def resolve_target(snapshot: dict, target: dict) -> dict:
    """The section a proposal targets, by ``section_index`` or explicit ``bars``."""
    if "section_index" in target:
        index = target["section_index"]
        sections = snapshot.get("sections") or []
        if not isinstance(index, int) or not 0 <= index < len(sections):
            raise PieceSnapshotError("no section %r in this snapshot" % (index,))
        section = sections[index]
        return {"start_beats": section["start_beats"], "end_beats": section["end_beats"],
                "status": section["status"], "bars": list(section["bars"])}
    bars = target.get("bars")
    if (not isinstance(bars, (list, tuple)) or len(bars) != 2
            or not all(isinstance(b, int) and b >= 1 for b in bars) or bars[1] < bars[0]):
        raise PieceSnapshotError("target needs section_index or bars [first, last]")
    bpb = snapshot["set"]["beats_per_bar"]
    return {"start_beats": (bars[0] - 1) * bpb, "end_beats": bars[1] * bpb,
            "status": "explicit", "bars": list(bars)}


def proposal_freshness(snapshot: dict, target: dict) -> dict:
    """Digest of everything a proposal about ``target`` depends on.

    ``target``: ``{"section_index": i | "bars": [first, last], "track_ids": [...],
    "protected_track_ids": [...], "decisions_revision": int | None}``.
    Covered: the target section bounds, the Set's tempo and meter (they time
    every section and the plan's duration), every clip placement on the
    involved tracks (start/end, loop region, looping), their routing, feeds,
    mute/solo state, the protected track list and the piece-decisions revision.
    Note content is not placement: :func:`source_integrity_map` digests it,
    and a stage compares both.
    Not covered, on purpose: the playhead, the selection and the arrangement
    loop brace — the audition itself moves the brace and playhead, and normal
    playback must not invalidate a proposal — nor clip renames. Moving one of
    an involved track's clips by 8 bars changes the digest.
    """
    by_id = {t["id"]: t for t in snapshot.get("tracks") or []}
    involved = sorted({str(tid) for tid in target.get("track_ids") or []})
    missing = [tid for tid in involved if tid not in by_id]
    tracks = {}
    for tid in involved:
        track = by_id.get(tid)
        if track is None:
            tracks[tid] = None
            continue
        tracks[tid] = {
            "placements": [[c["start_beats"], c["end_beats"], c["kind"],
                            c["region"]["start"], c["region"]["end"], c["region"]["looping"],
                            c["muted"]]
                           for c in track["arrangement_clips"]],
            "routing": track["routing"],
            "feeds": sorted(_canonical(item) for item in track["feeds"] + track["fed_by"]),
            "hazards": sorted(_canonical(item) for item in track.get("hazards") or []),
            "muted": track["muted"], "soloed": track["soloed"], "state": track["state"],
            "group_id": track["group_id"],
        }
    set_info = snapshot.get("set") or {}
    components = {
        "section": resolve_target(snapshot, target),
        "set": {"tempo": set_info.get("tempo"), "meter": set_info.get("meter"),
                "beats_per_bar": set_info.get("beats_per_bar")},
        "tracks": tracks,
        "protected_track_ids": sorted({str(t) for t in target.get("protected_track_ids") or []}),
        "decisions_revision": target.get("decisions_revision"),
    }
    return {"digest": _digest(components), "components": components, "missing_tracks": missing}


# --- the snapshot --------------------------------------------------------------

def _transport(values: dict | None, reason: str | None, *, num: int, den: int) -> dict:
    if values is None:
        return {"source": None, "reason": reason, "playhead_beats": None, "playhead": None,
                "playhead_bar": None, "is_playing": None, "loop": None}
    bpb = num * 4.0 / den

    def number(key):
        value = values.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
            and math.isfinite(float(value)) else None

    playhead = number("current_song_time")
    loop_start, loop_length = number("loop_start"), number("loop_length")
    loop = None
    if loop_start is not None and loop_length is not None:
        loop = {"on": bool(values.get("loop")) if values.get("loop") is not None else None,
                "start_beats": loop_start, "end_beats": loop_start + loop_length,
                "start": bar_beat(loop_start, num, den),
                "end": bar_beat(loop_start + loop_length, num, den)}
    playing = values.get("is_playing")
    return {"source": "live_api", "reason": None, "playhead_beats": playhead,
            "playhead": bar_beat(playhead, num, den) if playhead is not None else None,
            "playhead_bar": int(playhead // bpb) + 1 if playhead is not None else None,
            "is_playing": bool(playing) if playing is not None else None, "loop": loop}


def _selection(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    clip = raw.get("selectedClip") if isinstance(raw.get("selectedClip"), dict) else None
    arrangement_clip = None
    start = pp_codec.arrangement_start_of(clip)      # 2.2 field, 2.3 path position
    if clip is not None and start is not None:
        arrangement_clip = {"id": clip.get("id"), "start": start,
                            "track_index": pp_codec.clip_track_index(clip)}
    def placed(record, key, index):
        # 2.2 trackIndex / sceneIndex, 2.3 path 't0' / 's0': one field, whichever build
        if not isinstance(record, dict):
            return record
        out = {k: v for k, v in record.items() if k not in ("trackIndex", "sceneIndex", "path")}
        out[key] = index(record)
        return out

    return {"view": raw.get("view"),
            "track": placed(raw.get("selectedTrack"), "track_index", pp_codec.track_index_of),
            "clip": clip,
            "arrangement_clip": arrangement_clip,
            "scene": placed(raw.get("selectedScene"), "scene_index", pp_codec.scene_index_of),
            "device": raw.get("selectedDevice")}


def _other_track(read: dict | None, listed: dict, kind: str) -> dict:
    read = read if isinstance(read, dict) else {}
    merged = {**listed, **read}
    return {"id": merged.get("id"), "name": merged.get("name"), "type": kind,
            "index": pp_codec.return_index_of(merged),
            "devices": _devices(read), "sends": _sends(merged),
            "mixer": {"gain_db": merged.get("gainDb"), "pan": merged.get("pan")},
            "routing": {"output": _name(merged.get("outputRoutingType"))}}


def build_piece_snapshot(overview: dict, track_reads: dict, *, return_reads: dict | None = None,
                         master_read: dict | None = None, selection: Any = None,
                         transport: dict | None = None, transport_reason: str | None = None,
                         identity: dict | None = None, clip_reads: dict | None = None,
                         notes_reason: str | None = None) -> dict:
    """Assemble the snapshot from raw Producer Pal results (see module docstring).

    ``track_reads`` maps regular track index → the ``ppal-read-track`` result
    (arrangement-clips, notes, timing, sample, devices, routings, mixer).
    ``transport`` is the Live API ``live_set`` property map, or None with
    ``transport_reason``. ``identity`` comes from the reader (basis, verified,
    set_id, file_path, structural digest, attempts). ``clip_reads``: clip id ->
    the complete Live API read of that arrangement MIDI clip (None without the
    Live API, with ``notes_reason`` saying why).
    """
    problems = reads_consistent(overview, track_reads)
    if problems:
        raise PieceSnapshotError("; ".join(problems))
    signature, num, den, bpb = meter_of(overview.get("timeSignature"))
    try:
        tempo = float(overview.get("tempo"))
    except (TypeError, ValueError):
        raise PieceSnapshotError("the Set reports no tempo") from None
    listed_tracks = sorted((t for t in overview["tracks"] if isinstance(t, dict)),
                           key=pp_codec.track_index_of)
    groups = {t.get("id"): t.get("name") for t in listed_tracks if t.get("isGroup")}
    by_name: dict = {}
    for t in listed_tracks:
        by_name.setdefault(t.get("name"), []).append(t.get("id"))
    clip_reads = dict(clip_reads or {})
    returns = {t.get("name"): t.get("id") for t in overview.get("returnTracks") or []
               if isinstance(t, dict) and isinstance(t.get("name"), str)}
    tracks = [track_record(track_reads[pp_codec.track_index_of(t)], t, num=num, den=den,
                           by_name=by_name, groups=groups, clip_reads=clip_reads,
                           returns=returns)
              for t in listed_tracks]
    _mute_group_members(tracks)
    _link_feeds(tracks)
    link_hazards(tracks)
    mark_sidechains_unknown(tracks)     # attach_sidechains replaces it with edges
    names = {t["id"]: t["name"] for t in tracks}

    midi_clips, audio_clips, unheard = [], [], []
    for track in tracks:
        for clip in track["arrangement_clips"]:
            if clip["kind"] == "audio":
                unheard.append({"track_id": track["id"], "track": track["name"],
                                "clip_id": clip["id"], "clip_name": clip["name"],
                                "start": clip["start"], "start_bar": clip["start_bar"],
                                "end_bar": clip["end_bar"], "sample": clip["sample"],
                                "his_material": track["his_material"],
                                "heard": False})
            if not track["in_layers"] or clip["muted"]:
                continue
            entry = {"track": track["id"], "start": clip["start_beats"],
                     "end": clip["end_beats"], "clip_source": "arrangement"}
            (audio_clips if clip["kind"] == "audio" else midi_clips).append(entry)
    audio_names = sorted({names[c["track"]] for c in audio_clips})
    matrix = layer_matrix(midi_clips, beats_per_bar=bpb, audio_track_names=audio_names,
                          audio_clip_count=len(audio_clips))
    audio_matrix = layer_matrix(audio_clips, beats_per_bar=bpb) if audio_clips else None

    # The song ends where his material ends (SIDEKICK/host tracks do not stretch it).
    song_end = max([c["end_beats"] for t in tracks if t["his_material"]
                    for c in t["arrangement_clips"]] or [0.0])
    n_bars = int(math.ceil(song_end / bpb - 1e-9)) if song_end > 0 else 0
    counts = list(matrix.get("layers_per_bar") or []) + [0] * n_bars
    counts = counts[:n_bars]
    midi_sets = list(matrix.get("tracks_per_bar") or []) + [[]] * n_bars
    audio_counts = [0] * n_bars
    audio_sets = [[] for _ in range(n_bars)]
    if audio_matrix and not audio_matrix.get("no_arrangement"):
        for i, value in enumerate(audio_matrix["layers_per_bar"][:n_bars]):
            audio_counts[i] = value
            audio_sets[i] = audio_matrix["tracks_per_bar"][i]
    sets = [set(midi_sets[i]) | set(audio_sets[i]) for i in range(n_bars)]

    locators = [dict(item) for item in overview.get("locators") or [] if isinstance(item, dict)]
    usable, locators_ignored = split_locators(locators, num=num, den=den,
                                              song_end_beats=song_end)
    if len(usable) >= 2:
        raw_sections = locator_sections(usable, num=num, den=den, song_end_beats=song_end)
        section_rule = "locators (%d)" % len(usable)
    else:
        raw_sections = infer_sections([sorted(s) for s in sets], beats_per_bar=bpb)
        section_rule = ("inferred from clip boundaries: a new section where >=%d of his layers "
                        "enter or leave together, or the texture goes to/from silence; "
                        "blocks shorter than %d bars merge" % (SECTION_TURNOVER, SECTION_MIN_BARS))
    counts_ext = counts + [0] * max(0, int(math.ceil(max(
        [s["end_beats"] for s in raw_sections] or [0.0]) / bpb)) - len(counts))
    audio_ext = audio_counts + [0] * (len(counts_ext) - len(audio_counts))
    sets_ext = sets + [set() for _ in range(len(counts_ext) - len(sets))]
    sections = [_section_view(section, i, counts_ext, audio_ext, sets_ext, names, bpb)
                for i, section in enumerate(raw_sections)]

    uncertain, assumptions = [], []
    placed_midi = [(t, c) for t in tracks if t["his_material"]
                   for c in t["arrangement_clips"] if c["kind"] == "midi"]
    unread = [(t["name"], c) for t, c in placed_midi if c["notes_status"] == "unread"]
    if unread:
        partial = sum(1 for _, c in unread if c["coverage"] == cp.PARTIAL)
        uncertain.append(
            "%d placed MIDI clip%s have notes not read (%d unseen, %d partly seen): Producer "
            "Pal returns notes only in [-L, 2L) of clip-content time and their loop brace "
            "lies outside it — %s. They still count as layers (placement); their notes are "
            "neither audible nor hidden: %s" % (
                len(unread), "" if len(unread) == 1 else "s", len(unread) - partial, partial,
                notes_reason or "the Live API read failed",
                ", ".join("%s @ %s" % (name, c["start"]) for name, c in unread[:6])
                + (" …" if len(unread) > 6 else "")))
    fallback = [(t["name"], c) for t, c in placed_midi
                if c["notes_source"] == "producer_pal_window" and c["notes_status"] == "read"]
    if fallback:
        uncertain.append(
            "%d placed MIDI clip%s fell back to Producer Pal's [-L, 2L) note window "
            "(producer_pal_window) after the Live API read failed: what plays was read, "
            "notes outside the window were not: %s" % (
                len(fallback), "" if len(fallback) == 1 else "s",
                ", ".join("%s @ %s (%s)" % (name, c["start"], c["live_error"])
                          for name, c in fallback[:6]) + (" …" if len(fallback) > 6 else "")))
    silent = [(t["name"], c["start"]) for t, c in placed_midi
              if c["notes_status"] == "read" and c["notes"] and not c["notes_audible"]]
    if silent:
        uncertain.append(
            "%d placed MIDI clip%s read in full have no note inside the played region "
            "(silent, still counted as layers by placement): %s" % (
                len(silent), "" if len(silent) == 1 else "s",
                ", ".join("%s @ %s" % item for item in silent[:6])
                + (" …" if len(silent) > 6 else "")))
    sub_bar = [(t["name"], c["start"], c["end_beats"] - c["start_beats"])
               for t in tracks if t["in_layers"] for c in t["arrangement_clips"]
               if c["sub_bar"] and not c["muted"]]
    if sub_bar:
        uncertain.append(
            "%d clip%s shorter than a bar count as a whole-bar layer in the layer counts: %s"
            % (len(sub_bar), "" if len(sub_bar) == 1 else "s",
               ", ".join("%s @ %s (%g beats)" % item for item in sub_bar[:6])))
    states = sorted({str(t["state"]) for t in tracks if t["state"] not in (None, "muted")})
    if states:
        uncertain.append("track states other than muted reported: %s" % ", ".join(states))
    uncertain.append("sidechains unknown until the saved .als is read: every track of his "
                     "carries a sidechain_unknown hazard (sidechain edits since the last save "
                     "are never seen)")
    if audio_clips:
        assumptions.append("audio clips are placed and counted as audio layers, never heard")
    if locators_ignored:
        uncertain.append("%d locator%s at or after the song's end (bar %d) make no section: %s"
                         % (len(locators_ignored), "" if len(locators_ignored) == 1 else "s",
                            n_bars, ", ".join("%s at %s" % (x["name"] or "unnamed", x["time"])
                                              for x in locators_ignored)))
    if len(usable) == 1:
        assumptions.append("one locator only inside the song — sections inferred from clip "
                           "boundaries")
    lanes = [t["name"] for t in tracks if t["take_lanes"]]
    if lanes:
        assumptions.append("take lanes are alternates, not layers (%s)" % ", ".join(lanes))

    midi_entries = [{"bar": e["bar"] + 1, "track_id": e["track"], "track": names.get(e["track"])}
                    for e in matrix.get("entries") or []]
    midi_exits = [{"bar": e["bar"] + 1, "track_id": e["track"], "track": names.get(e["track"])}
                  for e in matrix.get("exits") or []]
    return {
        "kind": "piece_snapshot",
        "set": {"name": overview.get("name"), "tempo": tempo, "meter": signature,
                "beats_per_bar": bpb, "scale": overview.get("scale"),
                "song_end_beats": song_end, "bars": n_bars},
        "identity": dict(identity or {}),
        "tracks": tracks,
        "returns": [_other_track((return_reads or {}).get(pp_codec.return_index_of(t)), t,
                                 "return")
                    for t in overview.get("returnTracks") or [] if isinstance(t, dict)],
        "master": _other_track(master_read, pp_codec.main_track_of(overview) or {}, "master"),
        "muted_tracks": [{"track_id": t["id"], "track": t["name"]} for t in tracks if t["muted"]],
        "muted_by_group": [{"track_id": t["id"], "track": t["name"], "group": t["muted_by_group"]}
                           for t in tracks if t["muted_by_group"]],
        "soloed_tracks": [{"track_id": t["id"], "track": t["name"]} for t in tracks if t["soloed"]],
        "hazard_tracks": hazard_tracks(tracks),
        "not_his_material": [{"track_id": t["id"], "track": t["name"],
                              "reason": t["excluded_reason"]}
                             for t in tracks if not t["his_material"]],
        "unheard": unheard,
        "locators": locators,
        "locators_ignored": locators_ignored,
        "section_rule": section_rule,
        "sections": sections,
        "layers": {"basis": "midi_layers", "per_bar": counts, "audio_per_bar": audio_counts,
                   "bar_numbering": "per_bar[0] is bar 1",
                   "entries": midi_entries, "exits": midi_exits,
                   "audio_unseen": matrix.get("audio_unseen"),
                   "counted_tracks": [{"track_id": t["id"], "track": t["name"]}
                                      for t in tracks if t["in_layers"]]},
        "layer_matrix": matrix,
        "transport": _transport(transport, transport_reason, num=num, den=den),
        "selection": _selection(selection),
        "sidechains": {"status": "unknown", "reason": SIDECHAINS_UNKNOWN_REASON},
        "notes_read": {"source": "live_api" if any(
                           not (r or {}).get("error") for r in clip_reads.values())
                       else "producer_pal",
                       "reason": notes_reason,
                       "unread_clips": len(unread),
                       "producer_pal_window_clips": len(fallback)},
        "source_integrity": source_integrity_map(tracks, track_reads, clip_reads),
        "uncertain": uncertain,
        "assumptions": assumptions,
    }


def sidechains_from_reader(snapshot: dict, parsed: Any, *, file_path: str) -> dict:
    """Attach sidechains from ``tools.live_set_reader.read_live_set`` output —
    one parsed mapping PER Live Set in the file. A saved Set holds exactly one;
    anything else is refused rather than guessed."""
    if not isinstance(parsed, list) or len(parsed) != 1 or not isinstance(parsed[0], dict):
        raise PieceSnapshotError("the saved file must hold exactly one Live Set (found %s)"
                                 % (len(parsed) if isinstance(parsed, list) else "none"))
    return attach_sidechains(snapshot, parsed[0], file_path=file_path)


def _saved_set_sidechains(set_data: Any) -> tuple[list[dict], list[dict]]:
    """(file tracks and returns, sidechain edges) of one parsed Live Set,
    validated; anything malformed raises PieceSnapshotError before the
    snapshot is touched."""
    if not isinstance(set_data, dict):
        raise PieceSnapshotError("the saved Set read is not a mapping")
    file_tracks, edges = [], []
    for key in ("tracks", "returns"):
        items = set_data.get(key) or []
        if not isinstance(items, list) or not all(
                isinstance(t, dict) and isinstance(t.get("name"), (str, type(None)))
                for t in items):
            raise PieceSnapshotError("the saved Set's %s are malformed" % key)
        file_tracks.extend(items)
    items = set_data.get("sidechains") or []
    if not isinstance(items, list) or not all(
            isinstance(e, dict) and all(isinstance(e.get(k), (str, type(None)))
                                        for k in ("source_track", "target_track"))
            for e in items):
        raise PieceSnapshotError("the saved Set's sidechains are malformed")
    edges.extend(items)
    return file_tracks, edges


def attach_sidechains(snapshot: dict, set_data: dict, *, file_path: str) -> dict:
    """Enrich a snapshot with sidechain edges read from the saved .als.

    The .als track ids are not Live API ids, so edges map by track name, only
    when the name is unique in both the file and the snapshot; anything else
    is listed as unmatched. The ``sidechain_unknown`` hazards (and their
    ``uncertain`` line) are replaced by these edges. The saved file may predate
    unsaved edits: a sidechain added or removed since the last save is invisible.

    Fail closed: everything is computed before the snapshot changes, so a
    malformed read raises with every ``sidechain_unknown`` hazard and the
    uncertain line still in place.
    """
    file_tracks, file_edges = _saved_set_sidechains(set_data)
    tracks = snapshot.get("tracks") or []
    counts: dict = {}
    for track in tracks:
        counts[track["name"]] = counts.get(track["name"], 0) + 1
    file_counts: dict = {}
    for track in file_tracks:
        file_counts[track.get("name")] = file_counts.get(track.get("name"), 0) + 1
    ids = {t["name"]: t["id"] for t in tracks if counts[t["name"]] == 1}
    edges, unmatched = [], []
    for edge in file_edges:
        source, target = edge.get("source_track"), edge.get("target_track")
        mapped = all(name in ids and file_counts.get(name) == 1 for name in (source, target))
        item = {"source": source, "target": target, "device": edge.get("device"),
                "device_on": edge.get("device_on")}
        if mapped:
            edges.append({**item, "source_id": ids[source], "target_id": ids[target]})
        else:
            unmatched.append(item)
    additions: list[tuple[dict, dict]] = []
    by_id = {t["id"]: t for t in tracks}
    for edge in edges:
        additions.append((by_id[edge["source_id"]], _hazard(
            "sidechain_source", track_id=edge["target_id"], track=edge["target"],
            device=edge["device"], device_on=edge["device_on"])))
        additions.append((by_id[edge["target_id"]], _hazard(
            "sidechain_target", track_id=edge["source_id"], track=edge["source"],
            device=edge["device"], device_on=edge["device_on"])))
    for item in unmatched:      # a name we cannot pin to one track: every namesake
        for track in tracks:
            if track["name"] in (item["source"], item["target"]):
                additions.append((track, _hazard(
                    "sidechain_unmatched", source=item["source"], target=item["target"],
                    device=item["device"])))
    # --- apply: nothing above touched the snapshot ---
    for track in tracks:
        track["hazards"] = [h for h in track.get("hazards") or []
                            if h["type"] != "sidechain_unknown"]
    snapshot["uncertain"] = [item for item in snapshot.get("uncertain") or []
                             if not item.startswith("sidechains unknown")]
    for track, hazard in additions:
        track["hazards"].append(hazard)
    for track in tracks:
        if track.get("hazards"):
            _sort_hazards(track)
    snapshot["hazard_tracks"] = hazard_tracks(tracks)
    snapshot["sidechains"] = {"status": "from_saved_als", "file_path": file_path,
                              "edges": edges, "unmatched": unmatched,
                              "note": "read from the saved .als; unsaved sidechain edits "
                                      "(since the last save) are not seen"}
    return snapshot
