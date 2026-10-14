"""Controller probes of default Set binding and actual analyzed excerpts."""
import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from mcp_server import session_ears as se, moment
from tests.test_agy_listen_verification import plant_listen_evidence
from tests.test_moment import FakePPSession

IDENT = {"set_id": "/projects/A/Untitled.als", "file_path": "/projects/A/Untitled.als", "runtime_epoch": "run-a"}
OTHER = {"set_id": "/projects/B/Untitled.als", "file_path": "/projects/B/Untitled.als", "runtime_epoch": "run-b"}

def card():
    return {"take_id": "take-1", "track": "Vox", "clip_name": "take 1", "role_guess": "lead",
            "vibe": "dry vocal", "register": None, "density": "sparse", "pitch_claims": [],
            "timestamps": ["00:00"], "contract_version": se.CONTRACT_VERSION,
            "source": "live", "set_identity": IDENT}

class AudioBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.addCleanup(se._reset_registry_for_tests)

    def test_default_load_and_fold_recheck_current_set(self):
        se.write_take_cards([card()], "session", self.root, set_identity=IDENT)
        with patch.object(se, "_default_read_set_identity", return_value=OTHER):
            self.assertEqual(se.load_latest_take_cards("session", self.root), [])
            self.assertEqual(se.fold_take_cards([card()], {"marker": True}), {"marker": True})

    def test_identity_read_failure_cannot_fold_cards(self):
        with patch.object(se, "_default_read_set_identity", side_effect=RuntimeError("offline")):
            self.assertEqual(se.fold_take_cards([card()], {}), {})

    def test_load_without_open_ledger_uses_verified_set_storage(self):
        sid = se._resolve_persistence_session_id("display-name", self.root, set_identity=IDENT)
        se.write_take_cards([card()], sid, self.root, set_identity=IDENT)
        with patch.object(se, "_default_read_set_identity", return_value=IDENT):
            self.assertEqual(len(se.load_latest_take_cards(store_root=self.root)), 1)

    def test_same_filename_does_not_bind_unrelated_ledger(self):
        (self.root / "session_ledger.json").write_text(json.dumps({"sessions": [
            {"id": "wrong-ledger", "set_identity": OTHER, "closed_at": None}]}))
        self.assertNotEqual(se._resolve_persistence_session_id("Untitled", self.root, set_identity=IDENT), "wrong-ledger")

    def test_listener_metadata_matches_capped_excerpt(self):
        source = self.root / "source.wav"; source.write_bytes(b"RIFF")
        take = se._InternalTake(take_id="take-1", track_index=0, track_name="Vox", view="session",
                                clip_locator="0/0", clip_name="take 1", duration_s=120, bars=None,
                                path=str(source), analyzed_start_s=10, analyzed_end_s=110,
                                analyzed_duration_s=100, set_identity=IDENT)
        inv = se._register_inventory("set", [take], set_identity=IDENT)
        convs = self.root / "convs"
        convs.mkdir(exist_ok=True)

        def evidenced_runner(cmd, *args, **kwargs):
            prompt = cmd[-1]
            output = Path(re.search(
                r"write the same JSON object to:\n([^\n]+)", prompt).group(1))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(card()), encoding="utf-8")
            # _stage_audio is stubbed here, so the prompt's staged path
            # is not the verified one: plant for the verified path.
            plant_listen_evidence(convs, "conv-excerpt", str(source))
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"conversation_id": "conv-excerpt",
                                   "usage": {"input_tokens": 100}}))

        with patch.object(se, "_default_read_set_identity", return_value=IDENT), \
             patch.object(se, "_which", return_value="/fake/agy"), \
             patch.object(se, "_stage_audio", return_value=(source, 45)):
            heard = se.listen_to_takes(inv["inventory_id"], ["take-1"], store_root=self.root,
                runner=evidenced_runner, conversations_dir=convs)
        self.assertEqual(heard["errors"], [])
        cards = heard["cards"]
        self.assertEqual(cards[0]["analyzed_start_s"], 10)
        self.assertEqual(cards[0]["analyzed_end_s"], 55)
        self.assertEqual(cards[0]["analyzed_duration_s"], 45)

    def test_public_cards_do_not_expose_set_file_paths(self):
        with patch.object(se, "_default_read_set_identity", return_value=IDENT):
            result = se.fold_take_cards([card()], {})
        self.assertNotIn("/projects/", json.dumps(result))

    def test_staging_rejects_invalid_or_out_of_file_regions(self):
        source = self.root / "source.wav"; source.write_bytes(b"RIFF")
        for start, end in ((float("nan"), 5), (2, 1), (9, 12)):
            with self.subTest(start=start, end=end), \
                 patch.object(se, "_require_plausible_audio"), \
                 patch.object(se, "_duration_hint", return_value=10), \
                 patch.object(se, "ffmpeg_to_wav") as convert:
                with self.assertRaises(se.SessionEarsError):
                    se._stage_audio({"path": str(source), "analyzed_start_s": start,
                                     "analyzed_end_s": end}, self.root)
                convert.assert_not_called()

    def test_explicit_arrangement_bar_is_read_while_stopped(self):
        session = FakePPSession(responses={
            "ppal-read-live-set": {"tempo": 99.5, "timeSignature": "3/4", "isPlaying": False,
                "tracks": [{"trackIndex": 0, "name": "Vox", "type": "audio"}]},
            "ppal-read-track": {"arrangementClips": [{"id": "a", "name": "take", "type": "audio",
                "arrangementStart": "5|1", "arrangementLength": "1bar"}]}})
        result = asyncio.run(moment.read_moment_live(where="arrangement", bar=5, session=session))
        self.assertEqual(result["context"].get("bar"), 5)
        self.assertTrue(result["unheard"])
