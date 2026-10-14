"""Does Ricardo's historical comping agree with his explicit taste ratings?

Read-only experiment. Trains a THROWAWAY Bradley-Terry model on the mined
comping pairs (lead role only) and tests it against the explicit rated rounds
in ratings.json (his 14 frozen pairs). Writes nothing — no store artifact, no
taste model mutation. The verdict decides whether the 622-pair goldmine feeds
the real model.

Decision rule (pre-registered):
  cross-accuracy >= 0.70  -> comping IS his taste; merge (as a separate batch).
  0.55 - 0.70             -> weak alignment; keep segregated, revisit with more
                             explicit pairs from the booth.
  <= 0.55                 -> comping measured something else (recency, workflow);
                             the manifest stays an archive, not training data.

Run: .venv/bin/python tools/validate_comping_signal.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
import core  # noqa: E402
import taste  # noqa: E402

MANIFEST = os.path.join(ROOT, "tools/ideas/comping_pairs.json")


def comping_lead_pairs():
    m = json.load(open(MANIFEST))
    pairs, n_winners = [], 0
    for pr in m["pairs"]:
        if pr["role"] != "lead" or pr.get("is_drum"):
            continue
        try:
            fw = core._taste_features(pr["winner"]["notes"], role="lead")
        except Exception:
            continue
        added = False
        for loser in pr["losers"]:
            try:
                fl = core._taste_features(loser["notes"], role="lead")
            except Exception:
                continue
            pairs.append((fw, fl))
            added = True
        n_winners += added
    return pairs, n_winners


def main():
    print("featurizing comping lead pairs (winner/loser -> 9 taste features)...")
    comp_pairs, n_winners = comping_lead_pairs()
    print(f"  {len(comp_pairs)} lead A/B pairs from {n_winners} comped winners")
    if len(comp_pairs) < 10:
        print("  too few lead pairs to conclude anything — stop here.")
        return

    explicit_pairs = core._taste_pairs_from_rounds(
        core._load_ratings_file().get("candidate_rounds", []))
    print(f"  {len(explicit_pairs)} explicit pairs from rated rounds (frozen features)")

    dim = len(core._TASTE_FEATURE_NAMES)
    w_comp = taste.train(comp_pairs, dim)
    fit = taste.accuracy(comp_pairs, w_comp)
    cross = taste.accuracy(explicit_pairs, w_comp) if explicit_pairs else float("nan")

    shipped = core._taste_model().get("weights") or []
    shipped_on_comp = taste.accuracy(comp_pairs, shipped) if shipped else float("nan")

    print(f"\ncomping-trained model: fit on its own pairs        = {fit:.2f}")
    print(f"comping-trained model: accuracy on EXPLICIT pairs  = {cross:.2f}   <- the verdict")
    print(f"shipped explicit model: accuracy on comping pairs  = {shipped_on_comp:.2f}")

    print("\nwhat each signal rewards (weight per feature):")
    print(f"  {'feature':15s} {'comping':>9s} {'explicit':>9s}")
    for i, name in enumerate(core._TASTE_FEATURE_NAMES):
        sw = shipped[i] if i < len(shipped) else 0.0
        print(f"  {name:15s} {w_comp[i]:9.2f} {sw:9.2f}")

    if cross >= 0.70:
        print("\n-> ALIGNED: your comping decisions predict your explicit ratings. "
              "The 622-pair goldmine is real taste data; merging (as its own batch) is justified.")
    elif cross >= 0.55:
        print("\n-> WEAK: some overlap, not conclusive. Keep comping pairs segregated; "
              "judge more booth pairs, then re-run this.")
    else:
        print("\n-> NOT ALIGNED: comping optimized something else (recency/workflow, not taste). "
              "Manifest stays an archive; nothing trains on it.")


if __name__ == "__main__":
    main()
