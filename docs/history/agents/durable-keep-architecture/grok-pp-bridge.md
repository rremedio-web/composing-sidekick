# Grok task contract — Producer Pal codec and bridge

You are the Wave 2 Grok agent. Work only in the Grok worktree after verifying its
branch was created from the durable-KEEP integration baseline.

## Inputs

Read `AGENTS.md`, `ROADMAP.md`, `docs/CURSOR_CHAT_PLAYBOOK.md`,
`docs/agents/durable-keep-architecture/README.md`, `mcp_server/live_write.py`,
`mcp_server/moment.py`, `mcp_server/core.py` note/PP helpers, and existing selftests.
Inventory actual Producer Pal response text and transport calls from the repository;
do not infer undocumented fields.

## Owned files

Create the smallest pure codec module and transport/session adapter needed to
consolidate the existing PP parsing and call/readback seam. The exact module names
must follow the repository naming convention and be recorded in the handoff. Do not
edit Composer’s JAM lifecycle, Luna’s server wiring, Mix Adviser files, store data,
dependencies, or public MCP surfaces.

The codec may own pure parsing/serialization of already-supported payloads and note
shapes. The adapter may own transport setup, tool calls, errors, and readback
verification by delegating to the current transport behavior. It must preserve the
existing `live_write` policy and failure shapes.

## Invariants

- No invented Producer Pal tool names, arguments, response keys, API versions, or
  success conditions.
- `CallToolResult.isError`, unreachable transport, malformed output, and readback
  mismatch remain fail-closed; never parse an error as an empty clip.
- MCP notes and normalized notes retain exact pitch, start, duration, velocity, and
  fractional Producer Pal duration fidelity.
- JAM/BOOTH track policy, readback verification, and all public result shapes remain
  unchanged.
- A helper that can work without Live remains pure and deterministic.
- Do not claim that one long-lived Producer Pal session is safe. That claim requires a
  real Live probe covering initialization, multiple calls, readback, error recovery,
  and teardown; otherwise report the boundary as unverified.

## TDD requirements

Write pure codec and adapter boundary regressions first. RED must demonstrate the
actual missing behavior, not an import typo. GREEN must be the smallest implementation
that preserves existing outputs. Use temporary fixtures and explicit fake transport
objects only where the external dependency is unavoidable; assert returned payloads,
errors, and readback decisions rather than mock call counts. Run all unaffected
module selftests and the full project selftest before handoff.

## Deliverables

- pure PP codec module;
- transport/session adapter with explicit per-call or explicitly scoped session
  lifetime and fail-closed errors;
- focused selftests and one precise commit;
- handoff listing the exact module names, evidence, and whether a real Live session
  probe was available.

## Stop conditions

Return `BLOCKED` if existing PP behavior cannot be identified without guessing, if a
public API must change, or if safe session lifetime depends on an unavailable Live
probe. Return `DONE_WITH_CONCERNS` when offline codec/adapter tests pass but the real
Live/Producer Pal boundary remains unverified.
