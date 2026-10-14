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

# taste_bridge sole owners — core.py may facade-import these names but must never
# re-bind them with a top-level assignment (split-brain vs the live bridge state).
BRIDGE_OWNED_STATE = frozenset({
    "_PHRASE_POOL_OVERRIDE",
    "_GOLD_UNMARK_LABELS",
    "_TASTE_FEATURE_NAMES",
    "_TASTE_MIN_PAIRS",
    "_TASTE_FULL_PAIRS",
    "_CANDIDATE_SCHEMA_VERSION",
    "_CONTRAST_AXES",
    "_TRAINABLE_MODELS_BY_SCOPE",
})


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


def _bridge_owned_reassignments_in_core() -> list[str]:
    """Top-level Assign/AnnAssign in core.py that re-bind bridge-owned globals."""
    tree = ast.parse(_src("core.py"))
    out: list[str] = []
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id in BRIDGE_OWNED_STATE:
                    out.append(t.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            if n.target.id in BRIDGE_OWNED_STATE:
                out.append(n.target.id)
    return sorted(out)


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

    def test_core_does_not_reassign_bridge_owned_state(self):
        bad = _bridge_owned_reassignments_in_core()
        self.assertEqual(
            bad,
            [],
            "core.py must facade-import bridge-owned state, not assign duplicate copies",
        )


class TestPhrasePoolTestHook(unittest.TestCase):
    def test_set_phrase_pool_for_test_forwards_to_bridge_consumer(self):
        from mcp_server import core, taste_bridge

        sentinel = [{"id": "purity-pool-sentinel", "notes": [], "kind": "harmonic"}]
        try:
            core.set_phrase_pool_for_test(sentinel)
            self.assertIs(taste_bridge._phrases(), sentinel)
            self.assertIs(core._phrases(), sentinel)
        finally:
            core.set_phrase_pool_for_test(None)
        self.assertIsNone(taste_bridge._PHRASE_POOL_OVERRIDE)


if __name__ == "__main__":
    unittest.main()
