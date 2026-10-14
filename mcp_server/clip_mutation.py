"""Verified MIDI creation/replacement and one-bar Sidekick clip surgery.

update-clip ``notes`` is ADDITIVE (Producer Pal 2.4 captured it: 2 notes + 1
-> noteCount 3, v24_update_clip_notes / v24_read_arrangement_clip), so every
replace here clears first — ``preTransforms: "delete"`` for the whole clip
(write replace, snapshot restore) and ``"<bar>|*: delete"`` for one bar
(surgery). ``preTransforms`` itself is uncaptured on 2.4; the readback after
each update is the authority, never the reply.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from mcp_server import pp_codec, pp_session


_SESSION_PATH = re.compile(r"^t(?P<track>\d+)/s(?P<scene>\d+)$")
_DURATION = re.compile(r"^(?:\d+bar(?:\+n\d*/\d+)?|n\d*/\d+)$")
_TIME_SIGNATURE = re.compile(r"^(?P<numerator>\d{1,2})/(?P<denominator>\d{1,2})$")


class ClipMutationError(RuntimeError):
    pass


def _result_dict(response: Any, label: str) -> dict[str, Any]:
    value = pp_session._result(response)
    if not isinstance(value, dict):
        raise ClipMutationError("%s response is malformed" % label)
    return value


def _track_path(track: dict[str, Any]) -> int | None:
    """2.2 trackIndex or the 2.3 track path 't3'."""
    return pp_session.track_index_of(track)


def _require_written(response: Any, label: str, **structure) -> Any:
    """Prove a create happened from the reply's STRUCTURE (pp_session.write_failed:
    one object, at the requested place, with the sent noteCount) — never from
    the words of its warnings. update-clip replies are not judged: the
    readback after each update is the authority."""
    failure = pp_session.write_failed(response, **structure)
    if failure:
        raise ClipMutationError("%s was not done: %s" % (label, failure))
    return response


def _is_sidekick_name(name: str) -> bool:
    return name == "SIDEKICK" or name.startswith("SIDEKICK ")


async def _resolve_track(session: Any, track_name: str) -> dict[str, Any]:
    if not isinstance(track_name, str) or not _is_sidekick_name(track_name):
        raise ValueError("track_name must be an exact SIDEKICK-owned track name")
    overview = _result_dict(await session.call(
        "ppal-read-live-set", {"include": ["tracks"]}), "Live Set")
    tracks = overview.get("tracks")
    if not isinstance(tracks, list):
        raise ClipMutationError("Live track inventory is unavailable")
    scene_count = overview.get("sceneCount")
    if isinstance(scene_count, bool) or not isinstance(scene_count, int) or scene_count < 1:
        raise ClipMutationError("Live scene inventory is unavailable")
    matches = [track for track in tracks
               if isinstance(track, dict) and track.get("name") == track_name]
    if len(matches) != 1:
        raise ClipMutationError("track_name must resolve to exactly one Live track")
    track = matches[0]
    index = _track_path(track)
    if index is None or not isinstance(track.get("id"), str):
        raise ClipMutationError("SIDEKICK track identity is unavailable")
    if track.get("type") != "midi":
        detail = _result_dict(await session.call(
            "ppal-read-track", pp_session.requests_for(session).read_track(
                index, include=[])), "track")
        if detail.get("type") != "midi":
            raise ClipMutationError("target SIDEKICK track is not a MIDI track")
        track = {**track, "type": "midi"}
    return {**track, "trackIndex": index, "sceneCount": scene_count}


def _validate_scene(scene_index: int) -> None:
    if isinstance(scene_index, bool) or not isinstance(scene_index, int) or scene_index < 0:
        raise ValueError("scene_index must be a non-negative integer")


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ValueError("name must be a nonempty string of at most 120 characters")


def _validate_loop(loop_start: str, loop_length: str) -> None:
    if not isinstance(loop_start, str) or re.fullmatch(r"[1-9]\d*\|[1-9]\d*(?:\.\d+)?", loop_start) is None:
        raise ValueError("loop_start must be a one-based bar|beat position")
    if not isinstance(loop_length, str) or _DURATION.fullmatch(loop_length) is None:
        raise ValueError("loop_length must be a Producer Pal duration such as 1bar or n/4")


def _location_args(session: Any, track_index: int, scene_index: int) -> tuple[str, dict]:
    location = pp_session.requests_for(session).clip(track=track_index, scene=scene_index)
    return location["path"], location


def _location_matches(clip: dict[str, Any], path: str) -> bool:
    if clip.get("path") == path:
        return True
    match = _SESSION_PATH.fullmatch(path)
    return bool(match and clip.get("slot") == "%s/%s" % (
        match.group("track"), match.group("scene")))


async def _read_slot(session: Any, location: dict) -> Any | None:
    """The slot's clip read, or None when the slot is PROVEN empty
    (``pp_session.slot_read_verdict``: only a captured empty-slot shape for
    this path). Anything unrecognised raises — never read as empty, since a
    create into an occupied slot replaces it on 2.4
    (v24_create_session_clip_replaces)."""
    path = location.get("path")
    try:
        response = await session.call(
            "ppal-read-clip", {**location, "include": ["notes", "timing"]})
    except Exception as exc:
        if pp_session.is_empty_slot_error(exc, path=path):
            return None
        raise
    verdict = pp_session.slot_read_verdict(path, reply=response)
    if verdict["state"] == "free":
        return None
    if verdict["state"] == "unknown":
        raise ClipMutationError("slot %s could not be proven empty: %s" % (path, verdict["detail"]))
    return response


def _encoded(notes: list[dict]) -> tuple[list[dict], str]:
    if not isinstance(notes, list) or not notes:
        raise ValueError("notes must be a non-empty list")
    normalized = pp_codec.normalize_notes_for_pp(pp_codec.note_dicts(notes))
    if not normalized:
        raise ValueError("notes contain no writable MIDI events")
    return normalized, pp_codec.encode_notes(normalized, pp_codec.CodecMode.MIDI_JSON)


def _verify_clip(response: Any, *, clip_id: str, path: str, name: str,
                 loop_start: str, loop_length: str, notes: list[dict]) -> dict[str, Any]:
    clip = _result_dict(response, "clip readback")
    if (clip.get("id") != clip_id or clip.get("type") != "midi"
            or clip.get("name") != name or not _location_matches(clip, path)):
        raise ClipMutationError("clip identity, name, track, or slot readback mismatch")
    if (clip.get("start") != loop_start or clip.get("length") != loop_length
            or clip.get("looping") is not True):
        raise ClipMutationError("clip loop bounds readback mismatch")
    pp_session.require_verified_notes(notes, response, "MIDI clip write")
    return clip


def _snapshot(response: Any, path: str) -> dict[str, Any]:
    clip = _result_dict(response, "clip snapshot")
    notes = pp_session.extract_notes(response)
    required = ("id", "name", "start", "length", "looping")
    if (clip.get("type") != "midi" or not _location_matches(clip, path)
            or notes is None or any(key not in clip for key in required)
            or not isinstance(clip.get("id"), str)
            or not isinstance(clip.get("name"), str)
            or not isinstance(clip.get("start"), str)
            or not isinstance(clip.get("length"), str)
            or not isinstance(clip.get("looping"), bool)):
        raise ClipMutationError("complete MIDI clip rollback snapshot is unavailable")
    return {"clip": clip, "notes": notes}


async def _restore_snapshot(session: Any, snapshot: dict[str, Any],
                            location: dict, path: str) -> bool:
    clip = snapshot["clip"]
    try:
        # judged by the readback below (the update reply is not a verdict)
        await session.call("ppal-update-clip", {
            "id": clip["id"], "name": clip["name"], "start": clip["start"],
            "length": clip["length"], "looping": clip["looping"],
            "preTransforms": "delete",
            "notes": pp_codec.encode_notes(
                snapshot["notes"], pp_codec.CodecMode.MIDI_JSON),
        })
        response = await session.call(
            "ppal-read-clip", {**location, "include": ["notes", "timing"]})
        restored = _result_dict(response, "clip rollback")
        if (restored.get("id") != clip["id"] or restored.get("type") != "midi"
                or not _location_matches(restored, path)):
            return False
        for key in ("name", "start", "length", "looping", "end", "timeSignature"):
            if key in clip and restored.get(key) != clip.get(key):
                return False
        notes = pp_session.extract_notes(response)
        return notes is not None and _canonical(notes) == _canonical(snapshot["notes"])
    except Exception:
        return False


async def _delete_created_clip(session: Any, location: dict, path: str) -> bool:
    try:
        response = await _read_slot(session, location)
        if response is None:
            return True
        clip = _result_dict(response, "created clip cleanup")
        if not _location_matches(clip, path) or not isinstance(clip.get("id"), str):
            return False
        await session.call("ppal-delete", pp_session.requests_for(session).delete(
            "clip", ids=[clip["id"]]))
        return await _read_slot(session, location) is None
    except Exception:
        return False


async def write_midi_clip(track_name: str, scene_index: int, notes: list[dict],
                          name: str = "SIDEKICK MIDI", loop_start: str = "1|1",
                          loop_length: str = "1bar", replace: bool = False,
                          confirm: bool = False, *,
                          operation_session: Callable | None = None) -> dict[str, Any]:
    """Preview or write one verified MIDI clip on an exact Sidekick-owned track."""
    _validate_scene(scene_index)
    _validate_name(name)
    _validate_loop(loop_start, loop_length)
    normalized, wire_notes = _encoded(notes)
    operation_session = operation_session or pp_session.operation_session
    async with operation_session() as owned:
        session = pp_session.WarningLog(owned)
        track = await _resolve_track(session, track_name)
        if scene_index >= track["sceneCount"]:
            raise ClipMutationError("scene_index must name an existing session slot")
        path, location = _location_args(session, track["trackIndex"], scene_index)
        existing_response = await _read_slot(session, location)
        existing = pp_session._result(existing_response) if existing_response is not None else None
        if existing is not None and not _location_matches(existing, path):
            raise ClipMutationError("target slot readback returned a different Live location")
        if existing is not None and not replace:
            raise ClipMutationError("target session slot is occupied; pass replace=True to replace it")
        if existing is not None and existing.get("type") != "midi":
            raise ClipMutationError("replace=True may replace only a Sidekick-owned MIDI clip")
        if existing is not None and not isinstance(existing.get("id"), str):
            raise ClipMutationError("occupied Sidekick clip identity is unavailable")
        prior_snapshot = _snapshot(existing_response, path) if existing_response is not None else None
        prior_clip_ref = None
        if existing is not None:
            prior_clip_ref = {
                "clip_id": existing["id"], "track_id": track["id"],
                "track": track_name, "surface": "session", "path": path,
                "name": existing.get("name"),
                "answer_notes": (prior_snapshot or {}).get("notes"),
            }

        if existing is None:
            tool = "ppal-create-clip"
            arguments = {
                **location, "name": name, "start": loop_start,
                "length": loop_length, "looping": True, "notes": wire_notes,
            }
            prior_clip_id = None
        else:
            tool = "ppal-update-clip"
            arguments = {
                "id": existing["id"], "name": name, "start": loop_start,
                "length": loop_length, "looping": True,
                "preTransforms": "delete", "notes": wire_notes,
            }
            prior_clip_id = existing["id"]

        if not confirm:
            return {
                "status": "preview", "track_name": track_name, "path": path,
                "replace": existing is not None, "writes": 0,
                "intended_operation": {"tool": tool, "arguments": arguments},
            }

        set_identity = None
        if session.capabilities.get("live_api"):
            from mcp_server import live_write
            # Establish the Set boundary before the first mutation, while this
            # exact operation session still owns both the identity and write.
            set_identity = await live_write.read_set_identity(session)

        try:
            result = await session.call(tool, arguments)
            if existing is None:
                _require_written(result, "MIDI clip create", count=1, location=arguments,
                                 note_count=len(normalized))
            # a replace (update-clip) is judged by the readback below
            replied = pp_session.result_objects(result)
            clip_id = replied[0].get("id") if len(replied) == 1 else None
            if not isinstance(clip_id, str):
                clip_id = prior_clip_id
            if not isinstance(clip_id, str):
                raise ClipMutationError("Producer Pal did not return a clip id")
            readback = await session.call(
                "ppal-read-clip", {**location, "include": ["notes", "timing"]})
            _verify_clip(
                readback, clip_id=clip_id, path=path, name=name,
                loop_start=loop_start, loop_length=loop_length, notes=normalized)
            verified_clip = _result_dict(readback, "clip readback")
        except Exception as exc:
            restored = (await _restore_snapshot(
                session, prior_snapshot, location, path) if prior_snapshot is not None
                else await _delete_created_clip(session, location, path))
            if not restored:
                raise ClipMutationError(
                    "MIDI clip write failed and rollback could not be verified") from exc
            raise

    clip_ref = pp_session.clip_ref_from_record(
        verified_clip, track_id=track["id"], track_name=track_name,
        surface="session", notes=normalized)
    if clip_ref is None:
        raise ClipMutationError("verified MIDI write did not yield a complete clip reference")
    receipt = {
        "status": "verified", "operation": "replace" if prior_clip_id else "create",
        "track_name": track_name, "track_id": track["id"], "path": path,
        "clip_id": clip_id, "name": name, "note_count": len(normalized),
        "loop_start": loop_start, "loop_length": loop_length,
        "verification": "verified",
        "set_identity": set_identity, "clip_ref": clip_ref,
        "pp_warnings": list(session.warnings),
    }
    if prior_clip_ref is not None:
        receipt["prior_clip_ref"] = prior_clip_ref
    return receipt


def _canonical(notes: list[dict]) -> list[tuple[int, float, float, int]]:
    return sorted((
        int(note["pitch"]), round(float(note["start_beats"]), 6),
        round(float(note["duration_beats"]), 6), int(note["velocity"]),
    ) for note in notes)


def _in_bar(note: dict, start: float, end: float) -> bool:
    onset = float(note["start_beats"])
    return start <= onset < end


def _surgery_notes(surgery: Any, beats_per_bar: float,
                   bar_start: float) -> tuple[list[dict], list[dict]]:
    if not isinstance(surgery, dict) or set(surgery) != {"notes"} or not isinstance(surgery["notes"], list):
        raise ValueError("surgery must contain exactly a notes list")
    raw = pp_codec.note_dicts(surgery["notes"])
    for note in raw:
        if float(note["start"]) < 0:
            raise ValueError("surgery notes must fit wholly inside the selected bar")
    relative = pp_codec.normalize_notes_for_pp(raw)
    for note in relative:
        start = float(note["start"])
        duration = float(note["duration"])
        if start < 0 or start + duration > beats_per_bar + 1e-9:
            raise ValueError("surgery notes must fit wholly inside the selected bar")
    shifted = [{
        "pitch": note["pitch"], "start_beats": float(note["start"]) + bar_start,
        "duration_beats": note["duration"], "velocity": note["velocity"],
    } for note in relative]
    return relative, shifted


def _duration_beats(token: str, beats_per_bar: float) -> float | None:
    if not isinstance(token, str):
        return None
    match = re.fullmatch(
        r"(?:(?P<bars>\d+)bar)?(?:\+?(?:n(?P<count>\d*)/(?P<divisor>\d+)))?",
        token or "")
    if match is None or not any(match.group(name) is not None
                                for name in ("bars", "divisor")):
        return None
    total = int(match.group("bars") or 0) * beats_per_bar
    if match.group("divisor") is not None:
        divisor = int(match.group("divisor"))
        if divisor <= 0:
            return None
        total += int(match.group("count") or 1) * 4.0 / divisor
    return float(total)


async def surgery_clip(path: str, bar: int, surgery: dict,
                       confirm: bool = False, *,
                       operation_session: Callable | None = None) -> dict[str, Any]:
    """Replace one bar of a Sidekick MIDI clip and prove other bars unchanged."""
    match = _SESSION_PATH.fullmatch(path) if isinstance(path, str) else None
    if match is None:
        raise ValueError("path must use t<track>/s<scene>")
    if isinstance(bar, bool) or not isinstance(bar, int) or bar < 1:
        raise ValueError("bar must be a one-based positive integer")
    track_index = int(match.group("track"))
    scene_index = int(match.group("scene"))
    operation_session = operation_session or pp_session.operation_session
    async with operation_session() as owned:
        session = pp_session.WarningLog(owned)
        _path, location = _location_args(session, track_index, scene_index)
        overview = _result_dict(await session.call(
            "ppal-read-live-set", {"include": ["tracks"]}), "Live Set")
        track = next((item for item in overview.get("tracks", [])
                      if isinstance(item, dict) and _track_path(item) == track_index), None)
        if (not isinstance(track, dict) or not isinstance(track.get("name"), str)
                or not _is_sidekick_name(track["name"])):
            raise ClipMutationError("refusing surgery on a non-Sidekick track")
        before_response = await _read_slot(session, location)
        if before_response is None:
            raise ClipMutationError("path does not contain a clip")
        before = _result_dict(before_response, "source clip")
        if before.get("type") != "midi" or not _location_matches(before, path):
            raise ClipMutationError("surgery requires a Sidekick-owned MIDI clip")
        before_snapshot = _snapshot(before_response, path)
        notes_before = pp_session.extract_notes(before_response)
        if notes_before is None:
            raise ClipMutationError("complete clip note snapshot is unavailable")
        signature = before.get("timeSignature")
        signature_match = _TIME_SIGNATURE.fullmatch(signature) if isinstance(signature, str) else None
        if signature_match is None:
            raise ClipMutationError("clip time signature is unavailable")
        numerator = int(signature_match.group("numerator"))
        denominator = int(signature_match.group("denominator"))
        if numerator <= 0 or denominator <= 0:
            raise ClipMutationError("clip time signature is invalid")
        beats_per_bar = numerator * 4.0 / denominator
        bar_start = float((bar - 1) * beats_per_bar)
        bar_end = bar_start + beats_per_bar
        clip_length = _duration_beats(before.get("length"), beats_per_bar)
        if clip_length is None or bar_end > clip_length + 1e-9:
            raise ClipMutationError("selected bar is outside the complete clip region")
        relative, replacement = _surgery_notes(surgery, beats_per_bar, bar_start)
        unaffected_before = [note for note in notes_before
                             if not _in_bar(note, bar_start, bar_end)]
        original_bar = [note for note in notes_before if _in_bar(note, bar_start, bar_end)]
        pre_transform = "%d|*: delete" % bar
        wire_notes = pp_codec.encode_notes(replacement, pp_codec.CodecMode.MIDI_JSON)
        arguments = {
            "id": before["id"], "preTransforms": pre_transform,
            "notes": wire_notes,
        }
        set_identity = None
        if session.capabilities.get("live_api"):
            from mcp_server import live_write
            set_identity = await live_write.read_set_identity(session)
        if not confirm:
            return {
                "status": "preview", "path": path, "bar": bar, "writes": 0,
                "replacement_note_count": len(relative),
                "intended_operation": {"tool": "ppal-update-clip", "arguments": arguments},
            }

        try:
            # judged by the readback below (the update reply is not a verdict)
            await session.call("ppal-update-clip", arguments)
            after_response = await session.call(
                "ppal-read-clip", {**location, "include": ["notes", "timing"]})
            after = _result_dict(after_response, "surgery readback")
            if (after.get("id") != before.get("id") or after.get("name") != before.get("name")
                    or after.get("type") != "midi" or not _location_matches(after, path)):
                raise ClipMutationError("clip identity changed during surgery")
            timing_keys = ("timeSignature", "looping", "start", "end", "length")
            if any(after.get(key) != before.get(key) for key in timing_keys):
                raise ClipMutationError("clip loop bounds changed during surgery")
            notes_after = pp_session.extract_notes(after_response)
            if notes_after is None:
                raise ClipMutationError("surgery note readback is unavailable")
            unaffected_after = [note for note in notes_after
                                if not _in_bar(note, bar_start, bar_end)]
            actual_bar = [note for note in notes_after if _in_bar(note, bar_start, bar_end)]
            if _canonical(unaffected_after) != _canonical(unaffected_before):
                raise ClipMutationError("notes outside the selected bar changed")
            if _canonical(actual_bar) != _canonical(replacement):
                raise ClipMutationError("selected bar replacement readback mismatch")
            after_clip = dict(after)
        except Exception as exc:
            if not await _restore_snapshot(session, before_snapshot, location, path):
                raise ClipMutationError(
                    "clip surgery failed and rollback could not be verified") from exc
            raise

    undo_notes = [{
        "pitch": note["pitch"], "start_beats": float(note["start_beats"]) - bar_start,
        "duration_beats": note["duration_beats"], "velocity": note["velocity"],
    } for note in original_bar]
    track_name = track.get("name")
    if not isinstance(track_name, str):
        raise ClipMutationError("surgery track name is unavailable")
    prior_clip_ref = pp_session.clip_ref_from_record(
        before, track_id=str(track.get("id") or ""), track_name=track_name,
        surface="session", notes=notes_before)
    clip_ref = pp_session.clip_ref_from_record(
        after_clip, track_id=str(track.get("id") or ""), track_name=track_name,
        surface="session", notes=notes_after)
    if prior_clip_ref is None or clip_ref is None:
        raise ClipMutationError("surgery did not yield a complete verified clip reference")
    return {
        "status": "verified", "path": path, "bar": bar,
        "clip_id": before["id"], "replacement_note_count": len(relative),
        "unaffected_notes_verified": len(unaffected_before),
        "preTransforms": pre_transform, "verification": "verified",
        "set_identity": set_identity, "prior_clip_ref": prior_clip_ref,
        "clip_ref": clip_ref, "answer_notes": notes_after,
        "undo": {"tool": "surgery_clip", "arguments": {
            "path": path, "bar": bar, "surgery": {"notes": undo_notes},
            "confirm": True}},
        "pp_warnings": list(session.warnings),
    }
