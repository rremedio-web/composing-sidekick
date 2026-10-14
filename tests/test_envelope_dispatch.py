"""Lane B3 item 6: groove/envelope/probe entry points work on a running loop.

FastMCP runs sync tools directly on its event loop, so these entry points
reach their coroutines through pp_session.run_coro_blocking (item 1). Each
test calls the SYNC entry point from inside a running event loop with a fake
transport (patched send_cmds + stub MCP client at the Producer Pal
boundary): on the pre-item-1 bridge they fail with "asyncio.run() cannot be
called from a running event loop". No sockets, no Live contact.
"""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import envelope_bridge as eb
from tests.pp_fakes import schema_strict, on_both_versions
from tests._requires import requires_catalog


class _FakeStreamableClient:
    """Stub for mcp.client.streamable_http.streamablehttp_client."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return (object(), object())

    async def __aexit__(self, *args):
        return False


class _FakeTool:
    def __init__(self, name):
        self.name = name


class _FakeTools:
    def __init__(self, names):
        self.tools = [_FakeTool(name) for name in names]


@schema_strict
class _FakeClientSession:
    """Stub for mcp.ClientSession."""

    compact = False
    compact_payload = '{connected:true,producerPalVersion:"2.2.0-rc1"}'

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def initialize(self):
        return None

    async def list_tools(self):
        return _FakeTools(["ppal-connect", "ppal-read-clip", "ppal-live-api"])

    async def call_tool(self, name, arguments):
        class Result:
            isError = False
        payload = (self.compact_payload
                   if self.compact else
                   '{"result":{"producerPalVersion":"2.2.0-rc1"}}')
        Result.content = [type("Text", (), {"text": payload})()]
        return Result()


async def _fake_pp_call(session, name, args=None):
    if name == "ppal-live-api":
        return "tempo 120.0 (major_version 12)"
    return {"ok": True}


def _transport_patches():
    return (
        patch("mcp.client.streamable_http.streamablehttp_client",
              _FakeStreamableClient),
        patch("mcp.ClientSession", _FakeClientSession),
        patch.object(eb.live_write, "_call", side_effect=_fake_pp_call),
        patch.object(eb.live_write, "_find_str", return_value="clip-9"),
    )


@schema_strict
class _CapturedClipSession(_FakeClientSession):
    """Replays the captured 2.2.0-rc1 device responses: connect, then read-clip
    by path (tests/fixtures/pp/2.2.0-rc1/legacy/legacy_read_clip_path.json)."""

    async def call_tool(self, name, arguments):
        base = Path(__file__).parent / "fixtures/pp/2.2.0-rc1"
        fixture = {"ppal-connect": base / "read/connect.json",
                   "ppal-read-clip": base / "legacy/legacy_read_clip_path.json"}[name]
        body = json.loads(fixture.read_text())["response"]["content"][0]["json"]

        class Result:
            isError = False
        Result.content = [type("Text", (), {"text": json.dumps(body)})()]
        return Result()


@on_both_versions
class TestCapturedClipLookup(unittest.IsolatedAsyncioTestCase):
    async def test_clip_id_comes_from_the_real_read_clip_response(self):
        # No _find_str mock: the real 2.2.0-rc1 read-clip body must yield its id.
        with patch("mcp.client.streamable_http.streamablehttp_client", _FakeStreamableClient), \
                patch("mcp.ClientSession", _CapturedClipSession):
            self.assertEqual(eb._clip_id_for_slot("9/0"), "359")

    async def test_missing_clip_never_sends_a_null_clip_id(self):
        sent = []
        with patch.object(eb, "_clip_id_for_slot", return_value=None), \
                patch.object(eb, "send_cmds", side_effect=lambda b: sent.append(b) or [{"ok": True}]):
            self.assertFalse(eb.clear_clip_envelopes("3/0")["cleared"])
            with patch.object(eb, "list_grooves", return_value={"grooves": [
                    {"name": "RICARDO FEEL LEAD 1BAR", "id": "g"}]}):
                self.assertFalse(eb.assign_clip_groove("3/0", role="lead", dry_run=False)["executed"])
        self.assertEqual(sent, [])


@on_both_versions
@requires_catalog
class TestRunningLoopDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_probe_uses_guarded_session_and_schema_shaped_live_api_requests(self):
        from contextlib import asynccontextmanager
        from mcp_server import pp_session

        calls = []

        @schema_strict
        class Session:
            async def list_tools(self):
                return _FakeTools(["ppal-live-api"])

            async def call_tool(self, name, arguments):
                calls.append((name, arguments))
                return type("Result", (), {
                    "isError": False,
                    "content": [type("Text", (), {"text":
                        '{"result":{"major_version":12}}'})()],
                })()

        class GuardedSession:
            _session = Session()

        @asynccontextmanager
        async def guarded_session(url):
            self.assertEqual(url, eb.live_write.PP_URL)
            yield GuardedSession()

        expected = [
            pp_session.build_live_api_request("live_set", [{
                "type": "get", "property": "tempo"}]),
            pp_session.build_live_api_request("live_app", [{
                "type": "call", "method": "get_major_version"}]),
        ]
        with patch.object(pp_session, "operation_session", guarded_session):
            result = eb.probe(confirm=True)

        self.assertTrue(result["steps"]["P1_tool_present"])
        self.assertEqual(result["verdict"], "ppal_live_api_full")
        self.assertEqual(calls, [("ppal-live-api", expected[0]),
                                 ("ppal-live-api", expected[1])])

    async def test_probe_propagates_compact_and_unsupported_handshake_refusals(self):
        from contextlib import asynccontextmanager
        from mcp_server import pp_session

        for message in ("switch Producer Pal to JSON output",
                        "not verified by captured fixtures yet"):
            @asynccontextmanager
            async def refused(url):
                raise RuntimeError(message)
                yield  # pragma: no cover

            with self.subTest(message=message), \
                    patch.object(pp_session, "operation_session", refused), \
                    self.assertRaisesRegex(RuntimeError, message):
                eb.probe(confirm=True)

    async def test_envelope_clip_lookup_refuses_compact_handshake(self):
        # Captured device shape from tests/fixtures/pp/2.2.0-rc1-compact/read/connect.json.
        fixture = (Path(__file__).parent / "fixtures/pp/2.2.0-rc1-compact/read/connect.json")
        _FakeClientSession.compact_payload = json.loads(fixture.read_text())[
            "response"]["content"][0]["text"]
        _FakeClientSession.compact = True
        self.addCleanup(setattr, _FakeClientSession, "compact", False)
        self.addCleanup(setattr, _FakeClientSession, "compact_payload",
                        '{connected:true,producerPalVersion:"2.2.0-rc1"}')
        patches = _transport_patches()
        for entered in patches:
            entered.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        with self.assertRaisesRegex(RuntimeError, "switch Producer Pal to JSON output"):
            eb._clip_id_for_slot("1/0")

    async def test_assign_groove_executes_on_running_loop(self):
        calls = []

        def fake_send(op_batches):
            calls.append(op_batches)
            ops = [op.get("op") for batch in op_batches for op in batch]
            if "list_grooves" in ops:
                return [{"ok": True, "results": [{"grooves": [
                    {"name": "Other Groove", "id": "groove-1"},
                    {"name": "RICARDO FEEL LEAD 1BAR", "id": "groove-7"},
                ]}]}]
            return [{"ok": True, "results": [{"ok": True}]}]

        patches = _transport_patches() + (
            patch.object(eb, "send_cmds", side_effect=fake_send),)
        for entered in patches:
            entered.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        # SYNC call on the running loop: the bridge must offload, not asyncio.run.
        result = eb.assign_clip_groove("1/0", role="lead", dry_run=False)
        self.assertTrue(result["executed"])
        self.assertEqual(result["groove"]["id"], "groove-7")
        self.assertEqual(result["undo"]["op"], "clear_clip_groove")

    async def test_write_clip_envelopes_executes_on_running_loop(self):
        def fake_send(op_batches):
            ops = [op.get("op") for batch in op_batches for op in batch]
            if "list_params" in ops:
                return [{"ok": True, "results": [
                    {"ok": True}, {"id": "vol-1"}, {"id": "send-1"},
                    {"devices": []},
                ]}]
            self.assertIn("create_envelope", ops)
            return [{"ok": True, "results": [{"ok": True}]}]

        patches = _transport_patches() + (
            patch.object(eb, "send_cmds", side_effect=fake_send),)
        for entered in patches:
            entered.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        result = eb.write_clip_envelopes("1/0", None, bars=2, seed=0,
                                         dry_run=False)
        self.assertTrue(result["executed"])
        self.assertEqual(result["jam_slot"], "1/0")
        written = {entry["family"] for entry in result["written"]}
        self.assertIn("volume", written)

    async def test_probe_confirm_runs_read_steps_on_running_loop(self):
        patches = _transport_patches()
        for entered in patches:
            entered.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        result = eb.probe(confirm=True)
        self.assertTrue(result["executed"])
        self.assertTrue(result["steps"]["P1_tool_present"])
        self.assertEqual(result["verdict"], "ppal_live_api_full")


if __name__ == "__main__":
    unittest.main()
