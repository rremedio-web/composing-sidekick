"""Strict loader for portable approved-rhythm metadata and assets."""

from __future__ import annotations

import math
import os
import stat
from typing import Any

from mcp_server.core_shared import ROOT, STORE, _CACHE, _load_json

APPROVED_RHYTHMS_JSON = os.path.join(STORE, "approved_rhythms.json")
APPROVED_RHYTHMS_ASSETS_DIR = os.path.join(ROOT, "assets", "approved_rhythms")

_JSON_PATH_OVERRIDE: str | None = None
_REPO_ROOT_OVERRIDE: str | None = None

_BPM_MIN = 20
_BPM_MAX = 300

_REQUIRED_PATTERN_FIELDS = ("id", "title", "description", "midi_asset", "bpm_spec")
_REQUIRED_BPM_FIELDS = ("ideal_bpm", "bpm_range", "feel")


def set_loader_overrides(*, json_path: str | None = None, repo_root: str | None = None) -> None:
    global _JSON_PATH_OVERRIDE, _REPO_ROOT_OVERRIDE
    _JSON_PATH_OVERRIDE = json_path
    _REPO_ROOT_OVERRIDE = repo_root
    _CACHE.pop("approved_rhythms", None)


def reset_loader_overrides() -> None:
    set_loader_overrides(json_path=None, repo_root=None)


def _repo_root() -> str:
    return _REPO_ROOT_OVERRIDE or ROOT


def _json_path() -> str:
    return _JSON_PATH_OVERRIDE or APPROVED_RHYTHMS_JSON


def _require_non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"approved rhythm {field} must be a non-empty string")
    return value


def _require_finite_bpm(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"approved rhythm {field} must be a finite numeric BPM value")
    num = float(value)
    if not math.isfinite(num):
        raise ValueError(f"approved rhythm {field} must be a finite numeric BPM value")
    if not _BPM_MIN <= num <= _BPM_MAX:
        raise ValueError(
            f"approved rhythm {field} must be between {_BPM_MIN} and {_BPM_MAX}"
        )
    return num


def _parse_bpm_range_token(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"approved rhythm {field} must be a finite numeric BPM value")
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        token = value.strip()
        if not token:
            raise ValueError(f"approved rhythm {field} must be a finite numeric BPM value")
        try:
            num = float(token)
        except ValueError as exc:
            raise ValueError(
                f"approved rhythm {field} must be a finite numeric BPM value"
            ) from exc
    else:
        raise ValueError(f"approved rhythm {field} must be a finite numeric BPM value")
    if not math.isfinite(num):
        raise ValueError(f"approved rhythm {field} must be a finite numeric BPM value")
    if not _BPM_MIN <= num <= _BPM_MAX:
        raise ValueError(
            f"approved rhythm {field} must be between {_BPM_MIN} and {_BPM_MAX}"
        )
    return num


def _parse_bpm_range(value: Any, ideal_bpm: float) -> str:
    bpm_range = _require_non_empty_str(value, "bpm_spec.bpm_range")
    parts = bpm_range.split("-")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ValueError("approved rhythm bpm_spec.bpm_range must be 'low-high'")
    low = _parse_bpm_range_token(parts[0].strip(), "bpm_range low")
    high = _parse_bpm_range_token(parts[1].strip(), "bpm_range high")
    if low > high:
        raise ValueError("approved rhythm bpm_spec.bpm_range low must be <= high")
    if not low <= ideal_bpm <= high:
        raise ValueError("approved rhythm ideal_bpm must fall within bpm_range")
    return bpm_range


def _resolve_asset_path(repo_root: str, midi_asset: str) -> str:
    if os.path.isabs(midi_asset):
        raise ValueError(f"approved rhythm midi_asset must be repo-relative, not absolute: {midi_asset!r}")
    if ".." in midi_asset.replace("\\", "/").split("/"):
        raise ValueError(f"approved rhythm midi_asset must not traverse outside repo: {midi_asset!r}")
    norm = os.path.normpath(midi_asset)
    abs_path = os.path.abspath(os.path.join(repo_root, norm))
    real_path = os.path.realpath(abs_path)
    repo_real = os.path.realpath(repo_root)
    if not real_path.startswith(repo_real + os.sep) and real_path != repo_real:
        raise ValueError(f"approved rhythm midi_asset resolves outside repo: {midi_asset!r}")
    try:
        st = os.stat(real_path)
    except OSError as exc:
        raise ValueError(f"approved rhythm asset missing on disk: {midi_asset!r}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"approved rhythm asset must be a regular file: {midi_asset!r}")
    return real_path


def _compact_pattern(raw: dict[str, Any]) -> dict[str, Any]:
    missing = [field for field in _REQUIRED_PATTERN_FIELDS if field not in raw]
    if missing:
        raise ValueError(f"approved rhythm entry missing required field(s): {', '.join(missing)}")
    bpm_spec = raw["bpm_spec"]
    if not isinstance(bpm_spec, dict):
        raise ValueError("approved rhythm bpm_spec must be an object")
    missing_bpm = [field for field in _REQUIRED_BPM_FIELDS if field not in bpm_spec]
    if missing_bpm:
        raise ValueError(f"approved rhythm bpm_spec missing field(s): {', '.join(missing_bpm)}")
    ideal_bpm = _require_finite_bpm(bpm_spec["ideal_bpm"], "bpm_spec.ideal_bpm")
    return {
        "id": _require_non_empty_str(raw["id"], "id"),
        "title": _require_non_empty_str(raw["title"], "title"),
        "description": _require_non_empty_str(raw["description"], "description"),
        "midi_asset": _require_non_empty_str(raw["midi_asset"], "midi_asset"),
        "bpm_spec": {
            "ideal_bpm": int(ideal_bpm) if ideal_bpm.is_integer() else ideal_bpm,
            "bpm_range": _parse_bpm_range(bpm_spec["bpm_range"], ideal_bpm),
            "feel": _require_non_empty_str(bpm_spec["feel"], "bpm_spec.feel"),
        },
    }


def load_approved_rhythms(
    *,
    json_path: str | None = None,
    repo_root: str | None = None,
) -> list[dict[str, Any]]:
    """Load validated, compact approved-rhythm metadata for model-facing surfaces.

    The library is the owner's and is not shipped: with no explicit path and no
    ``store/approved_rhythms.json`` the library is empty. A file that exists is
    validated strictly."""
    path = json_path or _json_path()
    root = repo_root or _repo_root()
    if json_path is None and _JSON_PATH_OVERRIDE is None and not os.path.exists(path):
        return []
    data = _load_json(path)
    patterns = data.get("patterns")
    if not isinstance(patterns, list) or not patterns:
        raise ValueError("approved_rhythms.json must contain a non-empty patterns list")

    seen_ids: set[str] = set()
    compact: list[dict[str, Any]] = []
    for raw in patterns:
        if not isinstance(raw, dict):
            raise ValueError("approved rhythm pattern entries must be objects")
        pat = _compact_pattern(raw)
        if pat["id"] in seen_ids:
            raise ValueError(f"duplicate approved rhythm id: {pat['id']!r}")
        seen_ids.add(pat["id"])
        _resolve_asset_path(root, pat["midi_asset"])
        compact.append(pat)
    return compact


def approved_rhythms_data() -> list[dict[str, Any]]:
    if "approved_rhythms" not in _CACHE:
        _CACHE["approved_rhythms"] = load_approved_rhythms()
    return _CACHE["approved_rhythms"]
