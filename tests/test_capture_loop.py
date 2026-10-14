from __future__ import annotations

from tests._stage_passthrough import passthrough_listenable_staging
import json
import inspect
import re
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mcp_server import pp_session, profiles, server, session_ears
from mcp_server.capture_loop import CaptureLoopError, capture_loop
from tests import pp_fakes
from tests.pp_fakes import schema_strict, on_both_versions


def response(result):
    raw = json.dumps({"result": result})
    return pp_session.PPResponse(raw, result, [])


@schema_strict
class FakeSession:
    def __init__(self, project: Path, *, live_api=True, signature="3/4",
                 tempo=120, tracks=None, occupied=None, malformed_live=False,
                 ignore_delete=False):
        self.capabilities = {"live_api": live_api}
        self.project = project
        self.signature = signature
        self.tempo = tempo
        self.tracks = tracks if tracks is not None else [
            {"name": "User Source", "trackIndex": 0, "type": "midi"},
            {"name": "SIDEKICK EARS", "trackIndex": 1, "type": "audio"},
        ]
        self.occupied = set(occupied or ())
        self.arm = False
        self.calls = []
        self.scene_launches = []
        self.stops = 0
        self.malformed_live = malformed_live
        self.ignore_delete = ignore_delete
        self.sample = project.parent / "Samples" / "Recorded" / "capture.wav"

    def play_scene_reply(self, index):
        """The build's play-scene reply: 2.3 names the scene it played
        ({playing, scene: {id, name, path}}, v23b_play_scene_by_path); 2.4 the
        same without the name ({playing, scene: {id, path}},
        v24_play_scene_by_path); 2.2 has no capture and this fake answers
        {playing} only."""
        if self.pp_version == "2.3.0":
            return {"playing": True,
                    "scene": {"id": "364", "name": "Scene", "path": "s%d" % index}}
        if self.pp_version == "2.4.0":
            return {"playing": True, "scene": {"id": "610", "path": "s%d" % index}}
        return {"playing": True}

    async def call(self, name, args):
        self.calls.append((name, args))
        if name == "ppal-read-live-set":
            return response({"setName": "Scratch", "tempo": self.tempo,
                             "timeSignature": self.signature, "sceneCount": 3,
                             "tracks": self.tracks})
        if name == "ppal-read-track":
            clips = [{"slot": "1/%d" % i, "name": "occupied"}
                     for i in sorted(self.occupied)]
            return response({"sessionClips": clips})
        if name == "ppal-read-clip":
            return response({"name": "Capture", "sampleFile": str(self.sample),
                             "type": "audio"})
        if name == "ppal-playback":
            if args.get("action") == "stop":     # captured v23b_stop: {playing: false}
                self.stops += 1
                return response({"playing": False})
            self.scene_launches.append(args["sceneIndex"])
            reply = getattr(self, "playback_reply", None)
            if reply is None:
                reply = self.play_scene_reply(args["sceneIndex"])
            return response(reply)
        if name != "ppal-live-api":
            raise AssertionError(name)
        if self.malformed_live:
            return response(1)
        path = args["path"]
        receipts = []
        for operation in args["operations"]:
            kind = operation["type"]
            if kind in ("get", "getProperty"):
                prop = operation["property"]
                if prop == "has_clip":
                    result = int(path.rsplit(" ", 1)[-1]) in self.occupied
                elif prop == "name":
                    result = "SIDEKICK EARS"
                elif prop == "has_audio_input":
                    result = True
                elif prop == "file_path":
                    result = str(self.project)
                elif prop == "arm":
                    result = self.arm
                else:
                    raise AssertionError(prop)
            elif kind == "set":
                self.arm = bool(operation["value"])
                result = 1
            elif kind == "call":
                method = operation["method"]
                if method == "fire_scene_at_index":
                    self.scene_launches.append(operation["args"][0])
                elif method == "fire":
                    self.occupied.add(0)
                elif method == "delete_clip":
                    if not self.ignore_delete:
                        self.occupied.clear()
                elif method != "stop_all_clips":
                    raise AssertionError(method)
                result = 1
            else:
                raise AssertionError(kind)
            if kind == "get":
                result = [result]
            receipts.append({"operation": operation, "result": result})
        return response({"id": getattr(self, "root_id", "live_set"), "path": path, "results": receipts})

    def mutations(self):
        out = []
        for name, args in self.calls:
            if name == "ppal-playback":
                out.append((name, args))
            if name == "ppal-live-api" and any(
                    op["type"] in ("set", "call") for op in args["operations"]):
                out.append((name, args))
        return out


_SLOT_PATH = re.compile(r"^live_set tracks (\d+) clip_slots (\d+)$")


class BridgeTransport:
    """``playing`` is the transport (live_set is_playing); ``launched`` the
    clip slots that report is_playing / is_triggered after a scene launch —
    None: every slot of every track does; a set of 't<i>/s<j>': only those."""

    def __init__(self, project: Path, occupied=False, with_identity=True, playing=1,
                 launched=None):
        self.project = project
        self.occupied = occupied
        self.playing = playing
        self.launched = launched
        self.operations = []
        if with_identity:
            self.read_set_identity = lambda: pp_session.build_set_identity(str(self.project), "bridge-epoch")

    def _slot_value(self, path, prop):
        match = _SLOT_PATH.match(path)
        if match and prop in ("is_playing", "is_triggered"):
            slot = "t%s/s%s" % match.groups()
            return 1 if (self.launched is None or slot in self.launched) else 0
        if prop == "has_clip":
            return self.occupied
        if prop == "is_playing":
            return self.playing
        return str(self.project)

    def __call__(self, batches):
        operation = batches[0][0]
        self.operations.append(operation)
        if operation["op"] == "get":
            value = self._slot_value(str(operation.get("path")), operation["prop"])
            return [{"ok": True, "results": [{"value": value}]}]
        if operation["op"] == "call" and operation["method"] == "delete_clip":
            self.occupied = False
            return [{"ok": True, "results": [{"value": 1}]}]
        raise AssertionError(operation)


class WrappedBridgeTransport(BridgeTransport):
    """Match Max LiveAPI.get(), which wraps scalar properties in atom arrays."""

    def __call__(self, batches):
        reply = super().__call__(batches)
        if batches[0][0]["op"] == "get":
            result = reply[0]["results"][0]
            result["value"] = [result["value"]]
        return reply


class FakeBridgeClient:
    def __init__(self, sample_path: Path, fail=False):
        self.sample_path = sample_path
        self.fail = fail
        self.calls = []

    def start(self, capture_id, track, scene, duration):
        self.calls.append(("start", capture_id, track, scene, duration))
        return {"state": "recording", "capture_id": capture_id}

    def wait_ready(self, capture_id):
        self.calls.append(("wait", capture_id))
        if self.fail:
            raise TimeoutError("late")
        return {"state": "ready", "capture_id": capture_id}

    def finalize(self, capture_id):
        self.calls.append(("finalize", capture_id))
        return {"state": "finalized", "capture_id": capture_id,
                "path": "t1/s0", "cleanup": {"stopped": True, "arm_restored": True}}

    def cancel(self, capture_id):
        self.calls.append(("cancel", capture_id))
        return {"state": "cancelled", "capture_id": capture_id,
                "cleanup": {"stopped": True, "arm_restored": True,
                            "deleted": True}}


class FileCreatingTimeoutBridge(FakeBridgeClient):
    def __init__(self, sample_path: Path, unrelated_path: Path):
        super().__init__(sample_path, fail=True)
        self.unrelated_path = unrelated_path

    def start(self, capture_id, track, scene, duration):
        result = super().start(capture_id, track, scene, duration)
        self.sample_path.write_bytes(b"FORM")
        self.sample_path.with_name("SIDEKICK EARS 0002.aif").write_bytes(b"FORM")
        self.unrelated_path.write_bytes(b"FORM")
        return result


@on_both_versions
class TestCaptureLoop(unittest.TestCase):
    def setUp(self):
        pp_session.set_live_process_incarnation_resolver(
            lambda: {"pid": 1234, "start_time": 1000.0}
        )
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.project = root / "Scratch.als"
        self.project.write_text("set")
        self.recorded = root / "Samples" / "Recorded"
        self.recorded.mkdir(parents=True)
        self.sample = self.recorded / "capture.wav"
        self.sample.write_bytes(b"RIFF")
        self.registered = []
        self.listened = []

    def tearDown(self):
        pp_session.set_live_process_incarnation_resolver(None)
        self.tmp.cleanup()

    def operation(self, session):
        @asynccontextmanager
        async def factory():
            yield session
        return factory

    def grouped_operation(self, session):
        @asynccontextmanager
        async def factory():
            try:
                yield session
            except BaseException as exc:
                raise ExceptionGroup(
                    "transport teardown",
                    [ExceptionGroup("session teardown", [exc])],
                )
        return factory

    def registrar(self, **kwargs):
        self.registered.append(kwargs)
        return {"capture_id": "take-opaque", "inventory_id": "inv-secret"}

    def listener(self, inventory, take_ids):
        self.listened.append((inventory, take_ids, len(self.registered)))
        return [{"take_id": take_ids[0], "vibe": "tight"}]

    def kwargs(self, session, **extra):
        base = dict(
            operation_session=self.operation(session),
            audio_validator=lambda path, roots: {"duration_s": 1.0},
            registrar=self.registrar,
            listener=self.listener,
            sleep=lambda _: None,
        )
        base.update(extra)
        return base

    def test_preview_is_read_only_and_selects_capability_surface(self):
        for capable, expected in ((True, "live_api"), (False, "bridge")):
            with self.subTest(capable=capable):
                session = FakeSession(self.project, live_api=capable)
                bridge = BridgeTransport(self.project)
                result = capture_loop(bars=2, **self.kwargs(
                    session, bridge_transport=bridge))
                self.assertEqual(result["surface"], expected)
                self.assertFalse(result["executed"])
                self.assertEqual(session.mutations(), [])
                self.assertTrue(all(op["op"] == "get" for op in bridge.operations))
                self.assertEqual(self.registered, [])
                self.assertEqual(self.listened, [])

    def test_bridge_preview_accepts_liveapi_singleton_atom_arrays(self):
        session = FakeSession(self.project, live_api=False)
        bridge = WrappedBridgeTransport(self.project)

        result = capture_loop(surface="bridge", **self.kwargs(
            session, bridge_transport=bridge))

        self.assertEqual(result["status"], "preview")
        self.assertEqual(result["surface"], "bridge")
        self.assertEqual(session.mutations(), [])

    def test_unsaved_live_set_refuses_before_mutation_with_save_guidance(self):
        session = FakeSession(self.project, live_api=False)
        bridge = WrappedBridgeTransport("")

        with self.assertRaisesRegex(CaptureLoopError, "Save the Live Set before capturing"):
            capture_loop(surface="bridge", **self.kwargs(
                session, bridge_transport=bridge))

        self.assertEqual(session.mutations(), [])

    def test_transport_exception_group_preserves_capture_error(self):
        session = FakeSession(self.project, live_api=False)
        bridge = WrappedBridgeTransport("")

        with self.assertRaisesRegex(CaptureLoopError, "Save the Live Set before capturing"):
            capture_loop(
                surface="bridge",
                operation_session=self.grouped_operation(session),
                bridge_transport=bridge,
            )

        self.assertEqual(session.mutations(), [])

    def test_meter_timing_and_default_does_not_launch_scene(self):
        for signature, expected in (("3/4", 3.0), ("5/4", 5.0)):
            session = FakeSession(self.project, signature=signature)
            result = capture_loop(bars=2, **self.kwargs(session))
            self.assertEqual(result["duration_s"], expected)
            self.assertEqual(session.scene_launches, [])

    def test_explicit_live_api_capability_and_bad_scene_refuse(self):
        session = FakeSession(self.project, live_api=False)
        with self.assertRaises(CaptureLoopError):
            capture_loop(surface="live_api", **self.kwargs(session))
        self.assertEqual(session.mutations(), [])
        with self.assertRaises(CaptureLoopError):
            capture_loop(source_scene_index=3, **self.kwargs(session))
        self.assertEqual(session.mutations(), [])

    def test_target_missing_duplicate_non_audio_and_full_refuse(self):
        variants = (
            [],
            [{"name": "SIDEKICK EARS", "trackIndex": 1, "type": "audio"}] * 2,
            [{"name": "SIDEKICK EARS", "trackIndex": 1, "type": "midi"}],
        )
        for tracks in variants:
            session = FakeSession(self.project, tracks=tracks)
            with self.assertRaises(CaptureLoopError):
                capture_loop(**self.kwargs(session))
            self.assertEqual(session.mutations(), [])
        full = FakeSession(self.project, occupied={0, 1, 2})
        with self.assertRaises(CaptureLoopError):
            capture_loop(**self.kwargs(full))
        self.assertEqual(full.mutations(), [])

    def test_live_execution_readback_listener_and_source_immutability(self):
        session = FakeSession(self.project)
        result = capture_loop(confirm=True, listen=True, source_scene_index=2,
                              **self.kwargs(session))
        self.assertEqual(set(result), {"capture_id", "path", "listening_card",
                                      "surface", "cleanup_receipt"})
        self.assertEqual(result["capture_id"], "take-opaque")
        self.assertEqual(result["path"], "t1/s0")
        self.assertEqual(session.scene_launches, [2])
        self.assertFalse(session.arm)
        self.assertEqual(self.listened[0][2], 1)
        self.assertIsInstance(result["listening_card"].get("heard_at"), (int, float))
        public_set_id = session_ears._public_set_identity(
            self.registered[0]["set_identity"])["set_id"]
        self.assertEqual(self.registered[0]["session_id"], public_set_id)
        self.assertNotIn(".als", self.registered[0]["session_id"])
        self.assertNotIn(str(self.project), str(result["listening_card"]))
        mutation_paths = [args.get("path", "") for name, args in session.mutations()
                          if name == "ppal-live-api"]
        self.assertTrue(all(path == "live_set" or "tracks 1" in path
                            for path in mutation_paths))
        self.assertNotIn(str(self.project), str(result))
        self.assertNotIn(str(self.sample), str(result))

    def test_preconsume_listen_failure_keeps_one_registered_capability(self):
        session = FakeSession(self.project)

        def fails_before_claim(_inventory, _take_ids):
            raise RuntimeError("listener preflight failed")

        result = capture_loop(
            confirm=True, listen=True,
            **self.kwargs(session, listener=fails_before_claim))
        self.assertEqual(len(self.registered), 1)
        self.assertIn("listener preflight failed", result["listen_error"])
        self.assertIn("inventory_session_audio()", result["retry"])
        self.assertNotIn("inv-secret", str(result))

    def test_live_api_capture_uses_exact_meter_length_without_launch_quantization(self):
        session = FakeSession(self.project, signature="3/4", tempo=120)
        waits = []

        capture_loop(bars=2, confirm=True, surface="live_api", **self.kwargs(
            session, sleep=lambda seconds: waits.append(seconds)))

        fire = next(
            operation
            for name, args in session.calls if name == "ppal-live-api"
            for operation in args["operations"]
            if operation.get("method") == "fire"
        )
        self.assertEqual(fire.get("args"), [6.0, 0])
        self.assertEqual(waits, [3.25])

    def test_malformed_live_receipt_and_validation_failure_fail_closed(self):
        malformed = FakeSession(self.project, malformed_live=True)
        with self.assertRaises(Exception):
            capture_loop(**self.kwargs(malformed))
        self.assertEqual(malformed.mutations(), [])

        session = FakeSession(self.project)
        with self.assertRaisesRegex(CaptureLoopError, "local validation"):
            capture_loop(confirm=True, **self.kwargs(
                session, audio_validator=lambda *_: (_ for _ in ()).throw(ValueError("bad"))))
        self.assertFalse(session.arm)
        self.assertEqual(session.occupied, set())
        self.assertFalse(self.sample.exists())
        self.assertEqual(self.registered, [])
        self.assertEqual(self.listened, [])

    def test_bridge_success_and_timeout_cancel(self):
        session = FakeSession(self.project, live_api=False)
        bridge_transport = BridgeTransport(self.project)
        client = FakeBridgeClient(self.sample)
        result = capture_loop(confirm=True, surface="bridge", **self.kwargs(
            session, bridge_transport=bridge_transport,
            bridge_client_factory=lambda _: client))
        self.assertEqual(result["surface"], "bridge")
        self.assertEqual([call[0] for call in client.calls], ["start", "wait", "finalize"])

        failing = FakeBridgeClient(self.sample, fail=True)
        with self.assertRaisesRegex(CaptureLoopError, "cleanup was attempted"):
            capture_loop(confirm=True, surface="bridge", **self.kwargs(
                session, bridge_transport=bridge_transport,
                bridge_client_factory=lambda _: failing))
        self.assertEqual([call[0] for call in failing.calls], ["start", "wait", "cancel"])

    def test_bridge_plays_the_source_scene_with_the_builds_form(self):
        session = FakeSession(self.project, live_api=False)
        client = FakeBridgeClient(self.sample)
        result = capture_loop(confirm=True, surface="bridge", source_scene_index=2,
                              **self.kwargs(session, bridge_transport=BridgeTransport(self.project),
                                            bridge_client_factory=lambda _: client))
        self.assertEqual(result["surface"], "bridge")
        self.assertEqual(session.scene_launches, [2])
        sent = [args for name, args in session.wire_calls if name == "ppal-playback"]
        # 2.3 removed playback sceneIndex; both schemas take play-scene path 's<i>'
        self.assertEqual(sent, [{"action": "play-scene", "path": "s2"}]
                         if self.PP_VERSION in ("2.3.0", "2.4.0")
                         else [{"action": "play-scene", "sceneIndex": 2}])

    def test_bridge_source_scene_is_verified_by_reading_the_transport(self):
        # review M1: the play-scene reply is not a verdict (words or none); the
        # transport read after it is. Advisory warnings next to it are fine.
        session = FakeSession(self.project, live_api=False)
        session.device_warnings = list(pp_fakes.DEVICE_ADVISORY_WARNINGS[self.PP_VERSION])
        transport = BridgeTransport(self.project)
        result = capture_loop(confirm=True, surface="bridge", source_scene_index=2,
                              **self.kwargs(session, bridge_transport=transport,
                                            bridge_client_factory=lambda _: FakeBridgeClient(
                                                self.sample)))
        self.assertEqual(result["surface"], "bridge")
        self.assertIn({"op": "get", "path": "live_set", "prop": "is_playing"},
                      transport.operations)

        # the captured 2.3 reply names the scene it played (v23b_play_scene_by_path)
        session.playback_reply = {"playing": True,
                                  "scene": {"id": "364", "name": "B", "path": "s2"}}
        self.assertEqual(capture_loop(
            confirm=True, surface="bridge", source_scene_index=2,
            **self.kwargs(session, bridge_transport=BridgeTransport(self.project),
                          bridge_client_factory=lambda _: FakeBridgeClient(self.sample)))[
            "surface"], "bridge")
        for reply, message in (({"playing": True, "scene": {"path": "s3"}}, "played s3"),
                               ({"playing": False}, "did not start playing")):
            with self.subTest(reply=reply):
                session.playback_reply = reply
                client = FakeBridgeClient(self.sample)
                with self.assertRaisesRegex(CaptureLoopError, message):
                    capture_loop(confirm=True, surface="bridge", source_scene_index=2,
                                 **self.kwargs(session, bridge_transport=BridgeTransport(
                                     self.project), bridge_client_factory=lambda _: client))
                self.assertEqual(client.calls, [])
        session.playback_reply = None   # the build's own reply again

        stopped = BridgeTransport(self.project, playing=0)
        client = FakeBridgeClient(self.sample)
        with self.assertRaisesRegex(CaptureLoopError, "source scene 2 did not start playing"):
            capture_loop(confirm=True, surface="bridge", source_scene_index=2,
                         **self.kwargs(session, bridge_transport=stopped,
                                       bridge_client_factory=lambda _: client))
        self.assertEqual(client.calls, [])

    # Review 2026-09-30 finding 5 (scene_not_proven probe): {"playing": true}
    # with no scene, while the transport already plays, identifies no scene.
    # 2.3: the captured reply names the scene it played
    # (v23b_play_scene_by_path) — a reply without one refuses the capture.
    # 2.2 (no captured reply): fresh evidence from the Set — a clip slot of the
    # source scene playing or triggered — or the capture refuses.
    def _unproven_scene(self, transport):
        session = FakeSession(self.project, live_api=False)
        session.playback_reply = {"playing": True}
        client = FakeBridgeClient(self.sample)
        with self.assertRaises(CaptureLoopError) as ctx:
            capture_loop(confirm=True, surface="bridge", source_scene_index=2,
                         **self.kwargs(session, bridge_transport=transport,
                                       bridge_client_factory=lambda _: client))
        self.assertEqual(client.calls, [])          # never start/wait/finalize
        self.assertEqual(session.scene_launches, [2])
        self.assertEqual(self.registered, [])
        return session, str(ctx.exception)

    def test_play_scene_reply_without_a_scene_never_reaches_the_bridge(self):
        # the probe: transport already playing, nothing in scene 2 launched
        transport = BridgeTransport(self.project, playing=1, launched=set())
        session, message = self._unproven_scene(transport)
        self.assertIn("source scene 2", message)
        if self.PP_VERSION in ("2.3.0", "2.4.0"):
            self.assertIn("named no scene", message)
        else:
            self.assertIn("no clip slot of source scene 2", message)
        # playback was already running before this capture: left alone
        self.assertEqual(session.stops, 0)
        self.assertIn("already running", message)

    def test_a_refused_scene_proof_stops_the_playback_this_capture_started(self):
        transport = BridgeTransport(self.project, playing=0, launched=set())
        session, message = self._unproven_scene(transport)
        self.assertEqual(session.stops, 1)
        self.assertEqual([a for n, a in session.wire_calls if n == "ppal-playback"][-1],
                         {"action": "stop"})
        self.assertIn("stopped the playback this capture started", message)

    def test_on_22_a_launched_clip_slot_of_the_source_scene_is_the_proof(self):
        session = FakeSession(self.project, live_api=False)
        # scene 2 launched on the user's source track (t0), not on EARS (t1)
        transport = BridgeTransport(self.project, launched={"t0/s2"})
        result = capture_loop(confirm=True, surface="bridge", source_scene_index=2,
                              **self.kwargs(session, bridge_transport=transport,
                                            bridge_client_factory=lambda _: FakeBridgeClient(
                                                self.sample)))
        self.assertEqual(result["surface"], "bridge")
        slot_reads = [op for op in transport.operations
                      if op["op"] == "get" and op["prop"] in ("is_playing", "is_triggered")
                      and str(op["path"]).startswith("live_set tracks")]
        if self.PP_VERSION in ("2.3.0", "2.4.0"):
            self.assertEqual(slot_reads, [])     # the receipt is the proof
        else:
            self.assertEqual(slot_reads[0]["path"], "live_set tracks 0 clip_slots 2")
            self.assertTrue(all("tracks 1 " not in op["path"] for op in slot_reads))
            # a scene whose only launched clip sits on another scene proves nothing
            elsewhere = BridgeTransport(self.project, launched={"t0/s1"})
            client = FakeBridgeClient(self.sample)
            with self.assertRaisesRegex(CaptureLoopError, "no clip slot of source scene 2"):
                capture_loop(confirm=True, surface="bridge", source_scene_index=2,
                             **self.kwargs(session, bridge_transport=elsewhere,
                                           bridge_client_factory=lambda _: client))
            self.assertEqual(client.calls, [])

    def test_bridge_timeout_removes_only_new_sidekick_ears_recording(self):
        session = FakeSession(self.project, live_api=False)
        bridge_transport = BridgeTransport(self.project)
        failed_take = self.recorded / "SIDEKICK EARS 0001.aif"
        unrelated = self.recorded / "Producer Take.aif"
        client = FileCreatingTimeoutBridge(failed_take, unrelated)

        with self.assertRaisesRegex(CaptureLoopError, "cleanup was attempted"):
            capture_loop(confirm=True, surface="bridge", **self.kwargs(
                session, bridge_transport=bridge_transport,
                bridge_client_factory=lambda _: client))

        self.assertFalse(failed_take.exists())
        self.assertFalse((self.recorded / "SIDEKICK EARS 0002.aif").exists())
        self.assertTrue(unrelated.exists())

    def test_registration_failure_deletes_partial_and_unverified_delete_is_explicit(self):
        session = FakeSession(self.project)
        with self.assertRaisesRegex(CaptureLoopError, "post-processing failed"):
            capture_loop(confirm=True, **self.kwargs(
                session, registrar=lambda **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("registry unavailable"))))
        self.assertEqual(session.occupied, set())
        self.assertFalse(self.sample.exists())
        delete_calls = [args for name, args in session.calls
                        if name == "ppal-live-api" and any(
                            op.get("method") == "delete_clip" for op in args["operations"])]
        self.assertTrue(delete_calls)

        stuck = FakeSession(self.project, ignore_delete=True)
        with self.assertRaisesRegex(
                CaptureLoopError, "cleanup could not be verified"):
            capture_loop(confirm=True, **self.kwargs(
                stuck, registrar=lambda **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("registry unavailable"))))
        self.assertEqual(stuck.occupied, {0})

    def test_profile_membership(self):
        self.assertIn("capture_loop", profiles.get_profile_tools("all"))
        self.assertIn("capture_loop", profiles.get_profile_tools("compose"))
        self.assertIn("capture_loop", profiles.get_profile_tools("session"))
        self.assertNotIn("capture_loop", profiles.get_profile_tools("reference"))
        self.assertNotIn("capture_loop", profiles.get_profile_tools("debug"))

        signature = inspect.signature(server.capture_loop)
        self.assertEqual(list(signature.parameters), [
            "bars", "source_scene_index", "surface", "listen", "confirm"])
        self.assertEqual(signature.parameters["surface"].default, "auto")

    def test_validated_capture_registration_returns_only_opaque_ids(self):
        registered = session_ears.register_validated_capture(
            session_id="Scratch", track_index=1, scene_index=0,
            clip_name="Capture", path=str(self.sample), duration_s=1.0, bars=2)
        self.assertEqual(set(registered), {"capture_id", "inventory_id"})
        self.assertNotIn(str(self.sample), str(registered))

    def test_bridge_capture_without_verification_transport_fails_closed(self):
        session = FakeSession(self.project, live_api=False)
        bridge_transport = BridgeTransport(self.project, with_identity=False)
        client = FakeBridgeClient(self.sample)
        with self.assertRaisesRegex(
            CaptureLoopError,
            "Verified Set identity is unsupported for bridge capture without verification transport",
        ):
            capture_loop(
                confirm=True, surface="bridge",
                **self.kwargs(
                    session,
                    bridge_transport=bridge_transport,
                    bridge_client_factory=lambda _: client,
                ),
            )
        self.assertEqual(client.calls, [])

    def test_capture_aborts_without_mutating_new_set_when_identity_changes_after_recording(self):
        session = FakeSession(self.project)
        identities = [
            {"set_id": "Scratch", "runtime_epoch": "epoch-1"},
            {"set_id": "DifferentSet", "runtime_epoch": "epoch-2"},
        ]

        def custom_read_identity(_=None):
            return identities.pop(0)

        with self.assertRaisesRegex(
            CaptureLoopError, "Live Set identity changed during capture recording"
        ):
            capture_loop(
                confirm=True,
                **self.kwargs(
                    session,
                    _read_set_identity=custom_read_identity,
                ),
            )
        self.assertEqual(session.occupied, {0})
        self.assertTrue(self.sample.exists())
        self.assertEqual(self.registered, [])

    def test_capture_skips_post_switch_cleanup_when_set_changes_during_sleep(self):
        session = FakeSession(self.project)
        phase = ["recording"]
        identity_before = {
            "set_id": str(self.project),
            "runtime_epoch": "epoch-before",
        }
        identity_after = {
            "set_id": "Other.als",
            "runtime_epoch": "epoch-after",
        }

        def custom_read_identity(_=None):
            if phase[0] == "recording":
                return identity_before
            return identity_after

        def sleep_fn(_seconds):
            phase[0] = "after"

        with self.assertRaisesRegex(
            CaptureLoopError, "Live Set identity changed during capture recording"
        ):
            capture_loop(
                confirm=True,
                **self.kwargs(
                    session,
                    _read_set_identity=custom_read_identity,
                    sleep=sleep_fn,
                ),
            )

        stop_calls = [
            args for name, args in session.calls
            if name == "ppal-live-api" and any(
                op.get("method") == "stop_all_clips" for op in args["operations"])
        ]
        delete_calls = [
            args for name, args in session.calls
            if name == "ppal-live-api" and any(
                op.get("method") == "delete_clip" for op in args["operations"])
        ]
        self.assertEqual(stop_calls, [])
        self.assertEqual(delete_calls, [])
        self.assertEqual(session.occupied, {0})
        self.assertTrue(self.sample.exists())

    def test_bridge_default_path_uses_live_write_identity_with_live_api_capability(self):
        session = FakeSession(self.project, live_api=True)
        session.root_id = "live-root-bridge"
        bridge_transport = BridgeTransport(self.project, with_identity=False)
        client = FakeBridgeClient(self.sample)
        result = capture_loop(
            confirm=True,
            surface="bridge",
            **self.kwargs(
                session,
                bridge_transport=bridge_transport,
                bridge_client_factory=lambda _: client,
            ),
        )
        expected = pp_session.build_set_identity(
            str(self.project),
            pp_session.build_runtime_epoch("live-root-bridge", {"pid": 1234, "start_time": 1000.0}),
        )
        expected["live_root_id"] = "live-root-bridge"
        self.assertEqual(result["surface"], "bridge")
        self.assertEqual(len(self.registered), 1)
        self.assertEqual(self.registered[0]["set_identity"], expected)

    def test_bridge_set_switch_prevents_finalize_and_cancel(self):
        session = FakeSession(self.project, live_api=True)
        client = FakeBridgeClient(self.sample)
        from unittest.mock import Mock
        def switched(_capture_id):
            session.root_id = "different-root"
        client.wait_ready = switched
        client.finalize = Mock()
        client.cancel = Mock()
        with self.assertRaisesRegex(CaptureLoopError, "identity changed"):
            capture_loop(confirm=True, surface="bridge", **self.kwargs(
                session, bridge_transport=BridgeTransport(self.project, with_identity=False),
                bridge_client_factory=lambda _: client))
        client.finalize.assert_not_called()
        client.cancel.assert_not_called()
        self.assertTrue(self.sample.exists())

    def test_registrar_internal_typeerror_is_not_retried(self):
        session = FakeSession(self.project)
        calls = []

        def flaky_registrar(**kwargs):
            calls.append(dict(kwargs))
            raise TypeError("internal registrar failure")

        with self.assertRaisesRegex(CaptureLoopError, "post-processing failed"):
            capture_loop(
                confirm=True,
                **self.kwargs(session, registrar=flaky_registrar),
            )
        self.assertEqual(len(calls), 1)
        self.assertIn("set_identity", calls[0])
        self.assertEqual(session.occupied, set())
        self.assertFalse(self.sample.exists())

    def test_listener_receives_verified_identity_not_live_session(self):
        session = FakeSession(self.project)
        listener_calls = []

        def tracking_listener(inventory_id, take_ids, **kwargs):
            listener_calls.append(dict(kwargs))
            return [{"take_id": take_ids[0], "vibe": "tight"}]

        capture_loop(
            confirm=True,
            listen=True,
            **self.kwargs(session, listener=tracking_listener),
        )
        self.assertEqual(len(listener_calls), 1)
        # The listener gets the already verified identity; no live session is
        # loaned out for a second identity read.
        self.assertNotIn("_pp_operation_session", listener_calls[0])
        reader = listener_calls[0]["_read_set_identity"]
        self.assertTrue(callable(reader))
        self.assertEqual(reader(), self.registered[0]["set_identity"])

    def test_capture_registration_receives_verified_set_identity(self):
        session = FakeSession(self.project)
        custom_ident = {"set_id": "CustomSet", "runtime_epoch": "epoch-custom"}
        result = capture_loop(
            confirm=True,
            **self.kwargs(
                session,
                _read_set_identity=lambda _=None: custom_ident,
            ),
        )
        self.assertEqual(result["surface"], "live_api")
        self.assertEqual(len(self.registered), 1)
        self.assertEqual(self.registered[0]["set_identity"], custom_ident)


@on_both_versions
class TestCaptureListenFailureKeepsRecording(unittest.TestCase):
    """Item 5: listening runs after the session block with the verified
    identity; a failed listen returns capture_id + listen_error and the
    validated recording (clip + file) is never deleted. Uses the real
    session_ears listener with a stub runner."""

    def setUp(self):
        pp_session.set_live_process_incarnation_resolver(
            lambda: {"pid": 1234, "start_time": 1000.0}
        )
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.project = root / "Scratch.als"
        self.project.write_text("set")
        self.recorded = root / "Samples" / "Recorded"
        self.recorded.mkdir(parents=True)
        self.sample = self.recorded / "capture.wav"
        self.sample.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
        self.store = root / "store"
        self.identity = {"set_id": "ItemFiveSet", "runtime_epoch": "epoch-5"}

    def tearDown(self):
        pp_session.set_live_process_incarnation_resolver(None)
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def operation(self, session):
        @asynccontextmanager
        async def factory():
            yield session
        return factory

    def echo_runner(self, cmd, **kwargs):
        prompt = cmd[-1]
        take_id = re.search(r"- take_id: ([^\n]+)", prompt).group(1)
        track = re.search(r"- track: ([^\n]+)", prompt).group(1)
        clip = re.search(r"- clip_name: ([^\n]+)", prompt).group(1)
        card = {
            "take_id": take_id, "track": track, "clip_name": clip,
            "role_guess": "lead", "vibe": "close", "register": None,
            "density": "sparse", "pitch_claims": [], "timestamps": ["00:00"],
            "contract_version": session_ears.CONTRACT_VERSION,
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(card), stderr="")

    def failing_runner(self, cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="listener boom")

    def kwargs(self, session):
        return dict(
            operation_session=self.operation(session),
            audio_validator=lambda path, roots: {"duration_s": 1.0},
            registrar=session_ears.register_validated_capture,
            listener=session_ears.listen_to_takes,
            sleep=lambda _: None,
            _read_set_identity=lambda: dict(self.identity),
        )

    def envelope_runner(self, convs, prove=True):
        """Real-shaped agy --output-format json turn: card in the envelope's
        result text; proof-of-listening record planted unless prove=False."""
        from tests.test_agy_listen_verification import (
            _staged_path_from_prompt, plant_listen_evidence)

        def run(cmd, **kwargs):
            if "--print" not in cmd:  # e.g. the agy flag probe — not a listen
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            prompt = cmd[cmd.index("--print") + 1]
            card = json.loads(self.echo_runner(cmd, **kwargs).stdout)
            if prove:
                plant_listen_evidence(convs, "conv-capture",
                                      _staged_path_from_prompt(prompt))
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
                "conversation_id": "conv-capture" if prove else None,
                "result": json.dumps(card),
                "usage": {"input_tokens": 900}}))
        return run

    def test_capture_listen_with_proof_returns_verified_card(self):
        # End to end through the real listener: capture → listen → proof.
        session = FakeSession(self.project)
        convs = Path(self.tmp.name) / "convs"
        convs.mkdir()
        with passthrough_listenable_staging(), \
             mock.patch.dict("os.environ", {"AGY_CONVERSATIONS_DIR": str(convs)}), \
             mock.patch.object(session_ears, "_which", return_value="/fake/agy"), \
             mock.patch.object(session_ears, "STORE_ROOT", self.store), \
             mock.patch.object(session_ears.subprocess, "run",
                               self.envelope_runner(convs)):
            result = capture_loop(confirm=True, listen=True, **self.kwargs(session))
        self.assertNotIn("listen_error", result)
        self.assertIsNotNone(result["listening_card"])
        self.assertIs(result["listening_card"]["listen_verified"], True)
        self.assertTrue(self.sample.is_file())

    def test_capture_listen_without_conversation_record_fails_closed(self):
        # No conversation id / no record: the take is rejected (unverifiable),
        # no card is produced, and the validated recording is kept.
        session = FakeSession(self.project)
        convs = Path(self.tmp.name) / "convs"
        convs.mkdir()
        with passthrough_listenable_staging(), \
             mock.patch.dict("os.environ", {"AGY_CONVERSATIONS_DIR": str(convs)}), \
             mock.patch.object(session_ears, "_which", return_value="/fake/agy"), \
             mock.patch.object(session_ears, "STORE_ROOT", self.store), \
             mock.patch.object(session_ears.subprocess, "run",
                               self.envelope_runner(convs, prove=False)):
            result = capture_loop(confirm=True, listen=True, **self.kwargs(session))
        self.assertIsNone(result["listening_card"])
        self.assertIn("listen rejected", result["listen_error"])
        self.assertIn("unverifiable", result["listen_error"])
        self.assertTrue(self.sample.is_file())

    def test_failed_listen_returns_capture_id_and_listen_error(self):
        session = FakeSession(self.project)
        with passthrough_listenable_staging(), \
             mock.patch.object(session_ears, "_which", return_value="/fake/agy"), \
             mock.patch.object(session_ears, "STORE_ROOT", self.store), \
             mock.patch.object(session_ears.subprocess, "run", self.failing_runner):
            result = capture_loop(confirm=True, listen=True, **self.kwargs(session))
        self.assertEqual(
            set(result),
            {"capture_id", "path", "listening_card", "surface",
             "cleanup_receipt", "listen_error", "retry"})
        self.assertIsNone(result["listening_card"])
        self.assertIn("returncode 1", result["listen_error"])
        self.assertIn("inventory_session_audio()", result["retry"])
        self.assertIn("listen_to_takes()", result["retry"])
        self.assertNotIn("inventory_id", str(result))
        # The validated recording survives: clip slot still occupied, file kept.
        self.assertEqual(session.occupied, {0})
        self.assertTrue(self.sample.exists())



if __name__ == "__main__":
    unittest.main()
