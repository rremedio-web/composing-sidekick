"""Confirmed arrangement execution on fresh duplicated SIDEKICK tracks."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Any, Callable

from mcp_server import pp_codec, pp_session


class ArrangementExecutionError(RuntimeError):
    pass


def _dict(response: Any, label: str) -> dict[str, Any]:
    value = pp_session._result(response)
    if not isinstance(value, dict):
        raise ArrangementExecutionError("%s response is malformed" % label)
    return value


_TRACKS_N = re.compile(r"(tracks\s+)\d+")
_TRACK_PATH = re.compile(r"^t\d+(?=$|[/\[])")


def _position_free(value: Any) -> Any:
    """Normalize track positions that legitimately change during arrangement.

    Track paths retain their scene/arrangement location while replacing only
    the leading track number; notes, devices and mixer edits remain visible.
    """
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "trackIndex":
                continue
            if (key == "slot" and isinstance(item, str)
                    and re.fullmatch(r"\d+/\d+", item)):
                out[key] = item.split("/", 1)[1]
                continue
            if key == "path" and isinstance(item, str):
                out[key] = _TRACK_PATH.sub(
                    "t#", _TRACKS_N.sub(r"\1#", item))
            else:
                out[key] = _position_free(item)
        return out
    if isinstance(value, list):
        return [_position_free(item) for item in value]
    return value


def _fingerprint(response: Any) -> str:
    value = _position_free(pp_session._result(response))
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ArrangementExecutionError("source track fingerprint is unavailable") from exc
    return hashlib.sha256(encoded).hexdigest()


def _strip_identity(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_identity(item) for key, item in value.items()
                if key not in {"id", "path", "trackIndex"}}
    if isinstance(value, list):
        return [_strip_identity(item) for item in value]
    return value


def _duplicate_state(response: Any) -> dict[str, Any]:
    value = response if isinstance(response, dict) else _dict(response, "track state")
    if "devices" not in value or not isinstance(value["devices"], list):
        raise ArrangementExecutionError("device or mixer readback is unavailable")
    if all(key in value for key in ("gainDb", "pan", "sends")):
        mixer = {key: value[key] for key in ("gainDb", "pan", "sends")}
    else:
        raise ArrangementExecutionError("device or mixer readback is unavailable")
    return {
        "mixer": _strip_identity(mixer),
        "devices": _strip_identity(value.get("devices", [])),
    }


async def _read_track(session: Any, track_index: int) -> Any:
    return await session.call("ppal-read-track", pp_session.requests_for(session).read_track(
        track_index,
        include=["mixer", "devices", "notes", "session-clips", "arrangement-clips"]))


def _require_written(response: Any, label: str, **structure) -> Any:
    """Prove a write from the reply's STRUCTURE (pp_session.write_failed), never
    from the words of its warnings: a duplicated track's "removed the Producer
    Pal device" warning comes next to a duplicate that happened."""
    failure = pp_session.write_failed(response, **structure)
    if failure:
        raise ArrangementExecutionError("%s was not done: %s" % (label, failure))
    return response


def _index_of(track: Any) -> int | None:
    """2.2 trackIndex or the 2.3 track path 't3'."""
    return pp_session.track_index_of(track)


async def cleanup_fresh_tracks(session: Any, track_ids: list[str]) -> list[str]:
    """Delete fresh Sidekick tracks and return any IDs not proven absent."""
    requests = pp_session.requests_for(session)
    for track_id in reversed(track_ids):
        try:
            await session.call("ppal-delete", requests.delete("track", ids=[track_id]))
        except Exception:
            pass
    try:
        overview = _dict(await session.call(
            "ppal-read-live-set", {"include": ["tracks"]}), "cleanup Live Set")
        tracks = overview.get("tracks")
        if not isinstance(tracks, list):
            return list(track_ids)
        live_ids = {item.get("id") for item in tracks if isinstance(item, dict)}
        return [track_id for track_id in track_ids if track_id in live_ids]
    except Exception:
        return list(track_ids)


def _source_indices(parts: list[dict]) -> dict[str, tuple[int, int]]:
    out = {}
    for part in parts:
        match = re.fullmatch(r"t(\d+)/s(\d+)", part["source_path"])
        if match is None:
            raise ArrangementExecutionError("source path is malformed")
        out[part["part_id"]] = (int(match.group(1)), int(match.group(2)))
    return out


def _clip_results(response: Any, expected_track: int,
                  expected_start: str | None = None) -> list[dict[str, Any]]:
    """The copies a clip duplicate made: on the expected track and, with
    ``expected_start``, starting there (2.2 arrangementStart, 2.3 the position
    in the path 't3[17|1]'; a tiled copy's first piece starts there)."""
    value = pp_session._result(response)
    if isinstance(value, dict) and isinstance(value.get("clips"), list):
        items = value["clips"]
    elif isinstance(value, (dict, list)):
        # one copy (2.2, 2.3 dict) or several (a list)
        items = pp_session.result_objects(response)
    else:
        raise ArrangementExecutionError("clip duplicate response is malformed")
    envelope_path = None
    if isinstance(value, dict) and "trackIndex" in value:
        track_index = value["trackIndex"]
        if isinstance(track_index, bool) or not isinstance(track_index, int):
            raise ArrangementExecutionError("clip duplicate response is malformed")
        envelope_path = "t%d" % track_index
    clean = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ArrangementExecutionError("clip duplicate response is malformed")
        normalized = pp_session.normalize_clip(item)
        item_path = normalized["track_path"] or item.get("path")
        if normalized["arrangement_start"] is not None:
            item = {**item, "arrangementStart": normalized["arrangement_start"]}
        if item_path is not None and not isinstance(item_path, str):
            raise ArrangementExecutionError("clip duplicate response is malformed")
        if (item_path is not None and envelope_path is not None
                and item_path != envelope_path):
            raise ArrangementExecutionError("clip duplicate landed on the wrong track")
        if (item_path or envelope_path) != "t%d" % expected_track:
            raise ArrangementExecutionError("clip duplicate landed on the wrong track")
        clean.append(item)
    if not clean:
        raise ArrangementExecutionError("clip duplicate created no clips")
    if expected_start is not None:
        starts = [pp_session.clip_location({"path": "t%d" % expected_track,
                                            "arrangementStart": item.get("arrangementStart")})
                  for item in clean if item.get("arrangementStart")]
        wanted = pp_session.clip_location({"path": "t%d" % expected_track,
                                           "arrangementStart": expected_start})
        if starts and min(start[3] for start in starts) != wanted[3]:
            raise ArrangementExecutionError(
                "clip duplicate landed at %s, not at %s" % (
                    ", ".join(str(item.get("arrangementStart")) for item in clean),
                    expected_start))
    return clean


def _refuse_overlapping_placements(placements: list[dict]) -> None:
    """Placements of one part that overlap are refused before any write: a
    copy dropped on a clip replaces it and Producer Pal does not say so
    (v24_duplicate_over_existing; pp_codec.find_overlapping_spans)."""
    by_part: dict[str, list] = {}
    for ordinal, operation in enumerate(placements, 1):
        start = float(operation["bar_start"]) * pp_codec.BEATS_PER_BAR
        bars = int(operation["bars"])
        if bars < 1:
            raise ArrangementExecutionError("placement %d has no length" % ordinal)
        by_part.setdefault(str(operation["part_id"]), []).append(
            ("placement %d at bar %d" % (ordinal, int(operation["bar_start"]) + 1),
             start, start + bars * pp_codec.BEATS_PER_BAR))
    for part_id, spans in by_part.items():
        overlaps = pp_codec.find_overlapping_spans(spans)
        if overlaps:
            raise ArrangementExecutionError(
                "placements overlap on part %r (%s) — a copy placed over another replaces "
                "it; fix the plan" % (part_id, "; ".join("%s with %s" % pair for pair in overlaps)))


def _require_destination_free(listing: dict[str, Any], track_index: int, own_ids: set) -> None:
    """The fresh track must hold nothing but the copies this run placed on
    it: any other arrangement clip would be replaced by the next copy
    (Live's semantics, silent on 2.2 / 2.3 / 2.4 — v24_duplicate_over_existing)."""
    _session_clips, arrangement_clips = _require_clip_lists(listing)
    foreign = [clip for clip in arrangement_clips
               if not isinstance(clip, dict) or clip.get("id") not in own_ids]
    if foreign:
        first = foreign[0] if isinstance(foreign[0], dict) else {}
        raise ArrangementExecutionError(
            "destination t%d is not free: clip %s at %s is not one of this run's copies — "
            "a copy placed over it would replace it" % (
                track_index, first.get("id") or "?",
                pp_session.arrangement_start_of(first) or first.get("path") or "?"))


def _require_clip_lists(track: dict[str, Any]) -> tuple[list, list]:
    session_keys = ("sessionClips", "session-clips", "session_clips")
    arrangement_keys = ("arrangementClips", "arrangement-clips", "arrangement_clips")
    session = next((track[key] for key in session_keys if key in track), None)
    arrangement = next((track[key] for key in arrangement_keys if key in track), None)
    if not isinstance(session, list) or not isinstance(arrangement, list):
        raise ArrangementExecutionError("track clip readback is unavailable")
    return session, arrangement


async def execute_arrangement(parts: list[dict], placements: list[dict], *,
                              operation_session: Callable | None = None) -> dict[str, Any]:
    """Duplicate source tracks without clips, place copies, and verify sources unchanged."""
    operation_session = operation_session or pp_session.operation_session
    indices = _source_indices(parts)
    _refuse_overlapping_placements(placements)
    run_id = secrets.token_hex(4)
    created: list[dict[str, Any]] = []
    cleanup_track_ids: list[str] = []
    source_before: dict[int, str] = {}
    source_state: dict[int, dict[str, Any]] = {}
    clip_ids: dict[str, str] = {}

    async with operation_session() as owned:
        session = pp_session.WarningLog(owned)
        requests = pp_session.requests_for(session)
        overview = _dict(await session.call(
            "ppal-read-live-set", {"include": ["tracks"]}), "Live Set")
        tracks = overview.get("tracks")
        if not isinstance(tracks, list):
            raise ArrangementExecutionError("Live track inventory is unavailable")
        track_by_index = {_index_of(track): track for track in tracks
                          if isinstance(track, dict)}
        track_by_id = {track.get("id"): track for track in tracks
                       if isinstance(track, dict)}
        initial_source_ids = {index: track["id"] for index, track in track_by_index.items()
                              if isinstance(track.get("id"), str)}
        source_ids_by_part: dict[str, str] = {}
        for part in parts:
            track_index = indices[part["part_id"]][0]
            source_id = initial_source_ids.get(track_index)
            if source_id is None:
                raise ArrangementExecutionError("source track is unavailable")
            source_ids_by_part[part["part_id"]] = source_id
        existing_ids = {track.get("id") for track in tracks if isinstance(track, dict)}

        for track_index, _scene in set(indices.values()):
            track = track_by_index.get(track_index)
            if not isinstance(track, dict) or not isinstance(track.get("id"), str):
                raise ArrangementExecutionError("source track is unavailable")
            source_read = await _read_track(session, track_index)
            source_id = track["id"]
            source_before[source_id] = _fingerprint(source_read)
            source_state[source_id] = _duplicate_state(source_read)

        for part in parts:
            track_index, scene_index = indices[part["part_id"]]
            source_track = track_by_index[track_index]
            clip = _dict(await session.call("ppal-read-clip", {
                **requests.clip(track=track_index, scene=scene_index), "include": []}),
                "source clip")
            clip_id = clip.get("id")
            if (not isinstance(clip_id, str) or not clip_id
                    or clip.get("type") not in {"midi", "audio"}):
                raise ArrangementExecutionError("source clip id is unavailable")
            clip_ids[part["part_id"]] = clip_id

        try:
            for part in parts:
                source_index, _ = indices[part["part_id"]]
                source_id = track_by_index[source_index]["id"]
                name = "SIDEKICK ARR %s %s" % (part["part_id"], run_id)
                duplicate_reply = await session.call("ppal-duplicate", requests.duplicate_track(
                    source_id, count=1, name=name, withoutClips=True, withoutDevices=False))
                replied = pp_session.result_objects(duplicate_reply)
                new_id = replied[0].get("id") if len(replied) == 1 else None
                if isinstance(new_id, str) and new_id not in existing_ids:
                    cleanup_track_ids.append(new_id)
                _require_written(duplicate_reply, "track duplicate", count=1)
                if len(replied) != 1:
                    raise ArrangementExecutionError("track duplicate response is malformed")
                # 2.2 replies trackIndex, 2.3 the path 't3'
                new_index = _index_of(replied[0])
                if (not isinstance(new_id, str) or new_id in existing_ids
                        or isinstance(new_index, bool) or not isinstance(new_index, int)):
                    raise ArrangementExecutionError("fresh duplicate track identity is unavailable")
                existing_ids.add(new_id)
                # Producer Pal inserts a duplicate immediately after its source.
                # Every later track can move, so refresh the live inventory and
                # resolve all pending source indices by their stable IDs.
                overview = _dict(await session.call(
                    "ppal-read-live-set", {"include": ["tracks"]}), "Live Set")
                tracks = overview.get("tracks")
                if not isinstance(tracks, list):
                    raise ArrangementExecutionError("Live track inventory is unavailable")
                track_by_index = {_index_of(track): track for track in tracks
                                  if isinstance(track, dict)}
                track_by_id = {track.get("id"): track for track in tracks
                               if isinstance(track, dict)}
                new_track = track_by_id.get(new_id)
                if not isinstance(new_track, dict):
                    raise ArrangementExecutionError("fresh duplicate track is unavailable")
                new_index = _index_of(new_track)
                if isinstance(new_index, bool) or not isinstance(new_index, int):
                    raise ArrangementExecutionError("fresh duplicate track identity is unavailable")
                source_ids_by_part[part["part_id"]] = source_id
                for pending in parts:
                    pending_index, _ = indices[pending["part_id"]]
                    pending_id = source_ids_by_part.get(pending["part_id"])
                    if pending_id is None:
                        pending_id = initial_source_ids[pending_index]
                    current = track_by_id.get(pending_id)
                    if not isinstance(current, dict):
                        raise ArrangementExecutionError("source track is unavailable")
                    indices[pending["part_id"]] = (_index_of(current),
                                                    indices[pending["part_id"]][1])
                for item in created:
                    item["track_index"] = _index_of(track_by_id[item["track_id"]])
                created.append({"part_id": part["part_id"], "track_id": new_id,
                                "track_index": new_index, "name": name})
                readback = _dict(await _read_track(session, new_index), "new track")
                if (readback.get("name") != name or readback.get("id") != new_id
                        or _index_of(readback) != new_index):
                    raise ArrangementExecutionError("fresh duplicate track name mismatch")
                if _duplicate_state(readback) != source_state[source_id]:
                    raise ArrangementExecutionError("fresh duplicate lost device or mixer state")
                session_clips, arrangement_clips = _require_clip_lists(readback)
                if session_clips or arrangement_clips:
                    raise ArrangementExecutionError("fresh duplicate track contains clips")

            placed = []
            created_by_part = {item["part_id"]: item for item in created}
            own_ids: dict[str, set] = {item["part_id"]: set() for item in created}
            for operation in placements:
                destination = created_by_part[operation["part_id"]]
                # the span must be free of anything but this run's own copies
                # (the plan itself was checked for overlaps before any write)
                _require_destination_free(
                    _dict(await _read_track(session, destination["track_index"]),
                          "destination readback"),
                    destination["track_index"], own_ids[operation["part_id"]])
                # 2.2: toPath 't3' + arrangementStart; 2.3: toPath 't3[17|1]'
                args = requests.duplicate_clip_to_arrangement(
                    clip_ids[operation["part_id"]], track=destination["track_index"],
                    start="%d|1" % (operation["bar_start"] + 1),
                    length="%dbar" % operation["bars"])
                result = _require_written(await session.call("ppal-duplicate", args),
                                          "clip placement")
                copies = _clip_results(result, destination["track_index"],
                                       "%d|1" % (operation["bar_start"] + 1))
                own_ids[operation["part_id"]].update(copy["id"] for copy in copies)
                placed.append({"part_id": operation["part_id"], "arguments": args,
                               "clip_ids": [copy["id"] for copy in copies]})

            for item in created:
                listing = _dict(await _read_track(session, item["track_index"]),
                                "arrangement readback")
                _session_clips, arrangement_clips = _require_clip_lists(listing)
                known = {clip.get("id") for clip in arrangement_clips
                         if isinstance(clip, dict)}
                expected = {clip_id for placement in placed
                            if placement["part_id"] == item["part_id"]
                            for clip_id in placement["clip_ids"]}
                if not expected or not expected.issubset(known):
                    raise ArrangementExecutionError("arrangement clip readback mismatch")

            for source_id, before in source_before.items():
                current = track_by_id.get(source_id)
                if not isinstance(current, dict) or _fingerprint(
                        await _read_track(session, _index_of(current))) != before:
                    raise ArrangementExecutionError("source track changed during arrangement")
        except Exception as exc:
            orphans = await cleanup_fresh_tracks(session, cleanup_track_ids)
            if orphans:
                raise ArrangementExecutionError(
                    "arrangement failed and cleanup left fresh Sidekick tracks: %s"
                    % ",".join(orphans)) from exc
            raise

    return {
        "run_id": run_id,
        "created_tracks": created,
        "placements": placed,
        "source_fingerprints_verified": True,
        "undo": [{"tool": "ppal-delete", "arguments": requests.delete(
            "track", ids=[item["track_id"]])} for item in reversed(created)],
        "pp_warnings": list(session.warnings),
    }
