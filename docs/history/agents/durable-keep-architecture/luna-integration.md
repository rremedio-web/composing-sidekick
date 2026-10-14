# Luna task contract — durable-KEEP integration

You are the Wave 3 Luna integration agent. Work only in
`<worktrees>/integration` on
`feat/durable-keep-architecture`.

## Inputs

Verify the branch, current SHA, clean state, and the two component commit SHAs. Read
`AGENTS.md`, `ROADMAP.md`, `docs/CURSOR_CHAT_PLAYBOOK.md`, the durable-KEEP runbook,
both component handoffs, `mcp_server/ledger.py`, `mcp_server/live_write.py`, and all
focused/full selftest instructions.

## Owned files and sequence

Cherry-pick the Composer JAM lifecycle commit and Grok PP bridge commit. Resolve only
integration-boundary conflicts. Do not rewrite component-owned modules to make the
merge appear clean. Integration touches only the documented integration surfaces and
test wiring required to preserve the public contract; record every such file.

Then perform an independent cross-review:

- compare every six write path and two judge path against its pre-integration result
  and ledger behavior;
- inspect error/readback/live-write seams, not only unit/selftest output;
- confirm no external MCP name/signature/response shape or ledger schema/rate math
  changed;
- confirm no claim of safe long-lived PP session exists without a real Live probe.

## Invariants

The durable-KEEP compass remains session survival. JAM writes remain the only automatic
note writes, terminal states still come from `close_session()` while Live is reachable,
and Mix Adviser trials remain in their separate ledger. Ears default/promotion and
future Mix stages remain gated exactly as `ROADMAP.md` states.

Do not invent Live or Producer Pal APIs, weaken a regression, fabricate transport
success, or broaden the MCP capability surface.

## TDD and verification

Component commits must carry RED/GREEN evidence. After cherry-pick, run the smallest
affected module checks, then:

```bash
~/composing-sidekick/.venv/bin/python mcp_server/server.py --selftest
git diff --check
git status --short --branch
git log -2 --oneline
```

If localhost UDP is blocked by the parent sandbox, do not weaken or skip the relevant
test. Run unaffected focused/module checks, capture the exact environment error, and
leave the full parent-environment gate to the controller.

## Deliverables and status

- cherry-pick record and final SHA;
- exact files changed after cherry-pick;
- independent cross-review findings and resolutions;
- full gate output and explicit unverified Live/Producer Pal boundaries;
- result status: `DONE`, `DONE_WITH_CONCERNS`, or `BLOCKED`.

Use `BLOCKED` for unresolved contract conflicts or missing required component commits.
Use `DONE_WITH_CONCERNS` only when all offline gates pass and a real external boundary
is explicitly unverified.
