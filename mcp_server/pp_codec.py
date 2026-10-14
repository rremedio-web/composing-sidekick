"""Pure Producer Pal wire codec — parsing, serialization, clip classification.

Stdlib only. Must not import the MCP SDK, live_write, or core.
Consumers: live_write, core (re-exports), moment (adapter boundary).
"""

from __future__ import annotations

import math
import json
import re
import sys
from enum import Enum
from fractions import Fraction

# ONE module universe regardless of import style ('pp_codec' vs 'mcp_server.pp_codec').
for _alias in ("pp_codec", "mcp_server.pp_codec"):
    sys.modules.setdefault(_alias, sys.modules[__name__])


# ---------------------------------------------------------------------------
# Constants (mirrored; no analyze/style_profile import)
# ---------------------------------------------------------------------------

PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
BEATS_PER_BAR = 4
MAX_PP_DURATION_DENOMINATOR = 100_000
MAX_PP_DURATION_ERROR_BEATS = 1e-6
READBACK_TIME_TOLERANCE_BEATS = 0.001
MIDI_JSON_VERSION = "1"
MIDI_JSON_DEFAULT_START = 0.0
MIDI_JSON_DEFAULT_DURATION = 1.0
MIDI_JSON_DEFAULT_VELOCITY = 100

_LETTER_PC: dict[str, int] = {
    "C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11,
}
_PITCH_NAME_RE = re.compile(r"^([A-Ga-g])([#b]?)(-?\d+)$")
_VEL_TOKEN_RE = re.compile(r"^v(\d+)$", re.IGNORECASE)
_PROB_TOKEN_RE = re.compile(r"^p(\d+(?:\.\d+)?)$", re.IGNORECASE)
_DUR_TOKEN_RE = re.compile(r"^(n(?:\d+(?:\.\d+)?)?/\d+|\d+bar)$", re.IGNORECASE)
_POS_OFFSET_RE = re.compile(
    r"^(\d+)\|([\d.]+)(([+-])n(\d+(?:\.\d+)?)?/(\d+))?$",
    re.IGNORECASE,
)

_CLIP_TYPE_RE = re.compile(
    r'\btype\s*:\s*(null|["\'](audio|midi)["\'])',
    re.IGNORECASE,
)
_CLIP_ID_RE = re.compile(
    r'\bid\s*:\s*(null|["\']([^"\']*)["\'])',
    re.IGNORECASE,
)
_EMPTY_SLOT_CORE = (
    r'(?:no clip at (?:\d+/\d+|t\d+/s\d+|trackIndex\s+\d+(?:,\s*sceneIndex\s+\d+)?)'
    # PP 2.2.0-rc1 read-clip of a slot in a scene that does not exist
    # (tests/fixtures/pp/2.2.0-rc1/legacy/legacy_read_clip_empty_slot.json).
    # An EXISTING empty slot is not an error: it reads {id: null, …}
    # (check_read_clip_existing_empty_slot.json) and classifies as empty.
    r'|no scene at "?s\d+"?)'
)
_EMPTY_SLOT_ERROR = re.compile(
    r'^(?:Producer Pal tool error:\s*)?'
    # 2.2 wraps the device text in "Error executing tool '<tool>': ", 2.3
    # prefixes a bare "Error: " (2.3.0/legacy/legacy_read_clip_empty_slot.json).
    r"(?:Error executing tool '[\w-]+':\s*|Error:\s*)?"
    + _EMPTY_SLOT_CORE
    + r'[.!]?\s*$',
    re.IGNORECASE,
)
_LIVE_OBJECT_PATH_RE = re.compile(r"^t(\d+)(?:/(s|l)(\d+))?$")


# ---------------------------------------------------------------------------
# Wire scalar / object / track / clip parsers
# ---------------------------------------------------------------------------

def parse_live_object_path(path: str):
    """Parse Producer Pal compact object paths: tN, tN/sM session slot, tN/lM lane."""
    if not isinstance(path, str):
        return None
    match = _LIVE_OBJECT_PATH_RE.fullmatch(path.strip())
    if not match:
        return None
    return int(match.group(1)), match.group(2), (
        int(match.group(3)) if match.group(3) else None)


def is_lane_object_path(path: str) -> bool:
    """'t3/l0' (2.2), or a take-lane clip by position 't3/l0[5|1]' (2.3 form,
    from the 2.3 create-clip schema; no 2.3 lane read is captured)."""
    if isinstance(path, str) and re.fullmatch(r"t\d+/l\d+\[[^\]]+\]", path.strip()):
        return True
    parsed = parse_live_object_path(path)
    return parsed is not None and parsed[1] == "l"


def clip_session_slot(clip: dict) -> str | None:
    """Normalize a session clip record to ``track/scene`` when possible."""
    if not isinstance(clip, dict):
        return None
    slot = clip.get("slot")
    if isinstance(slot, str) and "/" in slot:
        return slot
    track_index = clip.get("trackIndex")
    scene_index = clip.get("sceneIndex")
    if track_index is not None and scene_index is not None:
        return "%s/%s" % (track_index, scene_index)
    parsed = parse_live_object_path(clip.get("path"))
    if parsed and parsed[1] == "s":
        return "%s/%s" % (parsed[0], parsed[2])
    return None


# ---------------------------------------------------------------------------
# Field readers: one value from either wire generation (2.2 numeric fields,
# 2.3 object paths — 't3', 'rt0', 'mt', 's2', 't3/s0', 't3[5|1]')
# ---------------------------------------------------------------------------

_TRACK_PATH_RE = re.compile(r"t(\d+)")
_RETURN_PATH_RE = re.compile(r"rt(\d+)")
_CLIP_TRACK_RE = re.compile(r"t(\d+)(?=$|[/\[])")
_ARRANGEMENT_PATH_RE = re.compile(r"t\d+(?:/l\d+)?\[(?P<position>[^\]]+)\]")
BAR_BEAT_RE = re.compile(r"[1-9]\d*\|\d+(?:\.\d+)?")
_SCENE_PATH_RE = re.compile(r"(?:t\d+/)?s(\d+)")


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def track_index_of(record) -> int | None:
    """A regular track's index: 2.2 ``trackIndex``, else the 2.3 track path 't3'.
    Clip/device paths ('t3/s0') and return/main paths name no regular track."""
    if not isinstance(record, dict):
        return None
    index = _int_or_none(record.get("trackIndex"))
    if index is not None:
        return index
    path = record.get("path")
    match = _TRACK_PATH_RE.fullmatch(path) if isinstance(path, str) else None
    return int(match.group(1)) if match else None


def return_index_of(record) -> int | None:
    """A return track's index: 2.2 ``returnTrackIndex``, else the 2.3 path 'rt0'."""
    if not isinstance(record, dict):
        return None
    index = _int_or_none(record.get("returnTrackIndex"))
    if index is not None:
        return index
    path = record.get("path")
    match = _RETURN_PATH_RE.fullmatch(path) if isinstance(path, str) else None
    return int(match.group(1)) if match else None


def is_main_track(record) -> bool:
    """The main (master) track: 2.2 ``type: master``, 2.3 path 'mt'."""
    return isinstance(record, dict) and (record.get("path") == "mt"
                                         or record.get("type") == "master")


def main_track_of(overview) -> dict | None:
    """The overview's main track: 2.2 ``masterTrack``, 2.3 ``mainTrack``."""
    if not isinstance(overview, dict):
        return None
    for key in ("mainTrack", "masterTrack"):
        if isinstance(overview.get(key), dict):
            return overview[key]
    return None


def clip_track_index(clip) -> int | None:
    """The track a clip record sits on: ``trackIndex``, else its path's
    leading 't<i>' (session 't3/s0', arrangement 't3[5|1]' or 2.2 't3', lane)."""
    if not isinstance(clip, dict):
        return None
    index = _int_or_none(clip.get("trackIndex"))
    if index is not None:
        return index
    path = clip.get("path")
    match = _CLIP_TRACK_RE.match(path) if isinstance(path, str) else None
    return int(match.group(1)) if match else None


def arrangement_start_of(clip) -> str | None:
    """An arrangement clip's bar|beat start: 2.2 ``arrangementStart``, else the
    position inside the 2.3 path ('t3[5|1]', lane 't3/l0[5|1]'). A locator
    position ('t3[loc:Chorus]') names no bar|beat and yields None."""
    if not isinstance(clip, dict):
        return None
    start = clip.get("arrangementStart")
    if isinstance(start, str) and start:
        return start
    path = clip.get("path")
    match = _ARRANGEMENT_PATH_RE.fullmatch(path) if isinstance(path, str) else None
    if match and BAR_BEAT_RE.fullmatch(match.group("position")):
        return match.group("position")
    return None


def find_overlapping_spans(spans) -> list[tuple]:
    """The pairs of ``(label, start, end)`` half-open spans that overlap —
    the preflight every writer that places copies runs on its own plan:
    Live replaces whatever a copy lands on, and Producer Pal does not say
    so (2.4 captured: v24_duplicate_over_existing replied ``{id, path}``
    only while the clip at that position vanished — v24_split_clip then
    said its id does not exist). Pure: beats in, labels out."""
    items = []
    for label, start, end in spans:
        start, end = float(start), float(end)
        if end <= start:
            raise ValueError("span %r has no length (%s to %s)" % (label, start, end))
        items.append((start, end, label))
    items.sort(key=lambda item: (item[0], item[1]))
    out = []
    for i, (start_a, end_a, label_a) in enumerate(items):
        for start_b, end_b, label_b in items[i + 1:]:
            if start_b >= end_a:
                break
            out.append((label_a, label_b))
    return out


def scene_index_of(record) -> int | None:
    """A scene index: 2.2 ``sceneIndex``, else a 2.3 scene path 's2' or the
    scene of a session-slot path 't3/s2'."""
    if not isinstance(record, dict):
        return None
    index = _int_or_none(record.get("sceneIndex"))
    if index is not None:
        return index
    path = record.get("path")
    match = _SCENE_PATH_RE.fullmatch(path) if isinstance(path, str) else None
    return int(match.group(1)) if match else None



def find_int(text, key, default=None):
    """Pull `key:<int>` from Producer Pal's JS-object-literal text output."""
    m = re.search(rf'{re.escape(key)}:\s*(\d+)', text)
    return int(m.group(1)) if m else default


def find_str(text: str, key: str, default=None):
    """Pull `key:"value"` from Producer Pal's JS-object-literal text output."""
    m = re.search(rf'{re.escape(key)}:\s*"([^"]*)"', text)
    return m.group(1) if m else default


def extract_object_block(text: str, key: str) -> str:
    m = re.search(rf'{re.escape(key)}:\s*\{{([^{{}}]*)\}}', text or "")
    return m.group(1) if m else ""


def extract_selected_context(text: str) -> dict:
    """Best-effort parse of `ppal-select` with no args."""
    track_block = extract_object_block(text, "selectedTrack")
    scene_block = extract_object_block(text, "selectedScene")
    clip_block = extract_object_block(text, "selectedClip")
    device_block = extract_object_block(text, "selectedDevice")
    return {
        "view": find_str(text, "view"),
        "selected_track": {
            "id": find_str(track_block, "id"),
            "type": find_str(track_block, "type"),
            "track_index": find_int(track_block, "trackIndex"),
        } if track_block else None,
        "selected_scene": {
            "id": find_str(scene_block, "id"),
            "scene_index": find_int(scene_block, "sceneIndex"),
        } if scene_block else None,
        "selected_clip": {
            "id": find_str(clip_block, "id"),
            "slot": find_str(clip_block, "slot"),
            "name": find_str(clip_block, "name"),
        } if clip_block else None,
        "selected_device": {
            "id": find_str(device_block, "id"),
            "path": find_str(device_block, "path"),
        } if device_block else None,
    }


def extract_pp_notes(text: str) -> str:
    """Pull the Producer Pal notes string from read-clip / read-track text."""
    if not text:
        return ""
    m = re.search(r"""notes:\s*['"`](.*?)['"`]""", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"notes:\s*([^\n}]+)", text)
    if m:
        return m.group(1).strip().strip("'\"")
    return ""


def balanced_objects(text: str):
    """Yield every balanced {...} block in `text` (all depths, string-aware),
    innermost first."""
    stack: list[int] = []
    in_str = esc = False
    for i, ch in enumerate(text or ""):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            yield text[stack.pop():i + 1]


def strip_nested(block_inner: str) -> str:
    """Remove ALL nested {...} levels, leaving only the block's own fields."""
    prev = None
    while prev != block_inner:
        prev = block_inner
        block_inner = re.sub(r"\{[^{}]*\}", "", block_inner)
    return block_inner


def extract_arrangement_clip_notes(text: str, clip_index: int = 0,
                                   clip_name: str | None = None) -> str:
    """Extract one arrangement clip's notes, preferring its stable clip name."""
    match = re.search(
        r'(?:(?:"|\')?(?:arrangement-clips|arrangementClips)(?:"|\')?)\s*:\s*\[',
        text or "",
    )
    if not match:
        return extract_pp_notes(text)

    start = match.end() - 1
    depth = 0
    quote = None
    escaped = False
    end = None
    for index in range(start, len(text)):
        char = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                end = index
                break
    if end is None:
        return ""

    clips = [block for block in balanced_objects(text[start + 1:end])
             if extract_pp_notes(block)]
    if not clips:
        return ""
    if clip_name is not None:
        matches = []
        for block in clips:
            name_match = re.search(r'name\s*:\s*"((?:[^"\\]|\\.)*)"', block)
            if name_match:
                actual_name = re.sub(r"\\(.)", r"\1", name_match.group(1))
                if actual_name == clip_name:
                    matches.append(extract_pp_notes(block))
        return matches[0] if len(matches) == 1 else ""
    idx = min(max(0, clip_index), len(clips) - 1)
    return extract_pp_notes(clips[idx])


def max_scene_index_in_track_text(text: str) -> int | None:
    """Largest scene index visible in a ppal-read-track session-clip listing."""
    if not text:
        return None
    scenes = [int(m) for m in re.findall(r"sceneIndex:\s*(\d+)", text)]
    scenes += [int(m) for m in re.findall(r'slot:\s*"\d+/(\d+)"', text)]
    return max(scenes) if scenes else None


def parse_session_clips(listing: str) -> list[dict]:
    """(slot, name) for every session clip in a ppal-read-track listing."""
    out: list[dict] = []
    for block in balanced_objects(listing or ""):
        flat = strip_nested(block[1:-1])
        m_name = re.search(r'name\s*:\s*"((?:[^"\\]|\\.)*)"', flat)
        if not m_name:
            continue
        m_slot = re.search(r'slot\s*:\s*"(\d+/\d+)"', flat)
        slot = m_slot.group(1) if m_slot else None
        if slot is None:
            m_scene = re.search(r"sceneIndex\s*:\s*(\d+)", flat)
            m_track = re.search(r"trackIndex\s*:\s*(\d+)", flat)
            if m_scene and m_track:
                slot = "%s/%s" % (m_track.group(1), m_scene.group(1))
        if slot is None:
            continue
        out.append({"slot": slot,
                    "name": re.sub(r"\\(.)", r"\1", m_name.group(1))})
    return out


def track_index_by_name(overview: str, name: str) -> tuple[int | None, str | None]:
    """(trackIndex, id) of the first track named `name` in a tracks listing."""
    for block in balanced_objects(overview):
        flat = strip_nested(block[1:-1])
        m_idx = re.search(r'trackIndex\s*:\s*(\d+)', flat)
        m_name = re.search(r'name\s*:\s*"((?:[^"\\]|\\.)*)"', flat)
        if not m_idx or not m_name:
            continue
        if re.sub(r"\\(.)", r"\1", m_name.group(1)) != name:
            continue
        m_id = re.search(r'\bid\s*:\s*"?([\w-]+)"?', flat)
        return int(m_idx.group(1)), (m_id.group(1) if m_id else None)
    return None, None


def regular_track_count(overview: str) -> int:
    """Count tracks; prefer regularTrackCount when present."""
    n = find_int(overview, "regularTrackCount")
    if n is not None:
        return n
    return len(re.findall(r"trackIndex\s*:\s*\d+", overview or ""))


def track_names_by_index(overview: str) -> dict:
    """Map trackIndex → track name from a read-live-set tracks listing."""
    out: dict[int, str] = {}
    for block in balanced_objects(overview or ""):
        flat = strip_nested(block[1:-1])
        m_idx = re.search(r'trackIndex\s*:\s*(\d+)', flat)
        m_name = re.search(r'name\s*:\s*"((?:[^"\\]|\\.)*)"', flat)
        if not m_idx or not m_name:
            continue
        out[int(m_idx.group(1))] = re.sub(r"\\(.)", r"\1", m_name.group(1))
    return out


# ---------------------------------------------------------------------------
# Clip presence / empty-slot classification
# ---------------------------------------------------------------------------

def clip_type_token(raw: str) -> str | None:
    """Return 'audio' | 'midi' | 'null' | None (field absent / unreadable)."""
    m = _CLIP_TYPE_RE.search(raw or "")
    if not m:
        return None
    if m.group(1).lower() == "null":
        return "null"
    return (m.group(2) or "").lower() or None


def clip_id_token(raw: str) -> str | None:
    """Return 'null' | the string id | None if the id field is absent."""
    m = _CLIP_ID_RE.search(raw or "")
    if not m:
        return None
    if m.group(1).lower() == "null":
        return "null"
    return m.group(2) if m.group(2) is not None else ""


_EMPTY_SLOT_PLACE = re.compile(
    r"no clip at (?:(?P<t1>\d+)/(?P<s1>\d+)|t(?P<t2>\d+)/s(?P<s2>\d+)"
    r"|trackIndex\s+(?P<t3>\d+)(?:,\s*sceneIndex\s+(?P<s3>\d+))?)"
    r"|no scene at \"?s(?P<s4>\d+)\"?", re.IGNORECASE)


def empty_slot_error_place(exc_or_text) -> tuple | None:
    """Where an empty-slot error says there is nothing: ``(track, scene)``
    for "no clip at …" (scene None when the text names only a track),
    ``(None, scene)`` for "no scene at sN"; None when the text is not a
    whole empty-slot error."""
    text = str(exc_or_text or "").strip()
    if not _EMPTY_SLOT_ERROR.fullmatch(text):
        return None
    match = _EMPTY_SLOT_PLACE.search(text)
    if match is None:
        return None
    if match.group("s4") is not None:
        return None, int(match.group("s4"))
    track = next(int(match.group(k)) for k in ("t1", "t2", "t3") if match.group(k) is not None)
    scene = next((int(match.group(k)) for k in ("s1", "s2", "s3") if match.group(k) is not None),
                 None)
    return track, scene


def is_empty_slot_error(exc_or_text, path: str | None = None) -> bool:
    """True only when the entire message is an empty-slot error (full match)
    — and, with ``path`` ('t3/s0'), only when it names that slot: "no clip at
    t3/s0" (2.4.0/v24/v24_read_clip_empty_slot) or "no scene at "s0"" for
    its scene (legacy_read_clip_empty_slot). Another slot's error frees
    nothing."""
    place = empty_slot_error_place(exc_or_text)
    if place is None:
        return False
    if path is None:
        return True
    parsed = parse_live_object_path(path)
    if parsed is None or parsed[1] != "s":
        return False
    track, scene = place
    return scene == parsed[2] and track in (None, parsed[0])


def classify_session_clip(raw: str, notes) -> str:
    """Classify a session-slot read for the Moment (Producer Pal 2.0 shapes).

    Returns one of:
      ``heard`` | ``unheard_audio`` | ``unheard_unknown`` | ``empty`` | ``invalid``
    """
    text = raw or ""
    if notes:
        return "heard"

    ctype = clip_type_token(text)
    if ctype == "null":
        return "empty"
    if ctype == "audio":
        return "unheard_audio"
    if ctype == "midi":
        return "empty"

    cid = clip_id_token(text)
    if cid == "null":
        return "empty"
    if cid:
        return "unheard_unknown"

    if not text.strip():
        return "empty"
    if is_empty_slot_error(text):
        return "empty"
    return "invalid"


# ---------------------------------------------------------------------------
# Note wire codec
# ---------------------------------------------------------------------------

class CodecMode(str, Enum):
    """Producer Pal notation selected globally by the launched PP process."""

    BARBEAT_TOKENS = "bar|beat"
    MIDI_JSON = "midi-json"


_MIDI_JSON_KEYS = frozenset({
    "p", "pitch", "t", "s", "start", "d", "duration", "v", "velocity",
    "vd", "velocityDeviation", "deviation", "c", "probability",
})


def _midi_json_number(value, field: str, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"midi-json {field} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"midi-json {field} must be finite")
    if integer:
        if not number.is_integer():
            raise ValueError(f"midi-json {field} must be an integer")
        return int(number)
    return number


def notes_to_midi_json_payload(notes: list[dict], **_kwargs) -> list[dict]:
    """Encode notes as Producer Pal's short-key MIDI JSON array.

    Both Producer Pal 2.1 and 2.2 use ``p`` (pitch), ``t`` (start), ``d``
    (duration), and ``v`` (velocity). Start is mandatory even when zero.
    """
    payload: list[dict] = []
    for note in notes:
        source = note_dicts([note])[0]
        pitch = _midi_json_number(source["pitch"], "p", integer=True)
        start = _midi_json_number(source["start"], "t")
        duration = _midi_json_number(source["duration"], "d")
        # Match the established bar|beat writer: MIDI note velocity 0 is
        # canonicalized to 1 rather than silently producing a dropped note.
        velocity = max(1, min(127, _midi_json_number(
            source["velocity"], "v", integer=True)))
        if not 0 <= pitch <= 127:
            raise ValueError(f"midi-json pitch outside MIDI range: {pitch}")
        if duration <= 0:
            raise ValueError("midi-json duration must be greater than zero")
        if not 1 <= velocity <= 127:
            raise ValueError(f"midi-json velocity outside MIDI range: {velocity}")
        item = {"p": pitch, "t": start, "d": duration, "v": velocity}
        payload.append(item)
    return payload


def _midi_json_literal(text: str) -> list[dict] | dict:
    """Parse Producer Pal's canonical JS-literal MIDI string without eval."""
    raw = text.strip()
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        decoded = None
    if isinstance(decoded, (list, dict)):
        return decoded
    if not (raw.startswith("[") and raw.endswith("]")):
        raise ValueError("malformed midi-json payload")
    body = raw[1:-1].strip()
    if not body:
        return []
    object_pattern = r"\{[^{}]*\}"
    if re.fullmatch(rf"\s*{object_pattern}(?:\s*,\s*{object_pattern})*\s*", body) is None:
        raise ValueError("malformed midi-json payload")
    chunks = re.findall(object_pattern, body)
    if not chunks:
        raise ValueError("malformed midi-json payload")

    def number(token: str):
        value = token.strip()
        ratio = re.fullmatch(r"(-?(?:\d+(?:\.\d+)?|\.\d+))/(\d+)", value)
        if ratio:
            denominator = int(ratio.group(2))
            if denominator == 0:
                raise ValueError("midi-json ratio denominator must not be zero")
            return float(ratio.group(1)) / denominator
        if not re.fullmatch(r"-?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?", value):
            raise ValueError("midi-json values must be numeric")
        parsed = float(value)
        return int(parsed) if parsed.is_integer() else parsed

    out = []
    for chunk in chunks:
        item = {}
        fields = chunk[1:-1].strip()
        if not fields:
            out.append(item)
            continue
        for field in fields.split(","):
            if ":" not in field:
                raise ValueError("malformed midi-json note")
            key, value = field.split(":", 1)
            key = key.strip().strip("\"'")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", key):
                raise ValueError("malformed midi-json key")
            item[key] = number(value)
        out.append(item)
    return out


def midi_json_to_notes(payload) -> list[dict]:
    """Decode Producer Pal MIDI JSON, including its canonical bare-key string.

    For offline fixtures the optional ``{"version":"1","notes": [...]}``
    envelope is accepted too; the live PP response adapter unwraps its normal
    ``result`` envelope before calling this function.
    """
    if isinstance(payload, str):
        payload = _midi_json_literal(payload)
    if isinstance(payload, dict):
        version = payload.get("version")
        if version != MIDI_JSON_VERSION:
            raise ValueError(f"unknown midi-json version: {version!r}")
        payload = payload.get("notes")
    if not isinstance(payload, list):
        raise ValueError("midi-json payload must be a JSON array")

    notes: list[dict] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"midi-json note {index} must be an object")
        unknown = set(item) - _MIDI_JSON_KEYS
        pitch_value = item.get("p", item.get("pitch"))
        has_t = "t" in item or "start" in item
        has_s = "s" in item
        if unknown or pitch_value is None or not (has_t or has_s) or (has_t and has_s):
            raise ValueError(
                f"midi-json note {index} has wrong or missing short keys: "
                f"{sorted(unknown)}"
            )
        pitch = _midi_json_number(pitch_value, "p", integer=True)
        start = _midi_json_number(
            item.get("t", item.get("start", item.get("s"))), "t")
        duration = _midi_json_number(
            item.get("d", item.get("duration", MIDI_JSON_DEFAULT_DURATION)), "d")
        velocity = _midi_json_number(
            item.get("v", item.get("velocity", MIDI_JSON_DEFAULT_VELOCITY)),
            "v", integer=True)
        if not 0 <= pitch <= 127:
            raise ValueError(f"midi-json pitch outside MIDI range: {pitch}")
        if duration <= 0:
            raise ValueError("midi-json duration is out of range")
        if not 1 <= velocity <= 127:
            raise ValueError(f"midi-json velocity outside MIDI range: {velocity}")
        notes.append({
            "pitch": pitch,
            "start_beats": start,
            "duration_beats": duration,
            "velocity": velocity,
        })
    return notes


def encode_notes(notes: list[dict], mode: CodecMode = CodecMode.BARBEAT_TOKENS):
    """Encode notes using the explicitly selected Producer Pal notation."""
    mode = CodecMode(mode)
    if mode is CodecMode.MIDI_JSON:
        return json.dumps(notes_to_midi_json_payload(notes), separators=(",", ":"))
    if mode is CodecMode.BARBEAT_TOKENS:
        return notes_to_pp_string(note_dicts(notes))[0]
    raise ValueError(f"unsupported Producer Pal codec mode: {mode!r}")


def decode_notes(payload, mode: CodecMode = CodecMode.BARBEAT_TOKENS) -> list[dict]:
    """Decode notes using the explicitly selected Producer Pal notation."""
    mode = CodecMode(mode)
    if mode is CodecMode.MIDI_JSON:
        return midi_json_to_notes(payload)
    if mode is CodecMode.BARBEAT_TOKENS:
        return pp_notes_to_notes(str(payload or ""))
    raise ValueError(f"unsupported Producer Pal codec mode: {mode!r}")

def note_dicts(notes: list[dict]) -> list[dict]:
    """Normalise MCP note shape → build_index shape (for PP write path)."""
    out = []
    for n in notes:
        out.append({
            "pitch": int(n["pitch"]),
            "start": float(n.get("start_beats", n.get("start", 0))),
            "duration": float(n.get("duration_beats", n.get("duration", 0.25))),
            "velocity": int(n.get("velocity", 80)),
        })
    return out


def midi_to_pitch_name(midi: int) -> str:
    return f"{PITCH_NAMES[midi % 12]}{midi // 12 - 1}"


def pitch_name_to_midi(name: str) -> int:
    """Producer Pal pitch name (C4, Db5, A#3) → MIDI note number."""
    m = _PITCH_NAME_RE.match(name.strip())
    if not m:
        raise ValueError(f"invalid pitch name: {name!r}")
    letter, acc, oct_s = m.group(1).upper(), m.group(2), m.group(3)
    pc = _LETTER_PC[letter]
    if acc == "#":
        pc += 1
    elif acc == "b":
        pc -= 1
    octave = int(oct_s)
    return (octave + 1) * 12 + (pc % 12)


def is_pp_duration_token(tok: str) -> bool:
    return bool(_DUR_TOKEN_RE.match(tok))


def is_pp_pitch_token(tok: str) -> bool:
    return bool(_PITCH_NAME_RE.match(tok))


def is_pp_prob_token(tok: str) -> bool:
    """PP 2.0 probability token p<float>: recognized only for a value in [0,1]."""
    m = _PROB_TOKEN_RE.match(tok)
    return bool(m) and 0.0 <= float(m.group(1)) <= 1.0


def bar_beat_to_beats(bar_beat: str, beats_per_bar: float = BEATS_PER_BAR) -> float:
    """Producer Pal bar|beat → 0-based quarter-note beats."""
    bar_s, beat_s = bar_beat.split("|", 1)
    bar = int(bar_s)
    beat = float(beat_s)
    return (bar - 1) * beats_per_bar + (beat - 1)


def pp_duration_to_beats(token: str, beats_per_bar: float = BEATS_PER_BAR) -> float:
    """Producer Pal duration → quarter-note beats: ``Nbar``, ``n[K]/D``, and the
    compound ``Nbar+n[K]/D`` / ``Nbar-n[K]/D`` forms arrangement lengths use."""
    token = token.strip()
    for sign in ("+", "-"):
        if sign in token[1:]:
            head, tail = token.split(sign, 1)
            tail_beats = pp_duration_to_beats(tail, beats_per_bar)
            return pp_duration_to_beats(head, beats_per_bar) + (tail_beats if sign == "+" else -tail_beats)
    if token.endswith("bar"):
        n = int(token[:-3] or "1")
        return n * beats_per_bar
    if token.startswith("n") and "/" in token:
        num_s, den_s = token[1:].split("/", 1)
        numerator = float(num_s) if num_s else 1.0
        return numerator * 4.0 / int(den_s)
    raise ValueError(f"unknown duration token: {token}")


def parse_pp_position_beats(pos: str) -> float:
    """bar|beat (+ optional ±n/D offset) → 0-based quarter-note beats."""
    pos = pos.strip()
    m = _POS_OFFSET_RE.match(pos)
    if not m:
        raise ValueError(f"invalid position: {pos!r}")
    beats = bar_beat_to_beats(f"{m.group(1)}|{m.group(2)}")
    if m.group(3):
        numerator = float(m.group(5)) if m.group(5) else 1.0
        off = numerator * pp_duration_to_beats(f"n/{m.group(6)}")
        if m.group(4) == "-":
            off = -off
        beats += off
    return beats


def expand_pp_positions(pos_token: str) -> list[str]:
    """Expand comma-separated bar|beat list (sticky bar for bare beat items)."""
    sticky_bar: str | None = None
    out: list[str] = []
    for raw in pos_token.split(","):
        part = raw.strip()
        if not part:
            continue
        if "|" in part:
            bar_s, beat_s = part.split("|", 1)
            sticky_bar = bar_s
            out.append(f"{bar_s}|{beat_s}")
        elif sticky_bar is not None:
            out.append(f"{sticky_bar}|{part}")
        else:
            raise ValueError(f"position missing bar: {pos_token!r}")
    return out


def pp_notes_to_notes(pp_str: str) -> list[dict]:
    """Parse Producer Pal notes string → list of note dicts."""
    if not pp_str or not pp_str.strip():
        return []

    pp_str = pp_str.replace("\\n", " ").replace("\n", " ")
    tokens = pp_str.split()
    notes: list[dict] = []
    velocity = 80
    duration_token = "n/4"
    i = 0

    while i < len(tokens):
        tok = tokens[i]
        vm = _VEL_TOKEN_RE.match(tok)
        if vm:
            velocity = max(0, min(127, int(vm.group(1))))
            i += 1
            continue
        if is_pp_prob_token(tok):
            i += 1
            continue
        if is_pp_duration_token(tok):
            duration_token = tok.lower() if tok[0].isdigit() else tok
            i += 1
            continue
        if is_pp_pitch_token(tok):
            pitch = pitch_name_to_midi(tok)
            dur_beats = pp_duration_to_beats(duration_token)
            i += 1
            while i < len(tokens) and not (
                _VEL_TOKEN_RE.match(tokens[i])
                or is_pp_prob_token(tokens[i])
                or is_pp_duration_token(tokens[i])
                or is_pp_pitch_token(tokens[i])
            ):
                for pos in expand_pp_positions(tokens[i]):
                    if velocity < 1:
                        continue
                    notes.append({
                        "pitch": pitch,
                        "start_beats": parse_pp_position_beats(pos),
                        "duration_beats": dur_beats,
                        "velocity": velocity,
                    })
                i += 1
            continue
        i += 1

    notes.sort(key=lambda n: (n["start_beats"], n["pitch"]))
    return notes


def beats_to_bar_beat(beats: float, beats_per_bar: float = BEATS_PER_BAR) -> str:
    """0-based quarter-note beats from clip start → Producer Pal bar|beat."""
    beats = max(0.0, beats)
    bar = int(beats // beats_per_bar) + 1
    beat_in_bar = beats % beats_per_bar
    beat_num = int(beat_in_bar) + 1
    frac = beat_in_bar - int(beat_in_bar)
    if frac > 1e-6:
        return f"{bar}|{beat_num + frac}"
    return f"{bar}|{beat_num}"


def duration_to_pp(dur_beats: float) -> str:
    """Quarter-note beat duration → Producer Pal n/D token (4/4 assumed)."""
    dur_beats = float(dur_beats)
    if not math.isfinite(dur_beats) or dur_beats <= 0:
        raise ValueError("duration must be finite and greater than zero")
    beats = Fraction(str(dur_beats))
    mapping = {
        Fraction(1, 4): "n/16",
        Fraction(1, 2): "n/8",
        Fraction(1): "n/4",
        Fraction(2): "n/2",
    }
    if beats in mapping:
        return mapping[beats]
    bars = beats / BEATS_PER_BAR
    if bars.denominator == 1 and bars.numerator >= 1:
        return "1bar" if bars.numerator == 1 else f"{bars.numerator}bar"

    note_value = beats / BEATS_PER_BAR
    if note_value.denominator > MAX_PP_DURATION_DENOMINATOR:
        low, high = 1, MAX_PP_DURATION_DENOMINATOR
        while low < high:
            middle = (low + high) // 2
            candidate = note_value.limit_denominator(middle)
            error = abs(float(candidate * BEATS_PER_BAR) - dur_beats)
            if error < MAX_PP_DURATION_ERROR_BEATS:
                high = middle
            else:
                low = middle + 1
        note_value = note_value.limit_denominator(low)
    encoded_beats = note_value * BEATS_PER_BAR
    if abs(float(encoded_beats) - dur_beats) >= MAX_PP_DURATION_ERROR_BEATS:
        raise ValueError(
            f"duration {dur_beats} cannot be represented within "
            f"{MAX_PP_DURATION_ERROR_BEATS} beats"
        )
    return f"n{note_value.numerator}/{note_value.denominator}"


def normalize_notes_for_pp(nd: list[dict], *, time_eps: float = 1e-6) -> list[dict]:
    """Deterministic PP write normalization: exact-dup drop + same-pitch overlap truncate."""
    if not nd:
        return []
    # Clamp negative starts to 0 exactly like the legacy bar|beat writer
    # (beats = max(0.0, beats)) so verification compares against the same
    # canonical wire payload PP actually receives. The decoder/codec still
    # accepts negative starts because Producer Pal reads pickup notes before
    # the playable region; this write normalizer intentionally clamps them.
    sorted_notes = sorted(
        ({**n, "start": max(0.0, n["start"])} for n in nd),
        key=lambda x: (x["start"], x["pitch"]),
    )
    deduped: list[dict] = []
    for n in sorted_notes:
        if deduped:
            prev = deduped[-1]
            if (
                n["pitch"] == prev["pitch"]
                and abs(n["start"] - prev["start"]) <= time_eps
                and abs(n["duration"] - prev["duration"]) <= time_eps
                and n["velocity"] == prev["velocity"]
            ):
                continue
        deduped.append(n)
    if not deduped:
        return []
    out: list[dict] = []
    last_idx_by_pitch: dict[int, int] = {}
    for n2 in deduped:
        pitch = int(n2["pitch"])
        prev_i = last_idx_by_pitch.get(pitch)
        if prev_i is not None:
            n1 = out[prev_i]
            if n2["start"] < n1["start"] + n1["duration"] - time_eps:
                truncated = dict(n1)
                truncated["duration"] = max(0.01, n2["start"] - n1["start"])
                out[prev_i] = truncated
        last_idx_by_pitch[pitch] = len(out)
        out.append(dict(n2))
    return out


def pp_normalized_note_count(notes: list[dict]) -> int:
    """Note count after PP write normalization (dedup + overlap truncate)."""
    return len(normalize_notes_for_pp(note_dicts(notes)))


def notes_to_pp_string(nd: list[dict]) -> tuple[str, list[dict]]:
    """Translate normalised notes → Producer Pal notes string + structured mapping."""
    nd = normalize_notes_for_pp(nd)
    parts: list[str] = []
    translated: list[dict] = []
    for n in sorted(nd, key=lambda x: (x["start"], x["pitch"])):
        pos = beats_to_bar_beat(n["start"])
        dur_tok = duration_to_pp(n["duration"])
        pitch = midi_to_pitch_name(n["pitch"])
        vel = max(1, min(127, n["velocity"]))
        parts.extend([f"v{vel}", dur_tok, pitch, pos])
        translated.append({
            "pitch": pitch,
            "pitch_midi": n["pitch"],
            "position_bar_beat": pos,
            "start_beats": n["start"],
            "duration_token": dur_tok,
            "duration_beats": n["duration"],
            "velocity": vel,
        })
    return " ".join(parts), translated


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------

def _assert_notes_match(src: list[dict], back: list[dict], label: str) -> None:
    assert len(back) == len(src), f"{label}: note count {len(back)} != {len(src)}"
    src_s = sorted(src, key=lambda n: (n["start_beats"], n["pitch"]))
    back_s = sorted(back, key=lambda n: (n["start_beats"], n["pitch"]))
    for a, b in zip(src_s, back_s):
        assert a["pitch"] == b["pitch"], f"{label}: pitch {b['pitch']} != {a['pitch']}"
        assert abs(a["start_beats"] - b["start_beats"]) < 1e-4, (
            f"{label}: start {b['start_beats']} != {a['start_beats']}"
        )
        assert abs(a["duration_beats"] - b["duration_beats"]) < 0.26, (
            f"{label}: duration {b['duration_beats']} != {a['duration_beats']}"
        )
        assert a["velocity"] == b["velocity"], (
            f"{label}: velocity {b['velocity']} != {a['velocity']}"
        )


def _selftest_import_purity() -> None:
    """Importing pp_codec must not pull mcp, live_write, or core."""
    import ast
    import os
    import subprocess

    path = os.path.abspath(__file__)
    tree = ast.parse(open(path, encoding="utf-8").read())
    banned = {"mcp", "live_write", "core", "mcp_server", "envelope_bridge"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                assert root not in banned, f"forbidden import {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".", 1)[0]
                assert root not in banned, f"forbidden from-import {node.module}"
    probe = (
        "import sys, importlib\n"
        "sys.path.insert(0, %r)\n"
        "importlib.import_module('pp_codec')\n"
        "bad = [n for n in ('mcp','live_write','core') if n in sys.modules]\n"
        "raise SystemExit(0 if not bad else 'imported '+str(bad))\n"
    ) % (os.path.dirname(path),)
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert r.returncode == 0, f"import purity failed: {r.stdout}{r.stderr}"


def run_selftest() -> None:
    print("--- pp_codec selftest ---")
    _selftest_import_purity()
    print("  import purity OK")

    assert find_int("regularTrackCount:12", "regularTrackCount", 0) == 12
    assert find_int("regularTrackCount: 12", "regularTrackCount", 0) == 12
    assert find_str('{name:"Take 4"}', "name") == "Take 4"
    assert find_str("nope", "name", "x") == "x"

    selected_ctx = extract_selected_context(
        '{view:"session",selectedTrack:{id:"2",type:"midi",trackIndex:0},'
        'selectedScene:{id:"8",sceneIndex:3},selectedDevice:{id:"17",path:"t0/d0"}}'
    )
    assert selected_ctx["selected_track"]["track_index"] == 0
    assert selected_ctx["selected_scene"]["scene_index"] == 3
    assert selected_ctx["view"] == "session"

    overview = (
        'tracks:[{trackIndex:0,name:"Pad",type:"midi",'
        'instrument:{name:"Operator"}},{trackIndex:1,name:"Bass \\"X\\"",type:"midi"}]'
    )
    names = track_names_by_index(overview)
    assert names[0] == "Pad"
    assert names[1] == 'Bass "X"'
    idx, tid = track_index_by_name(
        'tracks:[{trackIndex:2,id:"abc",name:"SIDEKICK JAM",'
        'instrument:{name:"Operator"}}]',
        "SIDEKICK JAM",
    )
    assert idx == 2 and tid == "abc"
    assert regular_track_count(overview) == 2

    raw_clip = '{id:"2",type:"midi",notes:"v90 n/2 C2 1|1"}'
    assert extract_pp_notes(raw_clip) == "v90 n/2 C2 1|1"
    assert extract_pp_notes("") == ""

    listing = (
        'sessionClips:[{slot:"0/0",name:"A",device:{x:1}},'
        '{slot:"0/1",name:"B \\"q\\"",sceneIndex:1,trackIndex:0}]'
    )
    clips = parse_session_clips(listing)
    assert {"slot": "0/0", "name": "A"} in clips
    assert any(c["name"] == 'B "q"' for c in clips)

    pp2_empty = '{id:null,type:null,name:null,slot:"3/0"}'
    pp2_audio = (
        '{id:"5",type:"audio",name:"Take 4",view:"session",'
        'trackIndex:3,sceneIndex:0}'
    )
    pp2_midi = (
        '{id:"2",type:"midi",name:"Bass loop",view:"session",'
        'trackIndex:1,sceneIndex:0,'
        'notes:"v90 n/2 C2 1|1 v90 n/2 C2 1|3"}'
    )
    pp2_blank_midi = (
        '{id:"9",type:"midi",name:"Empty MIDI",view:"session",'
        'trackIndex:2,sceneIndex:0}'
    )
    pp2_unknown = '{id:"c3",name:"Something",slot:"4/0"}'
    midi_notes = pp_notes_to_notes('v90 n/2 C2 1|1 v90 n/2 C2 1|3')
    assert classify_session_clip(pp2_empty, []) == "empty"
    assert classify_session_clip(pp2_audio, []) == "unheard_audio"
    assert classify_session_clip(pp2_midi, midi_notes) == "heard"
    assert classify_session_clip(pp2_blank_midi, []) == "empty"
    assert classify_session_clip(pp2_unknown, []) == "unheard_unknown"
    assert classify_session_clip("", []) == "empty"
    assert classify_session_clip("no clip at 2/0", []) == "empty"
    assert classify_session_clip(
        "Producer Pal tool error: bridge returned no clip data before disconnect",
        [],
    ) == "invalid"
    assert is_empty_slot_error("no clip at trackIndex 3, sceneIndex 0") is True
    assert is_empty_slot_error(
        "Producer Pal tool error: no clip at trackIndex 3, sceneIndex 0."
    ) is True
    embedded = "timeout after no clip at trackIndex 0, sceneIndex 0"
    assert is_empty_slot_error(embedded) is False
    assert classify_session_clip(embedded, []) == "invalid"
    assert is_empty_slot_error("before no clip at 2/0 after") is False
    print("  wire parsers + clip classification OK")

    rt_notes = [
        {"pitch": 62, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 84},
        {"pitch": 69, "start_beats": 0.5, "duration_beats": 0.5, "velocity": 72},
        {"pitch": 65, "start_beats": 1.0, "duration_beats": 1.0, "velocity": 84},
        {"pitch": 60, "start_beats": 3.5, "duration_beats": 0.25, "velocity": 90},
        {"pitch": 61, "start_beats": 4.0, "duration_beats": 0.25, "velocity": 90},
    ]
    pp_rt, _ = notes_to_pp_string(note_dicts(rt_notes))
    _assert_notes_match(rt_notes, pp_notes_to_notes(pp_rt), "sample")

    sticky_pp = "v84 n/8 D4 1|1 v72 A4 1|1.5,4.5 v84 F4 1|2"
    sticky_expected = [
        {"pitch": 62, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 84},
        {"pitch": 69, "start_beats": 0.5, "duration_beats": 0.5, "velocity": 72},
        {"pitch": 65, "start_beats": 1.0, "duration_beats": 0.5, "velocity": 84},
        {"pitch": 69, "start_beats": 3.5, "duration_beats": 0.5, "velocity": 72},
    ]
    _assert_notes_match(sticky_expected, pp_notes_to_notes(sticky_pp), "sticky/comma")

    frac_pp = "v90 n0.9994/4 C4 1|2+n0.4997/4 n/8 E4 2|1-n0.25/4"
    frac_back = pp_notes_to_notes(frac_pp)
    assert len(frac_back) == 2
    assert abs(frac_back[0]["start_beats"] - 1.4997) < 1e-6
    assert abs(frac_back[0]["duration_beats"] - 0.9994) < 1e-6
    assert abs(frac_back[1]["start_beats"] - 3.75) < 1e-6

    dup = note_dicts([
        {"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 80},
        {"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 80},
        {"pitch": 60, "start": 0.5, "duration": 1.0, "velocity": 90},
    ])
    norm = normalize_notes_for_pp(dup)
    assert len(norm) == 2
    assert abs(norm[0]["duration"] - 0.5) < 1e-9
    assert pp_normalized_note_count(dup) == 2

    assert duration_to_pp(1.0) == "n/4"
    assert duration_to_pp(2.25) == "n9/16"
    assert abs(pp_duration_to_beats("n/8") - 0.5) < 1e-9
    assert abs(bar_beat_to_beats("2|1") - 4.0) < 1e-9
    print("  note wire parse/serialize/round-trip OK")

    # Producer Pal 2.1/2.2 midi-json contract: exact beat durations and C1..B8
    # MIDI range must survive the purpose-built short-key representation.
    midi_json_notes = []
    durations = (0.25, 0.5, 1.0, 2.25, 2.5, 3.0, 3.75, 4.0)
    for i, pitch in enumerate((24, 119)):
        for j, duration in enumerate(durations):
            midi_json_notes.append({
                "pitch": pitch,
                "start_beats": float(i * 40 + j * 5),
                "duration_beats": duration,
                "velocity": 64 + j,
            })
    midi_payload = notes_to_midi_json_payload(midi_json_notes)
    assert all(set(item) == {"p", "t", "d", "v"} for item in midi_payload)
    midi_back = midi_json_to_notes(midi_payload)
    assert len(midi_back) == len(midi_json_notes)
    for expected, actual in zip(midi_json_notes, midi_back):
        assert expected["pitch"] == actual["pitch"]
        assert abs(expected["start_beats"] - actual["start_beats"]) <= 1e-6
        assert abs(expected["duration_beats"] - actual["duration_beats"]) <= 1e-6
        assert expected["velocity"] == actual["velocity"]
    assert midi_json_to_notes('{"version":"1","notes":[]}') == []
    for bad in ("[not-json", [{"pitch": 60}], {"version": "9", "notes": []}):
        try:
            midi_json_to_notes(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("malformed/wrong-key/unknown-version payload accepted")
    assert json.loads(encode_notes(
        midi_json_notes[:1], CodecMode.MIDI_JSON)) == midi_payload[:1]
    assert isinstance(encode_notes(midi_json_notes[:1], CodecMode.BARBEAT_TOKENS), str)
    print("  midi-json exact round-trip + negative cases OK")
    print("pp_codec selftest OK")


if __name__ == "__main__":
    run_selftest()
