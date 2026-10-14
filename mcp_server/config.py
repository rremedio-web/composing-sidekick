"""Centralized configuration constants for the composing-sidekick MCP server.

Consolidates previously scattered constants across core.py, live_write.py,
and analyze modules, with explicit provenance documentation for each.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "analyze") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "analyze"))

from roles import ROLE_SOURCE, ROLE_THRESHOLDS, ROLES  # noqa: E402

# -----------------------------------------------------------------------------
# The Moment (hear_the_room)
# -----------------------------------------------------------------------------
# Provenance: moment.read_moment_live's literal 12-track jam-row sweep
# (2026-09-03, Batch 2 room fixes). Arrangement mode costs one read-track per
# track plus one read-clip per overlapping clip, hence its own cap.
# A dropped phrase and its near-duplicates stop being served. 0.97 is not a new
# number: it already means "this is the same phrase" in build_index.search's
# max_sim and in _rerank_by_ratings' down_drop.
REJECT_NEAR_DUP_COSINE = 0.97

MOMENT_MAX_TRACKS = 12
# whats_missing("live") reads every MIDI track's arrangement clips: one
# read-track per track, so a cap keeps a 60-track Set from stalling a session.
ARRANGEMENT_READ_MAX_TRACKS = 32
# hear_the_room(where='arrangement') hears the same tracks the arrangement
# reader reads (round-2 review: a 12-track default hid Bassline, track 19, at
# bar 33). An explicit max_tracks still overrides it.
ARRANGEMENT_MAX_TRACKS = ARRANGEMENT_READ_MAX_TRACKS
# Live's own scale setting becomes the room's key when present. Kill switch for
# a Set whose scale was never set on purpose (SIDEKICK_TRUST_LIVE_SCALE=0).
MOMENT_TRUST_LIVE_SCALE = os.environ.get("SIDEKICK_TRUST_LIVE_SCALE", "1") != "0"

# -----------------------------------------------------------------------------
# Producer Pal MCP Connection
# -----------------------------------------------------------------------------
# Provenance: From live_write.py / pp_session.py.
# Default HTTP endpoint for Producer Pal MCP bridge (Max for Live device).
# Overridable via PRODUCER_PAL_URL environment variable.
PP_URL = os.environ.get("PRODUCER_PAL_URL", "http://localhost:3350/mcp")

# -----------------------------------------------------------------------------
# Metric & Timing Constants
# -----------------------------------------------------------------------------
# Provenance: From core.py and analyze/structure.py.
# Standard 4 beats per bar (4/4 meter prevalent throughout Ricardo's catalog).
# Note: pp_codec.py maintains an independent copy for wire encoding isolation.
BEATS_PER_BAR = 4

# Provenance: From core.py.
# Standard candidate section lengths in bars (ordered by structural preference)
# used in song skeleton and section planning.
SECTION_BUCKETS = (8, 4, 16, 2, 32, 64)

# -----------------------------------------------------------------------------
# Arrangement & Role Layering
# -----------------------------------------------------------------------------
# Provenance: From core.py.
# Minimum section energy threshold (0.0 to 1.0) required before a role is introduced:
# pad enters early (0.25), bass (0.30), chords (0.45), drums (0.55), lead at climax (0.65).
_ROLE_ENERGY_THRESHOLD: dict[str, float] = {
    "pad": 0.25,
    "bass": 0.30,
    "chords": 0.45,
    "drums": 0.55,
    "lead": 0.65,
}

# Provenance: From core.py.
# Layer stacking order for arrangement rendering (bottom to top).
_ROLE_LAYER_ORDER: dict[str, int] = {
    "pad": 0,
    "bass": 1,
    "chords": 2,
    "drums": 3,
    "lead": 4,
}

# Provenance: From core.py.
# Target energy threshold (0.70) defining high-energy drop/peak sections in structure.
_DROP_ENERGY = 0.70

# -----------------------------------------------------------------------------
# Density Parameters
# -----------------------------------------------------------------------------
# Provenance: From core.py.
# Density parameters across 3 tiers (minimal, mid, full):
# - lead_npb: lead notes per bar
# - bass_npb: bass notes per bar
# - rest_ratio: fraction of rests vs active steps
# - pad_vel: velocity for sustained pad chords
_DENSITY_PARAMS: dict[str, dict[str, float]] = {
    "minimal": {"lead_npb": 1.25, "bass_npb": 0.75, "rest_ratio": 0.50, "pad_vel": 52},
    "mid": {"lead_npb": 2.75, "bass_npb": 1.75, "rest_ratio": 0.28, "pad_vel": 62},
    "full": {"lead_npb": 4.75, "bass_npb": 3.0, "rest_ratio": 0.12, "pad_vel": 70},
}

# Provenance: From core.py.
# Target drum hit count ranges (min, max) for an 8-bar section across density tiers.
_DRUM_DENSITY_TARGETS: dict[str, tuple[int, int]] = {
    "minimal": (8, 16),
    "mid": (24, 48),
    "full": (48, 72),
}
