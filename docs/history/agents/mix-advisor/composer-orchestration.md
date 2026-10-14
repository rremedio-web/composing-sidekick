# Composer 2.5 — Mix Adviser orchestration prompt

You are **Composer 2.5** implementing the Mix Adviser arc in:

```
Worktree: <worktrees>/composer
Branch:   agent/mix-composer
```

**Baseline:** `10681ba5a284ca40f2445b90ff54169557f78f4e`

Read completely before editing:

- `AGENTS.md`
- `ROADMAP.md` → Current arc
- `docs/CURSOR_CHAT_PLAYBOOK.md`
- `docs/agents/mix-advisor/README.md`
- `tools/audio_ref.py` — agy validation / `--sandbox` patterns
- `mcp_server/ledger.py` — atomic evidence conventions (compose ledger; do not reuse for mix trials)

## Your role

Own Phase 0 contracts and runbooks, then implement the Composer component (witness +
orchestration) in parallel with Grok DSP and Luna trials. Perform the **read-only**
review in `composer-review.md` after Luna integrates.

## File ownership

### Phase 0 (contracts + runbooks — you only)

Create **only** these paths; **do not edit any existing file**:

1. `mcp_server/mix_contracts.py`
2. `docs/agents/mix-advisor/README.md`
3. `docs/agents/mix-advisor/composer-orchestration.md` (this file)
4. `docs/agents/mix-advisor/grok-dsp.md`
5. `docs/agents/mix-advisor/luna-trials.md`
6. `docs/agents/mix-advisor/luna-integration.md`
7. `docs/agents/mix-advisor/composer-review.md`
8. `docs/agents/mix-advisor/grok-review.md`

### Component phase (orchestration — you only)

Edit **only**:

- `tools/mix_witness.py`
- `tools/mix_check.py`
- `docs/MIX_ADVISOR.md`
- `ROADMAP.md`

**Do not edit** `tools/mix_dsp.py`, `mcp_server/mix_trials.py`, `mcp_server/server.py`,
or integration surfaces. **No overlapping edits** with Grok or Luna.

## Musical and safety invariants

1. **Instrument-neutral** — guitar is only an example.
2. **No scalar mix-quality score.**
3. **Maximum 3 manual moves** — `gain_db` and `pan` only; **Main bus read-only.**
4. **Measurements are evidence, not taste.**
5. **Preserve intentional abrasion/imbalance** (`preserve` field).
6. **Audio stays local** unless `listener=True` explicitly invokes sandboxed agy.
7. **No Producer Pal writes or Max for Live.**
8. **Separate mix trial ledger** — never contaminate `mcp_server/ledger.py` / composing session ledger.

## Locked contract decisions (Phase 0)

- `CONTRACT_VERSION = 1`
- Inputs: local `.wav` / `.aiff` / `.aif` / `.flac` only; reject URL-like strings
- `groups`: 2–4 unique nonblank display names → local paths
- Move: `{track, param, delta, evidence_refs, predicted_effect, preserve?}`
- `param`: exactly `gain_db` or `pan`; ≤3 moves; gain Δ ∈ [-1.0, 1.0] dB; pan Δ ∈ [-0.10, 0.10]
- **Main bus read-only** — `validate_move` rejects track name `Main` (case-insensitive)
- Reject bool/NaN/inf deltas; validation returns fresh normalized dicts (never mutates callers)
- Public ops (canonical stubs in `mix_contracts.py`):
  - `analyze_mix_pass(master_path, group_paths, target='', listener=False)`
  - `propose_mix_moves(trial_id, max_moves=3)`
  - `prepare_mix_ab(trial_id, after_master_path)`
  - `judge_mix_pass(trial_id, choice, witness_rating='not_shown', kept_in_set=None)` → judgment card
- Witness card: listener sections (Audible observations, Masking hypotheses, Preserve, Candidate checks, Cannot determine)
- **No new dependencies**

## TDD (mandatory for `mix_contracts.py`)

1. Write `_selftest()` first; run red:
   ```bash
   ~/composing-sidekick/.venv/bin/python mcp_server/mix_contracts.py --selftest
   ```
2. Implement minimum validators; run green (exit 0).
3. Default tests: **no Live, Producer Pal, network, or real agy.**

## Phase 0 scope boundary

**Do not implement:** DSP, witness execution, trial persistence, A/B rendering, CLI orchestration, MCP wrappers.

**Stop** if you believe a shared contract must change — file a handoff with `Contract changes: STOP` instead of editing validators locally.

## Pre-commit

```bash
cd <worktrees>/composer
~/composing-sidekick/.venv/bin/python mcp_server/mix_contracts.py --selftest
git diff --check
git diff --name-only   # must list only owned paths for the current phase
```

## Commit

Exactly one scoped commit per phase.

Phase 0: `feat(mix): define adviser contracts and agent runbooks`

Fill the handoff template from `README.md` (base SHA, head SHA, red/green evidence).

## Dispatch order (after Phase 0 contract SHA)

All three component agents start from the **same final contract SHA** and run **in parallel**:

1. Grok DSP:
   ```bash
   cursor-agent -p --model cursor-grok-4.5-high --sandbox enabled --force --trust --workspace <worktrees>/grok "$( <docs/agents/mix-advisor/grok-dsp.md)"
   ```
2. Luna trials:
   ```bash
   codex -a never exec -m gpt-5.6-luna -s workspace-write -C <worktrees>/luna - < docs/agents/mix-advisor/luna-trials.md
   ```
3. Composer component (this worktree):
   ```bash
   cursor-agent -p --model composer-2.5 --sandbox enabled --force --trust --workspace <worktrees>/composer "$( <docs/agents/mix-advisor/composer-orchestration.md)"
   ```

**After all three component commits exist**, Luna integration:

```bash
codex -a never exec -m gpt-5.6-luna -s workspace-write -C <worktrees>/integration - < docs/agents/mix-advisor/luna-integration.md
```

Then read-only reviews (use `-s read-only` for Luna if re-running review):

```bash
cursor-agent -p --model composer-2.5 --sandbox enabled --force --trust --workspace <worktrees>/composer "$( <docs/agents/mix-advisor/composer-review.md)"
cursor-agent -p --model cursor-grok-4.5-high --sandbox enabled --force --trust --workspace <worktrees>/grok "$( <docs/agents/mix-advisor/grok-review.md)"
```
