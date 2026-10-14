"""Do FEEL features separate comping winners from losers where melody failed?

The constructive twin of validate_comping_signal.py (which falsified the melodic
reading at fit 0.42). Trains throwaway Bradley-Terry models on the 622-pair comping
archive using mcp_server/feel.py's 9 performance features. Writes nothing.

PRE-REGISTERED GATE (decided before running):
  PASS  = lead in-sample fit > 0.65 AND split-half (by winner) >= 0.60
  Even on PASS, nothing wires into humanize/taste/score — that requires the booth
  cross-check to agree (>= 0.60 direction agreement on explicit pairs, auto-runs
  when rated rounds with raw notes exist) AND Ricardo's explicit approval.
  FAIL  = feel features join the melodic ones as an archive finding.

Run: .venv/bin/python tools/validate_feel_signal.py
"""
import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
import core  # noqa: E402  (tool-side only — feel.py itself never touches core)
import feel  # noqa: E402
import taste  # noqa: E402

MANIFEST = os.path.join(ROOT, "tools/ideas/comping_pairs.json")
GATE_FIT = 0.65
GATE_SPLIT = 0.60
GATE_CROSS = 0.60


def _slot_mean(role):
    vb = (core._aggregate().get("velocity_by_role") or {}).get(role) or {}
    return vb.get("slot_mean")


def collect(roles=None, with_density=False):
    """(winner_features, loser_features) pairs grouped by winner, from the manifest."""
    m = json.load(open(MANIFEST))
    grouped = []
    for pr in m["pairs"]:
        if roles is not None and pr["role"] not in roles:
            continue
        sm = _slot_mean(pr["role"])
        fw = feel.feel_features(pr["winner"]["notes"], sm)
        if with_density:
            fw = fw + [feel.density_feature(pr["winner"]["notes"])]
        wl = []
        for loser in pr["losers"]:
            fl = feel.feel_features(loser["notes"], sm)
            if with_density:
                fl = fl + [feel.density_feature(loser["notes"])]
            wl.append((fw, fl))
        if wl:
            grouped.append({"pairs": wl, "reduction": bool(pr.get("winner_is_reduction"))})
    return grouped


def flat(grouped):
    return [p for g in grouped for p in g["pairs"]]


def split_half(grouped, dim):
    """Train on even-indexed winners, test on odd — winner-level split avoids
    loser-correlation leakage between train and test."""
    train_g = [g for i, g in enumerate(grouped) if i % 2 == 0]
    test_g = [g for i, g in enumerate(grouped) if i % 2 == 1]
    if not train_g or not test_g:
        return float("nan")
    w = taste.train(flat(train_g), dim)
    return taste.accuracy(flat(test_g), w)


def shuffled_baseline(pairs, dim, seed=11):
    """Seeded per-pair label shuffle (a global flip would be symmetric, not null)."""
    rnd = random.Random(seed)
    shuffled = [(b, a) if rnd.random() < 0.5 else (a, b) for a, b in pairs]
    w = taste.train(shuffled, dim)
    return taste.accuracy(shuffled, w)


def explicit_feel_pairs():
    """best>others / others>worst pairs from rated rounds whose candidates carry raw
    notes — featurized with FEEL features (the frozen melodic features don't apply)."""
    rounds = core._load_ratings_file().get("candidate_rounds", [])
    pairs = []
    for rnd_ in rounds:
        cands = rnd_.get("candidates", [])
        if len(cands) < 2 or not all(isinstance(c, dict) and c.get("notes") for c in cands):
            continue
        sm = _slot_mean(str(rnd_.get("role") or "lead"))
        feats = [feel.feel_features(c["notes"], sm) for c in cands]
        b, w = rnd_.get("best"), rnd_.get("worst")
        if isinstance(b, int) and 0 <= b < len(cands):
            pairs += [(feats[b], feats[i]) for i in range(len(cands)) if i != b]
        if isinstance(w, int) and 0 <= w < len(cands) and w != b:
            pairs += [(feats[i], feats[w]) for i in range(len(cands)) if i != w and i != b]
    return pairs


def report(label, grouped, dim):
    pairs = flat(grouped)
    if len(pairs) < 10:
        print(f"{label}: only {len(pairs)} pairs — skipped")
        return None, None
    w = taste.train(pairs, dim)
    fit = taste.accuracy(pairs, w)
    half = split_half(grouped, dim)
    base = shuffled_baseline(pairs, dim)
    print(f"{label}: n={len(pairs)}  fit={fit:.2f}  split-half={half:.2f}  shuffled-baseline={base:.2f}")
    return w, (fit, half)


def main():
    print("=== feel-signal validation (pre-registered: fit>0.65 AND split-half>=0.60) ===\n")
    lead = collect(roles={"lead"})
    w_lead, gate_vals = report("LEAD (primary)", lead, len(feel.FEEL_FEATURE_NAMES))
    report("ALL ROLES (secondary)", collect(), len(feel.FEEL_FEATURE_NAMES))
    report("LEAD +density (ablation)", collect(roles={"lead"}, with_density=True),
           len(feel.FEEL_FEATURE_NAMES) + 1)
    red = [g for g in lead if g["reduction"]]
    non = [g for g in lead if not g["reduction"]]
    report("LEAD reduction-winners only", red, len(feel.FEEL_FEATURE_NAMES))
    report("LEAD non-reduction winners", non, len(feel.FEEL_FEATURE_NAMES))

    if w_lead:
        print("\nlead feel weights:")
        for name, wt in sorted(zip(feel.FEEL_FEATURE_NAMES, w_lead), key=lambda t: -abs(t[1])):
            print(f"  {name:18s} {wt:8.2f}")

    ep = explicit_feel_pairs()
    if ep and w_lead:
        cross = taste.accuracy(ep, w_lead)
        print(f"\nbooth/explicit cross-check: {len(ep)} pairs, agreement = {cross:.2f} "
              f"(gate {GATE_CROSS:.2f})")
    else:
        print("\nbooth/explicit cross-check: pending — no rated rounds with raw notes yet")

    if gate_vals:
        fit, half = gate_vals
        if fit > GATE_FIT and half >= GATE_SPLIT:
            print(f"\n-> GATE PASSED (fit {fit:.2f} > {GATE_FIT}, split-half {half:.2f} >= {GATE_SPLIT}).")
            print("   Comping IS a feel judgment. Still wires NOTHING — booth cross-check +")
            print("   Ricardo's approval gate any use in humanize/selection.")
        else:
            print(f"\n-> GATE FAILED (need fit > {GATE_FIT} AND split-half >= {GATE_SPLIT}).")
            print("   Feel features join the melodic ones as an archive finding. The comping")
            print("   manifest stays untrained-on either way.")


if __name__ == "__main__":
    main()
