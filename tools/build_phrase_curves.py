"""Build the phrase-curve library from harvested automation.

store/automation.json (1,149 raw envelopes) → store/phrase_curves.json (deduped,
normalized gesture templates with provenance). Read-only over the harvest; undo =
delete the artifact. Run after harvest_arrangement_dna.py.

Run: .venv/bin/python tools/build_phrase_curves.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
import curves  # noqa: E402

AUTOMATION = os.path.join(ROOT, "store/automation.json")


def main():
    if not os.path.exists(AUTOMATION):
        print("store/automation.json missing — run tools/harvest_arrangement_dna.py first")
        return
    automation = json.load(open(AUTOMATION))
    lib = curves.build_curve_library(automation)
    import core
    core._save_json_atomic(curves.CURVES_JSON, lib, indent=2)
    print(f"{lib['n_gesture_segments']} gesture segments -> {lib['n_templates']} templates")
    for bucket, n in lib["by_bucket"].items():
        print(f"  {bucket:24s} {n}")
    print("\nsample provenance:")
    for t in lib["templates"][:3]:
        p = t["provenance"]
        print(f"  {t['family']}/{t['class']}/{t['dur_bucket']}bar  "
              f"depth={t['depth']:.2f}  <- [{p['project']}] {p['track']!r} @beat {p['t0_beats']}")
    print(f"\nartifact: {os.path.relpath(curves.CURVES_JSON, ROOT)}")


if __name__ == "__main__":
    main()
