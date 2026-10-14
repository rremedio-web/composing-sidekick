from __future__ import annotations

import json
import inspect
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from mcp_server import lane_audition, ledger, pp_session, profiles, server
from tests.pp_fakes import V22, V23, schema_strict


def response(result):
    raw = json.dumps({"result": result})
    return pp_session.PPResponse(raw, result, [])


NOTES = [{"p": 60, "s": 0, "d": 1, "v": 90}]


@schema_strict
class FakeLaneSession:
    def __init__(self, *, supports_2_2=True, fail_lane_at=None,
                 promotion_mismatch=False, delete_fails=False,
                 fail_promoted_read=False):
        self.capabilities = {"supports_2_2": supports_2_2}
        self.handshake = {"pp_version": self.pp_version if supports_2_2 else "2.1.0"}
        self.calls = []
        self.events = []
        self.fail_lane_at = fail_lane_at
        self.promotion_mismatch = promotion_mismatch
        self.delete_fails = delete_fails
        self.fail_promoted_read = fail_promoted_read
        self.deleted = []
        self.lane_count = 0
        self.clips = {
            "source-clip": {"id": "source-clip", "path": "t0/s0", "name": "Loop",
                            "type": "midi", "notes": NOTES},
        }
        self.tracks = {
            0: {"id": "source-track", "name": "Lead", "trackIndex": 0,
                # Mixer fields match tests/fixtures/pp/2.2.0-rc1/read/read_track_t0_index.json.
                "gainDb": 0, "pan": 0, "sends": [],
                "devices": [{"id": "source-device", "name": "Synth"}],
                "sessionClips": [{"id": "source-clip", "path": "t0/s0"}],
                "arrangementClips": []},
        }

    async def call(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "ppal-read-live-set":
            return response({"tracks": [
                {"id": track["id"], "name": track["name"], "trackIndex": index}
                for index, track in sorted(self.tracks.items())]})
        if name == "ppal-read-track":
            return response(json.loads(json.dumps(self.tracks[args["trackIndex"]])))
        if name == "ppal-read-clip":
            clip_id = args.get("id")
            if clip_id is None:
                path = args.get("path")
                clip_id = next((item_id for item_id, clip in self.clips.items()
                                if clip["path"] == path), None)
            if (self.fail_promoted_read and isinstance(clip_id, str)
                    and clip_id.startswith("promoted-")):
                raise RuntimeError("promoted clip gone")
            clip = self.clips.get(clip_id)
            if clip is None:
                raise AssertionError((name, args))
            if str(clip_id).startswith("promoted-"):
                self.events.append("read-promoted")
            return response(json.loads(json.dumps(clip)))
        if name == "ppal-duplicate" and args["type"] == "track":
            self.tracks[1] = {
                "id": "audition-track", "name": args["name"], "trackIndex": 1,
                "gainDb": 0, "pan": 0, "sends": [],
                "devices": [{"id": "new-device", "name": "Synth"}],
                "sessionClips": [], "arrangementClips": [],
            }
            return response({"id": "audition-track", "trackIndex": 1})
        if name == "ppal-duplicate" and args["type"] == "clip":
            destination = args["toPath"]
            if destination.endswith("/l+"):
                if self.fail_lane_at == self.lane_count:
                    raise RuntimeError("simulated lane failure")
                path = "t1/l%d" % self.lane_count
                clip_id = "lane-%d" % self.lane_count
                self.lane_count += 1
            elif args["id"].startswith("lane-"):
                path = "t1"
                clip_id = "promoted-%s" % args["id"]
                self.events.append("duplicate-promoted")
            else:
                path = "t1"
                clip_id = "main-clip"
            notes = NOTES
            if self.promotion_mismatch and clip_id.startswith("promoted-"):
                notes = [{"p": 61, "s": 0, "d": 1, "v": 90}]
            self.clips[clip_id] = {
                "id": clip_id, "path": path, "name": "Loop", "type": "midi",
                "notes": notes, "arrangementStart": "1|1",
            }
            if path == "t1":
                # the main lane lists the copy; a copy over the clip at that
                # position replaces it (v24_duplicate_over_existing)
                self.tracks[1]["arrangementClips"] = [
                    clip for clip in self.tracks[1]["arrangementClips"]
                    if clip.get("arrangementStart") != args["arrangementStart"]
                ] + [{"id": clip_id, "path": "t1", "arrangementStart": args["arrangementStart"]}]
            # 2.2 duplicate reply keeps the position (duplicate_clip_to_arrangement.json)
            return response([{"arrangementStart": args["arrangementStart"], "id": clip_id,
                              "path": path}])
        if name == "ppal-delete":
            if self.delete_fails:
                raise RuntimeError("delete unavailable")
            self.deleted.append(args["id"])
            self.tracks.pop(1, None)
            return response({"deleted": True})
        raise AssertionError((name, args))


def operation(session):
    @asynccontextmanager
    async def factory(*_args, **_kwargs):
        yield session
    return factory


def speak(session, version):
    """The same Set after its Producer Pal device changed build."""
    session.pp_version = version
    session.handshake = {"pp_version": version}
    return session


class TestCopyPositions(unittest.TestCase):
    """review minor: copy positions are verified from the 2.3 path too."""

    def test_same_location_compares_positions_when_both_paths_have_one(self):
        same = lane_audition._same_location
        self.assertTrue(same("t1[1|1]", "t1"))
        self.assertTrue(same("t1[1|1]", "t1[1|1.0]"))
        self.assertFalse(same("t1[5|1]", "t1[1|1]"))
        self.assertTrue(same("t1/l0[1|1]", "t1/l0"))
        self.assertFalse(same("t1/l1[1|1]", "t1/l0[1|1]"))
        self.assertFalse(same("t2[1|1]", "t1"))

    def test_one_clip_refuses_a_copy_at_another_position(self):
        for payload in ([{"id": "c", "path": "t1[9|1]"}],
                        [{"arrangementStart": "9|1", "id": "c", "path": "t1"}]):
            with self.subTest(payload=payload), self.assertRaisesRegex(
                    lane_audition.LaneAuditionError, "landed at 9\\|1, not at 1\\|1"):
                lane_audition._one_clip(response(payload), "t1", "main-lane duplicate", "1|1")
        self.assertEqual(lane_audition._one_clip(
            response([{"id": "c", "path": "t1[1|1]"}]), "t1", "main", "1|1")["id"], "c")


class TestLaneAuditionAcrossBuilds(unittest.TestCase):
    """Take lanes are CREATED only on 2.2 (the 2.3 forms are not captured);
    an audition made on 2.2 is promoted on 2.3 with the captured 2.3 lane
    promotion form (v23b_promote_lane_clip: toPath 't<i>[pos]', re-created with
    a new id)."""

    def run_audition(self, session, registry, **kwargs):
        return server._run_async(lane_audition.audition_in_lanes(
            operation_session=operation(session), registry_path=registry, **kwargs))

    def test_audition_on_23_is_refused_before_any_write(self):
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            session = speak(FakeLaneSession(), V23)
            for confirm in (False, True):
                result = self.run_audition(session, registry, source_path="t0/s0", n=2,
                                           seed=1, confirm=confirm)
                self.assertEqual(result["status"], "unsupported_capability")
                self.assertEqual(result["pp_version"], V23)
                self.assertIn("cannot be deleted through the API", result["reason"])
                self.assertNotIn("promote_lane still works", result["reason"])
                self.assertEqual(result["writes"], 0)
            self.assertEqual(session.calls, [])
            self.assertFalse(Path(registry).exists())

    def test_audition_and_promotion_report_the_devices_warnings(self):
        from tests import pp_fakes
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            session = speak(FakeLaneSession(), V22)
            session.device_warnings = list(pp_fakes.DEVICE_ADVISORY_WARNINGS[V22])
            audition = self.run_audition(session, registry, source_path="t0/s0", n=1,
                                         seed=4, confirm=True)
            self.assertEqual(audition["status"], "verified")
            self.assertEqual(audition["pp_warnings"], session.device_warnings)
            result = server._run_async(lane_audition.promote_lane(
                audition["audition_id"], audition["candidates"][0]["candidate_id"],
                confirm=True, operation_session=operation(session), registry_path=registry,
                keep_recorder=lambda **kwargs: {"entry_id": "e-22", "verdict": "keep"}))
            self.assertEqual(result["status"], "verified_keep")
            self.assertEqual(result["pp_warnings"], session.device_warnings)

    def test_22_audition_promotes_on_23_with_the_path_position(self):
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            session = speak(FakeLaneSession(), V22)
            audition = self.run_audition(session, registry, source_path="t0/s0", n=1,
                                         seed=4, confirm=True)
            self.assertEqual(audition["status"], "verified")
            candidate_id = audition["candidates"][0]["candidate_id"]
            speak(session, V23)
            session.wire_calls = []

            result = server._run_async(lane_audition.promote_lane(
                audition["audition_id"], candidate_id, confirm=True,
                operation_session=operation(session), registry_path=registry,
                keep_recorder=lambda **kwargs: {"entry_id": "e-23", "verdict": "keep"}))
            self.assertEqual(result["status"], "verified_keep")
            self.assertEqual(result["promoted_clip_id"], "promoted-lane-0")
            self.assertEqual(result["pp_warnings"], [])
            sent = [args for name, args in session.wire_calls if name == "ppal-duplicate"]
            # v23/v23_duplicate_to_position.json form: the start is in toPath
            self.assertEqual(sent, [{"type": "clip", "id": "lane-0", "toPath": "t1[1|1]"}])
            self.assertIn(("ppal-read-track", {"path": "t1", "include": [
                "mixer", "devices", "notes", "session-clips", "arrangement-clips"]}),
                session.wire_calls)

    def test_promotion_mismatch_on_23_prevents_keep(self):
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            session = speak(FakeLaneSession(promotion_mismatch=True), V22)
            audition = self.run_audition(session, registry, source_path="t0/s0", n=1,
                                         seed=4, confirm=True)
            speak(session, V23)
            kept = []
            with self.assertRaises(pp_session.LiveWriteVerificationError):
                server._run_async(lane_audition.promote_lane(
                    audition["audition_id"], audition["candidates"][0]["candidate_id"],
                    confirm=True, operation_session=operation(session),
                    registry_path=registry, keep_recorder=lambda **kw: kept.append(kw)))
            self.assertEqual(kept, [])


class TestLaneAudition(unittest.TestCase):
    def test_captured_read_clip_path_is_midi_and_audio_is_rejected(self):
        fixture = (Path(__file__).parent / "fixtures" / "pp" / "2.2.0-rc1"
                   / "legacy" / "legacy_read_clip_path.json")
        # This response shape is captured from Producer Pal 2.2.0-rc1.
        capture = json.loads(fixture.read_text())
        clip = capture["response"]["content"][0]["json"]
        captured_response = response(clip)
        with patch.object(lane_audition.pp_session, "extract_notes", return_value=NOTES):
            self.assertEqual(lane_audition._clip_dict(
                captured_response, clip_id=clip["id"], expected_path=clip["path"],
                label="captured clip"), clip)
            with self.assertRaisesRegex(lane_audition.LaneAuditionError,
                                        "readback mismatch"):
                lane_audition._clip_dict(
                    response({**clip, "type": "audio"}), clip_id=clip["id"],
                    expected_path=clip["path"], label="non-MIDI clip")

    def run_audition(self, session, registry, **kwargs):
        return server._run_async(lane_audition.audition_in_lanes(
            operation_session=operation(session), registry_path=registry, **kwargs))

    def create_audition(self, session, registry, n=3):
        return self.run_audition(
            session, registry, source_path="t0/s0", n=n, seed=4, confirm=True)

    def test_preview_and_21_refusal_perform_no_writes(self):
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            session = FakeLaneSession()
            result = self.run_audition(
                session, registry, source_path="t0/s0", n=3, seed=4, confirm=False)
            self.assertEqual(result["status"], "preview")
            self.assertEqual(result["candidate_count"], 3)
            self.assertEqual(result["writes"], 0)
            self.assertFalse(Path(registry).exists())
            self.assertFalse(any(name in {"ppal-duplicate", "ppal-delete"}
                                 for name, _args in session.calls))

            old = FakeLaneSession(supports_2_2=False)
            result = self.run_audition(
                old, registry, source_path="t0/s0", confirm=True)
            self.assertEqual(result["status"], "unsupported_capability")
            self.assertEqual(result["pp_version"], "2.1.0")
            self.assertFalse(any(name == "ppal-duplicate" for name, _args in old.calls))

    def test_confirm_creates_fresh_track_main_and_three_verified_lanes(self):
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            session = FakeLaneSession()
            result = self.create_audition(session, registry)
            self.assertEqual(result["status"], "verified")
            self.assertEqual(result["track_id"], "audition-track")
            self.assertEqual(len(result["candidates"]), 3)
            self.assertFalse(result["envelopes_preserved"])
            track_args = next(args for name, args in session.calls
                              if name == "ppal-duplicate" and args["type"] == "track")
            self.assertTrue(track_args["withoutClips"])
            self.assertFalse(track_args["withoutDevices"])
            clip_calls = [args for name, args in session.calls
                          if name == "ppal-duplicate" and args["type"] == "clip"]
            self.assertEqual(clip_calls[0]["toPath"], "t1")
            self.assertEqual([args["toPath"] for args in clip_calls[1:]], ["t1/l+"] * 3)
            self.assertTrue(all("transforms" in args for args in clip_calls[1:]))
            stored = json.loads(Path(registry).read_text())
            record = stored["auditions"][0]
            self.assertEqual(record["audition_id"], result["audition_id"])
            self.assertEqual(len(record["candidates"]), 3)
            self.assertNotIn("registry_path", json.dumps(result))

    def test_limit_and_partial_failure_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            with self.assertRaisesRegex(ValueError, "1 through 8"):
                self.run_audition(FakeLaneSession(), registry,
                                  source_path="t0/s0", n=9, confirm=True)
            session = FakeLaneSession(fail_lane_at=1)
            with self.assertRaisesRegex(RuntimeError, "simulated lane failure"):
                self.create_audition(session, registry)
            self.assertEqual(session.deleted, ["audition-track"])
            self.assertFalse(Path(registry).exists())

    def test_promote_rereads_before_keep_and_preserves_lane_arrangement_start(self):
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            session = FakeLaneSession()
            audition = self.create_audition(session, registry, n=1)
            candidate_id = audition["candidates"][0]["candidate_id"]

            def keep_recorder(**kwargs):
                session.events.append("keep")
                self.assertEqual(kwargs["clip_id"], "promoted-lane-0")
                return {"entry_id": "e-verified", "verdict": "keep"}

            preview = server._run_async(lane_audition.promote_lane(
                audition["audition_id"], candidate_id, confirm=False,
                operation_session=operation(session), registry_path=registry,
                keep_recorder=keep_recorder))
            self.assertEqual(preview["status"], "preview")
            self.assertEqual(session.events, [])

            result = server._run_async(lane_audition.promote_lane(
                audition["audition_id"], candidate_id, confirm=True,
                operation_session=operation(session), registry_path=registry,
                keep_recorder=keep_recorder))
            self.assertEqual(result["status"], "verified_keep")
            self.assertTrue(result["keep_recorded"])
            self.assertEqual(session.events, [
                "duplicate-promoted", "read-promoted", "keep"])
            promote_args = [args for name, args in session.calls
                            if name == "ppal-duplicate" and args.get("id") == "lane-0"][-1]
            self.assertEqual(promote_args, {
                "type": "clip", "id": "lane-0", "toPath": "t1",
                "arrangementStart": "1|1"})

    def test_promote_refuses_when_his_clip_sits_on_the_main_lane(self):
        # the promotion copies the lane clip onto the main lane at the lane's
        # position; a clip there that is not the audition's would be replaced
        # silently (v24_duplicate_over_existing) — refused before any write
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            session = FakeLaneSession()
            audition = self.create_audition(session, registry, n=1)
            candidate_id = audition["candidates"][0]["candidate_id"]
            self.assertEqual([c["id"] for c in session.tracks[1]["arrangementClips"]],
                             ["main-clip"])
            session.tracks[1]["arrangementClips"].append(
                {"id": "his-clip", "path": "t1", "arrangementStart": "1|1"})
            before = len(session.calls)
            with self.assertRaisesRegex(lane_audition.LaneAuditionError,
                                        "main lane holds clip his-clip at 1\\|1"):
                server._run_async(lane_audition.promote_lane(
                    audition["audition_id"], candidate_id, confirm=True,
                    operation_session=operation(session), registry_path=registry,
                    keep_recorder=lambda **kw: self.fail("no keep without a promotion")))
            self.assertFalse(any(name == "ppal-duplicate"
                                 for name, _args in session.calls[before:]))
            self.assertEqual(session.events, [])

    def test_promote_twice_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            ledger_path = str(Path(td) / "session_ledger.json")
            session = FakeLaneSession()
            audition = self.create_audition(session, registry, n=1)
            candidate_id = audition["candidates"][0]["candidate_id"]
            with patch.object(ledger, "LEDGER_PATH", ledger_path):
                first = server._run_async(lane_audition.promote_lane(
                    audition["audition_id"], candidate_id, confirm=True,
                    operation_session=operation(session), registry_path=registry))
                second = server._run_async(lane_audition.promote_lane(
                    audition["audition_id"], candidate_id, confirm=True,
                    operation_session=operation(session), registry_path=registry))
                entries = ledger.load(ledger_path)["entries"]
            self.assertEqual(first["status"], "verified_keep")
            self.assertTrue(first["keep_recorded"])
            self.assertNotIn("idempotent", first)
            # the second confirm reports the existing promotion…
            self.assertEqual(second["status"], "verified_keep")
            self.assertTrue(second.get("idempotent"))
            self.assertEqual(second["promoted_clip_id"], first["promoted_clip_id"])
            self.assertEqual(second["keep"]["entry_id"], first["keep"]["entry_id"])
            # …and duplicated in Live / recorded in the ledger exactly once
            self.assertEqual(session.events.count("duplicate-promoted"), 1)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["suggestion_id"], candidate_id)
            self.assertEqual(entries[0]["immediate_verdict"], "keep")

    def test_retry_after_verdict_failure_records_one_real_keep(self):
        """A first attempt that stamped the entry but died at record_verdict
        must heal into exactly one KEEP on retry — no phantom verdict."""
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            ledger_path = str(Path(td) / "session_ledger.json")
            session = FakeLaneSession()
            audition = self.create_audition(session, registry, n=1)
            candidate_id = audition["candidates"][0]["candidate_id"]
            real_verdict = ledger.record_verdict
            failures = {"n": 0}

            def flaky_verdict(verdict, **kwargs):
                if failures["n"] == 0:
                    failures["n"] += 1
                    raise RuntimeError("verdict store down")
                return real_verdict(verdict, **kwargs)

            with patch.object(ledger, "LEDGER_PATH", ledger_path):
                with patch.object(ledger, "record_verdict", flaky_verdict):
                    failed = server._run_async(lane_audition.promote_lane(
                        audition["audition_id"], candidate_id, confirm=True,
                        operation_session=operation(session), registry_path=registry))
                retried = server._run_async(lane_audition.promote_lane(
                    audition["audition_id"], candidate_id, confirm=True,
                    operation_session=operation(session), registry_path=registry))
                entries = ledger.load(ledger_path)["entries"]
            self.assertEqual(failed["status"], "verified_keep_failed")
            self.assertEqual(retried["status"], "verified_keep")
            self.assertEqual(retried["keep"]["verdict"], "keep")
            # exactly one entry, and its verdict was actually stored
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["suggestion_id"], candidate_id)
            self.assertEqual(entries[0]["immediate_verdict"], "keep")

    def test_registry_failure_then_retry_leaves_one_keep_and_one_duplicate(self):
        """A registry save failing once after the KEEP must not raise; the
        retry finishes the accounting against the stored clip — one KEEP
        entry, one Live duplicate, bookkeeping healed."""
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            ledger_path = str(Path(td) / "session_ledger.json")
            session = FakeLaneSession()
            audition = self.create_audition(session, registry, n=1)
            candidate_id = audition["candidates"][0]["candidate_id"]
            real_save = lane_audition.core_shared._save_json_atomic
            saves = {"n": 0}

            def flaky_save(path, data):
                if path == registry:
                    saves["n"] += 1
                    # 1st registry save: pre-keep promoted_clip_id (ok)
                    # 2nd registry save: post-keep entry_id (fails once)
                    if saves["n"] == 2:
                        raise OSError("registry disk full")
                return real_save(path, data)

            with patch.object(ledger, "LEDGER_PATH", ledger_path):
                with patch.object(lane_audition.core_shared, "_save_json_atomic",
                                  flaky_save):
                    first = server._run_async(lane_audition.promote_lane(
                        audition["audition_id"], candidate_id, confirm=True,
                        operation_session=operation(session), registry_path=registry))
                    second = server._run_async(lane_audition.promote_lane(
                        audition["audition_id"], candidate_id, confirm=True,
                        operation_session=operation(session), registry_path=registry))
                entries = ledger.load(ledger_path)["entries"]
            self.assertEqual(first["status"], "verified_keep")
            self.assertTrue(first["keep_recorded"])
            self.assertIn("OSError", first["registry_error"])
            self.assertEqual(second["status"], "verified_keep")
            self.assertNotIn("registry_error", second)
            # exactly one Live duplicate and one KEEP entry, ever
            self.assertEqual(session.events.count("duplicate-promoted"), 1)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["immediate_verdict"], "keep")
            stored = json.loads(Path(registry).read_text())
            row = stored["auditions"][0]["candidates"][0]
            self.assertEqual(row["entry_id"], entries[0]["id"])
            self.assertEqual(row["promoted_clip_id"], "promoted-lane-0")
            self.assertIn("session_id", row)

    def test_pre_keep_registry_failure_still_records_keep(self):
        """The pre-KEEP registry write failing must not cost the KEEP: the
        promotion is verified in Live, so the verdict still records, the
        error is reported, and a retry replays without duplicating again."""
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            ledger_path = str(Path(td) / "session_ledger.json")
            session = FakeLaneSession()
            audition = self.create_audition(session, registry, n=1)
            candidate_id = audition["candidates"][0]["candidate_id"]
            real_save = lane_audition.core_shared._save_json_atomic
            saves = {"n": 0}

            def flaky_save(path, data):
                if path == registry:
                    saves["n"] += 1
                    # 1st registry save: pre-keep promoted_clip_id (fails once)
                    # 2nd registry save: post-keep entry_id (ok)
                    if saves["n"] == 1:
                        raise OSError("registry disk full")
                return real_save(path, data)

            with patch.object(ledger, "LEDGER_PATH", ledger_path):
                with patch.object(lane_audition.core_shared, "_save_json_atomic",
                                  flaky_save):
                    first = server._run_async(lane_audition.promote_lane(
                        audition["audition_id"], candidate_id, confirm=True,
                        operation_session=operation(session), registry_path=registry))
                second = server._run_async(lane_audition.promote_lane(
                    audition["audition_id"], candidate_id, confirm=True,
                    operation_session=operation(session), registry_path=registry))
                entries = ledger.load(ledger_path)["entries"]
            self.assertEqual(first["status"], "verified_keep")
            self.assertTrue(first["keep_recorded"])
            self.assertIn("OSError", first["registry_error"])
            self.assertEqual(second["status"], "verified_keep")
            self.assertTrue(second.get("idempotent"))
            self.assertNotIn("registry_error", second)
            # exactly one Live duplicate and one KEEP entry, ever
            self.assertEqual(session.events.count("duplicate-promoted"), 1)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["immediate_verdict"], "keep")
            stored = json.loads(Path(registry).read_text())
            row = stored["auditions"][0]["candidates"][0]
            self.assertEqual(row["entry_id"], entries[0]["id"])
            self.assertEqual(row["promoted_clip_id"], "promoted-lane-0")

    def test_replay_reports_stored_verdict_without_inventing_one(self):
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            ledger_path = str(Path(td) / "session_ledger.json")
            session = FakeLaneSession()
            audition = self.create_audition(session, registry, n=1)
            candidate_id = audition["candidates"][0]["candidate_id"]
            with patch.object(ledger, "LEDGER_PATH", ledger_path):
                promoted = server._run_async(lane_audition.promote_lane(
                    audition["audition_id"], candidate_id, confirm=True,
                    operation_session=operation(session), registry_path=registry))
                stored = json.loads(Path(ledger_path).read_text())
                stored["entries"][0]["immediate_verdict"] = None
                Path(ledger_path).write_text(json.dumps(stored))
                with self.assertRaisesRegex(
                        lane_audition.LaneAuditionError, "no recorded KEEP"):
                    server._run_async(lane_audition.promote_lane(
                        audition["audition_id"], candidate_id, confirm=True,
                        operation_session=operation(session), registry_path=registry))
            self.assertTrue(promoted["keep_recorded"])

    def test_replay_of_dropped_entry_reports_already_judged(self):
        """A promotion whose ledger entry was DROPped afterwards must not be
        echoed as verified_keep: the replay reports already_judged with the
        stored verdict and records nothing new."""
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            ledger_path = str(Path(td) / "session_ledger.json")
            session = FakeLaneSession()
            audition = self.create_audition(session, registry, n=1)
            candidate_id = audition["candidates"][0]["candidate_id"]
            with patch.object(ledger, "LEDGER_PATH", ledger_path):
                promoted = server._run_async(lane_audition.promote_lane(
                    audition["audition_id"], candidate_id, confirm=True,
                    operation_session=operation(session), registry_path=registry))
                self.assertEqual(promoted["status"], "verified_keep")
                # the entry was DROPped after promotion (the ledger forbids
                # a conflicting second verdict, so set the stored state the
                # way a drop leaves it, as the neighbouring replay test does)
                stored = json.loads(Path(ledger_path).read_text())
                stored["entries"][0]["immediate_verdict"] = "drop"
                Path(ledger_path).write_text(json.dumps(stored))
                replay = server._run_async(lane_audition.promote_lane(
                    audition["audition_id"], candidate_id, confirm=True,
                    operation_session=operation(session), registry_path=registry))
                entries = ledger.load(ledger_path)["entries"]
            self.assertEqual(replay["status"], "already_judged")
            self.assertEqual(replay["verdict"], "drop")
            self.assertFalse(replay["keep_recorded"])
            self.assertEqual(replay["promoted_clip_id"],
                             promoted["promoted_clip_id"])
            self.assertEqual(session.events.count("duplicate-promoted"), 1)
            self.assertEqual(len(entries), 1)

    def test_resume_against_dropped_entry_reports_already_judged(self):
        """The resume path (stored promoted_clip_id, no entry_id) reaches
        _record_keep's existing-entry branch: a stored non-keep verdict must
        report already_judged, not verified_keep."""
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            ledger_path = str(Path(td) / "session_ledger.json")
            session = FakeLaneSession()
            audition = self.create_audition(session, registry, n=1)
            candidate_id = audition["candidates"][0]["candidate_id"]
            session.clips["promoted-lane-0"] = {
                "id": "promoted-lane-0", "path": "t1", "name": "Loop",
                "type": "midi", "notes": NOTES, "arrangementStart": "1|1",
            }
            stored = json.loads(Path(registry).read_text())
            stored["auditions"][0]["candidates"][0]["promoted_clip_id"] = \
                "promoted-lane-0"
            Path(registry).write_text(json.dumps(stored))
            with patch.object(ledger, "LEDGER_PATH", ledger_path):
                entry = ledger.record_write(
                    clip_name="Loop", slot="t1", source="catalogue",
                    track="SIDEKICK AUD x", source_phrase_id=candidate_id,
                    n_notes=1, suggestion_id=candidate_id)
                ledger.record_verdict("drop", entry_id=entry["id"])
                result = server._run_async(lane_audition.promote_lane(
                    audition["audition_id"], candidate_id, confirm=True,
                    operation_session=operation(session), registry_path=registry))
            self.assertEqual(result["status"], "already_judged")
            self.assertEqual(result["verdict"], "drop")
            self.assertFalse(result["keep_recorded"])
            self.assertEqual(session.events.count("duplicate-promoted"), 0)

    def test_resume_readback_failure_unwedges_for_next_call(self):
        """A stored promoted_clip_id whose clip is gone or changed must not
        wedge forever: the failing resume reports resume_failed naming the
        stale clip, clears the pointer, and the next call duplicates afresh."""
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            ledger_path = str(Path(td) / "session_ledger.json")
            session = FakeLaneSession(fail_promoted_read=True)
            audition = self.create_audition(session, registry, n=1)
            candidate_id = audition["candidates"][0]["candidate_id"]
            # simulate a first attempt that stored the promoted clip but
            # died before the KEEP was durable
            stored = json.loads(Path(registry).read_text())
            stored["auditions"][0]["candidates"][0]["promoted_clip_id"] = \
                "promoted-lane-0"
            Path(registry).write_text(json.dumps(stored))

            with patch.object(ledger, "LEDGER_PATH", ledger_path):
                first = server._run_async(lane_audition.promote_lane(
                    audition["audition_id"], candidate_id, confirm=True,
                    operation_session=operation(session), registry_path=registry))
                # the failing call duplicated nothing and recorded no KEEP
                self.assertEqual(session.events.count("duplicate-promoted"), 0)
                # the stale pointer is cleared for the next call
                stored = json.loads(Path(registry).read_text())
                self.assertIsNone(
                    stored["auditions"][0]["candidates"][0]["promoted_clip_id"])
                session.fail_promoted_read = False
                second = server._run_async(lane_audition.promote_lane(
                    audition["audition_id"], candidate_id, confirm=True,
                    operation_session=operation(session), registry_path=registry))
                entries = ledger.load(ledger_path)["entries"]
            self.assertEqual(first["status"], "resume_failed")
            self.assertEqual(first["promoted_clip_id"], "promoted-lane-0")
            self.assertIn("promoted-lane-0", first["error"])
            self.assertFalse(first["keep_recorded"])
            self.assertEqual(second["status"], "verified_keep")
            self.assertTrue(second["keep_recorded"])
            self.assertEqual(session.events.count("duplicate-promoted"), 1)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["immediate_verdict"], "keep")

    def test_preview_of_already_promoted_candidate_says_so(self):
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            ledger_path = str(Path(td) / "session_ledger.json")
            session = FakeLaneSession()
            audition = self.create_audition(session, registry, n=1)
            candidate_id = audition["candidates"][0]["candidate_id"]
            with patch.object(ledger, "LEDGER_PATH", ledger_path):
                promoted = server._run_async(lane_audition.promote_lane(
                    audition["audition_id"], candidate_id, confirm=True,
                    operation_session=operation(session), registry_path=registry))
                preview = server._run_async(lane_audition.promote_lane(
                    audition["audition_id"], candidate_id, confirm=False,
                    operation_session=operation(session), registry_path=registry))
            self.assertEqual(preview["status"], "preview")
            self.assertEqual(preview["writes"], 0)
            self.assertTrue(preview["already_promoted"])
            self.assertEqual(preview["promoted_clip_id"], promoted["promoted_clip_id"])
            self.assertEqual(preview["entry_id"], promoted["keep"]["entry_id"])

    def test_promotion_mismatch_prevents_keep(self):
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            session = FakeLaneSession(promotion_mismatch=True)
            audition = self.create_audition(session, registry, n=1)
            candidate_id = audition["candidates"][0]["candidate_id"]
            kept = []
            with self.assertRaises(pp_session.LiveWriteVerificationError):
                server._run_async(lane_audition.promote_lane(
                    audition["audition_id"], candidate_id, confirm=True,
                    operation_session=operation(session), registry_path=registry,
                    keep_recorder=lambda **kwargs: kept.append(kwargs)))
            self.assertEqual(kept, [])

    def test_partial_failure_reports_unremoved_fresh_track(self):
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            session = FakeLaneSession(fail_lane_at=0, delete_fails=True)
            with self.assertRaisesRegex(
                    lane_audition.LaneAuditionError,
                    "cleanup left a fresh Sidekick track: audition-track"):
                self.create_audition(session, registry, n=1)
            self.assertIn(1, session.tracks)
            self.assertFalse(Path(registry).exists())

    def test_promotion_21_and_keep_failure_are_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            session = FakeLaneSession()
            audition = self.create_audition(session, registry, n=1)
            candidate_id = audition["candidates"][0]["candidate_id"]

            old = FakeLaneSession(supports_2_2=False)
            result = server._run_async(lane_audition.promote_lane(
                audition["audition_id"], candidate_id, confirm=True,
                operation_session=operation(old), registry_path=registry))
            self.assertEqual(result["status"], "unsupported_capability")
            self.assertFalse(any(name == "ppal-duplicate" for name, _args in old.calls))

            result = server._run_async(lane_audition.promote_lane(
                audition["audition_id"], candidate_id, confirm=True,
                operation_session=operation(session), registry_path=registry,
                keep_recorder=lambda **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("ledger unavailable"))))
            self.assertEqual(result["status"], "verified_keep_failed")
            self.assertEqual(result["verification"], "verified")
            self.assertFalse(result["keep_recorded"])

    def test_promotion_refuses_reused_track_index(self):
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "auditions.json")
            session = FakeLaneSession()
            audition = self.create_audition(session, registry, n=1)
            candidate_id = audition["candidates"][0]["candidate_id"]
            session.tracks[1]["id"] = "user-track"
            session.tracks[1]["name"] = "Vocals"
            kept = []
            with self.assertRaisesRegex(
                    lane_audition.LaneAuditionError, "identity no longer matches"):
                server._run_async(lane_audition.promote_lane(
                    audition["audition_id"], candidate_id, confirm=True,
                    operation_session=operation(session), registry_path=registry,
                    keep_recorder=lambda **kwargs: kept.append(kwargs)))
            self.assertEqual(kept, [])
            self.assertFalse(any(name == "ppal-duplicate" and args.get("id") == "lane-0"
                                 for name, args in session.calls))

    def test_default_keep_recorder_writes_existing_judgment_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            ledger_path = str(Path(td) / "session_ledger.json")
            with patch.object(ledger, "LEDGER_PATH", ledger_path):
                receipt = lane_audition._record_keep(
                    clip_id="promoted", clip_name="Loop", track_name="SIDEKICK AUD x",
                    track_path="t1", audition_id="a-" + "1" * 24,
                    candidate_id="c-" + "2" * 24, n_notes=1,
                )
                data = ledger.load(ledger_path)
            self.assertEqual(receipt["verdict"], "keep")
            self.assertEqual(data["entries"][0]["immediate_verdict"], "keep")
            self.assertEqual(data["entries"][0]["source_phrase_id"], "c-" + "2" * 24)
            # roles are stored canonically: an absent classification is the
            # honest "unknown", never a silent None
            self.assertEqual(data["entries"][0]["role"], "unknown")

    def test_keep_recorder_stamps_lane_snapshot_role_and_key(self):
        with tempfile.TemporaryDirectory() as td:
            ledger_path = str(Path(td) / "session_ledger.json")
            lane_notes = [{"pitch": 36, "start_beats": 0,
                           "duration_beats": 0.5, "velocity": 100}]
            with patch.object(ledger, "LEDGER_PATH", ledger_path):
                lane_audition._record_keep(
                    clip_id="promoted", clip_name="Bass lane",
                    track_name="SIDEKICK AUD x", track_path="t1",
                    audition_id="a-" + "3" * 24,
                    candidate_id="c-" + "4" * 24, n_notes=1,
                    lane_notes=lane_notes, role="bass", key="D minor",
                )
                row = ledger.load(ledger_path)["entries"][0]
            self.assertEqual(row["answer_notes"], [{
                "pitch": 36, "start": 0.0, "duration": 0.5, "velocity": 100,
            }])
            self.assertEqual(row["role"], "bass")
            self.assertEqual(row["key"], "D minor")

    def test_profiles_and_public_signatures(self):
        for profile in ("compose", "session", "all"):
            self.assertIn("audition_in_lanes", profiles.get_profile_tools(profile))
            self.assertIn("promote_lane", profiles.get_profile_tools(profile))
        for profile in ("reference", "debug"):
            self.assertNotIn("audition_in_lanes", profiles.get_profile_tools(profile))
            self.assertNotIn("promote_lane", profiles.get_profile_tools(profile))
        self.assertEqual(
            list(inspect.signature(server.audition_in_lanes).parameters),
            ["source_path", "n", "seed", "confirm"],
        )
        self.assertEqual(
            list(inspect.signature(server.promote_lane).parameters),
            ["audition_id", "candidate_id", "confirm"],
        )


if __name__ == "__main__":
    unittest.main()
