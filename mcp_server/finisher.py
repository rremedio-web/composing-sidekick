"""The finisher brain: read a project like a producer, name what's missing.

whats_missing() does the STRUCTURED half of the diagnosis: detect sections from the
arrangement timeline, trace the density/role coverage, and fire NAMED dramaturg
detectors (loop-with-ambitions, flat-energy runs, drop-without-contrast, uncommitted
ending, comping debris...). It returns facts and verb-menu proposals — never prose.
The musical narrative ("this is a 96-bar A that never learned a B") is the LLM's job,
written against these facts with the sombra-brain diagnosis procedure loaded.

Approximation, stated: arrangement clips loop, so note density is spread uniformly
across each clip's timeline span — good enough for structure truth, useless for
note-level detail (which the finisher doesn't need).

New module by design (external review: core stops growing). One module universe.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

for _alias in ("finisher", "mcp_server.finisher"):
    sys.modules.setdefault(_alias, sys.modules[__name__])

ROOT = core.ROOT
sys.path.insert(0, os.path.join(ROOT, "ingest"))
sys.path.insert(0, os.path.join(ROOT, "analyze"))
from parse_als import from_dict, is_timeline_clip_source  # noqa: E402
from roles import clip_role  # noqa: E402
from mcp_server import piece_context  # noqa: E402

BEATS_PER_BAR = core.BEATS_PER_BAR

VERB_MENU = ("remove", "answer", "thicken", "thin", "transpose", "humanize", "arrange")

DETECTORS = (
    "loop_with_ambitions", "flat_energy_run", "no_breathe", "drop_without_contrast",
    "uncommitted_ending", "comping_debris", "b_section_missing", "no_timeline",
    "no_low_end", "register_collision", "flat_velocity", "machine_timing",
    "silent_gap", "unison_wall",
)

# Detectors that read note content (density, pitches, velocities, timing). Over
# bars where a clip's notes were not read (project='live' without the Live API)
# they cannot be trusted and are withheld; placement detectors still run.
NOTE_DETECTORS = (
    "flat_energy_run", "no_breathe", "drop_without_contrast", "uncommitted_ending",
    "no_low_end", "register_collision", "flat_velocity", "machine_timing", "unison_wall",
)

SILENT_GAP_MIN_BARS = 4      # shorter holes are breaths, not gaps
UNISON_MIN_TRACKS = 3        # two doublers is a texture; three is a wall


# --- the open Set as a project ----------------------------------------------------

def _live_note(n: dict) -> dict | None:
    """One sidekick/PP note dict → the parse_als note shape (clip-relative beats)."""
    try:
        pitch = int(n.get("pitch", n.get("p")))
        start = float(n.get("start", n.get("start_beats", n.get("t", 0.0))) or 0.0)
        dur = float(n.get("duration", n.get("duration_beats", n.get("d", 0.0))) or 0.0)
        vel = int(n.get("velocity", n.get("v", 100)) or 100)
    except (TypeError, ValueError):
        return None
    return {"pitch": pitch, "start": start, "duration": dur, "velocity": vel, "enabled": True}


def _brace(loop_start, loop_end, audible_start, audible_end) -> dict:
    return {"loop_start": loop_start, "loop_end": loop_end,
            "audible_start": audible_start, "audible_end": audible_end}


def _live_playback(c: dict) -> dict:
    """Playback evidence of one Producer Pal clip, in the parse_als field names
    the intent fingerprint reads (_playback_keys).

    ``start_marker`` (firstStart, which Producer Pal omits when it equals the
    loop start; None when ``start`` is unreadable, so the phase stays unknown
    rather than invented) and ``disabled`` (muted / isMuted / deactivated /
    activated False) are always emitted; both are outside the hashed body.
    ``loop_on`` and ``loop_start`` enter the hashed body, so they are emitted
    only when NOT the default _playback_keys already assumes (loop on,
    brace at clip beat 0): a default live clip hashes exactly as before
    these fields existed, and rows recorded then keep matching.
    ``playback_brace`` (loop start/end and the audible markers, quarter beats)
    is what _playback_segments needs to seed three_endings; it is excluded
    from the fingerprint for the same reason (a live row keeps its hash).

    A clip carrying its Live API read (``c["live"]["frame"]``) takes the real
    start marker, loop and clip mute from it; otherwise firstStart is parsed
    in the clip's own meter (the beat of 6/8 is an eighth)."""
    import clip_playback as _cp
    import pp_session as _pps

    pp_disabled = bool(c.get("muted") or c.get("isMuted") or c.get("deactivated")
                       or c.get("activated") is False)
    clip_live = c.get("live")
    frame = (clip_live.get("frame") if isinstance(clip_live, dict)
             and not clip_live.get("error") else None)
    if frame is not None:
        # The Live API read carries the real markers: no firstStart inference.
        out = {"start_marker": frame["start_marker"],
               "disabled": bool(frame.get("muted")) or pp_disabled,
               "playback_brace": _brace(frame["loop_start"], frame["loop_end"],
                                        frame["start_marker"], frame["end_marker"])}
        if not frame["looping"]:
            out["loop_on"] = False
        if frame["loop_start"]:
            out["loop_start"] = frame["loop_start"]
        return out
    timing = _pps.clip_timing_beats(c)
    loop_start = timing["loop_start_beats"]
    num, den = _cp.parse_signature(c.get("timeSignature")) or (4, 4)
    marker = loop_start
    first = c.get("firstStart")
    if loop_start is not None and isinstance(first, str) and "|" in first:
        try:
            # the beat of firstStart is the clip meter's beat (an eighth in 6/8)
            marker = _cp.bar_beat_quarters(first, num, den)
        except (ValueError, TypeError):
            marker = None
    out = {"start_marker": marker, "disabled": pp_disabled}
    loop_end = timing["loop_end_beats"]
    if loop_start is not None and loop_end is not None:
        # Producer Pal's start/end ARE the markers of a clip that does not loop
        out["playback_brace"] = _brace(loop_start, loop_end, loop_start, loop_end)
    if not c.get("looping", True):
        out["loop_on"] = False
    if loop_start:
        out["loop_start"] = loop_start
    return out


def project_from_live_tracks(live: dict) -> dict:
    """A parse_als-shaped project dict built from live_write.read_arrangement_tracks:
    main-lane clips are timeline material, take-lane clips are comping debris,
    drum tracks come from the instrument (Drum Rack) or the track name, and only
    the notes INSIDE each clip's loop brace count (hidden ones are tallied under
    skip_detail['hidden_past_loop']). Note times are quarter beats of clip
    content, as parse_als writes them. Positions use the reader's song meter
    (``beats_per_bar``; 4/4 only when the reader reports none), and say so."""
    import json as _json
    import pp_codec as _pp_codec
    import pp_session as _pps
    from parse_als import DRUM_KEYWORDS, _drum_keyword_matches

    set_name = str(live.get("set_name") or "untitled")
    tempo = float(live.get("tempo") or 120.0)
    bpb = float(live.get("beats_per_bar") or _pp_codec.BEATS_PER_BAR)
    meter = str(live.get("meter") or "4/4")
    midi_names: list[str] = []
    audio_names: list[str] = []
    clips: list[dict] = []
    hidden_total = 0
    unread_total = 0

    import clip_playback as _cp
    song_meter = _cp.parse_signature(meter)

    def _convert(c: dict, track: str, is_drum: bool, source: str) -> dict | None:
        nonlocal hidden_total, unread_total
        # 2.2 arrangementStart, 2.3 the position in the clip path 't3[5|1]'
        start_s, length_s = _pp_codec.arrangement_start_of(c), c.get("arrangementLength")
        if not isinstance(start_s, str) or "|" not in start_s:
            return None
        try:
            # the beat of bar|beat is the song meter's beat (an eighth in 6/8)
            start = (_cp.bar_beat_quarters(start_s, *song_meter) if song_meter
                     else _pp_codec.bar_beat_to_beats(start_s, bpb))
        except (ValueError, TypeError):
            return None
        end = None
        for tok in (length_s, c.get("length")):
            if end is None and isinstance(tok, str):
                try:
                    end = start + _pp_codec.pp_duration_to_beats(tok, bpb)
                except ValueError:
                    end = None
        raw_notes = _pps.extract_notes(
            _pps.PPResponse(_json.dumps({"result": {k: v for k, v in c.items()
                                                    if k != "live"}}), c, [])) or []
        clip_live = c.get("live")
        if isinstance(clip_live, dict) and clip_live.get("error"):
            split = {"unread": True, "audible": [], "n_hidden": 0,
                     "unread_reason": "the Live API read failed: %s" % clip_live["error"]}
        else:
            split = _pps.split_audible_notes(c, raw_notes, live=clip_live)
        extra = {}
        if split.get("unread"):
            # Producer Pal saw only [-L, 2L) of this clip: its notes are unknown,
            # neither playing nor hidden.
            unread_total += 1
            extra = {"unread": True, "unread_reason": split.get("unread_reason")}
        hidden_total += split["n_hidden"]
        # Quarter beats, like parse_als and the playback brace: Producer Pal's
        # notes are in the clip meter's beats (an eighth in 6/8), a Live API
        # read is already in quarters (notes_den 4). 4/4 scales by exactly 1.
        scale = 4.0 / float(split.get("notes_den") or 4)
        notes = [ln for ln in (_live_note(n) for n in split["audible"]) if ln is not None]
        if scale != 1.0:
            notes = [{**n, "start": n["start"] * scale, "duration": n["duration"] * scale}
                     for n in notes]
        if end is None:
            end = start + max((n["start"] + n["duration"] for n in notes), default=0.0)
        rec = {"name": str(c.get("name") or ""), "track": track, "start": start,
               "end": end, "note_count": len(notes), "is_drum": is_drum,
               "clip_source": source, "notes": notes, **extra}
        rec.update(_live_playback(c))
        return rec

    for t in live.get("tracks") or []:
        name = str(t.get("name") or ("track %s" % t.get("track_index")))
        if t.get("type") == "audio":
            audio_names.append(name)
            continue
        midi_names.append(name)
        low = name.lower()
        is_drum = ("drum rack" in str(t.get("instrument") or "").lower()
                   or any(_drum_keyword_matches(low, k) for k in DRUM_KEYWORDS))
        for c in t.get("arrangement_clips") or []:
            if not isinstance(c, dict):
                continue
            lane = c.get("takeLane")
            source = "take_lane" if (isinstance(lane, int) and lane >= 1) else "arrangement"
            rec = _convert(c, name, is_drum, source)
            if rec:
                clips.append(rec)
        for lane in t.get("take_lanes") or []:
            for c in (lane.get("clips") or []) if isinstance(lane, dict) else []:
                if isinstance(c, dict):
                    rec = _convert(c, name, is_drum, "take_lane")
                    if rec:
                        clips.append(rec)

    record = {
        "path": live.get("file_path") or live.get("path") or "live:%s" % set_name,
        "tempo": tempo,
        "track_count": len(midi_names) + len(audio_names),
        "midi_track_names": midi_names,
        "audio_track_names": audio_names,
        "clip_count": len(clips),
        "total_notes": sum(c["note_count"] for c in clips),
        "audio_clip_count": 0,
        "notes_skipped": hidden_total,
        "skip_detail": {"hidden_past_loop": hidden_total},
        "unread_clips": unread_total,
        "assumptions": list(live.get("assumptions") or []) + (
            ["%d clip%s unread: Producer Pal returns notes only in [-L, 2L) of a clip "
             "and %s loop brace lies outside it — not counted as notes or as hidden"
             % (unread_total, "" if unread_total == 1 else "s",
                "its" if unread_total == 1 else "their")] if unread_total else []) + (
            ["song meter %s: arrangement positions placed in %g-beat bars; bar-based "
             "diagnosis still counts 4-beat bars" % (meter, bpb)] if bpb != 4.0 else []),
        "clips": clips,
    }
    if live.get("identity_verified") is False:
        record["identity_unverified"] = True
    if live.get("truncated"):
        record["read_truncated"] = dict(live["truncated"])
    return record


# --- reading -------------------------------------------------------------------

def _catalog_hits(project: str) -> list[dict]:
    """Catalog index entries matching an id, exact name, stem, or full path."""
    if project.strip().lower() == "live":
        raise ValueError("project 'live' means the open Set; it is not a catalog name")
    index_path = os.path.join(ROOT, "store", "projects", "_index.json")
    if not os.path.exists(index_path):
        return []  # no catalog materialized yet (fresh clone): nothing matches
    with open(index_path) as fh:
        index = json.load(fh)
    want = project.lower().removesuffix(".als")
    return [e for e in index if e["id"] == project
            or e["name"].lower().removesuffix(".als") == want
            or os.path.normcase(os.path.abspath(e.get("path", "")))
            == os.path.normcase(os.path.abspath(project))]


def _project_record(project: str) -> dict:
    """Load a materialized project by id, exact name, or case-insensitive stem match."""
    hits = _catalog_hits(project)
    if not hits:
        hint = ""
        if os.path.isabs(str(project)) and str(project).lower().endswith(".als"):
            hint = ("; a saved Set outside the catalog is not a project until it is "
                    "opened in Live and finished with project='live'")
        raise ValueError(f"no materialized project matches {project!r}{hint}")
    if len(hits) > 1:
        candidates = sorted(e.get("path", e["name"]) for e in hits)
        raise ValueError(f"project {project!r} is ambiguous; candidates: {candidates}")
    hit = hits[0]
    proj_dir = os.path.join(ROOT, "store", "projects")
    with open(os.path.join(proj_dir, f"{hit['id']}.json")) as fh:
        return json.load(fh)


def _finishing_ledger_keys() -> set[str]:
    """Set paths the finishing ledger already verified (one_hour_close wrote
    them from a verified open Set or a resolved project)."""
    import rituals  # lazy: rituals imports this module at load time
    return {str(key) for key, record in rituals._load_ledger().get("finishing", {}).items()
            if isinstance(record, dict) and record.get("started_at")}


def _saved_set_key(project: str) -> tuple[str, bool] | None:
    """(key, on_disk) when ``project`` is EXACTLY a key the finishing ledger
    recorded (one_hour_close wrote it, with started_at, from a verified open
    Set or a resolved project); None for anything else.

    A file merely existing on disk vouches for nothing (someone else's Set,
    a Factory Pack demo, a case/symlink/.ALS spelling of a catalogued Set),
    so there is no on-disk branch: a new uncatalogued Set enters through
    project='live'."""
    text = str(project)
    if text in _finishing_ledger_keys():
        return text, os.path.isfile(text)
    return None


def _parse_saved_set(path: str) -> dict:
    """A parse_als project record for a saved Set outside the catalog."""
    from parse_als import parse_als, to_dict
    try:
        record = to_dict(parse_als(path))
    except Exception as exc:  # gzip / XML / layout errors of a foreign file
        raise ValueError(f"{path!r} is not a readable Live Set ({exc})") from exc
    record["path"] = path
    return record


def _read_live_project() -> dict:
    """Read the open Set through the same Producer Pal arrangement reader as the tool."""
    from live_write import read_arrangement_tracks
    from pp_session import run_coro_blocking

    return project_from_live_tracks(run_coro_blocking(read_arrangement_tracks()))


def _unverified_set_reason(record: dict) -> str | None:
    """Why this project record cannot key a finishing decision, or None."""
    if record.get("identity_unverified"):
        return ("Set identity unavailable (Live API access off?); enable Live API "
                "access in Producer Pal so the Set file can be verified")
    path = record.get("path")
    if not path or str(path).startswith("live:"):
        return "the open Set has no reported file path; save the Set first in Live"
    return None


def resolve_project(project: str) -> dict:
    """Resolve a catalog name/path, the open saved Set, or a recorded
    finishing-ledger key outside the catalog (see resolve_project_key) to
    one project record."""
    if project.strip().lower() == "live":
        record = _read_live_project()
        reason = _unverified_set_reason(record)
        if reason:
            raise ValueError(reason + " before finishing")
        return record
    if not _catalog_hits(project):
        saved = _saved_set_key(project)
        if saved is not None:
            key, on_disk = saved
            if not on_disk:
                raise ValueError(f"{key!r} is a recorded finishing key but the Set file is "
                                 "not on disk; open it in Live and pass project='live'")
            return _parse_saved_set(key)
    return _project_record(project)


def resolve_project_key(project: str) -> str:
    """Return the canonical Set file path used as a finishing-ledger key.

    Catalog names/ids/paths resolve through the catalog; 'live' through the
    open saved Set. A path the catalog does not know is a valid key only
    when it is exactly a key the finishing ledger recorded (one_hour_close's
    returned project), so the returned key chains into confirm_bounce /
    declarations before ingestion. Existence on disk vouches for nothing."""
    if project.strip().lower() != "live" and not _catalog_hits(project):
        saved = _saved_set_key(project)
        if saved is not None:
            return saved[0]
    record = resolve_project(project)
    path = record.get("path")
    if not path:
        raise ValueError(f"project {project!r} has no Set file path")
    return str(path)


def read_structure(project_dict: dict) -> dict:
    """Timeline truth for one project: per-bar density + active roles, sections cut
    where the active-track set changes, take-lane debris counted.

    A clip whose notes were not read (``unread``: Producer Pal's [-L, 2L) note
    window missed its loop brace) still OCCUPIES its bars — it is placed and
    plays — but its notes are unknown: it adds no density, and the bars it
    covers are ``density_unknown`` (listed in ``unread_bars``, 1-based) so
    run_detectors withholds note-based findings there."""
    p = from_dict(project_dict)
    timeline = []
    unread_spans = []
    debris = 0
    for c, raw in zip(p.clips, project_dict.get("clips") or []):
        if c.clip_source == "take_lane":
            debris += 1
            continue
        if not is_timeline_clip_source(c.clip_source):
            continue
        if c.start is None or c.end is None or c.end <= c.start:
            continue
        if raw.get("unread"):
            unread_spans.append((c.start / BEATS_PER_BAR, c.end / BEATS_PER_BAR))
            timeline.append({
                "track": c.track, "role": "unknown", "unread": True,
                "start_bar": c.start / BEATS_PER_BAR, "end_bar": c.end / BEATS_PER_BAR,
                "note_sig": frozenset(), "notes_per_bar": 0.0, "register": None,
                "vel_distinct": 0, "n_notes": 0, "offgrid_frac": 0.0,
            })
            continue
        if c.note_count == 0:
            continue
        enabled = [n for n in c.notes if n.enabled]
        pitches = sorted(n.pitch for n in enabled) or [60]
        devs = [abs(n.start - round(n.start * 4) / 4.0) for n in enabled]
        timeline.append({
            "track": c.track,
            "role": clip_role(c.notes, c.is_drum),
            "start_bar": c.start / BEATS_PER_BAR,
            "end_bar": c.end / BEATS_PER_BAR,
            "note_sig": frozenset((n.pitch, round(n.start, 3)) for n in enabled),
            "notes_per_bar": c.note_count / max(1e-9, (c.end - c.start) / BEATS_PER_BAR),
            "register": pitches[len(pitches) // 2],
            "vel_distinct": len({n.velocity for n in enabled}),
            "n_notes": len(enabled),
            "offgrid_frac": (sum(1 for d in devs if d > 1e-6) / len(devs)) if devs else 0.0,
        })
    if not timeline:
        return {"tempo": p.tempo, "total_bars": 0, "bars": [], "sections": [],
                "timeline": [], "comping_debris": debris, "unread_bars": [],
                "tracks": sorted({c.track for c in p.clips})}

    total_bars = int(max(t["end_bar"] for t in timeline) + 0.999)
    bars = []
    for b in range(total_bars):
        active = [t for t in timeline if t["start_bar"] <= b < t["end_bar"]]
        bars.append({
            "bar": b + 1,
            "density": round(sum(t["notes_per_bar"] for t in active), 2),
            "tracks": frozenset(t["track"] for t in active),
            "roles": sorted({t["role"] for t in active if not t.get("unread")}),
            "density_unknown": any(t.get("unread") for t in active),
        })

    sections = []
    sec_start = 0
    for i in range(1, total_bars + 1):
        if i == total_bars or bars[i]["tracks"] != bars[i - 1]["tracks"]:
            seg = bars[sec_start:i]
            if sections and (i - sec_start) < 2:      # merge sub-2-bar slivers
                prev = sections[-1]
                prev["end_bar"] = i
                prev["bars"] = prev["end_bar"] - prev["start_bar"] + 1
                prev["density"] = round((prev["density"] + _mean([b["density"] for b in seg])) / 2, 2)
                prev["density_unknown"] = (prev["density_unknown"]
                                           or any(b["density_unknown"] for b in seg))
            else:
                sections.append({
                    "start_bar": sec_start + 1,
                    "end_bar": i,
                    "bars": i - sec_start,
                    "density": round(_mean([b["density"] for b in seg]), 2),
                    "roles": sorted({r for b in seg for r in b["roles"]}),
                    "density_unknown": any(b["density_unknown"] for b in seg),
                    "signature": tuple(sorted(bars[sec_start]["tracks"])),
                })
            sec_start = i
    return {"tempo": p.tempo, "total_bars": total_bars, "bars": bars,
            "sections": sections, "timeline": timeline, "comping_debris": debris,
            "unread_bars": [b["bar"] for b in bars if b["density_unknown"]],
            "tracks": sorted({t["track"] for t in timeline})}


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


# --- the dramaturg: named deterministic detectors --------------------------------

def _world(tempo: float | None) -> dict:
    """The catalog holds TWO dramaturgies (a slow piece judged by club norms reads as
    broken). Detector thresholds follow the tempo-world:
    the doom world breathes in longer spans and ends by decay-into-nothing."""
    if tempo and tempo <= 95:
        return {"name": "doom (~75 world)", "loop_bars": 96, "loop_frac": 0.75,
                "flat_run": 32, "tail_frac": 0.9}
    return {"name": "club (~120 world)", "loop_bars": 48, "loop_frac": 0.6,
            "flat_run": 16, "tail_frac": 0.8}


def run_detectors(reading: dict) -> list[dict]:
    """Named findings. Note-based ones (NOTE_DETECTORS) that span a bar whose
    notes were not read — or name no bars while any bar is unread — are
    withheld into ``reading["suppressed"]`` instead of being reported."""
    findings = _run_detectors(reading)
    unread = set(reading.get("unread_bars") or [])
    kept, suppressed = [], []
    for finding in findings:
        # a comparison finding also reads the span it compares against
        bars = finding.get("context_bars") or finding.get("bars")
        over = (bool(unread) if not bars else
                any(bars[0] <= b <= bars[1] for b in unread))
        if finding["detector"] in NOTE_DETECTORS and over:
            suppressed.append({**finding, "reason": (
                "withheld: notes unread over bars %s (Producer Pal's [-L, 2L) window; "
                "the Live API reads them)" % _bar_ranges(
                    sorted(unread) if not bars else
                    [b for b in sorted(unread) if bars[0] <= b <= bars[1]]))})
        else:
            kept.append(finding)
    reading["suppressed"] = suppressed
    return kept


def _bar_ranges(bars: list[int]) -> str:
    out, start, prev = [], None, None
    for b in bars:
        if start is None:
            start = prev = b
        elif b == prev + 1:
            prev = b
        else:
            out.append("%d–%d" % (start, prev) if prev != start else str(start))
            start = prev = b
    if start is not None:
        out.append("%d–%d" % (start, prev) if prev != start else str(start))
    return ", ".join(out)


def _run_detectors(reading: dict) -> list[dict]:
    findings: list[dict] = []
    sections = reading["sections"]
    bars = reading["bars"]
    total = reading["total_bars"]
    world = _world(reading.get("tempo"))

    if total == 0:
        return [{"detector": "no_timeline", "severity": "high",
                 "evidence": "no arrangement-timeline material — a Session-view sketch",
                 "bars": None}]

    densities = [b["density"] for b in bars]
    med = sorted(densities)[len(densities) // 2] if densities else 0.0

    for s in sections:
        if s["bars"] > world["loop_bars"] or s["bars"] / total > world["loop_frac"]:
            findings.append({"detector": "loop_with_ambitions", "severity": "ask",
                             "bars": [s["start_bar"], s["end_bar"]],
                             "evidence": f"is this intended? (one section by design); "
                                         f"note density / track count (MIDI only; audio unseen) "
                                         f"one unchanged section runs {s['bars']} bars "
                                         f"({s['bars'] * 100 // max(1, total)}% of the track; "
                                         f"{world['name']} norms)"})

    run_start, prev = 0, None
    for i, d in enumerate(densities + [None]):
        if prev is not None and (d is None or abs(d - prev) > 0.15 * max(prev, 1e-9)):
            if i - run_start >= world["flat_run"]:
                findings.append({"detector": "flat_energy_run", "severity": "ask",
                                 "bars": [run_start + 1, i],
                                 "evidence": f"is this intended? (held texture / one section by design); "
                                             f"note density / track count (MIDI only; audio unseen) "
                                             f"stays similar for {i - run_start} bars"})
            run_start = i
        prev = d

    body = bars[: max(1, int(total * 0.75))]
    if med > 0 and not any(b["density"] <= 0.5 * med for b in body):
        findings.append({"detector": "no_breathe", "severity": "medium", "bars": None,
                         "evidence": "no bar in the first three quarters drops below half "
                                     "the median density — the track never inhales"})

    if len(sections) >= 2:
        peak = max(sections, key=lambda s: s["density"])
        idx = sections.index(peak)
        if idx > 0 and sections[idx - 1]["density"] >= 0.85 * peak["density"]:
            findings.append({"detector": "drop_without_contrast", "severity": "medium",
                             "bars": [peak["start_bar"], peak["end_bar"]],
                             "context_bars": [sections[idx - 1]["start_bar"], peak["end_bar"]],
                             "evidence": "the densest section arrives from a section already "
                                         "at ≥85% of its density — no contrast to land on"})

    tail = bars[-min(8, total):]
    if med > 0 and _mean([b["density"] for b in tail]) >= world["tail_frac"] * med:
        findings.append({"detector": "uncommitted_ending", "severity": "ask",
                         "bars": [total - len(tail) + 1, total],
                         "evidence": "is this intended? (hard stop / held texture / one section by design); "
                                     "note density / track count (MIDI only; audio unseen) "
                                     "stays near the median at the end"})

    if reading["comping_debris"]:
        findings.append({"detector": "comping_debris", "severity": "low", "bars": None,
                         "evidence": f"{reading['comping_debris']} take-lane clips still "
                                     "stacked — decisions deferred"})

    mids = [s for s in sections if s is not sections[0] and s is not sections[-1]]
    if len({s["signature"] for s in mids}) <= 1 and total >= 32:
        findings.append({"detector": "b_section_missing", "severity": "ask", "bars": None,
                         "evidence": "is this intended? (one section by design); "
                                     "note density / track count (MIDI only; audio unseen) "
                                     "shows the same track signature across sections"})

    timeline = [t for t in reading.get("timeline", []) if not t.get("unread")]
    melodic = [t for t in timeline if t["role"] != "drum"]
    if melodic:
        if not any(t["role"] == "bass" or t["register"] < 48 for t in melodic):
            findings.append({"detector": "no_low_end", "severity": "medium", "bars": None,
                             "evidence": "nothing anchors below the pads — no bass role, "
                                         "no melodic material under C2"})
        for i, a in enumerate(melodic):
            for b in melodic[i + 1:]:
                overlap = min(a["end_bar"], b["end_bar"]) - max(a["start_bar"], b["start_bar"])
                if (a["role"] != b["role"] and overlap > 8
                        and abs(a["register"] - b["register"]) <= 5):
                    findings.append({"detector": "register_collision", "severity": "medium",
                                     "bars": [int(max(a["start_bar"], b["start_bar"])) + 1,
                                              int(min(a["end_bar"], b["end_bar"]))],
                                     "evidence": f"{a['role']} ({a['track']!r}) and {b['role']} "
                                                 f"({b['track']!r}) sit within 5 semitones for "
                                                 f"{int(overlap)} bars — fighting for the same air"})
                    break
            else:
                continue
            break
        note_mass = sum(t["n_notes"] for t in melodic) or 1
        flat_mass = sum(t["n_notes"] for t in melodic if t["vel_distinct"] <= 1)
        if flat_mass / note_mass >= 0.7:
            findings.append({"detector": "flat_velocity", "severity": "medium", "bars": None,
                             "evidence": f"{flat_mass * 100 // note_mass}% of melodic notes sit "
                                         "at one velocity — drawn, not played"})
        og = sum(t["offgrid_frac"] * t["n_notes"] for t in melodic) / note_mass
        if og < 0.05:
            findings.append({"detector": "machine_timing", "severity": "low", "bars": None,
                             "evidence": f"only {og:.0%} of melodic notes leave the grid "
                                         "(his played norm is ~56%) — robot time"})

    # silent_gap: a run of bars with NOTHING on the MIDI timeline strictly between
    # material. Audio may fill it — the evidence says so; the frame lists audio tracks.
    occupied = [i for i, b in enumerate(bars) if b["tracks"]]
    if occupied:
        first, last = occupied[0], occupied[-1]
        i = first
        while i <= last:
            if bars[i]["tracks"]:
                i += 1
                continue
            j = i
            while j <= last and not bars[j]["tracks"]:
                j += 1
            if j - i >= SILENT_GAP_MIN_BARS:
                findings.append({"detector": "silent_gap", "severity": "high",
                                 "bars": [i + 1, j],
                                 "evidence": f"{j - i} bars with nothing on the MIDI timeline "
                                             f"between material (bars {i + 1}–{j}) — a hole, "
                                             "not a breath, unless audio fills it"})
            i = j

    # unison_wall: three or more tracks carrying the identical figure at the same time
    groups: dict = {}
    for t in timeline:
        sig = t.get("note_sig")
        if sig and len(sig) >= 2:
            groups.setdefault(sig, []).append(t)
    for sig, members in sorted(groups.items(), key=lambda kv: sorted(kv[0])):
        if len({m["track"] for m in members}) < UNISON_MIN_TRACKS:
            continue
        for b in range(total):
            active = [m for m in members if m["start_bar"] <= b < m["end_bar"]]
            names = sorted({m["track"] for m in active})
            if len(names) >= UNISON_MIN_TRACKS:
                end_b = int(min(m["end_bar"] for m in active))
                findings.append({"detector": "unison_wall", "severity": "medium",
                                 "bars": [b + 1, end_b], "tracks": names,
                                 "evidence": f"{len(names)} tracks ({', '.join(names)}) play the "
                                             f"identical {len(sig)}-note figure for bars "
                                             f"{b + 1}–{end_b} — one voice {len(names)} times, "
                                             f"not {len(names)} voices"})
                break
    return findings


# --- proposals: findings → verb-menu moves ---------------------------------------

# detector → TYPED intervention: an id, a verb from the menu, a strategy line, and —
# where the tool already exists — the executable command. This is the wiring between
# diagnosis and the gearbox: proposals are commands, not prose.
_PROPOSAL_MAP = {
    "loop_with_ambitions": ("make_b_section", "answer",
                            "compose the B as this section's negative image, landed in "
                            "take lanes under it",
                            {"tool": "answer_my_loop", "args": {"gear": 5}}),
    "b_section_missing": ("make_b_section", "answer",
                          "negative-image B: invert density/register/rhythm placement, "
                          "keep key + one motif anchor",
                          {"tool": "answer_my_loop", "args": {"gear": 5}}),
    "flat_energy_run": ("raise_energy_8bars", "thin",
                        "subtract one voice for 4-8 bars mid-run, or ride volume/send "
                        "curves from his phrase-curve library (a lab-profile move — "
                        "no compose-profile tool executes it, so this proposal "
                        "is prose until one does)",
                        None),
    "no_breathe": ("cut_breathe_4bars", "remove",
                   "cut to ONE voice for 4 bars around the golden-ratio point — his "
                   "BREATHE is subtraction, not filtering", None),
    "drop_without_contrast": ("starve_pre_drop", "thin",
                              "starve the bar before the peak (silence is legal) so the "
                              "drop has a floor to land from", None),
    "uncommitted_ending": ("three_endings", "arrange",
                           "THREE ENDINGS: select the project's last-section clip and "
                           "pass it as slot or seed_notes; explore decay / subtraction / "
                           "hard-stop into room tone, comped like takes",
                           {"tool": "three_endings", "args": {}}),
    "comping_debris": ("comp_session", "arrange",
                       "promote or bury each stacked take; losers feed the crate", None),
    "no_timeline": ("lay_the_map", "arrange",
                    "lay_the_song on the strongest loop — locators first",
                    {"tool": "lay_the_song", "args": {}}),
    "no_low_end": ("add_bass_shadow", "thicken",
                   "the lead's shadow: strong beats dropped into the bass register",
                   {"tool": "answer_my_loop", "args": {"gear": 3, "surgery": "bass_shadow"}}),
    "register_collision": ("separate_registers", "transpose",
                           "move one role an octave — two voices, two floors", None),
    "flat_velocity": ("humanize_pass", "humanize",
                      "his velocity/timing DNA over the drawn parts",
                      {"tool": "humanize", "args": {"strength": 1.0}}),
    "machine_timing": ("humanize_pass", "humanize",
                       "loosen the grid toward his ~56% off-grid feel",
                       {"tool": "humanize", "args": {"strength": 1.0, "timing": True}}),
    "silent_gap": ("bridge_or_cut_gap", "arrange",
                   "answer the last bar before the hole into a transition, or cut the "
                   "timeline so the gap is a decision", None),
    "unison_wall": ("thin_unison", "thin",
                    "keep one voice per register; give the doublers rests, an octave, "
                    "or a displaced rhythm", None),
}

INTERVENTIONS = sorted({v[0] for v in _PROPOSAL_MAP.values()})


def _playback_segments(clip: dict) -> list[tuple[float, float, float]] | None:
    """One arrangement pass of a parse_als clip as ordered content-time segments
    (content_from, content_to, arrangement_offset), unrolled across end - start.

    Loop ON: playback starts at the raw start marker S (parse_als start_marker =
    LoopStart + StartRelative), runs to LoopEnd, then repeats [LoopStart,
    LoopEnd). No start_marker (an old record, or StartRelative out of range)
    returns None — never a guessed LoopStart phase. Loop OFF: one segment
    [audible_start, audible_end) — the start/end markers clamped to the pass.
    Intervals are half-open: a note exactly at LoopEnd never sounds. Returns
    None when the needed fields are missing."""
    clip_start, clip_end = clip.get("start"), clip.get("end")
    brace = clip.get("playback_brace")
    if isinstance(brace, dict):     # an open-Set (project='live') clip
        clip = {**clip, **{k: v for k, v in brace.items() if v is not None}}
    loop_start, loop_end = clip.get("loop_start"), clip.get("loop_end")
    if clip_start is None or clip_end is None or loop_start is None or loop_end is None:
        return None
    length = float(clip_end) - float(clip_start)
    loop_start, loop_end = float(loop_start), float(loop_end)
    if loop_end <= loop_start or length <= 0:
        return None
    if not clip.get("loop_on", True):
        begin = clip.get("audible_start")
        finish = clip.get("audible_end")
        begin = loop_start if begin is None else float(begin)
        finish = min(loop_end, loop_start + length) if finish is None else float(finish)
        finish = min(finish, begin + length)
        return [(begin, finish, 0.0)] if finish > begin else None
    marker = clip.get("start_marker")
    if marker is None:
        return None
    marker = float(marker)
    if not loop_start <= marker < loop_end:
        return None
    segments, offset, begin = [], 0.0, marker
    while offset < length:
        span = min(loop_end - begin, length - offset)
        segments.append((begin, begin + span, offset))
        offset += span
        begin = loop_start
    return segments


def _ending_seed_notes(project_dict: dict, bars: list[int] | None) -> list[dict]:
    """Return the arrangement MIDI notes that actually sound in the finding span,
    in playback order and rebased to its start. Notes outside a clip's played
    segments are skipped; a clip missing its loop fields, or an unread clip
    (notes not read) sounding in the span, returns [] (the caller then keeps
    the instruction instead of shipping guessed or partial material)."""
    if not bars or len(bars) != 2:
        return []
    start = (int(bars[0]) - 1) * BEATS_PER_BAR
    end = int(bars[1]) * BEATS_PER_BAR
    notes = []
    for clip in project_dict.get("clips") or []:
        if clip.get("clip_source", "legacy") not in {"arrangement", "legacy"}:
            continue
        # A deactivated clip is silent in Live, so it never seeds an ending.
        # Scoped to the seeds: corpus ingestion and the structure reading
        # still count it (unchanged this batch).
        if clip.get("disabled"):
            continue
        clip_start = clip.get("start")
        if clip_start is None:
            continue
        clip_start = float(clip_start)
        clip_end = float(clip.get("end") or clip_start)
        if clip_start >= end or clip_end <= start:
            continue
        if clip.get("unread"):
            # its notes are unknown: the rest of the span is not what plays
            return []
        segments = _playback_segments(clip)
        if segments is None:
            return []
        for seg_from, seg_to, seg_offset in segments:
            seg_abs = clip_start + seg_offset
            if seg_abs >= end or seg_abs + (seg_to - seg_from) <= start:
                continue
            for note in clip.get("notes") or []:
                if not note.get("enabled", True):
                    continue
                content_start = float(note.get("start", 0.0))
                if not seg_from <= content_start < seg_to:
                    continue
                absolute_start = seg_abs + (content_start - seg_from)
                sounding = min(float(note.get("duration", 0.0)), seg_to - content_start)
                absolute_end = min(absolute_start + sounding, clip_end)
                if absolute_start >= end or absolute_end <= start:
                    continue
                notes.append({"pitch": int(note["pitch"]),
                              "start": max(absolute_start, start) - start,
                              "duration": min(absolute_end, end) - max(absolute_start, start),
                              "velocity": int(note.get("velocity", 100)),
                              "enabled": True})
    return sorted(notes, key=lambda note: (note["start"], note["pitch"]))


def propose(findings: list[dict], project_dict: dict | None = None) -> list[dict]:
    out = []
    for f in findings:
        intervention, verb, strategy, command = _PROPOSAL_MAP[f["detector"]]
        if f["detector"] == "uncommitted_ending":
            seed_notes = _ending_seed_notes(project_dict or {}, f.get("bars"))
            if seed_notes:
                command = {"tool": "three_endings", "args": {"seed_notes": seed_notes}}
            else:
                command = None
                strategy = ("THREE ENDINGS: select the project's last-section MIDI clip and "
                            "pass it as slot or seed_notes; explore decay / subtraction / "
                            "hard-stop into room tone, comped like takes")
        out.append({"intervention": intervention, "verb": verb,
                    "detector": f["detector"], "bars": f["bars"],
                    "strategy": strategy, "command": command})
    return out


def _is_intended_protect(row: dict) -> bool:
    """An owner 'this finding is intended' row: finding_status now; rows written
    before batch 5's final review used the verdict decision field."""
    return (row.get("finding_status") == "intended"
            or (row.get("kind") == "protect" and row.get("verdict") == "intended"))


_FINGERPRINT_EXCLUDED = ("start_marker", "disabled", "playback_brace")


# Fingerprint keys a clip's start phase / brace adds; the phase-free
# fingerprint (rows recorded while playback was unknown) omits them all.
_BRACE_FINGERPRINT_KEYS = ("start_phase", "loop_length", "played_length", "audible_start")


def _live_brace_keys(clip: dict) -> dict:
    """Non-default played length of a project='live' clip (see _playback_keys)."""
    brace = clip.get("playback_brace")
    if not isinstance(brace, dict) or clip.get("start") is None or clip.get("end") is None:
        return {}
    span = round(float(clip["end"]) - float(clip["start"]), 6)
    try:
        if clip.get("loop_on", True):
            length = round(float(brace["loop_end"]) - float(brace["loop_start"]), 6)
            return {"loop_length": length} if length != span else {}
        begin, finish = float(brace["audible_start"]), float(brace["audible_end"])
    except (KeyError, TypeError, ValueError):
        return {}
    keys = {}
    if round(finish - begin, 6) != span:
        keys["played_length"] = round(finish - begin, 6)
    loop_start = brace.get("loop_start")
    if loop_start is not None and round(begin - float(loop_start), 6):
        keys["audible_start"] = round(begin, 6)
    return keys


def _playback_keys(clip: dict) -> tuple[dict, bool]:
    """Normalized playback evidence of one clip for the intent fingerprint,
    and whether its start phase is unknown.

    Only NON-DEFAULT playback adds a key: a loop-on start marker off LoopStart
    adds ``start_phase`` (marker - LoopStart, rounded), a deactivated clip adds
    ``disabled: True``. Default playback — marker on LoopStart, clip active, or
    an old record missing either field (mapped to that default) — adds
    nothing, so it hashes exactly as rows recorded before playback counted
    and a re-parse of the same Set hashes the same. A loop-on clip without a
    usable marker (old record, or StartRelative out of range) has an unknown
    phase.

    A project='live' clip carries its brace outside the hashed body
    (``playback_brace``); its played length enters here, again only when
    non-default: ``loop_length`` (loop on) or ``played_length`` plus
    ``audible_start`` (loop off: the start/end markers) when the brace does
    not play exactly once across the clip's arrangement span. A loop_end
    moved in the open Set therefore re-asks. parse_als records hash their
    loop fields in the body already."""
    keys: dict = {}
    if clip.get("disabled"):
        keys["disabled"] = True
    keys.update(_live_brace_keys(clip))
    if not clip.get("loop_on", True):
        return keys, False
    marker, loop_start = clip.get("start_marker"), clip.get("loop_start")
    if marker is None:
        return keys, True
    phase = round(float(marker) - float(loop_start or 0.0), 6)
    if phase:
        keys["start_phase"] = phase
    return keys, False


def _intent_fingerprints(project_dict: dict, detector: str, bars) -> tuple[str, str, bool]:
    """(playback fingerprint, phase-free fingerprint, phase unknown) of the
    material in one finding's span.

    The playback fingerprint binds an intended finding to what actually
    plays there: loop brace, audible regions, notes, and (normalized, see
    _playback_keys) the start phase and deactivation. The phase-free
    fingerprint omits only the start phase; it matches rows recorded while
    a phase was unknown (``playback_unknown``), so re-materializing such a
    record with its new start_marker field keeps the intent (unless the same
    span also held a known rotated phase at record time: then it re-asks
    once)."""
    clips = project_dict.get("clips") or []
    if isinstance(bars, (list, tuple)) and len(bars) == 2:
        start = (float(bars[0]) - 1) * BEATS_PER_BAR
        end = float(bars[1]) * BEATS_PER_BAR
        clips = [clip for clip in clips
                 if clip.get("start") is not None and clip.get("end") is not None
                 and float(clip["start"]) < end and float(clip["end"]) > start]
    full_clips, base_clips, unknown = [], [], False
    for clip in clips:
        body = {k: v for k, v in clip.items() if k not in _FINGERPRINT_EXCLUDED}
        keys, phase_unknown = _playback_keys(clip)
        unknown = unknown or phase_unknown
        full_clips.append({**body, **keys})
        base_clips.append({**body, **{k: v for k, v in keys.items()
                                      if k not in _BRACE_FINGERPRINT_KEYS}})

    def digest(material_clips):
        material = {"detector": detector, "bars": bars,
                    "tempo": project_dict.get("tempo"), "clips": material_clips}
        return hashlib.sha256(json.dumps(material, sort_keys=True, default=str,
                                         separators=(",", ":")).encode()).hexdigest()
    return digest(full_clips), digest(base_clips), unknown

INTENT_DETECTORS = ("uncommitted_ending", "flat_energy_run", "loop_with_ambitions",
                    "b_section_missing")


def _instance_label(finding: dict) -> str:
    bars = finding.get("bars")
    if isinstance(bars, (list, tuple)) and len(bars) == 2:
        return "%s@%d-%d" % (finding["detector"], int(bars[0]), int(bars[1]))
    return finding["detector"]


def _intent_targets(entry, findings: list[dict]) -> list[dict]:
    """The raised finding instance(s) one intended_findings entry names.

    Forms: {"detector": d, "bars": [a, b]} or "d@a-b" (one span), "d:all" (every
    instance), or a bare "d" (only when d fired exactly once)."""
    bars, every = None, False
    if isinstance(entry, dict):
        detector = str(entry.get("detector") or "")
        bars = entry.get("bars")
    else:
        detector = str(entry).strip()
        if detector.endswith(":all"):
            detector, every = detector[:-len(":all")], True
        elif "@" in detector:
            detector, _, span = detector.partition("@")
            first, sep, last = span.partition("-")
            try:
                bars = [int(first), int(last)] if sep else None
            except ValueError:
                bars = None
            if bars is None:
                raise ValueError("intended finding span must read detector@start-end: %s"
                                 % entry)
    if detector not in INTENT_DETECTORS:
        raise ValueError("unsupported intended finding: %s" % detector)
    instances = [f for f in findings if f["detector"] == detector]
    if not instances:
        raise ValueError("intended finding was not raised in this diagnosis: %s" % detector)
    choices = ", ".join([_instance_label(f) for f in instances]
                        + (["%s:all" % detector] if len(instances) > 1 else []))
    if every:
        return instances
    if bars is not None:
        try:
            wanted = [int(bars[0]), int(bars[1])]
        except (TypeError, ValueError, IndexError):
            raise ValueError("intended finding bars must be [start, end]: %r" % (entry,))
        hits = [f for f in instances if f.get("bars") and [int(x) for x in f["bars"]] == wanted]
        if not hits:
            raise ValueError("%s did not fire at bars %d-%d; choose one of: %s"
                             % (detector, wanted[0], wanted[1], choices))
        return hits
    if len(instances) > 1:
        raise ValueError("%s fired %d times; name the instance you mean: %s"
                         % (detector, len(instances), choices))
    return instances


def whats_missing(project: str | None = None, project_dict: dict | None = None,
                  intended_findings: list | None = None) -> dict:
    """Structured diagnosis of a materialized project (by name/id) or an explicit
    project dict. Facts + findings + verb proposals; the LLM writes the words.
    Pass intended_findings only when Ricardo explicitly said the named finding is
    intentional; it records that owner decision for this material and Set, one
    row per finding INSTANCE. An entry names an instance ({"detector": d,
    "bars": [a, b]} or "d@a-b"), every instance ("d:all"), or a bare "d" when d
    fired once — a bare name that fired more than once is refused with the
    instances to choose from. Re-marking an already intended instance records
    nothing. The result's intent_recorded lists each targeted instance with
    status recorded / already_intended. B-section link: an intended
    loop_with_ambitions instance silences that span and b_section_missing
    (one section by design); an intended b_section_missing names no span, so it
    silences only itself. A bare string is one entry (wrapped as a one-item
    list, never split into characters). Refused for an unsaved or unverified
    open Set, and for a truncated live read (max_tracks below the Set's track
    count): an intent is keyed to the whole Set's material, so a partial read
    can neither record one nor be trusted to match it. Owner protect rows carry
    finding_status="intended" (rows written before that carry
    verdict="intended" and still count)."""
    if isinstance(intended_findings, str):
        intended_findings = [intended_findings]
    if project_dict is None:
        if not project:
            raise ValueError("pass project= (catalog name/id) or project_dict=")
        project_dict = resolve_project(project)
    if intended_findings:
        reason = _unverified_set_reason(project_dict)
        if reason:
            raise ValueError(reason + " before recording intended findings")
        truncated = project_dict.get("read_truncated")
        if truncated:
            raise ValueError(
                "the Set has %s regular tracks but this read covered only %s (max_tracks); "
                "rerun with max_tracks of at least %s before recording intended findings"
                % (truncated.get("total"), truncated.get("read"), truncated.get("total")))
    reading = read_structure(project_dict)
    findings = run_detectors(reading)
    fingerprints: dict[int, tuple[str, str, bool]] = {}

    def fingerprints_of(finding: dict) -> tuple[str, str, bool]:
        if id(finding) not in fingerprints:
            fingerprints[id(finding)] = _intent_fingerprints(
                project_dict, finding["detector"], finding.get("bars"))
        return fingerprints[id(finding)]

    def is_protected(finding: dict) -> bool:
        full, base, _ = fingerprints_of(finding)
        return ((finding["detector"], full) in protected
                or (finding["detector"], base) in protected_phase_unknown)
    set_key = str(project_dict.get("path", project or "?"))
    intent_path = piece_context._decision_path()
    try:
        with open(intent_path, encoding="utf-8") as handle:
            decisions = json.load(handle).get("decisions", [])
    except FileNotFoundError:
        decisions = []
    except (OSError, ValueError, TypeError) as exc:
        import warnings
        warnings.warn(f"could not read piece_decisions.json ({exc}); treating as no decisions",
                      RuntimeWarning)
        decisions = []
    intent_rows = [row for row in decisions
                   if row.get("set_key") == set_key and row.get("kind") == "protect"
                   and row.get("source") in {"owner", "model_on_request"}
                   and _is_intended_protect(row)]
    protected = {(row.get("finding"), row.get("material_fingerprint"))
                 for row in intent_rows}
    # Rows recorded while a clip's start phase was unknown match the
    # phase-free fingerprint; every other row (including legacy rows, whose
    # missing fields mean default playback) must match what plays now.
    protected_phase_unknown = {(row.get("finding"), row.get("material_fingerprint"))
                               for row in intent_rows if row.get("playback_unknown")}
    intent_recorded = []
    if intended_findings:
        targets: list[dict] = []
        for entry in intended_findings:       # validate every entry before writing
            for finding in _intent_targets(entry, findings):
                if not any(finding is seen for seen in targets):
                    targets.append(finding)
        for finding in targets:
            full, _, phase_unknown = fingerprints_of(finding)
            key = (finding["detector"], full)
            status = "already_intended" if is_protected(finding) else "recorded"
            if status == "recorded":
                row = {"kind": "protect", "source": "model_on_request",
                       "finding": finding["detector"], "material_fingerprint": full,
                       "finding_status": "intended"}
                if phase_unknown:
                    # Matched later against the phase-free fingerprint. In a
                    # span that also holds a clip with a KNOWN rotated phase,
                    # full != phase-free, so filling in the unknown marker
                    # re-asks once (the safe direction).
                    row["playback_unknown"] = True
                    protected_phase_unknown.add(key)
                piece_context.record_decision(set_key, row)
                protected.add(key)
            intent_recorded.append({"detector": finding["detector"],
                                    "bars": finding.get("bars"), "status": status})
    one_section_by_design = any(
        f["detector"] == "loop_with_ambitions" and is_protected(f)
        for f in findings)
    findings = [finding for finding in findings
                if not is_protected(finding)
                and not (one_section_by_design
                         and finding["detector"] == "b_section_missing")]
    sev_rank = {"high": 0, "ask": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda f: sev_rank[f["severity"]])
    return {
        "project": project_dict.get("path", project or "?"),
        "frame": {
            "tempo": reading["tempo"],
            "total_bars": reading["total_bars"],
            "minutes": round(reading["total_bars"] * BEATS_PER_BAR /
                             max(reading["tempo"] or 120.0, 1e-9) , 2) if reading["total_bars"] else 0,
            "n_sections": len(reading["sections"]),
            "tracks": reading["tracks"],
            "audio_tracks": len(project_dict.get("audio_track_names") or []),
        },
        "assumptions": list(project_dict.get("assumptions") or []) + (
            ["%d note-based finding%s withheld over bars %s: those clips' notes were not "
             "read, so their bars count as occupied with unknown notes (see suppressed)"
             % (len(reading["suppressed"]), "" if len(reading["suppressed"]) == 1 else "s",
                _bar_ranges(reading["unread_bars"]))]
            if reading.get("suppressed") else []),
        "sections": [{k: v for k, v in s.items() if k != "signature"}
                     for s in reading["sections"]],
        "findings": findings,
        "suppressed": reading.get("suppressed") or [],
        "intent_recorded": intent_recorded,
        "proposals": propose(findings, project_dict=project_dict),
        "verb_menu": list(VERB_MENU),
        "norms": {"song_bars": "~148 @120-world", "breathe": "density halves ~1/3 in",
                  "sections": "mostly 8 bars with real 4s and 16s",
                  "ending": "decay, not cadence"},
    }
