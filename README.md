# composing-sidekick

An LLM composing **bandmate** for Ableton Live. It learns a composer's musical identity
from their own Live catalogue (the MIDI inside `.als` files — symbolic, never audio),
turns it into retrievable phrases, a style profile and critics, and works with the
composer inside Live through the [Producer Pal](https://producer-pal.org) MCP device.

The LLM client (Claude Code, Codex, Cursor, Gemini CLI…) is the composer. This
repository is its instrument: an MCP server of composing tools, an analysis pipeline,
and the operator docs that keep the loop honest.

Built by Ricardo Remédio with LLM assistants, for his own practice. The
public repository ships **without** his catalogue, notes, judgments or reference
library — see [What is personal](#what-is-personal-and-not-in-the-repo).

## What it does

- **Learns from your catalogue.** `ingest/` parses every Live Set (arrangement, session
  and take-lane clips, drum-aware), `analyze/` builds a phrase index, per-role melodic
  grammar, harmony and structure statistics, motifs, velocity/timing feel and direction
  axes under `store/`.
- **Composes from your material first.** `answer_my_loop`, `find_phrases`,
  `develop_my_loop`, `grow_my_loop`: call-and-response, variation and growth from the
  composer's own phrases. A seeded generator exists but is demoted to a critic
  (`score_phrase`, `critique`, `validate_composed`).
- **Hears the room.** `hear_the_room` reads the open Set through Producer Pal (tempo,
  key, playing clips, roles); audio clips are reported as *named but unheard*. An opt-in
  **session ears** pass (`inventory_session_audio` → `listen_to_takes`) can send takes to
  an external listener model and fold validated cards back into context.
- **Writes safely.** Automatic writes land only on tracks named `SIDEKICK JAM` and
  `SIDEKICK BOOTH`; every write is read back and verified; everything else is a preview
  or confirm-gated handoff.
- **Closes the judgment loop.** Every JAM write is stamped in a session ledger; the
  composer judges KEEP/DROP; `close_session` inventories the Set and scores *session
  survival* (suggestions still in the Set at the end of the night), the project's real
  metric.
- **Finishes songs.** `whats_missing` (dramaturg detectors on a Set), `lay_the_song`
  (locator maps in the composer's measured proportions), `three_endings`,
  `bury_or_marry`, `version_courage`, `one_hour_close`.
- **Uses references.** `analyze_audio_ref` files a local file or URL as a listening card
  plus a Basic Pitch MIDI sketch; `find_audio_refs` / `apply_audio_ref` / `beat_from_ref`
  let a reference steer *which of the composer's own* phrases and drum clips are used.
- **Offline Mix Adviser** (separate, gated): DSP evidence on exported stems, at most three
  bounded gain proposals, loudness-matched blind A/B, its own ledger.

## Requirements

| Need | Notes |
|---|---|
| macOS | Developed and tested on macOS only (POSIX file locking, Darwin fsync path, Max for Live). |
| Python 3.12 | In a virtualenv at `.venv/` — the MCP config and every doc assume that path. |
| Ableton Live 12 | Captures were made on Live 12.4.6. The optional envelope bridge needs Live 12.2+ (`v8` object). |
| Producer Pal | The `.amxd` device in your Set **and** the npm client. `.mcp.json` pins `producer-pal@2.3.0`; the code speaks `2.2.0-rc1`, `2.3.0` and `2.4.0` from captured fixtures (`docs/PRODUCER_PAL_VERSIONS.md`). Keep the client pin equal to the device build in the Set. |
| Node.js / `npx` | Runs the Producer Pal client. |
| `ffmpeg` (optional) | Session-ears staging, audio-reference cuts; related tests skip without it. |
| `agy` CLI (optional) | External listener used by session ears, audio references and the mix witness (default model `gemini-3.8-flash-medium`, override with `SESSION_EARS_MODEL` / `AUDIO_REF_MODEL` / `MIX_WITNESS_MODEL`). Nothing listens without it; nothing breaks without it. |

## Install

```bash
git clone <this repo> composing-sidekick && cd composing-sidekick
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
# optional: basic-pitch pulls a TensorFlow stack on macOS that the code never imports (it runs on ONNX).
# Removing it saves space; afterwards `pip check` warns about basic-pitch, which is expected.
.venv/bin/pip uninstall -y tensorflow tensorflow-macos keras tensorboard
.venv/bin/python -m unittest discover -s tests -t .   # works on a clean clone (skips what needs your catalogue)
```

The server selftest (`mcp_server/server.py --selftest`) reads the generated style
profile, so run it after **Build your catalogue** below.

## Build your catalogue

The store is empty on a clean clone. Point the scanner at the folders that hold your
Live Sets, then run the pipeline **in this order** (each stage reads the previous one's
artifacts):

```bash
export SIDEKICK_CATALOG_ROOTS="$HOME/Music/Ableton:/path/to/other/projects"
python3 ingest/scan_catalog.py                # → store/catalog_manifest.json
python3 ingest/materialize.py                 # → store/projects/*.json (one parse of the drive)
.venv/bin/python analyze/harmony.py           # music21 → store/harmony.json
python3 analyze/structure.py                  # → store/structure.json
.venv/bin/python analyze/style_profile.py     # AFTER harmony + structure → style_profile.json + STYLE.md
python3 analyze/build_index.py                # → store/phrase_index.json
python3 analyze/motifs.py                     # AFTER build_index + style_profile
.venv/bin/python tools/build_directions.py    # direction axes (reads the profile)
# optional digs
.venv/bin/python tools/harvest_arrangement_dna.py && .venv/bin/python tools/build_phrase_curves.py
.venv/bin/python tools/build_groove_templates.py
```

`tools/rebuild_corpus.py --commit` runs scan → materialize → analyze with fail-closed
gates (every previously scanned root still mounted, project counts within 5 %) and
restores the previous artifacts if a stage fails. Pack and demo Sets you do not want
learned from go in `store/not_his_projects.json` (`{"blocked_projects": ["<project name>", …]}`,
matched case-insensitively); every analyzer honours it.

`store/NOTES.md` is **yours**: hand-written notes that survive every rebuild and are
read by the assistants and by `style_profile.py`.

## MCP setup

Add to `.mcp.json` at the repo root (Claude Code) or to your client's MCP config; use the
absolute path of your clone, MCP hosts do not expand `~`:

```json
{
  "mcpServers": {
    "style-sidekick": {
      "command": "<repo>/.venv/bin/python",
      "args": ["<repo>/mcp_server/server.py"],
      "env": { "SIDEKICK_MCP_PROFILE": "compose" }
    },
    "producer-pal": {
      "command": "npx",
      "args": ["-y", "producer-pal@2.3.0", "-f", "json", "-n", "midi-json"]
    }
  }
}
```

Install the Producer Pal device on a MIDI track in your Set
([producer-pal.org/installation](https://producer-pal.org/installation)), restart the
client, and start with a read-only `ppal-connect`: the sidekick checks the reported
`producerPalVersion` against its captured builds before any write. The Live endpoint is
`http://localhost:3350/mcp` (`PRODUCER_PAL_URL` overrides it).

### Profiles (`SIDEKICK_MCP_PROFILE`)

| Profile | Tools | Focus |
|---|---|---|
| `compose` (default) | 63 | Hearing, answering, developing, auditioning, judging, closing the session |
| `session` | 37 | Live Set interaction: hearing, take inventory/listen, clip reads, judgment, closeout |
| `reference` | 18 | Audio references: analyze, find, tag, clip, batch, reindex, apply |
| `debug` | 19 | Diagnostics, envelope probes, the offline Mix Adviser |
| `all` | 77 | Everything |

Tool reference, ears workflow, judgment ledger and audio-reference workflow:
[`config/claude_mcp.md`](config/claude_mcp.md). How a session runs:
[`docs/CURSOR_CHAT_PLAYBOOK.md`](docs/CURSOR_CHAT_PLAYBOOK.md).

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t .    # stdlib unittest; pytest is not installed
.venv/bin/python mcp_server/server.py --selftest        # offline; needs the built catalogue; must end green
```

The suite never contacts Live: `tests/_guard.py` fails any test that reaches the
Producer Pal port, and the selftest snapshots the store before running and refuses to
leave it changed. Tests that need catalogue-generated artifacts, `ffmpeg`, or a Producer
Pal capture that was never made **skip with a reason**; on a clean clone expect a few
hundred skips, zero failures.

Producer Pal behaviour is tested against reply **envelopes captured** from the real
device under `tests/fixtures/pp/` (read, mutate, legacy and version-specific runs per
build): the shapes, keys, orderings, warning and error texts are the device's. The
music inside them is synthetic: names are invented and the piece replay's notes are newly
drawn over the captured arrangement skeleton (`tests/fixtures/pp/README.md`). The fakes in `tests/pp_fakes.py` validate every call against that build's tool
schemas. Adding a build means capturing it on a disposable scratch Set first — see
`docs/PRODUCER_PAL_VERSIONS.md`.

## What is personal and not in the repo

Everything the pipeline derives from a catalogue is regenerated locally and gitignored:
`corpus/`, `store/projects/`, `store/phrase_index.json`, `style_profile.json`,
`harmony.json`, `structure.json`, `motifs.json`, `taste_model.json`, `automation.json`,
`phrase_curves.json`, `directions.json`, `store/audio_refs/`, `store/session_ears/`,
`store/exports/`, `tools/ideas/` experiment data, `.scratch/`.

Files that hold a composer's **words and judgments** (`store/NOTES.md`, `STYLE.md`,
`ratings.json`, `session_ledger.json`, `rituals.json`, `pieces.json`,
`piece_records.json`, `piece_decisions.json`, booth and audition votes) are created on
your machine the first time you use the matching tool. The public repository contains
none of the author's. The musical-knowledge skills under `.claude/skills/` ship as
templates for the same reason: the idiom, theory and listening notes they held were his.

None of the author's words, melodies, rhythms or measurements ship. Where the code
needs a value or a fixture that used to come from his catalogue, it now has a neutral
or synthetic stand-in (and, for calibration, an optional local override). The one
structural remainder is named below:

- **Producer Pal fixtures** (`tests/fixtures/pp/**`): captured reply envelopes with
  invented names and newly drawn notes; no note matches one in its corresponding
  original clip. The piece replay keeps the captured arrangement skeleton (clip
  positions, lengths, loop windows) and its per-bar harmonic outline; see
  `tests/fixtures/pp/README.md`.
- **Engine calibration** (`mcp_server/idiom_cells.py`): neutral defaults (common
  four-note cells, a moderate human-ish feel), tagged `evidence: "default"`. Put values
  measured on your own catalogue in the gitignored `store/calibration.json` (keys
  `cells`, `feel`) to override them. Role thresholds (`analyze/roles.py`) are plain
  conventional register and texture boundaries, not measured on anyone.
- **Rhythm library**: none ships. If you keep one, it lives in the gitignored
  `store/approved_rhythms.json` with its MIDI under `assets/approved_rhythms/`; the
  loader and `get_style()` tolerate its absence.
- **Audio-reference fixtures** (`tests/fixtures/audio_ref/`): generated by
  `make_synthetic.py` in that folder — seeded plateau energy curves and invented cards
  designed to trigger the detectors the tests assert, measured on no recording.

## Status (2026-10-14)

A working prototype, used in the author's real composing sessions since mid-2026.
The current product arc is **durable KEEP**: fewer features, more suggestions that
survive the session. Parked by design: a full SOMBRA Max for Live control surface
(transport and OSC protocol exist, `docs/SOMBRA_DEVICE.md`), a `core`/`server` split,
stem separation, new note generators, automatic mixing. Ears-by-default is gated on a
pre-registered blind round (`docs/EARS_PROMOTION.md`).

## Licence

MIT — see [`LICENSE`](LICENSE). Copyright (c) 2026 Ricardo Remédio.

## Documentation map

| Doc | Purpose |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Module map, data flow, store artifacts, transport contract, house rules |
| [`docs/CURSOR_CHAT_PLAYBOOK.md`](docs/CURSOR_CHAT_PLAYBOOK.md) | The session loop for any assistant: ears, JAM/BOOTH, gears, KEEP/DROP, references |
| [`docs/KNOWLEDGE_GUIDE.md`](docs/KNOWLEDGE_GUIDE.md) | What has authority, where knowledge lives, how to store a result once |
| [`config/claude_mcp.md`](config/claude_mcp.md) | MCP registration, every tool, ledger contract, audio-reference workflow, bridge install |
| [`docs/PRODUCER_PAL_VERSIONS.md`](docs/PRODUCER_PAL_VERSIONS.md) | Supported Producer Pal builds, what each capture proved, how to add a build |
| [`docs/SESSION_EARS.md`](docs/SESSION_EARS.md) | Listening to Set audio takes (opt-in) |
| [`docs/MIX_ADVISOR.md`](docs/MIX_ADVISOR.md) | Offline mix trials, CLI and invariants |
| [`docs/SOMBRA_DEVICE.md`](docs/SOMBRA_DEVICE.md) | OSC protocol and status of the Max device |
| [`docs/EARS_PROMOTION.md`](docs/EARS_PROMOTION.md) | Checklist before flipping ears on by default |
| [`docs/pieces/EXAMPLE.md`](docs/pieces/EXAMPLE.md) | Template for a piece's notes file |
| [`docs/research/README.md`](docs/research/README.md) | How to file written research for the assistants |
| [`docs/history/README.md`](docs/history/README.md) | Roadmap eras, reviews, plans and agent runbooks from the build |
| [`AGENTS.md`](AGENTS.md) · [`CLAUDE.md`](CLAUDE.md) · [`GEMINI.md`](GEMINI.md) | Entry points for assistants working in this repo |
