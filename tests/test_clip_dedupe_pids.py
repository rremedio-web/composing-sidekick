"""48h review P4: after the corpus rebuild, phrase ids are content hashes and
the source location lives in `alias` ('project|cN|wN'). The re-roll's
one-window-per-clip filter must key on the alias, or distinct windows of one
clip (unrelated hashes) all pass. Uses real iter_phrases output."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "analyze"))

from build_index import iter_phrases  # noqa: E402
from mcp_server import core  # noqa: E402


def _clip(name, pitches):
    notes = [{"pitch": p, "start": float(i), "duration": 0.5, "velocity": 100,
              "enabled": True} for i, p in enumerate(pitches)]
    return {"name": name, "track": "Lead", "is_drum": False,
            "clip_source": "session", "is_sidekick": False, "notes": notes}


class ClipDedupeOnPidIndex(unittest.TestCase):
    def test_windows_of_one_clip_collapse_to_one(self):
        # 16 beats of distinct pitches -> two 8-beat windows of one clip
        data = {"path": "/tmp/song.als", "tempo": 120.0, "clips": [
            _clip("a", [60, 62, 64, 65, 67, 69, 71, 72,
                        74, 72, 71, 69, 67, 65, 64, 62]),
            _clip("b", [48, 50, 52, 53, 55, 57, 59, 60]),
        ]}
        phrases = list(iter_phrases(data))
        a_windows = [p for p in phrases if p["alias"].startswith("song|c0|")]
        self.assertEqual(len(a_windows), 2)
        self.assertNotEqual(a_windows[0]["id"], a_windows[1]["id"])  # pids
        kept = core._dedupe_ranked_by_clip(phrases)
        self.assertEqual(sorted(p["alias"].rsplit("|w", 1)[0] for p in kept),
                         ["song|c0", "song|c1"])

    def test_legacy_positional_ids_still_dedupe(self):
        ranked = [{"id": "song|c0|w0"}, {"id": "song|c0|w8"}, {"id": "song|c1|w0"}]
        self.assertEqual([p["id"] for p in core._dedupe_ranked_by_clip(ranked)],
                         ["song|c0|w0", "song|c1|w0"])



class BoothPickIdOnPidIndex(unittest.TestCase):
    def test_pid_and_legacy_ids_both_count_as_index_ids(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from build_develop_booth import _is_index_id
        from corpus_keys import phrase_lookup
        data = {"path": "/tmp/song.als", "tempo": 120.0,
                "clips": [_clip("a", [60, 62, 64, 65, 67, 69, 71, 72])]}
        phrases = list(iter_phrases(data))
        pid = phrases[0]["id"]
        self.assertNotIn("|", pid)                       # content hash
        lookup = phrase_lookup(phrases)
        self.assertTrue(_is_index_id(lookup, pid))
        self.assertTrue(_is_index_id(lookup, phrases[0]["alias"]))
        self.assertFalse(_is_index_id(lookup, "nope|c9|w0"))


if __name__ == "__main__":
    unittest.main()
