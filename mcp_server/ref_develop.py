"""Reference-guided proposal engine (plan batch8 1.4, .scratch/batch8/brief_1_4.md).

Pure and seeded: no IO, never touches Live or the store. Inputs are a piece
snapshot (moment.read_piece_snapshot), resolved piece decisions
(piece_context.current_decisions), reference sequences
(tools.audio_ref.reference_sequence, built by the caller) and his idiom cells
(mcp_server.idiom_cells). Every claim carries an evidence type:

- ``set_file``        read from his open Set (clips, notes, devices as Live reports them)
- ``measured``        DSP on audio
- ``model_heard``     a verified listen's card
- ``owner_confirmed`` he said so / heard it
- ``inferred``        derived here by a stated rule (a guess the chat must say is a guess)

Times are song quarter beats; bars are 1-based ``[first, last]`` inclusive.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from typing import Any

from mcp_server import idiom_cells as ic
from mcp_server import piece_snapshot as ps

EVIDENCE = ("set_file", "measured", "model_heard", "owner_confirmed", "inferred")

_FLAT_NAMES = ("C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
_SHARP_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_SHARP_CENTRES = {1, 2, 4, 6, 7, 9, 11}
_MODES = (("ionian", (0, 2, 4, 5, 7, 9, 11)), ("dorian", (0, 2, 3, 5, 7, 9, 10)),
          ("phrygian", (0, 1, 3, 5, 7, 8, 10)), ("lydian", (0, 2, 4, 6, 7, 9, 11)),
          ("mixolydian", (0, 2, 4, 5, 7, 9, 10)), ("aeolian", (0, 2, 3, 5, 7, 8, 10)),
          ("locrian", (0, 1, 3, 5, 6, 8, 10)))
_INTERVALS = ("unison", "semitone", "whole tone", "minor third", "major third", "fourth",
              "tritone", "fifth", "minor sixth", "major sixth", "minor seventh",
              "major seventh")

# Thresholds (fixed so identical Sets give identical maps).
_DRONE_SHARE = 0.85      # one pitch class carries this share -> a drone
_MAIN_SHARE = 0.10       # a "main tone" of a span
_CORE_SHARE = 0.01       # a tone that belongs to a world at all
_PHRASE_BARS = 4
_PITCHLESS = ("silent", "unpitched")  # segment kinds with no pitched layer
_KIT_PITCHES = (35, 51)  # GM kick .. crash
_KIT_MIN_DISTINCT = 5
_KIT_MAX_MEDIAN_BEATS = 0.5
# share of onsets struck together with another pitch: kits hit kick + hat at
# once (NOCTURNEv2: 0.21-0.47); a bass or lead line is one note at a time
# (every tonal track there <= 0.10)
_KIT_MIN_TOGETHER = 0.15


# --- small helpers -------------------------------------------------------------

def pc_name(pc: int, centre: int | None = None) -> str:
    """Pitch-class name, spelled with sharps in sharp-side centres."""
    names = _SHARP_NAMES if centre in _SHARP_CENTRES else _FLAT_NAMES
    return names[pc % 12]


def _bpb(snapshot: dict) -> float:
    return float(snapshot["set"]["beats_per_bar"])


def _song_bars(snapshot: dict) -> int:
    return int(snapshot["set"]["bars"])


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return 0.0
    return ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2


def _r(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


# --- tracks and notes ------------------------------------------------------------

def _track_events(track: dict) -> list[dict]:
    events = []
    for clip in track.get("arrangement_clips") or []:
        events.extend(ps.clip_events(clip))
    events.sort(key=lambda ev: (ev["start"], ev["pitch"]))
    return events


def track_roles(snapshot: dict) -> list[dict]:
    """Role of each layer track of his: ``kit`` | ``tonal`` | ``audio`` |
    ``unread`` (placed MIDI whose notes were not read) | ``empty``. A track
    with some clips unread carries ``unread_clips``.

    A kit is a drum-rack instrument (``set_file``) or, by its notes, a track
    whose audible pitches all lie in the GM drum zone 35-51 with at least five
    distinct pitches, a median note of at most half a beat and at least 15% of
    its onsets struck together with another pitch (``inferred``) — a
    monophonic low line in that zone stays tonal. Names are never read."""
    rows = []
    for track in snapshot.get("tracks") or []:
        if not track.get("in_layers"):
            continue
        row = {"track_id": track["id"], "track": track["name"]}
        if track.get("type") == "audio":
            if not track.get("arrangement_clips"):
                rows.append({**row, "role": "empty", "evidence": "set_file",
                             "why": "no arrangement clips"})
                continue
            rows.append({**row, "role": "audio", "evidence": "set_file",
                         "why": "audio track: its content is not read"})
            continue
        if any(str(d).startswith("drum-rack") for d in track.get("devices") or []):
            rows.append({**row, "role": "kit", "evidence": "set_file",
                         "why": "drum rack instrument"})
            continue
        events = _track_events(track)
        unread = [c for c in track.get("arrangement_clips") or []
                  if c.get("kind") == "midi" and not c.get("muted")
                  and c.get("notes_status") != "read"]
        if unread:
            row["unread_clips"] = len(unread)
        if not events:
            if unread:
                rows.append({**row, "role": "unread", "evidence": "set_file",
                             "why": "%d placed MIDI clip%s whose notes were not read: what it "
                                    "plays is unknown" % (len(unread),
                                                          "" if len(unread) == 1 else "s")})
                continue
            rows.append({**row, "role": "empty", "evidence": "set_file",
                         "why": "no audible notes placed"})
            continue
        pitches = sorted({ev["pitch"] for ev in events})
        median = _median([ev["duration"] for ev in events])
        in_zone = _KIT_PITCHES[0] <= pitches[0] and pitches[-1] <= _KIT_PITCHES[1]
        onsets: dict = {}
        for ev in events:
            onsets.setdefault(round(ev["start"], 3), set()).add(ev["pitch"])
        together = sum(1 for hit in onsets.values() if len(hit) > 1) / len(onsets)
        if in_zone and len(pitches) >= _KIT_MIN_DISTINCT and median <= _KIT_MAX_MEDIAN_BEATS \
                and together >= _KIT_MIN_TOGETHER:
            rows.append({**row, "role": "kit", "evidence": "inferred",
                         "why": "plays a drum map: %d pitches, all in %d-%d, median note "
                                "%.2f beat, %d%% of hits struck together"
                                % (len(pitches), pitches[0], pitches[-1], median,
                                   int(round(100 * together)))})
            continue
        rows.append({**row, "role": "tonal", "evidence": "set_file",
                     "why": "pitched notes %d-%d, median note %.2f beat"
                            % (pitches[0], pitches[-1], median)})
    return rows


def song_notes(snapshot: dict, *, roles: tuple = ("tonal",)) -> dict:
    """``{track_id: {"track", "role", "events"}}`` for his layer tracks of the
    given roles; events are clip_events on the song timeline."""
    wanted = {row["track_id"]: row for row in track_roles(snapshot) if row["role"] in roles}
    out = {}
    for track in snapshot.get("tracks") or []:
        if track["id"] in wanted:
            out[track["id"]] = {"track": track["name"], "role": wanted[track["id"]]["role"],
                                "events": _track_events(track)}
    return out


# --- harmony ------------------------------------------------------------------------

def _bar_weights(notes: dict, bars: int, bpb: float) -> list[dict]:
    """Per bar: sounding beats per pitch class, summed over tracks."""
    weights = [dict() for _ in range(bars)]
    for entry in notes.values():
        for ev in entry["events"]:
            start, end = ev["start"], ev["start"] + ev["duration"]
            first = max(0, int(start // bpb))
            last = min(bars - 1, int(math.ceil(end / bpb)) - 1)
            for index in range(first, last + 1):
                overlap = min(end, (index + 1) * bpb) - max(start, index * bpb)
                if overlap > 1e-9:
                    pc = ev["pitch"] % 12
                    weights[index][pc] = weights[index].get(pc, 0.0) + overlap
    return weights


def _span_weights(bar_weights: list[dict], first: int, last: int) -> dict:
    total: dict = {}
    for index in range(first - 1, last):
        for pc, value in bar_weights[index].items():
            total[pc] = total.get(pc, 0.0) + value
    return total


def _shares(weights: dict) -> dict:
    total = sum(weights.values())
    return {pc: value / total for pc, value in weights.items()} if total > 0 else {}


def _main_tones(weights: dict) -> set:
    return {pc for pc, share in _shares(weights).items() if share >= _MAIN_SHARE}


def _centre(weights: dict) -> tuple[int | None, float]:
    """(pitch class with the most sounding time, its lead over the next)."""
    ranked = sorted(_shares(weights).items(), key=lambda item: (-item[1], item[0]))
    if not ranked:
        return None, 0.0
    margin = ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0.0)
    return ranked[0][0], margin


def _edge_splits(bar_weights: list[dict], first: int, last: int) -> list[tuple[list[int], bool]]:
    """Split a section at its edges: its first or last 4-bar phrase becomes a
    segment of its own when none of its main tones is a main tone of the rest
    of the section and that rest is at least 8 bars long. ``[(span, split)]``."""

    def detached(phrase, rest):
        tones = _main_tones(_span_weights(bar_weights, *phrase))
        return bool(tones) and not tones & _main_tones(_span_weights(bar_weights, *rest))

    lo, hi = first, last
    head = tail = None
    if hi - lo + 1 >= 3 * _PHRASE_BARS and detached([hi - _PHRASE_BARS + 1, hi],
                                                    [lo, hi - _PHRASE_BARS]):
        tail, hi = [hi - _PHRASE_BARS + 1, hi], hi - _PHRASE_BARS
    if hi - lo + 1 >= 3 * _PHRASE_BARS and detached([lo, lo + _PHRASE_BARS - 1],
                                                    [lo + _PHRASE_BARS, hi]):
        head, lo = [lo, lo + _PHRASE_BARS - 1], lo + _PHRASE_BARS
    return ([(head, True)] if head else []) + [([lo, hi], False)] + ([(tail, True)] if tail else [])


def _pcs_view(weights: dict, centre: int | None) -> list[dict]:
    shares = _shares(weights)
    return [{"pc": pc, "name": pc_name(pc, centre), "share": _r(share)}
            for pc, share in sorted(shares.items(), key=lambda item: (-item[1], item[0]))
            if share >= 0.005]


def _segment(bar_weights, bars, section, index, split):
    weights = _span_weights(bar_weights, *bars)
    centre, margin = _centre(weights)
    shares = _shares(weights)
    if centre is None:
        # no pitched layer: silence only where the section itself is silent
        kind = "silent" if section.get("silent") else "unpitched"
    elif shares[centre] >= _DRONE_SHARE:
        kind = "drone"
    else:
        kind = "modal"
    return {"index": index, "bars": list(bars), "section_bars": list(section["bars"]),
            "kind": kind, "centre": centre,
            "centre_name": pc_name(centre, centre) if centre is not None else None,
            "centre_margin": _r(margin), "pcs": _pcs_view(weights, centre),
            "main": [pc_name(pc, centre) for pc in sorted(_main_tones(weights))],
            "split": split, "weights": weights}


def _mode_guess(shares: dict, centre: int) -> dict:
    scored = []
    for name, steps in _MODES:
        members = {(centre + step) % 12 for step in steps}
        scored.append((sum(shares.get(pc, 0.0) for pc in members), name, members))
    scored.sort(key=lambda item: (-item[0], [m for m, _ in _MODES].index(item[1])))
    best, runner = scored[0], scored[1]
    tied = [name for score, name, _ in scored if best[0] - score < 0.005]
    return {"best": best, "runner": runner, "tied": tied}


def _world(world_id, segments, centre, kind):
    weights: dict = {}
    for seg in segments:
        for pc, value in seg["weights"].items():
            weights[pc] = weights.get(pc, 0.0) + value
    shares = _shares(weights)
    present = sorted((pc for pc, share in shares.items() if share >= _CORE_SHARE),
                     key=lambda pc: (pc - centre) % 12)
    world = {"id": world_id, "centre": centre, "centre_name": pc_name(centre, centre),
             "kind": kind, "segments": [seg["index"] for seg in segments],
             "evidence": "inferred", "shares": shares}
    if kind == "drone":
        world.update(mode=None, mode_margin=None, mode_runner_up=None,
                     mode_note="drone: one tone carries >= %d%% of the sounding time"
                               % int(_DRONE_SHARE * 100),
                     core=[pc_name(centre, centre)],
                     outside=[pc for pc in present if pc != centre])
        return world
    guess = _mode_guess(shares, centre)
    score, name, members = guess["best"]
    if len(guess["tied"]) > 1:
        heard = sorted({(pc - centre) % 12 for pc in present})
        degrees = [_degree_name(step) for step in heard]
        world.update(mode=None, mode_margin=0.0, mode_runner_up=None,
                     mode_candidates=guess["tied"],
                     mode_note="open: degrees %s fit %s; no third heard"
                               % (", ".join(degrees), "/".join(guess["tied"]))
                     if not ({3, 4} & set(heard)) else
                     "open: degrees %s fit %s" % (", ".join(degrees), "/".join(guess["tied"])),
                     core=[pc_name(pc, centre) for pc in present], outside=[])
        return world
    world.update(mode=name, mode_margin=_r(score - guess["runner"][0]),
                 mode_runner_up=guess["runner"][1],
                 mode_note="%s by %.1f%% of sounding time over %s"
                           % (name, 100 * (score - guess["runner"][0]), guess["runner"][1]),
                 core=[pc_name(pc, centre) for pc in present if pc in members],
                 outside=[pc for pc in present if pc not in members])
    return world


def _degree_name(step: int) -> str:
    return ("1", "b2", "2", "b3", "3", "4", "#4", "5", "b6", "6", "b7", "7")[step % 12]


def _foreshadow(worlds: list[dict], segments: list[dict], bar_weights: list[dict]) -> None:
    order = {w["id"]: min(segments[i]["bars"][0] for i in w["segments"]) for w in worlds}
    for world in worlds:
        rows = []
        for pc in world.pop("outside"):
            later = [w for w in worlds if order[w["id"]] > order[world["id"]]
                     and pc_name(pc, w["centre"]) in w["core"] and w["kind"] != "drone"]
            if not later:
                continue
            bars = [b for i in world["segments"] for b in range(segments[i]["bars"][0],
                                                                  segments[i]["bars"][1] + 1)
                    if bar_weights[b - 1].get(pc, 0.0) > 0]
            rows.append({"pc": pc, "name": pc_name(pc, later[0]["centre"]),
                         "share": _r(world["shares"][pc]), "in_world": later[0]["id"],
                         "first_bar": bars[0] if bars else None, "bars": _runs(bars)})
        rows.sort(key=lambda row: (-row["share"], row["pc"]))
        world["foreshadow"] = rows


def _runs(bars: list[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for bar in bars:
        if runs and bar == runs[-1][1] + 1:
            runs[-1][1] = bar
        else:
            runs.append([bar, bar])
    return runs


def _core_pcs(world: dict) -> set:
    return {pc for pc in range(12) if pc_name(pc, world["centre"]) in world["core"]}


def harmony_map(snapshot: dict) -> dict:
    """Per section: duration-weighted pitch classes of his tonal tracks (kits
    excluded by track_roles), tonal centre with its margin, a mode guess, the
    tones adjacent segments share, and worlds.

    Segments are the snapshot's sections, each split only at its edges (see
    _edge_splits). A world gathers the segments with the same centre and the
    same kind (drone = one tone >= 85% of the sounding time; modal otherwise),
    so a drone intro and a modal body on the same centre are two worlds. A
    world's outside tones that are core tones of a later world are listed as
    foreshadow. A segment with no pitched layer is ``silent`` when its
    section is silent and ``unpitched`` otherwise (kits/audio only). A pivot is
    reported for each such segment between two different worlds: the core tones of the last modal world before it that the world
    after it shares, and where one of them is held alone right before."""
    bpb = _bpb(snapshot)
    bars = _song_bars(snapshot)
    notes = song_notes(snapshot)
    bar_weights = _bar_weights(notes, bars, bpb)
    segments = []
    for section in snapshot.get("sections") or []:
        first, last = section["bars"]
        for span, is_split in _edge_splits(bar_weights, first, last):
            split = ({"from_section": [first, last],
                      "rule": "edge phrase shares no main tone with the rest of its section"}
                     if is_split else None)
            segments.append(_segment(bar_weights, span, section, len(segments), split))
    worlds, keys = [], {}
    for seg in segments:
        if seg["kind"] in _PITCHLESS:
            seg["world"] = None
            continue
        key = (seg["centre"], seg["kind"])
        if key not in keys:
            keys[key] = len(worlds)
            worlds.append([key, []])
        worlds[keys[key]][1].append(seg)
        seg["world"] = "W%d" % (keys[key] + 1)
    world_rows = [_world("W%d" % (i + 1), segs, key[0], key[1])
                  for i, (key, segs) in enumerate(worlds)]
    _foreshadow(world_rows, segments, bar_weights)
    for world in world_rows:
        world["spans"] = _merge_spans([segments[i]["bars"] for i in world["segments"]])
    boundaries = _boundaries(segments, world_rows)
    pivots = _pivots(segments, world_rows)
    for world in world_rows:
        world.pop("shares")
    for seg in segments:
        seg.pop("weights")
    return {"kind": "harmony_map", "evidence": "set_file",
            "basis": "duration-weighted pitch classes of his tonal tracks per bar (kits and "
                     "audio excluded); centres, modes, worlds and pivots are inferred",
            "roles": track_roles(snapshot), "segments": segments, "worlds": world_rows,
            "boundaries": boundaries, "pivots": pivots,
            "uncertain": ["audio tracks are not heard: their pitches are not in this map"]
            if any(r["role"] == "audio" for r in track_roles(snapshot)) else []}


def _merge_spans(spans: list[list[int]]) -> list[list[int]]:
    merged: list[list[int]] = []
    for first, last in sorted(spans):
        if merged and first == merged[-1][1] + 1:
            merged[-1][1] = last
        else:
            merged.append([first, last])
    return merged


def _boundaries(segments: list[dict], worlds: list[dict]) -> list[dict]:
    """Tones shared across each boundary between sounding segments (silence skipped)."""
    out = []
    sounding = [s for s in segments if s["kind"] not in _PITCHLESS]
    for left, right in zip(sounding, sounding[1:]):
        gap = None
        if right["bars"][0] != left["bars"][1] + 1:
            gap = [left["bars"][1] + 1, right["bars"][0] - 1]
        shared = sorted(set(left["weights"]) & set(right["weights"])
                        & (_main_tones(left["weights"]) | _main_tones(right["weights"])))
        shared = [pc for pc in shared if pc in _main_tones(left["weights"])
                  and pc in _main_tones(right["weights"])]
        out.append({"bar": right["bars"][0], "from_segment": left["index"],
                    "to_segment": right["index"],
                    "shared": [pc_name(pc, right["centre"]) for pc in shared],
                    "across_silence": gap, "evidence": "set_file"})
    return out


def _pivots(segments: list[dict], worlds: list[dict]) -> list[dict]:
    by_id = {w["id"]: w for w in worlds}
    out = []
    for index, seg in enumerate(segments):
        if seg["kind"] not in _PITCHLESS:
            continue
        before = [s for s in segments[:index] if s["kind"] not in _PITCHLESS]
        after = [s for s in segments[index + 1:] if s["kind"] not in _PITCHLESS]
        if not before or not after or before[-1]["world"] == after[0]["world"]:
            continue
        target = by_id[after[0]["world"]]
        modal = [s for s in before if s["kind"] == "modal"]
        source = by_id[(modal[-1] if modal else before[-1])["world"]]
        shared = sorted(_core_pcs(source) & _core_pcs(target))
        held = before[-1]
        held_alone = (list(held["bars"]) if held["kind"] == "drone" and held["centre"] in shared
                      else None)
        step = (target["centre"] - source["centre"]) % 12
        out.append({"gap_bars": list(seg["bars"]), "gap_kind": seg["kind"],
                    "from_world": source["id"],
                    "to_world": target["id"],
                    "interval": _INTERVALS[min(step, 12 - step)],
                    "shared": [pc_name(pc, target["centre"]) for pc in shared],
                    "held_alone": held_alone, "evidence": "inferred",
                    "rule": "core tones of the last modal world before the silence that the "
                            "world after it shares"})
    return out


# --- layers ----------------------------------------------------------------------------

def _static_run(section: dict, change_bars: set) -> list[int] | None:
    """Longest run of bars inside a section with no entry or exit."""
    first, last = section["bars"]
    best, start = None, first
    for bar in range(first, last + 2):
        if bar > last or (bar in change_bars and bar != start):
            if best is None or bar - start > best[1] - best[0] + 1:
                best = [start, bar - 1]
            start = bar
    return best


def layer_arc(snapshot: dict) -> dict:
    """Layers per section (his MIDI layers, the snapshot's count), the peaks
    (sections whose peak layer count beats both neighbours'), whether the last
    peak exceeds the first, silent gaps, hand-offs at every boundary and how
    the song ends."""
    bars = _song_bars(snapshot)
    layers = snapshot.get("layers") or {}
    per_bar = layers.get("per_bar") or []
    change_bars = {row["bar"] for row in (layers.get("entries") or []) + (layers.get("exits") or [])}
    sections = []
    for sec in snapshot.get("sections") or []:
        sections.append({"bars": list(sec["bars"]), "layers_min": sec["layers_min"],
                         "layers_max": sec["layers_max"], "layers_mean": sec["layers_mean"],
                         "silent": sec["silent"],
                         "tracks": sorted(t["track"] for t in sec["tracks"]),
                         "enters": sorted(t["track"] for t in sec["enters"]),
                         "leaves": sorted(t["track"] for t in sec["leaves"]),
                         "longest_static": _static_run(sec, change_bars)})
    peaks = []
    for i, sec in enumerate(sections):
        before = sections[i - 1]["layers_max"] if i else -1
        after = sections[i + 1]["layers_max"] if i + 1 < len(sections) else -1
        if not sec["silent"] and sec["layers_max"] > before and sec["layers_max"] > after:
            first, last = sec["bars"]
            peaks.append({"bars": sec["bars"], "layers": sec["layers_max"],
                          "span_pct": [int(round(100.0 * (first - 1) / bars)),
                                       int(round(100.0 * last / bars))]})
    gaps = []
    for i, sec in enumerate(sections):
        if sec["silent"]:
            gaps.append({"bars": sec["bars"],
                         "between": [sections[i - 1]["bars"] if i else None,
                                     sections[i + 1]["bars"] if i + 1 < len(sections) else None]})
    handoffs = [{"bar": sec["bars"][0], "enters": sec["enters"], "leaves": sec["leaves"]}
                for sec in sections[1:] if sec["enters"] or sec["leaves"]]
    last_section = sections[-1] if sections else None
    ending = None
    if last_section:
        ending = {"bars": last_section["bars"], "last_bar": bars,
                  "layers_at_end": per_bar[-1] if per_bar else 0,
                  "peak_layers": max((p["layers"] for p in peaks), default=0)}
    first_peak = max(peaks, key=lambda p: (p["layers"], -p["bars"][0])) if peaks else None
    last_peak = peaks[-1] if peaks else None
    return {"kind": "layer_arc", "evidence": "set_file",
            "basis": "his MIDI layers per bar (snapshot layers); audio tracks unread",
            "sections": sections, "peaks": peaks, "first_peak": first_peak, "last_peak": last_peak,
            "last_peak_exceeds_first": bool(first_peak and last_peak and last_peak is not first_peak
                                            and last_peak["layers"] > first_peak["layers"]),
            "gaps": gaps, "handoffs": handoffs, "ending": ending}


# --- clashes ----------------------------------------------------------------------------

_RUB_CLASSES = {1: "semitone", 11: "semitone", 6: "tritone"}
_STEPS_PER_BEAT = 4  # 16ths in 4/4 quarter beats


def _events_of(value) -> tuple[list[dict], str | None, str | None]:
    if isinstance(value, dict):
        return value.get("events") or [], value.get("role"), value.get("track")
    return list(value or []), None, None


def _steps(ev: dict) -> range:
    start = float(ev.get("start", ev.get("start_beats", 0.0)))
    dur = float(ev.get("duration", ev.get("duration_beats", 0.0)))
    first = int(math.floor(start * _STEPS_PER_BEAT + 1e-6))
    last = int(math.ceil((start + dur) * _STEPS_PER_BEAT - 1e-6))
    return range(first, max(first + 1, last))


def _window_beats(window, bpb: float) -> tuple[float, float]:
    if isinstance(window, dict):
        first, last = window["bars"]
        return (first - 1) * bpb, last * bpb
    return float(window[0]), float(window[1])


def _occupancy(notes_by_track: dict, lo_step: int, hi_step: int) -> tuple[dict, list]:
    grid: dict = {}
    excluded = []
    for key in sorted(notes_by_track, key=str):
        events, role, name = _events_of(notes_by_track[key])
        if role == "kit":
            excluded.append(name or str(key))
            continue
        for ev in events:
            for step in _steps(ev):
                if lo_step <= step < hi_step:
                    grid.setdefault(step, set()).add((str(key), name or str(key),
                                                      int(ev["pitch"])))
    return grid, excluded


def clash_check(notes_by_track: dict, window, *, bpb: float = 4.0) -> dict:
    """Simultaneous semitone (incl. major 7th / minor 9th) and tritone overlaps
    between notes of different tracks at 16th resolution inside ``window``
    (``{"bars": [a, b]}`` or ``(start_beats, end_beats)``). Kit tracks
    (role ``kit``) are excluded. ``notes_by_track``: ``{key: [events]}`` or
    song_notes output. Runs of the same pair are merged; a run of a beat or
    more is ``sustained``."""
    lo, hi = _window_beats(window, bpb)
    lo_step, hi_step = int(round(lo * _STEPS_PER_BEAT)), int(round(hi * _STEPS_PER_BEAT))
    grid, excluded = _occupancy(notes_by_track, lo_step, hi_step)
    runs: dict = {}
    for step in sorted(grid):
        sounding = sorted(grid[step])
        for i, a in enumerate(sounding):
            for b in sounding[i + 1:]:
                if a[0] == b[0]:
                    continue
                kind = _RUB_CLASSES.get(abs(a[2] - b[2]) % 12)
                if kind:
                    low, high = sorted((a, b), key=lambda n: (n[2], n[0]))
                    runs.setdefault((low, high, kind), []).append(step)
    rubs = []
    for (low, high, kind), steps in runs.items():
        for first, last in _runs(steps):
            start, end = first / _STEPS_PER_BEAT, (last + 1) / _STEPS_PER_BEAT
            rubs.append({"pitches": [low[2], high[2]],
                         "names": [pc_name(pc) for pc in sorted({low[2] % 12, high[2] % 12})],
                         "interval": kind, "tracks": [low[1], high[1]],
                         "track_keys": [low[0], high[0]],
                         "start_beats": start, "end_beats": end,
                         "bars": [int(start // bpb) + 1, int(math.ceil(end / bpb - 1e-9))],
                         "sixteenths": last - first + 1,
                         "sustained": last - first + 1 >= _STEPS_PER_BEAT,
                         "evidence": "set_file"})
    rubs.sort(key=lambda r: (r["start_beats"], r["pitches"], r["tracks"]))
    return {"window": {"start_beats": lo, "end_beats": hi,
                       "bars": [int(lo // bpb) + 1, int(math.ceil(hi / bpb - 1e-9))]},
            "resolution": "16th", "rubs": rubs, "count": len(rubs),
            "sustained_count": sum(1 for r in rubs if r["sustained"]),
            "excluded": sorted(excluded),
            "rule": "different tracks sounding pitch classes a semitone or a tritone apart "
                    "in the same 16th; kits excluded"}


# --- diagnosis ---------------------------------------------------------------------------

_NEEDS = {
    "long_static_intro": ["delayed_entry"],
    "early_peak": ["late_peak", "thin_before_return"],
    "unbridged_gap": ["key_change_bridge", "thin_before_return"],
    "uncommitted_ending": ["strip_ending"],
}
_STATIC_MIN_BARS = 16
_EARLY_PEAK_END_PCT = 75
_ENDING_SHARE = 0.5


def _bars_text(bars) -> str:
    return "%d-%d" % (bars[0], bars[1])


def _finding(detector, bars, question, facts):
    return {"detector": detector, "label": "%s@%s" % (detector, _bars_text(bars)),
            "bars": list(bars), "question": question, "facts": facts,
            "needs": list(_NEEDS[detector])}


def _world_label(world: dict, spans=True) -> str:
    name = world["centre_name"] + (" " + world["mode"] if world.get("mode") else
                                   " drone" if world["kind"] == "drone" else "")
    if spans:
        name += " (%s)" % ", ".join(_bars_text(s) for s in world["spans"])
    return name


def _intro_finding(hmap, arc):
    if not arc["sections"]:
        return None
    run = arc["sections"][0]["longest_static"]
    if not run or run[1] - run[0] + 1 < _STATIC_MIN_BARS:
        return None
    seg = next((s for s in hmap["segments"] if s["bars"][0] <= run[0] <= s["bars"][1]), None)
    held = ("a %s drone" % seg["centre_name"]) if seg and seg["kind"] == "drone" else "the same layers"
    length = run[1] - run[0] + 1
    return _finding("long_static_intro", run,
                    "Bars %s (%d bars) hold %s with no entry or exit. Patience, or should one "
                    "entry arrive later, inside that run?" % (_bars_text(run), length, held),
                    [{"fact": "no layer enters or leaves in bars %s" % _bars_text(run),
                      "evidence": "set_file"},
                     {"fact": "harmony there is %s" % held, "evidence": "inferred"}])


def _peak_finding(arc):
    first = arc["first_peak"]
    if not first or arc["last_peak_exceeds_first"] or first["span_pct"][1] >= _EARLY_PEAK_END_PCT:
        return None
    later = [p for p in arc["peaks"] if p["bars"][0] > first["bars"][0]]
    after = ("the later peak at %s has %d" % (_bars_text(later[-1]["bars"]), later[-1]["layers"])
             if later else "nothing after it comes close")
    return _finding("early_peak", first["bars"],
                    "The densest moment (%d layers) is bars %s, %d-%d%% into the song, and %s. "
                    "Is %s the summit, or a false peak before a bigger return?"
                    % (first["layers"], _bars_text(first["bars"]), first["span_pct"][0],
                       first["span_pct"][1], after, _bars_text(first["bars"])),
                    [{"fact": "%d layers at bars %s" % (first["layers"], _bars_text(first["bars"])),
                      "evidence": "set_file"},
                     {"fact": after, "evidence": "set_file"}])


def _gap_findings(hmap):
    worlds = {w["id"]: w for w in hmap["worlds"]}
    out = []
    for pivot in hmap["pivots"]:
        src, dst = worlds[pivot["from_world"]], worlds[pivot["to_world"]]
        shared = pivot["shared"]
        tone = ("the only tone they share is %s" % shared[0] if len(shared) == 1 else
                "they share %s" % ", ".join(shared) if shared else "they share no tone")
        held = (", already held alone at %s" % _bars_text(pivot["held_alone"])
                if pivot["held_alone"] else "")
        carry = (" or should %s carry across it into the %s figure" % (shared[0], dst["centre_name"])
                 if shared else " or does it need a bridge")
        silent = pivot.get("gap_kind", "silent") == "silent"
        state = "are silent" if silent else "have no pitched layer (kits or audio only)"
        facts = [{"fact": "bars %s %s" % (_bars_text(pivot["gap_bars"]), state),
                  "evidence": "set_file"},
                 {"fact": "%s and %s are a %s apart; %s" % (_world_label(src), _world_label(dst),
                                                           pivot["interval"], tone),
                  "evidence": "inferred"}]
        out.append(_finding("unbridged_gap", pivot["gap_bars"],
                            "Bars %s %s between %s and %s, a %s apart; %s%s. Is the "
                            "%s the breath you want,%s?"
                            % (_bars_text(pivot["gap_bars"]), state, _world_label(src),
                               _world_label(dst), pivot["interval"], tone, held,
                               "silence" if silent else "break", carry),
                            facts))
    return out


def _ending_finding(arc):
    ending = arc["ending"]
    if not ending or not ending["peak_layers"] \
            or ending["layers_at_end"] < _ENDING_SHARE * ending["peak_layers"]:
        return None
    return _finding("uncommitted_ending", ending["bars"],
                    "The song stops at bar %d with %d layers still playing. A hard stop by "
                    "design, or should it strip down before the end?"
                    % (ending["last_bar"], ending["layers_at_end"]),
                    [{"fact": "%d layers in the last bar (peak %d)"
                              % (ending["layers_at_end"], ending["peak_layers"]),
                      "evidence": "set_file"}])


def diagnose(hmap: dict, arc: dict, decisions: dict | None = None) -> dict:
    """Questions, not verdicts: a long static intro run, an early peak never
    exceeded, a silence between two worlds, an ending that stops at density.
    Each finding names the reference moves (``needs``) that could inform it.
    ``decisions["intended"]`` (labels ``detector@first-last``, owner-sourced)
    silences a finding; silenced findings are listed, never dropped."""
    decisions = decisions or {}
    findings = [f for f in ([_intro_finding(hmap, arc), _peak_finding(arc)]
                            + _gap_findings(hmap) + [_ending_finding(arc)]) if f]
    findings.sort(key=lambda f: (f["bars"][0], f["detector"]))
    intended = [str(x) for x in decisions.get("intended") or []]
    silenced = [dict(f, evidence="owner_confirmed", why="owner marked it intended")
                for f in findings if f["label"] in intended]
    labels = {f["label"] for f in findings}
    return {"kind": "diagnosis", "findings": [f for f in findings if f["label"] not in intended],
            "silenced": silenced,
            "unmatched_intended": [x for x in intended if x not in labels]}


# --- references -------------------------------------------------------------------------

_EVIDENCE_RANK = {"owner_confirmed": 3, "model_heard": 2, "measured": 1}
_REAL_DELAY_FRACTION = 0.10
_LAYER_EVIDENCE = {"model_heard", "owner_confirmed"}
_LATE_PEAK_FRACTION = 0.60
_PEAK_WORDS = ("climax", "peak")
_BORROW_OF_MOVE = {"late_peak": "peak_placement", "strip_ending": "ending",
                   "delayed_entry": "layering", "thin_before_return": "density"}


def _clock(seconds: float) -> str:
    seconds = int(round(float(seconds)))
    return "%d:%02d" % (seconds // 60, seconds % 60)


def _section_late_peaks(seq: dict) -> list[dict]:
    """A verified section labelled climax/peak that starts at >= 60% of the
    reference: a late-peak claim about sections (not loudness, not layers)."""
    duration = seq.get("duration_s")
    if not isinstance(duration, (int, float)) or duration <= 0:
        return []
    out = []
    for section in seq.get("sections") or []:
        label = str(section.get("label") or "").lower()
        if section.get("evidence") not in _LAYER_EVIDENCE:
            continue
        if any(word in label for word in _PEAK_WORDS) and "post" not in label \
                and section["t_s"] / duration >= _LATE_PEAK_FRACTION:
            out.append({"kind": "late_peak", "t_s": section["t_s"],
                        "evidence": [section["evidence"]], "status": "inferred",
                        "label": section.get("label"),
                        "rule": "a verified section labelled climax/peak starting at >= 60% "
                                "of the reference"})
    return out


def _claim(move: dict) -> tuple[str, bool, str | None]:
    """(claim type, authorizes a layer change, what evidence is missing)."""
    evidence = set(move.get("evidence") or [])
    if move.get("rule", "").startswith("a verified section"):
        return "sections", False, None
    if move.get("layers") or move.get("authorizes_layer_removal"):
        # the flag alone is not evidence: a verified listen or his ear must back it
        if (evidence | set(move.get("supported_by") or [])) & _LAYER_EVIDENCE:
            return "layers", True, None
        return "loudness", False, "verified layer-change evidence or owner confirmation"
    if evidence <= {"measured"}:
        return "loudness", False, (move.get("missing_evidence")
                                   or "verified layer-change evidence or owner confirmation")
    return "sections", False, None


def _sentence(title: str, move: dict, claim: str) -> str:
    at = _clock(move["t_s"])
    kind = move["kind"]
    if claim == "loudness":
        return {"thin_before_return": "%s gets quieter for a while at %s before it returns "
                                      "(loudness only; which layers leave is not evidenced)",
                "late_peak": "%s is loudest late, from %s (loudness only)",
                "strip_ending": "%s drops to a quiet tail from %s (loudness only)"}.get(
            kind, "%s changes level at %s (loudness only)") % (title, at)
    if kind == "late_peak":
        return "%s saves its climax for %s (%s)" % (title, at, move.get("label") or "section")
    layers = ", ".join(move.get("layers") or []) or "layers"
    return {"strip_ending": "%s strips %s at %s and ends on what is left",
            "delayed_entry": "%s holds %s back until %s",
            "thin_before_return": "%s thins (%s leave) at %s before the return"}.get(
        kind, "%s: %s at %s") % (title, layers, at)


def _ref_moves(seq: dict) -> list[dict]:
    """The reference's moves plus section-label late peaks; an 'entry' in the
    first 10% of a reference is its opening, not a delay, and is left out."""
    duration = seq.get("duration_s")
    moves = [m for m in seq.get("moves") or [] if isinstance(m, dict) and "t_s" in m
             and not (m.get("kind") == "delayed_entry" and isinstance(duration, (int, float))
                      and duration > 0 and m["t_s"] / duration < _REAL_DELAY_FRACTION)]
    return moves + _section_late_peaks(seq)


def _match(entry: dict, move: dict, need: str, findings: list) -> dict | None:
    evidence = sorted({e for e in (move.get("evidence") or []) + (move.get("supported_by") or [])
                       if e in _EVIDENCE_RANK})
    if not evidence:
        return None
    claim, authorizes, missing = _claim(move)
    duration = entry["sequence"].get("duration_s")
    row = {"need": need, "finding": findings[0], "findings": list(findings),
           "kind": move["kind"], "t_s": _r(move["t_s"], 1),
           "t": _clock(move["t_s"]),
           "fraction": _r(move["t_s"] / duration, 2) if duration else None,
           # layer names are a layer claim: only a layer-evidenced move carries them
           "layers": list(move.get("layers") or []) if claim == "layers" else [],
           "evidence": evidence,
           "claim": claim, "authorizes_layer_change": authorizes,
           "strength": max(_EVIDENCE_RANK[e] for e in evidence),
           "sentence": _sentence(entry.get("title") or entry["ref_id"], move, claim)}
    if missing:
        row["missing"] = missing
    return row


def rank_references(refs: list[dict], diagnosis: dict, intent: dict | None = None, *,
                    tempo: float | None = None) -> dict:
    """Match typed reference moves to the diagnosed needs.

    ``refs``: ``[{"ref_id", "title", "tags", "sequence": reference_sequence(...),
    "owner_facts": [...]?}]``. Gate: a layer claim needs ``model_heard`` (only
    produced by a verified listen) or ``owner_confirmed``; ``measured``-only
    moves support a loudness sentence and never authorize a layer change;
    ``model_unverified`` is never evidence. Ranking: per need, the strongest
    evidence first (owner > model-heard > measured), then intent-tag match, then
    same tempo. The shortlist ranks refs by summed best evidence per need +1
    for an intent tag +0.5 for a grid within 3% of ``tempo``. Needs no ref
    supports are said plainly."""
    intent = intent or {}
    tags = set(intent.get("tags") or [])
    borrow = set(intent.get("borrow") or [])
    depart = set(intent.get("depart") or [])
    wanted: dict = {}
    for finding in diagnosis.get("findings") or []:
        for need in finding["needs"]:
            wanted.setdefault(need, []).append(finding["label"])
    need_order = list(wanted)
    shortlist, rejected, by_need = [], [], {}
    for entry in sorted(refs, key=lambda e: str(e.get("ref_id"))):
        seq = entry.get("sequence") or {}
        matches = []
        for need, findings in wanted.items():
            for move in _ref_moves(seq):
                if move["kind"] == need:
                    row = _match(entry, move, need, findings)
                    if row:
                        row["in_borrow"] = _BORROW_OF_MOVE.get(need) in borrow
                        row["departed"] = _BORROW_OF_MOVE.get(need) in depart
                        if row["departed"]:
                            row["note"] = ("you chose to depart from %s: this %s is listed "
                                           "last and never borrowed"
                                           % (_BORROW_OF_MOVE[need], need.replace("_", " ")))
                        matches.append(row)
        if not matches:
            warnings = seq.get("warnings") or []
            rejected.append({"ref_id": entry.get("ref_id"),
                             "reason": "; ".join(warnings) or "no move matches a diagnosed need"})
            continue
        grid = (seq.get("tempo") or {}).get("grid_bpm")
        same_tempo = bool(tempo and isinstance(grid, (int, float)) and grid > 0
                          and abs(grid - tempo) / tempo <= 0.03)
        tagged = bool(tags & set(entry.get("tags") or []))
        best = {}
        for row in matches:
            if not row["departed"]:
                best[row["need"]] = max(best.get(row["need"], 0), row["strength"])
            by_need.setdefault(row["need"], []).append(
                {"ref_id": entry.get("ref_id"), "title": entry.get("title"), "tagged": tagged,
                 "same_tempo": same_tempo, **{k: row[k] for k in (
                     "t", "t_s", "fraction", "layers", "evidence", "claim",
                     "authorizes_layer_change", "strength", "sentence", "departed")},
                 **({"note": row["note"]} if row["departed"] else {})})
        matches.sort(key=lambda m: (need_order.index(m["need"]), m["departed"], -m["strength"],
                                    m["t_s"]))
        shortlist.append({"ref_id": entry.get("ref_id"), "title": entry.get("title"),
                          "tags": sorted(entry.get("tags") or []), "tagged": tagged,
                          "grid_bpm": grid, "same_tempo": same_tempo,
                          "score": sum(best.values()) + (1.0 if tagged else 0.0)
                                   + (0.5 if same_tempo else 0.0),
                          "needs_covered": sorted(best), "matches": matches,
                          "owner_facts": list(entry.get("owner_facts") or [])})
    shortlist.sort(key=lambda r: (-r["score"], str(r["ref_id"])))
    for rows in by_need.values():
        rows.sort(key=lambda r: (r["departed"], -r["strength"], not r["authorizes_layer_change"],
                                 not r["tagged"], not r["same_tempo"], str(r["ref_id"]), r["t_s"]))
    unsupported = [{"need": need, "finding": findings[0], "findings": findings,
                    "say": "no reference in this set supports %s; anything proposed for it is "
                           "our inference from your own material" % need.replace("_", " ")}
                   for need, findings in wanted.items() if need not in by_need]
    departed = [{"need": need, "depart": _BORROW_OF_MOVE[need],
                 "refs": sorted({str(r["ref_id"]) for r in by_need.get(need, [])}),
                 "say": "you chose to depart from %s: references for %s are demoted, not borrowed"
                        % (_BORROW_OF_MOVE[need], need.replace("_", " ")), "evidence": "owner_confirmed"}
                for need in need_order if _BORROW_OF_MOVE.get(need) in depart]
    return {"kind": "reference_ranking", "needs": need_order, "departed": departed,
            "shortlist": shortlist, "by_need": by_need, "unsupported": unsupported,
            "rejected": rejected,
            "gate": "layer claims need model_heard (verified listen) or owner_confirmed; "
                    "measured supports loudness sentences only"}


# --- arrangement plan ----------------------------------------------------------------------

_SESSION_RULE = ("bridge distant worlds through the shared tone (hold it alone), then let the "
                 "new key's figure enter over it")
_STRETCH_ORDER = ("bridge", "real peak", "coda", "thinning")
_CODA_BARS = 8
_FIGURE_BARS = 4
_BASS_BELOW = 48          # median pitch under C3: a bass-register layer
_SUSTAINED_BEATS = 1.0    # median note of a beat or more: a sustained layer


def _entry(track: dict, role: str, from_bars, offset: int, span: int, part: str = "his") -> dict:
    return {"track_id": track["id"], "track": track["name"], "role": role,
            "from_bars": list(from_bars), "offset": offset, "span": span, "part": part,
            "copy": True}


def _section_row(role, source, length, entries, now_bars=None, **extra):
    return {"role": role, "source_bars": list(source) if source else None,
            "now_bars": list(now_bars) if now_bars else None, "length_bars": length,
            "tracks": entries, "changes": [], "borrow": [], "preserve": [], "test": [],
            "questions": [], "stretched_bars": 0, **extra}


def _events_in(events: list[dict], lo: float, hi: float) -> list[dict]:
    return [ev for ev in events if ev["start"] < hi and ev["start"] + ev["duration"] > lo]


def _best_ref(refs: dict, need: str) -> dict | None:
    rows = [r for r in (refs or {}).get("by_need", {}).get(need) or [] if not r.get("departed")]
    return rows[0] if rows else None


_DRUM_WORDS = ("drum", "kick", "perc", "hat", "snare", "ride", "clap", "rim")


def _pick_delay(refs: dict, kit: bool) -> dict | None:
    """The delayed entry to cite: one that holds drums back when the plan
    delays a kit, strongest first."""
    rows = [r for r in (refs or {}).get("by_need", {}).get("delayed_entry") or []
            if not r.get("departed")]
    drums = [r for r in rows if any(w in layer.lower() for layer in r["layers"]
                                    for w in _DRUM_WORDS)]
    pool = drums if kit and drums else rows
    return pool[0] if pool else None


def _borrow_ref(move: str, ref: dict | None) -> dict | None:
    if not ref:
        return None
    return {"move": move, "ref_id": ref["ref_id"], "title": ref.get("title"), "t": ref["t"],
            "layers": ref["layers"], "evidence": ref["evidence"][0] if len(ref["evidence"]) == 1
            else max(ref["evidence"], key=lambda e: _EVIDENCE_RANK.get(e, 0)),
            "claim": ref["claim"], "sentence": ref["sentence"]}


def _cite(ctx, row: dict, move: str, ref: dict | None) -> None:
    """Borrow the reference move for this plan rule, unless he departs from
    its direction: then the rule stands as our inference and says so."""
    value = _BORROW_OF_MOVE.get(move)
    if value in (ctx.decisions.get("depart") or []):
        row["test"].append({"what": "you depart from %s: this %s is our inference from your "
                                    "material, not borrowed from a reference"
                                    % (value, move.replace("_", " ")),
                            "evidence": "owner_confirmed"})
        return
    borrow = _borrow_ref(move, ref)
    if borrow:
        row["borrow"].append(borrow)


def _borrowed(key: str) -> dict:
    move = ic.BORROWED[key]
    return {"move": key, "ref_id": None, "title": "%s (%s)" % (move["artist"], move["source"]),
            "evidence": move["evidence"], "claim": "set_file",
            "sentence": "%s — %s; fits when %s" % (move["artist"], move["quote"].strip("*"),
                                                   move["fits"])}


_UNKNOWN_MATERIAL = ("audio", "unread")
_UNKNOWN_WHY = {"audio": "audio, never heard", "unread": "its notes were not read"}


class _PlanContext:
    """What the plan rules read: tracks by id, roles, events, protected ids."""

    def __init__(self, snapshot, hmap, arc, diagnosis, refs, decisions):
        self.snapshot, self.hmap, self.arc, self.refs = snapshot, hmap, arc, refs or {}
        self.bpb = _bpb(snapshot)
        self.tracks = {t["id"]: t for t in snapshot.get("tracks") or []}
        self.order = [t["id"] for t in snapshot.get("tracks") or []]
        self.roles = {r["track_id"]: r["role"] for r in hmap["roles"]}
        self.events = {tid: _track_events(self.tracks[tid]) for tid in self.roles}
        self.protected = {str(t) for t in (decisions or {}).get("protect_track_ids") or []}
        self.findings = {f["detector"]: f for f in diagnosis.get("findings") or []}
        self.decisions = decisions or {}

    def material(self, tid: str, bars) -> list[str]:
        """What his placed clips on a track hold over ``bars``: ``notes``
        (audible notes there), ``silent`` (read, nothing sounds there),
        ``audio`` (never heard) and ``unread`` (notes not read). Muted clips
        hold nothing."""
        lo, hi = (bars[0] - 1) * self.bpb, bars[1] * self.bpb
        states = set()
        for clip in self.tracks[tid].get("arrangement_clips") or []:
            if clip.get("muted") or clip["start_beats"] >= hi or clip["end_beats"] <= lo:
                continue
            if clip.get("kind") == "audio":
                states.add("audio")
            elif clip.get("notes_status") != "read":
                states.add("unread")
            elif _events_in(ps.clip_events(clip), lo, hi):
                states.add("notes")
            else:
                states.add("silent")
        return sorted(states)

    def unknown(self, tid: str, bars) -> list[str]:
        """The material over ``bars`` whose pitches are not known."""
        return [m for m in self.material(tid, bars) if m in _UNKNOWN_MATERIAL]

    def playing(self, tid: str, bars) -> bool:
        lo, hi = (bars[0] - 1) * self.bpb, bars[1] * self.bpb
        return bool(_events_in(self.events.get(tid, []), lo, hi))

    def median_pitch(self, tid, bars=None) -> float:
        events = self.events.get(tid, [])
        if bars:
            events = _events_in(events, (bars[0] - 1) * self.bpb, bars[1] * self.bpb)
        return _median([ev["pitch"] for ev in events])

    def median_duration(self, tid, bars) -> float:
        lo, hi = (bars[0] - 1) * self.bpb, bars[1] * self.bpb
        return _median([ev["duration"] for ev in _events_in(self.events.get(tid, []), lo, hi)])

    def pcs(self, tid, bars) -> set:
        lo, hi = (bars[0] - 1) * self.bpb, bars[1] * self.bpb
        return {ev["pitch"] % 12 for ev in _events_in(self.events.get(tid, []), lo, hi)}

    def by_order(self, ids):
        return sorted(ids, key=self.order.index)


def _base_sections(ctx: _PlanContext) -> list[dict]:
    """One plan section per snapshot section: every track with a clip placed
    there — notes, audio, unread MIDI or known-silent — copied in place, so
    the baseline is what his Set holds, not only what could be read. Roles
    from the layer arc."""
    first_peak = (ctx.arc.get("first_peak") or {}).get("bars")
    rows, after_peak, after_gap = [], False, False
    for index, sec in enumerate(ctx.snapshot.get("sections") or []):
        bars = sec["bars"]
        length = bars[1] - bars[0] + 1
        if sec["silent"]:
            role, after_gap = "gap", True
        elif index == 0:
            role = "opening"
        elif bars == first_peak:
            role, after_peak = "peak", True
        elif after_gap:
            role = "return"
        elif after_peak:
            role = "thinning"
        else:
            role = "build"
        entries = [_entry(ctx.tracks[tid], ctx.roles[tid], bars, 0, length)
                   for tid in ctx.by_order(t["track_id"] for t in sec["tracks"])
                   if tid in ctx.roles and ctx.material(tid, bars)]
        rows.append(_section_row(role, bars, length, entries, now_bars=bars))
        split = [seg["bars"] for seg in ctx.hmap["segments"]
                 if seg["split"] and seg["split"]["from_section"] == list(bars)]
        if split:
            rows[-1]["edge_split"] = split
        rows[-1]["changes"].append("as now: copies of the same clips at the same place")
    return rows


def _delay_entry(ctx: _PlanContext, rows: list[dict]) -> None:
    finding = ctx.findings.get("long_static_intro")
    if not finding or not rows:
        return
    opening = rows[0]
    run = finding["bars"]
    start = max(run[0], run[1] - 7)
    kits = [e for e in opening["tracks"] if e["role"] == "kit" and e["track_id"] not in ctx.protected
            and ctx.playing(e["track_id"], run)]
    candidates = kits or [e for e in opening["tracks"] if e["track_id"] not in ctx.protected
                          and ctx.playing(e["track_id"], run)][-1:]
    if not candidates:
        return
    moved = []
    for entry in candidates:
        entry["from_bars"] = [start, opening["source_bars"][1]]
        entry["offset"] = start - opening["source_bars"][0]
        entry["span"] = opening["source_bars"][1] - start + 1
        moved.append(entry["track"])
    opening["changes"] = ["%s enters at bar %d instead of now (delayed entry)"
                          % (" + ".join(moved), start)]
    kit_moved = any(e["role"] == "kit" for e in candidates)
    _cite(ctx, opening, "delayed_entry", _pick_delay(ctx.refs, kit_moved))
    opening["test"].append({"what": "delay %s to bar %d inside the static run %s"
                                    % (" + ".join(moved), start, _bars_text(run)),
                            "evidence": "inferred"})
    opening["questions"].append(finding["question"])


def _false_peak(ctx: _PlanContext, rows: list[dict]) -> list[str]:
    """Hold back the layers that enter at the early peak (never protected ones)."""
    finding = ctx.findings.get("early_peak")
    peak = next((r for r in rows if r["role"] == "peak"), None)
    if not finding or not peak:
        return []
    sec = next(s for s in ctx.snapshot["sections"] if s["bars"] == peak["source_bars"])
    entering = [t["track_id"] for t in sec["enters"]] or [peak["tracks"][-1]["track_id"]]
    held = [tid for tid in ctx.by_order(entering) if tid not in ctx.protected]
    for tid in entering:
        if tid in ctx.protected:
            peak["preserve"].append({"what": "%s stays in the peak (protected by you)"
                                             % ctx.tracks[tid]["name"],
                                     "evidence": "owner_confirmed"})
    peak["role"] = "false peak"
    peak["tracks"] = [e for e in peak["tracks"] if e["track_id"] not in held]
    if held:
        names = " + ".join(ctx.tracks[tid]["name"] for tid in held)
        peak["changes"] = ["%s held back: the peak is no longer the fullest moment" % names]
        peak["borrow"].append(_borrowed("hold_a_world_back"))
        peak["test"].append({"what": "hold back %s at %s" % (names, _bars_text(peak["source_bars"])),
                             "evidence": "inferred"})
    _cite(ctx, peak, "late_peak", _best_ref(ctx.refs, "late_peak"))
    peak["questions"].append(finding["question"])
    return held


def _real_peak(ctx: _PlanContext, rows: list[dict]) -> None:
    """Every return section plays each tonal layer of the return whose pitch
    classes lie inside that world's core, plus the early peak's kits (drums are
    key-free): the fullest moment of the song. A layer with outside tones stays
    only where he placed it."""
    finding = ctx.findings.get("early_peak")
    returns = [r for r in rows if r["role"] == "return"]
    peak = next((r for r in rows if r["role"] == "false peak"), None)
    if not finding or not returns or not peak:
        return
    union = {}
    for row in returns:
        for entry in row["tracks"]:
            union.setdefault(entry["track_id"], (row["source_bars"], entry["role"]))
    kits = [e["track_id"] for e in peak["tracks"] if e["role"] == "kit"]
    for row in returns:
        row["role"] = "real peak"
        present = {e["track_id"] for e in row["tracks"]}
        core = _section_core(ctx, row["source_bars"])
        added = []
        for tid in ctx.by_order(union):
            if tid in present:
                continue
            source, role = union[tid]
            unknown = ctx.unknown(tid, source)
            if unknown:
                row["changes"].append("%s not added here: %s, so whether it fits this world "
                                      "is unknown" % (ctx.tracks[tid]["name"], " and ".join(
                                          _UNKNOWN_WHY[m] for m in unknown)))
                continue
            outside = ctx.pcs(tid, source) - core
            if outside:
                row["changes"].append("%s not added here: it plays %s, outside this world's "
                                      "core" % (ctx.tracks[tid]["name"], ", ".join(
                                          pc_name(pc, 6) for pc in sorted(outside))))
                continue
            row["tracks"].append(_entry(ctx.tracks[tid], role, source, 0, row["length_bars"]))
            added.append(ctx.tracks[tid]["name"])
        for tid in ctx.by_order(kits):
            if tid not in present:
                row["tracks"].append(_entry(ctx.tracks[tid], "kit", peak["source_bars"], 0,
                                            row["length_bars"]))
                added.append(ctx.tracks[tid]["name"])
        row["tracks"].sort(key=lambda e: ctx.order.index(e["track_id"]))
        row["changes"] = [c for c in row["changes"] if not c.startswith("as now")]
        if added:
            row["changes"].insert(0, "adds %s: the return's layers that fit its world, plus the "
                                     "early peak's kits" % ", ".join(added))
            row["test"].append({"what": "the return as the real peak (%d layers)"
                                        % len({e["track_id"] for e in row["tracks"]}),
                                "evidence": "inferred"})
        row["questions"].append("Do you want the drums back in the return, or a drumless "
                                "doom coda?")
        _cite(ctx, row, "late_peak", _best_ref(ctx.refs, "late_peak"))


def _section_core(ctx: _PlanContext, bars) -> set:
    """Core pitch classes of the world(s) sounding in a span of the song."""
    core: set = set()
    for seg in ctx.hmap["segments"]:
        if seg["world"] and seg["bars"][0] <= bars[1] and seg["bars"][1] >= bars[0]:
            world = next(w for w in ctx.hmap["worlds"] if w["id"] == seg["world"])
            core |= _core_pcs(world)
    return core


def _pivot_tracks(ctx: _PlanContext, held_bars, pc: int) -> list[str]:
    return [tid for tid in ctx.order if ctx.roles.get(tid) == "tonal"
            and ctx.pcs(tid, held_bars) == {pc}]


def _bridge(ctx: _PlanContext, rows: list[dict]) -> None:
    finding = ctx.findings.get("unbridged_gap")
    if not finding:
        return
    pivot = next((p for p in ctx.hmap["pivots"] if p["gap_bars"] == finding["bars"]), None)
    index = next((i for i, r in enumerate(rows) if r["source_bars"] == finding["bars"]), None)
    if pivot is None or index is None or index + 1 >= len(rows):
        return
    row, after = rows[index], rows[index + 1]
    row["role"] = "bridge"
    row["pivot"] = pivot["shared"][0] if pivot["shared"] else None
    length = row["length_bars"]
    if pivot["held_alone"] and pivot["shared"]:
        pc = next(pc for pc in range(12)
                  if pc_name(pc, 6 if "#" in pivot["shared"][0] else 0) == pivot["shared"][0])
        holders = _pivot_tracks(ctx, pivot["held_alone"], pc)
        if holders:
            tid = holders[0]
            row["tracks"].append(_entry(ctx.tracks[tid], "tonal", pivot["held_alone"], 0, length,
                                        part="pivot"))
            row["changes"].append("%s holds %s alone across the silence (its %s clip, looped)"
                                  % (ctx.tracks[tid]["name"], row["pivot"],
                                     _bars_text(pivot["held_alone"])))
    if not row["tracks"]:
        row["changes"].append("no track of his holds %s alone: a SIDEKICK pedal carries it "
                              "(compose_parts)" % (row["pivot"] or "a shared tone"))
    # the held tone first, alone; the figure takes the last half of the gap
    # (at most 4 bars) and never more than the return section it comes from
    after_len = after["source_bars"][1] - after["source_bars"][0] + 1
    figure_len = min(_FIGURE_BARS, length // 2, after_len)
    if figure_len < 1:
        row["changes"] = [c for c in row["changes"] if not c.startswith("as now")]
        row["test"].append({"what": _SESSION_RULE, "evidence": "inferred"})
        _cite(ctx, row, "thin_before_return", _best_ref(ctx.refs, "thin_before_return"))
        row["questions"].append(finding["question"])
        row["questions"].append("The silence is one bar: there is no room for the return's "
                                "figure after the held tone. Lengthen the bridge (e.g. to 4 "
                                "bars) to let it enter, or keep the single bar?")
        return
    figure_bars = [after["source_bars"][0], after["source_bars"][0] + figure_len - 1]
    world = next((w for w in ctx.hmap["worlds"] if w["id"] == pivot["to_world"]), None)
    core = _core_pcs(world) if world else set()
    tonal = [e for e in after["tracks"] if e["role"] == "tonal"
             and ctx.playing(e["track_id"], [figure_bars[0], figure_bars[0]])
             and len(ctx.pcs(e["track_id"], figure_bars)) >= 2
             and ctx.pcs(e["track_id"], figure_bars) <= core]
    if tonal:
        top = max(tonal, key=lambda e: (ctx.median_pitch(e["track_id"], figure_bars),
                                        -ctx.order.index(e["track_id"])))
        row["tracks"].append(_entry(ctx.tracks[top["track_id"]], "tonal", figure_bars,
                                    length - figure_len, figure_len, part="figure"))
        row["changes"].append("%s's opening figure (%s) enters over it for the last %d bar%s"
                              % (top["track"], _bars_text(figure_bars), figure_len,
                                 "" if figure_len == 1 else "s"))
    row["changes"] = [c for c in row["changes"] if not c.startswith("as now")]
    row["test"].append({"what": _SESSION_RULE, "evidence": "inferred"})
    _cite(ctx, row, "thin_before_return", _best_ref(ctx.refs, "thin_before_return"))
    row["questions"].append(finding["question"])


def _coda(ctx: _PlanContext, rows: list[dict]) -> None:
    finding = ctx.findings.get("uncommitted_ending")
    if not finding or not rows:
        return
    last = rows[-1]
    source = last["source_bars"]
    upper = [e for e in last["tracks"] if e["role"] == "tonal"
             and ctx.median_pitch(e["track_id"], e["from_bars"]) >= _BASS_BELOW
             and ctx.median_duration(e["track_id"], e["from_bars"]) >= _SUSTAINED_BEATS]
    guarded = [e for e in last["tracks"] if e["track_id"] in ctx.protected]
    if not upper and not [e for e in guarded if e["role"] == "tonal"]:
        return
    figure = max(upper or [e for e in guarded if e["role"] == "tonal"],
                 key=lambda e: (ctx.median_pitch(e["track_id"], e["from_bars"]),
                                -ctx.order.index(e["track_id"])))
    keep = [e for e in last["tracks"] if e in upper or e in guarded]
    half = _CODA_BARS // 2
    # the figure and every protected layer play to the end; the rest leave halfway
    entries = [_entry(ctx.tracks[e["track_id"]], e["role"], e["from_bars"], 0,
                      _CODA_BARS if e is figure or e in guarded else half) for e in keep]
    coda = _section_row("coda", source, _CODA_BARS, entries)
    coda["coda"] = {"figure": figure["track"],
                    "dropped": sorted({e["track"] for e in last["tracks"]}
                                      - {e["track"] for e in keep})}
    _cite(ctx, coda, "strip_ending", _best_ref(ctx.refs, "strip_ending"))
    coda["test"].append({"what": "strip kits, bass-register and short-note layers; keep the "
                                 "sustained upper layers", "evidence": "inferred"})
    coda["questions"].append(finding["question"])
    rows.append(coda)


def _coda_text(row: dict) -> None:
    """The coda's change line, written from its final entries (after any stretch)."""
    info = row.pop("coda")
    end = row["bars"][1]
    last = [e["track"] for e in row["tracks"] if e["at_bars"][1] == end]
    earlier = [e["at_bars"][1] for e in row["tracks"] if e["at_bars"][1] < end]
    alone = end - max(earlier) if earlier else row["length_bars"]
    tail = ("%s last alone" % info["figure"] if last == [info["figure"]]
            else "%s play to the end (%s protected by you)"
            % (", ".join(last), ", ".join(t for t in last if t != info["figure"])))
    row["changes"] = ["new %d bars after the song's end: %s leave; %s for the final %d bars"
                      % (row["length_bars"], ", ".join(info["dropped"]) or "nothing", tail, alone)]


def _stretch(rows: list[dict], target_bars: int | None) -> list[str]:
    total = sum(r["length_bars"] for r in rows)
    if not target_bars or target_bars <= total:
        return (["the plan (%d bars) is already at or past the target (%d bars): which section "
                 "should give?" % (total, target_bars)] if target_bars and target_bars < total
                else [])
    # a section split at its edges (a phrase held apart, e.g. the pivot held
    # alone) would loop that phrase back into the body: it keeps its length
    order = [r for role in _STRETCH_ORDER for r in rows
             if r["role"] == role and not r.get("edge_split")]
    i = 0
    while total < target_bars and order:
        row = order[i % len(order)]
        step = min(_PHRASE_BARS, target_bars - total)
        row["length_bars"] += step
        row["stretched_bars"] += step
        for entry in row["tracks"]:
            if entry["part"] == "figure":
                entry["offset"] += step
            elif entry["offset"] + entry["span"] == row["length_bars"] - step:
                entry["span"] += step
        total += step
        i += 1
    for row in order:
        if row["stretched_bars"]:
            row["test"].append({"what": "+%d bars (loops of the same clips) toward the target "
                                        "length" % row["stretched_bars"], "evidence": "inferred"})
    return []


def _finalize(ctx: _PlanContext, rows: list[dict]) -> None:
    bar = 1
    for index, row in enumerate(rows):
        row["index"] = index
        row["bars"] = [bar, bar + row["length_bars"] - 1]
        for entry in row["tracks"]:
            offset, span = entry.pop("offset"), entry.pop("span")
            if offset < 0 or span < 1 or offset + span > row["length_bars"]:
                raise ValueError("plan entry %s (offset %d, %d bars) does not fit section %d "
                                 "(%d bars)" % (entry["track"], offset, span, index,
                                                row["length_bars"]))
            start = bar + offset
            entry["at_bars"] = [start, start + span - 1]
            track = ctx.tracks[entry["track_id"]]
            lo, hi = (entry["from_bars"][0] - 1) * ctx.bpb, entry["from_bars"][1] * ctx.bpb
            clips = [c for c in track["arrangement_clips"]
                     if c["start_beats"] < hi and c["end_beats"] > lo and not c.get("muted")]
            entry["clip_ids"] = [c["id"] for c in clips]
            entry["material"] = ctx.material(entry["track_id"], entry["from_bars"])
            entry["hazards"] = [h for h in track.get("hazards") or []
                                if h.get("type") != "sidechain_unknown"]
            entry["flags"] = ["clip %s is shorter than a bar (%s at %s)" % (c["id"], c["length"],
                                                                            c["start"])
                              for c in clips if c.get("sub_bar")] + \
                             ["clip %s is not bar-aligned (%s at %s): copied at the same offset"
                              % (c["id"], c["length"], c["start"])
                              for c in clips if not c.get("bar_aligned") and not c.get("sub_bar")]
        # a known-silent copy keeps the arrangement as it is but adds no layer
        row["layers"] = len({e["track_id"] for e in row["tracks"]
                             if e["material"] != ["silent"]})
        if row.get("coda"):
            _coda_text(row)
        bar += row["length_bars"]


def _hazard_flags(ctx: _PlanContext, rows: list[dict]) -> list[dict]:
    """One flag per hazard track the plan copies onto (sidechains are global
    and said once elsewhere): what the chain means for a copy, and which
    copies are moved or looped rather than left in place."""
    flags = []
    for tid in ctx.order:
        track = ctx.tracks[tid]
        hazards = [h for h in track.get("hazards") or [] if h.get("type") != "sidechain_unknown"]
        copies = [{"section": row["index"], "role": row["role"], "from_bars": e["from_bars"],
                   "at_bars": e["at_bars"], "in_place": e["at_bars"] == e["from_bars"]}
                  for row in rows for e in row["tracks"] if e["track_id"] == tid]
        if not hazards or not copies:
            continue
        said = []
        for h in hazards:
            other = "%s (track %s)" % (h.get("track"), h.get("track_id"))
            if h["type"] == "external_instrument_return" and h.get("role") == "midi_to_hardware":
                said.append("its MIDI plays external hardware whose sound returns on %s: a copy "
                            "sounds only with the hardware on and routed" % other)
            elif h["type"] == "external_instrument_return":
                said.append("it is the audio return of external hardware played by %s" % other)
            elif h["type"] == "fed_by_track":
                said.append("it is fed by %s (via %s): what it plays also depends on that "
                            "track, which is not copied" % (other, h.get("via")))
            elif h["type"] == "feeds_track":
                said.append("it feeds %s (via %s): a copy is also heard or recorded there"
                            % (other, h.get("via")))
            elif h["type"] in ("input_unknown", "output_unknown"):
                said.append("its %s routing is unknown" % h["type"].split("_")[0])
            else:
                said.append(h["type"].replace("_", " "))
        moved = [c for c in copies if not c["in_place"]]
        flags.append({"track_id": tid, "track": track["name"],
                      "types": sorted({h["type"] for h in hazards}), "copies": copies,
                      "evidence": "set_file",
                      "say": "%s: %s; %s" % (track["name"], "; ".join(dict.fromkeys(said)),
                                             "%d of %d copies are moved or looped (%s)"
                                             % (len(moved), len(copies), ", ".join(
                                                 _bars_text(c["at_bars"]) for c in moved))
                                             if moved else "every copy stays in place")})
    return flags


def arrangement_plan(snapshot: dict, hmap: dict, arc: dict, diagnosis: dict, refs: dict | None,
                     decisions: dict | None = None, *, target_duration_s: float | None = None) -> dict:
    """A whole-song plan built only from copies of his clips.

    Starts from the snapshot's sections copied in place, then answers each open
    finding with one rule: a long static intro delays its kit (or last entrant)
    into the last 8 bars of the run; an early peak holds back the layers that
    entered there (Frost: hold a world back) and the return becomes the real
    peak (its tonal layers inside the world's core + the early peak's kits);
    a silence between worlds becomes a bridge (the track that held the shared tone alone
    keeps holding it; the return's top figure enters over it for the last 4
    bars); a dense ending gains an 8-bar coda of the sustained upper layers,
    the top one alone for the last 4. Protected tracks are never held back,
    delayed or stripped. ``target_duration_s`` stretches bridge / real peak /
    coda / thinning in 4-bar loops; the plan never cuts toward a target."""
    ctx = _PlanContext(snapshot, hmap, arc, diagnosis, refs, decisions)
    rows = _base_sections(ctx)
    _delay_entry(ctx, rows)
    held = _false_peak(ctx, rows)
    _real_peak(ctx, rows)
    _bridge(ctx, rows)
    _coda(ctx, rows)
    tempo = float(snapshot["set"]["tempo"])
    target_bars = None
    if target_duration_s:
        target_bars = int(round(target_duration_s * tempo / 60.0 / ctx.bpb / _PHRASE_BARS)) \
            * _PHRASE_BARS
    questions = _stretch(rows, target_bars)
    _finalize(ctx, rows)
    for row in rows:
        for tid in ctx.by_order(ctx.protected & {e["track_id"] for e in row["tracks"]}):
            row["preserve"].append({"what": "%s plays as you placed it (protected)"
                                            % ctx.tracks[tid]["name"],
                                    "evidence": "owner_confirmed"})
        for item in ctx.decisions.get("preserve") or []:
            row["preserve"].append({"what": "%s: every layer is a copy of your clip on your track"
                                            % item, "evidence": "owner_confirmed"})
    rendered = render_plan(snapshot, {"sections": rows})
    for row in rows:
        unverified = _unverified([row], row["bars"])
        row["clashes"] = {**_summary(clash_check(rendered, {"bars": row["bars"]}, bpb=ctx.bpb)),
                          "complete": not unverified, "unverified": unverified}
    false_peak = next((r for r in rows if r["role"] == "false peak"), None)
    real = [r["layers"] for r in rows if r["role"] == "real peak"]
    total = sum(r["length_bars"] for r in rows)
    return {"kind": "arrangement_plan", "sections": rows, "total_bars": total,
            "duration_s": _r(total * ctx.bpb * 60.0 / tempo, 1),
            "target": {"duration_s": target_duration_s, "bars": target_bars},
            "held_back": [ctx.tracks[tid]["name"] for tid in held],
            "real_peak_exceeds_false_peak": bool(false_peak and real
                                                 and max(real) > false_peak["layers"]),
            "questions": questions, "writes": "none",
            "hazard_flags": _hazard_flags(ctx, rows),
            "copies_only": "every layer is a copy of one of his clips; staging is 1.5",
            "hazard_note": "sidechains are unknown until the saved .als is read"}


def _unverified(rows: list[dict], bars) -> list[dict]:
    """Plan entries over ``bars`` whose pitches are unknown (audio, unread
    MIDI): a clash check there covers only the material that was read."""
    out = []
    for row in rows:
        for entry in row["tracks"]:
            at = entry.get("at_bars")
            unknown = [m for m in entry.get("material") or [] if m in _UNKNOWN_MATERIAL]
            if at and unknown and at[0] <= bars[1] and at[1] >= bars[0]:
                out.append({"track_id": entry["track_id"], "track": entry["track"],
                            "bars": list(at), "material": unknown,
                            "why": "%s: its pitches are not in the check"
                                   % " and ".join(_UNKNOWN_WHY[m] for m in unknown)})
    return out


def _summary(report: dict) -> dict:
    return {"count": report["count"], "sustained_count": report["sustained_count"],
            "rubs": [{k: r[k] for k in ("names", "interval", "tracks", "bars", "sustained")}
                     for r in report["rubs"] if r["sustained"]][:8]}


def render_plan(snapshot: dict, plan: dict) -> dict:
    """His copied material in plan time: ``{track_id: {"track", "role", "events"}}``.
    Each entry's source window loops to fill its plan window; a note sounding
    into the source window starts at its edge, and a note is cut at the
    source window's end and at the plan window's end."""
    bpb = _bpb(snapshot)
    tracks = {t["id"]: t for t in snapshot.get("tracks") or []}
    cache: dict = {}
    out: dict = {}
    for row in plan["sections"]:
        for entry in row["tracks"]:
            tid = entry["track_id"]
            if tid not in cache:
                cache[tid] = _track_events(tracks[tid])
            at = entry.get("at_bars")
            if at is None:
                continue
            src_lo, src_hi = (entry["from_bars"][0] - 1) * bpb, entry["from_bars"][1] * bpb
            dst_lo, dst_hi = (at[0] - 1) * bpb, at[1] * bpb
            length = src_hi - src_lo
            # a note sustaining into the window keeps its audible part (the
            # overlap rule at window boundaries)
            source = [{**ev, "start": max(ev["start"], src_lo),
                       "duration": ev["start"] + ev["duration"] - max(ev["start"], src_lo)}
                      for ev in _events_in(cache[tid], src_lo, src_hi)]
            bucket = out.setdefault(tid, {"track": entry["track"], "role": entry["role"],
                                          "events": []})
            offset = dst_lo
            while offset < dst_hi - 1e-9:
                for ev in source:
                    start = offset + ev["start"] - src_lo
                    if start >= dst_hi - 1e-9:
                        continue
                    end = min(start + ev["duration"], offset + length, dst_hi)
                    bucket["events"].append({"pitch": ev["pitch"], "start": start,
                                             "duration": end - start,
                                             "velocity": ev.get("velocity")})
                offset += length
    for bucket in out.values():
        bucket["events"].sort(key=lambda ev: (ev["start"], ev["pitch"]))
    return out


# --- composed parts --------------------------------------------------------------------------

_SHAPE_BY_ROLE = {"thinning": "circling_8th", "build": "climb", "false peak": "climb",
                  "opening": "slow_bell", "bridge": "slow_bell", "real peak": "slow_bell",
                  "coda": "dotted_fall"}
_ROLE_PREFERENCE = ("thinning", "build", "opening", "bridge", "coda", "real peak", "false peak")
_MOTIF_BARS = 8
_PULSE_REGISTER = (36, 48)
_PEDAL_REGISTER = (48, 60)
_POLY_LOOP_BEATS = 11
_POLY_HITS = ((0.0, 37), (1.5, 42), (3.0, 42), (5.0, 37), (7.5, 42), (9.0, 42))
_REVOICE_RANGE = 5


def _plan_bar_worlds(hmap: dict, plan: dict) -> dict:
    """Plan bar -> world id: the world at the section's source bar (looped);
    a section carried by a pivot entry belongs to the pivot's world."""
    by_bar = {}
    for seg in hmap["segments"]:
        for bar in range(seg["bars"][0], seg["bars"][1] + 1):
            by_bar[bar] = seg["world"]
    out = {}
    for row in plan["sections"]:
        pivot = next((e for e in row["tracks"] if e.get("part") == "pivot"), None)
        source = pivot["from_bars"] if pivot else row["source_bars"]
        length = source[1] - source[0] + 1
        for bar in range(row["bars"][0], row["bars"][1] + 1):
            out[bar] = by_bar.get(source[0] + (bar - row["bars"][0]) % length)
    return out


def _nearest(pc: int, register: tuple, previous: int | None) -> int:
    lo, hi = register
    options = [p for p in range(lo, hi + 1) if p % 12 == pc] or [lo + (pc - lo) % 12]
    anchor = previous if previous is not None else (lo + hi) // 2
    return min(options, key=lambda p: (abs(p - anchor), p))


def _grid(notes_by_track: dict, skip: str | None = None) -> dict:
    grid: dict = {}
    for key, value in notes_by_track.items():
        events, role, name = _events_of(value)
        if role == "kit" or key == skip:
            continue
        for ev in events:
            for step in _steps(ev):
                grid.setdefault(step, []).append((name or str(key), int(ev["pitch"])))
    return grid


def _clash_with(grid: dict, pitch: int, note: dict) -> tuple | None:
    for step in _steps(note):
        for name, other in grid.get(step, ()):
            kind = _RUB_CLASSES.get(abs(pitch - other) % 12)
            if kind:
                return name, other, kind
    return None


def _humanise(notes: list[dict], rng: random.Random, bpb: float, floor: float) -> list[dict]:
    feel = ic.FEEL
    out = []
    for note in notes:
        start = note["start_beats"]
        downbeat = abs(start / bpb - round(start / bpb)) < 1e-9
        if rng.random() < feel["off_grid_share"]:
            shift = rng.uniform(*feel["off_grid_beats"]) * rng.choice((-1.0, 1.0))
            if start + shift >= floor:
                start += shift
        base = feel["downbeat_velocity"] if downbeat else feel["offbeat_velocity"]
        velocity = int(round(base + rng.gauss(0.0, feel["velocity_jitter_std"])))
        out.append({**note, "start_beats": _r(start, 4), "velocity": max(1, min(127, velocity)),
                    "downbeat": downbeat})
    for current, following in zip(out, out[1:]):
        end = current["start_beats"] + current["duration_beats"]
        if end > following["start_beats"]:
            current["duration_beats"] = following["start_beats"] - current["start_beats"]
    for note in out:
        note["duration_beats"] = _r(note["duration_beats"], 4)
    return [n for n in out if n["duration_beats"] > 0]


def _nearest_in(pitch: int, pcs: set) -> int | None:
    options = [p for p in range(pitch - 6, pitch + 7) if p % 12 in pcs]
    return min(options, key=lambda p: (abs(p - pitch), p)) if options else None


def _gate(part: dict, grid: dict, allowed: dict, dropped: list, *, bpb: float = 4.0,
          scales: dict | None = None) -> None:
    """With ``scales`` (strict_scale): first move each note whose pitch class
    lies outside its bar's world scale to the nearest scale tone
    (``rescaled``). Then keep each note that makes no semitone/tritone with
    the context; else re-voice it to the nearest allowed tone that does not
    clash, else drop it. A note's bar is its grid onset's (before humanise)."""
    kept, stats = [], {"dropped": 0, "revoiced": 0, "rescaled": 0}
    for note in part["notes"]:
        bar = int(note.get("grid_beats", note["start_beats"]) // bpb + 1e-9) + 1
        scale = (scales or {}).get(bar)
        if scale and note["pitch"] % 12 not in scale["pcs"]:
            to = _nearest_in(note["pitch"], scale["pcs"])
            stats["rescaled"] += 1
            dropped.append({"part_id": part["part_id"], "pitch": note["pitch"],
                            "name": pc_name(note["pitch"]), "start_beats": note["start_beats"],
                            "action": "rescaled", "to": to, "against": None,
                            "against_pitch": None, "interval": None,
                            "why": "%s is outside %s's scale (%s)"
                                   % (pc_name(note["pitch"], scale["centre"]), scale["world"],
                                      " ".join(scale["names"]))})
            note = {**note, "pitch": to}
        hit = _clash_with(grid, note["pitch"], _ev(note))
        if not hit:
            kept.append(note)
            continue
        pcs = allowed.get(bar, set())
        options = sorted((p for p in range(note["pitch"] - _REVOICE_RANGE,
                                           note["pitch"] + _REVOICE_RANGE + 1)
                          if p % 12 in pcs and p != note["pitch"]),
                         key=lambda p: (abs(p - note["pitch"]), p))
        fix = next((p for p in options if not _clash_with(grid, p, _ev(note))), None)
        row = {"part_id": part["part_id"], "pitch": note["pitch"], "name": pc_name(note["pitch"]),
               "start_beats": note["start_beats"], "against": hit[0],
               "against_pitch": hit[1], "interval": hit[2]}
        if fix is None:
            stats["dropped"] += 1
            dropped.append({**row, "action": "dropped"})
        else:
            stats["revoiced"] += 1
            dropped.append({**row, "action": "revoiced", "to": fix})
            kept.append({**note, "pitch": fix})
    part["notes"] = [{k: v for k, v in n.items() if k != "grid_beats"} for n in kept]
    part["clash"] = {"checked": True, **stats}


def _world_scale(world: dict, worlds: list[dict]) -> dict | None:
    """The tones a new part may use in a world: a modal world's core; a drone
    borrows the core of the modal world on its centre; else none is known."""
    source = world
    if world["kind"] == "drone":
        source = next((w for w in worlds if w["kind"] == "modal"
                       and w["centre"] == world["centre"]), None)
        if source is None:
            return None
    pcs = _core_pcs(source)
    why = ("the core of %s" % _world_label(source) if source is world else
           "the core of %s, the modal world on the drone's centre" % _world_label(source))
    return {"world": world["id"], "centre": world["centre"], "pcs": sorted(pcs),
            "names": [pc_name(pc, world["centre"]) for pc in sorted(pcs, key=lambda pc:
                                                                   (pc - world["centre"]) % 12)],
            "source": why}


_RESCALED_SHARE = 0.25


def _say_rescaled(part: dict, total: int) -> None:
    """Honest provenance: when a quarter or more of a part's notes were moved
    into the world's scale, the pitches are no longer the source's."""
    moved = part["clash"]["rescaled"]
    if not total or moved / total < _RESCALED_SHARE or not part.get("scale"):
        return
    whose = "his" if part["kind"] == "riff" else "the cell's"
    part["provenance"] += ("; %d of %d notes re-voiced to %s's scale (%s): the contour is %s, "
                           "the pitches are not all %s" % (moved, total, part["scale"]["world"],
                                                          " ".join(part["scale"]["names"]),
                                                          whose, whose))


def _ev(note: dict) -> dict:
    return {"start": note["start_beats"], "duration": note["duration_beats"]}


def _part(kind, world, root, plan_bars, notes, provenance, evidence, role="tonal", **extra):
    name = "SIDEKICK %s %s" % (kind.upper(), root or "")
    return {"part_id": "%s-%s-%d" % (kind, world or "x", plan_bars[0]), "kind": kind,
            "track": name.strip(), "new_track": True, "role": role, "world": world,
            "root": root, "plan_bars": list(plan_bars), "notes": notes,
            "provenance": provenance, "evidence": evidence, **extra}


def _motif_notes(shape: dict, centre: int, first_bar: int, bars: int, bpb: float) -> list[dict]:
    cell = ic.CELL_BY_ID[shape["cell"]]
    pcs = [(centre + ic.semitones(d)) % 12 for d in cell["degrees"]]
    notes, t, i, previous = [], (first_bar - 1) * bpb, 0, None
    end = (first_bar - 1 + bars) * bpb
    while t < end - 1e-9:
        for step in shape["rhythm"]:
            if t >= end - 1e-9:
                break
            pitch = _nearest(pcs[i % len(pcs)], shape["register"], previous)
            notes.append({"pitch": pitch, "start_beats": t,
                          "duration_beats": min(step, end - t) * 0.95})
            previous, t, i = pitch, t + step, i + 1
    return notes


def _motif_parts(ctx: dict) -> list[dict]:
    hmap, plan, worlds_by_bar = ctx["hmap"], ctx["plan"], ctx["worlds_by_bar"]
    parts = []
    held = [p["held_alone"] for p in hmap["pivots"] if p["held_alone"]]
    for world in hmap["worlds"]:
        if world["kind"] == "drone" and any(span in held for span in world["spans"]):
            ctx["skipped"].append({"kind": "motif", "world": world["id"],
                                   "why": "%s is the pivot held alone at %s before the bridge: "
                                          "a motif there would stop it being alone"
                                          % (world["centre_name"],
                                             ", ".join(_bars_text(s) for s in world["spans"]))})
            continue
        if world["id"] in ctx.get("scales", {}) and ctx["scales"][world["id"]] is None:
            ctx["skipped"].append({"kind": "motif", "world": world["id"],
                                   "why": "no scale heard beyond the %s drone (no modal world "
                                          "on %s): a motif would collapse to one pitch"
                                          % (world["centre_name"], world["centre_name"])})
            continue
        rows = [r for r in plan["sections"] if any(worlds_by_bar.get(b) == world["id"]
                                                   for b in range(r["bars"][0], r["bars"][1] + 1))]
        if not rows:
            continue
        row = min(rows, key=lambda r: (r["clashes"]["count"],
                                       _ROLE_PREFERENCE.index(r["role"])
                                       if r["role"] in _ROLE_PREFERENCE else 99, r["bars"][0]))
        bars = [b for b in range(row["bars"][0], row["bars"][1] + 1)
                if worlds_by_bar.get(b) == world["id"]]
        first = bars[0] if row["role"] != "opening" else max(bars[0], bars[-1] - _MOTIF_BARS + 1)
        length = min(_MOTIF_BARS, bars[-1] - first + 1)
        shape_name = _SHAPE_BY_ROLE.get(row["role"], "circling_8th")
        shape = ic.MOTIF_SHAPES[shape_name]
        cell = ic.CELL_BY_ID[shape["cell"]]
        notes = _motif_notes(shape, world["centre"], first, length, ctx["bpb"])
        parts.append(_part(
            "motif", world["id"], world["centre_name"], [first, first + length - 1], notes,
            "his cell %s %s (%s; %d tracks) as a %s (%s), rooted on %s — the world's centre "
            "at plan bars %d-%d" % ("-".join(cell["degrees"]), cell["gesture"], cell["source"],
                                    cell["tracks"], shape_name.replace("_", " "), shape["source"],
                                    world["centre_name"], first, first + length - 1),
            ["measured", "inferred"], shape=shape_name, cell=cell["id"]))
    return parts


def _riff_part(ctx: dict) -> dict | None:
    hmap, plan, snapshot = ctx["hmap"], ctx["plan"], ctx["snapshot"]
    modal = [w for w in hmap["worlds"] if w["kind"] == "modal"]
    target = next((r for r in plan["sections"] if r["role"] == "real peak"), None)
    if len(modal) < 2 or target is None:
        return None
    src_world, dst_world = modal[0], modal[-1]
    # his riff in its own mode: the first segment of the source world whose
    # main tones all lie in that world's core
    seg = next((s for s in hmap["segments"] if s["world"] == src_world["id"]
                and s["bars"][1] - s["bars"][0] + 1 >= _PHRASE_BARS
                and set(s["main"]) <= set(src_world["core"])), None)
    if seg is None:
        return None
    window = [seg["bars"][0], seg["bars"][0] + _PHRASE_BARS - 1]
    bpb = ctx["bpb"]
    lo, hi = (window[0] - 1) * bpb, window[1] * bpb
    notes = ctx["tonal"]
    best = max((tid for tid in notes), key=lambda tid: (
        len({ev["pitch"] % 12 for ev in _events_in(notes[tid]["events"], lo, hi)}),
        len(_events_in(notes[tid]["events"], lo, hi)), -ctx["order"].index(tid)))
    source = [ev for ev in notes[best]["events"] if lo <= ev["start"] < hi]
    step = (dst_world["centre"] - src_world["centre"]) % 12
    shift = step if step < 6 else step - 12
    if step == 6 and _median([ev["pitch"] for ev in source]) < _BASS_BELOW:
        shift = 6   # a tritone either way: a low riff goes up rather than into sub-bass
    out, t0 = [], (target["bars"][0] - 1) * bpb
    length = hi - lo
    while t0 < target["bars"][1] * bpb - 1e-9:
        for ev in source:
            start = t0 + ev["start"] - lo
            if start < target["bars"][1] * bpb - 1e-9:
                out.append({"pitch": ev["pitch"] + shift, "start_beats": start,
                            "duration_beats": min(ev["duration"], target["bars"][1] * bpb - start)})
        t0 += length
    return _part("riff", dst_world["id"], dst_world["centre_name"], target["bars"], out,
                 "his riff: %s at bars %s (%s), moved %+d semitones from %s into %s; looped over "
                 "the real peak" % (notes[best]["track"], _bars_text(window), "set_file", shift,
                                    _world_label(src_world, False), _world_label(dst_world, False)),
                 ["set_file", "inferred"], transpose=shift, source_track=notes[best]["track"],
                 source_bars=window)


def _pedal_part(ctx: dict) -> dict | None:
    bridge = next((r for r in ctx["plan"]["sections"] if r["role"] == "bridge"), None)
    if not bridge or not bridge.get("pivot"):
        return None
    pc = next(pc for pc in range(12) if pc_name(pc, 6) == bridge["pivot"]
              or pc_name(pc, 0) == bridge["pivot"])
    pitch = _nearest(pc, _PEDAL_REGISTER, None)
    bpb = ctx["bpb"]
    notes = [{"pitch": pitch, "start_beats": (bar - 1) * bpb, "duration_beats": bpb * 0.98}
             for bar in range(bridge["bars"][0], bridge["bars"][1] + 1)]
    world = ctx["worlds_by_bar"].get(bridge["bars"][0])
    return _part("pedal", world, bridge["pivot"], bridge["bars"], notes,
                 "the pivot %s (harmony map: the only tone both worlds share) held one bar at a "
                 "time across the bridge" % bridge["pivot"], ["inferred"])


def _pulse_part(ctx: dict) -> dict | None:
    row = next((r for r in ctx["plan"]["sections"] if r["role"] == "thinning"), None)
    if row is None:
        return None
    worlds = {w["id"]: w for w in ctx["hmap"]["worlds"]}
    bpb, notes, previous, roots = ctx["bpb"], [], None, {}
    for bar in range(row["bars"][0], row["bars"][1] + 1):
        world = worlds.get(ctx["worlds_by_bar"].get(bar))
        if not world:
            continue
        roots[bar] = world["centre_name"]
        pitch = _nearest(world["centre"], _PULSE_REGISTER, previous)
        previous = pitch
        for eighth in range(int(bpb * 2)):
            notes.append({"pitch": pitch, "start_beats": (bar - 1) * bpb + eighth * 0.5,
                          "duration_beats": 0.45})
    return _part("pulse", ctx["worlds_by_bar"].get(row["bars"][0]), None, row["bars"], notes,
                 "8th-note pulse on each bar's root, following the harmony map's centres",
                 ["inferred"], roots=_root_runs(roots))


def _root_runs(roots: dict) -> list[dict]:
    runs: list[dict] = []
    for bar in sorted(roots):
        if runs and runs[-1]["root"] == roots[bar] and runs[-1]["bars"][1] == bar - 1:
            runs[-1]["bars"][1] = bar
        else:
            runs.append({"root": roots[bar], "bars": [bar, bar]})
    return runs


def _polymeter_part(ctx: dict) -> dict | None:
    finding = next((s for s in ctx["plan"]["sections"] if s["role"] == "opening"), None)
    if finding is None:
        return None
    run = next((f["bars"] for f in ctx["diagnosis_findings"] if f["detector"] == "long_static_intro"),
               None)
    delayed = [t["at_bars"][0] for t in finding["tracks"] if t["at_bars"][0] > finding["bars"][0]
               and t["role"] == "kit"]
    if not run:
        return None
    last = (min(delayed) - 1) if delayed else run[1]
    bpb = ctx["bpb"]
    lo, hi = (run[0] - 1) * bpb, last * bpb
    notes, t = [], lo
    while t < hi - 1e-9:
        for offset, pitch in _POLY_HITS:
            if t + offset < hi - 1e-9:
                notes.append({"pitch": pitch, "start_beats": t + offset, "duration_beats": 0.25})
        t += _POLY_LOOP_BEATS
    move = ic.BORROWED["unequal_loops"]
    return _part("polymeter", None, None, [run[0], last], notes,
                 "an %d-beat loop against 4-beat bars — after %s's unequal loops (%s; %s; his "
                 "are bars, the beat scaling is ours): motion in the static run without new "
                 "harmony" % (_POLY_LOOP_BEATS, move["artist"], move["source"], move["quote"]),
                 ["set_file", "inferred"], role="kit", loop_beats=_POLY_LOOP_BEATS)


def compose_parts(snapshot: dict, hmap: dict, plan: dict, *, seed: int = 0,
                  kinds: tuple = ("motif", "riff", "pedal", "pulse", "polymeter"),
                  diagnosis: dict | None = None, strict_scale: bool = True) -> dict:
    """Optional new parts, each on a NEW SIDEKICK track (never his tracks):
    a motif per world from his idiom cells (root = the world's centre), his
    riff moved into the return's world, a pedal on the pivot across the
    bridge, an 8th pulse on the thinning section's roots, and an unequal
    (11-beat) percussion loop in the static intro run. Seeded humanise
    (idiom_cells.FEEL). Every pitched note is gated against the rendered plan
    and the parts accepted before it: clashing notes are re-voiced to the
    nearest core tone or dropped, and every change is listed. ``strict_scale``
    (default, the owner's "consonant by default"): passing tones outside the
    world's scale (_world_scale) are first moved to the nearest scale tone
    (``rescaled``); off keeps them when they do not clash."""
    bpb = _bpb(snapshot)
    rendered = render_plan(snapshot, plan)
    tonal = song_notes(snapshot)
    if diagnosis is None:
        diagnosis = diagnose(hmap, layer_arc(snapshot), {})
    ctx = {"snapshot": snapshot, "hmap": hmap, "plan": plan, "bpb": bpb, "tonal": tonal,
           "order": [t["id"] for t in snapshot.get("tracks") or []],
           "worlds_by_bar": _plan_bar_worlds(hmap, plan),
           "diagnosis_findings": diagnosis.get("findings") or [], "skipped": []}
    worlds = {w["id"]: w for w in hmap["worlds"]}
    world_scales = {w["id"]: _world_scale(w, hmap["worlds"]) for w in hmap["worlds"]}
    ctx["scales"] = world_scales if strict_scale else {}
    scales = ({bar: world_scales[wid] for bar, wid in ctx["worlds_by_bar"].items()
               if wid and world_scales[wid]} if strict_scale else {})
    allowed = {bar: _core_pcs(worlds[wid]) for bar, wid in ctx["worlds_by_bar"].items() if wid}
    allowed.update({bar: set(scale["pcs"]) for bar, scale in scales.items()})
    builders = {"motif": _motif_parts, "riff": lambda c: [_riff_part(c)],
                "pedal": lambda c: [_pedal_part(c)], "pulse": lambda c: [_pulse_part(c)],
                "polymeter": lambda c: [_polymeter_part(c)]}
    context = {tid: dict(value) for tid, value in rendered.items()}
    parts, dropped = [], []
    for kind in ("motif", "riff", "pedal", "pulse", "polymeter"):
        if kind not in kinds:
            continue
        for part in builders[kind](ctx):
            if part is None:
                continue
            rng = random.Random("%s:%s" % (seed, part["part_id"]))
            part["notes"] = _humanise([{**n, "grid_beats": n["start_beats"]}
                                       for n in part["notes"]], rng, bpb,
                                      (part["plan_bars"][0] - 1) * bpb)
            if part["role"] != "kit":
                total = len(part["notes"])
                _gate(part, _grid(context), allowed, dropped, bpb=bpb, scales=scales)
                if part.get("world") and world_scales.get(part["world"]):
                    part["scale"] = world_scales[part["world"]]
                _say_rescaled(part, total)
                check = clash_check({**context, part["part_id"]: {
                    "track": part["track"], "events": part["notes"]}},
                    {"bars": part["plan_bars"]}, bpb=bpb)
                remaining = sum(1 for r in check["rubs"] if part["part_id"] in r["track_keys"])
                unverified = _unverified(plan["sections"], part["plan_bars"])
                part["clash"]["checked"] = "partial" if unverified else "complete"
                if unverified:
                    # audio or unread MIDI sounds under it: no consonance claim
                    part["clash"].update(remaining=None, remaining_among_read=remaining,
                                         unverified=unverified)
                else:
                    part["clash"]["remaining"] = remaining
            else:
                part["clash"] = {"checked": False, "reason": "kit: no pitch", "remaining": 0}
                part["notes"] = [{k: v for k, v in n.items() if k != "grid_beats"}
                                 for n in part["notes"]]
            part["humanise"] = {"seed": seed, "feel": ic.FEEL["source"]}
            context[part["part_id"]] = {"track": part["track"], "role": part["role"],
                                        "events": [{"pitch": n["pitch"], "start": n["start_beats"],
                                                    "duration": n["duration_beats"]}
                                                   for n in part["notes"]]}
            parts.append(part)
    return {"parts": parts, "dropped_notes": dropped, "skipped": ctx["skipped"],
            "rule": "consonant by default: no new note may sound a semitone or tritone against "
                    "anything in the plan or an earlier part"}


# --- the proposal ----------------------------------------------------------------------------

def _existing_rubs(snapshot: dict) -> list[dict]:
    notes = song_notes(snapshot)
    out = []
    for sec in snapshot.get("sections") or []:
        report = clash_check(notes, {"bars": sec["bars"]}, bpb=_bpb(snapshot))
        pairs: dict = {}
        for rub in report["rubs"]:
            if rub["sustained"]:
                key = tuple(rub["names"])
                pairs.setdefault(key, {"names": list(key), "interval": rub["interval"],
                                       "tracks": set(), "sixteenths": 0})
                pairs[key]["tracks"].update(rub["tracks"])
                pairs[key]["sixteenths"] += rub["sixteenths"]
        top = sorted(pairs.values(), key=lambda r: (-r["sixteenths"], r["names"]))[:4]
        out.append({"bars": list(sec["bars"]), "count": report["count"],
                    "sustained_count": report["sustained_count"], "evidence": "set_file",
                    "top": [{**r, "tracks": sorted(r["tracks"])} for r in top]})
    return out


def _uncertain(snapshot, hmap, refs, involved, plan=None) -> list[str]:
    out = list(snapshot.get("uncertain") or []) + list(hmap.get("uncertain") or [])
    for world in hmap["worlds"]:
        if world.get("mode") and world.get("mode_margin") is not None \
                and world["mode_margin"] < 0.1:
            out.append("%s: %s (a guess from sounding time)" % (_world_label(world),
                                                                world["mode_note"]))
    for seg in hmap["segments"]:
        if seg["kind"] == "modal" and seg["centre_margin"] < 0.1:
            out.append("bars %s: centre %s leads by only %.0f%% of sounding time"
                       % (_bars_text(seg["bars"]), seg["centre_name"], 100 * seg["centre_margin"]))
    for row in refs.get("unsupported") or []:
        out.append("%s (%s): %s" % (row["need"].replace("_", " "), row["finding"], row["say"]))
    blind = {b.get("track_id") for b in (snapshot.get("source_integrity") or {}).get("blind") or []
             if isinstance(b, dict)}
    for tid in sorted(blind & set(involved)):
        out.append("track %s: not every note was read; its integrity digest is missing" % tid)
    for flag in (plan or {}).get("hazard_flags") or []:
        out.append("hazard track " + flag["say"])
    partial = {}
    for row in (plan or {}).get("sections") or []:
        for item in row.get("clashes", {}).get("unverified") or []:
            partial.setdefault(item["track"], item["why"])
    if partial:
        out.append("the consonance check covers only material that was read: %s — plan "
                   "sections and new parts over them claim no clash count"
                   % "; ".join("%s (%s)" % item for item in sorted(partial.items())))
    if not any("sidechain" in line for line in out):
        out.append("sidechains are unknown until the saved .als is read")
    return out


def _assumptions(snapshot) -> list[str]:
    out = list(snapshot.get("assumptions") or [])
    rule = snapshot.get("section_rule") or ""
    if rule:
        out.append("sections: " + rule)
    out += ["worlds group segments by tonal centre and kind (drone vs modal)",
            "meter %s at %s BPM; a bar is %s quarter beats" % (snapshot["set"]["meter"],
                                                                snapshot["set"]["tempo"],
                                                                snapshot["set"]["beats_per_bar"]),
            "audio tracks are counted as layers but not heard"]
    return out


def propose(snapshot: dict, decisions: dict | None = None, refs: list | tuple = (), *,
            intent_tags: list | tuple = (), target_duration_s: float | None = None, seed: int = 0,
            compose: bool = True, precedents: list | None = None,
            strict_scale: bool = True) -> dict:
    """The whole-song proposal: harmony map, layer arc, diagnosis questions,
    ranked typed references, an arrangement plan of copies of his clips and
    optional new SIDEKICK parts — pure, deterministic for a seed, never
    writes. ``decisions``: piece_context.current_decisions output (plus an
    optional ``intended`` list). ``refs``: see rank_references. ``strict_scale``
    (default on): composed passing tones outside a world's scale are
    re-voiced into it (compose_parts)."""
    decisions = dict(decisions or {})
    hmap, arc = harmony_map(snapshot), layer_arc(snapshot)
    diagnosis = diagnose(hmap, arc, decisions)
    intent = {"tags": list(intent_tags), "borrow": decisions.get("borrow") or [],
              "depart": decisions.get("depart") or []}
    references = rank_references(list(refs), diagnosis, intent,
                                 tempo=float(snapshot["set"]["tempo"]))
    plan = arrangement_plan(snapshot, hmap, arc, diagnosis, references, decisions,
                            target_duration_s=target_duration_s)
    composed = (compose_parts(snapshot, hmap, plan, seed=seed, diagnosis=diagnosis,
                              strict_scale=strict_scale)
                if compose else {"parts": [], "dropped_notes": [], "skipped": []})
    involved = sorted({t["track_id"] for s in plan["sections"] for t in s["tracks"]})
    # every track of his (and the groups, whose mute silences members): the
    # harmony map and the consonance gate read them all, so unmuting or
    # editing one the plan does not copy still matters
    theirs = {row.get("track_id") for row in snapshot.get("not_his_material") or []}
    watched = sorted(t["id"] for t in snapshot.get("tracks") or []
                     if t["id"] not in theirs or t.get("type") == "group")
    freshness_target = {
        "bars": [1, _song_bars(snapshot)], "track_ids": watched,
        # every track of the Set, his or not: a track added or removed since
        # (a new layer, a deleted one) makes the proposal about another Set
        "track_set": sorted(str(t["id"]) for t in snapshot.get("tracks") or []),
        "protected_track_ids": sorted(str(t) for t in decisions.get("protect_track_ids") or []),
        "decisions_revision": decisions.get("revision")}
    freshness = ps.proposal_freshness(snapshot, freshness_target)
    digests = (snapshot.get("source_integrity") or {}).get("digests") or {}
    inputs = {"seed": seed, "target_duration_s": target_duration_s,
              "intent_tags": sorted(intent_tags),
              "ref_ids": sorted(str(r.get("ref_id")) for r in refs),
              "compose": compose, "strict_scale": strict_scale}
    proposal = {
        "kind": "ref_develop_proposal", "proposal_id": None, "seed": seed, "inputs": inputs,
        "target_duration": {"requested_s": target_duration_s, "plan_bars": plan["total_bars"],
                            "plan_s": plan["duration_s"]},
        "direction": {"goal": decisions.get("goal"), "borrow": decisions.get("borrow") or [],
                      "preserve": decisions.get("preserve") or [],
                      "depart": decisions.get("depart") or [], "intent_tags": list(intent_tags),
                      "precedents": list(precedents or []), "evidence": "owner_confirmed"
                      if decisions.get("goal") or decisions.get("preserve") else "inferred"},
        "harmony_map": hmap, "layer_arc": arc, "diagnosis": diagnosis,
        "references": references, "plan": plan, "parts": composed["parts"],
        "dropped_notes": composed["dropped_notes"], "skipped_parts": composed["skipped"],
        "existing_rubs": _existing_rubs(snapshot),
        "uncertain": _uncertain(snapshot, hmap, references, involved, plan),
        "assumptions": _assumptions(snapshot),
        "freshness_digest": freshness["digest"], "freshness_missing": freshness["missing_tracks"],
        "freshness_target": freshness_target,
        # every watched track's content (copied or only heard by the harmony
        # map and the gate); a watched track with no digest was not read in
        # full, and nothing can vouch that it is unchanged
        "source_digests": {tid: digests[tid] for tid in watched if tid in digests},
        "source_blind": [tid for tid in watched if tid not in digests],
        "writes": "none",
    }
    proposal["proposal_id"] = proposal_id_of(proposal)
    return proposal


# what the proposal id is bound to: every input and everything reviewed that a
# stage writes (a proposal edited after review no longer matches its own id)
_IDENTITY_KEYS = ("inputs", "target_duration", "direction", "plan", "parts",
                  "freshness_digest", "freshness_target", "source_digests", "source_blind")


def proposal_id_of(proposal: dict) -> str:
    """The id a proposal's own content gives: ``rd-`` + a digest of its inputs
    (freshness digest over placement, tempo, meter and decisions; the content
    digest of every watched track; seed, target, tags, references, compose
    options) and of the reviewed plan and parts. Stable across a JSON round
    trip."""
    return "rd-" + _digest({key: proposal.get(key) for key in _IDENTITY_KEYS})[:16]


def proposal_validity(proposal: dict, snapshot: dict, decisions: dict | None) -> dict:
    """Does ``proposal`` still hold on ``snapshot`` (a fresh read of the Set it
    is about to be staged into) under ``decisions`` (piece_context.
    current_decisions, reloaded from the store)? Pure. ``problems`` lists each
    failed check as ``{"kind", "say", ...}``; kinds:

    - ``identity``: the proposal's content does not match its id
    - ``arrangement``: placements, routing, mutes, tempo or meter changed
      (proposal_freshness) or a watched track is gone
    - ``tracks``: a track was added to the Set or removed from it (the
      target's ``track_set``)
    - ``content``: notes/devices of a watched track changed (source digests)
    - ``blind``: a watched track's notes were not all read, now or when proposed
    - ``decisions``: the decisions revision or the protected tracks changed
    """
    problems = []
    if proposal_id_of(proposal) != proposal.get("proposal_id"):
        problems.append({"kind": "identity", "say": "the proposal's content does not match "
                         "its id (it was edited after it was made)"})
    target = proposal.get("freshness_target") or {}
    if not target or not proposal.get("freshness_digest"):
        problems.append({"kind": "identity", "say": "the proposal carries no freshness target"})
        return {"ok": False, "problems": problems}
    fresh = ps.proposal_freshness(snapshot, target)
    if fresh["digest"] != proposal["freshness_digest"] or fresh["missing_tracks"]:
        problems.append({"kind": "arrangement", "missing_tracks": fresh["missing_tracks"],
                         "say": "the arrangement changed (a clip moved, a track changed, a "
                                "mute, the tempo or the meter)"})
    if "track_set" in target:
        was, now = set(target["track_set"]), {str(t["id"]) for t in snapshot.get("tracks") or []}
        names = {str(t["id"]): t.get("name") for t in snapshot.get("tracks") or []}
        if was != now:
            problems.append({"kind": "tracks", "added": sorted(now - was),
                             "removed": sorted(was - now),
                             "say": "the Set's tracks changed (%s)" % "; ".join(
                                 ["added %s" % names.get(t, t) for t in sorted(now - was)]
                                 + ["removed track %s" % t for t in sorted(was - now)])})
    else:
        problems.append({"kind": "identity", "say": "the proposal does not record the Set's "
                                                    "tracks (made by an older proposer)"})
    watched = [str(t) for t in target.get("track_ids") or []]
    digests = (snapshot.get("source_integrity") or {}).get("digests") or {}
    was_blind = [tid for tid in watched if tid in set(proposal.get("source_blind") or [])
                 or tid not in (proposal.get("source_digests") or {})]
    now_blind = [tid for tid in watched if tid not in digests and tid not in
                 fresh["missing_tracks"]]
    blind = sorted(set(was_blind) | set(now_blind))
    if blind:
        problems.append({"kind": "blind", "track_ids": blind,
                         "say": "not every note was read on track%s %s, so nothing vouches "
                                "that they are unchanged" % ("" if len(blind) == 1 else "s",
                                                             ", ".join(blind))})
    changed = sorted(tid for tid in watched if tid not in blind and tid in digests
                     and digests[tid] != proposal["source_digests"].get(tid))
    if changed:
        problems.append({"kind": "content", "track_ids": changed,
                         "say": "notes changed on track%s %s" % (
                             "" if len(changed) == 1 else "s", ", ".join(changed))})
    decisions = decisions or {}
    now_revision = decisions.get("revision") or 0
    was_revision = target.get("decisions_revision") or 0
    now_protected = sorted({str(t) for t in decisions.get("protect_track_ids") or []})
    was_protected = sorted({str(t) for t in target.get("protected_track_ids") or []})
    if now_revision != was_revision or now_protected != was_protected:
        problems.append({"kind": "decisions", "revision": [was_revision, now_revision],
                         "protected_track_ids": [was_protected, now_protected],
                         "say": "your piece decisions changed (revision %s -> %s%s)" % (
                             was_revision, now_revision,
                             "; protected %s -> %s" % (was_protected or "none",
                                                       now_protected or "none")
                             if now_protected != was_protected else "")})
    return {"ok": not problems, "problems": problems}
