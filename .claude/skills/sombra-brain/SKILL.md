---
name: sombra-brain
description: The composer's musical brain for the composing sidekick. Load BEFORE composing new material (answer gears 5-6), translating between the composer's eras (COVER), diagnosing an unfinished project (whats_missing / finisher), laying or arguing about song maps, or any substantive discussion of their music. Holds the owner's confirmed idiom, working theory scoped to their practice, and the diagnosis procedure. Not needed for pure retrieval/recall (gears 1-2) or non-musical coding work.
---

# SOMBRA Brain

Shared musical knowledge for every client, despite the `.claude/` path. Follow the
[knowledge guide](../../../docs/KNOWLEDGE_GUIDE.md): read current owner notes and piece
context directly, retain source labels, and treat theory and catalogue statistics as
working guidance. The owner's scoped instructions and judgments take precedence over
every heuristic here.

**This public copy ships the references as templates.** The author's own idiom,
listening notes and verdicts were removed. Fill each file from *your* catalogue
(`get_style()`, `store/STYLE.md`), the owner's notes (`store/NOTES.md`) and their
words in sessions, keeping the evidence labels the templates ask for.

You are composing WITH the owner, not for a generic producer. Load the reference that
matches the job, then work:

- **Composing or translating** (gear 5 COMPOSE, gear 6 COVER, B-sides, endings) →
  `references/idiom.md` + `references/theory.md`.
- **Diagnosing / finishing** (whats_missing, bury-or-marry, version courage, one-hour
  close) → `references/diagnosis.md` + `references/idiom.md`.
- **Maps and structure arguments** → `references/diagnosis.md` § Context.
- **Theory, M4L/VST or production jobs** → the shared
  [research index](../../../docs/research/README.md), then the relevant complete card.
- **Proposals that borrow from a reference** (Borrow / Preserve / Test) →
  `references/borrowed-techniques.md` — other artists' moves with sources; never the
  owner's idiom.
- **Proposing new parts or arrangements** (drums, top lines, entrances, strips) → also
  `references/ideas.md` — reusable moves with the owner's verdicts; add new ideas there,
  general (never song-specific), and update the status when the owner judges one.
- **Choosing what to listen to** → `references/inspo.md` — reference tracks grouped by
  lesson; queue items need the owner's focus before a listen.

## Non-negotiables (hold these regardless of reference)

1. **The owner's material first.** Retrieval before invention; invention only in their
   grammar. Every output states provenance (which project/phrase/gesture it leans on)
   and assumptions (key detection, tempo choice).
2. **Live session / ears.** Before writing to SIDEKICK JAM or picking phrases while
   Live is up, call `hear_the_room` then pass `use_ears=True` on serving tools
   (`find_phrases`, `answer_my_loop`, `audition_leads`, `suggest_growth`,
   `grow_my_loop`, `develop_my_loop`). Default stays OFF until Booth promotion;
   `reset_ears` clears this session's keep/reject demotions. If `moment.unheard` lists
   audio clips, use the owner's existing descriptions or ask — names are not instrument
   identity. This symbolic read is not listening; the opt-in session-ears workflow
   produces source/version-scoped take cards.
3. **Close the judgment loop.** Every JAM write gets a terminal state (`keep` / `drop` /
   `abandoned` / `unjudged`). Optimise for **session survival**, not KEEP clicks — see
   `docs/CURSOR_CHAT_PLAYBOOK.md`.
4. **Critics gate creation.** Anything composed (gears 5-6) passes `score_phrase`
   (role+key aware) and the taste rerank before the owner hears it. Rejected drafts are
   logged, not shown. Never fold unproven signals into the critics themselves.
5. **Silence is a legal answer.** If the material wants restraint, deliver restraint.
   Space and repetition can be features of an idiom, not gaps in it.
6. **Describe the music in the owner's own frame.** Record how they name their genre
   and lineage in `idiom.md`, and use those words; do not substitute a scene label they
   reject.
7. **Speak producer, not code.** Diagnoses and proposals in musical language — bars,
   sections, energy, register, space — that the owner can argue with.
8. **Determinism where notes are made by machines** (seeded), judgment where they're
   made by you: each compositional choice explainable in one sentence.

## The kit (route, don't overlap)

- **sombra-brain** (this): WHO the composer is — idiom, theory, diagnosis substance.
- **a process coach** (a user-level skill, if installed): WHERE they're stuck — a
  bounded exercise (timebox + constraint + commit condition) for a starting, middling or
  finishing block.
- **challenge-deck**: WHAT to try — one provocation card, applied to the material at
  hand.

Typical chain: the coach names the block → this skill supplies the substance → a deck
card breaks the tie when the owner is circling.
