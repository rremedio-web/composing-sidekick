"""A Producer Pal write is judged by its reply's STRUCTURE (and the readbacks
that already follow it), never by words in its warnings (review M1).

Both real devices warn next to writes that HAPPENED (the project-context
backup sync on every call, colour quantization, a duplicated track losing its
Producer Pal device), and word lists both misfire on those and miss real
refusals. Texts: tests/pp_fakes.DEVICE_ADVISORY_WARNINGS /
DEVICE_REFUSAL_WARNINGS (from the device bundles of 2.2.0-rc1 and 2.3.0).
Every class runs on both builds.
"""
import asyncio
import json
import unittest
from unittest.mock import patch

from mcp_server import live_write as lw
from mcp_server import pp_codec
from mcp_server import pp_session as pp
from tests import pp_fakes
from tests.pp_fakes import V22, V23, V24, CapturedDevice, on_all_versions, on_both_versions


NOTES = [
    {"pitch": 60, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 100},
    {"pitch": 64, "start_beats": 1.0, "duration_beats": 1.0, "velocity": 90},
]


def setUpModule():
    pp.set_live_process_incarnation_resolver(
        lambda: {"pid": 4242, "start_time": "Sat Sep 26 10:00:00 2026"})


def tearDownModule():
    pp.set_live_process_incarnation_resolver(None)


def reply(payload, *warnings):
    """A device reply as pp_session.call_tool builds it (JSON part + warning parts)."""
    return pp.PPResponse(json.dumps({"result": payload}), payload, list(warnings))


def run(device, coro_fn, *args, **kwargs):
    with patch.object(pp, "operation_session", device.operation()):
        return asyncio.run(coro_fn(*args, **kwargs))


# Captured successful write replies, per build: (fixture, write_failed kwargs).
_SUCCESSES = {
    V22: [("mutate/duplicate_clip_to_arrangement.json",
           {"count": 1, "location": {"path": "t10", "arrangementStart": "5|1"}}),
          ("mutate/duplicate_track_without_clips.json", {"count": 1}),
          ("mutate/update_track_mute.json", {"count": 1, "ids": ["391"]}),
          ("mutate/delete_track.json", {"expect": "deleted", "ids": ["391"]}),
          ("legacy/legacy_create_track.json", {"count": 1}),
          ("legacy/legacy_create_clip_path.json",
           {"count": 1, "location": {"path": "t10/s1"}, "note_count": 1}),
          ("legacy/check_create_clip_arrangement.json",
           {"count": 1, "location": {"path": "t10", "arrangementStart": "3|1"},
            "note_count": 1}),
          ("legacy/check_delete_clip_by_id.json", {"expect": "deleted", "ids": ["378"]})],
    V23: [("v23/v23_duplicate_to_position.json",
           {"count": 1, "location": {"path": "t10[9|1]"}}),
          ("v23/v23_duplicate_trimmed.json", {"count": 1, "location": {"path": "t10[17|1]"}}),
          ("v23/v23_create_track_midi.json", {"count": 1}),
          ("v23/v23_create_clip_arrangement_path.json",
           {"count": 1, "location": {"path": "t10[1|1]"}, "note_count": 4}),
          ("v23/v23_move_clip.json", {"count": 1, "location": {"path": "t10[49|1]"}}),
          ("v23/v23_split_clip.json", {"count": 2}),
          ("v23/v23_mute_by_id.json", {"count": 1, "ids": ["423"]}),
          ("v23/v23_delete_by_id.json", {"expect": "deleted", "ids": ["438"]}),
          ("v23/v23_delete_by_path.json", {"expect": "deleted"}),
          ("v23/v23_delete_capture_tracks.json",
           {"expect": "deleted", "ids": ["423", "424"]}),
          ("mutate/duplicate_track_without_clips.json", {"count": 1})],
    # 2.4.0 (SCRATCH-capture, 2026-10-03): minimised replies, deletedPath-only deletes
    V24: [("v24/v24_duplicate_trimmed.json", {"count": 1, "location": {"path": "t10[13|1]"}}),
          ("v24/v24_create_tracks_batch.json", {"count": 2}),
          ("v24/v24_create_scenes_batch.json", {"count": 2}),
          ("v24/v24_create_arrangement_clip.json",
           {"count": 1, "location": {"path": "t10[1|1]"}, "note_count": 2}),
          ("v24/v24_create_session_clip.json",
           {"count": 1, "location": {"path": "t10/s0"}, "note_count": 2}),
          ("v24/v24_move_clip.json", {"count": 1, "location": {"path": "t10[17|1]"}}),
          ("v23/v23_split_clip.json", {"count": 2}),
          ("v24/v24_update_track_minimised.json", {"count": 1, "ids": ["529"]}),
          ("v23/v23_delete_by_id.json", {"expect": "deleted", "ids": ["429"]}),
          ("v24/v24_delete_clip_by_path.json", {"expect": "deleted"}),
          ("v24/v24_delete_capture_tracks.json",
           {"expect": "deleted", "ids": ["529", "530", "531"]}),
          ("mutate/duplicate_track_without_clips.json", {"count": 1})],
}


@on_all_versions
class AdvisoryWarningsNeverFailAWriteTests(unittest.TestCase):
    def warnings(self):
        return pp_fakes.DEVICE_ADVISORY_WARNINGS[self.PP_VERSION]

    def test_each_real_warning_next_to_each_captured_success_is_a_success(self):
        for relative, kwargs in _SUCCESSES[self.PP_VERSION]:
            payload = pp_fakes.captured_json(relative, self.PP_VERSION)
            for warning in self.warnings():
                with self.subTest(fixture=relative, warning=warning[:40]):
                    self.assertIsNone(pp.write_failed(reply(payload, warning), **kwargs))
            with self.subTest(fixture=relative, warning="all"):
                self.assertIsNone(pp.write_failed(reply(payload, *self.warnings()), **kwargs))

    def test_reads_do_not_go_incomplete_on_advisory_warnings(self):
        overview = pp_fakes.captured_json("legacy/legacy_live_set_after_create.json",
                                          self.PP_VERSION)
        self.assertEqual(pp.response_warnings_mean_incomplete(
            reply(overview, *self.warnings())), [])
        # a real truncation still counts
        self.assertTrue(pp.response_warnings_mean_incomplete(
            reply(overview, "WARNING: track listing truncated at 64 tracks")))

    def test_only_captured_advisories_are_exempt_from_the_timed_out_token(self):
        # review 2026-09-30 finding 5: the allow-list grows only from captures.
        # The backup-sync warning (device bundles, both builds) says "timed
        # out" and is exempt; an INVENTED warning that says the same is not —
        # fail-closed: an unfamiliar "timed out" marks the read incomplete
        # until a capture proves it unrelated to the inventory.
        overview = pp_fakes.captured_json("legacy/legacy_live_set_after_create.json",
                                          self.PP_VERSION)
        captured = ("WARNING: Project context backup sync failed: node_request "
                    "'projectContext.sync' timed out after 5000ms")
        self.assertTrue(pp.is_advisory_warning(captured))
        self.assertEqual(pp.response_warnings_mean_incomplete(reply(overview, captured)), [])
        for invented in ("WARNING: background metadata sync timed out",
                         "WARNING: track listing timed out",
                         "WARNING: track listing truncated"):
            with self.subTest(warning=invented):
                self.assertFalse(pp.is_advisory_warning(invented))
                self.assertEqual(pp.response_warnings_mean_incomplete(
                    reply(overview, invented)), [invented])
        # the docstring says so
        doc = pp.response_warnings_mean_incomplete.__doc__.lower()
        self.assertIn("grows only\n    from captures", doc)
        self.assertIn("fail-closed", doc)

    def test_a_composite_advisory_with_an_incomplete_tail_is_not_swallowed(self):
        # round 3: the allow-list matches a prefix; an advisory prefix followed
        # by an inventory failure must still count. The captured tails that
        # themselves say "timed out" / "timeout" stay exempt.
        overview = pp_fakes.captured_json("legacy/legacy_live_set_after_create.json",
                                          self.PP_VERSION)
        for composite in (
                "WARNING: Project context backup sync failed: session clip listing truncated",
                "WARNING: Project context backup sync failed: track listing timed out",
                "WARNING: Could not verify color quantization: clip listing partial",
                "WARNING: Requested clip color #FF6A00 was mapped to nearest palette "
                "color #FF3636. Session clip listing incomplete."):
            with self.subTest(warning=composite):
                self.assertTrue(pp.is_advisory_warning(composite))   # prefix still matches
                self.assertEqual(pp.response_warnings_mean_incomplete(
                    reply(overview, composite)), [composite])
        for captured in self.warnings():
            with self.subTest(warning=captured[:40]):
                self.assertEqual(pp.response_warnings_mean_incomplete(
                    reply(overview, captured)), [])

    def test_jam_write_lands_once_and_reports_the_warnings(self):
        device = CapturedDevice()
        device.device_warnings = list(self.warnings())
        receipt = run(device, lw.write_answer_to_jam_track, NOTES, "answer A", tempo=120)

        self.assertTrue(receipt["verified_ok"])
        self.assertEqual(receipt["verification_status"], "verified")
        self.assertEqual(receipt["pp_warnings"], self.warnings())
        self.assertEqual(len([c for c in device.calls if c[0] == "ppal-create-clip"]), 1)
        self.assertEqual(receipt["set_identity"]["live_root_id"], "1")
        self.assertIn("clip_ref", receipt)

    def test_booth_tracks_and_arrangement_writes_succeed_with_warnings(self):
        device = CapturedDevice()
        device.device_warnings = list(self.warnings())
        booth = run(device, lw.stage_named_clips, [{"name": "blind 1", "notes": NOTES}])
        self.assertTrue(booth["verified_ok"])
        self.assertEqual(booth["pp_warnings"], self.warnings())

        tracks = run(device, lw.write_tracks, [{"name": "bass", "notes": NOTES}], tempo=100)
        self.assertEqual(tracks["tracks"][0]["verification_status"], "verified")
        self.assertEqual(tracks["pp_warnings"], self.warnings())

        plan = {"tempo": 120, "total_bars": 8, "placements": [
            {"role": "lead", "section_index": 0, "arrangement_start_bar_beat": "5|1",
             "length_bars": 1, "notes": NOTES}]}
        arrangement = run(device, lw.write_arrangement, plan)
        self.assertEqual(arrangement["placements_written"], 1)
        self.assertEqual(arrangement["pp_warnings"], self.warnings())

    def test_inventory_stays_complete_with_warnings(self):
        device = CapturedDevice()
        device.device_warnings = list(self.warnings())
        inventory = run(device, lw.read_set_inventory)
        self.assertTrue(inventory["complete"], inventory["errors"])


@on_all_versions
class RealRefusalsFailTests(unittest.TestCase):
    def refusal(self, kind):
        return pp_fakes.DEVICE_REFUSAL_WARNINGS[self.PP_VERSION][kind]

    def test_structural_refusals_fail_and_name_the_device_warning(self):
        if self.PP_VERSION == V24:
            # 2.4 sends single-target refusals as MCP errors (PPToolError, see
            # tests/test_pp24_adapter.py); the two left in the reply's structure:
            # the split that cut nothing — ONE object + its WARNING
            # (v24_split_clip_deprecated_key) — and a delete miss, {detail:
            # "nothing to delete", id} with no warning (v24_delete_clip_missing_id).
            uncut = reply([{"id": "558", "path": "t10[25|1]"}], self.refusal("split"))
            self.assertIn("split cut nothing", pp.write_failed(uncut, count=2))
            self.assertIsNotNone(pp.write_failed(uncut, count=2))
            missed = pp.write_failed(reply({"detail": "nothing to delete", "id": "445"}),
                                     expect="deleted", ids=["445"])
            self.assertIn("did not delete 445", missed)
            self.assertIn("nothing to delete", missed)
            return
        cases = [
            ([], "move", {"count": 1}),                               # nothing moved
            ([{"id": "446", "path": "t10[57|1]"}], "split", {"count": 2}),  # nothing cut
            ([], "update", {"count": 1}),                             # not updated
            ({"deleted": False, "id": "445", "type": "clip"}, "delete",
             {"expect": "deleted", "ids": ["445"]}),
            ([], "duplicate", {"count": 1}),                          # captured on 2.3
        ]
        for payload, kind, kwargs in cases:
            with self.subTest(kind=kind):
                failure = pp.write_failed(reply(payload, self.refusal(kind)), **kwargs)
                self.assertIsNotNone(failure)
                self.assertIn(self.refusal(kind).split("WARNING: ", 1)[1][:30], failure)

    def test_a_reply_at_the_wrong_place_or_with_other_notes_fails(self):
        self.assertIn("placed", pp.write_failed(
            reply({"id": "9", "path": "t10/s2", "noteCount": 2}),
            count=1, location={"path": "t10/s1"}, note_count=2))
        self.assertIn("noteCount", pp.write_failed(
            reply({"id": "9", "path": "t10/s1", "noteCount": 1}),
            count=1, location={"path": "t10/s1"}, note_count=2))
        self.assertIsNotNone(pp.write_failed(
            reply({"id": "9", "path": "t10[9|1]"}), count=1, location={"path": "t10[5|1]"}))
        self.assertIsNotNone(pp.write_failed(
            reply({"arrangementStart": "9|1", "id": "9", "path": "t10"}),
            count=1, location={"path": "t10", "arrangementStart": "5|1"}))
        self.assertIsNotNone(pp.write_failed(reply({"id": "7"}), count=1, ids=["8"]))

    def test_a_deletion_that_left_the_clip_is_not_a_delete(self):
        self.assertIsNotNone(pp.write_failed(
            reply({"deleted": True, "id": "1", "type": "clip"}), expect="deleted",
            ids=["1", "2"]))

    def test_a_refused_jam_create_names_the_warning_and_the_slot(self):
        device = CapturedDevice()
        if self.PP_VERSION == V24:
            # 2.4 refuses the create with an MCP error (2.4.0/v23b/
            # v23b_refuse_create_midi_on_audio): nothing landed, the error is
            # the verdict and carries the device's text
            device.handlers["ppal-create-clip"] = lambda args: pp_fakes.error_response(
                pp_fakes.DEVICE_REFUSAL_ERRORS[V24]["create"])
            before = dict(device.clips)
            with self.assertRaises(pp.PPToolError) as caught:
                run(device, lw.write_answer_to_jam_track, NOTES, "answer X")
            self.assertIn("is audio; a MIDI clip needs a MIDI track", str(caught.exception))
            self.assertEqual(device.clips, before)
            return
        refusal = self.refusal("duplicate").replace("duplicated", "created")
        original = device.handlers["ppal-create-clip"]

        def refuse(args):
            original(args)       # the clip lands, but the reply says nothing useful
            return {"content": [{"json": []}, {"text": refusal}], "is_error": False}

        device.handlers["ppal-create-clip"] = refuse
        with self.assertRaises(lw.LiveWriteVerificationError) as caught:
            run(device, lw.write_answer_to_jam_track, NOTES, "answer X")
        detail = caught.exception.detail
        self.assertIn(refusal, detail)
        self.assertIn("may already be at SIDEKICK JAM slot 11/0", detail)


class WarningOnlyReplyTests(unittest.TestCase):
    def test_warning_only_reply_keeps_the_warning_and_says_what_happened(self):
        from types import SimpleNamespace

        text = pp_fakes.DEVICE_REFUSAL_WARNINGS[V23]["move"]

        class Raw:
            async def call_tool(self, name, arguments):
                return SimpleNamespace(isError=False, content=[SimpleNamespace(text=text)])

        response = asyncio.run(pp.call_tool(Raw(), "ppal-update-clip", {"id": "1"}))
        self.assertEqual(response.warnings, [text])
        self.assertIsNone(response.result)
        self.assertTrue(response.warning_only)
        failure = pp.write_failed(response, count=1)
        self.assertIn("warnings only", failure)
        self.assertIn("was not moved", failure)
        issues = pp.response_warnings_mean_incomplete(response)
        self.assertTrue(any("warnings only" in issue for issue in issues))


def v23b(name):
    return pp_fakes.load_fixture("v23b/%s.json" % name, V23)


def v23b_reply(name):
    record = v23b(name)

    class Raw:
        async def call_tool(self, tool, arguments):
            return pp_fakes.mcp_result(record["response"])

    return asyncio.run(pp.call_tool(Raw(), record["tool"], record["arguments"]))


class V23bCapturedVerdictTests(unittest.TestCase):
    """The v23b 2.3.0 captures (SCRATCH-capture): refusal, scene, lane shapes."""

    def test_the_captured_refusal_fails_by_structure_while_failed_backups_do_not(self):
        refused = v23b_reply("v23b_refuse_create_midi_on_audio")
        args = v23b("v23b_refuse_create_midi_on_audio")["arguments"]
        failure = pp.write_failed(refused, count=1, location=args, note_count=2)
        self.assertIn("no object", failure)
        self.assertIn("Failed to create clip at t11[1|1]", failure)
        # "Failed" also opens the harmless backup warning next to a good create
        good = v23b_reply("v23b_create_main_clip")
        good.warnings.append(pp_fakes.DEVICE_ADVISORY_WARNINGS[V23][0])
        self.assertIsNone(pp.write_failed(good, count=1, location=v23b(
            "v23b_create_main_clip")["arguments"], note_count=2))

    def test_captured_successes_and_the_lane_delete_refusal(self):
        for name, kwargs in (
                ("v23b_create_lane_clip", {"count": 1, "note_count": 2,
                                           "location": {"path": "t10/l0[5|1]"}}),
                ("v23b_create_scene_append", {"count": 1}),
                ("v23b_promote_lane_clip", {"count": 1, "location": {"path": "t10[9|1]"}}),
                ("v23b_delete_scene", {"expect": "deleted", "ids": ["364"]}),
                ("v23b_delete_capture_tracks", {"expect": "deleted", "ids": ["362", "363"]})):
            with self.subTest(name=name):
                self.assertIsNone(pp.write_failed(v23b_reply(name), **kwargs))
        lane_delete = pp.write_failed(v23b_reply("v23b_delete_lane_clip"),
                                      expect="deleted", ids=["376"])
        self.assertIn("did not delete 376", lane_delete)
        self.assertIn("cannot delete take-lane clip", lane_delete)

    def test_lane_and_promotion_notices_are_advisory(self):
        for name in ("v23b_create_lane_clip", "v23b_promote_lane_clip"):
            with self.subTest(name=name):
                warnings = v23b_reply(name).warnings
                self.assertTrue(warnings)
                self.assertTrue(all(pp.is_advisory_warning(w) for w in warnings), warnings)
        self.assertFalse(pp.is_advisory_warning(v23b_reply("v23b_delete_lane_clip").warnings[0]))

    def test_scene_and_promotion_request_forms_equal_the_captures(self):
        v23 = pp.PPRequests("2.3")
        append = dict(v23b("v23b_create_scene_append")["arguments"])
        append.pop("name")
        self.assertEqual(v23.append_scenes(count=1, scene_count=8), append)
        self.assertEqual(v23.read_scene(8), v23b("v23b_read_scene_by_path")["arguments"])
        self.assertEqual(v23.play_scene(8), v23b("v23b_play_scene_by_path")["arguments"])
        self.assertEqual(v23.stop(), v23b("v23b_stop")["arguments"])
        self.assertEqual(pp.PPRequests("2.2").stop(), v23b("v23b_stop")["arguments"])
        self.assertEqual(v23.duplicate_clip_to_arrangement("376", track=10, start="9|1"),
                         v23b("v23b_promote_lane_clip")["arguments"])
        self.assertEqual(v23.create_clip(track=10, arrangement_start="1|1", length="2bar",
                                         looping=True, name="M",
                                         notes=v23b("v23b_create_main_clip")["arguments"]["notes"]),
                         v23b("v23b_create_main_clip")["arguments"])

    def test_arrangement_selection_and_clip_read_by_id(self):
        selection = pp.extract_selected_context(v23b_reply("v23b_selection_readback"))
        self.assertEqual(selection["selected_clip"]["id"], "372")
        self.assertEqual(selection["selected_track"]["track_index"], 10)
        self.assertEqual(selection["selected_scene"]["scene_index"], 13)
        clip = pp.clip_record(v23b_reply("v23b_read_arrangement_clip_by_id"))
        self.assertEqual(pp.arrangement_start_of(clip), "1|1")
        self.assertEqual(pp.clip_track_index(clip), 10)
        self.assertEqual(len(pp.extract_notes(v23b_reply("v23b_read_arrangement_clip_by_id"))), 2)
        from mcp_server import piece_snapshot
        chosen = piece_snapshot._selection(pp_fakes.captured_json(
            "v23b/v23b_selection_readback.json", V23))
        self.assertEqual(chosen["arrangement_clip"], {"id": "372", "start": "1|1",
                                                      "track_index": 10})

        selected = v23b_reply("v23b_selection_readback")

        class Selecting:
            capabilities, handshake = {}, {"pp_version": V23}

            async def call(self, name, args=None):
                return selected

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def operation(*_args, **_kwargs):
            yield Selecting()

        with patch.object(pp, "operation_session", operation):
            self.assertEqual(asyncio.run(lw.selected_arrangement_bar()), 1)

    def test_lane_clips_live_only_under_take_lanes(self):
        track = pp_fakes.captured_json("v23b/v23b_read_track_with_lanes.json", V23)
        self.assertEqual([pp.arrangement_start_of(c) for c in track["arrangementClips"]],
                         ["1|1"])
        lane_clip = track["takeLanes"][0]["clips"][0]
        self.assertTrue(pp_codec.is_lane_object_path(lane_clip["path"]))
        normalized = pp.normalize_clip(lane_clip)
        self.assertEqual((normalized["take_lane"], normalized["arrangement_start"],
                          normalized["track_index"]), (0, "5|1", 10))
        record = v23b("v23b_read_track_with_lanes")
        self.assertEqual(pp_fakes.v22_payload_as_v23(
            record["tool"], pp_fakes.v23_request_as_v22(record["tool"], record["arguments"]),
            track), track)


@on_all_versions
class NoScenesAsASideEffectTests(unittest.TestCase):
    """2.3 creates missing scenes when a clip goes to a slot past the last
    one (v23b_refuse_create_past_last_scene); 2.4 too, and says so
    (`created: "s11-s13"`, v24_create_clip_past_last_scene); 2.2 refused.
    JAM/BOOTH writes append scenes first ('s+', verified by re-reading
    sceneCount) and never target a slot past the last scene."""

    def full_jam(self):
        device = CapturedDevice()
        jam = device.add_track(lw.JAM_TRACK_NAME)
        scenes = device.live_set["sceneCount"]
        for scene in range(scenes):
            device.add_clip("t%d/s%d" % (jam["trackIndex"], scene), "earlier",
                            pp_codec.encode_notes(NOTES, pp_codec.CodecMode.MIDI_JSON))
        return device, jam, scenes

    def test_the_device_fake_reproduces_the_side_effect(self):
        device = CapturedDevice()
        scenes = device.live_set["sceneCount"]
        reply = device.call_tool_sync("ppal-create-clip", {
            "path": "t0/s%d" % (scenes + 5), "notes": "[]"})
        if self.PP_VERSION in pp_fakes.PATH_VERSIONS:
            self.assertFalse(reply["is_error"])
            self.assertEqual(device.live_set["sceneCount"], scenes + 6)
        else:
            self.assertTrue(reply["is_error"])
            self.assertEqual(device.live_set["sceneCount"], scenes)

    def test_jam_appends_exactly_one_scene_before_the_clip(self):
        device, jam, scenes = self.full_jam()
        receipt = run(device, lw.write_answer_to_jam_track, NOTES, "answer S")
        self.assertEqual(receipt["slot"], "%d/%d" % (jam["trackIndex"], scenes))
        self.assertEqual(device.live_set["sceneCount"], scenes + 1)
        tools = [name for name, _ in device.calls]
        self.assertLess(tools.index("ppal-create-scene"), tools.index("ppal-create-clip"))

    def test_an_append_that_did_not_take_writes_no_clip(self):
        device, jam, scenes = self.full_jam()
        device.ignore_scene_creation = True
        with self.assertRaisesRegex(lw.LiveWriteVerificationError, "scenes after appending"):
            run(device, lw.write_answer_to_jam_track, NOTES, "answer T")
        self.assertNotIn("ppal-create-clip", [name for name, _ in device.calls])
        self.assertEqual(device.live_set["sceneCount"], scenes)


if __name__ == "__main__":
    unittest.main()
