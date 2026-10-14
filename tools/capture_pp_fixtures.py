"""Capture real Producer Pal responses as test fixtures (plan items 0.0a / 0.0b).

    .venv/bin/python tools/capture_pp_fixtures.py read      # any Set, read-only calls
    .venv/bin/python tools/capture_pp_fixtures.py mutate    # ONLY a Set named SCRATCH*
    .venv/bin/python tools/capture_pp_fixtures.py v23       # PP 2.3 addressing, move/split (SCRATCH* only)
    .venv/bin/python tools/capture_pp_fixtures.py v23b      # PP 2.3 scenes, selection, take lanes, refusals (SCRATCH* only)
    .venv/bin/python tools/capture_pp_fixtures.py v24       # PP 2.4 shapes: per-target entries, ok:false, deletes, replace, batch, lanes (SCRATCH* only)
    .venv/bin/python tools/capture_pp_fixtures.py legacy    # old vs new argument forms
                                                            # (reads on any Set; writes only in SCRATCH*)

Talks to Producer Pal with the raw MCP client (not our handshake validation), so it
records exactly what the device answers — including versions our guard would refuse —
and every error response. Output: tests/fixtures/pp/<pp_version>/<mode>/<name>.json with
the call, arguments and the raw content. Home paths are sanitized to /Users/USER.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = os.environ.get("PRODUCER_PAL_URL", "http://localhost:3350/mcp")
HOME = str(Path.home())


def _sanitize(value):
    if isinstance(value, str):
        return value.replace(HOME, "/Users/USER")
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    return value


def _content(result):
    """Raw tool result → JSON-able: text parts parsed as JSON when possible."""
    parts = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text is None:
            continue
        try:
            parts.append({"json": json.loads(text)})
        except (TypeError, ValueError):
            parts.append({"text": text})
    return {"is_error": bool(getattr(result, "isError", False)), "content": parts}


def _track_index(record):
    """A track's index from either reply shape: 2.2 ``trackIndex``, 2.3 path 't10'."""
    if not isinstance(record, dict):
        return None
    index = record.get("trackIndex")
    if isinstance(index, int) and not isinstance(index, bool):
        return index
    m = re.fullmatch(r"t(\d+)", str(record.get("path") or ""))
    return int(m.group(1)) if m else None


def _created_ids(reply):
    """Ids a create-track reply names: {id} / {ids} (2.2), {id, path} or a list (2.3)."""
    items = reply if isinstance(reply, list) else [reply]
    ids = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ids.extend(str(i) for i in (item.get("ids") or []) if i)
        if item.get("id"):
            ids.append(str(item["id"]))
    return ids


class Recorder:
    def __init__(self, session, out_dir: Path):
        self.session, self.out_dir, self.saved = session, out_dir, []

    async def call(self, name: str, args: dict, label: str):
        try:
            res = await self.session.call_tool(name, args)
            payload = _content(res)
        except Exception as exc:  # recorded, never fatal: errors are fixtures too
            payload = {"is_error": True, "exception": repr(exc)}
        record = _sanitize({"tool": name, "arguments": args, "response": payload})
        path = self.out_dir / ("%s.json" % label)
        path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        self.saved.append(path)
        return payload

    @staticmethod
    def first_json(payload):
        for part in payload.get("content", []):
            if "json" in part:
                return part["json"]
        return None


async def main(mode: str) -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    sys.path.insert(0, str(ROOT / "mcp_server"))
    from pp_session import pp_transport_headers
    # Same transport headers production sends (x-producer-pal-notation), so the
    # captured responses are the ones the sidekick actually receives.
    async with streamablehttp_client(URL, headers=pp_transport_headers()) as (rd, wr, *_):
        async with ClientSession(rd, wr) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = {t.name: t for t in listed.tools}
            connect = _content(await session.call_tool("ppal-connect", {}))
            info = Recorder.first_json(connect) or {}
            version = str(info.get("pp_version") or info.get("version")
                          or info.get("producerPalVersion") or "")
            fmt = "json" if info else "compact"
            if not version:
                # compact (non-JSON) output: {connected:true,producerPalVersion:"2.2.0-rc1",...}
                text = " ".join(p.get("text", "") for p in connect.get("content", []))
                m = re.search(r'producerPalVersion:"([^"]+)"', text)
                version = m.group(1) if m else "unknown"
            if fmt == "compact":
                version += "-compact"
                print("WARNING: Producer Pal answered in compact (non-JSON) notation; the "
                      "sidekick requires JSON responses on this device.")
            if not re.fullmatch(r"[\w.\-]+", version):
                version = "unknown"
            out = ROOT / "tests" / "fixtures" / "pp" / version / mode
            out.mkdir(parents=True, exist_ok=True)
            # Merge, never shrink: a device with Live API access off lists
            # fewer tools, and that must not erase schemas captured earlier.
            schema_path = out.parent / "tool_schemas.json"
            schemas = json.loads(schema_path.read_text()) if schema_path.exists() else {}
            schemas.update({n: {"description": t.description, "inputSchema": t.inputSchema}
                            for n, t in tools.items()})
            schema_path.write_text(json.dumps(schemas, indent=2, sort_keys=True))
            rec = Recorder(session, out)
            (out / "connect.json").write_text(json.dumps(
                _sanitize({"tool": "ppal-connect", "arguments": {}, "response": connect}),
                indent=2, sort_keys=True))

            live_set = Recorder.first_json(await rec.call(
                "ppal-read-live-set", {"include": ["tracks", "locators"]},
                "read_live_set_tracks_locators")) or {}
            await rec.call("ppal-read-live-set", {}, "read_live_set_default")
            # Producer Pal reports no Set name; Live's file_path is the only
            # reliable one (empty for an unsaved Set).
            set_name = ""
            if "ppal-live-api" in tools:
                sys.path.insert(0, str(ROOT / "mcp_server"))
                from pp_session import build_live_api_request
                fp = _content(await session.call_tool("ppal-live-api", build_live_api_request(
                    "live_set", [{"type": "getProperty", "property": "file_path"}])))
                res = (Recorder.first_json(fp) or {}).get("results") or [{}]
                set_name = os.path.basename(str(res[0].get("result") or ""))

            if mode == "read":
                tracks = live_set.get("tracks") or []
                for i, _t in enumerate(tracks[:3]):
                    inc = ["mixer", "devices", "session-clips", "arrangement-clips", "notes", "routings"]
                    await rec.call("ppal-read-track", {"path": "t%d" % i, "include": inc},
                                   "read_track_t%d_path" % i)
                    await rec.call("ppal-read-track", {"trackIndex": i, "include": inc},
                                   "read_track_t%d_index" % i)
                await rec.call("ppal-select", {}, "select_current")
                if "ppal-live-api" in tools:
                    sys.path.insert(0, str(ROOT / "mcp_server"))
                    from pp_session import build_live_api_request
                    await rec.call("ppal-live-api", build_live_api_request("live_set", [
                        {"type": "getProperty", "property": prop}
                        for prop in ("current_song_time", "is_playing", "file_path",
                                     "loop", "loop_start", "loop_length")]),
                        "live_api_live_set_properties")
                if "ppal-context" in tools:
                    await rec.call("ppal-context", {}, "context")
            elif mode == "piece":
                # Read-only full snapshot of the open piece for the Phase 1
                # snapshot reader: every track, return and master with the
                # arrangement, mixer, routing and devices. No writes.
                await rec.call("ppal-read-live-set", {"include": [
                    "tracks", "scenes", "routings", "mixer", "locators"]}, "piece_live_set")
                inc = ["arrangement-clips", "notes", "timing", "sample", "devices",
                       "routings", "mixer"]
                for i, _t in enumerate(live_set.get("tracks") or []):
                    await rec.call("ppal-read-track", {"trackIndex": i, "include": inc},
                                   "piece_track_%02d" % i)
                returns = live_set.get("returnTrackCount") or live_set.get("returnTracks") or 0
                n_returns = returns if isinstance(returns, int) else len(returns)
                for i in range(max(n_returns, 0) or 4):
                    await rec.call("ppal-read-track", {"trackIndex": i, "trackType": "return",
                                   "include": ["devices", "routings", "mixer"]},
                                   "piece_return_%02d" % i)
                await rec.call("ppal-read-track", {"trackType": "master",
                               "include": ["devices", "mixer"]}, "piece_master")
                await rec.call("ppal-select", {}, "piece_select")
                if "ppal-live-api" in tools:
                    sys.path.insert(0, str(ROOT / "mcp_server"))
                    from pp_session import build_live_api_request
                    await rec.call("ppal-live-api", build_live_api_request("live_set", [
                        {"type": "getProperty", "property": prop}
                        for prop in ("current_song_time", "is_playing", "file_path", "loop",
                                     "loop_start", "loop_length", "signature_numerator",
                                     "signature_denominator", "tempo")]),
                        "piece_live_api_live_set")
            elif mode == "piece-notes":
                # Read-only: for every arrangement MIDI clip, the clip's timing
                # properties and ALL its notes via the Live API. Producer Pal's
                # read-track/read-clip only return notes in [-L, 2L) of the clip.
                if "ppal-live-api" not in tools:
                    print("piece-notes needs Live API access on the Producer Pal device")
                    return 2
                sys.path.insert(0, str(ROOT / "mcp_server"))
                from pp_session import build_live_api_request
                props = ("name", "loop_start", "loop_end", "start_marker", "end_marker",
                         "looping", "start_time", "end_time", "signature_numerator",
                         "signature_denominator", "is_midi_clip", "muted")
                for i, _t in enumerate(live_set.get("tracks") or []):
                    tr = Recorder.first_json(await rec.call(
                        "ppal-read-track", {"trackIndex": i, "include": ["arrangement-clips"]},
                        "notes_track_%02d" % i)) or {}
                    if tr.get("type") != "midi":
                        continue
                    for j, clip in enumerate(tr.get("arrangementClips") or []):
                        cid = clip.get("id")
                        if not cid:
                            continue
                        await rec.call("ppal-live-api", build_live_api_request("id %s" % cid, [
                            *({"type": "getProperty", "property": p} for p in props),
                            {"type": "call", "method": "get_notes_extended",
                             "args": [0, 128, -100000, 200000]}]),
                            "notes_clip_t%02d_%02d" % (i, j))
            elif mode == "v23":
                # Producer Pal 2.3 addressing (arrangement positions inside the path,
                # 't2[5|1]'), clip move/split, reply shapes, and the automatable items
                # of the staging live checklist. Writes ONLY on two tracks it creates
                # in a Set saved as SCRATCH*.als, and deletes them at the end.
                if not set_name.upper().startswith("SCRATCH"):
                    print("REFUSED: v23 mode needs the open Set SAVED as SCRATCH*.als "
                          "(got %r). Nothing was changed." % (set_name or "unsaved"))
                    return 2

                async def call(tool, args, label):
                    return Recorder.first_json(await rec.call(tool, args, "v23_" + label)) or {}

                def index_of(reply, track_id):
                    m = re.fullmatch(r"t(\d+)", str((reply or {}).get("path") or ""))
                    return int(m.group(1)) if m else None

                async def track_index(track_id):
                    ls = await call("ppal-read-live-set", {"include": ["tracks"]}, "live_set_now")
                    for t in ls.get("tracks") or []:
                        if t.get("id") == track_id:
                            m = re.fullmatch(r"t(\d+)", str(t.get("path") or ""))
                            return int(m.group(1)) if m else t.get("trackIndex")
                    return None

                midi = await call("ppal-create-track", {"path": "t+", "type": "midi",
                                                        "name": "CAPTURE 23 MIDI"}, "create_track_midi")
                audio = await call("ppal-create-track", {"path": "t+", "type": "audio",
                                                         "name": "CAPTURE 23 AUDIO"}, "create_track_audio")
                midi = midi[0] if isinstance(midi, list) else midi
                audio = audio[0] if isinstance(audio, list) else audio
                mid, aid = midi.get("id"), audio.get("id")
                n = index_of(midi, mid)
                n = n if n is not None else await track_index(mid)
                a = await track_index(aid)
                t = "t%d" % n

                async def readback(label):
                    return await call("ppal-read-track", {"path": t, "include": [
                        "arrangement-clips", "timing", "notes"]}, "read_" + label)

                def clip_at(track, bar):
                    # 2.3 reports the position inside the path ('t10[9|1]'); 2.2 had arrangementStart
                    for c in track.get("arrangementClips") or []:
                        m = re.search(r"\[(\d+)\|", str(c.get("path") or ""))
                        start = m.group(1) if m else str(c.get("arrangementStart", "")).split("|")[0]
                        if start == str(bar):
                            return c.get("id")
                    return None

                notes = json.dumps([{"p": 60, "t": 0, "d": 1, "v": 100},
                                    {"p": 62, "t": 4, "d": 1, "v": 100},
                                    {"p": 64, "t": 8, "d": 1, "v": 100},
                                    {"p": 65, "t": 12, "d": 1, "v": 100}])
                # A: a 4-bar looping clip at bar 1, created on an arrangement path
                a_clip = await call("ppal-create-clip", {"path": t + "[1|1]", "length": "4bar",
                                    "looping": True, "name": "A", "notes": notes},
                                    "create_clip_arrangement_path")
                a_clip = a_clip[0] if isinstance(a_clip, list) else a_clip
                a_id = a_clip.get("id")
                await readback("after_create")
                # plain copy to bar 9; trimmed copy (2 bars) to 17; copy OF the trimmed copy to 25
                await call("ppal-duplicate", {"type": "clip", "id": a_id, "toPath": t + "[9|1]"},
                           "duplicate_to_position")
                trim = await call("ppal-duplicate", {"type": "clip", "id": a_id,
                                  "toPath": t + "[17|1]", "arrangementLength": "2bar"},
                                  "duplicate_trimmed")
                trim = trim[0] if isinstance(trim, list) else trim
                if trim.get("id"):
                    await call("ppal-duplicate", {"type": "clip", "id": trim["id"],
                               "toPath": t + "[25|1]"}, "duplicate_of_trimmed")
                await readback("after_copies")
                # split trap: B at 35 (4 bars); trimmed copy of A (2 bars) dropped at 33
                await call("ppal-duplicate", {"type": "clip", "id": a_id, "toPath": t + "[35|1]"},
                           "trap_place_b")
                before_trap = await readback("before_trap")
                await call("ppal-duplicate", {"type": "clip", "id": a_id, "toPath": t + "[33|1]",
                           "arrangementLength": "2bar"}, "trap_trimmed_drop")
                await readback("after_trap")
                # a 1-bar clip at 42, fully inside a 4-bar copy dropped at 41
                await call("ppal-create-clip", {"path": t + "[42|1]", "length": "1bar",
                           "name": "C", "notes": json.dumps([{"p": 67, "t": 0, "d": 1, "v": 90}])},
                           "inside_place_c")
                await call("ppal-duplicate", {"type": "clip", "id": a_id, "toPath": t + "[41|1]"},
                           "inside_drop_over_c")
                after_inside = await readback("after_inside")
                # move (update-clip toPath) and split (arrangementSplit)
                d_id = clip_at(after_inside, 9)
                if d_id:
                    await call("ppal-update-clip", {"id": d_id, "toPath": t + "[49|1]"}, "move_clip")
                e = await call("ppal-duplicate", {"type": "clip", "id": a_id, "toPath": t + "[57|1]"},
                               "split_place_e")
                e = e[0] if isinstance(e, list) else e
                if e.get("id"):
                    await call("ppal-update-clip", {"id": e["id"], "arrangementSplit": "59|1"},
                               "split_clip")
                after_split = await readback("after_move_split")
                # deletes: by arrangement path, by id, a missing id
                await call("ppal-delete", {"type": "clip", "path": t + "[49|1]"}, "delete_by_path")
                c25 = clip_at(after_split, 25)
                if c25:
                    await call("ppal-delete", {"type": "clip", "id": c25}, "delete_by_id")
                await call("ppal-delete", {"type": "clip", "id": "999999"}, "delete_missing_id")
                # MIDI clip onto an audio track: must refuse and write nothing
                if a is not None:
                    await call("ppal-duplicate", {"type": "clip", "id": a_id,
                               "toPath": "t%d[1|1]" % a}, "duplicate_midi_to_audio")
                    await call("ppal-read-track", {"path": "t%d" % a, "include": [
                               "arrangement-clips"]}, "read_audio_after_refusal")
                # 2.2 forms on a 2.3 device: still honoured?
                await call("ppal-duplicate", {"type": "clip", "id": a_id, "toPath": t,
                           "arrangementStart": "65|1"}, "legacy_duplicate_arrangementStart")
                await call("ppal-create-clip", {"path": t, "arrangementStart": "69|1",
                           "length": "1bar", "notes": notes}, "legacy_create_arrangementStart")
                await call("ppal-read-track", {"trackIndex": n, "include": ["arrangement-clips"]},
                           "legacy_read_track_trackIndex")
                await readback("after_legacy")
                # track mute by id and by path; returns and main track by path
                await call("ppal-update-track", {"id": mid, "mute": True}, "mute_by_id")
                await call("ppal-update-track", {"path": t, "mute": False}, "unmute_by_path")
                await call("ppal-read-track", {"path": "rt0", "include": ["devices", "mixer"]},
                           "read_return_rt0")
                await call("ppal-read-track", {"path": "mt", "include": ["devices", "mixer"]},
                           "read_main_mt")
                # loop brace / start time round trip (bar|beat) and the Live API view of it
                await call("ppal-playback", {"action": "update-arrangement", "loop": True,
                           "loopStart": "5|1", "loopEnd": "9|1", "startTime": "5|1"},
                           "brace_set")
                if "ppal-live-api" in tools:
                    sys.path.insert(0, str(ROOT / "mcp_server"))
                    from pp_session import build_live_api_request
                    await call("ppal-live-api", build_live_api_request("live_set", [
                        {"type": "getProperty", "property": p}
                        for p in ("loop", "loop_start", "loop_length", "current_song_time")]),
                        "brace_live_api")
                await call("ppal-playback", {"action": "update-arrangement", "loop": False},
                           "brace_off")
                # cleanup: both capture tracks, by id
                await call("ppal-delete", {"type": "track", "id": ",".join(
                           x for x in (mid, aid) if x)}, "delete_capture_tracks")
                await call("ppal-read-live-set", {"include": ["tracks"]}, "live_set_after_cleanup")
            elif mode == "v23b":
                # The 2.3 gaps the b10 review listed: scene append/play by path, selection
                # path, take-lane read/duplicate/delete shapes, structural refusal replies.
                # Writes ONLY on tracks/scenes it creates in a Set saved as SCRATCH*.als,
                # and removes them at the end.
                if not set_name.upper().startswith("SCRATCH"):
                    print("REFUSED: v23b mode needs the open Set SAVED as SCRATCH*.als "
                          "(got %r). Nothing was changed." % (set_name or "unsaved"))
                    return 2

                async def call(tool, args, label):
                    body = Recorder.first_json(await rec.call(tool, args, "v23b_" + label))
                    return body if body is not None else {}

                def one(reply):
                    return reply[0] if isinstance(reply, list) and reply else reply

                def tindex(reply):
                    m = re.fullmatch(r"t(\d+)", str((one(reply) or {}).get("path") or ""))
                    return int(m.group(1)) if m else None

                notes = json.dumps([{"p": 60, "t": 0, "d": 1, "v": 100},
                                    {"p": 64, "t": 2, "d": 1, "v": 100}])
                ls0 = await call("ppal-read-live-set", {"include": ["scenes"]}, "live_set_scenes_before")
                scenes_before = len(ls0.get("scenes") or []) or ls0.get("sceneCount") or 0
                midi = one(await call("ppal-create-track", {"path": "t+", "type": "midi",
                                      "name": "CAPTURE 23B MIDI"}, "create_track_midi"))
                audio = one(await call("ppal-create-track", {"path": "t+", "type": "audio",
                                       "name": "CAPTURE 23B AUDIO"}, "create_track_audio"))
                n, a = tindex(midi), tindex(audio)
                t = "t%d" % n
                # scenes: append by path, read it, play it by path, stop, delete it
                scene = one(await call("ppal-create-scene", {"path": "s+", "count": 1,
                                       "capture": False, "name": "CAPTURE 23B"}, "create_scene_append"))
                ls1 = await call("ppal-read-live-set", {"include": ["scenes"]}, "live_set_scenes_after")
                last = (len(ls1.get("scenes") or []) or ls1.get("sceneCount") or scenes_before + 1) - 1
                await call("ppal-read-scene", {"path": "s%d" % last}, "read_scene_by_path")
                await call("ppal-playback", {"action": "play-scene", "path": "s%d" % last},
                           "play_scene_by_path")
                await call("ppal-playback", {"action": "stop"}, "stop")
                # refusals: MIDI clip on an audio track; a slot past the last scene
                if a is not None:
                    await call("ppal-create-clip", {"path": "t%d[1|1]" % a, "length": "1bar",
                               "notes": notes}, "refuse_create_midi_on_audio")
                await call("ppal-create-clip", {"path": "%s/s%d" % (t, last + 5), "length": "1bar",
                           "notes": notes}, "refuse_create_past_last_scene")
                # an arrangement clip: read by id, select it, read the selection
                main = one(await call("ppal-create-clip", {"path": t + "[1|1]", "length": "2bar",
                                      "looping": True, "name": "M", "notes": notes},
                                      "create_main_clip"))
                if main.get("id"):
                    await call("ppal-read-clip", {"id": main["id"], "include": ["timing", "notes"]},
                               "read_arrangement_clip_by_id")
                    await call("ppal-select", {"id": main["id"], "view": "arrangement"},
                               "select_arrangement_clip")
                    await call("ppal-select", {}, "selection_readback")
                # take lanes: create on lane 0, copy the main clip onto lane 1, read, promote, delete
                lane = one(await call("ppal-create-clip", {"path": t + "/l0[5|1]", "length": "1bar",
                                      "name": "L0", "notes": notes}, "create_lane_clip"))
                if main.get("id"):
                    await call("ppal-duplicate", {"type": "clip", "id": main["id"],
                               "toPath": t + "/l1"}, "duplicate_to_lane")
                await call("ppal-read-track", {"path": t, "include": ["arrangement-clips", "timing"]},
                           "read_track_with_lanes")
                if lane.get("id"):
                    await call("ppal-duplicate", {"type": "clip", "id": lane["id"],
                               "toPath": t + "[9|1]"}, "promote_lane_clip")
                    await call("ppal-delete", {"type": "clip", "id": lane["id"]}, "delete_lane_clip")
                await call("ppal-read-track", {"path": t, "include": ["arrangement-clips", "timing"]},
                           "read_track_after_lane_ops")
                # cleanup: every scene this run caused — the appended one AND the ones 2.3
                # creates silently for a slot past the last scene — by id, then both tracks
                before_ids = {sc.get("id") for sc in ls0.get("scenes") or []}
                ls2 = await call("ppal-read-live-set", {"include": ["scenes"]}, "live_set_scenes_cleanup")
                made = [sc.get("id") for sc in ls2.get("scenes") or [] if sc.get("id") not in before_ids]
                if made:
                    await call("ppal-delete", {"type": "scene", "id": ",".join(made)}, "delete_scene")
                await call("ppal-delete", {"type": "track", "id": ",".join(
                           x for x in ((midi or {}).get("id"), (audio or {}).get("id")) if x)},
                           "delete_capture_tracks")
                await call("ppal-read-live-set", {"include": ["tracks", "scenes"]},
                           "live_set_after_cleanup")
            elif mode == "v24":
                # Producer Pal 2.4: the shapes the migration guide says changed, captured on the
                # real device. Writes ONLY on tracks/scenes/devices this run creates in a Set saved
                # as SCRATCH*.als, and removes them at the end. Per-target entries, ok:false reads,
                # delete shapes, create-clip replacing an occupied slot, duplicate overwrite,
                # minimised write results, batch create/read, device paths (inst, d+), deprecated
                # forms (deviceName, count), take lanes, select by arrangement position, the
                # Live API "does not exist" text for a missing id.
                if not set_name.upper().startswith("SCRATCH"):
                    print("REFUSED: v24 mode needs the open Set SAVED as SCRATCH*.als "
                          "(got %r). Nothing was changed." % (set_name or "unsaved"))
                    return 2

                async def call(tool, args, label):
                    body = Recorder.first_json(await rec.call(tool, args, "v24_" + label))
                    return body if body is not None else {}

                def one(reply):
                    return reply[0] if isinstance(reply, list) and reply else reply

                def tindex(reply):
                    m = re.fullmatch(r"t(\d+)", str((one(reply) or {}).get("path") or ""))
                    return int(m.group(1)) if m else None

                def ids_of(reply):
                    return [str(x.get("id")) for x in (reply if isinstance(reply, list) else [reply])
                            if isinstance(x, dict) and x.get("id")]

                notes = json.dumps([{"p": 60, "t": 0, "d": 1, "v": 100},
                                    {"p": 64, "t": 2, "d": 1, "v": 100}])
                notes2 = json.dumps([{"p": 67, "t": 0, "d": 0.5, "v": 90}])
                ls0 = await call("ppal-read-live-set", {"include": ["scenes", "tracks"]}, "live_set_before")
                if not isinstance(ls0.get("scenes"), list) or not isinstance(ls0.get("tracks"), list):
                    # the cleanup diffs against these ids: without them it would delete everything
                    print("REFUSED: the first read did not list scenes and tracks (%r); nothing was changed."
                          % sorted(ls0)[:8])
                    return 2
                scene_ids_before = {sc.get("id") for sc in ls0["scenes"]}
                track_ids_before = {t.get("id") for t in ls0["tracks"]}
                await call("ppal-read-live-set", {"include": ["*"]}, "live_set_star")
                # Everything below runs under try/finally: a Python-level surprise in a 2.4 reply must
                # not leave CAPTURE tracks or scenes in the Set (the recorded fixtures stay either way).
                try:
                    # batch create (2.4 form) + the deprecated count form
                    both = await call("ppal-create-track", {"path": "t+,t+", "type": "midi",
                                      "name": "CAPTURE 24 A,CAPTURE 24 B"}, "create_tracks_batch")
                    dep = await call("ppal-create-track", {"count": 1, "type": "audio",
                                     "name": "CAPTURE 24 AUDIO"}, "create_track_count_deprecated")
                    made = ids_of(both) + ids_of(dep)
                    ls1 = await call("ppal-read-live-set", {"include": ["tracks"]}, "live_set_after_create")
                    by_id = {t.get("id"): t for t in ls1.get("tracks") or []}
                    ta = by_id.get(ids_of(both)[0] if ids_of(both) else None, {}).get("path")
                    tb = by_id.get(ids_of(both)[1] if len(ids_of(both)) > 1 else None, {}).get("path")
                    tau = by_id.get(ids_of(dep)[0] if ids_of(dep) else None, {}).get("path")
                    if not ta:
                        print("no capture track created; stopping before any further write")
                        return 2
                    # batch read + ok:false shapes
                    await call("ppal-read-track", {"path": ",".join(p for p in (ta, tb) if p)}, "read_tracks_batch")
                    await call("ppal-read-track", {"path": "t999"}, "read_track_missing")
                    await call("ppal-read-clip", {"path": ta + "/s0"}, "read_clip_empty_slot")
                    await call("ppal-read-clip", {"id": "999999"}, "read_clip_missing_id")
                    await call("ppal-read-clip", {"id": "999999,999998"}, "read_clips_missing_batch")
                    # session clip: create, then create AGAIN in the same slot (2.4 replaces)
                    c1 = one(await call("ppal-create-clip", {"path": ta + "/s0", "length": "1bar",
                                        "name": "S0", "notes": notes}, "create_session_clip"))
                    await call("ppal-create-clip", {"path": ta + "/s0", "length": "1bar", "name": "S0 again",
                               "notes": notes2}, "create_session_clip_replaces")
                    await call("ppal-read-clip", {"path": ta + "/s0", "include": ["notes"]}, "read_replaced_slot")
                    # arrangement: create, update notes (minimised result?), duplicate over it (overwrite)
                    m1 = one(await call("ppal-create-clip", {"path": ta + "[1|1]", "length": "2bar", "looping": True,
                                        "name": "M1", "notes": notes}, "create_arrangement_clip"))
                    m2 = one(await call("ppal-create-clip", {"path": ta + "[5|1]", "length": "2bar",
                                        "name": "M2", "notes": notes2}, "create_arrangement_clip_2"))
                    if m1.get("id"):
                        await call("ppal-update-clip", {"id": m1["id"], "notes": notes2}, "update_clip_notes")
                        await call("ppal-update-clip", {"id": m1["id"], "name": "M1 renamed"}, "update_clip_name_only")
                        await call("ppal-read-clip", {"id": m1["id"], "include": ["notes", "timing"]}, "read_arrangement_clip")
                        await call("ppal-duplicate", {"type": "clip", "id": m1["id"], "toPath": ta + "[5|1]"},
                                   "duplicate_over_existing")
                        await call("ppal-duplicate", {"type": "clip", "id": m1["id"] + "," + (m2.get("id") or "0"),
                                   "toPath": ta + "[9|1]"}, "duplicate_list_mismatch_refused")
                        await call("ppal-duplicate", {"type": "clip", "id": m1["id"], "toPath": ta + "[13|1]",
                                   "arrangementLength": "1bar"}, "duplicate_trimmed")
                        await call("ppal-update-clip", {"id": m1["id"], "toPath": ta + "[17|1]"}, "move_clip")
                    if m2.get("id"):
                        # split: the key we use (arrangementSplit) and the deprecated one (split)
                        await call("ppal-update-clip", {"id": m2["id"], "arrangementSplit": "6|1"}, "split_clip")
                        await call("ppal-read-track", {"path": ta, "include": ["arrangement-clips"]}, "read_track_after_split")
                        m2b = one(await call("ppal-create-clip", {"path": ta + "[25|1]", "length": "2bar", "name": "M2b",
                                             "notes": notes2}, "create_arrangement_clip_for_deprecated_split"))
                        if m2b.get("id"):
                            await call("ppal-update-clip", {"id": m2b["id"], "split": "26|1"}, "split_clip_deprecated_key")
                    await call("ppal-read-track", {"path": ta, "include": ["arrangement-clips", "session-clips"]},
                               "read_track_after_clip_ops")
                    await call("ppal-select", {"path": ta + "[5|1]"}, "select_arrangement_position")
                    await call("ppal-select", {}, "selection_readback")
                    await call("ppal-select", {"path": ta + "," + (tb or ta)}, "select_list_refused")
                    # take lanes: append a lane, create on it, duplicate the track arrangement onto a lane
                    await call("ppal-update-track", {"path": ta + "/l+", "name": "LANE A"}, "lane_append")
                    await call("ppal-create-clip", {"path": ta + "/l0[1|1]", "length": "1bar", "name": "L0",
                               "notes": notes}, "create_lane_clip")
                    await call("ppal-read-track", {"path": ta + "/l0", "include": ["arrangement-clips"]}, "read_lane")
                    if tb:
                        await call("ppal-duplicate", {"type": "track", "id": ids_of(both)[0], "toPath": tb + "/l0"},
                                   "duplicate_track_onto_lane")
                    # devices: new `device` param, deprecated `deviceName`, path by type, d+, refused param
                    dv = one(await call("ppal-create-device", {"path": ta, "device": "Utility"}, "create_device_new_param"))
                    await call("ppal-create-device", {"path": ta + "/d+", "deviceName": "Compressor"},
                               "create_device_deprecated_name_append")
                    await call("ppal-read-device", {"path": ta + "/afx0", "include": ["params", "param-values"]},
                               "read_device_by_type_path")
                    await call("ppal-read-track", {"path": ta, "include": ["devices"]}, "read_track_devices")
                    if dv.get("id"):
                        await call("ppal-update-device", {"id": dv["id"], "params": [{"name": "Gain", "value": -3},
                                   {"name": "NoSuchParam", "value": 1}]}, "update_device_param_batch_one_bad")
                        await call("ppal-update-device", {"id": dv["id"], "gainDb": -6}, "update_device_refused_param")
                        await call("ppal-delete", {"type": "device", "id": dv["id"]}, "delete_device")
                    # sends: by return letter and by return name (the forms the sidekick uses), then a missing return
                    returns = ls0.get("returnTracks") or []
                    if returns:
                        rname = str(returns[0].get("name") or "")
                        await call("ppal-update-track", {"path": ta, "sends": [{"return": rname[:1], "gainDb": -6}]},
                                   "update_track_send_by_letter")
                        await call("ppal-update-track", {"path": ta, "sends": [{"return": rname, "gainDb": -3}]},
                                   "update_track_send_by_name")
                        await call("ppal-read-track", {"path": ta, "include": ["sends"]}, "read_track_sends")
                    # sends: a return that does not exist
                    await call("ppal-update-track", {"path": ta, "sends": [{"return": "NO SUCH RETURN", "gainDb": -6}]},
                               "update_track_send_missing_return")
                    await call("ppal-update-track", {"path": ta, "name": "CAPTURE 24 A renamed", "mute": True},
                               "update_track_minimised")
                    await call("ppal-update-track", {"path": ta, "mute": False}, "update_track_unmute")
                    # sidechain source: the param name on 2.4 (RoutingTypeId dropped its Id — did this?) and the
                    # refusal for a track with no audio-bearing devices
                    sc = one(await call("ppal-create-device", {"path": ta, "device": "Compressor", "name": "CAP24 sidechain",
                                        "params": [{"name": "S/C On", "value": "On"}]}, "create_compressor_for_sidechain"))
                    if sc.get("id"):
                        for label, tid in (("audio_track", ids_of(dep)[0] if ids_of(dep) else None),
                                           ("midi_track_no_instrument", ids_of(both)[1] if len(ids_of(both)) > 1 else None)):
                            if tid:
                                await call("ppal-update-device", {"id": sc["id"], "params": [
                                    {"name": "sidechainSourceTrackId", "value": tid}]}, "update_device_sidechain_" + label)
                                await call("ppal-update-device", {"id": sc["id"], "params": [
                                    {"name": "sidechainSourceTrack", "value": tid}]}, "update_device_sidechain_noid_" + label)
                        await call("ppal-read-device", {"id": sc["id"], "include": ["params", "param-values"],
                                   "paramSearch": "sidechain"}, "read_device_sidechain_params")
                    # scenes: batch append, deprecated count, play by path, slot past the last scene
                    await call("ppal-create-scene", {"path": "s+,s+", "capture": False, "name": "CAP24 S1,CAP24 S2"},
                               "create_scenes_batch")
                    await call("ppal-create-scene", {"path": "s+", "count": 1, "capture": False, "name": "CAP24 S3"},
                               "create_scene_count_deprecated")
                    ls2 = await call("ppal-read-live-set", {"include": ["scenes"]}, "live_set_scenes_after")
                    last = len(ls2.get("scenes") or []) - 1
                    await call("ppal-playback", {"action": "play-scene", "path": "s%d" % max(last, 0)}, "play_scene_by_path")
                    await call("ppal-playback", {"action": "stop"}, "stop")
                    await call("ppal-create-clip", {"path": "%s/s%d" % (ta, last + 3), "length": "1bar", "notes": notes},
                               "create_clip_past_last_scene")
                    # deletes: by id (deletedPath), missing id (ok:false), by path, batch with one bad
                    if c1.get("id"):
                        await call("ppal-delete", {"type": "clip", "id": c1["id"]}, "delete_clip_by_id_already_replaced")
                    await call("ppal-delete", {"type": "clip", "path": ta + "[13|1]"}, "delete_clip_by_path")
                    await call("ppal-delete", {"type": "clip", "id": "999999"}, "delete_clip_missing_id")
                    if m2.get("id"):
                        await call("ppal-delete", {"type": "clip", "id": m2["id"] + ",999998"}, "delete_clips_batch_one_bad")
                    # Live API: the text of a missing id, and get_notes_extended still there
                    from pp_session import build_live_api_request
                    await rec.call("ppal-live-api", build_live_api_request("id 999999", [
                        {"type": "getProperty", "property": "name"}]), "v24_live_api_missing_id")
                    arr = await call("ppal-read-track", {"path": ta, "include": ["arrangement-clips"]}, "read_track_final")
                    first = ((arr.get("arrangementClips") or [{}])[0]).get("id")
                    if first:
                        await rec.call("ppal-live-api", build_live_api_request("id %s" % first, [
                            {"type": "getProperty", "property": "start_time"},
                            {"type": "call", "method": "get_notes_extended", "args": [0, 128, -100000, 200000]}]),
                            "v24_live_api_full_notes")
                finally:
                    # cleanup: scenes this run caused (by id), then every track this run created
                    ls3 = await call("ppal-read-live-set", {"include": ["scenes", "tracks"]}, "live_set_cleanup_read")
                    new_scenes = [sc.get("id") for sc in ls3.get("scenes") or [] if sc.get("id") not in scene_ids_before]
                    if new_scenes:
                        await call("ppal-delete", {"type": "scene", "id": ",".join(new_scenes)}, "delete_scenes")
                    new_tracks = [t.get("id") for t in ls3.get("tracks") or [] if t.get("id") not in track_ids_before]
                    if new_tracks:
                        await call("ppal-delete", {"type": "track", "id": ",".join(new_tracks)}, "delete_capture_tracks")
                    await call("ppal-read-live-set", {"include": ["tracks", "scenes"]}, "live_set_after_cleanup")
            elif mode == "legacy":
                # Does this device honour the argument forms older sidekick code
                # sends (slot / clipId / ids / create-clip trackIndex) or only the
                # schema's forms (path / id)? Reads run on any Set.
                tracks = live_set.get("tracks") or []
                t_idx = next((t.get("trackIndex", i) for i, t in enumerate(tracks)
                              if (t.get("sessionClipCount") or 0) > 0
                              and not t.get("hasProducerPalDevice")), None)
                if t_idx is not None:
                    tr = Recorder.first_json(await rec.call(
                        "ppal-read-track", {"trackIndex": t_idx, "include": ["session-clips"]},
                        "legacy_read_track")) or {}
                    sc = (tr.get("sessionClips") or [{}])[0]
                    cpath, cid = sc.get("path"), sc.get("id")
                    scene = cpath.split("/s")[-1] if cpath and "/s" in cpath else "0"
                    await rec.call("ppal-read-clip", {"path": cpath}, "legacy_read_clip_path")
                    await rec.call("ppal-read-clip", {"slot": "%s/%s" % (t_idx, scene)},
                                   "legacy_read_clip_slot")
                    await rec.call("ppal-read-clip", {"id": cid}, "legacy_read_clip_id")
                    await rec.call("ppal-read-clip", {"clipId": cid}, "legacy_read_clip_clipId")
                    await rec.call("ppal-read-clip", {}, "legacy_read_clip_no_locator")
                    await rec.call("ppal-read-clip", {"id": "999999"}, "legacy_read_clip_missing_id")
                    await rec.call("ppal-read-clip", {"path": "t%s/s%d" % (t_idx, 97)},
                                   "legacy_read_clip_empty_slot")
                if set_name.upper().startswith("SCRATCH"):
                    dup = Recorder.first_json(await rec.call(
                        "ppal-create-track", {"count": 1, "name": "SIDEKICK TRY legacy"},
                        "legacy_create_track")) or {}
                    ids = _created_ids(dup)
                    new_id = ids[0] if ids else None
                    ls2 = Recorder.first_json(await rec.call(
                        "ppal-read-live-set", {"include": ["tracks"]}, "legacy_live_set_after_create")) or {}
                    # version-agnostic: 2.2 lists trackIndex, 2.3 only the path 't10'
                    new_idx = next((_track_index(t) for t in ls2.get("tracks") or []
                                    if t.get("id") == new_id), None)
                    if new_idx is None and isinstance(dup, dict):
                        new_idx = _track_index(dup)
                    if new_id and new_idx is not None:
                        await rec.call("ppal-update-track", {"ids": new_id, "name": "LEGACY IDS"},
                                       "legacy_update_track_ids")
                        await rec.call("ppal-read-track", {"trackIndex": new_idx},
                                       "legacy_read_after_update_ids")
                        await rec.call("ppal-update-track", {"id": new_id, "name": "LEGACY ID"},
                                       "legacy_update_track_id")
                        await rec.call("ppal-read-track", {"trackIndex": new_idx},
                                       "legacy_read_after_update_id")
                        await rec.call("ppal-create-clip", {"slot": "%d/0" % new_idx, "length": "1bar",
                                       "notes": json.dumps([{"p": 60, "t": 0, "d": 1, "v": 100}])}, "legacy_create_clip_slot")
                        await rec.call("ppal-create-clip", {"path": "t%d/s1" % new_idx, "length": "1bar",
                                       "notes": json.dumps([{"p": 60, "t": 0, "d": 1, "v": 100}])}, "legacy_create_clip_path")
                        await rec.call("ppal-read-track", {"trackIndex": new_idx,
                                       "include": ["session-clips"]}, "legacy_read_after_create_clip")
                        # device checks for forms the code now sends (b4/pp-compat review):
                        await rec.call("ppal-read-clip", {"path": "t%d/s5" % new_idx},
                                       "check_read_clip_existing_empty_slot")
                        arr = Recorder.first_json(await rec.call(
                            "ppal-create-clip", {"path": "t%d" % new_idx, "arrangementStart": "3|1",
                                                 "length": "1bar",
                                                 "notes": json.dumps([{"p": 62, "t": 0, "d": 1, "v": 100}])},
                            "check_create_clip_arrangement")) or {}
                        await rec.call("ppal-read-track", {"trackIndex": new_idx,
                                       "include": ["arrangement-clips"]}, "check_read_after_arrangement_create")
                        await rec.call("ppal-select", {"path": "t%d/s0" % new_idx, "view": "session"},
                                       "check_select_path")
                        await rec.call("ppal-select", {}, "check_select_readback")
                        made = Recorder.first_json(await rec.call(
                            "ppal-read-track", {"trackIndex": new_idx, "include": ["session-clips"]},
                            "check_clips_before_clip_delete")) or {}
                        victim = ((made.get("sessionClips") or [{}])[0]).get("id")
                        if victim:
                            await rec.call("ppal-delete", {"type": "clip", "id": victim},
                                           "check_delete_clip_by_id")
                            await rec.call("ppal-read-track", {"trackIndex": new_idx,
                                           "include": ["session-clips"]}, "check_clips_after_clip_delete")
                        await rec.call("ppal-delete", {"type": "track", "ids": new_id},
                                       "legacy_delete_ids")
                    # Cleanup whatever the probes above did or skipped: every
                    # track this mode created must be gone (the SCRATCH Set is
                    # left as it was found), whichever reply shape the build has.
                    for created_id in ids:
                        still = Recorder.first_json(await rec.call(
                            "ppal-read-live-set", {"include": ["tracks"]},
                            "legacy_live_set_after_delete_ids")) or {}
                        if any(t.get("id") == created_id for t in still.get("tracks") or []):
                            await rec.call("ppal-delete", {"type": "track", "id": created_id},
                                           "legacy_delete_id")
                else:
                    print("legacy write probes skipped: open a Set saved as SCRATCH*.als for them")
            else:
                if not set_name.upper().startswith("SCRATCH"):
                    print("REFUSED: mutate mode needs the open Set SAVED as SCRATCH*.als "
                          "(got %r). Nothing was changed." % (set_name or "unsaved"))
                    return 2
                # 2.2 addresses objects by id (read them first); 2.3 accepts paths.
                # source = first MIDI track with a session clip and no Producer Pal device
                src_index = next((t.get("trackIndex", i) for i, t in enumerate(
                    live_set.get("tracks") or [])
                    if (t.get("sessionClipCount") or 0) > 0
                    and not t.get("hasProducerPalDevice") and t.get("type") == "midi"), 0)
                src = Recorder.first_json(await rec.call(
                    "ppal-read-track", {"trackIndex": src_index,
                                        "include": ["session-clips", "notes"]},
                    "read_track_source")) or {}
                clips = src.get("sessionClips") or []
                src_id, clip_id = src.get("id"), (clips[0].get("id") if clips else None)
                dup = Recorder.first_json(await rec.call(
                    "ppal-duplicate", {"type": "track", "id": src_id, "withoutClips": True,
                                       "name": "SIDEKICK TRY capture"},
                    "duplicate_track_without_clips")) or {}
                new_id = dup.get("id")
                await rec.call("ppal-update-track", {"id": new_id, "mute": True},
                               "update_track_mute")
                await rec.call("ppal-update-track", {"id": new_id, "mute": False},
                               "update_track_unmute")
                await rec.call("ppal-playback", {"action": "update-arrangement", "loop": True,
                               "loopStart": "5|1", "loopEnd": "9|1", "startTime": "5|1"},
                               "playback_update_arrangement")
                if clip_id:
                    await rec.call("ppal-duplicate", {"type": "clip", "id": clip_id,
                                   "toPath": "t%d" % (src_index + 1), "arrangementStart": "5|1",
                                   "arrangementLength": "2bar"}, "duplicate_clip_to_arrangement")
                await rec.call("ppal-read-track", {"trackIndex": src_index + 1, "include":
                               ["arrangement-clips", "notes"]}, "read_track_after_dup")
                await rec.call("ppal-read-track", {"trackIndex": 999}, "error_missing_track")
                if new_id:
                    await rec.call("ppal-delete", {"type": "track", "id": new_id}, "delete_track")
                await rec.call("ppal-playback", {"action": "update-arrangement", "loop": False},
                               "playback_loop_off")
            print("pp_version:", version, "| set:", set_name)
            print("saved %d fixtures under %s" % (len(rec.saved) + 2, out.relative_to(ROOT)))
            return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("read", "mutate", "legacy", "piece", "piece-notes", "v23", "v23b", "v24"):
        sys.exit(__doc__)
    sys.exit(asyncio.run(main(mode)))
