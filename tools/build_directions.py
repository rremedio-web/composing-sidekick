"""Direction axes v1: place every his-confirmed project on the computable dials.

From style_profile.json per-project stats (no new .als reads, per his decision):
  darker_brighter  (-1 = darker):  slow + minor + low register
  song_club        (+1 = club):    tempo near ~122 + drum share
  sparse_maximal   (-1 = sparse):  low note density
The runtime-only axes (faithful_alien, human_machine, romantic_brutal,
clean_damaged) stay LLM-interpreted — no corpus stat pretends to measure them.

Writes store/directions.json: per-project axis values in [-1, 1] + pole exemplars
(top 5 project names per pole) that gear-6 COVER contracts cite as translation
targets. Regenerable; gitignored.

Run: python3 tools/build_directions.py  (after style_profile.py)
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
import core  # noqa: E402

OUT = os.path.join(ROOT, "store", "directions.json")


def _z(values):
    n = len(values)
    if n < 2:
        return [0.0] * n
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    sd = var ** 0.5 or 1.0
    return [(v - mean) / sd for v in values]


def _clamp(v):
    return max(-1.0, min(1.0, v))


def compute_axes(projects: list[dict]) -> dict:
    """projects: style_profile.json 'projects' records (his-confirmed only).
    Deterministic; returns {project_name: {axis: value}}."""
    rows = [p for p in projects if p.get("has_harmonic") and p.get("tempo")]
    if not rows:
        return {}
    tempo_z = _z([float(p["tempo"]) for p in rows])
    pitch_z = _z([float(p.get("pitch_mean", 60.0)) for p in rows])
    dens_z = _z([float(p.get("density_notes_per_beat", 1.0)) for p in rows])
    drum_z = _z([p.get("drum_notes", 0) /
                 max(1, p.get("drum_notes", 0) + p.get("harmonic_notes", 1))
                 for p in rows])
    club_prox = [1.0 - min(abs(float(p["tempo"]) - 122.0) / 30.0, 1.0) for p in rows]

    out = {}
    for i, p in enumerate(rows):
        minor = 1.0 if str(p.get("key", "")).endswith("minor") else -1.0
        name = p["name"]
        darkness = 0.5 * minor - 0.25 * tempo_z[i] - 0.25 * pitch_z[i]
        out[name] = {
            "darker_brighter": _clamp(-darkness),          # -1 = darker
            "song_club": _clamp(0.6 * (club_prox[i] * 2 - 1) + 0.4 * drum_z[i]),
            "sparse_maximal": _clamp(dens_z[i]),           # -1 = sparse
        }
    return out


def pole_exemplars(axes: dict, k: int = 5) -> dict:
    ex = {}
    for axis in ("darker_brighter", "song_club", "sparse_maximal"):
        ranked = sorted(axes.items(), key=lambda kv: kv[1][axis])
        ex[axis] = {"neg_pole": [n for n, _ in ranked[:k]],
                    "pos_pole": [n for n, _ in ranked[-k:]][::-1]}
    return ex


def main():
    sp = json.load(open(os.path.join(ROOT, "store", "style_profile.json")))
    axes = compute_axes(sp.get("projects", []))
    art = {"schema_version": 1, "n_projects": len(axes),
           "axes": axes, "exemplars": pole_exemplars(axes),
           "note": "darker_brighter -1=darker · song_club +1=club · sparse_maximal -1=sparse"}
    core._save_json_atomic(OUT, art, indent=2)
    ex = art["exemplars"]
    print(f"{len(axes)} projects placed on 3 axes -> store/directions.json")
    print("darkest:", ex["darker_brighter"]["neg_pole"])
    print("clubbiest:", ex["song_club"]["pos_pole"])
    print("sparsest:", ex["sparse_maximal"]["neg_pole"])


if __name__ == "__main__":
    main()
