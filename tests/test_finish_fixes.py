"""Batch 5 finish-fixes item 1 regression tests."""
import json
import os
import shutil
import struct
import sys
import tempfile
import wave
import unittest
from unittest import mock

from mcp_server import finisher, rituals


class ProjectResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old_root = finisher.ROOT
        finisher.ROOT = self.temp.name
        self.addCleanup(setattr, finisher, "ROOT", self.old_root)
        self.project_dir = os.path.join(self.temp.name, "store", "projects")
        os.makedirs(self.project_dir)

    def index(self, entries):
        with open(os.path.join(self.project_dir, "_index.json"), "w") as f:
            json.dump(entries, f)
        for entry in entries:
            with open(os.path.join(self.project_dir, entry["id"] + ".json"), "w") as f:
                json.dump({"path": entry["path"]}, f)

    def test_live_never_resolves_to_catalog_file(self):
        self.index([{"id": "p1", "name": "livedemo_new.als",
                     "path": "/music/livedemo Project/livedemo_new.als"}])
        with self.assertRaisesRegex(ValueError, "live.*open Set"):
            finisher._project_record("live")

    def test_duplicate_exact_name_reports_candidate_paths(self):
        self.index([
            {"id": "p1", "name": "song.als", "path": "/one/song.als"},
            {"id": "p2", "name": "song.als", "path": "/two/song.als"},
        ])
        with self.assertRaises(ValueError) as caught:
            finisher._project_record("song")
        self.assertIn("/one/song.als", str(caught.exception))
        self.assertIn("/two/song.als", str(caught.exception))

    def test_exact_stem_and_full_path_resolve(self):
        self.index([{"id": "p1", "name": "song.als", "path": "/one/song.als"}])
        self.assertEqual(finisher._project_record("song")["path"], "/one/song.als")
        self.assertEqual(finisher._project_record("/one/song.als")["path"], "/one/song.als")


class LiveRitualTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        rituals.set_rituals_paths_for_test(os.path.join(self.temp.name, "rituals.json"), None)
        self.addCleanup(rituals.set_rituals_paths_for_test, None, None)

    @staticmethod
    def live_record():
        # Shape produced by finisher.project_from_live_tracks for a saved empty Set.
        return {"path": "/music/Tonight.als", "tempo": 120, "track_count": 0,
                "midi_track_names": [], "audio_track_names": [], "clip_count": 0,
                "total_notes": 0, "audio_clip_count": 0, "notes_skipped": 0,
                "skip_detail": {"hidden_past_loop": 0}, "assumptions": [], "clips": []}

    def test_one_hour_close_live_reads_open_set(self):
        with mock.patch.object(finisher, "_read_live_project", return_value=self.live_record()) as reader:
            result = rituals.one_hour_close("live")
        reader.assert_called_once_with()
        self.assertEqual(result["project"], "/music/Tonight.als")

    def test_marry_live_reads_open_set(self):
        with mock.patch.object(rituals, "_load_ledger", return_value={"married": [], "buried": []}), \
             mock.patch.object(rituals, "_save_ledger"), \
             mock.patch.object(finisher, "_read_live_project", return_value=self.live_record()) as reader:
            result = rituals.marry("live")
        reader.assert_called_once_with()
        self.assertEqual(result["married"], "/music/Tonight.als")
        self.assertEqual(result["diagnosis"]["project"], "/music/Tonight.als")

    def test_close_then_confirm_uses_resolved_catalog_path(self):
        old_root = finisher.ROOT
        finisher.ROOT = self.temp.name
        self.addCleanup(setattr, finisher, "ROOT", old_root)
        projects = os.path.join(self.temp.name, "store", "projects")
        os.makedirs(projects, exist_ok=True)
        # Shape from store/projects/_index.json and a materialized parse_als project record.
        with open(os.path.join(projects, "_index.json"), "w") as fh:
            json.dump([{"id": "song-id", "name": "ballad.als", "path": "/music/ballad.als"}], fh)
        record = {"path": "/music/ballad.als", "tempo": 120, "track_count": 1,
                  "midi_track_names": ["Keys"], "audio_track_names": [], "clip_count": 0,
                  "total_notes": 0, "audio_clip_count": 0, "notes_skipped": 0,
                  "skip_detail": {}, "assumptions": [], "clips": []}
        with open(os.path.join(projects, "song-id.json"), "w") as fh:
            json.dump(record, fh)
        with mock.patch.object(finisher, "run_detectors", return_value=[]):
            result = rituals.one_hour_close("ballad")
        self.assertEqual(result["project"], "/music/ballad.als")
        path = os.path.join(self.temp.name, "bounce.wav")
        with wave.open(path, "wb") as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(8000)
            out.writeframes(b"\0\0" * 8000)
        with mock.patch.object(rituals, "_now", return_value="2099-01-01T00:00:00+00:00"):
            result = rituals.confirm_bounce("ballad", path, confirm=True)
        self.assertEqual(result["project"], "/music/ballad.als")

    def test_live_close_and_bounce_share_reported_set_path(self):
        with mock.patch.object(finisher, "_read_live_project", return_value=self.live_record()):
            close = rituals.one_hour_close("live")
        path = os.path.join(self.temp.name, "live.wav")
        with wave.open(path, "wb") as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(8000)
            out.writeframes(b"\0\0" * 8000)
        with mock.patch.object(finisher, "_read_live_project", return_value=self.live_record()), \
             mock.patch.object(rituals, "_now", return_value="2099-01-01T00:00:00+00:00"):
            bounce = rituals.confirm_bounce("live", path, confirm=True)
        self.assertEqual(close["project"], bounce["project"])
        self.assertEqual(bounce["project"], "/music/Tonight.als")

    def test_live_close_bounce_declaration_and_piece_decision_share_set_path(self):
        from mcp_server import piece_context
        live = self.live_record()
        with mock.patch.object(finisher, "_read_live_project", return_value=live), \
             mock.patch.object(rituals, "_now", return_value="2000-01-01T00:00:00+00:00"):
            close = rituals.one_hour_close("live")
            path = os.path.join(self.temp.name, "live.wav")
            with wave.open(path, "wb") as out:
                out.setnchannels(1); out.setsampwidth(2); out.setframerate(8000)
                out.writeframes(b"\0\0" * 8000)
            os.utime(path, (1900000000, 1900000000))
            bounce = rituals.confirm_bounce("live", path, confirm=True)
            declared = rituals.declare_finishing_state("live", "complete_draft")
        self.assertEqual({close["project"], bounce["project"], declared["project"]},
                         {"/music/Tonight.als"})
        decisions_path = os.path.join(self.temp.name, "piece_decisions.json")
        with mock.patch.object(piece_context, "_decision_path", return_value=decisions_path), \
             mock.patch.object(finisher, "run_detectors", return_value=[{
                 "detector": "flat_energy_run", "severity": "ask", "finding": "question",
                 "evidence": {}, "spans": []}]):
            finisher.whats_missing(project_dict=live, intended_findings=["flat_energy_run"])
        with open(decisions_path) as fh:
            decisions = json.load(fh)["decisions"]
        self.assertEqual(decisions[0]["set_key"], "/music/Tonight.als")

    def test_unsaved_live_set_requires_saving_before_finishing(self):
        unsaved = dict(self.live_record(), path="live:Tonight")
        with mock.patch.object(finisher, "_read_live_project", return_value=unsaved):
            with self.assertRaisesRegex(ValueError, "save the Set first"):
                rituals.one_hour_close("live")

    def test_unsaved_live_diagnosis_cannot_record_intent(self):
        from mcp_server import server, piece_context
        live = dict(self.live_record(), path="live:Tonight")
        decisions_path = os.path.join(self.temp.name, "piece_decisions.json")
        def consume_live_read(coro):
            coro.close()
            return {}
        with mock.patch.object(server, "_run_async", side_effect=consume_live_read), \
             mock.patch.object(finisher, "project_from_live_tracks", return_value=live), \
             mock.patch.object(piece_context, "_decision_path", return_value=decisions_path), \
             mock.patch.object(finisher, "run_detectors", return_value=[{
                 "detector": "flat_energy_run", "severity": "ask", "finding": "question",
                 "evidence": {}, "spans": []}]), \
             self.assertRaisesRegex(ValueError, "save the Set first"):
            server.whats_missing("live", intended_findings=["flat_energy_run"])
        self.assertFalse(os.path.exists(decisions_path))

    def test_same_name_projects_keep_separate_finishing_records(self):
        old_root = finisher.ROOT
        finisher.ROOT = self.temp.name
        self.addCleanup(setattr, finisher, "ROOT", old_root)
        projects = os.path.join(self.temp.name, "store", "projects")
        os.makedirs(projects)
        for project_id, path in (("one", "/one/Song.als"), ("two", "/two/Song.als")):
            with open(os.path.join(projects, project_id + ".json"), "w") as fh:
                json.dump({"path": path, "tempo": 120, "track_count": 0,
                           "midi_track_names": [], "audio_track_names": [], "clip_count": 0,
                           "total_notes": 0, "audio_clip_count": 0, "notes_skipped": 0,
                           "skip_detail": {}, "assumptions": [], "clips": []}, fh)
        with open(os.path.join(projects, "_index.json"), "w") as fh:
            json.dump([{"id": "one", "name": "Song.als", "path": "/one/Song.als"},
                       {"id": "two", "name": "Song.als", "path": "/two/Song.als"}], fh)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            rituals.declare_finishing_state("Song", "complete_draft")
        rituals.declare_finishing_state("/one/Song.als", "complete_draft")
        rituals.declare_finishing_state("/two/Song.als", "complete_draft")
        with open(os.path.join(self.temp.name, "rituals.json")) as fh:
            finishing = json.load(fh)["finishing"]
        self.assertEqual(set(finishing), {"/one/Song.als", "/two/Song.als"})


class FinishingStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rituals_path = os.path.join(self.temp.name, "rituals.json")
        rituals.set_rituals_paths_for_test(self.rituals_path, None)
        self.addCleanup(rituals.set_rituals_paths_for_test, None, None)
        self.old_root = finisher.ROOT
        finisher.ROOT = self.temp.name
        self.addCleanup(setattr, finisher, "ROOT", self.old_root)
        projects = os.path.join(self.temp.name, "store", "projects")
        os.makedirs(projects)
        with open(os.path.join(projects, "_index.json"), "w") as fh:
            json.dump([{"id": "song", "name": "Song.als", "path": "/music/Song.als"}], fh)
        # Shape from the materialized parse_als project record.
        with open(os.path.join(projects, "song.json"), "w") as fh:
            json.dump({"path": "/music/Song.als", "tempo": 120, "track_count": 0,
                       "midi_track_names": [], "audio_track_names": [], "clip_count": 0,
                       "total_notes": 0, "audio_clip_count": 0, "notes_skipped": 0,
                       "skip_detail": {}, "assumptions": [], "clips": []}, fh)

    def test_close_stores_start_and_bounce_requires_verified_new_audio(self):
        with mock.patch.object(rituals.finisher, "whats_missing", return_value={"project": "Song", "proposals": []}):
            close = rituals.one_hour_close("Song")
        self.assertEqual(close["project"], "/music/Song.als")
        with open(self.rituals_path) as fh:
            self.assertIn("started_at", json.load(fh)["finishing"]["/music/Song.als"])
        path = os.path.join(self.temp.name, "bounce.wav")
        # Shape produced by a PCM WAV exporter: RIFF/WAVE with PCM format header.
        with wave.open(path, "wb") as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(8000)
            out.writeframes(b"\0\0" * 8000)
        with mock.patch.object(rituals, "_now", return_value="2099-01-01T00:00:00+00:00"):
            result = rituals.confirm_bounce("Song", path, confirm=True)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["duration_s"], 1.0)
        with open(self.rituals_path) as fh:
            self.assertEqual(json.load(fh)["finishing"]["/music/Song.als"]["bounce_confirmed"][0]["exported_by"], "model_on_request")

    def test_repeated_close_preserves_start_for_bounce_confirmation(self):
        times = iter(("2000-01-01T00:00:00+00:00",
                      "2000-01-03T00:00:00+00:00"))
        with mock.patch.object(rituals.finisher, "whats_missing",
                               return_value={"project": "Song", "proposals": []}), \
             mock.patch.object(rituals, "_now", side_effect=lambda: next(times)):
            first_close = rituals.one_hour_close("Song")
            path = os.path.join(self.temp.name, "between-closes.wav")
            with wave.open(path, "wb") as out:
                out.setnchannels(1); out.setsampwidth(2); out.setframerate(8000)
                out.writeframes(b"\0\0" * 8000)
            os.utime(path, (946771200, 946771200))  # 2000-01-02, after the first close.
            second_close = rituals.one_hour_close("Song")
        self.assertEqual(second_close["started_at"], first_close["started_at"])
        with open(self.rituals_path) as fh:
            record = json.load(fh)["finishing"]["/music/Song.als"]
        self.assertEqual(record["started_at"], "2000-01-01T00:00:00+00:00")
        self.assertEqual(record["last_run_at"], "2000-01-03T00:00:00+00:00")
        with mock.patch.object(rituals, "_now", return_value="2000-01-04T00:00:00+00:00"):
            bounce = rituals.confirm_bounce("Song", path, confirm=True)
        self.assertEqual(bounce["status"], "confirmed")

    def test_owner_state_is_separate_and_model_path_cannot_accept_composition(self):
        from mcp_server import server
        with self.assertRaises(ValueError):
            rituals.declare_finishing_state("Song", "composition_accepted")
        with self.assertRaises(ValueError):
            rituals.declare_finishing_state("Song", "release_ready", note="ready")
        with self.assertRaises(ValueError):
            server.declare_finishing_state("Song", "composition_accepted")
        with self.assertRaises(ValueError):
            server.declare_finishing_state("Song", "release_ready")
        self.assertFalse(os.path.exists(self.rituals_path))

    def test_unknown_finishing_state_is_counted_as_other(self):
        with open(self.rituals_path, "w") as fh:
            json.dump({"finishing": {"Song": {"owner_states": [
                {"state": "future_state"}]}}}, fh)
        counts = rituals.finishing_counts()
        self.assertEqual(counts["other"], 1)

    def test_model_draft_rows_are_separate_from_owner_states_and_counts(self):
        with open(self.rituals_path, "w") as fh:
            json.dump({"finishing": {"Song": {
                "model_states": [{"state": "complete_draft", "declared_by": "model_on_request"}],
                "owner_states": [{"state": "complete_draft", "declared_by": "owner_cli"}],
            }}}, fh)
        counts = rituals.finishing_counts()
        self.assertEqual(counts["complete_draft"], 2)
        self.assertEqual(counts["composition_accepted"], 0)
        with mock.patch.object(rituals.finisher, "resolve_project_key", return_value="/music/Song.als"), \
             mock.patch.object(rituals, "_save_ledger"):
            rituals.declare_finishing_state("Song", "complete_draft")
        with open(self.rituals_path) as fh:
            record = json.load(fh)["finishing"]["Song"]
        self.assertEqual(len(record["model_states"]), 1)
        self.assertEqual(len(record["owner_states"]), 1)

    def test_confirm_bounce_header_edge_cases(self):
        def chunk(tag, payload):
            return tag + struct.pack(">I", len(payload)) + payload + (b"\0" if len(payload) & 1 else b"")

        def aifc(codec):
            rate = struct.pack(">H", 16398) + struct.pack(">Q", 8000 << 48)
            comm = struct.pack(">hIh", 1, 8000, 16) + rate + codec + b"\0"
            body = b"AIFC" + chunk(b"COMM", comm) + chunk(b"SSND", struct.pack(">II", 0, 0) + b"\0" * 16000)
            return b"FORM" + struct.pack(">I", len(body)) + body

        with open(self.rituals_path, "w") as fh:
            json.dump({"finishing": {"/music/Song.als": {"started_at": "2000-01-01T00:00:00+00:00"}}}, fh)
        for ext, codec in ((".aifc", b"NONE"), (".aifc", b"sowt")):
            path = os.path.join(self.temp.name, "bounce" + ext)
            with open(path, "wb") as fh:
                fh.write(aifc(codec))
            os.utime(path, (1900000000, 1900000000))
            self.assertAlmostEqual(rituals.confirm_bounce("Song", path)["duration_s"], 1.0)

        truncated = os.path.join(self.temp.name, "truncated.flac")
        # Real FLAC STREAMINFO layout: marker, last-metadata-block STREAMINFO, 34-byte payload.
        streaminfo = bytearray(34)
        streaminfo[10:18] = ((8000 << 44) | 8000).to_bytes(8, "big")
        with open(truncated, "wb") as fh:
            fh.write(b"fLaC\x80\x00\x00\x22" + streaminfo)
        os.utime(truncated, (1900000000, 1900000000))
        with self.assertRaisesRegex(ValueError, "duration could not be read"):
            rituals.confirm_bounce("Song", truncated)

    def test_confirm_bounce_hash_and_duration_do_not_read_entire_file(self):
        path = os.path.join(self.temp.name, "large.wav")
        with wave.open(path, "wb") as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(8000)
            out.writeframes(b"\0\0" * 8000)
        os.utime(path, (1900000000, 1900000000))
        with open(self.rituals_path, "w") as fh:
            json.dump({"finishing": {"/music/Song.als": {"started_at": "2000-01-01T00:00:00+00:00"}}}, fh)
        original_open = open
        class NoUnboundedRead:
            def __init__(self, wrapped): self.wrapped = wrapped
            def __enter__(self): return self
            def __exit__(self, *args): return self.wrapped.__exit__(*args)
            def fileno(self): return self.wrapped.fileno()
            def read(self, size=-1):
                if size < 0: raise AssertionError("unbounded read")
                return self.wrapped.read(size)
        def bounded_open(*args, **kwargs):
            wrapped = original_open(*args, **kwargs)
            mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")
            return NoUnboundedRead(wrapped) if mode == "rb" else wrapped
        with mock.patch.object(rituals.finisher, "resolve_project_key", return_value="/music/Song.als"), \
             mock.patch.object(rituals, "_load_ledger", return_value={"finishing": {"/music/Song.als": {"started_at": "2000-01-01T00:00:00+00:00"}}}), \
             mock.patch("builtins.open", side_effect=bounded_open):
            self.assertAlmostEqual(rituals.confirm_bounce("Song", path)["duration_s"], 1.0)

    def test_ask_findings_stay_open_questions_without_scheduled_moves(self):
        findings = [
            {"detector": "flat_energy_run", "severity": "ask",
             "question": "is this intended?", "bars": [1, 8]},
            {"detector": "uncommitted_ending", "severity": "ask",
             "question": "is this intended?", "bars": [9, 16]},
        ]
        diagnosis = {
            "project": None,
            "findings": findings,
            "proposals": [
                {"detector": "flat_energy_run", "intervention": "raise_energy_8bars",
                 "strategy": "change energy", "bars": [1, 8], "command": None},
                {"detector": "uncommitted_ending", "intervention": "three_endings",
                 "strategy": "choose an ending", "bars": [9, 16], "command": None},
            ],
        }
        close = rituals.one_hour_close(diagnosis=diagnosis)
        self.assertEqual([move["move"].split(":", 1)[0] for move in close["moves"]],
                         ["BOUNCE"])
        self.assertEqual([question["detector"] for question in close["open_questions"]],
                         ["flat_energy_run", "uncommitted_ending"])

        reports = [{"path": "/music/Song v1.als"}, {"path": "/music/Song v2.als"}]
        with mock.patch.object(rituals.finisher, "whats_missing",
                               return_value={"frame": {"tracks": [], "n_sections": 1,
                                                       "total_bars": 16},
                                             "findings": findings}):
            versions = rituals.version_courage("Song", members=reports)
        for member in versions["members"]:
            self.assertEqual(member["high_findings"], [])
            self.assertEqual(member["open_questions"],
                             ["flat_energy_run", "uncommitted_ending"])

    def test_audio_duration_headers_and_duplicate_confirmation(self):
        # Synthetic headers mirror WAV/AIFF/MP3 exporter container shapes.
        def chunk(tag, body, endian=">"):
            return tag + struct.pack(endian + "I", len(body)) + body + (b"\0" if len(body) % 2 else b"")

        def wav(fmt_tag=3, extensible=False):
            fmt = (struct.pack("<HHIIHH", 0xfffe if extensible else fmt_tag, 1, 8000,
                               32000, 4, 32) +
                   (struct.pack("<HHI16s", 22, 32, 3,
                                bytes.fromhex("0300000000001000800000aa00389b71")) if extensible else b""))
            body = b"WAVE" + chunk(b"fmt ", fmt, "<") + chunk(b"data", b"\0" * 32000, "<")
            return b"RIFF" + struct.pack("<I", len(body)) + body

        def extended_rate(rate):
            # 80-bit extended 8000 Hz: exponent 16398, explicit integer bit.
            return struct.pack(">H", 16398) + struct.pack(">Q", rate << 48)

        def aiff(aifc=False):
            comm = struct.pack(">hIh", 1, 8000, 32) + extended_rate(8000)
            if aifc:
                comm += b"fl32" + b"\0"
            body = (b"AIFC" if aifc else b"AIFF") + chunk(b"COMM", comm) + chunk(b"SSND", struct.pack(">II", 0, 0) + b"\0" * 32000)
            return b"FORM" + struct.pack(">I", len(body)) + body

        mp3 = b"ID3\x04\0\0\0\0\0\x05abcde" + bytes.fromhex("fffb9000") + b"\0" * 16000
        cases = [("float.wav", wav()), ("extensible.wav", wav(extensible=True)),
                 ("float.aiff", aiff(aifc=True)), ("tagged.mp3", mp3)]
        with open(self.rituals_path, "w") as fh:
            json.dump({"finishing": {"/music/Song.als": {"started_at": "2000-01-01T00:00:00+00:00"}}}, fh)
        for name, payload in cases:
            with self.subTest(name=name):
                path = os.path.join(self.temp.name, name)
                with open(path, "wb") as fh:
                    fh.write(payload)
                os.utime(path, (1900000000, 1900000000))
                self.assertAlmostEqual(rituals.confirm_bounce("Song", path)["duration_s"], 1.0, places=3)
        corrupt = os.path.join(self.temp.name, "bad.mp3")
        with open(corrupt, "wb") as fh:
            fh.write(b"ID3\x04\0\0\0\0\0\x00\xff\xfb\xf0\x00")
        os.utime(corrupt, (1900000000, 1900000000))
        with self.assertRaisesRegex(ValueError, "duration could not be read"):
            rituals.confirm_bounce("Song", corrupt)

        path = os.path.join(self.temp.name, "once.wav")
        with open(path, "wb") as fh:
            fh.write(wav())
        os.utime(path, (1900000000, 1900000000))
        first = rituals.confirm_bounce("Song", path, confirm=True)
        second = rituals.confirm_bounce("Song", path, confirm=True)
        self.assertEqual(first, second)
        with open(self.rituals_path) as fh:
            rows = json.load(fh)["finishing"]["/music/Song.als"]["bounce_confirmed"]
        self.assertEqual(len(rows), 1)


class DeclareFinishingCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        old_root = finisher.ROOT
        finisher.ROOT = self.temp.name
        self.addCleanup(setattr, finisher, "ROOT", old_root)
        projects = os.path.join(self.temp.name, "store", "projects")
        os.makedirs(projects)
        # Shape from store/projects/_index.json and its materialized <id>.json record.
        with open(os.path.join(projects, "_index.json"), "w") as fh:
            json.dump([{"id": "song-id", "name": "Song.als", "path": "/music/Song.als"}], fh)
        with open(os.path.join(projects, "song-id.json"), "w") as fh:
            json.dump({"path": "/music/Song.als"}, fh)
        rituals.set_rituals_paths_for_test(os.path.join(self.temp.name, "rituals.json"), None)
        self.addCleanup(rituals.set_rituals_paths_for_test, None, None)

    def test_cli_refuses_non_tty(self):
        from tools import declare_finishing
        with mock.patch.object(declare_finishing.sys.stdin, "isatty", return_value=False):
            with self.assertRaisesRegex(SystemExit, "interactive terminal"):
                declare_finishing.main(["Song", "composition_accepted"])

    def test_cli_requires_exact_yes(self):
        from tools import declare_finishing
        with mock.patch.object(declare_finishing.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", return_value="YES"), \
             mock.patch.object(declare_finishing, "_write_declaration") as write:
            self.assertEqual(declare_finishing.main(["Song", "release_ready"]), 1)
        write.assert_not_called()

    def test_cli_prompt_interruption_is_a_clean_refusal(self):
        from tools import declare_finishing
        for error in (EOFError(), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(declare_finishing.sys.stdin, "isatty", return_value=True), \
                 mock.patch("builtins.input", side_effect=error), \
                 mock.patch.object(declare_finishing, "_write_declaration") as write:
                self.assertEqual(declare_finishing.main(["Song", "release_ready"]), 1)
                write.assert_not_called()

    def test_cli_yes_writes_owner_only_declaration_to_exact_project_path(self):
        from tools import declare_finishing
        with mock.patch.object(declare_finishing.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", return_value="yes"):
            self.assertEqual(declare_finishing.main(["Song", "composition_accepted"]), 0)
        with open(os.path.join(self.temp.name, "rituals.json")) as fh:
            record = json.load(fh)["finishing"]["/music/Song.als"]["owner_states"][0]
        self.assertEqual(record["declared_by"], "owner_cli")

    def test_script_entrypoint_runs_under_pty_and_handles_refusal_and_confirmation(self):
        import pty
        import select
        import subprocess
        import time

        # Real catalogue/index record shape from store/projects/_index.json.
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        script = os.path.join(self.temp.name, "tools", "declare_finishing.py")
        os.makedirs(os.path.dirname(script))
        shutil.copyfile(os.path.join(repo, "tools", "declare_finishing.py"), script)
        os.symlink(os.path.join(repo, "tools", "boringness.py"),
                   os.path.join(self.temp.name, "tools", "boringness.py"))
        for directory in ("mcp_server", "ingest", "analyze"):
            os.symlink(os.path.join(repo, directory), os.path.join(self.temp.name, directory),
                       target_is_directory=True)
        projects = os.path.join(self.temp.name, "store", "projects")
        with open(os.path.join(projects, "_index.json"), "w") as fh:
            json.dump([{"id": "song-id", "name": "Song.als", "path": "/music/Song.als"}], fh)
        with open(os.path.join(projects, "song-id.json"), "w") as fh:
            json.dump({"path": "/music/Song.als"}, fh)
        env = os.environ.copy()
        env["PYTHONPATH"] = ""
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        def run(answer):
            master, slave = pty.openpty()
            proc = subprocess.Popen([sys.executable, script, "Song", "composition_accepted"],
                                    stdin=slave, stdout=slave, stderr=slave, cwd=self.temp.name,
                                    env=env, close_fds=True)
            os.close(slave)
            output = bytearray()
            deadline = time.time() + 10
            while time.time() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                    if b"Type yes to confirm:" in output:
                        os.write(master, (answer + "\n").encode())
                if proc.poll() is not None:
                    break
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                self.fail("CLI subprocess did not exit")
            os.close(master)
            return proc.returncode, output.decode(errors="replace")

        code, output = run("no")
        self.assertEqual(code, 1, output)
        self.assertIn("No declaration written.", output)
        ledger_path = os.path.join(self.temp.name, "store", "rituals.json")
        self.assertFalse(os.path.exists(ledger_path))

        code, output = run("yes")
        self.assertEqual(code, 0, output)
        with open(ledger_path) as fh:
            record = json.load(fh)["finishing"]["/music/Song.als"]["owner_states"][0]
        self.assertEqual(record["declared_by"], "owner_cli")

    def test_script_entrypoint_refuses_non_tty(self):
        import subprocess
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        script = os.path.join(self.temp.name, "tools", "declare_finishing.py")
        os.makedirs(os.path.dirname(script))
        shutil.copyfile(os.path.join(repo, "tools", "declare_finishing.py"), script)
        os.symlink(os.path.join(repo, "tools", "boringness.py"),
                   os.path.join(self.temp.name, "tools", "boringness.py"))
        for directory in ("mcp_server", "ingest", "analyze"):
            os.symlink(os.path.join(repo, directory), os.path.join(self.temp.name, directory),
                       target_is_directory=True)
        result = subprocess.run([sys.executable, script, "Song", "composition_accepted"],
                                input="", text=True, capture_output=True, cwd=self.temp.name,
                                env={**os.environ, "PYTHONPATH": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("interactive terminal", result.stderr)


class MinorFinishFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old_root = finisher.ROOT
        finisher.ROOT = self.temp.name
        self.addCleanup(setattr, finisher, "ROOT", self.old_root)
        self.old_rituals_root = rituals.ROOT
        rituals.ROOT = self.temp.name
        self.addCleanup(setattr, rituals, "ROOT", self.old_rituals_root)
        self.decisions = os.path.join(self.temp.name, "piece_decisions.json")

    def test_corrupt_decisions_warn_and_diagnosis_continues(self):
        from mcp_server import piece_context
        with open(self.decisions, "w") as fh:
            fh.write("{")
        project = {"path": "/music/Song.als", "tempo": 120, "track_count": 0,
                   "midi_track_names": [], "audio_track_names": [], "clip_count": 0,
                   "total_notes": 0, "clips": []}
        with mock.patch.object(piece_context, "_decision_path", return_value=self.decisions), \
             mock.patch.object(finisher, "run_detectors", return_value=[]), \
             self.assertWarnsRegex(RuntimeWarning, "piece_decisions"):
            result = finisher.whats_missing(project_dict=project)
        self.assertEqual(result["findings"], [])

    def test_deal_skips_ambiguous_project_stems(self):
        rituals.set_rituals_paths_for_test(os.path.join(self.temp.name, "rituals.json"), None)
        self.addCleanup(rituals.set_rituals_paths_for_test, None, None)
        projects = os.path.join(self.temp.name, "store", "projects")
        os.makedirs(projects)
        with open(os.path.join(projects, "_index.json"), "w") as fh:
            json.dump([{"id": "a", "name": "Song.als", "take_lane_notes": 2},
                       {"id": "b", "name": "Song.als", "take_lane_notes": 1},
                       {"id": "c", "name": "Unique.als", "take_lane_notes": 0}], fh)
        result = rituals.deal()
        self.assertEqual(result["dealt"], "Unique")

    def test_deal_skips_married_and_buried_resolved_paths_and_legacy_stems(self):
        projects = os.path.join(self.temp.name, "store", "projects")
        os.makedirs(projects)
        entries = [
            {"id": "married", "name": "Married.als", "path": "/music/Married.als",
             "take_lane_notes": 3},
            {"id": "buried", "name": "Buried.als", "path": "/music/Buried.als",
             "take_lane_notes": 2},
            {"id": "legacy", "name": "Legacy.als", "path": "/music/Legacy.als",
             "take_lane_notes": 1},
            {"id": "free", "name": "Free.als", "path": "/music/Free.als",
             "take_lane_notes": 0},
        ]
        with open(os.path.join(projects, "_index.json"), "w") as fh:
            json.dump(entries, fh)
        for entry in entries:
            with open(os.path.join(projects, entry["id"] + ".json"), "w") as fh:
                json.dump({"path": entry["path"], "clips": []}, fh)
        rituals.set_rituals_paths_for_test(os.path.join(self.temp.name, "rituals.json"),
                                           os.path.join(self.temp.name, "NOTES.md"))
        self.addCleanup(rituals.set_rituals_paths_for_test, None, None)
        with mock.patch.object(finisher, "whats_missing", return_value={"findings": []}):
            rituals.marry("Married")
        rituals.bury("Buried", "finished its work")
        with open(os.path.join(self.temp.name, "rituals.json")) as fh:
            ledger = json.load(fh)
        ledger["buried"].append({"project": "Legacy"})
        with open(os.path.join(self.temp.name, "rituals.json"), "w") as fh:
            json.dump(ledger, fh)
        self.assertEqual(rituals.deal()["dealt"], "Free")

    def test_marry_diagnoses_before_persisting_resolution(self):
        rituals.set_rituals_paths_for_test(os.path.join(self.temp.name, "rituals.json"), None)
        self.addCleanup(rituals.set_rituals_paths_for_test, None, None)
        record = {"path": "/music/Song.als", "clips": []}
        with mock.patch.object(finisher, "resolve_project", return_value=record), \
             mock.patch.object(finisher, "whats_missing", side_effect=ValueError("bad record")):
            with self.assertRaisesRegex(ValueError, "bad record"):
                rituals.marry("Song")
        self.assertFalse(os.path.exists(os.path.join(self.temp.name, "rituals.json")))

    def test_intent_fingerprint_ignores_edits_outside_finding_span(self):
        from mcp_server import piece_context
        project = {"path": "/music/Song.als", "tempo": 120, "track_count": 2,
                   "midi_track_names": ["Keys", "Bass"], "audio_track_names": [],
                   "clip_count": 2, "total_notes": 2, "clips": [
            {"name": "inside", "track": "Keys", "start": 0, "end": 8,
             "note_count": 1, "is_drum": False, "clip_source": "arrangement",
             "notes": [{"pitch": 60, "start": 0, "duration": 1, "velocity": 90}]},
            {"name": "outside", "track": "Bass", "start": 64, "end": 72,
             "note_count": 1, "is_drum": False, "clip_source": "arrangement",
             "notes": [{"pitch": 36, "start": 0, "duration": 1, "velocity": 90}]},
        ]}
        finding = {"detector": "flat_energy_run", "severity": "ask", "bars": [1, 2],
                   "evidence": "test"}
        with mock.patch.object(piece_context, "_decision_path", return_value=self.decisions), \
             mock.patch.object(finisher, "run_detectors", return_value=[finding]):
            finisher.whats_missing(project_dict=project,
                                   intended_findings=["flat_energy_run"])
            project["clips"][1]["notes"][0]["pitch"] = 40
            result = finisher.whats_missing(project_dict=project)
        self.assertEqual(result["findings"], [])


if __name__ == "__main__":
    unittest.main()
