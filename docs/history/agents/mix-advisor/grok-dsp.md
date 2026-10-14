# cursor-grok-4.5-high — Mix Adviser DSP / witness prompt (Phase 1)

You are **cursor-grok-4.5-high** implementing DSP measurement for the Mix Adviser.

```
Worktree: <worktrees>/grok
Branch:   agent/mix-dsp-grok
```

Branch from the **final Phase 0 contract SHA** on `agent/mix-composer` (same baseline as Luna-trials and Composer component).

Read before editing: `docs/agents/mix-advisor/README.md`, `mcp_server/mix_contracts.py`, `tools/audio_ref.py` (validation/sandbox patterns).

## File ownership (you only)

Edit **only**:

- `tools/mix_dsp.py`

**Do not edit:**

- `mcp_server/mix_contracts.py` (shared contract)
- `mcp_server/mix_trials.py` (Luna trials)
- `tools/mix_witness.py`, `tools/mix_check.py` (Composer orchestration)
- `mcp_server/server.py`, `config/claude_mcp.md` (Luna integration)
- `mcp_server/ledger.py` or composing-session paths
- `docs/agents/mix-advisor/*` unless explicitly assigned

**No overlapping edits** with Composer or Luna. One agent in flight per worktree.

## Musical and safety invariants

1. **Instrument-neutral** — guitar is only an example.
2. **No scalar mix-quality score.**
3. **Maximum 3 manual moves** — `gain_db` and `pan` only; **Main bus read-only.**
4. **Measurements are evidence, not taste** — emit `evidence_refs`, not verdicts.
5. **Preserve intentional abrasion/imbalance** — respect move `preserve` text.
6. **Audio stays local** unless `listener=True` explicitly invokes sandboxed agy (`--sandbox` when calling agy on untrusted audio).
7. **No Producer Pal writes or Max for Live.**
8. **Separate mix trial ledger** — never contaminate the composing session ledger.

## Contract usage

Import validators from `mcp_server.mix_contracts`:

- `validate_local_audio_path`, `validate_groups`, `validate_move`, `validate_moves`
- Card shapes: `ANALYSIS_CARD_EXAMPLE`, `MOVE_CARD_EXAMPLE`, `TRIAL_CARD_EXAMPLE`, `WITNESS_CARD_EXAMPLE`, `JUDGMENT_CARD_EXAMPLE`
- Canonical stubs: `analyze_mix_pass`, `propose_mix_moves`, `prepare_mix_ab`, `judge_mix_pass`
- `CONTRACT_VERSION` must match; do not fork schemas.

**If a shared contract must change, STOP** — return handoff with `Contract changes: STOP`; do not patch `mix_contracts.py` locally.

## TDD

1. Add module `_selftest()` or extend the phase selftest; run **red** (capture exit ≠ 0).
2. Implement minimum DSP code; run **green** (exit 0).
3. Tests must **not** require Live, Producer Pal, network, or real agy by default.

## Implementation notes

- Measurements feed `analyze_mix_pass` evidence refs (RMS, crest, correlation, etc.) — not a single quality number.
- Follow atomic write patterns from `mcp_server/ledger.py` for any new mix-trial store (separate path under `store/`).

## Pre-commit

```bash
cd <worktrees>/grok
~/composing-sidekick/.venv/bin/python mcp_server/mix_contracts.py --selftest
# plus your phase selftest command
git diff --check
git diff --name-only   # only tools/mix_dsp.py
```

## Commit

One scoped commit, e.g. `feat(mix): add measurement witness for analyze_mix_pass`.

Fill the README handoff template (base/head SHA, red/green commands). **Stop after your phase** — do not start Luna's work.

## Launch command (this prompt)

```bash
cursor-agent -p --model cursor-grok-4.5-high --sandbox enabled --force --trust --workspace <worktrees>/grok "$( <docs/agents/mix-advisor/grok-dsp.md)"
```
