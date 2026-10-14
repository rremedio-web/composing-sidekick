# Mix Adviser — multi-agent operating guide (Phase 0+)

Executable runbook for evidence-first mix trials. **Not** a composing-session tool —
never write to SIDEKICK JAM/BOOTH, Producer Pal, or `store/session_ledger.json`.

## Delivery topology

All component branches start from the **final contract SHA** after Phase 0 correction.
Grok and Luna-trials run **in parallel** from that SHA. Composer may continue its
component work in parallel from the same SHA. Integration starts **only after** all three
component commits exist.

| Agent / phase | Worktree | Branch | Owns (only) |
|---------------|----------|--------|-------------|
| **Composer 2.5** (orchestration) | `<worktrees>/composer` | `agent/mix-composer` | `tools/mix_witness.py`, `tools/mix_check.py`, `docs/MIX_ADVISOR.md`, `ROADMAP.md` |
| **cursor-grok-4.5-high** (DSP) | `<worktrees>/grok` | `agent/mix-dsp-grok` | `tools/mix_dsp.py` |
| **gpt-5.6-luna** (trials) | `<worktrees>/luna` | `agent/mix-trials-luna` | `mcp_server/mix_trials.py` |
| **gpt-5.6-luna** (integration) | `<worktrees>/integration` | `feat/mix-advisor-evidence` | `mcp_server/server.py`, `config/claude_mcp.md`, `.gitignore`, tracked ledger seed if required, and links/touches in `README.md`, `AGENTS.md`, `docs/CURSOR_CHAT_PLAYBOOK.md` |

**Phase 0** (contracts + runbooks) lives on `agent/mix-composer` and owns only:

- `mcp_server/mix_contracts.py`
- `docs/agents/mix-advisor/*`

**No overlapping edits.** One agent in flight per worktree. Integration cherry-picks
Grok, Luna-trials, and Composer component commits — it does **not** re-own DSP, trials,
witness, or orchestrator modules.

Baseline for this arc: `10681ba5a284ca40f2445b90ff54169557f78f4e`.

## Musical and safety invariants (every agent, every phase)

1. **Instrument-neutral** — guitar is only an example; contracts and prompts name roles/tracks, not fixed instruments.
2. **No scalar mix-quality score** — measurements are evidence refs, not a single “mix grade”.
3. **Maximum 3 automatic moves** — measured `gain_db` only in v1; `pan` remains
   contract-valid but parked until DSP emits defensible evidence; **Main bus is read-only**.
4. **Measurements are evidence, not taste** — every move cites `evidence_refs`; predicted_effect is hypothesis, not verdict.
5. **Preserve intentional abrasion/imbalance** — optional `preserve` on moves; do not “fix” character by default.
6. **Audio stays local** unless `listener=True` explicitly invokes sandboxed agy (same sandbox posture as `tools/audio_ref.py`: untrusted input, no network in default tests).
7. **No Producer Pal writes or Max for Live** in this arc.
8. **Separate mix trial ledger** — never contaminate the composing session ledger (`mcp_server/ledger.py`).

Shared contracts live in `mcp_server/mix_contracts.py`. **If a phase needs a contract change, stop and escalate** — do not fork validators locally.

## TDD red / green (required evidence)

Every phase that touches Python:

1. **Red** — extend or add `_selftest()` in the owned module; run the phase selftest command; capture **non-zero exit** or failing assertion output.
2. **Green** — implement minimum code; rerun until **exit 0**.
3. Record both commands and outcomes in the handoff template.

Default selftest command (Phase 0 contracts):

```bash
~/composing-sidekick/.venv/bin/python tools/mix_dsp.py --selftest
~/composing-sidekick/.venv/bin/python tools/mix_witness.py --selftest
~/composing-sidekick/.venv/bin/python tools/mix_check.py --selftest
~/composing-sidekick/.venv/bin/python mcp_server/mix_contracts.py --selftest
~/composing-sidekick/.venv/bin/python mcp_server/mix_trials.py --selftest
```

**Default tests must not** require Live, Producer Pal, network, or a real `agy` call.

## Handoff template (copy verbatim, fill every field)

```
## Mix Adviser handoff

- **Agent / model:**
- **Phase:**
- **Base SHA:**
- **Head SHA:**
- **Files touched:**
- **Red command:**
- **Red result:** (exit code + first failing line)
- **Green command:**
- **Green result:** (exit code; must be 0)
- **Assumptions:**
- **Unverified paths:** (files or runtime deps not exercised by selftest)
- **Contract changes:** none | STOP — needs shared contract revision
```

## Phase map (read the matching prompt file)

| Order | Agent | Doc | Role |
|-------|-------|-----|------|
| 0 | Composer 2.5 | (this README + inline task) | Contracts + runbooks |
| 1a | cursor-grok-4.5-high | `grok-dsp.md` | DSP / measurement witness (`tools/mix_dsp.py`) |
| 1b | gpt-5.6-luna | `luna-trials.md` | Trial persistence (`mcp_server/mix_trials.py`) — **parallel with Grok** |
| 1c | Composer 2.5 | `composer-orchestration.md` | Witness + orchestration — **parallel with Grok/Luna-trials** |
| 2 | gpt-5.6-luna | `luna-integration.md` | Cherry-pick + wire shared surfaces — **after all three component commits** |
| 3 | Composer 2.5 | `composer-review.md` | Read-only review |
| 4 | cursor-grok-4.5-high | `grok-review.md` | Read-only review |

Orchestration details: `composer-orchestration.md`.

## Launch commands

### Composer 2.5 (orchestration / Phase 0 / component / review)

```bash
cursor-agent -p --model composer-2.5 --sandbox enabled --force --trust --workspace <worktrees>/composer "$( <docs/agents/mix-advisor/composer-orchestration.md)"
```

Read-only review:

```bash
cursor-agent -p --model composer-2.5 --sandbox enabled --force --trust --workspace <worktrees>/integration "$( <docs/agents/mix-advisor/composer-review.md)"
```

### cursor-grok-4.5-high (DSP / review)

```bash
cursor-agent -p --model cursor-grok-4.5-high --sandbox enabled --force --trust --workspace <worktrees>/grok "$( <docs/agents/mix-advisor/grok-dsp.md)"
```

Read-only review:

```bash
cursor-agent -p --model cursor-grok-4.5-high --sandbox enabled --force --trust --workspace <worktrees>/integration "$( <docs/agents/mix-advisor/grok-review.md)"
```

### gpt-5.6-luna (trials)

```bash
codex -a never exec -m gpt-5.6-luna -s workspace-write -C <worktrees>/luna - < docs/agents/mix-advisor/luna-trials.md
```

### gpt-5.6-luna (integration — after all component commits)

```bash
codex -a never exec -m gpt-5.6-luna -s workspace-write -C <worktrees>/integration - < docs/agents/mix-advisor/luna-integration.md
```

Read-only Luna review passes use `-s read-only`, not `workspace-write`.

## Pre-commit checklist (every implementing agent)

```bash
~/composing-sidekick/.venv/bin/python mcp_server/mix_contracts.py --selftest
git diff --check
git diff --name-only
```

Confirm **only** paths owned by the current phase changed. One scoped commit per phase.

## Contract quick reference

- `CONTRACT_VERSION = 1`
- Local audio only: `.wav`, `.aiff`, `.aif`, `.flac` — reject URL-like strings
- `groups`: 2–4 unique nonblank display names → local paths
- Move: `{track, param, delta, evidence_refs, predicted_effect, preserve?}`
- `param`: contract accepts `gain_db` | `pan`; automatic v1 proposals emit measured
  `gain_db` only; ≤3 moves; gain Δ ∈ [-1, 1] dB; pan Δ ∈ [-0.10, 0.10]
- **Main/master bus read-only** — `validate_move` rejects either name (case-insensitive)
- Public ops (canonical stubs in `mix_contracts.py`):
  - `analyze_mix_pass(master_path, group_paths, target='', listener=False)`
  - `propose_mix_moves(trial_id, max_moves=3)`
  - `prepare_mix_ab(trial_id, after_master_path)`
  - `judge_mix_pass(trial_id, choice, witness_rating='not_shown', kept_in_set=None)` → **judgment card**, not witness card
- Witness card sections: Audible observations, Masking hypotheses, Preserve, Candidate checks, Cannot determine
