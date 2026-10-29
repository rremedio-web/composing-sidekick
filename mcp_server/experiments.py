"""Arrangement experiments and effect trials — register, judge, close.

The minimal 1.6 + 1.7 slice of ``.scratch/batch4/PLAN.md``: material the
sidekick already put in his Set on SIDEKICK tracks (line variants, a whole
drum track) and devices it added to his tracks (the two sidechain
compressors) get a durable row, one explicit verdict, and a verified terminal
state at close — reported as a **separate subtotal**, never inside the phrase
KEEP denominator (the phrase rate scores only judged phrase suggestions).

Rows live in ``store/session_ledger.json`` under two top-level lists,
``arrangement_experiments`` and ``effect_trials`` — never in ``entries``, so
nothing the phrase sweep, binder or scoreboard does can see them. They join
the ledger session like a write does (``ledger._active_session``, verified
Set identity), carry ``recovered: True`` because the write predates the
registration, and a before-readback digest of the clip content (or the device
parameters) taken from the Live API at registration.

Judgment: ``judge_answer(verdict, entry_id=<id or exact label>, reason=…)``
routes here BEFORE any note-material learning. A DROP never bans, un-golds
or re-rates a phrase. The verdict, reason, timestamp and the owner's intent
revision (his reason IS his revised intent about the experiment) land on the
row, and ``{kind, label, piece, verdict, reason}`` is appended to
``piece_decisions`` as model-level ``arrangement_feedback`` (owner records
outrank it). Selecting one variant judges that variant only; the reply names
its unjudged siblings.

Terminal-state contract (``close_session`` → :func:`sweep`), arrangement rows:

| evidence at close                                              | KEEP        | DROP | no verdict          |
|----------------------------------------------------------------|-------------|------|---------------------|
| identity mismatch (Set not the row's, nor in its session chain) | unknown*    | drop | unknown*            |
| inventory incomplete (warnings, failed reads)                   | unknown*    | drop | unknown*            |
| a registered clip's Live API read failed                        | unknown*    | drop | unknown*            |
| the id names something else now (Live reuses ids within a run:  | unknown*    | drop | unknown*            |
|   a device, a track — ``id_recycled``), or a clip on another    |             |      |                     |
|   track than the inventory lists (``object_mismatch``), or a    |             |      |                     |
|   clip the inventory lacks (``inventory_disagrees``)            |             |      |                     |
| some clips present, some gone (partial)                         | unknown*    | drop | unknown*            |
| all present, content digest changed                             | unknown*    | drop | unknown*            |
| all present + unchanged + audible (the track, its ancestor      | adopted     | drop | unjudged (pending)  |
|   groups and the clip unmuted, AND no solo anywhere in the Set  |             |      |                     |
|   unless this track or an ancestor is the one soloed; every     |             |      |                     |
|   group it sits in was read — ``observation.audibility`` =      |             |      |                     |
|   ``audible``) — reported by WHY: on_his_originals (the clip    |             |      |                     |
|   now lives on one of his tracks; PLAN §1.6's class),           |             |      |                     |
|   unmuted_since_registration (the SIDEKICK track was muted at   |             |      |                     |
|   registration and is not now), still_audible_on_sidekick (it   |             |      |                     |
|   was audible when registered and still is — no change of his   |             |      |                     |
|   was observed; the weakest class). WHICH is decided by track   |             |      |                     |
|   IDENTITY, never by name: the track the sidekick put it on     |             |      |                     |
|   (``origin_track``, this run) and any track the sidekick is    |             |      |                     |
|   known to have written on (:func:`sidekick_tracks`) keep the   |             |      |                     |
|   SIDEKICK classes — a rename is ``sidekick_track_renamed``;    |             |      |                     |
|   on_his_originals is the rest; never across runs               |             |      |                     |
| all present + unchanged + not provably audible — ``audibility`` | staged_kept | drop | unjudged (pending)  |
|   silent_by_mute (track, clip or an ancestor group muted),      |             |      |                     |
|   silent_by_solo (another track or group soloed), parent_unknown|             |      |                     |
|   (its group was not read), state_unknown (a track state no     |             |      |                     |
|   capture ever showed). Solo and group evidence is read from    |             |      |                     |
|   the overview; this is never a model of Live's routing.        |             |      |                     |
| every clip gone (Live says each id does not exist), or the      | not_adopted | drop | abandoned           |
|   track gone                                                    |             |      |                     |

``*`` unknown is **unresolved**: no terminal state, never counted, retried by
the next close (or settled by his word). ``unjudged`` rows stay pending too —
he can still judge them; only ``abandoned`` (gone without a word) is
terminal. A moved clip (same content, other bars) still counts; the move is
noted. Placement and mutes are observations beside the content digest, not
part of it. Survival subtotal: ``survived = adopted + staged_kept`` over
``resolved_judged = adopted + staged_kept + not_adopted + drop``, beside
``judged_total`` (every explicit verdict, resolved or not) and
``coverage`` (resolved/judged). The rate is published only when the two
are equal (``rate_status: definitive``); while a judged row is unresolved
it is withheld (``None``, ``partial``) — an unknown is never counted as a
drop — and under 8 judgments it carries the phrase rate's "too thin"
caution. ``adopted_by`` counts the three reasons apart. Ids count only inside the Live run that
issued them: in another runtime epoch a row is matched by content digest on
the track with its registered name — one match rebinds, none or several
leave it unknown, never not_adopted. Unchanged material of a SURVIVED row
(adopted / staged_kept / kept) seen again after its close — the same
content in another Live run, or under new ids in this one — is a
``survival_observations`` record on that row (next-session presence as
evidence on the row already in the score, recorded once per epoch/ids/
digest), never a second judged row; changed content, or a copy on HIS
track, is a new round (``new_round_of``); and ANY re-proposal of a row that
did not survive (abandoned / not_adopted / drop / removed) is a new
judgeable round (``re-proposed_after_<state>``) — a verdict on lost or
dropped material never forecloses the next one. A total never counts an
old unchanged-round child — not beside its parent, not alone.

Effect trials (devices on HIS tracks): ``kept`` = device present on that
track after KEEP — presence only; its parameters are a SEPARATE fact
(``params_coverage`` full/partial/none for what the close read could see,
``params_compared`` True only when every device was read in full at
registration and at close, ``params_changed`` / ``params_uncompared``,
``params_error`` saying why; the report counts ``kept_params_unchanged`` /
``_changed`` / ``_uncompared`` apart and says that none of it is evidence
the effect is audible — a compressor whose sidechain is unset ducks
nothing); ``removed`` =
device gone after KEEP; ``drop`` = his word; ``abandoned`` = gone unjudged.
GONE is proven, never inferred from a silent listing: the track's bound
device list lacks the id AND Live itself says that id does not exist —
only that text, for that id, proves absence; a warning-only, truncated,
empty or null reply is a FAILED read, never Live's word (on 2.2, whose
does-not-exist text for devices is uncaptured, a deleted device stays
unknown). ``unknown`` (unresolved) = identity mismatch, incomplete
inventory, the track's device list unread, the device still reading as
THIS device (same type and name) somewhere else — another track, a
return, the main track, inside a rack (``device_moved:<where>`` — it
exists, so it is neither gone nor kept where it was registered), the
by-id read of an unlisted device unread / failed (``device_unread`` /
``device_read_failed``), the id now naming something that is not this
device — a clip, an untyped object, another device (``id_recycled:<path>``),
or evidence that contradicts itself
(``evidence_mismatch:<what>``). Evidence is BOUND before it counts, as clip
reads are: the track's device list is trusted only when the reply is that
track (its own id, its overview index — ``track_id`` / ``track_path``);
a listed device must sit under the track's path (``device_path``) and
carry its registered type (``device_type``); a by-id read that answered
must name that very device — same id (``device_id``), type, parent track
(``device_parent``) — and must not place an unlisted device on this very
track (``listing``). Malformed or contradictory evidence refuses
registration and leaves a close unknown; it is never labelled with the
requested id. Reported on their own line, never in the MIDI denominator.

Pure stdlib; every write goes through the ledger's lock + atomic save.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid

for _alias in ("experiments", "mcp_server.experiments"):   # one module universe
    sys.modules.setdefault(_alias, sys.modules[__name__])

try:
    import ledger as _ledger
except ModuleNotFoundError:  # package import without mcp_server on sys.path
    from mcp_server import ledger as _ledger
# piece_context has no bare-name alias: the package name is the ONE module
# object every importer (finisher, piece_snapshot, the tests' patches) shares.
from mcp_server import piece_context as _piece_context

ARRANGEMENT_KIND = "arrangement_experiment"
EFFECT_KIND = "effect_trial"
ARRANGEMENT_LIST = "arrangement_experiments"
EFFECT_LIST = "effect_trials"
ARRANGEMENT_TERMINAL = ("adopted", "staged_kept", "not_adopted", "drop", "abandoned")
ARRANGEMENT_SURVIVED = ("adopted", "staged_kept")
ADOPTED_REASONS = ("on_his_originals", "unmuted_since_registration", "still_audible_on_sidekick")
EFFECT_TERMINAL = ("kept", "removed", "drop", "abandoned")
SIDEKICK_PREFIX = "SIDEKICK"
_LABEL_PREFIXES = ("SIDEKICK – ", "SIDEKICK - ", "SIDEKICK — ", "SIDEKICK ")
# Live API note detail Live reports beside pitch/time/velocity (clip_playback.LIVE_NOTE_DETAIL)
_NOTE_DETAIL = ("probability", "velocity_deviation", "release_velocity")
_TIMING_KEYS = ("loop_start", "loop_end", "start_marker", "end_marker", "looping",
                "signature_numerator", "signature_denominator")


# --- digests -------------------------------------------------------------------

def _digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def content_digest(live_read: dict) -> str:
    """Position-free, mute-free digest of a complete Live API clip read
    (``live_write.read_clip_live``): every note (pitch, start, duration,
    velocity, note-level mute, detail) plus the brace/marker timing. The
    clip's ``muted`` flag and its arrangement position are observations, not
    content — an unmute or a slide must not read as "changed"."""
    props = live_read.get("props") or {}
    notes = sorted(
        [round(float(n["start_beats"]), 6), int(n["pitch"]),
         round(float(n["duration_beats"]), 6), int(n["velocity"]), bool(n.get("mute"))]
        + [n.get(key) for key in _NOTE_DETAIL]
        for n in live_read.get("notes") or [])
    timing = {key: props.get(key) for key in _TIMING_KEYS}
    return _digest({"notes": notes, "timing": timing})


def params_digest(params: dict | None) -> str:
    return _digest(dict(params or {}))


def _strip_label(name: str) -> str:
    for prefix in _LABEL_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


# --- journals ------------------------------------------------------------------

def _group_of(journal: str | None) -> str | None:
    if not journal:
        return None
    return os.path.splitext(os.path.basename(str(journal)))[0]


def parse_journal(data: dict, *, journal: str | None = None) -> dict:
    """The candidates one write journal describes: ``{"set_path", "items"}``.

    Three shapes: the peak-compose journal (``tracks`` + ``clips`` keyed by
    variant name — one arrangement candidate per variant, the drums
    included; verification decides what still exists), the impact-drums
    journal (``track`` + ``new`` receipts — one whole-track candidate; its
    ``final_clips`` list is never read, it went stale before the riff
    re-lock) and the sidechain journal (``made`` — one effect trial per
    device on the owner track it was created on). Anything else is refused.
    """
    if not isinstance(data, dict):
        raise ValueError("journal must be a JSON object")
    group = _group_of(journal)
    set_path = data.get("set") if isinstance(data.get("set"), str) and data.get("set") else None
    items: list[dict] = []
    if isinstance(data.get("clips"), dict) and isinstance(data.get("tracks"), dict):
        for name, clip in data["clips"].items():
            track = data["tracks"].get(name)
            if not isinstance(clip, dict) or not isinstance(track, dict):
                continue
            track_id, clip_id = track.get("id"), clip.get("id")
            if not track_id or not clip_id:
                continue
            readback = track.get("readback")
            track_name = readback[0] if isinstance(readback, list) and readback and \
                isinstance(readback[0], str) else str(name)
            items.append({"kind": ARRANGEMENT_KIND, "label": _strip_label(track_name),
                          "track_id": str(track_id), "clip_ids": [str(clip_id)],
                          "group": group, "move": "arr:variant", "journal": journal})
    elif data.get("track") and isinstance(data.get("new"), dict):
        clip_ids = [str(rec["id"]) for rec in data["new"].values()
                    if isinstance(rec, dict) and rec.get("id")]
        state = data.get("track_state")
        track_name = state[0] if isinstance(state, list) and state and \
            isinstance(state[0], str) else "SIDEKICK track %s" % data["track"]
        items.append({"kind": ARRANGEMENT_KIND, "label": _strip_label(track_name),
                      "track_id": str(data["track"]), "clip_ids": clip_ids,
                      "group": group, "move": "arr:whole_track", "journal": journal})
    elif isinstance(data.get("made"), dict):
        before = data.get("before") if isinstance(data.get("before"), dict) else {}
        after = data.get("after_devices") if isinstance(data.get("after_devices"), dict) else {}
        for track_id, made in data["made"].items():
            if not isinstance(made, dict) or not made.get("id"):
                continue
            device_id = str(made["id"])
            device_name = next((row[1] for row in after.get(track_id) or []
                                if isinstance(row, list) and len(row) > 1
                                and str(row[0]) == device_id and row[1]), None)
            track_name = (before.get(track_id) or {}).get("name") if isinstance(
                before.get(track_id), dict) else None
            label = "%s on %s" % (device_name or "SIDEKICK device %s" % device_id,
                                  track_name or "track %s" % track_id)
            items.append({"kind": EFFECT_KIND, "label": label, "track_id": str(track_id),
                          "device_ids": [device_id], "group": group, "move": "fx:device",
                          "journal": journal})
    else:
        raise ValueError("unrecognised journal shape (expected the peak-compose, "
                         "impact-drums or sidechain journal keys)")
    if not items:
        raise ValueError("the journal names no candidates")
    return {"set_path": set_path, "items": items}


def load_journal(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return parse_journal(data, journal=path)


# --- verification (pure) --------------------------------------------------------

def _clip_record(clip_id: str, live: dict, location: dict) -> dict:
    frame = live.get("frame") or {}
    props = live.get("props") or {}
    return {"clip_id": clip_id, "name": props.get("name"),
            "digest": content_digest(live), "n_notes": len(live.get("notes") or []),
            "arr_start": frame.get("arr_start"), "arr_end": frame.get("arr_end"),
            "muted": bool(frame.get("muted")), "track_id": location.get("track_id"),
            "path": location.get("path")}


def _track_record(track: dict) -> dict:
    name = str(track.get("name") or "")
    return {"id": str(track.get("id")), "name": name, "index": track.get("index"),
            "muted": bool(track.get("muted")), "sidekick": name.startswith(SIDEKICK_PREFIX)}


def verify_candidate(candidate: dict, evidence: dict, *, expected_set_path: str) -> tuple:
    """One candidate against one evidence envelope → ``(row, None)`` or
    ``(None, reason)``. Nothing is written; ``row`` lacks id and session."""
    identity = evidence.get("set_identity") or {}
    if not identity or identity.get("file_path") != expected_set_path:
        return None, ("the open Set is %s, expected %s — nothing registered"
                      % (identity.get("file_path") or identity.get("set_id") or "unknown",
                         expected_set_path))
    if evidence.get("complete") is not True:
        return None, ("inventory incomplete (%s) — nothing registered"
                      % ("; ".join(str(e) for e in evidence.get("errors") or []) or "no detail"))
    kind = candidate.get("kind")
    track_id = str(candidate.get("track_id") or "")
    track = (evidence.get("tracks") or {}).get(track_id)
    if not track:
        return None, "track id %s is not in the Set" % track_id
    row = {"kind": kind, "label": candidate.get("label"), "group": candidate.get("group"),
           "journal": candidate.get("journal"), "move": candidate.get("move"),
           "proposal_id": candidate.get("proposal_id"),
           "source": "recovered", "recovered": True, "piece": identity.get("file_path"),
           "set_identity": _ledger._identity(identity), "track": _track_record(track),
           "immediate_verdict": None, "reason": None, "judged_at": None,
           "intent_revision": None, "terminal_state": None, "terminal_at": None,
           "observation": None}
    if kind == ARRANGEMENT_KIND:
        locations = evidence.get("clip_locations") or {}
        reads = evidence.get("clips") or {}
        clips = []
        for clip_id in [str(c) for c in candidate.get("clip_ids") or []]:
            location = locations.get(clip_id)
            if location is None:
                return None, "clip %s is not in the Set (journal says track %s)" % (clip_id, track_id)
            if str(location.get("track_id")) != track_id:
                return None, ("clip %s is on track %s, not on track %s"
                              % (clip_id, location.get("track_id"), track_id))
            if location.get("surface") != "arrangement":
                return None, "clip %s is a session clip, not an arrangement clip" % clip_id
            live = reads.get(clip_id)
            if not live or live.get("error"):
                return None, ("clip %s read failed: %s"
                              % (clip_id, (live or {}).get("error") or "no Live API read"))
            if not _on_track(live, track):
                return None, ("id %s is not a clip on track %s — Live reports %s (ids are "
                              "reused within a run)" % (clip_id, track_id,
                                                        live.get("object_path") or "no path"))
            clips.append(_clip_record(clip_id, live, location))
        if not clips:
            return None, "candidate %r names no clips" % candidate.get("label")
        registered = {c["clip_id"] for c in clips}
        row["clips"] = clips
        row["digest"] = _digest(sorted(c["digest"] for c in clips))
        row["unregistered_on_track"] = sorted(
            cid for cid, loc in locations.items()
            if str(loc.get("track_id")) == track_id and loc.get("surface") == "arrangement"
            and cid not in registered)
        return row, None
    if kind == EFFECT_KIND:
        listed = (evidence.get("track_devices") or {}).get(track_id)
        if listed is None:
            detail = (evidence.get("track_device_errors") or {}).get(track_id)
            return None, ("track %s device list unread%s"
                          % (track_id, ": " + str(detail) if detail else ""))
        reads = evidence.get("devices") or {}
        devices = []
        for device_id in [str(d) for d in candidate.get("device_ids") or []]:
            entry = next((d for d in listed if str(d.get("id")) == device_id), None)
            if entry is None:
                return None, "device %s is not on track %s" % (device_id, track_id)
            read = reads.get(device_id)
            if not read or (read.get("error") and not read.get("mismatch")):
                return None, ("device %s read failed: %s"
                              % (device_id, (read or {}).get("error") or "no device read"))
            why = _device_mismatch(device_id, entry, read, track)
            if why is not None:
                return None, why
            params = dict(read.get("params") or {})
            coverage = read.get("params_coverage") or ("full" if params else "none")
            devices.append({"device_id": device_id, "name": entry.get("name") or read.get("name"),
                            "type": entry.get("type"), "path": entry.get("path"),
                            "params": params, "params_coverage": coverage,
                            "params_error": read.get("params_error"),
                            "params_digest": params_digest(params)})
        if not devices:
            return None, "candidate %r names no devices" % candidate.get("label")
        row["devices"] = devices
        row["digest"] = _digest(sorted(d["params_digest"] for d in devices))
        return row, None
    return None, "unknown candidate kind %r" % kind


def evidence_needs(items: list[dict]) -> dict:
    """What the evidence reader must fetch for these candidates or rows: the
    ids they name (this runtime), and — for registered rows — their track
    NAMES, so that after a Live restart (new epoch, ids renumbered) the reader
    still brings back the content of every clip and the device list of every
    same-named track for content matching."""
    clip_ids: list[str] = []
    device_tracks: dict[str, str] = {}
    track_names: set[str] = set()
    for item in items:
        name = (item.get("track") or {}).get("name") if isinstance(item.get("track"), dict) else None
        if name:
            track_names.add(str(name))
        if item.get("kind") == ARRANGEMENT_KIND:
            for clip in item.get("clips") or []:
                clip_ids.append(str(clip.get("clip_id")))
            for clip_id in item.get("clip_ids") or []:
                clip_ids.append(str(clip_id))
        elif item.get("kind") == EFFECT_KIND:
            track_id = str((item.get("track") or {}).get("id") or item.get("track_id") or "")
            for device in item.get("devices") or []:
                device_tracks[str(device.get("device_id"))] = track_id
            for device_id in item.get("device_ids") or []:
                device_tracks[str(device_id)] = track_id
    return {"clip_ids": sorted(set(clip_ids)), "device_tracks": device_tracks,
            "track_names": sorted(track_names)}


# --- ledger ---------------------------------------------------------------------

def _lists(data: dict) -> tuple[list, list]:
    return data.setdefault(ARRANGEMENT_LIST, []), data.setdefault(EFFECT_LIST, [])


def all_rows(data: dict) -> list[dict]:
    arrangement, effect = _lists(data)
    return list(arrangement) + list(effect)


def pending_rows(data: dict, session_id: str | None = None) -> list[dict]:
    return [row for row in all_rows(data)
            if row.get("terminal_state") is None
            and (session_id is None or row.get("session_id") == session_id)]


def ledger_ids(data: dict) -> set[str]:
    """Every id the ledger already answers to: phrase entries (and their
    suggestion ids), sessions, experiment rows. A label may never be one."""
    ids: set[str] = set()
    for entry in data.get("entries") or []:
        for key in ("id", "suggestion_id"):
            if entry.get(key):
                ids.add(str(entry[key]))
    for session in data.get("sessions") or []:
        if session.get("id"):
            ids.add(str(session["id"]))
    for row in all_rows(data):
        if row.get("id"):
            ids.add(str(row["id"]))
    return ids


def find_row(data: dict, handle: str | None) -> dict | None:
    """A row by id, else by its exact label (whitespace-stripped, case kept)
    among PENDING rows — one match binds, none returns None, more than one
    raises: a handle that could mean two rows is never guessed. A handle
    that IS a phrase entry's id is that phrase's and returns None here,
    whatever any label says."""
    if not handle:
        return None
    if any(entry.get("id") == handle or entry.get("suggestion_id") == handle
           for entry in data.get("entries") or []):
        return None
    rows = all_rows(data)
    for row in rows:
        if row.get("id") == handle:
            return row
    key = str(handle).strip()
    matches = [row for row in rows if str(row.get("label") or "").strip() == key
               and row.get("terminal_state") is None]
    if len(matches) > 1:
        raise ValueError("label %r is ambiguous — it names %d pending candidates (%s); "
                         "judge by entry_id" % (key, len(matches),
                                                ", ".join(r["id"] for r in matches)))
    return matches[0] if matches else None


def _material_keys(row: dict) -> tuple[tuple, tuple, frozenset]:
    """What makes two rows the same material: this run's ids (epoch-bound,
    whatever track holds them now), or the same-named track holding the same
    content (a multiset of digests — two identical clips are not one) in any
    run."""
    identity = row.get("set_identity") or {}
    track = row.get("track") or {}
    if row.get("kind") == ARRANGEMENT_KIND:
        ids = frozenset(c["clip_id"] for c in row.get("clips") or [])
        content = tuple(sorted(c["digest"] for c in row.get("clips") or []))
    else:
        ids = frozenset(d["device_id"] for d in row.get("devices") or [])
        content = tuple(sorted((str(d.get("name")), str(d.get("type")))
                               for d in row.get("devices") or []))
    by_id = (row.get("kind"), identity.get("set_id"), identity.get("runtime_epoch"))
    by_content = (row.get("kind"), identity.get("set_id"), track.get("name"), content)
    return by_id, by_content, ids


def _existing_row(record: dict, pending: list[dict]) -> tuple[dict | None, str, str | None]:
    """How a verified record relates to the PENDING rows: ``(row, "existing",
    None)`` when a row already holds exactly these ids on this track in this
    run; ``(row, "rebind", None)`` when a row holds these very ids on another
    track (moved), or the same material under ids the current read no longer
    shows (deleted and written again in this run, or another Live run) — a
    pending unjudged row takes the verified track/ids; ``(row, "refused",
    reason)`` when that row is already judged (its verdict was given on the
    material as registered) or when the record overlaps a row's clips,
    whatever track either sits on; ``(None, "new", None)`` otherwise. A row
    is never handed back on ids the read shows gone, and never doubled."""
    by_id, by_content, ids = _material_keys(record)
    track_id = str((record.get("track") or {}).get("id"))
    for row in pending:
        row_by_id, row_by_content, row_ids = _material_keys(row)
        if row_by_id == by_id and row_ids == ids:
            if str((row.get("track") or {}).get("id")) == track_id:
                return row, "existing", None
            # the very objects, on another track now (he moved them): one row
            if row.get("immediate_verdict") is not None:
                return row, "refused", (
                    "%s (%s) holds these ids, now on track %s, and is already judged %s — "
                    "close_session() scores it where it sits; never a second row"
                    % (row["id"], row.get("label"), track_id, row["immediate_verdict"].upper()))
            return row, "rebind", None
        if row_by_content == by_content:
            if row.get("immediate_verdict") is not None:
                old_ids = ", ".join(sorted(row_ids)) or "none"
                return row, "refused", (
                    "%s (%s) holds this material under ids the Set no longer shows (%s) and "
                    "is already judged %s — close_session() resolves it; a new round can be "
                    "registered after that" % (row["id"], row.get("label"), old_ids,
                                               row["immediate_verdict"].upper()))
            return row, "rebind", None
        if row_by_id == by_id and (row_ids & ids):
            shared = sorted(row_ids & ids)
            return row, "refused", (
                "%s %s already registered under %s (%s) — one clip, one row"
                % ("clip" if len(shared) == 1 else "clips", ", ".join(shared),
                   row["id"], row.get("label")))
    return None, "new", None


def _closed_match(record: dict, closed: list[dict]) -> tuple[dict | None, str | None]:
    """The MOST RECENTLY closed row of this material and how the record
    relates to it: ``(row, None)`` when nothing changed since it was scored
    (same Set, same run, same ids, same content digest), else ``(row, what
    differs)``: ``"content"`` (the same ids in the same run, other notes),
    ``"epoch"`` (the same content in another Live run) or ``"ids"`` (the
    same content under other ids in the same run — deleted and written
    again); ``(None, None)`` when no closed row relates. Newest verdict
    first, so an older verdict on the same material never decides."""
    by_id, by_content, ids = _material_keys(record)
    kind, set_id, epoch = by_id
    # the LATEST verdict on this material decides: material dropped,
    # re-proposed and then kept is met at its adopted row, never at the old
    # drop (which would reopen a round for ever); notes changed and changed
    # back are met at the round in between
    for row in sorted(closed, key=lambda r: str(r.get("terminal_at") or ""), reverse=True):
        row_by_id, row_by_content, row_ids = _material_keys(row)
        if row_by_id[:2] != (kind, set_id):
            continue
        same_run = row_by_id[2] == epoch
        same_ids = row_ids == ids
        same_content = row_by_content[3] == by_content[3]
        if same_run and same_ids and same_content:
            return row, None
        if same_content:
            return row, "epoch" if not same_run else "ids"
        if same_run and same_ids:
            return row, "content"
    return None, None


UNCHANGED_ROUNDS = ("epoch", "ids")    # the same material again, unchanged
SURVIVED_STATES = ARRANGEMENT_SURVIVED + ("kept",)   # closed rows whose material is in the score


def _survival_observation(record: dict, closed: dict, differs: str, now_iso: str) -> dict:
    """What a registration read proved about a CLOSED, survived row's
    material, kept on that row: the same content is present again (in
    another Live run, or under new ids in this one) — next-session survival
    as evidence on the row already in the score, never a second judged row.
    Keyed by (epoch, ids, digest) so a repeated registration records it
    once."""
    identity = record.get("set_identity") or {}
    track = record.get("track") or {}
    if record.get("kind") == ARRANGEMENT_KIND:
        ids = [c["clip_id"] for c in record.get("clips") or []]
    else:
        ids = [d["device_id"] for d in record.get("devices") or []]
    return {"at": now_iso, "kind": "present_again", "differs": differs,
            "epoch": identity.get("runtime_epoch"), "set_id": identity.get("set_id"),
            "track_id": track.get("id"), "track_name": track.get("name"),
            "track_muted": bool(track.get("muted")), "ids": ids,
            "digest": record.get("digest"),
            "closed_state": closed.get("terminal_state"), "via": "registration"}


def _same_observation(a: dict, b: dict) -> bool:
    return all(a.get(key) == b.get(key) for key in ("epoch", "ids", "digest"))


def _on_his_track(record: dict, data: dict) -> bool:
    """The verified record sits on a track that is his: not the sidekick's
    by the ledger's provenance (:func:`sidekick_tracks`) and not named
    ``SIDEKICK…`` — a name never grants his ownership, it can only withhold
    it (the created-tracks gap, see :func:`resolve_arrangement`)."""
    if record.get("kind") != ARRANGEMENT_KIND:
        return False
    track = record.get("track") or {}
    if str(track.get("name") or "").startswith(SIDEKICK_PREFIX):
        return False
    return str(track.get("id")) not in sidekick_tracks(data, record.get("set_identity"))


def _same_copy_as_closed(record: dict, closed: dict) -> bool:
    """The record is the very copy the closed row was scored on: the same
    Live run, on a track the close saw that material on."""
    same_run = ((record.get("set_identity") or {}).get("runtime_epoch")
                == (closed.get("set_identity") or {}).get("runtime_epoch"))
    where = ((closed.get("observation") or {}).get("where") or {}).values()
    return same_run and str((record.get("track") or {}).get("id")) in {str(v) for v in where}


def closed_relation(record: dict, data: dict) -> tuple[dict | None, str | None, str | None]:
    """How a verified record relates to the CLOSED rows → ``(closed row,
    differs, action)``; ``(None, None, None)`` when none relates. Actions:
    ``"refused"`` — the very objects, unchanged, of a row whose material
    survived (already in the score); ``"observed"`` — unchanged material of
    a survived row present again on a sidekick track (or the very copy it
    was scored on): a survival observation, never a second judged row;
    ``"round"`` — a new judgeable round that names the closed row: changed
    content (``content``), a copy of survived material on HIS track
    (``copy_on_his_track``), or ANY re-proposal of material whose row did
    not survive (``re-proposed_after_<abandoned|not_adopted|drop|removed>``
    — an identical answer he dropped or lost can be proposed and KEPT
    again; an observation there would foreclose judgment)."""
    closed, differs = _closed_match(
        record, [r for r in all_rows(data) if r.get("terminal_state") is not None])
    if closed is None:
        return None, None, None
    state = closed.get("terminal_state")
    if state not in SURVIVED_STATES:
        return closed, "re-proposed_after_%s" % state, "round"
    if differs is None:
        return closed, None, "refused"
    if differs in UNCHANGED_ROUNDS:
        if _on_his_track(record, data) and not _same_copy_as_closed(record, closed):
            return closed, "copy_on_his_track", "round"
        return closed, differs, "observed"
    return closed, differs, "round"


def observed_on(rows: list[dict], *, path: str | None = None) -> list[dict | None]:
    """Read-only preview of :func:`register`'s observation outcome: for each
    verified row, the closed row it would be recorded on (``{entry_id,
    label, state, closed_at, differs}``) or None."""
    data = _ledger.load(path or _ledger.LEDGER_PATH)
    out: list[dict | None] = []
    for record in rows:
        closed, differs, action = closed_relation(record, data)
        out.append({"entry_id": closed["id"], "label": closed.get("label"),
                    "state": closed.get("terminal_state"), "closed_at": closed.get("terminal_at"),
                    "differs": differs} if action == "observed" else None)
    return out


def _rebind(row: dict, record: dict, now_iso: str) -> dict:
    """Move a pending, unjudged row onto the objects the registration read
    just verified: identity (epoch), track, clips or devices. The old ids are
    kept on the row's ``rebinds`` history; the content is the same by
    construction (that is what matched)."""
    if row.get("kind") == ARRANGEMENT_KIND:
        old_ids = [c["clip_id"] for c in row.get("clips") or []]
        new_ids = [c["clip_id"] for c in record.get("clips") or []]
    else:
        old_ids = [d["device_id"] for d in row.get("devices") or []]
        new_ids = [d["device_id"] for d in record.get("devices") or []]
    row.setdefault("rebinds", []).append({
        "at": now_iso, "from_ids": old_ids, "to_ids": new_ids,
        "from_epoch": (row.get("set_identity") or {}).get("runtime_epoch"),
        "to_epoch": (record.get("set_identity") or {}).get("runtime_epoch"),
        "from_track_id": (row.get("track") or {}).get("id"),
        "to_track_id": (record.get("track") or {}).get("id")})
    from_track = (row.get("track") or {}).get("id")
    to_track = (record.get("track") or {}).get("id")
    if from_track != to_track:
        row["rebinds"][-1]["moved_to"] = {"from_track_id": from_track, "to_track_id": to_track}
    for key in ("set_identity", "track", "clips", "devices", "digest", "unregistered_on_track"):
        if key in record:
            row[key] = record[key]
    # another Live run renumbers the very track the material was written on
    # (the content matched on its same-named track): the origin follows it;
    # a move within a run does not change where the sidekick put it
    if row["rebinds"][-1]["from_epoch"] != row["rebinds"][-1]["to_epoch"]:
        was = (row.get("origin_track") or {}).get("sidekick", True)
        row["origin_track"] = dict(_origin_track(record), sidekick=was)
    return {old: new for old, new in zip(old_ids, new_ids)}


def _origin_track(record: dict, data: dict | None = None) -> dict:
    """Where this material was registered, by identity: the track's id in
    the Live run that issued it, its name then (a label), and whether that
    track is the SIDEKICK's — by the ledger's provenance at registration
    (:func:`sidekick_tracks`) or, failing that, by the conservative reading
    of its name (a ``SIDEKICK…`` name withholds his ownership; it never
    grants the sidekick's). A row registered on HIS track (a copy he made)
    is not the sidekick's origin."""
    track = record.get("track") or {}
    name = str(track.get("name") or "")
    known = sidekick_tracks(data, record.get("set_identity")) if data is not None else set()
    return {"id": str(track.get("id")), "name": track.get("name"),
            "epoch": (record.get("set_identity") or {}).get("runtime_epoch"),
            "sidekick": name.startswith(SIDEKICK_PREFIX) or str(track.get("id")) in known}


def sidekick_tracks(data: dict, identity: dict | None) -> set[str]:
    """Track ids the sidekick is known to have written on in one Live run
    of one Set — provenance from the ledger, never from names: the origin
    track of every arrangement row registered in that run, and the
    JAM/BOOTH track of every phrase write of that run. (A created-tracks
    record would extend this; none is kept today.)"""
    set_id = (identity or {}).get("set_id")
    epoch = (identity or {}).get("runtime_epoch")
    out: set[str] = set()
    if not set_id or not epoch:
        return out
    for row in data.get(ARRANGEMENT_LIST) or []:
        if (row.get("set_identity") or {}).get("set_id") != set_id:
            continue
        origin = row.get("origin_track") or _origin_track(row)
        if origin.get("epoch") == epoch and origin.get("id") and origin.get("sidekick", True):
            out.add(str(origin["id"]))
    for entry in data.get("entries") or []:
        ident = entry.get("set_identity") or {}
        ref = entry.get("clip_ref") or {}
        if ident.get("set_id") == set_id and ident.get("runtime_epoch") == epoch \
                and ref.get("track_id"):
            out.add(str(ref["track_id"]))
    return out


def register(rows: list[dict], *, path: str | None = None) -> list[dict]:
    """Write verified rows (from :func:`verify_candidate`) under the ledger
    lock, joining the open ledger session for their Set identity. Idempotent:
    material a pending row already holds under exactly these ids comes back
    as that row (``registration: "existing"``); the same material under ids
    the read no longer shows (deleted and written again in this run, or
    another Live run) rebinds the pending unjudged row to the verified ids
    (``"rebound"``, with the old→new map and a ``rebinds`` record on the
    row) — a judged row is ``"refused"`` instead, named, since its verdict
    was given on the old objects; a candidate overlapping a pending row's
    clips is ``"refused"`` with the holding row named; the rest are
    ``"new"``. Closed rows (:func:`closed_relation`): a SURVIVED row's very
    objects, unchanged, are refused ("closed as … nothing changed since");
    its unchanged content in another Live run, or under new ids in this
    one, is ``"observed"`` — a ``survival_observations`` record on the
    closed row (next-session presence, recorded once per epoch/ids/digest),
    never a second judged row; its changed content, or a copy on HIS track,
    is a new round naming the closed row (``new_round_of``); and ANY
    re-proposal of a row that did not survive (abandoned, not_adopted,
    drop, removed) is a new judgeable round (``differs:
    re-proposed_after_<state>``)."""
    path = path or _ledger.LEDGER_PATH
    out: list[dict] = []
    with _ledger._lock(path):
        data = _ledger._load_for_mutation(path)
        arrangement, effect = _lists(data)
        now = _ledger._now()
        for row in rows:
            record = dict(row)
            label = str(record.get("label") or "").strip()
            if label in ledger_ids(data):
                out.append({"registration": "refused", "kind": record.get("kind"),
                            "track": record.get("track"), "label": record.get("label"),
                            "reason": "label %r equals an existing ledger id — a label may "
                                      "never shadow an id; pick another label" % label})
                continue
            closed, differs, action = closed_relation(record, data)
            if action == "refused":
                out.append({"registration": "refused", "kind": record.get("kind"),
                            "track": record.get("track"), "label": record.get("label"),
                            "reason": "%s (%s) closed as %s at %s, nothing changed since — the "
                                      "experiment is already in the score; a new round needs "
                                      "changed material"
                                      % (closed["id"], closed.get("label"),
                                         closed["terminal_state"], closed.get("terminal_at"))})
                continue
            if action == "observed":
                observation = _survival_observation(record, closed, differs, _ledger._iso(now))
                observations = closed.setdefault("survival_observations", [])
                already = next((o for o in observations if _same_observation(o, observation)), None)
                if already is None:
                    observations.append(observation)
                out.append({"registration": "observed", "id": closed["id"],
                            "label": closed.get("label"), "kind": closed.get("kind"),
                            "track": record.get("track"), "state": closed["terminal_state"],
                            "closed_at": closed.get("terminal_at"), "differs": differs,
                            "observation": already or observation,
                            "already_recorded": already is not None,
                            "reason": "%s (%s) closed as %s at %s and the same material is "
                                      "present %s — %s as an observation on that row "
                                      "(next-session presence), not a second judged row; a new "
                                      "round needs changed material"
                                      % (closed["id"], closed.get("label"),
                                         closed["terminal_state"], closed.get("terminal_at"),
                                         "in this Live run under new ids" if differs == "ids"
                                         else "in another Live run",
                                         "already recorded" if already else "recorded")})
                continue
            existing, relation, why = _existing_row(record, pending_rows(data))
            if relation == "existing":
                out.append(dict(existing, registration="existing"))
                continue
            if relation == "rebind":
                mapping = _rebind(existing, record, _ledger._iso(now))
                moved_to = existing["rebinds"][-1].get("moved_to")
                out.append(dict(existing, registration="rebound",
                                rebound={o: n for o, n in mapping.items() if o != n},
                                moved_to=moved_to))
                continue
            if relation == "refused":
                out.append({"registration": "refused", "reason": why, "label": record.get("label"),
                            "kind": record.get("kind"), "track": record.get("track")})
                continue
            session = _ledger._active_session(data, now, set_identity=record["set_identity"])
            prefix = "arr-" if record["kind"] == ARRANGEMENT_KIND else "fx-"
            record["id"] = prefix + uuid.uuid4().hex[:10]
            record["session_id"] = session["id"]
            record["registered_at"] = _ledger._iso(now)
            record["origin_track"] = _origin_track(record, data)
            if closed is not None:
                record["new_round_of"] = {"entry_id": closed["id"], "state": closed["terminal_state"],
                                          "closed_at": closed.get("terminal_at"), "differs": differs}
            (arrangement if record["kind"] == ARRANGEMENT_KIND else effect).append(record)
            out.append(dict(record, registration="new"))
        _ledger._save(data, path)
    return out


def judge(handle: str, verdict: str, *, reason: str | None, path: str | None = None) -> dict | None:
    """Stamp one explicit verdict on one experiment row and record the
    feedback. ``None`` when no row answers to ``handle`` (the caller runs
    the phrase path); ValueError on a contradiction or a closed row."""
    verdict = str(verdict).strip().lower()
    if verdict not in ("keep", "drop"):
        raise ValueError("verdict must be 'keep' or 'drop'")
    path = path or _ledger.LEDGER_PATH
    with _ledger._lock(path):
        data = _ledger._load_for_mutation(path)
        row = find_row(data, handle)
        if row is None:
            return None
        if row.get("terminal_state") is not None:
            state = row["terminal_state"]
            if state in SURVIVED_STATES:
                raise ValueError("%s (%s) already closed as %s on %s — that material is already "
                                 "in the score (an unchanged sighting is an observation on it); "
                                 "change the material for a new round"
                                 % (row["id"], row.get("label"), state, row.get("terminal_at")))
            raise ValueError("%s (%s) already closed as %s on %s — register it again: a "
                             "re-proposal after %s is a new round you can judge"
                             % (row["id"], row.get("label"), state, row.get("terminal_at"), state))
        replay = False
        if row.get("immediate_verdict") is not None:
            if row["immediate_verdict"] != verdict:
                raise ValueError("verdict %s contradicts the recorded %s on %s (%s)"
                                 % (verdict, row["immediate_verdict"], row["id"], row.get("label")))
            replay = True
            reason = row.get("reason")
        else:
            now = _ledger._now()
            row["immediate_verdict"] = verdict
            row["reason"] = reason
            row["intent_revision"] = reason
            row["judged_at"] = _ledger._iso(now)
            row["feedback_recorded"] = False
            session = next((s for s in data.get("sessions", [])
                            if s.get("id") == row.get("session_id")), None)
            if session is not None and session in _ledger._live_sessions(data, now):
                session["last_activity"] = now
            _ledger._save(data, path)
        siblings = [{"entry_id": other["id"], "label": other.get("label")}
                    for other in all_rows(data)
                    if other is not row and other.get("group") == row.get("group")
                    and other.get("kind") == row.get("kind")
                    and other.get("immediate_verdict") is None
                    and other.get("terminal_state") is None]
        row_id = row["id"]
    # The feedback write is retried by every re-press until it lands: a
    # replay whose feedback never got recorded is not a no-op.
    feedback_recorded = bool(row.get("feedback_recorded"))
    if not feedback_recorded:
        try:
            _piece_context.record_decision(row["piece"], {
                "kind": "arrangement_feedback", "source": "model",
                "experiment": row["kind"], "entry_id": row["id"],
                "label": row.get("label"), "verdict": verdict, "reason": reason})
            feedback_recorded = True
        except (OSError, ValueError, TypeError):
            feedback_recorded = False
        if feedback_recorded:
            with _ledger._lock(path):
                data = _ledger._load_for_mutation(path)
                stored = next((r for r in all_rows(data) if r.get("id") == row_id), None)
                if stored is not None and not stored.get("feedback_recorded"):
                    stored["feedback_recorded"] = True
                    _ledger._save(data, path)
    note = ("%s recorded for %s (%s); nothing about any phrase was learned or banned"
            % (verdict.upper(), row["id"], row.get("label")))
    if siblings:
        note += ("; the other %d candidate(s) in this group stay unjudged until you judge "
                 "them: %s" % (len(siblings), ", ".join(
                     "%s (%s)" % (s["entry_id"], s["label"]) for s in siblings)))
    return {"verdict": verdict, "entry_id": row["id"], "label": row.get("label"),
            "kind": row["kind"], "reason": reason, "judged_at": row.get("judged_at"),
            "replay": replay, "routed": "arrangement_feedback",
            "feedback_recorded": feedback_recorded, "siblings_unjudged": siblings,
            "note": note}


# --- resolution (pure) ----------------------------------------------------------

def _set_matches(row: dict, evidence: dict, chain: set) -> bool:
    """The evidence is about the row's Set: same set_id, or both ids in the
    row's session chain (first save, Save-As)."""
    identity = evidence.get("set_identity") or {}
    row_identity = row.get("set_identity") or {}
    set_id = identity.get("set_id")
    if not set_id or not row_identity.get("set_id"):
        return False
    return set_id == row_identity["set_id"] or (set_id in chain and row_identity["set_id"] in chain)


def _same_epoch(row: dict, evidence: dict) -> bool:
    """Runtime ids are meaningful only inside the Live run that issued them
    (the phrase sweep's rule: ``runtime_id_absent`` counts only in the same
    epoch). A restart renumbers everything — or, by coincidence, keeps a
    number for something else."""
    row_epoch = (row.get("set_identity") or {}).get("runtime_epoch")
    return bool(row_epoch) and row_epoch == (evidence.get("set_identity") or {}).get("runtime_epoch")


def _silenced(track_id: str | None, evidence: dict) -> bool:
    """Muted itself, or inside a muted group (any ancestor)."""
    tracks = evidence.get("tracks") or {}
    groups = evidence.get("groups") or {}
    node = tracks.get(track_id) if track_id is not None else None
    if node is None:
        return True
    seen: set[str] = set()
    while node is not None:
        if node.get("muted"):
            return True
        parent = node.get("group_id")
        if not parent or parent in seen:
            return False
        seen.add(parent)
        node = groups.get(str(parent))
        if node is None:
            return False
    return False


AUDIBILITY = ("audible", "silent_by_mute", "silent_by_solo", "parent_unknown", "state_unknown")
# Track states the overview reader has seen Producer Pal report (every capture
# shows "muted" only, with the state omitted when active; "soloed" is what the
# reader derives); any other string is a state no capture ever showed, and a
# solo could hide in it.
_KNOWN_STATES = (None, "", "active", "muted", "soloed")
# When the copies of one row (or a track and its own clip) disagree, the most
# definite silence wins, then the unknowns, then the solo; audible only when all are.
_AUDIBILITY_RANK = {"silent_by_mute": 0, "state_unknown": 1, "parent_unknown": 2,
                    "silent_by_solo": 3, "audible": 4}


def _audibility(track_id: str | None, evidence: dict) -> str:
    """Effective audibility of a track at close, from the overview's mute /
    solo states and its group tree (two-month review finding 6: a KEEP row
    used to close ``adopted`` while another track's solo silenced it):

    - ``silent_by_mute`` — the track or an ancestor group is muted
    - ``state_unknown`` — the track is not in the overview, or a track or
      group anywhere in the Set carries a state the captures never showed
    - ``parent_unknown`` — the track names a group the evidence does not
      hold: a muted or soloed parent would decide, and it was not read
    - ``silent_by_solo`` — some track or group in the Set is soloed and
      neither this track nor an ancestor is
    - ``audible`` — none of the above

    Not a model of routing: a track whose output feeds nothing audible, or
    sidechain-only material, still reads as audible here.
    """
    tracks = evidence.get("tracks") or {}
    groups = evidence.get("groups") or {}
    everyone = list(tracks.values()) + list(groups.values())
    node = tracks.get(track_id) if track_id is not None else None
    muted = chain_soloed = parent_unknown = False
    seen: set[str] = set()
    while node is not None:
        muted = muted or bool(node.get("muted"))
        chain_soloed = chain_soloed or bool(node.get("soloed"))
        parent = node.get("group_id")
        if not parent or parent in seen:
            break
        seen.add(parent)
        node = groups.get(str(parent))
        parent_unknown = parent_unknown or node is None
    if muted:
        return "silent_by_mute"
    if track_id is None or track_id not in tracks \
            or any(n.get("state") not in _KNOWN_STATES for n in everyone):
        return "state_unknown"
    if parent_unknown:
        return "parent_unknown"
    if any(n.get("soloed") for n in everyone) and not chain_soloed:
        return "silent_by_solo"
    return "audible"


def clip_object(read: dict | None) -> tuple[str, int | None]:
    """What a by-id read says the id names now: ``("clip", track index)``
    for a complete read of a clip on a regular track, ``("other", None)``
    when the id names something else (recycled), ``("missing", None)`` when
    Live says it does not exist, ``("unreadable", None)`` otherwise."""
    if not read:
        return "unreadable", None
    if read.get("mismatch"):
        return "other", None
    if read.get("error"):
        return ("missing" if read.get("missing") else "unreadable"), None
    try:
        from mcp_server import live_write as _live_write
    except ModuleNotFoundError:  # pragma: no cover
        import live_write as _live_write
    index = _live_write.clip_object_track(read.get("object_path"))
    if index is None:
        return "other", None
    return "clip", index


def _on_track(read: dict | None, track: dict | None) -> bool:
    """The read is a clip whose parent is ``track`` (by index)."""
    kind, index = clip_object(read)
    return kind == "clip" and track is not None and track.get("index") == index


def _tracks_named(evidence: dict, name: str) -> list[str]:
    return sorted(tid for tid, track in (evidence.get("tracks") or {}).items()
                  if track.get("name") == name)


def _rebind_clips(row: dict, evidence: dict) -> tuple[dict, str | None]:
    """Cross-epoch: each registered clip found by content digest on a track
    with the registered track's NAME. ``(old_id → new_id)`` when every clip
    matches exactly once; otherwise the reason it could not."""
    locations = evidence.get("clip_locations") or {}
    reads = evidence.get("clips") or {}
    home = set(_tracks_named(evidence, str((row.get("track") or {}).get("name"))))
    if not home:
        return {}, "cross_epoch_unresolved"
    digests: dict[str, str] = {}
    tracks = evidence.get("tracks") or {}
    for cid, loc in locations.items():
        if str(loc.get("track_id")) not in home or loc.get("surface") != "arrangement":
            continue
        live = reads.get(cid)
        if not live or live.get("error") or not _on_track(live, tracks.get(str(loc.get("track_id")))):
            continue
        digests[cid] = content_digest(live)
    bound: dict[str, str] = {}
    for clip in row.get("clips") or []:
        matches = [cid for cid, digest in digests.items()
                   if digest == clip["digest"] and cid not in bound.values()]
        if len(matches) > 1:
            return {}, "cross_epoch_ambiguous"
        if not matches:
            return {}, "cross_epoch_unresolved"
        bound[clip["clip_id"]] = matches[0]
    return bound, None


def resolve_arrangement(row: dict, evidence: dict, *, chain: set = frozenset(),
                        sidekick_tracks: set | frozenset = frozenset()) -> tuple:
    """→ ``(terminal_state | None, observation)`` per the contract above.
    ``sidekick_tracks`` are the ids of the tracks the sidekick is known to
    have written on in the evidence's Live run (:func:`sidekick_tracks`);
    with the row's own origin track they decide ``on_his_originals`` by
    identity — a track's name is never proof either way."""
    verdict = row.get("immediate_verdict")
    obs: dict = {"state": None, "reason": None, "same_epoch": None, "rebound": {},
                 "present": [], "absent": [], "where": {}, "changed": [], "moved": [],
                 "audible": None, "audibility": None, "unmute_observed": None}

    def unresolved(reason: str) -> tuple:
        obs.update(state="drop" if verdict == "drop" else "unknown", reason=reason)
        return ("drop" if verdict == "drop" else None), obs

    if not _set_matches(row, evidence, chain):
        return unresolved("identity_mismatch")
    if evidence.get("complete") is not True:
        return unresolved("inventory_incomplete")
    same_epoch = _same_epoch(row, evidence)
    obs["same_epoch"] = same_epoch
    locations = evidence.get("clip_locations") or {}
    reads = evidence.get("clips") or {}
    tracks = evidence.get("tracks") or {}
    track_name = str((row.get("track") or {}).get("name") or "")
    track_id = str((row.get("track") or {}).get("id"))
    before = {c["clip_id"]: c for c in row.get("clips") or []}
    if verdict == "drop":
        obs.update(state="drop", reason="owner_drop")
        return "drop", obs
    if same_epoch:
        clip_ids = list(before)
        present = [cid for cid in clip_ids if cid in locations]
        absent = [cid for cid in clip_ids if cid not in locations]
        obs.update(present=present, absent=absent,
                   where={cid: str(locations[cid].get("track_id")) for cid in present})
        # An id the inventory lacks is gone only when Live itself says the id
        # does not exist: ids are reused within a run, so one that still
        # answers — as another object, or as a clip the inventory did not
        # list — proves nothing either way.
        for cid in absent:
            kind, _ = clip_object(reads.get(cid))
            if kind == "other":
                return unresolved("id_recycled:%s" % ((reads.get(cid) or {}).get("object_path")
                                                       or "unknown object"))
            if kind == "clip":
                return unresolved("inventory_disagrees")
            if kind == "unreadable":
                return unresolved("id_unreadable")
        if not present:
            # verified absence: a complete inventory of the SAME Live run and
            # Live's own "does not exist" for every id
            reason = "track_gone" if track_id not in tracks else "clips_gone"
            state = "not_adopted" if verdict == "keep" else "abandoned"
            obs.update(state=state, reason=reason)
            return state, obs
        if absent:
            return unresolved("partial_presence")
        failed = [cid for cid in present if not reads.get(cid) or reads[cid].get("error")]
        if failed:
            obs["failed"] = failed
            return unresolved("clip_read_failed")
        mismatched = [cid for cid in present
                      if not _on_track(reads[cid], tracks.get(obs["where"][cid]))]
        if mismatched:
            obs["mismatched"] = mismatched
            return unresolved("object_mismatch")
        changed = [cid for cid in present if content_digest(reads[cid]) != before[cid]["digest"]]
        obs["changed"] = changed
        if changed:
            return unresolved("content_changed")
        found = {cid: cid for cid in present}
    else:
        # another Live run: ids prove nothing either way — content on the
        # same-named track does, or the row stays unresolved
        bound, why = _rebind_clips(row, evidence)
        if why is not None:
            return unresolved(why)
        obs["rebound"] = dict(bound)
        found = bound
        obs.update(present=sorted(bound.values()), absent=[],
                   where={new: str(locations[new].get("track_id")) for new in bound.values()})
    obs["moved"] = sorted(old for old, new in found.items()
                          if (reads[new].get("frame") or {}).get("arr_start") != before[old].get("arr_start"))
    # Whose track holds it now is decided by identity: the track the
    # material was registered on (the row's origin in this run — the
    # sidekick's, or his own when the row is a copy he made) and any other
    # track the sidekick is known to have written on keep the SIDEKICK
    # classes — a rename is an observation; his originals are the rest. A
    # NAME never grants the stronger class: with no created-tracks record,
    # a ``SIDEKICK…``-named destination outside the ledger's provenance is
    # read as the sidekick's (``on_sidekick_named_track``) — the gap is that
    # an unrecorded sidekick-created track under another name would read as
    # his. In another run the material was matched on its own same-named
    # track, so nothing there is his originals.
    origin = row.get("origin_track") or _origin_track(row)
    origin_id = str(origin.get("id")) if same_epoch and origin.get("epoch") == \
        (evidence.get("set_identity") or {}).get("runtime_epoch") else None
    audibility = "audible"
    on_his_track = False
    on_sidekick: dict[str, str] = {}
    on_named: dict[str, str] = {}
    for old, new in found.items():
        here = str(locations[new].get("track_id"))
        here_name = str((tracks.get(here) or {}).get("name") or "")
        # the track's effective audibility (mutes, solos, group tree), and
        # the clip's own deactivation
        audibility = min(audibility, _audibility(here, evidence),
                         "silent_by_mute" if (reads[new].get("frame") or {}).get("muted")
                         else "audible", key=_AUDIBILITY_RANK.__getitem__)
        if not same_epoch:
            continue
        if here == origin_id:
            if here_name != str(origin.get("name") or ""):
                obs["sidekick_track_renamed"] = {"from": origin.get("name"), "to": here_name}
            if not origin.get("sidekick", True):
                on_his_track = True  # registered on his track (his copy) and still there
        elif here in sidekick_tracks:
            on_sidekick[new] = here
        elif here_name.startswith(SIDEKICK_PREFIX):
            on_named[new] = here
        else:
            on_his_track = True      # on a track the sidekick never wrote on: his
    if on_sidekick:
        obs["on_sidekick_track"] = on_sidekick
    if on_named:
        obs["on_sidekick_named_track"] = on_named
    was_muted = bool((row.get("track") or {}).get("muted"))
    home = [track_id] if same_epoch and track_id in tracks else _tracks_named(evidence, track_name)
    now_muted = (_silenced(home[0], evidence) if len(home) == 1 else None)
    audible = audibility == "audible"
    obs.update(audible=audible, audibility=audibility,
               unmute_observed=bool(was_muted and now_muted is False))
    if verdict is None:
        obs.update(state="unjudged", reason="present_unjudged")
        return None, obs
    if audible:
        obs.update(state="adopted",
                   reason=("on_his_originals" if on_his_track
                           else "unmuted_since_registration" if obs["unmute_observed"]
                           else "still_audible_on_sidekick"))
        return "adopted", obs
    obs.update(state="staged_kept", reason="present_but_silent")
    return "staged_kept", obs


# A device path on any track: a regular track (``t24/d4``, or a rack member
# ``t24/d4/c0/d1``), a return (``rt0/d0``) or the main track (``mt/d0``).
# Anything else an id names (``t41/arr0``, a clip; a track) is not a device.
_DEVICE_PATH = re.compile(r"^(?:t(\d+)|rt\d+|mt)/d\d+(?:/|$)")


def _is_device_path(path) -> bool:
    return _DEVICE_PATH.match(str(path or "")) is not None


def _device_parent(path) -> int | None:
    """The REGULAR-track index a device path sits under; None for a return,
    the main track, or anything that is not a device path."""
    match = _DEVICE_PATH.match(str(path or ""))
    return int(match.group(1)) if match and match.group(1) is not None else None


def _device_mismatch(device_id: str, entry: dict, read: dict | None, track: dict,
                     registered_type: str | None = None) -> str | None:
    """Why the evidence about one LISTED device contradicts itself, or None
    when it binds. ``entry`` is the device's record in ``track``'s own device
    list: it must sit under the track's path and, at close, carry the type it
    was registered as. ``read`` is the by-id read: one that answered must
    name this very device — same id, same type, same parent track. A read
    that failed outright is not a contradiction (its parameters are simply
    unread); one that answered for ANOTHER id is (``mismatch``, like a clip
    read landing on another object)."""
    index = track.get("index")
    if index is None or _device_parent(entry.get("path")) != index:
        return ("evidence_mismatch:device_path (device %s is listed at %s, track %s is t%s)"
                % (device_id, entry.get("path") or "no path", track.get("id"), index))
    if registered_type is not None and entry.get("type") != registered_type:
        return ("evidence_mismatch:device_type (device %s is listed as %s, registered as %s)"
                % (device_id, entry.get("type") or "no type", registered_type))
    if not read:
        return None
    if read.get("mismatch"):
        return str(read.get("error") or "evidence_mismatch:device_id")
    if read.get("error"):
        return None
    if str(read.get("id")) != device_id:
        return ("evidence_mismatch:device_id (asked for id %s, Live answered %s)"
                % (device_id, read.get("id") or "no id"))
    if not read.get("type") or read.get("type") != entry.get("type"):
        return ("evidence_mismatch:device_type (device %s reads as %s, listed as %s)"
                % (device_id, read.get("type") or "no type", entry.get("type") or "no type"))
    if _device_parent(read.get("path")) != index:
        return ("evidence_mismatch:device_parent (device %s reads at %s, track %s is t%s)"
                % (device_id, read.get("path") or "no path", track.get("id"), index))
    return None


def _absent_device(device_id: str, read: dict | None, tracks: dict, track: dict,
                   registered: dict | None = None) -> tuple:
    """What the by-id read says about a device the bound listing of its own
    track LACKS → ``(fate, reason, detail)``. Absence is proven, never
    inferred from the silent listing: ``("gone", …)`` ONLY when Live itself
    said this id does not exist (``missing`` — ``is_missing_id_error`` for
    this very id); a result-less, warning-only, truncated, empty or null
    reply is a failed read, not Live's word. (The 2.4.0 captures hold no
    ppal-read-device of a missing id: on 2.4 a deleted device stays
    ``unknown`` unless the device answers the 2.3 text for that id.) ``("moved", <where>, …)`` when
    the id still reads as THIS device — same type and name as registered —
    on another track, a return, the main track or inside a rack: it exists,
    so it is neither gone nor kept; ``("unknown", <reason>, <detail>)`` for
    everything else: no read (``device_unread``), a failed read
    (``device_read_failed``), a read for another id
    (``evidence_mismatch:device_id``), an id now naming something that is
    not a device, or another device — other type or name
    (``id_recycled:<path>``), or a device-shaped read placing it on this
    very track (``evidence_mismatch:listing``)."""
    if not read:
        return "unknown", "device_unread", "device %s: no by-id read" % device_id
    if read.get("missing"):
        return "gone", "devices_gone", str(read.get("error") or "")
    if read.get("mismatch"):
        detail = str(read.get("error") or "evidence_mismatch:device_id")
        return "unknown", detail.split(" ", 1)[0], detail
    if read.get("error"):
        return "unknown", "device_read_failed", "device %s: %s" % (device_id, read["error"])
    if str(read.get("id")) != device_id:
        detail = ("evidence_mismatch:device_id (asked for id %s, Live answered %s)"
                  % (device_id, read.get("id") or "no id"))
        return "unknown", "evidence_mismatch:device_id", detail
    path = read.get("path")
    if not read.get("type") or not _is_device_path(path):
        reason = "id_recycled:%s" % (path or "no path")
        return "unknown", reason, ("device %s now reads as %s%s — not a device"
                                   % (device_id, path or "no path",
                                      "" if read.get("type") else " without a type"))
    parent = _device_parent(path)
    if parent is not None and parent == track.get("index"):
        detail = ("evidence_mismatch:listing (device %s reads at %s, yet track %s's device "
                  "list lacks it)" % (device_id, path, track.get("id")))
        return "unknown", "evidence_mismatch:listing", detail
    registered = registered or {}
    if registered and (read.get("type") != registered.get("type")
                       or (read.get("name") or None) != (registered.get("name") or None)):
        # the id names a device elsewhere, but not THIS one: reused
        return "unknown", "id_recycled:%s" % path, (
            "device %s now reads as %s %r at %s, registered as %s %r"
            % (device_id, read.get("type"), read.get("name"), path,
               registered.get("type"), registered.get("name")))
    by_index = {t.get("index"): t for t in tracks.values()}
    home = by_index.get(parent) if parent is not None else None
    where = str(home.get("name") or home.get("id")) if home else str(path)
    return "moved", where, "device %s reads at %s" % (device_id, path)


def _params_facts(devices: list[dict], present: list[str], reads: dict) -> dict:
    """Parameter evidence for the devices present, reported apart from
    presence and never silently degraded: ``params_coverage`` is what the
    close read could see (full / partial / none, over all present devices),
    ``params_compared`` is True only when EVERY present device was read in
    full both at registration and now, ``params_changed`` lists the compared
    devices whose parameters differ, ``params_uncompared`` the rest, and
    ``params_error`` says why (a failed read, an unreadable shape, a
    registration that saw nothing)."""
    compared: list[str] = []
    changed: list[str] = []
    coverage: dict[str, str] = {}
    errors: list[str] = []
    for device in devices:
        device_id = device["device_id"]
        if device_id not in present:
            continue
        read = reads.get(device_id)
        if not read or read.get("error"):
            coverage[device_id] = "none"
            errors.append("%s: %s" % (device_id, (read or {}).get("error") or "no device read"))
            continue
        now = read.get("params_coverage") or ("full" if read.get("params") else "none")
        coverage[device_id] = now
        if read.get("params_error"):
            errors.append("%s: %s" % (device_id, read["params_error"]))
        before = device.get("params_coverage") or ("full" if device.get("params") else "none")
        if before != "full":
            errors.append("%s: registered with %s parameter coverage%s"
                          % (device_id, before,
                             " (%s)" % device["params_error"] if device.get("params_error") else ""))
        if now == "full" and before == "full":
            compared.append(device_id)
            if params_digest(read.get("params")) != device["params_digest"]:
                changed.append(device_id)
    levels = set(coverage.values())
    return {"params_coverage": (None if not present else "full" if levels == {"full"}
                                else "none" if levels <= {"none"} else "partial"),
            "params_compared": bool(present) and len(compared) == len(present),
            "params_changed": changed,
            "params_uncompared": [d for d in present if d not in compared],
            "params_error": errors or None}


def resolve_effect(row: dict, evidence: dict, *, chain: set = frozenset()) -> tuple:
    verdict = row.get("immediate_verdict")
    obs: dict = {"state": None, "reason": None, "same_epoch": None, "rebound": {},
                 "present": [], "absent": [], "params_changed": [], "params_compared": None,
                 "params_coverage": None, "params_uncompared": [], "params_error": None}

    def unresolved(reason: str) -> tuple:
        obs.update(state="drop" if verdict == "drop" else "unknown", reason=reason)
        return ("drop" if verdict == "drop" else None), obs

    if not _set_matches(row, evidence, chain):
        return unresolved("identity_mismatch")
    if evidence.get("complete") is not True:
        return unresolved("inventory_incomplete")
    same_epoch = _same_epoch(row, evidence)
    obs["same_epoch"] = same_epoch
    if verdict == "drop":
        obs.update(state="drop", reason="owner_drop")
        return "drop", obs
    tracks = evidence.get("tracks") or {}
    track_id = str((row.get("track") or {}).get("id"))
    track_name = str((row.get("track") or {}).get("name") or "")
    devices = row.get("devices") or []
    listings = evidence.get("track_devices") or {}
    reads = evidence.get("devices") or {}
    if same_epoch:
        if track_id not in tracks:
            return unresolved("track_gone")
        track = tracks[track_id]
        listed = listings.get(track_id)
        if listed is None:
            detail = (evidence.get("track_device_errors") or {}).get(track_id)
            if detail and str(detail).startswith("evidence_mismatch:"):
                obs["detail"] = str(detail)
                return unresolved(str(detail).split(" ", 1)[0])
            return unresolved("devices_unread")
        by_id = {str(d.get("id")): d for d in listed}
        present = [d["device_id"] for d in devices if d["device_id"] in by_id]
        absent = [d["device_id"] for d in devices if d["device_id"] not in by_id]
        obs.update(present=present, absent=absent)
        # the evidence must bind before it counts: each listed device under
        # this track's path and of its registered type, each by-id read that
        # answered naming that very device — a contradiction is unknown
        for device in devices:
            if device["device_id"] not in by_id:
                continue
            why = _device_mismatch(device["device_id"], by_id[device["device_id"]],
                                   reads.get(device["device_id"]), track,
                                   registered_type=device.get("type"))
            if why is not None:
                obs["detail"] = why
                return unresolved(why.split(" ", 1)[0])
        obs.update(_params_facts(devices, present, reads))
        # a device the listing lacks is gone only on Live's own word; a read
        # that still names a device elsewhere is a move; anything else stays
        # unknown — a silent listing never proves absence
        moved: dict[str, str] = {}
        registered = {d["device_id"]: d for d in devices}
        for device_id in absent:
            fate, reason, detail = _absent_device(device_id, reads.get(device_id), tracks, track,
                                                  registered=registered.get(device_id))
            if fate == "unknown":
                obs["detail"] = detail
                return unresolved(reason)
            if fate == "moved":
                moved[device_id] = reason
        if moved:
            # the device exists, on another track: neither gone nor kept
            obs["moved"] = moved
            return unresolved("device_moved:%s" % ", ".join(sorted(set(moved.values()))))
        if not present:
            state = "removed" if verdict == "keep" else "abandoned"
            obs.update(state=state, reason="devices_gone")
            return state, obs
        if absent:
            return unresolved("partial_presence")
    else:
        # another Live run: the same-named track, a device of the same name
        # and type, exactly one of each — parameters are not compared
        home = _tracks_named(evidence, track_name)
        if len(home) != 1:
            return unresolved("cross_epoch_ambiguous" if home else "cross_epoch_unresolved")
        listed = listings.get(home[0])
        if listed is None:
            return unresolved("devices_unread")
        bound: dict[str, str] = {}
        for device in devices:
            matches = [str(d.get("id")) for d in listed
                       if d.get("name") == device.get("name") and d.get("type") == device.get("type")
                       and str(d.get("id")) not in bound.values()]
            if len(matches) > 1:
                return unresolved("cross_epoch_ambiguous")
            if not matches:
                return unresolved("cross_epoch_unresolved")
            bound[device["device_id"]] = matches[0]
        obs.update(rebound=dict(bound), present=sorted(bound.values()), absent=[],
                   params_compared=False, params_coverage=None,
                   params_uncompared=[d["device_id"] for d in devices],
                   params_error=["parameters are not compared across Live runs"])
    if verdict is None:
        obs.update(state="unjudged", reason="present_unjudged")
        return None, obs
    obs.update(state="kept", reason="present_on_his_track")
    return "kept", obs


# --- sweep + report --------------------------------------------------------------

def sessions_for(data: dict, evidence_identity: dict | None) -> list[str]:
    """The sessions one evidence envelope may score: every session holding
    a pending row whose chain contains the inventoried Set — never another
    Set's night (mirrors the phrase sweep's _sweep_targets)."""
    set_id = (evidence_identity or {}).get("set_id")
    if not set_id:
        return []
    sessions = {s.get("id"): s for s in data.get("sessions", [])}
    out: list[str] = []
    for row in pending_rows(data):
        sid = row.get("session_id")
        if sid in out or sid not in sessions:
            continue
        if set_id in _ledger._session_chain(sessions[sid]):
            out.append(sid)
    return out


def sweep(evidence: dict, *, session_ids: list[str] | None = None,
          path: str | None = None) -> dict:
    """Resolve the pending rows of the night(s) being closed against one
    evidence envelope (identity, complete inventory, Live API clip reads,
    device lists). ``session_ids`` names them; by default they are the
    sessions of the evidence's Set that hold pending rows. Rows of any other
    session keep their observation untouched. Terminal states land at once;
    unresolved rows keep their observation and stay pending."""
    path = path or _ledger.LEDGER_PATH
    with _ledger._lock(path):
        data = _ledger._load_for_mutation(path)
        sessions = {s.get("id"): s for s in data.get("sessions", [])}
        targets = list(session_ids) if session_ids is not None else \
            sessions_for(data, evidence.get("set_identity"))
        now_iso = _ledger._iso(_ledger._now())
        known = sidekick_tracks(data, evidence.get("set_identity"))
        for row in pending_rows(data):
            if row.get("session_id") not in targets:
                continue
            chain = _ledger._session_chain(sessions.get(row.get("session_id")))
            if row.get("kind") == ARRANGEMENT_KIND:
                state, observation = resolve_arrangement(row, evidence, chain=chain,
                                                         sidekick_tracks=known)
            else:
                state, observation = resolve_effect(row, evidence, chain=chain)
            observation["swept_at"] = now_iso
            row["observation"] = observation
            if state is not None:
                row["terminal_state"] = state
                row["terminal_at"] = now_iso
        _ledger._save(data, path)
        return report(data, session_ids=targets)


def _entry_view(row: dict) -> dict:
    observation = row.get("observation") or {}
    view = {"entry_id": row["id"], "label": row.get("label"), "kind": row.get("kind"),
            "verdict": row.get("immediate_verdict"), "reason": row.get("reason"),
            "state": row.get("terminal_state") or observation.get("state") or "pending",
            "why": observation.get("reason")}
    if row.get("kind") == ARRANGEMENT_KIND:
        # adopted means audible; staged_kept says why it is not (or not provably)
        view["audibility"] = observation.get("audibility")
    if row.get("kind") == EFFECT_KIND:
        # presence and parameters are separate facts: "kept" never implies
        # the parameters were read, let alone unchanged
        view.update(params_compared=observation.get("params_compared"),
                    params_coverage=observation.get("params_coverage"),
                    params_changed=list(observation.get("params_changed") or []),
                    params_error=observation.get("params_error"))
    return view


EFFECT_NOTE = ("kept = the device is still present on his track; its parameters are a separate "
               "fact (kept_params_unchanged / kept_params_changed / kept_params_uncompared) — "
               "none of this is evidence the effect is audible (sidechain routing unverified)")


def _effect_facts(rows: list[dict]) -> dict:
    kept = [r.get("observation") or {} for r in rows if r.get("terminal_state") == "kept"]
    return {"kept_params_unchanged": sum(1 for o in kept if o.get("params_compared")
                                         and not o.get("params_changed")),
            "kept_params_changed": sum(1 for o in kept if o.get("params_compared")
                                       and o.get("params_changed")),
            "kept_params_uncompared": sum(1 for o in kept if not o.get("params_compared")),
            "note": EFFECT_NOTE}


THIN_SAMPLE = 8      # the phrase rate's threshold (ledger._merge_session_reports)


def _subtotal(rows: list[dict], terminal: tuple, survived: tuple, rate_name: str) -> dict:
    """``survived ÷ resolved_judged`` — published only when every explicit
    verdict has a terminal state (``rate_status: definitive``); while a
    judged row is unresolved the rate is withheld (``None``, ``partial``)
    and ``coverage`` says how many verdicts the resolved count covers. An
    unknown is never turned into a drop. Under ``THIN_SAMPLE`` judgments
    the rate carries the phrase rate's caution."""
    out = {"rows": len(rows)}
    for state in terminal:
        out[state] = sum(1 for r in rows if r.get("terminal_state") == state)
    pending = [r for r in rows if r.get("terminal_state") is None]
    out["unknown"] = sum(1 for r in pending
                         if (r.get("observation") or {}).get("state") == "unknown")
    out["unjudged"] = sum(1 for r in pending if r.get("immediate_verdict") is None)
    out["awaiting_close"] = sum(1 for r in pending if r.get("immediate_verdict") is not None)
    out["judged_total"] = sum(1 for r in rows if r.get("immediate_verdict") is not None)
    out["resolved_judged"] = sum(out[state] for state in terminal if state != "abandoned")
    out["survived"] = sum(out[state] for state in survived)
    if "adopted" in terminal:
        # adopted is not one thing: on his originals (PLAN §1.6's class), an
        # unmute since registration, or merely still audible where it was
        # written — reported apart so the weaker class never reads as the stronger
        out["adopted_by"] = {why: sum(1 for r in rows if r.get("terminal_state") == "adopted"
                                      and (r.get("observation") or {}).get("reason") == why)
                             for why in ADOPTED_REASONS}
    definitive = out["awaiting_close"] == 0
    out["rate_status"] = "definitive" if definitive else "partial"
    out["coverage"] = {"resolved_judged": out["resolved_judged"], "judged_total": out["judged_total"]}
    out[rate_name] = (round(out["survived"] / out["resolved_judged"], 3)
                      if definitive and out["resolved_judged"] else None)
    if not definitive:
        out["warning"] = ("%d judged row(s) remain unresolved — the %s is withheld "
                          "(resolved %d/%d verdicts); close again with the Set open, or settle "
                          "them by his word" % (out["awaiting_close"], rate_name.replace("_", " "),
                                                out["resolved_judged"], out["judged_total"]))
    elif rows and out["judged_total"] == 0:
        out["warning"] = ("nothing judged yet — the %s is undefined, not 0 (%d unjudged, "
                          "%d abandoned)" % (rate_name.replace("_", " "), out["unjudged"],
                                             out["abandoned"]))
    elif 0 < out["judged_total"] < THIN_SAMPLE:
        out["warning"] = ("only %d judged — too thin to read as a rate; let it accumulate "
                          "before drawing a conclusion" % out["judged_total"])
    out["entries"] = [_entry_view(r) for r in rows]
    return out


def _without_unchanged_rounds(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Rows registered by the old code as a second round of unchanged
    material (``new_round_of.differs`` epoch/ids) are one suggestion with
    their parent and are never counted — not beside the parent, and not
    alone in a window the parent falls outside of (that would count the
    suggestion under a night it was not judged in). They are listed apart,
    with whether the parent is in this report."""
    ids = {r.get("id") for r in rows}
    excluded, kept = [], []
    for row in rows:
        follows = row.get("new_round_of") or {}
        if follows.get("differs") in UNCHANGED_ROUNDS:
            excluded.append({"entry_id": row.get("id"), "of": follows.get("entry_id"),
                             "differs": follows.get("differs"),
                             "parent_in_report": follows.get("entry_id") in ids})
        else:
            kept.append(row)
    return kept, excluded


def report(data: dict, session_ids: list[str] | None = None) -> dict:
    """The subtotal of the named sessions only (never a total over every
    night); ``session_ids`` None means the newest session holding rows."""
    arrangement, effect = _lists(data)
    if session_ids is None:
        newest = None
        for session in data.get("sessions", []):
            if any(r.get("session_id") == session.get("id") for r in all_rows(data)):
                newest = session.get("id")
        session_ids = [newest] if newest else []
    chosen = set(session_ids)
    arrangement, arrangement_excluded = _without_unchanged_rounds(
        [r for r in arrangement if r.get("session_id") in chosen])
    effect, effect_excluded = _without_unchanged_rounds(
        [r for r in effect if r.get("session_id") in chosen])
    out = {"session_ids": list(session_ids),
           "arrangement": _subtotal(list(arrangement), ARRANGEMENT_TERMINAL,
                                    ARRANGEMENT_SURVIVED, "survival_rate"),
           "effect_trials": _subtotal(list(effect), EFFECT_TERMINAL, ("kept",), "kept_rate")}
    out["arrangement"]["excluded_unchanged_rounds"] = arrangement_excluded
    out["effect_trials"]["excluded_unchanged_rounds"] = effect_excluded
    out["effect_trials"].update(_effect_facts(effect))
    return out


def advice(data: dict) -> list[str]:
    """Report lines for candidates the phrase ledger's next_action cannot see."""
    pending = pending_rows(data)
    unjudged = [r for r in pending if r.get("immediate_verdict") is None]
    judged = [r for r in pending if r.get("immediate_verdict") is not None]
    out: list[str] = []
    if unjudged:
        out.append("%d arrangement/effect candidate(s) await judgment — judge_answer('keep'|'drop', "
                   "entry_id=<id>, reason=...): %s" % (len(unjudged), ", ".join(
                       "%s (%s)" % (r["id"], r.get("label")) for r in unjudged)))
    if judged:
        out.append("%d judged arrangement/effect candidate(s) await close_session() while "
                   "their Set is open: %s" % (len(judged), ", ".join(
                       "%s (%s)" % (r["id"], r.get("label")) for r in judged)))
    return out
