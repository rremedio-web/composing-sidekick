"""Self-checks for ALS clip-source ingestion.

Pure stdlib; exercises the parser against a tiny synthetic Live Set so the
arrangement/session/take-lane split cannot silently regress.

Run:
    python3 ingest/selftest_ingest.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "analyze"))

from materialize import summarize_notes
from parse_als import (
    _is_drum_track,
    _store_path,
    from_dict,
    is_main_clip_source,
    is_timeline_clip_source,
    parse_als,
    to_dict,
)
import parse_als as parse_als_mod
from build_index import iter_phrases
from roles import clip_role


def _clip_xml(name: str, pitch: int, start: float = 0.0) -> str:
    return f"""
      <MidiClip>
        <Name Value="{name}" />
        <CurrentStart Value="{start}" />
        <CurrentEnd Value="{start + 4.0}" />
        <Notes>
          <KeyTracks>
            <KeyTrack>
              <MidiKey Value="{pitch}" />
              <Notes>
                <MidiNoteEvent Time="0.0" Duration="1.0" Velocity="100" IsEnabled="true" />
              </Notes>
            </KeyTrack>
          </KeyTracks>
        </Notes>
      </MidiClip>
    """


def _live_set_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Ableton>
  <LiveSet>
    <Tempo><Manual Value="123" /></Tempo>
    <Tracks>
      <MidiTrack>
        <Name><EffectiveName Value="Lead" /></Name>
        <DeviceChain>
          <MainSequencer>
            <ClipTimeable>
              <ArrangerAutomation>
                <Events>
                  {_clip_xml("arrangement", 60, 0.0)}
                </Events>
              </ArrangerAutomation>
            </ClipTimeable>
            <ClipSlotList>
              <ClipSlot>
                <ClipSlot>
                  <Value>
                    {_clip_xml("session", 62, 0.0)}
                  </Value>
                </ClipSlot>
              </ClipSlot>
            </ClipSlotList>
          </MainSequencer>
        </DeviceChain>
        <TakeLanes>
          <TakeLanes>
            <TakeLane>
              <ClipAutomation>
                <Events>
                  {_clip_xml("take", 64, 8.0)}
                </Events>
              </ClipAutomation>
            </TakeLane>
          </TakeLanes>
        </TakeLanes>
      </MidiTrack>
    </Tracks>
    <GroovePool>
      <Grooves>
        <Groove>
          <Clip>
            <Value>
              {_clip_xml("groovepool", 65, 0.0)}
            </Value>
          </Clip>
        </Groove>
      </Grooves>
    </GroovePool>
  </LiveSet>
</Ableton>
"""


def _parse_synthetic_project():
    fd, path = tempfile.mkstemp(suffix=".als")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(_live_set_xml())
        return parse_als(path)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def test_clip_sources() -> None:
    project = _parse_synthetic_project()
    sources = [c.clip_source for c in project.clips]
    assert sources == ["arrangement", "session", "take_lane"], sources
    assert [c.note_count for c in project.clips] == [1, 1, 1]
    assert project.total_notes == 3
    assert project.clip_count == 3

    counts = summarize_notes(to_dict(project))
    assert counts["raw_harmonic_notes"] == 3, counts
    assert counts["harmonic_notes"] == 2, counts
    assert counts["take_lane_notes"] == 1, counts
    assert counts["note_source_counts"] == {
        "arrangement": 1,
        "session": 1,
        "take_lane": 1,
    }, counts


def test_legacy_json_compat() -> None:
    project = from_dict({
        "path": "/tmp/legacy.als",
        "tempo": 120,
        "track_count": 1,
        "midi_track_names": ["Lead"],
        "audio_track_names": [],
        "clip_count": 1,
        "total_notes": 0,
        "audio_clip_count": 0,
        "clips": [{
            "name": "old",
            "track": "Lead",
            "start": 0.0,
            "end": 4.0,
            "note_count": 0,
            "is_drum": False,
            "notes": [],
        }],
    })
    assert project.clips[0].clip_source == "legacy"


def test_source_filters() -> None:
    assert is_main_clip_source("arrangement")
    assert is_main_clip_source("session")
    assert is_main_clip_source("legacy")
    assert not is_main_clip_source("take_lane")
    assert not is_main_clip_source("other_track")

    assert is_timeline_clip_source("arrangement")
    assert not is_timeline_clip_source("session")
    assert not is_timeline_clip_source("legacy")
    assert not is_timeline_clip_source("take_lane")


def _group_track_live_set_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Ableton>
  <LiveSet>
    <Tempo><Manual Value="120" /></Tempo>
    <Tracks>
      <GroupTrack>
        <Name><EffectiveName Value="Synth Group" /></Name>
        <Tracks>
          <MidiTrack>
            <Name><EffectiveName Value="Nested Lead" /></Name>
            <DeviceChain>
              <MainSequencer>
                <ClipTimeable>
                  <ArrangerAutomation>
                    <Events>
                      {_clip_xml("nested_arr", 67, 0.0)}
                    </Events>
                  </ArrangerAutomation>
                </ClipTimeable>
              </MainSequencer>
            </DeviceChain>
          </MidiTrack>
        </Tracks>
      </GroupTrack>
    </Tracks>
  </LiveSet>
</Ableton>
"""


def test_group_track_nested_midi() -> None:
    fd, path = tempfile.mkstemp(suffix=".als")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(_group_track_live_set_xml())
        project = parse_als(path)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    assert project.track_count == 1, project.track_count
    assert project.midi_track_names == ["Nested Lead"]
    assert project.clip_count == 1
    assert project.clips[0].track == "Nested Lead"
    assert project.clips[0].name == "nested_arr"
    assert project.clips[0].note_count == 1
    assert project.total_notes == 1


def _fake_track() -> ET.Element:
    return ET.Element("MidiTrack")


def test_drum_keyword_hardening() -> None:
    track = _fake_track()
    cases = {
        "What Pad": False,
        "Chatham Keys": False,
        "Override Bass": False,
        "909 Lead": False,
        "Drums": True,
        "808 Kit": True,
        "909": True,
    }
    for name, expected in cases.items():
        got = _is_drum_track(track, name)
        assert got is expected, f"{name!r}: expected {expected}, got {got}"

    rack = ET.Element("MidiTrack")
    ET.SubElement(rack, "DrumGroupDevice")
    assert _is_drum_track(rack, "Serum") is True


def test_drum_track_overrides() -> None:
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        with open(path, "w") as fh:
            json.dump({"what pad": True, "drums": False}, fh)
        parse_als_mod._DRUM_OVERRIDES_TRIED = False
        parse_als_mod._DRUM_OVERRIDES_CACHE = None
        orig = parse_als_mod._store_path
        parse_als_mod._store_path = (
            lambda filename: path if filename == "drum_track_overrides.json" else orig(filename)
        )
        try:
            track = _fake_track()
            assert _is_drum_track(track, "What Pad") is True
            assert _is_drum_track(track, "Drums") is False
        finally:
            parse_als_mod._store_path = orig
            parse_als_mod._DRUM_OVERRIDES_TRIED = False
            parse_als_mod._DRUM_OVERRIDES_CACHE = None
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    assert not os.path.exists(_store_path("drum_track_overrides.json"))


def _clip_xml_multi_notes(name: str, events: list[tuple[int, float, float]]) -> str:
    """Build a MidiClip with explicit (pitch, start, duration) note events."""
    by_pitch: dict[int, list[tuple[float, float]]] = {}
    for pitch, start, duration in events:
        by_pitch.setdefault(pitch, []).append((start, duration))
    key_tracks = []
    for pitch, note_events in sorted(by_pitch.items()):
        note_xml = "\n".join(
            f'                <MidiNoteEvent Time="{start}" Duration="{duration}" '
            f'Velocity="100" IsEnabled="true" />'
            for start, duration in note_events
        )
        key_tracks.append(
            f"""            <KeyTrack>
              <MidiKey Value="{pitch}" />
              <Notes>
{note_xml}
              </Notes>
            </KeyTrack>"""
        )
    return f"""
      <MidiClip>
        <Name Value="{name}" />
        <CurrentStart Value="0.0" />
        <CurrentEnd Value="4.0" />
        <Notes>
          <KeyTracks>
{chr(10).join(key_tracks)}
          </KeyTracks>
        </Notes>
      </MidiClip>
    """


def test_invalid_duration_notes_dropped() -> None:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Ableton>
  <LiveSet>
    <Tempo><Manual Value="120" /></Tempo>
    <Tracks>
      <MidiTrack>
        <Name><EffectiveName Value="Lead" /></Name>
        <DeviceChain>
          <MainSequencer>
            <ClipTimeable>
              <ArrangerAutomation>
                <Events>
                  {_clip_xml_multi_notes("bad_durations", [
                      (60, 0.0, 0.0),
                      (61, 0.5, 2.6e-7),
                      (62, 1.0, -0.5),
                      (64, 2.0, 1.0),
                      (65, 3.0, 0.5),
                  ])}
                </Events>
              </ArrangerAutomation>
            </ClipTimeable>
          </MainSequencer>
        </DeviceChain>
      </MidiTrack>
    </Tracks>
  </LiveSet>
</Ableton>
"""
    fd, path = tempfile.mkstemp(suffix=".als")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(xml)
        project = parse_als(path)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    assert len(project.clips) == 1
    clip = project.clips[0]
    assert clip.note_count == 2
    assert project.total_notes == 2
    kept = {(n.pitch, n.start, n.duration) for n in clip.notes}
    assert kept == {(64, 2.0, 1.0), (65, 3.0, 0.5)}


def test_phrase_window_sustained_and_role() -> None:
    sustained_clip = {
        "name": "pad",
        "track": "Pad",
        "start": 0.0,
        "end": 16.0,
        "note_count": 4,
        "is_drum": False,
        "clip_source": "arrangement",
        "notes": [
            {"pitch": 60, "start": -2.0, "duration": 10.0, "velocity": 80, "enabled": True},
            {"pitch": 62, "start": 0.0, "duration": 1.0, "velocity": 80, "enabled": True},
            {"pitch": 64, "start": 1.0, "duration": 1.0, "velocity": 80, "enabled": True},
            {"pitch": 65, "start": 2.0, "duration": 1.0, "velocity": 80, "enabled": True},
        ],
    }
    role_clip = {
        "name": "mixed",
        "track": "Keys",
        "start": 0.0,
        "end": 16.0,
        "note_count": 8,
        "is_drum": False,
        "clip_source": "arrangement",
        "notes": (
            [{"pitch": 36, "start": float(i), "duration": 0.5, "velocity": 90, "enabled": True}
             for i in range(4)]
            + [{"pitch": 72, "start": 8.0 + float(i), "duration": 0.5, "velocity": 90, "enabled": True}
               for i in range(4)]
        ),
    }
    data = {"path": "/tmp/phrase_window.als", "tempo": 120.0, "clips": [sustained_clip, role_clip]}
    phrases = list(iter_phrases(data))
    pad_phrase = next(p for p in phrases if p["track"] == "Pad" and p["id"].endswith("|w0"))
    sustained = [n for n in pad_phrase["notes"] if n["pitch"] == 60]
    assert sustained, pad_phrase["notes"]
    assert sustained[0]["start"] == 0.0
    assert sustained[0]["duration"] == 10.0

    all_notes = role_clip["notes"]
    assert clip_role(all_notes, False) == "lead"
    bass_phrase = next(p for p in phrases if p["id"].endswith("|w0") and p["track"] == "Keys")
    lead_phrase = next(p for p in phrases if p["id"].endswith("|w8") and p["track"] == "Keys")
    assert bass_phrase["role"] == "bass"
    assert lead_phrase["role"] == "lead"
    assert bass_phrase["role"] != clip_role(all_notes, False)


def main() -> int:
    test_clip_sources()
    test_legacy_json_compat()
    test_source_filters()
    test_group_track_nested_midi()
    test_drum_keyword_hardening()
    test_drum_track_overrides()
    test_invalid_duration_notes_dropped()
    test_phrase_window_sustained_and_role()
    print("ingest selftest passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
