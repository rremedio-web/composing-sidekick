"""Piece continuity records: what a next session can continue from.

One store, ``store/piece_records.json`` (``{"revision", "records": [...]}``,
a sibling of ``piece_decisions.json`` — see ``piece_context._records_path``),
holds four record kinds, every one tagged with who it came from::

    source: owner | model_heard | measured | set_file | inference
    quote:  the words verbatim (his, when source is owner)

* **statement** — a fact about the piece ("Bassline is the bass") or about a
  bounce/Set ("the shaker is quiet", a loudness table). Owner statements are
  piece-level; model-heard / measured ones are dated and bound to the bounce
  and Set they came from. Owner and model statements are read as separate
  blocks (:func:`statements_for`) and never merged.
* **proposal** — the musical question, his source gesture, the reference and
  its JOB (with evidence type), what stays, what varies, candidate handles in
  that Set, seeds. Bound to one owner-confirmed version of a piece.
* **reaction** — his words verbatim with a scope (section / track / candidate
  / piece / global); the model's reading is stored apart as ``interpretation``
  and never promoted; ``rule`` is true only when he states a rule. A reaction
  never creates a KEEP/DROP — verdicts stay with ``judge_answer``.
* **observation** — a Set change seen on a read (track deleted, muted, clip
  moved…) with the snapshot digest; ``observed, unjudged`` and nothing turns
  it into a verdict.

Pure stdlib; every write goes through ``piece_context._locked_update`` (process
+ file lock, atomic replace). Record ids are content hashes with the revision,
so identical re-records of a statement collapse onto the first.
"""
from __future__ import annotations

import hashlib
import json
import os

from mcp_server import piece_context as _pc

SOURCES = ("owner", "model_heard", "measured", "set_file", "inference")
MODEL_SOURCES = ("model_heard", "measured", "set_file", "inference")
SCOPES = ("section", "track", "candidate", "piece", "global")
# Scopes that point at one thing and are meaningless without saying which.
_SCOPES_NEEDING_DETAIL = ("section", "track", "candidate")
OBSERVED_UNJUDGED = "observed, unjudged"
_PREFIX = {"statement": "st", "proposal": "pr", "reaction": "re", "observation": "ob"}


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _record_id(kind: str, content: dict, revision: int) -> str:
    digest = hashlib.sha1((_canonical(content) + "#%d" % revision).encode("utf-8")).hexdigest()
    return "%s-%s" % (_PREFIX[kind], digest[:10])


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty string" % field)
    return value.strip()


def _verbatim(value, field: str) -> str:
    """His words exactly as given — validated non-empty, never stripped."""
    _text(value, field)
    return value


def _str_list(values, field: str) -> list:
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise ValueError("%s must be a list of strings" % field)
    out = []
    for value in values:
        if not isinstance(value, (str, int)):
            raise ValueError("%s must be a list of strings" % field)
        out.append(str(value))
    return out


def _json_dict(value, field: str, *, required: bool = True) -> dict | None:
    if value is None:
        if required:
            raise ValueError("%s is required" % field)
        return None
    if not isinstance(value, dict) or (required and not value):
        raise ValueError("%s must be a non-empty mapping" % field)
    try:
        return json.loads(_canonical(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be JSON-serialisable: %s" % (field, exc)) from None


def load_records(*, path=None) -> dict:
    return _pc._load_store(_pc._records_path(path), "records")


def _pieces_beside(path, pieces_path):
    """The pieces store for a records file: ``pieces_path`` when given, else
    ``pieces.json`` beside the records file in use (never the default store
    when ``path`` points elsewhere)."""
    return _pc._beside(_pc._records_path(path), "pieces.json", pieces_path)


def _piece_or_refuse(piece, *, pieces_path=None) -> dict:
    data = _pc.load_pieces(path=pieces_path)
    record = _pc._find_piece(data, piece) if piece is not None else None
    if record is None:
        raise ValueError("unknown piece %r — declare_piece_version(piece, set_path, "
                         "confirm=True) binds a Set to a piece on the owner's word" % (piece,))
    return _pc._piece_view(record)


def _confirmed_or_refuse(piece_view: dict, set_key) -> str:
    key = os.fspath(set_key)
    if key not in piece_view["confirmed_versions"]:
        raise ValueError("%s is not a confirmed version of piece %r — declare_piece_version"
                         "(%r, set_path, confirm=True) first; nothing is inferred from the "
                         "filename" % (key, piece_view["piece_id"], piece_view["piece_id"]))
    return key


def _append(kind: str, content: dict, *, path=None, dedupe_on: tuple = ()) -> dict:
    """Append one record (id = hash of content + revision). With
    ``dedupe_on`` naming content fields, an existing record equal on those
    fields is returned instead of a second row."""
    def mutate(data, revision):
        if dedupe_on:
            for row in data["records"]:
                if row.get("kind") == kind and all(
                        row.get(field) == content.get(field) for field in dedupe_on):
                    return dict(row), False
        record = dict(content)
        record["kind"] = kind
        record["record_id"] = _record_id(kind, content, revision)
        record["ts"] = _pc._now_iso()
        record["revision"] = revision
        data["records"].append(record)
        return dict(record), True

    return _pc._locked_update(_pc._records_path(path), "records", mutate)


# --- statements ---------------------------------------------------------------

def record_statement(piece, quote, *, source, topic=None, set_key=None, bound_to=None,
                     dated=None, evidence=None, note=None, path=None, pieces_path=None) -> dict:
    """One provenance-tagged fact about the piece. ``source`` must be one of
    :data:`SOURCES`; ``quote`` is the words verbatim. A model-heard or
    measured statement must say when (``dated``) and what it heard
    (``bound_to`` — the bounce id and/or ``set_key``). Re-recording the same
    quote from the same source for the same piece returns the first record."""
    if source not in SOURCES:
        raise ValueError("source must be one of %s" % ", ".join(SOURCES))
    quote = _verbatim(quote, "quote") if source == "owner" else _text(quote, "quote")
    view = _piece_or_refuse(piece, pieces_path=_pieces_beside(path, pieces_path))
    key = os.fspath(set_key) if set_key is not None else None
    binding = _json_dict(bound_to, "bound_to", required=False) or {}
    if key is not None:
        binding.setdefault("set_key", key)
    if source in ("model_heard", "measured"):
        if not binding:
            raise ValueError("a %s statement must carry bound_to (the bounce id and/or "
                             "set_key it came from)" % source)
        if not isinstance(dated, str) or not dated.strip():
            raise ValueError("a %s statement must be dated (YYYY-MM-DD)" % source)
    content = {"piece_id": view["piece_id"], "set_key": key, "source": source, "quote": quote,
               "topic": topic, "bound_to": binding or None, "dated": dated,
               "evidence": _json_dict(evidence, "evidence", required=False), "note": note}
    return _append("statement", content, path=path,
                   dedupe_on=("piece_id", "source", "quote", "set_key"))


def statements_for(piece_id: str, *, set_key=None, path=None) -> dict:
    """Two blocks, never merged: ``owner`` (his words) and ``model`` (heard,
    measured, set_file, inference). Piece-level rows plus, with ``set_key``,
    rows bound to other versions are still listed — each carries its own
    ``bound_to`` so the reader sees which bounce/Set it came from."""
    rows = [row for row in load_records(path=path)["records"]
            if row.get("kind") == "statement" and row.get("piece_id") == piece_id]
    return {"owner": [row for row in rows if row.get("source") == "owner"],
            "model": [row for row in rows if row.get("source") in MODEL_SOURCES]}


# --- proposals ------------------------------------------------------------------

def record_proposal(piece, set_key, question, *, source_gesture, reference_job=None,
                    preserve=(), vary=(), candidates=(), seeds=(), notes=None,
                    path=None, pieces_path=None) -> dict:
    """File one proposal and return it with its ``proposal_id``."""
    view = _piece_or_refuse(piece, pieces_path=_pieces_beside(path, pieces_path))
    key = _confirmed_or_refuse(view, set_key)
    question = _text(question, "question")
    gesture = _json_dict(source_gesture, "source_gesture")
    job = _json_dict(reference_job, "reference_job", required=False)
    if job is not None:
        for field in ("ref", "job"):
            _text(job.get(field), "reference_job.%s" % field)
        if job.get("evidence") not in SOURCES:
            raise ValueError("reference_job.evidence must be one of %s" % ", ".join(SOURCES))
    handles = []
    for item in candidates or ():
        if not isinstance(item, dict):
            raise ValueError("candidates must be mappings with a label")
        handle = _json_dict(item, "candidate")
        handle["label"] = _text(handle.get("label"), "candidate label")
        if "track_id" in handle and handle["track_id"] is not None:
            handle["track_id"] = str(handle["track_id"])
        if "clip_ids" in handle:
            handle["clip_ids"] = _str_list(handle["clip_ids"], "candidate clip_ids")
        handles.append(handle)
    content = {"piece_id": view["piece_id"], "set_key": key, "source": "inference",
               "question": question, "source_gesture": gesture, "reference_job": job,
               "preserve": _str_list(preserve, "preserve"), "vary": _str_list(vary, "vary"),
               "candidates": handles, "seeds": _str_list(seeds, "seeds"),
               "notes": notes if notes is None else str(notes)}
    record = _append("proposal", content, path=path)
    record["proposal_id"] = record["record_id"]
    return record


def find_proposal(proposal_id, *, path=None) -> dict | None:
    for row in load_records(path=path)["records"]:
        if row.get("kind") == "proposal" and row.get("record_id") == proposal_id:
            return dict(row, proposal_id=row["record_id"])
    return None


def proposals_for(piece_id: str, *, path=None) -> list:
    return [dict(row, proposal_id=row["record_id"])
            for row in load_records(path=path)["records"]
            if row.get("kind") == "proposal" and row.get("piece_id") == piece_id]


# --- reactions ------------------------------------------------------------------

def record_reaction(words, scope, *, proposal_id=None, piece=None, scope_detail=None,
                    interpretation=None, rule=False, set_key=None, path=None,
                    pieces_path=None) -> dict:
    """His words verbatim, scoped. Never a verdict and never a ledger write."""
    words = _verbatim(words, "words")
    if scope not in SCOPES:
        raise ValueError("scope must be one of %s" % ", ".join(SCOPES))
    if scope in _SCOPES_NEEDING_DETAIL and (
            not isinstance(scope_detail, str) or not scope_detail.strip()):
        raise ValueError("scope %r needs scope_detail (the bars, track or candidate label)"
                         % scope)
    if proposal_id is None and piece is None:
        raise ValueError("a reaction needs proposal_id or piece")
    proposal = None
    if proposal_id is not None:
        proposal = find_proposal(proposal_id, path=path)
        if proposal is None:
            raise ValueError("unknown proposal %r" % (proposal_id,))
    view = (_piece_or_refuse(piece, pieces_path=_pieces_beside(path, pieces_path))
            if piece is not None else None)
    if proposal is not None and view is not None and view["piece_id"] != proposal["piece_id"]:
        raise ValueError("proposal %s belongs to piece %r, not %r"
                         % (proposal_id, proposal["piece_id"], view["piece_id"]))
    piece_id = proposal["piece_id"] if proposal is not None else view["piece_id"]
    key = os.fspath(set_key) if set_key is not None else (
        proposal["set_key"] if proposal is not None else None)
    if set_key is not None:
        _confirmed_or_refuse(view or _piece_or_refuse(
            piece_id, pieces_path=_pieces_beside(path, pieces_path)), key)
    reading = None
    if interpretation is not None:
        reading = {"text": _text(interpretation, "interpretation"), "source": "inference"}
    content = {"piece_id": piece_id, "set_key": key, "proposal_id": proposal_id,
               "source": "owner", "quote": words,
               "scope": {"kind": scope, "detail": scope_detail.strip() if isinstance(
                   scope_detail, str) and scope_detail.strip() else None},
               "interpretation": reading, "rule": bool(rule)}
    return _append("reaction", content, path=path)


def reactions_for(*, proposal_id=None, piece_id=None, path=None) -> list:
    rows = [row for row in load_records(path=path)["records"] if row.get("kind") == "reaction"]
    if proposal_id is not None:
        rows = [row for row in rows if row.get("proposal_id") == proposal_id]
    if piece_id is not None:
        rows = [row for row in rows if row.get("piece_id") == piece_id]
    return rows


# --- observations ---------------------------------------------------------------

def record_observation(set_key, what, evidence, *, path=None, pieces_path=None) -> dict:
    """A Set change seen on a read, bound to the exact Set and the snapshot
    digest it was seen in. ``observed, unjudged`` — never a verdict."""
    key = os.fspath(set_key)
    what = _text(what, "what")
    facts = _json_dict(evidence, "evidence", required=False) or {}
    if not isinstance(facts.get("snapshot_digest"), str) or not facts["snapshot_digest"]:
        raise ValueError("evidence.snapshot_digest (the snapshot the change was seen in) "
                         "is required")
    piece = _pc.resolve_piece(key, path=_pieces_beside(path, pieces_path))
    content = {"set_key": key, "piece_id": piece["piece_id"] if piece else None,
               "source": "set_file", "what": what, "evidence": facts,
               "status": OBSERVED_UNJUDGED}
    return _append("observation", content, path=path)


def observations_for(set_key, *, path=None) -> list:
    key = os.fspath(set_key)
    return [row for row in load_records(path=path)["records"]
            if row.get("kind") == "observation" and row.get("set_key") == key]


# --- the brief ------------------------------------------------------------------

_NOTE_SECTIONS = {"suggestions": "suggestions", "cuts and transitions": "cuts_and_transitions"}
_OPEN_STATUSES = ("awaiting owner", "open")


def _table_rows(lines: list) -> tuple:
    """Parse the first markdown pipe table in ``lines`` → (columns, rows)."""
    table = [line.strip() for line in lines if line.strip().startswith("|")]
    if len(table) < 2:
        return [], []
    cells = lambda line: [cell.strip() for cell in line.strip("|").split("|")]
    columns = cells(table[0])
    rows = []
    for line in table[2:]:          # table[1] is the |---| separator
        values = cells(line)
        if len(values) != len(columns):
            continue
        rows.append(dict(zip(columns, values)))
    return columns, rows


def read_notes_sections(path: str) -> dict:
    """The "Suggestions" and "Cuts and transitions" tables of a piece-notes
    file (``docs/pieces/<piece>.md``), by heading prefix, with their status
    column. ``status: missing`` when the file is not there."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return {"status": "missing", "sections": {}}
    sections = {}
    current, body = None, []

    def flush():
        if current is not None:
            columns, rows = _table_rows(body)
            sections[current[0]] = {"heading": current[1], "columns": columns, "rows": rows}

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            heading = line[3:].strip()
            key = next((value for prefix, value in _NOTE_SECTIONS.items()
                        if heading.casefold().startswith(prefix)), None)
            current, body = ((key, heading) if key else None), []
        elif current is not None:
            body.append(line)
    flush()
    return {"status": "read", "sections": sections}


def _open_questions(notes: dict, proposals: list) -> list:
    out = []
    suggestions = (notes.get("sections") or {}).get("suggestions") or {}
    for row in suggestions.get("rows") or []:
        status = (row.get("Status") or "").strip().casefold()
        if status in _OPEN_STATUSES:
            out.append("suggestion #%s: %s (%s)" % (row.get("#", "?"),
                                                    row.get("Suggestion", ""), status))
    cuts = (notes.get("sections") or {}).get("cuts_and_transitions") or {}
    for row in cuts.get("rows") or []:
        status = (row.get("Status") or "").strip().casefold()
        if status in _OPEN_STATUSES:
            out.append("join %s: %s (%s)" % (row.get("Join", "?"), row.get("Edit", ""), status))
    for proposal in proposals:
        if not proposal.get("reactions"):
            out.append("proposal %s: %s (no reaction yet)"
                       % (proposal["proposal_id"], proposal.get("question", "")))
    return out


def snapshot_summary(snapshot: dict) -> dict:
    """A small, Set-bound summary of a ``piece_snapshot`` (never the clip ids)."""
    set_info = snapshot.get("set") or {}
    identity = snapshot.get("identity") or {}
    return {"status": "live",
            "set": {"name": set_info.get("name"), "tempo": set_info.get("tempo"),
                    "meter": set_info.get("meter"), "bars": set_info.get("bars")},
            "file_path": identity.get("file_path"),
            "structural_digest": (identity.get("structural") or {}).get("digest"),
            "tracks": len(snapshot.get("tracks") or []),
            "sections": len(snapshot.get("sections") or []),
            "muted_tracks": [t.get("track") for t in snapshot.get("muted_tracks") or []],
            "unheard": len(snapshot.get("unheard") or [])}


def _latest_snapshot(observations: list, snapshot: dict | None) -> dict:
    if snapshot is not None:
        return snapshot
    seen = [row for row in observations if (row.get("evidence") or {}).get("snapshot_digest")]
    if not seen:
        return {"status": "none", "note": "no snapshot cached; piece_brief(live=True) reads one"}
    last = max(seen, key=lambda row: row.get("revision", 0))
    return {"status": "cached_from_observation", "record_id": last["record_id"],
            "snapshot_digest": last["evidence"]["snapshot_digest"], "ts": last.get("ts")}


def _prose(out: dict) -> str:
    piece = out.get("piece")
    lines = []
    if piece is None:
        lines.append("%s is not a confirmed version of any piece — declare_piece_version(piece, "
                     "set_path, confirm=True) on the owner's word binds it; nothing is inferred "
                     "from the filename." % out["set_key"])
    else:
        names = ", ".join(os.path.basename(v) for v in piece["confirmed_versions"])
        lines.append("%s — this Set: %s (confirmed versions: %s)."
                     % (piece["name"], os.path.basename(piece["this_version"]), names))
        intent = out["intent"]
        if intent.get("goal") or intent.get("preserve") or intent.get("borrow"):
            inherited = intent.get("inherited") or {}
            parts = []
            if intent.get("goal"):
                parts.append("goal: %s" % intent["goal"])
            if intent.get("preserve"):
                parts.append("preserve %s" % ", ".join(intent["preserve"]))
            if intent.get("borrow"):
                parts.append("borrow %s" % ", ".join(intent["borrow"]))
            if intent.get("depart"):
                parts.append("depart %s" % ", ".join(intent["depart"]))
            where = (" [inherited from %s]" % ", ".join(sorted(
                {os.path.basename(v["from"]) for v in inherited.values()}))) if inherited else ""
            lines.append("Owner intent: %s%s." % ("; ".join(parts), where))
        else:
            lines.append("Owner intent: none recorded.")
    owner = out["owner_statements"]
    if owner:
        lines.append("His words (%d): %s." % (len(owner), " / ".join(
            "\"%s\"" % row["quote"] for row in owner)))
    model = out["model_observations"]
    if model:
        lines.append("Model-heard / measured (%d, not his words): %s." % (len(model), " / ".join(
            "%s %s: \"%s\"" % (row["source"], row.get("dated") or "undated", row["quote"])
            for row in model)))
    proposals = out["proposals"]
    if proposals:
        lines.append("Proposals (%d): %s." % (len(proposals), " / ".join(
            "%s \"%s\" — %d reaction(s)" % (p["proposal_id"], p["question"], len(p["reactions"]))
            for p in proposals)))
    for reaction in out["reactions_unlinked"]:
        lines.append("Reaction (%s%s): \"%s\"." % (
            reaction["scope"]["kind"],
            ": " + reaction["scope"]["detail"] if reaction["scope"].get("detail") else "",
            reaction["quote"]))
    if out["open_questions"]:
        lines.append("Open: %s." % "; ".join(out["open_questions"]))
    observations = out["observations"]
    if observations:
        lines.append("Observed on this Set, %s (%d): %s — verdicts only in his words." % (
            OBSERVED_UNJUDGED, len(observations), " / ".join(o["what"] for o in observations)))
    refs = out["references"]
    if refs:
        lines.append("References and their jobs: %s." % "; ".join(
            "%s → %s (%s)" % (r["ref"], r["job"], r["evidence"]) for r in refs))
    latest = out["latest_snapshot"]
    if latest.get("status") == "live":
        lines.append("Live snapshot: %s, %s BPM, %s bars, %d tracks, %d sections." % (
            latest["set"].get("name"), latest["set"].get("tempo"), latest["set"].get("bars"),
            latest["tracks"], latest["sections"]))
    elif latest.get("status") == "live_unverified":
        lines.append("Live snapshot unverified: the read names no Set file, so it is not "
                     "bound to this Set.")
    elif latest.get("status") == "live_other_set":
        lines.append("Live has another Set open (%s) — its snapshot is reported as that Set's, "
                     "not this one's." % os.path.basename(latest.get("file_path") or "?"))
    elif latest.get("status") == "unavailable":
        lines.append("Live snapshot unavailable: %s." % latest.get("reason"))
    return "\n".join(lines)


def piece_brief(set_key, *, root, path=None, pieces_path=None, snapshot=None) -> dict:
    """Everything a composing agent needs to continue a piece from this Set,
    read-only: piece + confirmed versions; owner intent (piece-level with
    version overrides); his statements; model/measured observations
    (provenance-tagged, dated, bound); the notes file's "Suggestions" and
    "Cuts and transitions" tables with their status; open questions;
    proposals with their reactions; observations (observed, unjudged); the
    latest snapshot (``snapshot`` from a live read, else the most recent
    observation's digest); references with their jobs; and a prose brief.
    Phrase retrieval is not invoked here."""
    key = os.fspath(set_key)
    pieces_path = _pieces_beside(path, pieces_path)
    errors = []
    _unreadable = (OSError, ValueError, TypeError, AttributeError, KeyError)
    try:
        piece = _pc.resolve_piece(key, path=pieces_path)
    except _unreadable as exc:
        errors.append("%s unreadable: %s" % (pieces_path, exc))
        piece = None
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)   # already named in store_errors
        try:
            intent = _pc.current_decisions(
                key, path=(_pc._beside(_pc._records_path(path), "piece_decisions.json")
                           if path else None), pieces_path=pieces_path)
        except _unreadable as exc:
            errors.append("piece_decisions.json unreadable: %s" % exc)
            intent = {"revision": 0}
    try:
        records = [row for row in load_records(path=path)["records"] if isinstance(row, dict)]
    except _unreadable as exc:
        errors.append("%s unreadable: %s" % (_pc._records_path(path), exc))
        records = []
    out = {"set_key": key, "piece": None, "intent": intent, "writes": 0,
           "store_errors": errors}
    if piece is not None:
        out["piece"] = {"piece_id": piece["piece_id"], "name": piece["name"],
                        "notes_path": piece["notes_path"],
                        "confirmed_versions": piece["confirmed_versions"], "this_version": key}
        statements = [row for row in records if row.get("kind") == "statement"
                      and row.get("piece_id") == piece["piece_id"]]
        blocks = {"owner": [r for r in statements if r.get("source") == "owner"],
                  "model": [r for r in statements if r.get("source") in MODEL_SOURCES]}
        proposals = [dict(row, proposal_id=row["record_id"],
                          reactions=[r for r in records if r.get("kind") == "reaction"
                                     and r.get("proposal_id") == row["record_id"]])
                     for row in records if row.get("kind") == "proposal"
                     and row.get("piece_id") == piece["piece_id"]]
        unlinked = [r for r in records if r.get("kind") == "reaction"
                    and r.get("piece_id") == piece["piece_id"] and r.get("proposal_id") is None]
        notes_path = piece.get("notes_path")
        notes = (dict(read_notes_sections(os.path.join(root, notes_path)), path=notes_path)
                 if notes_path else {"status": "missing", "sections": {}, "path": None})
    else:
        out["note"] = ("%s is not a confirmed version of any piece — declare_piece_version("
                       "piece, set_path, confirm=True) on the owner's word binds it" % key)
        blocks = {"owner": [], "model": []}
        proposals, unlinked = [], []
        notes = {"status": "missing", "sections": {}, "path": None}
    observations = [row for row in records
                    if row.get("kind") == "observation" and row.get("set_key") == key]
    references = []
    for proposal in proposals:
        job = proposal.get("reference_job")
        if job:
            references.append({"ref": job["ref"], "job": job["job"], "evidence": job["evidence"],
                               "proposal_id": proposal["proposal_id"]})
    out.update({
        "owner_statements": blocks["owner"],
        "model_observations": blocks["model"],
        "notes": notes,
        "open_questions": _open_questions(notes, proposals),
        "proposals": proposals,
        "reactions_unlinked": unlinked,
        "observations": observations,
        "latest_snapshot": _latest_snapshot(observations, snapshot),
        "references": references,
    })
    out["prose"] = _prose(out)
    if errors:
        out["prose"] += "\nStore unreadable (brief is partial): %s." % "; ".join(errors)
    return out
