"""tools/capture_pp_fixtures.py legacy mode leaves the SCRATCH Set as it found it.

On Producer Pal 2.3 the create-track reply names the new track by path
({"id", "path": "t10"}, 2.3.0/legacy/legacy_create_track.json) and the
overview lists tracks by path only (legacy_live_set_after_create.json); the
old cleanup looked for ``trackIndex`` and left an empty track behind. The
script runs against an in-memory device (no network) and writes its
fixtures to a temporary directory, never to tests/fixtures.
"""
import asyncio
import importlib.util
import json
import re
import sys
import tempfile
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from tests import pp_fakes
from tests.pp_fakes import V22, V23

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "capture_pp_fixtures_under_test", ROOT / "tools" / "capture_pp_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Device:
    """A SCRATCH Set speaking one build's reply shapes (the legacy captures)."""

    def __init__(self, version):
        self.version = version
        live_set = pp_fakes.captured_json("legacy/legacy_live_set_after_create.json", version)
        # the Set before the legacy mode ran: without the track it created
        created = pp_fakes.captured_json("legacy/legacy_create_track.json", version)
        self.created_id = created["id"]
        self.tracks = [t for t in live_set["tracks"] if t.get("id") != self.created_id]
        self.live_set = {k: v for k, v in live_set.items() if k != "tracks"}
        self.calls = []

    def _track_record(self, index, track_id, name):
        if self.version == V22:
            return {"id": track_id, "name": name, "trackIndex": index, "type": "midi"}
        return {"id": track_id, "name": name, "path": "t%d" % index, "type": "midi"}

    def reply(self, name, args):
        if name == "ppal-connect":
            return pp_fakes.load_fixture("read/connect.json", self.version)["response"]
        if name == "ppal-read-live-set":
            return pp_fakes.json_response({**self.live_set, "tracks": list(self.tracks)})
        if name == "ppal-live-api":
            return pp_fakes.json_response({"id": "1", "path": "live_set", "results": [
                {"operation": op, "result": "/Users/USER/Desktop/SCRATCH-capture.als"}
                for op in args["operations"]]})
        if name == "ppal-create-track":
            record = self._track_record(len(self.tracks), self.created_id, args.get("name"))
            self.tracks.append(record)
            reply = {"id": self.created_id}
            reply.update({"trackIndex": len(self.tracks) - 1} if self.version == V22
                         else {"path": "t%d" % (len(self.tracks) - 1)})
            return pp_fakes.json_response(reply)
        if name == "ppal-delete" and args.get("type") == "track":
            ids = str(args.get("id") or args.get("ids") or "").split(",")
            before = len(self.tracks)
            self.tracks = [t for t in self.tracks if t.get("id") not in ids]
            return pp_fakes.json_response({"deleted": len(self.tracks) < before,
                                           "id": ids[0], "type": "track"})
        if name == "ppal-read-track":
            return pp_fakes.json_response({"id": "x", "sessionClips": []})
        return pp_fakes.json_response({"id": None})


class _V23bDevice(_Device):
    """A 2.3 SCRATCH Set for the v23b mode: scenes by path with ids, tracks
    appended by 't+', and the captured side effect — a create-clip into a
    slot past the last scene silently creates the missing scenes
    (v23b_refuse_create_past_last_scene). Everything else replies a stub."""

    def __init__(self):
        super().__init__(V23)
        before = pp_fakes.captured_json("v23b/v23b_live_set_scenes_before.json", V23)
        self.scenes = [{"id": sc["id"], "name": sc["name"]} for sc in before["scenes"]]
        self.next_id = 900

    def _new_id(self):
        self.next_id += 1
        return str(self.next_id)

    def _scene_records(self):
        return [{**sc, "path": "s%d" % i} for i, sc in enumerate(self.scenes)]

    def reply(self, name, args):
        if name == "ppal-read-live-set":
            payload = {**self.live_set, "sceneCount": len(self.scenes)}
            include = args.get("include") or []
            if "tracks" in include:
                payload["tracks"] = list(self.tracks)
            if "scenes" in include:
                payload["scenes"] = self._scene_records()
            return pp_fakes.json_response(payload)
        if name == "ppal-create-track":
            record = self._track_record(len(self.tracks), self._new_id(), args.get("name"))
            record["type"] = args.get("type", "midi")
            self.tracks.append(record)
            return pp_fakes.json_response({"id": record["id"], "path": record["path"]})
        if name == "ppal-create-scene":       # v23b_create_scene_append: {id, path}
            self.scenes.append({"id": self._new_id(), "name": args.get("name") or ""})
            return pp_fakes.json_response({"id": self.scenes[-1]["id"],
                                           "path": "s%d" % (len(self.scenes) - 1)})
        if name == "ppal-create-clip":
            path = str(args.get("path") or "")
            slot = re.fullmatch(r"t(\d+)/s(\d+)", path)
            if slot:
                while len(self.scenes) <= int(slot.group(2)):   # the 2.3 side effect
                    self.scenes.append({"id": self._new_id(), "name": ""})
            return pp_fakes.json_response({"id": self._new_id(), "noteCount": 2, "path": path})
        if name == "ppal-delete" and args.get("type") == "scene":
            ids = str(args.get("id") or "").split(",")
            self.scenes = [sc for sc in self.scenes if sc["id"] not in ids]
            return pp_fakes.json_response([{"deleted": True, "id": i, "type": "scene"}
                                           for i in ids])
        return super().reply(name, args)


class _V24Device(_V23bDevice):
    """A SCRATCH Set answering in the 2.4 shapes the migration guide describes
    (no 2.4 captures exist yet — this fake is from the guide, not the device):
    one entry per target, batch "t+,t+" / "s+,s+" paths, failures as
    {ok:false, detail}, deletes as {deletedPath}. The connect reply is the 2.3
    capture with the version rewritten, since the script only reads the version."""

    def reply(self, name, args):
        if name == "ppal-connect":
            res = json.loads(json.dumps(super().reply(name, args)))
            for part in res.get("content", []):
                if "json" in part:
                    part["json"]["producerPalVersion"] = "2.4.0"
            return res
        if name == "ppal-create-track":
            out = []
            for i in range(max(len(str(args.get("path") or "t+").split(",")), int(args.get("count") or 1))):
                record = self._track_record(len(self.tracks), self._new_id(), "CAPTURE 24 %d" % i)
                record["type"] = args.get("type", "midi")
                self.tracks.append(record)
                out.append({"id": record["id"], "path": record["path"]})
            return pp_fakes.json_response(out if len(out) > 1 else out[0])
        if name == "ppal-create-scene":
            out = []
            for _ in str(args.get("path") or "s+").split(","):
                self.scenes.append({"id": self._new_id(), "name": args.get("name") or ""})
                out.append({"id": self.scenes[-1]["id"], "path": "s%d" % (len(self.scenes) - 1)})
            return pp_fakes.json_response(out if len(out) > 1 else out[0])
        if name == "ppal-delete":
            ids = [i for i in str(args.get("id") or "").split(",") if i]
            kind = args.get("type")
            if kind == "scene":
                self.scenes = [sc for sc in self.scenes if sc["id"] not in ids]
            elif kind == "track":
                self.tracks = [t for t in self.tracks if t.get("id") not in ids]
            if args.get("path"):
                return pp_fakes.json_response({"deletedPath": args["path"], "type": kind})
            out = [{"id": i, "deletedPath": "x"} if i.isdigit() and int(i) < 999000
                   else {"id": i, "ok": False, "detail": "id %s does not exist" % i} for i in ids]
            return pp_fakes.json_response(out if len(out) > 1 else out[0])
        if name in ("ppal-read-clip", "ppal-read-track") and (
                args.get("id") == "999999" or args.get("path") == "t999"):
            return pp_fakes.json_response({**args, "ok": False, "detail": "does not exist"})
        return super().reply(name, args)


class LegacyCleanupTests(unittest.TestCase):
    def run_legacy(self, device):
        return self.run_mode(device, "legacy")

    def run_mode(self, device, mode):
        script = _load_script()

        class Session:
            def __init__(self, *_args):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def initialize(self):
                return None

            async def list_tools(self):
                return types.SimpleNamespace(tools=[
                    types.SimpleNamespace(name=name, description="", inputSchema={})
                    for name in pp_fakes.schemas_for(device.version)])

            async def call_tool(self, name, args):
                device.calls.append((name, dict(args)))
                return pp_fakes.mcp_result(device.reply(name, args))

        @asynccontextmanager
        async def transport(*_args, **_kwargs):
            yield ("r", "w", None)

        fake_mcp = types.ModuleType("mcp")
        fake_mcp.ClientSession = Session
        fake_client = types.ModuleType("mcp.client")
        fake_http = types.ModuleType("mcp.client.streamable_http")
        fake_http.streamablehttp_client = transport
        with tempfile.TemporaryDirectory() as out, \
                patch.dict(sys.modules, {"mcp": fake_mcp, "mcp.client": fake_client,
                                         "mcp.client.streamable_http": fake_http}), \
                patch.object(script, "ROOT", Path(out)), \
                patch.object(sys, "path", [str(ROOT / "mcp_server")] + sys.path), \
                patch("builtins.print"):
            code = asyncio.run(script.main(mode))
            written = sorted(p.name for p in (Path(out) / "tests/fixtures/pp").rglob("*.json"))
        return code, written

    def test_legacy_mode_deletes_the_track_it_created_on_both_builds(self):
        for version in (V22, V23):
            with self.subTest(version=version):
                device = _Device(version)
                before = [t["id"] for t in device.tracks]
                code, written = self.run_legacy(device)
                self.assertEqual(code, 0)
                self.assertEqual([t["id"] for t in device.tracks], before)
                self.assertIn(("ppal-create-track", {"count": 1, "name": "SIDEKICK TRY legacy"}),
                              device.calls)
                self.assertIn("legacy_create_track.json", written)

    def test_v23b_mode_deletes_every_scene_it_caused_and_both_tracks(self):
        # Review 2026-09-30 finding 6a: the RECORDED v23b cleanup predates this
        # (v23b_delete_scene deletes only s8; five silently created scenes
        # stayed). The current script diffs scene ids and deletes them all;
        # this fake covers it until a fresh SCRATCH capture does.
        device = _V23bDevice()
        scenes_before = [sc["id"] for sc in device.scenes]
        tracks_before = [t["id"] for t in device.tracks]
        code, written = self.run_mode(device, "v23b")
        self.assertEqual(code, 0)
        self.assertEqual([sc["id"] for sc in device.scenes], scenes_before)
        self.assertEqual([t["id"] for t in device.tracks], tracks_before)
        scene_deletes = [a for n, a in device.calls
                         if n == "ppal-delete" and a.get("type") == "scene"]
        self.assertEqual(len(scene_deletes), 1)
        # the appended scene plus the five a slot past the last scene created
        self.assertEqual(len(scene_deletes[0]["id"].split(",")), 6)
        self.assertIn("v23b_delete_scene.json", written)
        self.assertIn("v23b_live_set_after_cleanup.json", written)

    def test_v24_mode_deletes_every_scene_and_track_it_created(self):
        # No 2.4 captures yet: the fake speaks the migration guide's shapes so the
        # cleanup contract (diff scene AND track ids, delete all) is pinned before
        # the mode ever runs on the real SCRATCH Set.
        device = _V24Device()
        scenes_before = [sc["id"] for sc in device.scenes]
        tracks_before = [t["id"] for t in device.tracks]
        code, written = self.run_mode(device, "v24")
        self.assertEqual(code, 0)
        self.assertEqual([sc["id"] for sc in device.scenes], scenes_before)
        self.assertEqual([t["id"] for t in device.tracks], tracks_before)
        creates = [a for n, a in device.calls if n == "ppal-create-track"]
        self.assertEqual(creates[0]["path"], "t+,t+")           # the 2.4 batch form
        self.assertEqual(creates[1].get("count"), 1)            # the deprecated form
        for fixture in ("v24_create_tracks_batch.json", "v24_create_session_clip_replaces.json",
                        "v24_delete_clip_missing_id.json", "v24_delete_capture_tracks.json",
                        "v24_live_set_after_cleanup.json"):
            self.assertIn(fixture, written)

    def test_v24_mode_refuses_a_set_not_named_scratch(self):
        device = _V24Device()
        real = device.reply

        def reply(name, args):
            if name == "ppal-live-api":
                return pp_fakes.json_response({"id": "1", "path": "live_set", "results": [
                    {"operation": op, "result": "/Users/USER/Music/EXAMPLEv4.als"}
                    for op in args["operations"]]})
            return real(name, args)
        device.reply = reply
        tracks_before = [t["id"] for t in device.tracks]
        code, written = self.run_mode(device, "v24")
        self.assertEqual(code, 2)
        self.assertEqual([t["id"] for t in device.tracks], tracks_before)
        self.assertFalse(any(n == "ppal-create-track" for n, _ in device.calls))
        self.assertFalse(any(name.startswith("v24_") for name in written))

    def test_v24_mode_stops_before_any_write_when_the_first_read_lacks_the_id_lists(self):
        # the cleanup diffs scene/track ids against the first read: an empty baseline
        # would delete every scene and track in the Set, so the mode must not start
        device = _V24Device()
        real = device.reply

        def reply(name, args):
            if name == "ppal-read-live-set" and "scenes" in (args.get("include") or []):
                return pp_fakes.json_response({"ok": False, "detail": "unknown include"})
            return real(name, args)
        device.reply = reply
        tracks_before = [t["id"] for t in device.tracks]
        scenes_before = [sc["id"] for sc in device.scenes]
        code, written = self.run_mode(device, "v24")
        self.assertEqual(code, 2)
        self.assertEqual([t["id"] for t in device.tracks], tracks_before)
        self.assertEqual([sc["id"] for sc in device.scenes], scenes_before)
        self.assertFalse(any(n in ("ppal-create-track", "ppal-delete") for n, _ in device.calls))

    def test_reply_readers_accept_both_shapes(self):
        script = _load_script()
        self.assertEqual(script._track_index({"trackIndex": 10}), 10)
        self.assertEqual(script._track_index(pp_fakes.captured_json(
            "legacy/legacy_create_track.json", V23)), 10)
        self.assertIsNone(script._track_index({"path": "t10/s0"}))
        self.assertEqual(script._created_ids([{"id": "a", "path": "t1"}, {"id": "b"}]), ["a", "b"])
        self.assertEqual(script._created_ids({"ids": ["c"]}), ["c"])
        self.assertEqual(json.loads(json.dumps(script._created_ids({}))), [])


if __name__ == "__main__":
    unittest.main()
