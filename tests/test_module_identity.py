"""Module identity: importing taste_bridge first must not hijack mcp_server.core.

taste_bridge.py once carried a copy of core.py's `sys.modules.setdefault(alias, self)`
loop. Imported first, it registered *itself* as `mcp_server.core`, so core.py never
loaded. Only core.py may claim those aliases. Runs in a fresh interpreter so this
process's import order cannot mask it.
"""
from __future__ import annotations

import hashlib
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


class TestStateOwningIdentity(unittest.TestCase):
    """F4: bare and package imports of state-owning modules must be one object.

    develop_session, taste_bridge, moment, and session_ears each own mutable
    module state (file paths, caches). Two instances fork that state: a
    package-side test patch then misses gears' bare import and the suite
    writes the real default store. Probed in both import orders, in a fresh
    interpreter each time so this process's import order cannot mask a fork.
    """

    def _probe(self, first: str) -> None:
        pkg_imports = (
            "from mcp_server import (develop_session as pkg_ds, taste_bridge as pkg_tb,\n"
            "    moment as pkg_m, session_ears as pkg_se)\n"
        )
        pairs = (
            "pairs = [(develop_session, pkg_ds), (taste_bridge, pkg_tb),\n"
            "         (moment, pkg_m), (session_ears, pkg_se)]"
        )
        if first == "bare":
            imports = (
                "import develop_session, taste_bridge, moment, session_ears\n"
                + pkg_imports + pairs
            )
        else:
            imports = (
                pkg_imports
                + "import develop_session, taste_bridge, moment, session_ears\n"
                + pairs
            )
        snippet = f"""
import os, sys
sys.path.insert(0, {ROOT!r})
sys.path.insert(0, os.path.join({ROOT!r}, 'mcp_server'))
{imports}
for bare, pkg in pairs:
    assert bare is pkg, (bare, pkg)
marker = object()
develop_session._module_identity_probe = marker
assert pkg_ds._module_identity_probe is marker
moment._module_identity_probe = marker
assert pkg_m._module_identity_probe is marker
print('STATE_OWNING_IDENTITY_OK_{first}')
"""
        r = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True, text=True, cwd=ROOT, timeout=180,
        )
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn("STATE_OWNING_IDENTITY_OK_%s" % first, r.stdout)

    def test_bare_then_package(self):
        self._probe("bare")

    def test_package_then_bare(self):
        self._probe("package")

    def test_package_patch_intercepts_bare_import(self):
        """The exact F4 failure: gears' bare develop_session must see a
        package-side patch, or judge_answer writes tools/ideas/develop_session.json."""
        for order in (
            "import develop_session\nfrom mcp_server import develop_session as pkg_ds",
            "from mcp_server import develop_session as pkg_ds\nimport develop_session",
        ):
            snippet = f"""
import os, sys
from unittest import mock
sys.path.insert(0, {ROOT!r})
sys.path.insert(0, os.path.join({ROOT!r}, 'mcp_server'))
{order}
with mock.patch.object(pkg_ds, "record_drop", return_value={{"patched": True}}):
    assert develop_session.record_drop() == {{"patched": True}}, {order!r}
print("PATCH_INTERCEPT_OK")
"""
            r = subprocess.run(
                [sys.executable, "-c", snippet],
                capture_output=True, text=True, cwd=ROOT, timeout=180,
            )
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            self.assertIn("PATCH_INTERCEPT_OK", r.stdout)


class TestJudgmentWriteBoundary(unittest.TestCase):
    """F4: the judgment suite must not touch real default stores.

    Runs tests/test_judgment_repairs.py in a subprocess behind the shared
    suite guard (tests/_guard.py, via sys.addaudithook) that refuses any
    mutating open/rename/remove aimed at the worktree store and tools/ideas
    roots — so a regression FAILS BEFORE any default-state mutation instead
    of damaging data first — and fingerprints the default sinks before and
    after as a retained backstop. Temp fixture paths stay permitted so the
    real suite still runs. This observes real filesystem effects regardless
    of which import spelling production code uses internally. No blanket
    production path guards are added; the guard lives in this test's
    subprocess preamble only, in the strict default (no lock exception).
    """

    SINKS = (
        "tools/ideas/develop_session.json",
        "tools/ideas/develop_session.json.lock",
        "tools/ideas/ears_session.json",
        "tools/ideas/ears_session.json.lock",
        "store/session_ledger.json",
        "store/ratings.json",
        "store/taste_model.json",
    )

    # Subprocess preamble: fail-fast write boundary built from the one shared
    # implementation in tests/_guard.py (loaded by file path so this process's
    # already-installed hook set cannot mask it). See tests/_guard.py for the
    # audited-event semantics.
    _GUARD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "_guard.py")
    GUARD_PREAMBLE = (
        "import importlib.util as _ilu, os\n"
        "_spec = _ilu.spec_from_file_location("
        "'suite_guard_impl', %r)\n"
        "_guard_mod = _ilu.module_from_spec(_spec)\n"
        "_spec.loader.exec_module(_guard_mod)\n"
        "_guard_mod.install("
        "os.environ['GUARD_ROOTS'].split(os.pathsep))\n"
    ) % _GUARD_FILE

    RUN_JUDGMENT = """
import sys, unittest
sys.path.insert(0, %r)
suite = unittest.TestLoader().loadTestsFromName("tests.test_judgment_repairs")
result = unittest.TextTestRunner(verbosity=1).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
""" % ROOT

    def _guard_roots(self) -> str:
        return os.pathsep.join(
            (os.path.join(ROOT, "store"), os.path.join(ROOT, "tools", "ideas")))

    def _fingerprint(self) -> dict:
        fp = {}
        for rel in self.SINKS:
            p = os.path.join(ROOT, rel)
            if not os.path.exists(p):
                fp[rel] = None
                continue
            with open(p, "rb") as fh:
                fp[rel] = (os.path.getsize(p), hashlib.sha256(fh.read()).hexdigest())
        return fp

    def _run_guarded(self, payload: str, guard_roots: str,
                     timeout: int) -> "subprocess.CompletedProcess[str]":
        env = dict(os.environ, GUARD_ROOTS=guard_roots)
        return subprocess.run(
            [sys.executable, "-c", self.GUARD_PREAMBLE + payload],
            capture_output=True, text=True, cwd=ROOT, timeout=timeout, env=env,
        )

    def test_judgment_suite_leaves_default_stores_untouched(self):
        before = self._fingerprint()
        r = self._run_guarded(self.RUN_JUDGMENT, self._guard_roots(), 300)
        self.assertEqual(r.returncode, 0,
                         (r.stdout + r.stderr)[-3000:])
        self.assertEqual(self._fingerprint(), before,
                         "judgment tests wrote a real default store")

    def test_write_guard_blocks_protected_mutations_and_permits_temp(self):
        """The guard itself: protected sentinel writes fail, temp writes pass."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            guarded = os.path.join(td, "guarded")
            os.mkdir(guarded)
            sentinel = os.path.join(guarded, "sentinel.json")
            with open(sentinel, "w") as fh:
                fh.write('{"keep": true}')
            permitted_dir = os.path.join(td, "open")
            os.mkdir(permitted_dir)
            permitted = os.path.join(permitted_dir, "fixture.json")
            payload = (
                "\nimport os\n"
                "blocked = 0\n"
                "sentinel = %r\n"
                "permitted = %r\n"
                "stage = os.path.join(os.path.dirname(permitted), 'stage.json')\n"
                "open(stage, 'w').write('x')\n"
                "for label, fn in [\n"
                "    ('open', lambda: open(sentinel, 'w')),\n"
                "    ('replace', lambda: os.replace(stage, sentinel)),\n"
                "    ('pathlib', lambda: __import__('pathlib').Path(sentinel).write_text('x')),\n"
                "    ('remove', lambda: os.remove(sentinel)),\n"
                "]:\n"
                "    try:\n"
                "        fn()\n"
                "    except OSError as e:\n"
                "        assert 'test-suite guard' in str(e), (label, e)\n"
                "        blocked += 1\n"
                "    else:\n"
                "        raise AssertionError('guard let %%s through' %% label)\n"
                "assert blocked == 4, blocked\n"
                "open(permitted, 'w').write('fixture')\n"
                "print('GUARD_SELFTEST_OK blocked=%%d' %% blocked)\n"
                % (sentinel, permitted)
            )
            r = self._run_guarded(payload, guarded, 120)
            self.assertEqual(r.returncode, 0,
                             (r.stdout + r.stderr)[-3000:])
            self.assertIn("GUARD_SELFTEST_OK blocked=4", r.stdout)
            with open(sentinel) as fh:
                self.assertEqual(fh.read(), '{"keep": true}')
            with open(permitted) as fh:
                self.assertEqual(fh.read(), "fixture")


class TestLegacyPackageIdentity(unittest.TestCase):
    def _probe(self, imports: str) -> None:
        snippet = f"""
import os, sys
sys.path.insert(0, {ROOT!r})
sys.path.insert(0, os.path.join({ROOT!r}, 'mcp_server'))
{imports}
for name in ('ledger', 'gears', 'ears', 'jam_lifecycle'):
    mod = sys.modules[name]
    assert sys.modules['mcp_server.' + name] is mod, name
    marker = object()
    mod._module_identity_probe = marker
    assert sys.modules['mcp_server.' + name]._module_identity_probe is marker
print('MODULE_ALIAS_IDENTITY_OK')
"""
        r = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True, text=True, cwd=ROOT, timeout=180,
        )
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn("MODULE_ALIAS_IDENTITY_OK", r.stdout)

    def test_bare_then_package(self):
        self._probe("import ledger, gears, ears, jam_lifecycle")

    def test_package_then_bare(self):
        self._probe(
            "from mcp_server import ledger, gears, ears, jam_lifecycle\n"
            "import ledger, gears, ears, jam_lifecycle"
        )

    def test_audio_ref_both_entrypoints_share_state(self):
        snippets = [f"""
import os, sys
sys.path.insert(0, {ROOT!r})
sys.path.insert(0, os.path.join({ROOT!r}, 'tools'))
import audio_ref
from tools import audio_ref as package_audio_ref
assert audio_ref is package_audio_ref
marker = object()
audio_ref._module_identity_probe = marker
assert package_audio_ref._module_identity_probe is marker
print('AUDIO_REF_ALIAS_IDENTITY_OK')
""", f"""
import os, sys
sys.path.insert(0, {ROOT!r})
sys.path.insert(0, os.path.join({ROOT!r}, 'tools'))
from tools import audio_ref as package_audio_ref
import audio_ref
assert audio_ref is package_audio_ref
marker = object()
package_audio_ref._module_identity_probe = marker
assert audio_ref._module_identity_probe is marker
print('AUDIO_REF_ALIAS_IDENTITY_OK')
"""]
        for snippet in snippets:
            r = subprocess.run(
                [sys.executable, "-c", snippet],
                capture_output=True, text=True, cwd=ROOT, timeout=180,
            )
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            self.assertIn("AUDIO_REF_ALIAS_IDENTITY_OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
