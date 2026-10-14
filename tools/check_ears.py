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
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))


def main():
    import ears
    import moment
    snap = moment.get_moment()
    print(json.dumps(snap, indent=2))
    print()
    print(ears.status_line(snap))
    if "--save" in sys.argv:
        idx = sys.argv.index("--save")
        if idx + 1 >= len(sys.argv):
            sys.exit("--save needs a path, e.g. --save tools/ideas/moment_snapshot.json")
        path = sys.argv[idx + 1]
        import core
        core._save_json_atomic(path, snap, indent=1)
        print("saved:", path)


if __name__ == "__main__":
    main()
