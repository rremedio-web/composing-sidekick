# The idiom — who this music is (template)

_Fill this from the owner's ear-confirmed notes (`store/NOTES.md`), the public record of
their releases, and measured corpus DNA (`store/style_profile.json`, `STYLE.md`). Label
every line **owner-confirmed**, **public record** or **measured** (with the profile build
date). Facts here are measured or confirmed, not vibes. The public repository ships this
file empty of any particular composer._

## Identity in one breath
The genre and lineage in the owner's own words; the one sentence they would accept.
Which scene labels they reject.

## Harmonic home
- Mode(s) and the gravity that replaces cadence (plagal motion, modal colour shifts…).
- The signature oscillations, with counts from `harmony.json` (e.g. "i↔iv ×N").
- The colour move — the chord that is a shadow, not a modulation.
- Rare-for-them progressions, cited as deliberate spice.

## Melodic vocabulary
- The scale degrees the lead lines actually speak; the landing gesture.
- Mono vs polyphonic habits; what repetition means in this idiom.
- Recurring cells with LOW support (dormant moves, good for growth and challenges):
  list each as degrees + gesture name + track count. `mcp_server/idiom_cells.py` cites
  this section as the provenance of its calibrated cells; recalibrating the engine is a
  scoped, auditioned change, never a doc edit.
- Genuinely under-touched in-key notes (landing on one is an event).

## Time and touch (the measured feel)
- Tempo worlds (from `structure.json` / the profile), typical floor and surface.
- Off-grid share per role, with the profile build date; machine parts vs played parts.
  `idiom_cells.FEEL` cites this section — it is frozen engine calibration; state the
  measurement the engine was tuned on and the current profile reading separately.
- Approved drum language(s) (owner-confirmed, dated) and their scope: one language is
  not a universal rule; ask which language a piece wants.
- Drum habits heard in the owner's own records (model-heard, not yet owner-confirmed —
  ask before leaning on them).
- How their syncopation is made (composed, or by a send/delay return) — owner-confirmed.
- Velocity contour: mean, spread, downbeat accent; what flat velocity reads as.
- Automation language from the harvest (`store/automation.json`): rides, throws, sweeps.

## Aesthetic laws
- What counts as a feature (space, repetition, decay…) and what counts as cliché in
  the owner's world: a whitelist and a blacklist, in their words where possible.

## Eras (direction axes, not archaeology)
- What the profile says about the catalogue's breadth (major share, tempo clusters,
  interval size) with the build date and extractor caveats — never collapse the owner to
  one era.
- Steer by DIRECTION: **darker** · **clubbier** · **sparser** · **weirder** (see
  `tools/build_directions.py`). Gear 6 COVER = translate material from one direction's
  language into another's, keeping the bones (contour + harmonic skeleton).
