# Producer Pal replay fixtures

Recorded MCP replies from the Producer Pal device, one directory per build
(`2.2.0-rc1`, `2.3.0`, `2.4.0`, plus `-compact` variants). `tests/pp_fakes.py` replays
them, so the transport, codec and write-verification code is tested against the device's
real reply *envelopes*: keys, error and warning shapes, id and path forms, orderings.

## What is synthetic

- **Names.** User-given track, clip, locator, preset and routing names were replaced with
  invented ones, consistently across every build. Stock Live and device labels
  (`Main`, `All Ins`, `N-MIDI`, instrument class strings, `SIDEKICK …`, capture names)
  are unchanged, because routing, kit and hazard detection key on them.
- **Context text.** The project, global and memory context returned by `ppal-connect` /
  `ppal-context` is placeholder text.
- **Notes of the `2.2.0-rc1` piece run** (`piece/`, `piece-notes/`, `read/read_track_t*`).
  The notes were newly drawn, clip by clip and bar by bar:
  - onsets are new positions inside the same bar and the same loop and read window as
    before, so window, coverage and visibility behaviour is unchanged;
  - pitch classes are reassigned within each bar, with new octaves inside the clip's
    register, so each bar keeps its pitch-class content while the lines, order and
    registers are new;
  - durations end no later than before, and velocities are new.

  No `(pitch, onset, duration)` note matches one in its corresponding original clip
  (cross-clip coincidences of ordinary quantized values remain at chance level). Every
  encoding (the Live API note arrays and Producer Pal's compact read strings) is
  rendered from the same note model, so `tests/piece_notes_truth.py` still checks the
  readers against an independent model.

## What is kept

The arrangement skeleton of the piece run: track list and order, clip ids, positions,
lengths, loop and marker windows, mutes, and the per-bar harmonic outline those notes
fill (a C drone, a C Phrygian middle, a C♯ held alone, F♯ after a silent gap). The
harmony and arrangement tests in `tests/test_ref_develop.py` are written against that
outline.

The `2.3.0` and `2.4.0` captures were made on disposable SCRATCH Sets.
