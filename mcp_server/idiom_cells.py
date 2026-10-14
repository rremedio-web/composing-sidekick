"""Idiom cells and feel calibration the proposal engine composes from.

The shipped values are neutral defaults (``evidence: "default"``): common
four-note cells and a moderate, human-ish feel. A composer can replace them
with values measured on their own catalogue in an optional, gitignored
``store/calibration.json`` (keys ``cells``, ``feel``; see ``load_calibration``).
The borrowed moves are Ben Frost's, read from the Híbakúsja Live pack Set
(``set_file``), never the owner's idiom.
"""
from __future__ import annotations

import json
import os

IDIOM_SOURCE = ".claude/skills/sombra-brain/references/idiom.md"
BORROWED_SOURCE = ".claude/skills/sombra-brain/references/borrowed-techniques.md"
DEFAULT_SOURCE = "neutral default (no owner calibration)"
CALIBRATION_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "store", "calibration.json")

_DEGREES = {"1": 0, "b2": 1, "2": 2, "b3": 3, "3": 4, "4": 5, "#4": 6, "b5": 6,
            "5": 7, "b6": 8, "6": 9, "b7": 10, "7": 11}


def semitones(degree: str) -> int:
    """Scale degree name (absolute interval above the root) -> semitones."""
    if degree not in _DEGREES:
        raise ValueError("unknown degree %r" % (degree,))
    return _DEGREES[degree]


def _cell(cell_id, degrees, gesture, tracks=1, evidence="default", source=None):
    for degree in degrees:
        semitones(degree)
    return {"id": cell_id, "degrees": tuple(degrees), "gesture": gesture, "tracks": int(tracks),
            "evidence": evidence,
            "source": source or (IDIOM_SOURCE + " § Melodic vocabulary — " + DEFAULT_SOURCE)}


# Neutral defaults: plain stepwise and triadic shapes in a minor-mode frame.
_DEFAULT_CELLS = (
    _cell("step_up", ("1", "2", "b3", "2"), "climb"),
    _cell("triad_fall", ("5", "b3", "1", "b7"), "fall"),
    _cell("cadence", ("1", "b7", "b6", "5"), "dark landing"),
    _cell("neighbour", ("1", "2", "1", "b7"), "wobble"),
    _cell("fourth_climb", ("5", "1", "2", "4"), "climb"),
    _cell("arch", ("1", "b3", "5", "b3"), "climb"),
)

_DEFAULT_FEEL = {
    "off_grid_share": 0.5,
    "off_grid_beats": (0.02, 0.04),
    "velocity_mean": 80,
    "velocity_std": 20,
    "downbeat_velocity": 90,
    "offbeat_velocity": 75,
    "velocity_jitter_std": 5,
    "evidence": "default",
    "measured_in": "neutral default (frozen; override in store/calibration.json)",
    "source": IDIOM_SOURCE + " § Time and touch — " + DEFAULT_SOURCE,
}

_FEEL_NUMBERS = ("off_grid_share", "velocity_mean", "velocity_std", "downbeat_velocity",
                 "offbeat_velocity", "velocity_jitter_std")


def load_calibration(path: str = CALIBRATION_PATH):
    """Return (cells, feel): the owner's calibration from ``path`` when present,
    else the neutral defaults. A present but malformed file raises, so a broken
    calibration is never silently replaced by defaults.

    Format: ``{"cells": [{"id", "degrees", "gesture", "tracks"}...] (exactly 6),
    "feel": {<_FEEL_NUMBERS>, "off_grid_beats": [lo, hi]}}``; either key may be
    omitted."""
    if not os.path.exists(path):
        return _DEFAULT_CELLS, dict(_DEFAULT_FEEL)
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    cells = _DEFAULT_CELLS
    if "cells" in data:
        rows = data["cells"]
        if not isinstance(rows, list) or len(rows) != len(_DEFAULT_CELLS):
            raise ValueError("calibration cells must be a list of %d cells" % len(_DEFAULT_CELLS))
        cells = tuple(_cell(r["id"], r["degrees"], r["gesture"], r.get("tracks", 1),
                            evidence="measured", source="store/calibration.json")
                      for r in rows)
    feel = dict(_DEFAULT_FEEL)
    if "feel" in data:
        given = data["feel"]
        for key in _FEEL_NUMBERS:
            if key in given:
                feel[key] = given[key]
        if "off_grid_beats" in given:
            feel["off_grid_beats"] = tuple(given["off_grid_beats"])
        feel.update(evidence="measured", measured_in="store/calibration.json",
                    source="store/calibration.json")
    return cells, feel


CELLS, FEEL = load_calibration()
CELL_BY_ID = {cell["id"]: cell for cell in CELLS}

# Motif rhythms (beats per note; the cell repeats until the rhythm is used up).
# Each names a cell by position so an owner calibration keeps the shapes.
_MOTIF_RHYTHMS = {
    "circling_8th": (0, (0.5,) * 8, (60, 72)),
    "climb": (4, (1.0, 1.0, 1.0, 1.0), (55, 67)),
    "slow_bell": (1, (4.0, 4.0, 4.0, 4.0), (72, 84)),
    "dotted_fall": (2, (1.5, 1.5, 1.0, 4.0), (60, 72)),
}
MOTIF_SHAPES = {name: {"cell": CELLS[index]["id"], "rhythm": rhythm, "register": register,
                       "evidence": "inferred", "source": "hand-composed motif rhythms"}
                for name, (index, rhythm, register) in _MOTIF_RHYTHMS.items()}


def _borrowed(key, quote, **fields):
    return {"key": key, "artist": "Ben Frost", "his_idiom": False, "evidence": "set_file",
            "source": BORROWED_SOURCE + " § Ben Frost — Híbakúsja Live pack Set", "quote": quote,
            **fields}


BORROWED = {
    "hold_a_world_back": _borrowed(
        "hold_a_world_back", "**Hold a world back.**",
        fits="the full palette appears too early; the last big section feels like a repeat"),
    "near_empty_break": _borrowed(
        "near_empty_break", "**Near-empty break before the last body.**",
        fits="the density never drops before the end"),
    "unequal_loops": _borrowed(
        "unequal_loops", "2, 4, 8, 11, 12, 16, 24, 34-bar loops run together; 11/12/34 never",
        loop_bars=(2, 4, 8, 11, 12, 16, 24, 34),
        fits="a long static section needs motion without new notes"),
}
