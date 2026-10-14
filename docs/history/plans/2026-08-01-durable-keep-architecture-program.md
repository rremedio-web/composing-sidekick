# Durable-KEEP architecture program — Wave 1 baseline

Date: 2026-08-01
Coordinator/integration agent: Luna
Expected baseline: `577239cb4d423c2b1099a944852e82a3e73148a0`
Integration branch: `feat/durable-keep-architecture`

## Contract

The product invariant is: optimize ideas that survive a real composing session.
Immediate KEEP is feedback; session survival is the primary score. Any architecture
work must improve selection, close the judgment loop, measure adoption, or remove
friction from that loop. A broad `core.py` / `server.py` split and new capability
surface are parked unless a narrowly evidenced change closes this loop.

Wave 0 is complete: Mix Adviser Stage A is closed and canonical at `577239c`. Stage B
requires at least 15 judged passes across at least 5 sessions plus the documented
usefulness gate; Stage C additionally requires a measured export bottleneck; Stage D
additionally requires the Producer Pal mixer surface/readback/undo probe. Those gates
are unchanged and the Mix Adviser has a separate ledger.

## Waves

### Wave 1 — Luna baseline

Four small correctness fixes, each with a focused behavioral regression before the
production edit:

- fresh nested state paths create the lock parent before `_locked_update` opens it;
- answer-boundary Ears directions canonicalize once, so `Darker` behaves exactly like
  `darker`, including announcement and gate behavior;
- MCP-shaped `start_beats` / `duration_beats` notes are normalized once before Ears
  gates call pure moment helpers;
- `ledger.summary()` truthfully says `close_session()` assigns terminal states while
  Live is reachable and never reverts to the stale “not built yet” claim.

No external MCP name, signature, response shape, Live write policy, ledger schema, or
rate math may change.

### Wave 2 — parallel component ownership

- **Composer:** one JAM suggestion lifecycle module. It must cover the six existing
  JAM write paths (`answer_my_loop`, `three_endings`, `apply_audio_ref`,
  `beat_from_ref`, SOMBRA `/sombra/answer`, and SOMBRA `/sombra/endings`) and the two
  judge paths (MCP `judge_answer` and SOMBRA KEEP/DROP), preserving exact result and
  ledger semantics.
- **Grok:** a pure Producer Pal codec module plus a transport/session adapter. It may
  consolidate existing parsing and call/readback behavior but must not claim that one
  long-lived Producer Pal session is safe without a real Live probe.

The component agents do not invent Live or Producer Pal APIs. Existing tool names,
arguments, response shapes, write gates, readback requirements, and failure behavior
are inputs, not suggestions.

### Wave 3 — Luna integration

Cherry-pick the two component commits into the integration branch, resolve only
boundary conflicts, run an independent cross-review of every owned seam, then run the
full offline and project gates. Do not silently rewrite a component to hide a review
finding; send it back to the owner or stop with a concrete escalation.

### Wave 4 — Ears admission redesign

Deferred until the Ears promotion arc resumes. No default flip, new Ears gate, or
promotion claim belongs in Waves 1–3.

### Wave 5 — Mix Adviser Stage B architecture

Deferred until the Stage B evidence gate is met: at least 15 judged passes across at
least 5 sessions and the documented usefulness gate passes. Mix trials remain outside
the composing KEEP denominator.

## Branches, worktrees, and ownership

Verify each path and branch before dispatch; do not create a second agent in an active
worktree.

| Phase | Worktree | Branch | Ownership |
|---|---|---|---|
| Wave 1 / Wave 3 | `<worktrees>/integration` | `feat/durable-keep-architecture` | Luna: integration surfaces and Wave 1 fixes |
| Wave 2 Composer | `<worktrees>/composer` | `agent/durable-keep-composer` | JAM lifecycle only |
| Wave 2 Grok | `<worktrees>/grok` | `agent/durable-keep-grok` | Pure PP codec and transport/session adapter only |

Wave 1 Commit 1 owns only the five new docs plus the one-line `AGENTS.md` link and
compact `ROADMAP.md` sequence. Wave 1 Commit 2 owns only the four production fixes
and their focused selftest regressions in the existing selftest surface.

## TDD and verification

For every code fix: add the smallest behavioral assertion, run it and capture a
non-zero RED result for the intended defect, make the smallest production change, and
rerun the focused check GREEN. A passing test written after the fix is not RED
evidence. Tests must be deterministic and must not require Live, Producer Pal,
network, audio, or generated data beyond the repository baseline.

Use:

```bash
~/composing-sidekick/.venv/bin/python mcp_server/server.py --selftest
git diff --check
git status --short --branch
git log -2 --oneline
```

If the parent environment blocks the localhost UDP selftest, keep the test unchanged,
run every unaffected focused/module check, record the exact environment error, and
leave the parent-environment gate to the controller.

## Commit and report format

Commit 1: `docs(durable-keep): add architecture program and operator runbook`
Commit 2: `fix(durable-keep): close Wave 1 correctness seams`

Each handoff records base/end SHA, exact changed files, RED command and first failure,
GREEN command and result, invariants checked, unavailable Live/Producer Pal boundaries,
and one of `DONE`, `DONE_WITH_CONCERNS`, or `BLOCKED`. The Wave 1 report is
`/tmp/composing-sidekick-luna-wave1-report.md`.

## Review, conflict, and rollback rules

Reviews are read-only unless a separate implementation task explicitly authorizes a
fix. Reviewers inspect behavioral seams and exact diffs, not commit narratives or
generic green output. On a conflict, stop at the boundary, identify the owning branch,
preserve the public contract, and escalate with the file, symbols, and competing
invariants. Stop if a change would require a new MCP surface, a schema/rate change, a
Live/Producer Pal API assumption, an unproven persistent session, or a broad split.

Rollback is by reverting the scoped commit before integration proceeds. Never reset a
shared worktree destructively, delete store data, weaken a failing test, or “fix” a
Live/Producer Pal failure by fabricating a successful response.
