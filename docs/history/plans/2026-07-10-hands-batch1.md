# The Hands (batch 1) — Implementation Plan

> Executors: Cursor Composer 2.5 (headless `cursor-agent`) and Claude Sonnet 5,
> alternating tasks, ONE task in flight at a time (shared working tree).
> AMENDED 2026-07-11 (the owner): Composer 2.5 is the sole executor from T8 on.
> Reviewer: the coordinating session agent reviews every task's diff against
> the spec before the next task dispatches. Steps use checkbox syntax.

**Goal:** develop_my_loop — 3 slow-burn flavours + 5 motivic ops as
velocity-provenance-tested transforms of one retrieved real phrase, dormant
ears, sticky develop, and a measurement-only develop_ab blind round.

**Spec (the contract — read first):**
`docs/superpowers/specs/2026-07-10-hands-develop-my-loop-design.md`

## Global Constraints (bind every task)

- Repo `~/composing-sidekick`, branch `feat/ear-semantics`,
  venv `.venv/bin/python`. Tests are named blocks in `mcp_server/server.py`
  `run_selftest()` inserted before `print("=== all selftests passed ===")` — NO
  pytest. Full check: `.venv/bin/python mcp_server/server.py --selftest`
  (exit 0) + `.venv/bin/python ingest/selftest_ingest.py` + `node
  tools/ideapack_panel.test.mjs` when core/ingest/tools are touched.
- Never touch `_ANSWER_TRANSFORMS`, gear-3's `random.choice`, the taste
  trainer, or the ears model. `develop_ab` never calls `rate_candidates`.
- Ears-off byte-identical; fail loud, never guess; READ-ONLY on Live;
  deterministic given seed/pass_index; canonical announcement voice; state
  never identity.
- Every commit message ends with:
  `Co-Authored-By: <executor name> via composing-sidekick Hands batch-1`
- One commit per task; working tree clean after each task.

## Tasks (sequential; assigned executor in brackets)

### T1 [Composer 2.5] — no_fit fold-in + latent bug (spec §8)
Files: `mcp_server/gears.py`, `mcp_server/sombra_listener.py`,
`mcp_server/server.py` (selftest block).
- Fix `gears.py:118` → `core._octave_variant(nd, 12)`.
- Guards: gear-1 (`gears.py:~144-148`) and gear-3 (`~165-167`) return the
  refusal `ans` (with `ans["gear"]` set) when `ans.get("no_fit")`, before any
  id access / surgery; `sombra_listener.py:~133` skips the jam write on
  `no_fit` and surfaces the refusal.
- Selftest: synthetic `no_fit` ans through each guard (no crash, refusal
  shape); `apply_surgery(nd, "octave_displacement", ...)` returns notes.

### T2 [Sonnet 5] — the worker refactor (spec §1)
Files: `mcp_server/core.py`, `mcp_server/server.py` (selftest block).
- Extract `_answer_worker(...)` from `answer_my_loop` (core.py:3467-3644)
  exactly per spec §1 — including `transform_fn`, `prospective_fn`,
  `pin_source` (ValueError on pinned id absent from bank), finalize tail
  inside the worker.
- `answer_my_loop` = thin wrapper, behaviour-preserving. `develop_my_loop` =
  second wrapper with `_DEVELOP_TRANSFORMS` registry (empty-but-shaped:
  `{move: {"transform": fn|None, "prospective": fn}}`), `develop_move` key only
  when a move ran.
- Selftests: existing answer blocks stay green (run full suite);
  `develop_my_loop(x, move=None) == answer_my_loop(x, transform=None)` literal
  dict equality; unknown move raises naming the roster; pin_source honoured +
  absent-pin raises.

### T3 [Composer 2.5] — legal set + snap (spec §2)
Files: `mcp_server/core.py`, selftest block.
- `_legal_degree_set(nd, tonic_pc)`; `_snap_to_legal(pitch, tonic_pc,
  legal_set)` nearest-PC snap (thin variant of `_snap_to_scale`,
  core.py:1861).
- Selftests per spec §10 "Legal set".

### T4 [Sonnet 5] — slow-burn:reveal + ramp (spec §3.1)
- `_slow_burn_reveal(nd, pass_index, n_passes=4)`; register
  `slow-burn`/`slow-burn:reveal` in `_DEVELOP_TRANSFORMS` (prospective mirror =
  transpose→reveal). Provenance + ramp selftests per §10.

### T5 [Composer 2.5] — slow-burn:accent (spec §3.2)
- `_slow_burn_accent(...)` — velocity-only re-voicing via velocity DNA
  (`velocity_by_role`, the humanize source), per-pass phase offset, ±18 bound,
  missing DNA → identity. Register + mirror. Selftests per §10.

### T6 [Sonnet 5] — slow-burn:add (spec §3.3)
- `_slow_burn_add(...)` — clone (velocity,duration) from real notes of the
  phrase; pitch via `_snap_to_legal`; placement from `_thicken_notes`' gap
  logic (core.py:1440) with invention lines replaced; +40% cap; pass-graded.
  Register + mirror. Selftests per §10.

### T7 [Composer 2.5] — motivic: reharm + retro (spec §4.1, §4.4)
- Register `motivic:reharm` = `_reharmonize_notes` (core.py:1619) as-is.
- `_retrograde_notes(nd)` — reversed pitch sequence over the original grid.
  Register both + mirrors. Selftests: full provenance for both; retro pitch
  multiset + grid identity.

### T8 [Sonnet 5] — motivic: fragment + walk (spec §4.2, §4.5)
- `_fragment_harmonic_notes(nd)` — `_fragment_notes` skeleton (core.py:1520)
  with `seq_offsets` (:1531) replaced by `_map_pitch_to_chord` (:1599) over
  `_reharmonize_pair`/`_roman_chord_pcs` chords.
- `_alphabet_walk_notes(nd, key_str, seed)` — motif restated on a seed-chosen
  walk through {0,+3,+5,+7,+10}, snapped to legal set.
  Register + move-specific (re-timing) mirrors. Velocity-only provenance
  selftests; walk degrees ∈ legal set.

### T9 [Composer 2.5] — motivic: invert (spec §4.3)
- `_invert_motif_notes(nd, key_str)` — skyline interval negation
  (motif_scan.py:33), `_snap_to_legal`, subset guard → identity fallback marked
  `motivic:identity`, `dark_landing` preference via `analyze/motifs.classify`
  (motifs.py:60). Register + mirror. Selftests per §10 "Inversion".

### T10 [Sonnet 5] — MCP surface + bare-`motivic`/`slow-burn` dispatch
Files: `mcp_server/server.py`.
- `develop_my_loop` MCP tool wrapper (docstring = LLM-facing usage; params
  mirror answer_my_loop's wrapper incl. use_ears/direction); bare move names
  dispatch deterministically by seed. Selftest: wrapper exposes the params;
  ears-off adds no ears keys through the tool path.

### T11 [Composer 2.5] — sticky develop (spec §7)
Files: `mcp_server/core.py` or new `mcp_server/develop_session.py` (executor's
call — keep it small), `mcp_server/gears.py` (judge_answer hook),
`mcp_server/server.py` (sticky_develop tool + answer wrapper route),
`mcp_server/sombra_listener.py` (ANSWER route), `tools/session.py` (reset at
start).
- Exactly per spec §7: state file + flock, toggle tool, KEEP holds/advances,
  DROP clears, serving routes via pin_source, no_fit keeps the hold, canonical
  announcements, session reset clears. Lifecycle selftests per §10.

### T12 [Sonnet 5] — develop_ab end to end (spec §6)
Files: `tools/build_develop_booth.py` (new), `tools/ingest_booth_votes.py`,
`mcp_server/server.py` (selftests), `HANDOFF.md`/`ROADMAP.md` (one status line
each).
- The seven ingest edits verbatim from spec §6 (train-guard, tuples, index_ids
  union, honest A-side statement, kind+per_move+develop_promotion_status+main
  branch, two-class F7 guard, gold rejection). Builder with even move sampling,
  A==B skip, <5 abort, dry-run writes nothing. Selftests per §10
  "develop_ab measurement-only" + "build_develop_booth".

## Review protocol (the coordinator, after every task)

1. `git diff <base>..HEAD` reviewed against the spec section + Global
   Constraints; run the full battery; verify provenance selftests actually
   assert what the spec scopes.
2. Findings → fix instructions to the SAME executor; re-review. Next task
   dispatches only on a clean review.
3. Ledger: `.superpowers/sdd/progress.md` (`Hands T<n>: complete (<sha>,
   executor, review clean)`).
