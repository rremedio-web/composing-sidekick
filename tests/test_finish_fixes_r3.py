"""Batch 5 finish-fixes round 3 regression tests (reviewer probes in
.scratch/batch5/review3-probes/)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from mcp_server import finisher, rituals


def _streaminfo(sample_rate: int, samples: int) -> bytes:
    # FLAC STREAMINFO payload (34 bytes): block sizes, frame sizes, then
    # 20-bit rate | 3-bit channels-1 | 5-bit bps-1 | 36-bit total samples.
    info = bytearray(34)
    info[0:2] = (4096).to_bytes(2, "big")
    info[2:4] = (4096).to_bytes(2, "big")
    packed = (sample_rate << 44) | (0 << 41) | (15 << 36) | samples
    info[10:18] = packed.to_bytes(8, "big")
    return bytes(info)


def _block(kind: int, payload: bytes, last: bool) -> bytes:
    return bytes([(0x80 if last else 0) | kind]) + len(payload).to_bytes(3, "big") + payload


class _BounceLedgerBase(unittest.TestCase):
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
        with open(self.rituals_path, "w") as fh:
            json.dump({"finishing": {"/music/Song.als": {
                "started_at": "2000-01-01T00:00:00+00:00"}}}, fh)

    def write(self, name: str, data: bytes) -> str:
        path = os.path.join(self.temp.name, name)
        with open(path, "wb") as fh:
            fh.write(data)
        os.utime(path, (1900000000, 1900000000))
        return path


class FlacMetadataWalkTests(_BounceLedgerBase):
    def test_synthetic_multi_block_flac_reads_duration(self):
        # Real encoder layout: STREAMINFO -> VORBIS_COMMENT -> PADDING (last), then a frame.
        from tests.test_review12h import _audio_offset, synthetic_flac
        header = (b"fLaC" + _block(0, _streaminfo(8000, 16000), False)
                  + _block(4, b"\x05\x00\x00\x00test\x00\x00\x00\x00", False)
                  + _block(1, b"\0" * 64, True))
        # Genuine frames (a bare sync stub no longer passes the frame walk).
        stream = synthetic_flac(rate=8000, channels=1, total=16000, blocksize=4096)
        path = self.write("multi.flac", header + stream[_audio_offset(stream):])
        self.assertAlmostEqual(rituals.confirm_bounce("Song", path)["duration_s"], 2.0)

    def test_ffmpeg_multi_block_flac_reads_duration(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("ffmpeg not installed")
        path = os.path.join(self.temp.name, "ffmpeg.flac")
        subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                        "anullsrc=r=44100:cl=mono", "-t", "1", "-c:a", "flac", path],
                       check=True, timeout=60)
        with open(path, "rb") as fh:
            data = fh.read()
        # ffmpeg writes more than one metadata block (STREAMINFO is not last).
        self.assertEqual(data[4] & 0x80, 0)
        os.utime(path, (1900000000, 1900000000))
        self.assertAlmostEqual(rituals.confirm_bounce("Song", path)["duration_s"], 1.0, places=2)

    def test_truncated_multi_block_flac_errors_cleanly(self):
        header = (b"fLaC" + _block(0, _streaminfo(8000, 16000), False)
                  + _block(1, b"\0" * 64, True))
        for label, data in (("inside padding", header[:-20]),
                            ("no frame after metadata", header),
                            ("bad sync", header + b"\xff\x00\x00\x00"),
                            ("no last block", b"fLaC" + _block(0, _streaminfo(8000, 16000), False))):
            with self.subTest(label):
                path = self.write("trunc.flac", data)
                with self.assertRaisesRegex(ValueError, "duration could not be read"):
                    rituals.confirm_bounce("Song", path)

    def test_mmap_closed_on_error_paths(self):
        import mmap as _mmap
        opened = []
        real = _mmap.mmap

        def tracking(*args, **kwargs):
            mapped = real(*args, **kwargs)
            opened.append(mapped)
            return mapped

        path = self.write("bad.flac", b"fLaC" + _block(0, _streaminfo(8000, 16000), False))
        with mock.patch.object(rituals.mmap, "mmap", side_effect=tracking), \
             mock.patch.object(rituals, "_mp3_frame_offset", side_effect=RuntimeError("boom")):
            with self.assertRaises(ValueError):
                rituals.confirm_bounce("Song", path)
            mp3 = self.write("bad.mp3", b"\xff\xfb\x90\x00" + b"\0" * 64)
            with self.assertRaises(RuntimeError):
                rituals.confirm_bounce("Song", mp3)
        self.assertEqual(len(opened), 2)
        self.assertTrue(all(mapped.closed for mapped in opened))


def _clip(start, end, notes, loop_start, loop_end, loop_on, audible_start, audible_end,
          regions=None, track="Keys", start_marker=None):
    # Shape of an ingest.parse_als arrangement MidiClip (to_dict): note times are
    # clip-content beats; loop_* is the brace (start/end markers when loop is off);
    # audible_* / audible_regions are what _parse_loop says one pass plays;
    # start_marker is the raw LoopStart + StartRelative of a loop-on clip (these
    # fixtures' passes start where their audible range / first region starts).
    if loop_on and start_marker is None and loop_start is not None:
        start_marker = regions[0][0] if regions else audible_start
    return {"start_marker": start_marker if loop_on else None, "name": "c", "track": track, "start": start, "end": end,
            "note_count": len(notes), "is_drum": False, "clip_source": "arrangement",
            "is_sidekick": False, "loop_start": loop_start, "loop_end": loop_end,
            "loop_on": loop_on, "audible_start": audible_start, "audible_end": audible_end,
            "audible_regions": regions,
            "notes": [{"pitch": p, "start": t, "duration": 0.5, "velocity": 100,
                       "enabled": True} for p, t in notes]}


class EndingSeedPlaybackTests(unittest.TestCase):
    """Ports .scratch/batch5/review3-probes/probe_seeds.py cases A-E2."""

    @staticmethod
    def seed(clips, bars):
        return [(n["start"], n["pitch"])
                for n in finisher._ending_seed_notes({"clips": clips}, bars)]

    def test_a_looped_clip_repeats_into_final_bar(self):
        clip = _clip(0, 16, [(60, 0), (62, 2)], 0, 4, True, 0, 4)
        self.assertEqual(self.seed([clip], [4, 4]), [(0.0, 60), (2.0, 62)])

    def test_b_content_before_brace_is_not_played(self):
        clip = _clip(0, 4, [(50, 1.0), (60, 4.0)], 4, 8, True, 4, 8)
        self.assertEqual(self.seed([clip], [1, 1]), [(0.0, 60)])

    def test_c_note_at_loop_end_never_sounds(self):
        clip = _clip(0, 8, [(60, 0.0), (61, 8.0)], 0, 8, True, 0, 8)
        self.assertEqual(self.seed([clip], [1, 2]), [(0.0, 60)])

    def test_d_mixed_looped_and_unlooped_clips(self):
        wood = _clip(0, 32, [(42, 0), (42, 1), (42, 2), (42, 3)], 0, 4, True, 0, 4,
                     track="wood")
        bass = _clip(24, 32, [(36, 0), (38, 4)], 0, 8, False, 0, 8, track="bass")
        self.assertEqual(self.seed([wood, bass], [7, 8]),
                         sorted([(float(b), 42) for b in range(8)] + [(0.0, 36), (4.0, 38)]))

    def test_e_start_marker_inside_short_pass(self):
        clip = _clip(0, 4, [(60, 0.0), (64, 4.0)], 0, 8, True, 4, 8)
        self.assertEqual(self.seed([clip], [1, 1]), [(0.0, 64)])

    def test_e2_start_marker_then_wrap_to_loop_start(self):
        clip = _clip(0, 8, [(60, 0.0), (64, 4.0)], 0, 8, True, 4, 8)
        self.assertEqual(self.seed([clip], [1, 2]), [(0.0, 64), (4.0, 60)])

    def test_wrapping_pass_follows_audible_regions(self):
        # parse_als: brace [0,8), StartRelative 6, 4-beat pass -> [[6,8],[0,2]].
        clip = _clip(0, 4, [(70, 6.0), (71, 1.0), (72, 3.0)], 0, 8, True, 0, 8,
                     regions=[[6.0, 8.0], [0.0, 2.0]])
        self.assertEqual(self.seed([clip], [1, 1]), [(0.0, 70), (3.0, 71)])

    def test_trimmed_loop_off_clip_seeds_from_played_part(self):
        # loop off: markers [4,12), 8-beat pass -> audible [4,12); content 0 is trimmed.
        clip = _clip(0, 8, [(50, 0.0), (60, 4.0), (62, 8.0)], 4, 12, False, 4, 12)
        self.assertEqual(self.seed([clip], [1, 2]), [(0.0, 60), (4.0, 62)])

    def test_note_durations_stop_at_the_loop_end(self):
        clip = _clip(0, 8, [], 0, 4, True, 0, 4)
        clip["notes"] = [{"pitch": 60, "start": 3.0, "duration": 2.0, "velocity": 100,
                          "enabled": True}]
        notes = finisher._ending_seed_notes({"clips": [clip]}, [2, 2])
        self.assertEqual([(n["start"], n["duration"]) for n in notes], [(3.0, 1.0)])

    def test_missing_loop_fields_keep_the_instruction(self):
        clip = _clip(0, 8, [(60, 0.0)], None, None, True, None, None)
        finding = {"detector": "uncommitted_ending", "severity": "ask", "bars": [1, 2],
                   "evidence": "MIDI only"}
        proposal = finisher.propose([finding], project_dict={"clips": [clip]})[0]
        self.assertIsNone(proposal["command"])
        self.assertIn("select the project's last-section MIDI clip", proposal["strategy"])


class LiveApiRefusedIdentityTests(unittest.TestCase):
    """Ports .scratch/batch5/review3-probes/probe_liveapi_refused.py: a SAVED Set
    whose Producer Pal has Live API access off. Every Live call goes to a fake."""

    def setUp(self):
        import asyncio
        from contextlib import asynccontextmanager
        from mcp_server import pp_session as pp, piece_context
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        rituals.set_rituals_paths_for_test(os.path.join(self.temp.name, "rituals.json"),
                                           os.path.join(self.temp.name, "NOTES.md"))
        self.addCleanup(rituals.set_rituals_paths_for_test, None, None)
        self.decisions = os.path.join(self.temp.name, "piece_decisions.json")
        self.calls = []
        calls = self.calls

        def resp(obj):
            return pp.PPResponse(json.dumps({"result": obj}), obj, [])

        # Shape of a PP 2.2 arrangement clip read with notes + timing.
        clip = {"id": "7", "name": "loop", "arrangementStart": "1|1",
                "arrangementLength": "8bar", "start": "1|1", "end": "9|1", "looping": False,
                "notes": [{"p": 60, "t": i * 1.0, "d": 0.5, "v": 90} for i in range(32)]}
        # The overview claims a filePath; with Live API refused it is NOT verified.
        overview = {"name": "Tonight", "tempo": 120, "filePath": "/claimed/by/overview.als",
                    "tracks": [{"trackIndex": 0, "name": "Bass", "type": "midi"}]}

        class Session:
            async def call(self, name, args=None):
                pp._selftest_schema_check(name, args)
                calls.append(name)
                if name == "ppal-live-api":
                    raise RuntimeError("Producer Pal tool error: Live API access is disabled")
                if name == "ppal-read-live-set":
                    return resp(overview)
                if name == "ppal-read-track":
                    return resp({"trackIndex": 0, "name": "Bass", "arrangementClips": [clip]})
                raise AssertionError(name)

        session = Session()

        @asynccontextmanager
        async def operation(*_args, **_kwargs):
            yield session

        for patcher in (mock.patch.object(pp, "operation_session", operation),
                        mock.patch.object(pp, "run_coro_blocking",
                                          side_effect=lambda coro: asyncio.run(coro)),
                        mock.patch.object(piece_context, "_decision_path",
                                          return_value=self.decisions)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_overview_file_path_is_not_verified_identity(self):
        import asyncio
        from mcp_server import live_write
        tracks = asyncio.run(live_write.read_arrangement_tracks())
        self.assertIsNone(tracks["file_path"])
        self.assertFalse(tracks["identity_verified"])
        project = finisher.project_from_live_tracks(tracks)
        self.assertEqual(project["path"], "live:Tonight")

    def test_diagnosis_works_but_finishing_says_identity_unavailable(self):
        from mcp_server import server
        diagnosis = server.whats_missing("live")
        self.assertEqual(diagnosis["project"], "live:Tonight")
        for label, call in (("one_hour_close", lambda: rituals.one_hour_close("live")),
                            ("marry", lambda: rituals.marry("live")),
                            ("bury", lambda: rituals.bury("live", "x"))):
            with self.subTest(label):
                with self.assertRaises(ValueError) as caught:
                    call()
                self.assertIn("Set identity unavailable (Live API access off?)",
                              str(caught.exception))
                self.assertNotIn("save the Set first", str(caught.exception))

    def test_intent_refused_without_verified_identity(self):
        from mcp_server import server
        with self.assertRaisesRegex(ValueError, r"Set identity unavailable \(Live API access off\?\)"):
            server.whats_missing("live", intended_findings=["flat_energy_run"])
        self.assertFalse(os.path.exists(self.decisions))


class IntentGuardInFinisherTests(unittest.TestCase):
    def test_finisher_refuses_intent_for_unsaved_live_project(self):
        from mcp_server import piece_context
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        decisions = os.path.join(temp.name, "piece_decisions.json")
        # Shape produced by finisher.project_from_live_tracks for an unsaved Set.
        live = {"path": "live:Tonight", "tempo": 120, "track_count": 0,
                "midi_track_names": [], "audio_track_names": [], "clip_count": 0,
                "total_notes": 0, "clips": [], "assumptions": []}
        finding = {"detector": "flat_energy_run", "severity": "ask", "bars": [1, 32],
                   "evidence": "is this intended?"}
        with mock.patch.object(piece_context, "_decision_path", return_value=decisions), \
             mock.patch.object(finisher, "run_detectors", return_value=[finding]):
            with self.assertRaisesRegex(ValueError, "save the Set first"):
                finisher.whats_missing(project_dict=live, intended_findings=["flat_energy_run"])
            # Plain diagnosis of the unsaved Set still works.
            self.assertEqual(finisher.whats_missing(project_dict=live)["project"], "live:Tonight")
        self.assertFalse(os.path.exists(decisions))


class IntentInstanceTargetingTests(unittest.TestCase):
    """Ports .scratch/batch5/review3-probes/probe_instances.py onto a ballad-like
    reading: 69 BPM (doom world), two loop_with_ambitions spans (1-106, 121-264),
    two flat_energy_run spans (1-70, 123-264), b_section_missing, uncommitted_ending."""

    def setUp(self):
        from mcp_server import piece_context
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.decisions = os.path.join(self.temp.name, "piece_decisions.json")
        patcher = mock.patch.object(piece_context, "_decision_path", return_value=self.decisions)
        patcher.start()
        self.addCleanup(patcher.stop)

        def bar(i):
            density = 1.0 if i < 70 else (2.0 if i >= 122 else (0.3 if i % 2 else 1.4))
            return {"bar": i + 1, "density": density, "tracks": frozenset({"Pad"}),
                    "roles": ["pad"]}
        # Shape returned by finisher.read_structure (sections carry a signature).
        self.reading = {
            "tempo": 69.0, "total_bars": 264, "bars": [bar(i) for i in range(264)],
            "sections": [
                {"start_bar": 1, "end_bar": 106, "bars": 106, "density": 1.0,
                 "roles": ["pad"], "signature": ("Pad",)},
                {"start_bar": 107, "end_bar": 120, "bars": 14, "density": 0.9,
                 "roles": ["pad"], "signature": ("Pad", "Bass")},
                {"start_bar": 121, "end_bar": 264, "bars": 144, "density": 2.0,
                 "roles": ["pad"], "signature": ("Pad",)},
            ],
            "timeline": [], "comping_debris": 0, "tracks": ["Pad"],
        }
        read = mock.patch.object(finisher, "read_structure", side_effect=lambda _p: self.reading)
        read.start()
        self.addCleanup(read.stop)
        # Shape of a materialized parse_als project (clips carry notes).
        self.project = {"path": "/music/ballad.als", "tempo": 69.0, "clips": [
            {"name": "A", "track": "Pad", "start": 0, "end": 280, "note_count": 1,
             "is_drum": False, "clip_source": "arrangement",
             "notes": [{"pitch": 48, "start": 0, "duration": 4, "velocity": 80,
                        "enabled": True}]},
            {"name": "B", "track": "Pad", "start": 480, "end": 1056, "note_count": 1,
             "is_drum": False, "clip_source": "arrangement",
             "notes": [{"pitch": 50, "start": 0, "duration": 4, "velocity": 80,
                        "enabled": True}]},
        ]}

    def asks(self, result):
        return [(f["detector"], tuple(f["bars"]) if f["bars"] else None)
                for f in result["findings"] if f["severity"] == "ask"]

    def run_wm(self, marks=None):
        return finisher.whats_missing(project_dict=json.loads(json.dumps(self.project)),
                                      intended_findings=marks)

    def rows(self):
        if not os.path.exists(self.decisions):
            return []
        with open(self.decisions) as fh:
            return json.load(fh)["decisions"]

    def test_baseline_has_two_spans_per_detector(self):
        asks = self.asks(self.run_wm())
        self.assertIn(("loop_with_ambitions", (1, 106)), asks)
        self.assertIn(("loop_with_ambitions", (121, 264)), asks)
        self.assertIn(("flat_energy_run", (1, 70)), asks)
        self.assertIn(("flat_energy_run", (123, 264)), asks)
        self.assertIn(("b_section_missing", None), asks)

    def test_bare_name_that_fired_twice_is_refused_with_instances(self):
        with self.assertRaises(ValueError) as caught:
            self.run_wm(["flat_energy_run"])
        message = str(caught.exception)
        self.assertIn("flat_energy_run@1-70", message)
        self.assertIn("flat_energy_run@123-264", message)
        self.assertIn("flat_energy_run:all", message)
        self.assertEqual(self.rows(), [])

    def test_instance_forms_target_one_span(self):
        for mark in ("flat_energy_run@123-264",
                     {"detector": "flat_energy_run", "bars": [123, 264]}):
            with self.subTest(mark=mark):
                if os.path.exists(self.decisions):
                    os.unlink(self.decisions)
                result = self.run_wm([mark])
                self.assertEqual(result["intent_recorded"], [
                    {"detector": "flat_energy_run", "bars": [123, 264], "status": "recorded"}])
                for asks in (self.asks(result), self.asks(self.run_wm())):
                    self.assertNotIn(("flat_energy_run", (123, 264)), asks)
                    self.assertIn(("flat_energy_run", (1, 70)), asks)
                self.assertEqual(len(self.rows()), 1)

    def test_all_form_marks_every_instance_and_remark_is_a_noop(self):
        result = self.run_wm(["flat_energy_run:all"])
        self.assertEqual([(r["bars"], r["status"]) for r in result["intent_recorded"]],
                         [([1, 70], "recorded"), ([123, 264], "recorded")])
        self.assertNotIn("flat_energy_run", {d for d, _ in self.asks(self.run_wm())})
        again = self.run_wm(["flat_energy_run@1-70", "flat_energy_run:all"])
        self.assertEqual({r["status"] for r in again["intent_recorded"]}, {"already_intended"})
        self.assertEqual(len(again["intent_recorded"]), 2)
        self.assertEqual(len(self.rows()), 2)

    def test_unknown_span_is_refused_with_instances(self):
        with self.assertRaisesRegex(ValueError, r"flat_energy_run@1-70.*flat_energy_run@123-264"):
            self.run_wm(["flat_energy_run@2-70"])
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.run_wm(["no_breathe:all"])
        self.assertEqual(self.rows(), [])

    def test_loop_with_ambitions_instance_silences_its_span_and_b_section(self):
        result = self.run_wm(["loop_with_ambitions@1-106"])
        for asks in (self.asks(result), self.asks(self.run_wm())):
            self.assertNotIn(("loop_with_ambitions", (1, 106)), asks)
            self.assertIn(("loop_with_ambitions", (121, 264)), asks)
            self.assertNotIn(("b_section_missing", None), asks)

    def test_b_section_missing_silences_only_itself(self):
        result = self.run_wm(["b_section_missing"])
        for asks in (self.asks(result), self.asks(self.run_wm())):
            self.assertNotIn(("b_section_missing", None), asks)
            self.assertIn(("loop_with_ambitions", (1, 106)), asks)
            self.assertIn(("loop_with_ambitions", (121, 264)), asks)

    def test_edit_inside_one_span_revives_only_that_instance(self):
        self.run_wm(["flat_energy_run:all"])
        self.project["clips"][0]["notes"][0]["velocity"] = 79
        asks = self.asks(self.run_wm())
        self.assertIn(("flat_energy_run", (1, 70)), asks)
        self.assertNotIn(("flat_energy_run", (123, 264)), asks)


class DealAndGraveyardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for module in (finisher, rituals):
            old = module.ROOT
            module.ROOT = self.temp.name
            self.addCleanup(setattr, module, "ROOT", old)
        projects = os.path.join(self.temp.name, "store", "projects")
        os.makedirs(projects)
        # Shape of store/projects/_index.json entries.
        entries = [
            {"id": "married", "name": "Married.als", "path": "/music/Married.als",
             "take_lane_notes": 3},
            {"id": "buried", "name": "Buried.als", "path": "/music/Buried.als",
             "take_lane_notes": 2},
            {"id": "free", "name": "Free.als", "path": "/music/Free.als",
             "take_lane_notes": 0},
        ]
        with open(os.path.join(projects, "_index.json"), "w") as fh:
            json.dump(entries, fh)
        for entry in entries:
            with open(os.path.join(projects, entry["id"] + ".json"), "w") as fh:
                json.dump({"path": entry["path"], "clips": []}, fh)
        self.rituals_path = os.path.join(self.temp.name, "rituals.json")
        self.notes_path = os.path.join(self.temp.name, "NOTES.md")
        rituals.set_rituals_paths_for_test(self.rituals_path, self.notes_path)
        self.addCleanup(rituals.set_rituals_paths_for_test, None, None)

    def test_resolved_so_far_counts_stem_and_path_rows_once(self):
        # Shape of store/rituals.json: a legacy stem row and a resolved-path row
        # for the same project, plus a repeated burial.
        with open(self.rituals_path, "w") as fh:
            json.dump({"schema_version": 1, "finishing": {},
                       "married": [{"project": "Married", "at": "2026-01-01T00:00:00+00:00"},
                                   {"project": "/music/Married.als",
                                    "at": "2026-02-01T00:00:00+00:00"}],
                       "buried": [{"project": "/music/Buried.als", "eulogy": "a"},
                                  {"project": "/music/Buried.als", "eulogy": "b"}]}, fh)
        result = rituals.deal()
        self.assertEqual(result["dealt"], "Free")
        self.assertEqual(result["resolved_so_far"], 2)

    def test_graveyard_line_names_the_project_and_its_path(self):
        rituals.bury("Buried", "finished its work")
        with open(self.notes_path) as fh:
            text = fh.read()
        line = [row for row in text.splitlines() if row.startswith("- ")][0]
        self.assertIn("**Buried**", line)
        self.assertIn("/music/Buried.als", line)
        self.assertIn("finished its work", line)


class FinishingProfileDocsTests(unittest.TestCase):
    def test_finishing_tool_rows_match_profile_registry(self):
        import re
        from pathlib import Path
        from mcp_server import profiles
        root = Path(__file__).resolve().parents[1]
        table = (root / "config" / "claude_mcp.md").read_text()
        for tool in ("bury_or_marry", "version_courage", "one_hour_close", "confirm_bounce",
                     "declare_finishing_state", "three_endings", "no_new_notes"):
            with self.subTest(tool=tool):
                row = next(line for line in table.splitlines()
                           if line.startswith("| `%s` |" % tool))
                documented = set(re.findall(r"`([a-z]+)`", row.split("|")[2]))
                exposed = {name for name, tools in profiles.PROFILE_TOOL_REGISTRY.items()
                           if tool in tools and name != "all"}
                self.assertEqual(documented, exposed)
        playbook = (root / "docs" / "CURSOR_CHAT_PLAYBOOK.md").read_text()
        self.assertIn("`session` profile only", playbook)


if __name__ == "__main__":
    unittest.main()
