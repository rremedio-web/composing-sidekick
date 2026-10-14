import json
import unittest
from pathlib import Path

from mcp_server import pp_session as pp


class ProducerPalAdapterTests(unittest.TestCase):
    def test_schema_request_helpers_build_only_current_argument_names(self):
        self.assertEqual(pp.clip_locator(path="t2/s3"), {"path": "t2/s3"})
        self.assertEqual(pp.clip_locator(clip_id="clip-7"), {"id": "clip-7"})
        self.assertEqual(pp.clip_locator(track=2, scene=3), {"path": "t2/s3"})
        self.assertEqual(pp.PPRequests("2.2").read_track(4), {"trackIndex": 4})
        self.assertEqual(pp.PPRequests("2.3").read_track(4), {"path": "t4"})
        self.assertEqual(pp.PPRequests("2.3").read_track(track_id="track-4"),
                         {"id": "track-4"})
        self.assertEqual(pp.ids_arg(["a", "b"]), {"id": "a,b"})

    def test_operator_docs_pin_supported_version_and_capture_workflow(self):
        root = Path(__file__).resolve().parents[1]
        config = (root / "config/claude_mcp.md").read_text()
        self.assertIn("producer-pal@2.2.0-rc1", config)
        self.assertIn("producer-pal@2.3.0", config)
        self.assertIn("producer-pal@2.4.0", config)
        self.assertIn("SUPPORTED_PP_VERSIONS", config)
        version_doc = (root / "docs/PRODUCER_PAL_VERSIONS.md").read_text()
        self.assertIn("tools/capture_pp_fixtures.py read", version_doc)
        self.assertIn("tools/capture_pp_fixtures.py mutate", version_doc)
        self.assertIn("SUPPORTED_PP_VERSIONS", version_doc)
        self.assertIn("tools/capture_pp_fixtures.py v23", version_doc)
        for version in sorted(pp.SUPPORTED_PP_VERSIONS):
            self.assertIn("`%s`" % version, version_doc)
            self.assertTrue((root / "tests/fixtures/pp" / version / "read").is_dir(), version)
            self.assertTrue((root / "tests/fixtures/pp" / version / "mutate").is_dir(), version)

    def test_rc1_unquoted_object_keys_require_json_output(self):
        with self.assertRaisesRegex(pp.PPProtocolError, "switch Producer Pal to JSON output"):
            pp.parse_tool_response(
                '{connected:true,producerPalVersion:"2.2.0-rc1",'
                'abletonLiveVersion:"12.4.3",liveSet:{name:"test",tempo:120,'
                'timeSignature:"4/4",message:"literal {connected:true}"}}'
            )

    def test_rc1_parser_still_rejects_executable_or_single_quoted_text(self):
        for payload in (
            "{connected:(lambda:1)()}",
            "{connected:'true'}",
            "{connected:true}; launch()",
        ):
            with self.subTest(payload=payload), self.assertRaises(pp.PPProtocolError):
                pp.parse_tool_response(payload)

    def test_connect_version_and_nested_result_text(self):
        response = pp.parse_tool_response(
            '{"result":"{\\"producerPalVersion\\":\\"2.2.0-rc1\\",'
            '\\"capabilities\\":[\\"ppal-live-api\\"]}"}'
        )
        handshake = pp.handshake_from_connect(response)
        self.assertEqual(handshake["pp_version"], "2.2.0-rc1")
        self.assertTrue(handshake["capabilities"]["supports_2_2"])
        self.assertTrue(handshake["capabilities"]["live_api"])

    def test_missing_and_bad_versions_fail_closed(self):
        missing = pp.handshake_from_connect(pp.parse_tool_response('{"result":{}}'))
        self.assertEqual(missing["pp_version"], "unknown")
        self.assertFalse(missing["capabilities"]["supports_2_2"])
        with self.assertRaises(pp.PPProtocolError):
            pp.handshake_from_connect(pp.parse_tool_response(
                '{"result":{"producerPalVersion":"2.2"}}'))

    def test_connect_without_notation_is_unverified_not_assumed(self):
        handshake = pp.handshake_from_connect(pp.parse_tool_response(
            '{"result":{"connected":true,"producerPalVersion":"2.2.0-rc1",'
            '"liveSet":{"tempo":120}}}'))
        self.assertIsNone(handshake["observed"]["notation"])
        self.assertEqual(handshake["requested"]["notation"], "midi-json")
        pp.validate_handshake(handshake)

    def test_mutation_guard_accepts_only_captured_pp_build(self):
        self.assertEqual(pp.SUPPORTED_PP_VERSIONS, frozenset({"2.2.0-rc1", "2.3.0", "2.4.0"}))
        for version in ("2.2.0-rc1", "2.3.0", "2.4.0"):
            handshake = pp.handshake_from_connect(pp.parse_tool_response(
                '{"result":{"producerPalVersion":"%s"}}' % version))
            pp.validate_handshake(handshake)
        for version in ("2.2.0", "2.2.1", "2.3.1", "2.4.1", "2.1.0"):
            with self.subTest(version=version):
                handshake = pp.handshake_from_connect(pp.parse_tool_response(
                    '{"result":{"producerPalVersion":"%s"}}' % version))
                with self.assertRaisesRegex(
                        pp.PPProtocolError,
                        r"Producer Pal %s is not verified by captured fixtures yet"
                        % version):
                    pp.validate_handshake(handshake)

    def test_compact_output_refusal_names_json_setting(self):
        compact = '{connected:true,producerPalVersion:"2.2.0-rc1"}'
        with self.assertRaisesRegex(pp.PPProtocolError, "switch Producer Pal to JSON output"):
            pp.parse_tool_response(compact)

    def test_uncaptured_pp_21_fails_mutation_guard(self):
        handshake = pp.handshake_from_connect(pp.parse_tool_response(
            '{"result":{"connected":true,"producerPalVersion":"2.1.0"}}'))
        with self.assertRaisesRegex(pp.PPProtocolError, "not verified by captured fixtures yet"):
            pp.validate_handshake(handshake)
        self.assertTrue(handshake["capabilities"]["supports_2_x"])
        self.assertFalse(handshake["capabilities"]["live_api"])

    def test_missing_session_clip_collection_is_incomplete(self):
        inventory = pp.session_clips_inventory(pp.parse_tool_response(
            '{"result":{"id":"jam","name":"SIDEKICK JAM"}}'))
        self.assertFalse(inventory["complete"])
        with self.assertRaises(pp.LiveWriteVerificationError):
            pp.parse_session_clips(pp.parse_tool_response(
                '{"result":{"id":"jam","name":"SIDEKICK JAM"}}'), strict=True)

    def test_captured_session_clips_without_names_remain_complete(self):
        root = Path(__file__).resolve().parent / "fixtures/pp/2.2.0-rc1"
        # Both fixtures are captured ppal-read-track records; the device omits
        # empty clip names while retaining each clip's id and path.
        for relative in (
                "read/read_track_t1_index.json",
                "legacy/legacy_read_after_create_clip.json"):
            with self.subTest(fixture=relative):
                captured = json.loads((root / relative).read_text())
                result = captured["response"]["content"][0]["json"]
                response = pp.parse_tool_response(json.dumps({"result": result}))
                inventory = pp.session_clips_inventory(response)
                self.assertTrue(inventory["complete"])
                unnamed = [clip for clip in result["sessionClips"]
                           if "name" not in clip]
                self.assertTrue(unnamed)
                self.assertTrue(all(item["name"] == "" for item in inventory["clips"]
                                    if item["clip"]["id"] in {
                                        clip["id"] for clip in unnamed}))
                self.assertEqual(pp.parse_session_clips(response, strict=True), [
                    {"slot": item["slot"], "name": item["name"]}
                    for item in inventory["clips"]
                ])

    def test_live_api_shape_falsey_values_and_receipts(self):
        payload = pp.build_live_api_request("live_set", [
            {"type": "set_path", "value": ""},
            {"type": "set_mode", "value": 0},
            {"type": "set_id", "value": False},
            {"type": "get", "property": "tempo"},
        ])
        self.assertEqual(payload["path"], "live_set")
        self.assertEqual(payload["operations"][1]["value"], 0)
        nested = pp.parse_tool_response('{"result":{"path":"live_set",'
                                         '"id":"x","results":['
                                         '{"operation":{"type":"get","property":"tempo"},"result":0},'
                                         '{"operation":{"type":"set","property":"tempo","value":false},"result":false}]}}')
        receipts = pp.extract_live_api_receipts(nested)
        self.assertEqual([r["result"] for r in receipts], [0, False])
        self.assertEqual(receipts[0]["operation"]["type"], "get")
        with self.assertRaises(pp.PPProtocolError):
            pp.extract_live_api_receipts(pp.parse_tool_response('{"result":{"results":{}}}'))

    def test_operation_validation_and_path_fallbacks(self):
        for operation in ({"type": "bogus"}, {"type": "get"},
                          {"type": "set", "value": None},
                          {"type": "goto", "value": ""}):
            with self.assertRaises(ValueError):
                pp.build_live_api_request(None, [operation])
        response = pp.parse_tool_response('{"result":{"tracks":[{"path":"t3",'
                                           '"name":"Pad"}],"sessionClips":[{"path":"t3/s4",'
                                           '"name":"Clip"}]}}')
        self.assertEqual(pp.track_index_by_name(response, "Pad"), (3, None))
        self.assertEqual(pp.track_names_by_index(response), {3: "Pad"})
        self.assertEqual(pp.parse_session_clips(response), [{"slot": "3/4", "name": "Clip"}])
        self.assertEqual(pp.max_scene_index_in_track(response), 4)
        lane = pp.parse_tool_response('{"result":{"sessionClips":[{"path":"t3/l4","name":"Lane"}]}}')
        self.assertEqual(pp.parse_session_clips(lane), [])
        self.assertIsNone(pp.max_scene_index_in_track(lane))


if __name__ == "__main__":
    unittest.main()
