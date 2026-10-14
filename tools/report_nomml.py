"""NOMML over the corpus: distribution report + store/nomml.json sidecar.

Compute-and-REPORT only — per the unproven-signal rule, nothing consumes the
performed/programmed split until Ricardo has eyeballed (and eared) it here.
The sidecar is regenerable bulk (gitignored); consumers-to-be (groove-DNA
harvest, candidate mining) get a performed_only flag in a LATER change, default
off, only after his confirm.

Prints the level distribution, the performed share by role and clip_source
(take lanes should skew performed — a built-in sanity check), and a seeded
10+10 spot-check list; --export writes those 20 as nomml_check_*.mid so the
spot-check is ear-checkable, not just eyeball-checkable.

Run: .venv/bin/python tools/report_nomml.py [--export]
"""
import json
import os
import random
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "ingest"))
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
from nomml import GRIDS, OFF_GRID, EPSILON_BEATS, nomml  # noqa: E402

SIDECAR = os.path.join(ROOT, "store/nomml.json")
SEED = 40


def main():
    idx = json.load(open(os.path.join(ROOT, "store/phrase_index.json")))
    phrases = idx if isinstance(idx, list) else idx.get("phrases", [])

    per, dist = {}, Counter()
    by_role, by_source, by_joint = {}, {}, {}
    for p in phrases:
        r = nomml(p.get("notes", []))
        if r["nomml"] is None:
            continue
        per[p["id"]] = {"nomml": r["nomml"], "performed": r["performed"],
                        "off_grid_share": round(r["off_grid_share"], 4),
                        "nomml_onsets": r["nomml_onsets"],
                        "performed_onsets": r["performed_onsets"],
                        "off_grid_share_onsets": round(r["off_grid_share_onsets"], 4)}
        dist[r["nomml"]] += 1
        joint = "%s/%s" % (p.get("role", "?"), p.get("clip_source", "?"))
        for key, bucket in ((p.get("role", "?"), by_role),
                            (p.get("clip_source", "?"), by_source),
                            (joint, by_joint)):
            n_all, n_p, n_po = bucket.get(key, (0, 0, 0))
            bucket[key] = (n_all + 1, n_p + (1 if r["performed"] else 0),
                           n_po + (1 if r["performed_onsets"] else 0))

    import core
    core._save_json_atomic(SIDECAR, {
        "epsilon_beats": EPSILON_BEATS, "grids": GRIDS,
        "off_grid_level": OFF_GRID, "phrases": per
    }, indent=1)

    total = sum(dist.values())
    n_perf = sum(1 for v in per.values() if v["performed"])
    n_perf_o = sum(1 for v in per.values() if v["performed_onsets"])
    n_disagree = sum(1 for v in per.values()
                     if v["performed"] != v["performed_onsets"])
    print("NOMML over %d phrases -> %s" % (total, os.path.relpath(SIDECAR, ROOT)))
    print("level distribution, note-weighted (0=bar .. %d=off-grid):" % OFF_GRID)
    for lvl in sorted(dist):
        grid = "off-grid" if lvl == OFF_GRID else "%.4g beats" % GRIDS[lvl]
        print("  %2d %-12s %6d  %s" % (lvl, grid, dist[lvl],
                                       "#" * max(1, round(60 * dist[lvl] / total))))
    print("PERFORMED  note-weighted: %d / %d = %.1f%%   unique-onset: %d = %.1f%%   "
          "(disagree on %d)"
          % (n_perf, total, 100.0 * n_perf / total, n_perf_o,
             100.0 * n_perf_o / total, n_disagree))
    for name, bucket in (("role", by_role), ("clip_source", by_source),
                         ("role/source", by_joint)):
        print("by %s (performed: notes | onsets):" % name)
        for key, (n_all, n_p, n_po) in sorted(bucket.items(), key=lambda kv: -kv[1][0]):
            print("  %-24s %6d phrases, %5.1f%% | %5.1f%%"
                  % (key, n_all, 100.0 * n_p / n_all if n_all else 0.0,
                     100.0 * n_po / n_all if n_all else 0.0))

    rnd = random.Random(SEED)
    performed = sorted(pid for pid, v in per.items() if v["performed"])
    programmed = sorted(pid for pid, v in per.items() if not v["performed"])
    picks = {"performed": rnd.sample(performed, min(10, len(performed))),
             "programmed": rnd.sample(programmed, min(10, len(programmed)))}
    by_id = {p.get("id"): p for p in phrases}
    export = None
    if "--export" in sys.argv:
        try:
            import core
            export = core.export_midi
        except Exception as e:
            print("(--export unavailable: %s)" % e)
    print("\nspot-check (SEED=%d) — do these split the way your ear says?" % SEED)
    for kind, pids in picks.items():
        print(" %s:" % kind.upper())
        for i, pid in enumerate(pids, 1):
            v, p = per[pid], by_id[pid]
            line = "  %-34s %-12s n=%-4d off-grid %.0f%%" % (
                pid, p.get("clip_source", "?"), v and by_id[pid].get("n_notes", 0),
                100 * v["off_grid_share"])
            if v["performed"] != v["performed_onsets"]:
                line += "  [unique-onset variant disagrees]"
            if export is not None:
                fn = "nomml_check_%s_%02d.mid" % (kind, i)
                try:
                    export(p["notes"], fn, tempo=float(p.get("tempo") or 120.0))
                    line += "  -> exports/%s" % fn
                except Exception as e:
                    line += "  (export failed: %s)" % e
            print(line)
    print("\ngate stays OFF until you confirm this split (then: performed_only flags).")


if __name__ == "__main__":
    main()
