"""Safe corpus rebuild: scan → materialize → analyze, with fail-closed gates.

Today's pipeline order (order = STAGES below): scan_catalog,
materialize, harmony, structure, style_profile (renders harmony+structure),
build_index, motifs (needs index+profile), directions (reads the profile).
Phrase curves need the automation harvest (a separate dig, not the .als
pipeline) and nomml is a runtime library — neither runs here.

Gates, in order, all before any store write lands:
  1. every configured root that fed the previous manifest is still mounted;
  2. the fresh scan's project count is within 5% of the previous manifest
     (a missing drive shows up as fewer projects, never silently dropped);
  3. after the stages, project counts and raw phrase-window counts are
     within 5% unless --allow-project-shrink / --allow-phrase-shrink —
     on failure the previous single-file artifacts are restored from
     backup and nothing half-written stays.

Dry-run by default: scans to a temp manifest and reports before/after
counts. --commit applies. Never run against the live store in tests —
pass --store/--manifest and an injectable stage runner.

Usage:
    python3 tools/rebuild_corpus.py                      # dry run, default roots
    python3 tools/rebuild_corpus.py --commit             # real rebuild
    python3 tools/rebuild_corpus.py --commit --allow-phrase-shrink --limit 50
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ingest"))
from scan_catalog import DEFAULT_ROOTS  # noqa: E402  (roots only; no scan here)

STORE = os.path.join(ROOT, "store")
MANIFEST = os.path.join(STORE, "catalog_manifest.json")

# Stage order = pipeline dependency order. harmony needs the venv (music21);
# run this script with the venv python for a real rebuild.
STAGES = ("scan", "materialize", "harmony", "structure", "style_profile",
          "build_index", "motifs", "directions")
STAGE_SCRIPTS = {
    "scan": "ingest/scan_catalog.py",
    "materialize": "ingest/materialize.py",
    "harmony": "analyze/harmony.py",
    "structure": "analyze/structure.py",
    "style_profile": "analyze/style_profile.py",
    "build_index": "analyze/build_index.py",
    "motifs": "analyze/motifs.py",
    "directions": "tools/build_directions.py",
}

# Single-file artifacts the stages rewrite (projects/*.json is append-only:
# materialize adds/overwrites but never deletes, so it cannot shrink).
BACKUP_FILES = ("catalog_manifest.json", "phrase_index.json",
                "phrase_id_map.json", "style_profile.json", "STYLE.md",
                "harmony.json", "structure.json", "motifs.json",
                "directions.json")

SHRINK_TOLERANCE = 0.05  # refuse a >5% drop without the matching --allow-*-shrink


def _norm(path: str) -> str:
    return os.path.normpath(os.path.abspath(path))


def check_roots_mounted(roots: list[str], prev_paths: list[str]) -> list[str]:
    """Configured roots that fed the previous manifest but are not mounted
    now. A root with no previous projects needs no check (fresh drives and
    first builds pass through)."""
    prev = [_norm(p) for p in prev_paths]
    missing = []
    for root in roots:
        norm = _norm(root)
        if not any(p == norm or p.startswith(norm + os.sep) for p in prev):
            continue
        if not os.path.isdir(root):
            missing.append(root)
    return missing


def manifest_counts(rows: list[dict]) -> dict:
    """Project count for shrink gating: rows that parsed without error."""
    return {"projects": sum(1 for r in rows if not r.get("error"))}


def index_counts(index: dict) -> dict:
    """Phrase-gate counts: n_windows (raw pre-dedupe windows) when the meta
    stamps it, else the phrase count — the same unit, so the first rebuild
    after the stamp compares new windows against old phrases."""
    phrases = index.get("phrases", [])
    n_phrases = int(index.get("count", len(phrases)))
    meta = index.get("meta") if isinstance(index, dict) else None
    corpus = meta.get("corpus") if isinstance(meta, dict) else None
    # build_index.build_meta stamps it under meta.corpus.n_windows
    n_windows = corpus.get("n_windows") if isinstance(corpus, dict) else None
    if not isinstance(n_windows, int):
        n_windows = n_phrases  # old meta: no stamp yet
    return {"phrases": n_phrases, "n_windows": n_windows}


def read_store_counts(store: str) -> dict:
    """Before-counts from the current store (missing files count 0: a first
    build can never trip the shrink gate)."""
    out = {"projects": 0, "phrases": 0, "n_windows": 0}
    try:
        with open(os.path.join(store, "catalog_manifest.json")) as fh:
            out.update(manifest_counts(json.load(fh)))
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(store, "phrase_index.json")) as fh:
            out.update(index_counts(json.load(fh)))
    except (OSError, ValueError):
        pass
    return out


def check_shrink(before: dict, after: dict, *,
                 allow_project_shrink: bool = False,
                 allow_phrase_shrink: bool = False) -> str | None:
    """None when counts hold; a refusal message when projects or raw windows
    drop more than SHRINK_TOLERANCE without the matching --allow-*-shrink
    flag. The phrase side gates on n_windows (raw pre-dedupe windows), not
    the deduped phrase count."""
    for key, allowed, flag in (
            ("projects", allow_project_shrink, "--allow-project-shrink"),
            ("n_windows", allow_phrase_shrink, "--allow-phrase-shrink")):
        if allowed:
            continue
        b, a = before.get(key, 0), after.get(key, 0)
        if b and a < b * (1 - SHRINK_TOLERANCE):
            label = "projects" if key == "projects" else "phrase windows"
            return (f"{label} would shrink {b} → {a} "
                    f"(>{SHRINK_TOLERANCE:.0%} drop) — refusing; "
                    f"re-mount the missing drive or pass {flag}")
    return None


def _backup(store: str) -> dict:
    """Copy existing artifacts aside; returns {name: bak_path}.

    Single-file artifacts are copied next to the originals; the materialized
    projects/ directory (append-only, but a failed run can half-write it)
    is copied as a whole tree."""
    saved = {}
    for name in BACKUP_FILES:
        src = os.path.join(store, name)
        if os.path.exists(src):
            bak = src + ".rebuild-bak"
            shutil.copy2(src, bak)
            saved[name] = bak
    projects = os.path.join(store, "projects")
    if os.path.isdir(projects):
        bak = projects + ".rebuild-bak"
        if os.path.isdir(bak):
            shutil.rmtree(bak)
        shutil.copytree(projects, bak)
        saved["projects/"] = bak
    return saved


def _restore(store: str, saved: dict) -> None:
    """Put every backup back; a projects/ dir the failed run created from
    nothing is removed again, so the store comes back byte-identical."""
    for name, bak in saved.items():
        if name == "projects/":
            target = os.path.join(store, "projects")
            if os.path.isdir(target):
                shutil.rmtree(target)
            shutil.copytree(bak, target)
        else:
            shutil.copy2(bak, os.path.join(store, name))
    for name in BACKUP_FILES:
        if name in saved:
            continue
        target = os.path.join(store, name)
        if os.path.isfile(target):
            os.unlink(target)
    if "projects/" not in saved:
        target = os.path.join(store, "projects")
        if os.path.isdir(target):
            shutil.rmtree(target)


def _drop_backups(saved: dict) -> None:
    for bak in saved.values():
        try:
            if os.path.isdir(bak):
                shutil.rmtree(bak)
            else:
                os.unlink(bak)
        except OSError:
            pass


def _stamp_manifest_time(store: str, manifest_time: str) -> None:
    """Stamp the catalog scan this build saw onto the corpus metas."""
    for name in ("phrase_index.json", "style_profile.json"):
        path = os.path.join(store, name)
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        meta = data.get("meta")
        if isinstance(meta, dict):
            corpus = meta.setdefault("corpus", {})
            if isinstance(corpus, dict):
                corpus["manifest_time"] = manifest_time
                with open(path, "w") as fh:
                    json.dump(data, fh)
                    fh.write("\n")


def default_run_stage(name: str, script: str, args: list[str]) -> int:
    """Run one stage as a subprocess (harmony needs the venv for music21)."""
    proc = subprocess.run([sys.executable, os.path.join(ROOT, script)] + args)
    return proc.returncode


def _stage_args(name: str, *, store: str, manifest: str, limit: int) -> list[str]:
    if name == "scan":
        return []  # caller appends --roots/--out (scan always goes to temp)
    if name == "materialize":
        return ["--manifest", manifest] + (["--limit", str(limit)] if limit else [])
    if name in ("harmony", "structure", "style_profile", "build_index"):
        return ["--limit", str(limit)] if limit else []
    return []


def preview_id_map(store: str, candidate_index: str) -> dict:
    """Derive the stored-id migration map for a candidate phrase index without
    changing the store or its cumulative map."""
    sys.path.insert(0, os.path.join(ROOT, "analyze"))
    from corpus_keys import load_id_map, write_id_map

    with open(os.path.join(store, "phrase_index.json")) as fh:
        old = json.load(fh).get("phrases", [])
    with open(candidate_index) as fh:
        new = json.load(fh).get("phrases", [])
    current_path = os.path.join(store, "phrase_id_map.json")
    try:
        with open(current_path) as fh:
            cumulative = json.load(fh)
    except (OSError, ValueError):
        cumulative = {"map": {}, "ambiguous": {}, "gone": {}}
    with tempfile.TemporaryDirectory(prefix="corpus-id-preview.") as tmp:
        temp_map = os.path.join(tmp, "phrase_id_map.json")
        with open(temp_map, "w") as fh:
            json.dump(cumulative, fh)
        return write_id_map(old, new, temp_map)


def main(argv=None, *, run_stage=None, store: str | None = None) -> int:
    ap = argparse.ArgumentParser(description="Safe corpus rebuild (dry-run by default).")
    ap.add_argument("--roots", nargs="*", default=list(DEFAULT_ROOTS))
    ap.add_argument("--store", default=STORE)
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--candidate-index", default=None,
                    help="phrase index built separately for an accurate migration preview")
    ap.add_argument("--commit", action="store_true", help="apply (default: dry run)")
    ap.add_argument("--allow-project-shrink", action="store_true")
    ap.add_argument("--allow-phrase-shrink", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="parse at most N (sampling)")
    args = ap.parse_args(argv)
    missing_roots = [r for r in args.roots if not os.path.isdir(r)]
    if missing_roots:
        # find_als would silently skip these: a typo or an unmounted drive
        # must be loud, never a partial corpus.
        print("REFUSED: configured roots not found:", file=sys.stderr)
        for root in missing_roots:
            print(f"  {root}", file=sys.stderr)
        print("Check the path or mount the drive — scanning on without it "
              "would silently drop its projects.", file=sys.stderr)
        return 2
    injected_runner = run_stage is not None
    run_stage = run_stage or default_run_stage
    store = args.store if store is None else store
    if args.commit and _norm(store) != _norm(STORE) and not injected_runner:
        # The stage scripts hardcode the default store (no --store plumbing),
        # so a real --commit elsewhere would rebuild the live store while the
        # gates and backups apply to --store. Refuse; injected runners (tests,
        # ops harnesses) route the stages themselves and stay allowed.
        print("REFUSED: --commit with a non-default --store would run the "
              "stage scripts against the default store — commit to the "
              "default store or drive the stages with an injected runner.",
              file=sys.stderr)
        return 2
    manifest = args.manifest or os.path.join(store, "catalog_manifest.json")

    before = read_store_counts(store)
    prev_paths: list[str] = []
    if os.path.exists(manifest):
        try:
            with open(manifest) as fh:
                prev_paths = [r.get("path", "") for r in json.load(fh)]
        except ValueError:
            pass

    # Stage 1 always runs (scan to temp, read-only over the drives): it feeds
    # every gate below and the dry-run report.
    tmp_fd, tmp_scan = tempfile.mkstemp(prefix="catalog_manifest.", suffix=".json")
    os.close(tmp_fd)
    try:
        rc = run_stage("scan", STAGE_SCRIPTS["scan"],
                       ["--roots", *args.roots, "--out", tmp_scan]
                       + (["--limit", str(args.limit)] if args.limit else []))
        if rc != 0:
            print(f"scan stage failed (rc={rc}) — nothing written", file=sys.stderr)
            return rc
        with open(tmp_scan) as fh:
            new_rows = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"scan output unreadable ({exc}) — nothing written", file=sys.stderr)
        return 2
    finally:
        try:
            os.unlink(tmp_scan)
        except OSError:
            pass

    # Gate 1: every previously-seen root still mounted — BEFORE any write.
    missing = check_roots_mounted(args.roots, prev_paths)
    if missing:
        print("REFUSED: previously-scanned roots not mounted:", file=sys.stderr)
        for root in missing:
            print(f"  {root}", file=sys.stderr)
        print("Re-mount and retry — a rebuild now would silently drop "
              "58% of the corpus.", file=sys.stderr)
        return 2

    # Gate 2: project-count shrink on the fresh scan — BEFORE any write.
    after_scan = dict(before)
    after_scan.update(manifest_counts(new_rows))
    refusal = check_shrink(
        before, after_scan,
        allow_project_shrink=args.allow_project_shrink,
        allow_phrase_shrink=args.allow_phrase_shrink)
    if refusal:
        print(f"REFUSED: {refusal}", file=sys.stderr)
        return 2

    print(f"scan: {after_scan['projects']} projects "
          f"(was {before['projects']}), {before['phrases']} phrases indexed")
    if not args.commit:
        try:
            sys.path.insert(0, os.path.join(ROOT, "analyze"))
            from corpus_keys import load_id_map, migrate_stored_ids
            if args.candidate_index:
                id_map = preview_id_map(store, args.candidate_index)
            else:
                id_map = None
                print("stored-id migration preview unavailable (no candidate index)")
            if id_map is not None:
                preview = migrate_stored_ids(
                    os.path.join(store, "ratings.json"),
                    os.path.join(store, "session_ledger.json"),
                    id_map["map"],
                    dry_run=True,
                    develop_session_path=os.path.join(ROOT, "tools", "ideas",
                                                      "develop_session.json"))
                print("stored-id migration preview: " + ", ".join(
                    f"{key}={value}" for key, value in preview.items()
                    if key.endswith("_rewritten")))
        except (OSError, ValueError) as exc:
            print(f"stored-id migration preview unavailable ({exc})")
        print("dry run — no store files written "
              "(pass --commit to run the full pipeline)")
        return 0

    saved = _backup(store)
    try:
        with open(manifest, "w") as fh:
            json.dump(new_rows, fh, indent=2)
        for name in STAGES[1:]:
            rc = run_stage(name, STAGE_SCRIPTS[name],
                           _stage_args(name, store=store, manifest=manifest,
                                       limit=args.limit))
            if rc != 0:
                print(f"{name} stage failed (rc={rc}) — restoring", file=sys.stderr)
                _restore(store, saved)
                return rc
        try:
            manifest_time = datetime.fromtimestamp(
                os.stat(manifest).st_mtime, tz=timezone.utc).isoformat()
        except OSError:
            manifest_time = None
        if manifest_time:
            _stamp_manifest_time(store, manifest_time)
        after = read_store_counts(store)
        refusal = check_shrink(
            before, after,
            allow_project_shrink=args.allow_project_shrink,
            allow_phrase_shrink=args.allow_phrase_shrink)
        if refusal:
            print(f"REFUSED: {refusal} — restoring previous artifacts",
                  file=sys.stderr)
            _restore(store, saved)
            return 2
        sys.path.insert(0, os.path.join(ROOT, "analyze"))
        from corpus_keys import load_id_map, migrate_stored_ids
        migration = migrate_stored_ids(
            os.path.join(store, "ratings.json"),
            os.path.join(store, "session_ledger.json"),
            load_id_map(os.path.join(store, "phrase_id_map.json")),
            dry_run=False,
            develop_session_path=os.path.join(ROOT, "tools", "ideas",
                                              "develop_session.json"))
        print("stored ids migrated: " + ", ".join(
            f"{key}={value}" for key, value in migration.items()
            if key.endswith("_rewritten")))
        print(f"rebuilt: {after['projects']} projects, {after['phrases']} phrases "
              f"(was {before['projects']}/{before['phrases']})")
        return 0
    except BaseException:
        # A raising stage (or a signal) must not leave a half-written store:
        # put the backups back, then let the original error propagate.
        _restore(store, saved)
        raise
    finally:
        _drop_backups(saved)


if __name__ == "__main__":
    raise SystemExit(main())
