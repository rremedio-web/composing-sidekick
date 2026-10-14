"""Pure FEEL features: how a performance sits in time and touch — never what it plays.

The comping falsification (2026-07-02) proved take-lane winners and losers are NOT
separable by melodic features: takes of the same part share their notes and differ in
performance. These 9 features measure exactly that difference — micro-timing (1-3),
dynamics (4-7), articulation (8-9). None of them read pitch, intervals, scale, or the
onset-slot histogram.

Isolation contract (mirrors taste.py): this module NEVER imports core, and nothing on
the scoring path (score_phrase / _taste_features / humanize) imports it. Feel features
cannot steer anything until their gate passes AND Ricardo approves a wiring batch.

Grid semantics copied from analyze/style_profile._dna_accumulate: deviation is measured
against the GLOBAL 16th grid (arrangement positions are grid-absolute, so no start
shifting — shifting by whole bars is a no-op by construction), clamped to ±0.125 beats;
off-grid means |dev| > 1e-6.
"""
from __future__ import annotations

FEEL_FEATURE_NAMES = [
    "offgrid_frac",      # where his hands land vs the grid
    "timing_dev_std",    # looseness amount
    "timing_dev_mean",   # push (ahead) vs drag (behind)
    "vel_mean",          # touch weight               (/127)
    "vel_std",           # dynamic expressiveness     (/127)
    "vel_range",         # accent spread              (/127)
    "vel_contour_corr",  # accents the meter like him? (Pearson vs role slot_mean)
    "legato_frac",       # overlapping consecutive onsets — articulation
    "duration_cv",       # articulation variance (std/mean of durations)
]


def _start(n: dict) -> float:
    return float(n.get("start", n.get("start_beats", 0.0)))


def _duration(n: dict) -> float:
    return float(n.get("duration", n.get("duration_beats", 0.0)))


def _grid_dev(start: float) -> float:
    """Signed deviation to the nearest 16th, clamped ±0.125 (style_profile convention)."""
    dev = start - round(start * 4) / 4.0
    return max(-0.125, min(0.125, dev))


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5


def _pearson(a: list[float], b: list[float]) -> float:
    """Pearson r; 0.0 for <3 points or zero variance on either side."""
    if len(a) != len(b) or len(a) < 3:
        return 0.0
    ma, mb = _mean(a), _mean(b)
    da = [x - ma for x in a]
    db = [x - mb for x in b]
    va = sum(x * x for x in da)
    vb = sum(x * x for x in db)
    if va <= 1e-12 or vb <= 1e-12:
        return 0.0
    return sum(x * y for x, y in zip(da, db)) / ((va * vb) ** 0.5)


def feel_features(notes: list[dict], slot_mean: list[float] | None = None) -> list[float]:
    """9 feel features for a note list ({pitch,start|start_beats,duration|…,velocity}).

    slot_mean: the role's 16-slot velocity contour from the style profile, passed IN by
    the caller (feel.py never reads artifacts). Fewer than 3 notes → all zeros.
    Deterministic; no rng."""
    if not notes or len(notes) < 3:
        return [0.0] * len(FEEL_FEATURE_NAMES)
    ordered = sorted(notes, key=lambda n: (_start(n), n.get("pitch", 0)))
    starts = [_start(n) for n in ordered]
    vels = [max(0.0, min(127.0, float(n.get("velocity", 0)))) for n in ordered]
    durs = [max(0.0, _duration(n)) for n in ordered]

    devs = [_grid_dev(s) for s in starts]
    offgrid = sum(1 for d in devs if abs(d) > 1e-6) / len(devs)

    vel_mean = _mean(vels)
    vel_std = _std(vels)
    vel_range = (max(vels) - min(vels)) if vels else 0.0

    contour = 0.0
    if slot_mean and len(slot_mean) >= 16:
        by_slot: dict[int, list[float]] = {}
        for s, v in zip(starts, vels):
            slot = int(round((s % 4.0) * 4)) % 16
            by_slot.setdefault(slot, []).append(v)
        slots = sorted(by_slot)
        if len(slots) >= 3:
            contour = _pearson([_mean(by_slot[k]) for k in slots],
                               [float(slot_mean[k]) for k in slots])

    overlaps = total_pairs = 0
    for i in range(len(ordered) - 1):
        s_next = starts[i + 1]
        if s_next <= starts[i] + 1e-9:
            continue                      # chord tones share an onset — not an articulation pair
        total_pairs += 1
        if starts[i] + durs[i] > s_next + 1e-9:
            overlaps += 1
    legato = overlaps / total_pairs if total_pairs else 0.0

    dur_mean = _mean(durs)
    dur_cv = (_std(durs) / dur_mean) if dur_mean > 1e-9 else 0.0

    return [
        round(offgrid, 6),
        round(_std(devs), 6),
        round(_mean(devs), 6),
        round(vel_mean / 127.0, 6),
        round(vel_std / 127.0, 6),
        round(vel_range / 127.0, 6),
        round(contour, 6),
        round(legato, 6),
        round(dur_cv, 6),
    ]


def density_feature(notes: list[dict]) -> float:
    """Notes-per-beat over the phrase span — NOT one of the core 9 (the 30
    reduction-winners make density a confound); used only by the validator's
    ablation arm."""
    if not notes or len(notes) < 2:
        return 0.0
    starts = [_start(n) for n in notes]
    span = max(starts) - min(starts)
    return round(len(notes) / span, 6) if span > 1e-9 else 0.0
