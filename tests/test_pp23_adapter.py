"""Producer Pal 2.3.0 next to 2.2.0-rc1: handshake, request forms, warnings,
failure receipts and response normalizers.

Every 2.3 expectation replays a real device capture under
tests/fixtures/pp/2.3.0 (SCRATCH-capture, commit fa11143); every 2.2 one a
capture under tests/fixtures/pp/2.2.0-rc1. Request forms without a capture
say so and are checked against the captured tool schemas only.
"""
import asyncio
import json
import unittest
from types import SimpleNamespace

from mcp_server import pp_codec
from mcp_server import pp_session as pp
from tests import pp_fakes
from tests.pp_fakes import V22, V23


def record(version, relative):
    return pp_fakes.load_fixture(relative, version=version)


def payload(version, relative):
    return pp_fakes.captured_json(relative, version=version)


def response(version, relative):
    """The captured MCP result replayed through the real pp_session.call_tool."""
    rec = record(version, relative)

    class Raw:
        async def call_tool(self, name, arguments):
            return pp_fakes.mcp_result(rec["response"])

    return asyncio.run(pp.call_tool(Raw(), rec["tool"], rec["arguments"]))


def args_of(version, relative):
    return record(version, relative)["arguments"]


class HandshakeAndAddressingTests(unittest.TestCase):
    def test_supported_builds_are_the_captured_versions(self):
        self.assertEqual(pp.SUPPORTED_PP_VERSIONS, frozenset({V22, V23, "2.4.0"}))

    def test_connect_captures_validate_and_name_their_addressing(self):
        for version, addressing in ((V22, "2.2"), (V23, "2.3")):
            with self.subTest(version=version):
                rec = record(version, "read/connect.json")
                tools = sorted(pp_fakes.schemas_for(version))
                handshake = pp.validate_handshake(pp.handshake_from_connect(
                    pp.parse_tool_response(pp_fakes.response_text(rec["response"])),
                    tools, requested={"notation": pp.PP_NOTATION}))
                self.assertEqual(handshake["pp_version"], version)
                caps = handshake["capabilities"]
                self.assertEqual(caps["addressing"], addressing)
                self.assertTrue(caps["supports_2_2"])
                self.assertEqual(caps["supports_2_3"], version == V23)
                self.assertTrue(caps["live_api"])

    def test_uncaptured_builds_fail_the_guard(self):
        for version in ("2.2.0", "2.3.1", "2.4.1", "2.1.0"):
            with self.subTest(version=version):
                handshake = pp.handshake_from_connect(pp.parse_tool_response(
                    '{"result":{"producerPalVersion":"%s"}}' % version))
                with self.assertRaisesRegex(pp.PPProtocolError, "not verified"):
                    pp.validate_handshake(handshake)

    def test_addressing_of_reads_capability_then_handshake_then_defaults_to_22(self):
        self.assertEqual(pp.addressing_of(pp.PPSession(object(), {"pp_version": V23})), "2.3")
        self.assertEqual(pp.addressing_of(pp.PPSession(object(), {"pp_version": V22})), "2.2")
        self.assertEqual(pp.addressing_of(pp.PPSession(
            object(), {"pp_version": V22, "capabilities": {"addressing": "2.3"}})), "2.3")
        # Older test doubles carry neither: the 2.2 forms they always received.
        self.assertEqual(pp.addressing_of(object()), "2.2")
        self.assertEqual(pp.addressing_of(SimpleNamespace(pp_version=V23)), "2.3")
        self.assertEqual(pp.PPSession(object(), {"pp_version": V23}).pp_version, V23)


class WarningAndFailureTests(unittest.TestCase):
    def test_call_tool_keeps_device_warning_parts(self):
        missing = response(V23, "v23/v23_delete_missing_id.json")
        self.assertEqual(missing.result, {"deleted": False, "id": "999999", "type": "clip"})
        self.assertEqual(missing.warnings, ['WARNING: id "999999" does not exist'])
        refused = response(V23, "v23/v23_duplicate_midi_to_audio.json")
        self.assertEqual(refused.result, [])
        self.assertIn("was not duplicated", refused.warnings[0])
        # 2.2 also sends warnings as extra parts (existing empty slot).
        empty = response(V22, "legacy/check_read_clip_existing_empty_slot.json")
        self.assertEqual(empty.warnings, ["WARNING: no clip at t10/s5"])

    def test_non_warning_text_parts_are_not_warnings(self):
        # connect.json carries skills/context text parts after the JSON.
        self.assertEqual(response(V22, "read/connect.json").warnings, [])
        self.assertEqual(response(V23, "read/connect.json").warnings, [])

    def test_tool_error_keeps_its_text_and_carries_warnings(self):
        with self.assertRaises(pp.PPToolError) as caught:
            response(V23, "mutate/error_missing_track.json")
        self.assertEqual(str(caught.exception),
                         "Producer Pal tool error: Error: trackIndex 999 does not exist")
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assertIn("deprecated", caught.exception.warnings[0])

    def test_write_failed_names_captured_refusals(self):
        # judged by structure ([] / deleted:false); the warning rides along
        refused = pp.write_failed(response(V23, "v23/v23_duplicate_midi_to_audio.json"))
        self.assertIn("no object", refused)
        self.assertIn("was not duplicated", refused)
        missing = pp.write_failed(response(V23, "v23/v23_delete_missing_id.json"),
                                  expect="deleted")
        self.assertIn("does not exist", missing)
        self.assertIsNotNone(pp.write_failed(
            pp.parse_tool_response('{"result":{"deleted":false,"id":"9","type":"clip"}}'),
            expect="deleted"))

    def test_write_failed_accepts_captured_successes(self):
        for version, relative, expect in (
                (V23, "v23/v23_duplicate_to_position.json", "object"),
                (V23, "v23/v23_create_track_midi.json", "object"),
                (V23, "v23/v23_create_clip_arrangement_path.json", "object"),
                (V23, "v23/v23_move_clip.json", "object"),
                (V23, "v23/v23_mute_by_id.json", "object"),
                (V23, "v23/v23_split_clip.json", "object"),
                (V23, "v23/v23_delete_by_id.json", "deleted"),
                (V23, "v23/v23_delete_by_path.json", "deleted"),
                (V23, "v23/v23_delete_capture_tracks.json", "deleted"),
                (V22, "mutate/duplicate_clip_to_arrangement.json", "object"),
                (V22, "mutate/delete_track.json", "deleted"),
                (V22, "legacy/legacy_create_track.json", "object"),
                (V22, "legacy/check_create_clip_arrangement.json", "object")):
            with self.subTest(fixture=relative):
                self.assertIsNone(pp.write_failed(response(version, relative), expect=expect))

    def test_deprecation_warnings_are_not_failures(self):
        # The write happened; the strict fakes are what forbid the old forms.
        for relative in ("v23/v23_legacy_create_arrangementStart.json",
                         "v23/v23_legacy_duplicate_arrangementStart.json"):
            with self.subTest(fixture=relative):
                reply = response(V23, relative)
                self.assertIn("deprecated", reply.warnings[0])
                self.assertIsNone(pp.write_failed(reply))

    def test_write_failed_refuses_empty_or_idless_results(self):
        for text in ('{"result":[]}', '{"result":{}}', '{"result":null}',
                     '{"result":[{"path":"t1"}]}'):
            with self.subTest(text=text):
                self.assertIsNotNone(pp.write_failed(pp.parse_tool_response(text)))
        self.assertIsNotNone(pp.write_failed(
            pp.parse_tool_response('{"result":[{"id":"1"}]}'), count=2))
        for removed in ("bogus", "objects", "any"):   # no warning-only judgement mode
            with self.assertRaises(ValueError):
                pp.write_failed(pp.parse_tool_response('{"result":{}}'), expect=removed)

    def test_missing_id_and_empty_slot_errors_on_both_versions(self):
        texts = {version: pp_fakes.response_text(record(
            version, "legacy/legacy_read_clip_missing_id.json")["response"])
            for version in (V22, V23)}
        for version, text in texts.items():
            with self.subTest(version=version):
                self.assertTrue(pp.is_missing_id_error(
                    "Producer Pal tool error: " + text, "999999"))
                self.assertFalse(pp.is_missing_id_error(
                    "Producer Pal tool error: " + text, "99999"))
                scene = pp_fakes.response_text(record(
                    version, "legacy/legacy_read_clip_empty_slot.json")["response"])
                self.assertTrue(pp_codec.is_empty_slot_error(
                    "Producer Pal tool error: " + scene))


class NormalizerTests(unittest.TestCase):
    def test_track_indexes_from_trackIndex_or_path(self):
        for version in (V22, V23):
            with self.subTest(version=version):
                overview = payload(version, "legacy/legacy_live_set_after_create.json")
                indexes = [pp.track_index_of(track) for track in overview["tracks"]]
                self.assertEqual(indexes, list(range(len(overview["tracks"]))))
                self.assertEqual([pp.return_index_of(track)
                                  for track in overview["returnTracks"]], [0, 1])
                self.assertEqual(pp.main_track_of(overview)["id"], "105")
        self.assertIsNone(pp.track_index_of({"path": "t3/s0"}))
        self.assertIsNone(pp.track_index_of({"path": "rt0"}))
        self.assertIsNone(pp.track_index_of({"trackIndex": True}))
        self.assertEqual(pp.track_index_of(payload(V23, "v23/v23_read_after_create.json")), 10)
        self.assertEqual(pp.return_index_of(payload(V23, "v23/v23_read_return_rt0.json")), 0)
        self.assertTrue(pp.is_main_track(payload(V23, "v23/v23_read_main_mt.json")))

    def test_arrangement_start_from_arrangementStart_or_clip_path(self):
        copies = payload(V23, "v23/v23_read_after_copies.json")["arrangementClips"]
        self.assertEqual([pp.arrangement_start_of(clip) for clip in copies],
                         ["1|1", "9|1", "17|1", "25|1"])
        self.assertEqual({pp.clip_track_index(clip) for clip in copies}, {10})
        old = payload(V22, "mutate/read_track_after_dup.json")["arrangementClips"][0]
        self.assertEqual(pp.arrangement_start_of(old), "5|1")
        self.assertEqual(pp.arrangement_start_of({"path": "t2/l0[3|2.5]"}), "3|2.5")
        # A locator position names no bar|beat; a session slot has no start.
        self.assertIsNone(pp.arrangement_start_of({"path": "t2[loc:Chorus]"}))
        self.assertIsNone(pp.arrangement_start_of({"path": "t2/s0"}))
        self.assertTrue(pp_codec.is_lane_object_path("t2/l0"))
        self.assertTrue(pp_codec.is_lane_object_path("t2/l0[3|2.5]"))
        self.assertFalse(pp_codec.is_lane_object_path("t2[3|1]"))

    def test_scene_indexes_and_selection_from_paths(self):
        for version in (V22, V23):
            with self.subTest(version=version):
                selection = pp.extract_selected_context(pp.parse_tool_response(
                    json.dumps({"result": payload(version, "read/select_current.json")})))
                self.assertEqual(selection["selected_track"]["track_index"], 0)
                self.assertEqual(selection["selected_scene"]["scene_index"], 0)
        self.assertEqual(pp.scene_index_of({"path": "t3/s7"}), 7)
        self.assertEqual(pp.scene_index_of({"path": "s4"}), 4)
        self.assertEqual(pp.scene_index_of({"sceneIndex": 2}), 2)
        self.assertIsNone(pp.scene_index_of({"path": "t3"}))

    def test_result_objects_flatten_dict_list_and_nested_replies(self):
        self.assertEqual(pp.result_objects(response(V23, "v23/v23_create_track_midi.json")),
                         [{"id": "423", "path": "t10"}])
        split = pp.result_objects(response(V23, "v23/v23_split_clip.json"))
        self.assertEqual([item["id"] for item in split], ["446", "450"])
        self.assertEqual(len(pp.result_objects(
            response(V23, "v23/v23_delete_capture_tracks.json"))), 2)
        self.assertEqual(pp.result_objects(pp.parse_tool_response(
            '{"result":[[{"id":"a"},{"id":"b"}]]}')), [{"id": "a"}, {"id": "b"}])
        self.assertEqual(pp.result_objects(
            response(V23, "v23/v23_duplicate_midi_to_audio.json")), [])

    def test_created_track_index_from_either_reply(self):
        self.assertEqual(pp.created_track(response(V22, "legacy/legacy_create_track.json")),
                         {"id": "369", "track_index": 10})
        self.assertEqual(pp.created_track(response(V23, "v23/v23_create_track_audio.json")),
                         {"id": "424", "track_index": 11})
        self.assertEqual(pp.created_track(response(V23, "mutate/duplicate_track_without_clips.json")),
                         {"id": "470", "track_index": 10})
        self.assertIsNone(pp.created_track(pp.parse_tool_response('{"result":[]}')))

    def test_playback_reply_without_current_time_claims_no_playhead(self):
        brace = pp.normalize_playback(payload(V23, "v23/v23_brace_set.json"))
        self.assertIsNone(brace["playhead"])
        self.assertEqual(brace["start_time"], "5|1")
        self.assertIsNone(brace["loop"]["on"])
        old = pp.normalize_playback(payload(V22, "mutate/playback_update_arrangement.json"))
        self.assertEqual(old["playhead"], "5|1")
        self.assertEqual(old["loop"], {"on": True, "start": "5|1", "end": "9|1"})

    def test_delete_receipts_from_both_versions(self):
        by_path = pp.normalize_delete(payload(V23, "v23/v23_delete_by_path.json"))
        self.assertTrue(by_path["deleted"])
        self.assertEqual(by_path["track_index"], 10)
        self.assertEqual(by_path["arrangement_start"], "49|1")
        self.assertFalse(pp.normalize_delete(payload(V23, "v23/v23_delete_missing_id.json"))["deleted"])
        tracks = pp.extract_delete_receipt(response(V23, "v23/v23_delete_capture_tracks.json"))
        self.assertEqual(tracks["deleted_ids"], ["423", "424"])
        missing = pp.extract_delete_receipt(response(V23, "v23/v23_delete_missing_id.json"))
        self.assertEqual(missing["deleted_ids"], [])
        self.assertEqual(missing["warnings"], ['WARNING: id "999999" does not exist'])
        old = pp.extract_delete_receipt(response(V22, "mutate/delete_track.json"))
        self.assertEqual(old["deleted_ids"], ["391"])


class RequestBuilderTests(unittest.TestCase):
    """ONE builder emits each version's form; 2.3 forms equal the captured literals."""

    def assertForm(self, version, tool, built, expected):
        self.assertEqual(built, expected)
        pp_fakes.validate_call_values(tool, built, version=version)

    def test_read_track_forms(self):
        v22, v23 = pp.PPRequests("2.2"), pp.PPRequests("2.3")
        include = ["arrangement-clips", "timing", "notes"]
        self.assertForm(V23, "ppal-read-track", v23.read_track(10, include=include),
                        args_of(V23, "v23/v23_read_after_create.json"))
        self.assertForm(V22, "ppal-read-track",
                        v22.read_track(10, include=["arrangement-clips", "notes"]),
                        args_of(V22, "mutate/read_track_after_dup.json"))
        self.assertForm(V23, "ppal-read-track",
                        v23.read_track(0, kind="return", include=["devices", "mixer"]),
                        args_of(V23, "v23/v23_read_return_rt0.json"))
        self.assertForm(V23, "ppal-read-track",
                        v23.read_track(kind="main", include=["devices", "mixer"]),
                        args_of(V23, "v23/v23_read_main_mt.json"))
        piece_return = pp_fakes.load_fixture("piece/piece_return_00.json")["arguments"]
        self.assertForm(V22, "ppal-read-track",
                        v22.read_track(0, kind="return", include=piece_return["include"]),
                        piece_return)
        piece_master = pp_fakes.load_fixture("piece/piece_master.json")["arguments"]
        self.assertForm(V22, "ppal-read-track",
                        v22.read_track(kind="main", include=piece_master["include"]),
                        piece_master)
        self.assertEqual(v23.read_track(track_id="423"), {"id": "423"})

    def test_read_device_form_is_an_id_locator_on_both_builds(self):
        for version, requests in ((V22, pp.PPRequests("2.2")), (V23, pp.PPRequests("2.3"))):
            self.assertForm(version, "ppal-read-device",
                            requests.read_device("2593", include=["params", "param-values"]),
                            {"id": "2593", "include": ["params", "param-values"]})
            self.assertForm(version, "ppal-read-device", requests.read_device(2617),
                            {"id": "2617"})

    def test_create_track_append_forms(self):
        self.assertForm(V23, "ppal-create-track",
                        pp.PPRequests("2.3").create_track(type="midi", name="CAPTURE 23 MIDI"),
                        args_of(V23, "v23/v23_create_track_midi.json"))
        self.assertForm(V22, "ppal-create-track",
                        pp.PPRequests("2.2").create_track(name="SIDEKICK TRY legacy"),
                        args_of(V22, "legacy/legacy_create_track.json"))

    def test_create_clip_destination_forms(self):
        notes = args_of(V23, "v23/v23_create_clip_arrangement_path.json")["notes"]
        self.assertForm(V23, "ppal-create-clip",
                        pp.PPRequests("2.3").create_clip(
                            track=10, arrangement_start="1|1", length="4bar", looping=True,
                            name="A", notes=notes),
                        args_of(V23, "v23/v23_create_clip_arrangement_path.json"))
        old = args_of(V22, "legacy/check_create_clip_arrangement.json")
        self.assertForm(V22, "ppal-create-clip",
                        pp.PPRequests("2.2").create_clip(
                            track=10, arrangement_start="3|1", length="1bar",
                            notes=old["notes"]), old)
        slot = args_of(V22, "legacy/legacy_create_clip_path.json")
        for version, addressing in ((V22, "2.2"), (V23, "2.3")):
            self.assertForm(version, "ppal-create-clip", pp.PPRequests(addressing).create_clip(
                track=10, scene=1, length="1bar", notes=slot["notes"]), slot)

    def test_duplicate_forms(self):
        self.assertForm(V23, "ppal-duplicate",
                        pp.PPRequests("2.3").duplicate_clip_to_arrangement(
                            "425", track=10, start="17|1", length="2bar"),
                        args_of(V23, "v23/v23_duplicate_trimmed.json"))
        self.assertForm(V23, "ppal-duplicate",
                        pp.PPRequests("2.3").duplicate_clip_to_arrangement(
                            "425", track=10, start="9|1"),
                        args_of(V23, "v23/v23_duplicate_to_position.json"))
        self.assertForm(V22, "ppal-duplicate",
                        pp.PPRequests("2.2").duplicate_clip_to_arrangement(
                            "359", track=10, start="5|1", length="2bar"),
                        args_of(V22, "mutate/duplicate_clip_to_arrangement.json"))
        for version, addressing in ((V22, "2.2"), (V23, "2.3")):
            self.assertForm(version, "ppal-duplicate", pp.PPRequests(addressing).duplicate_track(
                "358", withoutClips=True, name="SIDEKICK TRY capture"),
                args_of(version, "mutate/duplicate_track_without_clips.json"))

    def test_take_lane_duplicate_exists_only_on_22(self):
        built = pp.PPRequests("2.2").duplicate_clip_to_new_lane(
            "c1", track=5, start="1|1", lane_name="SIDEKICK CAND 1", transforms="pitch += 12")
        self.assertForm(V22, "ppal-duplicate", built, {
            "type": "clip", "id": "c1", "toPath": "t5/l+", "arrangementStart": "1|1",
            "takeLaneName": "SIDEKICK CAND 1", "transforms": "pitch += 12"})
        with self.assertRaises(pp.UnsupportedAddressing):
            pp.PPRequests("2.3").duplicate_clip_to_new_lane("c1", track=5, start="1|1")

    def test_move_split_delete_forms(self):
        v22, v23 = pp.PPRequests("2.2"), pp.PPRequests("2.3")
        self.assertForm(V23, "ppal-update-clip", v23.move_clip("434", track=10, start="49|1"),
                        args_of(V23, "v23/v23_move_clip.json"))
        # 2.2 moved arrangement clips by arrangementStart (schema form, not captured)
        # and only on their own track: a cross-track move has no 2.2 form.
        self.assertForm(V22, "ppal-update-clip",
                        v22.move_clip("434", track=10, start="49|1", from_track=10),
                        {"id": "434", "arrangementStart": "49|1"})
        with self.assertRaises(pp.UnsupportedAddressing):
            v22.move_clip("434", track=11, start="49|1", from_track=10)
        with self.assertRaises(ValueError):
            v22.move_clip("434", track=10, start="49|1")
        self.assertEqual(v23.move_clip("434", track=11, start="49|1", from_track=10),
                         {"id": "434", "toPath": "t11[49|1]"})
        for version, builder in ((V22, v22), (V23, v23)):
            self.assertForm(version, "ppal-update-clip", builder.split_clip("446", "59|1"),
                            args_of(V23, "v23/v23_split_clip.json"))
            self.assertForm(version, "ppal-delete", builder.delete("clip", ids=["438"]),
                            args_of(V23, "v23/v23_delete_by_id.json"))
            self.assertForm(version, "ppal-delete", builder.delete("track", ids=["423", "424"]),
                            args_of(V23, "v23/v23_delete_capture_tracks.json"))
        self.assertForm(V23, "ppal-delete",
                        v23.delete("clip", arrangement=(10, "49|1")),
                        args_of(V23, "v23/v23_delete_by_path.json"))
        with self.assertRaises(pp.UnsupportedAddressing):
            v22.delete("clip", arrangement=(10, "49|1"))
        with self.assertRaises(ValueError):
            v23.delete("clip")

    def test_select_scene_playback_and_track_update_forms(self):
        v22, v23 = pp.PPRequests("2.2"), pp.PPRequests("2.3")
        for version, builder in ((V22, v22), (V23, v23)):
            self.assertForm(version, "ppal-select", builder.select(track=10, scene=0, view="session"),
                            args_of(V22, "legacy/check_select_path.json"))
            self.assertForm(version, "ppal-playback", builder.update_arrangement(
                loop=True, loop_start="5|1", loop_end="9|1", start_time="5|1"),
                args_of(version, "mutate/playback_update_arrangement.json"))
            self.assertForm(version, "ppal-update-track", builder.update_track("423", mute=True),
                            args_of(V23, "v23/v23_mute_by_id.json"))
            self.assertForm(version, "ppal-read-clip", builder.clip(track=9, scene=0),
                            args_of(version, "legacy/legacy_read_clip_path.json"))
            self.assertForm(version, "ppal-read-clip", builder.clip(clip_id="359"),
                            args_of(version, "legacy/legacy_read_clip_id.json"))
        # Scene creation / reads / play-scene: schema forms (no capture).
        self.assertForm(V22, "ppal-create-scene", v22.create_scenes(count=2, at=8),
                        {"count": 2, "capture": False, "sceneIndex": 8})
        self.assertForm(V23, "ppal-create-scene", v23.create_scenes(count=2, at=8),
                        {"count": 2, "capture": False, "path": "s8"})
        self.assertForm(V22, "ppal-create-scene", v22.append_scenes(count=1, scene_count=8),
                        {"count": 1, "capture": False, "sceneIndex": 8})
        self.assertForm(V23, "ppal-create-scene", v23.append_scenes(count=1, scene_count=8),
                        {"count": 1, "capture": False, "path": "s+"})
        self.assertForm(V22, "ppal-read-scene", v22.read_scene(3), {"sceneIndex": 3})
        self.assertForm(V23, "ppal-read-scene", v23.read_scene(3), {"path": "s3"})
        self.assertForm(V22, "ppal-playback", v22.play_scene(3),
                        {"action": "play-scene", "sceneIndex": 3})
        self.assertForm(V23, "ppal-playback", v23.play_scene(3),
                        {"action": "play-scene", "path": "s3"})

    def test_22_forms_fail_the_23_schemas(self):
        for tool, args in (("ppal-read-track", {"trackIndex": 1}),
                           ("ppal-create-track", {"trackIndex": -1}),
                           ("ppal-create-clip", {"path": "t1", "arrangementStart": "1|1"}),
                           ("ppal-duplicate", {"type": "clip", "arrangementStart": "1|1"}),
                           ("ppal-update-clip", {"id": "1", "arrangementStart": "1|1"}),
                           ("ppal-create-scene", {"sceneIndex": 1}),
                           ("ppal-playback", {"action": "play-scene", "sceneIndex": 1}),
                           ("ppal-select", {"trackIndex": 1})):
            with self.subTest(tool=tool), self.assertRaisesRegex(AssertionError,
                                                                 "unknown arguments"):
                pp_fakes.validate_call(tool, args, version=V23)

    def test_addressing_accepts_a_generation_or_a_build_version(self):
        for value, addressing in (("2.2", "2.2"), ("2.3", "2.3"), ("2.2.0-rc1", "2.2"),
                                  ("2.3.0", "2.3"), (" 2.3.0 ", "2.3")):
            with self.subTest(value=value):
                self.assertEqual(pp.normalize_addressing(value), addressing)
                self.assertEqual(pp.PPRequests(value).addressing, addressing)
        for value in ("garbage", "", None, "1.4.13", "3.0.0", "2.1.0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pp.normalize_addressing(value)

    def test_requests_for_session_follows_its_handshake(self):
        self.assertEqual(pp.requests_for(pp.PPSession(object(), {"pp_version": V23})).addressing,
                         "2.3")
        self.assertEqual(pp.requests_for(object()).addressing, "2.2")
        with self.assertRaises(ValueError):
            pp.PPRequests("2.1")


if __name__ == "__main__":
    unittest.main()
