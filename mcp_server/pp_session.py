"""Producer Pal transport/session adapter — operation-scoped, fail-closed.

Owns connect/initialize/ppal-connect ceremony, tool-call isError handling,
and readback verification helpers. Does not claim process-long session safety.
"""

from __future__ import annotations

import asyncio
import os
import json
import sys
import re
import math
import shutil
import subprocess
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

# Script dir on path so bare `import pp_codec` works under package import too.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pp_codec  # noqa: E402
import clip_playback  # noqa: E402
from config import PP_URL  # noqa: E402,F401  (re-exported: live_write reads pp_session.PP_URL)

# ONE module universe ('pp_session' vs 'mcp_server.pp_session').
for _alias in ("pp_session", "mcp_server.pp_session"):
    sys.modules.setdefault(_alias, sys.modules[__name__])


# PP 2.0+ JSON is the supported wire contract. This explicit compatibility switch
# is off; only a deliberately marked PP<2.0 fixture shim may temporarily enable it.
LEGACY_TEXT_RESPONSE_MODE = False
PP_FORMAT = "json"
PP_NOTATION = pp_codec.CodecMode.MIDI_JSON.value
PP_NOTATION_HEADER = "x-producer-pal-notation"
PP_VERSION_UNKNOWN = "unknown"
# Only builds with committed device captures may pass the mutation guard.
SUPPORTED_PP_VERSIONS = frozenset({"2.2.0-rc1", "2.3.0", "2.4.0"})
# Request/response addressing generations. 2.2 names tracks, scenes and
# arrangement positions with numeric arguments (trackIndex, sceneIndex,
# arrangementStart); 2.3 puts them in the object path ('t3', 's2', 't3[5|1]',
# 'rt0', 'mt') and removed the numeric arguments. 2.4 keeps the paths and
# drops `count` from create-track / create-scene (tests/fixtures/pp/2.4.0/
# tool_schemas.json): a batch is one path per object, 't+,t+' / 's+,s+'
# (v24_create_tracks_batch, v24_create_scenes_batch), and single-target
# failures are MCP errors where 2.3 sent `[]` + WARNING.
ADDRESSING_22 = "2.2"
ADDRESSING_23 = "2.3"
ADDRESSING_24 = "2.4"
ADDRESSINGS = (ADDRESSING_22, ADDRESSING_23, ADDRESSING_24)
# The generations whose positions live in the object path.
PATH_ADDRESSINGS = (ADDRESSING_23, ADDRESSING_24)
SESSION_CLIP_KEYS = ("sessionClips", "session-clips", "session_clips", "clips")
ARRANGEMENT_CLIP_KEYS = ("arrangementClips", "arrangement-clips", "arrangement_clips")
# Producer Pal 2.x read-track include flags used by inventory/delete verification.
TRACK_READ_INCLUDE_INVENTORY = frozenset({
    "session-clips", "arrangement-clips", "notes", "timing",
})
TRACK_READ_INCLUDE_SESSION = frozenset({"session-clips"})

# The `include` vocabulary of each read tool per build — the enum of
# inputSchema.properties.include.items in that build's captured
# tool_schemas.json (tests assert the equality). An include outside it is
# refused by the MCP client before the device sees the call — "MCP error
# -32602: Input validation error: … Invalid option: expected one of …"
# (v24_read_track_sends: `sends` is not an include on any build; sends come
# with `mixer`). 2.3 added `warp` to read-track; 2.4 changed nothing.
_READ_INCLUDE_22 = {
    "ppal-read-track": frozenset({"session-clips", "arrangement-clips", "notes", "timing",
                                  "sample", "devices", "drum-map", "routings",
                                  "available-routings", "mixer", "color", "*"}),
    "ppal-read-clip": frozenset({"sample", "notes", "color", "timing", "warp", "*"}),
    "ppal-read-live-set": frozenset({"tracks", "scenes", "routings", "mixer", "color",
                                     "locators", "*"}),
    "ppal-read-scene": frozenset({"clips", "notes", "sample", "timing", "warp", "color", "*"}),
    "ppal-read-device": frozenset({"actions", "chains", "drum-map", "drum-pads", "params",
                                   "param-values", "return-chains", "sample", "options", "*"}),
}
_READ_INCLUDE_23 = {**_READ_INCLUDE_22,
                    "ppal-read-track": _READ_INCLUDE_22["ppal-read-track"] | {"warp"}}
READ_INCLUDE_VOCABULARY = {
    "2.2": _READ_INCLUDE_22,
    "2.3": _READ_INCLUDE_23,
    "2.4": _READ_INCLUDE_23,
}


def read_include_vocabulary(addressing: str, tool: str) -> frozenset:
    """The include flags ``tool`` accepts on the ``addressing`` generation."""
    by_tool = READ_INCLUDE_VOCABULARY.get(str(addressing))
    if by_tool is None or tool not in by_tool:
        raise ValueError("no include vocabulary for %s on Producer Pal %s" % (tool, addressing))
    return by_tool[tool]


def require_known_include(addressing: str, tool: str, include) -> list:
    """``include`` as a list, every flag in the build's vocabulary — or a
    ValueError naming the flag, raised here instead of the MCP client's
    -32602 refusal (``is_schema_refusal``) one round trip later."""
    flags = [str(flag) for flag in (include or [])]
    unknown = sorted(set(flags) - read_include_vocabulary(addressing, tool))
    if unknown:
        raise ValueError("%s has no include %s on Producer Pal %s (allowed: %s)" % (
            tool, ", ".join(unknown), addressing,
            ", ".join(sorted(read_include_vocabulary(addressing, tool)))))
    return flags

_PP_VERSION_RE = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:-(?P<pre>[0-9A-Za-z.-]+))?$")
_BARE_OBJECT_KEY_RE = re.compile(
    r"(?P<prefix>[{,]\s*)(?P<key>[A-Za-z_$][A-Za-z0-9_$]*)(?P<suffix>\s*:)",
)
LIVE_API_OPERATIONS = frozenset({
    "get", "set", "set_property", "call", "goto", "info", "getstring", "getcount",
    "getProperty", "getChildIds", "exists", "getColor", "setColor",
    "get_property", "set_path", "set_mode", "set_id", "call_method",
})
_PROPERTY_OPS = frozenset({"get", "set", "set_property", "get_property",
                           "getProperty", "getChildIds", "getcount", "getstring"})
_METHOD_OPS = frozenset({"call", "call_method"})
_VALUE_OPS = frozenset({"set", "set_property", "goto", "setColor", "set_path",
                        "set_mode", "set_id"})


def run_coro_blocking(coro):
    """Run a coroutine from sync code, on or off a running event loop.

    FastMCP runs sync tools directly on its event loop, so asyncio.run would
    fail there; offload to a helper thread instead and propagate exceptions.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict[str, Any] = {}

    def runner():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # propagate out of the helper thread
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


class PPProtocolError(RuntimeError):
    """Producer Pal returned a response outside the selected JSON contract."""


def _validated_pp_version(value) -> tuple[str, bool, bool]:
    """Return canonical version, base-2.x support, and 2.2+ capability."""
    if value is None:
        return PP_VERSION_UNKNOWN, False, False
    text = str(value).strip()
    match = _PP_VERSION_RE.fullmatch(text)
    if not match:
        raise PPProtocolError("malformed Producer Pal version: %r" % value)
    minor = int(match.group("minor"))
    major = int(match.group("major"))
    return text, major == 2, major == 2 and minor >= 2


def _capability_evidence(result) -> set[str]:
    """Collect only explicit capability/tool declarations, never object paths."""
    found = set()
    for node in _walk(result):
        if not isinstance(node, dict):
            continue
        for key in ("capabilities", "capability", "tools", "toolNames", "tool_names"):
            values = node.get(key)
            if isinstance(values, dict):
                found.update(str(k) for k, enabled in values.items() if enabled)
            elif isinstance(values, (list, tuple, set)):
                for value in values:
                    if isinstance(value, dict):
                        name = value.get("name") or value.get("tool") or value.get("id")
                        if name is not None and value.get("enabled", True):
                            found.add(str(name))
                    else:
                        found.add(str(value))
            elif isinstance(values, str):
                found.add(values)
    return found


def _live_path(path: str):
    """Parse only Producer Pal's compact object paths (track, scene, or lane)."""
    return pp_codec.parse_live_object_path(path)


def addressing_for_version(version) -> str:
    """2.4 for Producer Pal 2.4+, 2.3 for 2.3, else 2.2 (also for an unknown
    version: the forms every pre-2.3 caller and test double has always used)."""
    if version is None or version == PP_VERSION_UNKNOWN:
        return ADDRESSING_22
    match = _PP_VERSION_RE.fullmatch(str(version).strip())
    if not match:
        return ADDRESSING_22
    major, minor = int(match.group("major")), int(match.group("minor"))
    if (major, minor) >= (2, 4):
        return ADDRESSING_24
    return ADDRESSING_23 if (major, minor) >= (2, 3) else ADDRESSING_22


def normalize_addressing(value) -> str:
    """An addressing generation from "2.2" / "2.3" / "2.4" or from the build
    version ppal-connect reports ("2.2.0-rc1", "2.3.0", "2.4.0"). Anything
    else raises."""
    text = str(value).strip() if isinstance(value, str) else None
    if text in ADDRESSINGS:
        return text
    match = _PP_VERSION_RE.fullmatch(text) if text else None
    if match is None or int(match.group("major")) != 2 or int(match.group("minor")) < 2:
        raise ValueError("addressing must be \"2.2\", \"2.3\", \"2.4\" or a Producer Pal "
                         "2.2+ build version such as \"2.4.0\", got %r" % (value,))
    return addressing_for_version(text)


def addressing_of(session) -> str:
    """The addressing generation a session speaks: its handshake capability,
    else its handshake / ``pp_version``, else 2.2."""
    capabilities = getattr(session, "capabilities", None)
    if isinstance(capabilities, dict) and capabilities.get("addressing") in ADDRESSINGS:
        return capabilities["addressing"]
    handshake = getattr(session, "handshake", None)
    version = handshake.get("pp_version") if isinstance(handshake, dict) else None
    if version is None:
        version = getattr(session, "pp_version", None)
    return addressing_for_version(version)


def _capabilities(handshake: dict, evidence: set[str] | None = None) -> dict:
    version = handshake.get("pp_version", PP_VERSION_UNKNOWN)
    _, is_2x, is_22 = _validated_pp_version(
        None if version == PP_VERSION_UNKNOWN else version)
    explicit = {str(v) for v in (evidence or set())}
    live = "ppal-live-api" in explicit or "live_api" in explicit or "live-api" in explicit
    addressing = addressing_for_version(version) if is_2x else None
    return {
        "supports_2_x": is_2x,
        "producer_pal_2_2": is_22,
        "supports_2_2": is_22,
        # 2.3's path addressing, which 2.4 keeps (v24/connect.json + every v24 capture)
        "supports_2_3": addressing in PATH_ADDRESSINGS,
        "supports_2_4": addressing == ADDRESSING_24,
        "addressing": addressing,
        "live_api": bool(is_22 and live),
        "ppal-live-api": bool(is_22 and live),
    }


class PPResponse(str):
    """Raw-compatible response carrying the parsed PP JSON result and warnings.
    ``warning_only``: the device sent only WARNING parts and no result."""

    def __new__(cls, raw: str, result, warnings=None, is_json: bool = True,
                warning_only: bool = False):
        obj = str.__new__(cls, raw)
        obj.raw = raw
        obj.result = result
        obj.warnings = list(warnings or [])
        obj.is_json = bool(is_json)
        obj.warning_only = bool(warning_only)
        return obj


def _quote_bare_object_keys(raw: str) -> str:
    """Quote RC1 object keys outside JSON strings without evaluating code."""
    pieces = []
    outside_start = 0
    index = 0
    while index < len(raw):
        if raw[index] != '"':
            index += 1
            continue
        pieces.append(_BARE_OBJECT_KEY_RE.sub(
            lambda match: "%s%s%s" % (
                match.group("prefix"), json.dumps(match.group("key")),
                match.group("suffix")),
            raw[outside_start:index],
        ))
        string_start = index
        index += 1
        while index < len(raw):
            if raw[index] == "\\":
                index += 2
            elif raw[index] == '"':
                index += 1
                break
            else:
                index += 1
        pieces.append(raw[string_start:index])
        outside_start = index
    pieces.append(_BARE_OBJECT_KEY_RE.sub(
        lambda match: "%s%s%s" % (
            match.group("prefix"), json.dumps(match.group("key")),
            match.group("suffix")),
        raw[outside_start:],
    ))
    return "".join(pieces)


def _decode_structured_response(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError as strict_error:
        if not raw.lstrip().startswith(("{", "[")):
            raise strict_error
        normalized = _quote_bare_object_keys(raw)
        if normalized == raw:
            raise strict_error
        return json.loads(normalized)


def parse_tool_response(text: str | PPResponse) -> PPResponse:
    if isinstance(text, PPResponse):
        return text
    raw = str(text or "")
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        if LEGACY_TEXT_RESPONSE_MODE:
            return PPResponse(raw, raw, [], False)
        raise PPProtocolError(
            "Producer Pal JSON response required; switch Producer Pal to JSON output "
            "(device Setup → output format JSON)"
        ) from exc

    warnings = []
    result = decoded
    if isinstance(decoded, dict) and "result" in decoded:
        result = decoded.get("result")
        warnings = decoded.get("warnings") or []
    elif isinstance(decoded, dict) and "data" in decoded and "warnings" in decoded:
        result = decoded.get("data")
        warnings = decoded.get("warnings") or []
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            pass
    return PPResponse(raw, result, warnings, True)


class LiveWriteVerificationError(RuntimeError):
    """Read-back mismatch with enough context to recover from a partial write.

    ``created_track`` (``{"id", "track_index"}``) names a track the device
    DID create but the write could not place (an unexpected index) — the
    partial-write record a retry reconciles against; it is never deleted."""

    def __init__(self, context: str, sent: int, received: int | None,
                 completed: list[dict] | None = None, detail: str = "",
                 created_track: dict | None = None):
        self.failed_context = context
        self.sent = sent
        self.received = received
        self.completed = [dict(item) for item in (completed or [])]
        self.detail = detail
        self.created_track = dict(created_track) if created_track else None
        completed_text = ""
        if self.completed:
            labels = [
                str(item.get("name") or item.get("slot") or item.get("track_index") or "?")
                for item in self.completed
            ]
            completed_text = "; already written and verified: %s" % ", ".join(labels)
        super().__init__(
            f"{context} read-back mismatch: sent {sent} notes, received {received}"
            f"{'; ' + detail if detail else ''}"
            f"{completed_text}"
        )


class PPToolError(RuntimeError):
    """A Producer Pal tool failure (CallToolResult.isError). ``str()`` is
    exactly ``Producer Pal tool error: <device text>``; the device's
    ``WARNING: …`` parts ride on ``warnings``."""

    def __init__(self, text: str, warnings=None):
        self.text = text
        self.warnings = list(warnings or [])
        super().__init__("Producer Pal tool error: %s" % text)


class PPSchemaRefusal(PPToolError):
    """The MCP client refused the call's arguments against the tool schema
    before the device saw it (``MCP error -32602: Input validation error``,
    v24_read_track_sends). Never a Live fact — a sidekick bug to surface."""


def _is_warning_text(text) -> bool:
    return isinstance(text, str) and text.lstrip().upper().startswith("WARNING")


async def call_tool(session, name, args=None) -> PPResponse:
    """Call a Producer Pal tool; raise on CallToolResult.isError.

    A Producer Pal tool failure sets CallToolResult.isError and returns the
    error message as its text. Ignoring it let read_clip parse the error text
    as zero notes — fail loud so callers declare blind / abort writes.

    Producer Pal sends each warning as its own ``WARNING: …`` text part after
    the payload (2.2 and 2.3 captures). They are kept on ``PPResponse.warnings``
    — on 2.3 a refused write is a warning with an empty result, not an error
    (see ``write_failed``). Other text parts (connect's skills) are not warnings.
    """
    r = await session.call_tool(name, args or {})
    texts = [getattr(part, "text", None) for part in (getattr(r, "content", None) or [])]
    texts = [text for text in texts if isinstance(text, str)]
    body = next((text for text in texts if not _is_warning_text(text)), None)
    warnings = [text.strip() for text in texts if text is not body and _is_warning_text(text)]
    if getattr(r, "isError", False):
        text = body if body is not None else " | ".join(warnings)
        raise (PPSchemaRefusal if is_schema_refusal(text) else PPToolError)(text, warnings)
    if body is None and warnings:
        # Only WARNING parts: no result at all (a refusal the device did not
        # wrap in a JSON part). Kept as such — never "switch to JSON output".
        return PPResponse("", None, warnings, True, warning_only=True)
    parsed = parse_tool_response(body or "")
    parsed.warnings.extend(warnings)
    return parsed


class WarningLog:
    """A PPSession proxy remembering every device WARNING of one operation, so
    a receipt can say what the device warned (advisory: never a verdict)."""

    def __init__(self, session):
        self._session = session
        self.warnings: list[str] = []

    def __getattr__(self, name):
        return getattr(self._session, name)

    async def call(self, name, args=None):
        response = await self._session.call(name, args)
        for warning in getattr(response, "warnings", None) or []:
            if warning not in self.warnings:
                self.warnings.append(str(warning))
        return response

    async def call_unlogged(self, name, args=None):
        """A call whose WARNING parts are its own answer, not a device warning
        about the operation (a slot pre-read's "no clip at <path>" notice —
        the free-slot reply of 2.2): nothing is remembered."""
        return await self._session.call(name, args)


class PPSession:
    """Thin wrapper around an initialized, ppal-connected MCP ClientSession."""

    def __init__(self, session, handshake: dict | None = None):
        self._session = session
        self.handshake = dict(handshake or {})
        self.capabilities = dict(self.handshake.get("capabilities") or {})
        self._identity_cache: dict | None = None

    @property
    def pp_version(self) -> str:
        return self.handshake.get("pp_version", PP_VERSION_UNKNOWN)

    @property
    def addressing(self) -> str:
        return addressing_of(self)

    @property
    def runtime_epoch(self) -> str | None:
        cached = self._identity_cache
        return cached.get("runtime_epoch") if isinstance(cached, dict) else None

    def cache_set_identity(self, identity: dict) -> None:
        self._identity_cache = dict(identity)

    async def call(self, name: str, args: dict | None = None) -> PPResponse:
        return await call_tool(self._session, name, args)


def _result(response):
    return parse_tool_response(response).result


def _normalized_record() -> dict:
    """Create the stable record shape shared by Producer Pal wire versions."""
    return {
        "track_index": None,
        "return_index": None,
        "track_path": None,
        "scene_index": None,
        "device_index": None,
        "arrangement_start": None,
        "take_lane": None,
        "is_main": False,
        "loop": {"on": None, "start": None, "end": None},
        "playhead": None,
        "start_time": None,
        "deleted": False,
        "error_reason": None,
    }


def _path_indexes(path: str | None) -> tuple[int | None, int | None, int | None]:
    """Read indices from PP 2.3 paths, including return/master/device paths."""
    if not isinstance(path, str):
        return None, None, None
    match = re.fullmatch(r"t(\d+)(?:/([sld])(\d+))?", path)
    if match:
        kind, index = match.group(2), match.group(3)
        return (int(match.group(1)), int(index) if kind == "s" else None,
                int(index) if kind == "d" else None)
    if re.fullmatch(r"rt\d+", path) or path == "mt":
        return None, None, None
    return None, None, None


def normalize_track(obj: dict) -> dict:
    """Normalize a track record from captured 2.2 or documented 2.3 PP shapes."""
    value = obj if isinstance(obj, dict) else {}
    if isinstance(value.get("mainTrack"), dict):
        value = value["mainTrack"]
    elif isinstance(value.get("masterTrack"), dict):
        value = value["masterTrack"]
    result = _normalized_record()
    path = value.get("path")
    path_index, _, device_index = _path_indexes(path)
    valid_track_path = isinstance(path, str) and re.fullmatch(r"t\d+", path)
    return_match = re.fullmatch(r"rt(\d+)", path) if isinstance(path, str) else None
    index = value.get("trackIndex", path_index if valid_track_path else None)
    result.update(
        track_index=index if isinstance(index, int) and not isinstance(index, bool) else None,
        return_index=(value.get("returnTrackIndex") if value.get("returnTrackIndex") is not None
                      else int(return_match.group(1)) if return_match else None),
        track_path=path if valid_track_path else ("rt%d" % int(return_match.group(1))
                                                  if return_match else None),
        device_index=value.get("deviceIndex", device_index),
        is_main=(path == "mt" or value.get("type") == "master" or
                 bool(value.get("isMain", value.get("is_main", False)))),
    )
    return result


def normalize_clip(obj: dict, *, track_index=None) -> dict:
    """Normalize clip locations; raw takeLane remains unverified without a capture."""
    value = obj if isinstance(obj, dict) else {}
    result = _normalized_record()
    path = value.get("path")
    path_index, scene_index, device_index = _path_indexes(path)
    return_match = re.fullmatch(r"rt(\d+)", path) if isinstance(path, str) else None
    is_main = path == "mt"
    if path_index is None and isinstance(path, str):
        parent_match = re.match(r"^t(\d+)/", path)
        if parent_match:
            path_index = int(parent_match.group(1))
    if path_index is None and isinstance(path, str):
        match = re.match(r"^t(\d+)\[", path)
        if match:
            path_index = int(match.group(1))
    lane_match = re.fullmatch(r"t\d+/l(\d+)(?:\[([^\]]+)\])?", path) if isinstance(path, str) else None
    lane_index = int(lane_match.group(1)) if lane_match else None
    start = value.get("arrangementStart")
    if start is None and isinstance(path, str):
        match = re.fullmatch(r"t\d+\[([^\]]+)\]", path)
        if match:
            start = match.group(1)
    if start is None and lane_match and lane_match.group(2):
        start = lane_match.group(2)
    take_lane = value.get("takeLane")
    # takeLane has no captured fixture, so preserve it as unverified raw evidence.
    if take_lane is None and lane_index is not None:
        take_lane = lane_index
    index = value.get("trackIndex", track_index if track_index is not None else path_index)
    result.update(
        track_index=index if isinstance(index, int) and not isinstance(index, bool) else None,
        return_index=int(return_match.group(1)) if return_match else None,
        track_path=("t%d" % path_index if path_index is not None else None),
        scene_index=value.get("sceneIndex", scene_index),
        device_index=value.get("deviceIndex", device_index),
        arrangement_start=start,
        take_lane=take_lane,
        is_main=is_main,
    )
    return result


def clip_location_args(track_index: int, scene_index: int) -> dict:
    """Build the shared JSON read-clip location accepted by PP 2.2 and 2.3."""
    return {"path": "t%d/s%d" % (int(track_index), int(scene_index))}


def clip_locator(path: str | None = None, clip_id: str | None = None,
                 track: int | None = None, scene: int | None = None) -> dict:
    """Build one schema-listed Producer Pal clip locator (same on 2.2 and 2.3:
    a session slot path or a clip id)."""
    if path is not None:
        return {"path": str(path)}
    if clip_id is not None:
        return {"id": str(clip_id)}
    if track is not None and scene is not None:
        return clip_location_args(track, scene)
    raise ValueError("clip path or id, or track and scene, is required")


def ids_arg(ids) -> dict:
    """Build the schema's comma-separated `id` argument."""
    if isinstance(ids, str):
        value = ids
    else:
        value = ",".join(str(item) for item in ids)
    return {"id": value}


# --- field readers: one value from either wire generation (pure, in pp_codec) --
track_index_of = pp_codec.track_index_of
return_index_of = pp_codec.return_index_of
is_main_track = pp_codec.is_main_track
main_track_of = pp_codec.main_track_of
clip_track_index = pp_codec.clip_track_index
arrangement_start_of = pp_codec.arrangement_start_of
scene_index_of = pp_codec.scene_index_of
_BAR_BEAT_RE = pp_codec.BAR_BEAT_RE


def _flatten_objects(value) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _flatten_objects(child)]
    return []


def result_objects(response) -> list[dict]:
    """The object(s) a write replied with, flattened: a dict (one object,
    2.2 and 2.3), a list (2.3 multi-id delete, split) or a nested list."""
    return _flatten_objects(_result(response))


def created_track(response) -> dict | None:
    """``{"id", "track_index"}`` of a created/duplicated track: 2.2 replies
    carry ``trackIndex``, 2.3 the path 't10'. None without an id."""
    for item in result_objects(response):
        if isinstance(item.get("id"), str) and item["id"]:
            return {"id": item["id"], "track_index": track_index_of(item)}
    return None


# Warnings both devices send next to writes that HAPPENED and next to complete
# reads (from the warn(...) calls in the 2.2.0-rc1 and 2.3.0 device bundles,
# and the v23b captures for the take-lane notices): the project-context
# backup sync that runs on every call and times out when no node sidecar
# answers, colour quantization, a duplicated track losing its Producer Pal
# device. They never make a write fail or a read incomplete. This list grows
# ONLY from captures (a fixture or the device bundle's warn(...) text) that
# show the warning next to a complete read / a write that happened — never
# from a guess about what a warning "probably" means.
_ADVISORY_WARNING_RE = re.compile(
    r"^\s*(?:WARNING:\s*)?(?:"
    r"Project context backup sync failed\b"
    r"|Could not save the project context backup\b"
    r"|Failed to (?:read|write) project context backup\b"
    r"|Could not verify color quantization\b"
    r"|Requested .* color .* was mapped to nearest palette color\b"
    r"|Removed Producer Pal device from duplicated track\b"
    r"|removed the Producer Pal device from the new track\b"
    r"|could not check (?:the new track .* )?for the Producer Pal device\b"
    r"|Could not check for Producer Pal device in duplicated track\b"
    r"|param \"\w+\" is deprecated\b"
    # 2.4 (captured legacy_delete_ids, legacy_read_clip_clipId): an old
    # argument name accepted next to the read / write it named
    r"|\"\w+\" accepted as a fallback; the parameter is\b"
    # take lanes (captured v23b): a lane target notice, and a lane clip
    # promoted to the main lane "by re-creating it" (the copy has a new id)
    r"|targeting take lane\b"
    r"|clip .* was promoted to the main lane by re-creating it\b"
    r")", re.IGNORECASE)


def is_advisory_warning(text) -> bool:
    """A device warning that reports nothing about the call's own outcome."""
    return isinstance(text, str) and bool(_ADVISORY_WARNING_RE.match(text))


_INCOMPLETE_TOKENS = ("timeout", "timed out", "partial", "truncated", "incomplete")
# The captured advisory TAILS that themselves say "timed out" / "timeout" —
# the only text allowed after an advisory prefix when it carries one of the
# incomplete tokens (bundle warn(...) text: node_request '<name>' timed out
# after <n>ms; colour quantization: Error: timeout).
_ADVISORY_TAIL_WITH_TOKEN_RE = re.compile(
    r"^(?::\s*node_request '[^']+' timed out after \d+ms"
    r"|(?: for clip \S+)?:\s*Error:\s*timeout)\s*[.!]?\s*$", re.IGNORECASE)


def advisory_warning_hides_incompleteness(text) -> bool:
    """An allow-listed advisory PREFIX followed by text that says timeout /
    timed out / partial / truncated / incomplete and is not one of the
    captured tails — a composite like "Project context backup sync failed:
    session clip listing truncated" — counts as incomplete (round 3)."""
    match = _ADVISORY_WARNING_RE.match(text) if isinstance(text, str) else None
    if match is None:
        return False
    tail = text[match.end():]
    lowered = tail.lower()
    if not any(token in lowered for token in _INCOMPLETE_TOKENS):
        return False
    return _ADVISORY_TAIL_WITH_TOKEN_RE.match(tail) is None


def _bar_beat_key(position):
    if not isinstance(position, str) or not _BAR_BEAT_RE.fullmatch(position):
        return position
    bar, beat = position.split("|", 1)
    return int(bar), float(beat)


def clip_location(record) -> tuple | None:
    """Where a clip record (or a create/duplicate request's destination) sits,
    build-independent: ``("slot", track, scene)``, or ``("arrangement", track,
    lane, start)`` — 2.2 't10' + arrangementStart and 2.3 't10[5|1]' agree."""
    if not isinstance(record, dict):
        return None
    path = record.get("path") if record.get("path") is not None else record.get("toPath")
    if isinstance(path, str):
        slot = re.fullmatch(r"t(\d+)/s(\d+)", path)
        if slot:
            return ("slot", int(slot.group(1)), int(slot.group(2)))
    probe = {"path": path, "arrangementStart": record.get("arrangementStart"),
             "trackIndex": record.get("trackIndex")}
    track = clip_track_index(probe)
    if track is None:
        return None
    lane = re.match(r"t\d+/l(\d+)", path) if isinstance(path, str) else None
    return ("arrangement", track, int(lane.group(1)) if lane else None,
            _bar_beat_key(arrangement_start_of(probe)))


def _said(parsed) -> str:
    warnings = [str(item) for item in parsed.warnings or []]
    return ("; the device said: %s" % " | ".join(warnings)) if warnings else ""


# --- the three failure classes --------------------------------------------------
#
# 1. An MCP tool error (CallToolResult.isError, text "Error: …"): call_tool
#    raises PPToolError. 2.2 / 2.3 for a missing id or path; on 2.4 ALSO every
#    single-target refusal 2.3 sent as `[]` / `{deleted: false}` + WARNING
#    (2.4.0/v23b/v23b_refuse_create_midi_on_audio, v23/v23_duplicate_midi_to_audio,
#    v23b/v23b_delete_lane_clip, v24_update_track_send_missing_return, …).
# 2. A per-entry failure inside a batch reply: `{id, ok: false, detail}` with
#    no error (v24_read_clips_missing_batch) — ``failure_entries``.
# 3. A client-side schema refusal, before the device sees the call: the MCP
#    SDK's "MCP error -32602: Input validation error: …" (v24_read_track_sends,
#    `include: ["sends"]`) — ``is_schema_refusal``.

_SCHEMA_REFUSAL_RE = re.compile(
    r"^(?:Producer Pal tool error:\s*)?MCP error -32602: Input validation error\b")


def is_schema_refusal(message) -> bool:
    """The MCP client refused the call's arguments against the tool schema
    (``MCP error -32602: Input validation error: …``, v24_read_track_sends) —
    the device never saw it. Never a Live fact; a sidekick bug to surface."""
    return isinstance(message, str) and bool(_SCHEMA_REFUSAL_RE.match(message.strip()))


def failure_entries(response) -> list[dict]:
    """The ``{id, detail}`` of every per-entry failure in a batch reply —
    2.4's ``{id, ok: false, detail}`` entries (v24_read_clips_missing_batch).
    Empty for a reply without them (a single reply fails as an error)."""
    out = []
    for item in result_objects(parse_tool_response(response)):
        if item.get("ok") is False:
            out.append({"id": item.get("id"), "detail": item.get("detail")})
    return out


def _detail(item) -> str:
    detail = item.get("detail") if isinstance(item, dict) else None
    return detail.strip() if isinstance(detail, str) else ""


def delete_succeeded(item, object_id) -> bool:
    """Whether one delete receipt proves Producer Pal deleted ``object_id``:
    2.2 ``{deleted: true, id, type}`` (check_delete_clip_by_id), 2.3 the same
    plus ``deletedPath`` (v23_delete_by_id), 2.4 ``{deletedPath, id}`` with
    no ``deleted`` key at all (2.4.0/legacy/check_delete_clip_by_id,
    v24_delete_capture_tracks). Never for ``deleted: false`` (2.3 miss and
    lane refusal), for 2.4's ``{detail: "nothing to delete", id}``
    (v24_delete_clip_missing_id — it carries no ``deletedPath``), for a receipt naming another id, or for one
    that names no id. A receipt alone is still not absence: the readback
    decides (live_write._verify_clip_absent)."""
    if not isinstance(item, dict) or str(item.get("id")) != str(object_id):
        return False
    if "deleted" in item:
        return item["deleted"] is True
    return isinstance(item.get("deletedPath"), str) and bool(item["deletedPath"])


def live_api_missing(response) -> bool:
    """ppal-live-api's reply for an id Live does not have — 2.4 (captured
    v24_live_api_missing_id): ``{"id": "0", "path": "", "results":
    [{"operation": …}]}`` — no ``result`` on any receipt, no error, no text.
    No 2.2 / 2.3 capture of the same call exists (both raise through
    ``extract_live_api_receipts`` on a result-less receipt), so only this
    shape reads as missing; anything else is left to the receipt parser."""
    result = _result(response)
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        return False
    if result.get("id") != "0" or result.get("path") != "":
        return False
    receipts = result["results"]
    return bool(receipts) and all(isinstance(item, dict) and "result" not in item
                                  and isinstance(item.get("operation"), dict)
                                  for item in receipts)


def write_failed(response, expect: str = "object", *, count: int | None = None,
                 ids=None, location: dict | None = None,
                 note_count: int | None = None) -> str | None:
    """Why a Producer Pal write did NOT happen, or None when it did — judged by
    the reply's STRUCTURE, never by words in its warnings (both devices warn
    next to writes that happened: see ``is_advisory_warning``). The warnings
    ride along in the failure text.

    ``expect="object"``: every replied object has an id; ``count`` pins how
    many; ``ids`` the exact id set; ``location`` (the request's destination:
    ``{"path": …, "arrangementStart": …}`` or a ``toPath``) the place each
    object reports (``clip_location``); ``note_count`` the ``noteCount`` a
    create-clip reply reports — 2.4 minimises successful replies to ``{id,
    path}`` (v24_update_track_minimised, v24_move_clip) and nothing is
    demanded that a build omits, only what it reports is checked.
    ``expect="deleted"``: every replied object proves its delete
    (``delete_succeeded``: 2.2 / 2.3 ``deleted: true``, 2.4 ``deletedPath``;
    never ``nothing to delete``) and, with ``ids``, every requested id is
    among them. A split is judged with ``count=2`` (head and tail,
    v23_split_clip): an uncut clip replies ONE object next to "split cut
    nothing" (v24_split_clip_deprecated_key), and the count fails it with
    the warning in the text. A per-entry ``ok: false``
    (2.4 batches) and create-clip's ``detail: "overwrote the existing clip
    at …"`` (v24_create_session_clip_replaces — our writes never target an
    occupied slot, so this is a bug to surface) are refusals; every other
    captured ``detail`` is advisory. update-clip replies are not judged here:
    the readback that follows each update is the authority. Shared by every
    writer: on 2.3 a refusal is an empty (or ``deleted: false``) result plus
    a warning, not an error; on 2.4 a single-target refusal is an error
    (``PPToolError``, raised before this is reached)."""
    if expect not in ("object", "deleted"):
        raise ValueError("unknown write expectation %r" % (expect,))
    parsed = parse_tool_response(response)
    said = _said(parsed)
    if getattr(parsed, "warning_only", False):
        return "Producer Pal replied with warnings only and no result" + said
    if not parsed.is_json:
        # A legacy text reply (the isolated PP<2.0 fixture shim only; the
        # production adapter refuses non-JSON) carries no structured receipt.
        return None
    objects = result_objects(parsed)
    if not objects:
        return "Producer Pal replied with no object (nothing was written)" + said
    refused = [item for item in objects if item.get("ok") is False]
    if refused:
        return "Producer Pal refused %s%s" % (
            "; ".join("%s: %s" % (item.get("id") or "?", _detail(item) or "no detail")
                      for item in refused), said)
    if expect == "deleted":
        kept = [str(item.get("id") or item.get("deletedPath") or "?") for item in objects
                if not delete_succeeded(item, item.get("id"))]
        if kept:
            details = sorted({_detail(item) for item in objects if _detail(item)})
            return "Producer Pal did not delete %s%s%s" % (
                ", ".join(kept), (" (%s)" % "; ".join(details)) if details else "", said)
        if ids is not None:
            missing = sorted({str(i) for i in ids} - {str(item.get("id")) for item in objects})
            if missing:
                return "Producer Pal did not report deleting %s%s" % (", ".join(missing), said)
        return None
    if any(not isinstance(item.get("id"), str) or not item["id"] for item in objects):
        return "Producer Pal reply carries no object id" + said
    overwrote = [item for item in objects if _detail(item).lower().startswith("overwrote")]
    if overwrote:
        return "Producer Pal %s (%s) — the slot was occupied%s" % (
            _detail(overwrote[0]), ", ".join(item["id"] for item in overwrote), said)
    if count is not None and len(objects) != count:
        return "Producer Pal replied with %d objects, expected %d%s" % (
            len(objects), count, said)
    if ids is not None and {item["id"] for item in objects} != {str(i) for i in ids}:
        return "Producer Pal replied for %s, expected %s%s" % (
            ", ".join(sorted(item["id"] for item in objects)),
            ", ".join(sorted(str(i) for i in ids)), said)
    if location is not None:
        wanted = clip_location(location)
        for item in objects:
            got = clip_location(item)
            if got != wanted:
                return "Producer Pal placed %s at %s, not at %s%s" % (
                    item["id"], item.get("path"), location.get("path") or
                    location.get("toPath"), said)
    if note_count is not None:
        for item in objects:
            reported = item.get("noteCount")
            if isinstance(reported, int) and reported != note_count:
                return "Producer Pal reports noteCount %d for %s, sent %d%s" % (
                    reported, item["id"], note_count, said)
    return None


def is_missing_id_error(message, object_id) -> bool:
    """The device's 'id does not exist' error for exactly ``object_id``:
    2.2 'readClip failed: id "X" does not exist', 2.3 and 2.4 'Error: id "X"
    does not exist' (v24_read_clip_missing_id — the same text), or the bare
    per-entry ``detail`` of a 2.4 batch, 'id "X" does not exist'
    (v24_read_clips_missing_batch)."""
    text = str(message or "")
    quoted = re.escape(str(object_id))
    if re.search(r'(?:\breadClip failed: |\bError: )id "' + quoted + r'" does not exist\s*$',
                 text, re.IGNORECASE):
        return True
    return re.fullmatch(r'\s*id "' + quoted + r'" does not exist\s*', text,
                        re.IGNORECASE) is not None


# --- request builder: the ONE place argument forms depend on the build ---------

class UnsupportedAddressing(ValueError):
    """The operation has no captured argument form on this Producer Pal build."""


def _index(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("%s must be a non-negative integer, got %r" % (label, value))
    return value


def _position(value) -> str:
    if not isinstance(value, str) or not _BAR_BEAT_RE.fullmatch(value):
        raise ValueError("arrangement position must be bar|beat, got %r" % (value,))
    return value


class PPRequests:
    """Producer Pal request arguments for one addressing generation.

    2.2 (captured 2.2.0-rc1): numeric trackIndex / trackType / sceneIndex and
    arrangementStart next to a bare track path. 2.3 (captured 2.3.0): the
    object path carries it all — 't3', 'rt0', 'mt', 's2', 't3/s0', 't3[5|1]'
    — and the numeric arguments are gone. 2.4 (captured 2.4.0): the same
    paths; `count` is gone from create-track / create-scene, a batch is one
    path per object ('t+,t+', 's+,s+'). Session-slot paths and ids are the
    same on all three. Call sites never build locator dicts themselves.
    """

    def __init__(self, addressing: str = ADDRESSING_22):
        # "2.2" / "2.3" / "2.4", or the build version ppal-connect reports ("2.4.0")
        self.addressing = normalize_addressing(addressing)

    @property
    def v23(self) -> bool:
        """Path addressing (2.3 and 2.4): positions live in the object path."""
        return self.addressing in PATH_ADDRESSINGS

    @property
    def v24(self) -> bool:
        return self.addressing == ADDRESSING_24

    # -- locators --------------------------------------------------------------
    def clip(self, *, path: str | None = None, clip_id: str | None = None,
             track: int | None = None, scene: int | None = None) -> dict:
        """A read-clip / update-clip locator: a session slot path or an id."""
        return clip_locator(path=path, clip_id=clip_id, track=track, scene=scene)

    def track(self, index: int | None = None, *, kind: str = "regular",
              track_id: str | None = None) -> dict:
        """A read-track locator: an id, or a regular/return/main track."""
        if track_id is not None:
            return {"id": str(track_id)}
        if kind == "regular":
            index = _index(index, "track index")
            return {"path": "t%d" % index} if self.v23 else {"trackIndex": index}
        if kind == "return":
            index = _index(index, "return index")
            return ({"path": "rt%d" % index} if self.v23
                    else {"trackType": "return", "trackIndex": index})
        if kind == "main":
            return {"path": "mt"} if self.v23 else {"trackType": "master"}
        raise ValueError("track kind must be regular, return or main, got %r" % (kind,))

    def arrangement_path(self, track: int, start: str) -> str:
        """'t3[5|1]' — the 2.3 arrangement position path."""
        if not self.v23:
            raise UnsupportedAddressing("arrangement positions live in the path only on 2.3")
        return "t%d[%s]" % (_index(track, "track index"), _position(start))

    # -- tracks ----------------------------------------------------------------
    def read_track(self, index: int | None = None, *, include=None,
                   kind: str = "regular", track_id: str | None = None) -> dict:
        """A track read; ``include`` must be in this build's vocabulary
        (``require_known_include``: `sends` is not, v24_read_track_sends)."""
        args = self.track(index, kind=kind, track_id=track_id)
        if include is not None:
            args["include"] = require_known_include(self.addressing, "ppal-read-track", include)
        return args

    def create_track(self, *, count: int = 1, **fields) -> dict:
        """Append ``count`` tracks after the last one (2.2: trackIndex omitted;
        2.3: path 't+' + count; 2.4: one 't+' per track, 't+,t+' —
        v24_create_tracks_batch, `count` left the schema). A 2.4 batch
        replies a list, one ``{id, path}`` per track."""
        count = _index(count, "track count")
        if self.v24:
            return {"path": ",".join(["t+"] * max(count, 1)), **fields}
        if self.v23:
            return {"path": "t+", **({"count": count} if count != 1 else {}), **fields}
        return {"count": count, **fields}

    def update_track(self, track_id: str, **fields) -> dict:
        """Track update by id (the one locator both builds accept)."""
        return {"id": str(track_id), **fields}

    def duplicate_track(self, track_id: str, **fields) -> dict:
        return {"type": "track", "id": str(track_id), **fields}

    # -- devices -----------------------------------------------------------------
    def read_device(self, device_id: str, *, include=None) -> dict:
        """A device read by id (the one locator both builds accept). The
        reply shape (``parameters`` [{name, value}, …]) is the 2026-09-29
        live read of the two sidechain compressors; no capture of
        ppal-read-device exists in the fixtures."""
        args = {"id": str(device_id)}
        if include is not None:
            args["include"] = require_known_include(self.addressing, "ppal-read-device", include)
        return args

    # -- clips -----------------------------------------------------------------
    def create_clip(self, *, track: int, scene: int | None = None,
                    arrangement_start: str | None = None, **fields) -> dict:
        """A new clip in a session slot or at an arrangement position."""
        if (scene is None) == (arrangement_start is None):
            raise ValueError("create_clip needs exactly one of scene / arrangement_start")
        if scene is not None:
            return {"path": "t%d/s%d" % (_index(track, "track index"),
                                         _index(scene, "scene index")), **fields}
        if self.v23:
            return {"path": self.arrangement_path(track, arrangement_start), **fields}
        return {"path": "t%d" % _index(track, "track index"),
                "arrangementStart": _position(arrangement_start), **fields}

    def duplicate_clip_to_arrangement(self, clip_id: str, *, track: int, start: str,
                                      length: str | None = None, **fields) -> dict:
        """A copy of a clip placed on a track's arrangement (``length`` trims
        or tiles it; on 2.3 a trimmed copy no longer cuts its neighbour)."""
        args = {"type": "clip", "id": str(clip_id)}
        if self.v23:
            args["toPath"] = self.arrangement_path(track, start)
        else:
            args.update(toPath="t%d" % _index(track, "track index"),
                        arrangementStart=_position(start))
        if length is not None:
            args["arrangementLength"] = length
        return {**args, **fields}

    def duplicate_clip_to_new_lane(self, clip_id: str, *, track: int, start: str,
                                   lane_name: str | None = None, **fields) -> dict:
        """A copy on a fresh take lane — 2.2 only ('t<i>/l+' + takeLaneName)."""
        if self.v23:
            raise UnsupportedAddressing(
                "take-lane audition copies are not used on 2.3: lanes can be written "
                "('t<i>/l<k>[pos]'; 't<i>/l+' and takeLaneName are gone, a lane toPath "
                "needs a position), but a take-lane clip cannot be deleted through the "
                "API (v23b_delete_lane_clip), so an audition could not clean up")
        args = {"type": "clip", "id": str(clip_id),
                "toPath": "t%d/l+" % _index(track, "track index"),
                "arrangementStart": _position(start)}
        if lane_name is not None:
            args["takeLaneName"] = lane_name
        return {**args, **fields}

    def move_clip(self, clip_id: str, *, track: int, start: str,
                  from_track: int | None = None) -> dict:
        """Move an arrangement clip. 2.3: toPath 't3[49|1]' (the moved clip gets
        a NEW id). 2.2: arrangementStart, which moves it on its OWN track only,
        so 2.2 needs ``from_track`` and refuses a move to another track."""
        if self.v23:
            return {"id": str(clip_id), "toPath": self.arrangement_path(track, start)}
        _index(track, "track index")
        if from_track is None:
            raise ValueError("a 2.2 move needs from_track: 2.2 moves a clip on its own "
                             "track only")
        if _index(from_track, "from track") != track:
            raise UnsupportedAddressing(
                "2.2 cannot move an arrangement clip to another track (t%d -> t%d); "
                "arrangementStart moves it on its own track only" % (from_track, track))
        return {"id": str(clip_id), "arrangementStart": _position(start)}

    def split_clip(self, clip_id: str, at: str) -> dict:
        """Cut an arrangement clip (head keeps its id; on 2.3 the tail gets a
        new id and keeps its loop phase via firstStart)."""
        return {"id": str(clip_id), "arrangementSplit": at}

    def delete(self, object_type: str, *, ids=None, arrangement=None) -> dict:
        """Delete by id(s) (both builds), or an arrangement clip by its
        (track, start) position (2.3 only)."""
        if ids is not None:
            return {"type": object_type, **ids_arg(ids)}
        if arrangement is not None:
            if not self.v23:
                raise UnsupportedAddressing("2.2 cannot delete an arrangement clip by position")
            track, start = arrangement
            return {"type": object_type, "path": self.arrangement_path(track, start)}
        raise ValueError("delete needs ids or an arrangement position")

    def select(self, *, path: str | None = None, track: int | None = None,
               scene: int | None = None, view: str | None = None) -> dict:
        """ppal-select by path (both builds accept 't3/s0', 't3', 's2')."""
        if path is None:
            if track is not None and scene is not None:
                path = "t%d/s%d" % (_index(track, "track index"), _index(scene, "scene index"))
            elif track is not None:
                path = "t%d" % _index(track, "track index")
            elif scene is not None:
                path = "s%d" % _index(scene, "scene index")
        args = {} if path is None else {"path": path}
        if view is not None:
            args["view"] = view
        return args

    # -- scenes / transport ----------------------------------------------------
    def create_scenes(self, *, count: int, at: int) -> dict:
        """``count`` empty scenes inserted at index ``at`` (2.2 / 2.3 schema
        forms, not captured). 2.4 removed `count` and no insert-at-index was
        captured on it: refused rather than guessed."""
        if self.v24:
            raise UnsupportedAddressing(
                "inserting scenes at an index is not captured on Producer Pal 2.4 "
                "(`count` left create-scene's schema); append with append_scenes")
        where = ({"path": "s%d" % _index(at, "scene index")} if self.v23
                 else {"sceneIndex": _index(at, "scene index")})
        return {"count": count, "capture": False, **where}

    def append_scenes(self, *, count: int, scene_count: int, **fields) -> dict:
        """``count`` empty scenes after the last one: 2.2 sceneIndex = the
        current scene count, 2.3 count + path 's+' (v23b_create_scene_append),
        2.4 one 's+' per scene, 's+,s+' (v24_create_scenes_batch; a batch
        replies a list, one ``{id, path}`` per scene; a single 's+' one object,
        2.4.0/v23b/v23b_create_scene_append)."""
        count = _index(count, "scene count")
        if self.v24:
            return {"capture": False, "path": ",".join(["s+"] * max(count, 1)), **fields}
        if self.v23:
            return {"count": count, "capture": False, "path": "s+", **fields}
        return {"count": count, "capture": False,
                "sceneIndex": _index(scene_count, "scene count"), **fields}

    def read_scene(self, index: int, *, include=None) -> dict:
        args = ({"path": "s%d" % _index(index, "scene index")} if self.v23
                else {"sceneIndex": _index(index, "scene index")})
        if include is not None:
            args["include"] = require_known_include(self.addressing, "ppal-read-scene", include)
        return args

    def play_scene(self, index: int) -> dict:
        where = ({"path": "s%d" % _index(index, "scene index")} if self.v23
                 else {"sceneIndex": _index(index, "scene index")})
        return {"action": "play-scene", **where}

    def stop(self) -> dict:
        """Stop session and arrangement playback (same on both builds;
        captured v23b_stop: replies ``{playing: false}``)."""
        return {"action": "stop"}

    def update_arrangement(self, *, loop: bool | None = None, loop_start: str | None = None,
                           loop_end: str | None = None, start_time: str | None = None) -> dict:
        """Loop brace / start time in bar|beat (same keys on both builds)."""
        args = {"action": "update-arrangement"}
        for key, value in (("loop", loop), ("loopStart", loop_start),
                           ("loopEnd", loop_end), ("startTime", start_time)):
            if value is not None:
                args[key] = value
        return args


def requests_for(session) -> PPRequests:
    """The request builder for the build ``session`` is connected to."""
    return PPRequests(addressing_of(session))


def normalize_playback(obj: dict) -> dict:
    """Normalize playback and loop fields without fabricating missing playhead
    (2.3 update-arrangement replies carry no currentTime/arrangementLoop: read
    the brace and playhead through ppal-live-api)."""
    value = obj if isinstance(obj, dict) else {}
    result = _normalized_record()
    old_loop = value.get("arrangementLoop")
    old_loop = old_loop if isinstance(old_loop, dict) else {}
    enabled = value.get("loop")
    if enabled is None and "arrangementLoop" in value:
        enabled = bool(old_loop)
    result["loop"] = {
        "on": bool(enabled) if enabled is not None else None,
        "start": value.get("loopStart", old_loop.get("start")),
        "end": value.get("loopEnd", old_loop.get("end")),
    }
    result["playhead"] = value.get("currentTime")
    result["start_time"] = value.get("startTime")
    return result


def normalize_delete(obj: dict) -> dict:
    """Normalize PP delete receipts from 2.2 ({deleted, id, type}), 2.3
    (+ deletedPath; ``deleted: false`` for a missing object) and 2.4
    ({deletedPath, id}; ``{detail: "nothing to delete", id}`` for a missing
    object) — ``deleted`` is ``delete_succeeded`` for the receipt's own id."""
    value = obj if isinstance(obj, dict) else {}
    result = _normalized_record()
    path = value.get("deletedPath")
    index, _, _ = _path_indexes(path)
    if index is None and isinstance(path, str):
        index = clip_track_index({"path": path})
    result.update(
        track_index=value.get("trackIndex", index),
        track_path=path,
        arrangement_start=arrangement_start_of({"path": path}),
        deleted=delete_succeeded(value, value.get("id")) if value.get("id") is not None
        else bool(value.get("deleted", path is not None)),
    )
    return result


def parse_error(text: str) -> str | None:
    """Extract a PP 2.2/2.3 error message, preserving ordinary payloads."""
    if not isinstance(text, str):
        return None
    match = re.match(r"Error executing tool '[^']+':\s*(.*)", text.strip(), re.DOTALL)
    if match:
        return match.group(1).strip()
    match = re.match(r"Error:\s*(.*)", text.strip(), re.DOTALL)
    return match.group(1).strip() if match else None


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first_key(value, keys):
    wanted = set(keys)
    for node in _walk(value):
        if isinstance(node, dict):
            for key in wanted:
                if key in node:
                    return node[key]
    return None


def handshake_from_connect(response, explicit_capabilities=None,
                           requested: dict | None = None) -> dict:
    """Capture PP version and wire metadata from ppal-connect.

    Producer Pal connect does not report notation/format. Sidekick records what
    it requested on the transport separately from anything explicitly observed
    in the connect payload — never treating launch defaults as verified bridge
    settings (audit F9).
    """
    result = _result(response)
    req = dict(requested or {"notation": PP_NOTATION})
    out = {
        "pp_version": PP_VERSION_UNKNOWN,
        "requested": req,
        "observed": {"notation": None, "format": None},
        # Legacy flat keys: notation is transport-requested; format is contract only.
        "notation": req.get("notation", PP_NOTATION),
        "format": PP_FORMAT,
    }
    evidence = _capability_evidence(result)
    if explicit_capabilities is not None:
        if isinstance(explicit_capabilities, str):
            evidence.add(explicit_capabilities)
        elif isinstance(explicit_capabilities, dict):
            evidence.update(str(k) for k, enabled in explicit_capabilities.items() if enabled)
        else:
            evidence.update(str(value) for value in explicit_capabilities)
    if isinstance(result, dict):
        for key in ("pp_version", "ppVersion", "version", "producerPalVersion"):
            if result.get(key) is not None:
                out["pp_version"] = str(result[key])
                break
        for key in ("notation", "noteNotation"):
            if result.get(key) is not None:
                out["observed"]["notation"] = str(result[key])
                break
        for key in ("format", "responseFormat"):
            if result.get(key) is not None:
                out["observed"]["format"] = str(result[key])
                break
    for node in _walk(result):
        if not isinstance(node, dict):
            continue
        name = str(node.get("name", "")).strip().lower().replace("-", "_")
        value = node.get("value")
        if value is None:
            continue
        if name in {"version", "pp_version", "producer_pal_version"}:
            out["pp_version"] = str(value)
        elif name in {"notation", "note_notation"}:
            out["observed"]["notation"] = str(value)
        elif name in {"format", "response_format"}:
            out["observed"]["format"] = str(value)
    out["pp_version"], _, _ = _validated_pp_version(
        None if out["pp_version"] == PP_VERSION_UNKNOWN else out["pp_version"])
    out["capabilities"] = _capabilities(out, evidence)
    return out


def build_live_api_request(path: str | None, operations: list[dict]) -> dict:
    """Build and validate the exact ppal-live-api RC1 input envelope."""
    if path is not None and not isinstance(path, str):
        raise ValueError("path must be a string or None")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 50:
        raise ValueError("operations must contain 1..50 operations")
    clean = []
    allowed_keys = {"type", "property", "method", "args", "value"}
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise ValueError("operation %d must be an object" % index)
        unexpected = set(operation) - allowed_keys
        if unexpected:
            raise ValueError("unexpected operation keys: %s" % sorted(unexpected))
        op = operation.get("type")
        if op not in LIVE_API_OPERATIONS:
            raise ValueError("unsupported Live API operation: %r" % op)
        if op in _PROPERTY_OPS and (not isinstance(operation.get("property"), str) or not operation["property"]):
            raise ValueError("%s requires a nonempty string property" % op)
        if op in _METHOD_OPS and (not isinstance(operation.get("method"), str) or not operation["method"]):
            raise ValueError("%s requires a nonempty string method" % op)
        if "args" in operation:
            args = operation["args"]
            if not isinstance(args, list) or any(
                not isinstance(value, (str, int, float, bool))
                or (isinstance(value, float) and not math.isfinite(value)) for value in args
            ):
                raise ValueError("args must be a list of finite string/number/bool values")
        if op in _VALUE_OPS and "value" not in operation:
            raise ValueError("%s requires a value" % op)
        if "value" in operation:
            value = operation["value"]
            valid = isinstance(value, (str, int, float, bool)) and not (
                isinstance(value, float) and not math.isfinite(value))
            if isinstance(value, list):
                valid = all(isinstance(item, (int, float)) and not isinstance(item, bool)
                            and math.isfinite(item) for item in value)
            if not valid:
                raise ValueError("value has unsupported type")
        if op in {"goto", "setColor"} and not operation.get("value"):
            raise ValueError("%s requires a truthy value" % op)
        clean.append({key: operation[key] for key in ("type", "property", "method", "args", "value")
                      if key in operation})
    payload = {"operations": clean}
    if path is not None:
        payload["path"] = path
    return payload


def extract_live_api_receipts(response) -> list[dict]:
    """Extract ordered `{operation, result}` receipts; reject flattened shapes."""
    result = _result(response)
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        raise PPProtocolError("Live API response missing nested results list")
    receipts = []
    for item in result["results"]:
        if not isinstance(item, dict) or not isinstance(item.get("operation"), dict) or "result" not in item:
            raise PPProtocolError("invalid Live API operation receipt")
        try:
            operation = build_live_api_request(None, [item["operation"]])["operations"][0]
        except (TypeError, ValueError) as exc:
            raise PPProtocolError("invalid Live API operation receipt") from exc
        receipts.append({"operation": operation, "result": item["result"]})
    return receipts


def live_api_root_id(response) -> str:
    """Return the Live root object id from a ppal-live-api response envelope."""
    result = _result(response)
    if not isinstance(result, dict):
        raise PPProtocolError("Live API response must be an object")
    root_id = result.get("id")
    if not isinstance(root_id, str) or not root_id:
        raise PPProtocolError("Live API response missing root id")
    return root_id


def build_runtime_epoch(live_root_id: str, process_incarnation: dict) -> str:
    """Stable runtime epoch for one Live root + process incarnation."""
    pid = process_incarnation.get("pid")
    start = process_incarnation.get("start_time")
    if pid is None or start is None:
        raise ValueError("process incarnation requires pid and start_time")
    return "%s:%s:%s" % (live_root_id, pid, start)


_resolve_live_process_incarnation: Callable[[], dict] | None = None


def _default_resolve_live_process_incarnation() -> dict:
    """Best-effort local Ableton Live process pid + start time."""
    pid = None
    if shutil.which("pgrep"):
        proc = subprocess.run(
            ["pgrep", "-x", "Live"],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            candidates = proc.stdout.strip().splitlines()
            if len(candidates) != 1:
                raise RuntimeError("ambiguous local Live process identity")
            try:
                pid = int(candidates[0])
            except (TypeError, ValueError):
                pid = None
    if pid is None and shutil.which("ps"):
        proc = subprocess.run(
            ["ps", "-ax", "-o", "pid=,comm="],
            capture_output=True, text=True, check=False,
        )
        candidates = []
        for line in proc.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[1].endswith("Live"):
                try:
                    candidates.append(int(parts[0]))
                except ValueError:
                    continue
        if len(candidates) > 1:
            raise RuntimeError("ambiguous local Live process identity")
        if candidates:
            pid = candidates[0]
    if pid is None:
        raise RuntimeError("Ableton Live process not found locally")
    start_time = None
    if shutil.which("ps"):
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, check=False,
        )
        text = proc.stdout.strip()
        if proc.returncode == 0 and text:
            start_time = text
    if start_time is None:
        raise RuntimeError("Live process start time unavailable")
    return {"pid": pid, "start_time": start_time}


def resolve_live_process_incarnation() -> dict:
    """Return verified Live process incarnation; injectable for tests."""
    resolver = _resolve_live_process_incarnation or _default_resolve_live_process_incarnation
    return dict(resolver())


def set_live_process_incarnation_resolver(resolver: Callable[[], dict] | None) -> None:
    """Replace or restore the Live process incarnation resolver (tests)."""
    global _resolve_live_process_incarnation
    _resolve_live_process_incarnation = resolver


def response_warnings_mean_incomplete(response) -> list[str]:
    """Surface top-level warning tokens that imply partial inventory evidence.

    Fail-closed: a warning that says timeout / timed out / partial /
    truncated / incomplete marks the read incomplete unless it is on the
    advisory allow-list (``is_advisory_warning``), and that list grows only
    from captures — a warning a capture proved unrelated to the inventory
    (the project-context backup sync that "timed out" next to every complete
    read, colour quantization). An invented or not-yet-captured warning that
    merely contains "timed out" (say "background metadata sync timed out")
    stays incomplete on purpose: conservative false incompleteness, never a
    false complete read (review 2026-09-30 finding 5). The allow-list is a
    prefix match, so an advisory prefix followed by an incomplete token is
    exempt only when the tail is one of the captured tails that carry it
    (``advisory_warning_hides_incompleteness``); a composite "backup sync
    failed: session clip listing truncated" counts as incomplete."""
    parsed = parse_tool_response(response)
    issues = []
    if getattr(parsed, "warning_only", False):
        issues.append("Producer Pal replied with warnings only and no result: %s"
                      % " | ".join(str(item) for item in parsed.warnings))
        return issues
    for warning in parsed.warnings or []:
        if is_advisory_warning(warning) and not advisory_warning_hides_incompleteness(warning):
            continue
        text = str(warning).lower()
        if any(token in text for token in _INCOMPLETE_TOKENS):
            issues.append(str(warning))
    result = parsed.result
    if isinstance(result, dict):
        for key in ("partial", "timedOut", "timeout", "truncated", "incomplete"):
            if result.get(key):
                issues.append("%s flagged on response" % key)
    return issues

# Descriptive aliases for callers that use the protocol terminology.
build_ppal_live_api_request = build_live_api_request
parse_live_api_receipts = extract_live_api_receipts


def find_int(response, key, default=None):
    value = _first_key(_result(response), (key,))
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if LEGACY_TEXT_RESPONSE_MODE:
        return pp_codec.find_int(str(response), key, default)
    return default


def find_str(response, key: str, default=None):
    value = _first_key(_result(response), (key,))
    if isinstance(value, str):
        return value
    if LEGACY_TEXT_RESPONSE_MODE:
        return pp_codec.find_str(str(response), key, default)
    return default


def extract_selected_context(response) -> dict:
    result = _result(response)
    if isinstance(result, str) and LEGACY_TEXT_RESPONSE_MODE:
        return pp_codec.extract_selected_context(result)
    if not isinstance(result, dict):
        return {"view": None, "selected_track": None, "selected_scene": None,
                "selected_clip": None, "selected_device": None}

    def selected(name, fields):
        value = result.get(name)
        if not isinstance(value, dict):
            return None
        out = {target: value.get(source) for source, target in fields}
        if value.get("path") is not None:
            out["path"] = value["path"]
        return out

    track = selected("selectedTrack", (("id", "id"), ("type", "type")))
    if track is not None:
        # 2.2 trackIndex, 2.3 path 't3' (read/select_current.json, both builds)
        track["track_index"] = track_index_of(result["selectedTrack"])
    scene = selected("selectedScene", (("id", "id"),))
    if scene is not None:
        # 2.2 sceneIndex, 2.3 path 's0'
        scene["scene_index"] = scene_index_of(result["selectedScene"])
    return {
        "view": result.get("view"),
        "selected_track": track,
        "selected_scene": scene,
        "selected_clip": selected("selectedClip", (
            ("id", "id"), ("slot", "slot"), ("name", "name"))),
        "selected_device": selected("selectedDevice", (
            ("id", "id"), ("path", "path"))),
    }


def _notes_value(result):
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("notes", "midiNotes", "midi_json", "midiJson"):
            if key in result:
                return result[key]
        for key in ("clip", "data", "value"):
            if key in result:
                found = _notes_value(result[key])
                if found is not None:
                    return found
    return None


def extract_notes(response) -> list[dict] | None:
    result = _result(response)
    if isinstance(result, str) and LEGACY_TEXT_RESPONSE_MODE:
        text = pp_codec.extract_pp_notes(result)
        return pp_codec.pp_notes_to_notes(text) if text else None
    candidate = _notes_value(result)
    if candidate is None:
        return None
    if isinstance(candidate, str):
        try:
            return pp_codec.midi_json_to_notes(candidate)
        except ValueError:
            if LEGACY_TEXT_RESPONSE_MODE:
                return pp_codec.pp_notes_to_notes(candidate)
            raise
    return pp_codec.midi_json_to_notes(candidate)


def response_from_notes(notes: list[dict]) -> PPResponse:
    """Build an adapter response for one named arrangement clip's notes."""
    payload = pp_codec.encode_notes(notes, pp_codec.CodecMode.MIDI_JSON)
    result = {"notes": payload, "noteCount": len(notes)}
    return PPResponse(json.dumps({"result": result, "warnings": []}), result, [])


def extract_pp_notes(response):
    """Legacy-only compatibility helper; JSON callers use ``extract_notes``."""
    if LEGACY_TEXT_RESPONSE_MODE:
        return pp_codec.extract_pp_notes(str(response))
    return ""


def _collection(result, keys):
    wanted = set(keys)
    for node in _walk(result):
        if isinstance(node, dict):
            for key in wanted:
                if isinstance(node.get(key), list):
                    return node[key]
    return []


def extract_arrangement_clip_notes(response, clip_index: int = 0,
                                   clip_name: str | None = None):
    result = _result(response)
    clips = _collection(result, ("arrangementClips", "arrangement-clips", "arrangement_clips"))
    if not clips:
        if LEGACY_TEXT_RESPONSE_MODE:
            text = pp_codec.extract_arrangement_clip_notes(
                str(response), clip_index=clip_index, clip_name=clip_name)
            return pp_codec.pp_notes_to_notes(text) if text else None
        return extract_notes(response)
    matches = [clip for clip in clips if isinstance(clip, dict)
               and (clip_name is None or clip.get("name") == clip_name)]
    if clip_name is not None and len(matches) != 1:
        return None
    if not matches:
        if LEGACY_TEXT_RESPONSE_MODE:
            text = pp_codec.extract_arrangement_clip_notes(
                str(response), clip_index=clip_index, clip_name=clip_name)
            return pp_codec.pp_notes_to_notes(text) if text else None
        return None
    selected = matches[min(max(0, clip_index), len(matches) - 1)]
    return extract_notes(PPResponse(json.dumps({"result": selected}), selected, []))


def _clip_keys_present(result, keys) -> bool:
    wanted = set(keys)
    for node in _walk(result):
        if isinstance(node, dict):
            for key in wanted:
                if key in node:
                    return True
    return False


def session_clips_inventory(response) -> dict:
    """Return session clips with an explicit completeness flag.

    A missing ``sessionClips`` collection is incomplete evidence — not proof of
    an empty JAM track (audit F2).
    """
    result = _result(response)
    if isinstance(result, str) and LEGACY_TEXT_RESPONSE_MODE:
        clips = pp_codec.parse_session_clips(result)
        return {"clips": clips, "complete": True, "present": bool(result.strip())}
    clips_raw = _collection(result, SESSION_CLIP_KEYS)
    present = _clip_keys_present(result, SESSION_CLIP_KEYS)
    if not present:
        return {"clips": [], "complete": False, "present": False}
    complete = not any(
        key in node and not isinstance(node[key], list)
        for node in _walk(result) if isinstance(node, dict)
        for key in SESSION_CLIP_KEYS
    )
    out = []
    for clip in clips_raw:
        if not isinstance(clip, dict):
            complete = False
            continue
        slot = pp_codec.clip_session_slot(clip)
        name = clip.get("name")
        has_identity = clip.get("id") is not None and clip.get("path") is not None
        if slot is None or (name is None and not has_identity):
            complete = False
            continue
        out.append({"slot": str(slot), "name": str(name or ""), "clip": dict(clip)})
    return {"clips": out, "complete": complete, "present": True}


def parse_session_clips(response, *, strict: bool = False) -> list[dict]:
    inventory = session_clips_inventory(response)
    if strict and not inventory["complete"]:
        raise LiveWriteVerificationError(
            "session clip inventory", 0, None,
            detail="session clip listing is incomplete — refusing to treat missing "
                   "collection as an empty track",
        )
    return [{"slot": item["slot"], "name": item["name"]} for item in inventory["clips"]]


def track_index_by_name(response, name: str) -> tuple[int | None, str | None]:
    tracks = _collection(_result(response), ("tracks",))
    for track in tracks:
        if isinstance(track, dict) and track.get("name") == name:
            index = track.get("trackIndex")
            if index is None:
                parsed = _live_path(track.get("path"))
                if parsed:
                    index = parsed[0]
            return index, track.get("id")
    if LEGACY_TEXT_RESPONSE_MODE:
        return pp_codec.track_index_by_name(str(response), name)
    return None, None


def regular_track_count(response) -> int:
    n = find_int(response, "regularTrackCount")
    if n is not None:
        return n
    tracks = _collection(_result(response), ("tracks",))
    if tracks:
        return len(tracks)
    if LEGACY_TEXT_RESPONSE_MODE:
        return pp_codec.regular_track_count(str(response))
    return 0


def track_names_by_index(response) -> dict:
    result = _result(response)
    tracks = _collection(result, ("tracks",))
    if not tracks and LEGACY_TEXT_RESPONSE_MODE:
        return pp_codec.track_names_by_index(str(response))
    out = {}
    for track in tracks:
        if not isinstance(track, dict):
            continue
        index = track.get("trackIndex")
        if index is None:
            parsed = _live_path(track.get("path"))
            index = parsed[0] if parsed else None
        if index is not None:
            out[int(index)] = track.get("name")
    return out


_SCALE_PC = {
    "C": 0, "B#": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3, "E": 4, "FB": 4,
    "F": 5, "E#": 5, "F#": 6, "GB": 6, "G": 7, "G#": 8, "AB": 8, "A": 9,
    "A#": 10, "BB": 10, "B": 11, "CB": 11,
}
_SHARP_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def scale_from_overview(response) -> tuple[str | None, list[int] | None]:
    """Live's own scale setting from the live-set overview: ("<Tonic> <mode>",
    pitch classes) with the tonic normalised to sharps and the mode lowercased,
    e.g. ("C bhairav", [0,1,4,5,7,8,11]). Fail-closed: anything missing or
    unparseable (a tonic outside the twelve, a pitch token like "H", an explicit
    scaleEnabled=false) yields (None, None) — the caller then keeps its own
    estimate. Pure wire parse; no music theory here."""
    scale = find_str(response, "scale")
    if not isinstance(scale, str) or not scale.strip():
        return None, None
    parts = scale.strip().split(None, 1)
    if len(parts) != 2:
        return None, None
    tonic_idx = _SCALE_PC.get(parts[0].strip().upper())
    if tonic_idx is None:
        return None, None
    enabled = _first_key(_result(response), ("scaleEnabled", "scaleMode"))
    if enabled is False:
        return None, None
    key = "%s %s" % (_SHARP_NAMES[tonic_idx], parts[1].strip().lower())
    pitches = find_str(response, "scalePitches")
    pcs: list[int] | None = None
    if isinstance(pitches, str) and pitches.strip():
        pcs = []
        for tok in pitches.split(","):
            idx = _SCALE_PC.get(tok.strip().upper())
            if idx is None:
                return None, None
            pcs.append(idx)
        pcs = sorted(set(pcs))
    return key, pcs


def max_scene_index_in_track(response) -> int | None:
    clips = _collection(_result(response), ("sessionClips", "session-clips", "session_clips"))
    values = []
    for clip in clips:
        if isinstance(clip, dict):
            if clip.get("sceneIndex") is not None:
                values.append(int(clip["sceneIndex"]))
            elif isinstance(clip.get("slot"), str) and "/" in clip["slot"]:
                values.append(int(clip["slot"].split("/", 1)[1]))
            elif isinstance(clip.get("path"), str):
                parsed = _live_path(clip["path"])
                if parsed and parsed[1] == "s":
                    values.append(parsed[2])
    if values:
        return max(values)
    if LEGACY_TEXT_RESPONSE_MODE:
        return pp_codec.max_scene_index_in_track_text(str(response))
    return None


def clip_type_token(response) -> str | None:
    value = _first_key(_result(response), ("type",))
    if value is None:
        if LEGACY_TEXT_RESPONSE_MODE:
            return pp_codec.clip_type_token(str(response))
        return None
    return str(value).lower()


def clip_id_token(response) -> str | None:
    value = _first_key(_result(response), ("id",))
    if value is None and LEGACY_TEXT_RESPONSE_MODE:
        return pp_codec.clip_id_token(str(response))
    return "null" if value is None else str(value)


def is_empty_slot_error(exc_or_text, path: str | None = None) -> bool:
    return pp_codec.is_empty_slot_error(str(exc_or_text or ""), path=path)


_FREE_SLOT_WARNING_RE = re.compile(r"^\s*WARNING:\s*no clip at (\S+?)\s*$")


def slot_read_verdict(path: str, *, reply=None, error=None) -> dict:
    """What a ``ppal-read-clip`` of session slot ``path`` (``include: []``)
    proves before a write: ``{"state": "free" | "occupied" | "unknown",
    "clip_id", "detail"}``. Free is ONLY a captured empty-slot shape for
    that very slot:
      - 2.2 and 2.3 (the same shape): ``{id: null, name: null, path: <path>,
        type: null}``, with or without its "WARNING: no clip at <path>"
        (2.2.0-rc1/legacy/check_read_clip_existing_empty_slot.json,
        2.3.0/empty_slot/read_empty_slot.json);
      - 2.4: the error "Error: no clip at <path>"
        (2.4.0/v24/v24_read_clip_empty_slot, 2.4.0/legacy/
        check_read_clip_existing_empty_slot), or a missing scene's
        "no scene at "sN"" for that scene (legacy_read_clip_empty_slot).
    A reply with a clip id at that path is ``occupied``. Everything else —
    a warning-only reply, ``[]``, ``{}``, an id-less clip, another path, a
    non-advisory warning, any other error — is ``unknown``: never free."""
    if error is not None:
        if is_empty_slot_error(error, path=path):
            return {"state": "free", "clip_id": None, "detail": "empty-slot error"}
        return {"state": "unknown", "clip_id": None,
                "detail": "the read failed: %s" % (error or "no text")}
    parsed = parse_tool_response(reply)
    if getattr(parsed, "warning_only", False):
        return {"state": "unknown", "clip_id": None, "detail": "warnings only, no result"}
    result = parsed.result
    if not isinstance(result, dict) or not result:
        return {"state": "unknown", "clip_id": None,
                "detail": "no clip record (%r)" % (result,)}
    clip_id = result.get("id")
    if isinstance(clip_id, str) and clip_id:
        # a clip answered: never free (a reply naming another place is still
        # a clip — callers check the location themselves)
        elsewhere = result.get("path") not in (None, path)
        return {"state": "occupied", "clip_id": clip_id,
                "detail": "clip %s%s%s" % (clip_id, " %r" % result["name"]
                                           if result.get("name") else "",
                                           " (reads at %s)" % result.get("path")
                                           if elsewhere else "")}
    others = [str(w) for w in parsed.warnings or []
              if not is_advisory_warning(w)
              and not (_FREE_SLOT_WARNING_RE.match(str(w))
                       and _FREE_SLOT_WARNING_RE.match(str(w)).group(1) == path)]
    if ("id" in result and result.get("path") == path
            and all(value is None for key, value in result.items() if key != "path")
            and not others):
        return {"state": "free", "clip_id": None, "detail": "empty-slot record"}
    return {"state": "unknown", "clip_id": None,
            "detail": "unrecognised slot read %s%s" % (
                json.dumps(result, sort_keys=True)[:160],
                "; the device said: %s" % " | ".join(others) if others else "")}


def clip_record(response) -> dict:
    """The clip dict a structured read-clip response carries, or {} for legacy
    text (no timing → no claims about the loop brace)."""
    result = _result(response)
    return result if isinstance(result, dict) else {}


def arrangement_clip_record(response, clip_index: int = 0,
                            clip_name: str | None = None) -> dict | None:
    """The one arrangement clip an arrangement-clips listing selects (by name,
    else by index), as its dict — the record extract_arrangement_clip_notes
    reads notes from."""
    result = _result(response)
    clips = _collection(result, ("arrangementClips", "arrangement-clips", "arrangement_clips"))
    matches = [clip for clip in clips if isinstance(clip, dict)
               and (clip_name is None or clip.get("name") == clip_name)]
    if not matches or (clip_name is not None and len(matches) != 1):
        return None
    return matches[min(max(0, clip_index), len(matches) - 1)]


_HIDDEN_EPS = 1e-3


def clip_timing_beats(clip: dict) -> dict:
    """Loop brace of a clip record in quarter-note beats: start/end from the
    bar|beat fields in the clip's meter (the beat is the meter's beat: an eighth
    in 6/8), end falling back to start + length. Missing or unreadable fields
    stay None — no timing, no claim."""
    clip = clip if isinstance(clip, dict) else {}
    meter = clip_playback.parse_signature(clip.get("timeSignature")) or (4, 4)
    num, den = meter
    bpb = num * 4.0 / den
    start = end = None
    s = clip.get("start")
    if isinstance(s, str) and "|" in s:
        try:
            start = clip_playback.bar_beat_quarters(s, num, den)
        except (ValueError, TypeError):
            start = None
    e = clip.get("end")
    if isinstance(e, str) and "|" in e:
        try:
            end = clip_playback.bar_beat_quarters(e, num, den)
        except (ValueError, TypeError):
            end = None
    if end is None and start is not None and isinstance(clip.get("length"), str):
        try:
            end = start + pp_codec.pp_duration_to_beats(clip["length"], bpb)
        except ValueError:
            end = None
    return {"beats_per_bar": bpb, "loop_start_beats": start, "loop_end_beats": end,
            "looping": clip.get("looping")}


def _note_span(note: dict, den: int = 4) -> tuple[float, float]:
    """Clip-content (start, end) of a note in quarter beats (its own units are
    the clip meter's beats)."""
    start = float(note.get("start", note.get("start_beats", 0.0)) or 0.0) * 4.0 / den
    dur = float(note.get("duration", note.get("duration_beats", 0.0)) or 0.0) * 4.0 / den
    return start, start + dur


def split_audible_notes(clip: dict, notes: list[dict], *, live: dict | None = None) -> dict:
    """Which of a clip's notes actually play (see mcp_server.clip_playback).

    ``notes`` are Producer Pal's (clip-meter beats), read only inside its
    [-L, 2L) window. A clip whose played region is not wholly inside that
    window comes back ``unread`` with empty lists: its notes are neither
    audible nor hidden, and ``unread_reason`` says why. ``live`` — the Live API
    read of the same clip ({"frame", "notes"} from live_write.read_clip_live) —
    replaces them with the complete notes and the real markers. A note whose
    start sits outside the played region is HIDDEN (seen, not playing); a muted
    note never plays. Notes come back untouched, partitioned; without loop
    timing every note is audible (``coverage`` None). ``notes_den``: the
    beat unit of the returned notes (4 = quarters for a Live API read, the
    clip meter's denominator for Producer Pal's); ``played_region`` is in
    quarter beats of clip-content time."""
    timing = clip_timing_beats(clip or {})
    bpb = timing["beats_per_bar"]
    if live is not None and live.get("frame") is not None and not live.get("error"):
        frame = live["frame"]
        result = clip_playback.split(frame, live.get("notes") or [], quarters=True)
        den = 4
        timing = {**timing, "loop_start_beats": frame["loop_start"],
                  "loop_end_beats": frame["loop_end"], "looping": frame["looping"]}
    else:
        frame = clip_playback.frame_from_pp(clip or {})
        notes = list(notes or [])
        if frame is None:
            meter = clip_playback.parse_signature((clip or {}).get("timeSignature"))
            return {"audible": notes, "hidden": [], "n_hidden": 0, "hidden_span_bars": None,
                    "muted": [], "coverage": None, "unread": False, "unread_reason": None,
                    "notes_source": "producer_pal", "notes_den": meter[1] if meter else 4,
                    **timing}
        result = clip_playback.split(frame, notes)
        den = frame["den"]
    hidden = result["hidden"]
    span = None
    if hidden:
        spans = [_note_span(n, den) for n in hidden]
        first = min(s for s, _ in spans)
        last = max(e for _, e in spans)
        span = [int(first // bpb) + 1, max(int(first // bpb) + 1, int(math.ceil(last / bpb)))]
    return {"audible": result["audible"], "hidden": hidden, "n_hidden": len(hidden),
            "hidden_span_bars": span, "muted": result["muted"],
            "coverage": result["coverage"], "unread": result["unread"],
            "unread_reason": result["unread_reason"], "notes_source": result["notes_source"],
            "played_region": result["played_region"],
            "start_marker_known": result["start_marker_known"], "notes_den": den, **timing}


def _pp_frame(clip) -> dict | None:
    """clip_playback.frame_from_pp of a clip record, None when unreadable."""
    try:
        return clip_playback.frame_from_pp(clip) if isinstance(clip, dict) else None
    except (TypeError, ValueError, KeyError):
        return None


def classify_session_clip(response, notes) -> str:
    """heard | unread | empty | unheard_audio | unheard_unknown (| invalid for
    legacy text). ``unread``: a MIDI clip that came back without notes while
    its played region lies (partly) outside Producer Pal's [-L, 2L) note
    window — its notes are unknown (the Live API reads them)."""
    if notes:
        return "heard"
    # PPResponse subclasses str, so isinstance(response, str) is True for
    # structured JSON responses as well. Classify structured payloads FIRST
    # (review finding 1, 2026-08-16): the legacy text branch is only for
    # genuine non-JSON payloads (error strings, legacy fixtures).
    parsed = response if isinstance(response, PPResponse) else parse_tool_response(response)
    if parsed.is_json and isinstance(parsed.result, dict):
        ctype = clip_type_token(response)
        if ctype == "null" or clip_id_token(response) == "null":
            return "empty"
        if ctype == "audio":
            return "unheard_audio"
        if ctype == "midi":
            # Coverage before presence: Producer Pal reads notes only inside
            # [-L, 2L), so no notes from a clip that plays outside that window
            # means "not read", never "empty".
            frame = _pp_frame(parsed.result)
            if frame is not None and clip_playback.coverage(frame) != clip_playback.FULL:
                return "unread"
            return "empty"
        return "unheard_unknown" if clip_id_token(response) else "empty"
    # String payloads (legacy text or error messages) keep the full legacy
    # classification contract, including the fail-closed "invalid" tail:
    # unanchored error text must never be treated as an empty slot.
    return pp_codec.classify_session_clip(str(response), notes)


def validate_handshake(handshake: dict) -> dict:
    """Enforce the wire contract BEFORE any Live mutation (review finding 4).

    ppal-connect does not report notation/format. Sidekick sends
    ``x-producer-pal-notation: midi-json`` on the MCP transport and records
    requested vs observed separately. Only explicitly observed mismatches fail
    closed; unverified bridge settings are not treated as verified (audit F9).
    """
    requested = handshake.get("requested") or {}
    if requested.get("notation") != PP_NOTATION:
        raise PPProtocolError(
            "Sidekick requested notation %r != required %r"
            % (requested.get("notation"), PP_NOTATION)
        )
    observed = handshake.get("observed") or {}
    if observed.get("format") not in (None, PP_FORMAT):
        raise PPProtocolError(
            "Producer Pal bridge format %r != required %r — refusing to mutate Live."
            % (observed.get("format"), PP_FORMAT)
        )
    if observed.get("notation") not in (None, PP_NOTATION):
        raise PPProtocolError(
            "Producer Pal bridge notation %r != required %r — refusing to mutate Live."
            % (observed.get("notation"), PP_NOTATION)
        )
    version = handshake.get("pp_version", PP_VERSION_UNKNOWN)
    if version == PP_VERSION_UNKNOWN:
        raise PPProtocolError(
            "Producer Pal version unknown — refusing to mutate Live without a verified 2.x bridge"
        )
    canonical, _, _ = _validated_pp_version(version)
    if canonical not in SUPPORTED_PP_VERSIONS:
        raise PPProtocolError(
            "Producer Pal %s is not verified by captured fixtures yet (supported: %s). "
            "To add it: run `tools/capture_pp_fixtures.py read` on a real Set and "
            "`tools/capture_pp_fixtures.py mutate` in a Set saved as SCRATCH*.als, then "
            "see docs/PRODUCER_PAL_VERSIONS.md."
            % (canonical, ", ".join(sorted(SUPPORTED_PP_VERSIONS)))
        )
    return handshake


def pp_transport_headers() -> dict[str, str]:
    """Per-request MCP transport headers Sidekick sends to Producer Pal."""
    return {PP_NOTATION_HEADER: PP_NOTATION}


def extract_delete_receipt(response) -> dict:
    """Surface PP delete success ids and partial warning evidence: one receipt
    (2.2, 2.3, 2.4) or a list of them (multi-id delete); a 2.3 miss is
    ``deleted: false`` with its WARNING part in ``warnings``, a 2.4 miss
    ``{detail: "nothing to delete", id}`` (``delete_succeeded`` reads all)."""
    parsed = parse_tool_response(response)
    result = parsed.result
    ids: list[str] = []
    warn: list[str] = []
    for item in _flatten_objects(result):
        deleted = item.get("deleted")
        if isinstance(deleted, list):
            ids.extend(str(entry) for entry in deleted if entry is not None)
        elif isinstance(deleted, str) and deleted:
            ids.append(deleted)
        elif item.get("id") is not None and delete_succeeded(item, item["id"]):
            ids.append(str(item["id"]))
        if isinstance(item.get("warnings"), list):
            warn.extend(str(entry) for entry in item["warnings"])
    warn.extend(str(entry) for entry in parsed.warnings or [])
    return {"deleted_ids": ids, "warnings": warn, "result": result}


def build_set_identity(file_path: str | None, runtime_epoch: str) -> dict:
    """Shared set_identity envelope for write receipts and inventory."""
    normalized = (file_path or "").strip() or None
    if normalized:
        set_id = normalized
    else:
        set_id = "unsaved:%s" % runtime_epoch
    return {
        "set_id": set_id,
        "runtime_epoch": runtime_epoch,
        "file_path": normalized,
    }


def clip_ref_from_record(clip: dict, *, track_id: str | None, track_name: str,
                         surface: str, notes: list[dict] | None = None) -> dict | None:
    """Build a conservative clip_ref from one structured clip read."""
    if not isinstance(clip, dict):
        return None
    clip_id = clip.get("id")
    if not isinstance(clip_id, str) or not clip_id:
        return None
    path = clip.get("path")
    if not isinstance(path, str) or not path:
        slot = pp_codec.clip_session_slot(clip)
        if surface == "session" and slot:
            track_index, scene = slot.split("/", 1)
            path = "t%s/s%s" % (track_index, scene)
        else:
            return None
    if surface == "session" and pp_codec.is_lane_object_path(path):
        return None
    ref = {
        "clip_id": clip_id,
        "track_id": str(track_id or ""),
        "track": str(track_name or ""),
        "surface": surface,
        "path": path,
    }
    if isinstance(clip.get("name"), str) and clip["name"]:
        ref["clip_name"] = clip["name"]
    timing = clip_timing_beats(clip)
    if timing.get("loop_start_beats") is not None:
        ref["loop_start_beats"] = timing["loop_start_beats"]
    if timing.get("loop_end_beats") is not None:
        ref["loop_end_beats"] = timing["loop_end_beats"]
    if clip.get("start"):
        ref["start"] = clip["start"]
    if clip.get("end"):
        ref["end"] = clip["end"]
    if notes:
        ref["answer_notes"] = list(notes)
    return ref


@asynccontextmanager
async def operation_session(url: str | None = None) -> AsyncIterator[PPSession]:
    """Open one operation-scoped PP session: connect → initialize → ppal-connect.

    Multiple tool calls may share this session for one logical operation.
    Teardown closes the transport. Not a process-long client. The handshake is
    validated before the session is yielded, so no Live mutation can happen on
    a bridge speaking the wrong wire contract. Set identity is resolved from
    verified Live root id + process incarnation — never a per-request token.
    """
    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession

    endpoint = url or PP_URL
    transport_headers = pp_transport_headers()
    async with streamablehttp_client(endpoint, headers=transport_headers) as (rd, wr, *_):
        async with ClientSession(rd, wr) as session:
            await session.initialize()
            available = set()
            try:
                listed = await session.list_tools()
                tools = getattr(listed, "tools", None)
                if tools is None and isinstance(listed, dict):
                    tools = listed.get("tools")
                for tool in tools or []:
                    name = getattr(tool, "name", None)
                    if name is None and isinstance(tool, dict):
                        name = tool.get("name")
                    if name:
                        available.add(str(name))
            except Exception:
                available = set()
            connected = await call_tool(session, "ppal-connect", {})
            handshake = validate_handshake(handshake_from_connect(
                connected, available, requested={"notation": PP_NOTATION}))
            yield PPSession(session, handshake=handshake)


def raise_partial_write(context: str, sent: int, received: int | None,
                        completed: list[dict] | None = None, detail: str = ""):
    raise LiveWriteVerificationError(context, sent, received, completed, detail)


def require_verified_note_count(sent: int, received: int | None, context: str,
                                completed: list[dict] | None = None) -> int:
    if received != sent:
        raise_partial_write(context, sent, received, completed)
    return received


def require_scene_count(overview) -> int:
    """Scene count from read-live-set — fail closed (never default to 0)."""
    n = find_int(overview, "sceneCount")
    if n is None:
        raise LiveWriteVerificationError(
            "sceneCount", 0, None,
            detail="sceneCount unreadable from Producer Pal read-live-set — "
                   "refusing to write (row indexes would be unsafe)",
        )
    return n


def verify_count(response) -> dict:
    """Return an explicit verification status for a PP read-back note count."""
    notes = extract_notes(response)
    if notes is None:
        return {"status": "unverifiable", "count": None}
    advertised = find_int(response, "noteCount")
    if advertised is not None and advertised != len(notes):
        return {"status": "mismatched", "count": len(notes),
                "detail": f"advertised noteCount was {advertised}"}
    return {"status": "verified", "count": len(notes)}


def verify_notes(sent_notes: list[dict], readback_response, context: str) -> dict:
    """Compare canonical sent notes with JSON read-back without silent fallbacks."""
    normalized = pp_codec.normalize_notes_for_pp(pp_codec.note_dicts(sent_notes))
    sent = pp_codec.midi_json_to_notes(pp_codec.notes_to_midi_json_payload(normalized))
    try:
        received = extract_notes(readback_response)
    except (PPProtocolError, ValueError) as exc:
        return {"status": "unverifiable", "sent": len(sent), "received": None,
                "detail": str(exc)}
    if received is None:
        return {"status": "unverifiable", "sent": len(sent), "received": None,
                "detail": "read-back omitted notes"}
    advertised = find_int(readback_response, "noteCount")
    if advertised is not None and advertised != len(received):
        return {"status": "mismatched", "sent": len(sent), "received": len(received),
                "detail": f"advertised noteCount was {advertised}"}
    if len(received) != len(sent):
        return {"status": "mismatched", "sent": len(sent), "received": len(received),
                "detail": "note count differs"}

    def key(note):
        return (float(note["start_beats"]), int(note["pitch"]))

    for index, (expected, actual) in enumerate(zip(sorted(sent, key=key),
                                                    sorted(received, key=key)), 1):
        differences = []
        if expected["pitch"] != actual["pitch"]:
            differences.append("pitch %s != %s" % (expected["pitch"], actual["pitch"]))
        if expected["velocity"] != actual["velocity"]:
            differences.append("velocity %s != %s" % (expected["velocity"], actual["velocity"]))
        for field in ("start_beats", "duration_beats"):
            if abs(float(expected[field]) - float(actual[field])) > pp_codec.READBACK_TIME_TOLERANCE_BEATS:
                differences.append("%s %.6f != %.6f" % (field, expected[field], actual[field]))
        if differences:
            return {"status": "mismatched", "sent": len(sent), "received": len(received),
                    "detail": "note %d: %s" % (index, ", ".join(differences))}
    return {"status": "verified", "sent": len(sent), "received": len(received)}


def require_verified_notes(sent_notes: list[dict], readback_text, context: str,
                           completed: list[dict] | None = None) -> int:
    """Fail unless Producer Pal read-back preserves the complete canonical note list."""
    result = verify_notes(sent_notes, readback_text, context)
    if result["status"] != "verified":
        detail = "status=%s" % result["status"]
        if result.get("detail"):
            detail += "; " + result["detail"]
        raise_partial_write(context, result.get("sent", len(sent_notes)),
                            result.get("received"), completed, detail)
    return result["received"]


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------

_SELFTEST_FIXTURES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures", "pp")
_SELFTEST_SCHEMAS: dict[str, dict] = {}


def _selftest_schema_check(name: str, args: dict | None, version: str = "2.2.0-rc1") -> None:
    """Selftest fakes only: reject a tool or argument the captured schemas of
    ``version`` do not list (same rule as tests/pp_fakes.py)."""
    if version not in _SELFTEST_SCHEMAS:
        with open(os.path.join(_SELFTEST_FIXTURES, version, "tool_schemas.json"),
                  encoding="utf-8") as handle:
            _SELFTEST_SCHEMAS[version] = json.load(handle)
    tool = _SELFTEST_SCHEMAS[version].get(name)
    if tool is None:
        raise RuntimeError("Unknown tool '%s'" % name)
    unknown = set(args or {}) - set(tool.get("inputSchema", {}).get("properties", {}))
    if unknown:
        raise AssertionError("unknown arguments for %s: %s" % (name, sorted(unknown)))


def _selftest_package_import() -> None:
    """Fresh-subprocess package import must resolve (not only script-dir selftest)."""
    import subprocess

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    probe = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "import mcp_server.pp_session as ps\n"
        "import pp_codec\n"
        "import mcp_server.pp_codec as pkg_codec\n"
        "assert pp_codec is pkg_codec, 'pp_codec dual-universe'\n"
        "assert ps.pp_codec is pp_codec, 'session must share codec module'\n"
        "print('ok')\n"
    ) % repo
    r = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        capture_output=True,
        text=True,
        cwd=repo,
    )
    assert r.returncode == 0 and "ok" in r.stdout, (
        "package import failed: rc=%s stdout=%r stderr=%r"
        % (r.returncode, r.stdout, r.stderr)
    )


def run_selftest() -> None:
    import asyncio
    import types
    print("--- pp_session selftest ---")
    _selftest_package_import()
    print("  package import (fresh subprocess) OK")

    class _FakeContent:
        def __init__(self, text):
            self.text = text

    class _FakeResult:
        def __init__(self, text, is_error):
            self.content = [_FakeContent(text)]
            self.isError = is_error

    class _FakeSession:
        version = "2.2.0-rc1"

        def __init__(self):
            self.order = []
            self._responses = {}

        def queue(self, name, text, is_error=False):
            self._responses.setdefault(name, []).append((text, is_error))

        async def initialize(self):
            self.order.append("initialize")

        async def call_tool(self, name, args):
            _selftest_schema_check(name, args, self.version)
            self.order.append(("call", name, dict(args or {})))
            if name not in self._responses or not self._responses[name]:
                return _FakeResult("", False)
            text, is_error = self._responses[name].pop(0)
            return _FakeResult(text, is_error)

    fake = _FakeSession()
    fake.queue("ppal-read-clip", "track index 3 out of range", is_error=True)
    try:
        asyncio.run(call_tool(fake, "ppal-read-clip", {}))
        raise AssertionError("call_tool must raise on isError")
    except RuntimeError as exc:
        assert "Producer Pal tool error" in str(exc)
        assert "out of range" in str(exc)
    fake2 = _FakeSession()
    fake2.queue("ppal-read-clip", '{"result":{"notes":[]}}', is_error=False)
    assert asyncio.run(call_tool(fake2, "ppal-read-clip")).result["notes"] == []
    print("  isError fail-closed OK")

    # Enter the real operation_session body while replacing only the external
    # MCP transport. This catches a missing initialize(), ppal-connect, yield,
    # or context teardown without needing a running Producer Pal endpoint.
    order = []

    class _FakeClientSession(_FakeSession):
        def __init__(self, rd, wr):
            super().__init__()
            self.order = order
            order.append(("client_init", rd, wr))
            # Response shapes: read/connect.json, legacy/legacy_read_clip_path.json
            # (+ midi-json notes string as in mutate/read_track_source.json).
            self.queue("ppal-connect", '{"connected":true,"producerPalVersion":"2.2.0-rc1",'
                                       '"liveSet":{"sceneCount":4,"tempo":120}}')
            self.queue("ppal-read-live-set", '{"sceneCount":4,"tempo":120,"tracks":[]}')
            self.queue("ppal-read-clip", '{"id":"359","path":"t0/s0","type":"midi",'
                                         '"notes":"[{p:60,t:0,d:1,v:80}]"}')

        async def list_tools(self):
            self.order.append("list_tools")
            return types.SimpleNamespace(tools=[types.SimpleNamespace(name="ppal-live-api")])

        async def __aenter__(self):
            order.append("client_enter")
            return self

        async def __aexit__(self, *_exc):
            order.append("client_exit")
            return False

    @asynccontextmanager
    async def _fake_transport(endpoint, headers=None, **_kwargs):
        order.append(("transport_enter", endpoint, headers or {}))
        try:
            yield ("read-stream", "write-stream", None)
        finally:
            order.append("transport_exit")

    fake_mcp = types.ModuleType("mcp")
    fake_mcp.__path__ = []
    fake_mcp.ClientSession = _FakeClientSession
    fake_client = types.ModuleType("mcp.client")
    fake_client.__path__ = []
    fake_http = types.ModuleType("mcp.client.streamable_http")
    fake_http.streamablehttp_client = _fake_transport
    fake_mcp.client = fake_client
    fake_client.streamable_http = fake_http

    async def _exercise_operation_session():
        async with operation_session("http://fake-pp/mcp") as session:
            assert isinstance(session, PPSession)
            overview = await session.call(
                "ppal-read-live-set", {"include": ["tracks"]})
            clip_text = await session.call(
                "ppal-read-clip", {"path": "t0/s0", "include": ["notes"]})
            return overview, clip_text, session

    module_names = ("mcp", "mcp.client", "mcp.client.streamable_http")
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in module_names}
    sys.modules["mcp"] = fake_mcp
    sys.modules["mcp.client"] = fake_client
    sys.modules["mcp.client.streamable_http"] = fake_http
    try:
        overview, clip_text, session = asyncio.run(_exercise_operation_session())
    finally:
        for name, old in previous.items():
            if old is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old

    assert order == [
        ("transport_enter", "http://fake-pp/mcp", {"x-producer-pal-notation": "midi-json"}),
        ("client_init", "read-stream", "write-stream"),
        "client_enter",
        "initialize",
        "list_tools",
        ("call", "ppal-connect", {}),
        ("call", "ppal-read-live-set", {"include": ["tracks"]}),
        ("call", "ppal-read-clip", {"path": "t0/s0", "include": ["notes"]}),
        "client_exit",
        "transport_exit",
    ], order
    assert overview.result["sceneCount"] == 4
    assert extract_notes(clip_text)[0]["pitch"] == 60
    try:
        asyncio.run(call_tool(_FakeSession(), "ppal-read-clip", {"slot": "0/0"}))
        raise AssertionError("selftest fake must reject a non-schema argument")
    except AssertionError as exc:
        assert "unknown arguments" in str(exc), exc
    assert session.handshake["pp_version"] == "2.2.0-rc1"
    assert session.runtime_epoch is None
    handshake_probe = handshake_from_connect(parse_tool_response(
        '{"result":[{"name":"version","value":"2.2.0-rc1"},'
        '{"name":"notation","value":"midi-json"},'
        '{"name":"format","value":"json"}]}'
    ))
    assert handshake_probe["pp_version"] == "2.2.0-rc1"
    assert handshake_probe["observed"]["notation"] == "midi-json"
    assert handshake_probe["capabilities"]["supports_2_2"] is True
    assumed = handshake_from_connect(parse_tool_response(
        '{"result":{"connected":true,"producerPalVersion":"2.2.0-rc1",'
        '"liveSet":{"tempo":120}}}'
    ))
    assert assumed["observed"]["notation"] is None
    validate_handshake(assumed)
    assert handshake_probe["capabilities"]["live_api"] is False
    assert build_live_api_request("t3", [{"type": "set_path", "value": 0}])["path"] == "t3"
    assert extract_live_api_receipts(parse_tool_response(
        '{"result":{"id":"x","results":[{"operation":{"type":"get",'
        '"property":"tempo"},"result":0}]}}'
    )) == [{"operation": {"type": "get", "property": "tempo"}, "result": 0}]
    print("  connect/initialize/ppal-connect order + multi-call OK")

    # Producer Pal 2.3.0 (captured v23/connect.json + v23_read_after_create
    # shapes): same ceremony, the handshake names 2.3 addressing, the request
    # builder emits the path form the 2.3 schema lists, warnings survive.
    fake23 = _FakeSession()
    fake23.version = "2.3.0"
    fake23.queue("ppal-read-track", '{"id":"423","path":"t10","type":"midi",'
                                    '"arrangementClips":[{"id":"425","path":"t10[1|1]"}]}')
    handshake23 = handshake_from_connect(parse_tool_response(
        '{"connected":true,"producerPalVersion":"2.3.0","liveSet":{"sceneCount":8}}'),
        {"ppal-live-api"}, requested={"notation": PP_NOTATION})
    validate_handshake(handshake23)          # 2.3.0 has committed captures
    session23 = PPSession(fake23, handshake23)
    assert session23.capabilities["addressing"] == ADDRESSING_23, session23.capabilities
    args23 = requests_for(session23).read_track(10, include=["arrangement-clips"])
    assert args23 == {"path": "t10", "include": ["arrangement-clips"]}, args23
    track23 = asyncio.run(session23.call("ppal-read-track", args23))
    assert session23.pp_version == "2.3.0"
    assert track_index_of(track23.result) == 10
    assert arrangement_start_of(track23.result["arrangementClips"][0]) == "1|1"
    try:
        _selftest_schema_check("ppal-read-track", {"trackIndex": 10}, "2.3.0")
        raise AssertionError("the 2.3 schema must reject trackIndex")
    except AssertionError as exc:
        assert "unknown arguments" in str(exc), exc

    class _WarnPart:
        def __init__(self, text):
            self.text = text

    class _Warned:
        async def call_tool(self, name, args):
            return types.SimpleNamespace(isError=False, content=[
                _WarnPart('{"deleted":false,"id":"999999","type":"clip"}'),
                _WarnPart('WARNING: id "999999" does not exist')])

    missed = asyncio.run(call_tool(_Warned(), "ppal-delete", {"type": "clip", "id": "999999"}))
    assert missed.warnings == ['WARNING: id "999999" does not exist'], missed.warnings
    assert write_failed(missed, expect="deleted"), "a 2.3 delete miss is a failure"
    print("  Producer Pal 2.3.0 handshake, path addressing and warnings OK")

    # Producer Pal 2.4.0 (captured v24/connect.json): the handshake names the
    # 2.4 generation, which keeps 2.3's paths; `count` left create-track /
    # create-scene, so a batch is one path per object (v24_create_tracks_batch,
    # v24_create_scenes_batch) and the 2.4 schema refuses the old form.
    handshake24 = handshake_from_connect(parse_tool_response(
        '{"abletonLiveVersion":"12.4.6","connected":true,"producerPalVersion":"2.4.0",'
        '"liveSet":{"sceneCount":8}}'), {"ppal-live-api"}, requested={"notation": PP_NOTATION})
    validate_handshake(handshake24)          # 2.4.0 has committed captures
    assert handshake24["capabilities"]["addressing"] == ADDRESSING_24, handshake24
    assert handshake24["capabilities"]["supports_2_3"] and handshake24["capabilities"]["supports_2_4"]
    requests24 = PPRequests(ADDRESSING_24)
    assert requests24.read_track(10, include=["devices"]) == {"path": "t10", "include": ["devices"]}
    assert requests24.append_scenes(count=2, scene_count=8) == {"capture": False, "path": "s+,s+"}
    assert requests24.create_track(count=2) == {"path": "t+,t+"}
    _selftest_schema_check("ppal-create-scene", requests24.append_scenes(count=2, scene_count=8),
                           "2.4.0")
    try:
        _selftest_schema_check("ppal-create-scene", {"count": 1, "path": "s+"}, "2.4.0")
        raise AssertionError("the 2.4 schema must reject count")
    except AssertionError as exc:
        assert "unknown arguments" in str(exc), exc
    print("  Producer Pal 2.4.0 handshake and batch-path forms OK")

    assert require_scene_count(parse_tool_response('{"result":{"sceneCount":8}}')) == 8
    try:
        require_scene_count(parse_tool_response('{"result":{"tracks":[]}}'))
        raise AssertionError("missing sceneCount must raise")
    except LiveWriteVerificationError as exc:
        assert "sceneCount" in str(exc)
    print("  sceneCount fail-closed OK")

    sent = [{"pitch": 60, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 80}]
    try:
        require_verified_notes(
            sent, parse_tool_response(
                '{"result":{"notes":[{"p":62,"s":0,"d":1,"v":80}]}}'
            ), "test slot")
        raise AssertionError("pitch mismatch must raise")
    except LiveWriteVerificationError as exc:
        assert exc.sent == 1
        assert "pitch" in (exc.detail or str(exc))
    good = require_verified_notes(
        sent, parse_tool_response(
            '{"result":{"notes":[{"p":60,"s":0,"d":1,"v":80}]}}'
        ), "test slot ok")
    assert good == 1
    assert verify_notes(sent, parse_tool_response('{"result":{}}'), "missing")['status'] == "unverifiable"
    print("  readback mismatch fail-closed OK")

    # strict midi-json: a bar|beat text payload on a json/midi-json bridge is a
    # protocol violation, never a quiet downgrade to the legacy text parser
    global LEGACY_TEXT_RESPONSE_MODE
    assert LEGACY_TEXT_RESPONSE_MODE is False, "production default is strict midi-json"
    _legacy_text = parse_tool_response('{"result":{"notes":"v90 n/2 C2 1|1"}}')
    try:
        extract_notes(_legacy_text)
    except ValueError:
        pass
    else:
        raise AssertionError("malformed midi-json must raise, not fall back to pp_notes_to_notes")
    _old_mode = LEGACY_TEXT_RESPONSE_MODE
    LEGACY_TEXT_RESPONSE_MODE = True
    try:
        _legacy_parsed = extract_notes(_legacy_text)
    finally:
        LEGACY_TEXT_RESPONSE_MODE = _old_mode
    assert _legacy_parsed == pp_codec.pp_notes_to_notes("v90 n/2 C2 1|1"), \
        "the legacy switch is the only door to the text parser"
    print("  strict midi-json: malformed string raises; legacy switch is the only fallback OK")

    print("pp_session selftest OK")


if __name__ == "__main__":
    run_selftest()
