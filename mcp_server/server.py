"""style-sidekick MCP server — Claude's composition toolkit.

Claude (the MCP client) is the composer. These tools supply Ricardo's style
context, real phrases, structure templates, scoring, and MIDI export. They do
NOT generate music with their own model.

Run:
    .venv/bin/python mcp_server/server.py              # stdio MCP server
    .venv/bin/python mcp_server/server.py --selftest   # verify all tools
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Literal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mcp.server.fastmcp import FastMCP

from mcp_server import core as _core
from mcp_server import core_shared
from mcp_server import profiles as _profiles
from mcp_server.session_ears import DEFAULT_AGY_MODEL

_active_profile = _profiles.get_active_profile_name()

mcp = FastMCP(
    "style-sidekick",
    instructions=_profiles.get_profile_instructions(_active_profile),
)


def _run_async(coro):
    from mcp_server import pp_session as _pp_session
    return _pp_session.run_coro_blocking(coro)


@mcp.tool()
def get_style() -> dict:
    """Return Ricardo's style profile: STYLE.md text + structured facts."""
    return _core.get_style()


@mcp.tool()
def find_phrases(
    seed_notes: list[dict] | None = None,
    kind: str | None = None,
    k: int = 6,
    key: str | None = None,
    tempo: float | None = None,
    prefer_liked: bool = False,
    role: str | None = None,
    use_ears: bool | None = None,
    direction: str | None = None,
    moment: dict | None = None,
    relax: str | None = None,
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


@mcp.tool()
def hear_the_room(where: str = "session", bar: int | None = None) -> dict:
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
    behind the loop brace that do not play. Read-only; if Live is unreachable
    this declares blind mode. Unheard names are conversational evidence only —
    ask what they are doing; never infer musical content from the name. The
    result also carries the judgment loop state (open session,
    pending judgments, unswept nights, next action), a scoring-readiness probe
    for new writes, and the store disk's free space. Ended-but-unswept nights
    of the open Set are swept automatically and listed under auto_closed
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


@mcp.tool()
async def listen_to_takes(
    inventory_id: str,
    take_ids: list[str],
    focus: str = "full",
    model: str = DEFAULT_AGY_MODEL,
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


@mcp.tool()
def grow_my_loop(seed_notes: list[dict], n: int = 3, key: str | None = None,
                 tempo: float = 120.0, cont_bars: int = 4, seed: int = 0,
                 humanize: bool = False,
                 use_ears: bool | None = None) -> dict:
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


@mcp.tool()
def answer_my_loop(seed_notes: list[dict] | None = None, bars: int | None = None,
                   key: str | None = None, tempo: float | None = None,
                   transform: str | None = None, humanize: bool = False,
                   write: bool = False, seed: int = 0, gear: int = 2,
                   surgery: str | None = None, direction: str | None = None,
                   axes: dict | None = None,
                   use_ears: bool | None = None,
                   moment: dict | None = None,
                   ref_id: str | None = None,
                   role: str = "lead",
                   relax: str | None = None) -> dict:
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


@mcp.tool()
def develop_my_loop(seed_notes: list[dict] | None = None, move: str | None = None,
                    key: str | None = None, tempo: float | None = None,
                    bars: int | None = None, seed: int = 0,
                    use_ears: bool | None = None,
                    direction: str | None = None) -> dict:
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


@mcp.tool()
def judge_answer(verdict: str, slot: str | None = None,
                 source_phrase_id: str | None = None,
                 answer_notes: list[dict] | None = None,
                 reason: str | None = None, confirm: bool = False,
                 seed_notes: list[dict] | None = None,
                 key: str | None = None,
                 role: str | None = None,
                 entry_id: str | None = None,
                 source: str | None = None) -> dict:
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


@mcp.tool()
def score_phrase(notes: list[dict], tempo: float | None = None,
                 role: str | None = None, key: str | None = None) -> dict:
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


@mcp.tool()
def export_midi(notes: list[dict], path: str, tempo: float = 120.0,
                allow_outside_exports: bool = False) -> dict:
    """Export notes to .mid. Relative paths stay under store/exports by default."""
    return _core.export_midi(
        notes=notes,
        path=path,
        tempo=tempo,
        allow_outside_exports=allow_outside_exports,
    )


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


@mcp.tool()
def write_midi_clip(
    track_name: str,
    scene_index: int,
    notes: list[dict],
    name: str = "SIDEKICK MIDI",
    loop_start: str = "1|1",
    loop_length: str = "1bar",
    replace: bool = False,
    confirm: bool = False,
    source: str = "catalogue",
    role: str = "lead",
    gear: int | None = None,
    metadata: dict | None = None,
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
    if source != "catalogue" and not str(source).startswith("ref:"):
        raise ValueError("source must be 'catalogue' or 'ref:<ref_id>'")
    if not isinstance(role, str) or not role.strip():
        raise ValueError("role must be a non-empty string")
    if gear is not None and (isinstance(gear, bool) or not isinstance(gear, int)):
        raise ValueError("gear must be an integer or None")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("metadata must be a dict or None")

    result = _run_async(_clip_mutation.write_midi_clip(
        track_name, scene_index, notes, name=name, loop_start=loop_start,
        loop_length=loop_length, replace=replace, confirm=confirm))
    return result


@mcp.tool()
def write_composed_to_jam(notes: list[dict], contract: dict, name: str,
                          role: str = "lead", source: str = "catalogue",
                          ears: str | None = None, confirm: bool = False) -> dict:
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


@mcp.tool()
def close_session(session_id: str | None = None, attest: dict | None = None,
                  confirm: bool = False) -> dict:
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


@mcp.tool()
def session_report(refresh: bool = False, session_id: str | None = None) -> dict:
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


def _pp(obj, max_len: int = 1200) -> str:
    s = json.dumps(obj, indent=2, default=str)
    if len(s) > max_len:
        return s[:max_len] + f"\n... ({len(s) - max_len} chars truncated)"
    return s


def run_selftest() -> int:
    """Exercise every tool with sample inputs; verify MIDI round-trip."""
    import shutil
    import tempfile

    print("=== style-sidekick selftest ===\n")
    errors = []

    # The selftest must never touch HIS live session state. On 2026-09-03 block 81
    # judged through the real path without redirecting these module globals, and
    # wrote reject streaks into his ears session — test artifacts indistinguishable
    # from his playing. Snapshot FIRST, before the module sub-selftests run, so
    # their writes are covered too; assert unchanged at the end: one invariant
    # that catches the next leak whoever writes it.
    import hashlib as _hashlib_guard
    import develop_session as _ds_guard
    import ears as _ears_guard

    def _session_state_fingerprint():
        # mtime as well as content: a rewrite that happens to reproduce the same
        # bytes is still a write to his files, and a hash alone cannot see it.
        import glob as _glob_guard
        out = {}
        for _label, _path in (("ears", _ears_guard.SESSION_PATH),
                              ("develop", _ds_guard.STATE_PATH)):
            try:
                with open(_path, "rb") as _fh:
                    _digest = _hashlib_guard.sha256(_fh.read()).hexdigest()
                out[_label] = (_digest, os.stat(_path).st_mtime_ns)
            except FileNotFoundError:
                out[_label] = None
        # Every top-level store/tools-ideas document: the selftest owns none
        # of them (ratings/taste-model/rituals fixtures all live in temp
        # dirs, and the export block writes to a temp dir too), so any change
        # here is a leak into his live state. Lock files excluded: the
        # ratings/ledger flock heartbeat touches them by design (kept as a
        # defensive skip — the globs below never match *.lock today).
        for _path in sorted(
                _glob_guard.glob(os.path.join(ROOT, "store", "*.json"))
                + _glob_guard.glob(os.path.join(ROOT, "store", "*.md"))
                + _glob_guard.glob(os.path.join(ROOT, "tools", "ideas", "*.json"))):
            if _path.endswith(".lock"):
                continue
            try:
                with open(_path, "rb") as _fh:
                    _digest = _hashlib_guard.sha256(_fh.read()).hexdigest()
                out[_path] = (_digest, os.stat(_path).st_mtime_ns)
            except FileNotFoundError:
                out[_path] = None
        return out

    _session_state_before = _session_state_fingerprint()

    import jam_lifecycle as _jam_lifecycle
    _jam_lifecycle.run_selftest()

    import pp_codec as _pp_codec
    import pp_session as _pp_session
    from mcp_server import capture_support as _capture_support
    from mcp_server import capture_loop as _capture_loop_support
    _pp_codec.run_selftest()
    _pp_session.run_selftest()
    _capture_support.run_selftest()
    _capture_loop_support.run_selftest()

    print("--- profile registry, registration, and recipe/instruction/description closure ---")
    from mcp_server import profiles as _profiles_mod
    _all_docs = {name: getattr(sys.modules[__name__], name).__doc__ or "" for name in _profiles_mod.ALL_TOOLS}
    _profiles_mod.validate_registered_tools(list(_all_docs.keys()))
    _profiles_mod.validate_all_profiles(tool_descriptions=_all_docs)
    for _p in _profiles_mod.VALID_PROFILES:
        _tools = _profiles_mod.get_profile_tools(_p)
        assert len(_tools) > 0, f"profile {_p} has empty tool set"
        assert _tools.issubset(_profiles_mod.get_profile_tools("all")), f"profile {_p} contains unregistered tools"
        if _p != "all":
            assert len(_tools) < len(_profiles_mod.get_profile_tools("all")), f"profile {_p} must be strictly smaller than 'all'"
    assert _profiles_mod.get_active_profile_name() in _profiles_mod.VALID_PROFILES
    print("  OK\n")

    # ratings_tmp lives in its own mkdtemp (not bare $TMPDIR): rolling
    # backups of the fixture would otherwise accumulate in $TMPDIR/backups.
    _ratings_tmpdir = tempfile.mkdtemp(prefix="sidekick_ratings_selftest_")
    ratings_tmp = os.path.join(
        _ratings_tmpdir,
        f"sidekick_ratings_selftest_{os.getpid()}.json",
    )
    _core.set_ratings_path(ratings_tmp)
    assert _core._taste_model_path() == os.path.splitext(ratings_tmp)[0] + ".taste_model.json", (
        "selftest ratings and derived taste model must share a unique fixture stem"
    )

    # 1. get_style
    print("--- get_style() ---")
    style = _core.get_style()
    assert "style_md" in style and "structured" in style
    assert len(style["style_md"]) > 500
    assert style["structured"]["melody_shape"]
    print(f"  STYLE.md: {len(style['style_md'])} chars")
    print(f"  Top key: {style['structured']['keys'][0]}")
    print(f"  Tempo mean: {style['structured']['tempo']['mean']}")
    print("  OK\n")

    # 2. find_phrases (exemplars)
    print("--- find_phrases(kind='harmonic', k=3) ---")
    phrases = _core.find_phrases(kind="harmonic", k=3)
    assert len(phrases) == 3
    for p in phrases:
        assert p["notes"] and p["n_notes"] >= 4
    print(_pp(phrases, 800))
    print("  OK\n")

    # 3. find_phrases (seeded)
    print("--- find_phrases(seed_notes=..., kind='harmonic', k=3) ---")
    seed = phrases[0]["notes"][:6]
    seeded = _core.find_phrases(seed_notes=seed, kind="harmonic", k=3)
    assert len(seeded) >= 1
    assert seeded[0]["similarity"] is not None
    print(_pp(seeded, 600))
    print("  OK\n")

    # 4. find_phrases (drum)
    print("--- find_phrases(kind='drum', k=2) ---")
    drums = _core.find_phrases(kind="drum", k=2)
    assert all(p["kind"] == "drum" for p in drums)
    print(_pp(drums, 500))
    print("  OK\n")

    # 5. suggest_structure
    print("--- suggest_structure(target_minutes=4) ---")
    skel = _core.suggest_structure(target_minutes=4, tempo=120)
    assert skel["sections"] and skel["total_bars"] >= 8
    bar_sum = sum(s["bars"] for s in skel["sections"])
    assert bar_sum == skel["total_bars"], f"bar sum {bar_sum} != {skel['total_bars']}"
    print(_pp(skel, 900))
    print("  OK\n")

    # 6. score_phrase
    print("--- score_phrase(sample notes) ---")
    sample_notes = [
        {"pitch": 60, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 80},
        {"pitch": 62, "start_beats": 0.5, "duration_beats": 0.5, "velocity": 75},
        {"pitch": 64, "start_beats": 1.0, "duration_beats": 0.5, "velocity": 78},
        {"pitch": 65, "start_beats": 1.5, "duration_beats": 0.5, "velocity": 72},
        {"pitch": 67, "start_beats": 2.0, "duration_beats": 1.0, "velocity": 85},
    ]
    score = _core.score_phrase(sample_notes, tempo=120)
    assert 0 <= score["overall"] <= 1.0
    print(_pp(score))
    print("  OK\n")

    # 7. compose_brief
    print("--- compose_brief('dark 6 minute song') ---")
    brief = _core.compose_brief("dark 6 minute song with breakdown")
    assert brief["style_summary"] and brief["example_phrases"]
    assert brief["suggested_structure"] is not None
    print(f"  phrases: {len(brief['example_phrases'])}, "
          f"sections: {len(brief['suggested_structure']['sections'])}")
    print("  OK\n")

    # 8. export_midi + round-trip verify
    print("--- export_midi() + mido round-trip ---")
    import mido

    _exports_tmp = tempfile.mkdtemp(prefix="sidekick_selftest_exports_")
    out_path = os.path.join(_exports_tmp, "selftest_phrase.mid")
    assert _core._resolve_export_path("selftest_phrase.mid") == os.path.join(
        ROOT, "store", "exports", "selftest_phrase.mid")
    assert _core._resolve_export_path("store/exports/selftest_phrase.mid") == os.path.join(
        ROOT, "store", "exports", "selftest_phrase.mid")
    result = _core.export_midi(sample_notes, out_path, tempo=120,
                               allow_outside_exports=True)
    assert os.path.exists(out_path), "MIDI file not written"

    mid = mido.MidiFile(out_path)
    parsed_pitches = []
    for track in mid.tracks:
        for msg in track:
            if msg.type == "note_on" and msg.velocity > 0:
                parsed_pitches.append(msg.note)

    expected_pitches = sorted({n["pitch"] for n in sample_notes})
    assert len(parsed_pitches) == len(sample_notes), (
        f"expected {len(sample_notes)} note_on events, got {len(parsed_pitches)}"
    )
    assert sorted(parsed_pitches) == expected_pitches, (
        f"pitch mismatch: {sorted(parsed_pitches)} vs {expected_pitches}"
    )
    print(f"  wrote: {result['path']}")
    print(f"  note_on count: {len(parsed_pitches)}, pitches: {sorted(parsed_pitches)}")
    _unsafe_tmp = tempfile.mkdtemp(prefix="sidekick_unsafe_export_")
    unsafe_path = os.path.join(_unsafe_tmp, "sidekick_unsafe_export.mid")
    try:
        _core.export_midi(sample_notes, unsafe_path, tempo=120)
        raise AssertionError("export_midi must reject paths outside store/exports by default")
    except ValueError as exc:
        assert "store/exports" in str(exc)
    finally:
        shutil.rmtree(_unsafe_tmp, ignore_errors=True)
    print("  OK\n")
    shutil.rmtree(_exports_tmp, ignore_errors=True)

    # 9. duration token round-trip (incl. bar-length notes)
    print("--- _duration_to_pp() round-trip ---")
    for d in (0.0002, 0.0078, 0.0099, 0.1375, 0.208333, 0.25, 0.375, 0.5, 1.0, 1.125, 2.0,
              2.25, 2.5, 3.0, 3.75, 4.0, 8.0):
        tok = _core._duration_to_pp(d)
        back = _core._pp_duration_to_beats(tok)
        assert abs(back - d) < 1e-6, f"{d} -> {tok} -> {back} beats (delta {abs(back - d)})"
        print(f"  {d} beats -> {tok} -> {back} beats")
    assert _core._duration_to_pp(2.25) == "n9/16"
    assert _core._duration_to_pp(2.5) == "n5/8"
    assert _core._duration_to_pp(3.0) == "n3/4"
    assert _core._duration_to_pp(3.75) == "n15/16"
    assert _core._duration_to_pp(2.2552) == "n2819/5000"
    assert _core._duration_to_pp(0.208333) == "n5/96", \
        "high-precision floats should use the simplest in-tolerance PP fraction"
    _irrational_tok9 = _core._duration_to_pp(3.141592653589793)
    assert int(_irrational_tok9.split("/", 1)[1]) <= 100_000, \
        "Producer Pal duration tokens need a bounded denominator"
    assert abs(_core._pp_duration_to_beats(_irrational_tok9) - 3.141592653589793) < 1e-6
    for _invalid_duration9 in (0.0, -0.25, float("nan"), float("inf")):
        try:
            _core._duration_to_pp(_invalid_duration9)
            raise AssertionError("non-positive/non-finite durations must be rejected")
        except ValueError:
            pass
    print("  OK\n")

    # 10. send_to_ableton → Producer Pal translation
    print("--- send_to_ableton() Producer Pal translation ---")
    from mcp_server import live_write as _live_write
    assert _live_write._find_int("regularTrackCount:12", "regularTrackCount", 0) == 12
    assert _live_write._find_int("regularTrackCount: 12", "regularTrackCount", 0) == 12
    selected_ctx = _live_write._extract_selected_context(
        '{view:"session",selectedTrack:{id:"2",type:"midi",trackIndex:0},'
        'selectedScene:{id:"8",sceneIndex:3},selectedDevice:{id:"17",path:"t0/d0"}}'
    )
    assert selected_ctx["selected_track"]["track_index"] == 0
    assert selected_ctx["selected_scene"]["scene_index"] == 3
    handoff = _core.send_to_ableton(
        sample_notes,
        track=0,
        clip_name="selftest_sketch",
        tempo=120,
        bar_length=2,
    )
    assert handoff["note_count"] == len(sample_notes)
    assert len(handoff["translated_notes"]) == len(sample_notes)
    assert handoff["tool_calls"][-1]["tool"] == "ppal-create-clip"
    pp_notes = handoff["tool_calls"][-1]["arguments"]["notes"]
    assert pp_notes, "empty Producer Pal notes string"
    # Producer Pal length must be "Nbar" — the live API rejects "N:0" (regression guard).
    import re as _re
    assert _re.match(r"^\d+bar$", handoff["clip_length"]), \
        f"clip_length {handoff['clip_length']!r} is not a valid Producer Pal 'Nbar' token"
    assert _re.match(r"^\d+bar$", handoff["tool_calls"][-1]["arguments"]["length"])
    scene_handoff = _core.send_to_ableton(
        sample_notes,
        track=2,
        clip_name="scene-target",
        tempo=0,
        session_scene=3,
    )
    # schema-listed addressing (Producer Pal 2.2 schema + 2.3 path system): path, never slot
    assert scene_handoff["tool_calls"][-1]["arguments"]["path"] == "t2/s3"
    assert "slot" not in scene_handoff["tool_calls"][-1]["arguments"]

    for src, tr in zip(sample_notes, handoff["translated_notes"]):
        assert tr["pitch_midi"] == src["pitch"]
        assert tr["velocity"] == src["velocity"]
        assert abs(tr["start_beats"] - src["start_beats"]) < 1e-6
        assert abs(tr["duration_beats"] - src["duration_beats"]) < 1e-6
        pos_beats = _core._bar_beat_to_beats(tr["position_bar_beat"])
        assert abs(pos_beats - src["start_beats"]) < 1e-6, (
            f"bar|beat {tr['position_bar_beat']} != start {src['start_beats']}"
        )
        dur_beats = _core._pp_duration_to_beats(tr["duration_token"])
        assert abs(dur_beats - src["duration_beats"]) < 1e-6, (
            f"duration {tr['duration_token']} != {src['duration_beats']} beats"
        )

    print(f"  note_count: {handoff['note_count']}")
    print(f"  clip_length: {handoff['clip_length']}")
    print(f"  notes snippet: {pp_notes[:120]}...")
    print("  OK\n")

    # 10a. pp_notes_to_notes round-trip (incl. sticky velocity + comma positions)
    print("--- pp_notes_to_notes() round-trip ---")

    def _assert_notes_match(src: list[dict], back: list[dict], label: str) -> None:
        assert len(back) == len(src), (
            f"{label}: note count {len(back)} != {len(src)}"
        )
        src_s = sorted(src, key=lambda n: (n["start_beats"], n["pitch"]))
        back_s = sorted(back, key=lambda n: (n["start_beats"], n["pitch"]))
        for a, b in zip(src_s, back_s):
            assert a["pitch"] == b["pitch"], (
                f"{label}: pitch {b['pitch']} != {a['pitch']}"
            )
            assert abs(a["start_beats"] - b["start_beats"]) < 1e-4, (
                f"{label}: start {b['start_beats']} != {a['start_beats']}"
            )
            assert abs(a["duration_beats"] - b["duration_beats"]) < 0.26, (
                f"{label}: duration {b['duration_beats']} != {a['duration_beats']}"
            )
            assert a["velocity"] == b["velocity"], (
                f"{label}: velocity {b['velocity']} != {a['velocity']}"
            )

    rt_notes = [
        {"pitch": 62, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 84},
        {"pitch": 69, "start_beats": 0.5, "duration_beats": 0.5, "velocity": 72},
        {"pitch": 65, "start_beats": 1.0, "duration_beats": 1.0, "velocity": 84},
        {"pitch": 60, "start_beats": 3.5, "duration_beats": 0.25, "velocity": 90},
        {"pitch": 61, "start_beats": 4.0, "duration_beats": 0.25, "velocity": 90},
    ]
    pp_rt, _ = _core._notes_to_pp_string(_core._note_dicts(rt_notes))
    back_rt = _core.pp_notes_to_notes(pp_rt)
    _assert_notes_match(rt_notes, back_rt, "sample")
    print(f"  sample: {len(rt_notes)} notes round-trip OK")
    print(f"  pp: {pp_rt[:100]}...")

    sticky_pp = "v84 n/8 D4 1|1 v72 A4 1|1.5,4.5 v84 F4 1|2"
    sticky_expected = [
        {"pitch": 62, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 84},
        {"pitch": 69, "start_beats": 0.5, "duration_beats": 0.5, "velocity": 72},
        {"pitch": 65, "start_beats": 1.0, "duration_beats": 0.5, "velocity": 84},
        {"pitch": 69, "start_beats": 3.5, "duration_beats": 0.5, "velocity": 72},
    ]
    sticky_back = _core.pp_notes_to_notes(sticky_pp)
    _assert_notes_match(sticky_expected, sticky_back, "sticky/comma")
    print(f"  sticky/comma: {len(sticky_expected)} notes parsed OK")
    print(f"  pp: {sticky_pp}")

    acc_pp = "v80 Db4 1|1 n/4 A#3 2|3 v75 Eb5 2|4"
    acc_back = _core.pp_notes_to_notes(acc_pp)
    assert len(acc_back) == 3
    assert acc_back[0]["pitch"] == _core._pitch_name_to_midi("Db4")
    assert acc_back[1]["pitch"] == _core._pitch_name_to_midi("A#3")
    assert acc_back[2]["pitch"] == _core._pitch_name_to_midi("Eb5")
    assert acc_back[1]["duration_beats"] == 1.0
    print(f"  accidentals: Db4/A#/Eb5 pitches OK")

    # live reads of HUMAN-PLAYED clips carry fractional note-value numerators
    frac_pp = "v90 n0.9994/4 C4 1|2+n0.4997/4 n/8 E4 2|1-n0.25/4"
    frac_back = _core.pp_notes_to_notes(frac_pp)
    assert len(frac_back) == 2, frac_back
    assert abs(frac_back[0]["start_beats"] - 1.4997) < 1e-6, frac_back[0]
    assert abs(frac_back[0]["duration_beats"] - 0.9994) < 1e-6, frac_back[0]
    assert abs(frac_back[1]["start_beats"] - 3.75) < 1e-6, frac_back[1]
    print("  fractional n-numerators (unquantized live reads) parse OK")
    print("  OK\n")

    # 10a2. finish_my_idea bundle
    print("--- finish_my_idea(seed) ---")
    fmi = _core.finish_my_idea(sample_notes, target_minutes=4, tempo=120)
    for key in ("seed_summary", "critique", "continuation_brief", "structure", "guidance"):
        assert key in fmi, f"missing key: {key}"
    assert fmi["structure"]["sections"], "structure.sections empty"
    assert fmi["continuation_brief"]["variations_requested"] == 3
    print(_pp({
        "seed_key": fmi["seed_summary"].get("key"),
        "critique_verdict": fmi["critique"].get("verdict"),
        "variations": fmi["continuation_brief"]["variations_requested"],
        "sections": len(fmi["structure"]["sections"]),
        "guidance": fmi["guidance"][:120],
    }, 700))
    print("  OK\n")

    # 10b. send_to_ableton arrangement path
    print("--- send_to_ableton(arrangement) ---")
    arr_handoff = _core.send_to_ableton(
        sample_notes,
        track=2,
        clip_name="arr_drop",
        tempo=120,
        bar_length=8,
        arrangement_start="17|1",
    )
    arr_args = arr_handoff["tool_calls"][-1]["arguments"]
    assert arr_handoff["placement"] == "arrangement"
    assert arr_args.get("path") == "t2" and "trackIndex" not in arr_args
    assert arr_args.get("arrangementStart") == "17|1"
    assert "slot" not in arr_args
    assert _re.match(r"^\d+bar$", arr_args["length"])
    print(f"  placement={arr_handoff['placement']}, length={arr_args['length']}")
    print("  OK\n")

    print("  OK\n")

    # 10d. develop_idea — theme & variations
    print("--- develop_idea(seed phrase) ---")
    dev_seed = [
        {"pitch": 57, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 80},
        {"pitch": 60, "start_beats": 0.5, "duration_beats": 0.5, "velocity": 78},
        {"pitch": 62, "start_beats": 1.0, "duration_beats": 0.5, "velocity": 76},
        {"pitch": 64, "start_beats": 1.5, "duration_beats": 0.5, "velocity": 74},
        {"pitch": 65, "start_beats": 2.0, "duration_beats": 1.0, "velocity": 82},
        {"pitch": 62, "start_beats": 3.0, "duration_beats": 0.5, "velocity": 70},
        {"pitch": 60, "start_beats": 3.5, "duration_beats": 0.5, "velocity": 68},
    ]
    dev = _core.develop_idea(dev_seed, tempo=120)
    vars_ = dev["variations"]
    assert len(vars_) >= 6, f"expected >=6 variations, got {len(vars_)}"
    theme_int = _core._normalize_phrase(_core._note_dicts(vars_["theme"]))
    for label, notes in vars_.items():
        nd = _core._normalize_phrase(_core._note_dicts(notes))
        sim = _core._skyline_cosine(theme_int, nd)
        exact = _core._notes_exactly_equal(theme_int, nd)
        if label != "theme":
            assert 0.0 < sim < 1.0, (
                f"{label}: skyline cosine {sim} not in (0,1) exclusive"
            )
            assert not exact, f"{label}: must differ from theme"
        else:
            assert sim >= 0.99, f"theme self-similarity {sim}"
        print(f"  {label:<14} n={len(notes):>2}  skyline_sim={sim:.3f}")
    thin_n = len(vars_["thin"])
    theme_n = len(vars_["theme"])
    thick_n = len(vars_["thicken"])
    assert thin_n < theme_n < thick_n, (
        f"note counts: thin={thin_n} theme={theme_n} thicken={thick_n}"
    )
    assert thick_n <= theme_n * 1.6 + 0.5, (
        f"thicken too dense: {thick_n} notes vs theme {theme_n} "
        f"(ratio {thick_n / theme_n:.2f}, max ~1.6×)"
    )
    theme_span = _core._phrase_span(theme_int)
    half_span = _core._phrase_span(_core._note_dicts(vars_["half_time"]))
    assert abs(half_span - 2 * theme_span) < 0.5, (
        f"half_time span {half_span} not ~2x theme {theme_span}"
    )
    theme_pcs = _core._pitch_class_set(theme_int)
    reh_pcs = _core._pitch_class_set(_core._note_dicts(vars_["reharmonize"]))
    assert reh_pcs != theme_pcs, f"reharmonize pcs {reh_pcs} == theme {theme_pcs}"
    print("  OK\n")

    # 10c. arrange_song — full-song layout from loop parts
    print("--- arrange_song(3 parts, ~3 min) ---")
    import re as _re_arr

    loop_notes = {
        "drums": [
            {"pitch": 36, "start_beats": 0.0, "duration_beats": 0.25, "velocity": 100},
            {"pitch": 36, "start_beats": 1.0, "duration_beats": 0.25, "velocity": 95},
            {"pitch": 38, "start_beats": 2.0, "duration_beats": 0.25, "velocity": 90},
        ],
        "bass": [
            {"pitch": 36, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 85},
            {"pitch": 39, "start_beats": 2.0, "duration_beats": 1.0, "velocity": 80},
        ],
        "pad": [
            {"pitch": 48, "start_beats": 0.0, "duration_beats": 4.0, "velocity": 60},
            {"pitch": 55, "start_beats": 0.0, "duration_beats": 4.0, "velocity": 55},
        ],
        "lead": [
            {"pitch": 60, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 78},
            {"pitch": 63, "start_beats": 1.0, "duration_beats": 0.5, "velocity": 75},
            {"pitch": 67, "start_beats": 2.0, "duration_beats": 1.0, "velocity": 82},
        ],
    }
    parts = [
        {"role": "drums", "notes": loop_notes["drums"], "loop_bars": 4},
        {"role": "bass", "notes": loop_notes["bass"], "loop_bars": 4},
        {"role": "pad", "notes": loop_notes["pad"], "loop_bars": 4},
        {"role": "lead", "notes": loop_notes["lead"], "loop_bars": 4},
    ]
    song = _core.arrange_song(parts, target_minutes=3.5, tempo=120)
    bar_sum = sum(s["bars"] for s in song["sections"])
    assert bar_sum == song["total_bars"], (
        f"section bars {bar_sum} != total_bars {song['total_bars']}"
    )
    for pl in song["placements"]:
        assert _re_arr.match(r"^\d+bar$", pl["length_token"]), pl["length_token"]
        assert _re_arr.match(r"^\d+\|\d", pl["arrangement_start_bar_beat"]), (
            pl["arrangement_start_bar_beat"]
        )

    breakdown_secs = [s for s in song["sections"] if s.get("role") == "breakdown"]
    drop_secs = [
        s for s in song["sections"]
        if s.get("role") not in ("breakdown", "outro")
        and float(s.get("energy", 0)) >= _core._DROP_ENERGY
    ]
    assert breakdown_secs, "expected at least one breakdown section in ~3 min song"
    assert drop_secs, "expected at least one high-energy section"
    bd_roles = len(breakdown_secs[0]["active_roles"])
    drop_roles = max(len(s["active_roles"]) for s in drop_secs)
    assert drop_roles > bd_roles, (
        f"drop should activate more roles ({drop_roles}) than breakdown ({bd_roles})"
    )

    devs_used = {d for ds in song.get("devices", {}).values() for d in ds}
    assert {"buildup", "drop"} <= devs_used, f"expected buildup+drop devices, got {devs_used}"
    assert "reprise" in devs_used, f"expected a reprise after the breakdown, got {devs_used}"

    print(f"  total_bars={song['total_bars']}, sections={len(song['sections'])}, "
          f"placements={len(song['placements'])}")
    for sec in song["sections"]:
        print(f"    §{sec['index']:>2} {sec['role']:<10} bars {sec['bar_start']:>3}-{sec['bar_end']:<3} "
              f"E={sec['energy']:.2f}  roles={sec['active_roles']}"
              f"{('  lead=' + sec['lead_variation']) if sec.get('lead_variation') else ''}")
        if sec["role"] in ("intro", "breakdown", "outro"):
            assert "lead" not in sec["active_roles"], (
                f"lead should not be forced into {sec['role']} section {sec['index']}"
            )

    lead_placements = [p for p in song["placements"] if p["role"] == "lead"]
    lead_vars_used = {p.get("variation") for p in lead_placements if p.get("variation")}
    assert len(lead_vars_used) >= 2, (
        f"expected >=2 distinct lead variations, got {lead_vars_used}"
    )
    lead_seq = [p.get("variation") for p in lead_placements]
    run = 1
    for i in range(1, len(lead_seq)):
        if lead_seq[i] == lead_seq[i - 1]:
            run += 1
            assert run <= 2, (
                f"lead variation run >2 at placements[{i - run + 1}:{i + 1}]: "
                f"{lead_seq[i - run + 1:i + 1]}"
            )
        else:
            run = 1
    for role in ("bass", "drums", "pad"):
        role_vars = {
            p.get("variation") for p in song["placements"]
            if p["role"] == role and p.get("variation")
        }
        assert len(role_vars) >= 2, (
            f"expected >=2 distinct {role} variations, got {role_vars}"
        )
    peak_sec = song["sections"][song["peak_section_index"] - 1]
    peak_var = peak_sec.get("lead_variation")
    assert peak_var in ("theme", "thicken"), (
        f"peak section lead_variation={peak_var!r}, expected theme or thicken"
    )
    assert song["peak_lead_variation"] == "thicken", (
        f"peak_lead_variation={song['peak_lead_variation']!r}"
    )
    print("  OK\n")

    # 11. critique — off-style phrase must yield actionable suggestions
    print("--- critique(off-style phrase) ---")
    off_style = [
        {"pitch": 100, "start_beats": 0.37, "duration_beats": 0.33, "velocity": 90},
        {"pitch": 101, "start_beats": 0.71, "duration_beats": 0.33, "velocity": 90},
        {"pitch": 102, "start_beats": 1.19, "duration_beats": 0.33, "velocity": 90},
        {"pitch": 103, "start_beats": 1.63, "duration_beats": 0.33, "velocity": 90},
        {"pitch": 104, "start_beats": 2.11, "duration_beats": 0.33, "velocity": 90},
        {"pitch": 105, "start_beats": 2.59, "duration_beats": 0.33, "velocity": 90},
        {"pitch": 106, "start_beats": 3.07, "duration_beats": 0.33, "velocity": 90},
    ]
    crit = _core.critique(off_style, tempo=120)
    assert crit["suggestions"], f"expected suggestions, got: {crit}"
    assert crit["overall"] < 0.75, f"off-style phrase should not score high: {crit['overall']}"
    assert any("register" in s.lower() or "groove" in s.lower() or "leap" in s.lower()
               for s in crit["suggestions"]), crit["suggestions"]
    crit_known = _core.critique(off_style, tempo=120, role="lead", key="D minor")
    assert crit_known["detected_key"] == "D minor", "critique must honor known key"
    print(_pp(crit, 900))
    print("  OK\n")

    # 12. continue_clip — seed summary + example continuations
    print("--- continue_clip(seed phrase, bars=4) ---")
    seed_phrase = phrases[0]["notes"]
    cont = _core.continue_clip(seed_phrase, bars=4, tempo=120)
    assert cont["example_continuations"], "expected >=1 example continuation"
    assert len(cont["example_continuations"]) >= 1
    ss = cont["seed_summary"]
    assert ss["key"] and " " in ss["key"], f"implausible key: {ss['key']}"
    assert ss["end_beat"] > 0 and ss["end_pitch"]
    print(f"  key={ss['key']}, end_beat={ss['end_beat']}, "
          f"examples={len(cont['example_continuations'])}")
    print(_pp({"seed_summary": ss, "guidance": cont["guidance"][:200]}, 700))
    print("  OK\n")

    # 13. rate → get_ratings round-trip (temp file)
    print("--- rate() + get_ratings() round-trip ---")
    assert not os.path.exists(ratings_tmp) or _core.get_ratings()["total"] == 0
    rated = _core.rate("up", phrase_id=phrases[0]["id"], label="selftest_like")
    summary = _core.get_ratings()
    assert summary["counts"]["up"] >= 1
    assert summary["total"] >= 1
    assert any(r.get("label") == "selftest_like" for r in summary["recent"])
    assert rated["record"]["rating"] == 1
    _idem_rate13 = _core.rate(
        "up", phrase_id=phrases[0]["id"], label="selftest_idempotent",
        idempotency_key="selftest:rate:one",
    )
    _idem_rate13_repeat = _core.rate(
        "up", phrase_id=phrases[0]["id"], label="selftest_idempotent",
        idempotency_key="selftest:rate:one",
    )
    assert _idem_rate13_repeat["idempotent"] is True
    try:
        _core.rate(
            "down", phrase_id=phrases[0]["id"], label="selftest_idempotent",
            idempotency_key="selftest:rate:one",
        )
        raise AssertionError("one idempotency key must not accept conflicting rating content")
    except ValueError:
        pass
    _core.rate(
        "up", phrase_id=phrases[0]["id"], notes=phrases[0]["notes"], label="gold",
        idempotency_key="selftest:rate:gold-reconcile",
    )
    if os.path.exists(_core._taste_model_path()):
        os.remove(_core._taste_model_path())
    _gold_retry13 = _core.rate(
        "up", phrase_id=phrases[0]["id"], notes=phrases[0]["notes"], label="gold",
        idempotency_key="selftest:rate:gold-reconcile",
    )
    assert _gold_retry13["idempotent"] is True and os.path.exists(_core._taste_model_path()), \
        "an idempotent gold retry must rebuild missing derived model state"
    assert sum(r.get("idempotency_key") == "selftest:rate:one"
               for r in _core._load_ratings_file()["ratings"]) == 1
    print(_pp(summary, 500))
    print("  OK\n")

    # 14. prefer_liked re-ranks by similarity to up-rated vectors (must reorder)
    print("--- find_phrases(prefer_liked=True) reorder proof ---")
    _core._save_ratings_file({"ratings": []})
    sys.path.insert(0, os.path.join(ROOT, "analyze"))
    from build_index import cosine  # noqa: E402

    all_phrases = _core._phrases()
    by_id = {p["id"]: p for p in all_phrases}
    triple = None
    k_search = 12
    w_up = 0.4
    for seed_p in all_phrases:
        if seed_p["kind"] != "harmonic" or seed_p["n_notes"] < 4:
            continue
        seed_notes = seed_p["notes"][:8]
        plain_probe = _core.find_phrases(
            seed_notes=seed_notes, kind="harmonic", k=k_search, prefer_liked=False,
        )
        if len(plain_probe) < 3:
            continue
        result_ids = {r["id"] for r in plain_probe}
        for rank_a in range(1, len(plain_probe)):
            a_rec = plain_probe[rank_a]
            b_rec = plain_probe[rank_a - 1]
            base_a = a_rec["similarity"] or 0.0
            base_b = b_rec["similarity"] or 0.0
            gap = base_b - base_a
            a_p = by_id[a_rec["id"]]
            b_p = by_id[b_rec["id"]]
            for l_p in all_phrases:
                if l_p["kind"] != "harmonic":
                    continue
                if l_p["id"] in result_ids:
                    continue
                if l_p["id"] in (a_rec["id"], b_rec["id"]):
                    continue
                sim_a = cosine(a_p["vec"], l_p["vec"])
                sim_b = cosine(b_p["vec"], l_p["vec"])
                if w_up * (sim_a - sim_b) > gap + 0.002:
                    triple = (seed_notes, a_rec["id"], b_rec["id"], l_p["id"],
                              rank_a, sim_a, sim_b, gap)
                    break
            if triple:
                break
        if triple:
            break

    assert triple, (
        "could not find seed/A/B/L where A is more similar to L than B "
        "(prefer_liked reorder selftest needs this triple)"
    )
    seed_notes, aid, bid, lid, plain_rank_a, sim_a_l, sim_b_l, base_gap = triple
    _core.rate("up", phrase_id=lid, label="prefer_liked_rerank_proof")
    plain = _core.find_phrases(
        seed_notes=seed_notes, kind="harmonic", k=k_search, prefer_liked=False,
    )
    liked = _core.find_phrases(
        seed_notes=seed_notes, kind="harmonic", k=k_search, prefer_liked=True,
    )
    plain_ids = [p["id"] for p in plain]
    liked_ids = [p["id"] for p in liked]
    assert aid in plain_ids and aid in liked_ids, (
        f"phrase A must appear in both result sets: aid={aid}"
    )
    new_rank_a = liked_ids.index(aid)
    # invariant, not coincidence (corpus rebuilds reshuffle neighborhoods): liking L
    # must never WORSEN A's rank, and A (similar to L) must outrank B (dissimilar)
    assert new_rank_a <= plain_ids.index(aid), (
        f"prefer_liked must never worsen A's rank: plain={plain_ids.index(aid)} "
        f"liked={new_rank_a} (rated L={lid})"
    )
    assert bid not in liked_ids or new_rank_a < liked_ids.index(bid), (
        f"A (sim {sim_a_l:.3f} to liked L) must outrank B (sim {sim_b_l:.3f}): "
        f"A={new_rank_a}, B={liked_ids.index(bid)}"
    )
    print(f"  seed: {seed_notes[0]['pitch']}… ({len(seed_notes)} notes)")
    print(f"  rated up: {lid}")
    print(f"  A={aid[:40]}…  B={bid[:40]}…")
    print(f"  sim(A,L)={sim_a_l:.3f}  sim(B,L)={sim_b_l:.3f}  base_gap={base_gap:.3f}")
    print(f"  plain order (top 4): {plain_ids[:4]}")
    print(f"  liked order (top 4): {liked_ids[:4]}")
    print(f"  A rank: plain={plain_ids.index(aid)} -> liked={new_rank_a}")
    print("  OK\n")

    # 15. compose_from_profile — profile-driven generator proof
    print("--- compose_from_profile(profile proof) ---")
    harmony = _core._harmony_data()
    valid_progs = {p for p, _ in harmony.get("progressions", [])}
    agg = _core._aggregate()
    scale_for_key = lambda k: _core.diatonic_set(  # noqa: E731
        _core.PITCH_NAMES.index(k.rsplit(" ", 1)[0]),
        k.rsplit(" ", 1)[1],
    )
    reg_lo, reg_hi = agg.get("pitch_register", [36, 85])
    ref_groove = _core._normalized_weights(
        agg.get("harmonic_groove", [1.0] * 16),
    )

    _notes_orig = _core._read_notes_md
    _core._read_notes_md = lambda: ""  # isolate density mechanism from NOTES.md bias
    samples: dict[str, dict] = {}
    for dens in ("minimal", "mid", "full"):
        out = _core.compose_from_profile(bars=8, density=dens, seed=42)
        samples[dens] = out
        meta = out["meta"]
        lead = out["parts"]["lead"]
        assert meta["progression"] in valid_progs, (
            f"{dens}: progression {meta['progression']!r} not in harmony.json"
        )
        key_str = meta["key"]
        pcs = scale_for_key(key_str)
        in_scale = _core._in_scale_fraction(lead, pcs)
        assert in_scale == 1.0, f"{dens}: lead in-scale {in_scale:.1%} != 100%"
        assert meta["lead_groove_cosine"] > 0.5, (
            f"{dens}: groove cosine {meta['lead_groove_cosine']}"
        )
        for part_name in ("pad", "bass", "lead"):
            for n in out["parts"][part_name]:
                p = int(n["pitch"])
                assert reg_lo <= p <= reg_hi, (
                    f"{dens}/{part_name}: pitch {p} outside [{reg_lo},{reg_hi}]"
                )
        print(
            f"  {dens}: lead={meta['note_counts']['lead']} "
            f"prog={meta['progression']} "
            f"in_scale={meta['lead_in_scale_pct']}% "
            f"groove_cos={meta['lead_groove_cosine']}"
        )

    n_min = samples["minimal"]["meta"]["note_counts"]["lead"]
    n_full = samples["full"]["meta"]["note_counts"]["lead"]
    assert n_min < n_full, (
        f"minimal lead ({n_min}) must be strictly fewer than full ({n_full})"
    )
    assert samples["minimal"]["meta"]["note_counts"]["lead"] <= samples["mid"]["meta"]["note_counts"]["lead"] <= samples["full"]["meta"]["note_counts"]["lead"], (
        "density must be monotonic: minimal <= mid <= full lead counts"
    )

    det_a = _core.compose_from_profile(bars=4, seed=7)
    det_b = _core.compose_from_profile(bars=4, seed=7)
    assert det_a["parts"]["lead"] == det_b["parts"]["lead"], "seed must be deterministic"
    print(f"  monotonic lead counts: minimal={n_min} mid="
          f"{samples['mid']['meta']['note_counts']['lead']} full={n_full}")

    # Drum density must govern hit count (not raw phrase tiling).
    drum_counts = {
        d: len(samples[d]["parts"]["drums"]) for d in ("minimal", "mid", "full")
    }
    assert drum_counts["minimal"] < drum_counts["mid"] < drum_counts["full"], (
        f"drum density not monotonic: {drum_counts}"
    )
    assert drum_counts["mid"] < 64, (
        f"mid 8-bar drums ({drum_counts['mid']}) must stay under 64 hits"
    )
    print(f"  drum counts (8 bars): {drum_counts}")

    # Lead rejection sampling: mid density scores + melody_shape interval mix.
    ms = agg.get("melody_shape", {})
    target_step_ratio = ms["pct_steps"] / (ms["pct_steps"] + ms["pct_leaps"])
    for lead_seed in (7, 42):
        out = _core.compose_from_profile(bars=8, density="mid", seed=lead_seed)
        lead_score = out["meta"].get(
            "lead_style_score",
            _core.score_phrase(out["parts"]["lead"])["overall"],
        )
        assert lead_score >= 0.72, (
            f"seed={lead_seed} mid lead style score {lead_score} < 0.72"
        )
        nd = _core._note_dicts(out["parts"]["lead"])
        actual_steps = _core._phrase_style_analysis(nd)["raw"]["actual_steps"]
        assert abs(actual_steps - target_step_ratio) <= 0.12, (
            f"seed={lead_seed} step ratio {actual_steps:.3f} vs "
            f"profile {target_step_ratio:.3f}"
        )
        print(
            f"  lead seed={lead_seed}: score={lead_score:.3f} "
            f"step_ratio={actual_steps:.3f} (target {target_step_ratio:.3f})"
        )
    _core._read_notes_md = _notes_orig
    print("  OK\n")

    # 16. gesture-vocabulary lead — motifs artifact + gesture_fit discrimination
    print("--- gesture-vocabulary lead ---")
    _gflag = _core._USE_GESTURE_LEAD
    _core._USE_GESTURE_LEAD = True  # exercise the parked gesture engine in 16+17
    motifs = _core._motifs_data()
    assert motifs.get("cells"), "store/motifs.json empty/missing (run analyze/motifs.py)"
    for gkey in ("D minor", "C major"):
        gout = _core.compose_from_profile(bars=8, density="mid", seed=11, key=gkey)
        glead = gout["parts"]["lead"]
        gfrac = _core._in_scale_fraction(glead, scale_for_key(gkey))
        assert gfrac == 1.0, f"{gkey}: gesture lead in-scale {gfrac:.1%} != 100%"
        gscore = _core.score_phrase(glead)["scores"].get("gesture_fit")
        assert gscore is not None and 0.0 <= gscore <= 1.0, (
            f"{gkey}: gesture_fit {gscore} out of [0,1]"
        )
    # gesture_fit must reward his vocabulary far above random/chromatic shapes
    real = [{"pitch": p, "start_beats": i * 0.5, "duration_beats": 0.5, "velocity": 80}
            for i, p in enumerate([62, 65, 67, 69, 67, 65, 69, 62])]
    rand = [{"pitch": p, "start_beats": i * 0.5, "duration_beats": 0.5, "velocity": 80}
            for i, p in enumerate([61, 64, 66, 70, 63, 68, 71, 60])]
    gf_real = _core.score_phrase(real)["scores"]["gesture_fit"]
    gf_rand = _core.score_phrase(rand)["scores"]["gesture_fit"]
    assert gf_real > gf_rand, f"gesture_fit real {gf_real} !> random {gf_rand}"
    print(f"  cells={len(motifs['cells'])}  gesture_fit real={gf_real:.3f} "
          f"random={gf_rand:.3f}  (in-scale minor+major OK)")
    print("  OK\n")

    # 17. NOTES.md gesture dials measurably shift class usage
    print("--- NOTES.md gesture dials ---")
    from motifs import classify as _classify, _intervals as _ivs  # analyzer on sys.path

    def _wobble_count(lead):
        nd = _core._note_dicts(lead)
        top: dict = {}
        for n in sorted(nd, key=lambda n: n["start"]):
            s = round(n["start"] * 4)
            if s not in top or n["pitch"] > top[s]["pitch"]:
                top[s] = n
        degs = [(top[k]["pitch"] - _core.PITCH_NAMES.index("D")) % 12 for k in sorted(top)]
        return sum(1 for a, b, c in zip(degs, degs[1:], degs[2:])
                   if _classify((a, b, c), _ivs((a, b, c))) == "wobble")

    _orig_notes = _core._read_notes_md
    try:
        _core._read_notes_md = lambda: "wobble: hard\nclimb: off\nfall: off\ndark-landing: off\nrepeat: off"
        lead_w = _core.compose_from_profile(bars=8, density="mid", seed=5, key="D minor")["parts"]["lead"]
        _core._read_notes_md = lambda: "climb: hard\nwobble: off\nfall: off\ndark-landing: off\nrepeat: off"
        lead_c = _core.compose_from_profile(bars=8, density="mid", seed=5, key="D minor")["parts"]["lead"]
    finally:
        _core._read_notes_md = _orig_notes
    w_wob, c_wob = _wobble_count(lead_w), _wobble_count(lead_c)
    assert lead_w != lead_c, "NOTES.md dials produced identical leads"
    assert w_wob > c_wob, f"wobble dial ineffective: wobble-dialed {w_wob} !> climb-dialed {c_wob}"
    print(f"  wobble 3-grams: wobble-dialed={w_wob}  climb-dialed={c_wob}  (dials shift usage)")
    print("  OK\n")
    _core._USE_GESTURE_LEAD = _gflag

    # 18. role labeling separates lead/bass/pad; per-role grammar present; config consolidation sanity
    print("--- role labeling ---")
    from roles import (
        ROLE_SOURCE as _ROLE_SOURCE,
        ROLE_THRESHOLDS as _ROLE_THRESHOLDS,
        ROLES as _ROLES,
        clip_role as _clip_role,
        clip_role_report as _clip_role_report,
    )
    import config as _cfg

    # (1) Config import sanity: moved constants present and identical
    assert _cfg.PP_URL == _core.PP_URL
    assert _cfg.BEATS_PER_BAR == _core.BEATS_PER_BAR == 4
    assert _cfg.SECTION_BUCKETS == _core.SECTION_BUCKETS == (8, 4, 16, 2, 32, 64)
    assert _cfg._ROLE_ENERGY_THRESHOLD == _core._ROLE_ENERGY_THRESHOLD
    assert _cfg._ROLE_LAYER_ORDER == _core._ROLE_LAYER_ORDER
    assert _cfg._DROP_ENERGY == _core._DROP_ENERGY == 0.70
    assert _cfg._DENSITY_PARAMS == _core._DENSITY_PARAMS
    assert _cfg._DRUM_DENSITY_TARGETS == _core._DRUM_DENSITY_TARGETS
    assert _cfg.ROLE_THRESHOLDS == _core.ROLE_THRESHOLDS == _ROLE_THRESHOLDS
    assert _cfg.ROLE_SOURCE == _core.ROLE_SOURCE == _ROLE_SOURCE == "rules_v1"
    assert _cfg.ROLES == _core.ROLES == _ROLES == ("lead", "bass", "pad", "other")

    # (2) Synthetic clips battery (~11 clips: bass, lead, pad, other, drum, edge cases)
    synthetic_battery = [
        # (name, notes, is_drum, expected_role)
        ("drum_flag", [], True, "drum"),
        ("empty_clip", [], False, "other"),
        ("single_note", [{"pitch": 60, "start": 0.0, "duration": 1.0}], False, "other"),
        ("bass_low_mono", [{"pitch": 36, "start": 0.0}, {"pitch": 38, "start": 1.0}], False, "bass"),
        ("bass_boundary_47", [{"pitch": 47, "start": 0.0}, {"pitch": 47, "start": 1.0}], False, "bass"),
        ("lead_mid_mono", [{"pitch": 60, "start": 0.0}, {"pitch": 64, "start": 1.0}], False, "lead"),
        ("lead_boundary_55", [{"pitch": 55, "start": 0.0}, {"pitch": 55, "start": 1.0}], False, "lead"),
        ("pad_stacked_chords", [{"pitch": 60, "start": 0.0}, {"pitch": 64, "start": 0.0}, {"pitch": 67, "start": 0.0},
                                {"pitch": 62, "start": 1.0}, {"pitch": 65, "start": 1.0}, {"pitch": 69, "start": 1.0}], False, "pad"),
        ("pad_low_poly", [{"pitch": 36, "start": 0.0}, {"pitch": 40, "start": 0.0}, {"pitch": 43, "start": 0.0}], False, "pad"),
        ("other_ambiguous_mid", [{"pitch": 50, "start": 0.0}, {"pitch": 52, "start": 1.0}], False, "other"),
        ("edge_disabled_notes", [{"pitch": 60, "start": 0.0, "enabled": True}, {"pitch": 64, "start": 1.0, "enabled": True},
                                 {"pitch": 20, "start": 0.0, "enabled": False}], False, "lead"),
    ]

    for name, s_notes, s_is_drum, s_exp in synthetic_battery:
        role_out = _clip_role(s_notes, is_drum=s_is_drum)
        rep = _clip_role_report(s_notes, is_drum=s_is_drum)
        assert role_out == s_exp, f"clip_role({name}) = {role_out!r}, expected {s_exp!r}"
        assert rep["role"] == s_exp, f"clip_role_report({name}) role = {rep['role']!r}, expected {s_exp!r}"
        assert role_out == rep["role"], f"byte-compatibility mismatch for {name}"
        assert 0.0 <= rep["confidence"] <= 1.0, f"invalid confidence {rep['confidence']} for {name}"
        assert isinstance(rep["reason"], str) and len(rep["reason"]) > 0, f"missing reason for {name}"
        assert rep.get("role_source") == "rules_v1", f"wrong role_source in report for {name}"

    bass_clip = [{"pitch": 36 + (i % 3), "start_beats": i * 0.5, "duration_beats": 0.5,
                  "velocity": 80} for i in range(8)]
    lead_clip = [{"pitch": 67 + (i % 4), "start_beats": i * 0.5, "duration_beats": 0.5,
                  "velocity": 80} for i in range(8)]
    pad_clip = [{"pitch": p, "start_beats": (i // 3) * 1.0, "duration_beats": 1.0,
                 "velocity": 70} for i, p in enumerate([60, 64, 67, 62, 65, 69, 60, 64, 67])]
    assert _clip_role(bass_clip) == "bass", f"bass misclassified: {_clip_role(bass_clip)}"
    assert _clip_role(lead_clip) == "lead", f"lead misclassified: {_clip_role(lead_clip)}"
    assert _clip_role(pad_clip) == "pad", f"pad misclassified: {_clip_role(pad_clip)}"
    by_role = _core._aggregate().get("melodic_grammar_by_role", {})
    assert by_role.get("lead", {}).get("unigram"), "no lead-specific grammar in profile"

    # (3) _role_rounds filtering helper test
    test_rounds = [
        {"id": "r1", "role": "lead", "best": 0},
        {"id": "r2", "role": "bass", "best": 1},
        {"id": "r3", "role": "pad", "best": 0},
        {"id": "r4", "best": 0},  # legacy missing role -> defaults to lead
        {"id": "r5", "role": "lead", "best": 1},
    ]
    lead_r = _core._role_rounds(test_rounds, "lead")
    bass_r = _core._role_rounds(test_rounds, "bass")
    pad_r = _core._role_rounds(test_rounds, "pad")
    other_r = _core._role_rounds(test_rounds, "other")
    assert [r["id"] for r in lead_r] == ["r1", "r4", "r5"], f"unexpected lead rounds: {lead_r}"
    assert [r["id"] for r in bass_r] == ["r2"], f"unexpected bass rounds: {bass_r}"
    assert [r["id"] for r in pad_r] == ["r3"], f"unexpected pad rounds: {pad_r}"
    assert other_r == [], f"unexpected other rounds: {other_r}"

    print(f"  battery (11 cases) OK; report {rep['reason']!r}; _role_rounds OK; config sanity OK; lead-grammar degrees={len(by_role['lead']['unigram'])}")
    print("  OK\n")

    # 19. taste model: features, deterministic training, cold-start no-op, learns ranking
    print("--- taste model ---")
    import taste as _taste
    flowing = [{"pitch": p, "start_beats": i * 0.5, "duration_beats": 0.5, "velocity": 80}
               for i, p in enumerate([62, 65, 67, 69, 67, 65, 64, 62])]
    static = [{"pitch": 62, "start_beats": i * 0.5, "duration_beats": 0.5, "velocity": 80}
              for i in range(8)]
    ff, fs = _core._taste_features(flowing), _core._taste_features(static)
    assert len(ff) == len(_core._TASTE_FEATURE_NAMES), "taste feature dim mismatch"
    si = _core._TASTE_FEATURE_NAMES.index("staticness")
    assert fs[si] > ff[si], "static line should score higher 'staticness'"
    w = _taste.train([(ff, fs)] * 4, len(ff))
    assert _taste.score(ff, w) > _taste.score(fs, w), "taste did not learn flowing>static"
    assert _taste.train([(ff, fs)] * 4, len(ff)) == w, "taste training not deterministic"
    assert _taste.train([], len(ff)) == [0.0] * len(ff), "no pairs must give zero weights"
    _stale_tm = _core._taste_model_path()   # a mid-suite crash can leave a trained tmp
    if os.path.exists(_stale_tm):           # model behind; never let it shadow later
        os.remove(_stale_tm)                # failures with a bogus cold-start assert
    assert _core._taste_weight() == 0.0, "taste must be a no-op before enough ratings"
    cands4 = [flowing, static,
              [{"pitch": 62 + (i % 5), "start_beats": i * 0.5, "duration_beats": 0.5,
                "velocity": 80} for i in range(8)],
              [{"pitch": 67, "start_beats": i * 0.5, "duration_beats": 0.5,
                "velocity": 80} for i in range(8)]]
    try:
        _core.rate_candidates(cands4, best=0, worst=len(cands4), reason="bad-index")
        raise AssertionError("out-of-range worst index must be rejected")
    except ValueError:
        pass
    res = None
    # identical no-key rounds now replay idempotently (derived key), so each
    # iteration declares itself a distinct recorded event with an explicit key
    for _r19 in range(3):
        res = _core.rate_candidates(cands4, best=0, worst=1, reason="static",
                                    key="D minor", role="lead",
                                    idempotency_key=f"selftest:19:static:{_r19}")
    assert res["pairs"] >= _core._TASTE_MIN_PAIRS, f"expected enough pairs, got {res['pairs']}"
    assert _core._taste_weight() > 0.0, "taste must activate after enough ratings"
    assert _core._taste_score(flowing) > _core._taste_score(static), "taste must rank flowing>static"
    legacy_pairs = _core._taste_pairs_from_rounds(
        _core._load_ratings_file().get("candidate_rounds", [])
    )
    assert len(legacy_pairs) == res["pairs"], "legacy note-list rounds must keep training"
    # candidate schema: rated rounds store key + role + frozen features (provenance)
    _stored = _core._load_ratings_file()["candidate_rounds"][-1]
    assert _stored["key"] == "D minor" and _stored["role"] == "lead", "round must store key+role"
    assert len(_stored["features"]) == len(cands4), "round must store a feature vector per candidate"
    assert _stored["feature_names"] == list(_core._TASTE_FEATURE_NAMES), "round must store feature names"
    assert all(len(fv) == len(_core._TASTE_FEATURE_NAMES) for fv in _stored["features"]), "feature dim"
    _rounds_before_idem = len(_core._load_ratings_file()["candidate_rounds"])
    _core.rate_candidates(
        cands4, best=0, worst=1, reason="idempotent", key="D minor", role="lead",
        idempotency_key="selftest:round:one",
    )
    if os.path.exists(_core._taste_model_path()):
        os.remove(_core._taste_model_path())
    _idem_round_repeat = _core.rate_candidates(
        cands4, best=0, worst=1, reason="idempotent", key="D minor", role="lead",
        idempotency_key="selftest:round:one",
    )
    assert _idem_round_repeat["idempotent"] is True
    assert os.path.exists(_core._taste_model_path()) and _idem_round_repeat["pairs"] > 0, \
        "an idempotent candidate retry must rebuild missing derived model state"
    try:
        _core.rate_candidates(
            cands4, best=1, worst=0, reason="idempotent", key="D minor", role="lead",
            idempotency_key="selftest:round:one",
        )
        raise AssertionError("one idempotency key must not accept the opposite preference")
    except ValueError:
        pass
    assert len(_core._load_ratings_file()["candidate_rounds"]) == _rounds_before_idem + 1
    _cands4_floaty = json.loads(json.dumps(cands4))
    for _notes4f in _cands4_floaty:
        for _note4f in _notes4f:
            _note4f["velocity"] = float(_note4f.get("velocity", 80))
    _idem_round_floaty = _core.rate_candidates(
        _cands4_floaty, best=0, worst=1, reason="idempotent", key="D minor",
        role="lead", idempotency_key="selftest:round:one",
    )
    assert _idem_round_floaty["idempotent"] is True, \
        "a retry whose notes JSON-round-tripped (int vs float fields) must stay idempotent"
    import inspect as _inspect4
    for _tool_wrapper4 in (rate, rate_candidates):
        _wrapped4 = getattr(_tool_wrapper4, "fn", _tool_wrapper4)
        assert "idempotency_key" in _inspect4.signature(_wrapped4).parameters, \
            "the MCP tool surface must expose idempotency_key, not only core"
    # split generation from selection: audition serves UNBIASED candidates (no taste rerank),
    # else the loop just confirms the model's own picks (Codex #2)
    au = _core.audition_leads(bars=8, key="D minor", n=3, seed=0, use_ears=False)
    assert au["taste_applied"] is False, "audition must not pre-apply taste"
    assert len(au["candidates"]) == 3, "audition candidate count"
    assert au["selection_mode"] == "raw_pool_interval_bands"
    assert len(set(au["interval_step_ratios"])) > 1, "audition must expose interval-shape contrast"
    # get_ratings must surface the active taste loop — it used to hide it (Codex #1)
    gr = _core.get_ratings()
    assert "taste_model" in gr and "candidate_rounds" in gr, "get_ratings must expose the taste loop"
    assert gr["taste_model"]["n_pairs"] == _idem_round_repeat["pairs"], \
        "taste_model summary must match the latest idempotent result"
    pairs_before_harmony = gr["taste_model"]["n_pairs"]
    harmony_structured = [
        {"id": "lead-static", "role": "lead", "source": "lead", "key": "D minor", "notes": static},
        {
            "id": "harmony-color",
            "role": "harmony",
            "source": "harmony",
            "key": "D minor",
            "notes": flowing,
            "backing": {"pad": flowing, "bass": static},
        },
    ]
    structured_res = _core.rate_candidates(
        harmony_structured, best=1, worst=0, reason="harmony-color",
        key="D minor", role="lead",
    )
    assert structured_res["pairs"] == pairs_before_harmony, (
        "harmony-color candidate rounds must not add lead taste pairs"
    )
    stored_structured = _core._load_ratings_file()["candidate_rounds"][-1]
    assert stored_structured["candidates"][1]["backing"]["pad"], "structured backing must be preserved"
    assert stored_structured["candidates"][1]["taste_trainable"] is False, "harmony candidate must be segregated"
    print(f"  features={len(ff)}d, flowing>static learned, pairs={res['pairs']}, "
          f"weight={_core._taste_weight():.2f}; audition unbiased; loop visible")
    print("  OK\n")

    # 20. growth suggestions: rare-but-native moves, auditionable + feed the taste loop
    print("--- growth suggestions ---")
    g = _core.suggest_growth(n=3, key="D minor", seed=0, use_ears=False)
    assert g["count"] >= 3, f"expected >=3 growth suggestions, got {g['count']}"
    gpcs = scale_for_key("D minor")
    for s in g["suggestions"]:
        assert s["notes"], f"empty growth suggestion: {s['dimension']}"
        assert _core._in_scale_fraction(s["notes"], gpcs) == 1.0, f"{s['dimension']} not in-scale"
    pj = _core.compose_from_profile(bars=4, key="D minor", seed=1, progression="i→v")
    assert pj["meta"]["progression"] == "i→v", f"progression param ignored: {pj['meta']['progression']}"
    dims = {s["dimension"].split()[0] for s in g["suggestions"]}
    assert {"melody", "harmony"} <= dims, f"growth dims too narrow: {dims}"
    assert all(isinstance(c, dict) and c.get("notes") for c in g["candidates"]), (
        "growth candidates must use structured rating records"
    )
    assert any(c.get("source") == "harmony" and c.get("backing") for c in g["candidates"]), (
        "growth harmony candidate must preserve backing"
    )
    gr = _core.rate_candidates(g["candidates"], best=0, worst=g["count"] - 1, reason="growth-test")
    assert gr["ok"], "growth candidates failed to feed rate_candidates"
    stored_growth = _core._load_ratings_file()["candidate_rounds"][-1]
    assert any(c.get("source") == "harmony" and c.get("taste_trainable") is False
               for c in stored_growth["candidates"]), "growth harmony must be segregated"
    print(f"  {g['count']} suggestions ({', '.join(s['the_move'] for s in g['suggestions'])}); "
          f"all in-scale; fed taste loop")
    print("  OK\n")

    # 21. grow_my_loop: seed-relative growth — extend HIS loop, not standalone snippets
    print("--- grow_my_loop (seed-relative) ---")
    seed_loop = [
        {"pitch": 62, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 80},
        {"pitch": 65, "start_beats": 0.5, "duration_beats": 0.5, "velocity": 76},
        {"pitch": 69, "start_beats": 1.0, "duration_beats": 0.5, "velocity": 78},
        {"pitch": 67, "start_beats": 1.5, "duration_beats": 0.5, "velocity": 74},
        {"pitch": 65, "start_beats": 2.0, "duration_beats": 1.0, "velocity": 72},
        {"pitch": 62, "start_beats": 3.0, "duration_beats": 1.0, "velocity": 70},
    ]
    gl = _core.grow_my_loop(seed_loop, n=3, key="D minor", seed=0)
    assert gl["count"] >= 2, f"expected >=2 seed-relative suggestions, got {gl['count']}"
    glpcs = scale_for_key("D minor")
    seed_len = len(gl["suggestions"][0]["seed_notes"])
    end_beat = gl["seed_summary"]["continuation_starts_beat"]
    seed_end_pitch = seed_loop[-1]["pitch"]
    for s in gl["suggestions"]:
        assert s["continuation"], f"empty continuation: {s['dimension']}"
        # the seed is returned untouched as the prefix of `notes`
        assert s["notes"][:seed_len] == s["seed_notes"], f"seed not preserved: {s['dimension']}"
        # the continuation lands AFTER the seed (bar-aligned handoff)
        cont0 = s["continuation"][0]["start_beats"]
        assert cont0 >= end_beat - 1e-6, \
            f"continuation overlaps seed: {s['dimension']} ({cont0} < {end_beat})"
        if s["dimension"].startswith("melody"):
            assert _core._in_scale_fraction(s["continuation"], glpcs) == 1.0, \
                f"{s['dimension']} continuation not in-scale"
            # voice-leads from the seed's end (no wild jump into the continuation)
            assert abs(s["continuation"][0]["pitch"] - seed_end_pitch) <= 12, \
                f"continuation doesn't voice-lead from seed end: {s['dimension']}"
    dims = {s["dimension"].split()[0] for s in gl["suggestions"]}
    assert {"melody", "harmony"} <= dims, f"grow_my_loop dims too narrow: {dims}"
    assert all(isinstance(c, dict) and c.get("notes") for c in gl["candidates"]), (
        "grow_my_loop candidates must use structured rating records"
    )
    assert any(c.get("source") == "harmony" and c.get("backing") for c in gl["candidates"]), (
        "grow_my_loop harmony candidate must preserve backing"
    )
    glr = _core.rate_candidates(gl["candidates"], best=0, worst=gl["count"] - 1, reason="grow-test")
    assert glr["ok"], "grow_my_loop candidates failed to feed rate_candidates"
    print(f"  {gl['count']} stretches of a {gl['seed_summary']['n_notes']}-note loop; "
          f"seed preserved, continuations from beat {end_beat}; fed taste loop")
    print("  OK\n")

    # 22. IdeaPack: read-clip workflow object + opt-in audition lane handoff
    print("--- IdeaPack from clip (no auto-write) ---")
    pack = _core.create_idea_pack_from_clip(
        seed_loop,
        n=3,
        key="D minor",
        tempo=120,
        cont_bars=4,
        seed=0,
        source_context={"type": "selftest", "slot": "0/0"},
    )
    assert pack["schema_version"] == 1
    assert pack["contrast_axis"] == "mixed"
    assert pack["rating_contract"]["rating_scope"] == "mixed"
    assert pack["rating_contract"]["trainable_model"] == "none"
    assert pack["write_policy"]["auto_write"] is False
    assert pack["write_policy"]["requires_confirm"] is True
    assert pack["write_policy"]["requires_explicit_target"] is True
    assert pack["source_clip"]["source_context"]["slot"] == "0/0"
    assert len(pack["ideas"]) == len(pack["candidates"]) == len(pack["audition_lanes"])
    assert any(c.get("source") == "harmony" and c.get("backing") for c in pack["candidates"])
    mixed_pairs_before = _core._taste_model().get("n_pairs", 0)
    mixed_rating = _core.rate_candidates(
        pack["candidates"],
        best=0,
        worst=len(pack["candidates"]) - 1,
        reason="mixed-pack",
        contrast_axis=pack["contrast_axis"],
    )
    assert mixed_rating["rating_scope"] == "mixed"
    assert mixed_rating["trainable_model"] == "none"
    assert mixed_rating["pairs"] == mixed_pairs_before, "mixed packs must not train lead taste"
    untargeted_preview = _core.prepare_idea_pack_audition_lanes(pack, confirm=False)
    assert untargeted_preview["requires_explicit_target"] is True
    assert untargeted_preview["lanes_preview"][0]["slot"] is None
    try:
        _core.prepare_idea_pack_audition_lanes(pack, confirm=True)
        raise AssertionError("confirm=True without start_track must fail")
    except ValueError as exc:
        assert "start_track" in str(exc)
    preview = _core.prepare_idea_pack_audition_lanes(
        pack,
        confirm=False,
        start_track=1,
        scene_index=2,
    )
    assert preview["requires_confirm"] is True
    assert preview["tool_calls"] == []
    assert preview["lanes_preview"][0]["slot"] == "1/2"
    lane_handoff = _core.prepare_idea_pack_audition_lanes(
        pack,
        confirm=True,
        start_track=1,
        scene_index=2,
    )
    create_calls = [c for c in lane_handoff["tool_calls"] if c["tool"] == "ppal-create-clip"]
    assert lane_handoff["executed"] is False
    assert len(create_calls) == len(pack["ideas"])
    assert create_calls[0]["arguments"]["path"] == "t1/s2"
    assert create_calls[1]["arguments"]["path"] == "t2/s2"
    assert all("slot" not in c["arguments"] for c in create_calls)
    print(f"  pack={pack['id']} ideas={len(pack['ideas'])}; "
          f"confirm-gated calls={len(lane_handoff['tool_calls'])}")
    print("  OK\n")

    # 23. gold anchors + role-filtered REAL-phrase retrieval (synthetic — no corpus dependency)
    print("--- gold anchors + lead-phrase retrieval ---")
    from parse_als import is_melodic_clip_source, is_main_clip_source  # noqa: E402
    assert is_melodic_clip_source("take_lane") is True, "take_lane must feed melodic retrieval"
    assert is_melodic_clip_source("other_track") is False
    assert is_main_clip_source("take_lane") is False, "take_lane stays OUT of the main/style corpus"
    syn = [{"role": "lead", "kind": "harmonic", "n_notes": 4, "project": "p", "tempo": 120, "notes": []},
           {"role": "bass", "kind": "harmonic", "n_notes": 4, "project": "p", "tempo": 120, "notes": []},
           {"role": "lead", "kind": "harmonic", "n_notes": 4, "project": "p", "tempo": 120, "notes": []}]
    leads = _core._filter_phrases(syn, None, None, role="lead")
    assert len(leads) == 2 and all(p["role"] == "lead" for p in leads), "role filter must keep only lead"
    gnotes = [{"pitch": 62, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 80},
              {"pitch": 65, "start_beats": 0.5, "duration_beats": 0.5, "velocity": 78}]
    _core.rate(rating=1, label="gold", notes=gnotes, phrase_id="selftest|gold|1")
    assert any(g.get("phrase_id") == "selftest|gold|1" for g in _core.get_gold_phrases()), "gold must be retrievable"
    _core.rate(rating=-1, label="rejected", notes=gnotes, phrase_id="selftest|gold|1")
    assert not any(g.get("phrase_id") == "selftest|gold|1" for g in _core.get_gold_phrases()), "later reject must un-gold"
    # plain likes with other labels must NOT move gold membership
    gnotes2 = [{"pitch": 57, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 84},
               {"pitch": 60, "start_beats": 0.5, "duration_beats": 1.0, "velocity": 70}]
    _core.rate(rating=1, label="gold", notes=gnotes2, phrase_id="selftest|gold|2")
    _core.rate(rating=1, label="nice", notes=gnotes2, phrase_id="selftest|gold|2")
    assert any(g.get("phrase_id") == "selftest|gold|2" for g in _core.get_gold_phrases()), \
        "a plain thumbs-up must not un-gold"
    # a notes-only un-gold must reconcile with the id-golded record (digest→id alias)
    _core.rate(rating=-1, label="ungold", notes=gnotes2)
    assert not any(g.get("phrase_id") == "selftest|gold|2" for g in _core.get_gold_phrases()), \
        "a notes-only reject must un-gold the id-golded phrase"
    # 'drums'/'drum' alias: both spellings hit the singular index role
    dsyn = [{"role": "drum", "kind": "drum", "n_notes": 4, "project": "p", "tempo": 120, "notes": []}]
    assert len(_core._filter_phrases(dsyn, None, None, role="drums")) == 1, "role='drums' must alias to 'drum'"
    print("  take_lane→melodic-only; role filter keeps lead; gold round-trip + un-gold ok;")
    print("  plain 👍 keeps gold; digest alias un-golds; drums→drum alias ok")
    print("  OK\n")

    # 24. humanize_notes: synthetic profile — deterministic, bounded, strength=0 identity
    print("--- humanize_notes (velocity/timing DNA) ---")
    _saved_agg = _core._CACHE.get("aggregate")
    try:
        _core._CACHE["aggregate"] = {
            "velocity_by_role": {"lead": {"slot_mean": [100.0, 60.0, 74.0, 60.0] * 4,
                                          "slot_std": [4.0] * 16}},
            "timing_by_role": {"lead": {"dev_mean_beats": 0.0, "dev_std_beats": 0.02,
                                        "offgrid_frac": 0.1, "n_notes": 1000}},
            "tempo": {"mean": 120.0},
        }
        gen = [{"pitch": 60 + i, "start_beats": i * 0.25, "duration_beats": 0.25,
                "velocity": 76 if i % 4 == 0 else 68} for i in range(8)]
        h1 = _core.humanize_notes(gen, role="lead", strength=1.0, seed=3)
        h2 = _core.humanize_notes(gen, role="lead", strength=1.0, seed=3)
        assert h1 == h2, "humanize must be deterministic for a fixed seed"
        assert _core.humanize_notes(gen, role="lead", strength=0.0, seed=3) == gen, \
            "strength=0 must be identity"
        assert all(1 <= m["velocity"] <= 127 for m in h1)
        for orig, m in zip(gen, h1):
            assert abs(m["start_beats"] - orig["start_beats"]) <= 0.0601, (orig, m)
        assert h1[0]["start_beats"] == 0.0, "downbeat note must stay anchored"
        assert h1[0]["velocity"] > h1[1]["velocity"], "learned downbeat accent must show"
        real = [{"pitch": 64, "start_beats": 1.0, "duration_beats": 0.5, "velocity": 100}]
        soft = _core.humanize_notes(real, role="lead", seed=1)   # default gentle 0.35
        assert abs(soft[0]["velocity"] - 100) <= 4, "gentle strength must barely move real velocities"
        _core._CACHE["aggregate"] = {"tempo": {"mean": 120.0}}   # profile without DNA keys
        assert _core.humanize_notes(gen, role="lead", strength=1.0, seed=3) == gen, \
            "missing stats must be an honest identity, not an invented contour"
    finally:
        if _saved_agg is None:
            _core._CACHE.pop("aggregate", None)
        else:
            _core._CACHE["aggregate"] = _saved_agg
    print("  deterministic; jitter ≤ 0.06 beats; strength=0 + missing-stats = identity")
    print("  OK\n")

    # 25. answer_my_loop: synthetic pool — junk-gated, transposed, velocity-preserving, no writes
    print("--- answer_my_loop (jam from real phrases) ---")
    from build_index import phrase_vector as _pv  # noqa: E402
    _saved_agg25 = _core._CACHE.get("aggregate")
    _saved_pk25 = _core._CACHE.get("project_keys")
    try:
        _core._CACHE["aggregate"] = {
            "melodic_grammar_by_role": {"lead": {"unigram": {"0": 0.6, "3": 0.2, "7": 0.2},
                                                 "order_1": {}, "order_2": {}}},
            "tempo": {"mean": 120.0},
        }
        _core._CACHE["project_keys"] = {"pG": "G minor"}

        def _idx_phrase(pid, project, role, source, pitches, starts, vels, dur=0.45):
            notes = [{"pitch": p, "start": s, "duration": dur, "velocity": v}
                     for p, s, v in zip(pitches, starts, vels)]
            return {"id": pid, "project": project, "role": role, "kind": "harmonic",
                    "clip_source": source, "tempo": 122.0, "n_notes": len(notes),
                    "notes": notes, "vec": _pv(notes)}

        seed_line = [{"pitch": p, "start_beats": s, "duration_beats": 0.45, "velocity": 90}
                     for p, s in zip([62, 65, 69, 67, 65, 62], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0])]
        _vels = [91, 63, 77, 84, 58, 96]
        pool = [
            _idx_phrase("pG|c0|w0", "pG", "lead", "session",
                        [67, 70, 74, 72, 70, 65], [0.0, 0.5, 1.0, 2.0, 2.5, 3.0], _vels),
            _idx_phrase("junk|roll", "pJ", "lead", "session",
                        [67] * 12, [i * 0.25 for i in range(12)], [80] * 12),
            _idx_phrase("pX|take", "pX", "other", "take_lane",
                        [48, 50, 52, 53, 55, 57, 59, 60],
                        [i * 0.125 for i in range(8)], [70] * 4 + [75] * 4),
            _idx_phrase("pX|bass", "pX", "bass", "session",
                        [38, 41, 43, 45], [0.0, 1.0, 2.0, 3.0], [88, 70, 82, 74]),
            _idx_phrase("echo|seed", "pE", "lead", "session",
                        [62, 65, 69, 67, 65, 62], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0],
                        [90] * 6),
        ]
        _core.set_phrase_pool_for_test(pool)
        _core.rate(rating=1, label="gold", phrase_id="selftest|gold|answer",
                   notes=[{"pitch": 50, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 77},
                          {"pitch": 53, "start_beats": 1.0, "duration_beats": 1.0, "velocity": 69},
                          {"pitch": 55, "start_beats": 2.0, "duration_beats": 1.0, "velocity": 83}])

        ans = _core.answer_my_loop(seed_line, bars=1, key="D minor", seed=0, use_ears=False)
        ans2 = _core.answer_my_loop(seed_line, bars=1, key="D minor", seed=0, use_ears=False)
        assert ans == ans2, "answer_my_loop must be deterministic for a fixed seed"
        assert ans["bank_stats"]["n_gated_mechanical"] >= 1, "the planted roll must be junk-gated"
        assert "junk|roll" not in ans["source_phrase_ids"]
        assert ans["bank_stats"]["n_gold"] == 1, "gold anchor must join the bank"
        assert ans["source_phrase_ids"] == ["pG|c0|w0"], f"expected the G-minor cousin, got {ans['source_phrase_ids']}"
        assert "transpose:-5" in ans["transforms_applied"], ans["transforms_applied"]
        assert abs(ans["join"]["interval"]) <= 7, ans["join"]
        got_vels = [n["velocity"] for n in ans["answer_notes"]][:len(_vels)]
        assert got_vels == _vels, f"real velocities must ride along untouched: {got_vels}"
        assert ans["answer_notes"][0]["start_beats"] < 4.0 and ans["answer_notes"][0]["start_beats"] >= 0.0
        assert "live_write" not in ans, "core answer_my_loop must never write"
        assert ans["source_provenance"]["project"] == "pG"
    finally:
        _core.set_phrase_pool_for_test(None)
        if _saved_agg25 is None:
            _core._CACHE.pop("aggregate", None)
        else:
            _core._CACHE["aggregate"] = _saved_agg25
        if _saved_pk25 is None:
            _core._CACHE.pop("project_keys", None)
        else:
            _core._CACHE["project_keys"] = _saved_pk25
    import live_write as _lw  # noqa: E402  (pure parsers only — no connection)
    assert _lw._max_scene_index_in_track_text(
        'sessionClips:[{id:"9",slot:"5/2"},{id:"12",sceneIndex: 4}]') == 4
    assert _lw._max_scene_index_in_track_text("clipCount:0") is None
    # parser must survive Producer Pal serialization drift: order, spacing, string ids
    assert _lw._track_index_by_name(
        '{id:"65",type:"midi",name:"SIDEKICK JAM",trackIndex:7}', "SIDEKICK JAM") == (7, "65")
    assert _lw._track_index_by_name(
        '{ name : "SIDEKICK JAM" , type: "midi", trackIndex : 9 , id: "track-abc" }',
        "SIDEKICK JAM") == (9, "track-abc")
    assert _lw._track_index_by_name(
        '{trackIndex: 3, id: 12x, name: "SIDEKICK JAM"}', "SIDEKICK JAM")[0] == 3
    assert _lw._track_index_by_name('{id:"2",type:"midi",name:"1-MIDI",trackIndex:0}',
                                    "SIDEKICK JAM") == (None, None)
    # PP 1.4.14 dropped regularTrackCount — the counter must survive both schemas
    assert _lw._regular_track_count("regularTrackCount: 8, tracks:[...]") == 8
    assert _lw._regular_track_count('tracks:[{trackIndex:0},{trackIndex:1},{trackIndex:2}]') == 3
    assert _lw._regular_track_count("") == 0
    print("  roll gated; gold in bank; G→D transpose -5; join ≤ a 5th; velocities preserved; no writes;")
    print("  jam-track slot/name parsers OK")
    print("  OK\n")

    # 26. feel features: pure, deterministic, grid-global, isolated from core
    print("--- feel features (performance, not melody) ---")
    import feel  # noqa: E402
    assert "core" not in feel.__dict__, "feel.py must NEVER import core (isolation contract)"
    _slot_mean26 = [100.0, 60.0, 74.0, 60.0, 100.0, 60.0] + [74.0] * 10
    _mk = lambda s, v, d=0.2: {"pitch": 62, "start": s, "duration": d, "velocity": v}  # noqa: E731
    fnotes = [_mk(0.0, 100, d=0.5), _mk(0.28, 60), _mk(0.5, 74), _mk(0.70, 60),
              _mk(1.0, 100), _mk(1.25, 60)]
    f = feel.feel_features(fnotes, _slot_mean26)
    f2 = feel.feel_features(fnotes, _slot_mean26)
    assert f == f2, "feel_features must be deterministic"
    assert abs(f[0] - 2 / 6) < 1e-6, f"offgrid_frac: {f[0]}"          # 0.28 and 0.70 are off-grid
    assert f[1] > 0 and f[2] < 0, "dev std positive; net drag (later than grid) negative mean"
    assert f[6] > 0.9, f"velocities match the slot contour → strong positive corr, got {f[6]}"
    assert abs(f[7] - 0.2) < 1e-6, f"one overlapping onset-pair of five → legato 0.2, got {f[7]}"
    anti = [_mk(s, v) for s, v in zip([0.0, 0.25, 0.5, 0.75, 1.0, 1.25],
                                      [60, 100, 60, 100, 60, 100])]
    assert feel.feel_features(anti, _slot_mean26)[6] < -0.5, "anti-contour must correlate negative"
    shifted = [{**n, "start": n["start"] - 272.0} for n in fnotes]    # whole bars, negative starts
    assert feel.feel_features(shifted, _slot_mean26) == f, "bar-shift (grid-global) invariance"
    assert feel.feel_features(fnotes[:2], _slot_mean26) == [0.0] * len(feel.FEEL_FEATURE_NAMES), \
        "<3 notes → zeros, not a crash"
    print("  offgrid 2/6; drag sign; contour ±; legato 0.2; bar-shift invariant; core-isolated")
    print("  OK\n")

    # 27. groove templates: synthetic DNA — deterministic, anchored, bounded
    print("--- groove templates (RICARDO FEEL) ---")
    _saved_agg27 = _core._CACHE.get("aggregate")
    try:
        _core._CACHE["aggregate"] = {
            "velocity_by_role": {"lead": {"slot_mean": [100.0, 60.0, 74.0, 60.0] * 4,
                                          "slot_std": [4.0] * 16}},
            "timing_by_role": {"lead": {"dev_mean_beats": 0.0, "dev_std_beats": 0.03,
                                        "offgrid_frac": 0.25, "n_notes": 5000}},
            "tempo": {"mean": 120.0},
        }
        g1 = _core.build_groove_template_notes(role="lead", bars=1, seed=4)
        g2b = _core.build_groove_template_notes(role="lead", bars=2, seed=4)
        assert g1 == _core.build_groove_template_notes(role="lead", bars=1, seed=4), "deterministic"
        assert len(g1) == 16 and len(g2b) == 32
        assert g1[0]["start_beats"] == 0.0, "bar-1 downbeat must stay anchored"
        offgrid_per_bar = [0, 0]
        for n in g2b:
            grid = round(n["start_beats"] * 4) / 4.0
            dev = abs(n["start_beats"] - grid)
            assert dev <= 0.125 + 1e-9, n
            if dev > 1e-9:
                offgrid_per_bar[int(n["start_beats"] // 4)] += 1
        assert offgrid_per_bar == [4, 4], \
            f"round(0.25*16)=4 deviating slots per bar (slot 0 not in the k=4 set), got {offgrid_per_bar}"
        assert [n["velocity"] for n in g1] == [100, 60, 74, 60] * 4, "velocities = slot contour"
        _core._CACHE["aggregate"] = {"tempo": {"mean": 120.0}}
        try:
            _core.build_groove_template_notes(role="lead")
            raise AssertionError("missing DNA must raise, not invent a feel")
        except ValueError:
            pass
    finally:
        if _saved_agg27 is None:
            _core._CACHE.pop("aggregate", None)
        else:
            _core._CACHE["aggregate"] = _saved_agg27
    print("  deterministic; 16/32 notes; downbeat anchored; devs ≤ 0.125; contour velocities")
    print("  OK\n")

    # 28. curve library: synthetic envelope — segmentation, classes, normalization, picks
    print("--- phrase-curve library ---")
    import curves as _curves  # noqa: E402
    fake_auto = {"projects": [{
        "id": "pT", "name": "t.als",
        "automation_tracks": [{"track": "lead", "envelopes": [{
            "target": "MidiTrack/DeviceChain/Mixer/Volume",
            "n_events": 9,
            "events": (
                [[0.0, 0.001], [3.0, 0.15], [6.0, 0.55], [9.0, 0.85], [12.0, 1.0]]   # 12-beat rise
                + [[40.0, 0.3], [42.0, 0.9], [44.0, 0.95], [46.0, 0.85], [48.0, 0.3]]  # 8-beat arc
            ),
        }]}],
    }]}
    lib28 = _curves.build_curve_library(fake_auto)
    assert lib28["n_templates"] == 2, lib28["by_bucket"]
    classes28 = sorted(t["class"] for t in lib28["templates"])
    assert classes28 == ["arc", "rise"], classes28
    assert abs(_curves.normalize_value("volume", 1.0) - 60.0 / 66.0) < 1e-9, "gain 1.0 = 0 dB"
    assert _curves.normalize_value("cutoff", 20.0) == 0.0 and _curves.normalize_value("cutoff", 135.0) == 1.0
    try:
        _curves.set_library_for_test(lib28)
        p1 = _curves.pick_curve("volume", 4, seed=0, character="swell")
        p2 = _curves.pick_curve("volume", 4, seed=0, character="swell")
        assert p1 == p2 and p1["class"] == "rise", "pick_curve must be deterministic + class-true"
        r = _curves.render_curve(p1, bars=2, lo=0.0, hi=0.85)
        assert r[0][0] == 0.0 and len(r) <= 16 and all(0.0 <= v <= 0.85 for _, v in r)
        assert all(b1 < b2 for (b1, _), (b2, _) in zip(r, r[1:])), "times strictly increasing"
        try:
            _curves.pick_curve("nope", 4)
            raise AssertionError("unknown family must raise")
        except ValueError:
            pass
    finally:
        _curves.set_library_for_test(None)
    print("  2 segments -> rise+arc; volume/cutoff norms exact; deterministic pick; bounded render")
    print("  OK\n")

    # 29. envelope bridge: pure codec + capped deterministic plans — zero sockets
    print("--- envelope bridge (phrasing engine, offline half) ---")
    import envelope_bridge as _eb  # noqa: E402
    _big = {"id": 7, "part": [1, 1], "ops": [{"op": "x", "blob": "y" * 1024}]}
    assert _eb.decode_reply(_eb.encode_cmd(_big)) == _big, "codec must round-trip 1KB payloads"
    _addr, _tok = _eb.osc_unpack(_eb.osc_pack("/sk", "abc_-123"))
    assert _addr == "/sk" and _tok == "abc_-123"
    assert [len(c) for c in _eb.chunk_ops([{"op": i} for i in range(100)])] == [48, 48, 4]
    plan29 = _eb.build_write_plan("7/0", [{"family": "volume", "character": "swell"}],
                                  bars=2, seed=1, curves_lib=lib28)
    assert plan29 == _eb.build_write_plan("7/0", [{"family": "volume", "character": "swell"}],
                                          bars=2, seed=1, curves_lib=lib28), "plan determinism"
    assert plan29["policy_op"] == {"op": "assert_track_name", "track_index": 7,
                                   "expect": "SIDEKICK JAM"}, "policy op must lead every plan"
    assert plan29["executed"] is False and plan29["undo"], "dry plan carries undo + executed=False"
    _env29 = plan29["envelopes"][0]
    assert all(v <= 0.85 + 1e-9 for _, _, v in _env29["steps"]), "volume capped at 0.85 (0 dB)"
    _t29 = [t for t, _, _ in _env29["steps"]]
    assert _t29 == sorted(_t29) and _t29[0] == 0.0
    assert all(0.01 <= ln <= 0.25 + 1e-9 for _, ln, _ in _env29["steps"])
    assert _env29["provenance"]["project"] == "t.als", "curves must carry provenance"
    _probe29 = _eb.probe(confirm=False)
    assert _probe29["executed"] is False and len(_probe29["steps"]) == 4, "probe dry = plan only"
    try:
        _eb.build_write_plan("7/0", [{"family": "nope"}], bars=2, curves_lib=lib28)
        raise AssertionError("unknown family must raise")
    except ValueError:
        pass
    print("  codec round-trips; chunk cap 48; deterministic capped plan; policy-op first; probe dry")
    print("  OK\n")

    # 30. song map: synthetic skeleton — vocabulary names, locator times, determinism
    print("--- song map (locators-first) ---")
    _orig_ss = _core.suggest_structure

    def _fake_ss(target_minutes=None, tempo=None, seed=0):
        return {"tempo": tempo or 120.0, "total_bars": 32, "total_minutes": 1.07,
                "breakdown_likely": True,
                "sections": [
                    {"index": 1, "bars": 8, "bar_start": 0, "bar_end": 8,
                     "density": 3, "energy": 0.4, "role": "intro"},
                    {"index": 2, "bars": 8, "bar_start": 8, "bar_end": 16,
                     "density": 6, "energy": 0.7, "role": "section"},
                    {"index": 3, "bars": 4, "bar_start": 16, "bar_end": 20,
                     "density": 9, "energy": 0.95, "role": "section"},
                    {"index": 4, "bars": 4, "bar_start": 20, "bar_end": 24,
                     "density": 3, "energy": 0.35, "role": "breakdown"},
                    {"index": 5, "bars": 4, "bar_start": 24, "bar_end": 28,
                     "density": 8, "energy": 0.9, "role": "section"},
                    {"index": 6, "bars": 4, "bar_start": 28, "bar_end": 32,
                     "density": 2, "energy": 0.3, "role": "outro"},
                ]}

    _core.suggest_structure = _fake_ss
    try:
        m30 = _core.plan_song_map(tempo=120.0)
        names30 = [s["name"] for s in m30["sections"]]
        assert names30 == ["INTRO", "BUILD", "DROP", "BREATHE", "REPRISE", "OUT"], names30
        assert [s["locator_time"] for s in m30["sections"]] == \
            ["1|1", "9|1", "17|1", "21|1", "25|1", "29|1"]
        assert m30["locators"][-1] == {"time": "33|1", "name": "END"}, "terminal END marker"
        assert m30 == _core.plan_song_map(tempo=120.0), "map must be deterministic"
        assert all(any(s["name"].startswith(v) for v in _core._MAP_VOCAB)
                   for s in m30["sections"]), "names must stay in the fixed vocabulary"
        _seed30 = [{"pitch": 62, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 90},
                   {"pitch": 65, "start_beats": 1.0, "duration_beats": 1.0, "velocity": 80},
                   {"pitch": 69, "start_beats": 2.0, "duration_beats": 1.0, "velocity": 85}]
        m30b = _core.plan_song_map(seed_notes=_seed30, tempo=120.0)
        assert m30b["key"], "seed must yield a detected key"
        assert m30b["assumptions"], "detected key must be disclosed as an assumption"
    finally:
        _core.suggest_structure = _orig_ss
    print("  INTRO/BUILD/DROP/BREATHE/REPRISE/OUT named from the arc; times exact; deterministic")
    print("  OK\n")

    # 31. gearbox: recall verbatim, surgery, composed-contracts + critics gate
    print("--- gearbox (gears 1/3/5/6 synthetic) ---")
    import gears as _gears  # noqa: E402
    _saved_agg31 = _core._CACHE.get("aggregate")
    _saved_pk31 = _core._CACHE.get("project_keys")
    try:
        _core._CACHE["aggregate"] = {
            "melodic_grammar_by_role": {"lead": {"unigram": {"0": 0.6, "3": 0.2, "7": 0.2},
                                                 "order_1": {}, "order_2": {}}},
            "tempo": {"mean": 120.0},
        }
        _core._CACHE["project_keys"] = {"pG": "G minor"}
        _core.set_phrase_pool_for_test(pool)          # block 25's synthetic pool
        g1 = _gears.answer_with_gear(seed_line, gear=1, key="D minor", seed=0)
        assert g1["transforms_applied"] == ["verbatim"], g1["transforms_applied"]
        assert g1["source_phrase_ids"] == ["pG|c0|w0"], \
            f"module-universe fork? gear 1 must see the synthetic pool, got {g1['source_phrase_ids']}"
        assert [n["pitch"] for n in g1["answer_notes"]][:3] == [67, 70, 74], \
            "gear 1 must return the SOURCE phrase untransposed"
        g3 = _gears.answer_with_gear(seed_line, gear=3, key="D minor",
                                     surgery="bass_shadow", seed=0)
        assert g3 == _gears.answer_with_gear(seed_line, gear=3, key="D minor",
                                             surgery="bass_shadow", seed=0), "gear 3 deterministic"
        assert max(n["pitch"] for n in g3["answer_notes"]) < 50, "bass shadow must sit low"
        assert any(t.startswith("surgery:") for t in g3["transforms_applied"])
        g3g = _gears.answer_with_gear(seed_line, gear=3, key="D minor",
                                      surgery="ghost_answer", seed=0)
        g2n = len(_gears.answer_with_gear(seed_line, gear=2, key="D minor",
                                          seed=0)["answer_notes"])
        assert len(g3g["answer_notes"]) == 2 * g2n, "ghost answer doubles the notes"
        c5 = _gears.answer_with_gear(seed_line, gear=5, key="D minor", seed=0)
        assert "answer_notes" not in c5 and c5["mode"] == "compose", "gear 5 = contract only"
        c6 = _gears.answer_with_gear(seed_line, gear=6, key="D minor",
                                     direction="darker", seed=0)
        assert c6["bones"]["contour"] and c6["bones"]["direction_target"] == "darker"
        # gate MECHANICS under a zeroed style floor (real style scoring needs the real
        # profile, not this stub — the floor itself is exercised in live usage)
        # a contract can no longer LOWER a threshold (F1); left here to prove it
        c5["validation"]["min_style_score"] = 0.0
        good = [{"pitch": p, "start_beats": i * 0.5, "duration_beats": 0.4,
                 "velocity": v} for i, (p, v) in enumerate(
                     [(62, 90), (65, 60), (69, 75), (67, 82), (65, 58), (62, 96)])]
        v_ok = _gears.validate_composed(good, c5)
        assert v_ok["passes"], v_ok
        flat = [{**n, "velocity": 80} for n in good]
        v_flat = _gears.validate_composed(flat, c5)
        assert not v_flat["passes"] and not v_flat["checks"]["dynamics"], \
            "flat dynamics must be rejected"
        offkey = [{**n, "pitch": n["pitch"] + 1} for n in good]
        assert not _gears.validate_composed(offkey, c5)["passes"], "out-of-key must fail"
        # director axes: clamped into [-1, 1]; unknown axes refused
        c5ax = _gears.answer_with_gear(seed_line, gear=5, key="D minor",
                                       axes={"romantic_brutal": 0.7, "sparse_maximal": -2.0},
                                       seed=0)
        assert c5ax["axes"] == {"romantic_brutal": 0.7, "sparse_maximal": -1.0}, c5ax["axes"]
        try:
            _gears.answer_with_gear(seed_line, gear=5, key="D minor",
                                    axes={"loud_quiet": 1.0})
            raise AssertionError("unknown axis must raise")
        except ValueError:
            pass
        # KEEP/DROP (ratings half): keep golds the source; drop logs the why.
        # ears_session_path isolates the writer wiring (item Critical 2) from
        # the real tools/ideas/ears_session.json during selftest — same
        # isolation discipline as set_ratings_path elsewhere in this suite.
        _jd_td31 = tempfile.mkdtemp(prefix="judge31_")
        _jd_sp31 = os.path.join(_jd_td31, "ears_session.json")
        # a DROP also writes sticky-develop state; isolate BOTH or the selftest
        # writes into his live tools/ideas/develop_session.json
        _jd_dp31 = os.path.join(_jd_td31, "develop_session.json")
        kept31 = _gears.judge_answer("keep", source_phrase_id="pG|c0|w0", reason="slaps",
                                     ears_session_path=_jd_sp31,
                                     develop_session_path=_jd_dp31)
        assert kept31["verdict"] == "keep"
        assert kept31["ears_announcements"] == [], \
            "a lone KEEP has nothing to announce"
        assert any(g.get("phrase_id") == "pG|c0|w0" for g in _core.get_gold_phrases()), \
            "KEEP must gold the source phrase"
        drop31 = _gears.judge_answer("drop", answer_notes=seed_line, reason="too static",
                                     ears_session_path=_jd_sp31,
                                     develop_session_path=_jd_dp31)
        assert drop31["record"]["label"] == "dropped" and drop31["record"]["comment"] == "too static"
        assert drop31["ears_announcements"] == [], \
            "one reject is below the 3-strike demotion threshold"
        try:
            _gears.judge_answer("keep")
            raise AssertionError("judge without id or notes must raise")
        except ValueError:
            pass
    finally:
        _core.set_phrase_pool_for_test(None)
        if _saved_agg31 is None:
            _core._CACHE.pop("aggregate", None)
        else:
            _core._CACHE["aggregate"] = _saved_agg31
        if _saved_pk31 is None:
            _core._CACHE.pop("project_keys", None)
        else:
            _core._CACHE["project_keys"] = _saved_pk31
    print("  recall verbatim; bass shadow low; ghost doubles; contracts gate flat/off-key;")
    print("  axes clamped+validated; KEEP golds source, DROP logs the why")
    print("  OK\n")

    # 32. finisher: synthetic projects — dramaturg detectors + verb proposals
    print("--- whats_missing (finisher brain) ---")
    import finisher as _finisher  # noqa: E402

    def _clipd(track, start, end, count, pitches, drum=False, source="arrangement"):
        notes = [{"pitch": p, "start": i * 0.5, "duration": 0.4, "velocity": 70 + i,
                  "enabled": True} for i, p in enumerate(pitches)]
        return {"name": track, "track": track, "start": start, "end": end,
                "note_count": count, "is_drum": drum, "clip_source": source,
                "notes": notes}

    _sickp = {"path": "sick.als", "tempo": 120.0, "track_count": 2,
              "midi_track_names": ["loop"], "audio_track_names": [],
              "clip_count": 3, "total_notes": 210, "audio_clip_count": 0,
              "clips": [_clipd("loop", 0.0, 384.0, 200, [62, 65, 69, 67, 65]),
                        _clipd("loop", 0.0, 32.0, 5, [62, 65, 69, 67, 65],
                               source="take_lane"),
                        _clipd("loop", 32.0, 64.0, 5, [62, 64, 65, 67, 69],
                               source="take_lane")]}
    d_sick = _finisher.whats_missing(project_dict=_sickp)
    hit = {f["detector"] for f in d_sick["findings"]}
    assert {"loop_with_ambitions", "uncommitted_ending",
            "comping_debris", "b_section_missing", "machine_timing"} <= hit, hit
    assert all(p["verb"] in _finisher.VERB_MENU for p in d_sick["proposals"])
    assert all(p["intervention"] in _finisher.INTERVENTIONS for p in d_sick["proposals"]), \
        "proposals must be TYPED interventions"
    _cmds = [p["command"]["tool"] for p in d_sick["proposals"] if p["command"]]
    assert "answer_my_loop" in _cmds, "diagnosis must wire to the gearbox"
    assert d_sick["frame"]["total_bars"] == 96
    assert d_sick == _finisher.whats_missing(project_dict=_sickp), "diagnosis deterministic"

    _flatp = {"path": "flat.als", "tempo": 120.0, "track_count": 1,
              "midi_track_names": ["synth"], "audio_track_names": [],
              "clip_count": 1, "total_notes": 64, "audio_clip_count": 0,
              "clips": [{"name": "synth", "track": "synth", "start": 0.0, "end": 160.0,
                         "note_count": 64, "is_drum": False, "clip_source": "arrangement",
                         "notes": [{"pitch": 62 + (i % 4), "start": i * 0.5,
                                    "duration": 0.4, "velocity": 80, "enabled": True}
                                   for i in range(16)]}]}
    hit_flat = {f["detector"] for f in _finisher.whats_missing(project_dict=_flatp)["findings"]}
    assert {"flat_velocity", "machine_timing", "no_low_end"} <= hit_flat, hit_flat

    _healthy = {"path": "healthy.als", "tempo": 120.0, "track_count": 3,
                "midi_track_names": ["pad", "lead", "drums"], "audio_track_names": [],
                "clip_count": 6, "total_notes": 500, "audio_clip_count": 0,
                "clips": [_clipd("pad", 0.0, 240.0, 60, [48, 55, 52]),
                          _clipd("lead", 32.0, 160.0, 128, [62, 65, 69, 67, 65]),
                          _clipd("lead", 176.0, 208.0, 32, [62, 65, 69, 67, 65]),
                          _clipd("drums", 96.0, 160.0, 128, [36, 38, 42], drum=True),
                          _clipd("drums", 176.0, 208.0, 64, [36, 38, 42], drum=True)]}
    d_ok = _finisher.whats_missing(project_dict=_healthy)
    highs = [f for f in d_ok["findings"] if f["severity"] == "high"]
    assert not highs, f"healthy project must raise no high-severity findings: {highs}"
    assert d_ok["frame"]["n_sections"] >= 4, d_ok["frame"]
    print(f"  sick: {sorted(hit)} all fired; healthy: {len(d_ok['findings'])} "
          f"low/medium only, {d_ok['frame']['n_sections']} sections")
    print("  OK\n")

    # 33. rituals: THREE_ENDINGS shapes, BURY/MARRY ledger, VERSION_COURAGE, CLOSE
    print("--- rituals (coach-named) ---")
    import rituals as _rituals  # noqa: E402
    _seed33 = [{"pitch": p, "start_beats": s, "duration_beats": 0.45, "velocity": v}
               for p, s, v in zip([62, 65, 69, 67, 65, 62],
                                  [0.0, 0.5, 1.0, 4.0, 4.5, 5.0],
                                  [90, 70, 84, 88, 66, 92])]
    te = _rituals.three_endings(_seed33, key="D minor", bars=4, seed=0)
    assert [e["name"] for e in te["endings"]] == ["decay", "subtraction", "hard_stop"]
    assert te == _rituals.three_endings(_seed33, key="D minor", bars=4, seed=0), "deterministic"
    _decay = te["endings"][0]["notes"]
    assert _decay[-1]["pitch"] % 12 == 2 and _decay[-1]["duration_beats"] == 4.0, \
        "decay must land on a held tonic (D)"
    _sub = te["endings"][1]["notes"]
    _per_bar = [sum(1 for n in _sub if b * 4 <= n["start_beats"] < (b + 1) * 4)
                for b in range(4)]
    assert all(a >= b for a, b in zip(_per_bar, _per_bar[1:])) and _per_bar[-1] == 1, \
        f"subtraction must thin monotonically to one note: {_per_bar}"
    _hard = te["endings"][2]["notes"]
    _cut33 = max(n["start_beats"] for n in _hard)
    assert _cut33 <= 10.0 + 1e-6, f"hard stop must leave the room empty after the cut: {_cut33}"

    import tempfile as _tf33
    _td33 = _tf33.mkdtemp(prefix="sidekick_rituals_selftest_")
    _rt = os.path.join(_td33, "rituals_selftest.json")
    _nt = os.path.join(_td33, "notes_selftest.md")
    _rituals.set_rituals_paths_for_test(_rt, _nt)
    try:
        from unittest import mock as _mock33
        # bury resolves like marry; the selftest catalog has no old_sketch_77
        with _mock33.patch.object(_rituals.finisher, "resolve_project",
                                  return_value={"path": "/tmp/old_sketch_77.als"}):
            b = _rituals.bury("old_sketch_77", "good bass, wrong year")
        assert b["resolved_total"] == 1 and "organ donor" in b["note"]
        assert "## Graveyard" in open(_nt).read() and "old_sketch_77" in open(_nt).read()
        try:
            _rituals.marry("imaginary_project_x", deadline="2026-08-01")
            raise AssertionError("marrying an unknown project must fail before recording")
        except ValueError:
            pass
        with _mock33.patch.object(_finisher, "resolve_project", return_value=_sickp):
            m = _rituals.marry("imaginary_project_x", deadline="2026-08-01")
        assert m["resolved_total"] == 2 and m["married"] == "sick.als" \
            and m["diagnosis"]["project"] == "sick.als", \
            "marry must record and diagnose under the resolved Set path"
        try:
            _rituals.bury("y", "")
            raise AssertionError("burial without eulogy must raise")
        except ValueError:
            pass
        vc = _rituals.version_courage("synthfam", members=[_sickp, _healthy])
        assert vc["recommend_marry"] == "healthy", vc["recommend_marry"]
        assert "sick" in vc["harvest"], "the unrecommended member must appear as a donor"
        with _mock33.patch.object(_finisher, "resolve_project_key", return_value="sick.als"):
            oc = _rituals.one_hour_close(diagnosis=d_sick)
        assert oc["moves"][-1]["move"].startswith("BOUNCE") and oc["total_minutes"] <= 60
        assert any(m33.get("command") for m33 in oc["moves"][:-1]), \
            "close moves must carry executable commands where tools exist"
        nn = _rituals.no_new_notes(45)
        assert nn["active"] and "gear 4-6" in " ".join(nn["forbidden"])
        assert _rituals.no_new_notes_active(), "mode must read back active"
        try:   # ENFORCED, not advisory: gears 4-6 refuse while the mode holds
            _gears.answer_with_gear(_seed33, gear=5, key="D minor", seed=0)
            raise AssertionError("gear 5 must refuse under NO_NEW_NOTES")
        except ValueError as e:
            assert "NO_NEW_NOTES" in str(e)
        lift = _rituals.no_new_notes(0)
        assert not lift["active"] and _rituals.no_new_notes_active() is None
        c_ok = _gears.answer_with_gear(_seed33, gear=5, key="D minor", seed=0)
        assert c_ok["mode"] == "compose", "lifting the mode must restore gear 5"
    finally:
        _rituals.set_rituals_paths_for_test(None, None)
        shutil.rmtree(_td33, ignore_errors=True)
    print("  endings: tonic-landing decay, monotonic subtraction, room after the cut;")
    print("  ledger + graveyard + courage + close + no-new-notes contracts OK")
    print("  OK\n")

    # 34. direction axes: computable dials, sane signs, deterministic
    print("--- direction axes ---")
    sys.path.insert(0, os.path.join(_core.ROOT, "tools"))
    from build_directions import compute_axes as _cax  # noqa: E402
    _projs34 = [
        {"name": "doomy", "has_harmonic": True, "tempo": 70.0, "key": "C minor",
         "pitch_mean": 45.0, "density_notes_per_beat": 0.8, "drum_notes": 0,
         "harmonic_notes": 1000},
        {"name": "clubby", "has_harmonic": True, "tempo": 122.0, "key": "A minor",
         "pitch_mean": 62.0, "density_notes_per_beat": 3.0, "drum_notes": 900,
         "harmonic_notes": 1100},
        {"name": "bright", "has_harmonic": True, "tempo": 128.0, "key": "C major",
         "pitch_mean": 74.0, "density_notes_per_beat": 2.0, "drum_notes": 300,
         "harmonic_notes": 1000},
    ]
    ax = _cax(_projs34)
    assert ax == _cax(_projs34), "axes deterministic"
    assert ax["doomy"]["darker_brighter"] < ax["bright"]["darker_brighter"], \
        "slow+minor+low must sit darker than fast+major+high"
    assert ax["clubby"]["song_club"] > ax["doomy"]["song_club"], \
        "122bpm + drums must sit clubbier than 70bpm drumless"
    assert ax["doomy"]["sparse_maximal"] < ax["clubby"]["sparse_maximal"], \
        "low density must sit sparser"
    c6d = _gears.answer_with_gear(_seed33, gear=6, key="D minor",
                                  direction="weirder", seed=0)
    assert c6d["direction_exemplars"] == [], "unmapped directions stay LLM-interpreted"
    print("  doomy<bright on darkness; clubby wins club; sparse sane; weirder stays LLM's")
    print("  OK\n")

    # 35. SOMBRA nervous system: OSC event parse + dispatch plans + world norms
    print("--- sombra listener (pure) + world-aware norms ---")
    import sombra_listener as _sl  # noqa: E402
    import envelope_bridge as _eb35  # noqa: E402
    ev = _sl.parse_osc_event(_eb35.osc_pack("/sombra/ping", "x"))
    assert ev["address"] == "/sombra/ping" and ev["args"] == ["x"]
    import struct as _st35
    raw = (b"/sombra/answer\x00\x00" + b",i\x00\x00" + _st35.pack(">i", 3))
    ev3 = _sl.parse_osc_event(raw)
    assert ev3 == {"address": "/sombra/answer", "args": [3]}
    plan = _sl.dispatch(ev3, execute=False)
    assert plan["tool"] == "answer_my_loop" and plan["tool_args"]["gear"] == 3
    p5 = _sl.dispatch({"address": "/sombra/answer", "args": [5]}, execute=False)
    assert p5["action"] == "llm_needed", "gears 5-6 route to the chat, never the thread"
    _sl.STATE["last_answer_slot"] = None
    pk = _sl.dispatch({"address": "/sombra/keep", "args": []}, execute=False)
    assert pk["action"] == "noop", "KEEP with no answer on record must noop, not crash"
    _sl.STATE["last_answer_slot"] = "7/3"
    pd = _sl.dispatch({"address": "/sombra/drop", "args": []}, execute=False)
    assert pd["tool_args"]["confirm"] is True and pd["tool_args"]["slot"] == "7/3"
    _sl.STATE["last_answer_slot"] = None
    assert _sl.dispatch({"address": "/sombra/nope", "args": []},
                        execute=False)["action"] == "unknown"
    # world-aware norms: the same 60-bar monolith (62% of a 96-bar piece) is a flaw
    # at 120 (>48 bars AND >60%), patience at 70 (≤96 bars AND ≤75%)
    _mono = {"path": "w.als", "tempo": 120.0, "track_count": 2,
             "midi_track_names": ["x", "y"], "audio_track_names": [], "clip_count": 2,
             "total_notes": 160, "audio_clip_count": 0,
             "clips": [_clipd("x", 0.0, 240.0, 100, [62, 65, 69, 67, 65]),
                       _clipd("y", 240.0, 384.0, 60, [50, 53, 55, 57])]}
    hit120 = {f["detector"] for f in _finisher.whats_missing(project_dict=_mono)["findings"]}
    _mono70 = dict(_mono, tempo=70.0)
    hit70 = {f["detector"] for f in _finisher.whats_missing(project_dict=_mono70)["findings"]}
    assert "loop_with_ambitions" in hit120, hit120
    assert "loop_with_ambitions" not in hit70, \
        f"60 bars at 70 BPM is doom-world patience, not a flaw: {hit70}"
    print("  OSC parse exact; gear-5 routes to chat; keep-noop safe; drop confirm-gated;")
    print("  60 bars = flaw at 120, patience at 70 (world-aware norms)")
    print("  OK\n")

    # 36. gold → taste TRAINING: golds become guarded preference pairs (his ask:
    # "id like to be training as well" — golds must move weights, not just retrieval)
    print("--- gold-anchored taste training ---")
    import shutil as _sh36
    import tempfile as _tf36
    _dir36 = _tf36.mkdtemp(prefix="gold36_")
    _core.set_ratings_path(os.path.join(_dir36, "ratings.json"))
    flow36 = [{"pitch": p, "start_beats": j * 0.5, "duration_beats": 0.5, "velocity": 80}
              for j, p in enumerate([62, 65, 67, 69, 67, 65, 64, 62])]
    stat36 = [[{"pitch": [62, 62, 63, 62, 64, 62, 63, 62][j], "start_beats": j * 0.5,
                "duration_beats": 0.5, "velocity": 80} for j in range(8)]
              for _ in range(2)]
    pool36 = [{"id": f"p{i}", "project": "synthproj", "track": "t", "role": "lead",
               "kind": "harmonic", "tempo": 120.0, "n_notes": 8,
               "notes": [{"pitch": 60 + i % 2 + (2 if j in (2, 5) else 0) + (4 if j == 4 else 0),
                          "start": j * 0.5, "duration": 0.5, "velocity": 80}
                         for j in range(8)]}
              for i in range(8)]
    _core.set_phrase_pool_for_test(pool36)
    try:
        for _r36 in range(2):   # 2 rounds x 3 pairs = 6 real pairs = _TASTE_MIN_PAIRS
            _core.rate_candidates([flow36] + stat36, best=0, worst=1,
                                  reason="static", key="D minor", role="lead",
                                  idempotency_key=f"selftest:36:static:{_r36}")
        model0 = _core._taste_model()
        assert model0["n_pairs"] == 6 and model0.get("n_gold_pairs", 0) == 0, \
            "no golds yet → no gold pairs"
        real36 = _core._taste_pairs_from_rounds(
            _core._load_ratings_file().get("candidate_rounds", []))
        base_w36 = _taste.train(real36, len(_core._TASTE_FEATURE_NAMES))
        # (b) gold that AGREES with the rounds: trains, exam intact, direction learned
        rg = _core.rate(1, label="gold", phrase_id="gold36", notes=flow36)
        assert rg["taste_retrained"]["n_gold_pairs"] > 0, "gold must add training pairs"
        m1 = _core._taste_model()
        assert m1["n_pairs"] == 6, "golds must NOT inflate the cold-start ramp count"
        assert _taste.accuracy(real36, m1["weights"]) >= _taste.accuracy(real36, base_w36), \
            "augmentation may never worsen the real-pair exam"
        gscore = _core._taste_score(flow36)
        assert all(gscore > _core._taste_score(p["notes"]) for p in pool36), \
            "trained model must rank the gold above every typical negative"
        # (c) gold that CONTRADICTS the rounds: guard keeps the exam sovereign
        _core.rate(1, label="gold", phrase_id="p3", notes=pool36[3]["notes"])
        m2 = _core._taste_model()
        assert _taste.accuracy(real36, m2["weights"]) >= _taste.accuracy(real36, base_w36), \
            "contradictory golds must not break real-pair accuracy (distortion guard)"
        # (d) un-gold both → pseudo-pairs vanish, weights return to pairs-only exactly
        _core.rate(-1, label="ungold", phrase_id="gold36")
        ru = _core.rate(-1, label="ungold", phrase_id="p3")
        assert ru["taste_retrained"]["n_gold_pairs"] == 0, "ungold must clear gold pairs"
        assert _core._taste_model()["weights"] == base_w36, \
            "after un-golding, weights must equal pairs-only training exactly"
    finally:
        _core.set_phrase_pool_for_test(None)
        _sh36.rmtree(_dir36, ignore_errors=True)
        _core.set_ratings_path(ratings_tmp)
    print("  gold trains (guarded), ramp uninflated, contradiction survivable, ungold reverts")
    print("  OK\n")

    # 37. passable tier is INERT + positive-rating ungold (probe demotion) + 3-tier rounds
    # (2026-07-03: his audition "gold" clicks meant merely passable — the ledger must
    # be able to say "keeper, not anchor" without touching the gold set or the model)
    print("--- passable tier (ledger-only) + probe demotion + 3-tier ingest shapes ---")
    import shutil as _sh37
    import tempfile as _tf37
    _dir37 = _tf37.mkdtemp(prefix="pass37_")
    _core.set_ratings_path(os.path.join(_dir37, "ratings.json"))
    _pool37 = [{"id": "n%d" % i, "project": "synthproj", "track": "t", "role": "lead",
                "kind": "harmonic", "tempo": 120.0, "n_notes": 6,
                "notes": [{"pitch": 55 + (i + j) % 5, "start": j * 0.5, "duration": 0.5,
                           "velocity": 80} for j in range(6)]} for i in range(4)]
    _core.set_phrase_pool_for_test(_pool37)
    try:
        _pn37 = [{"pitch": 60 + j, "start_beats": j * 0.5, "duration_beats": 0.5,
                  "velocity": 80} for j in range(6)]
        rp37 = _core.rate(1, label="passable", phrase_id="selftest|pass|1", notes=_pn37)
        assert "taste_retrained" not in rp37, "passable must not retrain taste"
        assert not any(g.get("phrase_id") == "selftest|pass|1"
                       for g in _core.get_gold_phrases()), \
            "passable must never enter the gold set"
        assert not os.path.exists(_core._taste_model_path()), \
            "passable must not even create a taste model file"
        _core.rate(1, label="gold", phrase_id="selftest|pass|2", notes=_pn37)
        assert any(g.get("phrase_id") == "selftest|pass|2" for g in _core.get_gold_phrases())
        _core.rate(1, label="ungold", phrase_id="selftest|pass|2", notes=_pn37)
        assert not any(g.get("phrase_id") == "selftest|pass|2"
                       for g in _core.get_gold_phrases()), \
            "a POSITIVE-rating ungold must demote (probe keeps the upvote, loses the crown)"
        _core.rate(1, label="gold", phrase_id="selftest|pass|2", notes=_pn37)
        assert any(g.get("phrase_id") == "selftest|pass|2" for g in _core.get_gold_phrases()), \
            "re-gold must restore membership (latest-wins)"
    finally:
        _core.set_phrase_pool_for_test(None)
        _sh37.rmtree(_dir37, ignore_errors=True)
        _core.set_ratings_path(ratings_tmp)
    from ingest_audition_votes import plan_ingest as _plan37  # noqa: E402
    _man37 = [{"blind_id": i, "phrase_id": "synthproj|c%d|w0" % i,
               "picked_by": "gold_probe" if i == 5 else "x"} for i in range(1, 7)]
    _ids37 = {m["phrase_id"] for m in _man37}
    _p37 = _plan37(_man37, {1: "gold", 2: "passable", 3: "passable", 4: "meh",
                            5: "meh", 6: ""},
                   lambda proj: False, _ids37, "t37")
    _shapes37 = sorted((r["winner"], len(r["losers"])) for r in _p37["rounds"])
    assert _shapes37 == [("synthproj|c1|w0", 4), ("synthproj|c2|w0", 2),
                         ("synthproj|c3|w0", 2)], _shapes37
    _probe37 = [(r["label"], r["rating"]) for r in _p37["rates"]
                if r["phrase_id"] == "synthproj|c5|w0"]
    assert _probe37 == [("ungold", 0)], "meh on a probe must demote via a rating-0 ungold"
    _leg37 = _plan37(_man37, {1: "gold", 4: "meh"}, lambda proj: False, _ids37, "t37",
                     legacy_label="passable")
    assert [r["label"] for r in _leg37["rates"]] == ["passable"], \
        "legacy 2-value 'gold' must reinterpret as passable"
    try:
        _plan37(_man37, {1: "gold", 2: "passable"}, lambda proj: False, _ids37, "t37",
                legacy_label="passable")
        raise AssertionError("native-passable file must refuse --legacy-label")
    except ValueError:
        pass
    print("  passable = ledger-only (no model file); +1 ungold demotes; re-gold restores;")
    print("  rounds gold>P+M / passable>M; probe meh -> ungold(0); legacy remap + native guard")
    print("  OK\n")

    # 38. uncertainty booth pairing: deterministic, degree-capped, manifest stays BLIND
    print("--- booth pair selection (uncertain/random/repeat) + blindness ---")
    from rank_pairwise import select_pairs as _sp38, manifest_rows as _mr38  # noqa: E402
    _ids38 = ["c%02d" % i for i in range(12)]
    _taste38 = {cid: (i * 0.7) % 3.1 for i, cid in enumerate(_ids38)}
    _taste38["c03"] = _taste38["c07"]              # deliberate float tie
    _past38 = [("c01", "c02", 0), ("c04", "c09", 1)]
    _p38a, _m38a = _sp38(_ids38, _taste38, _past38, seed=7)
    _p38b, _m38b = _sp38(_ids38, _taste38, _past38, seed=7)
    assert _p38a == _p38b and _m38a == _m38b, "same seed must reproduce exactly"
    _cnt38 = {m: sum(1 for p in _p38a if p["selection_mode"] == m)
              for m in ("uncertain", "random", "repeat")}
    assert _cnt38 == {"uncertain": 11, "random": 5, "repeat": 2}, _cnt38
    _deg38 = {}
    for p in _p38a:
        if p["selection_mode"] == "repeat":
            continue
        for c in (p["a_id"], p["b_id"]):
            _deg38[c] = _deg38.get(c, 0) + 1
    assert max(_deg38.values()) <= 4, "degree cap must hold"
    _reps38 = {tuple(sorted((p["a_id"], p["b_id"]))) for p in _p38a
               if p["selection_mode"] == "repeat"}
    assert _reps38 == {("c01", "c02"), ("c04", "c09")}, "repeats must preserve past id-pairs"
    assert all(p["repeat_of"]["past_winner"] in ("c01", "c04") for p in _p38a
               if p["selection_mode"] == "repeat"), "repeat provenance must survive"
    _rows38 = _mr38(_p38a, {})
    _extra38 = {k for r in _rows38 for k in r} - {"pair_id", "A_phrase_id", "A_midi",
                                                  "B_phrase_id", "B_midi", "winner",
                                                  "selection_mode"}
    assert not _extra38, "BLINDNESS: model-derived keys reached the manifest: %s" % _extra38
    _pflat38, _mflat38 = _sp38(_ids38, {}, _past38, seed=7)
    assert _mflat38["flat_model"] and all(p["selection_mode"] in ("random", "repeat")
                                          for p in _pflat38), \
        "flat/absent model must yield no uncertain pairs (id-order garbage guard)"
    assert len(_pflat38) == 18, "cold-start must still fill all 18 pairs"
    print("  deterministic; 11/5/2 modes; degree<=4; repeats preserved; manifest blind;")
    print("  flat model -> all-random fallback (18 kept)")
    print("  OK\n")

    # 39. booth ingest plan: repeats never train; scoreboard is random-only on CLEAN
    # pairs vs pre-vote frozen predictions; pre-registered promotion rule math
    print("--- booth scoreboard math + repeat exclusion + promotion rule ---")
    from ingest_booth_votes import (plan_booth_ingest as _pbi39,  # noqa: E402
                                    promotion_status as _ps39,
                                    validate_booth_vote_payload as _vbp39)

    def _mk39(pid, mode, a, b, w):
        return {"pair_id": pid, "A_phrase_id": a, "B_phrase_id": b, "winner": w,
                "selection_mode": mode}
    _man39 = [
        _mk39(1, "random", "r1a", "r1b", "A"), _mk39(2, "random", "r2a", "r2b", "A"),
        _mk39(3, "random", "r3a", "r3b", "A"), _mk39(4, "random", "r4a", "r4b", "B"),
        _mk39(5, "random", "r5a", "r5b", "B"),
        _mk39(6, "uncertain", "u1a", "u1b", "A"), _mk39(7, "uncertain", "u2a", "u2b", "B"),
        _mk39(8, "uncertain", "u3a", "u3b", "A"), _mk39(9, "uncertain", "u4a", "u4b", "A"),
        _mk39(10, "repeat", "p1a", "p1b", "A"), _mk39(11, "repeat", "p2a", "p2b", "B"),
        _mk39(12, "random", "r6a", "r6b", "tie"), _mk39(13, "random", "r7a", "r7b", ""),
        _mk39(14, "random", "BLOCKED|c0|w0", "r8b", "A"),
        _mk39(15, "uncertain", "u5a", "GONE", "A"),
    ]
    _preds39 = {"round_id": "booth_t39", "pairs":
                [{"pair_id": n, "predicted_winner": "r%da" % n} for n in range(1, 6)]
                + [{"pair_id": 6, "predicted_winner": "u1a"},
                   {"pair_id": 7, "predicted_winner": "u2a"},
                   {"pair_id": 8, "predicted_winner": "u3b"},
                   {"pair_id": 9, "predicted_winner": "u4a"},
                   {"pair_id": 10, "repeat_of": {"past_winner": "p1a"}},
                   {"pair_id": 11, "repeat_of": {"past_winner": "p2a"}},
                   {"pair_id": 14, "predicted_winner": "BLOCKED|c0|w0"}]}
    _ids39 = {x for m in _man39 for x in (m["A_phrase_id"], m["B_phrase_id"])} - {"GONE"}
    _partial39 = _vbp39(
        {"round_id": "booth_t39", "votes": [{"pair_id": 1, "winner": "A"}]},
        _man39[:2], "booth_t39",
    )
    assert _partial39 == {1: "A"}, "valid autosave subset must be retained"
    for _bad39 in (
        {"round_id": "booth_t39", "votes": [
            {"pair_id": 1, "winner": "A"}, {"pair_id": 1, "winner": "B"}]},
        {"round_id": "booth_t39", "votes": [{"pair_id": 99, "winner": "A"}]},
        {"round_id": "booth_t39", "votes": [{"pair_id": True, "winner": "A"}]},
        {"round_id": "booth_t39", "votes": [{"pair_id": 0, "winner": "A"}]},
        {"round_id": "", "votes": [{"pair_id": 1, "winner": "A"}]},
        {"round_id": "booth_t39", "votes": [{"pair_id": 1, "winner": "wrong"}]},
    ):
        try:
            _vbp39(_bad39, _man39[:2], "booth_t39")
            raise AssertionError("invalid Booth autosave payload must be rejected")
        except ValueError:
            pass
    try:
        _vbp39(
            {"round_id": "booth_t39", "votes": [{"pair_id": 1, "winner": "A"}]},
            _man39[:2], "booth_t39", require_complete=True,
        )
        raise AssertionError("incomplete Booth payload must not reach ingestion")
    except ValueError:
        pass
    _complete39 = _vbp39(
        {"round_id": "booth_t39", "votes": [
            {"pair_id": 1, "winner": "A"}, {"pair_id": 2, "winner": "tie"}]},
        _man39[:2], "booth_t39", require_complete=True,
    )
    assert _complete39 == {1: "A", 2: "tie"}
    from ingest_booth_votes import (validate_booth_round_artifacts as _vbra39,
                                    validate_booth_golds as _vbg39,
                                    validate_booth_gold_sources as _vbgs39,
                                    booth_round_digest as _brd39)
    import tempfile as _tmp39
    _bound_manifest39 = [
        {"pair_id": 1, "A_phrase_id": "a", "B_phrase_id": "b",
         "selection_mode": "random"},
        {"pair_id": 2, "A_phrase_id": "c", "B_phrase_id": "d",
         "selection_mode": "uncertain"},
    ]
    _bound_predictions39 = {
        "model": {"weights_md5": "model-snapshot"},
        "pairs": [
            {"pair_id": 1, "A_phrase_id": "a", "B_phrase_id": "b",
             "selection_mode": "random", "predicted_winner": "a"},
            {"pair_id": 2, "A_phrase_id": "c", "B_phrase_id": "d",
             "selection_mode": "uncertain", "predicted_winner": "d"},
        ],
    }
    _bound_digest39 = _brd39(_bound_manifest39, _bound_predictions39)
    # F6 (i): booth_round_digest of an OLD-shaped manifest row (no *_notes_sha256)
    # must be byte-identical to before the F6 change — literal pinned pre-edit so a
    # regression in the digest of every existing round is caught immediately.
    assert _bound_digest39 == \
        "7a4fe9c89e6fae1ab594753af9edc6595dd3595d59aeec79365d57b9399c1765", \
        "F6 must not change the digest of any pre-existing (sha-less) Booth round"
    _bound_predictions39["artifact_digest"] = _bound_digest39
    _bound_predictions39["round_id"] = "booth_" + _bound_digest39[:12]
    assert _vbra39(_bound_manifest39, _bound_predictions39) == _bound_predictions39["round_id"]
    _cold_predictions39 = json.loads(json.dumps(_bound_predictions39))
    _cold_predictions39["model"] = {}
    for _cold_pair39 in _cold_predictions39["pairs"]:
        _cold_pair39.pop("predicted_winner", None)
    _cold_digest39 = _brd39(_bound_manifest39, _cold_predictions39)
    _cold_predictions39["artifact_digest"] = _cold_digest39
    _cold_predictions39["round_id"] = "booth_" + _cold_digest39[:12]
    assert _vbra39(_bound_manifest39, _cold_predictions39) == _cold_predictions39["round_id"], \
        "model-less cold-start rounds must remain ingestible"
    _mismatched_predictions39 = json.loads(json.dumps(_bound_predictions39))
    _mismatched_predictions39["pairs"][1]["B_phrase_id"] = "WRONG"
    try:
        _vbra39(_bound_manifest39, _mismatched_predictions39)
        raise AssertionError("Booth predictions must be content-bound to the manifest")
    except ValueError:
        pass
    _flipped_predictions39 = json.loads(json.dumps(_bound_predictions39))
    _flipped_predictions39["pairs"][1]["predicted_winner"] = "c"
    try:
        _vbra39(_bound_manifest39, _flipped_predictions39)
        raise AssertionError("valid-looking prediction changes must invalidate the round digest")
    except ValueError:
        pass
    with _tmp39.TemporaryDirectory() as _playback_td39:
        _a_midi39 = os.path.join(_playback_td39, "A.mid")
        _b_midi39 = os.path.join(_playback_td39, "B.mid")
        with open(_a_midi39, "wb") as _file39:
            _file39.write(b"A-one")
        with open(_b_midi39, "wb") as _file39:
            _file39.write(b"B-one")
        _playback_manifest39 = [{
            **_bound_manifest39[0], "A_midi": _a_midi39, "B_midi": _b_midi39,
        }]
        _playback_predictions39 = {"pairs": [_bound_predictions39["pairs"][0]]}
        _before_playback_digest39 = _brd39(
            _playback_manifest39, _playback_predictions39,
        )
        with open(_a_midi39, "wb") as _file39:
            _file39.write(b"A-changed")
        assert _brd39(_playback_manifest39, _playback_predictions39) != \
            _before_playback_digest39, \
            "manual playback content changes must invalidate the frozen Booth round"
    assert _vbg39(["a"], _bound_manifest39, {1: "A", 2: "tie"}) == ["a"]
    for _bad_golds39 in (["NOT-IN-ROUND"], ["b"], ["a", "a"], [["a"]]):
        try:
            _vbg39(_bad_golds39, _bound_manifest39, {1: "A", 2: "tie"})
            raise AssertionError("only unique, actually-voted winners may become gold")
        except ValueError:
            pass
    assert _vbgs39(["a"], {"a": {}}, lambda _project: False) == ["a"]
    for _gold_sources39, _blocked39 in ((["missing"], False), (["a"], True)):
        try:
            _vbgs39(_gold_sources39, {"a": {}}, lambda _project: _blocked39)
            raise AssertionError("missing or blocked Booth golds must fail preflight")
        except ValueError:
            pass
    from ingest_journal import (claim_submission as _claim39,
                                complete_submission as _complete39,
                                peek_submission as _peek39)  # noqa: E402
    with _tmp39.TemporaryDirectory() as _journal_td39:
        _journal_path39 = os.path.join(_journal_td39, "ingest_journal.json")
        assert _claim39(_journal_path39, "booth:round", {"votes": ["A"]}) == "new"
        assert _claim39(_journal_path39, "booth:round", {"votes": ["A"]}) == "pending"
        try:
            _claim39(_journal_path39, "booth:round", {"votes": ["B"]})
            raise AssertionError("a partial ingest must reject changed submission content")
        except ValueError:
            pass
        _complete39(_journal_path39, "booth:round")
        assert _claim39(_journal_path39, "booth:round", {"votes": ["A"]}) == "complete"
        assert _peek39(_journal_path39, "booth:round", {"votes": ["A"]}) == "complete"
        try:
            _peek39(_journal_path39, "booth:round", {"votes": ["B"]})
            raise AssertionError("peek must reject changed submission content")
        except ValueError:
            pass
        assert _peek39(_journal_path39, "booth:other", {"votes": ["A"]}) is None
        assert _claim39(_journal_path39, "booth:other", {"votes": ["A"]}) == "new", \
            "a refusal branch that peeked must never have manufactured a claim " \
            "a second identical run could recover as completed"
    _plan39 = _pbi39(_man39, _preds39, lambda p: p == "BLOCKED", _ids39)
    _s39 = _plan39["scoreboard"]
    assert _s39["n_random"] == 5 and _s39["n_agree"] == 3, _s39
    assert abs(_s39["agreement"] - 0.6) < 1e-9, "ties/skips/blocked out of the denominator"
    assert _s39["n_uncertain"] == 4 and _s39["n_uncertain_agree"] == 2, \
        "uncertain agreement is a diagnostic, tracked separately"
    assert _s39["n_repeat"] == 2 and abs(_s39["repeat_consistency"] - 0.5) < 1e-9
    assert _s39["n_tie_or_skip_random"] == 2
    _tids39 = {t["pair_id"] for t in _plan39["train"]}
    assert _tids39 == {1, 2, 3, 4, 5, 6, 7, 8, 9}, "repeats/blocked/missing must not train"
    assert _plan39["dropped_blocked"] == [14] and _plan39["dropped_missing"] == [15]
    _rule39 = {"min_agreement": 0.7, "min_random_pairs": 5, "consecutive_rounds": 3}
    _ok39 = {"n_random": 6, "agreement": 0.8}
    _bad39 = {"n_random": 6, "agreement": 0.6}
    _few39 = {"n_random": 4, "agreement": 0.9}
    assert _ps39([_ok39, _ok39, _ok39], _rule39)["promotable"] is True
    assert _ps39([_ok39, _bad39, _ok39], _rule39) == {"qualifying_streak": 1,
                                                      "promotable": False}
    assert _ps39([_ok39, _ok39, _few39], _rule39)["qualifying_streak"] == 0, \
        "too-few random pairs must break the streak even at high agreement"
    _ears_kind39 = dict(_ok39, kind="ears_ab")
    assert _ps39([_ears_kind39, _ears_kind39, _ears_kind39], _rule39) == \
        {"qualifying_streak": 0, "promotable": False}, \
        "ears_ab rounds must never advance the taste-model promotion streak"
    assert _ps39([_ok39, _ears_kind39, _ok39, _ok39], _rule39)["qualifying_streak"] == 3, \
        "an ears_ab round is transparent to the taste streak, not a break"
    assert _ps39([dict(_ok39, kind=None), _ok39, _ok39], _rule39)["promotable"] is True, \
        "absent/None kind (every historical round) still counts as taste"
    _leg39 = _pbi39([{"pair_id": 1, "A_phrase_id": "r1a", "B_phrase_id": "r1b",
                      "winner": "A"}], None, lambda p: False, _ids39)
    assert _leg39["legacy"] and len(_leg39["train"]) == 1, \
        "legacy manifests train everything, scoreboard skipped"
    assert _vbra39([{"pair_id": 1, "A_phrase_id": "r1a", "B_phrase_id": "r1b"}],
                   None) == ""
    try:
        _vbra39([{"pair_id": 1, "A_phrase_id": "a", "B_phrase_id": "b"},
                 {"pair_id": 1, "A_phrase_id": "c", "B_phrase_id": "d"}], None)
        raise AssertionError("legacy Booth manifests must reject duplicate pair IDs")
    except ValueError:
        pass
    from ingest_booth_votes import validate_booth_manifest_winners as _vbmw39
    _vbmw39([{"pair_id": 1, "winner": "A"}, {"pair_id": 2, "winner": ""},
             {"pair_id": 3, "winner": "tie"}, {"pair_id": 4, "winner": None}])
    for _bad_manifest_winner39 in ("AA", "yes", 1):
        try:
            _vbmw39([{"pair_id": 1, "winner": _bad_manifest_winner39}])
            raise AssertionError(
                "manifest-embedded winners must obey the vote grammar, not skip silently"
            )
        except ValueError:
            pass
    _ears_pair39 = [{"pair_id": 1, "A_phrase_id": "a", "B_phrase_id": "b",
                     "selection_mode": "ears_ab"}]
    # a VALID ears round is stamped model.kind == "ears_ab" (F7a); the sidecar
    # model was empty here before, which is now correctly rejected below.
    _ears_preds39 = {"model": {"kind": "ears_ab"}, "pairs": [
        {"pair_id": 1, "A_phrase_id": "a", "B_phrase_id": "b",
         "selection_mode": "ears_ab", "predicted_winner": "a"}]}
    _ears_digest39 = _brd39(_ears_pair39, _ears_preds39)
    _ears_preds39["artifact_digest"] = _ears_digest39
    _ears_preds39["round_id"] = "booth_" + _ears_digest39[:12]
    assert _vbra39(_ears_pair39, _ears_preds39) == _ears_preds39["round_id"], \
        "ears_ab rounds ride the same frozen-artifact contract"
    _ears_plan39 = _pbi39([{**_ears_pair39[0], "winner": "A"}], _ears_preds39,
                          lambda p: False, _ids39 | {"a", "b"})
    assert _ears_plan39["scoreboard"]["n_random"] == 1 and \
        _ears_plan39["scoreboard"]["n_agree"] == 1, \
        "ears_ab agreement counts like random pairs — that IS the promotion metric"
    # F7a: ears_ab classification is enforced in BOTH directions at validation —
    # an ears round mislabeled as taste (or a taste round wearing the ears model
    # kind) is REJECTED at the gate, killing the contamination scenario.
    def _rebind_ears39(manifest, preds):
        d = _brd39(manifest, preds)
        preds = {**preds, "artifact_digest": d, "round_id": "booth_" + d[:12]}
        return manifest, preds
    _stamped_taste39, _stamped_taste_preds39 = _rebind_ears39(
        _ears_pair39, {"model": {}, "pairs": _ears_preds39["pairs"]})
    try:
        _vbra39(_stamped_taste39, _stamped_taste_preds39)
        raise AssertionError("an ears_ab round stamped taste (model.kind absent) must be rejected")
    except ValueError as _exc39a:
        assert "ears_ab" in str(_exc39a), "the refusal must name the ears_ab contract"
    _mixed_manifest39 = [
        _ears_pair39[0],
        {"pair_id": 2, "A_phrase_id": "c", "B_phrase_id": "d", "selection_mode": "random"}]
    _mixed_manifest39, _mixed_preds39 = _rebind_ears39(_mixed_manifest39, {
        "model": {"kind": "ears_ab"}, "pairs": [
            _ears_preds39["pairs"][0],
            {"pair_id": 2, "A_phrase_id": "c", "B_phrase_id": "d",
             "selection_mode": "random", "predicted_winner": "c"}]})
    try:
        _vbra39(_mixed_manifest39, _mixed_preds39)
        raise AssertionError("a model.kind ears_ab round with a non-ears_ab pair must be rejected")
    except ValueError as _exc39b:
        assert "ears_ab" in str(_exc39b), "the refusal must name the ears_ab contract"
    # F6 (ii)/(iii): a NEW-shaped round binds notes by content hash. Staging/
    # training verify the hash against the live index and refuse loudly on a
    # mismatch (rebuilt index), pass on a match; a hash-less (old) row is skipped.
    from ingest_booth_votes import (notes_sha256 as _nsha39,
                                    verify_booth_note_bindings as _vbnb39)
    _f6_notes_a39 = [{"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 80}]
    _f6_notes_b39 = [{"pitch": 64, "start": 0.0, "duration": 1.0, "velocity": 80}]
    _f6_by_id39 = {"pa": {"id": "pa", "notes": _f6_notes_a39},
                   "pb": {"id": "pb", "notes": _f6_notes_b39}}
    _f6_bound39 = [{"pair_id": 1, "A_phrase_id": "pa", "B_phrase_id": "pb",
                    "selection_mode": "random",
                    "A_notes_sha256": _nsha39(_f6_notes_a39),
                    "B_notes_sha256": _nsha39(_f6_notes_b39)}]
    _vbnb39(_f6_bound39, _f6_by_id39)  # (iii) matching hashes pass silently
    _f6_changed39 = {"pa": {"id": "pa", "notes": _f6_notes_a39},
                     "pb": {"id": "pb", "notes": [{"pitch": 99, "start": 0.0,
                                                   "duration": 1.0, "velocity": 80}]}}
    try:
        _vbnb39(_f6_bound39, _f6_changed39)  # (ii) index note changed -> refuse
        raise AssertionError("a phrase changed since freeze must refuse, not silently restage")
    except ValueError as _exc39f6:
        assert "changed since the round was frozen" in str(_exc39f6) and "pb" in str(_exc39f6), \
            "the refusal must name the drifted phrase and say to rebuild"
    _f6_old39 = [{"pair_id": 1, "A_phrase_id": "pa", "B_phrase_id": "pb",
                  "selection_mode": "random"}]  # no hashes: grandfathered
    _vbnb39(_f6_old39, _f6_changed39)  # must NOT raise — absent binding is skipped
    # F7 (b): the ears qualification streak requires consecutive_rounds qualifying
    # EARS rounds (mirror of promotion_status, filtered TO ears_ab), never claiming
    # readiness on a single round.
    from ingest_booth_votes import ears_promotion_status as _eps39
    _eok39 = dict(_ok39, kind="ears_ab")
    _ebad39 = dict(_bad39, kind="ears_ab")
    assert _eps39([_eok39], _rule39) == {"qualifying_streak": 1, "promotable": False}, \
        "one qualifying ears round is NOT promotion-ready"
    assert _eps39([_eok39, _eok39, _eok39], _rule39)["promotable"] is True, \
        "three consecutive qualifying ears rounds are promotion-ready"
    assert _eps39([_eok39, _ebad39, _eok39], _rule39)["qualifying_streak"] == 1, \
        "a failing ears round breaks the ears streak"
    assert _eps39([_eok39, _ok39, _eok39, _eok39], _rule39)["qualifying_streak"] == 3, \
        "a taste round is transparent to the ears streak, not a break"
    assert _eps39([_ok39, _ok39, _ok39], _rule39)["qualifying_streak"] == 0, \
        "taste-only history advances the ears streak by nothing"
    print("  agreement 3/5 random-only (clean pairs, frozen predictions); uncertain 2/4")
    print("  diagnostic; repeats consistency 0.5, never train; promotion streak math OK")
    print("  ears_ab rides the same frozen-artifact contract; counts as random for promotion")
    print("  OK\n")

    # 40. NOMML played-vs-programmed gate: synthetic grids vs jitter (no corpus)
    print("--- NOMML (note-onset median metric level) gate ---")
    from nomml import OFF_GRID as _og40, nomml as _nomml40, onset_level as _ol40  # noqa: E402
    _grid40 = [{"pitch": 60 + j % 5, "start": j * 0.25, "duration": 0.25,
                "velocity": 80} for j in range(16)]
    _rg40 = _nomml40(_grid40)
    assert _rg40["performed"] is False and _rg40["nomml"] < _og40, \
        "a 16th-grid phrase must read as programmed"
    assert _rg40["off_grid_share"] == 0.0
    # deterministic human-scale jitter (~15-45ms at 120bpm) on every offbeat onset
    _jit40 = [dict(n, start=n["start"] + (0.03 + 0.015 * (j % 3)) * (1 if j % 2 else -1))
              for j, n in enumerate(_grid40)]
    for n in _jit40:
        n["start"] = max(0.0, n["start"])
    _rj40 = _nomml40(_jit40)
    assert _rj40["performed"] is True and _rj40["nomml"] == _og40, \
        "jittered playing must read as performed (median off-grid)"
    assert _rj40["off_grid_share"] > 0.9
    _tri40 = [{"pitch": 60, "start": j / 3.0, "duration": 0.3, "velocity": 80}
              for j in range(12)]
    assert _nomml40(_tri40)["performed"] is False, \
        "triplets are a GRID, not feel — must read as programmed"
    assert _ol40(0.0) == 0 and _ol40(1.0) == 2, \
        "onset level must be the COARSEST matching grid"
    _e40 = _nomml40([])
    assert (_e40["nomml"] is None and _e40["performed"] is False
            and _e40["n_onsets"] == 0 and _e40["performed_onsets"] is False)
    # 960-ppq file rounding must NOT read as feel
    _ppq40 = [dict(n, start=round(n["start"] * 960) / 960 + 0.0008) for n in _grid40]
    assert _nomml40(_ppq40)["performed"] is False, \
        "ppq rounding jitter must stay inside epsilon"
    # chord density must not pose as timing evidence: 3 on-grid 4-note chords
    # (12 note samples) + 4 off-grid singles -> note-weighted says programmed,
    # unique-onset says performed. Both reported; the ear decides which gates.
    _chords40 = [{"pitch": 48 + k * 3, "start": b * 1.0, "duration": 0.9, "velocity": 80}
                 for b in range(3) for k in range(4)]
    _chords40 += [{"pitch": 72 + j, "start": 3.0 + j * 0.25 + 0.04, "duration": 0.2,
                   "velocity": 80} for j in range(4)]
    _rc40 = _nomml40(_chords40)
    assert _rc40["performed"] is False and _rc40["performed_onsets"] is True, \
        "chord fixture must split the two variants (notes: programmed, onsets: performed)"
    assert _rc40["n_onsets"] == 16 and _rc40["n_unique_onsets"] == 7
    print("  16th grid -> programmed; ~25ms jitter -> performed; triplets = grid;")
    print("  coarsest-level assignment; ppq rounding absorbed; empty safe;")
    print("  chords split note-weighted vs unique-onset variants as designed")
    print("  OK\n")

    # 41. name-locked Live track lookup must survive NESTED track objects
    # (a missed match here creates a DUPLICATE auto-write track — policy break)
    print("--- live_write track lookup: nested-object tolerance ---")
    import live_write as _lw41  # noqa: E402
    _flat41 = 'tracks:[{id:"1", trackIndex:0, name:"SIDEKICK JAM", isArmed:false}]'
    _nested41 = ('tracks:[{id:"1", trackIndex:0, name:"SIDEKICK JAM", '
                 'instrument:{name:"Operator"}, isArmed:false}, '
                 '{id:"2", trackIndex:1, name:"other", devices:[{name:"EQ"}]}]')
    assert _lw41._track_index_by_name(_flat41, "SIDEKICK JAM") == (0, "1")
    assert _lw41._track_index_by_name(_nested41, "SIDEKICK JAM") == (0, "1"), \
        "nested instrument:{...} must not hide the track (duplicate-track bug)"
    assert _lw41._track_index_by_name(_nested41, "Operator") == (None, None), \
        "a nested device name must never pose as a track name"
    assert _lw41._track_index_by_name(_nested41, "other") == (1, "2")
    assert _lw41._track_index_by_name("", "x") == (None, None)
    _esc41 = 'tracks:[{id:"7", trackIndex:3, name:"weird \\"quoted{\\" name"}]'
    assert _lw41._track_index_by_name(_esc41, 'weird "quoted{" name')[0] == 3, \
        "escaped quotes and braces inside strings must not break the scanner"
    assert _lw41._require_verified_note_count(3, 3, "selftest") == 3
    for _bad_count41 in (None, 2):
        try:
            _lw41._require_verified_note_count(3, _bad_count41, "selftest")
            raise AssertionError("read-back mismatch must fail the write")
        except RuntimeError:
            pass
    _sent_notes41 = [
        {"pitch": 60, "start_beats": 0.0, "duration_beats": 2.25, "velocity": 80},
    ]
    assert _lw41._require_verified_notes(
        _sent_notes41, 'noteCount:1, notes:"v80 n9/16 C4 1|1"', "selftest-content",
    ) == 1
    assert _lw41._require_verified_notes(
        [{"pitch": 60, "start_beats": -0.25, "duration_beats": 0.5, "velocity": 0}],
        'noteCount:1, notes:"v1 n/8 C4 1|1"', "selftest-canonical-payload",
    ) == 1, "read-back must be compared with the exact clamped wire payload"
    _arrangement_read41 = (
        'trackIndex:2, arrangementClips:[{name:"lead §1", arrangementStart:"1|1", '
        'noteCount:1, notes:"v80 n/4 C4 1|1"}, '
        '{name:"lead §2", arrangementStart:"5|1", noteCount:1, '
        'notes:"v81 n5/8 D4 1|1"}]'
    )
    assert _lw41._extract_arrangement_clip_notes(
        _arrangement_read41, clip_name="lead §2",
    ) == "v81 n5/8 D4 1|1", "arrangement read-back must bind by clip name"
    _unnamed_placement41 = {"section_index": None, "arrangement_start_bar_beat": "1|1"}
    assert _lw41._arrangement_clip_name("lead", _unnamed_placement41, 1) != \
        _lw41._arrangement_clip_name("lead", _unnamed_placement41, 2), \
        "every arrangement placement needs a unique read-back identity"
    _duplicate_arrangement41 = (
        'arrangementClips:[{name:"same", notes:"v80 n/4 C4 1|1"}, '
        '{name:"same", notes:"v80 n/4 C4 1|1"}]'
    )
    assert _lw41._extract_arrangement_clip_notes(
        _duplicate_arrangement41, clip_name="same",
    ) == "", "ambiguous arrangement read-back identities must fail closed"
    for _bad_readback41 in (
        'noteCount:1, notes:"v80 n9/16 C#4 1|1"',
        'noteCount:1, notes:"v80 n/4 C4 1|1"',
        'noteCount:1, notes:"v79 n9/16 C4 1|1"',
    ):
        try:
            _lw41._require_verified_notes(
                _sent_notes41, _bad_readback41, "selftest-content",
            )
            raise AssertionError("same-count note corruption must fail read-back")
        except _lw41.LiveWriteVerificationError:
            pass
    try:
        _lw41._raise_partial_write(
            "SIDEKICK BOOTH 2/4", sent=3, received=2,
            completed=[{"name": "P01 A", "slot": "2/3"}],
        )
        raise AssertionError("partial Live writes must fail")
    except _lw41.LiveWriteVerificationError as _partial41:
        assert _partial41.completed == [{"name": "P01 A", "slot": "2/3"}]
        assert _partial41.failed_context == "SIDEKICK BOOTH 2/4"
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import session as _session41  # noqa: E402
    import tempfile as _tmp41
    import build_booth_page as _booth_page41  # noqa: E402
    import build_audition_page as _audition_page41  # noqa: E402
    _storage_manifest41 = [{"pair_id": 1, "A_phrase_id": "a", "B_phrase_id": "b"}]
    assert _booth_page41.vote_storage_key(_storage_manifest41, "booth_one") != \
        _booth_page41.vote_storage_key(_storage_manifest41, "booth_two"), \
        "Booth browser votes must be isolated by frozen round identity"
    _solo_storage41 = [{"blind_id": 1, "phrase_id": "a"}]
    assert _audition_page41.vote_storage_key(_solo_storage41, "r1") != \
        _audition_page41.vote_storage_key(_solo_storage41, "r2"), \
        "audition browser votes must be isolated by round identity"
    _rcs41 = _session41._require_complete_staging
    _rcs41({"verified_ok": True, "placed": [{"name": "P01 A", "ok": True}]}, ["P01 A"])
    try:
        _rcs41({"verified_ok": False, "placed": [{"name": "P01 A", "ok": False}]}, ["P01 A"])
        raise AssertionError("unverified Booth staging must fail closed")
    except RuntimeError:
        pass
    with _tmp41.TemporaryDirectory() as _stage_td41:
        _stage_path41 = os.path.join(_stage_td41, "round.stage.json")
        _session41._save_stage_progress(
            _stage_path41, "booth_round", ["P01 A"], ["P01 A", "P01 B"],
        )
        assert _session41._load_stage_progress(
            _stage_path41, "booth_round", ["P01 A", "P01 B"],
        ) == {"P01 A"}
        _session41._save_stage_progress(
            _stage_path41, "booth_round", ["P01 B"], ["P01 A", "P01 B"],
        )
        assert _session41._load_stage_progress(
            _stage_path41, "booth_round", ["P01 A", "P01 B"],
        ) == {"P01 A", "P01 B"}, "partial stage retries must preserve prior success"
        try:
            _session41._load_stage_progress(
                _stage_path41, "other_round", ["P01 A", "P01 B"],
            )
            raise AssertionError("stage progress must never cross round identities")
        except RuntimeError:
            pass
        _manual_a41 = os.path.join(_stage_td41, "A.mid")
        _manual_b41 = os.path.join(_stage_td41, "B.mid")
        for _manual_path41 in (_manual_a41, _manual_b41):
            with open(_manual_path41, "wb") as _manual_file41:
                _manual_file41.write(b"MThd")
        _manual_manifest41 = [{
            "pair_id": 1, "A_midi": _manual_a41, "B_midi": _manual_b41,
        }]
        assert _session41._manual_fallback_rows(
            "booth", _manual_manifest41, {"P01 A"},
        ) == [("P01 B", _manual_b41)], \
            "manual recovery must list only the unfinished playback artifact"
        os.remove(_manual_b41)
        try:
            _session41._manual_fallback_rows("booth", _manual_manifest41, {"P01 A"})
            raise AssertionError("missing manual playback artifacts must block voting")
        except RuntimeError:
            pass
    assert _session41._validate_vote_payload(
        "booth",
        {"round_id": "r", "votes": [{"pair_id": 1, "winner": "A"}],
         "golds": ["a"]},
        [{"pair_id": 1, "A_phrase_id": "a", "B_phrase_id": "b"}],
        "r",
    ) == 1
    _ideas41 = _session41.IDEAS
    try:
        with _tmp41.TemporaryDirectory() as _legacy_ideas41:
            _session41.IDEAS = _legacy_ideas41
            _legacy_manifest41 = [{"pair_id": 1, "A_phrase_id": "a", "B_phrase_id": "b"}]
            _legacy_path41, _legacy_round41 = _session41._votes_target(
                "booth", _legacy_manifest41
            )
            assert _legacy_round41 == "", "legacy Booth rounds must stay unbound"
            assert os.path.basename(_legacy_path41) == "booth_votes_booth_legacy.json"
            _modern_manifest41 = [{
                "pair_id": 1, "A_phrase_id": "a", "B_phrase_id": "b",
                "selection_mode": "random",
            }]
            _bound_predictions41 = {"pairs": [{
                    "pair_id": 1, "A_phrase_id": "a", "B_phrase_id": "b",
                    "selection_mode": "random", "predicted_winner": "a",
                }]}
            _bound_digest41 = _brd39(_modern_manifest41, _bound_predictions41)
            _bound_predictions41.update(
                artifact_digest=_bound_digest41,
                round_id="booth_" + _bound_digest41[:12],
            )
            with open(os.path.join(_legacy_ideas41, "pairwise_predictions.json"), "w") as _f41:
                json.dump(_bound_predictions41, _f41)
            _bound_path41, _bound_round41 = _session41._votes_target(
                "booth", _modern_manifest41
            )
            assert _bound_round41 == _bound_predictions41["round_id"]
            assert os.path.basename(_bound_path41) == \
                "booth_votes_%s.json" % _bound_predictions41["round_id"]
            with open(os.path.join(_legacy_ideas41, "pairwise_predictions.json"), "w") as _f41:
                json.dump({"round_id": "booth-contract", "pairs": [{
                    "pair_id": 1, "A_phrase_id": "a", "B_phrase_id": "WRONG",
                    "selection_mode": "random", "predicted_winner": "a",
                }]}, _f41)
            try:
                _session41._votes_target("booth", _modern_manifest41)
                raise AssertionError("session must reject a sidecar from another manifest")
            except RuntimeError:
                pass
            with open(os.path.join(_legacy_ideas41, "pairwise_predictions.json"), "w") as _f41:
                json.dump({}, _f41)
            try:
                _session41._votes_target("booth", _modern_manifest41)
                raise AssertionError("malformed prediction sidecars must not downgrade to legacy")
            except RuntimeError:
                pass
    finally:
        _session41.IDEAS = _ideas41
    from ingest_audition_votes import validate_audition_vote_payload as _vav41  # noqa: E402
    _aud_manifest41 = [
        {"blind_id": 1, "round": "2b"}, {"blind_id": 2, "round": "2b"},
    ]
    assert _vav41(
        {"round": "r2b", "ratings": [{"blind_id": 1, "rating": "gold"}]},
        _aud_manifest41, "r2b",
    ) == {1: "gold"}
    for _bad_aud41 in (
        {"round": "r2b", "ratings": [
            {"blind_id": 1, "rating": "gold"}, {"blind_id": 1, "rating": "meh"}]},
        {"round": "r2b", "ratings": [{"blind_id": 99, "rating": "gold"}]},
        {"round": "r2b", "ratings": [{"blind_id": True, "rating": "gold"}]},
        {"round": "r2b", "ratings": [{"blind_id": 0, "rating": "gold"}]},
        {"round": "wrong", "ratings": [{"blind_id": 1, "rating": "gold"}]},
        {"round": "r2b", "ratings": [{"blind_id": 1, "rating": "wrong"}]},
    ):
        try:
            _vav41(_bad_aud41, _aud_manifest41, "r2b")
            raise AssertionError("invalid audition vote payload must be rejected")
        except ValueError:
            pass
    try:
        _vav41(
            {"round": "r2b", "ratings": [{"blind_id": 1, "rating": "gold"}]},
            _aud_manifest41, "r2b", require_complete=True,
        )
        raise AssertionError("incomplete audition payload must not reach ingestion")
    except ValueError:
        pass
    _mixed_aud_manifest41 = json.loads(json.dumps(_aud_manifest41))
    _mixed_aud_manifest41[1]["round"] = "1"
    try:
        _vav41(
            {"round": "r2b", "ratings": [{"blind_id": 1, "rating": "gold"}]},
            _mixed_aud_manifest41, "r2b",
        )
        raise AssertionError("audition payload must bind to one manifest round")
    except ValueError:
        pass
    try:
        _session41._votes_target("audition", _mixed_aud_manifest41)
        raise AssertionError("session must reject a mixed-round audition manifest before staging")
    except RuntimeError:
        pass
    try:
        _session41._votes_target("audition", [{"blind_id": 1}])
        raise AssertionError("automatic sessions must not guess legacy audition semantics")
    except RuntimeError:
        pass
    try:
        _vav41(
            {"round": "r2b", "ratings": [{"blind_id": 1, "rating": "gold"}]},
            [{"blind_id": 1}], "r2b", require_complete=True,
        )
        raise AssertionError("roundless manifests require the explicit legacy path")
    except ValueError:
        pass
    assert _vav41(
        {"ratings": [{"blind_id": 1, "rating": "gold"}]},
        [{"blind_id": 1}], "rlegacy", require_complete=True,
        allow_roundless_manifest=True, allow_roundless_payload=True,
    ) == {1: "gold"}
    try:
        _vav41(
            {"ratings": [{"blind_id": 1, "rating": "gold"}]},
            [{"blind_id": 1, "round": "r2"}], "r1", require_complete=True,
            allow_roundless_manifest=True, allow_roundless_payload=True,
        )
        raise AssertionError("legacy payload mode must not unbind a modern manifest round")
    except ValueError:
        pass
    print("  nested instrument tolerated; device names never pose as tracks;")
    print("  escaped quotes/braces in names safe; empty input safe")
    print("  OK\n")

    _core.set_ratings_path(None)
    # ratings_tmp, its sibling taste-model fixture, and their rolling
    # backups all live under _ratings_tmpdir: one rmtree clears the block.
    shutil.rmtree(_ratings_tmpdir, ignore_errors=True)

    if errors:
        print("FAILURES:")
        for e in errors:
            print(f"  - {e}")
        return 1

    # 42. the Moment: pure room snapshot — synthetic tracks, no Live needed
    print("--- the Moment: tempo worlds, roles, registers, blind mode ---")
    import moment as _moment42  # noqa: E402
    assert _moment42.tempo_world(75) == "downtempo"
    assert _moment42.tempo_world(122) == "club"
    assert _moment42.tempo_world(96) == "downtempo", \
        "96 is slow music played a little faster — his ~75 world, not a no-man's-land"
    # F10: decimal Live tempos must survive the parse as floats (truncation used
    # to cross a world boundary; the bands are half-open now, so the boundary
    # cases below pin the edges explicitly).
    assert _moment42.parse_tempo("liveSet: { tempo: 92.5, isPlaying:true }") == 92.5, \
        "a decimal Live tempo must survive the parse, not truncate to an int"
    assert _moment42.tempo_world(92.5) == "downtempo"
    assert _moment42.tempo_world(99.99) == "downtempo" and _moment42.tempo_world(100) == "club", \
        "100 is the one edge: downtempo below it, club from it"
    assert _moment42.tempo_world(140.5) == "club" and _moment42.tempo_world(141) == "other"
    assert _moment42.tempo_world(59.9) == "other", "below 60 is outside both worlds"
    assert _moment42.parse_tempo("liveSet: { tempo: 120 }") == 120.0, \
        "an integer tempo still parses (as a float)"
    assert _moment42.parse_tempo("liveSet: { isPlaying:true }") is None, \
        "no tempo in the overview must fall through to None, never a phantom number"
    _busy42 = [{"pitch": 36, "start": i * 0.25, "duration": 0.2, "velocity": 100}
               for i in range(32)]
    _sub42 = [{"pitch": 32, "start": 0.0, "duration": 4.0, "velocity": 90}]
    _pad42 = [{"pitch": 60 + o, "start": b * 2.0, "duration": 2.0, "velocity": 70}
              for b in range(2) for o in (0, 3, 7)]
    assert _moment42.density_band(_busy42) == "busy"
    assert _moment42.density_band(_sub42) == "sparse"
    assert _moment42.infer_role("Weird Name 7", _sub42) == "bass", "note stats fallback"
    assert _moment42.infer_role("Weird Name 8", _pad42) == "harmony", "polyphony fallback"
    assert _moment42.infer_role("batida x", _busy42) == "drum", "his naming, his language"
    _m42 = _moment42.build_moment(
        [{"name": "Drums", "notes": _busy42}, {"name": "Sub", "notes": _sub42},
         {"name": "Pad", "notes": _pad42}], tempo=75.0)
    assert _m42["ears"] == "live" and _m42["tempo_world"] == "downtempo"
    assert set(_m42["roles"]) == {"drum", "bass", "harmony"}
    assert _m42["key"], "pitched material must produce a key estimate"
    assert _m42["digest"] == _moment42.build_moment(
        [{"name": "Drums", "notes": _busy42}, {"name": "Sub", "notes": _sub42},
         {"name": "Pad", "notes": _pad42}], tempo=75.0)["digest"], "moment deterministic"
    # F1: real Producer Pal reads are MCP-shaped (start_beats/duration_beats).
    # build_moment must normalize them so a nonempty live room doesn't KeyError
    # into blind — the MCP twin must produce the byte-identical moment digest.
    def _mcp_twin42(notes):
        return [{"pitch": n["pitch"], "start_beats": n["start"],
                 "duration_beats": n["duration"], "velocity": n["velocity"]}
                for n in notes]
    _m42_mcp = _moment42.build_moment(
        [{"name": "Drums", "notes": _mcp_twin42(_busy42)},
         {"name": "Sub", "notes": _mcp_twin42(_sub42)},
         {"name": "Pad", "notes": _mcp_twin42(_pad42)}], tempo=75.0)
    assert _m42_mcp["ears"] == "live" and _m42_mcp["digest"] == _m42["digest"], \
        "MCP-shaped Producer Pal notes must build the same live moment, not go blind"
    assert set(_m42_mcp["roles"]) == {"drum", "bass", "harmony"}, \
        "the MCP-shaped room must resolve the same roles, not collapse"
    _b42 = _moment42.blind_moment("bridge down")
    assert _b42["ears"] == "blind" and _b42["roles"] == {} and _b42["key"] is None
    print("  OK\n")

    # 43. ears admission gates: filter + report, refuse loudly, never score
    print("--- ears gates: order, register/density/role, empty-set refusal ---")
    import ears as _ears43  # noqa: E402
    _room43 = _moment42.build_moment(
        [{"name": "Drums", "notes": _busy42},
         {"name": "Lead x", "notes": [
             {"pitch": p, "start": i * 1.0, "duration": 0.9, "velocity": 90}
             for i, p in enumerate((74, 77, 81, 74))]}], tempo=75.0)
    _mk43 = _room43["key"]
    _fit43 = {"id": "fit", "role": "lead", "key": _mk43, "tempo": 76,
              "notes": [{"pitch": 62, "start": 0.0, "duration": 1.0, "velocity": 80},
                        {"pitch": 60, "start": 2.0, "duration": 1.0, "velocity": 80}]}
    _fit43_mcp = {**_fit43, "notes": [
        {"pitch": n["pitch"], "start_beats": n["start"],
         "duration_beats": n["duration"], "velocity": n["velocity"]}
        for n in _fit43["notes"]]}
    _clash43 = {"id": "clash", "role": "harmony", "key": _mk43, "tempo": 76,
                "notes": [{"pitch": 75, "start": 0.0, "duration": 1.0, "velocity": 80}]}
    _wall43 = {"id": "wall", "role": "lead", "key": _mk43, "tempo": 76,
               "notes": [{"pitch": 62, "start": i * 0.25, "duration": 0.2,
                          "velocity": 80} for i in range(32)]}
    _srv43, _rep43 = _ears43.admit([_fit43, _clash43, _wall43], _room43, role="lead")
    assert [c["id"] for c in _srv43] == ["fit"], _rep43
    assert [d["gate"] for d in _rep43["dropped"]] == list(_ears43.GATE_ORDER)
    _srv_mcp43, _rep_mcp43 = _ears43.admit([_fit43_mcp], _room43, role="lead")
    assert [c["id"] for c in _srv_mcp43] == ["fit"]
    assert _rep_mcp43 == _ears43.admit([_fit43], _room43, role="lead")[1], \
        "MCP-shaped notes must produce the same register+density gate report"
    _wall_only43, _rep_wall43 = _ears43.admit([_wall43], _room43, role="lead")
    assert _wall_only43 == [] and "nearest_relaxation" in _rep_wall43, \
        "an empty survivor set must offer its nearest single-gate relaxation"
    assert "relaxing the density gate" in _rep_wall43["nearest_relaxation"]["offer"]
    try:
        _ears43.admit([_fit43], _moment42.blind_moment("down"), role="lead")
        raise AssertionError("admit must refuse blind moments — serve handles blind")
    except ValueError:
        pass
    _hopeless43 = {"id": "hopeless", "role": "lead", "key": "C major", "tempo": 122,
                   "notes": [{"pitch": 75, "start": i * 0.25, "duration": 0.2,
                              "velocity": 80} for i in range(32)]}
    _none43, _rep_none43 = _ears43.admit([_hopeless43], _room43, role="lead")
    assert _none43 == [] and _rep_none43.get("nearest_relaxation"), \
        "a multi-gate failure must still refuse with an explicit offer"
    assert _rep_none43["nearest_relaxation"]["gate"] is None
    _minor_room43 = _room43
    _major43 = {"id": "major", "role": "lead", "key": "C major", "tempo": 76,
                "notes": [{"pitch": 62, "start": 0.0, "duration": 1.0, "velocity": 80}]}
    _srv_dark43, _ = _ears43.admit([_fit43, _major43], _minor_room43,
                                   role="lead", direction="darker")
    assert [c["id"] for c in _srv_dark43] == ["fit"], "darker must drop major-mode picks"
    _dmaj43 = {"id": "dmaj", "role": "lead", "key": "D major", "tempo": 76,
               "notes": [{"pitch": 62, "start": 0.0, "duration": 1.0, "velocity": 80}]}
    _srv_dark_dmaj43, _ = _ears43.admit([_dmaj43], _minor_room43,
                                        role="lead", direction="darker")
    assert _srv_dark_dmaj43 == [], \
        "darker drops major-mode picks even at the home tonic — he asked for minor"
    _srv_sparse43, _ = _ears43.admit([_fit43, _wall43], _minor_room43,
                                     role="lead", direction="sparser")
    assert [c["id"] for c in _srv_sparse43] == ["fit"], "sparser caps density at sparse"
    _srv_weird43, _rep_weird43 = _ears43.admit([_wall43], _minor_room43,
                                               role="lead", direction="weirder")
    assert [c["id"] for c in _srv_weird43] == ["wall"], \
        "weirder skips density/register so rare shapes get through"
    assert any(d.get("skipped") for d in _rep_weird43["dropped"])
    _srv_club43, _ = _ears43.admit([_fit43], _minor_room43,
                                   role="lead", direction="clubbier")
    assert _srv_club43 == [], "clubbier forces the club tempo world"
    print("  OK\n")

    # 44. ears session memory: demote after 3, announce once, keep resets, reset clears
    print("--- ears session memory: 3-strikes demotion, announce-once, reset ---")
    with _tmp39.TemporaryDirectory() as _ears_td44:
        _sp44 = os.path.join(_ears_td44, "ears_session.json")
        _busy_cand44 = {"notes": [{"pitch": 62, "start": i * 0.25, "duration": 0.2,
                                   "velocity": 80} for i in range(32)]}
        _state44 = _ears43.load_session(_sp44)
        assert _ears43.note_verdict(_state44, _busy_cand44, kept=False) == []
        assert _ears43.note_verdict(_state44, _busy_cand44, kept=False) == []
        _ann44 = _ears43.note_verdict(_state44, _busy_cand44, kept=False)
        assert any("easing off busy lines — 'reset ears' clears it" == a
                   for a in _ann44), "the canonical demotion voice, exactly"
        assert _ears43.note_verdict(_state44, _busy_cand44, kept=False) == [], \
            "a demotion announces once, not every serve"
        assert _state44["streaks"]["density:busy"] >= _ears43.DEMOTE_AFTER, \
            "precondition: a live streak exists before the keep"
        _ears43.note_verdict(_state44, _busy_cand44, kept=True)
        assert _state44["streaks"]["density:busy"] == 0, \
            "a keep must reset a LIVE streak to zero"
        assert "density:busy" in _state44["demoted"], \
            "a keep resets the streak but never un-demotes — demote, not ban"
        _ears43.save_session(_state44, _sp44)
        assert "density:busy" in _ears43.load_session(_sp44)["demoted"]
        _ears43.reset_session(_sp44)
        assert not os.path.exists(_sp44), "reset ears deletes the session file"
    print("  OK\n")

    # 45. serve(): blind is loud, demotions reorder without banning, one voice
    print("--- ears serve: blind declaration, demote-to-back, status line ---")
    _blind45 = _moment42.blind_moment("bridge down")
    _r45 = _ears43.serve([_fit43, _wall43], _blind45)
    assert _r45["ears"] == "blind" and len(_r45["served"]) == 2, \
        "blind mode serves exactly as today — declared, not degraded"
    assert any("context-blind" in a for a in _r45["announcements"])
    assert _ears43.status_line(_blind45) == "no ears — serving context-blind"
    # F8a: an unknown direction must fail loud even on a blind moment — validated
    # at the top of serve(), before the blind short-circuit, so "moist" never
    # falls open into an ungated serve just because the room can't be heard.
    try:
        _ears43.serve([_fit43], _blind45, direction="moist")
        raise AssertionError("serve must reject an unknown direction even when blind")
    except ValueError as _exc45:
        assert "moist" in str(_exc45), "the refusal names the offending word"
    with _tmp39.TemporaryDirectory() as _ears_td45:
        _sp45 = os.path.join(_ears_td45, "ears_session.json")
        _st45 = _ears43.load_session(_sp45)
        for _ in range(3):
            _ears43.note_verdict(_st45, _fit43, kept=False)
        _ears43.save_session(_st45, _sp45)
        _r45b = _ears43.serve([_fit43, _wall43], _room43, role="lead",
                              session_path=_sp45)
        assert _r45b["served"], "demotion must never empty the served list"
        assert _r45b["served"][-1]["id"] == "fit", "demoted buckets go to the back"
        assert any("easing off" in a and "reset ears" in a
                   for a in _r45b["announcements"]), \
            "serve speaks the canonical demotion voice, not raw bucket keys"
        # a genuine two-survivor partition: fit-rise escapes every bucket demoted
        # above (density:sparse, register:mid, contour:falling) except contour,
        # so only its rising contour keeps it out of the back.
        _fit_rise45 = {"id": "fit-rise", "role": "lead", "key": _fit43["key"],
                       "tempo": 76,
                       "notes": [{"pitch": 74, "start": 0.0, "duration": 1.0,
                                  "velocity": 80},
                                 {"pitch": 79, "start": 1.0, "duration": 1.0,
                                  "velocity": 80}]}
        _st45b = _ears43.load_session(_sp45)
        # _sp45 already demoted _fit43's buckets (3 rejects above) — falling included
        assert "contour:falling" in _st45b["demoted"]
        _r45c = _ears43.serve([_fit43, _fit_rise45], _room43, role="lead",
                              session_path=_sp45)
        assert [c["id"] for c in _r45c["served"]] == ["fit-rise", "fit"], \
            "a genuine two-survivor partition: demoted bucket to the back, stable"
        assert any("easing off" in a and "reset ears" in a
                   for a in _r45c["announcements"]), \
            "serve speaks the canonical demotion voice, not raw bucket keys"
        assert not any("density:" in a or "contour:" in a
                       for a in _r45c["announcements"]), \
            "raw bucket keys never reach the readout"
    _line45 = _ears43.status_line(_room43, _r45b["gate_report"])
    assert "fit: " in _line45 and "75" in _line45, _line45
    assert "HOMEM" not in _line45 and "demosong" not in _line45, \
        "state, never identity — no names on the chassis"
    print("  OK\n")

    # 46. ears wiring: off = byte-identical, on = reported, MCP surface exposes it
    print("--- ears wiring: default off, blind declared, wrappers expose params ---")
    # explicit use_ears=False everywhere an off-path pin must NOT depend on the
    # promoted default (fix round: promotion must not turn the suite red —
    # once store/ears_default.json flips true, a bare call would pick up ears)
    _r_off46 = _core.audition_leads(bars=4, key="D minor", n=3, seed=1, use_ears=False)
    assert "ears" not in _r_off46 and "gate_report" not in _r_off46, \
        "use_ears off must leave results byte-identical"
    _r_on46 = _core.audition_leads(bars=4, key="D minor", n=3, seed=1,
                                   use_ears=True, moment=_room43)
    assert _r_on46.get("ears") == "live" and "ears_status" in _r_on46
    _r_blind46 = _core.audition_leads(bars=4, key="D minor", n=3, seed=1,
                                      use_ears=True,
                                      moment=_moment42.blind_moment("down"))
    assert _r_blind46.get("ears") == "blind" and len(_r_blind46["candidates"]) == 3, \
        "blind mode must serve the full pool, declared"
    _r_off_grow46 = _core.suggest_growth(n=3, key="D minor", seed=1, use_ears=False)
    assert "ears" not in _r_off_grow46 and "gate_report" not in _r_off_grow46, \
        "suggest_growth with ears explicitly off must stay byte-identical"
    assert _core._ears_default() is False
    # promotion probe: isolate the store dir (same pattern as blocks 36/37) so a
    # flipped ears_default.json is exercised WITHOUT touching the real store,
    # and a bare (no use_ears=) call is proven to follow that promoted default.
    # NOTE: by this point in the suite the ratings-path override is already
    # back to None/real-store (see the cleanup right before block 42), which
    # is exactly why the plain `_ears_default() is False` asserts above/below
    # this probe are checking the REAL store, untouched.
    import shutil as _sh46b
    import tempfile as _tf46b
    _flagdir46 = _tf46b.mkdtemp(prefix="ears_default46_")
    try:
        _core.set_ratings_path(os.path.join(_flagdir46, "ratings.json"))
        with open(os.path.join(_flagdir46, "ears_default.json"), "w") as _fh46:
            json.dump({"use_ears_default": True}, _fh46)
        assert _core._ears_default() is True, \
            "an isolated store dir carrying the flag file must read as promoted"
        _r_default_on46 = _core.audition_leads(bars=4, key="D minor", n=3, seed=1,
                                               moment=_room43)  # bare use_ears=
        assert _r_default_on46.get("ears") == "live" and "gate_report" in _r_default_on46, \
            "a bare call must follow the promoted default, not require use_ears=True"
        # F11: the flag must be a real boolean — the string "false" is truthy and
        # bool() would silently ENABLE the ears. A non-boolean value fails loud.
        with open(os.path.join(_flagdir46, "ears_default.json"), "w") as _fh46:
            json.dump({"use_ears_default": "false"}, _fh46)
        try:
            _core._ears_default()
            raise AssertionError("a string flag value must raise, never enable the ears")
        except ValueError as _exc46:
            assert "malformed" in str(_exc46), "the malformed-flag error must name the fix"
    finally:
        _core.set_ratings_path(None)  # restore the ambient real-store path, not ratings_tmp
        _sh46b.rmtree(_flagdir46, ignore_errors=True)
    assert _core._ears_default() is False, \
        "the REAL store, untouched, must still read OFF after the probe"
    for _tool46 in (find_phrases, answer_my_loop, audition_leads, suggest_growth):
        _fn46 = getattr(_tool46, "fn", _tool46)
        _params46 = _inspect4.signature(_fn46).parameters
        assert "use_ears" in _params46 and "direction" in _params46, \
            "the MCP surface must expose the ears, not only core"
    for _gesture46 in (hear_the_room, reset_ears):
        assert callable(getattr(_gesture46, "fn", _gesture46))
    # fix round 1: answer_my_loop must refuse on zero survivors, never
    # auto-relax into an ungated pick (spec §8). The single-track room from
    # block 43 (_room43) only zeroes the tempo gate for this seed/bank — the
    # register/density/role gates are no-ops when the room has just one role
    # (there's no *other* role's registered band for a "lead" candidate to
    # collide with). Strengthened here with a mid-register "harmony" track
    # (reusing _pad42 from block 42) and a high-register "drum" track so both
    # bands the surviving catalog phrases occupy are already claimed by a
    # different role — that empties the bank for this seed, deterministically.
    _hopeless_room46 = _moment42.build_moment(
        [{"name": "Lead x", "notes": [{"pitch": 74 + (i % 3), "start": i * 0.25,
                                       "duration": 0.2, "velocity": 90}
                                      for i in range(32)]},
         {"name": "Pad", "notes": _pad42},
         {"name": "Hats", "notes": [{"pitch": 90, "start": i * 0.5,
                                     "duration": 0.1, "velocity": 60}
                                    for i in range(8)]}], tempo=122.0)
    _seed_notes46 = [{"pitch": 62, "start_beats": b * 1.0, "duration_beats": 0.9,
                      "velocity": 80} for b in range(4)]
    _r_nofit46 = _core.answer_my_loop(
        seed_notes=_seed_notes46, use_ears=True, moment=_hopeless_room46)
    if _r_nofit46.get("gate_report", {}).get("survived") == 0:
        assert _r_nofit46.get("no_fit") is True and not _r_nofit46.get("answer_notes"), \
            "zero survivors must refuse with an offer, never serve an ungated pick"
        assert _r_nofit46.get("announcements"), "the refusal carries the offer"
    else:
        raise AssertionError(
            "the hopeless room no longer zeroes the bank for this seed — "
            "strengthen it further; this assert must not go unreachable "
            f"(gate_report={_r_nofit46.get('gate_report')})")
    _r_off_ans46 = _core.answer_my_loop(seed_notes=_seed_notes46, use_ears=False)
    assert "ears" not in _r_off_ans46 and "no_fit" not in _r_off_ans46, \
        "answer_my_loop with ears off must stay byte-identical — no ears/no_fit keys"
    _r_dir46 = _core.answer_my_loop(
        seed_notes=_seed_notes46, direction="brighter", use_ears=True, moment=_room43)
    assert isinstance(_r_dir46, dict), \
        "gearbox-only direction words must not crash the ears path"
    assert any("gearbox word" in a and "brighter" in a
               for a in _r_dir46.get("announcements", [])), \
        "a gearbox-only direction word must announce gates-unbent, not silently no-op"
    _r_dark46 = _core.answer_my_loop(
        seed_notes=_seed_notes46, direction="darker", use_ears=True, moment=_room43)
    _r_dark_title46 = _core.answer_my_loop(
        seed_notes=_seed_notes46, direction="Darker", use_ears=True, moment=_room43)
    assert _r_dark_title46 == _r_dark46, \
        "answer boundary must treat case variants as the same Ears direction"
    assert _r_dark_title46["gate_report"]["direction"] == "darker"
    assert _r_dark_title46["announcements"] == _r_dark46["announcements"], \
        "case variants must preserve the same Ears announcement behavior"
    # F8b: a word in NEITHER vocabulary (not an ears gate word, not a gearbox pole)
    # must raise, not masquerade as a "gearbox word" that leaves the gates unbent.
    try:
        _core.answer_my_loop(seed_notes=_seed_notes46, direction="moist",
                             use_ears=True, moment=_room43)
        raise AssertionError("an unknown direction must raise on the answer path")
    except ValueError as _exc46b:
        assert "moist" in str(_exc46b) and "gearbox" in str(_exc46b), \
            "the refusal must name the word and both vocabularies"
    # F3a: the ears gates must judge each hit's PROSPECTIVE transposed register, not
    # its raw catalog register. A hit two octaves above the seed, keyed a tritone
    # away, is realized near the seed's register — _prospective_answer_notes must
    # return that realized material for the gate to see.
    _hit46 = {"id": "h", "project": "", "role": "lead",
              "notes": [{"pitch": 96, "start": 0.0, "duration": 1.0, "velocity": 90},
                        {"pitch": 99, "start": 1.0, "duration": 1.0, "velocity": 90},
                        {"pitch": 96, "start": 2.0, "duration": 1.0, "velocity": 90}]}
    _prosp46 = _core._prospective_answer_notes(_hit46, seed_tonic_pc=2,
                                               seed_last_pitch=62, seed_median=62)
    assert _prosp46, "the prospective transpose must yield notes, not drop the hit"
    assert _moment42.register_band(_prosp46) == "mid", \
        "the gate must see the realized (mid) register, not the raw (high) catalog one"
    assert _moment42.register_band(_hit46["notes"]) == "high", \
        "precondition: the raw catalog notes really do sit in a different register band"
    print("  OK\n")

    # 47. ears session-memory WRITER wiring: judge_answer feeds note_verdict.
    # Before this fix, ears.note_verdict had no production caller at all — spec
    # §4 ("session memory") was inert code. judge_answer's KEEP/DROP path is
    # the caller; this proves a verdict sequence reaches the real writer
    # functions (load_session/note_verdict/save_session), through judge_answer
    # itself (it needs no Live — only notes and the isolated session path).
    print("--- ears session writer: judge_answer -> note_verdict, through the real path ---")
    with tempfile.TemporaryDirectory() as _ears_td47:
        _sp47 = os.path.join(_ears_td47, "ears_session.json")
        _dp47 = os.path.join(_ears_td47, "develop_session.json")
        # judge_answer's KEEP/DROP path also calls core.rate() — by this point
        # in the suite the ratings-path override is back to None/real-store
        # (see block 46's note), so isolate ratings too or these writes would
        # land in store/ratings.json for real.
        _core.set_ratings_path(os.path.join(_ears_td47, "ratings.json"))
        try:
            # MCP-shaped (start_beats/duration_beats) on purpose: judge_answer
            # must normalize through core._note_dicts before ears can bucket it.
            _busy_mcp47 = [{"pitch": 62, "start_beats": i * 0.25, "duration_beats": 0.2,
                            "velocity": 80} for i in range(32)]
            for _ in range(2):
                _r47 = _gears.judge_answer("drop", answer_notes=_busy_mcp47,
                                           reason="too busy", ears_session_path=_sp47,
                                           develop_session_path=_dp47)
                assert _r47["ears_announcements"] == [], \
                    "no demotion before the 3rd same-bucket reject"
            _r47c = _gears.judge_answer("drop", answer_notes=_busy_mcp47,
                                        reason="too busy", ears_session_path=_sp47,
                                           develop_session_path=_dp47)
            assert any("easing off busy lines" in a for a in _r47c["ears_announcements"]), \
                "the 3rd same-bucket DROP through judge_answer must demote — proves the " \
                "writer is really wired, not just callable in isolation"
            # a KEEP resets the live streak but must never un-demote (demote, never ban)
            _r47keep = _gears.judge_answer("keep", answer_notes=_busy_mcp47,
                                           reason="fine here", ears_session_path=_sp47,
                                           develop_session_path=_dp47)
            assert _r47keep["ears_announcements"] == [], \
                "a keep announces nothing new — it only resets the streak"
            _state47 = _ears43.load_session(_sp47)
            assert "density:busy" in _state47["demoted"], \
                "a keep resets the streak but never un-demotes"
            # F4: session memory must learn the HEARD answer, not the resolved
            # SOURCE phrase. Point source_phrase_id at a sparse/low phrase while
            # the heard answer is busy/mid — only the heard buckets may increment.
            _sparse_src47 = [{"pitch": 40, "start_beats": 0.0,
                              "duration_beats": 4.0, "velocity": 80}]
            _heard_busy47 = [{"pitch": 62, "start_beats": i * 0.25,
                              "duration_beats": 0.2, "velocity": 80} for i in range(32)]
            _sp47f4 = os.path.join(_ears_td47, "ears_heard.json")
            _real_phrases47 = _core._phrases
            try:
                _core.set_phrase_pool_for_test([{"id": "src-x", "notes": _sparse_src47}])
                _gears.judge_answer("drop", source_phrase_id="src-x",
                                    answer_notes=_heard_busy47, reason="busy",
                                    ears_session_path=_sp47f4,
                                    develop_session_path=_dp47)
            finally:
                _core.set_phrase_pool_for_test(None)
            _state47f4 = _ears43.load_session(_sp47f4)
            assert _state47f4["streaks"].get("density:busy") == 1, \
                "the HEARD (busy) bucket must increment"
            assert _state47f4["streaks"].get("register:mid") == 1, \
                "the HEARD (mid) register bucket must increment"
            assert _state47f4["streaks"].get("density:sparse", 0) == 0 and \
                _state47f4["streaks"].get("register:low", 0) == 0, \
                "the SOURCE (sparse/low) buckets must NOT be learned — that was the bug"
            # F5: a corrupt session file must block the verdict BEFORE the rating
            # persists. Two failed retries on a FRESH ratings path leave ZERO
            # ratings (fail-closed, retry-safe) — nothing to duplicate.
            _bad_sp47 = os.path.join(_ears_td47, "corrupt_session.json")
            with open(_bad_sp47, "w") as _fh47:
                _fh47.write("{ this is not valid json")
            _f5_ratings47 = os.path.join(_ears_td47, "f5_ratings.json")
            _core.set_ratings_path(_f5_ratings47)
            for _ in range(2):
                try:
                    _gears.judge_answer("keep", answer_notes=_heard_busy47,
                                        reason="x", ears_session_path=_bad_sp47,
                                        develop_session_path=_dp47)
                    raise AssertionError("a corrupt session file must block the verdict")
                except ValueError as _exc47:
                    assert "reset ears" in str(_exc47), \
                        "the fail-closed error must name the one-gesture recovery"
            assert not os.path.exists(_f5_ratings47), \
                "two failed verdicts must persist ZERO ratings — the write never runs"
        finally:
            _core.set_ratings_path(None)
    print("  OK\n")

    # 48. answer_my_loop's write path must skip cleanly on a no_fit refusal,
    # never KeyError trying to build a clip name from keys a refusal doesn't
    # carry (source_phrase_ids/answer_notes) — a designed refusal, not a bug.
    print("--- answer_my_loop write path: no_fit + write=True skips clean ---")
    _amls_fn48 = getattr(answer_my_loop, "fn", answer_my_loop)
    _real_core_aml48 = _core.answer_my_loop
    _no_fit_result48 = {
        "no_fit": True, "answer_notes": [], "key": "D minor", "bars": 2,
        "bank_stats": {"n_bank": 0, "n_gold": 0, "n_gated_mechanical": 0},
        "assumptions": [], "ears": "live",
        "gate_report": {"pool": 3, "survived": 0, "dropped": []},
        "announcements": ["nothing fits — want the nearest relaxation?"],
    }
    try:
        _core.answer_my_loop = lambda *a, **k: dict(_no_fit_result48)
        _seed_notes48 = [{"pitch": 62, "start_beats": b * 1.0, "duration_beats": 0.9,
                          "velocity": 80} for b in range(4)]
        _r48 = _amls_fn48(seed_notes=_seed_notes48, write=True, use_ears=True)
    finally:
        _core.answer_my_loop = _real_core_aml48
    assert _r48.get("no_fit") is True, "the refusal shape must reach the caller unchanged"
    assert _r48.get("live_write_skipped") == "no fit — nothing to write", \
        "write=True on a no_fit result must skip cleanly with a clear reason, not KeyError"
    assert "live_write" not in _r48 and "live_write_error" not in _r48, \
        "a skipped write must not also report a write attempt or a write error"
    print("  OK\n")

    # 49. live_write._call must raise on a Producer Pal tool error, never pass the
    # error text back as if it were a normal read (F2). Before this fix, read_clip
    # parsed the error text as zero notes and the moment kept ears:"live" with the
    # track invisibly missing. No Live needed: a fake session + isError result.
    print("--- live_write._call: Producer Pal isError raises, never silent zero ---")
    import live_write as _lw49  # noqa: E402

    class _FakeContent49:
        def __init__(self, text):
            self.text = text

    class _FakeResult49:
        def __init__(self, text, is_error):
            self.content = [_FakeContent49(text)]
            self.isError = is_error

    class _FakeSession49:
        def __init__(self, text, is_error):
            self._text, self._is_error = text, is_error

        async def call_tool(self, name, args):
            _pp_session._selftest_schema_check(name, args)
            return _FakeResult49(self._text, self._is_error)

    _ok49 = asyncio.run(_lw49._call(_FakeSession49("notes: 'C3 1|1'", False), "ppal-read-clip"))
    assert _ok49 == "notes: 'C3 1|1'", "a non-error result must pass its text through unchanged"
    try:
        asyncio.run(_lw49._call(_FakeSession49("track index 3 out of range", True),
                                "ppal-read-clip"))
        raise AssertionError("_call must raise on a Producer Pal tool error, not return its text")
    except RuntimeError as _exc49:
        assert "Producer Pal tool error" in str(_exc49) and "out of range" in str(_exc49), \
            "the raised error must name the failure so the moment can declare blind"
    print("  OK\n")

    # 50. no_fit fold-in (T1): octave_displacement arg, gears guards, sombra skip-write
    print("--- no_fit fold-in (T1) ---")
    import gears as _gears50  # noqa: E402
    import sombra_listener as _sl50  # noqa: E402
    import live_write as _lw50  # noqa: E402

    _nd50 = [{"pitch": 62, "start": 0.0, "duration": 0.5, "velocity": 80},
             {"pitch": 65, "start": 0.5, "duration": 0.5, "velocity": 80},
             {"pitch": 69, "start": 1.0, "duration": 0.5, "velocity": 80}]
    _oct50 = _gears50.apply_surgery(_nd50, "octave_displacement", "D minor", seed=0)
    assert isinstance(_oct50, list) and len(_oct50) > 0, \
        "octave_displacement must return a non-empty note list (no TypeError)"

    _no_fit50 = {"no_fit": True, "answer_notes": [], "assumptions": []}
    _real_aml50 = _core.answer_my_loop
    _seed50 = [{"pitch": 62, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 80},
               {"pitch": 65, "start_beats": 0.5, "duration_beats": 0.5, "velocity": 80},
               {"pitch": 67, "start_beats": 1.0, "duration_beats": 0.5, "velocity": 80}]
    try:
        _core.answer_my_loop = lambda *a, **k: dict(_no_fit50)
        _g1_50 = _gears50.answer_with_gear(_seed50, gear=1, key="D minor", seed=0)
        assert _g1_50.get("no_fit") is True and _g1_50.get("gear") == 1, \
            "gear 1 must return the refusal with gear set, not KeyError on source_phrase_ids"
        _g3_50 = _gears50.answer_with_gear(_seed50, gear=3, key="D minor", seed=0)
        assert _g3_50.get("no_fit") is True and _g3_50.get("gear") == 3, \
            "gear 3 must return the refusal with gear set, not crash in apply_surgery"
    finally:
        _core.answer_my_loop = _real_aml50

    async def _raise_if_write50(*a, **k):
        raise AssertionError("write_answer_to_jam_track must not be called on no_fit")

    _orig_sel50 = _sl50._selection_notes
    _orig_write50 = _lw50.write_answer_to_jam_track
    try:
        _sl50._selection_notes = lambda: _seed50
        _lw50.write_answer_to_jam_track = _raise_if_write50
        _core.answer_my_loop = lambda *a, **k: dict(_no_fit50)
        _res50 = _sl50._execute({"tool": "answer_my_loop", "tool_args": {"gear": 1}})
        assert _res50.get("no_fit") is True, \
            "sombra_listener must surface the no_fit refusal unchanged"
        assert "slot" not in _res50, \
            "no_fit must skip the jam-track write entirely"
    finally:
        _sl50._selection_notes = _orig_sel50
        _lw50.write_answer_to_jam_track = _orig_write50
        _core.answer_my_loop = _real_aml50
    print("  OK\n")

    # 51. T2: the worker refactor — _answer_worker spine shared by
    # answer_my_loop/develop_my_loop; byte-identical move=None/transform=None;
    # pin_source honoured (+ absent-pin raises); unknown move/transform raise.
    print("--- _answer_worker spine: develop_my_loop == answer_my_loop(transform=None) ---")
    from build_index import phrase_vector as _pv51  # noqa: E402
    _saved_agg51 = _core._CACHE.get("aggregate")
    _saved_pk51 = _core._CACHE.get("project_keys")
    try:
        _core._CACHE["aggregate"] = {
            "melodic_grammar_by_role": {"lead": {"unigram": {"0": 0.6, "3": 0.2, "7": 0.2},
                                                 "order_1": {}, "order_2": {}}},
            "tempo": {"mean": 120.0},
        }
        _core._CACHE["project_keys"] = {"pG": "G minor"}

        def _idx_phrase51(pid, project, role, source, pitches, starts, vels, durs):
            notes = [{"pitch": p, "start": s, "duration": d, "velocity": v}
                     for p, s, v, d in zip(pitches, starts, vels, durs)]
            return {"id": pid, "project": project, "role": role, "kind": "harmonic",
                    "clip_source": source, "tempo": 122.0, "n_notes": len(notes),
                    "notes": notes, "vec": _pv51(notes)}

        # a fixed synthetic seed phrase; candidates span >= 1 bar so a fragment
        # transform doesn't need re-tiling — keeps transforms_applied's tail
        # legible for the "wrapper resolution intact" assertion below.
        _seed51 = [{"pitch": p, "start_beats": s, "duration_beats": 0.45, "velocity": 90}
                   for p, s in zip([62, 65, 69, 67, 65, 62], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0])]
        _durs51 = [0.45] * 6 + [0.6]
        _pool51 = [
            _idx_phrase51("pG|c0|w0", "pG", "lead", "session",
                          [67, 70, 74, 72, 70, 65, 67], [0.0, 0.5, 1.0, 2.0, 2.5, 3.0, 3.75],
                          [91, 63, 77, 84, 58, 96, 72], _durs51),
            _idx_phrase51("pX|other", "pX", "lead", "session",
                          [60, 63, 67, 65, 63, 60, 62], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 3.75],
                          [80, 66, 74, 88, 60, 92, 70], _durs51),
            _idx_phrase51("echo|seed", "pE", "lead", "session",
                          [62, 65, 69, 67, 65, 62, 64], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 3.75],
                          [90] * 7, _durs51),
        ]
        _core.set_phrase_pool_for_test(_pool51)

        # byte-identical: develop_my_loop(move=None) must equal
        # answer_my_loop(transform=None), literally — the whole point of
        # sharing one worker.
        _dev51 = _core.develop_my_loop(_seed51, move=None, key="D minor", seed=1)
        _ans51 = _core.answer_my_loop(_seed51, transform=None, key="D minor", seed=1)
        assert _dev51 == _ans51, \
            "develop_my_loop(move=None) must equal answer_my_loop(transform=None) byte-for-byte"
        assert "develop_move" not in _dev51, "no move ran — develop_move must be absent"

        # wrapper resolution intact: transform="fragment" still resolves through
        # the worker, and its label lands as the transform's own applied entry.
        _ans_frag51 = _core.answer_my_loop(_seed51, transform="fragment", key="D minor", seed=1)
        assert _ans_frag51["transforms_applied"][-1] == "fragment", _ans_frag51["transforms_applied"]

        # unknown move raises, naming the live registry (empty until later
        # Hands tasks register moves — the message must still name it honestly)
        try:
            _core.develop_my_loop(_seed51, move="not-a-real-move", key="D minor", seed=1)
            raise AssertionError("an unknown move must raise")
        except ValueError as _exc51:
            assert "not-a-real-move" in str(_exc51), str(_exc51)
            assert str(sorted(_core._DEVELOP_TRANSFORMS)) in str(_exc51), \
                "the error must name the live roster, not a stale/hardcoded one"

        # unknown transform still raises the existing ValueError, unchanged
        try:
            _core.answer_my_loop(_seed51, transform="not-a-real-transform", key="D minor", seed=1)
            raise AssertionError("an unknown transform must raise")
        except ValueError as _exc51b:
            assert "transform must be one of" in str(_exc51b), str(_exc51b)

        # pin_source: monkeypatch-free — call the worker directly with a real
        # id drawn from a prior answer's source_phrase_ids for the same seed.
        _pinned_id51 = _ans51["source_phrase_ids"][0]
        _pinned51 = _core._answer_worker(
            _seed51, key="D minor", tempo=None, bars=None, seed=1, humanize=False,
            transform_fn=None, prospective_fn=_core._prospective_answer_notes,
            use_ears=None, direction=None, moment=None, pin_source=_pinned_id51)
        assert _pinned51["source_phrase_ids"] == [_pinned_id51], _pinned51["source_phrase_ids"]
        # ...and the discriminating case: pin an id that is NOT the default
        # seed-pick's. The assert above alone is tautological — it pins exactly
        # the id the un-pinned call chose, so it would pass even if pin_source
        # silently fell through to the default pick. Compute the other id from
        # the pool (never hardcode — the test must not rot if ranking changes)
        # and prove the pin OVERRIDES the default pick; sticky develop (T11)
        # rides this mechanism.
        _other_id51 = next(p["id"] for p in _pool51 if p["id"] != _pinned_id51)
        _pinned_other51 = _core._answer_worker(
            _seed51, key="D minor", tempo=None, bars=None, seed=1, humanize=False,
            transform_fn=None, prospective_fn=_core._prospective_answer_notes,
            use_ears=None, direction=None, moment=None, pin_source=_other_id51)
        assert _pinned_other51["source_phrase_ids"] == [_other_id51], \
            _pinned_other51["source_phrase_ids"]
        assert _pinned_other51["source_phrase_ids"] != _ans51["source_phrase_ids"], \
            "the pinned pick must override the default seed-pick, not coincide with it"

        try:
            _core._answer_worker(
                _seed51, key="D minor", tempo=None, bars=None, seed=1, humanize=False,
                transform_fn=None, prospective_fn=_core._prospective_answer_notes,
                use_ears=None, direction=None, moment=None,
                pin_source="nonexistent|id|xyz")
            raise AssertionError("an absent pin_source must raise")
        except ValueError as _exc51c:
            assert "nonexistent|id|xyz" in str(_exc51c), str(_exc51c)

        # use_ears=False through develop_my_loop must add no ears keys
        _dev_off51 = _core.develop_my_loop(_seed51, move=None, key="D minor", seed=1,
                                           use_ears=False)
        assert "ears" not in _dev_off51 and "gate_report" not in _dev_off51, \
            "develop_my_loop with ears explicitly off must add no ears keys"
    finally:
        _core.set_phrase_pool_for_test(None)
        if _saved_agg51 is None:
            _core._CACHE.pop("aggregate", None)
        else:
            _core._CACHE["aggregate"] = _saved_agg51
        if _saved_pk51 is None:
            _core._CACHE.pop("project_keys", None)
        else:
            _core._CACHE["project_keys"] = _saved_pk51
    print("  develop_my_loop(move=None) == answer_my_loop(transform=None) byte-for-byte;")
    print("  fragment wrapper resolution intact; unknown move/transform raise naming the roster;")
    print("  pin_source honoured direct-to-worker + absent-pin raises; ears-off adds no keys")
    print("  OK\n")

    # 52. T3: self-calibrated legal degree set + nearest-PC snap (spec §2, §10).
    print("--- legal degree set + snap (T3) ---")
    _tonic52 = 60  # C4 — pitch-class reference for degree arithmetic
    _alphabet52 = {0, 3, 5, 7, 10}
    _pure52 = [
        {"pitch": _tonic52 + d, "start_beats": float(i), "duration_beats": 0.5, "velocity": 80}
        for i, d in enumerate((0, 3, 5, 7, 10))
    ]
    _legal_pure52 = _core._legal_degree_set(_pure52, _tonic52)
    assert _legal_pure52 == _alphabet52, _legal_pure52

    _with_two52 = _pure52 + [
        {"pitch": _tonic52 + 2, "start_beats": 5.0, "duration_beats": 0.5, "velocity": 80},
    ]
    assert 2 in _core._legal_degree_set(_with_two52, _tonic52)

    _no_eleven52 = _core._legal_degree_set(_pure52, _tonic52)
    assert 11 not in _no_eleven52
    _with_eleven52 = _pure52 + [
        {"pitch": _tonic52 + 11, "start_beats": 6.0, "duration_beats": 0.5, "velocity": 80},
    ]
    assert 11 in _core._legal_degree_set(_with_eleven52, _tonic52)

    _legal52 = _legal_pure52
    _legal_pitch52 = _tonic52 + 5
    assert _core._snap_to_legal(_legal_pitch52, _tonic52, _legal52) == _legal_pitch52

    _illegal52 = _tonic52 + 4  # degree 4 — equidistant between 3 and 5
    _snapped52 = _core._snap_to_legal(_illegal52, _tonic52, _legal52)
    assert (_snapped52 - _tonic52) % 12 in _legal52
    assert abs(_snapped52 - _illegal52) <= 6
    assert _snapped52 == _tonic52 + 3, "equidistant tie must snap downward (degree 3 not 5)"

    _empty_legal52 = _core._legal_degree_set([], _tonic52)
    assert _empty_legal52 == _alphabet52
    _snapped_empty52 = _core._snap_to_legal(_illegal52, _tonic52, _empty_legal52)
    assert (_snapped_empty52 - _tonic52) % 12 in _empty_legal52
    assert abs(_snapped_empty52 - _illegal52) <= 6

    assert _core._snap_to_legal(_illegal52, _tonic52, _legal52) == _snapped52
    assert _core._legal_degree_set(_pure52, _tonic52) == _legal_pure52
    print("  alphabet-only phrase -> {0,3,5,7,10}; self-calibration for degrees 2 and 11;")
    print("  snap identity / legality / ±6 / downward tie-break; empty phrase snaps to alphabet;")
    print("  deterministic across repeated calls")
    print("  OK\n")

    # 53. T4: slow-burn:reveal + pass ramp (spec §3.1, §10 "Slow-burn ramp").
    # _slow_burn_reveal is pure selection; _DEVELOP_TRANSFORMS wires
    # "slow-burn"/"slow-burn:reveal" through develop_my_loop's new needs_seed
    # partial (pass_index = min(seed, _SLOW_BURN_PASSES - 1)).
    print("--- slow-burn:reveal + pass ramp (T4) ---")
    assert _core._SLOW_BURN_PASSES == 4

    # unit-level: provenance + ramp on a fixed phrase, isolated from
    # retrieval — develop_my_loop's own `seed` ALSO selects the rank-pick
    # (`ranked[seed % ...]`), so sweeping seed through the full surface would
    # conflate "the pick changed" with "the pass changed"; this checks
    # _slow_burn_reveal's own contract directly instead.
    _nd53 = [
        {"pitch": 60, "start": 0.0, "duration": 0.4, "velocity": 90},
        {"pitch": 62, "start": 0.5, "duration": 0.2, "velocity": 70},
        {"pitch": 64, "start": 1.0, "duration": 0.9, "velocity": 100},
        {"pitch": 65, "start": 2.0, "duration": 0.3, "velocity": 60},
        {"pitch": 67, "start": 2.5, "duration": 0.3, "velocity": 65},
        {"pitch": 69, "start": 3.0, "duration": 0.5, "velocity": 95},
        {"pitch": 71, "start": 4.0, "duration": 0.4, "velocity": 80},
        {"pitch": 72, "start": 5.0, "duration": 0.2, "velocity": 55},
        {"pitch": 74, "start": 6.0, "duration": 1.0, "velocity": 110},
        {"pitch": 76, "start": 7.5, "duration": 0.3, "velocity": 66},
    ]
    _ids53 = {id(n) for n in _nd53}
    _tuples53 = {(n["pitch"], n["start"], n["duration"], n["velocity"]) for n in _nd53}
    _prev_count53 = None
    for _p53 in range(_core._SLOW_BURN_PASSES):
        _out53 = _core._slow_burn_reveal(_nd53, _p53)
        # provenance, both ways the task frames it: true object identity
        # (we return originals) and the portable (pitch,start,duration,
        # velocity)-tuple membership.
        assert all(id(n) in _ids53 for n in _out53), \
            "every reveal note must be one of the input's own objects"
        assert all((n["pitch"], n["start"], n["duration"], n["velocity"]) in _tuples53
                   for n in _out53), "reveal notes must match an input note's full tuple"
        if _prev_count53 is not None:
            assert len(_out53) >= _prev_count53, \
                f"pass {_p53} ({len(_out53)} notes) must be >= the previous pass ({_prev_count53})"
        _prev_count53 = len(_out53)
    assert _core._slow_burn_reveal(_nd53, _core._SLOW_BURN_PASSES - 1) == list(_nd53), \
        "the last pass must return the full phrase unchanged"
    assert _core._slow_burn_reveal(_nd53, 99) == list(_nd53), \
        "an over-range pass_index must clamp to the full phrase"
    assert _core._slow_burn_reveal(_nd53, -5) == _core._slow_burn_reveal(_nd53, 0), \
        "a negative pass_index must clamp to pass 0"
    assert _core._slow_burn_reveal(_nd53, 1) == _core._slow_burn_reveal(_nd53, 1), \
        "deterministic across repeated calls"

    # surface: registry wiring + needs_seed partial through develop_my_loop,
    # against a synthetic pool (block 51's pattern; restore caches in finally).
    from build_index import phrase_vector as _pv53  # noqa: E402
    _saved_agg53 = _core._CACHE.get("aggregate")
    _saved_pk53 = _core._CACHE.get("project_keys")
    try:
        _core._CACHE["aggregate"] = {
            "melodic_grammar_by_role": {"lead": {"unigram": {"0": 0.6, "3": 0.2, "7": 0.2},
                                                 "order_1": {}, "order_2": {}}},
            "tempo": {"mean": 120.0},
        }
        _core._CACHE["project_keys"] = {"pG": "G minor"}

        def _idx_phrase53(pid, project, role, source, pitches, starts, vels, durs):
            notes = [{"pitch": p, "start": s, "duration": d, "velocity": v}
                     for p, s, v, d in zip(pitches, starts, vels, durs)]
            return {"id": pid, "project": project, "role": role, "kind": "harmonic",
                    "clip_source": source, "tempo": 122.0, "n_notes": len(notes),
                    "notes": notes, "vec": _pv53(notes)}

        _seed53 = [{"pitch": p, "start_beats": s, "duration_beats": 0.45, "velocity": 90}
                   for p, s in zip([62, 65, 69, 67, 65, 62], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0])]
        _durs53 = [0.45] * 6 + [0.6]
        # the pin target: long enough (10 notes, spanning 2 bars) that its
        # reveal genuinely differs pass over pass.
        _pool53 = [
            _idx_phrase53("pG|c0|w0", "pG", "lead", "session",
                          [67, 70, 74, 72, 70, 65, 67, 69, 71, 73],
                          [0.0, 0.5, 1.0, 2.0, 2.5, 3.0, 3.75, 5.0, 6.0, 7.0],
                          [91, 63, 77, 84, 58, 96, 72, 80, 75, 60],
                          [0.45, 0.45, 0.45, 0.45, 0.45, 0.45, 0.6, 0.4, 0.4, 1.0]),
            _idx_phrase53("pX|other", "pX", "lead", "session",
                          [60, 63, 67, 65, 63, 60, 62], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 3.75],
                          [80, 66, 74, 88, 60, 92, 70], _durs53),
            _idx_phrase53("echo|seed", "pE", "lead", "session",
                          [62, 65, 69, 67, 65, 62, 64], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 3.75],
                          [90] * 7, _durs53),
        ]
        _core.set_phrase_pool_for_test(_pool53)
        _pin_id53 = _pool53[0]["id"]

        # develop_move + transforms_applied name the pass; ears-off adds no keys
        _dev53 = _core.develop_my_loop(_seed53, move="slow-burn:reveal", key="D minor",
                                       seed=1, use_ears=False)
        assert _dev53["develop_move"] == "slow-burn:reveal"
        assert _dev53["transforms_applied"][-1] == "slow-burn:reveal:pass1", \
            _dev53["transforms_applied"]
        assert "ears" not in _dev53 and "gate_report" not in _dev53, \
            "use_ears=False must add no ears keys"

        # bare "slow-burn" dispatches identically to "slow-burn:reveal"
        _bare53 = _core.develop_my_loop(_seed53, move="slow-burn", key="D minor",
                                        seed=2, use_ears=False)
        assert _bare53["develop_move"] == "slow-burn"
        assert _bare53["transforms_applied"][-1] == "slow-burn:pass2", _bare53["transforms_applied"]

        # pass_index clamps: seed=99 lands on pass3 like seed=3. The pick walks
        # ranked[seed % min(len, 6)] over the deduped list (8 with a ref),
        # so 99 and 3 may pick different phrases when the list is short —
        # but the same seed always picks the same one.
        _s99 = _core.develop_my_loop(_seed53, move="slow-burn:reveal", key="D minor",
                                     seed=99, use_ears=False)
        _s99b = _core.develop_my_loop(_seed53, move="slow-burn:reveal", key="D minor",
                                      seed=99, use_ears=False)
        _s3 = _core.develop_my_loop(_seed53, move="slow-burn:reveal", key="D minor",
                                    seed=3, use_ears=False)
        assert _s99["answer_notes"] == _s99b["answer_notes"]
        assert _s99["transforms_applied"][-1] == "slow-burn:reveal:pass3" == _s3["transforms_applied"][-1]

        # last pass == the untouched (move=None) answer for the same pick
        _none3 = _core.develop_my_loop(_seed53, move=None, key="D minor", seed=3, use_ears=False)
        assert _s3["answer_notes"] == _none3["answer_notes"]

        # determinism
        _s2a = _core.develop_my_loop(_seed53, move="slow-burn:reveal", key="D minor",
                                     seed=2, use_ears=False)
        _s2b = _core.develop_my_loop(_seed53, move="slow-burn:reveal", key="D minor",
                                     seed=2, use_ears=False)
        assert _s2a == _s2b

        # ramp through the real pipeline (transpose/tile/finalize) with the
        # pick held fixed via pin_source, since develop_my_loop's own seed
        # cannot isolate pass from pick (see the unit-level note above).
        _full_pinned53 = _core._answer_worker(
            _seed53, key="D minor", tempo=None, bars=1, seed=0, humanize=False,
            transform_fn=None, prospective_fn=_core._prospective_answer_notes,
            use_ears=False, direction=None, moment=None, pin_source=_pin_id53)
        _pinned_notes53 = []
        for _p53 in range(_core._SLOW_BURN_PASSES):
            _tf53 = (f"slow-burn:reveal:pass{_p53}",
                    lambda nd, _p=_p53: _core._slow_burn_reveal(nd, _p))
            _r53 = _core._answer_worker(
                _seed53, key="D minor", tempo=None, bars=1, seed=0, humanize=False,
                transform_fn=_tf53, prospective_fn=_core._prospective_answer_notes,
                use_ears=False, direction=None, moment=None, pin_source=_pin_id53)
            assert _r53["source_phrase_ids"] == [_pin_id53], "pin_source must hold the pick fixed"
            assert not any(a.startswith("tile:") for a in _r53["transforms_applied"]), \
                "this fixture must not need re-tiling — it would blur the ramp with tile copies"
            _pinned_notes53.append(_r53["answer_notes"])
        _pinned_counts53 = [len(x) for x in _pinned_notes53]
        assert _pinned_counts53 == sorted(_pinned_counts53), \
            f"pinned-source reveal counts must be non-decreasing across passes: {_pinned_counts53}"
        assert _pinned_notes53[-1] == _full_pinned53["answer_notes"], \
            "the last pass must equal the untouched (transform=None) answer for the same pinned pick"
    finally:
        _core.set_phrase_pool_for_test(None)
        if _saved_agg53 is None:
            _core._CACHE.pop("aggregate", None)
        else:
            _core._CACHE["aggregate"] = _saved_agg53
        if _saved_pk53 is None:
            _core._CACHE.pop("project_keys", None)
        else:
            _core._CACHE["project_keys"] = _saved_pk53
    print("  pure selection, provenance by identity + tuple; count/set monotone non-decreasing;")
    print("  last pass == full phrase; negative/over-range pass_index clamps;")
    print("  develop_move + transforms_applied name the pass; bare 'slow-burn' == reveal;")
    print("  ears-off adds no keys; deterministic")
    print("  OK\n")

    # 54. T5: slow-burn:accent — DNA re-voicing, bounded, resolves home (spec §3.2, §10).
    print("--- slow-burn:accent (T5) ---")
    _nd54 = [
        {"pitch": 60, "start": 0.0, "duration": 0.4, "velocity": 90},
        {"pitch": 62, "start": 0.5, "duration": 0.2, "velocity": 70},
        {"pitch": 64, "start": 1.0, "duration": 0.9, "velocity": 100},
        {"pitch": 65, "start": 2.0, "duration": 0.3, "velocity": 60},
        {"pitch": 67, "start": 2.5, "duration": 0.3, "velocity": 65},
        {"pitch": 69, "start": 3.0, "duration": 0.5, "velocity": 95},
        {"pitch": 71, "start": 4.0, "duration": 0.4, "velocity": 80},
        {"pitch": 72, "start": 5.0, "duration": 0.2, "velocity": 55},
        {"pitch": 74, "start": 6.0, "duration": 1.0, "velocity": 110},
        {"pitch": 76, "start": 7.5, "duration": 0.3, "velocity": 66},
    ]
    _grid54 = [(n["pitch"], n["start"], n["duration"]) for n in _nd54]

    def _assert_grid54(notes):
        assert [(n["pitch"], n["start"], n["duration"]) for n in notes] == _grid54

    def _assert_vel_bounds54(notes, originals):
        for out_n, orig_n in zip(notes, originals):
            ov = orig_n["velocity"]
            nv = out_n["velocity"]
            assert 1 <= nv <= 127, f"velocity {nv} out of range"
            assert abs(nv - ov) <= 18, f"drift {nv - ov} exceeds ±18"

    _saved_agg54 = _core._CACHE.get("aggregate")
    _contour54 = [40.0, 110.0, 50.0, 120.0, 45.0, 115.0, 55.0, 105.0,
                  60.0, 100.0, 65.0, 95.0, 70.0, 90.0, 75.0, 85.0]
    try:
        _core._CACHE["aggregate"] = {
            "melodic_grammar_by_role": {"lead": {"unigram": {"0": 0.6, "3": 0.2, "7": 0.2},
                                                 "order_1": {}, "order_2": {}}},
            "tempo": {"mean": 120.0},
            "velocity_by_role": {"lead": {"slot_mean": _contour54, "slot_std": [0.0] * 16}},
        }
        for _p54 in range(_core._SLOW_BURN_PASSES):
            _out54 = _core._slow_burn_accent(_nd54, _p54)
            _assert_grid54(_out54)
            assert len(_out54) == len(_nd54)
            if _p54 < _core._SLOW_BURN_PASSES - 1:
                _assert_vel_bounds54(_out54, _nd54)
            else:
                assert [n["velocity"] for n in _out54] == [n["velocity"] for n in _nd54]
                assert _out54 == [{**n} for n in _nd54], \
                    "the last pass must return exact input copies (velocities included)"

        assert _core._slow_burn_accent(_nd54, 99) == [{**n} for n in _nd54]
        assert _core._slow_burn_accent(_nd54, -5) == _core._slow_burn_accent(_nd54, 0)
        assert _core._slow_burn_accent(_nd54, 1) == _core._slow_burn_accent(_nd54, 1)

        _p0_54 = _core._slow_burn_accent(_nd54, 0)
        _p1_54 = _core._slow_burn_accent(_nd54, 1)
        assert [n["velocity"] for n in _p0_54] != [n["velocity"] for n in _p1_54], \
            "phase rotation must make pass 0 and pass 1 differ in at least one velocity"

        # missing DNA → identity
        _core._CACHE["aggregate"] = {
            "melodic_grammar_by_role": {"lead": {"unigram": {"0": 0.6}, "order_1": {}, "order_2": {}}},
            "tempo": {"mean": 120.0},
            "velocity_by_role": {},
        }
        for _p54 in range(_core._SLOW_BURN_PASSES):
            _id54 = _core._slow_burn_accent(_nd54, _p54)
            assert [n["velocity"] for n in _id54] == [n["velocity"] for n in _nd54]
    finally:
        if _saved_agg54 is None:
            _core._CACHE.pop("aggregate", None)
        else:
            _core._CACHE["aggregate"] = _saved_agg54

    # surface: registry wiring through develop_my_loop (block 53's pool pattern)
    from build_index import phrase_vector as _pv54  # noqa: E402
    _saved_agg54b = _core._CACHE.get("aggregate")
    _saved_pk54 = _core._CACHE.get("project_keys")
    try:
        _core._CACHE["aggregate"] = {
            "melodic_grammar_by_role": {"lead": {"unigram": {"0": 0.6, "3": 0.2, "7": 0.2},
                                                 "order_1": {}, "order_2": {}}},
            "tempo": {"mean": 120.0},
            "velocity_by_role": {"lead": {"slot_mean": _contour54, "slot_std": [0.0] * 16}},
        }
        _core._CACHE["project_keys"] = {"pG": "G minor"}

        def _idx_phrase54(pid, project, role, source, pitches, starts, vels, durs):
            notes = [{"pitch": p, "start": s, "duration": d, "velocity": v}
                     for p, s, v, d in zip(pitches, starts, vels, durs)]
            return {"id": pid, "project": project, "role": role, "kind": "harmonic",
                    "clip_source": source, "tempo": 122.0, "n_notes": len(notes),
                    "notes": notes, "vec": _pv54(notes)}

        _seed54 = [{"pitch": p, "start_beats": s, "duration_beats": 0.45, "velocity": 90}
                   for p, s in zip([62, 65, 69, 67, 65, 62], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0])]
        _durs54 = [0.45] * 6 + [0.6]
        _pool54 = [
            _idx_phrase54("pG|c0|w0", "pG", "lead", "session",
                          [67, 70, 74, 72, 70, 65, 67, 69, 71, 73],
                          [0.0, 0.5, 1.0, 2.0, 2.5, 3.0, 3.75, 5.0, 6.0, 7.0],
                          [91, 63, 77, 84, 58, 96, 72, 80, 75, 60],
                          [0.45, 0.45, 0.45, 0.45, 0.45, 0.45, 0.6, 0.4, 0.4, 1.0]),
            _idx_phrase54("pX|other", "pX", "lead", "session",
                          [60, 63, 67, 65, 63, 60, 62], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 3.75],
                          [80, 66, 74, 88, 60, 92, 70], _durs54),
        ]
        _core.set_phrase_pool_for_test(_pool54)

        _dev54 = _core.develop_my_loop(_seed54, move="slow-burn:accent", key="D minor",
                                       seed=1, use_ears=False)
        assert _dev54["develop_move"] == "slow-burn:accent"
        assert _dev54["transforms_applied"][-1] == "slow-burn:accent:pass1", \
            _dev54["transforms_applied"]
        assert "ears" not in _dev54 and "gate_report" not in _dev54
    finally:
        _core.set_phrase_pool_for_test(None)
        if _saved_agg54b is None:
            _core._CACHE.pop("aggregate", None)
        else:
            _core._CACHE["aggregate"] = _saved_agg54b
        if _saved_pk54 is None:
            _core._CACHE.pop("project_keys", None)
        else:
            _core._CACHE["project_keys"] = _saved_pk54
    print("  pitch/start/duration byte-identical; velocity drift ±18, clamped 1..127;")
    print("  last pass resolves home; phase wanders across passes; missing DNA → identity;")
    print("  develop_move + transforms_applied name the pass; ears-off adds no keys; deterministic")
    print("  OK\n")

    # 55. T6: slow-burn:add — clone-provenance ornaments in the legal set
    # (spec §3.3, §10 "slow-burn:add"). Unlike reveal/accent, add's ramp
    # INTENSIFIES toward the last pass (nothing -> fullest ornamentation) —
    # deliberate; see _slow_burn_add's docstring.
    print("--- slow-burn:add (T6) ---")
    _nd55 = [
        {"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 90},
        {"pitch": 67, "start": 1.0, "duration": 0.25, "velocity": 70},
        {"pitch": 60, "start": 1.5, "duration": 1.0, "velocity": 100},
        {"pitch": 69, "start": 2.5, "duration": 0.25, "velocity": 60},
        {"pitch": 60, "start": 3.0, "duration": 1.0, "velocity": 65},
        {"pitch": 65, "start": 4.0, "duration": 0.25, "velocity": 95},
        {"pitch": 60, "start": 4.5, "duration": 1.0, "velocity": 80},
        {"pitch": 64, "start": 5.5, "duration": 0.3, "velocity": 55},
    ]
    # 4 long (>=0.75 beat) anchors for neighbour candidates + 7 large (>=4
    # semitone) top-line leaps for passing candidates -> 11 raw candidates,
    # comfortably more than the cap (round(8*0.4) == 3), so the truncation/
    # tie-break path is actually exercised, not merely available.
    _tuples55 = {(n["pitch"], n["start"], n["duration"], n["velocity"]) for n in _nd55}
    _pairs55 = {(n["velocity"], n["duration"]) for n in _nd55}
    _cap55 = round(len(_nd55) * 0.4)

    _tonic_name55, _mode55 = _core._parse_key_string("D minor")
    _tonic_pc55 = _core.PITCH_NAMES.index(_tonic_name55)
    _legal55 = _core._legal_degree_set(_nd55, _tonic_pc55)

    def _added55(out, _tuples_in=_tuples55):
        return [n for n in out
                if (n["pitch"], n["start"], n["duration"], n["velocity"]) not in _tuples_in]

    _prev_added55 = None
    for _p55 in range(_core._SLOW_BURN_PASSES):
        _out55 = _core._slow_burn_add(_nd55, _p55, key_str="D minor")
        _out_tuples55 = {(n["pitch"], n["start"], n["duration"], n["velocity"]) for n in _out55}
        assert _tuples55 <= _out_tuples55, "every input note must survive verbatim in the output"
        _added_n55 = len(_out55) - len(_nd55)
        if _p55 == 0:
            assert _added_n55 == 0, "pass 0 must add nothing"
        assert _added_n55 <= _cap55, f"added count {_added_n55} exceeds the cap {_cap55}"
        if _prev_added55 is not None:
            assert _added_n55 >= _prev_added55, \
                f"pass {_p55} ({_added_n55} added) must be >= the previous pass ({_prev_added55})"
        _prev_added55 = _added_n55
        for _a55 in _added55(_out55):
            assert (_a55["velocity"], _a55["duration"]) in _pairs55, \
                f"added note {_a55} has no (velocity, duration) donor in the input"
            _deg55 = (_a55["pitch"] - _tonic_pc55) % 12
            assert _deg55 in _legal55, f"added pitch {_a55['pitch']} has illegal degree {_deg55}"
    assert _prev_added55 == _cap55, \
        (f"the last pass must reach the full cap ({_cap55}); reached {_prev_added55} — "
         "fixture must offer >= cap candidates")

    # determinism + pass clamps
    assert (_core._slow_burn_add(_nd55, 2, key_str="D minor")
            == _core._slow_burn_add(_nd55, 2, key_str="D minor")), "deterministic across repeated calls"
    assert (_core._slow_burn_add(_nd55, 99, key_str="D minor")
            == _core._slow_burn_add(_nd55, _core._SLOW_BURN_PASSES - 1, key_str="D minor")), \
        "an over-range pass_index must clamp to the last pass"
    assert (_core._slow_burn_add(_nd55, -5, key_str="D minor")
            == _core._slow_burn_add(_nd55, 0, key_str="D minor")), \
        "a negative pass_index must clamp to pass 0"

    # missing key (key_str=None): self-derived tonic, legal-degree property still holds
    _pc_hist55 = [0.0] * 12
    for _n55 in _nd55:
        _pc_hist55[_n55["pitch"] % 12] += 1
    _self_tonic_name55, _self_mode55, _ = _core.estimate_key(_pc_hist55)
    _self_tonic_pc55 = _core.PITCH_NAMES.index(_self_tonic_name55)
    _self_legal55 = _core._legal_degree_set(_nd55, _self_tonic_pc55)
    _out_none55 = _core._slow_burn_add(_nd55, _core._SLOW_BURN_PASSES - 1, key_str=None)
    _added_none55 = _added55(_out_none55)
    assert _added_none55, "the missing-key path must still add notes (self-derived tonic, not a refusal)"
    for _a55 in _added_none55:
        _deg55 = (_a55["pitch"] - _self_tonic_pc55) % 12
        assert _deg55 in _self_legal55, \
            f"self-derived path: pitch {_a55['pitch']} has illegal degree {_deg55}"

    # surface: registry wiring through develop_my_loop (block 53/54's pool
    # pattern); no key= passed here, so this ALSO exercises the needs_key
    # None-forward through the dispatch (develop_my_loop's own `key` default).
    from build_index import phrase_vector as _pv55  # noqa: E402
    _saved_agg55 = _core._CACHE.get("aggregate")
    _saved_pk55 = _core._CACHE.get("project_keys")
    try:
        _core._CACHE["aggregate"] = {
            "melodic_grammar_by_role": {"lead": {"unigram": {"0": 0.6, "3": 0.2, "7": 0.2},
                                                 "order_1": {}, "order_2": {}}},
            "tempo": {"mean": 120.0},
        }
        _core._CACHE["project_keys"] = {"pG": "G minor"}

        def _idx_phrase55(pid, project, role, source, pitches, starts, vels, durs):
            notes = [{"pitch": p, "start": s, "duration": d, "velocity": v}
                     for p, s, v, d in zip(pitches, starts, vels, durs)]
            return {"id": pid, "project": project, "role": role, "kind": "harmonic",
                    "clip_source": source, "tempo": 122.0, "n_notes": len(notes),
                    "notes": notes, "vec": _pv55(notes)}

        _seed55 = [{"pitch": p, "start_beats": s, "duration_beats": 0.45, "velocity": 90}
                   for p, s in zip([62, 65, 69, 67, 65, 62], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0])]
        _durs55 = [0.45] * 6 + [0.6]
        _pool55 = [
            _idx_phrase55("pG|c0|w0", "pG", "lead", "session",
                          [67, 70, 74, 72, 70, 65, 67, 69, 71, 73],
                          [0.0, 0.5, 1.0, 2.0, 2.5, 3.0, 3.75, 5.0, 6.0, 7.0],
                          [91, 63, 77, 84, 58, 96, 72, 80, 75, 60],
                          [0.45, 0.45, 0.45, 0.45, 0.45, 0.45, 0.6, 0.4, 0.4, 1.0]),
            _idx_phrase55("pX|other", "pX", "lead", "session",
                          [60, 63, 67, 65, 63, 60, 62], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 3.75],
                          [80, 66, 74, 88, 60, 92, 70], _durs55),
        ]
        _core.set_phrase_pool_for_test(_pool55)

        _dev55 = _core.develop_my_loop(_seed55, move="slow-burn:add", seed=3, use_ears=False)
        assert _dev55["develop_move"] == "slow-burn:add"
        assert _dev55["transforms_applied"][-1] == "slow-burn:add:pass3", _dev55["transforms_applied"]
        assert "ears" not in _dev55 and "gate_report" not in _dev55, \
            "use_ears=False must add no ears keys"
    finally:
        _core.set_phrase_pool_for_test(None)
        if _saved_agg55 is None:
            _core._CACHE.pop("aggregate", None)
        else:
            _core._CACHE["aggregate"] = _saved_agg55
        if _saved_pk55 is None:
            _core._CACHE.pop("project_keys", None)
        else:
            _core._CACHE["project_keys"] = _saved_pk55
    print("  originals verbatim (subset); clone provenance (velocity,duration) pairs; added")
    print("  degrees legal; ramp 0 -> cap non-decreasing, cap honoured + reached; deterministic;")
    print("  pass clamps; missing key self-derives tonic (legal-degree holds); develop_move +")
    print("  label tail name the pass; ears-off adds no keys")
    print("  OK\n")

    # 56. T7: motivic:reharm + motivic:retro (spec §4.1, §4.4, §10 provenance).
    print("--- motivic:reharm + motivic:retro (T7) ---")
    _nd56 = [
        {"pitch": 60, "start": 0.0, "duration": 0.4, "velocity": 90},
        {"pitch": 64, "start": 0.5, "duration": 0.2, "velocity": 70},
        {"pitch": 67, "start": 1.0, "duration": 0.9, "velocity": 100},
        {"pitch": 72, "start": 2.0, "duration": 0.3, "velocity": 60},
        {"pitch": 76, "start": 3.0, "duration": 0.5, "velocity": 95},
    ]
    # reharm: full (start, duration, velocity) provenance; only pitches may differ
    _grid56 = [(n["start"], n["duration"], n["velocity"]) for n in _nd56]
    _reharm56a = _core._reharmonize_notes(_nd56)
    _reharm56b = _core._reharmonize_notes(_nd56)
    assert [(n["start"], n["duration"], n["velocity"]) for n in _reharm56a] == _grid56, \
        "reharm must preserve (start, duration, velocity) order-preserved"
    assert _reharm56a == _reharm56b, "reharm must be deterministic"
    assert any(n["pitch"] != o["pitch"] for n, o in zip(_reharm56a, _nd56)), \
        "reharm fixture must actually change at least one pitch"

    # retro: asymmetric line so reversed sequence is discriminating (not a palindrome)
    _retro56 = [
        {"pitch": 60, "start": 0.0, "duration": 0.4, "velocity": 90},
        {"pitch": 62, "start": 0.5, "duration": 0.2, "velocity": 70},
        {"pitch": 64, "start": 1.0, "duration": 0.9, "velocity": 100},
        {"pitch": 67, "start": 2.0, "duration": 0.3, "velocity": 60},
        {"pitch": 71, "start": 3.0, "duration": 0.5, "velocity": 95},
    ]
    _ordered56 = sorted(_retro56, key=lambda n: (n["start"], n["pitch"]))
    _inp_pitches56 = [n["pitch"] for n in _ordered56]
    _out56 = _core._retrograde_notes(_retro56)
    _out_ordered56 = sorted(_out56, key=lambda n: (n["start"], n["pitch"]))
    _out_pitches56 = [n["pitch"] for n in _out_ordered56]
    assert sorted(_inp_pitches56) == sorted(_out_pitches56), \
        "retro pitch multiset must match input"
    assert [(n["start"], n["duration"], n["velocity"]) for n in _out_ordered56] == \
        [(n["start"], n["duration"], n["velocity"]) for n in _ordered56], \
        "retro (start, duration, velocity) grid must be identical position-by-position"
    assert _out_pitches56 == list(reversed(_inp_pitches56)), \
        "retro pitch sequence must be reversed (first out == last in)"
    assert _out_pitches56[0] == _inp_pitches56[-1] and _out_pitches56[-1] == _inp_pitches56[0], \
        "retro endpoints must mirror"
    assert _core._retrograde_notes(_out56) == _core._retrograde_notes(_out56), \
        "retro must be deterministic"
    # empty + 1-note pass through unchanged
    assert _core._retrograde_notes([]) == []
    _one56 = [{"pitch": 65, "start": 1.0, "duration": 0.5, "velocity": 80}]
    assert _core._retrograde_notes(_one56) == [{"pitch": 65, "start": 1.0,
                                                 "duration": 0.5, "velocity": 80}]
    # copies: mutating output must not touch input
    _mut_in56 = [{"pitch": 60, "start": 0.0, "duration": 0.25, "velocity": 90},
                 {"pitch": 64, "start": 1.0, "duration": 0.5, "velocity": 70}]
    _mut_snap56 = {n["pitch"]: n["velocity"] for n in _mut_in56}
    _mut_out56 = _core._retrograde_notes(_mut_in56)
    _mut_out56[0]["pitch"] = 99
    assert {n["pitch"]: n["velocity"] for n in _mut_in56} == _mut_snap56, \
        "retro output must be a copy — mutating it must not alter input"

    # registry hygiene: neither motivic entry needs seed nor key
    assert not _core._DEVELOP_TRANSFORMS["motivic:reharm"].get("needs_seed")
    assert not _core._DEVELOP_TRANSFORMS["motivic:reharm"].get("needs_key")
    assert not _core._DEVELOP_TRANSFORMS["motivic:retro"].get("needs_seed")
    assert not _core._DEVELOP_TRANSFORMS["motivic:retro"].get("needs_key")

    # surface: registry wiring through develop_my_loop (block 53's pool pattern)
    from build_index import phrase_vector as _pv56  # noqa: E402
    _saved_agg56 = _core._CACHE.get("aggregate")
    _saved_pk56 = _core._CACHE.get("project_keys")
    try:
        _core._CACHE["aggregate"] = {
            "melodic_grammar_by_role": {"lead": {"unigram": {"0": 0.6, "3": 0.2, "7": 0.2},
                                                 "order_1": {}, "order_2": {}}},
            "tempo": {"mean": 120.0},
        }
        _core._CACHE["project_keys"] = {"pG": "G minor"}

        def _idx_phrase56(pid, project, role, source, pitches, starts, vels, durs):
            notes = [{"pitch": p, "start": s, "duration": d, "velocity": v}
                     for p, s, v, d in zip(pitches, starts, vels, durs)]
            return {"id": pid, "project": project, "role": role, "kind": "harmonic",
                    "clip_source": source, "tempo": 122.0, "n_notes": len(notes),
                    "notes": notes, "vec": _pv56(notes)}

        _seed56 = [{"pitch": p, "start_beats": s, "duration_beats": 0.45, "velocity": 90}
                   for p, s in zip([62, 65, 69, 67, 65, 62], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0])]
        _durs56 = [0.45] * 6 + [0.6]
        _pool56 = [
            _idx_phrase56("pG|c0|w0", "pG", "lead", "session",
                          [67, 70, 74, 72, 70, 65, 67, 69, 71, 73],
                          [0.0, 0.5, 1.0, 2.0, 2.5, 3.0, 3.75, 5.0, 6.0, 7.0],
                          [91, 63, 77, 84, 58, 96, 72, 80, 75, 60],
                          [0.45, 0.45, 0.45, 0.45, 0.45, 0.45, 0.6, 0.4, 0.4, 1.0]),
            _idx_phrase56("pX|other", "pX", "lead", "session",
                          [60, 63, 67, 65, 63, 60, 62], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 3.75],
                          [80, 66, 74, 88, 60, 92, 70], _durs56),
            _idx_phrase56("echo|seed", "pE", "lead", "session",
                          [62, 65, 69, 67, 65, 62, 64], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 3.75],
                          [90] * 7, _durs56),
        ]
        _core.set_phrase_pool_for_test(_pool56)

        _dev56_retro = _core.develop_my_loop(_seed56, move="motivic:retro", seed=1,
                                             use_ears=False)
        assert _dev56_retro["develop_move"] == "motivic:retro"
        assert "motivic:retro" in _dev56_retro["transforms_applied"]
        assert not any(t.endswith(":pass0") or ":pass" in t.split("motivic:retro")[-1]
                       for t in _dev56_retro["transforms_applied"]
                       if t.startswith("motivic:retro")), \
            f"motivic:retro must not get a :passN suffix: {_dev56_retro['transforms_applied']}"
        assert _dev56_retro["transforms_applied"][-1] == "motivic:retro", \
            _dev56_retro["transforms_applied"]
        assert "ears" not in _dev56_retro and "gate_report" not in _dev56_retro, \
            "use_ears=False must add no ears keys"

        _dev56_reharm = _core.develop_my_loop(_seed56, move="motivic:reharm", seed=1,
                                              use_ears=False)
        assert _dev56_reharm["develop_move"] == "motivic:reharm"
        assert "motivic:reharm" in _dev56_reharm["transforms_applied"]
        assert _dev56_reharm["transforms_applied"][-1] == "motivic:reharm", \
            _dev56_reharm["transforms_applied"]
        assert "ears" not in _dev56_reharm and "gate_report" not in _dev56_reharm, \
            "use_ears=False must add no ears keys"
    finally:
        _core.set_phrase_pool_for_test(None)
        if _saved_agg56 is None:
            _core._CACHE.pop("aggregate", None)
        else:
            _core._CACHE["aggregate"] = _saved_agg56
        if _saved_pk56 is None:
            _core._CACHE.pop("project_keys", None)
        else:
            _core._CACHE["project_keys"] = _saved_pk56
    print("  reharm: (start,duration,velocity) grid preserved; pitches may differ; deterministic")
    print("  retro: pitch multiset + grid identical; sequence reversed; empty/1-note pass; copies")
    print("  develop_move + transforms_applied name the move (no :passN); registry has no")
    print("  needs_seed/needs_key; ears-off adds no keys")
    print("  OK\n")

    # 57. T8: motivic:fragment + motivic:walk (spec §4.2, §4.5, §10 provenance).
    print("--- motivic:fragment + motivic:walk (T8) ---")
    _nd57 = [
        {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90},
        {"pitch": 63, "start": 0.5, "duration": 0.5, "velocity": 70},
        {"pitch": 67, "start": 1.0, "duration": 0.5, "velocity": 100},
        {"pitch": 65, "start": 1.5, "duration": 0.5, "velocity": 80},
        {"pitch": 60, "start": 2.0, "duration": 0.5, "velocity": 90},
        {"pitch": 63, "start": 4.5, "duration": 0.5, "velocity": 75},
        {"pitch": 67, "start": 5.0, "duration": 0.5, "velocity": 95},
        {"pitch": 65, "start": 6.0, "duration": 1.0, "velocity": 85},
    ]
    _in_vels57 = {n["velocity"] for n in _nd57}
    _span57 = _core._phrase_span(_nd57)
    _motif_end57 = (_span57 / 2 if _span57 <= _core.BEATS_PER_BAR
                    else min(_core.BEATS_PER_BAR, _span57))
    _motif_end57 = max(_motif_end57, 0.5)
    _motif57 = [n for n in _nd57 if n["start"] < _motif_end57 - 1e-6]
    _motif_vel_seq57 = [n["velocity"] for n in sorted(_motif57, key=lambda x: (x["start"], x["pitch"]))]

    _pc_hist57 = [0.0] * 12
    for _n57 in _nd57:
        _pc_hist57[_n57["pitch"] % 12] += 1
    _tonic_name57, _mode57, _ = _core.estimate_key(_pc_hist57)
    _tonic_pc57 = _core.PITCH_NAMES.index(_tonic_name57)
    _legal57 = _core._legal_degree_set(_nd57, _tonic_pc57)
    _key57 = f"{_tonic_name57} {_mode57}"

    _frag57 = _core._fragment_harmonic_notes(_nd57, key_str=_key57)
    _frag57b = _core._fragment_harmonic_notes(_nd57, key_str=_key57)
    assert _frag57 == _frag57b, "fragment must be deterministic"
    for _o57 in _frag57:
        assert _o57["velocity"] in _in_vels57, \
            f"fragment velocity {_o57['velocity']} not from input pool"
    # per-repetition velocity sequence == motif's
    _rep_idx57 = 0
    _t57 = 0.0
    while _t57 < _span57 - 1e-6:
        _rep_notes57 = sorted(
            [n for n in _frag57 if abs(n["start"] - _t57) < 1e-5
             or any(abs(n["start"] - (m["start"] + _t57)) < 1e-5 for m in _motif57)],
            key=lambda x: (x["start"], x["pitch"]),
        )
        _rep_at_t57 = sorted(
            [n for n in _frag57 if any(abs(n["start"] - (m["start"] + _t57)) < 1e-5
                                        for m in _motif57)],
            key=lambda x: (x["start"], x["pitch"]),
        )
        if _rep_at_t57:
            assert [n["velocity"] for n in _rep_at_t57] == _motif_vel_seq57, \
                f"rep {_rep_idx57} velocity sequence must match motif"
        _t57 += _motif_end57
        _rep_idx57 += 1
    for _o57 in _frag57:
        _k_start57 = None
        for _k57 in range(20):
            _base57 = _k57 * _motif_end57
            for _m57 in _motif57:
                if abs(_o57["start"] - (_m57["start"] + _base57)) < 1e-6:
                    _k_start57 = _k57
                    assert _o57["duration"] <= _m57["duration"] + 1e-6, \
                        "fragment duration must be clipped at span"
                    break
            if _k_start57 is not None:
                break
        assert _k_start57 is not None, \
            f"fragment start {_o57['start']} must be motif start + k*motif_end"
        if _k_start57 > 0:
            assert (_o57["pitch"] - _tonic_pc57) % 12 in _legal57, \
                f"k>0 fragment pitch {_o57['pitch']} must land on legal set"
    _k0_frag57 = sorted(
        [n for n in _frag57 if n["start"] < _motif_end57 - 1e-6],
        key=lambda x: (x["start"], x["pitch"]),
    )
    _k0_motif57 = sorted(_motif57, key=lambda x: (x["start"], x["pitch"]))
    assert len(_k0_frag57) == len(_k0_motif57)
    for _a57, _b57 in zip(_k0_frag57, _k0_motif57):
        assert _a57["pitch"] == _b57["pitch"] and abs(_a57["start"] - _b57["start"]) < 1e-6
        assert _a57["duration"] == _b57["duration"] and _a57["velocity"] == _b57["velocity"]

    _walk0_57 = _core._alphabet_walk_notes(_nd57, key_str=_key57, walk_seed=0)
    _walk5_57 = _core._alphabet_walk_notes(_nd57, key_str=_key57, walk_seed=5)
    _walk0b_57 = _core._alphabet_walk_notes(_nd57, key_str=_key57, walk_seed=0)
    assert _walk0_57 == _walk0b_57, "walk must be deterministic for same seed"
    assert [n["pitch"] for n in _walk0_57] != [n["pitch"] for n in _walk5_57], \
        "different walk_seed values must differ in at least one pitch"
    for _o57 in _walk0_57 + _walk5_57:
        assert _o57["velocity"] in _in_vels57, \
            f"walk velocity {_o57['velocity']} not from input pool"
        assert (_o57["pitch"] - _tonic_pc57) % 12 in _legal57, \
            f"walk pitch {_o57['pitch']} must land on legal set"

    # self-derived key (key_str=None): legal-set membership vs estimate_key tonic
    _frag_none57 = _core._fragment_harmonic_notes(_nd57, key_str=None)
    for _o57 in _frag_none57:
        assert (_o57["pitch"] - _tonic_pc57) % 12 in _legal57, \
            "fragment self-derived path: pitch must be on legal set"
    _walk_none57 = _core._alphabet_walk_notes(_nd57, key_str=None, walk_seed=1)
    for _o57 in _walk_none57:
        assert (_o57["pitch"] - _tonic_pc57) % 12 in _legal57, \
            "walk self-derived path: pitch must be on legal set"

    # registry hygiene
    assert _core._DEVELOP_TRANSFORMS["motivic:fragment"].get("needs_key")
    assert not _core._DEVELOP_TRANSFORMS["motivic:fragment"].get("needs_seed")
    assert not _core._DEVELOP_TRANSFORMS["motivic:fragment"].get("seed_param")
    assert _core._DEVELOP_TRANSFORMS["motivic:walk"].get("needs_key")
    assert _core._DEVELOP_TRANSFORMS["motivic:walk"].get("seed_param") == "walk_seed"
    assert not _core._DEVELOP_TRANSFORMS["motivic:walk"].get("needs_seed")

    # surface: registry wiring through develop_my_loop (block 53's pool pattern)
    from build_index import phrase_vector as _pv57  # noqa: E402
    _saved_agg57 = _core._CACHE.get("aggregate")
    _saved_pk57 = _core._CACHE.get("project_keys")
    try:
        _core._CACHE["aggregate"] = {
            "melodic_grammar_by_role": {"lead": {"unigram": {"0": 0.6, "3": 0.2, "7": 0.2},
                                                 "order_1": {}, "order_2": {}}},
            "tempo": {"mean": 120.0},
        }
        _core._CACHE["project_keys"] = {"pG": "G minor"}

        def _idx_phrase57(pid, project, role, source, pitches, starts, vels, durs):
            notes = [{"pitch": p, "start": s, "duration": d, "velocity": v}
                     for p, s, v, d in zip(pitches, starts, vels, durs)]
            return {"id": pid, "project": project, "role": role, "kind": "harmonic",
                    "clip_source": source, "tempo": 122.0, "n_notes": len(notes),
                    "notes": notes, "vec": _pv57(notes)}

        _seed57 = [{"pitch": p, "start_beats": s, "duration_beats": 0.45, "velocity": 90}
                   for p, s in zip([62, 65, 69, 67, 65, 62], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0])]
        _durs57 = [0.45] * 6 + [0.6]
        _pool57 = [
            _idx_phrase57("pG|c0|w0", "pG", "lead", "session",
                          [67, 70, 74, 72, 70, 65, 67, 69, 71, 73],
                          [0.0, 0.5, 1.0, 2.0, 2.5, 3.0, 3.75, 5.0, 6.0, 7.0],
                          [91, 63, 77, 84, 58, 96, 72, 80, 75, 60],
                          [0.45, 0.45, 0.45, 0.45, 0.45, 0.45, 0.6, 0.4, 0.4, 1.0]),
            _idx_phrase57("pX|other", "pX", "lead", "session",
                          [60, 63, 67, 65, 63, 60, 62], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 3.75],
                          [80, 66, 74, 88, 60, 92, 70], _durs57),
        ]
        _core.set_phrase_pool_for_test(_pool57)

        _dev57_frag = _core.develop_my_loop(_seed57, move="motivic:fragment", seed=2,
                                            use_ears=False)
        assert _dev57_frag["develop_move"] == "motivic:fragment"
        assert "motivic:fragment" in _dev57_frag["transforms_applied"]
        assert _dev57_frag["transforms_applied"][-1] == "motivic:fragment", \
            _dev57_frag["transforms_applied"]
        assert "ears" not in _dev57_frag and "gate_report" not in _dev57_frag, \
            "use_ears=False must add no ears keys"

        _dev57_walk = _core.develop_my_loop(_seed57, move="motivic:walk", seed=2,
                                            use_ears=False)
        assert _dev57_walk["develop_move"] == "motivic:walk"
        assert _dev57_walk["transforms_applied"][-1] == "motivic:walk:walk2", \
            _dev57_walk["transforms_applied"]
        assert "ears" not in _dev57_walk and "gate_report" not in _dev57_walk, \
            "use_ears=False must add no ears keys"
    finally:
        _core.set_phrase_pool_for_test(None)
        if _saved_agg57 is None:
            _core._CACHE.pop("aggregate", None)
        else:
            _core._CACHE["aggregate"] = _saved_agg57
        if _saved_pk57 is None:
            _core._CACHE.pop("project_keys", None)
        else:
            _core._CACHE["project_keys"] = _saved_pk57
    print("  fragment: velocity-only provenance; motif grid + k=0 verbatim; legal pitches;")
    print("  walk: velocity-only provenance; legal degrees; seed-discriminating; deterministic;")
    print("  self-derived key legal-set holds; develop_move + labels (fragment plain, walk:walkN);")
    print("  registry needs_key / seed_param hygiene; ears-off adds no keys")
    print("  OK\n")

    # 58. T9: motivic:invert (spec §4.3, §10 Inversion).
    print("--- motivic:invert (T9) ---")
    from motif_scan import skyline as _sky58  # noqa: E402  (analyze/ on sys.path since block 14)
    from motifs import classify as _classify58, _intervals as _ivs58  # noqa: E402

    def _sky_ivs58(notes):
        line = _sky58(notes)
        return [b - a for a, b in zip(line, line[1:])]

    def _sky_degs58(notes, tonic_pc):
        return tuple((p - tonic_pc) % 12 for p in _sky58(notes))

    def _classify_nd58(notes, tonic_pc):
        degs = _sky_degs58(notes, tonic_pc)
        if len(degs) < 2:
            return "repeat"
        return _classify58(degs, _ivs58(degs))

    # provenance + copies on a multi-note fixture
    _nd58 = [
        {"pitch": 60, "start": 0.0, "duration": 0.4, "velocity": 90},
        {"pitch": 64, "start": 0.5, "duration": 0.2, "velocity": 70},
        {"pitch": 67, "start": 1.0, "duration": 0.9, "velocity": 100},
        {"pitch": 72, "start": 2.0, "duration": 0.3, "velocity": 60},
    ]
    _grid58 = [(n["start"], n["duration"], n["velocity"]) for n in _nd58]
    _inv58a = _core._invert_motif_notes(_nd58, key_str="C minor")
    _inv58b = _core._invert_motif_notes(_nd58, key_str="C minor")
    assert _inv58a == _inv58b, "invert must be deterministic"
    assert [(n["start"], n["duration"], n["velocity"]) for n in _inv58a] == _grid58, \
        "invert must preserve (start, duration, velocity) order-preserved"
    assert any(n["pitch"] != o["pitch"] for n, o in zip(_inv58a, _nd58)), \
        "invert fixture must change at least one pitch"
    _mut_in58 = [{"pitch": 60, "start": 0.0, "duration": 0.25, "velocity": 90},
                 {"pitch": 64, "start": 1.0, "duration": 0.5, "velocity": 70}]
    _mut_snap58 = {n["pitch"]: n["velocity"] for n in _mut_in58}
    _mut_out58 = _core._invert_motif_notes(_mut_in58, key_str="C minor")
    _mut_out58[0]["pitch"] = 99
    assert {n["pitch"]: n["velocity"] for n in _mut_in58} == _mut_snap58, \
        "invert output must be a copy — mutating it must not alter input"
    assert _core._invert_motif_notes([]) == []
    _one58 = [{"pitch": 65, "start": 1.0, "duration": 0.5, "velocity": 80}]
    assert _core._invert_motif_notes(_one58, key_str="C minor") == _one58

    # inversion property: skyline signed intervals negated (no snap on this fixture)
    _invprop58 = [
        {"pitch": 48, "start": 0.0, "duration": 0.5, "velocity": 90},
        {"pitch": 55, "start": 1.0, "duration": 0.5, "velocity": 80},
        {"pitch": 62, "start": 2.0, "duration": 0.5, "velocity": 70},
        {"pitch": 69, "start": 3.0, "duration": 0.5, "velocity": 60},
    ]
    _out_prop58 = _core._invert_motif_notes(_invprop58, key_str="C minor")
    _iv_in58 = _sky_ivs58(_invprop58)
    _iv_out58 = _sky_ivs58(_out_prop58)
    assert _iv_out58 == [-x for x in _iv_in58], \
        f"invert skyline intervals must negate input: in {_iv_in58} out {_iv_out58}"

    # legality: skyline degrees ⊆ legal set (snap-forcing + self-derived tonic)
    _tonic_pc58 = _core.PITCH_NAMES.index("C")
    _snap58 = [
        {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90},
        {"pitch": 72, "start": 1.0, "duration": 0.5, "velocity": 80},
        {"pitch": 76, "start": 2.0, "duration": 0.5, "velocity": 70},
        {"pitch": 79, "start": 3.0, "duration": 0.5, "velocity": 60},
    ]
    _legal_snap58 = _core._legal_degree_set(_snap58, _tonic_pc58)
    _ordered_snap58 = sorted(_snap58, key=lambda n: (n["start"], n["pitch"]))
    _axis_snap58 = _ordered_snap58[0]["pitch"]
    _raw_snap58 = [2 * _axis_snap58 - n["pitch"] for n in _ordered_snap58]
    assert any((p - _tonic_pc58) % 12 not in _legal_snap58 for p in _raw_snap58), \
        "snap fixture: naive inversion must leave the alphabet"
    _out_snap58 = _core._invert_motif_notes(_snap58, key_str="C minor")
    assert set(_sky_degs58(_out_snap58, _tonic_pc58)) <= _legal_snap58, \
        "snap fixture: output skyline degrees must ⊆ legal set"
    _pc_hist58 = [0.0] * 12
    for _n58 in _snap58:
        _pc_hist58[_n58["pitch"] % 12] += 1
    _tonic_name58, _mode58, _ = _core.estimate_key(_pc_hist58)
    _tonic_pc_der58 = _core.PITCH_NAMES.index(_tonic_name58)
    _legal_der58 = _core._legal_degree_set(_snap58, _tonic_pc_der58)
    _out_none58 = _core._invert_motif_notes(_snap58, key_str=None)
    assert set(_sky_degs58(_out_none58, _tonic_pc_der58)) <= _legal_der58, \
        "self-derived tonic: output skyline degrees must ⊆ legal set"

    # dark_landing preference: shifted candidate wins when plain does not
    _dark58 = [
        {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 90},
        {"pitch": 62, "start": 1.0, "duration": 0.5, "velocity": 80},
        {"pitch": 65, "start": 2.0, "duration": 0.5, "velocity": 70},
        {"pitch": 67, "start": 3.0, "duration": 0.5, "velocity": 60},
        {"pitch": 62, "start": 4.0, "duration": 0.5, "velocity": 50},
    ]
    _out_dark58 = _core._invert_motif_notes(_dark58, key_str="C minor")
    assert _classify_nd58(_out_dark58, _tonic_pc58) == "dark_landing", \
        "dark_landing fixture must return the shifted dark_landing candidate"

    # neither candidate dark_landing → plain inverted output
    _plain58 = [
        {"pitch": 62, "start": 0.0, "duration": 0.5, "velocity": 90},
        {"pitch": 65, "start": 1.0, "duration": 0.5, "velocity": 80},
        {"pitch": 69, "start": 2.0, "duration": 0.5, "velocity": 70},
        {"pitch": 74, "start": 3.0, "duration": 0.5, "velocity": 60},
    ]
    _ordered_plain58 = sorted(_plain58, key=lambda n: (n["start"], n["pitch"]))
    _axis_plain58 = _ordered_plain58[0]["pitch"]
    _legal_plain58 = _core._legal_degree_set(_plain58, _tonic_pc58)
    _plain_cand58 = [
        {"pitch": _core._snap_to_legal(2 * _axis_plain58 - n["pitch"], _tonic_pc58, _legal_plain58),
         "start": n["start"], "duration": n["duration"], "velocity": n["velocity"]}
        for n in _ordered_plain58
    ]
    _out_plain58 = _core._invert_motif_notes(_plain58, key_str="C minor")
    assert _classify_nd58(_plain_cand58, _tonic_pc58) != "dark_landing"
    assert _classify_nd58(_out_plain58, _tonic_pc58) != "dark_landing"
    assert _out_plain58 == _plain_cand58, \
        "when neither candidate is dark_landing, plain inverted output must be returned"

    # registry hygiene: needs_key only
    assert _core._DEVELOP_TRANSFORMS["motivic:invert"].get("needs_key")
    assert not _core._DEVELOP_TRANSFORMS["motivic:invert"].get("needs_seed")
    assert not _core._DEVELOP_TRANSFORMS["motivic:invert"].get("seed_param")

    # surface: registry wiring through develop_my_loop (block 53's pool pattern)
    from build_index import phrase_vector as _pv58  # noqa: E402
    _saved_agg58 = _core._CACHE.get("aggregate")
    _saved_pk58 = _core._CACHE.get("project_keys")
    try:
        _core._CACHE["aggregate"] = {
            "melodic_grammar_by_role": {"lead": {"unigram": {"0": 0.6, "3": 0.2, "7": 0.2},
                                                 "order_1": {}, "order_2": {}}},
            "tempo": {"mean": 120.0},
        }
        _core._CACHE["project_keys"] = {"pG": "G minor"}

        def _idx_phrase58(pid, project, role, source, pitches, starts, vels, durs):
            notes = [{"pitch": p, "start": s, "duration": d, "velocity": v}
                     for p, s, v, d in zip(pitches, starts, vels, durs)]
            return {"id": pid, "project": project, "role": role, "kind": "harmonic",
                    "clip_source": source, "tempo": 122.0, "n_notes": len(notes),
                    "notes": notes, "vec": _pv58(notes)}

        _seed58 = [{"pitch": p, "start_beats": s, "duration_beats": 0.45, "velocity": 90}
                   for p, s in zip([62, 65, 69, 67, 65, 62], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0])]
        _durs58 = [0.45] * 6 + [0.6]
        _pool58 = [
            _idx_phrase58("pG|c0|w0", "pG", "lead", "session",
                          [67, 70, 74, 72, 70, 65, 67, 69, 71, 73],
                          [0.0, 0.5, 1.0, 2.0, 2.5, 3.0, 3.75, 5.0, 6.0, 7.0],
                          [91, 63, 77, 84, 58, 96, 72, 80, 75, 60],
                          [0.45, 0.45, 0.45, 0.45, 0.45, 0.45, 0.6, 0.4, 0.4, 1.0]),
            _idx_phrase58("pX|other", "pX", "lead", "session",
                          [60, 63, 67, 65, 63, 60, 62], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 3.75],
                          [80, 66, 74, 88, 60, 92, 70], _durs58),
        ]
        _core.set_phrase_pool_for_test(_pool58)

        _dev58 = _core.develop_my_loop(_seed58, move="motivic:invert", seed=1,
                                       use_ears=False)
        assert _dev58["develop_move"] == "motivic:invert"
        assert "motivic:invert" in _dev58["transforms_applied"]
        assert _dev58["transforms_applied"][-1] == "motivic:invert", \
            _dev58["transforms_applied"]
        assert not any(":pass" in t for t in _dev58["transforms_applied"]
                       if t.startswith("motivic:invert")), \
            f"motivic:invert must not get a :passN suffix: {_dev58['transforms_applied']}"
        assert "ears" not in _dev58 and "gate_report" not in _dev58, \
            "use_ears=False must add no ears keys"
    finally:
        _core.set_phrase_pool_for_test(None)
        if _saved_agg58 is None:
            _core._CACHE.pop("aggregate", None)
        else:
            _core._CACHE["aggregate"] = _saved_agg58
        if _saved_pk58 is None:
            _core._CACHE.pop("project_keys", None)
        else:
            _core._CACHE["project_keys"] = _saved_pk58
    print("  invert: (start,duration,velocity) grid preserved; skyline intervals negated;")
    print("  legal degrees ⊆ legal set (snap + self-derived tonic); dark_landing preferred;")
    print("  plain fallback when neither dark; develop_move plain; needs_key only; ears-off clean")
    print("  OK\n")

    # 59. T10: mirror-fidelity keystone (spec §12 F3a) — every _DEVELOP_TRANSFORMS
    # entry's prospective must equal transpose-then-transform; iterate the live
    # registry so future moves auto-cover.
    print("--- develop mirror-fidelity keystone (T10 / spec §12 F3a) ---")
    from build_index import phrase_vector as _pv59  # noqa: E402
    _saved_agg59 = _core._CACHE.get("aggregate")
    _saved_pk59 = _core._CACHE.get("project_keys")
    try:
        _core._CACHE["aggregate"] = {
            "melodic_grammar_by_role": {"lead": {"unigram": {"0": 0.6, "3": 0.2, "7": 0.2},
                                                 "order_1": {}, "order_2": {}}},
            "tempo": {"mean": 120.0},
        }
        _core._CACHE["project_keys"] = {"pG": "G minor"}

        def _idx_phrase59(pid, project, role, source, pitches, starts, vels, durs):
            notes = [{"pitch": p, "start": s, "duration": d, "velocity": v}
                     for p, s, v, d in zip(pitches, starts, vels, durs)]
            return {"id": pid, "project": project, "role": role, "kind": "harmonic",
                    "clip_source": source, "tempo": 122.0, "n_notes": len(notes),
                    "notes": notes, "vec": _pv59(notes)}

        _durs59 = [0.45] * 6 + [0.6]
        _hit59 = _idx_phrase59("pG|c0|w0", "pG", "lead", "session",
                               [67, 70, 74, 72, 70, 65, 67], [0.0, 0.5, 1.0, 2.0, 2.5, 3.0, 3.75],
                               [91, 63, 77, 84, 58, 96, 72], _durs59)
        _tpc59, _lp59, _med59 = 62, 74, 67
        _S59 = 2
        _K59 = "D minor"
        _prospective_base59 = _core._prospective_answer_notes(_hit59, _tpc59, _lp59, _med59)

        def _mirror_dispatch59(entry, seed, key_str):
            """Reproduce develop_my_loop's partial-apply branch for one registry entry."""
            if entry.get("needs_seed"):
                pass_index = min(seed, _core._SLOW_BURN_PASSES - 1)
                extra = {"key_str": key_str} if entry.get("needs_key") else {}
                return ("needs_seed", pass_index, extra)
            if entry.get("seed_param"):
                sp = entry["seed_param"]
                extra = {sp: seed}
                if entry.get("needs_key"):
                    extra["key_str"] = key_str
                return ("seed_param", extra)
            if entry.get("needs_key"):
                return ("needs_key", {"key_str": key_str})
            return ("plain", {})

        for _move59, _entry59 in _core._DEVELOP_TRANSFORMS.items():
            _keys59 = (_K59, None) if _entry59.get("needs_key") else (_K59,)
            for _k59 in _keys59:
                _dispatch59 = _mirror_dispatch59(_entry59, _S59, _k59)
                _xf59 = _entry59["transform"]
                _pf59 = _entry59["prospective"]
                if _xf59 is None:
                    if _dispatch59[0] == "needs_seed":
                        _actual59 = _pf59(_hit59, _tpc59, _lp59, _med59, _dispatch59[1],
                                          **_dispatch59[2])
                    elif _dispatch59[0] == "seed_param":
                        _actual59 = _pf59(_hit59, _tpc59, _lp59, _med59, **_dispatch59[1])
                    elif _dispatch59[0] == "needs_key":
                        _actual59 = _pf59(_hit59, _tpc59, _lp59, _med59, **_dispatch59[1])
                    else:
                        _actual59 = _pf59(_hit59, _tpc59, _lp59, _med59)
                    assert _actual59 == _prospective_base59, \
                        f"{_move59} transform=None mirror drift (key={_k59!r})"
                else:
                    if _dispatch59[0] == "needs_seed":
                        _expected59 = _xf59(_prospective_base59, _dispatch59[1], **_dispatch59[2])
                        _actual59 = _pf59(_hit59, _tpc59, _lp59, _med59, _dispatch59[1],
                                          **_dispatch59[2])
                    elif _dispatch59[0] == "seed_param":
                        _expected59 = _xf59(_prospective_base59, **_dispatch59[1])
                        _actual59 = _pf59(_hit59, _tpc59, _lp59, _med59, **_dispatch59[1])
                    elif _dispatch59[0] == "needs_key":
                        _expected59 = _xf59(_prospective_base59, **_dispatch59[1])
                        _actual59 = _pf59(_hit59, _tpc59, _lp59, _med59, **_dispatch59[1])
                    else:
                        _expected59 = _xf59(_prospective_base59)
                        _actual59 = _pf59(_hit59, _tpc59, _lp59, _med59)
                    assert _actual59 == _expected59, \
                        (f"{_move59} mirror drift (key={_k59!r}): "
                         f"expected {_expected59!r} actual {_actual59!r}")
    finally:
        if _saved_agg59 is None:
            _core._CACHE.pop("aggregate", None)
        else:
            _core._CACHE["aggregate"] = _saved_agg59
        if _saved_pk59 is None:
            _core._CACHE.pop("project_keys", None)
        else:
            _core._CACHE["project_keys"] = _saved_pk59
    print("  every _DEVELOP_TRANSFORMS entry: prospective == transpose→transform")
    print("  OK\n")

    # 60. T10: MCP surface + bare-motivic dispatch + tool-path ears-off / unknown move.
    print("--- develop_my_loop MCP surface (T10) ---")
    from build_index import phrase_vector as _pv60  # noqa: E402
    _saved_agg60 = _core._CACHE.get("aggregate")
    _saved_pk60 = _core._CACHE.get("project_keys")
    try:
        _dml_fn60 = getattr(develop_my_loop, "fn", develop_my_loop)
        _params60 = _inspect4.signature(_dml_fn60).parameters
        for _p60 in ("seed_notes", "move", "key", "tempo", "bars", "seed",
                     "use_ears", "direction"):
            assert _p60 in _params60, f"develop_my_loop MCP must expose {_p60!r}"

        _core._CACHE["aggregate"] = {
            "melodic_grammar_by_role": {"lead": {"unigram": {"0": 0.6, "3": 0.2, "7": 0.2},
                                                 "order_1": {}, "order_2": {}}},
            "tempo": {"mean": 120.0},
        }
        _core._CACHE["project_keys"] = {"pG": "G minor"}

        def _idx_phrase60(pid, project, role, source, pitches, starts, vels, durs):
            notes = [{"pitch": p, "start": s, "duration": d, "velocity": v}
                     for p, s, v, d in zip(pitches, starts, vels, durs)]
            return {"id": pid, "project": project, "role": role, "kind": "harmonic",
                    "clip_source": source, "tempo": 122.0, "n_notes": len(notes),
                    "notes": notes, "vec": _pv60(notes)}

        _seed60 = [{"pitch": p, "start_beats": s, "duration_beats": 0.45, "velocity": 90}
                   for p, s in zip([62, 65, 69, 67, 65, 62], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0])]
        _durs60 = [0.45] * 6 + [0.6]
        _pool60 = [
            _idx_phrase60("pG|c0|w0", "pG", "lead", "session",
                          [67, 70, 74, 72, 70, 65, 67], [0.0, 0.5, 1.0, 2.0, 2.5, 3.0, 3.75],
                          [91, 63, 77, 84, 58, 96, 72], _durs60),
            _idx_phrase60("pX|other", "pX", "lead", "session",
                          [60, 63, 67, 65, 63, 60, 62], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 3.75],
                          [80, 66, 74, 88, 60, 92, 70], _durs60),
        ]
        _core.set_phrase_pool_for_test(_pool60)

        _motivic_sorted60 = sorted(k for k in _core._DEVELOP_TRANSFORMS
                                   if k.startswith("motivic:"))
        _resolved60 = []
        for _k60 in range(5):
            _dev_m60 = _core.develop_my_loop(_seed60, move="motivic", key="D minor",
                                             seed=_k60, use_ears=False)
            _resolved60.append(_dev_m60["develop_move"])
            assert _dev_m60["develop_move"] == _motivic_sorted60[_k60 % 5], \
                (_k60, _dev_m60["develop_move"], _motivic_sorted60)
        assert len(set(_resolved60)) == 5, \
            f"bare motivic seeds 0..4 must resolve 5 distinct moves: {_resolved60}"

        _tool_off60 = _dml_fn60(seed_notes=_seed60, move=None, key="D minor", seed=1,
                                use_ears=False)
        assert "ears" not in _tool_off60 and "gate_report" not in _tool_off60, \
            "develop_my_loop tool with use_ears=False must add no ears keys"

        try:
            _dml_fn60(seed_notes=_seed60, move="not-a-real-move", key="D minor", seed=1)
            raise AssertionError("unknown move through the tool wrapper must raise")
        except ValueError as _exc60:
            assert "not-a-real-move" in str(_exc60), str(_exc60)
            assert str(sorted(_core._DEVELOP_TRANSFORMS)) in str(_exc60), \
                "the tool-path error must name the live roster"
    finally:
        _core.set_phrase_pool_for_test(None)
        if _saved_agg60 is None:
            _core._CACHE.pop("aggregate", None)
        else:
            _core._CACHE["aggregate"] = _saved_agg60
        if _saved_pk60 is None:
            _core._CACHE.pop("project_keys", None)
        else:
            _core._CACHE["project_keys"] = _saved_pk60
    print("  MCP params exposed; bare motivic resolves 5 distinct moves by seed;")
    print("  tool-path ears-off clean; unknown move through wrapper names roster")
    print("  OK\n")

    # 61. T11: sticky develop lifecycle — hold, rotate, serve route, pin gate (spec §7, §10).
    print("--- sticky develop lifecycle (T11) ---")
    import develop_session as _ds61  # noqa: E402
    import tempfile as _tf61  # noqa: E402
    _dev_td61 = _tf61.mkdtemp(prefix="develop_session61_")
    _dev_path61 = os.path.join(_dev_td61, "develop_session.json")
    _orig_dev_path61 = _ds61.STATE_PATH
    try:
        _fresh_dev_path61 = os.path.join(_dev_td61, "fresh", "develop_session.json")
        _fresh_state61 = _ds61.set_sticky(True, path=_fresh_dev_path61)
        assert _fresh_state61["sticky"] is True and os.path.exists(_fresh_dev_path61), \
            "a fresh nested develop path must create its lock parent before saving"
        _ds61.STATE_PATH = _dev_path61
        assert _ds61.load_state() == {"sticky": False, "hold": None}, \
            "sticky develop off by default"
        _ds61.set_sticky(True)
        _seed_hold61 = [{"pitch": 62, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 80},
                        {"pitch": 65, "start_beats": 0.5, "duration_beats": 0.5, "velocity": 80},
                        {"pitch": 69, "start_beats": 1.0, "duration_beats": 0.5, "velocity": 80}]
        _st61 = _ds61.record_keep("pG|c0|w0", _seed_hold61, "D minor")
        assert _st61["hold"]["pass"] == 0 and _st61["hold"]["move_cursor"] == 0
        _st61b = _ds61.record_keep("pG|c0|w0", _seed_hold61, "D minor")
        assert _st61b["hold"]["pass"] == 1 and _st61b["hold"]["move_cursor"] == 1
        _rot61 = _ds61._rotation()
        assert len(_rot61) == 8, _rot61
        assert _ds61.current_move(_st61) == _rot61[0]
        assert _ds61.current_move(_st61b) == _rot61[1]
        _st61c = _ds61.record_drop()
        assert _st61c["hold"] is None and _st61c["sticky"] is True
        assert _ds61.reset_state() == (
            "develop session reset — hold off, held idea cleared")
        assert not os.path.exists(_dev_path61)
        with open(_dev_path61, "w") as _bad61:
            _bad61.write("{not json")
        try:
            _ds61.load_state()
            raise AssertionError("malformed develop session must raise")
        except ValueError as _exc61:
            assert "reset develop" in str(_exc61), str(_exc61)
        os.unlink(_dev_path61)

        # judge_answer hook: KEEP advances hold, DROP clears (isolated paths)
        _core.set_ratings_path(os.path.join(_dev_td61, "ratings.json"))
        _ds61.set_sticky(True)
        _j61 = _gears.judge_answer(
            "keep", source_phrase_id="pG|c0|w0", answer_notes=_seed_hold61,
            seed_notes=_seed_hold61, key="D minor", develop_session_path=_dev_path61,
            ears_session_path=os.path.join(_dev_td61, "ears_session.json"))
        assert _j61.get("sticky_develop", "").startswith("hold on — ")
        assert "holding your idea, pass 0" in _j61.get("sticky_develop", "")
        _j61b = _gears.judge_answer(
            "keep", source_phrase_id="pG|c0|w0", answer_notes=_seed_hold61,
            seed_notes=_seed_hold61, key="D minor", develop_session_path=_dev_path61,
            ears_session_path=os.path.join(_dev_td61, "ears_session.json"))
        assert _j61b.get("sticky_develop", "").startswith("hold on — ")
        assert "pass 1" in _j61b.get("sticky_develop", "")
        _j61d = _gears.judge_answer(
            "drop", source_phrase_id="pG|c0|w0", answer_notes=_seed_hold61,
            develop_session_path=_dev_path61,
            ears_session_path=os.path.join(_dev_td61, "ears_session.json"))
        assert _ds61.load_state(_dev_path61)["hold"] is None

        # sticky serving route through answer_my_loop wrapper
        _hold_route61 = {
            "source_phrase_id": "pG|c0|w0",
            "seed_notes": _seed_hold61,
            "key": "D minor",
            "pass": 2,
            "move_cursor": 2,
        }
        _captured61: dict = {}
        _real_dml61 = _core.develop_my_loop
        _real_load61 = _ds61.load_state

        def _cap_dml61(**kw):
            _captured61.update(kw)
            return {"answer_notes": [], "source_phrase_ids": [kw.get("pin_source")]}

        def _sticky_load61(path=None):
            return {"sticky": True, "hold": _hold_route61}

        _aml_fn61 = getattr(answer_my_loop, "fn", answer_my_loop)
        try:
            _ds61.load_state = _sticky_load61
            _core.develop_my_loop = _cap_dml61
            _route61 = _aml_fn61(seed_notes=_seed_hold61, gear=2, use_ears=False)
            assert _captured61.get("pin_source") == "pG|c0|w0"
            assert _captured61.get("seed") == 2
            assert _captured61.get("move") == _rot61[2]
            assert _route61.get("sticky_develop", "").startswith("hold on — ")
            assert "hold on — holding your idea, pass 2" in _route61.get(
                "sticky_develop", "")
            # no_fit must not clear the hold
            def _nofit_dml61(**kw):
                return {"no_fit": True, "answer_notes": []}
            _core.develop_my_loop = _nofit_dml61
            _nofit61 = _aml_fn61(seed_notes=_seed_hold61, gear=2, use_ears=False)
            assert _nofit61.get("no_fit") is True
            assert _ds61.load_state()["hold"] == _hold_route61
        finally:
            _ds61.load_state = _real_load61
            _core.develop_my_loop = _real_dml61

        # ears-live pin: gated-out pinned id refuses; ears-off keeps bank fallback
        from build_index import phrase_vector as _pv61  # noqa: E402
        _saved_agg61 = _core._CACHE.get("aggregate")
        _saved_pk61 = _core._CACHE.get("project_keys")
        try:
            _core._CACHE["aggregate"] = {
                "melodic_grammar_by_role": {"lead": {"unigram": {"0": 0.6, "3": 0.2, "7": 0.2},
                                                     "order_1": {}, "order_2": {}}},
                "tempo": {"mean": 120.0},
            }
            _core._CACHE["project_keys"] = {"pG": "G minor"}

            def _idx61(pid, pitches, starts, busy=False):
                notes = [{"pitch": p, "start": s, "duration": 0.2 if busy else 0.5,
                            "velocity": 80}
                           for p, s in zip(pitches, starts)]
                return {"id": pid, "project": "pG", "role": "lead", "kind": "harmonic",
                        "clip_source": "session", "tempo": 76, "n_notes": len(notes),
                        "notes": notes, "vec": _pv61(notes)}

            _seed_pin61 = [{"pitch": p, "start_beats": s, "duration_beats": 0.45, "velocity": 90}
                           for p, s in zip([62, 65, 69, 67, 65, 62],
                                           [0.0, 0.5, 1.0, 1.5, 2.0, 3.0])]
            _fit61 = _idx61("fit61", [67, 70, 74, 72, 70, 65, 67],
                            [0.0, 0.5, 1.0, 2.0, 2.5, 3.0, 3.75])
            _wall61 = _idx61("wall61",
                             [62, 65, 69, 67] * 8, [i * 0.25 for i in range(32)],
                             busy=True)
            _core.set_ratings_path(os.path.join(_dev_td61, "ratings_pin61.json"))
            with open(os.path.join(_dev_td61, "ratings_pin61.json"), "w") as _rf61:
                json.dump({"records": []}, _rf61)
            _core.set_phrase_pool_for_test([_fit61, _wall61])
            _live_pin61 = _core.develop_my_loop(
                _seed_pin61, move="motivic:reharm", key="D minor", seed=0,
                use_ears=True, moment=_room43, pin_source="wall61")
            assert _live_pin61.get("no_fit") is True, \
                "live ears must refuse a pinned id gated out of the room"
            _off_pin61 = _core.develop_my_loop(
                _seed_pin61, move="motivic:reharm", key="D minor", seed=0,
                use_ears=False, pin_source="wall61")
            assert not _off_pin61.get("no_fit"), \
                "ears off must keep today's bank-fallback pin behaviour"
            assert _off_pin61["source_phrase_ids"] == ["wall61"]
        finally:
            _core.set_phrase_pool_for_test(None)
            if _saved_agg61 is None:
                _core._CACHE.pop("aggregate", None)
            else:
                _core._CACHE["aggregate"] = _saved_agg61
            if _saved_pk61 is None:
                _core._CACHE.pop("project_keys", None)
            else:
                _core._CACHE["project_keys"] = _saved_pk61
    finally:
        _ds61.STATE_PATH = _orig_dev_path61
        _core.set_ratings_path(None)
        import shutil as _sh61
        _sh61.rmtree(_dev_td61, ignore_errors=True)
    print("  off→on→KEEP advances→rotate→DROP→reset; malformed names reset develop;")
    print("  wrapper routes develop with pin_source/seed/move; no_fit keeps hold;")
    print("  live pin gated-out refuses, ears-off bank-fallback intact")
    print("  OK\n")

    # 62. T12: develop_ab measurement-only blind round (spec §6, §10).
    print("--- develop_ab measurement-only + build_develop_booth contracts ---")
    from ingest_booth_votes import (derive_develop_per_move as _dpm62,
                                    develop_promotion_status as _dps62,
                                    accumulate_develop_per_move as _apm62,
                                    notes_sha256 as _nsha62,
                                    verify_booth_note_bindings as _vbnb62,
                                    validate_booth_golds as _vbg62,
                                    plan_booth_ingest as _pbi62,
                                    booth_round_digest as _brd62,
                                    validate_booth_round_artifacts as _vbra62)
    import tempfile as _tmp62
    _dev_notes62 = [{"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 80}]
    _ret_notes62 = [{"pitch": 64, "start": 0.0, "duration": 1.0, "velocity": 80}]
    _b_answer62 = [{"pitch": 67, "start": 0.0, "duration": 1.0, "velocity": 80}]
    _pick62 = "pG|harmonic|w0"
    _a_id62 = "develop:%s:slow-burn:reveal" % _pick62
    _dev_manifest62 = [{
        "pair_id": 1, "A_phrase_id": _a_id62, "B_phrase_id": _pick62,
        "selection_mode": "develop_ab",
        "A_notes_sha256": _nsha62(_dev_notes62),
        "B_notes_sha256": _nsha62(_b_answer62),
    }]
    _dev_preds62 = {
        "model": {"kind": "develop_ab"},
        "pairs": [{
            "pair_id": 1, "A_phrase_id": _a_id62, "B_phrase_id": _pick62,
            "selection_mode": "develop_ab", "predicted_winner": _a_id62,
            "A_notes": _dev_notes62,
            "B_notes": _b_answer62,
        }],
    }
    _dev_digest62 = _brd62(_dev_manifest62, _dev_preds62)
    _dev_preds62["artifact_digest"] = _dev_digest62
    _dev_preds62["round_id"] = "booth_" + _dev_digest62[:12]
    assert _vbra62(_dev_manifest62, _dev_preds62) == _dev_preds62["round_id"], \
        "develop_ab round must validate under the frozen-artifact contract"
    _by_id62 = {_pick62: {"id": _pick62, "notes": _ret_notes62}}
    _vbnb62(_dev_manifest62, _by_id62, _dev_preds62)  # sidecar A/B_notes match (B != index)
    _tampered_preds62 = json.loads(json.dumps(_dev_preds62))
    _tampered_preds62["pairs"][0]["A_notes"] = [{"pitch": 99, "start": 0.0,
                                                 "duration": 1.0, "velocity": 80}]
    try:
        _vbnb62(_dev_manifest62, _by_id62, _tampered_preds62)
        raise AssertionError("tampered develop A_notes must fail note-binding")
    except ValueError as _exc62a:
        assert "developed A notes" in str(_exc62a) or "sidecar" in str(_exc62a) or \
            "develop A notes" in str(_exc62a)
    _tampered_b62 = json.loads(json.dumps(_dev_preds62))
    _tampered_b62["pairs"][0]["B_notes"] = [{"pitch": 99, "start": 0.0,
                                             "duration": 1.0, "velocity": 80}]
    try:
        _vbnb62(_dev_manifest62, _by_id62, _tampered_b62)
        raise AssertionError("tampered develop B_notes must fail note-binding")
    except ValueError as _exc62a2:
        assert "develop B notes" in str(_exc62a2) or "sidecar" in str(_exc62a2)
    # non-develop regression: index-bound hash still passes; drifted index still fails
    _rand_notes62 = [{"pitch": 55, "start": 0.0, "duration": 1.0, "velocity": 80}]
    _rand_id62 = "pR|melodic|w1"
    _rand_manifest62 = [{
        "pair_id": 1, "A_phrase_id": _rand_id62, "B_phrase_id": "pS|melodic|w2",
        "selection_mode": "random",
        "A_notes_sha256": _nsha62(_rand_notes62),
    }]
    _rand_by62 = {_rand_id62: {"id": _rand_id62, "notes": _rand_notes62}}
    _vbnb62(_rand_manifest62, _rand_by62, None)
    _drift_by62 = {_rand_id62: {"id": _rand_id62, "notes": [{"pitch": 99, "start": 0.0,
                                                              "duration": 1.0, "velocity": 80}]}}
    try:
        _vbnb62(_rand_manifest62, _drift_by62, None)
        raise AssertionError("drifted index must fail non-develop note-binding")
    except ValueError as _exc62d:
        assert "changed since the round was frozen" in str(_exc62d)
    # _stage_clips: develop_ab sources sidecar notes for both sides (no Live)
    import session as _session62
    import live_write as _lw62
    _clip_list62 = []
    _orig_stage62 = _lw62.stage_named_clips

    async def _cap_stage62(clips):
        _clip_list62.extend(clips)
        return {"verified_ok": True, "placed": [{"name": c["name"], "ok": True}
                                                 for c in clips],
                "start_slot": 1, "track_created": False}

    try:
        _lw62.stage_named_clips = _cap_stage62
        _session62._stage_clips("booth", _dev_manifest62, _by_id62, _dev_preds62)
        assert len(_clip_list62) == 2, _clip_list62
        assert _clip_list62[0]["notes"] == _dev_notes62
        assert _clip_list62[1]["notes"] == _b_answer62
        assert _clip_list62[1]["notes"] != _ret_notes62, \
            "develop B clip must be transposed answer, not raw index phrase"
    finally:
        _lw62.stage_named_clips = _orig_stage62
    def _rebind62(manifest, preds):
        d = _brd62(manifest, preds)
        return manifest, {**preds, "artifact_digest": d, "round_id": "booth_" + d[:12]}
    _stamped_taste62, _stamped_taste_preds62 = _rebind62(
        _dev_manifest62, {"model": {}, "pairs": _dev_preds62["pairs"]})
    try:
        _vbra62(_stamped_taste62, _stamped_taste_preds62)
        raise AssertionError("a develop_ab round stamped taste must be rejected")
    except ValueError as _exc62b:
        assert "develop_ab" in str(_exc62b)
    _ears_pair62 = [{"pair_id": 1, "A_phrase_id": "a", "B_phrase_id": "b",
                     "selection_mode": "ears_ab"}]
    _mixed62 = _dev_manifest62 + [{"pair_id": 2, "A_phrase_id": "c",
                                   "B_phrase_id": "d", "selection_mode": "develop_ab"}]
    _mixed62, _mixed_preds62 = _rebind62(_mixed62, {
        "model": {"kind": "develop_ab"}, "pairs": _dev_preds62["pairs"] + [{
            "pair_id": 2, "A_phrase_id": "develop:d|x|0:motivic:retro",
            "B_phrase_id": "d", "selection_mode": "develop_ab",
            "predicted_winner": "develop:d|x|0:motivic:retro", "A_notes": _dev_notes62}]})
    try:
        _vbra62(_mixed62, _mixed_preds62)
        raise AssertionError("develop_ab must not mix with ears_ab")
    except ValueError:
        pass
    _ears_preds62 = {"model": {"kind": "ears_ab"}, "pairs": [
        {"pair_id": 1, "A_phrase_id": "a", "B_phrase_id": "b",
         "selection_mode": "ears_ab", "predicted_winner": "a"}]}
    _mixed_ears62, _mixed_ears_preds62 = _rebind62(
        _ears_pair62 + [_dev_manifest62[0]], {
            "model": {"kind": "ears_ab"},
            "pairs": _ears_preds62["pairs"] + _dev_preds62["pairs"]})
    try:
        _vbra62(_mixed_ears62, _mixed_ears_preds62)
        raise AssertionError("ears_ab must not mix with develop_ab")
    except ValueError:
        pass
    _decided62 = [{**_dev_manifest62[0], "winner": "A"}]
    _plan62 = _pbi62(_decided62, _dev_preds62, lambda _p: False,
                     set(_by_id62) | {_a_id62})
    assert _plan62["train"] == [], "develop_ab must never reach rate_candidates"
    assert _plan62["scoreboard"]["n_random"] == 1 and \
        _plan62["scoreboard"]["n_agree"] == 1, "develop agreement must count"
    _pm62 = _dpm62(_decided62, _dev_preds62)
    assert _pm62.get("slow-burn:reveal") == {"decided": 1, "agree": 1}, _pm62
    # swapped side: developed id on B — scoring and binding stay id-based, not letter-based
    _swap_manifest62 = [{
        "pair_id": 2, "A_phrase_id": _pick62, "B_phrase_id": _a_id62,
        "selection_mode": "develop_ab",
        "A_notes_sha256": _nsha62(_b_answer62),
        "B_notes_sha256": _nsha62(_dev_notes62),
    }]
    _swap_preds62 = {
        "model": {"kind": "develop_ab"},
        "pairs": [{
            "pair_id": 2, "A_phrase_id": _pick62, "B_phrase_id": _a_id62,
            "selection_mode": "develop_ab", "predicted_winner": _a_id62,
            "A_notes": _b_answer62,
            "B_notes": _dev_notes62,
        }],
    }
    _swap_digest62 = _brd62(_swap_manifest62, _swap_preds62)
    _swap_preds62["artifact_digest"] = _swap_digest62
    _swap_preds62["round_id"] = "booth_" + _swap_digest62[:12]
    assert _vbra62(_swap_manifest62, _swap_preds62) == _swap_preds62["round_id"]
    _vbnb62(_swap_manifest62, _by_id62, _swap_preds62)
    _tampered_swap_a62 = json.loads(json.dumps(_swap_preds62))
    _tampered_swap_a62["pairs"][0]["A_notes"] = [{"pitch": 99, "start": 0.0,
                                                  "duration": 1.0, "velocity": 80}]
    try:
        _vbnb62(_swap_manifest62, _by_id62, _tampered_swap_a62)
        raise AssertionError("tampered swapped develop A_notes must fail note-binding")
    except ValueError as _exc62sa:
        assert "develop A notes" in str(_exc62sa) or "sidecar" in str(_exc62sa)
    _tampered_swap_b62 = json.loads(json.dumps(_swap_preds62))
    _tampered_swap_b62["pairs"][0]["B_notes"] = [{"pitch": 99, "start": 0.0,
                                                  "duration": 1.0, "velocity": 80}]
    try:
        _vbnb62(_swap_manifest62, _by_id62, _tampered_swap_b62)
        raise AssertionError("tampered swapped develop B_notes must fail note-binding")
    except ValueError as _exc62sb:
        assert "develop B notes" in str(_exc62sb) or "sidecar" in str(_exc62sb)
    _decided_swap62 = [{**_swap_manifest62[0], "winner": "B"}]
    _plan_swap62 = _pbi62(_decided_swap62, _swap_preds62, lambda _p: False,
                          set(_by_id62) | {_a_id62})
    assert _plan_swap62["scoreboard"]["n_random"] == 1 and \
        _plan_swap62["scoreboard"]["n_agree"] == 1, \
        "winner=B on swapped develop pair must agree via phrase id"
    _pm_swap62 = _dpm62(_decided_swap62, _swap_preds62)
    assert _pm_swap62.get("slow-burn:reveal") == {"decided": 1, "agree": 1}, _pm_swap62
    _rule62 = {"min_agreement": 0.7, "min_random_pairs": 5, "consecutive_rounds": 3}
    _dok62 = {"n_random": 6, "agreement": 0.8, "kind": "develop_ab",
              "per_move": {"slow-burn:reveal": {"decided": 6, "agree": 5}}}
    assert _dps62([_dok62], _rule62) == {"qualifying_streak": 1, "promotable": False}, \
        "one qualifying develop round is NOT promotion-ready"
    assert _dps62([_dok62, _dok62, _dok62], _rule62)["promotable"] is True
    assert _dps62([_dok62, dict(_dok62, kind="taste"), _dok62, _dok62],
                  _rule62)["qualifying_streak"] == 3, \
        "non-develop rounds are transparent to the develop streak"
    _acc62 = _apm62([_dok62, dict(_dok62, per_move={"motivic:retro": {"decided": 2,
                                                                        "agree": 1}})])
    assert _acc62["slow-burn:reveal"]["decided"] == 6 and \
        _acc62["motivic:retro"]["decided"] == 2
    try:
        _vbg62(["develop:x|y|0:m"], _decided62, {1: "A"})
        raise AssertionError("develop: ids must never become gold anchors")
    except ValueError as _exc62c:
        assert "developed" in str(_exc62c).lower() or "develop:" in str(_exc62c)
    with _tmp62.TemporaryDirectory() as _td62:
        _ratings62 = os.path.join(_td62, "ratings_develop62.json")
        with open(_ratings62, "w") as _rf62:
            json.dump({"records": [], "candidate_rounds": []}, _rf62)
        _core.set_ratings_path(_ratings62)
        try:
            _before62 = _core._taste_model_summary()
            for t in _plan62["train"]:
                raise AssertionError("develop plan must not train")
            _after62 = _core._taste_model_summary()
            assert _before62["n_pairs"] == _after62["n_pairs"], \
                "develop_ab ingest must leave taste n_pairs unchanged"
        finally:
            _core.set_ratings_path(None)
    with _tmp62.TemporaryDirectory() as _bd62:
        _man_before62 = os.path.join(_bd62, "pairwise_manifest.json")
        _pred_before62 = os.path.join(_bd62, "pairwise_predictions.json")
        _marker62 = b"marker-round-unchanged"
        with open(_man_before62, "wb") as _mf62:
            _mf62.write(_marker62)
        with open(_pred_before62, "wb") as _pf62:
            _pf62.write(_marker62)
        import importlib.util as _ilu62
        _spec62 = _ilu62.spec_from_file_location(
            "build_develop_booth62",
            os.path.join(ROOT, "tools", "build_develop_booth.py"))
        _bdb62 = _ilu62.module_from_spec(_spec62)
        _spec62.loader.exec_module(_bdb62)
        _orig_man62 = _bdb62.MANIFEST
        _orig_pred62 = _bdb62.PREDICTIONS
        try:
            _bdb62.MANIFEST = _man_before62
            _bdb62.PREDICTIONS = _pred_before62
            import io as _io62
            import contextlib as _ctx62
            _argv62 = sys.argv
            sys.argv = ["build_develop_booth.py", "--dry-run", "--n", "16", "--seed", "7"]
            try:
                with _ctx62.redirect_stdout(_io62.StringIO()):
                    _bdb62.main()
            finally:
                sys.argv = _argv62
            assert open(_man_before62, "rb").read() == _marker62, \
                "build_develop_booth --dry-run must not write manifest"
            assert open(_pred_before62, "rb").read() == _marker62, \
                "build_develop_booth --dry-run must not write predictions"
        finally:
            _bdb62.MANIFEST = _orig_man62
            _bdb62.PREDICTIONS = _orig_pred62
    # builder: _DEVELOP_SEED=2 yields slow-burn:add survivors OR loud zero warning
    _idx62 = json.load(open(os.path.join(ROOT, "store/phrase_index.json")))
    _phrases62 = _idx62 if isinstance(_idx62, list) else _idx62.get("phrases", [])
    _pairs_seed62, _mc_seed62 = _bdb62.build_pairs(_phrases62, 16, 7, _core)
    _add_count62 = _mc_seed62.get("slow-burn:add", 0)
    if _add_count62 == 0:
        import io as _io62b
        import contextlib as _ctx62b
        _argv62b = sys.argv
        sys.argv = ["build_develop_booth.py", "--dry-run", "--n", "16", "--seed", "7"]
        try:
            with _ctx62b.redirect_stdout(_io62b.StringIO()) as _cap62b:
                _bdb62.main()
            _dry_out62 = _cap62b.getvalue()
            assert "WARNING: ZERO surviving pairs" in _dry_out62 and \
                "slow-burn:add" in _dry_out62, \
                "zero slow-burn:add must emit loud roster warning"
        finally:
            sys.argv = _argv62b
    else:
        assert _add_count62 >= 1, "expected slow-burn:add survivors at _DEVELOP_SEED=2"
    print("  develop_ab validates; taste/ears mixing rejected; train empty;")
    print("  per-move + develop streak math; sidecar A/B_notes binding; gold reject;")
    print("  swapped-side develop (dev=B) agreement + per-move; non-develop index binding")
    print("  regression; develop staging from sidecar; dry-run writes nothing;")
    print("  _DEVELOP_SEED roster coverage")
    print("  OK\n")

    # 63. device_readout: injected moment, stranger-clear gear one-liners.
    print("--- device readout contract (injected moment) ---")
    import device_readout as _dr63  # noqa: E402
    import moment as _moment63  # noqa: E402
    _syn63 = _moment63.build_moment(
        [{"name": "Lead", "notes": [
            {"pitch": 62, "start": 0.0, "duration": 1.0, "velocity": 80},
            {"pitch": 65, "start": 1.0, "duration": 1.0, "velocity": 80}]}],
        tempo=120.0)
    _called63: list = []

    def _boom_get_moment63(*_a63, **_k63):
        _called63.append(True)
        raise AssertionError("get_moment must not run when moment is injected")

    # the booby trap is scoped to THIS call and restored — a leaked stub turns
    # every later block's get_moment into a landmine (it did: block 89's
    # arrangement-blind assertion hit this one)
    _prev_gm63 = _moment63.get_moment
    try:
        _moment63.get_moment = _boom_get_moment63
        _out63 = _dr63.device_readout(_syn63)
    finally:
        _moment63.get_moment = _prev_gm63
    assert not _called63, "injected moment must bypass get_moment"
    assert _out63["ears_line"] == _ears43.status_line(_syn63), _out63
    assert _out63["hands_line"] == "not holding — answers start fresh"
    assert len(_out63["gear_names"]) == 6
    for _g63, _line63 in _out63["gear_names"].items():
        assert " — " in _line63, _line63
        assert _line63 == _dr63.GEAR_NAMES[int(_g63)]
    print("  injected moment; ears/hands lines; six em-dash gear one-liners")
    print("  OK\n")

    # 64. /sombra/sticky OSC: toggle flips develop session (temp path).
    print("--- sombra sticky OSC dispatch ---")
    import sombra_listener as _sl64  # noqa: E402
    import develop_session as _ds64  # noqa: E402
    import tempfile as _tf64  # noqa: E402
    _td64 = _tf64.mkdtemp(prefix="sombra_sticky64_")
    _dev64 = os.path.join(_td64, "develop_session.json")
    try:
        _ds64.STATE_PATH = _dev64
        _p64a = _sl64.dispatch(
            {"address": "/sombra/sticky", "args": [1]}, execute=True,
            develop_path=_dev64)
        assert _p64a["reply"] == "hold on — your next answer sticks"
        assert _ds64.load_state(_dev64)["sticky"] is True
        _p64b = _sl64.dispatch(
            {"address": "/sombra/sticky", "args": ["toggle"]}, execute=True,
            develop_path=_dev64)
        assert _p64b["reply"] == "hold off — answers start fresh"
        assert _ds64.load_state(_dev64)["sticky"] is False
        _plan64 = _sl64.dispatch(
            {"address": "/sombra/sticky", "args": ["toggle"]}, execute=False)
        assert _plan64["action"] == "sticky" and _plan64["sticky_arg"] == "toggle"
    finally:
        _ds64.STATE_PATH = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tools", "ideas", "develop_session.json")
    print("  sticky 1/on; toggle flips; plan-only path")
    print("  OK\n")

    # 65. /sombra/ears/reset: canonical string; develop hold untouched.
    print("--- sombra ears reset OSC (develop hold preserved) ---")
    import ears as _ears65  # noqa: E402
    _td65 = _tf64.mkdtemp(prefix="sombra_reset65_")
    _ears65_path = os.path.join(_td65, "ears_session.json")
    _dev65 = os.path.join(_td65, "develop_session.json")
    _ears65.save_session({"streaks": {"density:busy": 3}, "demoted": ["density:busy"]},
                         _ears65_path)
    _ds64.set_sticky(True, _dev65)
    _seed65 = [{"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 80}]
    _ds64.record_keep("pX|c0|w0", _seed65, "D minor", _dev65)
    _r65 = _sl64.dispatch(
        {"address": "/sombra/ears/reset", "args": []}, execute=True,
        ears_path=_ears65_path, develop_path=_dev65)
    assert _r65["reply"] == "ears reset — session demotions cleared"
    assert not os.path.exists(_ears65_path)
    _hold65 = _ds64.load_state(_dev65)["hold"]
    assert _hold65 is not None and _hold65["pass"] == 0
    print("  canonical reset string; ears file gone; sticky hold intact")
    print("  OK\n")

    # 66. /sombra/status OSC: both readout lines via injected moment seam.
    print("--- sombra status OSC dispatch ---")
    _ds64.set_sticky(True, _dev65)
    _ds64.record_drop(_dev65)
    _st66 = _sl64.dispatch(
        {"address": "/sombra/status", "args": []}, execute=True,
        moment=_syn63, develop_path=_dev65)
    assert _st66["reply"]["ears_line"] == _ears43.status_line(_syn63)
    assert _st66["reply"]["hands_line"] == "ready to hold — your next answer sticks"
    assert set(_st66["reply"]["gear_names"]) == {1, 2, 3, 4, 5, 6}
    assert _st66["reply"]["audition"] is False
    print("  status reply carries ears_line + hands_line + gear_names + audition")
    print("  OK\n")

    # 67. gear_names: em-dash descriptions, no uppercase project-ish tokens.
    print("--- gear_names stranger-clear microcopy ---")
    _bad67 = ("Ricardo", "SIDEKICK", "JAM", "Ableton", "SOMBRA", "MCP")
    for _n63 in _dr63.GEAR_NAMES.values():
        assert " — " in _n63
        for _tok67 in _bad67:
            assert _tok67 not in _n63, _n63
        assert _n63 == _n63.lower() or "your" in _n63, _n63
    print("  six gears: em-dash + second-person, no lore tokens")
    print("  OK\n")

    # 68. sombra_ui.js: ES5 syntax-clean (node --check).
    print("--- sombra_ui.js syntax (node --check) ---")
    import shutil as _sh68
    import subprocess as _sp68
    _ui68 = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tools", "m4l", "sombra_ui.js")
    if _sh68.which("node"):
        _chk68 = _sp68.run(
            ["node", "--check", _ui68], capture_output=True, text=True)
        assert _chk68.returncode == 0, _chk68.stderr or _chk68.stdout
        print("  node --check tools/m4l/sombra_ui.js exits 0")
    else:
        print("  node absent — syntax check skipped")
    print("  OK\n")

    # 69. gears ears plumbing + empty-surgery guards
    print("--- gears ears plumbing + empty-surgery guards ---")
    import gears as _gears69  # noqa: E402
    import inspect as _insp69  # noqa: E402

    _sig_awg69 = _insp69.signature(_gears69.answer_with_gear)
    assert _sig_awg69.parameters["use_ears"].kind == _insp69.Parameter.KEYWORD_ONLY
    assert _sig_awg69.parameters["moment"].kind == _insp69.Parameter.KEYWORD_ONLY
    _sig_gml69 = _insp69.signature(_core.grow_my_loop)
    assert _sig_gml69.parameters["use_ears"].kind == _insp69.Parameter.KEYWORD_ONLY
    assert _sig_gml69.parameters["moment"].kind == _insp69.Parameter.KEYWORD_ONLY

    _seed69 = [{"pitch": 62, "start_beats": b * 1.0, "duration_beats": 0.9,
                "velocity": 80} for b in range(4)]
    _g69a = _gears69.answer_with_gear(_seed69, gear=2, use_ears=False)
    _g69b = _gears69.answer_with_gear(_seed69, gear=2)
    assert _g69a == _g69b, "explicit use_ears=False must match the bare (off-default) call"
    for _k69 in ("ears", "gate_report", "ears_status"):
        assert _k69 not in _g69a, f"ears-off gear 2 must stay byte-identical: found {_k69!r}"

    _room69 = _moment42.build_moment(
        [{"name": "Drums", "notes": [{"pitch": 36, "start": i * 0.25, "duration": 0.2,
                                      "velocity": 100} for i in range(32)]},
         {"name": "Lead x", "notes": [{"pitch": p, "start": i * 1.0, "duration": 0.9,
                                       "velocity": 90}
                                      for i, p in enumerate((74, 77, 81, 74))]}],
        tempo=75.0)
    _g69c = _gears69.answer_with_gear(_seed69, gear=2, use_ears=True, moment=_room69)
    assert _g69c.get("ears") == "live", "an injected live moment must ride through to gear 2"

    for _gear69 in (1, 3):
        _g69_ears = _gears69.answer_with_gear(
            _seed69, gear=_gear69, use_ears=True, moment=_room69)
        assert _g69_ears.get("ears") == "live", \
            f"gear {_gear69} must ride ears through with an injected live moment"
        if _g69_ears.get("no_fit"):
            assert not _g69_ears.get("answer_notes"), \
                f"gear {_gear69} no_fit must carry ears live, not a silent ungated pick"

    _aml_fn69 = getattr(answer_my_loop, "fn", answer_my_loop)
    assert "moment" in _insp69.signature(_aml_fn69).parameters, \
        "answer_my_loop MCP must expose moment for injected-room selftests"
    _wrap69 = _aml_fn69(seed_notes=_seed69, gear=3, use_ears=True, moment=_room69)
    assert "ears" in _wrap69 or _wrap69.get("no_fit"), \
        "MCP answer_my_loop gear≠2 must not silently drop use_ears"
    if "ears" in _wrap69:
        assert _wrap69["ears"] == "live"

    assert _gears69._ending_variation([], "D minor") == []
    assert _gears69.apply_surgery([], "octave_displacement", "D minor") == [], \
        "_octave_variant must guard empty input, not crash on max() of an empty list"
    print("  keyword-only use_ears/moment; gear 1/2/3 ears wiring; MCP wrapper forwards ears; empty-input surgery guards")
    print("  OK\n")

    # 69b. P1: gears 1/3 must gate FINAL notes (not an intermediate), fail closed
    print("--- gear 1/3 ears: final notes match the gate ---")
    _bass_room69b = _moment42.build_moment(
        [{"name": "Bass", "notes": [
            {"pitch": 38, "start": i * 1.0, "duration": 0.9, "velocity": 90}
            for i in range(4)]}],
        tempo=120.0)
    assert _bass_room69b["ears"] == "live"
    assert _bass_room69b["roles"]["bass"]["register_band"] == "low"

    # gear 3 bass_shadow drops into low after the pre-surgery gate — must refuse
    _g3_low69b = _gears69.answer_with_gear(
        _seed69, gear=3, key="D minor", surgery="bass_shadow", seed=0,
        use_ears=True, moment=_bass_room69b)
    assert _g3_low69b.get("ears") == "live", _g3_low69b
    assert _g3_low69b.get("no_fit") is True, \
        "bass_shadow into a bass-occupied low band must fail closed, not serve colliding notes"
    assert not _g3_low69b.get("answer_notes"), _g3_low69b.get("answer_notes")
    assert "ears:refused_final" in (_g3_low69b.get("transforms_applied") or [])

    # gear 1 verbatim of a low catalog phrase: intermediate may pass high, final must refuse
    _fake_low69b = {
        "answer_notes": [
            {"pitch": p, "start_beats": float(i), "duration_beats": 0.9, "velocity": 80}
            for i, p in enumerate((36, 38, 41, 43))
        ],
        "key": "D minor",
        "tempo": 120.0,
        "source_phrase_ids": ["low|lead"],
        "transforms_applied": ["verbatim"],
        "ears": "live",
        "gate_report": {"pool": 1, "survived": 1, "dropped": []},
        "announcements": [],
    }
    _regate69b = _gears69._ears_regate_final(
        dict(_fake_low69b), use_ears=True, moment=_bass_room69b)
    assert _regate69b.get("no_fit") is True and not _regate69b.get("answer_notes")
    assert _regate69b.get("ears") == "live"
    # high final notes in the same room must still pass (band free for lead)
    _fake_high69b = {
        **_fake_low69b,
        "answer_notes": [
            {"pitch": p, "start_beats": float(i), "duration_beats": 0.9, "velocity": 80}
            for i, p in enumerate((74, 77, 81, 74))
        ],
    }
    _ok69b = _gears69._ears_regate_final(
        dict(_fake_high69b), use_ears=True, moment=_bass_room69b)
    assert not _ok69b.get("no_fit"), _ok69b
    assert len(_ok69b.get("answer_notes") or []) == 4
    print("  gear3 bass_shadow refuses over bass; low verbatim refused; high final admitted")
    print("  OK\n")

    # 69c. P1: answer_my_loop(transform=…) must gate the transformed prospective
    print("--- answer_my_loop transform: prospective matches final ---")
    _pros_plain69c = _core._prospective_answer_notes
    _hit69c = {
        "notes": [
            {"pitch": p, "start": float(i), "duration": 0.9, "velocity": 80}
            for i, p in enumerate((74, 77, 81, 74))
        ],
        "project": "pG",
    }
    _base69c = _pros_plain69c(_hit69c, 2, 62, 74)  # D minor-ish seed context
    _frag_pros69c = _core._prospective_with_transform(_core._fragment_notes)
    _frag69c = _frag_pros69c(_hit69c, 2, 62, 74)
    assert _frag69c != _base69c or len(_frag69c) != len(_base69c), \
        "fragment prospective must differ from plain transpose"
    # reharmonize changes pitches
    _re_pros69c = _core._prospective_with_transform(_core._reharmonize_notes)
    _re69c = _re_pros69c(_hit69c, 2, 62, 74)
    assert _re69c, "reharmonize prospective must return notes"
    # wiring: answer_my_loop with transform uses composed prospective (no crash;
    # ears-off still works)
    _seed69c = [{"pitch": 62, "start_beats": b * 1.0, "duration_beats": 0.9,
                 "velocity": 80} for b in range(4)]
    _ans_frag69c = _core.answer_my_loop(
        _seed69c, key="D minor", transform="fragment", seed=0, use_ears=False)
    assert "fragment" in (_ans_frag69c.get("transforms_applied") or [])

    # The contract, not just the helper: with ears LIVE the pool handed to
    # ears.serve must carry the TRANSFORMED notes. Reverting prospective_fn to
    # the plain mirror would make these three pools identical — that is exactly
    # the regression this pins.
    import ears as _ears69c
    _room69c = _moment42.build_moment(
        [{"name": "Bass", "notes": [
            {"pitch": 38, "start": i * 1.0, "duration": 0.9, "velocity": 90}
            for i in range(4)]}],
        tempo=120.0)
    _pools69c: list[list] = []
    _serve_prev69c = _ears69c.serve

    def _spy_serve69c(candidates, m, **kw):
        _pools69c.append([list(c.get("notes") or []) for c in candidates])
        return _serve_prev69c(candidates, m, **kw)

    _ears69c.serve = _spy_serve69c  # type: ignore[assignment]
    try:
        for _t69c in (None, "reharmonize", "fragment"):
            _core.answer_my_loop(_seed69c, key="D minor", transform=_t69c,
                                 seed=0, use_ears=True, moment=_room69c)
    finally:
        _ears69c.serve = _serve_prev69c  # type: ignore[assignment]
    assert len(_pools69c) == 3, _pools69c
    assert _pools69c[0] != _pools69c[1], \
        "reharmonize: the room must be shown the transformed notes, not the plain transpose"
    assert _pools69c[0] != _pools69c[2], \
        "fragment: the room must be shown the transformed notes, not the plain transpose"

    # ...and the gate's VERDICT must change with it. Synthetic (index-independent):
    # a long sparse phrase stays sparse plain, but fragment repeats its motif into
    # mid density — so direction='sparser' admits one and refuses the other.
    _sparse69c = {
        "notes": [{"pitch": p, "start": i * 0.5, "duration": 0.4, "velocity": 80}
                  for i, p in enumerate((74, 77, 81, 79))]
        + [{"pitch": 74, "start": 28.0, "duration": 0.5, "velocity": 80}],
        "project": "selftest-69c",
    }
    _plain69c = _core._prospective_answer_notes(_sparse69c, 2, 62, 74)
    _fragged69c = _core._prospective_with_transform(_core._fragment_notes)(
        _sparse69c, 2, 62, 74)
    assert _moment42.density_band(_plain69c) == "sparse", _moment42.density_band(_plain69c)
    assert _moment42.density_band(_fragged69c) != "sparse", "fragment must change the band"
    _adm_plain69c, _ = _ears69c.admit(
        [{"id": "plain", "notes": _plain69c, "role": "lead", "key": "D minor",
          "tempo": 120.0}], _room69c, role="lead", direction="sparser")
    _adm_frag69c, _ = _ears69c.admit(
        [{"id": "frag", "notes": _fragged69c, "role": "lead", "key": "D minor",
          "tempo": 120.0}], _room69c, role="lead", direction="sparser")
    assert len(_adm_plain69c) == 1 and len(_adm_frag69c) == 0, (
        "gating the plain transpose would admit material the transform makes "
        f"inadmissible (plain={len(_adm_plain69c)}, fragment={len(_adm_frag69c)})"
    )
    print("  transform composed into prospective; live gate pool carries the transform")
    print("  OK\n")

    # 70. F3b: backing gated against the room
    print("--- F3b: backing gated against the room ---")
    _bass70 = [{"pitch": 38, "start": 0.0, "duration": 1.0, "velocity": 90}]       # D2
    _chord70 = [{"pitch": 62, "start": 0.0, "duration": 2.0, "velocity": 70},      # D4
                {"pitch": 65, "start": 0.0, "duration": 2.0, "velocity": 70},      # F4
                {"pitch": 69, "start": 0.0, "duration": 2.0, "velocity": 70}]      # A4
    _m70 = _moment42.build_moment(
        [{"name": "Bass", "notes": _bass70}, {"name": "Pad", "notes": _chord70}],
        tempo=120.0)
    assert _m70["ears"] == "live" and _m70["key"] == "D minor"
    assert _m70["roles"]["bass"]["register_band"] == "low"
    assert _m70["roles"]["harmony"]["register_band"] == "mid"
    _m70_bass = _moment42.build_moment([{"name": "Bass", "notes": _bass70}], tempo=120.0)
    assert set(_m70_bass["roles"]) == {"bass"}

    def _assert_backing_clean70(result, roles):
        """Any suggestion still carrying `backing` must have no part whose role is
        already occupied in the room, and no part whose register band collides
        with a DIFFERENT occupied role — _gate_backing's own invariant."""
        for sug in result["suggestions"]:
            backing = sug.get("backing")
            assert backing != {}, "an emptied backing dict must be deleted, not left empty"
            if not backing:
                continue
            for part, part_role in (("pad", "harmony"), ("bass", "bass")):
                notes = backing.get(part)
                if notes is None:
                    continue
                assert part_role not in roles, \
                    f"a surviving backing {part} must not share an occupied role"
                band = _moment42.register_band(_core._note_dicts(notes))
                assert not any(r != part_role and info.get("register_band") == band
                              for r, info in roles.items()), \
                    f"a surviving backing {part} must not collide on register"

    # suggest_growth, both bass+harmony occupied: the harmony-role candidate is
    # dropped by the outer ROLE gate before _gate_backing ever inspects it (real
    # behavior for this seed/corpus) — no suggestions survive, no backing announcement.
    _sg70_both = _core.suggest_growth(n=3, key="D minor", seed=0, use_ears=True, moment=_m70)
    _assert_backing_clean70(_sg70_both, _m70["roles"])
    assert not any(a.startswith("left out the backing") for a in _sg70_both["announcements"]), \
        "with the harmony role already gated out upstream, no backing announcement fires"

    # suggest_growth, bass only: the harmony-role candidate now clears the outer
    # gates, so _gate_backing runs and strips only the colliding bass part.
    _sg70_bass = _core.suggest_growth(n=3, key="D minor", seed=0, use_ears=True,
                                      moment=_m70_bass)
    _assert_backing_clean70(_sg70_bass, _m70_bass["roles"])
    assert _sg70_bass["announcements"] == ["left out the backing bass — yours is playing"]
    assert not any("bass" in (s.get("backing") or {}) for s in _sg70_bass["suggestions"])

    # grow_my_loop: ears off is byte-identical to bare, backing left untouched
    _g70_off = _core.grow_my_loop(_seed69, key="D minor", use_ears=False)
    assert _g70_off == _core.grow_my_loop(_seed69, key="D minor")
    for _k70 in ("ears", "gate_report", "announcements", "ears_status", "no_fit"):
        assert _k70 not in _g70_off
    _harm70 = next(s for s in _g70_off["suggestions"] if s["dimension"].startswith("harmony"))
    assert set(_harm70["backing"]) == {"pad", "bass"}, "ears off must leave backing untouched"

    # grow_my_loop has no outer serve/gate on its suggestions (only _gate_backing
    # ever runs) — both bass+harmony occupied drops BOTH backing parts, deterministically.
    _g70_both = _core.grow_my_loop(_seed69, key="D minor", use_ears=True, moment=_m70)
    _assert_backing_clean70(_g70_both, _m70["roles"])
    assert set(_g70_both["announcements"]) == {
        "left out the backing bass — yours is playing",
        "left out the backing pad — yours is playing"}
    assert not any("backing" in s for s in _g70_both["suggestions"])

    # grow_my_loop, bass only: announcements name exactly the bass drop, pad survives
    _g70_bass = _core.grow_my_loop(_seed69, key="D minor", use_ears=True, moment=_m70_bass)
    assert _g70_bass["announcements"] == ["left out the backing bass — yours is playing"]
    assert not any("bass" in (s.get("backing") or {}) for s in _g70_bass["suggestions"])
    print("  suggest_growth + grow_my_loop: backing dropped only on real role/register collision")
    print("  OK\n")

    # 71. PP 2.0 p-token tolerance
    print("--- PP 2.0 p-token tolerance: probability tokens are a sticky no-op ---")
    assert _core.pp_notes_to_notes("v100 C3 1|1 1|3 p0.5 E3 2|1") == \
        _core.pp_notes_to_notes("v100 C3 1|1 1|3 E3 2|1")
    assert _core.pp_notes_to_notes("p1 C3 1|1") == _core.pp_notes_to_notes("C3 1|1")
    for _bad71 in ("C3 1|1 p2 2|1", "C3 1|1 p1.5 2|1",
                  "C3 1|1 p0.5-1.0 2|1", "C3 1|1 p-0.5 2|1"):
        try:
            _core.pp_notes_to_notes(_bad71)
            raise AssertionError(f"out-of-range/malformed p-token must raise: {_bad71!r}")
        except ValueError:
            pass
    print("  p<float> in [0,1] is a sticky no-op; out-of-range/ranges/negatives still raise")
    print("  OK\n")

    # 72. listener: ears audition switch + reply dual-send
    print("--- sombra listener: ears audition switch, answer wiring, reply port ---")
    import sombra_listener as _sl72  # noqa: E402
    import live_write as _lw72  # noqa: E402
    import socket as _sk72  # noqa: E402

    assert _sl72.STATE["ears_audition"] is False, "audition must start off for this probe"
    _r72a = _sl72.dispatch({"address": "/sombra/ears/audition", "args": [1]}, execute=True)
    assert _r72a["reply"] == "ears on for this session — answers listen to the room"
    assert _sl72.STATE["ears_audition"] is True
    _r72b = _sl72.dispatch({"address": "/sombra/ears/audition", "args": [0]}, execute=True)
    assert _r72b["reply"] == "ears off — back to defaults"
    assert _sl72.STATE["ears_audition"] is False
    _r72c = _sl72.dispatch(
        {"address": "/sombra/ears/audition", "args": ["toggle"]}, execute=True)
    assert _r72c["reply"] == "ears on for this session — answers listen to the room"
    assert _sl72.STATE["ears_audition"] is True
    _r72d = _sl72.dispatch(
        {"address": "/sombra/ears/audition", "args": ["toggle"]}, execute=True)
    assert _r72d["reply"] == "ears off — back to defaults"
    assert _sl72.STATE["ears_audition"] is False

    _st72a = _sl72.dispatch(
        {"address": "/sombra/status", "args": []}, execute=True, moment=_syn63)
    assert _st72a["reply"]["audition"] is False
    _sl72.STATE["ears_audition"] = True
    _st72b = _sl72.dispatch(
        {"address": "/sombra/status", "args": []}, execute=True, moment=_syn63)
    assert _st72b["reply"]["audition"] is True
    _sl72.STATE["ears_audition"] = False

    # the answer executor must ride the switch to core.answer_my_loop's use_ears kwarg
    _captured72: list = []
    _real_aml72 = _core.answer_my_loop
    _orig_sel72 = _sl72._selection_notes
    _seed72 = [{"pitch": 62, "start_beats": b * 1.0, "duration_beats": 0.9,
                "velocity": 80} for b in range(4)]

    def _capture_aml72(*a, **k):
        _captured72.append(k.get("use_ears"))
        return {"no_fit": True, "answer_notes": [], "assumptions": []}

    async def _raise_if_write72(*a, **k):
        raise AssertionError("no_fit must skip the jam-track write")

    _orig_write72 = _lw72.write_answer_to_jam_track
    try:
        _core.answer_my_loop = _capture_aml72
        _sl72._selection_notes = lambda: _seed72
        _lw72.write_answer_to_jam_track = _raise_if_write72
        _sl72.STATE["ears_audition"] = False
        _sl72._execute({"tool": "answer_my_loop", "tool_args": {"gear": 2}})
        assert _captured72[-1] is None, "audition off must leave use_ears at the stored default"
        _sl72.STATE["ears_audition"] = True
        _sl72._execute({"tool": "answer_my_loop", "tool_args": {"gear": 2}})
        assert _captured72[-1] is True, "audition on must force use_ears=True on the answer path"
    finally:
        _core.answer_my_loop = _real_aml72
        _sl72._selection_notes = _orig_sel72
        _lw72.write_answer_to_jam_track = _orig_write72
        _sl72.STATE["ears_audition"] = False

    # reply dual-send: the fan-out to the fixed reply port lives inline in
    # _listen_loop (never started by the selftest, per convention) — the strongest
    # assertion reachable without running that thread is the port itself, proven
    # bindable from a plain socket.
    _udp72 = _sk72.socket(_sk72.AF_INET, _sk72.SOCK_DGRAM)
    _udp72.setsockopt(_sk72.SOL_SOCKET, _sk72.SO_REUSEADDR, 1)
    try:
        _udp72.bind(("127.0.0.1", _sl72.REPLY_PORT))
    finally:
        _udp72.close()
    assert _sl72.REPLY_PORT == 3355
    print("  ears/audition toggles + status key; answer path wiring; reply port 3355 bindable")
    print("  OK\n")

    # 73. live_write: sceneCount fail-closed + focus fail-open
    # Transport lives behind pp_session.operation_session — inject there, not on
    # live_write streamablehttp_client / ClientSession (removed in Wave 2 split).
    print("--- live_write: sceneCount fail-closed, focus fail-open, stage never selects ---")
    import live_write as _lw73  # noqa: E402
    from contextlib import asynccontextmanager as _acm73

    _notes73 = [{"pitch": 62, "start": 0.0, "duration": 0.5, "velocity": 80},
                {"pitch": 65, "start": 0.5, "duration": 0.5, "velocity": 80}]
    _nd73 = _core._note_dicts(_notes73)
    _pp73, _translated73 = _core._notes_to_pp_string(_nd73)
    _JAM73 = 'tracks: [{trackIndex:0, name:"SIDEKICK JAM", id:"t1"}]'
    _OVERVIEW_NO_SC73 = _JAM73
    _OVERVIEW_OK73 = _JAM73 + ", sceneCount:4"

    _pps73 = _lw73.pp_session
    _orig_op73 = _pps73.operation_session
    # This block's fixtures speak the legacy compact-text protocol (pre-PP 2.0);
    # the new strict JSON adapter is exercised by its own selftest block.
    _orig_legacy73 = _pps73.LEGACY_TEXT_RESPONSE_MODE
    _pps73.LEGACY_TEXT_RESPONSE_MODE = True

    def _install_fakes73(calls, overview, *, fail_select=False):
        class _FakePP73:
            # New PPSession contract: expose handshake metadata for write summaries.
            handshake = {"pp_version": "2.1.0", "notation": "midi-json",
                         "format": "json"}

            async def call(self, name, args=None):
                _pp_session._selftest_schema_check(name, args)
                calls.append(name)
                if name == "ppal-live-api":
                    raise RuntimeError("legacy PP fixture has no verified identity capability")
                if name == "ppal-connect":
                    return ""
                if name == "ppal-read-live-set":
                    return overview
                if name == "ppal-read-track":
                    return "sessionClips: []"
                if name == "ppal-create-scene":
                    return "created:1"
                if name == "ppal-create-clip":
                    return "noteCount:%d" % len(_notes73)
                if name == "ppal-read-clip" and (args or {}).get("include") == []:
                    # the slot pre-read: the captured 2.2 empty-slot record
                    # (2.2.0-rc1/legacy/check_read_clip_existing_empty_slot.json)
                    return json.dumps({"id": None, "name": None,
                                       "path": args["path"], "type": None})
                if name == "ppal-read-clip":
                    return 'notes: "%s" noteCount:%d' % (_pp73, len(_nd73))
                if name == "ppal-select":
                    if fail_select:
                        raise RuntimeError("fake ppal-select failure")
                    return "ok"
                raise AssertionError("unexpected tool call in block 73: %s" % name)

        @_acm73
        async def _fake_op_session73(url=None):
            yield _FakePP73()

        _pps73.operation_session = _fake_op_session73

    try:
        # sceneCount missing must fail closed, before ANY scene/clip create call
        _calls73a: list = []
        _install_fakes73(_calls73a, _OVERVIEW_NO_SC73)
        try:
            asyncio.run(_lw73.write_answer_to_jam_track(_notes73, "probe", tempo=120.0))
            raise AssertionError("missing sceneCount must raise LiveWriteVerificationError")
        except _lw73.LiveWriteVerificationError as _exc73a:
            assert "sceneCount" in str(_exc73a)
        assert "ppal-create-scene" not in _calls73a and "ppal-create-clip" not in _calls73a, \
            "a fail-closed sceneCount read must not create scenes or clips"

        _calls73b: list = []
        _install_fakes73(_calls73b, _OVERVIEW_NO_SC73)
        try:
            asyncio.run(_lw73.stage_named_clips([{"name": "c1", "notes": _notes73}]))
            raise AssertionError("missing sceneCount must raise for stage_named_clips too")
        except _lw73.LiveWriteVerificationError as _exc73b:
            assert "sceneCount" in str(_exc73b)
        assert "ppal-create-scene" not in _calls73b and "ppal-create-clip" not in _calls73b, \
            "stage_named_clips must not create scenes or clips before sceneCount is known"

        # sceneCount present: happy path, ppal-select succeeds => focused True
        _calls73c: list = []
        _install_fakes73(_calls73c, _OVERVIEW_OK73, fail_select=False)
        _res73c = asyncio.run(_lw73.write_answer_to_jam_track(_notes73, "ok clip", tempo=120.0))
        assert _res73c["verified_ok"] is True and _res73c["focused"] is True

        # ppal-select fails => write still succeeds (already verified), focused False
        _calls73d: list = []
        _install_fakes73(_calls73d, _OVERVIEW_OK73, fail_select=True)
        _res73d = asyncio.run(
            _lw73.write_answer_to_jam_track(_notes73, "ok clip 2", tempo=120.0))
        assert _res73d["verified_ok"] is True and _res73d["focused"] is False

        # stage_named_clips never invokes ppal-select
        _calls73e: list = []
        _install_fakes73(_calls73e, _OVERVIEW_OK73)
        _res73e = asyncio.run(_lw73.stage_named_clips(
            [{"name": "c1", "notes": _notes73}], track_name="SIDEKICK JAM"))
        assert _res73e["verified_ok"] is True
        assert "ppal-select" not in _calls73e, "stage_named_clips must never invoke ppal-select"
    finally:
        _pps73.operation_session = _orig_op73
        _pps73.LEGACY_TEXT_RESPONSE_MODE = _orig_legacy73
    print("  sceneCount fail-closed (both writers); focus fail-open; stage never selects")
    print("  OK\n")

    # 74. corpus codec round-trip (PP 2.0 exposure)
    print("--- corpus codec round-trip (PP 2.0 exposure) ---")
    _phrase_index_path74 = os.path.join(ROOT, "store", "phrase_index.json")
    if not os.path.exists(_phrase_index_path74):
        print("  store/phrase_index.json absent — skipping (no corpus in this checkout)")
        print("  OK\n")
    else:
        with open(_phrase_index_path74) as _fh74:
            _phrases74 = json.load(_fh74)["phrases"]
        _n_checked74 = 0
        _n_dup74 = 0
        _n_overlap74 = 0
        for _i74 in range(0, len(_phrases74), 251):
            _ph74 = _phrases74[_i74]
            _nd74 = _core._note_dicts(_ph74["notes"])
            _norm74 = _core._normalize_notes_for_pp(_nd74)
            _s74, _ = _core._notes_to_pp_string(_nd74)
            _back74 = _core.pp_notes_to_notes(_s74)
            assert len(_norm74) == len(_back74), \
                f"phrase {_i74}: round-trip note count mismatch"
            _sorted_o74 = sorted(_norm74, key=lambda n: (n["start"], n["pitch"]))
            _sorted_b74 = sorted(_back74, key=lambda n: (n["start_beats"], n["pitch"]))
            for _o74, _b74 in zip(_sorted_o74, _sorted_b74):
                assert _o74["pitch"] == _b74["pitch"], f"phrase {_i74}: pitch drift"
                assert _o74["velocity"] == _b74["velocity"], f"phrase {_i74}: velocity drift"
                assert abs(_o74["start"] - _b74["start_beats"]) < 1e-3, \
                    f"phrase {_i74}: start drift"
                assert abs(_o74["duration"] - _b74["duration_beats"]) < 1e-3, \
                    f"phrase {_i74}: duration drift"
            _n_checked74 += 1
            _by_pitch74: dict = {}
            for _n74 in _nd74:
                _by_pitch74.setdefault(_n74["pitch"], []).append(_n74)
            _has_dup74 = False
            _has_overlap74 = False
            for _group74 in _by_pitch74.values():
                _group74 = sorted(_group74, key=lambda n: n["start"])
                for _j74 in range(len(_group74) - 1):
                    _n1_74, _n2_74 = _group74[_j74], _group74[_j74 + 1]
                    if abs(_n2_74["start"] - _n1_74["start"]) < 1e-9:
                        _has_dup74 = True
                    if _n2_74["start"] < _n1_74["start"] + _n1_74["duration"] - 1e-9:
                        _has_overlap74 = True
            _n_dup74 += _has_dup74
            _n_overlap74 += _has_overlap74
        print(f"  PP 2.0 dedup exposure: {_n_checked74} phrases sampled (every 251st); "
              f"{_n_dup74} with same-pitch+start duplicates, "
              f"{_n_overlap74} with same-pitch overlaps (informational only)")
        print("  OK\n")

    # 75. sombra patcher generator: deterministic + contract
    print("--- sombra patcher generator: deterministic + contract ---")
    sys.path.insert(0, os.path.join(ROOT, "tools", "m4l"))
    import generate_sombra_patcher as _gsp75  # noqa: E402

    # build twice -> byte-identical serialized output (no timestamps/random)
    _doc75 = _gsp75.build_smoke()
    _ser75 = _gsp75.serialize(_doc75)
    assert _ser75 == _gsp75.serialize(_gsp75.build_smoke()), \
        "smoke patcher build is not deterministic"

    # the freshly built doc must pass its own contract cleanly
    _findings75 = _gsp75.contract_report(_doc75)
    assert _findings75 == [], f"smoke patcher contract findings: {_findings75}"

    # json round-trips: loads(serialize(doc)) re-serializes identically
    _round75 = json.loads(_ser75)
    assert _gsp75.serialize(_round75) == _ser75, "smoke patcher json round-trip drift"

    # drift check: if the generated file is on disk its bytes must equal the
    # current serialization (regenerate with --smoke if this ever fails).
    _smoke_path75 = os.path.join(ROOT, "tools", "m4l", "SOMBRA_SMOKE.maxpat")
    if os.path.exists(_smoke_path75):
        with open(_smoke_path75, "rb") as _fh75:
            _disk75 = _fh75.read()
        assert _disk75 == _ser75.encode("utf-8"), (
            "tools/m4l/SOMBRA_SMOKE.maxpat is stale — regenerate: "
            ".venv/bin/python tools/m4l/generate_sombra_patcher.py --smoke")
        print("  on-disk SOMBRA_SMOKE.maxpat matches regenerated bytes")
    else:
        print("  SOMBRA_SMOKE.maxpat absent — skipping drift check")
    print(f"  deterministic build ({len(_ser75.encode())} bytes); contract clean; "
          f"{len(_doc75['patcher']['boxes'])} boxes, "
          f"{len(_doc75['patcher']['lines'])} patchlines")
    print("  OK\n")

    # 76. taste LOO quality gate: separable pairs pass, contradictory pairs zero weight
    print("--- taste LOO quality gate ---")
    import shutil as _sh76
    import tempfile as _tf76
    _dim76 = len(_core._TASTE_FEATURE_NAMES)
    _sep76 = ([1.0] + [0.0] * (_dim76 - 1), [0.0] * _dim76)
    _sep_pairs76 = [_sep76] * 8
    assert _core._taste_loo_accuracy(_sep_pairs76, [], _dim76) == 1.0, \
        "separable pairs must LOO perfectly"
    _flip76 = (_sep76[1], _sep76[0])
    _con_pairs76 = [_sep76, _flip76] * 4
    assert _core._taste_loo_accuracy(_con_pairs76, [], _dim76) <= 0.5, \
        "contradictory pairs must LOO at or below chance"
    # P2: LOO must audit the SHIPPED fit — pairs + gold augmentation UNDER the guard.
    # Poisoned gold (enough flipped pseudo-pairs to actually degrade the real pairs)
    # must be refused wholesale, inside every fold.
    _gold_bad76 = [(_sep76[1], _sep76[0])] * len(_sep_pairs76)
    _w_guarded76, _n_gold76 = _core._taste_fit(_sep_pairs76, _gold_bad76, _dim76)
    assert _n_gold76 == 0, "degrading gold augmentation must be refused wholesale"
    assert _w_guarded76 == _core.taste.train(_sep_pairs76, _dim76), \
        "the guarded fit must fall back to pairs-only weights"
    # The discriminating check: a naive "aligned" LOO that trained on held+gold
    # WITHOUT the guard would score 0.0 here. Only the guarded fold survives.
    assert _core.taste.accuracy(
        _sep_pairs76, _core.taste.train(_sep_pairs76 + _gold_bad76, _dim76)) == 0.0, \
        "unguarded augmentation really does invert these pairs (premise of the check)"
    assert _core._taste_loo_accuracy(_sep_pairs76, _gold_bad76, _dim76) == 1.0, \
        "LOO must run the GUARD inside each fold, not a bare train(held + gold)"
    _dir76a = _tf76.mkdtemp(prefix="loo76a_")
    _dir76b = _tf76.mkdtemp(prefix="loo76b_")
    _flow76 = [{"pitch": p, "start_beats": j * 0.5, "duration_beats": 0.5, "velocity": 80}
               for j, p in enumerate([62, 65, 67, 69, 67, 65, 64, 62])]
    _stat76 = [{"pitch": 62, "start_beats": j * 0.5, "duration_beats": 0.5, "velocity": 80}
               for j in range(8)]
    try:
        _core.set_ratings_path(os.path.join(_dir76a, "ratings.json"))
        for _r76a in range(6):
            _core.rate_candidates([_flow76, _stat76], best=0, worst=1,
                                    reason="static", key="D minor", role="lead",
                                    idempotency_key=f"selftest:76a:static:{_r76a}")
        _m76a = _core._taste_model()
        assert "loo_accuracy" in _m76a, "retrained model must store loo_accuracy"
        assert _m76a["loo_accuracy"] > 0.5, "separable rounds must pass LOO gate"
        _core.set_ratings_path(os.path.join(_dir76b, "ratings.json"))
        _same76 = [_stat76, _stat76]
        for _r76 in range(6):
            _core.rate_candidates(_same76, best=_r76 % 2, worst=1 - (_r76 % 2),
                                  reason="flip", key="D minor", role="lead",
                                  idempotency_key=f"selftest:76b:flip:{_r76}")
        _m76b = _core._taste_model()
        assert _m76b["n_pairs"] >= _core._TASTE_MIN_PAIRS
        assert _m76b.get("loo_accuracy", 1.0) <= 0.5, \
            "contradictory rounds must fail LOO gate"
        assert _core._taste_weight() == 0.0, \
            "contradictory LOO must zero taste weight despite enough pairs"
    finally:
        _sh76.rmtree(_dir76a, ignore_errors=True)
        _sh76.rmtree(_dir76b, ignore_errors=True)
        # Ambient path here is the real store (the ratings_tmp fixture dir
        # was removed with _ratings_tmpdir back before block 42): restore
        # None, not a path inside the deleted dir. This finally performs no
        # writes; later blocks set their own temp ratings paths before any
        # write, so nothing can land in the real store from this restore.
        _core.set_ratings_path(None)
    print("  separable LOO=1.0 + model key; contradictory LOO<=0.5 → weight 0")
    print("  OK\n")

    # 77. arrange_song tiles placement notes to section length
    print("--- arrange_song section-length tiling ---")
    _loop77 = [
        {"pitch": 60, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 80},
        {"pitch": 63, "start_beats": 1.0, "duration_beats": 0.5, "velocity": 75},
        {"pitch": 67, "start_beats": 2.0, "duration_beats": 1.0, "velocity": 82},
    ]
    _parts77 = [
        {"role": "lead", "notes": _loop77, "loop_bars": 4},
        {"role": "bass", "notes": [
            {"pitch": 36, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 85},
        ], "loop_bars": 4},
        {"role": "drums", "notes": [
            {"pitch": 36, "start_beats": 0.0, "duration_beats": 0.25, "velocity": 100},
        ], "loop_bars": 4},
    ]
    _input_counts77 = {p["role"]: len(p["notes"]) for p in _parts77}
    _loop_bars77 = {p["role"]: p["loop_bars"] for p in _parts77}
    _song77 = _core.arrange_song(_parts77, target_minutes=3.0, tempo=120, seed=0)
    for _pl77 in _song77["placements"]:
        _lb77 = _pl77["length_bars"]
        _loopb77 = _loop_bars77[_pl77["role"]]
        _notes77 = _pl77["notes"]
        assert _notes77, f"{_pl77['role']} placement has no notes"
        if _lb77 > _loopb77:
            _cycle77 = _loopb77 * 4
            assert any(n["start_beats"] >= _cycle77 - 1e-6 for n in _notes77), (
                f"{_pl77['role']} §{_pl77['section_index']}: expected a note in the "
                f"second {_loopb77}-bar cycle (start>={_cycle77}), "
                f"got starts={[n['start_beats'] for n in _notes77[:8]]}"
            )
            assert len(_notes77) > _input_counts77[_pl77["role"]], (
                f"{_pl77['role']} §{_pl77['section_index']}: expected tiled note count "
                f"> input {_input_counts77[_pl77['role']]}, got {len(_notes77)}"
            )
    # P1 regression: cycle uses loop_bars, not last-note span
    _short77 = [
        {"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 80},
        {"pitch": 62, "start": 3.0, "duration": 0.5, "velocity": 80},  # span ~3.5 beats
    ]
    _tiled77 = _core._tile_phrase_to_bars(_short77, bars=8, loop_bars=4)
    _c60 = [n["start"] for n in _tiled77 if n["pitch"] == 60]
    assert any(abs(s - 0.0) < 1e-6 for s in _c60)
    assert any(abs(s - 16.0) < 1e-6 for s in _c60), (
        f"4-bar loop must retile at beat 16, got starts={_c60}"
    )
    assert not any(3.0 < s < 5.0 for s in _c60), (
        f"must not cycle on phrase span (~3.5); got starts={_c60}"
    )
    # P1: half_time (×2 span) must not self-overlap when loop_bars < phrase bars
    _theme77 = [
        {"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 80},
        {"pitch": 62, "start": 4.0, "duration": 1.0, "velocity": 80},
        {"pitch": 64, "start": 8.0, "duration": 1.0, "velocity": 80},
        {"pitch": 65, "start": 12.0, "duration": 1.0, "velocity": 80},
    ]
    _half77 = _core._half_time_notes(_theme77)
    assert _core._phrase_span(_half77) > 16.0 + 1e-6, _core._phrase_span(_half77)
    _tiled_half77 = _core._tile_phrase_to_bars(_half77, bars=16, loop_bars=4)
    _starts_h77 = sorted(n["start"] for n in _tiled_half77)
    # No two notes of the same pitch within < half of phrase span of each other
    # from a too-short cycle (would put first note again at beat 16 while phrase
    # still spans ~32). First cycle must start at 0; second at >= phrase span.
    _p60_h = [n["start"] for n in _tiled_half77 if n["pitch"] == 60]
    assert abs(_p60_h[0] - 0.0) < 1e-6
    if len(_p60_h) > 1:
        assert _p60_h[1] >= _core._phrase_span(_half77) - 1e-6, (
            f"half_time tile must cycle on phrase span, not loop_bars=4; "
            f"got pitch-60 starts={_p60_h}"
        )
    # P2: an OMITTED loop_bars must land on a power-of-two bar grid, never on the
    # raw span. A part whose notes end at beat 10 is a 4-bar loop that breathes,
    # not a 3-bar loop — a 3-bar cycle walks the motif onto a new downbeat every
    # repeat against 4/8/16-bar sections.
    assert _core._default_loop_bars(0.0) == 4          # empty/degenerate → 4
    assert _core._default_loop_bars(4.0) == 1          # exactly 1 bar
    assert _core._default_loop_bars(10.0) == 4, "beat-10 span is a 4-bar loop, not 3"
    assert _core._default_loop_bars(16.0) == 4
    assert _core._default_loop_bars(17.0) == 8
    assert _core._default_loop_bars(64.0) == 16
    _breathing77 = [
        {"pitch": 36, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 90},
        {"pitch": 36, "start_beats": 4.0, "duration_beats": 1.0, "velocity": 90},
        {"pitch": 38, "start_beats": 8.0, "duration_beats": 2.0, "velocity": 90},
    ]
    _song_dflt77 = _core.arrange_song(
        [{"role": "bass", "notes": _breathing77}], target_minutes=2.0, tempo=120, seed=0)
    for _pl_d77 in _song_dflt77["placements"]:
        assert _pl_d77["loop_bars"] == 4, (
            f"omitted loop_bars must round up to the bar grid, got {_pl_d77['loop_bars']}"
        )
        # The invariant is the CYCLE, not the spacing inside one pass (a variation
        # may move notes around within the loop). Every start must be congruent
        # mod 16 beats to a first-cycle start — i.e. repeats land on the 4-bar grid.
        _cyc77 = _pl_d77["loop_bars"] * 4
        _s77 = sorted(n["start_beats"] for n in _pl_d77["notes"])
        _first77 = {round(s, 6) for s in _s77 if s < _cyc77 - 1e-9}
        for _st77 in _s77:
            assert round(_st77 % _cyc77, 6) in _first77, (
                f"repeat off the {_pl_d77['loop_bars']}-bar grid: start={_st77}, "
                f"first cycle={sorted(_first77)}"
            )
    print(f"  {len(_song77['placements'])} placements tile on loop_bars cycles")
    print("  half_time span-aware cycle OK; omitted loop_bars snaps to the bar grid")
    print("  OK\n")

    # 78. real ratings training probe (read-only)
    print("--- real ratings training probe (read-only) ---")
    if not os.path.exists(_core.RATINGS_JSON):
        print("  store/ratings.json absent — skipping")
        print("  OK\n")
    else:
        with open(_core.RATINGS_JSON) as _fh78:
            _data78 = json.load(_fh78)
        _rounds78 = _data78.get("candidate_rounds", [])
        if not _rounds78:
            print("  no candidate_rounds in store — skipping")
            print("  OK\n")
        else:
            import taste as _taste78
            _pairs78 = _core._taste_pairs_from_rounds(_rounds78)
            if not _pairs78:
                print("  no trainable pairs in store rounds — skipping")
                print("  OK\n")
            else:
                _dim78 = len(_core._TASTE_FEATURE_NAMES)
                _w78 = _taste78.train(_pairs78, _dim78)
                assert len(_w78) == _dim78
                assert _taste78.accuracy(_pairs78, _w78) >= 0.5
                _gold78 = _core._taste_pairs_from_golds(
                    max(len(_pairs78), _core._TASTE_MIN_PAIRS))
                _loo78 = _core._taste_loo_accuracy(_pairs78, _gold78, _dim78)
                assert 0.0 <= _loo78 <= 1.0
                _, _ngold78 = _core._taste_fit(_pairs78, _gold78, _dim78)
                print(f"  pairs={len(_pairs78)}, train_acc={_taste78.accuracy(_pairs78, _w78):.3f}, "
                      f"loo={_loo78:.3f} (gold offered={len(_gold78)}, used={_ngold78})")
                print("  OK\n")

    # 79. PP note normalization (dedup + same-pitch overlap truncate)
    print("--- PP note normalization (dedup + overlap truncate) ---")
    _probe3_79 = [
        {"pitch": 48, "start": 0.0, "duration": 1.0, "velocity": 80},
        {"pitch": 48, "start": 0.0, "duration": 1.0, "velocity": 80},
        {"pitch": 50, "start": 2.0, "duration": 2.0, "velocity": 90},
        {"pitch": 50, "start": 3.0, "duration": 1.0, "velocity": 90},
        {"pitch": 52, "start": 6.0, "duration": 0.5638, "velocity": 100},
        {"pitch": 53, "start": 8.0, "duration": 1.0, "velocity": 1},
    ]
    _pp79, _ = _core._notes_to_pp_string(_probe3_79)
    _back79 = _core.pp_notes_to_notes(_pp79)
    assert len(_back79) == 5, \
        f"probe-3 fixture: expected 5 notes after normalize, got {len(_back79)}"
    _overlap79 = [
        {"pitch": 50, "start": 2.0, "duration": 2.0, "velocity": 90},
        {"pitch": 50, "start": 3.0, "duration": 1.0, "velocity": 90},
    ]
    _norm79 = _core._normalize_notes_for_pp(_overlap79)
    assert len(_norm79) == 2
    assert abs(_norm79[0]["duration"] - 1.0) < 1e-6, \
        "same-pitch overlap: first note duration must truncate to second start"
    _diff79 = [
        {"pitch": 60, "start": 0.0, "duration": 2.0, "velocity": 80},
        {"pitch": 62, "start": 1.0, "duration": 1.0, "velocity": 80},
    ]
    assert len(_core._normalize_notes_for_pp(_diff79)) == 2, \
        "different-pitch overlaps must not drop notes"
    # P1: interleaved same-pitch overlap (C@0 len4, D@1, C@2)
    _inter79 = [
        {"pitch": 60, "start": 0.0, "duration": 4.0, "velocity": 80},
        {"pitch": 62, "start": 1.0, "duration": 1.0, "velocity": 80},
        {"pitch": 60, "start": 2.0, "duration": 1.0, "velocity": 80},
    ]
    _norm_i79 = _core._normalize_notes_for_pp(_inter79)
    assert len(_norm_i79) == 3
    _c0 = next(n for n in _norm_i79 if n["pitch"] == 60 and abs(n["start"] - 0.0) < 1e-9)
    assert abs(_c0["duration"] - 2.0) < 1e-6, (
        f"interleaved same-pitch: first C must truncate to 2.0, got {_c0['duration']}"
    )
    _immutable79 = [dict(n) for n in _probe3_79[:2]]
    _core._normalize_notes_for_pp(_immutable79)
    assert _immutable79 == _probe3_79[:2], "_normalize_notes_for_pp must not mutate input"
    _wire79, _ = _core._notes_to_pp_string(_core._note_dicts(_probe3_79))
    assert _lw41._require_verified_notes(
        _probe3_79, 'noteCount:5, notes:"%s"' % _wire79, "selftest-normalize-dup",
    ) == 5
    print("  probe-3 dup 6→5; overlap truncates; interleaved C/D/C ok; verify accepts normalized wire")
    print("  OK\n")

    # --- audio_ref helpers (no network / no ML / no agy in default selftest) ---
    print("--- audio_ref detect_source + parse_time + focus/moments/index ---")
    _tools80 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
    if _tools80 not in sys.path:
        sys.path.insert(0, _tools80)
    import audio_ref as _ar80
    from pathlib import Path
    assert _ar80.is_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert _ar80.is_youtube_url("https://youtu.be/dQw4w9WgXcQ")
    assert not _ar80.is_youtube_url("https://vimeo.com/123")
    assert _ar80.parse_time("90") == 90.0
    assert _ar80.parse_time("1:30") == 90.0
    assert _ar80.parse_time("0:48") == 48.0
    assert abs(_ar80.parse_time("1:02.5") - 62.5) < 1e-9
    assert _ar80.format_mmss(180) == "03:00"
    assert _ar80.normalize_focus("melody") == "melody"
    try:
        _ar80.normalize_focus("drums")
        raise AssertionError("bad focus must raise")
    except ValueError:
        pass
    _kind80, _src80 = _ar80.detect_source("https://youtu.be/abcdefghijk")
    assert _kind80 == "youtube" and "youtu.be" in _src80
    try:
        _ar80.detect_source("https://example.com/x.mp3")
        raise AssertionError("non-YouTube URL must raise")
    except ValueError:
        pass
    try:
        _ar80.analyze_audio_ref("/tmp/nope.wav", modes=[])
        raise AssertionError("empty modes must raise before out_dir")
    except ValueError as _e80:
        assert "non-empty" in str(_e80).lower()
    _brief80 = (
        "# Identity\nTest feel.\n\n# Groove\nx\n\n# Harmony / melody\nn/a\n\n"
        "# Texture / production\nx\n\n# Imitation rules\n"
        "- Do: keep sparse kicks\n- Don't: add swing\n\n# Moments\n"
        "- 00:30 – drop\n- 05:00 – past clip\n"
    )
    _mv80 = _ar80.validate_moments(_brief80, 180.0)
    assert _mv80["stamp_count"] == 2
    assert 300.0 in _mv80["out_of_range"] or 300 in _mv80["out_of_range"]
    assert _mv80["ok"] is True  # not all out of range
    _mv_all = _ar80.validate_moments(
        "# Identity\nX\n\n# Moments\n- 09:50 – nope\n- 10:00 – nope\n", 180.0
    )
    assert _mv_all["all_out_of_range"] is True
    with tempfile.TemporaryDirectory() as _td80:
        _idx80 = Path(_td80) / "audio_ref_index.json"
        _dir80 = Path(_td80) / "yt-selftest_1"
        _dir80.mkdir()
        _ar80_out_prev = _ar80.OUT_ROOT
        _ar80.OUT_ROOT = Path(_td80)  # containment checks relative to this for selftest
        try:
            (_dir80 / "source.wav").write_bytes(b"RIFF")
            (_dir80 / "describe.txt").write_text(_brief80, encoding="utf-8")
            (_dir80 / "meta.json").write_text(
                json.dumps(
                    {
                        "source": "https://youtu.be/selftest",
                        "source_kind": "youtube",
                        "focus": "melody",
                        "start": 0,
                        "end": 180,
                        "tags": ["selftest"],
                        "youtube": {"title": "Selftest Track"},
                        "features": {"duration_s": 180},
                    }
                ),
                encoding="utf-8",
            )
            _card80 = _ar80.card_from_out_dir(_dir80)
            _ar80.upsert_index_card(_card80, path=_idx80)
            _hits80 = _ar80.find_audio_refs(query="sparse", focus="melody", tag="selftest", k=3, index_path=_idx80, detail=True)
            assert len(_hits80) == 1 and _hits80[0]["ref_id"] == "yt-selftest_1"
            _ar80.tag_audio_ref("yt-selftest_1", tags=["for:domma"], project="domma", index_path=_idx80)
            _hits81 = _ar80.find_audio_refs(project="domma", k=3, index_path=_idx80, detail=True)
            assert len(_hits81) == 1 and "for:domma" in _hits81[0]["tags"]
            # structure parse from describe (no network)
            _struct80 = _ar80.parse_describe_structure(
                "# Identity\nG Phrygian sludge.\n\n# Groove\n"
                "- Density: low drum density; 0% swing (straight grid); ~60 BPM feel\n"
                "- Kick/snare: kick on 1, snare on 3\n\n"
                "# Harmony / melody\n- low-register sub-bass\n"
            )
            assert _struct80.get("feel_bpm") == 60.0
            assert _struct80.get("swing_pct") == 0.0
            assert _struct80.get("density") == "low"
            assert _struct80.get("modal_center")
            assert "phrygian" in _struct80["modal_center"].lower()
            assert _struct80.get("register") == "low"
            assert _struct80.get("drum_grid")
            # ricardo overlay note + verdict
            _ar80.tag_audio_ref(
                "yt-selftest_1",
                note="steal half-time snare + static drone",
                verdict="keep",
                index_path=_idx80,
            )
            _hits82 = _ar80.find_audio_refs(
                query="half-time", verdict="keep", k=3, index_path=_idx80,
                detail=True,
            )
            assert len(_hits82) == 1
            assert _hits82[0].get("verdict") == "keep"
            assert "drone" in (_hits82[0].get("ricardo_note") or "")
            # card structure from describe
            (_dir80 / "describe.txt").write_text(
                "# Identity\nG Phrygian sludge.\n\n# Groove\n"
                "- Density: low density; 0% swing; ~60 BPM feel\n"
                "- Kick on 1, snare on 3\n\n# Harmony / melody\n"
                "- low-register drone\n\n# Imitation rules\n"
                "- Do: keep sparse kicks\n- Don't: add swing\n\n"
                "# Moments\n- 00:02 – hit\n- 00:08 – swell\n",
                encoding="utf-8",
            )
            _card_s = _ar80.card_from_out_dir(_dir80)
            assert (_card_s.get("structure") or {}).get("feel_bpm") == 60.0
            # moment clip without Basic Pitch (features only; needs real wav + ffmpeg)
            _wav_ok = False
            try:
                import wave as _wave80
                import struct as _struct80
                _sr80 = 22050
                with _wave80.open(str(_dir80 / "source.wav"), "w") as _wf80:
                    _wf80.setnchannels(1)
                    _wf80.setsampwidth(2)
                    _wf80.setframerate(_sr80)
                    _wf80.writeframes(_struct80.pack("<" + "h" * _sr80 * 12, *([0] * (_sr80 * 12))))
                _wav_ok = True
            except Exception:
                pass
            if _wav_ok and _ar80._which("ffmpeg"):
                _ar80.upsert_index_card(_ar80.card_from_out_dir(_dir80), path=_idx80)
                _clip80 = _ar80.clip_moments(
                    "yt-selftest_1",
                    window_s=3.0,
                    pad_s=0.5,
                    modes=["features"],
                    index_path=_idx80,
                    max_moments=2,
                )
                assert _clip80["ok"] and _clip80["child_count"] >= 1
                _child80 = Path(_clip80["children"][0]["out_dir"])
                assert (_child80 / "source.wav").is_file()
                assert (_child80 / "describe.txt").is_file()
                assert "moment" in (
                    _ar80.load_index(_idx80)["refs"][_clip80["children"][0]["ref_id"]].get("tags")
                    or []
                )
            _man80 = {"batch_id": "selftest-batch", "defaults": {"focus": "full"}, "items": []}
            try:
                _ar80.run_batch(_man80)
                raise AssertionError("empty batch must raise")
            except ValueError:
                pass
            _plain80 = Path(_td80) / "urls.txt"
            _plain80.write_text("# comment\nhttps://youtu.be/aaaaaaaaaaa\n", encoding="utf-8")
            _loaded80 = _ar80.load_batch_manifest(_plain80)
            assert len(_loaded80["items"]) == 1
            # apply package (no Live)
            try:
                import mido as _mido80
                _mf80 = _mido80.MidiFile()
                _tr80 = _mido80.MidiTrack()
                _mf80.tracks.append(_tr80)
                _tr80.append(_mido80.MetaMessage("set_tempo", tempo=_mido80.bpm2tempo(100)))
                _tr80.append(_mido80.Message("note_on", note=60, velocity=80, time=0))
                _tr80.append(_mido80.Message("note_off", note=60, velocity=0, time=_mf80.ticks_per_beat))
                _mf80.save(str(_dir80 / "ref.mid"))
                _ar80.upsert_index_card(_ar80.card_from_out_dir(_dir80), path=_idx80)
                _app80 = _ar80.apply_audio_ref("yt-selftest_1", role="lead", bars=2, index_path=_idx80)
                assert _app80["ok"] and _app80["note_count"] >= 1
                assert "KEEP" in _app80["keep_drop"]
            except ImportError:
                pass
            # path containment: from_dir / ref_id must stay under store/audio_refs
            try:
                _ar80.resolve_from_dir("/tmp")
                raise AssertionError("from_dir outside OUT_ROOT must raise")
            except (ValueError, FileNotFoundError):
                pass
            try:
                _ar80.normalize_ref_id("../etc")
                raise AssertionError("traversal ref_id must raise")
            except ValueError:
                pass
            # corrupt index must fail loud (not wipe)
            _bad_idx = Path(_td80) / "bad_index.json"
            _bad_idx.write_text("{not-json", encoding="utf-8")
            try:
                _ar80.load_index(_bad_idx)
                raise AssertionError("corrupt index must raise")
            except RuntimeError as _idx_err:
                assert "corrupt" in str(_idx_err).lower()
            # composite batch resume keys distinguish same URL / different windows
            _k1 = _ar80.batch_item_key(
                {"source": "https://youtu.be/x", "start": "0:00", "end": "1:00", "focus": "groove"}
            )
            _k2 = _ar80.batch_item_key(
                {"source": "https://youtu.be/x", "start": "5:00", "end": "8:00", "focus": "melody"}
            )
            assert _k1 != _k2
            assert "focus=groove" in _k1 and "focus=melody" in _k2
            # P2: resume key includes prompt_extra / model / tempo
            _k_prompt_a = _ar80.batch_item_key(
                {"source": "https://youtu.be/x", "focus": "full", "prompt_extra": "darker"}
            )
            _k_prompt_b = _ar80.batch_item_key(
                {"source": "https://youtu.be/x", "focus": "full", "prompt_extra": "brighter"}
            )
            assert _k_prompt_a != _k_prompt_b
            assert "prompt=darker" in _k_prompt_a
            _k_tempo_a = _ar80.batch_item_key(
                {"source": "https://youtu.be/x"}, {"midi_tempo": 120}
            )
            _k_tempo_b = _ar80.batch_item_key(
                {"source": "https://youtu.be/x"}, {"midi_tempo": 100}
            )
            assert _k_tempo_a != _k_tempo_b
            # P2: failed midi mode → child ok false / partial true (features may still pass)
            if _wav_ok and _ar80._which("ffmpeg"):
                _orig_bp = _ar80.run_basic_pitch
                def _force_midi_fail(*_a, **_k):
                    raise RuntimeError("forced midi fail")
                _ar80.run_basic_pitch = _force_midi_fail  # type: ignore[method-assign]
                try:
                    _clip_fail = _ar80.clip_moments(
                        "yt-selftest_1",
                        window_s=3.0,
                        pad_s=0.5,
                        modes=["midi", "features"],
                        index_path=_idx80,
                        max_moments=1,
                    )
                    assert _clip_fail.get("child_count", 0) >= 1
                    _chf = _clip_fail["children"][0]
                    assert _chf.get("ok") is False
                    assert _chf.get("partial") is True
                    assert (_chf.get("mode_ok") or {}).get("midi") is False
                    assert (_chf.get("mode_ok") or {}).get("features") is True
                    assert _clip_fail.get("ok") is False
                    assert _clip_fail.get("partial") is True
                finally:
                    _ar80.run_basic_pitch = _orig_bp  # type: ignore[method-assign]
            # P2: out-of-range moment + one good child ⇒ partial, not ok=true
            if _wav_ok and _ar80._which("ffmpeg"):
                (_dir80 / "describe.txt").write_text(
                    "# Identity\nclip status probe\n\n# Moments\n"
                    "- 00:02 – hit\n- 02:00 – beyond\n",
                    encoding="utf-8",
                )
                (_dir80 / "features.json").write_text(
                    json.dumps({"duration_s": 12.0}) + "\n", encoding="utf-8"
                )
                _meta80 = json.loads((_dir80 / "meta.json").read_text(encoding="utf-8"))
                _meta80["duration_s"] = 12.0
                _meta80["features"] = {"duration_s": 12.0}
                (_dir80 / "meta.json").write_text(
                    json.dumps(_meta80) + "\n", encoding="utf-8"
                )
                _ar80.upsert_index_card(_ar80.card_from_out_dir(_dir80), path=_idx80)
                _clip_partial = _ar80.clip_moments(
                    "yt-selftest_1",
                    window_s=3.0,
                    pad_s=0.5,
                    modes=["features"],
                    index_path=_idx80,
                    max_moments=2,
                )
                assert _clip_partial.get("child_count") == 1, _clip_partial
                assert any("beyond duration" in (e or "") for e in (_clip_partial.get("errors") or []))
                assert _clip_partial.get("ok") is False
                assert _clip_partial.get("partial") is True
            # P2: all-partial batch rows ⇒ batch.partial true (not total-failure shape)
            _batch_status = _ar80.summarize_batch_results(
                [
                    {"ok": False, "partial": True, "error": "midi failed"},
                    {"ok": False, "partial": False, "error": "total fail"},
                ]
            )
            assert _batch_status["ok"] is False
            assert _batch_status["partial"] is True
            assert _batch_status["ok_count"] == 0
            # X2 intentionally added listen_tokens to the summary shape, and the
            # agy-listener lane added listen_rejected_count; the assertion tracks
            # both (all four original fields unchanged).
            assert _ar80.summarize_batch_results(
                [{"ok": False, "partial": True}]
            ) == {"ok": False, "partial": True, "ok_count": 0,
                  "fail_count": 1, "listen_rejected_count": 0,
                  "listen_tokens": {"items_with_usage": 0}}
            # (a) make_out_dir collision: same stem → distinct dirs under tempfile root
            _d_a = _ar80.make_out_dir("collision-stem", root=Path(_td80))
            _d_b = _ar80.make_out_dir("collision-stem", root=Path(_td80))
            assert _d_a != _d_b and _d_a.is_dir() and _d_b.is_dir()
            # (b) _pick_raw_download: requested present; single yt_raw.*; two → RuntimeError
            _pick_dir = Path(_td80) / "pick_raw"
            _pick_dir.mkdir()
            _req = _pick_dir / "requested.m4a"
            _req.write_bytes(b"req")
            (_pick_dir / "yt_raw.webm").write_bytes(b"other")
            assert _ar80._pick_raw_download(_pick_dir, str(_req)) == _req
            _req.unlink()
            assert _ar80._pick_raw_download(_pick_dir, str(_req)) == _pick_dir / "yt_raw.webm"
            (_pick_dir / "yt_raw.m4a").write_bytes(b"second")
            try:
                _ar80._pick_raw_download(_pick_dir, str(_req))
                raise AssertionError("two yt_raw.* must raise RuntimeError")
            except RuntimeError as _pick_err:
                assert "candidates" in str(_pick_err).lower() or "yt_raw" in str(_pick_err)

            # P2: resume must RETRY a partial row in place, not re-download it into
            # a second folder (duplicate ref + a wasted agy listen).
            _bdir80 = Path(_td80) / "batches"
            _bdir80.mkdir(exist_ok=True)
            _br_prev80 = _ar80.BATCH_ROOT
            _ar80.BATCH_ROOT = _bdir80
            _an_prev80 = _ar80.analyze_audio_ref
            try:
                _part_dir80 = Path(_td80) / "yt-partial_1"
                _part_dir80.mkdir()
                (_part_dir80 / "source.wav").write_bytes(b"RIFF")
                _item80 = {"source": "https://youtu.be/partialaaaa", "focus": "full"}
                _pkey80 = _ar80.batch_item_key(_item80, {})
                (_bdir80 / "resume-probe.json").write_text(json.dumps({
                    "batch_id": "resume-probe",
                    "results": [{
                        "ok": False, "partial": True, "resume_key": _pkey80,
                        "source": _item80["source"], "out_dir": str(_part_dir80),
                    }],
                }), encoding="utf-8")
                _calls80 = []

                def _spy_analyze80(*_a, **kw):
                    _calls80.append(kw)
                    return {"ok": True, "partial": False, "ref_id": "yt-partial_1",
                            "out_dir": str(_part_dir80), "focus": "full", "warnings": []}

                _ar80.analyze_audio_ref = _spy_analyze80  # type: ignore[assignment]
                _rep80 = _ar80.run_batch(
                    {"batch_id": "resume-probe", "defaults": {}, "items": [_item80]},
                    resume=True,
                )
                assert len(_calls80) == 1, _calls80
                assert _calls80[0].get("from_dir") == str(_part_dir80), (
                    "a partial row with audio on disk must be retried via from_dir, "
                    f"not re-fetched; got from_dir={_calls80[0].get('from_dir')!r}"
                )
                assert _rep80["results"][0].get("retried_partial") is True
                assert _rep80.get("complete") is True

                # a partial row whose audio is GONE must fall back to a full re-fetch
                _calls80.clear()
                (_part_dir80 / "source.wav").unlink()
                (_bdir80 / "resume-probe.json").write_text(json.dumps({
                    "batch_id": "resume-probe",
                    "results": [{
                        "ok": False, "partial": True, "resume_key": _pkey80,
                        "source": _item80["source"], "out_dir": str(_part_dir80),
                    }],
                }), encoding="utf-8")
                _ar80.run_batch(
                    {"batch_id": "resume-probe", "defaults": {}, "items": [_item80]},
                    resume=True,
                )
                assert len(_calls80) == 1 and _calls80[0].get("from_dir") is None, (
                    "a partial row without source.wav cannot be resumed in place"
                )
            finally:
                _ar80.analyze_audio_ref = _an_prev80  # type: ignore[assignment]
                _ar80.BATCH_ROOT = _br_prev80

            # batch reports are written atomically (a truncated report silently
            # disables --resume, which is how a crash becomes duplicate refs)
            _atomic80 = Path(_td80) / "atomic.json"
            _ar80.write_json_atomic(_atomic80, {"a": 1})
            assert json.loads(_atomic80.read_text(encoding="utf-8"))["a"] == 1
            assert not list(Path(_td80).glob(".atomic.json.*.tmp")), "temp file leaked"

            # overlay backup captures the judgment layer, never the audio
            _bk80 = _ar80.backup_overlays(
                dest_dir=Path(_td80) / "backups", root=Path(_td80), index_path=_idx80,
            )
            assert _bk80["ok"] and _bk80["file_count"] >= 2
            import tarfile as _tar80
            with _tar80.open(_bk80["archive"]) as _tf80:
                _names80 = _tf80.getnames()
            assert any(n.endswith("describe.txt") for n in _names80), _names80
            assert not any(n.endswith(".wav") for n in _names80), "audio must not be backed up"
        finally:
            _ar80.OUT_ROOT = _ar80_out_prev
    # agy hardening (M3): untrusted audio ⇒ sandbox unconditional for card
    # listens (the env override is gone on purpose), auto-approve flag off
    # by default behind one constant the orchestrator can flip after a real
    # no-tool print-mode call verifies it is unneeded.
    _cmd80 = _ar80._agy_cmd_for("gemini-3.8-flash-medium", "/tmp", "hi")
    assert "--sandbox" in _cmd80
    assert "--dangerously-skip-permissions" not in _cmd80
    _sbx_prev80 = os.environ.get("AUDIO_REF_AGY_SANDBOX")
    os.environ["AUDIO_REF_AGY_SANDBOX"] = "0"
    try:
        assert "--sandbox" in _ar80._agy_cmd_for(
            "gemini-3.8-flash-medium", "/tmp", "hi"), \
            "card listens ignore the sandbox env override"
    finally:
        if _sbx_prev80 is None:
            os.environ.pop("AUDIO_REF_AGY_SANDBOX", None)
        else:
            os.environ["AUDIO_REF_AGY_SANDBOX"] = _sbx_prev80
    print("  youtube/focus/moments/index/batch-parse OK")
    print("  batch resume retries partials in place; atomic reports; overlay backup; agy sandbox")
    print("  OK\n")

    # 81. ref-steered composing: translator purity, honest gating, drum grid
    print("--- ref steering: mods, honesty, drum grid, ledger ---")
    import ears as _ears81
    import ledger as _ledger81

    def _card81(**structure):
        return {"ref_id": "r-test", "structure": dict(structure) or None}

    # 1/2/3: translator is pure, frozen, and names what it did NOT gate
    _c81 = _card81(density="low", grid_bpm=104.0, feel_bpm=52.0,
                   register="low", modal_center="E Aeolian")
    assert _ar80.ref_to_mods(_c81) == _ar80.ref_to_mods(_c81), "ref_to_mods must be pure"
    _mods81, _unmapped81 = _ar80.ref_to_mods(_c81)
    assert _mods81 == {"density_max": "sparse"}, _mods81
    assert _ar80.ref_to_mods(_card81(density="high"))[0] == {"density_min": "mid"}
    assert _ar80.ref_to_mods(_card81(density="medium"))[0] == {"density_max": "mid"}
    assert _ar80.ref_to_mods(_card81())[0] == {}, "empty structure must gate nothing"
    # tempo / register / mode are deliberately NOT gates — each must say so
    assert any("tempo" in u for u in _unmapped81), _unmapped81
    assert any("register" in u for u in _unmapped81), _unmapped81
    assert any("modal" in u for u in _unmapped81), _unmapped81
    _steer81 = _ar80.ref_steering(_c81)
    assert _steer81["tempo"] == 104.0, "grid tempo drives retrieval, not the 52 feel"
    assert _steer81["feel_tempo"] == 52.0 and _steer81["mode_hint"] == "E Aeolian"

    # 7: ad-hoc mods must not open a hole in the unknown-direction contract
    for _bad81 in ("moist", {"nope": 1}):
        try:
            _ears81.serve([], {"ears": "blind"}, direction=_bad81)
            raise AssertionError("unknown direction/modifier must raise: %r" % (_bad81,))
        except ValueError:
            pass
    # a reference must never veto the room's tempo world
    try:
        _ears81._resolve_mods({"tempo_world": "club"})
        raise AssertionError("ad-hoc mods must not be able to set tempo_world")
    except ValueError as _e81:
        assert "tempo_world" in str(_e81)

    # 5/6: the reference state gates WITHOUT claiming Live was heard
    _refm81 = {"ears": "reference", "tempo": 124.0, "tempo_world": None,
               "key": None, "roles": {}}
    _cands81 = [
        {"id": "sparse", "role": "lead",
         "notes": [{"pitch": 60, "start": i * 2.0, "duration": 0.5, "velocity": 80}
                   for i in range(4)]},
        {"id": "busy", "role": "lead",
         "notes": [{"pitch": 60, "start": i * 0.25, "duration": 0.2, "velocity": 80}
                   for i in range(32)]},
    ]
    _srv81 = _ears81.serve(_cands81, _refm81, role="lead",
                           direction={"density_max": "sparse"})
    assert [c["id"] for c in _srv81["served"]] == ["sparse"], _srv81["served"]
    assert _srv81["ears"] == "reference", "a reference must never report ears=live"
    _line81 = _ears81.status_line(_refm81, _srv81["gate_report"], _srv81["announcements"])
    assert "empty room" not in _line81, _line81   # the chassis lie this pins shut
    assert "reference" in _line81, _line81

    # 8: with a ref the gate pool really narrows (mutation: ref_to_mods -> {})
    _room81 = _moment42.build_moment(
        [{"name": "Bass", "notes": [
            {"pitch": 38, "start": i * 1.0, "duration": 0.9, "velocity": 90}
            for i in range(4)]}], tempo=120.0)
    _seed81 = [{"pitch": 62, "start_beats": b * 1.0, "duration_beats": 0.9,
                "velocity": 80} for b in range(4)]
    _plain81 = _core.answer_my_loop(_seed81, key="D minor", seed=0, use_ears=False)
    _ref_card81 = {"ref_id": "r-live", "structure": {
        "density": "low", "grid_bpm": 104.0, "register": "low"}}
    _refd81 = _core.answer_my_loop(_seed81, key="D minor", seed=0, use_ears=False,
                                   ref=_ar80.ref_steering(_ref_card81))
    assert _refd81["ref"]["ref_id"] == "r-live"
    assert _refd81["source_phrase_ids"] != _plain81["source_phrase_ids"], \
        "a reference with tempo+density must change which phrase of his is picked"
    assert any("register nudged" in a for a in _refd81["assumptions"]), \
        _refd81["assumptions"]
    # Register bias must reach the NOTES, not just the prose. Compare against the
    # same reference minus its register field, so the pick is identical and the
    # only difference is the bias (comparing to the no-ref answer would compare
    # two different source phrases and prove nothing).
    def _med81(ans):
        p = sorted(n["pitch"] for n in ans["answer_notes"])
        return p[len(p) // 2]
    _noreg81 = _core.answer_my_loop(
        _seed81, key="D minor", seed=0, use_ears=False,
        ref=_ar80.ref_steering({"ref_id": "r-live", "structure": {
            "density": "low", "grid_bpm": 104.0}}))
    assert _noreg81["source_phrase_ids"] == _refd81["source_phrase_ids"], \
        "register must not change the pick — it is a realization bias, not a gate"
    assert _med81(_refd81) == _med81(_noreg81) - 12, \
        "a low-register reference must drop the answer an octave"
    assert "ref_register:-12" in _refd81["transforms_applied"], _refd81["transforms_applied"]
    # ref-off must stay byte-identical (the ears-off contract, extended)
    assert _core.answer_my_loop(_seed81, key="D minor", seed=0, use_ears=False) == _plain81, \
        "an answer with no reference must be unchanged by any of this"
    # a structure-less ref steers nothing and SAYS so (never a silent no-op)
    _bare81 = _core.answer_my_loop(_seed81, key="D minor", seed=0, use_ears=False,
                                   ref=_ar80.ref_steering({"ref_id": "r-bare"}))
    assert any("prose only" in a for a in _bare81["assumptions"]), _bare81["assumptions"]

    # 11: two different drum descriptions -> different grids AND different phrases
    _g_half81, _u_half81 = _ar80.drum_grid_to_notes(
        "Kick/snare: kick on beat 1, snare on beat 3 (half-time).")
    _g_four81, _u_four81 = _ar80.drum_grid_to_notes(
        "Four-on-the-floor kick; offbeat open hats; snare on 2 and 4.")
    assert [(n["pitch"], n["start"]) for n in _g_half81] == [(36, 0.0), (38, 2.0)], _g_half81
    assert len(_g_four81) == 10 and _u_half81["half_time"] is True
    # the label must not steal the positions of the mentions after it
    assert "kick on 1" in _u_half81["phrases"] and "snare on 3" in _u_half81["phrases"]
    # two instruments in one clause keep their own positions
    _g_mix81, _u_mix81 = _ar80.drum_grid_to_notes("16th hats with backbeat snare on 2 and 4.")
    assert "hat 16ths" in _u_mix81["phrases"] and "snare on 2+4" in _u_mix81["phrases"], _u_mix81
    assert _ar80.drum_grid_to_notes("Dense wash, no clear pulse.")[0] == [], "unparsable grid is empty, not invented"
    _b_half81 = _core.beat_from_grid(_g_half81, bars=2, tempo=70.0, seed=0)
    _b_four81 = _core.beat_from_grid(_g_four81, bars=2, tempo=125.0, seed=0)
    assert _b_half81["source_phrase_ids"] != _b_four81["source_phrase_ids"], \
        "different drum grids must retrieve different phrases of his (tempo-only seeding would not)"
    assert _b_half81["notes"] and _b_four81["notes"]
    assert "grid:" in _b_half81["match"]
    # 15: a grid-driven pick must NOT be re-thinned toward the stock backbeat
    assert not any(t.startswith("thin:") for t in _b_half81["transforms_applied"]), \
        _b_half81["transforms_applied"]
    _b_nogrid81 = _core.beat_from_grid([], bars=2, tempo=70.0, density="minimal", seed=0)
    assert "tempo only" in _b_nogrid81["match"], _b_nogrid81["match"]

    # 12/13/16/17: retrieval unblocked (was: same 9 phrases, nothing at 52 BPM)
    assert len(_core.find_phrases(kind="drum", tempo=52, k=5)) >= 1, \
        "tempo filter must run BEFORE diversify, or slow asks return nothing"
    assert ({p["id"] for p in _core.find_phrases(kind="drum", tempo=62, k=5)}
            != {p["id"] for p in _core.find_phrases(kind="drum", tempo=72, k=5)}), \
        "different tempo asks must return different drum phrases"
    _drumroom81 = _moment42.build_moment(
        [{"name": "Drums", "notes": [
            {"pitch": 36, "start": i * 0.5, "duration": 0.2, "velocity": 100}
            for i in range(16)]}], tempo=120.0)
    assert (_core.find_phrases_with_ears(kind="drum", role="drums", k=4, moment=_drumroom81)["gate_report"]
            == _core.find_phrases_with_ears(kind="drum", role="drum", k=4, moment=_drumroom81)["gate_report"]), \
        "role='drums' must normalize to 'drum' before the ears role gate"

    # ledger: the write stamp and truthful end-of-session sweep summary
    with tempfile.TemporaryDirectory() as _td81:
        _lp81 = os.path.join(_td81, "ledger.json")
        _e1_81 = _ledger81.record_write(clip_name="ans a s0", slot="3/4",
                                        source="catalogue", role="lead", gear=2,
                                        ears="live", path=_lp81)
        _e2_81 = _ledger81.record_write(clip_name="beat b s0", slot="3/5",
                                        source="ref:yt-abc", role="drum", path=_lp81)
        assert _e1_81["session_id"] == _e2_81["session_id"], "one night, one session"
        assert _e1_81["terminal_state"] is None and _e2_81["terminal_state"] is None
        _v81 = _ledger81.record_verdict("keep", slot="3/4", path=_lp81)
        assert _v81["immediate_verdict"] == "keep"
        assert _v81["terminal_state"] is None, \
            "an immediate KEEP is not a terminal state — survival decides that"
        assert _ledger81.record_verdict("drop", slot="9/9", path=_lp81) is None, \
            "a verdict on an unknown clip is reported, not invented"
        _sum81 = _ledger81.summary(path=_lp81)
        assert _sum81["by_source"] == {"catalogue": 1, "ref": 1}, _sum81
        assert _sum81["awaiting_sweep"] == 2 and _sum81["judged"] == 1
        assert "close_session()" in _sum81["note"] and "Live is reachable" in _sum81["note"], _sum81
        assert "not built yet" not in _sum81["note"], _sum81["note"]
        # a forgotten close must not merge two nights
        _d81 = _ledger81.load(_lp81)
        _d81["sessions"][-1]["last_activity"] -= _ledger81.SESSION_IDLE_SECONDS + 60
        with open(_lp81, "w") as _fh81:
            json.dump(_d81, _fh81)
        _e3_81 = _ledger81.record_write(clip_name="next night", slot="3/6",
                                        source="catalogue", path=_lp81)
        assert _e3_81["session_id"] != _e1_81["session_id"], "idle gap starts a new session"
        _stale81 = _ledger81.load(_lp81)["sessions"][0]
        assert _stale81["ended_at"] and _stale81["ended_by"] == "idle", \
            "the stale session must end at its last activity, never at 'now'"
        assert _stale81["closed_at"] is None, \
            "ending is not closing — only a verified sweep scores the night"
    # --- review round 2 (2026-07-30): honesty gaps found by measuring ---
    import inspect
    # P1-4: a subdivision AFTER an instrument that already has beats belongs to
    # the NEXT instrument. This bled onto 18% of real grids and produced
    # confidently WRONG retrieval, which is worse than an empty grid.
    _bleed81, _bu81 = _ar80.drum_grid_to_notes(
        "snare click on 2 and 4 with 16th-note hi-hat tick")
    assert sum(1 for n in _bleed81 if n["pitch"] == 38) == 2, \
        "snare with explicit beats must not inherit the hi-hat's 16ths"
    assert sum(1 for n in _bleed81 if n["pitch"] == 42) == 16, _bu81
    # ...while a modifier BEFORE the instrument still applies
    assert "hat 16ths" in _ar80.drum_grid_to_notes(
        "16th hats with backbeat snare on 2 and 4.")[1]["phrases"]

    # P1-3: ref density vocabulary (low/medium/high) vs _DRUM_DENSITY_TARGETS
    # (minimal/mid/full). The silent .get(..., "mid") default made all three
    # thin identically while transforms_applied printed "thin:low→N" as if not.
    _thin81 = {d: _core.beat_from_grid([], bars=2, tempo=70.0, density=d, seed=0)
               for d in ("low", "medium", "high")}
    assert len({len(r["notes"]) for r in _thin81.values()}) == 3, \
        "low/medium/high must thin to genuinely different densities: %s" % {
            d: len(r["notes"]) for d, r in _thin81.items()}
    assert "thin:low→minimal→" in " ".join(_thin81["low"]["transforms_applied"])
    _junk81 = _core.beat_from_grid([], bars=2, tempo=70.0, density="banana", seed=0)
    assert "thin:skipped" in _junk81["transforms_applied"], _junk81["transforms_applied"]
    assert any("isn't a density word" in a for a in _junk81["assumptions"]), \
        "an unrecognised density must be announced, never silently defaulted"

    # P1-2: a pool too small to re-roll must SAY so (announcing only at zero
    # survivors made a dead end look like a working narrow)
    _tiny81, _tinynotes81 = _core._steer_ranked_by_ref(
        [{"id": "only", "tempo": 120.0, "notes": []}],
        {"ref_id": "r", "tempo": None, "mods": {}}, lambda p: [])
    assert any("won't change the answer" in n for n in _tinynotes81), _tinynotes81
    # ...and with a ref the pick samples deeper than the top 3, so the reference's
    # narrowing is actually visible in the answer (ref-off must stay at 3)
    assert "_pick_window = 8 if ref is not None else 3" in inspect.getsource(_core._answer_worker)

    # P3: the reference pass must not read the ears SESSION file — that is the
    # room's memory of tonight, and an ears-OFF call must not fail on it
    assert "_ears.admit(" in inspect.getsource(_core._steer_ranked_by_ref), \
        "ref steering must use admit() (no session read), not serve()"

    # swing is parsed and stored but implemented nowhere — it must say so
    assert any("swing" in u for u in
               _ar80.ref_to_mods(_card81(swing_pct=0.0))[1]), "swing must be declared unmapped"
    # a drums-only ref steers nothing MELODIC and must not imply otherwise
    _drumonly81 = _core.answer_my_loop(
        _seed81, key="D minor", seed=0, use_ears=False,
        ref=_ar80.ref_steering({"ref_id": "r-d", "structure": {"drum_grid": "kick on 1"}}))
    assert any("prose only" in a for a in _drumonly81["assumptions"]), \
        _drumonly81["assumptions"]

    # P1-1: every JAM write surface routes through jam_lifecycle and stamps once;
    # pedal judgments reach the ledger — an uninstrumented surface biases KEEP by interface
    print("--- jam_lifecycle integration: six writes + two judge paths ---")
    import jam_lifecycle as _jl81
    import sombra_listener as _sl81
    import live_write as _lw81

    _aml_fn81 = getattr(answer_my_loop, "fn", answer_my_loop)
    _te_fn81 = getattr(three_endings, "fn", three_endings)
    _ja_fn81 = getattr(judge_answer, "fn", judge_answer)
    _ans_notes81 = [{"pitch": 60, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 80}]
    _lw_ok81 = {"slot": "3/7", "track": "SIDEKICK JAM"}
    _seed_te81 = [{"pitch": 62, "start_beats": b * 1.0, "duration_beats": 0.9, "velocity": 80}
                  for b in range(4)]

    with tempfile.TemporaryDirectory() as _td81b:
        _lp81b = os.path.join(_td81b, "ledger.json")
        _orig_lp81 = _ledger81.LEDGER_PATH
        _ledger81.LEDGER_PATH = _lp81b
        # judge_answer resolves ears/develop session paths at call time from these
        # module globals; without redirecting them the selftest writes streaks and
        # hold state into HIS live session files (2026-09-03: it did exactly that).
        import develop_session as _ds81
        _orig_esp81 = _ears81.SESSION_PATH
        _orig_dsp81 = _ds81.STATE_PATH
        _ears81.SESSION_PATH = os.path.join(_td81b, "ears_session.json")
        _ds81.STATE_PATH = os.path.join(_td81b, "develop_session.json")
        _orig_aml81 = _core.answer_my_loop
        _orig_lw81 = _lw81.write_answer_to_jam_track
        _orig_del81 = _lw81.delete_jam_clip
        _orig_sel81 = _sl81._selection_notes
        try:
            async def _ok81(notes, *, clip_name, tempo):
                return dict(_lw_ok81)

            _lw81.write_answer_to_jam_track = _ok81

            # 1. answer_my_loop verified write + exact ledger row
            _core.answer_my_loop = lambda *a, **k: {
                "answer_notes": _ans_notes81, "source_phrase_ids": ["pX|c0"],
                "tempo": 121.0, "ears": "live",
            }
            _r81_aml = _aml_fn81(seed_notes=_ans_notes81, write=True, gear=2, seed=3)
            assert _r81_aml["ledger"]["entry_id"].startswith("e-")
            _e81 = _ledger81.load(_lp81b)["entries"][-1]
            assert _e81["clip_name"] == "ans pX|c0 s3" and _e81["slot"] == "t3/s7"
            assert _e81["gear"] == 2 and _e81["source"] == "catalogue"
            assert _e81["ears"] == "live" and _e81["tempo"] == 121.0
            assert _e81["n_notes"] == 1

            # 2. Live writer raises — no new ledger row
            async def _boom81(notes, *, clip_name, tempo):
                raise RuntimeError("Producer Pal dead")
            _lw81.write_answer_to_jam_track = _boom81
            _r81_err = _aml_fn81(seed_notes=_ans_notes81, write=True, gear=2)
            assert "live_write_error" in _r81_err and "ledger" not in _r81_err
            assert len(_ledger81.load(_lp81b)["entries"]) == 1

            # 3. no_fit refusal — never calls Live
            _called81 = {"live": False}
            async def _never81(*a, **k):
                _called81["live"] = True
                return dict(_lw_ok81)
            _lw81.write_answer_to_jam_track = _never81
            _core.answer_my_loop = lambda *a, **k: {"no_fit": True, "answer_notes": []}
            _r81_nf = _aml_fn81(seed_notes=_ans_notes81, write=True, gear=2)
            assert _r81_nf.get("live_write_skipped") == _jl81.NO_FIT_SKIP_MSG
            assert not _called81["live"]

            # 4. three_endings write stamps each ending once; nullable ledger tempo
            _live_tempos_te: list[float | None] = []

            async def _ok_te81(notes, *, clip_name, tempo):
                _live_tempos_te.append(tempo)
                return dict(_lw_ok81)

            _lw81.write_answer_to_jam_track = _ok_te81
            _r81_te = _te_fn81(seed_notes=_seed_te81, write=True)
            assert len(_r81_te["live_writes"]) == 3
            assert all("ledger" in w for w in _r81_te["live_writes"])
            assert _live_tempos_te == [120.0, 120.0, 120.0]
            _te_rows = _ledger81.load(_lp81b)["entries"][-3:]
            assert all(r["tempo"] is None for r in _te_rows)
            assert len(_ledger81.load(_lp81b)["entries"]) == 4

            # 5–6. apply_audio_ref + beat_from_ref write paths stamp
            # the apply fixture carries HIS ownership: anyone else's
            # transcription stays a preview and never reaches the ledger
            _ar_pkg81 = {"notes": _ans_notes81, "clip_name": "ref yt-x",
                         "tempo": 118.0, "card": {"ownership": "his"}}
            import audio_ref as _ar81  # noqa: E402
            _orig_apply81 = _ar81.apply_audio_ref
            _orig_card81 = _ar81.get_ref_card
            _orig_steer81 = _ar81.ref_steering
            _orig_grid81 = _ar81.drum_grid_to_notes
            _orig_bfg81 = _core.beat_from_grid
            try:
                _lw81.write_answer_to_jam_track = _ok81
                _ar81.apply_audio_ref = lambda *a, **k: dict(_ar_pkg81)
                _r81_ar = apply_audio_ref("yt-x", write=True)
                assert _r81_ar["ledger"]["entry_id"].startswith("e-")
                _e_ar = _ledger81.load(_lp81b)["entries"][-1]
                # fixture package echoes no resolved role (auto) -> the single
                # written note classifies honestly as the ambiguous "other"
                assert _e_ar["source"] == "ref:yt-x" and _e_ar["role"] == "other"
                assert _e_ar["tempo"] == 118.0

                _ar_pkg_null = {"notes": _ans_notes81, "clip_name": "ref no-tempo",
                                "card": {"ownership": "his"}}
                _live_tempos_ar: list[float | None] = []

                async def _ok_ar_null(notes, *, clip_name, tempo):
                    _live_tempos_ar.append(tempo)
                    return dict(_lw_ok81)

                _lw81.write_answer_to_jam_track = _ok_ar_null
                _ar81.apply_audio_ref = lambda *a, **k: dict(_ar_pkg_null)
                _r81_ar_null = apply_audio_ref("yt-null", write=True)
                assert _r81_ar_null["ledger"]["entry_id"].startswith("e-")
                assert _ledger81.load(_lp81b)["entries"][-1]["tempo"] is None
                assert _live_tempos_ar == [120.0]

                _ar81.get_ref_card = lambda ref_id: {"structure": {"density": "mid"}}
                _ar81.ref_steering = lambda card: {"ref_id": "yt-x", "drum_grid": "kick on 1",
                                                   "identity": "x", "ricardo_note": ""}
                _ar81.drum_grid_to_notes = lambda g: ([], {"phrases": []})
                # the steered pick differs from the unsteered one (which runs
                # with density=None), so the ref honestly keeps its credit
                _core.beat_from_grid = lambda *a, **k: {
                    "notes": _ans_notes81,
                    "tempo": 119.0,
                    "source_phrase_ids": [
                        "drum|real|clip" if k.get("density") is not None
                        else "drum|plain|clip"],
                }
                _lw81.write_answer_to_jam_track = _ok81
                _r81_bfr = beat_from_ref("yt-x", write=True, seed=2)
                assert _r81_bfr["ledger"]["entry_id"].startswith("e-")
                _e_bfr = _ledger81.load(_lp81b)["entries"][-1]
                assert _e_bfr["source"] == "ref:yt-x" and _e_bfr["role"] == "drum"
                assert _e_bfr["clip_name"] == "beat yt-x s2"
                assert _e_bfr["source_phrase_id"] == "drum|real|clip"
                assert _e_bfr["tempo"] == 119.0
            finally:
                _ar81.apply_audio_ref = _orig_apply81
                _ar81.get_ref_card = _orig_card81
                _ar81.ref_steering = _orig_steer81
                _ar81.drum_grid_to_notes = _orig_grid81
                _core.beat_from_grid = _orig_bfg81

            # 7–8. SOMBRA answer + three_endings stamp via lifecycle
            _n_before_sl = len(_ledger81.load(_lp81b)["entries"])
            _sl81._selection_notes = lambda: _ans_notes81
            _core.answer_my_loop = lambda *a, **k: {
                "answer_notes": _ans_notes81, "source_phrase_ids": ["pedal-src"],
                "tempo": 120.0, "key": "D minor",
            }
            _r_sl_ans = _sl81._execute({"tool": "answer_my_loop", "tool_args": {"gear": 2}})
            assert _r_sl_ans["slot"] == "3/7"
            assert len(_ledger81.load(_lp81b)["entries"]) == _n_before_sl + 1

            _n_before_te = len(_ledger81.load(_lp81b)["entries"])
            _sl81._selection_notes = lambda: _seed_te81
            _r_sl_te = _sl81._execute({"tool": "three_endings", "tool_args": {}})
            assert len(_r_sl_te["slots"]) == 3
            assert len(_ledger81.load(_lp81b)["entries"]) == _n_before_te + 3

            # MCP judge_answer KEEP + DROP preview/confirm
            _core.set_ratings_path(os.path.join(_td81b, "ratings.json"))
            _set81 = {"set_id": "selftest-set-81", "runtime_epoch": "selftest-epoch-81",
                      "file_path": None}

            def _clip81(clip_id, slot):
                return {"clip_id": clip_id, "track_id": "jam-track-81",
                        "track": "SIDEKICK JAM", "surface": "session",
                        "path": "session:%s" % slot}

            # Judge an actual verified-write row: identity and the snapshot that
            # was heard must agree before the wrapper may stamp it.
            _judge_entry81 = _ledger81.record_write(
                clip_name="judge-me", slot="3/9", source="catalogue",
                source_phrase_id="pedal-src", answer_notes=_ans_notes81,
                key="D minor", role="lead", gear=2, set_identity=_set81,
                clip_ref=_clip81("clip-judge-keep-81", "3/9"), path=_lp81b)
            _r_keep81 = _ja_fn81("keep", slot="3/9", source_phrase_id="pedal-src",
                                 answer_notes=_ans_notes81)
            assert _r_keep81["ledger"]["entry_id"] == _judge_entry81["id"]
            assert _ledger81.load(_lp81b)["entries"][-1]["immediate_verdict"] == "keep"

            # KEEP consumes that pending row.  Use fresh verified rows for the
            # deletion preview and confirmed deletion so each call still has an
            # exact pending identity and cannot reuse the kept row by slot.
            _drop_preview_entry81 = _ledger81.record_write(
                clip_name="judge-me preview", slot="3/9", source="catalogue",
                source_phrase_id="pedal-src", answer_notes=_ans_notes81,
                key="D minor", role="lead", gear=2, set_identity=_set81,
                clip_ref=_clip81("clip-judge-preview-81", "3/9"), path=_lp81b)
            _r_drop_prev81 = _ja_fn81("drop", slot="3/9", source_phrase_id="pedal-src",
                                      answer_notes=_ans_notes81, confirm=False)
            assert _r_drop_prev81["clip_delete"]["would_delete"] == "t3/s9"
            assert _r_drop_prev81["clip_delete"]["expected_clip_id"] == "clip-judge-preview-81"
            assert _r_drop_prev81["ledger"]["entry_id"] == _drop_preview_entry81["id"]
            _delete_calls81: list[dict] = []

            async def _del81(slot, **expected):
                _delete_calls81.append({"slot": slot, **expected})
                return {"deleted": True, "slot": slot}

            _lw81.delete_jam_clip = _del81
            _drop_confirm_entry81 = _ledger81.record_write(
                clip_name="judge-me confirm", slot="3/9", source="catalogue",
                source_phrase_id="pedal-src", answer_notes=_ans_notes81,
                key="D minor", role="lead", gear=2, set_identity=_set81,
                clip_ref=_clip81("clip-judge-confirm-81", "3/9"), path=_lp81b)
            _r_drop81 = _ja_fn81("drop", slot="3/9", source_phrase_id="pedal-src",
                                 answer_notes=_ans_notes81, confirm=True)
            assert _r_drop81["clip_delete"] == {"deleted": True, "slot": "t3/s9"}
            assert _delete_calls81 == [{"slot": "t3/s9",
                                        "expected_clip_id": "clip-judge-confirm-81",
                                        "expected_track_id": "jam-track-81",
                                        "expected_set_identity": _set81}]
            assert _r_drop81["ledger"]["entry_id"] == _drop_confirm_entry81["id"]

            # SOMBRA judge KEEP + confirm DROP clears pedal slot
            _pedal_keep81 = _ledger81.record_write(
                clip_name="pedal-j keep", slot="3/8", source="catalogue",
                source_phrase_id="pedal-src", answer_notes=_ans_notes81, key="D minor",
                role="lead", gear=2, set_identity=_set81,
                clip_ref=_clip81("clip-pedal-keep-81", "3/8"), path=_lp81b)
            _sl81.STATE["last_answer_slot"] = "3/8"
            _sl81.STATE["last_answer_source"] = "pedal-src"
            _sl81.STATE["last_answer_notes"] = _ans_notes81
            _sl81.STATE["last_answer_seed"] = _ans_notes81
            _sl81.STATE["last_answer_key"] = "D minor"
            _sl81.STATE["last_answer_entry_id"] = _pedal_keep81["id"]
            _sl81.STATE["last_answer_clip_ref"] = _pedal_keep81["clip_ref"]
            _sl81.STATE["last_answer_set_identity"] = _pedal_keep81["set_identity"]
            _r_sl_keep = _sl81._execute({
                "tool": "judge_answer",
                "tool_args": {"verdict": "keep", "slot": "3/8"},
            })
            assert _r_sl_keep["ledger"]["entry_id"] == _pedal_keep81["id"]
            _pedal_drop81 = _ledger81.record_write(
                clip_name="pedal-j drop", slot="3/8", source="catalogue",
                source_phrase_id="pedal-src", answer_notes=_ans_notes81, key="D minor",
                role="lead", gear=2, set_identity=_set81,
                clip_ref=_clip81("clip-pedal-drop-81", "3/8"), path=_lp81b)
            _sl81.STATE["last_answer_entry_id"] = _pedal_drop81["id"]
            _sl81.STATE["last_answer_clip_ref"] = _pedal_drop81["clip_ref"]
            _sl81.STATE["last_answer_set_identity"] = _pedal_drop81["set_identity"]
            _r_sl_drop = _sl81._execute({
                "tool": "judge_answer",
                "tool_args": {"verdict": "drop", "slot": "3/8", "confirm": True},
            })
            assert _r_sl_drop["clip_delete"] == {"deleted": True, "slot": "3/8"}
            assert _delete_calls81[-1] == {
                "slot": "3/8", "expected_clip_id": "clip-pedal-drop-81",
                "expected_track_id": "jam-track-81", "expected_set_identity": _set81}
            assert _sl81.STATE["last_answer_slot"] is None

            # pedal confirm without explicit slot must not delete via STATE fallback
            _sl81.STATE["last_answer_slot"] = "3/6"
            _deleted_no_slot: list[str] = []

            async def _del_no_slot(slot, **expected):
                _deleted_no_slot.append(slot)
                return {"deleted": False, "slot": slot,
                        "reason": "explicit pedal slot required"}

            _lw81.delete_jam_clip = _del_no_slot
            _r_sl_drop_implicit = _sl81._execute({
                "tool": "judge_answer",
                "tool_args": {"verdict": "drop", "confirm": True},
            })
            assert _r_sl_drop_implicit["clip_delete"]["deleted"] is False
            assert _deleted_no_slot == []
            assert _sl81.STATE["last_answer_slot"] == "3/6"
            _lw81.delete_jam_clip = _del81

            # ledger failure after verified write — live_write preserved
            with open(_lp81b, "w") as _bad81:
                _bad81.write("{bad")
            _core.answer_my_loop = lambda *a, **k: {
                "answer_notes": _ans_notes81, "source_phrase_ids": ["pZ"],
                "tempo": 120.0,
            }
            _lw81.write_answer_to_jam_track = _ok81
            _r81_led_err = _aml_fn81(seed_notes=_ans_notes81, write=True, gear=2)
            assert _r81_led_err["live_write"]["slot"] == "3/7"
            assert "ledger_error" in _r81_led_err and "ledger" not in _r81_led_err
        finally:
            _ledger81.LEDGER_PATH = _orig_lp81
            _ears81.SESSION_PATH = _orig_esp81
            _ds81.STATE_PATH = _orig_dsp81
            _core.answer_my_loop = _orig_aml81
            _lw81.write_answer_to_jam_track = _orig_lw81
            _lw81.delete_jam_clip = _orig_del81
            _sl81._selection_notes = _orig_sel81

    with tempfile.TemporaryDirectory() as _td82:
        _lp82 = os.path.join(_td82, "l.json")
        _ledger81.record_write(clip_name="c", slot="1/1", source="ref:x", path=_lp82)
        assert _ledger81.load(_lp82)["sessions"][-1]["mode"] in (None, "ref")
    print("  translator pure + frozen; reference gates without claiming Live;")
    print("  drum grids retrieve different phrases; ledger stamps writes")
    print("  subdivision bleed closed; density vocabulary translated + honest;")
    print("  narrow pools announced; every JAM surface stamped incl. the pedal")
    print("  OK\n")

    # 82. the end-of-session sweep — the durable-KEEP number itself
    print("--- close_session: terminal states, survival rate, fail-closed ---")
    import live_write as _lw82

    # PP session-clip parse survives nested objects and both slot serializations
    _clips82 = _lw82._parse_session_clips(
        '{trackIndex:3,name:"SIDEKICK JAM",sessionClips:['
        '{slot:"3/0",name:"ans a s0",instrument:{name:"Operator"}},'
        '{slot:"3/1",name:"ans b s0"},'
        '{trackIndex:3,sceneIndex:2,name:"beat c s0"}]}')
    assert _clips82 == [{"slot": "3/0", "name": "ans a s0"},
                        {"slot": "3/1", "name": "ans b s0"},
                        {"slot": "3/2", "name": "beat c s0"}], _clips82

    # the resolution table, exhaustively (this IS the metric definition)
    for _v82, _p82, _want82 in (("keep", True, "keep"), ("keep", False, "drop"),
                                ("drop", True, "drop"), ("drop", False, "drop"),
                                (None, True, "unjudged"), (None, False, "abandoned")):
        assert _ledger81.resolve_terminal({"immediate_verdict": _v82}, _p82) == _want82, \
            (_v82, _p82)

    with tempfile.TemporaryDirectory() as _td83:
        _lp83 = os.path.join(_td83, "l.json")
        _set83 = {"set_id": "selftest-set-83", "runtime_epoch": "selftest-epoch-83",
                  "file_path": None}
        for _n83, _s83, _src83, _r83 in (("ans a s0", "3/0", "catalogue", "lead"),
                                         ("ans b s0", "3/1", "ref:yt-x", "lead"),
                                         ("beat c s0", "3/2", "ref:yt-x", "drum"),
                                         ("ans d s0", "3/3", "catalogue", "lead")):
            _clip_id83 = {"ans a s0": "clip-a-83", "ans b s0": "clip-b-83",
                          "beat c s0": "clip-c-83", "ans d s0": "clip-d-83"}[_n83]
            _ledger81.record_write(clip_name=_n83, slot=_s83, source=_src83,
                                   role=_r83, gear=2, set_identity=_set83,
                                   clip_ref={"clip_id": _clip_id83, "track_id": "jam-track-83",
                                             "track": "SIDEKICK JAM", "surface": "session",
                                             "path": "session:%s" % _s83}, path=_lp83)
        _ledger81.record_verdict("keep", slot="3/0", path=_lp83)   # kept + present
        _ledger81.record_verdict("keep", slot="3/1", path=_lp83)   # kept, then deleted
        _ledger81.record_verdict("drop", slot="3/2", path=_lp83)   # dropped, still there
        # "ans d" never judged and gone -> abandoned. "beat c" was dragged to a new row.
        _rep83 = _ledger81.close_session(
            {"set_identity": _set83, "complete": True, "errors": [], "clips": [
                {"clip_id": "clip-a-83", "track_id": "jam-track-83",
                 "track": "SIDEKICK JAM", "surface": "session", "path": "session:3/0"},
                {"clip_id": "clip-c-83", "track_id": "jam-track-83",
                 "track": "SIDEKICK JAM", "surface": "session", "path": "session:3/9"},
            ]}, path=_lp83)
        assert _rep83["counts"] == {"keep": 1, "drop": 2, "abandoned": 1, "unjudged": 0}, \
            _rep83["counts"]
        # the primary rate is over JUDGED only — unjudged/abandoned must not inflate it
        assert _rep83["judged"] == 3 and _rep83["keep_rate"] == round(1 / 3, 3)
        assert _rep83["rate_status"] == "definitive", _rep83["rate_status"]
        assert _rep83["late_verdicts"]["counts"] == {} \
            and _rep83["unscoreable"] == {"rows": 0, "judged": 0}
        assert _rep83["keep_rate_if_abandoned_counted_as_drop"] == 0.25, \
            "the harsher reading must be shown beside it, not instead of it"
        assert set(_rep83["strata"]) == {"catalogue|lead|gear2", "ref|lead|gear2",
                                         "ref|drum|gear2"}, _rep83["strata"]
        assert "too thin" in _rep83.get("warning", ""), "a 3-judgment night is not a rate"
        # dragging a clip to another row is reordering, not deletion
        assert next(e for e in _rep83["entries"] if e["clip_name"] == "beat c s0")["state"] == "drop"
        assert next(e for e in _rep83["entries"] if e["clip_name"] == "ans a s0")["state"] == "keep"
        # the session is closed, and closing again finds nothing open
        assert _ledger81.load(_lp83)["sessions"][-1]["closed_by"] == "sweep"
        assert _ledger81.close_session([], path=_lp83)["ok"] is False
        # a swept entry is never re-resolved by a later sweep
        assert all(e.get("terminal_state") for e in _ledger81.load(_lp83)["entries"])
        # a night with writes but no judgments has an UNDEFINED rate, not 0.0
        _lp84 = os.path.join(_td83, "l2.json")
        _ledger81.record_write(clip_name="x", slot="1/1", source="catalogue", path=_lp84)
        _rep84 = _ledger81.close_session([], path=_lp84)
        assert _rep84["keep_rate"] is None and _rep84["counts"] == {
            "keep": 0, "drop": 0, "abandoned": 0, "unjudged": 0}
        # an identity-less row is unscoreable by any sweep — never "unresolved"
        assert _rep84["resolved"] == 0 and _rep84["unresolved"] == []
        assert _rep84["unscoreable"] == {"rows": 1, "judged": 0}, _rep84["unscoreable"]
        assert _rep84["coverage"]["complete"] is False
        assert "unscoreable" in _rep84["warning"], _rep84["warning"]
        _sess84 = _ledger81.load(_lp84)["sessions"][-1]
        assert _sess84["closed_at"] is None and _sess84["closed_by"] is None, \
            "an identity-less night is never swept — only attestation settles it"
        assert _sess84["ended_at"] is None, \
            "a partial/identity-less inventory records coverage only, no ending"
    # the MCP tool must fail CLOSED: an unreachable Live leaves the session open
    _srv_src81 = inspect.getsource(sys.modules[__name__])
    _cs82_start = _srv_src81.index("def close_session(")
    _cs82_end = _srv_src81.find("\ndef ", _cs82_start + 1)
    _cs82 = (_srv_src81[_cs82_start:]
             if _cs82_end == -1 else _srv_src81[_cs82_start:_cs82_end])
    assert "left OPEN" in _cs82 and "read_set_inventory" in _cs82, \
        "an empty read looks like 'he deleted everything' — never score on it"
    print("  parse tolerant; 6-case resolution table; rate over judged only;")
    print("  reorder != deletion; legacy incomplete evidence stays unresolved; fails closed")
    print("  OK\n")

    # 85. Stage A — named-but-unheard audio (PP2 shapes; no Live / no agy)
    print("--- moment unheard: PP2 empty/audio/track-name map ---")
    import asyncio as _aio85  # noqa: E402
    import live_write as _lw85  # noqa: E402
    import moment as _moment85  # noqa: E402
    import ears as _ears85  # noqa: E402
    # Official-shaped PP2 fixtures (compact text, matching read-clip helpers).
    _pp2_empty85 = '{id:null,type:null,name:null,slot:"3/0"}'
    _pp2_audio85 = (
        '{id:"5",type:"audio",name:"Take 4",view:"session",'
        'trackIndex:3,sceneIndex:0}'
    )
    _pp2_midi85 = (
        '{id:"2",type:"midi",name:"Bass loop",view:"session",'
        'trackIndex:1,sceneIndex:0,'
        'notes:"v90 n/2 C2 1|1 v90 n/2 C2 1|3"}'
    )
    _pp2_blank_midi85 = (
        '{id:"9",type:"midi",name:"Empty MIDI",view:"session",'
        'trackIndex:2,sceneIndex:0}'
    )
    _pp2_unknown85 = '{id:"c3",name:"Something",slot:"4/0"}'  # id, no type
    import core as _core85  # noqa: E402
    _notes85 = _core85.pp_notes_to_notes('v90 n/2 C2 1|1 v90 n/2 C2 1|3')
    assert _lw85.classify_session_clip(_pp2_empty85, []) == "empty", \
        "PP2 null empty-slot must not become unheard_unknown"
    assert _lw85.classify_session_clip(_pp2_audio85, []) == "unheard_audio"
    assert _lw85.classify_session_clip(_pp2_midi85, _notes85) == "heard"
    assert _lw85.classify_session_clip(_pp2_blank_midi85, []) == "empty", \
        "blank MIDI is no audible content, not unknown audio"
    assert _lw85.classify_session_clip(_pp2_unknown85, []) == "unheard_unknown"
    assert _lw85.classify_session_clip("", []) == "empty"
    assert _lw85.classify_session_clip("no clip at 2/0", []) == "empty"
    assert _lw85.classify_session_clip(
        "Producer Pal tool error: bridge returned no clip data before disconnect",
        []) == "invalid", \
        "unanchored 'no clip' must not be treated as an empty slot"
    assert _lw85.is_empty_slot_error(
        "Producer Pal tool error: bridge returned no clip data before disconnect"
    ) is False
    assert _lw85.is_empty_slot_error("no clip at trackIndex 3, sceneIndex 0") is True
    assert _lw85.is_empty_slot_error(
        "Producer Pal tool error: no clip at trackIndex 3, sceneIndex 0"
    ) is True
    assert _lw85.is_empty_slot_error(
        "Producer Pal tool error: no clip at trackIndex 3, sceneIndex 0."
    ) is True
    # Embedded exact phrase must NOT be swallowed (transport/timeout wrappers).
    _embedded85 = "timeout after no clip at trackIndex 0, sceneIndex 0"
    assert _lw85.is_empty_slot_error(_embedded85) is False
    assert _lw85.classify_session_clip(_embedded85, []) == "invalid"
    assert _lw85.is_empty_slot_error(
        "before no clip at 2/0 after"
    ) is False
    # Track names come from the overview, not the clip raw.
    _ov85 = (
        'tempo: 120, tracks:['
        '{trackIndex:0,name:"Pad",type:"midi"},'
        '{trackIndex:1,name:"Bass",type:"midi"},'
        '{trackIndex:2,name:"EmptyMIDI",type:"midi"},'
        '{trackIndex:3,name:"GTR1",type:"audio"}]'
    )
    _names85 = _lw85.track_names_by_index(_ov85)
    assert _names85[3] == "GTR1" and _names85[1] == "Bass"
    _card85 = _moment85._unheard_card_from_clip(
        {"raw": _pp2_audio85, "slot": 0}, 3, "audio", _names85)
    assert _card85["track"] == "GTR1" and _card85["clip_name"] == "Take 4", \
        "must not fall back to 'track 3' when the overview knows GTR1"
    assert "likely_role" not in _card85
    # build_moment contracts (digest / status / gates) still hold
    _m85 = _moment85.build_moment(
        [{"name": "Bass", "notes": _notes85}],
        tempo=120.0,
        unheard=[_card85],
    )
    assert "bass" in _m85["roles"] and len(_m85["unheard"]) == 1
    assert any("cannot hear" in a for a in _m85["assumptions"])
    _m85u = _moment85.build_moment(
        [], tempo=120.0,
        unheard=[{"track": "FX", "track_index": 4, "slot": 0,
                  "kind": "unknown", "clip_name": "Something"}],
    )
    assert any("uncertain kind" in a for a in _m85u["assumptions"])
    _m85a = _moment85.build_moment(
        [], tempo=75.0,
        unheard=[{"track": "VOX", "track_index": 2, "slot": 0,
                  "kind": "audio", "clip_name": "verse"}],
    )
    _line85a = _ears85.status_line(_m85a)
    assert "no midi heard" in _line85a and "1 clip unheard" in _line85a
    assert "empty room" not in _line85a and "VOX" not in _line85a
    _m85_bare = _moment85.build_moment(
        [{"name": "Bass", "notes": _notes85}], tempo=120.0)
    assert _m85["digest"] != _m85_bare["digest"]
    assert _moment85.build_moment(
        [{"name": "Bass", "notes": _notes85}], tempo=120.0, unheard=[]
    )["digest"] == _m85_bare["digest"]
    assert _moment85.infer_role("GTR1", []) == "lead"
    _room85g = _moment85.build_moment(
        [{"name": "Lead x", "notes": [
            {"pitch": p, "start": i * 1.0, "duration": 0.9, "velocity": 90}
            for i, p in enumerate((74, 77, 81, 74))]}],
        tempo=75.0, unheard=[_card85],
    )
    _fit85 = {"id": "fit", "role": "lead", "key": _room85g["key"], "tempo": 76,
              "notes": [{"pitch": 62, "start": 0.0, "duration": 1.0, "velocity": 80},
                        {"pitch": 60, "start": 2.0, "duration": 1.0, "velocity": 80}]}
    _clash85 = {"id": "clash", "role": "harmony", "key": _room85g["key"], "tempo": 76,
                "notes": [{"pitch": 75, "start": 0.0, "duration": 1.0, "velocity": 80}]}
    _srv85, _rep85 = _ears85.admit([_fit85, _clash85], _room85g, role="lead")
    assert [c["id"] for c in _srv85] == ["fit"], _rep85
    assert _moment85.blind_moment("bridge down")["unheard"] == []
    # Exercise the current PP session seam with explicit playing-slot evidence.
    class _MomentFixture:
        capabilities = {}

        def __init__(self, overview, clips=None, arrangement=None):
            self.overview = overview
            self.clips = clips or {}
            self.arrangement = arrangement or {}

        async def call(self, name, args=None):
            _pp_session._selftest_schema_check(name, args)
            args = args or {}
            if name == "ppal-read-live-set":
                result = self.overview
            elif name == "ppal-read-track":
                result = {"playingSlotIndex": 0,
                          "arrangementClips": self.arrangement.get(args["trackIndex"], [])}
            elif name == "ppal-read-clip":
                # the device's schema forms: path "tN/sM" or id (captured 2.2.0-rc1)
                key = None
                _m85 = __import__("re").fullmatch(r"t(\d+)/s(\d+)", str(args.get("path") or ""))
                if _m85:
                    key = "%s/%s" % _m85.groups()
                elif args.get("id") is not None:
                    key = next((k for k, v in self.clips.items()
                                if isinstance(v, dict) and str(v.get("id")) == str(args["id"])),
                               args["id"])
                result = self.clips.get(key, {"id": None})
                if isinstance(result, Exception):
                    raise result
            else:
                raise AssertionError(name)
            return _pp_session.parse_tool_response(json.dumps({"result": result}))

    _overview85 = {"tempo": 120, "timeSignature": "4/4", "isPlaying": True,
                   "tracks": [{"trackIndex": i, "name": name, "type": kind}
                              for i, (name, kind) in enumerate([
                                  ("Pad", "midi"), ("Bass", "midi"),
                                  ("Empty", "midi"), ("GTR1", "audio")])]}
    _fixture85 = _MomentFixture(_overview85, {
        "0/0": {"id": None},
        "1/0": {"id": "2", "type": "midi", "notes": _core._note_dicts(_notes85)},
        "2/0": {"id": "9", "type": "midi", "notes": []},
        "3/0": {"id": "5", "type": "audio", "name": "Take 4"},
    })
    _live85 = _aio85.run(_moment85.read_moment_live(max_tracks=12, session=_fixture85))
    assert _live85["ears"] == "live"
    assert set(_live85["roles"]) == {"bass"}, _live85["roles"]
    assert len(_live85["unheard"]) == 1, _live85["unheard"]
    assert _live85["unheard"][0]["track"] == "GTR1"
    assert _live85["unheard"][0]["clip_name"] == "Take 4"
    assert _live85["unheard"][0]["kind"] == "audio"
    assert any("cannot hear" in a for a in _live85["assumptions"])
    assert _live85["context"]["active_slots"] == {"0": 0, "1": 0, "2": 0, "3": 0}
    _fixture85.clips["0/0"] = RuntimeError(_embedded85)
    try:
        _aio85.run(_moment85.read_moment_live(max_tracks=12, session=_fixture85))
        raise AssertionError("embedded empty-slot phrase must not hide a timeout")
    except RuntimeError as _exc85:
        assert "timeout after" in str(_exc85)
        assert not _lw85.is_empty_slot_error(_exc85)
    print("  PP2 empty→skip; audio→GTR1/Take 4; blank MIDI→skip;")
    print("  overview track map; e2e mock; embedded timeout raises;")
    print("  digest/gates unchanged")
    print("  OK\n")

    # 86. Mix Adviser integration — offline manual-export trial only
    print("--- Mix Adviser MCP wrappers: offline analyze → propose → A/B → judge → report ---")
    import shutil as _shutil86
    import subprocess as _subprocess86

    _mix_session_ledger86 = os.path.join(ROOT, "store", "session_ledger.json")
    _mix_session_before86 = (
        open(_mix_session_ledger86, "rb").read()
        if os.path.exists(_mix_session_ledger86)
        else None
    )
    _mix_seed86 = os.path.join(ROOT, "store", "mix_trial_ledger.json")
    _mix_seed_before86 = open(_mix_seed86, "rb").read()

    def _mix_make_wav86(path: str, frequency: int, gain_db: str) -> None:
        _ffmpeg86 = _shutil86.which("ffmpeg")
        assert _ffmpeg86, "ffmpeg is required for Mix Adviser integration selftest"
        _subprocess86.run(
            [
                _ffmpeg86, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i",
                f"sine=frequency={frequency}:duration=1.2",
                "-af", f"volume={gain_db}", "-ar", "48000", "-ac", "2",
                "-c:a", "pcm_s24le", "-y", path,
            ],
            check=True,
        )

    with tempfile.TemporaryDirectory(prefix="mix-mcp-integration-") as _mix_td86:
        _mix_ledger86 = os.path.join(_mix_td86, "mix_trial_ledger.json")
        _mix_artifacts86 = os.path.join(_mix_td86, "mix_checks")
        _mix_before86 = os.path.join(_mix_td86, "before.wav")
        _mix_group_a86 = os.path.join(_mix_td86, "group-a.wav")
        _mix_group_b86 = os.path.join(_mix_td86, "group-b.wav")
        _mix_after86 = os.path.join(_mix_td86, "after.wav")
        _mix_make_wav86(_mix_before86, 440, "-3dB")
        _mix_make_wav86(_mix_group_a86, 220, "-6dB")
        _mix_make_wav86(_mix_group_b86, 880, "-20dB")
        _mix_make_wav86(_mix_after86, 550, "-4dB")

        from mcp_server import mix_trials as _mix_trials86

        try:
            _mix_trials86.set_mix_trial_paths(_mix_ledger86, _mix_artifacts86)
            _mix_before_probe86 = _mix_trials86._probe_audio(_mix_before86)
            _mix_after_probe86 = _mix_trials86._probe_audio(_mix_after86)
            assert (_mix_before_probe86["sample_rate"], _mix_before_probe86["channels"]) == (48000, 2)
            assert (_mix_after_probe86["sample_rate"], _mix_after_probe86["channels"]) == (48000, 2)
            assert abs(_mix_before_probe86["duration"] - 1.2) <= 0.01
            assert abs(_mix_after_probe86["duration"] - _mix_before_probe86["duration"]) <= 0.01
            # analyze_mix_pass is async (runs off the MCP event loop).
            _mix_analysis86 = asyncio.run(analyze_mix_pass(
                _mix_before86,
                {"group_a": _mix_group_a86, "group_b": _mix_group_b86},
                target="clarity",
                listener=False,
            ))
            assert isinstance(_mix_analysis86["trial_id"], str)
            assert _mix_analysis86["trial_id"].strip()
            assert _mix_analysis86["measurements"]["master"]["astats"]["rms_db"]
            _mix_groups86 = _mix_analysis86["measurements"]["groups"]
            assert abs(
                _mix_groups86["group_a"]["astats"]["rms_db"]
                - _mix_groups86["group_b"]["astats"]["rms_db"]
            ) > 3.0
            assert _mix_analysis86["evidence_refs"]

            _mix_trial86 = propose_mix_moves(_mix_analysis86["trial_id"], max_moves=3)
            assert len(_mix_trial86["moves"]) <= 3
            assert _mix_trial86["moves"]
            for _move86 in _mix_trial86["moves"]:
                assert _move86["param"] == "gain_db"
                assert _move86["track"].strip().lower() not in ("main", "master")
                assert _move86["evidence_refs"]

            _mix_ab86 = prepare_mix_ab(_mix_analysis86["trial_id"], _mix_after86)
            for _key86 in (
                "a_path", "b_path", "artifacts_present", "sample_count", "output_lufs",
            ):
                assert _key86 in _mix_ab86
            assert _mix_ab86["artifacts_present"] is True
            assert _mix_ab86["sample_count"] > 0
            assert set(_mix_ab86["output_lufs"]) == {"A", "B"}
            _mix_a_probe86 = _mix_trials86._probe_audio(_mix_ab86["a_path"])
            _mix_b_probe86 = _mix_trials86._probe_audio(_mix_ab86["b_path"])
            assert _mix_a_probe86["sample_count"] == _mix_b_probe86["sample_count"]
            assert _mix_ab86["sample_count"] == _mix_a_probe86["sample_count"]
            _mix_ab_json86 = json.dumps(_mix_ab86)
            for _hidden86 in (
                "hidden_mapping", "before_after_mapping", "applied_gain_db",
                "duration_delta_seconds", "checksums",
            ):
                assert _hidden86 not in _mix_ab_json86

            _mix_judgment86 = judge_mix_pass(
                _mix_analysis86["trial_id"], "A", witness_rating="not_shown"
            )
            assert _mix_judgment86["choice_label"] in ("A", "B")
            assert _mix_judgment86["choice"] in ("before", "after")

            _mix_report86 = mix_trial_report()
            assert _mix_report86["decided_count"] == 1
            assert not any("score" in _key86.lower() for _key86 in _mix_report86)
        finally:
            _mix_trials86.set_mix_trial_paths()

    _mix_session_after86 = (
        open(_mix_session_ledger86, "rb").read()
        if os.path.exists(_mix_session_ledger86)
        else None
    )
    assert _mix_session_after86 == _mix_session_before86
    assert open(_mix_seed86, "rb").read() == _mix_seed_before86
    print("  local PCM WAVs only; listener off; session and default mix ledgers unchanged")
    print("  OK\n")

    # 87. Data integrity — atomic writes, schema versioning, stale taste model
    print("87. Data integrity — atomic writes, schema validation, stale taste model...")
    with tempfile.TemporaryDirectory(prefix="sidekick-integrity-test-") as _tmpdir87:
        # Test A: Atomic write cleanup on mid-write failure
        _atomic_target = os.path.join(_tmpdir87, "atomic_test.json")

        def _exploding_dump(obj, fp, *args, **kwargs):
            fp.write('{"partial": true')
            raise RuntimeError("simulated mid-write crash")

        # Verify atomic write cleans up temp file on exception
        import unittest.mock
        with unittest.mock.patch("json.dump", side_effect=_exploding_dump):
            try:
                _core._save_json_atomic(_atomic_target, {"key": "val"})
                assert False, "should have raised RuntimeError"
            except RuntimeError as _e:
                assert "simulated mid-write crash" in str(_e)

        # Target file must not exist
        assert not os.path.exists(_atomic_target)
        # No .tmp files left in tempdir
        _tmp_files = [f for f in os.listdir(_tmpdir87) if f.endswith(".tmp") or ".atomic_test.json." in f]
        assert len(_tmp_files) == 0, f"Leftover temp files found: {_tmp_files}"

        # Successful atomic write creates file cleanly
        _core._save_json_atomic(_atomic_target, {"status": "ok"}, indent=2)
        assert os.path.exists(_atomic_target)
        with open(_atomic_target, "r", encoding="utf-8") as _fh:
            assert json.load(_fh) == {"status": "ok"}

        # Test text atomic write cleanup on failure
        _text_target = os.path.join(_tmpdir87, "atomic_text.txt")
        with unittest.mock.patch("os.replace", side_effect=OSError("simulated replace error")):
            try:
                _core._write_text_atomic(_text_target, "hello world")
                assert False, "should have raised OSError"
            except OSError:
                pass
        assert not os.path.exists(_text_target)
        _tmp_files_text = [f for f in os.listdir(_tmpdir87) if ".atomic_text.txt." in f]
        assert len(_tmp_files_text) == 0

        # Successful text atomic write
        _core._write_text_atomic(_text_target, "hello atomic")
        assert open(_text_target, "r", encoding="utf-8").read() == "hello atomic"

        # Test B: Artifact schema validation
        _valid_artifact = os.path.join(_tmpdir87, "valid_artifact.json")
        _core._save_json_atomic(_valid_artifact, {
            "meta": {
                "schema_version": 1,
                "built_at": "2026-08-16T00:00:00Z",
                "code_rev": "abcdef",
                "corpus": {"n_projects": 42},
            },
            "data": "valid",
        })
        _loaded = _core._load_artifact(_valid_artifact, "valid_artifact.json")
        assert _loaded["data"] == "valid"

        # Schema version mismatch (e.g. version 999) must fail with clear rebuild message
        _mismatched_artifact = os.path.join(_tmpdir87, "mismatched_artifact.json")
        _core._save_json_atomic(_mismatched_artifact, {
            "meta": {
                "schema_version": 999,
                "built_at": "2026-08-16T00:00:00Z",
                "code_rev": "abcdef",
                "corpus": {"n_projects": 42},
            },
            "data": "mismatch",
        })
        try:
            _core._load_artifact(_mismatched_artifact, "mismatched_artifact.json")
            assert False, "should have raised ValueError on schema mismatch"
        except ValueError as _ve:
            assert "schema_version 999" in str(_ve)
            assert "rebuild" in str(_ve).lower()

        # Missing meta block policy (module default is strict: False):
        _legacy_artifact = os.path.join(_tmpdir87, "legacy_artifact.json")
        _core._save_json_atomic(_legacy_artifact, {"legacy_key": "legacy_val"})

        _legacy_default = core_shared._ALLOW_LEGACY_ARTIFACTS_WITHOUT_META
        try:
            # With ALLOW_LEGACY=True, it emits a warning and returns data
            import warnings
            core_shared._ALLOW_LEGACY_ARTIFACTS_WITHOUT_META = True
            with warnings.catch_warnings(record=True) as _w:
                warnings.simplefilter("always")
                _legacy_data = _core._load_artifact(_legacy_artifact, "legacy_artifact.json")
                assert _legacy_data["legacy_key"] == "legacy_val"
                assert any("lacks 'meta' block" in str(item.message) for item in _w)

            # With ALLOW_LEGACY=False, it raises ValueError
            core_shared._ALLOW_LEGACY_ARTIFACTS_WITHOUT_META = False
            try:
                _core._load_artifact(_legacy_artifact, "legacy_artifact.json")
                assert False, "should have raised ValueError when legacy disallowed"
            except ValueError as _ve:
                assert "lacks required 'meta' block" in str(_ve)
                assert "rebuild" in str(_ve).lower()
        finally:
            core_shared._ALLOW_LEGACY_ARTIFACTS_WITHOUT_META = _legacy_default

        # Test C: Two-step commit and Stale Taste Model Detection
        _ratings_file = os.path.join(_tmpdir87, "ratings.json")
        _taste_file = os.path.join(_tmpdir87, "taste_model.json")
        _sample_ratings = {
            "schema_version": 1,
            "ratings": [
                {
                    "id": 1,
                    "phrase_id": "test_lead_01",
                    "rating": 1,
                    "label": "gold",
                    "comment": "",
                },
                {
                    "id": 2,
                    "phrase_id": "test_lead_02",
                    "rating": -1,
                    "label": "meh",
                    "comment": "",
                },
            ],
        }
        _core._save_json_atomic(_ratings_file, _sample_ratings)
        _r_hash = _core._ratings_file_hash(_ratings_file)
        assert _r_hash is not None

        # Build synced taste model
        _taste_data = {
            "schema_version": 1,
            "n_pairs": 35,
            "n_gold_pairs": 5,
            "n_rounds": 0,
            "ratings_sha256": _r_hash,
            "train_accuracy": 0.85,
            "weights": [0.1] * len(_core._TASTE_FEATURE_NAMES),
            "feature_names": list(_core._TASTE_FEATURE_NAMES),
        }
        _core._save_json_atomic(_taste_file, _taste_data)

        # Monkeypatch paths in core
        _orig_cache = dict(_core._CACHE)
        try:
            _core.set_ratings_path(_ratings_file)
            _core.set_taste_model_path(_taste_file)
            _core._CACHE.clear()

            # Synced model: not stale
            _m = _core._taste_model()
            assert not _m.get("stale")
            _summary = _core._taste_model_summary()
            assert _summary["status"] == "fully active"
            assert "stale" not in _summary

            # Simulate desync (crash after modifying ratings.json without retraining taste model)
            _sample_ratings["ratings"].append({
                "id": 3,
                "phrase_id": "test_lead_03",
                "rating": 1,
                "label": "gold",
                "comment": "newly added",
            })
            _core._save_json_atomic(_ratings_file, _sample_ratings)
            _core._CACHE.clear()

            # Now loading taste model must detect stale hash mismatch
            _m_stale = _core._taste_model()
            assert _m_stale.get("stale") is True
            assert "stale model, retrain needed" in _m_stale.get("stale_reason", "")
            # Taste weight must drop to 0.0 to prevent stale steering
            assert _core._taste_weight() == 0.0
            # Taste score must return 0.0 when stale
            assert _core._taste_score([{"pitch": 60, "start": 0.0, "duration": 1.0, "velocity": 90}], role="lead") == 0.0
            # Taste summary must report status
            _stale_summary = _core._taste_model_summary()
            assert _stale_summary["status"] == "stale model, retrain needed"
            assert _stale_summary.get("stale") is True
        finally:
            _core.set_ratings_path(None)
            _core.set_taste_model_path(None)
            _core._CACHE.clear()
            _core._CACHE.update(_orig_cache)

    print("  atomic write rollback, schema validation, and stale taste model detection verified")
    print("  OK\n")

    # 88. Batch 1 hygiene (2026-09-02): ears retrieval crash, moment pass-through,
    #     tempo-world edges, stamped composed writes, session_report advice
    print("--- hygiene: find_phrases ears + prefer_liked; moment on gear 2; "
          "tempo edges; write_composed_to_jam; session_report advice ---")
    import inspect as _inspect88
    import os as _os88
    import tempfile as _tempfile88
    import develop_session as _ds88
    import gears as _gears88
    import ledger as _ledger88
    import moment as _moment88
    from mcp_server import live_write as _lw88

    # (a) the MCP wrapper forwards prefer_liked to the ears path — this used to
    #     raise TypeError on every use_ears=True call
    assert "prefer_liked" in _inspect88.signature(_core.find_phrases_with_ears).parameters, \
        "find_phrases_with_ears must accept prefer_liked (the MCP wrapper forwards it)"
    _fp88 = getattr(find_phrases, "fn", find_phrases)
    _open88 = _moment88.build_moment([], 150.0)   # tempo 'other', no key, no roles: every gate no-op
    _out88 = _fp88(kind="harmonic", k=4, use_ears=True, prefer_liked=True, moment=_open88)
    assert isinstance(_out88, dict) and _out88.get("ears") == "live", _out88.get("ears")
    assert (_out88["gate_report"]["pool"] == _out88["gate_report"]["survived"]
            == len(_out88["phrases"]) == 4), _out88["gate_report"]
    _blind88 = _fp88(kind="harmonic", k=3, use_ears=True, prefer_liked=True,
                     moment=_moment88.blind_moment("selftest"))
    assert _blind88.get("ears") == "blind" and len(_blind88["phrases"]) == 3
    assert _core.find_phrases is not None and _core._retrieve_phrases is not None
    print("  ears path accepts prefer_liked; one retrieval body; blind still serves")

    # (b) the injected room rides the default gear-2 path and the sticky path
    _aml88 = getattr(answer_my_loop, "fn", answer_my_loop)
    _seed88 = [{"pitch": 60 + i, "start": float(i), "duration": 0.9, "velocity": 90}
               for i in range(4)]
    _w288 = _aml88(seed_notes=_seed88, gear=2, use_ears=True, moment=_open88)
    assert _w288.get("ears") == "live" and str(_w288.get("ears_status", "")).startswith("150"), \
        "gear 2 must serve the moment it was handed, not re-read Live"
    _cap88: dict = {}
    _real_dml88, _real_load88 = _core.develop_my_loop, _ds88.load_state

    def _cap_dml88(**kw):
        _cap88.update(kw)
        return {"answer_notes": [], "source_phrase_ids": [kw.get("pin_source")], "tempo": 120.0}

    def _sticky_load88(path=None):
        return {"sticky": True, "hold": {"source_phrase_id": "pG|c0|w0", "seed_notes": _seed88,
                                         "key": "D minor", "pass": 2, "move_cursor": 2}}
    try:
        _ds88.load_state = _sticky_load88
        _core.develop_my_loop = _cap_dml88
        _aml88(seed_notes=_seed88, gear=2, use_ears=True, moment=_open88)
        assert _cap88.get("moment") is _open88, "sticky path must forward the injected moment"
    finally:
        _ds88.load_state, _core.develop_my_loop = _real_load88, _real_dml88
    print("  moment reaches gear 2 and the sticky develop path")

    # (c) tempo worlds are half-open and 92-100 belongs to his slow world
    for _t88, _want88 in ((75, "downtempo"), (96, "downtempo"), (99.99, "downtempo"),
                          (100, "club"), (140.5, "club"), (141, "other"), (59.9, "other")):
        assert _moment88.tempo_world(_t88) == _want88, (_t88, _moment88.tempo_world(_t88))
    print("  tempo worlds: 60-100 downtempo, 100-141 club, half-open")

    # (d) write_composed_to_jam: critics gate, preview, stamped write, unique names
    _wcj88 = getattr(write_composed_to_jam, "fn", write_composed_to_jam)
    _good88 = [{"pitch": 62 + [0, 3, 5, 7][i % 4], "start": float(i), "duration": 0.9,
                "velocity": 80 + 5 * i} for i in range(8)]
    _bad88 = [{"pitch": 100 + i % 3, "start": float(i), "duration": 0.9, "velocity": 90 + i}
              for i in range(8)]                                   # register ~38 semitones off
    _c588 = _gears88.answer_with_gear(_good88, gear=5, key="D minor")
    _c588["validation"]["min_style_score"] = 0.0   # ignored since F1: the server owns the floor
    _tmp88 = _tempfile88.mkdtemp()
    _lp88 = _os88.path.join(_tmp88, "ledger.json")
    _old_lp88 = _ledger88.LEDGER_PATH
    _orig_w88 = _lw88.write_answer_to_jam_track
    _calls88: list[str] = []

    async def _fake_w88(notes, *, clip_name, tempo):
        _calls88.append(clip_name)
        return {"slot": "3/7", "track": "SIDEKICK JAM", "verified_ok": True}
    try:
        _ledger88.LEDGER_PATH = _lp88
        _lw88.write_answer_to_jam_track = _fake_w88
        _r88 = _wcj88(notes=_bad88, contract=_c588, name="try", confirm=True)
        assert _r88["passes"] is False and _r88["wrote"] is False and _calls88 == [] \
            and _ledger88.summary(path=_lp88)["entries"] == 0, "a failing contract never writes"
        _r88 = _wcj88(notes=_good88, contract=_c588, name="try", confirm=False)
        assert _r88["passes"] is True and _r88["wrote"] is False and "would_write" in _r88 \
            and _calls88 == [], "confirm=False previews only"
        _r88 = _wcj88(notes=_good88, contract=_c588, name="try", ears="live", confirm=True)
        assert _r88["wrote"] is True and _calls88 == ["try"]
        _e88 = _ledger88.load(_lp88)["entries"][-1]
        assert (_e88["gear"] == 5 and _e88["role"] == "lead" and _e88["ears"] == "live"
                and _e88["slot"] == "t3/s7" and _e88["clip_name"] == "try"), _e88
        _r88 = _wcj88(notes=_good88, contract=_c588, name="try", confirm=True)
        assert _r88["clip_name"] == "try #2" and _r88.get("renamed_from") == "try", \
            "the sweep matches by name, so a repeated name gets a suffix"
        _c688 = _gears88.answer_with_gear(_good88, gear=6, key="D minor")
        _c688["validation"]["min_style_score"] = 0.0   # ignored since F1
        _wcj88(notes=_good88, contract=_c688, name="cover", role="bass", confirm=True)
        _e88 = _ledger88.load(_lp88)["entries"][-1]
        assert _e88["gear"] == 6 and _e88["role"] == "bass", _e88
        assert _gears88.validate_composed(_good88, _c588, role="bass")["role"] == "bass"
        assert _gears88.validate_composed(_good88, _c588)["role"] == "lead"
        _ans88 = _aml88(seed_notes=_good88, gear=5, key="D minor", write=True)
        assert "write_via" in _ans88 and "live_write_skipped" in _ans88 \
            and "live_write" not in _ans88, "gear 5 with write=True says where the write goes"
        try:
            _wcj88(notes=_good88, contract=_c588, name="x", source="bogus", confirm=False)
        except ValueError:
            pass
        else:
            raise AssertionError("source must be catalogue or ref:<id>")
    finally:
        _lw88.write_answer_to_jam_track = _orig_w88
        _ledger88.LEDGER_PATH = _old_lp88
    print("  composed writes: gated, previewed, stamped with gear/role/ears, unique names")

    # (e) session_report advice: unswept nag and verified identity coverage
    _lp88b = _os88.path.join(_tmp88, "ledger_b.json")
    _s88 = _ledger88.summary(path=_lp88b)
    assert _s88["advice"] == [] and _s88["open_session_awaiting_sweep"] == 0 \
        and _s88["duplicate_clip_names"] == []
    _set88 = {"set_id": "selftest-set-88", "runtime_epoch": "selftest-epoch-88",
              "file_path": None}
    _refs88 = {
        "31/0": {"clip_id": "clip-end-88-a", "track_id": "jam-track-88",
                 "track": "SIDEKICK JAM", "surface": "session", "path": "session:31/0"},
        "31/1": {"clip_id": "clip-end-88-b", "track_id": "jam-track-88",
                 "track": "SIDEKICK JAM", "surface": "session", "path": "session:31/1"},
        "t31/s2": {"clip_id": "clip-odd-88", "track_id": "jam-track-88",
                   "track": "SIDEKICK JAM", "surface": "session", "path": "session:t31/s2"},
    }
    _ledger88.record_write(clip_name="END decay", slot="31/0", source="catalogue",
                           set_identity=_set88, clip_ref=_refs88["31/0"], path=_lp88b)
    _ledger88.record_write(clip_name="END decay", slot="31/1", source="catalogue",
                           set_identity=_set88, clip_ref=_refs88["31/1"], path=_lp88b)
    _ledger88.record_write(clip_name="odd", slot="t31/s2", source="catalogue",
                           set_identity=_set88, clip_ref=_refs88["t31/s2"], path=_lp88b)
    _s88 = _ledger88.summary(path=_lp88b)
    assert _s88["open_session_awaiting_sweep"] == 3
    assert _s88["duplicate_clip_names"] == []
    assert _s88["coverage"]["entries_with_identity"] == 3
    _adv88 = " ".join(_s88["advice"])
    assert "close_session()" in _adv88, _adv88
    _ledger88.close_session(
        {"set_identity": _set88, "complete": True, "errors": [],
         "clips": list(_refs88.values())}, path=_lp88b)
    _s88 = _ledger88.summary(path=_lp88b)
    assert _s88["open_session_awaiting_sweep"] == 0 \
        and not any("close_session()" in a for a in _s88["advice"])
    print("  session_report nags until the verified sweep; names/slots are not keys")
    print("  OK\n")

    # 89. Batch 2 room fit (2026-09-03): bass answers, relax-gate, Live's scale
    #     as the room's key, arrangement-anchored scan
    print("--- room fit: bass answers; relax gate; Live scale is the key; "
          "arrangement scan ---")
    import asyncio as _asyncio89
    import ears as _ears89
    import gears as _gears89
    import moment as _moment89
    import pp_session as _pps89
    from mcp_server import live_write as _lw89

    # a room where every band is owned by a non-lead role: bass low, pad mid,
    # hats high; tempo 150 = "other" so the tempo gate is a no-op
    _pad89 = [{"pitch": (62, 65, 69)[i % 3], "start": float(i), "duration": 1.0, "velocity": 70}
              for i in range(8)]
    _room89 = _moment89.build_moment([
        {"name": "Bass", "notes": [{"pitch": 38, "start": float(i), "duration": 0.9, "velocity": 90}
                                   for i in range(4)]},
        {"name": "Pad", "notes": _pad89},
        {"name": "Hats", "notes": [{"pitch": 90, "start": i * 0.5, "duration": 0.1, "velocity": 60}
                                   for i in range(8)]},
    ], tempo=150.0)
    assert (_room89["roles"]["bass"]["register_band"] == "low"
            and _room89["roles"]["harmony"]["register_band"] == "mid"
            and _room89["roles"]["drum"]["register_band"] == "high"), _room89["roles"]
    _seed89 = [{"pitch": p, "start": float(i), "duration": 0.9, "velocity": 96}
               for i, p in enumerate((38, 41, 45, 38))]                   # D2 F2 A2 D2

    # (a) a bass riff is answered as a bass line
    _lead89 = _core.answer_my_loop(_seed89, key="D minor", use_ears=True, moment=_room89)
    assert _lead89.get("no_fit") is True, "a lead answer realized at the bass register is refused"
    assert _lead89["gate_report"]["nearest_relaxation"]["gate"] == "register"
    _bass89 = _core.answer_my_loop(_seed89, key="D minor", role="bass", use_ears=True,
                                   moment=_room89)
    assert (not _bass89.get("no_fit") and _bass89["answer_notes"]
            and _bass89["ears"] == "live" and _bass89["role"] == "bass"), _bass89.get("gate_report")
    assert _moment89.register_band(_core._note_dicts(_bass89["answer_notes"])) == "low"
    _src89 = next(p for p in _core._phrases() if p["id"] == _bass89["source_phrase_ids"][0])
    assert _src89["role"] == "bass", "the bass bank serves his bass phrases"
    _off89 = _core.answer_my_loop(_seed89, key="D minor", role="bass", use_ears=False)
    assert "ears" not in _off89 and _off89["role"] == "bass"
    try:
        _core.answer_my_loop(_seed89, role="pad")
    except ValueError:
        pass
    else:
        raise AssertionError("an answer is a line — pad/drum roles must refuse")
    _aml89 = getattr(answer_my_loop, "fn", answer_my_loop)
    _w89 = _aml89(seed_notes=_seed89, key="D minor", role="bass", gear=2, use_ears=True,
                  moment=_room89)
    assert _w89["role"] == "bass" and not _w89.get("no_fit")
    _g389 = _gears89.answer_with_gear(_seed89, gear=3, key="D minor", surgery="broken_rhythm",
                                      role="bass", use_ears=True, moment=_room89)
    assert _g389["ears"] == "live" and not _g389.get("no_fit"), _g389.get("gate_report")
    assert _gears89.answer_with_gear(_seed89, gear=5, key="D minor", role="bass")["role"] == "bass"
    print("  role='bass': his bass phrases, the bass register, the room's bass is the same voice")

    # (b) the relaxation offer can be accepted
    _rx89 = _core.answer_my_loop(_seed89, key="D minor", use_ears=True, moment=_room89,
                                 relax="register")
    assert (not _rx89.get("no_fit") and _rx89["answer_notes"]
            and _rx89["gate_report"]["relaxed"] == "register"), _rx89.get("gate_report")
    assert any("register gate relaxed by caller" in a for a in _rx89["announcements"])
    assert "relaxed" in _rx89["ears_status"]
    assert any(d.get("gate") == "register" and d.get("relaxed")
               for d in _rx89["gate_report"]["dropped"])
    _ra89 = _core.answer_my_loop(_seed89, key="D minor", use_ears=True, moment=_room89,
                                 relax="auto")
    assert (_ra89["gate_report"]["relaxed"] == "register"
            and _ra89["answer_notes"] == _rx89["answer_notes"]), "auto accepts the nearest offer"
    _ro89 = _core.answer_my_loop(_seed89, key="D minor", use_ears=True,
                                 moment=_moment89.build_moment([], 150.0), relax="auto")
    assert _ro89["gate_report"].get("relaxed") is None \
        and not any("relaxed" in a for a in _ro89.get("announcements", [])), \
        "auto relaxes nothing when the room already admits"
    try:
        _ears89.serve([], _moment89.blind_moment("x"), relax="moist")
    except ValueError as exc:
        assert "moist" in str(exc)
    else:
        raise AssertionError("an unknown gate name must fail loud, even blind")
    try:
        _core.answer_my_loop(_seed89, key="D minor", use_ears=False, relax="register")
    except ValueError:
        pass
    else:
        raise AssertionError("relax without ears has nothing to relax")
    assert _core.find_phrases_with_ears(kind="harmonic", k=4, moment=_room89,
                                        relax="register")["gate_report"]["relaxed"] == "register"
    _fp89 = getattr(find_phrases, "fn", find_phrases)
    assert _fp89(kind="harmonic", k=4, use_ears=True, moment=_room89,
                 relax="register")["gate_report"]["relaxed"] == "register"
    _g3r89 = _aml89(seed_notes=_seed89, key="D minor", gear=3, surgery="broken_rhythm",
                    use_ears=True, moment=_room89, relax="register")
    assert _g3r89["gate_report"]["relaxed"] == "register" and not _g3r89.get("no_fit")
    print("  relax: a named gate or 'auto', always announced; unknown names and ears-off refuse")

    # (c) Live's scale is the room's key
    assert _core._parse_key_string("Db phrygian") == ("C#", "phrygian")
    assert _core._parse_key_string("C Harmonic Minor") == ("C", "harmonic minor")
    assert 1 in _core.diatonic_set(0, "phrygian") and 1 not in _core.diatonic_set(0, "minor")
    assert _core.diatonic_set(0, "bhairav") == {0, 1, 4, 5, 7, 8, 11}
    assert "hirajoshi" not in _core.KNOWN_MODES \
        and _core.diatonic_set(0, "hirajoshi") == _core.diatonic_set(0, "minor")
    _ov89 = _pps89.parse_tool_response(
        '{"result":{"tempo":90,"scale":"C Bhairav","scalePitches":"C,Db,E,F,G,Ab,B",'
        '"tracks":[]},"warnings":[]}')
    assert _pps89.scale_from_overview(_ov89) == ("C bhairav", [0, 1, 4, 5, 7, 8, 11])
    assert _pps89.scale_from_overview(
        _pps89.parse_tool_response('{"result":{"tempo":90,"tracks":[]}}')) == (None, None)
    assert _pps89.scale_from_overview(_pps89.parse_tool_response(
        '{"result":{"scale":"C Bhairav","scalePitches":"C,H,E"}}')) == (None, None), \
        "an unreadable pitch token fails closed"
    assert _lw89.scale_from_overview(_ov89) == ("C bhairav", [0, 1, 4, 5, 7, 8, 11])
    _bassn89 = [{"pitch": p, "start": float(i), "duration": 0.9, "velocity": 90}
                for i, p in enumerate((38, 41, 45, 38))]
    _m189 = _moment89.build_moment([{"name": "Bass", "notes": _bassn89}], 90.0,
                                   scale=("C bhairav", [0, 1, 4, 5, 7, 8, 11]))
    assert _m189["key"] == "C bhairav" and _m189["scale_pcs"] == [0, 1, 4, 5, 7, 8, 11]
    assert _m189["key_estimate"] and _m189["key_estimate"] != _m189["key"]
    assert any("Live's scale setting" in a for a in _m189["assumptions"]) \
        and any("check the Set's scale" in a for a in _m189["assumptions"]), _m189["assumptions"]
    _m089 = _moment89.build_moment([{"name": "Bass", "notes": _bassn89}], 90.0)
    _m0b89 = _moment89.build_moment([{"name": "Bass", "notes": _bassn89}], 90.0)
    assert _m089["key"] == _m089["key_estimate"] and _m089["scale_pcs"] is None
    assert _m089["digest"] == _m0b89["digest"] != _m189["digest"], \
        "rooms without a Live scale keep their digests; a scale changes them"
    _mC89 = _moment89.build_moment([{"name": "Bass", "notes": _bassn89}], 90.0,
                                   scale=("C major", [0, 2, 4, 5, 7, 9, 11]))
    assert any("default" in a for a in _mC89["assumptions"]), "C Major may be Live's untouched default"
    assert _moment89.blind_moment("x")["scale_pcs"] is None
    _phr89 = _moment89.build_moment([{"name": "Bass", "notes": _bassn89}], 150.0,
                                    scale=("C phrygian", [0, 1, 3, 5, 7, 8, 10]))
    _fit89 = {"id": "fit", "key": "F# minor", "role": "lead",
              "notes": [{"pitch": p, "start": float(i), "duration": 0.5, "velocity": 80}
                        for i, p in enumerate((60, 61, 63, 65, 67, 68, 70, 72))]}
    _clash89 = {"id": "clash", "key": "F# minor", "role": "lead",
                "notes": [{"pitch": p, "start": float(i), "duration": 0.5, "velocity": 80}
                          for i, p in enumerate((66, 70, 73, 66, 70, 73, 66, 70))]}
    _surv89, _rep89 = _ears89.admit([_fit89, _clash89], _phr89, role="lead")
    assert [c["id"] for c in _surv89] == ["fit"], _rep89
    _db89 = [{"pitch": p, "start": float(i), "duration": 0.9, "velocity": 70 + 5 * i}
             for i, p in enumerate((61, 60, 61, 63, 61, 67))]
    _cphr89 = {"gear": 5, "key": "C phrygian", "bars": 2,
               "register": {"median": 61, "max_drift_semitones": 14}, "tempo": 90.0,
               "validation": {"min_style_score": 0.0, "min_in_key_frac": 0.75,
                              "min_distinct_velocities": 3},
               "scale_pcs": [0, 1, 3, 5, 7, 8, 10]}
    assert _gears89.validate_composed(_db89, _cphr89)["checks"]["in_key"] is True
    _cmin89 = dict(_cphr89)
    _cmin89["key"] = "C minor"
    _cmin89.pop("scale_pcs")
    assert _gears89.validate_composed(_db89, _cmin89)["checks"]["in_key"] is False
    _c589 = _gears89.answer_with_gear(
        _bassn89 + [{"pitch": 41, "start": 4.0, "duration": 0.9, "velocity": 90}],
        gear=5, moment=_phr89)
    assert _c589["key"] == "C phrygian" and _c589["scale_pcs"] == [0, 1, 3, 5, 7, 8, 10]
    assert (_core.score_phrase(_db89, key="C phrygian")["scale_conformity"]
            > _core.score_phrase(_db89, key="C minor")["scale_conformity"])
    assert (_core.score_phrase(_db89, key="C minor", scale_pcs=[0, 1, 3, 5, 7, 8, 10])["scale_conformity"]
            == _core.score_phrase(_db89, key="C phrygian")["scale_conformity"])
    print("  Live's scale wins the key, the estimate rides beside it, the critics honour the pcs")

    # (d) the room where he is: arrangement clips at a bar
    _clips89 = [{"id": "287", "arrangementStart": "1|1", "arrangementLength": "16bar"},
                {"id": "398", "arrangementStart": "17|1", "arrangementLength": "16bar"},
                {"id": "1171", "arrangementStart": "93|1", "arrangementLength": "8bar"},
                {"id": "x"}]

    def _sel89(bar):
        return [c["id"] for c in _moment89.select_arrangement_clips(_clips89, bar)]
    assert (_sel89(1) == ["287"] and _sel89(16) == ["287"] and _sel89(17) == ["398"]
            and _sel89(33) == [] and _sel89(100) == ["1171"] and _sel89(101) == [])
    _fixture89 = _MomentFixture(
        {"tempo": 90, "timeSignature": "4/4", "isPlaying": True,
         "tracks": [{"trackIndex": 0, "name": "Bass", "type": "midi"},
                    {"trackIndex": 1, "name": "Field", "type": "audio"}]},
        {"c0": {"id": "c0", "name": "riff", "type": "midi",
                "notes": [{"pitch": 38, "start": float(i), "duration": 1.0,
                           "velocity": 90} for i in range(4)]}},
        {0: [{"id": "c0", "name": "riff", "type": "midi", "arrangementStart": "17|1", "arrangementLength": "16bar"}],
         1: [{"id": "c1", "name": "field", "type": "audio", "arrangementStart": "17|1", "arrangementLength": "16bar"}]})
    _ma89 = _asyncio89.run(_moment89.read_moment_live(where="arrangement", bar=17, session=_fixture89))
    assert _ma89["context"]["bar"] == 17 and _ma89["context"]["beats_per_bar"] == 4
    assert "bass" in _ma89["roles"] and len(_ma89["unheard"]) == 1
    assert any("arrangement clips at bar 17" in a for a in _ma89["assumptions"])
    for _where89 in ("arrangement", "nope"):
        try:
            _asyncio89.run(_moment89.read_moment_live(where=_where89, session=_fixture89))
        except ValueError:
            pass
        else:
            raise AssertionError("unknown context or unavailable playhead must refuse")
    _bm89 = _moment89.get_moment(where="arrangement", session=_fixture89)
    assert _bm89["ears"] == "blind" and "bar" in _bm89["reason"]
    _htr89 = getattr(hear_the_room, "fn", hear_the_room)
    _cap89: dict = {}
    _prev_gm89, _prev_sel89 = _moment89.get_moment, _lw89.selected_arrangement_bar
    try:
        _moment89.get_moment = lambda where="session", bar=None: (
            _cap89.update(where=where, bar=bar) or _moment89.blind_moment("t"))

        async def _sel_bar89():
            return 77
        _lw89.selected_arrangement_bar = _sel_bar89
        _htr89(where="arrangement")
        assert _cap89 == {"where": "arrangement", "bar": None}, "the room reader owns transport position"
    finally:
        _moment89.get_moment, _lw89.selected_arrangement_bar = _prev_gm89, _prev_sel89
    print("  arrangement reads explicit bars or verified transport position")
    print("  OK\n")

    # 90. Batch 3 structure findings (2026-09-03): hidden notes past the loop
    #     brace on every clip read; whats_missing on the open Set
    print("--- structure: hidden notes behind the loop brace; whats_missing('live') ---")
    import asyncio as _asyncio90
    import json as _json90
    from contextlib import asynccontextmanager as _acm90
    import finisher as _finisher90
    import moment as _moment90
    import pp_codec as _ppc90
    import pp_session as _pps90
    from mcp_server import live_write as _lw90
    from parse_als import from_dict as _from_dict90

    # (a) pure splitter
    assert _pps90.clip_timing_beats({"timeSignature": "3/4", "start": "1|1", "end": "3|1"})["loop_end_beats"] == 6.0
    assert _pps90.split_audible_notes({}, [{"pitch": 60, "start": 20.0, "duration": 1, "velocity": 80}])["n_hidden"] == 0, \
        "no timing → no claim"
    assert _ppc90.pp_duration_to_beats("1bar+n/4") == 5.0 and _ppc90.pp_duration_to_beats("2bar+n3/8") == 9.5
    _clip90 = {"id": "c1", "type": "midi", "name": "Loop", "path": "t0/s0", "looping": True,
               "start": "1|1", "end": "5|1", "length": "4bar", "timeSignature": "4/4",
               "notes": [{"p": 60 + i % 5, "t": 2 * i, "d": 0.5, "v": 70 + i} for i in range(15)]}
    _calls90: list = []

    class _FakePP90:
        def __init__(self, text_mode=False):
            self.text_mode = text_mode

        async def call(self, name, args=None):
            _pp_session._selftest_schema_check(name, args)
            _calls90.append((name, args))
            if name == "ppal-read-clip":
                if self.text_mode:
                    return 'clip: { id:"c1", type:midi, notes: "v90 n/2 C2 1|1", noteCount:1 }'
                return _pps90.parse_tool_response(_json90.dumps({"result": _clip90}))
            if name == "ppal-select":
                return _pps90.parse_tool_response(_json90.dumps({"result": {
                    "selectedClip": {"id": "c1"}, "selectedTrack": {"trackIndex": 0},
                    "selectedScene": {"sceneIndex": 0}}}))
            raise AssertionError(name)

    def _session_factory90(fake):
        @_acm90
        async def _fake_session(url):
            yield fake
        return _fake_session
    _prev_os90 = _pps90.operation_session
    try:
        _pps90.operation_session = _session_factory90(_FakePP90())
        _r90 = _asyncio90.run(_lw90.read_clip(0, 0))
        assert (_r90["n_notes"] == 8 and _r90["n_hidden"] == 7
                and _r90["hidden_span_bars"] == [5, 8]), {k: _r90.get(k) for k in ("n_notes", "n_hidden", "hidden_span_bars")}
        assert _r90["loop_start_beats"] == 0.0 and _r90["loop_end_beats"] == 16.0 and _r90["presence"] == "heard"
        assert len(_r90["notes"]) == 8 and all(n["start"] >= 16 for n in _core._note_dicts(_r90["hidden_notes"]))
        assert "timing" in _calls90[0][1]["include"], "clip reads must ask for the loop brace"
        _r290 = _asyncio90.run(_lw90.read_current_clip())
        assert _r290["n_hidden"] == 7 and _r290["n_notes"] == 8 and _r290["loop_end_beats"] == 16.0
        _pps90.operation_session = _session_factory90(_FakePP90(text_mode=True))
        _old_mode90 = _pps90.LEGACY_TEXT_RESPONSE_MODE
        _pps90.LEGACY_TEXT_RESPONSE_MODE = True
        try:
            _rl90 = _asyncio90.run(_lw90.read_clip(0, 0))
        finally:
            _pps90.LEGACY_TEXT_RESPONSE_MODE = _old_mode90
        assert _rl90["n_notes"] == 1 and _rl90["n_hidden"] == 0, "legacy text: unchanged, no claim"
    finally:
        _pps90.operation_session = _prev_os90

    _fixture90 = _MomentFixture(
        {"tempo": 90, "timeSignature": "4/4", "isPlaying": True,
         "tracks": [{"trackIndex": 0, "name": "Lead", "type": "midi"}]},
        {"0/0": _clip90})
    _m90 = _asyncio90.run(_moment90.read_moment_live(session=_fixture90))
    assert len(_m90["hidden"]) == 1 and _m90["hidden"][0]["n_hidden"] == 7 \
        and _m90["hidden"][0]["track"] == "Lead", _m90.get("hidden")
    assert any("past the loop brace (bars 5–8)" in a for a in _m90["assumptions"])
    assert _m90["roles"]["lead"]["n_notes"] == 8, "the room counts what plays"
    assert _m90["digest"] == _moment90.build_moment(
        [{"name": "Lead", "notes": _r90["notes"]}], 90.0, context=_m90["context"])["digest"], \
        "hidden cards never enter the digest"
    assert _moment90.build_moment([], 90.0)["hidden"] == [] and _moment90.blind_moment("x")["hidden"] == []
    print("  notes past the loop brace are split out, announced, and kept out of density/key")

    # (b) the two new detectors on pure fixtures
    def _clipd90(track, start, end, count, pitches, drum=False, source="arrangement"):
        notes = [{"pitch": p, "start": i * 0.5, "duration": 0.4, "velocity": 70 + i, "enabled": True}
                 for i, p in enumerate(pitches)]
        return {"name": track, "track": track, "start": start, "end": end, "note_count": count,
                "is_drum": drum, "clip_source": source, "notes": notes}

    def _proj90(name, clips, tracks):
        return {"path": name, "tempo": 120.0, "track_count": len(tracks), "midi_track_names": tracks,
                "audio_track_names": [], "clip_count": len(clips),
                "total_notes": sum(c["note_count"] for c in clips), "audio_clip_count": 0, "clips": clips}
    _gap90 = _proj90("gap.als", [_clipd90("lead", 0.0, 64.0, 5, [62, 65, 69, 67, 65]),
                                 _clipd90("lead", 128.0, 192.0, 5, [62, 65, 69, 67, 65])], ["lead"])
    _sg90 = [f for f in _finisher90.whats_missing(project_dict=_gap90)["findings"] if f["detector"] == "silent_gap"]
    assert _sg90 and _sg90[0]["bars"] == [17, 32], _sg90
    _uni90 = _proj90("uni.als", [_clipd90(t, 0.0, 32.0, 5, [62, 65, 69, 67, 65]) for t in ("a", "b", "c")]
                     + [_clipd90("d", 0.0, 32.0, 5, [60, 64, 67, 72, 64])], ["a", "b", "c", "d"])
    _du90 = _finisher90.whats_missing(project_dict=_uni90)
    _uw90 = [f for f in _du90["findings"] if f["detector"] == "unison_wall"]
    assert _uw90 and _uw90[0]["bars"] == [1, 8] and _uw90[0]["tracks"] == ["a", "b", "c"], _uw90
    assert all(p["verb"] in _finisher90.VERB_MENU and p["intervention"] in _finisher90.INTERVENTIONS
               for p in _du90["proposals"])
    _healthy90 = _proj90("healthy.als", [_clipd90("pad", 0.0, 240.0, 60, [48, 55, 52]),
                                         _clipd90("lead", 32.0, 160.0, 128, [62, 65, 69, 67, 65]),
                                         _clipd90("lead", 176.0, 208.0, 32, [62, 65, 69, 67, 65]),
                                         _clipd90("drums", 96.0, 160.0, 128, [36, 38, 42], drum=True)],
                         ["pad", "lead", "drums"])
    _hh90 = {f["detector"] for f in _finisher90.whats_missing(project_dict=_healthy90)["findings"]}
    assert not ({"silent_gap", "unison_wall"} & _hh90), _hh90
    print("  silent_gap names the hole; unison_wall names the doublers; a healthy map fires neither")

    # (c) the open Set as a project
    _live90 = {"set_name": "Tonight", "tempo": 90.0, "assumptions": [], "tracks": [
        {"track_index": 0, "name": "Bass", "type": "midi", "instrument": "Operator", "take_lanes": [],
         "arrangement_clips": [
             {"id": "a1", "name": "riff", "arrangementStart": "1|1", "arrangementLength": "16bar",
              "start": "1|1", "end": "5|1", "length": "4bar", "looping": True, "timeSignature": "4/4",
              "notes": [{"p": 38, "t": 0, "d": 1, "v": 90}, {"p": 41, "t": 4, "d": 1, "v": 92},
                        {"p": 38, "t": 20, "d": 1, "v": 90}]},
             {"id": "a2", "name": "riff2", "arrangementStart": "33|1", "arrangementLength": "16bar",
              "start": "1|1", "end": "5|1", "length": "4bar",
              "notes": [{"p": 38, "t": 0, "d": 1, "v": 90}, {"p": 41, "t": 2, "d": 1, "v": 92}]}]},
        {"track_index": 1, "name": "Kit", "type": "midi", "instrument": "Drum Rack",
         "arrangement_clips": [{"id": "k1", "name": "beat", "arrangementStart": "1|1",
                                "arrangementLength": "16bar",
                                "notes": [{"p": 36, "t": 0, "d": 0.5, "v": 120},
                                          {"p": 38, "t": 2, "d": 0.5, "v": 110}]}],
         "take_lanes": [{"takeLane": 1, "name": "Lane", "clips": [
             {"id": "k9", "name": "beat alt", "arrangementStart": "1|1", "arrangementLength": "4bar",
              "notes": [{"p": 36, "t": 0, "d": 0.5, "v": 100}]}]}]},
        {"track_index": 2, "name": "GTR", "type": "audio"}]}
    _pd90 = _finisher90.project_from_live_tracks(_live90)
    _from_dict90(_pd90)   # round-trips through parse_als
    assert _pd90["path"] == "live:Tonight" and _pd90["audio_track_names"] == ["GTR"] \
        and _pd90["midi_track_names"] == ["Bass", "Kit"]
    assert {c["clip_source"] for c in _pd90["clips"]} == {"arrangement", "take_lane"}
    assert all(c["is_drum"] for c in _pd90["clips"] if c["track"] == "Kit") \
        and not any(c["is_drum"] for c in _pd90["clips"] if c["track"] == "Bass")
    _a190 = next(c for c in _pd90["clips"] if c["name"] == "riff")
    assert _a190["note_count"] == 2 and _pd90["skip_detail"]["hidden_past_loop"] == 1 \
        and _pd90["notes_skipped"] == 1, "only the notes inside the brace count"
    assert _a190["start"] == 0.0 and _a190["end"] == 64.0 and _a190["notes"][0]["enabled"] is True
    _diag90 = _finisher90.whats_missing(project_dict=_pd90)
    _hit90 = {f["detector"] for f in _diag90["findings"]}
    assert {"silent_gap", "comping_debris"} <= _hit90, _hit90
    assert _diag90["frame"]["audio_tracks"] == 1 and "assumptions" in _diag90
    _called90: list = []

    async def _fake_tracks90(max_tracks=None):
        _called90.append(max_tracks)
        return _live90
    _prev_rt90 = _lw90.read_arrangement_tracks
    _wm90 = getattr(whats_missing, "fn", whats_missing)
    try:
        _lw90.read_arrangement_tracks = _fake_tracks90
        _d190, _d290 = _wm90("live"), _wm90("LIVE")
        assert {f["detector"] for f in _d190["findings"]} >= {"silent_gap", "comping_debris"} \
            and _d190 == _d290 and _called90 == [None, None]
    finally:
        _lw90.read_arrangement_tracks = _prev_rt90
    try:
        _wm90("no-such-project-xyz")
    except ValueError:
        pass
    else:
        raise AssertionError("the catalog path must still refuse an unknown name")
    _pp_calls90: list = []

    class _FakePPTracks90:
        async def call(self, name, args=None):
            _pp_session._selftest_schema_check(name, args)
            _pp_calls90.append((name, args))
            if name == "ppal-read-live-set":
                return _pps90.parse_tool_response(_json90.dumps({"result": {
                    "name": "Tonight", "tempo": 90, "regularTrackCount": 3, "tracks": [
                        {"trackIndex": 0, "name": "Bass", "type": "midi", "instrument": "Operator"},
                        {"trackIndex": 1, "name": "Kit", "type": "midi", "instrument": "Drum Rack"},
                        {"trackIndex": 2, "name": "GTR", "type": "audio"},
                        {"trackIndex": 3, "name": "Grp", "type": "audio", "isGroup": True}]}}))
            if name == "ppal-read-track":
                t = _live90["tracks"][args["trackIndex"]]
                return _pps90.parse_tool_response(_json90.dumps({"result": {
                    "id": str(args["trackIndex"]), "type": "midi", "name": t["name"],
                    "instrument": t["instrument"], "arrangementClips": t["arrangement_clips"],
                    "takeLanes": t["take_lanes"]}}))
            if name == "ppal-live-api":
                # captured shape: read/live_api_live_set_properties.json
                return _pps90.parse_tool_response(_json90.dumps({
                    "id": "1", "path": "live_set", "results": [
                        {"operation": {"property": "file_path", "type": "getProperty"},
                         "result": "/tmp/Tonight Project/Tonight.als"}]}))
            raise AssertionError(name)
    try:
        _pps90.operation_session = _session_factory90(_FakePPTracks90())
        _out90 = _asyncio90.run(_lw90.read_arrangement_tracks())
    finally:
        _pps90.operation_session = _prev_os90
    assert _out90["set_name"] == "Tonight" and _out90["tempo"] == 90.0 \
        and [t["name"] for t in _out90["tracks"]] == ["Bass", "Kit", "GTR"], _out90["tracks"]
    _reads90 = [a for n, a in _pp_calls90 if n == "ppal-read-track"]
    assert [a["trackIndex"] for a in _reads90] == [0, 1] \
        and all("timing" in a["include"] and "notes" in a["include"] for a in _reads90), \
        "audio tracks are listed, never read; MIDI reads ask for notes + timing"
    assert _out90["tracks"][1]["take_lanes"][0]["clips"][0]["id"] == "k9"
    _pd290 = _finisher90.project_from_live_tracks(_out90)
    assert _finisher90.whats_missing(project_dict=_pd290) == _finisher90.whats_missing(project_dict=_pd290)
    print("  whats_missing('live'): the open Set read like a catalog project, deterministic")
    print("  OK\n")

    # 91. R1 (2026-09-03): a reference's BRIEF reaches the composer, on every gear.
    #     Its Do/Don't rules were extracted, stored, and then dropped by _ref_meta,
    #     so the only actionable part of a reference reached nobody.
    print("--- ref brief reaches the composer: prose, honesty, gear split ---")
    import copy as _copy91
    import inspect as _inspect91

    _card91 = {
        "ref_id": "r-brief91",
        "identity": "Industrial doom; mechanical sub-bass drone.",
        "model": "gemini-3.6-flash-high", "created_at": "2026-07-28T18:34:34Z",
        "imitation_do": ["kick on beat 1", "snare strictly on beat 3",
                         "hard-quantize to 16ths", "static C root pedal",
                         "long room tails", "sub-bass saturation",
                         "SEVENTH", "EIGHTH"],
        "imitation_dont": ["add swing", "frequent chord changes", "busy fills",
                           "bright leads", "FIFTH"],
        "describe_excerpt": ("# Identity\nIndustrial doom.\nSource label (for "
                             "Identity context only): https://youtube.com/watch?v=x\n"),
        "ricardo_note": None,
        "structure": {"density": "low", "grid_bpm": 104.0, "feel_bpm": 52.0,
                      "register": "low", "modal_center": "C minor"},
    }
    _seed91 = [{"pitch": 60 + i, "start": float(i), "duration": 0.9, "velocity": 90}
               for i in range(4)]

    # pure, deterministic, non-mutating
    assert _ar80.ref_brief(_card91) == _ar80.ref_brief(_copy91.deepcopy(_card91)), \
        "ref_brief must be pure"
    _before91 = _copy91.deepcopy(_card91)
    _b91 = _ar80.ref_brief(_card91)
    assert _card91 == _before91, "ref_brief must not mutate the card"

    # caps are tighter than apply_audio_ref's: one phrase can't honour eight rules.
    # R6 reframes key/mode commands and timbre rules, so the brief carries the
    # reframed prefix — never the card's verbatim text.
    _exp_do91 = [_ar80._reframe_rule(_rule) for _rule in _card91["imitation_do"][:6]]
    _exp_dont91 = [_ar80._reframe_rule(_rule) for _rule in _card91["imitation_dont"][:4]]
    assert len(_b91["imitation_do"]) <= 6, _b91["imitation_do"]
    assert len(_b91["imitation_dont"]) <= 4, _b91["imitation_dont"]
    assert _b91["imitation_do"] == _exp_do91, _b91["imitation_do"]
    assert _b91["imitation_dont"] == _exp_dont91, _b91["imitation_dont"]
    for _rule91 in _b91["imitation_do"] + _b91["imitation_dont"]:
        assert _ar80._reframe_rule(_rule91) == _rule91, \
            "a brief rule must carry no raw key/mode command: %r" % (_rule91,)

    # honesty: a description of someone else's record, with its provenance
    assert "not a source" in _b91["authority"]
    assert _b91["heard_by"]["model"] == "gemini-3.6-flash-high"
    assert _b91["heard_by"]["contract"] == "audio-ref-card-v1", \
        "a card with no contract_version is v1, computed not stored"
    assert "Source label" not in (_b91["excerpt"] or "") \
        and "http" not in (_b91["excerpt"] or ""), \
        "the injected source-URL line is prompt scaffolding, not something heard"
    assert _ar80.REF_OVERLAY_PREFIX in _ar80.ref_brief(
        dict(_card91, ricardo_note="keep it under 60"))["ricardo_note"], \
        "his overlay must outrank Gemini's words"

    # the guidance literal MOVED to a constant; it was not copied
    _apply_src91 = _inspect91.getsource(_ar80.apply_audio_ref)
    assert "REF_GUIDANCE" in _apply_src91 and "REF_KEEP_DROP" in _apply_src91
    assert "You (Claude) compose with this ref" not in _apply_src91, \
        "one owner for the composer-facing wording, or the two copies drift"

    # core forwards the brief; it never reads the audio-ref layer
    assert "audio_ref" not in _inspect91.getsource(_core._answer_worker), \
        "core must stay ignorant of the reference layer"

    _steer91 = _ar80.ref_steering(_card91)
    assert _steer91["brief"]["imitation_do"] == _exp_do91
    _r91 = _core.answer_my_loop(_seed91, key="C minor", ref=_steer91)
    assert _r91["ref"]["brief"]["imitation_do"] == _exp_do91, \
        "the rules must survive the trip into an actual answer"
    assert _r91["ref"]["steering"] == "gates"

    # every gear carries the brief; none refuses; only gear 2 runs the gates
    _aml91 = getattr(answer_my_loop, "fn", answer_my_loop)
    _orig_card91 = _ar80.get_ref_card
    try:
        _ar80.get_ref_card = lambda rid: _card91
        for _g91 in (1, 2, 3, 4, 5, 6):
            _rg91 = _aml91(seed_notes=_seed91, key="C minor", gear=_g91,
                           ref_id="r-brief91")
            assert "ref" in _rg91, "gear %d dropped the reference entirely" % _g91
            assert _rg91["ref"]["brief"]["imitation_do"], \
                "gear %d carries no Do rules" % _g91
            assert _rg91["ref"]["steering"] == ("gates" if _g91 == 2 else "prose_only"), \
                (_g91, _rg91["ref"].get("steering"))
            if _g91 != 2:
                assert _rg91["ref"]["notes"], \
                    "gear %d must say why the gates did not run" % _g91
                assert "applied" not in _rg91["ref"], \
                    "no gate metadata on a path where no gate ran"
    finally:
        _ar80.get_ref_card = _orig_card91

    # a reference-off answer is untouched
    assert "ref" not in _core.answer_my_loop(_seed91, key="C minor", seed=0)
    print("  brief travels on every gear; gates stay gear 2; core stays ignorant")
    print("  OK\n")

    # 92. Batch A (2026-09-03): a dropped phrase stops coming back.
    #     The answer path ranked on seed cosine alone and never consulted his
    #     ratings, so yesterday's reject was eligible to be today's top answer.
    print("--- rejects: latest-wins fold, id + near-dup cull, fail-open, undo ---")
    from build_index import phrase_vector as _pv92
    _tb92_rejected = _core.rejected_phrases

    def _ph92(pid, pitches, starts, vels, role="lead", project=None):
        _n = [{"pitch": p, "start": s, "duration": 0.45, "velocity": v}
              for p, s, v in zip(pitches, starts, vels)]
        # Synthetic records name their project outright — never parse it out
        # of the id (pids carry no project segment; only legacy positional
        # ids do, and those resolve through the record's project field).
        return {"id": pid, "project": project or pid, "role": role,
                "kind": "harmonic", "clip_source": "session", "tempo": 122.0,
                "n_notes": len(_n), "notes": _n, "vec": _pv92(_n)}

    with tempfile.TemporaryDirectory() as _td92:
        _core.set_ratings_path(os.path.join(_td92, "ratings.json"))
        # A is NEAR the seed, never identical: search(max_sim=0.97) drops an exact
        # copy by design — you don't answer a loop with the loop itself.
        _A92 = _ph92("pA|c0", [62, 65, 69, 67, 65, 64], [0.0, .5, 1.0, 1.5, 2.0, 3.0],
                     [91, 63, 77, 84, 58, 96])
        _B92 = _ph92("pB|c0", [67, 70, 74, 72, 70, 65], [0.0, .5, 1.0, 2.0, 2.5, 3.0],
                     [88, 70, 82, 74, 60, 90])
        _C92 = _ph92("pC|c0", [60, 63, 67, 70, 63, 60], [0.0, .75, 1.5, 2.25, 3.0, 3.5],
                     [70, 80, 75, 85, 65, 92])
        _D92 = _ph92("pD|c0", [64, 67, 71, 69, 67, 64], [0.0, .5, 1.25, 2.0, 2.75, 3.5],
                     [85, 66, 79, 88, 62, 94])
        _E92 = _ph92("pE|c0", [59, 62, 66, 64, 62, 59], [0.0, .6, 1.2, 1.8, 2.4, 3.2],
                     [80, 72, 86, 68, 90, 76])
        _seed92 = [{"pitch": p, "start": s, "duration": 0.45, "velocity": 90}
                   for p, s in zip([62, 65, 69, 67, 65, 62],
                                   [0.0, .5, 1.0, 1.5, 2.0, 3.0])]
        _core.set_phrase_pool_for_test([_A92, _B92, _C92, _D92, _E92])
        try:
            # latest wins: a later yes clears the no, a later no restores it
            _core.rate(-1, phrase_id="pA|c0", label="dropped", notes=_A92["notes"])
            assert "pA|c0" in {r["identity"] for r in _tb92_rejected()}
            _core.rate(1, phrase_id="pA|c0", label="liked", notes=_A92["notes"])
            assert "pA|c0" not in {r["identity"] for r in _tb92_rejected()}, \
                "a later yes must clear the reject"
            _core.rate(-1, phrase_id="pA|c0", label="dropped", notes=_A92["notes"])
            assert "pA|c0" in {r["identity"] for r in _tb92_rejected()}

            # un-golding is NOT rejecting: "not one of my best" != "never again"
            _core.rate(-1, phrase_id="pB|c0", label="ungold", notes=_B92["notes"])
            assert "pB|c0" not in {r["identity"] for r in _tb92_rejected()}

            # the cull is a pure, order-preserving filter that names its tier
            _pool92 = [_A92, _B92, _C92, _D92, _E92]
            _surv92, _sup92 = _core.reject_cull(_pool92)
            assert [p["id"] for p in _surv92] == ["pB|c0", "pC|c0", "pD|c0", "pE|c0"]
            assert _sup92[0]["tier"] == "dropped_id" and _sup92[0]["id"] == "pA|c0"
            assert _core.reject_cull(_pool92) == (_surv92, _sup92), "cull must be pure"

            # the headline: same seed, different answer, and it says why
            _r92 = _core.answer_my_loop(_seed92, key="D minor", seed=0, use_ears=False)
            assert "pA|c0" not in _r92["source_phrase_ids"], \
                "a dropped phrase must not be served again"
            assert _r92["rejects"]["suppressed"] and _r92["bank_stats"]["n_rejected"] >= 1
            assert any("clear_rejects()" in a for a in _r92["assumptions"]), _r92["assumptions"]
            assert "announcements" not in _r92, "ears-off answers carry no announcements"
            assert _r92 == _core.answer_my_loop(_seed92, key="D minor", seed=0,
                                                use_ears=False), "still deterministic"

            # Exhausting the pool refuses rather than restoring rejected material.
            for _p92 in (_B92, _C92, _D92, _E92):
                _core.rate(-1, phrase_id=_p92["id"], label="dropped", notes=_p92["notes"])
            _all92 = _core.answer_my_loop(_seed92, key="D minor", seed=0, use_ears=False)
            assert _all92.get("no_fit") is True and not _all92.get("answer_notes")
            assert _all92["rejects"]["pool_after"] == 0
            assert _all92["rejects"]["pool_before"] > 0

            # undo, append-only: preview then clear
            _cr92 = getattr(clear_rejects, "fn", clear_rejects)
            _n_before92 = _core.get_ratings()["total"]
            assert _cr92(phrase_id="pA|c0", confirm=False)["cleared"] == [], "preview writes nothing"
            assert _cr92(phrase_id="pA|c0", confirm=False)["would_clear"] == ["pA|c0"]
            assert _cr92(phrase_id="pA|c0", confirm=True)["cleared"] == ["pA|c0"]
            assert "pA|c0" not in {r["identity"] for r in _tb92_rejected()}
            assert _core.get_ratings()["total"] == _n_before92 + 1, \
                "undo appends a record; it never edits history"
        finally:
            _core.set_phrase_pool_for_test(None)
            _core.set_ratings_path(None)

    # a bare drop must work: the ledger knows what was written last
    with tempfile.TemporaryDirectory() as _td92b:
        import develop_session as _ds92
        import ears as _ears92
        import ledger as _ledger92
        _olp92 = _ledger92.LEDGER_PATH
        _ledger92.LEDGER_PATH = os.path.join(_td92b, "ledger.json")
        # judge_answer writes ears + sticky-develop state; isolate both
        _oesp92, _odsp92 = _ears92.SESSION_PATH, _ds92.STATE_PATH
        _ears92.SESSION_PATH = os.path.join(_td92b, "ears_session.json")
        _ds92.STATE_PATH = os.path.join(_td92b, "develop_session.json")
        _core.set_ratings_path(os.path.join(_td92b, "ratings.json"))
        try:
            _real92 = _core._phrases()[0]["id"]
            _ledger92.record_write(clip_name="ans 92", slot="31/0", source="catalogue",
                                   role="lead", gear=2, source_phrase_id=_real92)
            _ja92 = getattr(judge_answer, "fn", judge_answer)
            _bare92 = _ja92("drop")          # nothing typed at all
            assert _bare92["verdict"] == "drop"
            assert _bare92["resolved_from"]["clip_name"] == "ans 92"
            assert _bare92["record"]["phrase_id"] == _real92
            assert _real92 in {r["identity"] for r in _tb92_rejected()}
            _sr92 = getattr(session_report, "fn", session_report)()
            assert _sr92["n_rejected"] >= 1
            assert any("clear_reje" in a for a in _sr92["advice"]), _sr92["advice"]
        finally:
            _ledger92.LEDGER_PATH = _olp92
            _ears92.SESSION_PATH = _oesp92
            _ds92.STATE_PATH = _odsp92
            _core.set_ratings_path(None)
    print("  a dropped phrase stops coming back; fails open; undo is append-only")
    print("  OK\n")

    # 93. Batch B (2026-09-03): a bass gold stops being served as a lead, and a
    #     rating round can no longer record a null key.
    print("--- gold role + key provenance: bass keeps leave the lead bank ---")
    import inspect as _inspect93

    with tempfile.TemporaryDirectory() as _td93:
        _core.set_ratings_path(os.path.join(_td93, "ratings.json"))
        try:
            _bass93 = [{"pitch": p, "start": float(i), "duration": 0.9, "velocity": 90 + i}
                       for i, p in enumerate((29, 32, 34, 29, 30, 36))]   # F0..C1
            _lead93 = [{"pitch": p, "start": float(i), "duration": 0.9, "velocity": 88 + i}
                       for i, p in enumerate((62, 65, 69, 67, 65, 62))]
            _legacy93 = [{"pitch": p, "start": float(i), "duration": 0.9, "velocity": 92 + i}
                         for i, p in enumerate((30, 33, 35, 30, 31, 37))]  # sub-C1, no role

            _core.rate(1, label="gold", notes=_bass93, role="bass", key="C minor")
            assert any(g.get("role") == "bass" for g in _core.get_gold_phrases()), \
                "rate() must carry the role onto the record"
            assert not _core._gold_bank_records(role="lead"), \
                "a bass gold must NOT join the lead answer bank (the 2026-09-02 bug)"
            assert _core._gold_bank_records(role="bass"), "…it joins the bass bank"
            assert _core._gold_bank_records(role="bass")[0]["role_source"] == "stored"

            # a pre-role gold infers from register rather than assuming lead
            _core.rate(1, label="gold", notes=_legacy93)
            _inf93 = [r for r in _core._gold_bank_records(role="bass")
                      if r.get("role_source") == "register_inferred"]
            assert _inf93, "a sub-C1 gold with no stored role must infer bass"
            _core.rate(1, label="gold", notes=_lead93)
            assert any(r.get("role_source") == "register_inferred"
                       for r in _core._gold_bank_records(role="lead")), \
                "…and a mid-register one still infers lead"

            # the taste pseudo-pairs read the gold's OWN role, not a hard-coded lead
            assert 'g.get("role")' in _inspect93.getsource(_core._taste_pairs_from_golds), \
                "gold pseudo-pairs must draw negatives from the gold's own role pool"

            # a round can no longer store key: null — the train/serve skew that
            # corrupted scale_fit, the model's largest weight, on all 13 rounds
            _c93 = [{"notes": _lead93}, {"notes": _bass93}]
            try:
                _core.rate_candidates(_c93, best=0, worst=1)
            except ValueError as _e93:
                assert "key" in str(_e93).lower() and "audition_leads" in str(_e93)
            else:
                raise AssertionError("a keyless round must refuse, not store null")
            _auto93 = _core.rate_candidates(_c93, best=0, worst=1, key="auto")
            assert _auto93["key"] and _auto93["key_source"] == "auto", _auto93
            _given93 = _core.rate_candidates(_c93, best=0, worst=1, key="D minor")
            assert _given93["key"] == "D minor" and _given93["key_source"] == "caller"
            _echo93 = _core.rate_candidates(
                [{"notes": _lead93, "key": "E minor"}, {"notes": _bass93, "key": "E minor"}],
                best=0, worst=1)
            assert _echo93["key"] == "E minor" and _echo93["key_source"] == "candidates", \
                "a round with agreeing candidate keys adopts theirs"
        finally:
            _core.set_ratings_path(None)
    print("  role rides the rating; register infers it when absent; no null keys")
    print("  OK\n")

    # 94. R2 (2026-09-03): audio-ref-card-v2 — schema-enforced listening.
    #     v1 asked a model to write markdown and scraped it; the prompt's own
    #     example got copied instead of listened to ("0% swing" 57/59, 13 tracks
    #     reported at the example's 68 BPM), and a model that declined to write
    #     the file failed silently behind a stale card that still said ok.
    print("--- audio-ref-card-v2: closed schema, no few-shot, ownership ---")
    import json as _json94

    # the prompt hands the model no musical content to copy — THE regression guard
    for _w94 in ("68", "sludge", "ostinato", "Aeolian", "0% swing", "snare on 3",
                 "half-time", "hard-quantize", "2-bar", "crash-ride"):
        assert _w94 not in _ar80.DESCRIBE_PROMPT_V2, \
            "the v2 prompt must never hand the model %r to copy" % _w94

    # every object closed, every key required: null is how a model says "unheard"
    def _walk94(node, path="card"):
        if isinstance(node, dict) and node.get("type") == "object":
            assert node.get("additionalProperties") is False, path
            assert set(node.get("required", [])) == set(node.get("properties", {})), path
            for _k, _v in node["properties"].items():
                _walk94(_v, f"{path}.{_k}")
        if isinstance(node, dict) and node.get("type") == "array":
            _walk94(node.get("items", {}), path + "[]")
    _walk94(_ar80.V2_SCHEMA)
    _json94.dumps(_ar80.V2_SCHEMA)

    _card94 = {
        "contract_version": "audio-ref-card-v2",
        "identity": "Drone doom; amplifier sustain wall.",
        "tempo": {"grid_bpm": 82.0, "feel_bpm": 41.0, "confidence": "med"},
        "meter": {"beats_per_bar": 4, "half_time": True},
        "swing": {"pct": 0.0, "confidence": "high"},
        "density": "sparse",
        "register": {"band": "low", "pitch_span": "C1 - G2"},
        "harmony": {"modal_center": "C minor", "pitch_classes": ["C", "D#", "G"],
                    "interval_content": ["m3", "P5"], "harmonic_rhythm_bars": 8.0,
                    "confidence": "med"},
        "melody": {"phrase_length_bars": 2.0, "contour": "static",
                   "call_response": "none", "motif_note_count": 3,
                   "repetition": "verbatim"},
        "drums": {"grid": [{"instrument": "kick", "steps": [0]},
                           {"instrument": "snare", "steps": [8]}], "confidence": "high"},
        "sections": [{"t": "00:15", "label": "sub drone enters", "event": "enter",
                      "density": "sparse", "register": "low", "harmony": "C pedal"},
                     {"t": "04:04", "label": "feedback layer", "event": "texture_change",
                      "density": "medium", "register": "high", "harmony": None}],
        "imitation": {"do": ["sustain the root across 16-bar blocks"],
                      "dont": ["add quantized drum hits"]},
        "uncertain": ["no percussion audible"],
    }
    _ar80.validate_card_v2(_card94)

    # a card that heard little must still validate — honesty is a legal answer
    _blind94 = _json94.loads(_json94.dumps(_card94))
    _blind94["harmony"] = {"modal_center": None, "pitch_classes": [],
                           "interval_content": [], "harmonic_rhythm_bars": None,
                           "confidence": "low"}
    _blind94["register"] = {"band": None, "pitch_span": None}
    _blind94["drums"] = {"grid": [], "confidence": "low"}
    _ar80.validate_card_v2(_blind94)

    def _rejects94(mut, why):
        _bad = _json94.loads(_json94.dumps(_card94))
        mut(_bad)
        try:
            _ar80.validate_card_v2(_bad)
        except ValueError:
            return
        raise AssertionError("v2 validation must reject: " + why)
    _rejects94(lambda c: c.update(surprise=1), "an unknown key")
    _rejects94(lambda c: c.update(density="low"), "a v1 density word")
    _rejects94(lambda c: c["sections"][0].update(t="4:04.5"), "a malformed timestamp")
    _rejects94(lambda c: c["imitation"].update(do=["r%d" % i for i in range(9)]),
               "too many Do rules")
    _rejects94(lambda c: c.pop("melody"), "a missing key")
    _rejects94(lambda c: c.update(contract_version="audio-ref-card-v1"),
               "the wrong contract version")

    # steering is version-blind: a v2 card gates exactly like the v1 it replaces
    _idx94 = _ar80.card_from_v2(_card94, ref_id="r94", out_dir="/tmp/r94")
    _st94 = _ar80.ref_steering(_idx94)
    assert _st94["tempo"] == 82.0 and _st94["register"] == "low"
    assert _st94["mods"] == _ar80.ref_to_mods({"structure": {"density": "low"}})[0]
    assert _st94["contract_version"] == "audio-ref-card-v2"

    # the prose is RENDERED from the data, so the two cannot disagree
    _txt94 = _ar80.render_describe_v2(_card94)
    assert "# Identity" in _txt94 and "Source label" not in _txt94 and "http" not in _txt94
    assert _ar80.parse_describe_structure(_txt94)["modal_center"] == "C minor"

    # a structured grid replaces prose parsing; an empty one invents nothing
    _n94, _ok94 = _ar80.card_grid_to_notes(_card94)
    assert _ok94 and [(x["pitch"], x["start"]) for x in _n94] == [(36, 0.0), (38, 2.0)]
    assert _ar80.card_grid_to_notes(_blind94)[0] == []

    # stdout parsing tolerates the shapes agy emits, and refuses prose
    assert _ar80._agy_json_result(_json94.dumps(_card94))["identity"]
    assert _ar80._agy_json_result("```json\n%s\n```" % _json94.dumps(_card94))["identity"]
    assert _ar80._agy_json_result(_json94.dumps({"result": _json94.dumps(_card94)}))["identity"]
    try:
        _ar80._agy_json_result("Here is the analysis: the track is slow.")
    except ValueError:
        pass
    else:
        raise AssertionError("prose is a failed describe, never a partial success")

    # a slug that already names its effort must not also get --effort
    assert "--effort" not in " ".join(_ar80._agy_cmd_for("gemini-3.8-flash-high", "/tmp", "p"))
    assert "--effort" in " ".join(_ar80._agy_cmd_for("gemini-3.8-flash", "/tmp", "p"))
    assert "--json-schema" in " ".join(
        _ar80._agy_cmd_for("gemini-3.8-flash-high", "/tmp", "p", schema=_ar80.V2_SCHEMA))

    _old94 = os.environ.pop("AUDIO_REF_MODEL", None)
    try:
        assert _ar80._resolve_model("x") == "x"
        os.environ["AUDIO_REF_MODEL"] = "env-model"
        assert _ar80._resolve_model(None) == "env-model", \
            "AUDIO_REF_MODEL is documented in HANDOFF and was never read"
        del os.environ["AUDIO_REF_MODEL"]
        assert _ar80._resolve_model(None) == _ar80.DEFAULT_AGY_MODEL
    finally:
        if _old94:
            os.environ["AUDIO_REF_MODEL"] = _old94

    # HIS OWN record is material he may quote; an influence stays a target
    _mine94 = _json94.loads(_json94.dumps(_card94))
    _mine94["ownership"] = "his"
    _bh94 = _ar80.ref_brief(_ar80.card_from_v2(_mine94, ref_id="r94b", out_dir="/tmp/y"))
    assert "your own" in _bh94["authority"].lower() and "not a source" not in _bh94["authority"]
    assert _ar80.ref_brief(_idx94)["authority"].startswith("A machine's description")
    # the listener must see the AUDIO and nothing else. --add-dir hands over a
    # whole directory, and on a re-listen that directory holds the previous
    # answer; measured 2026-09-03, gemini-3.8-flash-medium returned the old
    # contaminated describe character-for-character instead of listening.
    with tempfile.TemporaryDirectory() as _rd94:
        _wav94 = os.path.join(_rd94, "source.wav")
        # a real (tiny) WAV: staging transcodes to plain PCM through ffmpeg
        import wave as _wave94
        with _wave94.open(_wav94, "wb") as _wf94:
            _wf94.setnchannels(1)
            _wf94.setsampwidth(2)
            _wf94.setframerate(22050)
            _wf94.writeframes(b"\x00\x00" * 2205)
        open(os.path.join(_rd94, "describe.txt"), "w").write("# Identity\nOLD ANSWER\n")
        open(os.path.join(_rd94, "meta.json"), "w").write('{"structure": {"feel_bpm": 68}}')
        with _ar80.staged_audio_only(_wav94) as _staged94:
            _seen94 = sorted(os.listdir(os.path.dirname(_staged94)))
            assert _seen94 == ["source.wav"], \
                "the listener must not see the previous describe/meta: %s" % _seen94
            assert os.path.dirname(_staged94) != _rd94
            # staged = a transcoded plain-PCM copy (fmt tag 1, 16-bit), not the raw file
            with _wave94.open(_staged94, "rb") as _sf94:
                assert _sf94.getsampwidth() == 2 and _sf94.getnframes() > 0
            _tmpdir94 = os.path.dirname(_staged94)
        assert not os.path.exists(_tmpdir94), "the staging dir must be cleaned up"
        assert os.path.isfile(os.path.join(_rd94, "describe.txt")), "…without touching the ref"
    print("  schema closed and enforced; prompt carries no music; own work is quotable")
    print("  listener sees the audio alone — a re-listen cannot copy the old answer")
    print("  OK\n")

    # 95. F1 (2026-09-03): the metric's own guards, after an external audit found
    #     all three broken — two of them by changes made earlier the same day.
    print("--- guards: the sweep nag survives idleness; provenance; server-owned gate ---")
    import copy as _copy95
    import time as _time95
    import ledger as _ledger95

    # (a) THE one that mattered: his 2026-09-02 session sat 11 h idle with five
    #     unresolved entries and session_report said nothing, because the advice
    #     was keyed on the 6-hour ACTIVITY window. The window answers "which
    #     session does a new write join"; it cannot answer "is a night unscored".
    with tempfile.TemporaryDirectory() as _td95:
        _lp95 = os.path.join(_td95, "l.json")
        _now95 = _time95.time()
        json.dump({"sessions": [{"id": "s-old95", "opened_at": "2026-09-02T21:52:52Z",
                                 "last_activity": _now95 - 11 * 3600,
                                 "closed_at": None, "mode": None}],
                   "entries": [{"id": "e-a95", "session_id": "s-old95", "clip_name": "A",
                                "slot": "31/0", "source": "catalogue", "role": "bass",
                                "gear": 5, "immediate_verdict": "keep",
                                "terminal_state": None},
                               {"id": "e-b95", "session_id": "s-old95", "clip_name": "B",
                                "slot": "31/1", "source": "catalogue", "role": "lead",
                                "gear": 2, "immediate_verdict": None,
                                "terminal_state": None}]},
                  open(_lp95, "w"))
        _s95 = _ledger95.summary(path=_lp95)
        assert _s95["open_session_awaiting_sweep"] == 2, \
            "an idle night with unresolved entries is still unswept: %r" % _s95
        assert _s95["coverage"]["entries_with_identity"] == 0 \
            and _s95["coverage"]["unresolved_identity"] == 2, \
            "legacy rows remain explicitly unidentified: %r" % _s95["coverage"]
        assert any("attest" in a and "close_session(" in a
                   for a in _s95["advice"]), \
            "the nag must outlive the idle window — an identity-less night is " \
            "attested or retired, never swept"
        assert _ledger95.open_session_entries(_ledger95.load(_lp95)) == [], \
            "the WRITE path still uses the idle window; only the report ignores it"
        _d95 = json.load(open(_lp95))
        for _e95 in _d95["entries"]:
            _e95["terminal_state"] = "keep"
        json.dump(_d95, open(_lp95, "w"))
        assert not any("close_session()" in a
                       for a in _ledger95.summary(path=_lp95)["advice"]), \
            "a swept night must stop nagging"
        # v3: a legacy idle-"closed" night with unresolved rows was never
        # scored — migration ends it (state only) instead of leaving it closed.
        with tempfile.TemporaryDirectory() as _td95c:
            _lp95c = os.path.join(_td95c, "l.json")
            json.dump({"version": 2,
                       "sessions": [{"id": "s-old95c",
                                     "opened_at": "2026-09-02T21:52:52Z",
                                     "last_activity": _now95 - 11 * 3600,
                                     "closed_at": "2026-09-03T08:52:52Z",
                                     "closed_by": "idle", "mode": None,
                                     "set_identity": None}],
                       "entries": [{"id": "e-c95", "session_id": "s-old95c",
                                    "clip_name": "C", "slot": "31/2",
                                    "source": "catalogue",
                                    "immediate_verdict": None,
                                    "terminal_state": None}],
                       "adoptions": [], "observations": [],
                       "judgment_events": []},
                      open(_lp95c, "w"))
            _m95c = _ledger95.load(_lp95c)["sessions"][0]
            assert _m95c["ended_by"] == "idle" \
                and _m95c["ended_at"] == "2026-09-03T08:52:52Z", _m95c
            assert _m95c["closed_at"] is None, \
                "ending is not closing — only a verified sweep scores the night"
            assert _m95c["set_aliases"] == [] \
                and _m95c["runtime_epochs"] == [], _m95c

    # (b) the critic's thresholds belong to the server, not to the contract it
    #     is judging. A contract may tighten, never loosen.
    _seed95 = [{"pitch": 62 + [0, 3, 5, 7][i % 4], "start": float(i), "duration": 0.9,
                "velocity": 80 + 3 * i} for i in range(8)]
    _c95 = _gears88.answer_with_gear(_seed95, gear=5, key="D minor")
    _loose95 = _copy95.deepcopy(_c95)
    _loose95["validation"]["min_style_score"] = 0.0
    _loose95["validation"]["min_in_key_frac"] = 0.0
    assert _gears88.validate_composed(
        [{"pitch": 127, "start": 0.0, "duration": 0.5, "velocity": 100}],
        _loose95)["passes"] is False, \
        "a caller must not be able to zero the gate it is being judged by"
    _tight95 = _copy95.deepcopy(_c95)
    _tight95["validation"]["min_style_score"] = 0.99
    assert _gears88.validate_composed(_seed95, _tight95)["passes"] is False, \
        "…but tightening must still work"
    assert _gears88.validate_composed(_seed95, _c95)["thresholds"]["min_style_score"] \
        == _gears88.MIN_STYLE_SCORE

    # (c) provenance follows what actually STEERED. R1 let a reference ride
    #     non-gear-2 paths as prose; the ledger kept stamping ref:<id>, which
    #     would have credited it in the source x role x gear strata.
    #     The card is deliberately NOT his: answer_my_loop answers with the
    #     owner's OWN phrases, so a reference of any ownership only steers
    #     which of them are picked — the steered write lands on JAM normally
    #     (the his-only write gate lives where the ref's own transcribed
    #     notes are pasted, apply_audio_ref(write=True)).
    _card95 = {"ref_id": "r-95", "identity": "x", "imitation_do": ["a"],
               "imitation_dont": ["b"], "ownership": "other",
               "structure": {"density": "low", "grid_bpm": 104.0, "register": "low"}}
    _prev_card95, _prev_w95 = _ar80.get_ref_card, _lw88.write_answer_to_jam_track
    _prev_lp95, _olp95 = _ledger95.LEDGER_PATH, _ledger95.LEDGER_PATH
    with tempfile.TemporaryDirectory() as _td95b:
        _ledger95.LEDGER_PATH = os.path.join(_td95b, "l.json")
        _core.set_ratings_path(os.path.join(_td95b, "r.json"))
        try:
            _ar80.get_ref_card = lambda rid: _card95

            async def _okw95(notes, *, clip_name, tempo):
                return {"slot": "3/7", "track": "SIDEKICK JAM", "verified_ok": True}
            _lw88.write_answer_to_jam_track = _okw95
            _r95 = _aml88(seed_notes=_seed95, key="D minor", gear=1,
                          ref_id="r-95", write=True)
            assert _r95["ref"]["steering"] == "prose_only"
            # a reference never refuses an answer: another artist's record
            # narrowed nothing here (gear 1), but the write still LANDS —
            # the answer is his phrases whichever record steered the pick.
            assert "live_write_skipped" not in _r95
            _entries95 = _ledger95.load(_ledger95.LEDGER_PATH)["entries"]
            assert len(_entries95) == 1, \
                "a steered answer writes to JAM, whatever the ref's ownership"
            _e95b = _entries95[-1]
            assert _e95b["source"] == "catalogue", \
                "a reference that never narrowed retrieval is not ref-assisted"
            assert _e95b["ref_prose"] == "r-95", \
                "…but it is still recorded, so nothing is lost"
        finally:
            _ar80.get_ref_card = _prev_card95
            _lw88.write_answer_to_jam_track = _prev_w95
            _ledger95.LEDGER_PATH = _olp95
            _core.set_ratings_path(None)
    print("  unswept nights nag after the night ends; the gate is the server's;")
    print("  a prose-only reference is recorded, not credited; a ref of any")
    print("  ownership steers his phrases and the steered write still lands")
    print("  OK\n")

    # 14. determinism regression — identical seed + identical args → identical output
    print("--- determinism regression (develop_idea) ---")
    import random

    seed_notes = [
        {"pitch": 57, "start_beats": 0.0, "duration_beats": 0.5, "velocity": 80},
        {"pitch": 60, "start_beats": 0.5, "duration_beats": 0.5, "velocity": 78},
        {"pitch": 62, "start_beats": 1.0, "duration_beats": 0.5, "velocity": 76},
        {"pitch": 64, "start_beats": 1.5, "duration_beats": 0.5, "velocity": 74},
        {"pitch": 65, "start_beats": 2.0, "duration_beats": 1.0, "velocity": 82},
    ]
    random.seed(42)
    result1 = _core.develop_idea(seed_notes, tempo=120)
    random.seed(42)
    result2 = _core.develop_idea(seed_notes, tempo=120)
    # Compare theme notes (list of dicts)
    theme1 = result1["variations"]["theme"]
    theme2 = result2["variations"]["theme"]
    assert len(theme1) == len(theme2), "theme note count must match"
    for n1, n2 in zip(theme1, theme2):
        assert n1["pitch"] == n2["pitch"], "theme pitch must match"
        assert n1["start_beats"] == n2["start_beats"], "theme start must match"
        assert n1["duration_beats"] == n2["duration_beats"], "theme duration must match"
        assert n1["velocity"] == n2["velocity"], "theme velocity must match"
    # Compare thin/thicken note counts (integers)
    assert len(result1["variations"]["thin"]) == len(result2["variations"]["thin"])
    assert len(result1["variations"]["thicken"]) == len(result2["variations"]["thicken"])
    print("  OK\n")

    _session_state_after = _session_state_fingerprint()
    _state_drift = sorted(
        key for key in set(_session_state_before) | set(_session_state_after)
        if _session_state_before.get(key) != _session_state_after.get(key))
    assert not _state_drift, (
        "the selftest wrote to production session state (%s) — redirect "
        "the offending block's path to a temp dir"
        % ", ".join(repr(key) for key in _state_drift))

    print("=== all selftests passed ===")
    return 0


def main():
    if "--selftest" in sys.argv:
        raise SystemExit(run_selftest())
    try:                                   # SOMBRA pedalboard listener + presence lamp
        import sombra_listener             # (never in selftest — real sessions only)
        sombra_listener.start()
    except Exception as exc:               # noqa: BLE001 — a dead lamp must not kill MCP
        print(f"sombra listener unavailable: {exc}", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
