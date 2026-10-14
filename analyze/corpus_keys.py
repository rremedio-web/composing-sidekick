"""Stable phrase identity for the corpus (lane B3 CORPUS, item 3).

Positional ids (``f"{basename}|c{clip}|w{window}"``) renumber whenever a clip
is added, silently repointing golds, DROP bans, ratings and ledger
``source_phrase_id`` at the wrong phrase after a rebuild. The content-
addressed ``pid`` — digest(role + canonical notes) ONLY — survives rebuilds
regardless of project sort order; the clip identities live in ``provenance``
(each entry: project, track, clip, window, alias) and the positional id stays
on the record as ``alias`` so old stored ids still resolve. A gold/DROP/
rating on a note-set then means that phrase wherever he played it.

Also home to the one-shot old→new id migration (dry-run by default, backup
first — never run against the live store here).
"""
from __future__ import annotations

import contextlib
from contextlib import ExitStack
import hashlib
import json
import os
import re
import shutil
import tempfile

ID_MAP_FILENAME = "phrase_id_map.json"


# Trailing version markers: "demosong10" → "demosong", "NOCTURNEv2" →
# "nocturne", "song 2026-08-16" → "song". Same grouping the rituals
# version_courage uses (prefix match on the family stem).
_VERSION_TAIL = re.compile(r"[\s_\-–—]*(\([^()]*\)|\[[^\]]*\]|v\d+|\d+)\s*$",
                           re.IGNORECASE)
_DATE_TAIL = re.compile(r"[\s_\-–—]*\d{4}[-_.]\d{1,2}[-_.]\d{1,2}.*$")


def song_family(name: str | None) -> str:
    """Song-family stem: the song across Save-As versions, not the file."""
    stem = os.path.splitext(os.path.basename(str(name or "")))[0].strip().lower()
    if not stem:
        return ""
    fam = stem
    for _ in range(3):
        narrowed = _VERSION_TAIL.sub("", fam).strip()
        narrowed = _DATE_TAIL.sub("", narrowed).strip()
        if narrowed == fam:
            break
        fam = narrowed
    return fam or stem


def canonical_notes(notes) -> tuple:
    """Canonical note-set: pitch/start/duration only (velocity is performance,
    not content). Works on note dicts or parse_als Note objects."""
    canon = []
    for n in notes:
        if isinstance(n, dict):
            pitch, start, dur = n["pitch"], n["start"], n["duration"]
        else:
            pitch, start, dur = n.pitch, n.start, n.duration
        canon.append((int(pitch), round(float(start), 3), round(float(dur), 3)))
    return tuple(sorted(canon))


def phrase_pid(notes, *, role: str = "") -> str:
    """Content-addressed phrase id: digest(role + canonical notes) ONLY.

    No project/track/clip/window enters the digest: dedupe already keeps one
    record per (note-set, role), so the pid is unique by construction and
    stable regardless of project sort order or clip renumbering. The clip
    identities live in ``provenance``."""
    h = hashlib.sha1()
    h.update(str(role or "").encode("utf-8"))
    h.update(b"\x00")
    h.update(repr(canonical_notes(notes)).encode("utf-8"))
    return "p" + h.hexdigest()[:12]


def _provenance_aliases(phrase: dict) -> list:
    """Every positional id folded into this record by dedupe (survivor's own
    alias first when present). Tolerates old records without provenance."""
    out = []
    if phrase.get("alias"):
        out.append(phrase["alias"])
    for entry in phrase.get("provenance", []) or []:
        alias = entry.get("alias") if isinstance(entry, dict) else None
        if alias and alias not in out:
            out.append(alias)
    return out


def _read_default_id_map() -> dict:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "store", ID_MAP_FILENAME)
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _map_parts(id_map):
    data = _read_default_id_map() if id_map is None else id_map
    if not isinstance(data, dict):
        return {}, set(), {}
    # Accept the legacy flat map as well as the explicit cumulative format.
    if "map" in data or "ambiguous" in data or "gone" in data:
        return (data.get("map", {}) if isinstance(data.get("map"), dict) else {},
                set((data.get("ambiguous") or {}).keys()),
                data.get("gone", {}) if isinstance(data.get("gone"), dict) else {})
    return data, set(), {}


def phrase_lookup(phrases: list[dict], id_map=None) -> dict:
    """Map every known id → phrase: pid, id, alias AND every provenance alias
    (a rating stored under a folded duplicate's alias resolves to the kept
    record). First wins in index order; pid binds last so a pid collision
    with someone's alias can never steal the match."""
    mapping, ambiguous, gone = _map_parts(id_map)
    idx: dict = {}
    for p in phrases:
        keys = [p.get("id")] + _provenance_aliases(p)
        for key in keys:
            if key and key not in idx:
                idx[key] = p
    for p in phrases:
        if p.get("pid"):
            idx[p["pid"]] = p
    for alias, pid in mapping.items():
        target = idx.get(pid)
        if target is not None:
            idx[alias] = target
        else:
            idx.pop(alias, None)
    for alias in gone:
        idx.pop(alias, None)
    for alias in ambiguous:
        idx.pop(alias, None)
    return idx


def ids_match(stored: str | None, phrase: dict) -> bool:
    """True if a stored id names this phrase by pid, id, alias or any folded
    provenance alias."""
    if not stored:
        return False
    if stored == phrase.get("pid") or stored == phrase.get("id"):
        return True
    return stored in _provenance_aliases(phrase)


def expand_ids(stored_ids, phrases: list[dict], id_map=None) -> set:
    """Expand stored ids to every form the index knows (pid + alias + id).

    Raw ids pass through, so non-index identities (notes:/gold: digests) keep
    matching exactly. Either direction then hits: a ban stored under the old
    alias still culls the re-pidded phrase, and vice versa."""
    idx = phrase_lookup(phrases, id_map=id_map)
    _mapping, _ambiguous, gone = _map_parts(id_map)
    out = set()
    for sid in stored_ids:
        if not sid:
            continue
        if sid in gone:
            continue
        hit = idx.get(sid)
        out.add(sid)
        if hit:
            forms = [hit.get("pid"), hit.get("id")] + _provenance_aliases(hit)
            for form in forms:
                if form:
                    out.add(form)
    return out


def dedupe_key(phrase: dict) -> tuple:
    """Identity for 'the same musical phrase': canonical note-set + role."""
    return (canonical_notes(phrase.get("notes", [])),
            phrase.get("role"), phrase.get("kind"))


def dedupe_pool(pool: list[dict]) -> list[dict]:
    """Count distinct phrases once: drop exact (note-set, role) duplicates,
    keeping the first. For taste-training pools over a pre-dedupe index."""
    seen: set = set()
    out = []
    for p in pool:
        key = dedupe_key(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _provenance_entry(phrase: dict) -> dict:
    return {"alias": phrase.get("alias", phrase.get("id")),
            "pid": phrase.get("pid"),
            "project": phrase.get("project"),
            "track": phrase.get("track"),
            "clip": phrase.get("clip"),
            "window": phrase.get("window")}


def dedupe_phrases(phrases: list[dict]) -> tuple[list[dict], dict]:
    """Collapse exact duplicates to one record per distinct note-set (per
    role) with a ``provenance`` list (projects/tracks/clips/windows).

    Returns (deduped, remap) where remap sends every collapsed alias to the
    kept record's pid, so the id map keeps resolving stored old ids."""
    kept: dict = {}
    deduped: list[dict] = []
    remap: dict = {}
    for p in phrases:
        key = dedupe_key(p)
        if key in kept:
            keep = kept[key]
            keep.setdefault("provenance", []).append(_provenance_entry(p))
            alias = p.get("alias", p.get("id"))
            if alias and keep.get("pid"):
                remap[alias] = keep["pid"]
        else:
            kept[key] = p
            p["provenance"] = [_provenance_entry(p)]
            deduped.append(p)
    return deduped, remap


def check_duplicate_pids(phrases: list[dict]) -> None:
    """Fail a build whose pid is not unique. Runs AFTER dedupe (build_index),
    where the pid is unique by construction — a duplicate here means two
    records with the same (note-set, role) survived dedupe, i.e. the dedupe
    key and the pid disagree."""
    seen: dict[str, dict] = {}
    for p in phrases:
        pid = p.get("pid")
        if not pid:
            continue
        if pid in seen:
            raise ValueError(
                f"duplicate phrase pid {pid}: "
                f"{seen[pid].get('alias')} and {p.get('alias')} "
                f"(same notes in the same clip window twice?)")
        seen[pid] = p


def write_id_map(old_phrases: list[dict], new_phrases: list[dict],
                 path: str) -> dict:
    """Write the old→new id map {old alias: pid} for one build, from the
    PREVIOUS phrase_index vs the new build. For each old record the alias maps
    to pid(role, old notes) — but only when that pid still exists in the new
    build (vanished content maps nowhere). Aliases shared by old records with
    DIFFERENT content are ambiguous (a renumbered alias could mean either
    phrase): excluded from the map, listed under ``ambiguous``.

    Returns {"map": mapping, "ambiguous": {alias: [pids]}}. The file stays a
    flat {alias: pid} object so the ledger's id-map reader keeps working.
    """
    new_pids = {p.get("pid") for p in new_phrases if p.get("pid")}
    by_alias: dict[str, set] = {}
    for old in old_phrases or []:
        if old.get("pid"):
            continue
        alias = old.get("alias", old.get("id"))
        if not alias:
            continue
        by_alias.setdefault(alias, set()).add(
            phrase_pid(old.get("notes", []), role=old.get("role", "")))
    mapping: dict[str, str] = {}
    gone: dict[str, str] = {}
    ambiguous: dict[str, list] = {}
    try:
        with open(path) as fh:
            previous = json.load(fh)
        had_previous_file = True
    except (OSError, ValueError):
        previous = {}
        had_previous_file = False
    previous_map, previous_ambiguous, previous_gone = _map_parts(previous)
    previous_ambiguous_map = (previous.get("ambiguous", {})
                              if isinstance(previous, dict) else {})
    ambiguous = {key: list(previous_ambiguous_map.get(key) or [])
                 for key in sorted(previous_ambiguous)}
    changed = False
    for alias in sorted(by_alias):
        if alias in previous_map or alias in previous_ambiguous or alias in previous_gone:
            continue
        pids = sorted(by_alias[alias])
        if len(pids) > 1:
            ambiguous[alias] = pids
            previous_ambiguous.add(alias)
            changed = True
        elif pids[0] in new_pids:
            mapping[alias] = pids[0]
            previous_map[alias] = pids[0]
            changed = True
        else:
            gone[alias] = pids[0]
            previous_gone[alias] = pids[0]
            changed = True
    ambiguous = {key: (ambiguous.get(key) or []) for key in sorted(previous_ambiguous)}
    directory = os.path.dirname(os.path.abspath(path)) or "."
    if changed or not had_previous_file:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"map": previous_map, "ambiguous": ambiguous,
                       "gone": previous_gone}, fh,
                      indent=2, sort_keys=True)
            fh.write("\n")
    return {"map": previous_map, "ambiguous": ambiguous,
            "gone": previous_gone}


STALE_AFTER_DAYS = 42  # 6 weeks without a rebuild earns a nudge

_INDEX_META_HEAD = 1 << 20  # the meta sits at the top; 1MB is plenty
_INDEX_META_CACHE: dict = {"path": None, "mtime": None, "meta": None}


def _meta_slice(text: str):
    """json.loads the {"meta": {...}} prefix without parsing the 42MB tail.
    None when the layout defeats the fast path (the caller falls back to a
    full parse, so odd layouts still work)."""
    i = text.find('"meta"')
    if i < 0:
        return None
    i = text.find(":", i)
    if i < 0:
        return None
    i += 1
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    if i >= len(text) or text[i] != "{":
        return None
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(text)):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[i:j + 1])
                except ValueError:
                    return None
    return None


def _read_index_meta(path: str):
    """The index meta, reading only the head when possible (None when the
    file is missing/unreadable; full-parse fallback when the fast path
    misses)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(_INDEX_META_HEAD)
    except OSError:
        return None
    try:
        meta = _meta_slice(head.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        meta = None
    if meta is not None:
        return meta
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data.get("meta", {}) if isinstance(data, dict) else None


def _index_meta_cached(store_dir: str):
    """Index meta cached by the index file's mtime — repeat calls in one
    session don't re-parse."""
    path = os.path.join(store_dir, "phrase_index.json")
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    if _INDEX_META_CACHE.get("path") == path \
            and _INDEX_META_CACHE.get("mtime") == mtime:
        return _INDEX_META_CACHE.get("meta")
    meta = _read_index_meta(path)
    _INDEX_META_CACHE.update(path=path, mtime=mtime, meta=meta)
    return meta


def corpus_staleness(store_dir: str, current_set_path: str | None = None,
                     now: str | None = None) -> list[str]:
    """Advice lines when the corpus is stale — else []. Fail-closed: missing
    or unreadable files mean silence, never a scary wrong number.

    Notes when phrase_index.json is older than 6 weeks, and when the open
    Set's path (when identity is available) is not in the catalog manifest.
    """
    from datetime import datetime, timezone
    notes: list[str] = []
    meta = _index_meta_cached(store_dir)
    if not isinstance(meta, dict):
        return []
    # Age from the catalog scan the build saw (stamped by rebuild_corpus),
    # else the build time.
    corpus = meta.get("corpus")
    manifest_time = corpus.get("manifest_time") \
        if isinstance(corpus, dict) else None
    built_at = manifest_time or meta.get("built_at")
    try:
        built = datetime.fromisoformat(str(built_at)) if built_at else None
        ref = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
        if built is not None and built.tzinfo is None:
            built = built.replace(tzinfo=timezone.utc)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        built = None
    if built is not None:
        age_days = (ref - built).days
        if age_days > STALE_AFTER_DAYS:
            notes.append(
                f"corpus is {age_days} days old (> 6 weeks) — rebuild with "
                f"tools/rebuild_corpus.py --commit so retrieval reflects "
                f"what he actually plays now")
    if current_set_path:
        try:
            with open(os.path.join(store_dir, "catalog_manifest.json")) as fh:
                known = {os.path.normpath(r.get("path", ""))
                         for r in json.load(fh) if r.get("path")}
        except (OSError, ValueError):
            known = set()
        if known and os.path.normpath(current_set_path) not in known:
            notes.append(
                f"open Set is not in the corpus manifest "
                f"({os.path.basename(current_set_path)}) — its phrases "
                f"cannot be retrieved until the next rebuild")
    return notes


def load_id_map(path: str) -> dict:
    try:
        with open(path) as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            mapping, ambiguous, _gone = _map_parts(data)
            return {key: value for key, value in mapping.items()
                    if key not in ambiguous and value is not None}
        return {}
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def _ledger_locked(path: str):
    """The ledger lock: same path + flock convention as ledger._lock (and
    develop_session._locked_update), inlined so analyze stays server-free."""
    import fcntl
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    with open(lock_path, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _state_file_locked(path: str):
    """Ratings/develop lock convention: exclusive flock on path + '.lock'."""
    import fcntl
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    with open(lock_path, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _timestamped_backup(path: str) -> str:
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    candidate = f"{path}.{stamp}.bak"
    suffix = 1
    while os.path.exists(candidate):
        candidate = f"{path}.{stamp}.{suffix}.bak"
        suffix += 1
    shutil.copy2(path, candidate)
    backups = sorted(
        (os.path.join(os.path.dirname(os.path.abspath(path)), name)
         for name in os.listdir(os.path.dirname(os.path.abspath(path)) or ".")
         if re.fullmatch(re.escape(os.path.basename(path)) +
                         r"\.\d{8}T\d{6}\.\d{6}Z(?:\.\d+)?\.bak", name)),
        key=lambda item: (os.path.getmtime(item), item), reverse=True)
    for stale in backups[3:]:
        os.unlink(stale)
    return candidate


def _write_json_atomic(path: str, data) -> None:
    """Temp-file + os.replace so a crash never leaves a half-written file."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".%s." % os.path.basename(path),
                                    suffix=".tmp", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _rewrite_file(path: str, rewrite, dry_run: bool) -> int:
    """Apply rewrite(data) -> (data, n_changed). Dry run reads and counts
    only; a real run lands via temp + os.replace. Backups (for restore on a
    later failure) are the caller's job, taken before the first rewrite.
    Returns n_changed."""
    with open(path) as fh:
        data = json.load(fh)
    new_data, n = rewrite(data)
    if dry_run or n == 0:
        return n
    _write_json_atomic(path, new_data)
    return n


def _walk_entries(obj):
    """Yield every ledger entry dict under sessions[].entries (tolerates the
    flat {"entries": [...]} shape too)."""
    if isinstance(obj, dict):
        sessions = obj.get("sessions")
        if isinstance(sessions, list):
            for s in sessions:
                if isinstance(s, dict):
                    for e in s.get("entries", []) or []:
                        if isinstance(e, dict):
                            yield e
        for e in obj.get("entries", []) or []:
            if isinstance(e, dict):
                yield e


def migrate_stored_ids(ratings_path: str, ledger_path: str, id_map: dict, *,
                       dry_run: bool = True, backup: bool = True,
                       develop_session_path: str | None = None) -> dict:
    """Rewrite old positional ids to pids in ratings.json (ratings, rejects
    and gold marks all live there as records with ``phrase_id``), in the
    ledger (entries with ``source_phrase_id``) and in the sticky-develop hold
    (``tools/ideas/develop_session.json`` hold.source_phrase_id) — in the SAME
    step, so no half-migrated state can strand a ban, a gold or a hold.

    Dry-run by default and never run against the live store here: pass
    explicit paths. Unknown ids (notes: digests, gold: digests, already-pids)
    pass through untouched. A missing develop-session file migrates nothing
    (sticky develop may never have run); only the hold's id moves, never the
    pass counters around it.
    """
    report: dict = {"dry_run": dry_run, "ratings_rewritten": 0,
                    "ledger_rewritten": 0, "develop_session_rewritten": 0}

    def _rewrite_ratings(data: dict):
        n = 0
        ratings = data.get("ratings")
        if isinstance(ratings, list):
            for rec in ratings:
                if isinstance(rec, dict) and rec.get("phrase_id") in id_map:
                    rec["phrase_id"] = id_map[rec["phrase_id"]]
                    n += 1
        rounds = data.get("candidate_rounds")
        if isinstance(rounds, list):
            for row in rounds:
                candidates = row.get("candidates", []) if isinstance(row, dict) else []
                for candidate in candidates:
                    if isinstance(candidate, dict) and candidate.get("id") in id_map:
                        candidate["id"] = id_map[candidate["id"]]
                        n += 1
        return data, n

    def _rewrite_ledger(data: dict):
        n = 0
        for entry in _walk_entries(data):
            if entry.get("source_phrase_id") in id_map:
                entry["source_phrase_id"] = id_map[entry["source_phrase_id"]]
                n += 1
            snapshot = entry.get("judged_snapshot")
            if isinstance(snapshot, dict) and snapshot.get("source_phrase_id") in id_map:
                snapshot["source_phrase_id"] = id_map[snapshot["source_phrase_id"]]
                n += 1
            for revision in entry.get("revision_history", []) or []:
                if not isinstance(revision, dict):
                    continue
                if revision.get("source_phrase_id") in id_map:
                    revision["source_phrase_id"] = id_map[revision["source_phrase_id"]]
                    n += 1
                snapshot = revision.get("snapshot")
                if isinstance(snapshot, dict) and snapshot.get("source_phrase_id") in id_map:
                    snapshot["source_phrase_id"] = id_map[snapshot["source_phrase_id"]]
                    n += 1
        # Pending judgment events own their original payload for replay. Move
        # its phrase identity with the ledger row but KEEP the event digest:
        # the ears and develop sinks hold their own copy of it, so a new
        # digest would turn every post-migration retry into a "conflict".
        # ledger.begin_judgment matches the retry alias-aware instead.
        for event in data.get("judgment_events", []) or []:
            payload = event.get("payload") if isinstance(event, dict) else None
            if isinstance(payload, dict) and payload.get("source_phrase_id") in id_map:
                payload["source_phrase_id"] = id_map[payload["source_phrase_id"]]
                n += 1
        return data, n

    def _rewrite_develop(state: dict):
        n = 0
        hold = state.get("hold")
        if isinstance(hold, dict) and hold.get("source_phrase_id") in id_map:
            hold["source_phrase_id"] = id_map[hold["source_phrase_id"]]
            n += 1
        return state, n

    targets = [ratings_path, ledger_path]
    if develop_session_path is not None and os.path.exists(develop_session_path):
        targets.append(develop_session_path)
    # else: no path given, or sticky develop never ran — nothing to migrate
    backups: dict[str, str] = {}
    lock_targets = [p for p in targets if os.path.exists(p)]
    with ExitStack() as locks:
        if not dry_run:
            # Deterministic lock order; retain every lock through rollback.
            for path in sorted(lock_targets):
                locks.enter_context(_ledger_locked(path) if path == ledger_path
                                    else _state_file_locked(path))
        try:
            rewrites = [(ratings_path, _rewrite_ratings, "ratings_rewritten"),
                        (ledger_path, _rewrite_ledger, "ledger_rewritten")]
            if develop_session_path is not None:
                rewrites.append((develop_session_path, _rewrite_develop,
                                 "develop_session_rewritten"))
            changed = []
            for path, rewrite, report_key in rewrites:
                if not os.path.exists(path):
                    continue
                with open(path) as fh:
                    data = json.load(fh)
                new_data, count = rewrite(data)
                report[report_key] = count
                if count:
                    changed.append((path, new_data))
            if not dry_run:
                for path, _ in changed:
                    backups[path] = _timestamped_backup(path)
                for path, data in changed:
                    _write_json_atomic(path, data)
        except BaseException:
            # Restore every start-of-run snapshot before releasing any lock.
            for path, bak in backups.items():
                shutil.copy2(bak, path)
            raise
    if not backup:
        for bak in backups.values():
            try:
                os.unlink(bak)
            except OSError:
                pass
    return report
