"""Ledger content matching on canonical readback notes (step 4).

Sent notes and Live readback disagree on purpose: Live sorts, dedups,
truncates same-pitch overlaps and clamps negative starts (pp_codec), while
the serving path stamps unsorted notes with runtime keys. Matching must
compare canonical content within readback tolerance — never names, tracks,
or fingerprints alone.
"""
import tempfile
import unittest
from pathlib import Path

from mcp_server import ledger


IDENTITY = {"set_id": "set-A", "runtime_epoch": "run-1", "file_path": "/tmp/a.als"}
OTHER_EPOCH = {"set_id": "set-A", "runtime_epoch": "run-2", "file_path": "/tmp/a.als"}
REF = {"clip_id": "clip-1", "track_id": "track-1", "track": "SIDEKICK JAM",
       "surface": "session", "path": "3/0"}

# Served shape: unsorted, one exact duplicate, one same-pitch overlap, one
# negative start, one velocity 0, runtime-only extra keys throughout.
MESSY = [
    {"pitch": 67, "start": 1.5, "duration": 0.5, "velocity": 80,
     "note_id": 7, "probability": 0.9, "release_velocity": 30},
    {"pitch": 60, "start": -0.5, "duration": 1.0, "velocity": 0, "note_id": 1},
    {"pitch": 64, "start": 1.0, "duration": 0.5, "velocity": 80, "note_id": 2},
    {"pitch": 64, "start": 1.0, "duration": 0.5, "velocity": 80, "note_id": 3},
    {"pitch": 67, "start": 1.7, "duration": 0.5, "velocity": 90, "note_id": 5},
    {"pitch": 72, "start": 2.0, "duration": 1.0, "velocity": 100, "note_id": 4,
     "mute": False},
]

# Canonical MESSY: (60, 0, 1.0, v1), (64, 1.0, 0.5, v80),
# (67, 1.5, 0.2, v80) truncated by the 1.7 overlap, (67, 1.7, 0.5, v90),
# (72, 2.0, 1.0, v100).
CANONICAL = [
    {"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 1},
    {"pitch": 64, "start": 1.0, "duration": 0.5, "velocity": 80},
    {"pitch": 67, "start": 1.5, "duration": 0.2, "velocity": 80},
    {"pitch": 67, "start": 1.7, "duration": 0.5, "velocity": 90},
    {"pitch": 72, "start": 2.0, "duration": 1.0, "velocity": 100},
]


def readback(jitter=0.0004):
    """Readback shape: sorted, no extras, start_beats keys, small jitter."""
    out = []
    for index, note in enumerate(CANONICAL):
        sign = 1.0 if index % 2 == 0 else -1.0
        out.append({"pitch": note["pitch"],
                    "start_beats": note["start"] + sign * jitter,
                    "duration_beats": note["duration"] - sign * jitter,
                    "velocity": note["velocity"]})
    return out


def inventory(identity, clips):
    return {"set_identity": identity, "complete": True, "clips": list(clips),
            "errors": []}


def clip(clip_id, notes, name="idea"):
    return {"clip_id": clip_id, "track_id": "track-1", "track": "SIDEKICK JAM",
            "surface": "session", "path": "4/0", "name": name,
            "answer_notes": notes}


class LedgerContentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "ledger.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write_keep(self, notes=MESSY):
        entry = ledger.record_write(clip_name="idea", slot="3/0", source="catalogue",
                                    set_identity=IDENTITY, clip_ref=dict(REF),
                                    answer_notes=notes, path=self.path)
        ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
        return entry

    def assertNotesAlmostEqual(self, left, right):
        self.assertEqual(len(left), len(right))
        for first, second in zip(left, right):
            self.assertEqual(first["pitch"], second["pitch"])
            self.assertEqual(first["velocity"], second["velocity"])
            self.assertAlmostEqual(first["start"], second["start"], places=9)
            self.assertAlmostEqual(first["duration"], second["duration"], places=9)

    def test_canonical_notes_drop_runtime_keys_and_clamp(self):
        self.assertNotesAlmostEqual(ledger.canonical_notes(MESSY), CANONICAL)
        self.assertEqual(ledger.canonical_notes(None), [])
        self.assertEqual(ledger.canonical_notes([]), [])

    def test_content_key_ignores_order_name_track_and_note_ids(self):
        # The key is exact (index/dedup only): stable across order and
        # runtime keys, but jitter near a truncation boundary may move it —
        # the equality test, never the key, decides a match.
        self.assertEqual(ledger.content_key(MESSY),
                         ledger.content_key(list(reversed(MESSY))))
        self.assertEqual(ledger.content_key(MESSY),
                         ledger.content_key([dict(n) for n in CANONICAL]))
        changed = [dict(CANONICAL[0], pitch=61)] + CANONICAL[1:]
        self.assertNotEqual(ledger.content_key(MESSY), ledger.content_key(changed))

    def test_content_equal_uses_readback_tolerance(self):
        self.assertTrue(ledger.content_equal(MESSY, readback(jitter=0.0004)))
        self.assertFalse(ledger.content_equal(MESSY, readback(jitter=0.002)))
        self.assertFalse(ledger.content_equal(MESSY, readback()[:-1]))

    def test_content_equal_compares_duration(self):
        # Same pitch, start and velocity but a held note is not a stab:
        # duration beyond tolerance is a mismatch.
        stab = [{"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90}]
        held = [{"pitch": 60, "start": 0.0, "duration": 0.6, "velocity": 90}]
        self.assertFalse(ledger.content_equal(stab, held))
        self.assertTrue(ledger.content_equal(stab, stab))

    def test_stored_fingerprint_ignores_name_and_track(self):
        first = ledger.record_write(clip_name="a", slot="3/0", source="catalogue",
                                    set_identity=IDENTITY, clip_ref=dict(REF),
                                    answer_notes=MESSY, path=self.path)
        second = ledger.record_write(clip_name="renamed", slot="3/1",
                                     source="catalogue", track="Song",
                                     set_identity=IDENTITY,
                                     clip_ref={**REF, "clip_id": "clip-2"},
                                     answer_notes=list(reversed(MESSY)),
                                     path=self.path)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(first["fingerprint"], ledger.content_key(MESSY))

    def test_messy_sent_notes_match_jittered_readback_cross_runtime(self):
        self.write_keep()
        result = ledger.close_session(
            inventory(OTHER_EPOCH, [clip("new-id", readback(), name="renamed")]),
            path=self.path)
        self.assertEqual(result["counts"]["keep"], 1)
        self.assertEqual(result["entries"][0]["match"], "unique_content_context")
        self.assertEqual(result["keep_rate"], 1.0)

    def test_jitter_beyond_tolerance_stays_unresolved(self):
        self.write_keep()
        result = ledger.close_session(
            inventory(OTHER_EPOCH, [clip("new-id", readback(jitter=0.002))]),
            path=self.path)
        self.assertEqual(result["counts"]["keep"], 0)
        self.assertEqual(len(result["unresolved"]), 1)
        # Step 8: the jittered clip still fails content matching, and a
        # five-note answer is too short for a near copy — the row stands on
        # its primary outcome alone, never scored as present.
        self.assertEqual(result["unresolved"][0]["reason"],
                         "cross_runtime_unresolved")
        self.assertEqual(result["adoption"]["candidates"], [])
        self.assertIsNone(result["keep_rate"])

    def test_two_identical_clips_stay_ambiguous(self):
        self.write_keep()
        notes = readback()
        result = ledger.close_session(
            inventory(OTHER_EPOCH, [clip("copy-a", notes), clip("copy-b", notes)]),
            path=self.path)
        self.assertEqual(result["counts"]["keep"], 0)
        self.assertEqual(result["unresolved"][0]["reason"],
                         "ambiguous_content_context")
        self.assertIsNone(result["keep_rate"])


if __name__ == "__main__":
    unittest.main()
