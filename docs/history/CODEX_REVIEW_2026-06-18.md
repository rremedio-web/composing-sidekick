# Composing Sidekick Critical Review

Date: 2026-06-18

Scope: read-only code and artifact review, plus the requested markdown report. I read `HANDOFF.md` end to end, inspected the requested modules, summarized the local gitignored JSON artifacts, and ran `.venv/bin/python mcp_server/server.py --selftest`.

## Verification Snapshot

- Selftest result: `=== all selftests passed ===`, with 20 blocks. This corrects the handoff text that still says 19 blocks.
- The selftest is not strictly read-only: it writes `store/exports/selftest_phrase.mid` as part of the MIDI round-trip check.
- Stored taste state: inspected (counts omitted from the public copy).
- Corpus/artifacts:
  - `store/phrase_index.json`: 27,844 phrases: 22,965 harmonic, 4,879 drum.
  - Harmonic phrase roles: 12,248 lead, 4,540 pad, 4,264 bass, 1,913 other.
  - `store/style_profile.json`: 277 projects, 266 harmonic projects, 511,655 harmonic notes, 101,081 drum notes.
  - `store/motifs.json`: 80 cells from 6,769 lead-like phrases; classes are fall 26, climb 25, dark_landing 17, wobble 7, repeat 5.

## Executive Verdict

The strategic bet is right: your edge is not "make a better note generator." It is a reasoning and steering system around your own MIDI: structure, role, arrangement, seed-relative development, retrieval, and taste. The note generator has already shown the classic problem: it can become statistically plausible while still not sounding keepable.

The hard truth: the current taste model is active, but it is learning from a narrow and partially confounded candidate stream. The anti-static signal is plausible, but the model is not yet robust enough to steer lead selection aggressively, and it is definitely not safe to use for harmony/riff/growth judgments without role and context tags.

The next valuable work is not a neural model. It is:

1. Fix the evaluator/taste data contract.
2. Make growth seed-relative.
3. Make role explicit everywhere: scoring, candidates, ratings, generation, arrangement.
4. Turn arrangement into an editable SongPlan with validation.
5. Only then consider a small symbolic model as one more candidate generator behind the same taste loop.

## Findings First

### High Priority

1. `get_ratings()` hides the main taste loop.

`mcp_server/core.py:3041` summarizes only the older `ratings` list. It ignores `candidate_rounds` and the trained taste model, even though `rate_candidates()` is now the main taste path. Locally, `get_ratings()` returns `total: 1`, while `taste_model.json` has 4 rounds and 14 pairs.

Impact: the public API can tell you "almost no ratings" while the lead selector is already being steered by taste. This will confuse the UI, future agents, and your own trust in the loop.

Fix: return both channels:

- retrieval ratings: count/up/down/recent
- candidate rounds: count, pair count, recent reasons, roles, generators
- taste model: feature names, weights, blend weight, last trained timestamp

2. `audition_leads()` is not a clean exploration sampler once taste is active.

`mcp_server/core.py:1795` calls `compose_from_profile()` for each audition candidate. `compose_from_profile()` calls `_select_best_lead()` at `mcp_server/core.py:2020`, and `_select_best_lead()` uses the current taste model at `mcp_server/core.py:1780`.

Impact: the thing you rate is already taste-selected from 12 hidden candidates. That creates an exploitation feedback loop: early weak taste weights shape later training data. It also throws away the losing hidden candidates that would be valuable preference data.

Fix: split generation from selection:

- `generate_lead_candidates(..., n=12, use_taste=False)` returns raw candidate objects.
- `select_lead(candidates, policy="style|taste|hybrid")` selects for actual composition.
- `audition_leads()` should default to exploration, not current-model exploitation.

3. Lead rhythmic diversity is mostly fake right now.

`_sample_groove_onsets()` takes `rng` and `groove_power`, but it sorts slots by weight and selects the top slots deterministically (`mcp_server/core.py:1201`). Exponentiating weights preserves rank order, so `groove_power` cannot change the selected onsets. Changing seed also does nothing.

I verified this with a read-only probe: powers 1.0, 1.5, 2.5, and 10.0 produced the same onset set; seeds 1, 2, and 3 also produced the same onset set. `audition_leads(bars=4, key="D minor", n=4, seed=100)` produced 4 candidates with identical onsets:

`[0.0, 3.0, 4.0, 7.0, 8.0, 11.0, 12.0, 15.0]`

Impact: the taste model is learning almost no rhythmic preference from lead auditions, because the candidate generator barely varies rhythm. That also explains why `groove` is nearly constant across candidate features.

Fix: use weighted sampling without replacement over bar/slot candidates, seeded by `rng`, with constraints for max notes per bar and minimum spacing. Make `groove_power` a real temperature. Keep determinism by seed.

4. `score_phrase()` and taste features are not role-aware, even though generation now is.

Lead generation correctly prefers `melodic_grammar_by_role["lead"]` at `mcp_server/core.py:1681`. But scoring still uses the pooled grammar at `mcp_server/core.py:2670`, and `_taste_features()` simply calls `score_phrase(notes)` at `mcp_server/core.py:249`.

Impact: lead candidates are generated by lead grammar, then scored by pooled lead+bass+pad grammar. That partially reintroduces the "warm average" problem through selection and taste training.

Fix: add `score_phrase(notes, role="lead", key=None, progression=None)` and use role-scoped grammar/groove/register targets. The lead selector should call `score_phrase(..., role="lead", key=known_key)`.

5. Short generated leads are scored under unstable self-detected keys.

`_phrase_style_analysis()` estimates key from the candidate's own pitch-class histogram (`mcp_server/core.py:2649`). But generated leads already have known key/tonic/mode in `compose_from_profile()`.

I verified 20 generated D minor leads. `score_phrase()` detected only 13 as D minor; others were D major, A minor, F major, and G minor.

Impact: base style score and taste features can reward/penalize the wrong tonal interpretation. This likely contributes to the learned negative `scale_fit` weight.

Fix: scoring should accept a known key and use local key estimation only as fallback.

6. Harmony growth candidates are being rated as lead-only notes.

In `suggest_growth()`, harmony candidates include backing at `mcp_server/core.py:2227`, but the training candidates returned to `rate_candidates()` are only `[s["notes"] for s in suggestions]` at `mcp_server/core.py:2238`. The harmonic novelty is in the progression/backing, not necessarily in the lead features.

Impact: the taste model can learn the wrong thing from a harmony judgment. If you like/dislike the harmonic color, the model sees only lead notes.

Fix: change candidate data from `list[notes]` to a versioned candidate object:

```json
{
  "id": "...",
  "role": "lead|bass|pad|harmony|arrangement",
  "generator": "growth_rare_progression",
  "notes": [...],
  "backing": {"pad": [...], "bass": [...]},
  "key": "D minor",
  "progression": "iv->i",
  "features": {...}
}
```

7. Arrangement currently forces lead into almost every section.

`_active_roles_for_section_with_develop()` adds lead when the section role is any of `"intro", "section", "breakdown", "outro"` (`mcp_server/core.py:2254`). That condition is effectively always true for normal sections. The selftest arrangement confirms lead appears in intro, breakdown, drop sections, and outro.

Impact: this fights your minimal/hypnotic aesthetic. Silence and absence are arrangement decisions; the current code treats the lead as a thing to keep present and varied everywhere.

Fix: add explicit lead-presence rules: intro teaser, sections where hook is intended, post-breakdown reprise, maybe outro fragment. Do not force lead into breakdown/outro by default.

8. The "pure stdlib" claim is not literally true for runtime paths.

`mcp_server/core.py` imports `music21` inside harmony realization functions (`mcp_server/core.py:943`, `mcp_server/core.py:1091`, `mcp_server/core.py:1108`) and imports `mido` inside `export_midi()` (`mcp_server/core.py:3100`). `mcp_server/server.py` imports FastMCP (`mcp_server/server.py:21`). There is also no `requirements.txt` or `pyproject.toml`.

Impact: the practical setup works in the venv, but the project is not reproducible from the repo alone, and the stated constraint is ambiguous.

Fix: either document the exception as "stdlib for ML/taste logic; mcp/mido/music21 runtime deps" or replace runtime music21/mido usage with stdlib implementations. At minimum add a small dependency manifest.

### Medium Priority

9. `core.py` is a 3,464-line multi-domain module.

It mixes artifact loading, retrieval, music theory, generation, taste, growth, arrangement, MIDI export, Producer Pal formatting, and ratings. The longest functions are:

- `_phrase_style_analysis`: 129 lines
- `send_to_ableton`: 117 lines
- `arrange_song`: 109 lines
- `compose_from_profile`: 103 lines
- `_compose_lead_from_profile`: 103 lines
- `critique`: 92 lines

This makes it easy to introduce cross-layer bugs like "taste feature calls pooled score" or "growth harmony gets rated as lead."

Recommended decomposition:

- `mcp_server/store.py`: paths, JSON loading, cache invalidation, artifact version checks.
- `mcp_server/notes.py`: note normalization, pitch names, beat/bar conversions, monophony.
- `mcp_server/retrieval.py`: phrase search, rating re-rank for retrieval.
- `mcp_server/scoring.py`: role-aware style metrics and critique.
- `mcp_server/taste_features.py`: feature extraction, candidate schema, pair extraction.
- `mcp_server/taste.py`: keep only the math model.
- `mcp_server/generation/harmony.py`: Roman parsing, chord timeline, cached chord pcs/midis.
- `mcp_server/generation/lead.py`, `bass.py`, `drums.py`, `pad.py`: role-specific generators.
- `mcp_server/growth.py`: seed-relative growth and rare move catalog.
- `mcp_server/arrangement.py`: SongPlan, section logic, devices.
- `mcp_server/ableton.py`: Producer Pal payloads.
- `mcp_server/midi_io.py`: MIDI export/import.
- `mcp_server/server.py`: thin wrappers only.

10. `run_selftest()` is too monolithic.

`mcp_server/server.py:242` is a 756-line selftest. It tests many useful behaviors, but it buffers output for a long time, mutates globals for tests, writes an export file, and contains an unused `errors` list.

Fix: keep the single command but split into small named test functions. Print and flush before each block. Use temp export paths for MIDI round trips.

11. Role detection is useful but too binary.

`analyze/roles.py:35` classifies by median pitch and average notes per occupied onset. It works as a first pass, but it has no confidence score and cannot distinguish "lead-ish riff in bass register" from bass line intent, or monophonic long pad from lead/bass.

Fix: return `{role, confidence, reasons}`. Use role-specific distributions from the corpus, not only hard thresholds. Let user override/label roles in ratings.

12. There is no artifact provenance contract.

Generated JSONs encode derived data, but candidates do not store enough metadata: generator version, seed, key, role, feature vector version, model version, and source artifact versions. That matters once the taste loop becomes durable.

Fix: version the candidate schema and feature extractor. Store features at rating time so retraining old data is possible even if feature code changes.

## Taste Model Review

### Is the approach right?

Yes, as a first learning loop. A hand-rolled pairwise logistic/Bradley-Terry model over interpretable features is exactly the right scale for 14 pairs. It is deterministic, inspectable, cheap, and aligned with "partner, not replacement."

But it should be treated as a preference probe, not a truth model. The current weights are not yet reliable musical laws.

### Current learned signal

`store/taste_model.json`:

- `melodic_fit`: +4.399
- `gesture_fit`: +4.307
- `staticness`: -2.616
- `jumpiness`: +1.736
- `scale_fit`: -1.380
- `register`: 0.0
- `polyphony`: 0.0
- `n_pairs`: 14
- `train_accuracy`: 0.929

Interpretation:

- Anti-static is probably real.
- Positive gesture/melodic fit is plausible.
- Positive jumpiness might mean "motion/energy," not literal jumpiness.
- Negative scale fit is probably a confound from local key estimation and tiny data.
- Register/polyphony are zero because generated lead candidates barely vary on those axes.
- Train accuracy is not meaningful with 14 pairwise samples that are not independent.

### Feature problems

The 9 features are too close to the current scorer and too far from your listening language.

Missing features that would likely learn your ear faster:

- Role: lead vs bass riff vs pad vs harmony.
- Known key/progression context.
- Ending/landing degree and landing strength.
- Phrase shape: state -> flow -> land.
- Repetition structure: run length, motif recurrence, variation after recurrence.
- Rhythmic variation: syncopation, barline anchors, rest placement, onset entropy.
- Contour: arch, fall, wobble, range, reversal count.
- Harmonic tension/release: chord tone on strong beat, color tone on weak beat, b7/2/b6 use in context.
- Seed distance for growth: how far the suggestion moved from the user's material.
- Role mismatch: "this is a bass riff, not a lead."

### Cold start and tiny data

The cold-start no-op below 6 pairs is good. The ramp to full weight by 30 pairs is reasonable, but with current candidate confounds I would cap steering below 40-60 pairs unless the feature extractor is fixed.

Change the blend policy:

- Always require a base style floor, e.g. do not select a lead below `score_phrase(role="lead") >= 0.70` unless explicitly exploring.
- Until 30 role-matched pairs, taste should mostly break ties among style-valid candidates.
- Use leave-one-round-out validation and bootstrap stability, not train accuracy.

### Rank blend

Rank blending avoids score-scale mismatch, which is good. But rank blending discards magnitude: a terrible taste score and a slightly worse taste score are just adjacent ranks. With only 12 candidates this can overreact.

Better:

- Filter by base validity.
- Normalize base and taste scores within the candidate set.
- Use a conservative weighted sum with a maximum taste override.
- Report "selected because..." with base/taste/rationale.

### Faster learning loop

Use active preference learning, not passive audition.

For each round:

1. Generate a larger pool, e.g. 24 candidates, across controlled axes: rhythm density, staticness, contour, landing, gesture class, role, progression.
2. Remove near-duplicates.
3. Pick 4 candidates that are both diverse and informative.
4. Ask for:
   - best keeper
   - worst
   - optional "interesting but not me"
   - optional role correction: lead, bass riff, pad, harmony
   - short reason
5. Store full candidate objects and feature snapshots.

Pure-stdlib uncertainty option: train 20 deterministic bootstrap models over the pairs and prefer comparisons where models disagree. No numpy required.

## Strategic Bet: Reasoning vs Note Synthesis

Your call is right. Lean into musical reasoning.

Why:

- 277 projects is valuable for personal style statistics, retrieval, and constraints, but small for training a robust note-level neural generator from scratch.
- You choose all sounds. The MIDI has to leave space for sound design. Dense note-level synthesis is not the core product.
- The things you are reacting to by ear are higher-level: role, context, taste, arrangement, "this move belongs here," "this is a bass riff," "the rare move alone is not me."
- The current generator already scores plausible. The missing layer is selection and context.

Should the lead get a different approach?

Not as the main bet. A small symbolic model can be useful later as a candidate generator, not as the composer.

Recommended ladder:

1. Fix current deterministic generator and taste loop.
2. Add retrieval-transform lead generation: find lead phrases similar to seed/context, transpose/time-warp/motif-edit them.
3. Add role/chord/beat-conditioned Markov generation before any neural model.
4. Only after you have 100+ curated "keeper" clips, consider a small symbolic model or LoRA. Keep it behind the same candidate/taste interface.

If you do try neural later, look at it as "one more source of candidates." Do not let it own arrangement or taste. Relevant research directions are Music Transformer, REMI/Pop Music Transformer, FIGARO, and Anticipatory Music Transformer, but they bring dependencies, tokenization complexity, and control problems.

## Course Corrections

### A. Growth on your material, not isolated rare snippets

Correct. Strongly correct.

The current `suggest_growth()` asks "which rare-native move is interesting?" in isolation. Your ear is answering "interesting compared to what?" A rare b7, degree 2, or progression only becomes "you" when it stretches a seed that already has your identity.

Better design:

```python
suggest_growth(
    seed_notes,
    role="lead",
    key=None,
    tempo=None,
    novelty="low|medium|high",
    intent="more interesting|darker|more hypnotic|more lift|less static",
    n=3,
    seed=0,
)
```

Return 2-3 candidate objects:

- `seed_preserve`: motif, rhythm, landing, register preserved.
- `twist`: rare cell, rare degree, progression color, rhythmic displacement.
- `distance`: numeric and plain-language "how far from seed."
- `before_after`: original seed plus transformed version, not orphan snippets.

Seed-relative growth operators:

- Replace one repeated cell with a rare native cell.
- Keep rhythm, change contour.
- Keep contour, shift one landing degree.
- Add a b7/2/b6 color as passing tone, not as a new loop.
- Reharmonize backing while preserving top-line anchor tones.
- Move a lead idea into bass-riff register intentionally, but label it as such.

### B. Role-aware / bass-riff

Correct. This is not a polish issue; it is a core ontology issue.

If you hear "that's a bass riff, not a lead," the model should not treat that as a rejected lead only. It should learn:

- this shape may be good
- in this role/register/context
- but not as a lead

Design:

- Add `role` to `suggest_growth`, `rate_candidates`, `score_phrase`, `candidate_rounds`.
- Train separate role models with fallback to global weights:
  - `taste_global`
  - `taste_lead = global + lead_delta`
  - `taste_bass = global + bass_delta`
  - etc.
- Until there is enough role data, use the global model plus role-specific hand-coded priors.

Bass-riff realization:

- Register: corpus bass range, not lead median.
- Pitch vocabulary: root, 5, octave, b7/approach tones.
- Rhythm: lock to kick/downbeats/offbeats; fewer long rests than lead.
- Harmony: track progression roots.
- Repetition: allow hypnotic riff repetition, but vary every 2/4/8 bars.
- Scoring: compare to bass grammar/groove, not lead grammar.

## Backlog and Ideas

### LLM SongPlan editor

Feasibility: high. Value: very high. Effort: medium.

Best concrete solution: define a strict `SongPlan` JSON schema:

- sections: id, role, bars, energy, active_roles, device tags
- per-role variation: theme/thin/thicken/fill/reprise/etc.
- constraints: total bars, no adjacent duplicate overload, breakdown/drop rules
- edit ops: `extend_section`, `insert_breakdown`, `mute_role`, `bring_role_in`, `reprise_motif`, `thin_outro`

The LLM edits SongPlan, not raw MIDI. A validator repairs or rejects invalid plans. `arrange_song()` becomes `arrange_from_plan()`.

Sequence: do this after role-aware growth basics. It is the clearest expression of "partner, not replacement."

### Eno + Ableton manual principle cards

Feasibility: high. Value: medium-high. Effort: low-medium.

Best concrete solution: do not paste copyrighted cards/manual text into the repo. Distill your own small rules:

- "Remove one layer before adding a new one."
- "Keep the process, change the register."
- "Let the breakdown answer a question from the intro."
- "Automation moves can be arrangement moves."

Attach each card to a SongPlan operation and a validator. This is useful only if operationalized; a text list alone will become decorative.

Sequence: after SongPlan exists.

### "My style x influence" via audio-to-MIDI

Feasibility: medium. Value: high if constrained. Effort: high.

Best concrete solution:

1. Use Basic Pitch for monophonic/sparse influence material, not dense full mixes.
2. Quantize and clean the MIDI.
3. Build an influence profile separately.
4. Compute deltas from your profile: gesture classes, contour, rhythm density, register, harmony color.
5. Apply deltas as dials to your generators, not by merging corpora.

Hard truth: audio-to-MIDI from full produced tracks will be noisy. Treat it as inspiration extraction, not ground truth.

Sequence: after role-aware candidate schema and seed-relative growth.

### Audio "ears" for sounds-like-X / buildups

Feasibility: medium if API-based, low if fully local/free/stdlib. Value: medium. Effort: medium-high.

This violates the clean MIDI-only boundary if it starts steering notes directly. But it can be useful as an external evaluator for bounced snippets:

- "Does this buildup increase energy?"
- "Does this exported sketch sound closer to reference A or B?"
- "Does the drop actually feel bigger?"

Keep it advisory. Do not let it own generation.

Sequence: later. You can get 80% of the benefit from MIDI energy/role/automation heuristics first.

### Conversational UI

Feasibility: high. Value: high. Effort: medium.

Best concrete solution: start with a local CLI/TUI audition queue before a polished app.

Core interactions:

- Generate 4 candidates.
- Write them to Ableton tracks/clips.
- You rate: best/worst/role/reason.
- UI shows why the system selected something.
- UI remembers session context: seed, key, role, intent, ratings.

Sequence: after candidate schema and `get_ratings()` are fixed.

### Evaluation harness

Feasibility: high. Value: high. Effort: low-medium.

Add a local "listening lab":

- fixed seeds
- exported MIDI per candidate
- JSON summaries of features
- taste predictions before listening
- your rating after listening
- regression comparisons over time

This is more valuable than another generator.

## External Tools, Papers, and Products Worth Knowing

Useful, but do not let them distract from the immediate fixes:

- Basic Pitch: free/open source audio-to-MIDI by Spotify. Good for monophonic or sparse influence extraction; risky for dense mixes. https://github.com/spotify/basic-pitch
- MGEval: older symbolic-music evaluation toolbox. Useful as inspiration for metrics, not as a substitute for your ear. https://github.com/RichardYang40148/mgeval
- MusPy: symbolic music toolkit with datasets/I/O/evaluation ideas. It is a dependency-heavy direction, but its abstractions are useful to study. https://arxiv.org/abs/2008.01951
- Music Transformer: strong reference for long-range symbolic structure, but not the first thing to implement here. https://arxiv.org/abs/1809.04281
- REMI / Pop Music Transformer: important because the representation encodes bar/beat/harmony structure. https://arxiv.org/abs/2002.00212
- FIGARO: relevant to controllable generation from high-level descriptions; conceptually close to SongPlan. https://arxiv.org/abs/2201.10936
- Anticipatory Music Transformer: relevant to infilling/continuation with controls. https://arxiv.org/abs/2306.08620
- Ableton Live manual: mine it for arrangement/automation operations, not prose. https://www.ableton.com/en/manual/arrangement-view/
- Oblique Strategies: useful as a design pattern for constraints, but distill your own operational cards. https://enoshop.co.uk/products/oblique-strategies

## Prioritized Plan

### Step 1: Repair observability and contracts

Goal: know what the system is learning.

- Update ratings summary to include candidate rounds and taste model state.
- Add candidate schema versioning.
- Store feature snapshots at rating time.
- Add role/key/progression/generator/seed metadata to candidates.
- Add a dependency manifest or document runtime dependency exceptions.

Do this first because every later loop depends on trustworthy evidence.

### Step 2: Fix lead candidate generation and scoring

Goal: make the taste model learn from real variation.

- Make `_sample_groove_onsets()` actually seeded/weighted.
- Split raw lead candidate generation from selected composition output.
- Add `score_phrase(role=..., key=...)`.
- Use lead-specific grammar and role targets for lead selection.
- Cache Roman chord pcs/midis to reduce music21 overhead.

Success criterion: an audition round should show varied rhythm, contour, landing, and staticness, with known-key scoring.

### Step 3: Make growth seed-relative

Goal: "make this more interesting" instead of "show rare snippets."

- Add `seed_notes` and `role` to growth.
- Return candidate objects with before/after and distance-from-seed.
- Implement 4-6 deterministic growth operators.
- Keep novelty budget low by default.

Success criterion: each candidate audibly preserves the seed and changes one intentional musical dimension.

### Step 4: Add role-aware taste

Goal: stop polluting lead taste with bass-riff judgments.

- Add role to `rate_candidates`.
- Allow role correction during rating.
- Train global + per-role delta models.
- Add role-specific feature extractors.
- Add bass-riff realization and scoring.

Success criterion: a "good bass riff, bad lead" rating improves bass suggestions without making lead selection prefer riff-like material.

### Step 5: Turn arrangement into SongPlan

Goal: make reasoning the product.

- Define SongPlan schema.
- Move current `suggest_structure()`/`arrange_song()` heuristics into plan creation and validation.
- Add edit ops: extend, thin, mute, bring in, reprise, breakdown, drop.
- Let the LLM edit plans by intent, then validate deterministically.

Success criterion: "longer breakdown, bass enters in section 3, bring lead back after drop" becomes a symbolic edit, not a new generation pass.

### Step 6: Build the audition UI loop

Goal: learn faster with less friction.

- Local CLI/TUI is enough.
- Write candidates to Ableton.
- Capture best/worst/role/reason.
- Show current model explanation and uncertainty.
- Keep session logs.

Success criterion: you can do 10 high-quality ratings in 10 minutes without touching JSON.

### Step 7: Influence blend

Goal: "my style x X" without losing you.

- Start with Basic Pitch on simple influence audio.
- Build influence profile.
- Compute deltas, not merged averages.
- Apply deltas as controlled dials in seed-relative growth and arrangement.

Success criterion: influence changes a named dimension while your seed, role, and taste guardrails remain intact.

### Step 8: Optional symbolic model

Only after the above.

Use a model as a candidate generator, not a decision-maker. If it cannot accept role, key, seed, density, and section intent, it is not yet useful for this project.

## What You Are Missing

1. A candidate object model. Right now "candidate" often means `list[notes]`. That is too poor for taste, role, growth, and harmony.
2. A listening lab. You need persistent A/B evidence, not just selftests.
3. Feature/version provenance. Taste data without feature versioning becomes hard to interpret after the next scorer change.
4. Negative space as first-class arrangement data. Minimal/hypnotic music needs "do not play" decisions.
5. Role confidence and user correction. Your ear is already doing role labeling; the system should capture that.
6. Model humility. Keep train accuracy out of user-facing success claims until there is leave-one-round-out validation.

## Pushback

Do not fine-tune a small symbolic model yet. It is tempting because "lead quality" feels like a model problem, but your observed failures point to context, role, candidate selection, and taste. A neural lead model would likely learn another average unless you first build the candidate schema, role labels, listening lab, and taste data pipeline.

Do not make audio ears central yet. They are useful later, but the current system has obvious MIDI-symbolic fixes that are cheaper and more aligned with your constraints.

Do not keep `suggest_growth()` as isolated rare moves. Your listening already answered that. Rare snippets alone are novelty demos; growth should be a transformation of your own material.

The product should become a controlled musical conversation:

1. You provide seed/intent.
2. The system proposes role-aware, seed-relative alternatives.
3. You rate with your ear.
4. The system learns which alternatives you keep.
5. Arrangement reasoning turns kept material into a song plan.

That is the defensible edge.
