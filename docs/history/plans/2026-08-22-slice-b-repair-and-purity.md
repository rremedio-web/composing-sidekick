# Slice B Repair + Purity Guard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **Owner's rule for this repo:** subagents must be `sonnet` or `haiku` (pass `model` explicitly). The orchestrating session reviews every diff and runs the gates. **Do not commit until Task 7 and only with the owner's go-ahead.**

**Goal:** Make the uncommitted slice-B extraction (`mcp_server/taste_bridge.py`) actually pure, fix the regression that turns the selftest red, and add a permanent guard so no future slice can ship a split-brain again.

**Architecture:** `core.py` is being decomposed into owner modules by "pure motion" (code moves, behavior does not): `core_shared.py` (slice 0), `export_bridge.py` (slice A), `taste_bridge.py` (slice B). `core.py` keeps a *facade* — a `from mcp_server.taste_bridge import (...)` block — so `server.py` and the selftest keep calling `_core.<name>`. A "split-brain" is when the same top-level `def` exists in two modules: Python binds whichever definition/import runs last, tests patch one copy while production runs the other, and edits to the dead copy do nothing. This plan removes every split-brain (30 functions across 4 pairs of files), fixes a test hook that stores a value nobody reads, and turns the purity check into a unit test that runs in one second *before* the 8-minute selftest.

**Tech Stack:** Python 3.12, pure stdlib (`ast`, `unittest`, `subprocess`), `.venv` only for music21 in `analyze/harmony.py`. No pytest, no ruff.

**Spec:** the "Verified findings" section below (there is no separate spec file — the findings were verified live on 2026-08-22 against this working tree; they are the spec).

**Repo:** `~/composing-sidekick` · branch `feat/audit-improvements` · HEAD `7921450` (green) · working tree = uncommitted slice B. Backups of every intermediate state: `/tmp/opencode/sliceB-backup/` (git HEAD is the real fallback: `git show HEAD:mcp_server/core.py`).

## Global Constraints

- Pure-stdlib Python; no numpy/sklearn; music21 confined to `analyze/harmony.py`.
- Deterministic by seed; MIDI/notes only; the system suggests, never replaces the owner's judgment.
- **Pure motion:** a slice moves code; it must not change any return shape, payload field, or default.
- **Hooks, not rebinds:** tests use `set_ratings_path` / `set_taste_model_path` / `set_phrase_pool_for_test`, never `_core._fn = lambda ...`.
- **Gates, in order:** `tests/test_purity.py` (1 s) → full unittest layer (~30 s) → `--selftest` (~8 min, 95 blocks, must print `=== all selftests passed ===` and the **python** process must exit 0 — a shell wrapper's exit code proves nothing).
- Line numbers below are from the 2026-08-22 21:30 working tree. **They shift after every task. Re-locate by the quoted pattern (`grep -n`), never by number.**
- Never `git add store/session_ears/` or `.scratch/`.

---

## Verified findings (the spec)

All confirmed by running code on 2026-08-22, not by reading the previous handoff.

| # | Finding | Where | Effect today |
|---|---------|-------|--------------|
| F1 | `set_taste_model_path()` stores `_TASTE_MODEL_PATH_OVERRIDE`, but `_taste_model_path()` never reads it (getter is byte-identical to HEAD). Four stray lines dangle off the setter (it "returns" a path). | `mcp_server/core_shared.py:179-198` | **Selftest RED** at block 87 (`server.py:8438`: `status == "fully active"` fails with `cold-start`) because the test writes a 35-pair model to file A and the code reads file B. |
| F2 | 26 top-level defs exist in both `core.py` and `taste_bridge.py` (476 dead lines, `core.py:126-643` + `_taste_model_summary` at `:5521`). The HEAD-based AST rebuild re-imported everything slice B had already moved. | `core.py` vs `taste_bridge.py` | Harmless *by accident*: the facade import sits at the **bottom** (`core.py:5743`) so 22 get overwritten at load; 4 (`_candidate_source`, `_normalise_contrast_axis`, `_rating_scope_for_contrast`, `_trainable_model_for_scope`) stay core-bound (byte-identical today). |
| F3 | `taste_bridge.py:75-76` copies core's `sys.modules.setdefault(alias, self)` loop. If the bridge is ever imported before `core`, `mcp_server.core` resolves to the *bridge* and `core.py` never loads. | `taste_bridge.py:75-76` | Latent; any test that imports the bridge first will hit it. |
| F4 | `_RATINGS_PATH_OVERRIDE` imported **by value** in `core.py:55` and `taste_bridge.py:46` (a module global copied as `None` forever). `set_ratings_path` imported twice in `core.py:44,56`. | import blocks | Unused today; trap for the next reader. |
| F5 | `_clip_digest` defined in `core.py:4398` **and** `core_shared.py:88`; core uses its own. | | split-brain |
| F6 | `_phrases` / `_project_keys` added to `core_shared.py:~270-289` but the live copies (with the `_PHRASE_POOL_OVERRIDE` test hook) are in `taste_bridge.py:934-953`. Nobody imports the `core_shared` copies. | | dead copies that *lack* the test hook |
| F7 | `send_to_ableton` defined in `core.py:5622-5740` **and** `export_bridge.py:102-219` (committed in slice A). Core's copy wins (`core.py:5615` binds the bridge's, then `:5622` redefines). The bridge copy **dropped the `"clip_length"` key** from the returned handoff; `server.py:1538` asserts on `handoff["clip_length"]`. Also `_resolve_export_path` is re-exported twice (`core.py:5616,5619`). | | Slice A was not pure motion; deleting core's copy naively would change the Producer Pal handoff and break the selftest. |
| F8 | `mcp_server/server.py.bak` (untracked; HEAD's server.py + 8 lines). | | clutter |

Not a finding, but context: `tests/` is stdlib `unittest` (51 tests, `.venv/bin/python -m unittest discover -s tests -t .`), pattern in `tests/test_core_shared.py`. The selftest's last line is `=== all selftests passed ===` (`server.py:8502`).

---

## File map

| File | Responsibility after this plan |
|------|-------------------------------|
| `mcp_server/core.py` | Generation/arrangement/scoring. Facade import block near the top (after the alias loop). Zero defs shared with any bridge. |
| `mcp_server/core_shared.py` | State, paths, atomic IO, caches, `_clip_digest`. Path hooks that are actually honored. No phrase-pool code. |
| `mcp_server/taste_bridge.py` | Sole owner of taste/ratings/gold/ears-retrieval code. No `sys.modules` aliasing. |
| `mcp_server/export_bridge.py` | Sole owner of `send_to_ableton` / `export_midi`, with `clip_length` restored. |
| `tests/test_core_shared.py` | + `TestPathOverrides` (Task 1). |
| `tests/test_purity.py` | **New.** AST pairwise zero-overlap + facade sanity + no by-value `_OVERRIDE` imports (Task 2). |
| `tests/test_module_identity.py` | **New.** Bridge-first import must not hijack `mcp_server.core` (Task 3). |
| `tests/test_export_bridge.py` | **New.** `clip_length` in handoff; core facade is the bridge function (Task 4). |
| `.scratch/excise_defs.py` | Throwaway AST tool used by Tasks 2 and 4 (not committed). |

---

### Task 0: Preflight (2 min)

**Files:** none modified.

- [ ] **Step 1: Confirm you are on the right tree**

Run:
```bash
cd ~/composing-sidekick
git branch --show-current && git log -1 --format='%h %s' && git status --short
```
Expected: `feat/audit-improvements`, `7921450 refactor(core): extract export/Producer-Pal bridge ...`, and modified `core.py core_shared.py server.py`, untracked `taste_bridge.py`, `server.py.bak`, `.scratch/`, `store/session_ears/`. If `taste_bridge.py` is missing, stop — the slice-B work is gone; restore from `/tmp/opencode/sliceB-backup/` and ask the owner.

- [ ] **Step 2: Snapshot the tree (in case a tool gutting a file happens again)**

```bash
mkdir -p .scratch/pre-plan-2026-08-22 && cp mcp_server/core.py mcp_server/core_shared.py mcp_server/taste_bridge.py mcp_server/export_bridge.py mcp_server/server.py .scratch/pre-plan-2026-08-22/
```

- [ ] **Step 3: Baseline the fast gate**

```bash
.venv/bin/python -m unittest discover -s tests -t . 2>&1 | tail -3
```
Expected: `Ran 51 tests ... OK`. (The selftest is known RED — do not run it yet; it costs 8 minutes and Task 1 is the fix.)

---

### Task 1: Make `set_taste_model_path` real (F1, F4)

**Files:**
- Modify: `mcp_server/core_shared.py` (pattern `def _taste_model_path` and `def set_taste_model_path`)
- Modify: `mcp_server/core.py` import block (pattern `^from mcp_server.core_shared import`)
- Modify: `mcp_server/taste_bridge.py` import block (pattern `_RATINGS_PATH_OVERRIDE,`)
- Test: `tests/test_core_shared.py`

**Interfaces:**
- Produces: `core_shared._taste_model_path() -> str` honoring `set_taste_model_path(path | None)`; precedence = explicit taste override → sibling of an overridden ratings file → `<store>/taste_model.json`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_core_shared.py`:

```python
class TestPathOverrides(unittest.TestCase):
    """set_taste_model_path must be honored by _taste_model_path (it was a no-op hook)."""

    def setUp(self):
        core_shared.set_ratings_path(None)
        core_shared.set_taste_model_path(None)

    def tearDown(self):
        core_shared.set_ratings_path(None)
        core_shared.set_taste_model_path(None)

    def test_taste_override_wins(self):
        with tempfile.TemporaryDirectory() as d:
            ratings = os.path.join(d, "ratings.json")
            taste = os.path.join(d, "custom_taste.json")
            core_shared.set_ratings_path(ratings)
            core_shared.set_taste_model_path(taste)
            self.assertEqual(core_shared._taste_model_path(), taste)

    def test_ratings_override_alone_derives_sibling(self):
        with tempfile.TemporaryDirectory() as d:
            ratings = os.path.join(d, "ratings.json")
            core_shared.set_ratings_path(ratings)
            self.assertEqual(
                core_shared._taste_model_path(),
                os.path.join(d, "ratings.taste_model.json"),
            )

    def test_reset_restores_store_default(self):
        with tempfile.TemporaryDirectory() as d:
            core_shared.set_ratings_path(os.path.join(d, "r.json"))
            core_shared.set_taste_model_path(os.path.join(d, "t.json"))
        core_shared.set_ratings_path(None)
        core_shared.set_taste_model_path(None)
        store_dir = os.path.dirname(os.path.abspath(core_shared.RATINGS_JSON)) or "."
        self.assertEqual(
            core_shared._taste_model_path(),
            os.path.join(store_dir, "taste_model.json"),
        )

    def test_setter_returns_none(self):
        # The setter used to fall through into a pasted copy of the getter.
        self.assertIsNone(core_shared.set_taste_model_path(None))
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m unittest tests.test_core_shared.TestPathOverrides -v 2>&1 | tail -8
```
Expected: `test_taste_override_wins` FAILS (`.../ratings.taste_model.json != .../custom_taste.json`), `test_setter_returns_none` FAILS (returns a path string). The other two pass.

- [ ] **Step 3: Fix the getter and setter** — in `mcp_server/core_shared.py`, replace the whole `_taste_model_path` + `set_taste_model_path` pair (from `def _taste_model_path` through the line before `def _load_ratings_file`) with:

```python
def _taste_model_path() -> str:
    """Selftest override first; else sibling of an overridden ratings file (so
    set_ratings_path isolates it in tests too); else the store default."""
    if _TASTE_MODEL_PATH_OVERRIDE:
        return _TASTE_MODEL_PATH_OVERRIDE
    ratings_path = os.path.abspath(_ratings_path())
    if _RATINGS_PATH_OVERRIDE:
        return os.path.splitext(ratings_path)[0] + ".taste_model.json"
    return os.path.join(os.path.dirname(ratings_path) or ".", "taste_model.json")


def set_taste_model_path(path: str | None) -> None:
    """Override taste model JSON path (selftest only). Pass None to reset."""
    global _TASTE_MODEL_PATH_OVERRIDE
    _TASTE_MODEL_PATH_OVERRIDE = path
```

(`_TASTE_MODEL_PATH_OVERRIDE: str | None = None` already exists just above the getter — keep it.)

- [ ] **Step 4: Remove the by-value `_OVERRIDE` imports and the duplicate `set_ratings_path` (F4)**

In `mcp_server/core.py`'s `from mcp_server.core_shared import (` block: delete the line `    _RATINGS_PATH_OVERRIDE,` and the **second** `    set_ratings_path,` (keep one). In `mcp_server/taste_bridge.py`'s `from mcp_server.core_shared import (` block: delete the line `    _RATINGS_PATH_OVERRIDE,`. Verify nothing used them:
```bash
grep -n "_RATINGS_PATH_OVERRIDE" mcp_server/core.py mcp_server/taste_bridge.py
```
Expected: no output.

- [ ] **Step 5: Run to verify it passes**

```bash
.venv/bin/python -m unittest tests.test_core_shared -v 2>&1 | tail -4
.venv/bin/python -c "import sys; sys.path.insert(0,'.'); from mcp_server import core; print('import ok', callable(core.score_phrase))"
```
Expected: all `test_core_shared` tests OK (4 new); `import ok True`.

- [ ] **Step 6: Prove block 87 is fixed without the 8-minute run** — reproduce its setup:

```bash
.venv/bin/python - <<'EOF'
import sys, os, tempfile; sys.path.insert(0, '.')
from mcp_server import core as _core
d = tempfile.mkdtemp()
rf, tf = os.path.join(d, 'ratings.json'), os.path.join(d, 'taste_model.json')
_core._save_json_atomic(rf, {"ratings": [{"id": 1, "phrase_id": "a", "rating": 1, "label": "gold", "comment": ""}]})
h = _core._ratings_file_hash(rf)
_core._save_json_atomic(tf, {"schema_version": 1, "n_pairs": 35, "n_gold_pairs": 5, "n_rounds": 0,
    "ratings_sha256": h, "train_accuracy": 0.85,
    "weights": [0.1] * len(_core._TASTE_FEATURE_NAMES), "feature_names": list(_core._TASTE_FEATURE_NAMES)})
_core.set_ratings_path(rf); _core.set_taste_model_path(tf); _core._CACHE.clear()
s = _core._taste_model_summary(); print(s["status"], s["n_pairs"])
_core.set_ratings_path(None); _core.set_taste_model_path(None); _core._CACHE.clear()
EOF
```
Expected: `fully active 35`.

---

### Task 2: Excise the 26 dead duplicates and move the facade up (F2)

**Files:**
- Create: `.scratch/excise_defs.py` (throwaway, not committed)
- Create: `tests/test_purity.py`
- Modify: `mcp_server/core.py` (dup region; facade block at pattern `# Taste bridge facade`)

**Interfaces:**
- Produces: `core.py` with zero top-level defs shared with `taste_bridge.py`; facade block located after the alias loop (pattern `sys.modules.setdefault(_alias, sys.modules[__name__])`) and extended with the 4 names that were core-bound: `_candidate_source`, `_normalise_contrast_axis`, `_rating_scope_for_contrast`, `_trainable_model_for_scope`.

- [ ] **Step 1: Write the failing purity test** — create `tests/test_purity.py`:

```python
"""Purity guard: the core.py decomposition must be pure motion.

A top-level def that exists in two mcp_server modules is a split-brain: Python
binds whichever definition/import runs last, tests patch one copy while
production calls the other, and edits to the dead copy silently do nothing.
This guard caught slice 0 and slice B after the fact; now it runs in one second,
before the eight-minute selftest.
"""
from __future__ import annotations

import ast
import itertools
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = ["core.py", "core_shared.py", "taste_bridge.py", "export_bridge.py"]


def _src(name: str) -> str:
    with open(os.path.join(ROOT, "mcp_server", name), encoding="utf-8") as fh:
        return fh.read()


def _top_level_defs(name: str) -> set[str]:
    tree = ast.parse(_src(name))
    return {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _top_level_assigned(name: str) -> set[str]:
    out: set[str] = set()
    for n in ast.parse(_src(name)).body:
        targets = n.targets if isinstance(n, ast.Assign) else (
            [n.target] if isinstance(n, ast.AnnAssign) else []
        )
        out.update(t.id for t in targets if isinstance(t, ast.Name))
    return out


def _facade_names() -> set[str]:
    out: set[str] = set()
    for n in ast.parse(_src("core.py")).body:
        if isinstance(n, ast.ImportFrom) and n.module and n.module.endswith("taste_bridge"):
            out.update(a.name for a in n.names)
    return out


class TestNoDuplicateDefs(unittest.TestCase):
    def test_pairwise_zero_overlap(self):
        defs = {m: _top_level_defs(m) for m in MODULES}
        for a, b in itertools.combinations(MODULES, 2):
            with self.subTest(pair=f"{a} x {b}"):
                self.assertEqual(sorted(defs[a] & defs[b]), [], f"{a} and {b} both define these")

    def test_facade_names_exist_in_bridge(self):
        bridge = _top_level_defs("taste_bridge.py") | _top_level_assigned("taste_bridge.py")
        missing = sorted(_facade_names() - bridge)
        self.assertEqual(missing, [], "core.py facade imports names taste_bridge.py does not define")

    def test_no_by_value_override_imports(self):
        # `from x import _FOO_OVERRIDE` copies None forever; read overrides via a getter.
        pat = re.compile(r"^\s+_[A-Z_]+_OVERRIDE,\s*$", re.M)
        for m in MODULES:
            with self.subTest(module=m):
                self.assertEqual(pat.findall(_src(m)), [], f"{m} imports an _OVERRIDE global by value")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m unittest tests.test_purity -v 2>&1 | tail -12
```
Expected: `test_pairwise_zero_overlap` FAILS for `core.py x taste_bridge.py` (26 names), `core.py x core_shared.py` (`_clip_digest`), `core.py x export_bridge.py` (`send_to_ableton`), `core_shared.py x taste_bridge.py` (`_phrases`, `_project_keys`). The other two tests pass (Task 1 already removed the by-value imports).

- [ ] **Step 3: Create the throwaway excision tool** — `.scratch/excise_defs.py`:

```python
"""Delete top-level function defs by name from a Python file, AST-accurately.

Usage: .venv/bin/python .scratch/excise_defs.py FILE NAME [NAME ...]
       .venv/bin/python .scratch/excise_defs.py FILE --shared-with OTHER_FILE
Deletes the def (from its first decorator through its last line) plus the blank
lines that follow it, so surrounding spacing stays PEP8-clean. Refuses to run if
a requested name is not a top-level def. Prints what it removed.
"""
import ast
import sys


def top_defs(path):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def main(argv):
    path = argv[1]
    if argv[2] == "--shared-with":
        names = sorted(set(top_defs(path)) & set(top_defs(argv[3])))
    else:
        names = argv[2:]
    defs = top_defs(path)
    missing = [n for n in names if n not in defs]
    if missing:
        sys.exit(f"not top-level defs in {path}: {missing}")
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines(keepends=True)
    ranges = []
    for n in names:
        node = defs[n]
        start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
        end = node.end_lineno
        while end < len(lines) and lines[end].strip() == "":
            end += 1
        ranges.append((start, end, n))
    for start, end, n in sorted(ranges, reverse=True):
        print(f"removed {n}: lines {start + 1}-{end}")
        del lines[start:end]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(lines))
    ast.parse("".join(lines))  # still valid Python
    left = set(names) & set(top_defs(path))
    assert not left, f"still present: {left}"
    print(f"OK: {len(names)} defs removed from {path}")


if __name__ == "__main__":
    main(sys.argv)
```

- [ ] **Step 4: Excise the 26**

```bash
.venv/bin/python .scratch/excise_defs.py mcp_server/core.py --shared-with mcp_server/taste_bridge.py
```
Expected: 26 `removed ...` lines printed bottom-up (first `_taste_model_summary`, last `_idempotency_fingerprint`), then `OK: 26 defs removed from mcp_server/core.py`. If the count is not 26, stop and diff against `.scratch/pre-plan-2026-08-22/core.py`.

- [ ] **Step 5: Move the facade block up and add the 4 missing names**

In `mcp_server/core.py`:
1. Cut the block starting at the line `# Taste bridge facade: all moved taste-related names remain accessible as core._name` through the closing `)` of `from mcp_server.taste_bridge import (` (it is at the very end of the file).
2. Paste it immediately after the alias loop, i.e. after the line `    sys.modules.setdefault(_alias, sys.modules[__name__])` and its following blank line. The alias loop must run **before** the bridge is imported (Task 3 explains why).
3. Inside the pasted block add four lines (anywhere in the list):
```python
    _candidate_source,
    _normalise_contrast_axis,
    _rating_scope_for_contrast,
    _trainable_model_for_scope,
```
4. Replace the comment line with:
```python
# Taste bridge facade (slice B): taste/ratings/gold/ears-retrieval code lives in
# mcp_server/taste_bridge.py — sole owner. These names stay reachable as core._name
# for server.py and the selftest. The bridge imports nothing from core at module
# load (only a lazy `score_phrase` inside _taste_features), so this is cycle-free.
```

- [ ] **Step 6: Verify import health, purity for this pair, and the fast gate**

```bash
.venv/bin/python - <<'EOF'
import sys; sys.path.insert(0, '.')
from mcp_server import core, taste_bridge as tb
for n in ["find_phrases_with_ears", "get_ratings", "rate_candidates", "score_phrase",
          "set_phrase_pool_for_test", "_select_best_lead", "_normalise_contrast_axis",
          "_trainable_model_for_scope", "_reharmonize_notes", "_harmony_data"]:
    assert hasattr(core, n), n
assert core.rate_candidates is tb.rate_candidates
assert core._taste_weight is tb._taste_weight
assert core._trainable_model_for_scope is tb._trainable_model_for_scope
print("attribute probes OK; core.py lines:", sum(1 for _ in open("mcp_server/core.py")))
EOF
.venv/bin/python -m unittest tests.test_purity -v 2>&1 | grep -E "core.py x taste_bridge|^OK|^FAILED"
.venv/bin/python -m unittest discover -s tests -t . 2>&1 | tail -2
```
Expected: `attribute probes OK; core.py lines: ~5310`; the `core.py x taste_bridge.py` subtest no longer appears as a failure (the other three pairs still fail until Task 4); the full unittest run reports only the `test_purity` failures, nothing else.

---

### Task 3: Remove the bridge's `sys.modules` alias loop (F3)

**Files:**
- Modify: `mcp_server/taste_bridge.py` (pattern `for _alias in ("core", "mcp_server.core"):`)
- Create: `tests/test_module_identity.py`

**Interfaces:**
- Produces: importing `mcp_server.taste_bridge` before `mcp_server.core` yields two distinct modules, with `core` fully loaded.

- [ ] **Step 1: Write the failing test** — create `tests/test_module_identity.py`:

```python
"""Module identity: importing taste_bridge first must not hijack mcp_server.core.

taste_bridge.py once carried a copy of core.py's `sys.modules.setdefault(alias, self)`
loop. Imported first, it registered *itself* as `mcp_server.core`, so core.py never
loaded. Only core.py may claim those aliases. Runs in a fresh interpreter so this
process's import order cannot mask it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SNIPPET = """
import sys; sys.path.insert(0, %r)
from mcp_server import taste_bridge
from mcp_server import core
assert core is not taste_bridge, "taste_bridge hijacked mcp_server.core"
assert sys.modules["mcp_server.core"] is core
assert sys.modules.get("core") is core, "core.py's own alias loop must still run"
assert callable(core.score_phrase), "core.py did not actually load"
assert core.rate_candidates is taste_bridge.rate_candidates, "facade must bind the bridge's function"
print("MODULE_IDENTITY_OK")
""" % ROOT


class TestBridgeFirstImport(unittest.TestCase):
    def test_bridge_then_core(self):
        r = subprocess.run(
            [sys.executable, "-c", SNIPPET],
            capture_output=True, text=True, cwd=ROOT, timeout=180,
        )
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn("MODULE_IDENTITY_OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m unittest tests.test_module_identity -v 2>&1 | tail -6
```
Expected: FAIL with `AssertionError: taste_bridge hijacked mcp_server.core`.

- [ ] **Step 3: Delete the loop from the bridge** — in `mcp_server/taste_bridge.py` remove these two lines (and any comment line directly above them that mentions "module universe" / aliases):

```python
for _alias in ("core", "mcp_server.core"):
    sys.modules.setdefault(_alias, sys.modules[__name__])
```
Confirm `mcp_server/core.py` still has its own copy (pattern `# ONE module universe`) — it must.

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m unittest tests.test_module_identity -v 2>&1 | tail -3
```
Expected: `OK`.

---

### Task 4: Close the other three split-brains (F5, F6, F7)

**Files:**
- Modify: `mcp_server/core.py` (`_clip_digest` def; `send_to_ableton` def; duplicate `_resolve_export_path` re-export; `core_shared` import block)
- Modify: `mcp_server/core_shared.py` (`_phrases`, `_project_keys` defs)
- Modify: `mcp_server/export_bridge.py` (return dict of `send_to_ableton`)
- Create: `tests/test_export_bridge.py`

**Interfaces:**
- Produces: `export_bridge.send_to_ableton(...)` returns the handoff dict **with** `"clip_length": "<N>bar"`; `core.send_to_ableton is export_bridge.send_to_ableton`; `core._clip_digest is core_shared._clip_digest`.

- [ ] **Step 1: Write the failing export test** — create `tests/test_export_bridge.py`:

```python
"""Unit tests for mcp_server/export_bridge.py (Producer Pal payload builder, no socket)."""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mcp_server import core, core_shared, export_bridge  # noqa: E402

NOTES = [
    {"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 96},
    {"pitch": 63, "start": 1.0, "duration": 1.0, "velocity": 90},
    {"pitch": 67, "start": 2.0, "duration": 2.0, "velocity": 84},
]


class TestSendToAbleton(unittest.TestCase):
    def test_handoff_reports_clip_length_token(self):
        # server.py's selftest asserts handoff["clip_length"] matches ^\d+bar$ —
        # slice A's bridge copy had silently dropped the key.
        handoff = export_bridge.send_to_ableton(
            NOTES, track=0, clip_name="unit", tempo=120, bar_length=2,
        )
        self.assertIn("clip_length", handoff)
        self.assertRegex(handoff["clip_length"], r"^\d+bar$")
        self.assertEqual(handoff["clip_length"], "2bar")
        # the same token is what ppal-create-clip receives as `length`
        create_clip = handoff["tool_calls"][-1]
        self.assertEqual(create_clip["tool"], "ppal-create-clip")
        self.assertEqual(create_clip["arguments"]["length"], handoff["clip_length"])

    def test_core_facade_is_the_bridge_function(self):
        self.assertIs(core.send_to_ableton, export_bridge.send_to_ableton)


class TestSharedHelpers(unittest.TestCase):
    def test_clip_digest_has_one_owner(self):
        self.assertIs(core._clip_digest, core_shared._clip_digest)
        self.assertEqual(len(core_shared._clip_digest(NOTES)), 10)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m unittest tests.test_export_bridge -v 2>&1 | tail -10
```
Expected: `test_handoff_reports_clip_length_token` FAILS (`'clip_length' not found`), `test_core_facade_is_the_bridge_function` FAILS, `test_clip_digest_has_one_owner` FAILS. (Verified on 2026-08-22: the bridge's handoff has no `clip_length` key, while its `tool_calls[-1]` is `ppal-create-clip` with `arguments.length == "2bar"` and core's shadow copy returns `clip_length == "2bar"`.)

- [ ] **Step 3: Port `clip_length` into the bridge (F7)** — in `mcp_server/export_bridge.py`, in the final `return {` of `send_to_ableton`, add one line after `"tempo": tempo,`:

```python
        "clip_length": clip_length,
```
Then confirm the two copies now differ only by the `core_shared._note_dicts` qualifier:
```bash
.venv/bin/python - <<'EOF'
import ast, difflib
def src(p, name):
    t = ast.parse(open(p).read()); L = open(p).read().splitlines()
    n = next(n for n in t.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return L[n.lineno-1:n.end_lineno]
d = [l for l in difflib.unified_diff(src("mcp_server/core.py","send_to_ableton"), src("mcp_server/export_bridge.py","send_to_ableton"), lineterm="", n=0) if l.startswith(("+","-")) and not l.startswith(("+++","---"))]
print("\n".join(d))
EOF
```
Expected: exactly two lines, `-    nd = _normalize_notes_for_pp(_note_dicts(notes))` and `+    nd = _normalize_notes_for_pp(core_shared._note_dicts(notes))`. Anything else → stop; the bridge copy has drifted further and must be reconciled line by line before deleting core's.

- [ ] **Step 4: Delete core's `send_to_ableton` and `_clip_digest`; delete core_shared's `_phrases`/`_project_keys`**

```bash
.venv/bin/python .scratch/excise_defs.py mcp_server/core.py send_to_ableton _clip_digest
.venv/bin/python .scratch/excise_defs.py mcp_server/core_shared.py _phrases _project_keys
```
Expected: `OK: 2 defs removed` twice. Then in `mcp_server/core.py`:
- add `    _clip_digest,` to the `from mcp_server.core_shared import (` block;
- delete the duplicate re-export — the comment `# Also re-export _resolve_export_path for tests that call it directly` and the second `_resolve_export_path = __import__(...)` line right under it (keep the first one, next to `send_to_ableton = __import__(...)`).

Confirm nothing imported the removed `core_shared` copies:
```bash
grep -rn "core_shared\._phrases\|core_shared\._project_keys" mcp_server tests; echo "(no output = good)"
```

- [ ] **Step 5: Run to verify it passes — purity is now fully green**

```bash
.venv/bin/python -m unittest tests.test_export_bridge tests.test_purity -v 2>&1 | tail -4
.venv/bin/python -m unittest discover -s tests -t . 2>&1 | tail -2
```
Expected: `OK` for both; full run `Ran 62 tests ... OK` (51 baseline + 4 `TestPathOverrides` + 3 purity + 1 identity + 3 export).

---

### Task 5: Selftest gate (the 8-minute run)

**Files:** none modified.

- [ ] **Step 1: Run it with a ≥10-minute allowance, log to disk, capture the python exit code**

```bash
cd ~/composing-sidekick
.venv/bin/python mcp_server/server.py --selftest > .scratch/selftest-sliceB.log 2>&1; echo "PYTHON_EXIT=$?" | tee -a .scratch/selftest-sliceB.log
```
(Run it in the background if your harness has a shorter foreground timeout; poll the log. CPU near 100% for minutes on the real-index blocks is normal — that is numeric crunching, not a hang.)

- [ ] **Step 2: Judge it by the log, not by any wrapper's exit code**

```bash
tail -3 .scratch/selftest-sliceB.log
grep -cE "^--- |^[0-9]+\. " .scratch/selftest-sliceB.log
grep -nE "Traceback|AssertionError|^FAILED" .scratch/selftest-sliceB.log | head
```
Expected: last lines `=== all selftests passed ===` then `PYTHON_EXIT=0`; ≥ 95 block headers; no Traceback lines. Block 87 (`Data integrity — atomic writes, schema validation, stale taste model`) prints `atomic write rollback, schema validation, and stale taste model detection verified`.

- [ ] **Step 3: If it is red** — do not patch around it. Find the block name above the traceback, reproduce that block's setup in a 10-line snippet (the way Task 1 Step 6 does), fix the root cause, re-run the fast gates, then re-run the selftest. If it stalls >10 min on one block: `.venv/bin/python -m cProfile -o .scratch/selftest.prof mcp_server/server.py --selftest` and name the hot function before deciding.

---

### Task 6: Review checklist (orchestrator, before asking the owner)

- [ ] `git diff --stat` shows only: `core.py` (large deletion), `core_shared.py`, `taste_bridge.py` (new file), `export_bridge.py` (+1 line), `server.py` (hook conversions from the previous session), four test files, this plan.
- [ ] `git diff mcp_server/export_bridge.py` is exactly the `clip_length` line.
- [ ] Top-level defs in `core.py` ≈ 145 (173 today − 28) and `wc -l` ≈ 5,190 (was 5,789; HEAD was 6,005).
- [ ] `.venv/bin/python -m unittest tests.test_purity` passes — run it **last**, after every other edit.
- [ ] No `print(` debugging left in the four modules: `git diff | grep "^+.*print(" | grep -v tests/`.
- [ ] Report to the owner in producer terms: "taste rack consolidated, test hook now real, handoff keeps its clip length, purity guard in place; selftest green N blocks" and **ask for the go-ahead to commit**.

---

### Task 7: Cleanup and commit (only after the owner's go-ahead)

**Files:**
- Delete: `mcp_server/server.py.bak`

- [ ] **Step 1: Remove clutter**

```bash
rm mcp_server/server.py.bak
grep -qxF ".scratch/" .gitignore 2>/dev/null || echo ".scratch/" >> .gitignore
git status --short
```
Expected: modified `core.py core_shared.py export_bridge.py server.py .gitignore`, untracked `taste_bridge.py`, `tests/test_purity.py`, `tests/test_module_identity.py`, `tests/test_export_bridge.py`, `docs/superpowers/plans/2026-08-22-slice-b-repair-and-purity.md`, and still-untracked `store/session_ears/` (leave it).

- [ ] **Step 2: Stage exactly the slice**

```bash
git add mcp_server/core.py mcp_server/core_shared.py mcp_server/taste_bridge.py mcp_server/export_bridge.py mcp_server/server.py \
        tests/test_core_shared.py tests/test_purity.py tests/test_module_identity.py tests/test_export_bridge.py \
        docs/superpowers/plans/2026-08-22-slice-b-repair-and-purity.md .gitignore
git status --short | grep -v "^??"
```

- [ ] **Step 3: Commit**

```bash
git commit -m "refactor(core): slice B — taste_bridge is sole owner of the taste layer; purity guard

- taste_bridge.py: all taste/ratings/gold/ears-retrieval code; lazy score_phrase
  import (core imports the bridge at load, so the reverse must be deferred)
- core.py: facade import moved to the top; 28 duplicate defs removed
  (26 taste, _clip_digest, send_to_ableton); -600 lines
- core_shared: dead _phrases/_project_keys copies removed (the live ones, with
  the phrase-pool test hook, are the bridge's)
- core_shared.set_taste_model_path() was a no-op hook — _taste_model_path()
  now honors it (selftest block 87 was red); stray setter tail removed
- export_bridge.send_to_ableton: restore 'clip_length' in the handoff
  (slice A's copy had dropped it; core's shadow copy was masking that)
- taste_bridge: drop the copied sys.modules alias loop (bridge-first import
  hijacked mcp_server.core)
- tests: test_purity (AST pairwise zero-overlap, facade sanity, no by-value
  _OVERRIDE imports), test_module_identity, test_export_bridge, TestPathOverrides
- selftest: hooks instead of rebinds for pool/paths (from previous session)

Selftest: 95 blocks green. unittest: all green."
```
(Add your harness's co-author trailer if it requires one.)

- [ ] **Step 4: Post-commit sanity**

```bash
git log -1 --stat | tail -15 && git status --short
```
Expected: only `store/session_ears/` remains untracked.

---

### Task 8 (optional, 5 min, independent): `motifs.py` ResourceWarnings

**Files:**
- Modify: `analyze/motifs.py` (pattern `json.load(open(`)

- [ ] **Step 1: Show the leak**

```bash
grep -n "json.load(open(" analyze/motifs.py
```
Expected: two lines (~137 and ~139) loading `phrase_index.json` and `style_profile.json` via bare `open(...)` — file handles are never closed → `ResourceWarning` noise in tests.

- [ ] **Step 2: Fix with context managers** — replace each `x = json.load(open(PATH))` with:

```python
    with open(os.path.join(STORE, "phrase_index.json"), encoding="utf-8") as fh:
        idx = json.load(fh)
    with open(os.path.join(STORE, "style_profile.json"), encoding="utf-8") as fh:
        sp = json.load(fh)
```
(keep the variable names the file already uses — `idx` and `sp` as of today).

- [ ] **Step 3: Verify**

```bash
grep -c "json.load(open(" analyze/motifs.py
.venv/bin/python -W error::ResourceWarning -m unittest tests.test_motifs 2>&1 | tail -2
```
Expected: `0`; `OK`. Commit separately: `fix(motifs): close store JSON handles (ResourceWarning)`.

---

## Other improvements (not tasks in this plan — decisions or follow-up plans)

**Needs the owner's decision**

1. **Off-machine backup / remote — the biggest risk in the project, open since 2026-07-09.** There is no `git remote`. Two months of work exist on one disk. Either:
   - private GitHub: `gh repo create composing-sidekick --private --source=. --remote=origin --push`, or
   - a bare repo on an external drive / iCloud folder: `git init --bare "<drive>/composing-sidekick.git" && git remote add origin "<drive>/composing-sidekick.git" && git push -u origin --all`.
   Either is a 2-minute job once he picks.
2. **Old stash** `stash@{0}: On feat/ear-semantics: T8-partial-composer-killed-mid-edit` (318 lines across `core.py`/`server.py`, from July). Archive, then drop:
   `git stash show -p stash@{0} > .scratch/stash0-T8-partial-2026-07.patch && git stash drop stash@{0}`.
3. `store/session_ears/` (untracked runtime data) — ignore it or keep it; it must not be committed by accident. Suggest adding `store/session_ears/` to `.gitignore`.

**Next plans (write a separate plan for each; do not fold into this one)**

4. **Slice C — `scoring.py`.** Move `score_phrase`, `_phrase_style_analysis`, `_aggregate`, `_phrase` (and whatever `_phrase_style_analysis` pulls in) out of `core.py` into `mcp_server/scoring.py`, then replace the lazy import in `taste_bridge._taste_features` with a normal top-level import. Run `tests/test_purity.py` (add `scoring.py` to `MODULES`) **before** the selftest. Same discipline: AST excise, facade, purity, gate, commit.
5. **Role-scoped ratings** — the agreed next move that actually changes what the sidekick suggests. The bridge already carries the scaffolding (`_rating_scope_for_contrast`, `_trainable_model_for_scope`, `_TRAINABLE_MODELS_BY_SCOPE`); the missing piece is stamping `role` on each rating round and training/serving per-role models so riff-vs-lead judgments stop polluting each other. Taste-model numbers as of today: 41 pairs (9 gold), 13 rounds, train acc 0.902 — still thin for 9 features; per-role splits will be thinner, so plan the cold-start ramp per role.
6. **Observability for fixed thresholds** (audit issue #5): log classification-margin outliers in `ingest/materialize.py` so role/section drift is visible as the catalogue grows.

**Standing rules for every future slice (add to `CLAUDE.md` in the live repo)**

- Purity check runs **before** the selftest, every move: `.venv/bin/python -m unittest tests.test_purity`.
- "Import succeeded" is not a health check. Probe attributes (`hasattr(core, name)` for a fixed list) and identities (`core.fn is bridge.fn`).
- A shell wrapper's exit code is not the program's exit code. Read the log's last line.
- Hooks, not rebinds, in selftests — and a hook is only real if its *reader* honors it (grep for the variable: if only the setter mentions it, it is a no-op).
- Never import a module-level `_*_OVERRIDE` global by value.
- Only `core.py` may register `sys.modules` aliases.
- Every edit task tees its log to `.scratch/` (or `/tmp/opencode/`) so a wedged executor leaves evidence.
- Backups before AST surgery; git HEAD is the fallback of record.

## Resume commands

```bash
cd ~/composing-sidekick
.venv/bin/python -m unittest tests.test_purity               # 1 s — run first and last
.venv/bin/python -m unittest discover -s tests -t .          # ~30 s
.venv/bin/python mcp_server/server.py --selftest             # ~8 min, 95 blocks
```
