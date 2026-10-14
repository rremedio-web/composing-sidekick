"""Exercise Session Ears through FastMCP's async dispatch boundary."""
import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from mcp_server import live_write, server, session_ears


class TestSessionEarsMCPDispatch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.identity = {"set_id": "dispatch-test", "runtime_epoch": "test", "file_path": None}

    def tearDown(self):
        session_ears._reset_registry_for_tests()

    async def test_inventory_reaches_async_discovery_through_mcp(self):
        discovery = AsyncMock(return_value=("dispatch-test", self.identity, []))
        with patch.object(session_ears, "_discover_live_takes", discovery):
            result = await server.mcp.call_tool("inventory_session_audio", {})
        discovery.assert_awaited_once()
        envelope = json.loads(result[0].text)
        self.assertEqual(envelope["takes"], [])
        self.assertTrue(envelope["inventory_id"].startswith("inv-"))

    async def test_unavailable_live_keeps_actionable_error_through_mcp(self):
        discovery = AsyncMock(side_effect=ConnectionError("test endpoint offline"))
        with patch.object(session_ears, "_discover_live_takes", discovery):
            with self.assertRaisesRegex(Exception, "live_inventory_unavailable"):
                await server.mcp.call_tool("inventory_session_audio", {})
        discovery.assert_awaited_once()

    async def test_listener_checks_set_change_before_upload_through_mcp(self):
        @asynccontextmanager
        async def operation():
            yield object()

        envelope = session_ears._register_inventory(
            "dispatch-test", [], set_identity=self.identity, pp_operation_session=operation,
        )
        changed = {**self.identity, "set_id": "other-set"}
        with patch.object(live_write, "read_set_identity", AsyncMock(return_value=changed)) as read:
            with patch.object(session_ears, "_which") as executable:
                with self.assertRaisesRegex(Exception, "live set identity changed"):
                    await server.mcp.call_tool("listen_to_takes", {
                        "inventory_id": envelope["inventory_id"], "take_ids": [],
                    })
        read.assert_awaited_once()
        executable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
