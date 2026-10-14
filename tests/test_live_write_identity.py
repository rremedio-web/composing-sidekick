import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import patch

from mcp_server import live_write as lw
from mcp_server import pp_session as pp
from mcp_server import finisher, rituals
from tests.pp_fakes import V22, V23, V24, active_version, on_all_versions, schema_strict
from tests import pp_fakes


def _resp(value, warnings=None):
    return pp.parse_tool_response(json.dumps({
        "result": value,
        "warnings": list(warnings or []),
    }))


def _live_api_resp(root_id: str, file_path: str | None):
    return _resp({
        "id": root_id,
        "path": "live_set",
        "results": [{
            "operation": {"type": "getProperty", "property": "file_path"},
            "result": file_path or "",
        }],
    })


def _delete_receipt(version, clip_id, path="t3/s0"):
    """The captured delete receipt of ``version`` for one clip, with the id and
    place of the test's clip: 2.2 ``{deleted, id, type}``
    (2.2.0-rc1/mutate/delete_track.json), 2.3 the same plus ``deletedPath``
    (2.3.0/v23/v23_delete_by_id.json), 2.4 ``{deletedPath, id}`` and nothing
    else (2.4.0/legacy/check_delete_clip_by_id.json)."""
    relative = {V22: "mutate/delete_track.json", V23: "v23/v23_delete_by_id.json",
                V24: "legacy/check_delete_clip_by_id.json"}[version]
    result = dict(pp_fakes.captured_json(relative, version))
    result["id"] = clip_id
    if "type" in result:
        result["type"] = "clip"
    if "deletedPath" in result:
        result["deletedPath"] = path
    return _resp(result)


def _refused_receipt(version, clip_id):
    """The captured reply of a delete that deleted nothing: 2.2 (uncaptured)
    stands in with 2.3's shape; 2.3 ``{deleted: false, id, type}`` + WARNING
    (v23/v23_delete_missing_id.json); 2.4 ``{detail: "nothing to delete", id}``
    and no warning (v24/v24_delete_clip_missing_id.json)."""
    if version == V24:
        return _resp({"detail": "nothing to delete", "id": clip_id})
    return _resp({"deleted": False, "id": clip_id, "type": "clip"},
                 warnings=['WARNING: id "%s" does not exist' % clip_id])


INCARNATION_A = {"pid": 4242, "start_time": "Sat Sep  5 10:00:00 2026"}
INCARNATION_B = {"pid": 9999, "start_time": "Sat Sep  5 11:00:00 2026"}
ROOT_A = "live-root-a"
ROOT_B = "live-root-b"
SAVED_PATH = "/tmp/test.als"


@schema_strict
class _IdentitySession:
    def __init__(self, handlers, *, root_id=ROOT_A, file_path=SAVED_PATH):
        self.handlers = handlers
        self.root_id = root_id
        self.file_path = file_path
        self.capabilities = {"live_api": True, "producer_pal_2_2": True}
        # "2.2.0" stands for any 2.2 build here; a 2.3 twin speaks 2.3.0.
        self.handshake = {
            "pp_version": "2.2.0" if self.pp_version == V22 else self.pp_version,
            "requested": {"notation": "midi-json"},
            "observed": {"notation": None, "format": None},
        }
        self._identity_cache = None

    def cache_set_identity(self, identity: dict) -> None:
        self._identity_cache = dict(identity)

    async def call(self, name, args=None):
        args = dict(args or {})
        if name == "ppal-live-api":
            return _live_api_resp(self.root_id, self.file_path)
        handler = self.handlers.get(name)
        if callable(handler):
            return handler(args)
        if handler is not None:
            return handler
        raise AssertionError("unexpected call %r %r" % (name, args))


@on_all_versions
class LiveWriteIdentityTests(unittest.TestCase):
    def test_arrangement_diagnosis_survives_live_api_identity_refusal(self):
        class _RefusingIdentity(_IdentitySession):
            async def call(self, name, args=None):
                if name == "ppal-live-api":
                    raise RuntimeError("ppal-live-api refused")
                return await super().call(name, args)

        session = _RefusingIdentity({
            "ppal-read-live-set": _resp({"name": "Tonight", "tempo": 120, "tracks": []}),
        })

        @asynccontextmanager
        async def operation(*_args, **_kwargs):
            yield session

        with patch.object(pp, "operation_session", operation):
            result = asyncio.run(lw.read_arrangement_tracks())
            project = finisher.project_from_live_tracks(result)
            diagnosis = finisher.whats_missing(project_dict=project)
            self.assertEqual(diagnosis["project"], "live:Tonight")
            with patch.object(pp, "run_coro_blocking", side_effect=lambda coro: asyncio.run(coro)):
                # Round 3: a refused Live API is not an unsaved Set.
                with self.assertRaisesRegex(ValueError, r"Set identity unavailable \(Live API access off\?\)"):
                    rituals.one_hour_close("live")

        self.assertIsNone(result["file_path"])
        self.assertTrue(any("identity unavailable" in note.lower()
                            for note in result["assumptions"]))

    def test_pp_captured_read_clip_errors_only_confirm_requested_missing_id(self):
        fixture_dir = (Path(__file__).parent / "fixtures" / "pp" / "2.2.0-rc1"
                       / "legacy")
        # Shapes and exact messages are captured from Producer Pal 2.2.0-rc1.
        missing = json.loads((fixture_dir / "legacy_read_clip_missing_id.json").read_text())
        empty = json.loads((fixture_dir / "legacy_read_clip_empty_slot.json").read_text())
        no_locator = json.loads((fixture_dir / "legacy_read_clip_no_locator.json").read_text())
        missing_text = missing["response"]["content"][0]["text"]
        empty_text = empty["response"]["content"][0]["text"]
        no_locator_text = no_locator["response"]["content"][0]["text"]

        self.assertTrue(lw._missing_clip_id_error(missing_text, "999999"))
        self.assertFalse(lw._missing_clip_id_error(missing_text, "999998"))
        self.assertFalse(lw._missing_clip_id_error(empty_text, "999999"))
        self.assertFalse(lw._missing_clip_id_error(no_locator_text, "999999"))

    def test_arrangement_inventory_inherits_verified_parent_track(self):
        track = {'id': '83', 'trackIndex': 5, 'name': 'SIDEKICK AUD'}
        clip = {'id': '100', 'name': 'Promoted', 'arrangementStart': '1|1',
                'arrangementLength': '1bar', 'start': '1|1', 'end': '2|1'}
        listing = _resp({**track, 'arrangementClips': [clip]})
        refs, complete, errors = lw._clip_inventory_from_track(
            track, listing, surface='arrangement')
        self.assertTrue(complete, errors)
        self.assertEqual(refs[0]['path'], 't5')
        self.assertEqual(refs[0]['clip_id'], '100')
        self.assertEqual(refs[0]['track_id'], '83')
        self.assertNotIn('path', clip)

    def test_arrangement_parent_mismatch_cannot_supply_missing_path(self):
        track = {'id': '83', 'trackIndex': 5, 'name': 'SIDEKICK AUD'}
        listing = _resp({'id': 'other', 'trackIndex': 6,
                         'arrangementClips': [{'id': '100', 'name': 'Promoted'}]})
        refs, complete, errors = lw._clip_inventory_from_track(
            track, listing, surface='arrangement')
        self.assertFalse(complete)
        self.assertEqual(refs, [])

    def test_malformed_session_items_do_not_disappear_as_complete_inventory(self):
        for clips in ([{'id': 'missing-path', 'name': 'Present'}], [None], {}):
            with self.subTest(clips=clips):
                self.assertFalse(pp.session_clips_inventory(
                    _resp({'sessionClips': clips}))['complete'])

    def test_released_pp_missing_id_confirms_post_delete_absence(self):
        def missing(_args):
            raise RuntimeError('Producer Pal tool error: Error executing tool '
                               '\'ppal-read-clip\': readClip failed: id "65" does not exist')
        session = _IdentitySession({'ppal-read-clip': missing})
        identity = asyncio.run(lw.read_set_identity(session))
        result = asyncio.run(lw._verify_clip_absent(
            session, '65', {'id': 'jam', 'trackIndex': 4}, identity))
        self.assertEqual(result['status'], 'absent')

    def test_missing_different_id_does_not_prove_target_absence(self):
        def missing(_args):
            raise RuntimeError('readClip failed: id "650" does not exist')
        # another id's missing reply decides nothing: the JAM inventory is
        # consulted, and an incomplete one leaves the outcome unknown
        partial = _resp({'id': 'jam', 'trackIndex': 4, 'sessionClips': []},
                        warnings=['partial session clip read'])
        session = _IdentitySession({'ppal-read-clip': missing, 'ppal-read-track': partial})
        identity = asyncio.run(lw.read_set_identity(session))
        result = asyncio.run(lw._verify_clip_absent(
            session, '65', {'id': 'jam', 'trackIndex': 4}, identity))
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('id "650" does not exist', result['reason'])
        self.assertIn('inventory incomplete', result['reason'])

    def test_missing_original_id_resolves_without_touching_reused_slot(self):
        def missing(_args):
            raise RuntimeError('readClip failed: id "65" does not exist')
        session = _IdentitySession({'ppal-read-clip': missing})
        self.assertIsNone(asyncio.run(lw._resolve_jam_clip(
            session, '65', {'id': 'jam', 'trackIndex': 4})))

    def test_overview_regular_count_includes_group_tracks(self):
        overview = _resp({"regularTrackCount": 2, "tracks": [
            {"id": "g", "trackIndex": 0, "isGroup": True},
            {"id": "t", "trackIndex": 1, "type": "midi"}]})
        self.assertTrue(lw._validate_live_set_overview(overview)[1])

    def test_overview_rejects_fractional_boolean_and_negative_indices(self):
        for value in (1.5, True, -1):
            with self.subTest(value=value):
                overview = _resp({"tracks": [{"id": "t", "trackIndex": value}]})
                self.assertFalse(lw._validate_live_set_overview(overview)[1])

    def test_multiple_local_live_processes_do_not_choose_an_arbitrary_epoch(self):
        with patch.object(pp.shutil, "which", return_value="/usr/bin/available"), \
                patch.object(pp.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout="123\n456\n")):
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                pp._default_resolve_live_process_incarnation()

    def test_same_transport_rechecks_live_root_instead_of_cached_identity(self):
        session = _IdentitySession({})
        first = asyncio.run(lw.read_set_identity(session))
        session.root_id = ROOT_B
        second = asyncio.run(lw.read_set_identity(session))
        self.assertNotEqual(first["runtime_epoch"], second["runtime_epoch"])

    def test_successful_text_without_clip_id_is_not_absence_evidence(self):
        response = _resp({"name": "no clip information was included"})
        self.assertEqual(lw._clip_presence_from_readback(response, "c1"), "unknown")

    def setUp(self):
        self._resolver = pp.resolve_live_process_incarnation
        pp.set_live_process_incarnation_resolver(lambda: dict(INCARNATION_A))

    def tearDown(self):
        pp.set_live_process_incarnation_resolver(None)

    def test_handshake_requested_vs_observed_not_assumed(self):
        assumed = pp.handshake_from_connect(_resp({
            "connected": True,
            "producerPalVersion": "2.2.0-rc1",
            "liveSet": {"tempo": 120},
        }))
        self.assertIsNone(assumed["observed"]["notation"])
        self.assertEqual(assumed["requested"]["notation"], "midi-json")
        self.assertNotIn("format", assumed["requested"])
        pp.validate_handshake(assumed)

    def test_validate_handshake_rejects_uncaptured_pp_21(self):
        handshake = pp.handshake_from_connect(_resp({
            "connected": True,
            "producerPalVersion": "2.1.0",
        }))
        with self.assertRaisesRegex(pp.PPProtocolError, "not verified by captured fixtures yet"):
            pp.validate_handshake(handshake)
        self.assertTrue(handshake["capabilities"]["supports_2_x"])
        self.assertFalse(handshake["capabilities"]["supports_2_2"])

    def test_missing_session_clip_collection_is_incomplete(self):
        inventory = pp.session_clips_inventory(_resp({"id": "jam", "name": "SIDEKICK JAM"}))
        self.assertFalse(inventory["complete"])
        self.assertEqual(inventory["clips"], [])
        with self.assertRaises(pp.LiveWriteVerificationError):
            pp.parse_session_clips(_resp({"id": "jam", "name": "SIDEKICK JAM"}), strict=True)

    def test_path_only_session_clip_normalizes(self):
        clips = pp.parse_session_clips(_resp({
            "sessionClips": [{"path": "t3/s4", "name": "Clip", "id": "c1"}],
        }))
        self.assertEqual(clips, [{"slot": "3/4", "name": "Clip"}])

    def test_read_set_identity_saved_path(self):
        session = _IdentitySession({})
        identity = asyncio.run(lw.read_set_identity(session))
        self.assertEqual(identity["file_path"], SAVED_PATH)
        self.assertEqual(identity["set_id"], SAVED_PATH)
        self.assertEqual(
            identity["runtime_epoch"],
            pp.build_runtime_epoch(ROOT_A, INCARNATION_A),
        )

    def test_read_set_identity_unsaved_uses_transient_epoch(self):
        session = _IdentitySession({}, file_path="")
        identity = asyncio.run(lw.read_set_identity(session))
        self.assertIsNone(identity["file_path"])
        epoch = pp.build_runtime_epoch(ROOT_A, INCARNATION_A)
        self.assertEqual(identity["set_id"], "unsaved:%s" % epoch)
        self.assertEqual(identity["runtime_epoch"], epoch)

    def test_runtime_epoch_stable_across_separate_sessions(self):
        async def _read_twice():
            first = await lw.read_set_identity(_IdentitySession({}))
            second = await lw.read_set_identity(_IdentitySession({}))
            return first, second

        first, second = asyncio.run(_read_twice())
        self.assertEqual(first["runtime_epoch"], second["runtime_epoch"])
        self.assertEqual(first["set_id"], second["set_id"])

    def test_runtime_epoch_invalidates_on_root_or_process_change(self):
        session_same = _IdentitySession({})
        base = asyncio.run(lw.read_set_identity(session_same))
        changed_root = asyncio.run(lw.read_set_identity(
            _IdentitySession({}, root_id=ROOT_B)))
        self.assertNotEqual(base["runtime_epoch"], changed_root["runtime_epoch"])
        pp.set_live_process_incarnation_resolver(lambda: dict(INCARNATION_B))
        changed_proc = asyncio.run(lw.read_set_identity(_IdentitySession({})))
        self.assertNotEqual(base["runtime_epoch"], changed_proc["runtime_epoch"])

    def test_read_set_inventory_marks_missing_track_listing_incomplete(self):
        session = _IdentitySession({
            "ppal-read-live-set": _resp({
                "tracks": [{"trackIndex": 0, "id": "t0", "name": "Pad", "type": "midi"}],
            }),
            "ppal-read-track": _resp({"id": "t0", "name": "Pad", "trackIndex": 0}),
        })
        inventory = asyncio.run(lw.read_set_inventory(session))
        self.assertFalse(inventory["complete"])
        self.assertIn("session clip listing incomplete", "\n".join(inventory["errors"]))

    def test_read_set_inventory_marks_clip_without_id_incomplete(self):
        session = _IdentitySession({
            "ppal-read-live-set": _resp({
                "tracks": [{"trackIndex": 0, "id": "t0", "name": "Pad", "type": "midi"}],
            }),
            "ppal-read-track": _resp({
                "trackIndex": 0,
                "sessionClips": [{"path": "t0/s0", "name": "NoId"}],
                "arrangementClips": [],
            }),
        })
        inventory = asyncio.run(lw.read_set_inventory(session))
        self.assertFalse(inventory["complete"])
        self.assertIn("clip missing id", "\n".join(inventory["errors"]))

    def test_read_set_inventory_marks_overview_truncation_incomplete(self):
        session = _IdentitySession({
            "ppal-read-live-set": _resp(
                {
                    "regularTrackCount": 4,
                    "tracks": [
                        {"trackIndex": 0, "id": "t0", "name": "A", "type": "midi"},
                        {"trackIndex": 1, "id": "t1", "name": "B", "type": "midi"},
                    ],
                },
                warnings=["track list truncated by timeout"],
            ),
        })
        inventory = asyncio.run(lw.read_set_inventory(session))
        self.assertFalse(inventory["complete"])
        self.assertIn("truncated", "\n".join(inventory["errors"]).lower())
        self.assertEqual(inventory["clips"], [])

    def test_read_set_inventory_marks_duplicate_track_index_incomplete(self):
        session = _IdentitySession({
            "ppal-read-live-set": _resp({
                "tracks": [
                    {"trackIndex": 0, "id": "t0", "name": "A", "type": "midi"},
                    {"trackIndex": 0, "id": "t0b", "name": "B", "type": "midi"},
                ],
            }),
        })
        inventory = asyncio.run(lw.read_set_inventory(session))
        self.assertFalse(inventory["complete"])
        self.assertIn("duplicate trackIndex", "\n".join(inventory["errors"]))
        self.assertEqual(inventory["clips"], [])

    def test_read_set_inventory_marks_regular_track_count_mismatch_incomplete(self):
        session = _IdentitySession({
            "ppal-read-live-set": _resp({
                "regularTrackCount": 5,
                "tracks": [
                    {"trackIndex": 0, "id": "t0", "name": "A", "type": "midi"},
                ],
            }),
        })
        inventory = asyncio.run(lw.read_set_inventory(session))
        self.assertFalse(inventory["complete"])
        self.assertIn("regularTrackCount", "\n".join(inventory["errors"]))
        self.assertEqual(inventory["clips"], [])

    def test_delete_jam_clip_fallback_does_not_claim_absence_on_partial_listing(self):
        epoch = pp.build_runtime_epoch(ROOT_A, INCARNATION_A)
        after_delete = {"flag": False}
        version = self.PP_VERSION

        @schema_strict
        class DeleteSession(_IdentitySession):
            async def call(self, name, args=None):
                args = dict(args or {})
                if name == "ppal-live-api":
                    return _live_api_resp(self.root_id, self.file_path)
                if name == "ppal-read-live-set":
                    return _resp({"tracks": [
                        {"trackIndex": 3, "name": lw.JAM_TRACK_NAME, "id": "jam"},
                    ]})
                if name == "ppal-read-clip" and args.get("id") == "clip-a":
                    if after_delete["flag"]:
                        return _resp({"name": "no clip information was included"})
                    return _resp({"id": "clip-a", "path": "t3/s0",
                                  "trackIndex": 3, "trackId": "jam"})
                if name == "ppal-delete":
                    after_delete["flag"] = True
                    # this build's captured delete receipt (see _delete_receipt)
                    return _delete_receipt(version, "clip-a")
                if name == "ppal-read-track":
                    return _resp(
                        {
                            "id": "jam", "trackIndex": 3,
                            "sessionClips": [{"id": "other", "path": "t3/s1",
                                              "name": "OTHER"}],
                        },
                        warnings=["partial session clip read"],
                    )
                raise AssertionError((name, args))

        session = DeleteSession({})

        @asynccontextmanager
        async def fake_operation(*_a, **_k):
            yield session

        with patch.object(pp, "operation_session", fake_operation):
            result = asyncio.run(lw.delete_jam_clip(
                "3/0",
                expected_clip_id="clip-a",
                expected_track_id="jam",
                expected_set_identity={
                    "set_id": SAVED_PATH,
                    "runtime_epoch": epoch,
                    "file_path": SAVED_PATH,
                },
            ))
        self.assertFalse(result["deleted"])
        self.assertIn("inventory incomplete", result["reason"])

    def test_delete_jam_clip_refuses_legacy_slot_only(self):
        result = asyncio.run(lw.delete_jam_clip("3/0"))
        self.assertFalse(result["deleted"])
        self.assertIn("expected_clip_id", result["reason"])

    def test_delete_jam_clip_refuses_missing_track_id(self):
        result = asyncio.run(lw.delete_jam_clip(
            "3/0",
            expected_clip_id="clip-a",
            expected_set_identity={
                "set_id": SAVED_PATH,
                "runtime_epoch": pp.build_runtime_epoch(ROOT_A, INCARNATION_A),
                "file_path": SAVED_PATH,
            },
        ))
        self.assertFalse(result["deleted"])
        self.assertIn("expected_track_id", result["reason"])

    def test_delete_jam_clip_never_deletes_replacement_at_stale_slot(self):
        calls = []
        epoch = pp.build_runtime_epoch(ROOT_A, INCARNATION_A)
        version = self.PP_VERSION

        @schema_strict
        class DeleteSession(_IdentitySession):
            async def call(self, name, args=None):
                args = dict(args or {})
                calls.append((name, dict(args)))
                if name == "ppal-live-api":
                    return _live_api_resp(self.root_id, self.file_path)
                if name == "ppal-read-live-set":
                    return _resp({"tracks": [
                        {"trackIndex": 3, "name": lw.JAM_TRACK_NAME, "id": "jam"},
                    ]})
                if name == "ppal-read-clip" and args.get("id") == "original-id":
                    return _resp({"id": "original-id", "name": "ANSWER",
                                  "path": "t3/s2", "trackIndex": 3, "trackId": "jam"})
                if name == "ppal-delete":
                    return _refused_receipt(version, "original-id")
                if name == "ppal-read-clip" and args.get("id") == "original-id":
                    return _resp({"id": "original-id", "path": "t3/s2", "trackIndex": 3})
                raise AssertionError((name, args))

        session = DeleteSession({})

        @asynccontextmanager
        async def fake_operation(*_a, **_k):
            yield session

        with patch.object(pp, "operation_session", fake_operation):
            result = asyncio.run(lw.delete_jam_clip(
                "3/0",
                expected_clip_id="original-id",
                expected_track_id="jam",
                expected_set_identity={
                    "set_id": SAVED_PATH,
                    "runtime_epoch": epoch,
                    "file_path": SAVED_PATH,
                },
            ))
        self.assertFalse(result["deleted"])
        delete_calls = [c for c in calls if c[0] == "ppal-delete"]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0][1]["id"], "original-id")

    def test_delete_jam_clip_requires_readback_absence(self):
        epoch = pp.build_runtime_epoch(ROOT_A, INCARNATION_A)
        version = self.PP_VERSION
        # the captured missing-id reply of this build, for THIS id
        missing_id = ('Producer Pal tool error: Error: id "clip-a" does not exist'
                      if self.PP_VERSION != V22 else
                      "Producer Pal tool error: Error executing tool 'ppal-read-clip': "
                      'readClip failed: id "clip-a" does not exist')

        @schema_strict
        class DeleteSession(_IdentitySession):
            deleted = False
            readback_error = missing_id
            listing_after = None   # a read-track reply once deleted, or None

            async def call(self, name, args=None):
                args = dict(args or {})
                if name == "ppal-live-api":
                    return _live_api_resp(self.root_id, self.file_path)
                if name == "ppal-read-live-set":
                    return _resp({"tracks": [
                        {"trackIndex": 3, "name": lw.JAM_TRACK_NAME, "id": "jam"},
                    ]})
                if name == "ppal-read-clip" and args.get("id") == "clip-a":
                    if self.deleted:
                        raise RuntimeError(self.readback_error)
                    return _resp({"id": "clip-a", "path": "t3/s0",
                                  "trackIndex": 3, "trackId": "jam"})
                if name == "ppal-read-track" and self.deleted and self.listing_after:
                    return self.listing_after
                if name == "ppal-delete":
                    self.deleted = True
                    # this build's captured delete receipt (see _delete_receipt)
                    return _delete_receipt(version, "clip-a")
                raise AssertionError((name, args))

        def delete_with(session):
            @asynccontextmanager
            async def fake_operation(*_a, **_k):
                yield session

            with patch.object(pp, "operation_session", fake_operation):
                return asyncio.run(lw.delete_jam_clip(
                    "3/0",
                    expected_clip_id="clip-a",
                    expected_track_id="jam",
                    expected_set_identity={
                        "set_id": SAVED_PATH,
                        "runtime_epoch": epoch,
                        "file_path": SAVED_PATH,
                    },
                ))

        result = delete_with(DeleteSession({}))
        self.assertTrue(result["deleted"])
        self.assertEqual(result["clip_id"], "clip-a")
        self.assertEqual(result["after_state"]["method"], "missing_id_reply")

        # an invented "no clip …" error is not that reply: only a complete JAM
        # inventory without the id can then prove absence
        invented = DeleteSession({})
        invented.readback_error = "Producer Pal tool error: no clip at clipId clip-a"
        invented.listing_after = _resp({"trackIndex": 3, "id": "jam", "sessionClips": []})
        result = delete_with(invented)
        self.assertTrue(result["deleted"])
        self.assertEqual(result["after_state"]["method"], "jam_inventory")
        # … and without one the delete stays unverified
        unverified = DeleteSession({})
        unverified.readback_error = invented.readback_error
        unverified.listing_after = _resp({"trackIndex": 3, "id": "jam", "sessionClips": []},
                                         warnings=["partial session clip read"])
        result = delete_with(unverified)
        self.assertFalse(result["deleted"])
        self.assertEqual(result["after_state"]["status"], "unknown")
        self.assertIn("no clip at clipId clip-a", result["reason"])

    def test_delete_jam_clip_empty_readback_is_unknown_not_deleted(self):
        epoch = pp.build_runtime_epoch(ROOT_A, INCARNATION_A)
        after_delete = {"flag": False}
        version = self.PP_VERSION

        @schema_strict
        class DeleteSession(_IdentitySession):
            async def call(self, name, args=None):
                args = dict(args or {})
                if name == "ppal-live-api":
                    return _live_api_resp(self.root_id, self.file_path)
                if name == "ppal-read-live-set":
                    return _resp({"tracks": [
                        {"trackIndex": 3, "name": lw.JAM_TRACK_NAME, "id": "jam"},
                    ]})
                if name == "ppal-read-clip" and args.get("id") == "clip-a":
                    if after_delete["flag"]:
                        return _resp({})
                    return _resp({"id": "clip-a", "path": "t3/s0",
                                  "trackIndex": 3, "trackId": "jam"})
                if name == "ppal-delete":
                    after_delete["flag"] = True
                    # this build's captured delete receipt (see _delete_receipt)
                    return _delete_receipt(version, "clip-a")
                if name == "ppal-read-track":
                    return _resp({"id": "jam", "trackIndex": 3, "name": lw.JAM_TRACK_NAME})
                raise AssertionError((name, args))

        session = DeleteSession({})

        @asynccontextmanager
        async def fake_operation(*_a, **_k):
            yield session

        with patch.object(pp, "operation_session", fake_operation):
            result = asyncio.run(lw.delete_jam_clip(
                "3/0",
                expected_clip_id="clip-a",
                expected_track_id="jam",
                expected_set_identity={
                    "set_id": SAVED_PATH,
                    "runtime_epoch": epoch,
                    "file_path": SAVED_PATH,
                },
            ))
        self.assertFalse(result["deleted"])
        self.assertIn("inventory incomplete", result["reason"])

    def test_augment_write_receipt_verifies_landed_notes(self):
        session = _IdentitySession({
            "ppal-read-live-set": _resp({
                "tracks": [{"trackIndex": 3, "id": "jam", "name": lw.JAM_TRACK_NAME}],
            }),
        })
        verify = _resp({
            "id": "clip-1",
            "path": "t3/s0",
            "trackIndex": 3,
            "name": "ANSWER",
            "notes": [{"p": 62, "t": 0, "d": 1, "v": 80}],
        })
        notes = [{"pitch": 60, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 80}]
        receipt = asyncio.run(lw._augment_write_receipt(
            {"verification_status": "verified"}, session,
            slot="3/0", notes=notes, verify_response=verify,
            expected_clip_id="clip-1",
        ))
        self.assertEqual(receipt.get("identity_status"), "accounting_error")
        self.assertNotIn("clip_ref", receipt)

    def _ready_session(self, **kwargs):
        session = _IdentitySession({}, **kwargs)
        session.capabilities = {"supports_2_x": True, "producer_pal_2_2": True,
                                "supports_2_2": True, "live_api": True,
                                "ppal-live-api": True}
        return session

    def test_scoring_readiness_ready_on_saved_set(self):
        result = asyncio.run(lw.scoring_readiness(self._ready_session()))
        self.assertTrue(result["ready"])
        self.assertIsNone(result["reason"])
        self.assertEqual(result["pp_version"],
                         "2.2.0" if self.PP_VERSION == V22 else self.PP_VERSION)
        self.assertTrue(result["live_api"])
        self.assertTrue(result["set_saved"])
        self.assertEqual(result["identity"]["set_id"], SAVED_PATH)

    def test_scoring_readiness_unsaved_set_is_ready_but_transient(self):
        result = asyncio.run(lw.scoring_readiness(self._ready_session(file_path="")))
        self.assertTrue(result["ready"])
        self.assertFalse(result["set_saved"])
        self.assertTrue(str(result["identity"]["set_id"]).startswith("unsaved:"))

    def test_scoring_readiness_missing_live_api_is_not_ready(self):
        session = self._ready_session()
        session.capabilities = {**session.capabilities,
                                "live_api": False, "ppal-live-api": False}
        result = asyncio.run(lw.scoring_readiness(session))
        self.assertFalse(result["ready"])
        self.assertFalse(result["live_api"])
        self.assertIn("Live API", result["reason"])
        self.assertNotIn("unreachable", result["reason"])

    def test_scoring_readiness_pp_below_22_is_not_ready(self):
        session = self._ready_session()
        session.handshake = {**session.handshake, "pp_version": "2.1.0"}
        session.capabilities = {"supports_2_x": True, "producer_pal_2_2": False,
                                "supports_2_2": False, "live_api": False,
                                "ppal-live-api": False}
        result = asyncio.run(lw.scoring_readiness(session))
        self.assertFalse(result["ready"])
        self.assertIn("2.2", result["reason"])
        self.assertNotIn("unreachable", result["reason"])

    def test_scoring_readiness_identity_failure_names_the_capability(self):
        @schema_strict
        class _NoIdentity(_IdentitySession):
            async def call(self, name, args=None):
                raise RuntimeError("ppal-live-api refused")

        session = _NoIdentity({})
        session.capabilities = {"supports_2_x": True, "producer_pal_2_2": True,
                                "supports_2_2": True, "live_api": True,
                                "ppal-live-api": True}
        result = asyncio.run(lw.scoring_readiness(session))
        self.assertFalse(result["ready"])
        self.assertIn("identity", result["reason"].lower())
        self.assertNotIn("unreachable", result["reason"])

    def test_scoring_readiness_connection_failure_is_unreachable(self):
        @asynccontextmanager
        async def dead_operation(*_a, **_k):
            raise OSError("connection refused")
            yield  # pragma: no cover

        with patch.object(pp, "operation_session", dead_operation):
            result = asyncio.run(lw.scoring_readiness())
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "Producer Pal unreachable")
        self.assertIsNone(result["identity"])

    def test_scoring_readiness_handshake_rejection_names_pp_22(self):
        @asynccontextmanager
        async def rejecting_operation(*_a, **_k):
            raise pp.PPProtocolError("bridge speaks the wrong wire contract")
            yield  # pragma: no cover

        with patch.object(pp, "operation_session", rejecting_operation):
            result = asyncio.run(lw.scoring_readiness())
        self.assertFalse(result["ready"])
        self.assertEqual(
            result["reason"],
            "Producer Pal handshake rejected: bridge speaks the wrong wire contract")
        self.assertNotIn("unreachable", result["reason"])
        self.assertIsNone(result["identity"])

    def test_scoring_readiness_wrong_version_reports_received_version_and_action(self):
        # 2.3.1 has no committed captures (2.3.0 does and is supported).
        handshake = pp.handshake_from_connect(_resp({
            "producerPalVersion": "2.3.1", "capabilities": ["ppal-live-api"]}))
        with self.assertRaises(pp.PPProtocolError) as caught:
            pp.validate_handshake(handshake)
        refusal = caught.exception

        @asynccontextmanager
        async def rejecting_operation(*_a, **_k):
            raise refusal
            yield  # pragma: no cover

        with patch.object(pp, "operation_session", rejecting_operation):
            result = asyncio.run(lw.scoring_readiness())
        self.assertEqual(result["pp_version"], "2.3.1")
        self.assertIn("2.3.1", result["reason"])
        self.assertIn("capture_pp_fixtures.py", result["reason"])

    def test_scoring_readiness_compact_output_reports_json_setting(self):
        refusal = pp.PPProtocolError(
            "Producer Pal JSON response required; switch Producer Pal to JSON output "
            "(device Setup → output format JSON)")

        @asynccontextmanager
        async def rejecting_operation(*_a, **_k):
            raise refusal
            yield  # pragma: no cover

        with patch.object(pp, "operation_session", rejecting_operation):
            result = asyncio.run(lw.scoring_readiness())
        self.assertIn("JSON output", result["reason"])
        self.assertIn("device Setup", result["reason"])
        self.assertNotEqual(result["reason"], "Producer Pal unreachable")

    def test_scoring_readiness_missing_tool_names_tool_and_action(self):
        session = self._ready_session()
        session.capabilities = {**session.capabilities, "live_api": False}
        result = asyncio.run(lw.scoring_readiness(session))
        self.assertIn("ppal-live-api", result["reason"])
        self.assertIn("enable Direct Live API", result["reason"])

    def test_scoring_readiness_group_wrapped_handshake_rejection_names_pp_22(self):
        # the real session raises validate_handshake's PPProtocolError inside
        # the MCP client's anyio TaskGroup, so it arrives wrapped in a
        # single-member ExceptionGroup — the probe must still name the
        # handshake, not "unreachable"
        import anyio

        @asynccontextmanager
        async def group_wrapping_operation(*_a, **_k):
            async with anyio.create_task_group() as tg:
                async def _reject():
                    raise pp.PPProtocolError("bridge speaks the wrong wire contract")
                tg.start_soon(_reject)
            yield  # pragma: no cover

        with patch.object(pp, "operation_session", group_wrapping_operation):
            result = asyncio.run(lw.scoring_readiness())
        self.assertFalse(result["ready"])
        self.assertIn("handshake", result["reason"].lower())
        self.assertNotIn("unreachable", result["reason"])
        self.assertIsNone(result["identity"])

    def test_scoring_readiness_teardown_error_cannot_replace_probe(self):
        ready = self._ready_session()

        @asynccontextmanager
        async def leaking_operation(*_a, **_k):
            yield ready
            raise RuntimeError("transport died on close")

        with patch.object(pp, "operation_session", leaking_operation):
            result = asyncio.run(lw.scoring_readiness())
        self.assertTrue(result["ready"])
        self.assertIsNone(result["reason"])

    def test_pp_transport_headers_include_notation(self):
        self.assertEqual(pp.pp_transport_headers(),
                         {"x-producer-pal-notation": "midi-json"})


if __name__ == "__main__":
    unittest.main()
