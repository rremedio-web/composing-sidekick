"""Build Ricardo's gesture vocabulary → store/motifs.json.

Graduates the read-only `motif_scan.py` into a writer. It reduces every pitched
('harmonic') phrase to its top-voice melody, converts to chroma-offset
scale-degrees `(pitch - tonic) % 12` relative to each project's tonic, and
collects his recurring 3- and 4-note melodic CELLS ranked by cross-song support.
Each cell is tagged into one of five human-named classes (climb / fall / wobble /
dark_landing / repeat). It also emits a corpus 3-gram distribution used as the
reference for the scorer's Jensen-Shannon `gesture_fit`.

Chroma-offset convention matches `melodic_grammar`, `motif_scan.py` and the lead
generator, so cells are key/mode-independent (b7 is always offset 10).

Run (offline build, writes the artifact):
    python3 analyze/motifs.py
"""
import collections
import json
import math
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from corpus_keys import song_family  # noqa: E402
from motif_scan import (  # noqa: E402  (reuse the read-only scanner primitives)
    _DEG,
    _PC,
    LEAD_MIN_MEDIAN,
    ngrams,
    skyline,
    tonic_pc,
)

STORE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "store")
MOTIFS_JSON = os.path.join(STORE, "motifs.json")

MIN_COUNT = 20      # drop rare cells
MIN_SUPPORT = 2     # drop one-project (loop) artifacts
TOP_PER_LENGTH = 40
CLASSES = ("climb", "fall", "wobble", "dark_landing", "repeat")
_STABLE = {0, 7}    # degree 1 and 5 (chroma offsets)


def _git_rev() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        ).strip()
        return out or "unknown"
    except Exception:
        return "unknown"


def _iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_meta(n_projects: int, schema_version: int = 1) -> dict:
    return {
        "schema_version": schema_version,
        "built_at": _iso_utc(),
        "code_rev": _git_rev(),
        "corpus": {"n_projects": int(n_projects)},
    }


def _save_json_atomic(path: str, data: dict, indent: int | None = 1) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(fd, "w") as fh:
            if indent is not None:
                json.dump(data, fh, indent=indent)
            else:
                json.dump(data, fh)
            fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _wrap(d: int) -> int:
    """Signed minimal chroma interval in [-5, 6] (so 10->0 reads as +2, not -10)."""
    d %= 12
    return d - 12 if d > 6 else d


def _intervals(degs: tuple[int, ...]) -> list[int]:
    return [_wrap(b - a) for a, b in zip(degs, degs[1:])]


def _alternating_sign(nz: list[int]) -> bool:
    return all(nz[i] * nz[i + 1] < 0 for i in range(len(nz) - 1))


def classify(degs: tuple[int, ...], ivs: list[int]) -> str:
    """Tag a cell into one of the 5 classes (priority order; first match wins)."""
    nz = [d for d in ivs if d != 0]
    # 1. repeat — mostly static (>= half the moves are unison)
    if len(nz) <= len(ivs) / 2:
        return "repeat"
    # 2. dark landing — ends on 1/5, reached via b7 (10), no leading tone (11)
    if degs[-1] in _STABLE and 10 in degs and 11 not in degs and 10 in degs[-3:]:
        return "dark_landing"
    # 3. wobble — neighbor figure: small steps, returns to start
    if all(abs(d) <= 2 for d in nz) and abs(sum(ivs)) <= 1 and degs[0] == degs[-1]:
        return "wobble"
    if all(abs(d) <= 2 for d in nz) and _alternating_sign(nz) and abs(sum(ivs)) <= 2:
        return "wobble"
    # 4/5. climb / fall by net direction
    s = sum(ivs)
    up = sum(1 for d in nz if d > 0)
    down = sum(1 for d in nz if d < 0)
    if s > 0 and up >= down:
        return "climb"
    if s < 0 and down >= up:
        return "fall"
    return "climb" if s > 0 else "fall"


def build() -> dict:
    with open(os.path.join(STORE, "phrase_index.json"), encoding="utf-8") as fh:
        idx = json.load(fh)
    phrases = idx["phrases"] if isinstance(idx, dict) else idx
    with open(os.path.join(STORE, "style_profile.json"), encoding="utf-8") as fh:
        sp = json.load(fh)
    key_of = {p["name"]: p["key"] for p in sp.get("projects", [])
              if p.get("has_harmonic") and p.get("key")}

    counts = {3: collections.Counter(), 4: collections.Counter()}
    support = {3: collections.defaultdict(set), 4: collections.defaultdict(set)}
    unigram = collections.Counter()
    trigram = collections.Counter()
    # `used` counts one valid (melodic line, song family) vote after all
    # filters, not raw phrase records or project files.
    used = projects_with_key = 0
    seen_lines: set = set()  # (line, family) votes: Save-As copies vote once

    for ph in phrases:
        if ph.get("kind") != "harmonic":
            continue
        if ph.get("is_sidekick") or str(ph.get("track", "")).strip().upper().startswith("SIDEKICK"):
            continue  # sidekick output, not his playing (also filtered at build_index)
        line = skyline(ph.get("notes", []))
        if len(line) < 4:
            continue
        if sorted(line)[len(line) // 2] < LEAD_MIN_MEDIAN:
            continue
        deltas = [b - a for a, b in zip(line, line[1:])]
        if len(set(line)) < 4 or sum(1 for d in deltas if d) / len(deltas) < 0.5:
            continue
        line_key = tuple(line)
        # One deduped record covers every project that played this note-set,
        # so families come from provenance (each entry: project, track, clip,
        # window, alias). Old records without provenance keep the survivor's
        # project, exactly as before.
        provenance = ph.get("provenance") or []
        if not provenance:
            provenance = [{"project": ph.get("project")}]
        family_project = {}
        for entry in provenance:
            project = entry.get("project")
            family = song_family(project)
            if family and family not in family_project:
                family_project[family] = project
        for family, project in sorted(family_project.items()):
            if (line_key, family) in seen_lines:
                continue
            key = key_of.get(project)
            if not key:
                continue
            tpc = tonic_pc(key)
            if tpc is None:
                continue
            seen_lines.add((line_key, family))
            used += 1
            degrees = [(p - tpc) % 12 for p in line]
            for d in degrees:
                unigram[d] += 1
            for g in ngrams(degrees, 3):
                trigram[",".join(map(str, g))] += 1
            for n in (3, 4):
                for g in set(ngrams(degrees, n)):  # once per (line, family)
                    counts[n][g] += 1
                    support[n][g].add(family)

    projects_with_key = len(key_of)

    cells: list[dict] = []
    for n in (3, 4):
        ranked = sorted(
            (g for g, c in counts[n].items() if c >= MIN_COUNT and len(support[n][g]) >= MIN_SUPPORT),
            key=lambda g: (len(support[n][g]), counts[n][g]),
            reverse=True,
        )[:TOP_PER_LENGTH]
        for g in ranked:
            sup = len(support[n][g])
            ivs = _intervals(g)
            cells.append({
                "degrees": list(g),
                "label": "-".join(_DEG[d] for d in g),
                "length": n,
                "count": counts[n][g],
                "support": sup,
                "weight": round(counts[n][g] * (1 + math.log2(sup)), 2),
                "intervals": ivs,
                "class": classify(g, ivs),
                "boundary_in": g[0],
                "boundary_out": g[-1],
            })

    class_index: dict[str, list[int]] = {c: [] for c in CLASSES}
    for i, cell in enumerate(cells):
        class_index[cell["class"]].append(i)

    uni_total = sum(unigram.values()) or 1
    tri_total = sum(trigram.values()) or 1
    n_projects = len(sp.get("projects", [])) or len(set(p.get("project") for p in phrases if p.get("project")))
    return {
        "meta": build_meta(n_projects),
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {
            "n_phrases_used": used,
            "n_projects_with_key": projects_with_key,
            "min_count": MIN_COUNT,
            "min_support": MIN_SUPPORT,
            "lead_min_median": LEAD_MIN_MEDIAN,
        },
        "degree_labels": _DEG,
        "cells": cells,
        "class_index": class_index,
        "unigram_degrees": {str(d): round(c / uni_total, 6) for d, c in unigram.most_common()},
        "trigram_distribution": {k: round(c / tri_total, 6) for k, c in trigram.most_common()},
    }


def _self_check(data: dict) -> None:
    cells = data["cells"]
    assert cells, "no cells extracted"
    assert all(c["length"] in (3, 4) for c in cells), "bad cell length"
    assert all(c["class"] in CLASSES for c in cells), "bad class tag"
    # known-vocabulary class tags (chroma offsets)
    cases = {
        (0, 3, 5, 7): "climb",       # 1-b3-4-5
        (7, 5, 3): "fall",           # 5-4-b3
        (5, 7, 5): "wobble",         # 4-5-4
        (7, 10, 0): "dark_landing",  # 5-b7-1
        (4, 10, 0): "dark_landing",  # 4-b7-1
        (0, 0, 0): "repeat",         # 1-1-1
    }
    for degs, want in cases.items():
        got = classify(degs, _intervals(degs))
        assert got == want, f"classify{degs} = {got!r}, want {want!r}"
    tri_sum = sum(data["trigram_distribution"].values())
    assert abs(tri_sum - 1.0) < 1e-3, f"trigram sum {tri_sum}"


def main() -> None:
    data = build()
    _self_check(data)
    _save_json_atomic(MOTIFS_JSON, data, indent=1)
    cells = data["cells"]
    by_class = collections.Counter(c["class"] for c in cells)
    print(f"wrote {MOTIFS_JSON}: {len(cells)} cells from "
          f"{data['source']['n_phrases_used']} phrases | classes {dict(by_class)}")
    print("top cells by support:")
    for c in sorted(cells, key=lambda c: (c["support"], c["count"]), reverse=True)[:12]:
        print(f"  {c['label']:<16} {c['class']:<13} support {c['support']:>3}  "
              f"count {c['count']:>4}  w {c['weight']:.0f}")
    print("self-checks passed")


if __name__ == "__main__":
    main()
