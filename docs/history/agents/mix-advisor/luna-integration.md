# gpt-5.6-luna — Mix Adviser integration prompt (Phase 3)

You are **gpt-5.6-luna** (Codex CLI) wiring component commits into an end-to-end mix pass flow.

```
Worktree: <worktrees>/integration
Branch:   feat/mix-advisor-evidence
```

**Start only after** Grok (`agent/mix-dsp-grok`), Luna-trials (`agent/mix-trials-luna`), and Composer component (`agent/mix-composer`) commits all exist. Cherry-pick those commits onto this branch.

Read before editing: `docs/agents/mix-advisor/README.md`, `mcp_server/mix_contracts.py`, `tools/mix_dsp.py`, `mcp_server/mix_trials.py`, `tools/mix_witness.py`, `tools/mix_check.py`.

## File ownership (you only)

Edit **only** shared integration surfaces:

- `mcp_server/server.py`
- `config/claude_mcp.md`
- `.gitignore`
- tracked ledger seed if required
- links/touches in `README.md`, `AGENTS.md`, `docs/CURSOR_CHAT_PLAYBOOK.md`

**Do not re-own or rewrite:**

- `tools/mix_dsp.py` (Grok)
- `mcp_server/mix_trials.py` (Luna trials)
- `tools/mix_witness.py`, `tools/mix_check.py` (Composer orchestration)
- `mcp_server/mix_contracts.py` without STOP escalation

Resolve import/wiring conflicts at the integration boundary only. **No overlapping edits** with in-flight component agents.

## Musical and safety invariants

1. **Instrument-neutral** — guitar is only an example.
2. **No scalar mix-quality score.**
3. **Maximum 3 manual moves** — `gain_db` and `pan` only; **Main bus read-only.**
4. **Measurements are evidence, not taste.**
5. **Preserve intentional abrasion/imbalance.**
6. **Audio stays local** unless `listener=True` explicitly invokes sandboxed agy.
7. **No Producer Pal writes or Max for Live.**
8. **Separate mix trial ledger** — never contaminate composing session ledger.

## Integration contract

Wire public operations (canonical stubs in `mix_contracts.py`):

| Operation | Responsibility |
|-----------|----------------|
| `analyze_mix_pass` | Validate inputs → analysis card + evidence refs |
| `propose_mix_moves` | Validate moves → trial ledger entry |
| `prepare_mix_ab` | Local before/after paths; no scalar score |
| `judge_mix_pass` | **Judgment card** (`JUDGMENT_CARD_EXAMPLE`); never a witness card |

All inputs/outputs must use `CONTRACT_VERSION = 1` card keys from `mix_contracts.py`.

**Contract change needed? STOP** — do not fork validators.

## TDD

1. Integration selftest: red then green; document both in handoff.
2. Default tests: fixtures on disk under `store/` or temp dirs; no Live/PPal/network/agy.

## Pre-commit

```bash
cd <worktrees>/integration
~/composing-sidekick/.venv/bin/python mcp_server/mix_contracts.py --selftest
# integration selftest
git diff --check
git diff --name-only
```

## Commit

One scoped commit, e.g. `feat(mix): wire analyze and judge through trial ledger`.

Fill README handoff template. After this commit, **Composer and Grok run read-only review only** — you do not run review passes.

## Launch command (this prompt)

```bash
codex -a never exec -m gpt-5.6-luna -s workspace-write -C <worktrees>/integration - < docs/agents/mix-advisor/luna-integration.md
```
