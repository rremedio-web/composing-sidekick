"""The NOCTURNEv2 piece replay device is faithful to its capture and strict."""
import json
import unittest

from tests.pp_fakes import (PieceDevice, captured_json, response_text,
                            validate_call_values)


class PieceDeviceFidelityTests(unittest.TestCase):
    def _overview(self, device, args):
        return json.loads(response_text(device.call_tool_sync("ppal-read-live-set", args)))

    def test_overview_filter_reproduces_every_captured_overview(self):
        device = PieceDevice()
        for fixture, args in (
                ("piece/read_live_set_default.json", {}),
                ("piece/read_live_set_tracks_locators.json", {"include": ["tracks", "locators"]}),
                ("piece/piece_live_set.json",
                 {"include": ["tracks", "scenes", "routings", "mixer", "locators"]})):
            with self.subTest(fixture=fixture):
                self.assertEqual(self._overview(device, args), captured_json(fixture))

    def test_read_track_answers_only_captured_include_lists(self):
        device = PieceDevice()
        record = json.loads(response_text(device.call_tool_sync("ppal-read-track", {
            "trackIndex": 6, "include": ["arrangement-clips", "notes", "timing", "sample",
                                         "devices", "routings", "mixer"]})))
        self.assertEqual(record, captured_json("piece/piece_track_06.json"))
        with self.assertRaisesRegex(AssertionError, "was not captured"):
            device.call_tool_sync("ppal-read-track", {"trackIndex": 6, "include": ["notes"]})

    def test_values_are_checked_against_schema_enums_and_types(self):
        device = PieceDevice()
        with self.assertRaisesRegex(AssertionError, "not in schema enum"):
            device.call_tool_sync("ppal-read-live-set", {"include": ["clips"]})
        with self.assertRaisesRegex(AssertionError, "must be integer"):
            device.call_tool_sync("ppal-read-track", {"trackIndex": "6"})
        with self.assertRaisesRegex(AssertionError, "not in schema enum"):
            device.call_tool_sync("ppal-read-track", {"trackType": "group", "trackIndex": 0})
        with self.assertRaisesRegex(AssertionError, "missing required"):
            validate_call_values("ppal-live-api", {"path": "live_set"})

    def test_live_api_is_off_like_the_capture_device(self):
        device = PieceDevice()
        with self.assertRaisesRegex(AssertionError, "Live API is off"):
            device.call_tool_sync("ppal-live-api", {
                "path": "live_set",
                "operations": [{"type": "getProperty", "property": "file_path"}]})
        on = PieceDevice(live_api=True)
        payload = json.loads(response_text(on.call_tool_sync("ppal-live-api", {
            "path": "live_set",
            "operations": [{"type": "getProperty", "property": "file_path"}]})))
        self.assertTrue(payload["results"][0]["result"].endswith("NOCTURNEv2.als"))


if __name__ == "__main__":
    unittest.main()
