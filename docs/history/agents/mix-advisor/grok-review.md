# cursor-grok-4.5-high — Mix Adviser read-only review prompt

You are **cursor-grok-4.5-high** performing a **read-only** DSP/measurement review after Luna integration.

```
Worktree: <worktrees>/integration
Branch:   feat/mix-advisor-evidence
```

## Rules

- **Do not edit any file.**
- Report contract gaps; recommend STOP if `mix_contracts.py` must change — never patch it in review.

Read: `docs/agents/mix-advisor/README.md`, `mcp_server/mix_contracts.py`, `tools/mix_dsp.py`, integration diff.

## Musical and safety invariants (checklist)

1. **Instrument-neutral** — guitar is only an example.
2. **No scalar mix-quality score.**
3. **Maximum 3 manual moves** — `gain_db` and `pan` only; **Main bus read-only.**
4. **Measurements are evidence, not taste** — evidence_refs are structured, not ad-hoc prose scores.
5. **Preserve intentional abrasion/imbalance.**
6. **Audio stays local** unless `listener=True` with sandboxed agy (compare `tools/audio_ref.py` `_agy_sandbox_enabled`).
7. **No Producer Pal writes or Max for Live.**
8. **Separate mix trial ledger** — composing ledger untouched.

## Verification (read-only)

```bash
cd <worktrees>/integration
~/composing-sidekick/.venv/bin/python mcp_server/mix_contracts.py --selftest
# run any Grok phase selftest command documented in the implementing commit
git diff --check
```

## Launch command (this prompt)

```bash
cursor-agent -p --model cursor-grok-4.5-high --sandbox enabled --force --trust --workspace <worktrees>/integration "$( <docs/agents/mix-advisor/grok-review.md)"
```

## Deliverable

- Measurement → `evidence_refs` mapping audit
- Witness card shape vs `WITNESS_CARD_EXAMPLE` (listener sections)
- Judgment card shape vs `JUDGMENT_CARD_EXAMPLE` (not witness card)
- DSP edge cases (NaN/bool deltas rejected at contract boundary; Main bus rejected)
- Blockers vs nits
- **No commit** in this pass

Contract change needed: **STOP** — escalate to Composer orchestration; do not edit `mix_contracts.py` locally.
