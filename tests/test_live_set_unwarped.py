"""Unwarped audio clips store loop points in seconds (Live 11/12 element paths)."""
import json
import tempfile
import unittest
from pathlib import Path

from tools import live_set_reader as lsr


def _audio_clip(loop_end, warped):
    return (f'<AudioClip Time="0"><CurrentStart Value="0"/><CurrentEnd Value="{loop_end}"/>'
            f'<Loop><LoopStart Value="0"/><LoopEnd Value="{loop_end}"/>'
            f'<StartRelative Value="0"/><LoopOn Value="false"/><OutMarker Value="{loop_end}"/></Loop>'
            f'<Name Value="Stem"/><IsWarped Value="{"true" if warped else "false"}"/>'
            f'<WarpMode Value="4"/></AudioClip>')


def _set(clip_a, clip_b):
    slots = "".join(
        f'<ClipSlot Id="{i}"><ClipSlot><Value>{c}</Value></ClipSlot></ClipSlot>'
        for i, c in enumerate((clip_a, clip_b)))
    return ('<?xml version="1.0"?><Ableton Creator="Ableton Live 12.1"><LiveSet>'
            '<Tracks><AudioTrack Id="7"><Name><EffectiveName Value="Stems"/></Name>'
            '<TrackGroupId Value="-1"/><DeviceChain><Mixer><Volume><Manual Value="1"/></Volume>'
            '<Speaker><Manual Value="true"/></Speaker></Mixer>'
            f'<MainSequencer><ClipSlotList>{slots}</ClipSlotList></MainSequencer>'
            '<DeviceChain><Devices/></DeviceChain></DeviceChain></AudioTrack></Tracks>'
            '<MainTrack><DeviceChain><Mixer><Tempo><Manual Value="120"/></Tempo>'
            '<TimeSignature><Manual Value="201"/></TimeSignature></Mixer></DeviceChain></MainTrack>'
            '<Scenes><Scene Id="0"><Name Value="A"/></Scene><Scene Id="1"><Name Value="B"/></Scene></Scenes>'
            '</LiveSet></Ableton>').encode()


class UnwarpedAudioTests(unittest.TestCase):
    def setUp(self):
        # scene A: unwarped, 16 s; scene B: warped, 16 beats
        self.data = lsr.read_live_set(_set(_audio_clip(16, False), _audio_clip(16, True)))[0]
        self.track = next(t for t in self.data["tracks"] if t["name"] == "Stems")

    def test_unwarped_clip_reports_seconds_not_bars(self):
        unwarped = self.track["session"][0]
        self.assertFalse(unwarped["is_warped"])
        self.assertIsNone(unwarped["loop_bars"])
        self.assertEqual(unwarped["loop_seconds"], 16)
        # 16 s at 120 BPM = 32 beats = 8 bars of 4/4 — an estimate only
        self.assertEqual(unwarped["loop_bars_estimate"], 8)
        self.assertIsNone(unwarped["warp_mode"])

    def test_warped_clip_is_measured_in_beats(self):
        warped = self.track["session"][1]
        self.assertTrue(warped["is_warped"])
        self.assertEqual(warped["loop_bars"], 4)

    def test_estimates_never_enter_measured_loop_lengths(self):
        self.assertEqual(self.data["loop_lengths_bars"], [4])
        row = next(iter(self.data["scene_matrix"].values()))
        self.assertEqual(row["loop_bars"], [8, 4])
        self.assertEqual(row["estimated_scenes"], [0])


class MalformedSidecarTests(unittest.TestCase):
    def test_non_object_sidecar_is_ignored_by_summary(self):
        with tempfile.TemporaryDirectory() as d:
            ref = Path(d) / "r-x"
            ref.mkdir()
            (ref / "set_analysis.json").write_text(json.dumps([1, 2]))
            card = {"ref_id": "r-x", "out_dir": str(ref)}
            self.assertIsNone(lsr._set_evidence_summary(card, audio_refs_root=d)
                              if "audio_refs_root" in lsr._set_evidence_summary.__code__.co_varnames
                              else lsr._set_evidence_summary(card))


if __name__ == "__main__":
    unittest.main()
