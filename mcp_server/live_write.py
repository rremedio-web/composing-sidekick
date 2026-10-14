"""Live write engine — drive Producer Pal over its MCP-HTTP endpoint to write
tracks/clips into the running Ableton Set.

Producer Pal exposes MCP at http://localhost:3350/mcp (v2.1 JSON responses).
This module connects via the `mcp` SDK, creates
tracks, and uses style-sidekick's `send_to_ableton` translation to place clips —
then reads them back to verify. Shared by the full-version write and every
"live" leap (full-song generation, live-context collaboration).

Usage (smoke test — prints the current Set):
    .venv/bin/python mcp_server/live_write.py

Programmatic:
    import asyncio, live_write
    asyncio.run(live_write.write_tracks(
        [{"name": "Lead", "notes": [...], "color": "#7B4DFF"}],
        tempo=75, set_tempo=True))
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402
import pp_codec  # noqa: E402
import pp_session  # noqa: E402
import core_shared  # noqa: E402

for _alias in ("live_write", "mcp_server.live_write"):   # one module universe
    sys.modules.setdefault(_alias, sys.modules[__name__])

# Re-export transport URL + verification error from the session adapter.
PP_URL = pp_session.PP_URL
LiveWriteVerificationError = pp_session.LiveWriteVerificationError
READBACK_TIME_TOLERANCE_BEATS = pp_codec.READBACK_TIME_TOLERANCE_BEATS


def _handshake_probe_failure(exc: Exception) -> dict:
    """Keep the rejected handshake evidence visible in readiness diagnostics."""
    detail = str(exc)
    version_match = re.search(r"Producer Pal (\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)", detail)
    version = version_match.group(1) if version_match else pp_session.PP_VERSION_UNKNOWN
    return {
        "pp_version": version,
        "live_api": False, "set_saved": False, "identity": None,
        "ready": False,
        "reason": "Producer Pal handshake rejected: %s" % detail,
    }

# The ONE track the sidekick may auto-write to (find-or-create by name). Everything
# else in the codebase stays payload-preview / confirm-gated. Undo = delete the track.
JAM_TRACK_NAME = "SIDEKICK JAM"
JAM_TRACK_COLOR = "#FF6A00"

# Second auto-write surface (2026-07-03): blind-listen staging for ear sessions.
# Same policy shape as the jam track: found-or-created by NAME, append-only —
# no deletes, no overwrites. Clearing old rounds is HIS manual act in Live.
BOOTH_TRACK_NAME = "SIDEKICK BOOTH"
BOOTH_TRACK_COLOR = "#FFB000"

# --- JSON session adapter + explicitly isolated PP<2.0 fixture shims ----------
def _legacy_fixture_call(fn, response, *args, **kwargs):
    """Run one old text fixture through the explicit legacy switch only."""
    if isinstance(response, pp_session.PPResponse):
        return fn(response, *args, **kwargs)
    old = pp_session.LEGACY_TEXT_RESPONSE_MODE
    pp_session.LEGACY_TEXT_RESPONSE_MODE = True
    try:
        return fn(response, *args, **kwargs)
    finally:
        pp_session.LEGACY_TEXT_RESPONSE_MODE = old


async def _call(session, name, args=None):
    # Existing envelope_bridge probes still hand this private alias PP<2.0 text.
    # The public operation session stays strict JSON; this shim is fixture-only.
    old = pp_session.LEGACY_TEXT_RESPONSE_MODE
    pp_session.LEGACY_TEXT_RESPONSE_MODE = True
    try:
        return await pp_session.call_tool(session, name, args)
    finally:
        pp_session.LEGACY_TEXT_RESPONSE_MODE = old


_find_int = lambda response, *a, **k: _legacy_fixture_call(pp_session.find_int, response, *a, **k)
_find_str = lambda response, *a, **k: _legacy_fixture_call(pp_session.find_str, response, *a, **k)
_extract_selected_context = lambda response, *a, **k: _legacy_fixture_call(
    pp_session.extract_selected_context, response, *a, **k)
_extract_notes = lambda response, *a, **k: _legacy_fixture_call(
    pp_session.extract_notes, response, *a, **k)
_max_scene_index_in_track_text = lambda response, *a, **k: _legacy_fixture_call(
    pp_session.max_scene_index_in_track, response, *a, **k)
_parse_session_clips = lambda response, *a, **k: _legacy_fixture_call(
    pp_session.parse_session_clips, response, *a, **k)
_track_index_by_name = lambda response, *a, **k: _legacy_fixture_call(
    pp_session.track_index_by_name, response, *a, **k)
_regular_track_count = lambda response, *a, **k: _legacy_fixture_call(
    pp_session.regular_track_count, response, *a, **k)
_clip_type_token = lambda response, *a, **k: _legacy_fixture_call(
    pp_session.clip_type_token, response, *a, **k)
_clip_id_token = lambda response, *a, **k: _legacy_fixture_call(
    pp_session.clip_id_token, response, *a, **k)
is_empty_slot_error = pp_session.is_empty_slot_error
classify_session_clip = lambda response, *a, **k: _legacy_fixture_call(
    pp_session.classify_session_clip, response, *a, **k)
track_names_by_index = lambda response, *a, **k: _legacy_fixture_call(
    pp_session.track_names_by_index, response, *a, **k)
_require_scene_count = lambda response, *a, **k: _legacy_fixture_call(
    pp_session.require_scene_count, response, *a, **k)
_verify_count = lambda response, *a, **k: _legacy_fixture_call(
    pp_session.verify_count, response, *a, **k)
_raise_partial_write = pp_session.raise_partial_write
_require_verified_note_count = pp_session.require_verified_note_count
_build_set_identity = pp_session.build_set_identity
_build_runtime_epoch = pp_session.build_runtime_epoch
_live_api_root_id = pp_session.live_api_root_id
_clip_ref_from_record = pp_session.clip_ref_from_record
_extract_delete_receipt = pp_session.extract_delete_receipt
SESSION_CLIP_KEYS = pp_session.SESSION_CLIP_KEYS
ARRANGEMENT_CLIP_KEYS = pp_session.ARRANGEMENT_CLIP_KEYS
TRACK_READ_INCLUDE_INVENTORY = pp_session.TRACK_READ_INCLUDE_INVENTORY
TRACK_READ_INCLUDE_SESSION = pp_session.TRACK_READ_INCLUDE_SESSION


class LiveSetIdentityError(LiveWriteVerificationError):
    """Live Set identity could not be verified for a mutation boundary."""


class LiveApiObjectMissing(RuntimeError):
    """ppal-live-api answered for an id Live does not have: the result-less
    ``{"id": "0", "path": "", "results": [{"operation": …}]}`` shape
    (``pp_session.live_api_missing``, captured v24_live_api_missing_id on
    2.4.0 — no 2.2 / 2.3 capture of that call exists). Live's own word that
    the object is gone, raised before the receipt parser would call the
    reply malformed."""

    def __init__(self, path):
        self.path = path
        super().__init__('Live has no object at %r (ppal-live-api replied id "0" and no '
                         'results)' % (path,))


async def _live_api_response(session, path: str | None, operations: list[dict]) -> tuple:
    """One ppal-live-api call: ``(object path Live reports, receipts)``;
    ``LiveApiObjectMissing`` for the missing-object reply."""
    if not session.capabilities.get("live_api"):
        raise LiveSetIdentityError(
            "live_set identity", 0, None,
            detail="ppal-live-api capability is unavailable",
        )
    payload = pp_session.build_live_api_request(path, operations)
    response = await session.call("ppal-live-api", payload)
    if pp_session.live_api_missing(response):
        raise LiveApiObjectMissing(path)
    result = pp_session._result(response)
    object_path = result.get("path") if isinstance(result, dict) else None
    return (object_path if isinstance(object_path, str) else None,
            pp_session.extract_live_api_receipts(response))


async def _live_api_results(session, path: str | None, operations: list[dict]) -> list:
    return (await _live_api_response(session, path, operations))[1]


# A clip that sits on a regular track: an Arrangement clip or a Session slot
# clip. Anything else an id names now (a device, a track, a take-lane clip,
# a scene) is not one — Live reuses object ids within a run.
CLIP_OBJECT_PATH = re.compile(r"^live_set tracks (\d+) (?:arrangement_clips \d+|clip_slots \d+ clip)$")


def clip_object_track(object_path: str | None) -> int | None:
    """The regular-track index of a clip object path, else None."""
    match = CLIP_OBJECT_PATH.match(str(object_path or ""))
    return int(match.group(1)) if match else None


def _has_live_api(session) -> bool:
    """ppal-live-api is on for this session (older test doubles carry no capabilities)."""
    return bool((getattr(session, "capabilities", None) or {}).get("live_api"))


async def read_clip_live(session, clip_id) -> dict:
    """One clip read completely through ppal-live-api (path ``id <clip id>``):
    its timing properties and EVERY note (Producer Pal's own clip reads return
    notes only inside [-L, 2L)). One request per clip.

    Returns ``{"clip_id", "props", "frame", "notes", "object_path"}`` (frame/
    notes in quarter beats, see mcp_server.clip_playback; ``object_path`` the
    Live path the id names, e.g. ``live_set tracks 3 arrangement_clips 0``),
    or ``{"clip_id", "error", …}`` when the read fails — callers treat that
    clip as unread, never as the Producer Pal window. Live reuses object ids
    within a run: an id that now names something that is not a clip on a
    track comes back as an error with ``mismatch: True`` and its
    ``object_path``; an id Live says does not exist carries ``missing: True``
    — the device's "id … does not exist" text for this id, or on 2.4 the
    live-api missing-object reply itself (``LiveApiObjectMissing``,
    v24_live_api_missing_id).
    """
    import clip_playback
    clip_id = str(clip_id)
    object_path = None
    try:
        object_path, receipts = await _live_api_response(
            session, "id " + clip_id, clip_playback.live_clip_operations())
        if (isinstance(object_path, str) and object_path.startswith("live_set ")
                and clip_object_track(object_path) is None):
            # Live named the object and it is not a clip on a track (a device,
            # a track, a take-lane clip …); a reply without a canonical path
            # is left to the caller — the experiments reader treats it as
            # unverified, hear_the_room as before.
            return {"clip_id": clip_id, "mismatch": True, "object_path": object_path,
                    "error": "id %s names %s, not a clip on a track" % (clip_id, object_path)}
        props, notes = {}, None
        for receipt in receipts:
            operation = receipt["operation"]
            if operation.get("type") == "getProperty":
                props[operation["property"]] = receipt["result"]
            elif operation.get("method") == "get_notes_extended":
                payload = receipt["result"]
                payload = json.loads(payload) if isinstance(payload, str) else payload
                notes = [clip_playback.note_from_live(n) for n in payload["notes"]]
        missing = [p for p in clip_playback.LIVE_CLIP_PROPERTIES if p not in props]
        if missing or notes is None:
            raise ValueError("incomplete Live API clip read (missing %s)"
                             % ", ".join(missing + ([] if notes is not None else ["notes"])))
        frame = clip_playback.frame_from_live(props)
        if None in (frame["loop_start"], frame["loop_end"], frame["start_marker"],
                    frame["end_marker"]):
            raise ValueError("Live API clip read has no loop/marker times")
    except Exception as exc:  # the caller marks the clip unread and says why
        out = {"clip_id": clip_id, "error": "%s: %s" % (type(exc).__name__, exc)}
        if isinstance(exc, LiveApiObjectMissing) or pp_session.is_missing_id_error(
                str(exc), clip_id):
            out["missing"] = True
        if object_path:
            out["object_path"] = object_path
        return out
    return {"clip_id": clip_id, "props": props, "frame": frame, "notes": notes,
            "object_path": object_path}


async def _read_live_set_identity_fields(session: pp_session.PPSession) -> tuple[str, str | None]:
    """Read Live root id and file_path in one ppal-live-api round trip."""
    payload = pp_session.build_live_api_request("live_set", [
        {"type": "getProperty", "property": "file_path"},
    ])
    response = await session.call("ppal-live-api", payload)
    live_root_id = _live_api_root_id(response)
    receipts = pp_session.extract_live_api_receipts(response)
    file_path = receipts[0]["result"]
    if file_path is not None and not isinstance(file_path, str):
        raise LiveSetIdentityError(
            "live_set identity", 0, None,
            detail="live_set.file_path returned a non-string result",
        )
    normalized = (file_path or "").strip() or None
    return live_root_id, normalized


async def read_set_identity(session: pp_session.PPSession | None = None) -> dict:
    """Return verified ``set_identity`` for the open Live Set.

    Combines the Live root id from ppal-live-api with a verified local Live
    process incarnation. Raises on identity failure — never silently inherits
    a prior ledger session or per-request token.
    """
    if session is None:
        async with pp_session.operation_session(PP_URL) as owned:
            return await read_set_identity(owned)
    try:
        live_root_id, normalized_path = await _read_live_set_identity_fields(session)
        incarnation = pp_session.resolve_live_process_incarnation()
        runtime_epoch = _build_runtime_epoch(live_root_id, incarnation)
    except (pp_session.PPProtocolError, RuntimeError, ValueError) as exc:
        raise LiveSetIdentityError(
            "live_set identity", 0, None,
            detail="Live Set identity unavailable: %s" % exc,
        ) from exc
    identity = _build_set_identity(normalized_path, runtime_epoch)
    identity["live_root_id"] = live_root_id
    if hasattr(session, "cache_set_identity"):
        session.cache_set_identity(identity)
    return identity


read_set_identity_in_session = read_set_identity


async def scoring_readiness(session: pp_session.PPSession | None = None) -> dict:
    """Probe whether writes made NOW can be scored at session close.

    Pure readiness report — deliberately not wired into any tool. Scoring
    needs Producer Pal 2.2 with the Direct Live API (ppal-live-api) so set
    identity is verifiable; ``set_saved`` then distinguishes a named Set
    from an unsaved one (its identity is transient, but ready stays True).
    Never raises: any failure becomes ``ready False`` with a reason naming
    the failing capability — only a failed connection reads "unreachable".
    """
    if session is None:
        probe = None
        try:
            async with pp_session.operation_session(PP_URL) as owned:
                # compute inside the block and return after it, so a teardown
                # error can never replace a completed probe result
                probe = await scoring_readiness(owned)
        except pp_session.PPProtocolError as exc:
            if probe is not None:  # teardown noise after a completed probe
                return probe
            return _handshake_probe_failure(exc)
        except Exception as exc:  # connect/transport failure only — the probe is total
            if probe is not None:  # teardown noise after a completed probe
                return probe
            # validate_handshake's PPProtocolError surfaces through the MCP
            # client's anyio TaskGroup wrapped in a single-member
            # ExceptionGroup, which never matches the clause above — unwrap
            # before classifying so a rejected handshake is not misreported
            # as unreachable
            from mcp_server.capture_loop import _single_group_error
            cause = _single_group_error(exc)
            if isinstance(cause, pp_session.PPProtocolError):
                return _handshake_probe_failure(cause)
            return {
                "pp_version": pp_session.PP_VERSION_UNKNOWN,
                "live_api": False, "set_saved": False, "identity": None,
                "ready": False, "reason": "Producer Pal unreachable",
            }
        return probe
    handshake = getattr(session, "handshake", None) or {}
    capabilities = getattr(session, "capabilities", None) or {}
    out = {
        "pp_version": handshake.get("pp_version", pp_session.PP_VERSION_UNKNOWN),
        "live_api": bool(capabilities.get("live_api")),
        "set_saved": False, "identity": None, "ready": False, "reason": None,
    }
    if not capabilities.get("supports_2_2"):
        out["reason"] = ("Producer Pal 2.2 is required for scoring-ready "
                         "writes (this Set reports %s)" % out["pp_version"])
        return out
    if not out["live_api"]:
        out["reason"] = ("ppal-live-api (Direct Live API) is unavailable — "
                         "enable Direct Live API for this Set in Producer Pal")
        return out
    try:
        identity = await read_set_identity(session)
    except Exception as exc:  # noqa: BLE001 — a probe reports, never raises
        out["reason"] = "Live Set identity unavailable: %s" % exc
        return out
    out["identity"] = identity
    out["set_saved"] = not str(identity.get("set_id") or "").startswith("unsaved:")
    out["ready"] = True
    return out


def _same_set_identity(expected: dict | None, actual: dict) -> bool:
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return False
    for key in ("set_id", "runtime_epoch"):
        value = expected.get(key)
        if not value or value != actual.get(key):
            return False
    return True


def _require_delete_identity(expected_clip_id, expected_track_id,
                             expected_set_identity) -> dict | None:
    if not expected_clip_id:
        return {
            "deleted": False,
            "reason": "missing expected_clip_id — legacy identity unresolved",
        }
    if not expected_track_id:
        return {
            "deleted": False,
            "reason": "missing expected_track_id — legacy identity unresolved",
        }
    if not isinstance(expected_set_identity, dict):
        return {
            "deleted": False,
            "reason": "missing expected_set_identity — legacy identity unresolved",
        }
    for key in ("set_id", "runtime_epoch"):
        if not expected_set_identity.get(key):
            return {
                "deleted": False,
                "reason": "missing expected_set_identity.%s" % key,
            }
    return None


def _tracks_named(response, name: str) -> list[dict]:
    tracks = pp_session._collection(pp_session._result(response), ("tracks",))
    return [track for track in tracks
            if isinstance(track, dict) and track.get("name") == name]


def _resolve_named_track(overview, track_name: str, expected_track_id: str) -> dict:
    matches = _tracks_named(overview, track_name)
    if not matches:
        raise ValueError("no '%s' track in this Set" % track_name)
    by_id = [track for track in matches if track.get("id") == expected_track_id]
    if len(by_id) == 1:
        return by_id[0]
    if len(matches) > 1:
        raise ValueError(
            "ambiguous '%s' tracks (%d) — expected_track_id %r required"
            % (track_name, len(matches), expected_track_id)
        )
    if matches[0].get("id") != expected_track_id:
        raise ValueError(
            "refusing: expected track id %r != '%s' track id %r"
            % (expected_track_id, track_name, matches[0].get("id"))
        )
    return matches[0]


def _inventory_include_flags(include: list[str] | tuple[str, ...]) -> list[str]:
    unknown = set(include) - TRACK_READ_INCLUDE_INVENTORY
    if unknown:
        raise ValueError("unsupported read-track include flags: %s" % sorted(unknown))
    return list(include)


def _validate_live_set_overview(overview) -> tuple[list[dict], bool, list[str]]:
    """Conservative live-set overview gate before a definitive inventory sweep.

    Reuses PP partial/truncation warning tokens. When ``regularTrackCount`` is
    present it must agree with all regular tracks (including groups). Duplicate
    track indices or ids also mark the overview incomplete.
    """
    errors: list[str] = []
    complete = True
    for item in pp_session.response_warnings_mean_incomplete(overview):
        complete = False
        errors.append("live-set overview: %s" % item)
    tracks = pp_session._collection(pp_session._result(overview), ("tracks",))
    if not isinstance(tracks, list):
        return [], False, errors + ["live-set track listing unavailable"]
    if not tracks:
        return [], False, errors + ["track inventory unavailable"]
    seen_indices: set[int] = set()
    seen_ids: set[str] = set()
    regular_count = 0
    for track in tracks:
        if not isinstance(track, dict):
            complete = False
            errors.append("malformed track entry in live-set overview")
            continue
        regular_count += 1
        raw_index = track.get("trackIndex")
        if raw_index is not None and (isinstance(raw_index, bool)
                                      or not isinstance(raw_index, int) or raw_index < 0):
            complete = False
            errors.append(
                "live-set overview trackIndex %r is not an integer" % raw_index)
        # 2.2 trackIndex, 2.3 path 't3' (pp_session.track_index_of)
        idx = pp_session.track_index_of(track)
        if idx is not None:
            if idx in seen_indices:
                complete = False
                errors.append(
                    "duplicate trackIndex %s in live-set overview" % idx)
            seen_indices.add(idx)
        track_id = track.get("id")
        if isinstance(track_id, str) and track_id:
            if track_id in seen_ids:
                complete = False
                errors.append(
                    "duplicate track id %r in live-set overview" % track_id)
            seen_ids.add(track_id)
    declared = pp_session.find_int(overview, "regularTrackCount")
    if declared is not None and declared != regular_count:
        complete = False
        errors.append(
            "live-set overview lists %d regular track(s) but regularTrackCount is %d"
            % (regular_count, declared))
    return tracks, complete, errors


def _clip_inventory_from_track(track: dict, listing: pp_session.PPResponse,
                              *, surface: str) -> tuple[list[dict], bool, list[str]]:
    errors: list[str] = []
    track_index = pp_session.track_index_of(track)
    track_id = track.get("id")
    track_name = str(track.get("name") or "")
    result = pp_session._result(listing)
    warning_issues = pp_session.response_warnings_mean_incomplete(listing)
    if warning_issues:
        errors.extend("track %s: %s" % (track_index, item) for item in warning_issues)
    if surface == "session":
        inventory = pp_session.session_clips_inventory(listing)
        if not inventory["complete"]:
            errors.append("track %s session clip listing incomplete" % track_index)
            return [], False, errors
        clips_raw = [item["clip"] for item in inventory["clips"]]
        if not isinstance(clips_raw, list):
            errors.append("track %s session clip collection is not a list" % track_index)
            return [], False, errors
    else:
        if not pp_session._clip_keys_present(result, ARRANGEMENT_CLIP_KEYS):
            errors.append("track %s arrangement clip listing missing" % track_index)
            return [], False, errors
        clips_raw = pp_session._collection(result, ARRANGEMENT_CLIP_KEYS)
        if not isinstance(clips_raw, list):
            errors.append("track %s arrangement clip collection is not a list" % track_index)
            return [], False, errors
    out: list[dict] = []
    complete = not warning_issues
    for clip in clips_raw:
        if not isinstance(clip, dict):
            complete = False
            errors.append("track %s malformed clip entry" % track_index)
            continue
        if not clip.get("id"):
            complete = False
            errors.append("track %s clip missing id" % track_index)
            continue
        path = clip.get("path")
        if isinstance(path, str) and pp_codec.is_lane_object_path(path):
            continue
        if (surface == "arrangement" and path is None
                and isinstance(result, dict) and track_id
                and result.get("id") == track_id
                and pp_session.track_index_of(result) == track_index):
            # PP 2.2 read-track nests Arrangement clips under their verified
            # parent; unlike read-clip, those children omit the track path.
            clip = {**clip, "path": "t%d" % track_index}
        notes = None
        if surface == "session" or clip.get("type") != "audio":
            notes = pp_session.extract_notes(
                pp_session.PPResponse(json.dumps({"result": clip}), clip, []))
        ref = _clip_ref_from_record(
            clip, track_id=str(track_id or ""), track_name=track_name,
            surface=surface, notes=notes)
        if ref is None:
            complete = False
            errors.append("track %s clip %r missing locatable path"
                          % (track_index, clip.get("id")))
            continue
        out.append(ref)
    return out, complete, errors


async def read_set_inventory(session: pp_session.PPSession | None = None) -> dict:
    """Inventory every regular-track Session and Arrangement clip, or mark incomplete."""
    if session is None:
        async with pp_session.operation_session(PP_URL) as owned:
            return await read_set_inventory(owned)
    set_identity = await read_set_identity(session)
    errors: list[str] = []
    clips: list[dict] = []
    complete = True
    overview = await session.call("ppal-read-live-set", {"include": ["tracks"]})
    tracks, overview_complete, overview_errors = _validate_live_set_overview(overview)
    errors.extend(overview_errors)
    complete = overview_complete
    if not overview_complete:
        final_identity = await read_set_identity(session)
        if not _same_set_identity(set_identity, final_identity):
            errors.append("set identity changed during inventory read")
            set_identity = final_identity
        return {"set_identity": set_identity, "complete": False, "clips": [],
                "errors": errors}
    expected_tracks = 0
    for track in tracks:
        if not isinstance(track, dict):
            complete = False
            errors.append("malformed track entry in live-set overview")
            continue
        if track.get("isGroup"):
            continue
        expected_tracks += 1
        track_index = pp_session.track_index_of(track)
        if track_index is None:
            complete = False
            errors.append("track entry missing trackIndex")
            continue
        try:
            listing = await session.call(
                "ppal-read-track", pp_session.requests_for(session).read_track(
                    track_index,
                    include=_inventory_include_flags(TRACK_READ_INCLUDE_INVENTORY)),
            )
        except Exception as exc:
            complete = False
            errors.append("track %s read failed: %s" % (track_index, exc))
            continue
        for surface in ("session", "arrangement"):
            found, surface_complete, surface_errors = _clip_inventory_from_track(
                track, listing, surface=surface)
            clips.extend(found)
            if not surface_complete:
                complete = False
            errors.extend(surface_errors)
    if expected_tracks == 0:
        complete = False
        errors.append("no regular tracks accounted for")
    final_identity = await read_set_identity(session)
    if not _same_set_identity(set_identity, final_identity):
        complete = False
        errors.append("set identity changed during inventory read")
        set_identity = final_identity
    return {"set_identity": set_identity, "complete": complete,
            "clips": clips, "errors": errors}


def _overview_track_states(tracks: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    """A validated overview by id: ``(tracks, groups)`` — name, index,
    mute/solo (and the raw ``state`` string, so a state no capture ever
    showed is seen for what it is) and the enclosing group (``groupId``) of
    every regular track; group tracks separately, so a muted or soloed
    parent can silence or carry its members (experiments._audibility)."""
    regular: dict[str, dict] = {}
    groups: dict[str, dict] = {}
    for track in tracks:
        if not isinstance(track, dict):
            continue
        track_id = track.get("id")
        if not isinstance(track_id, str) or not track_id:
            continue
        state = track.get("state")
        group_id = track.get("groupId")
        record = {"id": track_id, "name": str(track.get("name") or ""),
                  "index": pp_session.track_index_of(track),
                  "muted": state == "muted", "soloed": state == "soloed",
                  "state": state if isinstance(state, str) else None,
                  "group_id": str(group_id) if group_id else None}
        (groups if track.get("isGroup") else regular)[track_id] = record
    return regular, groups


def _device_params(result) -> dict:
    """``{"params": {name: value}, "coverage": full|partial|none, "error"}``
    from a read-device reply. Shape: the 2026-09-29 live read of the
    sidechain compressors (``parameters`` [{name, value}]) — no capture
    exists, so ``params`` is accepted as an alias. Coverage says how much of
    the reply was readable: ``full`` when every listed item is a {name,
    value} record, ``partial`` when some are not, ``none`` when the field is
    missing, empty or not a list — never an empty map passed off as "read"."""
    if not isinstance(result, dict):
        return {"params": {}, "coverage": "none", "error": "no device object in the reply"}
    listed = result.get("parameters")
    if not isinstance(listed, list):
        listed = result.get("params")
    if not isinstance(listed, list):
        found = result.get("parameters", result.get("params"))
        return {"params": {}, "coverage": "none",
                "error": "parameters is %s, expected a list of {name, value}"
                         % ("missing" if found is None else type(found).__name__)}
    out: dict = {}
    unreadable = 0
    for item in listed:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            out[item["name"]] = item.get("value")
        else:
            unreadable += 1
    if not out:
        return {"params": {}, "coverage": "none",
                "error": ("no parameters in the reply" if not listed else
                          "%d parameter item(s) are not {name, value} records" % unreadable)}
    if unreadable:
        return {"params": out, "coverage": "partial",
                "error": "%d of %d parameter item(s) are not {name, value} records"
                         % (unreadable, len(listed))}
    return {"params": out, "coverage": "full", "error": None}


async def read_experiment_evidence(*, clip_ids=(), device_tracks: dict | None = None,
                                   track_names=(),
                                   session: pp_session.PPSession | None = None,
                                   inventory: dict | None = None) -> dict:
    """Everything registration and close need about some candidates, read
    once: verified Set identity (before and after), the track overview
    (mute states), the complete clip inventory (where every clip is), a
    complete Live API read of each named clip that is present, and — for
    device trials — each owner track's device list plus each device's
    parameters. ``track_names`` are the rows' registered track names: every
    arrangement clip on a track of that name is read completely and that
    track's device list is fetched, so a row from another Live run (ids
    renumbered) can still be matched by content. ``inventory`` may be a
    ``read_set_inventory`` envelope already read this close (its identity
    must match). Never writes.

    ``complete`` is False whenever any part is partial; the pure resolver
    (``experiments``) then keeps every row unresolved.
    """
    if session is None:
        async with pp_session.operation_session(PP_URL) as owned:
            return await read_experiment_evidence(
                clip_ids=clip_ids, device_tracks=device_tracks, track_names=track_names,
                session=owned, inventory=inventory)
    device_tracks = dict(device_tracks or {})
    wanted_names = {str(name) for name in track_names or ()}
    errors: list[str] = []
    identity = await read_set_identity(session)
    overview = await session.call("ppal-read-live-set", {"include": ["tracks"]})
    tracks, complete, overview_errors = _validate_live_set_overview(overview)
    errors.extend(overview_errors)
    track_states, group_states = _overview_track_states(tracks) if complete else ({}, {})
    named_tracks = {tid for tid, track in track_states.items() if track["name"] in wanted_names}
    if inventory is None:
        inventory = await read_set_inventory(session)
    if inventory.get("complete") is not True:
        complete = False
        errors.extend(str(e) for e in inventory.get("errors") or [])
        if not inventory.get("errors"):
            errors.append("clip inventory incomplete")
    if not _same_set_identity(identity, inventory.get("set_identity")):
        complete = False
        errors.append("set identity differs between the inventory and this read")
    locations: dict[str, dict] = {}
    for clip in inventory.get("clips") or []:
        if isinstance(clip, dict) and clip.get("clip_id"):
            locations[str(clip["clip_id"])] = {
                "track_id": str(clip.get("track_id") or ""), "track": clip.get("track"),
                "surface": clip.get("surface"), "path": clip.get("path")}
    wanted_clips = {str(c) for c in clip_ids}
    wanted_clips.update(cid for cid, loc in locations.items()
                        if loc["track_id"] in named_tracks and loc["surface"] == "arrangement")
    # Every named id is read, listed in the inventory or not: an id the
    # inventory lacks may still name a live object (Live reuses ids within
    # a run) — only Live's own "does not exist" makes it gone.
    clips: dict[str, dict] = {}
    for clip_id in sorted(wanted_clips):
        clips[clip_id] = await read_clip_live(session, clip_id)
    requests = pp_session.requests_for(session)
    # A track's device list is labelled with the requested id only when the
    # reply is that track: its own id equals the requested one and its path
    # is the index the overview gave that id. A reply about another track
    # (ids are reused within a run) leaves the list unread, with the
    # contradiction named — never a list under the wrong label.
    track_devices: dict[str, list | None] = {}
    track_device_errors: dict[str, str] = {}
    for track_id in sorted(set(device_tracks.values()) | named_tracks):
        try:
            listing = await session.call(
                "ppal-read-track", requests.read_track(track_id=track_id, include=["devices"]))
            if pp_session.response_warnings_mean_incomplete(listing):
                raise ValueError("partial device listing")
            result = pp_session._result(listing)
            if not isinstance(result, dict):
                raise ValueError("evidence_mismatch:track_shape (no track object in the reply)")
            answered = result.get("id")
            if not isinstance(answered, str) or answered != str(track_id):
                raise ValueError("evidence_mismatch:track_id (asked for id %s, Live answered %s)"
                                 % (track_id, answered if answered is not None else "no id"))
            index = pp_session.track_index_of(result)
            expected = (track_states.get(track_id) or {}).get("index")
            if index is None or (expected is not None and index != expected):
                raise ValueError("evidence_mismatch:track_path (track %s is t%s in the overview, "
                                 "the reply says %s)"
                                 % (track_id, expected, "t%d" % index if index is not None
                                    else "no path"))
            devices = result.get("devices")
            if not isinstance(devices, list):
                raise ValueError("no device list in the track read")
            track_devices[track_id] = [
                {"id": str(d.get("id")), "name": d.get("name"), "type": d.get("type"),
                 "path": d.get("path")} for d in devices if isinstance(d, dict) and d.get("id")]
        except Exception as exc:  # noqa: BLE001 — unread is reported, never guessed
            track_devices[track_id] = None
            track_device_errors[track_id] = str(exc)
            errors.append("track %s devices unread: %s" % (track_id, exc))
    # A by-id device read is recorded under the requested id with what Live
    # answered (id, type, path); a reply naming another id is a contradiction
    # (``mismatch``), like a clip read that lands on another object.
    devices: dict[str, dict] = {}
    for device_id in sorted(device_tracks):
        try:
            response = await session.call(
                "ppal-read-device",
                requests.read_device(device_id, include=["params", "param-values"]))
            # A degraded read (a warning-only reply, a timeout / truncation
            # warning beside a body, a null / empty / list result) is a
            # FAILED read — never Live's word about the device; only its own
            # "id … does not exist" for this id proves absence (below).
            incomplete = pp_session.response_warnings_mean_incomplete(response)
            if incomplete:
                raise ValueError("partial device read: %s" % "; ".join(incomplete))
            result = pp_session._result(response)
            if not isinstance(result, dict) or not result:
                raise ValueError("no device object in the reply (%s)"
                                 % ("empty" if isinstance(result, dict)
                                    else type(result).__name__))
            answered = result.get("id")
            record = {"device_id": device_id,
                      "id": answered if isinstance(answered, str) else None,
                      "name": result.get("name"), "type": result.get("type"),
                      "path": result.get("path")}
            if record["id"] != device_id:
                devices[device_id] = dict(
                    record, mismatch=True,
                    error="evidence_mismatch:device_id (asked for id %s, Live answered %s)"
                          % (device_id, record["id"] or "no id"))
                continue
            params = _device_params(result)
            devices[device_id] = dict(record, params=params["params"],
                                      params_coverage=params["coverage"],
                                      params_error=params["error"])
        except Exception as exc:  # noqa: BLE001
            out = {"device_id": device_id, "error": "%s: %s" % (type(exc).__name__, exc)}
            if pp_session.is_missing_id_error(str(exc), device_id):
                out["missing"] = True        # Live's own word: the id does not exist
            devices[device_id] = out
    final = await read_set_identity(session)
    if not _same_set_identity(identity, final):
        complete = False
        errors.append("set identity changed during the evidence read")
    return {"set_identity": identity, "complete": complete, "errors": errors,
            "tracks": track_states, "groups": group_states, "clip_locations": locations,
            "clips": clips, "track_devices": track_devices,
            "track_device_errors": track_device_errors, "devices": devices}


def _missing_clip_id_error(message: str, clip_id: str) -> bool:
    """Recognize PP's missing-object error only for the requested ID (2.2
    'readClip failed: id "X" does not exist', 2.3 and 2.4 'Error: id "X" does
    not exist' — v24_read_clip_missing_id, the same text)."""
    return pp_session.is_missing_id_error(message, clip_id)


async def _resolve_jam_clip(session, clip_id: str, jam_track: dict) -> dict | None:
    jam_idx = pp_session.track_index_of(jam_track)
    jam_id = jam_track.get("id")
    try:
        raw = await session.call("ppal-read-clip", {
            **pp_session.clip_locator(clip_id=clip_id), "include": []})
    except RuntimeError as exc:
        if _missing_clip_id_error(str(exc), clip_id):
            return None
        raise
    clip = pp_session.clip_record(raw)
    if not isinstance(clip, dict) or clip.get("id") != clip_id:
        return None
    track_index = pp_session.clip_track_index(clip)
    clip_track_id = clip.get("trackId") or clip.get("track_id")
    if jam_id and clip_track_id and clip_track_id != jam_id:
        return None
    if jam_idx is not None and track_index is not None and int(track_index) != int(jam_idx):
        return None
    slot = pp_codec.clip_session_slot(clip)
    return {"clip": clip, "slot": slot, "track_index": track_index}


async def _verified_clip_ref(session, slot: str, notes: list[dict] | None = None,
                             *, verify_response: pp_session.PPResponse | None = None,
                             expected_clip_id: str | None = None) -> dict | None:
    raw = verify_response
    if raw is None:
        track_text, scene_text = slot.split("/", 1)
        raw = await session.call("ppal-read-clip", {
            **pp_session.clip_location_args(int(track_text), int(scene_text)),
            "include": ["notes", "timing"]})
    overview = await session.call("ppal-read-live-set", {"include": ["tracks"]})
    track_index = int(slot.split("/", 1)[0])
    tracks = pp_session._collection(pp_session._result(overview), ("tracks",))
    track = next((t for t in tracks if isinstance(t, dict)
                  and pp_session.track_index_of(t) == track_index), {})
    clip = pp_session.clip_record(raw)
    if expected_clip_id and clip.get("id") != expected_clip_id:
        raise LiveWriteVerificationError(
            "clip_ref", len(notes or []), None,
            detail="readback clip id %r != expected %r"
                   % (clip.get("id"), expected_clip_id),
        )
    read_notes = pp_session.extract_notes(raw) or []
    if notes is not None:
        verification = pp_session.verify_notes(notes, raw, "clip_ref %s" % slot)
        if verification["status"] != "verified":
            raise LiveWriteVerificationError(
                "clip_ref", verification.get("sent"), verification.get("received"),
                detail=verification.get("detail") or verification["status"],
            )
    else:
        read_notes = read_notes or []
    return _clip_ref_from_record(
        clip, track_id=str(track.get("id") or ""),
        track_name=str(track.get("name") or ""),
        surface="session", notes=read_notes)


async def _clip_ref_from_arrangement(session, track_index: int, clip_name: str,
                                     notes: list[dict],
                                     verify_response: pp_session.PPResponse) -> dict | None:
    overview = await session.call("ppal-read-live-set", {"include": ["tracks"]})
    tracks = pp_session._collection(pp_session._result(overview), ("tracks",))
    track = next((t for t in tracks if isinstance(t, dict)
                  and pp_session.track_index_of(t) == track_index), {})
    clip = pp_session.arrangement_clip_record(verify_response, clip_name=clip_name)
    if clip is None:
        raise LiveWriteVerificationError(
            "arrangement clip_ref", len(notes), None,
            detail="arrangement clip %r not uniquely readable" % clip_name,
        )
    verification = pp_session.verify_notes(
        notes,
        pp_session.response_from_notes(
            _extract_arrangement_clip_notes(verify_response, clip_name=clip_name) or []),
        "arrangement %s" % clip_name,
    )
    if verification["status"] != "verified":
        raise LiveWriteVerificationError(
            "arrangement clip_ref", verification.get("sent"), verification.get("received"),
            detail=verification.get("detail") or verification["status"],
        )
    read_notes = _extract_arrangement_clip_notes(verify_response, clip_name=clip_name) or []
    return _clip_ref_from_record(
        clip, track_id=str(track.get("id") or ""),
        track_name=str(track.get("name") or ""),
        surface="arrangement", notes=read_notes)


async def _augment_write_receipt(receipt: dict, session: pp_session.PPSession, *,
                                 slot: str | None = None,
                                 notes: list[dict] | None = None,
                                 verify_response: pp_session.PPResponse | None = None,
                                 expected_clip_id: str | None = None,
                                 arrangement_track_index: int | None = None,
                                 arrangement_clip_name: str | None = None) -> dict:
    out = dict(receipt)
    try:
        out["set_identity"] = await read_set_identity(session)
    except LiveSetIdentityError as exc:
        out["identity_status"] = "identity_error"
        out["identity_error"] = exc.detail or str(exc)
        out["verification_status"] = out.get("verification_status", "verified")
        return out
    try:
        if arrangement_track_index is not None and arrangement_clip_name and notes is not None:
            if verify_response is None:
                raise LiveWriteVerificationError(
                    "arrangement clip_ref", len(notes), None,
                    detail="missing arrangement verify response",
                )
            clip_ref = await _clip_ref_from_arrangement(
                session, arrangement_track_index, arrangement_clip_name, notes,
                verify_response)
        elif slot:
            clip_ref = await _verified_clip_ref(
                session, slot, notes, verify_response=verify_response,
                expected_clip_id=expected_clip_id)
        else:
            clip_ref = None
        if clip_ref:
            out["clip_ref"] = clip_ref
    except LiveWriteVerificationError as exc:
        out["identity_status"] = "accounting_error"
        out["identity_error"] = exc.detail or str(exc)
    return out


def _require_written(response, context: str, *, expect: str = "object", sent: int = 0,
                     completed: list[dict] | None = None, landing: str | None = None,
                     **structure):
    """Fail closed unless the reply's STRUCTURE proves Producer Pal did the
    write (pp_session.write_failed: ids, count, place, noteCount — never the
    words of its warnings). ``landing`` names where the object may already be,
    for the error: a refusal reply does not prove nothing landed."""
    failure = pp_session.write_failed(response, expect=expect, **structure)
    if failure:
        if landing:
            failure += "; the clip may already be at %s — check it before retrying" % landing
        raise LiveWriteVerificationError(context, sent, None, completed, detail=failure)
    return response


_WarningLog = pp_session.WarningLog


async def _append_track(session, context: str, expected_index: int,
                        completed: list[dict] | None = None) -> dict:
    """Append one track (2.2: create-track, trackIndex omitted; 2.3: path
    't+'); ``{"id", "track_index"}`` from the device's reply. The reply must
    name one new track, at the end (``expected_index``) when it names a place.
    When it names another place the track EXISTS but is unplaced: the error
    carries it as ``created_track`` (review 2026-09-30 finding 5) — surfaced,
    reconciled by a retry of a named create, never deleted here."""
    created = await session.call("ppal-create-track",
                                 pp_session.requests_for(session).create_track())
    _require_written(created, context, completed=completed, count=1)
    track = pp_session.created_track(created) or {}
    index = track.get("track_index")
    if index is not None and index != expected_index:
        raise LiveWriteVerificationError(
            context, 0, None, completed,
            detail="Producer Pal created track %s at index %d, expected the end (%d)"
                   % (track.get("id"), index, expected_index),
            created_track={"id": track.get("id"), "track_index": index})
    return {"id": track.get("id"),
            "track_index": expected_index if index is None else index,
            "reply": created}


# Tracks this process created for a named surface (SIDEKICK JAM / BOOTH) but
# could not place — create-track replied an unexpected index — keyed by
# (runtime epoch, name). A retry adopts one instead of creating a second
# track; see _adopt_unplaced_track. In-process only: after a restart the
# error text (which names the id) is the record.
_UNPLACED_TRACKS: dict[tuple[str, str], dict] = {}
# Live's own name for an unnamed track — captured '22-MIDI', '23-Audio'
# (2.2.0-rc1/read/read_live_set_tracks_locators.json).
_LIVE_DEFAULT_TRACK_NAME_RE = re.compile(r"^\d+-(?:MIDI|Audio)$")


def _forget_unplaced_tracks() -> None:
    """Reset hook (tests)."""
    _UNPLACED_TRACKS.clear()


def _track_by_id(overview, track_id: str) -> dict | None:
    tracks = pp_session._collection(pp_session._result(overview), ("tracks",))
    return next((t for t in tracks if isinstance(t, dict) and t.get("id") == track_id), None)


async def _remember_unplaced_track(session, name: str, created: dict) -> None:
    try:
        identity = await read_set_identity(session)
    except Exception:   # noqa: BLE001 — no identity, no adoptable record
        return
    _UNPLACED_TRACKS[(identity["runtime_epoch"], name)] = {
        "id": created.get("id"), "track_index": created.get("track_index"),
        "set_id": identity.get("set_id")}


async def _adopt_unplaced_track(session, name: str, overview) -> dict | None:
    """``{"id", "track_index"}`` of the track an earlier ``name`` create left
    unplaced, if it is still ours: same runtime Set (ids are reused across a
    reopen), present in the fresh overview by id, and named as Live named it
    ('N-MIDI' / 'N-Audio') or already ``name``. Anything else — gone, renamed
    by him, another Set — is forgotten, never deleted."""
    if not _UNPLACED_TRACKS:
        return None
    try:
        identity = await read_set_identity(session)
    except Exception:   # noqa: BLE001 — cannot prove the Set, do not adopt
        return None
    key = (identity["runtime_epoch"], name)
    record = _UNPLACED_TRACKS.get(key)
    if record is None:
        return None
    del _UNPLACED_TRACKS[key]
    found = _track_by_id(overview, record["id"])
    if found is None:
        return None
    track_name = str(found.get("name") or "")
    if track_name != name and not _LIVE_DEFAULT_TRACK_NAME_RE.match(track_name):
        return None
    index = pp_session.track_index_of(found)
    if index is None:
        return None
    return {"id": record["id"], "track_index": index}


async def _require_slot_free(session, track_index: int, scene_index: int, context: str,
                             completed: list[dict] | None = None) -> None:
    """Refuse to create into a session slot not PROVEN empty. Producer Pal
    2.4 REPLACES the clip in an occupied slot and says so only in a
    ``detail`` (v24_create_session_clip_replaces; 2.3 refused), so the slot
    is read first and judged by ``pp_session.slot_read_verdict``: free is
    only a captured empty-slot shape for this very slot (2.2 / 2.3 ``{id:
    null}``, 2.4 "no clip at <path>"); an occupied slot and every
    unrecognised read refuse, visibly (fail closed)."""
    locator = pp_session.clip_locator(track=track_index, scene=scene_index)
    path = locator["path"]
    # the free-slot reply's own "no clip at <path>" notice (2.2) is this
    # read's answer, not a device warning for the receipt
    call = getattr(session, "call_unlogged", None) or session.call
    try:
        verdict = pp_session.slot_read_verdict(
            path, reply=await call("ppal-read-clip", {**locator, "include": []}))
    except RuntimeError as exc:
        verdict = pp_session.slot_read_verdict(path, error=str(exc))
    if verdict["state"] == "free":
        return
    if verdict["state"] == "occupied":
        detail = ("slot %s is occupied by %s — refusing: Producer Pal 2.4 replaces the clip "
                  "in an occupied slot (the listing did not show it)" % (path, verdict["detail"]))
    else:
        detail = ("slot %s could not be proven empty before writing (%s) — refusing: an "
                  "occupied slot would be replaced" % (path, verdict["detail"]))
    raise LiveWriteVerificationError(context, 0, None, completed, detail=detail)


def _refuse_overlapping_placements(placements: list[dict]) -> None:
    """A plan whose placements overlap on one role is refused before any
    write: a clip created or copied over another replaces it, silently
    (pp_codec.find_overlapping_spans; v24_duplicate_over_existing)."""
    by_role: dict[str, list] = {}
    for ordinal, placement in enumerate(placements, 1):
        start = pp_codec.bar_beat_to_beats(str(placement["arrangement_start_bar_beat"]))
        length = max(1, int(placement["length_bars"])) * pp_codec.BEATS_PER_BAR
        by_role.setdefault(str(placement["role"]), []).append(
            ("placement %d at %s" % (ordinal, placement["arrangement_start_bar_beat"]),
             start, start + length))
    for role, spans in by_role.items():
        overlaps = pp_codec.find_overlapping_spans(spans)
        if overlaps:
            raise ValueError(
                "plan placements overlap on role %r (%s) — a clip placed over another "
                "replaces it; fix the plan" % (role, "; ".join(
                    "%s with %s" % pair for pair in overlaps)))


async def _append_scenes(session, count: int, n_scenes: int, context: str) -> int:
    """Append ``count`` empty scenes and prove it by re-reading the scene
    count (create-scene's reply is not a verdict). Returns the new count."""
    await session.call("ppal-create-scene", pp_session.requests_for(session).append_scenes(
        count=count, scene_count=n_scenes))
    now = _require_scene_count(await session.call("ppal-read-live-set", {}))
    if now < n_scenes + count:
        raise LiveWriteVerificationError(
            context, 0, None,
            detail="the Set has %d scenes after appending %d to %d" % (now, count, n_scenes))
    return now


def _require_verified_notes(notes, response, context, completed=None):
    if isinstance(response, pp_session.PPResponse):
        return pp_session.require_verified_notes(notes, response, context, completed)
    old = pp_session.LEGACY_TEXT_RESPONSE_MODE
    pp_session.LEGACY_TEXT_RESPONSE_MODE = True
    try:
        return pp_session.require_verified_notes(notes, response, context, completed)
    finally:
        pp_session.LEGACY_TEXT_RESPONSE_MODE = old


def _extract_arrangement_clip_notes(response, clip_index: int = 0,
                                    clip_name: str | None = None):
    # Keep the old private helper's string result for offline PP<2.0 fixtures;
    # actual MCP responses are PPResponse objects and use structured midi-json.
    if not isinstance(response, pp_session.PPResponse):
        return pp_codec.extract_arrangement_clip_notes(
            str(response), clip_index=clip_index, clip_name=clip_name)
    return pp_session.extract_arrangement_clip_notes(
        response, clip_index=clip_index, clip_name=clip_name)


def _create_clip_arguments(notes: list[dict], **kwargs) -> dict:
    """Build the existing handoff, then select PP 2.1 midi-json at the seam.
    Pass the session's ``addressing`` for an arrangement placement."""
    handoff = core.send_to_ableton(notes, **kwargs)
    args = next(c["arguments"] for c in handoff["tool_calls"]
                if c["tool"] == "ppal-create-clip")
    normalized = pp_codec.normalize_notes_for_pp(pp_codec.note_dicts(notes))
    args["notes"] = pp_codec.encode_notes(
        normalized, pp_codec.CodecMode.MIDI_JSON)
    return args


async def read_jam_clips() -> list[dict]:
    """Every clip currently sitting on the SIDEKICK JAM track.

    The end-of-session sweep's ONE read. Raises if the track can't be resolved or
    Producer Pal is unreachable — the sweep must fail closed, because an empty
    read looks exactly like "he deleted everything" and would silently score a
    whole night as drops."""
    async with pp_session.operation_session(PP_URL) as s:
        overview = await s.call("ppal-read-live-set", {"include": ["tracks"]})
        jam_idx, _jam_id = _track_index_by_name(overview, JAM_TRACK_NAME)
        if jam_idx is None:
            raise LiveWriteVerificationError(
                "sweep", 0, None,
                detail="no '%s' track in this Set — refusing to score a "
                       "session against a track that isn't there"
                       % JAM_TRACK_NAME)
        listing = await s.call("ppal-read-track", pp_session.requests_for(s).read_track(
            jam_idx, include=["session-clips"]))
        return pp_session.parse_session_clips(listing, strict=True)


async def read_clip(
    track_index: int,
    slot: int = 0,
    arrangement: bool = False,
) -> dict:
    """Read a session or arrangement clip via Producer Pal.

    Session reads request ``notes`` only — clip ``type`` is always on the default
    PP2 response, which is enough for Stage A named-but-unheard. Arrangement
    reads stay notes-oriented (arrangement-clips listing).
    """
    async with pp_session.operation_session(PP_URL) as s:
        if arrangement:
            raw = await s.call(
                "ppal-read-track", pp_session.requests_for(s).read_track(
                    track_index, include=["arrangement-clips", "notes", "timing"]),
            )
            all_notes = _extract_arrangement_clip_notes(raw, clip_index=slot) or []
            clip = (pp_session.arrangement_clip_record(raw, clip_index=slot)
                    if isinstance(raw, pp_session.PPResponse) else None) or {}
        else:
            raw = await s.call(
                "ppal-read-clip",
                {**pp_session.clip_locator(track=track_index, scene=slot),
                 "include": ["notes", "timing"]},
            )
            all_notes = _extract_notes(raw) or []
            clip = pp_session.clip_record(raw) if isinstance(raw, pp_session.PPResponse) else {}
        live = None
        if _has_live_api(s) and isinstance(clip, dict) and clip.get("id"):
            live = await read_clip_live(s, clip["id"])

    return _clip_read_result(
        {"source": "arrangement" if arrangement else "session",
         "track_index": track_index, "slot": slot},
        clip, all_notes, raw, presence=classify_session_clip(raw, all_notes), live=live)


def _clip_read_result(base: dict, clip: dict, all_notes: list[dict], raw,
                      presence: str | None = None, raw_limit: int = 2000,
                      live: dict | None = None) -> dict:
    """The shared clip-read envelope: ``notes`` are what PLAYS (inside the loop
    brace); notes past the brace ride separately as ``hidden_notes`` so seeds,
    density and key never count silent material.

    Producer Pal reads a clip's notes only inside [-L, 2L): a clip whose played
    region lies (partly) outside is ``unread`` — ``notes`` and ``hidden_notes``
    are empty and ``unread_reason`` says why — unless ``live`` (the complete
    Live API read, live_write.read_clip_live) supplies every note."""
    import clip_playback
    if isinstance(live, dict) and live.get("error"):
        split = {**pp_session.clip_timing_beats(clip), "audible": [], "hidden": [],
                 "n_hidden": 0, "hidden_span_bars": None, "unread": True, "coverage": None,
                 "notes_source": "live_api",
                 "unread_reason": "the Live API read of this clip failed (%s); Producer "
                                  "Pal's own read covers only [-L, 2L) of the clip"
                                  % live["error"]}
    else:
        split = pp_session.split_audible_notes(clip, all_notes, live=live)
        if live is not None:
            den = live["frame"]["den"]
            split = {**split, "audible": clip_playback.in_clip_meter(split["audible"], den),
                     "hidden": clip_playback.in_clip_meter(split["hidden"], den)}
    notes = split["audible"]
    out = dict(base)
    out.update({
        "clip_name": str(clip.get("name") or "") if isinstance(clip, dict) else "",
        "pp_notes": pp_codec.notes_to_midi_json_payload(notes),
        "notes": notes,
        "n_notes": len(notes),
        "hidden_notes": split["hidden"],
        "n_hidden": split["n_hidden"],
        "hidden_span_bars": split["hidden_span_bars"],
        "loop_start": clip.get("start") if isinstance(clip, dict) else None,
        "loop_end": clip.get("end") if isinstance(clip, dict) else None,
        "loop_start_beats": split["loop_start_beats"],
        "loop_end_beats": split["loop_end_beats"],
        "unread": bool(split.get("unread")),
        "unread_reason": split.get("unread_reason"),
        "coverage": split.get("coverage"),
        "notes_source": split.get("notes_source"),
        "raw": raw[:raw_limit] if isinstance(raw, str) else "",
    })
    if presence is not None:
        out["presence"] = presence
    return out


def notes_or_unread_error(read: dict, missing: str) -> list[dict]:
    """The playing notes of a read_clip / read_current_clip result, or a
    ValueError: the unread reason when Producer Pal could not read the clip's
    notes (its [-L, 2L) window missed the loop brace, Live API off) or this
    clip's Live API read failed (Live API on), else ``missing``."""
    notes = read.get("notes") if isinstance(read, dict) else None
    if notes:
        return notes
    if isinstance(read, dict) and read.get("unread"):
        if read.get("notes_source") == "live_api":
            # the Live API is on; this clip's own read failed
            raise ValueError(
                "the clip's notes were not read: this clip's Live API read failed "
                "(%s) — read it again, or pass seed_notes" % read.get("unread_reason"))
        raise ValueError(
            "the clip's notes were not read: %s — the Live API is off: turn on "
            "ppal-live-api in Producer Pal (it reads every note), or pass seed_notes"
            % read.get("unread_reason"))
    raise ValueError(missing)


async def read_current_clip() -> dict:
    """Read the currently selected Live clip, or the selected track/scene slot.

    Contract proven against Producer Pal 1.4.13:
    - ppal-select with no args returns selectedTrack/selectedScene and sometimes
      selectedClip.
    - ppal-read-clip reads by clipId when selectedClip exists.
    - otherwise ppal-read-clip reads the selected session slot
      selectedTrack.trackIndex/selectedScene.sceneIndex.
    """
    async with pp_session.operation_session(PP_URL) as s:
        selection_raw = await s.call("ppal-select", {})
        selected = _extract_selected_context(selection_raw)
        clip = selected.get("selected_clip") or {}
        track = selected.get("selected_track") or {}
        scene = selected.get("selected_scene") or {}

        read_args: dict = {"include": ["notes", "timing"]}
        source = "selected_clip"
        if clip.get("id"):
            read_args.update(pp_session.clip_locator(clip_id=clip["id"]))
        elif track.get("track_index") is not None and scene.get("scene_index") is not None:
            source = "selected_slot"
            read_args.update(pp_session.clip_locator(
                track=track["track_index"], scene=scene["scene_index"]))
        else:
            raise RuntimeError(f"Producer Pal selection has no readable clip context: {selection_raw}")

        try:
            raw = await s.call("ppal-read-clip", read_args)
            read_error = None
        except Exception as exc:
            raw = ""
            read_error = f"{type(exc).__name__}: {exc}"

        all_notes = (_extract_notes(raw) or []) if read_error is None else []
        clip = (pp_session.clip_record(raw)
                if read_error is None and isinstance(raw, pp_session.PPResponse) else {})
        live = None
        if (read_error is None and _has_live_api(s)
                and isinstance(clip, dict) and clip.get("id")):
            live = await read_clip_live(s, clip["id"])
    return _clip_read_result(
        {"source": source, "selected_context": selected, "read_args": read_args,
         "read_error": read_error},
        clip, all_notes, raw, raw_limit=1000, live=live)


async def write_tracks(tracks: list[dict], tempo: float | None = None,
                       set_tempo: bool = False) -> dict:
    """Create one new MIDI track per entry and place its clip (session slot 0).

    tracks: [{"name": str, "notes": [{pitch,start_beats,duration_beats,velocity}],
              "color": "#RRGGBB"?}]
    Returns a summary with each track's index and verified note count.
    """
    summary = {"tempo": tempo, "tracks": [], "producer_pal": None}
    async with pp_session.operation_session(PP_URL) as owned:
        s = _WarningLog(owned)
        summary["producer_pal"] = dict(s.handshake)
        if set_tempo and tempo:
            await s.call("ppal-update-live-set", {"tempo": float(tempo)})

        base = _regular_track_count(await s.call("ppal-read-live-set", {"include": ["tracks"]}))
        for i, t in enumerate(tracks):
            idx = (await _append_track(s, "track %d create" % (base + i), base + i,
                                       summary["tracks"]))["track_index"]
            cc = _create_clip_arguments(
                t["notes"], track=idx, clip_name=t["name"],
                tempo=tempo or 120.0)
            cc["path"] = pp_session.clip_locator(track=idx, scene=0)["path"]
            if t.get("color"):
                cc["color"] = t["color"]
            sent = core._pp_normalized_note_count(t["notes"])
            created = _require_written(
                await s.call("ppal-create-clip", cc), f"track {idx}/0",
                sent=sent, completed=summary["tracks"], landing="slot %d/0" % idx,
                count=1, location=cc, note_count=sent)
            verify = await s.call("ppal-read-clip",
                                 {**pp_session.clip_locator(track=idx, scene=0),
                                  "include": ["notes"]})
            verified = _require_verified_notes(
                t["notes"], verify, f"track {idx}/0",
                summary["tracks"],
            )
            track_receipt = {
                "name": t["name"],
                "track_index": idx,
                "sent_notes": core._pp_normalized_note_count(t["notes"]),
                "created": created[:120],
                "created_noteCount": _find_int(created, "noteCount"),
                "verified_noteCount": verified,
                "verification_status": "verified",
            }
            summary["tracks"].append(await _augment_write_receipt(
                track_receipt, s, slot=f"{idx}/0", notes=t["notes"],
                verify_response=verify,
                expected_clip_id=pp_session.clip_record(verify).get("id")))
        try:
            summary["set_identity"] = await read_set_identity(s)
        except LiveSetIdentityError as exc:
            summary["identity_status"] = "identity_error"
            summary["identity_error"] = exc.detail or str(exc)
        summary["pp_warnings"] = list(s.warnings)
    return summary


async def _create_named_track(session, name: str, color: str, n_tracks: int,
                              overview=None) -> tuple[int, str | None, str | None]:
    """Append a track and name it; its index comes from the create reply
    (2.2 trackIndex, 2.3 path) and is confirmed by reading it back. Returns
    ``(index, track_id, adopted_id)``: a track an earlier create for ``name``
    left unplaced in this runtime Set is adopted (``_adopt_unplaced_track``,
    ``overview`` needed) instead of a second one being created."""
    requests = pp_session.requests_for(session)
    adopted = (await _adopt_unplaced_track(session, name, overview)
               if overview is not None else None)
    if adopted is not None:
        appended = adopted
    else:
        try:
            appended = await _append_track(session, "%s create" % name, n_tracks)
        except LiveWriteVerificationError as exc:
            created_id = (exc.created_track or {}).get("id")
            if created_id and overview is not None:
                if _track_by_id(overview, created_id) is not None:
                    # a reply naming a track that was in the Set BEFORE the
                    # create: not created by us — never remembered, never
                    # adopted (round 3 hardening: his default-named track)
                    raise LiveWriteVerificationError(
                        exc.failed_context, exc.sent, exc.received, exc.completed,
                        detail="%s; id %s already existed in the Set before the create "
                               "— not created by us, not adopted"
                               % (exc.detail, created_id),
                        created_track=exc.created_track) from exc
                await _remember_unplaced_track(session, name, exc.created_track)
            raise
    index = appended["track_index"]
    raw = await session.call("ppal-read-track", requests.read_track(index, include=[]))
    track_id = _find_str(raw, "id")
    if appended["id"] and track_id and track_id != appended["id"]:
        raise LiveWriteVerificationError(
            "%s create" % name, 0, None,
            detail="track %d reads back as id %r, the create reply said %r"
                   % (index, track_id, appended["id"]))
    track_id = track_id or appended["id"]
    if track_id:
        _require_written(await session.call("ppal-update-track", requests.update_track(
            track_id, name=name, color=color)), "%s rename" % name, count=1, ids=[track_id])
    return index, track_id, (adopted["id"] if adopted is not None else None)


async def write_answer_to_jam_track(notes: list[dict], clip_name: str,
                                    tempo: float | None = None) -> dict:
    """Place an answer clip on the SIDEKICK JAM track — the only auto-write surface.

    Structurally policy-enforcing: no track parameter exists. The track is resolved
    (or created) by NAME, the clip lands in its next free session slot, scenes are
    extended when the slot row doesn't exist yet, and the clip is read back to verify.
    The receipt's ``pp_warnings`` lists what the device warned (advisory).
    """
    async with pp_session.operation_session(PP_URL) as owned:
        s = _WarningLog(owned)
        requests = pp_session.requests_for(s)
        overview = await s.call("ppal-read-live-set", {"include": ["tracks"]})
        n_tracks = _regular_track_count(overview)
        n_scenes = _require_scene_count(overview)

        jam_idx, jam_id = _track_index_by_name(overview, JAM_TRACK_NAME)
        track_created = False
        track_adopted = None
        if jam_idx is None:
            jam_idx, jam_id, track_adopted = await _create_named_track(
                s, JAM_TRACK_NAME, JAM_TRACK_COLOR, n_tracks, overview)
            track_created = True

        listing = await s.call("ppal-read-track", requests.read_track(
            jam_idx, include=["session-clips"]))
        top = _max_scene_index_in_track_text(listing)
        next_slot = 0 if top is None else top + 1
        if next_slot >= n_scenes:
            await _append_scenes(s, next_slot - n_scenes + 1, n_scenes,
                                 "SIDEKICK JAM scene create")

        cc = _create_clip_arguments(
            notes, track=jam_idx, clip_name=clip_name, tempo=tempo or 120.0)
        cc["path"] = pp_session.clip_locator(track=jam_idx, scene=next_slot)["path"]
        cc["color"] = JAM_TRACK_COLOR
        sent = core._pp_normalized_note_count(notes)
        context = f"SIDEKICK JAM {jam_idx}/{next_slot}"
        landing = f"SIDEKICK JAM slot {jam_idx}/{next_slot}"
        # the listing chose the slot; the slot itself says whether it is free
        await _require_slot_free(s, jam_idx, next_slot, context)
        created = _require_written(await s.call("ppal-create-clip", cc), context,
                                   sent=sent, landing=landing, count=1, location=cc,
                                   note_count=sent)
        verify = await s.call("ppal-read-clip",
                             {**pp_session.clip_locator(track=jam_idx, scene=next_slot),
                              "include": ["notes"]})
        try:
            verified = _require_verified_notes(notes, verify, context)
        except LiveWriteVerificationError as exc:
            # The create already happened: say where the clip may sit and what
            # the device warned, so a retry does not stack a second copy.
            said = ("; the device said: %s" % " | ".join(s.warnings)) if s.warnings else ""
            raise LiveWriteVerificationError(
                context, exc.sent, exc.received, exc.completed,
                detail="%s%s; the clip may already be at %s — check it before retrying"
                       % (exc.detail or "read-back mismatch", said, landing)) from exc
        # Attention guidance only — focus the clip we just verified so Live's
        # session view is looking at it. Fail-open: a selection hiccup must
        # never fail or delay a write that's already verified on disk.
        try:
            await s.call("ppal-select",
                         requests.select(track=jam_idx, scene=next_slot, view="session"))
            focused = True
        except Exception:
            focused = False
        receipt = {
            "track_index": jam_idx,
            "track_created": track_created,
            **({"track_adopted": track_adopted} if track_adopted else {}),
            "slot": f"{jam_idx}/{next_slot}",
            "clip_name": clip_name,
            "sent_notes": core._pp_normalized_note_count(notes),
            "created_noteCount": _find_int(created, "noteCount"),
            "verified_noteCount": verified,
            "verification_status": "verified",
            "verified_ok": True,
            "focused": focused,
            "producer_pal": dict(s.handshake),
        }
        receipt = await _augment_write_receipt(
            receipt, s, slot=f"{jam_idx}/{next_slot}", notes=notes,
            verify_response=verify,
            expected_clip_id=pp_session.clip_record(verify).get("id"))
        receipt["pp_warnings"] = list(s.warnings)
        return receipt


async def stage_named_clips(clips: list[dict], track_name: str = BOOTH_TRACK_NAME,
                            color: str = BOOTH_TRACK_COLOR) -> dict:
    """Append blind-listen clips to the named track, one per session slot.

    clips: [{"name": str, "notes": [...], "tempo": float?, "color": "#RRGGBB"?}]
    Track is resolved (or created) by NAME like the jam track; clips land in
    consecutive slots after the track's last used row; scenes are extended as
    needed. Structurally append-only — no track parameter, no deletes.
    """
    async with pp_session.operation_session(PP_URL) as owned:
        s = _WarningLog(owned)
        requests = pp_session.requests_for(s)
        overview = await s.call("ppal-read-live-set", {"include": ["tracks"]})
        n_tracks = _regular_track_count(overview)
        n_scenes = _require_scene_count(overview)

        idx, tid = _track_index_by_name(overview, track_name)
        track_created = False
        track_adopted = None
        if idx is None:
            idx, tid, track_adopted = await _create_named_track(
                s, track_name, color, n_tracks, overview)
            track_created = True

        listing = await s.call("ppal-read-track", requests.read_track(
            idx, include=["session-clips"]))
        top = _max_scene_index_in_track_text(listing)
        start = 0 if top is None else top + 1
        need = start + len(clips)
        if need > n_scenes:
            await _append_scenes(s, need - n_scenes, n_scenes, "%s scene create" % track_name)

        placed = []
        for i, c in enumerate(clips):
            context = f"{track_name} {idx}/{start + i}"
            try:
                cc = _create_clip_arguments(
                    c["notes"], track=idx, clip_name=c["name"],
                    tempo=float(c.get("tempo") or 120.0))
                cc["path"] = pp_session.clip_locator(
                    track=idx, scene=start + i)["path"]
                cc["color"] = c.get("color") or color
                sent = core._pp_normalized_note_count(c["notes"])
                await _require_slot_free(s, idx, start + i, context, placed)
                _require_written(await s.call("ppal-create-clip", cc), context,
                                 sent=sent, completed=placed,
                                 landing="%s slot %d/%d" % (track_name, idx, start + i),
                                 count=1, location=cc, note_count=sent)
                verify = await s.call("ppal-read-clip",
                                     {**pp_session.clip_locator(path=cc["path"]),
                                      "include": ["notes"]})
                got = _require_verified_notes(
                    c["notes"], verify, context, placed,
                )
            except LiveWriteVerificationError:
                raise
            except Exception as exc:
                _raise_partial_write(
                    context, core._pp_normalized_note_count(c["notes"]), None, placed,
                    "%s: %s" % (type(exc).__name__, exc),
                )
            placed.append(await _augment_write_receipt({
                "slot": f"{idx}/{start + i}", "name": c["name"],
                "sent_notes": core._pp_normalized_note_count(c["notes"]),
                "verified_noteCount": got, "verification_status": "verified",
                "ok": True}, s, slot=f"{idx}/{start + i}", notes=c["notes"],
                verify_response=verify,
                expected_clip_id=pp_session.clip_record(verify).get("id")))
        booth_summary = {"track_index": idx, "track_created": track_created,
                **({"track_adopted": track_adopted} if track_adopted else {}),
                "start_slot": start, "placed": placed,
                "verified_ok": True, "n_failed_verify": 0,
                "producer_pal": dict(s.handshake)}
        try:
            booth_summary["set_identity"] = await read_set_identity(s)
        except LiveSetIdentityError as exc:
            booth_summary["identity_status"] = "identity_error"
            booth_summary["identity_error"] = exc.detail or str(exc)
        booth_summary["pp_warnings"] = list(s.warnings)
        return booth_summary


def _arrangement_clip_name(role: str, placement: dict, ordinal: int) -> str:
    """Unique deterministic identity used for arrangement create/read-back."""
    section = placement.get("section_index")
    section_text = "?" if section is None else str(section)
    start = str(placement.get("arrangement_start_bar_beat") or "?")
    return f"{role} §{section_text} @{start} #{ordinal:03d}"


async def write_arrangement(
    plan: dict,
    tempo: float | None = None,
    set_tempo: bool = False,
) -> dict:
    """Create one MIDI track per role and place arrangement clips from arrange_song().

    plan: output of core.arrange_song() — sections + placements with notes per role.
  Returns track indices and verified note counts for a sample of placements.
    """
    placements = plan.get("placements") or []
    if not placements:
        raise ValueError("plan has no placements")
    _refuse_overlapping_placements(placements)

    song_tempo = tempo or plan.get("tempo") or 120.0
    roles = sorted({p["role"] for p in placements})
    summary: dict = {
        "tempo": song_tempo,
        "total_bars": plan.get("total_bars"),
        "roles": roles,
        "tracks": {},
        "placements_written": 0,
        "verified_placements": [],
        "producer_pal": None,
    }

    async with pp_session.operation_session(PP_URL) as owned:
        s = _WarningLog(owned)
        requests = pp_session.requests_for(s)
        summary["producer_pal"] = dict(s.handshake)
        if set_tempo and song_tempo:
            await s.call("ppal-update-live-set", {"tempo": float(song_tempo)})

        base = _regular_track_count(await s.call("ppal-read-live-set", {"include": ["tracks"]}))
        role_track: dict[str, int] = {}
        for i, role in enumerate(roles):
            idx = (await _append_track(s, "%s track create" % role, base + i))["track_index"]
            role_track[role] = idx
            summary["tracks"][role] = {"track_index": idx, "name": role}

        for placement_ordinal, p in enumerate(placements, 1):
            role = p["role"]
            idx = role_track[role]
            placement_name = _arrangement_clip_name(role, p, placement_ordinal)
            cc = _create_clip_arguments(
                p["notes"], track=idx, clip_name=placement_name,
                tempo=song_tempo, bar_length=p["length_bars"],
                arrangement_start=p["arrangement_start_bar_beat"],
                addressing=requests.addressing)
            sent = core._pp_normalized_note_count(p["notes"])
            created = _require_written(
                await s.call("ppal-create-clip", cc), f"arrangement {placement_name}",
                sent=sent, completed=summary["verified_placements"],
                landing="track %d at %s" % (idx, p["arrangement_start_bar_beat"]),
                count=1, location=cc, note_count=sent)
            tr = summary["tracks"][role]
            if "clips_created" not in tr:
                tr["clips_created"] = 0
                tr["sent_notes_total"] = 0
            # Clip notes come back only with "notes" in the include (captured:
            # legacy/check_read_after_arrangement_create.json lists none without it).
            verify = await s.call(
                "ppal-read-track", requests.read_track(
                    idx, include=["arrangement-clips", "notes"]),
            )
            arrangement_notes = _extract_arrangement_clip_notes(
                verify, clip_name=placement_name)
            if isinstance(verify, pp_session.PPResponse):
                verify_for_clip = (
                    pp_session.response_from_notes(arrangement_notes)
                    if arrangement_notes is not None else verify
                )
            else:
                verify_for_clip = f'notes:"{arrangement_notes or ""}"'
            verified = _require_verified_notes(
                p["notes"], verify_for_clip,
                f"arrangement {placement_name}", summary["verified_placements"],
            )
            tr["clips_created"] += 1
            tr["sent_notes_total"] += core._pp_normalized_note_count(p["notes"])
            tr["verified_noteCount"] = tr.get("verified_noteCount", 0) + verified
            tr["read_back"] = verify[:200]
            summary["placements_written"] += 1
            placement_receipt = {
                "name": placement_name,
                "track_index": idx,
                "arrangement_start": p["arrangement_start_bar_beat"],
                "sent_notes": core._pp_normalized_note_count(p["notes"]),
                "verification_status": "verified",
            }
            summary["verified_placements"].append(await _augment_write_receipt(
                placement_receipt, s,
                arrangement_track_index=idx,
                arrangement_clip_name=placement_name,
                notes=p["notes"],
                verify_response=verify,
            ))
            if "last_created" not in tr:
                tr["last_created"] = created[:120]
                tr["created_noteCount"] = _find_int(created, "noteCount")

        try:
            summary["set_identity"] = await read_set_identity(s)
        except LiveSetIdentityError as exc:
            summary["identity_status"] = "identity_error"
            summary["identity_error"] = exc.detail or str(exc)
        summary["pp_warnings"] = list(s.warnings)
    return summary


def _clip_presence_from_readback(readback, expected_clip_id: str) -> str:
    """present | unknown for one read-clip-by-id readback. Never ``absent``:
    a non-error ``{id: null, name: null, path, type: null}`` reply was only
    ever captured for a slot read by PATH (check_read_clip_existing_empty_slot
    .json), so by id it proves nothing about this clip (round 3 blocker 1)."""
    token = _clip_id_token(readback)
    if token == expected_clip_id:
        return "present"
    if token in (None, "null", ""):
        return "unknown"
    if token and token != expected_clip_id:
        return "present"
    return "unknown"


async def _jam_session_inventory(session, jam_track: dict) -> dict:
    """The JAM track's session clip ids, read BY ID (both schemas take it)
    and confirmed to be that track's listing — never the track that happens
    to sit at JAM's old index now (round 3 blocker 2). ``complete`` is False
    with a ``reason`` when the listing is another track's or partial."""
    jam_id = jam_track.get("id")
    if not jam_id:
        return {"complete": False, "clip_ids": [], "listing": None,
                "reason": "post-delete JAM inventory unread: the JAM track has no id"}
    listing = await session.call(
        "ppal-read-track", pp_session.requests_for(session).read_track(
            track_id=str(jam_id),
            include=_inventory_include_flags(TRACK_READ_INCLUDE_SESSION)),
    )
    record = pp_session._result(listing)
    listed_id = record.get("id") if isinstance(record, dict) else None
    if listed_id != jam_id:
        return {"complete": False, "clip_ids": [], "listing": listing,
                "reason": "post-delete JAM inventory is not SIDEKICK JAM's: read-track "
                          "by id %r replied track %r" % (jam_id, listed_id)}
    if pp_session.response_warnings_mean_incomplete(listing):
        return {"complete": False, "clip_ids": [], "listing": listing,
                "reason": "post-delete JAM inventory incomplete"}
    inventory = pp_session.session_clips_inventory(listing)
    clip_ids = []
    if inventory["complete"]:
        # ids compare as strings: a listing carrying an int id must not read
        # as "absent" against a str clip id (review round 3 follow-up)
        clip_ids = [str(item["clip"].get("id")) for item in inventory["clips"]
                    if isinstance(item.get("clip"), dict) and item["clip"].get("id") is not None]
    return {"complete": inventory["complete"], "clip_ids": clip_ids, "listing": listing}


async def _verify_clip_absent(session, clip_id: str, jam_track: dict,
                              set_identity_before: dict) -> dict:
    """present | absent | unknown for ``clip_id`` after a delete.

    Absence has exactly two proofs (review 2026-09-30 finding 1, round 3):
      (a) the device's missing-object reply for THIS id — 2.2 'readClip
          failed: id "X" does not exist', 2.3 and 2.4 'Error: id "X" does not
          exist' (legacy_read_clip_missing_id.json of each build,
          v24_read_clip_missing_id; ``pp_session.is_missing_id_error``);
      (b) a complete JAM inventory, read by the JAM track's id and confirmed
          to be that track's, that does not list the id.
    The delete receipt never decides: 2.4's ``{deletedPath, id}`` and
    ``{detail: "nothing to delete", id}`` (``pp_session.delete_succeeded``)
    are kept as evidence beside the readback, like 2.3's ``deleted`` flag.
    Any other readback — a failure whose text happens to say "no clip", an
    empty-slot error for another address, another id's missing reply, a
    non-error ``{id: null}`` reply — decides nothing: it is recorded and (b)
    is consulted; without a complete same-track inventory the outcome is
    ``unknown``, never absent.
    """
    current = await read_set_identity(session)
    if not _same_set_identity(set_identity_before, current):
        return {
            "status": "unknown",
            "reason": "set identity changed during delete verification",
            "set_identity": current,
        }
    read_error = None
    try:
        readback = await session.call("ppal-read-clip", {
            **pp_session.clip_locator(clip_id=clip_id), "include": []})
    except RuntimeError as exc:
        if _missing_clip_id_error(str(exc), clip_id):
            return {"status": "absent", "method": "missing_id_reply",
                    "set_identity": current}
        read_error = "delete readback error: %s" % exc
    else:
        if _clip_presence_from_readback(readback, clip_id) == "present":
            return {
                "status": "present",
                "readback": pp_session.clip_record(readback),
                "set_identity": current,
            }
    # the readback did not decide: proof (b) or unknown
    inventory = await _jam_session_inventory(session, jam_track)
    if not inventory["complete"]:
        return {
            "status": "unknown",
            "reason": "%s%s" % (read_error + "; " if read_error else "",
                                inventory.get("reason") or
                                "post-delete JAM inventory incomplete"),
            "delete_receipt_only": True,
            "set_identity": current,
        }
    if str(clip_id) in inventory["clip_ids"]:
        return {
            "status": "present",
            "method": "jam_inventory",
            "set_identity": current,
        }
    return {
        "status": "absent",
        "method": "jam_inventory",
        "set_identity": current,
    }


async def delete_jam_clip(
    slot: str,
    *,
    expected_clip_id: str | None = None,
    expected_track_id: str | None = None,
    expected_set_identity: dict | None = None,
) -> dict:
    """DROP's teeth: delete one verified SIDEKICK JAM clip by explicit identity.

    Refuses mutation without reliable explicit identity, resolves the clip's
    current location within the same runtime Set, verifies JAM ownership and
    post-delete absence. Never deletes by slot occupancy, name, or fingerprint.
    """
    refused = _require_delete_identity(
        expected_clip_id, expected_track_id, expected_set_identity)
    if refused is not None:
        refused["slot"] = slot
        return refused
    async with pp_session.operation_session(PP_URL) as s:
        set_identity = await read_set_identity(s)
        if not _same_set_identity(expected_set_identity, set_identity):
            raise ValueError(
                "refusing: expected Set identity %r != current %r"
                % (expected_set_identity, set_identity))
        overview = await s.call("ppal-read-live-set", {"include": ["tracks"]})
        jam_track = _resolve_named_track(overview, JAM_TRACK_NAME, expected_track_id)
        pre_delete_identity = await read_set_identity(s)
        if not _same_set_identity(set_identity, pre_delete_identity):
            raise ValueError(
                "refusing: Set identity changed before delete mutation "
                "(%r -> %r)" % (set_identity, pre_delete_identity)
            )
        located = await _resolve_jam_clip(s, expected_clip_id, jam_track)
        if located is None:
            return {
                "deleted": False,
                "reason": "clip %s not found on SIDEKICK JAM in this Set"
                          % expected_clip_id,
                "slot": slot,
                "set_identity": pre_delete_identity,
            }
        if located["clip"].get("id") != expected_clip_id:
            return {
                "deleted": False,
                "reason": "resolved clip id mismatch",
                "slot": slot,
                "set_identity": pre_delete_identity,
            }
        if not _same_set_identity(pre_delete_identity, await read_set_identity(s)):
            raise ValueError("refusing: Set changed while resolving the deletion target")
        delete_resp = await s.call("ppal-delete", pp_session.requests_for(s).delete(
            "clip", ids=[expected_clip_id]))
        receipt = _extract_delete_receipt(delete_resp)
        # The readback decides (review M2): PROVEN absence — the exact
        # missing-id reply or a complete JAM inventory without the id
        # (_verify_clip_absent) — means deleted, whatever the receipt says; a
        # receipt that disagrees is kept as evidence. A readback that failed
        # any other way proves nothing, and the refused receipt stays the
        # verdict (review 2026-09-30 finding 1).
        disagreed = pp_session.write_failed(delete_resp, expect="deleted",
                                            ids=[expected_clip_id])
        after = await _verify_clip_absent(
            s, expected_clip_id, jam_track, pre_delete_identity)
        if not _same_set_identity(pre_delete_identity, await read_set_identity(s)):
            after = {"status": "unknown", "reason": "Set changed during deletion readback"}
        if disagreed and after["status"] == "unknown":
            after = {**after, "reason": "%s; the delete receipt says: %s"
                     % (after.get("reason") or "delete absence unverified", disagreed)}
        if after["status"] == "present":
            return {
                "deleted": False,
                "reason": "clip still present after delete",
                "slot": located.get("slot") or slot,
                "clip_id": expected_clip_id,
                "delete_receipt": receipt,
                "after_state": after,
                "set_identity": after.get("set_identity", pre_delete_identity),
            }
        if after["status"] == "unknown":
            return {
                "deleted": False,
                "reason": after.get("reason") or "delete absence unverified",
                "slot": located.get("slot") or slot,
                "clip_id": expected_clip_id,
                "delete_receipt": receipt,
                "after_state": after,
                "set_identity": after.get("set_identity", pre_delete_identity),
            }
        deleted = {
            "deleted": True,
            "slot": located.get("slot") or slot,
            "clip_id": expected_clip_id,
            "delete_receipt": receipt,
            "after_state": after,
            "set_identity": after.get("set_identity", pre_delete_identity),
        }
        if disagreed:
            deleted["receipt_disagreed"] = disagreed
        return deleted


_MAP_STATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "store", "last_map_locators.json")


def _write_locator_state(path: str, names: list[str] | None) -> None:
    if names is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    core_shared._save_json_atomic(path, {"names": names})


def _locator_items(response: Any) -> list[dict[str, str]]:
    actual = pp_session._collection(pp_session._result(response), ("locators",))
    if any(not isinstance(item, dict)
           or not isinstance(item.get("name"), str)
           or not isinstance(item.get("time"), str) for item in actual):
        raise LiveWriteVerificationError(
            "song locator inventory", len(actual), 0,
            detail="locator inventory is malformed")
    return [{"name": item["name"], "time": item["time"]} for item in actual]


async def _restore_locator_snapshot(session: Any, snapshot: dict) -> None:
    clear_names = list(snapshot.get("clear_names") or [])
    previous = list(snapshot.get("previous_locators") or [])
    for name in clear_names:
        try:
            await session.call("ppal-update-live-set", {
                "locatorOperation": "delete", "locatorName": name})
        except Exception:
            pass
    for loc in previous:
        await session.call("ppal-update-live-set", {
            "locatorOperation": "create", "locatorTime": loc["time"],
            "locatorName": loc["name"]})
    verify = _locator_items(await session.call(
        "ppal-read-live-set", {"include": ["locators"]}))
    expected = sorted((loc["name"], loc["time"]) for loc in previous)
    actual = sorted((loc["name"], loc["time"]) for loc in verify
                    if loc["name"] in set(clear_names))
    if actual != expected:
        raise LiveWriteVerificationError(
            "song locator rollback", len(expected), len(set(expected) & set(actual)),
            detail="locator rollback readback mismatch")


async def rollback_song_locators(snapshot: dict, *, operation_session=None,
                                 state_path: str | None = None) -> dict:
    """Restore the map-owned locator/state snapshot after a later phase fails."""
    operation_session = operation_session or pp_session.operation_session
    state_path = state_path or _MAP_STATE
    async with operation_session(PP_URL) as session:
        await _restore_locator_snapshot(session, snapshot)
    previous_names = snapshot.get("previous_state_names")
    _write_locator_state(state_path, previous_names if isinstance(previous_names, list) else None)
    return {"restored": True, "locator_count": len(snapshot.get("previous_locators") or [])}


async def write_song_locators(map_plan: dict, replace: bool = True) -> dict:
    """Lay THE MAP's locators on the arrangement timeline. Non-destructive: markers
    only, natively draggable/deletable in Live. replace=True deletes ONLY markers a
    previous lay RECORDED creating (store/last_map_locators.json) plus exact incoming
    names — never a hand-placed marker the map doesn't own."""
    locators = map_plan.get("locators") or []
    if not locators:
        raise ValueError("map has no locators")
    prior: list[str] = []
    try:
        with open(_MAP_STATE) as fh:
            prior = json.load(fh).get("names", [])
    except (OSError, ValueError):
        pass
    cleanup = sorted(set(prior) | {loc["name"] for loc in locators})
    created = []
    async with pp_session.operation_session(PP_URL) as s:
        before = _locator_items(await s.call(
            "ppal-read-live-set", {"include": ["locators"]}))
        snapshot = {
            "clear_names": cleanup,
            "previous_locators": [item for item in before
                                  if item["name"] in set(cleanup)],
            "previous_state_names": prior if os.path.isfile(_MAP_STATE) else None,
        }
        try:
            if replace:
                for name in cleanup:
                    try:
                        await s.call("ppal-update-live-set",
                                    {"locatorOperation": "delete",
                                     "locatorName": name})
                    except Exception:
                        pass                 # nothing to delete on first lay
            for loc in locators:
                # judged by the locator re-read below, not by the reply
                await s.call("ppal-update-live-set",
                             {"locatorOperation": "create",
                              "locatorTime": loc["time"],
                              "locatorName": loc["name"]})
                created.append(loc)
            actual = _locator_items(await s.call(
                "ppal-read-live-set", {"include": ["locators"]}))
            expected_pairs = sorted((item["name"], item["time"]) for item in created)
            actual_pairs = sorted((item["name"], item["time"]) for item in actual
                                  if item["name"] in set(cleanup))
            if actual_pairs != expected_pairs:
                raise LiveWriteVerificationError(
                    "song locators", len(expected_pairs),
                    len(set(expected_pairs) & set(actual_pairs)),
                    detail="locator readback mismatch")
            _write_locator_state(_MAP_STATE, [loc["name"] for loc in created])
        except Exception as exc:
            try:
                await _restore_locator_snapshot(s, snapshot)
                _write_locator_state(_MAP_STATE, snapshot["previous_state_names"])
            except Exception as rollback_exc:
                raise LiveWriteVerificationError(
                    "song locator cleanup", 1, 0,
                    detail="locator cleanup could not be verified") from rollback_exc
            raise
    return {"created": len(created), "locators": created,
            "verification_status": "verified", "_rollback": snapshot}


scale_from_overview = lambda response, *a, **k: _legacy_fixture_call(  # noqa: E731
    pp_session.scale_from_overview, response, *a, **k)


async def selected_arrangement_bar() -> int | None:
    """The bar of the arrangement clip currently selected in Live (1-based), or
    None when nothing is selected there. Producer Pal exposes no playhead; the
    selected clip is the honest anchor for "the room where I am"."""
    async with pp_session.operation_session(PP_URL) as s:
        raw = await s.call("ppal-select", {})
    result = pp_session._result(raw)
    clip = result.get("selectedClip") if isinstance(result, dict) else None
    # 2.2 arrangementStart, 2.3 the position in the clip path 't3[5|1]'
    start = pp_session.arrangement_start_of(clip)
    if not isinstance(start, str) or "|" not in start:
        return None
    try:
        return int(pp_codec.bar_beat_to_beats(start) // pp_codec.BEATS_PER_BAR) + 1
    except (ValueError, TypeError):
        return None


async def read_arrangement_room(track_indices, at_beats: float) -> list[dict]:
    """The arrangement clips playing at ``at_beats`` (0-based beats) on the given
    tracks, notes included for MIDI clips. One listing per track, then one
    read-clip per overlapping clip only. Audio clips come back as
    presence='unheard_audio' with no notes — named, not musically heard."""
    import moment as _moment_mod
    bar = int(at_beats // pp_codec.BEATS_PER_BAR) + 1
    out: list[dict] = []
    async with pp_session.operation_session(PP_URL) as s:
        for idx in track_indices:
            raw = await s.call("ppal-read-track", pp_session.requests_for(s).read_track(
                idx, include=["arrangement-clips", "timing"]))
            result = pp_session._result(raw)
            track_type = result.get("type") if isinstance(result, dict) else None
            clips = pp_session._collection(
                result, ("arrangementClips", "arrangement-clips", "arrangement_clips"))
            for clip in _moment_mod.select_arrangement_clips(clips, bar):
                cid = clip.get("id")
                name = str(clip.get("name") or "")
                if track_type == "audio" or clip.get("type") == "audio":
                    out.append({"track_index": idx, "clip_id": cid, "clip_name": name,
                                "notes": [], "raw": json.dumps(clip),
                                "presence": "unheard_audio"})
                    continue
                craw = await s.call("ppal-read-clip", {
                    **pp_session.clip_locator(clip_id=cid), "include": ["notes"]})
                notes = _extract_notes(craw) or []
                out.append({"track_index": idx, "clip_id": cid, "clip_name": name,
                            "notes": notes,
                            "raw": craw[:2000] if isinstance(craw, str) else "",
                            "presence": classify_session_clip(craw, notes)})
    return out


async def read_arrangement_tracks(max_tracks: int | None = None) -> dict:
    """Every regular track's arrangement clips (notes + timing, take lanes
    included) for a producer's reading of the open Set — the raw material
    finisher.project_from_live_tracks turns into a project dict. Audio tracks
    are listed, never read (named, not heard). Groups are skipped. The song
    meter comes from the overview's timeSignature (``meter``/``beats_per_bar``);
    only a Set that reports none is assumed 4/4, and says so."""
    import config as _config
    cap = int(max_tracks or _config.ARRANGEMENT_READ_MAX_TRACKS)
    assumptions: list[str] = []
    async with pp_session.operation_session(PP_URL) as s:
        live_notes = _has_live_api(s)
        # Only ppal-live-api's live_set.file_path is verified identity; the
        # overview's filePath/path is never trusted in its place.
        try:
            _, reported_path = await _read_live_set_identity_fields(s)
            identity_verified = True
        except Exception as exc:
            reported_path = None
            identity_verified = False
            assumptions.append("live Set identity unavailable (%s); finishing needs "
                               "Live API access to verify the Set file" % exc)
        overview = await s.call("ppal-read-live-set", {"include": ["tracks"]})
        result = pp_session._result(overview)
        result = result if isinstance(result, dict) else {}
        set_name = next((str(result[k]) for k in ("name", "setName", "liveSetName", "title")
                         if result.get(k)), "untitled")
        file_path = reported_path
        try:
            tempo = float(result.get("tempo") or 0.0) or 120.0
        except (TypeError, ValueError):
            tempo = 120.0
        import moment as _moment_mod
        signature = result.get("timeSignature")
        if signature is None:
            meter, beats_per_bar = "4/4", 4.0
            assumptions.append("the Set reported no time signature; song meter assumed 4/4 "
                               "for arrangement positions")
        else:
            # A malformed meter raises: bars are never silently counted in 4/4.
            meter, beats_per_bar = _moment_mod.parse_meter(signature)
        tracks_raw = [t for t in pp_session._collection(result, ("tracks",))
                      if isinstance(t, dict) and pp_session.track_index_of(t) is not None
                      and not t.get("isGroup")]
        truncated = None
        if len(tracks_raw) > cap:
            assumptions.append("set has %d regular tracks; read the first %d only"
                               % (len(tracks_raw), cap))
            truncated = {"read": cap, "total": len(tracks_raw)}
            tracks_raw = tracks_raw[:cap]
        out: list[dict] = []
        for t in tracks_raw:
            idx = pp_session.track_index_of(t)
            rec: dict = {"track_index": idx, "name": str(t.get("name") or "track %d" % idx),
                         "type": t.get("type"), "instrument": t.get("instrument"),
                         "arrangement_clips": [], "take_lanes": []}
            if t.get("type") == "audio":
                out.append(rec)
                continue
            raw = await s.call("ppal-read-track", pp_session.requests_for(s).read_track(
                idx, include=["arrangement-clips", "notes", "timing"]))
            r = pp_session._result(raw)
            rec["arrangement_clips"] = [
                c for c in pp_session._collection(
                    r, ("arrangementClips", "arrangement-clips", "arrangement_clips"))
                if isinstance(c, dict)]
            lanes = r.get("takeLanes") if isinstance(r, dict) else None
            rec["take_lanes"] = [ln for ln in (lanes or []) if isinstance(ln, dict)]
            if live_notes:
                # Producer Pal's notes stop at [-L, 2L): every placed clip is
                # re-read whole (finisher.project_from_live_tracks uses "live").
                for clip in rec["arrangement_clips"]:
                    if clip.get("id") is not None:
                        clip["live"] = await read_clip_live(s, clip["id"])
            out.append(rec)
    if not live_notes:
        assumptions.append("ppal-live-api is off: clips whose loop brace lies outside "
                           "Producer Pal's [-L, 2L) note window are unread")
    return {"set_name": set_name, "file_path": file_path, "tempo": tempo,
            "meter": meter, "beats_per_bar": beats_per_bar,
            "identity_verified": identity_verified, "truncated": truncated,
            "tracks": out, "assumptions": assumptions}


async def read_set() -> str:
    """Smoke test / Moment reader: connect and return the live-set overview
    text, with tracks included — callers (e.g. moment.py's _regular_track_count
    sweep bound) rely on the trackIndex entries this produces."""
    async with pp_session.operation_session(PP_URL) as s:
        return await s.call("ppal-read-live-set", {"include": ["tracks"]})


if __name__ == "__main__":
    print("Producer Pal live-set:\n", asyncio.run(read_set())[:400])
