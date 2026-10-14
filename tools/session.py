"""One-command ear sessions: stage -> listen -> vote -> ingest, no ferrying.

  .venv/bin/python tools/session.py audition     # the 3-tier solo listen
  .venv/bin/python tools/session.py booth        # blind A/B pairs

What it does, in order:
  1. (booth, --rebuild only) rebuild the round via rank_pairwise.build();
     audition rounds are built by their own round scripts (build_audition_r2b).
  2. Stage the round's clips on the "SIDEKICK BOOTH" track in Live via Producer
     Pal — append-only, found-or-created by name, AUD NN / PNN A|B clip names.
     Normal runs stop if Live/PP staging fails; --manual prints the
     store/exports paths and continues with an explicit manual handoff.
  3. Serve the vote page on localhost with AUTO-SAVE: every click POSTs the
     votes to disk (tools/ideas/..._votes_....json). Losing a session's votes
     is now structurally impossible; the export button stays as fallback.
  4. When all votes are in (or you Ctrl-C early): dry-run the matching
     ingester, show the plan, ask once, run it for real, print the report.

Run it from the repo venv; Ctrl-C at any point exits without ingesting.
"""
import argparse
import json
import os
import subprocess
import sys
import webbrowser
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))

IDEAS = os.path.join(ROOT, "tools/ideas")
PORT = 3360

PAGES = {"audition": "audition_vote.html", "booth": "booth_vote.html"}
MANIFESTS = {"audition": "rank_audition_manifest.json", "booth": "pairwise_manifest.json"}


def _require_complete_staging(result, expected_names):
    """Fail before voting unless every requested clip was read back successfully."""
    placed = result.get("placed") if isinstance(result, dict) else None
    if not isinstance(placed, list):
        raise RuntimeError("Live staging returned no placement list")
    actual_names = [p.get("name") for p in placed]
    if actual_names != list(expected_names):
        raise RuntimeError(
            "Live staging placed %r, expected %r" % (actual_names, list(expected_names))
        )
    if result.get("verified_ok") is not True or any(p.get("ok") is not True for p in placed):
        bad = [p.get("name") for p in placed if p.get("ok") is not True]
        raise RuntimeError("Live staging read-back failed for %s" % bad)
    return result


def _load_manifest(kind):
    return json.load(open(os.path.join(IDEAS, MANIFESTS[kind])))


def _phrase_index(index_path=None, id_map=None):
    idx = json.load(open(index_path or os.path.join(ROOT, "store/phrase_index.json")))
    phrases = idx if isinstance(idx, list) else idx.get("phrases", [])
    from corpus_keys import phrase_lookup
    return phrase_lookup(phrases, id_map=id_map)


def _expected_clip_names(kind, manifest):
    if kind == "audition":
        return ["AUD %02d" % item["blind_id"] for item in manifest]
    return ["P%02d %s" % (item["pair_id"], side)
            for item in manifest for side in ("A", "B")]


def _load_stage_progress(path, round_tag, expected_names):
    if not os.path.exists(path):
        return set()
    data = json.load(open(path))
    if not isinstance(data, dict) or data.get("round") != round_tag:
        raise RuntimeError("stage progress belongs to another round")
    completed = data.get("completed_names")
    if not isinstance(completed, list) or any(not isinstance(name, str) for name in completed):
        raise RuntimeError("stage progress has an invalid completed_names list")
    unknown = set(completed) - set(expected_names)
    if unknown:
        raise RuntimeError("stage progress contains unknown clips: %s" % sorted(unknown))
    return set(completed)


def _save_stage_progress(path, round_tag, completed_names, expected_names):
    completed = _load_stage_progress(path, round_tag, expected_names)
    completed.update(completed_names)
    unknown = completed - set(expected_names)
    if unknown:
        raise RuntimeError("refusing unknown staged clips: %s" % sorted(unknown))
    import core
    core._save_json_atomic(
        path,
        {"round": round_tag, "completed_names": sorted(completed)},
        indent=2,
    )
    return completed


def _votes_target(kind, manifest):
    """(votes_path, round_tag) — where auto-save writes and what ingest is told."""
    if kind == "audition":
        from ingest_audition_votes import validate_audition_manifest_round
        try:
            rtag = validate_audition_manifest_round(manifest)
        except ValueError as exc:
            raise RuntimeError("invalid audition round artifact: %s" % exc) from exc
        if not rtag:
            raise RuntimeError(
                "roundless audition manifests require direct ingestion with an explicit "
                "--legacy-label; the automatic session refuses to guess gold semantics"
            )
        return os.path.join(IDEAS, "audition_votes_%s.json" % rtag), rtag
    preds_path = os.path.join(IDEAS, "pairwise_predictions.json")
    from ingest_booth_votes import validate_booth_round_artifacts
    predictions = json.load(open(preds_path)) if os.path.exists(preds_path) else None
    try:
        rid = validate_booth_round_artifacts(manifest, predictions)
    except ValueError as exc:
        raise RuntimeError("invalid Booth round artifacts: %s" % exc) from exc
    if not rid:
        return os.path.join(IDEAS, "booth_votes_booth_legacy.json"), ""
    return os.path.join(IDEAS, "booth_votes_%s.json" % rid), rid


def _stage_clips(kind, manifest, by_id, predictions=None):
    clips = []
    missing = []
    if kind != "audition":
        # F6: the frozen round staged by ID from the MUTABLE phrase index — refuse
        # loudly if a phrase changed since the round was frozen (absent hash on old
        # rounds is grandfathered inside the helper).
        from ingest_booth_votes import verify_booth_note_bindings
        verify_booth_note_bindings(manifest, by_id, predictions)
    sidecar_by_pair = {}
    if predictions:
        for pair in predictions.get("pairs") or []:
            sidecar_by_pair[pair.get("pair_id")] = pair
    if kind == "audition":
        for m in manifest:
            p = by_id.get(m["phrase_id"])
            if p is None:
                missing.append(m["phrase_id"])
                continue
            clips.append({"name": "AUD %02d" % m["blind_id"], "notes": p["notes"],
                          "tempo": p.get("tempo"), "color": "#FFB000"})
    else:
        for m in manifest:
            if m.get("selection_mode") == "develop_ab":
                sp = sidecar_by_pair.get(m["pair_id"])
                if sp is None:
                    missing.append("sidecar pair %s" % m["pair_id"])
                    continue
                for side, col in (("A", "#FFB000"), ("B", "#00C2B8")):
                    notes = sp.get("%s_notes" % side)
                    if notes is None:
                        missing.append("develop pair %s %s_notes" % (m["pair_id"], side))
                        continue
                    tempo = m.get("tempo")
                    if tempo is None:
                        pid = m.get("%s_phrase_id" % side)
                        phrase = by_id.get(pid)
                        tempo = phrase.get("tempo") if phrase else None
                    clips.append({"name": "P%02d %s" % (m["pair_id"], side),
                                  "notes": notes, "tempo": tempo, "color": col})
                continue
            for side, col in (("A", "#FFB000"), ("B", "#00C2B8")):
                p = by_id.get(m["%s_phrase_id" % side])
                if p is None:
                    missing.append(m["%s_phrase_id" % side])
                    continue
                clips.append({"name": "P%02d %s" % (m["pair_id"], side),
                              "notes": p["notes"], "tempo": p.get("tempo"),
                              "color": col})
    if missing:
        raise RuntimeError("cannot stage phrases missing from the index: %s" % sorted(set(missing)))
    if not clips:
        raise RuntimeError("no clips available to stage")

    import asyncio
    import live_write
    result = asyncio.run(live_write.stage_named_clips(clips))
    return _require_complete_staging(result, [c["name"] for c in clips])


def _validate_vote_payload(kind, data, manifest, expected_round):
    if kind == "booth":
        from ingest_booth_votes import validate_booth_vote_payload
        return len(validate_booth_vote_payload(data, manifest, expected_round))
    from ingest_audition_votes import validate_audition_vote_payload
    return len(validate_audition_vote_payload(data, manifest, expected_round))


class _VoteHandler(SimpleHTTPRequestHandler):
    votes_path = None
    expected = 0
    expected_round = ""
    manifest = ()
    kind = ""
    state = None

    def do_POST(self):
        if not self.path.startswith("/save"):
            self.send_error(404)
            return
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data = json.loads(body)
        except Exception:
            self.send_error(400)
            return
        got_round = (data.get("round") or data.get("round_id") or "").strip()
        if self.expected_round and got_round != self.expected_round:
            self.send_error(409, "round mismatch: page %s, session %s"
                            % (got_round, self.expected_round))
            return
        try:
            n = _validate_vote_payload(self.kind, data, self.manifest, self.expected_round)
        except ValueError as exc:
            self.send_error(422, "invalid vote payload: %s" % exc)
            return
        import core
        core._save_json_atomic(self.votes_path, data, indent=1)
        self.send_response(204)
        self.end_headers()
        self.state["received"] = True
        self.state["n"] = n
        if n >= self.expected:
            self.state["done"] = True

    def log_message(self, *args):   # keep the terminal for the session, not GETs
        pass


def _serve_until_voted(kind, votes_path, expected, expected_round="", manifest=()):
    handler = type("H", (_VoteHandler,), {
        "votes_path": votes_path, "expected": expected,
        "expected_round": expected_round, "manifest": tuple(manifest), "kind": kind,
        "state": {"done": False, "n": 0, "received": False},
    })
    # py3.7+: directory= serves tools/ideas without chdir
    srv = HTTPServer(("127.0.0.1", PORT), partial(handler, directory=IDEAS))
    srv.timeout = 1.0
    url = "http://127.0.0.1:%d/%s" % (PORT, PAGES[kind])
    print("vote page:  %s   (auto-saves every click -> %s)"
          % (url, os.path.relpath(votes_path, ROOT)))
    print("listen in Live, vote on the page; Ctrl-C here to stop early.")
    webbrowser.open(url)
    last = -1
    try:
        while not handler.state["done"]:
            srv.handle_request()
            if handler.state["n"] != last:
                last = handler.state["n"]
                print("  %d / %d voted" % (last, expected), end="\r")
    except KeyboardInterrupt:
        print("\nstopped early — votes so far are on disk.")
    finally:
        srv.server_close()
    if handler.state["done"]:
        print("\nall %d votes in." % expected)
    return handler.state


def _run(cmd):
    print("$ " + " ".join(os.path.relpath(c, ROOT) if os.path.isabs(c) else c
                          for c in cmd))
    return subprocess.run(cmd, cwd=ROOT).returncode


def _ingest(kind, votes_path, rtag):
    py = sys.executable
    if kind == "audition":
        base = [py, os.path.join(ROOT, "tools/ingest_audition_votes.py"),
                "--votes", votes_path, "--round", rtag]
        score = [py, os.path.join(ROOT, "tools/rank_audition.py"), "--score"]
    else:
        base = [py, os.path.join(ROOT, "tools/ingest_booth_votes.py"),
                "--votes", votes_path]
        score = None
    if _run(base + ["--dry-run"]) != 0:
        print("dry-run failed — not ingesting.")
        return
    try:
        go = input("\ningest for real? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        go = ""
    if go != "y":
        print("skipped. Re-run ingest later with the command above (minus --dry-run).")
        return
    if _run(base) == 0 and score:
        _run(score)


def _manual_fallback_rows(kind, manifest, completed_names=()):
    completed = set(completed_names)
    rows = []
    if kind == "audition":
        for item in manifest:
            name = "AUD %02d" % item["blind_id"]
            if name not in completed:
                rows.append((name, item.get("midi_file") or ""))
    else:
        for item in manifest:
            for side in ("A", "B"):
                name = "P%02d %s" % (item["pair_id"], side)
                if name not in completed:
                    rows.append((name, item.get("%s_midi" % side) or ""))
    missing = [path or "<unset>" for _name, path in rows if not path or not os.path.isfile(path)]
    if missing:
        raise RuntimeError("manual fallback files are missing: %s" % missing)
    return rows


def _print_manual_fallback(kind, manifest, completed_names=()):
    rows = _manual_fallback_rows(kind, manifest, completed_names)
    print("manual fallback (store/exports/):")
    if completed_names:
        print("  already verified in Live: %s" % ", ".join(sorted(completed_names)))
    for name, path in rows:
        print("  %-7s -> %s" % (name, os.path.basename(path)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("kind", choices=("audition", "booth"))
    ap.add_argument("--rebuild", action="store_true",
                    help="booth: rebuild the round first (archives a voted manifest)")
    ap.add_argument("--no-live", action="store_true", help="skip Live staging and use exports")
    ap.add_argument("--manual", action="store_true",
                    help="allow manual-export fallback if Live staging fails")
    ap.add_argument("--reset-stage-progress", action="store_true",
                    help="after manually clearing the staged clips, forget saved progress")
    ap.add_argument("--no-ingest", action="store_true", help="stop after voting")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    globals()["PORT"] = args.port

    # session memory dies with the session (spec §4): a new voting session is
    # the concrete boundary — start it with a clean slate, not yesterday's
    # demotions bleeding into today's round.
    import ears
    ears.reset_session()
    print("fresh ears for this session")
    import develop_session
    print(develop_session.reset_state())

    if args.rebuild:
        if args.kind == "booth":
            import build_booth_page
            import rank_pairwise
            rank_pairwise.build()
            build_booth_page.main()
        else:
            print("audition rounds are built by their round script "
                  "(e.g. build_audition_r2b.py) — rebuilding the page only.")
            import build_audition_page
            build_audition_page.main()

    manifest = _load_manifest(args.kind)
    voted = [m for m in manifest
             if (m.get("rating") or m.get("winner") or "").strip()]
    if voted:
        sys.exit("manifest already carries %d votes — this round looks ingested. "
                 "Rebuild a fresh round first." % len(voted))
    by_id = _phrase_index()
    predictions = None
    if args.kind == "booth":
        preds_path = os.path.join(IDEAS, "pairwise_predictions.json")
        if os.path.exists(preds_path):
            predictions = json.load(open(preds_path))
    votes_path, rtag = _votes_target(args.kind, manifest)
    expected_names = _expected_clip_names(args.kind, manifest)
    stage_progress_path = votes_path + ".stage.json"
    if args.reset_stage_progress and os.path.exists(stage_progress_path):
        os.unlink(stage_progress_path)
        print("cleared saved stage progress; Live cleanup was explicitly acknowledged")
    try:
        completed_names = _load_stage_progress(
            stage_progress_path, rtag, expected_names,
        )
    except RuntimeError as exc:
        sys.exit("invalid stage progress: %s" % exc)

    if completed_names and not (args.manual or args.no_live):
        sys.exit(
            "saved stage progress exists for %s, but prior Live clips are not trusted "
            "without rereading them. Use --manual/--no-live to reconcile every export, "
            "or clear the clips in Live and re-run with --reset-stage-progress."
            % ", ".join(sorted(completed_names))
        )

    if args.no_live or (args.manual and completed_names):
        if completed_names:
            print("saved progress is informational only; manually reconcile every clip.")
        try:
            _print_manual_fallback(args.kind, manifest)
        except RuntimeError as exc:
            sys.exit(str(exc))
    else:
        import live_write
        try:
            res = _stage_clips(args.kind, manifest, by_id, predictions)
            newly_completed = [item["name"] for item in res.get("placed", [])]
            completed_names = _save_stage_progress(
                stage_progress_path, rtag, newly_completed, expected_names,
            )
            # _stage_clips always stages the full manifest (saved progress is a
            # refusal gate, never a skip list), so success means placed is complete
            rows = "%d-%d" % (
                res["start_slot"], res["start_slot"] + len(res["placed"]) - 1,
            )
            print("staged %d clips on '%s' (rows %s%s)"
                  % (len(res["placed"]), "SIDEKICK BOOTH", rows,
                     ", track created" if res["track_created"] else ""))
        except live_write.LiveWriteVerificationError as e:
            newly_completed = [item.get("name") for item in e.completed if item.get("name")]
            completed_names = _save_stage_progress(
                stage_progress_path, rtag, newly_completed, expected_names,
            )
            if not args.manual:
                sys.exit("Live staging failed (%s). Re-run with --manual to use exports." % e)
            print("Live staging failed (%s); continuing only because --manual was explicit." % e)
            try:
                _print_manual_fallback(args.kind, manifest, completed_names)
            except RuntimeError as exc:
                sys.exit(str(exc))
        except Exception as e:
            sys.exit("Live staging failed before recoverable verification (%s)." % e)

    if os.path.exists(votes_path):
        print("note: %s already exists (an earlier attempt?) — reopening the page "
              "restores matching browser votes automatically; your first click "
              "overwrites the file." % os.path.relpath(votes_path, ROOT))
    state = _serve_until_voted(args.kind, votes_path, len(manifest), rtag, manifest)
    if args.no_ingest:
        return
    if not state["received"]:
        print("no votes reached THIS session — not ingesting.")
        if os.path.exists(votes_path):
            print("  (a votes file exists from earlier; ingest it deliberately with the "
                  "ingester's --votes flag if it's real.)")
        return
    if state["n"] < len(manifest):
        print("partial round (%d/%d) — not ingesting. Votes are saved; re-run this "
              "command to finish (the page picks up where you left off)."
              % (state["n"], len(manifest)))
        return
    _ingest(args.kind, votes_path, rtag)


if __name__ == "__main__":
    main()
