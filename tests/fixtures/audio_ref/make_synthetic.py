"""Generate the synthetic audio-reference fixtures in this directory.

Nothing here was measured on a recording. Each "measured" stream is a
seeded piecewise-plateau energy curve (small multiplicative noise) with a
beat grid derived from an invented tempo, in exactly the shape
``tools.audio_ref.measure_audio`` returns. Each card is invented prose that
validates against the v2 / v3 card contracts. The plateaus are designed so
that ``reference_sequence`` reports the moves the tests assert
(thin-before-return dips, no measured strip ending, a bar-aligned section),
and this script re-runs the detector on its own output to prove it.

Regenerate with::

    .venv/bin/python tests/fixtures/audio_ref/make_synthetic.py

Run from the repository root; the script imports ``tools.audio_ref``.
"""
from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from tools import audio_ref as _ar  # noqa: E402

NOISE = 0.03


def _stream(*, seed: int, bpm: float, phase_s: float, duration_s: float,
            plateaus: list[tuple[float, float]], dips: list[tuple[int, int, float]],
            fade_tail: list[float], key: str, strength: float) -> dict:
    """One measure_audio-shaped block.

    plateaus: (start_s, level) pairs, applied in order over the window grid.
    dips: (first_window, last_window, level) — overrides the plateau level on
    that inclusive window range (a designed thin-before-return).
    fade_tail: the final windows, verbatim (a monotone fade to near silence).
    """
    rng = random.Random(seed)
    period = 60.0 / bpm
    bar_len = 4 * period
    n_bars = int(duration_s / bar_len) + 1
    starts = [round(b * bar_len, 3) for b in range(n_bars)]
    levels = []
    for start in starts:
        level = plateaus[0][1]
        for at, value in plateaus:
            if start >= at:
                level = value
        levels.append(level)
    for first, last, value in dips:
        for i in range(first, last + 1):
            levels[i] = value
    energy = [round(v * (1 + rng.uniform(-NOISE, NOISE)), 6) for v in levels]
    for i, value in enumerate(fade_tail):
        energy[len(energy) - len(fade_tail) + i] = value
    beats = []
    t = phase_s
    while t < duration_s:
        beats.append(round(t, 3))
        t += period
    changes = sorted({at for at, _ in plateaus[1:]}
                     | {round(starts[first], 3) for first, _, _ in dips}
                     | {round(starts[last] + bar_len, 3) for _, last, _ in dips})
    boundaries: list[float] = []
    for change in changes:
        snapped = round(round(change / 1.5) * 1.5, 3)
        if snapped > 0 and all(abs(snapped - b) >= 12.0 for b in boundaries):
            boundaries.append(snapped)
    return {
        "tempo_bpm": round(bpm, 2),
        "beat_times_s": beats,
        "beat_period_s": round(period, 4),
        "bar_len_s": round(bar_len, 4),
        "energy_per_bar": energy,
        "energy_bar_start_s": starts,
        "novelty_boundaries_s": boundaries[:16],
        "chroma_key": {"key": key, "strength": strength},
        "duration_s": round(duration_s, 3),
    }


# ---------------------------------------------------------------- long arc
# A ten-minute instrumental arc: quiet opening, layers gathering, one
# designed dip (windows 181-195) that ends within 5 s of the 05:30 section,
# a climax, a long descent, then a short monotone fade.
LONGARC_BPM = 142.0
LONGARC_PHASE = 0.3
LONGARC_DURATION = 640.0
LONGARC_SECTIONS_S = [0, 80, 170, 250, 330, 420, 510, 580]

LONGARC = _stream(
    seed=20261014, bpm=LONGARC_BPM, phase_s=LONGARC_PHASE,
    duration_s=LONGARC_DURATION,
    plateaus=[(0, 0.004), (80, 0.012), (170, 0.020), (250, 0.036),
              (330, 0.060), (420, 0.090), (510, 0.050), (580, 0.030)],
    dips=[(181, 195, 0.018)],
    fade_tail=[0.02, 0.01, 0.004, 0.001],
    key="B minor", strength=0.21)

LONGARC_V2_CARD = {
    "contract_version": "audio-ref-card-v2",
    "identity": "Long-form instrumental in B minor at 142 BPM: a lone electric-piano figure "
                "gathers tape-saturated drums and bowed strings, then thins to one held note.",
    "tempo": {"grid_bpm": 142.0, "feel_bpm": 71.0, "confidence": "high"},
    "meter": {"beats_per_bar": 4, "half_time": False},
    "swing": {"pct": 0, "confidence": "high"},
    "density": "medium",
    "register": {"band": "mid", "pitch_span": "B1-F#6"},
    "harmony": {"modal_center": "B", "pitch_classes": ["B", "C#", "D", "E", "F#", "G", "A"],
                "interval_content": ["m3", "P4", "P5", "m7"], "harmonic_rhythm_bars": 4,
                "confidence": "high"},
    "melody": {"phrase_length_bars": 8, "contour": "arch", "call_response": "call_and_response",
               "motif_note_count": 5, "repetition": "varied"},
    "drums": {"grid": [{"instrument": "kick", "steps": [0, 6, 8]},
                       {"instrument": "snare", "steps": [4, 12]},
                       {"instrument": "ride", "steps": [0, 2, 4, 6, 8, 10, 12, 14]},
                       {"instrument": "tom", "steps": [14, 15]}],
              "confidence": "high"},
    "sections": [
        {"t": "00:00", "label": "Electric piano figure alone over a soft room tone",
         "event": "enter", "density": "sparse", "register": "mid", "harmony": "B"},
        {"t": "01:20", "label": "Brushed ride and a low tom pulse join the figure",
         "event": "texture_change", "density": "medium", "register": "low", "harmony": "B"},
        {"t": "02:50", "label": "Bowed strings enter in long tones under the piano",
         "event": "build", "density": "medium", "register": "mid", "harmony": "B"},
        {"t": "04:10", "label": "Full kit arrives; the figure doubles in octaves",
         "event": "build", "density": "dense", "register": "high", "harmony": "B"},
        {"t": "05:30", "label": "Drums return after a hollow stretch; strings swell",
         "event": "drop", "density": "dense", "register": "high", "harmony": "B"},
        {"t": "07:00", "label": "Climax with saturated drums and the figure at full width",
         "event": "drop", "density": "dense", "register": "high", "harmony": "B"},
        {"t": "08:30", "label": "Kit drops to the ride; strings thin to two voices",
         "event": "breakdown", "density": "medium", "register": "mid", "harmony": "B"},
        {"t": "09:40", "label": "One held string note and the room tone fade out",
         "event": "exit", "density": "sparse", "register": "low", "harmony": "B"},
    ],
    "imitation": {
        "do": ["Open with one figure alone and let it carry the first eighty seconds.",
               "Add layers one at a time; let the drums leave and return once before the climax.",
               "Keep the harmony circling B minor with the fourth and seventh as colour.",
               "End by subtracting to a single sustained note rather than a cadence."],
        "dont": ["Do not introduce the full kit before the strings have entered.",
                 "Do not modulate away from B minor.",
                 "Do not fade the ending in under twenty seconds."],
    },
    "uncertain": ["Whether the low tom pulse at 01:20 is played or sampled."],
}

LONGARC_V3_CARD = {
    "contract_version": "audio-ref-card-v3",
    "identity": LONGARC_V2_CARD["identity"],
    "tempo": {"grid_bpm": 142.0, "feel_bpm": 71.0, "confidence": "high", "evidence_t": None},
    "meter": {"beats_per_bar": 4, "half_time": False},
    "swing": {"pct": 0, "confidence": "high", "evidence_t": None},
    "density": "medium",
    "register": {"band": "mid", "pitch_span": "B1-F#6"},
    "harmony": {"modal_center": "B", "pitch_classes": ["B", "C#", "D", "E", "F#", "G", "A"],
                "confidence": "high", "evidence_t": None},
    "melody": {"phrase_length_bars": 8, "repetition": "varied"},
    "drums": dict(LONGARC_V2_CARD["drums"], evidence_t=None),
    "sections": [
        {"t": "00:00", "label": "Electric piano figure alone over a soft room tone",
         "energy": 1, "layers_in": [], "layers_out": [], "transition_in": None},
        {"t": "01:20", "label": "Brushed ride and a low tom pulse join the figure",
         "energy": 2, "layers_in": [], "layers_out": [], "transition_in": None},
    ],
    "layers": [],
    "low_end": {"kick_bass_relation": None, "sidechain_audible": None},
    "space": None,
    "the_trick": None,
    "imitation": {
        "do": [{"rule": "Open with one figure alone and let it carry the first eighty seconds.",
                "t": None, "ableton_move": None, "why": None},
               {"rule": "Keep the harmony circling B minor with the fourth and seventh as colour.",
                "t": None, "ableton_move": None, "why": None}],
        "dont": [{"rule": "Do not introduce the full kit before the strings have entered.",
                  "t": None, "ableton_move": None, "why": None},
                 {"rule": "Do not fade the ending in under twenty seconds.",
                  "t": None, "ableton_move": None, "why": None}],
    },
    "uncertain": [],
}

LONGARC_STALE_META = {
    "contract_version": "audio-ref-card-v3",
    "source": "https://example.invalid/watch?v=synthetic-longarc",
    "source_kind": "youtube",
    "title": "Longarc",
    "structure": {"feel_bpm": 71.0, "grid_bpm": 142.0, "density": "medium"},
}

# ---------------------------------------------------------------- three short refs
# Invented companions with one or two designed dips each and a plain fade.
REF_A = _stream(  # brisk, one dip at windows 32-38
    seed=11, bpm=128.0, phase_s=0.2, duration_s=210.0,
    plateaus=[(0, 0.010), (30, 0.030), (120, 0.045), (180, 0.030)],
    dips=[(32, 38, 0.015)],
    fade_tail=[0.02, 0.008, 0.002], key="E minor", strength=0.17)

REF_B = _stream(  # slow, two dips at windows 18-24 and 38-44
    seed=22, bpm=66.0, phase_s=0.45, duration_s=220.0,
    plateaus=[(0, 0.040), (160, 0.055), (200, 0.030)],
    dips=[(18, 24, 0.020), (38, 44, 0.022)],
    fade_tail=[0.015, 0.006, 0.001], key="G major", strength=0.12)

REF_C = _stream(  # fast, two dips at windows 88-95 and 124-148
    seed=33, bpm=160.0, phase_s=0.1, duration_s=300.0,
    plateaus=[(0, 0.070), (60, 0.080), (240, 0.060)],
    dips=[(88, 95, 0.035), (124, 148, 0.040)],
    fade_tail=[0.03, 0.012, 0.003], key="F# minor", strength=0.19)


def _v2_card(identity, grid_bpm, span, centre, pcs, intervals, grid, sections,
             do, dont, uncertain):
    return {
        "contract_version": "audio-ref-card-v2",
        "identity": identity,
        "tempo": {"grid_bpm": grid_bpm, "feel_bpm": grid_bpm, "confidence": "high"},
        "meter": {"beats_per_bar": 4, "half_time": False},
        "swing": {"pct": 0, "confidence": "high"},
        "density": "medium",
        "register": {"band": "mid", "pitch_span": span},
        "harmony": {"modal_center": centre, "pitch_classes": pcs,
                    "interval_content": intervals, "harmonic_rhythm_bars": 4,
                    "confidence": "high"},
        "melody": {"phrase_length_bars": 4, "contour": "arch",
                   "call_response": "call_and_response", "motif_note_count": 4,
                   "repetition": "varied"},
        "drums": {"grid": grid, "confidence": "high"},
        "sections": sections,
        "imitation": {"do": do, "dont": dont},
        "uncertain": uncertain,
    }


def _sec(t, label, event, density, register, harmony):
    return {"t": t, "label": label, "event": event, "density": density,
            "register": register, "harmony": harmony}


CARD_A = _v2_card(
    "Brisk synth-pop instrumental at 128 BPM: a plucked bass ostinato under a two-bar "
    "lead hook, one hollow bridge, then the hook returns an octave up.",
    128.0, "E1-B5", "E", ["E", "F#", "G", "A", "B", "D"], ["M2", "m3", "P4", "P5"],
    [{"instrument": "kick", "steps": [0, 4, 8, 12]},
     {"instrument": "clap", "steps": [4, 12]},
     {"instrument": "hat", "steps": [2, 6, 10, 14]}],
    [_sec("00:00", "Plucked bass ostinato alone with a filtered pad", "enter", "sparse", "low", "E"),
     _sec("00:30", "Kick and clap enter with the two-bar lead hook", "drop", "medium", "mid", "E"),
     _sec("01:00", "Hollow bridge: hook drops out, pad and hats carry", "breakdown", "sparse", "mid", "E"),
     _sec("01:13", "Hook returns with a counter-line in sixths", "drop", "medium", "mid", "E"),
     _sec("02:00", "Hook an octave up over doubled bass", "build", "dense", "high", "E"),
     _sec("03:00", "Pad and bass only, hats fade", "exit", "sparse", "low", "E")],
    ["Hold the bass ostinato for the whole track; vary only the hook above it.",
     "Make the bridge a subtraction of the hook, not a new part.",
     "Bring the hook back an octave up for the last section."],
    ["Do not add a second chord progression.", "Do not swing the hats."],
    ["Whether the counter-line at 01:13 is a second synth or a delayed copy of the hook."])

CARD_B = _v2_card(
    "Slow piano ballad at 66 BPM with brushed drums: verse and chorus share one chord "
    "cycle; two quiet breaks strip to piano and voice before each return.",
    66.0, "D2-A5", "D", ["D", "E", "F#", "G", "A", "B"], ["M2", "M3", "P4", "P5"],
    [{"instrument": "kick", "steps": [0, 8]},
     {"instrument": "snare", "steps": [4, 12]},
     {"instrument": "ride", "steps": [0, 2, 4, 6, 8, 10, 12, 14]}],
    [_sec("00:00", "Piano chord cycle with brushed ride", "enter", "sparse", "mid", "D"),
     _sec("00:36", "Voice enters over the cycle, kick and snare join", "build", "medium", "mid", "D"),
     _sec("01:05", "First break: piano and voice only", "breakdown", "sparse", "mid", "D"),
     _sec("01:35", "Chorus returns with strings pad", "drop", "medium", "high", "D"),
     _sec("02:20", "Second break, lower voice register", "breakdown", "sparse", "low", "D"),
     _sec("02:48", "Final chorus, then piano alone", "drop", "medium", "mid", "D"),
     _sec("03:20", "Piano cycle fades under room noise", "exit", "sparse", "mid", "D")],
    ["Keep one chord cycle for verse and chorus; change only the layers.",
     "Strip to piano and voice before each return.",
     "Let the final chorus be followed by the opening texture, thinner."],
    ["Do not change the tempo at the breaks.", "Do not add drum fills into the returns."],
    ["Whether the strings pad is real strings or a sampled layer."])

CARD_C = _v2_card(
    "Fast drum-and-bass sketch at 160 BPM: a two-note sub line, a chopped break, and "
    "two half-length breakdowns where the break drops to hats alone.",
    160.0, "F#1-C#5", "F#", ["F#", "A", "B", "C#", "E"], ["m3", "P4", "P5", "m7"],
    [{"instrument": "kick", "steps": [0, 10]},
     {"instrument": "snare", "steps": [4, 12]},
     {"instrument": "hat", "steps": [0, 2, 4, 6, 8, 10, 12, 14]},
     {"instrument": "perc", "steps": [7, 15]}],
    [_sec("00:00", "Hats and a filtered sub line", "enter", "sparse", "low", "F#"),
     _sec("01:00", "Chopped break drops in at full density", "drop", "dense", "mid", "F#"),
     _sec("02:12", "First breakdown: break out, hats and sub only", "breakdown", "sparse", "low", "F#"),
     _sec("02:24", "Break returns with a pitched stab", "drop", "dense", "mid", "F#"),
     _sec("03:06", "Second, longer breakdown with a rising pad", "breakdown", "medium", "mid", "F#"),
     _sec("03:43", "Break returns, stab doubled", "drop", "dense", "high", "F#"),
     _sec("04:00", "Break thins to hats, sub holds, fade", "exit", "sparse", "low", "F#")],
    ["Keep the sub to two notes; let the break carry the energy.",
     "Use two breakdowns of different length; the second one longer.",
     "Return from each breakdown on a downbeat with the full break."],
    ["Do not add melodic content above the stab.", "Do not half-time the break."],
    ["Whether the stab is a synth or a resampled vocal."])


def _designed_thins(measured):
    result = _ar.reference_sequence({"sections": []}, {
        "duration_s": measured["duration_s"], "listen_verified": False,
        "measured": measured})
    kinds = {m["kind"] for m in result["moves"]}
    thins = [(round(m["t_s"], 1), round(m["t_s"] + m["span_s"], 1),
              round(m["return_t_s"], 1))
             for m in result["moves"] if m["kind"] == "thin_before_return"]
    return thins, kinds


def main() -> None:
    _ar.validate_card(LONGARC_V2_CARD)
    _ar.validate_card_v3(LONGARC_V3_CARD)
    for card in (CARD_A, CARD_B, CARD_C):
        _ar.validate_card(card)

    out = {
        "longarc_measured.json": LONGARC,
        "longarc_v3.json": LONGARC_V3_CARD,
        "longarc_v2_stale_meta/card.json": LONGARC_V2_CARD,
        "longarc_v2_stale_meta/meta.json": LONGARC_STALE_META,
        "measured_ref_a.json": REF_A, "card_ref_a.json": CARD_A,
        "measured_ref_b.json": REF_B, "card_ref_b.json": CARD_B,
        "measured_ref_c.json": REF_C, "card_ref_c.json": CARD_C,
    }
    for name, data in out.items():
        path = HERE / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n",
                        encoding="utf-8")

    # Self-check: the designed moves are what the detector reports.
    report = {}
    for name, measured in (("longarc", LONGARC), ("ref_a", REF_A),
                           ("ref_b", REF_B), ("ref_c", REF_C)):
        thins, kinds = _designed_thins(measured)
        assert "strip_ending" not in kinds, (name, kinds)
        report[name] = {"thins": thins, "kinds": sorted(kinds),
                        "bar_len_s": measured["bar_len_s"],
                        "windows": len(measured["energy_per_bar"])}
    assert len(report["longarc"]["thins"]) == 1
    assert len(report["ref_a"]["thins"]) == 1
    assert len(report["ref_b"]["thins"]) == 2
    assert len(report["ref_c"]["thins"]) == 2
    longarc_dip_end = report["longarc"]["thins"][0][1]
    assert abs(longarc_dip_end - 330.0) <= 5.0, longarc_dip_end
    seq = _ar.reference_sequence(LONGARC_V3_CARD, {
        "ref_id": "longarc", "duration_s": LONGARC_DURATION,
        "listen_verified": False, "measured": LONGARC},
        {"downbeat_s": LONGARC_PHASE, "confirmed_sections": ["01:20"]})
    report["longarc"]["ref_bar_01_20"] = seq["sections"][1]["ref_bar"]
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
