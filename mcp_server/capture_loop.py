"""Safe orchestration for recording the currently playing Live loop."""
from __future__ import annotations

import asyncio
import inspect
import math
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from mcp_server import pp_session
from mcp_server.capture_support import (
    CaptureBridgeClient,
    analyze_captured_audio,
    capture_duration_seconds,
)
from mcp_server.session_ears import _public_set_identity

EARS_TRACK = "SIDEKICK EARS"
SURFACES = frozenset(("auto", "live_api", "bridge"))


class CaptureLoopError(RuntimeError):
    """Path-free capture orchestration failure."""


def _run_async(coro):
    return pp_session.run_coro_blocking(coro)


def _single_group_error(exc: BaseException) -> BaseException:
    """Recover one domain error wrapped by MCP/AnyIO task-group teardown."""
    current = exc
    while isinstance(current, BaseExceptionGroup) and len(current.exceptions) == 1:
        current = current.exceptions[0]
    return current


def _result(response: Any) -> dict[str, Any]:
    value = pp_session._result(response)
    if not isinstance(value, dict):
        raise CaptureLoopError("Producer Pal returned malformed capture state")
    return value


def _overview(response: Any) -> tuple[dict[str, Any], int, int, int, float, str, int]:
    value = _result(response)
    tracks = value.get("tracks")
    if not isinstance(tracks, list):
        raise CaptureLoopError("Live track inventory is unavailable")
    matches = [track for track in tracks if isinstance(track, dict)
               and track.get("name") == EARS_TRACK]
    if len(matches) != 1:
        raise CaptureLoopError("Live must contain exactly one SIDEKICK EARS track")
    track = matches[0]
    index = pp_session.track_index_of(track)       # 2.2 trackIndex, 2.3 path 't3'
    if index is None or index < 0:
        raise CaptureLoopError("SIDEKICK EARS track index is unavailable")
    audio = track.get("type") == "audio" or track.get("hasAudioInput") is True \
        or track.get("has_audio_input") is True
    if not audio:
        raise CaptureLoopError("SIDEKICK EARS must be an audio track")
    scene_count = pp_session.find_int(response, "sceneCount")
    tempo = value.get("tempo")
    signature = value.get("timeSignature")
    if (isinstance(scene_count, bool) or not isinstance(scene_count, int)
            or scene_count <= 0):
        raise CaptureLoopError("Live scene count is unavailable")
    if (isinstance(tempo, bool) or not isinstance(tempo, (int, float))
            or not math.isfinite(float(tempo)) or float(tempo) <= 0):
        raise CaptureLoopError("Live tempo is unavailable")
    if not isinstance(signature, str) or signature.count("/") != 1:
        raise CaptureLoopError("Live time signature is unavailable")
    try:
        numerator, denominator = (int(part) for part in signature.split("/"))
    except ValueError as exc:
        raise CaptureLoopError("Live time signature is unavailable") from exc
    session_id = next((value.get(key) for key in ("setName", "liveSetName", "name")
                       if isinstance(value.get(key), str) and value.get(key).strip()), "current")
    return track, index, scene_count, numerator, float(tempo), str(session_id), denominator


def _empty_slot(listing: Any, track_index: int, scene_count: int) -> int:
    occupied: set[int] = set()
    for clip in pp_session.parse_session_clips(listing):
        try:
            track_text, scene_text = str(clip["slot"]).split("/", 1)
            if int(track_text) == track_index:
                occupied.add(int(scene_text))
        except (KeyError, TypeError, ValueError):
            raise CaptureLoopError("SIDEKICK EARS slot inventory is malformed") from None
    for scene in range(scene_count):
        if scene not in occupied:
            return scene
    raise CaptureLoopError("SIDEKICK EARS has no empty Session slot")


def _bool_result(value: Any, label: str) -> bool:
    if value is True or value == 1:
        return True
    if value is False or value == 0:
        return False
    raise CaptureLoopError("%s readback is malformed" % label)


async def _live_ops(session: Any, path: str, operations: list[dict]) -> list[Any]:
    payload = pp_session.build_live_api_request(path, operations)
    response = await session.call("ppal-live-api", payload)
    response_value = _result(response)
    if response_value.get("path") != path:
        raise CaptureLoopError("Live API response path mismatch")
    receipts = pp_session.extract_live_api_receipts(response)
    if len(receipts) != len(payload["operations"]):
        raise CaptureLoopError("Live API receipt count mismatch")
    for expected, receipt in zip(payload["operations"], receipts):
        if receipt["operation"] != expected:
            raise CaptureLoopError("Live API operation receipt mismatch")
    values = []
    for receipt in receipts:
        value = receipt["result"]
        if (receipt["operation"]["type"] == "get"
                and isinstance(value, (list, tuple)) and len(value) == 1):
            value = value[0]
        values.append(value)
    return values


def _bridge_op(transport: Callable, operation: dict) -> dict[str, Any]:
    try:
        reply = transport([[operation]])
        if (not isinstance(reply, list) or len(reply) != 1
                or not isinstance(reply[0], dict) or reply[0].get("ok") is not True):
            raise ValueError
        results = reply[0].get("results")
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise ValueError
        if "error" in results[0]:
            raise ValueError
        return results[0]
    except Exception:
        raise CaptureLoopError("SIDEKICK BRIDGE operation failed") from None


def _bridge_read(transport: Callable, path: str, prop: str) -> Any:
    result = _bridge_op(transport, {"op": "get", "path": path, "prop": prop})
    if "value" not in result:
        raise CaptureLoopError("SIDEKICK BRIDGE readback failed")
    value = result["value"]
    if isinstance(value, (list, tuple)) and len(value) == 1:
        return value[0]
    return value


def _source_track_indices(overview: Any, ears_index: int) -> list[int]:
    """Every regular track of the Set but SIDEKICK EARS (2.2 trackIndex,
    2.3 path 't3') — where a launched source scene's clips can sit."""
    out = []
    for track in _result(overview).get("tracks") or []:
        index = pp_session.track_index_of(track)
        if index is not None and index != ears_index:
            out.append(index)
    return out


def _launched_slot_in_scene(transport: Callable, track_indices: list[int],
                            scene_index: int) -> str | None:
    """The first clip slot of ``scene_index`` that Live reports playing or
    triggered (LOM ClipSlot.is_playing / is_triggered — triggered is the
    honest state right after a launch under quantization), as 't<i>/s<j>'."""
    for track_index in track_indices:
        path = "live_set tracks %d clip_slots %d" % (track_index, scene_index)
        for prop in ("is_playing", "is_triggered"):
            if _bool_result(_bridge_read(transport, path, prop), "source clip slot"):
                return "t%d/s%d %s" % (track_index, scene_index, prop)
    return None


def _require_source_scene_launched(playback: Any, scene_index: int, v23: bool,
                                   transport: Callable, track_indices: list[int]) -> str:
    """Prove the play-scene call launched source scene ``scene_index``
    (review 2026-09-30 finding 5: the transport running identifies no scene).
    2.3: the captured reply names the scene it played ({playing, scene: {id,
    name, path}}, v23b_play_scene_by_path) — a reply without one refuses.
    2.2 (no captured reply): fresh evidence from the Set — a clip slot of
    that scene playing or triggered. Both then read the transport back.
    Returns what proved it; raises CaptureLoopError with the reason."""
    played = _result(playback)
    said = (("; the device said: %s" % " | ".join(playback.warnings))
            if getattr(playback, "warnings", None) else "")
    wanted = "s%d" % scene_index
    scene = played.get("scene")
    if isinstance(scene, dict) and scene.get("path") not in (None, wanted):
        raise CaptureLoopError("Producer Pal played %s, not source scene %s%s"
                               % (scene.get("path"), wanted, said))
    if played.get("playing") is False:
        raise CaptureLoopError("source scene %d did not start playing%s"
                               % (scene_index, said))
    if isinstance(scene, dict) and scene.get("path") == wanted:
        proof = "play-scene receipt %s" % wanted
    elif v23:
        raise CaptureLoopError(
            "Producer Pal 2.3 / 2.4 names the scene it played in the play-scene reply "
            "(v23b_play_scene_by_path, v24_play_scene_by_path); this reply named no scene, "
            "so source scene %d is unproven — refusing to capture%s" % (scene_index, said))
    else:
        launched = _launched_slot_in_scene(transport, track_indices, scene_index)
        if launched is None:
            raise CaptureLoopError(
                "no clip slot of source scene %d is playing or triggered after "
                "play-scene (2.2 replies name no scene) — refusing to capture%s"
                % (scene_index, said))
        proof = "clip slot %s" % launched
    if not _bool_result(_bridge_read(transport, "live_set", "is_playing"), "transport"):
        raise CaptureLoopError("source scene %d did not start playing%s"
                               % (scene_index, said))
    return proof


async def _undo_started_playback(session: Any, transport: Callable,
                                 was_playing: bool) -> str:
    """After a refused scene proof: stop what this capture started, never
    what was already running. Returns the note for the error."""
    if was_playing:
        return "; playback was already running before this capture and was left alone"
    try:
        await session.call("ppal-playback", pp_session.requests_for(session).stop())
        still = _bool_result(_bridge_read(transport, "live_set", "is_playing"), "transport")
    except Exception:
        return ("; stopping the playback this capture started FAILED — check Live's "
                "transport")
    if still:
        return "; sent stop, but the transport still reports playing — check Live"
    return "; stopped the playback this capture started"


async def _delete_capture_clip(session: Any, surface: str, slot_path: str,
                               bridge_transport: Callable,
                               cleanup: dict[str, Any]) -> bool:
    """Delete a partial Ears clip and prove its slot is empty."""
    try:
        if surface == "live_api":
            await _live_ops(session, slot_path,
                            [{"type": "call", "method": "delete_clip"}])
            remains = _bool_result((await _live_ops(
                session, slot_path,
                [{"type": "get", "property": "has_clip"}]))[0], "cleanup slot")
        else:
            _bridge_op(bridge_transport, {
                "op": "call", "path": slot_path,
                "method": "delete_clip", "args": []})
            remains = _bool_result(
                _bridge_read(bridge_transport, slot_path, "has_clip"), "cleanup slot")
        cleanup["partial_deleted"] = not remains
        if remains:
            cleanup["delete_failed"] = True
        return not remains
    except Exception:
        cleanup["delete_failed"] = True
        return False


def _delete_capture_file(sample_path: str, recording_root: Path,
                         cleanup: dict[str, Any]) -> bool:
    """Delete one failed take, but only from this Set's recording area."""
    try:
        root = recording_root.resolve(strict=True)
        target = Path(sample_path).expanduser().resolve(strict=False)
        if (not target.is_relative_to(root)
                or target.suffix.lower() not in {".wav", ".aif", ".aiff"}):
            raise ValueError
        if not target.exists():
            cleanup["recording_deleted"] = True
            return True
        if not target.is_file():
            raise ValueError
        target.unlink()
        sidecar = Path(str(target) + ".asd")
        if sidecar.is_file():
            sidecar.unlink()
        cleanup["recording_deleted"] = True
        return True
    except Exception:
        cleanup["recording_delete_failed"] = True
        return False


def _recording_snapshot(recording_root: Path) -> frozenset[Path]:
    try:
        return frozenset(
            child.resolve(strict=False)
            for child in recording_root.iterdir()
            if child.is_file()
        )
    except OSError:
        raise CaptureLoopError("Live recording area is unavailable") from None


def _delete_new_capture_file(recording_root: Path, before: frozenset[Path],
                             cleanup: dict[str, Any]) -> bool:
    """Remove newly-created Ears audio when no clip readback exists."""
    try:
        root = recording_root.resolve(strict=True)
        candidates = []
        for child in root.iterdir():
            target = child.resolve(strict=False)
            if (target in before or not target.is_relative_to(root)
                    or target.suffix.lower() not in {".wav", ".aif", ".aiff"}
                    or not target.name.startswith(EARS_TRACK + " ")):
                continue
            candidates.append(target)
        if not candidates:
            cleanup["recording_deleted"] = True
            return True
        for candidate in candidates:
            if not _delete_capture_file(str(candidate), root, cleanup):
                return False
        return True
    except Exception:
        cleanup["recording_delete_failed"] = True
        return False


def _recording_root(project_file: Any) -> Path:
    if isinstance(project_file, str) and not project_file.strip():
        raise CaptureLoopError("Save the Live Set before capturing")
    if not isinstance(project_file, str):
        raise CaptureLoopError("Live project location is unavailable")
    project = Path(project_file).expanduser()
    if (not project.is_absolute() or project.suffix.lower() != ".als"
            or not project.is_file()):
        raise CaptureLoopError("Live project location is unavailable")
    root = project.parent / "Samples" / "Recorded"
    if not root.is_dir():
        raise CaptureLoopError("Live recording area is unavailable")
    return root


def _sample_path(response: Any) -> tuple[str, str]:
    sample = pp_session.find_str(response, "sampleFile") \
        or pp_session.find_str(response, "sample_file")
    name = pp_session.find_str(response, "name", "SIDEKICK EARS Capture")
    if not isinstance(sample, str) or not sample:
        raise CaptureLoopError("Recorded audio readback is unavailable")
    return sample, str(name or "SIDEKICK EARS Capture")


def _public_result(value: dict[str, Any]) -> dict[str, Any]:
    """Reject path/token leakage even when an injected dependency misbehaves."""
    from mcp_server import session_ears

    forbidden_keys = {"inventory_id", "sampleFile", "sample_file", "file_path"}

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            if forbidden_keys.intersection(str(key) for key in item):
                raise CaptureLoopError("capture result contained private state")
            for child in item.values():
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)
        elif session_ears._contains_absolute_path(item):
            raise CaptureLoopError("capture result contained private state")

    walk(value)
    return value


def _same_set_identity(expected: dict[str, Any] | None, actual: dict[str, Any] | None) -> bool:
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return False
    for key in ("set_id", "runtime_epoch"):
        exp_val = expected.get(key)
        act_val = actual.get(key)
        if not isinstance(exp_val, str) or not exp_val:
            return False
        if not isinstance(act_val, str) or not act_val:
            return False
        if exp_val != act_val:
            return False
    return True


def _filter_supported_kwargs(func: Callable[..., Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return {}
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values()):
        return dict(kwargs)
    params = sig.parameters
    return {key: value for key, value in kwargs.items() if key in params}


def _call_identity_reader(reader: Callable[..., Any], session: Any) -> Any:
    try:
        params = inspect.signature(reader).parameters
    except (TypeError, ValueError):
        return reader(session)
    if not params:
        return reader()
    first = next(iter(params.values()))
    if first.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
        return reader(session)
    if first.name in ("session", "s") or first.default is not inspect.Parameter.empty:
        return reader(session)
    return reader()


async def _read_live_set_identity(session: Any) -> dict[str, Any]:
    from mcp_server import live_write
    val = await live_write.read_set_identity(session)
    if not isinstance(val, dict) or not val.get("set_id") or not val.get("runtime_epoch"):
        raise ValueError("malformed identity")
    return val


async def _read_capture_set_identity(
    session: Any,
    selected: str,
    bridge_transport: Callable | None,
    custom_reader: Callable | None = None,
) -> dict[str, Any]:
    if custom_reader is not None:
        val = _call_identity_reader(custom_reader, session)
        if hasattr(val, "__await__"):
            val = await val
        if not isinstance(val, dict) or not val.get("set_id") or not val.get("runtime_epoch"):
            raise CaptureLoopError("Verified Set identity readback failed")
        return val

    if selected in ("live_api", "bridge") and session.capabilities.get("live_api"):
        try:
            return await _read_live_set_identity(session)
        except Exception as exc:
            raise CaptureLoopError("Live Set identity verification failed") from exc
    if selected == "live_api":
        raise CaptureLoopError("Live Set identity verification failed")
    if selected == "bridge":
        reader = getattr(bridge_transport, "read_set_identity", None) or getattr(
            session, "read_set_identity", None)
        if callable(reader):
            val = _call_identity_reader(reader, session)
            if hasattr(val, "__await__"):
                val = await val
            if isinstance(val, dict) and val.get("set_id") and val.get("runtime_epoch"):
                return val
            raise CaptureLoopError("Verified Set identity readback failed")
        raise CaptureLoopError(
            "Verified Set identity is unsupported for bridge capture without verification transport")
    raise CaptureLoopError("Unsupported capture surface: %s" % selected)


async def _verify_identity_unchanged(
    session: Any,
    selected: str,
    bridge_transport: Callable | None,
    identity_before: dict[str, Any],
    custom_reader: Callable | None,
    cleanup: dict[str, Any],
) -> bool:
    try:
        current = await _read_capture_set_identity(
            session, selected, bridge_transport, custom_reader)
        if _same_set_identity(identity_before, current):
            return True
    except Exception:
        pass
    cleanup["cleanup_unverified"] = True
    return False


async def _safe_delete_capture_clip(
    session: Any,
    surface: str,
    slot_path: str,
    bridge_transport: Callable,
    cleanup: dict[str, Any],
    *,
    identity_before: dict[str, Any],
    custom_reader: Callable | None,
) -> bool:
    if not await _verify_identity_unchanged(
            session, surface, bridge_transport, identity_before, custom_reader, cleanup):
        return False
    return await _delete_capture_clip(session, surface, slot_path, bridge_transport, cleanup)


async def _capture_loop_async(
    bars: int, source_scene_index: int | None, surface: str,
    listen: bool, confirm: bool, *, operation_session: Callable | None = None,
    bridge_transport: Callable | None = None,
    bridge_client_factory: Callable | None = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
    audio_validator: Callable = analyze_captured_audio,
    registrar: Callable | None = None, listener: Callable | None = None,
    _read_set_identity: Callable | None = None,
) -> dict[str, Any]:
    if surface not in SURFACES:
        raise ValueError("surface must be auto, live_api, or bridge")
    if not isinstance(confirm, bool) or not isinstance(listen, bool):
        raise ValueError("confirm and listen must be booleans")
    if (source_scene_index is not None and
            (isinstance(source_scene_index, bool) or not isinstance(source_scene_index, int)
             or source_scene_index < 0)):
        raise ValueError("source_scene_index must be a non-negative integer or omitted")
    operation_session = operation_session or pp_session.operation_session
    if bridge_transport is None:
        from mcp_server.envelope_bridge import send_cmds
        bridge_transport = send_cmds
    if registrar is None or listener is None:
        from mcp_server import session_ears
        registrar = registrar or session_ears.register_validated_capture
        listener = listener or session_ears.listen_to_takes

    async with operation_session() as session:
        overview_raw = await session.call("ppal-read-live-set", {"include": ["tracks"]})
        track, track_index, scene_count, numerator, tempo, session_id, denominator = _overview(overview_raw)
        if source_scene_index is not None and source_scene_index >= scene_count:
            raise CaptureLoopError("source scene index is outside the Live Set")
        try:
            duration = capture_duration_seconds(bars, numerator, denominator, tempo)
        except ValueError as exc:
            raise CaptureLoopError(str(exc)) from None
        requests = pp_session.requests_for(session)
        listing = await session.call(
            "ppal-read-track", requests.read_track(track_index, include=["session-clips"]))
        target_scene = _empty_slot(listing, track_index, scene_count)
        selected = "live_api" if session.capabilities.get("live_api") else "bridge"
        if surface == "live_api" and selected != "live_api":
            raise CaptureLoopError("Producer Pal Live API capability is unavailable")
        if surface != "auto":
            selected = surface

        slot_path = "live_set tracks %d clip_slots %d" % (track_index, target_scene)
        track_path = "live_set tracks %d" % track_index
        live_object_path = "t%d/s%d" % (track_index, target_scene)
        if selected == "live_api":
            target_state = await _live_ops(session, track_path, [
                {"type": "get", "property": "name"},
                {"type": "get", "property": "has_audio_input"},
            ])
            if target_state[0] != EARS_TRACK or not _bool_result(
                    target_state[1], "SIDEKICK EARS audio type"):
                raise CaptureLoopError("Live API target is not the SIDEKICK EARS audio track")
            has_clip = _bool_result((await _live_ops(
                session, slot_path, [{"type": "get", "property": "has_clip"}]))[0],
                "target slot")
            project_file = (await _live_ops(
                session, "live_set", [{"type": "getProperty", "property": "file_path"}]))[0]
        else:
            has_clip = _bool_result(_bridge_read(bridge_transport, slot_path, "has_clip"),
                                    "target slot")
            project_file = _bridge_read(bridge_transport, "live_set", "file_path")
        if has_clip:
            raise CaptureLoopError("selected SIDEKICK EARS slot is occupied")
        recording_root = _recording_root(project_file)

        if not confirm:
            return {
                "status": "preview", "executed": False, "surface": selected,
                "bars": bars, "duration_s": duration, "path": live_object_path,
                "source_scene_index": source_scene_index,
                "operations": ["validate_target", "record", "verify_audio", "register_capture"],
            }

        identity_before = await _read_capture_set_identity(
            session, selected, bridge_transport, _read_set_identity
        )

        recording_before = _recording_snapshot(recording_root)
        capture_id = "cap-" + secrets.token_hex(24)
        cleanup: dict[str, Any] = {"stopped": False, "arm_restored": False,
                                   "partial_deleted": False}
        sample_path: str | None = None
        clip_name = "SIDEKICK EARS Capture"

        if selected == "bridge":
            client = (bridge_client_factory(duration) if bridge_client_factory
                      else CaptureBridgeClient(bridge_transport, timeout_s=duration + 30.0))
            started = False
            finalized = False
            try:
                if source_scene_index is not None:
                    # was the transport already running? (what a refusal may stop)
                    was_playing = _bool_result(
                        _bridge_read(bridge_transport, "live_set", "is_playing"),
                        "transport")
                    # 2.2 sceneIndex, 2.3 path 's<i>' (2.3 removed sceneIndex)
                    playback = await session.call(
                        "ppal-playback", requests.play_scene(source_scene_index))
                    try:
                        _require_source_scene_launched(
                            playback, source_scene_index, requests.v23, bridge_transport,
                            _source_track_indices(overview_raw, track_index))
                    except CaptureLoopError as exc:
                        note = await _undo_started_playback(
                            session, bridge_transport, was_playing)
                        raise CaptureLoopError("%s%s" % (exc, note)) from None
                client.start(capture_id, track_index, target_scene,
                             max(1, int(round(duration * 1000))))
                started = True
                client.wait_ready(capture_id)
                if not await _verify_identity_unchanged(
                        session, selected, bridge_transport, identity_before,
                        _read_set_identity, cleanup):
                    raise CaptureLoopError("Live Set identity changed during bridge capture")
                final = client.finalize(capture_id)
                finalized = True
                cleanup.update(final.get("cleanup") or {})
                if (cleanup.get("stopped") is not True
                        or cleanup.get("arm_restored") is not True):
                    raise CaptureLoopError("Bridge cleanup verification failed")
                if final.get("path") != live_object_path:
                    raise CaptureLoopError("Bridge finalized a different Live slot")
                readback = await session.call(
                    "ppal-read-clip", {**pp_session.clip_locator(
                        track=track_index, scene=target_scene),
                                       "include": ["sample"]})
                sample_path, clip_name = _sample_path(readback)
            except Exception:
                if finalized:
                    await _safe_delete_capture_clip(
                        session, selected, slot_path, bridge_transport, cleanup,
                        identity_before=identity_before,
                        custom_reader=_read_set_identity)
                raise
            finally:
                if started and not finalized and await _verify_identity_unchanged(
                        session, selected, bridge_transport, identity_before,
                        _read_set_identity, cleanup):
                    try:
                        receipt = client.cancel(capture_id)
                        cleanup.update(receipt.get("cleanup") or {})
                    except Exception:
                        cleanup["cancel_failed"] = True
                if started and sample_path is None and await _verify_identity_unchanged(
                        session, selected, bridge_transport, identity_before,
                        _read_set_identity, cleanup):
                    try:
                        slot_empty = not _bool_result(
                            _bridge_read(bridge_transport, slot_path, "has_clip"),
                            "cleanup slot")
                    except Exception:
                        slot_empty = False
                    if slot_empty:
                        _delete_new_capture_file(
                            recording_root, recording_before, cleanup)
                    else:
                        cleanup["recording_delete_failed"] = True
        else:
            record_beats = bars * numerator * 4.0 / denominator
            arm_before = _bool_result((await _live_ops(
                session, track_path, [{"type": "get", "property": "arm"}]))[0],
                "SIDEKICK EARS arm")
            clip_created = False
            try:
                await _live_ops(session, track_path,
                                [{"type": "set", "property": "arm", "value": True}])
                if not _bool_result((await _live_ops(
                        session, track_path, [{"type": "get", "property": "arm"}]))[0],
                        "SIDEKICK EARS arm"):
                    raise CaptureLoopError("SIDEKICK EARS did not arm")
                if source_scene_index is not None:
                    await _live_ops(session, "live_set", [{
                        "type": "call", "method": "fire_scene_at_index",
                        "args": [source_scene_index]}])
                await _live_ops(session, slot_path, [{
                    "type": "call", "method": "fire",
                    "args": [record_beats, 0],
                }])
                clip_created = True
                waited = sleep(duration + 0.25)
                if hasattr(waited, "__await__"):
                    await waited
                if not await _verify_identity_unchanged(
                        session, selected, bridge_transport, identity_before,
                        _read_set_identity, cleanup):
                    raise CaptureLoopError("Live Set identity changed during capture recording")
                await _live_ops(session, track_path,
                                [{"type": "call", "method": "stop_all_clips"}])
                cleanup["stopped"] = True
                if not _bool_result((await _live_ops(
                        session, slot_path, [{"type": "get", "property": "has_clip"}]))[0],
                        "recorded slot"):
                    raise CaptureLoopError("Live API capture did not create a clip")
                readback = await session.call(
                    "ppal-read-clip", {**pp_session.clip_locator(
                        track=track_index, scene=target_scene),
                                       "include": ["sample"]})
                sample_path, clip_name = _sample_path(readback)
            finally:
                identity_ok = await _verify_identity_unchanged(
                    session, selected, bridge_transport, identity_before,
                    _read_set_identity, cleanup)
                if identity_ok:
                    try:
                        await _live_ops(session, track_path,
                                        [{"type": "call", "method": "stop_all_clips"}])
                        cleanup["stopped"] = True
                    except Exception:
                        cleanup["stop_failed"] = True
                    try:
                        await _live_ops(session, track_path, [{
                            "type": "set", "property": "arm", "value": arm_before}])
                        restored = _bool_result((await _live_ops(
                            session, track_path, [{"type": "get", "property": "arm"}]))[0],
                            "SIDEKICK EARS arm")
                        cleanup["arm_restored"] = restored == arm_before
                    except Exception:
                        cleanup["arm_restore_failed"] = True
                    if clip_created and sample_path is None:
                        deleted = await _delete_capture_clip(
                            session, selected, slot_path, bridge_transport, cleanup)
                        if deleted:
                            _delete_new_capture_file(
                                recording_root, recording_before, cleanup)
                elif clip_created:
                    cleanup["cleanup_unverified"] = True

            if (cleanup.get("stopped") is not True
                    or cleanup.get("arm_restored") is not True):
                deleted = await _safe_delete_capture_clip(
                    session, selected, slot_path, bridge_transport, cleanup,
                    identity_before=identity_before,
                    custom_reader=_read_set_identity)
                if deleted:
                    if sample_path is not None:
                        _delete_capture_file(sample_path, recording_root, cleanup)
                    else:
                        _delete_new_capture_file(
                            recording_root, recording_before, cleanup)
                raise CaptureLoopError("Live API cleanup verification failed")

        if sample_path is None:
            raise CaptureLoopError("Recorded audio readback is unavailable")
        try:
            audio = audio_validator(sample_path, [recording_root])
        except Exception:
            deleted = await _safe_delete_capture_clip(
                session, selected, slot_path, bridge_transport, cleanup,
                identity_before=identity_before,
                custom_reader=_read_set_identity)
            file_deleted = deleted and _delete_capture_file(
                sample_path, recording_root, cleanup)
            if not deleted or not file_deleted:
                raise CaptureLoopError(
                    "Recorded audio failed validation and cleanup could not be verified") from None
            raise CaptureLoopError("Recorded audio failed local validation") from None
        try:
            identity_after = await _read_capture_set_identity(
                session, selected, bridge_transport, _read_set_identity
            )
            if not _same_set_identity(identity_before, identity_after):
                raise CaptureLoopError("Live Set identity changed during capture recording")
        except Exception as exc:
            deleted = await _safe_delete_capture_clip(
                session, selected, slot_path, bridge_transport, cleanup,
                identity_before=identity_before,
                custom_reader=_read_set_identity)
            file_deleted = deleted and (sample_path is None or _delete_capture_file(
                sample_path, recording_root, cleanup))
            if not deleted or not file_deleted:
                raise CaptureLoopError(
                    "Live Set identity check failed and cleanup could not be verified") from exc
            if isinstance(exc, CaptureLoopError):
                raise
            raise CaptureLoopError("Live Set identity verification failed") from exc

        try:
            reg_kwargs = {
                "session_id": _public_set_identity(identity_after).get(
                    "set_id", session_id),
                "track_index": track_index,
                "scene_index": target_scene,
                "clip_name": clip_name,
                "path": sample_path,
                "duration_s": audio.get("duration_s") if isinstance(audio, dict) else None,
                "bars": bars,
                "set_identity": identity_after,
            }
            registered = registrar(**_filter_supported_kwargs(registrar, reg_kwargs))
            if not _well_formed_registration(registered):
                raise CaptureLoopError("Capture registration returned malformed output")
        except Exception as exc:
            deleted = await _safe_delete_capture_clip(
                session, selected, slot_path, bridge_transport, cleanup,
                identity_before=identity_before,
                custom_reader=_read_set_identity)
            file_deleted = deleted and _delete_capture_file(
                sample_path, recording_root, cleanup)
            if not deleted or not file_deleted:
                raise CaptureLoopError(
                    "capture post-processing failed and cleanup could not be verified") from exc
            if isinstance(exc, CaptureLoopError):
                raise
            raise CaptureLoopError("capture post-processing failed") from None

    # The operation session is closed before listening: the recording is
    # validated, and a listening failure must never delete it. The listener
    # gets the already verified identity, so it needs no Live contact.
    listening_card = None
    listen_error = None
    capture_id = registered["capture_id"]
    if listen:
        frozen_identity = identity_after
        listener_kwargs = _filter_supported_kwargs(listener, {
            "_read_set_identity": lambda: frozen_identity,
        })
        try:
            heard = listener(
                registered["inventory_id"],
                [capture_id],
                **listener_kwargs,
            )
            if isinstance(heard, dict):
                cards, errors = heard.get("cards"), heard.get("errors")
            else:
                # Stub listeners in tests return the card list directly.
                cards, errors = heard, []
        except Exception as exc:
            cards, errors = None, [{"take_id": capture_id, "reason": str(exc)}]
        if isinstance(cards, list) and len(cards) == 1 and isinstance(cards[0], dict):
            listening_card = cards[0]
            listening_card.setdefault("heard_at", time.time())
        else:
            reason = _first_listen_error_reason(errors, capture_id)
            listen_error = "%s. Retry with inventory_session_audio(), then " \
                "listen_to_takes() for the captured take; the validated recording is kept." % reason
            # The capability is one-shot. Re-register only when the listener
            # actually consumed it; preflight failures leave it usable as-is.
            try:
                from mcp_server import session_ears
                session_ears._peek_inventory_takes(
                    registered["inventory_id"], [capture_id])
                consumed = False
            except Exception as exc:
                consumed = "unknown take_id" in str(exc)
            if consumed:
                try:
                    reregistered = registrar(
                        **_filter_supported_kwargs(registrar, reg_kwargs))
                except Exception:
                    reregistered = None
                if _well_formed_registration(reregistered):
                    registered = reregistered
                    capture_id = registered["capture_id"]
    public = _public_result({
        "capture_id": capture_id, "path": live_object_path,
        "listening_card": listening_card, "surface": selected,
        "cleanup_receipt": cleanup,
        **({"listen_error": listen_error} if listen_error is not None else {}),
        **({"retry": "Run inventory_session_audio() again, then listen_to_takes() for the captured take; the validated recording is kept."}
           if listen_error is not None else {}),
    })
    return public


def _well_formed_registration(registered: Any) -> bool:
    """True for a registrar receipt naming a capture and its inventory."""
    return (
        isinstance(registered, dict)
        and isinstance(registered.get("capture_id"), str)
        and isinstance(registered.get("inventory_id"), str)
    )


def _first_listen_error_reason(errors: Any, capture_id: str) -> str:
    """Public one-line reason for a failed capture listen."""
    from mcp_server import session_ears

    if isinstance(errors, list):
        for entry in errors:
            if isinstance(entry, dict) and entry.get("take_id") == capture_id:
                reason = entry.get("reason")
                if isinstance(reason, str) and reason.strip():
                    return session_ears._public_error_message(reason)
        for entry in errors:
            if isinstance(entry, dict) and isinstance(entry.get("reason"), str) \
                    and entry["reason"].strip():
                return session_ears._public_error_message(entry["reason"])
    return "capture listen produced no card for take %s" % capture_id


def capture_loop(bars: int = 8, source_scene_index: int | None = None,
                 surface: str = "auto", listen: bool = False,
                 confirm: bool = False, **_injected: Any) -> dict[str, Any]:
    """Preview or execute a verified capture into SIDEKICK EARS."""
    try:
        return _run_async(_capture_loop_async(
            bars, source_scene_index, surface, listen, confirm, **_injected))
    except (ValueError, CaptureLoopError):
        raise
    except Exception as exc:
        cause = _single_group_error(exc)
        if isinstance(cause, (ValueError, CaptureLoopError)):
            raise cause from None
        raise CaptureLoopError("capture failed; cleanup was attempted") from None


def run_selftest() -> None:
    assert _bool_result(1, "value") is True
    assert _bool_result(0, "value") is False
    listing = pp_session.parse_tool_response(
        '{"result":{"sessionClips":[{"slot":"2/0","name":"x"}]}}')
    assert _empty_slot(listing, 2, 2) == 1
