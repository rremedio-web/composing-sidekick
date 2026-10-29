"""Core tool implementations for the Composing Sidekick MCP server (registered as "style-sidekick").

Deterministic helpers only — no LLM calls. Reuses analyze/build_index.py and
analyze/style_profile.py metrics. Imported by server.py and --selftest.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import hashlib
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable
import math

from mcp_server.approved_rhythms import approved_rhythms_data
from mcp_server.core_shared import (
    _CACHE,
    _aggregate,
    _canonical_move,
    _clip_digest,
    _groove_grid,
    _interval_buckets,
    _load_artifact,
    _load_json,
    _l2_cosine,
    _load_ratings_file,
    _motifs_data,
    _note_dicts,
    _ratings_file_lock,
    _ratings_path,
    _taste_model_path,
    _read_style_md,
    _save_json_atomic,
    _save_ratings_file,
    _save_ratings_file_unlocked,
    _validate_artifact_meta,
    _ALLOW_LEGACY_ARTIFACTS_WITHOUT_META,
    _EXPECTED_ARTIFACT_SCHEMA_VERSION,
    EXPORTS_DIR,
    HARMONY_JSON,
    INDEX_JSON,
    set_ratings_path,
    set_taste_model_path,
    MOTIFS_JSON,
    NOTES_MD,
    ROOT,
    RATINGS_JSON,
    STORE,
    STYLE_JSON,
    STYLE_MD,
    STRUCTURE_JSON,
    TASTE_MODEL_JSON,
    _write_text_atomic,
)

sys.path.insert(0, os.path.join(ROOT, "analyze"))

from build_index import (  # noqa: E402
    cosine,
    drum_vector,
    phrase_vector,
    search,
)
from corpus_keys import phrase_lookup  # noqa: E402  (pid-first id resolution)
from style_profile import (  # noqa: E402
    KNOWN_MODES,
    MODE_STEPS,
    PITCH_NAMES,
    diatonic_set,
    estimate_key,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import taste  # noqa: E402  (pure-Python preference model; never imports core)

sys.path.insert(0, os.path.join(ROOT, "tools"))
from boringness import (is_mechanical, mechanical_reason,  # noqa: E402  junk FLOOR only — never a score term
                         phrase_to_degrees)

# ONE module universe regardless of import style ('core' vs 'mcp_server.core'):
# caches and test overrides must never fork across two instances (proven live by
# selftest block 31 reaching the real index through a second core).
for _alias in ("core", "mcp_server.core"):
    sys.modules.setdefault(_alias, sys.modules[__name__])

# Taste bridge facade (slice B): taste/ratings/gold/ears-retrieval code lives in
# mcp_server/taste_bridge.py — sole owner. These names stay reachable as core._name
# for server.py and the selftest. The bridge imports nothing from core at module
# load (only a lazy `score_phrase` inside _taste_features), so this is cycle-free.
from mcp_server.taste_bridge import (
    _idempotency_fingerprint,
    _require_matching_idempotency,
    _GOLD_UNMARK_LABELS,
    get_gold_phrases,
    _TASTE_FEATURE_NAMES,
    _TASTE_MIN_PAIRS,
    _TASTE_FULL_PAIRS,
    _CANDIDATE_SCHEMA_VERSION,
    _CONTRAST_AXES,
    _TRAINABLE_MODELS_BY_SCOPE,
    _ratings_file_hash,
    _taste_model,
    _taste_features,
    _candidate_notes,
    _candidate_role,
    _candidate_key,
    _candidate_taste_trainable,
    _normalise_rating_candidate,
    _candidate_features,
    _valid_candidate_index,
    _role_rounds,
    _taste_pairs_from_rounds,
    _taste_pairs_from_golds,
    _taste_fit,
    _taste_loo_accuracy,
    _retrain_taste,
    _stale_retrain_report,
    _taste_weight,
    _taste_score,
    rate_candidates,
    _normalize_rating,
    _rating_vectors,
    _rerank_by_ratings,
    _taste_model_summary,
    get_ratings,
    _normalize_role,
    _filter_phrases,
    _diverse_exemplars,
    _phrase_result,
    _latest_rating_by_identity,
    rejected_phrases,
    reject_cull,
    served_not_kept_identities,
    _demote_to_back,
    _retrieve_phrases,
    find_phrases_with_ears,
    _phrases,
    _project_keys,
    _ears_default,
    _apply_ears,
    _serve_indexed,
    _gate_backing,
    _candidate_source,
    _normalise_contrast_axis,
    _rating_scope_for_contrast,
    _trainable_model_for_scope,
)

from config import (  # noqa: E402
    BEATS_PER_BAR,
    PP_URL,
    ROLE_SOURCE,
    ROLE_THRESHOLDS,
    ROLES,
    SECTION_BUCKETS,
    _DENSITY_PARAMS,
    _DROP_ENERGY,
    _DRUM_DENSITY_TARGETS,
    _ROLE_ENERGY_THRESHOLD,
    _ROLE_LAYER_ORDER,
)

_LEAD_CANDIDATE_COUNT = 12
# Lead generation: the gesture-vocabulary engine is parked behind this flag — Ricardo
# preferred the smoother Markov lead by ear. The gesture engine + vocabulary remain the
# substrate for the influence-blend (task #29); flip True to A/B the gesture lead.
_USE_GESTURE_LEAD = False


def set_phrase_pool_for_test(pool: list[dict] | None) -> None:
    """Selftest hook: override the phrase-index pool (None restores the real index).

    The pool global lives in mcp_server.taste_bridge (sole owner since the
    slice-B extraction); this forwards so existing callers keep working."""
    from mcp_server import taste_bridge

    taste_bridge._PHRASE_POOL_OVERRIDE = pool


_EARS_DIRECTION_ALIASES = {
    # the gearbox speaks a wider dialect; only these words bend the gates
    "darker": "darker", "clubbier": "clubbier", "club": "clubbier",
    "sparser": "sparser", "sparse": "sparser", "weirder": "weirder",
}


# The index stamps the singular 'drum'; arrangement code speaks 'drums'. Accept both at the
# retrieval boundary so find_phrases(role="drums") stops silently returning nothing.
_ROLE_ALIASES = {"drums": "drum", "percussion": "drum"}


# --- public tools -----------------------------------------------------------

def get_style() -> dict:
    """Return STYLE.md + structured facts from profile/harmony/structure JSON."""
    agg = _aggregate()
    harmony = _harmony_data()
    structure = _structure_data()
    struct_agg = structure.get("aggregate", {})

    return {
        "style_md": _read_style_md(),
        "structured": {
            "keys": agg.get("key_distribution", [])[:8],
            "modes": agg.get("mode_distribution", []),
            "tonics": agg.get("tonic_distribution", [])[:6],
            "tempo": agg.get("tempo"),
            "melody_shape": agg.get("melody_shape"),
            "top_intervals": agg.get("top_melodic_intervals", [])[:6],
            "harmonic_groove": agg.get("harmonic_groove"),
            "drum_groove": agg.get("drum_groove"),
            "avg_scale_conformity": agg.get("avg_scale_conformity"),
            "avg_polyphony": agg.get("avg_polyphony"),
            "pitch_register": agg.get("pitch_register"),
            "duration_mix": agg.get("duration_distribution", [])[:5],
            "top_progressions": harmony.get("progressions", [])[:8],
            "roman_numerals": harmony.get("roman_numerals", [])[:8],
            "harmony_narrative": harmony.get("narrative", []),
            "arrangement": {
                "n_arranged": structure.get("n_arranged"),
                "section_length_histogram": struct_agg.get("section_length_histogram"),
                "sections_per_song_mean": struct_agg.get("sections_per_song", {}).get("mean"),
                "avg_song_minutes": struct_agg.get("song_length", {}).get("avg_minutes"),
                "energy_arc": struct_agg.get("energy_arc", {}).get("points"),
                "breakdown_pct": struct_agg.get("breakdown_pct"),
                "structure_narrative": structure.get("narrative", []),
            },
            "approved_rhythms": approved_rhythms_data(),
        },
    }


def find_phrases(
    seed_notes: list[dict] | None = None,
    kind: str | None = None,
    k: int = 6,
    key: str | None = None,
    tempo: float | None = None,
    prefer_liked: bool = False,
    role: str | None = None,
) -> list[dict]:
    """Retrieve diverse real phrases from the phrase index.

    Pass ``role`` (e.g. 'lead') as the primary filter to restrict to that role's phrases — ``kind``
    is then inferred. ``kind``: 'melodic'/'harmonic' (pitched) or 'drum'."""
    return _retrieve_phrases(seed_notes=seed_notes, kind=kind, k=k, key=key,
                             tempo=tempo, prefer_liked=prefer_liked, role=role)


def _section_energy(progress: float, role: str, corpus_energy: float) -> float:
    """A breathing energy arc: low intro → rise → late peak (~62%) → ease down, with
    breakdown/outro dips. Tilted 35% by his learned corpus arc so it keeps his
    character but actually moves (the raw corpus arc averages flat + front-loaded)."""
    peak_at = 0.62
    if progress <= peak_at:
        shape = 0.40 + 0.50 * (progress / peak_at)
    else:
        shape = 0.90 - 0.45 * ((progress - peak_at) / max(1e-6, 1.0 - peak_at))
    energy = 0.65 * shape + 0.35 * corpus_energy
    if role == "intro":
        energy = min(energy, 0.38)
    elif role == "outro":
        energy = min(energy, 0.42)
    elif role == "breakdown":
        energy = min(energy, 0.25)
    return round(max(0.12, min(1.0, energy)), 3)


def suggest_structure(
    target_minutes: float | None = None,
    tempo: float | None = None,
    seed: int = 0,
) -> dict:
    """Propose a section skeleton from learned arrangement templates.

    Section lengths are SAMPLED from his section-length histogram (seeded,
    deterministic) rather than always taking the most-common bucket — his real
    records are mostly-8s with 16s and 4s breaking the grid, and a greedy picker
    was flattening that variance into 8-8-8-8 ("true to the median, false to the
    variance")."""
    structure = _structure_data()
    agg = structure.get("aggregate", {})
    profile = _aggregate()
    tempo = tempo or profile.get("tempo", {}).get("mean") or 120.0

    if target_minutes:
        total_bars = max(8, int(round(target_minutes * tempo / BEATS_PER_BAR)))
    else:
        total_bars = max(8, int(round(agg.get("song_length", {}).get("avg_bars", 64))))

    hist = agg.get("section_length_histogram", {})
    # 32/64-bar histogram entries are mostly loop-project artifacts (a whole loop
    # recorded as one "section"), and raw 2-bar mass reads as fills, not the grid —
    # so sections sample from musical lengths 2..16 with the 2s damped.
    length_weights = {b: float(hist.get(str(b), 0)) for b in (2, 4, 8, 16)}
    length_weights[2] *= 0.3
    rng = random.Random(f"structure:{seed}")

    arc = agg.get("energy_arc", {}).get("points") or [0.5] * 16
    breakdown_pct = agg.get("breakdown_pct", 0)
    breakdown_idx = len(arc) // 3 if breakdown_pct >= 30 else None
    breakdown_done = False

    sections: list[dict] = []
    bars_used = 0
    idx = 0
    while bars_used < total_bars:
        remaining = total_bars - bars_used
        fits = [b for b in length_weights if b <= remaining]
        if fits and any(length_weights[b] > 0 for b in fits):
            length = rng.choices(fits, weights=[max(length_weights[b], 1e-4) for b in fits])[0]
        elif fits:
            length = max(fits)
        else:
            length = remaining
        if length <= 0:
            length = remaining

        progress = bars_used / total_bars if total_bars else 0
        arc_i = min(int(progress * (len(arc) - 1)), len(arc) - 1)
        corpus_energy = arc[arc_i]
        role = "section"
        if bars_used == 0:
            role = "intro"
        elif bars_used + length >= total_bars:
            role = "outro"
        elif (breakdown_idx is not None and not breakdown_done
              and arc_i >= breakdown_idx):
            # fire when the song CROSSES the breakdown point — an exact-index match
            # only worked when every section was the same length
            role = "breakdown"
            breakdown_done = True

        energy = _section_energy(progress, role, corpus_energy)
        density = max(1, round(energy * 10))
        if role == "breakdown":
            density = max(1, round(density * 0.5))

        sections.append({
            "index": idx + 1,
            "bars": length,
            "bar_start": bars_used,
            "bar_end": bars_used + length,
            "density": density,
            "energy": round(energy, 3),
            "role": role,
        })
        bars_used += length
        idx += 1

    return {
        "tempo": tempo,
        "total_bars": total_bars,
        "total_minutes": round(total_bars * BEATS_PER_BAR / tempo, 2),
        "breakdown_likely": breakdown_pct >= 30,
        "sections": sections,
        "template_notes": structure.get("narrative", [])[:3],
    }


_MAP_VOCAB = ("INTRO", "BUILD", "DROP", "BREATHE", "REPRISE", "MAIN", "OUT", "END")


def _map_section_names(sections: list[dict]) -> list[str]:
    """Friendly locator names in the fixed MAP vocabulary, derived from roles + the
    energy arc: the peak-energy section is the DROP, the one before it the BUILD,
    breakdowns BREATHE, the first post-BREATHE section back near peak energy the
    REPRISE; everything else is MAIN n. Deterministic, no rng."""
    if not sections:
        return []
    peak_i = max(range(len(sections)), key=lambda i: sections[i]["energy"])
    names: list[str] = []
    main_n = 0
    seen_breathe = False
    reprise_done = False
    for i, sec in enumerate(sections):
        role = sec.get("role", "section")
        if role == "intro":
            names.append("INTRO")
        elif role == "outro":
            names.append("OUT")
        elif role == "breakdown":
            names.append("BREATHE")
            seen_breathe = True
        elif i == peak_i:
            names.append("DROP")
        elif i == peak_i - 1:
            names.append("BUILD")
        elif (seen_breathe and not reprise_done
              and sec["energy"] >= 0.85 * sections[peak_i]["energy"]):
            names.append("REPRISE")
            reprise_done = True
        else:
            main_n += 1
            names.append(f"MAIN {main_n}")
    return names


def plan_song_map(
    seed_notes: list[dict] | None = None,
    key: str | None = None,
    tempo: float | None = None,
    target_minutes: float | None = None,
    seed: int = 0,
) -> dict:
    """THE MAP, locators-first: a full-song section plan in HIS proportions (learned
    section lengths, breathing energy arc, breakdown placement) named in the fixed
    vocabulary INTRO/BUILD/DROP/BREATHE/REPRISE/MAIN/OUT + a terminal END marker.
    Returns locator payloads only — nothing is written by this function, and a later
    commit derives the identical skeleton (suggest_structure is deterministic)."""
    skeleton = suggest_structure(target_minutes=target_minutes, tempo=tempo, seed=seed)
    names = _map_section_names(skeleton["sections"])
    assumptions: list[str] = []
    key_str = key
    if seed_notes and not key_str:
        nd = _note_dicts(seed_notes)
        pc_hist = [0.0] * 12
        for n in nd:
            pc_hist[n["pitch"] % 12] += 1
        t_d, m_d, _ = estimate_key(pc_hist)
        key_str = f"{t_d} {m_d}"
        assumptions.append(f"key {key_str} detected from the seed loop — pass key= to pin it")

    sections = []
    for sec, name in zip(skeleton["sections"], names):
        out = dict(sec)
        out["name"] = name
        out["locator_time"] = f"{sec['bar_start'] + 1}|1"
        sections.append(out)

    return {
        "sections": sections,
        "locators": ([{"time": s["locator_time"], "name": s["name"]} for s in sections]
                     + [{"time": f"{skeleton['total_bars'] + 1}|1", "name": "END"}]),
        "total_bars": skeleton["total_bars"],
        "total_minutes": skeleton.get("total_minutes"),
        "tempo": skeleton["tempo"],
        "key": key_str,
        "breakdown_likely": skeleton.get("breakdown_likely"),
        "assumptions": assumptions,
        "hint": "locators only — drag any marker you disagree with; commit fills sections on fresh tracks",
    }


def _role_energy_threshold(role: str) -> float:
    return _ROLE_ENERGY_THRESHOLD.get(role.lower(), 0.50)


def _active_roles_for_section(
    section: dict,
    available_roles: set[str],
) -> list[str]:
    """Pick which loop roles play in a section from energy + structural role."""
    energy = float(section.get("energy", 0.5))
    sec_role = section.get("role", "section")

    if sec_role == "breakdown":
        active = {r for r in ("pad", "bass") if r in available_roles}
        if not active and "pad" in available_roles:
            active = {"pad"}
        return sorted(active, key=lambda r: _ROLE_LAYER_ORDER.get(r, 99))

    if sec_role == "section" and energy >= _DROP_ENERGY:
        return sorted(available_roles, key=lambda r: _ROLE_LAYER_ORDER.get(r, 99))

    active = {
        r for r in available_roles
        if energy >= _role_energy_threshold(r)
    }

    if sec_role == "intro":
        sparse = {"pad", "bass", "chords"} & available_roles
        active &= sparse
        if energy < 0.50:
            active &= {"pad", "bass"}
    elif sec_role == "outro":
        thin = {"pad", "bass"} & available_roles
        active &= thin
        if energy < 0.45:
            active = {"pad"} & available_roles
    elif sec_role == "section" and energy < 0.40:
        active &= {"pad", "bass", "chords"}

    if not active:
        fallback = "pad" if "pad" in available_roles else min(
            available_roles,
            key=lambda r: _ROLE_LAYER_ORDER.get(r, 99),
        )
        active = {fallback}

    return sorted(active, key=lambda r: _ROLE_LAYER_ORDER.get(r, 99))


# --- development engine (theme & variations) --------------------------------

def _notes_to_mcp(nd: list[dict]) -> list[dict]:
    """Internal note dicts → MCP {pitch, start_beats, duration_beats, velocity}."""
    return [
        {
            "pitch": int(n["pitch"]),
            "start_beats": float(n["start"]),
            "duration_beats": float(n["duration"]),
            "velocity": int(n["velocity"]),
        }
        for n in nd
    ]


def _normalize_phrase(nd: list[dict]) -> list[dict]:
    """Rebase start to 0 and sort; drop zero-duration notes."""
    if not nd:
        return []
    t0 = min(n["start"] for n in nd)
    out = []
    for n in nd:
        dur = max(float(n["duration"]), 0.01)
        out.append({
            "pitch": int(n["pitch"]),
            "start": round(float(n["start"]) - t0, 6),
            "duration": dur,
            "velocity": int(n.get("velocity", 80)),
        })
    out.sort(key=lambda x: (x["start"], x["pitch"]))
    return out


def _phrase_span(nd: list[dict]) -> float:
    if not nd:
        return 0.0
    return max(n["start"] + n["duration"] for n in nd)


def _pitch_class_set(nd: list[dict]) -> set[int]:
    return {n["pitch"] % 12 for n in nd}


def _skyline_cosine(a: list[dict], b: list[dict]) -> float:
    """Cosine similarity of skyline interval-bucket vectors (0..1)."""
    return _l2_cosine(_interval_buckets(a), _interval_buckets(b))


def _notes_exactly_equal(a: list[dict], b: list[dict]) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(
        sorted(a, key=lambda n: (n["start"], n["pitch"])),
        sorted(b, key=lambda n: (n["start"], n["pitch"])),
    ):
        if (
            x["pitch"] != y["pitch"]
            or abs(x["start"] - y["start"]) > 1e-5
            or abs(x["duration"] - y["duration"]) > 1e-5
        ):
            return False
    return True


def _transpose_notes(nd: list[dict], semitones: int) -> list[dict]:
    return [
        {**n, "pitch": n["pitch"] + semitones}
        for n in nd
    ]


def _time_scale_notes(nd: list[dict], factor: float) -> list[dict]:
    return [
        {
            **n,
            "start": round(n["start"] * factor, 6),
            "duration": max(round(n["duration"] * factor, 6), 0.01),
        }
        for n in nd
    ]


def _thin_notes(nd: list[dict]) -> list[dict]:
    """Skeleton: one structurally important note per beat (downbeat / longest)."""
    beats: dict[int, tuple[dict, float]] = {}
    for n in nd:
        beat = int(round(n["start"]))
        is_down = abs(n["start"] - beat) < 0.06
        score = (3.0 if is_down else 0.0) + n["duration"]
        if beat not in beats or score > beats[beat][1]:
            beats[beat] = (n, score)
    return [beats[b][0] for b in sorted(beats)]


def _octave_variant(nd: list[dict], semitones: int) -> list[dict]:
    """Transpose + terminal turn so skyline tail differs but contour DNA remains."""
    if not nd:
        return []
    out = _transpose_notes(nd, semitones)
    last = max(out, key=lambda n: (n["start"], n["pitch"]))
    out = [n for n in out if n is not last]
    tail_dur = max(0.01, last["duration"] * 0.55)
    out.append({**last, "duration": tail_dur})
    out.append({
        "pitch": last["pitch"] + 2,
        "start": round(last["start"] + tail_dur, 6),
        "duration": 0.25,
        "velocity": max(42, last["velocity"] - 10),
    })
    out.sort(key=lambda x: (x["start"], x["pitch"]))
    return out


def _half_time_notes(nd: list[dict]) -> list[dict]:
    """Augmentation ×2 with one interior rest-point removed so skyline shifts."""
    scaled = sorted(
        _time_scale_notes(nd, 2.0),
        key=lambda n: (n["start"], n["pitch"]),
    )
    if len(scaled) > 3:
        scaled.pop(len(scaled) // 2)
    return scaled


def _double_time_notes(nd: list[dict]) -> list[dict]:
    """Diminution ×0.5 with cadential trim so skyline tail differs."""
    scaled = _time_scale_notes(nd, 0.5)
    if len(scaled) <= 2:
        return scaled
    return sorted(scaled, key=lambda n: (n["start"], n["pitch"]))[:-1]


def _time_variant(nd: list[dict], factor: float) -> list[dict]:
    """Legacy alias — prefer _half_time_notes / _double_time_notes."""
    if factor >= 1.5:
        return _half_time_notes(nd)
    return _double_time_notes(nd)


def _thicken_notes(nd: list[dict]) -> list[dict]:
    """Ornament: ~+40% density — neighbour on long notes, passing tone on large leaps only."""
    out = [dict(n) for n in nd]
    candidates: list[dict] = []

    for n in nd:
        if n["duration"] < 0.75:
            continue
        nt = round(n["start"] + n["duration"] * 0.6, 6)
        neighbour = n["pitch"] + (2 if (n["pitch"] % 12) in (0, 2, 5, 7, 9) else -1)
        candidates.append({
            "pitch": neighbour,
            "start": nt,
            "duration": min(0.25, max(0.01, n["duration"] * 0.35)),
            "velocity": max(45, n["velocity"] - 15),
            "_prio": n["duration"],
        })

    top: dict[int, dict] = {}
    for n in nd:
        slot = round(n["start"] * 4)
        if slot not in top or n["pitch"] > top[slot]["pitch"]:
            top[slot] = n
    seq = [top[k] for k in sorted(top)]
    for a, b in zip(seq, seq[1:]):
        gap = b["pitch"] - a["pitch"]
        if abs(gap) < 4:
            continue
        mid_start = (a["start"] + a["duration"] + b["start"]) / 2.0
        step = 2 if abs(gap) >= 5 else 1
        mid_pitch = a["pitch"] + (step if gap > 0 else -step)
        candidates.append({
            "pitch": mid_pitch,
            "start": round(mid_start, 6),
            "duration": 0.25,
            "velocity": max(45, min(a["velocity"], b["velocity"]) - 12),
            "_prio": abs(gap),
        })

    max_add = max(1, int(round(len(nd) * 0.4)))
    candidates.sort(key=lambda c: c["_prio"], reverse=True)
    for c in candidates[:max_add]:
        out.append({k: v for k, v in c.items() if k != "_prio"})
    out.sort(key=lambda x: (x["start"], x["pitch"]))
    return out


def _drum_fill_notes(nd: list[dict], loop_bars: int = 4) -> list[dict]:
    """Add a one-bar fill in the last bar of the loop (build / pre-drop energy)."""
    span = _phrase_span(nd)
    if span <= 0:
        return list(nd)
    last_bar_start = max(0.0, span - BEATS_PER_BAR)
    if loop_bars > 1 and span >= BEATS_PER_BAR:
        last_bar_start = (loop_bars - 1) * BEATS_PER_BAR

    pitches = [n["pitch"] for n in nd]
    kick = min(pitches) if pitches else 36
    snare = next((p for p in sorted(set(pitches)) if p > kick + 1), kick + 2)

    out = [dict(n) for n in nd]
    fill_times = (
        last_bar_start + 2.5,
        last_bar_start + 3.0,
        last_bar_start + 3.5,
        last_bar_start + 3.75,
    )
    for i, t in enumerate(fill_times):
        if t >= span - 1e-6:
            continue
        out.append({
            "pitch": snare if i % 2 else kick,
            "start": round(t, 6),
            "duration": 0.25,
            "velocity": max(70, 100 - i * 4),
        })
    out.sort(key=lambda x: (x["start"], x["pitch"]))
    return out


def _fragment_notes(nd: list[dict], motif_bars: int = 2) -> list[dict]:
    """First 1–2 bars as motif, repeated/sequenced to fill the phrase span."""
    span = _phrase_span(nd)
    if span <= 0:
        return list(nd)
    motif_end = span / 2 if span <= BEATS_PER_BAR else min(BEATS_PER_BAR, span)
    motif_end = max(motif_end, 0.5)
    motif = [n for n in nd if n["start"] < motif_end - 1e-6]
    if not motif:
        motif = list(nd)
        motif_end = _phrase_span(motif) or BEATS_PER_BAR
    seq_offsets = (0, -5, -7, 0)
    out: list[dict] = []
    t = 0.0
    rep = 0
    while t < span - 1e-6:
        semis = seq_offsets[rep % len(seq_offsets)]
        for n in motif:
            ns = n["start"] + t
            if ns >= span - 1e-6:
                break
            out.append({
                "pitch": n["pitch"] + semis,
                "start": round(ns, 6),
                "duration": max(0.01, min(n["duration"], span - ns)),
                "velocity": n["velocity"],
            })
        t += motif_end
        rep += 1
    return out


def _call_response_notes(nd: list[dict]) -> list[dict]:
    """State in first half; varied answer (octave lift) in second half."""
    span = _phrase_span(nd)
    if span <= 0:
        return list(nd)
    mid = span / 2.0
    call = [n for n in nd if n["start"] < mid - 1e-6]
    response: list[dict] = []
    for n in nd:
        if n["start"] < mid - 1e-6:
            continue
        response.append({
            "pitch": n["pitch"] + 12,
            "start": round(n["start"] + mid * 0.02, 6),
            "duration": max(0.01, n["duration"] * 0.92),
            "velocity": min(127, n["velocity"] + 6),
        })
    return sorted(call + response, key=lambda x: (x["start"], x["pitch"]))


def _harmony_data() -> dict:
    if "harmony" not in _CACHE:
        if os.path.exists(HARMONY_JSON):
            _CACHE["harmony"] = _load_artifact(HARMONY_JSON, "harmony.json")
        else:
            _CACHE["harmony"] = {}
    return _CACHE["harmony"]


def _structure_data() -> dict:
    if "structure" not in _CACHE:
        if os.path.exists(STRUCTURE_JSON):
            _CACHE["structure"] = _load_artifact(STRUCTURE_JSON, "structure.json")
        else:
            _CACHE["structure"] = {}
    return _CACHE["structure"]


def _roman_chord_pcs(figure: str, tonic: str, mode: str) -> list[int]:
    from music21 import key as m21key, roman

    k = m21key.Key(tonic, mode)
    rn = roman.RomanNumeral(figure, k)
    return sorted({p.pitchClass for p in rn.pitches})


def _reharmonize_pair() -> tuple[str, str]:
    """Pick a signature progression swap from harmony.json (default VI↔iv)."""
    harmony = _harmony_data()
    for prog, _ in harmony.get("progressions", []):
        if "→" in prog:
            a, b = prog.split("→", 1)
            if a and b and a != b:
                return a.strip(), b.strip()
    return "VI", "iv"


def _map_pitch_to_chord(pitch: int, src_pcs: list[int], tgt_pcs: list[int]) -> int:
    src = sorted(set(src_pcs))
    tgt = sorted(set(tgt_pcs))
    if not src or not tgt:
        return pitch
    pc = pitch % 12
    if pc in src:
        new_pc = tgt[src.index(pc) % len(tgt)]
    else:
        nearest_src = min(src, key=lambda s: min(abs(s - pc), 12 - abs(s - pc)))
        new_pc = tgt[src.index(nearest_src) % len(tgt)]
    octave = pitch // 12
    new_pitch = octave * 12 + new_pc
    while new_pitch < pitch - 7:
        new_pitch += 12
    while new_pitch > pitch + 7:
        new_pitch -= 12
    return new_pitch


def _reharmonize_notes(nd: list[dict]) -> list[dict]:
    """Re-fit phrase to an alternate chord from Ricardo's progressions (e.g. VI→iv)."""
    pc_hist = [0.0] * 12
    for n in nd:
        pc_hist[n["pitch"] % 12] += 1
    tonic, mode, _ = estimate_key(pc_hist)
    src_fig, tgt_fig = _reharmonize_pair()
    src_pcs = _roman_chord_pcs(src_fig, tonic, mode)
    tgt_pcs = _roman_chord_pcs(tgt_fig, tonic, mode)
    return [
        {**n, "pitch": _map_pitch_to_chord(n["pitch"], src_pcs, tgt_pcs)}
        for n in nd
    ]


def _retrograde_notes(nd: list[dict]) -> list[dict]:
    """Reverse the pitch sequence over the original rhythm/velocity grid.

    Notes are ordered by (start, pitch); each output slot keeps its input's
    (start, duration, velocity) while taking the pitch from the mirrored slot.
    Pitch multiset identical to input; zero snapping. Copies, never mutation."""
    if not nd:
        return []
    ordered_in = sorted((dict(n) for n in nd), key=lambda n: (n["start"], n["pitch"]))
    n_notes = len(ordered_in)
    if n_notes <= 1:
        return ordered_in
    return [
        {**note, "pitch": ordered_in[n_notes - 1 - i]["pitch"]}
        for i, note in enumerate(ordered_in)
    ]


def _invert_motif_notes(nd: list[dict], key_str: str | None = None) -> list[dict]:
    """Contour inversion around the first note's pitch, snapped to the legal set (spec §4.3).

    Notes are ordered by (start, pitch); axis = first pitch; each output slot keeps
    its input's (start, duration, velocity) while pitch becomes
    ``2 * axis - pitch`` then nearest-PC snap onto ``_legal_degree_set``. When the
    plain inversion does not already end on degree 0 or 7, an alternate candidate
    shifts all pitches by the smallest semitone move landing the skyline's final
    degree on the nearer of {0, 7}, then re-snaps; between the two, prefer
    ``analyze/motifs.classify == 'dark_landing'``. If neither qualifies, keep plain.

    ``key_str`` supplies tonic; when None, tonic is self-derived from nd's pitch-class
    histogram (slow-burn:add / motivic:fragment pattern).

    Identity fallback (spec §4.3): not implemented as a separate ``motivic:identity``
    label — snap-to-legal construction guarantees skyline degrees ⊆ legal set; an
    ``assert`` enforces the subset property instead of returning unchanged notes."""
    if not nd:
        return []
    ordered_in = sorted((dict(n) for n in nd), key=lambda n: (n["start"], n["pitch"]))
    if len(ordered_in) <= 1:
        return ordered_in

    from motif_scan import skyline  # lazy: analyze/ on sys.path (core.py:29)
    from motifs import _intervals, _wrap, classify

    if key_str is None:
        pc_hist = [0.0] * 12
        for note in nd:
            pc_hist[note["pitch"] % 12] += 1
        tonic_d, mode_d, _ = estimate_key(pc_hist)
        key_str = f"{tonic_d} {mode_d}"
    tonic, _mode = _parse_key_string(key_str)
    tonic_pc = PITCH_NAMES.index(tonic)
    legal = _legal_degree_set(nd, tonic_pc)
    axis = ordered_in[0]["pitch"]

    def _build(pitch_shift: int = 0) -> list[dict]:
        out: list[dict] = []
        for n in ordered_in:
            inv_pitch = 2 * axis - n["pitch"] + pitch_shift
            out.append({
                "pitch": _snap_to_legal(inv_pitch, tonic_pc, legal),
                "start": n["start"],
                "duration": n["duration"],
                "velocity": n["velocity"],
            })
        return out

    def _skyline_degs(notes: list[dict]) -> tuple[int, ...]:
        return tuple((p - tonic_pc) % 12 for p in skyline(notes))

    def _classify_notes(notes: list[dict]) -> str:
        degs = _skyline_degs(notes)
        if len(degs) < 2:
            return "repeat"
        return classify(degs, _intervals(degs))

    plain = _build()
    winner = plain
    plain_degs = _skyline_degs(plain)
    if plain_degs and plain_degs[-1] not in (0, 7):
        last_deg = plain_degs[-1]
        to0 = _wrap(0 - last_deg)
        to7 = _wrap(7 - last_deg)
        shift = to0 if abs(to0) <= abs(to7) else to7
        shifted = _build(pitch_shift=shift)
        if _classify_notes(shifted) == "dark_landing" and _classify_notes(plain) != "dark_landing":
            winner = shifted

    win_deg_set = set(_skyline_degs(winner))
    assert win_deg_set <= legal, (
        f"inversion skyline degrees {win_deg_set} not subset of legal {legal}")
    return winner


def _fragment_harmonic_notes(nd: list[dict], key_str: str | None = None) -> list[dict]:
    """Motif-repeat skeleton with his-harmony mapping per repetition (spec §4.2).

    Uses the same motif_end / repeat / span-fill / velocity / duration-clip
    structure as ``_fragment_notes``. Per repetition k: k=0 identity; odd k
    maps each motif pitch through ``_map_pitch_to_chord`` src→tgt; even k>0
    maps tgt→src. Chords from ``_reharmonize_pair`` / ``_roman_chord_pcs``.
    Every mapped pitch is snapped onto ``_legal_degree_set``. ``key_str``
    supplies tonic/mode; when None, tonic is self-derived from nd's pitch-class
    histogram (slow-burn:add pattern)."""
    span = _phrase_span(nd)
    if span <= 0:
        return list(nd)
    motif_end = span / 2 if span <= BEATS_PER_BAR else min(BEATS_PER_BAR, span)
    motif_end = max(motif_end, 0.5)
    motif = [n for n in nd if n["start"] < motif_end - 1e-6]
    if not motif:
        motif = list(nd)
        motif_end = _phrase_span(motif) or BEATS_PER_BAR

    if key_str is None:
        pc_hist = [0.0] * 12
        for note in nd:
            pc_hist[note["pitch"] % 12] += 1
        tonic_d, mode_d, _ = estimate_key(pc_hist)
        key_str = f"{tonic_d} {mode_d}"
    tonic, mode = _parse_key_string(key_str)
    tonic_pc = PITCH_NAMES.index(tonic)
    legal = _legal_degree_set(nd, tonic_pc)
    src_fig, tgt_fig = _reharmonize_pair()
    src_pcs = _roman_chord_pcs(src_fig, tonic, mode)
    tgt_pcs = _roman_chord_pcs(tgt_fig, tonic, mode)

    out: list[dict] = []
    t = 0.0
    rep = 0
    while t < span - 1e-6:
        for n in motif:
            ns = n["start"] + t
            if ns >= span - 1e-6:
                break
            pitch = n["pitch"]
            if rep == 0:
                mapped = pitch
            elif rep % 2 == 1:
                mapped = _map_pitch_to_chord(pitch, src_pcs, tgt_pcs)
            else:
                mapped = _map_pitch_to_chord(pitch, tgt_pcs, src_pcs)
            out.append({
                "pitch": _snap_to_legal(mapped, tonic_pc, legal),
                "start": round(ns, 6),
                "duration": max(0.01, min(n["duration"], span - ns)),
                "velocity": n["velocity"],
            })
        t += motif_end
        rep += 1
    return out


_WALK_OFFSETS = (0, 3, 5, 7, 10)


def _alphabet_walk_offsets(walk_seed: int) -> tuple[int, ...]:
    """Rotate (0,+3,+5,+7,+10) by walk_seed % 5; negate all when (walk_seed // 5) % 2 == 1."""
    rot = walk_seed % 5
    walk = _WALK_OFFSETS[rot:] + _WALK_OFFSETS[:rot]
    if (walk_seed // 5) % 2 == 1:
        return tuple(-x for x in walk)
    return walk


def _alphabet_walk_notes(nd: list[dict], key_str: str | None = None,
                         walk_seed: int = 0) -> list[dict]:
    """Motif-repeat skeleton with alphabet-degree walks (spec §4.5).

    Same motif_end / repeat / span-fill / velocity / duration-clip structure
    as ``_fragment_notes``. Repetition k transposes the motif by the k-th
    element of a deterministic walk through (0, +3, +5, +7, +10): rotate the
    tuple by walk_seed % 5; negate all offsets (descending walk) when
    (walk_seed // 5) % 2 == 1. Every pitch is snapped onto
    ``_legal_degree_set``. ``key_str`` supplies tonic; when None, tonic is
    self-derived from nd (slow-burn:add pattern)."""
    span = _phrase_span(nd)
    if span <= 0:
        return list(nd)
    motif_end = span / 2 if span <= BEATS_PER_BAR else min(BEATS_PER_BAR, span)
    motif_end = max(motif_end, 0.5)
    motif = [n for n in nd if n["start"] < motif_end - 1e-6]
    if not motif:
        motif = list(nd)
        motif_end = _phrase_span(motif) or BEATS_PER_BAR

    if key_str is None:
        pc_hist = [0.0] * 12
        for note in nd:
            pc_hist[note["pitch"] % 12] += 1
        tonic_d, mode_d, _ = estimate_key(pc_hist)
        key_str = f"{tonic_d} {mode_d}"
    tonic, _mode = _parse_key_string(key_str)
    tonic_pc = PITCH_NAMES.index(tonic)
    legal = _legal_degree_set(nd, tonic_pc)
    walk = _alphabet_walk_offsets(walk_seed)

    out: list[dict] = []
    t = 0.0
    rep = 0
    while t < span - 1e-6:
        semis = walk[rep % len(walk)]
        for n in motif:
            ns = n["start"] + t
            if ns >= span - 1e-6:
                break
            out.append({
                "pitch": _snap_to_legal(n["pitch"] + semis, tonic_pc, legal),
                "start": round(ns, 6),
                "duration": max(0.01, min(n["duration"], span - ns)),
                "velocity": n["velocity"],
            })
        t += motif_end
        rep += 1
    return out


# --- humanize-like-me: velocity/timing DNA post-pass ---------------------------
# Learned from HIS corpus (velocity_by_role / timing_by_role in style_profile.json).
# Never a score term — score_phrase/_taste_features don't read velocity. Missing
# stats → identity (no invented contour).

_GENERATED_STRENGTH = 1.0   # generated parts (flat 76/68 etc.) take the full contour
_REAL_STRENGTH = 0.35       # real phrases keep most of their own played dynamics


def _humanize_stats(role: str) -> tuple[dict | None, dict | None]:
    agg = _aggregate()
    vb = (agg.get("velocity_by_role") or {}).get(role)
    tb = (agg.get("timing_by_role") or {}).get(role)
    return vb or None, tb or None


def humanize_notes(notes: list[dict], role: str = "lead", strength: float | None = None,
                   timing: bool = True, tempo: float | None = None, seed: int = 0) -> list[dict]:
    """Map each note's 16th-slot onto the learned per-role velocity contour and add
    bounded micro-timing from the corpus. Deterministic given ``seed``; ``strength=0``
    (or missing profile stats) returns the input unchanged. Works on either note shape
    (start_beats or start); durations untouched; a note at 0.0 never moves earlier
    than clip start."""
    vb, tb = _humanize_stats(role)
    s = _REAL_STRENGTH if strength is None else max(0.0, min(1.0, float(strength)))
    if not notes or vb is None or s == 0.0:
        return [dict(n) for n in notes]
    slot_mean = vb.get("slot_mean") or []
    slot_std = vb.get("slot_std") or []
    bpm = float(tempo or (_aggregate().get("tempo") or {}).get("mean") or 120.0)
    jitter_bound = min(0.125, 0.030 * bpm / 60.0)   # ±30 ms expressed in beats
    rng = random.Random(f"humanize:{seed}:{role}")
    out: list[dict] = []
    for n in notes:
        m = dict(n)
        start_key = "start_beats" if "start_beats" in m else "start"
        start = float(m.get(start_key, 0.0))
        slot = int(round((start % 4.0) * 4)) % 16
        if slot < len(slot_mean):
            std = float(slot_std[slot]) if slot < len(slot_std) else 0.0
            wobble = max(-1.5 * std, min(1.5 * std, rng.gauss(0.0, std)))
            target = float(slot_mean[slot]) + wobble
            v0 = float(m.get("velocity", 80))
            m["velocity"] = int(max(1, min(127, round((1.0 - s) * v0 + s * target))))
        if timing and tb and start > 0.0:
            dev = rng.gauss(float(tb.get("dev_mean_beats", 0.0)),
                            float(tb.get("dev_std_beats", 0.0)))
            dev = max(-jitter_bound, min(jitter_bound, dev)) * s
            m[start_key] = round(max(0.0, start + dev), 6)
        out.append(m)
    return out


# Which 16th slots deviate first when a role's offgrid_frac says k of 16 move:
# off-16ths breathe before off-8ths before backbeats; the downbeat moves last.
_GROOVE_DEV_PRIORITY = (1, 3, 5, 7, 9, 11, 13, 15, 2, 6, 10, 14, 4, 8, 12, 0)


def build_groove_template_notes(role: str = "lead", bars: int = 1, seed: int = 0) -> list[dict]:
    """A groove-template clip distilled from HIS timing/velocity DNA: 16 notes per bar
    (pitch 60, 16th grid), where round(offgrid_frac*16) slots per bar deviate by
    clamp(gauss(dev_mean, dev_std), ±0.125) — drawn independently per bar, deviating
    slots fixed by _GROOVE_DEV_PRIORITY, bar-1 downbeat always anchored at 0.0 —
    and velocities follow the role's slot contour (no noise: Live's groove Velocity
    dial does the scaling). Drag into Live → Extract Groove(s) → it becomes a native,
    reusable RICARDO FEEL groove. Deterministic given seed; raises if the profile
    lacks this role's DNA (no invented feel)."""
    vb, tb = _humanize_stats(role)
    if vb is None or tb is None:
        raise ValueError(
            f"no velocity/timing DNA for role {role!r} in style_profile.json — "
            "rerun analyze/style_profile.py")
    slot_mean = vb.get("slot_mean") or []
    if len(slot_mean) < 16:
        raise ValueError(f"role {role!r} has no 16-slot velocity contour")
    bars = max(1, int(bars))
    k = max(0, min(16, round(float(tb.get("offgrid_frac", 0.0)) * 16)))
    dev_slots = set(_GROOVE_DEV_PRIORITY[:k])
    rng = random.Random(f"groove:{role}:{bars}:{seed}")
    notes: list[dict] = []
    for bar in range(bars):
        for slot in range(16):
            start = bar * BEATS_PER_BAR + slot * 0.25
            if slot in dev_slots and not (bar == 0 and slot == 0):
                dev = rng.gauss(float(tb.get("dev_mean_beats", 0.0)),
                                float(tb.get("dev_std_beats", 0.0)))
                start += max(-0.125, min(0.125, dev))
            notes.append({
                "pitch": 60,
                "start_beats": round(max(0.0, start), 6),
                "duration_beats": 0.125,
                "velocity": int(max(1, min(127, round(float(slot_mean[slot]))))),
            })
    return notes


# --- profile-driven composition -----------------------------------------------

def _read_notes_md() -> str:
    if not os.path.exists(NOTES_MD):
        return ""
    with open(NOTES_MD) as fh:
        return fh.read()


def _default_key_from_profile(agg: dict) -> str:
    """Top tonic+mode: dominant mode × highest-count key in that mode."""
    mode_dist = agg.get("mode_distribution", [])
    dominant_mode = mode_dist[0][0] if mode_dist else "minor"
    for key_str, _ in agg.get("key_distribution", []):
        parts = key_str.rsplit(" ", 1)
        if len(parts) == 2 and parts[1] == dominant_mode:
            return key_str
    keys = agg.get("key_distribution", [])
    return keys[0][0] if keys else "D minor"


_FLAT_TO_SHARP = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#",
                  "Cb": "B", "Fb": "E", "E#": "F", "B#": "C"}


def _parse_key_string(key: str) -> tuple[str, str]:
    """'Tonic mode' -> (sharp tonic, lowercase mode). Flats normalise to the
    sharp spelling PITCH_NAMES uses; multi-word modes ('harmonic minor') keep
    their words; the mode is lowercased so Live's 'C Major' reads as major."""
    parts = key.strip().split(None, 1)
    if len(parts) != 2:
        raise ValueError(f"key must be 'Tonic mode', got {key!r}")
    tonic = parts[0][:1].upper() + parts[0][1:].lower()
    tonic = _FLAT_TO_SHARP.get(tonic, tonic)
    if tonic not in PITCH_NAMES:
        raise ValueError(f"unknown tonic {parts[0]!r} in key {key!r}")
    return tonic, parts[1].strip().lower()


def _normalized_weights(weights: list[float]) -> list[float]:
    total = float(sum(weights)) or 1.0
    return [float(w) / total for w in weights]


def _notes_md_bias(notes_text: str, density: str) -> tuple[str, bool, bool]:
    """Return (effective_density, avoid_dominant, sparse_bias) from NOTES.md keywords."""
    low = notes_text.lower()
    sparse = any(w in low for w in ("space", "hypnotic", "minimal", "sparse", "room"))
    modal = any(w in low for w in ("modal", "no leading tone", "no leading-tone", "dorian"))
    avoid_dom = modal or "no leading tone" in low or "avoid v" in low
    eff = density
    if sparse and density == "full":
        eff = "mid"
    elif sparse and density == "mid":
        eff = "minimal"
    return eff, avoid_dom, sparse


_CLASS_NAMES = ("climb", "fall", "wobble", "dark_landing", "repeat")
_DIAL_MULT = {"hard": 2.0, "medium": 1.0, "easy": 0.5, "off": 0.0}
_DIAL_LINE_RE = re.compile(
    r"\b(climb|fall|wobble|dark[-_ ]?landing|repeat)\b\s*[:=]\s*"
    r"(hard|medium|med|easy|off)\b",
    re.IGNORECASE,
)


def _class_weights_from_notes_md(text: str) -> dict[str, float]:
    """Parse per-class dial lines ('wobble: hard') from NOTES.md → weight multipliers."""
    weights = {c: 1.0 for c in _CLASS_NAMES}
    for m in _DIAL_LINE_RE.finditer(text or ""):
        cls = m.group(1).lower().replace(" ", "_").replace("-", "_")
        lvl = m.group(2).lower()
        if lvl == "med":
            lvl = "medium"
        weights[cls] = _DIAL_MULT.get(lvl, 1.0)
    return weights


def _sample_progression(
    rng: random.Random,
    harmony: dict,
    *,
    avoid_dominant: bool = False,
) -> tuple[str, list[str]]:
    """Weighted sample from harmony.json; returns (progression_str, roman_figures)."""
    progs = harmony.get("progressions", [])
    if not progs:
        return "i→iv", ["i", "iv"]
    pool: list[tuple[str, int]] = []
    for prog, count in progs:
        figures = [f.strip() for f in prog.replace("→", "→").split("→") if f.strip()]
        if avoid_dominant and any(
            re.search(r"(?<![iv])V(?![I])", f) or f == "V" for f in figures
        ):
            continue
        pool.append((prog, int(count)))
    if not pool:
        pool = [(p, int(c)) for p, c in progs]
    weights = [c for _, c in pool]
    choice = rng.choices([p for p, _ in pool], weights=weights, k=1)[0]
    figures = [f.strip() for f in choice.split("→") if f.strip()]
    return choice, figures


def _roman_triad_midis(figure: str, tonic: str, mode: str, octave: int = 4) -> list[int]:
    from music21 import key as m21key, roman

    k = m21key.Key(tonic, mode)
    rn = roman.RomanNumeral(figure, k)
    base = (octave + 1) * 12
    midis: list[int] = []
    for p in rn.pitches[:3]:
        midi = p.midi
        while midi < base:
            midi += 12
        while midi > base + 24:
            midi -= 12
        midis.append(midi)
    return sorted(set(midis))


def _roman_root_midi(figure: str, tonic: str, mode: str, octave: int = 2) -> int:
    from music21 import key as m21key, roman

    k = m21key.Key(tonic, mode)
    rn = roman.RomanNumeral(figure, k)
    pc = rn.root().pitchClass
    return (octave + 1) * 12 + pc


def _scale_midi_pitches(
    scale_pcs: set[int],
    lo: int,
    hi: int,
) -> list[int]:
    return sorted(
        midi for midi in range(lo, hi + 1)
        if (midi % 12) in scale_pcs
    )


def _snap_to_scale(pitch: int, scale_pcs: set[int]) -> int:
    pc = pitch % 12
    if pc in scale_pcs:
        return pitch
    best = pitch
    best_dist = 13
    for cand in range(pitch - 6, pitch + 7):
        if (cand % 12) in scale_pcs:
            d = abs(cand - pitch)
            if d < best_dist:
                best_dist = d
                best = cand
    return best


def _legal_degree_set(nd: list[dict], tonic_pc: int) -> set[int]:
    """His lead alphabet (1/b3/4/5/b7) union every degree the source phrase already uses."""
    legal = {0, 3, 5, 7, 10}
    for n in nd:
        legal.add((n["pitch"] - tonic_pc) % 12)
    return legal


def _snap_to_legal(pitch: int, tonic_pc: int, legal: set[int]) -> int:
    """Nearest-PC snap onto ``legal`` degrees relative to ``tonic_pc``; ties break downward."""
    if (pitch - tonic_pc) % 12 in legal:
        return pitch
    best = pitch
    best_dist = 13
    for cand in range(pitch - 6, pitch + 7):
        if (cand - tonic_pc) % 12 in legal:
            d = abs(cand - pitch)
            if d < best_dist:
                best_dist = d
                best = cand
    return best


def _chord_timeline(
    figures: list[str],
    bars: int,
    beats_per_bar: float = BEATS_PER_BAR,
) -> list[tuple[float, float, str]]:
    """Chord segments cycling the progression across ``bars``, quantised to whole
    bars so chord changes land on bar lines (falls back to an equal split only when
    there are more chords than bars)."""
    total_beats = bars * beats_per_bar
    n = len(figures)
    if n == 0:
        return [(0.0, total_beats, "i")]
    timeline: list[tuple[float, float, str]] = []
    if bars >= n:
        base, extra = divmod(bars, n)
        bar_cursor = 0
        for i, fig in enumerate(figures):
            seg_bars = base + (1 if i < extra else 0)  # front-load the remainder
            start = bar_cursor * beats_per_bar
            bar_cursor += seg_bars
            end = total_beats if i == n - 1 else bar_cursor * beats_per_bar
            timeline.append((start, end, fig))
    else:
        seg = total_beats / n
        for i, fig in enumerate(figures):
            start = i * seg
            end = total_beats if i == n - 1 else (i + 1) * seg
            timeline.append((start, end, fig))
    return timeline


def _chord_pcs_at(timeline: list[tuple[float, float, str]], beat: float,
                  tonic: str, mode: str) -> list[int]:
    fig = timeline[-1][2]
    for start, end, f in timeline:
        if start <= beat < end - 1e-9:
            fig = f
            break
    return _roman_chord_pcs(fig, tonic, mode)


def _profile_duration_beats(rng: random.Random, agg: dict) -> float:
    dist = agg.get("duration_distribution", [])
    if not dist:
        return 0.25
    labels = [d[0] for d in dist[:5]]
    weights = [float(d[1]) for d in dist[:5]]
    label = rng.choices(labels, weights=weights, k=1)[0]
    mapping = {
        "1/16": 0.25,
        "1/8": 0.5,
        "1/4": 1.0,
        "1/2": 2.0,
        ">whole": 4.0,
    }
    return mapping.get(label, 0.25)


def _sample_groove_onsets(
    rng: random.Random,
    groove: list[float],
    bars: int,
    target_count: int,
    *,
    groove_power: float = 1.0,
) -> list[float]:
    """Sample onsets via weighted-WITHOUT-replacement over every bar/16th position,
    seeded by ``rng`` so candidates genuinely vary in rhythm (the old version ranked
    slots and picked the top deterministically, so seed/groove_power were inert). It
    still favours his groove; ``groove_power`` is a temperature (>1 peaks toward his
    hot slots, <1 flattens). A per-bar cap spreads onsets across the section."""
    w = _normalized_weights([x ** groove_power for x in _normalized_weights(groove)])
    positions = [(bar * BEATS_PER_BAR + slot / 4.0, w[slot])
                 for bar in range(bars) for slot in range(16)]
    target = min(target_count, len(positions))
    max_per_bar = max(1, (target_count + bars - 1) // bars)
    selected: list[float] = []
    per_bar: dict[int, int] = {}
    while positions and len(selected) < target:
        weights = [wt for _, wt in positions]
        i = (rng.choices(range(len(positions)), weights=weights, k=1)[0]
             if sum(weights) > 0 else rng.randrange(len(positions)))
        beat, _ = positions.pop(i)
        bar_i = int(beat // BEATS_PER_BAR)
        if per_bar.get(bar_i, 0) >= max_per_bar:
            continue
        selected.append(beat)
        per_bar[bar_i] = per_bar.get(bar_i, 0) + 1
    return sorted(selected)


def _compose_pad_from_progression(
    timeline: list[tuple[float, float, str]],
    tonic: str,
    mode: str,
    scale_pcs: set[int],
    *,
    pad_vel: int,
    reg_lo: int,
    reg_hi: int,
) -> list[dict]:
    notes: list[dict] = []
    for start, end, fig in timeline:
        triad = _roman_triad_midis(fig, tonic, mode, octave=4)
        triad = [
            _snap_to_scale(max(reg_lo, min(reg_hi, p)), scale_pcs)
            for p in triad
        ]
        dur = max(0.01, end - start)
        for i, pitch in enumerate(triad):
            notes.append({
                "pitch": pitch,
                "start_beats": round(start, 6),
                "duration_beats": dur,
                "velocity": max(40, pad_vel - i * 4),
            })
    return notes


def _enforce_monophony(notes: list[dict], min_dur: float = 0.1) -> list[dict]:
    """Clip each note so it ends before the next onset — no self-overlap in a line
    meant to read as monophonic. Returns notes in start order."""
    ordered = sorted(notes, key=lambda n: n["start_beats"])
    for i in range(len(ordered) - 1):
        gap = ordered[i + 1]["start_beats"] - ordered[i]["start_beats"]
        if gap > 0:
            ordered[i]["duration_beats"] = min(ordered[i]["duration_beats"], max(min_dur, gap))
    return ordered


def _compose_bass_from_progression(
    timeline: list[tuple[float, float, str]],
    tonic: str,
    mode: str,
    scale_pcs: set[int],
    rng: random.Random,
    *,
    bass_npb: float,
    bars: int,
    reg_lo: int,
    reg_hi: int,
    groove: list[float],
    minimal: bool,
) -> list[dict]:
    notes: list[dict] = []
    bass_lo = reg_lo
    bass_hi = min(reg_lo + 24, reg_hi)
    if minimal:
        for start, end, fig in timeline:
            root = _roman_root_midi(fig, tonic, mode, octave=2)
            root = max(bass_lo, min(bass_hi, _snap_to_scale(root, scale_pcs)))
            notes.append({
                "pitch": root,
                "start_beats": round(start, 6),
                "duration_beats": max(0.01, end - start),
                "velocity": 78,
            })
        return notes

    target = max(len(timeline), int(round(bass_npb * bars)))
    
    # Steadier bass rhythm: chord changes + downbeats + 8th-note slots
    onsets_set = set()
    for start, end, fig in timeline:
        onsets_set.add(round(start, 6))
    for b in range(bars):
        onsets_set.add(float(b * BEATS_PER_BAR))
    if bass_npb >= 1.5:
        for b in range(bars):
            onsets_set.add(float(b * BEATS_PER_BAR + 2.0))
            
    if len(onsets_set) < target:
        candidates = _sample_groove_onsets(rng, groove, bars, target * 2)
        for beat in candidates:
            slot = int(round((beat % BEATS_PER_BAR) * 4)) % 16
            if slot % 2 == 0:  # 8th notes only
                onsets_set.add(round(beat, 6))
            if len(onsets_set) >= target:
                break
                
    onsets = sorted(list(onsets_set))
    strong = {0, 4, 8, 12}
    for beat in onsets:
        fig = timeline[-1][2]
        for start, end, f in timeline:
            if start <= beat < end - 1e-9:
                fig = f
                break
        root = _roman_root_midi(fig, tonic, mode, octave=2)
        root = max(bass_lo, min(bass_hi, _snap_to_scale(root, scale_pcs)))
        slot = int(round((beat % BEATS_PER_BAR) * 4)) % 16
        dur = 1.0 if slot in strong else 0.5
        notes.append({
            "pitch": root,
            "start_beats": round(beat, 6),
            "duration_beats": dur,
            "velocity": 82 if slot in strong else 72,
        })
        
    notes = _enforce_monophony(notes)
        
    return notes


def _preferred_leap_sizes(agg: dict) -> list[tuple[int, float]]:
    """Weighted leap sizes from profile top_intervals (|semitones| >= 3)."""
    leaps: list[tuple[int, float]] = []
    for iv in agg.get("top_melodic_intervals", []):
        semi = int(iv.get("semitones", 0))
        if abs(semi) >= 3:
            leaps.append((abs(semi), float(iv.get("pct", 1.0))))
    if not leaps:
        return [(3, 1), (4, 1), (5, 1), (7, 1)]
    return leaps


def _target_step_ratio(melody_shape: dict) -> float:
    """Target step share among non-repeat moves, matching the learned profile shape."""
    pct_steps = melody_shape.get("pct_steps", 33.0)
    pct_leaps = melody_shape.get("pct_leaps", 51.0)
    return pct_steps / max(pct_steps + pct_leaps, 0.01)


def _planned_interval(
    rng: random.Random,
    melody_shape: dict,
    agg: dict,
    n_steps: int,
    n_leaps: int,
) -> tuple[int, bool]:
    """Pick signed semitone interval targeting profile step/leap mix."""
    target_step = _target_step_ratio(melody_shape)
    moves = n_steps + n_leaps
    if moves == 0:
        want_step = rng.random() < target_step
    else:
        actual = n_steps / moves
        if actual < target_step - 0.05:
            want_step = True
        elif actual > target_step + 0.05:
            want_step = False
        else:
            want_step = rng.random() < target_step

    pct_up = melody_shape.get("pct_up", 50.0) / 100.0
    direction = 1 if rng.random() < pct_up else -1

    if want_step:
        size = rng.choice([1, 1, 2])
        return direction * size, True

    leap_pool = _preferred_leap_sizes(agg)
    sizes, weights = zip(*leap_pool)
    size = rng.choices(list(sizes), weights=list(weights), k=1)[0]
    return direction * size, False


def _pitch_from_interval(
    prev: int,
    semitones: int,
    scale_pool: list[int],
    chord_pcs: list[int],
    *,
    strong_beat: bool,
) -> int:
    """Land on scale pitch near prev + semitones; chord tones preferred on strong beats."""
    target = prev + semitones
    scored: list[tuple[float, int]] = []
    for p in scale_pool:
        cost = abs(p - target)
        if (p % 12) not in chord_pcs:
            cost += 1.5 if strong_beat else 0.5
        if p == prev:
            cost += 4.0
        scored.append((cost, p))
    scored.sort(key=lambda x: (x[0], x[1]))
    return scored[0][1]


def _next_melody_pitch(
    rng: random.Random,
    prev: int,
    chord_pcs: list[int],
    scale_pool: list[int],
    melody_shape: dict,
    agg: dict,
    *,
    strong_beat: bool,
    hover_center: int,
    n_steps: int,
    n_leaps: int,
) -> tuple[int, bool]:
    """Voice-leading step/leap per profile. Returns (pitch, was_step)."""
    semis, was_step = _planned_interval(
        rng, melody_shape, agg, n_steps, n_leaps,
    )
    pitch = _pitch_from_interval(
        prev, semis, scale_pool, chord_pcs, strong_beat=strong_beat,
    )
    if pitch == prev:
        semis, was_step = _planned_interval(
            rng, melody_shape, agg, n_steps, n_leaps,
        )
        if not was_step:
            semis = (1 if semis > 0 else -1) * max(3, abs(semis))
        pitch = _pitch_from_interval(
            prev, semis, scale_pool, chord_pcs, strong_beat=False,
        )
    return pitch, was_step


def _sample_from_distribution(rng: random.Random, dist: dict[str, float]) -> int:
    if not dist:
        return 0
    states = list(dist.keys())
    weights = list(dist.values())
    sampled_str = rng.choices(states, weights=weights, k=1)[0]
    return int(sampled_str)


def _sample_melodic_state(
    rng: random.Random,
    grammar: dict,
    prev_state: int | None,
    prev2_state: int | None,
) -> int:
    unigram = grammar.get("unigram", {})
    order_1 = grammar.get("order_1", {})
    order_2 = grammar.get("order_2", {})

    # Try order 2
    if prev_state is not None and prev2_state is not None:
        key_2 = f"{prev2_state},{prev_state}"
        if key_2 in order_2 and order_2[key_2]:
            return _sample_from_distribution(rng, order_2[key_2])

    # Try order 1
    if prev_state is not None:
        key_1 = str(prev_state)
        if key_1 in order_1 and order_1[key_1]:
            return _sample_from_distribution(rng, order_1[key_1])

    # Fall back to unigram
    if unigram:
        return _sample_from_distribution(rng, unigram)

    return rng.choice([0, 2, 3, 5, 7, 8, 10])  # default diatonic minor


# ── Gesture-vocabulary lead generation (store/motifs.json) ───────────────────

def _deg_circ_dist(a: int, b: int) -> int:
    d = abs(a - b) % 12
    return min(d, 12 - d)


def _degree_to_pitch(deg, prev, tonic_idx, scale_pcs, lead_lo, lead_hi, hover_center):
    """Chroma-offset degree → MIDI pitch nearest prev, clamped to register then snapped
    in-scale (snap LAST so the register clamp can't reintroduce an out-of-scale note)."""
    target_pc = (tonic_idx + deg) % 12
    cands = [p for p in range(lead_lo, lead_hi + 1) if p % 12 == target_pc]
    pitch = (int(prev) if not cands
             else min(cands, key=lambda p: (abs(p - prev), abs(p - hover_center))))
    return _snap_to_scale(max(lead_lo, min(lead_hi, pitch)), scale_pcs)


def _harmonic_fit(degrees, timeline, onsets, i, tonic, mode):
    """Fraction of a cell's strong-slot degrees landing on chord tones (0.5 if none)."""
    tonic_idx = PITCH_NAMES.index(tonic)
    hits = total = 0
    for beat, deg in zip(onsets[i:i + len(degrees)], degrees):
        if int(round((beat % BEATS_PER_BAR) * 4)) % 16 != 0:
            continue
        total += 1
        if (tonic_idx + deg) % 12 in _chord_pcs_at(timeline, beat, tonic, mode):
            hits += 1
    return hits / total if total else 0.5


def _seed_gesture(rng, cells, class_weights, recent=()):
    """Pick an opening gesture by weight × class-dial, skipping recently-used cells."""
    cw = class_weights or {}
    pool, weights = [], []
    for idx, c in enumerate(cells):
        if idx in recent:
            continue
        w = c["weight"] * cw.get(c["class"], 1.0)
        if c["class"] == "repeat":
            w *= 0.4  # tame staticness (NOTES.md 'repeat: hard' can bring it back)
        if w > 0:
            pool.append(idx)
            weights.append(w)
    if not pool:
        idx = rng.randrange(len(cells))
        return idx, cells[idx]
    idx = rng.choices(pool, weights=weights, k=1)[0]
    return idx, cells[idx]


def _chain_gesture(rng, cells, class_weights, timeline, onsets, i, tonic, mode,
                   prev_out, recent, want_landing):
    """Pick the next gesture: connect by STEP (not unison), no back-to-back reuse."""
    cw = class_weights or {}
    pool, weights = [], []
    for idx, c in enumerate(cells):
        if idx in recent:
            continue
        gap = _deg_circ_dist(c["boundary_in"], prev_out)
        if gap == 0:
            compat = 0.5            # unison connection: allowed, repeats a note
        elif gap <= 2:
            compat = 1.0            # step connection: smoothest
        elif gap <= 5:
            compat = 0.8            # leaps are part of his voice — keep them
        else:
            compat = 0.4
        w = c["weight"] * cw.get(c["class"], 1.0) * compat
        if c["class"] == "repeat":
            w *= 0.4
        w *= 1.0 + 0.5 * _harmonic_fit(c["degrees"], timeline, onsets, i, tonic, mode)
        if want_landing and (c["class"] == "dark_landing" or c["degrees"][-1] in (0, 7)):
            w *= 2.5
        if w > 0:
            pool.append(idx)
            weights.append(w)
    if not pool:
        return _seed_gesture(rng, cells, class_weights, recent)
    idx = rng.choices(pool, weights=weights, k=1)[0]
    return idx, cells[idx]


def _fit_degrees_to_onsets(degs, k):
    """Match a gesture's degree count to available onsets (always keep the landing)."""
    if k == len(degs):
        return degs
    if k < len(degs):
        return degs[:k - 1] + [degs[-1]] if k >= 2 else [degs[-1]]
    out, idx, step = [], 0.0, len(degs) / k
    for _ in range(k):
        out.append(degs[min(len(degs) - 1, int(idx))])
        idx += step
    return out


def _gesture_lead(onsets, timeline, tonic, mode, scale_pcs, rng, agg, *,
                  lead_lo, lead_hi, class_weights=None):
    """Compose the lead pitch-contour from Ricardo's gesture vocabulary.

    Returns None if store/motifs.json is absent (caller falls back to the Markov
    path). Rhythm (the groove onsets) is preserved; only pitch selection changes.
    Cells are placed verbatim and chained by STEP (not unison), with no back-to-back
    reuse — so the line keeps moving and flows instead of sitting still.
    """
    cells = _motifs_data().get("cells")
    if not cells:
        return None
    if not onsets:
        return []
    tonic_idx = PITCH_NAMES.index(tonic)
    hover_center = (lead_lo + lead_hi) // 2
    n = len(onsets)

    notes: list[dict] = []
    i = 0
    prev = hover_center
    prev_out = None
    recent: list[int] = []
    while i < n:
        want_landing = (n - i) <= 4
        if prev_out is None:
            idx, cell = _seed_gesture(rng, cells, class_weights, recent)
        else:
            idx, cell = _chain_gesture(rng, cells, class_weights, timeline, onsets, i,
                                       tonic, mode, prev_out, recent, want_landing)
        seg_len = len(onsets[i:i + len(cell["degrees"])])
        degs = _fit_degrees_to_onsets(list(cell["degrees"]), seg_len)
        seg = onsets[i:i + len(degs)]
        for beat, deg in zip(seg, degs):
            strong = int(round((beat % BEATS_PER_BAR) * 4)) % 16 == 0
            pitch = _degree_to_pitch(deg, prev, tonic_idx, scale_pcs,
                                     lead_lo, lead_hi, hover_center)
            dur = _profile_duration_beats(rng, agg)
            if strong:
                dur = max(dur, 0.5)
            notes.append({
                "pitch": pitch,
                "start_beats": round(beat, 6),
                "duration_beats": dur,
                "velocity": 76 if strong else 68,
            })
            prev = pitch
        prev_out = degs[-1] if degs else prev_out
        recent = ([idx] + recent)[:3]
        i += len(seg)
    return _enforce_monophony(notes)


def _compose_lead_from_profile(
    timeline: list[tuple[float, float, str]],
    tonic: str,
    mode: str,
    scale_pcs: set[int],
    rng: random.Random,
    agg: dict,
    *,
    lead_npb: float,
    rest_ratio: float,
    bars: int,
    reg_lo: int,
    reg_hi: int,
    groove_power: float = 1.8,
) -> list[dict]:
    groove = agg.get("harmonic_groove", [1.0] * 16)
    median = int(agg.get("pitch_median", 58))
    lead_lo = max(reg_lo, median - 12)
    lead_hi = min(reg_hi, median + 14)

    target = max(2, int(round(lead_npb * bars * (1.0 - rest_ratio))))
    onsets = _sample_groove_onsets(
        rng, groove, bars, target, groove_power=groove_power,
    )

    if _USE_GESTURE_LEAD:
        gesture_notes = _gesture_lead(
            onsets, timeline, tonic, mode, scale_pcs, rng, agg,
            lead_lo=lead_lo, lead_hi=lead_hi,
            class_weights=_class_weights_from_notes_md(_read_notes_md()),
        )
        if gesture_notes is not None:
            return gesture_notes

    # --- Markov lead (the eared-preferred default; also the gesture fallback) ---
    # Prefer the LEAD-specific grammar so the line isn't a warm average of
    # lead+bass+pad; fall back to the pooled grammar until the role rebuild runs.
    grammar = agg.get("melodic_grammar_by_role", {}).get("lead") or agg.get("melodic_grammar", {})
    tonic_idx = PITCH_NAMES.index(tonic)

    # Sample the first pitch class
    unigram = grammar.get("unigram", {})
    if unigram:
        start_state = _sample_from_distribution(rng, unigram)
    else:
        start_state = 0  # tonic

    # Find candidate closest to register middle
    hover_center = (lead_lo + lead_hi) // 2
    candidates = [p for p in range(lead_lo, lead_hi + 1) if (p - tonic_idx) % 12 == start_state]
    if not candidates:
        candidates = [hover_center]
    prev = min(candidates, key=lambda p: abs(p - hover_center))

    prev2_state = None
    prev_state = start_state

    notes: list[dict] = []
    for beat in onsets:
        slot = int(round((beat % BEATS_PER_BAR) * 4)) % 16
        strong = slot == 0
        chord_pcs = _chord_pcs_at(timeline, beat, tonic, mode)

        # Sample next scale-degree/chroma-offset state
        next_pc_state = _sample_melodic_state(rng, grammar, prev_state, prev2_state)

        # Find candidate MIDI pitches matching the state in register
        candidates = [p for p in range(lead_lo, lead_hi + 1) if (p - tonic_idx) % 12 == next_pc_state]
        if not candidates:
            # Fallback to scale pool close to prev
            candidates = [p for p in range(lead_lo, lead_hi + 1)]

        # Score candidates: close to prev pitch, small penalty for same pitch (encourages movement),
        # preference for chord tones on strong beats.
        scored_candidates = []
        for p in candidates:
            cost = abs(p - prev)
            if p == prev:
                cost += 1.5
            if (p % 12) not in chord_pcs:
                cost += 1.0 if strong else 0.3
            cost += 0.2 * abs(p - hover_center)
            scored_candidates.append((cost, p))
        scored_candidates.sort()
        pitch = scored_candidates[0][1]
        pitch = _snap_to_scale(pitch, scale_pcs)
        pitch = max(lead_lo, min(lead_hi, pitch))

        dur = _profile_duration_beats(rng, agg)
        if strong:
            dur = max(dur, 0.5)
        notes.append({
            "pitch": pitch,
            "start_beats": round(beat, 6),
            "duration_beats": dur,
            "velocity": 76 if strong else 68,
        })

        prev2_state = prev_state
        prev_state = (pitch - tonic_idx) % 12
        prev = pitch

    return _enforce_monophony(notes)


def _generate_lead_candidates(
    timeline: list[tuple[float, float, str]],
    tonic: str,
    mode: str,
    scale_pcs: set[int],
    agg: dict,
    *,
    lead_npb: float,
    rest_ratio: float,
    bars: int,
    reg_lo: int,
    reg_hi: int,
    seed: int,
) -> list[tuple[list[dict], float]]:
    """The raw lead-candidate pool — pure style generation, NO taste rerank.
    Returns [(notes, style_score), ...]. This is the generation half of the pipeline;
    selection (taste rerank) is a separate step so auditions can rate unbiased options."""
    scored: list[tuple[list[dict], float]] = []
    for i in range(_LEAD_CANDIDATE_COUNT):
        cand_rng = random.Random(seed * 10007 + i * 7919)
        g_power = 1.5 + (i % 6) * 0.2
        notes = _compose_lead_from_profile(
            timeline, tonic, mode, scale_pcs, cand_rng, agg,
            lead_npb=lead_npb,
            rest_ratio=rest_ratio,
            bars=bars,
            reg_lo=reg_lo,
            reg_hi=reg_hi,
            groove_power=g_power,
        )
        scored.append((notes, score_phrase(notes, role="lead",
                                            key=f"{tonic} {mode}")["overall"]))
    return scored


def _lead_interval_stats(notes: list[dict], key_str: str) -> dict:
    return _phrase_style_analysis(_note_dicts(notes), role="lead", key=key_str)["raw"]


def _lead_selection_score(notes: list[dict], style_score: float, agg: dict,
                          key_str: str, *, apply_interval_penalty: bool = True) -> float:
    """Style score with an optional serve-only step/leap guard."""
    if not apply_interval_penalty:
        return style_score
    raw = _lead_interval_stats(notes, key_str)
    target = _target_step_ratio(agg.get("melody_shape", {}))
    penalty = min(0.25, abs(raw["actual_steps"] - target) * 0.55)
    return style_score - penalty


def _select_best_lead(
    timeline: list[tuple[float, float, str]],
    tonic: str,
    mode: str,
    scale_pcs: set[int],
    agg: dict,
    *,
    lead_npb: float,
    rest_ratio: float,
    bars: int,
    reg_lo: int,
    reg_hi: int,
    seed: int,
    use_taste: bool = True,
) -> tuple[list[dict], float]:
    """Pick the best candidate by style score, reranked by the learned taste model once
    enough of Ricardo's judgments exist (deterministic). ``use_taste=False`` returns the
    pure style-best — used by audition so he rates the model's *options*, not its picks."""
    scored = _generate_lead_candidates(
        timeline, tonic, mode, scale_pcs, agg,
        lead_npb=lead_npb, rest_ratio=rest_ratio, bars=bars,
        reg_lo=reg_lo, reg_hi=reg_hi, seed=seed,
    )
    key_str = f"{tonic} {mode}"
    base_scores = {
        i: _lead_selection_score(
            scored[i][0],
            scored[i][1],
            agg,
            key_str,
            apply_interval_penalty=use_taste,
        )
        for i in range(len(scored))
    }

    tw = _taste_weight() if use_taste else 0.0
    if tw <= 0.0:  # cold start (or taste disabled): pure style score
        best_i = max(range(len(scored)), key=lambda i: (
            base_scores[i], scored[i][1], len(scored[i][0]),
        ))
        return scored[best_i][0], scored[best_i][1]

    # rank-blend style quality with learned taste (ranks avoid score-scale mismatch)
    order_base = sorted(range(len(scored)), key=lambda i: -base_scores[i])
    order_taste = sorted(range(len(scored)),
                         key=lambda i: -_taste_score(scored[i][0], role="lead",
                                                     key=key_str))
    base_rank = {i: r for r, i in enumerate(order_base)}
    taste_rank = {i: r for r, i in enumerate(order_taste)}
    best_i = min(range(len(scored)),
                 key=lambda i: (1 - tw) * base_rank[i] + tw * taste_rank[i])
    return scored[best_i][0], scored[best_i][1]


def audition_leads(bars: int = 8, key: str | None = None, density: str = "mid",
                   n: int = 4, seed: int = 0, *,
                   use_ears: bool | None = None, direction: str | None = None,
                   moment: dict | None = None) -> dict:
    """Generate N distinct lead candidates for the ear-rating loop, with their base
    and taste scores. Audition them on his sounds, then call rate_candidates(best, worst).

    Audition samples the raw generated pool across step/leap bands. Normal serving may
    still use a step/leap guard, but the rating loop must expose boundary cases so
    Ricardo can teach the model instead of only confirming pre-filtered picks."""
    if density not in _DENSITY_PARAMS:
        raise ValueError("density must be 'minimal', 'mid', or 'full'")
    bars = max(1, int(bars))
    want = max(2, int(n))
    rng = random.Random(seed)
    agg = _aggregate()
    harmony = _harmony_data()
    notes_text = _read_notes_md()
    eff_density, avoid_dom, _sparse_bias = _notes_md_bias(notes_text, density)
    params = _DENSITY_PARAMS[eff_density]

    key_str = key or _default_key_from_profile(agg)
    tonic, mode = _parse_key_string(key_str)
    tonic_idx = PITCH_NAMES.index(tonic)
    scale_pcs = diatonic_set(tonic_idx, mode)
    reg_lo, reg_hi = agg.get("pitch_register", [36, 85])
    prog_str, figures = _sample_progression(rng, harmony, avoid_dominant=avoid_dom)
    timeline = _chord_timeline(figures, bars)

    pool: list[dict] = []
    seen: set[tuple] = set()
    seed_count = max(1, math.ceil(want / _LEAD_CANDIDATE_COUNT))
    for s in range(seed_count):
        for notes, style_score in _generate_lead_candidates(
            timeline, tonic, mode, scale_pcs, agg,
            lead_npb=params["lead_npb"],
            rest_ratio=params["rest_ratio"],
            bars=bars,
            reg_lo=reg_lo,
            reg_hi=reg_hi,
            seed=seed + s,
        ):
            signature = tuple(
                (n.get("pitch"), round(float(n.get("start", 0.0)), 3))
                for n in notes[:24]
            )
            if signature in seen:
                continue
            seen.add(signature)
            raw = _lead_interval_stats(notes, key_str)
            pool.append({
                "notes": notes,
                "style_score": style_score,
                "step_ratio": raw.get("actual_steps", 0.0),
            })

    pool.sort(key=lambda c: c["step_ratio"])
    chosen: list[dict] = []
    used: set[int] = set()
    if pool:
        if want == 1:
            targets = [0.5]
        else:
            targets = [i / (want - 1) for i in range(want)]
        half_window = max(1, math.ceil(len(pool) / (want * 2)))
        for target in targets:
            center = round(target * (len(pool) - 1))
            lo = max(0, center - half_window)
            hi = min(len(pool), center + half_window + 1)
            window = [i for i in range(lo, hi) if i not in used]
            if not window:
                window = [i for i in range(len(pool)) if i not in used]
            if not window:
                break
            best_i = max(window, key=lambda i: pool[i]["style_score"])
            used.add(best_i)
            chosen.append(pool[best_i])

    cands = [c["notes"] for c in chosen]
    result = {
        "candidates": cands,
        "count": len(cands),
        "key": key_str,
        "base_scores": [round(score_phrase(c, role="lead", key=key_str)["overall"], 3)
                        for c in cands],
        "taste_scores": [round(_taste_score(c, role="lead", key=key_str), 3) for c in cands],
        "interval_step_ratios": [round(c["step_ratio"], 3) for c in chosen],
        "taste_weight": round(_taste_weight(), 2),
        "taste_applied": False,
        "selection_mode": "raw_pool_interval_bands",
        "progression": prog_str,
        "role": "lead",
        "hint": ("rate_candidates(candidates, best=<i>, worst=<j>, reason='static'|'flowing'|"
                 "'jumpy'|..., key=<key>, role='lead', contrast_axis='lead')"),
    }
    if use_ears or (use_ears is None and _ears_default()):
        # ``candidates`` are bare MCP note-lists, not serve-shaped dicts, so the
        # ears gate an index-carrying wrapper (internal-shape notes) and every
        # parallel array is realigned to the survivors.
        order = _serve_indexed(result, [
            {"i": i, "role": "lead", "key": key_str, "notes": _note_dicts(nl)}
            for i, nl in enumerate(cands)
        ], role="lead", direction=direction, moment=moment)
        for _k in ("candidates", "base_scores", "taste_scores",
                   "interval_step_ratios"):
            result[_k] = [result[_k][i] for i in order]
        result["count"] = len(order)
    return result


def _tile_phrase_to_bars(
    nd: list[dict], bars: int, loop_bars: int | None = None
) -> list[dict]:
    """Repeat *nd* to fill ``bars``.

    When ``loop_bars`` is set (arrangement parts), the cycle is at least
    ``loop_bars`` bars, but never shorter than the phrase's own span — otherwise
    span-changing variations (e.g. half_time ×2) self-overlap when stamped every
    ``loop_bars`` bars. When omitted, fall back to the phrase span (fill/continue).
    """
    phrase_span = _phrase_span(nd)
    if loop_bars is not None and int(loop_bars) > 0:
        declared = int(loop_bars) * BEATS_PER_BAR
        if phrase_span > declared + 1e-9:
            phrase_bars = max(1, math.ceil(phrase_span / BEATS_PER_BAR - 1e-9))
            span = max(int(loop_bars), phrase_bars) * BEATS_PER_BAR
        else:
            span = declared
    else:
        span = phrase_span if phrase_span > 0 else BEATS_PER_BAR
    total = bars * BEATS_PER_BAR
    out: list[dict] = []
    offset = 0.0
    while offset < total - 1e-9:
        for n in nd:
            start = n["start"] + offset
            if start >= total:
                break
            out.append({
                "pitch": n["pitch"],
                "start": round(start, 6),
                "duration": min(n["duration"], total - start),
                "velocity": n["velocity"],
            })
        offset += span
    return out


def _drum_hit_target(density: str, bars: int, rng: random.Random) -> int:
    """Target drum hits scaled from 8-bar reference range."""
    lo, hi = _DRUM_DENSITY_TARGETS.get(density, _DRUM_DENSITY_TARGETS["mid"])
    scale = bars / 8.0
    lo_s = max(2, int(round(lo * scale)))
    hi_s = max(lo_s, int(round(hi * scale)))
    return rng.randint(lo_s, hi_s)


def _drum_hit_priority(n: dict) -> float:
    """Higher = keep when thinning (kick/snare on grid beats)."""
    beat_in_bar = n["start"] % BEATS_PER_BAR
    slot = int(round(beat_in_bar * 4)) % 16
    pitch = n["pitch"]
    score = n.get("velocity", 80) / 127.0
    if slot == 0:
        score += 3.0
    elif slot == 8:
        score += 2.5
    elif slot in (4, 12):
        score += 2.0
    elif slot % 2 == 0:
        score += 0.8
    if pitch <= 40:
        score += 1.5
    elif pitch <= 45:
        score += 0.5
    return score


def _thin_drum_notes(nd: list[dict], target: int, rng: random.Random) -> list[dict]:
    """Drop drum hits to ``target`` while preserving kick/snare skeleton."""
    if len(nd) <= target:
        return list(nd)
    ranked = sorted(
        nd,
        key=lambda n: (_drum_hit_priority(n), -n["start"]),
        reverse=True,
    )
    kept = ranked[:target]
    kept.sort(key=lambda n: (n["start"], n["pitch"]))
    return kept


def _synthesize_drum_groove(
    rng: random.Random,
    agg: dict,
    bars: int,
    target: int,
) -> list[dict]:
    """Drums from aggregate drum_groove profile, capped at ``target`` hits."""
    groove = agg.get("drum_groove", [1.0] * 16)
    w = _normalized_weights(groove)
    kick, snare, hat = 36, 38, 42
    candidates: list[dict] = []
    for bar in range(bars):
        for slot in range(16):
            beat = bar * BEATS_PER_BAR + slot / 4.0
            if slot % 8 == 0:
                pitch, vel = kick, 100
            elif slot % 8 == 4:
                pitch, vel = snare, 92
            else:
                if rng.random() > w[slot] * 3.0:
                    continue
                pitch, vel = hat, max(55, int(70 + w[slot] * 40))
            candidates.append({
                "pitch": pitch,
                "start": round(beat, 6),
                "duration": 0.25,
                "velocity": vel,
            })
    if len(candidates) > target:
        candidates = _thin_drum_notes(candidates, target, rng)
    elif len(candidates) < target:
        for bar in range(bars):
            if len(candidates) >= target:
                break
            beat = bar * BEATS_PER_BAR
            if not any(abs(c["start"] - beat) < 0.01 for c in candidates):
                candidates.append({
                    "pitch": kick, "start": round(beat, 6),
                    "duration": 0.25, "velocity": 100,
                })
            offbeat = bar * BEATS_PER_BAR + 2.0
            if len(candidates) < target and not any(
                abs(c["start"] - offbeat) < 0.01 for c in candidates
            ):
                candidates.append({
                    "pitch": snare, "start": round(offbeat, 6),
                    "duration": 0.25, "velocity": 92,
                })
        candidates.sort(key=lambda n: (n["start"], n["pitch"]))
        candidates = candidates[:target]
    return [
        {
            "pitch": n["pitch"],
            "start_beats": n["start"],
            "duration_beats": n["duration"],
            "velocity": n["velocity"],
        }
        for n in candidates
    ]


def _compose_drums_from_profile(
    rng: random.Random,
    agg: dict,
    bars: int,
    seed: int,
    density: str,
) -> tuple[list[dict], str]:
    """Drums respecting density: thin a real groove or synthesize to a hit target."""
    target = _drum_hit_target(density, bars, rng)
    phrases = find_phrases(kind="drum", k=3)
    if phrases:
        pick = phrases[seed % len(phrases)]
        nd = _normalize_phrase(_note_dicts(pick["notes"]))
        tiled = _tile_phrase_to_bars(nd, bars)
        thinned = _thin_drum_notes(tiled, target, rng)
        return _notes_to_mcp(thinned), f"phrase:{pick['id']}:thinned→{len(thinned)}"
    synth = _synthesize_drum_groove(rng, agg, bars, target)
    return synth, f"synthesized:drum_groove→{len(synth)}"


def _in_scale_fraction(notes: list[dict], scale_pcs: set[int]) -> float:
    if not notes:
        return 1.0
    ok = sum(1 for n in notes if (int(n["pitch"]) % 12) in scale_pcs)
    return ok / len(notes)


def compose_from_profile(
    bars: int = 8,
    key: str | None = None,
    density: str = "mid",
    seed: int = 0,
    progression: str | None = None,
    use_taste: bool = True,
    humanize: bool = False,
) -> dict:
    """Profile-driven, scale-locked sketch: pad, bass, lead, drums.

    Deterministic given ``seed``. Harmony from harmony.json progressions;
  melody/bass rhythm from style_profile aggregate; drums from real phrases or
  drum_groove synthesis. ``density`` is minimal | mid | full (monotonic note load).
    """
    if density not in _DENSITY_PARAMS:
        raise ValueError("density must be 'minimal', 'mid', or 'full'")
    bars = max(1, int(bars))
    rng = random.Random(seed)
    agg = _aggregate()
    harmony = _harmony_data()
    notes_text = _read_notes_md()
    eff_density, avoid_dom, sparse_bias = _notes_md_bias(notes_text, density)
    params = _DENSITY_PARAMS[eff_density]

    key_str = key or _default_key_from_profile(agg)
    tonic, mode = _parse_key_string(key_str)
    tonic_idx = PITCH_NAMES.index(tonic)
    scale_pcs = diatonic_set(tonic_idx, mode)
    reg_lo, reg_hi = agg.get("pitch_register", [36, 85])

    if progression:  # caller-chosen progression (e.g. a rare growth move)
        figures = [f.strip() for f in progression.replace("->", "→").split("→") if f.strip()]
        prog_str = "→".join(figures)
    else:
        prog_str, figures = _sample_progression(rng, harmony, avoid_dominant=avoid_dom)
    timeline = _chord_timeline(figures, bars)

    pad = _compose_pad_from_progression(
        timeline, tonic, mode, scale_pcs,
        pad_vel=int(params["pad_vel"]),
        reg_lo=max(reg_lo, 45), reg_hi=min(reg_hi, 72),
    )
    bass = _compose_bass_from_progression(
        timeline, tonic, mode, scale_pcs, rng,
        bass_npb=params["bass_npb"],
        bars=bars,
        reg_lo=reg_lo,
        reg_hi=min(reg_lo + 24, reg_hi),
        groove=agg.get("harmonic_groove", [1.0] * 16),
        minimal=eff_density == "minimal",
    )
    lead, lead_style_score = _select_best_lead(
        timeline, tonic, mode, scale_pcs, agg,
        lead_npb=params["lead_npb"],
        rest_ratio=params["rest_ratio"],
        bars=bars,
        reg_lo=reg_lo,
        reg_hi=reg_hi,
        seed=seed,
        use_taste=use_taste,
    )
    drums, drum_source = _compose_drums_from_profile(
        rng, agg, bars, seed, eff_density,
    )

    lead_nd = _note_dicts(lead)
    groove_cos = _l2_cosine(
        _groove_grid(lead_nd),
        _normalized_weights(agg.get("harmonic_groove", [1.0] * 16)),
    )

    if humanize:   # opt-in velocity/timing DNA; metrics above reflect the raw notes
        _th = agg.get("tempo", {}).get("mean")
        pad = humanize_notes(pad, role="pad", strength=_GENERATED_STRENGTH, tempo=_th, seed=seed)
        bass = humanize_notes(bass, role="bass", strength=_GENERATED_STRENGTH, tempo=_th, seed=seed)
        lead = humanize_notes(lead, role="lead", strength=_GENERATED_STRENGTH, tempo=_th, seed=seed)
        drums = humanize_notes(drums, role="drum", strength=_GENERATED_STRENGTH, tempo=_th, seed=seed)

    return {
        "parts": {
            "pad": pad,
            "bass": bass,
            "lead": lead,
            "drums": drums,
        },
        "meta": {
            "key": key_str,
            "tonic": tonic,
            "mode": mode,
            "progression": prog_str,
            "roman_figures": figures,
            "bars": bars,
            "density": density,
            "effective_density": eff_density,
            "seed": seed,
            "scale_pcs": sorted(scale_pcs),
            "pitch_register": [reg_lo, reg_hi],
            "lead_in_scale_pct": round(_in_scale_fraction(lead, scale_pcs) * 100, 1),
            "lead_groove_cosine": round(groove_cos, 3),
            "lead_style_score": round(lead_style_score, 3),
            "note_counts": {k: len(v) for k, v in {
                "pad": pad, "bass": bass, "lead": lead, "drums": drums,
            }.items()},
            "drum_source": drum_source,
            "humanized": humanize,
            "notes_md_excerpt": notes_text.strip()[:2000] if notes_text.strip() else "",
            "notes_md_bias": {
                "sparse": sparse_bias,
                "avoid_dominant": avoid_dom,
            },
            "tempo_hint": agg.get("tempo", {}).get("mean"),
        },
    }


# --- growth suggestions: rare-but-native moves to widen his palette ---------

_DEGREE_LABELS = {0: "1", 1: "b2", 2: "2", 3: "b3", 4: "3", 5: "4", 6: "b5",
                  7: "5", 8: "b6", 9: "6", 10: "b7", 11: "7"}


def _rare_cells(n: int = 2, max_support: int = 12, min_count: int = 25, rng=None) -> list[dict]:
    """Low-support but genuinely-used melodic cells (rare-but-native), spread across
    classes. With an rng, samples the rare pool (fresh moves each round); else rarest-first."""
    cells = _motifs_data().get("cells", [])
    pool = [c for c in cells if 2 <= c.get("support", 0) <= max_support
            and c.get("count", 0) >= min_count and c.get("length", 0) >= 3
            and c.get("class") != "repeat"]  # 'repeat' = static, not a real growth move
    if rng is not None:
        rng.shuffle(pool)
    else:
        pool.sort(key=lambda c: (c["support"], -c["count"]))  # rarest by support, then most-used
    out, seen = [], set()
    for c in pool:
        if c["class"] not in seen:
            seen.add(c["class"])
            out.append(c)
    for c in pool:  # backfill if not enough distinct classes
        if len(out) >= n:
            break
        if c not in out:
            out.append(c)
    return out[:n]


def _underused_degrees(n: int = 1, scale_pcs: set[int] | None = None,
                       tonic_idx: int = 0, rng=None) -> list[int]:
    """In-scale chroma-offset degrees with the lowest usage probability (dormant
    palette), kept diatonic so v1 suggestions stay in key. With an rng, samples among
    the rarest few for variety across rounds."""
    uni = _motifs_data().get("unigram_degrees", {})
    items = sorted(((int(k), v) for k, v in uni.items()), key=lambda kv: kv[1])
    cand = [deg for deg, _p in items
            if deg != 0 and (scale_pcs is None or ((tonic_idx + deg) % 12) in scale_pcs)]
    if not cand:
        return []
    if rng is not None:
        pool = cand[:4]  # the rarest few in-key degrees
        rng.shuffle(pool)
        return pool[:n]
    return cand[:n]


def _rare_progressions(n: int = 1, exclude_top: int = 8, rng=None) -> list[str]:
    """Low-count-but-present Roman progressions, excluding his most common ones.
    With an rng, samples the rare tail (fresh each round); else rarest-first."""
    progs = _harmony_data().get("progressions", [])
    tail = progs[exclude_top:] if len(progs) > exclude_top else progs
    if not tail:
        return []
    if rng is not None:
        picks = rng.sample(tail, min(n, len(tail)))
    else:
        picks = sorted(tail, key=lambda pc: pc[1])[:n]  # rarest first
    return [p for p, _c in picks]


def _realize_growth_core(
    degrees: list[int],
    key: str,
    bars: int = 4,
    seed: int = 0,
    *,
    start_pitch: int | None = None,
    onset_offset: float = 0.0,
    groove: list[float] | None = None,
    register: tuple[int, int] | None = None,
) -> list[dict]:
    """Give a (rare) move a fair, musical hearing: STATE the cell, then continue in his
    own lead grammar (so it flows, not loops), RESTATE it ~60% through, and LAND on the
    tonic. His rhythm + register; snapped in-scale by construction.

    Keyword args let a caller anchor the line to a seed loop: voice-lead from ``start_pitch``
    instead of the register midpoint, shift every onset by ``onset_offset`` beats (so the
    line lands AFTER the seed), and override ``groove``/``register`` to match the seed rather
    than the global profile. With all defaults it is the standalone realizer."""
    rng = random.Random(seed)
    agg = _aggregate()
    tonic, mode = _parse_key_string(key)
    tonic_idx = PITCH_NAMES.index(tonic)
    scale_pcs = diatonic_set(tonic_idx, mode)
    if register is None:
        reg_lo, reg_hi = agg.get("pitch_register", [36, 85])
        median = int(agg.get("pitch_median", 58))
        lead_lo, lead_hi = max(reg_lo, median - 12), min(reg_hi, median + 14)
    else:
        lead_lo, lead_hi = register
    hover = (lead_lo + lead_hi) // 2
    if groove is None:
        groove = agg.get("harmonic_groove", [1.0] * 16)
    grammar = (agg.get("melodic_grammar_by_role", {}).get("lead")
               or agg.get("melodic_grammar", {}))
    target = max(len(degrees) + 2, int(round(2.5 * bars)))
    onsets = _sample_groove_onsets(rng, groove, bars, target)
    n = len(onsets)
    L = len(degrees)

    # degree plan: state the cell, restate ~60% through, land on the tonic, grammar-fill the rest
    plan: list[int | None] = [None] * n
    for i in range(min(L, n)):
        plan[i] = degrees[i]
    mid = int(n * 0.6)
    for j in range(L):
        if mid + j < n - 1:
            plan[mid + j] = degrees[j]
    if n:
        plan[-1] = 0  # land on the tonic
    prev_state = prev2_state = None
    for i in range(n):
        if plan[i] is None:
            plan[i] = _sample_melodic_state(rng, grammar, prev_state, prev2_state)
        prev2_state, prev_state = prev_state, plan[i]

    notes, prev = [], (hover if start_pitch is None else int(start_pitch))
    for beat, deg in zip(onsets, plan):
        pitch = _degree_to_pitch(deg, prev, tonic_idx, scale_pcs, lead_lo, lead_hi, hover)
        strong = int(round((beat % BEATS_PER_BAR) * 4)) % 16 == 0
        notes.append({"pitch": pitch, "start_beats": round(beat + onset_offset, 6),
                      "duration_beats": 0.5 if strong else _profile_duration_beats(rng, agg),
                      "velocity": 76 if strong else 68})
        prev = pitch
    return _enforce_monophony(notes)


def _realize_growth_lead(degrees: list[int], key: str, bars: int = 4, seed: int = 0) -> list[dict]:
    """Standalone realization of a rare move (register-midpoint start, profile groove)."""
    return _realize_growth_core(degrees, key, bars=bars, seed=seed)


def _shift_notes(notes: list[dict], by: float) -> list[dict]:
    """Shift mcp-shape notes later by ``by`` beats (place a phrase after another)."""
    return [{**nt, "start_beats": round(nt.get("start_beats", nt.get("start", 0.0)) + by, 6)}
            for nt in notes]


def _extend_growth_lead(seed_nd: list[dict], degrees: list[int], key_str: str,
                        cont_bars: int, agg: dict, seed: int = 0) -> list[dict]:
    """Realize a rare move as a continuation that grows out of ``seed_nd``: voice-led from
    the seed's last pitch, onsets shifted to the bar after the seed ends, in the seed's own
    register and groove — so it reads as the same loop continuing, not a reset."""
    if not seed_nd:
        return _realize_growth_core(degrees, key_str, bars=cont_bars, seed=seed)
    last = max(seed_nd, key=lambda nt: (nt["start"], nt["pitch"]))
    end_pitch = int(last["pitch"])
    onset_offset = math.ceil(_phrase_span(seed_nd) / BEATS_PER_BAR) * BEATS_PER_BAR
    reg_lo, reg_hi = agg.get("pitch_register", [36, 85])
    median = int(agg.get("pitch_median", 58))
    prof_lo, prof_hi = max(reg_lo, median - 12), min(reg_hi, median + 14)
    seed_pitches = [int(nt["pitch"]) for nt in seed_nd]
    lead_lo = max(prof_lo, min(min(seed_pitches), end_pitch))
    lead_hi = min(prof_hi, max(max(seed_pitches), end_pitch))
    if lead_lo >= lead_hi:  # degenerate seed register → fall back to his lead register
        lead_lo, lead_hi = prof_lo, prof_hi
    seed_groove = _groove_grid(seed_nd) if len(seed_nd) >= 4 else None
    return _realize_growth_core(
        degrees, key_str, bars=cont_bars, seed=seed,
        start_pitch=end_pitch, onset_offset=onset_offset,
        groove=seed_groove, register=(lead_lo, lead_hi),
    )


def _growth_rating_candidate(suggestion: dict, *, index: int,
                             key: str, prefix: str) -> dict:
    dimension = str(suggestion.get("dimension", "")).lower()
    is_harmony = dimension.startswith("harmony")
    candidate = {
        "id": f"{prefix}-{index + 1}",
        "role": "harmony" if is_harmony else "lead",
        "source": "harmony" if is_harmony else "lead",
        "key": key,
        "notes": suggestion.get("notes") or [],
        "dimension": suggestion.get("dimension"),
        "the_move": suggestion.get("the_move"),
        "why": suggestion.get("why"),
    }
    if suggestion.get("backing"):
        candidate["backing"] = suggestion["backing"]
    return {k: v for k, v in candidate.items() if v is not None}


def suggest_growth(n: int = 3, key: str | None = None, seed: int = 0, *,
                   use_ears: bool | None = None, direction: str | None = None,
                   moment: dict | None = None) -> dict:
    """Suggest rare-but-native moves just outside his habits, each as auditionable MIDI.

    Sources: low-support melodic cells, under-used scale degrees, and rare progressions —
    all present in his catalog but infrequent. The candidates plug straight into
    rate_candidates() so his pick trains the taste model with genuinely varied options."""
    agg = _aggregate()
    key_str = key or _default_key_from_profile(agg)
    tonic, mode = _parse_key_string(key_str)
    tonic_idx = PITCH_NAMES.index(tonic)
    scale_pcs = diatonic_set(tonic_idx, mode)
    rng = random.Random(seed)
    suggestions: list[dict] = []

    for i, cell in enumerate(_rare_cells(n=max(1, n - 2), rng=rng)):
        suggestions.append({
            "dimension": "melody (rare cell)",
            "your_default": "your common moves (1·5·b3 climbs and wobbles)",
            "the_move": f"{cell['label']} ({cell['class']})",
            "why": f"a {cell['class']} move you've used in only {cell['support']} tracks — native but dormant",
            "notes": _realize_growth_lead(cell["degrees"], key_str, bars=4, seed=seed + i),
        })

    for j, deg in enumerate(_underused_degrees(n=1, scale_pcs=scale_pcs, tonic_idx=tonic_idx, rng=rng)):
        figure = [0, deg, 7, deg]  # frame the rare degree with home + the 5th so it lands
        suggestions.append({
            "dimension": "melody (rare degree)",
            "your_default": "you lean on 1·b3·4·5·b7",
            "the_move": f"the {_DEGREE_LABELS.get(deg, deg)} (a note you rarely touch)",
            "why": f"the {_DEGREE_LABELS.get(deg, deg)} is one of your least-used in-key notes",
            "notes": _realize_growth_lead(figure, key_str, bars=4, seed=seed + 100 + j),
        })

    for k, prog in enumerate(_rare_progressions(n=1, rng=rng)):
        out = compose_from_profile(bars=4, key=key_str, density="mid",
                                   seed=seed + 200 + k, progression=prog)
        suggestions.append({
            "dimension": "harmony (rare progression)",
            "your_default": "VI↔iv / i→II (your signatures)",
            "the_move": prog,
            "why": "a progression you reach for far less than VI↔iv — a different harmonic color",
            "notes": out["parts"]["lead"],
            "backing": {"pad": out["parts"]["pad"], "bass": out["parts"]["bass"]},
        })

    for s in suggestions:
        s["base_score"] = round(score_phrase(s["notes"], role="lead", key=key_str)["overall"], 3)
        s["taste_score"] = round(_taste_score(s["notes"], role="lead", key=key_str), 3)

    result = {
        "key": key_str,
        "count": len(suggestions),
        "suggestions": suggestions,
        "candidates": [
            _growth_rating_candidate(s, index=i, key=key_str, prefix="growth")
            for i, s in enumerate(suggestions)
        ],
        "taste_weight": round(_taste_weight(), 2),
        "hint": "audition the candidates, then rate_candidates(candidates, best=<i>, worst=<j>, reason='...')",
    }
    if use_ears or (use_ears is None and _ears_default()):
        # ``suggestions`` carry MCP-format notes (start_beats), which the ears'
        # density/contour buckets can't read — gate an index-carrying wrapper
        # built from the derived rating candidates (they carry role + key), then
        # realign suggestions AND the index-aligned candidates to the survivors.
        import moment as _moment_mod
        m = moment if moment is not None else _moment_mod.get_moment()
        order = _serve_indexed(result, [
            {"i": i, "role": c.get("role"), "key": c.get("key"),
             "notes": _note_dicts(c.get("notes") or [])}
            for i, c in enumerate(result["candidates"])
        ], role="lead", direction=direction, moment=m)
        result["suggestions"] = [suggestions[i] for i in order]
        result["candidates"] = [result["candidates"][i] for i in order]
        result["count"] = len(order)
        _gate_backing(result, m)
    return result


def grow_my_loop(seed_notes: list[dict], n: int = 3, key: str | None = None,
                 tempo: float = 120.0, cont_bars: int = 4, seed: int = 0,
                 humanize: bool = False, *, use_ears: bool | None = None,
                 moment: dict | None = None) -> dict:
    """Stretch HIS loop: return his idea untouched, each EXTENDED with a short continuation
    that grows out of it and introduces one rare-but-native move — a low-support melodic
    cell, an under-used scale degree, or a rare progression.

    Unlike suggest_growth (which floats rare moves as standalone phrases), every suggestion
    here is anchored to his own material: the continuation is voice-led from the seed's last
    note, sits in the seed's register/groove, and lands after it — so the novelty arrives in
    context, not as a reset. Each suggestion's ``notes`` (seed + continuation) plugs straight
    into rate_candidates() to train the taste model on stretches of his real loops."""
    nd = _normalize_phrase(_note_dicts(seed_notes))
    if not nd:
        raise ValueError("seed_notes list is empty")
    agg = _aggregate()
    if key:
        key_str = key
    else:
        pc_hist = [0.0] * 12
        for note in nd:
            pc_hist[note["pitch"] % 12] += 1
        tonic_d, mode_d, _ = estimate_key(pc_hist)
        key_str = f"{tonic_d} {mode_d}"
    tonic, mode = _parse_key_string(key_str)
    tonic_idx = PITCH_NAMES.index(tonic)
    scale_pcs = diatonic_set(tonic_idx, mode)
    rng = random.Random(seed)
    seed_mcp = _notes_to_mcp(nd)
    seed_end_beat = math.ceil(_phrase_span(nd) / BEATS_PER_BAR) * BEATS_PER_BAR
    suggestions: list[dict] = []

    for i, cell in enumerate(_rare_cells(n=max(1, n - 2), rng=rng)):
        suggestions.append({
            "dimension": "melody (rare cell)",
            "your_default": "your loop's own moves",
            "the_move": f"{cell['label']} ({cell['class']})",
            "why": f"grows your loop with a {cell['class']} move you've used in only "
                   f"{cell['support']} tracks — native but dormant",
            "continuation": _extend_growth_lead(nd, cell["degrees"], key_str, cont_bars,
                                                 agg, seed=seed + i),
        })

    for j, deg in enumerate(_underused_degrees(n=1, scale_pcs=scale_pcs,
                                               tonic_idx=tonic_idx, rng=rng)):
        figure = [0, deg, 7, deg]  # frame the rare degree with home + the 5th so it lands
        suggestions.append({
            "dimension": "melody (rare degree)",
            "your_default": "the notes your loop leans on",
            "the_move": f"the {_DEGREE_LABELS.get(deg, deg)} (a note you rarely touch)",
            "why": f"extends your loop toward the {_DEGREE_LABELS.get(deg, deg)}, one of "
                   f"your least-used in-key notes",
            "continuation": _extend_growth_lead(nd, figure, key_str, cont_bars,
                                                 agg, seed=seed + 100 + j),
        })

    for k, prog in enumerate(_rare_progressions(n=1, rng=rng)):
        out = compose_from_profile(bars=cont_bars, key=key_str, density="mid",
                                   seed=seed + 200 + k, progression=prog)
        suggestions.append({
            "dimension": "harmony (rare progression)",
            "your_default": "VI↔iv / i→II (your signatures)",
            "the_move": prog,
            "why": f"continues your loop through {prog} — a progression you reach for far "
                   f"less than VI↔iv, a different harmonic color",
            "continuation": _shift_notes(out["parts"]["lead"], seed_end_beat),
            "backing": {"pad": _shift_notes(out["parts"]["pad"], seed_end_beat),
                        "bass": _shift_notes(out["parts"]["bass"], seed_end_beat)},
        })

    for s in suggestions:
        if humanize:   # continuation only — the seed is his real playing, never touched
            s["continuation"] = humanize_notes(s["continuation"], role="lead",
                                               strength=_GENERATED_STRENGTH, tempo=tempo, seed=seed)
            for note in s["continuation"]:
                note["start_beats"] = max(note["start_beats"], seed_end_beat)
        s["seed_notes"] = seed_mcp
        s["notes"] = seed_mcp + s["continuation"]
        s["base_score"] = round(score_phrase(s["notes"], role="lead", key=key_str)["overall"], 3)
        s["taste_score"] = round(_taste_score(s["notes"], role="lead", key=key_str), 3)

    result = {
        "key": key_str,
        "tempo": tempo,
        "seed_summary": {
            "n_notes": len(nd),
            "span_beats": round(_phrase_span(nd), 4),
            "detected_key": key_str,
            "continuation_starts_beat": round(seed_end_beat, 4),
        },
        "count": len(suggestions),
        "suggestions": suggestions,
        "candidates": [
            _growth_rating_candidate(s, index=i, key=key_str, prefix="grow")
            for i, s in enumerate(suggestions)
        ],
        "taste_weight": round(_taste_weight(), 2),
        "hint": "audition each suggestion's `notes` (your loop + its continuation), then "
                "rate_candidates(candidates, best=<i>, worst=<j>, reason='...')",
    }
    if use_ears or (use_ears is None and _ears_default()):
        # no lead serving/gating here — only keep the backing off the room's own
        # bass/pad (F3b). Resolve the moment once and reuse it in _gate_backing.
        import moment as _moment_mod
        m = moment if moment is not None else _moment_mod.get_moment()
        _gate_backing(result, m)
    return result


# --- call-and-response jam: answer from REAL phrases ---------------------------
# The answer is retrieved from his catalog — gold anchors, real lead phrases, and
# take-lane improvisations — never generated. The Markov grammar appears ONLY inside
# the is_mechanical junk floor (an ear-validated retrieval FILTER, not a score).

def _phrase_tonic_pc(nd: list[dict]) -> int:
    """Most-common pitch class, ties → lowest (the quarantine-scan convention)."""
    counts = Counter(n["pitch"] % 12 for n in nd)
    top = max(counts.values())
    return min(pc for pc, c in counts.items() if c == top)


def _bank_tonic_pc(p: dict, nd: list[dict]) -> int:
    """Junk-floor tonic: the phrase's project key when it carries one, else
    the phrase's own pitch-class mode. Degrees read against the wrong tonic
    look chromatic either way — the project's key is the honest one."""
    key = _project_keys().get(p.get("project", ""), "")
    if key:
        try:
            tonic, _mode = _parse_key_string(key)
            return PITCH_NAMES.index(tonic)
        except (ValueError, IndexError):
            pass
    return _phrase_tonic_pc(nd)


# Unvalidated signal, OFF until ears check it: the project-key tonic plus
# the exact-period rule gate +376/-12 lead phrases vs base (and pass 12
# quarantined ids). Flip to True only after an ear pass over
# junk_floor_v2_report(); the flag-off bank is byte-identical to base.
JUNK_FLOOR_V2 = False


def _junk_gate(p: dict, nd: list[dict], grammar: dict) -> bool:
    """The answer-bank junk gate, honouring JUNK_FLOOR_V2. Off (base): own
    pitch-class tonic, no period rule. On: project-key tonic plus the
    exact period-<=2 rule. Pure."""
    pn = [{"pitch": n["pitch"], "start": n["start"]} for n in nd]
    if JUNK_FLOOR_V2:
        degs = phrase_to_degrees(pn, _bank_tonic_pc(p, nd))
        return len(degs) >= 2 and is_mechanical(degs, grammar)
    degs = phrase_to_degrees(pn, _phrase_tonic_pc(nd))
    return len(degs) >= 2 and is_mechanical(
        degs, grammar, period_rule=False)


def junk_floor_v2_report(role: str = "lead", pool: list[dict] | None = None,
                         grammar: dict | None = None) -> dict:
    """Ear-check sheet for the JUNK_FLOOR_V2 signal: every candidate the
    project-key tonic + period rule would gate differently from base.
    ``newly_gated``/``newly_ungated`` are phrase ids; ``periodic`` names
    the newly-gated ones the exact-period rule caught. Pure (reads only)."""
    if grammar is None:
        agg = _aggregate()
        grammar = (agg.get("melodic_grammar_by_role", {}).get(role)
                   or agg.get("melodic_grammar", {}))
    if pool is None:
        pool = [p for p in _phrases()
                if p.get("kind") == "harmonic" and not p.get("_gold")
                and _bank_role_match(p.get("role"), p.get("clip_source"),
                                     role)]
    newly_gated: list[str] = []
    newly_ungated: list[str] = []
    periodic: list[str] = []
    for p in pool:
        nd = p.get("notes") or []
        if len(nd) < 4:
            continue
        pn = [{"pitch": n["pitch"], "start": n["start"]} for n in nd]
        degs_base = phrase_to_degrees(pn, _phrase_tonic_pc(nd))
        base = (len(degs_base) >= 2
                and is_mechanical(degs_base, grammar, period_rule=False))
        degs_v2 = phrase_to_degrees(pn, _bank_tonic_pc(p, nd))
        v2 = len(degs_v2) >= 2 and is_mechanical(degs_v2, grammar)
        if v2 and not base:
            newly_gated.append(p.get("id"))
            if mechanical_reason(degs_v2, grammar) == "periodic":
                periodic.append(p.get("id"))
        elif base and not v2:
            newly_ungated.append(p.get("id"))
    return {"flag": JUNK_FLOOR_V2, "role": role,
            "n_candidates": len(pool),
            "newly_gated": newly_gated, "newly_ungated": newly_ungated,
            "periodic": periodic}


def _bank_role_match(rec_role: str | None, clip_source: str | None,
                     role: str) -> bool:
    """The answer-bank role predicate, shared by _answer_bank and
    _gold_bank_records: role match, or lead takes the take-lane pool (his
    improvisations). Filters keep existing order; this never reorders."""
    return rec_role == role or (role == "lead" and clip_source == "take_lane")


def _gold_bank_records(role: str | None = None) -> list[dict]:
    """Gold anchors as retrieval-bank records. When a gold phrase_id exists in the
    index, the index record wins (it carries provenance + the built vector).

    A gold without an index id used to be hard-coded ``role="lead"``, which put
    two sub-C1 bass keeps into the lead answer bank, junk-gate-exempt. Now the
    role comes from the rating; failing that it is inferred from the register
    and the record says which (``role_source``). An index-backed gold honours
    the rating's stored role first (``role_source="stored"``); with none it is
    admitted under the same predicate _answer_bank uses — except an
    UNCLASSIFIED index role ('other', ...), where a gold is an explicit
    endorsement the classifier could not place, so the register decides like
    an index-less gold instead of leaving it unreachable. ``role`` filters
    the result."""
    import moment as _moment_mod

    idx_by_id = phrase_lookup(_phrases())  # pid first, then alias
    out: list[dict] = []
    for g in get_gold_phrases():
        pid = g.get("phrase_id") or f"gold:{_clip_digest(g['notes'])}"
        if pid in idx_by_id:
            rec = {**idx_by_id[pid], "_gold": True}
            stored = str(g.get("role") or "").lower() or None
            if stored:
                rec["role"] = stored
                rec["role_source"] = "stored"
            elif rec.get("role") not in ("lead", "bass", "drum", "pad"):
                nd = sorted(_note_dicts(idx_by_id[pid].get("notes") or []),
                            key=lambda n: (n["start"], n["pitch"]))
                band = _moment_mod.register_band(nd) if nd else "mid"
                rec["role"] = "bass" if band == "low" else "lead"
                rec["role_source"] = "register_inferred"
            else:
                rec["role_source"] = "index"
            if role is None or _bank_role_match(
                    rec.get("role"), rec.get("clip_source"), role):
                out.append(rec)
            continue
        nd = sorted(_note_dicts(g["notes"]), key=lambda n: (n["start"], n["pitch"]))
        if len(nd) < 2:
            continue
        stored = str(g.get("role") or "").lower() or None
        if stored:
            g_role, source = stored, "stored"
        else:
            band = _moment_mod.register_band(nd)
            g_role = "bass" if band == "low" else "lead"
            source = "register_inferred"
        if role is not None and g_role != role:
            continue
        out.append({
            "id": pid, "project": "(gold)", "role": g_role,
            "kind": "drum" if g_role == "drum" else "harmonic",
            # composed, not catalog: no index phrase_id backs this gold, so
            # provenance (and the JAM stamp) must never call it 'catalogue'
            "clip_source": "gold_composed", "tempo": None, "n_notes": len(nd),
            "notes": nd, "vec": phrase_vector(nd), "_gold": True,
            "role_source": source,
        })
    return out


def _answer_bank(grammar: dict, role: str = "lead") -> tuple[list[dict], int, int]:
    """Real-material candidate bank: gold ∪ real phrases of ``role`` (lead also
    takes the take-lane pool, his improvisations), junk-gated by is_mechanical.
    Gold bypasses the gate — his explicit call outranks the junk floor. Returns
    (bank, n_gold, n_gated)."""
    seen: set[str] = set()
    cands: list[dict] = []
    n_gold = 0
    for p in _gold_bank_records(role=role):
        if p["id"] not in seen:
            seen.add(p["id"])
            cands.append(p)
            n_gold += 1
    for p in _phrases():
        if p.get("kind") != "harmonic" or p.get("id") in seen:
            continue
        if _bank_role_match(p.get("role"), p.get("clip_source"), role):
            seen.add(p["id"])
            cands.append(p)
    bank: list[dict] = []
    gated = 0
    for p in cands:
        nd = p.get("notes") or []
        if len(nd) < 4 and not p.get("_gold"):
            continue
        if not p.get("_gold"):
            if _junk_gate(p, nd, grammar):
                gated += 1
                continue
        bank.append(p)
    return bank, n_gold, gated


_GOLD_BONUS = 0.05             # gold lift, applied as a tie-breaker only
_GOLD_TIEBREAK_WINDOW = 0.02  # bonus needs the gold within this of rank-1 ...
_GOLD_TIEBREAK_ABSOLUTE = 0.9  # ... or at/above this absolute cosine


def _rank_with_gold_tiebreak(hits: list[dict], seed_vec: list[float]) -> list[dict]:
    """Rank hits by seed cosine; gold is a tie-breaker, not an attractor.

    The +0.05 gold lift applies only when the gold's cosine sits within
    ``_GOLD_TIEBREAK_WINDOW`` of the rank-1 cosine or at/above
    ``_GOLD_TIEBREAK_ABSOLUTE`` — a gold far behind the best match no longer
    jumps the queue. Both sorts are stable, so ties (and unaided golds) keep
    base-cosine order: existing ranking stays the only ranker."""
    base = [(cosine(seed_vec, p["vec"]), p) for p in hits]
    base.sort(key=lambda t: -t[0])
    rank1 = base[0][0] if base else 0.0
    scored = []
    for cos, p in base:
        bonus = 0.0
        if p.get("_gold") and (rank1 - cos <= _GOLD_TIEBREAK_WINDOW
                               or cos >= _GOLD_TIEBREAK_ABSOLUTE):
            bonus = _GOLD_BONUS
        scored.append((cos + bonus, p))
    scored.sort(key=lambda t: -t[0])
    return [p for _, p in scored]


def _single_gold_window(ranked: list[dict], window: int) -> list[dict]:
    """At most one gold in the pick window: later golds slide just below it.

    Pure reorder of the window head — nothing culled, order otherwise kept,
    so a re-roll still walks real alternatives instead of golds. Golds past
    the window keep their cosine position: no bonus applies out there, so
    lifting them ahead of the tail would re-introduce the attractor through
    the back door."""
    window = max(1, min(window, len(ranked)))
    head, tail = ranked[:window], ranked[window:]
    kept: list[dict] = []
    bumped: list[dict] = []
    seen_gold = False
    for p in head:
        if p.get("_gold"):
            if seen_gold:
                bumped.append(p)
                continue
            seen_gold = True
        kept.append(p)
    # refill the window head with non-gold tail so the bumped golds land
    # strictly below it; the tail itself keeps its order, golds included
    tail_plain = [p for p in tail if not p.get("_gold")]
    filler = tail_plain[:max(0, window - len(kept))]
    filler_ids = {id(p) for p in filler}
    rest = [p for p in tail if id(p) not in filler_ids]
    return kept + filler + bumped + rest


def _clip_key(cid: str | None) -> str | None:
    """Dedupe key: project|clip. Index ids look like 'project|cNN|wNN' — the
    trailing window slice is stripped so one clip serves one window."""
    if cid is None:
        return None
    return re.sub(r"\|w\d+$", "", str(cid))


def _dedupe_ranked_by_clip(ranked: list[dict]) -> list[dict]:
    """One window per clip: keep the first window of each project|clip, drop
    the rest. Pure filter — order kept, so existing ranking still rules."""
    seen: set = set()
    out: list[dict] = []
    for p in ranked:
        # After a rebuild the id is a content hash; the source location lives
        # in the positional alias. Legacy records carry it in the id itself.
        key = _clip_key(p.get("alias") or p.get("id"))
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        out.append(p)
    return out


_SERVED_SEED_COSINE = 0.97  # a served answer this near the seed = same seed


def _served_skip_ids(seed_vec: list[float],
                     seed_notes: list[dict] | None = None) -> set:
    """Source ids already served in a live ledger session from a
    near-identical seed. Seed-to-seed: the incoming seed is compared against
    the seed fingerprint stamped on each row at JAM-write time (digest equal,
    else seed-vector cosine >= 0.97). Seed-to-answer comparison never fires
    on real data — the pick-time transposition keeps it far below threshold.

    Legacy rows without a stamped seed carry no memory and are ignored.
    Sources with a keep verdict stay eligible — a keeper re-serves. The pin
    branch never calls this (pinning replays by construction). Fail-open: a
    ledger read error means no memory, exactly today's behavior."""
    try:
        import ledger as _ledger_mod
        data = _ledger_mod.load()
        now = _ledger_mod._now()
    except Exception:
        return set()
    try:
        live = {s.get("id") for s in _ledger_mod._live_sessions(data, now)}
        if not live:
            return set()
        incoming_fp = None
        if seed_notes is not None:
            try:
                incoming_fp = _ledger_mod.seed_fingerprint(seed_notes)
            except Exception:
                incoming_fp = None
        skip: set = set()
        for e in data.get("entries", []) or []:
            if e.get("session_id") not in live:
                continue
            sid = e.get("source_phrase_id")
            if not sid or e.get("immediate_verdict") == "keep":
                continue
            stored_fp = e.get("seed_fingerprint")
            stored_vec = e.get("seed_vector")
            if not stored_fp and not stored_vec:
                continue  # legacy row: no seed memory, ignored
            if incoming_fp and stored_fp and incoming_fp == stored_fp:
                skip.add(sid)
                continue
            if not stored_vec:
                continue
            try:
                sim = cosine(seed_vec, stored_vec)
            except Exception:
                continue
            if sim >= _SERVED_SEED_COSINE:
                skip.add(sid)
        return skip
    except Exception:
        return set()


def _demote_session_buckets(ranked: list[dict], prospective_fn,
                            seed_tonic_pc: int, seed_last_pitch: int,
                            seed_median: int, session_path=None) -> list[dict]:
    """Move tonight's reason-demoted ears buckets to the back (ears-off path).

    Buckets read off the notes each hit WOULD become (the prospective
    transpose) — the same material the verdict writer heard. Stable
    partition: nothing culled, no score touched. Fail-open: any session
    read error serves the list untouched. The session path resolves at
    call time (load_session binds its default at def time)."""
    try:
        import ears as _ears
        state = _ears.load_session(session_path or _ears.SESSION_PATH)
    except Exception:
        return list(ranked)
    try:
        import ledger as _ledger
        live = _ledger._live_sessions(_ledger.load(), _ledger._now())
        current_session_id = max(
            live, key=lambda session: session.get("last_activity", 0)
        ).get("id") if live else None
    except Exception:
        current_session_id = None
    if state.get("ledger_session_id") != current_session_id:
        return list(ranked)
    # Ears-off serving uses only reason-word demotions. General bucket
    # streaks remain diagnostic for the live ears gate.
    demoted = set(state.get("reason_demoted") or [])
    if not demoted:
        return list(ranked)
    try:
        front: list[dict] = []
        back: list[dict] = []
        for h in ranked:
            try:
                buckets = _ears.candidate_buckets(
                    {"notes": prospective_fn(h, seed_tonic_pc,
                                             seed_last_pitch, seed_median)})
            except Exception:
                front.append(h)
                continue
            keys = {"%s:%s" % (dim, val) for dim, val in buckets.items()}
            (back if keys & demoted else front).append(h)
        return front + back
    except Exception:
        return list(ranked)


def _transpose_answer(cand_nd: list[dict], cand_tonic_pc: int, seed_tonic_pc: int,
                      seed_last_pitch: int, seed_median: int) -> tuple[list[dict], int, int]:
    """Minimal chroma shift into the seed's key, then octave placement so the answer's
    first note joins the seed's last within a tritone; one extra octave nudge toward the
    seed register if the phrase sits wildly off and the join survives (≤ a fifth)."""
    delta = ((seed_tonic_pc - cand_tonic_pc) + 6) % 12 - 6
    shifted = [{**n, "pitch": n["pitch"] + delta} for n in cand_nd]
    k = round((seed_last_pitch - shifted[0]["pitch"]) / 12.0)
    shifted = [{**n, "pitch": n["pitch"] + 12 * k} for n in shifted]
    med = sorted(n["pitch"] for n in shifted)[len(shifted) // 2]
    if abs(med - seed_median) > 12:
        step = 12 if med < seed_median else -12
        nudged = [{**n, "pitch": n["pitch"] + step} for n in shifted]
        med2 = sorted(n["pitch"] for n in nudged)[len(nudged) // 2]
        if (abs(nudged[0]["pitch"] - seed_last_pitch) <= 7
                and abs(med2 - seed_median) < abs(med - seed_median)):
            shifted, k = nudged, k + step // 12
    shifted = [{**n, "pitch": max(0, min(127, n["pitch"]))} for n in shifted]
    return shifted, delta, 12 * k


def _prospective_answer_notes(hit: dict, seed_tonic_pc: int, seed_last_pitch: int,
                              seed_median: int) -> list[dict]:
    """The notes THIS retrieval hit WOULD become after answer transposition — the
    material the ears gates must actually judge (F3a). answer_my_loop transposes
    every pick into the seed's key/register before it is heard, so gating the raw
    catalog register admitted picks whose realized register collides with an
    occupied role. Mirrors the pick-time transpose (lines below) exactly; emits no
    assumptions (realization stays the sole source of those)."""
    cand_nd = sorted(({"pitch": n["pitch"], "start": n["start"],
                       "duration": n.get("duration", 0.25),
                       "velocity": n.get("velocity", 90)} for n in hit.get("notes") or []),
                     key=lambda n: (n["start"], n["pitch"]))
    if not cand_nd:
        return []
    t0 = cand_nd[0]["start"]
    cand_nd = [{**n, "start": n["start"] - t0} for n in cand_nd]
    project_key = _project_keys().get(hit.get("project", ""), "")
    if project_key:
        try:
            c_tonic, _cm = _parse_key_string(project_key)
            cand_tonic_pc = PITCH_NAMES.index(c_tonic)
        except (ValueError, IndexError):
            cand_tonic_pc = _phrase_tonic_pc(cand_nd)
    else:
        cand_tonic_pc = _phrase_tonic_pc(cand_nd)
    answer_nd, _delta, _oct = _transpose_answer(
        cand_nd, cand_tonic_pc, seed_tonic_pc, seed_last_pitch, seed_median)
    return answer_nd


# What a reference SAYS travels with the answer; core forwards these verbatim and
# never reads them, so the audio-ref layer stays out of the composing spine.
_REF_PASSTHROUGH_KEYS = ("brief", "identity", "contract_version", "inherited_from")

_REF_TEMPO_TOLERANCE = 0.12   # ±12% — a window has no dead zone, unlike tempo_world


def _ref_tempo_fits(cand_tempo, ref_tempo) -> bool:
    """True when a catalogue phrase sits at the reference's tempo — or at half or
    double it. A 78 BPM loop of his IS the pocket for a 156 BPM reference; the
    briefs already disagree with themselves about which number to name (46 of 185
    carry a feel/grid split like 52 vs 104), so refusing the octave would throw
    away the right material on a naming technicality."""
    if not cand_tempo or not ref_tempo:
        return True                      # unknown tempo stays admitted, reported
    for ratio in (1.0, 0.5, 2.0):
        if abs(cand_tempo - ref_tempo * ratio) <= ref_tempo * ratio * _REF_TEMPO_TOLERANCE:
            return True
    return False


def _shift_octaves(nd: list[dict], semitones: int) -> list[dict]:
    """Whole-octave transpose, clamped. Identity at 0 so the ref-off path stays
    byte-identical."""
    if not semitones or not nd:
        return nd
    return [{**n, "pitch": max(0, min(127, n["pitch"] + semitones))} for n in nd]


def _ref_register_shift(register: str | None, seed_median: int) -> int:
    """Octave bias from a reference's register. Bounded to ONE octave: a
    reference nudges where his line sits, it never relocates it wholesale."""
    if register == "low" and seed_median >= 60:
        return -12
    if register == "high" and seed_median < 60:
        return 12
    return 0


def _steer_ranked_by_ref(ranked: list[dict], ref: dict, gate_notes_fn) -> tuple[list[dict], list[str]]:
    """Narrow a retrieval list toward a reference. Returns (ranked, announcements).

    Two mechanisms, both auditable, neither a score: a tempo WINDOW on the
    catalogue phrase's own tempo, then the reference's density mods run through
    the ordinary ears gates under an honest ``ears:"reference"`` moment.

    Unlike the room, a reference NEVER vetoes. An empty room means his parts
    would actually collide, so that fails closed; an empty reference match just
    means nothing of his is quite that sparse, and refusing to answer would be
    a worse outcome than answering with the nearest thing and saying so. Every
    fallback is announced — the rule is 'never relax silently', not 'never relax'.
    """
    notes: list[str] = []
    if not ranked:
        return ranked, notes
    ref_tempo = ref.get("tempo")
    if ref_tempo:
        near = [p for p in ranked if _ref_tempo_fits(p.get("tempo"), ref_tempo)]
        if near:
            ranked = near
        else:
            notes.append("nothing of yours sits near %g BPM — keeping the closest "
                         "by shape instead" % ref_tempo)
    mods = ref.get("mods") or {}
    if mods:
        import ears as _ears
        ref_moment = {"ears": "reference", "tempo": ref_tempo, "tempo_world": None,
                      "key": None, "roles": {}}
        cands = [{"id": i, "notes": gate_notes_fn(p), "role": "lead",
                  "key": p.get("key"), "tempo": p.get("tempo")}
                 for i, p in enumerate(ranked)]
        # admit(), not serve(): serve() also reads the ears SESSION file to push
        # demoted buckets to the back. That is the room's memory of tonight, and
        # it has no business reordering a reference pass — least of all on an
        # ears-off call, where a corrupt session file would then fail a request
        # that never asked for ears at all.
        survivors_c, _report = _ears.admit(cands, ref_moment, role="lead",
                                           direction=mods)
        survivors = [ranked[c["id"]] for c in survivors_c]
        if survivors:
            ranked = survivors
        else:
            notes.append("nothing of yours is %s enough for that reference — "
                         "keeping the closest fit instead"
                         % (mods.get("density_max") or "dense"))
    if len(ranked) < 3:
        # A pool this small pins the pick: the chooser walks ranked[seed %
        # depth], so at one survivor `seed` stops changing anything and
        # re-rolling returns the same phrase forever. Announcing only at ZERO
        # survivors made that look like a working narrow instead of a dead end.
        notes.append(
            "that reference narrows your catalogue to %d phrase%s — re-rolling "
            "the seed won't change the answer; loosen it or try a nearer ref"
            % (len(ranked), "" if len(ranked) == 1 else "s"))
    return ranked, notes


# Reference briefs say low/medium/high; _DRUM_DENSITY_TARGETS speaks
# minimal/mid/full. Both vocabularies are load-bearing elsewhere, so translate
# rather than rename either. (In practice his briefs only ever emit low or high.)
_DRUM_DENSITY_ALIASES = {
    "low": "minimal", "sparse": "minimal", "minimal": "minimal",
    "medium": "mid", "mid": "mid", "moderate": "mid",
    "high": "full", "busy": "full", "dense": "full", "full": "full",
}


def beat_from_grid(
    grid_notes: list[dict] | None,
    bars: int = 4,
    tempo: float | None = None,
    density: str | None = None,
    seed: int = 0,
    humanize: bool = True,
) -> dict:
    """A beat from HIS OWN drum phrases, matched to a target onset grid.

    ``grid_notes`` is one bar of target hits (see audio_ref.drum_grid_to_notes);
    it becomes the query vector for the ordinary seeded drum search, so his 4,510
    indexed drum phrases are compared to the reference on the one axis that
    carries "half-time vs straight" — placement. Takes parsed hits rather than
    prose so core.py stays ignorant of the audio-ref layer.

    With no grid this falls back to a tempo-matched pick and says so; it never
    silently synthesizes, because "a beat like that reference, from my stuff"
    turning into a generated groove is exactly the substitution he'd not catch.
    """
    if bars < 1:
        raise ValueError("bars must be >= 1")
    notes_nd = _note_dicts(grid_notes or [])
    hits = find_phrases(kind="drum", seed_notes=notes_nd or None,
                        tempo=tempo, k=8)
    if hits:
        matched = ("grid: matched %d target hits against your drum phrases"
                   % len(notes_nd)) if notes_nd else \
                  "tempo only: that reference had no readable drum description"
    elif notes_nd:
        # Drop the TEMPO, keep the GRID. Placement is what makes a beat feel like
        # the reference; tempo is a clip property he changes in one gesture.
        # Falling straight through to the corpus spread threw away the good half.
        hits = find_phrases(kind="drum", seed_notes=notes_nd, k=8)
        matched = ("grid, tempo relaxed: nothing of yours sits near %s BPM — "
                   "matched on placement alone"
                   % (("%g" % tempo) if tempo else "?"))
    if not hits:
        hits = find_phrases(kind="drum", k=8)
        if not hits:
            raise ValueError(
                "no drum phrases in the index — rebuild analyze/build_index.py")
        matched = "none: nothing matched that reference, using the corpus spread"
    pick = hits[seed % len(hits)]
    src = _normalize_phrase(_note_dicts(pick["notes"]))
    tiled = _tile_phrase_to_bars(src, bars)
    applied = [f"phrase:{pick.get('id')}", f"tile:{bars}bars"]
    # Thin ONLY when no grid drove the pick. _drum_hit_priority re-weights toward
    # a stock backbeat (slot 0 / 8 / 4 / 12), so thinning a phrase that was
    # retrieved FOR its placement would discard the very thing it was chosen for.
    if density and not notes_nd:
        # _DRUM_DENSITY_TARGETS speaks minimal|mid|full; reference briefs say
        # low|medium|high. Passing a ref word straight in hit the silent
        # `.get(..., "mid")` default, so low/medium/high all thinned identically
        # while transforms_applied printed "thin:low→N" as though it hadn't.
        # Translate at the boundary and refuse to label a thin that didn't happen.
        want = _DRUM_DENSITY_ALIASES.get(str(density).strip().lower())
        if want is None:
            assumptions_extra = "density %r isn't a density word — left untouched" % density
            applied.append("thin:skipped")
        else:
            assumptions_extra = None
            _rng = random.Random(f"beat:{seed}")
            target = _drum_hit_target(want, bars, _rng)
            if target < len(tiled):
                tiled = _thin_drum_notes(tiled, target, _rng)
                applied.append(f"thin:{density}→{want}→{target}")
    else:
        assumptions_extra = None
    out = _notes_to_mcp(sorted(tiled, key=lambda n: (n["start"], n["pitch"])))
    if humanize:
        out = humanize_notes(out, role="drum", strength=_REAL_STRENGTH,
                             tempo=tempo, seed=seed)
        applied.append("humanize:drum-dna")
    return {
        "notes": out,
        "bars": bars,
        "tempo": float(tempo or pick.get("tempo") or 120.0),
        "source_phrase_ids": [pick.get("id")],
        "source_provenance": {"project": pick.get("project"),
                              "track": pick.get("track"),
                              "clip_source": pick.get("clip_source"),
                              "tempo": pick.get("tempo")},
        "match": matched,
        "transforms_applied": applied,
        "alternatives": [h.get("id") for h in hits[:5]],
        "assumptions": [
            "every note is from YOUR drum clips — nothing generated",
            "Basic Pitch never touched this: the grid came from reference steering evidence",
        ] + ([assumptions_extra] if assumptions_extra else []),
    }


def _prospective_with_transform(
    transform_apply: Callable[[list[dict]], list[dict]],
) -> Callable[[dict, int, int, int], list[dict]]:
    """Compose a post-transpose transform into the ears prospective (develop pattern).

    Without this, answer_my_loop(transform=…, use_ears=True) gates the plain
    transpose then serves fragment/reharmonize notes the room never saw.
    """
    def _fn(hit: dict, seed_tonic_pc: int, seed_last_pitch: int,
            seed_median: int) -> list[dict]:
        base = _prospective_answer_notes(
            hit, seed_tonic_pc, seed_last_pitch, seed_median)
        return transform_apply(base) if base else []
    return _fn


_ANSWER_TRANSFORMS = ("fragment", "reharmonize")   # provably velocity-preserving


def _answer_worker(
    seed_notes: list[dict],
    *,
    key: str | None,
    tempo: float | None,
    bars: int | None,
    seed: int,
    humanize: bool,
    transform_fn: tuple[str, Callable[[list[dict]], list[dict]]] | None,
    prospective_fn: Callable[[dict, int, int, int], list[dict]],
    use_ears: bool | None,
    direction: str | None,
    moment: dict | None,
    pin_source: str | None = None,
    n_candidates: int = 5,
    ref: dict | None = None,
    role: str = "lead",
    relax: str | None = None,
    move: str | None = None,
) -> dict:
    """Shared spine behind answer_my_loop and develop_my_loop: retrieve ONE real
    phrase near ``seed_notes`` from HIS catalog (gold anchors boosted, junk gated
    out), transpose it minimally into the seed's key and register, voice-lead the
    join off the seed's last note, optionally apply ONE caller-supplied
    ``transform_fn`` (a (label, fn) pair), then finalize (tile/clip to ``bars``,
    optional humanize, tempo resolution, full provenance). ``prospective_fn`` is
    what the ears gate judges each candidate on — mirrors the pick-time transpose
    (F3a) for the move that will actually run. ``pin_source``, when given a phrase
    id, forces the pick to that exact phrase (from the post-gate ``ranked`` list
    if it survived, else looked up directly in the bank); an id present in
    neither raises. Real velocities and micro-timing ride along untouched.
    Deterministic given ``seed``/``pin_source``; never writes anywhere."""
    nd = _normalize_phrase(_note_dicts(seed_notes))
    if len(nd) < 3 or _phrase_span(nd) < 2.0:
        raise ValueError("seed too short — play at least ~3 notes across a bar so the answer has something to talk to")
    assumptions: list[str] = []

    if key:
        key_str = key
        assumptions.append(f"key {key_str} (caller-provided)")
    else:
        pc_hist = [0.0] * 12
        for n in nd:
            pc_hist[n["pitch"] % 12] += 1
        tonic_d, mode_d, _ = estimate_key(pc_hist)
        key_str = f"{tonic_d} {mode_d}"
        assumptions.append(f"key {key_str} detected from the seed (short seeds detect unreliably — pass key= to pin it)")
    tonic, _mode = _parse_key_string(key_str)
    seed_tonic_pc = PITCH_NAMES.index(tonic)

    seed_sorted = sorted(nd, key=lambda n: (n["start"], n["pitch"]))
    seed_last_pitch = max(nd, key=lambda n: (n["start"], n["pitch"]))["pitch"]
    seed_median = sorted(n["pitch"] for n in nd)[len(nd) // 2]
    # A reference's register biases octave placement. Applied as an explicit
    # shift wrapped around BOTH the prospective and the realized transpose (one
    # function, called twice) rather than by nudging seed_median, which only
    # reaches _transpose_answer's conditional nudge branch and so moved the notes
    # or didn't depending on the phrase. Gate and final stay identical by
    # construction — the bug class this codebase keeps closing.
    _reg_shift = 0
    if ref is not None:
        _reg_shift = _ref_register_shift(ref.get("register"), seed_median)
        if _reg_shift:
            assumptions.append(
                "register nudged %+d semitones toward the reference's %s register"
                % (_reg_shift, ref.get("register")))
            _base_prospective = prospective_fn

            def prospective_fn(hit, tpc, lp, med, _f=_base_prospective, _s=_reg_shift):
                return _shift_octaves(_f(hit, tpc, lp, med), _s)
    seed_vec = phrase_vector(nd)
    seed_end_beat = math.ceil(_phrase_span(nd) / BEATS_PER_BAR) * BEATS_PER_BAR
    out_bars = int(bars) if bars else max(1, math.ceil(_phrase_span(nd) / BEATS_PER_BAR))

    role = _normalize_role(role) or "lead"
    if role not in ("lead", "bass"):
        raise ValueError("role must be 'lead' or 'bass' — an answer is a line, not a pad or a kit")
    agg = _aggregate()
    grammar = agg.get("melodic_grammar_by_role", {}).get(role) or agg.get("melodic_grammar", {})
    bank, n_gold, n_gated = _answer_bank(grammar, role=role)
    if not bank:
        raise ValueError("answer bank is empty — rebuild the phrase index (analyze/build_index.py) or gold a few phrases first")

    # A reference narrows this pool twice more (tempo window, then density), so
    # retrieve wider up front. Without this the ref post-filters ~15 seed-nearest
    # phrases down to one, and the pick index below collapses to a constant —
    # `seed` stops varying the answer, and the ref reads as a veto, not a nudge.
    _k_hits = max(n_candidates * 3, 9) * (4 if ref is not None else 1)
    hits = search(bank, seed_vec, k=_k_hits, kind="harmonic",
                  diversify=True, max_sim=0.97)
    if not hits:
        raise ValueError("no real phrase is near this seed — try a longer seed or loosen n_candidates")
    ranked = _rank_with_gold_tiebreak(hits, seed_vec)
    # What he said no to, before the room. A reject is a fact about HIM, not
    # about the room: it holds whether or not Live is up (the ears layer is off
    # by default, so an ears-only cull would never run), and relaxing a gate
    # must never resurrect one. The pick below walks ranked[seed % depth], so
    # at his usual seed it is always ranked[0] — culling it is what slides
    # the list and makes yesterday's second choice today's first.
    _n_pool_pre_cull = len(ranked)
    # the move being served scopes moved DROPs to their pairing: a reject of
    # (source, move) culls only that pairing, never the untouched source
    _serving_move = _canonical_move(move)
    if _serving_move is None and transform_fn is not None:
        _serving_move = _canonical_move(transform_fn[0])
    ranked, _suppressed = reject_cull(ranked, move=_serving_move)
    _rejects_meta = {
        "suppressed": _suppressed,
        "pool_before": _n_pool_pre_cull,
        "pool_after": len(ranked),
        "undo": "clear_rejects(phrase_id=…) puts it back",
    }
    if _suppressed:
        assumptions.append(
            "suppressed %d phrase%s you dropped earlier (%s) — clear_rejects() undoes it"
            % (len(_suppressed), "" if len(_suppressed) == 1 else "s",
               ", ".join(str(s["id"]) for s in _suppressed[:3])))
    if not ranked:
        # every candidate near this seed is something he dropped — refuse with
        # the same no_fit shape as the ears/ref refusals below (never serve a
        # reject, never crash on ranked[seed % ...]).
        assumptions.append(
            "everything near this seed is something you dropped — "
            "clear_rejects() undoes it, or play a different seed")
        return {
            "no_fit": True,
            "role": role,
            "answer_notes": [],
            "key": key_str,
            "bars": out_bars,
            "bank_stats": {"n_bank": len(bank), "n_gold": n_gold,
                          "n_gated_mechanical": n_gated,
                          "n_rejected": len(_rejects_meta["suppressed"])},
            "rejects": _rejects_meta,
            "assumptions": assumptions,
        }
    _ranked_pre_gate = list(ranked)
    _ears_extra: dict | None = None
    _want_ears = bool(use_ears or (use_ears is None and _ears_default()))
    if relax and not _want_ears:
        raise ValueError("relax needs use_ears — there is no gate to relax context-blind")
    if _want_ears:
        # the result carries ONE answer, not a candidate list — the pool the
        # ears gate is the internal ``ranked`` retrieval list (serve-shaped
        # index records), BEFORE the pick. ``direction`` here is the gearbox's
        # wider vocabulary (see server.py's answer_my_loop / _DIRECTION_POLES);
        # only the ears words bend the gates, so translate at the boundary
        # instead of forwarding the raw word into ears.admit (spec fix round 1).
        _canonical_direction = str(direction).lower() if direction is not None else None
        _resolved_direction = _EARS_DIRECTION_ALIASES.get(_canonical_direction)
        _gearbox_only = False
        if direction and _resolved_direction is None:
            # a word that maps to neither the ears' gate vocabulary nor the wider
            # gearbox poles must fail loud, never fall open as a phantom "gearbox
            # word" that leaves the gates silently unbent (F8b).
            import gears as _gears
            if _canonical_direction in _gears._DIRECTION_POLES:
                _gearbox_only = True
            else:
                raise ValueError(
                    "unknown direction %r — ears words: %s; gearbox words: %s"
                    % (direction, "/".join(sorted(_EARS_DIRECTION_ALIASES)),
                       "/".join(sorted(_gears._DIRECTION_POLES))))
        # F3a: gate each hit on the notes it WOULD become after transposition, not
        # its raw catalog register. Carry the index back so survivors realign to the
        # original ranked records (realization uses those unchanged).
        _gate_pool = [
            {**h, "notes": prospective_fn(
                h, seed_tonic_pc, seed_last_pitch, seed_median), "i": i}
            for i, h in enumerate(ranked)
        ]
        _ears_extra = {}
        _survivors_i = _serve_indexed(_ears_extra, _gate_pool, role=role,
                                      direction=_resolved_direction, moment=moment,
                                      relax=relax)
        if _gearbox_only:
            # gearbox-only word (e.g. brighter/song/maximal): a no-op for the gates
            # — say so instead of pretending nothing happened (spec fix round 7)
            _ears_extra["announcements"].append(
                "direction '%s' is a gearbox word — gates unbent" % _canonical_direction)
        if (_ears_extra["ears"] == "live"
                and _ears_extra["gate_report"].get("survived") == 0):
            # empty survivor set on a live moment → explicit refusal with the
            # nearest single-gate relaxation offered as a question (spec §8);
            # never silently relax a gate by serving the ungated pick instead.
            # Blind moments never reach here — serve() always returns the full
            # pool, declared, when the room can't be heard.
            result = {
                "no_fit": True,
                "role": role,
                "answer_notes": [],
                "key": key_str,
                "bars": out_bars,
                "bank_stats": {"n_bank": len(bank), "n_gold": n_gold,
                              "n_gated_mechanical": n_gated,
                              "n_rejected": len(_rejects_meta["suppressed"])},
                "rejects": _rejects_meta,
                "assumptions": assumptions,
            }
            result.update(_ears_extra)
            return result
        if _survivors_i:
            # realign to the ORIGINAL records in the gated (demotion-aware) order —
            # the gate saw transposed copies, but the pick transposes for real below.
            ranked = [ranked[i] for i in _survivors_i]
    else:
        # ears off: tonight's session memory still counts. Reason-demoted
        # buckets slide to the back (never a ban) so a "too static" DROP
        # keeps working when the room is off. Fail-open: an unreadable
        # session file must not fail a request that never asked for ears.
        _pre_demoted = list(ranked)
        ranked = _demote_session_buckets(ranked, prospective_fn,
                                         seed_tonic_pc, seed_last_pitch,
                                         seed_median)
        if len(ranked) == len(_pre_demoted) and any(
                a.get("id") != b.get("id")
                for a, b in zip(ranked, _pre_demoted)):
            assumptions.append(
                "session memory moved reason-demoted buckets to the back "
                "(ears off — demoted, never banned)")
    if ref is not None:
        # AFTER the room, deliberately. The room decides what would collide with
        # what he is actually hearing; the reference only narrows what survived
        # that. Running it first would let an aspiration reshape the pool the room
        # never got to judge.
        ranked, _ref_notes = _steer_ranked_by_ref(
            ranked, ref,
            lambda p: prospective_fn(p, seed_tonic_pc, seed_last_pitch, seed_median))
        _ref_meta = {
            "ref_id": ref.get("ref_id"),
            "steering": "gates",
            "applied": {k: v for k, v in (("tempo", ref.get("tempo")),
                                          ("density", (ref.get("mods") or {})),
                                          ("register", ref.get("register")))
                        if v},
            "unmapped": ref.get("unmapped") or [],
            "notes": _ref_notes,
        }
        # Forward what the reference SAYS without interpreting it: core stays
        # ignorant of the audio-ref layer, so this is an allowlist, not logic.
        for _k in _REF_PASSTHROUGH_KEYS:
            if ref.get(_k):
                _ref_meta[_k] = ref[_k]
        if ref.get("mode_hint"):
            _ref_meta["mode_hint"] = ref["mode_hint"]
        if not any(ref.get(k) for k in ("tempo", "register")) and not ref.get("mods"):
            # Route-aware on purpose: `usable` is true for a ref that only has a
            # drum_grid, but a drum grid steers nothing on the MELODIC path. A
            # drums-only reference must read as "prose only" here, not as a
            # working narrow that quietly did nothing.
            _ref_notes.append(
                "that reference has no readable tempo/density/register for a "
                "melodic answer — steering by its prose only")
        assumptions.extend(_ref_notes)
    if pin_source is not None:
        if (_ears_extra is not None and _ears_extra.get("ears") == "live"
                and any(p.get("id") == pin_source for p in _ranked_pre_gate)
                and not any(p.get("id") == pin_source for p in ranked)):
            # pinned id was in the pre-gate pool but gated out by a live room —
            # refuse, never bypass the gates via the bank fallback (T11 / T2).
            result = {
                "no_fit": True,
                "role": role,
                "answer_notes": [],
                "key": key_str,
                "bars": out_bars,
                "bank_stats": {"n_bank": len(bank), "n_gold": n_gold,
                              "n_gated_mechanical": n_gated,
                              "n_rejected": len(_rejects_meta["suppressed"])},
                "rejects": _rejects_meta,
                "assumptions": assumptions,
            }
            result.update(_ears_extra)
            return result
        pick = next((p for p in ranked if p.get("id") == pin_source), None)
        if pick is None:
            # below the search cut, not gated out — pinning means THAT phrase,
            # so fall back to the full bank before giving up on it.
            pick = next((p for p in bank if p.get("id") == pin_source), None)
        if pick is None:
            raise ValueError(f"pin_source {pin_source!r} not found in the bank")
    else:
        # Re-roll walks the deduped list. One window per clip (a second slice
        # of the same clip is never a "new" answer), then seed k serves
        # ranked[k] within the pick depth below instead of cycling the top
        # 3 — seeds 0..5 walk six distinct clips. Sources already served in
        # this ledger session for a near-identical seed are skipped
        # (keepers and pins excepted) and named in assumptions. None of
        # this re-ranks: the order is untouched.
        ranked = _dedupe_ranked_by_clip(ranked)
        # survival memory: tried-and-abandoned sources serve last — a
        # back-move, not a ban, and pins bypass it by construction
        ranked = _demote_to_back(ranked, served_not_kept_identities())
        _pick_window = 8 if ref is not None else 3
        ranked = _single_gold_window(ranked, _pick_window)
        _skip_ids = _served_skip_ids(seed_vec, seed_notes)
        if _skip_ids:
            _pre_skip = list(ranked)
            ranked = [p for p in ranked if p.get("id") not in _skip_ids]
            _n_skipped = len(_pre_skip) - len(ranked)
            if _n_skipped and ranked:
                assumptions.append(
                    "skipped %d phrase%s already served in this session for "
                    "a near-identical seed (%s) — pin_source replays one"
                    % (_n_skipped, "" if _n_skipped == 1 else "s",
                       ", ".join(str(p.get("id"))
                                 for p in _pre_skip
                                 if p.get("id") in _skip_ids)))
            elif _n_skipped:
                ranked = _pre_skip  # memory yields to serving: never refuse
        # Bounded re-roll depth: seed k walks the first 6 (8 with a
        # reference steering a wider pool) — seeds 0..5 still serve six
        # distinct answers when the list allows, and deeper seeds wrap
        # instead of digging past the relevance horizon.
        _pick_depth = min(len(ranked), 8 if ref is not None else 6)
        pick = ranked[seed % max(1, _pick_depth)]
    similarity = cosine(seed_vec, pick["vec"])

    cand_nd = sorted(({"pitch": n["pitch"], "start": n["start"],
                       "duration": n.get("duration", 0.25),
                       "velocity": n.get("velocity", 90)} for n in pick["notes"]),
                     key=lambda n: (n["start"], n["pitch"]))
    t0 = cand_nd[0]["start"]
    cand_nd = [{**n, "start": n["start"] - t0} for n in cand_nd]

    project_key = _project_keys().get(pick.get("project", ""), "")
    cand_tonic_pc: int
    if project_key:
        try:
            c_tonic, _cm = _parse_key_string(project_key)
            cand_tonic_pc = PITCH_NAMES.index(c_tonic)
            assumptions.append(f"source tonic from its project key ({project_key})")
        except (ValueError, IndexError):
            cand_tonic_pc = _phrase_tonic_pc(cand_nd)
            assumptions.append("source tonic from its own pitch-class mode (project key unparseable)")
    else:
        cand_tonic_pc = _phrase_tonic_pc(cand_nd)
        assumptions.append("source tonic from its own pitch-class mode (no project key)")

    answer_nd, delta, oct_shift = _transpose_answer(
        cand_nd, cand_tonic_pc, seed_tonic_pc, seed_last_pitch, seed_median)
    applied = [f"transpose:{delta:+d}", f"octave:{oct_shift:+d}"]
    if _reg_shift:
        # the SAME shift the gate pool already saw (see prospective_fn above)
        answer_nd = _shift_octaves(answer_nd, _reg_shift)
        applied.append(f"ref_register:{_reg_shift:+d}")

    if transform_fn is not None:
        transform_label, transform_apply = transform_fn
        answer_nd = transform_apply(answer_nd)
        applied.append(transform_label)

    span = _phrase_span(answer_nd)
    if span < out_bars * BEATS_PER_BAR - 1e-6 and span > 0:
        answer_nd = _tile_phrase_to_bars(answer_nd, out_bars)
        applied.append(f"tile:{out_bars}bars")
    else:
        limit = out_bars * BEATS_PER_BAR
        answer_nd = [{**n, "duration": min(n["duration"], limit - n["start"])}
                     for n in answer_nd if n["start"] < limit]

    answer_mcp = _notes_to_mcp(answer_nd)
    if humanize:
        answer_mcp = humanize_notes(answer_mcp, role=role, strength=_REAL_STRENGTH,
                                    tempo=tempo, seed=seed)
        applied.append("humanize:gentle")

    out_tempo = float(tempo or pick.get("tempo") or agg.get("tempo", {}).get("mean") or 120.0)
    if not tempo:
        assumptions.append(f"tempo {out_tempo:g} from the source phrase/profile — pass tempo= to match your Set")

    seed_mcp = _notes_to_mcp(seed_sorted)
    result = {
        "answer_notes": answer_mcp,
        "role": role,
        "combined_preview": seed_mcp + _shift_notes(answer_mcp, seed_end_beat),
        "source_phrase_ids": [pick.get("id")],
        "source_provenance": {
            "project": pick.get("project"), "clip_source": pick.get("clip_source"),
            "role": pick.get("role"), "similarity": round(similarity, 3),
            "gold": bool(pick.get("_gold")),
        },
        "transforms_applied": applied,
        "key": key_str,
        "tempo": out_tempo,
        "bars": out_bars,
        "join": {
            "seed_last_pitch": seed_last_pitch,
            "answer_first_pitch": answer_nd[0]["pitch"] if answer_nd else None,
            "interval": (answer_nd[0]["pitch"] - seed_last_pitch) if answer_nd else None,
        },
        "bank_stats": {"n_bank": len(bank), "n_gold": n_gold, "n_gated_mechanical": n_gated,
                       "n_rejected": len(_rejects_meta["suppressed"])},
        "rejects": _rejects_meta,
        "assumptions": assumptions,
    }
    if _ears_extra is not None:
        result.update(_ears_extra)
    if ref is not None:
        result["ref"] = _ref_meta
    return result


def answer_my_loop(
    seed_notes: list[dict],
    bars: int | None = None,
    key: str | None = None,
    tempo: float | None = None,
    n_candidates: int = 5,
    transform: str | None = None,
    humanize: bool = False,
    seed: int = 0,
    *,
    use_ears: bool | None = None,
    direction: str | None = None,
    moment: dict | None = None,
    ref: dict | None = None,
    role: str = "lead",
    relax: str | None = None,
    move: str | None = None,
) -> dict:
    """Answer HIS loop with a phrase from HIS catalog: retrieve the nearest real
    material (gold anchors boosted), transpose it minimally into the seed's key and
    register, voice-lead the join off the seed's last note, optionally apply ONE
    velocity-preserving transform. Real velocities and micro-timing ride along
    untouched. Deterministic given ``seed``; never writes anywhere."""
    t_choice = transform
    if t_choice == "auto":
        t_choice = random.Random(f"answer:{seed}").choice(_ANSWER_TRANSFORMS)
    transform_fn = None
    prospective_fn = _prospective_answer_notes
    if t_choice is not None:
        if t_choice not in _ANSWER_TRANSFORMS:
            raise ValueError(f"transform must be one of {_ANSWER_TRANSFORMS} or 'auto'")
        transform_apply = (
            _fragment_notes if t_choice == "fragment" else _reharmonize_notes
        )
        transform_fn = (t_choice, transform_apply)
        # Gate the notes that will actually be served (develop registry pattern).
        prospective_fn = _prospective_with_transform(transform_apply)
    return _answer_worker(
        seed_notes, key=key, tempo=tempo, bars=bars, seed=seed, humanize=humanize,
        transform_fn=transform_fn, prospective_fn=prospective_fn,
        use_ears=use_ears, direction=direction, moment=moment,
        n_candidates=n_candidates, ref=ref, role=role, relax=relax,
        move=move,
    )


_SLOW_BURN_PASSES = 4  # slow-burn's three flavours share this pass count (spec §3)


def _slow_burn_reveal(nd: list[dict], pass_index: int,
                      n_passes: int = _SLOW_BURN_PASSES) -> list[dict]:
    """Slow-burn: reveal. Pure SELECTION of ``nd``'s own note objects — no note
    is ever invented, so velocities and micro-timing ride along exact. Groups
    beats into a power-of-two stride ladder (wide at pass 0, halving each pass
    down to stride 1 — one bucket per beat, i.e. _thin_notes' own grouping —
    at the second-to-last pass) and keeps one winner per bucket by
    _thin_notes' score (3.0*is_downbeat + duration, is_downbeat reusing that
    function's exact per-note definition). The last pass returns ``nd``
    unchanged (the full phrase). ``pass_index`` clamps: negative -> 0;
    >= n_passes - 1 -> full phrase.

    Because a coarser bucket is always the exact union of two of the next
    pass's finer buckets, and "winner" is a strict total order (score desc,
    then earliest position in ``nd``) applied identically at every stride, a
    coarser bucket's winner is always the higher-priority of its two finer
    buckets' own winners — never a third note. That makes the KEPT-NOTE SET
    (not merely its count) provably monotone non-decreasing pass over pass;
    count monotonicity (what the selftest asserts) follows directly."""
    n = max(1, n_passes)
    clamped = max(0, min(pass_index, n - 1))
    if clamped >= n - 1:
        return list(nd)
    stride = 1 << max(0, (n - 2) - clamped)  # n-2..0 across passes 0..n-2
    buckets: dict[int, tuple[dict, float]] = {}
    for note in nd:
        beat = int(round(note["start"]))
        is_down = abs(note["start"] - beat) < 0.06
        score = (3.0 if is_down else 0.0) + note["duration"]
        bucket = beat // stride
        if bucket not in buckets or score > buckets[bucket][1]:
            buckets[bucket] = (note, score)
    return [buckets[b][0] for b in sorted(buckets)]


def _prospective_slow_burn_reveal(hit: dict, seed_tonic_pc: int, seed_last_pitch: int,
                                  seed_median: int, pass_index: int) -> list[dict]:
    """Mirrors _prospective_answer_notes' pick-time transpose, THEN applies the
    reveal at the same pass the worker will actually run — the ears gate must
    judge the develop answer's revealed shape, not the untransformed
    transpose (F3a)."""
    answer_nd = _prospective_answer_notes(hit, seed_tonic_pc, seed_last_pitch, seed_median)
    return _slow_burn_reveal(answer_nd, pass_index)


def _slow_burn_accent(nd: list[dict], pass_index: int,
                      n_passes: int = _SLOW_BURN_PASSES, role: str = "lead") -> list[dict]:
    """Slow-burn: accent. Pitch, start, and duration byte-identical to input;
    ONLY velocities re-voice toward HIS per-role velocity DNA (the same
    ``velocity_by_role`` contour ``humanize_notes`` reads via ``_humanize_stats``).
    Per pass the contour slot lookup rotates by ``pass_index`` (emphasis wanders);
    blend grows with pass; drift is hard-bounded to ±18 of each original and
    clamped 1..127. The last pass returns exact copies (velocities resolve home).
    Missing DNA stats → identity (no invented contour). Deterministic — no RNG."""
    n = max(1, n_passes)
    clamped = max(0, min(pass_index, n - 1))
    if clamped >= n - 1 or not nd:
        return [dict(note) for note in nd]
    vb, _ = _humanize_stats(role)
    if vb is None:
        return [dict(note) for note in nd]
    slot_mean = vb.get("slot_mean") or []
    if not slot_mean:
        return [dict(note) for note in nd]
    blend = min(1.0, (clamped + 1) / (n - 1))
    phase_offset = clamped
    out: list[dict] = []
    for note in nd:
        m = dict(note)
        start_key = "start_beats" if "start_beats" in m else "start"
        start = float(m.get(start_key, 0.0))
        slot = int(round((start % 4.0) * 4)) % 16
        rotated = (slot + phase_offset) % 16
        orig_vel = int(m.get("velocity", 80))
        if rotated < len(slot_mean):
            contour_vel = float(slot_mean[rotated])
            blended = orig_vel + round(blend * (contour_vel - orig_vel))
            delta = max(-18, min(18, blended - orig_vel))
            m["velocity"] = max(1, min(127, orig_vel + delta))
        out.append(m)
    return out


def _prospective_slow_burn_accent(hit: dict, seed_tonic_pc: int, seed_last_pitch: int,
                                  seed_median: int, pass_index: int) -> list[dict]:
    """Mirrors _prospective_answer_notes' pick-time transpose, THEN applies the
    accent at the same pass the worker will actually run (F3a)."""
    answer_nd = _prospective_answer_notes(hit, seed_tonic_pc, seed_last_pitch, seed_median)
    return _slow_burn_accent(answer_nd, pass_index)


def _slow_burn_add(nd: list[dict], pass_index: int, n_passes: int = _SLOW_BURN_PASSES,
                   key_str: str | None = "D minor") -> list[dict]:
    """Slow-burn: add. The ramp runs the OPPOSITE direction from reveal/accent
    (spec §3.3 — Ricardo's design note): pass 0 is the bare original and the
    LAST pass carries the fullest ornamentation, added count INTENSIFYING
    toward the end of the burn instead of resolving home. Deliberate, not an
    oversight: reveal thins toward the full phrase, accent's emphasis
    resolves to the recorded velocities, add builds UP to its densest voicing.

    Candidate placement reuses _thicken_notes' own gap-finding exactly
    (neighbour points on notes >= 0.75 beat long, at start + duration*0.6;
    passing points at the midpoint of a >=4-semitone gap between the
    phrase's top-line notes) but never invents a pitch, velocity, or
    duration the way _thicken does:

      - pitch: _thicken's diatonic neighbour/passing-tone pitch is computed
        exactly as it does, then SNAPPED via ``_snap_to_legal`` onto
        ``_legal_degree_set(nd, tonic_pc)`` (spec §2) — development can
        never be more chromatic than what he actually played.
      - (velocity, duration): CLONED AS A PAIR from the real note the
        placement anchors on — the long note itself for a neighbour, the
        earlier of the two top-line notes for a passing tone — never
        invented, never mixed from two different donors.

    ``key_str`` supplies the tonic for the legal-set snap. develop_my_loop's
    needs_key dispatch (_DEVELOP_TRANSFORMS["slow-burn:add"]) partially
    applies its OWN ``key`` param here, which may be None: key detection
    happens INSIDE _answer_worker, AFTER transform_fn is already built, so
    there is no worker-resolved key available at dispatch time to hand down
    (see develop_my_loop). When key_str is None, the tonic is self-derived
    from ``nd``'s own pitch-class histogram via ``estimate_key`` — the exact
    pattern _answer_worker uses when no key is supplied (core.py ~3507-3513).
    Self-calibration all the way down: never a hardcoded fallback key.

    Originals pass through as untouched copies (never mutated, never
    dropped); added notes are appended and the whole list re-sorted by
    (start, pitch), matching the file's other transforms. Added count is
    capped at round(len(nd) * 0.4) — the _thicken cap — and ramps from 0 at
    pass 0 up to that cap at the last pass; when the candidate pool exceeds
    the cap, kept candidates are chosen deterministically by the same
    priority _thicken uses (longest anchor note / widest leap first), tied
    by earliest start. No RNG anywhere in this function."""
    if not nd:
        return []
    n = max(1, n_passes)
    clamped = max(0, min(pass_index, n - 1))
    if clamped <= 0:
        return [dict(note) for note in nd]

    if key_str is None:
        pc_hist = [0.0] * 12
        for note in nd:
            pc_hist[note["pitch"] % 12] += 1
        tonic_d, mode_d, _ = estimate_key(pc_hist)
        key_str = f"{tonic_d} {mode_d}"
    tonic, _mode = _parse_key_string(key_str)
    tonic_pc = PITCH_NAMES.index(tonic)
    legal = _legal_degree_set(nd, tonic_pc)

    max_add = round(len(nd) * 0.4)
    n_add = round(max_add * clamped / (n - 1))
    out = [dict(note) for note in nd]
    if n_add <= 0:
        return out

    candidates: list[dict] = []
    for note in nd:
        if note["duration"] < 0.75:
            continue
        nt = round(note["start"] + note["duration"] * 0.6, 6)
        neighbour = note["pitch"] + (2 if (note["pitch"] % 12) in (0, 2, 5, 7, 9) else -1)
        candidates.append({
            "pitch": _snap_to_legal(neighbour, tonic_pc, legal),
            "start": nt,
            "duration": note["duration"],
            "velocity": note["velocity"],
            "_prio": note["duration"],
        })

    top: dict[int, dict] = {}
    for note in nd:
        slot = round(note["start"] * 4)
        if slot not in top or note["pitch"] > top[slot]["pitch"]:
            top[slot] = note
    seq = [top[k] for k in sorted(top)]
    for a, b in zip(seq, seq[1:]):
        gap = b["pitch"] - a["pitch"]
        if abs(gap) < 4:
            continue
        mid_start = (a["start"] + a["duration"] + b["start"]) / 2.0
        step = 2 if abs(gap) >= 5 else 1
        mid_pitch = a["pitch"] + (step if gap > 0 else -step)
        candidates.append({
            "pitch": _snap_to_legal(mid_pitch, tonic_pc, legal),
            "start": round(mid_start, 6),
            "duration": a["duration"],
            "velocity": a["velocity"],
            "_prio": abs(gap),
        })

    candidates.sort(key=lambda c: (-c["_prio"], c["start"]))
    for c in candidates[:n_add]:
        out.append({k: v for k, v in c.items() if k != "_prio"})
    out.sort(key=lambda x: (x["start"], x["pitch"]))
    return out


def _prospective_slow_burn_add(hit: dict, seed_tonic_pc: int, seed_last_pitch: int,
                               seed_median: int, pass_index: int,
                               key_str: str | None = None) -> list[dict]:
    """Mirrors _prospective_answer_notes' pick-time transpose, THEN applies add
    at the same pass the worker will actually run (F3a). ``key_str`` threads
    through from develop_my_loop's needs_key dispatch exactly like
    _slow_burn_add itself; None self-derives a tonic from the candidate's own
    (transposed) notes — the ears gate must judge legality against the same
    tonic the real move will use for this very candidate, not a stale one."""
    answer_nd = _prospective_answer_notes(hit, seed_tonic_pc, seed_last_pitch, seed_median)
    return _slow_burn_add(answer_nd, pass_index, key_str=key_str)


def _prospective_motivic_reharm(hit: dict, seed_tonic_pc: int, seed_last_pitch: int,
                                seed_median: int) -> list[dict]:
    """Mirrors _prospective_answer_notes' pick-time transpose, THEN reharmonizes
    (F3a)."""
    answer_nd = _prospective_answer_notes(hit, seed_tonic_pc, seed_last_pitch, seed_median)
    return _reharmonize_notes(answer_nd)


def _prospective_motivic_retro(hit: dict, seed_tonic_pc: int, seed_last_pitch: int,
                               seed_median: int) -> list[dict]:
    """Mirrors _prospective_answer_notes' pick-time transpose, THEN retrogrades
    (F3a)."""
    answer_nd = _prospective_answer_notes(hit, seed_tonic_pc, seed_last_pitch, seed_median)
    return _retrograde_notes(answer_nd)


def _prospective_motivic_fragment(hit: dict, seed_tonic_pc: int, seed_last_pitch: int,
                                  seed_median: int, key_str: str | None = None) -> list[dict]:
    """Mirrors _prospective_answer_notes' pick-time transpose, THEN fragments with
    his-harmony mapping (F3a — re-timing move-specific mirror)."""
    answer_nd = _prospective_answer_notes(hit, seed_tonic_pc, seed_last_pitch, seed_median)
    return _fragment_harmonic_notes(answer_nd, key_str=key_str)


def _prospective_motivic_walk(hit: dict, seed_tonic_pc: int, seed_last_pitch: int,
                              seed_median: int, walk_seed: int = 0,
                              key_str: str | None = None) -> list[dict]:
    """Mirrors _prospective_answer_notes' pick-time transpose, THEN alphabet-walks
    the motif (F3a — re-timing move-specific mirror)."""
    answer_nd = _prospective_answer_notes(hit, seed_tonic_pc, seed_last_pitch, seed_median)
    return _alphabet_walk_notes(answer_nd, key_str=key_str, walk_seed=walk_seed)


def _prospective_motivic_invert(hit: dict, seed_tonic_pc: int, seed_last_pitch: int,
                                seed_median: int, key_str: str | None = None) -> list[dict]:
    """Mirrors _prospective_answer_notes' pick-time transpose, THEN inverts
    contour with legal-set snap (F3a)."""
    answer_nd = _prospective_answer_notes(hit, seed_tonic_pc, seed_last_pitch, seed_median)
    return _invert_motif_notes(answer_nd, key_str=key_str)


_DEVELOP_TRANSFORMS: dict[str, dict] = {}
# Shape: {move_name: {"transform": Callable[[list[dict]], list[dict]] | None,
#                      "prospective": Callable[[dict, int, int, int], list[dict]],
#                      "needs_seed": bool, "needs_key": bool,
#                      "seed_param": str}}
#   (needs_seed / needs_key / seed_param optional, default False / absent)
# needs_seed=True moves (slow-burn's flavours, T4+) read a pass_index derived
# from ``seed`` instead: "transform" takes (nd, pass_index) and "prospective"
# takes (hit, tonic_pc, last_pitch, median, pass_index). develop_my_loop
# partially applies pass_index = min(seed, _SLOW_BURN_PASSES - 1) at dispatch
# time so _answer_worker's transform_fn/prospective_fn stay the exact plain
# shapes T2 established — no seed plumbing added to the worker itself.
# needs_key=True moves (slow-burn:add, T6) ALSO receive key_str: "transform"
# takes (nd, pass_index, key_str=...) and "prospective" takes (hit,
# tonic_pc, last_pitch, median, pass_index, key_str=...). develop_my_loop
# partially applies key_str = its OWN (possibly-None) ``key`` param — the
# worker only resolves key_str internally, AFTER transform_fn is already
# built, so there is no worker-resolved key to hand down here; the move
# itself self-derives a tonic from the phrase being developed when key_str
# is None (self-calibration all the way down — see _slow_burn_add).
# seed_param (motivic:walk, T8) partially applies {seed_param: seed} at dispatch
# (raw seed, not pass-clamped) and labels f"{move}:walk{seed % 10}"; these
# moves must NOT use needs_seed. needs_key-only moves (motivic:fragment) get
# key_str partial-apply with no label suffix.
_DEVELOP_TRANSFORMS["slow-burn"] = {
    "transform": _slow_burn_reveal,
    "prospective": _prospective_slow_burn_reveal,
    "needs_seed": True,
}
_DEVELOP_TRANSFORMS["slow-burn:reveal"] = _DEVELOP_TRANSFORMS["slow-burn"]  # bare "slow-burn" = reveal
_DEVELOP_TRANSFORMS["slow-burn:accent"] = {
    "transform": _slow_burn_accent,
    "prospective": _prospective_slow_burn_accent,
    "needs_seed": True,
}
_DEVELOP_TRANSFORMS["slow-burn:add"] = {
    "transform": _slow_burn_add,
    "prospective": _prospective_slow_burn_add,
    "needs_seed": True,
    "needs_key": True,
}
_DEVELOP_TRANSFORMS["motivic:reharm"] = {
    "transform": _reharmonize_notes,
    "prospective": _prospective_motivic_reharm,
}
_DEVELOP_TRANSFORMS["motivic:retro"] = {
    "transform": _retrograde_notes,
    "prospective": _prospective_motivic_retro,
}
_DEVELOP_TRANSFORMS["motivic:fragment"] = {
    "transform": _fragment_harmonic_notes,
    "prospective": _prospective_motivic_fragment,
    "needs_key": True,
}
_DEVELOP_TRANSFORMS["motivic:walk"] = {
    "transform": _alphabet_walk_notes,
    "prospective": _prospective_motivic_walk,
    "needs_key": True,
    "seed_param": "walk_seed",
}
_DEVELOP_TRANSFORMS["motivic:invert"] = {
    "transform": _invert_motif_notes,
    "prospective": _prospective_motivic_invert,
    "needs_key": True,
}


def develop_my_loop(
    seed_notes: list[dict],
    move: str | None = None,
    key: str | None = None,
    tempo: float | None = None,
    bars: int | None = None,
    seed: int = 0,
    *,
    use_ears: bool | None = None,
    direction: str | None = None,
    moment: dict | None = None,
    pin_source: str | None = None,
    role: str = "lead",
    relax: str | None = None,
) -> dict:
    """Develop HIS loop: the same retrieval/transpose spine as answer_my_loop
    (shared via _answer_worker) — retrieve ONE real phrase from HIS catalog,
    then apply at most ONE named move from the develop registry
    (_DEVELOP_TRANSFORMS) — a constrained transform of that real phrase, never
    free generation. move=None retrieves and returns untouched, byte-identical
    to answer_my_loop(transform=None), and carries no ``develop_move`` key. An
    unknown move raises, naming the known roster. Deterministic given
    ``seed``; never writes anywhere."""
    develop_move_out = move
    if move == "motivic":
        _motivic_ops = sorted(k for k in _DEVELOP_TRANSFORMS if k.startswith("motivic:"))
        move = _motivic_ops[seed % len(_motivic_ops)]
        develop_move_out = move
    if move is None:
        transform_fn = None
        prospective_fn = _prospective_answer_notes
    else:
        entry = _DEVELOP_TRANSFORMS.get(move)
        if entry is None:
            raise ValueError(
                f"unknown move {move!r} — known moves: {sorted(_DEVELOP_TRANSFORMS)}")
        move_fn = entry["transform"]
        prospective_entry_fn = entry["prospective"]
        label = move
        if entry.get("needs_seed"):
            # slow-burn's flavours read pass_index from seed (spec §3), not
            # from an nd-shaped arg — partially apply it here, at dispatch
            # time, so transform_fn/prospective_fn keep the exact plain
            # shapes _answer_worker (T2) expects; fold the pass into the
            # label so transforms_applied names it (e.g. "slow-burn:reveal:pass2").
            pass_index = min(seed, _SLOW_BURN_PASSES - 1)
            label = f"{move}:pass{pass_index}"
            # needs_key=True (slow-burn:add, T6) ALSO receives key_str — this
            # wrapper's own possibly-None ``key`` param, partially applied the
            # same way pass_index is: _answer_worker only resolves key_str
            # internally (auto-detect vs caller-provided), AFTER transform_fn
            # is already built, so there is no worker-resolved key to hand
            # down here. extra_kwargs stays empty for every other move, so
            # this is a no-op for reveal/accent.
            extra_kwargs = {"key_str": key} if entry.get("needs_key") else {}
            if move_fn is not None:
                move_fn = (lambda nd, _fn=move_fn, _p=pass_index, _kw=extra_kwargs:
                          _fn(nd, _p, **_kw))
            prospective_entry_fn = (
                lambda hit, tpc, lp, med, _fn=prospective_entry_fn, _p=pass_index, _kw=extra_kwargs:
                _fn(hit, tpc, lp, med, _p, **_kw))
        elif entry.get("seed_param"):
            sp = entry["seed_param"]
            extra_kwargs = {sp: seed}
            if entry.get("needs_key"):
                extra_kwargs["key_str"] = key
            label = f"{move}:walk{seed % 10}"
            if move_fn is not None:
                move_fn = (lambda nd, _fn=move_fn, _kw=extra_kwargs:
                          _fn(nd, **_kw))
            prospective_entry_fn = (
                lambda hit, tpc, lp, med, _fn=prospective_entry_fn, _kw=extra_kwargs:
                _fn(hit, tpc, lp, med, **_kw))
        elif entry.get("needs_key"):
            extra_kwargs = {"key_str": key}
            if move_fn is not None:
                move_fn = (lambda nd, _fn=move_fn, _kw=extra_kwargs:
                          _fn(nd, **_kw))
            prospective_entry_fn = (
                lambda hit, tpc, lp, med, _fn=prospective_entry_fn, _kw=extra_kwargs:
                _fn(hit, tpc, lp, med, **_kw))
        transform_fn = (label, move_fn) if move_fn is not None else None
        prospective_fn = prospective_entry_fn
    result = _answer_worker(
        seed_notes, key=key, tempo=tempo, bars=bars, seed=seed, humanize=False,
        transform_fn=transform_fn, prospective_fn=prospective_fn,
        use_ears=use_ears, direction=direction, moment=moment,
        pin_source=pin_source, role=role, relax=relax,
        # the resolved move name (no pass/walk suffix): a DROP of this
        # hearing bans the (source, move) pairing, not the source
        move=develop_move_out,
    )
    if develop_move_out is not None:
        result["develop_move"] = develop_move_out
    return result


def _clip_summary(notes: list[dict], *, key: str | None,
                  tempo: float | None, source_context: dict | None = None) -> dict:
    nd = _normalize_phrase(_note_dicts(notes))
    if not nd:
        raise ValueError("seed_notes list is empty")
    pitches = [n["pitch"] for n in nd]
    return {
        "id": f"clip-{_clip_digest(notes)}",
        "n_notes": len(nd),
        "span_beats": round(_phrase_span(nd), 4),
        "pitch_range": [min(pitches), max(pitches)],
        "key": key,
        "tempo": tempo,
        "source_context": source_context or {},
    }


def create_idea_pack_from_clip(
    seed_notes: list[dict],
    n: int = 3,
    key: str | None = None,
    tempo: float = 120.0,
    cont_bars: int = 4,
    seed: int = 0,
    role: str = "lead",
    source_context: dict | None = None,
    contrast_axis: str | None = None,
) -> dict:
    """Create a reusable IdeaPack from a seed clip without writing to Ableton.

    The pack is an orchestrator over existing engines: critique + develop_idea +
    grow_my_loop. It intentionally does not parse natural language and does not
    auto-write; callers pass structured controls and can later opt in to an
    audition-lane handoff.
    """
    source_clip = _clip_summary(seed_notes, key=key, tempo=tempo,
                                source_context=source_context)
    growth = grow_my_loop(
        seed_notes,
        n=n,
        key=key,
        tempo=tempo,
        cont_bars=cont_bars,
        seed=seed,
    )
    pack_key = growth["key"]
    source_clip["key"] = pack_key
    source_clip["detected_key"] = pack_key
    critique_result = critique(seed_notes, tempo=tempo, role=role, key=pack_key)
    development = develop_idea(seed_notes, tempo=tempo)

    ideas = []
    for i, suggestion in enumerate(growth["suggestions"]):
        candidate = growth["candidates"][i]
        idea_id = f"idea-{i + 1}"
        title = suggestion.get("the_move") or suggestion.get("dimension") or idea_id
        clip_name = f"sidekick {idea_id} {str(title)[:36]}".strip()
        ideas.append({
            "id": idea_id,
            "title": title,
            "role": candidate.get("role", "lead"),
            "source": candidate.get("source", "lead"),
            "dimension": suggestion.get("dimension"),
            "why": suggestion.get("why"),
            "notes": suggestion["notes"],
            "continuation": suggestion.get("continuation"),
            "backing": suggestion.get("backing"),
            "candidate": candidate,
            "base_score": suggestion.get("base_score"),
            "taste_score": suggestion.get("taste_score"),
            "clip_name": clip_name,
        })

    pack_contrast_axis = _normalise_contrast_axis(contrast_axis, growth["candidates"])
    pack_rating_scope = _rating_scope_for_contrast(pack_contrast_axis)
    pack_trainable_model = _trainable_model_for_scope(pack_rating_scope)
    audition_lanes = [
        {
            "lane_index": i,
            "idea_id": idea["id"],
            "track_offset": i,
            "scene_index": 0,
            "clip_name": idea["clip_name"],
            "role": idea["role"],
            "source": idea["source"],
            "n_notes": len(idea["notes"]),
            "requires_explicit_target": True,
        }
        for i, idea in enumerate(ideas)
    ]

    pack_id = f"ideapack-{source_clip['id'].split('-', 1)[1]}-{seed}-{len(ideas)}"
    return {
        "schema_version": 1,
        "id": pack_id,
        "source": "clip",
        "contrast_axis": pack_contrast_axis,
        "rating_contract": {
            "rating_scope": pack_rating_scope,
            "trainable_model": pack_trainable_model,
            "scope_source": "derived_from_contrast_axis",
        },
        "source_clip": source_clip,
        "inferred_controls": {
            "role": role,
            "key": pack_key,
            "tempo": tempo,
            "pack_size": n,
            "continuation_bars": cont_bars,
            "seed": seed,
            "contrast_axis": pack_contrast_axis,
        },
        "critique": critique_result,
        "development": {
            "variation_names": list(development["variations"].keys()),
            "variations": development["variations"],
        },
        "growth": {
            k: v for k, v in growth.items()
            if k not in {"suggestions", "candidates"}
        },
        "ideas": ideas,
        "candidates": growth["candidates"],
        "audition_lanes": audition_lanes,
        "write_policy": {
            "auto_write": False,
            "requires_confirm": True,
            "requires_explicit_target": True,
            "handoff_tool": "prepare_idea_pack_audition_lanes",
        },
        "rating_hint": (
            "After listening, call rate_candidates(pack['candidates'], best=<i>, "
            "worst=<j>, reason='...', contrast_axis=pack['contrast_axis']). "
            "Ratings train only when the pack varied one supported model axis."
        ),
    }


def prepare_idea_pack_audition_lanes(
    idea_pack: dict,
    confirm: bool = False,
    start_track: int | None = None,
    scene_index: int = 0,
    tempo: float | None = None,
) -> dict:
    """Prepare Producer Pal calls for audition lanes; never executes them.

    confirm=False returns a preview only. confirm=True returns the ppal-create-clip
    handoff calls, still for the MCP client/user to execute explicitly.
    """
    ideas = idea_pack.get("ideas") or []
    if not ideas:
        raise ValueError("idea_pack has no ideas")

    lanes = []
    explicit_target = start_track is not None
    target_base = int(start_track) if explicit_target else 0
    for i, idea in enumerate(ideas):
        track_index = target_base + i
        lanes.append({
            "idea_id": idea.get("id", f"idea-{i + 1}"),
            "slot": (f"{track_index}/{int(scene_index)}" if explicit_target else None),
            "track_index": (track_index if explicit_target else None),
            "scene_index": int(scene_index),
            "clip_name": idea.get("clip_name") or f"sidekick idea {i + 1}",
            "n_notes": len(idea.get("notes") or []),
            "role": idea.get("role"),
            "source": idea.get("source"),
            "requires_explicit_target": not explicit_target,
        })

    if not confirm:
        return {
            "ok": False,
            "requires_confirm": True,
            "requires_explicit_target": not explicit_target,
            "executed": False,
            "tool_calls": [],
            "lanes_preview": lanes,
            "message": (
                "No Producer Pal calls were created. Pick an explicit start_track "
                "and re-run with confirm=True to prepare audition lanes."
            ),
        }
    if not explicit_target:
        raise ValueError("confirm=True requires explicit start_track to avoid overwriting track 0")

    calls: list[dict[str, Any]] = []
    lane_details = []
    lane_tempo = tempo or idea_pack.get("source_clip", {}).get("tempo") or 120.0
    for i, idea in enumerate(ideas):
        lane = lanes[i]
        handoff = send_to_ableton(
            idea["notes"],
            track=lane["track_index"],
            clip_name=lane["clip_name"],
            tempo=lane_tempo if i == 0 else 0,
            session_scene=lane["scene_index"],
        )
        calls.extend(handoff["tool_calls"])
        lane_details.append({**lane, "handoff": {
            k: v for k, v in handoff.items()
            if k in {"placement", "note_count", "clip_length", "assumptions"}
        }})

    return {
        "ok": True,
        "confirmed": True,
        "executed": False,
        "pack_id": idea_pack.get("id"),
        "instruction": (
            "These are handoff calls only. Execute them on Producer Pal explicitly "
            "after confirming the target tracks/scene. Each ppal-create-clip follows a "
            "guard ppal-read-clip of the same slot: run the create only when that read "
            "proves the slot empty (error 'no clip at <path>' or an id-null record at that "
            "path); otherwise skip it — on Producer Pal 2.4 a create into an occupied "
            "slot replaces his clip."
        ),
        "tool_calls": calls,
        "lanes": lane_details,
    }


def _active_roles_for_section_with_develop(
    section: dict,
    available_roles: set[str],
    *,
    sec_idx: int,
    peak_idx: int,
    develop_lead: bool,
) -> list[str]:
    """Energy-gated roles; when developing a lead hook, place it on structural sections."""
    active = set(_active_roles_for_section(section, available_roles))
    if develop_lead and "lead" in available_roles:
        role = section.get("role", "section")
        energy = float(section.get("energy", 0.0))
        if role == "section" and (
            sec_idx == peak_idx
            or sec_idx == peak_idx - 1
            or energy >= _role_energy_threshold("lead")
        ):
            active.add("lead")
    return sorted(active, key=lambda r: _ROLE_LAYER_ORDER.get(r, 99))


def develop_idea(seed_notes: list[dict], tempo: float = 120.0) -> dict:
    """Develop a seed phrase into labelled symbolic variations (theme & variations).

    Deterministic transforms — no synthesis toward the catalog average. Each variation
    keeps the seed's DNA but differs from the literal theme.
    """
    nd = _normalize_phrase(_note_dicts(seed_notes))
    if not nd:
        raise ValueError("seed_notes list is empty")

    theme = list(nd)
    variations_internal: dict[str, list[dict]] = {
        "theme": theme,
        "octave_up": _octave_variant(theme, 12),
        "octave_down": _octave_variant(theme, -12),
        "half_time": _half_time_notes(theme),
        "double_time": _double_time_notes(theme),
        "thin": _thin_notes(theme),
        "thicken": _thicken_notes(theme),
        "fragment": _fragment_notes(theme),
        "reharmonize": _reharmonize_notes(theme),
        "call_response": _call_response_notes(theme),
    }

    variations = {k: _notes_to_mcp(v) for k, v in variations_internal.items()}
    src_fig, tgt_fig = _reharmonize_pair()
    pc_hist = [0.0] * 12
    for n in theme:
        pc_hist[n["pitch"] % 12] += 1
    tonic, mode, _ = estimate_key(pc_hist)

    return {
        "tempo": tempo,
        "seed_summary": {
            "n_notes": len(theme),
            "span_beats": round(_phrase_span(theme), 4),
            "detected_key": f"{tonic} {mode}",
            "reharmonize": f"{src_fig}→{tgt_fig}",
        },
        "variations": variations,
    }


def _peak_section_index(sections: list[dict]) -> int:
    """0-based index of the highest-energy section (excluding intro/outro)."""
    eligible = [
        (i, float(s.get("energy", 0)))
        for i, s in enumerate(sections)
        if s.get("role") not in ("intro", "outro")
    ]
    if not eligible:
        return 0
    return max(eligible, key=lambda x: x[1])[0]


_DEVELOPABLE_ROLES = frozenset({"lead", "bass", "drums", "pad", "chords"})
_VARIATION_FALLBACKS = (
    "theme", "octave_up", "double_time", "fragment", "call_response",
    "reharmonize", "half_time", "thin", "thicken", "octave_down", "fill",
)


def _role_variation_candidates(
    role: str,
    sec: dict,
    sec_idx: int,
    peak_idx: int,
    *,
    section_ordinal: int = 0,
) -> list[str]:
    """Ordered variation preferences for a role in one section."""
    sec_role = sec.get("role", "section")
    energy = float(sec.get("energy", 0.5))

    if role == "lead":
        if sec_role == "intro":
            return ["thin", "half_time", "octave_down"]
        if sec_role == "breakdown":
            return ["thin", "reharmonize", "half_time", "octave_down"]
        if sec_role == "outro":
            return ["fragment", "thin", "call_response", "half_time"]
        if sec_idx == peak_idx:
            return ["thicken", "theme", "octave_up", "double_time"]
        if peak_idx > 0 and sec_idx == peak_idx - 1:
            return ["fragment", "double_time", "call_response", "half_time"]
        if section_ordinal % 2 == 1:
            return [
                "octave_up", "call_response", "theme", "reharmonize",
                "double_time", "half_time", "fragment",
            ]
        return [
            "theme", "octave_up", "call_response", "reharmonize",
            "double_time", "half_time", "fragment", "octave_down",
        ]

    if role == "bass":
        if sec_role in ("breakdown", "outro") and energy < 0.45:
            return ["thin", "half_time", "octave_down"]
        if sec_role == "intro":
            return ["thin", "half_time", "theme", "octave_down"]
        if sec_idx == peak_idx:
            return ["octave_up", "double_time", "theme", "thicken"]
        if peak_idx > 0 and sec_idx == peak_idx - 1:
            return ["double_time", "fragment", "call_response", "theme"]
        return [
            "theme", "octave_up", "reharmonize", "call_response",
            "half_time", "double_time",
        ]

    if role == "drums":
        if sec_role == "breakdown":
            return ["thin", "half_time", "theme"]
        if sec_idx == peak_idx:
            return ["theme", "double_time", "thicken", "fragment"]
        if peak_idx > 0 and sec_idx == peak_idx - 1:
            return ["fill", "double_time", "fragment", "theme"]
        if sec_role == "section" and energy >= 0.55 and sec_idx < peak_idx:
            return ["theme", "fill", "double_time", "fragment"]
        return ["theme", "fragment", "double_time", "thin"]

    if role == "pad":
        if sec_role in ("breakdown", "intro") or energy < 0.40:
            return ["thin", "reharmonize", "half_time", "octave_down"]
        if sec_role == "outro":
            return ["thin", "fragment", "half_time", "reharmonize"]
        if sec_idx == peak_idx:
            return ["theme", "thicken", "octave_up", "call_response"]
        return [
            "reharmonize", "theme", "call_response", "octave_up",
            "fragment", "half_time",
        ]

    if role == "chords":
        if sec_role == "breakdown":
            return ["thin", "reharmonize", "half_time"]
        return ["theme", "reharmonize", "call_response", "octave_up", "thin"]

    return ["theme"]


def _assign_role_variations(
    sections: list[dict],
    peak_idx: int,
    role: str,
    *,
    max_run: int = 2,
) -> list[str]:
    """One variation per section; no more than max_run adjacent identical labels."""
    assigned: list[str] = []
    section_ordinal = 0
    for sec_idx, sec in enumerate(sections):
        if role == "lead" and sec.get("role") == "section":
            section_ordinal += 1
        cands = _role_variation_candidates(
            role, sec, sec_idx, peak_idx, section_ordinal=section_ordinal,
        )
        choice = cands[0]
        if len(assigned) >= max_run and all(
            v == assigned[-1] for v in assigned[-max_run:]
        ):
            for c in cands:
                if c != assigned[-1]:
                    choice = c
                    break
            else:
                for alt in _VARIATION_FALLBACKS:
                    if alt != assigned[-1]:
                        choice = alt
                        break
        assigned.append(choice)
    return assigned


def _build_develop_bank(
    role: str,
    notes: list[dict],
    tempo: float,
    loop_bars: int,
) -> dict[str, list[dict]]:
    """Per-role variation bank from develop_idea (+ drum fill when needed)."""
    bank = dict(develop_idea(notes, tempo=tempo)["variations"])
    if role == "drums":
        theme_int = _normalize_phrase(_note_dicts(bank["theme"]))
        bank["fill"] = _notes_to_mcp(_drum_fill_notes(theme_int, loop_bars))
    return bank


def _lead_variation_for_section(
    sec: dict,
    sec_idx: int,
    peak_idx: int,
    lead_sequence: list[str] | None = None,
) -> str:
    """Map section → lead variation (uses precomputed sequence when provided)."""
    if lead_sequence is not None:
        return lead_sequence[sec_idx]
    return _assign_role_variations([sec], 0, "lead")[0]


_DEVICE_NAMES = ("buildup", "drop", "reprise", "fill")


def _apply_arrangement_devices(sections, variation_maps, peak_idx):
    """Layer musical intent onto the per-section variation choices (mutates
    variation_maps in place) and return {section_idx: [device, ...]}.

    - buildup: thin the melodic parts + a drum fill in the section before the drop
    - drop: state the idea in full at the peak
    - reprise: bring the lead motif back an octave up after the last breakdown
    - fill: a drum fill heading into the outro
    Unknown variations fall back to 'theme' at placement time, so these are safe.
    """
    n = len(sections)
    devices: dict[int, list[str]] = {}

    def tag(i, d):
        if 0 <= i < n:
            devices.setdefault(i, []).append(d)

    def setv(role, i, v):
        if role in variation_maps and 0 <= i < n:
            variation_maps[role][i] = v

    # buildup → drop
    b = peak_idx - 1
    if b >= 1:  # don't thin the intro
        tag(b, "buildup")
        setv("lead", b, "thin")
        setv("pad", b, "thin")
        setv("drums", b, "fill")
    tag(peak_idx, "drop")
    setv("lead", peak_idx, "thicken")  # densest moment (also keeps peak != plain theme)
    setv("pad", peak_idx, "thicken")

    # reprise: restate the motif up an octave after the last breakdown ("bring it back")
    breakdowns = [i for i, s in enumerate(sections) if s.get("role") == "breakdown"]
    if breakdowns:
        for i in range(breakdowns[-1] + 1, n):
            if sections[i].get("role") == "section":
                tag(i, "reprise")
                setv("lead", i, "octave_up")
                break

    # fill heading into the outro
    for i in range(n - 1, 0, -1):
        if sections[i].get("role") == "outro":
            tag(i - 1, "fill")
            setv("drums", i - 1, "fill")
            break

    return devices


def _default_loop_bars(span_beats: float) -> int:
    """Loop length for an arrangement part that didn't declare ``loop_bars``.

    Rounds the phrase's own span UP to a power-of-two bar count (1/2/4/8/16 —
    his measured section grid). Using the raw span-in-bars instead would hand
    back odd cycles: a part whose notes end at beat 10 would retile every 3
    bars, drifting the motif onto a different downbeat on every repeat against
    4/8/16-bar sections. Declared ``loop_bars`` always wins over this guess.
    """
    if span_beats <= 0:
        return 4
    phrase_bars = max(1, math.ceil(span_beats / BEATS_PER_BAR - 1e-9))
    loop = 1
    while loop < phrase_bars:
        loop *= 2
    return loop


def _fold_persisted_session_takes(context: Any) -> Any:
    """Attach newest validated Set-take cards when they exist.

    The import is lazy so the existing MIDI-only core import path remains additive;
    session-ears is a context layer, not a new generation dependency. Identity
    is resolved once per fold inside session_ears.
    """
    from mcp_server import session_ears

    return session_ears.fold_persisted_session_takes(context)


def arrange_song(
    parts: list[dict],
    target_minutes: float | None = None,
    tempo: float = 120.0,
    seed: int = 0,
) -> dict:
    """Lay out loop parts on a full-song timeline using learned structure templates.

    Each part is a short loop ``{role, notes, loop_bars}`` repeated per active section.
    Lead, bass, drums, and pad each develop across sections (theme & variations);
    energy still gates which roles are active. Returns section skeleton plus placements
    for Producer Pal write-back.
    """
    if not parts:
        raise ValueError("parts list is empty")

    by_role: dict[str, dict] = {}
    for p in parts:
        role = str(p.get("role", "")).strip().lower()
        if not role:
            raise ValueError("each part must have a role")
        notes = p.get("notes")
        if not notes:
            raise ValueError(f"part '{role}' has no notes")
        by_role[role] = {
            "role": role,
            "notes": notes,
            "loop_bars": max(1, int(p["loop_bars"])) if p.get("loop_bars") is not None
            else _default_loop_bars(_phrase_span(_note_dicts(notes))),
        }

    skeleton = suggest_structure(target_minutes=target_minutes, tempo=tempo, seed=seed)
    available = set(by_role.keys())

    develop_banks: dict[str, dict[str, list[dict]]] = {}
    for role in by_role:
        if role in _DEVELOPABLE_ROLES:
            develop_banks[role] = _build_develop_bank(
                role,
                by_role[role]["notes"],
                tempo,
                by_role[role]["loop_bars"],
            )

    peak_idx = _peak_section_index(skeleton["sections"])
    variation_maps: dict[str, list[str]] = {
        role: _assign_role_variations(skeleton["sections"], peak_idx, role)
        for role in develop_banks
    }
    devices_by_section = _apply_arrangement_devices(
        skeleton["sections"], variation_maps, peak_idx,
    )
    sections_out: list[dict] = []
    placements: list[dict] = []

    for sec_i, sec in enumerate(skeleton["sections"]):
        active = _active_roles_for_section_with_develop(
            sec,
            available,
            sec_idx=sec_i,
            peak_idx=peak_idx,
            develop_lead="lead" in develop_banks,
        )
        sec_out = dict(sec)
        sec_out["active_roles"] = active
        sec_out["devices"] = devices_by_section.get(sec_i, [])
        for role in active:
            if role in variation_maps:
                sec_out[f"{role}_variation"] = variation_maps[role][sec_i]
        if "lead" in active and "lead" in variation_maps:
            sec_out["lead_variation"] = variation_maps["lead"][sec_i]
        sections_out.append(sec_out)

        start_bb = f"{sec['bar_start'] + 1}|1"
        length_bars = int(sec["bars"])
        length_token = f"{length_bars}bar"

        for role in active:
            part = by_role[role]
            notes = part["notes"]
            variation = None
            if role in develop_banks:
                variation = variation_maps[role][sec_i]
                notes = develop_banks[role].get(
                    variation, develop_banks[role]["theme"],
                )
            tiled_notes = _notes_to_mcp(
                _tile_phrase_to_bars(
                    _note_dicts(notes),
                    length_bars,
                    loop_bars=part["loop_bars"],
                )
            )
            placements.append({
                "role": role,
                "section_index": sec["index"],
                "arrangement_start_bar_beat": start_bb,
                "length_bars": length_bars,
                "length_token": length_token,
                "loop_bars": part["loop_bars"],
                "notes": tiled_notes,
                "variation": variation,
            })

    result = {
        "sections": sections_out,
        "placements": placements,
        "tempo": skeleton["tempo"],
        "total_bars": skeleton["total_bars"],
        "total_minutes": skeleton.get("total_minutes"),
        "peak_section_index": peak_idx + 1,
        "devices": {s["index"]: s["devices"] for s in sections_out if s.get("devices")},
        "peak_lead_variation": (
            variation_maps["lead"][peak_idx]
            if "lead" in variation_maps
            else None
        ),
    }
    return _fold_persisted_session_takes(result)


def _gesture_familiarity(degrees: list[int], ref_tri: dict, scale: float = 0.01) -> float:
    """How much a melody is built from Ricardo's common gestures, in [0,1].

    The mean corpus 3-gram probability of the line's scale-degree shapes, scaled.
    High for lines made of his recurring cells; near-zero for random/chromatic
    shapes he never plays — so it discriminates far better than a full-support
    divergence for short phrases. (Jensen-Shannon between two profiles is the
    yardstick reserved for the influence-blend feature, not this single-profile fit.)
    """
    tris = [f"{a},{b},{c}" for a, b, c in zip(degrees, degrees[1:], degrees[2:])]
    if not tris:
        return 0.0
    avg = sum(ref_tri.get(t, 0.0) for t in tris) / len(tris)
    return min(1.0, avg / scale)


def _phrase_style_analysis(nd: list[dict], role: str | None = None,
                           key: str | None = None,
                           scale_pcs: list[int] | None = None) -> dict:
    """Shared sub-scores and raw metrics for score_phrase / critique. When ``key`` is
    given (e.g. 'D minor') it's used instead of self-detecting from the phrase (short
    leads detect unstably). When ``role`` is given, melodic_fit uses that role's grammar
    instead of the pooled lead+bass+pad one. ``scale_pcs`` (e.g. Live's own scale)
    replaces the key's diatonic set as the in-scale reference."""
    agg = _aggregate()

    pc_hist = [0.0] * 12
    for n in nd:
        pc_hist[n["pitch"] % 12] += 1
    if key:
        tonic, mode = _parse_key_string(key)
    else:
        tonic, mode, _ = estimate_key(pc_hist)
    dia = ({int(p) % 12 for p in scale_pcs} if scale_pcs
           else diatonic_set(PITCH_NAMES.index(tonic), mode))
    in_scale = sum(pc_hist[i] for i in dia) / (sum(pc_hist) or 1)
    target_sc = agg.get("avg_scale_conformity", 0.9)
    scale_fit = 1.0 - min(1.0, abs(in_scale - target_sc) / 0.25)

    # the bonus compares on the parent family (a phrygian phrase is a minor-side
    # phrase, not a foreign key name), so a mode word never costs the phrase
    top_keys = {k for k, _ in agg.get("key_distribution", [])[:5]}
    family = "major" if 4 in MODE_STEPS.get(mode, MODE_STEPS["minor"]) else "minor"
    key_bonus = 0.1 if f"{tonic} {family}" in top_keys else 0.0
    scale_fit = min(1.0, scale_fit + key_bonus)

    iv = _interval_buckets(nd)
    step_mass = iv[2] + iv[4]
    leap_mass = iv[0] + iv[1] + iv[5] + iv[6]
    target_steps = _target_step_ratio(agg.get("melody_shape", {}))
    actual_steps = step_mass / (step_mass + leap_mass or 1)
    interval_shape = 1.0 - min(1.0, abs(actual_steps - target_steps) / 0.35)

    # Melodic fit using the transition model (role-specific when a role is given)
    grammar = ((agg.get("melodic_grammar_by_role", {}).get(role) if role else None)
               or agg.get("melodic_grammar", {}))
    melodic_fit = 0.0
    gesture_fit = 0.0
    sorted_notes = sorted(nd, key=lambda n: n["start"])
    top = {}
    for n in sorted_notes:
        slot = round(n["start"] * 4)
        if slot not in top or n["pitch"] > top[slot]["pitch"]:
            top[slot] = n
    seq = [top[k]["pitch"] for k in sorted(top)]
    if seq:
        tonic_idx = PITCH_NAMES.index(tonic)
        degrees = [(pitch - tonic_idx) % 12 for pitch in seq]
        unigram = grammar.get("unigram", {})
        order_1 = grammar.get("order_1", {})
        order_2 = grammar.get("order_2", {})

        probs = []
        if degrees:
            probs.append(unigram.get(str(degrees[0]), 0.0))
        for i in range(1, len(degrees)):
            prev2 = degrees[i-2] if i >= 2 else None
            prev1 = degrees[i-1]
            curr = degrees[i]

            p_val = 0.0
            if prev2 is not None and prev1 is not None:
                key2 = f"{prev2},{prev1}"
                if key2 in order_2 and str(curr) in order_2[key2]:
                    p_val = order_2[key2][str(curr)]
            if p_val == 0.0 and prev1 is not None:
                key1 = str(prev1)
                if key1 in order_1 and str(curr) in order_1[key1]:
                    p_val = order_1[key1][str(curr)]
            if p_val == 0.0:
                p_val = unigram.get(str(curr), 0.0)
            probs.append(p_val)

        avg_prob = sum(probs) / len(probs) if probs else 0.0
        melodic_fit = min(1.0, avg_prob / 0.15)

        ref_tri = _motifs_data().get("trigram_distribution")
        if ref_tri and len(degrees) >= 3:
            gesture_fit = _gesture_familiarity(degrees, ref_tri)
        else:
            gesture_fit = melodic_fit

    cand_g = _groove_grid(nd)
    ref_g = agg.get("harmonic_groove", [0] * 16)
    ref_total = sum(ref_g) or 1
    ref_g = [x / ref_total for x in ref_g]
    groove = _l2_cosine(cand_g, ref_g)

    pitches = [n["pitch"] for n in nd]
    mean_pitch = sum(pitches) / len(pitches)
    lo, hi = agg.get("pitch_register", [48, 72])
    if lo is not None and hi is not None and lo <= mean_pitch <= hi:
        register = 1.0
    else:
        dist = min(abs(mean_pitch - lo), abs(mean_pitch - hi)) if lo and hi else 12
        register = max(0.0, 1.0 - dist / 24.0)

    poly = list(Counter(round(n["start"] * 4) for n in nd).values())
    avg_poly = sum(poly) / len(poly) if poly else 1.0
    target_poly = agg.get("avg_polyphony", 1.26)
    polyphony = 1.0 - min(1.0, abs(avg_poly - target_poly) / 2.0)

    scores = {
        "scale_fit": round(scale_fit, 3),
        "interval_shape": round(interval_shape, 3),
        "melodic_fit": round(melodic_fit, 3),
        "gesture_fit": round(gesture_fit, 3),
        "groove": round(groove, 3),
        "register": round(register, 3),
        "polyphony": round(polyphony, 3),
    }
    weights = {
        "scale_fit": 0.175,
        "interval_shape": 0.125,
        "melodic_fit": 0.125,
        "gesture_fit": 0.20,
        "groove": 0.20,
        "register": 0.10,
        "polyphony": 0.075,
    }
    overall = round(sum(scores[k] * weights[k] for k in weights), 3)

    return {
        "overall": overall,
        "scores": scores,
        "detected_key": f"{tonic} {mode}",
        "scale_conformity": round(in_scale, 3),
        "raw": {
            "actual_steps": round(actual_steps, 3),
            "target_steps": round(target_steps, 3),
            "in_scale": round(in_scale, 3),
            "target_scale_conformity": round(target_sc, 3),
            "mean_pitch": round(mean_pitch, 1),
            "register_lo": lo,
            "register_hi": hi,
            "avg_polyphony": round(avg_poly, 2),
            "target_polyphony": target_poly,
        },
    }


def score_phrase(notes: list[dict], tempo: float | None = None,
                 role: str | None = None, key: str | None = None,
                 scale_pcs: list[int] | None = None) -> dict:
    """Score a candidate phrase against Ricardo's style profile. Pass ``role`` (e.g.
    'lead') to score against that role's grammar, and ``key`` (e.g. 'D minor') to score
    against a known key instead of self-detecting it from the phrase. ``scale_pcs``
    (Live's own scale, from hear_the_room) overrides the key's diatonic set."""
    agg = _aggregate()
    nd = _note_dicts(notes)
    if not nd:
        return {"overall": 0.0, "scores": {}, "verdict": "No notes to score."}

    analysis = _phrase_style_analysis(nd, role=role, key=key, scale_pcs=scale_pcs)
    overall = analysis["overall"]
    scores = analysis["scores"]

    if overall >= 0.75:
        verdict = ("Strong match — in-key, register and groove sit in your usual zone. "
                   "This could pass as your material.")
    elif overall >= 0.55:
        verdict = ("Partial match — some traits align but others drift "
                   "(check groove or interval shape).")
    else:
        verdict = ("Weak match — likely outside your usual key, register, or rhythmic feel.")

    return {
        "overall": overall,
        "scores": scores,
        "detected_key": analysis["detected_key"],
        "scale_conformity": analysis["scale_conformity"],
        "verdict": verdict,
        "tempo_hint": tempo or agg.get("tempo", {}).get("mean"),
    }


def critique(notes: list[dict], tempo: float | None = None,
             role: str | None = None, key: str | None = None) -> dict:
    """Richer style critique: sub-scores plus actionable suggestions."""
    agg = _aggregate()
    nd = _note_dicts(notes)
    if not nd:
        return {
            "overall": 0.0,
            "scores": {},
            "suggestions": ["Add at least one note to critique."],
            "verdict": "No notes to critique.",
        }

    analysis = _phrase_style_analysis(nd, role=role, key=key)
    scores = analysis["scores"]
    raw = analysis["raw"]
    suggestions: list[str] = []

    top_iv = agg.get("top_melodic_intervals", [])[:3]
    fav_leaps = ", ".join(
        iv["name"] for iv in top_iv if abs(iv.get("semitones", 0)) >= 3
    ) or "m3/P4/P5"

    if scores["interval_shape"] < 0.55 and raw["actual_steps"] > raw["target_steps"] + 0.08:
        suggestions.append(
            f"Lean into leaps — melody is very stepwise; you often use {fav_leaps}."
        )
    elif scores["interval_shape"] < 0.55 and raw["actual_steps"] < raw["target_steps"] - 0.08:
        suggestions.append(
            "Balance leaps with more stepwise motion (M2/m2) — your lines mix steps and jumps."
        )

    if raw["in_scale"] > raw["target_scale_conformity"] + 0.04:
        suggestions.append(
            "Add a colour/chromatic note — your catalog sits ~91% diatonic; "
            "a passing tone or modal colour would read more like you."
        )
    elif raw["in_scale"] < raw["target_scale_conformity"] - 0.12:
        suggestions.append(
            f"Anchor more to {analysis['detected_key']} — scale fit is low vs your ~91% in-key average."
        )

    if scores["groove"] < 0.55:
        suggestions.append(
            "Land more onsets on beat 1 / the 8th grid — your harmonic material "
            "clusters on downbeats and 16th subdivisions."
        )

    if scores["register"] < 0.7:
        lo_n = _midi_to_pitch_name(int(raw["register_lo"]))
        hi_n = _midi_to_pitch_name(int(raw["register_hi"]))
        direction = "up" if raw["mean_pitch"] < raw["register_lo"] else "down"
        suggestions.append(
            f"Shift register {direction} toward {lo_n}–{hi_n} "
            f"(mean now ~{_midi_to_pitch_name(int(round(raw['mean_pitch'])))})."
        )

    if scores["polyphony"] < 0.6 and raw["avg_polyphony"] > raw["target_polyphony"] + 0.5:
        suggestions.append(
            "Thin overlapping notes — your melodic clips are mostly monophonic (~1.26 notes/onset)."
        )
    elif scores["polyphony"] < 0.6 and raw["avg_polyphony"] < 1.1:
        suggestions.append(
            "Consider a brief overlap or double-stop for harmonic colour (still sparse vs chords)."
        )

    top_keys = [k for k, _ in agg.get("key_distribution", [])[:3]]
    if analysis["detected_key"] not in top_keys and scores["scale_fit"] < 0.7:
        suggestions.append(
            f"Key centre {analysis['detected_key']} is uncommon in your catalog "
            f"(top: {', '.join(top_keys[:3])})."
        )

    overall = analysis["overall"]
    if overall >= 0.75:
        verdict = "On-style — minor tweaks at most."
    elif overall >= 0.55:
        verdict = "Mixed — some traits match; address suggestions below."
    else:
        verdict = "Off-style — several dimensions drift from your profile."

    if not suggestions and overall < 0.75:
        suggestions.append("Review groove and interval balance against retrieved exemplars.")

    return {
        "overall": overall,
        "scores": scores,
        "suggestions": suggestions,
        "verdict": verdict,
        "detected_key": analysis["detected_key"],
        "scale_conformity": analysis["scale_conformity"],
        "tempo_hint": tempo or agg.get("tempo", {}).get("mean"),
    }


def finish_my_idea(
    seed_notes: list[dict],
    target_minutes: float | None = None,
    tempo: float | None = None,
) -> dict:
    """Bundle seed analysis + continuation brief + structure for finish-my-idea workflow.

    Does not generate notes — returns context for Claude to compose continuations and
    complementary parts, then use lay_the_song and three_endings.
    """
    nd = _note_dicts(seed_notes)
    if not nd:
        raise ValueError("seed_notes list is empty")

    cont = continue_clip(nd, bars=8, tempo=tempo)
    crit = critique(nd, tempo=tempo)
    structure = _fold_persisted_session_takes(
        suggest_structure(target_minutes=target_minutes, tempo=tempo)
    )

    continuation_brief = dict(cont)
    continuation_brief["variations_requested"] = 3
    continuation_brief["guidance"] = (
        f"{cont['guidance']} Compose {continuation_brief['variations_requested']} "
        "distinct continuation variants (develop the motif differently each time); "
        "do not paste example_continuations verbatim."
    )

    return {
        "seed_summary": cont["seed_summary"],
        "critique": crit,
        "continuation_brief": continuation_brief,
        "structure": structure,
        "guidance": (
            "Compose 3 continuations + complementary parts (bass, chords, pad, drums "
            "as needed). Use lay_the_song: preview, confirm locators, then commit; "
            "use three_endings to explore endings."
        ),
    }


def continue_clip(notes: list[dict], bars: int = 8, tempo: float | None = None) -> dict:
    """Analyze a seed clip and return a continuation brief for Claude (no note generation)."""
    agg = _aggregate()
    nd = _note_dicts(notes)
    if not nd:
        raise ValueError("notes list is empty")

    pc_hist = [0.0] * 12
    for n in nd:
        pc_hist[n["pitch"] % 12] += 1
    tonic, mode, _ = estimate_key(pc_hist)
    detected_key = f"{tonic} {mode}"

    pitches = [n["pitch"] for n in nd]
    mean_pitch = sum(pitches) / len(pitches)
    lo_reg, hi_reg = agg.get("pitch_register", [36, 85])

    groove_grid = _groove_grid(nd)
    peak_slot = max(range(16), key=lambda i: groove_grid[i])
    groove_hint = f"onset peak at 16th-slot {peak_slot} (0=beat1)"

    last_note = max(nd, key=lambda n: (n["start"], n["pitch"]))
    max_end = max(n["start"] + n["duration"] for n in nd)
    end_beat = round(max_end, 4)
    last_bar_start = max(0.0, (int(end_beat // BEATS_PER_BAR)) * BEATS_PER_BAR)
    last_bar_pitches = sorted({
        n["pitch"] for n in nd if n["start"] >= last_bar_start - 1e-6
    })

    tempo_hint = tempo or agg.get("tempo", {}).get("mean") or 120.0
    examples = find_phrases(seed_notes=nd, kind="harmonic", k=4, tempo=tempo_hint)

    bars = max(1, int(bars))
    end_bar = int(end_beat // BEATS_PER_BAR) + 1
    target_end_beat = end_beat + bars * BEATS_PER_BAR

    guidance = (
        f"You (Claude) compose the next {bars} bar(s) (~{bars * BEATS_PER_BAR} beats) "
        f"in Ricardo's style. Pick up from end_beat={end_beat} (bar {end_bar}|…); "
        f"target clip end ≈ beat {round(target_end_beat, 2)}. "
        f"Match key={detected_key}, register ~{_midi_to_pitch_name(int(round(mean_pitch)))}, "
        f"and rhythmic feel ({groove_hint}). "
        f"Last pitch: {_midi_to_pitch_name(last_note['pitch'])}. "
        f"Use example_continuations as reference — transpose/develop, do not paste verbatim. "
        f"Run critique() on your result before export."
    )

    return {
        "seed_summary": {
            "key": detected_key,
            "register": {
                "mean_midi": round(mean_pitch, 1),
                "mean_name": _midi_to_pitch_name(int(round(mean_pitch))),
                "profile_range": [_midi_to_pitch_name(lo_reg), _midi_to_pitch_name(hi_reg)],
                "in_range": lo_reg <= mean_pitch <= hi_reg,
            },
            "groove": {
                "grid_16ths": [round(x, 3) for x in groove_grid],
                "hint": groove_hint,
            },
            "end_pitch": _midi_to_pitch_name(last_note["pitch"]),
            "end_pitch_midi": last_note["pitch"],
            "end_beat": end_beat,
            "last_bar_pitches": [_midi_to_pitch_name(p) for p in last_bar_pitches],
            "n_notes": len(nd),
        },
        "example_continuations": examples,
        "bars_requested": bars,
        "tempo_hint": tempo_hint,
        "guidance": guidance,
    }


def _alias_matched_fingerprint(existing: dict, payload: dict, fingerprint: str) -> str:
    """The stored rating's fingerprint hashed the phrase id it was written
    under; the pid migration rewrites ``phrase_id`` but never that hash. When
    the digests differ, re-hash the replay with each id that is the same
    phrase (every alias of its pid, and the stored record's id) and return
    the one that matches the stored hash exactly. Anything else — another
    verdict, comment, notes or phrase — keeps the replay's own digest, so the
    caller's check still refuses it."""
    stored = existing.get("idempotency_fingerprint")
    if stored is None or stored == fingerprint:
        return fingerprint
    phrase_id = payload.get("phrase_id")
    if not phrase_id:
        return fingerprint
    import ledger as _ledger_mod
    id_map = _ledger_mod._phrase_id_map()
    pid = id_map.get(phrase_id, phrase_id)
    candidates = {existing.get("phrase_id"), pid}
    if pid is not None:
        candidates.update(alias for alias, target in id_map.items() if target == pid)
    for candidate in candidates:
        if candidate and candidate != phrase_id and \
                _ledger_mod._same_phrase_id(candidate, phrase_id):
            if _idempotency_fingerprint({**payload, "phrase_id": candidate}) == stored:
                return stored
    return fingerprint


def rate(
    rating: int | str,
    label: str | None = None,
    notes: list[dict] | None = None,
    phrase_id: str | None = None,
    comment: str | None = None,
    idempotency_key: str | None = None,
    role: str | None = None,
    key: str | None = None,
    entry_id: str | None = None,
    gear: int | None = None,
    source: str | None = None,
    move: str | None = None,
) -> dict:
    """Append a like/dislike rating for feedback-driven retrieval.

    Pass ``role`` when golding a bass or drum idea: a gold with no role and no
    index id joins the LEAD answer bank, which is how two sub-C1 bass keeps
    ended up served as leads on 2026-09-02. ``key`` records the tonal reading
    the rating was made under, so the taste model trains and serves under the
    same one. ``entry_id``/``gear``/``source`` carry the judgment provenance
    (which ledger row, which gearbox gear, which stamp) so a later reader can
    tell a composed gold from a catalog keep. ``move`` names the transform or
    surgery that was heard (a DROP then bans the pairing, not the source)."""
    norm = _normalize_rating(rating)
    if not notes and not phrase_id:
        raise ValueError("provide notes and/or phrase_id so retrieval can learn")
    normalized_notes = _note_dicts(notes) if notes else None
    # role/key join the fingerprint so two ratings that differ only by role are
    # distinguishable. Safe: rate() only compares fingerprints under an explicit
    # idempotency_key, and no stored record carries one.
    fingerprint_payload = {
        "kind": "rating",
        "rating": norm,
        "label": label,
        "phrase_id": phrase_id,
        "comment": comment,
        "notes": normalized_notes,
        "role": role,
        "key": key,
        "entry_id": entry_id,
        "gear": gear,
        "source": source,
        "move": _canonical_move(move),
    }
    idempotency_fingerprint = _idempotency_fingerprint(fingerprint_payload)

    with _ratings_file_lock():
        data = _load_ratings_file()
        if idempotency_key:
            existing = next(
                (item for item in data["ratings"]
                 if item.get("idempotency_key") == idempotency_key),
                None,
            )
            if existing is not None:
                _require_matching_idempotency(
                    existing,
                    _alias_matched_fingerprint(existing, fingerprint_payload,
                                               idempotency_fingerprint),
                    idempotency_key,
                )
                out = {
                    "ok": True,
                    "idempotent": True,
                    "record": {k: v for k, v in existing.items() if k != "notes"},
                }
                existing_label = str(existing.get("label") or "").lower()
                if existing_label in ({"gold"} | _GOLD_UNMARK_LABELS):
                    # the rating is already persisted; a failed retrain must
                    # not fail the replay — report the model stale instead
                    try:
                        model = _retrain_taste()
                        out["taste_retrained"] = {
                            "n_pairs": model["n_pairs"],
                            "n_gold_pairs": model["n_gold_pairs"],
                            "train_accuracy": model["train_accuracy"],
                        }
                    except Exception as exc:
                        out["taste_retrained"] = _stale_retrain_report(exc)
                return out
        rec = {
            "id": len(data["ratings"]) + 1,
            "rating": norm,
            "label": label,
            "phrase_id": phrase_id,
            "comment": comment,
            "notes": notes,
            "role": (str(role).lower() if role else None),
            "key": key,
            "entry_id": entry_id,
            "gear": gear,
            "source": source,
            "move": _canonical_move(move),
            "idempotency_key": idempotency_key,
            "idempotency_fingerprint": idempotency_fingerprint,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        data["ratings"].append(rec)
        _save_ratings_file_unlocked(data)
        # gold membership changed → the taste model retrains NOW (golds are training
        # signal, not just retrieval boosts); guarded inside _retrain_taste
        taste_update = None
        if str(label or "").lower() in ({"gold"} | _GOLD_UNMARK_LABELS):
            # the rating above is already saved; a retrain crash must not raise
            # past it — in judge_answer that error would look like a failed
            # rating and the ledger verdict would never be written
            try:
                m = _retrain_taste()
                taste_update = {"n_pairs": m["n_pairs"], "n_gold_pairs": m["n_gold_pairs"],
                                "train_accuracy": m["train_accuracy"]}
            except Exception as exc:
                taste_update = _stale_retrain_report(exc)
    out = {
        "ok": True,
        "record": {k: v for k, v in rec.items() if k != "notes"},
    }
    if taste_update is not None:
        out["taste_retrained"] = taste_update
    return out


_BRIEF_ROLE_WORDS = (
    ("bass", r"\bbass(?:line| line|-line)?\b"),
    ("lead", r"\blead\b|\bmelod\w*\b"),
    ("drum", r"\bdrums?\b"),
    ("pad", r"\bpad\b"),
)
# Drum words win over bass when both are present ("bass drum", "drums with
# a bassline" are drum asks, not bass ones).
_BRIEF_DRUM_RE = re.compile(r"\bdrums?\b", re.IGNORECASE)
_BRIEF_DNB_RE = re.compile(
    r"\b(?:drum\s*(?:and|&|n|'n'?)\s*bass|dnb|d&b|d'n'b)\b", re.IGNORECASE)
# Key only from an explicit note name + mode ("A minor", "F# maj", "Dm") —
# a bare "minor"/"major" never carries a tonic, so it hints nothing. A
# lowercase "a" is ambiguous with the article and is accepted only with a
# complete mode word; the mode spelling itself is case-insensitive.
_BRIEF_KEY_RE = re.compile(
    r"\b([A-Ga-g])([#b])?\s*((?i:major|minor|maj|min|m))\b")
_BRIEF_MODE_CANON = {"major": "major", "minor": "minor",
                     "maj": "major", "min": "minor", "m": "minor"}
_BRIEF_TEMPO_RE = re.compile(r"(\d+(?:\.\d+)?)\s*bpm\b", re.IGNORECASE)
_BRIEF_TEMPO_WORD_RE = re.compile(r"\btempo\s+(\d+(?:\.\d+)?)\b", re.IGNORECASE)


def _parse_brief_hints(request: str) -> dict:
    """Role/key/tempo hints heard in a compose request. Conservative: only
    spellings that map onto the retrieval vocabulary (a 'harmony' ask is not
    an index role, so it stays unhinted rather than emptying the pool)."""
    lowered = str(request or "")
    role_text = _BRIEF_DNB_RE.sub(" ", lowered)
    role = None
    for name, pattern in _BRIEF_ROLE_WORDS:
        if re.search(pattern, role_text, re.IGNORECASE):
            role = name
            break
    if role == "bass" and _BRIEF_DRUM_RE.search(role_text):
        role = "drum"
    key = None
    for match in _BRIEF_KEY_RE.finditer(lowered):
        raw_tonic, _, raw_mode = match.groups()
        mode_word = raw_mode.lower() in ("major", "minor")
        preceding = (lowered[match.start(1) - 2:match.start(1)].lower()
                     if match.start(1) >= 2 else "")
        preceded_by_first_person = preceding in ("i ", "a ")
        if (raw_tonic == "a" and not mode_word) or (
                raw_tonic == "A" and raw_mode.lower() == "m" and
                preceded_by_first_person):
            continue
        tonic = match.group(1).upper() + (match.group(2) or "").lower()
        tonic = _FLAT_TO_SHARP.get(tonic, tonic)
        candidate = "%s %s" % (
            tonic, _BRIEF_MODE_CANON[match.group(3).lower()])
        try:
            _parse_key_string(candidate)
            key = candidate
            break
        except ValueError:
            continue
    tempo = None
    tmatch = _BRIEF_TEMPO_RE.search(lowered) or _BRIEF_TEMPO_WORD_RE.search(lowered)
    if tmatch:
        tempo = float(tmatch.group(1))
        if not 30 < tempo < 300:
            tempo = None
    return {"role": role, "key": key, "tempo": tempo}


def _rotate_seed(items: list, seed: int) -> list:
    """Deterministic re-roll: seed k starts the list at position k."""
    if not items or not seed:
        return list(items)
    k = int(seed) % len(items)
    return list(items[k:]) + list(items[:k])


_BRIEF_HARMONIC_K = 4
_BRIEF_HARMONIC_POOL = 8
_BRIEF_DRUM_K = 2
_BRIEF_DRUM_POOL = 4


def _seed_window(items: list, seed: int, n: int) -> list:
    """Seed k serves a window of n over the candidate pool instead of
    reordering the same n: different seeds select different exemplars.
    Small pools fall back to a rotation (no duplicates); deterministic."""
    if not items:
        return []
    if len(items) <= n:
        return _rotate_seed(items, seed)
    start = int(seed) % len(items)
    return [items[(start + i) % len(items)] for i in range(n)]


def compose_brief(request: str, seed: int = 0) -> dict:
    """Bundle style context + phrases + optional structure for Claude to compose from.

    Role/key/tempo hints in the request reach find_phrases (a hint that
    empties the pool falls back to the unhinted call, so hints narrow but
    never starve); ``seed`` rotates the served exemplars deterministically."""
    style = get_style()
    song_scale = any(w in request.lower() for w in (
        "song", "track", "arrange", "arrangement", "minutes", "full", "structure",
    ))
    minutes = None
    for token in request.replace(",", " ").split():
        try:
            val = float(token.rstrip("m."))
            if 1 <= val <= 30:
                minutes = val
                break
        except ValueError:
            continue

    hints = _parse_brief_hints(request)
    harmonic_role = hints["role"] if hints["role"] in ("lead", "bass", "pad") else None
    drum_role = "drum" if hints["role"] == "drum" else None
    phrases = _seed_window(
        find_phrases(kind="harmonic", k=_BRIEF_HARMONIC_POOL,
                     role=harmonic_role, key=hints["key"],
                     tempo=hints["tempo"]),
        seed, _BRIEF_HARMONIC_K)
    if not phrases and any(hints.values()):
        phrases = _seed_window(
            find_phrases(kind="harmonic", k=_BRIEF_HARMONIC_POOL), seed,
            _BRIEF_HARMONIC_K)
    drum_phrases = _seed_window(
        find_phrases(kind="drum", k=_BRIEF_DRUM_POOL, role=drum_role,
                     tempo=hints["tempo"]),
        seed, _BRIEF_DRUM_K)
    if not drum_phrases and (drum_role or hints["tempo"]):
        drum_phrases = _seed_window(
            find_phrases(kind="drum", k=_BRIEF_DRUM_POOL), seed,
            _BRIEF_DRUM_K)
    structure = (
        _fold_persisted_session_takes(suggest_structure(target_minutes=minutes))
        if song_scale else None
    )

    return {
        "request": request,
        "request_hints": {**hints, "seed": seed},
        "guidance": (
            "You (Claude) are the composer. Use the style facts and real phrase notes below "
            "as steering and material to adapt — do NOT paste verbatim unless intentional. "
            "Combine, transpose, and develop; then score_phrase your result and export_midi."
        ),
        "style_summary": style["structured"],
        "style_md_excerpt": style["style_md"][:4000],
        "approved_rhythms": approved_rhythms_data(),
        "example_phrases": phrases,
        "example_drum_grooves": drum_phrases,
        "suggested_structure": structure,
    }


# Producer Pal note codec lives in pp_codec (pure stdlib). Re-export under the
# historical private names so existing internal imports / selftests keep working.
import pp_codec as _pp_codec  # noqa: E402

_midi_to_pitch_name = _pp_codec.midi_to_pitch_name
_pitch_name_to_midi = _pp_codec.pitch_name_to_midi
_is_pp_duration_token = _pp_codec.is_pp_duration_token
_is_pp_pitch_token = _pp_codec.is_pp_pitch_token
_is_pp_prob_token = _pp_codec.is_pp_prob_token
_parse_pp_position_beats = _pp_codec.parse_pp_position_beats
_expand_pp_positions = _pp_codec.expand_pp_positions
pp_notes_to_notes = _pp_codec.pp_notes_to_notes
_beats_to_bar_beat = _pp_codec.beats_to_bar_beat
_duration_to_pp = _pp_codec.duration_to_pp
_bar_beat_to_beats = _pp_codec.bar_beat_to_beats
_pp_duration_to_beats = _pp_codec.pp_duration_to_beats
_normalize_notes_for_pp = _pp_codec.normalize_notes_for_pp
_pp_normalized_note_count = _pp_codec.pp_normalized_note_count
_notes_to_pp_string = _pp_codec.notes_to_pp_string

# Re-export moved functions for backward compatibility
export_midi = __import__("mcp_server.export_bridge", fromlist=["export_midi"]).export_midi
send_to_ableton = __import__("mcp_server.export_bridge", fromlist=["send_to_ableton"]).send_to_ableton
_resolve_export_path = __import__("mcp_server.export_bridge", fromlist=["_resolve_export_path"])._resolve_export_path
