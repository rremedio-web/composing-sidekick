# EXAMPLE — piece notes (template)

One file per **piece**, not per Set file: a piece spans every confirmed version
(`EXAMPLEv1.als`, `EXAMPLEv2.als`, …). Register the piece and bind its Sets with
`declare_piece_version(…, confirm=True)` or `tools/declare_piece.py --spec` (format:
`tools/piece_spec.example.json`), and point `notes_path` at this file. `piece_brief()`
reads the two tables below by their heading prefixes; everything else is read directly.

## Current checkpoint

- Version: `EXAMPLEv2.als` (confirmed by the owner, date)
- Tempo / key as Live reports them; the owner's intent in their own words, dated
- Active, unjudged experiments on `SIDEKICK …` tracks, with the bars to audition
- Undo and evidence paths (journals, before/after snapshots)

## Suggestions

| # | Suggestion | Status |
|---|---|---|
| 1 | A proposed audition, with its `proposal_id` and the bars it touches | open |
| 2 | A second option, one varied dimension | awaiting owner |

Statuses `open` and `awaiting owner` become open questions in the brief. Other
statuses (`kept`, `dropped`, `withdrawn`) stay as table data; none creates a KEEP/DROP —
verdicts come only from the owner through `judge_answer`.

## Cuts and transitions

| Join | Edit | Status |
|---|---|---|
| A to B (bar 64) | Proposed transition; original preserved on its own track | awaiting owner |

## The owner's words

Verbatim, dated, scoped (section bars / track / candidate / piece / global). Keep the
model's reading in a separate paragraph, never merged into the quote.

## Observations (model-heard / measured)

Dated, bound to the exact Set or bounce they came from, with the evidence type
(`set_file`, `measured`, `model_heard`, `inference`). Observed is not judged.

## History

Earlier checkpoints, oldest last; link journals instead of pasting dumps.
