"""Offload-safe audio references: reindex keyed off artifacts + reversible
--offload-sources / --restore-sources. All fixtures live in temp dirs; the
mount check is patched so no real volume is consulted."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import audio_ref

FAKE_EXTERNAL_MOUNT = Path("/Volumes/audioref-test-external")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _make_ref(out_root: Path, ref_id: str, *, payload: bytes = b"RIFFfake-audio",
              meta: dict | None = None) -> Path:
    out = out_root / ref_id
    out.mkdir(parents=True)
    (out / "source.wav").write_bytes(payload)
    (out / "meta.json").write_text(json.dumps(meta or {
        "source": "file", "source_kind": "file", "tags": ["test"],
    }))
    return out


def _offload_meta(extra: dict | None = None) -> dict:
    meta = {"source": "file", "source_kind": "file", "tags": ["test"]}
    meta.update(extra or {})
    return meta


class OffloadTestCase(unittest.TestCase):
    """Temp OUT_ROOT / INDEX_PATH (+ fake external mount) per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out_root = self.root / "refs"
        self.out_root.mkdir()
        self.index_path = self.root / "index.json"
        self.external_parent = self.root / "external"
        self.external_parent.mkdir()
        self.external = self.external_parent / "audioref-sources"

    def tearDown(self):
        self._tmp.cleanup()

    def _patch_store(self, mount: Path | None = FAKE_EXTERNAL_MOUNT,
                     devices: bool = True):
        """Patch ROOT / OUT_ROOT / INDEX_PATH and (optionally) the mount check.

        ROOT is pinned too: main()'s confirmed offload takes an overlay backup
        under ROOT/store/backups/, which must stay inside the temp dir. The
        device check is faked unless ``devices=False`` (for the same-device
        refusal test, where the REAL st_dev must be consulted).
        """
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(audio_ref, "ROOT", self.root))
        stack.enter_context(mock.patch.object(audio_ref, "OUT_ROOT", self.out_root))
        stack.enter_context(mock.patch.object(audio_ref, "INDEX_PATH", self.index_path))
        if mount is not None:
            stack.enter_context(mock.patch.object(
                audio_ref, "_mount_point_of", return_value=mount))
        if devices:
            external = self.external_parent

            def fake_device(path):
                p = Path(path)
                if p == external or external in p.parents:
                    return 999_999
                return 1

            stack.enter_context(mock.patch.object(
                audio_ref, "_device_of", side_effect=fake_device))
        return stack

    def _reindex(self) -> dict:
        return audio_ref.reindex_audio_refs(
            root=self.out_root, index_path=self.index_path)


class TestReindexKeepsOffloadedRefs(OffloadTestCase):
    def test_reindex_keeps_offloaded_ref_and_skips_bare_dirs(self):
        payload = b"RIFFfake-audio"
        out = _make_ref(self.out_root, "yt-keep")
        bare = self.out_root / "not-a-ref"
        bare.mkdir()
        wav_only = self.out_root / "wav-only"
        wav_only.mkdir()
        (wav_only / "source.wav").write_bytes(b"RIFFpartial-fetch")
        with self._patch_store():
            # A source.wav alone IS a filed ref under the shared predicate
            # (same rule get_ref_card's disk fallback uses).
            self.assertEqual(self._reindex()["count"], 2)

            # Offload: wav gone, record in meta.json, analysis artifacts kept.
            (out / "source.wav").unlink()
            meta = _offload_meta({"source_offloaded": {
                "path": str(self.external / "yt-keep" / "source.wav"),
                "sha256": _sha(payload), "bytes": len(payload), "at": "2026-09-24T00:00:00Z",
            }})
            (out / "meta.json").write_text(json.dumps(meta))

            report = self._reindex()
        self.assertEqual(report["count"], 2)
        refs = json.loads(self.index_path.read_text())["refs"]
        self.assertIn("yt-keep", refs)
        self.assertNotIn("not-a-ref", refs)
        self.assertIn("wav-only", refs)

    def test_failed_fetch_meta_only_dir_is_not_a_phantom_ref(self):
        dead = self.out_root / "yt-dead"
        dead.mkdir()
        # A failed download writes meta.json (ok:false) with no wav and no
        # card: it was never analyzed, so it must not be indexed nor resolve.
        (dead / "meta.json").write_text(json.dumps(
            {"ok": False, "partial": False,
             "error": "RuntimeError: yt-dlp failed"}))
        with self._patch_store():
            report = self._reindex()
            with self.assertRaises(KeyError):
                audio_ref.get_ref_card("yt-dead", index_path=self.index_path)
        self.assertEqual(report["count"], 0)
        self.assertNotIn("yt-dead", json.loads(self.index_path.read_text())["refs"])


class TestOffloadSources(OffloadTestCase):
    def test_dry_run_moves_nothing(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        meta_before = (self.out_root / "yt-a" / "meta.json").read_text()
        stdout = io.StringIO()
        with self._patch_store(), contextlib.redirect_stdout(stdout):
            rc = audio_ref.main(["--offload-sources", str(self.external)])
        self.assertEqual(rc, 0)
        report = json.loads(stdout.getvalue())
        self.assertTrue(report["dry_run"])
        self.assertEqual([item["ref_id"] for item in report["plan"]], ["yt-a"])
        self.assertEqual(report["total_bytes"], len(payload))
        self.assertTrue((self.out_root / "yt-a" / "source.wav").is_file())
        self.assertEqual(
            (self.out_root / "yt-a" / "meta.json").read_text(), meta_before)
        self.assertFalse(self.external.exists())

    def test_confirm_moves_records_and_keeps_index_count(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        _make_ref(self.out_root, "yt-b", payload=payload + b"-other")
        no_meta = self.out_root / "yt-nometa"
        no_meta.mkdir()
        (no_meta / "source.wav").write_bytes(b"RIFForphan")
        with self._patch_store():
            # yt-nometa's bare wav makes it a filed ref for the INDEX (shared
            # predicate), while offload still skips it: no meta.json means no
            # place for the record to live.
            self.assertEqual(self._reindex()["count"], 3)
            result = audio_ref.offload_sources(str(self.external), confirm=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual([row["ref_id"] for row in result["offloaded"]],
                         ["yt-a", "yt-b"])
        self.assertEqual([row["reason"] for row in result["skipped"]],
                         ["no meta.json"])
        self.assertEqual(result["bytes_freed"], len(payload) + len(payload) + 6)
        for ref_id, expected in (("yt-a", payload), ("yt-b", payload + b"-other")):
            self.assertFalse((self.out_root / ref_id / "source.wav").exists())
            external = self.external / ref_id / "source.wav"
            self.assertEqual(external.read_bytes(), expected)
            record = json.loads(
                (self.out_root / ref_id / "meta.json").read_text())["source_offloaded"]
            self.assertEqual(record["path"], str(external))
            self.assertEqual(record["sha256"], _sha(expected))
            self.assertEqual(record["bytes"], len(expected))
            self.assertTrue(record["at"])
        # The offload must not drop refs from the index.
        self.assertEqual(result["reindex"]["refs_before"], 3)
        self.assertEqual(result["reindex"]["refs_after"], 3)
        self.assertTrue(result["reindex"]["equal"])
        refs = json.loads(self.index_path.read_text())["refs"]
        self.assertEqual(set(refs), {"yt-a", "yt-b", "yt-nometa"})

    def test_sha_mismatch_keeps_local_file_and_meta(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        meta_before = (self.out_root / "yt-a" / "meta.json").read_text()
        real_copy2 = shutil.copy2

        def corrupting_copy2(src, dst, **kwargs):
            real_copy2(src, dst, **kwargs)
            Path(dst).write_bytes(Path(dst).read_bytes() + b"CORRUPT")

        with self._patch_store(), \
                mock.patch.object(audio_ref.shutil, "copy2", side_effect=corrupting_copy2):
            result = audio_ref.offload_sources(str(self.external), confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual([row["ref_id"] for row in result["failed"]], ["yt-a"])
        self.assertIn("sha256 mismatch", result["failed"][0]["reason"])
        self.assertTrue((self.out_root / "yt-a" / "source.wav").is_file())
        self.assertEqual(
            (self.out_root / "yt-a" / "meta.json").read_text(), meta_before)
        # A corrupt copy must never sit at the destination name either.
        self.assertFalse((self.external / "yt-a" / "source.wav").exists())

    def test_reoffload_with_truncated_local_wav_keeps_external_copy(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        with self._patch_store():
            self.assertEqual(self._reindex()["count"], 1)
            self.assertTrue(audio_ref.offload_sources(
                str(self.external), confirm=True)["ok"])
            external = self.external / "yt-a" / "source.wav"
            meta_before = (self.out_root / "yt-a" / "meta.json").read_text()
            # Interrupted restore: truncated local wav, record still in place.
            # Re-offloading from here must NOT copy the truncated bytes over
            # the good external copy.
            (self.out_root / "yt-a" / "source.wav").write_bytes(payload[:5])
            result = audio_ref.offload_sources(str(self.external), confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual([row["ref_id"] for row in result["failed"]], ["yt-a"])
        self.assertIn("differs from the offload record",
                      result["failed"][0]["reason"])
        # The hint must describe the actual recovery: restore moves the
        # mismatched file aside and brings the recorded bytes back.
        self.assertIn("source.wav.mismatch-", result["failed"][0]["reason"])
        self.assertIn("--restore-sources", result["failed"][0]["reason"])
        self.assertEqual(external.read_bytes(), payload)
        self.assertEqual(
            (self.out_root / "yt-a" / "meta.json").read_text(), meta_before)

    def test_relisten_then_reoffload_never_overwrites_external_copy(self):
        payload = b"RIFFfake-audio"
        out = _make_ref(self.out_root, "yt-a", payload=payload)
        with self._patch_store():
            self._reindex()
            self.assertTrue(audio_ref.offload_sources(
                str(self.external), confirm=True)["ok"])
            external = self.external / "yt-a" / "source.wav"
            # The local wav changed under the record (an interrupted restore
            # left a truncated file, or the file was edited in place).
            edited = payload + b"-edited"
            (out / "source.wav").write_bytes(edited)
            # A re-listen rewrites meta.json wholesale: hashes.source_sha256
            # now describes the CHANGED local file while the record still
            # points at the good external copy. That must not license
            # overwriting the external copy with the changed bytes.
            relisten = audio_ref.analyze_audio_ref(
                from_dir=str(out), modes=["features"])
            self.assertTrue(relisten["ok"], relisten)
            meta = json.loads((out / "meta.json").read_text())
            self.assertEqual(meta["hashes"]["source_sha256"], _sha(edited))
            self.assertIn("source_offloaded", meta)

            result = audio_ref.offload_sources(str(self.external), confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual([row["ref_id"] for row in result["failed"]], ["yt-a"])
        self.assertIn("differs from the offload record",
                      result["failed"][0]["reason"])
        # The good external copy and the changed local file both stand.
        self.assertEqual(external.read_bytes(), payload)
        self.assertEqual((out / "source.wav").read_bytes(), edited)

    def test_refuses_unmounted_or_root_volume_dest(self):
        _make_ref(self.out_root, "yt-a")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        # Patch the mount walk to "/" — how the host's TMPDIR is mounted must
        # not decide the outcome of the root-volume refusal.
        with self._patch_store(mount=Path("/")):
            with self.assertRaisesRegex(ValueError, "root volume"):
                audio_ref.offload_sources(str(elsewhere / "dest"))
            # Unmounted volume: the destination's parent does not exist.
            with self.assertRaisesRegex(ValueError, "not mounted"):
                audio_ref.offload_sources(str(self.root / "no-volume" / "dest"))
        stderr = io.StringIO()
        with self._patch_store(mount=Path("/")), contextlib.redirect_stderr(stderr):
            rc = audio_ref.main(["--offload-sources", str(elsewhere / "dest")])
        self.assertEqual(rc, 1)
        self.assertIn("root volume", stderr.getvalue())
        self.assertTrue((self.out_root / "yt-a" / "source.wav").is_file())
        self.assertFalse((elsewhere / "dest").exists())

    def test_refuses_dest_on_same_device_as_store(self):
        _make_ref(self.out_root, "yt-a")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        # /System/Volumes/Data style: the mount walk says external volume, but
        # st_dev says it is still the store's own internal disk. The REAL
        # device ids must be consulted here (devices=False).
        with self._patch_store(devices=False):
            with self.assertRaisesRegex(ValueError, "own device"):
                audio_ref.offload_sources(str(elsewhere / "dest"), confirm=True)
        self.assertTrue((self.out_root / "yt-a" / "source.wav").is_file())
        self.assertFalse((elsewhere / "dest").exists())

    def test_mount_root_destination_is_accepted(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        # The destination IS the volume's mount root: its PARENT lives on the
        # internal disk, so probing the parent's device falsely refused it.
        with self._patch_store():
            self._reindex()
            result = audio_ref.offload_sources(str(self.external_parent),
                                               confirm=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            (self.external_parent / "yt-a" / "source.wav").read_bytes(), payload)
        self.assertFalse((self.out_root / "yt-a" / "source.wav").exists())

    def test_device_check_uses_the_offload_root_argument(self):
        # A refs root on the SAME (fake) device as the destination must be
        # refused even though the global OUT_ROOT sits on another device.
        same_device_root = self.external_parent / "refs-root"
        same_device_root.mkdir()
        with self._patch_store():
            with self.assertRaisesRegex(ValueError, "own device"):
                audio_ref._require_external_dest(self.external,
                                                root=same_device_root)

    def test_second_confirm_run_is_idempotent(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        with self._patch_store():
            self.assertEqual(self._reindex()["count"], 1)
            first = audio_ref.offload_sources(str(self.external), confirm=True)
            self.assertTrue(first["ok"])
            record_at = json.loads(
                (self.out_root / "yt-a" / "meta.json").read_text()
            )["source_offloaded"]["at"]
            second = audio_ref.offload_sources(str(self.external), confirm=True)
        self.assertTrue(second["ok"])
        self.assertEqual(second["offloaded"], [])
        self.assertEqual([row["reason"] for row in second["skipped"]],
                         ["already offloaded"])
        self.assertEqual(second["bytes_freed"], 0)
        self.assertIsNone(second["reindex"])
        # The record was not rewritten.
        self.assertEqual(
            json.loads((self.out_root / "yt-a" / "meta.json").read_text())
            ["source_offloaded"]["at"], record_at)


class TestConfirmedOffloadFailsClosed(OffloadTestCase):
    def test_corrupt_index_aborts_confirmed_offload_before_any_copy(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        self.index_path.write_text("{corrupt")
        stderr = io.StringIO()
        with self._patch_store(), contextlib.redirect_stderr(stderr):
            rc = audio_ref.main(["--offload-sources", str(self.external),
                                 "--confirm"])
        self.assertEqual(rc, 1)
        self.assertIn("corrupt audio_ref index", stderr.getvalue())
        self.assertTrue((self.out_root / "yt-a" / "source.wav").is_file())
        self.assertFalse(self.external.exists())

    def test_unusable_meta_json_is_skipped_not_fatal(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        non_dict = self.out_root / "yt-list"
        non_dict.mkdir()
        (non_dict / "source.wav").write_bytes(payload)
        (non_dict / "meta.json").write_text(json.dumps([1, 2, 3]))
        bad_json = self.out_root / "yt-bad"
        bad_json.mkdir()
        (bad_json / "source.wav").write_bytes(payload)
        (bad_json / "meta.json").write_text("{not json")
        with self._patch_store():
            self._reindex()
            result = audio_ref.offload_sources(str(self.external), confirm=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual([row["ref_id"] for row in result["offloaded"]], ["yt-a"])
        self.assertEqual([row["ref_id"] for row in result["skipped"]],
                         ["yt-bad", "yt-list"])
        for row in result["skipped"]:
            self.assertIn("meta.json", row["reason"])
        self.assertTrue((non_dict / "source.wav").is_file())

    def test_reindex_failure_is_reported_not_raised(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        with self._patch_store():
            self._reindex()

            def boom(*_args, **_kwargs):
                raise OSError("disk full during reindex")

            with mock.patch.object(audio_ref, "reindex_audio_refs",
                                   side_effect=boom):
                result = audio_ref.offload_sources(str(self.external),
                                                   confirm=True)
        self.assertFalse(result["ok"])
        # The offload itself happened; its lists still come back.
        self.assertEqual([row["ref_id"] for row in result["offloaded"]], ["yt-a"])
        self.assertIn("OSError", result["reindex"]["error"])
        self.assertFalse((self.out_root / "yt-a" / "source.wav").exists())

    def test_reindex_dropping_refs_rolls_back_index(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        with self._patch_store():
            self._reindex()
            # A ref the reindex cannot rebuild (its dir vanished) must not be
            # silently dropped: roll the index back to the pre-offload bytes.
            index = json.loads(self.index_path.read_text())
            index["refs"]["ghost"] = {"ref_id": "ghost",
                                      "out_dir": str(self.out_root / "ghost")}
            self.index_path.write_text(json.dumps(index))
            before = self.index_path.read_bytes()
            result = audio_ref.offload_sources(str(self.external), confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reindex"]["missing_refs"], ["ghost"])
        self.assertFalse(result["reindex"]["equal"])
        self.assertEqual(self.index_path.read_bytes(), before)

    def test_confirmed_run_backs_up_overlays_first(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        with self._patch_store():
            self._reindex()
            stderr = io.StringIO()
            # Result JSON goes to the captured stream, never the runner's
            # stdout — and each run must actually report success there.
            stdout = io.StringIO()
            with contextlib.redirect_stderr(stderr), \
                    contextlib.redirect_stdout(stdout):
                rc = audio_ref.main(["--offload-sources", str(self.external),
                                     "--confirm"])
                self.assertEqual(rc, 0)
                offload_report = json.loads(stdout.getvalue())
                # restore --confirm rewrites meta.json too: same safety net.
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    rc = audio_ref.main(["--restore-sources", "--confirm"])
                    self.assertEqual(rc, 0)
                restore_report = json.loads(stdout.getvalue())
        self.assertTrue(offload_report["ok"], offload_report)
        self.assertTrue(restore_report["ok"], restore_report)
        # Both confirmed runs announced an archive before touching anything
        # (same-second stamps may share one file).
        self.assertEqual(stderr.getvalue().count("overlay_backup"), 2)
        archives = list((self.root / "store" / "backups")
                        .glob("audio_ref_overlays-*.tar.gz"))
        self.assertGreaterEqual(len(archives), 1)


class TestOverlayBackupEdges(OffloadTestCase):
    """One odd meta.json must not crash every confirmed offload/restore."""

    def test_backup_overlays_skips_odd_metas_and_bad_verdicts(self):
        _make_ref(self.out_root, "yt-good", meta=_offload_meta(
            {"verdict": "keep", "ricardo_note": "keeper"}))
        odd = self.out_root / "yt-odd"
        odd.mkdir()
        (odd / "meta.json").write_text(json.dumps([1, 2, 3]))
        bad = self.out_root / "yt-bad"
        bad.mkdir()
        (bad / "meta.json").write_text(json.dumps(_offload_meta(
            {"verdict": "banana"})))
        with self._patch_store():
            report = audio_ref.backup_overlays()
        self.assertTrue(report["ok"])
        # Only the readable verdict counts; the odd metas are still archived
        # (counting skips them, the tar does not).
        self.assertEqual(report["verdicts_captured"], 1)
        self.assertEqual(report["notes_captured"], 1)
        self.assertGreaterEqual(report["file_count"], 3)

    def test_backup_failure_refuses_confirmed_offload(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        stderr = io.StringIO()
        with self._patch_store(), contextlib.redirect_stderr(stderr), \
                mock.patch.object(audio_ref, "backup_overlays",
                                  side_effect=OSError("disk full")):
            rc = audio_ref.main(["--offload-sources", str(self.external),
                                 "--confirm"])
        self.assertEqual(rc, 1)
        self.assertIn("overlay_backup_failed", stderr.getvalue())
        self.assertTrue((self.out_root / "yt-a" / "source.wav").is_file())
        self.assertFalse(self.external.exists())

    def test_backup_failure_refuses_confirmed_restore(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        with self._patch_store():
            self.assertTrue(audio_ref.offload_sources(
                str(self.external), confirm=True)["ok"])
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), \
                    mock.patch.object(audio_ref, "backup_overlays",
                                      side_effect=RuntimeError("tar exploded")):
                rc = audio_ref.main(["--restore-sources", "--confirm"])
        self.assertEqual(rc, 1)
        self.assertIn("overlay_backup_failed", stderr.getvalue())
        # Nothing moved: the wav stays offloaded, the record stays put.
        self.assertFalse((self.out_root / "yt-a" / "source.wav").exists())
        meta = json.loads((self.out_root / "yt-a" / "meta.json").read_text())
        self.assertIn("source_offloaded", meta)


class TestDurableCopies(OffloadTestCase):
    """Copies must reach stable storage before the only other copy goes away."""

    def _spies(self, events: list[str]) -> tuple:
        real_fsync_file = audio_ref._fsync_file
        real_fsync_dir = audio_ref._fsync_dir
        real_replace = os.replace
        real_unlink = os.unlink

        def spy_fsync_file(path):
            events.append(f"fsync_file:{Path(path).name}")
            return real_fsync_file(path)

        def spy_fsync_dir(path):
            # Parent name too: the destination dir and the ref dir can share
            # a name (both are "<ref_id>" under different roots).
            p = Path(path)
            events.append(f"fsync_dir:{p.parent.name}/{p.name}")
            return real_fsync_dir(p)

        def spy_replace(src, dst):
            events.append(f"replace:{Path(src).name}>{Path(dst).name}")
            return real_replace(src, dst)

        def spy_unlink(path, *args, **kwargs):
            events.append(f"unlink:{Path(path).name}")
            return real_unlink(path, *args, **kwargs)

        return (
            mock.patch.object(audio_ref, "_fsync_file", side_effect=spy_fsync_file),
            mock.patch.object(audio_ref, "_fsync_dir", side_effect=spy_fsync_dir),
            mock.patch.object(audio_ref.os, "replace", side_effect=spy_replace),
            mock.patch.object(audio_ref.os, "unlink", side_effect=spy_unlink),
        )

    def test_offload_makes_published_name_durable_before_unlink(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        events: list[str] = []
        patches = self._spies(events)
        with self._patch_store(), contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            self._reindex()
            result = audio_ref.offload_sources(str(self.external), confirm=True)
        self.assertTrue(result["ok"], result)
        publish = events.index("replace:.source.wav.copying>source.wav")
        # Temp content durable and verified BEFORE the publish rename.
        self.assertLess(events.index("fsync_file:.source.wav.copying"), publish)
        # The destination DIRECTORY is fsynced only AFTER the rename: the
        # published name is what must survive, not the temp entry.
        self.assertGreater(events.index("fsync_dir:audioref-sources/yt-a"),
                           publish)
        # Meta record published atomically, its directory fsynced, and only
        # THEN does the only local copy go away. (The reindex inside the
        # spied block also writes meta.json — take the one after publish.)
        meta_publish = next(i for i, e in enumerate(events)
                            if i > publish
                            and e.startswith("replace:.meta.json.")
                            and e.endswith(">meta.json"))
        self.assertGreater(meta_publish, publish)
        self.assertLess(meta_publish, events.index("fsync_dir:refs/yt-a"))
        self.assertLess(events.index("fsync_dir:refs/yt-a"),
                        events.index("unlink:source.wav"))

    def test_restore_fsyncs_temp_before_publish(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        with self._patch_store():
            self._reindex()
            self.assertTrue(audio_ref.offload_sources(
                str(self.external), confirm=True)["ok"])
        events: list[str] = []
        patches = self._spies(events)
        with self._patch_store(), contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            result = audio_ref.restore_sources(confirm=True)
        self.assertTrue(result["ok"], result)
        self.assertLess(
            events.index("fsync_file:.source.wav.restoring"),
            events.index("replace:.source.wav.restoring>source.wav"))


class TestRestoreSources(OffloadTestCase):
    def test_round_trip_restores_wav_and_clears_record(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        with self._patch_store():
            self.assertEqual(self._reindex()["count"], 1)
            self.assertTrue(audio_ref.offload_sources(
                str(self.external), confirm=True)["ok"])
            external_copy = self.external / "yt-a" / "source.wav"

            dry = audio_ref.restore_sources(confirm=False)
            self.assertTrue(dry["dry_run"])
            self.assertEqual([row["ref_id"] for row in dry["plan"]], ["yt-a"])
            self.assertFalse((self.out_root / "yt-a" / "source.wav").exists())

            result = audio_ref.restore_sources(confirm=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual([row["ref_id"] for row in result["restored"]], ["yt-a"])
        wav = self.out_root / "yt-a" / "source.wav"
        self.assertEqual(wav.read_bytes(), payload)
        meta = json.loads((self.out_root / "yt-a" / "meta.json").read_text())
        self.assertNotIn("source_offloaded", meta)
        # The external copy stays: re-offload is the reverse operation.
        self.assertTrue(external_copy.is_file())

        with self._patch_store():
            again = audio_ref.restore_sources(confirm=True)
        self.assertTrue(again["ok"])
        self.assertEqual(again["restored"], [])

    def test_enospc_during_restore_leaves_no_partial_wav(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        with self._patch_store():
            self.assertEqual(self._reindex()["count"], 1)
            self.assertTrue(audio_ref.offload_sources(
                str(self.external), confirm=True)["ok"])
            meta_before = (self.out_root / "yt-a" / "meta.json").read_text()

            def enospc_copy2(src, dst, **kwargs):
                Path(dst).write_bytes(Path(src).read_bytes()[:5])  # mid-write
                raise OSError(28, "No space left on device")

            with mock.patch.object(audio_ref.shutil, "copy2",
                                   side_effect=enospc_copy2):
                result = audio_ref.restore_sources(confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual([row["ref_id"] for row in result["failed"]], ["yt-a"])
        self.assertIn("No space left on device", result["failed"][0]["reason"])
        ref = self.out_root / "yt-a"
        # A partial source.wav must never appear under its own name: every
        # later path (re-offload, re-listen, clip_moments) trusts that file.
        self.assertFalse((ref / "source.wav").exists())
        self.assertEqual(list(ref.glob(".source.wav*")), [])
        self.assertEqual((ref / "meta.json").read_text(), meta_before)

    def test_skips_when_external_copy_unreachable(self):
        out = _make_ref(self.out_root, "yt-a")
        (out / "source.wav").unlink()
        record = {"path": str(self.external / "yt-a" / "source.wav"),
                  "sha256": _sha(b"RIFFfake-audio"), "bytes": 15,
                  "at": "2026-09-24T00:00:00Z"}
        (out / "meta.json").write_text(json.dumps(_offload_meta(
            {"source_offloaded": record})))
        meta_before = (out / "meta.json").read_text()
        with self._patch_store():
            result = audio_ref.restore_sources(confirm=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["restored"], [])
        self.assertIn("not reachable", result["skipped"][0]["reason"])
        self.assertFalse((out / "source.wav").exists())
        self.assertEqual((out / "meta.json").read_text(), meta_before)

    def test_restore_moves_mismatched_local_aside_and_restores(self):
        payload = b"RIFFfake-audio"
        out = _make_ref(self.out_root, "yt-a", payload=payload)
        with self._patch_store():
            self._reindex()
            self.assertTrue(audio_ref.offload_sources(
                str(self.external), confirm=True)["ok"])
            # Interrupted restore: a truncated local wav sits under the
            # record. This was a dead end — offload refuses it, restore used
            # to skip it. Restore must move it aside and bring the recorded
            # bytes back.
            (out / "source.wav").write_bytes(payload[:5])
            dry = audio_ref.restore_sources(confirm=False)
            self.assertEqual([row["ref_id"] for row in dry["plan"]], ["yt-a"])
            self.assertTrue(str(dry["plan"][0].get("moved_aside")).startswith(
                "source.wav.mismatch-"))
            result = audio_ref.restore_sources(confirm=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual([row["ref_id"] for row in result["restored"]], ["yt-a"])
        self.assertTrue(
            str(result["restored"][0].get("moved_aside")).startswith(
                "source.wav.mismatch-"))
        # Good wav back in place; the mismatched bytes preserved, not deleted.
        self.assertEqual((out / "source.wav").read_bytes(), payload)
        aside = list(out.glob("source.wav.mismatch-*"))
        self.assertEqual(len(aside), 1)
        self.assertEqual(aside[0].read_bytes(), payload[:5])
        meta = json.loads((out / "meta.json").read_text())
        self.assertNotIn("source_offloaded", meta)

    def test_restore_skips_local_wav_matching_the_record(self):
        payload = b"RIFFfake-audio"
        out = _make_ref(self.out_root, "yt-a", payload=payload)
        with self._patch_store():
            self._reindex()
            self.assertTrue(audio_ref.offload_sources(
                str(self.external), confirm=True)["ok"])
            # Crash between record and unlink: the local wav is back and
            # still hashes to the record. Nothing to move aside — the ref
            # already converged.
            (out / "source.wav").write_bytes(payload)
            result = audio_ref.restore_sources(confirm=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["restored"], [])
        self.assertEqual([row["reason"] for row in result["skipped"]],
                         ["local source.wav already present"])
        self.assertEqual(list(out.glob("source.wav.mismatch-*")), [])
        self.assertIn("source_offloaded",
                      json.loads((out / "meta.json").read_text()))


class TestOffloadedRefsFailWithRecoveryHint(OffloadTestCase):
    def _offloaded_parent(self) -> Path:
        out = self.out_root / "parent"
        out.mkdir()
        (out / "describe.txt").write_text(
            "# Identity\nparent\n\n# Moments\n- 00:05 – motif\n")
        (out / "meta.json").write_text(json.dumps(_offload_meta(
            {"source_offloaded": {
                "path": str(self.external / "parent" / "source.wav"),
                "sha256": _sha(b"RIFFfake"), "bytes": 9,
                "at": "2026-09-24T00:00:00Z"}})))
        self.index_path.write_text(json.dumps({"refs": {"parent": {
            "ref_id": "parent", "out_dir": str(out), "duration_s": 30,
        }}}))
        return out

    def test_clip_moments_mentions_restore_command(self):
        self._offloaded_parent()
        with self._patch_store():
            with self.assertRaises(FileNotFoundError) as ctx:
                audio_ref.clip_moments("parent", modes=[],
                                       index_path=self.index_path)
        message = str(ctx.exception)
        self.assertIn("--restore-sources", message)
        self.assertIn(str(self.external / "parent" / "source.wav"), message)

    def test_resolve_from_dir_mentions_restore_command(self):
        out = self._offloaded_parent()
        with self._patch_store():
            with self.assertRaises(FileNotFoundError) as ctx:
                audio_ref.resolve_from_dir(str(out))
        self.assertIn("--restore-sources", str(ctx.exception))

    def test_get_ref_card_fallback_resolves_offloaded_ref(self):
        self._offloaded_parent()
        empty = self.out_root / "empty"
        empty.mkdir()
        # Drop the ref from the index so resolution must use the disk fallback.
        self.index_path.write_text(json.dumps({"refs": {}}))
        with self._patch_store():
            card = audio_ref.get_ref_card("parent", index_path=self.index_path)
            with self.assertRaises(KeyError):
                audio_ref.get_ref_card("empty", index_path=self.index_path)
        self.assertEqual(card["ref_id"], "parent")


class TestBatchPartialRetryOfOffloadedRef(OffloadTestCase):
    def test_batch_stops_after_two_consecutive_listen_rejections(self):
        manifest = {"batch_id": "reject-streak", "items": [
            {"source": "https://youtu.be/reject-1"},
            {"source": "https://youtu.be/reject-2"},
            {"source": "https://youtu.be/reject-3"},
        ]}
        calls = []

        def reject(**kwargs):
            calls.append(kwargs["source"])
            return {"ok": False, "partial": False,
                    "describe_result": {"listen_rejected": {"reason": "no_listen"}},
                    "listen_rejected": {"reason": "no_listen"}}

        batch_root = self.root / "reject-streak-batches"
        batch_root.mkdir()
        with self._patch_store(), \
                mock.patch.object(audio_ref, "BATCH_ROOT", batch_root), \
                mock.patch.object(audio_ref, "analyze_audio_ref", side_effect=reject):
            report = audio_ref.run_batch(manifest, resume=False)
        self.assertEqual(len(calls), 2)
        self.assertEqual(report["stopped_reason"], "listen_rejected_streak")

    def test_rejected_row_retries_in_place_only_with_explicit_override(self):
        source = "https://youtu.be/rejected-listen"
        part_dir = self.out_root / "rejected-partial"
        part_dir.mkdir()
        (part_dir / "source.wav").write_bytes(b"audio")
        manifest = {"batch_id": "retry-rejected", "items": [{"source": source}]}
        key = audio_ref.batch_item_key({"source": source}, {"timeout_s": 180})
        batch_root = self.root / "rejected-batches"
        batch_root.mkdir()
        previous_report = {
            "manifest_sha256": audio_ref.batch_manifest_hash(manifest, 180),
            "results": [{"ok": False, "partial": False,
                         "failure_kind": "listen_rejected",
                         "listen_rejected": {"reason": "no_listen"},
                         "resume_key": key, "source": source,
                         "out_dir": str(part_dir)}],
        }
        report_path = batch_root / "retry-rejected.json"
        report_path.write_text(json.dumps(previous_report))
        calls = []

        def fake_analyze(**kwargs):
            calls.append(kwargs.get("from_dir"))
            return {"ok": True, "partial": False, "ref_id": "r",
                    "out_dir": str(part_dir), "focus": "full"}

        with self._patch_store(), \
                mock.patch.object(audio_ref, "BATCH_ROOT", batch_root), \
                mock.patch.object(audio_ref, "analyze_audio_ref",
                                  side_effect=fake_analyze):
            skipped = audio_ref.run_batch(manifest, resume=True, timeout_s=180)
            self.assertTrue(skipped["results"][0]["skipped_resume"])
            self.assertEqual(skipped["results"][0]["failure_kind"],
                             "listen_rejected")
            report_path.write_text(json.dumps(previous_report))
            audio_ref.run_batch(manifest, resume=True, timeout_s=180,
                                retry_rejected=True)
        self.assertEqual(calls, [str(part_dir)])

    def test_old_timeout_key_resumes_and_timeout_not_in_new_key(self):
        source = "https://youtu.be/old-timeout-key"
        manifest = {"batch_id": "old-timeout", "items": [{"source": source}]}
        old_key = audio_ref.batch_item_key(
            {"source": source}, {"timeout_s": 600}) + "|timeout_s=600|"
        batch_root = self.root / "old-key-batches"
        batch_root.mkdir()
        (batch_root / "old-timeout.json").write_text(json.dumps({
            "manifest_sha256": audio_ref.batch_manifest_hash(manifest, 600),
            "results": [{"ok": True, "resume_key": old_key,
                         "source": source, "ref_id": "cached"}],
        }))
        with self._patch_store(), \
                mock.patch.object(audio_ref, "BATCH_ROOT", batch_root), \
                mock.patch.object(audio_ref, "analyze_audio_ref",
                                  side_effect=AssertionError("must resume")):
            report = audio_ref.run_batch(manifest, resume=True, timeout_s=180)
        self.assertTrue(report["results"][0]["skipped_resume"])
        self.assertEqual(
            audio_ref.batch_item_key({"source": source}, {"timeout_s": 180}),
            audio_ref.batch_item_key({"source": source}, {"timeout_s": 600}))

    def test_offloaded_partial_dir_is_retried_in_place_not_refetched(self):
        part_dir = self.out_root / "yt-partial"
        part_dir.mkdir()
        (part_dir / "describe.txt").write_text("# Identity\npartial\n")
        (part_dir / "meta.json").write_text(json.dumps(_offload_meta(
            {"source_offloaded": {
                "path": str(self.external / "yt-partial" / "source.wav"),
                "sha256": _sha(b"RIFFfake"), "bytes": 9,
                "at": "2026-09-24T00:00:00Z"}})))
        source_url = "https://youtu.be/offloaded"
        manifest = {"batch_id": "offload-retry",
                    "items": [{"source": source_url}]}
        resume_key = audio_ref.batch_item_key(
            {"source": source_url}, {"timeout_s": 600})
        batch_root = self.root / "batches"
        batch_root.mkdir()
        (batch_root / "offload-retry.json").write_text(json.dumps({
            "manifest_sha256": audio_ref.batch_manifest_hash(manifest, 600),
            "results": [{"ok": False, "partial": True, "resume_key": resume_key,
                         "source": source_url, "out_dir": str(part_dir)}],
        }))
        calls: list[str | None] = []

        def fake_analyze(**kwargs):
            from_dir = kwargs.get("from_dir")
            calls.append(from_dir)
            if from_dir is None:
                raise AssertionError(
                    "partial row re-fetched instead of retried in place")
            # The restore hint must come from the real resolution path.
            audio_ref.resolve_from_dir(from_dir)
            raise AssertionError("resolve_from_dir unexpectedly succeeded")

        reports = []
        with self._patch_store(), \
                mock.patch.object(audio_ref, "BATCH_ROOT", batch_root), \
                mock.patch.object(audio_ref, "analyze_audio_ref",
                                  side_effect=fake_analyze):
            # Every --resume must keep retrying in place, not just the first.
            reports.append(audio_ref.run_batch(manifest, resume=True, timeout_s=600))
            reports.append(audio_ref.run_batch(manifest, resume=True, timeout_s=600))
        self.assertEqual(calls, [str(part_dir), str(part_dir)])
        for report in reports:
            row = report["results"][0]
            self.assertFalse(row["ok"])
            self.assertIn("--restore-sources", row["error"])
            # The row keeps its out_dir under a retry_in_place marker so the
            # NEXT resume also retries in place instead of downloading a
            # duplicate ref — WITHOUT reading as usable output: the batch
            # partial flag keeps meaning "produced usable output this run".
            self.assertTrue(row["retry_in_place"])
            self.assertFalse(row.get("partial"))
            self.assertEqual(row["out_dir"], str(part_dir))
            self.assertFalse(report["partial"])
        self.assertTrue((part_dir / "describe.txt").is_file())
        self.assertTrue((part_dir / "meta.json").is_file())


class TestRelistenKeepsOffloadRecord(OffloadTestCase):
    def test_relisten_carries_source_offloaded_record(self):
        payload = b"RIFFfake-audio"
        record = {"path": str(self.external / "yt-a" / "source.wav"),
                  "sha256": _sha(payload), "bytes": len(payload),
                  "at": "2026-09-24T00:00:00Z"}
        out = _make_ref(self.out_root, "yt-a", payload=payload,
                        meta=_offload_meta({"source_offloaded": record}))
        with self._patch_store():
            result = audio_ref.analyze_audio_ref(
                from_dir=str(out), modes=["features"])
        self.assertTrue(result["ok"], result)
        # meta.json is rewritten wholesale by a re-listen; the record must be
        # carried forward or the external copy is orphaned.
        meta = json.loads((out / "meta.json").read_text())
        self.assertEqual(meta.get("source_offloaded"), record)


class TestRestoreEdges(OffloadTestCase):
    def test_bad_record_bytes_falls_back_to_external_stat_size(self):
        payload = b"RIFFfake-audio"
        # "lots" is not a number; NaN/Infinity are numbers that cannot be
        # int()'d. None of them may skip the ref or crash the run — the
        # external file's real size is the accounting truth.
        bad_bytes = {"yt-str": "lots", "yt-nan": float("nan"),
                     "yt-inf": float("inf")}
        for ref_id, bad in bad_bytes.items():
            out = _make_ref(self.out_root, ref_id)
            (out / "source.wav").unlink()
            (out / "meta.json").write_text(json.dumps(_offload_meta(
                {"source_offloaded": {
                    "path": str(self.external / ref_id / "source.wav"),
                    "sha256": _sha(payload), "bytes": bad,
                    "at": "2026-09-24T00:00:00Z"}})))
            (self.external / ref_id).mkdir(parents=True)
            (self.external / ref_id / "source.wav").write_bytes(payload)
        with self._patch_store():
            dry = audio_ref.restore_sources(confirm=False)
            confirmed = audio_ref.restore_sources(confirm=True)
        for report in (dry, confirmed):
            self.assertEqual(report["skipped"], [])
            rows = report["plan"] if report["dry_run"] else report["restored"]
            self.assertEqual(sorted(row["ref_id"] for row in rows),
                             ["yt-inf", "yt-nan", "yt-str"])
            for row in rows:
                self.assertEqual(row["bytes"], len(payload))
        self.assertEqual(confirmed["bytes_restored"], 3 * len(payload))
        for ref_id in bad_bytes:
            self.assertEqual(
                (self.out_root / ref_id / "source.wav").read_bytes(), payload)
            self.assertNotIn("source_offloaded", json.loads(
                (self.out_root / ref_id / "meta.json").read_text()))

    def test_restore_with_tampered_external_copy_fails_closed(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        _make_ref(self.out_root, "yt-b", payload=payload + b"-b")
        with self._patch_store():
            self._reindex()
            self.assertTrue(audio_ref.offload_sources(
                str(self.external), confirm=True)["ok"])
            (self.external / "yt-b" / "source.wav").write_bytes(
                payload + b"-B-TAMPERED")
            result = audio_ref.restore_sources(confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["restored_count"], 1)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual([row["ref_id"] for row in result["failed"]], ["yt-b"])
        self.assertEqual((self.out_root / "yt-a" / "source.wav").read_bytes(),
                         payload)
        self.assertFalse((self.out_root / "yt-b" / "source.wav").exists())
        # Record kept for the failed ref: the external copy stays known.
        meta = json.loads((self.out_root / "yt-b" / "meta.json").read_text())
        self.assertIn("source_offloaded", meta)


class TestCliEdges(OffloadTestCase):
    def test_confirm_without_offload_or_restore_is_rejected(self):
        stderr = io.StringIO()
        with self._patch_store(), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                audio_ref.main(["--confirm"])
        self.assertIn("requires --offload-sources", stderr.getvalue())

    def test_empty_offload_dest_is_an_error_not_a_usage_crash(self):
        stderr = io.StringIO()
        with self._patch_store(), contextlib.redirect_stderr(stderr):
            rc = audio_ref.main(["--offload-sources", "", "--confirm"])
        self.assertEqual(rc, 1)
        self.assertIn("absolute", stderr.getvalue())

    def test_invalid_dest_refused_before_overlay_backup(self):
        _make_ref(self.out_root, "yt-a")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        stderr = io.StringIO()
        backup = mock.Mock()
        # Same-device destination (REAL device ids): the run cannot proceed,
        # so the overlay backup must not even be taken.
        with self._patch_store(devices=False), \
                contextlib.redirect_stderr(stderr), \
                mock.patch.object(audio_ref, "backup_overlays", backup):
            rc = audio_ref.main(["--offload-sources", str(elsewhere / "dest"),
                                 "--confirm"])
        self.assertEqual(rc, 1)
        self.assertIn("own device", stderr.getvalue())
        backup.assert_not_called()
        self.assertTrue((self.out_root / "yt-a" / "source.wav").is_file())

    def test_offload_reports_counts(self):
        payload = b"RIFFfake-audio"
        _make_ref(self.out_root, "yt-a", payload=payload)
        no_meta = self.out_root / "yt-b"
        no_meta.mkdir()
        (no_meta / "source.wav").write_bytes(b"RIFForphan")
        with self._patch_store():
            self._reindex()
            dry = audio_ref.offload_sources(str(self.external), confirm=False)
            result = audio_ref.offload_sources(str(self.external), confirm=True)
        self.assertEqual(result["offloaded_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["failed_count"], 0)
        self.assertEqual(dry["skipped_count"], 1)


class TestWriteProtectedSources(OffloadTestCase):
    """A 0444 source.wav (and the 0444 temp copy2 leaves) must still move.

    ``shutil.copy2`` preserves the mode, so the temp file inherits the
    write protection — fsync must never need write access to the file.
    """

    def test_fsync_file_accepts_write_protected_file(self):
        locked = self.root / "locked.wav"
        locked.write_bytes(b"RIFF")
        locked.chmod(0o444)
        try:
            audio_ref._fsync_file(locked)  # must not raise PermissionError
        finally:
            locked.chmod(0o644)

    def test_write_protected_source_offloads_and_restores(self):
        payload = b"RIFFfake-audio"
        out = _make_ref(self.out_root, "yt-a", payload=payload)
        (out / "source.wav").chmod(0o444)
        with self._patch_store():
            self._reindex()
            offloaded = audio_ref.offload_sources(str(self.external), confirm=True)
            self.assertTrue(offloaded["ok"], offloaded)
            self.assertEqual((self.external / "yt-a" / "source.wav").read_bytes(),
                             payload)
            restored = audio_ref.restore_sources(confirm=True)
            self.assertTrue(restored["ok"], restored)
        self.assertEqual((out / "source.wav").read_bytes(), payload)


class TestStagedAudioOnly(OffloadTestCase):
    def test_offloaded_wav_fails_with_restore_hint(self):
        out = self.out_root / "yt-a"
        out.mkdir()
        (out / "meta.json").write_text(json.dumps(_offload_meta(
            {"source_offloaded": {
                "path": str(self.external / "yt-a" / "source.wav"),
                "sha256": _sha(b"RIFFfake-audio"), "bytes": 15,
                "at": "2026-09-24T00:00:00Z"}})))
        with self._patch_store():
            with self.assertRaisesRegex(FileNotFoundError, "--restore-sources"):
                with audio_ref.staged_audio_only(str(out / "source.wav")):
                    pass


if __name__ == "__main__":
    unittest.main()
