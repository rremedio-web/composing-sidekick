"""SOMBRA's nervous system: device buttons → UDP/OSC events → sidekick actions.

The device (SOMBRA.amxd) is a pedalboard: message-boxes send OSC over UDP 3353;
this listener (a thread inside the running MCP server, i.e. alive exactly when a
session is) executes the DETERMINISTIC tier immediately and flags LLM-tier
presses (gears 5-6, diagnosis) for the chat. A 2-second heartbeat to UDP 3354
lights the device's presence lamp — dark lamp = playing solo, by design.

Pure parts (OSC event parsing, dispatch planning) are selftested; the threads
are started only by server main() in stdio mode, never by the selftest.
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _alias in ("sombra_listener", "mcp_server.sombra_listener"):
    sys.modules.setdefault(_alias, sys.modules[__name__])

EVENT_PORT = 3353
LAMP_PORT = 3354
REPLY_PORT = 3355
HEARTBEAT_S = 2.0

STATE: dict = {"running": False, "last_answer_slot": None,
               "last_answer_entry_id": None, "log": [],
               "ears_audition": False, "accounting_failures": []}
_THREADS: list = []
_STOP = threading.Event()

# The pedal log is a device readout, not a ledger: cap it so a bad night
# cannot grow it without bound.
ACCOUNTING_FAILURE_CAP = 20


# --- pure: OSC event parsing -----------------------------------------------------

def parse_osc_event(data: bytes) -> dict:
    """Minimal OSC parse for pedal events: address + int/float/string args."""
    def _pad4(n):
        return (n + 4) & ~3

    end = data.index(b"\x00")
    address = data[:end].decode()
    off = _pad4(end)
    args: list = []
    if off < len(data) and data[off:off + 1] == b",":
        tend = data.index(b"\x00", off)
        tags = data[off + 1:tend].decode()
        off = _pad4(tend)
        import struct
        for t in tags:
            if t == "i":
                args.append(struct.unpack(">i", data[off:off + 4])[0]); off += 4
            elif t == "f":
                args.append(struct.unpack(">f", data[off:off + 4])[0]); off += 4
            elif t == "s":
                send_ = data.index(b"\x00", off)
                args.append(data[off:send_].decode()); off = _pad4(send_)
    return {"address": address, "args": args}


# --- dispatch: event → plan (pure) → execution (live) ------------------------------

def _parse_sticky_arg(args: list) -> bool | str:
    """0/1 or the string 'toggle' from the device dial."""
    if not args:
        raise ValueError("sticky needs 0, 1, or toggle")
    raw = args[0]
    if isinstance(raw, str):
        tok = raw.strip().lower()
        if tok == "toggle":
            return "toggle"
        if tok in ("0", "off", "false"):
            return False
        if tok in ("1", "on", "true"):
            return True
        raise ValueError("sticky arg must be 0, 1, or toggle — got %r" % raw)
    if raw in (0, 1):
        return bool(raw)
    raise ValueError("sticky arg must be 0, 1, or toggle — got %r" % raw)


def dispatch(
    event: dict,
    execute: bool = True,
    *,
    moment=None,
    develop_path: str | None = None,
    ears_path: str | None = None,
) -> dict:
    """Map a pedal press to a sidekick action. execute=False returns the PLAN only
    (selftested); execute=True runs it against core/gears/rituals/live_write."""
    addr = event.get("address", "")
    args = event.get("args", [])
    plan: dict

    if addr == "/sombra/ping":
        plan = {"action": "ping"}
    elif addr == "/sombra/deal":
        plan = {"action": "deal", "tool": "bury_or_marry", "tool_args": {"action": "deal"}}
    elif addr == "/sombra/answer":
        gear = int(args[0]) if args else 2
        if gear >= 5:
            plan = {"action": "llm_needed", "gear": gear,
                    "note": f'gear {gear} composes with the LLM — say "gear {gear}" in chat'}
        else:
            plan = {"action": "answer", "tool": "answer_my_loop",
                    "tool_args": {"gear": gear, "write": True}}
    elif addr == "/sombra/keep" or addr == "/sombra/drop":
        verdict = addr.rsplit("/", 1)[1]
        slot = STATE.get("last_answer_slot")
        if not slot:
            plan = {"action": "noop", "note": f"{verdict}: no answer on record yet — "
                                              "press ANSWER first"}
        else:
            plan = {"action": verdict, "tool": "judge_answer",
                    "tool_args": {"verdict": verdict, "slot": slot,
                                  "confirm": verdict == "drop"}}
    elif addr == "/sombra/lay":
        plan = {"action": "lay", "tool": "lay_the_song",
                "tool_args": {"from_selection": True}}
    elif addr == "/sombra/endings":
        plan = {"action": "endings", "tool": "three_endings",
                "tool_args": {"from_selection": True, "write": True}}
    elif addr == "/sombra/sticky":
        plan = {"action": "sticky", "sticky_arg": _parse_sticky_arg(args)}
    elif addr == "/sombra/ears/audition":
        plan = {"action": "ears_audition", "audition_arg": _parse_sticky_arg(args)}
    elif addr == "/sombra/ears/reset":
        plan = {"action": "ears_reset"}
    elif addr == "/sombra/status":
        plan = {"action": "status"}
    else:
        plan = {"action": "unknown", "address": addr}

    if not execute:
        return plan
    if plan.get("action") == "sticky":
        return _run_sticky(plan, develop_path)
    if plan.get("action") == "ears_audition":
        return _run_ears_audition(plan)
    if plan.get("action") == "ears_reset":
        return _run_ears_reset(plan, ears_path)
    if plan.get("action") == "status":
        return _run_status(plan, moment=moment, develop_path=develop_path)
    if "tool" not in plan:
        return plan
    try:
        plan["result"] = _execute(plan)
        plan["ok"] = True
    except Exception as exc:  # noqa: BLE001 — a pedal press must never kill the thread
        plan["ok"] = False
        plan["error"] = f"{type(exc).__name__}: {exc}"
    return plan


def _run_sticky(plan: dict, develop_path: str | None) -> dict:
    import develop_session as _develop_session
    import device_readout as _device_readout

    arg = plan["sticky_arg"]
    if arg == "toggle":
        st = _develop_session.load_state(develop_path)
        st = _develop_session.set_sticky(not st.get("sticky"), develop_path)
    else:
        st = _develop_session.set_sticky(arg, develop_path)
    plan["reply"] = _device_readout.sticky_announcement(st)
    plan["ok"] = True
    return plan


def _run_ears_audition(plan: dict) -> dict:
    arg = plan["audition_arg"]
    on = (not STATE["ears_audition"]) if arg == "toggle" else bool(arg)
    STATE["ears_audition"] = on
    plan["reply"] = ("ears on for this session — answers listen to the room"
                     if on else "ears off — back to defaults")
    plan["ok"] = True
    return plan


def _run_ears_reset(plan: dict, ears_path: str | None) -> dict:
    import ears as _ears

    plan["reply"] = _ears.reset_session(ears_path)
    plan["ok"] = True
    return plan


def _run_status(plan: dict, *, moment=None, develop_path: str | None) -> dict:
    import device_readout as _device_readout

    readout = _device_readout.device_readout(moment, develop_path=develop_path)
    # audition is listener session state, not a device_readout() concern (kept pure)
    readout["audition"] = bool(STATE["ears_audition"])
    plan["reply"] = readout
    plan["ok"] = True
    return plan


def _selection_notes():
    import live_write
    live = asyncio.run(live_write.read_current_clip())
    return live_write.notes_or_unread_error(live, "no MIDI in the current Live selection")


def _execute(plan: dict):
    import core
    import gears
    import jam_lifecycle as _jl
    import live_write
    import rituals

    tool, ta = plan["tool"], dict(plan.get("tool_args", {}))
    if tool == "bury_or_marry":
        return rituals.deal()
    if tool == "answer_my_loop":
        # A new ANSWER starts a fresh judgment cycle: the previous entry id
        # must never leak into this answer's KEEP/DROP binding.
        STATE["last_answer_entry_id"] = None
        seed = _selection_notes()
        import develop_session as _develop_session
        _dst = _develop_session.load_state()
        if ta.get("gear", 2) == 2 and _dst.get("sticky") and _dst.get("hold"):
            _hold = _dst["hold"]
            _move = _develop_session.current_move(_dst)
            # audition on → listen to the room this session; off → follow the
            # stored default (use_ears=None, today's behavior).
            use_ears = True if STATE.get("ears_audition") else None
            ans = core.develop_my_loop(
                seed_notes=_hold["seed_notes"], move=_move, key=_hold["key"],
                seed=_hold["pass"], pin_source=_hold["source_phrase_id"],
                use_ears=use_ears)
            ans["sticky_develop"] = (
                "hold on — holding your idea, pass %d (%s)"
                % (_hold["pass"], _move))
        else:
            # audition on → listen to the room this session; off → follow the
            # stored default (use_ears=None, today's behavior).
            use_ears = True if STATE.get("ears_audition") else None
            ans = (core.answer_my_loop(seed, seed=0, use_ears=use_ears)
                   if ta.get("gear", 2) == 2
                   else gears.answer_with_gear(seed, gear=ta["gear"], seed=0,
                                               use_ears=use_ears))
        STATE["last_answer_seed"] = seed
        if ans.get("no_fit"):
            return ans
        res = asyncio.run(live_write.write_answer_to_jam_track(
            ans["answer_notes"], clip_name=f"ans g{ta.get('gear', 2)}",
            tempo=ans.get("tempo") or 120.0))
        STATE["last_answer_slot"] = res.get("slot")
        STATE["last_answer_source"] = (ans.get("source_phrase_ids") or [None])[0]
        STATE["last_answer_key"] = ans.get("key")
        STATE["last_answer_notes"] = ans.get("answer_notes")
        STATE["last_answer_clip_ref"] = res.get("clip_ref")
        STATE["last_answer_set_identity"] = res.get("set_identity")
        _buf: dict = {}
        _jl.stamp_verified_write(
            _buf,
            clip_name=f"ans g{ta.get('gear', 2)}",
            slot=res.get("slot"),
            source="catalogue",
            role="lead",
            gear=ta.get("gear", 2),
            metadata_from=ans,
            source_phrase_id=STATE["last_answer_source"],
            notes=ans.get("answer_notes") or [],
            live_receipt=res,
            ledger_error_sink=lambda msg: _log({"ledger_error": msg}),
        )
        if _buf.get("ledger"):
            STATE["last_answer_entry_id"] = _buf["ledger"].get("entry_id")
        if _buf.get("scoreable") is False:
            # the write is accounted but can never be scored at session
            # close — surface that in the pedal log, not only in chat results
            _log({"unscoreable": _buf.get("scoreable_reason")})
        return {"slot": res.get("slot"), "source": STATE.get("last_answer_source"),
                "ledger": _buf.get("ledger"), "ledger_error": _buf.get("ledger_error")}
    if tool == "judge_answer":
        import ledger as _ledger

        # Same binder as chat judgments: only a bound entry id is stamped,
        # never a slot guess or a stale remembered id. The slot that binds
        # is the one this PRESS carries — the remembered slot is display
        # state and must never veto the remembered entry.
        binding = _ledger.resolve_judgment_target(
            _ledger.load(), ta["verdict"],
            entry_id=STATE.get("last_answer_entry_id"),
            answer_notes=STATE.get("last_answer_notes"),
            slot=ta.get("slot"),
            origin="pedal")
        if binding.get("refused"):
            _accounting_failure("binding", binding["error"])
            return {"verdict": ta["verdict"], "pedal": True,
                    "ledger_error": binding["error"]}
        bound = binding.get("entry")
        bound_id = bound["id"] if bound else None
        if bound is not None:
            # The server's contradiction gate, before any stamping: a
            # conflicting press is an error, never a half-learned judgment.
            check_row = bound
            if bound.get("judged_snapshot") is not None:
                check_row = {**bound, **bound["judged_snapshot"]}
            verdict_word = str(ta["verdict"]).strip().lower()
            if check_row.get("immediate_verdict") is not None and \
                    check_row["immediate_verdict"] != verdict_word:
                raise ValueError(
                    "judgment verdict contradicts the existing immutable verdict")
            state_notes = STATE.get("last_answer_notes")
            if state_notes is not None and check_row.get("answer_notes") is not None:
                if _ledger._normalize_answer_notes(state_notes) != \
                        check_row["answer_notes"]:
                    raise ValueError(
                        "judgment answer_notes contradict the verified write")
        # Verdict first: the ledger row is the durable fact; learning sinks
        # are retryable through the judgment journal (record_judgment's own
        # stamp below re-hits the same-verdict branch and journals it). A
        # stamp that fails outright is REPORTED — accounting_failures sees
        # it, the press returns ledger_error, and no learning runs.
        if bound_id:
            try:
                _ledger.record_verdict(str(ta["verdict"]).strip().lower(),
                                       entry_id=bound_id,
                                       path=_ledger.LEDGER_PATH)
            except Exception as exc:  # noqa: BLE001 — a press must never die
                message = "%s: %s" % (type(exc).__name__, exc)
                _accounting_failure("stamp", message)
                return {"verdict": str(ta["verdict"]).strip().lower(),
                        "pedal": True, "ledger_error": message}
        try:
            res = gears.judge_answer(ta["verdict"],
                                     source_phrase_id=STATE.get("last_answer_source"),
                                     answer_notes=STATE.get("last_answer_notes"),
                                     seed_notes=STATE.get("last_answer_seed"),
                                     key=STATE.get("last_answer_key"),
                                     entry_id=bound_id,
                                     idempotency_key=ta.get("idempotency_key"))
        except Exception as exc:  # noqa: BLE001 — a sink crash never undoes the verdict
            # Only a press whose verdict actually landed may degrade to a
            # retryable learning failure; anything else is a real error.
            verdict_word = str(ta["verdict"]).strip().lower()
            verdict_landed = False
            if bound_id:
                landed_row = next(
                    (e for e in _ledger.load().get("entries", [])
                     if e.get("id") == bound_id), None)
                verdict_landed = bool(landed_row and
                                      landed_row.get("immediate_verdict") == verdict_word)
            if not verdict_landed:
                raise
            res = {"verdict": verdict_word,
                   "learning_failure": {
                       "stage": "learning",
                       "error": f"{type(exc).__name__}: {exc}",
                       "retry": "press the same pedal verdict again to finish "
                                "learning (entry_id=%s)" % bound_id},
                   "note": "the ledger verdict stands; learning did not complete"}
            if bound_id:
                res["entry_id"] = bound_id

        _jl.record_judgment(
            res,
            entry_id=bound_id,
            event_id=res.get("event_id"),
            ledger_path=_ledger.LEDGER_PATH,
            slot=(bound or {}).get("slot") or ta.get("slot") or STATE.get("last_answer_slot"),
            confirm=bool(ta.get("confirm")),
            delete_slot=ta.get("slot"),
            record_verdict_fn=_ledger.record_verdict if bound is not None else (
                lambda *_args, **_kwargs: None),
            delete_jam_clip_fn=lambda s, **expected: asyncio.run(
                live_write.delete_jam_clip(s, **expected)),
            no_match_note=_jl.PEDAL_NO_LEDGER_MATCH,
            pedal_drop_only=True,
            on_deleted=lambda: STATE.update({"last_answer_slot": None}),
            clip_ref=(bound or {}).get("clip_ref"),
            set_identity=(bound or {}).get("set_identity"),
        )
        stamp_error = res.get("ledger_error") or (
            res.get("accounting_failure") or {}).get("message")
        if stamp_error:
            _accounting_failure("stamp", stamp_error)
        return res
    if tool == "lay_the_song":
        seed = _selection_notes()
        m = core.plan_song_map(seed_notes=seed)
        return asyncio.run(live_write.write_song_locators(m))
    if tool == "three_endings":
        seed = _selection_notes()
        te = rituals.three_endings(seed)
        slots = []
        for e in te["endings"]:
            _name = f"END {e['name']}"
            r = asyncio.run(live_write.write_answer_to_jam_track(
                e["notes"], clip_name=_name, tempo=120.0))
            slots.append(r.get("slot"))
            _buf: dict = {}
            _jl.stamp_verified_write(
                _buf,
                clip_name=_name,
                slot=r.get("slot"),
                source="catalogue",
                role="lead",
                gear=None,
                key=te.get("key"),
                ledger_tempo=120.0,
                n_notes=len(e["notes"]),
                notes=e["notes"],
                live_receipt=r,
                ledger_error_sink=lambda msg: _log({"ledger_error": msg}),
            )
            if _buf.get("scoreable") is False:
                _log({"unscoreable": _buf.get("scoreable_reason")})
        STATE["last_endings_slots"] = slots
        return {"slots": slots}
    raise ValueError(f"no executor for {tool}")


# --- threads: listener + presence lamp ---------------------------------------------

def _log(entry):
    STATE["log"] = (STATE["log"] + [entry])[-20:]


def _accounting_failure(stage: str, message: str) -> None:
    """A pedal press that could not be accounted is kept for status()."""
    STATE["accounting_failures"] = (
        STATE.get("accounting_failures", []) +
        [{"stage": stage, "message": message, "at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}])[-ACCOUNTING_FAILURE_CAP:]


def _listen_loop():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", EVENT_PORT))
    sock.settimeout(1.0)
    while not _STOP.is_set():
        try:
            data, peer = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            event = parse_osc_event(data)
            result = dispatch(event, execute=True)
            _log({"event": event["address"], "result": result})
            reply = result.get("reply")
            if reply is not None:
                import envelope_bridge as _eb
                import json as _json
                payload = (_json.dumps(reply, separators=(",", ":"))
                           if isinstance(reply, dict) else str(reply))
                datagram = _eb.osc_pack("/sombra/reply", payload)
                sock.sendto(datagram, peer)
                # peer is the ephemeral sender port — a dead end for Max's
                # [udpsend]; also fan out to the fixed reply port [udpreceive 3355]
                # binds. Defensive: a refused/unbound reply port never kills the loop.
                try:
                    sock.sendto(datagram, ("127.0.0.1", REPLY_PORT))
                except OSError:
                    pass
        except Exception as exc:  # noqa: BLE001
            _log({"error": f"{type(exc).__name__}: {exc}"})
    sock.close()


def _lamp_loop():
    import envelope_bridge
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    beat = envelope_bridge.osc_pack("/alive", "sombra")
    while not _STOP.is_set():
        try:
            sock.sendto(beat, ("127.0.0.1", LAMP_PORT))
        except OSError:
            pass
        _STOP.wait(HEARTBEAT_S)
    sock.close()


def start() -> dict:
    """Idempotent; called by server main() in stdio mode only (never selftest)."""
    if STATE["running"]:
        return {"running": True, "note": "already up"}
    _STOP.clear()
    for target in (_listen_loop, _lamp_loop):
        t = threading.Thread(target=target, daemon=True)
        t.start()
        _THREADS.append(t)
    STATE["running"] = True
    return {"running": True, "event_port": EVENT_PORT, "lamp_port": LAMP_PORT}


def status() -> dict:
    return {"running": STATE.get("running", False), "event_port": EVENT_PORT,
            "lamp_port": LAMP_PORT, "last_answer_slot": STATE.get("last_answer_slot"),
            "recent": (STATE.get("log") or [])[-8:],
            "accounting_failures": list(STATE.get("accounting_failures") or [])}
