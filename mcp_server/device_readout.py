"""SOMBRA device readout — canonical chassis strings (state never identity).

Design: ears context layer (docs/history/plans/2026-07-10-ears-context-layer.md).
Every line is second-person, plain, artist-agnostic — clear to a stranger.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

GEAR_NAMES: dict[int, str] = {
    1: "recall — your phrase, untouched",
    2: "recontext — your phrase, refit to the room",
    3: "develop — your phrase, reworked",
    4: "grow — your phrase + a composed tail",
    5: "compose — fresh, in your grammar",
    6: "cover — one era of you as another",
}


def sticky_announcement(st: dict) -> str:
    """Canonical hold state line — device, chat, and MCP share these words."""
    if not st.get("sticky"):
        return "hold off — answers start fresh"
    hold = st.get("hold")
    if not hold:
        return "hold on — your next answer sticks"
    import develop_session as _develop_session
    move = _develop_session.current_move(st)
    return "hold on — holding your idea, pass %d (%s)" % (
        hold["pass"], move)


def hands_line(st: dict | None = None, *, develop_path: str | None = None) -> str:
    """Hands tab readout: hold state only — never track or project names."""
    if st is None:
        import develop_session as _develop_session
        st = _develop_session.load_state(develop_path)
    if not st.get("sticky"):
        return "not holding — answers start fresh"
    hold = st.get("hold")
    if not hold:
        return "ready to hold — your next answer sticks"
    import develop_session as _develop_session
    move = _develop_session.current_move(st)
    return "holding your idea, pass %d (%s)" % (hold["pass"], move)


def device_readout(
    moment=None,
    *,
    develop_path: str | None = None,
) -> dict:
    """Device poll payload: ears + hands lines and the six gear one-liners."""
    import ears as _ears
    if moment is None:
        import moment as _moment_mod
        moment = _moment_mod.get_moment()
    import develop_session as _develop_session
    st = _develop_session.load_state(develop_path)
    return {
        "ears_line": _ears.status_line(moment),
        "hands_line": hands_line(st),
        "gear_names": dict(GEAR_NAMES),
    }
