"""moment.read_piece_snapshot on the real NOCTURNEv2 capture (plan batch8 1.3).

Every Producer Pal call is replayed by tests.pp_fakes.PieceDevice, which checks
argument names AND values against the captured 2.2.0-rc1 schemas and answers
read-track only with include lists that were really captured. No Live, no
network, no store.
"""
import asyncio
import copy
import unittest
from unittest.mock import patch

from mcp_server import moment
from mcp_server import piece_snapshot as ps
from mcp_server import pp_session as pp
from tests.pp_fakes import V23, PieceDevice, on_both_versions


def setUpModule():
    pp.set_live_process_incarnation_resolver(
        lambda: {"pid": 4242, "start_time": "Sat Sep 26 10:00:00 2026"})


def tearDownModule():
    pp.set_live_process_incarnation_resolver(None)


def snapshot(device, **kwargs):
    with patch.object(pp, "operation_session", device.operation()):
        return asyncio.run(moment.read_piece_snapshot(**kwargs))


def track(snap, name, kind=None):
    found = [t for t in snap["tracks"] if t["name"] == name and (kind is None or t["type"] == kind)]
    assert len(found) == 1, (name, found)
    return found[0]


@on_both_versions
class RealCaptureSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.device = PieceDevice()
        cls.snap = snapshot(cls.device)

    def test_identity_is_structural_and_says_so_without_the_live_api(self):
        identity = self.snap["identity"]
        self.assertEqual(identity["basis"], "structural")
        self.assertFalse(identity["verified"])
        self.assertIsNone(identity["file_path"])
        self.assertIn("Live API off", identity["note"])
        self.assertEqual(identity["attempts"], 1)
        self.assertNotIn("ppal-live-api", [name for name, _ in self.device.calls])
        # identity read before and after: two overviews around the collection
        names = [name for name, _ in self.device.calls]
        self.assertEqual(names[0], "ppal-read-live-set")
        self.assertEqual(names[-1], "ppal-read-live-set")
        self.assertEqual(names.count("ppal-read-live-set"), 2)

    def test_set_meter_tempo_and_track_model(self):
        self.assertEqual(self.snap["set"]["name"], "NOCTURNEv2")
        self.assertEqual(self.snap["set"]["tempo"], 90.0)
        self.assertEqual(self.snap["set"]["meter"], "4/4")
        self.assertEqual(self.snap["set"]["bars"], 108)
        self.assertEqual(len(self.snap["tracks"]), 32)
        self.assertEqual(track(self.snap, "6-Group")["type"], "group")
        self.assertEqual(track(self.snap, "Drum Map")["group"], "6-Group")
        self.assertEqual(track(self.snap, "Synth A", "midi")["group"], "Digital In")
        self.assertEqual(track(self.snap, "Synth A", "audio")["group"], "Digital In")
        self.assertTrue(track(self.snap, "25-MIDI")["armed"])
        self.assertEqual(track(self.snap, "Grain Pad")["devices"],
                         ["instrument: Kontakt 8", "audio-effect: DimensionExpander"])
        self.assertEqual([len(self.snap["returns"]), self.snap["master"]["name"]], [2, "Main"])

    def test_muted_tracks_are_reported_and_left_out_of_the_layers(self):
        self.assertEqual([m["track"] for m in self.snap["muted_tracks"]],
                         ["Interface", "31-Kontakt 8"])
        for name in ("Interface", "31-Kontakt 8"):
            self.assertTrue(track(self.snap, name)["muted"])
            self.assertTrue(track(self.snap, name)["his_material"])
            self.assertFalse(track(self.snap, name)["in_layers"])

    def test_sidekick_host_and_group_tracks_are_listed_not_counted(self):
        excluded = {item["track"]: item["reason"] for item in self.snap["not_his_material"]}
        self.assertEqual(set(excluded), {"PP", "6-Group", "Digital In", "SIDEKICK JAM"})
        self.assertIn("sidekick", excluded["SIDEKICK JAM"])
        counted = {item["track"] for item in self.snap["layers"]["counted_tracks"]}
        self.assertFalse(counted & {"PP", "6-Group", "Digital In", "SIDEKICK JAM"})

    def test_inferred_sections_on_the_real_capture(self):
        # 0 locators, so blocks come from clip boundaries: a new section where
        # >=2 of his layers enter or leave together, or silence starts/ends.
        #  1-32  Low Drone + Noise Bed, then single entries at 5, 11, 13 (no split)
        # 33-44  Synth A, Bassline, Grain Pad, Drum Map enter together
        # 45-60  three Tape Strings + Glitch Kit enter, 3 intro layers leave
        #        (Grain Pad's 1bar+n/8 clips flicker 9/10 inside — one track, no split)
        # 61-68  Grain Pad + 30-Kontakt 8 enter: the 11-layer peak
        # 69-84  five layers leave; six remain (NOT empty — the 09-02 "empty 69-84"
        #        note predates his fills "Block B" / "Block A" / "Block D")
        # 85-92  silence: nothing is placed on any track
        # 93-100 seven layers return
        # 101-108 Bassline + Voice FX join for the last eight bars
        sections = self.snap["sections"]
        self.assertEqual([s["bars"] for s in sections],
                         [[1, 32], [33, 44], [45, 60], [61, 68], [69, 84], [85, 92],
                          [93, 100], [101, 108]])
        self.assertTrue(all(s["status"] == "inferred" for s in sections))
        self.assertIn("inferred from clip boundaries", self.snap["section_rule"])
        self.assertEqual([(s["layers_min"], s["layers_max"]) for s in sections],
                         [(2, 5), (8, 9), (9, 10), (11, 11), (6, 6), (0, 0), (6, 7), (8, 8)])
        self.assertEqual([s["silent"] for s in sections],
                         [False, False, False, False, False, True, False, False])
        self.assertEqual(sorted(t["track"] for t in sections[4]["tracks"]),
                         ["20-Bassline", "27-Tape Strings",
                          "28-Tape Strings",
                          "29-Tape Strings",
                          "9-Glitch Kit", "Drum Map"])
        self.assertEqual(sorted(t["track"] for t in sections[4]["leaves"]),
                         ["30-Kontakt 8", "Grain Pad", "Kit Var 3", "Low Drone",
                          "Synth A"])
        self.assertEqual(sections[5]["start_beats"], 336.0)
        self.assertEqual(sections[5]["end_beats"], 368.0)

    def test_layer_matrix_totals_come_from_the_shared_pure_function(self):
        layers = self.snap["layers"]
        self.assertEqual(len(layers["per_bar"]), 108)
        self.assertEqual(sum(layers["per_bar"]), 688)
        self.assertEqual(max(layers["per_bar"]), 11)
        self.assertEqual(layers["per_bar"][68:84], [6] * 16)     # bars 69-84
        self.assertEqual(layers["per_bar"][84:92], [0] * 8)      # bars 85-92
        self.assertEqual(layers["audio_per_bar"], [0] * 108)
        matrix = self.snap["layer_matrix"]
        self.assertEqual(matrix["basis"], "midi_layers")
        self.assertEqual(matrix["layers_per_bar"], layers["per_bar"])
        self.assertEqual(matrix["audio_unseen"], {"tracks": 0, "clips": 0})
        # the same pure function precedents use, fed the snapshot's own clips
        from mcp_server.piece_context import layer_matrix
        clips = [{"track": t["id"], "start": c["start_beats"], "end": c["end_beats"]}
                 for t in self.snap["tracks"] if t["in_layers"]
                 for c in t["arrangement_clips"]]
        self.assertEqual(layer_matrix(clips, beats_per_bar=4, audio_track_names=[],
                                      audio_clip_count=0), matrix)
        # 1-based view of entries: the Tape Strings enter at bar 45
        entries = {(e["bar"], e["track"]) for e in layers["entries"]}
        self.assertIn((45, "27-Tape Strings"), entries)

    def test_clip_positions_loops_and_notes(self):
        heart = track(self.snap, "Low Drone")["arrangement_clips"]
        first = heart[0]
        self.assertEqual((first["start"], first["length"]), ("1|1", "16bar"))
        self.assertEqual((first["start_beats"], first["end_beats"]), (0.0, 64.0))
        self.assertEqual((first["start_bar"], first["end_bar"]), (1, 17))
        self.assertTrue(first["bar_aligned"])
        self.assertEqual(first["region"]["start"], "19|1")
        self.assertEqual((first["region"]["start_beats"], first["region"]["end_beats"]),
                         (72.0, 88.0))
        self.assertTrue(first["region"]["looping"])
        self.assertEqual(len(first["notes"]), 7)
        self.assertEqual((first["notes"][0]["pitch"], first["notes"][0]["start_beats"]), (42, 1.5))
        named = [c for c in heart if c["name"] == "Example Rack (8-Bar)"]
        self.assertEqual([c["start_bar"] for c in named], [45, 49, 53, 57, 61, 65])
        silo = track(self.snap, "Grain Pad")["arrangement_clips"]
        odd = next(c for c in silo if c["start"] == "41|1")
        self.assertEqual(odd["end_beats"] - odd["start_beats"], 20.5)   # 5bar+n/8
        self.assertFalse(odd["bar_aligned"])
        self.assertEqual(len(track(self.snap, "Grain Pad")["take_lanes"][0]["clips"]), 1)

    def test_notes_outside_the_read_window_are_unread_not_hidden(self):
        # Producer Pal returns notes only in [-L, 2L); Bassline at 33|1 loops 88-104
        # in a 16-beat clip, so none of what plays was read (Live API off here).
        self.assertTrue(any("[-L, 2L)" in item for item in self.snap["uncertain"]))
        bassline = track(self.snap, "20-Bassline")["arrangement_clips"]
        at_33 = next(c for c in bassline if c["start"] == "33|1")
        self.assertEqual((at_33["coverage"], at_33["notes_status"]), ("unseen", "unread"))
        self.assertEqual((at_33["notes_audible"], at_33["notes_hidden"]), (None, None))
        self.assertEqual(len(at_33["notes"]), 7)          # what the window showed
        # placement still counts: Bassline is a layer at bar 33
        self.assertIn(track(self.snap, "20-Bassline")["id"],
                      self.snap["layer_matrix"]["tracks_per_bar"][32])

    def test_routing_feeds_expose_the_voice_fx_chain(self):
        voice_fx = track(self.snap, "Voice FX")
        self.assertEqual(track(self.snap, "24-MIDI")["routing"]["output"],
                         {"kind": "track", "target": "Voice FX", "target_ids": [voice_fx["id"]]})
        self.assertEqual(sorted(f["track"] for f in voice_fx["fed_by"]), ["24-MIDI", "25-MIDI"])
        self.assertEqual(voice_fx["feeds"],
                         [{"track_id": "865", "track": "23-Audio", "via": "input"}])
        self.assertEqual(track(self.snap, "Drum Map")["routing"]["output"]["kind"], "group")
        self.assertEqual(track(self.snap, "PP")["routing"]["output"]["kind"], "none")
        self.assertEqual(self.snap["sidechains"]["status"], "unknown")

    def test_transport_and_selection_without_the_live_api(self):
        transport = self.snap["transport"]
        self.assertIsNone(transport["source"])
        self.assertIsNone(transport["playhead_beats"])
        self.assertIsNone(transport["loop"])
        self.assertIn("Live API", transport["reason"])
        self.assertEqual(self.snap["selection"]["view"], "session")
        self.assertIsNone(self.snap["selection"]["arrangement_clip"])
        self.assertEqual(self.snap["locators"], [])

    def test_every_call_was_a_captured_read(self):
        tools = {name for name, _ in self.device.calls}
        self.assertEqual(tools, {"ppal-read-live-set", "ppal-read-track", "ppal-select"})
        # 2 overviews + 32 tracks + 2 returns + main + select
        self.assertEqual(len(self.device.calls), 38)


@on_both_versions
class AudioClipTests(unittest.TestCase):
    def test_audio_clips_are_named_located_and_unheard(self):
        device = PieceDevice()
        # Synthetic: the capture has no arrangement audio clip. Shape = a 2.2
        # arrangement clip plus read-clip's documented `sample` fields.
        device.add_arrangement_clip(18, {
            "arrangementLength": "4bar", "arrangementStart": "5|1", "end": "5|1",
            "id": "7001", "length": "4bar", "looping": False, "name": "vox take 3",
            "start": "1|1", "timeSignature": "4/4", "sampleFile": "/x/vox take 3.wav",
            "gainDb": -3.0})
        snap = snapshot(device)
        self.assertEqual(snap["unheard"], [{
            "track_id": "20", "track": "19-Audio", "clip_id": "7001",
            "clip_name": "vox take 3", "start": "5|1", "start_bar": 5, "end_bar": 9,
            "sample": {"sampleFile": "/x/vox take 3.wav", "gainDb": -3.0},
            "his_material": True, "heard": False}])
        clip = track(snap, "19-Audio")["arrangement_clips"][0]
        self.assertEqual(clip["kind"], "audio")
        self.assertNotIn("notes", clip)
        self.assertEqual(snap["layers"]["audio_per_bar"][4:8], [1, 1, 1, 1])
        self.assertEqual(snap["layers"]["audio_per_bar"][8], 0)
        self.assertEqual(sum(snap["layers"]["per_bar"]), 688)          # MIDI basis unchanged
        self.assertEqual(snap["layer_matrix"]["audio_unseen"], {"tracks": 1, "clips": 1})


@on_both_versions
class LiveApiSnapshotTests(unittest.TestCase):
    def test_live_api_identity_transport_and_saved_set_path(self):
        device = PieceDevice(live_api=True)
        snap = snapshot(device)
        identity = snap["identity"]
        self.assertEqual(identity["basis"], "live_api")
        self.assertTrue(identity["verified"])
        self.assertTrue(identity["file_path"].endswith("NOCTURNEv2.als"))
        # read/live_api_live_set_properties.json: song time 321.31, loop 320 + 16
        transport = snap["transport"]
        self.assertEqual(transport["source"], "live_api")
        self.assertEqual(transport["playhead_bar"], 81)
        self.assertEqual(transport["loop"]["start"], "81|1")
        self.assertEqual(transport["loop"]["end"], "85|1")
        self.assertTrue(transport["loop"]["on"])
        self.assertFalse(transport["is_playing"])
        self.assertEqual(snap["sidechains"]["status"], "unknown")
        self.assertIn("not on this disk", snap["sidechains"]["reason"])


@on_both_versions
class IdentityRetryTests(unittest.TestCase):
    def _rename_before_call(self, device, *call_numbers):
        for number in call_numbers:
            device.before_call[number] = (
                lambda d, n=number: d.rename_track(3, "Noise Bed %d" % n))

    def test_structural_change_mid_read_retries_once_and_succeeds(self):
        device = PieceDevice()
        self._rename_before_call(device, 5)            # during attempt 1's collection
        snap = snapshot(device)
        self.assertEqual(snap["identity"]["attempts"], 2)
        self.assertTrue(any("changed" in reason for reason in snap["identity"]["retried_because"]))
        self.assertEqual(track(snap, "Noise Bed 5")["index"], 3)
        self.assertEqual(len(device.calls), 76)

    def test_second_change_refuses(self):
        device = PieceDevice()
        self._rename_before_call(device, 5, 43)        # both attempts see a change
        with self.assertRaisesRegex(ps.SetChangedWhileReading, "the Set changed while reading"):
            snapshot(device)

    def test_live_identity_change_refuses_after_retry(self):
        device = PieceDevice(live_api=True)
        flips = iter(range(100))

        def other_set(d):
            d.live_api_values["file_path"] = "/Users/USER/other%d.als" % next(flips)
        for number in range(2, 200, 3):
            device.before_call[number] = other_set
        with self.assertRaisesRegex(ps.SetChangedWhileReading, "set_id"):
            snapshot(device)


@on_both_versions
class MinorFindingTests(unittest.TestCase):
    def test_a_track_muted_mid_read_is_not_a_changed_set(self):
        """round-2 review: mute/solo/monitoring are what auditions flip; a long
        Live API read (126 extra calls) must not refuse over one. Freshness
        (proposal_freshness) still sees the state."""
        device = PieceDevice(live_api=True)

        def mute(d):
            d.track(19)["state"] = "muted"
            next(t for t in d.overview["tracks"] if t["trackIndex"] == 19)["state"] = "muted"
        device.before_call[5] = mute                    # during attempt 1's collection
        snap = snapshot(device)
        self.assertEqual(snap["identity"]["attempts"], 1)
        self.assertTrue(track(snap, "20-Bassline")["muted"])
        target = {"bars": [33, 36], "track_ids": [track(snap, "20-Bassline")["id"]]}
        unmuted = snapshot(PieceDevice(live_api=True))
        self.assertNotEqual(ps.proposal_freshness(snap, target)["digest"],
                            ps.proposal_freshness(unmuted, target)["digest"])

    def test_a_track_added_mid_read_still_retries_then_refuses(self):
        def add_track(d, n=[0]):
            n[0] += 1
            index = len(d.overview["tracks"])
            listed = dict(d.overview["tracks"][-1], id="new%d" % n[0], name="New %d" % n[0],
                          trackIndex=index, arrangementClipCount=0)
            d.overview["tracks"].append(listed)
            d.track_records[index] = dict(d.track_records[index - 1], id=listed["id"],
                                          name=listed["name"], trackIndex=index,
                                          arrangementClips=[])
            d.track_includes[("regular", index)] = d.track_includes[("regular", index - 1)]
        once = PieceDevice()
        once.before_call[5] = add_track
        snap = snapshot(once)
        self.assertEqual(snap["identity"]["attempts"], 2)
        self.assertTrue(any("track list" in r for r in snap["identity"]["retried_because"]))
        twice = PieceDevice()
        twice.before_call[5] = add_track
        twice.before_call[45] = add_track
        with self.assertRaisesRegex(ps.SetChangedWhileReading, "the Set changed while reading"):
            snapshot(twice)

    def test_fingerprint_ignores_mute_and_monitoring_but_sees_routing(self):
        overview = copy.deepcopy(PieceDevice().overview)
        base = ps.structural_fingerprint(overview)["digest"]
        muted = copy.deepcopy(overview)
        muted["tracks"][19]["state"] = "muted"
        muted["tracks"][19]["monitoringState"] = "in"
        self.assertEqual(ps.structural_fingerprint(muted)["digest"], base)
        routed = copy.deepcopy(overview)
        routed["tracks"][19]["outputRoutingType"] = {"name": "6-Group", "outputId": "6"}
        self.assertNotEqual(ps.structural_fingerprint(routed)["digest"], base)

    def test_members_of_a_muted_group_are_not_layers(self):
        device = PieceDevice()
        device.track(5)["state"] = "muted"                                  # 6-Group
        next(t for t in device.overview["tracks"] if t["trackIndex"] == 5)["state"] = "muted"
        snap = snapshot(device)
        for name in ("Drum Map", "Kit Var 3", "9-Glitch Kit"):
            member = track(snap, name)
            self.assertTrue(member["his_material"], name)
            self.assertFalse(member["in_layers"], name)
            self.assertEqual(member["muted_by_group"], "6-Group")
        counted = {item["track"] for item in snap["layers"]["counted_tracks"]}
        self.assertNotIn("Drum Map", counted)
        self.assertIn("20-Bassline", counted)

    def test_sub_bar_clips_are_flagged(self):
        snap = snapshot(PieceDevice())
        storm = next(c for c in track(snap, "Noise Bed")["arrangement_clips"]
                     if c["start"] == "93|1")
        self.assertTrue(storm["sub_bar"])
        self.assertTrue(any("shorter than a bar" in item and "Noise Bed @ 93|1" in item
                            for item in snap["uncertain"]))


@on_both_versions
class DigestTests(unittest.TestCase):
    TARGET = {"section_index": 4, "track_ids": ["8"], "protected_track_ids": ["3"],
              "decisions_revision": 7}

    def test_moving_a_clip_eight_bars_invalidates_freshness_not_source(self):
        device = PieceDevice()
        before = snapshot(device)
        device.move_clip(6, device.track(6)["arrangementClips"][-1]["id"], 8)   # 81|1 -> 89|1
        after = snapshot(device)
        self.assertEqual(after["sections"][4]["bars"], before["sections"][4]["bars"])
        fresh_before = ps.proposal_freshness(before, self.TARGET)
        fresh_after = ps.proposal_freshness(after, self.TARGET)
        self.assertNotEqual(fresh_before["digest"], fresh_after["digest"])
        self.assertEqual(before["source_integrity"], after["source_integrity"])

    def test_playhead_and_selection_change_neither_digest(self):
        device = PieceDevice(live_api=True)
        before = snapshot(device)
        device.set_playhead(12.0)
        device.set_selection({"view": "arrangement", "selectedClip": {
            "id": "287", "arrangementStart": "1|1", "trackIndex": 1}})
        after = snapshot(device)
        self.assertNotEqual(before["transport"]["playhead_bar"], after["transport"]["playhead_bar"])
        self.assertEqual(after["selection"]["arrangement_clip"]["id"], "287")
        self.assertEqual(ps.proposal_freshness(before, self.TARGET)["digest"],
                         ps.proposal_freshness(after, self.TARGET)["digest"])
        self.assertEqual(before["source_integrity"], after["source_integrity"])

    def test_freshness_tracks_mutes_routing_protection_and_decisions(self):
        snap = snapshot(PieceDevice())
        base = ps.proposal_freshness(snap, self.TARGET)["digest"]
        muted = copy.deepcopy(snap)
        track(muted, "Drum Map")["muted"] = True
        self.assertNotEqual(ps.proposal_freshness(muted, self.TARGET)["digest"], base)
        for change in ({"decisions_revision": 8}, {"protected_track_ids": ["3", "4"]},
                       {"section_index": 3}):
            with self.subTest(change=change):
                self.assertNotEqual(
                    ps.proposal_freshness(snap, {**self.TARGET, **change})["digest"], base)
        loop_moved = copy.deepcopy(snap)
        loop_moved["transport"] = {"loop": {"start_beats": 0.0, "end_beats": 4.0}}
        self.assertEqual(ps.proposal_freshness(loop_moved, self.TARGET)["digest"], base)

    def test_freshness_tracks_tempo_and_meter(self):
        # review 72h finding 1: a tempo change re-times every section and the
        # plan's duration, so a proposal made at 90 BPM is not current at 120
        device = PieceDevice()
        base = ps.proposal_freshness(snapshot(device), self.TARGET)
        self.assertEqual(base["components"]["set"]["tempo"], 90.0)
        device.overview["tempo"] = 120
        faster = ps.proposal_freshness(snapshot(device), self.TARGET)
        self.assertNotEqual(faster["digest"], base["digest"])
        snap = snapshot(PieceDevice())
        other_meter = copy.deepcopy(snap)
        other_meter["set"]["meter"] = "7/8"
        self.assertNotEqual(ps.proposal_freshness(other_meter, self.TARGET)["digest"],
                            base["digest"])

    def test_source_integrity_sees_notes_and_devices_not_mute(self):
        device = PieceDevice(live_api=True)
        base = snapshot(device)["source_integrity"]["digests"]["8"]
        device.track(6)["state"] = "muted"
        device.track(6)["arrangementClips"][0]["name"] = "renamed"
        self.assertEqual(snapshot(device)["source_integrity"]["digests"]["8"], base)
        first = device.track(6)["arrangementClips"][0]["id"]
        device.clip_live[first]["notes"][0]["pitch"] += 1
        edited = snapshot(device)["source_integrity"]["digests"]["8"]
        self.assertNotEqual(edited, base)
        device.track(6)["devices"].pop()
        self.assertNotEqual(snapshot(device)["source_integrity"]["digests"]["8"], edited)

    def test_without_the_live_api_a_track_with_unread_notes_has_no_digest(self):
        integrity = snapshot(PieceDevice())["source_integrity"]
        blind = {item["track_id"] for item in integrity["blind"]}
        self.assertIn("21", blind)                      # 20-Bassline: loops past 2L
        self.assertNotIn("21", integrity["digests"])
        self.assertIn("8", integrity["digests"])        # Drum Map: all read


@on_both_versions
class PureSectionTests(unittest.TestCase):
    def test_two_locators_make_locator_sections(self):
        sections = ps.locator_sections(
            [{"name": "Drop", "time": "33|1"}, {"name": "Intro", "time": "9|1"}],
            num=4, den=4, song_end_beats=256.0)
        self.assertEqual([(s["name"], s["start_beats"], s["end_beats"]) for s in sections],
                         [("before Intro", 0.0, 32.0), ("Intro", 32.0, 128.0),
                          ("Drop", 128.0, 256.0)])

    def test_locator_snapshot_uses_them(self):
        device = PieceDevice()
        device.overview["locators"] = [{"name": "A", "time": "1|1"}, {"name": "B", "time": "69|1"}]
        snap = snapshot(device)
        self.assertEqual([(s["name"], s["bars"], s["status"]) for s in snap["sections"]],
                         [("A", [1, 68], "locator"), ("B", [69, 108], "locator")])

    def test_single_track_flicker_never_splits(self):
        bars = [["a", "b"], ["a", "b", "c"], ["a", "b"], ["a", "b", "c"], [], [], ["a"]]
        sections = ps.infer_sections(bars, beats_per_bar=4.0)
        self.assertEqual([(s["bar_index_start"], s["bar_index_end"]) for s in sections],
                         [(0, 4), (4, 6), (6, 7)])

    def test_meter_places_bars_in_three_four(self):
        self.assertEqual(ps.position_beats("3|2", 3, 4), 7.0)
        self.assertEqual(ps.position_beats("2|4", 6, 8), 4.5)
        self.assertEqual(ps.bar_beat(7.0, 3, 4), "3|2")
        with self.assertRaises(ps.PieceSnapshotError):
            ps.meter_of("4/3")


def hazard_types(snap, name, kind=None):
    """Routing/sidechain-edge hazards (``sidechain_unknown`` has its own tests)."""
    return sorted(h["type"] for h in track(snap, name, kind)["hazards"]
                  if h["type"] != "sidechain_unknown")


@on_both_versions
class HazardTests(unittest.TestCase):
    """Typed per-track hazards: an experiment that mutes one of these tracks
    changes more than the experiment, so staging (1.5) must refuse them."""

    @classmethod
    def setUpClass(cls):
        cls.snap = snapshot(PieceDevice())

    def test_external_instrument_pair_is_flagged_on_both_tracks(self):
        midi = track(self.snap, "Synth A", "midi")
        audio = track(self.snap, "Synth A", "audio")
        self.assertEqual(hazard_types(self.snap, "Synth A", "midi"),
                         ["external_instrument_return", "input_unknown", "output_unknown"])
        self.assertEqual(hazard_types(self.snap, "Synth A", "audio"),
                         ["external_instrument_return"])
        pair = next(h for h in midi["hazards"] if h["type"] == "external_instrument_return")
        self.assertEqual(pair["track_id"], audio["id"])
        back = next(h for h in audio["hazards"] if h["type"] == "external_instrument_return")
        self.assertEqual(back["track_id"], midi["id"])

    def test_track_to_track_feeds_including_midi_out(self):
        self.assertEqual(hazard_types(self.snap, "24-MIDI"), ["feeds_track"])
        self.assertEqual(hazard_types(self.snap, "Voice FX"),
                         ["fed_by_track", "fed_by_track", "feeds_track"])
        self.assertEqual(hazard_types(self.snap, "23-Audio"), ["fed_by_track"])
        feed = track(self.snap, "24-MIDI")["hazards"][0]
        self.assertEqual((feed["track"], feed["via"]), ("Voice FX", "output"))

    def test_unknown_input_and_clean_tracks(self):
        self.assertEqual(hazard_types(self.snap, "Omnisphere"), ["input_unknown"])
        for name in ("20-Bassline", "Drum Map", "Low Drone"):
            self.assertEqual(hazard_types(self.snap, name), [], name)
        listed = {item["track"] for item in self.snap["hazard_tracks"]}
        self.assertTrue({"Synth A", "Voice FX", "24-MIDI", "Omnisphere"} <= listed)

    def test_unread_sidechains_are_a_hazard_on_every_track_of_his(self):
        """round-2 review: without the saved .als nobody knows who ducks whom,
        so muting any of his tracks may change another — staging must refuse."""
        self.assertEqual(self.snap["sidechains"]["status"], "unknown")
        for t in self.snap["tracks"]:
            types = [h["type"] for h in t["hazards"]]
            self.assertEqual("sidechain_unknown" in types, t["his_material"], t["name"])
        listed = {item["track"] for item in self.snap["hazard_tracks"]}
        self.assertIn("20-Bassline", listed)
        self.assertTrue(any("sidechain" in item for item in self.snap["uncertain"]))
        target = {"bars": [33, 36], "track_ids": [track(self.snap, "20-Bassline")["id"]]}
        read = copy.deepcopy(self.snap)
        ps.attach_sidechains(read, {"tracks": [], "returns": [], "sidechains": []},
                             file_path="/x/NOCTURNEv2.als")
        self.assertEqual(track(read, "20-Bassline")["hazards"], [])
        self.assertNotIn("20-Bassline", {item["track"] for item in read["hazard_tracks"]})
        self.assertIn("unsaved", read["sidechains"]["note"])
        self.assertNotEqual(ps.proposal_freshness(read, target)["digest"],
                            ps.proposal_freshness(self.snap, target)["digest"])

    def test_an_unreadable_saved_set_keeps_the_hazard(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Broken.als")
            with open(path, "wb") as handle:
                handle.write(b"not a set")
            device = PieceDevice(live_api=True)
            device.live_api_values["file_path"] = path
            snap = snapshot(device)
        self.assertEqual(snap["sidechains"]["status"], "unknown")
        self.assertIn("sidechain_unknown",
                      [h["type"] for h in track(snap, "20-Bassline")["hazards"]])

    def test_ambiguous_routing_is_a_hazard_never_a_guess(self):
        by_name = {"Out": ["1", "2"], "Src": ["3"]}
        read = {"id": "3", "name": "Src", "outputRoutingType": {"name": "Out"},
                "inputRoutingType": {"name": "Out"}, "type": "midi"}
        record = ps.track_record(read, {"trackIndex": 0}, num=4, den=4, by_name=by_name,
                                 groups={})
        ps.link_hazards([record])
        self.assertEqual(sorted(h["type"] for h in record["hazards"]),
                         ["input_ambiguous", "output_ambiguous"])
        self.assertEqual(record["hazards"][0]["candidates"], ["1", "2"])

    def test_routing_kinds_return_resampling_unknown_and_true_external(self):
        """round-2 review: only Live's own external ports are ``external``;
        a return track, Resampling and an unrecognised name are told apart."""
        def route(**fields):
            track = {"id": "9", "name": "T", "type": fields.pop("type", "audio"), **{
                key: {"name": value} for key, value in fields.items()}}
            return ps.classify_routing(track, {"Other": ["5"]}, {},
                                       returns={"A-Reverb": "r0"})
        self.assertEqual(route(inputRoutingType="Ext. In")["input"]["kind"], "external")
        self.assertEqual(route(inputRoutingType="All Ins")["input"]["kind"], "external")
        self.assertEqual(route(outputRoutingType="Ext. Out")["output"]["kind"], "external")
        self.assertEqual(route(inputRoutingType="Resampling")["input"]["kind"], "resampling")
        returned = route(inputRoutingType="A-Reverb", outputRoutingType="A-Reverb")
        self.assertEqual((returned["input"]["kind"], returned["input"]["target_ids"]),
                         ("return", ["r0"]))
        self.assertEqual(returned["output"]["kind"], "return")
        self.assertEqual(route(outputRoutingType="Sends Only")["output"]["kind"], "sends_only")
        port = route(type="midi", outputRoutingType="Moog (USB)")
        self.assertEqual((port["output"]["kind"], port["output"]["target"]),
                         ("unknown", "Moog (USB)"))

    def test_a_midi_track_with_an_unknown_output_is_a_hazard(self):
        def hazards(**fields):
            read = {"id": "3", "name": "Src", "type": fields.pop("type", "midi"),
                    "inputRoutingType": {"name": "All Ins"}, **{
                        key: {"name": value} for key, value in fields.items()}}
            record = ps.track_record(read, {"trackIndex": 0}, num=4, den=4, by_name={},
                                     groups={}, returns={"A-Reverb": "r0"})
            ps.link_hazards([record])
            return sorted(h["type"] for h in record["hazards"])
        self.assertEqual(hazards(outputRoutingType="Moog (USB)"), ["output_unknown"])
        self.assertEqual(hazards(outputRoutingType="Main"), [])
        self.assertEqual(hazards(type="audio", inputRoutingType="Resampling",
                                 outputRoutingType="Main"), ["input_resampling"])
        self.assertEqual(hazards(type="audio", inputRoutingType="A-Reverb",
                                 outputRoutingType="Main"), ["input_from_return"])

    def test_sidechain_edges_become_hazards(self):
        snap = copy.deepcopy(self.snap)
        ps.attach_sidechains(snap, {"tracks": [{"name": "Drum Map"},
                                                {"name": "Grain Pad"}], "returns": [],
                                     "sidechains": [{"source_track": "Drum Map",
                                                     "target_track": "Grain Pad",
                                                     "device": "Compressor2",
                                                     "device_on": True}]},
                             file_path="/x/NOCTURNEv2.als")
        self.assertEqual(hazard_types(snap, "Drum Map"), ["sidechain_source"])
        self.assertEqual(hazard_types(snap, "Grain Pad"), ["sidechain_target"])
        self.assertIn("Grain Pad", {item["track"] for item in snap["hazard_tracks"]})


@on_both_versions
class SidechainEnrichmentTests(unittest.TestCase):
    def test_saved_live_12_set_is_read_with_the_real_reader(self):
        import gzip
        import os
        import tempfile
        from tests.live_set_fixtures import live_set_xml
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Tonight.als")
            with open(path, "wb") as handle:
                handle.write(gzip.compress(live_set_xml(12)))
            device = PieceDevice(live_api=True)
            device.live_api_values["file_path"] = path
            # make the synthetic Set's names meet the snapshot's (unique both sides)
            device.rename_track(3, "Track B")
            device.rename_track(6, "Track C")
            snap = snapshot(device)
        chains = snap["sidechains"]
        self.assertEqual(chains["status"], "from_saved_als", chains)
        self.assertEqual(chains["file_path"], path)
        # Track C ducks Track B: both names unique in the file and the Set
        self.assertEqual([(e["source"], e["target"], e["source_id"], e["target_id"])
                          for e in chains["edges"]],
                         [("Track C", "Track B", "8", "4")])
        # Track A is two tracks in the file: never guessed
        self.assertEqual([(e["source"], e["target"]) for e in chains["unmatched"]],
                         [("Track A", "Track C")])

    def test_a_file_with_several_sets_is_refused(self):
        snap = snapshot(PieceDevice())
        with self.assertRaisesRegex(ps.PieceSnapshotError, "exactly one"):
            ps.sidechains_from_reader(snap, [{}, {}], file_path="/x/pack.alp")

    def test_als_edges_map_by_unique_name_only(self):
        snap = snapshot(PieceDevice())
        set_data = {"tracks": [{"name": "Drum Map"}, {"name": "Grain Pad"},
                               {"name": "Synth A"}, {"name": "Synth A"}],
                    "returns": [],
                    "sidechains": [
                        {"source_track": "Drum Map", "target_track": "Grain Pad",
                         "device": "Compressor2", "device_on": True},
                        {"source_track": "Synth A", "target_track": "Grain Pad",
                         "device": "Compressor2", "device_on": True}]}
        ps.attach_sidechains(snap, set_data, file_path="/x/NOCTURNEv2.als")
        self.assertEqual(snap["sidechains"]["edges"][0]["source_id"], "8")
        self.assertEqual([e["source"] for e in snap["sidechains"]["unmatched"]], ["Synth A"])


if __name__ == "__main__":
    unittest.main()


class CrossBuildSnapshotTests(unittest.TestCase):
    """The same Set read through 2.2.0-rc1 shapes (the capture) and through
    2.3.0 shapes (the capture reshaped: SYNTHETIC, no 2.3 piece capture
    exists) must give the same snapshot: positions come from the path on
    2.3, and nothing downstream may depend on which build answered."""

    def test_snapshot_is_the_same_on_both_builds(self):
        from tests.pp_fakes import V22
        for live_api in (False, True):
            with self.subTest(live_api=live_api):
                old = snapshot(PieceDevice(live_api=live_api, version=V22),
                               read_saved_set=False)
                new_device = PieceDevice(live_api=live_api, version=V23)
                new = snapshot(new_device, read_saved_set=False)
                self.assertEqual(new, old)
                reads = [args for name, args in new_device.wire_calls
                         if name == "ppal-read-track"]
                self.assertTrue(reads)
                self.assertTrue(all("trackIndex" not in args and "trackType" not in args
                                    for args in reads))
                self.assertIn({"path": "mt", "include": ["devices", "mixer"]}, reads)
                self.assertTrue(any(args.get("path") == "rt0" for args in reads))
