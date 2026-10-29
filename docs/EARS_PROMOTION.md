# Ears Promotion Checklist

Everything on this page is gated on flipping `store/ears_default.json` to
`{"use_ears_default": true, ...}` — the one human act that turns `use_ears`
on by default across every serving surface (the design spec is not
published; the implementation plan is
`docs/history/plans/2026-07-10-ears-context-layer.md`). The
flag stays absent (OFF) until a pre-registered `build_ears_booth.py` round
qualifies under the scoreboard rule (`tools/ingest_booth_votes.py`,
`promotion_status`). Read this list before flipping it.

## 1. Refusal-shape callers that assume a normal answer — closed 2026-07-24

`use_ears=True` + an empty survivor set makes `core.answer_my_loop` return a
`no_fit` shape: `{"no_fit": True, "answer_notes": [], ...}` with **no**
`source_phrase_ids` key at all (spec §8). Three callers used to assume a normal
answer; until promotion `_ears_default()` is False and `no_fit` stays
unreachable through them, but the 2026-07-24 fix wave guarded all three so a
promoted default can't surface the gap:

- **`gears.py` gear 1** (`answer_with_gear(..., gear=1)`): guards `no_fit`
  before reading `ans["source_phrase_ids"][0]`, returning the refusal shape
  with `gear=1` — no more `KeyError` (pinned in selftest blocks 48/50).
- **`gears.py` gears 2/3**: gear 3's `_ending_variation` surgery gained a
  `no_fit` guard before its `max(range(len(out)), ...)` step, so an empty
  `answer_notes` no longer raises `ValueError`; gear 2 already passed the
  empty list through harmlessly (landed 2026-07-24).
- **`mcp_server/sombra_listener.py`'s `_execute("answer_my_loop")`**: already
  checks `ans.get("no_fit")` and returns the refusal before
  `live_write.write_answer_to_jam_track(...)`, so a refusal no longer writes an
  **empty clip** to SIDEKICK JAM (pinned in selftest blocks 48/50).

(`mcp_server/server.py`'s `answer_my_loop` MCP wrapper — the other write path
with the same shape of risk — has long checked `ans.get("no_fit")` and set
`live_write_skipped` instead of writing or crashing. As of this wave the gear
paths and the SOMBRA listener match it.)

**Landed in the 2026-07-24 fix wave**: `gears.answer_with_gear` gained a
`use_ears`/`moment` path (it no longer hard-codes `use_ears=None`, i.e. "follow
the default" — the SOMBRA listener now threads its session audition switch
through this path), and gear 3 gained its `_ending_variation` `no_fit` guard.
The gear-1 guard and the listener skip-write were already in place
(selftest blocks 48/50). Nothing in this section blocks promotion any longer.

## 2. The Moment-read contract test only runs against real Live manually

The spec §9 acceptance requires the staged-clip Moment-read contract test to
run against **real** Live via `tools/check_ears.py` before the Booth round
is built. `read_moment_live`'s tempo parse and track sweep (`mcp_server/
moment.py`) are exercised by the synthetic selftest blocks (42-46) with
injected track/clip data, but the actual Producer Pal bridge read — tempo
key lookup, track-name resolution, clip note decoding against a real Set —
is **manually verified only**. Run `tools/check_ears.py` (optionally
`--save tools/ideas/moment_snapshot.json`) against a real Live session
immediately before `build_ears_booth.py`, every time, and eyeball the
printed Moment against what's actually playing. The check must include a set where some of the first 12 tracks have no row-0 clip, to confirm Producer Pal answers empty slots with a normal (non-error) response under the universal isError raise; if any version marks them isError, read_moment_live needs a tolerance scoped to the empty-slot message only — everything else must still propagate to blind.

## 3. `build_ears_booth.py` operational caveats

- **Replaces the in-flight Booth round.** It overwrites
  `tools/ideas/pairwise_manifest.json` and `pairwise_predictions.json` — the
  same files a normal (taste) Booth round uses. If a round is mid-vote when
  you run this, its pairs are gone from those files. Votes already collected
  for the old round are not corrupted or mis-ingested — `ingest_booth_votes.
  py` refuses by `round_id` (the old votes' round_id no longer matches the
  frozen artifact digest) — but they are operationally orphaned unless you
  rebuild the old round from its own inputs first.
- **Reads the REAL ears session file.** `build_pairs()` calls `ears.serve()`
  with the default `session_path` (`tools/ideas/ears_session.json`), so
  whatever demotions have accumulated from real usage bias which candidate
  becomes the A pick. Run `ears.reset_session()` (or `tools/session.py`,
  which now does this automatically at the start of every voting session)
  before building a Booth round, so the round is judged on gates alone, not
  on session history nobody can see on the page.
- **`--n` greater than the lead-phrase count repeats prompts.** `build_pairs`
  indexes `lead[(seed + i * step) % len(lead)]` — once `n` exceeds the
  number of lead-role phrases in the index, prompts wrap around and repeat.
  Not a crash, just a quietly smaller effective round than `--n` suggests;
  check `len(ab)` against `--n` in the printed line before trusting round
  size.

## 4. Gearbox-only direction words — FIXED this round

`answer_my_loop` translates the gearbox's wider direction vocabulary
(`brighter`/`song`/`maximal`/etc., `gears.py`'s `_DIRECTION_POLES`) into the
ears' narrower vocabulary (`core._EARS_DIRECTION_ALIASES`) at the boundary.
Words that don't map (gearbox-only words) used to silently leave the gates
unbent. As of this fix round, `answer_my_loop` appends an announcement —
`"direction '<word>' is a gearbox word — gates unbent"` — whenever this
happens with ears on (`mcp_server/core.py`, pinned in selftest block 46).
Nothing further needed here before promotion; listed for the record.

## 5. The un-wired fifth surface: SOMBRA's deal path

Spec §5 lists retrieval, answers (`answer_my_loop`), growth
(`suggest_growth`), and the deal as routing through `serve()`. Four of those
are wired (`mcp_server/core.py`'s `_apply_ears`/`_serve_indexed` call sites,
selftest block 46). The SOMBRA "deal" (`rituals.deal()` / `bury_or_marry`)
is **not** — it picks a whole *project* by take-lane debris count, not a
phrase candidate from a pool, so there's no candidate list for `serve()` to
gate in its current shape. This is a recorded descope for this phase, not
an oversight: revisit whether/how "the room" should influence which project
gets dealt when this surface is worked on again, at or after promotion.

## 6. `suggest_growth` backing roles bypass the gates (F3b — promotion blocker)

`suggest_growth`'s rare-progression suggestion carries a `backing` block
(`{"pad": ..., "bass": ...}`, `mcp_server/core.py`). The ears only gate the
`lead` candidates — the wrapper handed to `serve()` is built from the derived
lead rating candidates, so the backing pad/bass are **never seen by any gate**.

Reviewer's scenario: the room already has bass occupied; a growth suggestion is
admitted because its *lead* clears the role/register gates; but its `backing`
adds a second bassline the role gate never inspected — exactly the "no second
bassline while bass is occupied" collision the gates exist to prevent, slipping
through on the backing channel. Deliberately **not** fixed this wave (the growth
backing is a multi-role bundle, not a single candidate the current single-role
`serve()` shape can gate). **Before promotion**: either gate each backing role
against the Moment separately, or drop `backing` from a suggestion whose backing
roles collide with the room. Until then, a promoted-ears `suggest_growth` can
still stack a colliding backing role.

(The sibling F3a bug — the register gate judging pre-transposition catalog
material in `answer_my_loop` — was fixed this round: `core._prospective_answer_notes`
hands the gates each hit's realized, transposed notes; pinned in selftest
block 46.)

## 7. Booth note-content binding does not yet cover the audition manifest (F6)

Booth pairs are now bound to their frozen note content via `A_notes_sha256`/
`B_notes_sha256` (`tools/rank_pairwise.py`, `tools/build_ears_booth.py`), and
both use sites — `tools/session.py`'s `_stage_clips` and `ingest_booth_votes.py`
training — verify the hash against the live phrase index and refuse loudly if a
phrase drifted since the round was frozen (`verify_booth_note_bindings`; absent
hash on pre-F6 rounds is grandfathered/skipped). The **audition** manifest
(`tools/build_audition_page.py` / `ingest_audition_votes.py`) has the same
staged-by-ID-from-a-mutable-index exposure and is **out of scope this wave** —
bind and verify audition rows the same way before promoting anything that leans
on audition provenance.

## 8. Selftest hygiene

Every selftest call to a serving tool must pass `use_ears=False` explicitly (or inject a synthetic moment) — bare calls follow the promoted default and reach for the live bridge mid-suite.

## Promotion mechanics, for reference

- `tools/build_ears_booth.py` builds the pre-registered A/B round from a
  real (`ears: "live"`) Moment snapshot.
- Vote it like any Booth round (`tools/session.py booth`, or manually).
- `tools/ingest_booth_votes.py` scores it: `ears_ab` rounds count toward
  their **own** promotion metric (printed separately as "ears promotion
  metric") and never advance or break the taste-model's promotion streak
  (`promotion_status` skips non-taste `kind`s — fixed this round).
- Promotion itself is one human act: write `store/ears_default.json` with
  `{"use_ears_default": true, "round_id": "<the qualifying round>"}`.
  Nothing reads "qualifies" to flip this automatically.
