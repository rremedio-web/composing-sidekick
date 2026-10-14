"""Boringness scorer: the Markov model, read backwards.

The generator picks the *most probable* next note and that is exactly why its
leads sound average-correct but dead. This scores the opposite quantity:

    surprise(note) = -log2 P(degree | history)

Low mean surprise = predictable = boring (the center of the distribution, where
melodies go to die). High surprise that stays in key = alive.

This is deliberately standalone. It reads the same `melodic_grammar` the
generator uses, but it does NOT touch `score_phrase` / the main taste score.
It is a diagnostic for the ear to validate, not a signal to fold in yet.

Grammar shape (from store/style_profile.json -> aggregate.melodic_grammar):
    {
      "unigram": {"<deg>": prob, ...},
      "order_1": {"<prev>": {"<deg>": prob, ...}, ...},
      "order_2": {"<prev2>,<prev>": {"<deg>": prob, ...}, ...},
    }
Degrees are chroma offsets from the tonic (0-11).
"""
import math


def phrase_to_degrees(notes, tonic_pc):
    """Reduce a phrase to its skyline degree sequence.

    notes: list of {"pitch", "start", ...} dicts.
    tonic_pc: pitch class (0-11) of the tonic.
    Returns the top pitch per onset, converted to chroma offset, ordered by time.
    """
    top_by_onset = {}
    for n in notes:
        start = n["start"]
        if start not in top_by_onset or n["pitch"] > top_by_onset[start]:
            top_by_onset[start] = n["pitch"]
    return [(top_by_onset[s] - tonic_pc) % 12 for s in sorted(top_by_onset)]


def _prob(degrees, i, grammar, floor):
    """P(degrees[i] | history) with trigram -> bigram -> unigram backoff."""
    curr = str(degrees[i])
    unigram = grammar.get("unigram", {})

    if i >= 2:
        key = "%d,%d" % (degrees[i - 2], degrees[i - 1])
        dist = grammar.get("order_2", {}).get(key)
        if dist and curr in dist:
            return dist[curr]
    if i >= 1:
        dist = grammar.get("order_1", {}).get(str(degrees[i - 1]))
        if dist and curr in dist:
            return dist[curr]
    return unigram.get(curr, floor)


def surprise_sequence(degrees, grammar, floor=1e-4):
    """Per-note surprise in bits: -log2 P(degree | history).

    The opening note is scored by the unigram. Each subsequent note backs off
    trigram -> bigram -> unigram, and unseen degrees get `floor` probability so
    surprise stays finite instead of crashing on log2(0).
    """
    out = []
    for i in range(len(degrees)):
        p = _prob(degrees, i, grammar, floor)
        if p <= 0.0:
            p = floor
        out.append(-math.log2(p))
    return out


def mean_surprise(degrees, grammar, floor=1e-4):
    """Average surprise in bits. Low = boring, high = alive (if in key)."""
    seq = surprise_sequence(degrees, grammar, floor)
    return sum(seq) / len(seq) if seq else 0.0


def has_small_period(degrees, max_period=2):
    """True when the skyline repeats exactly with period <= max_period —
    a stuck trill/oscillation the surprise model can miss (an alternating
    pair the grammar saw often is 'probable', not alive)."""
    n = len(degrees)
    for period in range(1, max_period + 1):
        if all(degrees[i] == degrees[i % period] for i in range(n)):
            return True
    return False


def is_mechanical(degrees, grammar, min_bits=1.2, min_distinct=2, floor=1e-4,
                  min_period_onsets=8, max_period=2, period_rule=True):
    """Junk-gate: True for rolls/trills the grammar is never surprised by.

    Three ways to be mechanical:
      - too few distinct skyline degrees (mono-pitch rolls), or
      - an exact period-<=2 skyline over >= min_period_onsets onsets, or
      - uniformly low surprise (predictable oscillations / trills).
    This is a quarantine filter for fake leads, NOT a quality score.
    ``period_rule=False`` restores the pre-period base gate (the
    JUNK_FLOOR_V2-off behaviour the bank serves until ears validate it).
    """
    if len(set(degrees)) < min_distinct:
        return True
    if (period_rule and len(degrees) >= min_period_onsets
            and has_small_period(degrees, max_period)):
        return True
    return mean_surprise(degrees, grammar, floor) < min_bits


def mechanical_reason(degrees, grammar, min_bits=1.2, min_distinct=2, floor=1e-4,
                      min_period_onsets=8, max_period=2):
    """Why a phrase is mechanical, or None. Powers the quarantine manifest.

    'mono_pitch'  -> too few distinct skyline degrees (a roll)
    'periodic'    -> exact period-<=2 skyline over >= 8 onsets (a stuck trill)
    'predictable' -> uniformly low surprise (a trill / oscillation)
    """
    if len(set(degrees)) < min_distinct:
        return "mono_pitch"
    if len(degrees) >= min_period_onsets and has_small_period(degrees, max_period):
        return "periodic"
    if mean_surprise(degrees, grammar, floor) < min_bits:
        return "predictable"
    return None


def stratified_sample(scored, k):
    """Pick k items spread evenly across the score range (sorted low -> high).

    scored: list of (score, item). Returns up to k (score, item) pairs spanning
    the min and max so an audition covers the whole spectrum, not one clump.
    """
    ordered = sorted(scored, key=lambda t: t[0])
    n = len(ordered)
    if k >= n:
        return ordered
    idx = sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})
    return [ordered[i] for i in idx]


def auc_separation(gold_scores, meh_scores):
    """P(a random gold scores above a random meh). 1.0 = perfect, 0.5 = chance.

    Mann-Whitney style: ties count as half. Returns 0.5 if either side is empty.
    """
    if not gold_scores or not meh_scores:
        return 0.5
    wins = 0.0
    for g in gold_scores:
        for m in meh_scores:
            if g > m:
                wins += 1.0
            elif g == m:
                wins += 0.5
    return wins / (len(gold_scores) * len(meh_scores))


def resolve_score(degrees, grammar, tension_min=2.0, floor=1e-4):
    """Surprise that RESOLVES: tension (a surprising note) then release (a drop).

    Aliveness as a derivative, not a value. For each note that is genuinely
    surprising (>= tension_min bits), credit how much the surprise DROPS into
    the next note -- a return toward the probable/home. Returning to the tonic
    yields the biggest drop (home is the most probable note), so this captures
    "lands somewhere stable" without hardcoding scale tones. Unlike alive_score
    it does not gate on key, so it keeps the blue-note-that-resolves.
    """
    s = surprise_sequence(degrees, grammar, floor)
    if len(s) < 2:
        return 0.0
    total = 0.0
    for i in range(1, len(s)):
        if s[i - 1] >= tension_min and s[i - 1] > s[i]:
            total += s[i - 1] - s[i]
    return total / len(s)


def pairwise_agreement(paired):
    """Fraction of pairs where the winner outscores the loser (ties = 0.5).

    paired: list of (winner_score, loser_score). This is the core ranker test:
    for each A/B judgment the ear made, did the scorer rank the chosen one higher?
    """
    if not paired:
        return 0.5
    agree = 0.0
    for w, l in paired:
        if w > l:
            agree += 1.0
        elif w == l:
            agree += 0.5
    return agree / len(paired)


def elo_ratings(results, k=32.0, base=1000.0):
    """Elo ratings from pairwise results. results: list of (winner, loser) ids.

    Zero-sum updates, every player seeded at `base`. Gives a global ranking for
    display; pairwise_agreement is what actually decides the hypothesis.
    """
    ratings = {}
    for w, l in results:
        ratings.setdefault(w, base)
        ratings.setdefault(l, base)
    for w, l in results:
        exp_w = 1.0 / (1.0 + 10.0 ** ((ratings[l] - ratings[w]) / 400.0))
        delta = k * (1.0 - exp_w)
        ratings[w] += delta
        ratings[l] -= delta
    return ratings


def alive_score(degrees, grammar, scale_pcs, floor=1e-4):
    """Surprise that stays in key: mean surprise counting only in-scale notes.

    Out-of-scale degrees contribute 0, so the score is dragged down by BOTH
    boredom (low in-scale surprise) and noise (chromatic flailing leaves few
    in-scale notes). scale_pcs is the set of diatonic chroma offsets (relative
    to the tonic). Blue notes are treated as noise here — a known limitation.
    """
    if not degrees:
        return 0.0
    surprises = surprise_sequence(degrees, grammar, floor)
    in_key = sum(s for d, s in zip(degrees, surprises) if d in scale_pcs)
    return in_key / len(degrees)
