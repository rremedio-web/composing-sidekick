# Architecture

How composing-sidekick is put together: the pipeline that learns a catalogue, the MCP
server that serves it, the transport that talks to Ableton Live, and the stores that
remember what happened. Current as of 2026-10-14. The earlier map this replaces is kept
at [`history/ARCHITECTURE_2026-08.md`](history/ARCHITECTURE_2026-08.md).

## 1. System overview

```
your Live Sets (.als)                         Ableton Live + Producer Pal device
        │ ingest/                                        ▲   │
        ▼                                                │   ▼  MCP over HTTP (localhost:3350)
store/projects/*.json ──analyze/──► store/*.json ──► mcp_server/server.py ◄── MCP client (Claude, Codex, …)
   (materialized MIDI)   (profile, index,          (FastMCP tools, profiles)       the composer
                          harmony, structure,            │
                          motifs, directions)            ▼
                                               store/session_ledger.json, ratings, pieces,
                                               audio_refs/, session_ears/  (what happened)
```

Three rules shape every module:

1. **The composer's material first.** Retrieval and transformation of real phrases
   precede invention; the seeded generator is a critic, never the headline.
2. **Writes are name-locked and verified.** Automatic writes land only on tracks named
   `SIDEKICK JAM` (answers) and `SIDEKICK BOOTH` (blind staging, append-only). Every
   write is read back; a mismatch raises. Everything else is a preview or a
   confirm-gated handoff.
3. **Judgment is closed, not assumed.** Every suggestion written to Live is stamped in
   the session ledger and ends in a terminal state (`keep` / `drop` / `abandoned` /
   `unjudged`). The score is *session survival* ÷ judged suggestions.

## 2. Pipeline (`ingest/`, `analyze/`, `tools/`)

| Stage | Module | Reads → writes |
|---|---|---|
| Scan | `ingest/scan_catalog.py` | Catalogue roots (`SIDEKICK_CATALOG_ROOTS`, `--roots`) → `store/catalog_manifest.json` |
| Parse | `ingest/parse_als.py` | gzip-XML `.als` → tracks, clips, notes; drum-aware; every clip tagged with its source (`arrangement` / `session` / `take_lane` / `other_track`) |
| Materialize | `ingest/materialize.py` | manifest → `store/projects/<id>.json` + index (one slow drive pass, then local) |
| Harmony | `analyze/harmony.py` | music21 chord / Roman analysis → `store/harmony.json` |
| Structure | `analyze/structure.py` | section lengths, energy curves, breakdowns → `store/structure.json` |
| Style profile | `analyze/style_profile.py` | per-role melodic grammar, groove grids, velocity/timing feel → `store/style_profile.json` and the human-readable `store/STYLE.md` (appends `store/NOTES.md`, which it never rewrites) |
| Phrase index | `analyze/build_index.py` | phrases with role, project and clip-source provenance, two embeddings (melody 44-d, drums 96-d) → `store/phrase_index.json` |
| Motifs | `analyze/motifs.py`, `motif_scan.py` | recurring cells → `store/motifs.json` |
| Roles | `analyze/roles.py` | versioned heuristic role classifier (`rules_v1`): register and polyphony against catalogue baselines |
| Digs | `tools/harvest_arrangement_dna.py`, `build_phrase_curves.py`, `build_groove_templates.py`, `build_directions.py` | automation envelopes → gesture library; groove `.mid` templates; direction axes (darker / clubbier / sparser / weirder) |
| Rebuild | `tools/rebuild_corpus.py` | scan → materialize → analyze with fail-closed gates and backup/restore |

Take-lane phrases feed retrieval only, never the style profile. A blocklist
(`store/not_his_projects.json`) removes pack and demo Sets from every stage.

### Store artifacts carry a `meta` block

Every generated artifact (`style_profile`, `phrase_index`, `harmony`, `structure`,
`motifs`) has a top-level `meta` with `schema_version`, `built_at`, `code_rev` and
corpus counts. `core._load_artifact` validates it: a schema mismatch or a missing block
raises with the regeneration instruction (strict mode; the legacy tolerance exists only
for migration). `corpus_keys.py` gives phrases stable identities across rebuilds and
reports a stale corpus (older than six weeks, or the open Set missing from the manifest).

## 3. MCP server (`mcp_server/`)

`server.py` is the only FastMCP surface: thin wrappers that forward to the modules
below, plus a `--selftest` entry. The selftest itself lives in `selftest.py`: an
offline suite of titled blocks that must end with `=== all selftests passed ===`. It
needs the built catalogue; without `store/style_profile.json` and
`store/phrase_index.json` it prints one line and exits with status 2. `profiles.py` declares which tools each
`SIDEKICK_MCP_PROFILE` exposes (`compose` 63, `session` 37, `reference` 18, `debug`
19, `all` 77) and the instructions the client receives.

| Area | Modules | Role |
|---|---|---|
| Knowledge and retrieval | `core.py`, `core_shared.py`, `config.py` | Style aggregation, `find_phrases`, compose briefs, arrangement templates, atomic store helpers; `config.py` centralises constants with provenance (`PP_URL`, beats per bar, role and density thresholds) |
| Taste | `taste.py`, `taste_bridge.py` | Bradley-Terry preference model from pairwise votes; the bridge is the sole owner of ratings, gold phrases and stale-model detection (`core` imports it as a facade, guarded against re-binding) |
| Gears and growth | `gears.py`, `develop_session.py`, `ref_develop.py`, `idiom_cells.py`, `curves.py`, `feel.py` | `answer_my_loop` gears 1–6 (recall · recontext · develop · grow · compose · cover), motif surgeries, sticky develop, reference-shaped proposals built from idiom cells (neutral defaults; `store/calibration.json` overrides them) |
| Ears | `moment.py`, `ears.py`, `session_ears.py`, `capture_loop.py`, `capture_support.py` | `hear_the_room` (the Moment: tempo, key, roles, `unheard` / `unread` / `hidden` clips), admission gates, opt-in listening of audio takes, loop capture onto `SIDEKICK EARS` |
| Finishing | `finisher.py`, `rituals.py`, `song_structure.py`, `arrangement_exec.py` | `whats_missing` detectors → typed interventions; `lay_the_song` locators; the rituals; arrangement execution with overlap refusal |
| Writing to Live | `live_write.py`, `jam_lifecycle.py`, `clip_mutation.py`, `clip_playback.py`, `lane_audition.py`, `envelope_bridge.py` | Name-locked JAM/BOOTH writes with read-back, arrangement writes, take-lane audition and promotion, the suggestion lifecycle (stamp → judge → terminal state), the parked envelope bridge |
| Transport | `pp_session.py`, `pp_codec.py` | Operation-scoped Producer Pal sessions and the pure wire codec (section 4) |
| Judgment | `ledger.py`, `experiments.py`, `piece_context.py`, `piece_continuity.py`, `piece_snapshot.py` | Session ledger, arrangement experiments and effect trials, piece ↔ Set identity, proposals / reactions / observations, Set snapshots and digests |
| Mix | `mix_contracts.py`, `mix_trials.py` (+ `tools/mix_check.py`, `mix_dsp.py`, `mix_witness.py`) | Offline Mix Adviser: contracts, separate trial ledger, DSP, optional witness |
| Device | `sombra_listener.py`, `device_readout.py` (+ `tools/m4l/`) | OSC listener for the SOMBRA pedal/device, presence heartbeat, readout contract |
| Export | `export_bridge.py` | Standard MIDI File writing (the one `mido` dependency) |

Rule of the module universe: `core`, `live_write`, `gears`, `finisher` and `rituals`
self-alias in `sys.modules` so `core` and `mcp_server.core` are one module. Removing that
forks caches.

## 4. Producer Pal transport

Live is reached only through the Producer Pal device's MCP endpoint. The contract:

- **Operation-scoped sessions.** `pp_session.operation_session()` opens a fresh
  transport, initialises MCP, runs `ppal-connect`, validates the handshake (JSON output,
  `midi-json` notation, a build in `SUPPORTED_PP_VERSIONS`) and only then yields. No
  mutation can start on a bridge speaking the wrong wire contract.
- **One request builder per build.** The handshake names the build's addressing
  (`2.2`: numeric indices; `2.3` / `2.4`: positions in the object path, batch appends).
  `PPRequests` / `requests_for(session)` emit that build's argument forms; no call site
  builds locator dicts itself.
- **Pure codec.** `pp_codec.py` (stdlib only) encodes notes as `{p, s, d, v}`
  `midi-json`, parses bar|beat tokens including Producer Pal's fractional timing, extracts
  balanced JSON from mixed replies and classifies clips (`heard`, `unread`, `empty`,
  `unheard_audio`, `unheard_unknown`).
- **Fail closed.** `call_tool` raises on `isError`; `require_verified_note_count` and
  `require_scene_count` raise `LiveWriteVerificationError` rather than defaulting;
  `write_failed` judges a reply by its structure, never by warning text; absence of a clip
  is proven only by Live's own missing-id reply for that id or a complete inventory.
- **Captured, not assumed.** Every supported build has read, mutate and legacy reply
  envelopes captured from the device under `tests/fixtures/pp/<build>/`; the musical
  payload inside them is a synthetic piece model. `tests/pp_fakes.py` validates each call
  against that build's `tool_schemas.json` and answers in its shapes. Uncaptured behaviour is refused
  (`unsupported_capability`) or left `unknown`. Details and the per-build differences:
  [`PRODUCER_PAL_VERSIONS.md`](PRODUCER_PAL_VERSIONS.md).

Known traps the captures taught: a create-clip into an occupied session slot
**replaces** it on 2.4 (the writers read the slot first); a duplicate onto an occupied
arrangement span destroys the clip there silently (arrangement writers refuse
overlaps); `update-clip notes` is additive (every replace clears first); the device
reads a clip's notes only inside `[-L, 2L)` of its length, so the Live API read is the
authority for long clips; Live recycles object ids within a run.

## 5. Data flow of a session

1. `hear_the_room()` → the Moment (and `ledger_status`: unswept nights, pending
   judgments) — nothing is generated before the loop state is read.
2. A serving tool (`answer_my_loop`, `find_phrases`, …) with `use_ears=True` gates its
   candidates to the room (tempo world, key, register, density, role); a reference
   (`ref_id`) narrows *which of the composer's* phrases, never re-keys the loop.
3. `jam_lifecycle` writes to `SIDEKICK JAM`, reads it back, and stamps the ledger with
   Set and clip identity, source, role, gear and ears context.
4. `judge_answer` records KEEP or DROP in the composer's words (DROP deletes JAM clips
   only, confirm-gated, absence verified).
5. `close_session()` inventories Session and Arrangement, resolves identities, assigns
   terminal states and reports survival by source × role × gear. Unresolved evidence
   stays unresolved; it is never turned into a drop.

Arrangement experiments (material already on SIDEKICK tracks) and effect trials
(devices added to the composer's tracks) use their own rows and subtotal
(`register_arrangement_candidates` → `judge_answer(entry_id=…)` → `close_session`).
Mix trials use a separate ledger and never enter the composing denominator.

## 6. Stores

| Store | Owner | Contents |
|---|---|---|
| `store/STYLE.md`, `style_profile.json`, `harmony.json`, `structure.json`, `motifs.json`, `phrase_index.json`, `directions.json`, `automation.json`, `phrase_curves.json` | pipeline | Generated; regenerate, never hand-edit |
| `store/NOTES.md` | the composer | Hand-written notes; read directly by assistants and appended into `STYLE.md` at build time |
| `store/ratings.json`, `taste_model.json`, `tools/ideas/*_manifest.json`, `*_scores.json` | taste tools | Votes and the model trained on them; the model records the ratings hash and goes stale (weight 0) when they diverge |
| `store/session_ledger.json` | `ledger.py` | Every JAM write, its judgment and terminal state; the durable-KEEP evidence |
| `store/rituals.json`, `scoreboard.json`, `ears_default.json` | rituals / promotion | Bury/marry decisions; the pre-registered promotion rule; the one flag that turns ears on by default |
| `store/pieces.json`, `piece_records.json`, `piece_decisions.json` | piece tools | Piece ↔ Set bindings confirmed by the owner; statements, proposals, reactions, observations with provenance |
| `store/audio_refs/<ref_id>/`, `audio_ref_index.json` | `tools/audio_ref.py` | Reference cards (`card.json`, `describe.txt`, `features.json`, `ref.mid`, `meta.json` overlays) and the search index |
| `store/session_ears/<session>/` | `session_ears.py` | Take cards bound to Set identity |
| `store/mix_trial_ledger.json` | `mix_trials.py` | Mix judgments, separate from composing |
| `store/exports/` | `export_midi` | MIDI files; paths outside it need explicit opt-in |
| `store/calibration.json`, `store/approved_rhythms.json` + `assets/approved_rhythms/` | the composer (optional, gitignored) | Engine calibration measured on your catalogue; your own rhythm library. Absent on a clean clone; the code uses neutral defaults and tolerates a missing library |

Every store write goes through the atomic house pattern: `tempfile.mkstemp` in the
target's directory, write, `os.replace`; the temp file is unlinked on any failure.
Ledgers add `fcntl` locking. The selftest snapshots every top-level store document
before running and fails if one changed.

## 7. Listening and references

The sidekick never hears audio itself. Two opt-in paths call an external listener CLI
(`agy`, default model `gemini-3.8-flash-medium`) in a sandbox with a minimal
environment, staging only a plain-PCM copy of the exact region:

- **Audio references** (`tools/audio_ref.py`): a local file or URL → `describe.txt`
  listening brief with Do/Don't rules, `features.json` (DSP: loudness, band shares,
  onsets, change points), `ref.mid` (Basic Pitch, weak on drums), indexed for
  `find_audio_refs`. Cards are model analyses; the owner's overlay (`note`, `verdict`,
  suspect flags) outranks them and derived moments inherit a parent's suspect flag.
- **Session ears** (`session_ears.py`): inventory of audio takes in the open Set through
  Producer Pal (no paths cross the MCP boundary) → single-use take capabilities → proof-
  checked listening (the listener's conversation record must show it opened that file)
  → cards with honest nulls, persisted under the verified Set identity.

Measured DSP facts are kept apart from model-heard claims, which are kept apart from the
owner's words; the knowledge guide spells out that authority order.

## 8. House rules for changes

- Seeded randomness wherever a machine makes notes; a one-sentence explanation wherever
  a model does.
- Unproven signals never fold into the critics or the taste model; experiments run
  behind pre-registered gates and are archived when they fail.
- Producer Pal output schemas drift between releases: find tracks by name, count them
  through the overview, never trust a field two releases deep, and add a build only from
  captures.
- New selftest blocks isolate ratings and the cache with `try`/`finally`, assert
  invariants rather than index coincidences (rebuilds reshuffle neighbourhoods), and pass
  `use_ears=False` explicitly so a promoted default can never reach for the bridge
  mid-suite.
- Scripts keep `__main__` guards; the automation harvest aborts rather than silently
  gutting its artifact when more than a fifth of the catalogue is unreachable.
- Keep the operator docs in step with the code: several tests read
  `config/claude_mcp.md`, the playbook and the Producer Pal versions doc and assert the
  tool table and contract sentences.
