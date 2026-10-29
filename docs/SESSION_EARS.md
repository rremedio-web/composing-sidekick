# Session ears — organic listen

Session ears is an opt-in listening pass for audio present in the current Ableton Live Set.
It is separate from `hear_the_room`: named-but-unheard clips remain honest until this pass
returns validated take cards from Gemini.

## Golden Rule for Agents (Zero-Friction Live Flow)

**NEVER ask the composer to manually edit or create a JSON manifest during live sessions.**
When Ableton Live is running with Producer Pal:

1. When the composer records audio on ANY track (hardware ins, resampled takes, vocal/guitar takes) or freezes a track, Live automatically saves the `.wav` file into the project's `Samples/Recorded/` (or `Processed/`) directory.
2. Call `inventory_session_audio()` — the sidekick discovers audio clips through Producer Pal inside one validated `operation_session` and returns a short-lived capability envelope. **No absolute path or `sampleFile` crosses the MCP boundary.**
3. the owner chooses `focus` (`full`, `groove`, `melody`, or `composition`), then call `listen_to_takes(inventory_id, take_ids, focus=…)`.
4. Cards are scoped per ledger session ONLY when `store/session_ledger.json` has an open session whose verified `set_identity` matches the open Live Set; otherwise they use the Live Set identity. Unrelated open ledger sessions are never borrowed. Cards are stored under the verified Set identity and merged by clip identity (track, clip locator, and Live clip id when available); the ledger session id stays inside each card for measurement. New listens replace the prior card for that clip and leave other clips intact. Inventory envelopes carry the Producer Pal Live Set `session_id`; persisted cards also include server-authored `live_set_session_id`, `heard_at`, `clip_locator`, `live_clip_id`, `source_kind`, `analyzed_start_s`, `analyzed_end_s`, `analyzed_duration_s`, `transformations`, and `limitations`.

Without Live/Producer Pal the MCP inventory fails closed with `live_inventory_unavailable` — there is no manifest fallback on the public boundary.

## Operator Flow across Any Project & Any Track

1. **Inventory (Live only)**: `inventory_session_audio(track_indices=None, views=None)` — optional subset of regular track indices and `session` / `arrangement` views (default: all regular tracks, both views). Compact Producer Pal 2.2 paths (`path: "t0/s1"`) and standard slot locators are normalized consistently. Initial and post-discovery Live Set identity verification ensures Set stability.
2. **Listen**: `listen_to_takes(inventory_id, take_ids, focus=…)` after the owner chooses `focus`. Each selected take capability is single-use and expires after ten minutes. Requested takes are checked before consumption; only attempted takes are consumed. The result is `{"cards": [...], "errors": [{"take_id": "...", "reason": "..."}]}` so one bad take does not discard successful siblings. Live Set identity is rechecked immediately before listening to prevent cross-Set contamination if the composer switched sets.
   Every take listen is **proof-checked**: agy runs with `--output-format json` in a staging dir holding only a plain-PCM copy of the take, and the card counts only if agy's conversation record shows a successful `view_file` result on that exact file within the token cap (150k per take). Otherwise the take lands in `errors` with reason `no_listen`, `over_budget`, or `unverifiable` and no card is persisted. Accepted cards carry `listen_verified: true`.
3. **Persist & Use**: Cards fold into structure, arrangement, and reference steering context via `load_latest_take_cards` / `fold_take_cards`. Live identity is verified before folding cards into composing context; offline manifest cards qualify under explicit matching identity without requiring Live.
4. **Capture Loop**: `capture_loop` performs pre- and post-recording Set identity verification, fails closed with cleanup on Set shifts, and propagates verified `set_identity` to registration and listening passes.

## Capability Envelope & Region Truthfulness

`inventory_session_audio` returns:

```json
{
  "inventory_id": "inv-…",
  "session_id": "My-Live-Set",
  "expires_at": 1710000000.0,
  "takes": [
    {
      "take_id": "take-…",
      "track_index": 1,
      "track_name": "Vox",
      "view": "session",
      "clip_locator": "1/1",
      "clip_name": "Vox take",
      "source_kind": "source_region",
      "analyzed_start_s": 2.5,
      "analyzed_end_s": 14.5,
      "analyzed_duration_s": 12.0,
      "transformations": { "pitch_coarse": 2 },
      "limitations": [
        "Raw source audio reading does not include track effects or device chain.",
        "Clip pitch/gain adjustments are not rendered in raw audio source."
      ],
      "duration_s": 12.5
    }
  ]
}
```

Canonical audio paths live only in a process-local registry until `listen_to_takes` consumes the requested take capability. Inventories cap at 128 takes; each listen call caps at 64 take IDs.

### True Region Cropping and Warp Handling

- **Source Region vs Rendered Capture**: Raw clip takes are labeled `source_kind: "source_region"`. Validated captures from `capture_loop` are labeled `source_kind: "rendered_capture"`.
- **Unwarped Clips (LOM Seconds)**: Per Cycling '74 LOM Clip spec, unwarped clips define `start_marker`, `end_marker`, `loop_start`, and `loop_end` in **seconds**. Gain is normalized to `[0.0, 1.0]`. Takes with unbounded end markers (`end_marker` is null/omitted) are accommodated over the full audio file without triggering false `capture_required`. Staging crops the exact audible excerpt (`[analyzed_start_s, analyzed_end_s]`) up to the 45-second cap using `ffmpeg`. Late excerpts are never truncated back to second zero.
- **Warped Clips (LOM Beats & Markers)**: Warped clips define markers in **beats**. When valid monotonic warp markers span the selected beat range, timing is interpolated to sample seconds. If warp markers are missing, non-monotonic, or out of range, the descriptor marks `capture_required: True` with actionable guidance to use `capture_loop(bars=..., surface='auto')` preview. Calling `listen_to_takes` on an unsupported take immediately raises `SessionEarsError` without invoking `agy` or any model.
- **Track Effects & Performed Sound**: Raw source reading inspects the recording before track effects or device chains. For the true performed/processed sound, the composer opts into `capture_loop`.

## Card Contract

Each card has `take_id`, `track`, `clip_name`, `role_guess`, `vibe`, `register`, `density`, `pitch_claims`, `timestamps`, and `contract_version`.
- `role_guess`: `lead` | `bass` | `pad` | `drum` | `texture` | `unknown`
- `vibe`: descriptive prose of timbre, space, and texture (<= 12 words)
- `register`: `null` or `{ "band": "low|mid|high", "pitch_span": "..." | null }`
- `density`: `sparse` | `medium` | `dense`
- `pitch_claims`: actual heard pitches and intervals (`[{ "claim": "...", "confidence": "low|med|high" }]`), never invented notes
- `timestamps`: `MM:SS` strings
- `contract_version`: `session-take-card-v1`
- Server metadata: `source_kind`, `analyzed_start_s`, `analyzed_end_s`, `analyzed_duration_s`, `transformations`, `limitations`, `source`, `session_id`, `live_set_session_id`.

## Cost and Safety Levers

- Default model: `gemini-3.8-flash-medium`. The `model` argument wins when provided; `SESSION_EARS_MODEL` is consulted only when the internal Python caller passes `model=None` (the MCP wire default is explicit, not env-driven).
- Each take gets one `agy` call. Audio is staged into a temporary sandbox grant and clipped to the true analyzed region (up to 45 seconds), matching the posture used by `tools/audio_ref.py`. `focus` uses its own take-card vocabulary (`full`, `groove`, `melody`, `composition` — `TAKE_FOCUS_OVERLAYS` in `mcp_server/session_ears.py`), not the audio-ref card `FOCUS_OVERLAYS`.
- The subprocess uses the established minimal environment allowlist, `--sandbox`, timeouts, and stdout recovery. It also passes `--dangerously-skip-permissions`, unlike audio-reference card listens: the ears listener is asked to write its card to `take_card.json` in the staging directory, and a non-interactive print turn cannot approve that write (`EARS_AGY_SKIP_PERMISSIONS` in `mcp_server/session_ears.py`). Invalid or missing output raises an explicit error; no card is fabricated.
- Staging directories are always cleaned up; source paths never appear in MCP responses, errors, persisted cards, or normal logs.

## Offline / Test Helper

`_inventory_from_manifest(manifest_path, trusted_root)` is a private offline helper for selftests and CLI — it requires both a manifest path and an explicit trusted root and rejects any take whose audio path resolves outside that root. It is not exposed on the MCP boundary.
