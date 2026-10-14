# Durable KEEP audit repairs — 2026-09-05

Implementation and controller acceptance record for the five approved improvements.
Controller decision: **GO for local delivery** of all five improvements.
Physical bridge and Live API capture both passed scratch Live verification.
The owner has resumed the previously deferred musical trial; preparation has only
reached reading the session workflow. No musical material has been auditioned
or judged. Test results do not establish that suggestions sound better or survive
real composing sessions.

## Behavior

1. **Stable suggestion identity and safe DROP.** Verified writes retain Set,
   runtime, track and clip identity. DROP resolves the original clip on SIDEKICK
   JAM, refuses stale/replaced/adopted targets, and verifies absence. A renamed or
   moved clip cannot be replaced by whatever later occupies its old slot.
2. **Complete accounting and recoverable judgment.** Public JAM writes are
   recorded; revisions preserve one suggestion and its original judgment.
   Judgment events bind their payload and learning stores deduplicate retries.
   Preview then confirm can execute pending deletion without learning twice.
3. **Producer Pal 2.2.0.** The portal is pinned to stable 2.2.0. The adapter
   distinguishes requested notation from observed response properties, supplies
   the notation header to direct MCP requests, and shares path-only clip parsing.
   Advanced operations require verified Live API capability.
4. **Truthful room and audio context.** Fractional tempos are supported. Room
   context reads playing slots and meter-aware Arrangement position. Audio
   inventory distinguishes source regions from rendered captures and discloses
   unrendered transformations. Missing mapping requires capture. Cards are bound
   to the Set and checked again when used.
5. **Survival and adoption evidence.** Complete Session and Arrangement inventory
   resolves one-to-one matches. Ambiguous, partial, or legacy evidence stays
   unresolved; a definitive rate is withheld. Next-session observations and
   explicit verified adoption links are separate from historical closure.

Legacy ledger reads do not rewrite files. The first schema-changing mutation
creates a backup. Existing ratings and musical source state are preserved.

## Verification status

Controller verification uses disposable copies of code and state, separate from
The owner's learning stores:

- Full unit suite: 479 tests, PASS; four existing approved-rhythm fixture skips.
- Purity: 5 tests PASS. Node bridge harness: PASS. Panel: 30 tests PASS.
- Focused hostile probes: transport/identity 35; rejection/import isolation 18;
  ledger identity 17; lifecycle 43; audio/context 85; capture 23; final pedal and
  judgment regression group 20. These overlap the full suite.
- Full server selftest: PASS, exit 0 and exact
  `=== all selftests passed ===` sentinel, in a fresh disposable copy (177.03 s).
- Scratch Live MIDI, Live API capture, and physical bridge capture gates: PASS
  (details below).

The ten original findings map to the repairs above: F1/F2 identity and inventory,
F3 public write accounting, F4 import/state isolation, F5 shared PP clip parsing,
F6 selected audio regions, F7 fractional tempo, F8 judgment recovery, F9 honest
handshake capabilities, and F10 rejection filtering. Additional controller probes
cover Set switches, ambiguous inventory, closed-session history, seed conflicts,
audio-card reuse, capped excerpt metadata, and pedal slot reuse.

## Producer Pal device

Official stable device staged from the
[2.2.0 release](https://github.com/adamjmurray/producer-pal/releases/tag/v2.2.0),
9,422,544 bytes. SHA256 checked against the release asset digest:

`41003be8962e62a0d82fa6d560a60aa9ca6bf6af149368f1b26c26119649d914`

Installed alongside the older devices as
`Producer_Pal_2.2.0.amxd` in the User Library
`Presets/MIDI Effects/Max MIDI Effect/Imported/` directory. The installed hash
matches the staged release asset. The actual Live handshake confirmed 2.2.0.
The device's Direct Live API setting was enabled in the scratch Set; enable it
in any other Set that needs the identity and raw clip-property reads used here.

## Scratch Live evidence and remaining boundary

Verified in Ableton Live 12 Suite 12.4.5 with the stable 2.2.0 device:

- Public MIDI write and note readback; KEEP then revision retains one entry.
- DROP preview, confirmed deletion with absence readback, replay without repeated
  learning, and refusal to delete a replacement occupying the old slot.
- Full duplicate audition track retains mixer settings; promotion preserves its
  Arrangement start and records KEEP after note readback. The source-track
  fingerprint remains unchanged.
- Verified adoption into Arrangement. When two synthetic entries claim the same
  promoted clip, the report marks the collision unresolved and withholds the rate.
- Actual playing Session slot 1 is observed; fractional tempo 120.5 is retained.
  A saved-path change rejects the previous Set identity without mutation.
- Live API captures at 120.5 BPM: one 3/4 bar measured 1.493787 seconds and one 4/4
  bar 1.991723 seconds. Both were intentionally silent PCM fixtures, not musical
  listening trials. Arm state was restored; normal cleanup and an injected
  post-recording failure both removed clips and backing files.
- Initial Live API gate cleanup: four original tracks, no test clips or temporary Sidekick
  tracks, and an empty scratch Project recording directory. Audio input/output
  were restored to No Device, and Live was closed.

The Live gate exposed two adapter gaps, now fixed and covered by six additional
unit tests: exact missing-ID error readback and inherited Arrangement track paths.
Malformed Session clip collections also remain explicitly incomplete.

Physical **bridge capture: PASS** in the continuation. The existing
`Downloads/SIDEKICK BRIDGE/SIDEKICK_BRIDGE.amxd` was located and staged with its
adjacent executor, which matches `tools/m4l/sidekick_bridge.js` byte for byte
(SHA256 `f699fd7111edf6a4f00c7d9267be1a1b53a48ef1e5c55f7ecc0aea32fc9dd6d9`).
The device's UDP reply confirmed the scratch Set path. Explicit `surface="bridge"`
captures at 120.5 BPM measured 1.494014 seconds for one 3/4 bar and 1.992018 seconds
for one 4/4 bar. Both contained silent PCM. Arm restoration, clip absence, and
backing-file deletion passed; an injected post-recording registration failure
also left no clip or recording. The two temporary tracks were removed, original
track IDs verified, and tempo/meter restored to 120 BPM and 4/4. This verifies
the existing device; it does not establish a reproducible AMXD build.

After the bridge continuation, the cleaned scratch Set was saved and stopped.
Live was left open with audio input No Device and output MacBook Pro Speakers.
This supersedes the initial gate's closed-application state above; recheck the
current Set before starting the musical trial. Bridge verification was recorded
in commit `f7e861f`.

No real musical A/B, next-session listening, or model-audio upload was run.

The subsequent Codex MCP connection check exposed an event-loop boundary defect
that direct Python acceptance calls had missed. `inventory_session_audio` and
`listen_to_takes` now offload their synchronous helpers at the async MCP wrapper.
Three dispatch regressions reproduced the defect and verify discovery, offline
errors, and Set-switch rejection before upload. The focused group passes all
94 tests; fresh stdio startup returns `live_inventory_unavailable` with Ableton
closed and advertises the updated `gemini-3.8-flash-medium` listener default.
This is additional transport evidence, not a completed musical listening trial.

The initial template copy was outside a Live Project, so its first capture failed
the recording-root guard. Live Save As created a proper scratch Project; the
failed capture's clip and relocated backing file were removed. Startup also had
an audio sample-rate mismatch; a temporary built-in output enabled verification
and was restored afterward.

## Execution evidence

Workers used the requested economical routes: Muse Spark 1.3, Cursor Composer 2.5,
Codex Luna, and AGY Gemini Flash 3.8. Each worked in an isolated Git worktree;
the controller reviewed, repaired, and tested before accepting commits.
The existing Session Ears listener model was not changed by the worker selection.

Detailed local prompts, logs, reports, staged device, and scratch acceptance Set
are under `.scratch/durable-keep-audit-fixes/`. There is no Git remote.
The SDK migration, new generators, SOMBRA panel, and broad core split remain
outside this implementation.

Canonical learning-store verification: all 19 pre-execution JSON hashes remain
unchanged. Test state and judgments stayed in disposable copies. The pre-existing
untracked `Updating` file was preserved.
