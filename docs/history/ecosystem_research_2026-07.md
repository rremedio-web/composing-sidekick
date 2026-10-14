# Ableton / AI-music tooling ecosystem report (2026-07-03)

Researched for composing-sidekick: what exists beyond our stack (Producer Pal,
hand-forged M4L, own .als parser, Bradley-Terry taste model) that could concretely
help. Grouped by lane; "top 5 actionable" at the end. Full source links at bottom
of each entry's lane.

## Lane 1 — Control surfaces / remote scripts / Live API bridges

- **AbletonOSC** (ideoforms) — LOM over OSC (11000/11001), maintained through 2025,
  Live 11+/12. Second M4L-independent control channel; roughly the same LOM surface
  Producer Pal reaches → low marginal value for us.
- **pylive** (ideoforms) — Python wrapper over AbletonOSC; maintained. Quickest way
  to script Live from Python without MCP for batch ops.
- **ableton-js** (leolabs) — Node Live bridge, v4.1.0 ~May 2026, Live 12 tested.
  Relevant mainly because ableton-copilot-mcp builds on it.
- **ClyphX Pro** (paid) — macro action strings, Live 12.1/Py3 current. User-side
  "panic buttons" from a MIDI controller; not a server API.
- **Ziforge/ableton-liveapi-tools** — 220 LiveAPI tools over TCP/JSON, incl.
  **take lanes (Live 12+), groove pool, browser/device loading, arrangement
  control**. v1.0.0 Oct 2025, GPLv3 → capability MAP only, don't vendor.

**KEY FINDING:** Live 12 LOM exposes `GroovePool` (enumerable grooves), `TakeLane`
(`create_midi_clip()` at a beat position — **arrangement take-lane MIDI writes are
possible from an M4L device today**), and `Clip.quantize(grid, amount)` /
`quantize_pitch()`. The gap is that Producer Pal doesn't surface these — a small
companion .amxd from our forge could add take-lane writes, groove-pool reads,
locator ops, quantize.

## Lane 2 — MIDI / symbolic Python libraries

- **symusic** — C++ core, 100–500× faster MIDI parsing; v0.6.0 Apr 2026, very
  active. Corpus-wide recomputes become background ops.
- **miditok** — MIDI tokenizers for DL models (REMI/TSD), v3.0.6 active; the bridge
  if we feed the corpus to any transformer/RWKV.
- **mido** stable-maintenance (keep) · **pretty_midi** frozen 2023 (legacy only) ·
  **muspy** dormant (skip) · **music21** heavyweight, classical-leaning (deep
  harmonic passes only) · **miditoolkit** superseded by symusic (migrate away).
- **partitura** (CPJKU) — performance features (onset deviation, velocity curves,
  articulation), actively maintained; complements our timing-DNA extraction.

## Lane 3 — Local/open symbolic music AI

- **Magenta classic** — main repo ARCHIVED Jan 2026; note-seq lives; Magenta Studio
  2.0 M4L devices frozen TF-era. Dead end for corpus conditioning.
- **Magenta RealTime / RT2** (2025) — open-weights live AUDIO models, steerable in
  real time; the only credible open "jam partner that listens" — texture
  co-improviser on a bus, not symbolic.
- **MIDI-RWKV** (June 2025) — RWKV-7 long-context multitrack symbolic INFILLING,
  trained on GigaMIDI, **built for personalization via state tuning in the
  low-sample regime typical of individual composers, CPU-trainable**. Weights +
  code public. Closest existing thing to what this project wants: state-tune on
  the owner's corpus, infill as call-and-response, taste model reranks.
- **MIDI-GPT** (Metacreation, Jan 2025) — controllable multitrack infilling with
  attribute controls (density, polyphony), runs on personal machines; MIT code,
  RAIL-M weights. Heavier alternative to MIDI-RWKV.
- **Anticipatory Music Transformer** (Stanford) — Apache-2.0 infilling baseline;
  superseded for our use by MIDI-RWKV.
- **NotaGen** (IJCAI 2025) — classical-leaning, BUT its **CLaMP-DPO** recipe
  (preference optimization) is the playbook for graduating our taste model from
  reranker to training signal.
- **MIDI-LLM** — Llama-3.2-1B text-to-MIDI; provocation oracle, no personalization.
- **Ableton Live 12.3 AI** — stem separation in-DAW (useful for mining his audio),
  Splice search-by-sound; MIDI "Generators by Iftah" are algorithmic toys.

## Lane 4 — Max for Live ecosystems

- **Ableton/maxdevtools `amxd_textconv.py`** — OFFICIAL .amxd header parser /
  maxpat extractor (built for git diffing). Use as validator/round-trip check for
  our forged devices. No official builder CLI exists; our forge is state of the art.
- Pattern Engine / GENE / Iftah Generators — generative sequencers; none is a
  call-and-response partner. That niche is unoccupied — the sidekick IS the product.
- Magenta Studio 2.0 — only in-Live ML groove tools (Groove/Drumify), frozen.

## Lane 5 — Groove / feel research

- **GrooVAE** — archived TF1 repo, painful locally; the M4L wrappers still run.
  Our own timing-DNA system is the better long-term bet.
- **GigaMIDI NOMML/DNODR/DNVR heuristics** (TISMIR 2025) — simple non-ML ratios
  classifying expressive vs mechanical MIDI at ~100% acc. Run over our 25,600
  phrases to tag performed-feel vs grid-programmed; complements the junk floor and
  selects groove-extraction candidates. (Falsification gate before any scoring use.)
- **partitura** — formalizes groove features if ever needed publishable-grade.
- **Quantize primitives exist at every layer**: LOM Clip.quantize from M4L, groove
  pool enumerable, symusic grid ops — a quantize/dequantize (re-humanize from
  timing DNA) tool pair is buildable today.

## Lane 6 — MCP + DAWs beyond Producer Pal

- **ahujasid/ableton-mcp** — most-starred; Session-focused, arrangement thin.
  Dec 2025 hands-on: usable but immature; its fork's **.als automation-injection
  trick** (tempo/volume automation + warp markers written into the gzip XML) is
  stealable for our parser — automation writes with no live API.
- **xiaolaa2/ableton-copilot-mcp** — explicit Arrangement View ops on ableton-js;
  best arrangement-write reference among peers.
- **LofiFren/ableton-mcp-lofifren** — ~35 tools; arrangement BETA: clips to
  timeline, loop, **locators**, scenes→arrangement bounce. Proof our lay_the_song
  lane works via remote script too.
- Others (uisato extended, nicholasbien pro, jpoindexter 200+ tools, OSC servers):
  none support take lanes; younger than Producer Pal. Mine for tool ideas only.

## Lane 7 — LLM+MIDI products

- **MIDI Agent** — commercial VST: prompt→MIDI with BYO API key. Competitor
  datapoint; corpus-grounding beats it.
- **Composer's Assistant 2** — free local multitrack infilling in REAPER; its
  fine-grained control UI is the best reference for "controls a composer wants."

## Lane 8 — Audio analysis (closing the "can't hear itself" gap)

- **librosa** (active) — onset/spectral judging of rendered candidates.
- **Essentia** (AGPLv3) — pretrained mood/texture taggers for his stems → an
  audio-side vocabulary of the owner's sound.
- **Demucs (htdemucs)** — local stem separation: mine FINISHED tracks (lost .als
  eras, e.g. released material) back into stems → audio-to-MIDI → phrase library.
- **LAION-CLAP** — "vibe search" over samples/stems ("cold air, distant
  machinery"); cheap is-this-the-owner audio-similarity metric.
- **madmom** — downbeat detection for imported loops (frozen but works).

## Top 5 most actionable

1. **MIDI-RWKV** — state-tune on the corpus (CPU-feasible, built for exactly this),
   infilling = the composing gear, taste model reranks, critics gate, blind
   audition vs retrieval answers decides if it stays. The one genuinely new
   capability available today.
2. **Take-lane/groove-pool/quantize LOM surface** — reachable from our forge NOW;
   a companion .amxd unlocks the P2 take-lane comping dream (Ziforge repo =
   capability map, GPLv3 reference-only).
3. **symusic** — parsing speedup that turns corpus recomputes into casual ops.
4. **GigaMIDI NOMML** — free performed-feel tagger for all 25,600 phrases
   (gate-tested before it touches anything).
5. **amxd_textconv** — official validator wired into selftest/CI for forged devices.

Watching, not adopting: Magenta RT2 (audio jam bus), NotaGen's CLaMP-DPO recipe
(taste → DPO when a generator lands), .als automation injection.

---
Sources: github.com/ideoforms/AbletonOSC · github.com/ideoforms/pylive ·
github.com/leolabs/ableton-js · github.com/Ziforge/ableton-liveapi-tools ·
docs.cycling74.com/apiref/lom/{groovepool,takelane}/ · pypi.org/project/symusic ·
github.com/Natooz/MidiTok · github.com/magenta/{magenta,note-seq} ·
magenta.withgoogle.com/magenta-realtime · github.com/christianazinn/MIDI-RWKV
(arxiv 2506.13001) · github.com/Metacreation-Lab/MIDI-GPT (arxiv 2501.17011) ·
crfm.stanford.edu anticipatory-music-transformer · ijcai.org/proceedings/2025/1134
(NotaGen) · github.com/slSeanWU/MIDI-LLM · ableton.com/en/blog/live-12-3 ·
github.com/Ableton/maxdevtools (amxd_textconv) ·
github.com/Metacreation-Lab/GigaMIDI-Dataset (arxiv 2502.17726) ·
github.com/ahujasid/ableton-mcp · github.com/xiaolaa2/ableton-copilot-mcp ·
github.com/LofiFren/ableton-mcp-lofifren · jhurliman.org ableton-mcp experiments ·
midiagent.com · arxiv 2407.14700 (Composer's Assistant 2) · essentia.upf.edu ·
librosa · Demucs htdemucs · LAION-CLAP
