"""Owner-only CLI for declaring artistic acceptance or release readiness."""
from __future__ import annotations

import argparse
import os
import sys

# Direct execution sets sys.path[0] to tools/, so add the repository root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


VALID_STATES = {"composition_accepted", "release_ready"}


def _write_declaration(project_key: str, state: str, note: str | None) -> dict:
    from mcp_server import rituals
    ledger = rituals._load_ledger()
    declaration = {"state": state, "declared_at": rituals._now(),
                   "declared_by": "owner_cli"}
    if note is not None:
        declaration["note"] = note
    ledger.setdefault("finishing", {}).setdefault(project_key, {}).setdefault(
        "owner_states", []).append(declaration)
    rituals._save_ledger(ledger)
    return {"project": project_key, **declaration}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="catalog project id/name/path, or live for the saved open Set")
    parser.add_argument("state", choices=sorted(VALID_STATES))
    parser.add_argument("--note")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        raise SystemExit("owner declarations require an interactive terminal")
    from mcp_server import finisher

    try:
        project_key = finisher.resolve_project_key(args.project)
    except (ValueError, RuntimeError) as exc:
        # unknown/ambiguous project, unsaved or unverified open Set, Live unreachable
        print(f"No declaration written: {exc}", file=sys.stderr)
        return 1
    print(f"Will record {args.state} for {project_key!r} as declared_by=owner_cli.")
    if args.note:
        print(f"Note: {args.note}")
    try:
        answer = input("Type yes to confirm: ")
    except (EOFError, KeyboardInterrupt):
        print("No declaration written.")
        return 1
    if answer != "yes":
        print("No declaration written.")
        return 1
    print(_write_declaration(project_key, args.state, args.note))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
