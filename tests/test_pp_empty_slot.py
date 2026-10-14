"""Empty-slot handling against real PP captures (legacy/ fixtures of 2.2.0-rc1
and, where 2.3.0 captured the same call, of 2.3.0)."""
import unittest

from mcp_server import pp_session
from tests import pp_fakes


@pp_fakes.on_all_versions
class EmptySlotCaptures(unittest.TestCase):
    def test_existing_empty_slot_reads_as_empty_not_error(self):
        if self.PP_VERSION == pp_fakes.V24:
            # 2.4: an existing empty slot read by path is an ERROR, "Error: no
            # clip at t10/s5" (2.4.0/legacy/check_read_clip_existing_empty_slot,
            # v24/v24_read_clip_empty_slot) — the callers' is_empty_slot_error
            # branch reads it as empty, never as a failed read
            for relative in ("legacy/check_read_clip_existing_empty_slot.json",
                             "v24/v24_read_clip_empty_slot.json"):
                record = pp_fakes.load_fixture(relative, pp_fakes.V24)
                self.assertTrue(record["response"]["is_error"])
                text = pp_fakes.response_text(record["response"])
                self.assertTrue(pp_session.is_empty_slot_error("Producer Pal tool error: " + text))
            return
        # 2.2 and 2.3 (2.3.0/empty_slot/read_empty_slot.json): a non-error {id: null} reply
        record = pp_fakes.load_fixture("legacy/check_read_clip_existing_empty_slot.json")
        self.assertFalse(record["response"]["is_error"])
        # call_tool parses only the first content part; the WARNING text part is dropped
        raw = pp_fakes.response_text(record["response"])
        self.assertEqual(pp_session.classify_session_clip(raw, []), "empty")

    def test_real_missing_scene_error_is_an_empty_slot(self):
        text = pp_fakes.response_text(
            pp_fakes.load_fixture("legacy/legacy_read_clip_empty_slot.json")["response"])
        self.assertTrue(pp_session.is_empty_slot_error(text))
        self.assertTrue(pp_session.is_empty_slot_error("Producer Pal tool error: " + text))

    def test_other_real_errors_are_not_empty_slots(self):
        for name in ("legacy/legacy_read_clip_missing_id.json",
                     "legacy/legacy_read_clip_no_locator.json"):
            text = pp_fakes.response_text(pp_fakes.load_fixture(name)["response"])
            self.assertFalse(pp_session.is_empty_slot_error("Producer Pal tool error: " + text), name)

    def test_embedded_phrase_is_not_an_empty_slot(self):
        self.assertFalse(pp_session.is_empty_slot_error(
            "Producer Pal tool error: failed after no scene at \"s3\" while writing"))

    def test_23_missing_scene_and_other_errors_classify_like_22(self):
        empty = pp_fakes.response_text(pp_fakes.load_fixture(
            "legacy/legacy_read_clip_empty_slot.json", pp_fakes.V23)["response"])
        self.assertTrue(pp_session.is_empty_slot_error("Producer Pal tool error: " + empty))
        for name in ("legacy/legacy_read_clip_missing_id.json",
                     "legacy/legacy_read_clip_no_locator.json"):
            text = pp_fakes.response_text(pp_fakes.load_fixture(name, pp_fakes.V23)["response"])
            self.assertFalse(pp_session.is_empty_slot_error("Producer Pal tool error: " + text),
                             name)

    def test_captured_device_returns_real_empty_reply(self):
        device = pp_fakes.CapturedDevice()
        response = device.call_tool_sync("ppal-read-clip", {"path": "t0/s7"})
        if self.PP_VERSION == pp_fakes.V24:
            self.assertTrue(response["is_error"])
            self.assertTrue(pp_session.is_empty_slot_error(pp_fakes.response_text(response)))
            self.assertIn("t0/s7", pp_fakes.response_text(response))
            return
        self.assertFalse(response["is_error"])
        self.assertEqual(pp_session.classify_session_clip(
            pp_fakes.response_text(response), []), "empty")
        self.assertIn("t0/s7", pp_fakes.response_text(response))


if __name__ == "__main__":
    unittest.main()
