"""ref_develop on the real NOCTURNEv2 capture (plan batch8 1.4, brief_1_4.md).

The snapshot is built once by moment.read_piece_snapshot over PieceDevice
(live_api=True: every arrangement MIDI clip's notes read through the Live API
fakes). No Live, no network, no store: ref_develop is pure.
"""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

from mcp_server import idiom_cells as ic
from mcp_server import moment
from mcp_server import piece_snapshot as ps
from mcp_server import pp_session as pp
from mcp_server import ref_develop as rd
from tests.pp_fakes import PieceDevice, on_both_versions

_SNAPSHOT = None


def setUpModule():
    pp.set_live_process_incarnation_resolver(
        lambda: {"pid": 4242, "start_time": "Sat Sep 26 10:00:00 2026"})


def tearDownModule():
    pp.set_live_process_incarnation_resolver(None)


def real_snapshot():
    global _SNAPSHOT
    if _SNAPSHOT is None:
        device = PieceDevice(live_api=True)
        with patch.object(pp, "operation_session", device.operation()):
            _SNAPSHOT = asyncio.run(moment.read_piece_snapshot())
    return _SNAPSHOT


def by_track(rows, name):
    found = [row for row in rows if row["track"] == name and row.get("role") != "empty"]
    assert len(found) == 1, (name, found)
    return found[0]


@on_both_versions
class TrackRoleTests(unittest.TestCase):
    def test_kits_by_device_or_note_shape_never_by_name(self):
        roles = rd.track_roles(real_snapshot())
        calm = by_track(roles, "Kit Var 3")
        self.assertEqual((calm["role"], calm["evidence"]), ("kit", "set_file"))
        glitch = by_track(roles, "9-Glitch Kit")
        self.assertEqual((glitch["role"], glitch["evidence"]), ("kit", "set_file"))
        # a Kontakt instrument playing a drum map: kit by its notes
        drum_map = by_track(roles, "Drum Map")
        self.assertEqual((drum_map["role"], drum_map["evidence"]), ("kit", "inferred"))
        self.assertIn("36-50", drum_map["why"])
        # short notes alone do not make a kit
        for name in ("5-Float Pad", "30-Kontakt 8", "Voice FX",
                     "Low Drone", "Synth A"):
            self.assertEqual(by_track(roles, name)["role"], "tonal", name)
        self.assertEqual({row["role"] for row in roles if row["track_id"] == "18"}, {"empty"})
        names = {row["track"] for row in roles}
        self.assertNotIn("Interface", names)          # muted: not a layer
        self.assertNotIn("SIDEKICK JAM", names)     # not his material


@on_both_versions
class MutedClipTests(unittest.TestCase):
    def test_a_muted_clip_never_sounds_nor_is_copied(self):
        # review 1.4 M5: a muted arrangement clip still fed the harmony map,
        # the clash gate and the plan's copies
        snap = copy.deepcopy(real_snapshot())
        heart = [t for t in snap["tracks"] if t["name"] == "Low Drone"][0]
        clip = [c for c in heart["arrangement_clips"] if c["id"] == "288"][0]
        clip["muted"] = True
        self.assertEqual(ps.clip_events(clip), [])
        lo, hi = clip["start_beats"], clip["end_beats"]
        events = rd.song_notes(snap)["3"]["events"]
        self.assertFalse([e for e in events if lo <= e["start"] < hi])
        plan = rd.propose(snap, {}, [], seed=0, compose=False)["plan"]
        ids = [i for s in plan["sections"] for t in s["tracks"] for i in t["clip_ids"]]
        self.assertNotIn("288", ids)


def synthetic_track(track_id, name, notes, *, beats=16.0):
    clip = {"id": "c" + track_id, "kind": "midi", "notes_status": "read", "muted": False,
            "start_beats": 0.0, "end_beats": beats,
            "playback": {"looping": False, "start_marker": 0.0, "end_marker": beats,
                         "arr_start": 0.0, "arr_end": beats},
            "audible_notes": [{"pitch": p, "start_beats": t, "duration_beats": d,
                               "velocity": 90} for p, t, d in notes]}
    return {"id": track_id, "name": name, "type": "midi", "in_layers": True,
            "devices": ["instrument: Synth"], "arrangement_clips": [clip]}


@on_both_versions
class KitHeuristicTests(unittest.TestCase):
    def test_a_monophonic_sixteenth_bass_in_the_drum_zone_is_tonal(self):
        # review 1.4 M6: C2..D#3 16ths, one note at a time, used to read as a kit
        line = [36, 39, 43, 46, 48, 46, 43, 39, 41, 44, 48, 51]
        bass = synthetic_track("b1", "Bass", [(line[i % len(line)], i * 0.25, 0.2)
                                              for i in range(64)])
        kit = synthetic_track("k1", "Kit", [(36, i * 1.0, 0.2) for i in range(16)]
                              + [(42, i * 0.5, 0.1) for i in range(32)]
                              + [(38, 1.0 + 2 * i, 0.2) for i in range(8)]
                              + [(46, 3.5 + 4 * i, 0.2) for i in range(4)]
                              + [(49, 0.0, 0.5)])
        roles = {r["track"]: r for r in rd.track_roles({"tracks": [bass, kit]})}
        self.assertEqual(roles["Bass"]["role"], "tonal")
        self.assertEqual((roles["Kit"]["role"], roles["Kit"]["evidence"]), ("kit", "inferred"))
        self.assertIn("together", roles["Kit"]["why"])


@on_both_versions
class HarmonyMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hmap = rd.harmony_map(real_snapshot())

    def test_segments_split_only_the_tone_held_alone_at_81(self):
        segments = self.hmap["segments"]
        self.assertEqual([s["bars"] for s in segments],
                         [[1, 32], [33, 44], [45, 60], [61, 68], [69, 80], [81, 84],
                          [85, 92], [93, 100], [101, 108]])
        self.assertEqual([s["kind"] for s in segments],
                         ["drone", "modal", "modal", "modal", "modal", "drone", "silent",
                          "modal", "modal"])
        held = segments[5]
        self.assertEqual((held["centre_name"], held["split"]["from_section"]), ("C#", [69, 84]))
        # negatives: C inside 33-44, F# inside 93-100, C at 29-32 stay in their sections
        self.assertEqual([s["split"] for s in segments if s["bars"] != [81, 84]],
                         [None] * 8)

    def test_worlds_c_drone_c_phrygian_c_sharp_alone_f_sharp(self):
        worlds = self.hmap["worlds"]
        self.assertEqual([w["spans"] for w in worlds],
                         [[[1, 32]], [[33, 80]], [[81, 84]], [[93, 108]]])
        drone, phrygian, held, return_world = worlds
        self.assertEqual((drone["centre_name"], drone["kind"], drone["mode"]), ("C", "drone", None))
        self.assertEqual((phrygian["centre_name"], phrygian["mode"]), ("C", "phrygian"))
        self.assertEqual(phrygian["core"], ["C", "Db", "F", "G", "Ab", "Bb"])
        self.assertGreater(phrygian["mode_margin"], 0)
        self.assertEqual(phrygian["mode_runner_up"], "aeolian")
        self.assertEqual(phrygian["foreshadow"][0]["name"], "F#")
        self.assertEqual(phrygian["foreshadow"][0]["in_world"], return_world["id"])
        self.assertEqual((held["centre_name"], held["core"]), ("C#", ["C#"]))
        self.assertEqual(return_world["centre_name"], "F#")
        self.assertEqual(sorted(return_world["core"]), ["C#", "E", "F#"])
        self.assertIsNone(return_world["mode"])
        self.assertIn("third", return_world["mode_note"])
        self.assertTrue(all(w["evidence"] == "inferred" for w in worlds))

    def test_segment_centres_carry_their_margin(self):
        weight = {s["bars"][0]: s for s in self.hmap["segments"]}
        self.assertEqual(weight[33]["centre_name"], "C")
        self.assertGreater(weight[33]["centre_margin"], 0.0)
        self.assertLess(weight[33]["centre_margin"], 0.1)     # C over G by a hair

    def test_a_drums_only_break_is_unpitched_not_silent(self):
        # review 1.4 M4: a kit playing 85-92 was reported as "silent" (set_file)
        snap = copy.deepcopy(real_snapshot())
        calm = [t for t in snap["tracks"] if t["name"] == "Kit Var 3"][0]
        clip = copy.deepcopy(calm["arrangement_clips"][0])
        clip.update(id="probe", start_beats=336.0, end_beats=368.0)
        clip["playback"] = dict(clip["playback"], arr_start=336.0, arr_end=368.0)
        calm["arrangement_clips"].append(clip)
        section = [s for s in snap["sections"] if s["bars"] == [85, 92]][0]
        section["silent"] = False
        hmap = rd.harmony_map(snap)
        seg = [s for s in hmap["segments"] if s["bars"] == [85, 92]][0]
        self.assertEqual((seg["kind"], seg["world"]), ("unpitched", None))
        self.assertEqual([p["gap_bars"] for p in hmap["pivots"]], [[85, 92]])
        self.assertEqual(hmap["pivots"][0]["gap_kind"], "unpitched")
        finding = [f for f in rd.diagnose(hmap, rd.layer_arc(snap))["findings"]
                   if f["detector"] == "unbridged_gap"][0]
        self.assertNotIn("silent", finding["question"])
        self.assertIn("no pitched layer", finding["question"])
        self.assertNotIn("silent", " ".join(f["fact"] for f in finding["facts"]))
        # the real capture's gap stays silent
        self.assertEqual(self.hmap["pivots"][0]["gap_kind"], "silent")

    def test_the_pivot_is_c_sharp(self):
        pivots = self.hmap["pivots"]
        self.assertEqual(len(pivots), 1)
        pivot = pivots[0]
        self.assertEqual(pivot["gap_bars"], [85, 92])
        self.assertEqual(pivot["shared"], ["C#"])
        self.assertEqual(pivot["held_alone"], [81, 84])
        self.assertEqual(pivot["interval"], "tritone")
        crossing = [b for b in self.hmap["boundaries"] if b["bar"] == 93]
        self.assertEqual(crossing[0]["shared"], ["C#"])
        self.assertEqual(crossing[0]["across_silence"], [85, 92])


@on_both_versions
class LayerArcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.arc = rd.layer_arc(real_snapshot())

    def test_early_peak_61_68_is_never_exceeded(self):
        arc = self.arc
        self.assertEqual([p["bars"] for p in arc["peaks"]], [[61, 68], [101, 108]])
        self.assertEqual(arc["first_peak"]["layers"], 11)
        self.assertEqual(arc["first_peak"]["span_pct"], [56, 63])
        self.assertEqual(arc["last_peak"]["layers"], 8)
        self.assertFalse(arc["last_peak_exceeds_first"])
        self.assertEqual(arc["evidence"], "set_file")

    def test_the_85_92_gap_and_the_hand_offs(self):
        arc = self.arc
        self.assertEqual([g["bars"] for g in arc["gaps"]], [[85, 92]])
        self.assertEqual(arc["gaps"][0]["between"], [[69, 84], [93, 100]])
        handoff = {h["bar"]: h for h in arc["handoffs"]}
        self.assertEqual(sorted(handoff[69]["leaves"]),
                         ["30-Kontakt 8", "Grain Pad", "Kit Var 3", "Low Drone", "Synth A"])
        self.assertIn("Low Drone", handoff[93]["enters"])

    def test_ending_and_static_runs(self):
        arc = self.arc
        self.assertEqual(arc["ending"]["bars"], [101, 108])
        self.assertEqual((arc["ending"]["last_bar"], arc["ending"]["layers_at_end"]), (108, 8))
        intro = arc["sections"][0]
        self.assertEqual(intro["longest_static"], [13, 32])


@on_both_versions
class ClashCheckTests(unittest.TestCase):
    def test_finds_the_sustained_rubs_at_33_44(self):
        notes = rd.song_notes(real_snapshot())
        report = rd.clash_check(notes, {"bars": [33, 44]}, bpb=4.0)
        self.assertEqual(report["resolution"], "16th")
        sustained = [r for r in report["rubs"] if r["sustained"]]
        pairs = {tuple(r["names"]) for r in sustained}
        self.assertIn(("F#", "G"), pairs)
        self.assertIn(("E", "F"), pairs)
        for rub in sustained:
            self.assertTrue(33 <= rub["bars"][0] <= rub["bars"][1] <= 44)
            self.assertEqual(rub["evidence"], "set_file")
            self.assertIn(rub["interval"], ("semitone", "tritone"))

    def test_kits_never_count(self):
        notes = rd.song_notes(real_snapshot(), roles=("tonal", "kit"))
        with_kits = rd.clash_check(notes, {"bars": [45, 60]}, bpb=4.0)
        tonal = rd.clash_check(rd.song_notes(real_snapshot()), {"bars": [45, 60]}, bpb=4.0)
        self.assertEqual(with_kits["rubs"], tonal["rubs"])
        self.assertTrue(with_kits["excluded"])

    def test_consonant_notes_have_no_rub_and_sixteenth_resolution(self):
        fifth = {"a": [{"pitch": 48, "start": 0.0, "duration": 4.0}],
                 "b": [{"pitch": 55, "start": 0.0, "duration": 4.0}]}
        self.assertEqual(rd.clash_check(fifth, (0.0, 4.0))["rubs"], [])
        # a semitone that only touches inside one 16th still counts; one that
        # starts after the other ends does not
        touch = {"a": [{"pitch": 60, "start": 0.0, "duration": 1.0}],
                 "b": [{"pitch": 61, "start": 0.9, "duration": 1.0}]}
        self.assertEqual(len(rd.clash_check(touch, (0.0, 4.0))["rubs"]), 1)
        apart = {"a": [{"pitch": 60, "start": 0.0, "duration": 1.0}],
                 "b": [{"pitch": 61, "start": 1.0, "duration": 1.0}]}
        self.assertEqual(rd.clash_check(apart, (0.0, 4.0))["rubs"], [])
        tritone = {"a": [{"pitch": 60, "start": 0.0, "duration": 2.0}],
                   "b": [{"pitch": 66, "start": 0.0, "duration": 2.0}]}
        rub = rd.clash_check(tritone, (0.0, 4.0))["rubs"][0]
        self.assertEqual((rub["interval"], rub["sustained"]), ("tritone", True))


@on_both_versions
class DiagnoseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        snap = real_snapshot()
        cls.hmap, cls.arc = rd.harmony_map(snap), rd.layer_arc(snap)

    def test_questions_not_verdicts_on_the_real_capture(self):
        diagnosis = rd.diagnose(self.hmap, self.arc, {})
        labels = [f["label"] for f in diagnosis["findings"]]
        self.assertEqual(labels, ["long_static_intro@13-32", "early_peak@61-68",
                                  "unbridged_gap@85-92", "uncommitted_ending@101-108"])
        for finding in diagnosis["findings"]:
            self.assertTrue(finding["question"].endswith("?"), finding["question"])
            self.assertTrue(finding["facts"])
            for fact in finding["facts"]:
                self.assertIn(fact["evidence"], rd.EVIDENCE)
            self.assertTrue(finding["needs"])
        gap = diagnosis["findings"][2]
        self.assertIn("C#", gap["question"])
        self.assertIn("81-84", gap["question"])
        self.assertEqual(gap["needs"], ["key_change_bridge", "thin_before_return"])
        self.assertEqual(diagnosis["findings"][1]["needs"], ["late_peak", "thin_before_return"])
        self.assertEqual(diagnosis["findings"][3]["needs"], ["strip_ending"])
        self.assertEqual(diagnosis["findings"][0]["needs"], ["delayed_entry"])
        self.assertEqual(diagnosis["silenced"], [])

    def test_owner_intended_silences_a_finding_and_says_so(self):
        diagnosis = rd.diagnose(self.hmap, self.arc,
                                {"intended": ["unbridged_gap@85-92", "early_peak@1-4"]})
        labels = [f["label"] for f in diagnosis["findings"]]
        self.assertNotIn("unbridged_gap@85-92", labels)
        self.assertEqual([s["label"] for s in diagnosis["silenced"]], ["unbridged_gap@85-92"])
        self.assertEqual(diagnosis["silenced"][0]["evidence"], "owner_confirmed")
        self.assertEqual(diagnosis["unmatched_intended"], ["early_peak@1-4"])


def _card_sections(rows):
    return [{"t": t, "label": label, "layers_in": list(ins), "layers_out": list(outs)}
            for t, label, ins, outs in rows]


def synthetic_ref(ref_id, title, rows, duration, *, tags=("ben-frost",), verified=True,
                  grid_bpm=90.0, owner_notes=None):
    """A reference entry built through the real tools.audio_ref.reference_sequence."""
    from tools import audio_ref
    layers, seen = [], set()
    for t, _label, ins, _outs in rows:
        for role in ins:
            if role not in seen:
                seen.add(role)
                layers.append({"role": role, "first_t": t})
    card = {"sections": _card_sections(rows), "layers": layers,
            "tempo": {"grid_bpm": grid_bpm, "feel_bpm": grid_bpm}}
    # a flat measured loudness profile: reference_sequence reads moves only
    # when a measurement exists, and a flat one adds no measured move itself
    bar_len = 240.0 / (grid_bpm or 90.0)
    meta = {"ref_id": ref_id, "listen_verified": verified, "duration_s": duration,
            "measured": {"energy_per_bar": [1.0] * int(duration // bar_len),
                         "bar_len_s": bar_len, "tempo_bpm": grid_bpm}}
    return {"ref_id": ref_id, "title": title, "tags": list(tags),
            "sequence": audio_ref.reference_sequence(card, meta, owner_notes)}


def synthetic_refs():
    """Shapes of the real verified Ben Frost cards (store/audio_refs, 2026-09-26),
    rebuilt here so the test never reads the store."""
    return [
        synthetic_ref("pv2", "Peter Venkman Pt.II", [
            ("0:00", "Silence / Pre-roll", ["texture"], []),
            ("0:02", "Atmospheric Intro", ["lead", "pad"], []),
            ("1:36", "Groove A", ["drums", "bass"], []),
            ("3:42", "Groove B / Climax", [], []),
            ("4:44", "Outro Drone", [], ["drums", "bass"])], 305.2),
        synthetic_ref("secant", "Secant", [
            ("0:00", "Intro", ["drums", "bass"], []),
            ("0:31", "Groove Build", ["lead", "texture"], []),
            ("1:02", "Main Section A", ["pad"], []),
            ("2:46", "Breakdown", [], ["drums", "bass"]),
            ("3:04", "Drop / Climax", ["drums", "bass", "lead"], []),
            ("4:16", "Outro Build-down", [], ["lead"]),
            ("4:54", "Tail", [], ["drums", "bass", "pad", "texture", "fx"])], 295.2,
            grid_bpm=None),
        synthetic_ref("killshot", "Killshot", [
            ("0:00", "Intro", ["kick", "bass", "texture"], []),
            ("0:38", "Groove Build", ["hat"], []),
            ("0:57", "Main Groove", ["lead"], []),
            ("1:24", "Breakdown 1", ["pad"], ["kick", "bass"]),
            ("3:30", "Peak Drop", ["kick", "bass", "ride"], []),
            ("5:30", "Outro Decay", [], ["kick", "bass", "hat", "ride"])], 371.8,
            grid_bpm=None),
        # the same shape as pv2 but from an unverified listen: never evidence
        synthetic_ref("unverified", "Unverified Listen", [
            ("0:02", "Atmospheric Intro", ["lead", "pad"], []),
            ("3:42", "Climax", ["drums"], []),
            ("4:44", "Outro", [], ["drums", "bass"])], 305.0, verified=False),
        # an owner-confirmed strip ending outranks model-heard ones for that move
        synthetic_ref("owner", "Owner Heard", [
            ("0:00", "Intro", ["drone", "breath"], []),
            ("6:00", "Exhaustion", [], ["breath"])], 445.0, tags=(), grid_bpm=118.0,
            owner_notes={"confirmed_layer_changes": [{"t": "6:00", "layers_out": ["breath"]}]}),
    ]


@on_both_versions
class RankReferencesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        snap = real_snapshot()
        cls.diagnosis = rd.diagnose(rd.harmony_map(snap), rd.layer_arc(snap), {})
        cls.ranked = rd.rank_references(synthetic_refs(), cls.diagnosis,
                                        {"tags": ["ben-frost"]}, tempo=90.0)

    def test_late_peak_and_strip_ending_come_from_pv2_secant_killshot(self):
        top = [row["ref_id"] for row in self.ranked["shortlist"][:3]]
        self.assertEqual(sorted(top), ["killshot", "pv2", "secant"])
        self.assertEqual(top[0], "pv2")        # same 90 BPM grid breaks the tie
        pv2 = self.ranked["shortlist"][0]
        late = [m for m in pv2["matches"] if m["need"] == "late_peak"][0]
        self.assertEqual((late["t"], late["claim"], late["evidence"]),
                         ("3:42", "sections", ["model_heard"]))
        strip = [m for m in pv2["matches"] if m["need"] == "strip_ending"
                 and m["claim"] == "layers"][0]
        self.assertEqual((strip["t"], strip["layers"]), ("4:44", ["drums", "bass"]))
        self.assertTrue(strip["authorizes_layer_change"])
        # PV II's 0:02 intro entry is its opening, not a delay
        self.assertEqual([m["t"] for m in pv2["matches"] if m["need"] == "delayed_entry"],
                         ["1:36"])
        killshot = [r for r in self.ranked["shortlist"] if r["ref_id"] == "killshot"][0]
        self.assertNotIn("late_peak", {m["need"] for m in killshot["matches"]})  # 56% is not late

    def test_no_reference_supports_a_key_change(self):
        unsupported = {row["need"]: row for row in self.ranked["unsupported"]}
        self.assertIn("key_change_bridge", unsupported)
        self.assertIn("no reference", unsupported["key_change_bridge"]["say"])
        self.assertEqual(unsupported["key_change_bridge"]["finding"], "unbridged_gap@85-92")

    def test_the_evidence_gate(self):
        ids = [row["ref_id"] for row in self.ranked["shortlist"]]
        self.assertNotIn("unverified", ids)
        rejected = {row["ref_id"]: row for row in self.ranked["rejected"]}
        self.assertIn("listen_verified", rejected["unverified"]["reason"])
        for row in self.ranked["shortlist"]:
            for match in row["matches"]:
                self.assertNotIn("model_unverified", match["evidence"])
                if match["claim"] == "layers":
                    self.assertTrue({"model_heard", "owner_confirmed"} & set(match["evidence"]))
                if match["evidence"] == ["measured"]:
                    self.assertFalse(match["authorizes_layer_change"])

    def test_depart_demotes_the_matching_moves_with_a_note(self):
        # Job B2: depart "ending" demotes strip_ending, "peak_placement" late_peak
        ranked = rd.rank_references(synthetic_refs(), self.diagnosis,
                                    {"tags": ["ben-frost"],
                                     "depart": ["ending", "peak_placement"]}, tempo=90.0)
        for need, value in (("strip_ending", "ending"), ("late_peak", "peak_placement")):
            rows = ranked["by_need"][need]
            self.assertTrue(rows)
            for row in rows:
                self.assertTrue(row["departed"], (need, row["ref_id"]))
                self.assertIn("depart from %s" % value, row["note"])
        for row in ranked["by_need"]["delayed_entry"]:
            self.assertFalse(row["departed"])
        self.assertEqual(sorted(d["need"] for d in ranked["departed"]),
                         ["late_peak", "strip_ending"])
        # demoted: the departed needs no longer lift a reference's score
        before = {r["ref_id"]: r["score"] for r in self.ranked["shortlist"]}
        after = {r["ref_id"]: r["score"] for r in ranked["shortlist"]}
        self.assertLess(after["pv2"], before["pv2"])
        # within a ref, its departed matches sort after its kept ones
        pv2 = [r for r in ranked["shortlist"] if r["ref_id"] == "pv2"][0]
        order = [m["departed"] for m in pv2["matches"] if m["need"] == "late_peak"]
        self.assertEqual(order, sorted(order))

    def test_measured_only_supports_a_loudness_sentence_never_layers(self):
        seq = {"ref_id": "loud", "duration_s": 300.0, "sections": [], "warnings": [],
               "tempo": {"grid_bpm": None},
               "moves": [{"kind": "thin_before_return", "t_s": 150.0, "evidence": ["measured"],
                          "status": "inferred", "authorizes_layer_removal": False,
                          "missing_evidence": "verified layer-change evidence or owner "
                                              "confirmation required"}]}
        ranked = rd.rank_references([{"ref_id": "loud", "title": "Loud", "tags": [],
                                      "sequence": seq}], self.diagnosis)
        match = ranked["shortlist"][0]["matches"][0]
        self.assertEqual((match["claim"], match["authorizes_layer_change"]), ("loudness", False))
        self.assertIn("quieter", match["sentence"])
        self.assertIn("missing", match)

    def test_measured_only_never_carries_layer_names_or_authority(self):
        # review 1.4 m1: a hand-built measured move kept its layer names (and
        # the plan matched "drums" on them); a bare authorizes flag was trusted
        seq = {"ref_id": "hand", "duration_s": 300.0, "sections": [], "warnings": [],
               "tempo": {"grid_bpm": None},
               "moves": [{"kind": "delayed_entry", "t_s": 90.0, "evidence": ["measured"],
                          "layers": ["drums"]},
                         {"kind": "thin_before_return", "t_s": 150.0, "evidence": ["measured"],
                          "supported_by": ["measured"], "authorizes_layer_removal": True}]}
        ranked = rd.rank_references([{"ref_id": "hand", "title": "Hand", "tags": [],
                                      "sequence": seq}], self.diagnosis)
        for match in ranked["shortlist"][0]["matches"]:
            self.assertEqual((match["claim"], match["layers"], match["authorizes_layer_change"]),
                             ("loudness", [], False), match["need"])

    def test_a_verified_layer_change_backs_a_measured_thinning(self):
        seq = {"ref_id": "sec", "duration_s": 295.0, "sections": [], "warnings": [],
               "tempo": {"grid_bpm": None},
               "moves": [{"kind": "thin_before_return", "t_s": 168.0, "evidence": ["measured"],
                          "supported_by": ["measured", "model_heard"], "status": "inferred",
                          "authorizes_layer_removal": True}]}
        ranked = rd.rank_references([{"ref_id": "sec", "title": "Secant", "tags": [],
                                      "sequence": seq}], self.diagnosis)
        matches = ranked["shortlist"][0]["matches"]
        self.assertEqual(len(matches), 1)       # one move, however many findings need it
        match = matches[0]
        self.assertEqual(match["findings"], ["early_peak@61-68", "unbridged_gap@85-92"])
        self.assertEqual((match["evidence"], match["claim"], match["authorizes_layer_change"]),
                         (["measured", "model_heard"], "layers", True))

    def test_a_post_climax_section_is_not_a_late_peak(self):
        ref = synthetic_ref("pv1", "Peter Venkman Pt.I", [
            ("0:00", "Intro", ["pad"], []), ("2:52", "Post-Climax Echoes", [], [])], 250.0)
        ranked = rd.rank_references([ref], self.diagnosis)
        self.assertNotIn("late_peak", ranked["by_need"])

    def test_owner_confirmation_outranks_model_heard_for_its_move(self):
        strip = self.ranked["by_need"]["strip_ending"]
        self.assertEqual(strip[0]["ref_id"], "owner")
        self.assertEqual(strip[0]["evidence"], ["owner_confirmed"])


@on_both_versions
class ArrangementPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = real_snapshot()
        cls.hmap, cls.arc = rd.harmony_map(cls.snap), rd.layer_arc(cls.snap)
        cls.diagnosis = rd.diagnose(cls.hmap, cls.arc, {})
        cls.refs = rd.rank_references(synthetic_refs(), cls.diagnosis, {"tags": ["ben-frost"]},
                                      tempo=90.0)
        cls.plan = rd.arrangement_plan(cls.snap, cls.hmap, cls.arc, cls.diagnosis, cls.refs, {})

    def section(self, role):
        return [s for s in self.plan["sections"] if s["role"] == role]

    def names(self, section, at_bar=None):
        return sorted(t["track"] for t in section["tracks"]
                      if at_bar is None or t["at_bars"][0] <= at_bar <= t["at_bars"][1])

    def test_whole_song_bar_map(self):
        plan = self.plan
        self.assertEqual([(s["role"], s["bars"]) for s in plan["sections"]],
                         [("opening", [1, 32]), ("build", [33, 44]), ("build", [45, 60]),
                          ("false peak", [61, 68]), ("thinning", [69, 84]),
                          ("bridge", [85, 92]), ("real peak", [93, 100]),
                          ("real peak", [101, 108]), ("coda", [109, 116])])
        self.assertEqual(plan["total_bars"], 116)
        self.assertEqual(plan["duration_s"], 309.3)
        self.assertEqual(plan["writes"], "none")

    def test_every_part_is_a_copy_of_his_clips(self):
        clip_ids = {c["id"]: t["id"] for t in self.snap["tracks"] for c in t["arrangement_clips"]}
        for section in self.plan["sections"]:
            for entry in section["tracks"]:
                self.assertTrue(entry["copy"])
                self.assertTrue(entry["clip_ids"], (section["bars"], entry["track"]))
                for clip_id in entry["clip_ids"]:
                    self.assertEqual(clip_ids[clip_id], entry["track_id"])
                self.assertTrue(section["bars"][0] <= entry["at_bars"][0]
                                <= entry["at_bars"][1] <= section["bars"][1])

    def test_false_peak_holds_a_world_back_and_the_return_exceeds_it(self):
        false_peak = self.section("false peak")[0]
        self.assertNotIn("Grain Pad", self.names(false_peak))
        self.assertNotIn("30-Kontakt 8", self.names(false_peak))
        self.assertEqual(false_peak["layers"], 9)
        borrow = {b["move"]: b for b in false_peak["borrow"]}
        self.assertEqual(borrow["hold_a_world_back"]["evidence"], "set_file")
        self.assertEqual(borrow["late_peak"]["ref_id"], "pv2")
        for real in self.section("real peak"):
            self.assertEqual(real["layers"], 11)
            self.assertIn("Kit Var 3", self.names(real))       # drums back...
            self.assertIn("20-Bassline", self.names(real))       # ...and the return's layers
        # never spread a layer outside the world's core: Voice FX (G A B) and
        # Noise Bed's C blip stay where he put them
        first = self.section("real peak")[0]
        self.assertNotIn("Voice FX", self.names(first))
        self.assertTrue(any(c.startswith("Voice FX not added") for c in first["changes"]))
        self.assertNotIn("Noise Bed", self.names(self.section("real peak")[1]))
        self.assertTrue(any("drums back" in q for q in self.section("real peak")[0]["questions"]))
        self.assertTrue(self.plan["real_peak_exceeds_false_peak"])

    def test_bridge_holds_the_pivot_alone_then_the_new_figure_enters(self):
        bridge = self.section("bridge")[0]
        pedal = [t for t in bridge["tracks"] if t["part"] == "pivot"]
        figure = [t for t in bridge["tracks"] if t["part"] == "figure"]
        self.assertEqual(len(pedal), 1)
        self.assertEqual((pedal[0]["from_bars"], pedal[0]["at_bars"]), ([81, 84], [85, 92]))
        self.assertEqual((figure[0]["track"], figure[0]["from_bars"], figure[0]["at_bars"]),
                         ("Low Drone", [93, 96], [89, 92]))
        self.assertEqual(self.names(bridge, 86), [pedal[0]["track"]])
        self.assertEqual(bridge["pivot"], "C#")
        # the pivot really sounds: rendered, alone, on C# through 85-88
        rendered = rd.render_plan(self.snap, self.plan)
        held = [e for e in rendered[pedal[0]["track_id"]]["events"] if 336 <= e["start"] < 368]
        self.assertTrue(held)
        self.assertEqual({e["pitch"] % 12 for e in held}, {1})
        others = [v["track"] for tid, v in rendered.items() if tid != pedal[0]["track_id"]
                  and any(e["start"] < 352 and e["start"] + e["duration"] > 336
                          for e in v["events"])]
        self.assertEqual(others, [])
        self.assertEqual(bridge["clashes"]["count"], 0)

    def test_coda_strips_to_the_figure(self):
        coda = self.section("coda")[0]
        roles = {r["track"]: r["role"] for r in self.hmap["roles"]}
        self.assertFalse([n for n in self.names(coda) if roles.get(n) == "kit"])
        self.assertEqual(self.names(coda, 116), ["Low Drone"])
        self.assertEqual(coda["borrow"][0]["move"], "strip_ending")
        self.assertIn(coda["borrow"][0]["ref_id"], ("owner", "pv2"))

    def test_opening_delays_the_kit_into_the_static_run(self):
        opening = self.section("opening")[0]
        kit = [t for t in opening["tracks"] if t["track"] == "Kit Var 3"][0]
        self.assertEqual((kit["from_bars"], kit["at_bars"]), ([25, 32], [25, 32]))
        borrow = opening["borrow"][0]
        self.assertEqual((borrow["move"], borrow["ref_id"], borrow["t"]),
                         ("delayed_entry", "pv2", "1:36"))     # drums held back, not 0:02
        self.assertIn("drums", borrow["layers"])

    def test_render_keeps_a_note_sustaining_into_the_source_window(self):
        # Low Drone's first C3 starts at beat 0 and rings 7.66 beats: copying
        # bar 2 alone must keep its tail, not drop it
        plan = {"sections": [{"tracks": [{"track_id": "3", "track": "Low Drone",
                                          "role": "tonal", "from_bars": [2, 2],
                                          "at_bars": [1, 1]}]}]}
        events = rd.render_plan(self.snap, plan)["3"]["events"]
        self.assertEqual([(e["pitch"], e["start"]) for e in events], [(48, 0.0)])
        self.assertAlmostEqual(events[0]["duration"], 3.664, places=2)

    def test_evidence_and_hazards_are_typed(self):
        for section in self.plan["sections"]:
            for key in ("borrow", "preserve", "test"):
                for item in section[key]:
                    self.assertIn(item["evidence"], rd.EVIDENCE, (section["role"], key, item))
        synth_a = [t for s in self.plan["sections"] for t in s["tracks"]
                    if t["track"] == "Synth A"][0]
        self.assertIn("external_instrument_return", {h["type"] for h in synth_a["hazards"]})
        flags = [f for s in self.plan["sections"] for t in s["tracks"] for f in t["flags"]]
        self.assertTrue(any("1121" in f for f in flags))       # Noise Bed's sub-bar blip

    def test_copies_onto_hazard_tracks_are_flagged(self):
        # review 1.4 M7: Synth A (MIDI -> hardware) and the Voice FX chain
        flags = {f["track_id"]: f for f in self.plan["hazard_flags"]}
        self.assertEqual(sorted(flags), ["19", "22"])
        synth_a, voice_fx = flags["19"], flags["22"]
        self.assertIn("hardware", synth_a["say"])
        self.assertIn("Synth A", synth_a["say"])                  # its return track
        self.assertIn("routing", synth_a["say"])                   # input/output unknown
        self.assertIn("24-MIDI", voice_fx["say"])
        self.assertIn("23-Audio", voice_fx["say"])
        self.assertTrue(all(c["in_place"] for c in voice_fx["copies"]))
        longer = rd.arrangement_plan(self.snap, self.hmap, self.arc, self.diagnosis, self.refs,
                                     {}, target_duration_s=420)
        voice_fx = [f for f in longer["hazard_flags"] if f["track_id"] == "22"][0]
        self.assertTrue([c for c in voice_fx["copies"] if not c["in_place"]])  # moved/looped
        self.assertIn("moved", voice_fx["say"])
        self.assertTrue(all(c["in_place"] == (c["at_bars"] == c["from_bars"])
                            for f in flags.values() for c in f["copies"]))
        self.assertTrue(all(f["evidence"] == "set_file" for f in flags.values()))

    def test_target_duration_stretches_toward_seven_minutes(self):
        plan = rd.arrangement_plan(self.snap, self.hmap, self.arc, self.diagnosis, self.refs, {},
                                   target_duration_s=420)
        self.assertEqual(plan["total_bars"], 156)
        stretched = [s for s in plan["sections"] if s["stretched_bars"]]
        self.assertTrue(stretched)
        self.assertTrue(all(s["role"] in ("bridge", "real peak", "coda", "thinning", "opening")
                            for s in stretched))
        self.assertEqual(sum(s["length_bars"] for s in plan["sections"]), 156)

    def test_stretch_keeps_the_figure_last_and_the_held_tone_right_before_the_bridge(self):
        # review 1.4 M2: the stretch extended the figure to 12 bars and looped
        # the thinning's held C# back into C phrygian before the bridge
        plan = rd.arrangement_plan(self.snap, self.hmap, self.arc, self.diagnosis, self.refs, {},
                                   target_duration_s=420)
        self.assertEqual(plan["total_bars"], 156)
        bridge = [s for s in plan["sections"] if s["role"] == "bridge"][0]
        self.assertGreater(bridge["stretched_bars"], 0)
        figure = [t for t in bridge["tracks"] if t["part"] == "figure"][0]
        self.assertEqual(figure["at_bars"], [bridge["bars"][1] - 3, bridge["bars"][1]])
        thinning = [s for s in plan["sections"] if s["role"] == "thinning"][0]
        self.assertEqual(thinning["stretched_bars"], 0)
        self.assertEqual(thinning["bars"][1] + 1, bridge["bars"][0])
        rendered = rd.render_plan(self.snap, plan)
        lo, hi = (thinning["bars"][1] - 4) * 4.0, thinning["bars"][1] * 4.0
        pcs = {e["pitch"] % 12 for tid, v in rendered.items() if v["role"] == "tonal"
               for e in v["events"] if e["start"] < hi and e["start"] + e["duration"] > lo}
        self.assertEqual(pcs, {1})          # C# alone, right before the bridge

    def test_a_departed_move_is_never_borrowed(self):
        refs = rd.rank_references(synthetic_refs(), self.diagnosis,
                                  {"tags": ["ben-frost"], "depart": ["ending", "peak_placement"]},
                                  tempo=90.0)
        plan = rd.arrangement_plan(self.snap, self.hmap, self.arc, self.diagnosis, refs,
                                   {"depart": ["ending", "peak_placement"]})
        moves = {b["move"] for s in plan["sections"] for b in s["borrow"]}
        self.assertNotIn("strip_ending", moves)
        self.assertNotIn("late_peak", moves)
        self.assertIn("delayed_entry", moves)
        coda = [s for s in plan["sections"] if s["role"] == "coda"][0]
        self.assertTrue(any("depart from ending" in t["what"] and t["evidence"] == "owner_confirmed"
                            for t in coda["test"]), coda["test"])
        false_peak = [s for s in plan["sections"] if s["role"] == "false peak"][0]
        self.assertTrue(any("depart from peak_placement" in t["what"] for t in false_peak["test"]))

    def test_protected_tracks_are_never_held_back(self):
        plan = rd.arrangement_plan(self.snap, self.hmap, self.arc, self.diagnosis, self.refs,
                                   {"protect_track_ids": ["23"]})
        false_peak = [s for s in plan["sections"] if s["role"] == "false peak"][0]
        self.assertIn("Grain Pad", sorted(t["track"] for t in false_peak["tracks"]))
        self.assertTrue(any(p["evidence"] == "owner_confirmed" and "Grain Pad" in p["what"]
                            for p in false_peak["preserve"]))

    def test_protected_tracks_are_never_stripped_in_the_coda(self):
        # review 1.4 M1: a protected kit left the coda; protected tonal
        # layers were cut to its first half
        protect = ["9", "8", "21"]          # Kit Var 3, Drum Map (kits), 20-Bassline
        plan = rd.arrangement_plan(self.snap, self.hmap, self.arc, self.diagnosis, self.refs,
                                   {"protect_track_ids": protect}, target_duration_s=420)
        coda = [s for s in plan["sections"] if s["role"] == "coda"][0]
        spans = {t["track_id"]: t["at_bars"] for t in coda["tracks"]}
        for tid in protect:
            self.assertEqual(spans.get(tid), coda["bars"], tid)
        self.assertFalse(any("Kit Var 3" in c.split(":")[1].split(";")[0]
                             for c in coda["changes"] if c.startswith("new ")))
        # the text says how long the figure is alone, after any stretch
        alone = coda["bars"][1] - max(t["at_bars"][1] for t in coda["tracks"]
                                      if t["at_bars"][1] < coda["bars"][1])
        self.assertTrue(any("final %d bars" % alone in c for c in coda["changes"]),
                        coda["changes"])


@on_both_versions
class ComposePartsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = real_snapshot()
        cls.hmap, cls.arc = rd.harmony_map(cls.snap), rd.layer_arc(cls.snap)
        diagnosis = rd.diagnose(cls.hmap, cls.arc, {})
        refs = rd.rank_references(synthetic_refs(), diagnosis, {"tags": ["ben-frost"]}, tempo=90.0)
        cls.plan = rd.arrangement_plan(cls.snap, cls.hmap, cls.arc, diagnosis, refs, {})
        cls.rendered = rd.render_plan(cls.snap, cls.plan)
        cls.result = rd.compose_parts(cls.snap, cls.hmap, cls.plan, seed=7)
        cls.parts = cls.result["parts"]

    def kind(self, kind):
        return [p for p in self.parts if p["kind"] == kind]

    def rubs_against_plan(self, part):
        lo = (part["plan_bars"][0] - 1) * 4.0
        hi = part["plan_bars"][1] * 4.0
        notes = {**self.rendered, "__part__": {"track": part["track"], "role": part["role"],
                                               "events": part["notes"]}}
        report = rd.clash_check(notes, (lo, hi))
        return [r for r in report["rubs"] if "__part__" in r["track_keys"]]

    def test_a_motif_in_each_world_passes_clash_check(self):
        motifs = self.kind("motif")
        # W3 is the C# held alone before the bridge: no motif there (review 1.4 M3)
        self.assertEqual(sorted(p["world"] for p in motifs), ["W1", "W2", "W4"])
        skipped = [s for s in self.result["skipped"] if s["kind"] == "motif"]
        self.assertEqual([s["world"] for s in skipped], ["W3"])
        self.assertIn("held alone", skipped[0]["why"])
        worlds = {w["id"]: w for w in self.hmap["worlds"]}
        for part in motifs:
            self.assertTrue(part["notes"], part["part_id"])
            self.assertEqual(self.rubs_against_plan(part), [], part["part_id"])
            self.assertEqual(part["clash"]["remaining"], 0)
            self.assertEqual(part["root"], worlds[part["world"]]["centre_name"])
            self.assertIn(ic.IDIOM_SOURCE, part["provenance"])

    def test_every_part_is_new_sidekick_material_with_provenance(self):
        kinds = {p["kind"] for p in self.parts}
        self.assertEqual(kinds, {"motif", "riff", "pedal", "pulse", "polymeter"})
        for part in self.parts:
            self.assertTrue(part["new_track"])
            self.assertTrue(part["track"].startswith("SIDEKICK "), part["track"])
            self.assertTrue(part["provenance"])
            for item in part["evidence"]:
                self.assertIn(item, rd.EVIDENCE)
            for note in part["notes"]:
                self.assertTrue(part["plan_bars"][0] <= note["start_beats"] / 4.0 + 1
                                < part["plan_bars"][1] + 1, (part["part_id"], note))
            if part["role"] != "kit":
                self.assertEqual(self.rubs_against_plan(part), [], part["part_id"])

    def test_pedal_holds_the_pivot_and_the_pulse_follows_the_roots(self):
        pedal = self.kind("pedal")[0]
        self.assertEqual({n["pitch"] % 12 for n in pedal["notes"]}, {1})
        self.assertEqual(pedal["plan_bars"], [85, 92])
        pulse = self.kind("pulse")[0]
        self.assertEqual(pulse["roots"], [{"root": "C", "bars": [69, 80]},
                                          {"root": "C#", "bars": [81, 84]}])
        # a pulse note off its root is one the gate re-voiced, and says so
        revoiced = {(d["start_beats"], d["to"]) for d in self.result["dropped_notes"]
                    if d["part_id"] == pulse["part_id"] and d["action"] == "revoiced"}
        for note in pulse["notes"]:
            bar = int((round(note["start_beats"] * 2) / 2) // 4) + 1    # before humanise
            root = 0 if bar <= 80 else 1
            if note["pitch"] % 12 != root:
                self.assertIn(note["pitch"], {to for start, to in revoiced
                                              if abs(start - note["start_beats"]) < 0.06})

    def test_riff_is_his_riff_moved_into_the_new_world(self):
        riff = self.kind("riff")[0]
        self.assertEqual(riff["transpose"], 6)      # C -> F#: a low riff moves up a tritone
        self.assertGreaterEqual(min(n["pitch"] for n in riff["notes"]), 28)
        self.assertEqual(riff["source_bars"], [45, 48])        # in-mode, not the 33-44 rubs
        self.assertIn(riff["source_track"], riff["provenance"])
        self.assertIn(riff["world"], ("W4",))
        dropped = [d for d in self.result["dropped_notes"] if d["part_id"] == riff["part_id"]]
        clash_rows = [d for d in dropped if d["action"] != "rescaled"]
        self.assertEqual(len(clash_rows), riff["clash"]["dropped"] + riff["clash"]["revoiced"])
        for row in clash_rows:
            self.assertIn(row["action"], ("dropped", "revoiced"))
            self.assertIn(row["interval"], ("semitone", "tritone"))
            self.assertTrue(row["against"])

    def test_strict_scale_by_default_every_note_in_its_worlds_scale(self):
        # Job B1: out-of-core passing tones are re-voiced to the nearest tone
        # of the world's scale (a drone world borrows the modal world on its
        # centre: C drone -> C phrygian), and every change is reported
        scales = {"W1": {0, 1, 5, 7, 8, 10}, "W2": {0, 1, 5, 7, 8, 10}, "W4": {6, 4, 1}}
        for part in self.parts:
            if part["kind"] in ("motif", "riff"):
                self.assertLessEqual({n["pitch"] % 12 for n in part["notes"]},
                                     scales[part["world"]], part["part_id"])
                self.assertEqual(set(part["scale"]["pcs"]), scales[part["world"]])
        w1 = [p for p in self.kind("motif") if p["world"] == "W1"][0]
        self.assertIn("C phrygian", w1["scale"]["source"])
        self.assertGreater(len({n["pitch"] % 12 for n in w1["notes"]}), 1)   # not a mono roll
        rescaled = [d for d in self.result["dropped_notes"] if d["action"] == "rescaled"]
        self.assertTrue(rescaled)
        for row in rescaled:
            part = [p for p in self.parts if p["part_id"] == row["part_id"]][0]
            self.assertNotIn(row["pitch"] % 12, part["scale"]["pcs"])
            self.assertIn(row["to"] % 12, part["scale"]["pcs"])
            self.assertLessEqual(abs(row["to"] - row["pitch"]), 3)
            self.assertIn("outside", row["why"])
        for part in self.parts:
            if part["role"] != "kit":
                self.assertEqual(part["clash"]["rescaled"],
                                 sum(1 for d in rescaled if d["part_id"] == part["part_id"]))
        # his riff lands mostly outside the return's three tones: said plainly
        riff = self.kind("riff")[0]
        self.assertIn("re-voiced to", riff["provenance"])
        self.assertIn("the contour is his", riff["provenance"])

    def test_strict_scale_skips_a_motif_on_a_drone_with_no_scale(self):
        # a drone with no modal world on its centre has no scale to re-voice
        # into: no motif (it would collapse to one pitch), said plainly
        hmap = copy.deepcopy(self.hmap)
        hmap["pivots"] = []                       # W3 no longer a held pivot
        result = rd.compose_parts(self.snap, hmap, self.plan, seed=7, kinds=("motif",))
        self.assertNotIn("W3", [p["world"] for p in result["parts"]])
        why = [s["why"] for s in result["skipped"] if s["world"] == "W3"]
        self.assertTrue(why and "no scale" in why[0], result["skipped"])

    def test_strict_scale_off_keeps_passing_tones(self):
        loose = rd.compose_parts(self.snap, self.hmap, self.plan, seed=7, strict_scale=False)
        self.assertFalse([d for d in loose["dropped_notes"] if d["action"] == "rescaled"])
        w2 = [p for p in loose["parts"] if p["part_id"].startswith("motif-W2")][0]
        self.assertIn(3, {n["pitch"] % 12 for n in w2["notes"]})     # Eb: b3, not in the core
        for part in loose["parts"]:
            if part["role"] != "kit":
                self.assertEqual(self.rubs_against_plan(part), [], part["part_id"])

    def test_polymeter_is_an_unequal_loop_on_a_kit(self):
        poly = self.kind("polymeter")[0]
        self.assertEqual(poly["role"], "kit")
        self.assertEqual(poly["loop_beats"], 11)
        self.assertEqual(poly["evidence"], ["set_file", "inferred"])   # 11 bars -> 11 beats is ours
        self.assertIn("Frost", poly["provenance"])

    def test_humanise_follows_the_calibrated_feel(self):
        notes = [n for p in self.parts if p["kind"] in ("motif", "riff") for n in p["notes"]]
        off = [n for n in notes if abs(n["start_beats"] * 4 - round(n["start_beats"] * 4)) > 1e-6]
        self.assertTrue(0.3 < len(off) / len(notes) < 0.8, len(off) / len(notes))
        for n in off:
            delta = abs(n["start_beats"] * 4 - round(n["start_beats"] * 4)) / 4
            lo, hi = ic.FEEL["off_grid_beats"]
            self.assertTrue(lo - 0.001 <= delta <= hi + 0.001 or n["start_beats"] % 4 == 0, n)
        down = [n["velocity"] for n in notes if n.get("downbeat")]
        rest = [n["velocity"] for n in notes if not n.get("downbeat")]
        self.assertGreater(sum(down) / len(down), sum(rest) / len(rest))
        self.assertGreater(len({n["velocity"] for n in notes}), 5)

    def test_same_seed_same_parts_other_seed_other_touch(self):
        again = rd.compose_parts(self.snap, self.hmap, self.plan, seed=7)
        self.assertEqual(again, self.result)
        other = rd.compose_parts(self.snap, self.hmap, self.plan, seed=8)
        self.assertNotEqual(other["parts"], self.result["parts"])


@on_both_versions
class ProposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = real_snapshot()
        cls.decisions = {"revision": 2, "goal": "Keep the warmth, borrow the pacing",
                         "preserve": ["sound"], "borrow": ["pacing", "layering"]}
        cls.refs = synthetic_refs()
        cls.proposal = rd.propose(cls.snap, cls.decisions, cls.refs, intent_tags=["ben-frost"],
                                  seed=3)

    def test_shape(self):
        p = self.proposal
        for key in ("proposal_id", "target_duration", "harmony_map", "layer_arc", "diagnosis",
                    "references", "plan", "parts", "dropped_notes", "uncertain", "assumptions",
                    "freshness_digest", "source_digests", "existing_rubs", "direction"):
            self.assertIn(key, p)
        self.assertTrue(p["proposal_id"].startswith("rd-"))
        self.assertEqual(p["target_duration"], {"requested_s": None, "plan_bars": 116,
                                                "plan_s": 309.3})
        self.assertEqual(p["direction"]["preserve"], ["sound"])
        self.assertEqual(p["direction"]["evidence"], "owner_confirmed")
        self.assertEqual(p["writes"], "none")
        json.dumps(p)

    def test_freshness_is_the_snapshot_digest_over_every_track_of_his(self):
        p = self.proposal
        involved = sorted({t["track_id"] for s in p["plan"]["sections"] for t in s["tracks"]})
        his = {row["track_id"] for row in self.snap["not_his_material"]}
        watched = sorted(t["id"] for t in self.snap["tracks"]
                         if t["id"] not in his or t.get("type") == "group")
        self.assertLess(set(involved), set(watched))
        expected = ps.proposal_freshness(self.snap, {"bars": [1, 108], "track_ids": watched,
                                                     "protected_track_ids": [],
                                                     "decisions_revision": 2})
        self.assertEqual(p["freshness_digest"], expected["digest"])
        self.assertEqual(p["freshness_target"]["track_ids"], watched)
        # every watched track's content, not only the copied ones: context
        # tracks feed the harmony map and the consonance gate (review 72h #1)
        self.assertEqual(sorted(p["source_digests"]), watched)
        self.assertEqual(p["source_blind"], [])
        for tid in watched:
            self.assertEqual(p["source_digests"][tid],
                             self.snap["source_integrity"]["digests"][tid])

    def test_unmuting_a_track_the_plan_does_not_use_invalidates_it(self):
        # review 1.4 M8: the consonance gate depends on every track of his,
        # not only the ones the plan copies
        involved = {t["track_id"] for s in self.proposal["plan"]["sections"]
                    for t in s["tracks"]}
        self.assertNotIn("962", involved)
        unmuted = copy.deepcopy(self.snap)
        track = [t for t in unmuted["tracks"] if t["id"] == "962"][0]
        self.assertTrue(track["muted"])
        track["muted"] = False
        other = rd.propose(unmuted, self.decisions, self.refs, intent_tags=["ben-frost"], seed=3)
        self.assertNotEqual(other["freshness_digest"], self.proposal["freshness_digest"])

    def test_muting_a_group_invalidates_it(self):
        # review 1.4 M8 follow-up: a group mute silences its members' layers
        # without touching any member's own fields
        muted = copy.deepcopy(self.snap)
        group = [t for t in muted["tracks"] if t["id"] == "7"][0]
        group["muted"] = True
        for track in muted["tracks"]:
            if track.get("group_id") == "7":
                track["muted_by_group"] = group["name"]
        other = rd.propose(muted, self.decisions, self.refs, intent_tags=["ben-frost"], seed=3)
        self.assertNotEqual(other["freshness_digest"], self.proposal["freshness_digest"])

    def test_moving_one_of_his_clips_invalidates_it(self):
        moved = copy.deepcopy(self.snap)
        track = [t for t in moved["tracks"] if t["name"] == "Low Drone"][0]
        track["arrangement_clips"][0]["start_beats"] += 32.0
        track["arrangement_clips"][0]["end_beats"] += 32.0
        other = rd.propose(moved, self.decisions, self.refs, intent_tags=["ben-frost"], seed=3)
        self.assertNotEqual(other["freshness_digest"], self.proposal["freshness_digest"])
        self.assertNotEqual(other["proposal_id"], self.proposal["proposal_id"])

    def test_deterministic_and_seeded(self):
        again = rd.propose(self.snap, self.decisions, self.refs, intent_tags=["ben-frost"], seed=3)
        self.assertEqual(again, self.proposal)
        other = rd.propose(self.snap, self.decisions, self.refs, intent_tags=["ben-frost"], seed=4)
        self.assertNotEqual(other["proposal_id"], self.proposal["proposal_id"])
        self.assertNotEqual(other["parts"], self.proposal["parts"])
        self.assertEqual(other["plan"], self.proposal["plan"])

    def test_pure_no_io(self):
        with patch("builtins.open", side_effect=AssertionError("ref_develop opened a file")), \
                patch.object(pp, "operation_session", side_effect=AssertionError("touched Live")):
            rd.propose(self.snap, self.decisions, self.refs, intent_tags=["ben-frost"], seed=3)

    def test_existing_rubs_and_uncertainty_are_said(self):
        p = self.proposal
        rubs = {tuple(r["bars"]): r for r in p["existing_rubs"]}
        self.assertGreater(rubs[(33, 44)]["sustained_count"], 0)
        self.assertIn(("F#", "G"), {tuple(x["names"]) for x in rubs[(33, 44)]["top"]})
        text = " ".join(p["uncertain"])
        self.assertIn("sidechain", text)
        self.assertIn("phrygian by", text)
        self.assertIn("key change bridge", text)
        self.assertTrue(any("inferred from clip boundaries" in a for a in p["assumptions"]))

    def test_depart_reaches_the_ranking_through_propose(self):
        p = rd.propose(self.snap, {**self.decisions, "depart": ["ending"]}, self.refs,
                       intent_tags=["ben-frost"], seed=3)
        self.assertEqual([d["need"] for d in p["references"]["departed"]], ["strip_ending"])
        self.assertEqual(p["direction"]["depart"], ["ending"])

    def test_strict_scale_is_on_by_default_and_can_be_turned_off(self):
        actions = {d["action"] for d in self.proposal["dropped_notes"]}
        self.assertIn("rescaled", actions)
        loose = rd.propose(self.snap, self.decisions, self.refs, intent_tags=["ben-frost"],
                           seed=3, strict_scale=False)
        self.assertNotIn("rescaled", {d["action"] for d in loose["dropped_notes"]})
        self.assertNotEqual(loose["proposal_id"], self.proposal["proposal_id"])

    def test_hazard_copies_are_said_in_uncertain(self):
        text = " ".join(self.proposal["uncertain"])
        self.assertIn("Voice FX", text)
        self.assertIn("hardware", text)

    def test_compose_off_gives_a_plan_without_parts(self):
        p = rd.propose(self.snap, self.decisions, self.refs, seed=3, compose=False)
        self.assertEqual((p["parts"], p["dropped_notes"]), ([], []))


def device_snapshot(device):
    with patch.object(pp, "operation_session", device.operation()):
        return asyncio.run(moment.read_piece_snapshot())


@on_both_versions
class ProposalIdentityTests(unittest.TestCase):
    """review 72h finding 1: the proposal id is bound to what was reviewed —
    the plan, the parts and every input (content digests, tempo, meter,
    decisions) — so a changed Set gives a new id and an edited proposal no
    longer matches its own id."""

    @classmethod
    def setUpClass(cls):
        cls.snap = real_snapshot()
        cls.proposal = rd.propose(cls.snap, {}, [], seed=0, compose=False)

    def test_the_id_is_recomputable_and_survives_json(self):
        p = self.proposal
        self.assertEqual(rd.proposal_id_of(p), p["proposal_id"])
        self.assertEqual(rd.proposal_id_of(json.loads(json.dumps(p))), p["proposal_id"])

    def test_an_edited_plan_no_longer_matches_its_id(self):
        edited = copy.deepcopy(self.proposal)
        edited["plan"]["sections"][0]["tracks"].pop()
        self.assertNotEqual(rd.proposal_id_of(edited), edited["proposal_id"])
        with_parts = copy.deepcopy(self.proposal)
        with_parts["parts"] = [{"part_id": "x", "notes": []}]
        self.assertNotEqual(rd.proposal_id_of(with_parts), with_parts["proposal_id"])

    def test_a_note_edit_changes_the_source_digest_and_the_id(self):
        # the review's probe: transpose clip 287 without moving it
        device = PieceDevice(live_api=True)
        for note in device.clip_live["287"]["notes"]:
            note["pitch"] += 1
        edited = rd.propose(device_snapshot(device), {}, [], seed=0, compose=False)
        self.assertNotEqual(edited["source_digests"]["3"], self.proposal["source_digests"]["3"])
        self.assertNotEqual(edited["proposal_id"], self.proposal["proposal_id"])
        self.assertTrue(rd.proposal_validity(self.proposal, device_snapshot(device), {})
                        ["problems"])

    def test_a_tempo_edit_changes_freshness_and_the_id(self):
        faster = copy.deepcopy(self.snap)
        faster["set"]["tempo"] = 120.0
        other = rd.propose(faster, {}, [], seed=0, compose=False)
        self.assertNotEqual(other["target_duration"]["plan_s"],
                            self.proposal["target_duration"]["plan_s"])
        self.assertNotEqual(other["freshness_digest"], self.proposal["freshness_digest"])
        self.assertNotEqual(other["proposal_id"], self.proposal["proposal_id"])
        kinds = {x["kind"] for x in rd.proposal_validity(self.proposal, faster, {})["problems"]}
        self.assertIn("arrangement", kinds)

    def test_validity_on_the_same_set_is_clean(self):
        self.assertEqual(rd.proposal_validity(self.proposal, self.snap, {"revision": 0}),
                         {"ok": True, "problems": []})

    def test_a_context_track_edit_is_seen(self):
        # 962 is muted and never copied, but it is his: its content is watched
        involved = {t["track_id"] for s in self.proposal["plan"]["sections"]
                    for t in s["tracks"]}
        self.assertNotIn("962", involved)
        changed = copy.deepcopy(self.snap)
        changed["source_integrity"]["digests"]["962"] = "0" * 64
        problems = rd.proposal_validity(self.proposal, changed, {})["problems"]
        self.assertEqual([(x["kind"], x["track_ids"]) for x in problems],
                         [("content", ["962"])])

    def test_a_blind_track_refuses(self):
        blind = copy.deepcopy(self.snap)
        del blind["source_integrity"]["digests"]["3"]
        blind["source_integrity"]["blind"] = [{"track_id": "3", "track": "Low Drone",
                                               "reason": "notes_unread", "clips": []}]
        kinds = {x["kind"] for x in rd.proposal_validity(self.proposal, blind, {})["problems"]}
        self.assertIn("blind", kinds)
        made_blind = rd.propose(blind, {}, [], seed=0, compose=False)
        self.assertIn("3", made_blind["source_blind"])
        kinds = {x["kind"] for x in rd.proposal_validity(made_blind, blind, {})["problems"]}
        self.assertIn("blind", kinds)

    def test_a_track_added_or_removed_refuses(self):
        # review 1.5a minor: the proposal records every track of the Set
        self.assertEqual(self.proposal["freshness_target"]["track_set"],
                         sorted(t["id"] for t in self.snap["tracks"]))
        added = copy.deepcopy(self.snap)
        added["tracks"].append(dict(copy.deepcopy(added["tracks"][-1]), id="9999",
                                    name="new layer", arrangement_clips=[]))
        problems = rd.proposal_validity(self.proposal, added, {"revision": 0})["problems"]
        self.assertEqual([(x["kind"], x["added"]) for x in problems], [("tracks", ["9999"])])
        self.assertIn("added new layer", problems[0]["say"])
        older = copy.deepcopy(self.proposal)
        del older["freshness_target"]["track_set"]
        kinds = {x["kind"] for x in rd.proposal_validity(older, self.snap, {})["problems"]}
        self.assertIn("identity", kinds)

    def test_a_new_protection_or_decision_refuses(self):
        for decisions in ({"revision": 1}, {"revision": 0, "protect_track_ids": ["23"]}):
            with self.subTest(decisions=decisions):
                kinds = {x["kind"] for x in rd.proposal_validity(
                    self.proposal, self.snap, decisions)["problems"]}
                self.assertEqual(kinds, {"decisions"})


def _audio_device(start_bar, bars=8):
    """The capture with one audio clip placed on Omnisphere (track 11, an
    audio track with no clips in the capture)."""
    device = PieceDevice(live_api=True)
    index = next(i for i, t in device.track_records.items() if t.get("id") == "11")
    device.track_records[index]["arrangementClips"] = [{
        "id": "review-audio", "type": "audio", "name": "Audio bed",
        "arrangementStart": "%d|1" % start_bar, "arrangementLength": "%dbar" % bars,
        "timeSignature": "4/4", "start": "1|1", "end": "%d|1" % (bars + 1), "looping": True,
        "sampleFile": "/tmp/review-audio.wav"}]
    return device


def _copies(plan, track_id):
    return [(row["role"], e["at_bars"], e["clip_ids"], e.get("material"))
            for row in plan["sections"] for e in row["tracks"] if e["track_id"] == track_id]


@on_both_versions
class MaterialCompletenessTests(unittest.TestCase):
    """review 72h finding 2: the plan's baseline is every placed clip span —
    notes, audio, unread MIDI, known-silent — not only the notes it could
    read; harmony it could not read is never claimed consonant."""

    def test_protected_audio_stays_in_the_plan(self):
        snap = device_snapshot(_audio_device(1))
        self.assertEqual([u["clip_id"] for u in snap["unheard"]], ["review-audio"])
        plan = rd.propose(snap, {"protect_track_ids": ["11"]}, compose=False)["plan"]
        copies = _copies(plan, "11")
        self.assertEqual(copies, [("opening", [1, 32], ["review-audio"], ["audio"])])
        opening = plan["sections"][0]
        self.assertIn("Omnisphere plays as you placed it (protected)",
                      [p["what"] for p in opening["preserve"]])

    def test_protected_audio_at_the_end_plays_through_the_coda(self):
        plan = rd.propose(device_snapshot(_audio_device(101)), {"protect_track_ids": ["11"]},
                          compose=False)["plan"]
        roles = [c[0] for c in _copies(plan, "11")]
        self.assertEqual(roles, ["real peak", "coda"])
        coda = plan["sections"][-1]
        self.assertEqual([e["at_bars"] for e in coda["tracks"] if e["track_id"] == "11"],
                         [coda["bars"]])

    def test_audio_is_never_added_as_fitting_a_world(self):
        # 93-100 only: the real-peak rule must not copy it into 101-108 as a
        # layer "inside this world's core" — its pitches were never heard
        plan = rd.propose(device_snapshot(_audio_device(93)), {}, compose=False)["plan"]
        self.assertEqual([c[1] for c in _copies(plan, "11")], [[93, 100]])
        later = [r for r in plan["sections"] if r["source_bars"] == [101, 108]][0]
        self.assertTrue(any(c.startswith("Omnisphere not added here") for c in later["changes"]),
                        later["changes"])

    def test_audio_under_a_part_makes_its_clash_check_partial(self):
        p = rd.propose(device_snapshot(_audio_device(1, bars=108)), {}, seed=0)
        self.assertTrue(p["parts"])
        for part in p["parts"]:
            if part["role"] == "kit":
                continue
            with self.subTest(part=part["part_id"]):
                self.assertIsNone(part["clash"]["remaining"])
                self.assertEqual(part["clash"]["checked"], "partial")
                self.assertIn("Omnisphere", {u["track"] for u in part["clash"]["unverified"]})
        for row in p["plan"]["sections"]:
            if any(e["track_id"] == "11" for e in row["tracks"]):
                self.assertFalse(row["clashes"]["complete"])
                self.assertIn("Omnisphere", {u["track"] for u in row["clashes"]["unverified"]})

    def test_unread_midi_stays_in_the_plan_and_never_claims_zero_clashes(self):
        snap = device_snapshot(PieceDevice(live_api=False))
        self.assertGreater(snap["notes_read"]["unread_clips"], 0)
        roles = {r["track_id"]: r["role"] for r in rd.track_roles(snap)}
        p = rd.propose(snap, {}, seed=0)
        plan = p["plan"]
        copied = {cid for row in plan["sections"] for e in row["tracks"] for cid in e["clip_ids"]}
        held = set(plan["held_back"])
        for track in snap["tracks"]:
            if not track["in_layers"]:
                continue
            for clip in track["arrangement_clips"]:
                if clip.get("notes_status") == "unread" and not clip["muted"]:
                    with self.subTest(track=track["name"], clip=clip["id"]):
                        self.assertTrue(clip["id"] in copied or track["name"] in held)
                        self.assertNotEqual(roles[track["id"]], "empty")
        unread_rows = [row for row in plan["sections"]
                       if any("unread" in (e.get("material") or []) for e in row["tracks"])]
        self.assertTrue(unread_rows)
        for row in unread_rows:
            self.assertFalse(row["clashes"]["complete"])
        # a part over unread material claims no count; one over a truly
        # silent gap (nothing placed) is a complete check
        pitched = [x for x in p["parts"] if x["role"] != "kit"]
        partial = 0
        for part in pitched:
            lo, hi = part["plan_bars"]
            under = [e for row in plan["sections"] for e in row["tracks"]
                     if "unread" in e["material"] and e["at_bars"][0] <= hi
                     and e["at_bars"][1] >= lo]
            with self.subTest(part=part["part_id"]):
                if under:
                    partial += 1
                    self.assertIsNone(part["clash"]["remaining"])
                    self.assertEqual(part["clash"]["checked"], "partial")
                else:
                    self.assertEqual(part["clash"]["checked"], "complete")
        self.assertGreater(partial, 0)
        self.assertTrue(any("not read" in line for line in p["uncertain"]))

    def test_the_real_capture_plan_is_unchanged_and_complete(self):
        p = rd.propose(real_snapshot(), {}, seed=0)
        for row in p["plan"]["sections"]:
            self.assertTrue(row["clashes"]["complete"])
            for entry in row["tracks"]:
                self.assertEqual(entry["material"], ["notes"])
        for part in p["parts"]:
            if part["role"] != "kit":
                self.assertEqual(part["clash"]["checked"], "complete")
                self.assertIsInstance(part["clash"]["remaining"], int)


def _shift_from(device, first_bar, bars):
    """Move every clip at or after ``first_bar`` by ``bars`` (negative:
    earlier), in both the Producer Pal placement and the Live API read."""
    for track in device.track_records.values():
        for clip in track.get("arrangementClips") or []:
            bar, beat = clip["arrangementStart"].split("|")
            if float(bar) >= first_bar:
                clip["arrangementStart"] = "%d|%s" % (int(bar) + bars, beat)
                live = device.clip_live.get(clip["id"])
                if live:
                    live["values"]["start_time"] += 4 * bars
                    live["values"]["end_time"] += 4 * bars


def _shorten(device, start_bar, bars):
    """Every clip placed at ``start_bar`` lasts ``bars`` bars."""
    for track in device.track_records.values():
        for clip in track.get("arrangementClips") or []:
            if clip["arrangementStart"] == "%d|1" % start_bar and \
                    clip["arrangementLength"].endswith("bar"):
                clip["arrangementLength"] = "%dbar" % bars
                live = device.clip_live.get(clip["id"])
                if live:
                    live["values"]["end_time"] = live["values"]["start_time"] + 4 * bars


def _out_of_section(plan):
    return [(row["bars"], e["track"], e["at_bars"]) for row in plan["sections"]
            for e in row["tracks"]
            if e["at_bars"][0] < row["bars"][0] or e["at_bars"][1] > row["bars"][1]]


@on_both_versions
class ShortBridgeTests(unittest.TestCase):
    """review 72h finding 3: the bridge figure fits the gap and its source."""

    def test_one_to_four_bar_gaps(self):
        for gap in (1, 2, 3, 4):
            with self.subTest(gap=gap):
                device = PieceDevice(live_api=True)
                _shift_from(device, 93, gap - 8)
                snap = device_snapshot(device)
                self.assertIn([85, 84 + gap], [x["bars"] for x in snap["sections"] if x["silent"]])
                plan = rd.propose(snap, {}, seed=0, compose=False)["plan"]
                self.assertEqual(_out_of_section(plan), [])
                bridge = [r for r in plan["sections"] if r["source_bars"] == [85, 84 + gap]][0]
                figure = [e for e in bridge["tracks"] if e["part"] == "figure"]
                for entry in figure:
                    # the held tone comes first, alone: the figure never covers the gap
                    self.assertLess(entry["at_bars"][1] - entry["at_bars"][0] + 1, gap)
                    self.assertEqual(entry["at_bars"][1], bridge["bars"][1])
                if gap == 1:
                    self.assertEqual(figure, [])
                    self.assertTrue(any("one bar" in q for q in bridge["questions"]),
                                    bridge["questions"])

    def test_the_eight_bar_gap_keeps_its_four_bar_figure(self):
        plan = rd.propose(real_snapshot(), {}, seed=0, compose=False)["plan"]
        bridge = [r for r in plan["sections"] if r["role"] == "bridge"][0]
        figure = [e for e in bridge["tracks"] if e["part"] == "figure"][0]
        self.assertEqual((figure["from_bars"], figure["at_bars"]), ([93, 96], [89, 92]))

    def test_a_short_return_section_bounds_the_figure(self):
        device = PieceDevice(live_api=True)
        _shorten(device, 93, 2)
        _shift_from(device, 101, -6)
        snap = device_snapshot(device)
        self.assertIn([93, 94], [x["bars"] for x in snap["sections"]])
        plan = rd.propose(snap, {}, seed=0, compose=False)["plan"]
        self.assertEqual(_out_of_section(plan), [])
        bridge = [r for r in plan["sections"] if r["role"] == "bridge"][0]
        for entry in bridge["tracks"]:
            if entry["part"] == "figure":
                self.assertGreaterEqual(entry["from_bars"][0], 93)
                self.assertLessEqual(entry["from_bars"][1], 94)

    def test_every_entry_fits_its_section_on_the_real_capture(self):
        for target in (None, 420):
            plan = rd.propose(real_snapshot(), {}, seed=0, compose=False,
                              target_duration_s=target)["plan"]
            self.assertEqual(_out_of_section(plan), [])


@on_both_versions
class LocatorBoundsTests(unittest.TestCase):
    """review 72h finding 4: a locator at or past the song's end (an old end
    marker after shortening) makes no section and never stretches one past
    the song; it is ignored and said."""

    def check(self, device, locators):
        device.overview["locators"] = [{"name": n, "time": t} for n, t in locators]
        snap = device_snapshot(device)
        bars = snap["set"]["bars"]
        for section in snap["sections"]:
            self.assertTrue(1 <= section["bars"][0] <= section["bars"][1] <= bars,
                            (section["bars"], bars))
        p = rd.propose(snap, {}, seed=0)
        for seg in p["harmony_map"]["segments"]:
            self.assertLessEqual(seg["bars"][1], bars)
        return snap

    def test_a_marker_past_the_song(self):
        snap = self.check(PieceDevice(live_api=True), [("Start", "1|1"), ("Old end", "129|1")])
        self.assertEqual([x["name"] for x in snap["locators_ignored"]], ["Old end"])
        self.assertTrue(any("Old end" in line for line in snap["uncertain"]))
        # one usable locator: sections are inferred, as with one locator
        self.assertTrue(snap["section_rule"].startswith("inferred"))

    def test_an_exact_end_marker_and_markers_inside(self):
        snap = self.check(PieceDevice(live_api=True),
                          [("A", "1|1"), ("B", "61|1"), ("C", "108|1"), ("End", "109|1"),
                           ("Old", "150|1")])
        self.assertEqual([x["bars"] for x in snap["sections"]], [[1, 60], [61, 107], [108, 108]])
        self.assertEqual([x["name"] for x in snap["locators_ignored"]], ["End", "Old"])

    def test_markers_before_at_and_after_the_clips(self):
        device = PieceDevice(live_api=True)
        _shift_from(device, 1, 4)           # the first clip now starts at bar 5
        snap = self.check(device, [("Pre", "1|1"), ("In", "37|1"), ("End", "113|1"),
                                   ("After", "129|1")])
        self.assertEqual(snap["set"]["bars"], 112)
        self.assertEqual([x["bars"] for x in snap["sections"]], [[1, 36], [37, 112]])
        self.assertEqual([x["name"] for x in snap["locators_ignored"]], ["End", "After"])

    def test_no_marker_past_the_song_ignores_nothing(self):
        snap = self.check(PieceDevice(live_api=True), [("A", "1|1"), ("B", "33|1")])
        self.assertEqual(snap["locators_ignored"], [])
        self.assertEqual([x["bars"] for x in snap["sections"]], [[1, 32], [33, 108]])


if __name__ == "__main__":
    unittest.main()
