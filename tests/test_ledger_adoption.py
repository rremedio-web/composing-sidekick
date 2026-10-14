"""Adoption by copy: survival evidence when the row's own clip is gone (step 8).

A whole or contained copy of a generating gear's answer, at a musically
neutral transposition, adopts its row on its own; everything else — near
copies, odd transpositions, retrieval rows — becomes a candidate for the
owner to confirm, and collisions stay unresolved. Sidekick scaffolding
tracks (BOOTH/AUD/EARS) are never the song.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mcp_server import ledger


IDENTITY = {"set_id": "set-A", "runtime_epoch": "run-1", "file_path": "/tmp/a.als"}
REF = {"clip_id": "clip-1", "track_id": "track-1", "track": "SIDEKICK JAM",
       "surface": "session", "path": "3/0"}
# Six distinctive notes: six distinct pitches, uneven spacing (four distinct
# inter-onset intervals), no repeating motif — the minimum an answer needs
# for a copy to ever adopt its row on its own.
ANSWER = [{"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 90},
          {"pitch": 62, "start": 1.0, "duration": 0.5, "velocity": 80},
          {"pitch": 64, "start": 1.5, "duration": 0.5, "velocity": 80},
          {"pitch": 65, "start": 3.0, "duration": 0.5, "velocity": 80},
          {"pitch": 67, "start": 4.0, "duration": 0.5, "velocity": 80},
          {"pitch": 69, "start": 6.0, "duration": 0.5, "velocity": 80}]
# Eight notes for the near-copy floor: edits of shorter answers are never
# near copies.
EIGHT = ANSWER + [{"pitch": 71, "start": 7.0, "duration": 0.5, "velocity": 80},
                  {"pitch": 72, "start": 8.5, "duration": 0.5, "velocity": 80}]
OTHER_NOTES = [{"pitch": 67, "start": 0.0, "duration": 0.5, "velocity": 90},
               {"pitch": 71, "start": 1.0, "duration": 0.5, "velocity": 90}]

T0 = 1_000_000.0


def shifted(notes, semitones):
    return [{"pitch": n["pitch"] + semitones, "start": n["start"],
             "duration": n["duration"], "velocity": n["velocity"]} for n in notes]


def clip(clip_id, notes, track="BASS", track_id=None):
    return {"clip_id": clip_id, "track_id": track_id or ("tid-%s" % clip_id),
            "track": track, "surface": "session", "path": "4/0",
            "name": clip_id, "answer_notes": [dict(n) for n in notes]}


class AdoptionTierTests(unittest.TestCase):
    """Shape classification: onset+pitch only, drums never transpose."""

    def test_whole_exact_and_neutral_transposition(self):
        tier, t = ledger._adoption_tier(ledger.canonical_notes(ANSWER),
                                        ledger.canonical_notes(ANSWER), False)
        self.assertEqual((tier, t), ("whole_copy", 0))
        tier, t = ledger._adoption_tier(ledger.canonical_notes(ANSWER),
                                        ledger.canonical_notes(shifted(ANSWER, 12)), False)
        self.assertEqual((tier, t), ("whole_copy", 12))
        # +5 is a whole copy too, but never auto-adoptable (not a 12th)
        tier, t = ledger._adoption_tier(ledger.canonical_notes(ANSWER),
                                        ledger.canonical_notes(shifted(ANSWER, 5)), False)
        self.assertEqual((tier, t), ("whole_copy", 5))
        entry = {"role": "lead", "gear": 5, "answer_notes": [dict(n) for n in ANSWER],
                 "clip_ref": {"answer_notes": [dict(n) for n in ANSWER]}}
        self.assertTrue(ledger._auto_adoptable(entry, {"tier": "whole_copy",
                                                      "transposition": 0}))
        self.assertFalse(ledger._auto_adoptable(entry, {"tier": "whole_copy",
                                                         "transposition": 5}))

    def test_consolidated_loop_is_a_whole_copy(self):
        loop = ANSWER[:4]
        doubled = [dict(n) for n in loop]
        for repeat in range(1, 4):
            doubled.extend({**n, "start": n["start"] + 4.0 * repeat} for n in loop)
        tier, t = ledger._adoption_tier(ledger.canonical_notes(loop),
                                        ledger.canonical_notes(doubled), False)
        self.assertEqual((tier, t), ("whole_copy", 0))

    def test_near_copy_needs_eight_notes_and_three_quarter_overlap(self):
        # Near copies are a long-answer game: under eight notes, or under
        # three quarters of the answer, a heavy edit is no shape match.
        kept5, kept6 = EIGHT[:5], EIGHT[:6]
        self.assertEqual(ledger._adoption_tier(ledger.canonical_notes(EIGHT),
                                               ledger.canonical_notes(kept5), False),
                         (None, 0))
        self.assertEqual(ledger._adoption_tier(ledger.canonical_notes(EIGHT),
                                               ledger.canonical_notes(kept6), False),
                         ("near_copy", 0))

    def test_near_copy_only_at_neutral_transposition(self):
        # Six of eight notes kept but moved by five semitones: a heavy edit
        # at a non-neutral transposition is no near copy either.
        heavy = shifted(EIGHT[:6], 5)
        self.assertEqual(ledger._adoption_tier(ledger.canonical_notes(EIGHT),
                                               ledger.canonical_notes(heavy), False),
                         (None, 0))

    def test_anchor_uses_first_two_notes_symmetrically(self):
        # A copy missing the first note matches through the second anchor at
        # the neutral transposition — dropping either end reads the same.
        line = [{"pitch": pitch, "start": float(i), "duration": 0.5, "velocity": 80}
                for i, pitch in enumerate((60, 67, 63, 70, 58, 65, 62, 69))]
        entry_notes = ledger.canonical_notes(line)
        self.assertEqual(
            ledger._adoption_tier(entry_notes, ledger.canonical_notes(line[1:]),
                                  False), ("near_copy", 0))
        self.assertEqual(
            ledger._adoption_tier(entry_notes, ledger.canonical_notes(line[:-1]),
                                  False), ("near_copy", 0))

    def test_subset_inside_a_bigger_clip_is_contained(self):
        subset = ANSWER[:2]
        tier, t = ledger._adoption_tier(ledger.canonical_notes(subset),
                                        ledger.canonical_notes(ANSWER[:4]), False)
        self.assertEqual((tier, t), ("contained_copy", 0))

    def test_drums_never_transpose(self):
        drums = [{"pitch": 36, "start": 0.0, "duration": 0.25, "velocity": 100},
                 {"pitch": 38, "start": 1.0, "duration": 0.25, "velocity": 100},
                 {"pitch": 42, "start": 2.0, "duration": 0.25, "velocity": 100}]
        self.assertEqual(ledger._adoption_tier(ledger.canonical_notes(drums),
                                               ledger.canonical_notes(shifted(drums, 12)),
                                               True),
                         (None, 0))
        self.assertEqual(ledger._adoption_tier(ledger.canonical_notes(drums),
                                               ledger.canonical_notes(drums), True),
                         ("whole_copy", 0))


class AdoptionSweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "ledger.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, verdict=None, gear=5, clip_id="clip-1", notes=ANSWER,
              now=T0, name="idea", path=None, role="lead"):
        path = path or self.path
        ref = {**REF, "clip_id": clip_id, "answer_notes": [dict(n) for n in notes]}
        with patch.object(ledger, "_now", return_value=now):
            entry = ledger.record_write(
                clip_name=name, slot="3/0", source="catalogue", role=role,
                gear=gear, set_identity=IDENTITY, clip_ref=ref,
                answer_notes=[dict(n) for n in notes], path=path)
        if verdict is not None:
            with patch.object(ledger, "_now", return_value=now + 1):
                ledger.record_verdict(verdict, entry_id=entry["id"], path=path)
        return entry

    def sweep(self, clips=(), now=T0 + 120, path=None):
        with patch.object(ledger, "_now", return_value=now):
            return ledger.close_session(
                {"set_identity": IDENTITY, "complete": True, "errors": [],
                 "clips": list(clips)}, path=path or self.path)

    def row(self, entry_id, path=None):
        return next(e for e in ledger.load(path or self.path)["entries"]
                    if e["id"] == entry_id)

    def test_whole_copies_on_four_song_tracks_adopt(self):
        entry = self.write(verdict="keep")
        copies = [clip("copy-%s%d" % (track.lower(), t), shifted(ANSWER, t),
                       track=track)
                  for t, track in ((12, "BASS"), (-12, "KEYS"),
                                   (12, "LEAD"), (-12, "PAD"))]
        result = self.sweep(clips=copies)
        row = self.row(entry["id"])
        self.assertEqual(row["terminal_state"], "keep")
        self.assertEqual(row["presence"]["state"], "adopted")
        self.assertEqual(row["presence"]["adoption"]["kind"], "auto")
        self.assertEqual(row["presence"]["adoption"]["tier"], "whole_copy")
        self.assertEqual(sorted(row["presence"]["adoption"]["transpositions"]),
                         [-12, 12])
        self.assertEqual(len(row["presence"]["matched_clips"]), 4)
        self.assertEqual(result["rate_status"], "definitive")
        self.assertEqual(result["keep_rate"], 1.0)
        self.assertEqual(result["adoption"]["adopted"], 1)
        self.assertEqual(result["adoption"]["kept_in_jam"], 0)
        self.assertEqual(result["adoption"]["candidates"], [])
        self.assertEqual(len(result["adoption"]["destinations"][entry["id"]]), 4)

    def test_one_clip_never_keeps_two_nights(self):
        # Night 1's KEEP clip is deleted; night 2's own clip carries the same
        # content. The sweep is two-phase — primaries first with one shared
        # reservation — so night 1's adoption cannot steal night 2's clip:
        # one clip yields exactly one KEEP.
        night1 = self.write(verdict="keep", clip_id="gone-1", now=T0)
        night2 = self.write(verdict="keep", clip_id="song-x", now=T0 + 8 * 3600)
        sessions = ledger.load(self.path)["sessions"]
        self.assertEqual(len(sessions), 2)
        result = self.sweep(
            clips=[clip("song-x", ANSWER, track="KEYS")],
            now=T0 + 8 * 3600 + 120)
        self.assertEqual(result["counts"]["keep"], 1)
        first = self.row(night1["id"])
        second = self.row(night2["id"])
        self.assertEqual(second.get("terminal_state"), "keep")
        self.assertNotEqual(first.get("terminal_state"), "keep")
        keepers = [e for e in ledger.load(self.path)["entries"]
                   if e.get("terminal_state") == "keep"]
        self.assertEqual(len(keepers), 1)

    def test_pedal_tone_never_adopts_an_ostinato(self):
        # His older 16-note C ostinato is not survival of a 4-note C pedal
        # answer: one pitch is not distinctive enough to claim it.
        pedal = [{"pitch": 48, "start": float(i), "duration": 0.5, "velocity": 90}
                 for i in range(4)]
        ostinato = [{"pitch": 36, "start": float(i), "duration": 0.5, "velocity": 90}
                    for i in range(16)]
        entry = self.write(verdict="keep", clip_id="jam1", notes=pedal)
        result = self.sweep(clips=[clip("hisbass", ostinato, track="Bass")])
        row = self.row(entry["id"])
        self.assertEqual(result["adoption"]["adopted"], 0)
        self.assertNotEqual(row.get("terminal_state"), "keep")

    def test_single_voice_drum_figure_never_adopts(self):
        # Four-on-the-floor kick against his long kick clip: drums adopt
        # only with two voices and six onsets behind them.
        kicks = [{"pitch": 36, "start": float(i), "duration": 0.25, "velocity": 100}
                 for i in range(4)]
        long_kicks = [{"pitch": 36, "start": float(i), "duration": 0.25, "velocity": 100}
                      for i in range(64)]
        entry = self.write(verdict="keep", clip_id="jam1", notes=kicks,
                           role="drum")
        result = self.sweep(clips=[clip("hiskick", long_kicks, track="Drums")])
        row = self.row(entry["id"])
        self.assertEqual(result["adoption"]["adopted"], 0)
        self.assertNotEqual(row.get("terminal_state"), "keep")

    def test_generic_kick_hat_groove_is_candidate_only(self):
        """fix2 item 3 (repro correct/x3.py): a one-bar kick + 8th-hat
        groove is a contained copy of ANY song's 16-bar groove on the same
        grid. Drums adopt on their own only when the pattern is NOT a
        short-cell repetition and carries six or more DISTINCT onsets."""
        bar = []
        for i in range(8):
            bar.append({"pitch": 42, "start": i * 0.5, "duration": 0.1,
                        "velocity": 90})
            if i % 2 == 0:
                bar.append({"pitch": 36, "start": i * 0.5, "duration": 0.1,
                            "velocity": 100})
        groove = [dict(n, start=n["start"] + 4 * b)
                  for b in range(16) for n in bar]
        entry = self.write(verdict="keep", clip_id="jamdrum", notes=bar,
                           role="drum")
        result = self.sweep(clips=[clip("hisgroove", groove, track="Drums")])
        self.assertEqual(result["adoption"]["adopted"], 0)
        candidates = result["adoption"]["candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["clips"][0]["clip_id"], "hisgroove")
        row = self.row(entry["id"])
        self.assertEqual(row["resolution_reason"], "adoption_candidate")
        self.assertNotEqual(row.get("terminal_state"), "keep")
        self.assertEqual(result["rate_status"], "partial")

    def test_distinctive_drum_pattern_still_adopts(self):
        # The same gate, other side: two voices, seven distinct onsets and
        # no cell of a beat or less repeating — the copy still adopts.
        pattern = [
            {"pitch": 36, "start": 0.0, "duration": 0.1, "velocity": 100},
            {"pitch": 42, "start": 0.75, "duration": 0.1, "velocity": 90},
            {"pitch": 36, "start": 2.0, "duration": 0.1, "velocity": 100},
            {"pitch": 42, "start": 2.5, "duration": 0.1, "velocity": 90},
            {"pitch": 38, "start": 3.25, "duration": 0.1, "velocity": 95},
            {"pitch": 42, "start": 4.5, "duration": 0.1, "velocity": 90},
            {"pitch": 36, "start": 5.0, "duration": 0.1, "velocity": 100},
        ]
        entry = self.write(verdict="keep", clip_id="jamdrum", notes=pattern,
                           role="drum")
        result = self.sweep(clips=[clip("hisbeat", pattern, track="Drums")])
        self.assertEqual(result["adoption"]["adopted"], 1)
        self.assertEqual(self.row(entry["id"])["terminal_state"], "keep")
        self.assertEqual(result["keep_rate"], 1.0)

    def test_uniform_figure_inside_a_longer_lead_never_adopts(self):
        # His 32-note lead already contains the 6-note figure, played evenly:
        # a single inter-onset interval is not distinctive enough to claim it.
        figure = [{"pitch": pitch, "start": i * 0.5, "duration": 0.5, "velocity": 90}
                  for i, pitch in enumerate([60, 63, 65, 67, 70, 72])]
        lead = ([{"pitch": pitch, "start": i * 0.5, "duration": 0.5, "velocity": 90}
                 for i, pitch in enumerate([55, 57, 58, 50, 52, 53])] +
                [{**n, "start": n["start"] + 3.0} for n in figure] +
                [{"pitch": pitch, "start": 6.0 + i * 0.5, "duration": 0.5,
                  "velocity": 90}
                 for i, pitch in enumerate([48, 50, 51, 53] * 5)])
        entry = self.write(verdict="keep", clip_id="jam1", notes=figure)
        result = self.sweep(clips=[clip("hislead", lead, track="Lead")])
        row = self.row(entry["id"])
        self.assertEqual(result["adoption"]["adopted"], 0)
        self.assertNotEqual(row.get("terminal_state"), "keep")

    def test_whole_copy_across_mixed_transpositions_never_adopts(self):
        # K stacks the answer at t=0 and again at t=+5: no single
        # transposition covers the whole clip, so it is never a whole copy.
        base = [{"pitch": pitch, "start": float(i), "duration": 0.5, "velocity": 90}
                for i, pitch in enumerate([60, 62, 64, 67])]
        stacked = base + [{"pitch": n["pitch"] + 5, "start": n["start"] + 4.0,
                           "duration": 0.5, "velocity": 90} for n in base]
        entry = self.write(verdict="keep", clip_id="jam1", notes=base)
        result = self.sweep(clips=[clip("song", stacked, track="Keys")])
        row = self.row(entry["id"])
        self.assertEqual(result["adoption"]["adopted"], 0)
        self.assertNotEqual(row.get("terminal_state"), "keep")
        for candidate in row.get("adoption_candidates", []):
            self.assertNotEqual(candidate["tier"], "whole_copy")

    def test_verdictless_row_adopts_as_unjudged(self):
        # A verdict-less row whose copy survives adopts without a verdict:
        # adopted_unjudged counts it, and it never withholds the rate.
        entry = self.write(clip_id="gone-1")
        result = self.sweep(clips=[clip("exact", ANSWER, track="BASS")])
        row = self.row(entry["id"])
        self.assertEqual(row["presence"]["state"], "adopted")
        self.assertEqual(row["terminal_state"], "unjudged")
        self.assertEqual(result["adoption"]["adopted_unjudged"], 1)
        self.assertEqual(result["rate_status"], "definitive")
        self.assertIsNone(result["keep_rate"])

    def test_consolidated_loop_adopts_through_a_real_sweep(self):
        # Four repeats of a short distinctive loop are a whole copy that
        # adopts its row on its own.
        loop = [{"pitch": pitch, "start": start, "duration": 0.5, "velocity": 90}
                for pitch, start in zip([60, 62, 64, 65],
                                        [0.0, 1.0, 1.5, 3.0])]
        song = [dict(n, start=n["start"] + 4.0 * rep) for rep in range(4)
                for n in loop]
        entry = self.write(verdict="keep", clip_id="gone-1", notes=loop)
        result = self.sweep(clips=[clip("song", song, track="KEYS")])
        row = self.row(entry["id"])
        self.assertEqual(row["presence"]["state"], "adopted")
        self.assertEqual(row["presence"]["match"], "whole_copy")
        self.assertEqual(row["terminal_state"], "keep")
        self.assertEqual(result["adoption"]["adopted"], 1)
        self.assertEqual(result["rate_status"], "definitive")

    def test_three_note_whole_copy_stays_a_candidate(self):
        # Whole-copy auto-adoption needs four answer notes: a three-note
        # answer with an exact copy stays a candidate. The trio keeps a
        # varied rhythm so it clears the distinctiveness gate — only the
        # four-note floor may hold it back.
        trio = [{"pitch": pitch, "start": start, "duration": 0.5,
                 "velocity": 90}
                for pitch, start in zip([60, 64, 67], [0.0, 1.0, 1.5])]
        entry = self.write(verdict="keep", clip_id="gone-1", notes=trio)
        result = self.sweep(clips=[clip("exact", trio, track="BASS")])
        row = self.row(entry["id"])
        self.assertEqual(result["adoption"]["adopted"], 0)
        self.assertEqual(row["resolution_reason"], "adoption_candidate")
        self.assertIsNone(row.get("terminal_state"))

    def test_two_onset_drum_copy_stays_a_candidate(self):
        # Contained copies need three distinct onsets: a six-hit two-voice
        # drum part over two stabs stays a candidate even when the drum
        # gate (two voices, six onsets) is met.
        stabs = ([{"pitch": pitch, "start": 0.0, "duration": 0.1,
                   "velocity": 100} for pitch in (36, 42, 38)] +
                 [{"pitch": pitch, "start": 0.5, "duration": 0.1,
                   "velocity": 100} for pitch in (36, 42, 38)])
        song = stabs + [{"pitch": 36, "start": 1.0, "duration": 0.1,
                         "velocity": 80}]
        entry = self.write(verdict="keep", clip_id="gone-1", notes=stabs,
                           role="drum")
        result = self.sweep(clips=[clip("song", song, track="Drums")])
        row = self.row(entry["id"])
        self.assertEqual(result["adoption"]["adopted"], 0)
        self.assertEqual(row["resolution_reason"], "adoption_candidate")
        self.assertIsNone(row.get("terminal_state"))

    def test_four_note_distinctive_contained_copy_stays_a_candidate(self):
        # Contained copies need six answer notes: a short distinctive
        # figure inside a longer clip stays a candidate.
        figure = [{"pitch": pitch, "start": start, "duration": 0.5,
                   "velocity": 90}
                  for pitch, start in zip([60, 62, 64, 67],
                                          [0.0, 1.0, 1.5, 3.0])]
        song = figure + [{"pitch": 72, "start": 4.0, "duration": 0.5,
                          "velocity": 90},
                         {"pitch": 74, "start": 5.0, "duration": 1.0,
                          "velocity": 90}]
        entry = self.write(verdict="keep", clip_id="gone-1", notes=figure)
        result = self.sweep(clips=[clip("song", song, track="KEYS")])
        row = self.row(entry["id"])
        self.assertEqual(result["adoption"]["adopted"], 0)
        self.assertEqual(row["resolution_reason"], "adoption_candidate")
        self.assertIsNone(row.get("terminal_state"))

    def test_extras_heavy_edit_is_no_copy_at_all(self):
        # Six of eight notes kept but swimming in extras: above the extra
        # bound, the edit is no shape match and the row stands alone.
        edit = (EIGHT[:6] +
                [{"pitch": 40 + i, "start": 0.25 * i, "duration": 0.25,
                  "velocity": 60} for i in range(10)])
        entry = self.write(verdict="keep", clip_id="gone-1", notes=EIGHT)
        result = self.sweep(clips=[clip("edit", edit, track="BASS")])
        row = self.row(entry["id"])
        self.assertEqual(result["adoption"]["adopted"], 0)
        self.assertNotIn("adoption_candidates", row)
        self.assertEqual(row["terminal_state"], "drop")

    def test_sticky_claim_blocks_later_adoption(self):
        # Row A matched clip X in an earlier sweep; a later row with the
        # same content cannot adopt X away from it.
        entry_a = self.write(verdict="keep", clip_id="x", notes=ANSWER,
                             now=T0)
        self.write(name="ambiguous", clip_id="clip-b", notes=OTHER_NOTES,
                   verdict="keep", now=T0 + 2)
        self.sweep(clips=[clip("x", ANSWER),
                           clip("amb-1", OTHER_NOTES), clip("amb-2", OTHER_NOTES)],
                   now=T0 + 60)
        self.assertEqual(self.row(entry_a["id"])["presence"]["state"],
                         "present")
        entry_c = self.write(name="late", clip_id="gone-c", notes=ANSWER,
                             verdict="keep", now=T0 + 70)
        result = self.sweep(clips=[clip("x", ANSWER),
                                    clip("amb-1", OTHER_NOTES),
                                    clip("amb-2", OTHER_NOTES)],
                            now=T0 + 120)
        row_c = self.row(entry_c["id"])
        self.assertNotEqual(row_c.get("terminal_state"), "keep")
        self.assertEqual(result["counts"]["keep"], 1)

    def test_explicit_link_through_alias_matches_in_sweep(self):
        # An explicit link recorded under the saved Set matches a sweep of
        # the pre-save Set: the chain makes the link apply. The row is
        # gear 2 (retrieval), whose copies never auto-adopt, so the link
        # is the only path to adoption here.
        unsaved = {"set_id": "unsaved:run-1", "runtime_epoch": "run-1",
                   "file_path": None}
        saved = {"set_id": "/music/a.als", "runtime_epoch": "run-1",
                 "file_path": "/music/a.als"}
        other_notes = [{"pitch": 67, "start": 0.0, "duration": 0.5,
                        "velocity": 90},
                       {"pitch": 71, "start": 1.0, "duration": 0.5,
                        "velocity": 90}]
        with patch.object(ledger, "_now", return_value=T0):
            entry = ledger.record_write(
                clip_name="idea", slot="3/0", source="catalogue", role="lead",
                gear=2, set_identity=unsaved,
                clip_ref={**REF, "clip_id": "gone",
                          "answer_notes": [dict(n) for n in ANSWER]},
                answer_notes=[dict(n) for n in ANSWER], path=self.path)
            ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
            # a post-save write puts the saved id in the session chain
            ledger.record_write(
                clip_name="later", slot="3/1", source="catalogue", role="lead",
                gear=5, set_identity=saved,
                clip_ref={**REF, "clip_id": "clip-9",
                          "answer_notes": [dict(n) for n in other_notes]},
                answer_notes=[dict(n) for n in other_notes], path=self.path)
        destination = clip("song-x", ANSWER, track="KEYS")
        ledger.adopt_entry(entry["id"], destination, set_identity=unsaved,
                           path=self.path)
        # He reworks the adopted clip after linking: the sweep sees the
        # same clip id with different notes, so neither the content loop
        # nor the tier scan can adopt — only the id-based link applies.
        edited = clip("song-x", OTHER_NOTES, track="KEYS")
        result = ledger.close_session(
            {"set_identity": saved, "complete": True, "errors": [],
             "clips": [edited]}, path=self.path)
        row = self.row(entry["id"])
        # .get() reads: without the chain clause the link is ignored and
        # the row ends alias_unconfirmed with no presence — that must
        # FAIL here, not raise.
        self.assertEqual((row.get("presence") or {}).get("state"), "adopted")
        self.assertEqual(row.get("resolution_match"), "explicit_adoption")
        self.assertEqual(result["counts"]["keep"], 1)

    def test_short_answer_heavy_edit_is_no_copy_at_all(self):
        # Six-note answers never yield near copies: a 5-of-6 heavy edit is
        # no shape match, so the row stands on its primary outcome alone.
        base = [{"pitch": pitch, "start": start, "duration": 0.5, "velocity": 90}
                for pitch, start in zip([60, 62, 64, 67, 70, 72],
                                        [0.0, 1.0, 1.5, 3.0, 4.0, 6.0])]
        entry = self.write(verdict="keep", clip_id="jam1", notes=base)
        result = self.sweep(clips=[clip("edit", base[:5], track="BASS")])
        row = self.row(entry["id"])
        self.assertEqual(result["adoption"]["adopted"], 0)
        self.assertNotIn("adoption_candidates", row)
        self.assertEqual(row["terminal_state"], "drop")

    def test_gear_three_copy_stays_a_candidate(self):
        # Gear 3 is retrieval + surgery: its copies could coincide with his
        # pre-existing clips, so they never adopt on their own.
        entry = self.write(verdict="keep", gear=3, clip_id="jam1")
        result = self.sweep(clips=[clip("exact", ANSWER, track="BASS")])
        row = self.row(entry["id"])
        self.assertEqual(result["adoption"]["adopted"], 0)
        self.assertEqual(row["resolution_reason"], "adoption_candidate")
        self.assertIsNone(row.get("terminal_state"))

    def test_jam_track_copy_counts_as_kept_in_jam(self):
        entry = self.write(verdict="keep")
        result = self.sweep(clips=[clip("jam-copy", ANSWER, track="SIDEKICK JAM")])
        row = self.row(entry["id"])
        self.assertEqual(row["terminal_state"], "keep")
        self.assertEqual(row["presence"]["state"], "jam_copy")
        self.assertEqual(result["adoption"]["jam_copy"], 1)
        self.assertEqual(result["adoption"]["kept_in_jam"], 1)
        self.assertEqual(result["adoption"]["adopted"], 0)

    def test_odd_transposition_stays_a_candidate(self):
        entry = self.write(verdict="keep")
        result = self.sweep(clips=[clip("copy+5", shifted(ANSWER, 5), track="BASS")])
        row = self.row(entry["id"])
        self.assertIsNone(row["terminal_state"])
        self.assertIsNone(row.get("provisional_state"))
        self.assertEqual(row["resolution_reason"], "adoption_candidate")
        self.assertEqual(row["presence"]["state"], "absent")
        self.assertEqual(row["presence"]["adoption"], {"kind": "candidate"})
        self.assertEqual(row["adoption_candidates"][0]["tier"], "whole_copy")
        self.assertEqual(row["adoption_candidates"][0]["transposition"], 5)
        self.assertEqual(result["rate_status"], "partial")
        self.assertIn("withheld", result["warning"])
        self.assertEqual(result["adoption"]["candidates"][0]["entry_id"],
                         entry["id"])

    def test_retrieval_gear_copy_stays_a_candidate(self):
        entry = self.write(verdict="keep", gear=2)
        result = self.sweep(clips=[clip("exact", ANSWER, track="BASS")])
        row = self.row(entry["id"])
        self.assertEqual(row["resolution_reason"], "adoption_candidate")
        self.assertEqual(row["adoption_candidates"][0]["tier"], "whole_copy")
        self.assertIsNone(row["terminal_state"])
        self.assertEqual(result["adoption"]["candidates"][0]["clips"][0]["clip_id"],
                         "exact")

    def test_sidekick_booth_copy_is_ignored(self):
        entry = self.write(verdict="keep")
        result = self.sweep(clips=[clip("booth-copy", ANSWER, track="SIDEKICK BOOTH")])
        row = self.row(entry["id"])
        # BOOTH is sidekick scaffolding, not the song: the row's own clip is
        # verified gone, so the KEEP drops — never rescued by scaffolding.
        self.assertEqual(row["terminal_state"], "drop")
        self.assertEqual(row["presence"]["state"], "absent")
        self.assertNotIn("adoption_candidates", row)
        self.assertEqual(result["adoption"],
                         {"kept_in_jam": 0, "jam_copy": 0, "adopted": 0,
                          "adopted_unjudged": 0, "candidates": [], "destinations": {}})

    def test_identical_entries_collide_on_one_copy(self):
        first = self.write(verdict="keep", clip_id="clip-a", name="a")
        second = self.write(verdict="keep", clip_id="clip-b", name="b")
        result = self.sweep(clips=[clip("the-copy", ANSWER, track="BASS")])
        for entry in (first, second):
            row = self.row(entry["id"])
            # .get() reads: without the first-wins guard one row would win
            # the clip outright, and that must FAIL here, not raise.
            self.assertEqual(row.get("resolution_reason"),
                             "adoption_collision")
            self.assertIsNone(row.get("terminal_state"))
            candidates = row.get("adoption_candidates") or []
            self.assertEqual(candidates[0]["clip_id"] if candidates else None,
                             "the-copy")
            self.assertEqual((row.get("presence") or {}).get("state"),
                             "absent")
        self.assertEqual(result["rate_status"], "partial")
        self.assertEqual(len(result["adoption"]["candidates"]), 2)
        self.assertIn("withheld", result["warning"])

    def test_whole_copy_beats_subset_entry(self):
        whole = self.write(verdict="keep", clip_id="clip-w", notes=ANSWER[:4],
                           name="w")
        subset = self.write(verdict="keep", clip_id="clip-s", notes=ANSWER[:2],
                            name="s")
        result = self.sweep(clips=[clip("big", ANSWER[:4], track="BASS")])
        whole_row, subset_row = self.row(whole["id"]), self.row(subset["id"])
        self.assertEqual(whole_row["presence"]["state"], "adopted")
        self.assertEqual(whole_row["provisional_state"], "keep")
        self.assertEqual(subset_row["resolution_reason"], "adoption_candidate")
        self.assertIsNone(subset_row["terminal_state"])
        # the open subset candidate withholds the night: no terminal states yet
        self.assertEqual(result["rate_status"], "partial")
        self.assertIsNone(whole_row["terminal_state"])
        self.assertEqual(result["adoption"]["adopted"], 1)
        self.assertEqual(len(result["adoption"]["candidates"]), 1)

    def test_clip_reserved_by_present_entry_is_never_adopted(self):
        present = self.write(verdict="keep", clip_id="clip-a", name="a")
        twin = self.write(verdict="keep", clip_id="clip-b", name="b")  # same notes
        result = self.sweep(clips=[clip("clip-a", ANSWER, track="SIDEKICK JAM")])
        present_row, twin_row = self.row(present["id"]), self.row(twin["id"])
        self.assertEqual(present_row["terminal_state"], "keep")
        self.assertEqual(present_row["presence"]["state"], "present")
        # the twin's own clip is gone and the surviving clip already belongs
        # to the present row: no candidate, verified absence, KEEP drops
        self.assertEqual(twin_row["terminal_state"], "drop")
        self.assertNotIn("adoption_candidates", twin_row)
        self.assertEqual(result["adoption"]["candidates"], [])

    def test_explicit_adoption_link_is_alias_aware(self):
        # A pre-save KEEP adopted after the first save: the link's Set is in
        # the entry session's chain, so adopt_entry accepts it.
        unsaved = {"set_id": "unsaved:run-1", "runtime_epoch": "run-1",
                   "file_path": None}
        saved = {"set_id": "/music/a.als", "runtime_epoch": "run-1",
                 "file_path": "/music/a.als"}
        with patch.object(ledger, "_now", return_value=T0):
            entry = ledger.record_write(
                clip_name="idea", slot="3/0", source="catalogue", role="lead",
                gear=5, set_identity=unsaved,
                clip_ref={**REF, "answer_notes": [dict(n) for n in ANSWER]},
                answer_notes=[dict(n) for n in ANSWER], path=self.path)
        with patch.object(ledger, "_now", return_value=T0 + 1):
            ledger.record_verdict("keep", entry_id=entry["id"], path=self.path)
            ledger.record_write(
                clip_name="later", slot="3/1", source="catalogue", role="lead",
                gear=5, set_identity=saved,
                clip_ref={**REF, "clip_id": "clip-2",
                          "answer_notes": [dict(n) for n in ANSWER]},
                answer_notes=[dict(n) for n in ANSWER], path=self.path)
        destination = clip("song-x", ANSWER, track="KEYS")
        linked = ledger.adopt_entry(entry["id"], destination, set_identity=saved,
                                    path=self.path)
        self.assertEqual(linked["adoption_links"][-1]["set_identity"], saved)
        data = ledger.load(self.path)
        session = next(s for s in data["sessions"]
                       if s["id"] == entry["session_id"])
        self.assertIn(saved["set_id"], ledger._session_chain(session))

    def test_candidate_then_explicit_adoption_link_scores_keep(self):
        entry = self.write(verdict="keep", gear=2)
        destination = clip("exact", ANSWER, track="BASS")
        first = self.sweep(clips=[destination])
        self.assertEqual(self.row(entry["id"])["resolution_reason"],
                         "adoption_candidate")
        ledger.adopt_entry(entry["id"], destination, set_identity=IDENTITY,
                           path=self.path)
        second = self.sweep(clips=[destination], now=T0 + 240)
        row = self.row(entry["id"])
        self.assertEqual(row["terminal_state"], "keep")
        self.assertEqual(row["presence"]["state"], "adopted")
        self.assertEqual(row["presence"]["match"], "explicit_adoption")
        self.assertNotIn("adoption_candidates", row)
        self.assertEqual(second["rate_status"], "definitive")
        self.assertEqual(second["keep_rate"], 1.0)
        self.assertEqual(second["adoption"]["candidates"], [])
        self.assertEqual(len(ledger.load(self.path)["adoptions"]), 1)

    def test_explicit_link_never_pops_sticky_present(self):
        # The row's own clip already proved survival: an explicit link is
        # recorded, but the sticky present presence stands.
        keep1 = self.write(verdict="keep", clip_id="c1", now=T0)
        self.write(name="ambiguous", clip_id="clip-2",
                   notes=OTHER_NOTES, verdict="keep", now=T0 + 2)
        self.sweep(clips=[clip("c1", ANSWER), clip("amb-1", OTHER_NOTES),
                           clip("amb-2", OTHER_NOTES)], now=T0 + 60)
        self.assertEqual(self.row(keep1["id"])["presence"]["state"], "present")
        destination = clip("song-x", ANSWER, track="KEYS")
        ledger.adopt_entry(keep1["id"], destination, set_identity=IDENTITY,
                           path=self.path)
        self.sweep(clips=[clip("c1", ANSWER), clip("amb-1", OTHER_NOTES),
                           clip("amb-2", OTHER_NOTES), destination],
                   now=T0 + 120)
        presence = self.row(keep1["id"])["presence"]
        self.assertEqual(presence["state"], "present")
        self.assertEqual(presence["matched_clips"][0]["clip_id"], "c1")
        links = self.row(keep1["id"])["adoption_links"]
        self.assertEqual(links[-1]["destination_ref"]["clip_id"], "song-x")

    def test_not_adopted_row_returns_for_a_terminal_state(self):
        # A declined candidate stays on the verified path: the next sweep
        # lands its terminal state and the scoreboard counts it.
        entry = self.write(verdict="keep", gear=2)
        self.sweep(clips=[clip("exact", ANSWER, track="BASS")])
        self.assertEqual(self.row(entry["id"])["resolution_reason"],
                         "adoption_candidate")
        session_id = ledger.load(self.path)["sessions"][0]["id"]
        ledger.attest(session_id, {entry["id"]: "not_adopted"},
                      confirm=True, path=self.path)
        needing = ledger.sessions_needing_sweep(ledger.load(self.path))
        self.assertEqual([group["session"]["id"] for group in needing],
                         [session_id])
        result = self.sweep(clips=[clip("exact", ANSWER, track="BASS")],
                            now=T0 + 240)
        row = self.row(entry["id"])
        self.assertEqual(row["terminal_state"], "drop")
        self.assertEqual(result["rate_status"], "definitive")
        scoreboard = ledger.summary(path=self.path)["scoreboard"]
        self.assertEqual(scoreboard["verified"]["judged"], 1)
        self.assertEqual(scoreboard["verified"]["keep"], 0)
        self.assertEqual(scoreboard["verified"]["keep_rate"], 0.0)

    def test_not_adopted_declines_a_scan_candidate(self):
        entry = self.write(verdict="keep", gear=2)
        self.sweep(clips=[clip("exact", ANSWER, track="BASS")])
        session_id = ledger.load(self.path)["sessions"][0]["id"]
        result = ledger.attest(session_id, {entry["id"]: "not_adopted"},
                               confirm=True, path=self.path)
        row = self.row(entry["id"])
        self.assertNotIn("adoption_candidates", row)
        self.assertIsNone(row["presence"]["adoption"])
        self.assertEqual(row["provisional_state"], "drop")
        self.assertEqual(row["attestation"]["fact"], "not_adopted")
        # declined candidates leave the row verified, never attested-stratum
        self.assertEqual(result["counts"]["drop"], 1)
        self.assertEqual(result["attested"]["judged"], 0)

    def test_four_hundred_clip_sweep_stays_fast(self):
        answers = [
            [dict(n) for n in ANSWER],
            [{"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 80},
             {"pitch": 63, "start": 1.0, "duration": 0.5, "velocity": 80},
             {"pitch": 67, "start": 1.5, "duration": 0.5, "velocity": 80},
             {"pitch": 70, "start": 3.0, "duration": 0.5, "velocity": 80},
             {"pitch": 74, "start": 4.0, "duration": 0.5, "velocity": 80},
             {"pitch": 77, "start": 6.0, "duration": 0.5, "velocity": 80}],
            [{"pitch": 55, "start": 0.0, "duration": 0.5, "velocity": 80},
             {"pitch": 56, "start": 1.0, "duration": 0.5, "velocity": 80},
             {"pitch": 60, "start": 1.5, "duration": 0.5, "velocity": 80},
             {"pitch": 61, "start": 3.0, "duration": 0.5, "velocity": 80},
             {"pitch": 65, "start": 4.0, "duration": 0.5, "velocity": 80},
             {"pitch": 66, "start": 6.0, "duration": 0.5, "velocity": 80}],
        ]
        for index, notes in enumerate(answers):
            self.write(verdict="keep", clip_id="gone-%d" % index, notes=notes,
                       name="gone%d" % index)
        clips = [clip("copy-%d" % i, shifted(notes, 12), track="TRACK-%d" % i)
                 for i, notes in enumerate(answers)]
        # 397 fillers with spacing-3 pitch pairs: no two row notes can ever
        # land on two filler notes under one transposition.
        for i in range(397):
            base = 24 + (i % 3)
            filler = [{"pitch": base + 3 * j, "start": 0.37 * j + (i % 7) * 0.11,
                       "duration": 0.25, "velocity": 64} for j in range(4)]
            clips.append(clip("filler-%d" % i, filler, track="FILLER-%d" % i))
        started = time.monotonic()
        result = self.sweep(clips=clips)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertEqual(result["adoption"]["adopted"], 3)
        self.assertEqual(result["adoption"]["candidates"], [])
        self.assertEqual(result["keep_rate"], 1.0)

    def test_big_set_sweep_stays_fast(self):
        # 20 rows x 30 tracks x 8 clips with 4 long (640-note) drum clips,
        # plus 300 historical rows: one sweep must stay under 6 s. Wall
        # time is flaky on a loaded machine (JSON load/save included), so
        # the regression guard is deterministic: the vote-then-verify
        # matcher may run the exact onset matcher at most 8,000 times on
        # this fixture (6,300 today) — trying every offset instead of
        # voting explodes that count by orders of magnitude.
        import random as _random

        random = _random.Random(20260924)

        def rnd(notes, span=16.0):
            return [{"pitch": random.randrange(48, 72),
                     "start": round(random.randrange(int(span * 4)) / 4, 3),
                     "duration": 0.25, "velocity": 90}
                    for _ in range(notes)]

        def drumclip(notes):
            return [{"pitch": 36 if i % 2 == 0 else 42,
                     "start": round(i * 0.125, 3), "duration": 0.1,
                     "velocity": 100 if i % 2 == 0 else 90}
                    for i in range(notes)]

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "l.json")
            data = ledger._empty()
            data["sessions"].append(
                {"id": "s-old", "closed_at": "2026-01-01T00:00:00Z",
                 "closed_by": "sweep", "rate_status": "definitive",
                 "set_identity": IDENTITY, "last_activity": 0})
            for i in range(300):
                notes = rnd(32)
                matched = clip("old%d" % i, notes)
                data["entries"].append(
                    {"id": "e-o%d" % i, "session_id": "s-old",
                     "terminal_state": "keep", "set_identity": IDENTITY,
                     "presence": {"state": "present", "matched_clips": [matched],
                                  "swept_at": "x"},
                     "matched_clip": matched,
                     "matched_inventory_identity": {"runtime_epoch": "old"}})
            Path(path).write_text(json.dumps(data))
            with patch.object(ledger, "_now", return_value=T0):
                for i in range(20):
                    drum = i % 4 == 0
                    notes = drumclip(16) if drum else rnd(32, 8.0)
                    ref = {**REF, "clip_id": "gone-%d" % i,
                           "answer_notes": [dict(n) for n in notes]}
                    entry = ledger.record_write(
                        clip_name="gone-%d" % i, slot="3/%d" % i,
                        source="catalogue", role="drum" if drum else "lead",
                        gear=5, set_identity=IDENTITY, clip_ref=ref,
                        answer_notes=[dict(n) for n in notes], path=path)
                    if i % 2 == 0:
                        with patch.object(ledger, "_now", return_value=T0 + 1):
                            ledger.record_verdict("keep", entry_id=entry["id"],
                                                  path=path)
            clips = []
            for track in range(30):
                for slot in range(8):
                    if track < 4 and slot == 0:
                        notes = drumclip(640)
                    else:
                        notes = rnd(48)
                    clips.append(clip("k%d_%d" % (track, slot), notes,
                                      track="T%d" % track))
            matcher_calls = {"n": 0}
            exact_matcher = ledger._match_onsets

            def counting_matcher(*args, **kwargs):
                matcher_calls["n"] += 1
                return exact_matcher(*args, **kwargs)

            with patch.object(ledger, "_now", return_value=T0 + 100), \
                    patch.object(ledger, "_match_onsets", counting_matcher):
                started = time.monotonic()
                result = ledger.close_session(
                    {"set_identity": IDENTITY, "complete": True, "errors": [],
                     "clips": clips}, path=path)
                elapsed = time.monotonic() - started
            self.assertTrue(result["ok"])
            self.assertLess(elapsed, 6.0)
            self.assertGreater(matcher_calls["n"], 0,
                               "the counter no longer sees the matcher — "
                               "this guard is dead, fix its wiring")
            self.assertLessEqual(matcher_calls["n"], 8000,
                                 "the vote-then-verify matcher regressed to "
                                 "verifying without voting (%d exact-matcher calls)"
                                 % matcher_calls["n"])


class AdoptAnswerAliasTests(unittest.IsolatedAsyncioTestCase):
    async def test_adopt_answer_works_across_first_save(self):
        from mcp_server import live_write, server

        unsaved = {"set_id": "unsaved:run-1", "runtime_epoch": "run-1",
                   "file_path": None}
        saved = {"set_id": "/music/a.als", "runtime_epoch": "run-1",
                 "file_path": "/music/a.als"}
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ledger.json")
            with patch.object(ledger, "_now", return_value=T0):
                entry = ledger.record_write(
                    clip_name="idea", slot="3/0", source="catalogue", role="lead",
                    gear=5, set_identity=unsaved,
                    clip_ref={**REF, "answer_notes": [dict(n) for n in ANSWER]},
                    answer_notes=[dict(n) for n in ANSWER], path=path)
            with patch.object(ledger, "_now", return_value=T0 + 1):
                ledger.record_verdict("keep", entry_id=entry["id"], path=path)
                ledger.record_write(
                    clip_name="later", slot="3/1", source="catalogue", role="lead",
                    gear=5, set_identity=saved,
                    clip_ref={**REF, "clip_id": "clip-2",
                              "answer_notes": [dict(n) for n in ANSWER]},
                    answer_notes=[dict(n) for n in ANSWER], path=path)
            destination = {"clip_id": "song-x", "track_id": "tid-song-x",
                           "track": "KEYS", "surface": "session", "path": "4/0",
                           "name": "song-x",
                           "answer_notes": [dict(n) for n in ANSWER]}
            inventory = {"set_identity": saved, "complete": True, "errors": [],
                         "clips": [destination]}
            with patch.object(ledger, "LEDGER_PATH", path), \
                    patch.object(live_write, "read_set_inventory",
                                 AsyncMock(return_value=inventory)):
                result = await server.mcp.call_tool(
                    "adopt_answer", {"entry_id": entry["id"],
                                     "clip_id": "song-x", "confirm": True})
            envelope = json.loads(result[0].text)
            self.assertEqual(envelope["status"], "verified", envelope)
            self.assertEqual(envelope["adoption"]["set_identity"], saved)


if __name__ == "__main__":
    unittest.main()
