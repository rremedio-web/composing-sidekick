"""Lane B3 item 3: take-card persistence keyed by verified Set identity.

The card file is keyed by the verified Set identity (public set token +
runtime_epoch) instead of the volatile ledger session id; the ledger session
id rides INSIDE each card for measurement. A new listen batch merges by
take_id (newest wins) instead of replacing the file.
"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import ledger as _ledger
from mcp_server import session_ears
from tests.pp_fakes import on_both_versions, schema_strict

IDENT = {"set_id": "persist-live-set", "runtime_epoch": "epoch-9"}


def _card(take_id: str, vibe: str = "close and grainy") -> dict:
    return {
        "take_id": take_id,
        "track": "Vocal",
        "clip_name": "take 1",
        "role_guess": "lead",
        "vibe": vibe,
        "register": None,
        "density": "sparse",
        "pitch_claims": [],
        "timestamps": ["00:00"],
        "contract_version": session_ears.CONTRACT_VERSION,
        "set_identity": session_ears._public_set_identity(IDENT),
        # Persisted shape: write_take_cards stamps heard_at before storage.
        "heard_at": time.time(),
    }


def _keydir() -> str:
    return session_ears._set_card_dirname(
        session_ears._public_set_identity(IDENT))



# Staging transcodes through ffmpeg; these tests use fake RIFF bytes and test
# persistence, not staging — pass the staged copy through for the module.
_STAGING = None


def setUpModule():
    global _STAGING
    from tests._stage_passthrough import passthrough_listenable_staging
    _STAGING = passthrough_listenable_staging()
    _STAGING.__enter__()


def tearDownModule():
    _STAGING.__exit__(None, None, None)

class TestSetKeyedPersistence(unittest.TestCase):
    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory(prefix="persist-")
        self.store = Path(self.tmp.name) / "store"

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def _load(self, include_history=False):
        with patch.object(session_ears, "_default_read_set_identity",
                          return_value=dict(IDENT)):
            return session_ears.load_latest_take_cards(
                store_root=self.store, max_age_s=None,
                include_history=include_history)

    def test_write_keys_by_set_identity_not_ledger_session(self):
        session_ears.write_take_cards(
            [_card("take-a")], session_id="ledger-s1",
            store_root=self.store, set_identity=dict(IDENT))
        keyed = self.store / "session_ears" / _keydir() / "take_cards.json"
        self.assertTrue(keyed.is_file())
        self.assertFalse(
            (self.store / "session_ears" / "ledger-s1").exists())
        loaded = self._load()
        self.assertEqual([c["take_id"] for c in loaded], ["take-a"])
        # The ledger session id rides inside the card for measurement.
        self.assertEqual(loaded[0]["session_id"], "ledger-s1")

    def test_ledger_fallback_session_id_uses_public_set_token(self):
        identity = {"set_id": "/Users/x/Music/My Song.als", "runtime_epoch": "epoch"}
        with patch.object(session_ears, "_ledger_open_session_id", return_value=None):
            sid = session_ears._resolve_persistence_session_id(
                "Live Set", self.store, set_identity=identity)
        self.assertEqual(sid, session_ears._public_set_identity(identity)["set_id"])
        self.assertNotIn(".als", sid)

    def test_manifest_identity_fallback_never_returns_live_path(self):
        identity = {
            "set_id": "/Users/x/Music/My Song.als",
            "runtime_epoch": "manifest",
        }
        with patch.object(session_ears, "_ledger_open_session_id", return_value=None):
            sid = session_ears._resolve_persistence_session_id(
                identity["set_id"], self.store, set_identity=identity)
        self.assertEqual(sid, session_ears._public_set_identity(identity)["set_id"])
        self.assertNotIn(".als", sid)

    def test_second_batch_merges_by_take_id_newest_wins(self):
        session_ears.write_take_cards(
            [_card("take-a", vibe="first pass")], session_id="ledger-s1",
            store_root=self.store, set_identity=dict(IDENT))
        session_ears.write_take_cards(
            [_card("take-b")], session_id="ledger-s2",
            store_root=self.store, set_identity=dict(IDENT))
        self.assertEqual(
            sorted(c["take_id"] for c in self._load()), ["take-a", "take-b"])
        session_ears.write_take_cards(
            [_card("take-a", vibe="second pass")], session_id="ledger-s2",
            store_root=self.store, set_identity=dict(IDENT))
        loaded = {c["take_id"]: c for c in self._load()}
        self.assertEqual(sorted(loaded), ["take-a", "take-b"])
        self.assertEqual(loaded["take-a"]["vibe"], "second pass")

    def test_capture_without_live_clip_id_merges_with_inventory_card(self):
        capture = _card("capture-id", vibe="captured")
        capture.update(track="SIDEKICK EARS", clip_locator="5/2")
        inventory = _card("inventory-id", vibe="inventory")
        inventory.update(track="SIDEKICK EARS", clip_locator="5/2",
                         live_clip_id="77")

        session_ears.write_take_cards(
            [capture], session_id="ledger-s1", store_root=self.store,
            set_identity=dict(IDENT))
        session_ears.write_take_cards(
            [inventory], session_id="ledger-s2", store_root=self.store,
            set_identity=dict(IDENT))

        loaded = self._load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["take_id"], "inventory-id")
        self.assertEqual(loaded[0]["vibe"], "inventory")

    def test_replaced_slot_card_is_historical_and_not_folded(self):
        # Shape mirrors a persisted card emitted by listen_to_takes.
        old = _card("old", vibe="bright vocal")
        old.update(track="Vocal", clip_locator="0/0", live_clip_id="101")
        new = _card("new", vibe="low drone")
        new.update(track="Vocal", clip_locator="0/0", live_clip_id="202")
        session_ears.write_take_cards([old], session_id="ledger-s1",
                                      store_root=self.store, set_identity=dict(IDENT))
        session_ears.write_take_cards([new], session_id="ledger-s2",
                                      store_root=self.store, set_identity=dict(IDENT))

        history = self._load(include_history=True)
        self.assertEqual(len(history), 2)
        self.assertEqual(len(self._load()), 1)
        prior = next(card for card in history if card["take_id"] == "old")
        self.assertEqual(prior["superseded_by"], "new")
        folded = session_ears.fold_take_cards(
            history, {"sections": []}, expected_set_identity=dict(IDENT),
            _resolved_identity=dict(IDENT))
        self.assertEqual([card["vibe"] for card in folded["session_take_cards"]],
                         ["low drone"])

    def test_moved_clip_and_renamed_track_keep_clip_card(self):
        # Shape mirrors successive persisted listen_to_takes cards for one Live clip.
        first = _card("same-clip", vibe="heard once")
        first.update(track="Vocal", clip_locator="0/0", live_clip_id="101")
        moved = _card("same-clip", vibe="heard after move")
        moved.update(track="Lead", clip_locator="2/3", live_clip_id="101")
        session_ears.write_take_cards([first], session_id="ledger-s1",
                                      store_root=self.store, set_identity=dict(IDENT))
        session_ears.write_take_cards([moved], session_id="ledger-s2",
                                      store_root=self.store, set_identity=dict(IDENT))
        loaded = self._load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["take_id"], "same-clip")
        self.assertEqual(loaded[0]["track"], "Lead")
        self.assertEqual(loaded[0]["clip_locator"], "2/3")

    def test_fold_uses_current_clip_snapshot_when_present(self):
        card = _card("stale-listening", vibe="removed sound")
        card.update(track="Vocal", clip_locator="0/0", live_clip_id="101")
        folded = session_ears.fold_take_cards(
            [card], {"sections": [], "current_clips": [{"clip_id": "202"}]},
            expected_set_identity=dict(IDENT), _resolved_identity=dict(IDENT))
        self.assertNotIn("session_take_cards", folded)

    def test_track_rename_does_not_prevent_slot_replacement_history(self):
        old = _card("old-renamed", vibe="old take")
        old.update(track="Vocal", clip_locator="0/0", live_clip_id="101")
        new = _card("new-renamed", vibe="new take")
        new.update(track="Vox", clip_locator="0/0", live_clip_id="202")
        session_ears.write_take_cards([old], store_root=self.store, set_identity=dict(IDENT))
        session_ears.write_take_cards([new], store_root=self.store, set_identity=dict(IDENT))
        loaded = self._load(include_history=True)
        prior = next(card for card in loaded if card["take_id"] == "old-renamed")
        self.assertEqual(prior.get("superseded_by"), "new-renamed")

    def test_producer_pal_id_snapshot_excludes_stale_card(self):
        card = _card("pp-shape", vibe="removed sound")
        card.update(track="Vocal", clip_locator="0/0", live_clip_id="101")
        folded = session_ears.fold_take_cards(
            [card], {"sections": [], "current_clips": [{"id": "202", "path": "opaque"}]},
            expected_set_identity=dict(IDENT), _resolved_identity=dict(IDENT))
        self.assertNotIn("session_take_cards", folded)

        card["live_clip_id"] = "202"
        folded = session_ears.fold_take_cards(
            [card], {"sections": [], "current_clips": [{"id": "202", "path": "opaque"}]},
            expected_set_identity=dict(IDENT), _resolved_identity=dict(IDENT))
        self.assertEqual([item["take_id"] for item in folded["session_take_cards"]], ["pp-shape"])

    def test_fold_without_snapshot_marks_membership_unverified(self):
        card = _card("no-snapshot")
        card.update(track="Vocal", clip_locator="0/0", live_clip_id="101")
        folded = session_ears.fold_take_cards(
            [card], {"sections": []}, expected_set_identity=dict(IDENT),
            _resolved_identity=dict(IDENT))
        self.assertTrue(folded["session_take_cards"][0]["membership_unverified"])

    def test_moved_clip_remains_current_when_snapshot_still_contains_id(self):
        card = _card("moved-current")
        card.update(track="Vocal", clip_locator="0/0", live_clip_id="101")
        folded = session_ears.fold_take_cards(
            [card], {"sections": [], "current_clips": [{"id": "101", "path": "opaque"}]},
            expected_set_identity=dict(IDENT), _resolved_identity=dict(IDENT))
        self.assertEqual([item["take_id"] for item in folded["session_take_cards"]],
                         ["moved-current"])

    def _fold_history(self, snapshot):
        """Production order: load WITH history, membership decides in the fold."""
        history = self._load(include_history=True)
        return session_ears.fold_take_cards(
            history, {"sections": [], "current_clips": snapshot},
            expected_set_identity=dict(IDENT), _resolved_identity=dict(IDENT))

    def test_moved_clip_still_in_set_keeps_card_beside_new_slot_occupant(self):
        # Round-3 probe r2_cards scenario 1: clip 101 was heard at 0/0, then
        # moved to 0/1 (not re-listened); clip 202 recorded at 0/0 and heard.
        # Card shapes are persisted listen_to_takes cards; snapshot entries
        # are Producer Pal 2.2 clip rows ({"id", "path"}).
        # Each card carries the content signature of the clip it heard; the
        # snapshot rows carry the same clips' current evidence (review 12h).
        clip_101 = {"id": "101", "path": "t0/s1", "sampleFile": "/m/bright.wav", "start": "1|1"}
        clip_202 = {"id": "202", "path": "t0/s0", "sampleFile": "/m/drone.wav", "start": "1|1"}
        moved = _card("a", vibe="bright")
        moved.update(track="Vocal", clip_locator="0/0", live_clip_id="101",
                     content_signature=session_ears._content_signature(clip_101))
        newer = _card("b", vibe="drone")
        newer.update(track="Vocal", clip_locator="0/0", live_clip_id="202",
                     content_signature=session_ears._content_signature(clip_202))
        session_ears.write_take_cards([moved], session_id="s0",
                                      store_root=self.store, set_identity=dict(IDENT))
        session_ears.write_take_cards([newer], session_id="s1",
                                      store_root=self.store, set_identity=dict(IDENT))
        folded = self._fold_history([clip_101, clip_202])
        cards = folded["session_take_cards"]
        self.assertEqual(sorted(card["vibe"] for card in cards), ["bright", "drone"])
        self.assertFalse(any(card.get("membership_unverified") for card in cards))
        # Only the new occupant remains once 101 is really gone.
        folded = self._fold_history([clip_202])
        self.assertEqual([card["vibe"] for card in folded["session_take_cards"]], ["drone"])

    def test_track_reorder_keeps_both_cards_when_both_clips_exist(self):
        # Tracks swapped: B's clip 202 now sits at A's old locator 0/0 and is
        # re-listened there; A's clip 101 now lives at 1/0.
        track_a = _card("a", vibe="vocal A")
        track_a.update(track="A", clip_locator="0/0", live_clip_id="101")
        track_b = _card("b", vibe="bass B")
        track_b.update(track="B", clip_locator="1/0", live_clip_id="202")
        session_ears.write_take_cards([track_a, track_b], session_id="s0",
                                      store_root=self.store, set_identity=dict(IDENT))
        relisten = _card("b2", vibe="bass B again")
        relisten.update(track="B", clip_locator="0/0", live_clip_id="202")
        session_ears.write_take_cards([relisten], session_id="s1",
                                      store_root=self.store, set_identity=dict(IDENT))
        folded = self._fold_history([{"id": "202", "path": "t0/s0"},
                                     {"id": "101", "path": "t1/s0"}])
        self.assertEqual(sorted(card["vibe"] for card in folded["session_take_cards"]),
                         ["bass B again", "vocal A"])

    def test_snapshot_fold_keeps_newest_card_per_clip_id(self):
        older = _card("old-listen", vibe="first listen")
        older.update(track="Vocal", clip_locator="0/0", live_clip_id="101",
                     heard_at=time.time() - 60)
        newer = _card("new-listen", vibe="second listen")
        newer.update(track="Vocal", clip_locator="0/1", live_clip_id="101")
        folded = session_ears.fold_take_cards(
            [older, newer], {"sections": [], "current_clips": [{"id": "101", "path": "t0/s1"}]},
            expected_set_identity=dict(IDENT), _resolved_identity=dict(IDENT))
        self.assertEqual([card["vibe"] for card in folded["session_take_cards"]],
                         ["second listen"])

    def test_card_without_clip_id_is_unverified_even_with_snapshot(self):
        card = _card("no-id", vibe="captured")
        card.update(track="SIDEKICK EARS", clip_locator="5/2")
        folded = session_ears.fold_take_cards(
            [card], {"sections": [], "current_clips": [{"id": "999", "path": "t5/s0"}]},
            expected_set_identity=dict(IDENT), _resolved_identity=dict(IDENT))
        self.assertTrue(folded["session_take_cards"][0]["membership_unverified"])

    def test_listener_cannot_supply_server_supersession_metadata(self):
        card = _card("listener-key")
        card["superseded_by"] = "forged"
        cleaned = session_ears._sanitize_listener_card(card)
        self.assertNotIn("superseded_by", cleaned)
        self.assertTrue(session_ears.validate_take_card(cleaned))

    def test_concurrent_batches_do_not_lose_cards(self):
        barrier = threading.Barrier(3)
        errors = []

        def persist(take_id):
            try:
                barrier.wait()
                session_ears.write_take_cards(
                    [_card(take_id)], session_id="ledger-s",
                    store_root=self.store, set_identity=dict(IDENT))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=persist, args=(name,))
                   for name in ("take-a", "take-b")]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(c["take_id"] for c in self._load()),
                         ["take-a", "take-b"])

    def test_expired_card_is_not_resurrected_by_new_batch(self):
        old = _card("take-old")
        session_ears.write_take_cards(
            [old], session_id="ledger-s", store_root=self.store,
            set_identity=dict(IDENT))
        path = self.store / "session_ears" / _keydir() / "take_cards.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["cards"][0]["heard_at"] = time.time() - 3 * 86400
        path.write_text(json.dumps(payload), encoding="utf-8")
        session_ears.write_take_cards(
            [_card("take-new")], session_id="ledger-s", store_root=self.store,
            set_identity=dict(IDENT))
        self.assertEqual([c["take_id"] for c in self._load()], ["take-new"])


_PP_READ = Path(__file__).resolve().parent / "fixtures" / "pp" / "2.2.0-rc1" / "read"


def _pp_fixture(name: str) -> dict:
    """Captured Producer Pal 2.2.0-rc1 tool response (NOCTURNEv2 Set)."""
    return json.loads((_PP_READ / name).read_text(encoding="utf-8"))["response"]


@schema_strict
class _FixturePP:
    """Operation session answering from captured PP 2.2 read fixtures.

    Schema-strict for the build the running test speaks: in a ``…PP23`` twin
    the calls are checked against the 2.3.0 schemas and answered in 2.3
    shapes (tests/pp_fakes.py); ``calls`` holds the 2.2-equivalent arguments.

    ppal-read-live-set answers with the captured 32-track overview; tracks
    0-2 answer with their captured ppal-read-track payloads (filtered to the
    clip lists the request includes), every other track with empty lists.
    ``overrides`` replaces a track's session/arrangement clip rows. Every call
    is recorded so tests can count Live round trips.
    """

    def __init__(self, overrides=None):
        self.calls = []
        self.overview = _pp_fixture("read_live_set_tracks_locators.json")["content"][0]["json"]
        self.tracks = {i: _pp_fixture("read_track_t%d_index.json" % i)["content"][0]["json"]
                       for i in (0, 1, 2)}
        self.overrides = overrides or {}

    async def call(self, tool, args):
        from mcp_server.pp_session import PPResponse
        self.calls.append((tool, json.loads(json.dumps(args))))
        if tool == "ppal-read-live-set":
            body = self.overview
        elif tool == "ppal-read-track":
            index = args["trackIndex"]
            full = dict(self.tracks.get(index, {"sessionClips": [], "arrangementClips": [],
                                                 "trackIndex": index}))
            full.update(self.overrides.get(index, {}))
            include = args.get("include") or []
            body = {key: value for key, value in full.items()
                    if (key == "sessionClips" and "session-clips" in include)
                    or (key == "arrangementClips" and "arrangement-clips" in include)
                    or key in ("id", "name", "trackIndex")}
        else:
            raise AssertionError("unexpected Producer Pal tool %s" % tool)
        return PPResponse(json.dumps({"result": body}), body, [])


def _fixture_live(fake, identity=None, identity_calls=None):
    """Patch the Live seams the production fold uses onto ``fake``."""
    import contextlib
    from mcp_server import live_write, pp_session as pps

    @contextlib.asynccontextmanager
    async def operation(url=None):
        yield fake

    async def read_identity(session=None):
        if identity_calls is not None:
            identity_calls.append(session)
        return dict(identity or IDENT)

    stack = contextlib.ExitStack()
    stack.enter_context(patch.object(pps, "operation_session", operation))
    stack.enter_context(patch.object(live_write, "read_set_identity", read_identity))
    stack.enter_context(patch.object(session_ears, "_default_read_set_identity",
                                     return_value=dict(identity or IDENT)))
    return stack


@on_both_versions
class TestProductionFoldMembership(unittest.TestCase):
    """fold_persisted_session_takes against a Producer Pal fake built from
    captured 2.2 read fixtures — the path compose_brief / arrange_song use."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="prod-fold-")
        self.store = Path(self.tmp.name) / "store"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, *cards):
        session_ears.write_take_cards(list(cards), session_id="s", store_root=self.store,
                                      set_identity=dict(IDENT))

    def _card(self, take_id, vibe, locator, clip_id, heard=None):
        card = _card(take_id, vibe=vibe)
        card.update(track="Low Drone", clip_locator=locator)
        if clip_id is not None:
            card["live_clip_id"] = clip_id
        if heard is not None:
            # the Producer Pal clip row this card heard (review 12h signature)
            card["content_signature"] = session_ears._content_signature(heard)
        return card

    def test_moved_clip_card_survives_production_fold(self):
        # 53 was heard at 1/0, a new clip 60 took 1/0; the captured Set still
        # holds 53 (it moved), so both cards are current.
        clip_53 = {"id": "53", "path": "t1/s3", "sampleFile": "/m/53.wav",
                   "start": "1|1", "end": "3|1", "looping": True}
        clip_60 = {"id": "60", "path": "t1/s0", "sampleFile": "/m/60.wav",
                   "start": "1|1", "end": "5|1", "looping": True}
        self._write(self._card("a", "moved", "1/0", "53", heard=dict(clip_53, path="t1/s0")))
        self._write(self._card("b", "new occupant", "1/0", "60", heard=clip_60))
        fake = _FixturePP(overrides={1: {"sessionClips": [clip_60, clip_53]}})
        with _fixture_live(fake):
            out = session_ears.fold_persisted_session_takes({"sections": []},
                                                            store_root=self.store)
        self.assertEqual(sorted(c["vibe"] for c in out["session_take_cards"]),
                         ["moved", "new occupant"])
        self.assertFalse(any(c.get("membership_unverified")
                             for c in out["session_take_cards"]))



    def test_production_fold_reads_each_clip_track_once(self):
        # Round-3 probe r2_prod: 32-track NOCTURNEv2 made 66 PP calls.
        self._write(self._card("a", "present", "1/0", "53"),
                    self._card("b", "deleted", "1/9", "777"),
                    self._card("arr", "arr present", "arr:287", "287"))
        fake = _FixturePP()
        identity_calls = []
        with _fixture_live(fake, identity_calls=identity_calls):
            out = session_ears.fold_persisted_session_takes({"sections": []},
                                                            store_root=self.store)
        self.assertEqual(sorted(c["vibe"] for c in out["session_take_cards"]),
                         ["arr present", "present"])
        self.assertNotIn("current_clips", out)
        self.assertEqual(len(identity_calls), 1)
        tools = [tool for tool, _ in fake.calls]
        self.assertEqual(tools.count("ppal-read-live-set"), 1)
        reads = [args for tool, args in fake.calls if tool == "ppal-read-track"]
        # One read per regular track (32 in the captured Set), not two.
        self.assertEqual([args["trackIndex"] for args in reads], list(range(32)))
        for args in reads:
            self.assertEqual(sorted(args), ["include", "trackIndex"])
            self.assertEqual(args["include"], ["session-clips", "arrangement-clips",
                                               "timing", "sample"])
        schema = json.loads((_PP_READ.parent / "tool_schemas.json").read_text())
        allowed = set(schema["ppal-read-track"]["inputSchema"]["properties"]["include"]["items"]["enum"])
        self.assertTrue(set(reads[0]["include"]) <= allowed)
        self.assertEqual(len(fake.calls), 1 + len(reads))

    def test_no_clip_id_cards_skip_the_clip_scan(self):
        self._write(self._card("a", "captured", "5/2", None))
        fake = _FixturePP()
        with _fixture_live(fake):
            out = session_ears.fold_persisted_session_takes({"sections": []},
                                                            store_root=self.store)
        self.assertEqual(fake.calls, [])
        self.assertTrue(out["session_take_cards"][0]["membership_unverified"])

    def test_failed_clip_scan_marks_membership_unverified(self):
        self._write(self._card("a", "present", "1/0", "53"))

        class Broken(_FixturePP):
            async def call(self, tool, args):
                if tool == "ppal-read-track":
                    raise OSError("pp dropped the connection")
                return await super().call(tool, args)

        with _fixture_live(Broken()):
            out = session_ears.fold_persisted_session_takes({"sections": []},
                                                            store_root=self.store)
        self.assertEqual([(c["vibe"], c.get("membership_unverified"))
                          for c in out["session_take_cards"]], [("present", True)])
        self.assertNotIn("current_clips", out)

    def test_live_unreachable_passes_context_through(self):
        import contextlib
        from mcp_server import pp_session as pps
        self._write(self._card("a", "present", "1/0", "53"))

        @contextlib.asynccontextmanager
        async def down(url=None):
            raise OSError("Producer Pal not listening")
            yield  # pragma: no cover

        with patch.object(pps, "operation_session", down):
            out = session_ears.fold_persisted_session_takes({"sections": []},
                                                            store_root=self.store)
        self.assertEqual(out, {"sections": []})

    def test_read_current_session_clips_returns_pp_22_rows(self):
        fake = _FixturePP()
        with _fixture_live(fake):
            clips = session_ears._run_async(
                session_ears._read_current_session_clips(dict(IDENT)))
        ids = [clip["id"] for clip in clips]
        # Captured track 1 session rows carry {"id", "path"}; arrangement
        # rows carry an id only.
        self.assertIn({"id": "53", "path": "t1/s0"}, clips)
        self.assertIn({"id": "354", "path": "t2/s6"}, clips)
        self.assertIn("287", ids)
        self.assertIn("1023", ids)
        self.assertTrue(all(isinstance(i, str) for i in ids))

    def test_read_current_session_clips_identity_change_is_unknown(self):
        fake = _FixturePP()
        with _fixture_live(fake, identity={**IDENT, "runtime_epoch": "other"}):
            clips = session_ears._run_async(
                session_ears._read_current_session_clips(dict(IDENT)))
        self.assertIsNone(clips)
        self.assertEqual(fake.calls, [])
        card = self._card("a", "present", "1/0", "53")
        folded = session_ears.fold_take_cards(
            [card], {"sections": [], "current_clips": clips},
            expected_set_identity=dict(IDENT), _resolved_identity=dict(IDENT))
        self.assertTrue(folded["session_take_cards"][0]["membership_unverified"])

    def test_write_take_cards_strips_caller_supersession(self):
        forged = self._card("a", "forged history", "1/0", "53")
        forged["superseded_by"] = "someone"
        self._write(forged)
        payload = json.loads((self.store / "session_ears" / _keydir()
                              / "take_cards.json").read_text())
        self.assertNotIn("superseded_by", payload["cards"][0])

    def test_listener_cannot_supply_membership_flag(self):
        card = _card("listener")
        card["membership_unverified"] = False
        cleaned = session_ears._sanitize_listener_card(card)
        self.assertNotIn("membership_unverified", cleaned)
        self.assertTrue(session_ears.validate_take_card(cleaned))


class TestFoldSessionTeardown(unittest.TestCase):
    """An error raised while the PP session closes never escapes the fold."""

    def _run(self, exit_exc, identity_ok):
        import contextlib
        from mcp_server import live_write, pp_session

        class _Dead:
            async def call(self, name, args=None):
                raise OSError("connection dropped")

        @contextlib.asynccontextmanager
        async def opsess(url=None):
            try:
                yield _Dead()
            finally:
                if exit_exc is not None:
                    raise exit_exc

        async def rsi(_session):
            if not identity_ok:
                raise OSError("identity read failed")
            return dict(IDENT)

        card = _card("a", vibe="a")
        card.update(track="Vocal", clip_locator="0/0", live_clip_id="101")
        with tempfile.TemporaryDirectory() as d:
            session_ears.write_take_cards([card], session_id="s", store_root=d,
                                          set_identity=dict(IDENT))
            with patch.object(pp_session, "operation_session", opsess), \
                    patch.object(live_write, "read_set_identity", rsi):
                return session_ears.fold_persisted_session_takes(
                    {"sections": []}, store_root=d)

    def test_teardown_error_after_body_folds_cards_unverified(self):
        out = self._run(RuntimeError("cancel scope exit"), identity_ok=True)
        cards = out.get("session_take_cards", [])
        self.assertEqual([c["vibe"] for c in cards], ["a"])
        self.assertTrue(all(c.get("membership_unverified") for c in cards))

    def test_teardown_error_after_identity_failure_passes_context_through(self):
        out = self._run(OSError("close failed"), identity_ok=False)
        self.assertEqual(out, {"sections": []})

    def test_clean_session_still_folds(self):
        out = self._run(None, identity_ok=True)
        self.assertEqual([c["vibe"] for c in out.get("session_take_cards", [])], ["a"])

    def test_corrupt_card_load_still_raises_through_teardown(self):
        import contextlib
        from mcp_server import live_write, pp_session

        @contextlib.asynccontextmanager
        async def opsess(url=None):
            try:
                yield object()
            finally:
                raise RuntimeError("teardown")

        async def rsi(_session):
            return dict(IDENT)

        async def run():
            def load(_identity):
                raise ValueError("corrupt card file")
            return await session_ears._read_identity_cards_and_clips(load)

        import asyncio
        with patch.object(pp_session, "operation_session", opsess), \
                patch.object(live_write, "read_set_identity", rsi):
            with self.assertRaisesRegex(ValueError, "corrupt card file"):
                asyncio.run(run())


class TestListenTwiceKeepsBothCards(unittest.TestCase):
    """Two listen batches against one Set leave both cards persisted."""

    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory(prefix="listen-twice-")
        self.root = Path(self.tmp.name)
        audio = self.root / "take.wav"
        audio.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
        trusted = self.root / "trusted"
        trusted.mkdir()
        for name in ("one.wav", "two.wav"):
            (trusted / name).write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"session_id": "merge-session", "takes": [
            {"track": "Vocal", "clip_name": "take one",
             "path": str(trusted / "one.wav"), "bars": 8, "locator": "0/0"},
            {"track": "Vocal", "clip_name": "take two",
             "path": str(trusted / "two.wav"), "bars": 8, "locator": "0/1"},
        ]}), encoding="utf-8")
        self.env = session_ears._inventory_from_manifest(manifest, trusted)

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def _fake_runner(self, cmd, **kwargs):
        import re
        prompt = cmd[-1]
        take_id = re.search(r"- take_id: ([^\n]+)", prompt).group(1)
        track = re.search(r"- track: ([^\n]+)", prompt).group(1)
        clip = re.search(r"- clip_name: ([^\n]+)", prompt).group(1)
        card = dict(_card("placeholder"), take_id=take_id, track=track,
                    clip_name=clip)
        card["set_identity"] = {"set_id": "merge-session",
                                "runtime_epoch": "manifest"}
        output = Path(re.search(
            r"write the same JSON object to:\n([^\n]+)", prompt).group(1))
        output.write_text(json.dumps(card), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def test_listen_twice_both_cards_present_under_public_token(self):
        store = self.root / "store"
        take_ids = [t["take_id"] for t in self.env["takes"]]
        self.assertEqual(len(take_ids), 2)
        with patch.object(session_ears, "_which", return_value="/fake/agy"):
            first = session_ears.listen_to_takes(
                self.env["inventory_id"], take_ids[:1],
                runner=self._fake_runner, store_root=store,
                allow_unverified_listen=True)
            env2 = session_ears._inventory_from_manifest(
                self.root / "manifest.json", self.root / "trusted")
            second = session_ears.listen_to_takes(
                env2["inventory_id"], [t["take_id"] for t in env2["takes"]][1:],
                runner=self._fake_runner, store_root=store,
                allow_unverified_listen=True)
        self.assertEqual(first["cards"][0]["clip_name"], "take one")
        self.assertEqual(first["errors"], [])
        self.assertEqual(second["cards"][0]["clip_name"], "take two")
        self.assertEqual(second["errors"], [])
        loaded = session_ears.load_latest_take_cards(
            session_id=session_ears._public_set_identity(
                {"set_id": "merge-session", "runtime_epoch": "manifest"}
            )["set_id"], store_root=store, max_age_s=None,
            expected_set_identity={"set_id": "merge-session",
                                   "runtime_epoch": "manifest"})
        self.assertEqual(
            sorted(c["clip_name"] for c in loaded), ["take one", "take two"])

    def test_relisten_same_clip_replaces_card_under_public_token(self):
        store = self.root / "stable-clip-store"
        take_ids = [t["take_id"] for t in self.env["takes"]]
        with patch.object(session_ears, "_which", return_value="/fake/agy"):
            first = session_ears.listen_to_takes(
                self.env["inventory_id"], take_ids[:1], runner=self._fake_runner,
                store_root=store, allow_unverified_listen=True)
            fresh = session_ears._inventory_from_manifest(
                self.root / "manifest.json", self.root / "trusted")
            second = session_ears.listen_to_takes(
                fresh["inventory_id"], [fresh["takes"][0]["take_id"]],
                runner=self._fake_runner, store_root=store,
                allow_unverified_listen=True)
        loaded = session_ears.load_latest_take_cards(
            session_id=session_ears._public_set_identity(
                {"set_id": "merge-session", "runtime_epoch": "manifest"}
            )["set_id"], store_root=store, max_age_s=None,
            expected_set_identity={"set_id": "merge-session", "runtime_epoch": "manifest"})
        self.assertNotEqual(first["cards"][0]["take_id"], second["cards"][0]["take_id"])
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["take_id"], second["cards"][0]["take_id"])


class TestListenThenJamWriteKeepsCards(unittest.TestCase):
    """Cards persist under the Set key, so the first JAM write (which opens a
    new ledger session) cannot orphan them."""

    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory(prefix="jam-write-")
        self.root = Path(self.tmp.name)
        self.store = self.root / "store"
        self.source = self.root / "source.wav"
        self.source.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def test_cards_found_after_first_jam_write(self):
        take = session_ears._InternalTake(
            take_id="take-jam-1", track_index=0, track_name="Vox",
            view="session", clip_locator="0/0", clip_name="Vox take",
            duration_s=8.0, bars=8, path=str(self.source),
            analyzed_start_s=0.0, analyzed_end_s=8.0, analyzed_duration_s=8.0,
            set_identity=dict(IDENT))
        inv = session_ears._register_inventory(
            "persist-live-set", [take], set_identity=dict(IDENT))

        def fake_runner(cmd, **kwargs):
            card = _card("take-jam-1")
            card["track"] = "Vox"
            card["clip_name"] = "Vox take"
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(card), stderr="")

        with patch.object(session_ears, "_which", return_value="/fake/agy"), \
             patch.object(session_ears, "_stage_audio",
                          return_value=(self.source, None)):
            heard = session_ears.listen_to_takes(
                inv["inventory_id"], ["take-jam-1"], store_root=self.store,
                runner=fake_runner, allow_unverified_listen=True,
                _read_set_identity=lambda: dict(IDENT))
        self.assertEqual(heard["errors"], [])
        cards = heard["cards"]
        self.assertEqual(cards[0]["take_id"], "take-jam-1")
        self.assertIsInstance(cards[0].get("heard_at"), (int, float))
        # The first JAM write opens a fresh ledger session for the same Set.
        _ledger.record_write(
            clip_name="jam 1", slot="0/0", source="catalogue",
            set_identity=dict(IDENT),
            path=str(self.store / "session_ledger.json"))
        with patch.object(session_ears, "_default_read_set_identity",
                          return_value=dict(IDENT)):
            loaded = session_ears.load_latest_take_cards(
                store_root=self.store, max_age_s=None)
        self.assertEqual([c["take_id"] for c in loaded], ["take-jam-1"])


if __name__ == "__main__":
    unittest.main()
