# Voice, Ears, Structure, and KEEP Loop — Replacement Implementation Plan

> **Baseline:** commit `e6e4974`. All assumptions tied to `83d60cc` are stale.
> **Execution:** every worker uses an isolated Git worktree based on the latest
> controller-accepted commit. Workers never edit the controller checkout, update
> the controller ledger, or integrate their own commits.

## Goal

Deliver the complete original scope:

- A durable Producer Pal voice/context and smaller MCP profiles.
- Session Ears recording through both SIDEKICK BRIDGE and Producer Pal Live API surfaces.
- Safe song arrangement built only on fresh, fully duplicated `SIDEKICK` tracks.
- Take-lane audition, promotion, and verified KEEP recording.
- Executing MIDI writes and one-bar surgery on Sidekick-owned clips.
- Explicit stem-aware reference analysis.
- Compatibility with Producer Pal 2.1, with 2.2 RC features strictly version-gated.

Every mutating tool follows one contract: `confirm=False` may inspect and preview
but performs zero writes; `confirm=True` executes immediately, reads the result
back, and reports verified state or failure.

## Public Interfaces and Safety Contracts

### Voice and profiles

- Add an idempotent global-context synchronizer that merges a managed Sidekick
  digest into the existing Producer Pal context while preserving all text outside
  managed markers.
- Back up the original context, cap the merged document at 9,500 characters,
  write atomically, reread it, and restore the backup on verification failure.
- The digest includes musical identity, rhythm rules, routing language,
  KEEP/DROP discipline, and safe mutation boundaries. It contains no
  project-specific absolute paths.
- Introduce registry-based MCP profiles: `compose`, `session`, `reference`,
  `debug`, and `all`.
- Make `compose` the default in `.mcp.json`. It includes the complete core loop:
  hear/capture, inventory/listen, generate/write, judge, and promote. Fallback
  recipes may reference only tools present in the active profile.
- Preserve existing tool names and schemas where possible; profile selection
  changes registration, not tool behavior.

### Producer Pal adapter and Session Ears

- Fix existing Session Ears correctness issues first:
  - Registering a new inventory must not expire previous live inventories.
  - Reject UNC paths such as `\\server\share`.
  - Inventory tokens remain opaque, bounded, expiring, and free of raw paths in
    public responses.
- Add a validated Producer Pal capability handshake. Use reported `pp_version`;
  never infer 2.2 support from a successful path lookup.
- Parse actual Producer Pal response envelopes, including nested `results`.
- Emit the real `ppal-live-api` schema: top-level `path` plus an `operations`
  array using typed `get`, `set`, `call`, and related operations.
- Add:
  `capture_loop(bars=8, source_scene_index=None, surface="auto", listen=False, confirm=False)`.
- `surface="auto"` prefers Live API when capability-tested and otherwise uses
  SIDEKICK BRIDGE.
- Default capture records what is already playing. A supplied scene index
  explicitly launches that scene.
- Compute bar duration from Live's current numerator, denominator, and tempo.
- Capture only to a `SIDEKICK EARS` audio track and an existing empty session slot.
  Never arm, reroute, rename, or otherwise mutate a source track.
- Refactor the bridge into explicit start/status/finalize/cancel states. The
  scheduled stop must finish before reading the result.
- Validate audio under the current Live project's recording area, register it
  through Session Ears, and return only an opaque capture ID, Live object path,
  listening card, surface, and cleanup receipt.
- In `finally`, always disarm, stop pending tasks, and restore Sidekick-owned
  temporary state, including timeout and partial-failure paths.
- `listen=True` invokes the existing Gemini listener only after local validation.

### Structure, arrangement, and KEEP closure

- Extend `lay_the_song` while preserving legacy preview inputs:
  `lay_the_song(scene_index=0, parts=None, target_minutes=None, tempo=None, seed=0, locators=True, commit=False, confirm=False)`.
- `parts` optionally contains `{part_id, role, source_path}` objects and permits
  several parts with the same role.
- Automatic discovery reads nonempty clips from the selected scene and accepts
  only unambiguous track-role aliases. Unknown or ambiguous mappings return
  `needs_parts`; confirmed execution refuses until explicitly resolved.
- Reuse `plan_song_map` and the existing arrangement role/variation policy,
  adapting its real `bar_start` and density `1..10` shapes.
- Preview returns the complete structure and intended operations without writes.
- `confirm=True, commit=False` writes and verifies locators only.
- `confirm=True, commit=True`:
  - Creates a fresh full duplicate of every selected source track named
    `SIDEKICK ARR <part> <run-id>`.
  - Duplicates devices and mixer state but no clips.
  - Places explicit clip duplicates on those new tracks with `toPath`,
    `arrangementStart`, and `arrangementLength`.
  - Supports MIDI and audio source clips without transforming originals.
  - Fingerprints each source track before execution and rereads it afterward;
    any source change fails acceptance.
  - Returns exact created track IDs and an undo manifest. Previous Sidekick
    arrangement tracks may never be reused.
- Add `audition_in_lanes(source_path, n=3, seed=0, confirm=False)`:
  - Create a fresh Sidekick duplicate track with devices and no clips.
  - Put the source copy on the main arrangement lane and at most eight candidates
    in newly appended take lanes.
  - Persist an opaque audition/candidate-to-lane mapping.
- Add `promote_lane(audition_id, candidate_id, confirm=False)`:
  - On Producer Pal 2.2, use `ppal-duplicate` from the lane clip to
    `toPath:"t<track>"`.
  - Reread the promoted main-lane clip before recording KEEP in the judgment ledger.
  - Do not claim clip-envelope preservation: Producer Pal lane duplication may
    discard envelopes.
  - On 2.1, return a clear unsupported-capability response without mutation.

### Executing clip writes and stem-aware references

- Keep `send_to_ableton` a pure handoff builder. Remove recipes that pass a track
  name where its legacy numeric schema expects an index.
- Add an executing `write_midi_clip` tool:
  - Target tracks by exact `SIDEKICK` name.
  - Create only in an empty slot unless `replace=True`.
  - Refuse every non-Sidekick destination.
  - Execute through Producer Pal and verify note count, loop bounds, name, track,
    and slot.
- Add `surgery_clip(path, bar, surgery, confirm=False)`:
  - Accept only a Sidekick-owned MIDI clip and a one-based bar.
  - Read the complete clip and snapshot unaffected bars.
  - Emit `preTransforms` only for the selected bar, write replacement notes, and
    prove unaffected bars are unchanged.
- Extend `analyze_audio_ref` with `stem_files: dict[str, str] | None`.
  - Require explicit stem paths; never guess Ableton processed-stem filenames.
  - Copy validated stems into the reference artifact and expose no raw paths.
  - Decode drums to mono PCM, derive deterministic energy-flux onsets, suppress
    nearby duplicate peaks, and snap accepted onsets to a sixteenth-note grid
    using the known reference tempo.
  - Treat the result as rhythmic steering evidence, not transcription.
  - `beat_from_ref` prefers the measured drums grid and falls back to the existing
    prose `drum_grid` when no valid tempo/stem grid exists.

## Multi-model Execution and Controller Review

Before delegation the controller verifies the clean integration baseline,
`agy` with `gemini-3.7-flash-high`, and Luna as `gpt-5.6-luna`. Gemini always
runs at high effort (encoded by the `-high` model ID). Stop before a wave if its
required worker is unavailable; never silently substitute another model.

### Wave 1 — Foundation

1. Luna, high: Session Ears expiry/UNC fixes and managed global-context synchronization.
2. Luna, medium: Producer Pal response parser, version handshake, and 2.1/2.2 adapter.
3. Gemini 3.7 Flash High through `agy`: registry profiles, compose-profile closure, schema tests.

### Wave 2 — Session Ears

4. Luna, high: bridge state machine, cleanup transactions, time-signature timing, local capture DSP.
5. Luna, high: Live API executor and `capture_loop` orchestration.
6. Gemini 3.7 Flash High: hostile capture fixtures, response-redaction tests, capability-path documentation.

### Wave 3 — Structure and KEEP

7. Gemini 3.7 Flash High: scene inventory, explicit part mapping, role-aware preview.
8. Luna, high: full-track duplication, arrangement tiling, locators, source-fingerprint verification.
9. Luna, high: take-lane audition/promotion, persisted mapping, KEEP integration, undo receipts.

### Wave 4 — Executing tools and references

10. Luna, high: verified MIDI create/replace and bounded clip surgery.
11. Gemini 3.7 Flash High: explicit stem ingestion and deterministic onset-grid analysis.
12. Luna, high: shared failure cleanup and adversarial integration hardening.

Gemini uses `agy` with `gemini-3.7-flash-high` (high effort encoded in the model
ID), edit acceptance, sandboxing, and only its worktree added. Luna uses
`gpt-5.6-luna` with high reasoning for cross-layer mutation work and medium only
for bounded adapters. Each worker delivers
one reviewable commit, exact test output, and a short risk note. The controller
independently inspects and reruns relevant probes, integrates accepted commits
individually, and returns rejected work to the same worker with evidence.

## Automated Gates

After every accepted commit run:

1. `.venv/bin/python -m unittest tests.test_purity`
2. `.venv/bin/python -m unittest discover -s tests -t .`
3. `.venv/bin/python mcp_server/server.py --selftest` and require exact final
   sentinel `=== all selftests passed ===`
4. `git diff --check`
5. Clean-status verification
6. Targeted hostile-input and state-transition probes for the subsystem

Coverage must include multiple live inventories, UNC rejection, context merge
preservation/idempotence/rollback, profile fallback closure, 2.1/2.2 fixtures,
nested Producer Pal results, bridge cancellation, 3/4 and 5/4 timing,
source-track immutability, explicit `toPath`, take-lane limits, verified
promotion, occupied-slot refusal, bounded surgery, stem containment, and
deterministic onset grids.

## Real Ableton Acceptance and Rollback

Offline completion remains **NO-GO** for release until this scratch-set gate passes:

1. Back up the working Producer Pal device and global context.
2. At a user-present checkpoint, verify Producer Pal 2.2.0 RC1 against SHA-256
   `b6845cae25fb8454737f15a734dd8d513b0ffb9b036fb60a4fb2503b031cd6d3`
   and only then install it.
3. Test bridge capture in 4/4 and Live API capture in 3/4, including timeout cleanup.
4. Arrange MIDI/audio loops and prove originals unchanged with all new objects on
   fresh Sidekick tracks.
5. Create three lane candidates, promote one, reread it, and record KEEP only
   after verification.
6. Test create, replace, and one-bar surgery on Sidekick clips and refusal on a
   user-owned track.
7. Analyze explicit drums/bass stems and prove `beat_from_ref` uses the measured grid.
8. Start a fresh MCP session and verify the merged global voice and every profile.

If the RC regresses, restore the saved 2.1 device and context. Keep `.mcp.json`
pinned to the stable Producer Pal portal until compatible GA is validated. No
remote deployment or production system is touched.

## Assumptions

- The user is present for device installation and Live playback/recording checkpoints.
- 2.2-only arrangement-lane behavior follows the RC implementation notes in
  `https://github.com/adamjmurray/producer-pal/pull/1084`.
- Ableton-separated stems are supplied explicitly because processed-stem filenames
  have no stable sibling-filename contract.
