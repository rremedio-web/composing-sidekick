"""Lane B3 item 2: take-card fold reaches composing tools over MCP dispatch.

Proves arrange_song / compose_brief / finish_my_idea / answer_my_loop include
session_take_cards when a fresh valid card is persisted, with the Set identity
resolved ONCE per fold (no repeated Live round trips).
"""
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import live_write, pp_session, server, session_ears

_SEED_NOTES = [
    {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90},
    {"pitch": 62, "start": 0.5, "duration": 0.5, "velocity": 90},
    {"pitch": 64, "start": 1.0, "duration": 1.0, "velocity": 90},
]


def _fold_identity() -> dict:
    return {"set_id": "fold-test-set", "runtime_epoch": "fold-epoch-1"}


def _fold_card() -> dict:
    return {
        "take_id": "take-fold-1",
        "track": "Vocal",
        "clip_name": "take 1",
        "role_guess": "lead",
        "vibe": "close and grainy",
        "register": None,
        "density": "sparse",
        "pitch_claims": [],
        "timestamps": ["00:00"],
        "contract_version": session_ears.CONTRACT_VERSION,
        "set_identity": session_ears._public_set_identity(_fold_identity()),
    }


class TestTakeCardFoldDispatch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fold-dispatch-")
        self.store = Path(self.tmp.name)
        identity = _fold_identity()
        session_ears.write_take_cards(
            [_fold_card()], session_id="fold-test-set",
            store_root=self.store, set_identity=identity)
        self.resolve_calls = []

        async def fake_read_identity(_session=None):
            self.resolve_calls.append(1)
            return dict(identity)

        self._identity_patch = patch.object(
            live_write, "read_set_identity", fake_read_identity)
        self._store_patch = patch.object(session_ears, "STORE_ROOT", self.store)
        # The production fold opens one Producer Pal session for identity and
        # (only when a card carries a Live clip id) the clip scan. These cards
        # carry none, so the session must never be asked for anything.
        self.pp_calls = []

        class _NoScanSession:
            async def call(inner_self, tool, args):
                self.pp_calls.append(tool)
                raise AssertionError("no clip scan expected: %s" % tool)

        @contextlib.asynccontextmanager
        async def fake_operation_session(url=None):
            yield _NoScanSession()

        self._session_patch = patch.object(
            pp_session, "operation_session", fake_operation_session)
        self._identity_patch.start()
        self._store_patch.start()
        self._session_patch.start()

    def tearDown(self):
        self._identity_patch.stop()
        self._store_patch.stop()
        self._session_patch.stop()
        self.assertEqual(self.pp_calls, [])
        self.tmp.cleanup()

    async def _call(self, tool: str, args: dict) -> dict:
        result = await server.mcp.call_tool(tool, args)
        return json.loads(result[0].text)

    async def _assert_folded_once(
            self, tool: str, args: dict, *path: str) -> dict:
        before = len(self.resolve_calls)
        # Leave the composing functions and their fold calls real. Replace
        # only their corpus-loading/retrieval seams with a small synthetic profile.
        aggregate = {
            "tempo": {"mean": 120}, "pitch_register": [48, 84],
            "key_distribution": [], "mode_distribution": [],
            "tonic_distribution": [], "top_melodic_intervals": [],
            "duration_distribution": [],
        }
        structure = {"aggregate": {
            "song_length": {"avg_bars": 8}, "section_length_histogram": {},
            "energy_arc": {"points": [0.5] * 16}, "breakdown_pct": 0,
        }}
        phrase = {"id": "fixture-phrase", "role": "lead", "kind": "harmonic",
                  "notes": _SEED_NOTES, "tempo": 120}
        with patch.object(server._core, "_aggregate", return_value=aggregate), \
             patch.object(server._core, "_structure_data", return_value=structure), \
             patch.object(server._core, "_harmony_data", return_value={}), \
             patch.object(server._core, "_read_style_md", return_value="fixture style"), \
             patch.object(server._core, "find_phrases", return_value=[phrase]), \
             patch.object(server._core, "answer_my_loop",
                          return_value={"sections": []}):
            envelope = await self._call(tool, args)
        node = envelope
        for key in path:
            self.assertIsInstance(
                node, dict, "%s has no %r section" % (tool, key))
            node = node.get(key)
        cards = node.get("session_take_cards")
        self.assertIsInstance(cards, list, "%s folded no cards" % tool)
        self.assertEqual(cards[0]["take_id"], "take-fold-1")
        self.assertEqual(
            len(self.resolve_calls) - before, 1,
            "%s resolved the Set identity %d times, want once"
            % (tool, len(self.resolve_calls) - before))
        return envelope

    async def test_arrange_song_folds_cards(self):
        await self._assert_folded_once("arrange_song", {
            "parts": [{"role": "lead", "notes": _SEED_NOTES, "loop_bars": 2}],
        })

    async def test_compose_brief_folds_cards(self):
        envelope = await self._assert_folded_once(
            "compose_brief", {"request": "a full song sketch"},
            "suggested_structure")
        self.assertIsNotNone(envelope.get("suggested_structure"))

    async def test_finish_my_idea_folds_cards(self):
        await self._assert_folded_once(
            "finish_my_idea", {"seed_notes": _SEED_NOTES}, "structure")

    async def test_answer_my_loop_folds_cards(self):
        await self._assert_folded_once(
            "answer_my_loop", {"seed_notes": _SEED_NOTES})


if __name__ == "__main__":
    unittest.main()
