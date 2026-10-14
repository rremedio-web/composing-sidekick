# The Ears — Context Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every serving surface (retrieval, answers, growth, audition) awareness of the room — what's playing, the stated direction, and this session's keeps/rejects — behind admission gates that filter but never score, promoted only by a blind Booth round.

**Architecture:** Two new small modules — `mcp_server/moment.py` (read-only room snapshot, pure builders + one Producer Pal reader) and `mcp_server/ears.py` (admission gates, direction modifiers, session memory, the `serve()` chokepoint) — wired lazily into existing core tools via `use_ears` parameters. No module-level imports of `core` from the new modules, and only lazy in-function imports of `ears`/`moment` from `core`, so no import cycles.

**Tech Stack:** Python 3 (repo venv `.venv/bin/python`), the repo's own named-selftest convention (`mcp_server/server.py --selftest`), Producer Pal MCP bridge via `mcp_server/live_write.py`.

**Spec:** `docs/superpowers/specs/2026-07-10-ears-context-layer-design.md` — read it before starting.

## Global Constraints

- **No new scorers.** Gates filter; survivors keep their order for the existing taste ranking (spec §3).
- **Fail loud.** Blind mode is declared, never guessed; empty survivor sets refuse with a nearest-relaxation offer; unknown direction words raise (spec §8).
- **Read-only on Live.** Nothing in the ears path writes clips, tracks, or automation (spec §8).
- **Session memory never touches the taste model** — session-scoped file only, demote-not-ban, every demotion announced (spec §4).
- **`use_ears` defaults OFF** until the Booth round promotes it via `store/ears_default.json` (spec §6).
- **Controls show state, never identity** — no track/project names in any device-facing string (spec §7).
- **Canonical voice:** all ears announcements come from `ears.status_line()` / the exact strings defined here — same words everywhere (spec §7).
- **Testing convention:** this repo does NOT use pytest. Tests are named blocks inside `run_selftest()` in `mcp_server/server.py`, inserted immediately BEFORE the line `print("=== all selftests passed ===")` (currently server.py:3113). Red/green cycles use small `.venv/bin/python - <<'PY'` probes; the full suite runs once per task before commit.
- All commands run from the repo root: `cd ~/composing-sidekick`.
- Every commit message ends with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 1: The Moment — pure builders (`mcp_server/moment.py`)

**Files:**
- Create: `mcp_server/moment.py`
- Test: new named block in `mcp_server/server.py` (see Step 4)

**Interfaces:**
- Consumes: `core.estimate_key(pc_hist) -> (tonic, mode, _)` (lazy import inside `build_moment` only).
- Produces (later tasks rely on these exact names):
  - `tempo_world(tempo) -> str` — `"club" | "downtempo" | "other"`
  - `density_band(notes) -> str` — `"sparse" | "mid" | "busy"`
  - `register_band(notes) -> str | None` — `"low" | "mid" | "high"` (None for empty)
  - `register_range(notes) -> tuple[int, int] | None`
  - `infer_role(track_name, notes) -> str` — `"drum" | "bass" | "lead" | "harmony"`
  - `build_moment(tracks, tempo) -> dict` with keys `ears="live"`, `tempo`, `tempo_world`, `key`, `assumptions`, `roles`, `digest`
  - `blind_moment(reason) -> dict` with `ears="blind"`, `reason`, empty `roles`

- [ ] **Step 1: Write the failing probe**

```bash
.venv/bin/python - <<'PY'
import sys; sys.path.insert(0, "mcp_server")
import moment
m = moment.build_moment(
    [{"name": "Drums", "notes": [{"pitch": 36, "start": i * 0.25, "duration": 0.2,
                                  "velocity": 100} for i in range(32)]},
     {"name": "Sub", "notes": [{"pitch": 32, "start": 0.0, "duration": 4.0,
                                "velocity": 90}]}],
    tempo=75.0)
assert m["ears"] == "live" and m["tempo_world"] == "downtempo"
assert m["roles"]["drum"]["density"] == "busy"
assert m["roles"]["bass"]["register_band"] == "low"
b = moment.blind_moment("Live unreachable")
assert b["ears"] == "blind" and b["roles"] == {}
print("MOMENT_OK")
PY
```

- [ ] **Step 2: Run it — expect failure**

Expected: `ModuleNotFoundError: No module named 'moment'`

- [ ] **Step 3: Write `mcp_server/moment.py`**

```python
"""The Moment — a read-only snapshot of the room.

Spec: docs/superpowers/specs/2026-07-10-ears-context-layer-design.md (section 2).
Pure builders here so selftests can feed synthetic rooms; the Producer Pal
reader injects into build_moment(). ears is "live" or "blind" — NEVER guessed.
"""

from __future__ import annotations

import hashlib
import json

TEMPO_WORLDS = (("downtempo", 60.0, 92.0), ("club", 100.0, 140.0))
DENSITY_BANDS = (("sparse", 0.0, 1.0), ("mid", 1.0, 3.0), ("busy", 3.0, float("inf")))
REGISTER_BANDS = (("low", 0, 48), ("mid", 48, 72), ("high", 72, 128))
_ROLE_KEYWORDS = (
    ("drum", ("drum", "kick", "perc", "hat", "snare", "808", "batida")),
    ("bass", ("bass", "sub", "baixo")),
    ("harmony", ("pad", "chord", "keys", "harmon", "string")),
    ("lead", ("lead", "melod", "arp", "voz", "vox")),
)


def tempo_world(tempo) -> str:
    t = float(tempo)
    for name, lo, hi in TEMPO_WORLDS:
        if lo <= t <= hi:
            return name
    return "other"


def density_band(notes) -> str:
    if not notes:
        return "sparse"
    span = max(n["start"] + n.get("duration", 0.0) for n in notes)
    per_beat = len(notes) / max(span, 1.0)
    for name, lo, hi in DENSITY_BANDS:
        if lo <= per_beat < hi:
            return name
    return "busy"


def register_range(notes):
    pitches = [int(n["pitch"]) for n in notes]
    return (min(pitches), max(pitches)) if pitches else None


def register_band(notes):
    if not notes:
        return None
    median = sorted(int(n["pitch"]) for n in notes)[len(notes) // 2]
    for name, lo, hi in REGISTER_BANDS:
        if lo <= median < hi:
            return name
    return "high"


def infer_role(track_name, notes) -> str:
    low = str(track_name or "").lower()
    for role, words in _ROLE_KEYWORDS:
        if any(w in low for w in words):
            return role
    if not notes:
        return "lead"
    if register_band(notes) == "low":
        return "bass"
    starts = {}
    for n in notes:
        key = round(float(n["start"]), 3)
        starts[key] = starts.get(key, 0) + 1
    poly = sum(1 for c in starts.values() if c > 1) / max(len(starts), 1)
    return "harmony" if poly > 0.4 else "lead"


def build_moment(tracks, tempo) -> dict:
    """tracks: [{"name": str, "notes": [{"pitch","start","duration","velocity"}]}]"""
    by_role = {}
    for t in tracks:
        notes = t.get("notes") or []
        by_role.setdefault(infer_role(t.get("name", ""), notes), []).extend(notes)

    roles = {}
    pc_hist = [0.0] * 12
    for role, notes in sorted(by_role.items()):
        roles[role] = {
            "n_notes": len(notes),
            "density": density_band(notes),
            "register": register_range(notes),
            "register_band": register_band(notes),
        }
        if role != "drum":
            for n in notes:
                pc_hist[int(n["pitch"]) % 12] += 1.0

    if any(pc_hist):
        import core as _core  # lazy: no cycle at import time
        tonic, mode, _ = _core.estimate_key(pc_hist)
        key = "%s %s" % (tonic, mode)
        key_note = "key %s heard from the playing clips (minor-leaning corpus prior)" % key
    else:
        key, key_note = None, "no pitched material playing — key unknown"

    fingerprint = json.dumps({"tempo": float(tempo), "roles": roles, "key": key},
                             sort_keys=True, separators=(",", ":"))
    return {
        "ears": "live",
        "tempo": float(tempo),
        "tempo_world": tempo_world(tempo),
        "key": key,
        "assumptions": [key_note],
        "roles": roles,
        "digest": hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12],
    }


def blind_moment(reason) -> dict:
    return {"ears": "blind", "reason": str(reason), "tempo": None,
            "tempo_world": None, "key": None, "roles": {},
            "assumptions": ["no ears — serving context-blind"]}
```

- [ ] **Step 4: Add the named selftest block**

In `mcp_server/server.py`, immediately before `print("=== all selftests passed ===")`, insert:

```python
    # 42. the Moment: pure room snapshot — synthetic tracks, no Live needed
    print("--- the Moment: tempo worlds, roles, registers, blind mode ---")
    import moment as _moment42  # noqa: E402
    assert _moment42.tempo_world(75) == "downtempo"
    assert _moment42.tempo_world(122) == "club"
    assert _moment42.tempo_world(96) == "other", "between worlds must say so, not guess"
    _busy42 = [{"pitch": 36, "start": i * 0.25, "duration": 0.2, "velocity": 100}
               for i in range(32)]
    _sub42 = [{"pitch": 32, "start": 0.0, "duration": 4.0, "velocity": 90}]
    _pad42 = [{"pitch": 60 + o, "start": b * 2.0, "duration": 2.0, "velocity": 70}
              for b in range(2) for o in (0, 3, 7)]
    assert _moment42.density_band(_busy42) == "busy"
    assert _moment42.density_band(_sub42) == "sparse"
    assert _moment42.infer_role("Weird Name 7", _sub42) == "bass", "note stats fallback"
    assert _moment42.infer_role("Weird Name 8", _pad42) == "harmony", "polyphony fallback"
    assert _moment42.infer_role("batida x", _busy42) == "drum", "his naming, his language"
    _m42 = _moment42.build_moment(
        [{"name": "Drums", "notes": _busy42}, {"name": "Sub", "notes": _sub42},
         {"name": "Pad", "notes": _pad42}], tempo=75.0)
    assert _m42["ears"] == "live" and _m42["tempo_world"] == "downtempo"
    assert set(_m42["roles"]) == {"drum", "bass", "harmony"}
    assert _m42["key"], "pitched material must produce a key estimate"
    assert _m42["digest"] == _moment42.build_moment(
        [{"name": "Drums", "notes": _busy42}, {"name": "Sub", "notes": _sub42},
         {"name": "Pad", "notes": _pad42}], tempo=75.0)["digest"], "moment deterministic"
    _b42 = _moment42.blind_moment("bridge down")
    assert _b42["ears"] == "blind" and _b42["roles"] == {} and _b42["key"] is None
    print("  OK\n")
```

- [ ] **Step 5: Run the probe from Step 1 — expect `MOMENT_OK`**

- [ ] **Step 6: Run the full suite**

```bash
.venv/bin/python mcp_server/server.py --selftest > /tmp/ears-t1.log 2>&1; echo exit=$?; tail -3 /tmp/ears-t1.log
```
Expected: `exit=0`, `=== all selftests passed ===`

- [ ] **Step 7: Commit**

```bash
git add mcp_server/moment.py mcp_server/server.py
git commit -m "feat(ears): the Moment — pure room snapshot with declared blind mode"
```

---

### Task 2: The Moment — Producer Pal reader

**Files:**
- Modify: `mcp_server/moment.py` (append)

**Interfaces:**
- Consumes: `live_write.read_set() -> str` (overview text), `live_write.read_clip(track_index, slot=0) -> dict` with `notes`/`raw`, `live_write._find_int(text, key, default)`, `live_write._find_str(text, key, default)`.
- Produces: `read_moment_live(max_tracks=12) -> dict` (async, raises on bridge failure) and `get_moment() -> dict` (sync, never raises — converts failure to `blind_moment`).

- [ ] **Step 1: Write the failing probe**

```bash
.venv/bin/python - <<'PY'
import sys; sys.path.insert(0, "mcp_server")
import moment
m = moment.get_moment()          # no Live running in CI — must go blind, not raise
assert m["ears"] in ("live", "blind")
if m["ears"] == "blind":
    assert m["reason"], "blind mode must say why"
print("GET_MOMENT_OK", m["ears"])
PY
```

Expected now: `AttributeError: module 'moment' has no attribute 'get_moment'`

- [ ] **Step 2: Append to `mcp_server/moment.py`**

```python
async def read_moment_live(max_tracks: int = 12) -> dict:
    """Read the room through Producer Pal. Raises on any bridge failure so the
    caller converts it into a DECLARED blind moment — no silent guessing.

    v1 hears session row 0 of each track (the jam-row convention); playing-clip
    detection is a follow-up, and check_ears prints exactly what was heard.
    """
    import live_write  # lazy: keeps moment importable without the MCP deps

    overview = await live_write.read_set()
    tempo = live_write._find_int(overview, "tempo", None)
    if tempo is None:
        raise RuntimeError("live-set overview carries no tempo")

    tracks = []
    for idx in range(max_tracks):
        try:
            clip = await live_write.read_clip(idx, slot=0)
        except Exception:
            break  # first unreadable index ends the sweep — sets are contiguous
        if clip.get("notes"):
            name = live_write._find_str(clip.get("raw", ""), "trackName", None) \
                or "track %d" % idx
            tracks.append({"name": name, "notes": clip["notes"]})
    return build_moment(tracks, tempo)


def get_moment() -> dict:
    """Sync entry: the live room, or a declared-blind moment. Never raises."""
    import asyncio
    try:
        return asyncio.run(read_moment_live())
    except Exception as exc:
        return blind_moment("%s: %s" % (type(exc).__name__, exc))
```

- [ ] **Step 3: Run the probe — expect `GET_MOMENT_OK blind`** (no Live running; with Live up it prints `live`)

- [ ] **Step 4: Run the full suite** (same command as Task 1 Step 6). Expected: exit=0.

- [ ] **Step 5: Commit**

```bash
git add mcp_server/moment.py
git commit -m "feat(ears): read the room via Producer Pal, blind mode on any failure"
```

---

### Task 3: Admission gates (`mcp_server/ears.py`)

**Files:**
- Create: `mcp_server/ears.py`
- Test: new named block in `mcp_server/server.py`

**Interfaces:**
- Consumes: `moment.tempo_world`, `moment.density_band`, `moment.register_band` (module import `import moment as _moment` at top — moment never imports ears, so no cycle). Lazy in-function `import core as _core` for `_parse_key_string` and `PITCH_NAMES`.
- Produces:
  - `GATE_ORDER = ("tempo_world", "key", "register", "density", "role")`
  - `admit(candidates, moment_dict, *, role=None, direction=None) -> (survivors, gate_report)` — candidates are phrase dicts with optional `notes`, `key`, `tempo`, `role`. `gate_report` keys: `pool`, `direction`, `dropped` (list of `{"gate", "n"}` in gate order), `survived`, `notes`, and `nearest_relaxation` (`{"gate", "offer"}`) only when survivors is empty.
  - Raises `ValueError` on unknown `direction` and on a blind moment (serve() handles blind, Task 6).

- [ ] **Step 1: Write the failing probe**

```bash
.venv/bin/python - <<'PY'
import sys; sys.path.insert(0, "mcp_server")
import moment, ears
m = moment.build_moment(
    [{"name": "Drums", "notes": [{"pitch": 36, "start": i * 0.25, "duration": 0.2,
                                  "velocity": 100} for i in range(32)]}],
    tempo=75.0)
pool = [
    {"id": "fits", "role": "lead", "key": m["key"], "tempo": 75,
     "notes": [{"pitch": 62, "start": 0.0, "duration": 1.0, "velocity": 80}]},
    {"id": "wrong-world", "role": "lead", "key": m["key"], "tempo": 122,
     "notes": [{"pitch": 62, "start": 0.0, "duration": 1.0, "velocity": 80}]},
]
survivors, report = ears.admit(pool, m, role="lead")
assert [c["id"] for c in survivors] == ["fits"]
assert report["pool"] == 2 and report["survived"] == 1
assert report["dropped"][0] == {"gate": "tempo_world", "n": 1}
try:
    ears.admit(pool, m, role="lead", direction="moist")
    raise SystemExit("FAIL: unknown direction accepted")
except ValueError:
    pass
print("ADMIT_OK")
PY
```

Expected now: `ModuleNotFoundError: No module named 'ears'`

- [ ] **Step 2: Write `mcp_server/ears.py`**

```python
"""The ears — admission gates, direction modifiers, session memory, serve().

Spec: docs/superpowers/specs/2026-07-10-ears-context-layer-design.md (3-5).
Gates FILTER, they never score; survivors keep their order so the caller's
existing taste ranking stays the only ranker. Session memory demotes, never
bans, and never writes to the taste model.
"""

from __future__ import annotations

import json
import os

import moment as _moment

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION_PATH = os.path.join(ROOT, "tools", "ideas", "ears_session.json")

GATE_ORDER = ("tempo_world", "key", "register", "density", "role")
_DENSITY_ORDER = ("sparse", "mid", "busy")

DIRECTIONS = {
    # gate MODIFIERS only, never scores (spec section 3; device-locked words)
    "darker": {"density_max": "mid", "minor_only": True},
    "clubbier": {"tempo_world": "club", "density_min": "mid"},
    "sparser": {"density_max": "sparse"},
    "weirder": {"skip_gates": ("density", "register")},
}


def _key_compatible(moment_key, cand_key, minor_only=False):
    if not moment_key or not cand_key:
        return not minor_only or "major" not in str(cand_key).lower()
    import core as _core
    try:
        mt, mm = _core._parse_key_string(str(moment_key))
        ct, cm = _core._parse_key_string(str(cand_key))
    except Exception:
        return True  # unparseable keys stay admitted — reported, not dropped
    if minor_only and cm == "major":
        return False
    mpc = _core.PITCH_NAMES.index(mt)
    cpc = _core.PITCH_NAMES.index(ct)
    diff = (cpc - mpc) % 12
    if diff == 0:
        return True  # same tonic; mode borrowing is his idiom
    if {mm, cm} == {"minor", "major"} and diff in (3, 9):
        return True  # relative pair
    return mm == cm and diff in (5, 7)  # the i<->iv / v neighborhood


def _gate_passes(gate, cand, m, role, mods, notes):
    if gate == "tempo_world":
        want = mods.get("tempo_world") or m.get("tempo_world")
        if want in (None, "other"):
            return True
        ct = cand.get("tempo")
        if ct is None:
            if "tempo-less candidates admitted" not in notes:
                notes.append("tempo-less candidates admitted")
            return True
        return _moment.tempo_world(ct) == want
    if gate == "key":
        return _key_compatible(m.get("key"), cand.get("key"),
                               minor_only=bool(mods.get("minor_only")))
    if gate == "register":
        band = _moment.register_band(cand.get("notes") or [])
        if band is None:
            return True
        target = role or cand.get("role")
        for r, info in (m.get("roles") or {}).items():
            if r != target and info.get("register_band") == band:
                return False
        return True
    if gate == "density":
        band = _moment.density_band(cand.get("notes") or [])
        dmax, dmin = mods.get("density_max"), mods.get("density_min")
        if dmax and _DENSITY_ORDER.index(band) > _DENSITY_ORDER.index(dmax):
            return False
        if dmin and _DENSITY_ORDER.index(band) < _DENSITY_ORDER.index(dmin):
            return False
        room = m.get("roles") or {}
        busiest = max((_DENSITY_ORDER.index(i.get("density", "sparse"))
                       for i in room.values()), default=0)
        if busiest >= 2:  # busy room wants complement, not competition
            return band != "busy"
        return True
    if gate == "role":
        cr = cand.get("role")
        if cr is None or cr == role:
            return True
        return cr not in set(m.get("roles") or {})
    raise ValueError("unknown gate %r" % gate)


def _run_gates(cands, m, role, mods, disabled=()):
    skip = set(mods.get("skip_gates", ()))
    drops, notes = [], []
    for gate in GATE_ORDER:
        if gate in disabled:
            continue
        if gate in skip:
            drops.append({"gate": gate, "n": 0, "skipped": True})
            continue
        keep = [c for c in cands if _gate_passes(gate, c, m, role, mods, notes)]
        drops.append({"gate": gate, "n": len(cands) - len(keep)})
        cands = keep
    return cands, drops, notes


def admit(candidates, m, *, role=None, direction=None):
    """The admission ladder. Returns (survivors, gate_report). Never relaxes
    silently: an empty result carries a nearest_relaxation offer instead."""
    if direction is not None and direction not in DIRECTIONS:
        raise ValueError("unknown direction %r — one of %s"
                         % (direction, "/".join(sorted(DIRECTIONS))))
    if m.get("ears") != "live":
        raise ValueError("admit() needs a live moment — serve() handles blind loudly")

    mods = dict(DIRECTIONS.get(direction, {}))
    pool = list(candidates)
    survivors, drops, notes = _run_gates(pool, m, role, mods)
    report = {"pool": len(pool), "direction": direction, "dropped": drops,
              "survived": len(survivors), "notes": notes}
    if not survivors and pool:
        for gate in reversed(GATE_ORDER):
            retry, _, _ = _run_gates(pool, m, role, mods, disabled=(gate,))
            if retry:
                report["nearest_relaxation"] = {
                    "gate": gate,
                    "offer": "nothing fits — relaxing the %s gate would admit %d; "
                             "want it?" % (gate, len(retry)),
                }
                break
    return survivors, report
```

- [ ] **Step 3: Run the probe — expect `ADMIT_OK`**

- [ ] **Step 4: Add the named selftest block** (before the `all selftests passed` print, after block 42):

```python
    # 43. ears admission gates: filter + report, refuse loudly, never score
    print("--- ears gates: order, register/density/role, empty-set refusal ---")
    import ears as _ears43  # noqa: E402
    _room43 = _moment42.build_moment(
        [{"name": "Drums", "notes": _busy42},
         {"name": "Lead x", "notes": [{"pitch": 74, "start": 0.0, "duration": 1.0,
                                       "velocity": 90}] * 4}], tempo=75.0)
    _mk43 = _room43["key"]
    _fit43 = {"id": "fit", "role": "lead", "key": _mk43, "tempo": 76,
              "notes": [{"pitch": 62, "start": 0.0, "duration": 1.0, "velocity": 80},
                        {"pitch": 60, "start": 2.0, "duration": 1.0, "velocity": 80}]}
    _clash43 = {"id": "clash", "role": "harmony", "key": _mk43, "tempo": 76,
                "notes": [{"pitch": 75, "start": 0.0, "duration": 1.0, "velocity": 80}]}
    _wall43 = {"id": "wall", "role": "lead", "key": _mk43, "tempo": 76,
               "notes": [{"pitch": 62, "start": i * 0.25, "duration": 0.2,
                          "velocity": 80} for i in range(32)]}
    _srv43, _rep43 = _ears43.admit([_fit43, _clash43, _wall43], _room43, role="lead")
    assert [c["id"] for c in _srv43] == ["fit"], _rep43
    assert [d["gate"] for d in _rep43["dropped"]] == list(_ears43.GATE_ORDER)
    _wall_only43, _rep_wall43 = _ears43.admit([_wall43], _room43, role="lead")
    assert _wall_only43 == [] and "nearest_relaxation" in _rep_wall43, \
        "an empty survivor set must offer its nearest single-gate relaxation"
    assert "relaxing the density gate" in _rep_wall43["nearest_relaxation"]["offer"]
    try:
        _ears43.admit([_fit43], _moment42.blind_moment("down"), role="lead")
        raise AssertionError("admit must refuse blind moments — serve handles blind")
    except ValueError:
        pass
    print("  OK\n")
```

- [ ] **Step 5: Run the full suite. Expected: exit=0.**

- [ ] **Step 6: Commit**

```bash
git add mcp_server/ears.py mcp_server/server.py
git commit -m "feat(ears): admission gates — filter with a report, refuse with an offer"
```

---

### Task 4: Direction modifiers

**Files:**
- Modify: `mcp_server/server.py` (selftest block additions only — `DIRECTIONS` already landed in Task 3)

**Interfaces:**
- Consumes: `ears.admit`, `ears.DIRECTIONS` (exactly `darker/clubbier/sparser/weirder`).
- Produces: verified gate-modifier semantics later tasks and the device rely on.

- [ ] **Step 1: Extend block 43 with direction asserts** (append inside the same block, before its `print("  OK\n")`):

```python
    _minor_room43 = _room43
    _major43 = {"id": "major", "role": "lead", "key": "C major", "tempo": 76,
                "notes": [{"pitch": 62, "start": 0.0, "duration": 1.0, "velocity": 80}]}
    _srv_dark43, _ = _ears43.admit([_fit43, _major43], _minor_room43,
                                   role="lead", direction="darker")
    assert [c["id"] for c in _srv_dark43] == ["fit"], "darker must drop major-mode picks"
    _srv_sparse43, _ = _ears43.admit([_fit43, _wall43], _minor_room43,
                                     role="lead", direction="sparser")
    assert [c["id"] for c in _srv_sparse43] == ["fit"], "sparser caps density at sparse"
    _srv_weird43, _rep_weird43 = _ears43.admit([_wall43], _minor_room43,
                                               role="lead", direction="weirder")
    assert [c["id"] for c in _srv_weird43] == ["wall"], \
        "weirder skips density/register so rare shapes get through"
    assert any(d.get("skipped") for d in _rep_weird43["dropped"])
    _srv_club43, _ = _ears43.admit([_fit43], _minor_room43,
                                   role="lead", direction="clubbier")
    assert _srv_club43 == [], "clubbier forces the club tempo world"
```

- [ ] **Step 2: Run the full suite. Expected: exit=0** (if `clubbier` unexpectedly admits, the tempo-world modifier is broken — fix `_gate_passes`, don't weaken the assert).

- [ ] **Step 3: Commit**

```bash
git add mcp_server/server.py
git commit -m "test(ears): direction words are gate modifiers with locked semantics"
```

---

### Task 5: Session memory — demote, announce, reset

**Files:**
- Modify: `mcp_server/ears.py` (append)
- Test: new named block in `mcp_server/server.py`

**Interfaces:**
- Produces (exact names Task 6/7 use):
  - `candidate_buckets(cand) -> {"density": .., "register": .., "contour": ..}`
  - `contour(notes) -> "rising" | "falling" | "flat"`
  - `load_session(path=SESSION_PATH) -> {"streaks": {}, "demoted": []}`
  - `note_verdict(state, cand, kept: bool) -> list[str]` — mutates state, returns NEW announcements
  - `save_session(state, path=SESSION_PATH)` (atomic via `core._save_json_atomic`)
  - `reset_session(path=SESSION_PATH) -> str` — deletes the file, returns the canonical confirmation
  - `DEMOTE_AFTER = 3`

- [ ] **Step 1: Write the failing probe**

```bash
.venv/bin/python - <<'PY'
import sys, tempfile, os; sys.path.insert(0, "mcp_server")
import ears
busy = {"notes": [{"pitch": 62, "start": i * 0.25, "duration": 0.2, "velocity": 80}
                  for i in range(32)]}
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "ears_session.json")
    state = ears.load_session(p)
    assert ears.note_verdict(state, busy, kept=False) == []
    assert ears.note_verdict(state, busy, kept=False) == []
    third = ears.note_verdict(state, busy, kept=False)
    assert any("easing off busy lines" in a for a in third), third
    ears.save_session(state, p)
    assert "density:busy" in ears.load_session(p)["demoted"]
    print(ears.reset_session(p))
    assert not os.path.exists(p)
print("SESSION_OK")
PY
```

Expected now: `AttributeError: module 'ears' has no attribute 'load_session'`

- [ ] **Step 2: Append to `mcp_server/ears.py`**

```python
DEMOTE_AFTER = 3

_BUCKET_PHRASES = {
    "density": {"sparse": "sparse lines", "mid": "mid-density lines",
                "busy": "busy lines"},
    "register": {"low": "the low register", "mid": "the mid register",
                 "high": "the high register"},
    "contour": {"rising": "rising lines", "falling": "falling lines",
                "flat": "static lines"},
}


def contour(notes):
    pitched = sorted((n for n in notes or [] if "pitch" in n),
                     key=lambda n: float(n["start"]))
    if len(pitched) < 2:
        return "flat"
    net = int(pitched[-1]["pitch"]) - int(pitched[0]["pitch"])
    if net > 1:
        return "rising"
    if net < -1:
        return "falling"
    return "flat"


def candidate_buckets(cand):
    notes = cand.get("notes") or []
    return {"density": _moment.density_band(notes),
            "register": _moment.register_band(notes) or "mid",
            "contour": contour(notes)}


def load_session(path=SESSION_PATH):
    if not os.path.exists(path):
        return {"streaks": {}, "demoted": []}
    data = json.load(open(path))
    if not isinstance(data, dict):
        raise ValueError("ears session file has an invalid shape")
    data.setdefault("streaks", {})
    data.setdefault("demoted", [])
    return data


def note_verdict(state, cand, kept):
    """Session memory: 3 straight rejects in a bucket demote it (never ban).
    Returns only NEW announcements; never touches the taste model."""
    announcements = []
    for dim, value in candidate_buckets(cand).items():
        bkey = "%s:%s" % (dim, value)
        if kept:
            state["streaks"][bkey] = 0
            continue
        state["streaks"][bkey] = state["streaks"].get(bkey, 0) + 1
        if state["streaks"][bkey] >= DEMOTE_AFTER and bkey not in state["demoted"]:
            state["demoted"].append(bkey)
            announcements.append("easing off %s — 'reset ears' clears it"
                                 % _BUCKET_PHRASES[dim][value])
    return announcements


def save_session(state, path=SESSION_PATH):
    import core as _core
    _core._save_json_atomic(path, state)


def reset_session(path=SESSION_PATH):
    if os.path.exists(path):
        os.unlink(path)
    return "ears reset — session demotions cleared"
```

- [ ] **Step 3: Run the probe — expect `SESSION_OK`**

- [ ] **Step 4: Add the named selftest block** (after block 43):

```python
    # 44. ears session memory: demote after 3, announce once, keep resets, reset clears
    print("--- ears session memory: 3-strikes demotion, announce-once, reset ---")
    with _tmp39.TemporaryDirectory() as _ears_td44:
        _sp44 = os.path.join(_ears_td44, "ears_session.json")
        _busy_cand44 = {"notes": [{"pitch": 62, "start": i * 0.25, "duration": 0.2,
                                   "velocity": 80} for i in range(32)]}
        _state44 = _ears43.load_session(_sp44)
        assert _ears43.note_verdict(_state44, _busy_cand44, kept=False) == []
        assert _ears43.note_verdict(_state44, _busy_cand44, kept=False) == []
        _ann44 = _ears43.note_verdict(_state44, _busy_cand44, kept=False)
        assert any("easing off busy lines — 'reset ears' clears it" == a
                   for a in _ann44), "the canonical demotion voice, exactly"
        assert _ears43.note_verdict(_state44, _busy_cand44, kept=False) == [], \
            "a demotion announces once, not every serve"
        _state44b = _ears43.load_session(_sp44)
        _ears43.note_verdict(_state44b, _busy_cand44, kept=True)
        assert _state44b["streaks"].get("density:busy") == 0, "a keep resets the streak"
        _ears43.save_session(_state44, _sp44)
        assert "density:busy" in _ears43.load_session(_sp44)["demoted"]
        _ears43.reset_session(_sp44)
        assert not os.path.exists(_sp44), "reset ears deletes the session file"
    print("  OK\n")
```

- [ ] **Step 5: Run the full suite. Expected: exit=0.**

- [ ] **Step 6: Commit**

```bash
git add mcp_server/ears.py mcp_server/server.py
git commit -m "feat(ears): session memory — demote after 3 rejects, announce once, reset"
```

---

### Task 6: `serve()` chokepoint + the canonical status line + `tools/check_ears.py`

**Files:**
- Modify: `mcp_server/ears.py` (append)
- Create: `tools/check_ears.py`
- Test: new named block in `mcp_server/server.py`

**Interfaces:**
- Produces:
  - `serve(candidates, moment_dict, *, role=None, direction=None, session_path=SESSION_PATH) -> {"served", "ears", "gate_report", "announcements"}` — blind moments bypass gates LOUDLY; survivors keep order; demoted buckets move to the back (stable).
  - `status_line(moment_dict, gate_report=None, announcements=()) -> str` — the ONE canonical ears line (device readout, chat, pages).

- [ ] **Step 1: Write the failing probe**

```bash
.venv/bin/python - <<'PY'
import sys; sys.path.insert(0, "mcp_server")
import moment, ears
m = moment.blind_moment("Live unreachable")
r = ears.serve([{"id": "x", "notes": []}], m)
assert r["ears"] == "blind" and len(r["served"]) == 1
assert any("context-blind" in a for a in r["announcements"])
line = ears.status_line(m)
assert line == "no ears — serving context-blind", line
print("SERVE_OK")
PY
```

Expected now: `AttributeError: module 'ears' has no attribute 'serve'`

- [ ] **Step 2: Append to `mcp_server/ears.py`**

```python
def serve(candidates, m, *, role=None, direction=None, session_path=SESSION_PATH):
    """The chokepoint (spec section 5). Blind bypasses gates LOUDLY; survivors
    keep their order for the caller's existing taste ranking; demoted buckets
    go to the back — demoted, never banned."""
    if m.get("ears") != "live":
        return {"served": list(candidates), "ears": "blind",
                "gate_report": {"pool": len(candidates),
                                "survived": len(candidates),
                                "dropped": [], "skipped": "blind"},
                "announcements": ["no ears — serving context-blind (%s)"
                                  % m.get("reason", "Live unreachable")]}
    survivors, report = admit(candidates, m, role=role, direction=direction)
    state = load_session(session_path)
    demoted = set(state.get("demoted", []))
    announcements = []
    if demoted and survivors:
        front, back = [], []
        for c in survivors:
            hit = {"%s:%s" % (d, v)
                   for d, v in candidate_buckets(c).items()} & demoted
            (back if hit else front).append(c)
        if back:
            announcements.append("session demotions active: %s"
                                 % ", ".join(sorted(demoted)))
        survivors = front + back
    if not survivors and report.get("nearest_relaxation"):
        announcements.append(report["nearest_relaxation"]["offer"])
    return {"served": survivors, "ears": "live", "gate_report": report,
            "announcements": announcements}


def status_line(m, gate_report=None, announcements=()):
    """The one canonical ears line — same words on the device, in chat, on
    pages. State only, never identity: no track or project names ever."""
    if m.get("ears") != "live":
        return "no ears — serving context-blind"
    head = " · ".join([
        ("%g" % m["tempo"]) if m.get("tempo") else "?",
        m.get("key") or "key ?",
        m.get("tempo_world") or "?",
    ])
    roles = m.get("roles") or {}
    room = " · ".join("%s %s" % ("drums" if r == "drum" else r,
                                 i.get("density", "?"))
                      for r, i in sorted(roles.items())) or "empty room"
    parts = [head, room]
    if gate_report and "survived" in gate_report:
        parts.append("fit: %d→%d" % (gate_report.get("pool", 0),
                                     gate_report["survived"]))
    parts.extend(announcements)
    return " | ".join(parts)
```

- [ ] **Step 3: Create `tools/check_ears.py`** (the manual live contract check, spec §9 + the real-session acceptance tool):

```python
#!/usr/bin/env python
"""Manual ears check: read the room, print the Moment and its status line.

Read-only by contract. Run with Live + Producer Pal up:
    .venv/bin/python tools/check_ears.py
    .venv/bin/python tools/check_ears.py --save tools/ideas/moment_snapshot.json
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))


def main():
    import ears
    import moment
    snap = moment.get_moment()
    print(json.dumps(snap, indent=2))
    print()
    print(ears.status_line(snap))
    if "--save" in sys.argv:
        path = sys.argv[sys.argv.index("--save") + 1]
        json.dump(snap, open(path, "w"), indent=1)
        print("saved:", path)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the probe from Step 1 — expect `SERVE_OK`.** Also run `.venv/bin/python tools/check_ears.py` — without Live it must print a blind moment and `no ears — serving context-blind`, exit 0.

- [ ] **Step 5: Add the named selftest block** (after block 44):

```python
    # 45. serve(): blind is loud, demotions reorder without banning, one voice
    print("--- ears serve: blind declaration, demote-to-back, status line ---")
    _blind45 = _moment42.blind_moment("bridge down")
    _r45 = _ears43.serve([_fit43, _wall43], _blind45)
    assert _r45["ears"] == "blind" and len(_r45["served"]) == 2, \
        "blind mode serves exactly as today — declared, not degraded"
    assert any("context-blind" in a for a in _r45["announcements"])
    assert _ears43.status_line(_blind45) == "no ears — serving context-blind"
    with _tmp39.TemporaryDirectory() as _ears_td45:
        _sp45 = os.path.join(_ears_td45, "ears_session.json")
        _st45 = _ears43.load_session(_sp45)
        for _ in range(3):
            _ears43.note_verdict(_st45, _fit43, kept=False)
        _ears43.save_session(_st45, _sp45)
        _r45b = _ears43.serve([_fit43, _wall43], _room43, role="lead",
                              session_path=_sp45)
        assert _r45b["served"], "demotion must never empty the served list"
        assert _r45b["served"][-1]["id"] == "fit", "demoted buckets go to the back"
        assert any("session demotions active" in a for a in _r45b["announcements"])
    _line45 = _ears43.status_line(_room43, _r45b["gate_report"])
    assert "fit: " in _line45 and "75" in _line45, _line45
    assert "<project-a>" not in _line45 and "<project-b>" not in _line45, \
        "state, never identity — no names on the chassis"
    print("  OK\n")
```

- [ ] **Step 6: Run the full suite. Expected: exit=0.**

- [ ] **Step 7: Commit**

```bash
git add mcp_server/ears.py mcp_server/server.py tools/check_ears.py
git commit -m "feat(ears): serve() chokepoint, canonical status line, manual room check"
```

---

### Task 7: Wire the ears into the serving tools + MCP surface

**Files:**
- Modify: `mcp_server/core.py` — add `_ears_default()`, `find_phrases_with_ears()`, and `use_ears`/`moment` parameters on `answer_my_loop`, `audition_leads`, `suggest_growth`
- Modify: `mcp_server/server.py` — pass-through params on the `find_phrases`, `answer_my_loop`, `audition_leads`, `suggest_growth` tool wrappers; new `hear_the_room` and `reset_ears` tools; selftest block
- Test: new named block in `mcp_server/server.py`

**Interfaces:**
- Consumes: `ears.serve`, `ears.status_line`, `ears.reset_session`, `moment.get_moment`, `moment.blind_moment`.
- Produces:
  - `core._ears_default() -> bool` — reads `ears_default.json` beside the taste model (`os.path.join(os.path.dirname(_taste_model_path()), "ears_default.json")`); absent file → `False`.
  - `core.find_phrases_with_ears(seed_notes=None, kind=None, k=6, key=None, tempo=None, prefer_liked=False, role=None, direction=None, moment=None) -> dict` with keys `phrases`, `ears`, `gate_report`, `announcements`, `status`. (`find_phrases` itself keeps its `list` return type — internal callers untouched.)
  - `answer_my_loop(..., use_ears=None, direction=None, moment=None)`, `audition_leads(..., use_ears=None, direction=None, moment=None)`, `suggest_growth(..., use_ears=None, direction=None, moment=None)` — `use_ears=None` means "the promoted default"; when ears run, the result dict gains `ears`, `gate_report`, `announcements`, `ears_status` keys; with ears off the result is BYTE-IDENTICAL to today (no new keys).
  - MCP tools `hear_the_room() -> {"moment", "status"}` and `reset_ears() -> {"status"}`.

- [ ] **Step 1: Write the failing probe**

```bash
.venv/bin/python - <<'PY'
import sys; sys.path.insert(0, "mcp_server")
import core, moment
r_off = core.audition_leads(bars=4, key="D minor", n=3, seed=1)
assert "ears" not in r_off, "ears off => byte-identical result, no new keys"
m = moment.build_moment(
    [{"name": "Drums", "notes": [{"pitch": 36, "start": i * 0.25, "duration": 0.2,
                                  "velocity": 100} for i in range(32)]}], tempo=75.0)
r_on = core.audition_leads(bars=4, key="D minor", n=3, seed=1,
                           use_ears=True, moment=m)
assert r_on["ears"] == "live" and "gate_report" in r_on and "ears_status" in r_on
r_blind = core.audition_leads(bars=4, key="D minor", n=3, seed=1, use_ears=True,
                              moment=moment.blind_moment("down"))
assert r_blind["ears"] == "blind"
assert core._ears_default() is False, "no flag file => ears default OFF"
d = core.find_phrases_with_ears(role="lead", k=6, moment=m)
assert set(d) >= {"phrases", "ears", "gate_report", "status"}
print("WIRING_OK")
PY
```

Expected now: `TypeError: audition_leads() got an unexpected keyword argument 'use_ears'`

- [ ] **Step 2: Implement in `mcp_server/core.py`**

Add near `_taste_model_path()`:

```python
def _ears_default() -> bool:
    """The Booth-promoted default for use_ears. Absent file => OFF (spec 6)."""
    path = os.path.join(os.path.dirname(_taste_model_path()), "ears_default.json")
    try:
        return bool(json.load(open(path)).get("use_ears_default"))
    except FileNotFoundError:
        return False


def _apply_ears(result: dict, pool_key: str, *, role, use_ears, direction, moment):
    """Shared ears hook: filter result[pool_key] through ears.serve. With ears
    off this NEVER touches the result — byte-identical behavior is a contract."""
    if use_ears is None:
        use_ears = _ears_default()
    if not use_ears:
        return result
    import ears as _ears
    import moment as _moment_mod
    m = moment if moment is not None else _moment_mod.get_moment()
    served = _ears.serve(result.get(pool_key) or [], m, role=role,
                         direction=direction)
    result[pool_key] = served["served"]
    result["ears"] = served["ears"]
    result["gate_report"] = served["gate_report"]
    result["announcements"] = served["announcements"]
    result["ears_status"] = _ears.status_line(m, served["gate_report"],
                                              served["announcements"])
    return result


def find_phrases_with_ears(seed_notes=None, kind=None, k=6, key=None, tempo=None,
                           prefer_liked=False, role=None, direction=None,
                           moment=None) -> dict:
    """Retrieval with the room in its ears — find_phrases + serve()."""
    phrases = find_phrases(seed_notes=seed_notes, kind=kind, k=max(k * 3, k),
                           key=key, tempo=tempo, prefer_liked=prefer_liked,
                           role=role)
    out = {"phrases": phrases}
    _apply_ears(out, "phrases", role=role, use_ears=True, direction=direction,
                moment=moment)
    out["phrases"] = out["phrases"][:k]
    out["status"] = out.get("ears_status", "")
    return out
```

Then on each of `answer_my_loop`, `audition_leads`, `suggest_growth`: append the three keyword-only parameters `use_ears: bool | None = None, direction: str | None = None, moment: dict | None = None` to the signature, and immediately before each function's final `return <result>` statement, wire the hook on the result's candidate list key (find each function's exact result key first — `audition_leads` returns `candidates`, `answer_my_loop` returns `candidates`, `suggest_growth` returns `suggestions`; confirm with `grep -n "return {" mcp_server/core.py` around each function and use the actual key):

```python
    return _apply_ears(result, "candidates", role="lead", use_ears=use_ears,
                       direction=direction, moment=moment)
```

(where `result` is the dict each function currently returns — bind it to a local first if it is returned inline.)

- [ ] **Step 3: Expose on the MCP surface in `mcp_server/server.py`**

Add `use_ears: bool | None = None, direction: str | None = None` to the `find_phrases`, `answer_my_loop`, `audition_leads`, `suggest_growth` tool wrappers and forward them (for `find_phrases`, route to `_core.find_phrases_with_ears(...)` when `use_ears or (use_ears is None and _core._ears_default())`, else the existing list call). Add one docstring line to each: `"With use_ears the pick is fit to the room — see hear_the_room."` Then add the two new tools (near the other small tools):

```python
@mcp.tool()
def hear_the_room() -> dict:
    """Read the room through Producer Pal: tempo world, key, who's playing,
    how busy. Read-only; if Live is unreachable this declares blind mode."""
    import ears as _ears
    import moment as _moment_mod
    m = _moment_mod.get_moment()
    return {"moment": m, "status": _ears.status_line(m)}


@mcp.tool()
def reset_ears() -> dict:
    """The 'reset ears' gesture: clear this session's keep/reject demotions."""
    import ears as _ears
    return {"status": _ears.reset_session()}
```

- [ ] **Step 4: Run the probe from Step 1 — expect `WIRING_OK`**

- [ ] **Step 5: Add the named selftest block** (after block 45):

```python
    # 46. ears wiring: off = byte-identical, on = reported, MCP surface exposes it
    print("--- ears wiring: default off, blind declared, wrappers expose params ---")
    _r_off46 = _core.audition_leads(bars=4, key="D minor", n=3, seed=1)
    assert "ears" not in _r_off46 and "gate_report" not in _r_off46, \
        "use_ears off must leave results byte-identical"
    _r_on46 = _core.audition_leads(bars=4, key="D minor", n=3, seed=1,
                                   use_ears=True, moment=_room43)
    assert _r_on46.get("ears") == "live" and "ears_status" in _r_on46
    _r_blind46 = _core.audition_leads(bars=4, key="D minor", n=3, seed=1,
                                      use_ears=True,
                                      moment=_moment42.blind_moment("down"))
    assert _r_blind46.get("ears") == "blind" and len(_r_blind46["candidates"]) == 3, \
        "blind mode must serve the full pool, declared"
    assert _core._ears_default() is False
    for _tool46 in (find_phrases, answer_my_loop, audition_leads, suggest_growth):
        _fn46 = getattr(_tool46, "fn", _tool46)
        _params46 = _inspect4.signature(_fn46).parameters
        assert "use_ears" in _params46 and "direction" in _params46, \
            "the MCP surface must expose the ears, not only core"
    for _gesture46 in (hear_the_room, reset_ears):
        assert callable(getattr(_gesture46, "fn", _gesture46))
    print("  OK\n")
```

- [ ] **Step 6: Run the full suite. Expected: exit=0.** Also re-run the ingest surfaces (they import core):

```bash
.venv/bin/python ingest/selftest_ingest.py && node tools/ideapack_panel.test.mjs
```
Expected: `ingest selftest passed`, `PASS — 30 checks passed, 0 failed`

- [ ] **Step 7: Commit**

```bash
git add mcp_server/core.py mcp_server/server.py
git commit -m "feat(ears): wire serve() into retrieval/answers/growth/audition + hear_the_room/reset_ears tools, default OFF"
```

---

### Task 8: The evidence gate — ears-vs-current Booth round builder

**Files:**
- Create: `tools/build_ears_booth.py`
- Modify: `tools/ingest_booth_votes.py` — allow `selection_mode == "ears_ab"` in `validate_booth_round_artifacts` (the `("random", "uncertain", "repeat")` tuple) and count it like `"random"` in `plan_booth_ingest` (agreement vs `predicted_winner` = the ears pick IS the promotion metric)
- Modify: `HANDOFF.md`, `ROADMAP.md` — one status line each: ears layer built, default OFF, Booth round pending
- Test: extend selftest block 39's planner asserts + a builder dry-run

**Interfaces:**
- Consumes: `core.find_phrases`, `core.export_midi(notes, filename, tempo=...)`, `ears.serve`, `moment` snapshot JSON from `tools/check_ears.py --save`, `ingest_booth_votes.booth_round_digest`, the standard `MANIFEST`/`PREDICTIONS` paths from `tools/rank_pairwise.py`.
- Produces: a standard frozen Booth round (manifest + predictions sidecar + MIDI) where every pair is `selection_mode: "ears_ab"`, `A_phrase_id` = ears pick, `B_phrase_id` = current pick, `predicted_winner` = ears pick. The existing page builder, vote flow, and ingester run unchanged.

- [ ] **Step 1: Extend the ingest contract (red first)** — in the block-39 selftest area add:

```python
    _ears_pair39 = [{"pair_id": 1, "A_phrase_id": "a", "B_phrase_id": "b",
                     "selection_mode": "ears_ab"}]
    _ears_preds39 = {"model": {}, "pairs": [
        {"pair_id": 1, "A_phrase_id": "a", "B_phrase_id": "b",
         "selection_mode": "ears_ab", "predicted_winner": "a"}]}
    _ears_digest39 = _brd39(_ears_pair39, _ears_preds39)
    _ears_preds39["artifact_digest"] = _ears_digest39
    _ears_preds39["round_id"] = "booth_" + _ears_digest39[:12]
    assert _vbra39(_ears_pair39, _ears_preds39) == _ears_preds39["round_id"], \
        "ears_ab rounds ride the same frozen-artifact contract"
    _ears_plan39 = _pbi39([{**_ears_pair39[0], "winner": "A"}], _ears_preds39,
                          lambda p: False, _ids39 | {"a", "b"})
    assert _ears_plan39["scoreboard"]["n_random"] == 1 and \
        _ears_plan39["scoreboard"]["n_agree"] == 1, \
        "ears_ab agreement counts like random pairs — that IS the promotion metric"
```

Run the full suite — expect FAIL at `_vbra39(_ears_pair39, ...)` ("invalid selection_mode").

- [ ] **Step 2: Make it pass** — in `tools/ingest_booth_votes.py`:
  - In `validate_booth_round_artifacts`, change `("random", "uncertain", "repeat")` to `("random", "uncertain", "repeat", "ears_ab")`.
  - In `plan_booth_ingest`, where `mode == "random"` increments `n_random`/`n_agree`, treat `mode in ("random", "ears_ab")` identically (and in the tie/skip counter: `mode in ("random", "ears_ab")`).

Run the full suite — expect exit=0.

- [ ] **Step 3: Write `tools/build_ears_booth.py`**

```python
#!/usr/bin/env python
"""Pre-registered ears-vs-current Booth round (spec section 6).

For N prompt seeds: A = the ears-gated pick, B = today's pick, same pool.
Pairs where they agree are skipped (nothing to learn). --silence-pairs adds
flag-gated diagnostic pairs (A = the pick, B = nothing) — diagnostic only,
never part of promotion.

Usage:
  .venv/bin/python tools/check_ears.py --save tools/ideas/moment_snapshot.json
  .venv/bin/python tools/build_ears_booth.py --moment tools/ideas/moment_snapshot.json \
      [--n 12] [--seed 7] [--silence-pairs 2] [--dry-run]
Then: build_booth_page.py -> vote -> ingest_booth_votes.py as any round.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from ingest_booth_votes import booth_round_digest  # noqa: E402
from rank_pairwise import MANIFEST, PREDICTIONS  # noqa: E402


def build_pairs(index_phrases, m, n, seed, ears_mod, core_mod):
    lead = [p for p in index_phrases if p.get("role") == "lead" and p.get("notes")]
    lead.sort(key=lambda p: p.get("id", ""))
    step = max(len(lead) // max(n, 1), 1)
    prompts = [lead[(seed + i * step) % len(lead)] for i in range(n)]
    pairs = []
    for prompt in prompts:
        pool = core_mod.find_phrases(seed_notes=prompt["notes"], role="lead",
                                     k=24, prefer_liked=True)
        if not pool:
            continue
        current = pool[0]
        served = ears_mod.serve(pool, m, role="lead")["served"]
        if not served:
            continue
        eared = served[0]
        if eared.get("id") == current.get("id"):
            continue  # nothing to learn from an identical pick
        pairs.append((eared, current))
    return pairs


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--moment", required=True,
                    help="moment snapshot from check_ears --save (a REAL room)")
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--silence-pairs", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import core
    import ears

    m = json.load(open(args.moment))
    if m.get("ears") != "live":
        sys.exit("the saved moment is blind — capture one with Live up; "
                 "a blind round would test nothing")

    idx = json.load(open(os.path.join(ROOT, "store/phrase_index.json")))
    phrases = idx if isinstance(idx, list) else idx.get("phrases", [])
    ab = build_pairs(phrases, m, args.n, args.seed, ears, core)
    if len(ab) < 5:
        sys.exit("only %d disagreeing pairs — not enough to judge; raise --n "
                 "or capture a moment with more happening in it" % len(ab))

    midi_dir = os.path.join(ROOT, "tools/ideas")
    manifest, preds = [], []
    for i, (a, b) in enumerate(ab, 1):
        a_res = core.export_midi(a["notes"], "ears_%02dA.mid" % i,
                                 tempo=float(a.get("tempo") or m["tempo"]))
        b_res = core.export_midi(b["notes"], "ears_%02dB.mid" % i,
                                 tempo=float(b.get("tempo") or m["tempo"]))
        row = {"pair_id": i, "A_phrase_id": a["id"], "B_phrase_id": b["id"],
               "selection_mode": "ears_ab",
               "A_midi": a_res.get("path") if isinstance(a_res, dict) else "",
               "B_midi": b_res.get("path") if isinstance(b_res, dict) else ""}
        manifest.append(row)
        preds.append({"pair_id": i, "A_phrase_id": a["id"],
                      "B_phrase_id": b["id"], "selection_mode": "ears_ab",
                      "predicted_winner": a["id"]})

    sidecar = {"model": {"kind": "ears_ab", "moment_digest": m.get("digest")},
               "pairs": preds}
    digest = booth_round_digest(manifest, sidecar)
    sidecar["artifact_digest"] = digest
    sidecar["round_id"] = "booth_" + digest[:12]

    print("ears A/B round %s: %d pairs (A = ears pick, blind to the voter)"
          % (sidecar["round_id"], len(manifest)))
    if args.silence_pairs:
        print("note: %d silence pairs requested — diagnostic only, "
              "add after the core round is judged" % args.silence_pairs)
    if args.dry_run:
        print("dry run — nothing written.")
        return
    json.dump(manifest, open(MANIFEST, "w"), indent=1)
    json.dump(sidecar, open(PREDICTIONS, "w"), indent=1)
    print("wrote %s + %s — next: build_booth_page.py, vote blind, ingest."
          % (os.path.relpath(MANIFEST, ROOT), os.path.relpath(PREDICTIONS, ROOT)))
    print("promotion: if the round qualifies under the scoreboard rule, write "
          "store/ears_default.json {\"use_ears_default\": true, \"round_id\": ...} "
          "— an explicit human act, never automatic.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Dry-run against a synthetic moment**

```bash
.venv/bin/python - <<'PY'
import sys, json; sys.path.insert(0, "mcp_server")
import moment
m = moment.build_moment(
    [{"name": "Drums", "notes": [{"pitch": 36, "start": i * 0.25, "duration": 0.2,
                                  "velocity": 100} for i in range(32)]}], tempo=75.0)
json.dump(m, open("/tmp/moment_test.json", "w"))
PY
.venv/bin/python tools/build_ears_booth.py --moment /tmp/moment_test.json --dry-run
```
Expected: either `ears A/B round booth_… N pairs` + `dry run — nothing written.`, or the honest `only N disagreeing pairs` refusal — both prove the pipeline; no files may be written either way.

- [ ] **Step 5: Update the docs** — `HANDOFF.md` (the "Active plan" bullet) and `ROADMAP.md` (the ear-loop section): one line each, e.g. `**Ears layer built (default OFF)** — hear_the_room/reset_ears live; promotion waits on the pre-registered ears-vs-current Booth round (tools/build_ears_booth.py).`

- [ ] **Step 6: Run everything**

```bash
.venv/bin/python mcp_server/server.py --selftest > /tmp/ears-t8.log 2>&1; echo exit=$?; tail -3 /tmp/ears-t8.log
.venv/bin/python ingest/selftest_ingest.py && node tools/ideapack_panel.test.mjs
.venv/bin/python tools/build_booth_page.py && .venv/bin/python tools/build_audition_page.py
```
Expected: all green, both pages still build against the CURRENT round (the ears round only replaces it when run without --dry-run, by the owner, with a real saved moment).

- [ ] **Step 7: Commit**

```bash
git add tools/build_ears_booth.py tools/ingest_booth_votes.py mcp_server/server.py HANDOFF.md ROADMAP.md
git commit -m "feat(ears): pre-registered ears-vs-current Booth round builder (ears_ab mode)"
```

---

## After the plan

- **Real-session acceptance (spec success criteria 4-5):** the owner runs `tools/check_ears.py` with Live up, saves a real moment, runs `build_ears_booth.py`, votes blind, ingests. Promotion = writing `store/ears_default.json` by hand after a qualifying round — never automatic.
- **Device readout (spec §7):** the M4L chassis consumes `ears.status_line()` verbatim when P3 device work resumes; nothing here blocks on it.
- **Spec §9 "moment-read contract test" mapping:** the automated layer is covered by the existing `read_clip`/read-back verification selftests plus block 42's synthetic moments; the against-real-Live contract check is `tools/check_ears.py` (manual, part of real-session acceptance). No new Live-dependent selftest — the suite stays synthetic-first by design.
- **Not built, on purpose:** development moves (Hands, phase 2), the Idea Dealer (phase 3), playing-clip detection (v1 hears session row 0 — documented in `read_moment_live`), and silence-pair page semantics (`--silence-pairs` is accepted and explicitly deferred as diagnostic until the core round is judged).
