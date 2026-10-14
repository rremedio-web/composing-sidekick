"""Envelope bridge: get HIS dynamics gestures onto sidekick-written clips.

Producer Pal cannot touch automation, but the Live API can (session clips:
Clip.create_automation_envelope + AutomationEnvelope.insert_step). Two surfaces:
  1. `ppal-live-api` — Producer Pal's raw-LOM escape hatch (user-enabled in the
     device's Setup tab). probe() establishes whether it can CALL functions or is
     get/set-only.
  2. The SIDEKICK BRIDGE v8 M4L device (tools/m4l/sidekick_bridge.js): a dumb
     execute-ops JSON surface over UDP — OSC frame "/sk" carrying ONE base64url
     token (Max atom-tokenization safe). Ports 3351 (cmd) / 3352 (reply).

Policy is structural: every op batch OPENS with assert_track_name(track, "SIDEKICK
JAM") executed by the device itself; there is no any-clip parameter. Dry-run is the
default everywhere; every write plan carries its own undo (clear_envelopes).

Pure parts (codec, chunking, plan building) are selftested without sockets.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import curves  # noqa: E402
import live_write  # noqa: E402  (read-side reuse: PP text parsers, jam-track lookup)

BRIDGE_HOST = "127.0.0.1"
BRIDGE_CMD_PORT = 3351
BRIDGE_REPLY_PORT = 3352
OSC_ADDR = "/sk"
MAX_OPS_PER_CMD = 48
JAM_TRACK_NAME = live_write.JAM_TRACK_NAME

_CMD_COUNTER = [0]   # correlation ids: monotonic counter, never wall-clock


# --- pure codec ---------------------------------------------------------------

def _pad4(b: bytes) -> bytes:
    return b + b"\x00" * ((4 - len(b) % 4) % 4)


def osc_pack(addr: str, s: str) -> bytes:
    return (_pad4(addr.encode() + b"\x00")
            + _pad4(b",s\x00")
            + _pad4(s.encode() + b"\x00"))


def osc_unpack(data: bytes) -> tuple[str, str]:
    addr_end = data.index(b"\x00")
    addr = data[:addr_end].decode()
    off = (addr_end + 4) & ~3
    tag_end = data.index(b"\x00", off)
    off = (tag_end + 4) & ~3
    s_end = data.index(b"\x00", off)
    return addr, data[off:s_end].decode()


def _b64url_encode(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _b64url_decode(tok: str) -> str:
    pad = "=" * (-len(tok) % 4)
    return base64.urlsafe_b64decode(tok + pad).decode()


def encode_cmd(cmd: dict) -> bytes:
    return osc_pack(OSC_ADDR, _b64url_encode(json.dumps(cmd, separators=(",", ":"))))


def decode_reply(data: bytes) -> dict:
    addr, tok = osc_unpack(data)
    if addr != OSC_ADDR:
        raise ValueError(f"unexpected OSC address {addr!r}")
    return json.loads(_b64url_decode(tok))


def chunk_ops(ops: list[dict], max_ops: int = MAX_OPS_PER_CMD) -> list[list[dict]]:
    return [ops[i:i + max_ops] for i in range(0, len(ops), max_ops)] or [[]]


# --- pure plan building ---------------------------------------------------------

def _steps_from_points(points: list[list[float]]) -> list[list[float]]:
    """[[beat, value]] → insert_step triplets [time, length, value]; each step runs to
    the next point (capped at a 16th) so slow curves read as ramps, not spikes."""
    steps = []
    for i, (t, v) in enumerate(points):
        nxt = points[i + 1][0] if i + 1 < len(points) else t + 0.25
        steps.append([round(t, 4), round(min(0.25, max(0.01, nxt - t)), 4), v])
    return steps


DEFAULT_SPECS = (
    {"family": "volume", "character": "swell"},
    {"family": "send", "character": "arc", "tail_bars": 1},
    {"family": "cutoff", "character": "arc", "optional": True},
)


def build_write_plan(jam_slot: str, envelope_specs: list[dict] | None, bars: int,
                     seed: int = 0, curves_lib: dict | None = None) -> dict:
    """Pure: pick + render curves for each spec, emit transport-neutral ops with the
    SIDEKICK JAM policy op first and the undo op attached. No sockets, no resolution
    of live ids (the executor fills clip_id/param_id after runtime lookup)."""
    track_index = int(str(jam_slot).split("/")[0])
    specs = list(envelope_specs) if envelope_specs else [dict(s) for s in DEFAULT_SPECS]
    bars = max(1, int(bars))
    rendered = []
    for i, spec in enumerate(specs):
        family = spec.get("family")
        if family not in curves.FAMILIES:
            raise ValueError(f"spec {i}: family must be one of {sorted(curves.FAMILIES)}")
        span = max(1, int(spec.get("tail_bars", bars)))
        tpl = curves.pick_curve(family, span, seed=seed + i,
                                character=spec.get("character"), curves=curves_lib)
        lo, hi = spec.get("lo"), spec.get("hi")
        d_lo, d_hi = curves.DEFAULT_WRITE_RANGES[family]
        lo = d_lo if lo is None else float(lo)
        hi = d_hi if hi is None else float(hi)
        pts = curves.render_curve(tpl, span, lo, hi)
        if spec.get("tail_bars"):   # tail gestures land at the END of the clip
            offset = (bars - span) * 4.0
            pts = [[round(t + offset, 4), v] for t, v in pts]
        rendered.append({
            "family": family,
            "class": tpl["class"],
            "optional": bool(spec.get("optional")),
            "range_mode": "param_fraction" if family in ("cutoff", "pan") else "absolute",
            "provenance": tpl["provenance"],
            "steps": _steps_from_points(pts),
        })
    return {
        "jam_slot": jam_slot,
        "bars": bars,
        "policy_op": {"op": "assert_track_name", "track_index": track_index,
                      "expect": JAM_TRACK_NAME},
        "envelopes": rendered,
        "undo": [{"op": "clear_envelopes", "slot": jam_slot}],
        "executed": False,
    }


# --- I/O: UDP transport to the v8 bridge ----------------------------------------

def send_cmds(op_batches: list[list[dict]], host: str = BRIDGE_HOST,
              port: int = BRIDGE_CMD_PORT, reply_port: int = BRIDGE_REPLY_PORT,
              timeout_s: float = 3.0, retries: int = 2) -> list[dict]:
    """Send op batches to the bridge device; ops are idempotent so retries are safe."""
    replies = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rx:
        rx.bind((host, reply_port))
        rx.settimeout(timeout_s)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
            for part, ops in enumerate(op_batches):
                _CMD_COUNTER[0] += 1
                cmd = {"id": _CMD_COUNTER[0], "part": [part + 1, len(op_batches)], "ops": ops}
                packet = encode_cmd(cmd)
                got = None
                for _ in range(retries + 1):
                    tx.sendto(packet, (host, port))
                    try:
                        data, _addr = rx.recvfrom(65535)
                        rep = decode_reply(data)
                        if rep.get("id") == cmd["id"]:
                            got = rep
                            break
                    except socket.timeout:
                        continue
                if got is None:
                    raise TimeoutError(
                        f"no reply from SIDEKICK BRIDGE on udp {port}->{reply_port} "
                        "(is the .amxd on the SIDEKICK JAM track? see config/claude_mcp.md)")
                replies.append(got)
                if not got.get("ok"):
                    break
    return replies


def _clip_id_for_slot(slot: str):
    """Read the clip at track/scene via Producer Pal and pull its Live object id."""
    from mcp_server import pp_session
    track_index, scene = (int(x) for x in slot.split("/"))

    async def read():
        async with pp_session.operation_session(live_write.PP_URL) as session:
            response = await session.call("ppal-read-clip", {
                **pp_session.requests_for(session).clip(track=track_index, scene=scene),
                "include": []})
            # the whole response (not .result): _find_str reads the parsed JSON body
            return live_write._find_str(response, "id")

    return pp_session.run_coro_blocking(read())


def write_clip_envelopes(jam_slot: str, envelope_specs: list[dict] | None = None,
                         bars: int = 2, seed: int = 0, dry_run: bool = True) -> dict:
    """Plan (pure) → resolve live ids → execute via the v8 bridge → report.
    dry_run=True returns the full plan without opening a socket."""
    plan = build_write_plan(jam_slot, envelope_specs, bars, seed=seed)
    if dry_run:
        return plan

    track_index = int(jam_slot.split("/")[0])
    clip_id = _clip_id_for_slot(jam_slot)
    if not clip_id:
        raise ValueError(f"no clip found at slot {jam_slot}")

    setup = [plan["policy_op"],
             {"op": "get_id", "path": f"live_set tracks {track_index} mixer_device volume"},
             {"op": "get_id", "path": f"live_set tracks {track_index} mixer_device sends 0"},
             {"op": "list_params", "track_index": track_index}]
    setup_reply = send_cmds([setup])[0]
    if not setup_reply.get("ok"):
        return {"executed": False, "error": setup_reply, "plan": plan}
    res = setup_reply["results"]
    param_ids = {"volume": res[1].get("id"), "send": res[2].get("id")}
    cutoff = None
    for dev in res[3].get("devices", []):
        if dev.get("class_name") == "AutoFilter":
            for p in dev.get("params", []):
                if str(p.get("name", "")).lower() in ("frequency", "cutoff", "filter freq"):
                    cutoff = p
    ops: list[dict] = [plan["policy_op"]]
    written, skipped = [], []
    for env in plan["envelopes"]:
        fam = env["family"]
        if fam == "cutoff":
            if cutoff is None:
                skipped.append({"family": fam, "reason": "no AutoFilter on the jam track"})
                continue
            # param_fraction steps are already fractions of the param range (0.2..1.0);
            # map straight onto the live parameter's own [min, max]
            pmin, pmax = float(cutoff["min"]), float(cutoff["max"])
            steps = [[t, ln, round(pmin + (pmax - pmin) * v, 5)] for t, ln, v in env["steps"]]
            pid = cutoff["id"]
        elif fam in param_ids and param_ids[fam]:
            steps, pid = env["steps"], param_ids[fam]
        else:
            skipped.append({"family": fam, "reason": "param id unresolved"})
            continue
        ops.append({"op": "create_envelope", "clip_id": clip_id, "param_id": pid,
                    "label": fam})
        ops.append({"op": "insert_steps_last", "points": steps, "label": fam})
        written.append({"family": fam, "class": env["class"],
                        "provenance": env["provenance"], "n_steps": len(steps)})
    replies = send_cmds(chunk_ops(ops))
    ok = all(r.get("ok") for r in replies)
    return {"executed": ok, "jam_slot": jam_slot, "clip_id": clip_id,
            "written": written, "skipped": skipped,
            "undo": plan["undo"], "replies": replies if not ok else replies[-1:]}


def clear_clip_envelopes(jam_slot: str) -> dict:
    track_index = int(jam_slot.split("/")[0])
    clip_id = _clip_id_for_slot(jam_slot)
    if not clip_id:
        return {"cleared": False, "error": "no clip found at slot %s" % jam_slot}
    reply = send_cmds([[{"op": "assert_track_name", "track_index": track_index,
                         "expect": JAM_TRACK_NAME},
                        {"op": "clear_envelopes", "clip_id": clip_id}]])[0]
    return {"cleared": bool(reply.get("ok")), "reply": reply}


def list_grooves() -> dict:
    reply = send_cmds([[{"op": "list_grooves"}]])[0]
    return reply["results"][0] if reply.get("ok") else {"error": reply}


def assign_clip_groove(jam_slot: str, role: str = "lead",
                       prefix: str = "RICARDO FEEL", dry_run: bool = True) -> dict:
    """Assign the pool groove named '<prefix> <ROLE> ...' to the jam clip. If the pool
    has no match, return the listing + the manual Extract Groove instruction — never
    guess."""
    if dry_run:
        return {"executed": False, "would_match": f"{prefix} {role.upper()}",
                "note": "confirm=True lists the pool and assigns on exact prefix match"}
    track_index = int(jam_slot.split("/")[0])
    pool = list_grooves()
    grooves = pool.get("grooves", [])
    match = next((g for g in grooves
                  if str(g.get("name", "")).upper().startswith(f"{prefix} {role}".upper())), None)
    if match is None:
        return {"executed": False, "pool": grooves,
                "instruction": "no matching groove — drag store/exports/grooves/"
                               f"'{prefix} {role.upper()} 1BAR.mid' into Live and Extract Groove(s) once"}
    clip_id = _clip_id_for_slot(jam_slot)
    if not clip_id:
        return {"executed": False, "error": "no clip found at slot %s" % jam_slot}
    reply = send_cmds([[{"op": "assert_track_name", "track_index": track_index,
                         "expect": JAM_TRACK_NAME},
                        {"op": "set_clip_groove", "clip_id": clip_id,
                         "groove_id": match.get("id")}]])[0]
    return {"executed": bool(reply.get("ok")), "groove": match,
            "undo": {"op": "clear_clip_groove", "clip_id": clip_id}}


# --- probe: is the escape hatch enough, or is the v8 device required? ------------

PROBE_STEPS = (
    "P1 list Producer Pal tools — is ppal-live-api enabled (device Setup tab)?",
    "P2 ppal-live-api GET live_set tempo (read-only sanity)",
    "P3 ppal-live-api CALL live_app get_major_version (side-effect-free call test)",
    "P4 (separate confirm) create 'ENV PROBE (delete me)' jam clip; create_automation_envelope",
)


def probe(confirm: bool = False) -> dict:
    """Establish the envelope-writing surface. confirm=False returns the plan only."""
    if not confirm:
        return {"executed": False, "steps": list(PROBE_STEPS),
                "verdicts": ["ppal_live_api_full", "get_set_only -> v8_required",
                             "tool_absent -> v8_required"]}
    from mcp_server import pp_session as _pp_session

    async def run():
        out = {"executed": True, "steps": {}}
        async with _pp_session.operation_session(live_write.PP_URL) as session:
            listed = await session._session.list_tools()
            tools = getattr(listed, "tools", None)
            if tools is None and isinstance(listed, dict):
                tools = listed.get("tools")
            names = [getattr(tool, "name", None) or tool.get("name")
                     for tool in (tools or [])]
            out["steps"]["P1_tool_present"] = "ppal-live-api" in names
            if not out["steps"]["P1_tool_present"]:
                out["verdict"] = "tool_absent -> v8_required"
                out["instruction"] = ("enable 'Live API access' in the Producer Pal "
                                      "device's Setup tab, or install the SIDEKICK "
                                      "BRIDGE .amxd (config/claude_mcp.md)")
                return out
            get_request = _pp_session.build_live_api_request("live_set", [
                {"type": "get", "property": "tempo"}])
            get_rep = await _pp_session.call_tool(
                session._session, "ppal-live-api", get_request)
            out["steps"]["P2_get_tempo"] = str(get_rep)[:200]
            try:
                call_request = _pp_session.build_live_api_request("live_app", [
                    {"type": "call", "method": "get_major_version"}])
                call_rep = await _pp_session.call_tool(
                    session._session, "ppal-live-api", call_request)
                call_text = str(call_rep)
                out["steps"]["P3_call"] = call_text[:200]
                out["verdict"] = ("ppal_live_api_full" if "error" not in call_text.lower()
                                  else "get_set_only -> v8_required")
            except Exception as exc:
                out["steps"]["P3_call"] = f"{type(exc).__name__}: {exc}"
                out["verdict"] = "get_set_only -> v8_required"
            return out

    return _pp_session.run_coro_blocking(run())
