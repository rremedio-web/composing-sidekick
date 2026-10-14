"""Owner-run seed: declare a piece, its confirmed Set versions and the known
facts with their provenance, from a JSON spec file.

    .venv/bin/python tools/declare_piece.py --spec my_piece.json            # dry run
    .venv/bin/python tools/declare_piece.py --spec my_piece.json --confirm  # writes
                                                  # store/pieces.json + store/piece_records.json

See ``tools/piece_spec.example.json`` for the format. Nothing here runs at
import. Without ``--confirm`` nothing is written. The run is idempotent: a
version already confirmed, a statement already filed (same piece, source,
quote) and a reaction already filed are skipped, so a second run reports
"nothing new". ``--store DIR`` targets another store directory (tests run it
on temp copies); ``--versions 2,4,5`` narrows the versions declared.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Direct execution sets sys.path[0] to tools/, so add the repository root.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

EXAMPLE_SPEC = os.path.join(ROOT, "tools", "piece_spec.example.json")


def load_spec(path: str) -> dict:
    """Read a piece spec: piece_id, name, notes_path, set_dir, set_pattern
    (``%d`` = version), default_versions, owner_statements, model_statements,
    reactions. Model statements name the version their Set key derives from."""
    with open(path, encoding="utf-8") as handle:
        spec = json.load(handle)
    missing = {"piece_id", "name", "set_dir", "set_pattern"} - set(spec)
    if missing:
        raise ValueError("piece spec %s is missing %s" % (path, ", ".join(sorted(missing))))
    return spec


def set_key(spec: dict, version: int) -> str:
    return os.path.join(os.path.expanduser(spec["set_dir"]), spec["set_pattern"] % version)


def plan(spec: dict, versions=None) -> dict:
    versions = versions if versions is not None else tuple(spec.get("default_versions", ()))
    model = [dict(row, set_key=set_key(spec, row["version"]))
             for row in spec.get("model_statements", ())]
    return {"piece_id": spec["piece_id"], "name": spec["name"],
            "notes_path": spec.get("notes_path"),
            "versions": [set_key(spec, v) for v in versions],
            "owner_statements": list(spec.get("owner_statements", ())),
            "model_statements": model,
            "reactions": list(spec.get("reactions", ()))}


def print_plan(the_plan: dict, store: str) -> None:
    print("Piece %s (%s), notes %s — store %s" % (the_plan["name"], the_plan["piece_id"],
                                                  the_plan["notes_path"], store))
    print("Confirmed versions (owner):")
    for key in the_plan["versions"]:
        print("  " + key)
    print("Owner statements (his words):")
    for row in the_plan["owner_statements"]:
        print("  [%s] %s" % (row["topic"], row["quote"]))
    print("Model-heard / measured (provenance-tagged, bound to their bounce):")
    for row in the_plan["model_statements"]:
        print("  [%s %s, %s, v%d] %s" % (row["source"], row["dated"], row["bound_to"]["bounce"],
                                         row["version"], row["quote"]))
    print("Reactions (his words, scoped):")
    for row in the_plan["reactions"]:
        print("  [%s%s] %s" % (row["scope"], ": " + row["scope_detail"]
                               if row.get("scope_detail") else "", row["words"]))


def apply(the_plan: dict, *, store: str) -> dict:
    """Write the plan into ``store`` (pieces.json, piece_records.json). Returns
    counts of what was new; everything already present is skipped."""
    from mcp_server import piece_context, piece_continuity

    pieces_path = os.path.join(store, "pieces.json")
    records_path = os.path.join(store, "piece_records.json")
    counts = {"versions": 0, "statements": 0, "reactions": 0}
    for key in the_plan["versions"]:
        out = piece_context.declare_piece_version(
            the_plan["piece_id"], key, confirm=True, name=the_plan["name"],
            notes_path=the_plan["notes_path"], via="cli", path=pieces_path)
        counts["versions"] += int(out["status"] == "confirmed")
    before = len(piece_continuity.load_records(path=records_path)["records"])
    for row in the_plan["owner_statements"]:
        piece_continuity.record_statement(
            the_plan["piece_id"], row["quote"], source="owner", topic=row["topic"],
            path=records_path, pieces_path=pieces_path)
    for row in the_plan["model_statements"]:
        piece_continuity.record_statement(
            the_plan["piece_id"], row["quote"], source=row["source"], topic=row["topic"],
            set_key=row["set_key"], bound_to=row["bound_to"], dated=row["dated"],
            evidence=row.get("evidence"), note=row.get("note"),
            path=records_path, pieces_path=pieces_path)
    after_statements = len(piece_continuity.load_records(path=records_path)["records"])
    counts["statements"] = after_statements - before
    existing = {(r["quote"], r["scope"]["kind"], r["scope"].get("detail"))
                for r in piece_continuity.reactions_for(piece_id=the_plan["piece_id"],
                                                        path=records_path)}
    for row in the_plan["reactions"]:
        marker = (row["words"], row["scope"], row.get("scope_detail"))
        if marker in existing:
            continue
        piece_continuity.record_reaction(
            row["words"], row["scope"], piece=the_plan["piece_id"],
            scope_detail=row.get("scope_detail"), path=records_path, pieces_path=pieces_path)
        counts["reactions"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--store", default=os.path.join(ROOT, "store"),
                        help="store directory (default: the repo's store/)")
    parser.add_argument("--confirm", action="store_true",
                        help="write; without it only the plan is printed")
    parser.add_argument("--spec", required=True,
                        help="piece spec JSON (format: tools/piece_spec.example.json)")
    parser.add_argument("--versions", default=None,
                        help="comma-separated version numbers to confirm "
                             "(default: the spec's default_versions)")
    args = parser.parse_args(argv)
    try:
        spec = load_spec(args.spec)
    except (OSError, ValueError) as exc:
        print("cannot read piece spec: %s" % exc, file=sys.stderr)
        return 2
    versions = None
    if args.versions is not None:
        try:
            versions = tuple(int(v) for v in args.versions.split(",") if v.strip())
        except ValueError:
            print("--versions must be comma-separated integers", file=sys.stderr)
            return 2
    the_plan = plan(spec, versions)
    print_plan(the_plan, args.store)
    if not args.confirm:
        print("\nDry run — nothing written. Re-run with --confirm to record the above as "
              "confirmed by the owner.")
        return 0
    counts = apply(the_plan, store=args.store)
    if not any(counts.values()):
        print("\nAlready recorded — nothing new written.")
    else:
        print("\nWritten: %d version(s) confirmed, %d statement(s), %d reaction(s) → %s"
              % (counts["versions"], counts["statements"], counts["reactions"], args.store))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
