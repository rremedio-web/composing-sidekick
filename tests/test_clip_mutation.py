from __future__ import annotations

import json
import inspect
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from unittest import mock

from mcp_server import clip_mutation, ledger, pp_codec, pp_session, profiles, server
from tests import pp_fakes
from tests.pp_fakes import V23, V24, PATH_VERSIONS, SchemaStrictSession, on_all_versions


def response(result):
    raw = json.dumps({"result": result})
    return pp_session.PPResponse(raw, result, [])


NOTES = [
    {"pitch": 60, "start_beats": 0, "duration_beats": 1, "velocity": 90},
    {"pitch": 62, "start_beats": 1, "duration_beats": 1, "velocity": 80},
]


def wire(notes):
    return pp_codec.encode_notes(notes, pp_codec.CodecMode.MIDI_JSON)


class FakeClipSession(SchemaStrictSession):
    def __init__(self, *, supports_2_2=True, occupied=False,
                 mismatch=False, mutate_unaffected=False, delete_fails=False):
        super().__init__()
        self.capabilities = {"supports_2_2": supports_2_2}
        self.handshake = {"pp_version": self.pp_version if supports_2_2 else "2.1.0"}
        self.calls = []
        self.mismatch = mismatch
        self.delete_fails = delete_fails
        self.mutate_unaffected = mutate_unaffected
        self.tracks = [
            {"id": "user", "name": "Vocals", "trackIndex": 0, "path": "t0",
             "type": "midi"},
            {"id": "sidekick", "name": "SIDEKICK MIDI", "trackIndex": 1,
             "path": "t1", "type": "midi"},
            {"id": "ears", "name": "SIDEKICK EARS", "trackIndex": 2,
             "path": "t2", "type": "audio"},
        ]
        self.clips = {}
        if occupied:
            self.clips["t1/s0"] = self.clip(
                "existing", "t1/s0", "Old", NOTES, length="1bar")
        self.clips["t1/s2"] = self.clip(
            "surgery", "t1/s2", "Two Bars", [
                {"pitch": 60, "start_beats": 0, "duration_beats": 1, "velocity": 90},
                {"pitch": 62, "start_beats": 1, "duration_beats": 1, "velocity": 80},
                {"pitch": 64, "start_beats": 4, "duration_beats": 1, "velocity": 70},
                {"pitch": 65, "start_beats": 5, "duration_beats": 1, "velocity": 75},
            ], length="2bar")

    @staticmethod
    def clip(clip_id, path, name, notes, *, length):
        return {
            "id": clip_id, "name": name, "type": "midi", "path": path,
            # Captured session clips use path (see read_track_t1_index.json).
            "timeSignature": "4/4",
            "looping": True, "start": "1|1", "end": "3|1" if length == "2bar" else "2|1",
            "length": length, "notes": wire(notes),
        }


    def path_from_args(self, args):
        if "path" in args:
            return args["path"]
        return None

    async def call(self, name, args):
        self.validate_call(name, args)
        self.calls.append((name, dict(args)))
        if name == "ppal-read-live-set":
            return response({"sceneCount": 4, "tracks": self.tracks})
        if name == "ppal-read-track":
            track = self.tracks[args["trackIndex"]]
            return response(track)
        if name == "ppal-read-clip":
            path = self.path_from_args(args)
            clip = self.clips.get(path)
            if clip is None:
                if self.pp_version == V24:
                    # 2.4: an empty slot read by path is an ERROR (v24_read_clip_empty_slot)
                    raise pp_session.PPToolError("Error: no clip at %s" % path)
                return response({"id": None, "type": None, "path": path})
            out = json.loads(json.dumps(clip))
            if self.mismatch and path == "t1/s0" and any(
                    tool in {"ppal-create-clip", "ppal-update-clip"}
                    for tool, _call_args in self.calls):
                out["name"] = "Wrong"
                self.mismatch = False
            return response(out)
        if name == "ppal-create-clip":
            path = self.path_from_args(args)
            replaced = path in self.clips
            if replaced and self.pp_version != V24:
                raise AssertionError("a create into an occupied slot was not captured on %s"
                                     % self.pp_version)
            self.clips[path] = self.clip(
                "created", path, args["name"], pp_codec.midi_json_to_notes(args["notes"]),
                length=args["length"])
            self.clips[path]["start"] = args["start"]
            reply = {"id": "created", "path": path, "noteCount": len(NOTES)}
            if replaced:   # 2.4 replaces and says so (v24_create_session_clip_replaces)
                reply["detail"] = "overwrote the existing clip at %s" % path
            return response(reply)
        if name == "ppal-update-clip":
            clip_id = args["id"]
            path, clip = next((path, item) for path, item in self.clips.items()
                              if item["id"] == clip_id)
            incoming = pp_codec.midi_json_to_notes(args["notes"])
            if args["preTransforms"] == "delete":
                clip["notes"] = wire(incoming)
                clip["name"] = args["name"]
                clip["start"] = args["start"]
                clip["length"] = args["length"]
                bars = int(args["length"].removesuffix("bar")) \
                    if args["length"].endswith("bar") else 1
                clip["end"] = "%d|1" % (bars + 1)
            else:
                bar = int(args["preTransforms"].split("|", 1)[0])
                numerator, denominator = (
                    int(value) for value in clip["timeSignature"].split("/"))
                beats_per_bar = numerator * 4 / denominator
                start, end = (bar - 1) * beats_per_bar, bar * beats_per_bar
                existing = pp_codec.midi_json_to_notes(clip["notes"])
                kept = [note for note in existing
                        if not start <= note["start_beats"] < end]
                if self.mutate_unaffected:
                    kept[0] = {**kept[0], "velocity": kept[0]["velocity"] + 1}
                clip["notes"] = wire(kept + incoming)
            return response({"id": clip_id, "path": path,
                             "noteCount": len(pp_codec.midi_json_to_notes(clip["notes"]))})
        if name == "ppal-delete":
            if self.delete_fails:
                raise RuntimeError("delete unavailable")
            clip_id = args["id"]
            deleted_path = None
            for path, clip in list(self.clips.items()):
                if clip["id"] == clip_id:
                    self.clips.pop(path)
                    deleted_path = path
            # Captured shapes: 2.2 mutate/delete_track.json; 2.3 + deletedPath
            # (v23_delete_by_id); 2.4 {deletedPath, id} (the reshaper drops the rest)
            receipt = {"deleted": True, "id": clip_id, "type": args["type"]}
            if self.pp_version in PATH_VERSIONS and deleted_path is not None:
                receipt["deletedPath"] = deleted_path
            return response(receipt)
        raise AssertionError((name, args))


def operation(session):
    @asynccontextmanager
    async def factory(*_args, **_kwargs):
        yield session
    return factory


class TestMidiWireContract(unittest.TestCase):
    def test_fake_session_clip_uses_captured_path_locator(self):
        # Captured shape from tests/fixtures/pp/2.2.0-rc1/read/read_track_t1_index.json.
        clip = FakeClipSession.clip("existing", "t1/s0", "Old", NOTES, length="1bar")
        self.assertEqual(clip["path"], "t1/s0")
        self.assertNotIn("slot", clip)

    def test_real_pp_midi_json_string_and_legacy_fixture(self):
        encoded = pp_codec.encode_notes(NOTES, pp_codec.CodecMode.MIDI_JSON)
        self.assertIsInstance(encoded, str)
        self.assertEqual(json.loads(encoded)[0], {"p": 60, "t": 0.0, "d": 1.0, "v": 90})
        actual = response({"notes": "[{p:60,t:0,d:1,v:90},{p:62,t:1/3,d:2/3,v:80}]"})
        decoded = pp_session.extract_notes(actual)
        self.assertEqual(decoded[1]["start_beats"], 1 / 3)
        self.assertEqual(decoded[1]["duration_beats"], 2 / 3)
        pickup = pp_codec.midi_json_to_notes("[{p:59,t:-0.5,d:0.5,v:70}]")
        self.assertEqual(pickup[0]["start_beats"], -0.5)
        legacy = response({"notes": [{"p": 60, "s": 0, "d": 1, "v": 90}]})
        self.assertEqual(pp_session.extract_notes(legacy)[0]["pitch"], 60)
        synthetic = pp_session.response_from_notes(NOTES)
        self.assertEqual(pp_session.find_int(synthetic, "noteCount"), 2)
        self.assertEqual(len(pp_session.extract_notes(synthetic)), 2)
        with self.assertRaises(ValueError):
            pp_codec.midi_json_to_notes("[{p:60,t:0}{p:61,t:1}]")


@on_all_versions
class TestWriteMidiClip(unittest.TestCase):
    def run_write(self, session, **kwargs):
        return server._run_async(clip_mutation.write_midi_clip(
            operation_session=operation(session), **kwargs))

    def kwargs(self, **updates):
        values = {"track_name": "SIDEKICK MIDI", "scene_index": 0,
                  "notes": NOTES, "name": "New Loop"}
        values.update(updates)
        return values

    def test_preview_is_read_only_and_confirmed_create_is_verified(self):
        session = FakeClipSession()
        preview = self.run_write(session, confirm=False, **self.kwargs())
        self.assertEqual(preview["status"], "preview")
        self.assertEqual(preview["writes"], 0)
        self.assertFalse(any(name in {"ppal-create-clip", "ppal-update-clip"}
                             for name, _args in session.calls))
        self.assertEqual(preview["intended_operation"]["arguments"]["path"], "t1/s0")
        self.assertIsInstance(preview["intended_operation"]["arguments"]["notes"], str)

        result = self.run_write(session, confirm=True, **self.kwargs())
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["operation"], "create")
        self.assertEqual(result["path"], "t1/s0")
        self.assertEqual(result["note_count"], 2)

    def test_create_succeeds_next_to_the_devices_advisory_warnings(self):
        # review M1: a good create next to real warnings is kept, not rolled back
        session = FakeClipSession()
        session.device_warnings = list(pp_fakes.DEVICE_ADVISORY_WARNINGS[self.PP_VERSION])
        result = self.run_write(session, confirm=True, **self.kwargs())
        self.assertEqual(result["status"], "verified")
        self.assertIn("t1/s0", session.clips)
        self.assertFalse(any(name == "ppal-delete" for name, _args in session.calls))
        self.assertEqual(result["pp_warnings"], session.device_warnings)

    def test_refused_create_is_a_failure_and_leaves_nothing(self):
        # On 2.3 a refused write is an empty result plus a WARNING part, not an
        # error (captured refusal shape: v23/v23_duplicate_midi_to_audio.json).
        class RefusingCreate(FakeClipSession):
            async def call(self, name, args):
                if name == "ppal-create-clip":
                    self.calls.append((name, dict(args)))
                    return pp_session.PPResponse(
                        '{"result":[]}', [], ["WARNING: clip t1/s0 was not created"])
                return await super().call(name, args)

        session = RefusingCreate()
        with self.assertRaisesRegex(clip_mutation.ClipMutationError,
                                    "MIDI clip create was not done: .*no object.*WARNING"):
            self.run_write(session, confirm=True, **self.kwargs())
        self.assertNotIn("t1/s0", session.clips)
        if self.PP_VERSION in PATH_VERSIONS:
            self.assertIn(("ppal-read-clip", {"path": "t1/s0", "include": ["notes", "timing"]}),
                          session.wire_calls)

    def test_occupied_refusal_and_verified_replace(self):
        session = FakeClipSession(occupied=True)
        with self.assertRaisesRegex(clip_mutation.ClipMutationError, "occupied"):
            self.run_write(session, confirm=True, **self.kwargs())
        self.assertFalse(any(name == "ppal-update-clip" for name, _args in session.calls))

        result = self.run_write(session, replace=True, confirm=True, **self.kwargs())
        self.assertEqual(result["operation"], "replace")
        update = next(args for name, args in session.calls if name == "ppal-update-clip")
        self.assertEqual(update["preTransforms"], "delete")
        self.assertEqual(update["id"], "existing")

        hostile = FakeClipSession(occupied=True)
        hostile.clips["t1/s0"]["path"] = "t0/s0"
        hostile.clips["t1/s0"]["slot"] = "0/0"
        with self.assertRaisesRegex(clip_mutation.ClipMutationError, "different"):
            self.run_write(hostile, replace=True, confirm=True, **self.kwargs())
        self.assertFalse(any(name == "ppal-update-clip" for name, _args in hostile.calls))

    def test_update_and_cleanup_always_use_schema_id_argument(self):
        session = FakeClipSession(supports_2_2=False, occupied=True)
        self.run_write(session, replace=True, confirm=True, **self.kwargs())
        read = next(args for name, args in session.calls if name == "ppal-read-clip")
        update = next(args for name, args in session.calls if name == "ppal-update-clip")
        self.assertEqual(read["path"], "t1/s0")
        self.assertEqual(update["id"], "existing")
        self.assertNotIn("ids", update)

    def test_non_sidekick_audio_and_readback_mismatch_refuse(self):
        for kwargs in (
            self.kwargs(track_name="Vocals"),
            self.kwargs(track_name="SIDEKICK EARS"),
        ):
            session = FakeClipSession()
            with self.assertRaises((ValueError, clip_mutation.ClipMutationError)):
                self.run_write(session, confirm=True, **kwargs)
            self.assertFalse(any(name in {"ppal-create-clip", "ppal-update-clip"}
                                 for name, _args in session.calls))
        ears = FakeClipSession()
        with self.assertRaises(clip_mutation.ClipMutationError):
            self.run_write(ears, confirm=True, **self.kwargs(track_name="SIDEKICK EARS"))
        self.assertIn(("ppal-read-track", {"path": "t2", "include": []}
                       if self.PP_VERSION in PATH_VERSIONS else {"trackIndex": 2, "include": []}),
                      ears.wire_calls)
        with self.assertRaises(clip_mutation.ClipMutationError):
            mismatch = FakeClipSession(mismatch=True)
            self.run_write(mismatch, confirm=True, **self.kwargs())
        self.assertNotIn("t1/s0", mismatch.clips)

        incomplete = FakeClipSession(mismatch=True, delete_fails=True)
        with self.assertRaisesRegex(clip_mutation.ClipMutationError,
                                   "rollback could not be verified"):
            self.run_write(incomplete, confirm=True, **self.kwargs())
        self.assertIn("t1/s0", incomplete.clips)

    def test_scene_must_already_exist(self):
        session = FakeClipSession()
        with self.assertRaisesRegex(clip_mutation.ClipMutationError, "existing"):
            self.run_write(session, confirm=True, **self.kwargs(scene_index=4))
        self.assertFalse(any(name == "ppal-create-clip" for name, _args in session.calls))

    def test_failed_replace_restores_complete_previous_clip(self):
        session = FakeClipSession(occupied=True, mismatch=True)
        with self.assertRaises(clip_mutation.ClipMutationError):
            self.run_write(session, replace=True, confirm=True, **self.kwargs())
        restored = session.clips["t1/s0"]
        self.assertEqual(restored["id"], "existing")
        self.assertEqual(restored["name"], "Old")
        self.assertEqual(pp_codec.midi_json_to_notes(restored["notes"]),
                         pp_codec.midi_json_to_notes(wire(NOTES)))

    def test_unscoreable_receipt_flags_the_public_result(self):
        """The public composed write must not drop scoreable/scoreable_reason:
        an identity_error receipt leaves a write that can never be scored at
        session close, and the caller has to see that. (write_midi_clip no
        longer writes SIDEKICK JAM at all — that surface is refused up front.)"""
        from mcp_server import gears as _gears
        from mcp_server import live_write as _live_write
        receipt = {"status": "verified", "slot": "1/0",
                   "identity_status": "identity_error",
                   "identity_error": "live api unavailable",
                   "track": "SIDEKICK JAM"}
        with tempfile.TemporaryDirectory() as td:
            ledger_path = os.path.join(td, "ledger.json")
            with (mock.patch.object(ledger, "LEDGER_PATH", ledger_path),
                  mock.patch.object(_gears, "validate_composed",
                                    return_value={"passes": True, "score": 1.0,
                                                  "checks": {}}),
                  mock.patch.object(_live_write, "write_answer_to_jam_track",
                                    lambda *_args, **_kwargs: dict(receipt)),
                  mock.patch.object(server, "_run_async",
                                    side_effect=lambda value: value)):
                result = server.write_composed_to_jam(
                    NOTES, {"gear": 5, "key": "D minor"}, name="Unscoreable",
                    confirm=True)
        self.assertTrue(result["wrote"])
        self.assertTrue(result["ledger"]["entry_id"].startswith("e-"))
        self.assertFalse(result["scoreable"])
        self.assertIn("cannot be scored", result["scoreable_reason"])


@on_all_versions
class TestSurgeryClip(unittest.TestCase):
    def run_surgery(self, session, **kwargs):
        return server._run_async(clip_mutation.surgery_clip(
            operation_session=operation(session), **kwargs))

    def test_preview_and_confirm_are_bounded_to_one_bar(self):
        surgery = {"notes": [
            {"pitch": 67, "start_beats": 0, "duration_beats": 1, "velocity": 100},
            {"pitch": 69, "start_beats": 2, "duration_beats": 1, "velocity": 95},
        ]}
        session = FakeClipSession()
        preview = self.run_surgery(
            session, path="t1/s2", bar=2, surgery=surgery, confirm=False)
        self.assertEqual(preview["writes"], 0)
        self.assertEqual(preview["intended_operation"]["arguments"]["preTransforms"],
                         "2|*: delete")
        self.assertFalse(any(name == "ppal-update-clip" for name, _args in session.calls))

        result = self.run_surgery(
            session, path="t1/s2", bar=2, surgery=surgery, confirm=True)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["unaffected_notes_verified"], 2)
        after = pp_codec.midi_json_to_notes(session.clips["t1/s2"]["notes"])
        self.assertEqual([note["pitch"] for note in after], [60, 62, 67, 69])

    def test_surgery_succeeds_next_to_the_devices_advisory_warnings(self):
        # review M1: real warnings next to good writes must not fail or roll back
        session = FakeClipSession()
        session.device_warnings = list(pp_fakes.DEVICE_ADVISORY_WARNINGS[self.PP_VERSION])
        result = self.run_surgery(session, path="t1/s2", bar=2, confirm=True, surgery={
            "notes": [{"pitch": 67, "start_beats": 0, "duration_beats": 1, "velocity": 100}]})
        self.assertEqual(result["status"], "verified")
        updates = [args for name, args in session.calls if name == "ppal-update-clip"]
        self.assertEqual(len(updates), 1)   # no rollback update
        self.assertEqual(result["pp_warnings"], session.device_warnings)

    def test_update_the_device_did_not_apply_fails_by_readback(self):
        # review M1: real refusals ("Ran out of time … Not updated", "notes
        # parameter ignored for audio clip") come with a plausible reply; the
        # readback, not the words, catches them and the clip is restored.
        for kind in ("update", "audio_notes"):
            with self.subTest(kind=kind):
                class NotApplied(FakeClipSession):
                    async def call(self, name, args):
                        if name == "ppal-update-clip" and args.get("preTransforms") != "delete":
                            self.calls.append((name, dict(args)))
                            return response({"id": args["id"], "path": "t1/s2"})
                        return await super().call(name, args)

                session = NotApplied()
                # 2.4 captured no "Ran out of time" / "notes parameter ignored"
                # refusal: the 2.3 texts stand in — the readback decides, not the words
                session.device_warnings = [pp_fakes.DEVICE_REFUSAL_WARNINGS[
                    V23 if self.PP_VERSION == V24 else self.PP_VERSION][kind]]
                before = session.clips["t1/s2"]["notes"]
                with self.assertRaisesRegex(clip_mutation.ClipMutationError,
                                            "replacement readback mismatch"):
                    self.run_surgery(session, path="t1/s2", bar=2, confirm=True, surgery={
                        "notes": [{"pitch": 67, "start_beats": 0, "duration_beats": 1,
                                   "velocity": 100}]})
                self.assertEqual(session.clips["t1/s2"]["notes"], before)

    def test_unaffected_mutation_and_non_sidekick_refuse(self):
        with self.assertRaisesRegex(clip_mutation.ClipMutationError, "outside"):
            hostile = FakeClipSession(mutate_unaffected=True)
            self.run_surgery(
                hostile, path="t1/s2", bar=2,
                surgery={"notes": []}, confirm=True)
        restored = pp_codec.midi_json_to_notes(hostile.clips["t1/s2"]["notes"])
        self.assertEqual([note["pitch"] for note in restored], [60, 62, 64, 65])

        session = FakeClipSession()
        session.clips["t0/s0"] = session.clip("user-clip", "t0/s0", "User", NOTES,
                                                     length="1bar")
        with self.assertRaisesRegex(clip_mutation.ClipMutationError, "non-Sidekick"):
            self.run_surgery(
                session, path="t0/s0", bar=1, surgery={"notes": []}, confirm=True)
        self.assertFalse(any(name == "ppal-update-clip" for name, _args in session.calls))

    def test_replacement_notes_must_fit_selected_bar(self):
        with self.assertRaisesRegex(ValueError, "fit wholly"):
            self.run_surgery(
                FakeClipSession(), path="t1/s2", bar=2,
                surgery={"notes": [{"pitch": 60, "start_beats": 3.5,
                                     "duration_beats": 1, "velocity": 90}]},
                confirm=False)
        with self.assertRaisesRegex(clip_mutation.ClipMutationError, "outside"):
            self.run_surgery(
                FakeClipSession(), path="t1/s2", bar=3,
                surgery={"notes": []}, confirm=False)

    def test_compound_meter_uses_quarter_note_live_beats(self):
        session = FakeClipSession()
        clip = session.clips["t1/s2"]
        clip["timeSignature"] = "6/8"
        clip["notes"] = wire([
            {"pitch": 60, "start_beats": 0, "duration_beats": 1, "velocity": 90},
            {"pitch": 64, "start_beats": 3, "duration_beats": 1, "velocity": 80},
        ])
        result = self.run_surgery(
            session, path="t1/s2", bar=2,
            surgery={"notes": [{"pitch": 67, "start_beats": 0,
                                 "duration_beats": 0.5, "velocity": 100}]},
            confirm=True)
        self.assertEqual(result["status"], "verified")
        after = pp_codec.midi_json_to_notes(clip["notes"])
        self.assertEqual([(note["pitch"], note["start_beats"]) for note in after],
                         [(60, 0.0), (67, 3.0)])

    def test_profiles_and_public_signatures(self):
        for profile in ("compose", "session", "all"):
            self.assertIn("write_midi_clip", profiles.get_profile_tools(profile))
            self.assertIn("surgery_clip", profiles.get_profile_tools(profile))
        for profile in ("reference", "debug"):
            self.assertNotIn("write_midi_clip", profiles.get_profile_tools(profile))
            self.assertNotIn("surgery_clip", profiles.get_profile_tools(profile))
        self.assertEqual(
            list(inspect.signature(server.write_midi_clip).parameters),
            ["track_name", "scene_index", "notes", "name", "loop_start",
             "loop_length", "replace", "confirm", "source", "role", "gear",
             "metadata"],
        )
        self.assertEqual(
            list(inspect.signature(server.surgery_clip).parameters),
            ["path", "bar", "surgery", "confirm"],
        )


if __name__ == "__main__":
    unittest.main()
