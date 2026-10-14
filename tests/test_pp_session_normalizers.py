"""Producer Pal record normalization from captured and documented wire shapes."""

import json
from pathlib import Path
import unittest

from mcp_server import pp_session as pp


FIXTURES = Path(__file__).parent / "fixtures" / "pp" / "2.2.0-rc1"


def captured(tool, name):
    """Load the recorded response payload from an actual 2.2 device call."""
    record = json.loads((FIXTURES / name).read_text())
    assert record["tool"] == tool
    return record["response"]["content"][0]["json"]


def captured_error(tool, name):
    """Load captured error text from the actual 2.2 device response."""
    record = json.loads((FIXTURES / name).read_text())
    assert record["tool"] == tool
    return record["response"]["content"][0]["text"]


class ProducerPalRecordNormalizerTests(unittest.TestCase):
    def test_all_captured_22_records_pass_through_the_adapter(self):
        # Every JSON response is replayed from committed 2.2 device captures.
        def visit(value):
            if isinstance(value, dict):
                for key in ("tracks", "returnTracks"):
                    for record in value.get(key, []):
                        normalized = pp.normalize_track(record)
                        self.assertEqual(set(normalized), set(pp._normalized_record()))
                for key in ("masterTrack", "mainTrack"):
                    if isinstance(value.get(key), dict):
                        normalized = pp.normalize_track({key: value[key]})
                        self.assertTrue(normalized["is_main"])
                for key in ("sessionClips", "arrangementClips"):
                    for record in value.get(key, []):
                        normalized = pp.normalize_clip(record)
                        self.assertEqual(set(normalized), set(pp._normalized_record()))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        for path in FIXTURES.rglob("*.json"):
            record = json.loads(path.read_text())
            response = record.get("response", {})
            if record.get("tool") == "ppal-playback":
                for item in response.get("content", []):
                    if "json" in item:
                        self.assertEqual(set(pp.normalize_playback(item["json"])),
                                         set(pp._normalized_record()))
            elif record.get("tool") == "ppal-delete":
                for item in response.get("content", []):
                    if "json" in item:
                        self.assertEqual(set(pp.normalize_delete(item["json"])),
                                         set(pp._normalized_record()))
            for item in response.get("content", []):
                if "json" in item:
                    visit(item["json"])
                elif "text" in item:
                    pp.parse_error(item["text"])

    def test_normalize_captured_22_records(self):
        # Shapes are captured from read_live_set_tracks_locators and read_track_t1_index.
        overview = captured("ppal-read-live-set", "read/read_live_set_tracks_locators.json")
        track = pp.normalize_track(overview["tracks"][1])
        self.assertEqual(track["track_index"], 1)
        self.assertEqual(track["track_path"], None)
        self.assertFalse(track["is_main"])

        detail = captured("ppal-read-track", "read/read_track_t1_index.json")
        clip = pp.normalize_clip(detail["arrangementClips"][0], track_index=1)
        self.assertEqual(clip["arrangement_start"], "1|1")
        self.assertEqual(clip["track_index"], 1)

        playback = pp.normalize_playback(captured(
            "ppal-playback", "mutate/playback_update_arrangement.json"))
        self.assertEqual(playback["loop"], {"on": True, "start": "5|1", "end": "9|1"})
        self.assertEqual(playback["playhead"], "5|1")

        receipt = pp.normalize_delete(captured("ppal-delete", "mutate/delete_track.json"))
        self.assertTrue(receipt["deleted"])

    def test_normalize_migration_guide_23_shapes(self):
        # Guide-derived shapes (not captured): migration guide removes numeric indexes and
        # arrangementStart, encodes them in paths, and removes playback currentTime.
        track = pp.normalize_track({"id": "3", "path": "t3", "name": "Pad"})
        self.assertEqual(track["track_index"], 3)
        self.assertEqual(track["track_path"], "t3")
        clip = pp.normalize_clip({"id": "c", "path": "t0[5|1]"}, track_index=0)
        self.assertEqual(clip["arrangement_start"], "5|1")
        self.assertEqual(clip["track_index"], 0)
        main = pp.normalize_track({"id": "m", "path": "mt", "type": "master"})
        self.assertTrue(main["is_main"])
        playback = pp.normalize_playback({"loop": True, "loopStart": "1|1", "loopEnd": "9|1"})
        self.assertEqual(playback["loop"], {"on": True, "start": "1|1", "end": "9|1"})
        self.assertIsNone(playback["playhead"])
        deleted = pp.normalize_delete({"deletedPath": "t3"})
        self.assertTrue(deleted["deleted"])
        self.assertEqual(deleted["track_path"], "t3")
        self.assertEqual(pp.normalize_track({"path": "rt0"})["track_index"], None)
        self.assertEqual(pp.normalize_clip({"path": "t1/l0"})["take_lane"], 0)
        self.assertIsNone(pp.normalize_clip({"path": "t1/l0"})["scene_index"])
        self.assertEqual(pp.normalize_clip({"path": "t1/d2"})["device_index"], 2)

    def test_parse_captured_and_guide_error_messages(self):
        # The first is the captured 2.2 error envelope; the second is guide-derived 2.3.
        text = captured_error("ppal-read-track", "read/read_track_t2_path.json")
        self.assertEqual(pp.parse_error(text), "Either id or trackIndex must be provided")
        self.assertEqual(pp.parse_error("Error: Track not found"), "Track not found")
        self.assertIsNone(pp.parse_error("{\"id\":\"3\"}"))

    def test_adapter_handles_return_main_and_lane_paths_without_index_collisions(self):
        # Guide-derived 2.3 shapes (not captured): paths replace numeric response indexes.
        returned = pp.normalize_track({"path": "rt0", "name": "Return"})
        self.assertIsNone(returned["track_index"])
        self.assertEqual(returned["return_index"], 0)
        self.assertIsNone(pp.normalize_track({"path": "t3/s0"})["track_index"])
        main_clip = pp.normalize_clip({"path": "mt"})
        self.assertTrue(main_clip["is_main"])
        lane_clip = pp.normalize_clip({"path": "t0/l0[5|1]"})
        self.assertEqual(lane_clip["track_index"], 0)
        self.assertEqual(lane_clip["take_lane"], 0)
        self.assertEqual(lane_clip["arrangement_start"], "5|1")

    def test_unverified_take_lane_is_preserved_and_absent_loop_stays_unknown(self):
        # takeLane has no captured fixture; retain its raw value pending device evidence.
        self.assertEqual(pp.normalize_clip({"takeLane": 0})["take_lane"], 0)
        self.assertEqual(pp.normalize_clip({"takeLane": 3})["take_lane"], 3)
        self.assertIsNone(pp.normalize_playback({})["loop"]["on"])

    def test_delete_receipt_extracts_id_from_captured_success_shape(self):
        # Replays tests/fixtures/pp/2.2.0-rc1/mutate/delete_track.json.
        result = captured("ppal-delete", "mutate/delete_track.json")
        receipt = pp.extract_delete_receipt(
            pp.parse_tool_response(json.dumps({"result": result})))
        self.assertEqual(receipt["deleted_ids"], [result["id"]])

    def test_version_30_gets_actionable_fixture_message(self):
        # Version is intentionally unsupported until captured; refuse with capture instructions.
        handshake = pp.handshake_from_connect(pp.parse_tool_response(
            '{"result":{"producerPalVersion":"3.0.0"}}'))
        with self.assertRaisesRegex(pp.PPProtocolError,
                                    r"Producer Pal 3\.0\.0 is not verified.*capture_pp_fixtures"):
            pp.validate_handshake(handshake)


if __name__ == "__main__":
    unittest.main()
