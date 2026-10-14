"""Observed arrangement layer structure for materialized projects and snapshots."""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import fcntl


# Habit thresholds are intentionally small and fixed so identical arrangements
# always produce the same observations: a thinning lasts at least two bars,
# and an ending strip lasts at least two bars.
_MIN_THIN_BARS = 2
_MAX_THIN_BARS = 16
_MIN_STRIP_BARS = 2
_MAX_STRIP_BARS = 32
_BORROW_VALUES = {"pacing", "layering", "peak_placement", "ending", "density"}
_PRESERVE_VALUES = {"harmony", "motif", "sound", "groove"}
_DEPART_VALUES = _BORROW_VALUES
_DECISION_FIELDS = (
    "goal", "borrow", "preserve", "depart", "protect_track_ids",
    "ref_id", "proposal_id", "verdict", "reason",
)
# Direction fields follow the PIECE across its owner-confirmed versions. Track
# ids, proposal ids, verdicts and their reasons are facts about one Set and
# never cross to another (a stale clip id must not resolve under a new Set).
_INHERITED_FIELDS = ("goal", "borrow", "preserve", "depart", "ref_id")
_DECISION_LOCKS_GUARD = threading.Lock()
_DECISION_LOCKS = {}


def _decision_path(path=None):
    if path is not None:
        return os.fspath(path)
    return os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "store", "piece_decisions.json")


def _pieces_path(path=None):
    """store/pieces.json — a sibling of the decisions file, so a test that
    redirects ``_decision_path`` to a temp dir redirects the pieces too."""
    if path is not None:
        return os.fspath(path)
    return os.path.join(os.path.dirname(os.path.abspath(_decision_path())), "pieces.json")


def _beside(store_file, name, explicit=None):
    """``explicit`` when given, else ``name`` in the directory of ``store_file``."""
    if explicit is not None:
        return os.fspath(explicit)
    return os.path.join(os.path.dirname(os.path.abspath(store_file)), name)


def _records_path(path=None):
    """store/piece_records.json — statements, proposals, reactions and
    observations; a sibling of the decisions file like ``_pieces_path``."""
    if path is not None:
        return os.fspath(path)
    return os.path.join(os.path.dirname(os.path.abspath(_decision_path())),
                        "piece_records.json")


def _decision_lock(path):
    with _DECISION_LOCKS_GUARD:
        return _DECISION_LOCKS.setdefault(os.path.abspath(path), threading.RLock())


@contextmanager
def _decision_file_lock(path):
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    with open(lock_path, "a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _save_decisions_atomic(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".",
                                     suffix=".tmp", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _load_store(path, list_key):
    """Read one revisioned store file (``{"revision": n, <list_key>: [...]}``);
    a missing file is an empty store."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"revision": 0, list_key: []}
    data.setdefault(list_key, [])
    return data


def _locked_update(path, list_key, mutate):
    """Load → ``mutate(data, next_revision)`` → atomic save, under the
    process and file locks ``record_decision`` uses. ``mutate`` returns
    ``(result, changed)``; with ``changed`` false the file is left untouched
    (idempotent re-declarations) and ``result`` is still handed back."""
    with _decision_lock(path):
        with _decision_file_lock(path):
            data = _load_store(path, list_key)
            revision = int(data.get("revision", 0)) + 1
            result, changed = mutate(data, revision)
            if changed:
                data["revision"] = revision
                _save_decisions_atomic(path, data)
    return result


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _piece_slug(name):
    slug = "".join(ch if ch.isalnum() else "-" for ch in str(name).strip().casefold())
    slug = "-".join(part for part in slug.split("-") if part)
    if not slug:
        raise ValueError("piece needs a name")
    return slug


def load_pieces(*, path=None):
    """The pieces store: ``{"revision", "pieces": [{piece_id, name, notes_path,
    set_keys: [{set_key, confirmed_by: "owner", ts, via}]}]}``."""
    return _load_store(_pieces_path(path), "pieces")


def _find_piece(data, piece):
    """A piece by id, by exact slug of the given name, or by name (case-insensitive)."""
    wanted = str(piece).strip()
    slug = _piece_slug(wanted)
    for record in data.get("pieces", []):
        if record.get("piece_id") == wanted or record.get("piece_id") == slug:
            return record
    for record in data.get("pieces", []):
        if str(record.get("name", "")).casefold() == wanted.casefold():
            return record
    return None


def _piece_view(record, set_key=None):
    versions = [row["set_key"] for row in record.get("set_keys", [])]
    view = {"piece_id": record["piece_id"], "name": record.get("name"),
            "notes_path": record.get("notes_path"), "confirmed_versions": versions}
    if set_key is not None:
        view["set_key"] = os.fspath(set_key)
        view["siblings"] = [key for key in versions if key != os.fspath(set_key)]
    return view


def _holder_of(data, key):
    for record in data.get("pieces", []):
        if any(row.get("set_key") == key for row in record.get("set_keys", [])):
            return record
    return None


def resolve_piece(set_key, *, path=None):
    """The piece this exact Set key was confirmed into, or None. Exact match
    only (``os.fspath`` equality, like ``current_decisions``): a similar
    filename, a different case or a copy never resolves."""
    key = os.fspath(set_key)
    record = _holder_of(load_pieces(path=path), key)
    return _piece_view(record, key) if record else None


def declare_piece_version(piece, set_key, *, confirm=False, name=None, notes_path=None,
                          via="tool", path=None):
    """Bind one Set (its exact key) to a piece on the OWNER's confirmation.

    Preview (``confirm=False``) writes nothing. ``confirm=True`` appends the
    version as ``confirmed_by: "owner"``; the same key again is
    ``already_confirmed`` (no write); a key already held by another piece
    is refused — a Set belongs to exactly one piece. A new piece is created
    from ``piece`` (``name`` / ``notes_path`` optional) on first confirm.
    """
    key = os.fspath(set_key)
    if not key:
        raise ValueError("set_key must name the Set file")
    if via not in ("tool", "cli"):
        raise ValueError("via must be tool or cli")
    target = _pieces_path(path)

    def plan(data):
        record = _find_piece(data, piece)
        holder = _holder_of(data, key)
        if holder is not None and (record is None or holder["piece_id"] != record["piece_id"]):
            raise ValueError("%s is already a confirmed version of piece %r — a Set belongs "
                             "to exactly one piece" % (key, holder["piece_id"]))
        return record, holder

    if not confirm:
        record, holder = plan(load_pieces(path=target))
        return {"status": "preview", "piece_id": record["piece_id"] if record else _piece_slug(piece),
                "name": (record or {}).get("name") or (name or str(piece).strip()),
                "set_key": key, "already_confirmed": holder is not None,
                "new_piece": record is None, "writes": 0,
                "note": "confirm=True records the owner's confirmation; nothing written"}

    def mutate(data, revision):
        record, holder = plan(data)
        if record is None:
            record = {"piece_id": _piece_slug(piece), "name": name or str(piece).strip(),
                      "notes_path": notes_path, "set_keys": []}
            data["pieces"].append(record)
        elif notes_path and not record.get("notes_path"):
            record["notes_path"] = notes_path
        if holder is not None:
            return dict(_piece_view(record, key), status="already_confirmed", writes=0), False
        record["set_keys"].append({"set_key": key, "confirmed_by": "owner",
                                   "ts": _now_iso(), "via": via, "revision": revision})
        return dict(_piece_view(record, key), status="confirmed", writes=1), True

    return _locked_update(target, "pieces", mutate)


def record_decision(set_key, decision, *, path=None):
    """Append one owner- or model-sourced structured decision atomically."""
    if not isinstance(decision, dict):
        raise TypeError("decision must be a mapping")
    if decision.get("kind") not in {
            "intent", "preserve", "depart", "protect", "arrangement_feedback"}:
        raise ValueError("unsupported decision kind")
    if decision.get("source") not in {"owner", "model", "model_on_request"}:
        raise ValueError("decision source must be owner, model, or model_on_request")
    for field, allowed in (("borrow", _BORROW_VALUES),
                           ("preserve", _PRESERVE_VALUES),
                           ("depart", _DEPART_VALUES)):
        if field in decision:
            values = decision[field]
            if not isinstance(values, (list, tuple)) or any(
                    not isinstance(value, str) or value not in allowed for value in values):
                raise ValueError("unknown %s value" % field)
    record = dict(decision)
    record["set_key"] = os.fspath(set_key)
    record["ts"] = _now_iso()

    def mutate(data, revision):
        record["revision"] = revision
        data["decisions"].append(record)
        return dict(record), True

    return _locked_update(_decision_path(path), "decisions", mutate)


def current_decisions(set_key, *, path=None, pieces_path=None):
    """Resolve current fields for a Set; owner records outrank model records.

    When the owner has confirmed this Set as a version of a piece
    (:func:`declare_piece_version`), the direction fields
    (``_INHERITED_FIELDS``) also resolve from the piece's other confirmed
    versions: an owner record anywhere outranks a model record anywhere,
    then this Set's own records outrank inherited ones, then the latest
    revision wins. ``inherited`` names the version each borrowed field came
    from. Set-bound fields (track ids, proposal ids, verdicts, reasons) and
    ``revision`` never cross Sets.
    """
    key = os.fspath(set_key)
    target = _decision_path(path)
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        data = {"decisions": []}
    # The pieces store lives beside the decisions file IN USE: an explicit
    # path never reaches back into the default store for its piece bindings.
    pieces_file = _beside(target, "pieces.json", pieces_path)
    try:
        piece = resolve_piece(key, path=pieces_file)
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        # an unreadable pieces store falls back to the pre-piece, Set-only view
        import warnings
        warnings.warn("could not read %s (%s); resolving this Set's own decisions only"
                      % (pieces_file, exc), RuntimeWarning)
        piece = None
    siblings = set(piece["siblings"]) if piece else set()

    def resolvable(rows):
        rows = [row for row in rows if row.get("kind") != "arrangement_feedback"]
        # Finding-level protect rows are not Set decisions; legacy ones stored
        # their "intended" mark in verdict, which must not resolve as the
        # Set's verdict.
        return [{k: v for k, v in row.items() if k != "verdict"}
                if row.get("kind") == "protect" else row for row in rows]

    all_records = [row for row in data.get("decisions", []) if row.get("set_key") == key]
    records = resolvable(all_records)
    inherited_rows = resolvable([row for row in data.get("decisions", [])
                                 if row.get("set_key") in siblings]) if siblings else []
    result = {"revision": max((row.get("revision", 0) for row in all_records), default=0)}
    inherited = {}
    for field in _DECISION_FIELDS:
        candidates = [(row, True) for row in records if field in row]
        if field in _INHERITED_FIELDS:
            candidates += [(row, False) for row in inherited_rows if field in row]
        if not candidates:
            continue
        winner, exact = max(candidates, key=lambda item: (
            item[0].get("source") == "owner", item[1], item[0].get("revision", 0)))
        result[field] = winner[field]
        if not exact:
            inherited[field] = {"from": winner.get("set_key"), "source": winner.get("source"),
                                "revision": winner.get("revision", 0)}
    if piece:
        result["piece"] = {"piece_id": piece["piece_id"], "name": piece["name"],
                           "confirmed_versions": piece["confirmed_versions"]}
        result["inherited"] = inherited
    return result


def resolve_precedent(name_or_path, *, index_path):
    """Resolve an explicitly selected project name or source path.

    The index is store/projects/_index.json; project records are stored beside
    it as <id>.json. Names may be duplicated, so ambiguity is returned for the
    caller to present to the owner instead of choosing an arbitrary project.
    """
    with open(index_path, encoding="utf-8") as handle:
        index = json.load(handle)

    query = os.fspath(name_or_path)
    normalize_path = lambda value: os.path.realpath(os.fspath(value)).casefold()
    normalized_query = normalize_path(query)
    path_matches = [entry for entry in index if entry.get("path") and
                    normalize_path(entry["path"]) == normalized_query]
    if path_matches:
        matches = path_matches
    else:
        wanted = query.casefold()
        wanted_stem = wanted[:-4] if wanted.endswith(".als") else wanted
        matches = [entry for entry in index
                   if entry.get("id") == query or
                   (entry.get("name", "").casefold().removesuffix(".als") == wanted_stem)]

    if not matches:
        return {"missing": query}
    if len(matches) > 1:
        return {"ambiguous": [entry.get("path") for entry in matches]}

    project_path = os.path.join(os.path.dirname(os.fspath(index_path)),
                                matches[0]["id"] + ".json")
    with open(project_path, encoding="utf-8") as handle:
        return {"project": json.load(handle)}


def layer_matrix(clips, *, beats_per_bar, audio_track_names=None,
                 audio_clip_count=None):
    """Return arrangement tracks active per bar; take lanes are alternates.

    Bars are zero-based, cover [bar * beats_per_bar, (bar + 1) * beats_per_bar),
    and clips are treated as half-open intervals. Section guesses split where
    the layer count changes; those boundaries are inferred from clips alone.
    """
    try:
        beats_per_bar = float(beats_per_bar)
    except (TypeError, ValueError):
        raise ValueError("beats_per_bar must be positive") from None
    if not math.isfinite(beats_per_bar) or beats_per_bar <= 0:
        raise ValueError("beats_per_bar must be positive")

    arrangement = []
    max_end = 0.0
    for clip in clips:
        if clip.get("clip_source") not in (None, "arrangement"):
            continue
        track = clip.get("track")
        try:
            start, end = float(clip["start"]), float(clip["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end) or end <= start or track is None:
            continue
        arrangement.append((start, end, str(track)))
        max_end = max(max_end, end)

    if not arrangement:
        return {
            "no_arrangement": True,
            "message": "No arrangement clips are available; this project is session-only.",
        }

    bars = int(math.ceil(max_end / beats_per_bar)) if max_end > 0 else 0
    tracks_per_bar = []
    for bar in range(bars):
        left, right = bar * beats_per_bar, (bar + 1) * beats_per_bar
        active = sorted({track for start, end, track in arrangement
                         if start < right and end > left})
        tracks_per_bar.append(active)

    entries, exits = [], []
    for bar, active in enumerate(tracks_per_bar):
        previous = set(tracks_per_bar[bar - 1]) if bar else set()
        following = set(tracks_per_bar[bar + 1]) if bar + 1 < bars else set()
        entries.extend({"bar": bar, "track": track} for track in set(active) - previous)
        exits.extend({"bar": bar + 1, "track": track} for track in set(active) - following)
    entries.sort(key=lambda item: (item["bar"], item["track"]))
    exits.sort(key=lambda item: (item["bar"], item["track"]))

    counts = [len(tracks) for tracks in tracks_per_bar]
    sections = []
    if counts:
        start = 0
        for bar in range(1, bars + 1):
            if bar == bars or counts[bar] != counts[start]:
                sections.append({
                    "bar_start": start,
                    "bar_end": bar,
                    "layer_mean": float(counts[start]),
                    "status": "inferred",
                })
                start = bar

    audio_known = isinstance(audio_track_names, list) and isinstance(audio_clip_count, int)
    audio_unseen = ({"tracks": len(audio_track_names), "clips": audio_clip_count}
                    if audio_known else "unknown")
    matrix = {
        "basis": "midi_layers",
        "audio_unseen": audio_unseen,
        "bars": bars,
        "layers_per_bar": counts,
        "tracks_per_bar": tracks_per_bar,
        "entries": entries,
        "exits": exits,
        "sections_guess": sections,
        "evidence": "observed",
    }
    if not audio_known:
        matrix["warning"] = (
            "Audio-track metadata is unavailable; layer counts and habits cover MIDI only."
        )
    return matrix


def precedent_habits(matrix):
    """Extract deterministic, clip-observed arrangement habits from a matrix."""
    # A bar with no reading (None) holds no observed layer: count it as 0.
    counts = [value if isinstance(value, (int, float)) and not isinstance(value, bool)
              else 0 for value in (matrix.get("layers_per_bar") or [])]
    habits = []
    partial = matrix.get("audio_unseen") == "unknown" or bool(
        isinstance(matrix.get("audio_unseen"), dict) and
        matrix["audio_unseen"].get("tracks", 0)
    )
    if not counts:
        return habits

    # A thin-before-return is a >=2-bar valley between a previous maximum and
    # a later return to that maximum.
    for left in range(len(counts)):
        prior_max = max(counts[:left + 1])
        if counts[left] != prior_max:
            continue
        for right in range(left + _MIN_THIN_BARS + 1,
                           min(len(counts), left + _MAX_THIN_BARS + 2)):
            if counts[right] < prior_max:
                continue
            valley = counts[left + 1:right]
            if len(valley) >= _MIN_THIN_BARS and all(value < prior_max for value in valley):
                habits.append({
                    "kind": "thin_before_return", "bars": len(valley),
                    "bar_start": left + 1, "layers": valley[0],
                    "detail": "drop to %d layers for %d bars at bar %d before returning to %d layers" %
                              (valley[0], len(valley), left + 1, prior_max),
                    "status": "observed",
                })
                break

    # Use four-bar means like the measured reference envelope: first span
    # within 90% of the maximum marks plateau onset. Require 3/4 bars at 1.5x
    # baseline so one dense MIDI bar is not a peak, and apply the lateness
    # test where the rise BEGAN (walk back over the contiguous bars already
    # at 1.5x baseline), so a mid-piece plateau that thickens again later is
    # not a late peak. Same rule and comparator as tools/audio_ref.py.
    if len(counts) >= 4:
        spans = [(sum(counts[i:i + 4]) / 4, i) for i in range(len(counts) - 3)]
        peak_mean = max(mean for mean, _ in spans)
        onset = next(i for mean, i in spans if mean >= peak_mean * 0.9)
        prior = sorted(counts[:onset])
        track = sorted(counts)
        if prior and track:
            baseline = max(prior[len(prior) // 2], track[len(track) // 2])
            sustained = sum(value >= baseline * 1.5 for value in counts[onset:onset + 4])
            rise_start = onset
            while rise_start > 0 and counts[rise_start - 1] >= baseline * 1.5:
                rise_start -= 1
            if (peak_mean > 0 and baseline > 0 and sustained >= 3
                    and rise_start > len(counts) * 0.70):
                habits.append({"kind": "late_peak", "bars": rise_start,
                               "detail": "layer-count peak begins late and is sustained",
                               "status": "observed"})

    first_active = next((i for i, value in enumerate(counts) if value), None)
    if first_active is not None and first_active > 0:
        habits.append({"kind": "delayed_entry", "bars": first_active,
                       "detail": "arrangement layers first enter at bar %d" % first_active,
                       "status": "observed"})

    peak = max(counts)
    trailing = 0
    for value in reversed(counts):
        if value < peak:
            trailing += 1
        else:
            break
    if _MIN_STRIP_BARS <= trailing <= _MAX_STRIP_BARS:
        habits.append({"kind": "strip_ending", "bars": trailing,
                       "detail": "layer count stays below its peak for the final %d bars" % trailing,
                       "status": "observed"})
    for habit in habits:
        habit["partial"] = partial
    return habits
