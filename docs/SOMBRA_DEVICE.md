# SOMBRA Device Status

## Current status

P3 transport/prototype is shipped. The user-facing device workflow is not yet
accepted in a real composing session.

The versioned implementation is:

- `mcp_server/sombra_listener.py`: local OSC listener and presence heartbeat.
  It receives pedal events on UDP 3353 and emits `/alive` heartbeats to UDP 3354.
- `tools/m4l/sidekick_bridge.js`: the LiveAPI executor used by
  `SIDEKICK_BRIDGE.amxd`. It receives `/sk <base64url(JSON)>` on UDP 3351 and
  replies on UDP 3352. It has no authentication and `[udpreceive]` listens on all
  interfaces, so do not load it on a shared or untrusted network. Clip-id ops
  (envelopes, groove) are refused unless the clip is on a SIDEKICK track.
- `mcp_server/server.py`: starts the listener only in normal MCP-server mode;
  self-tests use the pure parse/dispatch paths only.

The listener currently recognizes `/sombra/ping`, `/sombra/deal`,
`/sombra/answer`, `/sombra/keep`, `/sombra/drop`, `/sombra/lay`,
`/sombra/endings`, `/sombra/sticky`, `/sombra/ears/audition`,
`/sombra/ears/reset`, and `/sombra/status`.
Gears 1-4 dispatch deterministically; gears 5-6 deliberately return a chat/LLM
handoff. Device-side logic for the future `.amxd` lives in
`tools/m4l/sombra_ui.js` (ES5 glue — not runnable headless; a step toward closing
the artifact-reproducibility gap).

## OSC protocol (UDP 3353 in, replies to sender)

| Address | Args | Action | Reply (`/sombra/reply`) |
|---------|------|--------|-------------------------|
| `/sombra/ping` | string (ignored) | presence check | (none) |
| `/sombra/deal` | — | bury/marry ritual | tool result |
| `/sombra/answer` | int gear 1–6 | answer path (+ write) | tool result |
| `/sombra/keep` | — | judge keep | tool result |
| `/sombra/drop` | — | judge drop (+ clip delete) | tool result |
| `/sombra/lay` | — | lay the song | tool result |
| `/sombra/endings` | — | three endings | tool result |
| `/sombra/sticky` | `0` \| `1` \| `toggle` | set/toggle sticky develop | canonical announcement string |
| `/sombra/ears/audition` | `0` \| `1` \| `toggle` | session-scoped ears audition — answers listen to the room this session; never flips the stored default | `ears on for this session — answers listen to the room` / `ears off — back to defaults` |
| `/sombra/ears/reset` | — | clear ears session demotions; develop hold untouched | `ears reset — session demotions cleared` |
| `/sombra/status` | — | read-only device poll | JSON: `device_readout()` + `audition` flag |

Presence lamp: the MCP server emits `/alive sombra` on UDP 3354 every 2 s while
the session is up.

Port ledger: 3351/3352 bridge · 3353 events · 3354 heartbeat · 3355 replies.
Every `/sombra/reply` datagram is sent to both the ephemeral sender port and the
fixed reply port 3355, so a Max `[udpreceive 3355]` reliably catches replies.

### Readout contract (`/sombra/status` reply JSON)

Second-person, plain, artist-agnostic — state never identity (no track or project
names on the chassis):

```json
{
  "ears_line": "<moment summary from ears.status_line>",
  "hands_line": "<hold state: holding your idea, pass N (move) | ready to hold — your next answer sticks | not holding — answers start fresh>",
  "audition": false,
  "gear_names": {
    "1": "recall — your phrase, untouched",
    "2": "recontext — your phrase, refit to the room",
    "3": "develop — your phrase, reworked",
    "4": "grow — your phrase + a composed tail",
    "5": "compose — fresh, in your grammar",
    "6": "cover — one era of you as another"
  }
}
```

The device shows `ears_line` and `hands_line` as the compact readout; gear
one-liners populate dial microcopy / hover text. `audition` is the listener's
session-scoped ears switch (`/sombra/ears/audition`), not a `device_readout()`
field — the listener adds it to the status reply and it drives the
`audition_on` / `audition_off` lamp.

## Artifact boundary

Neither the SOMBRA Max device nor `SIDEKICK_BRIDGE.amxd` is tracked in this
repository. The repository therefore preserves the protocol and executor, but
does not yet contain a reproducible Max patcher source or forge/export recipe.
Do not treat the device as portable to another machine until that artifact is
versioned or a repeatable build procedure is added here.

## Remaining P3 acceptance work

1. Build the one-chassis, three-tab workflow with FINISH as the default tab.
2. Wire the presence lamp, KEEP/DROP, gear dial, and ritual controls to the
   documented OSC addresses.
3. Add a versioned patcher source or a reproducible export recipe for the Max
   artifacts.
4. Record a real-session acceptance run, including failed Live/bridge behavior.

This document is the status source of truth. The historical milestone view is
[`docs/history/ROADMAP_2026-09.md`](history/ROADMAP_2026-09.md).
