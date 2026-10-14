"""Materialize the parsed MIDI catalog to local JSON (one slow drive scan, then fast).

Re-parses every project with MIDI notes and writes its full normalized form
(tracks, clips, notes, is_drum flags, tempo) to store/projects/<id>.json, plus
an index. After this, all downstream analysis reads local JSON instead of
re-reading the slow external drive on every run.

Usage:
    python3 ingest/materialize.py
    python3 ingest/materialize.py --limit 30
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_als import clip_source_of, is_main_clip_source, parse_als, to_dict  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "store", "catalog_manifest.json")
OUT_DIR = os.path.join(ROOT, "store", "projects")


def project_id(path: str) -> str:
    """Stable, collision-free id: basename + short hash of the full path."""
    base = os.path.splitext(os.path.basename(path))[0]
    safe = "".join(ch if ch.isalnum() else "_" for ch in base)[:40]
    h = hashlib.md5(path.encode("utf-8")).hexdigest()[:8]
    return f"{safe}__{h}"


def summarize_notes(data: dict) -> dict:
    """Raw and main-corpus note counts for one materialized project."""
    clip_sources = Counter()
    note_sources = Counter()
    raw_harm = raw_drum = main_harm = main_drum = 0

    for c in data["clips"]:
        source = clip_source_of(c)
        n_notes = c["note_count"]
        clip_sources[source] += 1
        note_sources[source] += n_notes
        if c["is_drum"]:
            raw_drum += n_notes
        else:
            raw_harm += n_notes
        if is_main_clip_source(source):
            if c["is_drum"]:
                main_drum += n_notes
            else:
                main_harm += n_notes

    return {
        "harmonic_notes": main_harm,
        "drum_notes": main_drum,
        "raw_harmonic_notes": raw_harm,
        "raw_drum_notes": raw_drum,
        "take_lane_notes": note_sources.get("take_lane", 0),
        "clip_source_counts": dict(sorted(clip_sources.items())),
        "note_source_counts": dict(sorted(note_sources.items())),
        "notes_skipped": data.get("notes_skipped", 0),
        "skip_detail": data.get("skip_detail", {}),
    }


def _atomic_write_json(path: str, payload) -> None:
    """Write-temp-then-replace (review finding 6, 2026-08-16): the materialized
    corpus is regenerable, but a torn file would silently poison the next
    analyze build that reads it."""
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(args.manifest) as fh:
        manifest = json.load(fh)
    projects = [r for r in manifest if not r.get("error") and r.get("notes", 0) > 0]
    if args.limit:
        projects = projects[:args.limit]
    print(f"Materializing {len(projects)} MIDI projects → {OUT_DIR}")

    index, t0, ok = [], time.time(), 0
    corpus_notes = Counter()
    corpus_clips = Counter()
    raw_total = main_total = 0
    for i, r in enumerate(projects, 1):
        try:
            data = to_dict(parse_als(r["path"]))
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {r['name']}: {exc}")
            continue
        pid = project_id(r["path"])
        _atomic_write_json(os.path.join(OUT_DIR, f"{pid}.json"), data)
        counts = summarize_notes(data)
        for source, n in counts["note_source_counts"].items():
            corpus_notes[source] += n
        for source, n in counts["clip_source_counts"].items():
            corpus_clips[source] += n
        raw_total += counts["raw_harmonic_notes"] + counts["raw_drum_notes"]
        main_total += counts["harmonic_notes"] + counts["drum_notes"]
        index.append({"id": pid, "name": data["path"].split("/")[-1], "path": data["path"],
                      "tempo": data["tempo"], **counts})
        ok += 1
        if i % 40 == 0 or i == len(projects):
            print(f"  {i}/{len(projects)}  ({time.time()-t0:.0f}s)")

    _atomic_write_json(os.path.join(OUT_DIR, "_index.json"), index)
    print(f"\nMaterialized {ok} projects → {OUT_DIR}/")
    print(f"Index → {OUT_DIR}/_index.json")
    print(f"Raw notes: {raw_total:,}  Main-corpus notes: {main_total:,}")
    print(f"Note sources: {dict(sorted(corpus_notes.items()))}")
    print(f"Clip sources: {dict(sorted(corpus_clips.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
