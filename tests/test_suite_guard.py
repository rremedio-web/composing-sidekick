"""Subprocess probe of the shared suite guard (tests/_guard.py).

Runs in a fresh interpreter with temp roots so this process's own hook set
stays isolated: protected writes refused, temp writes allowed, loopback
connects to the guarded port refused, and — in the strict default — even
existing *.lock opens refused. The opt-in (allow_existing_locks=True) keeps
existing *.lock opens permitted while still refusing new ones. Dir-fd
pairing is pinned by a nested same-basename rmtree, split-fd rename
probes, a relative BYTES probe, and a closed-dir_fd probe. Only the
connect probe needs a loopback listener; without one it is skipped via a
sentinel port while every other probe still runs.
"""
from __future__ import annotations

import importlib.util as _ilu
import os
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD_FILE = os.path.join(ROOT, "tests", "_guard.py")


def _load_guard():
    spec = _ilu.spec_from_file_location("suite_guard_probe", GUARD_FILE)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PAYLOAD = """
import importlib.util as _ilu, os, socket
_impl = os.environ.get('GUARD_IMPL', %r)
_spec = _ilu.spec_from_file_location('suite_guard_impl', _impl)
_guard_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_guard_mod)
_guarded = os.environ['GUARD_ROOT']
_open_dir = os.environ['OPEN_ROOT']
_other = os.environ.get('GUARD_OTHER_ROOT', '')
_port = int(os.environ['GUARD_PP_PORT'])
_skip_connect = os.environ.get('GUARD_SKIP_CONNECT') == '1'
_guard_mod.install([_guarded], pp_port=_port)
# A second install is a no-op: it must not start guarding anything else.
if _other:
    _guard_mod.install([_other], pp_port=_port + 2)
    open(os.path.join(_other, 'other.json'), 'w').write('other')
    try:
        socket.create_connection(('127.0.0.1', _port + 2), timeout=5)
    except Exception as e:
        assert 'test-suite guard' not in str(e), ('other-port must not trip the guard', e)
blocked = 0
expected = 0
def _expect_refusal(label, fn, exc=Exception):
    global blocked
    try:
        fn()
    except exc as e:
        assert 'test-suite guard' in str(e), (label, e)
        blocked += 1
    else:
        raise AssertionError('guard let %%s through' %% label)
sentinel = os.path.join(_guarded, 'sentinel.json')
stage = os.path.join(_open_dir, 'stage.json')
open(stage, 'w').write('x')
_probes = [
        ('open', lambda: open(sentinel, 'w')),
        ('replace', lambda: os.replace(stage, sentinel)),
        ('pathlib', lambda: __import__('pathlib').Path(sentinel).write_text('x')),
        ('remove', lambda: os.remove(sentinel)),
        ('existing-lock', lambda: open(os.path.join(_guarded, 'existing.lock'), 'a+')),
        ('new-lock', lambda: open(os.path.join(_guarded, 'new.lock'), 'a+'))]
if not _skip_connect:
    # Only this probe needs a real listener (bound by the parent test);
    # without loopback the parent passes a sentinel port and skips it.
    _probes.insert(4, ('connect',
                       lambda: socket.create_connection(('127.0.0.1', _port), timeout=5)))
for _label, _fn in _probes:
    _expect_refusal(_label, _fn)
    expected += 1
try:
    socket.create_connection(('127.0.0.1', _port + 1), timeout=5)
except Exception as e:
    assert 'test-suite guard' not in str(e), ('other-port must not trip the guard', e)
if os.path.exists(_guarded.swapcase()):
    # Case-insensitive filesystem: a differently-cased root spelling is
    # still the guarded root.
    _expect_refusal('case-variant',
                    lambda: open(os.path.join(_guarded.swapcase(), 'sentinel.json'), 'w'))
    expected += 1
assert blocked == expected, (blocked, expected)
import shutil
# Nested same-basename tree: rmtree passes the top-level path in full, so
# bare names with dir_fd only appear for NESTED entries. The inner dir is
# deliberately named like the guarded root: resolving those bare names
# against the cwd (the guarded root's parent here) instead of their own
# dir_fd would misattribute this temp tree to the guard and refuse it.
_root_base = os.path.basename(_guarded)
wrap = os.path.join(_open_dir, 'wrap')
inner = os.path.join(wrap, _root_base)
os.mkdir(wrap)
os.mkdir(inner)
open(os.path.join(inner, 'f.json'), 'w').write('x')
shutil.rmtree(wrap)
assert not os.path.exists(wrap)
# rename with split dir_fds: only the DESTINATION is inside the guarded
# root, so the guard must refuse (each path resolves against its own fd).
_open_fd = os.open(_open_dir, os.O_RDONLY)
_guarded_fd = os.open(_guarded, os.O_RDONLY)
try:
    open(os.path.join(_open_dir, 'a.txt'), 'w').write('a')
    _expect_refusal('rename-into-guarded',
                    lambda: os.rename('a.txt', 'b.txt',
                                      src_dir_fd=_open_fd, dst_dir_fd=_guarded_fd))
    expected += 1
    # Reverse: the SOURCE is named like the guarded root but lives in a
    # free dir, so the guard must allow. (Against the cwd this bare name
    # would resolve to the guarded root itself and be refused.)
    wrap2 = os.path.join(_open_dir, 'wrap2')
    os.mkdir(wrap2)
    os.mkdir(os.path.join(wrap2, _root_base))
    _wrap2_fd = os.open(wrap2, os.O_RDONLY)
    try:
        os.rename(_root_base, 'relocated', src_dir_fd=_wrap2_fd, dst_dir_fd=_wrap2_fd)
    finally:
        os.close(_wrap2_fd)
    assert os.path.isdir(os.path.join(wrap2, 'relocated'))
    assert not os.path.exists(os.path.join(wrap2, _root_base))
    # Relative BYTES paths beside a free dir_fd are allowed too.
    open(os.path.join(_open_dir, 'bx.txt'), 'w').write('bx')
    os.remove(b'bx.txt', dir_fd=_open_fd)
    assert not os.path.exists(os.path.join(_open_dir, 'bx.txt'))
    # An unresolvable (closed) dir_fd fails CLOSED, never unattributed.
    _dead = os.open(_open_dir, os.O_RDONLY)
    os.close(_dead)
    _expect_refusal('closed-dir-fd',
                    lambda: os.remove('whatever.txt', dir_fd=_dead))
    expected += 1
finally:
    os.close(_open_fd)
    os.close(_guarded_fd)
assert blocked == expected, (blocked, expected)
permitted = os.path.join(_open_dir, 'fixture.json')
open(permitted, 'w').write('fixture')
print('GUARD_SUBPROCESS_OK blocked=%%d' %% blocked)
""" % GUARD_FILE

LOCK_OPTIN_PAYLOAD = """
import importlib.util as _ilu, os
_spec = _ilu.spec_from_file_location('suite_guard_impl', %r)
_guard_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_guard_mod)
_guarded = os.environ['GUARD_ROOT']
_port = int(os.environ['GUARD_PP_PORT'])
_guard_mod.install([_guarded], pp_port=_port, allow_existing_locks=True)
existing_lock = os.path.join(_guarded, 'existing.lock')
with open(existing_lock, 'a+') as fh:
    fh.write('heartbeat\\n')
try:
    open(os.path.join(_guarded, 'new.lock'), 'a+')
except OSError as e:
    assert 'test-suite guard' in str(e), e
else:
    raise AssertionError('guard let new-lock through')
print('GUARD_LOCK_OPTIN_OK')
""" % GUARD_FILE


# No connect is ever attempted on this port: it is only passed through
# install() when the child has no connect probe (lock opt-in), or when
# loopback bind is unavailable and the connect probe is skipped.
SENTINEL_PP_PORT = 34999


class TestSuiteGuardSubprocess(unittest.TestCase):
    def _guard_env(self, td):
        guarded = os.path.join(td, "guarded")
        open_root = os.path.join(td, "open")
        other = os.path.join(td, "other")
        os.mkdir(guarded)
        os.mkdir(open_root)
        os.mkdir(other)
        sentinel = os.path.join(guarded, "sentinel.json")
        with open(sentinel, "w") as fh:
            fh.write('{"keep": true}')
        with open(os.path.join(guarded, "existing.lock"), "w") as fh:
            fh.write("locked\n")
        return guarded, open_root, other, sentinel

    def _listener_or_sentinel(self):
        # A real listener proves the guard (not a dead port) refused the
        # connect: with the hook installed the child sees the guard's
        # ConnectionRefusedError before any SYN leaves. Only the connect
        # probe needs it — when loopback bind is unavailable, return a
        # sentinel port and skip just that probe instead of the whole test.
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            return listener, listener.getsockname()[1], False
        except OSError:
            listener.close()
            return None, SENTINEL_PP_PORT, True

    def test_guard_refuses_protected_writes_and_live_contact(self):
        with tempfile.TemporaryDirectory() as td:
            guarded, open_root, other, sentinel = self._guard_env(td)
            listener, port, skip_connect = self._listener_or_sentinel()
            try:
                env = dict(os.environ, GUARD_ROOT=guarded,
                           OPEN_ROOT=open_root, GUARD_OTHER_ROOT=other,
                           GUARD_PP_PORT=str(port))
                if skip_connect:
                    env["GUARD_SKIP_CONNECT"] = "1"
                r = subprocess.run(
                    [sys.executable, "-c", PAYLOAD],
                    capture_output=True, text=True, cwd=td, timeout=120,
                    env=env,
                )
            finally:
                if listener is not None:
                    listener.close()
            self.assertEqual(r.returncode, 0,
                             (r.stdout + r.stderr)[-3000:])
            self.assertIn("GUARD_SUBPROCESS_OK blocked=", r.stdout)
            with open(sentinel) as fh:
                self.assertEqual(fh.read(), '{"keep": true}')
            with open(os.path.join(open_root, "fixture.json")) as fh:
                self.assertEqual(fh.read(), "fixture")
            with open(os.path.join(other, "other.json")) as fh:
                self.assertEqual(fh.read(), "other")
            self.assertFalse(os.path.exists(os.path.join(guarded, "new.lock")))

    def test_guard_existing_lock_exception_is_opt_in(self):
        with tempfile.TemporaryDirectory() as td:
            guarded, open_root, _other, _sentinel = self._guard_env(td)
            # No connect probe here, so no listener is needed at all.
            env = dict(os.environ, GUARD_ROOT=guarded,
                       OPEN_ROOT=open_root,
                       GUARD_PP_PORT=str(SENTINEL_PP_PORT))
            r = subprocess.run(
                [sys.executable, "-c", LOCK_OPTIN_PAYLOAD],
                capture_output=True, text=True, cwd=td, timeout=120,
                env=env,
            )
            self.assertEqual(r.returncode, 0,
                             (r.stdout + r.stderr)[-3000:])
            self.assertIn("GUARD_LOCK_OPTIN_OK", r.stdout)
            self.assertFalse(os.path.exists(os.path.join(guarded, "new.lock")))


class TestGuardCaseDetection(unittest.TestCase):
    def test_detection_agrees_with_filesystem(self):
        mod = _load_guard()
        with tempfile.TemporaryDirectory() as td:
            root = os.path.join(td, "MiXeD")
            os.mkdir(root)
            self.assertEqual(mod._root_case_insensitive(root),
                             os.path.exists(root.swapcase()))

    def test_missing_root_matches_nearest_existing_ancestor(self):
        mod = _load_guard()
        with tempfile.TemporaryDirectory() as td:
            ancestor = os.path.join(td, "MiXeD")
            os.mkdir(ancestor)
            missing = os.path.join(ancestor, "no", "such", "root.json")
            self.assertFalse(os.path.exists(missing))
            # A not-yet-created guarded root must not silently default to
            # case-sensitive: it inherits its ancestor's probe result.
            self.assertEqual(mod._root_case_insensitive(missing),
                             mod._root_case_insensitive(ancestor))

    @unittest.skipUnless("PC_CASE_SENSITIVE" in getattr(os, "pathconf_names", {}),
                         "platform offers pathconf case-sensitivity")
    def test_missing_root_pathconfs_nearest_ancestor(self):
        import unittest.mock as _mock

        mod = _load_guard()
        with tempfile.TemporaryDirectory() as td:
            ancestor = os.path.join(td, "sub")
            os.mkdir(ancestor)
            missing = os.path.join(ancestor, "no", "such", "dir")
            calls = []
            real_pathconf = os.pathconf

            def _record(name, names):
                calls.append(name)
                return real_pathconf(name, names)

            with _mock.patch.object(mod.os, "pathconf", side_effect=_record):
                mod._root_case_insensitive(missing)
            self.assertTrue(calls, "expected a pathconf probe")
            for name in calls:
                self.assertEqual(name, ancestor)


if __name__ == "__main__":
    unittest.main()
