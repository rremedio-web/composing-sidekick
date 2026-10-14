from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from mcp_server import arrangement_exec, live_write, pp_session, server
from tests import pp_fakes
from tests.pp_fakes import V23, SchemaStrictSession, on_both_versions, schema_strict
from tests._requires import requires_catalog


def response(result):
    raw = json.dumps({"result": result})
    return pp_session.PPResponse(raw, result, [])


class FakeArrangementSession(SchemaStrictSession):
    def __init__(self, *, mutate_source=False, dirty_duplicate=False,
                 invalid_duplicate_index=False, delete_fails=False,
                 foreign_clip_after_fresh_readback=False):
        super().__init__()
        self.mutate_source = mutate_source
        self.dirty_duplicate = dirty_duplicate
        self.invalid_duplicate_index = invalid_duplicate_index
        self.delete_fails = delete_fails
        # after the fresh track's first readback, a clip of his lands on it
        # (between the creation check and the placement)
        self.foreign_clip_after_fresh_readback = foreign_clip_after_fresh_readback
        self.fresh_reads = 0
        self.copies = {}          # track index -> copies ever placed there
        self.placed = False
        self.deleted = []
        self.tracks = {
            0: {"id": "source-0", "name": "Lead", "trackIndex": 0,
                "gainDb": -1.94, "pan": -0.1, "sends": [],
                "devices": [{"id": "device-source", "name": "Synth"}],
                # Session clip identity follows read_track_t1_index.json: id + path.
                "sessionClips": [{"id": "clip-0", "path": "t0/s0", "name": "Loop"}],
                "arrangementClips": [{"id": "arr-0", "path": "t0",
                                       "arrangementStart": "1|1"}]},
            1: {"id": "old-arr", "name": "SIDEKICK ARR old old", "trackIndex": 1,
                "gainDb": 0, "pan": 0, "sends": [], "devices": [],
                "sessionClips": [], "arrangementClips": []},
        }

    async def call(self, name, args):
        self.validate_call(name, args)
        self.calls.append((name, dict(args)))
        if name == "ppal-read-live-set":
            return response({"tracks": [
                {**({"id": track["id"]} if track.get("id") else {}),
                 "name": track["name"], "trackIndex": index}
                for index, track in sorted(self.tracks.items())]})
        if name == "ppal-read-track":
            track = json.loads(json.dumps(self.tracks[args["trackIndex"]]))
            if self.mutate_source and self.placed and args["trackIndex"] == 0:
                track["sessionClips"][0]["notes"] = [{"pitch": 61}]
            if (self.foreign_clip_after_fresh_readback
                    and str(self.tracks[args["trackIndex"]]["id"]).startswith("new-")):
                self.fresh_reads += 1
                if self.fresh_reads == 1:
                    # lands after this (creation) readback: the next read shows it
                    self.tracks[args["trackIndex"]]["arrangementClips"].append(
                        {"id": "his-clip", "path": "t%d" % args["trackIndex"],
                         "arrangementStart": "17|1"})
            return response(track)
        if name == "ppal-read-clip":
            return response({"id": "clip-0", "name": "Loop", "type": "midi"})
        if name == "ppal-duplicate" and args["type"] == "track":
            index = max(self.tracks) + 1
            devices = [{"id": "device-%d" % index, "name": "Synth"}]
            if self.dirty_duplicate:
                devices = []
            self.tracks[index] = {
                "id": "new-%d" % index, "name": args["name"], "trackIndex": index,
                "gainDb": -1.94, "pan": -0.1, "sends": [], "devices": devices,
                "sessionClips": ([{"id": "bad"}] if self.dirty_duplicate else []),
                "arrangementClips": [],
            }
            return response({"id": "new-%d" % index,
                             "trackIndex": None if self.invalid_duplicate_index else index,
                             "clips": []})
        if name == "ppal-duplicate" and args["type"] == "clip":
            index = int(args["toPath"][1:])
            # Live's semantics, captured on 2.4 (v24_duplicate_over_existing):
            # a copy dropped on a clip at that position replaces it, and the
            # reply names only the new clip
            self.tracks[index]["arrangementClips"] = [
                clip for clip in self.tracks[index]["arrangementClips"]
                if clip.get("arrangementStart") != args["arrangementStart"]]
            self.copies[index] = self.copies.get(index, 0) + 1
            clip_id = "arr-%d-%d" % (index, self.copies[index] - 1)
            clip = {"id": clip_id, "path": "t%d" % index,
                    "arrangementStart": args["arrangementStart"]}
            self.tracks[index]["arrangementClips"].append(clip)
            self.placed = True
            return response(clip)
        if name == "ppal-delete":
            if self.delete_fails:
                raise RuntimeError("delete unavailable")
            self.deleted.append(args["id"])
            for index, track in list(self.tracks.items()):
                if track["id"] == args["id"]:
                    self.tracks.pop(index)
            # Captured shape: mutate/delete_track.json.
            return response({"deleted": True, "id": args["id"], "type": args["type"]})
        raise AssertionError((name, args))


class InsertingArrangementSession(FakeArrangementSession):
    """Producer Pal fake that inserts each duplicate after its source."""

    def __init__(self):
        super().__init__()
        second = json.loads(json.dumps(self.tracks[0]))
        second.update(id="source-2", name="Bass", trackIndex=2,
                      sessionClips=[{"id": "clip-2", "path": "t2/s0"}])
        old = self.tracks.pop(1)
        middle = json.loads(json.dumps(old))
        middle.update(id="source-1", name="Keys", trackIndex=1,
                      sessionClips=[{"id": "clip-1", "path": "t1/s0"}],
                      gainDb=-1.94, pan=-0.1, sends=[],
                      devices=[{"id": "device-middle", "name": "Synth"}])
        self.tracks[1] = middle
        self.tracks[2] = second
        self.tracks[3] = old
        for index, track in self.tracks.items():
            track["trackIndex"] = index
        self.next_id = 0

    async def call(self, name, args):
        self.validate_call(name, args)
        if name == "ppal-duplicate" and args["type"] == "track":
            self.calls.append((name, dict(args)))
            source_index = next(index for index, track in self.tracks.items()
                                if track["id"] == args["id"])
            source = self.tracks[source_index]
            new_id = "inserted-%d" % self.next_id
            self.next_id += 1
            for index in sorted((i for i in self.tracks if i > source_index), reverse=True):
                moved = self.tracks.pop(index)
                # Live reports CURRENT positions after an insert: the moved
                # track's index and its clip slots' track part shift too.
                moved["trackIndex"] = index + 1
                for clip in moved.get("sessionClips") or []:
                    if isinstance(clip.get("slot"), str) and "/" in clip["slot"]:
                        clip["slot"] = "%d/%s" % (index + 1,
                                                  clip["slot"].split("/", 1)[1])
                for clip in moved.get("arrangementClips") or []:
                    if isinstance(clip.get("path"), str):
                        clip["path"] = "t%d" % (index + 1)
                self.tracks[index + 1] = moved
            self.tracks[source_index + 1] = {
                "id": new_id, "name": args["name"],
                "trackIndex": source_index + 1,
                "gainDb": source["gainDb"], "pan": source["pan"],
                "sends": json.loads(json.dumps(source["sends"])),
                "devices": json.loads(json.dumps(source["devices"])),
                "sessionClips": [], "arrangementClips": [],
            }
            return response({"id": new_id, "trackIndex": source_index + 1})
        if name == "ppal-read-clip":
            index = int(args["path"].split("/")[0].removeprefix("t"))
            return response({"id": "clip-%d" % index, "type": "midi"})
        return await super().call(name, args)


def operation(session):
    @asynccontextmanager
    async def factory(*_args, **_kwargs):
        yield session
    return factory


PARTS = [{"part_id": "lead-a", "role": "lead", "source_path": "t0/s0"}]
PLACEMENTS = [{"part_id": "lead-a", "role": "lead", "source_path": "t0/s0",
               "bar_start": 16, "bars": 8, "density": 7, "section": "DROP"}]


@on_both_versions
@requires_catalog
class TestArrangementExecutor(unittest.TestCase):
    def test_position_free_normalizes_track_paths_but_keeps_scene(self):
        # Path forms mirror tests/fixtures/pp/2.2.0-rc1/tool_schemas.json.
        self.assertEqual(arrangement_exec._position_free({"path": "t3"}),
                         {"path": "t#"})
        self.assertEqual(arrangement_exec._position_free({"path": "t3/s0"}),
                         {"path": "t#/s0"})
        self.assertEqual(arrangement_exec._position_free({"path": "t3/l0"}),
                         {"path": "t#/l0"})
        self.assertEqual(arrangement_exec._position_free({"path": "t2/d1"}),
                         {"path": "t#/d1"})
        self.assertEqual(arrangement_exec._position_free({"path": "t0[5|1]"}),
                         {"path": "t#[5|1]"})
        self.assertNotEqual(arrangement_exec._position_free({"path": "t2/s0"}),
                            arrangement_exec._position_free({"path": "t2/s1"}))
        self.assertEqual(arrangement_exec._position_free({"name": "t1/bass"}),
                         {"name": "t1/bass"})
        self.assertEqual(arrangement_exec._position_free({"path": "tracks 3"}),
                         {"path": "tracks #"})
        self.assertEqual(arrangement_exec._position_free({"name": "tracks 3"}),
                         {"name": "tracks 3"})

    def test_source_arrangement_clip_bare_path_survives_track_index_shift(self):
        # Arrangement clip path shape is t<track> per tool_schemas.json capture.
        first = {"id": "source", "trackIndex": 0,
                 "arrangementClips": [{"id": "arr-0", "path": "t0"}]}
        shifted = {"id": "source", "trackIndex": 3,
                   "arrangementClips": [{"id": "arr-0", "path": "t3"}]}
        self.assertEqual(arrangement_exec._fingerprint(response(first)),
                         arrangement_exec._fingerprint(response(shifted)))

    def test_read_track_requests_note_data(self):
        # include tokens mirror the ppal-read-track contract used by live_write.
        @schema_strict
        class Session:
            async def call(self, name, args):
                self.call_args = (name, args)
                return response({})
        session = Session()
        server._run_async(arrangement_exec._read_track(session, 3))
        self.assertIn("notes", session.call_args[1]["include"])

    def test_missing_and_idless_source_tracks_have_explicit_error(self):
        scenarios = [
            (FakeArrangementSession(), "t9/s0"),
            (FakeArrangementSession(), "t0/s0"),
        ]
        del scenarios[1][0].tracks[0]["id"]
        for session, source_path in scenarios:
            part = [{"part_id": "lead-a", "role": "lead", "source_path": source_path}]
            with self.subTest(source_path=source_path), self.assertRaisesRegex(
                    arrangement_exec.ArrangementExecutionError,
                    "source track is unavailable"):
                server._run_async(arrangement_exec.execute_arrangement(
                    part, [], operation_session=operation(session)))

    def test_duplicates_resolve_shifted_tracks_by_id_for_all_part_orders(self):
        scenarios = [
            [("lead", 0), ("bass", 2)],
            [("bass", 2), ("lead", 0)],
            [("lead-a", 0), ("lead-b", 0)],
        ]
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                parts = [{"part_id": part_id, "role": "lead",
                          "source_path": "t%d/s0" % source_index}
                         for part_id, source_index in scenario]
                placements = [{**part, "bar_start": 8, "bars": 4}
                              for part in parts]
                session = InsertingArrangementSession()
                result = server._run_async(arrangement_exec.execute_arrangement(
                    parts, placements, operation_session=operation(session)))
                track_dups = [args for name, args in session.calls
                              if name == "ppal-duplicate" and args["type"] == "track"]
                self.assertEqual([call["id"] for call in track_dups],
                                 ["source-%d" % source_index
                                  for _, source_index in scenario])
                created = {item["part_id"]: item["track_id"]
                           for item in result["created_tracks"]}
                clip_dups = [args for name, args in session.calls
                             if name == "ppal-duplicate" and args["type"] == "clip"]
                self.assertEqual(
                    [call["toPath"] for call in clip_dups],
                    ["t%d" % next(index for index, track in session.tracks.items()
                                  if track["id"] == created[part_id])
                     for part_id, _ in scenario],
                )

    def test_real_source_edit_during_arrangement_still_rolls_back(self):
        # Position changes are normalized away; a changed note/device is not.
        class EditingSession(InsertingArrangementSession):
            async def call(self, name, args):
                result = await super().call(name, args)
                if name == "ppal-duplicate" and args["type"] == "clip":
                    bass = next(t for t in self.tracks.values()
                                if t["id"] == "source-2")
                    bass["devices"] = [{"id": "device-x", "name": "Edited"}]
                return result

        parts = [{"part_id": "lead", "role": "lead", "source_path": "t0/s0"},
                 {"part_id": "bass", "role": "lead", "source_path": "t2/s0"}]
        placements = [{**part, "bar_start": 8, "bars": 4} for part in parts]
        session = EditingSession()
        with self.assertRaisesRegex(arrangement_exec.ArrangementExecutionError,
                                    "source track changed"):
            server._run_async(arrangement_exec.execute_arrangement(
                parts, placements, operation_session=operation(session)))
        # cleanup removed only the inserted tracks; every source survives
        ids = {t["id"] for t in session.tracks.values()}
        self.assertFalse(any(i.startswith("inserted-") for i in ids))
        self.assertTrue({"source-1", "source-2"} <= ids)

    def test_clip_results_accepts_pp21_envelope_track_index(self):
        copies = arrangement_exec._clip_results(response({
            "trackIndex": 2,
            "clips": [{"id": "arr-2-0", "arrangementStart": "17|1"}],
        }), 2)

        self.assertEqual(copies, [
            {"id": "arr-2-0", "arrangementStart": "17|1"},
        ])

    def test_clip_results_normalizes_23_arrangement_path(self):
        # Shape documented by the Producer Pal 2.3 migration guide (not captured).
        copies = arrangement_exec._clip_results(response({
            "clips": [{"id": "arr-2-0", "path": "t2[17|1]"}],
        }), 2)
        self.assertEqual(copies[0]["arrangementStart"], "17|1")

    def test_clip_results_rejects_wrong_or_conflicting_destination(self):
        with self.assertRaisesRegex(arrangement_exec.ArrangementExecutionError,
                                   "landed on the wrong track"):
            arrangement_exec._clip_results(response({
                "trackIndex": 3,
                "clips": [{"id": "arr-3-0"}],
            }), 2)

        with self.assertRaisesRegex(arrangement_exec.ArrangementExecutionError,
                                   "landed on the wrong track"):
            arrangement_exec._clip_results(response({
                "trackIndex": 2,
                "clips": [{"id": "arr-2-0", "path": "t3"}],
            }), 2)

    def test_producer_pal_21_flat_mixer_state_is_normalized(self):
        state = arrangement_exec._duplicate_state(response({
            "id": "source-0",
            "trackIndex": 0,
            "gainDb": -3,
            "pan": 0.25,
            "sends": [{"return": "A-Reverb", "gainDb": -12}],
            "devices": [{"id": "device-1", "name": "Synth"}],
        }))

        self.assertEqual(state, {
            "mixer": {
                "gainDb": -3,
                "pan": 0.25,
                "sends": [{"return": "A-Reverb", "gainDb": -12}],
            },
            "devices": [{"name": "Synth"}],
        })

    def test_fresh_full_duplicate_explicit_placement_readback_and_undo(self):
        session = FakeArrangementSession()
        result = server._run_async(arrangement_exec.execute_arrangement(
            PARTS, PLACEMENTS, operation_session=operation(session)))
        self.assertTrue(result["source_fingerprints_verified"])
        self.assertEqual(len(result["created_tracks"]), 1)
        created = result["created_tracks"][0]
        self.assertEqual(created["track_id"], "new-2")
        self.assertTrue(created["name"].startswith("SIDEKICK ARR lead-a "))
        self.assertNotEqual(created["track_id"], "old-arr")
        track_call = next(args for name, args in session.calls
                          if name == "ppal-duplicate" and args["type"] == "track")
        self.assertTrue(track_call["withoutClips"])
        self.assertFalse(track_call["withoutDevices"])
        clip_call = next(args for name, args in session.calls
                         if name == "ppal-duplicate" and args["type"] == "clip")
        self.assertEqual(clip_call["toPath"], "t2")
        self.assertEqual(clip_call["arrangementStart"], "17|1")
        self.assertEqual(clip_call["arrangementLength"], "8bar")
        sent = next(args for name, args in session.wire_calls
                    if name == "ppal-duplicate" and args["type"] == "clip")
        if self.PP_VERSION in pp_fakes.PATH_VERSIONS:   # v23/v23_duplicate_trimmed.json form (2.4: v24_duplicate_trimmed)
            self.assertEqual(sent, {"type": "clip", "id": "clip-0", "toPath": "t2[17|1]",
                                    "arrangementLength": "8bar"})
            self.assertEqual(next(args for name, args in session.wire_calls
                                  if name == "ppal-read-track")["path"], "t0")
        else:                        # mutate/duplicate_clip_to_arrangement.json form
            self.assertEqual(sent, clip_call)
        self.assertEqual(result["undo"], [{"tool": "ppal-delete", "arguments": {
            "type": "track", "id": "new-2"}}])
        # The returned undo plan must itself be schema-valid for the device.
        for step in result["undo"]:
            pp_fakes.validate_call(step["tool"], step["arguments"])

    def test_overlapping_placements_are_refused_before_any_write(self):
        # a copy placed over a clip replaces it silently (v24_duplicate_over_existing
        # on 2.4; the same Live semantics on 2.2 / 2.3): the plan is checked first
        session = FakeArrangementSession()
        placements = [dict(PLACEMENTS[0]), {**PLACEMENTS[0], "bar_start": 20, "bars": 4}]
        with self.assertRaisesRegex(arrangement_exec.ArrangementExecutionError,
                                    "overlap on part 'lead-a'.*bar 17 with .*bar 21"):
            server._run_async(arrangement_exec.execute_arrangement(
                PARTS, placements, operation_session=operation(session)))
        self.assertEqual(session.calls, [])
        # the fake reproduces the capture: a second copy at the same position
        # replaces the first, the reply names only the new clip
        requests = pp_session.PPRequests(pp_session.addressing_for_version(self.PP_VERSION))
        first = server._run_async(session.call(
            "ppal-duplicate", requests.duplicate_clip_to_arrangement("clip-0", track=1, start="17|1")))
        second = server._run_async(session.call(
            "ppal-duplicate", requests.duplicate_clip_to_arrangement("clip-0", track=1, start="17|1")))
        self.assertNotEqual(pp_session._result(first)["id"], pp_session._result(second)["id"])
        self.assertEqual([c["id"] for c in session.tracks[1]["arrangementClips"]],
                         [pp_session._result(second)["id"]])

    def test_a_foreign_clip_on_the_fresh_track_refuses_the_placement_and_cleans_up(self):
        session = FakeArrangementSession(foreign_clip_after_fresh_readback=True)
        with self.assertRaisesRegex(arrangement_exec.ArrangementExecutionError,
                                    "destination t2 is not free: clip his-clip at 17\\|1"):
            server._run_async(arrangement_exec.execute_arrangement(
                PARTS, PLACEMENTS, operation_session=operation(session)))
        self.assertFalse(any(name == "ppal-duplicate" and args["type"] == "clip"
                             for name, args in session.calls))
        self.assertEqual(session.deleted, ["new-2"])
        self.assertEqual([c["id"] for c in session.tracks[0]["arrangementClips"]], ["arr-0"])

    def test_duplicates_succeed_next_to_the_devices_advisory_warnings(self):
        # review M1: a duplicated track always loses its Producer Pal device
        # ("… cannot be duplicated") and every call warns about the backup sync
        session = FakeArrangementSession()
        session.device_warnings = list(pp_fakes.DEVICE_ADVISORY_WARNINGS[self.PP_VERSION])
        result = server._run_async(arrangement_exec.execute_arrangement(
            PARTS, PLACEMENTS, operation_session=operation(session)))
        self.assertTrue(result["source_fingerprints_verified"])
        self.assertEqual(session.deleted, [])
        self.assertEqual(result["pp_warnings"], session.device_warnings)

    def test_a_copy_placed_elsewhere_is_refused_and_cleaned_up(self):
        # review minor: the copy's position is read back from the reply
        # (2.2 arrangementStart, 2.3 the path 't2[17|1]')
        class Misplacing(FakeArrangementSession):
            async def call(self, name, args):
                if name == "ppal-duplicate" and args["type"] == "clip":
                    args = {**args, "arrangementStart": "33|1"}
                return await super().call(name, args)

        session = Misplacing()
        with self.assertRaisesRegex(arrangement_exec.ArrangementExecutionError,
                                    "landed at 33\\|1, not at 17\\|1"):
            server._run_async(arrangement_exec.execute_arrangement(
                PARTS, PLACEMENTS, operation_session=operation(session)))
        self.assertEqual(session.deleted, ["new-2"])

    def test_source_fingerprint_drift_deletes_only_fresh_track(self):
        session = FakeArrangementSession(mutate_source=True)
        with self.assertRaisesRegex(arrangement_exec.ArrangementExecutionError,
                                   "source track changed"):
            server._run_async(arrangement_exec.execute_arrangement(
                PARTS, PLACEMENTS, operation_session=operation(session)))
        self.assertEqual(session.deleted, ["new-2"])
        self.assertNotIn("source-0", session.deleted)

    def test_dirty_or_incomplete_duplicate_is_deleted_before_placement(self):
        session = FakeArrangementSession(dirty_duplicate=True)
        with self.assertRaises(arrangement_exec.ArrangementExecutionError):
            server._run_async(arrangement_exec.execute_arrangement(
                PARTS, PLACEMENTS, operation_session=operation(session)))
        self.assertEqual(session.deleted, ["new-2"])
        self.assertFalse(any(name == "ppal-duplicate" and args["type"] == "clip"
                             for name, args in session.calls))

    def test_malformed_creation_receipt_still_cleans_and_failed_cleanup_is_explicit(self):
        session = FakeArrangementSession(invalid_duplicate_index=True)
        with self.assertRaisesRegex(arrangement_exec.ArrangementExecutionError,
                                   "identity is unavailable"):
            server._run_async(arrangement_exec.execute_arrangement(
                PARTS, PLACEMENTS, operation_session=operation(session)))
        self.assertEqual(session.deleted, ["new-2"])
        self.assertNotIn(2, session.tracks)

        session = FakeArrangementSession(
            invalid_duplicate_index=True, delete_fails=True)
        with self.assertRaisesRegex(arrangement_exec.ArrangementExecutionError,
                                   "cleanup left fresh Sidekick tracks: new-2"):
            server._run_async(arrangement_exec.execute_arrangement(
                PARTS, PLACEMENTS, operation_session=operation(session)))
        self.assertIn(2, session.tracks)

    def test_lay_the_song_locator_only_and_commit_contract(self):
        parts = [{"part_id": "lead", "role": "lead", "source_path": "t0/s0"}]
        with patch("mcp_server.live_write.write_song_locators",
                   return_value={"created": 2, "verification_status": "verified"}) as locators:
            result = server.lay_the_song(parts=parts, confirm=True, commit=False)
        locators.assert_called_once()
        self.assertTrue(result["locators_written"]["executed"])
        self.assertFalse(result["committed"]["executed"])

        with patch("mcp_server.live_write.write_song_locators",
                   return_value={"created": 2, "verification_status": "verified"}), patch(
                "mcp_server.arrangement_exec.execute_arrangement",
                return_value={"created_tracks": [{"track_id": "new"}], "undo": []}) as execute:
            result = server.lay_the_song(parts=parts, confirm=True, commit=True)
        execute.assert_called_once()
        self.assertTrue(result["committed"]["executed"])

        rolled_back = []

        async def rollback(snapshot):
            rolled_back.append(snapshot)
            return {"restored": True}

        with patch("mcp_server.live_write.write_song_locators",
                   return_value={"created": 2, "verification_status": "verified",
                                 "_rollback": {"clear_names": ["INTRO"]}}), patch(
                "mcp_server.arrangement_exec.execute_arrangement",
                side_effect=arrangement_exec.ArrangementExecutionError("placement failed")), patch(
                "mcp_server.live_write.rollback_song_locators", side_effect=rollback):
            with self.assertRaisesRegex(arrangement_exec.ArrangementExecutionError,
                                       "placement failed"):
                server.lay_the_song(parts=parts, confirm=True, commit=True)
        self.assertEqual(rolled_back, [{"clear_names": ["INTRO"]}])


@schema_strict
class FakeLocatorSession:
    def __init__(self, mismatch=False):
        self.locators = []
        self.mismatch = mismatch

    async def call(self, name, args):
        if name == "ppal-update-live-set":
            if args["locatorOperation"] == "create":
                self.locators.append({"name": args["locatorName"],
                                      "time": args["locatorTime"]})
            elif args["locatorOperation"] == "delete":
                self.locators = [item for item in self.locators
                                 if item["name"] != args["locatorName"]]
            return response({"ok": True})
        if name == "ppal-read-live-set":
            return response({"locators": [] if self.mismatch else self.locators})
        raise AssertionError(name)


@on_both_versions
class TestLocatorVerification(unittest.TestCase):
    def test_verified_locator_write_and_mismatch_refusal(self):
        plan = {"locators": [{"name": "INTRO", "time": "1|1"}]}
        with tempfile.TemporaryDirectory() as td:
            state = str(Path(td) / "locators.json")
            session = FakeLocatorSession()
            with patch.object(pp_session, "operation_session", operation(session)), patch.object(
                    live_write, "_MAP_STATE", state):
                result = server._run_async(live_write.write_song_locators(plan, replace=False))
            self.assertEqual(result["verification_status"], "verified")
            self.assertTrue(Path(state).is_file())

            session = FakeLocatorSession(mismatch=True)
            with patch.object(pp_session, "operation_session", operation(session)), patch.object(
                    live_write, "_MAP_STATE", state):
                with self.assertRaises(pp_session.LiveWriteVerificationError):
                    server._run_async(live_write.write_song_locators(plan, replace=False))
            self.assertEqual(session.locators, [])

    def test_locator_snapshot_restores_previous_map_after_later_failure(self):
        plan = {"locators": [
            {"name": "INTRO", "time": "1|1"},
            {"name": "BUILD", "time": "9|1"},
        ]}
        with tempfile.TemporaryDirectory() as td:
            state = str(Path(td) / "locators.json")
            Path(state).write_text(json.dumps({"names": ["INTRO"]}))
            session = FakeLocatorSession()
            session.locators = [
                {"name": "INTRO", "time": "5|1"},
                {"name": "HAND", "time": "3|1"},
            ]
            with patch.object(pp_session, "operation_session", operation(session)), patch.object(
                    live_write, "_MAP_STATE", state):
                receipt = server._run_async(live_write.write_song_locators(plan))
                snapshot = receipt.pop("_rollback")
                self.assertEqual(receipt["verification_status"], "verified")
                server._run_async(live_write.rollback_song_locators(
                    snapshot, operation_session=operation(session), state_path=state))
            self.assertEqual(sorted(session.locators, key=lambda item: item["name"]), [
                {"name": "HAND", "time": "3|1"},
                {"name": "INTRO", "time": "5|1"},
            ])
            self.assertEqual(json.loads(Path(state).read_text())["names"], ["INTRO"])


if __name__ == "__main__":
    unittest.main()
