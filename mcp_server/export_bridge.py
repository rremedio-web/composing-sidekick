"""Producer Pal export bridge — MIDI export and Ableton handoff.

Pure code motion from core.py (SLICE A). No logic changes.
"""

from __future__ import annotations

import math
import os
from typing import Any

from mcp_server import core_shared
from mcp_server import pp_session
from mcp_server.config import BEATS_PER_BAR
from mcp_server.pp_codec import (
    normalize_notes_for_pp as _normalize_notes_for_pp,
    notes_to_pp_string as _notes_to_pp_string,
    pp_notes_to_notes,
)

# Constants from core.py that are only used by the moved functions
MAX_PP_DURATION_DENOMINATOR = 100_000
MAX_PP_DURATION_ERROR_BEATS = 1e-6


# Where export_midi may write. store/exports is always allowed; the operator
# can add directories with SIDEKICK_EXPORT_DIRS (os.pathsep-separated). The
# allowlist is configuration, never a tool argument: the model cannot widen it.
EXPORT_DIRS_ENV = "SIDEKICK_EXPORT_DIRS"
MIDI_SUFFIXES = (".mid", ".midi")


def _allowed_export_roots() -> list[str]:
    """store/exports plus SIDEKICK_EXPORT_DIRS, each resolved with realpath."""
    roots = [core_shared.EXPORTS_DIR]
    extra = os.environ.get(EXPORT_DIRS_ENV, "")
    roots.extend(p.strip() for p in extra.split(os.pathsep) if p.strip())
    return [os.path.realpath(os.path.expanduser(r)) for r in roots]


def _resolve_export_path(path: str) -> str:
    """Resolve an export path and refuse anything outside the allowlist.

    Relative paths land under store/exports (a leading ``store/exports/`` is
    accepted). Both the target and every allowed root are resolved with
    realpath, so a symlink or ``..`` cannot step outside. The name must end in
    .mid or .midi, and an existing file is only overwritten if it already is a
    Standard MIDI file (starts with ``MThd``).
    """
    if not path or not str(path).strip():
        raise ValueError("path is required")
    expanded = os.path.expanduser(str(path).strip())
    if os.path.isabs(expanded):
        candidate = expanded
    else:
        normal = os.path.normpath(expanded)
        parts = normal.split(os.sep)
        if len(parts) >= 2 and parts[0] == "store" and parts[1] == "exports":
            candidate = os.path.join(core_shared.ROOT, normal)
        else:
            candidate = os.path.join(core_shared.EXPORTS_DIR, normal)
    resolved = os.path.realpath(candidate)

    if not resolved.lower().endswith(MIDI_SUFFIXES):
        raise ValueError("export_midi only writes .mid / .midi files")

    def _inside(root: str) -> bool:
        try:
            return os.path.commonpath([root, resolved]) == root and resolved != root
        except ValueError:
            return False

    if not any(_inside(root) for root in _allowed_export_roots()):
        raise ValueError(
            "export_midi paths must stay under store/exports (or a directory "
            "listed in %s, set by the operator)" % EXPORT_DIRS_ENV
        )
    if os.path.lexists(resolved):
        if not os.path.isfile(resolved):
            raise ValueError("export_midi will not replace a non-file at the target path")
        with open(resolved, "rb") as fh:
            if fh.read(4) != b"MThd":
                raise ValueError(
                    "export_midi will not overwrite an existing file that is not "
                    "a Standard MIDI file")
    return resolved


def export_midi(notes: list[dict], path: str, tempo: float = 120.0) -> dict:
    """Write notes to a Standard MIDI file using mido."""
    import mido

    nd = core_shared._note_dicts(notes)
    if not nd:
        raise ValueError("notes list is empty")
    out_path = _resolve_export_path(path)

    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(tempo)))

    events: list[tuple[float, dict]] = []
    for n in nd:
        start = n["start"]
        end = start + max(n["duration"], 0.01)
        events.append((start, {"type": "on", "pitch": n["pitch"], "vel": n["velocity"]}))
        events.append((end, {"type": "off", "pitch": n["pitch"], "vel": 0}))
    events.sort(key=lambda e: (e[0], 0 if e[1]["type"] == "off" else 1))

    last_beat = 0.0
    for beat, ev in events:
        delta_beats = max(0.0, beat - last_beat)
        delta_ticks = int(round(delta_beats * mid.ticks_per_beat))
        if ev["type"] == "on":
            track.append(mido.Message("note_on", note=ev["pitch"], velocity=ev["vel"],
                                      time=delta_ticks))
        else:
            track.append(mido.Message("note_off", note=ev["pitch"], velocity=0,
                                      time=delta_ticks))
        last_beat = beat

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    mid.save(out_path)
    return {
        "path": out_path,
        "n_notes": len(nd),
        "tempo": tempo,
        "pitches": sorted({n["pitch"] for n in nd}),
    }


def send_to_ableton(
    notes: list[dict],
    track: int | str | None = None,
    clip_name: str | None = None,
    tempo: float = 120.0,
    bar_length: int | None = None,
    arrangement_start: str | None = None,
    session_scene: int = 0,
    addressing: str = pp_session.ADDRESSING_22,
) -> dict:
    """Build Producer Pal tool-call payload(s) for Ableton write-back (no socket).

    Returns the exact MCP arguments Claude should pass to Producer Pal after
  connecting. Does not open Ableton or call Producer Pal itself.
    ``addressing`` is the connected build: its generation ("2.2" / "2.3" /
    "2.4") or the version ppal-connect reports ("2.2.0-rc1", "2.3.0",
    "2.4.0"). An arrangement clip is 't<i>' + arrangementStart on 2.2 and
    't<i>[<bar|beat>]' on 2.3 and 2.4.

    Producer Pal API targeted (https://producer-pal.org/features):
      - ppal-connect (prerequisite)
      - ppal-update-live-set (optional tempo)
      - ppal-create-clip (session slot or arrangement clip + notes string)

    Note format: ``v{vel} n/{denom} {PitchName} {bar|beat}`` per
    https://producer-pal.org/features — pitches as names (C4), times as
    1-indexed bar|beat, durations as absolute note values (n/4, n/8, …).
    """
    nd = _normalize_notes_for_pp(core_shared._note_dicts(notes))
    if not nd:
        raise ValueError("notes list is empty")

    notes_str, translated = _notes_to_pp_string(nd)

    if bar_length is not None:
        clip_bars = max(1, int(bar_length))
    else:
        max_end = max(n["start"] + n["duration"] for n in nd)
        clip_bars = max(1, int(math.ceil(max_end / BEATS_PER_BAR)))

    # Producer Pal length format is "Nbar" (e.g. "8bar") — NOT "N:0" (bars:beats), which
    # the live API rejects ("Invalid duration format"). Confirmed against ppal-create-clip.
    clip_length = f"{clip_bars}bar"

    track_index: int | None = None
    if track is not None:
        track_index = track if isinstance(track, int) else int(track)

    use_arrangement = arrangement_start is not None
    if use_arrangement and track_index is None:
        raise ValueError("arrangement_start requires track (trackIndex)")

    create_args: dict[str, Any] = {
        "length": clip_length,
        "looping": True,
        "notes": notes_str,
    }
    if clip_name:
        create_args["name"] = clip_name

    requests = pp_session.PPRequests(addressing)
    if use_arrangement:
        placement = "arrangement"
        create_args.update(requests.create_clip(
            track=track_index, arrangement_start=arrangement_start))
    else:
        create_args["start"] = "1|1"
        placement = "session"
        scene_index = max(0, int(session_scene))
        if track_index is not None:
            create_args["path"] = pp_session.clip_locator(
                track=track_index, scene=scene_index)["path"]
        else:
            placement = "session_selected_track"
            create_args["path"] = pp_session.clip_locator(
                track=0, scene=scene_index)["path"]

    tool_calls: list[dict[str, Any]] = []
    if tempo and tempo > 0:
        tool_calls.append({
            "tool": "ppal-update-live-set",
            "arguments": {"tempo": tempo},
        })
    # Guard: Producer Pal 2.4 REPLACES the clip in an occupied session slot
    # (2.4.0/v24/v24_create_session_clip_replaces) and a clip placed over an
    # arrangement clip replaces it silently (v24_duplicate_over_existing). The
    # target is read first; the create runs only on the proven-empty shape
    # (pp_session.slot_read_verdict) / a span no listed clip overlaps.
    if use_arrangement:
        tool_calls.append({
            "tool": "ppal-read-track",
            "arguments": requests.read_track(track_index, include=["arrangement-clips"]),
            "proceed_only_if": "span_free",
            "guard": ("proceed with the next ppal-create-clip ONLY if no listed "
                      "arrangement clip overlaps %s for %s; otherwise stop — it would "
                      "be replaced" % (arrangement_start, clip_length)),
        })
    else:
        tool_calls.append({
            "tool": "ppal-read-clip",
            "arguments": {"path": create_args["path"], "include": []},
            "proceed_only_if": "empty_slot",
            "guard": ("proceed with the next ppal-create-clip ONLY if this read proves "
                      "%s empty — the error 'no clip at %s' or a record with id null at "
                      "that path; a clip, any other error or anything unclear means "
                      "stop — Producer Pal 2.4 would replace the clip there"
                      % (create_args["path"], create_args["path"])),
        })
    tool_calls.append({
        "tool": "ppal-create-clip",
        "arguments": create_args,
    })

    assumptions = [
        "ASSUMPTION: Clip meter is 4/4 (matches our start_beats = quarter notes).",
        "ASSUMPTION: ppal-connect must succeed before other tool calls.",
    ]
    if use_arrangement:
        assumptions.append(
            f"ASSUMPTION: Arrangement clip on track {track_index} (path "
            f"{create_args['path']!r}) at {arrangement_start!r}, length={clip_length!r}; "
            f"Producer Pal {requests.addressing} argument forms."
        )
    else:
        assumptions.extend([
            f"ASSUMPTION: Session clip at scene index {int(session_scene)} (slot trackIndex/{int(session_scene)}).",
            f"ASSUMPTION: When track is omitted, slot 0/{int(session_scene)} is used; "
            "Producer Pal may require an existing MIDI track at that index.",
        ])
        if track_index is None:
            assumptions.append(
                "ASSUMPTION: With track=None, clip lands on track 0 — pass track=<index> to target another."
            )

    instruction = (
        "1) Ensure Producer Pal M4L device is loaded and Ableton is running. "
        "2) Call Producer Pal tool ppal-connect (no args). "
        "3) Apply each entry in tool_calls in order (tempo, the guard read, then "
        "create-clip). The guard read decides: run ppal-create-clip only if it proves "
        "the target slot empty (or the arrangement span free) — on Producer Pal 2.4 a "
        "create into an occupied slot replaces the clip there; otherwise stop and ask. "
        "4) notes in ppal-create-clip use Producer Pal bar|beat notation — do not rewrite."
    )

    return {
        "instruction": instruction,
        "producer_pal_docs": "https://producer-pal.org/features",
        "prerequisite": {"tool": "ppal-connect", "arguments": {}},
        "tool_calls": tool_calls,
        "placement": placement,
        "note_count": len(nd),
        "translated_notes": translated,
        "assumptions": assumptions,
        "tempo": tempo,
        "clip_length": clip_length,
    }
