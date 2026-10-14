"""Heuristic role labelling for non-drum clips/phrases.

Track names in .als are device/instrument names ("Serum 2", "Classic Road"), not
roles, so role is inferred from REGISTER (median pitch) + POLYPHONY/density, which
separate cleanly in typical catalogues. Used by build_index (stamp each phrase's
role) and style_profile (per-role melodic grammar) so the LEAD model stops being a
warm average of lead + bass + pad material.

Works on either parse_als Note objects (.pitch/.start/.enabled) or MCP note dicts.
"""
from __future__ import annotations

from collections import Counter

ROLES = ("lead", "bass", "pad", "other")

# Threshold rationale (conventional register and texture boundaries):
#   - pad_min_polyphony = 2.2: separates chords / stacked textures (>= 2.2) from monophonic lines
#   - bass_max_pitch = 48 (< 48, below C3): low register baseline
#   - lead_min_pitch = 55 (>= 55, G3+): mid/high register baseline
#   - [48..54] with polyphony < 2.2 is classified as "other" (the ambiguous middle zone)
ROLE_THRESHOLDS = {
    "pad_min_polyphony": 2.2,
    "bass_max_pitch": 48,
    "lead_min_pitch": 55,
}

ROLE_SOURCE = "rules_v1"


def _pitch(n) -> int:
    return int(n["pitch"] if isinstance(n, dict) else n.pitch)


def _start(n) -> float:
    if isinstance(n, dict):
        return float(n.get("start_beats", n.get("start", 0.0)))
    return float(n.start)


def _enabled(n) -> bool:
    if isinstance(n, dict):
        return bool(n.get("enabled", True))
    return bool(getattr(n, "enabled", True))


def clip_role_report(notes, is_drum: bool = False) -> dict:
    """Evaluate register + polyphony and return {role, confidence, reason}.

    Confidence is a heuristic in [0.0, 1.0] derived from distance to threshold boundaries.
    Reason describes the measured values that drove the classification.
    """
    if is_drum:
        return {
            "role": "drum",
            "confidence": 1.0,
            "reason": "drum track (is_drum=True)",
            "role_source": ROLE_SOURCE,
        }
    ns = [n for n in notes if _enabled(n)]
    if len(ns) < 2:
        return {
            "role": "other",
            "confidence": 1.0,
            "reason": f"fewer than 2 enabled notes (n={len(ns)})",
            "role_source": ROLE_SOURCE,
        }
    pitches = sorted(_pitch(n) for n in ns)
    median = pitches[len(pitches) // 2]
    slots = Counter(round(_start(n) * 4) for n in ns)
    avg_poly = sum(slots.values()) / len(slots)

    pad_thresh = ROLE_THRESHOLDS["pad_min_polyphony"]
    bass_thresh = ROLE_THRESHOLDS["bass_max_pitch"]
    lead_thresh = ROLE_THRESHOLDS["lead_min_pitch"]

    if avg_poly >= pad_thresh:
        # Distance above the pad threshold (2.2); confidence saturates 1.3 above it.
        conf = min(1.0, max(0.5, 0.5 + 0.5 * (avg_poly - pad_thresh) / 1.3))
        return {
            "role": "pad",
            "confidence": round(conf, 3),
            "reason": f"pad: avg_poly {avg_poly:.2f} >= {pad_thresh}, median {median}",
            "role_source": ROLE_SOURCE,
        }
    if median < bass_thresh:
        # Distance below the bass threshold (48); confidence saturates 9 semitones below.
        conf = min(1.0, max(0.5, 0.5 + 0.5 * (bass_thresh - median) / 9.0))
        return {
            "role": "bass",
            "confidence": round(conf, 3),
            "reason": f"bass: median pitch {median} < {bass_thresh}, avg_poly {avg_poly:.2f}",
            "role_source": ROLE_SOURCE,
        }
    if median >= lead_thresh:
        # Distance above the lead threshold (55); confidence saturates 24 semitones above.
        conf = min(1.0, max(0.5, 0.5 + 0.5 * (median - lead_thresh) / 24.0))
        return {
            "role": "lead",
            "confidence": round(conf, 3),
            "reason": f"lead: median pitch {median} >= {lead_thresh}, avg_poly {avg_poly:.2f}",
            "role_source": ROLE_SOURCE,
        }

    # Ambiguous middle register [48..54] with monophonic/low density
    dist = min(median - bass_thresh, lead_thresh - 1 - median)
    conf = max(0.3, round(0.5 - 0.05 * dist, 3))
    return {
        "role": "other",
        "confidence": round(conf, 3),
        "reason": (f"other: ambiguous register (median {median} in "
                   f"[{bass_thresh}..{lead_thresh-1}]), avg_poly {avg_poly:.2f} < {pad_thresh}"),
        "role_source": ROLE_SOURCE,
    }


def clip_role(notes, is_drum: bool = False) -> str:
    """lead | bass | pad | other | drum, from register + polyphony.

    pad   = stacked/sustained (avg >= 2.2 notes per 16th-onset)
    bass  = low + mostly monophonic (median pitch < 48 ≈ below C3)
    lead  = mid/high + mostly monophonic (median pitch >= 55 ≈ G3+)
    other = the ambiguous middle (kept out of the lead grammar)
    """
    return clip_role_report(notes, is_drum=is_drum)["role"]

