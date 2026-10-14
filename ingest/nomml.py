"""Note-Onset Median Metric Level — the played-vs-programmed GATE.

After GigaMIDI (TISMIR 2025): each onset gets the COARSEST metric grid it sits
on (binary bar .. 32nd + triplet 2/3 .. 1/6 beat, within epsilon); onsets
matching no grid are OFF-GRID (the finest level). NOMML = median onset level.
Mouse-drawn or quantized material medians onto a musical grid; real playing —
his lead DNA is ~56% off-grid at ~25ms — medians off-grid. The 32nd (0.125
beats) is deliberately the finest grid treated as programmable: a 64th "grid"
is ±31ms at 120bpm, which would swallow exactly the feel this gate exists to
find. Median onsets finer than a 32nd ARE the feel.

`performed` = the median onset is OFF-GRID (median_high: exactly-half off-grid
counts as performed). Deliberately strict: a quantized performance reads as
"programmed" because it carries no timing feel worth harvesting — this is a
GATE for the groove-DNA diet and candidate mining, never a ranker, and it
enters no score (the project's metrics-are-gates rule).

Pure functions over note dicts ({start|start_beats, ...}); no I/O, no core.
"""
import statistics

# Coarse -> fine; binary halvings down to the 32nd + triplet subdivisions, in beats.
GRIDS = sorted((4.0, 2.0, 1.0, 0.5, 0.25, 0.125,
                2.0 / 3.0, 1.0 / 3.0, 1.0 / 6.0), reverse=True)
OFF_GRID = len(GRIDS)
# 960-ppq file rounding is ~0.001 beats; human feel lives at 0.02-0.1 beats.
EPSILON_BEATS = 0.005


def onset_level(start_beats, epsilon=EPSILON_BEATS):
    """Index into GRIDS of the coarsest grid containing this onset, else OFF_GRID."""
    for lvl, g in enumerate(GRIDS):
        d = abs(start_beats / g - round(start_beats / g)) * g
        if d <= epsilon + 1e-12:
            return lvl
    return OFF_GRID


def nomml(notes, epsilon=EPSILON_BEATS):
    """Both NOMML variants for one phrase.

    Note-weighted (`nomml`/`performed`): every note is a sample — a chord's
    notes each count. Unique-onset (`nomml_onsets`/`performed_onsets`): notes
    sharing a timestamp (within 1e-3 beats) count ONCE, so chord density can't
    pose as timing evidence. Both reported; the ear spot-check decides which
    one gates (neither is validated yet)."""
    starts = [float(n.get("start_beats", n.get("start", 0.0))) for n in notes]
    if not starts:
        return {"nomml": None, "performed": False, "n_onsets": 0,
                "off_grid_share": 0.0, "nomml_onsets": None,
                "performed_onsets": False, "n_unique_onsets": 0,
                "off_grid_share_onsets": 0.0}
    levels = [onset_level(s, epsilon) for s in starts]
    med = statistics.median_high(levels)
    uniq = sorted({round(s, 3) for s in starts})
    ulevels = [onset_level(s, epsilon) for s in uniq]
    umed = statistics.median_high(ulevels)
    return {"nomml": med,
            "performed": med >= OFF_GRID,
            "n_onsets": len(starts),
            "off_grid_share": sum(1 for l in levels if l == OFF_GRID) / len(levels),
            "nomml_onsets": umed,
            "performed_onsets": umed >= OFF_GRID,
            "n_unique_onsets": len(uniq),
            "off_grid_share_onsets": sum(1 for l in ulevels if l == OFF_GRID) / len(ulevels)}
