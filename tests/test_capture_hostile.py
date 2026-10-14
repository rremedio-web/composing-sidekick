from __future__ import annotations

import unittest

from mcp_server.capture_loop import CaptureLoopError, capture_loop
from tests.pp_fakes import schema_strict, on_both_versions
from tests.test_capture_loop import (
    BridgeTransport,
    FakeBridgeClient,
    FakeSession,
    TestCaptureLoop,
    response,
)


@schema_strict
class WrongPathSession(FakeSession):
    async def call(self, name, args):
        result = await super().call(name, args)
        if name == "ppal-live-api" and isinstance(result.result, dict):
            result.result["path"] = "live_set tracks 999"
        return result


@schema_strict
class FailedRestoreSession(FakeSession):
    async def call(self, name, args):
        restoring = (name == "ppal-live-api" and any(
            op.get("type") == "set" and op.get("property") == "arm"
            and op.get("value") is False for op in args.get("operations", [])))
        result = await super().call(name, args)
        if restoring:
            self.arm = True
        return result


@schema_strict
class MalformedPlaybackSession(FakeSession):
    async def call(self, name, args):
        if name == "ppal-playback":
            self.calls.append((name, args))
            return response(1)
        return await super().call(name, args)


class BadCleanupBridge(FakeBridgeClient):
    def finalize(self, capture_id):
        self.calls.append(("finalize", capture_id))
        return {"state": "finalized", "capture_id": capture_id,
                "path": "t1/s0", "cleanup": {
                    "stopped": False, "arm_restored": True}}


@on_both_versions
class TestHostileCapture(unittest.TestCase):
    setUp = TestCaptureLoop.setUp
    tearDown = TestCaptureLoop.tearDown
    operation = TestCaptureLoop.operation
    registrar = TestCaptureLoop.registrar
    listener = TestCaptureLoop.listener
    kwargs = TestCaptureLoop.kwargs

    def test_wrong_live_api_response_path_refuses_before_mutation(self):
        session = WrongPathSession(self.project)
        with self.assertRaisesRegex(CaptureLoopError, "path mismatch"):
            capture_loop(**self.kwargs(session))
        self.assertEqual(session.mutations(), [])

    def test_failed_live_arm_restore_deletes_capture_and_refuses_success(self):
        session = FailedRestoreSession(self.project)
        with self.assertRaisesRegex(CaptureLoopError, "cleanup verification"):
            capture_loop(confirm=True, **self.kwargs(session))
        self.assertEqual(session.occupied, set())
        self.assertEqual(self.registered, [])
        self.assertEqual(self.listened, [])

    def test_bridge_finalize_without_cleanup_proof_is_deleted(self):
        session = FakeSession(self.project, live_api=False)
        transport = BridgeTransport(self.project)
        client = BadCleanupBridge(self.sample)
        with self.assertRaisesRegex(CaptureLoopError, "cleanup verification"):
            capture_loop(confirm=True, surface="bridge", **self.kwargs(
                session, bridge_transport=transport,
                bridge_client_factory=lambda _: client))
        self.assertTrue(any(op.get("method") == "delete_clip"
                            for op in transport.operations))
        self.assertEqual(self.registered, [])

    def test_malformed_explicit_scene_launch_never_starts_bridge(self):
        session = MalformedPlaybackSession(self.project, live_api=False)
        transport = BridgeTransport(self.project)
        client = FakeBridgeClient(self.sample)
        with self.assertRaisesRegex(CaptureLoopError, "malformed capture state"):
            capture_loop(confirm=True, surface="bridge", source_scene_index=1,
                         **self.kwargs(session, bridge_transport=transport,
                                       bridge_client_factory=lambda _: client))
        self.assertEqual(client.calls, [])

    def test_hostile_registrar_and_listener_cannot_leak_paths_or_tokens(self):
        secret = str(self.sample)
        session = FakeSession(self.project)
        with self.assertRaisesRegex(CaptureLoopError, "private state") as registrar_error:
            capture_loop(confirm=True, **self.kwargs(
                session, registrar=lambda **_: {
                    "capture_id": secret, "inventory_id": "inv-secret"}))
        self.assertNotIn(secret, str(registrar_error.exception))

        session = FakeSession(self.project)
        with self.assertRaisesRegex(CaptureLoopError, "private state") as listener_error:
            capture_loop(confirm=True, listen=True, **self.kwargs(
                session, listener=lambda *_: [{"file_path": secret}]))
        self.assertNotIn(secret, str(listener_error.exception))


if __name__ == "__main__":
    unittest.main()
