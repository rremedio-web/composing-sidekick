"""Synthetic Ableton Live Set XML with the element paths real Sets use.

Origin of every path below (read-only inspection, nothing copied):
- Live 8: an Ableton Live 8.2.1 .als — parameters stored as
  ``<P><ArrangerAutomation><Events><FloatEvent Time="-63072000" Value=…/>``,
  tempo at ``MasterTrack/MasterChain/Mixer/Tempo``, scenes at
  ``SceneNames/Scene[@Value]``, track names at ``Name/UserName``.
- Live 11: an Ableton Live 11.3.25 .als — parameters as ``<P><Manual Value=…/>``,
  tempo at ``MasterTrack/DeviceChain/Mixer/Tempo``, scenes at
  ``Scenes/Scene/Name[@Value]``, names at ``Name/EffectiveName`` with an empty
  ``Name/UserName``.
- Live 12: an Ableton Live 12.4.3 .als — as Live 11 but ``MainTrack`` replaces
  ``MasterTrack``.
Shared by all three: ``Tracks/<Kind>Track[@Id]``, ``TrackGroupId``,
``DeviceChain/Mixer/{Volume,Speaker,Sends/TrackSendHolder/Send}``,
``DeviceChain/MainSequencer/ClipSlotList/ClipSlot/ClipSlot/Value/<Clip>``,
arrangement clips under ``MainSequencer/{Sample,ClipTimeable}/ArrangerAutomation/
Events``, devices under ``DeviceChain/DeviceChain/Devices``, racks under
``Branches/<Branch>/DeviceChain/<Chain>/Devices``, sidechains under
``SideChain/{OnOff,RoutedInput/Routable/Target}``, plugin names under
``PluginDesc/{VstPluginInfo/PlugName,Vst3PluginInfo/Name,AuPluginInfo/Name}``.
Names and values are generic.
"""
from __future__ import annotations

FLT_MAX = "340282326356119256160033759537265639424"  # Compressor2 Ratio at "inf"


def _p(version: int, tag: str, value, event: str = "FloatEvent") -> str:
    if version == 8:
        return (f'<{tag}><ArrangerAutomation><Events>'
                f'<{event} Time="-63072000" Value="{value}"/>'
                f'</Events></ArrangerAutomation></{tag}>')
    return f'<{tag}><LomId Value="0"/><Manual Value="{value}"/></{tag}>'


def _b(version: int, tag: str, value: bool) -> str:
    return _p(version, tag, "true" if value else "false", "BoolEvent")


def _name(version: int, name: str) -> str:
    if version == 8:
        return f'<Name><UserName Value="{name}"/><Annotation Value=""/></Name>'
    return (f'<Name><EffectiveName Value="{name}"/><UserName Value=""/>'
            f'<Annotation Value=""/></Name>')


def _sidechain(version: int, target: str, on: bool = True) -> str:
    return (f'<SideChain>{_b(version, "OnOff", on)}<RoutedInput><Routable>'
            f'<Target Value="{target}"/><UpperDisplayString Value="Stale Name"/>'
            f'<LowerDisplayString Value="Post FX"/></Routable>'
            f'{_p(version, "Volume", 1)}</RoutedInput></SideChain>')


def _compressor(version, *, threshold, ratio, gain, attack, release, sidechain=""):
    return (f'<Compressor2 Id="0">{_b(version, "On", True)}<UserName Value=""/>'
            f'{_p(version, "Threshold", threshold)}{_p(version, "Ratio", ratio)}'
            f'{_p(version, "Gain", gain)}{_p(version, "Attack", attack)}'
            f'{_p(version, "Release", release)}'
            f'{sidechain or _sidechain(version, "AudioIn/None", False)}</Compressor2>')


def _mixer(version, *, volume, sends=(), on=True):
    holders = "".join(
        f'<TrackSendHolder Id="{i}">{_p(version, "Send", v)}'
        f'<Active Value="true"/></TrackSendHolder>' for i, v in enumerate(sends))
    return (f'<Mixer>{_b(version, "On", True)}<Sends>{holders}</Sends>'
            f'{_b(version, "Speaker", on)}{_p(version, "Pan", 0)}'
            f'{_p(version, "Volume", volume)}</Mixer>')


def _slot(clip: str = "") -> str:
    inner = f'<Value>{clip}</Value>' if clip else '<Value/>'
    return f'<ClipSlot><ClipSlot>{inner}</ClipSlot><HasStop Value="true"/></ClipSlot>'


def _clip(kind, name, loop_end, *, loop_on=True, warp=None, sample_volume=None,
          start=0, end=None):
    extra = ""
    if warp is not None:
        extra += f'<WarpMode Value="{warp}"/>'
    if sample_volume is not None:
        extra += f'<SampleVolume Value="{sample_volume}"/>'
    end = loop_end if end is None else end
    return (f'<{kind} Time="{start}"><CurrentStart Value="{start}"/>'
            f'<CurrentEnd Value="{end}"/><Loop><LoopStart Value="0"/>'
            f'<LoopEnd Value="{loop_end}"/><StartRelative Value="0"/>'
            f'<LoopOn Value="{"true" if loop_on else "false"}"/>'
            f'<OutMarker Value="{loop_end}"/></Loop><Name Value="{name}"/>'
            f'{extra}</{kind}>')


def _track(version, tag, tid, name, group, *, devices="", slots=(), sends=(),
           volume=1, arrangement="", on=True):
    seq_child = "Sample" if tag == "AudioTrack" else "ClipTimeable"
    return (f'<{tag} Id="{tid}">{_name(version, name)}'
            f'<TrackGroupId Value="{group}"/><DeviceChain>'
            f'<AudioOutputRouting><Target Value="AudioOut/Master"/></AudioOutputRouting>'
            f'{_mixer(version, volume=volume, sends=sends, on=on)}'
            f'<MainSequencer><ClipSlotList>{"".join(_slot(c) for c in slots)}'
            f'</ClipSlotList><{seq_child}><ArrangerAutomation><Events>{arrangement}'
            f'</Events></ArrangerAutomation></{seq_child}></MainSequencer>'
            f'<DeviceChain><Devices>{devices}</Devices></DeviceChain>'
            f'</DeviceChain></{tag}>')


def _plugin(version: int) -> str:
    if version == 8:
        return ('<PluginDevice Id="3">{on}<UserName Value=""/><PluginDesc>'
                '<VstPluginInfo Id="0"><PlugName Value="Plugin V"/></VstPluginInfo>'
                '</PluginDesc></PluginDevice>').format(on=_b(version, "On", True))
    if version == 11:
        return ('<AuPluginDevice Id="3">{on}<UserName Value=""/><PluginDesc>'
                '<AuPluginInfo Id="0"><Name Value="Plugin A"/>'
                '<Manufacturer Value="Maker"/></AuPluginInfo></PluginDesc>'
                '</AuPluginDevice>').format(on=_b(version, "On", True))
    return ('<PluginDevice Id="3">{on}<UserName Value=""/><PluginDesc>'
            '<Vst3PluginInfo Id="0"><Name Value="Plugin V3"/></Vst3PluginInfo>'
            '</PluginDesc></PluginDevice>').format(on=_b(version, "On", True))


def live_set_xml(version: int, *, time_signature: int = 201, tempo: float = 100,
                 scenes=("Intro", "Main", "")) -> bytes:
    """A 3-scene Set: a group, two same-named MIDI tracks, two audio tracks
    (one ducked by a later-defined track, one with a sidechained compressor
    inside a rack), one return with a compressor and a switched-off reverb."""
    v = version
    n = len(scenes)
    if v == 8:
        scene_xml = ("<SceneNames>" + "".join(
            f'<Scene Value="{s}"><Annotation Value=""/></Scene>' for s in scenes)
            + "</SceneNames>")
        master = (f'<MasterTrack>{_name(v, "")}<MasterChain>'
                  f'<Mixer>{_p(v, "Tempo", tempo)}'
                  f'{_p(v, "TimeSignature", time_signature, "EnumEvent")}</Mixer>'
                  f'<DeviceChain><Devices/></DeviceChain></MasterChain></MasterTrack>')
    else:
        scene_xml = ("<Scenes>" + "".join(
            f'<Scene Id="{i}"><Name Value="{s}"/><Tempo Value="120"/></Scene>'
            for i, s in enumerate(scenes)) + "</Scenes>")
        main = "MasterTrack" if v == 11 else "MainTrack"
        master = (f'<{main}>{_name(v, "Main")}<DeviceChain><Mixer>'
                  f'{_p(v, "Tempo", tempo)}{_p(v, "TimeSignature", time_signature)}'
                  f'</Mixer><DeviceChain><Devices/></DeviceChain></DeviceChain></{main}>')
    group_slots = "<Slots>" + "<GroupTrackSlot/>" * n + "</Slots>"
    group = (f'<GroupTrack Id="10">{_name(v, "Group A")}<TrackGroupId Value="-1"/>'
             f'{group_slots}<DeviceChain>{_mixer(v, volume=1, sends=[0.00031622799])}'
             f'<DeviceChain><Devices/></DeviceChain></DeviceChain></GroupTrack>')
    erosion = (f'<Erosion Id="0">{_b(v, "On", True)}<UserName Value=""/>'
               f'{_p(v, "Mode", 0, "EnumEvent")}{_p(v, "Freq", 5000)}'
               f'{_p(v, "Amplitude", 100)}</Erosion>')
    autofilter = (f'<AutoFilter Id="1">{_b(v, "On", True)}<UserName Value=""/>'
                  f'{_p(v, "FilterType", 0, "EnumEvent")}{_p(v, "Cutoff", 60)}'
                  f'{_p(v, "Resonance", 0.3)}</AutoFilter>')
    midi_a = _track(v, "MidiTrack", 20, "Track A", 10,
                    devices=erosion + autofilter,
                    slots=[_clip("MidiClip", "Pulse", 8), _clip("MidiClip", "Pulse", 8), ""],
                    sends=[0.5])
    beat_repeat = (f'<BeatRepeat Id="0">{_b(v, "On", True)}<UserName Value=""/>'
                   f'{_p(v, "Chance", 0.5)}{_p(v, "Interval", 4)}{_p(v, "Grid", 8)}'
                   f'{_p(v, "Gate", 4)}</BeatRepeat>')
    midi_b = _track(v, "MidiTrack", 21, "Track A", 10, devices=beat_repeat,
                    slots=["", "", _clip("MidiClip", "Odd", 20)], sends=[0.75])
    ducked = _compressor(v, threshold=0.05, ratio=FLT_MAX, gain=6, attack=2,
                         release=40, sidechain=_sidechain(v, "AudioIn/Track.40/PostFxOut"))
    audio_b = _track(v, "AudioTrack", 30, "Track B", -1, devices=ducked,
                     slots=[_clip("AudioClip", "Air", 12, warp=4,
                                  sample_volume=0.1)] + [""] * (n - 1),
                     sends=[1.0], volume=0.5,
                     arrangement=_clip("AudioClip", "Air", 12, start=0, end=32,
                                       warp=4, sample_volume=0.1))
    rack_comp = _compressor(v, threshold=0.2, ratio=2, gain=3, attack=5,
                            release=80,
                            sidechain=_sidechain(v, "AudioIn/Track.21/PostFxOut"))
    rack = (f'<AudioEffectGroupDevice Id="0">{_b(v, "On", True)}<UserName Value="Rack"/>'
            f'<Branches><AudioEffectBranch Id="0"><DeviceChain><AudioToAudioDeviceChain>'
            f'<Devices>{rack_comp}</Devices></AudioToAudioDeviceChain></DeviceChain>'
            f'</AudioEffectBranch></Branches></AudioEffectGroupDevice>')
    gate = (f'<Gate Id="1">{_b(v, "On", True)}<UserName Value=""/>'
            f'{_p(v, "Threshold", 0.05)}{_p(v, "Attack", 1)}{_p(v, "Hold", 20)}'
            f'{_p(v, "Release", 30)}{_p(v, "Gain", -40)}'
            f'{_sidechain(v, "AudioIn/Track.20/PostFxOut", False)}'
            f'{_b(v, "Flip", True)}</Gate>')
    audio_c = _track(v, "AudioTrack", 40, "Track C", -1, devices=rack + gate + _plugin(v),
                     slots=["", _clip("AudioClip", "Wall", 64, loop_on=False, warp=0)] + [""] * (n - 2),
                     sends=[0.25], on=False)
    reverb = (f'<Reverb Id="1">{_b(v, "On", False)}<UserName Value=""/>'
              f'{_p(v, "DecayTime", 1000)}{_p(v, "RoomSize", 50)}'
              f'{_p(v, "MixDirect", 0.5)}</Reverb>')
    ret_comp = _compressor(v, threshold=0.1, ratio=4, gain=-6,
                           attack=1, release=50)
    ret = (f'<ReturnTrack Id="50">{_name(v, "Return A")}<TrackGroupId Value="-1"/>'
           f'<DeviceChain>{_mixer(v, volume=1, sends=[0.00031622799])}'
           f'<DeviceChain><Devices>{ret_comp}{reverb}</Devices></DeviceChain>'
           f'</DeviceChain></ReturnTrack>')
    creator = {8: "Ableton Live 8.2.1", 11: "Ableton Live 11.3.25",
               12: "Ableton Live 12.4.3"}[v]
    locators = ('<Locators><Locators><Locator Id="0"><Time Value="16"/>'
                '<Name Value="Mark"/></Locator></Locators></Locators>')
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
           f'<Ableton MajorVersion="5" MinorVersion="x" Creator="{creator}">'
           f'<LiveSet><Tracks>{group}{midi_a}{midi_b}{audio_b}{audio_c}{ret}</Tracks>'
           f'{master}{scene_xml}{locators}</LiveSet></Ableton>')
    return xml.encode("utf-8")


def preset_xml() -> bytes:
    """A device preset document as Live Packs embed them next to Sets (an
    ``<Ableton>`` root without a ``LiveSet``)."""
    return (b'<?xml version="1.0" encoding="UTF-8"?>\n<Ableton MajorVersion="4" '
            b'Creator="Ableton Live 8.2.1"><AudioEffectGroupDevicePreset>'
            b'<Device/></AudioEffectGroupDevicePreset></Ableton>')
