# Research index and filing guide

Read the shared [knowledge guide](../KNOWLEDGE_GUIDE.md) for authority, retrieval
and storage rules. This is the manual entrypoint for written research; no MCP
tool indexes every document here or ingests arbitrary PDFs. The audio-reference
search (`find_audio_refs`) is a separate index of listening cards.

## Available research

| Material | Location | Evidence and use |
|---|---|---|
| Percussion on one reference record, 2026-09-30 | [`2026-09-29-drums/aurora-percussion.md`](2026-09-29-drums/aurora-percussion.md) | Web research with per-claim tags (sourced / review); an example of the filing format below |
| Reusable borrowed techniques | [Sombra techniques](../../.claude/skills/sombra-brain/references/borrowed-techniques.md) | Template: other artists' practices with sources, never the owner's identity |
| Listening references by lesson | [Sombra listening guide](../../.claude/skills/sombra-brain/references/inspo.md) | Template: what each reference teaches; queue items need the owner's focus before a listen |
| Earlier ecosystem survey | [July 2026 survey](../history/ecosystem_research_2026-07.md) | Historical; verify current products and APIs before recommending |

The author's own research notes (studies of his records, reference-library repairs,
listening trials on his pieces) were removed from the public repository.

## Add research that another assistant can use

File a small dated Markdown note here, with a descriptive filename. Link it in
this index. Keep original PDFs/audio in an explicitly documented local location
or link the author's/publisher's legitimate source; preserve page/section anchors.
Do not imply a paid preview is a free full book. A download without an extraction
or reading note is not yet usable guidance.

Suggested note template (editorial fields, **not a new machine schema**):

```markdown
# Specific musical question or technique

Status: research candidate / verified source / auditioned (state actual outcome)
Date checked:
Source: author/artist, title, URL or local path, page/section/timestamp
Source version: publication date, plugin version, or exact recording/Set
Evidence: owner statement / primary source / review / model-heard / measured /
          set-file / inference (keep distinct)

## Supported finding
What the source actually establishes; short quotation or faithful paraphrase.
Separate our interpretation and any unresolved contradiction.

## Musical job
The problem this could solve and the audible change we expect.
When it would be inappropriate or undermine the piece.

## Application to the owner's work
One relevant gesture or project example; original material to preserve.
One variable to audition and a success criterion the owner can judge by listening.
Link the proposal record if an experiment is filed.

## Practical implementation
Existing tools first. For M4L/VSTs: installed observation date, official manual,
version/compatibility evidence, routing, macros, latency/CPU measurements if
actually measured, print/freeze option, and a reversible audition plan.

## Feedback and corrections
The owner's words verbatim, with project/section scope and date.
Superseded claim and its replacement, with evidence; do not silently erase history.
```

Theory notes, product manuals and artist listening cards have different roles.
Cross-link them through a musical job: for example, expectation/repetition theory
→ a verified return in a recording → a bounded experiment on the owner's theme.
Keep untested recommendations out of the owner's confirmed preferences.

After filing, verify that the index link works, the source supports the claim,
and a new assistant can find the note by its musical question. Reference search
remains a separate audio-card index; don't promise automatic retrieval here.
