# Cursor Chat Playbook — Composing Sidekick

Despite its historical filename, this is the shared playbook for every assistant.
Read the [knowledge guide](KNOWLEDGE_GUIDE.md) alongside it: storage, retrieval,
authority, corrections and handoffs. Read `store/NOTES.md` and the current piece's
registered notes directly; generated summaries can be stale or incomplete.

## Read this when

- the owner asks for an answer, develop, finish, booth, ears, or anything in Live
- Not for pure repo engineering (use `docs/ARCHITECTURE.md`; past agent runbooks are in `docs/history/agents/`)

## Current arc — durable KEEP (2026-07-30)

Optimize for ideas that **survive the session**, not KEEP-button rate. Full strategy:
[`docs/history/ROADMAP_2026-09.md`](history/ROADMAP_2026-09.md) → **Current arc** (historical).

| Signal | Use |
|--------|-----|
| Immediate KEEP/DROP | Taste learning + fast feedback |
| **Session survival** | **Primary score** (still in set at end) |
| Next-session survival | Confirmation (record when possible) |
| Section adoption | JAM → real section (stretch) |

`close_session()` assigns a **terminal state** when identity and inventory evidence resolve the candidate. Missing identity, ambiguous matches, or incomplete reads remain unresolved:

| his verdict | verified in this Set | state | in the rate? |
|-------------|--------------|-------|--------------|
| KEEP | yes | `keep` | numerator |
| KEEP | no | `drop` | denominator — he liked it, then removed it |
| DROP | either | `drop` | denominator (deleting is optional) |
| — | no | `abandoned` | **excluded**, reported beside |
| — | yes | `unjudged` | **excluded**, reported beside |
| late (after the sweep sealed the night) | either | scored verdict-less | **excluded** from the verified rate; reported in `late_verdicts` |
| owner-attested (identity-less rows) | attested present/adopted/absent | via attestation | **own stratum** — never mixed with verified numbers |
| adopted (whole/contained copy found elsewhere) | copy present | `adopted` / kept-in-JAM copy | counted as survival of the original row |

`judge_answer` speaks exactly two verdicts: **keep** and **drop**. You never
assign `abandoned` or `unjudged` yourself — `close_session()` assigns them
from what the inventory proves when the night is swept.

Primary rate = `keep ÷ judged`. Abandoned and unjudged never inflate it — but a big
pile of either means the loop isn't being closed, so the report shows both, plus a
harsher `keep_rate_if_abandoned_counted_as_drop`. Under 8 judgments it refuses to
read as a rate.

When logging outcomes, note **source** (catalogue / ref-assisted), **role**, **gear**,
and whether ears were live or blind (context only).

**Say no** to new tools/generators/SOMBRA panel work unless it improves selection,
closes judgment, measures durable adoption, or removes friction from this loop.

## Default session loop

This is the serving-tool workflow, not a requirement to force every conversation
through a generator or demand a verdict before continuing authorized musical work.
Use the owner's existing answers and pace; only he assigns KEEP/DROP. Read the
knowledge guide before turning observations or historical averages into rules.

1. `hear_the_room()` → pass `use_ears=True` on serving calls
2. `answer_my_loop` (gear **2** default, **3** to develop) — write to JAM only when
   notes are worth hearing
3. Listen → `judge_answer` KEEP/DROP (the only two verdicts; `close_session()`
   assigns abandoned/unjudged from the end-of-night sweep)
4. Continue: grow / develop / arrange only after a judgment
5. **Session end: `close_session()`** — inventories Session and Arrangement clips across the Set and scores resolved outcomes

Verified suggestion writes are stamped in `store/session_ledger.json` with Set and
clip identity, session, source, role, gear, and ears context. Revisions keep the
same suggestion entry and preserve the original judged snapshot.
**Run `close_session()` while the intended Set is open.** Incomplete reads stay
retryable; missing or ambiguous identity never means deletion. Legacy entries
without reliable identity remain unresolved. Reports with unresolved judgments
withhold a definitive survival rate. `session_report(refresh=True)` can collect
next-session evidence from the current Set; `adopt_answer` records a verified
link from a suggestion to its song destination.

Prefer this path over inventing a new tool chain each session.

## Arrangement candidates (recovered writes → one closed loop)

1. `register_arrangement_candidates(journal_path=…, set_path=…, confirm=True)` with
   the Set open — every id is re-read from the Set; refused ids are named
2. He listens and says **1/2/3** or **KEEP/DROP with why** — one variant at a time
3. `judge_answer('keep'|'drop', entry_id=<entry_id or label>, reason=<his words>)`
   — judges that variant only; the reply names the siblings still unjudged
4. A DROP here is arrangement feedback, never a phrase ban; feedback lands in
   `piece_decisions` (owner corrections outrank it)
5. Night end: `close_session()` verifies the intended change (content digest,
   whereabouts, mutes, device presence) → adopted / staged_kept / not_adopted /
   drop; unknown stays unresolved. **Any later `close_session()` with that Set
   open re-resolves it** — a plain close does, even when the phrase night is
   already sealed (`close_session(session_id=…)` also works); after a Live
   restart a row is matched by content on its same-named track, never by id
6. Read the **arrangement subtotal** and **effect-trial line** in `session_report()`
   beside — never inside — the phrase KEEP rate. **Read the coverage before the
   rate:** the rate is published only when every verdict is resolved
   (`rate_status: definitive`, `coverage.resolved_judged == coverage.judged_total`);
   while a judged row is unknown it is `null` / `partial` — report the counts
   ("1 adopted, 2 unresolved"), never a percentage, and never call an unknown a
   drop. Under 8 judgments it is too thin to read as a rate.
7. **Observation vs round.** If the row **survived** (adopted / staged_kept /
   kept) and the same material turns up again — the Set reopened (new ids), or
   deleted and written again unchanged — registering it is `observed`:
   next-session presence recorded once on the closed row, already in the score,
   never a second judged row. Tell him "still there" rather than asking for the
   same KEEP twice. Changed content, or his own copy on one of **his** tracks, is
   a new round (`new_round_of`). If the row did **not** survive (abandoned /
   not_adopted / drop / removed), proposing the same material again — even the
   identical clip — is a new round he can judge (`re-proposed_after_<state>`):
   a DROP last week never forbids a KEEP tonight.
8. **Kept ≠ audible ducking.** An effect trial's `kept` means the device is still
   present on his track — by identity, never by name; `removed` only when Live
   itself says the id no longer exists — only Live's does-not-exist proves
   absence; a moved device, a timed-out or empty read, or a recycled id stay
   `unknown` with the reason, never "removed" (on PP 2.2 a deleted device stays
   unknown too). Its
   parameters are a separate line (`params_compared` / `kept_params_unchanged` /
   `kept_params_uncompared`; unread or unreadable parameters are named, not
   hidden). With the sidechain source unset the compressor ducks nothing: never
   report "ducking works" from a kept row — that needs routing and a listen.

## Piece continuity (one piece, many Sets)

`piece_brief()` does not ingest arbitrary prose: it extracts the `Suggestions`
and `Cuts and transitions` tables. Always open its returned `notes.path` too.
See [the exact supported table format](KNOWLEDGE_GUIDE.md#a-piece-brief-is-not-the-whole-notes-file).

The composing loop that works — brief → question → his gesture → one reference
job → distinct options → audition → his reaction → observation — leaves a
record a next session can continue from. Verdicts only in his words.

1. **Brief first.** `piece_brief(set_path)` (read-only; `live=True` adds one
   snapshot read). It prints owner intent (piece-level, with version
   overrides and `inherited` named), **his statements** and the
   **model-heard / measured** observations in separate blocks, the notes'
   suggestion and join tables with status, open questions, proposals with his
   reactions, Set changes "observed, unjudged", references with their jobs.
   Do not make him re-enter what it already says. If the Set is not a
   confirmed version, ask him and `declare_piece_version(piece, set_path,
   confirm=True)` on his word — never from a similar filename.
2. **One musical question** ("how can bars 65–80 keep their intensity while the
   foreground clears?") — not "generate drums".
3. **His gesture** — the clip / phrase / cell with ids, or a catalogue phrase.
4. **One reference job** — the reference and what it does here ("that record's
   bells sit on their own grid, apart from the kick"), with its evidence type; preserve his sound.
5. **Distinct options** (2–3, or "leave it"), one varied dimension each →
   `record_proposal(piece, set_path, question, source_gesture, reference_job,
   preserve, vary, candidates, seeds)` → `proposal_id`. Material already in
   the Set registers with that id (`register_arrangement_candidates(items=[{…,
   "proposal_id": …}])`).
6. **Audition** in context, original preserved, reversible.
7. **His reaction** → `record_reaction(words, scope, proposal_id=…)` — his
   words verbatim, scoped (section bars / track / candidate / piece / global);
   your reading goes in `interpretation`, never promoted; `rule=True` only
   when he states a rule. A KEEP/DROP is still `judge_answer`, in his words.
8. **Observation** — a track deleted, muted, a clip moved, a device gone on a
   read → `record_observation(set_path, what, evidence={"snapshot_digest": …})`.
   Observed, unjudged; it never becomes a verdict.

## Loops → finished track

1. `bury_or_marry` / `version_courage` — choose the idea or version to finish
   (`version_courage` compares the members of a version family; it is in the
   `session` profile only, so run it there — `compose` does not expose it)
2. `whats_missing('live')` — diagnose the open Set (load `sombra-brain`)
3. `lay_the_song` — preview the arrangement, confirm locators, then commit
4. `no_new_notes` — constrain the session to the material already recorded
5. `three_endings` — generate and choose an ending
6. `one_hour_close` → export, then `confirm_bounce`
7. `close_session` — close the composing-session ledger

## Offline mix pass (only when asked)

The Mix Adviser is a separate, gated future/offline trial surface — not part of the
default Live composing loop. Use [`docs/MIX_ADVISOR.md`](./MIX_ADVISOR.md) only when
the owner explicitly asks: export a local master plus 2–4 aligned groups, inspect
evidence, manually apply any ≤3 bounded gain proposals (`pan` is parked for
automatic v1), then return for a randomized-label A/B judgment without trying to
reconstruct the hidden mapping. `listener=True` is opt-in and may send audio through
sandboxed `agy`; the tools never write Live or Producer Pal, and judgments live in
the separate mix trial ledger rather than the composing-session KEEP denominator.

## Session open (Live up)

0. **Read the loop state first.** `hear_the_room()` returns `ledger_status`
   (or call `session_report()` when Live is down): act on `next_action` —
   sweep unswept nights of the open Set, attest legacy rows, judge pending
   writes — before generating anything new. `auto_closed` lists nights the
   call already swept for you; `scoring_ready` says whether tonight's writes
   can be scored at all.
1. Call `hear_the_room()`; keep the `moment` if useful for serving calls
2. Pass `use_ears=True` on: `find_phrases`, `answer_my_loop`, `audition_leads`,
   `suggest_growth`, `grow_my_loop`, `develop_my_loop`
   (`grow_my_loop` / gear 4 gates **backing** only — continuation is stamped
   `ears: not_gated`; do not treat it as room-admitted)
3. When `moment.unheard` is nonempty, acknowledge the named audio material and
   ask the owner what it is doing before giving structure advice. Do not infer
   its musical content from the track/clip name (guitar, vocal, field recording,
   etc. are all just "unheard" until he says). The scan follows actual playing Session clips and Arrangement context;
   stopped transport does not imply that row 0 is playing.
4. `reset_ears()` only when he asks to clear demotions
5. `store/ears_default.json` stays OFF until Booth promotion — still pass `True`
   explicitly in chat

## Organic Listen (Hearing Audio Takes across Any Project & Track)

When the owner asks to "hear" or "listen to" a recorded audio take or frozen track:
1. **Never ask for a JSON manifest**: Live automatically saves audio to `<Project>/Samples/Recorded/` and Producer Pal discovers clips internally.
2. **Inventory capability**: `inventory_session_audio()` → opaque `inventory_id` + sanitized take descriptors (no paths).
3. **Ask focus**: Ask or confirm `focus` (`full` | `groove` | `melody` | `composition`).
4. **Call listener**: `listen_to_takes(inventory_id, take_ids, focus=…)` (configured listener; single-use capability).
5. **Scoped results**: Cards are saved automatically under `store/session_ears/<session_id>/take_cards.json` bound to verified Set identity. The active Set is checked again before cards are used.

## Tracks (never violate)

| Track | Auto-write | Role |
|-------|------------|------|
| `SIDEKICK JAM` | yes | Answers land here (`write=True` on `answer_my_loop`) |
| `SIDEKICK BOOTH` | append-only | Blind staging; read-back verified; he clears old rounds |
| Everything else | no | Payload preview or confirm-gated only |

Locators are the one exception — non-destructive, natively deletable.

## Gearbox (`answer_my_loop` `gear=N`)

| 1 RECALL | 2 RECONTEXT (default) | 3 DEVELOP | 4 GROW | 5 COMPOSE | 6 COVER |
|----------|----------------------|-----------|--------|-----------|---------|

Silence and rests are valid at every gear. Gears 5–6 return **contracts**, not notes —
load `.claude/skills/sombra-brain/` and run `score_phrase` before he hears anything.

Optional `direction` (`darker`, `brighter`, `sparser`, `weirder`, `clubbier`, …) bends
ears gates when `use_ears=True`.

## Audio references

Default search excludes derived moments and marked-suspect cards; an unflagged
erroneous card can still return, so inspect the full card before steering by it.
The owner's corrections remain authoritative; a successful import or file-access
receipt does not prove listening accuracy.

External YouTube / file refs (style-sidekick MCP — shared across clients):

- **You choose `focus`** (`full`|`groove`|`harmony`|`melody`) before `analyze_audio_ref`
- `analyze_audio_ref(source, focus=…)` → brief + `ref.mid` under `store/audio_refs/` (no Ableton write)
- `find_audio_refs(query=…)` while composing; `tag_audio_ref(project=…)` pins to a **project you name** (not the Ableton set)
- `tag_audio_ref(note=…, verdict=keep|drop)` after Live — the owner overlay beats Gemini when they conflict
- `apply_audio_ref(ref_id, role=…)` → imitation rules + MIDI handoff; `write=True` → **SIDEKICK JAM only**

### Composing WITH a ref (his material, ref-shaped)

| Want | Call |
|------|------|
| "one of my lines, in the mood of that track" | `answer_my_loop(ref_id=…)` — gear 2 only |
| "a beat like that track, from my drums" | `beat_from_ref(ref_id=…)` |
| "help me structure a track" | no tool — read the ref's `describe.txt` prose; if `hear_the_room` shows `unheard`, ask what those takes carry first |

Both pull from **his** catalogue; the ref only narrows which phrase. What it does:
tempo picks a window (half/double-time aware), `density` runs the ordinary ears
gate, `register` nudges the octave. What it does **not** do: re-key his loop, or
refuse — a reference narrows and announces, it never vetoes. Where ref and room
disagree the **room wins**. Check `result["ref"]["unmapped"]` for what the brief
said that the machine could not act on.

- `clip_audio_ref_moments(ref_id)` → jam-sized mini-refs from `# Moments` (no re-listen)
- `analyze_audio_refs_batch` for a pile of URLs; Basic Pitch is weak on drums — trust the brief
- Back the judgments up before any reindex: `python3 tools/audio_ref.py --backup-overlays`
  (index + every `meta.json`/`describe.txt` → `store/backups/`, no audio; `--reindex` now
  does it first automatically). The .wav files are re-downloadable — his notes/verdicts are not.

### Reference Live hour (use before deepening)

1. `hear_the_room`
2. `find_audio_refs(project="<your label>", k=5)` (or a query / `verdict="keep"`)
3. Pick one → `apply_audio_ref(ref_id, role=…)` — `write=True` only if notes look useful; else compose from `imitation_do` / `ricardo_note`
4. Listen → `judge_answer` KEEP/DROP (terminal state — no silent skips)
5. `tag_audio_ref(ref_id, note="steal …", verdict="keep"|"drop")`

Success for this hour: **session-surviving** material that cites a filed ref — not
merely an immediate KEEP click. Prefer describe / `ricardo_note` / imitation rules over
pasting Basic Pitch melody. Alternate ref-assisted vs catalogue-only sessions when
comparing (avoid selection bias).

Details: `config/claude_mcp.md` → **Audio references**.

## After an answer

- **KEEP / DROP** via `judge_answer` — JAM track only; judge every write KEEP or DROP,
  and `close_session()` labels the rest `abandoned` / `unjudged`. Prefer `answer_notes=` (what he heard) so
  ears session memory buckets the served register/density, not only the source id
- **DROP** is confirm-gated and deletes jam-track clips only
- **Arrangement / effect route** (material already on SIDEKICK tracks, devices on
  his tracks): `register_arrangement_candidates(…, confirm=True)` with the Set open
  → `judge_answer('keep'|'drop', entry_id=<id or label>, reason=<his words>)` per
  variant (never a phrase ban) → `close_session()` the same night. The row's
  `entry_id` is what you judge; a `refused` candidate names why (an id that is not
  in the Set, evidence that does not bind); an `observed` one is a closed row seen
  again — say "still there", do not re-judge. Report the subtotal by its counts
  and coverage; `kept` on a device is presence, not a heard effect
- Rituals (coach-exact names): `whats_missing`, `three_endings`, `bury_or_marry` —
  ledger rules in `config/claude_mcp.md` → The judgment ledger

## Voice

Speak producer: bars, sections, energy. Use the owner's own genre words (recorded in
`idiom.md`); never a label they reject.

## Skill routing

| Need | Skill |
|------|-------|
| Substance, compose, diagnose | `.claude/skills/sombra-brain/` |
| Stuck on process | `making-music-pattern-coach` (user-level, if available) |
| One provocation | `.claude/skills/challenge-deck/` |

Coach names the block, sombra-brain supplies substance, deck breaks ties.

## Deep refs (do not duplicate)

- `docs/ARCHITECTURE.md` — module map, data flow, store artifacts
- `config/claude_mcp.md` — MCP setup, tool list, ears workflow
- `docs/EARS_PROMOTION.md` — before flipping `store/ears_default.json`
- `docs/SOMBRA_DEVICE.md` — device workflow and acceptance protocol
