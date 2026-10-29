"""Composing Sidekick MCP server (registered as "style-sidekick") — Claude's composition toolkit.

Claude (the MCP client) is the composer. These tools supply Ricardo's style
context, real phrases, structure templates, scoring, and MIDI export. They do
NOT generate music with their own model.

Run:
    .venv/bin/python mcp_server/server.py              # stdio MCP server
    .venv/bin/python mcp_server/server.py --selftest   # offline selftest; needs the built catalogue
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_server import core as _core
from mcp_server import core_shared
from mcp_server import profiles as _profiles
from mcp_server.session_ears import DEFAULT_AGY_MODEL

_active_profile = _profiles.get_active_profile_name()

mcp = FastMCP(
    "style-sidekick",
    instructions=_profiles.get_profile_instructions(_active_profile),
)


# Parameter descriptions and tool hints reach the client in each tool's schema
# (Annotated[..., Field(description=...)] is what FastMCP reads). They are
# written from the code each parameter feeds; keep them in step with it.
_NOTES = ("MIDI notes, each {pitch: 0-127, start_beats: position from the clip start "
          "in beats, duration_beats (default 0.25), velocity (default 80)}. "
          "start/duration are accepted as aliases.")
_SEED_FROM_LIVE = (_NOTES + " Omit to use the MIDI clip currently selected in Live "
                   "(read through Producer Pal).")
_KEY = "Key such as 'D minor'. Omit to detect it from the notes."
_ROLE = "Musical role whose grammar and register apply, e.g. 'lead' or 'bass'."
_SEED = "Random seed; the same inputs and seed give the same result."
_USE_EARS = ("Fit the pick to the room read by hear_the_room (tempo world, key, "
             "register, density, role gates). Omit to use the promoted default in "
             "store/ears_default.json (off when the file is absent).")
_DIRECTION = ("Ears gate modifier (needs use_ears): 'darker', 'clubbier', 'sparser' "
              "or 'weirder'.")
_MOMENT = ("A room dict returned by hear_the_room, to reuse it instead of reading "
           "Live again (ears only).")
_RELAX = ("Accept a relaxation the ears offered after a no_fit: 'auto', or one gate: "
          "tempo_world, key, register, density, role. Needs use_ears.")

# Tool hints (MCP ToolAnnotations). Only set where the code shows the behaviour.
_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
_MAY_REPLACE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def _run_async(coro):
    from mcp_server import pp_session as _pp_session
    return _pp_session.run_coro_blocking(coro)


@mcp.tool(annotations=_READ_ONLY)
def get_style() -> dict:
    """Return Ricardo's style profile: STYLE.md text + structured facts."""
    return _core.get_style()


@mcp.tool(annotations=_READ_ONLY)
def find_phrases(
    seed_notes: Annotated[list[dict] | None, Field(description=_NOTES + ' Optional query: phrases nearest to these notes come first; omit for a varied spread.')] = None,
    kind: Annotated[str | None, Field(description="'melodic'/'harmonic' (pitched) or 'drum'. Omit to infer it from role (drum for role='drum', else pitched).")] = None,
    k: Annotated[int, Field(description='How many phrases to return.')] = 6,
    key: Annotated[str | None, Field(description="Keep only phrases from projects in this key, e.g. 'D minor'.")] = None,
    tempo: Annotated[float | None, Field(description='Keep only phrases within 20 BPM of this tempo.')] = None,
    prefer_liked: Annotated[bool, Field(description='Rerank the results by your past ratings.')] = False,
    role: Annotated[str | None, Field(description="Restrict to one role's phrases, e.g. 'lead', 'bass', 'drum'.")] = None,
    use_ears: Annotated[bool | None, Field(description=_USE_EARS)] = None,
    direction: Annotated[str | None, Field(description=_DIRECTION)] = None,
    moment: Annotated[dict | None, Field(description=_MOMENT)] = None,
    relax: Annotated[str | None, Field(description=_RELAX)] = None,
) -> list[dict] | dict:
    """Find diverse REAL phrases from Ricardo's catalog (his actual playing, not generated).

    Pass role='lead' to get only lead-role phrases (kind inferred) — the natural call is
    find_phrases(role='lead', k=8). kind: 'melodic'/'harmonic' (pitched) or 'drum'. Results carry
    role/project/clip_source provenance. The phrase index now includes take-lane phrases.
    With use_ears the pick is fit to the room — see hear_the_room; pass the moment
    it returned to reuse that room instead of reading Live again. When nothing
    fits, the gate_report offers a relaxation — say yes with relax='auto' or a
    gate name (tempo_world/key/register/density/role)."""
    if use_ears or (use_ears is None and _core._ears_default()):
        return _core.find_phrases_with_ears(
            seed_notes=seed_notes,
            kind=kind,
            k=k,
            key=key,
            tempo=tempo,
            prefer_liked=prefer_liked,
            role=role,
            direction=direction,
            moment=moment,
            relax=relax,
        )
    if relax:
        raise ValueError("relax needs use_ears — there is no gate to relax context-blind")
    return _core.find_phrases(
        seed_notes=seed_notes,
        kind=kind,
        k=k,
        key=key,
        tempo=tempo,
        prefer_liked=prefer_liked,
        role=role,
    )


def _store_disk_state() -> dict:
    """Free-space reading of the store directory; never raises."""
    import shutil
    try:
        usage = shutil.disk_usage(os.path.join(ROOT, "store"))
    except OSError as exc:
        return {"free_bytes": None, "note": "disk usage unreadable: %s" % exc}
    free = usage.free
    out: dict = {"free_bytes": free}
    if free < 200 * 1024 * 1024:
        out["warning"] = ("only %d MB free on the store disk — recordings, "
                          "audio refs and the ledger are at risk; free space "
                          "now" % (free // (1024 * 1024)))
    elif free < 1024 * 1024 * 1024:
        out["note"] = "%d GB free on the store disk" % (free // (1024 * 1024 * 1024))
    return out


def _auto_close_ended_sessions(moment: dict) -> tuple[list[dict], list[dict], str | None]:
    """Sweep ended-but-unswept nights of the open Set from hear_the_room.

    Reads the inventory once and closes only ENDED sessions whose Set or
    alias chain contains the open Set; the currently open session is never
    auto-closed. An inventory without Set identity can prove no chain and
    sweeps nothing. Nights that actually closed come back as the first
    list; partial sweeps (scored what they could, night still open) as the
    second, so the operator can tell a finished night from a waiting one.
    Any failure becomes a warning — hearing the room must not fail because
    a sweep could not run.
    """
    import ledger as _ledger
    from mcp_server import live_write as _live_write

    if moment.get("ears") == "blind":
        return [], [], None
    data = _ledger.load()
    needing = {group["session"].get("id") for group in
               _ledger.sessions_needing_sweep(data)}
    open_ids = {s.get("id") for s in data.get("sessions", [])
                if not s.get("closed_at") and not s.get("ended_at")}
    # The current Set as the ledger last knew it: an ended night whose chain
    # does not CONTAIN it can never match the inventory, so it must not
    # cost a Live read either.
    current_set = _ledger.last_known_set_id(data)
    ended = [s for s in data.get("sessions", [])
             if s.get("id") in needing and s.get("ended_at")
             and not s.get("closed_at") and s.get("id") not in open_ids
             and current_set is not None
             and current_set in _ledger._session_chain(s)]
    if not ended:
        return [], [], None
    try:
        inventory = _run_async(_live_write.read_set_inventory())
    except Exception as exc:  # noqa: BLE001 — a sweep failure is a warning
        return [], [], "auto-close skipped: %s" % _close_failure_reason(exc)
    inventory_set = (inventory.get("set_identity") or {}).get("set_id") \
        if isinstance(inventory, dict) else None
    if not inventory_set:
        # no Set identity means no chain can be verified — sweep nothing
        return [], [], None
    swept: list[tuple[str | None, dict]] = []
    warning: str | None = None
    for session in ended:
        if inventory_set not in _ledger._session_chain(session):
            continue  # another Set's night: its sweep is not ours to run
        try:
            report = _ledger.close_session(
                inventory, session_id=session.get("id"))
        except Exception as exc:  # noqa: BLE001
            warning = "auto-close of %s failed: %s: %s" % (
                session.get("id"), type(exc).__name__, exc)
            continue
        if report.get("ok"):
            swept.append((session.get("id"),
                          {"session_id": session.get("id"),
                           "judged": report.get("judged"),
                           "keep_rate": report.get("keep_rate"),
                           "rate_status": report.get("rate_status")}))
        elif warning is None:
            warning = "auto-close of %s: %s" % (
                session.get("id"), report.get("error"))
    closed: list[dict] = []
    partial: list[dict] = []
    if swept:
        closed_at = {s.get("id"): s.get("closed_at")
                     for s in _ledger.load().get("sessions", [])}
        for sid, row in swept:
            (closed if closed_at.get(sid) else partial).append(row)
    return closed, partial, warning


_READINESS_CACHE_SECONDS = 60.0
_READINESS_CACHE: dict = {"at": None, "result": None, "set_id": None}


def _scoring_readiness_cached(current_set: str | None) -> dict:
    """The readiness probe is a Live round trip; one per minute per Set is
    plenty. The cache is keyed by the Set the ledger last saw, and a probe
    that FAILED is never cached — the next call tries again."""
    import time as _time
    now = _time.monotonic()
    cached = _READINESS_CACHE["result"]
    if cached is not None and _READINESS_CACHE["set_id"] == current_set \
            and now - _READINESS_CACHE["at"] < _READINESS_CACHE_SECONDS:
        return cached
    from mcp_server import live_write as _live_write
    try:
        result = _run_async(_live_write.scoring_readiness())
    except Exception as exc:  # noqa: BLE001 — a probe must not deafen the room
        return {"ready": False,
                "reason": "readiness probe failed: %s: %s" % (type(exc).__name__, exc)}
    _READINESS_CACHE["at"] = now
    _READINESS_CACHE["result"] = result
    _READINESS_CACHE["set_id"] = current_set
    return result


@mcp.tool(annotations=_WRITES)
def hear_the_room(
    where: Annotated[str, Field(description="'session' (default): the Session clips playing now. 'arrangement': the Arrangement clips at `bar` on every regular track.")] = "session",
    bar: Annotated[int | None, Field(description="1-based Arrangement bar to hear (where='arrangement' only). Omit to use the transport playhead from the Live API; without the Live API, pass it or the room reads as blind.")] = None,
) -> dict:
    """Read the room through Producer Pal: tempo world, key (Live's own scale
    setting when it carries one, with the heard estimate beside it), the MIDI of
    the Session clips actually PLAYING on the first MOMENT_MAX_TRACKS (12)
    regular tracks (muted tracks omitted; a stopped transport reads an empty
    room — row 0 is never substituted), and which playing clips are named but
    unheard (audio / ambiguous). where='arrangement' hears the Arrangement clips
    at ``bar`` on every regular track (up to ARRANGEMENT_READ_MAX_TRACKS, 32)
    instead (default: the transport playhead from the Live API; without the
    Live API pass bar=N or the room is declared blind). moment.unread lists MIDI
    clips whose notes were not read (Producer Pal returns notes only in
    [-L, 2L) of a clip; the Live API reads them all, or its read of that clip
    failed) — never an empty room; moment.hidden lists heard clips with notes
    behind the loop brace that do not play. It never writes to Live; if Live
    is unreachable this declares blind mode. Unheard names are conversational evidence only —
    ask what they are doing; never infer musical content from the name. The
    result also carries the judgment loop state (open session,
    pending judgments, unswept nights, next action), a scoring-readiness probe
    for new writes, and the store disk's free space. Not read-only: ended-but-
    unswept nights of the open Set are swept automatically (their outcomes
    are written to the local ledger) and listed under auto_closed
    (nights that actually closed) or auto_swept_partial (scored what they
    could, still waiting) — never the currently open session."""
    import ears as _ears
    import ledger as _ledger
    import moment as _moment_mod

    m = _moment_mod.get_moment(where=where, bar=bar)
    out: dict = {"moment": m, "status": _ears.status_line(m)}
    import rituals as _rituals
    out["finishing_counts"] = _rituals.finishing_counts()
    auto_closed, auto_partial, auto_warning = _auto_close_ended_sessions(m)
    if auto_closed:
        out["auto_closed"] = auto_closed
    if auto_partial:
        out["auto_swept_partial"] = auto_partial
    status = _ledger.ledger_status()
    if auto_warning:
        status["warning"] = auto_warning
    out["ledger_status"] = status
    # Corpus freshness rides beside the loop state (not inside it:
    # ledger_status stays Live-free and dependency-light).
    try:
        from corpus_keys import corpus_staleness as _room_staleness
        out["corpus"] = {
            "stale_notes": _room_staleness(
                os.path.join(ROOT, "store"),
                _ledger.last_known_set_path(_ledger.load())),
        }
    except Exception:  # noqa: BLE001 — advisory, never fatal
        pass
    if m.get("ears") == "blind":
        # no Live contact when the room itself says Live is down
        out["scoring_ready"] = {
            "ready": False,
            "reason": "Live unreachable — the room is blind; probe skipped",
        }
    else:
        out["scoring_ready"] = _scoring_readiness_cached(
            _ledger.last_known_set_id(_ledger.load()))
    out["disk"] = _store_disk_state()
    return out


@mcp.tool()
async def inventory_session_audio(
    track_indices: list[int] | None = None,
    views: list[Literal["session", "arrangement"]] | None = None,
) -> dict:
    """Inventory current Set audio takes via Producer Pal and return a capability envelope.

    Returns opaque take descriptors plus a short-lived inventory_id. Canonical audio
    paths never cross this boundary. Without Live/Producer Pal the call fails closed
    with live_inventory_unavailable — there is no manifest fallback on MCP.
    """
    from mcp_server import session_ears as _session_ears

    return await asyncio.to_thread(
        _session_ears.inventory_session_audio, track_indices=track_indices, views=views,
    )


@mcp.tool()
async def capture_loop(
    bars: int = 8,
    source_scene_index: int | None = None,
    surface: Literal["auto", "live_api", "bridge"] = "auto",
    listen: bool = False,
    confirm: bool = False,
) -> dict:
    """Preview or record what is playing into an empty SIDEKICK EARS slot.

    ``confirm=False`` is read-only. ``surface='auto'`` prefers a validated
    Producer Pal Live API capability and otherwise uses SIDEKICK BRIDGE.
    """
    from mcp_server.capture_loop import capture_loop as _capture_loop

    return await asyncio.to_thread(
        _capture_loop, bars=bars, source_scene_index=source_scene_index,
        surface=surface, listen=listen, confirm=confirm,
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
async def listen_to_takes(
    inventory_id: Annotated[str, Field(description='The inventory_id returned by inventory_session_audio (short-lived).')],
    take_ids: Annotated[list[str], Field(description='take_id values from that inventory to listen to.')],
    focus: Annotated[str, Field(description="What the listener should attend to: 'full', 'groove', 'melody' or 'composition'.")] = "full",
    model: Annotated[str, Field(description="agy listener model name. Each take's audio is sent to this outside model.")] = DEFAULT_AGY_MODEL,
) -> dict:
    """Consume an inventory capability and persist validated session take cards.

    Returns {"cards": [...], "errors": [{"take_id", "reason"}]}: one bad take
    no longer fails its siblings.
    """
    from mcp_server import session_ears as _session_ears

    return await asyncio.to_thread(
        _session_ears.listen_to_takes, inventory_id, take_ids, focus=focus, model=model,
    )


@mcp.tool()
def reset_ears() -> dict:
    """The 'reset ears' gesture: clear this session's keep/reject demotions."""
    import ears as _ears
    return {"status": _ears.reset_session()}


@mcp.tool()
def suggest_structure(
    target_minutes: float | None = None,
    tempo: float | None = None,
) -> dict:
    """Propose a section skeleton (bar lengths, density, breakdown placement)."""
    return _core.suggest_structure(target_minutes=target_minutes, tempo=tempo)


@mcp.tool()
def develop_idea(seed_notes: list[dict], tempo: float = 120.0) -> dict:
    """Develop a seed phrase into labelled symbolic variations (theme & variations).

    Deterministic transforms of the seed — not synthesis toward the catalog average.
    Returns theme, octave_up/down, half/double_time, thin/thicken, fragment,
    reharmonize, call_response.
    """
    return _core.develop_idea(seed_notes=seed_notes, tempo=tempo)


@mcp.tool()
def arrange_song(
    parts: list[dict],
    target_minutes: float | None = None,
    tempo: float = 120.0,
) -> dict:
    """Lay out loop parts on a full-song timeline (sections + arrangement placements)."""
    return _core.arrange_song(parts=parts, target_minutes=target_minutes, tempo=tempo)


@mcp.tool()
def audition_leads(bars: int = 8, key: str | None = None, density: str = "mid",
                   n: int = 4, seed: int = 0,
                   use_ears: bool | None = None,
                   direction: str | None = None) -> dict:
    """Generate N interval-band sampled lead candidates to audition and rate.

    Returns each candidate's notes, base style score, and current taste score. Write
    them to clips, listen, then call rate_candidates(best, worst, reason).
    With use_ears the pick is fit to the room — see hear_the_room."""
    return _core.audition_leads(bars=bars, key=key, density=density, n=n, seed=seed,
                                use_ears=use_ears, direction=direction)


@mcp.tool()
def rate_candidates(candidates: list[Any], best: int,
                    worst: int | None = None, reason: str | None = None,
                    key: str | None = None, role: str = "lead",
                    contrast_axis: str | None = None,
                    rating_scope: str | None = None,
                    trainable_model: str | None = None,
                    idempotency_key: str | None = None) -> dict:
    """Record a best/worst judgment over auditioned candidates and retrain the taste
    model only when the round is scoped to a supported trainable axis.

    reason is a free one-word tag, e.g. 'static', 'flowing', 'jumpy', 'good-landing'.
    Pass key + role straight from the audition_leads result so training and selection
    score the candidates under the same known key. For IdeaPacks, pass
    contrast_axis=pack["contrast_axis"]; rating_scope and trainable_model are derived.
    Pass a stable idempotency_key (e.g. 'session:<date>:round:<n>') so a retried
    call can never double-count the same judgment as fresh taste evidence.
    Identical rounds replay idempotently — a deliberate repeat judgment needs a
    distinct idempotency_key."""
    return _core.rate_candidates(
        candidates=candidates,
        best=best,
        worst=worst,
        reason=reason,
        key=key,
        role=role,
        contrast_axis=contrast_axis,
        rating_scope=rating_scope,
        trainable_model=trainable_model,
        idempotency_key=idempotency_key,
    )


@mcp.tool()
def suggest_growth(n: int = 3, key: str | None = None, seed: int = 0,
                   use_ears: bool | None = None,
                   direction: str | None = None) -> dict:
    """Suggest rare-but-native moves just outside Ricardo's habits — melodic cells, scale
    degrees, and progressions he uses rarely but really does use — each as auditionable
    MIDI with a reason. The returned `candidates` feed rate_candidates() to train the
    taste model with genuinely varied options.
    With use_ears the pick is fit to the room — see hear_the_room."""
    return _core.suggest_growth(n=n, key=key, seed=seed,
                                use_ears=use_ears, direction=direction)


@mcp.tool(annotations=_READ_ONLY)
def grow_my_loop(seed_notes: Annotated[list[dict], Field(description='Your loop. ' + _NOTES)],
                 n: Annotated[int, Field(description='How many extended versions to return.')] = 3,
                 key: Annotated[str | None, Field(description=_KEY)] = None,
                 tempo: Annotated[float, Field(description='Tempo in BPM, used for humanize timing and reported in the result.')] = 120.0,
                 cont_bars: Annotated[int, Field(description='Length of each continuation in bars.')] = 4,
                 seed: Annotated[int, Field(description=_SEED)] = 0,
                 humanize: Annotated[bool, Field(description='Apply your learned velocity/timing feel to the continuations only; your loop is never changed.')] = False,
                 use_ears: Annotated[bool | None, Field(description=_USE_EARS)] = None) -> dict:
    """Stretch YOUR loop: returns your idea untouched, each extended with a short
    continuation that grows out of it and introduces one rare-but-native move — a melodic
    cell, an under-used scale degree, or a rare progression. Unlike suggest_growth (which
    proposes moves from scratch), every suggestion is anchored to the seed you pass: the
    continuation is voice-led from your last note and sits in your register/groove, landing
    after the loop. Each suggestion's `notes` (your loop + its continuation) feeds
    rate_candidates() so taste learns from stretches of your real material.
    humanize=True applies your learned velocity/timing DNA to the continuations only.
    With use_ears the pick is fit to the room — see hear_the_room."""
    return _core.grow_my_loop(seed_notes=seed_notes, n=n, key=key, tempo=tempo,
                              cont_bars=cont_bars, seed=seed, humanize=humanize,
                              use_ears=use_ears)


def _stamp_source_for_answer(ans: dict, ref_id: str | None,
                             ref_drove_pick: bool) -> str:
    """Ledger stamp source for a JAM write: the steering ref when it drove the
    pick, 'gold_composed' when the pick is a composed gold (no index phrase
    behind it — never 'catalogue'), else 'catalogue'."""
    if ref_drove_pick:
        return f"ref:{ref_id}"
    if ((ans.get("source_provenance") or {}).get("clip_source")
            == "gold_composed"):
        return "gold_composed"
    return "catalogue"


@mcp.tool(annotations=_WRITES)
def answer_my_loop(seed_notes: Annotated[list[dict] | None, Field(description='Your loop to answer. ' + _SEED_FROM_LIVE)] = None,
                   bars: Annotated[int | None, Field(description="Answer length in bars (gear 2 only). Omit to match the loop's length.")] = None,
                   key: Annotated[str | None, Field(description=_KEY)] = None,
                   tempo: Annotated[float | None, Field(description="Tempo in BPM for the answer. Omit to use the source phrase's tempo, else the profile's mean, else 120 (gears 1-3; gear 4 defaults to 120).")] = None,
                   transform: Annotated[str | None, Field(description="Gear 2 only: one velocity-preserving transform, 'fragment' or 'reharmonize', or 'auto' to pick one by seed.")] = None,
                   humanize: Annotated[bool, Field(description='Gear 2 only: apply your learned velocity/timing feel to the answer.')] = False,
                   write: Annotated[bool, Field(description='Also write the answer to the SIDEKICK JAM track (read back and stamped in the ledger). Gears 5-6 never write here; use write_composed_to_jam.')] = False,
                   seed: Annotated[int, Field(description=_SEED)] = 0,
                   gear: Annotated[int, Field(description="1 recall (your nearest phrase as is), 2 recontext (transposed, voice-led; default), 3 develop (one motif surgery), 4 grow (composed continuation, role='lead' only), 5 compose / 6 cover (returns a contract for the LLM to compose against).")] = 2,
                   surgery: Annotated[str | None, Field(description='Gear 3 only: ghost_answer, bass_shadow, broken_rhythm, ending_variation or octave_displacement. Omit to pick one by seed.')] = None,
                   direction: Annotated[str | None, Field(description=_DIRECTION)] = None,
                   axes: Annotated[dict | None, Field(description='Gears 5-6 only: aesthetic targets from -1.0 to 1.0 on faithful_alien, sparse_maximal, song_club, human_machine, romantic_brutal, clean_damaged, darker_brighter.')] = None,
                   use_ears: Annotated[bool | None, Field(description=_USE_EARS)] = None,
                   moment: Annotated[dict | None, Field(description=_MOMENT)] = None,
                   ref_id: Annotated[str | None, Field(description='An audio reference id from find_audio_refs. On gear 2 it narrows which of your phrases can answer; on other gears its brief travels with the answer as text.')] = None,
                   role: Annotated[str, Field(description="'lead' (default) or 'bass': which of your phrase banks and which register the answer uses.")] = "lead",
                   relax: Annotated[str | None, Field(description=_RELAX)] = None) -> dict:
    """Call-and-response from Ricardo's OWN material, through the GEARBOX:
    1 RECALL (his nearest phrase, verbatim) · 2 RECONTEXT (transposed, voice-led —
    default) · 3 DEVELOP (one named motif surgery: ghost_answer / bass_shadow /
    broken_rhythm / ending_variation / octave_displacement) · 4 GROW (composed
    continuation, one rare-but-native move) · 5 COMPOSE / 6 COVER (returns a CONTRACT —
    the LLM composes in-session with sombra-brain, gears.validate_composed gates it).
    write=True lands note-producing gears on the 'SIDEKICK JAM' track (the ONLY
    auto-write surface). When one slaps: gold its source.
    With use_ears the pick is fit to the room — see hear_the_room.
    With ref_id (from find_audio_refs) the pick is ALSO steered toward that
    reference: HIS phrases, narrowed by the ref's tempo/density and nudged toward
    its register. The room still wins where they disagree, and a reference never
    refuses an answer — it narrows, and says so. Gear 2 only.
    Ownership never gates the write: a ref of ANY ownership (his, another
    artist's, unknown) steers WHICH of his phrases answer, and what lands
    on JAM is always his own phrases — write=True behaves the same
    whichever record steered the pick.
    role='bass' answers a bass riff as a bass line (his bass phrases, the bass
    register); default 'lead'. When the ears report no_fit with a relaxation
    offer, relax='auto' (or a gate name) accepts it."""
    from mcp_server import live_write as _live_write

    ref_steer = None
    if ref_id:
        _ar = _audio_ref_mod()
        _ref_card = _ar.get_ref_card(ref_id)
        ref_steer = _ar.ref_steering(_ref_card)

    seed_source = None
    if seed_notes is None:
        live = _run_async(_live_write.read_current_clip())
        seed_notes = _live_write.notes_or_unread_error(live, (
            f"no notes in the current selection (source={live.get('source')}, "
            f"read_error={live.get('read_error')}) — select a MIDI clip or pass seed_notes"))
        seed_source = {"source": live.get("source"), "n_notes": live.get("n_notes")}

    def _ref_prose_block(g: int) -> dict:
        """A reference's brief, on a gear whose gates it cannot steer.

        The gates are gear-2 only because they narrow RETRIEVAL, which the other
        gears bypass, reshape after the fact, or never do. The PROSE needs no
        retrieval, so refusing to carry it was the one place in the whole
        reference path where a reference refused an answer — against the
        contract in this tool's own docstring."""
        _ar_mod = _audio_ref_mod()
        _why = {
            1: ("gear 1 returns your phrase verbatim — the reference can't shape "
                "it. Its brief is here; gear 2 lets it narrow which phrase you get."),
            3: ("gear 3 reshapes after retrieval, so the reference's tempo/density "
                "narrowing doesn't apply. Its brief is here."),
            4: ("gear 4 composes a continuation, so the reference's narrowing "
                "doesn't apply. Its brief is here."),
        }.get(g, "no retrieval on a contract gear — the reference travels as a "
                 "brief. Compose in Ricardo's grammar; these rules shape "
                 "placement, not content.")
        return {
            "ref_id": ref_id,
            "steering": "prose_only",
            "notes": [_why],
            "unmapped": ref_steer.get("unmapped") or [],
            "identity": ref_steer.get("identity"),
            "contract_version": ref_steer.get("contract_version"),
            "brief": _ar_mod.ref_brief(_ar_mod.get_ref_card(ref_id)),
        }

    if gear != 2:
        import gears as _gears
        ans = _gears.answer_with_gear(seed_notes, gear=gear, key=key, tempo=tempo,
                                      surgery=surgery, direction=direction,
                                      axes=axes, seed=seed, use_ears=use_ears,
                                      moment=moment, role=role, relax=relax)
        if ref_steer is not None:
            # gates are gear-2 only; the brief travels on every gear
            ans["ref"] = _ref_prose_block(gear)
            ans.setdefault("assumptions", []).extend(ans["ref"]["notes"])
        if gear >= 5:
            # a contract, not notes — the LLM composes, validate_composed gates,
            # and write_composed_to_jam is the stamped way to land what passes
            ans["write_via"] = ("write_composed_to_jam(notes, contract=<this>, "
                                "name, role=..., confirm=True)")
            if write:
                ans["live_write_skipped"] = (
                    "gear %d returns a contract — compose, validate, then call "
                    "write_composed_to_jam" % gear)
            return ans
    else:
        import develop_session as _develop_session
        _dst = _develop_session.load_state()
        if _dst.get("sticky") and _dst.get("hold"):
            _hold = _dst["hold"]
            _move = _develop_session.current_move(_dst)
            ans = _core.develop_my_loop(
                seed_notes=_hold["seed_notes"], move=_move, key=_hold["key"],
                bars=bars, seed=_hold["pass"], use_ears=use_ears,
                direction=direction, pin_source=_hold["source_phrase_id"],
                moment=moment, role=role, relax=relax)
            ans["sticky_develop"] = (
                "hold on — holding your idea, pass %d (%s)"
                % (_hold["pass"], _move))
            if ref_steer is not None:
                # a hold pins ONE phrase on purpose, so narrowing the pool is a
                # no-op — but the brief still travels
                ans["ref"] = _ref_prose_block(2)
                ans["ref"]["notes"] = [
                    "reference's gates ignored — you're holding an idea. Its "
                    "brief still travels; drop the hold (sticky_develop off) to "
                    "let it narrow the pool."]
                ans.setdefault("assumptions", []).extend(ans["ref"]["notes"])
        else:
            ans = _core.answer_my_loop(seed_notes=seed_notes, bars=bars, key=key,
                                       tempo=tempo, transform=transform,
                                       humanize=humanize, seed=seed,
                                       use_ears=use_ears, direction=direction,
                                       ref=ref_steer, moment=moment,
                                       role=role, relax=relax)
            if (ref_steer is not None
                    and (ans.get("ref") or {}).get("steering") == "gates"
                    and not ans.get("no_fit")):
                # Honest stamping: one extra retrieval with the same seed and
                # no reference. A ref that narrowed nothing keeps its brief
                # but must not take credit for the pick.
                try:
                    _unsteered = _core.answer_my_loop(
                        seed_notes=seed_notes, bars=bars, key=key,
                        tempo=tempo, transform=transform,
                        humanize=humanize, seed=seed,
                        use_ears=use_ears, direction=direction,
                        ref=None, moment=moment,
                        role=role, relax=relax)
                    _changed = (_unsteered.get("source_phrase_ids")
                                != ans.get("source_phrase_ids"))
                    ans["ref"]["changed_pick"] = bool(_changed)
                    if not _changed:
                        ans.setdefault("assumptions", []).append(
                            "reference %s left the pick unchanged "
                            "(same phrase with and without it)" % ref_id)
                except Exception as _exc:
                    ans["ref"]["changed_pick"] = True
                    ans.setdefault("assumptions", []).append(
                        "unsteered comparison failed (%s: %s) — "
                        "stamped ref:%s as before"
                        % (type(_exc).__name__, _exc, ref_id))
    if seed_source:
        ans["seed_source"] = seed_source
    # The same validated Set-take context travels with ref-steered answers so the
    # composer can judge a reference against material already in the session.
    ans = _core._fold_persisted_session_takes(ans)
    if write:
        import jam_lifecycle as _jl

        if ans.get("no_fit"):
            _jl.handle_no_fit_refusal(ans)
        else:
            _clip_name = f"ans {str((ans.get('source_phrase_ids') or ['g%d' % gear])[0])[:20]} s{seed}"
            # Provenance by what actually STEERED, not by what was passed.
            # A reference only narrows retrieval on gear 2; elsewhere it
            # travels as prose the composer may ignore. And a ref that left
            # the pick unchanged keeps its brief but takes no credit.
            # Stamping either ref-assisted would corrupt the source x role x
            # gear strata that exist to keep the survival rate honest.
            _ref_drove_pick = (
                bool(ref_id)
                and (ans.get("ref") or {}).get("steering") == "gates"
                and bool((ans.get("ref") or {}).get("changed_pick")))
            if ref_id and not _ref_drove_pick:
                # the reference is not lost, just not credited with the notes
                if (ans.get("ref") or {}).get("steering") != "gates":
                    ans.setdefault("assumptions", []).append(
                        "recorded as catalogue: reference %s travelled as prose on "
                        "gear %d and did not narrow retrieval" % (ref_id, gear))
                else:
                    ans.setdefault("assumptions", []).append(
                        "recorded as catalogue: reference %s narrowed "
                        "retrieval but the pick stands" % ref_id)
            _stamp_source = _stamp_source_for_answer(ans, ref_id, _ref_drove_pick)
            if _stamp_source == "gold_composed":
                ans.setdefault("assumptions", []).append(
                    "stamped gold_composed: this answer serves a composed gold, "
                    "not a catalog phrase")
            _jl.write_answer_to_jam(
                ans,
                notes=ans["answer_notes"],
                clip_name=_clip_name,
                tempo=ans["tempo"],
                write_fn=lambda notes, *, clip_name, tempo: _run_async(
                    _live_write.write_answer_to_jam_track(
                        notes, clip_name=clip_name, tempo=tempo)),
                source=_stamp_source,
                # not lost, just not credited with the notes
                ref_prose=(ref_id if ref_id and not _ref_drove_pick else None),
                role=ans.get("role", "lead"),
                gear=gear,
                # seed memory: the next re-roll skips this source for a
                # near-identical seed (seed-to-seed, never seed-to-answer)
                seed_notes=seed_notes,
            )
    return ans


@mcp.tool(annotations=_READ_ONLY)
def develop_my_loop(seed_notes: Annotated[list[dict] | None, Field(description='Your loop. ' + _SEED_FROM_LIVE)] = None,
                    move: Annotated[str | None, Field(description="One named move: slow-burn:reveal, slow-burn:accent, slow-burn:add, motivic:reharm, motivic:retro, motivic:fragment, motivic:walk, motivic:invert; bare 'slow-burn' means reveal, bare 'motivic' picks one by seed. Omit to return the retrieved phrase untouched.")] = None,
                    key: Annotated[str | None, Field(description=_KEY)] = None,
                    tempo: Annotated[float | None, Field(description="Tempo in BPM for the result. Omit to use the source phrase's tempo, else the profile's mean, else 120.")] = None,
                    bars: Annotated[int | None, Field(description="Result length in bars. Omit to match the loop's length.")] = None,
                    seed: Annotated[int, Field(description='Random seed; also drives the slow-burn pass and the motivic walk direction.')] = 0,
                    use_ears: Annotated[bool | None, Field(description=_USE_EARS)] = None,
                    direction: Annotated[str | None, Field(description=_DIRECTION)] = None) -> dict:
    """Develop HIS loop: retrieve ONE real phrase from his catalog and apply ONE
    named move — slow-burn:reveal, slow-burn:accent, slow-burn:add,
    motivic:reharm, motivic:retro, motivic:fragment, motivic:walk,
    motivic:invert (bare slow-burn aliases reveal; bare motivic picks one of
    the five motivic ops by seed). seed drives slow-burn pass index and
    alphabet-walk direction. use_ears defaults off until promotion — see
    hear_the_room. Never writes to Live."""
    from mcp_server import live_write as _live_write

    if seed_notes is None:
        live = _run_async(_live_write.read_current_clip())
        seed_notes = _live_write.notes_or_unread_error(live, (
            f"no notes in the current selection (source={live.get('source')}, "
            f"read_error={live.get('read_error')}) — select a MIDI clip or pass seed_notes"))

    return _core.develop_my_loop(seed_notes=seed_notes, move=move, key=key,
                                 tempo=tempo, bars=bars, seed=seed,
                                 use_ears=use_ears, direction=direction)


@mcp.tool()
def lay_the_song(scene_index: int = 0, parts: list[dict] | None = None,
                 target_minutes: float | None = None, tempo: float | None = None,
                 seed: int = 0, locators: bool = True, commit: bool = False,
                 confirm: bool = False, slot: str | None = None,
                 seed_notes: list[dict] | None = None, key: str | None = None,
                 part_role: str = "lead") -> dict:
    """Preview or confirm a role-aware full-song map from one Live scene.

    Automatic discovery reports unknown/ambiguous track roles as ``needs_parts``;
    pass explicit ``part_id``/``role``/``source_path`` objects to resolve them.
    ``confirm=False`` performs no writes, including when locators are requested.
    Legacy ``slot`` and ``seed_notes`` preview inputs remain accepted.
    """
    from mcp_server import live_write as _live_write
    from mcp_server import pp_session as _pp_session
    from mcp_server import song_structure as _song_structure

    if seed_notes is None and slot:
        t_i, s_i = (int(x) for x in slot.split("/"))
        read = _run_async(_live_write.read_clip(t_i, s_i))
        if read.get("unread"):
            _live_write.notes_or_unread_error(read, "")
        seed_notes = read.get("notes") or None

    if parts is None and slot:
        t_i, s_i = (int(x) for x in slot.split("/"))
        parts = [{"part_id": "legacy", "role": part_role,
                  "source_path": "t%d/s%d" % (t_i, s_i)}]

    unresolved = []
    if parts is None and seed_notes is not None:
        resolved_parts = []
    elif parts is None:
        discovered = _run_async(_song_structure.discover_scene_parts(
            scene_index, _pp_session.operation_session))
        resolved_parts = discovered["parts"]
        unresolved = discovered["unresolved"]
        if tempo is None:
            tempo = discovered["tempo"]
    else:
        resolved_parts = _song_structure.validate_parts(parts)

    m = _core.plan_song_map(seed_notes=seed_notes, key=key, tempo=tempo,
                            target_minutes=target_minutes, seed=seed)
    m["parts"] = resolved_parts
    m["part_resolution"] = "needs_parts" if unresolved else "resolved"
    m["unresolved"] = unresolved
    m["intended_operations"] = {
        "locators": list(m["locators"]) if locators else [],
        "placements": _song_structure.part_placements(resolved_parts, m["sections"]),
    }
    m["locators_written"] = {"created": 0, "executed": False}
    m["committed"] = {"created": 0, "executed": False}
    if not confirm:
        return m
    if unresolved:
        raise ValueError("confirmed lay_the_song requires explicit parts for unresolved tracks")
    locator_rollback = None
    if locators:
        m["locators_written"] = _run_async(_live_write.write_song_locators(m))
        locator_rollback = m["locators_written"].pop("_rollback", None)
        m["locators_written"]["executed"] = True
    if not commit:
        return m
    if not resolved_parts:
        raise ValueError("commit=True requires at least one explicit or discovered part")
    from mcp_server import arrangement_exec as _arrangement_exec
    try:
        m["committed"] = _run_async(_arrangement_exec.execute_arrangement(
            resolved_parts, m["intended_operations"]["placements"]))
    except Exception as exc:
        if locator_rollback is not None:
            try:
                _run_async(_live_write.rollback_song_locators(locator_rollback))
            except Exception as rollback_exc:
                raise RuntimeError(
                    "arrangement failed and locator rollback could not be verified") from rollback_exc
        raise
    m["committed"]["executed"] = True
    return m


@mcp.tool()
def audition_in_lanes(source_path: str, n: int = 3, seed: int = 0,
                      confirm: bool = False) -> dict:
    """Preview or create a fresh SIDEKICK audition track with up to eight take lanes.

    Requires Producer Pal 2.2 and a MIDI session clip at ``source_path``.
    ``confirm=False`` inspects only; confirmed partial failures delete the fresh
    audition track. Candidate IDs are opaque and are consumed by ``promote_lane``.
    """
    from mcp_server import lane_audition as _lane_audition

    return _run_async(_lane_audition.audition_in_lanes(
        source_path, n=n, seed=seed, confirm=confirm))


@mcp.tool()
def promote_lane(audition_id: str, candidate_id: str,
                 confirm: bool = False) -> dict:
    """Preview or promote one persisted take-lane candidate and record verified KEEP.

    Producer Pal 2.2 duplicates the candidate to the audition track's main lane.
    KEEP is recorded only after note readback matches. Take-lane duplication may
    discard clip envelopes, so this tool never claims envelope preservation.
    """
    from mcp_server import lane_audition as _lane_audition

    return _run_async(_lane_audition.promote_lane(
        audition_id, candidate_id, confirm=confirm))


@mcp.tool()
def three_endings(seed_notes: list[dict] | None = None, slot: str | None = None,
                  key: str | None = None, tempo: float | None = None,
                  bars: int = 4, write: bool = False, seed: int = 0) -> dict:
    """3 ENDINGS — three landings built from the loop's own final bar — decay /
    subtraction / hard-stop-into-room. No middle options. write=True lands all three
    on SIDEKICK JAM (KEEP one, DROP two). Seed via slot='track/scene' or seed_notes."""
    import rituals as _rituals
    from mcp_server import live_write as _live_write

    if seed_notes is None and slot:
        t_i, s_i = (int(x) for x in slot.split("/"))
        seed_notes = _live_write.notes_or_unread_error(
            _run_async(_live_write.read_clip(t_i, s_i)),
            "give me the loop that needs to land: slot= or seed_notes=")
    if not seed_notes:
        raise ValueError("give me the loop that needs to land: slot= or seed_notes=")
    res = _rituals.three_endings(seed_notes, key=key, tempo=tempo, bars=bars, seed=seed)
    if write:
        import jam_lifecycle as _jl

        writes = []
        for e in res["endings"]:
            _name = f"END {e['name']}"
            _stamped: dict = {
                "tempo": res.get("tempo"),
                "develop_move": f"ending:{e['name'].replace('-', '_')}",
            }
            _lw = _jl.write_answer_to_jam(
                _stamped,
                notes=e["notes"],
                clip_name=_name,
                tempo=res.get("tempo") or 120.0,
                write_fn=lambda notes, *, clip_name, tempo: _run_async(
                    _live_write.write_answer_to_jam_track(
                        notes, clip_name=clip_name, tempo=tempo)),
                source="catalogue",
                role="lead",
                gear=None,
                key=res.get("key"),
            )
            if _lw is None:
                writes.append({"error": _stamped["live_write_error"]})
            else:
                _lw_out = dict(_lw)
                _lw_out.update({k: v for k, v in _stamped.items()
                                if k in ("ledger", "ledger_error",
                                         "scoreable", "scoreable_reason")})
                writes.append(_lw_out)
        res["live_writes"] = writes
    return res


@mcp.tool()
def bury_or_marry(action: str = "deal", project: str | None = None,
                  eulogy: str | None = None, deadline: str | None = None) -> dict:
    """REVISIT — deal one of your saved ideas: keep it in play or retire it.
    action='deal' serves ONE unresolved project (most deferred decisions first);
    'bury' needs project+eulogy (one line into NOTES.md's Graveyard; material stays
    an organ donor); 'marry' commits to finishing it (optional deadline) and returns
    the whats_missing diagnosis to start on. No third option."""
    import rituals as _rituals

    action = str(action).lower()
    if action == "deal":
        return _rituals.deal()
    if action == "bury":
        if not project or not eulogy:
            raise ValueError("bury needs project= and a one-line eulogy=")
        return _rituals.bury(project, eulogy)
    if action == "marry":
        if not project:
            raise ValueError("marry needs project=")
        return _rituals.marry(project, deadline=deadline)
    raise ValueError("action must be deal | bury | marry")


@mcp.tool()
def version_courage(family: str) -> dict:
    """VERSION_COURAGE: face a version family (e.g. 'demosong', '1BBB'). Names what is
    UNIQUE in each member, recommends ONE to marry (most song-shaped, not newest),
    lists what the others donate to the crate. Bury/marry stay separate calls."""
    import rituals as _rituals

    return _rituals.version_courage(family)


@mcp.tool()
def one_hour_close(project: str) -> dict:
    """ONE_HOUR_CLOSE: the bounce ritual — the project's top findings become ≤10-min
    finishing moves (with executable commands where they exist) and the hour ENDS IN
    A DATED BOUNCE, imperfect allowed. No new ideas; impulses go to NOTES."""
    import rituals as _rituals

    return _rituals.one_hour_close(project=project)


@mcp.tool()
def confirm_bounce(project: str, bounce_path: str, confirm: bool = False) -> dict:
    """Verify a fresh audio bounce; confirm=True appends a model_on_request bounce record."""
    import rituals as _rituals

    return _rituals.confirm_bounce(project, bounce_path, confirm=confirm)


@mcp.tool()
def declare_finishing_state(project: str, state: Literal["complete_draft"],
                            note: str | None = None) -> dict:
    """Record a model_on_request complete_draft marker; acceptance/release are owner-only."""
    import rituals as _rituals

    return _rituals.declare_finishing_state(project, state, note=note)


@mcp.tool()
def sombra_status() -> dict:
    """The SOMBRA pedalboard's nervous system: is the listener up (device lamp
    blinking?), which slot holds the last pedal-made answer, recent pedal events."""
    import sombra_listener as _sl

    return _sl.status()


@mcp.tool()
def no_new_notes(minutes: int = 60) -> dict:
    """NO_NEW_NOTES: session-mode contract — arrangement, muting, automation,
    deletion, transposition of EXISTING material only; gears above 3 refuse for the
    duration. Arrangement is composition."""
    import rituals as _rituals

    return _rituals.no_new_notes(minutes=minutes)


@mcp.tool()
def sticky_develop(on: bool | None = None) -> dict:
    """HOLD MY IDEA — when on, KEEP holds your idea and the answer path serves
    it developed across passes; DROP releases. None reports state."""
    import develop_session as _develop_session

    import device_readout as _device_readout

    if on is None:
        st = _develop_session.load_state()
    else:
        st = _develop_session.set_sticky(on)
    hold = st.get("hold")
    out = {
        "sticky": bool(st.get("sticky")),
        "holding": hold is not None,
        "pass": hold["pass"] if hold else None,
        "current_move": (_develop_session.current_move(st) if hold else None),
        "source_phrase_id": hold["source_phrase_id"] if hold else None,
        "announcement": _device_readout.sticky_announcement(st),
    }
    return out


def _ledger_normalize_notes(notes: list[dict]) -> list[dict]:
    import ledger as _ledger_resolve
    return _ledger_resolve._normalize_answer_notes(notes)


@mcp.tool(annotations=_MAY_REPLACE)
def judge_answer(verdict: Annotated[str, Field(description="'keep' or 'drop'.")],
                 slot: Annotated[str | None, Field(description="Live slot of the judged clip as 'track/scene', e.g. '3/0'; binds the verdict to the write there.")] = None,
                 source_phrase_id: Annotated[str | None, Field(description='Catalogue phrase the answer came from (KEEP golds it, DROP suppresses it). Normally taken from the ledger row.')] = None,
                 answer_notes: Annotated[list[dict] | None, Field(description='The notes that were heard; must match the verified write when a ledger row is bound. ' + _NOTES)] = None,
                 reason: Annotated[str | None, Field(description="Optional, one word is plenty. 'too static', 'too jumpy' or 'wrong register' demote the matching ears bucket for the session.")] = None,
                 confirm: Annotated[bool, Field(description='DROP only: True deletes the judged clip from SIDEKICK JAM after checking its verified clip and Set identity; False previews the deletion. KEEP never deletes.')] = False,
                 seed_notes: Annotated[list[dict] | None, Field(description='The loop the answer responded to; on KEEP it is remembered for hold-my-idea (sticky_develop). ' + _NOTES)] = None,
                 key: Annotated[str | None, Field(description='Key of the answer; must agree with the ledger row when one is bound.')] = None,
                 role: Annotated[str | None, Field(description='Role of the answer; must agree with the ledger row when one is bound.')] = None,
                 entry_id: Annotated[str | None, Field(description='Ledger entry id to judge, naming the row explicitly (needed when the bare judgment is refused). Also judges arrangement experiments.')] = None,
                 source: Annotated[str | None, Field(description="Stamp source such as 'catalogue' or 'ref:<ref_id>'; must agree with the ledger row when one is bound.")] = None) -> dict:
    """KEEP/DROP with teeth. KEEP golds the answer's source phrase (the crate digs
    itself). DROP logs an explicit negative and suppresses that phrase (and its
    near-duplicates) from future answers — clear_rejects() undoes it. With
    confirm=True and a slot it also deletes the clip, refusing any track not
    literally named SIDEKICK JAM; confirm=False previews the deletion.

    A bare judge_answer('drop') binds the durable judgment cursor for the
    current session — and is REFUSED when no live session remains inside the
    6-hour idle window; the refusal names the pending entries so the press can
    name one explicitly by ``entry_id``. An already-judged cursor replays only
    for the same verdict within 15 minutes of the original press; after that
    the press must name its entry. ``reason`` is optional; one word is plenty,
    and 'too static' / 'too jumpy' / 'wrong register' demote the matching
    ears bucket for the session, ears on or off. A DROP of a moved answer
    (transform/surgery) bans only that (source, move) pairing — the untouched
    source stays servable."""
    import gears as _gears
    import ledger as _ledger_resolve

    if entry_id is not None:
        # An arrangement experiment / effect trial (register_arrangement_candidates)
        # is judged here and nowhere else: its verdict is arrangement feedback,
        # never note-material learning — no gold, no ban, no rating, no tally.
        import experiments as _experiments
        routed = _experiments.judge(entry_id, verdict, reason=reason,
                                    path=_ledger_resolve.LEDGER_PATH)
        if routed is not None:
            try:
                routed["ledger_status"] = _ledger_resolve.ledger_status(
                    path=_ledger_resolve.LEDGER_PATH)
            except Exception:  # noqa: BLE001 — loop state is a rider, never the verdict
                pass
            return routed

    bare_judgment = (source_phrase_id is None and answer_notes is None and entry_id is None
                     and slot is None)
    # One binder decides which row this verdict belongs to; recency is
    # identity nowhere else, and a refusal names the pending rows instead
    # of guessing (or stamping a three-week-old night).
    binding = _ledger_resolve.resolve_judgment_target(
        _ledger_resolve.load(), verdict, entry_id=entry_id, slot=slot,
        answer_notes=answer_notes, source_phrase_id=source_phrase_id,
        origin="mcp")
    if binding.get("refused"):
        raise ValueError(binding["error"])
    bound_entry = binding.get("entry")
    binding_note = None if bound_entry is not None else binding.get("note")

    if bound_entry is not None and bound_entry.get("judged_snapshot") is not None:
        # The row may have been revised after the original judgment. Keep the
        # retry bound to the material and Live identity that was actually heard.
        bound_entry = {**bound_entry, **bound_entry["judged_snapshot"]}
    if bare_judgment and bound_entry is not None:
        # Persist the cursor before any sink can fail. A retry therefore resolves
        # the same write even when another pending write shares its slot.
        _ledger_resolve.bind_judgment_target(
            bound_entry["id"], path=_ledger_resolve.LEDGER_PATH)

    if bound_entry is not None:
        if bound_entry.get("immediate_verdict") is not None and \
                bound_entry.get("immediate_verdict") != str(verdict).strip().lower():
            raise ValueError("judgment verdict contradicts the existing immutable verdict")
        # Roles compare canonically ('drums' IS 'drum') and the honest
        # 'unknown' placeholder compares as no role at all.
        supplied = {"answer_notes": answer_notes,
                    "role": _ledger_resolve.learning_role(role),
                    "key": key,
                    "source_phrase_id": source_phrase_id, "source": source}
        authoritative = {"answer_notes": bound_entry.get("answer_notes"),
                         "role": _ledger_resolve.learning_role(
                             bound_entry.get("role")),
                         "key": bound_entry.get("key"),
                         "source_phrase_id": bound_entry.get("source_phrase_id"),
                         "source": bound_entry.get("source")}
        if answer_notes is not None:
            try:
                given_notes = _ledger_normalize_notes(answer_notes)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("answer_notes could not be normalized") from exc
            if authoritative["answer_notes"] is not None and \
                    given_notes != authoritative["answer_notes"]:
                raise ValueError("judgment answer_notes contradict the verified write")
        for field in ("role", "key", "source_phrase_id", "source"):
            if supplied[field] is not None and authoritative[field] is not None and \
                    (not _ledger_resolve._same_phrase_id(
                        supplied[field], authoritative[field])
                     if field == "source_phrase_id"
                     else supplied[field] != authoritative[field]):
                raise ValueError("judgment %s contradicts the verified write" % field)
        entry_id = bound_entry.get("id")
        source_phrase_id = bound_entry.get("source_phrase_id") or source_phrase_id
        # The write ledger is authoritative for what was actually heard and for
        # the controls used to serve it. Explicit values remain useful when a
        # legacy row lacks that field, but never replace a stored snapshot.
        if answer_notes is None:
            answer_notes = bound_entry.get("answer_notes")
        if role is None:
            role = bound_entry.get("role")
        # the 'unknown' placeholder is bookkeeping: learning sinks get None
        # so the taste model infers from register instead of trusting it
        role = _ledger_resolve.learning_role(role)
        if key is None:
            key = bound_entry.get("key")
        slot = bound_entry.get("slot")
        source = bound_entry.get("source") if source is None else source
        internal_idempotency_key = f"judge:{entry_id}:{str(verdict).strip().lower()}"
        resolved_from = {"entry_id": entry_id,
                         "clip_name": bound_entry.get("clip_name"),
                         "slot": slot}
    else:
        internal_idempotency_key = None
        resolved_from = None

    # Verdict first: the ledger row is the durable fact his score is computed
    # from. Learning sinks are retryable through the judgment journal; a
    # verdict lost to a later crash is not. record_judgment's second stamp
    # below hits the same-verdict branch and marks the journal's ledger step.
    verdict_word = str(verdict).strip().lower()
    # A row with no material (no answer notes, no source phrase the index
    # can resolve) has nothing to learn from: the verdict lands and learning
    # is skipped with a note — never a retry-forever learning failure.
    skip_learning_note = None
    if bound_entry is not None and answer_notes is None:
        from corpus_keys import phrase_lookup as _resolvable_lookup
        resolvable = source_phrase_id is not None and source_phrase_id in \
            _resolvable_lookup(_gears.core._phrases())
        if not resolvable:
            skip_learning_note = (
                "verdict recorded; nothing to learn — the row has no answer "
                "notes and no source phrase the index can resolve")
    if bound_entry is not None:
        try:
            _ledger_resolve.record_verdict(
                verdict_word, entry_id=bound_entry["id"],
                path=_ledger_resolve.LEDGER_PATH)
        except Exception:  # noqa: BLE001 — record_judgment retries and surfaces
            pass

    if skip_learning_note is not None:
        res = {"verdict": verdict_word, "note": skip_learning_note}
        if entry_id:
            res["entry_id"] = entry_id
        # no learning ran, so there is no journal to finish on retry
        internal_idempotency_key = None
    else:
        try:
            res = _gears.judge_answer(verdict, source_phrase_id=source_phrase_id,
                                      answer_notes=answer_notes, reason=reason,
                                      seed_notes=seed_notes, key=key, role=role,
                                      idempotency_key=internal_idempotency_key,
                                      entry_id=entry_id,
                                      ledger_path=_ledger_resolve.LEDGER_PATH,
                                      source=source)
        except Exception as exc:  # noqa: BLE001 — a sink crash never undoes the verdict
            # "The ledger verdict stands" is only honest when a verdict actually
            # landed. With nothing bound (or the stamp failed) this exception is
            # an input or contradiction error and must surface as one.
            verdict_landed = False
            if entry_id:
                landed_row = next(
                    (e for e in _ledger_resolve.load(
                        _ledger_resolve.LEDGER_PATH).get("entries", [])
                     if e.get("id") == entry_id), None)
                verdict_landed = bool(landed_row and
                                      landed_row.get("immediate_verdict") == verdict_word)
            if not verdict_landed:
                raise
            stage = "learning"
            if internal_idempotency_key is not None:
                event = _ledger_resolve.judgment_event(
                    internal_idempotency_key, path=_ledger_resolve.LEDGER_PATH)
                failed = sorted(name for name, step in (event or {}).get("steps", {}).items()
                                if step.get("status") == "failed")
                stage = failed[-1] if failed else stage
            res = {"verdict": verdict_word,
                   "learning_failure": {
                       "stage": stage,
                       "error": f"{type(exc).__name__}: {exc}",
                       "retry": "re-send the same judgment to finish learning "
                                "(entry_id=%s)" % entry_id},
                   "note": "the ledger verdict stands; learning did not complete"}
            if entry_id:
                res["entry_id"] = entry_id
    if resolved_from:
        res["resolved_from"] = resolved_from
    if binding.get("replay"):
        res["replay"] = True
    if binding.get("late"):
        # this verdict scores as late (the night was already sealed or
        # closed when it landed) — say so
        res["late"] = True
    if binding_note:
        res["ledger_binding"] = binding_note
    import jam_lifecycle as _jl
    import ledger as _ledger
    from mcp_server import live_write as _live_write

    # An explicit judgment can still be useful when the ledger predates the
    # answer snapshot.  It must never stamp a different row by slot.
    record_verdict_fn = _ledger.record_verdict if bound_entry is not None else (
        lambda *_args, **_kwargs: None)
    _jl.record_judgment(
        res,
        slot=slot,
        confirm=confirm,
        entry_id=entry_id,
        record_verdict_fn=record_verdict_fn,
        no_match_note=binding_note or _jl.MCP_NO_LEDGER_MATCH,
        delete_jam_clip_fn=lambda s, **expected: _run_async(
            _live_write.delete_jam_clip(s, **expected)),
        clip_ref=(bound_entry or {}).get("clip_ref"),
        set_identity=(bound_entry or {}).get("set_identity"),
        event_id=internal_idempotency_key,
        conflict_fingerprint=None,
        ledger_path=_ledger_resolve.LEDGER_PATH,
    )
    try:
        res["ledger_status"] = _ledger_resolve.ledger_status(
            path=_ledger_resolve.LEDGER_PATH)
    except Exception:  # noqa: BLE001 — loop state is a rider, never the verdict
        pass
    _ledger_ref = res.get("ledger") if isinstance(res.get("ledger"), dict) else {}
    _acct = res.get("accounting_failure")
    _ledger_recorded = bool(_ledger_ref.get("entry_id")) and not (
        isinstance(_acct, dict) and _acct.get("status") == "retryable")
    if _ledger_recorded:
        # Only a verdict the ledger actually recorded counts — a failed or
        # conflicting verdict (no entry id, or a retryable accounting
        # failure) tallies nothing, and the retry that lands it counts once
        # through the card's tallied_entries.
        _v = str(verdict).strip().lower()
        if _v in ("keep", "drop"):
            _tally = _record_ref_tally_for_source(
                source, "kept" if _v == "keep" else "dropped",
                entry_id=_ledger_ref.get("entry_id"))
            if _tally is not None:
                res["ref_tally"] = _tally
    return res


@mcp.tool()
def whats_missing(project: str, max_tracks: int | None = None,
                  intended_findings: list[str | dict] | str | None = None) -> dict:
    """FINISH: read a project like a producer. Returns the structured diagnosis —
    frame, detected sections, NAMED dramaturg findings (loop_with_ambitions,
    flat_energy_run, no_breathe, drop_without_contrast, uncommitted_ending,
    comping_debris, b_section_missing, silent_gap, unison_wall, …) — plus verb-menu
    proposals (remove/answer/thicken/thin/transpose/humanize/arrange). Facts only:
    the musical narrative gets written in chat with sombra-brain loaded.
    Pass intended_findings only when Ricardo explicitly said a named finding is
    intentional; this records his decision for this material and Set, per finding
    INSTANCE (uncommitted_ending, flat_energy_run, loop_with_ambitions,
    b_section_missing). Name an instance as {"detector": d, "bars": [a, b]} or
    "d@a-b", every instance as "d:all", or a bare "d" only when it fired once — a
    bare name that fired more than once is refused with the instances to pick
    from. Re-marking an already intended instance is a no-op; the result's
    intent_recorded lists what was targeted (status recorded / already_intended).
    An intended loop_with_ambitions instance silences that span plus
    b_section_missing; b_section_missing silences only itself. A bare string is
    taken as one entry. Refused on an unsaved open Set ('save the Set first'),
    when Live API access is off (Set identity unavailable), or when max_tracks
    truncated the live read (intent is keyed to the whole Set's material).
    project = catalog name, id, or stem ('ballad', 'demosong10'), or 'live' for the
    Set open in Ableton (arrangement clips of every MIDI track via Producer Pal;
    take lanes count as comping debris; only notes inside each loop brace count;
    audio tracks are named, not heard — a silent_gap may be audio). A MIDI clip
    whose notes were not read (unread; Live API off, or its read failed) still
    occupies its bars: note-based findings over them are withheld into
    ``suppressed`` (with the reason) instead of reported, and an ending over an
    unread clip ships no seed notes."""
    import finisher as _finisher

    if str(project).strip().lower() == "live":
        from mcp_server import live_write as _live_write
        live = _run_async(_live_write.read_arrangement_tracks(max_tracks))
        project_dict = _finisher.project_from_live_tracks(live)
        return _finisher.whats_missing(
            project_dict=project_dict,
            intended_findings=intended_findings)
    return _finisher.whats_missing(project=project,
                                   intended_findings=intended_findings)


@mcp.tool()
def probe_envelope_surface(confirm: bool = False) -> dict:
    """Establish the envelope-writing surface: Producer Pal's ppal-live-api escape
    hatch (needs 'Live API access' enabled in the device Setup tab) vs the SIDEKICK
    BRIDGE v8 device (config/claude_mcp.md). confirm=False returns the probe plan;
    confirm=True runs the read-only steps P1-P3 and returns a verdict."""
    import envelope_bridge as _eb

    return _eb.probe(confirm=confirm)


@mcp.tool()
def phrase_envelopes(slot: str, bars: int = 2, seed: int = 0,
                     character: str | None = None, confirm: bool = False) -> dict:
    """Land Ricardo's dynamics grammar on a SIDEKICK JAM clip: volume swell-in +
    send throw at the phrase tail (+ filter arc when an AutoFilter sits on the track),
    every curve picked from HIS real automation gestures (store/phrase_curves.json,
    provenance included). character (swell/fade/arc/dip/wiggle) narrows to a single
    volume gesture. confirm=False = dry-run plan; confirm=True executes via the
    bridge. Undo: clear_envelopes op returned with every plan."""
    import envelope_bridge as _eb

    specs = [{"family": "volume", "character": character}] if character else None
    return _eb.write_clip_envelopes(slot, specs, bars=bars, seed=seed,
                                    dry_run=not confirm)


@mcp.tool()
def assign_groove(slot: str, role: str = "lead", confirm: bool = False) -> dict:
    """Assign the 'RICARDO FEEL <ROLE>' groove from Live's pool to a SIDEKICK JAM
    clip. The pool is populated once by hand: tools/build_groove_templates.py →
    drag a template in → Extract Groove(s). No match → returns the pool listing and
    that instruction instead of guessing."""
    import envelope_bridge as _eb

    return _eb.assign_clip_groove(slot, role=role, dry_run=not confirm)


@mcp.tool()
def read_clip(track_index: int, scene_index: int = 0,
              arrangement: bool = False) -> dict:
    """Read a Producer Pal clip by explicit track/scene slot, or arrangement clip index."""
    from mcp_server import live_write as _live_write

    return _run_async(_live_write.read_clip(
        track_index=track_index,
        slot=scene_index,
        arrangement=arrangement,
    ))


@mcp.tool()
def read_current_clip() -> dict:
    """Read the current Producer Pal selection, falling back to selected track/scene slot."""
    from mcp_server import live_write as _live_write

    return _run_async(_live_write.read_current_clip())


@mcp.tool()
def create_idea_pack_from_clip(
    seed_notes: list[dict],
    n: int = 3,
    key: str | None = None,
    tempo: float = 120.0,
    cont_bars: int = 4,
    seed: int = 0,
    role: str = "lead",
    source_context: dict | None = None,
    contrast_axis: str | None = None,
) -> dict:
    """Build an IdeaPack from notes with a derived rating contract. No Ableton write happens here."""
    return _core.create_idea_pack_from_clip(
        seed_notes=seed_notes,
        n=n,
        key=key,
        tempo=tempo,
        cont_bars=cont_bars,
        seed=seed,
        role=role,
        source_context=source_context,
        contrast_axis=contrast_axis,
    )


@mcp.tool()
def create_idea_pack_from_live_clip(
    track_index: int | None = None,
    scene_index: int = 0,
    use_current_selection: bool = True,
    n: int = 3,
    key: str | None = None,
    tempo: float = 120.0,
    cont_bars: int = 4,
    seed: int = 0,
    role: str = "lead",
    contrast_axis: str | None = None,
) -> dict:
    """Read a live clip, then build an IdeaPack. No Ableton write happens here."""
    from mcp_server import live_write as _live_write

    if track_index is None and use_current_selection:
        live_clip = _run_async(_live_write.read_current_clip())
    elif track_index is not None:
        live_clip = _run_async(_live_write.read_clip(
            track_index=track_index,
            slot=scene_index,
            arrangement=False,
        ))
    else:
        raise ValueError("provide track_index or set use_current_selection=True")

    _live_write.notes_or_unread_error(live_clip, f"live clip read returned no notes: {live_clip}")

    pack = _core.create_idea_pack_from_clip(
        seed_notes=live_clip["notes"],
        n=n,
        key=key,
        tempo=tempo,
        cont_bars=cont_bars,
        seed=seed,
        role=role,
        contrast_axis=contrast_axis,
        source_context={
            "type": "producer_pal",
            "read": {k: v for k, v in live_clip.items() if k != "notes"},
        },
    )
    pack["live_read"] = live_clip
    return pack


@mcp.tool()
def prepare_idea_pack_audition_lanes(
    idea_pack: dict,
    confirm: bool = False,
    start_track: int | None = None,
    scene_index: int = 0,
    tempo: float | None = None,
) -> dict:
    """Prepare Producer Pal audition-lane calls for an IdeaPack; confirm=True required."""
    return _core.prepare_idea_pack_audition_lanes(
        idea_pack=idea_pack,
        confirm=confirm,
        start_track=start_track,
        scene_index=scene_index,
        tempo=tempo,
    )


@mcp.tool(annotations=_READ_ONLY)
def score_phrase(notes: Annotated[list[dict], Field(description=_NOTES)],
                 tempo: Annotated[float | None, Field(description='Tempo in BPM of the phrase.')] = None,
                 role: Annotated[str | None, Field(description="Score under this role's grammar, e.g. 'lead'. Omit to detect it.")] = None,
                 key: Annotated[str | None, Field(description=_KEY)] = None) -> dict:
    """Score a candidate phrase against Ricardo's style (0–1 + verdict). Pass role
    (e.g. 'lead') and/or key (e.g. 'D minor') to score under that role's grammar and a
    known key instead of self-detecting them."""
    return _core.score_phrase(notes=notes, tempo=tempo, role=role, key=key)


@mcp.tool()
def critique(notes: list[dict], tempo: float | None = None,
             role: str | None = None, key: str | None = None) -> dict:
    """Rich style critique. Pass role/key to use the same grammar as score_phrase."""
    return _core.critique(notes=notes, tempo=tempo, role=role, key=key)


@mcp.tool()
def continue_clip(
    notes: list[dict],
    bars: int = 8,
    tempo: float | None = None,
) -> dict:
    """Analyze a seed clip and return a continuation brief (Claude composes the next bars)."""
    return _core.continue_clip(notes=notes, bars=bars, tempo=tempo)


@mcp.tool()
def finish_my_idea(
    seed_notes: list[dict],
    target_minutes: float | None = None,
    tempo: float | None = None,
) -> dict:
    """Finish-my-idea bundle: analyze seed clip, critique, continuations brief + structure."""
    return _core.finish_my_idea(
        seed_notes=seed_notes,
        target_minutes=target_minutes,
        tempo=tempo,
    )


@mcp.tool()
def rate(
    rating: int | str,
    label: str | None = None,
    notes: list[dict] | None = None,
    phrase_id: str | None = None,
    comment: str | None = None,
    idempotency_key: str | None = None,
    role: str | None = None,
    key: str | None = None,
) -> dict:
    """Rate a phrase up/down/neutral for feedback-driven retrieval (-1/0/1 or up/down).

    Mark a 'gold' anchor (a real phrase that genuinely sounds like him) with
    rate(rating=1, label='gold', notes=..., phrase_id=...). Un-gold with a later
    rate(rating=-1, label='gold', phrase_id=...) — or label='rejected'/'ungold' — on the same
    phrase. Ordinary ratings with other labels never touch gold membership.
    Pass role/key from the serving context and a stable idempotency_key so a
    retried call can never record the same judgment twice."""
    return _core.rate(
        rating=rating,
        label=label,
        notes=notes,
        phrase_id=phrase_id,
        comment=comment,
        idempotency_key=idempotency_key,
        role=role,
        key=key,
    )


@mcp.tool()
def get_ratings() -> dict:
    """Return counts and recent entries from the ratings feedback loop."""
    return _core.get_ratings()


@mcp.tool()
def get_gold_phrases() -> list[dict]:
    """The gold anchor set — real phrases Ricardo hand-picked as genuinely *him* (net of un-golds).
    These are the curated reference for composing-from-real-material; feed a gold phrase's notes
    into develop_idea/grow_my_loop, or to send_to_ableton/export_midi."""
    return _core.get_gold_phrases()


@mcp.tool()
def compose_brief(request: str, seed: int = 0) -> dict:
    """Bundle style summary + example phrases + optional structure for composing."""
    return _core.compose_brief(request=request, seed=seed)


@mcp.tool()
def compose_from_profile(
    bars: int = 8,
    key: str | None = None,
    density: str = "mid",
    seed: int = 0,
    progression: str | None = None,
    use_taste: bool = True,
    humanize: bool = False,
) -> dict:
    """Generate a scale-locked sketch from Ricardo's profile (pad/bass/lead/drums).

    Deterministic given seed. Uses harmony.json progressions, melodic groove/register
    from style_profile, and real or synthesized drums. density: minimal | mid | full.
    Pass progression (e.g. 'i->iv->VI->v') to realize a chosen chord sequence; set
    use_taste=False to get the pure style-best lead (no learned-taste rerank).
    humanize=True maps every part through your learned velocity/timing DNA.
    """
    return _core.compose_from_profile(
        bars=bars, key=key, density=density, seed=seed,
        progression=progression, use_taste=use_taste, humanize=humanize,
    )


@mcp.tool()
def humanize(notes: list[dict], role: str = "lead", strength: float | None = None,
             timing: bool = True, tempo: float = 120.0, seed: int = 0,
             export_ab_basename: str | None = None) -> dict:
    """Apply Ricardo's velocity/timing DNA (per-role contour + micro-timing learned from
    his corpus) to notes. strength: None = gentle 0.35 (real material), 1.0 = full (flat
    generated parts), 0 = identity. Deterministic given seed. export_ab_basename='x'
    writes store/exports/x_raw.mid + x_humanized.mid for a blind ear A/B."""
    raw = [dict(n) for n in notes]
    out = _core.humanize_notes(notes, role=role, strength=strength, timing=timing,
                               tempo=tempo, seed=seed)
    vb, _tb = _core._humanize_stats(role)
    res: dict = {
        "notes": out,
        "raw_notes": raw,
        "applied": vb is not None and (strength is None or strength > 0),
        "params": {"role": role, "timing": timing, "tempo": tempo, "seed": seed,
                   "strength": _core._REAL_STRENGTH if strength is None else strength},
        "assumptions": [],
    }
    if vb is None:
        res["assumptions"].append(
            f"no velocity DNA for role '{role}' in style_profile.json — returned unchanged "
            "(rerun analyze/style_profile.py after the extraction batch)")
    if export_ab_basename:
        res["exports"] = {
            "raw": _core.export_midi(raw, f"{export_ab_basename}_raw.mid", tempo=tempo)["path"],
            "humanized": _core.export_midi(out, f"{export_ab_basename}_humanized.mid",
                                           tempo=tempo)["path"],
        }
    return res


@mcp.tool(annotations=_MAY_REPLACE)
def export_midi(notes: Annotated[list[dict], Field(description=_NOTES)],
                path: Annotated[str, Field(description='File name ending in .mid or .midi. Relative paths land under store/exports; absolute paths only inside store/exports or a SIDEKICK_EXPORT_DIRS directory.')],
                tempo: Annotated[float, Field(description='Tempo in BPM written into the file.')] = 120.0) -> dict:
    """Export notes to a .mid file under store/exports (relative paths land there).

    Absolute paths are accepted only inside store/exports or a directory the
    operator listed in SIDEKICK_EXPORT_DIRS. The name must end in .mid/.midi,
    and an existing file is replaced only if it is already a MIDI file."""
    return _core.export_midi(notes=notes, path=path, tempo=tempo)


@mcp.tool()
def send_to_ableton(
    notes: list[dict],
    track: int | str | None = None,
    clip_name: str | None = None,
    tempo: float = 120.0,
    bar_length: int | None = None,
    arrangement_start: str | None = None,
    session_scene: int = 0,
    addressing: str = "2.2",
) -> dict:
    """Build Producer Pal MCP payload to create a MIDI clip in Ableton (handoff only).

    Returns tool_calls for ppal-connect / ppal-update-live-set / ppal-create-clip.
    Does not connect to Ableton — Claude must call Producer Pal MCP separately.
    addressing: the version ppal-connect reports ("2.2.0-rc1", "2.3.0", "2.4.0")
    or its generation ("2.2" / "2.3" / "2.4"); on 2.3 and 2.4 the arrangement
    position lives in the path, 't2[17|1]'.
    """
    return _core.send_to_ableton(
        notes=notes,
        track=track,
        clip_name=clip_name,
        tempo=tempo,
        bar_length=bar_length,
        arrangement_start=arrangement_start,
        session_scene=session_scene,
        addressing=addressing,
    )


@mcp.tool(annotations=_MAY_REPLACE)
def write_midi_clip(
    track_name: Annotated[str, Field(description="Exact name of an existing MIDI track called 'SIDEKICK' or 'SIDEKICK ...' (not SIDEKICK JAM).")],
    scene_index: Annotated[int, Field(description='0-based Session scene (slot) index on that track.')],
    notes: Annotated[list[dict], Field(description=_NOTES)],
    name: Annotated[str, Field(description='Clip name, 1-120 characters.')] = "SIDEKICK MIDI",
    loop_start: Annotated[str, Field(description="Loop start as 1-based bar|beat, e.g. '1|1'.")] = "1|1",
    loop_length: Annotated[str, Field(description="Loop length as a Producer Pal duration, e.g. '1bar', '4bar' or 'n/4'.")] = "1bar",
    replace: Annotated[bool, Field(description='Allow replacing an occupied MIDI clip in that slot (same SIDEKICK track only).')] = False,
    confirm: Annotated[bool, Field(description='False previews only; True writes and reads the clip back to verify it.')] = False,
) -> dict:
    """Preview or execute a verified MIDI clip write on an exact SIDEKICK track.

    The named track must already exist, be MIDI, and have an empty target slot;
    ``replace=True`` permits replacing an occupied MIDI clip on that same
    Sidekick-owned track. SIDEKICK JAM is refused before any Live call: JAM is
    the stamped answer surface, and an answer there must land through a stamped
    answer write so the ledger can score it. ``confirm=False`` performs no
    mutation.
    """
    from mcp_server import clip_mutation as _clip_mutation

    if str(track_name).strip().upper() == "SIDEKICK JAM":
        # JAM answers must land on the stamped path — a bare clip write here
        # would be invisible to session scoring no matter how well it verifies.
        raise ValueError(
            "SIDEKICK JAM is the stamped answer surface — write it with "
            "answer_my_loop(write=True) or write_composed_to_jam so the "
            "ledger can score it; write_midi_clip serves the other "
            "Sidekick-owned tracks")
    result = _run_async(_clip_mutation.write_midi_clip(
        track_name, scene_index, notes, name=name, loop_start=loop_start,
        loop_length=loop_length, replace=replace, confirm=confirm))
    return result


@mcp.tool(annotations=_WRITES)
def write_composed_to_jam(notes: Annotated[list[dict], Field(description='The composed notes. ' + _NOTES)],
                          contract: Annotated[dict, Field(description='The contract answer_my_loop(gear=5 or 6) returned.')],
                          name: Annotated[str, Field(description='Clip name, 1-120 characters; made unique within the open session.')],
                          role: Annotated[str, Field(description="Role the critics validate the notes under, e.g. 'lead'.")] = "lead",
                          source: Annotated[str, Field(description="Ledger stamp: 'catalogue' or 'ref:<ref_id>'.")] = "catalogue",
                          ears: Annotated[str | None, Field(description="Ears context stamped in the ledger: 'live', 'blind', 'reference' or 'off'. Omit if unknown.")] = None,
                          confirm: Annotated[bool, Field(description='False validates and previews only; True writes to SIDEKICK JAM if validation passes.')] = False) -> dict:
    """Land LLM-composed material (gear 5 COMPOSE / gear 6 COVER) on SIDEKICK JAM.

    The critics gate it first (gears.validate_composed under ``role``), and a
    passing write is stamped in the judgment ledger like every other JAM answer,
    so close_session() can score it. ``contract`` is what answer_my_loop(gear=5|6)
    returned. confirm=False validates and previews only; a failing contract never
    writes. Clip names are made unique within the open session for readability;
    the survival sweep uses verified identity and conservative reconciliation."""
    from mcp_server import live_write as _live_write
    import gears as _gears
    import jam_lifecycle as _jl
    import ledger as _ledger

    if not isinstance(contract, dict) or contract.get("gear") not in (5, 6):
        raise ValueError("contract must come from answer_my_loop(gear=5) or gear=6")
    if source != "catalogue" and not str(source).startswith("ref:"):
        raise ValueError("source must be 'catalogue' or 'ref:<ref_id>'")
    if ears not in (None, "live", "blind", "reference", "off"):
        raise ValueError("ears must be one of live/blind/reference/off, or omitted")
    name = (name or "").strip()
    if not name or len(name) > 120:
        raise ValueError("name must be 1-120 characters")

    val = _gears.validate_composed(notes, contract, role=role)
    result: dict = {"validation": val, "passes": bool(val.get("passes")),
                    "gear": contract["gear"], "role": role, "clip_name": name,
                    "wrote": False}
    if not result["passes"]:
        result["note"] = "nothing written — fix the failing checks or compose again"
        return result

    base = name
    taken = _ledger.clip_name_counts(_ledger.open_session_entries(_ledger.load()))
    suffix = 2
    while name in taken:
        name = f"{base} #{suffix}"
        suffix += 1
    if name != base:
        result["renamed_from"] = base
    result["clip_name"] = name
    tempo = float(contract.get("tempo") or 120.0)
    if not confirm:
        result["would_write"] = {"track": "SIDEKICK JAM", "clip_name": name,
                                 "n_notes": len(notes), "tempo": tempo}
        return result

    _jl.write_answer_to_jam(
        result,
        notes=notes,
        clip_name=name,
        tempo=tempo,
        write_fn=lambda notes, *, clip_name, tempo: _run_async(
            _live_write.write_answer_to_jam_track(
                notes, clip_name=clip_name, tempo=tempo)),
        source=source,
        role=role,
        gear=contract["gear"],
        ears=ears,
        source_phrase_id=None,
        ledger_tempo=tempo,
        n_notes=len(notes),
        key=contract.get("key"),
    )
    result["wrote"] = "live_write" in result
    return result


@mcp.tool()
def surgery_clip(path: str, bar: int, surgery: dict,
                 confirm: bool = False) -> dict:
    """Replace exactly one bar of a Sidekick-owned MIDI session clip.

    ``surgery`` is ``{"notes": [...]}``; note starts are relative to the chosen
    one-based bar and use the clip's musical-beat unit. The tool snapshots the
    complete clip, clears only ``bar|*``, rereads it, and proves all other bars
    and loop bounds are unchanged. ``confirm=False`` performs no mutation.
    """
    from mcp_server import clip_mutation as _clip_mutation

    result = _run_async(_clip_mutation.surgery_clip(
        path, bar, surgery, confirm=confirm))
    if confirm and result.get("status") == "verified":
        import ledger as _ledger
        import jam_lifecycle as _jl
        prior = _ledger.entry_for_clip(result.get("prior_clip_ref"),
                                       result.get("set_identity"))
        if prior is not None:
            target = dict(result)
            entry = _jl.stamp_verified_write(
                target, clip_name=str(result.get("clip_ref", {}).get("clip_name")
                                      or prior.get("clip_name") or "edited"),
                slot=path, source=prior.get("source") or "catalogue",
                role=prior.get("role"), gear=prior.get("gear"),
                ears=prior.get("ears"), source_phrase_id=prior.get("source_phrase_id"),
                ledger_tempo=prior.get("tempo"), key=prior.get("key"),
                notes=result.get("answer_notes") or [], live_receipt=result,
                revision_of=prior["id"])
            if entry is not None:
                result["ledger_entry_id"] = entry["id"]
            result.update({key: value for key, value in target.items()
                           if key in ("ledger", "ledger_error", "accounting_failure",
                                      "scoreable", "scoreable_reason")})
        elif not result.get("prior_clip_ref") or not result.get("set_identity"):
            # The edit is real but reconciliation can never run: no clip or
            # Set identity to match at close, so say so instead of silence.
            result["scoreable"] = False
            result["scoreable_reason"] = (
                "this edit cannot be scored at session close: Live Set identity "
                "unavailable — use Producer Pal 2.2 with Direct Live API enabled "
                "in this Set")
    return result


def _audio_ref_mod():
    tools_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import audio_ref as _audio_ref  # noqa: WPS433 — tools/ is intentionally lazy

    return _audio_ref


def _record_ref_tally_for_source(source: str | None, outcome: str,
                                 entry_id: str | None = None) -> dict | None:
    """Bump the overlay tally of the ref a judged ledger row credits.

    Returns the tally receipt, or None when the row is not ref-credited.
    Never raises: a missing ref index must not break judging or the sweep —
    the miss is reported in the receipt instead. ``entry_id`` dedups through
    the card's tallied_entries, so one ledger row counts exactly once no
    matter how many paths resolve it.
    """
    if not source or not str(source).startswith("ref:"):
        return None
    ref_id = str(source)[len("ref:"):]
    if not ref_id:
        return None
    try:
        _ar = _audio_ref_mod()
        card = _ar.record_ref_judgment(ref_id, outcome, entry_id=entry_id)
        return {"ref_id": ref_id, "outcome": outcome, "ok": True,
                "tally": _ar.ref_tally_of(card)}
    except Exception as exc:
        return {"ref_id": ref_id, "outcome": outcome, "ok": False,
                "error": f"{type(exc).__name__}: {exc}"}


def _tally_ref_attested_rows(report: dict, entries: dict) -> list[dict]:
    """Tally attest-settled ref rows from a close/attest report, once each.

    Attested rows never land a terminal_state on the ledger entry — the
    report's attested stratum IS their settlement — so they are counted here
    while the sweep block counts terminal_state transitions. Both dedup by
    ledger entry id.
    """
    tallies: list[dict] = []
    attested = report.get("attested") if isinstance(report, dict) else None
    rows = (attested or {}).get("entries", []) or []
    for _att in rows:
        if not isinstance(_att, dict):
            continue
        _state = _att.get("state")
        if _state == "keep":
            _outcome: str | None = "survived"
        elif _state == "drop":
            _outcome = "dropped"
        else:
            continue
        _eid = _att.get("entry_id")
        _entry = entries.get(_eid, {}) if isinstance(entries, dict) else {}
        _tally = _record_ref_tally_for_source(
            _entry.get("source"), _outcome, entry_id=_eid)
        if _tally is not None:
            tallies.append(_tally)
    return tallies


def _mix_check_mod():
    """Load offline mix orchestration only when a Mix Adviser tool is called."""
    tools_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    from tools import mix_check as _mix_check  # noqa: WPS433 — intentionally lazy

    return _mix_check


@mcp.tool()
async def analyze_mix_pass(
    master_path: str,
    group_paths: dict[str, str],
    target: str = "",
    listener: bool = False,
) -> dict:
    """Analyze a manual offline export; never write to Live or Producer Pal.

    master_path must be a local WAV/AIFF/FLAC master and group_paths must contain
    2–4 aligned local group exports. listener=False is the default; listener=True
    explicitly invokes sandboxed agy and may send audio according to that account
    and tool. Measurements and any witness are evidence/hypotheses, not taste.
    """
    _mix_check = _mix_check_mod()
    return await asyncio.to_thread(
        _mix_check.analyze_mix_pass,
        master_path, group_paths, target=target, listener=listener,
    )


@mcp.tool()
def propose_mix_moves(trial_id: str, max_moves: int = 3) -> dict:
    """Propose at most three manual gain moves from offline evidence.

    This is read-only with respect to the Main/master bus and makes no Live,
    Producer Pal, or mixer writes. Ricardo applies any proposed gain_db move
    manually; pan stays parked until DSP emits defensible pan evidence. Proposals
    cite evidence and remain hypotheses, not taste judgments.
    """
    return _mix_check_mod().propose_mix_moves(trial_id, max_moves=max_moves)


@mcp.tool()
def prepare_mix_ab(trial_id: str, after_master_path: str) -> dict:
    """Prepare a local loudness-matched A/B after Ricardo manually re-exports.

    The after master must be a local WAV/AIFF/FLAC with the same technical format
    and aligned duration. Public pre-judgment output withholds mapping, source
    paths, and checksums to reduce accidental deblinding; the operating agent
    must not reconstruct them. This tool never writes Live or Producer Pal and
    exposes no scalar mix score.
    """
    return _mix_check_mod().prepare_mix_ab(trial_id, after_master_path)


@mcp.tool()
def judge_mix_pass(
    trial_id: str,
    choice: str,
    witness_rating: str = "not_shown",
    kept_in_set: bool | None = None,
) -> dict:
    """Record a human offline mix judgment in the separate mix trial ledger.

    choice must be randomized label A, B, or same; the before/after semantic is
    returned only after this judgment. This records evidence about a manual
    trial, never applies mixer changes, and never writes the composing-session
    KEEP ledger.
    """
    return _mix_check_mod().judge_mix_pass(
        trial_id,
        choice,
        witness_rating=witness_rating,
        kept_in_set=kept_in_set,
    )


@mcp.tool()
def mix_trial_report() -> dict:
    """Read the separate mix trial ledger without changing either ledger.

    The report is read-only, does not touch store/session_ledger.json or the
    composing-session KEEP denominator, and intentionally provides no scalar mix
    score: measurements and judgments remain evidence for Ricardo's taste.
    """
    return _mix_check_mod().mix_report()


@mcp.tool()
async def analyze_audio_ref(
    source: str | None = None,
    modes: str | None = None,
    prompt_extra: str | None = None,
    start: str | None = None,
    end: str | None = None,
    midi_tempo: float = 120.0,
    model: str | None = None,
    focus: str = "full",
    from_dir: str | None = None,
    tags: str | None = None,
    stem_files: dict[str, str] | None = None,
    timeout_s: int = 180,
) -> dict:
    """Analyze a local audio file OR YouTube URL into reference artifacts.

    YOU choose focus before calling (full|groove|harmony|melody). Optional
    prompt_extra is your freeform note and overrides emphasis when set.

    Writes store/audio_refs/<stem>_<timestamp>/ (or reuses from_dir) with
    source.wav, features.json, describe.txt (agy/Gemini on local wav), ref.mid.
    Upserts store/audio_ref_index.json. Does NOT write to Ableton.

    modes: comma list describe,midi,features (default all).
    start/end: clip window (seconds or mm:ss).
    from_dir: existing audio_refs folder to re-describe without re-download.
    tags: comma tags for the index card.
    stem_files: explicit local role-to-path mapping. Stem source paths are never
    retained in the artifact or public response.
    timeout_s: agy timeout in seconds (default 180).
    """
    _audio_ref = _audio_ref_mod()
    mode_list = None
    if modes:
        mode_list = [m.strip() for m in modes.split(",") if m.strip()]
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()] or None
    return await asyncio.to_thread(
        _audio_ref.analyze_audio_ref,
        source,
        modes=mode_list,
        prompt_extra=prompt_extra,
        start=start,
        end=end,
        midi_tempo=midi_tempo,
        model=model,
        focus=focus,
        from_dir=from_dir,
        tags=tag_list,
        stem_files=stem_files,
        timeout_s=timeout_s,
    )


@mcp.tool()
def find_audio_refs(
    query: str | None = None,
    focus: str | None = None,
    tag: str | None = None,
    project: str | None = None,
    verdict: str | None = None,
    ownership: str | None = None,
    k: int = 5,
    include_moments: bool = False,
    include_suspect: bool = False,
    detail: bool = False,
) -> list:
    """Search the audio-ref index for past YouTube/local listening briefs.

    Use while composing (with hear_the_room separately for Live). Filter by
    project= a name Ricardo chose for the creative project (not the .als set).
    Optional verdict=keep|drop|unset.
    The judgment tally only ranks results down — even twice-dropped refs
    keep matching. Moment slices are excluded by
    default (their briefs paraphrase the parent track); include_moments=True
    brings them back, and a moment ref_id still steers when passed directly.
    Owner-marked suspect refs stay out until cleared; include_suspect=True
    lists them for triage.
    Rows are compact by default (ref_id, title, ownership, identity,
    ricardo_note, up to 3 dos, tempo/density/register, section labels,
    uncertain); detail=True returns the full cards (with imitation rules,
    structure, out_dir, describe_path). ownership=his|other narrows to his
    records or other artists' — his own record is quotable, theirs is a
    target only.
    """
    return _audio_ref_mod().find_audio_refs(
        query=query,
        focus=focus,
        tag=tag,
        project=project,
        verdict=verdict,
        ownership=ownership,
        k=k,
        include_moments=include_moments,
        include_suspect=include_suspect,
        detail=detail,
    )


@mcp.tool()
def tag_audio_ref(
    ref_id: str,
    tags: str | None = None,
    project: str | None = None,
    note: str | None = None,
    verdict: str | None = None,
    ownership: str | None = None,
) -> dict:
    """Tag / link a project / attach Ricardo overlay on an audio ref.

    project= creative project name (not Ableton set). note= 1–3 lines of his taste
    ("steal the half-time snare"). verdict= keep|drop|unset after Live apply.
    ownership= operator authorship, one of his|other|unknown; never model-guessed.
    """
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    return _audio_ref_mod().tag_audio_ref(
        ref_id,
        tags=tag_list or None,
        project=project,
        note=note,
        verdict=verdict,
        ownership=ownership,
    )


@mcp.tool()
async def clip_audio_ref_moments(
    ref_id: str,
    window_s: float = 45.0,
    pad_s: float = 5.0,
    modes: str = "midi,features",
) -> dict:
    """Cut jam-sized mini-refs from a parent's # Moments timestamps (no Gemini).

    Copies parent imitation rules into a short describe; runs Basic Pitch on the
    clip only. Children are tagged moment + parent:<id> and inherit linked_projects.
    """
    mode_list = [m.strip() for m in (modes or "").split(",") if m.strip()]
    _audio_ref = _audio_ref_mod()
    return await asyncio.to_thread(
        _audio_ref.clip_moments,
        ref_id,
        window_s=window_s,
        pad_s=pad_s,
        modes=mode_list or ["midi", "features"],
    )


@mcp.tool()
def analyze_audio_refs_batch(
    manifest_json: str,
    resume: bool = True,
    timeout_s: int = 180,
) -> dict:
    """Batch-analyze many YouTube URLs / local tracks sequentially.

    manifest_json: JSON object with batch_id, defaults, items[] OR a JSON array
    of {source, start?, end?, focus?, prompt_extra?, tags?} OR a newline URL list
    as a JSON string with embedded newlines (prefer object form).

    Continues on failure; writes store/audio_refs/batches/<batch_id>.json.
    Does not write to Ableton — use find_audio_refs after.
    timeout_s: function-level timeout default; manifest item/default values take precedence.
    """
    _ar = _audio_ref_mod()
    text = manifest_json.strip()
    if text.startswith("{") or text.startswith("["):
        data = json.loads(text)
        if isinstance(data, list):
            manifest = {
                "batch_id": _ar.new_batch_id("mcp-batch"),
                "defaults": {},
                "items": data,
            }
        else:
            manifest = dict(data)
            if not manifest.get("batch_id"):
                manifest["batch_id"] = _ar.new_batch_id("mcp-batch")
    else:
        items = []
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                items.append({"source": line})
        manifest = {
            "batch_id": _ar.new_batch_id("mcp-batch"),
            "defaults": {},
            "items": items,
        }
    return _ar.run_batch(manifest, resume=resume, timeout_s=timeout_s)


@mcp.tool()
def reindex_audio_refs() -> dict:
    """Back up overlays, then rebuild the audio-ref index."""
    ar = _audio_ref_mod()
    backup = ar.backup_overlays()
    result = ar.reindex_audio_refs()
    result["overlay_backup"] = backup
    return result


@mcp.tool()
def restore_audio_ref_ownership() -> dict:
    """Restore exact operator ownership IDs into audio-ref index and metadata."""
    return _audio_ref_mod().restore_ownership()


@mcp.tool()
def apply_audio_ref(
    ref_id: str,
    role: str = "auto",
    bars: float = 8.0,
    tempo: float | None = None,
    write: bool = False,
) -> dict:
    """Compose-apply: load a filed audio ref into a jam-ready handoff.

    Pulls describe.txt imitation rules + optional Basic Pitch notes from ref.mid
    (pitch-filtered by role=auto|drums|bass|pad|lead). Returns KEEP/DROP guidance.

    write=True lands notes on SIDEKICK JAM (only surface allowed) — but only
    for HIS OWN records (ownership=his), where quoting is legitimate. Anyone
    else's transcription stays a preview: answer it from YOUR OWN phrases
    steered by the ref instead of pasting their notes.
    If MIDI is empty or weak (common for drums), returns the brief for you
    to compose — does not invent notes.
    """
    _ar = _audio_ref_mod()
    pkg = _ar.apply_audio_ref(
        ref_id, role=role, bars=bars, tempo=tempo
    )
    if not write:
        return pkg
    _owner = _ar.normalize_ownership((pkg.get("card") or {}).get("ownership"))
    if _owner != "his":
        # Another artist's transcription must never land on JAM as notes —
        # the brief (rules, pocket) is the usable part, not their pitches.
        pkg["live_write"] = None
        pkg["write_skipped"] = True
        pkg["write_reason"] = (
            "that ref is another artist's record (ownership=%s), not yours — "
            "its transcription stays a preview. Compose from its rules with "
            "answer_my_loop(ref_id=%r, write=True), which answers from YOUR "
            "phrases steered by the ref" % (_owner, ref_id))
        return pkg
    notes = pkg.get("notes") or []
    if not notes:
        pkg["live_write"] = None
        pkg["write_skipped"] = True
        pkg["write_reason"] = (
            "no notes to write — compose from imitation_do/dont, then send_to_ableton "
            "or answer_my_loop(write=True)"
        )
        return pkg
    from mcp_server import live_write as _live_write
    import jam_lifecycle as _jl

    _clip_name = str(pkg.get("clip_name") or f"ref {ref_id}")[:40]
    # stamp the role the WRITE actually carries — tools.apply_audio_ref only
    # echoes the argument back, so "auto" resolves nothing there: classify
    # the notes actually written with the versioned role classifier instead;
    # drums maps to the catalogue's "drum"; an explicit role is kept as given
    _resolved_role = str(pkg.get("role") or "").strip().lower()
    if _resolved_role in ("", "auto"):
        from roles import clip_role as _clip_role  # analyze/ is on sys.path via core
        _resolved_role = _clip_role(notes)
    _ledger_role = _core._ROLE_ALIASES.get(_resolved_role, _resolved_role)
    _jl.write_answer_to_jam(
        pkg,
        notes=notes,
        clip_name=_clip_name,
        tempo=float(pkg.get("tempo") or tempo or 120.0),
        write_fn=lambda notes, *, clip_name, tempo: _run_async(
            _live_write.write_answer_to_jam_track(
                notes, clip_name=clip_name, tempo=tempo)),
        source=f"ref:{ref_id}",
        role=_ledger_role,
        gear=None,
    )
    return pkg


@mcp.tool()
def beat_from_ref(ref_id: str, bars: int = 4, tempo: float | None = None,
                  seed: int = 0, humanize: bool = True,
                  write: bool = False) -> dict:
    """"Give me a beat like <that track>" — built from YOUR OWN drum clips.

    Prefers a measured sixteenth-note onset grid from an explicit drums stem,
    otherwise parses the written drum description ("kick on 1, snare on 3,
    half-time"). It finds your catalogue phrases that land that way. Nothing is
    generated and Basic Pitch is not involved.

    write=True lands it on SIDEKICK JAM. Judge it with judge_answer like any answer.

    Any record may steer (a reference never refuses an answer): the ref's
    grid/tempo/density only pick WHICH of your drum phrases play — the beat
    itself is always your own catalogue phrases, never the ref's own
    grid/transcription notes.
    """
    _ar = _audio_ref_mod()
    card = _ar.get_ref_card(ref_id)
    steer = _ar.ref_steering(card)
    grid_notes, understood = _ar.measured_drum_grid_to_notes(
        steer.get("measured_drum_grid"))
    grid_source = "measured_drums_stem" if grid_notes else "prose_drum_grid"
    if not grid_notes:
        # The structured card grid is first-hand data; the rendered prose is a
        # lossy paraphrase ("kick on 0, 4, 8, 12" reads as beats 0/4/8/12 and
        # parses to nothing). Prefer structure, fall back to prose. The card
        # here is index-shaped (the filed card rides under "card"), so unwrap
        # before reading the grid.
        _inner = card.get("card") or card
        _card_notes, _card_ok = _ar.card_grid_to_notes(_inner)
        if _card_notes:
            grid_notes = _card_notes
            grid_source = "card_grid"
            _insts = sorted({str(lane.get("instrument") or "")
                             for lane in (_inner.get("drums") or {}).get("grid") or []})
            understood = {"source": "card_grid", "phrases": ["card grid"],
                          "instruments": [i for i in _insts if i]}
    if not grid_notes:
        grid_notes, understood = _ar.drum_grid_to_notes(steer.get("drum_grid"))
    res = _core.beat_from_grid(
        grid_notes, bars=bars, tempo=tempo or steer.get("tempo"),
        density=(card.get("structure") or {}).get("density"),
        seed=seed, humanize=humanize,
    )
    res["ref"] = {"ref_id": ref_id, "identity": steer.get("identity"),
                  "steering": "gates",
                  "drum_grid": steer.get("drum_grid"), "understood": understood,
                  "grid_source": grid_source if grid_notes else "tempo_only",
                  "measured_drum_grid": steer.get("measured_drum_grid"),
                  "ricardo_note": steer.get("ricardo_note"),
                  # same brief as the melodic route: one reference, one voice
                  "contract_version": steer.get("contract_version"),
                  "brief": steer.get("brief")}
    if not grid_notes:
        res.setdefault("assumptions", []).append(
            "that reference's drum description didn't parse into a grid — "
            "picked on tempo alone; lean on its imitation rules instead")
    # Honest stamping, same contract as answer_my_loop: one extra retrieval
    # with the same seed and no reference. Only the caller's explicit tempo
    # travels — a tempo that came from the ref is part of its influence.
    try:
        _unsteered = _core.beat_from_grid(
            [], bars=bars, tempo=tempo, density=None,
            seed=seed, humanize=humanize,
        )
        _beat_changed = (_unsteered.get("source_phrase_ids")
                         != res.get("source_phrase_ids"))
        res["ref"]["changed_pick"] = bool(_beat_changed)
        if not _beat_changed:
            res.setdefault("assumptions", []).append(
                "reference %s left the pick unchanged "
                "(same phrase with and without it)" % ref_id)
    except Exception as _exc:
        res["ref"]["changed_pick"] = True
        res.setdefault("assumptions", []).append(
            "unsteered comparison failed (%s: %s) — stamped ref:%s as before"
            % (type(_exc).__name__, _exc, ref_id))
    if write:
        from mcp_server import live_write as _live_write
        import jam_lifecycle as _jl

        _clip = f"beat {ref_id[:18]} s{seed}"
        _beat_drove_pick = bool(res["ref"].get("changed_pick"))
        _jl.write_answer_to_jam(
            res,
            notes=res["notes"],
            clip_name=_clip,
            tempo=res["tempo"],
            write_fn=lambda notes, *, clip_name, tempo: _run_async(
                _live_write.write_answer_to_jam_track(
                    notes, clip_name=clip_name, tempo=tempo)),
            source=(f"ref:{ref_id}" if _beat_drove_pick else "catalogue"),
            ref_prose=(None if _beat_drove_pick else ref_id),
            role="drum",
            gear=None,
        )
    return res


def _close_failure_reason(exc: BaseException) -> str:
    """Why a night could not be closed, in the operator's vocabulary.

    "Live/Producer Pal unreachable" is true for a dead bridge but a lie for
    a connected Set whose identity cannot be read — that night fails for a
    fixable reason (Direct Live API off, Producer Pal older than 2.2) and
    saying so is the difference between retrying and reconfiguring.
    """
    from mcp_server import live_write as _lw
    from mcp_server import pp_session as _pps
    if isinstance(exc, _lw.LiveSetIdentityError):
        return ("connected, but Live Set identity is unavailable (Direct Live "
                "API off or Producer Pal older than 2.2?)")
    if isinstance(exc, _pps.PPProtocolError):
        return "protocol/version mismatch"
    if isinstance(exc, (ConnectionError, OSError)):
        return "Producer Pal unreachable"
    return "unexpected %s" % type(exc).__name__


def _outcome_op_applied(entry_id: str | None) -> bool:
    """Whether an 'outcome:<entry_id>' op already landed in the ratings
    file. Pure read — lets a re-close retry only what never applied."""
    if not entry_id:
        return False
    import taste_bridge as _tb
    try:
        ratings = _tb._load_ratings_file().get("ratings", [])
    except Exception:
        return False
    key = "outcome:%s" % entry_id
    return any(r.get("idempotency_key") == key for r in ratings)


def _timestamp_after(first: str | None, second: str | None) -> bool:
    """Compare ISO timestamps by instant, including Z and numeric offsets."""
    if not first or not second:
        return False
    try:
        def parse(value):
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        return parse(first) > parse(second)
    except (TypeError, ValueError):
        return False


def _close_outcome_ratings(res: dict, post: dict, pre_terms: dict,
                           attestation_confirmed: bool = False
                           ) -> tuple[list[dict], bool]:
    """Outcome rating ops for a scored close. Returns (outcomes, newly_terminal).

    Only rows made terminal in this close, or marked after a failed prior
    attempt, are eligible. Attestations are handled when confirmed."""
    import ledger as _ledger
    outcomes: list[dict] = []
    newly_terminal = False
    handled: set = set()
    targets: list[dict] = []
    for item in res.get("entries", []) or []:
        eid = item.get("entry_id")
        if eid in handled:
            continue
        handled.add(eid)
        entry = post.get(eid, {})
        if entry.get("terminal_state") is None:
            continue
        if pre_terms.get(eid) is None:
            newly_terminal = True
        if pre_terms.get(eid) is None or entry.get("outcome_op_pending"):
            targets.append(entry)
    for stored in post.values():
        eid = stored.get("id")
        if eid in handled or not stored.get("outcome_op_pending"):
            continue
        if stored.get("terminal_state") is not None:
            targets.append(stored)
        elif stored.get("attestation"):
            fact = (stored.get("attestation") or {}).get("fact")
            if fact in ("present", "adopted", "absent"):
                entry = dict(stored)
                # Same verdict as confirm time: a late verdict counts as none.
                entry["immediate_verdict"] = _ledger._verdict_at_evidence(stored)
                entry["terminal_state"] = _ledger.resolve_terminal(
                    entry, fact in ("present", "adopted"))
                targets.append(entry)
        handled.add(eid)
    if attestation_confirmed:
        for arow in (res.get("attested") or {}).get("entries", []) or []:
            eid = arow.get("entry_id")
            if eid in handled:
                continue
            handled.add(eid)
            stored = post.get(eid, {})
            if stored.get("attestation"):
                entry = dict(stored)
                entry["terminal_state"] = arow.get("state")
                entry["immediate_verdict"] = _ledger._verdict_at_evidence(stored)
                targets.append(entry)
    for entry in targets:
        eid = entry.get("id")
        if _outcome_op_applied(eid):
            continue
        import taste_bridge as _tb
        try:
            ratings = _tb._load_ratings_file().get("ratings", [])
        except Exception:
            ratings = []
        settled_at = (entry.get("terminal_at") or
                      (entry.get("attestation") or {}).get("attested_at"))
        source_id = entry.get("source_phrase_id")
        if settled_at and any(
                ((source_id is not None and r.get("phrase_id") == source_id) or
                 (eid is not None and r.get("entry_id") == eid)) and
                _timestamp_after(r.get("created_at"), settled_at)
                for r in ratings):
            if entry.get("outcome_op_pending"):
                try:
                    _ledger.set_outcome_op_pending(eid, False)
                except Exception as exc:
                    outcomes.append({"entry_id": eid,
                                     "error": "could not clear outcome retry marker: %s" % exc})
            continue
        applied = _apply_outcome_rating_ops(entry)
        pending = any(op.get("error") for op in applied)
        try:
            _ledger.set_outcome_op_pending(eid, pending)
        except Exception as exc:
            outcomes.append({"entry_id": eid,
                             "error": "could not update outcome retry marker: %s" % exc})
        if applied:
            outcomes.append({"entry_id": eid, "applied": applied})
    return outcomes, newly_terminal


def _should_reset_streaks() -> bool:
    """Ears streak reset gate: only when no ledger session is still live
    (a definitive close of one night must not clear another live night's
    session memory). A just-closed night carries closed_at, so it is
    never live by this check."""
    import ledger as _ledger
    try:
        data = _ledger.load()
        return not _ledger._live_sessions(data, _ledger._now())
    except Exception:
        return False


def _apply_outcome_rating_ops(entry: dict) -> list[dict]:
    """Apply ledger.outcome_rating_ops(entry) to the ratings file, once per
    entry: every op rides idempotency key 'outcome:<entry_id>', so a repeated
    close replays instead of duplicating. Never raises — a learning-sink
    failure must not fail the scored close above it."""
    import ledger as _ledger
    import taste_bridge as _tb
    applied: list[dict] = []
    for op in _ledger.outcome_rating_ops(entry):
        key = "outcome:%s" % entry.get("id")
        try:
            if op.get("action") == "ungold":
                # label "ungold" (in _GOLD_UNMARK_LABELS): un-golding is not
                # a rejection — a label="gold" negative would fold into
                # rejected_phrases and permanently ban the source
                rec = _core.rate(rating=-1, label="ungold",
                                 phrase_id=op.get("phrase_id"),
                                 notes=op.get("notes"),
                                 comment=op.get("comment"),
                                 idempotency_key=key,
                                 entry_id=entry.get("id"))
            elif op.get("action") == "demote":
                rec = _core.rate(rating=0, label=_tb.SERVED_NOT_KEPT_LABEL,
                                 phrase_id=op.get("phrase_id"),
                                 notes=op.get("notes"),
                                 comment=op.get("comment"),
                                 idempotency_key=key,
                                 entry_id=entry.get("id"))
            else:
                continue
            applied.append({"action": op.get("action"),
                            "idempotent": bool(rec.get("idempotent")),
                            "record": rec.get("record")})
        except Exception as exc:
            applied.append({"action": op.get("action"), "error": str(exc)})
    return applied


@mcp.tool(annotations=_WRITES)
def close_session(session_id: Annotated[str | None, Field(description='Ledger session to sweep. Omit to sweep every open session of the Set that is open now.')] = None,
                  attest: Annotated[dict | None, Field(description='Ledger entry id -> owner-attested fact (present, adopted, absent, not_adopted, retire) for rows no sweep can verify.')] = None,
                  confirm: Annotated[bool, Field(description='Only used with attest: False previews the attestation without contacting Live; True writes it, then sweeps if rows still need it. Without attest the sweep always runs.')] = False) -> dict:
    """END OF NIGHT — score the session: which suggestions are still in the Set.

    Reads the complete current Set inventory once and resolves every clip written this session to a
    terminal state (keep / drop / abandoned / unjudged), then reports the
    survival rate stratified by source x role x gear. This is the durable-KEEP
    number the whole arc is about; until it runs, a night's rows stay unresolved.
    Sweep while this Set is still open: after Live restarts only content
    matching remains and some rows may stay unresolved.

    ``attest`` maps ledger entry ids to owner-attested facts (present |
    adopted | absent | not_adopted | retire) for rows no sweep can verify —
    identity-less legacy nights. Without ``confirm`` it is a pure preview;
    with it the attestation is written and Live is contacted only if rows
    still need the sweep.

    Fails closed if Live is unreachable — an empty read is indistinguishable from
    "he deleted everything", and scoring a night as all-drops on a dead bridge
    would be worse than not scoring it at all.
    """
    import ledger as _ledger
    from mcp_server import live_write as _live_write

    attest_report = None
    attest_outcome_ops: list[dict] = []
    if attest is not None:
        try:
            res = _ledger.attest(session_id, attest, confirm=confirm)
        except ValueError as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        if confirm:
            # the owner's word is saved the moment attest returns it; a
            # refused sweep below must not swallow that report
            attest_report = res
            try:
                _post_attest = {e.get("id"): e for e in
                                _ledger.load().get("entries", [])}
            except Exception:
                _post_attest = {}
            attest_outcome_ops, _ = _close_outcome_ratings(
                {"attested": res.get("attested")}, _post_attest, {},
                attestation_confirmed=True)
        pending = [group for group in _ledger.sessions_needing_sweep(_ledger.load())
                   if session_id is None or group["session"].get("id") == session_id]
        # Live is contacted only when a pending row is scoreable by a sweep
        # (it knows its Set). Identity-less rows are settled by attestation
        # alone, so the night stays open with advice until they all are.
        scoreable = [group for group in pending
                     if any(entry.get("set_identity") for entry in group["entries"])]
        if not confirm or not scoreable:
            # Nothing verifiable left (or preview only): Live is not contacted.
            if confirm:
                # A preview writes nothing and tallies nothing; a confirmed
                # attestation just settled rows — count each once.
                try:
                    _post_a = {e.get("id"): e
                               for e in _ledger.load().get("entries", [])}
                except Exception:
                    _post_a = {}
                _tallies_a = _tally_ref_attested_rows(res, _post_a)
                if _tallies_a:
                    res["ref_tallies"] = _tallies_a
                if attest_outcome_ops:
                    res["outcome_ratings"] = attest_outcome_ops
            return res

    try:
        present = _run_async(_live_write.read_set_inventory())
    except Exception as exc:
        out = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "note": "%s — the session is left OPEN on purpose. Re-run with "
                    "Live up; nothing was scored." % _close_failure_reason(exc),
        }
        if attest is not None:
            out["attest_applied"] = confirm
            if attest_report is not None:
                out["attest_report"] = attest_report
            if attest_outcome_ops:
                out["outcome_ratings"] = attest_outcome_ops
        return out
    try:
        _pre_terms = {e.get("id"): e.get("terminal_state")
                      for e in _ledger.load().get("entries", [])}
    except Exception:
        _pre_terms = {}
    res = _ledger.close_session(present, session_id=session_id)
    if attest_report is not None and not res.get("ok"):
        res["attest_applied"] = True
        res["attest_report"] = attest_report
    res["jam_clips_seen"] = sum(1 for clip in present.get("clips", [])
                                 if clip.get("track") == "SIDEKICK JAM")
    res["inventory_coverage"] = present.get("complete") is True
    if res.get("ok"):
        # Only rows whose terminal_state this sweep first set: a cached
        # re-close replays the stored report, and a partial sweep only sets
        # provisional states — neither must bump tallies. Rows the judge
        # path already counted skip again via the card's tallied_entries.
        try:
            _post = {e.get("id"): e
                     for e in _ledger.load().get("entries", [])}
        except Exception:
            _post = {}
        _tallies: list[dict] = []
        for _item in res.get("entries", []) or []:
            _eid = _item.get("entry_id")
            if _pre_terms.get(_eid) is not None:
                continue
            _entry = _post.get(_eid, {})
            if _entry.get("terminal_state") is None:
                continue
            _src = _entry.get("source") or _item.get("source")
            _state = _entry.get("terminal_state")
            if _state == "keep":
                _outcome: str | None = "survived"
            elif _state == "drop":
                _outcome = "dropped"
            else:
                continue
            _tally = _record_ref_tally_for_source(
                _src, _outcome, entry_id=_eid)
            if _tally is not None:
                _tallies.append(_tally)
        # Attest-settled rows carry no terminal_state; their stratum in this
        # same report is their settlement.
        _tallies.extend(_tally_ref_attested_rows(res, _post))
        if _tallies:
            res["ref_tallies"] = _tallies
        # Survival feedback applies to terminal rows created now and explicit
        # retry markers left by a failed write.
        _outcomes, _newly_terminal = _close_outcome_ratings(
            res, _post, _pre_terms)
        _outcomes = attest_outcome_ops + _outcomes
        if _outcomes:
            res["outcome_ratings"] = _outcomes
        if _newly_terminal:
            # DROP streaks are scoped to the ledger session: a definitively
            # closed night clears them (the judgment-event replay guard
            # stays) — but never while another ledger session is still
            # live. A learning-sink failure must not fail the scored close.
            try:
                import ears as _ears
                if _should_reset_streaks():
                    if _ears.reset_streaks():
                        res["ears_reset"] = True
                else:
                    res["ears_reset_skipped"] = True
            except Exception as _exc:
                res["ears_reset_error"] = str(_exc)
    # Arrangement experiments / effect trials close on the same night, in
    # their own subtotal: the intended change is verified (content digest,
    # whereabouts, mutes, device presence), never mere presence. They close
    # on ANY close that read this Set — a sealed phrase night with rows still
    # unknown re-resolves on the next plain close — and only the sessions of
    # this Set are touched. Nothing here changes the phrase denominator.
    res.update(_close_experiments(present, session_id,
                                  res.get("session_ids") if res.get("ok") else None))
    return res


def _close_experiments(present: dict, session_id: str | None,
                       phrase_session_ids: list[str] | None) -> dict:
    import experiments as _experiments
    import ledger as _ledger
    from mcp_server import live_write as _live_write

    data = _ledger.load()
    inventory_set = (present.get("set_identity") or {}).get("set_id")
    if session_id:
        # An explicit night is scored only by evidence from its own Set —
        # the phrase sweep's _sweep_targets rule; another Set's night is
        # never touched, not even to mark it unknown.
        named = next((s for s in data.get("sessions", []) if s.get("id") == session_id), None)
        if named is None or inventory_set not in _ledger._session_chain(named):
            out = _experiments.report(data, session_ids=[])   # nothing swept, nothing totalled
            out["experiments_closed"] = False
            out["experiments_refused"] = (
                "unknown session %r" % session_id if named is None else
                "inventory set %s is not in session %s's chain — refusing to sweep another "
                "Set's night" % (inventory_set, session_id))
            out["experiment_sessions"] = out.pop("session_ids", [])
            return out
        targets = [session_id]
    else:
        targets = sorted(set(phrase_session_ids or [])
                         | set(_experiments.sessions_for(data, present.get("set_identity"))))
    pending = [row for row in _experiments.pending_rows(data) if row.get("session_id") in targets]
    if not pending:
        out = _experiments.report(data, session_ids=targets)
        out["experiments_closed"] = False
        # the phrase close's own session_ids stay its own
        out["experiment_sessions"] = out.pop("session_ids", targets)
        return out
    needs = _experiments.evidence_needs(pending)
    try:
        evidence = _run_async(_live_write.read_experiment_evidence(
            clip_ids=needs["clip_ids"], device_tracks=needs["device_tracks"],
            track_names=needs["track_names"], inventory=present))
    except Exception as exc:  # noqa: BLE001 — an unread Set resolves nothing
        evidence = {"set_identity": present.get("set_identity"), "complete": False,
                    "errors": ["experiment evidence unread: %s: %s" % (type(exc).__name__, exc)],
                    "tracks": {}, "groups": {}, "clip_locations": {}, "clips": {},
                    "track_devices": {}, "devices": {}}
    out = _experiments.sweep(evidence, session_ids=targets)
    out["experiments_closed"] = True
    out["experiment_sessions"] = out.pop("session_ids", targets)
    if evidence.get("errors"):
        out["experiment_evidence_errors"] = list(evidence["errors"])
    return out


@mcp.tool()
def clear_rejects(phrase_id: str | None = None, confirm: bool = False) -> dict:
    """Put back phrases you dropped, so they can be served again.

    A DROP suppresses that phrase (and its near-duplicates) from every future
    answer. This is the undo. Append-only: it writes an explicit positive
    record rather than editing history, so the audit trail of what you rejected
    and when survives. ``phrase_id`` clears one; omit it to clear all.
    ``confirm=False`` previews."""
    import taste_bridge as _tb

    rejects = _tb.rejected_phrases()
    targets = [r for r in rejects
               if phrase_id is None or r["identity"] == phrase_id
               or r.get("phrase_id") == phrase_id]
    pairings = [{"identity": r["identity"], "move": r.get("move")}
                for r in targets]
    n_rejected = len({r["identity"] for r in rejects})
    # would_clear / cleared stay distinct phrase identities (the documented
    # shape); the (identity, move) pairings behind them ride alongside.
    phrases = list(dict.fromkeys(r["identity"] for r in targets))
    if not confirm:
        return {"would_clear": phrases, "would_clear_pairings": pairings,
                "cleared": [], "n_rejected": n_rejected,
                "note": "confirm=True to put these back in the pool"}
    for r in targets:
        # supersede by the same identity: phrase_id when it has one, else the
        # notes whose digest IS the identity
        # same pairing too: a moved DROP is lifted only by a positive on
        # that (source, move) pairing
        _core.rate(1, label="un-rejected", phrase_id=r.get("phrase_id"),
                   notes=r.get("notes"), move=r.get("move"),
                   comment="cleared by clear_rejects")
    remaining = _tb.rejected_phrases()
    return {"would_clear": [], "cleared": phrases,
            "cleared_pairings": pairings,
            "remaining_pairings": [{"identity": r["identity"], "move": r.get("move")}
                                   for r in remaining],
            "n_rejected": len({r["identity"] for r in remaining})}


@mcp.tool()
def adopt_answer(entry_id: str, clip_id: str, confirm: bool = False) -> dict:
    """Preview or record a verified adoption link for an answer already in Live.

    The destination is resolved by exact clip ID in a complete current-Set
    inventory. Confirming stores evidence only; it never moves or writes Live.
    """
    import ledger as _ledger
    from mcp_server import live_write as _live_write

    inventory = _run_async(_live_write.read_set_inventory())
    if inventory.get("complete") is not True:
        return {"status": "unresolved", "entry_id": entry_id, "clip_id": clip_id,
                "reason": "current Set inventory is incomplete",
                "coverage": inventory.get("errors", [])}
    destination = [clip for clip in inventory.get("clips", [])
                   if isinstance(clip, dict) and clip.get("clip_id") == clip_id]
    if len(destination) != 1:
        return {"status": "unresolved", "entry_id": entry_id, "clip_id": clip_id,
                "reason": "clip_id did not resolve uniquely in the current Set"}
    entry = next((item for item in _ledger.load().get("entries", [])
                  if item.get("id") == entry_id), None)
    if entry is None:
        raise ValueError("unknown ledger entry")
    identity = inventory.get("set_identity")
    if not entry.get("set_identity"):
        raise ValueError(
            "ledger entry %s is identity-less — no adoption can verify it; "
            "attest or retire it instead: close_session(session_id=..., "
            "attest={'%s': 'present|adopted|absent|retire'}, confirm=True)"
            % (entry_id, entry_id))
    chain = _ledger.session_chain(entry["session_id"]) if entry.get("session_id") else set()
    if not identity or (identity.get("set_id") != entry["set_identity"].get("set_id")
                        and identity.get("set_id") not in chain):
        raise ValueError("adoption requires the entry and destination to share a verified Set")
    preview = {"status": "preview", "entry_id": entry_id,
               "destination": destination[0], "writes": 0}
    if not confirm:
        return preview
    linked = _ledger.adopt_entry(entry_id, destination[0], set_identity=identity)
    return {"status": "verified", "entry_id": entry_id,
            "destination": destination[0], "adoption": linked.get("adoption_links", [])[-1],
            "writes": 0}


@mcp.tool()
def register_arrangement_candidates(journal_path: str | None = None,
                                    items: list[dict] | None = None,
                                    set_path: str | None = None,
                                    confirm: bool = False) -> dict:
    """Give already-written arrangement experiments (SIDEKICK line variants, a
    whole SIDEKICK drum track) and effect trials (devices the sidekick added to
    his tracks) a durable judgment row — the material was written before any
    row existed, so it is registered as RECOVERED, then judged with
    judge_answer(verdict, entry_id=<id or label>, reason=...) and closed by
    close_session in its own subtotal (never the phrase KEEP rate).

    Input: ``journal_path`` (a write journal: the peak-compose, impact-drums or
    sidechain shape) and/or ``items`` — each {"kind": "arrangement_experiment",
    "label", "track_id", "clip_ids"} or {"kind": "effect_trial", "label",
    "track_id", "device_ids"}. ``set_path`` is the Set the material belongs to
    (taken from the journal when it names one). With Live up: the open Set
    must be exactly that file (Live API identity + runtime epoch), every id
    is read afresh from the Set — the track by id, each clip by a complete
    Live API read (its notes, brace, placement, mute), each device on its
    track with its parameters — and a candidate whose identity or any id
    fails is refused with the reason, never written. A journal's own clip
    lists are never trusted over the track. Preview by default; confirm=True
    writes the rows. Zero writes to Live either way."""
    import experiments as _experiments
    import ledger as _ledger
    from mcp_server import live_write as _live_write

    candidates: list[dict] = []
    expected_set = set_path
    if journal_path:
        try:
            parsed = _experiments.load_journal(journal_path)
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": "journal unusable: %s" % exc, "writes": 0}
        candidates.extend(parsed["items"])
        if expected_set is None:
            expected_set = parsed["set_path"]
    for item in items or []:
        if not isinstance(item, dict):
            return {"ok": False, "error": "items must be dicts", "writes": 0}
        kind = item.get("kind") or (_experiments.EFFECT_KIND if item.get("device_ids")
                                    else _experiments.ARRANGEMENT_KIND)
        proposal_id = item.get("proposal_id")
        if proposal_id is not None:
            # the link must point at a filed proposal: a typo would bind the
            # row to nothing a later brief can find
            from mcp_server import piece_continuity as _piece_continuity
            proposal = _piece_continuity.find_proposal(str(proposal_id))
            if proposal is None:
                return {"ok": False, "writes": 0,
                        "error": "item %r names proposal_id %r, which is not a filed "
                                 "proposal (record_proposal returns the id)"
                                 % (item.get("label") or "candidate", proposal_id)}
            # the proposal is bound to ONE Set: another version of the same
            # piece, or another piece, never links to this registration
            if expected_set is not None and proposal.get("set_key") != expected_set:
                return {"ok": False, "writes": 0,
                        "error": "item %r names proposal_id %r, filed on %s — not the Set "
                                 "being registered (%s); nothing read, nothing registered"
                                 % (item.get("label") or "candidate", proposal_id,
                                    proposal.get("set_key"), expected_set)}
            proposal_id = str(proposal_id)
        candidates.append({"kind": kind, "label": item.get("label") or "candidate",
                           "track_id": str(item.get("track_id") or ""),
                           "clip_ids": [str(c) for c in item.get("clip_ids") or []],
                           "device_ids": [str(d) for d in item.get("device_ids") or []],
                           "group": item.get("group") or "items", "move": item.get("move") or (
                               "fx:device" if kind == _experiments.EFFECT_KIND else "arr:variant"),
                           "journal": None, "proposal_id": proposal_id})
    if not candidates:
        return {"ok": False, "error": "nothing to register: pass journal_path and/or items",
                "writes": 0}
    if not expected_set:
        return {"ok": False, "writes": 0,
                "error": "the journal names no Set — pass set_path (the .als the material "
                         "was written into) so identity can be verified"}
    needs = _experiments.evidence_needs(candidates)
    try:
        evidence = _run_async(_live_write.read_experiment_evidence(
            clip_ids=needs["clip_ids"], device_tracks=needs["device_tracks"]))
    except Exception as exc:  # noqa: BLE001 — unreachable Live registers nothing
        return {"ok": False, "writes": 0,
                "error": "%s: %s" % (type(exc).__name__, exc),
                "note": "%s — nothing registered; re-run with Live up and the Set open"
                        % _close_failure_reason(exc)}
    verified: list[dict] = []
    refused: list[dict] = []
    for candidate in candidates:
        row, why = _experiments.verify_candidate(candidate, evidence,
                                                 expected_set_path=expected_set)
        if row is None:
            refused.append({"label": candidate.get("label"), "kind": candidate.get("kind"),
                            "track_id": candidate.get("track_id"), "reason": why})
        else:
            verified.append(row)
    identity = evidence.get("set_identity") or {}
    if not verified and refused and all(
            "nothing registered" in (r["reason"] or "") for r in refused):
        # identity or inventory failed for everything: one error, not N rows
        return {"ok": False, "writes": 0, "error": refused[0]["reason"],
                "set_identity": identity, "evidence_errors": evidence.get("errors")}

    def _view(row: dict) -> dict:
        view = {"label": row["label"], "kind": row["kind"], "track": row["track"],
                "group": row.get("group"), "move": row.get("move"),
                "proposal_id": row.get("proposal_id")}
        if row["kind"] == _experiments.ARRANGEMENT_KIND:
            view["clip_ids"] = [c["clip_id"] for c in row["clips"]]
            view["n_notes"] = [c["n_notes"] for c in row["clips"]]
            view["unregistered_on_track"] = row.get("unregistered_on_track") or []
        else:
            view["device_ids"] = [d["device_id"] for d in row["devices"]]
            view["params"] = {d["device_id"]: d["params"] for d in row["devices"]}
            view["params_coverage"] = {d["device_id"]: d.get("params_coverage")
                                       for d in row["devices"]}
            errors = {d["device_id"]: d["params_error"] for d in row["devices"]
                      if d.get("params_error")}
            if errors:
                view["params_error"] = errors
        return view

    out = {"ok": True, "written": False, "writes": 0, "set_identity": identity,
           "verified": [_view(row) for row in verified], "refused": refused}
    if not confirm:
        # read-only: which verified candidates are a closed, survived row's
        # material seen again (an observation on it, never a second row)
        try:
            would = _experiments.observed_on(verified, path=_ledger.LEDGER_PATH) if verified else []
        except Exception:  # noqa: BLE001 — a preview never fails on the ledger
            would = [None] * len(verified)
        for view, closed in zip(out["verified"], would):
            view["would_observe"] = closed
        observed = [c for c in would if c]
        out["note"] = ("preview — %d candidate(s) verified against the open Set, %d refused; "
                       "confirm=True registers the verified ones (recovered writes)%s"
                       % (len(verified), len(refused),
                          ("; %d would be recorded as an observation on %s (already in the "
                           "score, not a new judged row)"
                           % (len(observed), ", ".join(sorted({c["entry_id"] for c in observed}))))
                          if observed else ""))
        return out
    results = _experiments.register(verified, path=_ledger.LEDGER_PATH) if verified else []
    registered = [r for r in results if r.get("registration") in ("new", "existing", "rebound")]
    for r in results:
        if r.get("registration") == "refused":
            refused.append({"label": r.get("label"), "kind": r.get("kind"),
                            "track_id": (r.get("track") or {}).get("id"), "reason": r.get("reason")})
    # the same material as a CLOSED row, present again (another Live run, or
    # new ids in this one): an observation on that row, never a second row
    observed = [{"entry_id": r["id"], "label": r.get("label"), "kind": r.get("kind"),
                 "state": r.get("state"), "closed_at": r.get("closed_at"),
                 "differs": r.get("differs"), "observation": r.get("observation"),
                 "already_recorded": bool(r.get("already_recorded")),
                 "reason": r.get("reason")}
                for r in results if r.get("registration") == "observed"]
    out["refused"] = refused
    out["observed"] = observed
    out["written"] = any(r.get("registration") == "new" for r in results)
    out["registered"] = [dict(_view(row), entry_id=row["id"], session_id=row["session_id"],
                              already_registered=row.get("registration") != "new",
                              rebound=row.get("rebound") or {},
                              moved_to=row.get("moved_to"),
                              new_round_of=row.get("new_round_of"))
                         for row in registered]
    n_new = sum(1 for r in results if r.get("registration") == "new")
    n_existing = len(registered) - n_new
    n_rebound = sum(1 for r in results if r.get("registration") == "rebound")
    unread_params = [row for row in out["registered"]
                     if any(c != "full" for c in (row.get("params_coverage") or {}).values())]
    out["note"] = ("%d row(s) registered as recovered writes%s, %d refused%s. Next: he listens and "
                   "says which (1/2/3) or KEEP/DROP with why → judge_answer('keep'|'drop', "
                   "entry_id=<entry_id or label>, reason=...) — selecting one judges that one "
                   "only; at night close_session() verifies what survived"
                   % (n_new, (" (%d already registered — the existing row(s) are returned%s)"
                              % (n_existing, (", %d rebound to the ids this read verified"
                                              % n_rebound if n_rebound else ""))
                              if n_existing else ""), len(refused),
                      (", %d closed row(s) seen again — %s as a next-session observation "
                       "on the closed row, already in the score, never a second judged row"
                       % (len(observed),
                          "already recorded" if all(o["already_recorded"] for o in observed)
                          else "recorded")) if observed else ""))
    if unread_params:
        out["note"] += (". %d effect row(s) registered with partial or no parameter coverage "
                        "(see params_coverage / params_error): presence is verified, parameters "
                        "will never compare for them — a device present is not an effect heard"
                        % len(unread_params))
    try:
        out["ledger_status"] = _ledger.ledger_status(path=_ledger.LEDGER_PATH)
    except Exception:  # noqa: BLE001
        pass
    return out


@mcp.tool(annotations=_WRITES)
def session_report(refresh: Annotated[bool, Field(description='False (default) only reads the ledger. True reads the current Set and records next-session / adoption evidence.')] = False,
                   session_id: Annotated[str | None, Field(description='Limit the report to one ledger session.')] = None) -> dict:
    """What the judgment ledger holds; optionally refresh from current Set evidence.

    The default is read-only. ``refresh=True`` reads a complete inventory and
    records deduplicated next-session/adoption observations without rewriting a
    closed session's terminal history.
    """
    import ledger as _ledger
    import taste_bridge as _tb
    from corpus_keys import corpus_staleness as _corpus_staleness  # analyze/ on path via core

    out = _ledger.summary()
    import rituals as _rituals
    out["finishing_counts"] = _rituals.finishing_counts()
    out["disk"] = _store_disk_state()
    try:
        set_path = _ledger.last_known_set_path(_ledger.load())
    except Exception:  # noqa: BLE001 — staleness is advisory, never fatal
        set_path = None
    for note in _corpus_staleness(os.path.join(ROOT, "store"), set_path):
        out.setdefault("advice", []).append(note)
    # Arrangement experiments / effect trials: their own subtotal and their
    # own reminders (ledger_status.next_action only sees phrase rows).
    import experiments as _experiments
    _experiment_data = _ledger.load()
    _experiment_report = _experiments.report(
        _experiment_data, session_ids=[session_id] if session_id else None)
    out["arrangement"] = _experiment_report["arrangement"]
    out["effect_trials"] = _experiment_report["effect_trials"]
    out["experiment_sessions"] = _experiment_report["session_ids"]
    for note in _experiments.advice(_experiment_data):
        out.setdefault("advice", []).append(note)
    if refresh:
        from mcp_server import live_write as _live_write
        present = _run_async(_live_write.read_set_inventory())
        out["refresh"] = _ledger.refresh_observations(present, session_id=session_id)
        out["observations"] = _ledger.observation_report()
    # The reject cull is the thing that stops an idea coming back, and it has
    # nothing to work from until he says no once. Ask here rather than in
    # ledger.py, which the OSC listener imports and must stay dependency-light.
    rejected = _tb.rejected_phrases()
    out["n_rejected"] = len({r["identity"] for r in rejected})
    out["rejected_pairings"] = [{"identity": r["identity"], "move": r.get("move")}
                                for r in rejected]
    if rejected:
        out.setdefault("advice", []).append(
            "%d phrase(s) are suppressed because you dropped them — clear_rejects() "
            "lists and undoes them" % out["n_rejected"])
    elif out.get("open_session_awaiting_sweep", 0) >= 3:
        out.setdefault("advice", []).append(
            "no DROP has ever been recorded — until one exists the system cannot "
            "stop repeating what you reject. judge_answer('drop') on the next one "
            "that misses; nothing else is needed")
    return out


# --- piece continuity: piece identity apart from Set identity -----------------
# piece_context / piece_continuity are imported by package name only, the ONE
# module object the tests patch (experiments.py says why).

@mcp.tool()
def declare_piece_version(piece: str, set_path: str, confirm: bool = False,
                          name: str | None = None, notes_path: str | None = None) -> dict:
    """Bind a Set (exact .als path) to a piece on RICARDO's confirmation — never
    inferred from a similar filename. Piece-level records (intent, his
    statements, proposals, reactions) then resolve for every confirmed version;
    Set-level facts (clip ids, snapshots, ledger identity) stay bound to the
    exact Set. Preview by default; confirm=True records it as confirmed by the
    owner. Zero writes to Live. The NOCTURNE seed (v2→v5) is an owner-run
    script: tools/declare_piece.py --confirm."""
    from mcp_server import piece_context as _piece_context

    try:
        return _piece_context.declare_piece_version(
            piece, set_path, confirm=confirm, name=name, notes_path=notes_path, via="tool")
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "writes": 0}


@mcp.tool()
def record_proposal(piece: str, set_path: str, question: str, source_gesture: dict,
                    reference_job: dict | None = None, preserve: list[str] | None = None,
                    vary: list[str] | None = None, candidates: list[dict] | None = None,
                    seeds: list[str] | None = None, notes: str | None = None) -> dict:
    """File one proposal so a next session can continue it; returns its
    proposal_id. question: the musical question ("how can 79–90 keep its
    intensity while the foreground clears?"). source_gesture: HIS clip /
    phrase / cell with ids (or a catalogue phrase id). reference_job: {ref, job,
    evidence} — the reference and its one JOB ("Nolan: bells on their own
    grid"; evidence in owner|model_heard|measured|set_file|inference).
    preserve: what stays unchanged; vary: what was varied (one dimension).
    candidates: [{label, track_id, clip_ids}] handles in that Set; seeds: seed
    numbers / script names. set_path must be a version RICARDO confirmed with
    declare_piece_version. register_arrangement_candidates items may carry the
    proposal_id so the ledger row links back. Zero writes to Live."""
    from mcp_server import piece_continuity as _piece_continuity

    try:
        return _piece_continuity.record_proposal(
            piece, set_path, question, source_gesture=source_gesture,
            reference_job=reference_job, preserve=preserve or [], vary=vary or [],
            candidates=candidates or [], seeds=seeds or [], notes=notes)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "writes": 0}


@mcp.tool()
def record_reaction(words: str,
                    scope: Literal["section", "track", "candidate", "piece", "global"],
                    proposal_id: str | None = None, piece: str | None = None,
                    scope_detail: str | None = None, interpretation: str | None = None,
                    rule: bool = False) -> dict:
    """RICARDO's words verbatim, scoped — section (scope_detail = bars), track,
    candidate (its label), piece (optional topic, e.g. "drums") or global.
    interpretation is YOUR reading, stored apart and never promoted to a rule;
    rule=True only when he states a rule. A reaction never creates a KEEP/DROP
    — verdicts stay with judge_answer. Pass proposal_id (binds piece + Set) or
    piece. Zero writes to Live."""
    from mcp_server import piece_continuity as _piece_continuity

    try:
        return _piece_continuity.record_reaction(
            words, scope, proposal_id=proposal_id, piece=piece, scope_detail=scope_detail,
            interpretation=interpretation, rule=rule)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "writes": 0}


@mcp.tool()
def record_observation(set_path: str, what: str, evidence: dict) -> dict:
    """A Set change seen on a read — track deleted / muted, clip moved or
    added, device removed — with evidence.snapshot_digest (required) and the
    ids involved. Bound to that exact Set; "observed, unjudged" and nothing
    turns it into a KEEP/DROP (an unjudged row never enters a rate). Zero
    writes to Live."""
    from mcp_server import piece_continuity as _piece_continuity

    try:
        return _piece_continuity.record_observation(set_path, what, evidence)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "writes": 0}


@mcp.tool()
def piece_brief(set_path: str, live: bool = False) -> dict:
    """Read-only brief to continue a piece from this Set: piece + confirmed
    versions; owner intent (piece-level, version overrides, `inherited`
    named); HIS statements and the model-heard / measured observations in
    separate blocks (provenance, date, the bounce/Set they came from); the
    piece notes' "Suggestions" and "Cuts and transitions" tables with status;
    open questions; proposals with his reactions; Set changes "observed,
    unjudged"; references with their jobs; a prose brief. Never contacts Live
    unless live=True (then one read-only snapshot summary; a failed read is
    reported, not raised). Never authorises a write; phrase retrieval is not
    invoked here."""
    from mcp_server import piece_continuity as _piece_continuity

    snapshot = None
    if live:
        from mcp_server import moment as _moment_mod
        try:
            snapshot = _piece_continuity.snapshot_summary(
                _run_async(_moment_mod.read_piece_snapshot()))
        except Exception as exc:  # noqa: BLE001 — the brief still answers offline
            snapshot = {"status": "unavailable",
                        "reason": "%s: %s" % (type(exc).__name__, exc)}
        else:
            if not snapshot.get("file_path"):
                # no Set file in the read's identity: cannot be bound to this Set
                snapshot["status"] = "live_unverified"
                snapshot["note"] = ("the read carries no Set file path — it cannot be "
                                    "verified as %s" % set_path)
            elif snapshot["file_path"] != set_path:
                # bound to the Set Live actually has open, never passed off as this one
                snapshot["status"] = "live_other_set"
                snapshot["note"] = ("Live has %s open, not %s — this read describes that Set"
                                    % (snapshot["file_path"], set_path))
    return _piece_continuity.piece_brief(set_path, root=ROOT, snapshot=snapshot)


# Validate FastMCP server registration and description closure before pruning
_profiles.validate_server_registration(mcp)
_profiles.validate_server_description_closure(mcp)
_pruned_tools = _profiles.prune_tools_for_profile(mcp, _active_profile)


def main():
    if "--selftest" in sys.argv:
        from mcp_server import selftest as _selftest
        raise SystemExit(_selftest.run_selftest(sys.modules[__name__]))
    try:                                   # SOMBRA pedalboard listener + presence lamp
        import sombra_listener             # (never in selftest — real sessions only)
        sombra_listener.start()
    except Exception as exc:               # noqa: BLE001 — a dead lamp must not kill MCP
        print(f"sombra listener unavailable: {exc}", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
