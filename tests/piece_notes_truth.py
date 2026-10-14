"""Independent model of what each NOCTURNEv2 arrangement MIDI clip plays.

Ported from the batch-8 reviewer's probes (.scratch/batch8/review1-probes:
indep.py, als_cmp.py) and deliberately NOT built on mcp_server.clip_playback:
it reads the Live API truth captured in fixtures/pp/2.2.0-rc1/piece-notes
directly (loop/marker properties, start/end time, every note) and maps song
time back to clip time to decide what sounds.

* audible set: notes (not muted) with start in the played brace — loop
  [loop_start, loop_end) when looping (first pass from start_marker), else
  [start_marker, end_marker).
* arrangement events: for each song position from the clip's start_time up to
  its end_time, the clip-content time Live is playing there is
  ``clip_time(p)``; a note sounds at ``p`` iff clip_time(p) is its start.
"""
import json
from pathlib import Path

ROOT = Path(__file__).parent / "fixtures/pp/2.2.0-rc1/piece-notes"
TOL = 1e-4


def load_truth() -> dict:
    """clip id -> {"track_index", "props", "notes"} from the piece-notes capture."""
    out = {}
    for path in sorted(ROOT.glob("notes_clip_t*.json")):
        record = json.loads(path.read_text())
        payload = record["response"]["content"][0]["json"]
        props, notes = {}, []
        for item in payload["results"]:
            op = item["operation"]
            if op["type"] == "getProperty":
                props[op["property"]] = item["result"]
            else:
                notes = json.loads(item["result"])["notes"]
        track_index = int(payload["path"].split()[2])
        out[payload["id"]] = {"track_index": track_index, "props": props, "notes": notes,
                              "file": path.name}
    return out


def _brace(props):
    if props["looping"]:
        return min(props["start_marker"], props["loop_start"]), props["loop_end"]
    return props["start_marker"], props["end_marker"]


def audible_set(clip: dict) -> list:
    """Sorted (clip start rounded to 4 places, pitch) of the notes that play."""
    lo, hi = _brace(clip["props"])
    return sorted((round(n["start_time"], 4), n["pitch"]) for n in clip["notes"]
                  if not n.get("mute") and lo - TOL <= n["start_time"] < hi - TOL)


def _clip_time(props, song_pos):
    """The clip-content time Live plays at song position ``song_pos``."""
    elapsed = song_pos - props["start_time"]
    if not props["looping"]:
        return props["start_marker"] + elapsed
    first = props["loop_end"] - props["start_marker"]
    if elapsed < first - TOL:
        return props["start_marker"] + elapsed
    length = props["loop_end"] - props["loop_start"]
    return props["loop_start"] + ((elapsed - first) % length)


def arrangement_events(clip: dict) -> list:
    """Sorted (song position, pitch) note-ons, rounded to 4 places."""
    props = clip["props"]
    a, e = props["start_time"], props["end_time"]
    lo, hi = _brace(props)
    length = (props["loop_end"] - props["loop_start"]) if props["looping"] else (hi - lo)
    out = []
    for note in clip["notes"]:
        t = note["start_time"]
        if note.get("mute") or not (lo - TOL <= t < hi - TOL):
            continue
        # candidate onsets: every place this clip time could recur
        k_max = int((e - a) / length) + 2 if length > 0 else 1
        for k in range(-1, k_max + 1):
            p = a + (t - props["start_marker"]) + k * length
            if a - TOL <= p < e - TOL and abs(_clip_time(props, p) - t) < TOL:
                out.append((round(p, 4), note["pitch"]))
    return sorted(set(out))
