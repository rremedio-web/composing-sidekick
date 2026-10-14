# Knowledge guide for every composing assistant

Applies to Codex, Claude, Gemini, Fable, Cursor, and other clients using this
workspace. Read after [the chat playbook](CURSOR_CHAT_PLAYBOOK.md). This guide
describes existing storage and retrieval; it does not introduce a new database,
automatic document ingestion, or authority to edit Live.

## Start a session with the right evidence

1. Read the owner's current request and preserve its scope and existing authorization.
2. Read `store/NOTES.md` (the owner's own notes file) directly, then use `get_style()` for catalogue
   context. Notes contain labelled owner statements **and** research candidates;
   retain those labels. Statistics describe a corpus, not rules for every piece.
3. For a named piece, call `piece_brief(set_path, live=False)` and open its
   `notes.path` directly. The brief only extracts two Markdown tables; a successful
   read with empty `sections` does not mean the notes contain nothing useful.
4. For Live work, verify the current Set's exact file path and read the relevant
   playing context before drawing conclusions. Confirmed piece versions share
   intent, but clip IDs, edits, snapshots and audio belong to their exact Set/version.
5. Load the relevant [sombra-brain reference](../.claude/skills/sombra-brain/SKILL.md).
   Its location under `.claude/` does not make it Claude-only. Separate the owner's
   idiom, borrowed techniques, hypotheses, and judged experiments.
6. For references, use `find_audio_refs(...)`, inspect the selected full card and
   provenance, and state the specific musical job it serves. For written research,
   read [the research index](research/README.md) and the original linked source.

For an offline research/docs task, skip Live reads, captures, listener calls and
session-close actions. Reading a guide is not authorization to run its write tools.
Use current tool schemas and [Producer Pal version guidance](PRODUCER_PAL_VERSIONS.md);
do not copy old model names, tool counts, or API examples out of dated handoffs.

## What has authority

**Intent and taste:** the owner's current, scoped instructions and corrections take
precedence over older general preferences, catalogue averages, model opinions and
genre conventions. Keep his words separate from an assistant's interpretation.
An observation that a track is muted is not a preference or a DROP.

**Technical state:** a fresh read establishes the actual Set, loop windows, mutes,
bypasses, automation, routing and IDs. An earlier snapshot is historical evidence.
MIDI describes notes that may play after playback-window and activation checks;
it does not establish the resulting timbre, masking, impact or emotional effect.

**Audio:** a listener's successful file-access receipt establishes access, not
musical accuracy. `listen_verified: true` and a valid card schema are not owner
approval. Measurements support their measured quantity; crest factor is not proof
of unmasked instruments, and an FFT peak is not automatically a played fundamental.
A raw source-region listen excludes the effects of the current track chain.

**External research:** identify the author/artist, exact work or document, source
URL or file, date/version, and the claim supported. An artist interview, a critic's
interpretation, a model listening report and our proposed experiment are different
evidence. Never attach an artist's name retroactively to an unrelated change.

## Storage and retrieval map

| Knowledge | Existing home | How an assistant obtains it | Update/authority boundary |
|---|---|---|---|
| Owner statements and general musical context | `store/NOTES.md` | Direct file read; selected code paths also parse it | Preserve quotes and scope; public research candidates are not owner-confirmed taste |
| Generated catalogue profile | `store/style_profile.json`, `STYLE.md`, `harmony.json`, `structure.json`, `motifs.json` | `get_style()` and catalogue tools | Generated analysis; inspect build metadata, avoid hand-editing measurements |
| Original symbolic catalogue | `store/catalog_manifest.json`, `store/projects/`, `store/phrase_index.json`, arrangement/phrase-curve artifacts | Phrase retrieval, project records and targeted file reads | Preserve source project/clip provenance; distinguish versions, takes, pack/demo material and assistant additions |
| Audio references | `store/audio_refs/<ref_id>/card.json`, `meta.json`, `describe.txt`, source/features/MIDI artifacts | `find_audio_refs`, full card, `apply_audio_ref` | Validated `card.json` is canonical when present; `meta.json` carries operator overlays; prose is a rendering/fallback |
| Audio reference search | `store/audio_ref_index.json` | `find_audio_refs` | Derived search view plus overlays; use supported update/reindex tools, not independent hand edits |
| Session audio takes | `store/session_ears/` | Inventory/listener workflow and identity-checked take-card folding | Exact source region, transforms, Set identity, listener and limitations matter |
| Piece/version identity | `store/pieces.json` | `declare_piece_version`, `piece_brief` | Bind versions on the owner's confirmation; never infer identity from a similar filename |
| Piece goals and decisions | `store/piece_decisions.json` | Piece context/brief | Intent can inherit across confirmed versions; Set-specific identities/verdicts cannot |
| Proposals, statements, reactions, observations | `store/piece_records.json` | `record_proposal`, `record_reaction`, `record_observation`, `piece_brief` | Preserve source type, exact words, scope and proposal/Set linkage |
| Human-readable piece narrative | `docs/pieces/<PIECE>.md` (template: `docs/pieces/EXAMPLE.md`) or registered `notes_path` | Direct read; selected tables via `piece_brief` | Current checkpoint first, history dated; link exact journals instead of copying large dumps |
| Written theory and borrowed techniques | `.claude/skills/sombra-brain/references/`, `docs/research/` | Explicit file reads | Not automatically returned by `get_style` or `find_audio_refs` |
| Candidate identity, verdict and survival | `store/session_ledger.json`; ratings/taste artifacts | Registration, `judge_answer`, `close_session`, reports | the owner judges; inventory establishes survival; unresolved is not DROP |
| VST/M4L manuals and production research | `docs/research/` for curated notes; installed device files remain external | Research index + official manual + current installed-state read | No dedicated plugin-knowledge retrieval API currently; installation is not proof of licence, compatibility or CPU cost |
| Local experiments and large receipts | `.scratch/` | Explicit linked paths | Git-ignored working evidence; not the only durable home of an important musical decision |

Assistant-specific memories and chat transcripts can help locate evidence. They
are not a replacement for these shared stores and do not automatically reach
another assistant. Do not imply that adding a file trains a model.

## Known retrieval limits and how to work around them

### Style notes can disagree

`suggest_structure()` emits a seeded skeleton based on catalogue aggregates,
with intro/section/breakdown/outro role assumptions. It is an optional comparison,
not the current piece's intended form; current owner instructions outrank it.
If an owner rhythm library is present (`store/approved_rhythms.json`, optional and
local), its formulas in `get_style()` describe one approved vocabulary, not a universal
ban on other tempos, density or drum forms.

`get_style()` reads the generated `STYLE.md`; its embedded notes are a copy made
when the profile is built. It does not freshly append `NOTES.md` on every call.
Read `NOTES.md` directly and flag discrepancies. Do not rebuild the whole corpus
just to make one correction visible. Some structured artifacts are cached for the
server process lifetime; a disk update alone is not proof that a running tool has
reloaded them. Verify the returned revision/build information after an authorized
refresh. Code: [get_style](../mcp_server/core.py),
[loaders](../mcp_server/core_shared.py), [profile writer](../analyze/style_profile.py).

### A piece brief is not the whole notes file

The current parser recognises level-two headings starting with `Suggestions` or
`Cuts and transitions`, and their pipe tables. For predictable extraction use:

```markdown
## Suggestions

| # | Suggestion | Status |
|---|---|---|
| 1 | A proposed audition, with its proposal ID | open |

## Cuts and transitions

| Join | Edit | Status |
|---|---|---|
| A to B | Proposed transition; original preserved | awaiting owner |
```

Only `open` and `awaiting owner` currently become table-based open questions.
Other statuses remain table data; none automatically create a KEEP/DROP.
Keep musical narrative under other headings and read it directly. The structured
proposal/reaction records supplement these tables; they do not ingest arbitrary
prose. Code: [piece continuity](../mcp_server/piece_continuity.py).

### Reference search is a selection aid

Current search uses weighted words in title/identity, tags, rules, owner notes,
description and structure, plus filters. It does not search PDFs or all repository
Markdown. Missing results do not prove that a subject is absent from research notes.
Derived moments and marked-suspect cards are excluded by default, but an erroneous
card that was never marked can still appear. Inspect contradictions before use.

Read the current `ricardo_note`, uncertainty, ownership, source/version and suspect
state. Do not apply a contradictory card merely because retrieval returned it.
Supported suspect/tag operations preserve overlays in metadata and the index;
reindexing is not a factual correction or a new listen. Back up overlays before an
authorized reindex. Code: [audio reference tools](../tools/audio_ref.py).

Direct lookup and search inherit a parent card's suspect flag to its derived
moments. That inherited status is computed, not a new owner verdict or proof that
a moment is wrong. Explicit child flags remain independent. Restart older MCP
processes before relying on a changed lookup.

### Provenance must stay scoped

Preserve the difference between a source recording, a live-performance Set,
a library/demo Set, a rendered bounce and the currently open arrangement. A Set
can explain how one version was built without describing a different recording.
Stored notes outside a clip's playing window do not sound. A bounce from v2 cannot
validate changes made later in v3.

### Stored MIDI is not a playback transcript

Before claiming that a note or chord plays, check clip activation (including saved
`Disabled` flags), note activation, clip-time offset/start marker, loop state and
boundaries, and the arrangement span. Non-looping clips do not repeat merely
because loop braces exist. For looping clips distinguish the initial pass from
subsequent cycles, and establish phase at the actual arrangement position. Test
note-duration overlap with the played window, not just whether a note is stored.

Then check instrument/rack activation, frozen or replacement audio, routing,
track/group/solo states, automation and Session-versus-Arrangement playback. These
establish candidate events and signal paths, not audible timbre or impact. A muted
track can still supply an upstream routing tap; a self-sequencing instrument can
play without MIDI clip notes. Do not reduce audibility to a single mute boolean.
Check automation per relevant parameter rather than extrapolating from one track.

### Authored colour and proposed clashes are different decisions

Preserve the owner's authored overlaps. Apply a scoped consonance request to new
material, checking exact simultaneous notes, register, duration and musical role.
Pitch-class semitone/tritone flags are diagnostics, not authority to delete his
notes or punch holes in a repeating figure. Extensions do not automatically make
every voicing consonant; low-register intervals do not automatically establish
mud without sound evidence. Prefer changing the proposed voice or offering a
reversible alternative within the existing authorization.

### Excerpts test claims; they do not certify a listener

When a permitted listening pass yields contradictions, short excerpts can isolate
the disputed event. Use neutral questions, allow uncertainty, retain file/version
identity and map excerpt-local timestamps back to the original. Compare the same
passage across runs before attributing improvement to excerpt length. A better
local answer does not validate its other claims or the whole-track form. Retain
full-track context for composition judgments; do not start a new listen without
the applicable authorization and focus.

Full-focus reference prompts now withhold DSP change points until reconciliation;
other focuses retain labelled hints. New source ingestion and listening cuts keep
available stereo channels (PCM, up to 44.1 kHz), while measurement downmixes its
own input. Existing mono sources cannot regain width through re-staging: recover
the original before making spatial claims. Observation-only v3 cards may have
empty imitation lists; continuous/periodic repetition and null space are valid.

## Store a useful result once, then link it

- **A proposal:** use `record_proposal` with the musical question, source gesture,
  one reference job and its evidence, what stays, what changes, and candidate IDs.
- **the owner's response:** use `record_reaction` with verbatim words and scope;
  put the model's reading in `interpretation`. Set `rule=True` only for a rule he
  actually states. A reaction is not a `judge_answer` verdict.
- **A technical observation:** use `record_observation` with exact Set identity,
  IDs and snapshot evidence. CPU-related mutes/bypasses remain observations.
- **A written source or production idea:** create a sourced note following the
  [research template](research/README.md), link it from that index, and add only a
  short reusable pointer to the appropriate sombra reference. Do not copy the
  whole paper into several files or file a text-only source as an audio listen.
- **A session handoff:** update the piece's durable narrative when authorized;
  record current version, owner words, active/unjudged experiment, audition range,
  undo/evidence paths, and what remains to hear. Re-read the brief and direct notes
  to check that a fresh assistant can recover them.

Use the existing tools for structured-store writes; they enforce locking, atomic
updates and provenance. Do not hand-edit the ledger or JSON stores to manufacture
missing judgments, confirmed versions or listening receipts.

## Musical collaboration checks

- Treat paired sounds with identical MIDI as potential composite instruments.
  Establish their roles before proposing subtraction.
- Preserve owner material and timeline automation within the current authorization.
  Use reversible copies when that is the agreed workflow; retain IDs and read back
  every write. Existing authorization is not a reason to ask again.
- Respect repetition, long ambient passages, and several possible forms. A fixed
  rise/drop/decay template is an option, not the owner's compulsory song structure.
- Check proposed new notes against sounding notes and context. Do not erase his
  authored tensions or quantize his performance to satisfy a generic critic.
- Match whole-track decisions with a whole-track audition. Short loops answer
  local questions; leave transport/loop state consistent with his listening request.
- State what changed, why, whose idea/reference informed it, and what was actually
  verified. Let the owner's listening determine whether it works.

## Documentation maintenance

Keep entrypoints short and link here. Each fact should have an authoritative home;
derived summaries identify their source and date. Check current code/tool schemas
before documenting capabilities. Label proposed storage/API changes as proposals.
Documentation updates alone do not change backend retrieval, resolve suspect flags,
refresh the corpus, or migrate piece records.

Known retrieval gaps belong in a dated note under `docs/research/`; recheck its
counts and Set state before treating them as current.
