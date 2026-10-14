"""Harvest arrangement DNA the note pipeline ignores: locators + automation envelopes.

Re-reads the original .als files (paths from store/projects/_index.json) and writes
store/automation.json — locator names/times (his structural vocabulary) and
per-track automation envelopes resolved to parameter names (volume rides, filter
sweeps = his real energy arcs). Read-only over .als; undo = delete the artifact.

Run: python3 tools/harvest_arrangement_dna.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "ingest"))
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
from parse_als import load_als_root, _track_name, is_blocked_project  # noqa: E402
import core  # noqa: E402

OUT = os.path.join(ROOT, "store/automation.json")
TRACK_TAGS = {"MidiTrack", "AudioTrack", "GroupTrack", "ReturnTrack"}
MIN_EVENTS = 4
MAX_EVENTS_KEPT = 400


def _ancestry(el, parents, depth=4):
    names, cur = [], el
    for _ in range(depth):
        cur = parents.get(cur)
        if cur is None:
            break
        names.append(cur.tag)
    return "/".join(reversed(names))


def harvest(path):
    root = load_als_root(path)
    parents = {c: p for p in root.iter() for c in p}

    locators = []
    for loc in root.iter("Locator"):
        t = loc.find("Time")
        n = loc.find("Name")
        if t is not None and t.get("Value") is not None:
            locators.append({"beat": float(t.get("Value")),
                             "name": (n.get("Value") if n is not None else "") or ""})
    locators.sort(key=lambda d: d["beat"])

    tracks = []
    tracks_el = root.find(".//LiveSet/Tracks")
    for track_el in (list(tracks_el) if tracks_el is not None else []):
        if track_el.tag not in TRACK_TAGS:
            continue
        target_names = {}
        for tgt in track_el.iter("AutomationTarget"):
            tid = tgt.get("Id")
            if tid:
                target_names[tid] = _ancestry(tgt, parents)
        envs = []
        for env in track_el.findall(".//AutomationEnvelopes/Envelopes/AutomationEnvelope"):
            pid_el = env.find(".//EnvelopeTarget/PointeeId")
            pid = pid_el.get("Value") if pid_el is not None else None
            events = []
            for ev in env.iter("FloatEvent"):
                try:
                    tm = float(ev.get("Time", 0.0))
                    val = float(ev.get("Value", 0.0))
                except (TypeError, ValueError):
                    continue
                if tm >= 0:                      # drop the -inf initial-value sentinel
                    events.append([round(tm, 3), round(val, 5)])
            if len(events) < MIN_EVENTS:
                continue
            events.sort()
            envs.append({
                "target": target_names.get(pid, f"pointee:{pid}"),
                "n_events": len(events),
                "events": events[:MAX_EVENTS_KEPT],
            })
        if envs:
            tracks.append({"track": _track_name(track_el), "envelopes": envs})

    return locators, tracks


def main():
    index = json.load(open(os.path.join(ROOT, "store/projects/_index.json")))
    index = [e for e in index if not is_blocked_project(e["name"])]
    missing = sum(1 for e in index if not os.path.exists(e["path"]))
    if missing > 0.2 * len(index):
        print(f"ABORT: {missing}/{len(index)} .als files unreachable (external drive "
              "unmounted?). Refusing to overwrite store/automation.json with a gutted "
              "harvest — mount the drive and rerun.")
        return
    out = {"schema_version": 1, "harvested_at": "2026-07-02", "projects": []}
    n_loc = n_env = n_missing = 0
    for i, e in enumerate(index):
        path = e["path"]
        if not os.path.exists(path):
            n_missing += 1
            continue
        try:
            locators, tracks = harvest(path)
        except Exception as exc:  # noqa: BLE001 — skip unreadable sets, keep harvesting
            print(f"  skip {e['name']}: {type(exc).__name__}: {exc}")
            continue
        if locators or tracks:
            out["projects"].append({
                "id": e["id"], "name": e["name"], "tempo": e.get("tempo"),
                "locators": locators, "automation_tracks": tracks,
            })
            n_loc += len(locators)
            n_env += sum(len(t["envelopes"]) for t in tracks)
        if (i + 1) % 25 == 0:
            print(f"  {i + 1}/{len(index)} scanned... ({n_loc} locators, {n_env} envelopes so far)")

    out["totals"] = {"projects_with_dna": len(out["projects"]),
                     "locators": n_loc, "envelopes": n_env, "missing_files": n_missing}
    core._save_json_atomic(OUT, out, indent=2)
    print(f"\nDONE: {len(out['projects'])} projects with arrangement DNA "
          f"({n_loc} locators, {n_env} envelopes; {n_missing} files missing)")
    named = [(l["name"], p["name"]) for p in out["projects"] for l in p["locators"]
             if l["name"] and not l["name"].strip().isdigit()]
    print(f"locators with WORD names: {len(named)}; sample: {named[:12]}")
    print(f"artifact: {os.path.relpath(OUT, ROOT)}")


if __name__ == "__main__":
    main()
