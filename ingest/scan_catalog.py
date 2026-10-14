"""Scan a whole catalog of .als files and summarize the MIDI-vs-audio reality.

Walks one or more root directories, skips Ableton auto-save Backup folders,
parses every Live Set, and writes a manifest plus an aggregate report. This is
how we decide where the symbolic (MIDI) pipeline has data and where the
Music Flamingo "ears" lane is the only way in (audio-only projects).

Usage:
    python3 ingest/scan_catalog.py                 # default roots
    python3 ingest/scan_catalog.py --roots /Volumes/MyDrive ~/Desktop
    python3 ingest/scan_catalog.py --limit 50      # quick sample
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_als import clip_source_of, is_main_clip_source, parse_als  # noqa: E402

# Extra roots (e.g. an external drive) come from SIDEKICK_CATALOG_ROOTS,
# separated by os.pathsep, or from --roots.
DEFAULT_ROOTS = [
    os.path.expanduser("~/Desktop"),
    os.path.expanduser("~/Documents"),
    os.path.expanduser("~/Music"),
    *[r for r in os.environ.get("SIDEKICK_CATALOG_ROOTS", "").split(os.pathsep) if r],
]


def find_als(roots: list[str]) -> list[str]:
    """All .als under the roots, excluding Ableton auto-save Backup folders."""
    found: list[str] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune Backup dirs in-place so we don't descend into auto-saves.
            dirnames[:] = [d for d in dirnames if d.lower() != "backup"]
            for fn in filenames:
                if fn.lower().endswith(".als"):
                    found.append(os.path.join(dirpath, fn))
    return found


def summarize(path: str) -> dict:
    try:
        p = parse_als(path)
        clip_sources = Counter()
        note_sources = Counter()
        main_notes = 0
        for c in p.clips:
            source = clip_source_of(c)
            clip_sources[source] += 1
            note_sources[source] += c.note_count
            if is_main_clip_source(source):
                main_notes += c.note_count
        return {
            "path": path,
            "name": os.path.splitext(os.path.basename(path))[0],
            "tempo": p.tempo,
            "midi_tracks": len(p.midi_track_names),
            "audio_tracks": len(p.audio_track_names),
            "midi_clips": p.clip_count,
            "notes": p.total_notes,
            "main_notes": main_notes,
            "clip_source_counts": dict(sorted(clip_sources.items())),
            "note_source_counts": dict(sorted(note_sources.items())),
            "audio_clips": p.audio_clip_count,
            "notes_skipped": p.notes_skipped,
            "skip_detail": p.skip_detail or {},
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"path": path, "name": os.path.basename(path), "error": str(exc)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scan .als catalog → manifest + report.")
    ap.add_argument("--roots", nargs="*", default=DEFAULT_ROOTS)
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "store", "catalog_manifest.json"))
    ap.add_argument("--limit", type=int, default=0, help="parse at most N (sampling)")
    args = ap.parse_args(argv)

    files = find_als(args.roots)
    if args.limit:
        files = files[:args.limit]
    print(f"Found {len(files)} .als files (Backup folders excluded). Parsing...")

    rows, t0 = [], time.time()
    for i, f in enumerate(files, 1):
        rows.append(summarize(f))
        if i % 50 == 0 or i == len(files):
            print(f"  {i}/{len(files)}  ({time.time()-t0:.0f}s)")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(rows, fh, indent=2)

    ok = [r for r in rows if not r.get("error")]
    failed = [r for r in rows if r.get("error")]
    with_midi = [r for r in ok if r["notes"] > 0]
    audio_only = [r for r in ok if r["notes"] == 0 and r["audio_clips"] > 0]
    empty = [r for r in ok if r["notes"] == 0 and r["audio_clips"] == 0]
    total_notes = sum(r["notes"] for r in ok)
    main_notes = sum(r.get("main_notes", r["notes"]) for r in ok)
    total_skips = sum(r.get("notes_skipped", 0) for r in ok)
    projects_with_skips = [r for r in ok if r.get("notes_skipped", 0) > 0]

    print("\n===== CATALOG REPORT =====")
    print(f"Parsed OK        : {len(ok)}   (failed: {len(failed)})")
    print(f"With MIDI notes  : {len(with_midi)}  ({_pct(len(with_midi), len(ok))})")
    print(f"Audio-only       : {len(audio_only)}  ({_pct(len(audio_only), len(ok))})")
    print(f"Empty/other      : {len(empty)}  ({_pct(len(empty), len(ok))})")
    print(f"Total MIDI notes : {total_notes}")
    print(f"Main-corpus notes: {main_notes}")
    if total_skips > 0:
        print(f"Notes skipped    : {total_skips}")
        print(f"Projects with skips: {len(projects_with_skips)}")
    if projects_with_skips:
        print("\nProjects with skipped notes:")
        for r in sorted(projects_with_skips, key=lambda x: x.get("notes_skipped", 0), reverse=True)[:10]:
            print(f"  {r['name']}: {r.get('notes_skipped', 0)} skipped")
            detail = r.get("skip_detail", {})
            if detail:
                print(f"    {detail}")
    if with_midi:
        top = sorted(with_midi, key=lambda r: r["notes"], reverse=True)[:15]
        print("\nMost MIDI-rich projects:")
        for r in top:
            print(f"  {r['notes']:>6} notes  {r['midi_clips']:>3} clips   {r['name']}")
    if failed:
        print(f"\nFirst parse failures ({len(failed)} total):")
        for r in failed[:5]:
            print(f"  {r['name']}: {r['error']}")
    print(f"\nManifest → {args.out}")
    return 0


def _pct(n, d):
    return f"{(100.0 * n / d):.0f}%" if d else "0%"


if __name__ == "__main__":
    raise SystemExit(main())
