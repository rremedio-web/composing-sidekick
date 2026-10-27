"""Behavioral tests for trusted Live Session Ears capability flow."""
from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mcp_server import session_ears
from tests._stage_passthrough import passthrough_listenable_staging
from mcp_server.pp_session import PPResponse
from tests.pp_fakes import V22, schema_strict, on_both_versions
from mcp_server.session_ears import (
    CONTRACT_VERSION,
    DEFAULT_AGY_MODEL,
    SessionEarsError,
    _inventory_from_manifest,
    inventory_session_audio,
    listen_to_takes,
    validate_take_card,
)


def setUpModule():
    from mcp_server import pp_session
    pp_session.set_live_process_incarnation_resolver(lambda: {"pid": 1234, "start_time": 1000.0})


def tearDownModule():
    from mcp_server import pp_session
    pp_session.set_live_process_incarnation_resolver(None)


def _wav_bytes() -> bytes:
    return b"RIFF\x24\x00\x00\x00WAVEfmt "


def _proof_convs(base: str | Path) -> Path:
    """A fresh conversations dir beside a test's tmp root."""
    convs = Path(base) / "convs"
    convs.mkdir(parents=True, exist_ok=True)
    return convs


def _evidenced_runner(fake_run, convs: Path, conversation_id,
                      usage: dict | None = None):
    """Wrap a take fake-runner with proof-of-listening.

    The wrapped runner plants a view_file success record for the staged
    take path parsed out of the prompt directive and returns an agy
    envelope stdout carrying the conversation id, so the harness
    verification passes. ``conversation_id`` may be a callable taking
    the call index (concurrent listens need distinct records).
    """
    from tests.test_agy_listen_verification import (
        _staged_path_from_prompt,
        plant_listen_evidence,
    )
    calls: list = []

    def wrapper(cmd, **kwargs):
        proc = fake_run(cmd, **kwargs)
        index = len(calls)
        calls.append(cmd)
        cid = (conversation_id(index) if callable(conversation_id)
               else conversation_id)
        staged = _staged_path_from_prompt(cmd[cmd.index("--print") + 1])
        plant_listen_evidence(convs, cid, staged)
        return type("Proc", (), {
            "returncode": proc.returncode,
            "stdout": json.dumps({
                "conversation_id": cid,
                "usage": dict(usage) if usage else {"input_tokens": 100},
            }),
            "stderr": getattr(proc, "stderr", ""),
        })()

    return wrapper


@schema_strict
class _FakePP:
    """Minimal Producer Pal session stub with PP-shaped JSON responses."""

    def __init__(self, audio_path: str, set_name: str = "test-set", project_file: str | None = None):
        self.audio_path = audio_path
        self.set_name = set_name
        self.project_file = project_file or f"/tmp/{set_name}.als"
        self.calls: list[tuple[str, dict | None]] = []
        # 2.1.0 on the 2.2-addressing run (the historical stub); the 2.3 twin speaks 2.3.0
        self.handshake = {"pp_version": "2.1.0" if self.pp_version == V22 else self.pp_version,
                          "notation": "midi-json", "format": "json"}
        self.capabilities = {"live_api": True}
        self.lom_props = {
            "warping": False,
            "looping": False,
            "start_marker": 0.0,
            "end_marker": 12.5,
            "loop_start": 0.0,
            "loop_end": 12.5,
            "pitch_coarse": 0,
            "pitch_fine": 0,
            "gain": 1.0,
            "warp_markers": [],
        }

    async def call(self, name: str, args: dict | None = None) -> object:
        from mcp_server.pp_session import PPResponse

        self.calls.append((name, args or {}))
        args = args or {}
        if name == "ppal-live-api":
            path = args.get("path", "")
            operations = args.get("operations", [])
            if path == "live_set":
                results = []
                for op in operations:
                    prop = op.get("property")
                    if prop == "file_path":
                        results.append({"operation": op, "result": self.project_file})
                    else:
                        results.append({"operation": op, "result": None})
                result = {"id": "live_set", "path": path, "results": results}
                return PPResponse(json.dumps({"result": result}), result, [])
            elif path.startswith("id "):
                clip_id = path.split(" ", 1)[1]
                results = []
                for op in operations:
                    prop = op.get("property")
                    val = self.lom_props.get(prop)
                    results.append({"operation": op, "result": val})
                result = {"id": clip_id, "path": path, "results": results}
                return PPResponse(json.dumps({"result": result}), result, [])
            raise AssertionError(f"unexpected live-api path: {path}")
        if name == "ppal-read-live-set":
            result = {
                "setName": self.set_name,
                "regularTrackCount": 2,
                "tracks": [
                    {"trackIndex": 0, "name": "Drums", "type": "midi"},
                    {"trackIndex": 1, "name": "Vox", "type": "audio"},
                ],
            }
            return PPResponse(json.dumps({"result": result}), result, [])
        if name == "ppal-read-track":
            include = args.get("include") or []
            if "session-clips" in include:
                result = {
                    "trackIndex": args["trackIndex"],
                    "sessionClips": [
                        # Shape from read_track_t1_index.json: session clips locate by path.
                        {"path": f"t{args['trackIndex']}/s0", "name": "MIDI loop", "type": "midi"},
                        {"path": f"t{args['trackIndex']}/s1", "name": "Vox take", "type": "audio"},
                    ],
                }
            elif "arrangement-clips" in include:
                result = {
                    "trackIndex": args["trackIndex"],
                    "arrangementClips": [
                        {"id": f"arr-{args['trackIndex']}", "name": "Arr take", "type": "audio"},
                    ],
                }
            else:
                result = {"trackIndex": args["trackIndex"]}
            return PPResponse(json.dumps({"result": result}), result, [])
        if name == "ppal-read-clip":
            if args.get("id"):
                result = {
                    "id": args["id"],
                    "type": "audio",
                    "name": "Arr take",
                    "view": "arrangement",
                    "sampleFile": self.audio_path,
                    "length": 8.0,
                }
            else:
                path = args["path"]
                track_idx, scene_idx = path[1:].split("/s")
                if scene_idx == "0":
                    result = {"id": "midi", "type": "midi", "name": "MIDI loop", "path": f"t{track_idx}/s{scene_idx}"}
                else:
                    result = {
                        "id": "audio",
                        "type": "audio",
                        "name": "Vox take",
                        "view": "session",
                        "trackIndex": int(track_idx),
                        "sceneIndex": int(scene_idx),
                        "sampleFile": self.audio_path,
                        "length": 12.5,
                    }
            return PPResponse(json.dumps({"result": result}), result, [])
        raise AssertionError(f"unexpected PP call: {name} {args}")


@asynccontextmanager
async def _fake_operation(fake: _FakePP):
    yield fake


@on_both_versions
class TestMCPWireContract(unittest.TestCase):
    def test_inventory_accepts_only_track_indices_and_views(self):
        from mcp_server import server

        sig = inspect.signature(server.inventory_session_audio)
        params = list(sig.parameters)
        self.assertEqual(params, ["track_indices", "views"])
        listen_sig = inspect.signature(server.listen_to_takes)
        self.assertEqual(
            list(listen_sig.parameters),
            ["inventory_id", "take_ids", "focus", "model"],
        )

    def test_legacy_raw_take_inventory_rejected(self):
        with self.assertRaises((TypeError, SessionEarsError)):
            inventory_session_audio({"manifest_path": "/tmp/evil.json"})


@on_both_versions
class TestLiveInventory(unittest.TestCase):
    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.audio = Path(self.tmp.name) / "take.wav"
        self.audio.write_bytes(_wav_bytes())
        self.fake = _FakePP(str(self.audio.resolve()))

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def _inventory(self, **kwargs):
        return inventory_session_audio(
            _pp_operation_session=lambda: _fake_operation(self.fake),
            **kwargs,
        )

    def test_default_scope_covers_session_and_arrangement(self):
        env = self._inventory()
        views = {t["view"] for t in env["takes"]}
        self.assertEqual(views, {"session", "arrangement"})
        track_indices = {t["track_index"] for t in env["takes"]}
        self.assertEqual(track_indices, {0, 1})
        called_views = [
            args.get("include")
            for name, args in self.fake.calls
            if name == "ppal-read-track"
        ]
        self.assertIn(["session-clips"], called_views)
        self.assertIn(["arrangement-clips"], called_views)

    def test_inventory_is_sanitized(self):
        env = self._inventory()
        blob = json.dumps(env)
        self.assertNotIn("sampleFile", blob)
        self.assertNotIn(str(self.audio), blob)
        self.assertNotIn("/", blob.split("inventory_id")[0])  # no absolute paths in prefix
        for take in env["takes"]:
            self.assertIn("take_id", take)
            self.assertNotIn("path", take)
            self.assertIn("track_name", take)

    def test_subset_track_indices(self):
        env = self._inventory(track_indices=[1], views=["session"])
        self.assertTrue(all(t["track_index"] == 1 for t in env["takes"]))
        self.assertTrue(all(t["view"] == "session" for t in env["takes"]))

    def test_midi_clips_excluded(self):
        env = self._inventory(track_indices=[1], views=["session"])
        names = {t["clip_name"] for t in env["takes"]}
        self.assertNotIn("MIDI loop", names)


@on_both_versions
class TestCapabilitySecurity(unittest.TestCase):
    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.audio = Path(self.tmp.name) / "take.wav"
        self.audio.write_bytes(_wav_bytes())
        self.unrelated = Path(self.tmp.name) / "other.wav"
        self.unrelated.write_bytes(_wav_bytes())
        self.fake = _FakePP(str(self.audio.resolve()))
        self.env = inventory_session_audio(
            track_indices=[1],
            views=["session"],
            _pp_operation_session=lambda: _fake_operation(self.fake),
        )
        self.take_id = self.env["takes"][0]["take_id"]

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def _fake_runner(self, cmd, **kwargs):
        prompt = cmd[-1]
        output = Path(
            __import__("re").search(
                r"write the same JSON object to:\n([^\n]+)", prompt
            ).group(1)
        )
        take_id = __import__("re").search(r"- take_id: ([^\n]+)", prompt).group(1)
        card = {
            "take_id": take_id,
            "track": "Vox",
            "clip_name": "Vox take",
            "role_guess": "lead",
            "vibe": "dry close vocal",
            "register": None,
            "density": "sparse",
            "pitch_claims": [],
            "timestamps": ["00:00"],
            "contract_version": CONTRACT_VERSION,
        }
        output.write_text(json.dumps(card), encoding="utf-8")
        return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def test_unrelated_wav_not_selectable(self):
        with passthrough_listenable_staging(), \
             mock.patch.object(session_ears, "_which", return_value="/fake/agy"):
            with self.assertRaises(SessionEarsError):
                listen_to_takes(
                    self.env["inventory_id"],
                    ["take-not-in-registry"],
                    runner=self._fake_runner,
                    store_root=self.tmp.name,
                )

    def test_unknown_take_id_fails_closed(self):
        with self.assertRaises(SessionEarsError):
            listen_to_takes(
                self.env["inventory_id"],
                ["take-deadbeefdeadbeef"],
            )

    def test_expired_inventory_fails_closed(self):
        session_ears._expire_inventory_for_tests(self.env["inventory_id"])
        with self.assertRaises(SessionEarsError):
            listen_to_takes(self.env["inventory_id"], [self.take_id])

    def test_replayed_inventory_fails_closed(self):
        convs = _proof_convs(self.tmp.name)
        with passthrough_listenable_staging(), \
             mock.patch.object(session_ears, "_which", return_value="/fake/agy"):
            listen_to_takes(
                self.env["inventory_id"],
                [self.take_id],
                runner=_evidenced_runner(
                    self._fake_runner, convs, "conv-replayed"),
                store_root=self.tmp.name,
                conversations_dir=convs,
            )
        with self.assertRaises(SessionEarsError):
            listen_to_takes(
                self.env["inventory_id"],
                [self.take_id],
                runner=self._fake_runner,
                store_root=self.tmp.name,
            )

    def test_cross_inventory_take_id_fails_closed(self):
        other = inventory_session_audio(
            track_indices=[1],
            views=["session"],
            _pp_operation_session=lambda: _fake_operation(self.fake),
        )
        with self.assertRaises(SessionEarsError):
            listen_to_takes(other["inventory_id"], [self.take_id])

    def test_nonzero_returncode_reports_error_not_raise(self):
        """Listener returns non-zero exit code even with valid card staged."""
        def failing_runner(cmd, **kwargs):
            prompt = cmd[-1]
            output = Path(
                __import__("re").search(
                    r"write the same JSON object to:\n([^\n]+)", prompt
                ).group(1)
            )
            take_id = __import__("re").search(r"- take_id: ([^\n]+)", prompt).group(1)
            card = {
                "take_id": take_id,
                "track": "Vox",
                "clip_name": "Vox take",
                "role_guess": "lead",
                "vibe": "dry close vocal",
                "register": None,
                "density": "sparse",
                "pitch_claims": [],
                "timestamps": ["00:00"],
                "contract_version": CONTRACT_VERSION,
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(card), encoding="utf-8")
            return type("Proc", (), {"returncode": 1, "stdout": "", "stderr": "error: listener crashed\n"})()

        with passthrough_listenable_staging(), \
             mock.patch.object(session_ears, "_which", return_value="/fake/agy"):
            heard = listen_to_takes(
                self.env["inventory_id"],
                [self.take_id],
                runner=failing_runner,
                store_root=self.tmp.name,
            )
        self.assertEqual(heard["cards"], [])
        self.assertEqual(len(heard["errors"]), 1)
        self.assertEqual(heard["errors"][0]["take_id"], self.take_id)
        self.assertIn("returncode 1", heard["errors"][0]["reason"].lower())
        self.assertIn(self.take_id, heard["errors"][0]["reason"])
        persisted = list(Path(self.tmp.name).rglob("take_cards.json"))
        self.assertEqual(persisted, [], "no cards should be persisted on failure")

    def test_zero_returncode_succeeds(self):
        """Listener returns zero exit code with valid card - should succeed."""
        convs = _proof_convs(self.tmp.name)
        with passthrough_listenable_staging(), \
             mock.patch.object(session_ears, "_which", return_value="/fake/agy"):
            heard = listen_to_takes(
                self.env["inventory_id"],
                [self.take_id],
                runner=_evidenced_runner(
                    self._fake_runner, convs, "conv-zero"),
                store_root=self.tmp.name,
                conversations_dir=convs,
            )
        cards = heard["cards"]
        self.assertEqual(heard["errors"], [])
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["take_id"], self.take_id)
        persisted = list(Path(self.tmp.name).rglob("take_cards.json"))
        self.assertEqual(len(persisted), 1, "cards should be persisted on success")

    def test_concurrent_claim_single_winner(self):
        barrier = threading.Barrier(2)
        results: list[str] = []

        # Both patches stay outside the threads: entering/exiting a mock
        # per attempt races (one thread's restore unpatches the other),
        # which the real staging copy this stand-in replaces never did.
        convs = _proof_convs(self.tmp.name)
        evidenced = _evidenced_runner(
            self._fake_runner, convs, lambda n: "conv-race-%d" % n)

        def attempt():
            try:
                barrier.wait(timeout=2)
                listen_to_takes(
                    self.env["inventory_id"],
                    [self.take_id],
                    runner=evidenced,
                    store_root=self.tmp.name,
                    conversations_dir=convs,
                )
                results.append("ok")
            except SessionEarsError:
                results.append("denied")

        with passthrough_listenable_staging(), \
                mock.patch.object(session_ears, "_which",
                                  return_value="/fake/agy"):
            threads = [threading.Thread(target=attempt) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
        self.assertEqual(sorted(results), ["denied", "ok"])

    def test_single_use_even_when_staging_fails(self):
        original_stage = session_ears._stage_audio

        def boom(take, staging_dir):
            raise SessionEarsError("simulated staging failure")

        session_ears._stage_audio = boom
        try:
            with passthrough_listenable_staging(), \
             mock.patch.object(session_ears, "_which", return_value="/fake/agy"):
                heard = listen_to_takes(
                    self.env["inventory_id"],
                    [self.take_id],
                    runner=self._fake_runner,
                    store_root=self.tmp.name,
                )
            self.assertEqual(heard["cards"], [])
            self.assertEqual(len(heard["errors"]), 1)
            self.assertEqual(heard["errors"][0]["take_id"], self.take_id)
            # The attempted take was consumed: retrying it fails closed.
            with self.assertRaises(SessionEarsError):
                listen_to_takes(
                    self.env["inventory_id"],
                    [self.take_id],
                    runner=self._fake_runner,
                    store_root=self.tmp.name,
                )
        finally:
            session_ears._stage_audio = original_stage


@on_both_versions
class TestManifestHelper(unittest.TestCase):
    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.trusted = self.root / "trusted"
        self.trusted.mkdir()
        self.audio = self.trusted / "take.wav"
        self.audio.write_bytes(_wav_bytes())

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def test_rejects_paths_outside_trusted_root(self):
        outside = self.root / "outside.wav"
        outside.write_bytes(_wav_bytes())
        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "session_id": "offline",
                    "takes": [
                        {"track": "Vox", "clip_name": "take 1", "path": str(outside)},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(SessionEarsError):
            _inventory_from_manifest(manifest, self.trusted)

    def test_manifest_read_error_redacts_secret_paths(self):
        secret = str((self.root / "trusted" / "secret-manifest.json").resolve())
        Path(secret).write_text("{}", encoding="utf-8")

        original_read_text = Path.read_text

        def _raise_oserror(self, *args, **kwargs):
            if str(self) == secret:
                raise OSError(2, "No such file", secret)
            return original_read_text(self, *args, **kwargs)

        with mock.patch.object(Path, "read_text", _raise_oserror):
            with self.assertRaises(SessionEarsError) as ctx:
                _inventory_from_manifest(secret, self.trusted)
        message = str(ctx.exception)
        self.assertEqual(message, "audio manifest unreadable")
        self.assertNotIn(secret, message)
        self.assertIsInstance(ctx.exception.__cause__, OSError)

    def test_accepts_paths_inside_trusted_root(self):
        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "session_id": "offline",
                    "takes": [
                        {"track": "Vox", "clip_name": "take 1", "path": str(self.audio)},
                    ],
                }
            ),
            encoding="utf-8",
        )
        env = _inventory_from_manifest(manifest, self.trusted)
        self.assertEqual(env["session_id"], "offline")
        self.assertEqual(len(env["takes"]), 1)


@on_both_versions
class TestTakeFocusSet(unittest.TestCase):
    """M1: session ears has its own take-card focus set (full/groove/
    melody/composition) — it must not import the v3 card overlays."""

    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.audio = Path(self.tmp.name) / "take.wav"
        self.audio.write_bytes(_wav_bytes())
        self.fake = _FakePP(str(self.audio.resolve()))
        self.env = inventory_session_audio(
            track_indices=[1],
            views=["session"],
            _pp_operation_session=lambda: _fake_operation(self.fake),
        )
        self.take_id = self.env["takes"][0]["take_id"]

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def _fake_runner(self, cmd, **kwargs):
        import re as _re

        prompt = cmd[-1]
        output = Path(
            _re.search(
                r"write the same JSON object to:\n([^\n]+)", prompt
            ).group(1)
        )
        take_id = _re.search(r"- take_id: ([^\n]+)", prompt).group(1)
        card = {
            "take_id": take_id,
            "track": "Vox",
            "clip_name": "Vox take",
            "role_guess": "lead",
            "vibe": "dry close vocal",
            "register": None,
            "density": "sparse",
            "pitch_claims": [],
            "timestamps": ["00:00"],
            "contract_version": CONTRACT_VERSION,
        }
        output.write_text(json.dumps(card), encoding="utf-8")
        return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def test_focus_vocabulary_is_take_cards_not_v3_cards(self):
        self.assertEqual(
            set(session_ears.TAKE_FOCUS_OVERLAYS),
            {"full", "groove", "melody", "composition"})
        self.assertEqual(session_ears.TAKE_FOCUS_OVERLAYS["full"], "")
        self.assertFalse(hasattr(session_ears, "FOCUS_OVERLAYS"))

    def test_composition_focus_listens(self):
        convs = _proof_convs(self.tmp.name)
        with passthrough_listenable_staging(), \
             mock.patch.object(session_ears, "_which", return_value="/fake/agy"):
            heard = listen_to_takes(
                self.env["inventory_id"],
                [self.take_id],
                focus="composition",
                runner=_evidenced_runner(
                    self._fake_runner, convs, "conv-focus"),
                store_root=self.tmp.name,
                conversations_dir=convs,
            )
        cards = heard["cards"]
        self.assertEqual(heard["errors"], [])
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["take_id"], self.take_id)

    def test_listened_card_carries_content_signature(self):
        # Review 12h finding 1: the hearing is bound to what the clip played.
        convs = _proof_convs(self.tmp.name)
        with passthrough_listenable_staging(), \
             mock.patch.object(session_ears, "_which", return_value="/fake/agy"):
            heard = listen_to_takes(
                self.env["inventory_id"], [self.take_id], focus="full",
                runner=_evidenced_runner(self._fake_runner, convs, "conv-sig"),
                store_root=self.tmp.name, conversations_dir=convs)
        card = heard["cards"][0]
        expected = session_ears._content_signature(
            {"sampleFile": str(self.audio.resolve())})
        self.assertEqual(card["live_clip_id"], "audio")
        self.assertEqual(card["content_signature"]["sample"], expected["sample"])
        self.assertNotIn(str(self.audio), json.dumps(card))
        read_clip = [args for name, args in self.fake.calls if name == "ppal-read-clip"]
        self.assertTrue(all("timing" in args["include"] for args in read_clip))

    def test_card_focus_harmony_is_not_a_take_focus(self):
        with self.assertRaises(ValueError):
            listen_to_takes(
                self.env["inventory_id"],
                [self.take_id],
                focus="harmony",
                runner=self._fake_runner,
                store_root=self.tmp.name,
            )


@on_both_versions
class TestPersistenceAndCleanup(unittest.TestCase):
    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.trusted = self.root / "trusted"
        self.trusted.mkdir()
        self.audio = self.trusted / "take.wav"
        self.audio.write_bytes(_wav_bytes())
        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "session_id": "persist-session",
                    "takes": [
                        {"track": "Vox", "clip_name": "take 1", "path": str(self.audio)},
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.env = _inventory_from_manifest(manifest, self.trusted)
        self.take_id = self.env["takes"][0]["take_id"]

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def test_staging_cleaned_on_failure(self):
        seen: list[Path] = []

        def failing_runner(cmd, **kwargs):
            staging = Path(cmd[cmd.index("--add-dir") + 1])
            seen.append(staging)
            raise OSError("simulated agy failure")

        with passthrough_listenable_staging(), \
             mock.patch.object(session_ears, "_which", return_value="/fake/agy"):
            heard = listen_to_takes(
                self.env["inventory_id"],
                [self.take_id],
                runner=failing_runner,
                store_root=self.root / "store",
            )
        self.assertEqual(heard["cards"], [])
        self.assertEqual(len(heard["errors"]), 1)
        self.assertEqual(heard["errors"][0]["take_id"], self.take_id)
        self.assertTrue(seen)
        self.assertFalse(seen[0].exists())

    def test_manifest_card_persistence_uses_public_session_token(self):
        def fake_runner(cmd, **kwargs):
            prompt = cmd[-1]
            output = Path(
                __import__("re").search(
                    r"write the same JSON object to:\n([^\n]+)", prompt
                ).group(1)
            )
            card = {
                "take_id": self.take_id,
                "track": "Vox",
                "clip_name": "take 1",
                "role_guess": "lead",
                "vibe": "dry close vocal",
                "register": None,
                "density": "sparse",
                "pitch_claims": [],
                "timestamps": ["00:00"],
                "contract_version": CONTRACT_VERSION,
            }
            output.write_text(json.dumps(card), encoding="utf-8")
            return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        store = self.root / "store"
        convs = _proof_convs(self.root)
        with passthrough_listenable_staging(), \
             mock.patch.object(session_ears, "_which", return_value="/fake/agy"):
            heard = listen_to_takes(
                self.env["inventory_id"],
                [self.take_id],
                runner=_evidenced_runner(
                    fake_runner, convs, "conv-persist"),
                store_root=store,
                conversations_dir=convs,
            )
        self.assertEqual(heard["errors"], [])
        cards = heard["cards"]
        validate_take_card(cards[0])
        public_id = session_ears._public_set_identity(
            {"set_id": "persist-session", "runtime_epoch": "manifest"})["set_id"]
        path = store / "session_ears" / session_ears._session_id(
            public_id) / "take_cards.json"
        self.assertTrue(path.is_file())
        loaded = session_ears.load_latest_take_cards(
            session_id=public_id, store_root=store, max_age_s=None,
            expected_set_identity={"set_id": "persist-session", "runtime_epoch": "manifest"}
        )
        self.assertEqual(loaded[0]["take_id"], self.take_id)


@on_both_versions
class TestLiveUnavailable(unittest.TestCase):
    def setUp(self):
        session_ears._reset_registry_for_tests()

    def tearDown(self):
        session_ears._reset_registry_for_tests()

    def test_live_inventory_unavailable_without_pp(self):
        @asynccontextmanager
        async def broken():
            raise RuntimeError("Producer Pal unreachable")
            yield  # pragma: no cover

        with self.assertRaises(SessionEarsError) as ctx:
            inventory_session_audio(_pp_operation_session=broken)
        self.assertIn("live_inventory_unavailable", str(ctx.exception).lower())


@schema_strict
class _FakePPNoSetIdentity(_FakePP):
    def __init__(self, audio_path: str):
        super().__init__(audio_path, set_name="", project_file="")
        self.capabilities = {}

    async def call(self, name: str, args: dict | None = None) -> object:
        from mcp_server.pp_session import PPResponse

        if name == "ppal-read-live-set":
            result = {
                "regularTrackCount": 2,
                "tracks": [
                    {"trackIndex": 0, "name": "Nested-Track-Name", "type": "midi"},
                    {"trackIndex": 1, "name": "Vox", "type": "audio"},
                ],
            }
            return PPResponse(json.dumps({"result": result}), result, [])
        return await super().call(name, args)


@schema_strict
class _FakePPPathBearingError:
    handshake = {"pp_version": "2.1.0"}

    async def call(self, name: str, args: dict | None = None) -> object:
        raise RuntimeError("failed opening /Users/secret/Projects/audio/take.wav")


@on_both_versions
class TestFixRound1PathRedaction(unittest.TestCase):
    def setUp(self):
        session_ears._reset_registry_for_tests()

    def tearDown(self):
        session_ears._reset_registry_for_tests()

    def test_inventory_error_redacts_path_bearing_pp_failure(self):
        @asynccontextmanager
        async def broken():
            yield _FakePPPathBearingError()

        with self.assertRaises(SessionEarsError) as ctx:
            inventory_session_audio(_pp_operation_session=broken)
        message = str(ctx.exception)
        self.assertIn("live_inventory_unavailable", message.lower())
        self.assertNotIn("/Users/secret/Projects/audio/take.wav", message)

    def test_listen_staging_error_redacts_path_bearing_oserror(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        audio = Path(tmp.name) / "take.wav"
        audio.write_bytes(_wav_bytes())
        env = inventory_session_audio(
            track_indices=[1],
            views=["session"],
            _pp_operation_session=lambda: _fake_operation(_FakePP(str(audio.resolve()))),
        )
        take_id = env["takes"][0]["take_id"]

        def boom(take, staging_dir):
            raise OSError(2, "No such file", "/Users/secret/Projects/audio/take.wav")

        original = session_ears._stage_audio
        session_ears._stage_audio = boom
        self.addCleanup(setattr, session_ears, "_stage_audio", original)
        with passthrough_listenable_staging(), \
             mock.patch.object(session_ears, "_which", return_value="/fake/agy"):
            heard = listen_to_takes(
                env["inventory_id"],
                [take_id],
                runner=lambda *a, **k: None,
                store_root=tmp.name,
            )
        self.assertEqual(heard["cards"], [])
        self.assertEqual(len(heard["errors"]), 1)
        message = heard["errors"][0]["reason"]
        self.assertIn(take_id, message)
        self.assertNotIn("/Users/secret/Projects/audio/take.wav", message)


@on_both_versions
class TestFixRound1LiveSetIdentity(unittest.TestCase):
    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.audio = Path(self.tmp.name) / "take.wav"
        self.audio.write_bytes(_wav_bytes())

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def test_missing_live_set_identity_fails_closed(self):
        fake = _FakePPNoSetIdentity(str(self.audio.resolve()))
        with self.assertRaises(SessionEarsError) as ctx:
            inventory_session_audio(
                _pp_operation_session=lambda: _fake_operation(fake),
            )
        self.assertIn("live_set_identity_unavailable", str(ctx.exception).lower())

    def test_nested_track_name_not_used_as_session_id(self):
        fake = _FakePPNoSetIdentity(str(self.audio.resolve()))
        with self.assertRaises(SessionEarsError):
            inventory_session_audio(
                _pp_operation_session=lambda: _fake_operation(fake),
            )


@on_both_versions
class TestFixRound1TrackIndices(unittest.TestCase):
    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.audio = Path(self.tmp.name) / "take.wav"
        self.audio.write_bytes(_wav_bytes())
        self.fake = _FakePP(str(self.audio.resolve()))

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def test_out_of_range_track_index_rejected(self):
        with self.assertRaises(SessionEarsError):
            inventory_session_audio(
                track_indices=[99],
                _pp_operation_session=lambda: _fake_operation(self.fake),
            )
        bad_calls = [
            args for name, args in self.fake.calls
            if name == "ppal-read-track" and args.get("trackIndex") == 99
        ]
        self.assertEqual(bad_calls, [])

    def test_negative_track_index_rejected(self):
        with self.assertRaises(SessionEarsError):
            inventory_session_audio(
                track_indices=[-1],
                _pp_operation_session=lambda: _fake_operation(self.fake),
            )


@on_both_versions
class TestFixRound1MCPAnnotations(unittest.TestCase):
    def test_views_annotation_is_literal_enum(self):
        from mcp_server import server

        hints = server.inventory_session_audio.__annotations__
        views_hint = str(hints.get("views", ""))
        self.assertIn("Literal", views_hint)
        self.assertIn("session", views_hint)
        self.assertIn("arrangement", views_hint)

    def test_listen_model_default_matches_wire_contract(self):
        from mcp_server import server

        sig = inspect.signature(server.listen_to_takes)
        model_param = sig.parameters["model"]
        self.assertEqual(model_param.default, DEFAULT_AGY_MODEL)


@on_both_versions
class TestFinalFixIdentity(unittest.TestCase):
    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.audio = self.root / "take.wav"
        self.audio.write_bytes(_wav_bytes())
        self.fake = _FakePP(str(self.audio.resolve()), set_name="My Live Set")

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def _fake_runner(self, cmd, **kwargs):
        prompt = cmd[-1]
        output = Path(
            __import__("re").search(
                r"write the same JSON object to:\n([^\n]+)", prompt
            ).group(1)
        )
        take_id = __import__("re").search(r"- take_id: ([^\n]+)", prompt).group(1)
        card = {
            "take_id": take_id,
            "track": "Vox",
            "clip_name": "Vox take",
            "role_guess": "lead",
            "vibe": "dry close vocal",
            "register": None,
            "density": "sparse",
            "pitch_claims": [],
            "timestamps": ["00:00"],
            "contract_version": CONTRACT_VERSION,
        }
        output.write_text(json.dumps(card), encoding="utf-8")
        return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def test_inventory_session_id_is_live_set_identity(self):
        env = inventory_session_audio(
            track_indices=[1],
            views=["session"],
            _pp_operation_session=lambda: _fake_operation(self.fake),
        )
        self.assertEqual(env["session_id"], "My-Live-Set")

    def test_listen_persists_ledger_session_with_live_set_provenance(self):
        from mcp_server import live_write
        current = session_ears._run_async(live_write.read_set_identity(self.fake))
        store = self.root / "store"
        store.mkdir(parents=True)
        (store / "session_ledger.json").write_text(
            json.dumps(
                {
                    "sessions": [
                        {"id": "s-ledger", "closed_at": "2026-08-16T00:00:00Z"},
                        {"id": "s-ledger", "set_identity": current},
                    ]
                }
            ),
            encoding="utf-8",
        )
        env = inventory_session_audio(
            track_indices=[1],
            views=["session"],
            _pp_operation_session=lambda: _fake_operation(self.fake),
        )
        take_id = env["takes"][0]["take_id"]
        convs = _proof_convs(self.root)
        with passthrough_listenable_staging(), \
             mock.patch.object(session_ears, "_which", return_value="/fake/agy"):
            heard = listen_to_takes(
                env["inventory_id"],
                [take_id],
                runner=_evidenced_runner(
                    self._fake_runner, convs, "conv-ledger"),
                store_root=store,
                conversations_dir=convs,
            )
        self.assertEqual(heard["errors"], [])
        cards = heard["cards"]
        self.assertEqual(cards[0]["session_id"], "s-ledger")
        self.assertEqual(cards[0]["live_set_session_id"], "My-Live-Set")
        with mock.patch.object(session_ears, "_default_read_set_identity", return_value=current):
            loaded = session_ears.load_latest_take_cards(store_root=store, max_age_s=None)
            self.assertEqual(loaded[0]["take_id"], take_id)
            folded = session_ears.fold_take_cards(loaded, {"sections": []})
        self.assertEqual(folded["session_take_cards"][0]["session_id"], "s-ledger")
        self.assertEqual(
            folded["session_take_cards"][0]["live_set_session_id"], "My-Live-Set"
        )


@on_both_versions
class TestEffortSuffixHeuristic(unittest.TestCase):
    """Unit tests for Antigravity effort-suffix heuristic in command builders."""

    def test_build_agy_cmd_includes_effort_for_plain_model(self):
        """Plain model name (no -high/-medium/-low suffix) should include --effort high."""
        cmd = session_ears._build_agy_cmd(
            agy="/fake/agy",
            audio_dir=Path("/tmp/audio"),
            audio_name="take.wav",
            take={
                "take_id": "take-1",
                "track": "Vox",
                "clip_name": "take 1",
                "path": "/tmp/take.wav",
            },
            focus="full",
            model="gemini-3.7-flash",
            timeout_s=300,
            output_path=Path("/tmp/output.json"),
        )
        self.assertIn("--effort", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")

    def test_build_agy_cmd_excludes_effort_for_high_suffix(self):
        """Model ending with -high should NOT include --effort flag."""
        cmd = session_ears._build_agy_cmd(
            agy="/fake/agy",
            audio_dir=Path("/tmp/audio"),
            audio_name="take.wav",
            take={
                "take_id": "take-1",
                "track": "Vox",
                "clip_name": "take 1",
                "path": "/tmp/take.wav",
            },
            focus="full",
            model="gemini-3.7-flash-high",
            timeout_s=300,
            output_path=Path("/tmp/output.json"),
        )
        self.assertNotIn("--effort", cmd)

    def test_build_agy_cmd_excludes_effort_for_medium_suffix(self):
        """Model ending with -medium should NOT include --effort flag."""
        cmd = session_ears._build_agy_cmd(
            agy="/fake/agy",
            audio_dir=Path("/tmp/audio"),
            audio_name="take.wav",
            take={
                "take_id": "take-1",
                "track": "Vox",
                "clip_name": "take 1",
                "path": "/tmp/take.wav",
            },
            focus="full",
            model="gemini-3.7-flash-medium",
            timeout_s=300,
            output_path=Path("/tmp/output.json"),
        )
        self.assertNotIn("--effort", cmd)

    def test_build_agy_cmd_excludes_effort_for_low_suffix(self):
        """Model ending with -low should NOT include --effort flag."""
        cmd = session_ears._build_agy_cmd(
            agy="/fake/agy",
            audio_dir=Path("/tmp/audio"),
            audio_name="take.wav",
            take={
                "take_id": "take-1",
                "track": "Vox",
                "clip_name": "take 1",
                "path": "/tmp/take.wav",
            },
            focus="full",
            model="gemini-3.7-flash-low",
            timeout_s=300,
            output_path=Path("/tmp/output.json"),
        )
        self.assertNotIn("--effort", cmd)

    def test_build_agy_cmd_includes_effort_for_explicit_high_model(self):
        """Explicit --effort high should be added for plain model names."""
        cmd = session_ears._build_agy_cmd(
            agy="/fake/agy",
            audio_dir=Path("/tmp/audio"),
            audio_name="take.wav",
            take={
                "take_id": "take-1",
                "track": "Vox",
                "clip_name": "take 1",
                "path": "/tmp/take.wav",
            },
            focus="full",
            model="explicit-model-name",
            timeout_s=300,
            output_path=Path("/tmp/output.json"),
        )
        self.assertIn("--effort", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")


@on_both_versions
class TestFinalFixRedaction(unittest.TestCase):
    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.audio = self.root / "take.wav"
        self.audio.write_bytes(_wav_bytes())

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def _valid_card(self, **overrides):
        card = {
            "take_id": "take-1",
            "track": "Vox",
            "clip_name": "take 1",
            "role_guess": "lead",
            "vibe": "dry close vocal",
            "register": None,
            "density": "sparse",
            "pitch_claims": [],
            "timestamps": ["00:00"],
            "contract_version": CONTRACT_VERSION,
            "session_id": "s-ledger",
            "live_set_session_id": "My-Live-Set",
            "source": "live",
        }
        card.update(overrides)
        return card

    def test_rejects_unknown_card_key(self):
        with self.assertRaises(ValueError):
            validate_take_card(self._valid_card(source_path="/tmp/evil.wav"))

    def test_rejects_top_level_absolute_path_field(self):
        with self.assertRaises(ValueError):
            validate_take_card(
                self._valid_card(notes={"path": "/Users/secret/take.wav"})
            )

    def test_rejects_embedded_absolute_path_in_string(self):
        with self.assertRaises(ValueError):
            validate_take_card(
                self._valid_card(vibe="heard at /Users/secret/take.wav")
            )

    def test_rejects_windows_absolute_path(self):
        with self.assertRaises(ValueError):
            validate_take_card(
                self._valid_card(vibe=r"heard at C:\Users\secret\take.wav")
            )

    def test_public_descriptor_rejects_path_bearing_pp_metadata(self):
        take = session_ears._InternalTake(
            take_id="take-1",
            track_index=1,
            track_name="Vox",
            view="session",
            clip_locator="1/1",
            clip_name="take",
            duration_s=1.0,
            bars=None,
            path="/Users/secret/take.wav",
        )
        take.track_name = "/Users/secret/track"
        with self.assertRaises(SessionEarsError):
            session_ears._public_take_descriptor(take)

    def test_listen_response_and_persistence_redact_paths(self):
        trusted = self.root / "trusted"
        trusted.mkdir()
        shutil.copyfile(self.audio, trusted / "take.wav")
        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "session_id": "My-Live-Set",
                    "takes": [
                        {
                            "track": "Vox",
                            "clip_name": "take 1",
                            "path": str(trusted / "take.wav"),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        env = _inventory_from_manifest(manifest, trusted)
        take_id = env["takes"][0]["take_id"]

        def fake_runner(cmd, **kwargs):
            prompt = cmd[-1]
            output = Path(
                __import__("re").search(
                    r"write the same JSON object to:\n([^\n]+)", prompt
                ).group(1)
            )
            card = {
                "take_id": take_id,
                "track": "Vox",
                "clip_name": "take 1",
                "role_guess": "lead",
                "vibe": "dry close vocal",
                "register": None,
                "density": "sparse",
                "pitch_claims": [],
                "timestamps": ["00:00"],
                "contract_version": CONTRACT_VERSION,
            }
            output.write_text(json.dumps(card), encoding="utf-8")
            return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        store = self.root / "store"
        convs = _proof_convs(self.root)
        with passthrough_listenable_staging(), \
             mock.patch.object(session_ears, "_which", return_value="/fake/agy"):
            heard = listen_to_takes(
                env["inventory_id"],
                [take_id],
                runner=_evidenced_runner(
                    fake_runner, convs, "conv-redact"),
                store_root=store,
                conversations_dir=convs,
            )
        self.assertEqual(heard["errors"], [])
        cards = heard["cards"]
        blob = json.dumps(cards)
        self.assertNotIn("/Users/", blob)
        self.assertNotIn("trusted", blob)
        path = store / "session_ears" / "My-Live-Set" / "take_cards.json"
        if not path.is_file():
            path = next(store.rglob("take_cards.json"))
        persisted = path.read_text(encoding="utf-8")
        self.assertNotIn("/Users/", persisted)
        self.assertNotIn("trusted", persisted)


@on_both_versions
class TestFinalFixPPErrors(unittest.TestCase):
    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.audio = Path(self.tmp.name) / "take.wav"
        self.audio.write_bytes(_wav_bytes())

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def test_empty_slot_read_clip_is_skipped_not_fatal(self):
        @schema_strict
        class _EmptySlotPP(_FakePP):
            async def call(self, name: str, args: dict | None = None) -> object:
                if name == "ppal-read-clip":
                    raise RuntimeError("no clip at trackIndex 1, sceneIndex 1")
                return await super().call(name, args)

        env = inventory_session_audio(
            track_indices=[1],
            views=["session"],
            _pp_operation_session=lambda: _fake_operation(_EmptySlotPP(str(self.audio.resolve()))),
        )
        self.assertEqual(len(env["takes"]), 0)

    def test_path_bearing_read_clip_error_fails_inventory(self):
        @schema_strict
        class _BrokenClipPP(_FakePP):
            async def call(self, name: str, args: dict | None = None) -> object:
                if name == "ppal-read-clip":
                    raise RuntimeError(
                        "Producer Pal tool error: failed opening "
                        "/Users/secret/Projects/audio/take.wav"
                    )
                return await super().call(name, args)

        with self.assertRaises(SessionEarsError) as ctx:
            inventory_session_audio(
                track_indices=[1],
                views=["session"],
                _pp_operation_session=lambda: _fake_operation(
                    _BrokenClipPP(str(self.audio.resolve()))
                ),
            )
        message = str(ctx.exception)
        self.assertIn("live_inventory_unavailable", message.lower())
        self.assertNotIn("/Users/secret/Projects/audio/take.wav", message)


@on_both_versions
class TestFinalFixHardening(unittest.TestCase):
    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.audio = Path(self.tmp.name) / "take.wav"
        self.audio.write_bytes(_wav_bytes())
        self.fake = _FakePP(str(self.audio.resolve()))

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def test_bool_track_index_rejected(self):
        with self.assertRaises(SessionEarsError):
            inventory_session_audio(
                track_indices=[True],
                _pp_operation_session=lambda: _fake_operation(self.fake),
            )

    def test_empty_views_rejected(self):
        with self.assertRaises(SessionEarsError):
            inventory_session_audio(
                views=[],
                _pp_operation_session=lambda: _fake_operation(self.fake),
            )

    def test_registry_purges_expired_on_register(self):
        env1 = inventory_session_audio(
            track_indices=[1],
            views=["session"],
            _pp_operation_session=lambda: _fake_operation(self.fake),
        )
        session_ears._expire_inventory_for_tests(env1["inventory_id"])
        env2 = inventory_session_audio(
            track_indices=[1],
            views=["session"],
            _pp_operation_session=lambda: _fake_operation(self.fake),
        )
        with session_ears._registry_lock:
            self.assertNotIn(env1["inventory_id"], session_ears._registry)
            self.assertIn(env2["inventory_id"], session_ears._registry)


@on_both_versions
class TestHostileRegistryAndPaths(unittest.TestCase):
    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.trusted = self.root / "trusted"
        self.trusted.mkdir()
        self.audio = self.trusted / "take.wav"
        self.audio.write_bytes(_wav_bytes())
        self.fake = _FakePP(str(self.audio.resolve()))

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def test_two_live_inventories_remain_on_register(self):
        env_a = inventory_session_audio(
            track_indices=[1],
            views=["session"],
            _pp_operation_session=lambda: _fake_operation(self.fake),
        )
        env_b = inventory_session_audio(
            track_indices=[1],
            views=["session"],
            _pp_operation_session=lambda: _fake_operation(self.fake),
        )
        with session_ears._registry_lock:
            self.assertIn(env_a["inventory_id"], session_ears._registry)
            self.assertIn(env_b["inventory_id"], session_ears._registry)
        self.assertNotEqual(env_a["inventory_id"], env_b["inventory_id"])

    def test_expiration_still_purges_on_register(self):
        env1 = inventory_session_audio(
            track_indices=[1],
            views=["session"],
            _pp_operation_session=lambda: _fake_operation(self.fake),
        )
        session_ears._expire_inventory_for_tests(env1["inventory_id"])
        env2 = inventory_session_audio(
            track_indices=[1],
            views=["session"],
            _pp_operation_session=lambda: _fake_operation(self.fake),
        )
        with session_ears._registry_lock:
            self.assertNotIn(env1["inventory_id"], session_ears._registry)
            self.assertIn(env2["inventory_id"], session_ears._registry)

    def test_unc_backslash_rejected_in_manifest(self):
        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "session_id": "offline",
                    "takes": [
                        {
                            "track": "Vox",
                            "clip_name": "take 1",
                            "path": r"\\server\share\take.wav",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(SessionEarsError) as ctx:
            _inventory_from_manifest(manifest, self.trusted)
        message = str(ctx.exception)
        self.assertIn("UNC", message)
        self.assertNotIn("server", message.lower())

    def test_unc_forward_rejected_in_manifest(self):
        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "session_id": "offline",
                    "takes": [
                        {
                            "track": "Vox",
                            "clip_name": "take 1",
                            "path": "//server/share/take.wav",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(SessionEarsError) as ctx:
            _inventory_from_manifest(manifest, self.trusted)
        message = str(ctx.exception)
        self.assertIn("UNC", message)
        self.assertNotIn("server", message.lower())

    def test_public_inventory_never_exposes_manifest_paths(self):
        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "session_id": "offline",
                    "takes": [
                        {
                            "track": "Vox",
                            "clip_name": "take 1",
                            "path": str(self.audio),
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        env = _inventory_from_manifest(manifest, self.trusted)
        blob = json.dumps(env)
        self.assertNotIn(str(self.audio), blob)
        self.assertNotIn("trusted", blob)
        self.assertNotIn("manifest", blob.lower())


@on_both_versions
class TestTruthfulAudioRegionsAndIdentity(unittest.TestCase):
    """Tests for F5/F6: truthful audio region analysis, warp validation, and Set identity binding."""

    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wav_path = self.root / "sample_6s.wav"
        self._write_valid_wav(self.wav_path, duration_s=6.0)

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def _write_valid_wav(self, path: Path, duration_s: float = 6.0, sample_rate: int = 22050):
        import wave
        import struct
        import math
        n_frames = int(duration_s * sample_rate)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            data = bytearray()
            for i in range(n_frames):
                val = int(32767.0 * 0.5 * math.sin(2.0 * math.pi * 440.0 * i / sample_rate))
                data.extend(struct.pack("<h", val))
            w.writeframes(data)

    def test_path_only_session_audio_f5(self):
        """F5: Path-only t0/s1 clip descriptors must be discovered without slot or trackIndex/sceneIndex."""
        @schema_strict
        class _PathOnlyFake(_FakePP):
            def __init__(self, wav_path):
                super().__init__(str(wav_path), "test-set")
                self.read_clip_args = None

            async def call(self, name, args):
                if name == "ppal-read-track":
                    result = {
                        "trackIndex": 0,
                        "sessionClips": [
                            {"path": "t0/s1", "name": "Path Vox", "type": "audio"},
                        ],
                    }
                    return PPResponse(json.dumps({"result": result}), result, [])
                if name == "ppal-read-clip":
                    self.read_clip_args = args
                    result = {
                        "id": "path-clip-1",
                        "name": "Path Vox",
                        "type": "audio",
                        "sampleFile": str(self.audio_path),
                        "length": 6.0,
                    }
                    return PPResponse(json.dumps({"result": result}), result, [])
                return await super().call(name, args)

        fake = _PathOnlyFake(self.wav_path)
        env = inventory_session_audio(
            track_indices=[0],
            views=["session"],
            _pp_operation_session=lambda: _fake_operation(fake),
        )
        self.assertEqual(len(env["takes"]), 1)
        take = env["takes"][0]
        self.assertEqual(take["clip_locator"], "0/1")
        self.assertEqual(take["clip_name"], "Path Vox")
        self.assertEqual(fake.read_clip_args["path"], "t0/s1")

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg on PATH")
    def test_real_wav_late_region_and_crop_args_f6(self):
        """F6: Audio staging crops the true late region [2.0, 5.0] rather than defaulting to [0, 45]."""
        take = {
            "take_id": "take-late",
            "path": str(self.wav_path),
            "analyzed_start_s": 2.0,
            "analyzed_end_s": 5.0,
            "length": 6.0,
        }
        staged_dir = self.root / "staged"
        staged_dir.mkdir()
        staged_path, duration = session_ears._stage_audio(take, staged_dir)
        self.assertTrue(staged_path.is_file())
        self.assertAlmostEqual(duration, 3.0, places=1)

        # Verify crop arguments passed to ffmpeg_to_wav
        with mock.patch("mcp_server.session_ears.ffmpeg_to_wav") as mock_ffmpeg:
            session_ears._stage_audio(take, staged_dir)
            mock_ffmpeg.assert_called_once_with(
                Path(str(self.wav_path)).resolve(),
                staged_dir / "take.wav",
                2.0,
                5.0,
            )

    def test_warped_unsupported_no_subprocess_or_model(self):
        """F6: Warped clip with unsupported/missing warp markers sets capture_required and aborts listen without subprocess."""
        @schema_strict
        class _WarpedFake(_FakePP):
            def __init__(self, wav_path):
                super().__init__(str(wav_path), "test-set")
                self.lom_props = {
                    "warping": True,
                    "looping": True,
                    "start_marker": 0.0,
                    "end_marker": 6.0,
                    "loop_start": 0.0,
                    "loop_end": 4.0,
                    "pitch_coarse": 0,
                    "pitch_fine": 0,
                    "gain": 1.0,
                    "warp_markers": None,
                }

            async def call(self, name, args):
                if name == "ppal-read-track":
                    result = {
                        "trackIndex": 0,
                        "sessionClips": [{"slot": "0/0", "name": "Warped Loop", "type": "audio"}],
                    }
                    return PPResponse(json.dumps({"result": result}), result, [])
                if name == "ppal-read-clip":
                    result = {
                        "id": "warped-clip-1",
                        "name": "Warped Loop",
                        "type": "audio",
                        "sampleFile": str(self.audio_path),
                        "length": 6.0,
                        "warping": True,
                        "looping": True,
                        "start_marker": 0.0,
                        "loop_end": 4.0,
                    }
                    return PPResponse(json.dumps({"result": result}), result, [])
                return await super().call(name, args)

        fake = _WarpedFake(self.wav_path)
        env = inventory_session_audio(
            track_indices=[0],
            views=["session"],
            _pp_operation_session=lambda: _fake_operation(fake),
        )
        self.assertEqual(len(env["takes"]), 1)
        take = env["takes"][0]
        self.assertTrue(take.get("capture_required"))
        self.assertIn("capture_loop", take.get("guidance", ""))

        # Calling listen_to_takes must raise SessionEarsError and NEVER invoke runner
        runner_mock = mock.MagicMock()
        with passthrough_listenable_staging(), \
             mock.patch.object(session_ears, "_which", return_value="/fake/agy"):
            with self.assertRaises(SessionEarsError) as ctx:
                listen_to_takes(
                    env["inventory_id"],
                    [take["take_id"]],
                    runner=runner_mock,
                    store_root=self.root / "store",
                )
            self.assertIn("requires capture", str(ctx.exception))
            self.assertEqual(runner_mock.call_count, 0)

    def test_exact_source_range_metadata_in_descriptor_and_cards(self):
        """F6: Analyzed range, transformations, and limitations are reflected in descriptors, prompt, and cards."""
        clip_props = {
            "warping": False,
            "looping": True,
            "start_marker": 1.5,
            "loop_end": 4.5,
            "pitch_coarse": 3,
            "gain_db": -2.0,
        }
        region = session_ears._analyze_audio_region(clip_props, duration_s=6.0)
        self.assertEqual(region["source_kind"], "source_region")
        self.assertEqual(region["analyzed_start_s"], 1.5)
        self.assertEqual(region["analyzed_end_s"], 4.5)
        self.assertEqual(region["analyzed_duration_s"], 3.0)
        self.assertEqual(region["transformations"]["pitch_coarse"], 3)
        self.assertFalse(region["capture_required"])

        take = session_ears._InternalTake(
            take_id="take-meta",
            track_index=0,
            track_name="Lead",
            view="session",
            clip_locator="0/0",
            clip_name="Lead Vox",
            duration_s=6.0,
            bars=None,
            path=str(self.wav_path),
            source_kind=region["source_kind"],
            analyzed_start_s=region["analyzed_start_s"],
            analyzed_end_s=region["analyzed_end_s"],
            analyzed_duration_s=region["analyzed_duration_s"],
            transformations=region["transformations"],
            limitations=region["limitations"],
        )
        desc = session_ears._public_take_descriptor(take)
        self.assertEqual(desc["source_kind"], "source_region")
        self.assertEqual(desc["analyzed_start_s"], 1.5)
        self.assertEqual(desc["analyzed_end_s"], 4.5)
        self.assertEqual(desc["transformations"]["pitch_coarse"], 3)

        cmd = session_ears._build_agy_cmd(
            agy="/fake/agy",
            audio_dir=self.root,
            audio_name="take.wav",
            take=desc,
            focus="full",
            model="gemini-3.7-flash-medium",
            timeout_s=300,
            output_path=self.root / "take_card.json",
        )
        prompt = cmd[-1]
        self.assertIn("- source_kind: source_region", prompt)
        self.assertIn("1.50s to 4.50s", prompt)
        self.assertIn('"pitch_coarse": 3', prompt)

    def test_same_filename_different_set_rejection(self):
        """F6: Live Set identity mismatch between inventory and listen triggers rejection before staging."""
        fake = _FakePP(str(self.wav_path))
        env = inventory_session_audio(
            track_indices=[1],
            views=["session"],
            _pp_operation_session=lambda: _fake_operation(fake),
            _read_set_identity=lambda s=None: {"set_id": "Project-A", "runtime_epoch": "e1", "file_path": "/A.als"},
        )
        take_id = env["takes"][0]["take_id"]

        runner_mock = mock.MagicMock()
        with passthrough_listenable_staging(), \
             mock.patch.object(session_ears, "_which", return_value="/fake/agy"):
            with self.assertRaises(SessionEarsError) as ctx:
                listen_to_takes(
                    env["inventory_id"],
                    [take_id],
                    runner=runner_mock,
                    store_root=self.root / "store",
                    _read_set_identity=lambda: {"set_id": "Project-B", "runtime_epoch": "e2", "file_path": "/B.als"},
                )
            self.assertIn("live set identity changed", str(ctx.exception))
            self.assertEqual(runner_mock.call_count, 0)

    def test_unrelated_open_ledger_cannot_hijack_cards(self):
        """F6: An unrelated open ledger session from a different Set cannot hijack card persistence."""
        store = self.root / "store_hijack"
        store.mkdir(parents=True)
        (store / "session_ledger.json").write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "id": "open-session-from-other-set",
                            "set_identity": {"set_id": "Other-Project-ALS"},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        fake = _FakePP(str(self.wav_path))
        env = inventory_session_audio(
            track_indices=[1],
            views=["session"],
            _pp_operation_session=lambda: _fake_operation(fake),
            _read_set_identity=lambda s=None: {"set_id": "My-Current-Set", "runtime_epoch": "curr", "file_path": "/Current.als"},
        )
        take_id = env["takes"][0]["take_id"]

        def fake_runner(cmd, **kwargs):
            prompt = cmd[-1]
            t_id = re.search(r"- take_id: ([^\n]+)", prompt).group(1)
            t_track = re.search(r"- track: ([^\n]+)", prompt).group(1)
            t_clip = re.search(r"- clip_name: ([^\n]+)", prompt).group(1)
            card = {
                "take_id": t_id,
                "track": t_track,
                "clip_name": t_clip,
                "role_guess": "lead",
                "vibe": "clear presence",
                "register": None,
                "density": "sparse",
                "pitch_claims": [],
                "timestamps": ["00:01"],
                "contract_version": CONTRACT_VERSION,
            }
            out_path = Path(re.search(r"write the same JSON object to:\n([^\n]+)", prompt).group(1))
            out_path.write_text(json.dumps(card), encoding="utf-8")
            return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        convs = _proof_convs(self.root)
        with passthrough_listenable_staging(), \
             mock.patch.object(session_ears, "_which", return_value="/fake/agy"):
            heard = listen_to_takes(
                env["inventory_id"],
                [take_id],
                runner=_evidenced_runner(
                    fake_runner, convs, "conv-hijack"),
                store_root=store,
                conversations_dir=convs,
                _read_set_identity=lambda: {"set_id": "My-Current-Set", "runtime_epoch": "curr", "file_path": "/Current.als"},
            )
            cards = heard["cards"]
            self.assertEqual(heard["errors"], [])

        # Must NOT be saved under open-session-from-other-set!
        self.assertNotEqual(cards[0]["session_id"], "open-session-from-other-set")
        self.assertFalse((store / "session_ears" / "open-session-from-other-set" / "take_cards.json").exists())
        # Live cards persist under the Set-identity key (public token + epoch).
        keydir = session_ears._set_card_dirname(session_ears._public_set_identity(
            {"set_id": "My-Current-Set", "runtime_epoch": "curr"}))
        self.assertTrue((store / "session_ears" / keydir / "take_cards.json").exists())


@on_both_versions
class TestLoadLatestTakeCardsLiveContact(unittest.TestCase):
    """load_latest_take_cards must not touch Live when no card could qualify."""

    IDENT = {"set_id": "My-Current-Set", "runtime_epoch": "curr",
             "file_path": "/Current.als"}

    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "store"

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def _reader(self, calls: list):
        def _read():
            calls.append(1)
            return dict(self.IDENT)
        return _read

    def _write_cards(self, sid: str = "s1") -> Path:
        card = {
            "take_id": "take-1",
            "track": "Vox",
            "clip_name": "take 1",
            "role_guess": "lead",
            "vibe": "dry close vocal",
            "register": None,
            "density": "sparse",
            "pitch_claims": [],
            "timestamps": ["00:01"],
            "contract_version": CONTRACT_VERSION,
            "set_identity": dict(self.IDENT),
        }
        session_ears.write_take_cards(
            [card], session_id=sid, store_root=self.store,
            set_identity=dict(self.IDENT),
        )
        # Live cards persist under the Set-identity key, not the session id.
        keydir = session_ears._set_card_dirname(
            session_ears._public_set_identity(dict(self.IDENT)))
        return self.store / "session_ears" / keydir / "take_cards.json"

    def test_no_fresh_card_makes_no_live_contact(self):
        # No session_ears dir at all.
        calls: list = []
        self.assertEqual(
            session_ears.load_latest_take_cards(
                store_root=self.store, _read_set_identity=self._reader(calls)), [])
        self.assertEqual(calls, [])
        # Empty session_ears dir, no cards anywhere.
        (self.store / "session_ears").mkdir(parents=True)
        self.assertEqual(
            session_ears.load_latest_take_cards(
                store_root=self.store, _read_set_identity=self._reader(calls)), [])
        self.assertEqual(calls, [])
        # Only a stale card (heard_at older than max_age), pinned and unpinned.
        path = self._write_cards()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["cards"][0]["heard_at"] = time.time() - 7200.0
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(
            session_ears.load_latest_take_cards(
                store_root=self.store, max_age_s=3600.0,
                _read_set_identity=self._reader(calls)), [])
        self.assertEqual(calls, [])
        self.assertEqual(
            session_ears.load_latest_take_cards(
                session_id="s1", store_root=self.store, max_age_s=3600.0,
                _read_set_identity=self._reader(calls)), [])
        self.assertEqual(calls, [])

    def test_fresh_card_resolves_identity_once(self):
        self._write_cards()
        calls: list = []
        loaded = session_ears.load_latest_take_cards(
            session_id="s1", store_root=self.store, max_age_s=None,
            _read_set_identity=self._reader(calls))
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["take_id"], "take-1")
        self.assertEqual(calls, [1])

    def test_corrupt_card_raises_and_candidate_json_is_parsed_once(self):
        path = self._write_cards()
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(SessionEarsError):
            session_ears.load_latest_take_cards(
                store_root=self.store, max_age_s=None,
                _read_set_identity=self._reader([]))

        path.unlink()
        path = self._write_cards()
        original_loads = json.loads
        parses = []

        def counting_loads(value, *args, **kwargs):
            if isinstance(value, str) and value.startswith("{"):
                parses.append(value)
            return original_loads(value, *args, **kwargs)

        with mock.patch.object(session_ears.json, "loads", side_effect=counting_loads):
            cards = session_ears.load_latest_take_cards(
                store_root=self.store, max_age_s=None,
                _read_set_identity=self._reader([]))
        self.assertEqual(len(cards), 1)
        self.assertEqual(len(parses), 1)

    def test_nonmatching_set_key_falls_back_to_legacy_file_when_pinned(self):
        source = self._write_cards()
        good = json.loads(source.read_text(encoding="utf-8"))["cards"][0]
        keyed_dir = session_ears._set_card_dirname(
            session_ears._public_set_identity(self.IDENT))
        keyed = self.store / "session_ears" / keyed_dir / "take_cards.json"
        wrong = dict(good)
        wrong["set_identity"] = session_ears._public_set_identity(
            {"set_id": "Different Set", "runtime_epoch": "other"})
        keyed.write_text(json.dumps({"session_id": "ignored",
                                     "set_identity": wrong["set_identity"],
                                     "cards": [wrong]}), encoding="utf-8")

        legacy_sid = session_ears._session_id(self.IDENT["set_id"])
        legacy = self.store / "session_ears" / legacy_sid / "take_cards.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({
            "session_id": legacy_sid,
            "set_identity": session_ears._public_set_identity(self.IDENT),
            "cards": [good],
        }), encoding="utf-8")
        with mock.patch.object(session_ears, "_resolve_current_set_identity",
                               return_value=dict(self.IDENT)):
            loaded = session_ears.load_latest_take_cards(
                session_id="pinned-new-session", store_root=self.store,
                max_age_s=None)
        self.assertEqual([card["take_id"] for card in loaded], ["take-1"])

    def test_empty_session_id_behaves_as_unpinned(self):
        # "" must follow the same truthiness the resolution below uses:
        # it reads like "no pin", not like a pin on a "current" file.
        self._write_cards(sid="My-Current-Set")
        calls: list = []
        unpinned = session_ears.load_latest_take_cards(
            store_root=self.store, max_age_s=None,
            _read_set_identity=self._reader(calls))
        self.assertEqual(len(unpinned), 1)
        empty = session_ears.load_latest_take_cards(
            session_id="", store_root=self.store, max_age_s=None,
            _read_set_identity=self._reader(calls))
        self.assertEqual(empty, unpinned)


if __name__ == "__main__":
    unittest.main()
