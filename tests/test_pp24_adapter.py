"""Producer Pal 2.4.0 — every branch proved by a captured fixture under
tests/fixtures/pp/2.4.0/ (SCRATCH-capture.als, Live 12.4.6, 2026-10-03).

The sidekick speaks 2.2.0-rc1, 2.3.0 and 2.4.0 at once: the handshake names
the build, the request builder emits that build's forms, the readers accept
every captured reply shape. Nothing here is ported from the release notes.
"""
import asyncio
import json
import unittest

from mcp_server import lane_audition
from mcp_server import pp_codec
from mcp_server import pp_session as pp
from tests import pp_fakes
from tests.pp_fakes import V22, V23, V24


def record(relative, version=V24):
    return pp_fakes.load_fixture(relative, version=version)


def args_of(relative, version=V24):
    return record(relative, version)["arguments"]


def reply_of(relative, version=V24):
    """The PPResponse pp_session.call_tool builds from a captured reply."""
    rec = record(relative, version)

    class Raw:
        async def call_tool(self, tool, arguments):
            return pp_fakes.mcp_result(rec["response"])

    return asyncio.run(pp.call_tool(Raw(), rec["tool"], rec["arguments"]))


class VersionGateTests(unittest.TestCase):
    def test_the_captured_24_connect_passes_the_mutation_guard(self):
        # v24/connect.json: {abletonLiveVersion, connected, liveSet{…}, producerPalVersion: "2.4.0"}
        rec = record("v24/connect.json")
        handshake = pp.validate_handshake(pp.handshake_from_connect(
            pp.parse_tool_response(pp_fakes.response_text(rec["response"])),
            sorted(pp_fakes.schemas_for(V24)), requested={"notation": pp.PP_NOTATION}))
        self.assertEqual(handshake["pp_version"], "2.4.0")
        caps = handshake["capabilities"]
        self.assertEqual(caps["addressing"], "2.4")
        self.assertTrue(caps["supports_2_2"])
        self.assertTrue(caps["supports_2_3"], "2.4 keeps 2.3's path addressing")
        self.assertTrue(caps["supports_2_4"])
        self.assertTrue(caps["live_api"])
        self.assertEqual(pp.SUPPORTED_PP_VERSIONS, frozenset({V22, V23, V24}))

    def test_other_24_builds_still_fail_the_guard(self):
        for version in ("2.4.1", "2.5.0"):
            with self.subTest(version=version):
                handshake = pp.handshake_from_connect(pp.parse_tool_response(
                    '{"result":{"producerPalVersion":"%s"}}' % version))
                with self.assertRaisesRegex(pp.PPProtocolError, "not verified"):
                    pp.validate_handshake(handshake)

    def test_addressing_generation_24(self):
        for value in ("2.4", "2.4.0", " 2.4.0 ", "2.5.0"):
            with self.subTest(value=value):
                self.assertEqual(pp.normalize_addressing(value), "2.4")
        self.assertEqual(pp.addressing_for_version("2.4.0"), "2.4")
        self.assertEqual(pp.addressing_of(pp.PPSession(object(), {"pp_version": V24})), "2.4")
        requests = pp.requests_for(pp.PPSession(object(), {"pp_version": V24}))
        self.assertEqual(requests.addressing, "2.4")
        self.assertTrue(requests.v23, "path addressing (positions in the path) stays")
        self.assertTrue(requests.v24)
        self.assertFalse(pp.PPRequests("2.3").v24)
        self.assertIn("2.4", pp.ADDRESSINGS)

    def test_the_24_schemas_dropped_count_and_the_fake_refuses_it(self):
        # tool_schemas.json: create-track / create-scene have no `count` on 2.4
        # (the device still honours it with a deprecation WARNING —
        # v24_create_track_count_deprecated, v24_create_scene_count_deprecated —
        # but the sidekick sends only schema-listed forms).
        for tool in ("ppal-create-track", "ppal-create-scene"):
            with self.subTest(tool=tool):
                self.assertNotIn("count", pp_fakes.schemas_for(V24)[tool]["inputSchema"]["properties"])
                self.assertIn("count", pp_fakes.schemas_for(V23)[tool]["inputSchema"]["properties"])
                with self.assertRaisesRegex(AssertionError, "unknown arguments.*count"):
                    pp_fakes.validate_call(tool, {"count": 1, "path": "s+"}, version=V24)
        self.assertEqual(sorted(pp_fakes.schemas_for(V24)), sorted(pp_fakes.schemas_for(V23)))
        self.assertEqual(len(pp_fakes.schemas_for(V24)), 22)


class RequestFormsEqualTheCapturesTests(unittest.TestCase):
    """PPRequests("2.4") emits exactly the argument dicts the 2.4 captures sent."""

    def setUp(self):
        self.r = pp.PPRequests("2.4")

    def assertForm(self, tool, built, relative):
        self.assertEqual(built, args_of(relative))
        pp_fakes.validate_call_values(tool, built, version=V24)

    def test_batch_track_and_scene_creates_use_repeated_paths(self):
        # v24_create_tracks_batch: path "t+,t+", name "A,B"
        self.assertForm("ppal-create-track",
                        self.r.create_track(count=2, type="midi",
                                            name="CAPTURE 24 A,CAPTURE 24 B"),
                        "v24/v24_create_tracks_batch.json")
        self.assertEqual(self.r.create_track(), {"path": "t+"})
        # v24_create_scenes_batch: path "s+,s+" (no count)
        self.assertForm("ppal-create-scene",
                        self.r.append_scenes(count=2, scene_count=8, name="CAP24 S1,CAP24 S2"),
                        "v24/v24_create_scenes_batch.json")
        self.assertEqual(self.r.append_scenes(count=1, scene_count=8),
                         {"capture": False, "path": "s+"})
        self.assertEqual(self.r.append_scenes(count=3, scene_count=8),
                         {"capture": False, "path": "s+,s+,s+"})
        # 2.3 keeps its captured form (v23b_create_scene_append: count + path "s+")
        self.assertEqual(pp.PPRequests("2.3").append_scenes(count=2, scene_count=8),
                         {"count": 2, "capture": False, "path": "s+"})
        with self.assertRaises(pp.UnsupportedAddressing):
            self.r.create_scenes(count=1, at=2)   # insert-at-index: not captured on 2.4

    def test_path_forms_unchanged_from_23(self):
        self.assertForm("ppal-read-track", self.r.read_track(10, include=["devices"]),
                        "v24/v24_read_track_devices.json")
        self.assertForm("ppal-delete", self.r.delete("clip", arrangement=(10, "13|1")),
                        "v24/v24_delete_clip_by_path.json")
        self.assertForm("ppal-delete", self.r.delete("clip", ids=["552", "999998"]),
                        "v24/v24_delete_clips_batch_one_bad.json")
        self.assertForm("ppal-update-clip", self.r.move_clip("551", track=10, start="17|1"),
                        "v24/v24_move_clip.json")
        self.assertForm("ppal-update-clip", self.r.split_clip("552", "6|1"),
                        "v24/v24_split_clip.json")
        self.assertForm("ppal-duplicate", self.r.duplicate_clip_to_arrangement(
            "551", track=10, start="13|1", length="1bar"), "v24/v24_duplicate_trimmed.json")
        self.assertForm("ppal-select", self.r.select(path="t10[5|1]"),
                        "v24/v24_select_arrangement_position.json")
        self.assertForm("ppal-select", self.r.select(), "v24/v24_selection_readback.json")
        self.assertForm("ppal-playback", self.r.play_scene(10), "v24/v24_play_scene_by_path.json")
        self.assertForm("ppal-playback", self.r.stop(), "v24/v24_stop.json")
        self.assertForm("ppal-create-clip", self.r.create_clip(
            track=10, arrangement_start="1|1", length="2bar", looping=True, name="M1",
            notes=args_of("v24/v24_create_arrangement_clip.json")["notes"]),
            "v24/v24_create_arrangement_clip.json")
        self.assertForm("ppal-create-clip", self.r.create_clip(
            track=10, scene=0, length="1bar", name="S0",
            notes=args_of("v24/v24_create_session_clip.json")["notes"]),
            "v24/v24_create_session_clip.json")
        with self.assertRaises(pp.UnsupportedAddressing):
            self.r.duplicate_clip_to_new_lane("1", track=10, start="1|1")

    def test_lane_auditions_stay_refused_on_24(self):
        # 2.4.0/v23b/v23b_delete_lane_clip.json: the lane-clip delete is now an
        # MCP error ("cannot delete take-lane clip … via the API") — an
        # audition still could not clean up, so it refuses before any write.
        rec = record("v23b/v23b_delete_lane_clip.json")
        self.assertTrue(rec["response"]["is_error"])
        self.assertIn("cannot delete take-lane clip", rec["response"]["content"][0]["text"])
        session = pp.PPSession(object(), {"pp_version": V24,
                                          "capabilities": {"supports_2_2": True}})
        refused = lane_audition._lanes_unsupported(session)
        self.assertEqual(refused["status"], "unsupported_capability")
        self.assertIsNone(lane_audition._lanes_unsupported(
            pp.PPSession(object(), {"pp_version": V22, "capabilities": {"supports_2_2": True}})))


class ReaderTests(unittest.TestCase):
    """One reader per shape, each accepting the 2.2 / 2.3 / 2.4 captured replies."""

    # -- three failure classes ------------------------------------------------
    def test_single_target_failures_are_mcp_errors(self):
        # 2.4: a single failing target is an MCP error (is_error: true, "Error: …")
        # where 2.3 sent `[]` / `{deleted: false}` + WARNING. call_tool raises.
        for relative, text in (
                ("v24/v24_read_clip_missing_id.json", 'Error: id "999999" does not exist'),
                ("v24/v24_read_track_missing.json", 'Error: nothing at path "t999"'),
                ("v24/v24_read_clip_empty_slot.json", "Error: no clip at t10/s0"),
                ("v23b/v23b_refuse_create_midi_on_audio.json", "is audio; a MIDI clip needs"),
                ("v23/v23_duplicate_midi_to_audio.json", "is audio; a MIDI clip needs"),
                ("v23b/v23b_delete_lane_clip.json", "cannot delete take-lane clip"),
                ("v24/v24_update_track_send_missing_return.json", "no send landed"),
                ("v24/v24_update_device_refused_param.json", "gainDb not applicable"),
                ("v24/v24_select_list_refused.json", "select takes one target per call"),
                ("v24/v24_duplicate_list_mismatch_refused.json", "toPath names 1 destination")):
            with self.subTest(fixture=relative):
                self.assertTrue(record(relative)["response"]["is_error"])
                with self.assertRaises(pp.PPToolError) as caught:
                    reply_of(relative)
                self.assertIn(text, str(caught.exception))
                self.assertFalse(pp.is_schema_refusal(str(caught.exception)))

    def test_batch_failures_are_per_entry_ok_false(self):
        # v24_read_clips_missing_batch: no error, one {id, ok: false, detail} per miss
        reply = reply_of("v24/v24_read_clips_missing_batch.json")
        self.assertEqual(pp.failure_entries(reply), [
            {"id": "999999", "detail": 'id "999999" does not exist'},
            {"id": "999998", "detail": 'id "999998" does not exist'}])
        self.assertTrue(pp.is_missing_id_error(pp.failure_entries(reply)[0]["detail"], "999999"))
        self.assertFalse(pp.is_missing_id_error(pp.failure_entries(reply)[0]["detail"], "999998"))
        for relative in ("v24/v24_read_tracks_batch.json", "v24/v24_create_tracks_batch.json",
                         "v24/v24_delete_capture_tracks.json", "v24/v24_read_arrangement_clip.json"):
            with self.subTest(fixture=relative):
                self.assertEqual(pp.failure_entries(reply_of(relative)), [])
        # as a write reply, an ok:false entry is a refusal naming its detail
        failure = pp.write_failed(reply, count=2)
        self.assertIn("999999", failure)
        self.assertIn("does not exist", failure)

    def test_a_schema_refusal_is_a_third_error_class(self):
        # v24_read_track_sends: the MCP client refuses `include: ["sends"]` before
        # the device sees it — "MCP error -32602: Input validation error: …"
        rec = record("v24/v24_read_track_sends.json")
        text = rec["response"]["content"][0]["text"]
        self.assertTrue(rec["response"]["is_error"])
        self.assertTrue(pp.is_schema_refusal(text))
        self.assertTrue(pp.is_schema_refusal("Producer Pal tool error: " + text))
        with self.assertRaises(pp.PPToolError) as caught:
            reply_of("v24/v24_read_track_sends.json")
        self.assertTrue(pp.is_schema_refusal(str(caught.exception)))
        for other in ('Error: id "999999" does not exist', 'Error: nothing at path "t999"',
                      "Error: no clip at t10/s0", ""):
            self.assertFalse(pp.is_schema_refusal(other), other)

    # -- deletes --------------------------------------------------------------
    def test_delete_succeeded_reads_every_captured_receipt(self):
        yes = (("v23/v23_delete_by_id.json", V24), ("v23/v23_delete_by_path.json", V24),
               ("legacy/check_delete_clip_by_id.json", V24),
               ("v24/v24_delete_clip_by_path.json", V24),
               ("v24/v24_delete_device.json", V24),
               ("mutate/delete_track.json", V24),
               ("v23/v23_delete_by_id.json", V23),
               ("mutate/delete_track.json", V23),
               ("mutate/delete_track.json", V22),
               ("legacy/check_delete_clip_by_id.json", V22))
        for relative, version in yes:
            with self.subTest(fixture=relative, version=version):
                item = pp_fakes.captured_json(relative, version)
                self.assertTrue(pp.delete_succeeded(item, item["id"]))
                self.assertFalse(pp.delete_succeeded(item, "other-id"))
                if version == V24:
                    self.assertNotIn("deleted", item)      # 2.4: deletedPath only
        for entry in pp_fakes.captured_json("v24/v24_delete_capture_tracks.json", V24):
            self.assertTrue(pp.delete_succeeded(entry, entry["id"]))
        for entry in pp_fakes.captured_json("v24/v24_delete_scenes.json", V24):
            self.assertTrue(pp.delete_succeeded(entry, entry["id"]))
        no = (("v24/v24_delete_clip_missing_id.json", V24, "999999"),       # {detail, id}
              ("v24/v24_delete_clip_by_id_already_replaced.json", V24, "548"),
              ("v23/v23_delete_missing_id.json", V24, "999999"),
              ("v23/v23_delete_missing_id.json", V23, "999999"),           # deleted: false
              ("v23b/v23b_delete_lane_clip.json", V23, "376"))             # deleted: false + path
        for relative, version, object_id in no:
            with self.subTest(fixture=relative, version=version):
                self.assertFalse(pp.delete_succeeded(
                    pp_fakes.captured_json(relative, version), object_id))
        for entry in pp_fakes.captured_json("v24/v24_delete_clips_batch_one_bad.json", V24):
            self.assertFalse(pp.delete_succeeded(entry, entry["id"]))
        self.assertFalse(pp.delete_succeeded({"id": "1"}, "1"))             # says nothing
        self.assertFalse(pp.delete_succeeded({"deletedPath": "t1/s0"}, "1"))  # names no id

    def test_write_failed_expect_deleted_on_24(self):
        for relative, ids in (("v23/v23_delete_by_id.json", ["429"]),
                              ("v24/v24_delete_clip_by_path.json", None),
                              ("v24/v24_delete_capture_tracks.json", ["529", "530", "531"]),
                              ("v24/v24_delete_scenes.json", None),
                              ("v24/v24_delete_device.json", ["567"]),
                              ("legacy/check_delete_clip_by_id.json", ["396"])):
            with self.subTest(fixture=relative):
                self.assertIsNone(pp.write_failed(reply_of(relative), expect="deleted", ids=ids))
        missed = pp.write_failed(reply_of("v24/v24_delete_clip_missing_id.json"),
                                 expect="deleted", ids=["999999"])
        self.assertIn("did not delete 999999", missed)
        self.assertIn("nothing to delete", missed)
        batch = pp.write_failed(reply_of("v24/v24_delete_clips_batch_one_bad.json"),
                                expect="deleted", ids=["552", "999998"])
        self.assertIn("552", batch)
        self.assertIn("999998", batch)
        self.assertIn("did not report deleting 530", pp.write_failed(
            reply_of("v24/v24_delete_device.json"), expect="deleted", ids=["567", "530"]))
        receipt = pp.extract_delete_receipt(reply_of("v24/v24_delete_capture_tracks.json"))
        self.assertEqual(receipt["deleted_ids"], ["529", "530", "531"])
        self.assertEqual(pp.extract_delete_receipt(
            reply_of("v24/v24_delete_clip_missing_id.json"))["deleted_ids"], [])
        gone = pp.normalize_delete(pp_fakes.captured_json("v24/v24_delete_device.json", V24))
        self.assertEqual((gone["deleted"], gone["track_index"], gone["track_path"]),
                         (True, 10, "t10/d0"))
        self.assertFalse(pp.normalize_delete(
            pp_fakes.captured_json("v24/v24_delete_clip_missing_id.json", V24))["deleted"])

    # -- live-api on a missing id ---------------------------------------------
    def test_live_api_missing_reads_the_result_less_shape(self):
        # v24_live_api_missing_id: {"id": "0", "path": "", "results": [{"operation": …}]}
        missing = reply_of("v24/v24_live_api_missing_id.json")
        self.assertTrue(pp.live_api_missing(missing))
        with self.assertRaises(pp.PPProtocolError):
            pp.extract_live_api_receipts(missing)       # still no receipts: check first
        full = reply_of("v24/v24_live_api_full_notes.json")
        self.assertFalse(pp.live_api_missing(full))
        self.assertEqual(len(pp.extract_live_api_receipts(full)), 2)
        self.assertFalse(pp.live_api_missing(reply_of("read/live_api_live_set_properties.json")))
        self.assertFalse(pp.live_api_missing(reply_of("read/live_api_live_set_properties.json", V23)))
        # a result-less receipt under a real object is not the missing shape
        self.assertFalse(pp.live_api_missing(pp.parse_tool_response(json.dumps({"result": {
            "id": "553", "path": "live_set tracks 10 arrangement_clips 0",
            "results": [{"operation": {"type": "getProperty", "property": "name"}}]}}))))

    # -- successful writes are minimised -------------------------------------
    def test_write_failed_accepts_the_minimised_24_successes(self):
        cases = (
            ("v24/v24_update_track_minimised.json", {"count": 1, "ids": ["529"]}),
            ("v24/v24_update_track_send_by_letter.json", {"count": 1, "ids": ["529"]}),
            ("v24/v24_update_track_send_by_name.json", {"count": 1, "ids": ["529"]}),
            ("v24/v24_update_track_unmute.json", {"count": 1}),
            ("v24/v24_update_clip_name_only.json", {"count": 1, "ids": ["551"]}),
            ("v24/v24_create_arrangement_clip.json",
             {"count": 1, "location": {"path": "t10[1|1]"}, "note_count": 2}),
            ("v24/v24_create_session_clip.json",
             {"count": 1, "location": {"path": "t10/s0"}, "note_count": 2}),
            ("v24/v24_create_lane_clip.json",
             {"count": 1, "location": {"path": "t10/l0[1|1]"}, "note_count": 2}),
            ("v24/v24_move_clip.json", {"count": 1, "location": {"path": "t10[17|1]"}}),
            ("v24/v24_duplicate_trimmed.json", {"count": 1, "location": {"path": "t10[13|1]"}}),
            ("v24/v24_duplicate_over_existing.json", {"count": 1, "location": {"path": "t10[5|1]"}}),
            ("v24/v24_create_tracks_batch.json", {"count": 2}),
            ("v24/v24_create_scenes_batch.json", {"count": 2}),
            ("v23b/v23b_create_scene_append.json", {"count": 1}),
            ("v24/v24_lane_append.json", {"count": 1}),
            ("v24/v24_duplicate_track_onto_lane.json", {"count": 1}),
            ("v23b/v23b_promote_lane_clip.json", {"count": 1, "location": {"path": "t10[9|1]"}}),
            ("v24/v24_create_device_new_param.json", {"count": 1}),
            ("v23/v23_split_clip.json", {"count": 2}),
        )
        for relative, kwargs in cases:
            with self.subTest(fixture=relative):
                self.assertIsNone(pp.write_failed(reply_of(relative), **kwargs))
                for warning in pp_fakes.DEVICE_ADVISORY_WARNINGS[V24]:
                    reply = reply_of(relative)
                    reply.warnings.append(warning)
                    self.assertIsNone(pp.write_failed(reply, **kwargs), warning)
        # a create-clip / update-clip notes reply still echoes noteCount: a wrong one fails
        self.assertIn("noteCount", pp.write_failed(
            reply_of("v24/v24_create_arrangement_clip.json"), count=1, note_count=5))
        # the update-clip `notes` reply is ADDITIVE: 2 notes + 1 -> noteCount 3
        self.assertEqual(pp.find_int(reply_of("v24/v24_update_clip_notes.json"), "noteCount"), 3)

    def test_an_overwrote_detail_after_one_of_our_writes_is_a_hard_failure(self):
        # v24_create_session_clip_replaces: create-clip into an occupied slot
        # REPLACES his clip and says so only in `detail` — never swallowed.
        reply = reply_of("v24/v24_create_session_clip_replaces.json")
        failure = pp.write_failed(reply, count=1, location={"path": "t10/s0"}, note_count=1)
        self.assertIsNotNone(failure)
        self.assertIn("overwrote the existing clip at t10/s0", failure)
        self.assertIn("550", failure)
        # the other captured details are advisory (lane notices, promotion)
        for relative in ("v24/v24_create_lane_clip.json", "v23b/v23b_promote_lane_clip.json",
                         "v24/v24_duplicate_track_onto_lane.json"):
            with self.subTest(fixture=relative):
                self.assertIn("detail", pp_fakes.captured_json(relative, V24))
                self.assertIsNone(pp.write_failed(reply_of(relative), count=1))

    def test_a_split_that_cut_nothing_is_read_from_its_warning(self):
        # v24_split_clip_deprecated_key: {id, path} (ONE object, the uncut clip)
        # + "WARNING: split cut nothing …" — a write that did NOT happen
        reply = reply_of("v24/v24_split_clip_deprecated_key.json")
        self.assertEqual(len(pp.result_objects(reply)), 1)
        failure = pp.write_failed(reply, count=2)
        self.assertIsNotNone(failure)
        self.assertIn("split cut nothing", failure)
        self.assertFalse(pp.is_advisory_warning(reply.warnings[0]))
        self.assertTrue(pp.is_advisory_warning(reply.warnings[1]))    # "param split is deprecated"
        # a successful split: head + tail (v23_split_clip on the 2.4 run)
        self.assertIsNone(pp.write_failed(reply_of("v23/v23_split_clip.json"), count=2))

    # -- warnings / errors that still exist on 2.4 ----------------------------
    def test_deprecation_and_fallback_warnings_are_advisory(self):
        for relative in ("legacy/legacy_delete_ids.json", "legacy/legacy_read_clip_clipId.json",
                         "legacy/legacy_create_clip_slot.json", "mutate/duplicate_clip_to_arrangement.json",
                         "v23b/v23b_create_scene_append.json", "v24/v24_create_scene_count_deprecated.json",
                         "v24/v24_create_track_count_deprecated.json",
                         "v24/v24_create_device_deprecated_name_append.json",
                         "legacy/check_clips_after_clip_delete.json"):
            with self.subTest(fixture=relative):
                reply = reply_of(relative)
                self.assertTrue(reply.warnings, relative)
                self.assertTrue(all(pp.is_advisory_warning(w) for w in reply.warnings), reply.warnings)
                self.assertEqual(pp.response_warnings_mean_incomplete(reply), [])
        for warning in pp_fakes.DEVICE_ADVISORY_WARNINGS[V24]:
            self.assertTrue(pp.is_advisory_warning(warning), warning)
        self.assertFalse(pp.is_advisory_warning(pp_fakes.DEVICE_REFUSAL_WARNINGS[V24]["split"]))

    def test_missing_id_and_empty_slot_texts_on_24(self):
        missing = pp_fakes.response_text(record("v24/v24_read_clip_missing_id.json")["response"])
        self.assertTrue(pp.is_missing_id_error("Producer Pal tool error: " + missing, "999999"))
        self.assertFalse(pp.is_missing_id_error("Producer Pal tool error: " + missing, "99999"))
        for relative in ("v24/v24_read_clip_empty_slot.json",
                         "legacy/check_read_clip_existing_empty_slot.json",
                         "legacy/legacy_read_clip_empty_slot.json"):
            text = pp_fakes.response_text(record(relative)["response"])
            with self.subTest(fixture=relative):
                self.assertTrue(pp.is_empty_slot_error("Producer Pal tool error: " + text))
                self.assertFalse(pp.is_missing_id_error(text, "999999"))
        for relative in ("v24/v24_read_track_missing.json", "mutate/error_missing_track.json",
                         "v24/v24_read_clip_missing_id.json", "v24/v24_read_track_sends.json"):
            text = pp_fakes.response_text(record(relative)["response"])
            with self.subTest(fixture=relative):
                self.assertFalse(pp.is_empty_slot_error("Producer Pal tool error: " + text))
        # the production path: the read raises, the caller asks is_empty_slot_error
        with self.assertRaises(pp.PPToolError) as caught:
            reply_of("v24/v24_read_clip_empty_slot.json")
        self.assertTrue(pp.is_empty_slot_error(caught.exception))
        self.assertEqual(pp_codec.classify_session_clip(str(caught.exception), []), "empty")

    # -- shapes that moved -----------------------------------------------------
    def test_selection_and_clip_reads(self):
        selected = pp.extract_selected_context(reply_of("v24/v24_selection_readback.json"))
        self.assertEqual(selected["selected_clip"]["id"], "553")
        self.assertEqual(selected["selected_clip"]["path"], "t10[5|1]")
        self.assertEqual(selected["selected_track"]["track_index"], 10)
        self.assertEqual(selected["selected_scene"]["scene_index"], 7)
        by_position = pp.extract_selected_context(reply_of("v24/v24_select_arrangement_position.json"))
        self.assertEqual(by_position["selected_clip"]["id"], "553")
        self.assertIsNone(by_position["selected_scene"])
        clip = pp.clip_record(reply_of("v24/v24_read_arrangement_clip.json"))
        self.assertEqual(pp.arrangement_start_of(clip), "1|1")
        self.assertEqual(pp.clip_track_index(clip), 10)
        self.assertEqual(len(pp.extract_notes(reply_of("v24/v24_read_arrangement_clip.json"))), 3)
        self.assertEqual(pp.classify_session_clip(reply_of("v24/v24_read_replaced_slot.json"),
                                                  pp.extract_notes(reply_of("v24/v24_read_replaced_slot.json"))),
                         "heard")
        tracks = pp.result_objects(reply_of("v24/v24_create_tracks_batch.json"))
        self.assertEqual([pp.track_index_of(t) for t in tracks], [10, 11])
        self.assertEqual(pp.created_track(reply_of("v24/v24_create_tracks_batch.json")),
                         {"id": "529", "track_index": 10})
        lane = pp.clip_record(reply_of("v24/v24_read_lane.json"))
        self.assertEqual(pp.normalize_clip(lane["clips"][0])["take_lane"], 0)
        overview = pp_fakes.captured_json("v24/v24_live_set_before.json", V24)
        self.assertIsNotNone(pp.main_track_of(overview))
        self.assertEqual([pp.return_index_of(r) for r in overview["returnTracks"]], [0, 1])


class DeleteGateTests(unittest.TestCase):
    """Absence on 2.4 is proven by the readback — the missing-id error for THIS
    id (v24_read_clip_missing_id, the 2.3 text) or the live-api result-less
    shape (v24_live_api_missing_id) — never by a delete receipt alone:
    `{deletedPath, id}` says Live deleted it, `{detail: "nothing to delete"}`
    says it had nothing, and only the readback says what is there NOW."""

    def setUp(self):
        pp.set_live_process_incarnation_resolver(
            lambda: {"pid": 4242, "start_time": "Sat Oct  3 10:00:00 2026"})

    def tearDown(self):
        pp.set_live_process_incarnation_resolver(None)

    def _run(self, device, fn, *args, **kwargs):
        from unittest.mock import patch
        with patch.object(pp, "operation_session", device.operation()):
            return asyncio.run(fn(*args, **kwargs))

    def test_read_clip_live_marks_the_24_live_api_shape_missing(self):
        from mcp_server import experiments, live_write as lw
        rec = record("v24/v24_live_api_missing_id.json")

        @pp_fakes.schema_strict
        class Session:
            capabilities = {"live_api": True}
            handshake = {"pp_version": V24}

            async def call(self, name, args=None):
                assert name == "ppal-live-api" and args["path"] == "id 999999", (name, args)
                return pp.parse_tool_response(json.dumps(
                    {"result": rec["response"]["content"][0]["json"]}))

        session = Session()
        session.pp_version = V24
        read = asyncio.run(lw.read_clip_live(session, "999999"))
        self.assertTrue(read.get("missing"), read)
        self.assertIn("error", read)
        self.assertEqual(experiments.clip_object(read), ("missing", None))
        # the receipt parser alone would have said "invalid receipt", not missing
        with self.assertRaises(pp.PPProtocolError):
            pp.extract_live_api_receipts(reply_of("v24/v24_live_api_missing_id.json"))

    def test_verify_clip_absent_accepts_the_24_readback_error(self):
        from mcp_server import live_write as lw
        device = pp_fakes.CapturedDevice(version=V24)
        jam = device.add_track(lw.JAM_TRACK_NAME)
        identity = self._run(device, lw.read_set_identity)
        session = device.pp_session()
        after = asyncio.run(lw._verify_clip_absent(session, "999999", jam, identity))
        self.assertEqual((after["status"], after["method"]), ("absent", "missing_id_reply"))
        # what the device said is the captured 2.4 text (same as 2.3)
        read = [a for n, a in device.wire_calls if n == "ppal-read-clip"][-1]
        self.assertEqual(read, {"id": "999999", "include": []})
        self.assertFalse(lw._missing_clip_id_error('Error: nothing at path "t999"', "999999"))

    def _jam_with_clip(self, device):
        from mcp_server import live_write as lw
        jam = device.add_track(lw.JAM_TRACK_NAME)
        notes = pp_codec.encode_notes([{"pitch": 60, "start_beats": 0.0, "duration_beats": 1.0,
                                        "velocity": 100}], pp_codec.CodecMode.MIDI_JSON)
        clip = device.add_clip("t%d/s0" % jam["trackIndex"], "answer", notes)
        identity = self._run(device, lw.read_set_identity)
        return jam, clip, identity

    def test_delete_jam_clip_on_24_proves_absence_by_readback(self):
        from mcp_server import live_write as lw
        device = pp_fakes.CapturedDevice(version=V24)
        jam, clip, identity = self._jam_with_clip(device)
        result = self._run(device, lw.delete_jam_clip, "%d/0" % jam["trackIndex"],
                           expected_clip_id=clip["id"], expected_track_id=jam["id"],
                           expected_set_identity=identity)
        self.assertTrue(result["deleted"], result)
        # the receipt is 2.4's {deletedPath, id} (2.4.0/legacy/check_delete_clip_by_id)
        self.assertEqual(result["delete_receipt"]["result"],
                         {"deletedPath": "t%d/s0" % jam["trackIndex"], "id": clip["id"]})
        self.assertEqual(result["delete_receipt"]["deleted_ids"], [clip["id"]])
        self.assertEqual(result["after_state"]["method"], "missing_id_reply")
        self.assertNotIn("receipt_disagreed", result)

    def test_nothing_to_delete_with_the_clip_still_there_is_not_a_delete(self):
        from mcp_server import live_write as lw
        device = pp_fakes.CapturedDevice(version=V24)
        device.refuse_deletes = True
        jam, clip, identity = self._jam_with_clip(device)
        result = self._run(device, lw.delete_jam_clip, "%d/0" % jam["trackIndex"],
                           expected_clip_id=clip["id"], expected_track_id=jam["id"],
                           expected_set_identity=identity)
        self.assertFalse(result["deleted"], result)
        self.assertEqual(result["reason"], "clip still present after delete")
        # v24_delete_clip_missing_id: {detail: "nothing to delete", id}, no warning
        self.assertEqual(result["delete_receipt"]["result"],
                         {"detail": "nothing to delete", "id": clip["id"]})
        self.assertEqual(result["delete_receipt"]["warnings"], [])
        self.assertEqual(result["delete_receipt"]["deleted_ids"], [])
        self.assertIn("t%d/s0" % jam["trackIndex"], device.clips)

    def test_nothing_to_delete_with_the_clip_gone_is_a_delete_the_receipt_disputed(self):
        from mcp_server import live_write as lw
        device = pp_fakes.CapturedDevice(version=V24)
        device.misreport_deletes = True
        jam, clip, identity = self._jam_with_clip(device)
        result = self._run(device, lw.delete_jam_clip, "%d/0" % jam["trackIndex"],
                           expected_clip_id=clip["id"], expected_track_id=jam["id"],
                           expected_set_identity=identity)
        self.assertTrue(result["deleted"], result)
        self.assertEqual(result["after_state"]["status"], "absent")
        self.assertIn("nothing to delete", result["receipt_disagreed"])
        self.assertNotIn("t%d/s0" % jam["trackIndex"], device.clips)


class OccupiedSlotTests(unittest.TestCase):
    """2.4 replaces: create-clip into an occupied session slot overwrites the
    clip (v24_create_session_clip_replaces); a duplicate onto an occupied
    arrangement position destroys the clip there and says nothing
    (v24_duplicate_over_existing). His clips must never be replaceable by
    us, so every writer reads the slot / span first."""

    def setUp(self):
        pp.set_live_process_incarnation_resolver(
            lambda: {"pid": 4242, "start_time": "Sat Oct  3 10:00:00 2026"})

    def tearDown(self):
        pp.set_live_process_incarnation_resolver(None)

    def _run(self, device, fn, *args, **kwargs):
        from unittest.mock import patch
        with patch.object(pp, "operation_session", device.operation()):
            return asyncio.run(fn(*args, **kwargs))

    def test_the_captured_m2_sequence_shows_the_silent_destruction(self):
        # M2 (id 552) created at t10[5|1] (v24_create_arrangement_clip_2); M1 duplicated
        # onto t10[5|1] replied {id: "553", path} only (v24_duplicate_over_existing);
        # splitting 552 then errored "does not exist" (v24_split_clip); the listing
        # shows 553 at t10[5|1] and no 552 (v24_read_track_after_clip_ops).
        m2 = pp_fakes.captured_json("v24/v24_create_arrangement_clip_2.json", V24)
        self.assertEqual((m2["id"], m2["path"]), ("552", "t10[5|1]"))
        over = record("v24/v24_duplicate_over_existing.json")
        self.assertEqual(over["arguments"]["toPath"], "t10[5|1]")
        self.assertEqual(pp_fakes.captured_json("v24/v24_duplicate_over_existing.json", V24),
                         {"id": "553", "path": "t10[5|1]"})
        self.assertEqual([p for p in over["response"]["content"] if "text" in p], [])
        split = record("v24/v24_split_clip.json")
        self.assertEqual(split["arguments"]["id"], "552")
        self.assertTrue(split["response"]["is_error"])
        self.assertTrue(pp.is_missing_id_error(split["response"]["content"][0]["text"], "552"))
        listing = pp_fakes.captured_json("v24/v24_read_track_after_clip_ops.json", V24)
        at_bar_5 = [c for c in listing["arrangementClips"] if pp.arrangement_start_of(c) == "5|1"]
        self.assertEqual([c["id"] for c in at_bar_5], ["553"])
        self.assertNotIn("552", [c["id"] for c in listing["arrangementClips"]])
        # the replaced session clip is gone too: its id deletes nothing
        self.assertEqual(pp_fakes.captured_json(
            "v24/v24_delete_clip_by_id_already_replaced.json", V24)["detail"], "nothing to delete")
        self.assertEqual(pp_fakes.captured_json("v24/v24_read_replaced_slot.json", V24)["id"], "550")

    def test_jam_and_booth_read_the_slot_before_creating(self):
        from mcp_server import live_write as lw
        notes = [{"pitch": 60, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 100}]
        for version in (V22, V23, V24):
            with self.subTest(version=version, writer="jam"):
                device = pp_fakes.CapturedDevice(version=version)
                receipt = self._run(device, lw.write_answer_to_jam_track, notes, "answer")
                reads = [a for n, a in device.wire_calls if n == "ppal-read-clip"]
                create = next(a for n, a in device.wire_calls if n == "ppal-create-clip")
                self.assertEqual(reads[0], {"path": create["path"], "include": []})
                self.assertTrue(receipt["verified_ok"])
            with self.subTest(version=version, writer="booth"):
                device = pp_fakes.CapturedDevice(version=version)
                summary = self._run(device, lw.stage_named_clips,
                                    [{"name": "a", "notes": notes}, {"name": "b", "notes": notes}])
                creates = [a["path"] for n, a in device.wire_calls if n == "ppal-create-clip"]
                pre_reads = [a["path"] for n, a in device.wire_calls
                             if n == "ppal-read-clip" and a.get("include") == []]
                self.assertEqual(pre_reads, creates)
                self.assertTrue(summary["verified_ok"])

    def test_a_slot_the_listing_hid_is_refused_not_replaced(self):
        from mcp_server import live_write as lw
        notes = [{"pitch": 60, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 100}]
        wire = pp_codec.encode_notes(notes, pp_codec.CodecMode.MIDI_JSON)
        for writer, fn, args in (("jam", lambda d: self._run(d, lw.write_answer_to_jam_track,
                                                              notes, "answer"), None),
                                 ("booth", lambda d: self._run(d, lw.stage_named_clips,
                                                                [{"name": "a", "notes": notes}]), None)):
            with self.subTest(writer=writer):
                device = pp_fakes.CapturedDevice(version=V24)
                track = device.add_track(lw.JAM_TRACK_NAME if writer == "jam"
                                         else lw.BOOTH_TRACK_NAME)
                his = device.add_clip("t%d/s0" % track["trackIndex"], "his loop", wire)
                device.hide_from_listing = {"t%d/s0" % track["trackIndex"]}
                with self.assertRaises(lw.LiveWriteVerificationError) as caught:
                    fn(device)
                self.assertIn("is occupied by clip %s" % his["id"], caught.exception.detail)
                self.assertIn("his loop", caught.exception.detail)
                self.assertNotIn("ppal-create-clip", [n for n, _ in device.wire_calls])
                self.assertEqual(device.clips["t%d/s0" % track["trackIndex"]]["id"], his["id"])

    def test_an_overwrote_reply_after_our_write_surfaces_as_a_failure(self):
        # the slot read free, then a clip appeared before the create (a race):
        # the reply's `detail: "overwrote …"` is the verdict, never swallowed
        from mcp_server import live_write as lw
        notes = [{"pitch": 60, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 100}]
        device = pp_fakes.CapturedDevice(version=V24)
        jam = device.add_track(lw.JAM_TRACK_NAME)
        original = device.handlers["ppal-create-clip"]

        def race(args):
            device.add_clip(args["path"], "his loop", "[]")
            return original(args)

        device.handlers["ppal-create-clip"] = race
        with self.assertRaises(lw.LiveWriteVerificationError) as caught:
            self._run(device, lw.write_answer_to_jam_track, notes, "answer")
        self.assertIn("overwrote the existing clip at t%d/s0" % jam["trackIndex"],
                      caught.exception.detail)
        self.assertIn("may already be at", caught.exception.detail)

    def test_write_arrangement_refuses_an_overlapping_plan_before_any_write(self):
        from mcp_server import live_write as lw
        notes = [{"pitch": 60, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 100}]
        device = pp_fakes.CapturedDevice(version=V24)
        plan = {"tempo": 120, "total_bars": 8, "placements": [
            {"role": "lead", "section_index": 0, "arrangement_start_bar_beat": "1|1",
             "length_bars": 4, "notes": notes},
            {"role": "lead", "section_index": 1, "arrangement_start_bar_beat": "3|1",
             "length_bars": 2, "notes": notes},
            {"role": "bass", "section_index": 0, "arrangement_start_bar_beat": "3|1",
             "length_bars": 2, "notes": notes}]}
        with self.assertRaisesRegex(ValueError, "overlap on role 'lead'.*placement 1 at 1\\|1 with placement 2 at 3\\|1"):
            self._run(device, lw.write_arrangement, plan)
        self.assertEqual(device.wire_calls, [])
        self.assertEqual(pp_codec.find_overlapping_spans(
            [("a", 0, 16), ("b", 8, 16), ("c", 16, 20)]), [("a", "b")])
        self.assertEqual(pp_codec.find_overlapping_spans([("a", 0, 4), ("b", 4, 8)]), [])


class AdditiveNotesTests(unittest.TestCase):
    """update-clip `notes` is ADDITIVE on 2.4 (captured): a replace must clear
    first. Every replace path in clip_mutation sends `preTransforms` — the
    whole-clip "delete" (write replace, snapshot restore) or the bar form
    "N|*: delete" (surgery). `preTransforms` itself is not captured on 2.4:
    the readback after each update stays the authority (as on 2.3)."""

    def setUp(self):
        self._previous = pp_fakes._set_active(V24)

    def tearDown(self):
        pp_fakes._set_active(self._previous)

    def test_the_captured_pair_proves_additive_notes(self):
        # v24_create_arrangement_clip: 2 notes (60, 64) -> noteCount 2;
        # v24_update_clip_notes: notes [67] -> noteCount 3 (not 1);
        # v24_read_arrangement_clip: the readback lists all three
        created = record("v24/v24_create_arrangement_clip.json")
        self.assertEqual(len(json.loads(created["arguments"]["notes"])), 2)
        self.assertEqual(pp_fakes.captured_json("v24/v24_create_arrangement_clip.json", V24)["noteCount"], 2)
        update = record("v24/v24_update_clip_notes.json")
        self.assertEqual(update["arguments"], {"id": "551", "notes": '[{"p": 67, "t": 0, "d": 0.5, "v": 90}]'})
        self.assertNotIn("preTransforms", update["arguments"])
        self.assertEqual(pp_fakes.captured_json("v24/v24_update_clip_notes.json", V24)["noteCount"], 3)
        notes = pp.extract_notes(reply_of("v24/v24_read_arrangement_clip.json"))
        self.assertEqual(sorted(n["pitch"] for n in notes), [60, 64, 67])
        self.assertEqual(pp.verify_count(reply_of("v24/v24_read_arrangement_clip.json")),
                         {"status": "verified", "count": 3})
        # the same notes sent as a replacement would NOT verify: the readback catches it
        sent = pp_codec.midi_json_to_notes(update["arguments"]["notes"])
        verdict = pp.verify_notes(
            [{"pitch": n["pitch"], "start_beats": n["start_beats"], "duration_beats": n["duration_beats"],
              "velocity": n["velocity"]} for n in sent],
            reply_of("v24/v24_read_arrangement_clip.json"), "replace")
        self.assertEqual(verdict["status"], "mismatched")

    def test_every_replace_path_clears_before_writing(self):
        from mcp_server import clip_mutation
        from tests.test_clip_mutation import FakeClipSession, operation
        from mcp_server import server
        notes = [{"pitch": 72, "start_beats": 0, "duration_beats": 1, "velocity": 100}]
        session = FakeClipSession(occupied=True)
        self.assertEqual(session.pp_version, V24)
        preview = server._run_async(clip_mutation.write_midi_clip(
            "SIDEKICK MIDI", 0, notes, replace=True, operation_session=operation(session)))
        self.assertEqual(preview["intended_operation"]["tool"], "ppal-update-clip")
        self.assertEqual(preview["intended_operation"]["arguments"]["preTransforms"], "delete")
        self.assertIn("notes", preview["intended_operation"]["arguments"])
        pp_fakes.validate_call_values("ppal-update-clip",
                                      preview["intended_operation"]["arguments"], version=V24)
        surgery = server._run_async(clip_mutation.surgery_clip(
            "t1/s2", 2, {"notes": notes}, operation_session=operation(session)))
        self.assertEqual(surgery["intended_operation"]["arguments"]["preTransforms"], "2|*: delete")
        pp_fakes.validate_call_values("ppal-update-clip",
                                      surgery["intended_operation"]["arguments"], version=V24)
        # a create into a free slot is a create, never an additive update
        free = FakeClipSession()
        preview = server._run_async(clip_mutation.write_midi_clip(
            "SIDEKICK MIDI", 0, notes, operation_session=operation(free)))
        self.assertEqual(preview["intended_operation"]["tool"], "ppal-create-clip")
        # the fake answered the 2.4 empty-slot ERROR for the free slot
        self.assertIn(("ppal-read-clip", {"path": "t1/s0", "include": ["notes", "timing"]}),
                      free.wire_calls)


class IncludeVocabularyTests(unittest.TestCase):
    """Any include the code sends must exist in that build's schema: the MCP
    client refuses the rest with -32602 before the device sees the call
    (v24_read_track_sends)."""

    READ_TOOLS = ("ppal-read-track", "ppal-read-clip", "ppal-read-live-set",
                  "ppal-read-scene", "ppal-read-device")

    def test_the_vocabulary_equals_each_captured_schema_enum(self):
        for version, addressing in ((V22, "2.2"), (V23, "2.3"), (V24, "2.4")):
            schemas = pp_fakes.schemas_for(version)
            for tool in self.READ_TOOLS:
                with self.subTest(version=version, tool=tool):
                    enum = schemas[tool]["inputSchema"]["properties"]["include"]["items"]["enum"]
                    self.assertEqual(pp.read_include_vocabulary(addressing, tool), frozenset(enum))
        self.assertIn("warp", pp.read_include_vocabulary("2.3", "ppal-read-track"))
        self.assertNotIn("warp", pp.read_include_vocabulary("2.2", "ppal-read-track"))
        for addressing in ("2.2", "2.3", "2.4"):
            self.assertNotIn("sends", pp.read_include_vocabulary(addressing, "ppal-read-track"))

    def test_sends_is_refused_here_before_the_client_refuses_it(self):
        rec = record("v24/v24_read_track_sends.json")
        self.assertEqual(rec["arguments"], {"include": ["sends"], "path": "t10"})
        for addressing in ("2.2", "2.3", "2.4"):
            with self.subTest(addressing=addressing), \
                    self.assertRaisesRegex(ValueError, "no include sends on Producer Pal"):
                pp.PPRequests(addressing).read_track(10, include=["sends"])
        self.assertEqual(pp.PPRequests("2.4").read_track(10, include=["mixer", "devices"]),
                         {"path": "t10", "include": ["mixer", "devices"]})
        with self.assertRaisesRegex(ValueError, "no include params on Producer Pal 2.4"):
            pp.PPRequests("2.4").read_scene(1, include=["params"])
        with self.assertRaisesRegex(ValueError, "no include notes"):
            pp.PPRequests("2.4").read_device("9", include=["notes"])
        # the captured refusal itself is the third error class, typed
        with self.assertRaises(pp.PPSchemaRefusal) as caught:
            reply_of("v24/v24_read_track_sends.json")
        self.assertIsInstance(caught.exception, pp.PPToolError)
        self.assertTrue(pp.is_schema_refusal(str(caught.exception)))
        with self.assertRaises(pp.PPToolError) as other:
            reply_of("v24/v24_read_track_missing.json")
        self.assertNotIsInstance(other.exception, pp.PPSchemaRefusal)

    def test_every_include_the_code_sends_is_known_on_every_build(self):
        import pathlib
        import re
        from mcp_server import moment
        root = pathlib.Path(__file__).resolve().parents[1] / "mcp_server"
        literal = re.compile(r'(?:"include"\s*:|\binclude\s*=)\s*\[([^\]]*)\]')
        sent: dict[str, set] = {}
        for path in sorted(root.glob("*.py")):
            for match in literal.finditer(path.read_text()):
                for flag in match.group(1).split(","):
                    flag = flag.strip().strip("\"'")
                    if flag:
                        sent.setdefault(flag, set()).add(path.name)
        self.assertTrue(sent)
        for addressing in ("2.2", "2.3", "2.4"):
            known = set().union(*(pp.read_include_vocabulary(addressing, tool)
                                  for tool in self.READ_TOOLS))
            for flag, files in sorted(sent.items()):
                with self.subTest(addressing=addressing, flag=flag):
                    self.assertIn(flag, known, "%s sent by %s" % (flag, sorted(files)))
        for addressing in ("2.2", "2.3", "2.4"):
            for tool, flags in (("ppal-read-track", pp.TRACK_READ_INCLUDE_INVENTORY),
                                ("ppal-read-track", pp.TRACK_READ_INCLUDE_SESSION),
                                ("ppal-read-track", moment.PIECE_TRACK_INCLUDE),
                                ("ppal-read-track", moment.PIECE_RETURN_INCLUDE),
                                ("ppal-read-track", moment.PIECE_MASTER_INCLUDE),
                                ("ppal-read-live-set", moment.PIECE_OVERVIEW_INCLUDE)):
                with self.subTest(addressing=addressing, tool=tool, flags=sorted(flags)):
                    pp.require_known_include(addressing, tool, flags)


if __name__ == "__main__":
    unittest.main()
