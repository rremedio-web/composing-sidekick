# Register the style-sidekick MCP server

The server supplies the owner's style context, real phrases, arrangement templates,
scoring, deterministic sketch generation, and MIDI export. **The MCP client (Claude,
Codex, Cursor, Gemini…) is the composer**: the tools provide material, checks, and
Ableton handoff.

## Prerequisites

Build the local corpus artifacts first (one-time, or when the catalog changes) —
the full order is in [README → Build your catalogue](../README.md#build-your-catalogue):

```bash
cd ~/composing-sidekick
.venv/bin/python tools/rebuild_corpus.py --commit   # scan → materialize → analyze
```

Install MCP server deps (already in `.venv` if you ran setup):

```bash
cd ~/composing-sidekick
.venv/bin/pip install -r requirements.txt
```

Verify tools without a live MCP client:

```bash
.venv/bin/python mcp_server/server.py --selftest
```

## Claude Code (`.mcp.json`)

Add to `.mcp.json` at the repo root (project-local) or `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "style-sidekick": {
      "command": "<repo>/.venv/bin/python",
      "args": ["<repo>/mcp_server/server.py"],
      "env": {
        "SIDEKICK_MCP_PROFILE": "compose"
      }
    },
    "producer-pal": {
      "command": "npx",
      "args": ["-y", "producer-pal@2.3.0", "-f", "json", "-n", "midi-json"]
    }
  }
}
```

Replace `<repo>` with the absolute path of your clone (MCP hosts do not expand `~`).
Restart Claude Code / Cursor after editing.

Producer Pal mutations are guarded against the captured device builds listed by
`mcp_server.pp_session.SUPPORTED_PP_VERSIONS`: `2.2.0-rc1`, `2.3.0` and `2.4.0`.
The sidekick picks each build's argument forms from the `ppal-connect`
handshake (2.3 and 2.4 put positions in the path, `t3[5|1]`; 2.4 appends in
batches, `t+,t+` / `s+,s+`, `count` having left its schemas), so all three work
at once. Keep the client pin aligned with the device the Set runs:
`producer-pal@2.2.0-rc1` for a 2.2.0-rc1 device, `producer-pal@2.3.0` for a
2.3.0 device (the committed pin), `producer-pal@2.4.0` for a 2.4.0 device
(the SCRATCH capture ran it). The pin moves when the owner moves a Set to the
2.4.0 device: (1) re-add the device in that Set (per the 2.4 release notes the install location changed; not a capture), (2) change `.mcp.json` to `producer-pal@2.4.0` and update its pin test (tests/test_profiles.py), (3) `/mcp`, then a read-only `ppal-connect` must report `producerPalVersion: "2.4.0"` before any write. No capture exists of a 2.4 client against a 2.3 device, so the pin does not move before the Set does. Other
builds are refused until their read and mutation fixtures have been captured
and reviewed (docs/PRODUCER_PAL_VERSIONS.md). On 2.3 and 2.4,
`audition_in_lanes` refuses (lanes can be written, but a take-lane clip
cannot be deleted through the API, so an audition could not clean up);
`promote_lane` uses the captured lane promotion (the clip is re-created on the
main lane with a new id). On 2.4 a create-clip into an occupied slot REPLACES
the clip and a copy dropped on an occupied arrangement position destroys the
clip there silently. So: the writers into an existing slot (JAM, BOOTH,
`write_midi_clip`) read it first and write only when the read proves it empty
(a captured empty-slot shape for that slot — anything unclear refuses); the
arrangement writers refuse an overlapping plan and a destination span holding
anything but their own copies (`arrange_song` execution, `promote_lane`);
writers onto a track they just created (`write_tracks`, `write_arrangement`,
the lane audition track) rely on that track's creation readback; the handoffs
that never execute (`send_to_ableton`, `prepare_idea_pack_audition_lanes`)
emit a guard read before each create, and their instruction says to run the
create only when that read proves the slot empty / span free. Single-target refusals are MCP errors, a batch fails per
entry (`ok: false`), an include outside the schema is refused by the client
(`-32602`). `send_to_ableton` takes `addressing` = the version `ppal-connect`
reports (e.g. `"2.4.0"`) or its generation (`"2.3"` / `"2.4"`); the default is 2.2.

### Server Profiles (`SIDEKICK_MCP_PROFILE`)

The server uses declarative profiles to manage tool surface size and cognitive load:

| Profile | Tools | Focus |
|---|---|---|
| `compose` (default) | 63 | Core composing loop: hearing, generation, development, take-lane audition/promotion, reference application (`apply_audio_ref`, `beat_from_ref`), judging, and session closeout |
| `session` | 37 | Live Set interaction: hearing, audio take inventory/listen, clip inspection, auditioning, judgment, and closeout |
| `reference` | 18 | External audio reference toolkit: analysis, clipping, batch processing, reindexing, and jam-track writes |
| `debug` | 19 | Diagnostic inspection, envelope probing, and Offline Mix Adviser tools (`analyze_mix_pass`, etc.) |
| `all` | 77 | The complete registered MCP tool surface |

Set `"SIDEKICK_MCP_PROFILE": "all"` in `.mcp.json` if you need the full tool suite in a single session.

## Claude Desktop (`claude_desktop_config.json`)

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "style-sidekick": {
      "command": "<repo>/.venv/bin/python",
      "args": ["<repo>/mcp_server/server.py"],
      "env": {
        "SIDEKICK_MCP_PROFILE": "compose"
      }
    }
  }
}
```

Merge with any existing `mcpServers` entries — do not replace the whole file.

## Ears (Live session)

Room-aware serving is **opt-in per call** until Booth promotion flips the default.
`store/ears_default.json` is absent ⇒ default OFF.

1. While Live is up, call `hear_the_room()` first — returns `{moment, status}`.
2. Pass `use_ears=True` on serving tools when you want picks gated to the room:
   `find_phrases`, `answer_my_loop`, `audition_leads`, `suggest_growth`,
   `grow_my_loop`, `develop_my_loop`.
3. Optional `direction` (`darker`, `brighter`, `sparser`, `weirder`, `clubbier`, …)
   bends admission gates when ears are on.
4. `reset_ears()` clears this session's keep/reject demotions.
5. **Named-but-unheard audio.** `moment.unheard` lists audio (or ambiguous) clips in the current playing context — shape
   `{track, track_index, slot, kind: "audio"|"unknown", clip_name}`. MIDI notes
   still populate `moment.roles`. The sidekick does **not** hear those audio
   takes: acknowledge them and **ask the owner what they are doing** before
   structure advice. Do not infer musical content from the name. Explicit Arrangement bars are also supported. To actually listen (opt-in; Stage B
   shipped 2026-08-16): `inventory_session_audio()` → `listen_to_takes(inventory_id,
   take_ids, focus=…)` — listener take-cards with honest nulls, operator contract
   in `docs/SESSION_EARS.md`. Status line may say `N clips unheard` / `N clips unread` /
   `no midi heard` — never track names on the chassis line.
6. **Unread and hidden MIDI.** `moment.unread` lists MIDI clips whose notes were not
   read — `{track, track_index, slot, clip_name, coverage, reason}`: Producer Pal
   returns notes only in `[-L, 2L)` of a clip, so with the Live API off a clip whose
   loop brace lies outside that window is unread (turn on `ppal-live-api`); with it
   on, only a clip whose own Live API read failed is. Unread is never "empty": say
   the notes are unknown. `moment.hidden` lists heard clips with notes past the loop
   brace that do not play — `{track, track_index, slot, clip_name, n_hidden,
   hidden_bars, loop_end_beats}`. `where="arrangement"` reads up to 32 regular
   tracks by default (`ARRANGEMENT_READ_MAX_TRACKS`; Session reads 12).

Without `use_ears=True` (and with no promoted default), tools behave as before —
byte-identical retrieval and answers.

## Tools exposed

`ref` means the `reference` profile; `all` means every profile.

| Tool | Profile(s) | Purpose |
|------|------------|---------|
| `get_style` | `compose`, `ref`, `debug` | Full `STYLE.md` + structured facts (keys, tempo, groove, harmony, arrangement) |
| `find_phrases` | `compose` | Retrieve real phrase notes (seeded similarity or diverse exemplars) |
| `hear_the_room` | `all` | Read the room via Producer Pal: tempo/key, MIDI roles in actual playing Session/Arrangement context, plus `unheard` cards for audio/ambiguous clips, `unread` cards for MIDI clips whose notes were not read, and `hidden` cards for notes behind a loop brace. Arrangement reads 32 regular tracks by default. Ask what unheard material is doing — do not invent content from names. Blind when Live is unreachable |
| `inventory_session_audio` | `compose`, `session`, `debug` | Opt-in session-ears pass, step 1: discover audio takes in current Live Set via Producer Pal and return capability envelope (`inventory_id`, sanitized take descriptors — no paths). Fail-closed without Live: `live_inventory_unavailable` |
| `listen_to_takes` | `compose`, `session`, `debug` | Opt-in session-ears pass, step 2: consume `inventory_id` capability and selected `take_ids` → validated listener take-cards (role_guess, vibe, register, density, pitch_claims — null when uncertain). The owner picks `focus=full\|groove\|melody\|composition`. Single-use capability; cards persist atomically under `store/session_ears/<session_id>/` |
| `capture_loop` | `compose`, `session` | Preview, or with `confirm=True` record, what is playing into an empty `SIDEKICK EARS` slot (`surface=auto` prefers a validated Producer Pal Live API capability, else SIDEKICK BRIDGE); `listen=True` hands the take to `listen_to_takes` |
| `reset_ears` | `compose`, `session`, `debug` | Clear this session's keep/reject demotions (the "reset ears" gesture) |
| `suggest_structure` | `compose` | Section skeleton from your arrangement templates |
| `develop_idea` | `compose` | Turn a seed phrase into theme/variation transforms |
| `arrange_song` | `compose` | Lay loop parts across a full-song timeline with placements |
| `audition_leads` | `compose`, `session` | Generate interval-band sampled lead candidates for listening tests |
| `rate_candidates` | `compose`, `session` | Record best/worst candidate rounds; `contrast_axis` derives rating scope and trainable model |
| `suggest_growth` | `compose` | Suggest rare-but-native melody/harmony moves as structured candidates |
| `grow_my_loop` | `compose` | Extend a seed loop with contextual growth candidates |
| `answer_my_loop` | `compose` | Answer a loop on `SIDEKICK JAM` using his phrase library, optional audio reference steering (`ref_id`), and room-aware transposition |
| `develop_my_loop` | `compose` | Generate variations and evolutionary transforms of an active loop on SIDEKICK tracks |
| `lay_the_song` | `compose` | Lay loop sections into full timeline arrangements with locators in Live |
| `audition_in_lanes` | `compose`, `session` | Stage multiple candidate variations into Live take lanes for blind listening |
| `promote_lane` | `compose`, `session` | Pick winning lane from take-lane audition, copy to active clip, and log durable KEEP |
| `three_endings` | `compose` | Generate 3 contrasting ending gestures (fade/hit/hang) |
| `bury_or_marry` | `compose` | Decision ritual to either commit to an idea or bury it to avoid endless fiddling |
| `version_courage` | `session` | Version-family comparison across saved project members to choose what to keep or develop |
| `one_hour_close` | `compose`, `session` | Time-boxed session winding-down ritual ensuring clean closure |
| `confirm_bounce` | `compose`, `session` | Verify an exported audio bounce and record confirmation requested by the owner |
| `declare_finishing_state` | `compose`, `session` | Record `complete_draft` only; acceptance and release states are owner CLI only |
| `sombra_status` | `compose`, `session`, `debug` | Query active SOMBRA Max for Live device state, connection, and parameters |
| `no_new_notes` | `compose`, `session` | Session constraint ritual: work only with existing recorded notes |
| `sticky_develop` | `compose`, `session` | Develop ideas using sticky thematic constraints |
| `judge_answer` | `compose`, `session`, `ref` | Record human judgment on a generated answer (`keep` or `drop`) |
| `whats_missing` | `compose` | Analyze current arrangement/roles and identify missing textural/frequency functions. `project="live"` reads the Set open in Ableton (arrangement clips, take lanes as debris, `silent_gap` / `unison_wall`). Note-based findings over bars whose MIDI notes were not read (unread clips) are withheld into `suppressed` with a reason, never reported as facts; an ending over an unread clip ships no `three_endings` seed notes |
| `probe_envelope_surface` | `debug` | Diagnostic probe of clip parameter and volume envelopes |
| `phrase_envelopes` | `debug` | Extract and inspect velocity/timing curves for phrases |
| `assign_groove` | `compose` | Apply catalog-extracted groove template to candidate notes |
| `read_clip` | `compose`, `session`, `debug` | Read a Producer Pal session slot or arrangement clip into sidekick note shape |
| `read_current_clip` | `compose`, `session`, `debug` | Resolve Producer Pal's current selection and read selected clip/slot |
| `create_idea_pack_from_clip` | `compose`, `session` | Build reusable IdeaPack from notes without writing to Ableton |
| `create_idea_pack_from_live_clip` | `compose`, `session` | Read a live clip, then build an IdeaPack without writing |
| `prepare_idea_pack_audition_lanes` | `compose`, `session` | Confirm-gated Producer Pal handoff for IdeaPack audition lanes |
| `score_phrase` | `compose`, `session`, `debug` | Score a candidate vs your profile (0–1 + verdict; optional role/key) |
| `critique` | `compose`, `debug` | Rich critique using same optional role/key contract as scoring |
| `continue_clip` | `compose` | Analyze a seed and return a continuation brief |
| `finish_my_idea` | `compose` | Bundle seed critique, continuation brief, and structure |
| `rate` | `compose`, `session`, `ref` | Rate a single clip or phrase (1-5 or keep/drop) and update taste ledger |
| `get_ratings` | `compose`, `session`, `debug` | View historical rating ledger summary and candidate statistics |
| `clear_rejects` | `compose`, `session`, `ref` | Undo DROP suppression so a phrase can be served again: `phrase_id` clears one, omit it to clear all; append-only record; `confirm=False` previews |
| `get_gold_phrases` | `compose` | Retrieve phrases curated as gold exemplars |
| `compose_brief` | `compose` | One-shot context bundle for composing |
| `compose_from_profile` | `compose` | Deterministically generate pad/bass/lead/drums from the profile |
| `humanize` | `compose` | Apply microtiming and velocity humanization based on catalog distributions |
| `export_midi` | `compose`, `ref` | Write notes to `store/exports` by default; outside paths require opt-in |
| `send_to_ableton` | `compose`, `ref` | Build Producer Pal MCP payload for live Ableton write-back (handoff only) |
| `write_midi_clip` | `compose`, `session` | Direct verified write of MIDI notes to a named clip on SIDEKICK tracks |
| `write_composed_to_jam` | `compose` | Gear 5/6 material: `validate_composed` gates it, then a verified, ledger-stamped write to SIDEKICK JAM (`confirm=False` previews) |
| `surgery_clip` | `compose`, `session` | Bounded, surgical note edit on a specific bar of an existing clip |
| `analyze_mix_pass` | `debug` | Offline local master + 2–4 aligned groups → DSP evidence and optional listener witness; no Live/Producer Pal writes |
| `propose_mix_moves` | `debug` | Read-only evidence-based proposal of ≤3 automatic `gain_db` moves; master excluded from stem peers |
| `prepare_mix_ab` | `debug` | Prepare local loudness-matched randomized-label A/B manifest; public pre-judgment cards omit mapping |
| `judge_mix_pass` | `debug` | Record randomized-label A/B/`same` human judgment and optional witness/KEEP context in separate mix ledger |
| `mix_trial_report` | `debug` | Read-only separate mix-trial summary with no scalar mix score |
| `analyze_audio_ref` | `ref` | Analyze local audio file or YouTube URL → `describe.txt` (Gemini listening brief), `features.json`, `ref.mid`; pick `focus=full\|groove\|harmony\|melody` (take-cards use `full\|groove\|melody\|composition` instead) |
| `find_audio_refs` | `compose`, `ref` | Search filed audio refs by query / focus / tag / project while composing |
| `tag_audio_ref` | `ref` | Tag filed ref, link `project=` (creative name — not Ableton set), and/or set the owner `note=` + `verdict=keep\|drop` |
| `clip_audio_ref_moments` | `ref` | Cut jam-sized mini-refs from parent `# Moments` timestamps (no Gemini) |
| `analyze_audio_refs_batch` | `ref` | Batch-analyze many URLs/files sequentially with resume |
| `reindex_audio_refs` | `ref` | Rebuild `store/audio_ref_index.json` from `store/audio_refs/*/` on disk (also refreshes parsed `structure`) |
| `restore_audio_ref_ownership` | `ref` | Restore the operator ownership registry (`tools/data/audio_ref_ownership.json`) into the audio-ref index and metadata by exact `ref_id`; refuses a partial or mismatched library |
| `apply_audio_ref` | `compose`, `ref` | Load filed ref into jam-ready handoff (imitation rules + role-filtered `ref.mid` notes); `write=True` lands on SIDEKICK JAM only |
| `beat_from_ref` | `compose`, `ref` | Beat from **his own drum clips**, matched to reference's written drum grid. Nothing generated; Basic Pitch not involved |
| `close_session` | `compose`, `session`, `ref` | **End of night** — inventory this Set across Session/Arrangement, resolve identities, report survival by source × role × gear. Partial/ambiguous evidence remains unresolved. The `arrangement` / `effect_trials` lines publish their rate only with every verdict resolved (`rate_status`, `coverage`); `kept` on an effect trial is presence, never audible ducking |
| `session_report` | `all` | Ledger outcomes and unresolved coverage; `refresh=True` gathers next-session evidence from Live |
| `adopt_answer` | `compose`, `session` | Link a suggestion to a verified destination clip in the song |
| `register_arrangement_candidates` | `compose`, `session` | Register already-written arrangement experiments (SIDEKICK line variants, a whole SIDEKICK drum track) and effect trials (devices added to his tracks) as **recovered** judgment rows: journal or `items`, exact Set identity, every id re-read from the Set (clip content digest, placement, mutes; device params). Effect evidence must bind (track/device/parent identity) or the candidate is refused. Preview by default; `confirm=True` writes rows, never Live. The same content as a closed row (another Live run, or new ids) is `observed` on that row, never a second judged row. Judged with `judge_answer(entry_id=…)`, closed by `close_session` in their own subtotal |
| `declare_piece_version` | `compose`, `session` | Bind a Set (exact `.als` path) to a **piece** on the owner's confirmation — never inferred from a similar filename; one piece per Set. Preview by default, `confirm=True` writes `store/pieces.json` (`confirmed_by: owner`). Piece-level records then resolve for every confirmed version; Set-level facts stay bound to the exact Set |
| `record_proposal` | `compose`, `session` | File one proposal → `proposal_id`: the musical question, his source gesture (clip/phrase/cell ids), the reference and its JOB (`{ref, job, evidence}`), what stays, what varied, candidate handles in that Set, seeds. Refuses a Set the owner has not confirmed. `register_arrangement_candidates` items may carry the `proposal_id`; the ledger row stores it |
| `record_reaction` | `compose`, `session` | His words verbatim with a scope (`section` bars / `track` / `candidate` label / `piece` / `global`); `interpretation` is the model's reading stored apart and never promoted; `rule=True` only when he states a rule. Never a KEEP/DROP — verdicts stay with `judge_answer` |
| `record_observation` | `compose`, `session` | A Set change seen on a read (track deleted/muted, clip moved/added, device removed) with `evidence.snapshot_digest`; bound to the exact Set, "observed, unjudged", never a verdict |
| `piece_brief` | `compose`, `session` | Read-only brief for this Set: piece + confirmed versions; owner intent (piece-level, version overrides, `inherited` named); his statements and model-heard/measured observations in separate blocks (provenance, date, bounce/Set); the notes' "Suggestions" and "Cuts and transitions" tables with status; open questions; proposals with reactions; observations; latest snapshot (cached digest, or one live read with `live=True`); references with jobs; prose. Never contacts Live by default; never authorises a write |

Owner acceptance or release declarations are made in the owner's terminal with
`.venv/bin/python tools/declare_finishing.py <project> composition_accepted|release_ready [--note NOTE]`.
A piece seed (its confirmed Set versions, the owner's statements, model-heard /
measured facts and the owner's reactions — all provenance-tagged) is an owner-run
migration from a JSON spec, never an import-time write:
`.venv/bin/python tools/declare_piece.py --spec my_piece.json` prints the plan;
`--confirm` writes `store/pieces.json` and `store/piece_records.json` (idempotent;
`--versions 2,4,5` narrows). Format: `tools/piece_spec.example.json`; piece notes
template: `docs/pieces/EXAMPLE.md`.

## Offline Mix Adviser (only when the owner asks)

Follow [`docs/MIX_ADVISOR.md`](../docs/MIX_ADVISOR.md): manual local WAV/AIFF/FLAC
exports only, then `analyze_mix_pass` → `propose_mix_moves` → manually apply any
bounded moves → re-export → `prepare_mix_ab` → the owner judges randomized-label A/B
without asking the operating agent to reconstruct the mapping; record the choice with
`judge_mix_pass` → `mix_trial_report`. Measurements and witness text are evidence or
hypotheses, not taste. `listener=False` is the default; `listener=True` explicitly
invokes sandboxed optional `agy` and may send audio according to that account/tool.
The tools never change Live or Producer Pal, and the separate
`store/mix_trial_ledger.json` is not the composing-session KEEP denominator.

## The judgment ledger (durable KEEP)

Every JAM write is stamped automatically into `store/session_ledger.json` (local and
gitignored, like `ratings.json`) with session, source (`catalogue` / `ref:<id>`),
role, gear and ears. `judge_answer` attaches the immediate verdict.

`close_session()` requires the intended Set to be open and a complete inventory
of Session and Arrangement clips. Runtime clip identity follows moves within the
same Live incarnation. Reopened Sets use conservative content/context matching;
explicit adoption links account for copied material. Missing legacy identity,
ambiguous matches, and partial reads remain unresolved and retryable.

| his verdict | verified in this Set | state | counted |
|---|---|---|---|
| KEEP | yes | `keep` | numerator |
| KEEP | no | `drop` | denominator |
| DROP | either | `drop` | denominator |
| — | no | `abandoned` | excluded, reported beside |
| — | yes | `unjudged` | excluded, reported beside |

A definitive rate is withheld while judged outcomes are unresolved. Revisions
retain one suggestion entry and preserve its original judgment. Confirmed DROP
requires the recorded Set, track and clip IDs, current JAM ownership, and verified
absence afterward. Replaying a judgment does not repeat learning; preview then
confirm can still execute its pending deletion.

### Arrangement experiments and effect trials (separate subtotal)

Material the sidekick already put in his Set on SIDEKICK tracks, and devices it
added to his tracks, are judged through their own rows —
`register_arrangement_candidates` → `judge_answer(verdict, entry_id=<id or
label>, reason=…)` → `close_session()`. Rows live under
`arrangement_experiments` / `effect_trials` in the ledger, never in `entries`,
so they never enter the phrase KEEP denominator; `session_report` /
`close_session` show `arrangement` (survived ÷ resolved judged) and
`effect_trials` as separate lines. A DROP here never bans, un-golds or re-rates
a phrase; the feedback lands in `piece_decisions` as model-level
`arrangement_feedback` (owner records outrank it). Selecting one variant judges
that one only.

| evidence at close (arrangement row) | KEEP | DROP | no verdict |
|---|---|---|---|
| identity mismatch / incomplete inventory / clip read failed / partial presence / content changed | `unknown` (unresolved, retried) | `drop` | `unknown` |
| all clips present, unchanged, audible (track, its groups and the clip unmuted) | `adopted` — by why: `on_his_originals` (now on one of his tracks; the stronger class), `unmuted_since_registration`, `still_audible_on_sidekick` (was audible when registered and still is: unchanged, no action of his observed) | `drop` | unjudged (still pending) |
| all present, unchanged, silent (track or clip muted) | `staged_kept` | `drop` | unjudged (still pending) |
| every clip gone / track gone | `not_adopted` | `drop` | `abandoned` |

**Rate and coverage.** `survived = adopted + staged_kept`;
`resolved_judged = adopted + staged_kept + not_adopted + drop`; `judged_total`
= every explicit verdict, resolved or not; `coverage = {resolved_judged,
judged_total}`. The rate (`survival_rate` / `kept_rate`) is published only when
every verdict is resolved (`rate_status: definitive`); while any judged row is
unresolved it is `null` with `rate_status: partial` and a `warning` naming the
coverage — read the counts, not a percentage, and never turn an unknown into a
drop. Under 8 judgments the definitive rate carries the phrase rate's "too
thin" caution; nothing judged means undefined, not 0.

**Adoption by identity, never by name.** `adopted_by` counts the three reasons
apart — an `adopted` on the SIDEKICK track means "still audible, unchanged",
not that he carried it into his song. Which class applies is decided by track
identity: the track the sidekick put the material on (`origin_track`, this Live
run) and any track the sidekick is known to have written on (the other rows'
origin tracks, the JAM/BOOTH tracks of this run's phrase writes) keep the
SIDEKICK classes — renaming it is reported as `sidekick_track_renamed`, moving
onto another sidekick track as `on_sidekick_track`; `on_his_originals` is the
rest (a row registered on his own track — a copy he made — is his where it
sits), and never across Live runs. A name never *grants* the stronger class:
without a created-tracks record, a `SIDEKICK…`-named destination outside the
ledger's provenance is read as the sidekick's (`on_sidekick_named_track`). The
remaining gap: a sidekick-created track that was never registered and carries
another name would read as his — keep sidekick tracks named `SIDEKICK…`.

**Unknown, restart, and the same material again.** Unknown rows re-resolve on
**any** later `close_session()` that reads their Set — a plain close re-runs the
experiment sweep for every session of that Set holding pending rows, even when
the phrase night is already sealed (or name it: `close_session(session_id=…)`);
the result says `experiments_closed`. Only the sessions of the open Set are ever
swept or totalled; another Set's night is never touched. Ids count only inside
the Live run that issued them: after a restart (new runtime epoch) a row is
matched by content digest on the track with its registered name — one match
rebinds, none or several stay unknown, never `not_adopted`. **Observation vs
round:** for a closed row whose material **survived** (`adopted` /
`staged_kept` / `kept`), registering the same content again — in another Live
run, or under new ids in this one — is `observed`: a `survival_observations`
record on that row (next-session presence, with epoch/track/ids/digest, recorded
once; a repeat says `already_recorded`; the preview says "would be recorded as an
observation on"), not a second judged row — the material is already in the
score; changed content, or a copy on **his** track, is a new round
(`new_round_of` with `differs: content` / `copy_on_his_track`). For a row that
did **not** survive (`abandoned`, `not_adopted`, `drop`, `removed`) *any*
re-proposal — even the identical clip — is a new judgeable round (`differs:
re-proposed_after_<state>`): he can KEEP tonight what he dropped or lost last
time. Totals never count an old unchanged-round child (`excluded_unchanged_rounds`
names it, with `parent_in_report`) — not beside its parent and not alone.

**Effect trials — bound evidence, kept ≠ heard.** `kept` (device still on his
track after KEEP), `removed` (gone after KEEP), `drop`, `abandoned`, `unknown`.
**Gone is proven, never inferred:** the track's bound list lacks the id *and*
Live itself says that id does not exist — only Live's own "id … does not exist"
for that id proves absence, full stop. A timed-out, warning-only, truncated,
empty or null read stays `unknown` (`device_read_failed`; no read at all is
`device_unread`) — a degraded read is never Live's word; on Producer Pal 2.2,
whose does-not-exist text for devices is uncaptured, a deleted device stays
unknown (conservative). Otherwise the row is `unknown` with the reason —
`device_moved:<where>` when the id still reads as *this* device (same type and
name) on another track, a return, the main track or inside a rack (it exists,
so it is neither removed nor kept), `id_recycled:<path>` when the id now names
a clip, an untyped object, or another device (other type or name) — and
`evidence_mismatch:<what>` when the evidence contradicts itself: the track's
device list counts only when the reply is that track (`track_id` /
`track_path`), a listed device must sit under the track's path (`device_path`)
with its registered type (`device_type`), and a by-id read must name that very
device (`device_id`, `device_parent`, `listing`) — a contradiction refuses
registration and leaves a close unknown, never a row under the requested id.
Parameters are a **separate fact** from presence: `params_coverage`
(full / partial / none — what the read could see; a dict-shaped or empty
`parameters` is `none` with `params_error`, never `{}` passed off as read),
`params_compared` (true only when every device was read in full at registration
and at close), `params_changed` / `params_uncompared`; the line counts
`kept_params_unchanged` / `kept_params_changed` / `kept_params_uncompared` apart.
**`kept` means the device is present — never that ducking works or is audible**:
with the sidechain source unset a compressor ducks nothing; audibility needs its
own routing/listening evidence. Placement and mutes are observations beside
the content digest, not part of it.

### Piece continuity (piece ≠ Set)

A **piece** (say, NOCTURNE) is not a Set file (`NOCTURNEv5.als`). A Set
belongs to a piece only when the owner confirmed it (`declare_piece_version(…,
confirm=True)` or the owner-run `tools/declare_piece.py --confirm`); a similar
filename never binds. `store/pieces.json` holds `{piece_id, name, notes_path,
set_keys: [{set_key, confirmed_by: owner, ts, via}]}`. With that binding
`current_decisions(set_key)` resolves the direction fields (goal / borrow /
preserve / depart / ref_id) across the piece's confirmed versions — an owner
record anywhere outranks a model record anywhere, this Set's own records
outrank inherited ones, then the latest revision — and names each `inherited`
field's source version. Track ids, proposal ids, verdicts, reasons and the
`revision` never cross Sets: a stale clip id never resolves under a new Set
and no piece record authorises a cross-Set mutation.

`store/piece_records.json` holds the rest, every record with `source ∈ owner |
model_heard | measured | set_file | inference` and `quote` (his words verbatim
when owner): **statements** (owner = piece-level; model-heard / measured =
dated and bound to the bounce/Set they came from; read as two blocks, never
merged), **proposals** (`record_proposal`), **reactions** (`record_reaction`)
and **observations** (`record_observation`). `piece_brief(set_path)` reads all
of it plus the notes file's status tables; it is the session's starting point
and never a write. Verdicts come only from his words through `judge_answer`;
a reaction, an observation or a brief never creates one.

## Audio references (external refs → composing fuel)

**You (the composer / agent) choose `focus`** — do not invent one for the owner.
`full` | `groove` | `harmony` | `melody`; freeform `prompt_extra` when he gives a note.
(Session take cards from `listen_to_takes` use `composition` in place of `harmony`.)

`analyze_audio_ref` turns a local audio file or YouTube URL into a folder under
`store/audio_refs/<stem>_<timestamp>/` (gitignored): `source.wav`, `features.json`,
`describe.txt` (structured Gemini brief via `agy`), `ref.mid` (Basic Pitch), `meta.json`.
Optionally clip with `start`/`end` (seconds or `mm:ss`). Re-describe without re-fetch via
`from_dir`. Does NOT write to Ableton.

Index: successful describes upsert `store/audio_ref_index.json`. Search with
`find_audio_refs`; pin with `tag_audio_ref(project="<name you choose>")` — a creative
**project** label (album / arc / WIP identity), not the Ableton `.als` set name. After Live
apply, add `tag_audio_ref(note="…", verdict="keep"|"drop")` so find/apply prefer *his*
overlay. `reindex` / CLI `--enrich-structure` parses feel BPM / swing / density / mode from
existing `describe.txt` into `structure` (no re-listen). `clip_audio_ref_moments` cuts
30–60s mini-refs from `# Moments` for jam handoff. Dump many URLs with
`analyze_audio_refs_batch` (sequential, unique `batch_id`, composite resume keys).

**Reference Live hour:** `hear_the_room` → `find_audio_refs(project="<your label>")` →
`apply_audio_ref` → KEEP/DROP (terminal judgment) → `tag_audio_ref(note=…, verdict=…)`.
Score **session survival**, not the KEEP click alone — see
`docs/CURSOR_CHAT_PLAYBOOK.md` and `docs/history/ROADMAP_2026-09.md` → Current arc.

Typical flow: ask the owner for focus → `analyze_audio_ref(url, focus=...)` → while on a
track, `find_audio_refs(query=...)` → `apply_audio_ref(ref_id, role="bass"|"lead"|…)` for
imitation rules + role-filtered MIDI → `apply_audio_ref(..., write=True)` lands on
**SIDEKICK JAM only**. If `ref.mid` is weak (common for drums), compose from the describe
brief / `ricardo_note` instead.

CLI: `.venv/bin/python tools/audio_ref.py <path-or-url> [--start 0:48 --end 1:10]
[--focus groove] [--from-dir …] [--batch refs.json | --reindex | --enrich-structure |
--find q | --find-verdict keep | --apply ref_id | --note-ref id --note "…" --verdict keep |
--clip-moments ref_id]`

## Example workflow (in Claude)

1. `compose_brief("moody 5 minute track in D minor")` — gather context
2. Compose note lists yourself from the returned phrases + structure
3. `score_phrase(notes=[...])` — self-check fit
4. `export_midi(notes=[...], path="sketch.mid", tempo=120)` — writes `store/exports/sketch.mid`
5. Or `send_to_ableton(notes=[...], track=0, clip_name="sketch")` — live handoff (below)

`export_midi` writes only under `store/exports`, plus any directories you list in
the `SIDEKICK_EXPORT_DIRS` environment variable (separated by `:`) in the server's MCP
config. The model cannot widen that list: there is no tool argument for it. Paths are
resolved with `realpath` (symlinks and `..` cannot step outside), the name must end in
`.mid` or `.midi`, and an existing file is replaced only if it is already a MIDI file.

## Minimal IdeaPack workflow

The live read path is intentionally explicit and does not auto-write:

1. `read_current_clip()` or `read_clip(track_index=0, scene_index=0)` reads notes from Producer Pal.
2. `create_idea_pack_from_clip(seed_notes=read["notes"], key="D minor")` builds the pack.
3. Read `pack["contrast_axis"]` and `pack["rating_contract"]`. Scope is derived from the controlled contrast, not from user introspection.
4. `prepare_idea_pack_audition_lanes(pack, confirm=false)` previews the lane plan. Without `start_track`, this stays untargeted.
5. Re-run with `confirm=true, start_track=<explicit track>` only when the target tracks/scenes are correct. This still returns handoff calls; it does not execute them.

`create_idea_pack_from_live_clip()` combines steps 1 and 2. It uses Producer Pal read APIs only.

Rating rule: pass `contrast_axis=pack["contrast_axis"]` to `rate_candidates`.
`lead` trains `lead_taste`; `harmony` is preserved for a future harmony model;
`mixed`/`groove`/`arrangement` are stored but do not train the current lead model.

## Writing to Live through Producer Pal

`send_to_ableton()` does **not** talk to Ableton. It translates your note list into
the exact **Producer Pal** MCP tool-call payload(s) Claude should run next.

### Manual prerequisites (live Ableton only)

1. **Install Producer Pal** — download `Producer_Pal.amxd` from
   [producer-pal.org](https://producer-pal.org/installation) or
   [GitHub releases](https://github.com/adamjmurray/producer-pal/releases) and drop it
   onto a MIDI track in Ableton Live.
2. **Run Ableton** with the device loaded (Producer Pal starts its local MCP server).
3. **Register Producer Pal** in your MCP config alongside style-sidekick (pinned to the device's build — 2.2.0-rc1 below, `producer-pal@2.3.0` for a 2.3.0 device, `producer-pal@2.4.0` for a 2.4.0 device — with JSON flags), e.g.:

```json
{
  "mcpServers": {
    "style-sidekick": {
      "command": "<repo>/.venv/bin/python",
      "args": ["<repo>/mcp_server/server.py"]
    },
    "producer-pal": {
      "command": "npx",
      "args": ["-y", "producer-pal@2.2.0-rc1", "-f", "json", "-n", "midi-json"]
    }
  }
}
```

See [Producer Pal installation](https://producer-pal.org/installation) for Claude Desktop
extension / device-specific setup.

### Handoff workflow

1. Compose and `score_phrase` as usual in style-sidekick.
2. Call `send_to_ableton(notes=[...], track=0, clip_name="sidekick sketch", tempo=120)`.
3. Read the returned `instruction` and `tool_calls` array.
4. On the **Producer Pal** MCP connection (not style-sidekick):
   - `ppal-connect` (no arguments)
   - Then each `tool_calls` entry in order — typically `ppal-update-live-set` (tempo) then
     `ppal-create-clip` with the translated notes payload.

### Translation & wire details

The transport speaks Producer Pal 2.1+ structured JSON output and `midi-json` short-key notation via `mcp_server/pp_codec.py`:

| style-sidekick | Producer Pal `midi-json` (`ppal-create-clip`) | Legacy bar\|beat |
|---|---|---|
| `pitch` (MIDI 0–127) | `p` integer (e.g. `60`) | Pitch name (`C4`, `F#3`, …) |
| `start_beats` (0-based quarters from clip start) | `s` float beats (omitted if default `0.0`) | `bar\|beat` (1-indexed, e.g. `1\|1`, `1\|2`) |
| `duration_beats` (quarter-note units) | `d` float beats (omitted if default `0.25`) | Duration token (`n/8`, `n/4`, `1bar`, …) |
| `velocity` | `v` integer 1–127 (omitted if default `80`) | `v{1-127}` prefix per note |

Docs: [producer-pal.org/features](https://producer-pal.org/features) — tools
`ppal-create-clip`, `ppal-update-live-set`, `ppal-connect`.

Default placement is **Session** slot `trackIndex/0` (scene 0). Pass `track=<index>`
to target a MIDI track. Pass `arrangement_start="17|1"` plus `bar_length=<bars>` for
Arrangement placement; `arrange_song` already emits `arrangement_start_bar_beat`,
`length_bars`, and per-role placements for this path.

Read and verification contract (`mcp_server/pp_session.py`):

- **Operation-scoped session**: `pp_session.operation_session` opens a fresh `streamablehttp_client` transport, initializes MCP, runs `ppal-connect`, and captures version metadata (`handshake_from_connect`).
- **Fail-closed verification**: `require_verified_note_count` verifies read-back note count matches sent note count exactly, raising `LiveWriteVerificationError` on mismatch. `require_scene_count` fails closed if `sceneCount` cannot be read (never defaults to 0).
- **Tool error handling**: `call_tool` checks `CallToolResult.isError` and fails loud instead of treating error text as empty notes.
- **Clip classification**: `classify_session_clip` evaluates `heard`, `unread`, `empty`, `unheard_audio`, and `unheard_unknown` from structured response objects (`unread`: a MIDI clip returned without notes while its played region lies outside Producer Pal's `[-L, 2L)` note window — its notes are unknown, never empty).
- `read_current_clip()` composes `ppal-select` + `ppal-read-clip`.

Live arrangement write-back is implemented in `mcp_server/live_write.py` and suggests/verifies
every written role with Producer Pal read-back. Auto-writes are restricted strictly to `SIDEKICK JAM` and `SIDEKICK BOOTH`.

## SIDEKICK BRIDGE (envelope writer) — one-time install

Producer Pal cannot write automation; the bridge can (Live 12.2+ `v8` + LiveAPI:
`Clip.create_automation_envelope` → `insert_step`, session clips only). It is a dumb
execute-ops device — delete it from the track to disable everything.

1. In Live, on the **SIDEKICK JAM** track, create a new **Max MIDI Effect** and open it.
2. Add three objects wired in a row:
   `[udpreceive 3351]` → `[v8 sidekick_bridge.js]` → `[udpsend 127.0.0.1 3352]`
3. In the v8 object's editor, paste the contents of `tools/m4l/sidekick_bridge.js`
   (or place the file next to the device and let v8 load it by name). Save the device
   as **SIDEKICK_BRIDGE.amxd**.
4. Verify from the repo: `probe_envelope_surface(confirm=True)` — or skip the escape
   hatch entirely; the bridge is the expected surface.

Alternative surface: Producer Pal's **ppal-live-api** escape hatch (device Setup tab →
enable Live API access). `probe_envelope_surface` reports whether it supports function
calls; if not (expected), the v8 bridge is required for envelopes + groove assignment.

Safety: every op batch begins with `assert_track_name(track, "SIDEKICK JAM")` executed
inside the device, and the device itself refuses `create_envelope`, `clear_envelopes`,
`set_clip_groove` and `clear_clip_groove` for any clip id whose clip is not on a track
named `SIDEKICK` or `SIDEKICK …`. Undo per clip: `clear_envelopes`; global undo: delete
the track or the device.

Network: the bridge has no authentication. Anything that can send UDP to port 3351 on
this machine can run its ops. The listening address is set by `[udpreceive]` in your
patcher, not by the bridge script, and nothing in this repo limits it to localhost, so
treat it as reachable from the network. **Do not load the device on a shared or
untrusted network.**

### Session Ears capture surfaces

`capture_loop` records only to one existing audio track named exactly
`SIDEKICK EARS` and only into an existing empty Session slot. Its default preview
is read-only; pass `confirm=True` to record. Omitting `source_scene_index` records
what is already playing, while supplying an index explicitly launches that scene.

Surface selection is capability-based:

- Producer Pal 2.1, an unknown/malformed version, or a failed tools list uses
  `SIDEKICK BRIDGE` when `surface="auto"`.
- Producer Pal 2.2+ uses Live API only when `ppal-connect` reports a valid version
  and MCP `tools/list` explicitly contains `ppal-live-api`.
- A successful object-path lookup is never capability evidence.
- `surface="live_api"` fails without that evidence; `surface="bridge"` is an
  explicit override.

Both surfaces reread the target, restore the prior Ears arm state, validate the
recorded audio under the current project's `Samples/Recorded` area, and register
only an opaque capture ID. Raw project/audio paths and internal inventory tokens
do not cross the MCP response boundary. `listen=True` runs Session Ears listening
only after local validation.

The repository's automated tests use hostile fakes. They do not prove Max task
timing, real Live recording/finalization, or Producer Pal RC behavior. Release
status remains **NO-GO** until the user-present Ableton scratch-set gate in the
implementation plan passes.
