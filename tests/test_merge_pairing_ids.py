"""Merge of b3/selection (moved DROPs ban only their pairing) with b3/corpus
(content-addressed pids; stored aliases still resolve): the pairing check must
find the right DROP record even when it was stored under the phrase's OLD
positional alias, and a global ban under one id form is never narrowed by a
moved DROP stored under the other.
"""
from __future__ import annotations

import unittest
from unittest import mock

from mcp_server import taste_bridge as TB

ALIAS = "song|c1|w0"
PID = "p-abc123"


def _cand():
    # a rebuilt index record: id == pid, the old positional id kept as alias
    return {"id": PID, "pid": PID, "alias": ALIAS, "project": "song",
            "kind": "harmonic", "notes": [], "vec": None}


def _reject(identity, move, created_at="2026-09-25T10:00:00+00:00"):
    return {"identity": identity, "phrase_id": identity, "move": move,
            "created_at": created_at, "comment": "", "vec": None}


class MovedDropUnderOldAliasTests(unittest.TestCase):
    def _cull(self, rejects, move):
        with mock.patch.object(TB, "rejected_phrases", return_value=rejects):
            return TB.reject_cull([_cand()], move=move)

    def test_moved_drop_stored_under_alias_bans_only_its_pairing(self):
        rejects = [_reject(ALIAS, "surgery:thin")]
        kept, suppressed = self._cull(rejects, move="slow-burn:reveal")
        self.assertEqual([c["id"] for c in kept], [PID])
        kept, suppressed = self._cull(rejects, move="surgery:thin")
        self.assertEqual(kept, [])
        self.assertEqual(suppressed[0]["move"], "surgery:thin")

    def test_global_ban_under_alias_wins_over_moved_drop_under_pid(self):
        rejects = [_reject(PID, "surgery:thin"), _reject(ALIAS, None)]
        for move in (None, "slow-burn:reveal", "surgery:thin"):
            kept, suppressed = self._cull(rejects, move=move)
            self.assertEqual(kept, [], move)
            self.assertIsNone(suppressed[0]["move"], move)


if __name__ == "__main__":
    unittest.main()
