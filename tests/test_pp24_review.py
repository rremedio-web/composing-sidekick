"""Adversarial review of b15/pp24 — each test cites the captured fixture it
rests on (tests/fixtures/pp/<build>/…)."""
import asyncio
import json
import unittest
from unittest.mock import patch

from mcp_server import core, export_bridge, live_write as lw, pp_codec
from mcp_server import pp_session as pp
from tests import pp_fakes
from tests.pp_fakes import V22, V23, V24

NOTES = [{"pitch": 60, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 100}]


def captured_error(relative, version):
    return pp_fakes.response_text(pp_fakes.load_fixture(relative, version)["response"])


def free_22_reply(path):
    """2.2.0-rc1/legacy/check_read_clip_existing_empty_slot.json for ``path``:
    {id: null, name: null, path, type: null} + WARNING "no clip at <path>"."""
    rec = pp_fakes.load_fixture("legacy/check_read_clip_existing_empty_slot.json", V22)
    content = json.loads(json.dumps(rec["response"]["content"]).replace("t10/s5", path))

    class Raw:
        async def call_tool(self, tool, arguments):
            return pp_fakes.mcp_result({"content": content, "is_error": False})

    return asyncio.run(pp.call_tool(Raw(), "ppal-read-clip", {"path": path}))


# --- P1: handoffs never create into a slot they have not proven empty ---------

class HandoffSlotGuardTests(unittest.TestCase):
    """2.4 create-clip REPLACES an occupied slot (2.4.0/v24/
    v24_create_session_clip_replaces.json): every emitted create is preceded
    by a guard read of the same slot that proceeds only on a captured
    empty-slot shape."""

    def test_send_to_ableton_guards_every_session_create(self):
        for addressing in ("2.2", "2.3", "2.4"):
            for kwargs in (dict(track=3, session_scene=2), dict(track=None)):
                with self.subTest(addressing=addressing, **kwargs):
                    handoff = export_bridge.send_to_ableton(
                        NOTES, clip_name="x", tempo=120, addressing=addressing, **kwargs)
                    tools = [c["tool"] for c in handoff["tool_calls"]]
                    create = tools.index("ppal-create-clip")
                    guard = handoff["tool_calls"][create - 1]
                    path = handoff["tool_calls"][create]["arguments"]["path"]
                    self.assertEqual(guard["tool"], "ppal-read-clip")
                    self.assertEqual(guard["arguments"], {"path": path, "include": []})
                    self.assertEqual(guard["proceed_only_if"], "empty_slot")
                    self.assertIn("only if", handoff["instruction"])
                    self.assertIn("empty", handoff["instruction"])
                    pp_fakes.validate_call("ppal-read-clip", guard["arguments"],
                                           version={"2.2": V22, "2.3": V23, "2.4": V24}[addressing])

    def test_arrangement_create_is_guarded_by_a_track_read(self):
        handoff = export_bridge.send_to_ableton(NOTES, track=4, arrangement_start="9|1",
                                                addressing="2.4")
        tools = [c["tool"] for c in handoff["tool_calls"]]
        guard = handoff["tool_calls"][tools.index("ppal-create-clip") - 1]
        self.assertEqual(guard["tool"], "ppal-read-track")
        self.assertEqual(guard["arguments"], {"path": "t4", "include": ["arrangement-clips"]})
        self.assertEqual(guard["proceed_only_if"], "span_free")

    def test_idea_pack_lanes_guard_each_of_his_slots(self):
        pack = {"id": "p", "ideas": [{"notes": NOTES}, {"notes": NOTES}]}
        out = core.prepare_idea_pack_audition_lanes(pack, confirm=True, start_track=5)
        reads = [c for c in out["tool_calls"] if c["tool"] == "ppal-read-clip"]
        creates = [c for c in out["tool_calls"] if c["tool"] == "ppal-create-clip"]
        self.assertEqual([r["arguments"]["path"] for r in reads],
                         [c["arguments"]["path"] for c in creates])
        self.assertEqual([c["arguments"]["path"] for c in creates], ["t5/s0", "t6/s0"])
        for c in out["tool_calls"]:
            if c["tool"] == "ppal-create-clip":
                prev = out["tool_calls"][out["tool_calls"].index(c) - 1]
                self.assertEqual(prev["tool"], "ppal-read-clip")
        self.assertIn("empty", out["instruction"])

    def test_the_guard_verdict_reads_the_captured_shapes(self):
        # the verdict the instruction names is pp_session.slot_read_verdict
        occupied = pp_fakes.load_fixture("v24/v24_read_replaced_slot.json", V24)
        self.assertEqual(pp.slot_read_verdict(
            "t10/s0", reply=pp.parse_tool_response(json.dumps(
                {"result": occupied["response"]["content"][0]["json"]})))["state"], "occupied")
        self.assertEqual(pp.slot_read_verdict(
            "t10/s0", error=captured_error("v24/v24_read_clip_empty_slot.json", V24))["state"],
            "free")


# --- P2: free is ONLY a captured free shape for the requested slot ------------

class SlotReadVerdictTests(unittest.TestCase):
    def test_captured_free_shapes_are_free(self):
        # 2.2 {id: null, …} (+ its "no clip at" warning), with and without the warning
        reply = free_22_reply("t3/s0")
        self.assertEqual(pp.slot_read_verdict("t3/s0", reply=reply)["state"], "free")
        bare = pp.parse_tool_response(json.dumps({"result": {
            "id": None, "name": None, "path": "t3/s0", "type": None}}))
        self.assertEqual(pp.slot_read_verdict("t3/s0", reply=bare)["state"], "free")
        # 2.4 error, for the requested path (v24_read_clip_empty_slot,
        # 2.4.0/legacy/check_read_clip_existing_empty_slot)
        for relative, path in (("v24/v24_read_clip_empty_slot.json", "t10/s0"),
                               ("legacy/check_read_clip_existing_empty_slot.json", "t10/s5")):
            text = "Producer Pal tool error: " + captured_error(relative, V24)
            self.assertEqual(pp.slot_read_verdict(path, error=text)["state"], "free")

    def test_anything_else_is_not_free(self):
        not_free = {
            "warning_only": pp.PPResponse("", None, ["WARNING: no clip at t3/s0"], True,
                                          warning_only=True),
            "empty list": pp.parse_tool_response('{"result": []}'),
            "empty object": pp.parse_tool_response('{"result": {}}'),
            "his clip without id": pp.parse_tool_response(json.dumps(
                {"result": {"name": "his loop", "type": "midi"}})),
            "null id, other path": pp.parse_tool_response(json.dumps({"result": {
                "id": None, "name": None, "path": "t9/s9", "type": None}})),
            "null id, named": pp.parse_tool_response(json.dumps({"result": {
                "id": None, "name": "his loop", "path": "t3/s0", "type": None}})),
            "null id + truncation": pp.PPResponse("", {
                "id": None, "name": None, "path": "t3/s0", "type": None},
                ["WARNING: session clip listing truncated"], True),
        }
        for label, reply in not_free.items():
            with self.subTest(label=label):
                self.assertNotEqual(pp.slot_read_verdict("t3/s0", reply=reply)["state"], "free")
        occupied = pp.parse_tool_response(json.dumps({"result": {
            "id": "77", "name": "his loop", "path": "t3/s0", "type": "midi"}}))
        self.assertEqual(pp.slot_read_verdict("t3/s0", reply=occupied)["state"], "occupied")

    def test_an_error_must_name_the_requested_slot(self):
        empty = "Producer Pal tool error: " + captured_error("v24/v24_read_clip_empty_slot.json", V24)
        self.assertTrue(pp.is_empty_slot_error(empty, path="t10/s0"))
        self.assertFalse(pp.is_empty_slot_error(empty, path="t3/s0"))
        self.assertTrue(pp.is_empty_slot_error(empty))          # readers without a path
        self.assertNotEqual(pp.slot_read_verdict("t3/s0", error="Error: no clip at t9/s9")["state"],
                            "free")
        # 2.2 / 2.3 missing scene: "no scene at "s97"" frees only a read of scene 97
        scene = "Producer Pal tool error: " + captured_error(
            "legacy/legacy_read_clip_empty_slot.json", V23)
        self.assertTrue(pp.is_empty_slot_error(scene, path="t9/s97"))
        self.assertFalse(pp.is_empty_slot_error(scene, path="t9/s2"))
        # any other read failure is not free (kills "any failure counts as free")
        for text in ("Producer Pal tool error: " + captured_error(
                         "v24/v24_read_clip_missing_id.json", V24),
                     "Producer Pal tool error: " + captured_error(
                         "v24/v24_read_track_missing.json", V24),
                     "Producer Pal tool error: read timed out", ""):
            with self.subTest(text=text):
                self.assertEqual(pp.slot_read_verdict("t3/s0", error=text)["state"], "unknown")


class SlotReadVerdict23CaptureTests(unittest.TestCase):
    """2.3.0/empty_slot/ — a read-only capture on the 2.3 SCRATCH Set
    (2026-10-03), the shape the JAM/BOOTH pre-read meets on his 2.3 Sets."""

    def read(self, name, path):
        rec = pp_fakes.load_fixture("empty_slot/%s.json" % name, V23)
        if rec["response"].get("is_error"):
            return pp.slot_read_verdict(path, error="Producer Pal tool error: "
                                        + pp_fakes.response_text(rec["response"]))

        class Raw:
            async def call_tool(self, tool, arguments):
                return pp_fakes.mcp_result(rec["response"])

        reply = asyncio.run(pp.call_tool(Raw(), "ppal-read-clip", {"path": path}))
        return pp.slot_read_verdict(path, reply=reply)

    def test_captured_23_empty_slot_is_free(self):
        for name in ("read_empty_slot", "read_empty_slot_notes"):
            with self.subTest(name=name):
                self.assertEqual(self.read(name, "t0/s7")["state"], "free")

    def test_captured_23_empty_slot_is_not_free_for_another_slot(self):
        self.assertNotEqual(self.read("read_empty_slot", "t0/s6")["state"], "free")

    def test_captured_23_occupied_slot_is_occupied(self):
        verdict = self.read("read_occupied_slot", "t9/s0")
        self.assertEqual((verdict["state"], verdict["clip_id"]), ("occupied", "359"))

    def test_captured_23_slot_past_last_scene_is_free_for_that_scene_only(self):
        self.assertEqual(self.read("read_slot_past_last_scene", "t0/s13")["state"], "free")
        self.assertNotEqual(self.read("read_slot_past_last_scene", "t0/s12")["state"], "free")


class SlotGuardInWritersTests(unittest.TestCase):
    def setUp(self):
        pp.set_live_process_incarnation_resolver(
            lambda: {"pid": 1, "start_time": "Sat Oct  3 10:00:00 2026"})

    def tearDown(self):
        pp.set_live_process_incarnation_resolver(None)

    def run_with(self, device, fn, *args):
        with patch.object(pp, "operation_session", device.operation()):
            return asyncio.run(fn(*args))

    def test_jam_and_booth_still_write_on_every_build(self):
        # the empty-slot reads the fakes replay: 2.2 and 2.3 the 2.2 capture
        # (2.3 captured none of its own), 2.4 its error
        for version in (V22, V23, V24):
            with self.subTest(version=version):
                device = pp_fakes.CapturedDevice(version=version)
                self.assertTrue(self.run_with(device, lw.write_answer_to_jam_track,
                                              NOTES, "a")["verified_ok"])
                device = pp_fakes.CapturedDevice(version=version)
                self.assertTrue(self.run_with(device, lw.stage_named_clips,
                                              [{"name": "b", "notes": NOTES}])["verified_ok"])

    def test_an_unrecognised_slot_read_refuses_the_write(self):
        for label, answer in (
                ("his clip without id", lambda path: pp_fakes.json_response(
                    {"name": "his loop", "type": "midi", "path": path})),
                ("empty object", lambda path: pp_fakes.json_response({})),
                ("another slot's empty error", lambda path: pp_fakes.error_response(
                    "Error: no clip at t99/s9")),
                ("timeout", lambda path: pp_fakes.error_response("Error: read timed out"))):
            with self.subTest(label=label):
                device = pp_fakes.CapturedDevice(version=V24)
                original = device.handlers["ppal-read-clip"]

                def read(args, answer=answer, original=original):
                    if args.get("include") == [] and "path" in args:
                        return answer(args["path"])
                    return original(args)

                device.handlers["ppal-read-clip"] = read
                with self.assertRaises(lw.LiveWriteVerificationError) as caught:
                    self.run_with(device, lw.write_answer_to_jam_track, NOTES, "a")
                self.assertIn("refusing", caught.exception.detail)
                self.assertNotIn("ppal-create-clip", [n for n, _ in device.wire_calls])


# --- P3 -------------------------------------------------------------------------

class CleanupTests(unittest.TestCase):
    def test_delete_succeeded_needs_deleted_true_or_deletedPath(self):
        # v24_delete_clip_missing_id: {detail, id} — not a delete by structure alone
        miss = pp_fakes.captured_json("v24/v24_delete_clip_missing_id.json", V24)
        self.assertFalse(pp.delete_succeeded(miss, miss["id"]))

    def test_an_uncut_split_fails_by_count(self):
        # v24_split_clip_deprecated_key: ONE object + "split cut nothing"; a cut
        # replies head and tail (v23_split_clip): count=2 is the judgement
        rec = pp_fakes.load_fixture("v24/v24_split_clip_deprecated_key.json", V24)
        content = rec["response"]["content"]
        uncut = pp.PPResponse(json.dumps({"result": content[0]["json"]}), content[0]["json"],
                              [p["text"] for p in content[1:]])
        failure = pp.write_failed(uncut, count=2)
        self.assertIn("split cut nothing", failure)
        with self.assertRaises(ValueError):
            pp.write_failed(uncut, expect="split")

    def test_example_config_pins_producer_pal_23(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        config = json.loads((root / ".mcp.example.json").read_text())
        self.assertIn("producer-pal@2.3.0", config["mcpServers"]["producer-pal"]["args"])


if __name__ == "__main__":
    unittest.main()
