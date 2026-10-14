"""Read structural evidence from Ableton Live Sets without contacting Live.

Element paths follow real Sets (Live 8.2.1, 11.3.x, 12.x): parameters are
``<P><ArrangerAutomation><Events><FloatEvent|BoolEvent|EnumEvent/>`` in Live 8
and ``<P><Manual/>`` from Live 9 on; tempo sits on the master track's mixer;
scene names are ``SceneNames/Scene[@Value]`` (Live 8) or
``Scenes/Scene/Name[@Value]`` (Live 10+). Sidechain sources are track ids,
resolved only after every track and return is known — names repeat.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    # `python tools/live_set_reader.py attach|compare …` imports tools.audio_ref.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Compressor2 stores an "inf" ratio as FLT_MAX.
_INF_SENTINEL = 1e30
_WARP_MODES = {0: "beats", 1: "tones", 2: "texture", 3: "repitch",
               4: "complex", 5: "rex", 6: "complex_pro"}
_PLAYABLE_TRACKS = {"MidiTrack": "midi", "AudioTrack": "audio", "GroupTrack": "group"}
# (output key, XML parameter) per device type; threshold keys are linear in the
# file and reported in dB, makeup (Compressor2 Gain) is already dB as stored.
_DEVICE_PARAMS = {
    "Compressor2": (("threshold_db", "Threshold"), ("ratio", "Ratio"), ("attack", "Attack"),
                    ("release", "Release"), ("makeup_db", "Gain")),
    "Gate": (("threshold_db", "Threshold"), ("attack", "Attack"), ("hold", "Hold"),
             ("release", "Release"), ("floor_db", "Gain"), ("flip", "Flip")),
    "Erosion": (("mode", "Mode"), ("frequency", "Freq"), ("amplitude", "Amplitude")),
    "BeatRepeat": (("chance", "Chance"), ("interval", "Interval"), ("grid", "Grid"),
                   ("gate", "Gate")),
    "AutoFilter": (("filter_type", "FilterType"), ("cutoff", "Cutoff"),
                   ("resonance", "Resonance")),
    "Reverb": (("decay", "DecayTime"), ("size", "RoomSize"), ("dry_wet", "MixDirect")),
}
_PLUGIN_NAME_PATHS = ("PluginDesc/VstPluginInfo/PlugName", "PluginDesc/Vst3PluginInfo/Name",
                      "PluginDesc/AuPluginInfo/Name")


def _attr(node, path):
    found = node.find(path) if node is not None else None
    return None if found is None else found.get("Value")


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool(value):
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


def _param_raw(node):
    """A parameter's stored value: ``Manual`` (Live 9+) or the initial Live 8
    automation event (the earliest ``Time``, i.e. -63072000)."""
    if node is None:
        return None
    manual = node.find("Manual")
    if manual is not None:
        return manual.get("Value")
    events = [e for e in node.findall("ArrangerAutomation/Events/*") if e.get("Value") is not None]
    if events:
        return min(events, key=lambda e: _number(e.get("Time")) or 0.0).get("Value")
    return node.get("Value")


def _param(node):
    raw = _param_raw(node)
    flag = _bool(raw)
    return flag if flag is not None else _number(raw)


def _db(linear, places=2):
    """Linear gain → dB; Live's -inf floor (≤ 0.000316) → None, never Infinity."""
    if not isinstance(linear, (int, float)) or linear <= 0.00031623:
        return None
    return round(20 * math.log10(linear), places)


def _round(value, places=4):
    return round(value, places) if isinstance(value, float) else value


def _name(node):
    return (_attr(node, "Name/UserName") or _attr(node, "Name/EffectiveName") or "")


def _time_signature(live):
    for path in ("MainTrack/DeviceChain/Mixer/TimeSignature",
                 "MasterTrack/DeviceChain/Mixer/TimeSignature",
                 "MasterTrack/MasterChain/Mixer/TimeSignature"):
        value = _number(_param_raw(live.find(path)))
        if value is not None:
            code = int(value)
            return {"numerator": code % 99 + 1, "denominator": 2 ** (code // 99)}
    return {"numerator": 4, "denominator": 4}


def _tempo(live):
    for path in ("MainTrack/DeviceChain/Mixer/Tempo", "MasterTrack/DeviceChain/Mixer/Tempo",
                 "MasterTrack/MasterChain/Mixer/Tempo"):
        value = _number(_param_raw(live.find(path)))
        if value is not None:
            return value
    return None


def _scenes(live):
    if live.find("Scenes") is not None:
        return [_attr(s, "Name") or "" for s in live.findall("Scenes/Scene")]
    return [s.get("Value") or _attr(s, "Name") or "" for s in live.findall("SceneNames/Scene")]


def _device_entry(device, rack_path):
    entry = {"type": device.tag, "name": _attr(device, "UserName") or "",
             "on": _param(device.find("On")), "rack_path": list(rack_path), "params": {}}
    plugin = next((v for v in (_attr(device, p) for p in _PLUGIN_NAME_PATHS) if v), None)
    if plugin:
        entry["plugin"] = plugin
        maker = _attr(device, "PluginDesc/AuPluginInfo/Manufacturer")
        if maker:
            entry["manufacturer"] = maker
    for key, tag in _DEVICE_PARAMS.get(device.tag, ()):
        value = _param(device.find(tag))
        if value is None:
            continue
        if key == "threshold_db" and isinstance(value, float):
            value = _db(value)
        elif key == "ratio" and isinstance(value, float) and value >= _INF_SENTINEL:
            value = "inf"
        entry["params"][key] = _round(value)
    return entry


def _walk_devices(devices_node, rack_path=()):
    """Yield (device element, device entry) for a Devices list, descending into
    rack branches (``Branches``/``ReturnBranches`` → ``DeviceChain/*/Devices``)."""
    if devices_node is None:
        return
    for device in devices_node:
        yield device, _device_entry(device, rack_path)
        branches = device.findall("Branches/*") + device.findall("ReturnBranches/*")
        if branches:
            label = device.tag + (":" + (_attr(device, "UserName") or "") if _attr(device, "UserName") else "")
            for branch in branches:
                for chain in branch.findall("DeviceChain/*"):
                    yield from _walk_devices(chain.find("Devices"), (*rack_path, label))


def _clip(clip, bar_beats, tempo=None):
    start = _number(_attr(clip, "Loop/LoopStart")) or 0.0
    end = _number(_attr(clip, "Loop/LoopEnd"))
    gain = _number(_attr(clip, "SampleVolume"))
    warp = _number(_attr(clip, "WarpMode"))
    warp_id = int(warp) if warp is not None else None
    # Unwarped audio stores its loop points in SECONDS, not beats. Its length
    # in bars is only an estimate at the Set tempo, never a measured value.
    warped = _bool(_attr(clip, "IsWarped"))
    unwarped = clip.tag == "AudioClip" and warped is False
    length = (end - start) if end is not None else None
    entry = {"clip_name": _attr(clip, "Name") or "",
             "loop_bars": (round(length / bar_beats, 4)
                           if length is not None and not unwarped else None),
             "loop_on": _bool(_attr(clip, "Loop/LoopOn")),
             "is_warped": (not unwarped) if clip.tag == "AudioClip" else None,
             "warp_mode": (_WARP_MODES.get(warp_id) if warp_id is not None and not unwarped
                           else None),
             "warp_mode_id": warp_id,
             "gain": gain, "gain_db": _db(gain)}
    if unwarped:
        entry["loop_seconds"] = round(length, 4) if length is not None else None
        entry["loop_bars_estimate"] = (
            round(length * tempo / 60.0 / bar_beats, 4)
            if length is not None and tempo else None)
    return entry


def _mixer(track, returns_order):
    mixer = track.find("DeviceChain/Mixer")
    volume = _param(mixer.find("Volume")) if mixer is not None else None
    sends = []
    if mixer is not None:
        for i, holder in enumerate(mixer.findall("Sends/TrackSendHolder")):
            value = _param(holder.find("Send"))
            ret_id, ret_name = returns_order[i] if i < len(returns_order) else (None, None)
            sends.append({"return_id": ret_id, "return": ret_name, "value": _round(value),
                          "db": _db(value)})
    return {"volume": _round(volume), "volume_db": _db(volume),
            "on": _param(mixer.find("Speaker")) if mixer is not None else None,
            "sends": sends}


def _parse(root):
    live = root.find("LiveSet")
    if live is None:
        raise ValueError("XML does not contain a LiveSet")
    signature = _time_signature(live)
    bar_beats = signature["numerator"] * 4 / signature["denominator"]
    set_tempo = _tempo(live)
    scenes = _scenes(live)
    tracks_node = live.find("Tracks")
    elements = list(tracks_node) if tracks_node is not None else []
    names = {tr.get("Id"): _name(tr) for tr in elements}
    returns_order = [(tr.get("Id"), names[tr.get("Id")]) for tr in elements if tr.tag == "ReturnTrack"]
    tracks, returns, pending = [], [], []
    for tr in elements:
        if tr.tag not in _PLAYABLE_TRACKS and tr.tag != "ReturnTrack":
            continue
        tid = tr.get("Id")
        group_raw = _attr(tr, "TrackGroupId")
        group_id = group_raw if group_raw not in (None, "-1") else None
        entry = {"id": tid, "name": names[tid],
                 "kind": _PLAYABLE_TRACKS.get(tr.tag, "return"),
                 "group_id": group_id, "group": names.get(group_id) if group_id else None,
                 **_mixer(tr, returns_order), "devices": []}
        for device, info in _walk_devices(tr.find("DeviceChain/DeviceChain/Devices")):
            entry["devices"].append(info)
            side = device.find("SideChain")
            target = _attr(side, "RoutedInput/Routable/Target") or ""
            match = re.match(r"AudioIn/Track\.(\d+)(?:/(.*))?$", target)
            if side is not None and _param(side.find("OnOff")) is True and match:
                pending.append({"target_track_id": tid, "device": device.tag,
                                "device_on": info.get("on"),
                                "in_rack": bool(info["rack_path"]),
                                "source_track_id": match.group(1),
                                "source_tap": match.group(2)})
        if tr.tag == "ReturnTrack":
            returns.append(entry)
            continue
        session = []
        for index, slot in enumerate(tr.findall("DeviceChain/MainSequencer/ClipSlotList/ClipSlot")):
            clip = slot.find("ClipSlot/Value/*")
            if clip is not None and clip.tag in ("AudioClip", "MidiClip"):
                session.append({"scene_index": index, **_clip(clip, bar_beats, set_tempo)})
        arrangement = []
        for clip in (tr.findall("DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events/*")
                     + tr.findall("DeviceChain/MainSequencer/ClipTimeable/ArrangerAutomation/Events/*")):
            start, end = _number(_attr(clip, "CurrentStart")), _number(_attr(clip, "CurrentEnd"))
            if start is not None and end is not None:
                arrangement.append({"start_bar": round(start / bar_beats + 1, 4),
                                    "end_bar": round(end / bar_beats + 1, 4),
                                    "name": _attr(clip, "Name") or ""})
        entry["session"], entry["arrangement"] = session, arrangement
        tracks.append(entry)
    # Resolve sidechain sources by id now that every track and return is known.
    sidechains = [{"target_track_id": e["target_track_id"], "target_track": names.get(e["target_track_id"]),
                   "device": e["device"], "device_on": e["device_on"], "in_rack": e["in_rack"],
                   "source_track_id": e["source_track_id"],
                   "source_track": names.get(e["source_track_id"]),
                   "source_tap": e["source_tap"]} for e in pending]
    counts = {}
    for t in tracks:
        counts[t["name"]] = counts.get(t["name"], 0) + 1
    matrix, lengths = {}, set()
    for t in tracks:
        if t["kind"] == "group":
            continue
        cells = [None] * len(scenes)
        estimated = []
        for clip in t["session"]:
            value = clip["loop_bars"]
            if value is None and clip.get("loop_bars_estimate") is not None:
                value = clip["loop_bars_estimate"]
                estimated.append(clip["scene_index"])
            if clip["scene_index"] < len(scenes):
                cells[clip["scene_index"]] = value
            if clip["loop_bars"] is not None:
                lengths.add(clip["loop_bars"])
        label = t["name"] if counts[t["name"]] == 1 else f"{t['name']} [{t['id']}]"
        matrix[t["id"]] = {"name": t["name"], "label": label, "loop_bars": cells,
                           "estimated_scenes": estimated}
    locators = []
    for loc in live.findall("Locators/Locators/Locator"):
        time = _number(_attr(loc, "Time"))
        locators.append({"time_beats": time,
                         "bar": round(time / bar_beats + 1, 4) if time is not None else None,
                         "name": _attr(loc, "Name") or ""})
    return {"creator": root.get("Creator"), "tempo": _tempo(live), "time_signature": signature,
            "locators": locators, "scenes": scenes, "tracks": tracks, "returns": returns,
            "sidechains": sidechains, "scene_matrix": matrix,
            "loop_lengths_bars": sorted(lengths),
            "xml_sha256": hashlib.sha256(ET.tostring(root)).hexdigest()}


def _live_set_root(chunk):
    try:
        root = ET.fromstring(chunk)
    except ET.ParseError:
        return None
    return root if root.tag == "Ableton" and root.find("LiveSet") is not None else None


def _xml_documents(data):
    """Every ``<Ableton>`` document with a LiveSet in a Set, XML or Pack blob.
    Packs also embed device presets (``<Ableton>`` without a LiveSet): skipped."""
    if data.startswith(b"\x1f\x8b"):
        try:
            data = gzip.decompress(data)
        except (EOFError, zlib.error, gzip.BadGzipFile, OSError) as exc:
            raise ValueError(f"not a readable gzip Live Set (truncated or corrupt): {exc}") from exc
    docs = []
    for match in re.finditer(rb"<\?xml\b", data):
        end = data.find(b"</Ableton>", match.start())
        if end >= 0:
            root = _live_set_root(data[match.start():end + len(b"</Ableton>")])
            if root is not None:
                docs.append(root)
    if not docs:
        root = _live_set_root(data)
        if root is not None:
            docs.append(root)
    if not docs:
        raise ValueError("no Ableton Live Set XML found")
    return docs


def read_live_set(source):
    """Return one parsed mapping per Ableton Live Set in a Set, XML or Pack."""
    if isinstance(source, (str, Path)):
        data = Path(source).read_bytes()
    elif isinstance(source, bytes):
        data = source
    else:
        raise TypeError("source must be a path or bytes")
    return [_parse(root) for root in _xml_documents(data)]


def _print_matrix(data):
    scenes = data["scenes"]
    sig = data.get("time_signature") or {}
    print(f"{data.get('creator') or 'Live Set'} | tempo: {data['tempo']} BPM | "
          f"{sig.get('numerator')}/{sig.get('denominator')} | scenes: {len(scenes)} | "
          f"loops (bars): {', '.join(f'{v:g}' for v in data['loop_lengths_bars'])}")
    print(f"{'track':24}" + "".join(f"{(s or '-')[:8]:>9}" for s in scenes))
    for row in data["scene_matrix"].values():
        print(f"{row['label'][:24]:24}"
              + "".join(f"{((('~' if i in row.get('estimated_scenes', []) else '') + f'{v:g}b')) if v is not None else '.':>9}"
                        for i, v in enumerate(row["loop_bars"])))
    for edge in data["sidechains"]:
        print(f"sidechain: {edge['target_track']} [{edge['target_track_id']}] {edge['device']}"
              f" <- {edge['source_track']} [{edge['source_track_id']}]")


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


_OWNERSHIP_HINT = ("set it first: python tools/audio_ref.py --note-ref {rid} --ownership other "
                   "(or tag_audio_ref('{rid}', ownership='other'))")


def _sidecar_path(card, audio_refs_root):
    """set_analysis.json beside the card: its out_dir, else <audio_refs>/<ref_id>."""
    from tools.audio_ref import normalize_ref_id

    out_raw = card.get("out_dir")
    if out_raw:
        return Path(out_raw) / "set_analysis.json"
    if not card.get("ref_id"):
        return None
    return Path(audio_refs_root) / normalize_ref_id(str(card["ref_id"])) / "set_analysis.json"


def attach_set_evidence(ref_id, set_file, *, store_dir=None, index_path=None,
                        force=False, same_version_confirmed_by_owner=False):
    """Attach measured Set structure to an other-artist audio reference.

    Ownership must already be ``other`` on the card — set by the owner, never
    inferred here. Nothing is created on disk unless the Set parses and the
    sidecar is written (atomically)."""
    from tools.audio_ref import load_index, normalize_ownership, normalize_ref_id, write_json_atomic

    rid = normalize_ref_id(ref_id)
    store = Path(store_dir) if store_dir else Path(__file__).resolve().parents[1] / "store"
    audio_refs = store / "audio_refs"
    index = load_index(Path(index_path) if index_path else store / "audio_ref_index.json")
    card = (index.get("refs") or {}).get(rid)
    if not card:
        raise KeyError(f"unknown ref_id: {rid}")
    owner = normalize_ownership(card.get("ownership"))
    if owner == "his":
        raise ValueError(f"ref {rid} has ownership 'his'; Set evidence is for another artist's Set only")
    if owner != "other":
        raise ValueError(f"ref {rid} has ownership {owner!r}; Set evidence needs ownership 'other' — "
                         + _OWNERSHIP_HINT.format(rid=rid))
    if card.get("out_dir"):
        out = Path(card["out_dir"]).resolve()
        try:
            out.relative_to(audio_refs.resolve())
        except ValueError as exc:
            raise ValueError("ref_id out_dir is outside --store/audio_refs") from exc
        if not out.is_dir():
            raise ValueError(f"ref {rid} out_dir missing on disk: {out}")
    target = _sidecar_path(card | {"ref_id": rid}, audio_refs)
    set_path = Path(set_file)
    container = set_path.read_bytes()
    parsed = read_live_set(container)
    if len(parsed) != 1:
        raise ValueError("attach expects a Set file containing exactly one Ableton Live Set")
    payload = dict(parsed[0])
    source_sha = payload.pop("xml_sha256")
    previous = None
    if target.is_file():
        try:
            previous = json.loads(target.read_text())
        except (OSError, ValueError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        if previous.get("source_sha256") != source_sha and not force:
            raise ValueError("set_analysis.json records a different source; pass --force to replace")
    same_source = bool(previous) and previous.get("source_sha256") == source_sha
    confirmed = same_version_confirmed_by_owner or (
        same_source and previous.get("version_match") == "owner_confirmed")
    payload.update({"evidence": "set_file", "source_path": str(set_path.resolve()),
                    # source_sha256 is over the parsed Set XML, so the same Set as
                    # .als, .xml or inside an .alp matches; container_sha256 is the file.
                    "source_sha256": source_sha,
                    "container_sha256": hashlib.sha256(container).hexdigest(),
                    "analysed_at": datetime.now(timezone.utc).isoformat(),
                    "ownership": owner,
                    "version_match": "owner_confirmed" if confirmed else "unconfirmed",
                    "note": "Measured from another artist's Live Set; exact for this Set only."})
    write_json_atomic(target, payload)
    return payload


def _set_evidence_summary(card, audio_refs_root=None):
    """Read-only summary of an attached sidecar for get_ref_card/find_audio_refs."""
    if audio_refs_root is None:
        from tools.audio_ref import OUT_ROOT as audio_refs_root
    try:
        path = _sidecar_path(card, audio_refs_root)
    except ValueError:
        return None
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {"available": True, "scenes": data.get("scenes", []),
            "loop_lengths_bars": data.get("loop_lengths_bars", []),
            "sidechain_count": len(data.get("sidechains", [])),
            "return_count": len(data.get("returns", [])),
            "version_match": data.get("version_match", "unconfirmed")}


_LAYER_TERMS = {
    "kick": ("kick",), "drums": ("drum", "hat", "snare", "tom", "kick", "percussion", "perc"),
    "bass": ("bass", "sub"), "guitar": ("guitar", "gtr"),
    "piano": ("piano", "keys", "keyboard"), "noise": ("noise", "texture", "drone"),
    "breath/vocal": ("breath", "vocal", "voice", "sing", "choir"),
}
# A term matches a whole word, optionally inflected ("gtrs", "toms", "singing");
# never a substring ("rising" is not "sing", "custom" is not "tom").
_TERM_SUFFIXES = ("", "s", "es", "ing")


def _words(value):
    """Lower-case words of a name, split on case changes, digits and punctuation."""
    return [w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+", str(value or ""))]


def _layer_roles(value):
    words = set(_words(value))
    return {role for role, terms in _LAYER_TERMS.items()
            if any(term + suffix in words for term in terms for suffix in _TERM_SUFFIXES)}


def _layer_label(value):
    """Known roles of a name, else the name itself as its own layer."""
    roles = _layer_roles(value)
    return roles or ({" ".join(_words(value))} if _words(value) else set())


def _card_layer_roles(layer):
    """Explicit role and timbre are read separately; either may name the role."""
    roles = _layer_roles(layer.get("role")) | _layer_roles(layer.get("timbre"))
    return roles or _layer_label(layer.get("timbre") or layer.get("role"))


def compare_card_to_set(card, set_data):
    """Compare verified card layers with measured Set tracks, without timing claims."""
    if card.get("listen_verified") is not True:
        raise ValueError("compare requires a verified audio-ref card")
    card_roles = set()
    for layer in card.get("layers") or []:
        card_roles |= _card_layer_roles(layer)
    for section in card.get("sections") or []:
        for field in ("layers_in", "layers_out"):
            for value in section.get(field, []):
                card_roles |= _layer_label(value)
    set_roles = set()
    matrix = set_data.get("scene_matrix") or {}
    for track in set_data.get("tracks") or []:
        # The matrix is keyed by track id: two tracks may share a name. A
        # track is a layer of this Set only if it holds clips.
        row = matrix.get(str(track.get("id")))
        cells = row.get("loop_bars") if isinstance(row, dict) else []
        if any(value is not None for value in cells) or track.get("arrangement"):
            set_roles |= _layer_label(track.get("name"))
    sections = card.get("sections") or []
    scene_count = len(set_data.get("scenes") or [])
    version_match = set_data.get("version_match", "unconfirmed")
    if version_match != "owner_confirmed":
        return {
            "label": "DIAGNOSTIC ONLY — VERSION UNCONFIRMED",
            "warning": "Version unconfirmed — differences may be a different version, not model error.",
            "version_match": "unconfirmed",
            "layers": {"matched": [], "card_only": [], "set_only": []},
            "section_count": {"card": len(sections), "set": scene_count, "compatible": None},
            "timing_claims": False,
        }
    return {
        "label": "DIAGNOSTIC ONLY",
        "version_match": "owner_confirmed",
        "layers": {"matched": sorted(card_roles & set_roles),
                   "card_only": sorted(card_roles - set_roles),
                   "set_only": sorted(set_roles - card_roles)},
        "section_count": {"card": len(sections), "set": scene_count,
                           "compatible": len(sections) == scene_count},
        "timing_claims": False,
    }


def compare_ref_to_set(ref_id, *, store_dir=None, index_path=None):
    """Compare a verified audio-ref card with its attached Set analysis.

    Card and sidecar are read from ``store_dir`` (default: the repo store), so a
    non-default store is checked against its own audio_refs, not the repo's."""
    from tools.audio_ref import load_index, normalize_ref_id

    rid = normalize_ref_id(ref_id)
    store = Path(store_dir) if store_dir else Path(__file__).resolve().parents[1] / "store"
    audio_refs = store / "audio_refs"
    index = load_index(Path(index_path) if index_path else store / "audio_ref_index.json")
    card = (index.get("refs") or {}).get(rid)
    if not card:
        raise KeyError(f"unknown ref_id: {rid}")
    if card.get("suspect") is True:
        # Same quarantine get_ref_card applies: a suspect ref is not compared.
        raise ValueError(f"ref {rid} is marked suspect "
                         f"({card.get('suspect_reason') or 'no reason recorded'}); compare refused")
    if card.get("out_dir"):
        try:
            Path(card["out_dir"]).resolve().relative_to(audio_refs.resolve())
        except ValueError as exc:
            raise ValueError(f"ref {rid} out_dir is outside {audio_refs}") from exc
    path = _sidecar_path(card | {"ref_id": rid}, audio_refs)
    if path is None or not path.is_file():
        raise ValueError(f"reference {rid} has no attached Set evidence")
    try:
        set_data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("set_analysis.json is unreadable: %s" % exc) from exc
    if not isinstance(set_data, dict):
        raise ValueError("set_analysis.json is not a JSON object")
    return compare_card_to_set(card, set_data)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "compare":
        parser = argparse.ArgumentParser(description="Diagnostic card vs Live Set comparison")
        parser.add_argument("action")
        parser.add_argument("ref_id")
        parser.add_argument("--store", type=Path)
        args = parser.parse_args(argv)
        try:
            result = compare_ref_to_set(args.ref_id, store_dir=args.store)
        except (OSError, ValueError, KeyError) as exc:
            print(f"live_set_reader: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2))
        print("Set scenes have no stored durations; no timing claims are made.")
        return 0
    if argv and argv[0] == "attach":
        parser = argparse.ArgumentParser(description="Attach read-only Set evidence to a reference")
        parser.add_argument("action")
        parser.add_argument("ref_id")
        parser.add_argument("set_file", type=Path)
        parser.add_argument("--store", type=Path)
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--same-version-confirmed-by-owner", action="store_true",
                            help="record that the owner confirmed this Set matches the recording")
        args = parser.parse_args(argv)
        try:
            attach_set_evidence(args.ref_id, args.set_file, store_dir=args.store,
                                force=args.force,
                                same_version_confirmed_by_owner=args.same_version_confirmed_by_owner)
        except (OSError, ValueError, KeyError, gzip.BadGzipFile) as exc:
            print(f"live_set_reader: {exc}", file=sys.stderr)
            return 2
        print(f"Attached Set evidence for {args.ref_id}")
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    try:
        result = read_live_set(args.file)
        if args.json:
            args.json.write_text(json.dumps(result[0] if len(result) == 1 else result, indent=2) + "\n")
        for item in result:
            _print_matrix(item)
    except (OSError, ValueError, gzip.BadGzipFile) as exc:
        print(f"live_set_reader: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
