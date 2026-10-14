"""Phrase index — the 'compose-like-me' retrieval engine.

Slices Ricardo's materialized catalog into short phrases (windows of his real
clips), embeds each as a lightweight, interpretable musical feature vector, and
stores them so we can answer "find/show phrases like X in my own work."

The embedding is hand-built (no neural model, no downloads): per phrase we encode
pitch-class profile, rhythm (16th grid), melodic-interval shape, note-length mix,
and a few scalars (density, register, polyphony). Each block is L2-normalised so
all musical dimensions get a fair say, then concatenated. Similarity = cosine.
At a few thousand phrases this is instant in pure Python and you can SEE why two
phrases match.

Usage:
    python3 analyze/build_index.py            # build store/phrase_index.json
    python3 analyze/build_index.py --demo     # build, then show a similarity demo
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJ_DIR = os.path.join(ROOT, "store", "projects")
INDEX_PATH = os.path.join(ROOT, "store", "phrase_index.json")
PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

sys.path.insert(0, os.path.join(ROOT, "ingest"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_als import clip_source_of, is_blocked_project, is_melodic_clip_source  # noqa: E402
from corpus_keys import (  # noqa: E402
    ID_MAP_FILENAME,
    check_duplicate_pids,
    dedupe_phrases,
    phrase_pid,
    write_id_map,
)
from roles import ROLE_SOURCE, clip_role  # noqa: E402

ID_MAP_PATH = os.path.join(ROOT, "store", ID_MAP_FILENAME)

WINDOW = 8.0          # phrase length in beats (2 bars of 4/4)
MIN_NOTES = 4         # ignore near-empty windows
MAX_WIN_PER_CLIP = 16  # don't let one long arrangement clip dominate


def _git_rev() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        ).strip()
        return out or "unknown"
    except Exception:
        return "unknown"


def _iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_meta(n_projects: int, schema_version: int = 1,
               manifest_time: str | None = None,
               n_windows: int | None = None) -> dict:
    corpus = {"n_projects": int(n_projects)}
    if manifest_time is not None:
        corpus["manifest_time"] = manifest_time  # catalog scan this build saw
    if n_windows is not None:
        # Raw pre-dedupe window count: the shrink gate compares this, not the
        # deduped phrase count (dedupe improvements move that without loss).
        corpus["n_windows"] = int(n_windows)
    return {
        "schema_version": schema_version,
        "built_at": _iso_utc(),
        "code_rev": _git_rev(),
        "corpus": corpus,
    }


def _save_json_atomic(path: str, data: dict, indent: int | None = None) -> None:
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
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


# --- feature vector ---------------------------------------------------------

def _l2(block):
    n = math.sqrt(sum(x * x for x in block))
    return [x / n for x in block] if n else block


def _global_l2(vec):
    """Normalise the whole vector to unit length so cosine = plain dot product."""
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


# Drum roles by MIDI pitch (Ableton Drum Racks follow the GM-ish convention:
# kick≈36, snare/clap≈38-40, hats≈42-46…). Custom kits may remap; "perc" catches the rest.
_ROLE_NAMES = ["kick", "snare", "hats", "toms", "cymbals", "perc"]


def _drum_role(pitch):
    if pitch in (35, 36): return 0
    if pitch in (37, 38, 39, 40): return 1
    if pitch in (42, 44, 46): return 2
    if pitch in (41, 43, 45, 47, 48, 50): return 3
    if pitch in (49, 51, 52, 53, 55, 57, 59): return 4
    return 5


def drum_vector(notes):
    """Groove fingerprint: a 16-step onset grid per drum role (6×16 = 96 dims)."""
    grid = [0.0] * (6 * 16)
    for n in notes:
        role = _drum_role(n["pitch"])
        step = int(round((n["start"] % 4.0) * 4)) % 16
        grid[role * 16 + step] += 1
    return _global_l2(grid)


def _dur_idx(d):
    for i, val in enumerate([0.25, 0.5, 1.0, 2.0]):
        if d <= val * 1.25:
            return i
    return 4


def phrase_vector(notes):
    """notes: list of dicts with pitch/start/duration/velocity (start re-based to 0)."""
    pc = [0.0] * 12
    onset = [0.0] * 16
    dur = [0.0] * 5
    for n in notes:
        pc[n["pitch"] % 12] += 1
        onset[int(round((n["start"] % 4.0) * 4)) % 16] += 1
        dur[_dur_idx(n["duration"])] += 1

    # melodic intervals along the skyline (top note per onset slot)
    top = {}
    for n in notes:
        slot = round(n["start"] * 4)
        if slot not in top or n["pitch"] > top[slot]["pitch"]:
            top[slot] = n
    seq = [top[k]["pitch"] for k in sorted(top)]
    iv = [0.0] * 7  # <=-5, -4..-3, -2..-1, 0(repeat), 1..2, 3..4, >=5
    for a, b in zip(seq, seq[1:]):
        d = b - a
        if d <= -5: iv[0] += 1
        elif d <= -3: iv[1] += 1
        elif d <= -1: iv[2] += 1
        elif d == 0: iv[3] += 1
        elif d <= 2: iv[4] += 1
        elif d <= 4: iv[5] += 1
        else: iv[6] += 1

    pitches = [n["pitch"] for n in notes]
    poly = list(Counter(round(n["start"] * 4) for n in notes).values())
    scal = [
        min(len(notes) / WINDOW / 4.0, 1.0),                      # density
        (sum(pitches) / len(pitches)) / 127.0,                    # mean register
        (max(pitches) - min(pitches)) / 60.0,                     # range (cap ~5 oct)
        min((sum(poly) / len(poly)) / 4.0, 1.0) if poly else 0.0,  # polyphony
    ]
    # Per-block unit norm (equal say per musical dimension), then global unit norm.
    return _global_l2(_l2(pc) + _l2(onset) + _l2(dur) + _l2(iv) + _l2(scal))


def cosine(a, b):
    # Both vectors are globally unit-normalised, so the dot product IS the cosine
    # similarity (0..1 here; 1.0 = identical). Only valid within one phrase kind
    # (melody vectors are 44-dim, drum vectors 96-dim).
    return sum(x * y for x, y in zip(a, b))


# --- phrase extraction ------------------------------------------------------

def iter_phrases(data):
    name = os.path.splitext(os.path.basename(data["path"]))[0]
    for ci, c in enumerate(data["clips"]):
        source = clip_source_of(c)
        if not is_melodic_clip_source(source):   # phrase retrieval pool incl. take_lane (melodic only)
            continue
        if c.get("is_sidekick"):   # sidekick output tracks are not his playing
            continue
        notes = [n for n in c["notes"] if n.get("enabled", True)]
        a0, a1 = c.get("audible_start"), c.get("audible_end")
        regions = c.get("audible_regions")
        if regions:
            # A wrapped pass: lay the played slices end to end in playback
            # order, so windows see what he hears (tail then head) and never
            # the unheard middle of the brace.
            played, offset = [], 0.0
            for r0, r1 in regions:
                for n in notes:
                    if r0 <= n["start"] < r1:
                        s0 = max(n["start"], r0)
                        played.append({**n, "start": offset + (s0 - r0),
                                       "duration": min(n["start"] + n["duration"], r1) - s0})
                offset += r1 - r0
            notes, a0, a1 = played, 0.0, offset
        elif a0 is not None or a1 is not None:
            # Window only what he hears: notes cropped out by the loop brace
            # never consume MAX_WIN_PER_CLIP.
            lo_b = a0 if a0 is not None else float("-inf")
            hi_b = a1 if a1 is not None else float("inf")
            notes = [n for n in notes
                     if n["start"] >= lo_b and n["start"] < hi_b
                     and n["start"] + n["duration"] > lo_b]
        if len(notes) < MIN_NOTES:
            continue
        starts = [n["start"] for n in notes]
        lo, hi = min(starts), max(starts)
        win = 0
        # Windows open at the audible start (what he hears), not at the
        # floored first-note position (which spends windows on cropped time).
        # Legacy clips without loop markers keep the old grid (byte-identical).
        w = float(a0) if a0 is not None else math.floor(lo / WINDOW) * WINDOW
        while w <= hi and win < MAX_WIN_PER_CLIP:
            seg = [n for n in notes
                   if n["start"] < w + WINDOW and n["start"] + n["duration"] > w]
            if len(seg) >= MIN_NOTES:
                rebased = [{"pitch": n["pitch"],
                            "start": round(max(0.0, n["start"] - w), 4),
                            "duration": round(n["duration"], 4), "velocity": n["velocity"]}
                           for n in seg if round(n["duration"], 4) > 0.0]
                is_drum = c["is_drum"]
                vec = drum_vector(rebased) if is_drum else phrase_vector(rebased)
                alias = f"{name}|c{ci}|w{int(w)}"
                role = clip_role(rebased, is_drum)
                pid = phrase_pid(rebased, role=role)
                yield {
                    # The record id IS the pid (stable across rebuilds); the
                    # positional id survives as alias for old stored ids.
                    "id": pid,
                    "pid": pid,
                    "alias": alias,
                    "project": name,
                    "window": int(w),
                    "track": c["track"],
                    "clip": c.get("name", ""),
                    "audible": True,  # windows cover the audible region only
                    "clip_source": source,
                    "kind": "drum" if is_drum else "harmonic",
                    "role": role,
                    "role_source": ROLE_SOURCE,
                    "tempo": data["tempo"],
                    "n_notes": len(seg),
                    "notes": rebased,
                    "vec": [round(x, 4) for x in vec],
                }
            win += 1
            w += WINDOW


# --- build / search ---------------------------------------------------------

def build(limit=0, id_map_path: str | None = None):
    with open(os.path.join(PROJ_DIR, "_index.json")) as fh:
        index = json.load(fh)
    n_all = len(index)
    index = [r for r in index if not is_blocked_project(r["name"])]
    if n_all != len(index):
        print(f"  blocklist: skipping {n_all - len(index)} pack/demo projects")
    if limit:
        index = index[:limit]
    phrases = []
    for i, r in enumerate(index, 1):
        with open(os.path.join(PROJ_DIR, f"{r['id']}.json")) as fh:
            data = json.load(fh)
        phrases.extend(iter_phrases(data))
        if i % 50 == 0 or i == len(index):
            print(f"  {i}/{len(index)} projects, {len(phrases)} phrases")
    n_raw = len(phrases)
    phrases, collapsed = dedupe_phrases(phrases)  # one record per note-set
    # Uniqueness runs after dedupe: same notes anywhere share one pid, so a
    # pre-dedupe check would refuse every Save-As duplicate (59/191 projects).
    check_duplicate_pids(phrases)
    if collapsed:
        print(f"  dedupe: {n_raw} windows → {len(phrases)} distinct phrases "
              f"({len(collapsed)} Save-As duplicates folded into provenance)")
    out = {
        "meta": build_meta(len(index), n_windows=n_raw),
        "window_beats": WINDOW,
        "count": len(phrases),
        "phrases": phrases,
    }
    # The id map migrates stored old ids: read the previous index BEFORE
    # overwriting it (missing on a first build → empty map, nothing to migrate).
    try:
        with open(INDEX_PATH) as fh:
            prev = json.load(fh)
        old_phrases = prev.get("phrases", []) if isinstance(prev, dict) else []
    except (OSError, ValueError):
        old_phrases = []
    _save_json_atomic(INDEX_PATH, out, indent=None)
    id_map = write_id_map(old_phrases, phrases, id_map_path or ID_MAP_PATH)
    if id_map["ambiguous"]:
        print(f"  id map: {len(id_map['map'])} aliases migrated, "
              f"{len(id_map['ambiguous'])} ambiguous (left unmapped)")
    return phrases


def search(phrases, vec, k=5, kind=None, exclude_id=None, exclude_project=None,
           diversify=True, dup_thresh=0.985, max_sim=None):
    cands = [p for p in phrases
             if (kind is None or p["kind"] == kind)
             and p["id"] != exclude_id
             and (exclude_project is None or p["project"] != exclude_project)]
    if max_sim is not None:  # drop near-duplicates of the query ("show me OTHER takes")
        cands = [p for p in cands if cosine(vec, p["vec"]) < max_sim]
    cands.sort(key=lambda p: cosine(vec, p["vec"]), reverse=True)
    if not diversify:
        return cands[:k]
    # Greedy diverse top-k: skip a candidate that's near-identical to one already chosen,
    # so version-variants don't crowd out genuinely different takes.
    out = []
    for p in cands:
        if all(cosine(p["vec"], q["vec"]) < dup_thresh for q in out):
            out.append(p)
            if len(out) >= k:
                break
    return out


def _describe(p):
    if p["kind"] == "drum":
        roles = sorted({_drum_role(n["pitch"]) for n in p["notes"]})
        return (f"{p['project']} [{p['track']}] drum · {p['n_notes']} hits · "
                + " ".join(_ROLE_NAMES[r] for r in roles))
    pcs = sorted({n["pitch"] % 12 for n in p["notes"]})
    names = " ".join(PITCH_NAMES[x] for x in pcs)
    return f"{p['project']} [{p['track']}] harmonic · {p['n_notes']} notes · pitches: {names}"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--demo-only", action="store_true", help="load existing index, skip rebuild")
    args = ap.parse_args(argv)

    if args.demo_only:
        with open(INDEX_PATH) as fh:
            phrases = json.load(fh)["phrases"]
        print(f"Loaded {len(phrases)} phrases from {INDEX_PATH}")
    else:
        print("Building phrase index from local corpus...")
        phrases = build(args.limit)
        harm = sum(1 for p in phrases if p["kind"] == "harmonic")
        print(f"\nIndexed {len(phrases)} phrases ({harm} harmonic, {len(phrases)-harm} drum)")
        print(f"  → {INDEX_PATH}")

    if (args.demo or args.demo_only) and phrases:
        # Melody demo — exclude near-duplicates of the query (max_sim) for genuinely different takes.
        harm_ph = sorted([p for p in phrases if p["kind"] == "harmonic"], key=lambda p: p["n_notes"])
        if harm_ph:
            q = harm_ph[len(harm_ph) // 2]
            print("\n=== MELODY: phrases like this (different takes) ===")
            print("QUERY:", _describe(q))
            for rank, hit in enumerate(search(phrases, q["vec"], k=5, kind="harmonic", max_sim=0.97), 1):
                print(f"  {rank}. sim={cosine(q['vec'], hit['vec']):.3f}  {_describe(hit)}")

        # Drum demo — groove-fingerprint retrieval.
        drum_ph = sorted([p for p in phrases if p["kind"] == "drum"], key=lambda p: p["n_notes"])
        if drum_ph:
            q = drum_ph[len(drum_ph) // 2]
            print("\n=== DRUMS: grooves like this (different takes) ===")
            print("QUERY:", _describe(q))
            for rank, hit in enumerate(search(phrases, q["vec"], k=5, kind="drum", max_sim=0.97), 1):
                print(f"  {rank}. sim={cosine(q['vec'], hit['vec']):.3f}  {_describe(hit)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
