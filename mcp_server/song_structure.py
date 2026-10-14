"""Read-only scene-to-part resolution for safe song arrangement."""
from __future__ import annotations

import re
from typing import Any, Callable

from mcp_server import pp_session


ARRANGEMENT_ROLES = frozenset(("lead", "bass", "pad", "chords", "drums"))
ROLE_ALIASES = {
    "lead": frozenset(("lead", "melody", "melodic")),
    "bass": frozenset(("bass", "sub")),
    "pad": frozenset(("pad", "atmosphere", "atmos")),
    "chords": frozenset(("chord", "chords", "keys", "harmony")),
    "drums": frozenset(("drum", "drums", "perc", "percussion", "kit")),
}
_SOURCE_PATH = re.compile(r"^t(?P<track>\d+)/s(?P<scene>\d+)$")


class SongStructureError(RuntimeError):
    pass


def _role_for_track(name: str) -> tuple[str | None, str | None]:
    tokens = set(re.findall(r"[a-z0-9]+", str(name).lower()))
    matches = [role for role, aliases in ROLE_ALIASES.items() if tokens & aliases]
    if len(matches) == 1:
        return matches[0], None
    return None, "unknown" if not matches else "ambiguous"


def validate_parts(parts: Any) -> list[dict[str, str]]:
    if not isinstance(parts, list) or not parts:
        raise ValueError("parts must be a non-empty list")
    clean = []
    seen_ids = set()
    for index, part in enumerate(parts):
        if not isinstance(part, dict) or set(part) != {"part_id", "role", "source_path"}:
            raise ValueError("each part must contain exactly part_id, role, and source_path")
        part_id = part["part_id"]
        role = part["role"]
        path = part["source_path"]
        if (not isinstance(part_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", part_id)
                or part_id in seen_ids):
            raise ValueError("part_id values must be unique path-free identifiers")
        if role == "drum":
            role = "drums"
        if role not in ARRANGEMENT_ROLES:
            raise ValueError("part role must be lead, bass, pad, chords, or drums")
        if not isinstance(path, str) or _SOURCE_PATH.fullmatch(path) is None:
            raise ValueError("source_path must use t<track>/s<scene>")
        seen_ids.add(part_id)
        clean.append({"part_id": part_id, "role": role, "source_path": path})
    return clean


async def discover_scene_parts(scene_index: int, operation_session: Callable) -> dict[str, Any]:
    if isinstance(scene_index, bool) or not isinstance(scene_index, int) or scene_index < 0:
        raise ValueError("scene_index must be a non-negative integer")
    async with operation_session() as session:
        overview = await session.call("ppal-read-live-set", {"include": ["tracks"]})
        result = pp_session._result(overview)
        if not isinstance(result, dict) or not isinstance(result.get("tracks"), list):
            raise SongStructureError("Live track inventory is unavailable")
        scene_count = pp_session.find_int(overview, "sceneCount")
        tempo = pp_session._first_key(result, ("tempo",))
        if scene_count is None or scene_index >= scene_count:
            raise SongStructureError("scene_index is outside the Live Set")
        if isinstance(tempo, bool) or not isinstance(tempo, (int, float)) or tempo <= 0:
            raise SongStructureError("Live tempo is unavailable")
        parts = []
        unresolved = []
        for track in result["tracks"]:
            if not isinstance(track, dict):
                continue
            track_index = pp_session.track_index_of(track)   # 2.2 trackIndex, 2.3 't3'
            track_name = track.get("name")
            if track_index is None or not isinstance(track_name, str):
                raise SongStructureError("Live track inventory is malformed")
            listing = await session.call(
                "ppal-read-track", pp_session.requests_for(session).read_track(
                    track_index, include=["session-clips"]))
            clips = pp_session._collection(
                pp_session._result(listing),
                ("sessionClips", "session-clips", "session_clips", "clips"))
            selected = []
            for clip in clips:
                if not isinstance(clip, dict):
                    continue
                path = clip.get("path")
                slot = clip.get("slot")
                if slot is None and isinstance(path, str):
                    match = _SOURCE_PATH.fullmatch(path)
                    slot = "%s/%s" % (match.group("track"), match.group("scene")) if match else None
                if slot == "%d/%d" % (track_index, scene_index):
                    selected.append(clip)
            if len(selected) > 1:
                raise SongStructureError("scene inventory contains duplicate clips for one slot")
            if not selected:
                continue
            role, issue = _role_for_track(track_name)
            source_path = "t%d/s%d" % (track_index, scene_index)
            if issue:
                unresolved.append({"track_name": track_name, "source_path": source_path,
                                   "reason": issue})
                continue
            parts.append({"part_id": "track-%d" % track_index,
                          "role": role, "source_path": source_path})
        return {"parts": parts, "unresolved": unresolved, "tempo": float(tempo)}


def part_placements(parts: list[dict], sections: list[dict]) -> list[dict]:
    """Adapt the existing role policy to distinct part IDs sharing a role."""
    from mcp_server import core

    if not parts:
        return []
    roles = {part["role"] for part in parts}
    operations = []
    for section in sections:
        active = set(core._active_roles_for_section(section, roles))
        for part in parts:
            if part["role"] in active:
                operations.append({
                    "part_id": part["part_id"], "role": part["role"],
                    "source_path": part["source_path"],
                    "bar_start": section["bar_start"], "bars": section["bars"],
                    "density": section["density"], "section": section["name"],
                })
    return operations
