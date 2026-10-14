"""Sticky develop — session hold state woven into the KEEP/DROP jam flow.

Design: hands batch 1 (docs/history/plans/2026-07-10-hands-batch1.md).
Lifecycle mirrors ears.py (fcntl flock, session reset at tools/session.py start).
"""

from __future__ import annotations

import json
import hashlib
import os
import sys

# One module universe for the bare import (gears.judge_answer) and the package
# import (tests): a package-side patch must intercept the bare reference (F4).
for _alias in ("develop_session", "mcp_server.develop_session"):
    sys.modules.setdefault(_alias, sys.modules[__name__])

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "tools", "ideas", "develop_session.json")

ROTATION: tuple[str, ...] | None = None


def _rotation() -> tuple[str, ...]:
    global ROTATION
    if ROTATION is None:
        import core as _core
        ROTATION = tuple(sorted(k for k in _core._DEVELOP_TRANSFORMS if ":" in k))
    return ROTATION


def load_state(path: str | None = None) -> dict:
    path = path or STATE_PATH
    if not os.path.exists(path):
        return {"sticky": False, "hold": None}
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (ValueError, OSError) as exc:
        raise ValueError(
            "develop session file is corrupt — say 'reset develop' to clear it (%s)" % exc
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            "develop session file has an invalid shape — say 'reset develop' to clear it")
    data.setdefault("sticky", False)
    if data.get("hold") is not None and not isinstance(data["hold"], dict):
        raise ValueError(
            "develop session file has an invalid shape — say 'reset develop' to clear it")
    return data


def _save_state(state: dict, path: str | None = None) -> None:
    path = path or STATE_PATH
    import core as _core
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    _core._save_json_atomic(path, state)


def _locked_update(fn, path: str | None = None) -> dict:
    path = path or STATE_PATH
    import fcntl
    os.makedirs(os.path.dirname(os.path.abspath(path + ".lock")), exist_ok=True)
    with open(path + ".lock", "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            state = load_state(path)
            state = fn(state)
            _save_state(state, path)
            return state
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def set_sticky(on: bool, path: str | None = None) -> dict:
    def _set(state: dict) -> dict:
        state["sticky"] = bool(on)
        return state
    return _locked_update(_set, path)


def _same_source_id(a: str | None, b: str | None) -> bool:
    """Hold continuity across the pid migration: equal strings, or equal once
    old aliases resolve through the build's id map (ledger._same_phrase_id).
    Fail-closed toward 'different phrase' when the ledger is unreachable."""
    if a == b:
        return True
    if not a or not b:
        return False
    try:
        import ledger as _ledger
    except ImportError:
        return False
    try:
        return bool(_ledger._same_phrase_id(a, b))
    except Exception:  # noqa: BLE001 — advisory match only; exact already failed
        return False


def record_keep(source_phrase_id: str, seed_notes: list[dict], key: str | None,
                path: str | None = None, *, event_id: str | None = None,
                conflict_fingerprint: str | None = None) -> dict:
    def _keep(state: dict) -> dict:
        if event_id:
            fingerprint = conflict_fingerprint or hashlib.sha256(json.dumps(
                {"kind": "keep", "source_phrase_id": source_phrase_id,
                 "seed_notes": seed_notes, "key": key}, sort_keys=True,
                separators=(",", ":"), default=str).encode()).hexdigest()
            existing = state.setdefault("judgment_events", {}).get(event_id)
            if existing is not None:
                if existing.get("fingerprint") != fingerprint:
                    raise ValueError("conflicting retry for develop event %s" % event_id)
                return state
        if not state.get("sticky"):
            if event_id:
                state.setdefault("judgment_events", {})[event_id] = {"fingerprint": fingerprint}
            return state
        hold = state.get("hold")
        if hold and _same_source_id(hold.get("source_phrase_id"), source_phrase_id):
            hold = dict(hold)
            hold["pass"] = hold.get("pass", 0) + 1
            hold["move_cursor"] = hold.get("move_cursor", 0) + 1
            state["hold"] = hold
        else:
            state["hold"] = {
                "source_phrase_id": source_phrase_id,
                "seed_notes": seed_notes,
                "key": key,
                "pass": 0,
                "move_cursor": 0,
            }
        if event_id:
            state.setdefault("judgment_events", {})[event_id] = {"fingerprint": fingerprint}
        return state
    return _locked_update(_keep, path)


def record_drop(path: str | None = None, *, event_id: str | None = None,
                conflict_fingerprint: str | None = None) -> dict:
    def _drop(state: dict) -> dict:
        if event_id:
            fingerprint = conflict_fingerprint or hashlib.sha256(b"drop").hexdigest()
            existing = state.setdefault("judgment_events", {}).get(event_id)
            if existing is not None:
                if existing.get("fingerprint") != fingerprint:
                    raise ValueError("conflicting retry for develop event %s" % event_id)
                return state
        state["hold"] = None
        if event_id:
            state.setdefault("judgment_events", {})[event_id] = {"fingerprint": fingerprint}
        return state
    return _locked_update(_drop, path)


def reset_state(path: str | None = None) -> str:
    path = path or STATE_PATH
    if os.path.exists(path):
        os.unlink(path)
    return "develop session reset — hold off, held idea cleared"


def current_move(state: dict) -> str:
    hold = state.get("hold")
    if not hold:
        raise ValueError("no hold in develop session state")
    rot = _rotation()
    return rot[hold.get("move_cursor", 0) % len(rot)]
