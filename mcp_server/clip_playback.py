"""What a MIDI clip actually plays — pure model, no Producer Pal, no store.

Producer Pal 2.2's read-track / read-clip return a MIDI clip's notes only in
the clip-content window ``[-L, 2L)`` (its processMidiClip asks Live for
``get_notes_extended(0, 128, -L, 3L)``; ``L`` = the clip's ``length``: the loop
length when looping, end marker - start marker otherwise). A clip whose loop
brace sits past ``2L`` — Bassline at 33|1 loops 88-104 in a 16-beat clip — comes
back with none of the notes that play. So every reading is classified:

* ``coverage``: ``full`` (the played region lies inside the read window),
  ``partial`` (it straddles the window edge) or ``unseen`` (it lies outside).
  A Live API read of the whole clip is always complete (``notes_source``
  ``live_api``).
* Notes of a clip that was not completely read are ``unread``: neither audible
  nor hidden. Nothing is ever called hidden unless it was seen.

Time units: every value in a *frame* is in quarter-note beats (Live API units).
Clip-content time counts from the clip's own 1|1 (not loop-relative). A Producer
Pal note ``t`` is in clip-meter beats (Live ``start_time`` x den/4); bar|beat
strings are in the meter they belong to (clip meter for start/end, song meter
for arrangementStart) and the beat is the meter's beat (an eighth in 6/8).

Playback (Live): the first pass plays ``[start_marker, loop_end)`` from the
clip's arrangement start; later passes repeat ``[loop_start, loop_end)``; all
of it is cut at the arrangement end. A clip that does not loop plays
``[start_marker, end_marker)`` once. Muted notes (``mute=1``) never sound.
"""
from __future__ import annotations

import math
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pp_codec  # noqa: E402

for _alias in ("clip_playback", "mcp_server.clip_playback"):   # one module universe
    sys.modules.setdefault(_alias, sys.modules[__name__])


EPS = 1e-6
# Brace membership tolerance for note starts (the historical split_audible_notes value).
NOTE_EPS = 1e-3
FULL, PARTIAL, UNSEEN = "full", "partial", "unseen"

# The Live API read of one arrangement/session clip: its timing and every note.
# One ppal-live-api request per clip, path "id <clip id>". The note range is the
# whole clip (not just the played region): the played region is only known once
# these properties come back, the start marker is invisible to Producer Pal,
# and notes outside the brace must be seen to be called hidden.
LIVE_CLIP_PROPERTIES = (
    "name", "loop_start", "loop_end", "start_marker", "end_marker", "looping",
    "start_time", "end_time", "signature_numerator", "signature_denominator",
    "is_midi_clip", "muted")
LIVE_NOTES_ARGS = [0, 128, -100000, 200000]
READ_WINDOW_REASON = (
    "Producer Pal returns a MIDI clip's notes only inside [-L, 2L) of clip-content "
    "time (L = clip length); this clip plays %s-%s, %s that window — its notes "
    "were not read (the Live API reads them)")


def live_clip_operations() -> list[dict]:
    """The ppal-live-api operations that read one clip completely."""
    ops = [{"type": "getProperty", "property": prop} for prop in LIVE_CLIP_PROPERTIES]
    ops.append({"type": "call", "method": "get_notes_extended",
                "args": list(LIVE_NOTES_ARGS)})
    return ops


# --- meters ---------------------------------------------------------------------

def parse_signature(signature: Any) -> tuple[int, int] | None:
    """'6/8' -> (6, 8); None when missing or unreadable."""
    if not isinstance(signature, str) or "/" not in signature:
        return None
    try:
        num, den = (int(part.strip()) for part in signature.split("/", 1))
    except ValueError:
        return None
    if num <= 0 or den <= 0 or den & (den - 1):
        return None
    return num, den


def bar_beat_quarters(bar_beat: str, num: int, den: int) -> float:
    """bar|beat in a num/den meter (the beat is the meter's beat) -> quarter beats."""
    bar_s, beat_s = str(bar_beat).split("|", 1)
    return (int(bar_s) - 1) * num * 4.0 / den + (float(beat_s) - 1) * 4.0 / den


# --- frames ---------------------------------------------------------------------

def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def frame_from_pp(clip: dict, *, song_signature: Any = None) -> dict | None:
    """A frame from a Producer Pal clip record (start/end/length/looping/
    timeSignature, plus arrangementStart/Length when placed). None without a
    readable brace. ``firstStart`` (clip meter) is the start marker; Producer
    Pal omits it when it equals ``start``, so without it a looping clip's
    first pass is assumed to start at its loop start (``start_marker_known``
    False)."""
    clip = clip if isinstance(clip, dict) else {}
    meter = parse_signature(clip.get("timeSignature")) or parse_signature(song_signature) \
        or (4, 4)
    num, den = meter
    bpb = num * 4.0 / den
    start = end = None
    try:
        if isinstance(clip.get("start"), str) and "|" in clip["start"]:
            start = bar_beat_quarters(clip["start"], num, den)
        if isinstance(clip.get("end"), str) and "|" in clip["end"]:
            end = bar_beat_quarters(clip["end"], num, den)
        if end is None and start is not None and isinstance(clip.get("length"), str):
            end = start + pp_codec.pp_duration_to_beats(clip["length"], bpb)
    except (TypeError, ValueError):
        return None
    if end is None:
        return None
    if start is None:
        start = 0.0     # only an end marker: the brace starts at the clip start
    looping = clip.get("looping")
    looping = bool(looping) if isinstance(looping, bool) else True
    marker, marker_known = start, not looping
    first = clip.get("firstStart")
    if isinstance(first, str) and "|" in first:
        try:
            marker, marker_known = bar_beat_quarters(first, num, den), True
        except (TypeError, ValueError):
            pass
    song = parse_signature(song_signature) or meter
    arr_start = arr_end = None
    # 2.2 arrangementStart, 2.3 the position in the clip path 't3[5|1]'
    placed_at = pp_codec.arrangement_start_of(clip)
    try:
        if isinstance(placed_at, str):
            arr_start = bar_beat_quarters(placed_at, *song)
            if isinstance(clip.get("arrangementLength"), str):
                arr_end = arr_start + pp_codec.pp_duration_to_beats(
                    clip["arrangementLength"], song[0] * 4.0 / song[1])
    except (TypeError, ValueError):
        arr_start = arr_end = None
    return {"source": "producer_pal", "looping": looping,
            "loop_start": start, "loop_end": end,
            "start_marker": marker, "end_marker": None if looping else end,
            "start_marker_known": marker_known,
            "arr_start": arr_start, "arr_end": arr_end,
            "num": num, "den": den, "muted": bool(clip.get("muted") is True
                                                  or clip.get("state") == "muted")}


def frame_from_live(props: dict) -> dict:
    """A frame from the Live API clip properties (LIVE_CLIP_PROPERTIES)."""
    get = props.get
    looping = bool(get("looping"))
    return {"source": "live_api", "looping": looping,
            "loop_start": _num(get("loop_start")), "loop_end": _num(get("loop_end")),
            "start_marker": _num(get("start_marker")), "end_marker": _num(get("end_marker")),
            "start_marker_known": True,
            "arr_start": _num(get("start_time")), "arr_end": _num(get("end_time")),
            "num": int(get("signature_numerator") or 4),
            "den": int(get("signature_denominator") or 4),
            "muted": bool(get("muted"))}


def clip_length(frame: dict) -> float:
    """Live's clip ``length``: loop length when looping, else end - start marker."""
    if frame["looping"]:
        return frame["loop_end"] - frame["loop_start"]
    return frame["end_marker"] - frame["start_marker"]


def played_region(frame: dict) -> tuple[float, float]:
    """Clip-content span whose notes can sound: [min(start marker, loop start),
    loop end) when looping, [start marker, end marker) otherwise."""
    if frame["looping"]:
        return min(frame["start_marker"], frame["loop_start"]), frame["loop_end"]
    return frame["start_marker"], frame["end_marker"]


def read_window(frame: dict) -> tuple[float, float]:
    """Producer Pal 2.2's note window [-L, 2L) in clip-content quarter beats."""
    length = clip_length(frame)
    return -length, 2 * length


def coverage(frame: dict) -> str:
    """full | partial | unseen: how much of the played region Producer Pal's
    note window saw. A Live API frame's notes are read whole: always full."""
    if frame.get("source") == "live_api":
        return FULL
    lo, hi = played_region(frame)
    wlo, whi = read_window(frame)
    if clip_length(frame) <= 0:
        return UNSEEN
    if lo >= wlo - EPS and hi <= whi + EPS:
        return FULL
    if hi <= wlo + EPS or lo >= whi - EPS:
        return UNSEEN
    return PARTIAL


def unread_reason(frame: dict) -> str:
    lo, hi = played_region(frame)
    where = "outside" if coverage(frame) == UNSEEN else "partly outside"
    return READ_WINDOW_REASON % (_fmt(lo), _fmt(hi), where)


def _fmt(value: float) -> str:
    return ("%d" % value) if abs(value - round(value)) < EPS else ("%.4g" % value)


# --- notes ----------------------------------------------------------------------

LIVE_NOTE_DETAIL = ("probability", "velocity_deviation", "release_velocity")


def note_from_live(note: dict) -> dict:
    """A Live API note (pitch/start_time/duration/velocity/mute) in the shared
    shape, keeping the detail fields (LIVE_NOTE_DETAIL) Live reports."""
    out = {"pitch": int(note["pitch"]), "start_beats": float(note["start_time"]),
           "duration_beats": float(note["duration"]),
           "velocity": int(round(float(note.get("velocity", 100)))),
           "mute": bool(note.get("mute"))}
    for key in LIVE_NOTE_DETAIL:
        value = _num(note.get(key))
        if value is not None:
            out[key] = value
    return out


def in_clip_meter(notes: list[dict], den: int) -> list[dict]:
    """Live API notes (quarters) in Producer Pal's units (clip-meter beats), the
    shape every Producer Pal clip read hands its callers (no ``mute`` key)."""
    scale = den / 4.0
    return [{"pitch": n["pitch"], "start_beats": n["start_beats"] * scale,
             "duration_beats": n["duration_beats"] * scale, "velocity": n["velocity"]}
            for n in notes]


def note_start_quarters(note: dict, den: int = 4) -> float:
    """Clip-content start of a Producer Pal note dict in quarter beats."""
    start = note.get("start", note.get("start_beats", note.get("t", 0.0)))
    return float(start or 0.0) * 4.0 / den


def is_muted(note: dict) -> bool:
    value = note.get("mute", note.get("muted", False))
    return bool(value) and value not in ("0", "false")


def split(frame: dict, notes: list[dict], *, quarters: bool = False) -> dict:
    """Partition ``notes`` into audible / hidden / muted by the played region.

    ``notes`` keep their own shape and are returned untouched. Their start is
    in clip-meter beats (Producer Pal), or already quarters with
    ``quarters=True`` (Live API). A frame read through Producer Pal whose
    coverage is not ``full`` returns ``unread`` True and every list empty:
    those notes are neither audible nor hidden.
    """
    notes = list(notes or [])
    cov = coverage(frame)
    lo, hi = played_region(frame)
    base = {"coverage": cov, "played_region": [lo, hi], "read_window": list(read_window(frame)),
            "notes_source": frame.get("source"),
            "start_marker_known": frame.get("start_marker_known")}
    if cov != FULL:
        return {**base, "unread": True, "unread_reason": unread_reason(frame),
                "audible": [], "hidden": [], "muted": [], "n_seen": len(notes)}
    den = 4 if quarters else frame["den"]
    audible, hidden, muted = [], [], []
    for note in notes:
        start = note_start_quarters(note, den)
        if is_muted(note):
            muted.append(note)
        elif lo - NOTE_EPS <= start < hi - NOTE_EPS:
            audible.append(note)
        else:
            hidden.append(note)
    return {**base, "unread": False, "unread_reason": None,
            "audible": audible, "hidden": hidden, "muted": muted, "n_seen": len(notes)}


def events(frame: dict, notes: list[dict], *, quarters: bool = True) -> list[dict]:
    """Arrangement-time note-ons of the audible ``notes`` of a placed clip.

    ``[{pitch, start (song quarter beats), duration, velocity, clip_start}]``
    sorted by time, cut at the arrangement end (a note is dropped when its
    onset is at/after the end, and its duration is clipped to it). A note
    also stops where its pass does (loop end when looping, end marker
    otherwise). A muted clip plays nothing.
    """
    a, e = frame.get("arr_start"), frame.get("arr_end")
    if a is None or e is None or e <= a or frame.get("muted"):
        return []
    den = 4 if quarters else frame["den"]
    parsed = []
    for note in notes:
        if is_muted(note):
            continue
        dur = note.get("duration_beats", note.get("duration", note.get("d", 0.0)))
        parsed.append((note_start_quarters(note, den), float(dur or 0.0) * 4.0 / den,
                       int(note.get("pitch", note.get("p"))),
                       note.get("velocity", note.get("v"))))
    out = []

    def emit(onset, t, dur, pitch, vel, stop):
        # a note sounds until the loop wraps / the end marker, and the clip's end
        if a - EPS <= onset < e - EPS:
            out.append({"pitch": pitch, "start": onset,
                        "duration": min(dur, stop - t, e - onset),
                        "velocity": vel, "clip_start": t})

    if frame["looping"]:
        sm, ls, le = frame["start_marker"], frame["loop_start"], frame["loop_end"]
        length = le - ls
        for t, dur, pitch, vel in parsed:
            if sm - NOTE_EPS <= t < le - NOTE_EPS:
                emit(a + (t - sm), t, dur, pitch, vel, le)
        if length > EPS:
            base = a + (le - sm)
            passes = int(math.ceil((e - base) / length)) if e > base else 0
            for j in range(passes):
                offset = base + j * length
                for t, dur, pitch, vel in parsed:
                    if ls - NOTE_EPS <= t < le - NOTE_EPS:
                        emit(offset + (t - ls), t, dur, pitch, vel, le)
    else:
        sm, em = frame["start_marker"], frame["end_marker"]
        for t, dur, pitch, vel in parsed:
            if sm - NOTE_EPS <= t < em - NOTE_EPS:
                emit(a + (t - sm), t, dur, pitch, vel, em)
    out.sort(key=lambda ev: (ev["start"], ev["pitch"]))
    return out
