# Mix Adviser — operator how-to

Evidence-first mix trials: **manual export → deterministic DSP analysis → ≤3 bounded
manual move proposals → optional listener witness → loudness-matched randomized-label A/B →
judgment/report.** The CLI orchestrates only; it never touches Live, Producer Pal, or
a mixer.

**Separate ledger:** mix trials live in their own store (`mcp_server/mix_trials.py`).
They must **not** contaminate the composing session KEEP denominator
(`store/session_ledger.json`).

---

## Preconditions

1. Ableton Live is open with a mix you are willing to export (instrument-neutral —
   guitar is only an example in docs).
2. You can export a **master** stereo file and **2–4 aligned group stems** from the
   same loop/section (same length, same start point).
3. Python venv at `~/composing-sidekick/.venv` with repo on `PYTHONPATH` or run from
   repo root.
4. **Listener off by default.** Only pass `--listener` when you explicitly want a
   sandboxed `agy` call (same trust model as `tools/audio_ref.py`).

### Export from Live (manual — v1)

Exact menu labels and bounce dialogs depend on **your installed Live version**. The
machine must be probed; **do not assume API or dialog text from this doc.**

Preferred workflow (**unverified** until probed on your Live version):

1. Use Live's native export to bounce the **selected master** plus **individual tracks or
   groups** in one aligned pass — only after confirming the exact options on the owner's
   installed Live version.
2. Do **not** manufacture stems by soloing groups through a differently loaded
   master/return graph. Solo passes can change return effects, sidechains, and
   master-bus loading.
3. Export format: WAV/AIFF/FLAC; same time range for master and stems.
4. Note paths; keep names stable across before/after passes.

If the exact export surface has not been probed on this machine, mark it **unverified**
and stop — do not invent dialog or API behavior.

---

## CLI reference

Binary: `tools/mix_check.py` — JSON on **stdout**, human diagnostics on **stderr**,
nonzero exit on invalid input.

```bash
cd ~/composing-sidekick   # or your worktree
.venv/bin/python tools/mix_check.py --selftest   # offline verification
```

### `analyze`

```bash
.venv/bin/python tools/mix_check.py analyze \
  --master /path/to/master.wav \
  --group drums=/path/to/drums.wav \
  --group bass=/path/to/bass.wav \
  --target "more vocal clarity" \
  # --listener   # optional; invokes sandboxed agy
```

- **`--master`**: local `.wav` / `.aiff` / `.aif` / `.flac` only (URLs rejected).
- **`--group`**: repeat 2–4 times as `NAME=PATH`; names must be unique.
- **`--target`**: free-text intent (constraint for proposals, not a score).
- **`--listener`**: opt-in witness via `tools/mix_witness.py` / `agy`. Only the selected
  master/stems are copied into a temporary sandbox directory and named in the prompt.
  Failures add a **warning** and continue DSP-only — never fabricate a witness.

**JSON `result` fields (analysis card):**

| Field | Meaning |
|-------|---------|
| `trial_id` | Id for this pass in the mix trial ledger |
| `master_path`, `groups` | Normalized input paths |
| `measurements` | DSP dict (RMS, crest, etc.) — evidence, not taste |
| `evidence_refs` | Stable strings moves may cite (e.g. `rms:bass:-22.0`) |
| `witness` | Present only when listener succeeded; section headings per contract |
| `warnings` | Listener skip, DSP caveats, etc. |
| `provenance` | Which modules ran (`dsp`, `listener`) |

### `propose`

```bash
.venv/bin/python tools/mix_check.py propose mix-trial-abc123 --max-moves 3
```

Returns a **trial card**: ≤3 automatic `gain_db` moves, each citing the exact
`evidence_refs` token from analysis. Stem balance compares group stems with one another;
the summed master is not a peer stem. **Main bus is never proposed.** May return **zero
moves** (abstention) when evidence does not support a bounded hypothesis. `pan` remains
contract-valid for future/manual cards but automatic pan proposals are parked until DSP
emits a defensible pan measurement.

Apply moves **manually** in Live (gain on the named track/group). v1 writes nothing to
the mixer.

### `prepare-ab`

After manual tweaks, re-export master:

```bash
.venv/bin/python tools/mix_check.py prepare-ab mix-trial-abc123 \
  --after-master /path/to/master_after.wav
```

Builds a **loudness-matched randomized-label A/B** manifest (`A`, `B`). Before judgment,
public cards omit the mapping, source paths, and render checksums to prevent accidental
deblinding. This is not cryptographic blindness against the operating agent: it already
handled the source files and could deliberately reconstruct the renders. For an honest
blind judgment, the owner listens in a separate player/presentation surface and the
operating agent must not inspect or recreate the mapping.

Rendered A/B audio is disposable. If it is later archived or deleted, trial history and
reports remain usable and the trial reports `artifacts_present: false`.

### `judge`

```bash
.venv/bin/python tools/mix_check.py judge mix-trial-abc123 \
  --choice B \
  --witness-rating not_shown \
  --kept-in-set unknown
```

| Flag | Values |
|------|--------|
| `--choice` | `A`, `B`, or `same` — randomized render labels only (no mapping returned before judgment) |
| `--witness-rating` | `useful`, `not_useful`, `misleading`, `not_shown` |
| `--kept-in-set` | `yes`, `no`, `unknown` |

Returns a **judgment card** (not a witness card). The card may reveal which render was
which export **after** your choice is recorded. Musical metric: would you keep this
change in the set? Technical: did you complete blind A/B honestly?

### `report`

```bash
.venv/bin/python tools/mix_check.py report
```

Summary of mix trials (counts, judgments). Does not touch composing session ledger.

---

## Optional listener witness

Implemented in `tools/mix_witness.py`. **Default: off.**

When `--listener` is set on `analyze`:

1. Discovers `agy` on `PATH` (same as audio refs).
2. Runs with **sanitized environment** (whitelist: `PATH`, `HOME`, `TMPDIR`, …).
3. Passes **`--sandbox`** unless `AUDIO_REF_AGY_SANDBOX=0`.
4. Copies only the selected inputs into one temporary directory, names every file in the
   prompt, and uses that same directory for `--add-dir` and witness output.
5. Requires strict markdown headings:
   - Audible observations
   - Masking hypotheses
   - Preserve
   - Candidate checks
   - Cannot determine
6. On timeout, missing `agy`, nonzero exit, or bad headings → **structured warning**,
   DSP-only continuation.

**Gemini/agy is a replaceable optional sensor**, not the planner. Witness numeric claims
are **not** trusted as `evidence_refs` unless DSP independently measured the same thing.
Prefer qualitative witness text.

Override the listener model without changing `mix_contracts` signatures:

```bash
MIX_WITNESS_MODEL=<agy-supported-model> .venv/bin/python tools/mix_check.py analyze \
  --master /path/to/master.wav \
  --group drums=/path/to/drums.wav \
  --group bass=/path/to/bass.wav \
  --listener
```

Precedence: explicit `model` argument (internal) → `MIX_WITNESS_MODEL` environment
variable → default (`gemini-3.8-flash-medium`). Blank values fall through.

The model ID must be **actually supported by your installed `agy`**. An unsupported
model adds a **warning** and the run continues **DSP-only** — never fabricates witness
text.

This repo does **not** implement a `gpt-audio` adapter and does not assume that the
installed `agy` supports one. It remains a future bake-off candidate only when an
actual, probed invocation path exists.

### Privacy

Audio stays local unless you opt into `--listener`. No upload path in the orchestrator
beyond what `agy` does under your account when invoked.

---

## Agent instructions (model-neutral)

When helping the owner mix via this repo:

1. **Do not claim you heard audio** unless you ran `--listener` and the result includes a
   validated `witness` card — and even then, treat it as hypothesis, not verdict.
2. **Measurements are evidence, not taste** — cite `evidence_refs`, not a scalar mix score.
3. **Maximum 3 automatic moves** — measured `gain_db` only in v1; never Main.
4. **Preserve** intentional abrasion — read `witness.sections.Preserve` and move
   `preserve` fields before suggesting fixes.
5. **Close the loop** — analyze → propose → human applies → re-export → prepare-ab →
   judge → report.
6. **Do not write to SIDEKICK JAM/BOOTH** or session ledger for mix work.

---

## How it was built

The Mix Adviser was delivered by three agents on parallel branches (orchestration,
DSP, trial persistence) and integrated afterwards; the runbooks are archived under
[`docs/history/agents/mix-advisor/`](history/agents/mix-advisor/README.md).
`mix_contracts.py` validates shapes and fails closed on bad witness headings.
Human blind A/B is the musical verdict.

---

## Failure modes

| Symptom | Likely cause | Action |
|---------|----------------|--------|
| `audio path must be local` | URL or wrong extension | Export to `.wav`/`.flac`/`.aiff` |
| `groups must contain 2–4 entries` | Too few/many `--group` | Re-export stem set |
| `listener skipped: …` | agy missing, timeout, bad headings | Continue DSP-only; fix agy or retry |
| `trial not found` | Wrong `trial_id` | Run `analyze` again or check ledger |
| Zero moves on `propose` | Abstention — weak evidence | Adjust export or target; don't force moves |
| `loudness matching attenuated …` | Before/after export levels differ by >12 dB | Check export settings before judging |
| CLI `ok: false` | Validation error | Read `error` on stdout, details on stderr |

---

## Rollback (v1)

The adviser **does not apply mixer changes**. Rollback = revert your manual gain
tweaks in Live and re-export. Trial ledger records judgments for learning, not automation.

---

## Verification (offline)

```bash
.venv/bin/python tools/mix_dsp.py --selftest
.venv/bin/python tools/mix_witness.py --selftest
.venv/bin/python tools/mix_check.py --selftest
.venv/bin/python mcp_server/mix_contracts.py --selftest
.venv/bin/python mcp_server/mix_trials.py --selftest
.venv/bin/python mcp_server/server.py --selftest
```

No network, Live, Producer Pal, or real `agy` in default selftests.
