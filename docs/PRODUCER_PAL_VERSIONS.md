# Producer Pal versions

The adapter normalizes Producer Pal wire responses, but mutations remain
fail-closed. `mcp_server.pp_session.SUPPORTED_PP_VERSIONS` lists exact builds
whose device behavior has committed read and mutation captures. Do not add a
version based only on a migration guide or a successful handshake.

## Supported builds

| Build | Fixtures | Addressing | Envelopes captured on |
|---|---|---|---|
| `2.2.0-rc1` | `tests/fixtures/pp/2.2.0-rc1/{read,mutate,legacy,piece,piece-notes}` | `2.2` — numeric `trackIndex` / `trackType` / `sceneIndex`, `arrangementStart` next to a bare track path | the author's working Sets (2026-09) |
| `2.3.0` | `tests/fixtures/pp/2.3.0/{read,mutate,legacy,v23,v23b}` | `2.3` — positions live in the object path | a disposable SCRATCH Set |
| `2.4.0` | `tests/fixtures/pp/2.4.0/{read,mutate,legacy,v23,v23b,v24}` (Live 12.4.6) | `2.4` — 2.3's paths; `count` gone, a batch is one path per object (`t+,t+`, `s+,s+`) | a disposable SCRATCH Set (client pin stays `producer-pal@2.3.0` until a Set moves) |

**What a fixture proves.** The reply *envelopes* are the device's: which keys a reply
carries, how a failure is shaped (MCP error, `ok: false` entry, `WARNING:` part), the
exact error and warning texts, id and path forms, counts and orderings. The *music*
inside the fixtures is synthetic: user-given track and clip names are replaced with
invented ones (stock device and routing labels stay), and the notes of the 2.2.0-rc1
piece run are newly drawn (see `tests/fixtures/pp/README.md`). That run keeps the
captured arrangement skeleton (track list, clip positions, lengths and loop windows), so
the window and coverage tests still exercise real Live shapes. A fixture establishes how
the device answers, not what any Set's music was.

All three work at once: `ppal-connect` reports the build, the handshake names its
`addressing` capability (`pp_session.addressing_of(session)`), and ONE request
builder (`pp_session.PPRequests` / `requests_for(session)`) emits that build's
argument forms. No call site builds locator dicts itself. 2.4 still honours the
legacy parameters with deprecation warnings, but only schema-listed forms are sent.

### What changed in 2.3.0 (all from the captures)

- **Positions in the path.** Tracks are `t3`, returns `rt0`, the main track
  `mt`, scenes `s2`; an arrangement clip is `t3[5|1]` (or `t3[loc:Chorus]`).
  create-clip `path "t10[1|1]"`; duplicate `toPath "t10[9|1]"`; move =
  update-clip `toPath "t10[49|1]"` (the moved clip gets a **new id**); split =
  update-clip `arrangementSplit "59|1"` (head keeps its id, the tail gets a new
  id and keeps its loop phase via `firstStart`); delete a clip by id or by
  `path "t10[49|1]"`.
- **Trimmed copies no longer cut neighbours.** `arrangementLength` trims the
  copy before it is placed (v23_trap_*: the 2.2 "split trap" is gone); a copy
  dropped over a clip wholly inside its span still replaces that clip
  (v23_inside_*).
- **Removed parameters** (tool_schemas.json diff): create-clip / duplicate /
  update-clip `arrangementStart` (and `takeLaneName`, `locator`); read-track
  `trackIndex` / `trackType`; create-track `trackIndex` (`path` `t+` / `t<i>` /
  `rt+`); select `trackIndex` / `sceneIndex` / `trackType`; create-scene /
  read-scene `sceneIndex` (`path` `s<i>`); playback `sceneIndex` and the
  `*Locator` params (`loc:` positions); update-track `*RoutingTypeId` /
  `*RoutingChannelId` (routing by name, plus `sends [{return, gainDb}]`);
  library `queries` → `searches`.
- **Reply shapes.** Tracks carry `path` (no `trackIndex`) in read-live-set and
  read-track; the overview has `mainTrack` (not `masterTrack`) and returns
  with `path` (no `returnTrackIndex` / `type`); arrangement clips carry
  `path "t10[1|1]"` (no `arrangementStart`) plus `arrangementLength`, `start`,
  `end`, `firstStart` (only when ≠ start); select reports `selectedTrack.path`
  / `selectedScene.path`; single creates and duplicates reply with one object
  (`{id, path}`, create-clip adds `noteCount`), a split and a multi-id delete
  with a list; delete adds `deletedPath`; playback update-arrangement replies
  `{playing, startTime}` only — read the brace and playhead through
  `ppal-live-api` (`v23_brace_live_api`); errors read `Error: …` instead of
  `Error executing tool 'x': readX failed: …`.
- **Failures are warnings — and so are some successes.** A MIDI clip
  duplicated onto an audio track returns `[]` plus `WARNING: … was not
  duplicated …`; deleting a missing id returns `deleted: false` plus
  `WARNING: id "…" does not exist`; `isError` is false. But both builds also
  warn next to writes that HAPPENED: the project-context backup sync on every
  call (`Project context backup sync failed: node_request
  'projectContext.sync' timed out after 5000ms`, `Could not save the project
  context backup beside the Live Set …`), colour quantization, a duplicated
  track losing its Producer Pal device. So a write is judged by the reply's
  STRUCTURE, never by warning words: `pp_session.write_failed(response, …)`
  checks the ids, the count, the place each object reports
  (`clip_location`: 2.2 `t10` + `arrangementStart` = 2.3 `t10[5|1]`), a
  create-clip's `noteCount` and a delete's `deleted: true`. update-clip
  replies are left to the readback that follows each update; scene creation
  is proven by re-reading `sceneCount`, locators by their re-read, the
  capture's play-scene by the 2.3 reply's `scene.path` (2.2: a clip slot of
  that scene playing/triggered, read through the bridge) and then the
  transport read back — no proof, no capture, and playback the capture
  itself started is stopped; a JAM clip delete by
  PROVEN absence (`live_write._verify_clip_absent`): the device's missing-id
  reply for that exact id (`legacy_read_clip_missing_id`, both builds) or a
  complete JAM inventory — read by the JAM track's id and confirmed to be
  that track's, never the track now at its old index — without it; then
  deleted, whatever the receipt says. Any other readback (a timeout, an
  empty-slot error for another address, another id's missing reply, a
  non-error `{id: null}` — only ever captured for a read by PATH) proves
  nothing and the outcome stays `unknown`; the receipt's refusal is kept
  as the verdict.
  `pp_session.call_tool` keeps every `WARNING:` part on `PPResponse.warnings`
  (a warning-only reply keeps them and says so); receipts list them as
  `pp_warnings`; reads ignore the advisory ones
  (`pp_session.is_advisory_warning`).
- **A slot past the last scene creates scenes** (v23b_refuse_create_past_last_scene):
  a create-clip into `t10/s13` with 9 scenes is not refused on 2.3 — Producer
  Pal silently creates s9..s13 and replies `{id, noteCount, path}`; 2.2
  refused ("no scene at"). JAM/BOOTH writes therefore append scenes first
  (create-scene `path "s+"`, v23b_create_scene_append → `{id, path: "s8"}`),
  prove it by re-reading `sceneCount`, and never target a slot past the last
  scene; an append that did not take writes no clip.
  Provenance: the recorded v23b cleanup fixtures show the PRE-fix capture run
  (`v23b_delete_scene` deletes only s8/364; `v23b_live_set_after_cleanup`
  still has 365–369); the fixed script (scene ids diffed, all six deleted)
  is covered by the fake in `tests/test_capture_script.py`, and a fresh
  capture of it is due on the next SCRATCH session — old captures are not
  edited to pretend otherwise.
- **Refusals start with "Failed" — and so do harmless warnings.** The
  captured create refusal is `[]` + `WARNING: Failed to create clip at
  t11[1|1]: track t11 (id 363) is audio; …` (v23b_refuse_create_midi_on_audio),
  while the backup-sync warning next to every good write also starts
  "Project context backup sync failed". Only the structure decides.
- **Scenes and selection by path** (v23b): read-scene / play-scene /
  delete-scene by `path "s<i>"` (play-scene replies `{playing: true, scene:
  {id, name, path}}`, which the capture REQUIRES on 2.3 before reading the
  transport back — the transport running identifies no scene); an arrangement clip selected in Live reads back as `selectedClip
  {id, path: "t10[1|1]"}`; read-clip by id of an arrangement clip carries
  `path "t10[1|1]"`, `arrangementLength`, `view: "arrangement"`.
- **Legacy forms still honoured** on 2.3 with `WARNING: param … is deprecated`
  (v23_legacy_*, legacy/, mutate/) — used only to know nothing breaks
  mid-migration.
- **Notes window unchanged.** The 2.3.0 device (`Producer_Pal (3).amxd`) still
  reads a clip's notes with `get_notes_extended(0, 128, -L, 3L)` —
  `processMidiClip` and `clipNoteScanWindow` — i.e. only `[-L, 2L)`, and its
  `noteCount` uses the same window. The Live API full-note read
  (`live_write.read_clip_live`, `clip_playback`) stays the authority.

### Not captured on 2.3.0 (fail-closed or marked)

- Take lanes (captured v23b): lanes can be written — create-clip `path
  "t10/l0[5|1]"` (+ an advisory "targeting take lane" notice); lane clips
  appear ONLY under `takeLanes[].clips` (`path "t10/l0[5|1]"`), never in
  `arrangementClips`; a duplicate to a lane needs a position (`toPath
  "t10/l1"` is an error, `"t10/l1[5|1]"` the form); promoting a lane clip to
  `"t10[9|1]"` re-creates it on the main lane (new id, advisory notice) — the
  form `promote_lane` sends; deleting a take-lane clip through the API is
  refused (`{deleted: false}` + "cannot delete take-lane clip … remove it in
  Live's UI"). `audition_in_lanes` therefore stays refused on 2.3
  (`unsupported_capability`, before any write): it could not clean up the
  lane candidates it made. `toPath 't<i>/l+'` and `takeLaneName` are gone.
- 2.2 move by `arrangementStart` and 2.2 scene creation by `sceneIndex`:
  schema forms, not captured.
- The piece replay's envelope was captured on 2.2.0-rc1 only. The 2.3 piece
  fakes (`tests/pp_fakes.PieceDevice(version="2.3.0")`) are that envelope
  reshaped by the paired-capture rules in `tests/pp_fakes.py`; the piece inside
  is the synthetic model on every build.

### What changed in 2.4.0 (all from the captures; supported since 2026-10-03)

Captures: `tests/fixtures/pp/2.4.0/{read,mutate,legacy,v23,v23b,v24}` + `tool_schemas.json`
(SCRATCH-capture.als, Live 12.4.6 — the 2.2 / 2.3 capture runs repeated on the
2.4.0 device plus the `v24` run). Where the release notes (the section this replaced) disagreed
with the device, the captures won.

- **Addressing.** 2.3's paths, unchanged (`t3`, `rt0`, `mt`, `s2`, `t3/s0`, `t3[5|1]`, lanes
  `t3/l0[5|1]`). `count` left create-track and create-scene: a batch is one path per object,
  `t+,t+` (v24_create_tracks_batch) / `s+,s+` (v24_create_scenes_batch), and replies a list; the
  deprecated `count` still works with a WARNING (v24_create_*_count_deprecated) but the sidekick
  sends schema forms only. Inserting scenes at an index is uncaptured on 2.4 and refused.
  Tool list unchanged (22 tools). create-device by `device` (`deviceName` deprecated), `d+`
  appends; `read_device_by_type_path` (`t10/afx0`) works; the sidechain param is still
  `sidechainSourceTrackId` (an audio track is accepted; a MIDI track without an instrument is
  an error "cannot be a sidechain source"; `sidechainSourceTrack` is "not found").
- **Three failure classes** (`pp_session`): a single failing target is an MCP error
  (`is_error`, `Error: …`) — a missing id (`Error: id "999999" does not exist`, the 2.3 text,
  v24_read_clip_missing_id), a missing path (`Error: nothing at path "t999"; …`), an empty
  slot (`Error: no clip at t10/s0`, v24_read_clip_empty_slot — where 2.2 replied `{id: null}`),
  create MIDI on audio and duplicate MIDI to audio (errors; 2.3 sent `[]` + WARNING), the
  lane-clip delete, a missing send return, a refused device param, a select list, a duplicate
  list mismatch. A batch fails per entry, `{id, ok: false, detail}` with no error
  (`failure_entries`; v24_read_clips_missing_batch). An include outside the schema is refused
  by the MCP client before the device sees it, `MCP error -32602: Input validation error`
  (`PPSchemaRefusal`; v24_read_track_sends — `sends` is not an include on any build).
- **Deletes.** Success is `{deletedPath, id}` with NO `deleted` / `type` keys (v23_delete_by_id
  on the 2.4 run, v24_delete_capture_tracks list). A missing id is `{detail: "nothing to
  delete", id}` — not an error, not `ok: false`, no warning (v24_delete_clip_missing_id,
  batch v24_delete_clips_batch_one_bad). The notes' "`ok:false` for a missing delete" was
  wrong. `delete_succeeded` reads all three builds; a receipt never proves absence by itself:
  `live_write._verify_clip_absent` wants the readback (the missing-id error for THIS id, or a
  complete JAM inventory) — a receipt that disagrees is kept as evidence.
- **Live API on a missing id**: `{"id": "0", "path": "", "results": [{"operation": …}]}` — no
  `result` key, no error, no text (v24_live_api_missing_id; `pp_session.live_api_missing`,
  `live_write.LiveApiObjectMissing` → `read_clip_live` marks `missing`). No 2.2 / 2.3 capture
  of that call exists.
- **create-clip into an occupied session slot REPLACES**: `{detail: "overwrote the existing
  clip at t10/s0", id, noteCount, path}` (v24_create_session_clip_replaces; 2.3 refused). The
  replaced clip's id then deletes nothing (v24_delete_clip_by_id_already_replaced). JAM / BOOTH
  read the slot first (`live_write._require_slot_free`) and refuse an occupied one; an
  `overwrote` detail after one of our writes is a hard failure (`write_failed`).
- **duplicate onto an occupied arrangement position destroys the clip there silently**: M2
  (552) at `t10[5|1]` vanished after v24_duplicate_over_existing (reply `{id: "553", path}`
  only); v24_split_clip then said 552 does not exist. No `deleted: true` in the reply — the
  notes' "duplicate overwrites report `deleted:true`" was wrong. `arrangement_exec`,
  `write_arrangement` and `promote_lane` refuse an overlapping plan / a span that holds
  anything but their own copies before any write.
- **Successful writes are minimised**: update-track, update-clip name, sends → `{id, path}`
  (v24_update_track_minimised, `_send_by_letter`, `_send_by_name`); create-clip and update-clip
  `notes` still echo `noteCount` (v24_create_arrangement_clip, v24_update_clip_notes).
  `write_failed` checks only what a build reports. **update-clip `notes` is ADDITIVE** (2 + 1 →
  `noteCount: 3`, the readback v24_read_arrangement_clip lists all three): every replace in
  `clip_mutation` clears first with `preTransforms` (uncaptured on 2.4 itself; the readback
  stays the authority).
- **Warnings that still exist**: the deprecation notices (`count`, `deviceName`, `trackIndex`,
  `slot`, `arrangementStart`, `split`) and the `ids` / `clipId` fallbacks — advisory
  (`is_advisory_warning`). One real one: `WARNING: split cut nothing …` next to a `{id, path}`
  object (v24_split_clip_deprecated_key) — `write_failed(count=2)` fails it (no split writer exists yet). No
  backup-sync / colour warning was captured on 2.4.
- **Shapes that moved**: `ppal-select {}` returns `selectedClip` (v24_selection_readback);
  playback echoes nothing (no `startTime`, play-scene's `scene` without `name`); arrangement
  clips carry `arrangementLength` without `timing`; take lanes carry `id`; a slot past the last
  scene creates scenes and says so (`created: "s11-s13"`, v24_create_clip_past_last_scene);
  lane append `{created: "l0", id, name, path}` (v24_lane_append); `duplicate_track_onto_lane`
  replies `clips[]` + `created: true` + an advisory `detail`; `ppal-connect` is `{abletonLiveVersion,
  connected, liveSet{…}, producerPalVersion: "2.4.0"}`; live-set keys `mainTrack`, `returnTracks`.
- **Lanes**: `audition_in_lanes` stays refused (the lane-clip delete is now an MCP error,
  2.4.0/v23b/v23b_delete_lane_clip); `promote_lane` uses the captured promotion
  (`detail: "promoted to the main lane by re-creating it"`).

**Moving a Set to 2.4.0.** The client pin moves when the owner moves a Set to the 2.4.0 device: (1) re-add the device in that Set (per the 2.4 release notes the install location changed; not a capture), (2) change `.mcp.json` to `producer-pal@2.4.0` and update its pin test (tests/test_profiles.py), (3) `/mcp`, then a read-only `ppal-connect` must report `producerPalVersion: "2.4.0"` before any write. No capture exists of a 2.4 client against a 2.3 device, so the pin does not move before the Set does.

### Not captured on 2.4.0 (fail-closed or marked)

- ppal-read-device of a missing id (`experiments._absent_device`: a deleted device stays
  `unknown` unless the device answers the 2.3 text for that id).
- `preTransforms` on update-clip (the clearing every replace relies on; readback-verified).
- Inserting scenes at an index (`PPRequests.create_scenes` refuses on 2.4).
- The backup-sync / colour-quantization advisories (the 2.4 device bundle was not read).
- A 2.2 / 2.3 live-api missing-id reply (only 2.4's shape is read as missing). (The 2.3
  empty-slot read was captured 2026-10-03: `2.3.0/empty_slot/` — same `{id: null}` + WARNING
  shape as 2.2, read as free.)
- The piece replay envelope is still 2.2.0-rc1 only (the 2.3 / 2.4 `PieceDevice`
  reshapes it; the piece itself is synthetic on every build).

## Fakes per version

`tests/pp_fakes.py` loads `tool_schemas.json` for the build a fake speaks. A
fake validates the raw call against that build's schemas (a 2.3 / 2.4 call
using a removed parameter fails the test), hands its handler the 2.2
equivalent (`path_request_as_v22`; the 2.4 batch appends become `count`) and
answers in that build's shapes (`v22_payload_as_v23` / `v22_payload_as_v24`;
each rule is a difference between a 2.2.0-rc1 capture and the 2.3.0 / 2.4.0
capture of the same call, tested in `tests/test_pp_fakes_versions.py`).
`CapturedDevice(version=V24)` replaces an occupied slot with the `overwrote`
detail, errors on an empty slot, replies `{deletedPath, id}` / `nothing to
delete`, and the result-less live-api shape. `@on_both_versions` (alias
`@on_all_versions`) runs a TestCase as-is (2.2.0-rc1) and as `…PP23` and
`…PP24` twins (2.3.0, 2.4.0): every twinned class in the suite runs on all
three captured builds.

## Adding a supported build

1. Run `tools/capture_pp_fixtures.py read` against that device and commit the
   sanitized read responses, tool schemas, and handshake.
2. Run `tools/capture_pp_fixtures.py mutate` only in the designated disposable
   scratch Set; commit the sanitized mutation responses and errors. For a
   path-addressed build also run `tools/capture_pp_fixtures.py v23` (new forms:
   move, split, trimmed copies, refusals, deletes), `v23b` (scenes, lanes,
   refusals), `v24` (per-target entries, replace, overwrite, minimised results,
   batches, devices, sends) and `tools/capture_pp_fixtures.py legacy` (old
   forms; its SCRATCH cleanup reads either reply shape).
3. Diff the new `tool_schemas.json` against the previous build's and extend
   `pp_session.PPRequests` (requests) and the field readers in `pp_codec`
   (responses) for every change; extend the fakes' translation rules in
   `tests/pp_fakes.py` from the paired captures.
4. Add the exact captured version to `SUPPORTED_PP_VERSIONS` last, after the
   call sites and their parametrized strict tests pass on the new build.
5. Update the Producer Pal client pin and supported-version text in
   `config/claude_mcp.md` to match the reviewed capture.

Migration-guide shapes may help prepare adapter code and synthetic expectations,
but they do not qualify a build for the mutation guard. Keep their provenance
marked as guide-derived in tests until real captures replace them.
