"""Parse an Ableton Live Set (.als) into a normalized JSON structure.

An .als file is gzip-compressed XML. This module decompresses it, walks the
LiveSet → Tracks tree, and extracts the symbolic data a composing assistant
cares about: tempo, per-track MIDI clips, and the notes inside them
(pitch / start-beat / duration / velocity), plus arrangement locators.

Stdlib only — no third-party deps — so Phase 0 runs on system Python.

Usage:
    python3 ingest/parse_als.py "/path/to/Project.als"
    python3 ingest/parse_als.py "/path/to/Project.als" --json out.json
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field


# --- low-level loading ------------------------------------------------------

def load_als_root(path: str) -> ET.Element:
    """Decompress a .als (gzip) and return the parsed XML root element.

    Some .als files are stored as plain (un-gzipped) XML; we fall back to that
    if the gzip magic bytes are absent.
    """
    with open(path, "rb") as fh:
        head = fh.read(2)
    opener = gzip.open if head == b"\x1f\x8b" else open
    with opener(path, "rb") as fh:
        return ET.parse(fh).getroot()


def _attr(el: ET.Element | None, name: str = "Value", default=None):
    """Safely read an attribute from an element that may be None."""
    if el is None:
        return default
    return el.get(name, default)


# --- musical extraction -----------------------------------------------------

@dataclass
class Note:
    pitch: int          # MIDI note number 0-127
    start: float        # beats from clip start
    duration: float     # beats
    velocity: int       # 1-127
    enabled: bool = True


@dataclass
class Clip:
    name: str
    track: str
    start: float | None     # arrangement position in beats (None for session clips)
    end: float | None
    note_count: int
    is_drum: bool = False    # clip lives on a drum/percussion track
    clip_source: str = "legacy"  # arrangement / session / take_lane / other_track / legacy
    is_sidekick: bool = False  # clip lives on a SIDEKICK track (sidekick output, not his playing)
    loop_start: float | None = None  # loop brace in clip-relative beats (None = unknown)
    loop_end: float | None = None
    loop_on: bool = True
    audible_start: float | None = None  # what he actually hears (None = whole clip)
    audible_end: float | None = None
    # the played sequence, in playback order, when it is not one range: a
    # short arrangement pass that wraps plays [S, LoopEnd) then
    # [LoopStart, ...). None = the single range above.
    audible_regions: list | None = None
    # Raw start marker S = LoopStart + StartRelative of a loop-ON clip, in
    # clip-relative beats: where one arrangement pass begins before it repeats
    # the brace. Independent of audible_* (which report the brace envelope once
    # a pass covers it). None = loop off, no usable brace, StartRelative outside
    # [0, brace length], or an old record without the field.
    start_marker: float | None = None
    # the clip's Activator (Disabled="true" = deactivated, Live does not play
    # it). Raw flag only: corpus ingestion keeps deactivated clips; the
    # finisher's ending seeds skip them.
    disabled: bool = False
    notes: list[Note] = field(default_factory=list)


@dataclass
class Project:
    path: str
    tempo: float | None
    track_count: int
    midi_track_names: list[str]
    audio_track_names: list[str]
    clip_count: int
    total_notes: int
    audio_clip_count: int = 0
    notes_skipped: int = 0
    skip_detail: dict[str, int] = field(default_factory=dict)
    clips: list[Clip] = field(default_factory=list)


def get_tempo(root: ET.Element) -> float | None:
    """The set tempo lives in a <Tempo><Manual Value=".."/></Tempo> block."""
    for tempo_el in root.iter("Tempo"):
        manual = tempo_el.find("Manual")
        if manual is not None and manual.get("Value") is not None:
            try:
                return float(manual.get("Value"))
            except ValueError:
                pass
    return None


def _track_name(track_el: ET.Element) -> str:
    """Effective (display) name of a track, with a sensible fallback."""
    eff = track_el.find("Name/EffectiveName")
    if eff is not None and eff.get("Value"):
        return eff.get("Value")
    user = track_el.find("Name/UserName")
    if user is not None and user.get("Value"):
        return user.get("Value")
    return "(unnamed)"


# Substrings (lowercased track name) that mark a drum/percussion track. Mix of
# English and Portuguese, since Ricardo's projects use both.
DRUM_KEYWORDS = (
    "drum", "perc", "bateria", "kick", "bumbo", "snare", "caixa", "hihat",
    "hi-hat", "hat", "clap", "cymbal", "prato", "808", "909", "707", "drumkit",
    "conga", "bongo", "shaker", "tambor", "rimshot", "ride", "crash",
)

MELODIC_ROLE_WORDS = (
    "lead", "bass", "pad", "keys", "chord", "melo", "melody", "vox", "vocal",
    "synth", "guitar", "piano", "riff",
)

_NOT_HIS_CACHE: set | None = None
_DRUM_OVERRIDES_CACHE: dict | None = None
_DRUM_OVERRIDES_TRIED = False


def blocked_projects() -> set:
    """Projects Ricardo confirmed are packs/demos/presets — NOT his playing.

    Read from store/not_his_projects.json (tracked; his judgment, 2026-07-03).
    Excluded from ALL learning and retrieval; raw materialization stays complete
    so the undo is: edit the file, rebuild artifacts."""
    global _NOT_HIS_CACHE
    if _NOT_HIS_CACHE is None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "store", "not_his_projects.json")
        try:
            with open(path) as fh:
                _NOT_HIS_CACHE = {n.lower() for n in json.load(fh)["blocked_projects"]}
        except (OSError, KeyError, ValueError):
            _NOT_HIS_CACHE = set()
    return _NOT_HIS_CACHE


def is_blocked_project(name: str | None) -> bool:
    if not name:
        return False
    stem = str(name).lower()
    if stem.endswith(".als"):
        stem = stem[:-4]
    return stem in blocked_projects()


# Track-name prefix for sidekick output tracks (SIDEKICK JAM/BOOTH/ARR…):
# note-producing gears write there, so those clips are the assistant's, not his.
SIDEKICK_TRACK_PREFIX = "SIDEKICK"


def is_sidekick_track(name: str | None) -> bool:
    """True if the track holds sidekick output rather than his playing."""
    return bool(name) and str(name).strip().upper().startswith(SIDEKICK_TRACK_PREFIX)


MAIN_CLIP_SOURCES = {"arrangement", "session", "legacy"}
TIMELINE_CLIP_SOURCES = {"arrangement"}
MELODIC_CLIP_SOURCES = {"arrangement", "session", "legacy", "take_lane"}


def clip_source_of(clip: dict | Clip) -> str:
    """Return a stable clip-source label, defaulting old JSON to legacy."""
    if isinstance(clip, Clip):
        return clip.clip_source or "legacy"
    return clip.get("clip_source") or "legacy"


def is_main_clip_source(source: str | None) -> bool:
    """True for clips that should train the main style corpus."""
    return (source or "legacy") in MAIN_CLIP_SOURCES


def is_timeline_clip_source(source: str | None) -> bool:
    """True for clips that should count as arrangement-timeline material."""
    return (source or "legacy") in TIMELINE_CLIP_SOURCES


def is_melodic_clip_source(source: str | None) -> bool:
    """True for clips that feed MELODIC/phrase/groove retrieval — INCLUDES take_lane, since
    alternate takes are real playing and may hold his most alive lines. Scoped to the phrase
    retrieval index ONLY; structure/style/harmony profiles still use is_main_clip_source.
    Only other_track is excluded."""
    return (source or "legacy") in MELODIC_CLIP_SOURCES


_MIDI_CLIP_SOURCE_PATHS = (
    ("arrangement", (
        ".//DeviceChain/MainSequencer/ClipTimeable/ArrangerAutomation/Events/MidiClip",
    )),
    ("session", (
        ".//DeviceChain/MainSequencer/ClipSlotList/ClipSlot/ClipSlot/Value/MidiClip",
        ".//DeviceChain/MainSequencer/ClipSlotList/ClipSlot/Value/MidiClip",
    )),
    ("take_lane", (
        ".//TakeLanes/TakeLanes/TakeLane/ClipAutomation/Events/MidiClip",
    )),
)


def _store_path(filename: str) -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "store", filename)


def _drum_track_overrides() -> dict:
    """Lazy-load optional manual drum/non-drum overrides (case-insensitive keys)."""
    global _DRUM_OVERRIDES_CACHE, _DRUM_OVERRIDES_TRIED
    if _DRUM_OVERRIDES_TRIED:
        return _DRUM_OVERRIDES_CACHE or {}
    _DRUM_OVERRIDES_TRIED = True
    try:
        with open(_store_path("drum_track_overrides.json")) as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            _DRUM_OVERRIDES_CACHE = {str(k).lower(): bool(v) for k, v in raw.items()}
        else:
            _DRUM_OVERRIDES_CACHE = {}
    except (OSError, ValueError, TypeError):
        _DRUM_OVERRIDES_CACHE = {}
    return _DRUM_OVERRIDES_CACHE


def _drum_keyword_matches(low: str, keyword: str) -> bool:
    if not re.search(r"\b" + re.escape(keyword) + r"s?\b", low):
        return False
    if any(c.isdigit() for c in keyword):
        if any(re.search(r"\b" + re.escape(w) + r"\b", low) for w in MELODIC_ROLE_WORDS):
            return False
    return True


def _is_drum_track(track_el: ET.Element, name: str) -> bool:
    """True if the track is percussion.

    Primary signal: a <DrumGroupDevice> (Drum Rack) anywhere in its chain —
    structural and reliable. Fallback: drum keywords in the track name, for
    drum sounds loaded outside a Drum Rack.
    """
    if next(track_el.iter("DrumGroupDevice"), None) is not None:
        return True
    low = name.lower()
    overrides = _drum_track_overrides()
    if low in overrides:
        return overrides[low]
    return any(_drum_keyword_matches(low, k) for k in DRUM_KEYWORDS)


def _iter_midi_clips_by_source(track_el: ET.Element):
    """Yield MIDI clips under a track with their Ableton source bucket.

    A broad ``track_el.iter("MidiClip")`` correctly avoids GroovePool clips but
    also blends arrangement, Session, and Take Lane material. These buckets keep
    the raw material while letting downstream analysis exclude Take Lanes from
    the main learned corpus.
    """
    seen: set[int] = set()
    for source, paths in _MIDI_CLIP_SOURCE_PATHS:
        for path in paths:
            for clip_el in track_el.findall(path):
                marker = id(clip_el)
                if marker in seen:
                    continue
                seen.add(marker)
                yield source, clip_el

    for clip_el in track_el.iter("MidiClip"):
        marker = id(clip_el)
        if marker in seen:
            continue
        seen.add(marker)
        yield "other_track", clip_el


def parse_notes(clip_el: ET.Element) -> tuple[list[Note], dict[str, int]]:
    """Notes are grouped by pitch under KeyTracks/KeyTrack.

    Each <KeyTrack> has a <MidiKey Value="<pitch>"/> and a list of
    <MidiNoteEvent Time=".." Duration=".." Velocity=".." IsEnabled=".."/>.
    Returns (notes, skip_detail) where skip_detail is a counter of specific
    skip reasons.
    """
    notes: list[Note] = []
    skip_detail: dict[str, int] = {}
    for key_track in clip_el.iter("KeyTrack"):
        midi_key = key_track.find("MidiKey")
        events = list(key_track.iter("MidiNoteEvent"))
        if midi_key is None:
            if events:
                skip_detail["missing_pitch"] = (
                    skip_detail.get("missing_pitch", 0) + len(events)
                )
            continue
        try:
            pitch = int(midi_key.get("Value"))
        except (TypeError, ValueError):
            if events:
                skip_detail["malformed_pitch"] = (
                    skip_detail.get("malformed_pitch", 0) + len(events)
                )
            continue
        for ev in events:
            try:
                duration = float(ev.get("Duration", 0.0))
            except (TypeError, ValueError):
                skip_detail["non_positive_duration"] = skip_detail.get("non_positive_duration", 0) + 1
                continue
            if not math.isfinite(duration) or duration <= 0.0 or round(duration, 4) <= 0.0:
                skip_detail["non_positive_duration"] = skip_detail.get("non_positive_duration", 0) + 1
                continue
            try:
                velocity = int(float(ev.get("Velocity", 100)))
            except (TypeError, ValueError):
                skip_detail["bad_velocity"] = skip_detail.get("bad_velocity", 0) + 1
                continue
            try:
                notes.append(
                    Note(
                        pitch=pitch,
                        start=float(ev.get("Time", 0.0)),
                        duration=duration,
                        velocity=velocity,
                        enabled=ev.get("IsEnabled", "true") == "true",
                    )
                )
            except (TypeError, ValueError):
                skip_detail["unparsable_note"] = skip_detail.get("unparsable_note", 0) + 1
                continue
    notes.sort(key=lambda n: (n.start, n.pitch))
    return notes, skip_detail


def _parse_loop(clip_el: ET.Element, clip_source: str = "session") -> dict:
    """Read the clip's Loop brace into clip-relative beat bounds.

    Note Times are stored relative to the clip start. With the loop ON the
    audible region is the brace — for arrangement clips clamped to one pass
    of the timeline length (the clip plays loop repeats, but the index takes
    one pass of the brace). With the loop OFF, LoopStart/LoopEnd hold the
    start/end markers (in real files, e.g. demo.als ls=476 le=508),
    so audible = [LoopStart, min(LoopEnd, LoopStart + timeline)). Missing or
    degenerate markers mean the whole clip (None bounds); a bare OutMarker
    is only a legacy fallback when no start/end markers exist.
    """
    loop_el = clip_el.find("Loop")
    if loop_el is None:
        return {"loop_start": None, "loop_end": None, "loop_on": True,
                "audible_start": None, "audible_end": None,
                "audible_regions": None, "start_marker": None}
    loop_on = _attr(loop_el.find("LoopOn"), "Value", "true") == "true"
    loop_start = _to_float(_attr(loop_el.find("LoopStart")))
    loop_end = _to_float(_attr(loop_el.find("LoopEnd")))
    out_marker = _to_float(_attr(loop_el.find("OutMarker")))
    cur_start = _to_float(_attr(clip_el.find("CurrentStart")))
    cur_end = _to_float(_attr(clip_el.find("CurrentEnd")))
    timeline = (cur_end - cur_start) if cur_start is not None \
        and cur_end is not None and cur_end > cur_start else None
    markers = loop_start is not None and loop_end is not None \
        and loop_end > loop_start
    start_marker = None
    if loop_on and markers:
        # A missing StartRelative element reads as 0 (as the audible logic
        # below does); 0 and exactly one brace both mean the marker sits on
        # LoopStart; anything outside [0, L] is not a phase we can trust.
        relative = _to_float(_attr(loop_el.find("StartRelative")))
        relative = 0.0 if relative is None else relative
        brace = loop_end - loop_start
        if 0.0 < relative < brace:
            start_marker = loop_start + relative
        elif relative == 0.0 or relative == brace:
            start_marker = loop_start
    regions = None
    if loop_on and markers:
        start, end = loop_start, loop_end
        if clip_source != "session" and timeline is not None:
            # Arrangement and take-lane clips play one timeline-length pass
            # from the start marker S = LoopStart + StartRelative:
            #   - pass >= brace length: every beat of the brace plays;
            #   - S + pass <= LoopEnd: just [S, S + pass) (exact end = no wrap);
            #   - otherwise it wraps: [S, LoopEnd) then [LoopStart,
            #     LoopStart + (S + pass - LoopEnd)) — two slices, never the
            #     unheard middle of the brace.
            rel = _to_float(_attr(loop_el.find("StartRelative"))) or 0.0
            if 0.0 < rel < loop_end - loop_start:
                start = loop_start + rel
            if timeline >= loop_end - loop_start:
                start, end = loop_start, loop_end
            elif start + timeline <= loop_end:
                end = start + timeline
            else:
                head_end = loop_start + (start + timeline - loop_end)
                regions = [[start, loop_end], [loop_start, head_end]]
                start, end = loop_start, loop_end   # envelope; regions rule
        audible = (start, end) if end > start else (None, None)
    elif not loop_on and markers and timeline is not None:
        audible = (loop_start, min(loop_end, loop_start + timeline))
    elif not loop_on and out_marker is not None and out_marker > 0:
        audible = (0.0, out_marker)
    else:
        audible = (None, None)
    return {"loop_start": loop_start, "loop_end": loop_end, "loop_on": loop_on,
            "audible_start": audible[0], "audible_end": audible[1],
            "audible_regions": regions, "start_marker": start_marker}


def parse_als(path: str) -> Project:
    root = load_als_root(path)
    tempo = get_tempo(root)

    track_tags = {"MidiTrack", "AudioTrack", "GroupTrack", "ReturnTrack"}
    midi_names: list[str] = []
    audio_names: list[str] = []
    clips: list[Clip] = []
    audio_clip_count = 0
    track_count = 0
    notes_skipped = 0
    skip_detail: dict[str, int] = {}

    def _parse_track(track_el: ET.Element) -> None:
        nonlocal track_count, audio_clip_count, notes_skipped, skip_detail
        if track_el.tag == "GroupTrack":
            nested = track_el.find("Tracks")
            if nested is not None:
                for child in nested:
                    if child.tag in track_tags:
                        _parse_track(child)
            return
        if track_el.tag not in track_tags:
            return
        track_count += 1
        name = _track_name(track_el)
        is_drum = _is_drum_track(track_el, name)
        sidekick = is_sidekick_track(name)
        if track_el.tag == "MidiTrack":
            midi_names.append(name)
        elif track_el.tag == "AudioTrack":
            audio_names.append(name)

        # Count audio clips on this track. Iterating the track subtree keeps us
        # clear of GroovePool MidiClips (which live under LiveSet, not a track).
        audio_clip_count += sum(1 for _ in track_el.iter("AudioClip"))

        # MidiClip appears in Arrangement, Session, and Take Lanes. Tag source
        # explicitly so the main corpus can exclude take-lane alternates while
        # preserving the raw extraction.
        for clip_source, clip_el in _iter_midi_clips_by_source(track_el):
            notes, clip_skip_detail = parse_notes(clip_el)
            notes_skipped += sum(clip_skip_detail.values())
            for reason, count in clip_skip_detail.items():
                skip_detail[reason] = skip_detail.get(reason, 0) + count
            loop = _parse_loop(clip_el, clip_source)
            clips.append(
                Clip(
                    name=_attr(clip_el.find("Name")) or "(unnamed clip)",
                    track=name,
                    start=_to_float(_attr(clip_el.find("CurrentStart"))),
                    end=_to_float(_attr(clip_el.find("CurrentEnd"))),
                    note_count=len(notes),
                    is_drum=is_drum,
                    clip_source=clip_source,
                    is_sidekick=sidekick,
                    disabled=_attr(clip_el.find("Disabled")) == "true",
                    notes=notes,
                    **loop,
                )
            )

    tracks_el = root.find(".//LiveSet/Tracks")
    track_iter = list(tracks_el) if tracks_el is not None else []
    for track_el in track_iter:
        if track_el.tag in track_tags:
            _parse_track(track_el)

    return Project(
        path=path,
        tempo=tempo,
        track_count=track_count,
        midi_track_names=midi_names,
        audio_track_names=audio_names,
        clip_count=len(clips),
        total_notes=sum(c.note_count for c in clips),
        audio_clip_count=audio_clip_count,
        notes_skipped=notes_skipped,
        skip_detail=skip_detail,
        clips=clips,
    )


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# --- serialization & CLI ----------------------------------------------------

def to_dict(project: Project) -> dict:
    d = asdict(project)
    # Ensure skip_detail is always a dict, even if empty
    d["skip_detail"] = project.skip_detail or {}
    return d


def from_dict(data: dict) -> Project:
    """Rebuild a Project (with Note/Clip objects) from materialized JSON."""
    clips = [
        Clip(
            name=c["name"], track=c["track"], start=c["start"], end=c["end"],
            note_count=c["note_count"], is_drum=c.get("is_drum", False),
            clip_source=c.get("clip_source", "legacy"),
            is_sidekick=c.get("is_sidekick", False),
            loop_start=c.get("loop_start"), loop_end=c.get("loop_end"),
            loop_on=c.get("loop_on", True),
            audible_start=c.get("audible_start"), audible_end=c.get("audible_end"),
            audible_regions=c.get("audible_regions"),
            start_marker=c.get("start_marker"),
            disabled=c.get("disabled", False),
            notes=[Note(**n) for n in c["notes"]],
        )
        for c in data["clips"]
    ]
    return Project(
        path=data["path"], tempo=data["tempo"], track_count=data["track_count"],
        midi_track_names=data["midi_track_names"], audio_track_names=data["audio_track_names"],
        clip_count=data["clip_count"], total_notes=data["total_notes"],
        audio_clip_count=data.get("audio_clip_count", 0),
        notes_skipped=data.get("notes_skipped", 0),
        skip_detail=data.get("skip_detail") or {},
        clips=clips,
    )


def _print_summary(p: Project) -> None:
    print(f"Project : {p.path}")
    print(f"Tempo   : {p.tempo} BPM")
    print(f"Tracks  : {p.track_count}  "
          f"({len(p.midi_track_names)} MIDI, {len(p.audio_track_names)} audio)")
    print(f"Clips   : {p.clip_count} MIDI clips, {p.total_notes} notes total")
    print()
    print("MIDI tracks:", ", ".join(p.midi_track_names) or "(none)")
    print("Audio tracks:", ", ".join(p.audio_track_names) or "(none)")
    print()
    for c in p.clips[:12]:
        pitches = [n.pitch for n in c.notes if n.enabled]
        rng = f"{min(pitches)}-{max(pitches)}" if pitches else "-"
        print(f"  [{c.track}] '{c.name}' ({c.clip_source}): "
              f"{c.note_count} notes, pitch range {rng}")
    if p.clip_count > 12:
        print(f"  ... and {p.clip_count - 12} more clips")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Parse an Ableton .als into normalized JSON.")
    ap.add_argument("als", help="path to the .als file")
    ap.add_argument("--json", help="write full normalized JSON to this path")
    args = ap.parse_args(argv)

    try:
        project = parse_als(args.als)
    except Exception as exc:  # noqa: BLE001 — surface any parse failure clearly
        print(f"ERROR parsing {args.als}: {exc}", file=sys.stderr)
        return 1

    _print_summary(project)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(to_dict(project), fh, indent=2)
        print(f"\nWrote JSON → {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
