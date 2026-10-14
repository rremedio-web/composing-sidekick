"""Unit tests for mcp_server/core_shared.py."""
from __future__ import annotations

import unittest

import datetime as _dt
import sys
import json
import os
import subprocess
import tempfile
from unittest import mock

# Insert repo root to allow imports from mcp_server/ and analyze/
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mcp_server import core_shared


def _freeze_today(frozen):
    """Patch the clock core_shared reads to an aware-UTC `frozen` datetime.

    core_shared calls datetime.datetime.now/.strptime/.fromtimestamp and
    datetime.timedelta/timezone through its module-global `datetime` name,
    so patching that attribute freezes both stamping (_rolling_backup) and
    windowing (_prune_rolling_backups) without touching the real module.
    """
    import datetime as _real_dt
    import types

    class _FrozenDateTime(_real_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen.replace(tzinfo=None)
            return frozen.astimezone(tz)

    fake = types.SimpleNamespace(
        datetime=_FrozenDateTime,
        timedelta=_real_dt.timedelta,
        timezone=_real_dt.timezone,
    )
    return mock.patch.object(core_shared, "datetime", fake)


_FROZEN_DAY_ONE = _dt.datetime(
    2026, 3, 14, 23, 30, tzinfo=_dt.timezone.utc)
_FROZEN_DAY_TWO = _dt.datetime(
    2026, 3, 15, 0, 30, tzinfo=_dt.timezone.utc)


class TestJsonAtomic(unittest.TestCase):
    """Test _save_json_atomic writes + reloads a dict round-trip via tempfile.TemporaryDirectory."""

    def test_roundtrip(self):
        """_save_json_atomic writes + reloads a dict round-trip via tempfile.TemporaryDirectory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.json")
            data = {"key": "value", "number": 42}
            core_shared._save_json_atomic(path, data)
            reloaded = core_shared._load_json(path)
            self.assertEqual(reloaded, data)


class TestWriteTextAtomic(unittest.TestCase):
    """Test _write_text_atomic writes exact text."""

    def test_exact_text(self):
        """_write_text_atomic writes exact text."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.txt")
            text = "Hello, world!\n"
            core_shared._write_text_atomic(path, text)
            with open(path) as fh:
                self.assertEqual(fh.read(), text)


class TestSetRatingsPath(unittest.TestCase):
    """Test set_ratings_path override: point at a temp path, assert _ratings_path() returns it,
    call _save_ratings_file({"ratings": [1]}) and _load_ratings_file() round-trips, then reset
    with None and assert default restored."""

    def test_override_and_reset(self):
        """set_ratings_path override: point at a temp path, assert _ratings_path() returns it,
        call _save_ratings_file({"ratings": [1]}) and _load_ratings_file() round-trips, then reset
        with None and assert default restored."""
        with tempfile.TemporaryDirectory() as tmpdir:
            override_path = os.path.join(tmpdir, "ratings.json")
            # Override
            core_shared.set_ratings_path(override_path)
            self.assertEqual(core_shared._ratings_path(), override_path)
            # Write and reload
            core_shared._save_ratings_file({"ratings": [1]})
            reloaded = core_shared._load_ratings_file()
            self.assertEqual(reloaded, {"ratings": [1]})
            # Reset
            core_shared.set_ratings_path(None)
            self.assertEqual(core_shared._ratings_path(), core_shared.RATINGS_JSON)


class TestIntervalBuckets(unittest.TestCase):
    """Test _interval_buckets output sums to ~1.0 on a simple two-note input."""

    def test_sum_to_one(self):
        """_interval_buckets output sums to ~1.0 on a simple two-note input."""
        notes = [
            {"pitch": 60, "start": 0.0, "duration": 0.25},
            {"pitch": 64, "start": 1.0, "duration": 0.25},
        ]
        buckets = core_shared._interval_buckets(notes)
        total = sum(buckets)
        self.assertAlmostEqual(total, 1.0, places=5)


class TestRatingsFileLock(unittest.TestCase):
    """Test _ratings_file_lock can be entered/exited around a temp ratings path without error."""

    def test_lock_context(self):
        """_ratings_file_lock creates a persistent .lock beside the overridden ratings path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ratings_path = os.path.join(tmpdir, "ratings.json")
            lock_path = ratings_path + ".lock"
            core_shared.set_ratings_path(ratings_path)
            try:
                self.assertFalse(os.path.exists(lock_path))
                with core_shared._ratings_file_lock():
                    self.assertTrue(os.path.exists(lock_path))
                # flock releases on close, but the lock FILE persists by design
                self.assertTrue(os.path.exists(lock_path))
            finally:
                core_shared.set_ratings_path(None)


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


class TestDurability(unittest.TestCase):
    """Fsync + rolling backups for the durable judgment stores."""

    def _synced_kinds(self):
        """Patch file+dir sync; return list of (channel, is_dir) per sync call."""
        import stat
        from unittest import mock

        kinds: list = []
        exit_stack = []

        def _record_fsync(fd):
            kinds.append(("fsync", stat.S_ISDIR(os.fstat(fd).st_mode)))

        exit_stack.append(mock.patch.object(os, "fsync", side_effect=_record_fsync))
        if sys.platform == "darwin":
            import fcntl as _fcntl_mod

            _full = getattr(_fcntl_mod, "F_FULLFSYNC", None)
            _real_fcntl = _fcntl_mod.fcntl

            def _record_fcntl(fd, cmd, *args):
                if _full is not None and cmd == _full:
                    kinds.append(("fullfsync", stat.S_ISDIR(os.fstat(fd).st_mode)))
                    return None
                return _real_fcntl(fd, cmd, *args)

            exit_stack.append(mock.patch.object(_fcntl_mod, "fcntl", side_effect=_record_fcntl))
        return kinds, exit_stack

    def test_save_fsyncs_file_and_directory(self):
        from unittest import mock  # noqa: F401 (kept for symmetry with sibling tests)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.json")
            kinds, patches = self._synced_kinds()
            for p in patches:
                p.start()
            try:
                core_shared._save_json_atomic(path, {"key": "value"})
            finally:
                for p in patches:
                    p.stop()
            isdirs = {is_dir for _, is_dir in kinds}
            self.assertIn(False, isdirs, f"file fd was never synced: {kinds}")
            self.assertIn(True, isdirs, f"directory fd was never synced: {kinds}")
            self.assertEqual(core_shared._load_json(path), {"key": "value"})

    def test_write_text_atomic_syncs_file_fd(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.txt")
            kinds, patches = self._synced_kinds()
            for p in patches:
                p.start()
            try:
                core_shared._write_text_atomic(path, "hello\n")
            finally:
                for p in patches:
                    p.stop()
            self.assertIn((kinds[0][0], False), [(c, d) for c, d in kinds],
                          f"file fd was never synced: {kinds}")
            with open(path) as fh:
                self.assertEqual(fh.read(), "hello\n")

    def test_full_fsync_falls_back_to_fsync(self):
        import stat
        from unittest import mock

        if sys.platform != "darwin":
            self.skipTest("F_FULLFSYNC path is darwin-only")
        import fcntl as _fcntl_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "t.txt")
            with open(path, "w") as fh:
                fh.write("x")
            with open(path) as fh:
                fd = fh.fileno()
                self.assertFalse(stat.S_ISDIR(os.fstat(fd).st_mode))
                synced = []
                with mock.patch.object(
                        _fcntl_mod, "fcntl", side_effect=OSError("no fullsync")) as ff, \
                        mock.patch.object(os, "fsync", side_effect=lambda f: synced.append(f)):
                    core_shared._full_fsync(fd)
                ff.assert_called_once()
                self.assertEqual(synced, [fd])

    def test_committed_save_survives_dir_sync_failure(self):
        """os.replace won; a later directory-sync error must not fail the save."""
        from unittest import mock

        real_open = os.open

        def _no_dir_open(path, flags, *args, **kwargs):
            if isinstance(path, str) and os.path.isdir(path):
                raise PermissionError("EACCES (simulated 0o300 directory)")
            return real_open(path, flags, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.json")
            data = {"key": "new-content"}
            with mock.patch.object(os, "open", side_effect=_no_dir_open):
                core_shared._save_json_atomic(path, data)
                core_shared._write_text_atomic(os.path.join(tmpdir, "t.txt"), "hi\n")
            self.assertEqual(core_shared._load_json(path), data)
            with open(os.path.join(tmpdir, "t.txt")) as fh:
                self.assertEqual(fh.read(), "hi\n")

    def test_backup_preserves_source_permissions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "ratings.json")
            with open(path, "w") as fh:
                fh.write("{}")
            os.chmod(path, 0o600)
            backup = core_shared._rolling_backup(path)
            self.assertIsNotNone(backup)
            import stat
            self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)

    def test_backup_created_with_source_mode(self):
        """os.open receives the source mode, so there is no 0644 window."""
        import stat

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "ratings.json")
            with open(path, "w") as fh:
                fh.write("{}")
            os.chmod(path, 0o600)
            seen = {}
            real_open = os.open

            def _capture(p, flags, *args, **kwargs):
                if isinstance(p, str) and p.endswith(".json") and "backups" in p:
                    seen["flags"] = flags
                    seen["mode"] = args[0] if args else None
                return real_open(p, flags, *args, **kwargs)

            with mock.patch.object(os, "open", side_effect=_capture):
                backup = core_shared._rolling_backup(path)
            self.assertIsNotNone(backup)
            self.assertIn("flags", seen)
            self.assertTrue(seen["flags"] & os.O_EXCL)
            self.assertEqual(stat.S_IMODE(seen["mode"]), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)

    def test_backup_mode_survives_without_chmod(self):
        """With chmod disabled the backup is still 0600: the mode came
        from os.open, not from the post-write chmod (on the old code this
        stayed at the 0644 open default under a 022 umask)."""
        import stat

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "ratings.json")
            with open(path, "w") as fh:
                fh.write("{}")
            os.chmod(path, 0o600)
            old_umask = os.umask(0o022)
            try:
                with mock.patch.object(
                        os, "chmod", side_effect=OSError("blocked")):
                    backup = core_shared._rolling_backup(path)
            finally:
                os.umask(old_umask)
            self.assertIsNotNone(backup)
            self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)

    def test_backup_skips_identical_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "ratings.json")
            core_shared.set_ratings_path(path)
            try:
                core_shared._save_ratings_file({"ratings": [{"n": 1}]})
                core_shared._save_ratings_file({"ratings": [{"n": 1}]})
                core_shared._save_ratings_file({"ratings": [{"n": 1}]})
            finally:
                core_shared.set_ratings_path(None)
            backup_dir = os.path.join(tmpdir, "backups", "ratings.json")
            self.assertEqual(len(os.listdir(backup_dir)), 1)

    def test_identical_skip_yields_to_next_utc_day(self):
        """Identical bytes across two UTC days write two backups.

        The skip applies only when the newest backup is from today (UTC),
        so the first save of each day always writes that day's protected
        first backup — otherwise the pre-session state could be pruned.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "ratings.json")
            with open(path, "w") as fh:
                fh.write('{"ratings": []}')
            with _freeze_today(_FROZEN_DAY_ONE):
                first = core_shared._rolling_backup(path, keep=1)
            self.assertIsNotNone(first)
            with _freeze_today(_FROZEN_DAY_TWO):
                second = core_shared._rolling_backup(path, keep=1)
            self.assertIsNotNone(second)
            self.assertNotEqual(first, second)
            backup_dir = os.path.join(tmpdir, "backups", "ratings.json")
            names = sorted(os.listdir(backup_dir))
            self.assertEqual(len(names), 2)
            # keep=1 would prune everything but the newest + daily firsts:
            # the older day's first backup survives as its day's first.
            self.assertEqual(
                [os.path.join(backup_dir, n) for n in names], [first, second])
            self.assertTrue(os.path.basename(first).startswith(
                "ratings.json.20260314T"))
            self.assertTrue(os.path.basename(second).startswith(
                "ratings.json.20260315T"))

    def test_identical_skip_holds_within_same_utc_day(self):
        """Identical bytes twice on one UTC day still write a single backup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "ratings.json")
            with open(path, "w") as fh:
                fh.write('{"ratings": []}')
            with _freeze_today(_FROZEN_DAY_TWO):
                first = core_shared._rolling_backup(path)
                second = core_shared._rolling_backup(path)
            self.assertEqual(first, second)
            backup_dir = os.path.join(tmpdir, "backups", "ratings.json")
            self.assertEqual(len(os.listdir(backup_dir)), 1)

    def test_retention_keeps_newest_plus_daily_firsts(self):
        import datetime
        import shutil

        def _content(backup_dir, name):
            with open(os.path.join(backup_dir, name)) as fh:
                return fh.read()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "ratings.json")
            with open(path, "w") as fh:
                fh.write("v0")
            # Fabricate one backup per UTC day for 20 days; two per day on
            # the oldest 6 so the "first of day" rule has something to keep.
            now = datetime.datetime.now(datetime.timezone.utc)
            staged = []
            for days_ago in range(20):
                day = now - datetime.timedelta(days=days_ago)
                stamp_a = day.strftime("%Y%m%dT%H%M%S") + ("000%03dZ" % days_ago)
                staged.append(("ratings.json.%s.json" % stamp_a, "day-%d-a" % days_ago))
                if days_ago >= 14:
                    stamp_b = day.strftime("%Y%m%dT%H%M%S") + ("999%03dZ" % days_ago)
                    staged.append(("ratings.json.%s.json" % stamp_b, "day-%d-b" % days_ago))
            backup_dir = os.path.join(tmpdir, "backups", "ratings.json")
            os.makedirs(backup_dir)
            for name, content in staged:
                with open(os.path.join(backup_dir, name), "w") as fh:
                    fh.write(content)
            self.assertEqual(len(os.listdir(backup_dir)), 26)
            core_shared._rolling_backup(path, keep=5)
            after = sorted(os.listdir(backup_dir))
            by_content = {}
            for name in after:
                by_content.setdefault(_content(backup_dir, name), name)
            # Newest-keep rule: days 0..4 survive.
            for days_ago in range(5):
                self.assertIn("day-%d-a" % days_ago, by_content,
                              "newest backup day-%d pruned" % days_ago)
            # Daily-first rule: days 5..13 survive as exactly their first backup.
            for days_ago in range(5, 14):
                self.assertIn("day-%d-a" % days_ago, by_content,
                              "first-of-day day-%d pruned" % days_ago)
                self.assertTrue(
                    by_content["day-%d-a" % days_ago].endswith("000%03dZ.json" % days_ago))
            # Outside both rules: days 14..19 pruned entirely (both stamps).
            for days_ago in range(14, 20):
                self.assertNotIn("day-%d-a" % days_ago, by_content)
                self.assertNotIn("day-%d-b" % days_ago, by_content)

    def test_retention_prunes_second_backup_of_same_in_window_day(self):
        """A second backup on an in-window day is pruned; its first survives.

        "Today" is frozen so the window cannot shift under the test at a UTC
        midnight boundary.
        """
        frozen = _dt.datetime(2026, 3, 15, 12, 0,
                              tzinfo=_dt.timezone.utc)
        staged = []
        for days_ago in range(14):
            day = frozen - _dt.timedelta(days=days_ago)
            staged.append(
                ("ratings.json.%s.json"
                 % (day.strftime("%Y%m%dT000000") + ("000%03dZ" % days_ago)),
                 "day-%d-a" % days_ago))
        day7 = frozen - _dt.timedelta(days=7)
        staged.append(
            ("ratings.json.%s.json"
             % (day7.strftime("%Y%m%dT120000") + "999007Z"),
             "day-7-b"))

        def _content(backup_dir, name):
            with open(os.path.join(backup_dir, name)) as fh:
                return fh.read()

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_dir = os.path.join(tmpdir, "backups", "ratings.json")
            os.makedirs(backup_dir)
            for name, content in staged:
                with open(os.path.join(backup_dir, name), "w") as fh:
                    fh.write(content)
            with _freeze_today(frozen):
                core_shared._prune_rolling_backups(backup_dir, keep=5)
                after = sorted(os.listdir(backup_dir))
            by_content = {}
            for name in after:
                by_content.setdefault(_content(backup_dir, name), name)
            # Day 7 keeps exactly its first stamp; the later one is pruned.
            self.assertIn("day-7-a", by_content)
            self.assertNotIn("day-7-b", by_content)
            self.assertTrue(by_content["day-7-a"].endswith("000007Z.json"))
            # The rest of the window is untouched: newest 0..4 plus daily
            # firsts 5..13.
            for days_ago in list(range(5)) + list(range(8, 14)):
                self.assertIn("day-%d-a" % days_ago, by_content,
                              "in-window day-%d pruned" % days_ago)

    def test_ledger_save_twice_yields_single_backup_with_first_content(self):
        import json as _json
        from mcp_server import ledger as _ledger

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "session_ledger.json")
            first = {"version": 2, "sessions": [], "entries": [{"id": "e-1"}],
                     "adoptions": [], "observations": [], "judgment_events": []}
            _ledger._save(dict(first), path)
            second = dict(first, entries=[{"id": "e-1"}, {"id": "e-2"}])
            _ledger._save(second, path)
            backup_dir = os.path.join(tmpdir, "backups", "session_ledger.json")
            names = sorted(os.listdir(backup_dir))
            self.assertEqual(len(names), 1)
            with open(os.path.join(backup_dir, names[0])) as fh:
                self.assertEqual(_json.load(fh), first)

    def test_ledger_fallback_save_survives_dir_sync_failure(self):
        """The inline fallback (no core_shared) also never fails a committed save."""
        import builtins
        from unittest import mock
        from mcp_server import ledger as _ledger

        real_import = builtins.__import__

        def _no_shared(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "core_shared" or (
                    name == "mcp_server" and fromlist and "core_shared" in fromlist):
                raise ModuleNotFoundError("blocked for fallback test")
            return real_import(name, globals, locals, fromlist, level)

        real_open = os.open

        def _no_dir_open(path, flags, *args, **kwargs):
            if isinstance(path, str) and os.path.isdir(path):
                raise PermissionError("EACCES (simulated 0o300 directory)")
            return real_open(path, flags, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "session_ledger.json")
            data = {"version": 2, "sessions": [], "entries": [],
                    "adoptions": [], "observations": [], "judgment_events": []}
            with mock.patch.object(builtins, "__import__", side_effect=_no_shared), \
                    mock.patch.object(os, "open", side_effect=_no_dir_open):
                _ledger._save(dict(data), path)
            with open(path) as fh:
                import json as _json
                self.assertEqual(_json.load(fh), data)

    def test_rolling_backup_keeps_newest_twenty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "ratings.json")
            core_shared.set_ratings_path(path)
            try:
                for i in range(25):
                    core_shared._save_ratings_file({"ratings": [{"n": i}]})
            finally:
                core_shared.set_ratings_path(None)
            backup_dir = os.path.join(tmpdir, "backups", "ratings.json")
            names = sorted(os.listdir(backup_dir))
            # 20 newest plus today's first backup (daily-first protection:
            # all 25 saves land on the same UTC day, so the very first
            # backup is pinned alongside the newest 20).
            self.assertEqual(len(names), 21)
            with open(os.path.join(backup_dir, names[-1]), "rb") as fh:
                newest = fh.read()
            previous = (json.dumps({"ratings": [{"n": 23}]}, indent=2) + "\n").encode()
            self.assertEqual(newest, previous)
            with open(os.path.join(backup_dir, names[0]), "rb") as fh:
                oldest = fh.read()
            first = (json.dumps({"ratings": [{"n": 0}]}, indent=2) + "\n").encode()
            self.assertEqual(oldest, first)
            self.assertEqual(
                core_shared._load_json(path), {"ratings": [{"n": 24}]})

    def test_blocked_backup_dir_never_blocks_save(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "ratings.json")
            core_shared._save_json_atomic(path, {"ratings": []})
            with open(os.path.join(tmpdir, "backups"), "w") as fh:
                fh.write("not a directory")
            self.assertIsNone(core_shared._rolling_backup(path))
            core_shared.set_ratings_path(path)
            try:
                core_shared._save_ratings_file({"ratings": [{"n": 1}]})
            finally:
                core_shared.set_ratings_path(None)
            self.assertEqual(
                core_shared._load_json(path), {"ratings": [{"n": 1}]})

    def test_bare_and_package_imports_share_state(self):
        snippet = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "import mcp_server.core_shared as a\n"
            "sys.path.insert(0, %r)\n"
            "import core_shared as b\n"
            "assert a is b, 'core_shared forked across import styles'\n"
            "marker = object()\n"
            "a._module_identity_probe = marker\n"
            "assert b._module_identity_probe is marker\n"
            "print('CORE_SHARED_IDENTITY_OK')\n"
        ) % (ROOT, os.path.join(ROOT, "mcp_server"))
        r = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True, text=True, cwd=ROOT, timeout=180,
        )
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn("CORE_SHARED_IDENTITY_OK", r.stdout)
