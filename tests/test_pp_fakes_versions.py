"""The per-version fakes are faithful to the captures they claim.

The 2.2 -> 2.3 reshaper must turn each 2.2.0-rc1 capture into the shape of
the 2.3.0 capture of the SAME call on the same SCRATCH-capture Set (mutate/,
legacy/, read/select_current); the 2.3 -> 2.2 request translation must turn
each captured 2.3.0 request into arguments the 2.2.0-rc1 schemas accept.
"""
import asyncio
import json
import unittest

from mcp_server import pp_session as pp
from tests import pp_fakes
from tests.pp_fakes import V22, V23, V24


def _json(version, relative):
    record = pp_fakes.load_fixture(relative, version=version)
    for part in record["response"].get("content", []):
        if "json" in part:
            return part["json"]
    return None


def _shape(value, path="$", out=None):
    out = set() if out is None else out
    if isinstance(value, dict):
        for key, item in value.items():
            out.add(path + "." + key)
            _shape(item, path + "." + key, out)
    elif isinstance(value, list):
        for item in value:
            _shape(item, path + "[]", out)
    return out


# Keys a fake cannot reproduce without live device state (documented in pp_fakes).
_UNREPRODUCIBLE = {
    "mutate/delete_track.json": {"$.deletedPath"},
    "mutate/update_track_mute.json": {"$.path"},
    "mutate/update_track_unmute.json": {"$.path"},
}


class ReshaperMatchesPairedCapturesTests(unittest.TestCase):
    PAIRS = ("mutate/delete_track.json", "mutate/duplicate_clip_to_arrangement.json",
             "mutate/duplicate_track_without_clips.json", "mutate/playback_loop_off.json",
             "mutate/playback_update_arrangement.json", "mutate/read_live_set_default.json",
             "mutate/read_live_set_tracks_locators.json", "mutate/read_track_after_dup.json",
             "mutate/read_track_source.json", "mutate/update_track_mute.json",
             "mutate/update_track_unmute.json", "legacy/legacy_create_track.json",
             "legacy/legacy_live_set_after_create.json", "legacy/legacy_read_clip_id.json",
             "legacy/legacy_read_clip_path.json", "legacy/legacy_read_track.json",
             "read/select_current.json")

    def test_reshaped_22_replies_have_the_23_capture_shape(self):
        for relative in self.PAIRS:
            with self.subTest(fixture=relative):
                old = pp_fakes.load_fixture(relative, version=V22)
                new = _json(V23, relative)
                reshaped = pp_fakes.v22_payload_as_v23(old["tool"], old["arguments"],
                                                       _json(V22, relative))
                self.assertEqual(_shape(reshaped) | _UNREPRODUCIBLE.get(relative, set()),
                                 _shape(new))

    def test_reshaped_positions_equal_the_captured_23_paths(self):
        old = pp_fakes.load_fixture("mutate/read_track_after_dup.json", version=V22)
        reshaped = pp_fakes.v22_payload_as_v23(old["tool"], old["arguments"],
                                               _json(V22, "mutate/read_track_after_dup.json"))
        new = _json(V23, "mutate/read_track_after_dup.json")
        self.assertEqual(reshaped["path"], new["path"])
        self.assertEqual([c["path"] for c in reshaped["arrangementClips"]],
                         [c["path"] for c in new["arrangementClips"]])
        dup = pp_fakes.load_fixture("mutate/duplicate_clip_to_arrangement.json", version=V22)
        self.assertEqual(pp_fakes.v22_payload_as_v23(dup["tool"], dup["arguments"],
                                                     _json(V22, "mutate/duplicate_clip_to_arrangement.json"))["path"],
                         _json(V23, "mutate/duplicate_clip_to_arrangement.json")["path"])
        brace = pp_fakes.load_fixture("mutate/playback_update_arrangement.json", version=V22)
        self.assertEqual(pp_fakes.v22_payload_as_v23(brace["tool"], brace["arguments"],
                                                     _json(V22, "mutate/playback_update_arrangement.json")),
                         _json(V23, "mutate/playback_update_arrangement.json"))
        overview = pp_fakes.v22_payload_as_v23(
            "ppal-read-live-set", {}, _json(V22, "legacy/legacy_live_set_after_create.json"))
        captured = _json(V23, "legacy/legacy_live_set_after_create.json")
        self.assertEqual([t["path"] for t in overview["tracks"]],
                         [t["path"] for t in captured["tracks"]])
        self.assertEqual(overview["mainTrack"], captured["mainTrack"])
        self.assertEqual(overview["returnTracks"], captured["returnTracks"])

    def test_error_texts_match_the_23_captures(self):
        for relative in ("mutate/error_missing_track.json",
                         "legacy/legacy_read_clip_missing_id.json",
                         "legacy/legacy_read_clip_no_locator.json",
                         "legacy/legacy_read_clip_empty_slot.json"):
            with self.subTest(fixture=relative):
                old = pp_fakes.response_text(pp_fakes.load_fixture(relative, V22)["response"])
                new = pp_fakes.response_text(pp_fakes.load_fixture(relative, V23)["response"])
                self.assertEqual(pp_fakes.v22_error_as_v23(old), new)

    def test_reshaper_is_idempotent_on_23_captures(self):
        for relative in ("v23/v23_read_after_copies.json", "v23/v23_live_set_now.json",
                         "read/select_current.json", "v23/v23_brace_set.json"):
            with self.subTest(fixture=relative):
                record = pp_fakes.load_fixture(relative, version=V23)
                value = _json(V23, relative)
                self.assertEqual(pp_fakes.v22_payload_as_v23(
                    record["tool"], pp_fakes.v23_request_as_v22(
                        record["tool"], record["arguments"]), value), value)


class RequestTranslationTests(unittest.TestCase):
    def test_captured_23_requests_translate_to_valid_22_arguments(self):
        expected = {
            "v23/v23_read_after_create.json":
                {"trackIndex": 10, "include": ["arrangement-clips", "timing", "notes"]},
            "v23/v23_read_return_rt0.json":
                {"trackType": "return", "trackIndex": 0, "include": ["devices", "mixer"]},
            "v23/v23_read_main_mt.json": {"trackType": "master", "include": ["devices", "mixer"]},
            "v23/v23_create_track_midi.json": {"name": "CAPTURE 23 MIDI", "type": "midi"},
            "v23/v23_duplicate_trimmed.json":
                {"type": "clip", "id": "425", "toPath": "t10", "arrangementStart": "17|1",
                 "arrangementLength": "2bar"},
            "v23/v23_move_clip.json": {"id": "434", "arrangementStart": "49|1"},
            "v23/v23_split_clip.json": {"id": "446", "arrangementSplit": "59|1"},
            "v23/v23_delete_by_id.json": {"type": "clip", "id": "438"},
            "v23/v23_mute_by_id.json": {"id": "423", "mute": True},
            "v23/v23_brace_set.json": {"action": "update-arrangement", "loop": True,
                                       "loopStart": "5|1", "loopEnd": "9|1",
                                       "startTime": "5|1"},
        }
        for relative, args22 in expected.items():
            with self.subTest(fixture=relative):
                record = pp_fakes.load_fixture(relative, version=V23)
                translated = pp_fakes.v23_request_as_v22(record["tool"], record["arguments"])
                self.assertEqual(translated, args22)
                pp_fakes.validate_call_values(record["tool"], translated, version=V22)
        create = pp_fakes.load_fixture("v23/v23_create_clip_arrangement_path.json", version=V23)
        translated = pp_fakes.v23_request_as_v22(create["tool"], create["arguments"])
        self.assertEqual((translated["path"], translated["arrangementStart"]), ("t10", "1|1"))

    def test_23_only_forms_do_not_translate(self):
        for relative in ("v23/v23_delete_by_path.json", "v23/v23_unmute_by_path.json"):
            with self.subTest(fixture=relative):
                record = pp_fakes.load_fixture(relative, version=V23)
                with self.assertRaisesRegex(AssertionError, "no 2.2 form"):
                    pp_fakes.v23_request_as_v22(record["tool"], record["arguments"])
        with self.assertRaisesRegex(AssertionError, "no 2.2 bar|beat"):
            pp_fakes.v23_request_as_v22("ppal-duplicate", {"type": "clip", "id": "1",
                                                           "toPath": "t2[loc:Chorus]"})


@pp_fakes.on_all_versions
class TwinnedFakeTests(unittest.TestCase):
    def test_fakes_speak_the_class_version(self):
        self.assertIn(self.PP_VERSION, (V22, V23, V24))
        self.assertEqual(pp_fakes.active_version(), self.PP_VERSION)
        self.assertEqual(pp_fakes.CapturedDevice().pp_version, self.PP_VERSION)
        self.assertEqual(pp_fakes.PieceDevice().pp_version, self.PP_VERSION)
        self.assertEqual(pp_fakes.SchemaStrictSession().pp_version, self.PP_VERSION)

    def test_captured_device_answers_in_its_builds_shapes(self):
        device = pp_fakes.CapturedDevice()
        overview = pp_fakes.response_text(device.call_tool_sync(
            "ppal-read-live-set", {"include": ["tracks"]}))
        overview = json.loads(overview)
        created = json.loads(pp_fakes.response_text(device.call_tool_sync(
            "ppal-create-track", pp.PPRequests(pp.addressing_for_version(
                self.PP_VERSION)).create_track())))
        if self.PP_VERSION in pp_fakes.PATH_VERSIONS:
            self.assertTrue(all("path" in t and "trackIndex" not in t for t in overview["tracks"]))
            self.assertIn("mainTrack", overview)
            self.assertEqual(created, {"id": created["id"], "path": "t11"})
            self.assertEqual(device.wire_calls[-1], ("ppal-create-track", {"path": "t+"}))
            self.assertEqual(device.calls[-1], ("ppal-create-track", {}))
            with self.assertRaisesRegex(AssertionError, "unknown arguments.*trackIndex"):
                device.call_tool_sync("ppal-read-track", {"trackIndex": 11})
        else:
            self.assertTrue(all("trackIndex" in t for t in overview["tracks"]))
            self.assertEqual(created, {"id": created["id"], "trackIndex": 11})
        read = json.loads(pp_fakes.response_text(device.call_tool_sync(
            "ppal-read-track", pp.PPRequests(pp.addressing_for_version(
                self.PP_VERSION)).read_track(11, include=[]))))
        self.assertEqual(pp.track_index_of(read), 11)

    def test_piece_device_answers_in_its_builds_shapes(self):
        device = pp_fakes.PieceDevice()
        requests = pp.PPRequests(pp.addressing_for_version(self.PP_VERSION))
        include = pp_fakes.load_fixture("piece/piece_track_01.json")["arguments"]["include"]
        read = json.loads(pp_fakes.response_text(device.call_tool_sync(
            "ppal-read-track", requests.read_track(1, include=include))))
        starts = [pp.arrangement_start_of(clip) for clip in read["arrangementClips"]]
        self.assertEqual(starts[0], "1|1")
        self.assertEqual(pp.track_index_of(read), 1)
        if self.PP_VERSION in pp_fakes.PATH_VERSIONS:
            self.assertEqual(read["arrangementClips"][0]["path"], "t1[1|1]")
            self.assertNotIn("arrangementStart", read["arrangementClips"][0])
            with self.assertRaisesRegex(AssertionError, "unknown arguments"):
                device.call_tool_sync("ppal-read-track", {"trackIndex": 1, "include": include})

    def test_schema_strict_session_level_fake_reshapes_its_replies(self):
        @pp_fakes.schema_strict
        class Fake:
            async def call(self, name, args):
                self.seen = dict(args)
                return pp.PPResponse(json.dumps({"result": {"id": "7", "trackIndex": 3}}),
                                     {"id": "7", "trackIndex": 3}, [])

        fake = Fake()
        requests = pp.requests_for(fake)
        reply = asyncio.run(fake.call("ppal-read-track", requests.read_track(3, include=[])))
        self.assertEqual(fake.seen, {"trackIndex": 3, "include": []})
        self.assertEqual(pp.track_index_of(reply.result), 3)
        if self.PP_VERSION in pp_fakes.PATH_VERSIONS:
            self.assertEqual(reply.result, {"id": "7", "path": "t3"})
            self.assertEqual(fake.wire_calls, [("ppal-read-track", {"path": "t3", "include": []})])


class TwinRegistrationTests(unittest.TestCase):
    def test_twin_class_is_registered_for_discovery(self):
        twin = globals().get("TwinnedFakeTestsPP23")
        self.assertIsNotNone(twin)
        self.assertEqual(twin.PP_VERSION, V23)
        self.assertEqual(TwinnedFakeTests.PP_VERSION, V22)
        self.assertEqual(pp_fakes.active_version(), V22)
        twin24 = globals().get("TwinnedFakeTestsPP24")
        self.assertIsNotNone(twin24)
        self.assertEqual(twin24.PP_VERSION, V24)


# --- Producer Pal 2.4.0: the 2.4 captures of the SAME calls on the same Set ------
#
# tests/fixtures/pp/2.4.0/{read,mutate,legacy,v23,v23b} repeat the 2.2/2.3
# capture runs on the 2.4.0 device; the 2.2 -> 2.4 reshaper must produce the
# 2.4 shape of each, and the 2.4 -> 2.2 request translation must turn each
# captured 2.4 request (the new batch forms included) into 2.2 arguments.

# Keys a fake cannot reproduce without live device state.
_UNREPRODUCIBLE_24 = {
    "mutate/delete_track.json": {"$.deletedPath"},
    "mutate/update_track_mute.json": {"$.path"},
    "mutate/update_track_unmute.json": {"$.path"},
    # 2.4 lists arrangementLength on every arrangement clip (read_track_after_dup)
    "mutate/read_track_after_dup.json": {"$.arrangementClips[].arrangementLength"},
}


class Reshaper24MatchesPairedCapturesTests(unittest.TestCase):
    PAIRS = ReshaperMatchesPairedCapturesTests.PAIRS

    def test_reshaped_22_replies_have_the_24_capture_shape(self):
        for relative in self.PAIRS:
            with self.subTest(fixture=relative):
                old = pp_fakes.load_fixture(relative, version=V22)
                new = _json(V24, relative)
                reshaped = pp_fakes.v22_payload_as_v24(old["tool"], old["arguments"],
                                                       _json(V22, relative))
                self.assertEqual(_shape(reshaped) | _UNREPRODUCIBLE_24.get(relative, set()),
                                 _shape(new))

    def test_24_deltas_from_23(self):
        # deletes: {deletedPath, id} — no deleted / type (mutate/delete_track, v23_delete_by_id)
        old = pp_fakes.load_fixture("mutate/delete_track.json", version=V22)
        reshaped = pp_fakes.v22_payload_as_v24(old["tool"], old["arguments"],
                                               _json(V22, "mutate/delete_track.json"))
        self.assertEqual(set(reshaped) | {"deletedPath"},
                         set(_json(V24, "mutate/delete_track.json")))
        self.assertNotIn("deleted", reshaped)
        # a delete that deleted nothing: {detail: "nothing to delete", id}, no warning
        # (v23/v23_delete_missing_id on the 2.4 run == v24/v24_delete_clip_missing_id)
        missed = pp_fakes.v22_payload_as_v24(
            "ppal-delete", {"type": "clip", "id": "999999"},
            {"deleted": False, "id": "999999", "type": "clip"})
        self.assertEqual(missed, _json(V24, "v23/v23_delete_missing_id.json"))
        self.assertEqual(missed, _json(V24, "v24/v24_delete_clip_missing_id.json"))
        # playback echoes nothing: no startTime (mutate/playback_update_arrangement,
        # v23/v23_brace_set), no scene.name (v23b/v23b_play_scene_by_path)
        brace = pp_fakes.load_fixture("mutate/playback_update_arrangement.json", version=V22)
        self.assertEqual(pp_fakes.v22_payload_as_v24(
            brace["tool"], brace["arguments"],
            _json(V22, "mutate/playback_update_arrangement.json")),
            _json(V24, "mutate/playback_update_arrangement.json"))
        # (2.2 captured no play-scene reply; the delta is between the 2.3 and 2.4 captures)
        played = pp_fakes.v22_payload_as_v24(
            "ppal-playback", {"action": "play-scene", "sceneIndex": 8},
            _json(V23, "v23b/v23b_play_scene_by_path.json"))
        self.assertEqual(_shape(played), _shape(_json(V24, "v23b/v23b_play_scene_by_path.json")))
        self.assertNotIn("name", played["scene"])

    def test_error_texts_match_the_24_captures(self):
        for relative in ("mutate/error_missing_track.json",
                         "legacy/legacy_read_clip_missing_id.json",
                         "legacy/legacy_read_clip_no_locator.json",
                         "legacy/legacy_read_clip_empty_slot.json"):
            with self.subTest(fixture=relative):
                old = pp_fakes.response_text(pp_fakes.load_fixture(relative, V22)["response"])
                new = pp_fakes.response_text(pp_fakes.load_fixture(relative, V24)["response"])
                self.assertEqual(pp_fakes.v22_error_as_v24(old), new)

    def test_reshaper_is_idempotent_on_24_captures(self):
        for relative in ("v24/v24_read_track_after_clip_ops.json", "v24/v24_live_set_before.json",
                         "v24/v24_selection_readback.json", "v24/v24_delete_capture_tracks.json",
                         "v24/v24_play_scene_by_path.json", "v24/v24_stop.json"):
            with self.subTest(fixture=relative):
                rec = pp_fakes.load_fixture(relative, version=V24)
                value = _json(V24, relative)
                self.assertEqual(pp_fakes.v22_payload_as_v24(
                    rec["tool"], pp_fakes.v24_request_as_v22(
                        rec["tool"], rec["arguments"]), value), value)


class RequestTranslation24Tests(unittest.TestCase):
    def test_captured_24_requests_translate_to_valid_22_arguments(self):
        expected = {
            "v24/v24_create_tracks_batch.json":
                {"count": 2, "type": "midi", "name": "CAPTURE 24 A,CAPTURE 24 B"},
            "v24/v24_create_scenes_batch.json":
                {"count": 2, "capture": False, "name": "CAP24 S1,CAP24 S2"},
            "v24/v24_read_track_devices.json": {"trackIndex": 10, "include": ["devices"]},
            "v24/v24_duplicate_trimmed.json":
                {"type": "clip", "id": "551", "toPath": "t10", "arrangementStart": "13|1",
                 "arrangementLength": "1bar"},
            "v24/v24_move_clip.json": {"id": "551", "arrangementStart": "17|1"},
            "v24/v24_split_clip.json": {"id": "552", "arrangementSplit": "6|1"},
            "v24/v24_delete_clips_batch_one_bad.json": {"type": "clip", "id": "552,999998"},
            "v24/v24_play_scene_by_path.json": {"action": "play-scene", "sceneIndex": 10},
            "v24/v24_create_arrangement_clip.json":
                {"path": "t10", "arrangementStart": "1|1", "length": "2bar", "looping": True,
                 "name": "M1",
                 "notes": pp_fakes.load_fixture("v24/v24_create_arrangement_clip.json",
                                                V24)["arguments"]["notes"]},
        }
        for relative, args22 in expected.items():
            with self.subTest(fixture=relative):
                rec = pp_fakes.load_fixture(relative, version=V24)
                translated = pp_fakes.v24_request_as_v22(rec["tool"], rec["arguments"])
                self.assertEqual(translated, args22)
                pp_fakes.validate_call_values(rec["tool"], translated, version=V22)

    def test_24_only_forms_do_not_translate(self):
        for relative in ("v24/v24_delete_clip_by_path.json", "v24/v24_update_track_minimised.json",
                         "v24/v24_read_tracks_batch.json", "v24/v24_lane_append.json"):
            with self.subTest(fixture=relative):
                rec = pp_fakes.load_fixture(relative, version=V24)
                with self.assertRaisesRegex(AssertionError, "no 2.2 form|not translated"):
                    pp_fakes.v24_request_as_v22(rec["tool"], rec["arguments"])


class CapturedDevice24Tests(unittest.TestCase):
    """The 2.4 twin of CapturedDevice answers in the 2.4 captured shapes."""

    def test_deletes_reply_deletedPath_only_and_a_miss_says_nothing_to_delete(self):
        device = pp_fakes.CapturedDevice(version=V24)
        clip = device.add_clip("t0/s1", "x", "[]")
        gone = json.loads(pp_fakes.response_text(device.call_tool_sync(
            "ppal-delete", {"type": "clip", "id": clip["id"]})))
        self.assertEqual(gone, {"deletedPath": "t0/s1", "id": clip["id"]})   # v23_delete_by_id (2.4)
        missed = device.call_tool_sync("ppal-delete", {"type": "clip", "id": "999999"})
        self.assertFalse(missed["is_error"])
        self.assertEqual(json.loads(pp_fakes.response_text(missed)),
                         _json(V24, "v24/v24_delete_clip_missing_id.json"))
        self.assertEqual([p for p in missed["content"] if "text" in p], [])

    def test_an_empty_slot_read_by_path_is_an_error_on_24(self):
        device = pp_fakes.CapturedDevice(version=V24)
        reply = device.call_tool_sync("ppal-read-clip", {"path": "t0/s7"})
        self.assertTrue(reply["is_error"])
        captured = pp_fakes.load_fixture("legacy/check_read_clip_existing_empty_slot.json", V24)
        self.assertEqual(reply["content"][0]["text"],
                         captured["response"]["content"][0]["text"].replace("t10/s5", "t0/s7"))

    def test_a_create_into_an_occupied_slot_replaces_and_says_overwrote(self):
        device = pp_fakes.CapturedDevice(version=V24)
        notes = pp_fakes.load_fixture("v24/v24_create_session_clip.json", V24)["arguments"]["notes"]
        first = json.loads(pp_fakes.response_text(device.call_tool_sync(
            "ppal-create-clip", {"path": "t0/s0", "name": "S0", "notes": notes})))
        again = json.loads(pp_fakes.response_text(device.call_tool_sync(
            "ppal-create-clip", {"path": "t0/s0", "name": "S0 again", "notes": notes})))
        captured = _json(V24, "v24/v24_create_session_clip_replaces.json")
        self.assertEqual(set(again), set(captured))
        self.assertEqual(again["detail"], captured["detail"].replace("t10/s0", "t0/s0"))
        self.assertNotEqual(again["id"], first["id"])
        self.assertEqual(device.clips["t0/s0"]["name"], "S0 again")
        # v24_delete_clip_by_id_already_replaced: the replaced clip's id deletes nothing
        self.assertEqual(json.loads(pp_fakes.response_text(device.call_tool_sync(
            "ppal-delete", {"type": "clip", "id": first["id"]}))),
            {"detail": "nothing to delete", "id": first["id"]})
        for version in (V22, V23):
            with self.subTest(version=version), \
                    self.assertRaisesRegex(AssertionError, "not captured"):
                other = pp_fakes.CapturedDevice(version=version)
                other.call_tool_sync("ppal-create-clip", {"path": "t0/s0", "notes": "[]"})
                other.call_tool_sync("ppal-create-clip", {"path": "t0/s0", "notes": "[]"})

    def test_batch_creates_reply_lists(self):
        device = pp_fakes.CapturedDevice(version=V24)
        tracks = json.loads(pp_fakes.response_text(device.call_tool_sync(
            "ppal-create-track", {"path": "t+,t+", "type": "midi", "name": "A,B"})))
        self.assertEqual([t["path"] for t in tracks], ["t11", "t12"])
        self.assertEqual([t["name"] for t in device.tracks[-2:]], ["A", "B"])
        self.assertEqual(_shape(tracks), _shape(_json(V24, "v24/v24_create_tracks_batch.json")))
        scenes = json.loads(pp_fakes.response_text(device.call_tool_sync(
            "ppal-create-scene", {"path": "s+,s+", "capture": False})))
        self.assertEqual(_shape(scenes), _shape(_json(V24, "v24/v24_create_scenes_batch.json")))
        self.assertEqual([s["path"] for s in scenes], ["s8", "s9"])
        self.assertEqual(device.live_set["sceneCount"], 10)
        with self.assertRaisesRegex(AssertionError, "unknown arguments.*count"):
            device.call_tool_sync("ppal-create-scene", {"path": "s+", "count": 1})

    def test_live_api_on_a_missing_id_replies_the_captured_result_less_shape(self):
        device = pp_fakes.CapturedDevice(version=V24)
        rec = pp_fakes.load_fixture("v24/v24_live_api_missing_id.json", V24)
        reply = device.call_tool_sync("ppal-live-api", rec["arguments"])
        self.assertEqual(json.loads(pp_fakes.response_text(reply)), _json(V24, "v24/v24_live_api_missing_id.json"))
        with self.assertRaisesRegex(AssertionError, "not captured"):
            pp_fakes.CapturedDevice(version=V23).call_tool_sync("ppal-live-api", rec["arguments"])


if __name__ == "__main__":
    unittest.main()
