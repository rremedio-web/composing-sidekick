# Composing Sidekick Full Review

Date: 2026-07-03
Scope: current `~/composing-sidekick` tree. Source review plus live verification. I did not edit source code.

## Verification

- `python3 ingest/selftest_ingest.py` passed.
- `.venv/bin/python -m compileall mcp_server analyze ingest tools` passed.
- `node tools/ideapack_panel.test.mjs` passed: `PASS - 30 checks passed, 0 failed`.
- `.venv/bin/python mcp_server/server.py --selftest` initially passed before the latest NOMML work landed, ending with `=== all selftests passed ===`.
- Re-running the same selftest on the current tree fails in the newly added NOMML block:
  - Failing line: `mcp_server/server.py:2543`
  - Assertion: `_rj40["off_grid_share"] > 0.9`
  - Actual probe result: `{"nomml": 11, "performed": true, "n_onsets": 16, "off_grid_share": 0.625}`
- `tools/report_nomml.py` runs and writes ignored sidecar `store/nomml.json`.
- Corpus purity spot-check: `store/not_his_projects.json` has 22 blocked projects, `store/phrase_index.json` has 25,600 phrases, and blocked phrase leaks were `0`.
- Current git state at review end:
  - modified: `.gitignore`, `mcp_server/server.py`
  - untracked: `ingest/nomml.py`, `tools/report_nomml.py`

## Findings

### P0 - Current main selftest is red after NOMML block

`mcp_server/server.py:2526-2555` adds the NOMML selftest. The test says deterministic human-scale jitter should produce more than 90% off-grid onsets, but the actual jitter pattern leaves several starts on the triplet/64th grids, producing `off_grid_share = 0.625`.

Impact: the repo no longer has a green advertised verification command. This also blocks any reliable merge or handoff because `mcp_server/server.py --selftest` is the main regression surface.

Likely fix: either change the synthetic jitter pattern so it really avoids every declared grid, or weaken/remove the `> 0.9` assertion if the real invariant is only median-off-grid/performed. Keep the test about `performed is True`; do not overfit to a false off-grid percentage.

### P1 - One-command booth staging and the booth page describe different Live layouts

`tools/session.py:72-80` turns each booth pair into two clips named `Pnn A` and `Pnn B`, then sends all clips to `live_write.stage_named_clips()`.

`mcp_server/live_write.py:369-420` appends those clips one per consecutive session slot on one named track, `SIDEKICK BOOTH`.

But `tools/build_booth_page.py:110` tells the user to play A and B in `BOOTH A` and `BOOTH B` in the same scene row, and `tools/build_booth_page.py:153-154` displays `scene row == pair_id`. `HANDOFF.md:153-155` also still documents separate `BOOTH A/B` tracks.

Impact: the listening UI can point the owner at the wrong clips. That can corrupt the taste model because votes no longer necessarily correspond to the pair in `pairwise_manifest.json`.

Fix direction: choose one layout contract and enforce it end to end. Either stage A/B on two named tracks at the same row, or keep one `SIDEKICK BOOTH` track and make the page display actual clip names/slots returned by staging.

### P1 - `tools/session.py` can ingest stale vote files

`tools/session.py:117-143` returns `os.path.exists(votes_path)` after the server exits. If an old `tools/ideas/*votes*.json` already exists for the same round, Ctrl-C before any new POST still returns true. Then `tools/session.py:220-225` proceeds to `_ingest()`.

Impact: a cancelled or partial listening session can dry-run or ingest stale votes from a prior run. This is dangerous because the whole point of the session wrapper is to make the ear loop harder to mess up.

Fix direction: track whether this server invocation received at least one POST, or validate a round key/hash inside the saved payload. Ideally write to a new temp votes path and atomically promote only after the first save for this invocation.

### P1 - NOMML report contradicts its own sanity expectation

`tools/report_nomml.py:9-11` says take lanes should skew performed. The actual current report says:

- all phrases: 9,566 / 25,600 performed = 37.4%
- lead: 63.4% performed
- arrangement: 39.8% performed
- session: 54.4% performed
- take_lane: 21.3% performed

Impact: the metric may still be useful as a gate, but it currently does not validate the assumed source behavior. If this later feeds `performed_only` filtering, it may exclude a lot of take-lane material despite the project narrative that take lanes contain alive alternate playing.

Fix direction: treat NOMML as unvalidated until the 10+10 spot-check is heard. Consider reporting by role and source jointly, not just source, before wiring any consumer.

### P2 - `stage_named_clips()` skips read-back verification

`write_answer_to_jam_track()` verifies clip note count after creation at `mcp_server/live_write.py:355-365`. `stage_named_clips()` creates each booth clip at `mcp_server/live_write.py:409-418` but records only slot/name.

Impact: booth staging can silently write wrong or empty clips while the voting page still proceeds. For taste-training data, that is almost as bad as writing to the wrong track.

Fix direction: mirror the jam-track verification: read each created slot back and include `sent_notes`, `verified_noteCount`, and an error flag in the returned placement list.

### P2 - Documentation is stale in multiple high-trust places

Examples:

- `README.md:21-26` says taste is 21 pairs / weight 0.63 / 38 MCP tools / 34 selftest blocks. Current `store/taste_model.json` has 41 pairs / weight about 1.0 by the ramp formula, server exposes 39 `@mcp.tool()` wrappers, and the current selftest has a new failing NOMML block.
- `HANDOFF.md:24-25`, `HANDOFF.md:101-102`, and `HANDOFF.md:138` disagree among themselves on selftest block count.
- `HANDOFF.md:3-5` still says P1+P2 complete and P3 next, while commits show P3 and session work already landed.
- `HANDOFF.md:21-23` says the only auto-write surface is `SIDEKICK JAM`, but `mcp_server/live_write.py:44-48` adds `SIDEKICK BOOTH` as a second auto-write surface.

Impact: future agents will make wrong operational assumptions unless they rerun commands. In this project, stale docs are not harmless because they steer live Ableton actions and taste data collection.

Fix direction: after the selftest is green, regenerate the handoff from current facts: tool count, selftest count, taste pairs/weight, live track contracts, current phase, and current dirty/unmerged state.

### P2 - `core.py` and `server.py` remain high-blast-radius modules

The repo has improved with `gears.py`, `finisher.py`, `rituals.py`, `live_write.py`, and pure helpers, but `mcp_server/core.py` is still 4,919 lines and `mcp_server/server.py` is 2,556+ lines. The selftest is still one monolithic function.

Impact: feature additions like NOMML can break the single selftest late in the run, and behavior spanning ratings, taste, Live writing, and corpus artifacts is still hard to audit locally.

Fix direction: keep the single `--selftest` entrypoint, but split blocks into named pure test functions. New experimental gates should live near their pure module and be called by the umbrella selftest.

## Healthy Areas

- The established ingest/source-tagging pipeline is holding: current phrase index has zero blocked-project leaks.
- The main MCP behavior was green before the latest NOMML addition.
- Browser panel JavaScript test is still passing.
- The project has the right discipline around unproven metrics: the new NOMML report explicitly says the gate stays off until ear confirmation.
- The live-write policy is generally careful: name-locked tracks, confirm-gated destructive action, and jam-track verification are the right patterns to copy.

## Recommended Order

1. Fix the NOMML selftest so `mcp_server/server.py --selftest` is green again.
2. Fix the booth staging/page layout contract before using `tools/session.py booth` for new taste data.
3. Add stale-votes protection to `tools/session.py`.
4. Add read-back verification to `stage_named_clips()`.
5. Refresh `README.md` and `HANDOFF.md` from current command output.

## Deeper Pass Addendum

Additional review after the first report focused on Live-session safety, vote identity, and NOMML semantics.

### P1 - Name-locked Live track lookup is still fragile with nested PP track objects

`mcp_server/live_write.py:147-159` claims to be tolerant of Producer Pal schema drift, but it scans flat object blocks using `r"\{[^{}]*\}"`. That fails if a track object contains a nested object such as the `instrument` payload mentioned in `HANDOFF.md`.

Synthetic proof:

```text
flat_lookup   -> (0, '1')
nested_lookup -> (None, None)
nested_count  -> 1
```

The tested nested string was:

```text
tracks:[{id:"1", trackIndex:0, name:"SIDEKICK JAM", instrument:{name:"Operator"}, isArmed:false}]
```

Impact: `write_answer_to_jam_track()` and `stage_named_clips()` can fail to find an existing `SIDEKICK JAM` or `SIDEKICK BOOTH` track and create a duplicate at the end of the set. This undercuts the main safety policy: auto-writes are supposed to be name-locked.

Fix direction: replace the flat brace regex with a small balanced-brace scanner over the `tracks` array, or parse PP output through a tolerant JS-object parser. Add a selftest for nested `instrument:{...}`.

### P1 - Completed localStorage votes can leave `tools/session.py` waiting forever

The vote pages load prior votes from localStorage:

- `tools/build_booth_page.py:136-138`
- `tools/build_audition_page.py:96-97`

If the page already has a complete localStorage payload for the same `VKEY`, it can render the done state without calling `save()`. `tools/session.py:131-143` only advances when its local HTTP handler receives a POST. In that case the server can sit at `0 / N voted` even though the browser says done.

Impact: the "one-command" session can stall or push the user toward Ctrl-C, which combines badly with the stale-file issue above.

Fix direction: on page load, if localStorage already contains votes, immediately POST the current payload once. Better: include a `round_id`/`vkey` in the payload and have the server reject mismatches.

### P2 - Booth page vote key and ingest round id are different identities

Current live values:

```text
page_vkey           booth_votes_ce6e483b
computed_round_id   booth_751e6395
predictions_round_id booth_751e6395
session votes path  tools/ideas/booth_votes_booth_751e6395.json
```

The page `VKEY` is derived from `pair_id`, `A_phrase_id`, and `B_phrase_id`. The ingest/prediction round id also includes `selection_mode`. The POST body contains neither id.

Impact: a localStorage payload can be reused across rounds that have the same visible pair ids but different selection semantics or prediction sidecars. That is not likely every day, but it is exactly the kind of metadata drift that makes ear-vs-model scoreboards hard to trust later.

Fix direction: pass the prediction `round_id` into the generated page and include it in every POST/export. Have both session and ingesters reject payloads whose round id does not match the current manifest/predictions sidecar.

### P2 - NOMML currently counts note starts, not unique onset times

`ingest/nomml.py:38-42` computes levels over every note in `notes`. For polyphonic material, a single chord onset contributes multiple samples to the median. That may be defensible if following note-onset literally, but for a played-vs-programmed timing gate it can over-weight chord density rather than timing feel.

Impact: pads/chords and dense drum hits can be classified partly by how many notes share a timestamp, not just by whether the human timing is off-grid. This may explain some role/source skew and should be decided explicitly before NOMML feeds any `performed_only` consumer.

Fix direction: report both note-weighted and unique-onset-weighted NOMML in `tools/report_nomml.py`. Let the spot-check decide which better matches the ear before wiring a gate.

### P2 - Session fallback text is too vague for pairwise booth rounds

When Live staging fails, `tools/session.py:217-218` prints `drag store/exports/pair_NN.mid`. But pairwise exports are per clip id, while the voting task is per pair side. The authoritative mapping lives in `pairwise_manifest.json` as `A_midi`/`B_midi`.

Impact: in fallback mode, the user can easily drag or play the wrong MIDI for A/B, again contaminating taste data.

Fix direction: print the actual manifest mapping for each pair: `P01 A -> <A_midi>`, `P01 B -> <B_midi>`, or generate a small fallback checklist file.

### Extra Verification From Deep Pass

- Current `tools/session.py` vote targets do not exist yet for the active round:
  - audition: `tools/ideas/audition_votes_r2b.json`, tag `r2b`
  - booth: `tools/ideas/booth_votes_booth_751e6395.json`, tag `booth_751e6395`
- Existing historical vote/archive files:
  - `tools/ideas/audition_votes_r1.json`
  - `tools/ideas/pairwise_r1_rated.json`
  - `tools/ideas/rank_audition_r1_rated.json`
- Current branch baseline at deep pass: `9a17e9b feat(feel): NOMML played-vs-programmed gate - compute + report, consumers OFF`.
