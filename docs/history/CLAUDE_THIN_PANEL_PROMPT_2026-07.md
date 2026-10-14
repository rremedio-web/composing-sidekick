# Claude Prompt: Thin IdeaPack Audition/Rate Panel

You are working in `~/composing-sidekick` on branch `fix/audit-depth-first`.

Build Track 1: the thinnest usable IdeaPack audition/rate panel. Do not expand scope into a dashboard, M4L device, arrangement editor, or Streamable HTTP migration yet.

## Current State

- Audit fixes are committed.
- Live read -> IdeaPack workflow is committed.
- Rating Contract is committed in `578f5a4`.
- Important files:
  - `mcp_server/core.py`
  - `mcp_server/server.py`
  - `config/claude_mcp.md`
  - `HANDOFF.md`

The Rating Contract is now:

```text
IdeaPack
  contrast_axis: lead | harmony | groove | arrangement | mixed
  rating_contract
    rating_scope       # derived from contrast_axis
    trainable_model    # derived from rating_scope
    scope_source: derived_from_contrast_axis
```

User ratings should not ask the owner to explain why they liked something. The system controls the contrast; he only picks A over B.

## Product Goal

Create a minimal local panel that makes taste re-validation less painful:

1. Load or accept an IdeaPack JSON object.
2. Show source clip metadata and `contrast_axis`.
3. Show 3-5 idea candidates with enough detail to compare them.
4. Preview audition lanes without writing to Ableton.
5. Require explicit `start_track` before preparing confirmed handoff calls.
6. Let the user choose best and worst.
7. Emit the exact MCP/tool call payload for `rate_candidates(..., contrast_axis=pack["contrast_axis"])`.

## Scope Rules

- No auto-write to Ableton.
- No user-entered `rating_scope` in the normal UI.
- No harmony/groove/arrangement training claims. If `trainable_model` is `none` or `harmony_future`, say stored/not trained.
- No full dashboard.
- No auth.
- No database.
- No new heavy frontend framework unless the repo already has one.
- Prefer a simple static HTML/JS panel or the smallest local server shape that fits the repo.

## UX Contract

The first screen should be the working panel, not a landing page.

Required controls:

- IdeaPack JSON input or loader.
- Start track numeric input.
- Scene index numeric input.
- Preview lanes button.
- Prepare handoff button, disabled or blocked until `start_track` is explicit.
- Best selector.
- Worst selector.
- Generate rating call button.

Required visible state:

- Pack ID.
- Source clip summary.
- Contrast axis.
- Rating scope and trainable model.
- Candidate list.
- Lane preview.
- Handoff calls preview.
- Rating call preview.

## Acceptance Checks

- A mixed pack displays `trainable_model: none`.
- The panel never asks the user to pick `rating_scope`.
- Confirmed audition handoff cannot be prepared without explicit `start_track`.
- The generated rating call includes `contrast_axis=pack["contrast_axis"]`.
- The UI is intentionally small and usable on desktop and mobile.

## Recommended First Implementation

Start with one local static file if practical:

```text
tools/ideapack_panel.html
```

It can be a client-side JSON workbench. Do not wire real MCP transport yet unless it is trivial. The goal is to validate the workflow shape and rating contract, not to ship the final app spine.

After implementing, update `HANDOFF.md` with what exists and what remains.
