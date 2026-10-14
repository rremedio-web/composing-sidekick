import json
import unittest
from pathlib import Path

from tests.pp_fakes import SchemaStrictSession


class SchemaStrictFakeTests(unittest.TestCase):
    def test_rejects_argument_not_in_captured_tool_schema(self):
        session = SchemaStrictSession()
        with self.assertRaisesRegex(AssertionError, "unknown arguments.*slot"):
            session.call_tool_sync("ppal-read-clip", {"slot": "t1/s0"})

    def test_unknown_tool_fails_like_device_and_captured_response_replays(self):
        session = SchemaStrictSession()
        with self.assertRaisesRegex(RuntimeError, "Unknown tool"):
            session.call_tool_sync("ppal-not-a-tool", {})

        # Response record shape captured from tests/fixtures/pp/2.2.0-rc1/legacy/legacy_read_clip_path.json.
        root = Path(__file__).parent / "fixtures/pp/2.2.0-rc1/read"
        fixture = json.loads((root.parent / "legacy/legacy_read_clip_path.json").read_text())
        session.responses[fixture["tool"]] = fixture["response"]
        result = session.call_tool_sync(fixture["tool"], fixture["arguments"])
        self.assertEqual(result, fixture["response"])

    def test_decorated_fake_rejects_legacy_locators_before_it_runs(self):
        from tests.pp_fakes import schema_strict
        import asyncio

        @schema_strict
        class Fake:
            async def call(self, name, args=None):
                return "ran"

        for tool, args in (("ppal-read-clip", {"slot": "1/0"}),
                           ("ppal-delete", {"ids": "3", "type": "clip"}),
                           ("ppal-read-clip", {"clipId": "3"}),
                           ("ppal-update-clip", {"trackIndex": 1, "notes": "[]"})):
            with self.subTest(tool=tool, args=args), \
                    self.assertRaisesRegex(AssertionError, "unknown arguments"):
                asyncio.run(Fake().call(tool, args))
        self.assertEqual(asyncio.run(Fake().call("ppal-read-clip", {"path": "t1/s0"})), "ran")

    def test_error_fixture_raises_through_real_pp_session(self):
        import asyncio
        from mcp_server import pp_session
        from tests.pp_fakes import load_fixture

        fixture = load_fixture("legacy/legacy_read_clip_missing_id.json")
        raw = SchemaStrictSession()
        raw.responses[fixture["tool"]] = fixture["response"]
        session = pp_session.PPSession(raw, {})
        with self.assertRaisesRegex(RuntimeError, "Producer Pal tool error: .*999999"):
            asyncio.run(session.call(fixture["tool"], fixture["arguments"]))
