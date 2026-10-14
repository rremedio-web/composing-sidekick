"""Tests for Producer Pal managed-context synchronizer."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

from tests._requires import requires_store
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.sync_pp_context import (
    DEFAULT_RHYTHMS,
    DEFAULT_STYLE,
    MANAGED_END,
    MANAGED_START,
    MAX_DOCUMENT_CHARS,
    MANDATORY_HEADINGS,
    SyncContextError,
    assemble_digest,
    build_digest,
    build_digest_sections,
    fit_document,
    format_managed_block,
    merge_managed_block,
    preview_merge,
    sync_context,
    validate_managed_markers,
    write_context,
)


def _fixture_files(tmp: Path) -> tuple[Path, Path]:
    style = tmp / "STYLE.md"
    style.write_text(
        "\n".join(
            [
                "# Style",
                "## In plain words",
                "Example plain-words line.",
                "## The feel",
                "Example feel line.",
                "## Do",
                "Example do line.",
                "## Don't",
                "Example don't line.",
            ]
        ),
        encoding="utf-8",
    )
    rhythms = tmp / "APPROVED_RHYTHMS.md"
    rhythms.write_text(
        "\n".join(
            [
                "# Approved rhythms",
                "Core Philosophy: example rhythm philosophy.",
                "## 1. Track 10",
                "- Scene 1: example pattern.",
            ]
        ),
        encoding="utf-8",
    )
    return style, rhythms


class TestDigest(unittest.TestCase):
    def test_build_digest_has_no_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            style, rhythms = _fixture_files(Path(tmp))
            digest = build_digest(style, rhythms, generated_on="2026-08-23")
            self.assertIn("Musical identity", digest)
            self.assertIn("KEEP / DROP", digest)
            self.assertNotIn("/Users/", digest)
            self.assertNotRegex(digest, r"(?i)file://")

    def test_build_digest_strips_file_links_from_rhythms(self):
        with tempfile.TemporaryDirectory() as tmp:
            style, rhythms = _fixture_files(Path(tmp))
            rhythms.write_text(
                rhythms.read_text(encoding="utf-8")
                + "\n[`t10.mid`](file:///Users/ricardo/secret/t10.mid)\n",
                encoding="utf-8",
            )
            digest = build_digest(style, rhythms, generated_on="2026-08-23")
            self.assertNotIn("/Users/ricardo", digest)
            self.assertNotIn("file://", digest.lower())

    def test_path_sanitization_covers_all_spellings(self):
        with tempfile.TemporaryDirectory() as tmp:
            style, rhythms = _fixture_files(Path(tmp))
            secret = "/srv/private/secret.wav"
            style.write_text(
                style.read_text(encoding="utf-8") + "\n" + secret + "\n",
                encoding="utf-8",
            )
            digest = build_digest(style, rhythms, generated_on="2026-08-23")
            self.assertNotIn(secret, digest)
            self.assertIn("[path redacted]", digest)

        samples = [
            r"\\server\share\take.wav",
            "//server/share/take.wav",
            "/mnt/audio/take.wav",
            "C:\\Users\\ricardo\\secret.wav",
            "file:///Users/ricardo/secret.wav",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                with tempfile.TemporaryDirectory() as tmp:
                    style, rhythms = _fixture_files(Path(tmp))
                    style.write_text(
                        "\n".join(
                            [
                                "# Style",
                                "## In plain words",
                                "Leak %s here." % sample,
                            ]
                        ),
                        encoding="utf-8",
                    )
                    digest = build_digest(style, rhythms, generated_on="2026-08-23")
                    self.assertNotIn("server", digest.lower())
                    self.assertNotIn("/srv/", digest)
                    self.assertNotIn("/mnt/", digest)
                    self.assertNotIn("/Users/ricardo", digest)
                    self.assertNotIn("file://", digest.lower())

    def test_managed_markers_in_source_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            style, rhythms = _fixture_files(Path(tmp))
            style.write_text(
                "\n".join(
                    [
                        "# Style",
                        "## In plain words",
                        MANAGED_START,
                        "injected",
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(SyncContextError):
                build_digest(style, rhythms, generated_on="2026-08-23")

    def test_real_corpus_retains_mandatory_headings(self):
        if not DEFAULT_STYLE.is_file() or not DEFAULT_RHYTHMS.is_file():
            self.skipTest("tracked corpus files missing")
        merged = preview_merge(b"", generated_on="2026-08-23")
        text = merged.decode("utf-8")
        self.assertLessEqual(len(merged), MAX_DOCUMENT_CHARS)
        for heading in MANDATORY_HEADINGS:
            self.assertIn(heading, text)


class TestMerge(unittest.TestCase):
    def test_preserves_unmanaged_prefix_and_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            style, rhythms = _fixture_files(Path(tmp))
            doc = "Producer Pal intro\n\nTail notes stay."
            merged = preview_merge(
                doc,
                style_path=style,
                rhythms_path=rhythms,
                generated_on="2026-08-23",
            )
            text = merged.decode("utf-8")
            self.assertTrue(text.startswith("Producer Pal intro"))
            self.assertIn("Tail notes stay.", text)
            self.assertIn(MANAGED_START, text)
            self.assertIn(MANAGED_END, text)

    def test_preserves_crlf_bytes_outside_managed_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            style, rhythms = _fixture_files(Path(tmp))
            original = b"line one\r\n\r\nline two\r\n"
            merged = preview_merge(
                original,
                style_path=style,
                rhythms_path=rhythms,
                generated_on="2026-08-23",
            )
            self.assertTrue(merged.startswith(b"line one\r\n\r\nline two\r\n"))
            self.assertIn(MANAGED_START.encode("utf-8"), merged)

    def test_preserves_mixed_newline_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            style, rhythms = _fixture_files(Path(tmp))
            original = b"unix\n\r\nwindows\r\n"
            merged = preview_merge(
                original,
                style_path=style,
                rhythms_path=rhythms,
                generated_on="2026-08-23",
            )
            prefix = merged.split(MANAGED_START.encode("utf-8"))[0]
            self.assertEqual(prefix, original)

    def test_replaces_managed_block_preserving_mixed_newline_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            style, rhythms = _fixture_files(Path(tmp))
            original = (
                b"prefix\r\n"
                + format_managed_block("old").encode("utf-8")
                + b"\r\nsuffix\n\r\n"
            )
            merged = preview_merge(
                original,
                style_path=style,
                rhythms_path=rhythms,
                generated_on="2026-08-23",
            )
            marker = MANAGED_START.encode("utf-8")
            end_marker = MANAGED_END.encode("utf-8")
            self.assertEqual(merged[: original.index(marker)], original[: original.index(marker)])
            self.assertEqual(merged[merged.index(end_marker) + len(end_marker) :], b"\r\nsuffix\n\r\n")

    def test_idempotent_second_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            style, rhythms = _fixture_files(Path(tmp))
            doc = "# Global voice\n\nUser prose.\n"
            first = preview_merge(
                doc, style_path=style, rhythms_path=rhythms, generated_on="2026-08-23",
            )
            second = preview_merge(
                first, style_path=style, rhythms_path=rhythms, generated_on="2026-08-23",
            )
            self.assertEqual(first.count(MANAGED_START.encode("utf-8")), 1)
            self.assertEqual(first, second)

    def test_first_insertion_appends_with_minimal_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            style, rhythms = _fixture_files(Path(tmp))
            doc = b"existing line"
            merged = preview_merge(
                doc, style_path=style, rhythms_path=rhythms, generated_on="2026-08-23",
            )
            self.assertEqual(merged.index(b"existing line"), 0)
            self.assertEqual(merged[len(b"existing line")], ord("\n"))
            self.assertTrue(merged.endswith(MANAGED_END.encode("utf-8")))

    def test_malformed_markers_fail_closed(self):
        for doc, label in (
            (MANAGED_START + "\n" + MANAGED_START + "\n" + MANAGED_END, "duplicate"),
            (MANAGED_END + "\n" + MANAGED_START, "reversed"),
            (MANAGED_START + "\nno end", "unmatched"),
        ):
            with self.subTest(label=label):
                with self.assertRaises(SyncContextError):
                    validate_managed_markers(doc)

    @requires_store("STYLE.md")
    def test_cap_trims_digest_not_unmanaged(self):
        sections = build_digest_sections(generated_on="2026-08-23")
        unmanaged = b"X" * 8000
        merged = fit_document(unmanaged, sections)
        self.assertLessEqual(len(merged), MAX_DOCUMENT_CHARS)
        self.assertTrue(merged.startswith(b"X" * 100))
        text = merged.decode("utf-8")
        self.assertIn(MANAGED_START, text)
        for heading in MANDATORY_HEADINGS:
            self.assertIn(heading, text)

    @requires_store("STYLE.md")
    def test_cap_fails_when_unmanaged_too_large(self):
        sections = build_digest_sections(generated_on="2026-08-23")
        unmanaged = b"U" * (MAX_DOCUMENT_CHARS - 10)
        with self.assertRaises(SyncContextError):
            fit_document(unmanaged, sections)

    @requires_store("STYLE.md")
    def test_cap_fails_when_mandatory_skeleton_cannot_fit(self):
        sections = build_digest_sections(generated_on="2026-08-23")
        unmanaged = b"Z" * (MAX_DOCUMENT_CHARS - 500)
        with self.assertRaises(SyncContextError):
            fit_document(unmanaged, sections)


class TestWrite(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            style, rhythms = _fixture_files(Path(tmp))
            dest = Path(tmp) / "context.md"
            dest.write_bytes(b"original\n")
            result = sync_context(
                dest,
                style_path=style,
                rhythms_path=rhythms,
                generated_on="2026-08-23",
                dry_run=True,
            )
            self.assertFalse(result["wrote"])
            self.assertEqual(dest.read_bytes(), b"original\n")

    def test_confirmed_write_backs_up_and_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            style, rhythms = _fixture_files(Path(tmp))
            dest = Path(tmp) / "context.md"
            backup = Path(tmp) / "context.md.bak"
            dest.write_bytes(b"user header\n")
            result = sync_context(
                dest,
                style_path=style,
                rhythms_path=rhythms,
                generated_on="2026-08-23",
                confirm_write=True,
                backup_path=backup,
            )
            self.assertTrue(result["wrote"])
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_bytes(), b"user header\n")
            written = dest.read_bytes()
            self.assertIn(MANAGED_START.encode("utf-8"), written)
            self.assertTrue(written.startswith(b"user header"))

    def test_rollback_on_verification_failure_restores_exact_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "context.md"
            backup = Path(tmp) / "context.md.bak"
            original = b"preserve me exactly\r\n"
            dest.write_bytes(original)
            merged = format_managed_block("digest").encode("utf-8")

            def bad_read(_path: Path) -> bytes:
                return b"tampered"

            with self.assertRaises(SyncContextError):
                write_context(
                    dest,
                    merged,
                    backup_path=backup,
                    read_back=bad_read,
                )
            self.assertEqual(dest.read_bytes(), original)
            self.assertTrue(backup.is_file())

    def test_merge_managed_block_replaces_existing_block_only(self):
        doc = "before\n" + format_managed_block("old") + "\nafter\n"
        new_block = format_managed_block("new")
        merged = merge_managed_block(doc, new_block)
        self.assertEqual(merged, "before\n" + new_block + "\nafter\n")


if __name__ == "__main__":
    unittest.main()
