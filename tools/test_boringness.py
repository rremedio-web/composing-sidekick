"""Synthetic selftests for the boringness scorer (no corpus dependency).

Run: .venv/bin/python tools/test_boringness.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from boringness import (
    phrase_to_degrees,
    surprise_sequence,
    mean_surprise,
    is_mechanical,
    alive_score,
    mechanical_reason,
    stratified_sample,
    auc_separation,
    resolve_score,
    pairwise_agreement,
    elo_ratings,
)

# Hand grammar: 0 = home (probable), 6 = rare tension, 6 -> 0 strongly resolves.
RES_GRAMMAR = {
    "unigram": {"0": 0.6, "7": 0.25, "6": 0.05, "4": 0.1},
    "order_1": {
        "0": {"6": 0.1, "0": 0.5, "7": 0.4},
        "6": {"0": 0.85, "7": 0.15},
        "7": {"0": 0.8, "7": 0.2},
    },
    "order_2": {},
}

# A major scale in chroma-offset degrees (relative to tonic).
MAJOR = {0, 2, 4, 5, 7, 9, 11}


def _note(pitch, start, dur=0.25, vel=80):
    return {"pitch": pitch, "start": start, "duration": dur, "velocity": vel}


# A tiny hand-built grammar: home degree 0 is sticky (P(0|0)=0.9),
# leaping out to 7 is rare (P=0.1). Nothing here touches the real corpus.
GRAMMAR = {
    "unigram": {"0": 0.7, "7": 0.2, "4": 0.1},
    "order_1": {
        "0": {"0": 0.9, "7": 0.1},
        "7": {"0": 0.9, "7": 0.1},
    },
    "order_2": {},
}


def test_phrase_to_degrees_skyline_and_chroma():
    # C E G ascending, tonic C(0) -> degrees 0, 4, 7
    notes = [_note(60, 0.0), _note(64, 0.5), _note(67, 1.0)]
    assert phrase_to_degrees(notes, tonic_pc=0) == [0, 4, 7]


def test_skyline_picks_top_note_per_onset():
    # chord at onset 0 (C + G) -> skyline keeps G(7); then E(4)
    notes = [_note(60, 0.0), _note(67, 0.0), _note(64, 0.5)]
    assert phrase_to_degrees(notes, tonic_pc=0) == [7, 4]


def test_degrees_respect_tonic_and_wrap():
    # tonic D(2): D F# A -> 0, 4, 7
    notes = [_note(62, 0.0), _note(66, 0.5), _note(69, 1.0)]
    assert phrase_to_degrees(notes, tonic_pc=2) == [0, 4, 7]


def test_first_note_surprise_is_unigram():
    seq = surprise_sequence([7], GRAMMAR)
    assert len(seq) == 1
    assert abs(seq[0] - (-math.log2(0.2))) < 1e-9


def test_transition_surprise_is_neg_log2_of_order1():
    # [0, 0]: opening unigram P(0)=0.7, then P(0|0)=0.9
    seq = surprise_sequence([0, 0], GRAMMAR)
    assert len(seq) == 2
    assert abs(seq[0] - (-math.log2(0.7))) < 1e-9
    assert abs(seq[1] - (-math.log2(0.9))) < 1e-9


def test_backoff_to_unigram_when_transition_unseen():
    # 4 -> 4: order_1 has no "4" key, so back off to unigram P(4)=0.1
    seq = surprise_sequence([4, 4], GRAMMAR)
    assert abs(seq[1] - (-math.log2(0.1))) < 1e-9


def test_unseen_degree_uses_floor_not_crash():
    # degree 3 appears nowhere -> floor probability, finite surprise
    seq = surprise_sequence([3], GRAMMAR, floor=1e-4)
    assert abs(seq[0] - (-math.log2(1e-4))) < 1e-9


def test_dull_phrase_is_less_surprising_than_lively():
    # The whole point: averaged/sticky phrase scores low surprise,
    # the one that dares to leap-and-return scores high.
    dull = [0, 0, 0, 0]
    lively = [0, 7, 0, 7]
    assert mean_surprise(lively, GRAMMAR) > mean_surprise(dull, GRAMMAR)


# --- junk-gate: is_mechanical ---

def test_mono_roll_is_mechanical():
    # 30 notes, one degree -> the example|c8|w104 case
    assert is_mechanical([0] * 30, GRAMMAR) is True


def test_sticky_single_degree_is_mechanical():
    assert is_mechanical([0, 0, 0, 0], GRAMMAR) is True


def test_predictable_oscillation_is_mechanical():
    # two distinct degrees but every transition is near-certain -> a trill
    trill = {
        "unigram": {"0": 0.5, "5": 0.5},
        "order_1": {"0": {"5": 0.95, "0": 0.05}, "5": {"0": 0.95, "5": 0.05}},
        "order_2": {},
    }
    assert is_mechanical([0, 5, 0, 5, 0], trill) is True


def test_varied_in_key_phrase_is_not_mechanical():
    # 3 distinct degrees, real surprise -> a lead, not a roll
    assert is_mechanical([0, 7, 4, 7, 0, 4], GRAMMAR) is False


# --- in-key ranker: alive_score ---

def test_alive_equals_mean_surprise_when_all_in_scale():
    degs = [0, 7, 4]  # all diatonic to MAJOR
    assert abs(alive_score(degs, GRAMMAR, MAJOR) - mean_surprise(degs, GRAMMAR)) < 1e-9


def test_alive_zeros_out_of_scale_surprise():
    degs = [0, 1, 7]  # degree 1 is chromatic -> its surprise must not count
    sur = surprise_sequence(degs, GRAMMAR)
    expected = (sur[0] + 0.0 + sur[2]) / 3
    assert abs(alive_score(degs, GRAMMAR, MAJOR) - expected) < 1e-9


def test_diatonic_leaps_beat_chromatic_flailing():
    # Same length; chromatic phrase's surprise is mostly out-of-scale noise.
    diatonic = [0, 7, 4, 7, 0]   # all in MAJOR, leaping
    chromatic = [0, 1, 6, 10, 1]  # mostly out of MAJOR
    assert alive_score(diatonic, GRAMMAR, MAJOR) > alive_score(chromatic, GRAMMAR, MAJOR)


# --- quarantine: mechanical_reason ---

def test_reason_mono_pitch():
    assert mechanical_reason([0] * 30, GRAMMAR) == "mono_pitch"


def test_reason_predictable():
    trill = {
        "unigram": {"0": 0.5, "5": 0.5},
        "order_1": {"0": {"5": 0.95, "0": 0.05}, "5": {"0": 0.95, "5": 0.05}},
        "order_2": {},
    }
    assert mechanical_reason([0, 5, 0, 5, 0], trill) == "predictable"


def test_reason_none_for_real_lead():
    assert mechanical_reason([0, 7, 4, 7, 0, 4], GRAMMAR) is None


# --- blind audition sampler: stratified_sample ---

def test_stratified_sample_spans_range_and_count():
    scored = [(float(i), "item%d" % i) for i in range(10)]
    picked = stratified_sample(scored, 5)
    scores = [s for s, _ in picked]
    assert len(picked) == 5
    assert scores[0] == 0.0           # includes the low end
    assert scores[-1] == 9.0          # includes the high end
    assert scores == sorted(scores)   # ordered low -> high


def test_stratified_sample_k_ge_n_returns_all_sorted():
    scored = [(3.0, "a"), (1.0, "b"), (2.0, "c")]
    picked = stratified_sample(scored, 10)
    assert [s for s, _ in picked] == [1.0, 2.0, 3.0]


# --- ranker validation: auc_separation ---

def test_auc_perfect_separation():
    assert auc_separation([3.0, 4.0], [1.0, 2.0]) == 1.0


def test_auc_inverted_separation():
    assert auc_separation([1.0, 2.0], [3.0, 4.0]) == 0.0


def test_auc_ties_are_half():
    assert auc_separation([2.0], [2.0]) == 0.5


def test_auc_mixed():
    # pairs: 3>2 (1), 1>2 (0) -> 0.5
    assert auc_separation([3.0, 1.0], [2.0]) == 0.5


# --- resolve_score: surprise that resolves (tension -> release) ---

def test_resolve_blue_note_beats_chromatic_wander():
    # [0,6,0] returns home after the tension; [0,6,7] does not resolve as hard
    assert resolve_score([0, 6, 0], RES_GRAMMAR) > resolve_score([0, 6, 7], RES_GRAMMAR)


def test_resolve_dead_phrase_is_zero():
    assert resolve_score([0, 0, 0], RES_GRAMMAR) == 0.0


def test_resolve_blue_note_beats_dead():
    assert resolve_score([0, 6, 0], RES_GRAMMAR) > resolve_score([0, 0, 0], RES_GRAMMAR)


def test_resolve_numeric_lock():
    # tension at note 6 = -log2(0.1)=3.3219; release into 0 = -log2(0.85)=0.2345
    # drop = 3.0875, averaged over 3 notes = 1.0292
    assert abs(resolve_score([0, 6, 0], RES_GRAMMAR) - 1.0292) < 1e-3


def test_resolve_degenerate_single_note_is_zero():
    assert resolve_score([0], RES_GRAMMAR) == 0.0


# --- pairwise_agreement: does the scorer rank the winner above the loser? ---

def test_agreement_all_correct():
    assert pairwise_agreement([(3.0, 1.0), (4.0, 2.0)]) == 1.0


def test_agreement_all_wrong():
    assert pairwise_agreement([(1.0, 3.0)]) == 0.0


def test_agreement_tie_is_half():
    assert pairwise_agreement([(2.0, 2.0)]) == 0.5


# --- elo_ratings: global ranking from pairwise results ---

def test_elo_winner_outranks_loser():
    elo = elo_ratings([("a", "b"), ("a", "b")])
    assert elo["a"] > elo["b"]


def test_elo_is_zero_sum():
    elo = elo_ratings([("a", "b"), ("b", "c"), ("c", "a")])
    assert abs(sum(elo.values()) - 3 * 1000.0) < 1e-6


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERR  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
