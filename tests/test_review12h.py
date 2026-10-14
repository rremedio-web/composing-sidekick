"""12-hour re-review (2026-09-26) P2 findings — ports of the runnable probes in
.scratch/review-12h-20260926/probes.py. Temporary state only; no Live."""
import json
import os
import tempfile
import unittest
import wave
from unittest import mock

from mcp_server import finisher, piece_context, rituals


def _live_record(path: str) -> dict:
    # Shape produced by finisher.project_from_live_tracks for a saved Set.
    return {"path": path, "tempo": 120, "track_count": 0,
            "midi_track_names": [], "audio_track_names": [], "clip_count": 0,
            "total_notes": 0, "audio_clip_count": 0, "notes_skipped": 0,
            "skip_detail": {"hidden_past_loop": 0}, "assumptions": [], "clips": []}


class UncataloguedFinishingKeyTests(unittest.TestCase):
    """Finding 4: the key one_hour_close('live') returns must resolve again
    for a saved Set that was never ingested into the catalog."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="r12h-key-")
        self.addCleanup(self.temp.cleanup)
        root = self.temp.name
        projects = os.path.join(root, "store", "projects")
        os.makedirs(projects)
        with open(os.path.join(projects, "_index.json"), "w") as fh:
            json.dump([], fh)
        patcher = mock.patch.object(finisher, "ROOT", root)
        patcher.start()
        self.addCleanup(patcher.stop)
        decisions = mock.patch.object(piece_context, "_decision_path",
                                      return_value=os.path.join(root, "decisions.json"))
        decisions.start()
        self.addCleanup(decisions.stop)
        rituals.set_rituals_paths_for_test(os.path.join(root, "rituals.json"),
                                           os.path.join(root, "NOTES.md"))
        self.addCleanup(rituals.set_rituals_paths_for_test, None, None)
        # Not on disk: only the finishing ledger vouches for it.
        self.song = os.path.join(root, "Song.als")

    def _open(self):
        with mock.patch.object(finisher, "_read_live_project",
                               return_value=_live_record(self.song)):
            return rituals.one_hour_close("live")

    def _wav(self, name="bounce.wav"):
        path = os.path.join(self.temp.name, name)
        with wave.open(path, "wb") as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(8000)
            out.writeframes(b"\0\0" * 16000)
        os.utime(path, (1900000000, 1900000000))
        return path

    def test_returned_key_resolves_and_confirms_bounce(self):
        opened = self._open()
        self.assertEqual(opened["project"], self.song)
        self.assertEqual(finisher.resolve_project_key(opened["project"]), self.song)
        result = rituals.confirm_bounce(opened["project"], self._wav(), confirm=True)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["project"], self.song)

    def test_returned_key_accepts_draft_declaration(self):
        opened = self._open()
        result = rituals.declare_finishing_state(opened["project"], "complete_draft")
        self.assertEqual(result["project"], self.song)
        ledger = json.load(open(os.path.join(self.temp.name, "rituals.json")))
        self.assertEqual(len(ledger["finishing"][self.song]["model_states"]), 1)

    def test_existing_uncatalogued_als_is_refused(self):
        # b9 review #4: a file on disk is not a vouched project. Only the
        # exact key one_hour_close recorded (or the catalog) resolves.
        path = os.path.join(self.temp.name, "Saved.als")
        open(path, "wb").close()
        with self.assertRaisesRegex(ValueError, "no materialized project matches.*project='live'"):
            finisher.resolve_project_key(path)
        with mock.patch.object(finisher, "_parse_saved_set") as parse:
            with self.assertRaisesRegex(ValueError, "no materialized project matches"):
                rituals.marry(path)
        parse.assert_not_called()

    def test_other_artists_set_path_is_refused(self):
        other = os.path.join(self.temp.name, "ArtistSet-Someone", "Someone-Track.als")
        os.makedirs(os.path.dirname(other))
        with open(other, "wb") as fh:
            fh.write(b"x")
        for call in (finisher.resolve_project_key, rituals.one_hour_close, rituals.marry):
            with self.subTest(call=call.__name__):
                with self.assertRaisesRegex(ValueError, "no materialized project matches"):
                    call(other)
        ledger_path = os.path.join(self.temp.name, "rituals.json")
        if os.path.exists(ledger_path):
            with open(ledger_path) as fh:
                self.assertNotIn(other, json.load(fh).get("finishing", {}))

    def test_recorded_key_on_disk_still_resolves_and_marries(self):
        opened = self._open()
        open(self.song, "wb").close()
        parsed = _live_record(self.song)
        with mock.patch.object(finisher, "_parse_saved_set", return_value=parsed) as parse:
            result = rituals.marry(opened["project"])
        parse.assert_called_once_with(self.song)
        self.assertEqual(result["married"], self.song)

    def test_unreadable_saved_set_is_a_refusal_not_a_traceback(self):
        opened = self._open()
        with open(self.song, "wb") as fh:
            fh.write(b"not a gzipped Live Set")
        with self.assertRaisesRegex(ValueError, "not a readable Live Set"):
            rituals.marry(opened["project"])

    def test_ledger_only_key_cannot_be_married_without_the_file(self):
        opened = self._open()
        with self.assertRaisesRegex(ValueError, "not on disk"):
            rituals.marry(opened["project"])

    def test_unknown_names_and_unvouched_paths_still_refused(self):
        self._open()
        for key in ("Song", "Song.als", os.path.join(self.temp.name, "Other.als"),
                    "relative/Song.als", self.song + ".bak"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "no materialized project matches"):
                    finisher.resolve_project_key(key)

    def test_ambiguous_catalog_names_still_refused(self):
        projects = os.path.join(self.temp.name, "store", "projects")
        with open(os.path.join(projects, "_index.json"), "w") as fh:
            json.dump([{"id": "a", "name": "song.als", "path": "/one/song.als"},
                       {"id": "b", "name": "song.als", "path": "/two/song.als"}], fh)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            finisher.resolve_project_key("song")


class CatalogKeyVariantTests(unittest.TestCase):
    """b9 review #4: a case variant, symlink or .ALS spelling of a catalogued
    Set never becomes a second finishing key (no ledger fork)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="r12h-variant-")
        self.addCleanup(self.temp.cleanup)
        root = self.temp.name
        projects = os.path.join(root, "store", "projects")
        os.makedirs(projects)
        sets = os.path.join(root, "Sets")
        os.makedirs(sets)
        self.song = os.path.join(sets, "Mine.als")
        open(self.song, "wb").close()
        with open(os.path.join(projects, "_index.json"), "w") as fh:
            json.dump([{"id": "mine", "name": "Mine.als", "path": self.song}], fh)
        with open(os.path.join(projects, "mine.json"), "w") as fh:
            json.dump(_live_record(self.song), fh)
        for patcher in (mock.patch.object(finisher, "ROOT", root),
                        mock.patch.object(piece_context, "_decision_path",
                                          return_value=os.path.join(root, "decisions.json"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ledger = os.path.join(root, "rituals.json")
        rituals.set_rituals_paths_for_test(self.ledger, os.path.join(root, "NOTES.md"))
        self.addCleanup(rituals.set_rituals_paths_for_test, None, None)
        self.link = os.path.join(root, "link.als")
        os.symlink(self.song, self.link)

    def test_catalogued_set_keys_by_its_catalog_path(self):
        self.assertEqual(finisher.resolve_project_key("Mine"), self.song)
        self.assertEqual(finisher.resolve_project_key(self.song), self.song)

    def test_variants_do_not_create_a_new_key(self):
        variants = [self.song.replace("/Sets/", "/SETS/"), self.song[:-4] + ".ALS",
                    self.link]
        with mock.patch.object(finisher, "whats_missing",
                               return_value={"findings": [], "proposals": []}):
            rituals.one_hour_close("Mine")
            for variant in variants:
                with self.subTest(variant=variant):
                    with self.assertRaisesRegex(ValueError, "no materialized project matches"):
                        finisher.resolve_project_key(variant)
                    with self.assertRaises(ValueError):
                        rituals.one_hour_close(variant)
        with open(self.ledger) as fh:
            keys = set(json.load(fh)["finishing"])
        self.assertEqual(keys, {self.song})


def _ending_project(path: str) -> dict:
    """The probe's one-clip project: a 16-beat brace looped over 64 bars."""
    return {"path": path, "tempo": 120, "track_count": 1, "clip_count": 1,
            "total_notes": 2, "midi_track_names": ["Lead"], "audio_track_names": [],
            "clips": [{"name": "A", "track": "Lead", "start": 0, "end": 256,
                       "note_count": 2, "clip_source": "arrangement", "is_drum": False,
                       "loop_on": True, "loop_start": 0, "loop_end": 16,
                       "start_marker": 0, "disabled": False,
                       "notes": [{"pitch": 60, "start": 0, "duration": 2,
                                  "velocity": 80, "enabled": True},
                                 {"pitch": 72, "start": 8, "duration": 2,
                                  "velocity": 80, "enabled": True}]}]}


class IntentFollowsPlaybackTests(unittest.TestCase):
    """Finding 3: an intended finding is bound to what actually plays in its
    span; rotating or deactivating that material re-asks."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="r12h-intent-")
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.object(piece_context, "_decision_path",
                                    return_value=os.path.join(self.temp.name, "d.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.project = _ending_project(os.path.join(self.temp.name, "Song.als"))

    @staticmethod
    def raised(project):
        return [f["detector"] for f in finisher.whats_missing(project_dict=project)["findings"]]

    def mark(self, project):
        self.assertIn("uncommitted_ending", self.raised(project))
        finisher.whats_missing(project_dict=project, intended_findings="uncommitted_ending")
        self.assertNotIn("uncommitted_ending", self.raised(project))

    def test_rotating_the_start_marker_re_asks(self):
        import copy
        self.mark(self.project)
        rotated = copy.deepcopy(self.project)
        rotated["clips"][0]["start_marker"] = 8
        self.assertNotEqual(
            [n["pitch"] for n in finisher._ending_seed_notes(self.project, [57, 64])][:4],
            [n["pitch"] for n in finisher._ending_seed_notes(rotated, [57, 64])][:4])
        self.assertIn("uncommitted_ending", self.raised(rotated))

    def test_deactivating_the_clip_re_asks(self):
        import copy
        self.mark(self.project)
        disabled = copy.deepcopy(self.project)
        disabled["clips"][0]["disabled"] = True
        self.assertIn("uncommitted_ending", self.raised(disabled))

    def test_unchanged_rematerialization_keeps_the_intent(self):
        import copy
        self.mark(self.project)
        again = json.loads(json.dumps(copy.deepcopy(self.project)))
        again["clips"][0]["start_marker"] = 0.0     # re-parsed float spelling
        self.assertNotIn("uncommitted_ending", self.raised(again))
        # A marker exactly on LoopStart is the default phase, however stored.
        self.assertEqual(finisher._intent_fingerprints(self.project, "uncommitted_ending",
                                                       [57, 64]),
                         finisher._intent_fingerprints(again, "uncommitted_ending",
                                                       [57, 64]))

    def test_rotated_marker_recorded_then_unchanged_stays_intended(self):
        import copy
        rotated = copy.deepcopy(self.project)
        rotated["clips"][0]["start_marker"] = 8
        self.mark(rotated)
        self.assertNotIn("uncommitted_ending", self.raised(copy.deepcopy(rotated)))
        self.assertIn("uncommitted_ending", self.raised(self.project))

    def test_legacy_row_maps_missing_fields_to_default_playback(self):
        # A row written before playback was fingerprinted: its hash covered
        # the clips without start_marker/disabled.
        import copy
        import hashlib
        clips = [{k: v for k, v in c.items() if k not in ("start_marker", "disabled")}
                 for c in self.project["clips"]]
        legacy = hashlib.sha256(json.dumps(
            {"detector": "uncommitted_ending", "bars": [57, 64], "tempo": 120,
             "clips": clips}, sort_keys=True, default=str,
            separators=(",", ":")).encode()).hexdigest()
        piece_context.record_decision(self.project["path"], {
            "kind": "protect", "source": "model_on_request",
            "finding": "uncommitted_ending", "material_fingerprint": legacy,
            "finding_status": "intended"})
        self.assertNotIn("uncommitted_ending", self.raised(self.project))
        rotated = copy.deepcopy(self.project)
        rotated["clips"][0]["start_marker"] = 8
        self.assertIn("uncommitted_ending", self.raised(rotated))


def _live_tracks(path: str, first_start=None, muted=False, start="1|1", end="5|1") -> dict:
    """read_arrangement_tracks shape (port of .scratch/review-b9-probes/p3live.py).
    Producer Pal omits firstStart when it equals start."""
    clip = {"id": "7", "arrangementStart": "1|1", "arrangementLength": "64bar",
            "start": start, "end": end, "length": "4bar", "looping": True,
            "notes": "[{p:60,t:0,d:2,v:80},{p:72,t:8,d:2,v:80}]"}
    if first_start:
        clip["firstStart"] = first_start
    if muted:
        clip["muted"] = True
    return {"file_path": path, "tempo": 120, "identity_verified": True,
            "tracks": [{"name": "Lead", "type": "midi", "arrangement_clips": [clip]}]}


class LiveIntentPlaybackTests(unittest.TestCase):
    """b9 review #3 (live path): project='live' records carry the start
    marker and deactivation, so the intent fingerprint re-asks after a
    rotated marker or a deactivated clip in the open Set."""

    PRE_FIX_KEYS = {"name", "track", "start", "end", "note_count", "is_drum",
                    "clip_source", "notes"}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="r12h-live-intent-")
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.object(piece_context, "_decision_path",
                                    return_value=os.path.join(self.temp.name, "d.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = os.path.join(self.temp.name, "Song.als")

    def record(self, **kw):
        return finisher.project_from_live_tracks(_live_tracks(self.path, **kw))

    @staticmethod
    def raised(project):
        return [f["detector"] for f in finisher.whats_missing(project_dict=project)["findings"]]

    def mark(self, project):
        self.assertIn("uncommitted_ending", self.raised(project))
        finisher.whats_missing(project_dict=project, intended_findings="uncommitted_ending")
        self.assertNotIn("uncommitted_ending", self.raised(project))

    def test_live_clip_carries_playback_fields(self):
        base = self.record()["clips"][0]
        self.assertEqual(base["notes"][0]["pitch"], 60)
        self.assertEqual((base["start_marker"], base["disabled"]), (0.0, False))
        self.assertEqual(self.record(first_start="3|1")["clips"][0]["start_marker"], 8.0)
        self.assertTrue(self.record(muted=True)["clips"][0]["disabled"])
        braced = finisher.project_from_live_tracks(
            _live_tracks(self.path, start="2|1", end="6|1"))["clips"][0]
        self.assertEqual((braced["loop_start"], braced["start_marker"]), (4.0, 4.0))

    def test_rotated_start_marker_re_asks(self):
        self.mark(self.record())
        self.assertIn("uncommitted_ending", self.raised(self.record(first_start="3|1")))
        self.assertNotIn("uncommitted_ending", self.raised(self.record()))

    def test_deactivated_clip_re_asks(self):
        self.mark(self.record())
        self.assertIn("uncommitted_ending", self.raised(self.record(muted=True)))
        deactivated = _live_tracks(self.path)
        deactivated["tracks"][0]["arrangement_clips"][0]["activated"] = False
        self.assertIn("uncommitted_ending",
                      self.raised(finisher.project_from_live_tracks(deactivated)))

    def test_default_playback_hashes_like_the_pre_fix_record(self):
        base = self.record()
        self.assertEqual(set(base["clips"][0]) - set(finisher._FINGERPRINT_EXCLUDED),
                         self.PRE_FIX_KEYS)

    def test_live_record_seeds_three_endings(self):
        """round-2 review: project='live' clips carry their loop brace, so the
        uncommitted_ending proposal ships real seed notes (was always [])."""
        project = self.record()
        seeds = finisher._ending_seed_notes(project, [57, 64])
        self.assertEqual([(n["pitch"], n["start"]) for n in seeds],
                         [(60, 0.0), (72, 8.0), (60, 16.0), (72, 24.0)])
        rotated = finisher._ending_seed_notes(self.record(first_start="3|1"), [57, 64])
        self.assertEqual([(n["pitch"], n["start"]) for n in rotated][:2],
                         [(72, 0.0), (60, 8.0)])
        proposals = finisher.whats_missing(project_dict=project)["proposals"]
        ending = [p for p in proposals if p["detector"] == "uncommitted_ending"][0]
        self.assertEqual(ending["command"]["tool"], "three_endings")
        self.assertTrue(ending["command"]["args"]["seed_notes"])

    def test_row_recorded_with_playback_unknown_keeps_matching(self):
        base = self.record()
        pre_fix = json.loads(json.dumps(base))
        pre_fix["clips"] = [{k: v for k, v in c.items() if k in self.PRE_FIX_KEYS}
                            for c in pre_fix["clips"]]
        self.mark(pre_fix)
        with open(os.path.join(self.temp.name, "d.json")) as fh:
            rows = json.load(fh)["decisions"]
        self.assertTrue(rows[-1].get("playback_unknown"))
        self.assertNotIn("uncommitted_ending", self.raised(base))


def _crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xff if crc & 0x80 else (crc << 1) & 0xff
    return crc


def _crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xffff if crc & 0x8000 else (crc << 1) & 0xffff
    return crc


def _utf8_number(value: int) -> bytes:
    if value < 0x80:
        return bytes([value])
    for count in range(2, 8):
        if value < 1 << (5 * count + 1):
            out = []
            for _ in range(count - 1):
                out.insert(0, 0x80 | (value & 0x3f))
                value >>= 6
            lead = (0xff << (8 - count)) & 0xff
            return bytes([lead | value]) + bytes(out)
    raise ValueError(value)


def synthetic_flac(rate=8000, channels=1, total=16000, blocksize=4096,
                   variable=False) -> bytes:
    """A genuinely valid FLAC stream (16-bit CONSTANT subframes), built
    independently of the reader under test."""
    info = bytearray(34)
    info[0:2] = blocksize.to_bytes(2, "big")
    info[2:4] = blocksize.to_bytes(2, "big")
    packed = (rate << 44) | ((channels - 1) << 41) | (15 << 36) | total
    info[10:18] = packed.to_bytes(8, "big")
    data = bytearray(b"fLaC" + bytes([0x00]) + (34).to_bytes(3, "big") + info
                     + bytes([0x81]) + (16).to_bytes(3, "big") + b"\0" * 16)
    number, done = 0, 0
    while done < total:
        size = min(blocksize, total - done)
        header = (bytes([0xff, 0xf9 if variable else 0xf8, (7 << 4) | 0,
                         ((channels - 1) << 4) | 0])
                  + _utf8_number(done if variable else number)
                  + (size - 1).to_bytes(2, "big"))
        header += bytes([_crc8(header)])
        frame = header + b"".join(b"\x00" + (1000 + ch).to_bytes(2, "big")
                                  for ch in range(channels))
        data += frame + _crc16(frame).to_bytes(2, "big")
        number, done = number + 1, done + size
    return bytes(data)


def _audio_offset(data: bytes) -> int:
    offset = 4
    while True:
        header = data[offset]
        offset += 4 + int.from_bytes(data[offset + 1:offset + 4], "big")
        if header & 0x80:
            return offset


class FlacCompletenessTests(unittest.TestCase):
    """Finding 2: a FLAC whose audio frames are missing or cut short is not
    a bounce, whatever STREAMINFO declares."""

    def setUp(self):
        import shutil
        self.shutil = shutil
        self.temp = tempfile.TemporaryDirectory(prefix="r12h-flac-")
        self.addCleanup(self.temp.cleanup)
        self.rituals_path = os.path.join(self.temp.name, "rituals.json")
        rituals.set_rituals_paths_for_test(self.rituals_path, None)
        self.addCleanup(rituals.set_rituals_paths_for_test, None, None)
        self.song = os.path.join(self.temp.name, "Song.als")
        with open(self.rituals_path, "w") as fh:
            json.dump({"finishing": {self.song: {
                "started_at": "2000-01-01T00:00:00+00:00"}}}, fh)

    def write(self, name, data):
        path = os.path.join(self.temp.name, name)
        with open(path, "wb") as fh:
            fh.write(data)
        os.utime(path, (1900000000, 1900000000))
        return path

    def encode(self, tool, seconds=5.0, channels=1):
        import subprocess
        binary = self.shutil.which(tool)
        if not binary:
            self.skipTest(tool + " not installed")
        path = os.path.join(self.temp.name, "%s-%d.flac" % (tool, channels))
        if tool == "ffmpeg":
            subprocess.run([binary, "-v", "error", "-y", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=%g" % seconds, "-ac", str(channels),
                            "-c:a", "flac", path], check=True, capture_output=True, timeout=60)
        else:
            wav = os.path.join(self.temp.name, "src-%d.wav" % channels)
            with wave.open(wav, "wb") as out:
                out.setnchannels(channels); out.setsampwidth(2); out.setframerate(44100)
                import math, struct
                frames = b"".join(struct.pack("<h", int(8000 * math.sin(i / 20.0))) * channels
                                  for i in range(int(44100 * seconds)))
                out.writeframes(frames)
            subprocess.run([binary, "-s", "-f", "-o", path, wav], check=True,
                           capture_output=True, timeout=60)
        with open(path, "rb") as fh:
            return fh.read()

    def confirm(self, path, confirm=False):
        return rituals.confirm_bounce(self.song, path, confirm=confirm)

    def assertRefused(self, data, name="broken.flac"):
        path = self.write(name, data)
        with self.assertRaisesRegex(ValueError, "FLAC|duration could not be read"):
            self.confirm(path, confirm=True)
        ledger = json.load(open(self.rituals_path))
        self.assertNotIn("bounce_confirmed", ledger["finishing"][self.song])

    def test_probe_truncated_after_two_frame_bytes_is_refused(self):
        data = self.encode("ffmpeg")
        self.assertAlmostEqual(self.confirm(self.write("full.flac", data))["duration_s"], 5.0)
        self.assertRefused(data[:_audio_offset(data) + 2])

    def test_tail_truncation_and_missing_middle_frames_are_refused(self):
        data = self.encode("ffmpeg")
        self.assertRefused(data[:-1], "short-by-one.flac")
        self.assertRefused(data[:len(data) // 2], "half.flac")
        start = _audio_offset(data)
        self.assertRefused(data[:start + 3000] + data[start + 9000:], "gap.flac")

    def test_ffmpeg_and_libflac_encodes_pass(self):
        for tool, channels in (("ffmpeg", 1), ("ffmpeg", 2), ("flac", 1), ("flac", 2)):
            with self.subTest(tool=tool, channels=channels):
                data = self.encode(tool, seconds=3.0, channels=channels)
                path = self.write("%s-%d.flac" % (tool, channels), data)
                self.assertAlmostEqual(self.confirm(path)["duration_s"], 3.0, places=2)

    def test_synthetic_valid_stream_passes_and_its_truncations_fail(self):
        data = synthetic_flac()
        self.assertAlmostEqual(self.confirm(self.write("ok.flac", data))["duration_s"], 2.0)
        stereo = synthetic_flac(rate=8000, channels=2, total=12000, blocksize=1024)
        self.assertAlmostEqual(self.confirm(self.write("st.flac", stereo))["duration_s"], 1.5)
        self.assertRefused(data[:-2], "no-crc.flac")
        # Multi-byte coded frame numbers (> 127 frames) and variable-blocksize
        # streams (coded sample numbers) walk too.
        many = synthetic_flac(rate=8000, channels=1, total=16000, blocksize=64)
        self.assertAlmostEqual(self.confirm(self.write("many.flac", many))["duration_s"], 2.0)
        var = synthetic_flac(rate=8000, channels=1, total=16000, blocksize=100, variable=True)
        self.assertAlmostEqual(self.confirm(self.write("var.flac", var))["duration_s"], 2.0)
        self.assertRefused(var[:-40], "var-cut.flac")
        frames = data[_audio_offset(data):]
        # STREAMINFO says 16000 samples; drop the last frame entirely.
        last = frames.rfind(b"\xff\xf8")
        self.assertRefused(data[:_audio_offset(data) + last], "no-last-frame.flac")

    def test_trailing_data_is_named_as_such_not_as_an_interrupted_export(self):
        # Port of .scratch/review-b9-probes/p2b.py: tags appended after the
        # last frame are refused, and the message says what is actually wrong.
        data = synthetic_flac()
        id3 = (b"TAG" + b"title".ljust(30, b"\0") + b"artist".ljust(30, b"\0")
               + b"album".ljust(30, b"\0") + b"2026" + b"\0" * 30 + b"\xff")
        for name, tail in (("id3.flac", id3), ("ape.flac", b"APETAGEX" + b"\0" * 24)):
            with self.subTest(name=name):
                path = self.write(name, data + tail)
                with self.assertRaisesRegex(
                        ValueError, r"unexpected data after the last audio frame "
                                    r"\(tags appended after export\?\)"):
                    self.confirm(path, confirm=True)
        with self.assertRaisesRegex(ValueError, "interrupted export") as cut:
            self.confirm(self.write("cut.flac", data[:-1]), confirm=True)
        self.assertNotIn("unexpected data", str(cut.exception))


class InPlaceClipEditTests(unittest.TestCase):
    """Finding 1: a hearing bound to a Live clip id is only current while the
    clip still plays the same source and range."""

    def setUp(self):
        from mcp_server import session_ears
        from tests.test_take_card_persistence import IDENT, _card
        self.se, self.ident, self.make = session_ears, IDENT, _card
        self.original = {"id": "101", "path": "t0/s0", "sampleFile": "/music/vocal.wav",
                         "start": "1|1", "end": "3|1", "looping": False}

    def card(self, take_id="heard-original", row=None, heard_at=None, **extra):
        import time
        card = self.make(take_id, vibe=extra.pop("vibe", "bright vocal in the original range"))
        card.update(live_clip_id="101", clip_locator="0/0", analyzed_start_s=0,
                    analyzed_end_s=4, analyzed_duration_s=4,
                    heard_at=heard_at if heard_at is not None else time.time())
        if row is not None:
            card["content_signature"] = self.se._content_signature(row)
        card.update(extra)
        return card

    def fold(self, cards, rows):
        out = self.se.fold_take_cards(
            cards, {}, expected_set_identity=dict(self.ident),
            _resolved_identity=dict(self.ident), _current_clips=rows)
        return [(c["take_id"], c.get("membership_unverified"))
                for c in out.get("session_take_cards", [])]

    def test_probe_legacy_card_with_changed_clip_is_not_silently_current(self):
        rows = [{"id": "101", "path": "t0/s0", "sampleFile": "/different/drone.wav",
                 "start_marker": 32, "end_marker": 48}]
        self.assertEqual(self.fold([self.card()], rows), [("heard-original", True)])

    def test_signed_card_matching_the_clip_is_current(self):
        moved = dict(self.original, path="t0/s5", arrangementStart="9|1")
        self.assertEqual(self.fold([self.card(row=self.original)], [moved]),
                         [("heard-original", None)])

    def test_changed_source_or_range_makes_the_hearing_historical(self):
        for label, change in (("source", {"sampleFile": "/different/drone.wav"}),
                              ("start", {"start": "9|1"}),
                              ("end", {"end": "13|1"}),
                              ("looping", {"looping": True}),
                              ("pitch", {"pitchShift": 7})):
            with self.subTest(label):
                card = self.card(row=dict(self.original, pitchShift=0))
                row = {**self.original, "pitchShift": 0, **change}
                self.assertEqual(self.fold([card], [row]), [])

    def test_unreadable_current_evidence_is_membership_unverified(self):
        card = self.card(row=self.original)
        for row in ({"id": "101", "path": "t0/s0"},
                    {"id": "101", "sampleFile": "/music/vocal.wav"}):
            with self.subTest(row=row):
                self.assertEqual(self.fold([card], [row]), [("heard-original", True)])

    def test_newest_matching_hearing_wins_after_a_revert(self):
        import time
        now = time.time()
        edited = dict(self.original, start="5|1")
        first = self.card("first", row=self.original, heard_at=now - 60, vibe="first")
        second = self.card("second", row=edited, heard_at=now, vibe="edited")
        self.assertEqual(self.fold([first, second], [self.original]), [("first", None)])
        self.assertEqual(self.fold([first, second], [edited]), [("second", None)])

    def test_legacy_card_older_than_a_changed_hearing_does_not_fold(self):
        import time
        now = time.time()
        legacy = self.card("legacy", heard_at=now - 60, vibe="legacy")
        changed = self.card("changed", row=dict(self.original, start="5|1"), heard_at=now)
        self.assertEqual(self.fold([legacy, changed], [self.original]), [])

    def test_default_phase_card_against_a_rotated_clip_is_a_change(self):
        # b9 review minor: Producer Pal omits firstStart when it equals start,
        # so the card's missing firstStart means "start", not "unknown".
        card = self.card(row=self.original)
        self.assertEqual(self.fold([card], [dict(self.original, firstStart="1|3")]), [])
        self.assertEqual(self.fold([card], [dict(self.original, firstStart="1|1")]),
                         [("heard-original", None)])

    def test_default_gain_and_pitch_card_against_a_changed_clip_is_a_change(self):
        card = self.card(row=self.original)
        for change in ({"gainDb": -6}, {"pitchShift": 7}):
            with self.subTest(change=change):
                self.assertEqual(self.fold([card], [{**self.original, **change}]), [])
        self.assertEqual(self.fold([card], [dict(self.original, gainDb=0, pitchShift=0)]),
                         [("heard-original", None)])

    def test_in_place_overwrite_of_the_sample_is_a_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = os.path.join(tmp, "vocal.wav")
            with open(audio, "wb") as fh:
                fh.write(b"a" * 100)
            os.utime(audio, (1900000000, 1900000000))
            row = dict(self.original, sampleFile=audio)
            card = self.card(row=row)
            self.assertEqual(self.fold([card], [row]), [("heard-original", None)])
            os.utime(audio, (1900000100, 1900000100))
            self.assertEqual(self.fold([card], [row]), [], "touched in place")
            with open(audio, "wb") as fh:
                fh.write(b"b" * 200)
            os.utime(audio, (1900000000, 1900000000))
            self.assertEqual(self.fold([card], [row]), [], "rewritten, same mtime")

    def test_signature_is_path_free_and_server_owned(self):
        signature = self.se._content_signature(self.original)
        self.assertNotIn("/music", json.dumps(signature))
        card = self.card(row=self.original)
        self.assertEqual(self.se.validate_take_card(card)["content_signature"], signature)
        self.assertNotIn("content_signature", self.se._sanitize_listener_card(card))
        nested = {k: v for k, v in self.original.items() if k != "sampleFile"}
        nested["sample"] = {"sampleFile": "/music/vocal.wav"}
        self.assertEqual(self.se._content_signature(nested), signature)

    def test_listen_and_scan_read_the_same_signature(self):
        # One Producer Pal clip, read at listen time (ppal-read-clip) and by
        # the membership scan (ppal-read-track rows): same signature.
        import contextlib
        from mcp_server import live_write, pp_session as pps
        from mcp_server.pp_session import PPResponse
        with tempfile.TemporaryDirectory() as tmp:
            audio = os.path.join(tmp, "take.wav")
            with wave.open(audio, "wb") as out:
                out.setnchannels(1); out.setsampwidth(2); out.setframerate(8000)
                out.writeframes(b"\0\0" * 8000)
            clip = {"id": "101", "type": "audio", "name": "Vox", "sampleFile": audio,
                    "start": "1|1", "end": "3|1", "looping": False, "gainDb": 0,
                    "pitchShift": 0, "arrangementStart": "5|1"}
            calls = []

            class Fake:
                capabilities = {}

                async def call(self, tool, args):
                    calls.append((tool, args))
                    if tool == "ppal-read-clip":
                        body = dict(clip)
                    elif tool == "ppal-read-live-set":
                        body = {"tracks": [{"trackIndex": 0, "name": "Vox", "type": "audio"}]}
                    elif tool == "ppal-read-track":
                        body = {"trackIndex": 0, "sessionClips": [],
                                "arrangementClips": [dict(clip)]}
                    else:
                        raise AssertionError(tool)
                    return PPResponse(json.dumps({"result": body}), body, [])

            async def listen_read():
                return await self.se._read_audio_take(Fake(), {"id": "101"}, 0, "Vox",
                                                      "arrangement")
            take = self.se._run_async(listen_read())
            rows = self.se._run_async(self.se._scan_current_clips(Fake()))
        read_clip = [args for tool, args in calls if tool == "ppal-read-clip"][0]
        self.assertIn("timing", read_clip["include"])
        scan = [args for tool, args in calls if tool == "ppal-read-track"][0]
        self.assertEqual(scan["include"], ["session-clips", "arrangement-clips",
                                           "timing", "sample"])
        self.assertIsNotNone(take.content_signature)
        self.assertEqual(rows, [{"id": "101", "content_signature": take.content_signature}])


if __name__ == "__main__":
    unittest.main()
