# composing-sidekick — Roadmap

Status legend: ✅ done · 🔶 built, waiting on the owner's gesture · 🔜 next · ⬜ planned · 💤 parked

---

## ▶ Current arc (re-dated 2026-09-24) — durable KEEP

**Outcome for the next 4–6 weeks:** more ideas that **belong in the song**, not more
features. Capability surface is already large (gears, ears, audio-refs, rituals,
taste, arrangement). Full SOMBRA panel, `core`/`server` split, stem pipeline, and
new generators stay **parked**.

**Where the first window landed (ended ~2026-09-10): 0 verified scored nights.**
No night closed with a definitive verified rate in the whole window — the loop's
plumbing was being finished, not run. The 2026-09-02 night is settled by owner
attestation only (5 identity-less rows, 2 keeps by his word): it never entered,
and can never enter, the verified rate. The next window is the first that can
actually produce verified nights end to end.

### Scoreboard (from `session_report()` → `scoreboard`, 2026-09-24)

| Stratum | Sessions | Judged | Keeps | keep_rate |
|---------|----------|--------|-------|-----------|
| verified (definitive) | 0 | 0 | 0 | — |
| attested (owner word) | 0 recorded | 0 | 0 | — |

Nothing has been verified yet; the attested 09-02 night still needs its
attestation recorded through `close_session(attest=…, confirm=True)` to appear
in its own stratum. **Freeze gates on verified numbers only.**

### FEATURE FREEZE — lifted for one arc (2026-09-25)

> The freeze (**≥8 verified judged suggestions across ≥3 definitive sessions**,
> `session_report()` → `freeze.met`) is **lifted by the owner for the
> reference-guided development arc only** (`.scratch/batch4/PLAN.md`): propose
> and audition one reference-supported section change from his current piece,
> stated direction and selected precedents; preserve the original, remember his
> reaction, verify what survives. Everything else stays under the freeze. The
> durable-KEEP scoreboard remains the score; arrangement experiments are
> reported as a **separate subtotal** until their scoring is validated on real
> nights.

### KEEP definition

| Rank | Signal | Role |
|------|--------|------|
| 1 | Immediate KEEP (`judge_answer`) | Diagnostic + taste learning only |
| 2 | **Session survival** | **Primary score:** still in the set at session end |
| 3 | Next-session survival | Stronger confirmation (slower) |
| 4 | Section adoption | JAM → real song section (stretch) |

**Primary rate:** session-surviving suggestions ÷ all **judged** suggestions.

### Anti-gaming / measurement rules

- Every written candidate gets a **terminal state**: `keep` | `drop` | `abandoned` |
  `unjudged`. Unjudged must not inflate KEEP rate.
- Stratify by **source × role × gear** (catalogue / ref-assisted × lead / bass / pad /
  drums × gear). Aggregate alone hides where improvement happened.
- **Audit hardening (2026-09-05):** verified writes carry Set/clip identity;
  `close_session()` reads Session and Arrangement inventory. Partial or ambiguous
  evidence remains unresolved and retryable. Revisions preserve one suggestion
  and its judged snapshot. Next-session survival and verified adoption are tracked;
  all five improvements and the scratch MIDI, Live API, and physical bridge
  gates are complete. The previously deferred musical trial has resumed at
  preparation only: no musical material auditioned or judged yet. Next is to
  verify the current Set and choose the intended material and listening focus.
  Validation is recorded in
  [`docs/AUDIT_FIXES_2026-09.md`](AUDIT_FIXES_2026-09.md).
- Ears live vs blind = **data-quality context**, not a success metric.
- Ref-assisted vs catalogue-only: **alternating sessions or seeded A/B** — not casual
  side-by-side (selection bias).
- Project-scoped taste stays gated until enough per-project judgments exist. Start
  with **global taste + project context**.

### Say-no rule

> A task enters this arc only if it improves candidate selection, closes the judgment
> loop, measures durable musical adoption, or removes friction from that exact loop.

**Listener update (2026-09-05):** Session Ears, audio references, and Mix Witness
default to `gemini-3.8-flash-medium` through AGY.

### Thin session spine (support only)

`hear_the_room` → `answer_my_loop` (gear 2–3) → terminal judgment → grow / continue /
arrange. Details: [`docs/CURSOR_CHAT_PLAYBOOK.md`](../CURSOR_CHAT_PLAYBOOK.md).
If the Moment lists `unheard` audio in the current playing context, ask what those takes carry
before structure advice (named-but-unheard Stage A). To actually listen, the
opt-in session-ears pass (`inventory_session_audio` → `listen_to_takes`,
Stage B shipped 2026-08-16 — see `docs/SESSION_EARS.md`) turns takes into
validated Gemini take-cards that fold into structure context.

Baseline collection starts **after** the sweep landed (2026-07-30) — before that the
stamp existed with nothing to total, and earlier still there was no record at all.

**Bridge ownership hygiene (2026-08-23).** Task 4 on `feat/audit-improvements`:
removed shadow duplicate taste globals from `core.py`; `taste_bridge` owns live
taste state; purity guard enforces the explicit bridge-owned name list. Not a
core-split milestone — full `core`/`server` split and new generators remain
parked per the say-no rule above.

### Ref-steered composing (2026-07-30)

A reference can now steer retrieval of **his own** material:
`answer_my_loop(ref_id=…)` for a lead, `beat_from_ref(ref_id=…)` for a beat built
from his real drum clips. Tempo narrows a window, `density` gates, `register` biases
the octave; modal centre and swing stay prose. The **room wins** where they disagree
and a reference never refuses — it narrows and announces.

**Known resolution limit (measured, not estimated):** ~8 distinct answers per seed
loop across 60 refs; re-rolling a seed gives 5. His briefs only emit two density
values on ~30% of refs and the tempo window accepts half/double time, so the fields
simply don't discriminate further. Steering is real but coarse — do not expect the
KEEP rate to separate ref-assisted from catalogue-only on small n.

---

## ✅ Era 1 — Extraction & corpus foundation
- `.als` parser (gzip-XML, drum-aware, **clip-source-tagged**: arrangement / session /
  take_lane / other_track) · catalog scan · local materialization → `store/projects/`
- **260 his-confirmed projects · ~406k learning notes** (19 pack/demo projects
  blocklisted 2026-07-03 — packs were 17% of note mass; signatures held, the identity
  was real) · phrase index **25,605 phrases** (take-lane feeds retrieval only, never
  the style profile)

## ✅ Era 2 — Learn & describe the style
- `store/STYLE.md` + `style_profile.json`: per-role melodic grammar, groove grids,
  key/register/density · music21 harmony (i↔iv home, iv↔VI signature, phrygian i→II) ·
  structure templates · two embeddings (melody 44-d, drums 96-d)
- **Velocity/timing DNA** (2026-07-02): per-role velocity contours + micro-timing —
  lead: mean 63/std 32, downbeat-accented, **56% off-grid (~25 ms)**. The old
  generator's flat 76/68-on-grid output was discarding half his feel.

## ✅ Era 3 — Generation, arrangement, taste (the plausible-but-not-keepable era)
- `compose_from_profile` (scale-locked sketches) · `arrange_song`/`write_arrangement`
  (energy arc, buildups/drops/reprises) · taste model (Bradley-Terry on his best/worst
  ratings; **14 pairs**, anti-static, awaiting more ear data) · IdeaPack workflow
- The owner's blind listening pivoted everything: generated leads were judged plausible
  but not keepable → **generator demoted to critic**.

## ✅ Era 4 — Ear-first: compose from HIS material (2026-07-02, "the long night")
- **Gate fixes**: gold-anchor semantics hardened · `drums`→`drum` alias · fractional
  Producer-Pal position parser (live reads of HUMAN-played clips were broken)
- **`answer_my_loop`** — call-and-response from his catalog only (gold ∪ real leads ∪
  take-lane pool; `is_mechanical` junk floor as retrieval FILTER — 2,301 of 13,462
  gated live), cosine-vs-seed, minimal transposition, voice-led join, velocities
  preserved. Auto-writes ONLY to the **SIDEKICK JAM** track. Verified in Live.
- **Listening Booth** staged in his Set: 18 blind pairs (BOOTH A/B) + 12-lead audition
  + 5 reclaimed rolls (BOOTH REJECTS). ✅ Pairs judged 2026-07-03 (15 votes via the
  voting page — see Era 5). 🔶 Still open: the 12-lead solo audition + first golds.
- **`humanize_notes`** (DNA post-pass, deterministic, identity on missing stats) ·
  **owner-feel groove templates** (10 .mids) 🔶 awaiting one-time Extract Groove(s)
- **Catalog digs**: automation harvest (1,149 envelopes/133 projects — his energy
  language: volume 562 · sends 142 · filter 60) → **phrase-curve library** (501
  gestures → 141 templates w/ provenance) · comping archive (305 winners / 622 pairs)
- **Falsifications (pre-registered gates, both honored)**: comping ≠ melodic taste
  (fit 0.42) · feel features failed overall (0.61/0.69) — exploratory: reduction-winner
  subgroup separates 0.84/0.84, future hypothesis only. Nothing polluted the model.
- **Envelope bridge** (phrasing engine): curves + writer + v8 device forged
  (`(local Max device folder)`) 🔶 💤 parked as capability — probe verdict
  `v8_required`; not the headline (his brief: bandmate, not fader-rider)
- **Ears investigation**: third-party server NO-GO (undisableable eval); Audio Tap
  **device-only GO** — nothing installed
- **NOTES.md is real**: the owner's ear-confirmed feel and researched north stars; dead
  dials removed

## ▶ Era 5 — SOMBRA: spark · map · finish (the active plan)
- ✅ **P1 THE MAP**: `plan_song_map` + `lay_the_song` — full-song section maps in his
  measured proportions as draggable LOCATORS (commit fills fresh tracks on his word).
  Ear-corrected same night: section lengths now *sample* his histogram (4s/8s/16s +
  fill cells), breakdown fires on crossing. Live on his timeline.
- ✅ **LLM knowledge layer**: repo skills — **`sombra-brain`** (idiom/theory/diagnosis,
  measured facts) + **`challenge-deck`** (30 original cards incl. a PERSONAL suit
  mined from his corpus; apply-don't-recite)
- ✅ **P2 THE FINISHER** (complete 2026-07-03): **gearbox 1–6** (`gears.py`: verbatim
  RECALL · RECONTEXT · DEVELOP w/ named motif surgeries · GROW · COMPOSE/COVER as
  critic-gated contracts carrying **director axes**; flat dynamics auto-reject;
  silence legal) · **`whats_missing`** (`finisher.py`: 12 named dramaturg detectors →
  TYPED interventions with executable commands; real verdicts: one project = 15-min
  loop-with-ambitions, another = arranged w/ 205 stacked takes) · **KEEP/DROP with
  teeth** (KEEP golds the source; DROP logs why + jam-gated delete) · **all five
  rituals** to the coach's exact names (`rituals.py`; tracked decisions ledger;
  graveyard in NOTES.md; buried projects stay organ donors) · **direction axes v1**
  (218 projects on darker/club/sparse dials; COVER cites his pole exemplars)
- ✅ **The morning the ear arrived** (2026-07-03): booth **voting page** (blind,
  keyboard, provenance-after) → 15 votes → **falsification #3: no scorer tracks his
  ear** (all ≤0.47; metrics stay side tools; temperature-knob idea dead) · **pack
  purge**: he spotted demo MIDI → 19 projects blocklisted (`store/not_his_projects.json`,
  his adjudication) → clean rebuild (packs were 17% of note mass; **signatures held**)
  · 7 clean votes → **taste model retrained on the clean votes** (jumpiness turned
  positive) · **SIDEKICK_BRIDGE.amxd v2 forged from Live's own
  template — loaded, ping-verified** (phrasing engine awake, still parked as capability)
- 💤 **P3 SOMBRA device** (parked for the durable-KEEP arc): transport/prototype
  shipped (OSC listener, presence lamp, dispatch, bridge). Full one-chassis / three-tab
  panel is **not** the 4–6 week goal — see Current arc above. Protocol:
  `docs/SOMBRA_DEVICE.md`.
- 🔶 **Ears layer built (default OFF)** — hear_the_room/reset_ears live; promotion
  waits on the pre-registered ears-vs-current Booth round (`tools/build_ears_booth.py`).
  **Hands batch 1 built** (develop default-usable via MCP; develop_ab blind round
  pending; promotion = 3 consecutive qualifying rounds — `tools/build_develop_booth.py`).
  Everything gated on flipping that default (unfixed refusal-shape callers, Booth
  operational caveats, the un-wired deal surface) is tracked in `docs/EARS_PROMOTION.md`
  — read it before flipping `store/ears_default.json`.
- ⬜ MAP extras: energy knob · section reroll · scenes→song · blocklist round 2
  (candidate pack names — the owner's call)

## 💤 Parked (alive, waiting for their moment)
- Full SOMBRA one-chassis panel (transport shipped; panel not the durable-KEEP goal)
- Phrasing engine live half (bridge drag + first swell) · take-lane judging round-trip ·
  oblique-card transforms w/ dynamics inversion · Extensions SDK panel (beta watch) ·
  Max MIDI Tools clip-editor transforms · the single pipeline (booth champion → track) ·
  feel-model reduction-subgroup hypothesis (needs its own pre-registered gate) ·
  audio ears via Audio Tap device-only · influence-blend (#29, audio→MIDI) ·
  Demucs stem pipeline · `core.py`/`server.py` architecture split · new generators

## 💤 Future gated arc — Mix Adviser (not a promise)

**Does not displace durable KEEP.** Mix trials use a **separate ledger** — they must not
contaminate the composing-session KEEP denominator. Operator doc: [`docs/MIX_ADVISOR.md`](../MIX_ADVISOR.md).

| Stage | Gate | Scope |
|-------|------|--------|
| **A — MVP (current build)** | None — manual exports only | DSP measurements, ≤3 bounded automatic `gain_db` proposals (manual apply; `pan` parked pending evidence), optional sandboxed listener witness, loudness-matched randomized-label A/B ledger. No scalar mix score. No auto-mixing. |
| **B — listener compare** | ≥15 judged passes across ≥5 sessions **and** usefulness gate passes | Compare DSP-only vs optional listeners (Gemini/`agy`, gpt-audio when actually available) with hallucination/consistency gates. |
| **C — export friction** | Stage B + measured export bottleneck | Master-first M4L Tap probe, then aligned group taps. |
| **D — mixer writeback** | PP mixer surface/readback/undo probe passes | Confirm-gated snapshot / write / readback / rollback. |

**Parked for this arc:** streaming listen, auto-mixing, VST control, Demucs, solo-pass
pseudo-stems, scalar mix score, continuous listening.

**Musical metric:** judged suggestion KEEP/survival and whether the owner would have tried it unaided.
**Technical metrics:** valid proposals, A/B completion rate, listener honesty rating,
latency. Component branches: Composer (orchestration), Grok (DSP), Luna (trials) →
integration MCP wiring.

### Durable-KEEP architecture sequence

This is an implementation sequence under the current arc, not a capability expansion:

1. **Wave 0 — complete:** Mix Adviser Stage A is closed and canonical at
   `577239c`; Stages B–D remain gated exactly by the table above.
2. **Wave 1 — complete:** Luna baseline: fresh-path locking, case-insensitive Ears direction
   admission, MCP-note normalization at Ears gates, and truthful ledger summary (`fc0f13c`).
3. **Wave 2 — complete:** parallel components: Composer JAM suggestion lifecycle
   module (`mcp_server/jam_lifecycle.py`) across six write paths and two judge paths; Grok pure Producer Pal
   codec (`mcp_server/pp_codec.py`) plus transport/session adapter (`mcp_server/pp_session.py`).
4. **Wave 3 — complete:** integration & 10-point migration landed on trunk (`ca35c68`):
   - **PP 2.1 migration**: JSON transport + `midi-json` short-key notation, fail-closed verification (`require_verified_note_count`, `require_scene_count`), version handshake, `.mcp.json` pinned to `producer-pal@2.1.0`.
   - **Data integrity**: atomic writes (`tempfile.mkstemp` + `os.replace`) across all store writers; `meta.schema_version` block on store artifacts (`style_profile`, `phrase_index`, `harmony`, `structure`, `motifs`); stale taste model detection (`ratings_sha256` / round count).
   - **Roles & config**: `mcp_server/config.py` centralized constants with provenance; versioned `rules_v1` role classifier with `clip_role_report()`; role field stamped on candidate rating rounds.
   - **Listener models**: defaults bumped to `gemini-3.7-flash-medium` in `tools/audio_ref.py` and `tools/mix_witness.py`.
5. **Wave 4 — deferred Ears:** redesign Ears admission only when the Ears promotion
   arc resumes; do not use this sequence to flip the default or broaden gates.
6. **Wave 5 — deferred Mix B:** start Mix Adviser Stage B only after at least 15
   judged passes across at least 5 sessions and the documented usefulness gate passes.

Runbook: [`docs/agents/durable-keep-architecture/README.md`](agents/durable-keep-architecture/README.md).

---
Resume cold: `HANDOFF.md` (personal session log, not published) · current arc: top of this file · playbook: [`docs/CURSOR_CHAT_PLAYBOOK.md`](../CURSOR_CHAT_PLAYBOOK.md)
