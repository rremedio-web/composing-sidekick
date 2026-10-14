#!/usr/bin/env python
"""Producer Pal 2.0 upgrade probe — a one-sitting checklist tool.

Producer Pal was just upgraded from 1.x to 2.0. This probes it for behavior
drifts that matter to THIS codebase (mcp_server/live_write.py, core.py,
moment.py) and prints a PASS/FAIL/INFO verdict table. Read-mostly: writes
ONLY to the name-locked tracks the rest of the codebase already trusts —
SIDEKICK JAM (live_write.write_answer_to_jam_track) and SIDEKICK BOOTH
(live_write.stage_named_clips) — plus one scratch MIDI track this tool
creates and deletes itself for the take-lane probe. Every clip this tool
writes to SIDEKICK JAM is deleted again before it exits (live_write.
delete_jam_clip); SIDEKICK BOOTH clips are left in place, same as every
other Booth writer (append-only by policy — clearing it is a manual act).

Run in a supervised sitting, with Live + Producer Pal 2.0 up (probe 5 plays
~1s of audio; nothing else makes sound):

Usage:
    .venv/bin/python tools/probe_pp2.py
    .venv/bin/python tools/probe_pp2.py --skip-writes
    .venv/bin/python tools/probe_pp2.py --json tools/ideas/pp2_probe.json
    .venv/bin/python tools/probe_pp2.py --probe-slot 2/0

--skip-writes runs only the read-only probes (read_set smoke, moment read,
read-format). --probe-slot track/scene feeds the read-format probe (#7) —
point it at a clip that already has a probability token and a velocity range
on some of its notes.

Exit 0 if no probe FAILed (INFOs are fine, they're drift notes for a human to
read); exit 1 otherwise.
"""
import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

# Every import that touches Live/Producer Pal (live_write, moment, ears, core,
# the mcp SDK) is deferred into the probe functions below — NOT imported here
# at module scope — so `--help` works with no Live running and no `mcp`
# package installed. This mirrors tools/check_ears.py and tools/session.py.

NAME_READ_SET = "1 read_set smoke"
NAME_MOMENT = "2 moment read"
NAME_DEDUP = "3 dedup/truncation"
NAME_DEDUP_CLEAN = "3b clean 4-note clip"
NAME_BOOTH = "4 booth stager"
NAME_PLAYBACK = "5 playback"
NAME_TAKELANE = "6 take-lane"
NAME_READFORMAT = "7 read-format"
NAME_LOCATOR = "8 locator + arrangement playback"
NAME_INVARIANT = "text notation invariant"

RESULTS = []            # [{"probe": str, "status": "PASS"|"FAIL"|"INFO", "detail": str}]
RAW_TEXT_SAMPLES = []   # note-bearing read texts collected for the notation-invariant check


def record(probe, status, detail):
    print("[%-4s] %-32s %s" % (status, probe, detail))
    RESULTS.append({"probe": probe, "status": status, "detail": detail})


def _find_live_write_verification_error(exc):
    import live_write
    if isinstance(exc, live_write.LiveWriteVerificationError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            found = _find_live_write_verification_error(sub)
            if found is not None:
                return found
    return None


def run_probe(name, fn, *args, **kwargs):
    """Call one probe, catching anything it didn't already turn into its own
    PASS/FAIL/INFO — a probe blowing up must never stop the rest of the run."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        lwe = _find_live_write_verification_error(exc)
        if lwe is not None:
            record(name, "FAIL", "LiveWriteVerificationError: %s" % lwe)
            return None
        detail = "%s: %s" % (type(exc).__name__, exc)
        if isinstance(exc, BaseExceptionGroup):
            subs = ["%s: %s" % (type(s).__name__, s) for s in exc.exceptions]
            detail += " (sub: %s)" % "; ".join(subs)
        record(name, "FAIL", "unhandled %s" % detail)
        return None


async def _raw_call(tool, args=None):
    """One raw Producer Pal call not wrapped by live_write.

    Uses the shared operation-scoped session adapter (pp_session) — same
    connect → initialize → ppal-connect order as live_write.*."""
    import pp_session
    async with pp_session.operation_session(pp_session.PP_URL) as s:
        return await s.call(tool, args or {})


def _extract_slot_from_verification_error(exc):
    """LiveWriteVerificationError.failed_context ends in 'TRACK trackIndex/scene'."""
    m = re.search(r"(\d+/\d+)$", exc.failed_context or "")
    return m.group(1) if m else None


def _looks_like_bar_beat_text(sample):
    if not sample:
        return False
    if re.search(r'notes\s*:\s*\[', sample):
        return False           # JSON-array notes — the thing we're pinned against
    return "|" in sample


# ---- 1: read_set smoke (read-only) -----------------------------------------

def probe_read_set_smoke():
    name = NAME_READ_SET
    import asyncio
    import live_write
    import moment

    overview = asyncio.run(live_write.read_set())
    tempo = moment.parse_tempo(overview)
    if tempo is None:
        record(name, "FAIL", "tempo unparseable from ppal-read-live-set overview")
        return
    n_tracks = live_write._regular_track_count(overview)
    if n_tracks <= 0:
        record(name, "FAIL", "regular track count is %r (expected > 0)" % n_tracks)
        return
    try:
        n_scenes = live_write._require_scene_count(overview)
    except live_write.LiveWriteVerificationError as exc:
        record(name, "FAIL", "sceneCount unreadable (_require_scene_count raised): %s" % exc)
        return
    record(name, "PASS", "tempo=%g, regular tracks=%d, scenes=%d" % (tempo, n_tracks, n_scenes))


# ---- 2: moment read (read-only) --------------------------------------------

def probe_moment_read():
    name = NAME_MOMENT
    import moment
    import ears

    snap = moment.get_moment()
    if snap.get("ears") == "live":
        record(name, "PASS", ears.status_line(snap))
    else:
        reason = str(snap.get("reason", ""))
        hint = ""
        if "Producer Pal tool error" in reason or "isError" in reason:
            hint = (" — looks like an empty-slot isError from Producer Pal; if this is a "
                    "Set with an empty row-0 slot in the first 12 tracks, read_moment_live "
                    "needs the empty-slot tolerance described in docs/EARS_PROMOTION.md §2")
        record(name, "INFO", "moment came back blind: %s%s" % (reason, hint))
    print("  operator: also re-run this checklist on a Set where some of the first 12 "
          "tracks have no row-0 clip (the empty-slot isError case, docs/EARS_PROMOTION.md "
          "§2) — confirm Producer Pal 2.0 still answers those with a normal response.")


# ---- 3 / 3b: dedup/truncation probe (write) --------------------------------

def probe_dedup_truncation():
    name = NAME_DEDUP
    import asyncio
    import live_write

    probe_notes = [
        {"pitch": 48, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 80},      # C3
        {"pitch": 48, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 80},      # exact duplicate
        {"pitch": 50, "start_beats": 2.0, "duration_beats": 2.0, "velocity": 90},      # D3, long
        {"pitch": 50, "start_beats": 3.0, "duration_beats": 1.0, "velocity": 90},      # D3, starts inside it
        {"pitch": 52, "start_beats": 6.0, "duration_beats": 0.5638, "velocity": 100},  # fractional duration
        {"pitch": 53, "start_beats": 8.0, "duration_beats": 1.0, "velocity": 1},       # v1
    ]
    slot = None
    try:
        res = asyncio.run(live_write.write_answer_to_jam_track(
            probe_notes, clip_name="pp2 probe", tempo=120))
        slot = res.get("slot")
        record(name, "PASS", "wire format unchanged (%s notes verified at %s)"
               % (res.get("verified_noteCount"), slot))
    except BaseException as exc:
        lwe = _find_live_write_verification_error(exc)
        if lwe is not None:
            slot = _extract_slot_from_verification_error(lwe)
            record(name, "INFO",
                   "2.0 dedups/truncates — normalization needed at _notes_to_pp_string (%s)" % lwe)
        else:
            raise
    finally:
        if slot:
            try:
                asyncio.run(live_write.delete_jam_clip(slot))
            except Exception as exc:
                print("  cleanup warning: could not delete probe clip at %s (%s)" % (slot, exc))


def probe_clean_jam_clip():
    name = NAME_DEDUP_CLEAN
    import asyncio
    import live_write

    clean_notes = [
        {"pitch": 60, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 90},
        {"pitch": 62, "start_beats": 1.0, "duration_beats": 1.0, "velocity": 90},
        {"pitch": 64, "start_beats": 2.0, "duration_beats": 1.0, "velocity": 90},
        {"pitch": 65, "start_beats": 3.0, "duration_beats": 1.0, "velocity": 90},
    ]
    slot = None
    try:
        res = asyncio.run(live_write.write_answer_to_jam_track(
            clean_notes, clip_name="pp2 probe clean", tempo=120))
        slot = res.get("slot")
        record(name, "PASS", "clean 4-note clip verified green at %s" % slot)
    except live_write.LiveWriteVerificationError as exc:
        slot = _extract_slot_from_verification_error(exc)
        record(name, "FAIL", "clean 4-note clip did NOT verify green: %s" % exc)
    finally:
        if slot:
            try:
                asyncio.run(live_write.delete_jam_clip(slot))
            except Exception as exc:
                print("  cleanup warning: could not delete probe clip at %s (%s)" % (slot, exc))


# ---- 4: booth stager probe (write) -----------------------------------------

def probe_booth_stager():
    name = NAME_BOOTH
    import asyncio
    import live_write

    clips = [
        {"name": "PP2 PROBE A", "tempo": 120, "notes": [
            {"pitch": 60, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 90},
            {"pitch": 64, "start_beats": 1.0, "duration_beats": 1.0, "velocity": 90},
        ]},
        {"name": "PP2 PROBE B", "tempo": 120, "notes": [
            {"pitch": 62, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 90},
            {"pitch": 65, "start_beats": 1.0, "duration_beats": 1.0, "velocity": 90},
        ]},
    ]
    try:
        res = asyncio.run(live_write.stage_named_clips(clips))
    except live_write.LiveWriteVerificationError as exc:
        record(name, "FAIL", "stage_named_clips verification failed: %s" % exc)
        return None

    placed = res.get("placed") or []
    if len(placed) != len(clips) or not all(p.get("ok") for p in placed):
        record(name, "FAIL", "stage_named_clips placed %d/%d clips: %r"
               % (len(placed), len(clips), placed))
        return None
    record(name, "PASS", "staged %d clips on '%s' starting at slot %s (track_created=%s) "
                        "— left in place, Booth is append-only by policy"
           % (len(placed), live_write.BOOTH_TRACK_NAME, placed[0]["slot"], res.get("track_created")))
    return placed[0]["slot"]


# ---- 5: playback probe (write-adjacent: needs probe 4's slot) --------------

def probe_playback(booth_slot):
    name = NAME_PLAYBACK
    if not booth_slot:
        record(name, "INFO", "no clip slot from the booth stager probe — nothing to play")
        return
    import asyncio

    asyncio.run(_raw_call("ppal-playback", {"action": "play-session-clips", "slots": booth_slot}))
    time.sleep(1)
    asyncio.run(_raw_call("ppal-playback", {"action": "stop-all-session-clips"}))
    record(name, "PASS", "play-session-clips(%s) + stop-all-session-clips both returned "
                        "without error" % booth_slot)


# ---- 6: take-lane probe (write, scratch) -----------------------------------

def probe_take_lane():
    name = NAME_TAKELANE
    import asyncio
    import live_write

    overview = asyncio.run(_raw_call("ppal-read-live-set", {"include": ["tracks"]}))
    idx = live_write._regular_track_count(overview)
    asyncio.run(_raw_call("ppal-create-track", {"count": 1, "type": "midi", "name": "PP2 SCRATCH"}))
    raw_track = asyncio.run(_raw_call("ppal-read-track", {"trackIndex": idx, "include": []}))
    tid = live_write._find_str(raw_track, "id")
    try:
        asyncio.run(_raw_call("ppal-create-clip", {
            "trackIndex": idx,
            "arrangementStart": "1|1",
            "notes": "v90 n/4 C3 1|1",
            "takeLane": "new",
            "takeLaneName": "SDK PROBE",
        }))
        readback = asyncio.run(_raw_call(
            "ppal-read-track", {"trackIndex": idx, "include": ["arrangement-clips"]}))
    finally:
        if tid:
            asyncio.run(_raw_call("ppal-delete", {"type": "track", "ids": tid}))
        else:
            print("  cleanup warning: scratch track 'PP2 SCRATCH' has no readable id — "
                  "remove it by hand in Live")

    RAW_TEXT_SAMPLES.append(readback)
    has_clip = "SDK PROBE" in readback
    has_take_lane = re.search(r"takeLane", readback, re.IGNORECASE) is not None
    if has_take_lane and has_clip:
        record(name, "PASS", "take lane round-trips (takeLane info + clip name both present "
                            "in ppal-read-track's arrangement-clips output)")
    elif has_clip:
        record(name, "INFO", "clip round-tripped but no takeLane field seen in read-track "
                            "output — raw: %r" % readback[:300])
    else:
        record(name, "INFO", "scratch clip not found in read-back — take lane may not have "
                            "round-tripped — raw: %r" % readback[:300])


# ---- 7: read-format probe (read-only, needs --probe-slot) ------------------

def probe_read_format(probe_slot):
    name = NAME_READFORMAT
    if not probe_slot:
        record(name, "INFO", "provide --probe-slot to run")
        print("  instruction: add probability + a velocity range to any clip in Live first, "
              "then re-run with --probe-slot track/scene")
        return
    try:
        track_s, scene_s = probe_slot.split("/")
        track_index, scene = int(track_s), int(scene_s)
    except ValueError:
        record(name, "FAIL", "--probe-slot must be 'track/scene' (e.g. '2/0'), got %r" % probe_slot)
        return

    import asyncio
    import core
    import live_write

    clip = asyncio.run(live_write.read_clip(track_index, slot=scene))
    RAW_TEXT_SAMPLES.append(clip.get("raw", ""))
    pp_notes = clip.get("pp_notes") or ""
    try:
        parsed = core.pp_notes_to_notes(pp_notes)
    except ValueError as exc:
        record(name, "FAIL", "%s" % exc)
        return
    record(name, "PASS", "parsed %d notes from %s (p-token tolerant)" % (len(parsed), probe_slot))


# ---- 8: locator + arrangement playback probe (write) -----------------------

def probe_locator_arrangement():
    name = NAME_LOCATOR
    import asyncio
    import live_write

    locator_name = "PP2 PROBE"
    # replace=False: create our one marker only, never touch/replace whatever
    # THE MAP may already have laid down in this Set.
    create_res = asyncio.run(live_write.write_song_locators(
        {"locators": [{"time": "1|1", "name": locator_name}]}, replace=False))
    try:
        if not create_res.get("created"):
            record(name, "INFO", "write_song_locators created 0 locators: %r" % create_res)
            return
        asyncio.run(_raw_call("ppal-playback",
                              {"action": "play-arrangement", "startLocator": locator_name}))
        time.sleep(1)
        asyncio.run(_raw_call("ppal-playback", {"action": "stop"}))
        record(name, "PASS", "locator created; play-arrangement by name + stop both "
                            "returned without error")
    finally:
        try:
            asyncio.run(_raw_call("ppal-update-live-set",
                                  {"locatorOperation": "delete", "locatorName": locator_name}))
        except Exception as exc:
            print("  cleanup warning: could not delete locator %r (%s)" % (locator_name, exc))


# ---- final: text notation invariant ----------------------------------------

def probe_text_notation_invariant():
    name = NAME_INVARIANT
    if not RAW_TEXT_SAMPLES:
        record(name, "INFO", "no note-bearing reads captured this run — pass --probe-slot "
                            "or drop --skip-writes to exercise one before trusting this line")
        return
    bad = [s for s in RAW_TEXT_SAMPLES if not _looks_like_bar_beat_text(s)]
    if not bad:
        record(name, "INFO", "holds: %d read(s) this run used bar|beat text, no JSON note "
                            "arrays seen (repo pins text notation until the midi-json "
                            "migration)" % len(RAW_TEXT_SAMPLES))
    else:
        record(name, "INFO", "%d/%d read(s) this run did NOT look like bar|beat text — check "
                            "Producer Pal 2.0's note serialization before trusting any "
                            "pp_notes_to_notes parse" % (len(bad), len(RAW_TEXT_SAMPLES)))


# ---- verdict table + main ---------------------------------------------------

def print_verdict_table(results):
    print()
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)
    width = max((len(r["probe"]) for r in results), default=10)
    for r in results:
        print("%-4s %-*s %s" % (r["status"], width, r["probe"], r["detail"]))
    n_pass = sum(1 for r in results if r["status"] == "PASS")
    n_info = sum(1 for r in results if r["status"] == "INFO")
    n_fail = sum(1 for r in results if r["status"] == "FAIL")
    print("-" * 78)
    print("%d PASS, %d INFO, %d FAIL" % (n_pass, n_info, n_fail))
    return n_fail


def main():
    ap = argparse.ArgumentParser(
        description="Producer Pal 2.0 upgrade probe — behavior-drift checklist.")
    ap.add_argument("--skip-writes", action="store_true",
                    help="run only the read-only probes (read_set smoke, moment read, "
                         "read-format)")
    ap.add_argument("--json", metavar="PATH",
                    help="dump the verdict table as JSON to PATH (for archiving)")
    ap.add_argument("--probe-slot", metavar="track/scene",
                    help="clip slot for the read-format probe, e.g. '2/0' — that clip "
                         "should carry a probability token and a velocity range")
    args = ap.parse_args()

    print("Producer Pal 2.0 probe — one-sitting checklist.")
    print("Writes only to SIDEKICK JAM / SIDEKICK BOOTH, plus one scratch MIDI track "
          "this tool creates and deletes for the take-lane probe.")
    if args.skip_writes:
        print("--skip-writes: read-only probes only.")
    print()

    # Read-only first.
    run_probe(NAME_READ_SET, probe_read_set_smoke)
    run_probe(NAME_MOMENT, probe_moment_read)
    run_probe(NAME_READFORMAT, probe_read_format, args.probe_slot)

    # Writes.
    if not args.skip_writes:
        run_probe(NAME_DEDUP, probe_dedup_truncation)
        run_probe(NAME_DEDUP_CLEAN, probe_clean_jam_clip)
        booth_slot = run_probe(NAME_BOOTH, probe_booth_stager)
        run_probe(NAME_PLAYBACK, probe_playback, booth_slot)
        run_probe(NAME_TAKELANE, probe_take_lane)
        run_probe(NAME_LOCATOR, probe_locator_arrangement)
    else:
        for nm in (NAME_DEDUP, NAME_DEDUP_CLEAN, NAME_BOOTH, NAME_PLAYBACK,
                   NAME_TAKELANE, NAME_LOCATOR):
            record(nm, "INFO", "skipped (--skip-writes)")

    run_probe(NAME_INVARIANT, probe_text_notation_invariant)

    n_fail = print_verdict_table(RESULTS)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(RESULTS, fh, indent=2)
        print("wrote verdict JSON: %s" % args.json)
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
