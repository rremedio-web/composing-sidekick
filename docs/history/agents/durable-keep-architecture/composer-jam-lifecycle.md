# Composer task contract — JAM suggestion lifecycle

You are the Wave 2 Composer agent. Work only in the Composer worktree after verifying
the branch and the integration baseline recorded in the durable-KEEP runbook.

## Inputs

Read `AGENTS.md`, `ROADMAP.md` current durable-KEEP arc, `docs/CURSOR_CHAT_PLAYBOOK.md`,
`docs/agents/durable-keep-architecture/README.md`, `mcp_server/server.py`,
`mcp_server/ledger.py`, `mcp_server/sombra_listener.py`, and the focused selftest
blocks. Treat current behavior and tests as the contract.

## Owned files

Own one small JAM suggestion lifecycle module and its focused selftests. You may edit
the six current JAM write call sites and two judge call sites only as needed to route
through that module:

1. MCP `answer_my_loop(write=True)`;
2. MCP `three_endings(write=True)`;
3. MCP `apply_audio_ref(write=True)`;
4. MCP `beat_from_ref(write=True)`;
5. SOMBRA `/sombra/answer`;
6. SOMBRA `/sombra/endings`;
7. MCP `judge_answer`;
8. SOMBRA `/sombra/keep` and `/sombra/drop`.

Do not re-own Grok’s PP codec/adapter, Luna’s integration wiring, Mix Adviser files,
store data, or public MCP signatures.

## Invariants

- JAM is the only automatic note-write track; preserve all existing confirm/readback
  and dead-Producer-Pal behavior.
- Preserve exact returned result shapes and all current `live_write`,
  `live_write_error`, `ledger`, and `ledger_error` semantics.
- Every successful JAM write is stamped exactly once with the same source, role, gear,
  ears, phrase, tempo, note-count, and session-mode values as today.
- Immediate KEEP/DROP remains distinct from terminal survival; do not change ledger
  schema or rate math.
- `judge_answer` and pedal judgments retain their existing ears/develop ordering,
  failure behavior, confirm-gated delete policy, and response fields.
- No new MCP tool, argument, response key, Live API, or Producer Pal API is invented.

## TDD requirements

Add a focused behavioral regression for each lifecycle seam before changing it. Run an
offline RED probe, implement the smallest routing change, then run the same probe GREEN
and the full selftest. Test real returned dictionaries and temporary ledgers; do not
assert only that a mock was called. Include duplicate-stamp and exception-preservation
cases where routing could accidentally double-write or mask a failed bridge.

## Deliverables

- one narrowly scoped lifecycle module;
- focused selftests covering all six writes and both judge paths;
- one precise commit and a handoff with base/end SHA, files, RED/GREEN evidence,
  invariants, unverified real Live/Producer Pal boundary, and status.

## Stop conditions

Stop and report `BLOCKED` if the change needs a public contract change, cannot prove
exact result/ledger equivalence, requires a new PP API, or depends on a real Live
probe unavailable in the environment. Use `DONE_WITH_CONCERNS` when offline behavior
is complete but a real Live boundary remains unverified.
