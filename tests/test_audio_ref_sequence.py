import json
from pathlib import Path
import unittest

from tools.audio_ref import reference_sequence, validate_card


FIXTURES = Path(__file__).parent / "fixtures" / "audio_ref"


def load_json(path):
    return json.loads(path.read_text())


class ReferenceSequenceTests(unittest.TestCase):
    def setUp(self):
        # Synthetic v3 card and measured block (tests/fixtures/audio_ref/make_synthetic.py):
        # a 640 s arc at 142 BPM with sections at 00:00 and 01:20, a beat grid phased
        # at 0.3 s, and one designed dip over windows 181-195 (305.9-331.3 s).
        self.card = load_json(FIXTURES / "longarc_v3.json")
        self.meta = {
            "ref_id": "longarc",
            "duration_s": 640.0,
            "listen_verified": False,
            "measured": load_json(FIXTURES / "longarc_measured.json"),
        }

    def test_unverified_card_exposes_timestamps_without_heard_claims(self):
        result = reference_sequence(self.card, self.meta)
        self.assertEqual(result["contract_version"], "audio-ref-card-v3")
        self.assertFalse(result["evidence_summary"]["model_heard"])
        self.assertTrue(result["sections"])
        self.assertEqual(result["sections"][0]["evidence"], "model_unverified")
        self.assertIsNone(result["sections"][0].get("label"))
        self.assertTrue(result["warnings"])
        self.assertNotIn("ref_bar", result["sections"][0])

    def test_verified_card_emits_heard_facts_and_owner_alignment(self):
        meta = dict(self.meta, listen_verified=True)
        result = reference_sequence(
            self.card, meta,
            {"downbeat_s": 0.3, "confirmed_sections": ["00:00", "01:20"]},
        )
        self.assertTrue(result["evidence_summary"]["model_heard"])
        self.assertEqual(result["sections"][0]["evidence"], "owner_confirmed")
        self.assertEqual(result["sections"][0]["label"], self.card["sections"][0]["label"])
        self.assertEqual(result["sections"][1]["evidence"], "owner_confirmed")
        self.assertEqual(result["alignment"]["ref_bar_available"], True)
        self.assertIn("ref_bar", result["sections"][0])
        self.assertEqual(result["sections"][0]["ref_bar_evidence"], "owner_confirmed")

    def test_tempo_evidence_is_per_field_and_bar_uses_measured_beat_grid(self):
        result = reference_sequence(
            self.card, self.meta,
            {"downbeat_s": 0.3, "confirmed_sections": ["01:20"]},
        )
        self.assertEqual(result["tempo"]["grid_evidence"], "model_unverified")
        self.assertEqual(result["tempo"]["feel_evidence"], "model_unverified")
        self.assertEqual(result["tempo"]["measured_bpm_evidence"], "measured")
        self.assertEqual(result["sections"][1]["ref_bar"], 48)
        self.assertIn("meter assumption: 4/4 from reference card",
                      result["assumptions"])

    def test_loudness_windows_use_measured_window_timestamps(self):
        result = reference_sequence(dict(self.card, sections=[]), self.meta)
        self.assertEqual(result["loudness"]["windows_s"],
                         self.meta["measured"]["energy_bar_start_s"])

    def test_measured_only_is_loudness_evidence_not_layer_authorization(self):
        card = dict(self.card, sections=[])
        result = reference_sequence(card, self.meta)
        self.assertTrue(result["evidence_summary"]["measured"])
        self.assertIsNotNone(result["loudness"])
        self.assertEqual(result["loudness"]["evidence"], "measured")
        thinning = [m for m in result["moves"] if m["kind"] == "thin_before_return"]
        for move in thinning:
            self.assertFalse(move["authorizes_layer_removal"])
            self.assertIn("missing_evidence", move)

    def test_longarc_owner_drums_in_at_0530_authorizes_thin_before_return(self):
        # Pair (i): the synthetic v2 card (eight sections, including 04:10 and
        # 05:30) with longarc_measured.json (1.69 s windows). Owner-confirmed
        # drums layers_in at 05:30 authorizes the designed ≈306–331 s dip: the
        # span ends within 5 s of the 05:30 section boundary.
        card = load_json(FIXTURES / "longarc_v2_stale_meta" / "card.json")
        validate_card(card)
        result = reference_sequence(card, self.meta, {"confirmed_layer_changes": [
            {"t": "05:30", "layers_in": ["drums"]},
        ]})
        move = next(move for move in result["moves"]
                    if move["kind"] == "thin_before_return"
                    and 300 <= move["t_s"] <= 350)
        self.assertTrue(move["authorizes_layer_removal"])
        self.assertEqual(move["supported_by"], ["measured", "owner_confirmed"])
        self.assertEqual(move["section_t"], "04:10")
        self.assertLessEqual(abs(move["t_s"] + move["span_s"] - 330.0), 5.0)

    def test_return_section_layers_in_authorizes_qualifying_dip_at_boundary(self):
        card = dict(self.card, sections=[
            {"t": "00:00", "layers_in": ["bass"]},
            {"t": "00:16", "layers_out": ["drums"]},
            {"t": "00:24", "layers_in": ["drums"]},
        ])
        meta = dict(self.meta, measured={"energy_per_bar": [10] * 8 + [6] * 4 + [10] * 4,
            "energy_bar_start_s": [i * 2 for i in range(16)], "bar_len_s": 2.0})
        meta["listen_verified"] = True
        result = reference_sequence(card, meta)
        move = next(move for move in result["moves"]
                    if move["kind"] == "thin_before_return" and move["t_s"] == 16)
        self.assertTrue(move["authorizes_layer_removal"])
        self.assertEqual(move["section_t"], "00:16")
        self.assertEqual(move["supported_by"], ["measured", "model_heard"])

    def test_longarc_owner_drums_out_at_0120_authorizes_nothing(self):
        # Pair (ii): same synthetic fixtures, but the owner confirms drums
        # layers_out at 01:20. That boundary is far from the designed
        # ≈306–331 s dip, so no thin_before_return is authorized — while the
        # measured dip itself is still reported as a loudness observation.
        card = load_json(FIXTURES / "longarc_v2_stale_meta" / "card.json")
        validate_card(card)
        result = reference_sequence(card, self.meta, {"confirmed_layer_changes": [
            {"t": "01:20", "layers_out": ["drums"]},
        ]})
        thins = [move for move in result["moves"]
                 if move["kind"] == "thin_before_return"]
        self.assertTrue(thins)
        for move in thins:
            self.assertFalse(move["authorizes_layer_removal"])
            self.assertIn("missing_evidence", move)
        self.assertTrue(result["evidence_summary"]["owner_confirmed"])

    def test_owner_confirmation_requires_section_alignment(self):
        card = load_json(FIXTURES / "longarc_v2_stale_meta" / "card.json")
        validate_card(card)
        result = reference_sequence(card, self.meta, {"confirmed_sections": ["02:55"]})
        self.assertFalse(result["evidence_summary"]["owner_confirmed"])
        self.assertTrue(any("could not align" in warning for warning in result["warnings"]))

    def test_owner_confirmed_section_is_typed_as_owner_evidence(self):
        result = reference_sequence(self.card, self.meta, {"confirmed_sections": ["01:20"]})
        section = next(section for section in result["sections"] if section["t"] == "01:20")
        self.assertEqual(section["evidence"], "owner_confirmed")

    def test_empty_aligned_layer_confirmation_warns_nothing(self):
        # An owner confirmation that aligns to a card section but names no
        # layer change is not evidence — and must not warn about alignment.
        result = reference_sequence(self.card, self.meta, {"confirmed_layer_changes": [
            {"t": "01:20"},
        ]})
        self.assertFalse(result["evidence_summary"]["owner_confirmed"])
        self.assertFalse([warning for warning in result["warnings"]
                          if "could not align" in warning])

    def test_phrase_wobble_does_not_count_as_thinning(self):
        values = [10] * 8 + [6, 6, 6] + [10] * 8
        meta = dict(self.meta, measured={"energy_per_bar": values,
            "energy_bar_start_s": [i * 2 for i in range(len(values))], "bar_len_s": 2.0})
        result = reference_sequence(dict(self.card, sections=[]), meta)
        self.assertNotIn("thin_before_return", {m["kind"] for m in result["moves"]})

    def test_sustained_thinning_requires_four_windows_and_recovery(self):
        values = [10] * 8 + [6] * 4 + [10] * 4
        meta = dict(self.meta, measured={"energy_per_bar": values,
            "energy_bar_start_s": [i * 2 for i in range(len(values))], "bar_len_s": 2.0})
        result = reference_sequence(dict(self.card, sections=[]), meta)
        move = next(m for m in result["moves"] if m["kind"] == "thin_before_return")
        self.assertEqual(move["t_s"], 16)

    def test_delayed_entry_requires_verified_layer_in_evidence(self):
        card = dict(self.card, sections=[
            {"t": "00:00", "layers_in": ["bass"]},
            {"t": "00:08", "layers_in": ["bass", "drums"]},
        ])
        result = reference_sequence(card, dict(self.meta, listen_verified=True))
        delayed = [m for m in result["moves"] if m["kind"] == "delayed_entry"]
        self.assertEqual(len(delayed), 1)
        self.assertEqual(delayed[0]["evidence"], ["model_heard"])

    def test_returning_roles_are_not_delayed_entries(self):
        # Review fix (item 3): a role that returns after having been present
        # earlier is not a delayed entry. Synthetic v3 sections (shape from the
        # reviewer's owner_probe.py v3 block): drums
        # enter 01:25, leave 04:20, return 05:40; bass present from 00:00.
        card = dict(self.card, sections=[
            {"t": "00:00", "layers_in": ["guitar", "bass"]},
            {"t": "01:25", "layers_in": ["drums"]},
            {"t": "04:20", "layers_out": ["drums"]},
            {"t": "05:40", "layers_in": ["drums", "lead"]},
            {"t": "07:15", "layers_in": ["bass"]},
        ])
        result = reference_sequence(card, dict(self.meta, listen_verified=True))
        delayed = [m for m in result["moves"] if m["kind"] == "delayed_entry"]
        self.assertFalse(any("drums" in m["layers"] and m["t_s"] == 340.0
                             for m in delayed))
        self.assertFalse(any(m["t_s"] == 435.0 for m in delayed))
        # First appearances still count: drums at 01:25, lead at 05:40.
        self.assertTrue(any(m["t_s"] == 85.0 and m["layers"] == ["drums"]
                            for m in delayed))
        self.assertTrue(any(m["t_s"] == 340.0 and m["layers"] == ["lead"]
                            for m in delayed))

    def test_owner_confirmed_return_is_not_delayed_entry(self):
        # Review fix (item 3): owner "drums in 05:30" on the synthetic v2 card
        # (tests/fixtures/audio_ref/longarc_v2_stale_meta/card.json, whose
        # 01:20 section describes ride and tom entering) is not a delayed
        # entry of drums when drums were active earlier — here via an earlier
        # owner confirmation at 01:20.
        card = load_json(FIXTURES / "longarc_v2_stale_meta" / "card.json")
        validate_card(card)
        result = reference_sequence(
            card, dict(self.meta, listen_verified=False),
            {"confirmed_layer_changes": [
                {"t": "01:20", "layers_in": ["drums"]},
                {"t": "05:30", "layers_in": ["drums"]},
            ]})
        delayed = [m for m in result["moves"] if m["kind"] == "delayed_entry"]
        self.assertEqual(len(delayed), 1)
        self.assertEqual(delayed[0]["t_s"], 80.0)
        self.assertEqual(delayed[0]["layers"], ["drums"])
        self.assertEqual(delayed[0]["evidence"], ["owner_confirmed"])

    def test_v3_first_t_marks_role_heard_earlier(self):
        # Review fix (item 3): a v3 layers[].first_t earlier than the entry
        # section means the role was present before, so it is a return.
        card = dict(self.card, sections=[
            {"t": "00:00", "layers_in": ["bass"]},
            {"t": "05:40", "layers_in": ["drums"]},
        ], layers=[{"role": "drums", "timbre": "kit", "first_t": "01:25"}])
        result = reference_sequence(card, dict(self.meta, listen_verified=True))
        delayed = [m for m in result["moves"] if m["kind"] == "delayed_entry"]
        self.assertFalse(any("drums" in m["layers"] for m in delayed))
        # A first_t at the entry section itself is the first appearance.
        card = dict(card, layers=[
            {"role": "drums", "timbre": "kit", "first_t": "05:40"}])
        result = reference_sequence(card, dict(self.meta, listen_verified=True))
        delayed = [m for m in result["moves"] if m["kind"] == "delayed_entry"]
        self.assertTrue(any(m["t_s"] == 340.0 and m["layers"] == ["drums"]
                            for m in delayed))

    def test_loudness_rise_alone_is_not_delayed_entry(self):
        meta = dict(self.meta, measured={"energy_per_bar": [1, 1, 1, 2, 2],
            "energy_bar_start_s": [0, 2, 4, 6, 8], "bar_len_s": 2.0})
        result = reference_sequence(dict(self.card, sections=[]), meta)
        self.assertNotIn("delayed_entry", {m["kind"] for m in result["moves"]})

    def test_late_peak_is_a_sustained_span(self):
        values = [1] * 20 + [5] * 4 + [1] * 4
        meta = dict(self.meta, duration_s=56, measured={"energy_per_bar": values,
            "energy_bar_start_s": [i * 2 for i in range(len(values))], "bar_len_s": 2.0})
        result = reference_sequence(dict(self.card, sections=[]), meta)
        peak = next(m for m in result["moves"] if m["kind"] == "late_peak")
        self.assertEqual(peak["span_windows"], 4)

    def test_late_peak_requires_non_silent_contrasting_late_rise(self):
        # Shape from measure_audio's measured block: energy_per_bar,
        # energy_bar_start_s and bar_len_s. Synthetic windows reproduce the
        # review probe's 2-second measured-window cadence.
        def moves_for(values):
            meta = dict(self.meta, duration_s=len(values) * 2,
                        measured={"energy_per_bar": values,
                                  "energy_bar_start_s": [i * 2 for i in range(len(values))],
                                  "bar_len_s": 2.0})
            return reference_sequence(dict(self.card, sections=[]), meta)["moves"]

        for values in ([0] * 16, [1] * 16, [5] * 16, [5] * 8 + [1] * 8):
            with self.subTest(values=values):
                self.assertNotIn("late_peak", {move["kind"] for move in moves_for(values)})

        values = [1] * 20 + [5] * 8
        peak = next(move for move in moves_for(values) if move["kind"] == "late_peak")
        self.assertEqual(peak["t_s"], 40)
        self.assertEqual(peak["span_windows"], 4)
        self.assertEqual(peak["evidence"], ["measured"])

    def test_late_peak_uses_onset_sustain_and_sanitized_envelope(self):
        # Shape from measure_audio's measured block; values model real
        # energy_per_bar windows, including numerical noise and invalid samples.
        import random

        def kinds(values):
            meta = dict(self.meta, duration_s=len(values) * 2,
                        measured={"energy_per_bar": values,
                                  "energy_bar_start_s": [i * 2 for i in range(len(values))],
                                  "bar_len_s": 2.0})
            return [move for move in reference_sequence(
                dict(self.card, sections=[]), meta)["moves"]
                if move["kind"] == "late_peak"]

        self.assertFalse(kinds([0.1] * 10 + [0.5] * 6))  # onset at 62.5%
        self.assertFalse(kinds([0.1] * 15 + [1.0]))  # isolated spike
        self.assertFalse(kinds([-1.0] * 12 + [0.5] * 4))
        self.assertFalse(kinds([float("nan")] * 12 + [0.5] * 4))

        hits = 0
        for seed in range(200):
            rng = random.Random(seed)
            plateau = [0.5 * (1 + rng.uniform(-0.03, 0.03)) for _ in range(6)]
            hits += bool(kinds([0.1] * 10 + plateau))
        self.assertEqual(hits, 0)

        late = kinds([0.1] * 20 + [0.5] * 8)
        self.assertEqual(late[0]["t_s"], 40)
        self.assertEqual(kinds([0.1 * (i + 1) for i in range(16)]), [])

    def _late_peaks(self, values):
        # Shape from measure_audio's measured block (2-second windows).
        meta = dict(self.meta, duration_s=len(values) * 2,
                    measured={"energy_per_bar": values,
                              "energy_bar_start_s": [i * 2 for i in range(len(values))],
                              "bar_len_s": 2.0})
        return [move for move in reference_sequence(
            dict(self.card, sections=[]), meta)["moves"] if move["kind"] == "late_peak"]

    def test_rising_plateau_from_mid_track_is_not_a_late_peak(self):
        # Round-3 probe r2_peak: a plateau that starts at 62.5% and keeps
        # drifting up puts its 90%-of-max onset past 70%; the rise itself
        # began mid-track, so the position test belongs at the run's start.
        import random
        for drift in (0.0, 0.1, 0.2, 0.3):
            with self.subTest(drift=drift):
                values = [1.0] * 25 + [2.0 * (1 + drift * i / 14) for i in range(15)]
                self.assertEqual(self._late_peaks(values), [])
        hits = 0
        for seed in range(200):
            rng = random.Random(seed)
            drift = 0.2 + 0.1 * rng.random()
            values = ([1.0 * (1 + rng.uniform(-0.03, 0.03)) for _ in range(25)]
                      + [2.0 * (1 + drift * i / 14) * (1 + rng.uniform(-0.03, 0.03))
                         for i in range(15)])
            hits += bool(self._late_peaks(values))
        self.assertEqual(hits, 0)

    def test_genuine_late_rise_is_still_a_late_peak(self):
        self.assertEqual([m["t_s"] for m in self._late_peaks([0.2] * 30 + [1.0] * 10)], [60])
        self.assertEqual([m["t_s"] for m in self._late_peaks([0.2] * 37 + [1.0] * 3)], [72])
        # an early chorus at the same height does not hide the late climax
        self.assertEqual([m["t_s"] for m in self._late_peaks(
            [0.3] * 8 + [0.85] * 6 + [0.3] * 16 + [1.0] * 10)], [60])

    def test_crescendo_behaviour_is_pinned(self):
        # A crescendo has no plateau; what it reads as depends on where it
        # first clears 1.5x the median (the baseline). A linear ramp from
        # near silence clears it at 77.5% of the track: a late peak there.
        # A ramp from a floor of 1.0 to 2.0 never clears it: none. An
        # exponential ramp clears it at 65% and stays above: not late.
        self.assertEqual([m["t_s"] for m in self._late_peaks(
            [(i + 1) / 40 for i in range(40)])], [62])
        self.assertEqual(self._late_peaks([1 + i / 39 for i in range(40)]), [])
        self.assertEqual(self._late_peaks([1.08 ** i for i in range(40)]), [])

    def test_last_two_fade_windows_alone_are_not_strip_ending(self):
        values = [10] * 10 + [9] * 2 + [1, 0.1]
        meta = dict(self.meta, measured={"energy_per_bar": values,
            "energy_bar_start_s": [i * 2 for i in range(len(values))], "bar_len_s": 2.0})
        result = reference_sequence(dict(self.card, sections=[]), meta)
        self.assertNotIn("strip_ending", {m["kind"] for m in result["moves"]})

    def _thins_on_measured_fixture(self, measured_name):
        # Golden DSP tests run on the synthetic measured blocks from
        # make_synthetic.py: each carries one or two designed plateau dips
        # (first window, last window) that the detector must report, and a
        # plain monotone fade that it must not.
        measured = load_json(FIXTURES / measured_name)
        meta = {"duration_s": measured["duration_s"], "listen_verified": False,
                "measured": measured}
        result = reference_sequence(dict(self.card, sections=[]), meta)
        thins = [m for m in result["moves"] if m["kind"] == "thin_before_return"]
        return meta["measured"]["bar_len_s"], thins

    def test_longarc_golden_thin_before_return(self):
        # Designed dip: windows 181-195 of 1.6901 s, recovery at window 196.
        bar_len, thins = self._thins_on_measured_fixture("longarc_measured.json")
        self.assertEqual(len(thins), 1)
        move = thins[0]
        self.assertAlmostEqual(move["t_s"], 305.9, delta=bar_len)
        self.assertAlmostEqual(move["t_s"] + move["span_s"], 331.3, delta=bar_len)
        self.assertAlmostEqual(move["return_t_s"], 331.3, delta=bar_len)

    def test_ref_a_golden_thin_before_return(self):
        # Designed dip: windows 32-38 of 1.875 s.
        bar_len, thins = self._thins_on_measured_fixture("measured_ref_a.json")
        self.assertEqual(len(thins), 1)
        move = thins[0]
        self.assertAlmostEqual(move["t_s"], 60.0, delta=bar_len)
        self.assertAlmostEqual(move["t_s"] + move["span_s"], 73.1, delta=bar_len)

    def test_ref_c_golden_thin_before_return(self):
        # Designed dips: windows 88-95 and 124-148 of 1.5 s.
        bar_len, thins = self._thins_on_measured_fixture("measured_ref_c.json")
        self.assertEqual(len(thins), 2)
        self.assertAlmostEqual(thins[0]["t_s"], 132.0, delta=bar_len)
        self.assertAlmostEqual(thins[0]["t_s"] + thins[0]["span_s"], 144.0,
                               delta=bar_len)
        self.assertAlmostEqual(thins[1]["t_s"], 186.0, delta=bar_len)
        self.assertAlmostEqual(thins[1]["t_s"] + thins[1]["span_s"], 223.5,
                               delta=bar_len)

    def test_ref_b_golden_thin_before_return(self):
        # Designed dips: windows 18-24 and 38-44 of 3.6364 s.
        bar_len, thins = self._thins_on_measured_fixture("measured_ref_b.json")
        self.assertEqual(len(thins), 2)
        self.assertAlmostEqual(thins[0]["t_s"], 65.5, delta=bar_len)
        self.assertAlmostEqual(thins[0]["t_s"] + thins[0]["span_s"], 90.9,
                               delta=bar_len)
        self.assertAlmostEqual(thins[1]["t_s"], 138.2, delta=bar_len)
        self.assertAlmostEqual(thins[1]["t_s"] + thins[1]["span_s"], 163.6,
                               delta=bar_len)

    def test_no_measured_strip_ending_on_synthetic_refs(self):
        # Review fix (item 2): on all four synthetic refs the quiet tail is a
        # short monotone fade, not a strip ending — nothing measured may fire.
        for name in ("longarc_measured.json", "measured_ref_a.json",
                     "measured_ref_b.json", "measured_ref_c.json"):
            measured = load_json(FIXTURES / name)
            meta = {"duration_s": measured["duration_s"], "listen_verified": False,
                    "measured": measured}
            result = reference_sequence(dict(self.card, sections=[]), meta)
            self.assertNotIn("strip_ending",
                             {m["kind"] for m in result["moves"]}, name)

    def test_quiet_plateau_before_fade_is_strip_ending(self):
        # A sustained quiet plateau ahead of the final fade still counts;
        # the fade strip stops at the plateau's closing uptick, leaving
        # four quiet windows in the body.
        values = [10] * 8 + [3, 4, 3, 4, 5] + [2, 1, 0.5, 0.1, 0.0]
        meta = dict(self.meta, measured={"energy_per_bar": values,
            "energy_bar_start_s": [i * 2 for i in range(len(values))],
            "bar_len_s": 2.0})
        result = reference_sequence(dict(self.card, sections=[]), meta)
        move = next(m for m in result["moves"] if m["kind"] == "strip_ending")
        self.assertEqual(move["t_s"], 16)

    def test_final_section_layers_out_is_strip_ending(self):
        # Review fix (item 2): heard/owner layers_out on the final section
        # still counts even when the measured tail is only the final fade.
        card = dict(self.card, sections=[
            {"t": "00:00", "layers_in": ["bass", "drums"]},
            {"t": "00:16", "layers_out": ["drums"]},
        ])
        values = [10] * 8 + [0.5, 0.2, 0.05, 0.0]
        meta = dict(self.meta, listen_verified=True,
                    measured={"energy_per_bar": values,
                        "energy_bar_start_s": [i * 2 for i in range(len(values))],
                        "bar_len_s": 2.0})
        result = reference_sequence(card, meta)
        move = next(m for m in result["moves"] if m["kind"] == "strip_ending")
        self.assertEqual(move["t_s"], 16)
        self.assertEqual(move["evidence"], ["model_heard"])

    def test_final_section_strip_ending_needs_heard_or_owner_evidence(self):
        card = dict(self.card, sections=[
            {"t": "00:00", "layers_in": ["bass", "drums"]},
            {"t": "00:16", "layers_out": ["drums"]},
        ])
        values = [10] * 8 + [0.5, 0.2, 0.05, 0.0]
        meta = dict(self.meta, measured={"energy_per_bar": values,
            "energy_bar_start_s": [i * 2 for i in range(len(values))],
            "bar_len_s": 2.0})
        result = reference_sequence(card, meta)
        self.assertNotIn("strip_ending", {m["kind"] for m in result["moves"]})

    def test_owner_layers_out_on_final_section_is_strip_ending(self):
        card = load_json(FIXTURES / "longarc_v2_stale_meta" / "card.json")
        validate_card(card)
        final_t = card["sections"][-1]["t"]
        result = reference_sequence(
            card, dict(self.meta, listen_verified=False),
            {"confirmed_layer_changes": [
                {"t": final_t, "layers_out": ["drums"], "layers_in": []},
            ]})
        move = next(m for m in result["moves"] if m["kind"] == "strip_ending")
        self.assertEqual(move["evidence"], ["owner_confirmed"])

    def test_longarc_measurement_shape_does_not_crash_delayed_entry_detection(self):
        result = reference_sequence(dict(self.card, sections=[]), self.meta)
        self.assertIsInstance(result["moves"], list)

    def test_single_intro_fade_is_not_a_thin_before_return(self):
        meta = dict(self.meta, duration_s=16, measured={
            "energy_per_bar": [10, 2, 10, 10, 10, 10, 10, 10], "bar_len_s": 2.0,
        })
        result = reference_sequence(dict(self.card, sections=[]), meta)
        self.assertNotIn("thin_before_return", {move["kind"] for move in result["moves"]})

    def test_empty_confirmed_layer_change_sets_no_owner_evidence(self):
        # Review fix (minors): an aligned but empty confirmed layer change
        # ({layers_out: [], layers_in: []}) is not owner evidence.
        result = reference_sequence(self.card, self.meta, {"confirmed_layer_changes": [
            {"t": "01:20", "layers_out": [], "layers_in": []},
        ]})
        self.assertFalse(result["evidence_summary"]["owner_confirmed"])
        self.assertEqual(result["owner_confirmations"], [])

    def test_no_downbeat_warning_without_owner_downbeat(self):
        # Review fix (minors): the owner gave sections but no downbeat, so
        # there is nothing to align and no warning should fire.
        result = reference_sequence(
            self.card, self.meta, {"confirmed_sections": ["01:20"]})
        self.assertNotIn("downbeat alignment unavailable",
                         " ".join(result["warnings"]))

    def test_downbeat_warning_when_owner_downbeat_cannot_align(self):
        # The owner gave a downbeat but there is no measured beat grid, so
        # the alignment warning must still fire.
        result = reference_sequence(
            dict(self.card, sections=[]),
            dict(self.meta, measured={"energy_per_bar": [10] * 16,
                                      "bar_len_s": 2.0}),
            {"downbeat_s": 0.3, "confirmed_sections": []})
        self.assertIn("downbeat alignment unavailable",
                      " ".join(result["warnings"]))


if __name__ == "__main__":
    unittest.main()
