"""live_write call sites driven through the schema-strict captured device.

Every Producer Pal call below is checked against tool_schemas.json captured
from 2.2.0-rc1 and, in each ``…PP23`` / ``…PP24`` twin, from 2.3.0 / 2.4.0
(tests/pp_fakes.py); responses use that build's captured record shapes. ``device.calls`` holds
the 2.2-equivalent arguments on both builds; ``device.wire_calls`` what was
actually sent.
"""
import asyncio
import json
import unittest
from unittest.mock import patch

from mcp_server import live_write as lw
from mcp_server import pp_codec
from mcp_server import pp_session as pp
from tests.pp_fakes import (V22, V23, V24, PATH_VERSIONS, CapturedDevice, captured_json,
                            on_all_versions)


NOTES = [
    {"pitch": 60, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 100},
    {"pitch": 64, "start_beats": 1.0, "duration_beats": 1.0, "velocity": 90},
]


def setUpModule():
    pp.set_live_process_incarnation_resolver(
        lambda: {"pid": 4242, "start_time": "Sat Sep 26 10:00:00 2026"})


def tearDownModule():
    pp.set_live_process_incarnation_resolver(None)


def run(device, coro_fn, *args, **kwargs):
    with patch.object(pp, "operation_session", device.operation()):
        return asyncio.run(coro_fn(*args, **kwargs))


def tools(device):
    return [name for name, _args in device.calls]


def args_for(device, name):
    return [args for tool, args in device.calls if tool == name]


def wire_for(device, name):
    return [args for tool, args in device.wire_calls if tool == name]


@on_all_versions
class JamWriteTests(unittest.TestCase):
    def test_creates_jam_track_by_id_and_writes_by_path(self):
        device = CapturedDevice()
        receipt = run(device, lw.write_answer_to_jam_track, NOTES, "answer A", tempo=120)

        new_index = 11  # captured Set has 11 regular tracks
        self.assertTrue(receipt["verified_ok"])
        self.assertTrue(receipt["track_created"])
        self.assertEqual(receipt["slot"], "%d/0" % new_index)
        self.assertEqual(receipt["verified_noteCount"], 2)
        update = args_for(device, "ppal-update-track")[0]
        self.assertEqual(update["id"], device.tracks[new_index]["id"])
        self.assertEqual(update["name"], lw.JAM_TRACK_NAME)
        self.assertEqual(args_for(device, "ppal-create-clip")[0]["path"],
                         "t%d/s0" % new_index)
        # the slot is read before the create (free on every build: 2.2 {id: null},
        # 2.4 "no clip at" — v24_create_session_clip_replaces is why), then verified
        self.assertEqual(args_for(device, "ppal-read-clip")[:2], [
            {"path": "t%d/s0" % new_index, "include": []},
            {"path": "t%d/s0" % new_index, "include": ["notes"]}])
        self.assertEqual(args_for(device, "ppal-select")[0],
                         {"path": "t%d/s0" % new_index, "view": "session"})
        self.assertTrue(receipt["focused"])
        if self.PP_VERSION in PATH_VERSIONS:   # the forms sent (v23_create_track_midi, v23_mute_by_id)
            self.assertEqual(wire_for(device, "ppal-create-track"), [{"path": "t+"}])
            self.assertEqual(wire_for(device, "ppal-read-track")[:2], [
                {"path": "t%d" % new_index, "include": []},
                {"path": "t%d" % new_index, "include": ["session-clips"]}])
        else:
            self.assertEqual(wire_for(device, "ppal-create-track"), [{"count": 1}])
        self.assertEqual(wire_for(device, "ppal-update-track"),
                         args_for(device, "ppal-update-track"))
        # Identity and clip_ref come from the captured live-api / read shapes.
        self.assertEqual(receipt["set_identity"]["live_root_id"], "1")
        self.assertEqual(receipt["clip_ref"]["clip_id"],
                         device.clips["t%d/s0" % new_index]["id"])

    def test_existing_jam_track_appends_after_last_used_slot(self):
        device = CapturedDevice()
        jam = device.add_track(lw.JAM_TRACK_NAME)
        index = jam["trackIndex"]
        device.add_clip("t%d/s0" % index, "earlier", pp_codec.encode_notes(
            NOTES, pp_codec.CodecMode.MIDI_JSON))
        receipt = run(device, lw.write_answer_to_jam_track, NOTES, "answer B")

        self.assertFalse(receipt["track_created"])
        self.assertEqual(receipt["slot"], "%d/1" % index)
        self.assertNotIn("ppal-create-track", tools(device))
        self.assertEqual(args_for(device, "ppal-read-track")[0],
                         {"trackIndex": index, "include": ["session-clips"]})
        if self.PP_VERSION in PATH_VERSIONS:
            self.assertEqual(wire_for(device, "ppal-read-track")[0],
                             {"path": "t%d" % index, "include": ["session-clips"]})
        self.assertEqual(args_for(device, "ppal-create-clip")[0]["path"],
                         "t%d/s1" % index)

    def test_full_jam_track_appends_a_scene_first(self):
        device = CapturedDevice()
        jam = device.add_track(lw.JAM_TRACK_NAME)
        index = jam["trackIndex"]
        scenes = device.live_set["sceneCount"]
        for scene in range(scenes):
            device.add_clip("t%d/s%d" % (index, scene), "earlier", pp_codec.encode_notes(
                NOTES, pp_codec.CodecMode.MIDI_JSON))
        receipt = run(device, lw.write_answer_to_jam_track, NOTES, "answer C")

        self.assertEqual(receipt["slot"], "%d/%d" % (index, scenes))
        self.assertEqual(device.live_set["sceneCount"], scenes + 1)
        # 2.2: sceneIndex = the scene count (schema: shifts nothing at the end);
        # 2.3: count + path 's+' (v23b_create_scene_append); 2.4: path 's+' alone
        # (`count` left the schema; 2.4.0/v23b/v23b_create_scene_append).
        self.assertEqual(wire_for(device, "ppal-create-scene"), [
            {"count": 1, "capture": False, "path": "s+"} if self.PP_VERSION == V23
            else {"capture": False, "path": "s+"} if self.PP_VERSION == V24
            else {"count": 1, "capture": False, "sceneIndex": scenes}])
        self.assertEqual(args_for(device, "ppal-create-clip")[0]["path"],
                         "t%d/s%d" % (index, scenes))


@on_all_versions
class BoothAndTrackWriteTests(unittest.TestCase):
    def test_stage_named_clips_appends_by_path_with_id_rename(self):
        device = CapturedDevice()
        clips = [{"name": "blind 1", "notes": NOTES},
                 {"name": "blind 2", "notes": NOTES[:1], "tempo": 96}]
        summary = run(device, lw.stage_named_clips, clips)

        index = summary["track_index"]
        self.assertTrue(summary["track_created"])
        self.assertEqual([item["slot"] for item in summary["placed"]],
                         ["%d/0" % index, "%d/1" % index])
        self.assertEqual(args_for(device, "ppal-update-track")[0]["id"],
                         device.tracks[index]["id"])
        self.assertEqual([a["path"] for a in args_for(device, "ppal-create-clip")],
                         ["t%d/s0" % index, "t%d/s1" % index])
        self.assertEqual(device.tracks[index]["name"], lw.BOOTH_TRACK_NAME)

    def test_write_tracks_places_each_clip_by_path(self):
        device = CapturedDevice()
        summary = run(device, lw.write_tracks,
                      [{"name": "bass", "notes": NOTES},
                       {"name": "lead", "notes": NOTES, "color": "#112233"}],
                      tempo=100, set_tempo=True)

        self.assertEqual([t["track_index"] for t in summary["tracks"]], [11, 12])
        self.assertEqual(args_for(device, "ppal-update-live-set")[0], {"tempo": 100.0})
        self.assertEqual([a["path"] for a in args_for(device, "ppal-create-clip")],
                         ["t11/s0", "t12/s0"])
        self.assertTrue(all(t["verification_status"] == "verified"
                            for t in summary["tracks"]))


@on_all_versions
class ReadTests(unittest.TestCase):
    def test_read_current_clip_reads_captured_selected_slot_by_path(self):
        device = CapturedDevice()
        # read/select_current.json selects track 0 / scene 0 with no clip.
        device.add_clip("t0/s0", "selected", pp_codec.encode_notes(
            NOTES, pp_codec.CodecMode.MIDI_JSON))
        result = run(device, lw.read_current_clip)

        self.assertEqual(result["source"], "selected_slot")
        self.assertEqual(result["read_args"], {"include": ["notes", "timing"],
                                               "path": "t0/s0"})
        self.assertIsNone(result["read_error"])
        self.assertEqual(result["n_notes"], 2)

    def test_read_current_clip_reads_selected_clip_by_id(self):
        device = CapturedDevice()
        clip = device.add_clip("t9/s0", "picked", pp_codec.encode_notes(
            NOTES, pp_codec.CodecMode.MIDI_JSON))
        # selectedClip is not in the capture; keys are those
        # pp_session.extract_selected_context reads.
        device.selection = {**captured_json("read/select_current.json"),
                            "selectedClip": {"id": clip["id"], "path": "t9/s0"}}
        result = run(device, lw.read_current_clip)

        self.assertEqual(result["source"], "selected_clip")
        self.assertEqual(args_for(device, "ppal-read-clip")[0],
                         {"include": ["notes", "timing"], "id": clip["id"]})
        self.assertEqual(result["n_notes"], 2)

    def test_read_arrangement_room_lists_by_track_index_and_reads_by_id(self):
        device = CapturedDevice()
        listing = captured_json("mutate/read_track_after_dup.json")
        captured_clip = dict(listing["arrangementClips"][0])
        # arrangementLength is the value the captured duplicate call placed
        # (mutate/duplicate_clip_to_arrangement.json); the capture read had no timing.
        captured_clip["arrangementLength"] = "2bar"
        captured_clip["type"] = "midi"
        device.arrangement[10] = [captured_clip]
        room = run(device, lw.read_arrangement_room, [10], 16.0)  # bar 5

        self.assertEqual(args_for(device, "ppal-read-track")[0],
                         {"trackIndex": 10, "include": ["arrangement-clips", "timing"]})
        if self.PP_VERSION in PATH_VERSIONS:
            self.assertEqual(wire_for(device, "ppal-read-track")[0],
                             {"path": "t10", "include": ["arrangement-clips", "timing"]})
        self.assertEqual(args_for(device, "ppal-read-clip")[0],
                         {"id": "402", "include": ["notes"]})
        self.assertEqual(room[0]["clip_id"], "402")
        self.assertEqual(len(room[0]["notes"]), 4)

    def test_read_clip_session_slot_uses_path(self):
        device = CapturedDevice()
        device.add_clip("t9/s0", "10-Omnisphere 2", captured_json(
            "mutate/read_track_source.json")["sessionClips"][0]["notes"])
        result = run(device, lw.read_clip, 9, 0)

        self.assertEqual(args_for(device, "ppal-read-clip")[0],
                         {"path": "t9/s0", "include": ["notes", "timing"]})
        self.assertEqual(result["n_notes"] + result["n_hidden"], 4)

    def test_read_jam_clips_lists_the_jam_track_by_index(self):
        device = CapturedDevice()
        jam = device.add_track(lw.JAM_TRACK_NAME)
        device.add_clip("t%d/s0" % jam["trackIndex"], "a", "[]")
        clips = run(device, lw.read_jam_clips)

        self.assertEqual(args_for(device, "ppal-read-track")[0],
                         {"trackIndex": jam["trackIndex"], "include": ["session-clips"]})
        self.assertEqual(len(clips), 1)

    def test_read_arrangement_tracks_reads_each_listed_track(self):
        device = CapturedDevice()
        listing = captured_json("mutate/read_track_after_dup.json")
        device.arrangement[10] = [dict(listing["arrangementClips"][0])]
        result = run(device, lw.read_arrangement_tracks)

        reads = args_for(device, "ppal-read-track")
        midi = [t["trackIndex"] for t in device.tracks
                if t.get("type") == "midi" and not t.get("isGroup")]
        self.assertEqual([a["trackIndex"] for a in reads], midi)
        lead = next(t for t in result["tracks"] if t["track_index"] == 10)
        self.assertEqual([lw.pp_session.arrangement_start_of(c)
                          for c in lead["arrangement_clips"]], ["5|1"])


@on_all_versions
class ArrangementAndDeleteTests(unittest.TestCase):
    def test_write_arrangement_places_clips_at_their_positions(self):
        device = CapturedDevice()
        plan = {"tempo": 120, "total_bars": 8, "placements": [
            {"role": "lead", "section_index": 0, "arrangement_start_bar_beat": "5|1",
             "length_bars": 1, "notes": NOTES}]}
        summary = run(device, lw.write_arrangement, plan)

        self.assertEqual(summary["placements_written"], 1)
        self.assertEqual(summary["tracks"]["lead"]["track_index"], 11)
        create = args_for(device, "ppal-create-clip")[0]
        self.assertEqual((create["path"], create["arrangementStart"]), ("t11", "5|1"))
        wire = wire_for(device, "ppal-create-clip")[0]
        if self.PP_VERSION in PATH_VERSIONS:   # v23_create_clip_arrangement_path.json form
            self.assertEqual(wire["path"], "t11[5|1]")
            self.assertNotIn("arrangementStart", wire)
        else:                        # legacy/check_create_clip_arrangement.json form
            self.assertEqual((wire["path"], wire["arrangementStart"]), ("t11", "5|1"))
        self.assertEqual(summary["verified_placements"][0]["verification_status"], "verified")

    def _jam_with_clip(self):
        device = CapturedDevice()
        jam = device.add_track(lw.JAM_TRACK_NAME)
        clip = device.add_clip("t%d/s0" % jam["trackIndex"], "answer", pp_codec.encode_notes(
            NOTES, pp_codec.CodecMode.MIDI_JSON))
        identity = run(device, lw.read_set_identity)
        return device, jam, clip, identity

    def test_delete_jam_clip_deletes_by_id_and_verifies_absence(self):
        device, jam, clip, identity = self._jam_with_clip()
        result = run(device, lw.delete_jam_clip, "%d/0" % jam["trackIndex"],
                     expected_clip_id=clip["id"], expected_track_id=jam["id"],
                     expected_set_identity=identity)

        self.assertTrue(result["deleted"], result)
        self.assertEqual(result["delete_receipt"]["deleted_ids"], [clip["id"]])
        self.assertEqual(wire_for(device, "ppal-delete"), [{"type": "clip", "id": clip["id"]}])
        self.assertEqual(result["after_state"]["status"], "absent")
        # proof (a): the device's exact missing-id reply for THIS id
        # (legacy_read_clip_missing_id.json of this build)
        self.assertEqual(result["after_state"]["method"], "missing_id_reply")

    # Review 2026-09-30 finding 1 (merge gate): after a delete, absence is
    # proven only by (a) the captured missing-id reply for THIS id or (b) a
    # complete same-Set JAM inventory without it. Any other failed readback —
    # a timeout whose text happens to say "no clip", an empty-slot error for
    # another address, another id's missing reply — proves nothing by itself.
    def _failed_readback_texts(self):
        other = ('Error: id "999" does not exist' if self.PP_VERSION != V22 else
                 'Error executing tool \'ppal-read-clip\': readClip failed: '
                 'id "999" does not exist')
        return ["Error: no clip data could be retrieved: read timed out",
                "Error: no clip at t3/s2", 'Error: no scene at "s2"', other]

    def _delete_with_failed_readback(self, text, *, refuse=False, incomplete=False):
        device, jam, clip, identity = self._jam_with_clip()
        device.refuse_deletes = refuse
        device.read_clip_error_after_delete = text
        if incomplete:
            device.read_track_warnings_after_delete = [
                "WARNING: session clip listing truncated"]
        result = run(device, lw.delete_jam_clip, "%d/0" % jam["trackIndex"],
                     expected_clip_id=clip["id"], expected_track_id=jam["id"],
                     expected_set_identity=identity)
        return device, jam, clip, result

    def test_refused_delete_with_a_failed_readback_is_not_a_delete(self):
        if self.PP_VERSION == V22:
            self.skipTest("2.2.0-rc1 has no captured delete refusal")
        # the review's probe: the delete is refused, then the readback raises
        # "… no clip data could be retrieved: read timed out" (an injected
        # sentence, not a capture) — b10 @ d4f8080 answered deleted:true
        for text in self._failed_readback_texts():
            with self.subTest(text=text):
                device, jam, clip, result = self._delete_with_failed_readback(
                    text, refuse=True)
                self.assertFalse(result["deleted"], result)
                self.assertIn("t%d/s0" % jam["trackIndex"], device.clips)
                # proof (b) finds it: the complete JAM inventory still lists it
                self.assertEqual(result["after_state"]["status"], "present")
                self.assertEqual(result["after_state"]["method"], "jam_inventory")
                self.assertEqual(result["reason"], "clip still present after delete")
                self._assert_refused_receipt(result["delete_receipt"])
            with self.subTest(text=text, inventory="incomplete"):
                device, jam, clip, result = self._delete_with_failed_readback(
                    text, refuse=True, incomplete=True)
                self.assertFalse(result["deleted"], result)
                self.assertIn("t%d/s0" % jam["trackIndex"], device.clips)
                self.assertEqual(result["after_state"]["status"], "unknown")
                self.assertIn("inventory incomplete", result["reason"])
                self.assertIn(text, result["reason"])
                self.assertIn("the delete receipt says", result["reason"])

    def test_a_failed_readback_alone_never_proves_absence(self):
        # the delete DID happen here; with the inventory incomplete the
        # outcome is unknown, never deleted:true, whatever the error text says
        for text in self._failed_readback_texts():
            with self.subTest(text=text):
                device, jam, clip, result = self._delete_with_failed_readback(
                    text, incomplete=True)
                self.assertFalse(result["deleted"], result)
                self.assertEqual(result["after_state"]["status"], "unknown")
                self.assertIn("inventory incomplete", result["reason"])
                self.assertIn(text, result["reason"])
                self.assertEqual(result["delete_receipt"]["deleted_ids"], [clip["id"]])

    # Round 3 (adversarial re-review). BLOCKER 1: a non-error {id: null}
    # readback BY ID is not an absence proof — the only captured {id: null}
    # shape is a slot read by PATH (check_read_clip_existing_empty_slot.json).
    def _refused_delete_with_empty_id_readback(self, *, incomplete=False):
        device, jam, clip, identity = self._jam_with_clip()
        device.refuse_deletes = True
        device.read_clip_empty_after_delete = True
        if incomplete:
            device.read_track_warnings_after_delete = [
                "WARNING: session clip listing truncated"]
        result = run(device, lw.delete_jam_clip, "%d/0" % jam["trackIndex"],
                     expected_clip_id=clip["id"], expected_track_id=jam["id"],
                     expected_set_identity=identity)
        self.assertIn("t%d/s0" % jam["trackIndex"], device.clips)   # the clip stayed
        return result

    def test_an_id_null_readback_by_id_never_proves_absence(self):
        if self.PP_VERSION == V22:
            self.skipTest("2.2.0-rc1 has no captured delete refusal")
        # the reviewer's fake: refused delete, then read-clip by id replies
        # {id: null, name: null, path: "t11/s0", type: null} + WARNING "no
        # clip at t11/s0" — b10 @ d2574d0 answered deleted:true, method readback
        result = self._refused_delete_with_empty_id_readback()
        self.assertFalse(result["deleted"], result)
        self.assertEqual(result["after_state"]["status"], "present")
        self.assertEqual(result["after_state"]["method"], "jam_inventory")
        # … and with the JAM inventory incomplete the outcome is unknown
        result = self._refused_delete_with_empty_id_readback(incomplete=True)
        self.assertFalse(result["deleted"], result)
        self.assertEqual(result["after_state"]["status"], "unknown")
        self.assertIn("inventory incomplete", result["reason"])
        self.assertIn("the delete receipt says", result["reason"])

    def test_an_id_null_readback_with_a_complete_inventory_is_proof_b(self):
        # the delete happened; the readback alone still decides nothing, the
        # complete JAM inventory without the id does
        device, jam, clip, identity = self._jam_with_clip()
        device.read_clip_empty_after_delete = True
        result = run(device, lw.delete_jam_clip, "%d/0" % jam["trackIndex"],
                     expected_clip_id=clip["id"], expected_track_id=jam["id"],
                     expected_set_identity=identity)
        self.assertTrue(result["deleted"], result)
        self.assertEqual(result["after_state"]["method"], "jam_inventory")
        device, jam, clip, identity = self._jam_with_clip()
        device.read_clip_empty_after_delete = True
        device.read_track_warnings_after_delete = ["WARNING: session clip listing truncated"]
        result = run(device, lw.delete_jam_clip, "%d/0" % jam["trackIndex"],
                     expected_clip_id=clip["id"], expected_track_id=jam["id"],
                     expected_set_identity=identity)
        self.assertFalse(result["deleted"], result)
        self.assertEqual(result["after_state"]["status"], "unknown")

    # BLOCKER 2: the inventory proof must be bound to the JAM track by id,
    # not to the index it had before the delete.
    def test_the_inventory_is_read_by_jam_id_not_by_its_old_index(self):
        if self.PP_VERSION == V22:
            self.skipTest("2.2.0-rc1 has no captured delete refusal")
        # the reviewer's fake: refused delete, the readback times out, and
        # during the stall a track is inserted at the top of the Set (Set
        # identity unchanged) — the track now at JAM's old index has no such
        # clip. b10 @ d2574d0 read that track and answered deleted:true.
        device, jam, clip, identity = self._jam_with_clip()
        old_index = jam["trackIndex"]
        device.refuse_deletes = True
        device.read_clip_error_after_delete = "Error: no clip data could be retrieved: read timed out"
        device.after_delete_hook = lambda: device.insert_track_at_top("his new track")
        result = run(device, lw.delete_jam_clip, "%d/0" % old_index,
                     expected_clip_id=clip["id"], expected_track_id=jam["id"],
                     expected_set_identity=identity)
        self.assertFalse(result["deleted"], result)
        self.assertEqual(result["after_state"]["status"], "present")
        self.assertEqual(result["after_state"]["method"], "jam_inventory")
        self.assertEqual(jam["trackIndex"], old_index + 1)          # JAM moved down
        self.assertIn("t%d/s0" % (old_index + 1), device.clips)
        self.assertNotIn("t%d/s0" % old_index, device.clips)
        reads = [a for n, a in device.wire_calls if n == "ppal-read-track"]
        self.assertEqual(reads[-1]["id"], jam["id"])          # by id, both builds

    def test_an_inventory_of_another_track_is_unknown_not_absence(self):
        # a device that answers an id read with another track's record: the
        # listing's id is not JAM's — unknown, with the reason, never absent
        device, jam, clip, identity = self._jam_with_clip()
        device.read_clip_error_after_delete = "Error: no clip data could be retrieved: read timed out"
        device.read_track_id_alias = {jam["id"]: device.tracks[0]["id"]}
        result = run(device, lw.delete_jam_clip, "%d/0" % jam["trackIndex"],
                     expected_clip_id=clip["id"], expected_track_id=jam["id"],
                     expected_set_identity=identity)
        self.assertFalse(result["deleted"], result)
        self.assertEqual(result["after_state"]["status"], "unknown")
        self.assertIn("not SIDEKICK JAM", result["reason"])
        self.assertIn(device.tracks[0]["id"], result["reason"])

    def test_a_complete_jam_inventory_without_the_id_proves_absence(self):
        # proof (b) after a failed readback: the delete happened, the JAM
        # inventory is complete and does not list the id
        device, jam, clip, result = self._delete_with_failed_readback(
            "Error: no clip data could be retrieved: read timed out")
        self.assertTrue(result["deleted"], result)
        self.assertEqual(result["after_state"]["status"], "absent")
        self.assertEqual(result["after_state"]["method"], "jam_inventory")
        self.assertNotIn("t%d/s0" % jam["trackIndex"], device.clips)

    def test_an_inventory_listing_the_clip_with_an_int_id_is_not_absence(self):
        # review round 3 follow-up: an (uncaptured) listing whose clip id is an
        # int must still read as present against the str clip id
        listing = {"id": "901", "sessionClips": [{"id": 902, "name": "x"}]}
        inventory = {"complete": True, "clips": [{"clip": {"id": 902, "name": "x"}}], "reason": None}

        class Session:
            async def call(self, tool, args):
                return listing

        class Requests:
            def read_track(self, **kw):
                return kw

        with patch.object(pp, "requests_for", return_value=Requests()), \
                patch.object(pp, "_result", return_value=listing), \
                patch.object(pp, "response_warnings_mean_incomplete", return_value=False), \
                patch.object(pp, "session_clips_inventory", return_value=inventory):
            out = asyncio.run(lw._jam_session_inventory(Session(), {"id": "901"}))
        self.assertTrue(out["complete"])
        self.assertEqual(out["clip_ids"], ["902"])
        self.assertIn(str(902), out["clip_ids"])

    def test_readback_absence_wins_over_a_refusing_receipt(self):
        # review M2: the clip is gone though the receipt says deleted:false +
        # "… still exists, so Live did not delete it": absent means deleted.
        device, jam, clip, identity = self._jam_with_clip()
        device.misreport_deletes = True
        result = run(device, lw.delete_jam_clip, "%d/0" % jam["trackIndex"],
                     expected_clip_id=clip["id"], expected_track_id=jam["id"],
                     expected_set_identity=identity)

        self.assertTrue(result["deleted"], result)
        self.assertEqual(result["after_state"]["status"], "absent")
        self.assertIn("nothing to delete" if self.PP_VERSION == V24 else "did not delete it",
                      result["receipt_disagreed"])
        self.assertNotIn("t%d/s0" % jam["trackIndex"], device.clips)

    def test_jam_lifecycle_journals_a_readback_proven_delete_and_fires_on_deleted(self):
        import tempfile
        from mcp_server import jam_lifecycle, ledger

        device, jam, clip, identity = self._jam_with_clip()
        device.misreport_deletes = True
        fired = []
        with tempfile.TemporaryDirectory() as td:
            ledger_path = td + "/ledger.json"
            ledger.begin_judgment("judge:e-1:drop", {"entry_id": "e-1", "verdict": "drop"},
                                  entry_id="e-1", path=ledger_path)
            result = {"verdict": "drop"}
            jam_lifecycle.record_judgment(
                result, slot="%d/0" % jam["trackIndex"], confirm=True,
                record_verdict_fn=lambda verdict, **kw: {"id": "e-1"},
                delete_jam_clip_fn=lambda slot, **expected: run(
                    device, lw.delete_jam_clip, slot, **expected),
                on_deleted=lambda: fired.append("deleted"),
                clip_ref={"clip_id": clip["id"], "track_id": jam["id"]},
                set_identity=identity, event_id="judge:e-1:drop", ledger_path=ledger_path)
            step = ledger.judgment_event("judge:e-1:drop", path=ledger_path)["steps"]["deletion"]

        self.assertTrue(result["clip_delete"]["deleted"])
        self.assertEqual(step["status"], "applied")
        self.assertTrue(step["result"]["deleted"])
        self.assertEqual(fired, ["deleted"])

    def test_refused_delete_is_not_a_delete(self):
        if self.PP_VERSION == V22:
            self.skipTest("2.2.0-rc1 has no captured delete refusal; the 2.3 twin replays "
                          "v23/v23_delete_missing_id.json, the 2.4 twin "
                          "v24/v24_delete_clip_missing_id.json")
        device, jam, clip, identity = self._jam_with_clip()
        device.refuse_deletes = True
        result = run(device, lw.delete_jam_clip, "%d/0" % jam["trackIndex"],
                     expected_clip_id=clip["id"], expected_track_id=jam["id"],
                     expected_set_identity=identity)

        self.assertFalse(result["deleted"])
        self.assertEqual(result["delete_receipt"]["deleted_ids"], [])
        self.assertEqual(result["reason"], "clip still present after delete")
        self._assert_refused_receipt(result["delete_receipt"])

    def _assert_refused_receipt(self, receipt):
        """The captured non-deleting reply of this build: 2.3 `deleted: false` +
        WARNING "id … does not exist" (v23_delete_missing_id); 2.4 `{detail:
        "nothing to delete", id}` and no warning (v24_delete_clip_missing_id)."""
        self.assertEqual(receipt["deleted_ids"], [])
        if self.PP_VERSION == V24:
            self.assertEqual(receipt["result"].get("detail"), "nothing to delete")
            self.assertNotIn("deleted", receipt["result"])
            self.assertEqual(receipt["warnings"], [])
        else:
            self.assertIs(receipt["result"].get("deleted"), False)
            self.assertIn("does not exist", receipt["warnings"][0])


@on_all_versions
class AppendTrackReconcileTests(unittest.TestCase):
    """Review 2026-09-30 finding 5 (append_wrong_index probe): create-track
    made track 901 but replied an unexpected index. The error must name 901
    in a structured record, nothing is deleted, and a retry adopts 901 (found
    by id, still unnamed) instead of creating a second track."""

    def setUp(self):
        lw._forget_unplaced_tracks()
        self.addCleanup(lw._forget_unplaced_tracks)

    def _wrong_index_write(self, device):
        device.create_track_index_offset = -1     # 901 sits at 11, the reply says 10
        with self.assertRaises(lw.LiveWriteVerificationError) as ctx:
            run(device, lw.write_answer_to_jam_track, NOTES, "answer A", tempo=120)
        exc = ctx.exception
        self.assertIn("created track 901 at index 10, expected the end (11)", str(exc))
        self.assertEqual(exc.created_track, {"id": "901", "track_index": 10})
        self.assertEqual(wire_for(device, "ppal-delete"), [])         # never auto-deleted
        self.assertEqual(wire_for(device, "ppal-create-clip"), [])
        self.assertEqual(device.tracks[11]["name"], "12-MIDI")        # left as Live named it
        device.create_track_index_offset = 0
        return exc

    def test_the_error_names_the_created_track_and_the_retry_adopts_it(self):
        device = CapturedDevice()
        self._wrong_index_write(device)

        receipt = run(device, lw.write_answer_to_jam_track, NOTES, "answer A", tempo=120)
        self.assertTrue(receipt["verified_ok"])
        self.assertEqual(len(args_for(device, "ppal-create-track")), 1)   # no second track
        self.assertEqual(receipt["track_index"], 11)
        self.assertEqual(receipt["track_adopted"], "901")
        self.assertEqual(device.tracks[11], device.tracks[11] | {
            "id": "901", "name": lw.JAM_TRACK_NAME})
        self.assertEqual(receipt["slot"], "11/0")
        self.assertEqual(len(device.tracks), 12)
        self.assertEqual(wire_for(device, "ppal-delete"), [])

    def test_a_track_renamed_by_him_or_gone_is_not_adopted(self):
        # renamed: it is his now — forgotten, a fresh track is created
        device = CapturedDevice()
        self._wrong_index_write(device)
        device.tracks[11]["name"] = "his bass"
        receipt = run(device, lw.write_answer_to_jam_track, NOTES, "answer A", tempo=120)
        self.assertNotIn("track_adopted", receipt)
        self.assertEqual(len(args_for(device, "ppal-create-track")), 2)
        self.assertEqual(receipt["track_index"], 12)
        self.assertEqual(device.tracks[11]["name"], "his bass")
        self.assertEqual(wire_for(device, "ppal-delete"), [])
        # gone: forgotten too
        device = CapturedDevice()
        self._wrong_index_write(device)
        del device.tracks[11]
        receipt = run(device, lw.write_answer_to_jam_track, NOTES, "answer A", tempo=120)
        self.assertNotIn("track_adopted", receipt)
        self.assertEqual(receipt["track_index"], 11)
        self.assertNotEqual(device.tracks[11]["id"], "901")

    def test_another_runtime_set_never_adopts_a_reused_id(self):
        device = CapturedDevice()
        self._wrong_index_write(device)
        # Live restarted / the Set reopened: ids are reused, the record is not ours
        pp.set_live_process_incarnation_resolver(
            lambda: {"pid": 9999, "start_time": "Sat Sep 26 12:00:00 2026"})
        self.addCleanup(pp.set_live_process_incarnation_resolver,
                        lambda: {"pid": 4242, "start_time": "Sat Sep 26 10:00:00 2026"})
        receipt = run(device, lw.write_answer_to_jam_track, NOTES, "answer A", tempo=120)
        self.assertNotIn("track_adopted", receipt)
        self.assertEqual(len(args_for(device, "ppal-create-track")), 2)
        self.assertEqual(device.tracks[11]["name"], "12-MIDI")

    def test_a_lying_reply_naming_his_existing_track_is_never_adopted(self):
        # Round 3 hardening: create-track replies an EXISTING, default-named
        # track's id ("10-MIDI", id 358 — in the overview before the create)
        # at a wrong index. That id was not created by us: not remembered, the
        # retry does not rename or write into his track.
        device = CapturedDevice()
        his = device.tracks[9]
        self.assertEqual(his["id"], "358")
        his["name"] = "10-MIDI"
        device.create_track_replies_id = "358"
        device.create_track_index_offset = -2
        with self.assertRaises(lw.LiveWriteVerificationError) as ctx:
            run(device, lw.write_answer_to_jam_track, NOTES, "answer A", tempo=120)
        self.assertEqual(ctx.exception.created_track["id"], "358")
        self.assertIn("not created by us", str(ctx.exception))
        self.assertEqual(wire_for(device, "ppal-update-track"), [])
        device.create_track_replies_id = None
        device.create_track_index_offset = 0

        receipt = run(device, lw.write_answer_to_jam_track, NOTES, "answer A", tempo=120)
        self.assertNotIn("track_adopted", receipt)
        self.assertEqual(device.tracks[9]["name"], "10-MIDI")          # his, untouched
        self.assertNotIn("t9/s0", device.clips)
        self.assertEqual(len(args_for(device, "ppal-create-track")), 2)
        self.assertEqual(device.tracks[receipt["track_index"]]["name"], lw.JAM_TRACK_NAME)
        self.assertNotEqual(device.tracks[receipt["track_index"]]["id"], "358")
        self.assertEqual(wire_for(device, "ppal-delete"), [])

    def test_an_unnamed_track_write_surfaces_the_created_track_too(self):
        # write_tracks names no track, so nothing is adopted; the record still
        # names the track Live made, and nothing is deleted
        device = CapturedDevice()
        device.create_track_index_offset = -1
        with self.assertRaises(lw.LiveWriteVerificationError) as ctx:
            run(device, lw.write_tracks, [{"name": "bass", "notes": NOTES}], tempo=100)
        self.assertEqual(ctx.exception.created_track, {"id": "901", "track_index": 10})
        self.assertEqual(ctx.exception.completed, [])
        self.assertEqual(wire_for(device, "ppal-delete"), [])
        self.assertEqual(len(device.tracks), 12)

    def test_booth_adopts_its_own_unplaced_track_the_same_way(self):
        device = CapturedDevice()
        device.create_track_index_offset = -1
        with self.assertRaises(lw.LiveWriteVerificationError) as ctx:
            run(device, lw.stage_named_clips, [{"name": "blind 1", "notes": NOTES}])
        self.assertEqual(ctx.exception.created_track["id"], "901")
        device.create_track_index_offset = 0
        booth = run(device, lw.stage_named_clips, [{"name": "blind 1", "notes": NOTES}])
        self.assertTrue(booth["verified_ok"])
        self.assertEqual(booth["track_adopted"], "901")
        self.assertEqual(len(args_for(device, "ppal-create-track")), 1)
        self.assertEqual(device.tracks[11]["name"], lw.BOOTH_TRACK_NAME)


if __name__ == "__main__":
    unittest.main()
