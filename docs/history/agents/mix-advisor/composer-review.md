# Composer 2.5 — Mix Adviser read-only review prompt

You are **Composer 2.5** performing a **read-only** review after Luna integration.

```
Worktree: <worktrees>/integration
Branch:   feat/mix-advisor-evidence
```

## Rules

- **Do not edit any file.** Review only: `git diff`, `git log`, selftest output.
- If you find a **contract violation**, report it and recommend STOP + contract revision — do not patch code in this pass.

Read: `docs/agents/mix-advisor/README.md`, `mcp_server/mix_contracts.py`, and the diff since Phase 0 baseline.

## Musical and safety invariants (checklist)

1. **Instrument-neutral** — guitar is only an example.
2. **No scalar mix-quality score** introduced.
3. **Maximum 3 manual moves** — `gain_db` and `pan` only; **Main bus read-only.**
4. **Measurements are evidence, not taste.**
5. **Preserve intentional abrasion/imbalance** honored in move handling.
6. **Audio stays local** unless `listener=True` + sandboxed agy path is explicit and gated.
7. **No Producer Pal writes or Max for Live.**
8. **Separate mix trial ledger** — no writes to `store/session_ledger.json` / composing ledger.

## Verification commands (read-only)

```bash
cd <worktrees>/integration
~/composing-sidekick/.venv/bin/python mcp_server/mix_contracts.py --selftest
git diff --check
git log --oneline <phase0_head>..HEAD
```

## Launch command (this prompt)

```bash
cursor-agent -p --model composer-2.5 --sandbox enabled --force --trust --workspace <worktrees>/integration "$( <docs/agents/mix-advisor/composer-review.md)"
```

## Deliverable

Markdown review with:

- Contract compliance (yes/no per invariant)
- TDD evidence present in commits (red/green noted in messages or handoffs)
- File ownership respected (no cross-phase collisions)
- Blockers vs nits
- **No commit** unless the owner explicitly asks for fixes in a new implementing phase

If shared contract must change: state **STOP — needs `mix_contracts.py` revision**; do not edit locally.
