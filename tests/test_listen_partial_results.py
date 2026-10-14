"""Lane B3 item 4: listen_to_takes validates before consuming, reports per-take errors.

The batch validates focus, agy presence, and every take's capture gate BEFORE
consuming the inventory; only attempted takes are consumed; each validated
card is persisted as it arrives; per-take failures land in errors instead of
raising on the first bad take::

    {"cards": [...], "errors": [{"take_id": ..., "reason": ...}]}
"""
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import session_ears
from tests._stage_passthrough import passthrough_listenable_staging
from mcp_server.session_ears import (
    CONTRACT_VERSION,
    SessionEarsError,
)

MANIFEST_IDENT = {"set_id": "partial-session", "runtime_epoch": "manifest"}


def _card(take_id, track, clip):
    return {
        "take_id": take_id,
        "track": track,
        "clip_name": clip,
        "role_guess": "lead",
        "vibe": "close and grainy",
        "register": None,
        "density": "sparse",
        "pitch_claims": [],
        "timestamps": ["00:00"],
        "contract_version": CONTRACT_VERSION,
    }


def _manifest_env(root: Path, session_id: str, clips) -> dict:
    trusted = root / "trusted"
    trusted.mkdir(exist_ok=True)
    takes = []
    for i, clip in enumerate(clips):
        audio = trusted / ("take%d.wav" % i)
        audio.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
        takes.append({
            "track": "Vocal", "clip_name": clip,
            "path": str(audio), "bars": 8, "locator": "0/%d" % i,
        })
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps({"session_id": session_id, "takes": takes}),
        encoding="utf-8")
    return session_ears._inventory_from_manifest(manifest, trusted)


_CONVS: dict = {"dir": None}


def _runner(cards_by_take: dict, fail: set | None = None,
            deaf: set | None = None):
    """Fake agy runner. Writes the card to the prompt's output path and plants
    a real-shaped proof-of-listening record (view_file RESULT on the staged
    take) in _CONVS["dir"]; OSError for takes in ``fail``; takes in ``deaf``
    get a card but their view_file failed with "unsupported mime type" (the
    real 2026-09 failure: the agent asked, the audio never reached it)."""
    from tests.test_agy_listen_verification import (
        _staged_path_from_prompt, plant_listen_evidence,
        plant_mime_failure_evidence)
    fail = fail or set()
    deaf = deaf or set()

    def run(cmd, **kwargs):
        prompt = cmd[-1]
        take_id = re.search(r"- take_id: ([^\n]+)", prompt).group(1)
        if take_id in fail:
            raise OSError("simulated agy failure")
        output = Path(re.search(
            r"write the same JSON object to:\n([^\n]+)", prompt).group(1))
        output.write_text(json.dumps(cards_by_take[take_id]), encoding="utf-8")
        cid = "conv-%s" % take_id
        plant = (plant_mime_failure_evidence if take_id in deaf
                 else plant_listen_evidence)
        plant(_CONVS["dir"], cid, _staged_path_from_prompt(prompt))
        return SimpleNamespace(
            returncode=0, stderr="",
            stdout=json.dumps({"conversation_id": cid,
                               "usage": {"input_tokens": 100}}))
    return run


def _listen(inventory_id, take_ids, **kwargs):
    """listen_to_takes with the proof-of-listening conversations dir."""
    return session_ears.listen_to_takes(
        inventory_id, take_ids, conversations_dir=_CONVS["dir"], **kwargs)


class TestPartialFailure(unittest.TestCase):
    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory(prefix="partial-")
        self.root = Path(self.tmp.name)
        _CONVS["dir"] = self.root / "convs"
        _CONVS["dir"].mkdir(exist_ok=True)

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def test_bad_take_lands_in_errors_good_take_persists_under_public_token(self):
        env = _manifest_env(self.root, "partial-session", ["take one", "take two"])
        ids = [t["take_id"] for t in env["takes"]]
        by_take = {
            ids[0]: _card(ids[0], "Vocal", "take one"),
            ids[1]: _card(ids[1], "Vocal", "take two"),
        }
        store = self.root / "store"
        with passthrough_listenable_staging(), \
                patch.object(session_ears, "_which", return_value="/fake/agy"):
            heard = _listen(
                env["inventory_id"], ids, runner=_runner(by_take, fail={ids[1]}),
                store_root=store)
        self.assertEqual([c["take_id"] for c in heard["cards"]], [ids[0]])
        self.assertEqual(len(heard["errors"]), 1)
        self.assertEqual(heard["errors"][0]["take_id"], ids[1])
        self.assertIn(ids[1], heard["errors"][0]["reason"])
        # The good card was persisted as it arrived, not lost with its sibling.
        loaded = session_ears.load_latest_take_cards(
            session_id=session_ears._public_set_identity(MANIFEST_IDENT)["set_id"], store_root=store, max_age_s=None,
            expected_set_identity=dict(MANIFEST_IDENT))
        self.assertEqual([c["take_id"] for c in loaded], [ids[0]])

    def test_deaf_take_lands_in_errors_and_is_never_persisted(self):
        # Merge of the agy-listener lane into the per-take listener: a take
        # whose agy run shows no view_file RESULT on the staged audio is a
        # rejected listen — an errors entry (reason no_listen), never a card,
        # while its sibling in the same call still lands and persists.
        env = _manifest_env(self.root, "partial-session", ["take one", "take two"])
        ids = [t["take_id"] for t in env["takes"]]
        by_take = {
            ids[0]: _card(ids[0], "Vocal", "take one"),
            ids[1]: _card(ids[1], "Vocal", "take two"),
        }
        store = self.root / "store"
        with passthrough_listenable_staging(), \
                patch.object(session_ears, "_which", return_value="/fake/agy"):
            heard = _listen(env["inventory_id"], ids,
                            runner=_runner(by_take, deaf={ids[1]}),
                            store_root=store)
        self.assertEqual([c["take_id"] for c in heard["cards"]], [ids[0]])
        self.assertEqual([e["take_id"] for e in heard["errors"]], [ids[1]])
        self.assertIn("listen rejected", heard["errors"][0]["reason"])
        self.assertIn("no_listen", heard["errors"][0]["reason"])
        loaded = session_ears.load_latest_take_cards(
            session_id=session_ears._public_set_identity(MANIFEST_IDENT)["set_id"],
            store_root=store, max_age_s=None,
            expected_set_identity=dict(MANIFEST_IDENT))
        self.assertEqual([c["take_id"] for c in loaded], [ids[0]])

    def test_unattempted_take_stays_listenable_and_persists_under_public_token(self):
        env = _manifest_env(self.root, "partial-session", ["take one", "take two"])
        ids = [t["take_id"] for t in env["takes"]]
        by_take = {
            ids[0]: _card(ids[0], "Vocal", "take one"),
            ids[1]: _card(ids[1], "Vocal", "take two"),
        }
        store = self.root / "store"
        with passthrough_listenable_staging(), \
                patch.object(session_ears, "_which", return_value="/fake/agy"):
            first = _listen(
                env["inventory_id"], [ids[0]], runner=_runner(by_take),
                store_root=store)
            second = _listen(
                env["inventory_id"], [ids[1]], runner=_runner(by_take),
                store_root=store)
        self.assertEqual([c["take_id"] for c in first["cards"]], [ids[0]])
        self.assertEqual(first["errors"], [])
        self.assertEqual([c["take_id"] for c in second["cards"]], [ids[1]])
        loaded = session_ears.load_latest_take_cards(
            session_id=session_ears._public_set_identity(MANIFEST_IDENT)["set_id"], store_root=store, max_age_s=None,
            expected_set_identity=dict(MANIFEST_IDENT))
        self.assertEqual(sorted(c["take_id"] for c in loaded), sorted(ids))

    def test_missing_agy_fails_closed_before_consuming(self):
        env = _manifest_env(self.root, "partial-session", ["take one"])
        take_id = env["takes"][0]["take_id"]
        by_take = {take_id: _card(take_id, "Vocal", "take one")}
        store = self.root / "store"
        with patch.object(session_ears, "_which", return_value=None):
            with self.assertRaises(SessionEarsError):
                _listen(
                    env["inventory_id"], [take_id], runner=_runner(by_take),
                    store_root=store)
        # Nothing was consumed: the same inventory still listens.
        with passthrough_listenable_staging(), \
                patch.object(session_ears, "_which", return_value="/fake/agy"):
            heard = _listen(
                env["inventory_id"], [take_id], runner=_runner(by_take),
                store_root=store)
        self.assertEqual([c["take_id"] for c in heard["cards"]], [take_id])
        self.assertEqual(heard["errors"], [])


class TestCaptureGateBeforeConsume(unittest.TestCase):
    def setUp(self):
        session_ears._reset_registry_for_tests()
        self.tmp = tempfile.TemporaryDirectory(prefix="capgate-")
        self.root = Path(self.tmp.name)
        _CONVS["dir"] = self.root / "convs"
        _CONVS["dir"].mkdir(exist_ok=True)
        self.source = self.root / "source.wav"
        self.source.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")

    def tearDown(self):
        session_ears._reset_registry_for_tests()
        self.tmp.cleanup()

    def _register(self):
        ident = {"set_id": "cap-set", "runtime_epoch": "manifest"}
        good = session_ears._InternalTake(
            take_id="take-good", track_index=0, track_name="Vox",
            view="session", clip_locator="0/0", clip_name="good take",
            duration_s=8.0, bars=8, path=str(self.source),
            analyzed_start_s=0.0, analyzed_end_s=8.0,
            analyzed_duration_s=8.0, set_identity=dict(ident))
        gated = session_ears._InternalTake(
            take_id="take-gated", track_index=1, track_name="Warped",
            view="session", clip_locator="1/0", clip_name="warped take",
            duration_s=8.0, bars=8, path=str(self.source),
            analyzed_start_s=0.0, analyzed_end_s=8.0,
            analyzed_duration_s=8.0, capture_required=True,
            guidance="use capture_loop preview", set_identity=dict(ident))
        return session_ears._register_inventory(
            "cap-session", [good, gated], set_identity=dict(ident))

    def test_gated_batch_raises_without_consuming(self):
        inv = self._register()
        by_take = {"take-good": _card("take-good", "Vox", "good take")}
        runner_mock = _runner(by_take)
        calls: list = []

        def counting_runner(cmd, **kwargs):
            calls.append(cmd)
            return runner_mock(cmd, **kwargs)

        store = self.root / "store"
        with passthrough_listenable_staging(), \
                patch.object(session_ears, "_which", return_value="/fake/agy"):
            with self.assertRaises(SessionEarsError) as ctx:
                _listen(
                    inv["inventory_id"], ["take-good", "take-gated"],
                    runner=counting_runner, store_root=store)
        self.assertIn("requires capture", str(ctx.exception))
        self.assertEqual(calls, [])
        # The good take is still listenable on the same inventory.
        with passthrough_listenable_staging(), \
                patch.object(session_ears, "_which", return_value="/fake/agy"):
            heard = _listen(
                inv["inventory_id"], ["take-good"],
                runner=counting_runner, store_root=store)
        self.assertEqual([c["take_id"] for c in heard["cards"]], ["take-good"])
        self.assertEqual(heard["errors"], [])


if __name__ == "__main__":
    unittest.main()
