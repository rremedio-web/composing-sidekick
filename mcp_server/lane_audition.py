"""Producer Pal 2.2 take-lane audition and verified KEEP promotion."""
from __future__ import annotations

import os
import re
import secrets
import time
from typing import Any, Callable

from mcp_server import arrangement_exec, core_shared, pp_session


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY_PATH = os.path.join(ROOT, "store", "lane_auditions.json")
MAX_AUDITIONS = 64

_SOURCE_PATH = re.compile(r"^t(?P<track>\d+)/s(?P<scene>\d+)$")
_OPAQUE_ID = re.compile(r"^[ac]-[0-9a-f]{24}$")
_LANE_PATH = re.compile(r"^t(?P<track>\d+)/l(?P<lane>[0-7])(?:\[[^\]]+\])?$")
# An arrangement or take-lane clip location: 2.2 't1' / 't1/l0' (+ arrangementStart),
# 2.3 't1[1|1]' / 't1/l0[1|1]' (the position in the path).
_ARRANGEMENT_LOCATION = re.compile(
    r"t(?P<track>\d+)(?:/l(?P<lane>\d+))?(?:\[(?P<position>[^\]]+)\])?")
_TRANSFORMS = (
    "velocity *= 0.85",
    "velocity *= 1.10",
    "pitch += 12",
    "pitch -= 12",
    "duration *= 0.75",
    "duration *= 1.25",
    "velocity *= 0.70",
    "velocity *= 1.20",
)


class LaneAuditionError(RuntimeError):
    pass


def _validate_request(source_path: str, n: int, seed: int) -> tuple[int, int]:
    if not isinstance(source_path, str) or (match := _SOURCE_PATH.fullmatch(source_path)) is None:
        raise ValueError("source_path must use t<track>/s<scene>")
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 8:
        raise ValueError("n must be an integer from 1 through 8")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    return int(match.group("track")), int(match.group("scene"))


def _objects(response: Any, label: str) -> list[dict[str, Any]]:
    value = pp_session._result(response)
    if isinstance(value, dict) and isinstance(value.get("clips"), list):
        value = value["clips"]
    elif isinstance(value, dict):
        value = [value]
    if (not isinstance(value, list) or not value
            or any(not isinstance(item, dict) for item in value)):
        raise LaneAuditionError("%s response is malformed" % label)
    return value


def _same_location(actual: Any, expected: str) -> bool:
    """Same track (and take lane) whichever build named it, and the same
    position when both paths carry one (2.3 't1[1|1]'); other paths (session
    slots) must match exactly."""
    found = _ARRANGEMENT_LOCATION.fullmatch(actual) if isinstance(actual, str) else None
    wanted = _ARRANGEMENT_LOCATION.fullmatch(expected) if isinstance(expected, str) else None
    if found is None or wanted is None:
        return actual == expected
    if (found.group("track"), found.group("lane")) != (wanted.group("track"),
                                                        wanted.group("lane")):
        return False
    if found.group("position") and wanted.group("position"):
        return (pp_session.clip_location({"path": "t0", "arrangementStart":
                                          found.group("position")})
                == pp_session.clip_location({"path": "t0", "arrangementStart":
                                             wanted.group("position")}))
    return True


def _require_written(response: Any, label: str, **structure) -> Any:
    """Prove a write from the reply's STRUCTURE (pp_session.write_failed), never
    from the words of its warnings."""
    failure = pp_session.write_failed(response, **structure)
    if failure:
        raise LaneAuditionError("%s was not done: %s" % (label, failure))
    return response


def _one_clip(response: Any, expected_path: str, label: str,
              expected_start: str | None = None) -> dict[str, Any]:
    """The one clip a duplicate made, on the expected track/lane and, with
    ``expected_start``, at that position (2.2 arrangementStart, 2.3 the path)."""
    items = _objects(response, label)
    if len(items) != 1:
        raise LaneAuditionError("%s created an unexpected number of clips" % label)
    item = items[0]
    if not isinstance(item.get("id"), str) or not _same_location(item.get("path"), expected_path):
        raise LaneAuditionError("%s landed at an unexpected destination" % label)
    if expected_start is not None:
        start = pp_session.arrangement_start_of(item)
        if start is not None and pp_session.clip_location(
                {"path": "t0", "arrangementStart": start}) != pp_session.clip_location(
                {"path": "t0", "arrangementStart": expected_start}):
            raise LaneAuditionError("%s landed at %s, not at %s"
                                    % (label, start, expected_start))
    return item


def _clip_dict(response: Any, *, clip_id: str, expected_path: str,
               label: str) -> dict[str, Any]:
    value = pp_session._result(response)
    if (not isinstance(value, dict) or value.get("id") != clip_id
            or not _same_location(value.get("path"), expected_path)
            or value.get("type") != "midi"):
        raise LaneAuditionError("%s readback mismatch" % label)
    try:
        notes = pp_session.extract_notes(response)
    except (ValueError, pp_session.PPProtocolError) as exc:
        raise LaneAuditionError("%s note readback is unavailable" % label) from exc
    if notes is None:
        raise LaneAuditionError("%s note readback is unavailable" % label)
    return value


def _empty_registry() -> dict[str, Any]:
    return {"version": 1, "auditions": []}


def _load_registry(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return _empty_registry()
    try:
        data = core_shared._load_json(path)
    except (OSError, ValueError) as exc:
        raise LaneAuditionError("audition registry is unreadable") from exc
    if (not isinstance(data, dict) or data.get("version") != 1
            or not isinstance(data.get("auditions"), list)):
        raise LaneAuditionError("audition registry has an invalid shape")
    return data


def _store_audition(path: str, record: dict[str, Any]) -> None:
    data = _load_registry(path)
    records = [item for item in data["auditions"]
               if isinstance(item, dict) and item.get("audition_id") != record["audition_id"]]
    records.append(record)
    data["auditions"] = records[-MAX_AUDITIONS:]
    core_shared._save_json_atomic(path, data)


def _patch_candidate(path: str, audition_id: str, candidate_id: str,
                     updates: dict[str, Any]) -> None:
    """Re-load the registry and patch ONE candidate.

    Promotion bookkeeping must never write a stale whole-audition snapshot
    back: a concurrent audition in the same registry would lose its state.
    """
    data = _load_registry(path)
    audition = next((item for item in data["auditions"]
                     if isinstance(item, dict) and item.get("audition_id") == audition_id), None)
    if audition is None:
        raise LaneAuditionError("audition_id is unknown")
    candidate = next((item for item in audition.get("candidates", [])
                      if isinstance(item, dict) and item.get("candidate_id") == candidate_id), None)
    if candidate is None:
        raise LaneAuditionError("candidate_id is unknown for this audition")
    candidate.update(updates)
    core_shared._save_json_atomic(path, data)


def _find_candidate(path: str, audition_id: str, candidate_id: str) -> tuple[dict, dict]:
    if (not isinstance(audition_id, str) or _OPAQUE_ID.fullmatch(audition_id) is None
            or not isinstance(candidate_id, str) or _OPAQUE_ID.fullmatch(candidate_id) is None):
        raise ValueError("audition_id and candidate_id must be opaque Sidekick identifiers")
    data = _load_registry(path)
    audition = next((item for item in data["auditions"]
                     if isinstance(item, dict) and item.get("audition_id") == audition_id), None)
    if audition is None:
        raise LaneAuditionError("audition_id is unknown")
    candidate = next((item for item in audition.get("candidates", [])
                      if isinstance(item, dict) and item.get("candidate_id") == candidate_id), None)
    if candidate is None:
        raise LaneAuditionError("candidate_id is unknown for this audition")
    return audition, candidate


def _require_main_lane_is_the_auditions(track_read: dict[str, Any], audition: dict) -> None:
    """A promotion copies the lane clip onto the audition track's main lane,
    where the audition's own main clip (or an earlier promotion) sits — that
    copy REPLACES whatever is there, and Producer Pal does not say so
    (v24_duplicate_over_existing: the clip at the position vanished, the reply
    was ``{id, path}`` only). So the main lane may hold nothing but the
    audition's own clips: the main clip and the candidates' promoted clips.
    Anything else refuses the promotion before any write."""
    _session_clips, arrangement_clips = arrangement_exec._require_clip_lists(track_read)
    own = {audition.get("main_clip_id")}
    own.update(item.get("promoted_clip_id") for item in audition.get("candidates") or []
               if isinstance(item, dict))
    foreign = [clip for clip in arrangement_clips
               if not isinstance(clip, dict) or clip.get("id") not in own]
    if foreign:
        first = foreign[0] if isinstance(foreign[0], dict) else {}
        raise LaneAuditionError(
            "audition track main lane holds clip %s at %s that is not the audition's — a "
            "promotion would replace it; move it off the SIDEKICK AUD track first" % (
                first.get("id") or "?",
                pp_session.arrangement_start_of(first) or first.get("path") or "?"))


def _unsupported(session: Any) -> dict[str, Any] | None:
    if session.capabilities.get("supports_2_2"):
        return None
    return {
        "status": "unsupported_capability",
        "pp_version": session.handshake.get("pp_version", "unknown"),
        "required": "Producer Pal 2.2",
        "writes": 0,
    }


def _lanes_unsupported(session: Any) -> dict[str, Any] | None:
    """Auditioning in take lanes stays on 2.2.0-rc1 for now. On 2.3 and 2.4
    lanes CAN be written (captured v23b / v24: create-clip 't10/l0[5|1]'),
    but a take-lane clip cannot be deleted through the API (2.3: {deleted:
    false} + WARNING; 2.4: the MCP error "cannot delete take-lane clip … via
    the API; remove it in Live's UI", 2.4.0/v23b/v23b_delete_lane_clip),
    which breaks the audition's cleanup of candidates it made."""
    if pp_session.addressing_of(session) == pp_session.ADDRESSING_22:
        return None
    return {
        "status": "unsupported_capability",
        "pp_version": session.handshake.get("pp_version", "unknown"),
        "required": "Producer Pal 2.2 take lanes",
        "reason": ("take lanes on Producer Pal 2.3 / 2.4 can be written, but a take-lane "
                   "clip cannot be deleted through the API (Producer Pal: remove it in "
                   "Live's UI), so an audition could not clean up its candidates"),
        "writes": 0,
    }


async def audition_in_lanes(source_path: str, n: int = 3, seed: int = 0,
                            confirm: bool = False, *,
                            operation_session: Callable | None = None,
                            registry_path: str = REGISTRY_PATH) -> dict[str, Any]:
    """Preview or create one fresh Sidekick track with MIDI take-lane candidates."""
    source_index, _scene_index = _validate_request(source_path, n, seed)
    operation_session = operation_session or pp_session.operation_session
    async with operation_session() as owned:
        session = pp_session.WarningLog(owned)
        if (unsupported := _unsupported(session) or _lanes_unsupported(session)) is not None:
            return unsupported
        requests = pp_session.requests_for(session)

        overview = arrangement_exec._dict(await session.call(
            "ppal-read-live-set", {"include": ["tracks"]}), "Live Set")
        tracks = overview.get("tracks")
        if not isinstance(tracks, list):
            raise LaneAuditionError("Live track inventory is unavailable")
        source_summary = next((item for item in tracks if isinstance(item, dict)
                               and pp_session.track_index_of(item) == source_index), None)
        if not isinstance(source_summary, dict) or not isinstance(source_summary.get("id"), str):
            raise LaneAuditionError("source track is unavailable")

        source_track_response = await arrangement_exec._read_track(session, source_index)
        source_fingerprint = arrangement_exec._fingerprint(source_track_response)
        source_state = arrangement_exec._duplicate_state(source_track_response)
        source_clip_response = await session.call(
            "ppal-read-clip", {**requests.clip(path=source_path), "include": ["notes"]})
        source_clip = pp_session._result(source_clip_response)
        if (not isinstance(source_clip, dict) or not isinstance(source_clip.get("id"), str)
                or source_clip.get("path") != source_path or source_clip.get("type") != "midi"):
            raise LaneAuditionError("source_path must name a MIDI session clip")
        source_notes = pp_session.extract_notes(source_clip_response)
        if not source_notes:
            raise LaneAuditionError("source MIDI clip has no readable notes")

        offset = seed % len(_TRANSFORMS)
        transforms = [_TRANSFORMS[(offset + index) % len(_TRANSFORMS)] for index in range(n)]
        if not confirm:
            return {
                "status": "preview", "source_path": source_path,
                "candidate_count": n, "candidate_transforms": transforms,
                "fresh_track": True, "writes": 0,
                "envelopes_preserved": False,
            }

        run_id = secrets.token_hex(4)
        audition_id = "a-" + secrets.token_hex(12)
        track_name = "SIDEKICK AUD %s" % run_id
        created_track_id = None
        created_track_index = None
        try:
            duplicate_reply = await session.call("ppal-duplicate", requests.duplicate_track(
                source_summary["id"], count=1, name=track_name, withoutClips=True,
                withoutDevices=False))
            duplicate = arrangement_exec._dict(duplicate_reply, "track duplicate")
            created_track_id = duplicate.get("id")
            _require_written(duplicate_reply, "audition track duplicate", count=1)
            created_track_index = pp_session.track_index_of(duplicate)
            existing_ids = {item.get("id") for item in tracks if isinstance(item, dict)}
            if (not isinstance(created_track_id, str) or created_track_id in existing_ids
                    or isinstance(created_track_index, bool)
                    or not isinstance(created_track_index, int)):
                raise LaneAuditionError("fresh audition track identity is unavailable")
            track_path = "t%d" % created_track_index
            readback = arrangement_exec._dict(
                await arrangement_exec._read_track(session, created_track_index),
                "audition track")
            if (readback.get("id") != created_track_id
                    or pp_session.track_index_of(readback) != created_track_index
                    or readback.get("name") != track_name):
                raise LaneAuditionError("fresh audition track readback mismatch")
            if arrangement_exec._duplicate_state(readback) != source_state:
                raise LaneAuditionError("fresh audition track lost device or mixer state")
            session_clips, arrangement_clips = arrangement_exec._require_clip_lists(readback)
            if session_clips or arrangement_clips:
                raise LaneAuditionError("fresh audition track contains clips")

            main_result = _require_written(await session.call(
                "ppal-duplicate", requests.duplicate_clip_to_arrangement(
                    source_clip["id"], track=created_track_index, start="1|1")),
                "main-lane duplicate", count=1)
            main = _one_clip(main_result, track_path, "main-lane duplicate", "1|1")
            main_read = await session.call(
                "ppal-read-clip", {"id": main["id"], "include": ["notes"]})
            _clip_dict(main_read, clip_id=main["id"], expected_path=track_path,
                       label="main-lane clip")
            pp_session.require_verified_notes(
                source_notes, main_read, "audition main-lane duplicate")

            candidates = []
            lane_paths = set()
            for index, transform in enumerate(transforms):
                lane_result = _require_written(await session.call(
                    "ppal-duplicate", requests.duplicate_clip_to_new_lane(
                        source_clip["id"], track=created_track_index, start="1|1",
                        lane_name="SIDEKICK CAND %d" % (index + 1), transforms=transform)),
                    "take-lane duplicate", count=1)
                lane_items = _objects(lane_result, "take-lane duplicate")
                if len(lane_items) != 1:
                    raise LaneAuditionError("take-lane duplicate created an unexpected number of clips")
                lane = lane_items[0]
                lane_path = lane.get("path")
                match = _LANE_PATH.fullmatch(lane_path) if isinstance(lane_path, str) else None
                if (match is None or int(match.group("track")) != created_track_index
                        or lane_path in lane_paths or not isinstance(lane.get("id"), str)):
                    raise LaneAuditionError("take-lane duplicate landed at an unexpected destination")
                lane_paths.add(lane_path)
                lane_read = await session.call(
                    "ppal-read-clip", {"id": lane["id"], "include": ["notes"]})
                _clip_dict(lane_read, clip_id=lane["id"], expected_path=lane_path,
                           label="take-lane clip")
                candidates.append({
                    "candidate_id": "c-" + secrets.token_hex(12),
                    "clip_id": lane["id"], "lane_path": lane_path,
                    "lane_name": "SIDEKICK CAND %d" % (index + 1),
                    "transform": transform,
                    # PP may provide explicit context on the source track/clip;
                    # preserve it for promotion, but never invent a lead role.
                    "role": source_summary.get("role") or source_clip.get("role"),
                    "key": source_summary.get("key") or source_clip.get("key"),
                })

            if arrangement_exec._fingerprint(
                    await arrangement_exec._read_track(session, source_index)) != source_fingerprint:
                raise LaneAuditionError("source track changed during lane audition")

            record = {
                "audition_id": audition_id, "created_at": int(time.time()),
                "pp_version": session.handshake.get("pp_version", "unknown"),
                "source_path": source_path, "track_id": created_track_id,
                "track_index": created_track_index, "track_name": track_name,
                "main_clip_id": main["id"], "candidates": candidates,
            }
            _store_audition(registry_path, record)
        except Exception as exc:
            orphans = []
            if (isinstance(created_track_id, str)
                    and created_track_id not in {
                        item.get("id") for item in tracks if isinstance(item, dict)}):
                orphans = await arrangement_exec.cleanup_fresh_tracks(
                    session, [created_track_id])
            if orphans:
                raise LaneAuditionError(
                    "lane audition failed and cleanup left a fresh Sidekick track: %s"
                    % orphans[0]) from exc
            raise

    return {
        "status": "verified", "audition_id": audition_id,
        "track_id": created_track_id, "track_path": "t%d" % created_track_index,
        "track_name": track_name, "main_clip_id": main["id"],
        "candidates": [{"candidate_id": item["candidate_id"],
                        "lane_name": item["lane_name"]} for item in candidates],
        "source_unchanged": True, "envelopes_preserved": False,
        "undo": {"tool": "ppal-delete", "arguments": requests.delete(
            "track", ids=[created_track_id])},
        "pp_warnings": list(session.warnings),
    }


_CLIP_REF_IDENTITY_KEYS = ("clip_id", "track_id", "surface")


def _same_clip_ref(stored: dict | None, new: dict | None) -> bool:
    """Same durable Live clip (ledger's adoption stable key), or not."""
    if not isinstance(stored, dict) or not isinstance(new, dict):
        return False
    return all(stored.get(key) == new.get(key) for key in _CLIP_REF_IDENTITY_KEYS)


def _record_keep(*, clip_id: str, clip_name: str, track_name: str,
                 track_path: str, audition_id: str, candidate_id: str,
                 n_notes: int, lane_notes: list[dict] | None = None,
                 role: str | None = None, key: str | None = None,
                 set_identity: dict | None = None,
                 clip_ref: dict | None = None) -> dict[str, Any]:
    from mcp_server import ledger

    data = ledger.load()
    existing = next((entry for entry in data.get("entries", [])
                     if entry.get("id") == candidate_id or
                     entry.get("suggestion_id") == candidate_id), None)
    if existing is not None:
        # a first attempt may have stamped the entry and died before the
        # verdict — record what is actually missing instead of reporting a
        # KEEP that never landed
        verdict = existing.get("immediate_verdict")
        if verdict is None:
            stored = ledger.record_verdict("keep", entry_id=existing["id"])
            if stored is None or stored.get("immediate_verdict") != "keep":
                raise LaneAuditionError("verified promotion could not be recorded as KEEP")
            verdict = stored["immediate_verdict"]
        adoption = None
        if (set_identity and clip_ref
                and not _same_clip_ref(existing.get("clip_ref"), clip_ref)):
            linked = ledger.adopt_entry(existing["id"], clip_ref,
                                        set_identity=set_identity)
            adoption = linked.get("adoption_links", [])[-1]
        return {"entry_id": existing["id"], "session_id": existing.get("session_id"),
                "verdict": verdict, "adopted": adoption is not None,
                "adoption": adoption}

    entry = ledger.record_write(
        clip_name=clip_name, slot=track_path, source="catalogue", role=role,
        track=track_name, source_phrase_id=candidate_id, n_notes=n_notes,
        answer_notes=lane_notes, key=key, set_identity=set_identity,
        clip_ref=clip_ref, suggestion_id=candidate_id,
    )
    verdict = ledger.record_verdict("keep", entry_id=entry["id"])
    if verdict is None or verdict.get("immediate_verdict") != "keep":
        raise LaneAuditionError("verified promotion could not be recorded as KEEP")
    return {"entry_id": entry["id"], "session_id": entry["session_id"],
            "verdict": "keep"}


async def promote_lane(audition_id: str, candidate_id: str,
                       confirm: bool = False, *,
                       operation_session: Callable | None = None,
                       registry_path: str = REGISTRY_PATH,
                       keep_recorder: Callable | None = None) -> dict[str, Any]:
    """Preview or promote a persisted take-lane candidate, then record verified KEEP."""
    audition, candidate = _find_candidate(registry_path, audition_id, candidate_id)
    if confirm and candidate.get("promoted_clip_id") and candidate.get("entry_id"):
        # the promotion is durable: replay the recorded one instead of
        # duplicating the lane clip into Live a second time — reporting the
        # verdict ACTUALLY stored, never an assumed KEEP
        from mcp_server import ledger

        entry = next((row for row in ledger.load().get("entries", [])
                      if row.get("id") == candidate["entry_id"]), None)
        verdict = (entry or {}).get("immediate_verdict")
        if verdict is None:
            raise LaneAuditionError("replayed promotion has no recorded KEEP verdict")
        if verdict != "keep":
            # the promotion is durable but the stored judgment is not a
            # KEEP (e.g. DROPped afterwards) — report what is stored
            return {
                "status": "already_judged", "audition_id": audition_id,
                "candidate_id": candidate_id,
                "promoted_clip_id": candidate["promoted_clip_id"],
                "verdict": verdict, "keep_recorded": False,
                "keep": {"entry_id": candidate["entry_id"], "verdict": verdict},
                "idempotent": True, "envelopes_preserved": False,
            }
        return {
            "status": "verified_keep", "audition_id": audition_id,
            "candidate_id": candidate_id,
            "promoted_clip_id": candidate["promoted_clip_id"],
            "verification": "verified", "keep_recorded": True,
            "keep": {"entry_id": candidate["entry_id"], "verdict": verdict},
            "idempotent": True, "envelopes_preserved": False,
        }
    operation_session = operation_session or pp_session.operation_session
    keep_recorder = keep_recorder or _record_keep
    registry_error = None
    async with operation_session() as owned:
        session = pp_session.WarningLog(owned)
        if (unsupported := _unsupported(session)) is not None:
            return unsupported
        track_index = audition.get("track_index")
        if isinstance(track_index, bool) or not isinstance(track_index, int):
            raise LaneAuditionError("audition track identity is unavailable")
        track_path = "t%d" % track_index
        track_read = arrangement_exec._dict(
            await arrangement_exec._read_track(session, track_index),
            "audition track")
        if (track_read.get("id") != audition.get("track_id")
                or track_read.get("name") != audition.get("track_name")
                or not str(track_read.get("name", "")).startswith("SIDEKICK AUD ")):
            raise LaneAuditionError("persisted audition track identity no longer matches Live")
        _require_main_lane_is_the_auditions(track_read, audition)
        lane_clip_id = candidate.get("clip_id")
        lane_path = candidate.get("lane_path")
        if not isinstance(lane_clip_id, str) or not isinstance(lane_path, str):
            raise LaneAuditionError("candidate mapping is malformed")
        lane_read = await session.call(
            "ppal-read-clip", {"id": lane_clip_id, "include": ["notes"]})
        lane_clip = _clip_dict(
            lane_read, clip_id=lane_clip_id, expected_path=lane_path,
            label="take-lane source")
        lane_notes = pp_session.extract_notes(lane_read)
        arrangement_start = pp_session.normalize_clip(lane_clip)["arrangement_start"]
        if not isinstance(arrangement_start, str) or not arrangement_start:
            raise LaneAuditionError("take-lane arrangement position is unavailable")
        if not confirm:
            preview = {
                "status": "preview", "audition_id": audition_id,
                "candidate_id": candidate_id, "destination": track_path,
                "writes": 0, "envelopes_preserved": False,
            }
            if candidate.get("promoted_clip_id"):
                # this candidate is (at least partly) promoted already — the
                # preview must not read as a pending first promotion
                preview["already_promoted"] = True
                preview["promoted_clip_id"] = candidate["promoted_clip_id"]
                preview["entry_id"] = candidate.get("entry_id")
            return preview

        set_identity = None
        if session.capabilities.get("live_api"):
            from mcp_server import live_write
            set_identity = await live_write.read_set_identity(session)

        # a first attempt may have verified the promoted clip's readback but
        # died before the KEEP was durable — finish the accounting against
        # the stored clip instead of duplicating the lane into Live again
        resume_clip_id = None
        if isinstance(candidate.get("promoted_clip_id"), str) \
                and not candidate.get("entry_id"):
            resume_clip_id = candidate["promoted_clip_id"]
        promoted_clip_id = resume_clip_id
        if resume_clip_id is None:
            # 2.2: toPath 't1' + arrangementStart; 2.3: toPath 't1[<start>]'
            promoted_result = _require_written(await session.call(
                "ppal-duplicate", pp_session.requests_for(session).duplicate_clip_to_arrangement(
                    lane_clip_id, track=track_index, start=arrangement_start)),
                "lane promotion", count=1)
            promoted = _one_clip(promoted_result, track_path, "lane promotion",
                                 arrangement_start)
            promoted_clip_id = promoted["id"]
        try:
            promoted_read = await session.call(
                "ppal-read-clip", {"id": promoted_clip_id, "include": ["notes"]})
            promoted_clip = _clip_dict(
                promoted_read, clip_id=promoted_clip_id, expected_path=track_path,
                label="promoted main-lane clip")
            pp_session.require_verified_notes(
                lane_notes or [], promoted_read, "take-lane promotion")
        except Exception as exc:
            if resume_clip_id is None:
                raise
            # the stored promoted clip is gone or changed: clear the stale
            # pointer so this cannot wedge forever, and report — the next
            # call takes the normal duplicate path instead of silently
            # duplicating again inside this one
            resume_failed = {
                "status": "resume_failed", "audition_id": audition_id,
                "candidate_id": candidate_id,
                "promoted_clip_id": resume_clip_id,
                "error": "stored promoted clip %s could not be re-verified "
                         "(%s: %s)" % (resume_clip_id, type(exc).__name__, exc),
                "keep_recorded": False, "envelopes_preserved": False,
            }
            try:
                _patch_candidate(registry_path, audition_id, candidate_id,
                                 {"promoted_clip_id": None})
            except Exception as reg_exc:
                resume_failed["registry_error"] = "%s: %s" % (
                    type(reg_exc).__name__, reg_exc)
            return resume_failed

        clip_ref = pp_session.clip_ref_from_record(
            promoted_clip, track_id=str(audition.get("track_id") or ""),
            track_name=str(audition.get("track_name") or "SIDEKICK AUD"),
            surface="arrangement", notes=lane_notes)
        if clip_ref is None:
            raise LaneAuditionError("promotion did not yield a complete verified clip reference")

        if resume_clip_id is None:
            # readback verified: remember the promoted clip BEFORE keep
            # recording so a retry that dies mid-KEEP resumes above instead
            # of duplicating the lane clip again; a registry hiccup here is
            # reported and the KEEP still records — the post-KEEP patch
            # stores promoted_clip_id too, so a retry replays either way
            candidate["promoted_clip_id"] = promoted_clip_id
            try:
                _patch_candidate(registry_path, audition_id, candidate_id,
                                 {"promoted_clip_id": promoted_clip_id})
            except Exception as exc:
                registry_error = "%s: %s" % (type(exc).__name__, exc)

        try:
            keep = keep_recorder(
                clip_id=promoted_clip_id, clip_name=str(promoted_clip.get("name") or lane_clip.get("name") or "SIDEKICK KEEP"),
                track_name=str(audition.get("track_name") or "SIDEKICK AUD"),
                track_path=track_path, audition_id=audition_id,
                candidate_id=candidate_id, n_notes=len(lane_notes or []),
                lane_notes=lane_notes,
                role=candidate.get("role"), key=candidate.get("key"),
                set_identity=set_identity, clip_ref=clip_ref,
            )
        except Exception as exc:
            return {
                "status": "verified_keep_failed", "audition_id": audition_id,
                "candidate_id": candidate_id, "promoted_clip_id": promoted_clip_id,
                "verification": "verified", "keep_recorded": False,
                "error": "ledger KEEP recording failed (%s)" % type(exc).__name__,
                "envelopes_preserved": False,
            }

        # remember the durable promotion so a retried confirm replays it
        # instead of duplicating the lane clip again; the KEEP is already
        # durable, so a registry hiccup is reported, never raised
        candidate["entry_id"] = keep["entry_id"]
        try:
            _patch_candidate(registry_path, audition_id, candidate_id, {
                "promoted_clip_id": promoted_clip_id,
                "entry_id": keep["entry_id"],
                "session_id": keep.get("session_id"),
            })
        except Exception as exc:
            registry_error = "%s: %s" % (type(exc).__name__, exc)

    if keep.get("verdict") != "keep":
        # _record_keep's existing-entry branch found a stored non-keep
        # verdict (e.g. DROPped after promotion) — the promotion verified
        # against Live, but no KEEP was (or may be) recorded for it
        result = {
            "status": "already_judged", "audition_id": audition_id,
            "candidate_id": candidate_id, "promoted_clip_id": promoted_clip_id,
            "verdict": keep.get("verdict"), "keep_recorded": False,
            "keep": keep, "envelopes_preserved": False,
        }
    else:
        result = {
            "status": "verified_keep", "audition_id": audition_id,
            "candidate_id": candidate_id, "promoted_clip_id": promoted_clip_id,
            "verification": "verified", "keep_recorded": True,
            "keep": keep, "envelopes_preserved": False,
        }
    if registry_error is not None:
        result["registry_error"] = registry_error
    result["pp_warnings"] = list(session.warnings)
    return result
