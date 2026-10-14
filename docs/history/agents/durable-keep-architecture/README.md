# Durable-KEEP architecture — operator runbook

This is the executable contract for the Wave 1 baseline and the subsequent Composer,
Grok, and Luna handoffs. It is for agents operating the local repository, not an
architecture essay.

## Product compass and locked baseline

Optimize ideas that survive the session. The durable measure is session-surviving
judged suggestions; KEEP-button rate alone is not the product target. Every written
candidate still gets the existing terminal-state treatment, and `close_session()` is
the authoritative sweep while Live is reachable.

Wave 0 Mix Adviser Stage A is complete and canonical at `577239c`. Future stages stay
exactly as `ROADMAP.md` specifies:

- Stage B: at least 15 judged passes across at least 5 sessions plus the usefulness
  gate;
- Stage C: Stage B plus a measured export bottleneck;
- Stage D: Stage C plus a passing Producer Pal mixer surface/readback/undo probe.

Do not broaden `core.py` / `server.py`, add generators, flip Ears promotion, or add a
new MCP capability unless evidence shows it closes the durable-KEEP loop. Mix trials
use their separate ledger and never enter the composing KEEP denominator.

## Landed state

Waves 1–3 landed on `feat/audio-ref-agy` at `acea9bc` on 2026-08-01:

- `d5d564c` — architecture program and operator contracts;
- `fc0f13c` — Wave 1 correctness seams;
- `beeda90` — shared JAM lifecycle;
- `233bf12` — pure PP codec plus operation-scoped session adapter;
- `acea9bc` — integration selftest migrated to the session-adapter seam.

The worktree topology below records how the implementation was produced. The
integration worktree and `feat/durable-keep-architecture` branch were removed after
the verified fast-forward; they are historical provenance, not cold-resume targets.

## Delivery map

| Wave | Owner | Required result |
|---|---|---|
| 0 | complete | Mix Adviser Stage A at `577239c`; B–D gated |
| 1 | complete — Luna | Four correctness regressions and minimal fixes at `fc0f13c` |
| 2a | complete — Composer | One JAM lifecycle module over six writes and two judge paths |
| 2b | complete — Grok | Pure PP codec plus operation-scoped session adapter |
| 3 | complete — Luna | Integrated and independently verified at `acea9bc` |
| 4 | deferred | Ears admission redesign only when its promotion arc resumes |
| 5 | deferred | Mix Stage B only after the 15-pass/5-session/usefulness gate |

## Historical worktrees and file ownership

The implementation used an integration worktree at
`<worktrees>/integration` on
`feat/durable-keep-architecture`, starting at `577239c`. The component worktrees were:

| Agent | Worktree | Branch | Owned surface |
|---|---|---|---|
| Composer | `.../composing-sidekick/composer` | `agent/durable-keep-composer` | JAM lifecycle module and its focused tests |
| Grok | `.../composing-sidekick/grok` | `agent/durable-keep-grok` | PP codec and transport/session adapter |
| Luna | `.../composing-sidekick/integration` | `feat/durable-keep-architecture` | Wave 1, cherry-pick, integration review |

Wave 1 Commit 1 changes the five new docs, `AGENTS.md`, and `ROADMAP.md`. Wave 1
Commit 2 changes only `develop_session.py`, `core.py`, `ears.py`, `ledger.py`, and the
existing `server.py` selftest blocks. Do not touch Mix Adviser files, the canonical
worktree, dependencies, generated assets, store data, or the two untracked canonical
review prompts.

## Wave 1 gate

Run one RED/GREEN cycle for each item:

1. `_locked_update` accepts a fresh nested temporary state path and creates its lock
   parent before opening the lock.
2. `answer_my_loop(..., direction="Darker", use_ears=True)` has the same gate report,
   canonical direction, and announcement behavior as lowercase `darker`.
3. Ears admission accepts MCP note dictionaries with `start_beats` and
   `duration_beats` in both register and density gates, with the same result as
   normalized `start`/`duration` notes.
4. `ledger.summary()` contains the truthful `close_session()` terminal-state claim;
   the stale “not built yet” text is forbidden.

The smallest focused probes must be offline. The full gate is:

```bash
~/composing-sidekick/.venv/bin/python mcp_server/server.py --selftest
git diff --check
git status --short --branch
git log -2 --oneline
```

## Shared invariants

- Existing MCP tool names, signatures, response shapes, Live write policy, and ledger
  schema/rate math are immutable in this program.
- Ears-off behavior remains byte-identical; Ears-on behavior fails loudly on unknown
  directions and does not invent room evidence.
- Normalize MCP notes at the admission boundary once; pure helper contracts remain
  unchanged.
- Live/Producer Pal is an external boundary. No agent may invent tool names, fields,
  transport behavior, API versions, readback semantics, or session lifetime safety.
- A dead bridge is not an empty Set. Never turn transport errors into scored drops or
  successful writes.

## Deferred review findings

These post-landing suggestions were consciously left out of the low-risk gate/docs
follow-up. They are not silently forgotten and are not new roadmap commitments:

- **SOMBRA error visibility — next real-night candidate.** A listener exception is
  currently retained only in the in-memory status ring. Prefer a concise
  `/sombra/reply` error plus `last_error` in the existing status response; do not add
  a persistent error ledger unless real sessions prove it necessary.
- **Two note-shape normalizers — retained boundary.** `core._note_dicts` serves
  general musical inputs; `pp_codec.note_dicts` keeps the PP wire module stdlib-only.
  Consolidate only if behavior drifts or a neutral shared module earns more consumers.
- **`jam_lifecycle` module alias — deferred.** The module is stateless and production
  imports use one bare path. Add a package/bare alias only if package imports become a
  real second entry path.
- **`three_endings` provenance — needs an explicit contract.** The current tool has no
  `ref_id` or other reliable provenance input, while the ledger domain is
  `catalogue`/`ref:<id>`. Never infer reference provenance from notes or track names.
- **Envelope-bridge PP migration — parked with the bridge.** Move its duplicated PP
  ceremony onto `pp_session.operation_session` only when that parked capability is
  promoted or otherwise needs production work.
- **More metrics/device acknowledgments — evidence-gated.** Same-mode streaks, ledger
  IDs in pedal replies, and additional dashboards wait for real durable-KEEP sessions
  to show a decision or recovery gap.
- **Raw `/tmp` agent reports — remain ephemeral.** Durable contracts and adjudicated
  decisions belong here; do not preserve whole agent transcripts in the repository.

## Handoff, review, and escalation

Every agent reports base/end SHA, exact files, RED and GREEN evidence, invariants,
unverified boundaries, and status: `DONE`, `DONE_WITH_CONCERNS`, or `BLOCKED`.
Read-only reviewers inspect the diff and behavioral seams independently. A conflict or
contract change is escalated with the exact symbol and evidence; do not resolve it by
rewriting another owner’s module. Roll back by reverting the scoped commit before
cherry-pick, never by destructive reset or store deletion.

Read the standalone contracts before dispatch:

- [`composer-jam-lifecycle.md`](composer-jam-lifecycle.md)
- [`grok-pp-bridge.md`](grok-pp-bridge.md)
- [`luna-integration.md`](luna-integration.md)
