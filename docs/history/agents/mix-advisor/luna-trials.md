# gpt-5.6-luna — Mix Adviser trial persistence prompt (Phase 2)

You are **gpt-5.6-luna** (Codex CLI) implementing trial persistence for the Mix Adviser.

```
Worktree: <worktrees>/luna
Branch:   agent/mix-trials-luna
```

Branch from the **final Phase 0 contract SHA** on `agent/mix-composer`. Run **in parallel** with Grok DSP and Composer component — do not wait for Grok.

Read before editing: `docs/agents/mix-advisor/README.md`, `mcp_server/mix_contracts.py`, `mcp_server/ledger.py` (atomic/flock patterns — **new** mix ledger file only).

## File ownership (you only)

Edit **only**:

- `mcp_server/mix_trials.py`

**Do not edit:**

- `mcp_server/mix_contracts.py`
- `tools/mix_dsp.py` (Grok DSP)
- `tools/mix_witness.py`, `tools/mix_check.py` (Composer orchestration)
- `mcp_server/server.py`, `config/claude_mcp.md` (Luna integration)
- `mcp_server/ledger.py` (composing session ledger)
- `docs/agents/mix-advisor/*` unless assigned

**No overlapping edits.** One agent in flight per worktree.

## Musical and safety invariants

1. **Instrument-neutral** — guitar is only an example.
2. **No scalar mix-quality score.**
3. **Maximum 3 manual moves** — `gain_db` and `pan` only; **Main bus read-only.**
4. **Measurements are evidence, not taste.**
5. **Preserve intentional abrasion/imbalance.**
6. **Audio stays local** unless `listener=True` explicitly invokes sandboxed agy.
7. **No Producer Pal writes or Max for Live.**
8. **Separate mix trial ledger** — new store path; never write to `store/session_ledger.json`.

## Contract usage

All persisted moves and trial cards must pass `mix_contracts.validate_moves` / validators before write.

Operations to implement (signatures in `mix_contracts.py`):

- `propose_mix_moves(trial_id, max_moves=3)`
- Trial card shape: `TRIAL_CARD_EXAMPLE`

**If `mix_contracts.py` must change, STOP** — hand off to Composer; do not edit the contract locally.

## TDD

1. Selftest red → green with captured commands in handoff.
2. No Live, Producer Pal, network, or real agy in default tests.
3. Use temp directories for ledger I/O in tests.

## Pre-commit

```bash
cd <worktrees>/luna
~/composing-sidekick/.venv/bin/python mcp_server/mix_contracts.py --selftest
# phase selftest
git diff --check
git diff --name-only   # only mcp_server/mix_trials.py
```

## Commit

One scoped commit, e.g. `feat(mix): persist trial proposals in mix ledger`.

Fill README handoff template. **Stop after Phase 2** — integration is a separate prompt (`luna-integration.md`).

## Launch command (this prompt)

```bash
codex -a never exec -m gpt-5.6-luna -s workspace-write -C <worktrees>/luna - < docs/agents/mix-advisor/luna-trials.md
```
