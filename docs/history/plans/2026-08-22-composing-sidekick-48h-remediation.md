# Composing-Sidekick 48h Remediation Implementation Plan

> Execute test-first. Preserve current musical outputs unless this plan explicitly changes them.

**Goal:** Close the Session Ears arbitrary-file disclosure and repair the confirmed ingest, rhythm-delivery, bridge-ownership, CLI, and repository-hygiene findings from the 48-hour audit.

**Architecture:** Producer Pal is the sole MCP trust source for Live audio paths. The MCP layer returns opaque, short-lived inventory capabilities and sanitized take descriptors; filesystem paths remain in a process-local registry until a caller selects inventoried takes. Smaller repairs remain inside their existing subsystem owners and must not restart the parked core split.

## Global Constraints

- Work only on `feat/audit-improvements`; do not merge, push, deploy, install dependencies, or write to Ableton Live.
- Preserve `store/session_ears/<piece>/take_cards.json`; never delete or rewrite it.
- Use strict red-green-refactor for every production behavior change and record the exact failing and passing commands.
- The MCP Session Ears path is Live-only. No caller-provided manifest, session state, audio path, or automatic manifest fallback may reach it.
- MCP responses, errors, persisted cards, and normal logs must not contain absolute source audio paths.
- Existing deterministic outputs from `compose_from_profile`, `develop_idea`, `send_to_ableton`, and `find_phrases` remain unchanged.
- Do not add generators or continue the parked core split. Durable KEEP remains the product priority.
- All subprocess staging and cleanup must remain fail-closed.

## Task 1: Trusted Live Session Ears Capability Flow

**Primary files:** `mcp_server/session_ears.py`, `mcp_server/server.py`, Session Ears tests and operator docs.

1. Add failing behavioral tests proving:
   - the public MCP inventory accepts only optional `track_indices` and `views`, never raw state or paths;
   - the default scope covers Session and Arrangement views on regular Live tracks;
   - inventory responses contain sanitized descriptors and no `sampleFile` or absolute path;
   - valid unrelated WAV paths cannot be selected unless Producer Pal inventoried them;
   - unknown, expired, replayed, concurrently claimed, and cross-inventory take IDs fail closed;
   - a claimed capability is single-use even when staging or agy later fails;
   - the private manifest helper rejects paths outside an explicit trusted root;
   - staging cleanup and session-bound card persistence still work.
2. Implement async Producer Pal discovery using one validated `pp_session.operation_session` per inventory. Enumerate regular tracks and requested Session/Arrangement clip views, read sample metadata for audio clips, and ignore MIDI clips.
3. Replace the MCP wire contract with:
   - `inventory_session_audio(track_indices: list[int] | None = None, views: list[Literal["session", "arrangement"]] | None = None) -> InventoryEnvelope`
   - `listen_to_takes(inventory_id: str, take_ids: list[str], focus: str = "full", model: str = DEFAULT_MODEL) -> {"cards": list[TakeCard], "errors": list[ListenError]}`
4. `InventoryEnvelope` contains `inventory_id`, Producer-Pal-derived `session_id`, `expires_at`, and `takes`. Each take exposes only opaque `take_id`, track index/name, view, clip locator/name, and available duration metadata.
5. Store canonical audio paths only in a locked in-memory registry. Use at least 192 bits of randomness, a ten-minute TTL, a 128-take inventory ceiling, and a 64-take listen ceiling. Atomically consume a capability before staging begins.
6. Retain extension, magic-byte, size, existence, and regular-file validation. Remove source paths from cards/errors/logs and clean staging in `finally` paths.
7. Replace generic manifest/state inventory with a private offline test/CLI helper requiring both manifest path and trusted root. MCP failure without Live/Producer Pal is `live_inventory_unavailable`, not a fallback.
8. Update `docs/SESSION_EARS.md`, the chat playbook, and MCP reference to the new zero-manifest capability workflow.

**Acceptance:** Focused Session Ears tests and selftest pass; legacy raw-take calls fail; no absolute path crosses the MCP boundary.

## Task 2: Exact ALS Skip Accounting and Catalog CLI Output

**Primary files:** `ingest/parse_als.py`, `ingest/scan_catalog.py`, `tests/test_parse_als.py` plus a focused CLI test.

1. Add failing tests for a malformed-pitch KeyTrack containing multiple events, a missing-pitch KeyTrack containing multiple events, aggregate invariants, and captured CLI output containing real newlines rather than literal `\\n`.
2. Count `malformed_pitch` once per descendant MIDI event discarded. Add `missing_pitch` and count it the same way. A pitch-invalid KeyTrack with no events contributes zero discarded notes.
3. Preserve existing event-level reasons and ensure `notes_skipped == sum(skip_detail.values())` at clip, project, materialization, and catalog-manifest boundaries.
4. Replace escaped newline literals in catalog reporting with real line breaks without changing report content.

**Acceptance:** Focused ingest/CLI tests and the full unit suite pass; five discarded events report exactly five skips.

## Task 3: Portable Approved-Rhythm Delivery

**Primary files:** `store/approved_rhythms.json`, tracked approved MIDI assets, `mcp_server/core.py`, and focused style/brief tests.

1. Add failing behavioral tests proving `get_style()["structured"]["approved_rhythms"]` exposes compact approved pattern metadata and `compose_brief` contains the approved rhythms even when the relevant STYLE.md prose lies beyond its excerpt limit.
2. Test that model-facing results contain no absolute path or raw Producer Pal response and that malformed tracked rhythm metadata fails clearly.
3. Normalize the tracked schema to IDs, titles, descriptions, BPM guidance, and repo-relative MIDI asset paths. Move every referenced approved MIDI into a tracked portable asset directory outside ignored `store/exports/`.
4. Add a strict loader that rejects absolute paths, traversal, missing assets, duplicate IDs, and malformed entries.
5. Wire the compact metadata into the structured style result and a dedicated `compose_brief` section. Keep archival raw responses out of model context.
6. Do not alter deterministic idea generation or introduce a rhythm generator.

**Acceptance:** Focused tests pass, the brief names each approved rhythm, assets resolve inside the repo, and the four protected public musical probes remain byte-identical.

## Task 4: Bridge Ownership, Hygiene, and Active Status

**Primary files:** `mcp_server/core.py`, `mcp_server/taste_bridge.py`, `tests/test_purity.py`, `.gitignore`, `HANDOFF.md`, and `ROADMAP.md`.

1. Add failing tests that behaviorally prove the phrase-pool test hook changes the bridge-owned pool used by consumers and that bridge-owned state names cannot be reassigned at top level in `core.py`.
2. Remove dead duplicate `_PHRASE_POOL_OVERRIDE` and duplicate taste-threshold assignments from `core.py`. Preserve necessary read compatibility through facade imports while `taste_bridge` remains the sole owner.
3. Strengthen the AST purity guard only for the explicit bridge-owned state list; avoid a broad overlap rule that rejects legitimate constants.
4. Add `store/session_ears/` to `.gitignore` without deleting the existing card. Fix touched whitespace and require `git diff --check`.
5. Update active handoff/roadmap status to describe this bounded maintenance repair and reaffirm that further core splitting and new generators remain parked behind durable KEEP.

**Acceptance:** Purity and module-identity tests pass, the existing card is unchanged and ignored, and no deterministic public output changes.

## Task 5: Integrated Verification and Release Verdict

1. Run all unit tests, component selftests, and `python3 mcp_server/server.py --selftest`; require the exact `=== all selftests passed ===` sentinel.
2. Run differential probes for `compose_from_profile`, `develop_idea`, `send_to_ableton`, and `find_phrases` against the pre-remediation commit.
3. Run `git diff --check`, `git fsck --full`, and inspect `git status` for unintended artifacts or user-data changes.
4. Perform a controlled read-only Live/Producer Pal/agy acceptance when those external systems are available: one Session audio clip, one Arrangement audio clip, one MIDI clip, and multiple tracks. Confirm path redaction, MIDI exclusion, explicit subset listening, one-shot consumption, cleanup, and rejection of a non-inventoried valid WAV.
5. If the real external capture is unavailable, report automated completion but retain the release verdict as **NO-GO pending external capture**. Green mocks/selftests alone do not promote it.

**Acceptance:** Automated checks are green and an independent whole-branch review has no open Critical or Important findings. GO requires the real external capture.
