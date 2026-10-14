"""Schema checked Producer Pal fakes built from committed device evidence.

Every argument name a test fake receives is checked against the tool's
``inputSchema.properties`` captured from the Producer Pal build the fake
speaks: 2.2.0-rc1 (tests/fixtures/pp/2.2.0-rc1/tool_schemas.json), 2.3.0
(tests/fixtures/pp/2.3.0/tool_schemas.json) or 2.4.0
(tests/fixtures/pp/2.4.0/tool_schemas.json). Unknown tools raise like the
device does. Responses replay captured records (``content[0].json`` is the
decoded text the device sent, see tools/capture_pp_fixtures.py ``_content``).
"""
import functools
import json
import re
from pathlib import Path
from types import SimpleNamespace


V22 = "2.2.0-rc1"
V23 = "2.3.0"
V24 = "2.4.0"
VERSIONS = (V22, V23, V24)
# The builds whose positions live in the object path (2.3's addressing, kept by 2.4).
PATH_VERSIONS = (V23, V24)
_FIXTURES = Path(__file__).parent / "fixtures/pp"
FIXTURE_ROOT = _FIXTURES / V22
_SCHEMA_PATH = FIXTURE_ROOT / "tool_schemas.json"
SCHEMAS = json.loads(_SCHEMA_PATH.read_text())
_SCHEMAS_BY_VERSION = {V22: SCHEMAS}


def fixture_root(version=None):
    """tests/fixtures/pp/<version> (2.2.0-rc1 when omitted)."""
    version = version or V22
    if version not in VERSIONS:
        raise ValueError("no captured Producer Pal fixtures for %r" % (version,))
    return _FIXTURES / version


def schemas_for(version=None):
    """The captured tool schemas of one Producer Pal build."""
    version = version or V22
    if version not in _SCHEMAS_BY_VERSION:
        _SCHEMAS_BY_VERSION[version] = json.loads(
            (fixture_root(version) / "tool_schemas.json").read_text())
    return _SCHEMAS_BY_VERSION[version]


def validate_call(name, arguments, version=None):
    """Raise when ``name`` is not a captured tool or an argument is unlisted
    (``version`` defaults to the build the running test speaks)."""
    tool = schemas_for(version or active_version()).get(name)
    if tool is None:
        raise RuntimeError("Unknown tool '%s'" % name)
    properties = tool.get("inputSchema", {}).get("properties", {})
    unknown = set(arguments or {}) - set(properties)
    if unknown:
        raise AssertionError("unknown arguments for %s: %s" %
                             (name, ", ".join(sorted(unknown))))


# --- real device warning texts ------------------------------------------------
#
# Taken from the warn(...) calls in the device bundles themselves (read-only
# ``strings`` of ~/Documents/Producer_Pal_2.2.0-rc1.amxd and
# ~/Documents/Producer_Pal (3).amxd, the 2.3.0 build); runtime parts (ids,
# paths, error text) filled in. Not device captures: no capture recorded them,
# but both devices emit them.
#
# Sent NEXT TO A WRITE THAT HAPPENED (the project-context backup sync runs on
# every call and times out when no node sidecar answers; colours are
# quantized; a duplicated track loses its Producer Pal device):
DEVICE_ADVISORY_WARNINGS = {
    V22: [
        "WARNING: Project context backup sync failed: node_request "
        "'projectContext.sync' timed out after 5000ms",
        "WARNING: Could not save the project context backup beside the Live Set (the "
        "Live project folder may be read-only). The context is safe in the device, but "
        "won't survive a device upgrade. Tell the user.",
        "WARNING: Could not verify color quantization: Error: timeout",
        "WARNING: Requested clip color #FF6A00 was mapped to nearest palette color "
        "#FF3636. Live uses a fixed color palette.",
        "WARNING: Removed Producer Pal device from duplicated track - the device cannot "
        "be duplicated",
    ],
    V23: [
        "WARNING: Project context backup sync failed: node_request "
        "'projectContext.sync' timed out after 5000ms",
        "WARNING: Could not save the project context backup beside the Live Set (the "
        "Live project folder may be read-only). The context is safe in the device, but "
        "won't survive a device upgrade. Tell the user.",
        "WARNING: Could not verify color quantization for clip t11/s0: Error: timeout",
        "WARNING: Requested clip id 901 color #FF6A00 was mapped to nearest palette "
        "color #FF3636. Live uses a fixed color palette.",
        "WARNING: removed the Producer Pal device from the new track t11 - it cannot "
        "be duplicated",
    ],
    # 2.4.0: CAPTURED next to complete reads and writes that happened
    # (tests/fixtures/pp/2.4.0/legacy, mutate, v23b, v24): the deprecation
    # notices for the old argument forms and the id fallbacks. The 2.4 device
    # bundle was not read, so the backup-sync / colour texts are not listed
    # for it — nothing in the 2.4 captures carried them.
    V24: [
        "WARNING: param \"trackIndex\" is deprecated and will be removed; use \"path\" "
        "instead (e.g. path: \"t10\")",                        # legacy_read_track
        "WARNING: param \"count\" is deprecated and will be removed; use \"path\" instead "
        "(e.g. path: \"s+\"). path names every scene, so repeat it once per scene instead "
        "of counting",                                          # v24_create_scene_count_deprecated
        "WARNING: param \"arrangementStart\" is deprecated and will be removed; use "
        "\"toPath\" instead (e.g. toPath: \"t2[5|1]\")",        # mutate/duplicate_clip_to_arrangement
        "WARNING: \"ids\" accepted as a fallback; the parameter is \"id\"",   # legacy_delete_ids
        "WARNING: \"clipId\" accepted as a fallback; the parameter is \"id\"",  # legacy_read_clip_clipId
        "WARNING: param \"deviceName\" is deprecated and will be removed; use \"device\" "
        "instead",                                              # v24_create_device_deprecated_name_append
    ],
}
# 2.4.0 refusals that are MCP errors (is_error: true), captured; 2.3 sent
# `[]` / `{deleted: false}` + the WARNING for the same calls.
DEVICE_REFUSAL_ERRORS = {
    V24: {
        # 2.4.0/v23b/v23b_refuse_create_midi_on_audio.json
        "create": "Error: track t11 (id 461) is audio; a MIDI clip needs a MIDI track",
        # 2.4.0/v23/v23_duplicate_midi_to_audio.json
        "duplicate": "Error: track t11 (id 415) is audio; a MIDI clip needs a MIDI track",
        # 2.4.0/v23b/v23b_delete_lane_clip.json
        "lane_delete": "Error: cannot delete take-lane clip t10/l0[5|1] (id 473) via the "
                       "API; remove it in Live's UI",
        # v24/v24_update_track_send_missing_return.json
        "send": "Error: no send landed — \"NO SUCH RETURN\": no return track matching "
                "\"NO SUCH RETURN\" (Available: A-Reverb, B-Delay)",
    },
}
# Sent next to a write that did NOT happen (same source), keyed by kind.
DEVICE_REFUSAL_WARNINGS = {
    V22: {
        "move": "WARNING: clip not moved: toPath t10 has no clip slot",
        "split": "WARNING: arrangementSplit cut nothing: no split point falls inside any "
                 "of the clips. Positions are song positions (bar|beat); the clips span "
                 "57|1-61|1.",
        "update": "WARNING: Ran out of time after updating 0 of 1 clips. Not updated: 446. "
                  "Re-run for those ids.",
        "audio_notes": "WARNING: notes parameter ignored for audio clip",
        "delete": 'WARNING: delete: clip "445" still exists, so Live did not delete it',
        "duplicate": "WARNING: MIDI clip 425 was not duplicated: track 11 is audio",
    },
    V23: {
        "move": "WARNING: clip t10[9|1] was not moved: it would land on clip t10[13|1], "
                "which Live wouldn't move; move that clip first, or use separate calls",
        "split": "WARNING: arrangementSplit cut nothing: no split point falls inside any "
                 "of the clips. Positions are song positions (bar|beat); the clips span "
                 "57|1-61|1.",
        "update": "WARNING: Ran out of time after updating 0 of 1 clips. Not updated: "
                  "t10[57|1]. Re-run for those clips.",
        "audio_notes": "WARNING: notes parameter ignored for audio clip t1/s0",
        "delete": "WARNING: clip t10[49|1] still exists, so Live did not delete it",
        # captured: v23/v23_duplicate_midi_to_audio.json
        "duplicate": "WARNING: clip t10[1|1] (id 425) was not duplicated: track t11 (id "
                     "424) is audio; a MIDI clip needs a MIDI track",
        # captured: v23b/v23b_refuse_create_midi_on_audio.json
        "create": "WARNING: Failed to create clip at t11[1|1]: track t11 (id 363) is "
                  "audio; a MIDI clip needs a MIDI track",
        # captured: v23b/v23b_delete_lane_clip.json
        "lane_delete": "WARNING: cannot delete take-lane clip t10/l0[5|1] (id 376) via "
                       "the API; remove it in Live's UI",
    },
    V24: {
        # the ONE 2.4 refusal still sent as a WARNING next to a successful-looking
        # {id, path} object: v24/v24_split_clip_deprecated_key.json
        "split": "WARNING: split cut nothing: no split point falls inside any of the "
                 "clips. Positions are relative to each clip's start (1|1), and must be "
                 "before its end; the clips span 558 (1|1 to 3|1).",
    },
}


def _with_device_warnings(fake, response):
    """``response`` plus the fake's ``device_warnings`` (every reply carries
    them, like the backup-sync warning on a real device)."""
    extra = list(getattr(fake, "device_warnings", None) or [])
    if not extra:
        return response
    from mcp_server import pp_session
    if isinstance(response, dict) and isinstance(response.get("content"), list):
        return {**response, "content": response["content"] + [{"text": w} for w in extra]}
    if isinstance(response, pp_session.PPResponse):
        return pp_session.PPResponse(response.raw, response.result,
                                     list(response.warnings) + extra, response.is_json)
    return response


# --- which build a fake speaks -------------------------------------------------

ACTIVE_VERSION = V22     # what a fake constructed without a version speaks


def active_version():
    return ACTIVE_VERSION


def _set_active(version):
    global ACTIVE_VERSION
    previous, ACTIVE_VERSION = ACTIVE_VERSION, version
    return previous


def fake_version(fake):
    return getattr(fake, "pp_version", None) or V22


def _twin_suffix(version):
    return "PP" + "".join(version.split(".")[:2])     # 2.3.0 -> PP23, 2.4.0 -> PP24


def on_both_versions(cls=None, *, versions=PATH_VERSIONS):
    """TestCase decorator: run the class as-is (Producer Pal 2.2.0-rc1) AND as
    a twin ``<Name>PP23`` against 2.3.0 (``versions`` names the twins:
    ``on_all_versions`` adds the ``PP24`` twin against 2.4.0). Fakes
    constructed during a test (or its setUpClass) speak the class's
    ``PP_VERSION``: their calls are checked against that build's captured
    schemas (a 2.3 call carrying a removed 2.2 parameter fails the test) and
    they answer in that build's shapes."""
    import sys
    import unittest

    if cls is None:
        return functools.partial(on_both_versions, versions=versions)

    original_setup_class = cls.__dict__.get("setUpClass")
    original_teardown_class = cls.__dict__.get("tearDownClass")
    original_setup = cls.__dict__.get("setUp")

    def setUpClass(klass):
        klass._pp_previous_version = _set_active(klass.PP_VERSION)
        try:
            if original_setup_class is not None:
                original_setup_class.__func__(klass)
            else:
                super(cls, klass).setUpClass()
        except BaseException:
            _set_active(klass._pp_previous_version)
            raise

    def tearDownClass(klass):
        try:
            if original_teardown_class is not None:
                original_teardown_class.__func__(klass)
            else:
                super(cls, klass).tearDownClass()
        finally:
            _set_active(getattr(klass, "_pp_previous_version", V22))

    def setUp(self):
        _set_active(self.PP_VERSION)
        if original_setup is not None:
            original_setup(self)
        else:
            super(cls, self).setUp()

    assert issubclass(cls, unittest.TestCase), cls
    cls.PP_VERSION = V22
    cls.setUpClass = classmethod(setUpClass)
    cls.tearDownClass = classmethod(tearDownClass)
    cls.setUp = setUp
    for version in versions:
        assert version in VERSIONS, version
        name = cls.__name__ + _twin_suffix(version)
        twin = type(name, (cls,), {"PP_VERSION": version, "__module__": cls.__module__,
                                   "__qualname__": name,
                                   "__doc__": (cls.__doc__ or "") + " [Producer Pal %s]" % version})
        setattr(sys.modules[cls.__module__], name, twin)
    return cls


def on_all_versions(cls):
    """``on_both_versions`` with the 2.4.0 twin too: ``<Name>PP23`` and ``<Name>PP24``."""
    return on_both_versions(cls, versions=PATH_VERSIONS)


# --- 2.3 requests in 2.2 terms (so one fake handler serves both builds) --------
#
# A 2.3 fake validates the RAW arguments against the 2.3 schemas, then hands
# its handler the equivalent 2.2 arguments. Only forms with an exact 2.2
# equivalent translate; anything else fails the test loudly.

_T = re.compile(r"t(\d+)")
_RT = re.compile(r"rt(\d+)")
_S = re.compile(r"s(\d+)")
_ARR = re.compile(r"t(\d+)\[([^\]]+)\]")
_LANE_ARR = re.compile(r"t(\d+)/l(\d+)\[([^\]]+)\]")
_OWN_ARR = re.compile(r"\[([^\]]+)\]")
_BAR_BEAT = re.compile(r"[1-9]\d*\|\d+(?:\.\d+)?")


def _v22_position(position, label):
    if not _BAR_BEAT.fullmatch(position):
        raise AssertionError("%s position %r has no 2.2 bar|beat form" % (label, position))
    return position


def path_request_as_v22(name, arguments):
    """The 2.2.0-rc1 arguments equivalent to a 2.3.0 / 2.4.0 request (strict).
    The 2.4 batch appends — create-track 't+,t+' (v24_create_tracks_batch),
    create-scene 's+,s+' (v24_create_scenes_batch) — become the 2.2 ``count``;
    every other comma-separated path has no 2.2 form."""
    args = dict(arguments or {})
    path = args.get("path")
    if isinstance(path, str) and "," in path:
        parts = path.split(",")
        if name == "ppal-create-track" and all(part == "t+" for part in parts):
            return {**{k: v for k, v in args.items() if k != "path"}, "count": len(parts)}
        if name == "ppal-create-scene" and all(part == "s+" for part in parts):
            return {**{k: v for k, v in args.items() if k != "path"}, "count": len(parts)}
        raise AssertionError("%s: comma-separated paths are not translated" % name)
    if name == "ppal-read-track" and path is not None:
        args.pop("path")
        if _T.fullmatch(path):
            args["trackIndex"] = int(_T.fullmatch(path).group(1))
        elif _RT.fullmatch(path):
            args.update(trackType="return", trackIndex=int(_RT.fullmatch(path).group(1)))
        elif path == "mt":
            args["trackType"] = "master"
        else:
            raise AssertionError("read-track path %r has no 2.2 form" % path)
    elif name == "ppal-create-track" and path is not None:
        args.pop("path")
        if path == "rt+":
            args["type"] = "return"
        elif _T.fullmatch(path):
            args["trackIndex"] = int(_T.fullmatch(path).group(1))
        elif path != "t+":
            raise AssertionError("create-track path %r has no 2.2 form" % path)
    elif name == "ppal-create-clip" and path is not None:
        if _ARR.fullmatch(path):
            track, position = _ARR.fullmatch(path).groups()
            args.update(path="t%s" % track,
                        arrangementStart=_v22_position(position, "create-clip"))
        elif _LANE_ARR.fullmatch(path):
            track, lane, position = _LANE_ARR.fullmatch(path).groups()
            args.update(path="t%s/l%s" % (track, lane),
                        arrangementStart=_v22_position(position, "create-clip"))
    elif name == "ppal-duplicate":
        to_path = args.get("toPath")
        if isinstance(to_path, str) and "," in to_path:
            raise AssertionError("duplicate: comma-separated toPath is not translated")
        if isinstance(to_path, str) and _ARR.fullmatch(to_path):
            track, position = _ARR.fullmatch(to_path).groups()
            args.update(toPath="t%s" % track,
                        arrangementStart=_v22_position(position, "duplicate"))
        elif isinstance(to_path, str) and _OWN_ARR.fullmatch(to_path):
            args.pop("toPath")
            args["arrangementStart"] = _v22_position(
                _OWN_ARR.fullmatch(to_path).group(1), "duplicate")
        if path is not None and "/d" not in path:
            raise AssertionError("duplicate by path %r has no 2.2 form" % path)
    elif name == "ppal-update-clip":
        to_path = args.get("toPath")
        if isinstance(to_path, str) and _ARR.fullmatch(to_path):
            # 2.2 moved an arrangement clip on its OWN track (arrangementStart)
            args.pop("toPath")
            args["arrangementStart"] = _v22_position(
                _ARR.fullmatch(to_path).group(2), "update-clip")
        if isinstance(path, str) and not re.fullmatch(r"t\d+/s\d+", path):
            raise AssertionError("update-clip path %r has no 2.2 form" % path)
    elif name == "ppal-delete" and path is not None:
        if (_ARR.fullmatch(path) or _T.fullmatch(path) or _RT.fullmatch(path)
                or _S.fullmatch(path)):
            raise AssertionError("delete path %r has no 2.2 form (delete by id)" % path)
    elif name == "ppal-create-scene" and path is not None:
        args.pop("path")
        if path == "s+":
            pass            # append: the 2.2 handler appends when sceneIndex is absent
        elif not _S.fullmatch(path):
            raise AssertionError("create-scene path %r has no 2.2 form" % path)
        else:
            args["sceneIndex"] = int(_S.fullmatch(path).group(1))
    elif name == "ppal-read-scene" and path is not None:
        args.pop("path")
        if not _S.fullmatch(path):
            raise AssertionError("read-scene path %r has no 2.2 form" % path)
        args["sceneIndex"] = int(_S.fullmatch(path).group(1))
    elif name == "ppal-update-scene" and path is not None:
        raise AssertionError("update-scene by path has no 2.2 form (update by id)")
    elif name == "ppal-playback":
        if args.get("action") == "play-scene" and isinstance(path, str) and _S.fullmatch(path):
            args.pop("path")
            args["sceneIndex"] = int(_S.fullmatch(path).group(1))
        for key, locator in (("startTime", "startLocator"), ("loopStart", "loopStartLocator"),
                             ("loopEnd", "loopEndLocator")):
            value = args.get(key)
            if isinstance(value, str) and value.startswith("loc:"):
                args.pop(key)
                args[locator] = value[4:]
    elif name == "ppal-update-track":
        if path is not None:
            raise AssertionError("update-track by path has no 2.2 form (update by id)")
        for key in ("inputRoutingType", "inputRoutingChannel", "outputRoutingType",
                    "outputRoutingChannel", "sends"):
            if key in args:
                raise AssertionError("update-track %s has no 2.2 form" % key)
    elif name == "ppal-library" and "searches" in args:
        args["queries"] = args.pop("searches")
    return args


v23_request_as_v22 = path_request_as_v22
v24_request_as_v22 = path_request_as_v22


def request_as_v22(name, arguments, version):
    """The 2.2 arguments a fake's handler sees for a request of ``version``."""
    return path_request_as_v22(name, arguments) if version in PATH_VERSIONS else arguments


# --- 2.2 replies in 2.3 shapes (from the paired captures) -----------------------
#
# Each rule is a difference between a 2.2.0-rc1 capture and the 2.3.0 capture
# of the same call on the same SCRATCH-capture Set (mutate/, legacy/, read/):
#   tracks[].trackIndex            -> path 't<i>'          (read_live_set_*, read_track_*)
#   returnTracks[] returnTrackIndex, type -> path 'rt<i>'   (read_live_set_tracks_locators)
#   masterTrack{type: master}      -> mainTrack{path: 'mt'} (read_live_set_tracks_locators)
#   locators[]                     + position 'loc:<name>' (read_live_set_tracks_locators)
#   arrangementClips[] arrangementStart (+ bare track path) -> path 't<i>[<pos>]'
#                                                          (read_track_after_dup, v23_read_*)
#   duplicate / create-track trackIndex -> path            (duplicate_track_without_clips,
#                                                           legacy_create_track)
#   duplicate / create-clip arrangementStart -> path 't<i>[<pos>]'
#                                                          (duplicate_clip_to_arrangement)
#   playback: currentTime / arrangementLoop dropped, startTime echoed
#                                                          (playback_update_arrangement)
#   select: selectedTrack.trackIndex / selectedScene.sceneIndex -> path
#                                                          (read/select_current)
#   errors: "Error executing tool 'x': readX failed: …" -> "Error: …"
#                                                          (error_missing_track, legacy_read_clip_*)
# Inferred, not captured (marked where used): scenes[].sceneIndex -> path 's<i>',
# take-lane clips 't<i>/l<k>' + arrangementStart -> 't<i>/l<k>[<pos>]'. Not
# reproducible without device state: update-track's echoed path, delete's
# deletedPath, read-track sends[].returnId.


def _arrangement_clip_v23(clip, track_index):
    out = {key: value for key, value in clip.items()
           if key not in ("arrangementStart", "trackIndex")}
    start = clip.get("arrangementStart")
    path = clip.get("path")
    if start is None:
        return out
    if path is None or _T.fullmatch(str(path)):
        index = int(_T.fullmatch(path).group(1)) if path else track_index
        if index is not None:
            out["path"] = "t%d[%s]" % (index, start)
    elif re.fullmatch(r"t\d+/l\d+", str(path)):
        out["path"] = "%s[%s]" % (path, start)           # inferred (no 2.3 lane capture)
    return out


def _session_clip_v23(clip):
    out = dict(clip)
    if "path" not in out and out.get("trackIndex") is not None and out.get("sceneIndex") is not None:
        out["path"] = "t%d/s%d" % (out["trackIndex"], out["sceneIndex"])
    if "path" in out:
        out.pop("trackIndex", None)
        out.pop("sceneIndex", None)
    return out


def _track_v23(record, kind="regular", index=None):
    if not isinstance(record, dict):
        return record
    out = dict(record)
    if kind == "regular":
        index = out.pop("trackIndex", index)
        if "path" not in out and isinstance(index, int) and not isinstance(index, bool):
            out["path"] = "t%d" % index
    elif kind == "return":
        index = out.pop("returnTrackIndex", index)
        if out.get("type") == "return":
            out.pop("type")
        if isinstance(index, int):
            out["path"] = "rt%d" % index
    elif kind == "main":
        if out.get("type") == "master":
            out.pop("type")
        out["path"] = "mt"
    track_index = index if kind == "regular" else None
    for key in ("sessionClips", "session-clips"):
        if isinstance(out.get(key), list):
            out[key] = [_session_clip_v23(c) if isinstance(c, dict) else c for c in out[key]]
    for key in ("arrangementClips", "arrangement-clips"):
        if isinstance(out.get(key), list):
            out[key] = [_arrangement_clip_v23(c, track_index) if isinstance(c, dict) else c
                        for c in out[key]]
    if isinstance(out.get("takeLanes"), list):
        out["takeLanes"] = [
            dict(lane, clips=[_arrangement_clip_v23(
                c if "path" in c else {**c, "path": lane.get("path")}, track_index)
                for c in lane.get("clips") or [] if isinstance(c, dict)])
            if isinstance(lane, dict) else lane for lane in out["takeLanes"]]
    return out


def _scene_v23(scene):
    if isinstance(scene, dict) and isinstance(scene.get("sceneIndex"), int):
        return {**{k: v for k, v in scene.items() if k != "sceneIndex"},
                "path": "s%d" % scene["sceneIndex"]}
    return scene


def _each(value, fn):
    if isinstance(value, list):
        return [_each(item, fn) for item in value]
    return fn(value) if isinstance(value, dict) else value


def v22_payload_as_v23(name, args22, value):
    """A 2.2-shaped reply payload in the 2.3.0 shape (idempotent on 2.3 data)."""
    args22 = args22 or {}
    if name == "ppal-read-live-set" and isinstance(value, dict):
        out = dict(value)
        if isinstance(out.get("tracks"), list):
            out["tracks"] = [_track_v23(t) for t in out["tracks"]]
        if isinstance(out.get("returnTracks"), list):
            out["returnTracks"] = [_track_v23(t, "return") for t in out["returnTracks"]]
        if isinstance(out.get("masterTrack"), dict):
            out["mainTrack"] = _track_v23(out.pop("masterTrack"), "main")
        if isinstance(out.get("locators"), list):
            out["locators"] = [dict(loc, position="loc:%s" % loc["name"])
                               if isinstance(loc, dict) and "name" in loc
                               and "position" not in loc else loc for loc in out["locators"]]
        if isinstance(out.get("scenes"), list):              # inferred, not captured
            out["scenes"] = [_scene_v23(scene) for scene in out["scenes"]]
        return out
    if name == "ppal-read-track":
        kind = {"return": "return", "master": "main"}.get(args22.get("trackType"), "regular")
        return _each(value, lambda rec: _track_v23(rec, kind, args22.get("trackIndex")))
    if name == "ppal-create-track":
        return _each(value, _track_v23)
    if name in ("ppal-duplicate", "ppal-create-clip", "ppal-update-clip"):
        if name == "ppal-duplicate" and args22.get("type") == "track":
            return _each(value, _track_v23)
        target = args22.get("toPath") if name == "ppal-duplicate" else args22.get("path")
        match = _T.fullmatch(target) if isinstance(target, str) else None
        index = int(match.group(1)) if match else None

        def clip(rec):
            out = _arrangement_clip_v23(rec, index)
            if isinstance(out.get("clips"), list):
                out["clips"] = [_arrangement_clip_v23(c, index) if isinstance(c, dict) else c
                                for c in out["clips"]]
            return out
        return _each(value, clip)
    if name == "ppal-read-clip":
        return _each(value, lambda rec: _session_clip_v23(
            _arrangement_clip_v23(rec, rec.get("trackIndex"))))
    if name == "ppal-select" and isinstance(value, dict):
        out = dict(value)
        if isinstance(out.get("selectedTrack"), dict):
            out["selectedTrack"] = _track_v23(out["selectedTrack"])
        if isinstance(out.get("selectedScene"), dict):
            out["selectedScene"] = _scene_v23(out["selectedScene"])
        if isinstance(out.get("selectedClip"), dict):
            out["selectedClip"] = _session_clip_v23(_arrangement_clip_v23(
                out["selectedClip"], out["selectedClip"].get("trackIndex")))
        return out
    if name == "ppal-playback" and isinstance(value, dict):
        out = {k: v for k, v in value.items() if k not in ("currentTime", "arrangementLoop")}
        if args22.get("startTime") is not None and "startTime" not in out:
            out["startTime"] = args22["startTime"]
        return out
    return value


def v22_error_as_v23(text):
    """2.2 "Error executing tool 'x': readTrack: …" -> 2.3 "Error: …"."""
    if not isinstance(text, str):
        return text
    match = re.match(r"Error executing tool '[^']+':\s*(.*)", text, re.DOTALL)
    if not match:
        return text
    body = re.sub(r"^[a-z][A-Za-z]*(?: failed)?:\s+", "", match.group(1))
    return "Error: " + body


# --- 2.2 replies in 2.4 shapes: the 2.3 shape plus the 2.4 deltas between the
# 2.3.0 and 2.4.0 captures of the same calls (tests/test_pp_fakes_versions.py
# Reshaper24MatchesPairedCapturesTests):
#   delete {deleted: true, id, type}  -> {deletedPath, id}       (mutate/delete_track,
#                                                                 v23/v23_delete_by_id)
#   delete {deleted: false, id, type} + WARNING "id … does not exist"
#                                     -> {detail: "nothing to delete", id}, no warning
#                                                                (v23/v23_delete_missing_id)
#   playback: startTime no longer echoed                         (mutate/playback_update_arrangement,
#                                                                 v23/v23_brace_set)
#   play-scene: scene {id, path} without name                    (v23b/v23b_play_scene_by_path)
#   errors: "Error: trackIndex 999 does not exist" -> 'Error: no track at "t999"'
#                                                                (mutate/error_missing_track)
# Not reproducible without device state: arrangementClips[].arrangementLength
# (2.4 lists it on every arrangement clip, mutate/read_track_after_dup).

def _delete_v24(receipt):
    if not isinstance(receipt, dict) or "deleted" not in receipt:
        return receipt
    if receipt.get("deleted") is True:
        return {key: value for key, value in receipt.items() if key not in ("deleted", "type")}
    return {"detail": "nothing to delete", "id": receipt.get("id")}


def v22_payload_as_v24(name, args22, value):
    """A 2.2-shaped reply payload in the 2.4.0 shape (idempotent on 2.4 data)."""
    out = v22_payload_as_v23(name, args22, value)
    if name == "ppal-delete":
        return _each(out, _delete_v24)
    if name == "ppal-playback" and isinstance(out, dict):
        out = {key: item for key, item in out.items() if key != "startTime"}
        if isinstance(out.get("scene"), dict):
            out["scene"] = {key: item for key, item in out["scene"].items() if key != "name"}
    return out


def v22_error_as_v24(text):
    """2.2 error text in the 2.4 form: the 2.3 "Error: …" and, for a missing
    track by the deprecated trackIndex, 'Error: no track at "t999"'
    (2.4.0/mutate/error_missing_track.json)."""
    text = v22_error_as_v23(text)
    if not isinstance(text, str):
        return text
    return re.sub(r'^Error: trackIndex (\d+) does not exist$', r'Error: no track at "t\1"', text)


_PAYLOAD_AS = {V23: v22_payload_as_v23, V24: v22_payload_as_v24}
_ERROR_AS = {V23: v22_error_as_v23, V24: v22_error_as_v24}


def _is_delete_miss_warning(text):
    return isinstance(text, str) and re.match(r'WARNING: id "[^"]+" does not exist\s*$', text)


def v22_response_as(version, name, args22, response):
    """A captured-shape response dict ({"content": [...], "is_error"}) in the
    shapes of ``version``."""
    if version not in PATH_VERSIONS:
        return response
    if not isinstance(response, dict) or not isinstance(response.get("content"), list):
        return response
    payload_as, error_as = _PAYLOAD_AS[version], _ERROR_AS[version]
    parts = []
    for part in response["content"]:
        if "json" in part:
            parts.append({"json": payload_as(name, args22, part["json"])})
        elif response.get("is_error") and "text" in part:
            parts.append({"text": error_as(part["text"])})
        elif (version == V24 and name == "ppal-delete"
              and _is_delete_miss_warning(part.get("text"))):
            continue        # 2.4 says {detail: "nothing to delete"} and warns nothing
        else:
            parts.append(dict(part))
    return {**response, "content": parts}


def v22_response_as_v23(name, args22, response):
    return v22_response_as(V23, name, args22, response)


def v22_response_as_v24(name, args22, response):
    return v22_response_as(V24, name, args22, response)


def _reshape_result(version, name, args22, result):
    """Reshape whatever a PPSession-level fake returned (PPResponse, JSON text,
    captured-shape dict or CallToolResult) into the shapes of ``version``."""
    from mcp_server import pp_session
    if version not in PATH_VERSIONS:
        return result
    payload_as, error_as = _PAYLOAD_AS[version], _ERROR_AS[version]
    if isinstance(result, pp_session.PPResponse):
        if not result.is_json:
            return result
        value = payload_as(name, args22, result.result)
        warnings = [w for w in result.warnings
                    if not (version == V24 and name == "ppal-delete" and _is_delete_miss_warning(w))]
        return pp_session.PPResponse(json.dumps({"result": value, "warnings": warnings}),
                                     value, warnings)
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        return v22_response_as(version, name, args22, result)
    if hasattr(result, "content") and isinstance(getattr(result, "content"), list):
        texts = []
        for part in result.content:
            text = getattr(part, "text", None)
            try:
                decoded = json.loads(text)
            except (TypeError, ValueError):
                if (version == V24 and name == "ppal-delete" and _is_delete_miss_warning(text)):
                    continue
                texts.append(error_as(text) if getattr(result, "isError", False) else text)
                continue
            if isinstance(decoded, dict) and "result" in decoded:
                decoded = {**decoded, "result": payload_as(name, args22, decoded["result"])}
            else:
                decoded = payload_as(name, args22, decoded)
            texts.append(json.dumps(decoded))
        return SimpleNamespace(content=[SimpleNamespace(text=t) for t in texts],
                               isError=getattr(result, "isError", False))
    if isinstance(result, str):
        try:
            decoded = json.loads(result)
        except ValueError:
            return result
        if isinstance(decoded, dict) and "result" in decoded:
            decoded = {**decoded, "result": payload_as(name, args22, decoded["result"])}
        else:
            decoded = payload_as(name, args22, decoded)
        return json.dumps(decoded)
    return result


def _reshape_result_v23(name, args22, result):
    return _reshape_result(V23, name, args22, result)


def _versioned(original):
    """Wrap a fake's async ``call``/``call_tool``: validate the RAW arguments
    against the fake's build, record them on ``wire_calls``, hand the handler
    2.2 arguments and return that build's shapes when the fake speaks 2.3 or
    2.4. Re-entrant calls (a subclass ``call`` delegating to ``super().call``)
    pass through."""
    @functools.wraps(original)
    async def wrapped(self, name, args=None, *rest, **kwargs):
        if getattr(self, "_pp_depth", 0):
            return await original(self, name, args, *rest, **kwargs)
        version = fake_version(self)
        validate_call(name, args, version=version)
        if not hasattr(self, "wire_calls"):
            self.wire_calls = []
        self.wire_calls.append((name, dict(args or {})))
        inner = request_as_v22(name, args, version)
        self._pp_depth = 1
        try:
            result = await original(self, name, inner, *rest, **kwargs)
        finally:
            self._pp_depth = 0
        result = _reshape_result(version, name, inner, result)
        return _with_device_warnings(self, result)
    wrapped._pp_versioned = True
    return wrapped


def schema_strict(cls):
    """Class decorator: validate every ``call``/``call_tool`` before the fake runs,
    against the captured schemas of the build the fake speaks (``pp_version``,
    set at construction from ``active_version()`` unless given). A 2.3 fake's
    handler receives 2.2 arguments and its replies are returned in 2.3 shapes.

    Apply it to each fake class that defines its own ``call`` (a subclass
    override is not wrapped by the base class decoration).
    """
    for method_name in ("call", "call_tool"):
        original = cls.__dict__.get(method_name)
        if original is None or getattr(original, "_pp_versioned", False):
            continue
        setattr(cls, method_name, _versioned(original))
    original_init = cls.__dict__.get("__init__")

    def __init__(self, *args, **kwargs):
        if "pp_version" not in self.__dict__:
            self.pp_version = active_version()
        if original_init is not None:
            original_init(self, *args, **kwargs)
        else:
            super(cls, self).__init__(*args, **kwargs)

    cls.__init__ = __init__
    return cls


def load_fixture(relative, version=None):
    """One captured record: {"tool", "arguments", "response"}."""
    return json.loads((fixture_root(version) / relative).read_text())


def _part_text(part):
    return json.dumps(part["json"]) if "json" in part else part.get("text", "")


def response_text(response):
    """The raw text of the first content part (what pp_session.call_tool reads)."""
    content = response.get("content", [])
    return _part_text(content[0]) if content else ""


def captured_json(relative, version=None):
    """The decoded JSON payload of a captured (non-error) response."""
    record = load_fixture(relative, version=version)
    return json.loads(response_text(record["response"]))


def mcp_result(response):
    """A CallToolResult-shaped object for a captured response dict."""
    return SimpleNamespace(
        content=[SimpleNamespace(text=_part_text(part))
                 for part in response.get("content", [])],
        isError=bool(response.get("is_error")))


def json_response(payload, is_error=False):
    """A captured-shape response dict whose single content part is ``payload``."""
    return {"content": [{"json": payload}], "is_error": is_error}


def error_response(text):
    return {"content": [{"text": text}], "is_error": True}


class SchemaStrictSession:
    """Raw MCP ClientSession fake: rejects arguments outside captured schemas.

    ``responses`` maps tool name -> captured response dict (replayed as-is);
    ``handlers`` maps tool name -> callable(arguments) returning one, for
    multi-step flows that call the same tool with different arguments.

    ``version`` (default ``active_version()``) is the build it speaks: raw
    arguments are validated against that build's schemas and recorded on
    ``wire_calls``; ``calls`` and the handlers see the 2.2 equivalent
    (``v23_request_as_v22``) and replies come back in the build's shapes
    (``v22_response_as_v23``). A subclass ``call`` (PPSession-level fake) is
    wrapped the same way.
    """

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        original = cls.__dict__.get("call")
        if original is not None and not getattr(original, "_pp_versioned", False):
            cls.call = _versioned(original)

    def __init__(self, version=None):
        self.pp_version = version or active_version()
        self.schemas = schemas_for(self.pp_version)
        self.responses = {}
        self.handlers = {}
        self.calls = []
        self.wire_calls = []
        self.device_warnings = []   # appended to every reply (DEVICE_ADVISORY_WARNINGS)

    @property
    def speaks_v23(self):
        """Path addressing: 2.3.0 or 2.4.0."""
        return self.pp_version in PATH_VERSIONS

    @property
    def speaks_v24(self):
        return self.pp_version == V24

    def validate_call(self, name, arguments):
        # Inside a wrapped call the handler already holds 2.2 arguments.
        validate_call(name, arguments,
                      version=V22 if getattr(self, "_pp_depth", 0) else self.pp_version)

    def call_tool_sync(self, name, arguments):
        self.validate_call(name, arguments)
        self.wire_calls.append((name, dict(arguments or {})))
        inner = dict(request_as_v22(name, arguments, self.pp_version) or {})
        self.calls.append((name, dict(inner)))
        if name in self.handlers:
            response = self.handlers[name](dict(inner))
        elif name not in self.responses:
            raise AssertionError("no captured response configured for %s" % name)
        else:
            response = self.responses[name]
        response = v22_response_as(self.pp_version, name, inner, response)
        return _with_device_warnings(self, response)

    async def call_tool(self, name, arguments):
        return mcp_result(self.call_tool_sync(name, arguments))

    def handshake_for(self, capabilities=None):
        """A handshake naming this fake's build (and its addressing)."""
        from mcp_server import pp_session
        caps = {"supports_2_x": True, "supports_2_2": True, "producer_pal_2_2": True,
                "supports_2_3": self.speaks_v23, "supports_2_4": self.speaks_v24,
                "addressing": pp_session.addressing_for_version(self.pp_version)}
        caps.update(capabilities or {})
        return {"pp_version": self.pp_version, "capabilities": caps}


def _error_text(relative, version=None):
    return response_text(load_fixture(relative, version=version)["response"])


class CapturedDevice(SchemaStrictSession):
    """A small stateful Producer Pal built from captured records of one build
    (2.2.0-rc1, 2.3.0 with ``version=V23`` or 2.4.0 with ``version=V24`` / an
    active 2.3 or 2.4 twin).

    Starts from the SCRATCH-capture Set (legacy/legacy_live_set_after_create.json
    of that build) and answers every tool with the captured response shape for
    that tool: create-track -> legacy_create_track, update-track ->
    legacy_update_track_id (2.3: v23_mute_by_id, {id, path}), create-clip ->
    legacy_create_clip_path, read-clip -> legacy_read_clip_path, read-track ->
    legacy_read_track / read_track_after_dup, select -> read/select_current,
    missing track -> mutate/error_missing_track, missing clip id ->
    legacy/legacy_read_clip_missing_id. Read-clip ``notes`` echo the midi-json
    string create-clip received (no captured read-clip carries notes; the
    string form is the captured read_track_source.json session-clip ``notes``).
    An existing empty slot replays the 2.2 capture on both builds (2.3 has no
    capture of it). State is kept in 2.2 terms; 2.3 replies are reshaped.
    """

    def __init__(self, version=None):
        super().__init__(version)
        live_set = captured_json("legacy/legacy_live_set_after_create.json", self.pp_version)
        self.live_set = {key: value for key, value in live_set.items() if key != "tracks"}
        self.tracks = []
        for position, track in enumerate(live_set["tracks"]):
            record = {key: value for key, value in track.items() if key != "path"}
            record["trackIndex"] = track.get("trackIndex", position)
            self.tracks.append(record)
        self.clips = {}           # path -> clip record
        self.arrangement = {}     # trackIndex -> [arrangement clip records]
        self.selection = captured_json("read/select_current.json", self.pp_version)
        self.live_refusals = []
        self.next_id = 900
        self.handlers = {
            "ppal-read-live-set": self._read_live_set,
            "ppal-create-track": self._create_track,
            "ppal-read-track": self._read_track,
            "ppal-update-track": self._update_track,
            "ppal-create-clip": self._create_clip,
            "ppal-read-clip": self._read_clip,
            "ppal-select": self._select,
            "ppal-update-live-set": self._update_live_set,
            "ppal-live-api": self._live_api,
            "ppal-delete": self._delete,
            "ppal-create-scene": self._create_scene,
        }
        self.refuse_deletes = False
        self.misreport_deletes = False
        self.ignore_scene_creation = False
        # After the first delete: read-clip by id replies this device error
        # text (a failed post-delete readback), and read-track carries these
        # warnings (an incomplete post-delete inventory). Both None: normal.
        self.read_clip_error_after_delete = None
        self.read_track_warnings_after_delete = []
        # After the first delete: read-clip BY ID replies the captured
        # empty-slot shape ({id: null, name: null, path, type: null} + WARNING
        # "no clip at <path>", check_read_clip_existing_empty_slot.json — a
        # shape only ever captured for a read by PATH) instead of the clip.
        self.read_clip_empty_after_delete = False
        # Runs once, at the first read after the first delete (a stall during
        # which the Set changed): e.g. device.insert_track_at_top(...)
        self.after_delete_hook = None
        # read-track by id answers the record of ANOTHER id (a device that
        # misroutes an id read): {requested id: id whose record is returned}
        self.read_track_id_alias = {}
        self.deleted_once = False
        # create-track replies this offset from the created track's real index
        # (a track that exists, but not where the reply says)
        self.create_track_index_offset = 0
        # create-track appends a track but LIES: the reply names this existing
        # id (a track that was in the Set before the create)
        self.create_track_replies_id = None
        # session clip paths a read-track listing leaves out (a listing that
        # does not show a clip the slot holds — the hazard a slot pre-read
        # guards: 2.4 would replace it, v24_create_session_clip_replaces)
        self.hide_from_listing = set()
        live_api = load_fixture("read/live_api_live_set_properties.json", self.pp_version)
        self.live_api_record = live_api["response"]["content"][0]["json"]

    def _new_id(self):
        self.next_id += 1
        return str(self.next_id)

    def add_track(self, name, type_="midi"):
        track = {"arrangementClipCount": 0, "deviceCount": 0, "id": self._new_id(),
                 "name": name, "sessionClipCount": 0,
                 "trackIndex": len(self.tracks), "type": type_}
        self.tracks.append(track)
        return track

    def add_clip(self, path, name, notes, type_="midi"):
        clip = {"id": self._new_id(), "name": name, "path": path, "type": type_,
                "view": "session", "notes": notes}
        self.clips[path] = clip
        return clip

    def insert_track_at_top(self, name, type_="midi"):
        """A track inserted before every other one (as Live does when he adds a
        track at the top): every track, session clip path and arrangement
        clip moves down one index; ids do not change."""
        track = {"arrangementClipCount": 0, "deviceCount": 0, "id": self._new_id(),
                 "name": name, "sessionClipCount": 0, "trackIndex": 0, "type": type_}
        for existing in self.tracks:
            existing["trackIndex"] += 1
        self.tracks.insert(0, track)
        moved = {}
        for path, clip in self.clips.items():
            match = re.fullmatch(r"t(\d+)/(s\d+)", path)
            new_path = "t%d/%s" % (int(match.group(1)) + 1, match.group(2))
            moved[new_path] = {**clip, "path": new_path}
        self.clips = moved
        self.arrangement = {index + 1: clips for index, clips in self.arrangement.items()}
        return track

    def _fire_after_delete_hook(self):
        hook, self.after_delete_hook = self.after_delete_hook, None
        if hook is not None:
            hook()

    def _read_live_set(self, args):
        payload = dict(self.live_set)
        if "tracks" in (args.get("include") or []):
            payload["tracks"] = [dict(track) for track in self.tracks]
        return json_response(payload)

    def _create_track(self, args):
        # Live's default name for an unnamed track: 'N-MIDI' / 'N-Audio'
        # (captured 22-MIDI / 23-Audio, 2.2.0-rc1/read/read_live_set_tracks_locators.json)
        count = int(args.get("count", 1))
        if count > 1:
            # a 2.4 batch ('t+,t+', names comma-separated one per path) replies a
            # list, one {id, path} per track (v24/v24_create_tracks_batch.json)
            names = str(args.get("name") or "").split(",") if args.get("name") else []
            made = [self.add_track((names[i].strip() if i < len(names) and names[i].strip()
                                    else "%d-MIDI" % (len(self.tracks) + 1)))
                    for i in range(count)]
            return json_response([{"id": t["id"], "trackIndex": t["trackIndex"]} for t in made])
        track = self.add_track(args.get("name") or "%d-MIDI" % (len(self.tracks) + 1))
        return json_response({"id": self.create_track_replies_id or track["id"],
                              "trackIndex": track["trackIndex"] + self.create_track_index_offset})

    def _track(self, index):
        if not isinstance(index, int) or not 0 <= index < len(self.tracks):
            return None
        return self.tracks[index]

    def _read_track(self, args):
        if "id" in args:
            # read-track by id: the locator both schemas take; its reply is
            # uncaptured and this fake answers the same track record
            wanted = self.read_track_id_alias.get(args["id"], args["id"])
            track = next((t for t in self.tracks if t["id"] == wanted), None)
            index = track["trackIndex"] if track else None
        else:
            index = args.get("trackIndex")
            track = self._track(index)
        if track is None:
            if self.speaks_v24 and "path" in (self.wire_calls[-1][1] if self.wire_calls else {}):
                # read by path: 'Error: nothing at path "t999"; …' (v24/v24_read_track_missing);
                # the deprecated trackIndex form says 'no track at "t999"' (mutate/error_missing_track)
                return error_response(_error_text("v24/v24_read_track_missing.json", V24)
                                      .replace("t999", "t%s" % index))
            return error_response(_error_text("mutate/error_missing_track.json",
                                              self.pp_version).replace("999", str(index)))
        include = args.get("include") or []
        out = {key: track[key] for key in ("id", "name", "trackIndex", "type",
                                           "deviceCount", "arrangementClipCount")}
        prefix = "t%d/" % index
        if "session-clips" in include:
            out["sessionClips"] = [
                {key: clip[key] for key in ("id", "name", "path")}
                | ({"notes": clip["notes"]} if "notes" in include else {})
                for path, clip in sorted(self.clips.items())
                if path.startswith(prefix) and path not in self.hide_from_listing]
        if "arrangement-clips" in include:
            out["arrangementClips"] = [
                {key: value for key, value in clip.items()
                 if key != "notes" or "notes" in include}
                for clip in self.arrangement.get(index, [])]
        response = json_response(out)
        if self.deleted_once and self.read_track_warnings_after_delete:
            response["content"] += [{"text": w} for w in self.read_track_warnings_after_delete]
        return response

    def _update_track(self, args):
        track = next((t for t in self.tracks if t["id"] == args["id"]), None)
        if track is None:
            raise AssertionError("update-track on unknown id %r" % args["id"])
        for key in ("name", "color", "mute"):
            if key in args:
                track[key] = args[key]
        if self.speaks_v23:   # v23_mute_by_id.json: {id, path}
            return json_response({"id": track["id"], "trackIndex": track["trackIndex"]})
        return json_response({"id": track["id"]})

    def _create_clip(self, args):
        path = args["path"]
        notes = args.get("notes") or "[]"
        count = len(json.loads(notes)) if notes.startswith("[{\"") else None
        if "arrangementStart" in args:
            # 2.2 form (a 2.3 't10[3|1]' path arrives translated). Reply:
            # legacy/check_create_clip_arrangement.json; listing record:
            # check_read_after_arrangement_create / read_track_after_dup.
            index = int(_T.fullmatch(path).group(1))
            clip = {"arrangementStart": args["arrangementStart"], "id": self._new_id(),
                    "notes": notes}
            if args.get("name"):
                clip["name"] = args["name"]
            self.arrangement.setdefault(index, []).append(clip)
            return json_response({"arrangementStart": args["arrangementStart"],
                                  "id": clip["id"],
                                  **({"noteCount": count} if count is not None else {}),
                                  "path": path})
        slot = re.fullmatch(r"t\d+/s(\d+)", path)
        if slot and int(slot.group(1)) >= self.live_set["sceneCount"]:
            if not self.speaks_v23:
                # 2.2 refuses a slot in a scene that does not exist
                return error_response("Error executing tool 'ppal-create-clip': no scene "
                                      "at \"s%s\"" % slot.group(1))
            # 2.3 creates the missing scenes (v23b_refuse_create_past_last_scene)
            self.live_set["sceneCount"] = int(slot.group(1)) + 1
        replaced = path in self.clips
        if replaced and not self.speaks_v24:
            raise AssertionError("a create-clip into an occupied slot (%s) was not captured "
                                 "on %s" % (path, self.pp_version))
        clip = self.add_clip(path, args.get("name") or "", notes)
        reply = {"id": clip["id"], **({"noteCount": count} if count is not None else {}),
                 "path": path}
        if replaced:
            # 2.4 REPLACES the clip in an occupied slot and says so in `detail`
            # (v24/v24_create_session_clip_replaces.json); the old id is gone
            # (v24_delete_clip_by_id_already_replaced: "nothing to delete")
            reply["detail"] = "overwrote the existing clip at %s" % path
        return json_response(reply)

    def _create_scene(self, args):
        # Appends ``count`` empty scenes: 2.3 'path "s+"' (captured
        # v23b_create_scene_append: {id, path}); 2.2 sceneIndex == sceneCount
        # (not captured). ``ignore_scene_creation`` replies without appending.
        at = args.get("sceneIndex", self.live_set["sceneCount"])
        if at != self.live_set["sceneCount"]:
            raise AssertionError("CapturedDevice only appends scenes, got %r" % (args,))
        count = int(args.get("count", 1))
        first = self.live_set["sceneCount"]
        if not self.ignore_scene_creation:
            self.live_set["sceneCount"] += count
        if self.speaks_v24 and count > 1:
            # a 2.4 batch ('s+,s+') replies a list, one {id, path} per scene
            # (v24/v24_create_scenes_batch.json)
            return json_response([{"id": self._new_id(), "path": "s%d" % (first + i)}
                                  for i in range(count)])
        return json_response({"id": self._new_id(), "path": "s%d" % first})

    def _delete(self, args):
        ids = str(args.get("id") or "").split(",") if args.get("id") else []
        if args.get("type") != "clip" or len(ids) != 1:
            raise AssertionError("CapturedDevice deletes one clip by id, got %r" % (args,))
        clip_id = ids[0]
        self.deleted_once = True
        for path, clip in list(self.clips.items()):
            if clip["id"] == clip_id and self.misreport_deletes:
                # the clip IS gone, but the receipt says it was not (the
                # "still exists, so Live did not delete it" refusal text; on
                # 2.4 the only captured non-deleting reply, "nothing to delete")
                del self.clips[path]
                if self.speaks_v24:
                    return json_response({"detail": "nothing to delete", "id": clip_id})
                return {"content": [
                    {"json": {"deleted": False, "id": clip_id, "type": "clip"}},
                    {"text": DEVICE_REFUSAL_WARNINGS[self.pp_version]["delete"]}],
                    "is_error": False}
            if clip["id"] == clip_id and not self.refuse_deletes:
                del self.clips[path]
                if self.speaks_v24:   # {deletedPath, id}: 2.4.0/legacy/check_delete_clip_by_id
                    return json_response({"deletedPath": path, "id": clip_id})
                reply = {"deleted": True, "id": clip_id, "type": "clip"}  # check_delete_clip_by_id
                if self.speaks_v23:
                    reply["deletedPath"] = path                           # v23_delete_by_id
                return json_response(reply)
        if self.speaks_v24:   # v24_delete_clip_missing_id.json: {detail, id}, no warning
            record = load_fixture("v24/v24_delete_clip_missing_id.json", V24)
            return json.loads(json.dumps(record["response"]).replace("999999", clip_id))
        if self.speaks_v23:   # v23_delete_missing_id.json: deleted:false + WARNING
            record = load_fixture("v23/v23_delete_missing_id.json", V23)
            return json.loads(json.dumps(record["response"]).replace("999999", clip_id))
        raise AssertionError("a 2.2 delete that deletes nothing was not captured")

    def _read_clip(self, args):
        if "id" in args:
            if self.deleted_once:
                self._fire_after_delete_hook()
            if self.deleted_once and self.read_clip_error_after_delete:
                return error_response(self.read_clip_error_after_delete)
            if self.deleted_once and self.read_clip_empty_after_delete:
                clip = next((c for c in self.clips.values() if c["id"] == args["id"]), None)
                slot = clip["path"] if clip else "t0/s0"
                record = load_fixture("legacy/check_read_clip_existing_empty_slot.json")
                content = json.loads(json.dumps(record["response"]["content"])
                                     .replace("t10/s5", slot))
                return {"content": content, "is_error": False}
            clip = next((c for c in self.clips.values() if c["id"] == args["id"]), None)
            if clip is None:
                for clips in self.arrangement.values():
                    clip = next((c for c in clips if c["id"] == args["id"]), clip)
            if clip is None:
                return error_response(
                    _error_text("legacy/legacy_read_clip_missing_id.json", self.pp_version)
                    .replace("999999", str(args["id"])))
        elif "path" in args:
            clip = self.clips.get(args["path"])
            if clip is None:
                if self.speaks_v24:
                    # 2.4: an existing empty slot is an ERROR, 'Error: no clip at <path>'
                    # (2.4.0/legacy/check_read_clip_existing_empty_slot.json)
                    return error_response(_error_text(
                        "legacy/check_read_clip_existing_empty_slot.json", V24)
                        .replace("t10/s5", args["path"]))
                record = load_fixture("legacy/check_read_clip_existing_empty_slot.json")
                content = json.loads(json.dumps(record["response"]["content"])
                                     .replace("t10/s5", args["path"]))
                return {"content": content, "is_error": False}
        else:
            return error_response(_error_text("legacy/legacy_read_clip_no_locator.json",
                                              self.pp_version))
        include = args.get("include") or []
        return json_response({key: value for key, value in clip.items()
                              if key != "notes" or "notes" in include})

    def _select(self, args):
        # Captured read/select_current.json; a focus call replies with the
        # same selection record (its reply is not consumed by production).
        return json_response(self.selection)

    def _update_live_set(self, args):
        self.live_set.update({k: v for k, v in args.items() if k == "tempo"})
        return json_response({"tempo": self.live_set.get("tempo")})

    def _live_api(self, args):
        # Replays read/live_api_live_set_properties.json: one captured receipt
        # per requested getProperty, same root id, in request order.
        path = args.get("path")
        if isinstance(path, str) and path.startswith("id "):
            object_id = path[3:]
            known = {c["id"] for c in self.clips.values()} | {t["id"] for t in self.tracks}
            if object_id not in known and self.speaks_v24:
                # 2.4: a missing id answers {id: "0", path: "", results: [{operation}]}
                # — no result key, no error (v24/v24_live_api_missing_id.json)
                return json_response({"id": "0", "path": "", "results": [
                    {"operation": dict(op)} for op in args["operations"]]})
            raise AssertionError("ppal-live-api on %r was not captured for %s"
                                 % (path, self.pp_version))
        if path != self.live_api_record["path"]:
            self.live_refusals.append(path)
            return error_response("Error executing tool ppal-live-api: live-api path %r "
                                  "was not captured" % (path,))
        captured = {item["operation"]["property"]: item
                    for item in self.live_api_record["results"]}
        results = []
        for operation in args["operations"]:
            if operation.get("type") != "getProperty" or operation.get("property") not in captured:
                raise AssertionError("live-api operation %r not captured" % operation)
            results.append(captured[operation["property"]])
        return json_response({**self.live_api_record, "results": results})

    def pp_session(self, capabilities=None):
        from mcp_server import pp_session
        return pp_session.PPSession(self, {
            "pp_version": self.pp_version,
            "capabilities": dict(capabilities or {}),
        })

    def operation(self, capabilities=None):
        """An ``operation_session`` replacement yielding the real PPSession."""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def factory(*_args, **_kwargs):
            yield self.pp_session(capabilities)
        return factory


def _type_ok(value, expected):
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def _check_value(label, value, spec):
    """The subset of JSON Schema the captured tool schemas use."""
    options = spec.get("anyOf")
    if options:
        failures = []
        for option in options:
            try:
                _check_value(label, value, option)
                return
            except AssertionError as exc:
                failures.append(str(exc))
        raise AssertionError("%s matches no anyOf branch: %s" % (label, "; ".join(failures)))
    expected = spec.get("type")
    if expected is not None and not _type_ok(value, expected):
        raise AssertionError("%s must be %s, got %r" % (label, expected, value))
    if "enum" in spec and value not in spec["enum"]:
        raise AssertionError("%s value %r not in schema enum" % (label, value))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in spec and value < spec["minimum"]:
            raise AssertionError("%s below schema minimum" % label)
        if "maximum" in spec and value > spec["maximum"]:
            raise AssertionError("%s above schema maximum" % label)
    if isinstance(value, list):
        if "minItems" in spec and len(value) < spec["minItems"]:
            raise AssertionError("%s has fewer than %d items" % (label, spec["minItems"]))
        if "maxItems" in spec and len(value) > spec["maxItems"]:
            raise AssertionError("%s has more than %d items" % (label, spec["maxItems"]))
        for position, item in enumerate(value):
            _check_value("%s[%d]" % (label, position), item, spec.get("items") or {})
    if isinstance(value, dict) and "properties" in spec:
        unknown = set(value) - set(spec["properties"])
        if unknown and spec.get("additionalProperties") is False:
            raise AssertionError("%s has unknown keys %s" % (label, sorted(unknown)))
        for key in spec.get("required", []):
            if key not in value:
                raise AssertionError("%s missing required %r" % (label, key))
        for key, item in value.items():
            if key in spec["properties"]:
                _check_value("%s.%s" % (label, key), item, spec["properties"][key])


def validate_call_values(name, arguments, version=None):
    """Names AND values: types, enums, bounds and required keys per captured schema."""
    version = version or active_version()
    validate_call(name, arguments, version=version)
    schema = schemas_for(version)[name].get("inputSchema", {})
    arguments = arguments or {}
    for key in schema.get("required", []):
        if key not in arguments:
            raise AssertionError("%s missing required argument %r" % (name, key))
    for key, value in arguments.items():
        _check_value("%s.%s" % (name, key), value, schema["properties"][key])


PIECE = "piece"
PIECE_NOTES = "piece-notes"
# hear_the_room's arrangement read; PieceDevice(narrow_reads=True) projects it.
_NARROW_TRACK_INCLUDE = ["arrangement-clips", "notes", "timing"]
_NARROW_TRACK_KEYS = ("id", "name", "type", "trackIndex", "arrangementClips", "takeLanes",
                      "isGroup", "groupId", "isGroupMember", "state")
# Keys the device adds to overview tracks only when the include asks for them
# (checked against piece/read_live_set_tracks_locators.json by the tests).
_OVERVIEW_ROUTING_KEYS = ("inputRoutingChannel", "inputRoutingType", "monitoringState",
                          "outputRoutingChannel", "outputRoutingType")
_OVERVIEW_MIXER_KEYS = ("gainDb", "pan", "sends")


def _without_clip_notes(record: dict) -> dict:
    """A track read projected without ``notes`` on its clips (arrangement and
    take-lane), derived from the captured read."""
    out = dict(record)
    if isinstance(out.get("arrangementClips"), list):
        out["arrangementClips"] = [{k: v for k, v in c.items() if k != "notes"}
                                   for c in out["arrangementClips"]]
    if isinstance(out.get("takeLanes"), list):
        out["takeLanes"] = [dict(lane, clips=[{k: v for k, v in c.items() if k != "notes"}
                                              for c in lane.get("clips") or []])
                            if isinstance(lane, dict) else lane
                            for lane in out["takeLanes"]]
    return out


class PieceDevice(SchemaStrictSession):
    """Replays the read-only NOCTURNEv2 capture (fixtures/pp/2.2.0-rc1/piece).

    Every call is checked by name AND value against the captured schemas.
    read-track answers only the include lists that were captured (the reader
    must ask exactly what the device was asked); read-live-set filters the
    full captured overview by ``include`` the way the device does. The Live
    API was OFF on the piece capture device: with ``live_api=False`` any
    ppal-live-api call fails the test. With ``live_api=True`` it replays
    read/live_api_live_set_properties.json for path ``live_set`` (the same Set,
    captured earlier) and fixtures/pp/2.2.0-rc1/piece-notes for path ``id <clip
    id>`` (every arrangement MIDI clip: its 12 timing properties and
    ``call get_notes_extended``). A notes call is answered like Live: the
    captured notes whose start_time lies in [from_time, from_time + span) and
    pitch in [from_pitch, from_pitch + span), as a JSON string. Any other path
    is refused like the device refuses an uncaptured object.

    ``narrow_reads=True`` (opt-in, for hear_the_room) also answers read-track
    with include ["arrangement-clips", "notes", "timing"] (or the same without
    "notes") and read-clip by id with include ["notes", "timing"] by
    PROJECTING the captured superset read (arrangement clips with their notes
    and timing; devices/routing/mixer dropped). That is derived from the
    capture, not a capture of its own. A regular track's captured read asked
    for WITHOUT "notes" (the reader's Live API path) is projected the same
    way: the capture minus every clip's notes.

    Test hooks mutate the replayed Set: move_clip, set_playhead,
    set_selection, add_arrangement_clip, and ``before_call`` callbacks that
    run before the Nth call (to change the Set mid-read).

    Speaking 2.3.0 or 2.4.0 (``version=V23`` / ``V24`` or an active twin) it is
    SYNTHETIC: no 2.3 / 2.4 piece capture exists (NOCTURNEv2 still runs 2.2.0-rc1). Calls
    are validated against the captured 2.3.0 schemas and every reply is the
    2.2 piece capture reshaped by the paired-capture rules above
    (``v22_payload_as_v23``: tracks by path, clip starts in the path,
    mainTrack, rt<i> returns); take lanes and scenes use the inferred shapes.
    """

    def __init__(self, live_api=False, narrow_reads=False, version=None):
        super().__init__(version)
        self.live_api = bool(live_api)
        self.narrow_reads = bool(narrow_reads)
        self.overview = captured_json(PIECE + "/piece_live_set.json")
        self.track_records = {}
        self.track_includes = {}
        index = 0
        while (FIXTURE_ROOT / PIECE / ("piece_track_%02d.json" % index)).exists():
            record = load_fixture(PIECE + "/piece_track_%02d.json" % index)
            self.track_records[index] = json.loads(response_text(record["response"]))
            self.track_includes[("regular", index)] = sorted(record["arguments"]["include"])
            index += 1
        self.return_records = {}
        index = 0
        while (FIXTURE_ROOT / PIECE / ("piece_return_%02d.json" % index)).exists():
            record = load_fixture(PIECE + "/piece_return_%02d.json" % index)
            self.return_records[index] = json.loads(response_text(record["response"]))
            self.track_includes[("return", index)] = sorted(record["arguments"]["include"])
            index += 1
        master = load_fixture(PIECE + "/piece_master.json")
        self.master_record = json.loads(response_text(master["response"]))
        self.track_includes[("master", 0)] = sorted(master["arguments"]["include"])
        self.selection = captured_json(PIECE + "/piece_select.json")
        live_api = load_fixture("read/live_api_live_set_properties.json")
        self.live_api_record = json.loads(response_text(live_api["response"]))
        self.live_api_values = {item["operation"]["property"]: item["result"]
                                for item in self.live_api_record["results"]}
        self.clip_live = {}       # clip id -> {"path", "values", "notes", "ops"}
        for path in sorted((FIXTURE_ROOT / PIECE_NOTES).glob("notes_clip_t*.json")):
            record = load_fixture(PIECE_NOTES + "/" + path.name)
            payload = json.loads(response_text(record["response"]))
            values, notes = {}, None
            for item in payload["results"]:
                operation = item["operation"]
                if operation["type"] == "getProperty":
                    values[operation["property"]] = item["result"]
                else:
                    notes = json.loads(item["result"])["notes"]
            self.clip_live[payload["id"]] = {
                "path": payload["path"], "values": values, "notes": notes,
                "ops": record["arguments"]["operations"]}
        self.live_refusals = []   # uncaptured live-api paths asked for
        self.before_call = {}     # call number (1-based) -> callable(device)
        self.handlers = {
            "ppal-read-live-set": self._read_live_set,
            "ppal-read-track": self._read_track,
            "ppal-read-clip": self._read_clip,
            "ppal-select": self._select,
            "ppal-live-api": self._live_api,
        }

    def validate_call(self, name, arguments):
        validate_call_values(name, arguments,
                             version=V22 if getattr(self, "_pp_depth", 0) else self.pp_version)

    def call_tool_sync(self, name, arguments):
        hook = self.before_call.get(len(self.calls) + 1)
        if hook is not None:
            hook(self)
        return super().call_tool_sync(name, arguments)

    # --- test hooks -----------------------------------------------------------

    def track(self, index):
        return self.track_records[index]

    def move_clip(self, track_index, clip_id, bars):
        """Move one arrangement clip by whole bars (song meter from the overview)."""
        from mcp_server import pp_codec
        numerator, denominator = self.overview["timeSignature"].split("/")
        bpb = int(numerator) * 4.0 / int(denominator)
        for clip in self.track_records[track_index].get("arrangementClips", []):
            if clip["id"] == clip_id:
                beats = pp_codec.bar_beat_to_beats(clip["arrangementStart"], bpb) + bars * bpb
                clip["arrangementStart"] = pp_codec.beats_to_bar_beat(beats, bpb)
                live = self.clip_live.get(clip_id)
                if live is not None:     # the same move, seen through the Live API
                    live["values"]["start_time"] += bars * bpb
                    live["values"]["end_time"] += bars * bpb
                return clip
        raise AssertionError("no arrangement clip %r on track %d" % (clip_id, track_index))

    def add_arrangement_clip(self, track_index, clip):
        self.track_records[track_index].setdefault("arrangementClips", []).append(dict(clip))
        for track in self.overview["tracks"]:
            if track["trackIndex"] == track_index:
                track["arrangementClipCount"] = track.get("arrangementClipCount", 0) + 1

    def rename_track(self, track_index, name):
        self.track_records[track_index]["name"] = name
        for track in self.overview["tracks"]:
            if track["trackIndex"] == track_index:
                track["name"] = name

    def set_playhead(self, beats):
        self.live_api_values["current_song_time"] = beats

    def set_selection(self, selection):
        self.selection = dict(selection)

    # --- handlers --------------------------------------------------------------

    def _read_live_set(self, args):
        include = set(args.get("include") or [])
        if "*" in include:
            include = {"tracks", "scenes", "routings", "mixer", "color", "locators"}
        payload = {key: value for key, value in self.overview.items()
                   if key not in ("tracks", "scenes", "returnTracks", "masterTrack", "locators")}
        def detail(track):
            drop = set()
            if "routings" not in include:
                drop.update(_OVERVIEW_ROUTING_KEYS)
            if "mixer" not in include:
                drop.update(_OVERVIEW_MIXER_KEYS)
            return {key: value for key, value in track.items() if key not in drop}

        if "tracks" in include:
            payload["tracks"] = [detail(track) for track in self.overview["tracks"]]
            payload["returnTracks"] = [detail(track) for track in self.overview["returnTracks"]]
            payload["masterTrack"] = detail(self.overview["masterTrack"])
        else:
            payload.update(regularTrackCount=len(self.overview["tracks"]),
                           returnTrackCount=len(self.overview["returnTracks"]))
        if "scenes" in include:
            payload["scenes"] = [dict(scene) for scene in self.overview["scenes"]]
        else:
            payload["sceneCount"] = len(self.overview["scenes"])
        if "locators" in include:
            payload["locators"] = [dict(item) for item in self.overview["locators"]]
        return json_response(json.loads(json.dumps(payload)))

    def _read_track(self, args):
        kind = args.get("trackType") or "regular"
        index = args.get("trackIndex", 0)
        wanted = sorted(args.get("include") or [])
        captured = self.track_includes.get((kind, index))
        if captured is None:
            return error_response(_error_text("mutate/error_missing_track.json")
                                  .replace("999", str(index)))
        notes_free = "notes" not in wanted
        if self.narrow_reads and kind == "regular" and sorted(
                wanted + (["notes"] if notes_free else [])) == _NARROW_TRACK_INCLUDE:
            record = self.track_records[index]
            projected = {key: value for key, value in record.items()
                         if key in _NARROW_TRACK_KEYS}
            if notes_free:
                projected = _without_clip_notes(projected)
            return json_response(json.loads(json.dumps(projected)))
        # The same read without "notes" is the capture minus every clip's notes
        # (the device omits a key it was not asked for).
        if notes_free and "notes" in captured and wanted == [i for i in captured
                                                              if i != "notes"]:
            record = _without_clip_notes(self.track_records[index]) if kind == "regular" \
                else None
            if record is not None:
                return json_response(json.loads(json.dumps(record)))
        if wanted != captured:
            raise AssertionError("read-track %s %s include %r was not captured (captured %r)"
                                 % (kind, index, wanted, captured))
        record = {"regular": self.track_records, "return": self.return_records,
                  "master": {0: self.master_record}}[kind][index]
        return json_response(json.loads(json.dumps(record)))

    def _read_clip(self, args):
        if not self.narrow_reads:
            raise AssertionError("read-clip was not captured (PieceDevice(narrow_reads=True) "
                                 "projects it from the track reads)")
        if sorted(args.get("include") or []) != ["notes", "timing"] or "id" not in args:
            raise AssertionError("read-clip %r is not a projectable read" % (args,))
        for record in self.track_records.values():
            for clip in record.get("arrangementClips") or []:
                if clip.get("id") == args["id"]:
                    return json_response(json.loads(json.dumps(clip)))
        return error_response("Error executing tool ppal-read-clip: no clip with id %s"
                              % args["id"])

    def _select(self, args):
        if args:
            raise AssertionError("PieceDevice replays only the read-only select {}")
        return json_response(dict(self.selection))

    def _live_api(self, args):
        if not self.live_api:
            raise AssertionError("ppal-live-api called while the Live API is off "
                                 "(the piece capture device had it off)")
        path = args.get("path")
        if isinstance(path, str) and path.startswith("id "):
            return self._live_api_clip(path[3:], args["operations"])
        if path != self.live_api_record["path"]:
            self.live_refusals.append(path)
            return error_response("Error executing tool ppal-live-api: live-api path %r "
                                  "was not captured" % (path,))
        results = []
        for operation in args["operations"]:
            prop = operation.get("property")
            if operation.get("type") != "getProperty" or prop not in self.live_api_values:
                raise AssertionError("live-api operation %r not captured" % operation)
            results.append({"operation": {"property": prop, "type": "getProperty"},
                            "result": self.live_api_values[prop]})
        return json_response({"id": self.live_api_record["id"],
                              "path": self.live_api_record["path"], "results": results})

    def _live_api_clip(self, clip_id, operations):
        live = self.clip_live.get(clip_id)
        if live is None:
            self.live_refusals.append("id " + clip_id)
            return error_response("Error executing tool ppal-live-api: no Live object "
                                  "with id %s was captured" % clip_id)
        results = []
        for operation in operations:
            kind = operation.get("type")
            if kind == "getProperty" and operation.get("property") in live["values"]:
                results.append({"operation": {"property": operation["property"],
                                              "type": "getProperty"},
                                "result": live["values"][operation["property"]]})
                continue
            args = operation.get("args")
            if (kind == "call" and operation.get("method") == "get_notes_extended"
                    and isinstance(args, list) and len(args) == 4
                    and all(isinstance(a, (int, float)) and not isinstance(a, bool)
                            for a in args)):
                pitch, pitch_span, start, span = args
                notes = [dict(note) for note in live["notes"]
                         if pitch <= note["pitch"] < pitch + pitch_span
                         and start <= note["start_time"] < start + span]
                results.append({"operation": {"args": list(args),
                                              "method": "get_notes_extended",
                                              "type": "call"},
                                "result": json.dumps({"notes": notes})})
                continue
            raise AssertionError("live-api operation %r on clip %s not captured"
                                 % (operation, clip_id))
        return json_response({"id": clip_id, "path": live["path"], "results": results})

    def pp_session(self, capabilities=None):
        from mcp_server import pp_session
        caps = {"supports_2_x": True, "supports_2_2": True, "producer_pal_2_2": True,
                "live_api": self.live_api, "ppal-live-api": self.live_api}
        caps.update(capabilities or {})
        return pp_session.PPSession(self, {"pp_version": self.pp_version, "capabilities": caps})

    def operation(self, capabilities=None):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def factory(*_args, **_kwargs):
            yield self.pp_session(capabilities)
        return factory
