"""Lane B3 AGY-LISTENER item 3: proof of listening + cost guard.

A returned card is no evidence of a listen (2026-09-25 pilot: 3.46M
tokens, 0 wav view_file calls), so after each agy call the harness
reads the run's agy conversation record and requires a view_file step
on the staged audio path, plus input_tokens + cache_read_tokens within
the cap (250000 full track / 150000 window). A failed check holds the
card back (no automatic retry) and records
``listen_rejected: {reason, usage, conversation_id}`` in meta.json.
An unreadable record rejects as 'unverifiable' (fail closed) unless
``--allow-unverified-listen`` is passed.

Synthetic only: fixture conversation DBs built in temp dirs, fake
runners — never agy, Live, or the real ~/.gemini store.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import audio_ref as _ar
from tests.test_agy_workspace_isolation import _v3_envelope
from tests._stage_passthrough import passthrough_listenable_staging


_STEPS_DDL = (
    "CREATE TABLE IF NOT EXISTS steps (idx INTEGER PRIMARY KEY, "
    "step_type INTEGER, status INTEGER, metadata BLOB, error_details BLOB, "
    "render_info BLOB, step_payload BLOB)"
)


def _fixture_db(conversations_dir: str | Path,
                conversation_id: str,
                rows: list[tuple], *, usage: dict | None = None) -> Path:
    """Write a fixture <conversation_id>.db with the real agy record
    schema (steps table with step_type/status + blob columns)."""
    conversations_dir = Path(conversations_dir)
    conversations_dir.mkdir(parents=True, exist_ok=True)
    db_path = conversations_dir / f"{conversation_id}.db"
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(_STEPS_DDL)
        con.execute("DELETE FROM steps")
        for row in rows:
            con.execute(
                "INSERT INTO steps "
                "(idx, step_type, status, metadata, error_details, "
                "render_info, step_payload) VALUES (?,?,?,?,?,?, ?)",
                row)
        if usage is not None:
            con.execute("CREATE TABLE gen_metadata (metadata TEXT)")
            con.execute("INSERT INTO gen_metadata VALUES (?)",
                        (json.dumps({"usage": usage}),))
        con.commit()
    finally:
        con.close()
    return db_path


def _planner_view_bytes(staged_audio_path: str) -> bytes:
    """A step_type 15 tool CALL: the agent requesting view_file. A
    request alone is never proof of a listen."""
    return (b"\tview_file\x1a\x08call_1"
            b'{"AbsolutePath":"' + str(staged_audio_path).encode()
            + b'","toolAction":"view","toolSummary":"View take.wav"}')


def _result_view_bytes(staged_audio_path: str) -> bytes:
    """A step_type 132 / status 3 success RESULT: the heard bytes."""
    return (b"The following is the entire, complete content of the "
            b"requested file. audio/wav "
            b".tempmediaStorage/media_1.wav")


def _result_metadata(staged_audio_path: str) -> bytes:
    return (b"view_file "
            b'{"AbsolutePath":"' + str(staged_audio_path).encode()
            + b'"}')


def _result_mime_failure_bytes(staged_audio_path: str,
                               mime: str = "audio/wave") -> bytes:
    """A step_type 17 failure RESULT: the file never reached the model
    (raw 24-bit/float session takes, mp3s)."""
    return (b"view_file result\x1a\x08call_1"
            b'{"AbsolutePath":"' + str(staged_audio_path).encode()
            + b'"}invalid tool call error (invalid_args) unsupported '
            b"mime type " + mime.encode())


def plant_listen_evidence(conversations_dir: str | Path,
                          conversation_id: str,
                          staged_audio_path: str) -> Path:
    """Write a fixture <conversation_id>.db with a planner view_file call
    plus its step_type 132 / status 3 success result on the staged
    audio path — the shape the proof reader accepts. Returns the DB."""
    return _fixture_db(conversations_dir, conversation_id, [
        (0, 15, 1, _planner_view_bytes(staged_audio_path), None, None,
         None),
        (1, 132, 3, _result_metadata(staged_audio_path), None, None,
         _result_view_bytes(staged_audio_path)),
    ])


def plant_mime_failure_evidence(conversations_dir: str | Path,
                                conversation_id: str,
                                staged_audio_path: str,
                                mime: str = "audio/wave") -> Path:
    """Write a fixture <conversation_id>.db with a planner view_file call
    plus its step_type 17 mime failure — the agent asked, the file
    never reached the model. Must REJECT."""
    return _fixture_db(conversations_dir, conversation_id, [
        (0, 15, 1, _planner_view_bytes(staged_audio_path), None, None,
         None),
        (1, 17, 4, None,
         _result_mime_failure_bytes(staged_audio_path, mime), None,
         None),
    ])


def plant_planner_only_evidence(conversations_dir: str | Path,
                                conversation_id: str,
                                staged_audio_path: str) -> Path:
    """Write a fixture <conversation_id>.db with only the step_type 15
    view_file CALL and no result — a request alone. Must REJECT."""
    return _fixture_db(conversations_dir, conversation_id, [
        (0, 15, 1, _planner_view_bytes(staged_audio_path), None, None,
         None),
    ])


def plant_thinking_mention_evidence(conversations_dir: str | Path,
                                    conversation_id: str,
                                    staged_audio_path: str) -> Path:
    """Write a fixture <conversation_id>.db where thinking prose merely
    mentions view_file and the staged path (unquoted). Must REJECT."""
    return _fixture_db(conversations_dir, conversation_id, [
        (0, 10, 1, None, None,
         b"I should view_file " + str(staged_audio_path).encode()
         + b" after reading the repo", None),
    ])


def plant_run_command_evidence(conversations_dir: str | Path,
                               conversation_id: str,
                               staged_audio_path: str) -> Path:
    """Write a fixture <conversation_id>.db where a tool call only runs
    a command (ffprobe) on the staged file — a python-read of the wav
    is not a listen. Must REJECT."""
    return _fixture_db(conversations_dir, conversation_id, [
        (0, 15, 1,
         b"call_2run_command"
         b'{"CommandLine":"ffprobe -hide_banner '
         + str(staged_audio_path).encode()
         + b'","toolAction":"run"}', None, None, None),
    ])


def plant_deaf_evidence(conversations_dir: str | Path,
                        conversation_id: str) -> Path:
    """Write a fixture <conversation_id>.db shaped like the pilot's deaf
    run: repo tool calls plus the prompt echo, but no wav view."""
    return _fixture_db(conversations_dir, conversation_id, [
        (0, 14, 1,
         b"File: source.wav\x1a\x0bview_file\x10\x01run_command tool list",
         None, None, None),
        (1, 15, 1,
         b"call_1run_command"
         b'{"CommandLine":"git grep contract","Cwd":"/repo"}',
         None, None, None),
    ])


def _staged_path_from_prompt(prompt: str) -> str:
    match = re.search(
        r"view_file on (?:each of: )?(.+?)(?:,|\. Do not run commands)", prompt)
    assert match, "prompt must carry the listening directive"
    return match.group(1)


class TestVerifyAgyListen(unittest.TestCase):
    def test_rejected_listen_has_own_batch_failure_count(self):
        report = _ar.summarize_batch_results([
            {"ok": False, "listen_rejected": {"reason": "no_listen"}}
        ])
        self.assertEqual(report["listen_rejected_count"], 1)
        self.assertFalse(report["partial"])

    def test_exact_path_matches_realpath_across_symlinked_temp_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            actual = root / "private-tmp"
            actual.mkdir()
            alias = root / "tmp-link"
            alias.symlink_to(actual, target_is_directory=True)
            staged_alias = str(alias / "source.wav")
            recorded_real = os.path.realpath(staged_alias)
            plant_listen_evidence(td, "conv-realpath-a", recorded_real)
            first = _ar.verify_agy_listen(
                conversation_id="conv-realpath-a",
                staged_audio_path=staged_alias,
                usage={"input_tokens": 100}, conversations_dir=td)
            self.assertTrue(first["ok"])

            plant_listen_evidence(td, "conv-realpath-b", staged_alias)
            second = _ar.verify_agy_listen(
                conversation_id="conv-realpath-b",
                staged_audio_path=recorded_real,
                usage={"input_tokens": 100}, conversations_dir=td)
            self.assertTrue(second["ok"])

    def test_deaf_record_rejects_no_listen(self):
        with tempfile.TemporaryDirectory() as td:
            plant_deaf_evidence(td, "conv-deaf")
            res = _ar.verify_agy_listen(
                conversation_id="conv-deaf",
                staged_audio_path="/tmp/audioref-listen-X/source.wav",
                usage={"input_tokens": 100, "cache_read_tokens": 0},
                conversations_dir=td)
            self.assertFalse(res["ok"])
            self.assertFalse(res["verified"])
            self.assertEqual(res["reason"], "no_listen")

    def test_missing_record_rejects_unverifiable_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            res = _ar.verify_agy_listen(
                conversation_id="conv-gone",
                staged_audio_path="/tmp/audioref-listen-X/source.wav",
                usage={"input_tokens": 100},
                conversations_dir=td)
            self.assertFalse(res["ok"])
            self.assertEqual(res["reason"], "unverifiable")

    def test_allow_unverified_listen_proceeds_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            res = _ar.verify_agy_listen(
                conversation_id="conv-gone",
                staged_audio_path="/tmp/audioref-listen-X/source.wav",
                usage={"input_tokens": 100},
                conversations_dir=td, allow_unverified=True)
            self.assertTrue(res["ok"])
            self.assertFalse(res["verified"])
            self.assertEqual(res["reason"], "unverifiable")

    def test_allow_unverified_still_enforces_token_cap(self):
        with tempfile.TemporaryDirectory() as td:
            res = _ar.verify_agy_listen(
                conversation_id="conv-gone",
                staged_audio_path="/tmp/audioref-listen-X/source.wav",
                usage={"input_tokens": 200000, "cache_read_tokens": 100000},
                token_cap=250000, conversations_dir=td,
                allow_unverified=True)
            self.assertFalse(res["ok"])
            self.assertEqual(res["reason"], "over_budget")

    def test_over_budget_rejects(self):
        with tempfile.TemporaryDirectory() as td:
            staged = "/tmp/audioref-listen-X/source.wav"
            plant_listen_evidence(td, "conv-rich", staged)
            res = _ar.verify_agy_listen(
                conversation_id="conv-rich", staged_audio_path=staged,
                usage={"input_tokens": 200000, "cache_read_tokens": 100000},
                token_cap=250000, conversations_dir=td)
            self.assertFalse(res["ok"])
            self.assertEqual(res["reason"], "over_budget")

    def test_within_cap_verifies(self):
        with tempfile.TemporaryDirectory() as td:
            staged = "/tmp/audioref-listen-X/source.wav"
            plant_listen_evidence(td, "conv-cheap", staged)
            res = _ar.verify_agy_listen(
                conversation_id="conv-cheap", staged_audio_path=staged,
                usage={"input_tokens": 15000, "cache_read_tokens": 4000},
                token_cap=250000, conversations_dir=td)
            self.assertTrue(res["ok"])
            self.assertTrue(res["verified"])
            self.assertIsNone(res["reason"])

    def test_missing_usage_passes_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            staged = "/tmp/audioref-listen-X/source.wav"
            plant_listen_evidence(td, "conv-n usage".replace(" ", ""), staged)
            res = _ar.verify_agy_listen(
                conversation_id="conv-nusage", staged_audio_path=staged,
                usage={}, conversations_dir=td)
            self.assertTrue(res["ok"])
            self.assertTrue(res["verified"])
            self.assertTrue(res.get("usage_missing"))

    def test_protobuf_gen_metadata_surfaces_usage_missing(self):
        with tempfile.TemporaryDirectory() as td:
            staged = "/tmp/audioref-listen-X/source.wav"
            _fixture_db(td, "conv-db-usage", [
                (0, 132, 3, _result_metadata(staged), None, None,
                 _result_view_bytes(staged)),
            ], usage={"input_tokens": 120, "cache_read_tokens": 30})
            res = _ar.verify_agy_listen(
                conversation_id="conv-db-usage", staged_audio_path=staged,
                usage={}, conversations_dir=td)
            self.assertTrue(res["ok"])
            self.assertTrue(res["usage_missing"])

    def test_step_type_8_success_uses_metadata_and_payload_columns(self):
        with tempfile.TemporaryDirectory() as td:
            staged = "/tmp/audioref-listen-X/source.wav"
            _fixture_db(td, "conv-step-eight", [
                (0, 8, 3, _result_metadata(staged), None, None,
                 _result_view_bytes(staged)),
            ])
            self.assertTrue(_ar.conversation_shows_wav_view(
                "conv-step-eight", staged, conversations_dir=td))

    def test_run_command_echoing_path_and_audio_mime_rejects(self):
        with tempfile.TemporaryDirectory() as td:
            staged = "/tmp/audioref-listen-X/source.wav"
            _fixture_db(td, "conv-echo", [
                (0, 132, 3,
                 b'run_command {"CommandLine":"cat ' + staged.encode()
                 + b'"}', None, None, b"audio/wav tempmediaStorage")
            ])
            self.assertFalse(_ar.conversation_shows_wav_view(
                "conv-echo", staged, conversations_dir=td))

    def test_failed_mime_view_rejects(self):
        with tempfile.TemporaryDirectory() as td:
            staged = "/tmp/audioref-listen-X/source.wav"
            plant_mime_failure_evidence(td, "conv-mime", staged)
            res = _ar.verify_agy_listen(
                conversation_id="conv-mime", staged_audio_path=staged,
                usage={"input_tokens": 100}, conversations_dir=td)
            self.assertFalse(res["ok"])
            self.assertFalse(res["verified"])
            self.assertEqual(res["reason"], "no_listen")

    def test_failed_mime_vetoes_beside_success(self):
        with tempfile.TemporaryDirectory() as td:
            staged = "/tmp/audioref-listen-X/source.wav"
            _fixture_db(td, "conv-both", [
                (0, 15, 1, _planner_view_bytes(staged), None, None,
                 None),
                (1, 132, 3, None, None, None,
                 _result_view_bytes(staged)),
                (2, 17, 4, None,
                 _result_mime_failure_bytes(staged), None, None),
            ])
            res = _ar.verify_agy_listen(
                conversation_id="conv-both", staged_audio_path=staged,
                usage={"input_tokens": 100}, conversations_dir=td)
            self.assertFalse(res["ok"])

    def test_planner_call_alone_rejects(self):
        with tempfile.TemporaryDirectory() as td:
            staged = "/tmp/audioref-listen-X/source.wav"
            plant_planner_only_evidence(td, "conv-plan", staged)
            res = _ar.verify_agy_listen(
                conversation_id="conv-plan", staged_audio_path=staged,
                usage={"input_tokens": 100}, conversations_dir=td)
            self.assertFalse(res["ok"])
            self.assertEqual(res["reason"], "no_listen")

    def test_thinking_mention_rejects(self):
        with tempfile.TemporaryDirectory() as td:
            staged = "/tmp/audioref-listen-X/source.wav"
            plant_thinking_mention_evidence(td, "conv-think", staged)
            res = _ar.verify_agy_listen(
                conversation_id="conv-think", staged_audio_path=staged,
                usage={"input_tokens": 100}, conversations_dir=td)
            self.assertFalse(res["ok"])
            self.assertEqual(res["reason"], "no_listen")

    def test_run_command_on_wav_rejects(self):
        with tempfile.TemporaryDirectory() as td:
            staged = "/tmp/audioref-listen-X/source.wav"
            plant_run_command_evidence(td, "conv-cmd", staged)
            res = _ar.verify_agy_listen(
                conversation_id="conv-cmd", staged_audio_path=staged,
                usage={"input_tokens": 100}, conversations_dir=td)
            self.assertFalse(res["ok"])
            self.assertEqual(res["reason"], "no_listen")

    def test_exact_path_sibling_prefix_rejects(self):
        with tempfile.TemporaryDirectory() as td:
            staged = "/tmp/audioref-listen-X/source.wav"
            _fixture_db(td, "conv-sib", [
                (0, 132, 3, None, None, None,
                 _result_view_bytes(staged + ".bak")),
            ])
            res = _ar.verify_agy_listen(
                conversation_id="conv-sib", staged_audio_path=staged,
                usage={"input_tokens": 100}, conversations_dir=td)
            self.assertFalse(res["ok"])
            self.assertEqual(res["reason"], "no_listen")

    def test_result_without_media_marker_rejects(self):
        with tempfile.TemporaryDirectory() as td:
            staged = "/tmp/audioref-listen-X/source.wav"
            bare = (b"view_file result"
                    b'{"AbsolutePath":"' + staged.encode() + b'"}done')
            _fixture_db(td, "conv-bare", [
                (0, 132, 3, None, None, None, bare),
            ])
            res = _ar.verify_agy_listen(
                conversation_id="conv-bare", staged_audio_path=staged,
                usage={"input_tokens": 100}, conversations_dir=td)
            self.assertFalse(res["ok"])
            self.assertEqual(res["reason"], "no_listen")


class TestHarnessVerification(unittest.TestCase):
    def _envelope(self, conversation_id, usage=None):
        payload = _v3_envelope()
        payload["conversation_id"] = conversation_id
        if usage is not None:
            payload["usage"] = usage
        return payload

    def test_harness_returns_unverified_card_without_proof_no_retry(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "source.wav"
            wav.write_bytes(b"RIFFfake")
            convs = Path(td) / "convs"
            convs.mkdir()
            captured: dict = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = list(cmd)
                return _ar.subprocess.CompletedProcess(
                    cmd, 0,
                    json.dumps(self._envelope("conv-noproof")), "")

            with passthrough_listenable_staging(), \
                 mock.patch.object(_ar, "_which", return_value="agy"), \
                    mock.patch.object(_ar, "_run", side_effect=fake_run):
                result = _ar.run_agy_describe_v3(
                    wav=wav, focus="full", prompt_extra=None, duration_s=60,
                    timeout_s=61, model="gemini-3.8-flash-high",
                    verify_listen=True, conversations_dir=convs)
            self.assertFalse(result["ok"])
            self.assertIn("card", result)
            self.assertFalse(result["listen_verification"]["verified"])
            rejected = result["listen_rejected"]
            self.assertEqual(rejected["reason"], "unverifiable")
            self.assertEqual(rejected["conversation_id"], "conv-noproof")
            self.assertIn("usage", rejected)
            # No automatic retry: a single attempt, never the repair loop.
            self.assertEqual(result["attempts"], 1)

    def test_harness_accepts_verified_listen(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "source.wav"
            wav.write_bytes(b"RIFFfake")
            convs = Path(td) / "convs"

            def fake_run(cmd, **kwargs):
                prompt = cmd[cmd.index("--print") + 1]
                staged = _staged_path_from_prompt(prompt)
                plant_listen_evidence(convs, "conv-heard", staged)
                return _ar.subprocess.CompletedProcess(
                    cmd, 0,
                    json.dumps(self._envelope("conv-heard")), "")

            with passthrough_listenable_staging(), \
                 mock.patch.object(_ar, "_which", return_value="agy"), \
                    mock.patch.object(_ar, "_run", side_effect=fake_run):
                result = _ar.run_agy_describe_v3(
                    wav=wav, focus="full", prompt_extra=None, duration_s=60,
                    timeout_s=61, model="gemini-3.8-flash-high",
                    verify_listen=True, conversations_dir=convs)
            self.assertTrue(result["ok"])
            self.assertIn("card", result)
            self.assertEqual(result["conversation_id"], "conv-heard")
            self.assertTrue(
                result["listen_verification"]["verified"])

    def test_harness_rejects_over_budget_window(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "source.wav"
            wav.write_bytes(b"RIFFfake")
            convs = Path(td) / "convs"

            def fake_run(cmd, **kwargs):
                prompt = cmd[cmd.index("--print") + 1]
                staged = _staged_path_from_prompt(prompt)
                plant_listen_evidence(convs, "conv-fat", staged)
                return _ar.subprocess.CompletedProcess(
                    cmd, 0,
                    json.dumps(self._envelope(
                        "conv-fat",
                        usage={"input_tokens": 200000,
                               "cache_read_tokens": 0})), "")

            with passthrough_listenable_staging(), \
                 mock.patch.object(_ar, "_which", return_value="agy"), \
                    mock.patch.object(_ar, "_run", side_effect=fake_run):
                result = _ar.run_agy_describe_v3(
                    wav=wav, focus="full", prompt_extra=None, duration_s=60,
                    timeout_s=61, model="gemini-3.8-flash-high",
                    verify_listen=True, listen_token_cap=150000,
                    conversations_dir=convs)
            self.assertFalse(result["ok"])
            self.assertEqual(
                result["listen_rejected"]["reason"], "over_budget")


class TestAnalyzeVerification(unittest.TestCase):
    def _analyze(self, root: Path, convs: Path, plant: bool):
        out = root / "refs" / "new"
        out.mkdir(parents=True)
        (out / "source.wav").write_bytes(b"RIFFfake")
        (out / "meta.json").write_text(json.dumps({"source": "old"}))

        def fake_run(cmd, **kwargs):
            if "--add-dir" in cmd:
                if plant:
                    staged = _staged_path_from_prompt(
                        cmd[cmd.index("--print") + 1])
                    plant_listen_evidence(convs, "conv-live", staged)
                payload = _v3_envelope()
                payload["conversation_id"] = "conv-live"
                return _ar.subprocess.CompletedProcess(
                    cmd, 0, json.dumps(payload), "")
            return _ar.subprocess.CompletedProcess(cmd, 0, "", "")

        old_root, old_index = _ar.OUT_ROOT, _ar.INDEX_PATH
        _ar.OUT_ROOT, _ar.INDEX_PATH = root / "refs", root / "index.json"
        try:
            with passthrough_listenable_staging(), \
                 mock.patch.object(_ar, "_which", return_value="agy"), \
                 mock.patch.object(_ar, "_run", side_effect=fake_run), \
                 mock.patch.object(_ar, "ffprobe_info",
                                   return_value={"duration_s": 60}), \
                 mock.patch.object(_ar, "_version_first_line",
                                   return_value=None):
                return _ar.analyze_audio_ref(
                    from_dir=str(out), modes=["describe"],
                    model="model-v2", conversations_dir=convs)
        finally:
            _ar.OUT_ROOT, _ar.INDEX_PATH = old_root, old_index

    def test_analyze_persists_unverified_card_and_records_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            root, convs = Path(td), Path(td) / "convs"
            convs.mkdir()
            result = self._analyze(root, convs, plant=False)
            self.assertFalse(result["mode_ok"]["describe"])
            out = Path(result["out_dir"])
            self.assertFalse((out / "card.json").exists(),
                             "an unverified card is not canonical evidence")
            self.assertTrue((out / "card.unverified.json").is_file())
            self.assertTrue((out / "describe.unverified.txt").is_file())
            saved = json.loads((out / "meta.json").read_text())
            self.assertEqual(saved["listen_rejected"]["reason"],
                             "unverifiable")
            self.assertEqual(saved["listen_rejected"]["conversation_id"],
                             "conv-live")
            self.assertIn("usage", saved["listen_rejected"])
            self.assertTrue(any("listen proof unavailable" in w
                                for w in saved["warnings"]))

    def test_analyze_persists_verified_listen(self):
        with tempfile.TemporaryDirectory() as td:
            root, convs = Path(td), Path(td) / "convs"
            convs.mkdir()
            result = self._analyze(root, convs, plant=True)
            self.assertTrue(result["mode_ok"]["describe"])
            out = Path(result["out_dir"])
            self.assertTrue((out / "card.json").is_file())
            saved = json.loads((out / "meta.json").read_text())
            self.assertIsNone(saved["listen_rejected"])
            self.assertEqual(saved["conversation_id"], "conv-live")


class TestBatchReportsRejection(unittest.TestCase):
    def test_batch_row_carries_listen_rejected(self):
        def fake_analyze(**kw):
            return {"ok": False, "partial": False, "ref_id": None,
                    "out_dir": "/tmp/r", "error": "listen rejected: no_listen",
                    "describe_result": {
                        "ok": False,
                        "error": "listen rejected: no_listen",
                        "listen_rejected": {
                            "reason": "no_listen",
                            "usage": {"input_tokens": 10},
                            "conversation_id": "conv-x",
                        },
                    },
                    "listen_usage": None, "focus": "full", "agy_model": "m",
                    "timeout_s": 600, "contract_version": None, "hashes": None,
                    "evidence": None, "conversation_id": "conv-x",
                    "listen_rejected": {
                        "reason": "no_listen",
                        "usage": {"input_tokens": 10},
                        "conversation_id": "conv-x",
                    }}

        with tempfile.TemporaryDirectory() as td:
            manifest = {"batch_id": "rej", "items": [{"source": "a"}]}
            with mock.patch.object(_ar, "BATCH_ROOT", Path(td)), \
                 mock.patch.object(_ar, "analyze_audio_ref",
                                   side_effect=fake_analyze):
                report = _ar.run_batch(manifest, resume=False)
            row = report["results"][0]
            self.assertFalse(row["ok"])
            self.assertEqual(row["listen_rejected"]["reason"], "no_listen")
            self.assertEqual(row["conversation_id"], "conv-x")


if __name__ == "__main__":
    unittest.main()
