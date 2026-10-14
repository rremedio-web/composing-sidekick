"""Shared utilities and constants for mcp_server/core.py.

This module holds module-level admin/state and universally-shared helpers that are
imported by core.py. All moved names remain accessible as core._name from the
public API (server.py, selftest, etc.).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

# ONE module universe regardless of import style ('core_shared' vs
# 'mcp_server.core_shared'): caches and test overrides must never fork across
# two instances (a bare-import set_ratings_path once missed the instance core
# uses and leaked a synthetic rating into the real ratings.json).
for _alias in ("core_shared", "mcp_server.core_shared"):
    sys.modules.setdefault(_alias, sys.modules[__name__])

# Path constants and ROOT/sys.path setup
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "analyze"))

STORE = os.path.join(ROOT, "store")
STYLE_MD = os.path.join(STORE, "STYLE.md")
STYLE_JSON = os.path.join(STORE, "style_profile.json")
HARMONY_JSON = os.path.join(STORE, "harmony.json")
NOTES_MD = os.path.join(STORE, "NOTES.md")
STRUCTURE_JSON = os.path.join(STORE, "structure.json")
INDEX_JSON = os.path.join(STORE, "phrase_index.json")
RATINGS_JSON = os.path.join(STORE, "ratings.json")
MOTIFS_JSON = os.path.join(STORE, "motifs.json")
TASTE_MODEL_JSON = os.path.join(STORE, "taste_model.json")
EXPORTS_DIR = os.path.join(STORE, "exports")

# Module-level cache (safe: mutated in place, never rebound)
_CACHE: dict[str, Any] = {}

# Read-only constants (imported from config.py, not defined here)
# SECTION_BUCKETS, BEATS_PER_BAR, _ROLE_ENERGY_THRESHOLD, _ROLE_LAYER_ORDER,
# _DROP_ENERGY, _DENSITY_PARAMS, _DRUM_DENSITY_TARGETS are imported in core.py

# Artifact schema version policy (point #6):
# Store artifacts (style_profile.json, phrase_index.json, harmony.json, structure.json, motifs.json)
# carry a top-level "meta" dict: {"schema_version": 1, "built_at": iso-utc, "code_rev": sha, "corpus": {"n_projects": N}}.
# When loading:
# - If meta.schema_version mismatches _EXPECTED_ARTIFACT_SCHEMA_VERSION, raise ValueError with a rebuild instruction.
# - If meta is missing:
#   - In legacy compatibility mode (_ALLOW_LEGACY_ARTIFACTS_WITHOUT_META=True), emit a warning and proceed.
#   - In strict mode (_ALLOW_LEGACY_ARTIFACTS_WITHOUT_META=False), raise ValueError with a rebuild instruction.
_EXPECTED_ARTIFACT_SCHEMA_VERSION = 1
_ALLOW_LEGACY_ARTIFACTS_WITHOUT_META = False

def _load_json(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def _full_fsync(fd: int) -> None:
    """Flush a file fd toward stable storage.

    macOS fsync only pushes to the drive cache, so on darwin use
    F_FULLFSYNC (which pushes through it); any OSError falls back to a
    plain fsync. Directory fds keep the plain best-effort fsync in
    _fsync_dir — F_FULLFSYNC is for file data, and some filesystems
    refuse even fsync on directories.
    """
    if (sys.platform == "darwin" and fcntl is not None
            and hasattr(fcntl, "F_FULLFSYNC")):
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass
    os.fsync(fd)


def _fsync_dir(directory: str) -> None:
    """Fsync a directory so a rename into it is durable.

    Fully best-effort: once os.replace has succeeded the new content is on
    disk, so nothing here (open, fsync, close) may propagate and turn a
    committed save into a reported failure.
    """
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass


def _save_json_atomic(path: str, data: dict, indent: int | None = 2) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(fd, "w") as fh:
            if indent is not None:
                json.dump(data, fh, indent=indent)
            else:
                json.dump(data, fh)
            fh.write("\n")
            fh.flush()
            _full_fsync(fh.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(directory)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


_BACKUP_STAMP_RE = re.compile(r"\.(\d{8})T\d+Z\.json$")


def _backup_utc_day(name: str, backup_dir: str) -> datetime.date | None:
    """UTC day a backup belongs to: parsed from its stamp, else file mtime."""
    match = _BACKUP_STAMP_RE.search(name)
    if match:
        try:
            return datetime.datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            pass
    try:
        stamp = os.stat(os.path.join(backup_dir, name)).st_mtime
        return datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).date()
    except OSError:
        return None


def _prune_rolling_backups(backup_dir: str, keep: int) -> None:
    """Prune to the newest `keep` backups plus protected daily firsts.

    The first backup of each of the last 14 UTC days is never pruned: one
    judged answer triggers several ledger saves, so count-only retention
    rotates the pre-session state out within a single session.
    """
    try:
        names = sorted(os.listdir(backup_dir))
    except OSError:
        return
    keep_set = set(names[-keep:]) if keep > 0 else set()
    today = datetime.datetime.now(datetime.timezone.utc).date()
    recent = {today - datetime.timedelta(days=offset) for offset in range(14)}
    seen_days: set[datetime.date] = set()
    for name in names:  # oldest first: the first hit per day is its first backup
        day = _backup_utc_day(name, backup_dir)
        if day is not None and day in recent and day not in seen_days:
            seen_days.add(day)
            keep_set.add(name)
    for stale in names:
        if stale not in keep_set:
            try:
                os.unlink(os.path.join(backup_dir, stale))
            except OSError:
                pass


def _rolling_backup(path: str, keep: int = 20) -> str | None:
    """Copy path to <dir>/backups/<basename>/<basename>.<UTC stamp>.json.

    The backup keeps the source file's permission bits (the judgment stores
    are 0600). A save whose bytes equal the newest existing backup writes
    nothing — unless that backup is from an earlier UTC day, so the first
    save of each UTC day always writes that day's protected first backup
    (one judged answer triggers ~7 ledger saves). Retention keeps
    the newest `keep` backups plus the first backup of each of the last 14
    UTC days. Best-effort: any OSError returns None so a blocked backup
    directory never blocks the save itself.
    """
    try:
        if not os.path.exists(path):
            return None
        with open(path, "rb") as fh:
            content = fh.read()
        basename = os.path.basename(path)
        backup_dir = os.path.join(
            os.path.dirname(os.path.abspath(path)), "backups", basename)
        os.makedirs(backup_dir, exist_ok=True)
        existing = sorted(os.listdir(backup_dir))
        today = datetime.datetime.now(datetime.timezone.utc).date()
        if existing:
            try:
                with open(os.path.join(backup_dir, existing[-1]), "rb") as fh:
                    identical = (fh.read() == content)
                if identical and _backup_utc_day(existing[-1], backup_dir) == today:
                    return os.path.join(backup_dir, existing[-1])
            except OSError:
                pass
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = os.path.join(backup_dir, "%s.%s.json" % (basename, stamp))
        try:
            src_mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            src_mode = 0o600
        fd = os.open(backup_path,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, src_mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        try:
            # Best-effort only: os.open already applied src_mode, this just
            # cancels whatever the umask stripped from it.
            os.chmod(backup_path, src_mode)
        except OSError:
            pass
        _prune_rolling_backups(backup_dir, keep)
        return backup_path
    except OSError:
        return None


def _clip_digest(notes: list[dict]) -> str:
    """SHA1 hash of normalized note list, truncated to 10 chars."""
    payload = json.dumps(_note_dicts(notes), sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]


def _write_text_atomic(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            _full_fsync(fh.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(directory)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _validate_artifact_meta(path: str, data: dict, artifact_name: str | None = None) -> None:
    name = artifact_name or os.path.basename(path)
    if not isinstance(data, dict):
        raise ValueError(
            f"store artifact {name} ({path}) has an invalid shape — rebuild via analyze pipeline"
        )
    meta = data.get("meta")
    if meta is None:
        if not _ALLOW_LEGACY_ARTIFACTS_WITHOUT_META:
            raise ValueError(
                f"store artifact {name} ({path}) lacks required 'meta' block — rebuild via analyze pipeline"
            )
        import warnings

        warnings.warn(
            f"store artifact {name} lacks 'meta' block (legacy artifact) — run analyze pipeline to upgrade to schema_version {_EXPECTED_ARTIFACT_SCHEMA_VERSION}",
            UserWarning,
            stacklevel=3,
        )
        return
    if not isinstance(meta, dict) or "schema_version" not in meta:
        raise ValueError(
            f"store artifact {name} ({path}) has malformed 'meta' block (missing 'schema_version') — rebuild via analyze pipeline"
        )
    version = meta.get("schema_version")
    if version != _EXPECTED_ARTIFACT_SCHEMA_VERSION:
        raise ValueError(
            f"store artifact {name} ({path}) schema_version {version!r} does not match expected {_EXPECTED_ARTIFACT_SCHEMA_VERSION} — rebuild via analyze pipeline"
        )


def _load_artifact(path: str, name: str | None = None) -> dict:
    data = _load_json(path)
    _validate_artifact_meta(path, data, name)
    return data


@contextmanager
def _ratings_file_lock():
    path = f"{_ratings_path()}.lock"
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "a") as fh:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


_RATINGS_PATH_OVERRIDE: str | None = None


def _ratings_path() -> str:
    return _RATINGS_PATH_OVERRIDE or RATINGS_JSON


def set_ratings_path(path: str | None) -> None:
    """Override ratings JSON path (selftest only). Pass None to reset."""
    global _RATINGS_PATH_OVERRIDE
    _RATINGS_PATH_OVERRIDE = path


_TASTE_MODEL_PATH_OVERRIDE: str | None = None


def _taste_model_path() -> str:
    """Selftest override first; else sibling of an overridden ratings file (so
    set_ratings_path isolates it in tests too); else the store default."""
    if _TASTE_MODEL_PATH_OVERRIDE:
        return _TASTE_MODEL_PATH_OVERRIDE
    ratings_path = os.path.abspath(_ratings_path())
    if _RATINGS_PATH_OVERRIDE:
        return os.path.splitext(ratings_path)[0] + ".taste_model.json"
    return os.path.join(os.path.dirname(ratings_path) or ".", "taste_model.json")


def set_taste_model_path(path: str | None) -> None:
    """Override taste model JSON path (selftest only). Pass None to reset."""
    global _TASTE_MODEL_PATH_OVERRIDE
    _TASTE_MODEL_PATH_OVERRIDE = path


def _load_ratings_file() -> dict:
    path = _ratings_path()
    if not os.path.exists(path):
        return {"ratings": []}
    with open(path) as fh:
        data = json.load(fh)
    if "ratings" not in data:
        data["ratings"] = []
    return data


def _save_ratings_file(data: dict) -> None:
    with _ratings_file_lock():
        _save_ratings_file_unlocked(data)


def _save_ratings_file_unlocked(data: dict) -> None:
    path = _ratings_path()
    _rolling_backup(path)
    _save_json_atomic(path, data)


def _read_style_md() -> str:
    with open(STYLE_MD) as fh:
        return fh.read()


def _aggregate() -> dict:
    if "aggregate" not in _CACHE:
        _CACHE["aggregate"] = _load_artifact(STYLE_JSON, "style_profile.json")["aggregate"]
    return _CACHE["aggregate"]


def _motifs_data() -> dict:
    """Gesture vocabulary from store/motifs.json (empty dict if not built yet)."""
    if "motifs" not in _CACHE:
        if os.path.exists(MOTIFS_JSON):
            _CACHE["motifs"] = _load_artifact(MOTIFS_JSON, "motifs.json")
        else:
            _CACHE["motifs"] = {}
    return _CACHE["motifs"]


def _canonical_move(label: str | None) -> str | None:
    """Canonical reject-pairing move: develop labels carry dispatch suffixes
    (slow-burn pass, motivic walk seed) that name the same hearing, so the
    pairing strips them — (source, 'slow-burn:reveal') bans no pass in
    particular and every pass of that pairing in general."""
    if label is None:
        return None
    text = str(label)
    text = re.sub(r":pass\d+$", "", text)
    text = re.sub(r":walk\d+$", "", text)
    return text or None


def _note_dicts(notes: list[dict]) -> list[dict]:
    """Normalise MCP note shape → build_index shape."""
    out = []
    for n in notes:
        out.append(
            {
                "pitch": int(n["pitch"]),
                "start": float(n.get("start_beats", n.get("start", 0))),
                "duration": float(n.get("duration_beats", n.get("duration", 0.25))),
                "velocity": int(n.get("velocity", 80)),
            }
        )
    return out


def _groove_grid(notes: list[dict]) -> list[float]:
    g = [0.0] * 16
    for n in notes:
        g[int(round((n["start"] % 4.0) * 4)) % 16] += 1.0
    total = sum(g) or 1.0
    return [x / total for x in g]


def _l2_cosine(a: list[float], b: list[float]) -> float:
    """True cosine similarity. (Both groove grids are sum-to-1 probability vectors;
    a plain dot product of those is ~0.1 even for matching shapes — must L2-normalise.)"""
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def _interval_buckets(notes: list[dict]) -> list[float]:
    top: dict[int, dict] = {}
    for n in notes:
        slot = round(n["start"] * 4)
        if slot not in top or n["pitch"] > top[slot]["pitch"]:
            top[slot] = n
    seq = [top[k]["pitch"] for k in sorted(top)]
    iv = [0.0] * 7
    for a, b in zip(seq, seq[1:]):
        d = b - a
        if d <= -5:
            iv[0] += 1
        elif d <= -3:
            iv[1] += 1
        elif d <= -1:
            iv[2] += 1
        elif d == 0:
            iv[3] += 1
        elif d <= 2:
            iv[4] += 1
        elif d <= 4:
            iv[5] += 1
        else:
            iv[6] += 1
    total = sum(iv) or 1.0
    return [x / total for x in iv]
