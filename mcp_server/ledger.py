"""The judgment ledger — one line per clip the sidekick puts in his set.

Why this exists: `ratings.json` records that he pressed KEEP in the moment. It
records nothing about whether that clip was still in the Set when he closed Live.
The 4-6 week score is **session survival of judged suggestions** (`ROADMAP.md` →
Current arc), and that number is uncomputable without a written record of what
was written, when, and where it came from.

This module owns the write stamp and the end-of-night sweep (`close_session`) that
assigns terminal states while Live is reachable. The stamp has to exist first,
because a surviving clip cannot be traced back to the reference or gear that produced
it after the fact. The clip does not remember.

Terminal states (`ROADMAP.md`): keep | drop | abandoned | unjudged. Nothing here
assigns them except an explicit verdict; the sweep closes the rest. Unjudged must
never inflate the KEEP rate, so entries start with ``terminal_state: None`` and
are counted only once something actually resolves them.

Pure stdlib, atomic writes, flock — same contract as ratings.
"""
from __future__ import annotations

import bisect
import calendar
import json
import hashlib
import os
import re
import sys
import tempfile
import time
import uuid

# Keep bare imports used by the MCP adapters and package imports in one module
# universe.  This must run before any dependent module imports ``ledger``.
for _alias in ("ledger", "mcp_server.ledger"):
    sys.modules.setdefault(_alias, sys.modules[__name__])

try:
    import pp_codec as _pp_codec  # script-dir / selftest universe
except ModuleNotFoundError:  # package import without mcp_server on sys.path
    from mcp_server import pp_codec as _pp_codec

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER_PATH = os.path.join(ROOT, "store", "session_ledger.json")
ID_MAP_PATH = os.path.join(ROOT, "store", "phrase_id_map.json")

_ID_MAP_CACHE: dict | None = None
_ID_MAP_MTIME: float | None = None


def _phrase_id_map() -> dict:
    """Old positional alias → pid map from the last corpus build (missing →
    empty; resolution then degrades to exact match). Kept dependency-light:
    plain JSON, no analyze import. The cache is keyed on the map file's
    mtime, so a fresh build's map takes effect without a restart."""
    global _ID_MAP_CACHE, _ID_MAP_MTIME
    try:
        mtime = os.stat(ID_MAP_PATH).st_mtime
    except OSError:
        mtime = None
    if _ID_MAP_CACHE is None or _ID_MAP_MTIME != mtime:
        try:
            with open(ID_MAP_PATH) as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("map"), dict):
                _ID_MAP_CACHE = dict(data["map"])
                _ID_MAP_CACHE.update({key: None for key in
                                      (data.get("ambiguous") or {})})
            else:
                _ID_MAP_CACHE = dict(data) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _ID_MAP_CACHE = {}
        _ID_MAP_MTIME = mtime
    return _ID_MAP_CACHE


def _same_phrase_id(a: str | None, b: str | None) -> bool:
    """Stored-id equality across the pid migration: equal strings, or equal
    once old aliases resolve through the build's id map. pid first by
    construction — the map only ever points alias → pid."""
    if a == b:
        return True
    if not a or not b:
        return False
    id_map = _phrase_id_map()
    return id_map.get(a, a) is not None and id_map.get(b, b) is not None \
        and id_map.get(a, a) == id_map.get(b, b)

# A forgotten close must not merge two nights into one session. Six hours of
# silence ends a session at the last thing that actually happened, not at the
# moment somebody next opens Live.
SESSION_IDLE_SECONDS = 6 * 3600

# A bare retry of an already-judged cursor is a replay only this close to the
# original press; after that it is a stale press and must name its entry.
BARE_REPLAY_WINDOW_SECONDS = 15 * 60

TERMINAL_STATES = ("keep", "drop", "abandoned", "unjudged")
LEDGER_VERSION = 3
ATTEST_FACTS = ("present", "adopted", "absent", "not_adopted", "retire")


def _identity(value: dict | None) -> dict | None:
    """Return only the stable Set identity fields, never inventing identity."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("set_identity must be a dict")
    set_id = value.get("set_id")
    epoch = value.get("runtime_epoch")
    if not isinstance(set_id, str) or not set_id or not isinstance(epoch, str) or not epoch:
        raise ValueError("set_identity requires non-empty set_id and runtime_epoch")
    file_path = value.get("file_path")
    if file_path is not None and not isinstance(file_path, str):
        raise ValueError("set_identity.file_path must be a string or None")
    return {"set_id": set_id, "runtime_epoch": epoch, "file_path": file_path}


def canonical_notes(notes: list[dict] | None) -> list[dict]:
    """Canonical readback content: sorted, deduped, overlap-truncated, clamped.

    Mirrors ``pp_session.verify_notes`` so sent notes and Live readback agree:
    ``pp_codec.note_dicts`` → ``normalize_notes_for_pp`` (clamps negative
    starts, sorts by (start, pitch), drops exact dups, truncates same-pitch
    overlaps); velocity clamped 1..127; every other key (note_id,
    probability, release_velocity…) dropped. Short-key readback shapes
    (p/t/d/v, start_beats/duration_beats) are accepted too.
    """
    if not notes:
        return []
    reshaped = []
    for note in notes:
        if not isinstance(note, dict):
            raise TypeError("each answer note must be a dict")
        reshaped.append({
            "pitch": note.get("pitch", note.get("p")),
            "start": note.get("start_beats", note.get(
                "start", note.get("t", note.get("s", 0)))),
            "duration": note.get("duration_beats", note.get(
                "duration", note.get("d", 0.25))),
            "velocity": note.get("velocity", note.get("v", 80)),
        })
    normalized = _pp_codec.normalize_notes_for_pp(_pp_codec.note_dicts(reshaped))
    return [{"pitch": int(item["pitch"]), "start": float(item["start"]),
             "duration": float(item["duration"]),
             "velocity": max(1, min(127, int(item["velocity"])))}
            for item in normalized]


def content_equal(a: list[dict] | None, b: list[dict] | None) -> bool:
    """Canonical content equality within Live readback tolerance.

    Same note count after canonicalization, then pitch and velocity equal
    with start and duration within ``READBACK_TIME_TOLERANCE_BEATS`` (0.001)
    after (start, pitch) sort. The equality test — never the key alone —
    decides a cross-runtime match.
    """
    left = canonical_notes(a)
    right = canonical_notes(b)
    if len(left) != len(right):
        return False
    tolerance = _pp_codec.READBACK_TIME_TOLERANCE_BEATS
    left.sort(key=lambda n: (n["start"], n["pitch"]))
    right.sort(key=lambda n: (n["start"], n["pitch"]))
    for first, second in zip(left, right):
        if first["pitch"] != second["pitch"] or first["velocity"] != second["velocity"]:
            return False
        if abs(first["start"] - second["start"]) > tolerance:
            return False
        if abs(first["duration"] - second["duration"]) > tolerance:
            return False
    return True


_LEGACY_SLOT_RE = re.compile(r"^(?:session:)?(\d+)/(\d+)$")


def canonical_slot(slot: str | None) -> str | None:
    """Canonical slot spelling: ``31/0`` and ``session:31/0`` → ``t31/s0``.

    ``t31/s0`` is the clip-path form verified writes report; ``t9``
    (arrangement track) and ``t9/lN`` (take lane) are already paths and pass
    through unchanged. Rows are stored canonically from ``record_write`` on;
    comparisons canonicalize both sides so legacy spellings still bind.
    """
    if not isinstance(slot, str):
        return slot
    match = _LEGACY_SLOT_RE.match(slot.strip())
    if match:
        return "t%s/s%s" % (match.group(1), match.group(2))
    return slot.strip()


_DRUM_ROLE_WORDS = ("drums", "drum", "perc", "percussion")


def canonical_role(role: str | None) -> str:
    """Canonical role spelling: drum synonyms fold to ``drum``; an absent or
    automatic classification is honestly ``unknown``; anything else is
    lower-case. Drum-ness decides transposition rights at adoption, so
    ``percussion`` and ``drum`` must not straddle two strata."""
    if role is None:
        return "unknown"
    text = str(role).strip().lower()
    if text in ("", "auto"):
        return "unknown"
    if text in _DRUM_ROLE_WORDS:
        return "drum"
    return text


def learning_role(role: str | None) -> str | None:
    """The role LEARNING sinks should see: canonical spelling, with the
    honest ``unknown`` placeholder mapped back to None so core.rate infers
    from register instead of trusting a placeholder as a stored role."""
    canonical = canonical_role(role)
    return None if canonical == "unknown" else canonical


def _key_of_canon(canon: list[dict]) -> str:
    """Index/dedup key over canonical tick tuples. Never the sole match test."""
    tuples = [(item["pitch"], round(item["start"] * 960),
               round(item["duration"] * 960), item["velocity"])
              for item in canon]
    return hashlib.sha256(json.dumps(
        tuples, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def content_key(notes: list[dict] | None) -> str:
    """Index/dedup key: sha256 of canonical tick tuples. Never the sole match test."""
    return _key_of_canon(canonical_notes(notes))


def _canon_equal(left: list[dict], right: list[dict]) -> bool:
    """Content equality over canonical lists within Live readback tolerance.

    Same comparisons as ``content_equal`` without re-canonicalizing: the
    sweep canonicalizes each clip once and reuses the lists everywhere.
    """
    if len(left) != len(right):
        return False
    tolerance = _pp_codec.READBACK_TIME_TOLERANCE_BEATS
    left = sorted(left, key=lambda n: (n["start"], n["pitch"]))
    right = sorted(right, key=lambda n: (n["start"], n["pitch"]))
    for first, second in zip(left, right):
        if first["pitch"] != second["pitch"] or first["velocity"] != second["velocity"]:
            return False
        if abs(first["start"] - second["start"]) > tolerance:
            return False
        if abs(first["duration"] - second["duration"]) > tolerance:
            return False
    return True


def _content_fingerprint(answer_notes: list[dict] | None) -> str:
    """Content-only fingerprint: the canonical key, no clip name, no track.

    Names change (renames) and readback normalizes note order, dups and
    overlaps, so neither may enter the key. This is deliberately the sole
    fingerprint implementation.
    """
    return content_key(answer_notes)


def seed_fingerprint(seed_notes: list[dict] | None) -> str | None:
    """Digest of canonical seed notes, stamped at JAM-write time.

    Session memory compares seed-to-seed (this digest, else the seed
    vectors), never seed-to-transposed-answer — the transposition the
    answer path applies keeps seed/answer cosine far below any useful
    threshold on real data. Pure; None when there is nothing to stamp.
    """
    if not seed_notes:
        return None
    try:
        return _content_fingerprint(_normalize_answer_notes(seed_notes))
    except (TypeError, ValueError, KeyError):
        return None


def _seed_vector(seed_notes: list[dict] | None) -> list[float] | None:
    """Compact seed similarity stamp used by session re-roll memory."""
    if not seed_notes:
        return None
    try:
        try:
            from build_index import phrase_vector
        except ModuleNotFoundError:
            from mcp_server.build_index import phrase_vector
        return [round(float(value), 3)
                for value in phrase_vector(_normalize_answer_notes(seed_notes))]
    except Exception:
        return None


def _entry_notes(entry: dict) -> list[dict] | None:
    """The row's own readback notes when present, else the sent notes."""
    ref = entry.get("clip_ref") or {}
    if _has_content(ref):
        return ref.get("answer_notes")
    return entry.get("answer_notes")


def _clip_ref(value: dict | None) -> dict | None:
    """Validate a verified clip reference without coercing runtime IDs."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("clip_ref must be a dict")
    required = ("clip_id", "track_id", "track", "surface", "path")
    if any(not isinstance(value.get(k), str) or not value.get(k) for k in required):
        raise ValueError("clip_ref requires string clip_id, track_id, track, surface and path")
    if value["surface"] not in ("session", "arrangement"):
        raise ValueError("clip_ref.surface must be session or arrangement")
    result = dict(value)
    if "answer_notes" in result and result["answer_notes"] is not None:
        result["answer_notes"] = _normalize_answer_notes(result["answer_notes"])
    result["fingerprint"] = _content_fingerprint(result.get("answer_notes"))
    return result


def _same_set(left: dict | None, right: dict | None, *, runtime: bool = True) -> bool:
    if not left or not right:
        return False
    keys = ("set_id", "runtime_epoch") if runtime else ("set_id",)
    return all(left.get(k) == right.get(k) for k in keys)


def _normalize_answer_notes(notes: list[dict]) -> list[dict]:
    """Canonicalize MCP or internal note dictionaries without importing core."""
    if not isinstance(notes, list):
        raise TypeError("answer_notes must be a list")
    expressive = {"probability", "velocity_deviation", "release_velocity", "note_id",
                  "mute", "muted", "off_velocity", "chance", "duration_ticks"}
    result = []
    for note in notes:
        if not isinstance(note, dict):
            raise TypeError("each answer note must be a dict")
        item = {
            "pitch": int(note["pitch"]),
            "start": float(note.get("start_beats", note.get("start", 0))),
            "duration": float(note.get("duration_beats", note.get("duration", 0.25))),
            "velocity": int(note.get("velocity", 80)),
        }
        for key in expressive:
            if key in note:
                item[key] = note[key]
        result.append(item)
    return result


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    # Microsecond precision: verdicts and sweeps landing in the same second
    # still order correctly (lateness compares these strings).
    whole = int(ts)
    micros = int(round((ts - whole) * 1_000_000))
    if micros >= 1_000_000:
        whole += 1
        micros -= 1_000_000
    return "%s.%06dZ" % (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole)),
                         micros)


def _iso_after(first: str | None, second: str | None) -> bool:
    """Strictly-after comparison tolerant of pre-microsecond stamps.

    Old stores stamp whole seconds ("…52Z"); normalizing them to
    "…52.000000Z" keeps mixed-shape comparisons ordered.
    """
    def normalize(stamp: str) -> str:
        if stamp.endswith("Z") and "." not in stamp:
            return stamp[:-1] + ".000000Z"
        return stamp
    return bool(first and second) and normalize(first) > normalize(second)


def _empty() -> dict:
    return {"version": LEDGER_VERSION, "sessions": [], "entries": [], "adoptions": [],
            "observations": [], "judgment_events": [], "accounting_failures": []}


def _session_chain(session: dict | None) -> set[str]:
    """Every Set id one session has been: its identity plus recorded aliases."""
    chain: set[str] = set()
    session = session or {}
    identity = session.get("set_identity") or {}
    if identity.get("set_id"):
        chain.add(identity["set_id"])
    for alias in session.get("set_aliases") or []:
        alias_id = alias.get("set_id") if isinstance(alias, dict) else alias
        if alias_id:
            chain.add(alias_id)
    return chain


def session_chain(session_id: str, path: str | None = None) -> set[str]:
    """Every Set id one session has been: its identity plus recorded aliases.

    Public so explicit adoption links (which resolve against Live evidence,
    not the sweep) can accept any id the night has carried.
    """
    data = load(path)
    session = next((s for s in data.get("sessions", [])
                    if s.get("id") == session_id), None)
    return _session_chain(session)


def _migrate(data: dict) -> bool:
    """Additive, idempotent migration; old evidence is never reclassified."""
    version = data.get("version", 1)
    if not isinstance(version, int):
        raise ValueError("session ledger has an invalid version")
    if version > LEDGER_VERSION:
        raise ValueError("session ledger version %s is newer than supported version %s" %
                         (version, LEDGER_VERSION))
    changed = False
    if version < 3:
        data["version"] = 3
        changed = True
    if "sessions" not in data:
        data["sessions"] = []
        changed = True
    if "entries" not in data:
        data["entries"] = []
        changed = True
    if "adoptions" not in data:
        data["adoptions"] = []
        changed = True
    if "observations" not in data:
        data["observations"] = []
        changed = True
    if "judgment_events" not in data:
        data["judgment_events"] = []
        changed = True
    if "accounting_failures" not in data:
        data["accounting_failures"] = []
        changed = True
    for session in data["sessions"]:
        if "set_identity" not in session:
            session["set_identity"] = None
            changed = True
        if "mode" not in session:
            session["mode"] = None
            changed = True
        if "set_aliases" not in session:
            session["set_aliases"] = []
            changed = True
        if "runtime_epochs" not in session:
            epoch = (session.get("set_identity") or {}).get("runtime_epoch")
            session["runtime_epochs"] = [epoch] if epoch else []
            changed = True
        if "ended_at" not in session:
            session["ended_at"] = None
            changed = True
        if "ended_by" not in session:
            session["ended_by"] = None
            changed = True
        if "closed_by" not in session:
            session["closed_by"] = None
            changed = True
        if "rate_status" not in session:
            session["rate_status"] = None
            changed = True
        # A night the sweep closed with nothing left open was scored under
        # the old rule: restore its definitive standing.
        if session.get("closed_by") == "sweep" and session.get("rate_status") is None:
            open_rows = [entry for entry in data["entries"]
                         if entry.get("session_id") == session.get("id") and
                         entry.get("terminal_state") is None and
                         not entry.get("superseded_by") and
                         not entry.get("retired") and not entry.get("attestation")]
            if not open_rows:
                session["rate_status"] = "definitive"
                changed = True
        # A night the old code "closed" idle with rows still unresolved was
        # never actually scored — it only stopped being reminded about. Mark
        # it ended (state only; no evidence reclassified) so it can be swept.
        if session.get("closed_by") == "idle" and session.get("closed_at") is not None:
            rows = [entry for entry in data["entries"]
                    if entry.get("session_id") == session.get("id") and
                    entry.get("terminal_state") is None and
                    not entry.get("superseded_by")]
            if rows:
                session["ended_at"] = session["closed_at"]
                session["ended_by"] = "idle"
                session["closed_at"] = None
                changed = True
    for entry in data["entries"]:
        for key, default in (("set_identity", None), ("clip_ref", None),
                             ("fingerprint", None), ("revision_of", None),
                             ("suggestion_id", entry.get("id")),
                             ("adoption_links", [])):
            if key not in entry:
                entry[key] = default
                changed = True
        # Legacy identity is intentionally unresolved, even if a later session
        # happens to have the same name or file path.
        if "identity_status" not in entry:
            entry["identity_status"] = "unknown" if entry.get("set_identity") is None else "verified"
            changed = True
    return changed


def _read_data(path: str) -> tuple[dict, bool, bytes | None]:
    if not os.path.exists(path):
        return _empty(), False, None
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        data = json.loads(raw)
    except (ValueError, OSError) as exc:
        raise ValueError(
            "session ledger is unreadable (%s) — move %s aside to start fresh"
            % (exc, path)) from exc
    if not isinstance(data, dict):
        raise ValueError("session ledger has an invalid shape — move %s aside" % path)
    return data, _migrate(data), raw


def load(path: str | None = None) -> dict:
    path = path or LEDGER_PATH
    data, _, _ = _read_data(path)
    return data


def _load_for_mutation(path: str) -> dict:
    """Read and migrate while already holding the ledger lock.

    Reads stay byte-for-byte read-only.  If this is the first mutation that
    needs an additive migration, preserve the original bytes once before the
    caller's atomic save.
    """
    data, changed, raw = _read_data(path)
    if changed and raw is not None:
        backup = path + ".bak"
        if not os.path.exists(backup):
            with open(backup, "xb") as fh:
                fh.write(raw)
    return data


def _fallback_file_fsync(fd: int) -> None:
    """Inline F_FULLFSYNC-or-fsync for _save's no-core_shared path."""
    try:
        import fcntl as _fcntl_mod
    except ImportError:
        _fcntl_mod = None
    if (_fcntl_mod is not None and sys.platform == "darwin"
            and hasattr(_fcntl_mod, "F_FULLFSYNC")):
        try:
            _fcntl_mod.fcntl(fd, _fcntl_mod.F_FULLFSYNC)
            return
        except OSError:
            pass
    os.fsync(fd)


def _fallback_fsync_dir(directory: str) -> None:
    """Inline best-effort directory sync: never fails a committed save."""
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


def _save(data: dict, path: str | None = None) -> None:
    try:
        import core_shared as _shared
    except ModuleNotFoundError:  # package entrypoint without mcp_server on sys.path
        try:
            from mcp_server import core_shared as _shared
        except ModuleNotFoundError:
            _shared = None
    target = path or LEDGER_PATH
    if _shared is not None:
        _shared._rolling_backup(target)
        _shared._save_json_atomic(target, data)
        return
    directory = os.path.dirname(os.path.abspath(target)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".%s." % os.path.basename(target),
                                      suffix=".tmp", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            _fallback_file_fsync(fh.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    # After os.replace the new content is on disk: directory durability
    # stays outside the tmp-cleanup try so it can never fail the save.
    try:
        _fallback_fsync_dir(directory)
    except OSError:
        pass


def set_outcome_op_pending(entry_id: str, pending: bool,
                           path: str | None = None) -> None:
    """Persist the sole retry signal for a failed outcome rating write."""
    path = path or LEDGER_PATH
    with _lock(path):
        data = _load_for_mutation(path)
        entry = next((e for e in data.get("entries", [])
                      if e.get("id") == entry_id), None)
        if entry is None:
            return
        if bool(entry.get("outcome_op_pending")) == bool(pending):
            return
        if pending:
            entry["outcome_op_pending"] = True
        else:
            entry.pop("outcome_op_pending", None)
        _save(data, path)


def _lock(path: str):
    import contextlib
    import fcntl

    @contextlib.contextmanager
    def _cm():
        lock_path = (path or LEDGER_PATH) + ".lock"
        os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
        with open(lock_path, "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return _cm()


def _end_session(session: dict, now: float, ended_by: str) -> None:
    """End a session at its last real activity, never at "now".

    Ending is not closing: the night stays sweepable until a complete
    inventory actually scores it. Only the sweep writes ``closed_at``.
    """
    session["ended_at"] = _iso(session.get("last_activity", now))
    session["ended_by"] = ended_by


def _join_session(session: dict, set_identity: dict | None, now: float,
                  holds_entries: bool = False) -> bool:
    """Decide whether a write joins the open session, recording alias evidence.

    Identity-less writes always join and never change identity. A session
    that never learned its Set and holds no rows adopts the first real
    identity instead of splitting — but a night that already HOLDS
    identity-less rows can never adopt one (no sweep could reconcile those
    rows against a Set they predate), so tonight's Set opens a new session.
    The same Set under a new id in one Live run (first save, Save-As)
    continues with an alias; a Live restart (same Set, new epoch) continues
    with a recorded epoch. Anything else is a different Set.
    """
    current = session.get("set_identity")
    if set_identity is None:
        return True
    if current is None:
        if holds_entries:
            return False
        session["set_identity"] = set_identity
        epochs = session.setdefault("runtime_epochs", [])
        if set_identity["runtime_epoch"] not in epochs:
            epochs.append(set_identity["runtime_epoch"])
        return True
    if set_identity.get("set_id") == current.get("set_id") or \
            set_identity.get("set_id") in _session_chain(session):
        # The same Set, possibly reached through a recorded alias (first
        # save, Save-As): a Live restart under any known id still joins,
        # recording the new epoch.
        if set_identity.get("runtime_epoch") != current.get("runtime_epoch"):
            epochs = session.setdefault("runtime_epochs", [])
            if set_identity["runtime_epoch"] not in epochs:
                epochs.append(set_identity["runtime_epoch"])
        return True
    epochs = session.get("runtime_epochs") or [current.get("runtime_epoch")]
    if set_identity.get("runtime_epoch") == current.get("runtime_epoch") or \
            set_identity.get("runtime_epoch") == epochs[-1]:
        # The same Live run under a new id (first save, Save-As): alias it.
        # Epochs are compared against the latest known run, so a Save-As
        # after a restart still joins.
        kind = ("first_save" if str(current.get("set_id", "")).startswith("unsaved:")
                else "save_as")
        if set_identity["set_id"] not in _session_chain(session):
            session.setdefault("set_aliases", []).append(
                {"set_id": set_identity["set_id"], "alias_kind": kind,
                 "at": _iso(now)})
        session["alias_kind"] = kind
        return True
    return False


def _active_session(data: dict, now: float, mode: str | None = None,
                    set_identity: dict | None = None) -> dict:
    """Return the open session, starting one if none is live. Idempotent.

    A night survives saves (aliases within one runtime epoch) and Live
    restarts (same Set, new epoch, inside the idle window). A different Set
    or an idle gap only ENDS the previous session at its last activity —
    never closes it, so the sweep can still score it.
    """
    sessions = data["sessions"]
    if sessions:
        last = sessions[-1]
        # An ended or closed session is finished. A complete sweep ends the
        # night it scores, so a later write must open a NEW session — joining
        # scored history would let one clip land in two nights.
        if not last.get("closed_at") and not last.get("ended_at"):
            if (now - last.get("last_activity", 0)) > SESSION_IDLE_SECONDS:
                _end_session(last, now, "idle")
            elif _join_session(last, set_identity, now, holds_entries=any(
                    e.get("session_id") == last.get("id")
                    for e in data["entries"])):
                last["last_activity"] = now
                if mode and not last.get("mode"):
                    last["mode"] = mode
                elif mode and last.get("mode") and last.get("mode") != mode:
                    last["mode"] = "mixed"
                return last
            else:
                _end_session(last, now, "set_change")
    fresh = {
        "id": "s-" + uuid.uuid4().hex[:10],
        "opened_at": _iso(now),
        "last_activity": now,
        "closed_at": None,
        "closed_by": None,
        "ended_at": None,
        "ended_by": None,
        "rate_status": None,
        "alias_kind": None,
        "mode": mode,
        "set_identity": set_identity,
        "set_aliases": [],
        "runtime_epochs": [set_identity["runtime_epoch"]] if set_identity else [],
    }
    sessions.append(fresh)
    return fresh


def _needs_sweep(entry: dict) -> bool:
    """Whether a row still needs a sweep to land its verified state.

    Owner-attested and retired rows count as resolved — except a declined
    adoption candidate (``not_adopted``), which only declined the copy: the
    verified absence of the row's own clip still needs its terminal state.
    """
    if entry.get("terminal_state") is not None or entry.get("superseded_by") \
            or entry.get("retired"):
        return False
    attestation = entry.get("attestation") or {}
    return not attestation or attestation.get("fact") == "not_adopted"


def sessions_needing_sweep(data: dict) -> list[dict]:
    """Every non-closed session with unresolved rows, oldest first. Read-only.

    Owner-attested and retired rows count as resolved: they have an answer,
    just not a verified one."""
    out = []
    for session in data.get("sessions", []):
        if session.get("closed_at"):
            continue
        rows = [entry for entry in data.get("entries", [])
                if entry.get("session_id") == session.get("id") and
                _needs_sweep(entry)]
        if rows:
            out.append({"session": session, "entries": rows})
    return out


def open_session(mode: str | None = None, path: str | None = None, *,
                 set_identity: dict | None = None) -> dict:
    """Start (or return) the current session. ``mode``: 'ref' | 'catalogue'.

    Mode is recorded so ref-assisted and catalogue-only nights can be compared
    without selection bias — the ROADMAP asks for alternating sessions, and that
    is unenforceable if nobody writes down which kind a night was.
    """
    path = path or LEDGER_PATH
    with _lock(path):
        data = _load_for_mutation(path)
        s = _active_session(data, _now(), mode=mode,
                            set_identity=_identity(set_identity))
        _save(data, path)
        return dict(s)


def record_write(*, clip_name: str, slot: str | None, source: str,
                 role: str = "lead", gear: int | None = None,
                 move: str | None = None,
                 ears: str | None = None, track: str = "SIDEKICK JAM",
                 source_phrase_id: str | None = None,
                 tempo: float | None = None, n_notes: int | None = None,
                 answer_notes: list[dict] | None = None,
                 seed_notes: list[dict] | None = None,
                 key: str | None = None,
                 session_mode: str | None = None,
                 ref_prose: str | None = None,
                 set_identity: dict | None = None,
                 clip_ref: dict | None = None,
                 suggestion_id: str | None = None,
                 revision_of: str | None = None,
                 adoption_links: list[dict] | None = None,
                 path: str | None = None) -> dict:
    """Stamp one clip as it lands. ``source`` is 'catalogue', 'ref:<ref_id>',
    or 'gold_composed' (a served composed gold — never 'catalogue').

    ``ref_prose`` names a reference that was PRESENT but did not steer (its
    brief travelled as text on a gear that does no retrieval). It is recorded
    so nothing is lost, and kept out of ``source`` so the strata stay honest.

    Stamped at write time on purpose: which gear, which reference, and whether
    the room was live cannot be reconstructed later by looking at the Set.
    """
    path = path or LEDGER_PATH
    with _lock(path):
        data = _load_for_mutation(path)
        now = _now()
        slot = canonical_slot(slot)
        role = canonical_role(role)
        canonical_identity = _identity(set_identity)
        normalized_answer_notes = (_normalize_answer_notes(answer_notes)
                                   if answer_notes is not None else None)
        seed_fp = seed_fingerprint(seed_notes)
        seed_vec = _seed_vector(seed_notes)
        canonical_ref = dict(_clip_ref(clip_ref) or {}) or None
        if canonical_ref is not None:
            canonical_ref.setdefault("name", clip_name)
            if "answer_notes" not in canonical_ref:
                canonical_ref["answer_notes"] = normalized_answer_notes
            canonical_ref = _clip_ref(canonical_ref)
        fingerprint = _content_fingerprint(normalized_answer_notes)
        if revision_of:
            entry = next((e for e in data["entries"] if e["id"] == revision_of), None)
            if entry is None:
                raise ValueError("revision_of does not name an existing ledger entry")
            final_session = next((s for s in data["sessions"]
                                  if s.get("id") == entry.get("session_id")
                                  and s.get("closed_by") == "sweep"), None)
            if (entry.get("set_identity") and canonical_identity and
                    entry["set_identity"].get("set_id") != canonical_identity.get("set_id")):
                raise ValueError("revision must remain in the same Set")
            snapshot_keys = ("clip_name", "slot", "track", "source",
                             "source_phrase_id", "answer_notes",
                             "key", "ref_prose", "role", "gear", "move", "ears",
                             "tempo", "n_notes", "set_identity", "clip_ref", "fingerprint")
            entry.setdefault("revision_history", []).append({
                "revision": int(entry.get("revision", 1)), "recorded_at": _iso(now),
                "snapshot": {key_: entry.get(key_) for key_ in snapshot_keys},
            })
            entry.pop("seed_notes", None)
            entry.update({
                "written_at": _iso(now), "track": track, "slot": slot,
                "clip_name": clip_name, "source": source, "source_phrase_id": source_phrase_id,
                "answer_notes": normalized_answer_notes,
                "seed_fingerprint": seed_fp,
                "seed_vector": seed_vec,
                "key": key, "ref_prose": ref_prose,
                "role": role, "gear": gear, "move": move, "ears": ears, "tempo": tempo,
                "n_notes": n_notes,
                "set_identity": canonical_identity, "clip_ref": canonical_ref,
                "fingerprint": fingerprint, "revision": int(entry.get("revision", 1)) + 1,
                "identity_status": "verified" if canonical_identity and canonical_ref else "unknown",
            })
            if final_session is None:
                entry["terminal_state"] = None
                for field in ("swept_at", "resolution_reason", "resolution_match",
                              "matched_clip", "matched_inventory_identity",
                              "presence", "provisional_state",
                              "adoption_candidates"):
                    entry.pop(field, None)
            _save(data, path)
            return dict(entry)

        session = _active_session(data, now, mode=session_mode,
                                  set_identity=canonical_identity)
        entry = {
            "id": "e-" + uuid.uuid4().hex[:10], "session_id": session["id"],
            "written_at": _iso(now), "track": track, "slot": slot,
            "clip_name": clip_name, "source": source, "source_phrase_id": source_phrase_id,
            "answer_notes": normalized_answer_notes,
            "seed_fingerprint": seed_fp,
            "seed_vector": seed_vec,
            "key": key, "ref_prose": ref_prose,
            "role": role, "gear": gear, "move": move, "ears": ears, "tempo": tempo,
            "n_notes": n_notes,
            "immediate_verdict": None, "terminal_state": None,
            "set_identity": canonical_identity, "clip_ref": canonical_ref,
            "fingerprint": fingerprint,
            "suggestion_id": suggestion_id or "e-" + uuid.uuid4().hex[:10],
            "revision_of": None, "revision": 1, "revision_history": [],
            "adoption_links": list(adoption_links or []),
            "identity_status": "verified" if canonical_identity and canonical_ref else "unknown",
        }
        data["entries"].append(entry)
        # This is both the serving cursor and the durable default judgment
        # target.  A bare retry must stay on this suggestion until a newer
        # write advances it; pending-row recency alone is not identity.
        session["last_served_entry_id"] = entry["id"]
        session["last_judgment_target_entry_id"] = entry["id"]
        _save(data, path)
        return dict(entry)


def _event_fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _same_judgment_modulo_alias(payload: dict, stored: dict | None) -> bool:
    """A retry equals the stored judgment when its phrase id is the same
    phrase across the pid migration and every other field is byte-identical
    under the digest's own canonical JSON. The phrase-id migration rewrites
    the stored payload's id but keeps the original digest (the sinks hold
    copies of it), so the digests alone can no longer decide."""
    if not isinstance(stored, dict):
        return False
    if not _same_phrase_id(payload.get("source_phrase_id"),
                           stored.get("source_phrase_id")):
        return False
    return _event_fingerprint(
        {**payload, "source_phrase_id": stored.get("source_phrase_id")}) == \
        _event_fingerprint(stored)


def begin_judgment(event_id: str, payload: dict, *, entry_id: str | None = None,
                   path: str | None = None) -> dict:
    """Create or recover the durable judgment journal record.

    This is deliberately a small journal, not a general transaction manager:
    each adapter sink records its own event id and this record says which safe
    steps have been observed. A retry can therefore finish unapplied steps
    after a crash between a sink's atomic save and this journal acknowledgement.
    """
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("event_id must be a non-empty string")
    if not isinstance(payload, dict):
        raise TypeError("judgment payload must be a dict")
    fingerprint = _event_fingerprint(payload)
    path = path or LEDGER_PATH
    with _lock(path):
        data = _load_for_mutation(path)
        events = data.setdefault("judgment_events", [])
        event = next((item for item in events if item.get("event_id") == event_id), None)
        if event is not None:
            if event.get("fingerprint") != fingerprint and \
                    not _same_judgment_modulo_alias(payload, event.get("payload")):
                raise ValueError("conflicting retry for judgment event %s" % event_id)
            # The STORED digest goes back to the caller: the sinks saved that
            # one on the first pass and compare against it on the retry.
            return dict(event)
        event = {"event_id": event_id, "fingerprint": fingerprint,
                 "entry_id": entry_id, "payload": dict(payload),
                 "status": "pending", "steps": {}, "result": None,
                 "created_at": _iso(_now()), "updated_at": _iso(_now())}
        events.append(event)
        _save(data, path)
        return dict(event)


def judgment_event(event_id: str, *, path: str | None = None) -> dict | None:
    data = load(path)
    return next((dict(event) for event in data.get("judgment_events", [])
                 if event.get("event_id") == event_id), None)


def mark_judgment_step(event_id: str, step: str, *, status: str = "applied",
                       result=None, error: str | None = None,
                       path: str | None = None) -> dict:
    if status not in ("pending", "applied", "failed"):
        raise ValueError("invalid judgment step status")
    path = path or LEDGER_PATH
    with _lock(path):
        data = _load_for_mutation(path)
        event = next((item for item in data.setdefault("judgment_events", [])
                      if item.get("event_id") == event_id), None)
        if event is None:
            raise ValueError("unknown judgment event %s" % event_id)
        current = event.setdefault("steps", {}).get(step)
        if current and current.get("status") == "applied" and status == "failed":
            return dict(event)
        event["steps"][step] = {"status": status, "result": result,
                                 "error": error, "updated_at": _iso(_now())}
        event["updated_at"] = _iso(_now())
        if status == "failed":
            event["status"] = "failed"
        elif {"ears", "develop", "rating", "ledger"}.issubset(event["steps"]) and \
                all(event["steps"][name].get("status") == "applied"
                    for name in ("ears", "develop", "rating", "ledger")):
            event["status"] = "complete"
        else:
            event["status"] = "pending"
        _save(data, path)
        return dict(event)


def complete_judgment(event_id: str, result: dict, *, path: str | None = None) -> dict:
    path = path or LEDGER_PATH
    with _lock(path):
        data = _load_for_mutation(path)
        event = next((item for item in data.setdefault("judgment_events", [])
                      if item.get("event_id") == event_id), None)
        if event is None:
            raise ValueError("unknown judgment event %s" % event_id)
        event["result"] = dict(result)
        event["status"] = "complete" if {"ears", "develop", "rating", "ledger"}.issubset(
            event.get("steps", {})) and all(
                event["steps"][name].get("status") == "applied"
                for name in ("ears", "develop", "rating", "ledger")) else "pending"
        event["updated_at"] = _iso(_now())
        _save(data, path)
        return dict(event)


def _judgable(entry: dict) -> bool:
    """Whether a row can still take a verdict: nothing terminal, superseded,
    retired or owner-attested stands in the way."""
    return (entry.get("terminal_state") is None
            and not entry.get("superseded_by")
            and not entry.get("retired")
            and not entry.get("attestation"))


def _live_sessions(data: dict, now: float) -> list[dict]:
    """Sessions a judgment may still reach without naming an entry id:
    not closed, not ended, and active inside the idle window."""
    return [s for s in data.get("sessions", [])
            if not s.get("closed_at") and not s.get("ended_at")
            and (now - s.get("last_activity", 0)) <= SESSION_IDLE_SECONDS]


def _epoch_of_iso(stamp: str | None) -> float | None:
    """UTC epoch of an ``_iso`` stamp, tolerant of pre-microsecond shapes."""
    if not isinstance(stamp, str):
        return None
    try:
        whole = calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
        micros = float(stamp[20:26]) / 1_000_000 if len(stamp) >= 26 else 0.0
        return whole + micros
    except (ValueError, TypeError, OverflowError):
        return None


def record_verdict(verdict: str, *, slot: str | None = None,
                   entry_id: str | None = None, event_id: str | None = None,
                   conflict_fingerprint: str | None = None,
                   path: str | None = None) -> dict | None:
    """Attach an immediate KEEP/DROP to the one entry a caller has bound.

    An explicit entry id must match exactly (a canonical slot mismatch
    raises — never stamp a different row); a slot alone is honoured only
    inside the active session and only when it identifies ONE pending row.
    There is deliberately no newest-across-all-sessions fallback: binding
    is ``resolve_judgment_target``'s job, not the stamper's.

    Returns the updated entry, or None when nothing matches — a verdict on a
    clip the ledger never saw is reported, not invented.
    """
    verdict = str(verdict).strip().lower()
    if verdict not in ("keep", "drop"):
        raise ValueError("verdict must be 'keep' or 'drop'")
    path = path or LEDGER_PATH
    with _lock(path):
        data = _load_for_mutation(path)
        match = None
        if entry_id is not None:
            match = next((e for e in data["entries"] if e["id"] == entry_id), None)
            if match is None:
                return None
            if slot is not None and canonical_slot(slot) != canonical_slot(match.get("slot")):
                raise ValueError(
                    "slot %r does not match ledger entry %s (stored %r)"
                    % (slot, entry_id, match.get("slot")))
        elif slot is not None:
            # Slot-only: the active session's unique pending row, or nothing.
            live = _live_sessions(data, _now())
            if live:
                sid = live[-1].get("id")
                slot_key = canonical_slot(slot)
                candidates = [e for e in data["entries"]
                              if e.get("session_id") == sid and _judgable(e)
                              and e.get("immediate_verdict") is None
                              and canonical_slot(e.get("slot")) == slot_key]
                if len(candidates) == 1:
                    match = candidates[0]
            if match is None:
                return None
        else:
            return None
        # A judgment is session activity: it keeps the night joined while he
        # is still deciding — but a night idle past the window is effectively
        # ended, and bumping it would resurrect a dead night so tonight's
        # write joins it (the legacy-night swallow).
        now = _now()
        session = next((item for item in data.get("sessions", [])
                        if item.get("id") == match.get("session_id")), None)
        if session is not None and session in _live_sessions(data, now):
            session["last_activity"] = now
        if match.get("immediate_verdict") is not None:
            if match["immediate_verdict"] != verdict:
                raise ValueError("conflicting verdict for ledger entry %s" % match["id"])
            if event_id:
                event = next((item for item in data.setdefault("judgment_events", [])
                              if item.get("event_id") == event_id), None)
                if event is not None:
                    event.setdefault("steps", {})["ledger"] = {
                        "status": "applied", "result": {"entry_id": match["id"]},
                        "error": None, "updated_at": _iso(_now())}
                    if {"ears", "develop", "rating", "ledger"}.issubset(event["steps"]) and \
                            all(event["steps"][name].get("status") == "applied"
                                for name in ("ears", "develop", "rating", "ledger")):
                        event["status"] = "complete"
                    _save(data, path)
            return dict(match)
        if event_id:
            event = next((item for item in data.setdefault("judgment_events", [])
                          if item.get("event_id") == event_id), None)
            if event is not None and conflict_fingerprint is not None and \
                    event.get("fingerprint") != conflict_fingerprint:
                raise ValueError("conflicting retry for judgment event %s" % event_id)
        match["immediate_verdict"] = verdict
        match["judged_at"] = _iso(_now())
        # A verdict after the night's evidence was sealed (the first sweep
        # attempt), or on a closed session, is accepted but marked late: it
        # scores as verdict-less and is reported separately, never reshaping
        # the sealed night.
        if session is not None and (
                session.get("closed_at") is not None
                or _iso_after(match["judged_at"], session.get("swept_at"))
                or _iso_after(match["judged_at"],
                              (match.get("presence") or {}).get("swept_at"))):
            match["late"] = True
        if "judged_snapshot" not in match:
            snapshot_keys = ("clip_name", "slot", "track", "source", "source_phrase_id",
                             "answer_notes", "key", "ref_prose", "role", "gear", "move", "ears",
                             "tempo", "n_notes", "set_identity", "clip_ref", "fingerprint")
            match["judged_snapshot"] = {key_: match.get(key_) for key_ in snapshot_keys}
        # NOT a terminal state. He can press KEEP and delete the clip an hour
        # later — that is the whole reason this project stopped scoring the
        # button. Survival is settled by the end-of-session sweep.
        session = next((item for item in data.get("sessions", [])
                        if item.get("id") == match.get("session_id")), None)
        if session is not None:
            session["last_judged_entry_id"] = match["id"]
        if event_id:
            event = next((item for item in data.setdefault("judgment_events", [])
                          if item.get("event_id") == event_id), None)
            if event is not None:
                event.setdefault("steps", {})["ledger"] = {
                    "status": "applied", "result": {"entry_id": match["id"]},
                    "error": None, "updated_at": _iso(_now())}
                if {"ears", "develop", "rating", "ledger"}.issubset(event["steps"]) and \
                        all(event["steps"][name].get("status") == "applied"
                            for name in ("ears", "develop", "rating", "ledger")):
                    event["status"] = "complete"
                event["updated_at"] = _iso(_now())
        _save(data, path)
        return dict(match)


# Label for the soft survival demotion: served but not kept. Neutral rating
# (never a reject, never a ban) consumed as "move to the back" by selection.
OUTCOME_DEMOTION_LABEL = "served-not-kept"


def outcome_rating_ops(entry: dict) -> list[dict]:
    """Rating ops implied by a definitively closed row. Pure: no reads, no
    writes — the caller applies each op once under idempotency key
    ``outcome:<entry_id>``.

    | row                                  | op                                |
    |--------------------------------------|-----------------------------------|
    | keep verdict, absent (terminal drop) | un-gold the source                |
    | abandoned (removed without a word)   | soft demote (back, not banned)    |
    | keep (incl. adopted keeps)           | none — the judge KEEP golded      |
    | drop verdict, unjudged, unaddressable| none                              |

    A later 'dropped' label un-golds through the gold-membership fold, so a
    plain DROP of a golded source lands there without an op of its own."""
    verdict = entry.get("immediate_verdict")
    state = entry.get("terminal_state")
    if state == "keep" or (state in ("drop", "unjudged")
                           and verdict != "keep"):
        # terminal keep: judge KEEP golded already (adopted keeps land here
        # too); drop-by-word and unjudged rows carry no survival signal
        return []
    ops: list[dict] = []
    phrase_id = entry.get("source_phrase_id")
    notes = entry.get("answer_notes")
    if not phrase_id and not notes:
        return []
    comment = ("survival outcome %s after definitive close of entry %s"
               % (state, entry.get("id")))
    if verdict == "keep" and state == "drop":
        # liked, then removed: not durable — un-gold the source
        ops.append({"action": "ungold", "phrase_id": phrase_id,
                    "notes": notes, "comment": comment})
    elif state == "abandoned":
        # served but never kept: to the back, never banned
        ops.append({"action": "demote", "phrase_id": phrase_id,
                    "notes": notes, "comment": comment})
    return ops


def resolve_terminal(entry: dict, present: bool) -> str:
    """Terminal state for one written clip. Pure — the sweep's whole judgment.

    | verdict | in the set | state     |
    |---------|-----------|-----------|
    | keep    | yes       | keep      |  <- the only thing in the numerator
    | keep    | no        | drop      |  he liked it, then removed it: not durable
    | drop    | either    | drop      |  his word wins; deletion is confirm-gated
    | none    | no        | abandoned |  removed without a word
    | none    | yes       | unjudged  |  still sitting there unheard-from

    An explicit DROP stays a drop even when the clip is physically present,
    because deleting is optional and confirm-gated — presence would otherwise
    read a lazy non-deletion as approval.
    """
    verdict = entry.get("immediate_verdict")
    if verdict == "keep":
        return "keep" if present else "drop"
    if verdict == "drop":
        return "drop"
    return "unjudged" if present else "abandoned"


def _inventory(value) -> tuple[dict | None, bool, list[dict], list[str]]:
    """Parse the only close evidence accepted by the new contract."""
    if not isinstance(value, dict):
        return None, False, [], ["legacy bare clip list is incomplete evidence"]
    errors: list[str] = [str(error) for error in value.get("errors", [])] \
        if isinstance(value.get("errors", []), list) else ["inventory errors are invalid"]
    try:
        identity = _identity(value.get("set_identity")) if value.get("set_identity") else None
    except (TypeError, ValueError) as exc:
        identity = None
        errors.append("invalid inventory Set identity: %s" % exc)
    clips = value.get("clips")
    if not isinstance(clips, list) or not all(isinstance(c, dict) for c in clips):
        return identity, False, [], [*errors, "inventory clips are missing or invalid"]
    parsed: list[dict] = []
    seen: set[str] = set()
    for raw in clips:
        try:
            clip = _clip_ref(raw)
        except (TypeError, ValueError, KeyError) as exc:
            errors.append("invalid inventory clip: %s" % exc)
            continue
        key = clip["clip_id"]
        if key in seen:
            errors.append("duplicate inventory clip identity: %s" % key)
        seen.add(key)
        parsed.append(clip)
    complete = value.get("complete") is True and not errors and identity is not None
    return identity, complete, parsed, errors


def _adoption_matches(entry: dict, clips: list[dict], identity: dict | None,
                      session: dict | None = None) -> set[int]:
    chain = _session_chain(session) if session is not None else set()

    def _link_applies(link_set):
        if not link_set:
            return False
        return link_set == (identity or {}).get("set_id") or (
            bool(chain) and link_set in chain)

    matches = set()
    for link in entry.get("adoption_links") or []:
        destination = link.get("destination_ref") if isinstance(link, dict) else None
        link_identity = link.get("set_identity") if isinstance(link, dict) else None
        link_set = link_identity.get("set_id") if isinstance(link_identity, dict) else None
        if not destination or not _link_applies(link_set):
            continue
        for i, clip in enumerate(clips):
            if (clip.get("clip_id") == destination.get("clip_id") and
                    clip.get("track_id") == destination.get("track_id") and
                    clip.get("surface") == destination.get("surface")):
                matches.add(i)
    return matches


def _has_content(clip: dict | None) -> bool:
    return isinstance(clip, dict) and clip.get("answer_notes") is not None


def _clip_key(clip: dict) -> str:
    return clip.get("clip_id")


def _candidate_matches(entry: dict, clips: list[dict], inventory_identity: dict | None,
                       reserved: set[int], session: dict | None = None,
                       canon_clips: list[list] | None = None) -> tuple[str, list[int]]:
    """Find all evidence for an entry; callers resolve collisions globally.

    ``session`` is the entry's session: a Set counts as matching when the
    inventory id equals the entry's Set id or any alias in that session's
    chain (first save, Save-As). Without the chain the check stays strict.
    ``canon_clips`` are the clips' canonical notes, computed once per sweep.
    """
    entry_identity = entry.get("set_identity")
    if not entry_identity or not inventory_identity:
        return "identity_unknown", []
    if entry_identity.get("set_id") != inventory_identity.get("set_id"):
        chain = _session_chain(session) if session is not None else set()
        if not (entry_identity.get("set_id") in chain and
                inventory_identity.get("set_id") in chain):
            return "different_set", []
    ref = entry.get("clip_ref") or {}
    explicit = _adoption_matches(entry, clips, inventory_identity, session=session)
    explicit = {index for index in explicit if index not in reserved}
    if explicit:
        return ("explicit_adoption", sorted(explicit))
    entry_notes = _entry_notes(entry)
    adopted_content: set[int] = set()
    chain = _session_chain(session) if session is not None else set()
    for link in entry.get("adoption_links") or []:
        if not isinstance(link, dict):
            continue
        link_set = (link.get("set_identity") or {}).get("set_id")
        inv_set = (inventory_identity or {}).get("set_id")
        if not link_set or not (link_set == inv_set or
                                (chain and (link_set in chain or inv_set in chain))):
            continue
        destination = link.get("destination_ref") or {}
        if not _has_content(destination):
            continue
        destination_notes = destination.get("answer_notes")
        adopted_content.update(index for index, clip in enumerate(clips)
                              if index not in reserved and _has_content(clip) and
                              content_equal(destination_notes, clip.get("answer_notes")) and
                              clip.get("surface") == destination.get("surface") and
                              clip.get("track") == destination.get("track"))
    if adopted_content:
        return "adoption_content", sorted(adopted_content)
    if entry_identity.get("runtime_epoch") == inventory_identity.get("runtime_epoch"):
        if not ref.get("clip_id"):
            return "identity_unknown", []
        candidates = [i for i, clip in enumerate(clips) if i not in reserved and
                      clip.get("clip_id") == ref.get("clip_id")]
        return ("runtime_id" if candidates else "runtime_id_absent"), candidates
    if entry_notes is None:
        return "cross_runtime_content_unavailable", []
    entry_canon = canonical_notes(entry_notes)
    surface, track = ref.get("surface"), ref.get("track")
    candidates = []
    for i, clip in enumerate(clips):
        if i in reserved:
            continue
        if clip.get("surface") != surface or clip.get("track") != track:
            continue
        if not _has_content(clip):
            continue
        clip_canon = canon_clips[i] if canon_clips is not None \
            else canonical_notes(clip.get("answer_notes"))
        if _canon_equal(entry_canon, clip_canon):
            candidates.append(i)
    return ("unique_content_context" if len(candidates) == 1 else
            "ambiguous_content_context" if candidates else "cross_runtime_unresolved"), candidates


def _reconcile(entry: dict, clips: list[dict], inventory_identity: dict | None,
               used: set[int]) -> tuple[bool | None, str, int | None]:
    """Return (presence, reason, clip-index); None means unresolved."""
    reason, candidates = _candidate_matches(entry, clips, inventory_identity, used)
    if len(candidates) == 1:
        return True, reason, candidates[0]
    if candidates:
        return None, reason, None
    if reason == "runtime_id_absent":
        return False, reason, None
    return None, reason, None


def adopt_entry(entry_id: str, destination_ref: dict, *, set_identity: dict,
                path: str | None = None) -> dict:
    """Record a verified, local adoption link; never searches by name/fingerprint.

    The caller must obtain ``destination_ref`` and ``set_identity`` from the same
    verified inventory/readback. This API is intentionally pure ledger state and
    does not mutate Live. Follow-on lifecycle code can call it after promotion.
    """
    identity = _identity(set_identity)
    destination = _clip_ref(destination_ref)
    path = path or LEDGER_PATH
    with _lock(path):
        data = _load_for_mutation(path)
        entry = next((e for e in data["entries"] if e.get("id") == entry_id), None)
        if entry is None:
            raise ValueError("unknown ledger entry")
        session = next((s for s in data.get("sessions", [])
                        if s.get("id") == entry.get("session_id")), None)
        entry_set = (entry.get("set_identity") or {}).get("set_id")
        if identity["set_id"] != entry_set and \
                identity["set_id"] not in _session_chain(session):
            # The link's Set must be the entry's own or another id the same
            # night has been (first save, Save-As): anything else is a
            # different Set's clip.
            raise ValueError("adoption must use the entry's Set identity")
        stable_key = (identity["set_id"], destination["clip_id"],
                      destination["track_id"], destination["surface"])
        links = entry.setdefault("adoption_links", [])
        existing = next((link for link in links if (
            link.get("set_identity", {}).get("set_id"),
            link.get("destination_ref", {}).get("clip_id"),
            link.get("destination_ref", {}).get("track_id"),
            link.get("destination_ref", {}).get("surface")) == stable_key), None)
        if existing is None:
            link = {"set_identity": identity, "destination_ref": destination,
                    "verified_at": _iso(_now())}
            entry["adoption_links"].append(link)
            data.setdefault("adoptions", []).append({"entry_id": entry_id, **link})
        else:
            link = existing
        _save(data, path)
        return dict(entry)


record_adoption = adopt_entry


def entry_for_clip(clip_ref: dict, set_identity: dict | None = None,
                   path: str | None = None) -> dict | None:
    """Find a tracked suggestion by exact verified runtime identity only."""
    reference = _clip_ref(clip_ref)
    identity = _identity(set_identity) if set_identity else None
    if reference is None or identity is None:
        return None
    data = load(path)
    for entry in reversed(data.get("entries", [])):
        stored = entry.get("clip_ref") or {}
        if not _same_set(entry.get("set_identity"), identity):
            continue
        if all(stored.get(key) == reference.get(key)
               for key in ("clip_id", "track_id", "surface")):
            return dict(entry)
    return None


def reopen_session(session_id: str, *, path: str | None = None) -> dict:
    """Re-open a partially observed session for a later verified inventory."""
    path = path or LEDGER_PATH
    with _lock(path):
        data = _load_for_mutation(path)
        session = next((s for s in data["sessions"] if s.get("id") == session_id), None)
        if session is None:
            raise ValueError("unknown session")
        session["closed_at"] = None
        session["closed_by"] = None
        session["reopened_at"] = _iso(_now())
        _save(data, path)
        return dict(session)


def record_observation(entry_id: str, *, observation: str, present: bool | None,
                       set_identity: dict | None = None, clip_ref: dict | None = None,
                       session_id: str | None = None, observation_key: str | None = None,
                       observed_session: str | None = None,
                       path: str | None = None) -> dict:
    """Record next-session/adoption evidence separately from the close result.

    ``observation`` is ``next_session`` or ``adoption``. This never changes a
    terminal state or the session denominator; it is a later measurement.
    """
    if observation not in ("next_session", "adoption"):
        raise ValueError("observation must be next_session or adoption")
    if present is not None and type(present) is not bool:
        raise TypeError("present must be bool or None")
    identity = _identity(set_identity) if set_identity else None
    reference = _clip_ref(clip_ref) if clip_ref else None
    path = path or LEDGER_PATH
    with _lock(path):
        data = _load_for_mutation(path)
        entry = next((e for e in data["entries"] if e.get("id") == entry_id), None)
        if entry is None:
            raise ValueError("unknown ledger entry")
        if not identity or not entry.get("set_identity"):
            raise ValueError("observation requires verified Set identity")
        if not _same_set(entry.get("set_identity"), identity, runtime=False):
            raise ValueError("observation belongs to a different Set")
        if reference is not None:
            _, matches = _candidate_matches(entry, [reference], identity, set())
            observed = True if matches == [0] else None
        else:
            observed = None
        if present is True and observed is not True:
            raise ValueError("present=True lacks matching verified clip evidence")
        if present is False:
            observed = None
        stable_key = observation_key or "%s:%s:%s" % (
            session_id or "unknown-session", observed_session or identity["runtime_epoch"],
            identity["runtime_epoch"])
        duplicate = next((row for row in data.setdefault("observations", [])
                          if row.get("entry_id") == entry_id and
                          row.get("observation") == observation and
                          row.get("observation_key") == stable_key), None)
        if duplicate is not None:
            return dict(duplicate)
        record = {"id": "o-" + uuid.uuid4().hex[:10], "entry_id": entry_id,
                  "observation": observation, "present": observed,
                  "set_identity": identity, "clip_ref": reference,
                  "observation_key": stable_key, "recorded_at": _iso(_now())}
        data.setdefault("observations", []).append(record)
        _save(data, path)
        return dict(record)


def refresh_observations(present, session_id: str | None = None,
                         path: str | None = None) -> dict:
    """Reconcile a current inventory into independent next-session evidence.

    This intentionally does not alter terminal states or the closed session's
    historical sweep. Only complete, identity-bearing inventories produce
    observations; repeated refreshes use the observed-session key as a dedupe
    boundary.
    """
    path = path or LEDGER_PATH
    identity, complete, clips, errors = _inventory(present)
    if not complete or identity is None:
        return {"ok": True, "recorded": 0, "coverage": {
            "complete": complete, "inventory_errors": errors,
            "observed_clips": len(clips), "unresolved": True}}
    with _lock(path):
        data = _load_for_mutation(path)
        if session_id:
            target = next((s for s in data.get("sessions", [])
                           if s.get("id") == session_id and s.get("closed_at") and
                           _same_set(s.get("set_identity"), identity, runtime=False)), None)
        else:
            # A later unrelated Set must not hide the latest closed session
            # belonging to the Set being refreshed.
            target = next((s for s in reversed(data.get("sessions", []))
                           if s.get("closed_at") and
                           _same_set(s.get("set_identity"), identity, runtime=False)), None)
        if target is None:
            return {"ok": False, "recorded": 0,
                    "error": "no closed session for current Set"}
        entries = [entry for entry in data.get("entries", [])
                   if entry.get("session_id") == target.get("id") and
                   not entry.get("superseded_by")]
        observed_session = str(present.get("observed_session_id") or
                               identity.get("runtime_epoch"))
        later_runtime = (identity.get("runtime_epoch") !=
                          (target.get("set_identity") or {}).get("runtime_epoch"))
        later_session = bool(present.get("observed_session_id")) and \
            observed_session != target.get("id")
        eligible = later_runtime or later_session
        if not eligible:
            return {"ok": True, "recorded": 0,
                    "coverage": {"complete": True, "inventory_errors": errors,
                                 "observed_clips": len(clips), "unresolved": len(entries),
                                 "unresolved_entries": [e["id"] for e in entries],
                                 "next_session_eligible": False,
                                 "reason": "no later verified session or runtime epoch",
                                 "target_session_id": target["id"],
                                 "observed_session_id": observed_session}}
        recorded = 0
        unresolved: list[dict] = []
        candidates: dict[str, tuple[str, list[int]]] = {}
        claims: dict[int, list[str]] = {}
        for entry in entries:
            reason, indexes = _candidate_matches(entry, clips, identity, set())
            candidates[entry["id"]] = (reason, indexes)
            if len(indexes) == 1:
                claims.setdefault(indexes[0], []).append(entry["id"])
        for entry in entries:
            reason, indexes = candidates[entry["id"]]
            clip = None
            if len(indexes) > 1:
                unresolved.append({"entry_id": entry["id"], "reason": reason})
                continue
            if len(indexes) == 1 and len(claims.get(indexes[0], [])) > 1:
                unresolved.append({"entry_id": entry["id"], "reason": "shared_clip_collision"})
                continue
            if len(indexes) == 1:
                clip = clips[indexes[0]]
                observation = ("adoption" if reason.startswith("explicit_adoption")
                               else "next_session")
                observed = True
            elif reason == "runtime_id_absent" and eligible:
                observation = "next_session"
                observed = False
            else:
                unresolved.append({"entry_id": entry["id"], "reason": reason})
                continue
            stable_key = "%s:%s:%s" % (target["id"], observed_session, observation)
            duplicate = next((row for row in data.setdefault("observations", [])
                              if row.get("entry_id") == entry["id"] and
                              row.get("observation") == observation and
                              row.get("observation_key") == stable_key), None)
            if duplicate is not None:
                continue
            data["observations"].append({
                "id": "o-" + uuid.uuid4().hex[:10], "entry_id": entry["id"],
                "observation": observation, "present": observed,
                "set_identity": identity, "clip_ref": dict(clip) if clip else None,
                "observation_key": stable_key, "observed_session": observed_session,
                "recorded_at": _iso(_now()),
            })
            recorded += 1
        _save(data, path)
    return {"ok": True, "recorded": recorded,
            "coverage": {"complete": True, "inventory_errors": errors,
                         "observed_clips": len(clips), "unresolved": len(unresolved),
                         "unresolved_entries": unresolved,
                         "next_session_eligible": True,
                         "target_session_id": target["id"],
                         "observed_session_id": observed_session}}


def observation_report(path: str | None = None) -> dict:
    """Summarize independent post-close observations without re-scoring history."""
    data = load(path)
    rows = data.get("observations", [])
    by_type = {kind: {"observed": 0, "present": 0, "absent": 0, "unresolved": 0}
               for kind in ("next_session", "adoption")}
    for row in rows:
        cell = by_type.setdefault(row.get("observation"), {"observed": 0, "present": 0,
                                                            "absent": 0, "unresolved": 0})
        cell["observed"] += 1
        key = "present" if row.get("present") is True else "absent" if row.get("present") is False else "unresolved"
        cell[key] += 1
    return {"observations": len(rows), "by_type": by_type}


def _sweep_targets(data: dict, session_id: str | None,
                   inventory_identity: dict | None) -> tuple[list[dict], str | None]:
    """Sessions one inventory may sweep: the explicit id, or every non-closed
    session whose Set/alias chain contains the inventoried Set, oldest first."""
    if session_id:
        found = [s for s in data["sessions"] if s.get("id") == session_id]
        if not found:
            return found, "unknown session %r" % session_id
        if inventory_identity is not None and \
                inventory_identity.get("set_id") not in _session_chain(found[0]):
            # An explicit sweep names the night being scored: evidence from
            # a Set outside its chain must never end, seal, or unlock it.
            return [], ("inventory set %s is not in session %s's chain — "
                        "refusing to sweep another Set's night"
                        % (inventory_identity.get("set_id"), session_id))
        return found, None
    open_sessions = [s for s in data["sessions"] if not s.get("closed_at")]
    if not open_sessions:
        return [], "no open session to close"
    if inventory_identity is None:
        # Legacy bare/partial evidence names no Set, so it can match nothing;
        # keep sweeping the newest open session, which resolves nothing.
        return [open_sessions[-1]], None
    matched = [s for s in open_sessions
               if inventory_identity.get("set_id") in _session_chain(s)]
    if not matched:
        return [], "no open session to close for set %s" % inventory_identity.get("set_id")
    return matched, None


def _scoreable(entry: dict) -> bool:
    """Only a row that knows its Set can be scored by evidence about a Set."""
    return bool(entry.get("set_identity"))


def _verdict_at_evidence(entry: dict) -> str | None:
    """The verdict as it stood when the night's evidence was collected.

    A late verdict is accepted and stored but scores as verdict-less: it must
    not reshape a night whose evidence was already sealed.
    """
    return None if entry.get("late") else entry.get("immediate_verdict")


def _is_late(entry: dict, session: dict | None) -> bool:
    """Late = judged after the first sweep attempt sealed the session, or on
    an already-closed session."""
    if entry.get("late"):
        return True
    judged_at = entry.get("judged_at")
    if not judged_at:
        return False
    sealed = (session or {}).get("swept_at") or (session or {}).get("closed_at")
    return _iso_after(judged_at, sealed)


def _state_of(entry: dict) -> str | None:
    """Terminal if the night closed definitively, else the live provisional."""
    return entry.get("terminal_state") or entry.get("provisional_state")


def _reserve_sticky_claims(data: dict, clips: list[dict],
                           inventory_identity: dict | None, reserved: set[int],
                           canon_clips: list[list] | None = None,
                           clip_keys: list[str | None] | None = None) -> None:
    """Clips already claimed by sticky presence (this sweep or an earlier one)
    are excluded from every later claim — a second unresolved row must not
    inherit the first match.

    Only rows whose session chain contains the inventoried Set can reserve:
    history from unrelated Sets never touches this sweep. The one crossing
    that is NOT unrelated: a night swept while the Set was unsaved keeps its
    claims after the first save, because ``unsaved:<epoch>`` in its chain IS
    the saved Set of the same runtime. Track and surface are compared before
    any content, and content keys short-circuit the equality test (same key
    implies equal content); the gated ``content_equal`` fallback keeps the
    decision exact.
    """
    sessions = {s.get("id"): s for s in data.get("sessions", [])}
    inv_set = (inventory_identity or {}).get("set_id")
    inv_epoch = (inventory_identity or {}).get("runtime_epoch")
    unsaved_of_this_run = "unsaved:%s" % inv_epoch if inv_epoch else None
    for entry in data["entries"]:
        if entry.get("superseded_by"):
            continue
        chain = _session_chain(sessions.get(entry.get("session_id")))
        if inv_set not in chain and unsaved_of_this_run not in chain:
            continue
        matched_refs = list((entry.get("presence") or {}).get("matched_clips") or [])
        if entry.get("matched_clip") and entry["matched_clip"] not in matched_refs:
            matched_refs.append(entry["matched_clip"])
        matched_epoch = (entry.get("matched_inventory_identity") or {}).get("runtime_epoch")
        same_runtime_set = (inventory_identity is not None and
                            matched_epoch == inventory_identity.get("runtime_epoch"))
        for matched in matched_refs:
            _reserve_one_claim(matched, same_runtime_set, clips, reserved,
                               canon_clips, clip_keys)


def _reserve_one_claim(matched: dict, same_runtime_set: bool, clips: list[dict],
                       reserved: set[int], canon_clips: list[list] | None,
                       clip_keys: list[str | None] | None) -> None:
    """Reserve the first clip matching one sticky claim, if any.

    Track and surface are compared before any content; equal content keys
    short-circuit the equality test (same key implies equal content), with
    the gated equality fallback keeping the decision exact.
    """
    key = _clip_key(matched)
    track, surface = matched.get("track"), matched.get("surface")
    ref_canon = canonical_notes(matched.get("answer_notes")) \
        if _has_content(matched) else None
    ref_key = _key_of_canon(ref_canon) \
        if ref_canon is not None and clip_keys is not None else None
    for index, clip in enumerate(clips):
        if index in reserved:
            continue
        if same_runtime_set and key is not None and _clip_key(clip) == key:
            reserved.add(index)
            return
        if ref_canon is None or not _has_content(clip):
            continue
        if clip.get("track") != track or clip.get("surface") != surface:
            continue
        if ref_key is not None and clip_keys is not None and \
                clip_keys[index] == ref_key:
            reserved.add(index)
            return
        if canon_clips is not None:
            if not _canon_equal(ref_canon, canon_clips[index]):
                continue
        elif not content_equal(matched.get("answer_notes"),
                               clip.get("answer_notes")):
            continue
        reserved.add(index)
        return


def _write_presence(entry: dict, state: str, reason: str, indexes: list[int],
                    clips: list[dict], inventory_identity: dict | None,
                    now_iso: str) -> None:
    """Sticky presence: only the FIRST complete sweep after the night ended
    writes it. Later sweeps never overwrite it — later observations belong to
    ``refresh_observations``."""
    if entry.get("presence"):
        return
    entry["presence"] = {"state": state, "match": reason, "swept_at": now_iso,
                         "matched_clips": [dict(clips[i]) for i in indexes],
                         "adoption": None}
    if indexes:
        entry["matched_clip"] = dict(clips[indexes[0]])
        entry["matched_inventory_identity"] = dict(inventory_identity or {})


def _infer_outcome(reason: str, indexes: list[int], claims: dict[int, int],
                   reserved: set[int]) -> tuple[str, str, list[int]]:
    """Map one row's raw candidates to (kind, reason, indexes).

    kind: 'presence' (matched clip), 'absent' (verified absent) or
    'unresolved'. Collisions never inherit another row's clip.
    """
    if len(indexes) > 1:
        return ("unresolved",
                "ambiguous_content_context" if reason != "runtime_id"
                else "ambiguous_runtime_id", [])
    if len(indexes) == 1:
        index = indexes[0]
        if index in reserved or claims.get(index, 0) > 1:
            return "unresolved", "shared_clip_collision", []
        return "presence", reason, [index]
    if reason == "runtime_id_absent":
        return "absent", reason, []
    return "unresolved", reason, []


# Adoption shape tiers, weakest to strongest: a consolidated loop is a whole
# copy of the answer, a contained copy sits inside a bigger clip, a near copy
# is a heavy edit. Only the top two may ever auto-adopt.
_TIER_RANK = {"near_copy": 1, "contained_copy": 2, "whole_copy": 3}
# Primary outcomes that leave room for adoption: the row's own clip is gone
# (or could not be matched), so a copy elsewhere may carry the survival.
_ADOPTABLE_REASONS = ("runtime_id_absent", "cross_runtime_unresolved",
                      "cross_runtime_content_unavailable")


def _match_onsets(e: list[dict], k: list[dict], t: int, d: float,
                    index: dict[int, list] | None = None) -> tuple[set[int], int]:
    """Greedy onset+pitch match of E into K under (transposition t, offset d).

    Duration and velocity are deliberately ignored — copies keep the shape.
    With the tier's pitch index each note scans only same-pitch candidates
    near its onset instead of the whole clip.
    """
    tolerance = _pp_codec.READBACK_TIME_TOLERANCE_BEATS + 1e-9
    used: set[int] = set()
    matched = 0
    if index is None:
        for note in e:
            want_pitch = note["pitch"] + t
            want_start = note["start"] + d
            for pos, other in enumerate(k):
                if pos in used:
                    continue
                if other["pitch"] == want_pitch and \
                        abs(other["start"] - want_start) <= tolerance:
                    used.add(pos)
                    matched += 1
                    break
        return used, matched
    tol_ms = tolerance * 1000.0
    for note in e:
        bucket = index.get(note["pitch"] + t)
        if not bucket:
            continue
        want_ms = (note["start"] + d) * 1000.0
        lo = bisect.bisect_left(bucket, (want_ms - tol_ms, -1.0, -1))
        end = want_ms + tol_ms
        for ms, _, pos in bucket[lo:]:
            if ms > end:
                break
            if pos not in used:
                used.add(pos)
                matched += 1
                break
    return used, matched


def _clip_scan(canon: list[dict]) -> tuple[set[int], dict[int, list]]:
    """Per-clip matching structures, built once per sweep and shared by
    every row: pitch classes plus the pitch-indexed millisecond starts."""
    index: dict[int, list] = {}
    for pos, note in enumerate(canon):
        index.setdefault(note["pitch"], []).append(
            (round(note["start"] * 1000), note["start"], pos))
    for bucket in index.values():
        bucket.sort()
    return ({note["pitch"] % 12 for note in canon}, index)


def _adoption_tier(e: list[dict], k: list[dict], drum: bool,
                   scan: tuple[set[int], dict[int, list]] | None = None,
                   ) -> tuple[str | None, int]:
    """Best shape class of clip K versus the row's notes E, with its
    transposition. Drums may not transpose; any other row may shift by a
    multiple of 12 (up to ±36) or by anything within ±24 (candidate only).
    Transpositions anchor on the first TWO notes, so a copy missing either
    end still matches at its true transposition. A whole copy needs ONE
    transposition covering the whole clip — a union of fragments at mixed
    transpositions is never whole.

    K is indexed by (pitch, millisecond start): per transposition the
    alignment offsets are voted, and only offsets with enough votes to
    reach a tier are verified with the exact matcher. Skipped offsets can
    never reach a tier (every matched pair votes its offset), so the
    result is the same as trying every anchor — at a fraction of the cost.
    """
    n_e = len(e)
    best: tuple[int, str | None, int] = (0, None, 0)

    def better(tier: str, t: int) -> None:
        nonlocal best
        if _TIER_RANK[tier] > best[0]:
            best = (_TIER_RANK[tier], tier, t)

    def evaluate(t: int, d: float) -> bool:
        """Verify one alignment; True when the unbeatable whole copy lands."""
        used, matched = _match_onsets(e, k, t, d, index)
        if matched < 2:
            return False
        window_start, window_end = e[0]["start"] + d, e[-1]["start"] + d
        tolerance = _pp_codec.READBACK_TIME_TOLERANCE_BEATS + 1e-9
        if matched == n_e:
            covered = full_by_t.setdefault(t, set())
            covered.update(used)
            if len(covered) == len(k):
                better("whole_copy", t)
                return True
            if best[0] < _TIER_RANK["contained_copy"]:
                inside = [i for i, note in enumerate(k) if i not in used and
                          window_start - tolerance <= note["start"] <= window_end + tolerance]
                outside = [i for i, note in enumerate(k) if i not in used and
                           (note["start"] < window_start - tolerance or
                            note["start"] > window_end + tolerance)]
                if not inside and outside:
                    better("contained_copy", t)
        # Near copies are a long-answer game: eight notes minimum, three
        # quarters of the answer matched, at a neutral transposition —
        # anything looser fires on random in-key material. Once near (or
        # better) is found, further near checks cannot win.
        if best[0] < _TIER_RANK["near_copy"] and t % 12 == 0 and n_e >= 8 and \
                matched >= max(4, -(-75 * n_e // 100)):
            extras = sum(1 for i, note in enumerate(k) if i not in used and
                         window_start - tolerance <= note["start"] <= window_end + tolerance)
            if extras <= max(1, (4 * n_e) // 10):
                better("near_copy", t)
        return False

    # Transposition shifts every pitch class by the same amount: E-notes
    # whose shifted class is absent from K can never match under that
    # transposition, so cap the reachable tier per t up front.
    if scan is None:
        scan = _clip_scan(k)
    k_classes, index = scan
    full_by_t: dict[int, set[int]] = {}
    base_pitches = [e[0]["pitch"]] + ([e[1]["pitch"]] if n_e > 1 else [])
    allowed_t: set[int] = set()
    for key_pitch in index:
        for base_pitch in base_pitches:
            t = key_pitch - base_pitch
            if drum and t != 0:
                continue
            if abs(t) <= 24 or (t % 12 == 0 and abs(t) <= 36):
                allowed_t.add(t)
    e_pitches = [note["pitch"] for note in e]
    e_starts = [note["start"] for note in e]
    e_mss = [round(start * 1000) for start in e_starts]
    index_get = index.get
    # Neutral transpositions first: they win ties the way the old
    # anchor-ordered scan did, and a whole copy ends the search at once —
    # no higher tier exists.
    ordered_t = sorted((abs(t), t) for t in allowed_t)
    for _, t in ordered_t:
        near_eligible = t % 12 == 0 and n_e >= 8
        floor = max(4, -(-75 * n_e // 100)) if near_eligible else n_e
        votes: dict[int, list[float]] = {}
        pairs = 0
        touched = 0
        for pitch, e_ms, e_start in zip(e_pitches, e_mss, e_starts):
            bucket = index_get(pitch + t)
            if not bucket:
                continue
            touched += 1
            for k_ms, k_start, _ in bucket:
                votes.setdefault(k_ms - e_ms, []).append(k_start - e_start)
                pairs += 1
        # Every matched pair votes once: fewer pairs — or fewer touched
        # E-notes — than the floor means no tier is reachable under t.
        if pairs < floor or touched < floor:
            continue
        # Cluster nearby offsets (readback jitter spreads true votes over a
        # few milliseconds); a cluster smaller than the floor can never
        # reach any tier, so only qualifying clusters are verified — each
        # at one exact offset per millisecond value it holds.
        for cluster in _qualifying_clusters(votes, floor):
            seen: set[int] = set()
            for vote in cluster:
                if vote in seen:
                    continue
                seen.add(vote)
                if evaluate(t, votes[vote][0]):
                    return best[1], best[2]
    return best[1], best[2]


def _qualifying_clusters(votes: dict[int, list[float]],
                         floor: int) -> list[list[int]]:
    """Millisecond-offset clusters (chained within 4 ms) holding at least
    ``floor`` votes.

    Votes for one true alignment spread a couple of milliseconds through
    rounding and readback jitter; distinct alignments sit further apart.
    Merging can only add verifications, and a sub-floor cluster can never
    reach any tier (every matched pair votes its offset).
    """
    out: list[list[int]] = []
    members: list[int] = []
    size = 0
    prev: int | None = None
    for vote in sorted(votes):
        if prev is not None and vote - prev > 4:
            if size >= floor:
                out.append(members)
            members = []
            size = 0
        members.append(vote)
        size += len(votes[vote])
        prev = vote
    if size >= floor:
        out.append(members)
    return out



def _adoption_candidates(entry: dict, clips: list[dict],
                         reserved: set[int],
                         canon_clips: list[list] | None = None,
                         scan_clips: list | None = None) -> list[dict]:
    """Clips that look like copies of the row's content, with tier and
    transposition. Clips reserved by a primary match are never candidates —
    that clip already belongs to another row's own evidence. Destination
    tracks are classified by the caller (JAM copy / adopted / sidekick
    scaffolding excluded)."""
    e = canonical_notes(_entry_notes(entry))
    if len(e) < 2:
        return []
    drum = entry.get("role") == "drum"
    out: list[dict] = []
    for index, clip in enumerate(clips):
        if index in reserved or not _has_content(clip):
            continue
        k = canon_clips[index] if canon_clips is not None \
            else canonical_notes(clip.get("answer_notes"))
        if len(k) < 2:
            continue
        scan = scan_clips[index] if scan_clips is not None else None
        tier, transposition = _adoption_tier(e, k, drum, scan)
        if tier is not None:
            out.append({"index": index, "tier": tier,
                        "transposition": transposition})
    return out


def _distinctive_answer(notes: list[dict]) -> bool:
    """Whether an answer is too plain for a copy to claim on its own.

    A pedal tone, a shorter motif on repeat, or a figure with a single
    inter-onset interval could coincide with his pre-existing clips by
    accident — those stay candidates for the owner to confirm.
    """
    pitches = [n["pitch"] for n in notes]
    if len(set(pitches)) < 3:
        return False
    count = len(pitches)
    for period in range(1, count // 2 + 1):
        if all(pitch == pitches[i % period] for i, pitch in enumerate(pitches)):
            return False
    onsets = sorted({round(n["start"], 3) for n in notes})
    intervals = {round(later - earlier, 3)
                 for earlier, later in zip(onsets, onsets[1:])
                 if later > earlier}
    return len(intervals) >= 2


def _repeats_short_cell(notes: list[dict], max_beats: float = 1.0) -> bool:
    """Whether the pattern repeats a cell no longer than ``max_beats``.

    Straight 8th hats, four-on-the-floor kicks, any one-beat loop — these
    are the generic grooves his songs share by coincidence, so a drum row
    built from one never adopts on its own. A cell length is only tested
    where an onset actually sits (a repetition anchors on its own grid),
    and the trailing partial cell may be a prefix of the cell.
    """
    tolerance = _pp_codec.READBACK_TIME_TOLERANCE_BEATS + 1e-9
    onsets = sorted({round(n["start"], 4) for n in notes})
    if not onsets:
        return False
    end = onsets[-1]
    for period in onsets[1:]:
        if period > max_beats + tolerance:
            break
        cells: dict[int, list[tuple[int, float]]] = {}
        for note in notes:
            position = note["start"] / period
            index = round(position)
            if abs(position - index) * period <= tolerance:
                offset = 0.0
            else:
                index = int(position)
                offset = note["start"] - index * period
            cells.setdefault(index, []).append(
                (note["pitch"], round(offset, 3)))
        if 0 not in cells or sorted(cells) != list(range(max(cells) + 1)):
            continue  # gaps: this period is not the pattern's grid
        first = sorted(cells[0])
        last_index = max(cells)
        repeats = True
        for index in range(1, last_index + 1):
            remaining = end - index * period
            if remaining >= period - tolerance:
                expected = first
            else:
                # trailing partial cell: only the prefix that fits
                expected = [item for item in first
                            if item[1] <= remaining + tolerance]
            if sorted(cells[index]) != expected:
                repeats = False
                break
        if repeats:
            return True
    return False


def _auto_adoptable(entry: dict, match: dict) -> bool:
    """A copy may only adopt its row on its own: a whole/contained copy at a
    musically neutral transposition (multiple of 12; none for drums), of
    distinctive content, from a generating gear. Everything else — retrieval
    rows could coincide with his pre-existing clips — is a candidate for the
    owner to confirm."""
    tier = match["tier"]
    if tier not in ("whole_copy", "contained_copy"):
        return False
    t = match["transposition"]
    notes = canonical_notes(_entry_notes(entry))
    if entry.get("role") == "drum":
        # A drum part reads as his beat only with two voices going and
        # six DISTINCT onsets behind them (grace notes at one onset count
        # once) — and only when the pattern is not a cell of a beat or
        # less repeating: straight 8ths and four-on-the-floor are grooves
        # any song shares, so they stay candidates for the owner.
        onsets = {round(n["start"], 3) for n in notes}
        if t != 0 or len(set(n["pitch"] for n in notes)) < 2 \
                or len(onsets) < 6 or _repeats_short_cell(notes):
            return False
    elif t % 12 != 0 or abs(t) > 36 or not _distinctive_answer(notes):
        return False
    if tier == "whole_copy":
        if len(notes) < 4:
            return False
    elif len(notes) < 6 or len({round(n["start"], 3) for n in notes}) < 3:
        return False
    # Generating gears only: gear 2 is retrieval and gear 3 is retrieval +
    # surgery, so their copies could coincide with his pre-existing clips.
    return entry.get("gear") in (4, 5, 6)


def _resolve_adoptions(queue: list[tuple[dict, str]], clips: list[dict],
                       inventory_identity: dict | None, reserved: set[int],
                       now_iso: str,
                       canon_clips: list[list] | None = None,
                       scan_clips: list | None = None) -> None:
    """Apply copy-adoption to the rows whose own clip the sweep could not
    find: top-tier uncontested copies adopt their row (or count as kept JAM
    copies); anything else that matched becomes a candidate or a collision
    and the row stays unresolved for the owner to confirm."""
    matches: dict[str, list[dict]] = {}
    claimants: dict[int, list[tuple[dict, dict]]] = {}
    for entry, _reason in queue:
        kept = []
        for match in _adoption_candidates(entry, clips, reserved, canon_clips,
                                          scan_clips):
            track = str(clips[match["index"]].get("track") or "")
            if track != "SIDEKICK JAM" and track.startswith("SIDEKICK"):
                continue  # BOOTH/AUD/EARS are sidekick scaffolding, not the song
            kept.append(match)
        if kept:
            matches[entry["id"]] = kept
            for match in kept:
                claimants.setdefault(match["index"], []).append((entry, match))
    won: dict[str, list[dict]] = {}
    collided: dict[str, list[dict]] = {}
    for index, claims in claimants.items():
        top = max(_TIER_RANK[match["tier"]] for _entry, match in claims)
        finalists = [(entry, match) for entry, match in claims
                     if _TIER_RANK[match["tier"]] == top]
        if len(finalists) > 1:
            for entry, match in finalists:
                collided.setdefault(entry["id"], []).append(match)
        else:
            won.setdefault(finalists[0][0]["id"], []).append(finalists[0][1])
    for entry, primary_reason in queue:
        verdict = _verdict_at_evidence(entry)
        entry_matches = matches.get(entry["id"], [])
        auto = [m for m in won.get(entry["id"], []) if _auto_adoptable(entry, m)]
        auto_indexes = {m["index"] for m in auto}
        if auto:
            # an uncontested copy of a generating gear's answer IS the
            # survival, wherever he put it
            state = ("adopted" if any(
                clips[m["index"]].get("track") != "SIDEKICK JAM" for m in auto)
                else "jam_copy")
            tier = max(auto, key=lambda m: _TIER_RANK[m["tier"]])["tier"]
            indexes = sorted(auto_indexes)
            # An auto-adoption win owns its clips: reserve them so no later
            # claim in this pass can inherit the same evidence.
            reserved.update(indexes)
            _write_presence(entry, state, tier, indexes, clips,
                            inventory_identity, now_iso)
            entry["presence"]["adoption"] = {
                "kind": "auto", "tier": tier,
                "transpositions": sorted({m["transposition"] for m in auto})}
            entry["resolution_match"] = tier
            entry.pop("resolution_reason", None)
            entry["provisional_state"] = resolve_terminal(
                {"immediate_verdict": verdict}, True)
            entry.pop("adoption_candidates", None)
            continue
        candidate_matches = [m for m in entry_matches if m["index"] not in auto_indexes]
        if candidate_matches:
            entry["adoption_candidates"] = [
                {"clip_id": clips[m["index"]].get("clip_id"),
                 "track": clips[m["index"]].get("track"),
                 "tier": m["tier"], "transposition": m["transposition"]}
                for m in candidate_matches]
            entry["resolution_reason"] = ("adoption_collision" if entry["id"] in collided
                                          else "adoption_candidate")
            entry.pop("resolution_match", None)
            entry.pop("provisional_state", None)
            if primary_reason == "runtime_id_absent":
                # the row's own clip is verified gone; only the adoption
                # decision is still open (attest not_adopted to close it)
                _write_presence(entry, "absent", primary_reason, [], clips,
                                inventory_identity, now_iso)
                entry["presence"]["adoption"] = {"kind": "candidate"}
            continue
        # nothing adoptable: the primary outcome stands on its own
        entry.pop("adoption_candidates", None)
        if primary_reason == "runtime_id_absent":
            _write_presence(entry, "absent", primary_reason, [], clips,
                            inventory_identity, now_iso)
            entry["resolution_match"] = primary_reason
            entry.pop("resolution_reason", None)
            entry["provisional_state"] = resolve_terminal(
                {"immediate_verdict": verdict}, False)
        else:
            entry["resolution_reason"] = primary_reason
            entry.pop("resolution_match", None)
            entry.pop("provisional_state", None)


def _sweep_primary(session: dict, rows: list[dict], clips: list[dict],
                   inventory_identity: dict | None, complete: bool,
                   reserved: set[int], now: float,
                   canon_clips: list[list] | None = None) -> list[tuple[dict, str]]:
    """Phase one of a sweep: primary matching for one session.

    DROP rows resolve without presence. Every other row needs presence
    evidence — matched (present/adopted) or verified absent. Primary matches
    claim their clips in the shared ``reserved`` set at once, so a later
    session's own clip is never available to an earlier session's adoption
    scan. Rows whose own clip is gone are returned for the single global
    adoption pass; nothing here adopts.
    """
    now_iso = _iso(now)
    # Only a COMPLETE inventory may end or seal the night it scores. A
    # partial (or identity-less) inventory records coverage only: later
    # verdicts are not late, and later writes still join.
    may_seal = bool(complete and session.get("set_identity"))
    if may_seal and not session.get("swept_at"):
        # The first sealing attempt fixes the evidence: verdicts that arrive
        # afterwards are late and never reshape the score.
        session["swept_at"] = now_iso
    if complete and session.get("set_identity"):
        session["swept_complete"] = True
    if may_seal and not session.get("ended_at"):
        # A sealing sweep ends the night it scores; no later write joins it.
        _end_session(session, now, "sweep")
    pending = [e for e in rows if _needs_sweep(e)]

    raw: dict[str, tuple[str, list[int]]] = {}
    for entry in pending:
        if not complete and not entry.get("presence"):
            raw[entry["id"]] = ("inventory_incomplete", [])
            continue
        raw[entry["id"]] = _candidate_matches(entry, clips, inventory_identity,
                                              set(), session=session,
                                              canon_clips=canon_clips)

    # Explicit adoption links always beat inferred matches, across rows and
    # sessions: claim their destinations first.
    outcomes: dict[str, tuple[str, str, list[int]]] = {}
    for entry in pending:
        reason, indexes = raw[entry["id"]]
        if (complete and _scoreable(entry) and indexes and
                reason in ("explicit_adoption", "adoption_content")):
            usable = sorted(i for i in indexes if i not in reserved)
            if usable:
                outcomes[entry["id"]] = ("adopted", reason, usable)
                reserved.update(usable)
            else:
                outcomes[entry["id"]] = ("unresolved", "shared_clip_collision", [])

    claims: dict[int, int] = {}
    for entry in pending:
        if entry["id"] in outcomes:
            continue
        for index in raw[entry["id"]][1]:
            claims[index] = claims.get(index, 0) + 1

    # Alias continuity: an id-only match that reaches the inventoried Set
    # through the session's alias chain must also carry the row's own
    # content, and at least one row of the session must be confirmed present
    # by id AND content before any absence-based resolution may stand —
    # never fabricate drops from a Set the sweep cannot prove it saw.
    aliased = bool(inventory_identity and session.get("set_identity") and
                   inventory_identity.get("set_id") !=
                   session["set_identity"].get("set_id"))
    confirmed = 0
    for entry in pending:
        if entry["id"] in outcomes or not _scoreable(entry):
            continue
        reason, indexes = raw[entry["id"]]
        if reason != "runtime_id" or len(indexes) != 1:
            continue
        entry_notes = _entry_notes(entry)
        clip_notes = clips[indexes[0]].get("answer_notes")
        if entry_notes is None or clip_notes is None or \
                not content_equal(entry_notes, clip_notes):
            # A runtime-id match is only confirmed by equal content — with
            # no content on either side, or a mismatch, it stays unconfirmed
            # once the night has moved to another id of the same Set.
            if entry.get("set_identity", {}).get("set_id") != \
                    (inventory_identity or {}).get("set_id"):
                raw[entry["id"]] = ("alias_unconfirmed", [])
            continue
        confirmed += 1
    if aliased and not confirmed:
        for entry in pending:
            if raw[entry["id"]][0] == "runtime_id_absent":
                raw[entry["id"]] = ("alias_unconfirmed", [])

    adopt_queue: list[tuple[dict, str]] = []
    for entry in pending:
        if not _scoreable(entry):
            # Identity-less rows are unscoreable by any sweep; attestation is
            # their only path, and they never block the verified rate.
            entry["resolution_reason"] = "identity_unknown"
            entry.pop("resolution_match", None)
            entry.pop("provisional_state", None)
            continue
        verdict = _verdict_at_evidence(entry)
        if entry["id"] in outcomes:
            kind, reason, indexes = outcomes[entry["id"]]
            if kind != "adopted":
                entry["resolution_reason"] = reason
                entry.pop("resolution_match", None)
                entry.pop("provisional_state", None)
                continue
            # an explicit adoption link is the owner's decision, not an
            # observation: it may upgrade sticky absence evidence and closes
            # any open candidate question — but it never pops a sticky
            # present presence: the row's own clip already proved survival
            if (entry.get("presence") or {}).get("state") not in (
                    None, "absent"):
                entry["resolution_match"] = reason
                entry.pop("resolution_reason", None)
                entry["provisional_state"] = resolve_terminal(
                    {"immediate_verdict": verdict}, True)
                continue
            entry.pop("presence", None)
            entry.pop("adoption_candidates", None)
            _write_presence(entry, "adopted", reason, indexes, clips,
                            inventory_identity, now_iso)
            reserved.update(indexes)
            entry["resolution_match"] = reason
            entry.pop("resolution_reason", None)
            entry["provisional_state"] = resolve_terminal(
                {"immediate_verdict": verdict}, True)
            continue
        if entry.get("adoption_candidates"):
            # a copy decision is still open for the owner; the row stays
            # unresolved no matter what sticky evidence it already carries
            entry.pop("resolution_match", None)
            entry.pop("provisional_state", None)
            continue
        if entry.get("presence"):
            # Sticky presence from an earlier complete sweep is the evidence;
            # this sweep only refreshes the live provisional view.
            presence = entry["presence"]
            entry["provisional_state"] = resolve_terminal(
                {"immediate_verdict": verdict}, presence.get("state") != "absent")
            continue
        reason, rindexes = raw[entry["id"]]
        kind, reason, indexes = _infer_outcome(reason, rindexes, claims, reserved)
        if kind == "presence":
            _write_presence(entry, "present", reason, indexes, clips,
                            inventory_identity, now_iso)
            reserved.update(indexes)
            entry["resolution_match"] = reason
            entry.pop("resolution_reason", None)
            entry.pop("adoption_candidates", None)
            entry["provisional_state"] = resolve_terminal(
                {"immediate_verdict": verdict}, True)
        elif kind == "absent":
            if verdict != "drop":
                # the row's own clip is gone; a copy elsewhere may carry the
                # survival, so adoption scans before absence is final
                adopt_queue.append((entry, reason))
                continue
            _write_presence(entry, "absent", reason, [], clips,
                            inventory_identity, now_iso)
            entry["resolution_match"] = reason
            entry.pop("resolution_reason", None)
            entry.pop("adoption_candidates", None)
            entry["provisional_state"] = resolve_terminal(
                {"immediate_verdict": verdict}, False)
        elif verdict == "drop":
            # His word wins; presence is only an observation beside it.
            entry["resolution_match"] = reason
            entry.pop("resolution_reason", None)
            entry.pop("adoption_candidates", None)
            entry["provisional_state"] = "drop"
        elif reason in _ADOPTABLE_REASONS:
            # no cross-runtime match of its own — a copy may still survive
            adopt_queue.append((entry, reason))
        else:
            entry["resolution_reason"] = reason
            entry.pop("resolution_match", None)
            entry.pop("provisional_state", None)
    return adopt_queue


def _sweep_finalize(session: dict, rows: list[dict], complete: bool,
                    now: float) -> None:
    """Phase two of a sweep: land terminal states once adoption has run.

    Presence and a provisional state were written while the rate was not
    definitive; terminal states land on all rows at once the moment it is.
    """
    pending = [e for e in rows if _needs_sweep(e)]
    # Definitive (the rate rule): the night ended, a complete inventory was
    # applied, and every verified KEEP that is still being scored resolved —
    # presence evidence of its own, a copy adoption, or a verified absence.
    # Verdict-less, late and identity-less rows never block the rate.
    scored_keeps = [e for e in pending if _scoreable(e) and not e.get("retired") and
                    e.get("immediate_verdict") == "keep" and not e.get("late")]
    # A rate needs verified evidence: at least one scoreable row still on
    # the verified path (not retired, not answered by attestation alone —
    # a declined copy is still verified evidence of the row's own clip).
    has_scoreable = any(
        _scoreable(e) and not e.get("retired") and
        (not e.get("attestation") or
         (e.get("attestation") or {}).get("fact") == "not_adopted")
        for e in rows)
    # Unattested identity-less rows can only be settled by the owner's word:
    # a night holding them is never sealed, so the advice cannot go quiet
    # while they wait (closing here is what made legacy rows vanish).
    unattested_idless = [e for e in rows
                         if not e.get("set_identity") and _needs_sweep(e)]
    # Rule (b) alternative: a DROP resolves by his word alone, so an ENDED
    # night whose scoreable rows are ALL drop verdicts is definitive even
    # on partial evidence. Keeps need presence and unjudged rows need the
    # inventory, so either keeps the night partial for a real sweep.
    scoreable_rows = [e for e in pending
                      if _scoreable(e) and not e.get("retired")]
    drops_only = bool(scoreable_rows) and all(
        _verdict_at_evidence(e) == "drop" for e in scoreable_rows)
    definitive = bool(session.get("ended_at") and
                      (complete or session.get("swept_complete") or drops_only) and
                      has_scoreable and not unattested_idless and
                      all(_state_of(e) for e in scored_keeps))
    if definitive:
        for entry in pending:
            if entry.get("resolution_reason"):
                continue  # open questions (candidates, collisions) stay open
            presence = entry.get("presence")
            verdict = _verdict_at_evidence(entry)
            if presence and not entry.get("adoption_candidates"):
                entry["terminal_state"] = resolve_terminal(
                    {"immediate_verdict": verdict}, presence.get("state") != "absent")
            elif (verdict == "drop" and _scoreable(entry) and
                    not entry.get("retired")):
                entry["terminal_state"] = "drop"
            if entry.get("terminal_state") is not None:
                entry["terminal_at"] = _iso(now)
        session["closed_at"] = _iso(now)
        session["closed_by"] = "sweep"
        session["rate_status"] = "definitive"
    else:
        session["closed_at"] = None
        session["closed_by"] = "sweep_partial"
        session["rate_status"] = "partial"


def _report_row(entry: dict) -> dict:
    """Report view of one scored row — judged rows shown as they were judged."""
    snapshot = entry.get("judged_snapshot") or {} \
        if entry.get("immediate_verdict") else {}
    return {
        "entry_id": entry["id"],
        "clip_name": snapshot.get("clip_name", entry.get("clip_name")),
        "slot": snapshot.get("slot", entry.get("slot")),
        "source": snapshot.get("source", entry.get("source")),
        "state": _state_of(entry),
        "match": ((entry.get("presence") or {}).get("match") or
                  entry.get("resolution_match") or "prior"),
    }


def _source_stratum(source) -> str:
    """Stratum bucket for a row's source: ref-assisted nights, composed
    golds, and catalogue phrases each score in their own stratum — a
    composed gold served is not a catalogue keep."""
    text = str(source or "")
    if text.startswith("ref:"):
        return "ref"
    if text == "gold_composed":
        return "composed"
    return "catalogue"


def _session_report(data: dict, session: dict) -> dict:
    """Recompute one session's report from persisted rows. Never a cached
    sweep_report: later events can never rewrite what a sealed night said."""
    rows = [e for e in data["entries"]
            if e.get("session_id") == session.get("id") and
            not e.get("superseded_by")]
    counts = {key: 0 for key in TERMINAL_STATES}
    strata: dict[str, dict[str, int]] = {}
    resolved: list[dict] = []
    unresolved: list[dict] = []
    unscoreable = {"rows": 0, "judged": 0}
    late = {"counts": {}, "entries": []}
    attested_counts = {key: 0 for key in TERMINAL_STATES}
    attested_strata: dict[str, dict[str, int]] = {}
    attested_entries: list[dict] = []
    retired = 0
    withholding = 0
    adoption = {"kept_in_jam": 0, "jam_copy": 0, "adopted": 0,
                "adopted_unjudged": 0, "candidates": [], "destinations": {}}
    for entry in rows:
        if entry.get("retired"):
            retired += 1
            continue
        presence = entry.get("presence") or {}
        if presence.get("state") in ("jam_copy", "adopted"):
            # copy survival: where the answer actually lives now
            evidence_verdict = _verdict_at_evidence(entry)
            if presence["state"] == "jam_copy":
                adoption["jam_copy"] += 1
                if evidence_verdict == "keep":
                    adoption["kept_in_jam"] += 1
            elif evidence_verdict == "keep":
                adoption["adopted"] += 1
            else:
                adoption["adopted_unjudged"] += 1
            adoption["destinations"][entry["id"]] = [
                {"clip_id": clip.get("clip_id"), "track": clip.get("track")}
                for clip in presence.get("matched_clips") or []]
        if entry.get("adoption_candidates"):
            adoption["candidates"].append({
                "entry_id": entry["id"], "clip_name": entry.get("clip_name"),
                "clips": entry["adoption_candidates"]})
        verdict = entry.get("immediate_verdict")
        if verdict and _is_late(entry, session):
            late["counts"][verdict] = late["counts"].get(verdict, 0) + 1
            late["entries"].append({"entry_id": entry["id"],
                                    "clip_name": entry.get("clip_name"),
                                    "verdict": verdict,
                                    "judged_at": entry.get("judged_at"),
                                    "state": _state_of(entry)})
        attestation = entry.get("attestation")
        if attestation and attestation.get("fact") != "not_adopted":
            # The owner's word, in its own stratum — never mixed with the
            # verified numbers above or below.
            fact = attestation.get("fact")
            state = resolve_terminal(
                {"immediate_verdict": _verdict_at_evidence(entry)},
                fact != "absent")
            attested_counts[state] += 1
            key = "%s|%s|gear%s" % (
                _source_stratum(entry.get("source")),
                entry.get("role") or "?", entry.get("gear") if entry.get("gear") else "-")
            attested_strata.setdefault(key, {k: 0 for k in TERMINAL_STATES})[state] += 1
            attested_entries.append({"entry_id": entry["id"],
                                     "clip_name": entry.get("clip_name"),
                                     "fact": fact, "note": attestation.get("note"),
                                     "state": state,
                                     "attested_at": attestation.get("attested_at")})
            continue
        state = _state_of(entry)
        if state in TERMINAL_STATES and _scoreable(entry):
            counts[state] += 1
            key = "%s|%s|gear%s" % (
                _source_stratum(entry.get("source")),
                entry.get("role") or "?", entry.get("gear") if entry.get("gear") else "-")
            strata.setdefault(key, {k: 0 for k in TERMINAL_STATES})[state] += 1
            resolved.append(_report_row(entry))
        elif not _scoreable(entry):
            if state is None:
                unscoreable["rows"] += 1
                if verdict:
                    unscoreable["judged"] += 1
        elif state is None:
            unresolved.append({"entry_id": entry["id"],
                               "clip_name": entry.get("clip_name"),
                               "reason": entry.get("resolution_reason", "pending")})
            if _verdict_at_evidence(entry) == "keep":
                withholding += 1
    judged = counts["keep"] + counts["drop"]
    attested_judged = attested_counts["keep"] + attested_counts["drop"]
    definitive = session.get("rate_status") == "definitive"
    bounds = None
    pending_keep = 0
    if not definitive:
        # Summed-counts interval: [k/(j+p), (k+p)/(j+p)] with k = keeps,
        # j = judged (keep+drop), p = pending unresolved KEEP rows. Only
        # [None, None] when there is nothing to bound (j+p == 0).
        pending_keep = sum(1 for e in rows if _scoreable(e) and
                           not e.get("retired") and not e.get("attestation") and
                           not _state_of(e) and _verdict_at_evidence(e) == "keep")
        denom = judged + pending_keep
        bounds = ([round(counts["keep"] / denom, 3),
                   round((counts["keep"] + pending_keep) / denom, 3)]
                  if denom else [None, None])
    report = {
        "ok": True, "session_id": session.get("id"), "mode": session.get("mode"),
        "resolved": len(resolved), "counts": counts, "judged": judged,
        "keep_rate": round(counts["keep"] / judged, 3) if definitive and judged else None,
        "keep_rate_if_abandoned_counted_as_drop": (
            round(counts["keep"] / (judged + counts["abandoned"]), 3)
            if definitive and not unresolved and (judged + counts["abandoned"]) else None),
        "rate_status": "definitive" if definitive else "partial",
        "keep_rate_bounds": bounds,
        "strata": {key: dict(value, judged=value["keep"] + value["drop"])
                   for key, value in sorted(strata.items())},
        "entries": resolved, "unresolved": unresolved,
        "late_verdicts": late, "unscoreable": unscoreable,
        "attested": {"counts": attested_counts, "judged": attested_judged,
                     "keep_rate": (round(attested_counts["keep"] / attested_judged, 3)
                                   if attested_judged else None),
                     "strata": {key: dict(value, judged=value["keep"] + value["drop"])
                                for key, value in sorted(attested_strata.items())},
                     "entries": attested_entries},
        "retired": retired,
        "adoption": adoption,
        "coverage": session.get("sweep_coverage") or {},
    }
    if withholding:
        report["_withholding"] = withholding
    report["_pending_keep"] = pending_keep
    return report


def _merge_session_reports(reports: list[dict], sessions: list[dict],
                           coverage: dict | None) -> dict:
    """One report over everything this close touched; the historical shape is
    kept verbatim for the single-session case every existing caller uses."""
    withholding = sum(r.pop("_withholding", 0) for r in reports)
    counts = {key: sum(r["counts"][key] for r in reports) for key in TERMINAL_STATES}
    strata: dict[str, dict[str, int]] = {}
    for report in reports:
        for key, cell in report["strata"].items():
            target = strata.setdefault(key, {k: 0 for k in TERMINAL_STATES})
            for state in TERMINAL_STATES:
                target[state] += cell.get(state, 0)
    judged = counts["keep"] + counts["drop"]
    definitive = bool(reports) and all(r["rate_status"] == "definitive"
                                       for r in reports)
    unresolved = [row for r in reports for row in r["unresolved"]]
    unscoreable_rows = sum(r["unscoreable"]["rows"] for r in reports)
    late_counts: dict[str, int] = {}
    for r in reports:
        for verdict, number in r["late_verdicts"]["counts"].items():
            late_counts[verdict] = late_counts.get(verdict, 0) + number
    attested_counts = {key: sum(r["attested"]["counts"][key] for r in reports)
                       for key in TERMINAL_STATES}
    attested_strata: dict[str, dict[str, int]] = {}
    for r in reports:
        for key, cell in r["attested"]["strata"].items():
            target = attested_strata.setdefault(key, {k: 0 for k in TERMINAL_STATES})
            for state in TERMINAL_STATES:
                target[state] += cell.get(state, 0)
    attested_judged = attested_counts["keep"] + attested_counts["drop"]
    adoption = {"kept_in_jam": sum(r["adoption"]["kept_in_jam"] for r in reports),
                "jam_copy": sum(r["adoption"]["jam_copy"] for r in reports),
                "adopted": sum(r["adoption"]["adopted"] for r in reports),
                "adopted_unjudged": sum(r["adoption"]["adopted_unjudged"]
                                        for r in reports),
                "candidates": [row for r in reports
                               for row in r["adoption"]["candidates"]],
                "destinations": {entry_id: clips for r in reports
                                 for entry_id, clips in
                                 r["adoption"]["destinations"].items()}}
    bounds = None
    if not definitive:
        # Never average per-session rates: bound the summed counts across
        # every session this report covers.
        pending = sum(r.pop("_pending_keep", 0) for r in reports)
        denom = judged + pending
        bounds = ([round(counts["keep"] / denom, 3),
                   round((counts["keep"] + pending) / denom, 3)]
                  if denom else [None, None])
    else:
        for r in reports:
            r.pop("_pending_keep", None)
    report = {
        "ok": True, "session_id": sessions[-1]["id"],
        "session_ids": [s["id"] for s in sessions],
        "mode": sessions[-1].get("mode"),
        "resolved": sum(len(r["entries"]) for r in reports),
        "counts": counts, "judged": judged,
        "keep_rate": round(counts["keep"] / judged, 3) if definitive and judged else None,
        "keep_rate_if_abandoned_counted_as_drop": (
            round(counts["keep"] / (judged + counts["abandoned"]), 3)
            if definitive and not unresolved and (judged + counts["abandoned"]) else None),
        "rate_status": "definitive" if definitive else "partial",
        "keep_rate_bounds": bounds,
        "strata": {key: dict(value, judged=value["keep"] + value["drop"])
                   for key, value in sorted(strata.items())},
        "entries": [row for r in reports for row in r["entries"]],
        "unresolved": unresolved,
        "late_verdicts": {"counts": late_counts,
                          "entries": [row for r in reports
                                      for row in r["late_verdicts"]["entries"]]},
        "unscoreable": {"rows": unscoreable_rows,
                        "judged": sum(r["unscoreable"]["judged"] for r in reports)},
        "attested": {"counts": attested_counts, "judged": attested_judged,
                     "keep_rate": (round(attested_counts["keep"] / attested_judged, 3)
                                   if attested_judged else None),
                     "strata": {key: dict(value, judged=value["keep"] + value["drop"])
                                for key, value in sorted(attested_strata.items())},
                     "entries": [row for r in reports
                                 for row in r["attested"]["entries"]]},
        "retired": sum(r["retired"] for r in reports),
        "adoption": adoption,
        "coverage": dict(coverage if coverage is not None
                         else reports[-1]["coverage"],
                         unresolved_entries=len(unresolved)),
    }
    if withholding:
        report["warning"] = ("terminal outcomes remain unresolved; the definitive "
                             "keep rate is withheld (%d unresolved)" % len(unresolved))
    elif unresolved or unscoreable_rows:
        report["warning"] = ("%d row(s) remain unresolved or unscoreable — they never "
                             "inflate the KEEP rate; identity-less rows are scored "
                             "only by attestation" % (len(unresolved) + unscoreable_rows))
    elif judged == 0:
        report["warning"] = ("nothing was judged this session — the KEEP rate is undefined, not 0. "
                             "%d abandoned, %d left unjudged." %
                             (counts["abandoned"], counts["unjudged"]))
    elif judged < 8:
        report["warning"] = ("only %d judged this session — too thin to read as a rate; let it "
                             "accumulate before drawing a conclusion" % judged)
    return report


def close_session(present, session_id: str | None = None,
                  path: str | None = None) -> dict:
    """Score the night(s): sweep every open session this inventory can prove.

    ``present`` is the complete ``read_set_inventory()`` envelope. A bare list is
    accepted only for API compatibility and leaves every outcome unresolved.
    One inventory sweeps every non-closed session whose Set or alias chain
    matches, oldest first, with one global clip reservation; a closed session
    is never re-swept — its report is recomputed from persisted rows.

    The primary rate is survivors / JUDGED and is definitive only when every
    verified KEEP row still being scored has presence evidence. ``abandoned``
    and ``unjudged`` are reported beside it and never inside it: the arc's
    anti-gaming rule is that unjudged material must not inflate the KEEP rate.
    """
    path = path or LEDGER_PATH
    with _lock(path):
        data = _load_for_mutation(path)
        inventory_identity, complete, clips, inventory_errors = _inventory(present)
        targets, error = _sweep_targets(data, session_id, inventory_identity)
        if error is not None or not targets:
            return {"ok": False, "error": error or "no open session to close"}
        # A session that never learned its Set is never swept: no inventory
        # can score identity-less rows, and sweeping would seal the night
        # and hide its unattested rows. Only attestation settles those.
        to_sweep = [s for s in targets
                    if not s.get("closed_at") and s.get("set_identity") is not None]
        # Every close report states the coverage behind it, complete or not:
        # this inventory whenever the call sweeps (or nothing can be swept),
        # and — for a close that only recomputes a sealed night — the
        # coverage of the sweep that sealed it.
        coverage = {"complete": complete,
                    "inventory_errors": list(inventory_errors),
                    "observed_clips": len(clips)}
        if to_sweep:
            reserved: set[int] = set()
            # Canonicalize each clip once per sweep: every matcher below
            # reuses these lists instead of re-canonicalizing per row.
            canon_clips = [canonical_notes(clip.get("answer_notes"))
                           if _has_content(clip) else []
                           for clip in clips]
            clip_keys = [(_key_of_canon(canon) if canon else None)
                         for canon in canon_clips]
            scan_clips = [_clip_scan(canon) if canon else None
                          for canon in canon_clips]
            _reserve_sticky_claims(data, clips, inventory_identity, reserved,
                                   canon_clips, clip_keys)
            now = _now()
            # Two phases with one shared reservation: primary matching for
            # ALL target sessions first (later sessions' own clips are
            # claimed before any adoption runs), then ONE adoption/collision
            # pass over every session's queue together.
            swept: list[tuple[dict, list[dict]]] = []
            queues: list[tuple[dict, str]] = []
            for session in to_sweep:
                rows = [e for e in data["entries"]
                        if e.get("session_id") == session.get("id") and
                        not e.get("superseded_by")]
                session["sweep_coverage"] = dict(coverage)
                queues.extend(_sweep_primary(session, rows, clips,
                                             inventory_identity, complete,
                                             reserved, now, canon_clips))
                swept.append((session, rows))
            if complete and queues:
                _resolve_adoptions(queues, clips, inventory_identity,
                                   reserved, _iso(now), canon_clips,
                                   scan_clips)
            for session, rows in swept:
                _sweep_finalize(session, rows, complete, now)
            _save(data, path)
        else:
            sealed = [s["sweep_coverage"] for s in targets
                      if s.get("sweep_coverage")]
            if sealed:
                coverage = dict(sealed[-1])
        reports = [_session_report(data, s) for s in targets]
        return _merge_session_reports(reports, targets, coverage)


def attest(session_id: str, attest: dict, confirm: bool = False,
           path: str | None = None) -> dict:
    """Owner-attested facts for rows no sweep can verify. Preview by default.

    ``attest`` maps entry ids to a fact ("present" | "adopted" | "absent" |
    "not_adopted" | "retire") or {"fact": ..., "note": ...}. The fact feeds
    ``resolve_terminal`` with the row's EXISTING verdict — an attestation
    never creates one — and is reported in its own stratum, never mixed with
    verified numbers. Allowed only on rows without a verified resolution
    (identity-less, or unresolved after a complete sweep); "not_adopted"
    only declines a verified-absent row's adoption candidate and leaves the
    row verified. Nothing is written without ``confirm``.
    """
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("attest requires the session id of the night being attested")
    if not isinstance(attest, dict) or not attest:
        raise ValueError("attest must be a non-empty {entry_id: fact} mapping")
    path = path or LEDGER_PATH
    with _lock(path):
        if confirm:
            data = _load_for_mutation(path)
        else:
            data, _, _ = _read_data(path)
        session = next((s for s in data.get("sessions", [])
                        if s.get("id") == session_id), None)
        if session is None:
            raise ValueError("unknown session %r" % session_id)
        changes: list[tuple[dict, str, str | None]] = []
        for entry_id, value in attest.items():
            fact = value if isinstance(value, str) else \
                value.get("fact") if isinstance(value, dict) else None
            note = value.get("note") if isinstance(value, dict) else None
            if fact not in ATTEST_FACTS:
                raise ValueError(
                    "attest fact for %s must be one of %s" % (entry_id, ATTEST_FACTS))
            entry = next((e for e in data.get("entries", [])
                          if e.get("id") == entry_id), None)
            if entry is None or entry.get("session_id") != session_id:
                raise ValueError("attest entry %r is not part of session %s"
                                 % (entry_id, session_id))
            changes.append((entry, fact, note))
        # All-or-nothing: every target is validated before anything changes.
        settled: set[str] = set()
        for entry, fact, note in changes:
            attestation = entry.get("attestation")
            if attestation:
                if attestation.get("fact") == fact and \
                        attestation.get("note") == note:
                    # Re-sent identical word: idempotent, changes nothing.
                    settled.add(entry["id"])
                    continue
                raise ValueError("entry %s is already attested — attestations "
                                 "are immutable" % entry["id"])
            if fact == "not_adopted":
                presence = entry.get("presence") or {}
                if presence.get("state") != "absent" or \
                        not entry.get("adoption_candidates"):
                    raise ValueError("not_adopted needs a verified-absent row "
                                     "with an adoption candidate")
                continue
            if _state_of(entry) is not None or (
                    _scoreable(entry) and
                    _verdict_at_evidence(entry) == "drop"):
                # Any verified outcome — a terminal state, a provisional
                # state, or his settled DROP word on a scoreable row — is
                # immutable: attest and retire only touch rows with no
                # verified state.
                raise ValueError("entry %s already has a verified outcome — "
                                 "attest only what a sweep could not resolve"
                                 % entry["id"])
            if (_scoreable(entry) and session.get("set_identity") and
                    session.get("swept_complete") is not True):
                raise ValueError("entry %s is still sweepable — sweep first; "
                                 "attest only what a complete sweep could not "
                                 "resolve" % entry["id"])
        now_iso = _iso(_now())
        for entry, fact, note in changes:
            if entry["id"] in settled:
                continue
            entry["attestation"] = {"fact": fact, "note": note,
                                    "attested_at": now_iso}
            if fact == "retire":
                entry["retired"] = True
            elif fact == "not_adopted":
                # the candidates are declined; the verified absence of the
                # row's own clip is the answer after all
                entry.pop("adoption_candidates", None)
                if entry.get("presence"):
                    entry["presence"]["adoption"] = None
                    if not _state_of(entry):
                        entry["resolution_match"] = entry["presence"].get("match")
                        entry.pop("resolution_reason", None)
                        entry["provisional_state"] = resolve_terminal(
                            {"immediate_verdict": _verdict_at_evidence(entry)}, False)
        if confirm:
            if session.get("set_identity") is None:
                # An identity-less night can never be swept: once every row
                # is attested or retired it closes here, in its own stratum.
                remaining = [e for e in data.get("entries", [])
                             if e.get("session_id") == session_id and
                             not e.get("superseded_by") and
                             not e.get("attestation") and not e.get("retired")]
                if not remaining:
                    session["closed_at"] = now_iso
                    session["closed_by"] = "attested"
            _save(data, path)
        return _merge_session_reports([_session_report(data, session)],
                                      [session], None)


_SWEEP_SLOT_RE = re.compile(r"^\d+/\d+$")


def open_session_entries(data: dict, now: float | None = None) -> list[dict]:
    """Entries of the still-open session. Read-only twin of _active_session:
    never opens, closes or touches anything. An ended session is finished
    even while it awaits its scoring sweep, so it is never the open one."""
    sessions = data.get("sessions") or []
    if not sessions:
        return []
    last = sessions[-1]
    now = _now() if now is None else now
    if last.get("closed_at") or last.get("ended_at") or \
            (now - last.get("last_activity", 0)) > SESSION_IDLE_SECONDS:
        return []
    sid = last.get("id")
    return [e for e in data.get("entries", []) if e.get("session_id") == sid]


def _pending_refusal_ids(data: dict, limit: int = 8) -> tuple[list[str], list[str]]:
    """Ids a refusal can name, newest first: verdict-pending rows a judgment
    could still bind explicitly, and — kept separate — the identity-less rows
    only attestation can settle (judging them changes nothing about their
    scoreability)."""
    pending: list[str] = []
    attest_only: list[str] = []
    for group in reversed(sessions_needing_sweep(data)):
        for entry in reversed(group["entries"]):
            if not entry.get("set_identity"):
                attest_only.append(entry.get("id"))
            elif entry.get("immediate_verdict") is None and _judgable(entry):
                pending.append(entry.get("id"))
    return ([i for i in pending if i][:limit],
            [i for i in attest_only if i][:limit])


def resolve_judgment_target(data: dict, verdict: str | None, *,
                            entry_id: str | None = None,
                            slot: str | None = None,
                            answer_notes: list[dict] | None = None,
                            source_phrase_id: str | None = None,
                            origin: str = "mcp",
                            now: float | None = None) -> dict:
    """Bind one judgment to one ledger row — or refuse. Read-only.

    Explicit identity wins in descending strength: entry id, the exact
    answer snapshot (``content_equal``), source id plus slot, then a slot
    alone when it identifies ONE pending row. Everything but an explicit
    entry id is scoped to the ACTIVE sessions (not closed, not ended,
    inside the 6-hour idle window), so nothing can reach back into a night
    that already moved on. Attested, retired and superseded rows can never
    be (re-)judged on any path. An explicit entry id on a swept, ended or
    idle night still binds — marked ``late``, because it scores that way.
    A bare judgment binds the durable session cursor; an already-judged
    cursor (and a source+slot match, and the legacy no-cursor fallback)
    replays only for the same verdict within 15 minutes of ``judged_at``,
    and refuses afterwards. Refusals name the verdict-pending rows and,
    separately, the identity-less rows only attestation can settle.

    Returns ``{"bound": True, "entry": …, "binding": …, "replay": bool,
    "late": bool, "origin": …}``, a soft ``{"bound": False,
    "learning_only": True, "note": …}`` when today's behaviour is to record
    the verdict in ratings only, or a refusal ``{"bound": False,
    "refused": True, "error": …, "pending_ids": […], "attest_only": […]}``
    the caller must raise or surface. ``verdict=None`` (internal cursor
    probes) skips the replay window check.
    """
    now = _now() if now is None else now
    verdict = str(verdict).strip().lower() if verdict is not None else None
    if verdict is not None and verdict not in ("keep", "drop"):
        raise ValueError("verdict must be 'keep' or 'drop'")
    slot_key = canonical_slot(slot) if slot is not None else None
    sessions = {s.get("id"): s for s in data.get("sessions", [])}
    live = _live_sessions(data, now)
    live_ids = {s.get("id") for s in live}
    entries = data.get("entries", [])

    def _refuse(error: str) -> dict:
        pending, attest_only = _pending_refusal_ids(data)
        message = error
        if pending:
            message = "%s (pending: %s)" % (message, ", ".join(pending))
        if attest_only:
            # with nothing judgable left, say what those rows actually need
            qualifier = ("these rows need attest, not judgment: %s" if not pending
                         else "attest-only: %s")
            message = "%s (%s)" % (message, qualifier % ", ".join(attest_only))
        return {"bound": False, "refused": True, "error": message,
                "pending_ids": pending, "attest_only": attest_only,
                "origin": origin}

    def _bind(row: dict, how: str, *, replay: bool = False,
              late: bool = False) -> dict:
        return {"bound": True, "entry": dict(row), "binding": how,
                "replay": replay, "late": late, "origin": origin}

    def _settled_reason(row: dict) -> str | None:
        """Why a row can no longer take a verdict, or None if it can."""
        if row.get("retired"):
            return "retired"
        if row.get("attestation"):
            return "attested"
        if row.get("superseded_by"):
            return "superseded"
        return None

    def _is_late(row: dict) -> bool:
        """Whether a verdict landing now would score as late — ONE rule,
        the scorer's: the night's evidence was already sealed (a complete
        sweep ran — swept_at, its presence evidence exists — or the night
        closed) before the verdict lands. An ended or long-idle night that
        was never swept is NOT late; its verdict still scores normally."""
        if row.get("presence"):
            return True
        session = sessions.get(row.get("session_id"))
        if session is None:
            return False
        return bool(session.get("swept_at") or session.get("closed_at"))

    if entry_id is not None:
        row = next((e for e in entries if e.get("id") == entry_id), None)
        if row is None:
            return _refuse("unknown ledger entry %s" % entry_id)
        settled = _settled_reason(row)
        if settled is not None:
            # An attested row's owner word is immutable, a retired row is
            # out of the score, a superseded row's judgment belongs to its
            # replacement — none of them may be re-judged after the fact.
            return _refuse("ledger entry %s is %s — it can no longer take a "
                           "verdict" % (entry_id, settled))
        if slot_key is not None and canonical_slot(row.get("slot")) != slot_key:
            return _refuse("slot %r does not match ledger entry %s (stored %r)"
                           % (slot, entry_id, row.get("slot")))
        return _bind(row, "entry_id",
                     replay=row.get("immediate_verdict") is not None,
                     late=_is_late(row))

    scope = [e for e in entries
             if e.get("session_id") in live_ids
             and not e.get("superseded_by") and not e.get("retired")]

    if answer_notes is not None:
        try:
            normalized = canonical_notes(answer_notes)
        except (KeyError, TypeError, ValueError):
            return {"bound": False, "learning_only": True, "origin": origin,
                    "note": "answer_notes could not be normalized for ledger matching"}

        def _carries(row: dict) -> bool:
            if source_phrase_id is not None and not _same_phrase_id(
                    row.get("source_phrase_id"), source_phrase_id):
                return False
            if slot_key is not None and canonical_slot(row.get("slot")) != slot_key:
                return False
            candidates = [_entry_notes(row)]
            snapshot = (row.get("judged_snapshot") or {}).get("answer_notes")
            if snapshot is not None:
                candidates.append(snapshot)
            return any(notes is not None and _canon_equal(normalized, canonical_notes(notes))
                       for notes in candidates)

        pending = [e for e in scope
                   if _judgable(e) and e.get("immediate_verdict") is None and _carries(e)]
        if len(pending) > 1:
            return _refuse("ambiguous ledger match for exact answer_notes (%d pending "
                           "entries: %s)" % (len(pending),
                                             ", ".join(e["id"] for e in pending)))
        if pending:
            return _bind(pending[0], "answer_notes")
        judged = [e for e in scope if e.get("immediate_verdict") is not None and _carries(e)]
        if len(judged) > 1:
            return _refuse("ambiguous replay match for exact answer_notes (%d judged "
                           "entries: %s)" % (len(judged),
                                             ", ".join(e["id"] for e in judged)))
        if judged:
            return _bind(judged[0], "answer_notes", replay=True)
        return {"bound": False, "learning_only": True, "origin": origin,
                "note": "no pending ledger entry matched exact answer_notes"}

    if source_phrase_id is not None and slot_key is not None:
        matches_all = [e for e in scope
                       if _same_phrase_id(e.get("source_phrase_id"), source_phrase_id)
                       and canonical_slot(e.get("slot")) == slot_key]
        matches = [e for e in matches_all if _settled_reason(e) is None]
        if not matches and matches_all:
            return _refuse("source_phrase_id=%s and slot=%s name a row that is "
                           "%s — it can no longer take a verdict"
                           % (source_phrase_id, slot, _settled_reason(matches_all[0])))
        if len(matches) > 1:
            return _refuse("ambiguous ledger match for source_phrase_id=%s and slot=%s "
                           "(%d entries: %s)" % (source_phrase_id, slot, len(matches),
                                                 ", ".join(e["id"] for e in matches)))
        if matches:
            row = matches[0]
            if row.get("immediate_verdict") is None:
                return _bind(row, "source_phrase_id+slot")
            judged_at = _epoch_of_iso(row.get("judged_at"))
            within_window = (verdict is not None
                             and row.get("immediate_verdict") == verdict
                             and judged_at is not None
                             and 0 <= now - judged_at <= BARE_REPLAY_WINDOW_SECONDS)
            if within_window:
                return _bind(row, "source_phrase_id+slot", replay=True)
            return _refuse("entry %s was already judged %r at %s — replay the same "
                           "verdict within %d minutes or use its entry_id"
                           % (row["id"], row.get("immediate_verdict"),
                              row.get("judged_at"), BARE_REPLAY_WINDOW_SECONDS // 60))
        return {"bound": False, "learning_only": True, "origin": origin,
                "note": "no pending ledger entry matched source_phrase_id=%s and slot=%s"
                        % (source_phrase_id, slot)}

    if slot_key is not None:
        matches = [e for e in scope
                   if _judgable(e) and e.get("immediate_verdict") is None
                   and canonical_slot(e.get("slot")) == slot_key]
        if len(matches) > 1:
            return _refuse("ambiguous ledger match for slot=%s (%d pending entries: %s)"
                           % (slot, len(matches), ", ".join(e["id"] for e in matches)))
        if matches:
            return _bind(matches[0], "slot")
        return {"bound": False, "learning_only": True, "origin": origin,
                "note": "no pending ledger entry matched slot=%s" % slot}

    # Bare: the durable cursor of the active session — recency is identity
    # nowhere else.
    if not live:
        return _refuse("no active session to judge — name one of the pending "
                       "entries explicitly")
    session = live[-1]
    served_id = (session.get("last_judgment_target_entry_id") or
                 session.get("last_served_entry_id"))

    def _cursor_outcome(row: dict) -> dict:
        """One cursor candidate: unjudged binds, judged replays only for the
        same verdict inside the window, settled rows are refused."""
        settled = _settled_reason(row)
        if settled is not None:
            return _refuse("session cursor %s is %s — it can no longer take a "
                           "verdict" % (row.get("id"), settled))
        if row.get("immediate_verdict") is None:
            return _bind(row, "cursor")
        judged_at = _epoch_of_iso(row.get("judged_at"))
        within_window = (verdict is not None
                         and row.get("immediate_verdict") == verdict
                         and judged_at is not None
                         and 0 <= now - judged_at <= BARE_REPLAY_WINDOW_SECONDS)
        if within_window:
            return _bind(row, "cursor", replay=True)
        return _refuse("session cursor %s was already judged %r at %s — replay the same "
                       "verdict within %d minutes or name a pending entry explicitly"
                       % (row.get("id"), row.get("immediate_verdict"),
                          row.get("judged_at"), BARE_REPLAY_WINDOW_SECONDS // 60))

    cursor = next((e for e in entries if e.get("id") == served_id), None)
    if cursor is not None:
        return _cursor_outcome(cursor)
    # No cursor recorded (legacy session): the newest still-judgable row,
    # under the same settled and replay-window rules as a real cursor.
    fallback = next((e for e in reversed(entries)
                     if e.get("session_id") == session.get("id") and _judgable(e)), None)
    if fallback is None:
        return {"bound": False, "learning_only": True, "origin": origin,
                "note": "no pending ledger entry available for bare judgment"}
    return _cursor_outcome(fallback)


def bind_judgment_target(entry_id: str, *, path: str | None = None) -> dict:
    """Persist the entry selected by a bare judgment as its replay cursor."""
    path = path or LEDGER_PATH
    with _lock(path):
        data = _load_for_mutation(path)
        entry = next((item for item in data.get("entries", [])
                      if item.get("id") == entry_id), None)
        if entry is None:
            raise ValueError("unknown ledger entry %s" % entry_id)
        session = next((item for item in data.get("sessions", [])
                        if item.get("id") == entry.get("session_id")), None)
        if session is None:
            raise ValueError("entry has no ledger session")
        session["last_judgment_target_entry_id"] = entry_id
        _save(data, path)
        return dict(entry)


def unswept_session_entries(data: dict) -> list[dict]:
    """Entries of the newest session that has never been closed.

    Compat wrapper over ``sessions_needing_sweep``. Deliberately NOT
    idle-gated. ``open_session_entries`` answers "which session does a new
    write join", where the 6-hour window is right. This answers "is a night
    still unscored", where the window is exactly wrong: it went quiet
    BECAUSE the night ended, so the reminder fell silent at the only moment
    it mattered (his 2026-09-02 session: 5 entries, 11 h idle, advice empty).
    """
    needing = sessions_needing_sweep(data)
    if not needing:
        return []
    return list(needing[-1]["entries"])


def last_known_set_id(data: dict) -> str | None:
    """The Set id the ledger most recently saw: the newest session that
    ever learned its identity. That is the Set hear_the_room's cheap
    pre-filters may assume is open until a write says otherwise."""
    for session in reversed(data.get("sessions", [])):
        identity = session.get("set_identity")
        if identity and identity.get("set_id"):
            return identity.get("set_id")
    return None


def last_known_set_path(data: dict) -> str | None:
    """The open Set's file path when identity carried one (None otherwise) —
    the corpus-membership check in session_report/hear_the_room."""
    for session in reversed(data.get("sessions", [])):
        identity = session.get("set_identity") or {}
        file_path = identity.get("file_path")
        if isinstance(file_path, str) and file_path:
            return file_path
    return None


def ledger_status(path: str | None = None, now: float | None = None) -> dict:
    """Compact Live-free loop state for every push of the loop.

    One glance answers: which session is live and how many of its writes
    still await judgment, which ended nights still need their sweep (and how
    many KEEP rows are withholding the rate), what legacy rows only the
    owner's word can settle, and the single next action. Pure read — no
    Live contact, so it can ride along on any result.
    """
    data = load(path)
    now = _now() if now is None else now
    entries = [e for e in data.get("entries", [])
               if not e.get("superseded_by")]
    live = _live_sessions(data, now)
    current = live[-1] if live else None
    session_block = None
    if current is not None:
        rows = [e for e in entries if e.get("session_id") == current.get("id")]
        session_block = {
            "id": current.get("id"),
            "state": "open",
            "writes": len(rows),
            "pending_judgment": sum(
                1 for e in rows
                if e.get("immediate_verdict") is None and _judgable(e)),
            "scoreable_writes": sum(1 for e in rows if _scoreable(e)),
        }
    unswept = []
    legacy = {"unscoreable_rows": 0, "sessions": []}
    for group in sessions_needing_sweep(data):
        session = group["session"]
        unswept.append({
            "session_id": session.get("id"),
            "set_id": (session.get("set_identity") or {}).get("set_id"),
            "ended_at": session.get("ended_at"),
            "keep_rows_unresolved": sum(
                1 for e in group["entries"]
                if _verdict_at_evidence(e) == "keep"),
        })
        idless = [e for e in group["entries"] if not e.get("set_identity")]
        if idless:
            legacy["unscoreable_rows"] += len(idless)
            legacy["sessions"].append(session.get("id"))
    # One next action, by precedence: judging tonight's pending writes
    # (the loop's engine — nothing else matters while decisions wait),
    # then the owner's word for rows no sweep can settle, then the sweep
    # for nights that have ENDED — a live session's close reminder waits
    # for its own ending.
    next_action = None
    if session_block and session_block["pending_judgment"]:
        next_action = (
            "%d write(s) this session still await judgment — keep or drop "
            "them before the night ends" % session_block["pending_judgment"])
    elif legacy["unscoreable_rows"]:
        sid = legacy["sessions"][0]
        next_action = (
            "%d identity-less row(s) can never be swept — attest or retire "
            "them: close_session(session_id='%s', attest={'<entry_id>': "
            "'present|adopted|absent|not_adopted|retire'}, confirm=True)"
            % (legacy["unscoreable_rows"], sid))
    else:
        # a night past the idle window is over for advice purposes even if
        # no newer write ever ended it — that is the normal morning after
        live_ids = {s.get("id") for s in live}
        ended_unswept = [row for row in unswept
                         if row.get("ended_at")
                         or row.get("session_id") not in live_ids]
        if ended_unswept:
            target = ended_unswept[0]
            withholding = target["keep_rows_unresolved"]
            next_action = (
                "run close_session() for session %s while its Set is still open%s"
                % (target["session_id"],
                   " — %d KEEP row(s) are withholding a rate" % withholding
                   if withholding else ""))
    return {
        "session": session_block,
        "unswept": unswept,
        "legacy": legacy,
        "next_action": next_action,
        "accounting_failures": list(data.get("accounting_failures") or []),
    }


def clip_name_counts(entries) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in entries:
        name = e.get("clip_name")
        if name:
            out[name] = out.get(name, 0) + 1
    return out


def summary(path: str | None = None) -> dict:
    """Cheap read-only view: what is on the record so far, plus the advice that
    keeps a night scoreable (sweep before the Set moves on; identity and content
    evidence are required for a definitive survival result)."""
    data = load(path)
    entries = [entry for entry in data["entries"] if not entry.get("superseded_by")]
    # Open means joinable: ended sessions are finished even while they
    # await their scoring sweep, so they are never the open one.
    open_sessions = [s for s in data["sessions"]
                     if not s.get("closed_at") and not s.get("ended_at")]
    by_source: dict[str, int] = {}
    for e in entries:
        kind = _source_stratum(e.get("source"))
        by_source[kind] = by_source.get(kind, 0) + 1

    # the REPORT asks "is a night unscored", which outlives the idle window —
    # and it asks it of EVERY unclosed session, not just the newest one
    needing = sessions_needing_sweep(data)
    open_entries = [entry for group in needing for entry in group["entries"]]
    awaiting_open = len(open_entries)
    # Names and slots are no longer reconciliation keys; identity/content and
    # explicit adoption links are.  Keep the legacy result fields, but never
    # advise users to rename or relocate clips to satisfy the old matcher.
    duplicates: list[str] = []
    odd_slots: list[str] = []
    advice: list[str] = []
    for group in needing:
        sid = group["session"].get("id")
        idless = [entry for entry in group["entries"] if not entry.get("set_identity")]
        if len(idless) < len(group["entries"]):
            advice.append(
                "%d clip(s) in session %s still await the sweep — run "
                "close_session() to sweep while this Set is still open; after "
                "Live restarts only content matching remains and some rows may "
                "stay unresolved" % (len(group["entries"]) - len(idless), sid))
    # Identity-less rows can only be settled by the owner's word, and the
    # reminder must survive anything — including a night that was closed
    # around them (history, or the pre-fix swallow). Every session, closed
    # or not, keeps nagging until each row is attested or retired.
    for session in data["sessions"]:
        sid = session.get("id")
        idless = [entry for entry in entries
                  if entry.get("session_id") == sid
                  and not entry.get("set_identity") and _needs_sweep(entry)]
        if idless:
            advice.append(
                "%d identity-less row(s) in session %s cannot be scored by any "
                "sweep — attest or retire the remaining rows: close_session(session_id='%s', "
                "attest={'<entry_id>': 'present|adopted|absent|not_adopted|retire'}, "
                "confirm=True)" % (len(idless), sid, sid))

    # The scoreboard aggregates only what its stratum can prove; verified and
    # attested numbers are never combined.
    definitive_sessions = 0
    verified_judged = verified_keep = 0
    attested_sessions = 0
    attested_judged_total = attested_keep_total = 0
    for session in data["sessions"]:
        rows = [e for e in entries if e.get("session_id") == session.get("id")]
        attested_here = False
        verified_here = 0
        for e in rows:
            if e.get("retired"):
                continue
            attestation = e.get("attestation")
            if attestation and attestation.get("fact") != "not_adopted":
                state = resolve_terminal(
                    {"immediate_verdict": _verdict_at_evidence(e)},
                    attestation.get("fact") != "absent")
                if state in ("keep", "drop"):
                    attested_judged_total += 1
                    if state == "keep":
                        attested_keep_total += 1
                attested_here = True
            elif (session.get("rate_status") == "definitive" and
                    e.get("terminal_state") in ("keep", "drop") and _scoreable(e)):
                verified_judged += 1
                verified_here += 1
                if e["terminal_state"] == "keep":
                    verified_keep += 1
        # A definitive night only lends weight when it actually judged
        # something on the verified path.
        if session.get("rate_status") == "definitive" and verified_here:
            definitive_sessions += 1
        if attested_here:
            attested_sessions += 1
    scoreboard = {
        "verified": {"definitive_sessions": definitive_sessions,
                     "judged": verified_judged, "keep": verified_keep,
                     "keep_rate": (round(verified_keep / verified_judged, 3)
                                   if verified_judged else None)},
        "attested": {"attested_sessions": attested_sessions,
                     "judged": attested_judged_total, "keep": attested_keep_total,
                     "keep_rate": (round(attested_keep_total / attested_judged_total, 3)
                                   if attested_judged_total else None)},
    }
    return {
        "sessions": len(data["sessions"]),
        "open_session": open_sessions[-1]["id"] if open_sessions else None,
        "entries": len(entries),
        "by_source": by_source,
        "judged": sum(1 for e in entries if e.get("immediate_verdict")),
        "awaiting_sweep": sum(1 for e in entries
                              if e.get("terminal_state") is None and not e.get("superseded_by")
                              and not e.get("attestation") and not e.get("retired")),
        "open_session_awaiting_sweep": awaiting_open,
        "duplicate_clip_names": duplicates,
        "advice": advice,
        "note": "close_session() assigns terminal states while Live is reachable; "
                "entries awaiting that sweep remain unresolved",
        "scoreboard": scoreboard,
        "freeze": {"rule": "≥8 verified judged across ≥3 definitive sessions",
                   "met": verified_judged >= 8 and definitive_sessions >= 3},
        "observations": observation_report(path),
        "coverage": {"sessions": len(data["sessions"]),
                     "entries_with_identity": sum(1 for e in entries if e.get("set_identity")),
                     "entries_with_runtime_ref": sum(1 for e in entries if e.get("clip_ref")),
                     "unresolved_identity": sum(1 for e in entries
                                                 if e.get("identity_status") == "unknown")},
        "path": path or LEDGER_PATH,
    }
