#!/usr/bin/env python3
"""Generate the SOMBRA Max 9 patcher (.maxpat) — stage 2, SMOKE wave.

This emits real Max 9 patcher JSON (a `{"patcher": {...}}` document Max opens
natively). This wave ships the SMOKE patcher only — a ~30-box rig that exercises
every risk class of the future full pedalboard panel (live.* parameter widgets,
a fallback textbutton, script show/hide via thispatcher, the v8 protocol glue,
the UDP heartbeat/reply/poll plumbing). The full three-tab panel is a later
stage; the Patch builder and the SMOKE data layout are structured so it drops in.

Usage:
    .venv/bin/python tools/m4l/generate_sombra_patcher.py --smoke
        write tools/m4l/SOMBRA_SMOKE.maxpat (validated first; refuses on findings)

    .venv/bin/python tools/m4l/generate_sombra_patcher.py --validate PATH
        run contract_report on any .maxpat and print findings (exit 1 if any)

    .venv/bin/python tools/m4l/generate_sombra_patcher.py --smoke --out PATH
        override the output path

Determinism: no timestamps, no randomness. Object ids are assigned in creation
order ("obj-1", "obj-2", ...); output is json.dumps(doc, indent=2,
sort_keys=True) + "\n". A double build is byte-identical.

Skeleton verified against Max-9.0.5-saved example files on this machine
(jitter-examples/render/mrt/pass.motionblur.maxpat for the top-level shape;
spatialization/quad-spatializer for the live.tab / live.text parameter attrs).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

# Verified from Max-9.0.5-saved .maxpat files under
# "~/Library/Application Support/Cycling '74/Max 9/Examples".
APPVERSION = {
    "major": 9,
    "minor": 0,
    "revision": 5,
    "architecture": "x64",
    "modernui": 1,
}
# originid is always present at the top level of a saved patcher (e.g. "pat-24");
# we emit a fixed deterministic value so a double build stays byte-identical.
ORIGINID = "pat-sombra-1"

DEFAULT_OUT = os.path.join(ROOT, "tools", "m4l", "SOMBRA_SMOKE.maxpat")


# --------------------------------------------------------------------------- #
# Patch builder                                                               #
# --------------------------------------------------------------------------- #

class Patch:
    """Accumulate Max boxes + patchlines and emit a real .maxpat document.

    box(...) returns the created object id ("obj-N", N in creation order) so the
    caller can wire it with line(src_id, outlet, dst_id, inlet). live_param(...)
    builds the live.* parameter-attribute block and enforces unique
    parameter_longname across the patch (Max silently breaks on collisions).
    """

    def __init__(self, rect=(120.0, 120.0, 720.0, 480.0)):
        self._boxes: list[dict] = []
        self._lines: list[tuple] = []
        self._n = 0
        self._longnames: set[str] = set()
        self.rect = tuple(float(v) for v in rect)

    def box(self, maxclass, text=None, rect=(0.0, 0.0, 100.0, 22.0),
            presentation_rect=None, varname=None, hidden=False, **attrs):
        """Append a box; return its "obj-N" id. Coords are emitted as floats to
        match real files. presentation_rect implies "presentation": 1."""
        self._n += 1
        oid = "obj-%d" % self._n
        box: dict = {
            "id": oid,
            "maxclass": maxclass,
            "patching_rect": [float(v) for v in rect],
        }
        if text is not None:
            box["text"] = text
        if varname is not None:
            box["varname"] = varname
        if hidden:
            box["hidden"] = 1
        if presentation_rect is not None:
            box["presentation"] = 1
            box["presentation_rect"] = [float(v) for v in presentation_rect]
        # remaining attrs (numinlets/numoutlets/outlettype/parameter_enable/...)
        for key, value in attrs.items():
            box[key] = value
        self._boxes.append(box)
        return oid

    def line(self, src_id, outlet, dst_id, inlet):
        """Wire src_id:outlet -> dst_id:inlet (a Max patchline)."""
        self._lines.append((src_id, int(outlet), dst_id, int(inlet)))

    def live_param(self, longname, shortname, ptype, mmax, *, enum=None,
                   mmin=None, initial=0, initial_enable=1, unitstyle=0):
        """Return the attr block for a live.* parameter widget.

        Shape copied verbatim from real Max 9 files (quad-spatializer.maxpat):
        live.tab / live.text enum widgets carry parameter_enum + parameter_mmax
        and NO parameter_mmin. mmin is accepted for the numeric widgets the full
        panel will add later. Enforces unique parameter_longname across the patch.
        """
        if longname in self._longnames:
            raise ValueError("duplicate parameter_longname: %r" % longname)
        self._longnames.add(longname)
        valueof = {
            "parameter_longname": longname,
            "parameter_shortname": shortname,
            "parameter_type": ptype,
            "parameter_mmax": mmax,
            "parameter_initial": [initial],
            "parameter_initial_enable": initial_enable,
            "parameter_unitstyle": unitstyle,
        }
        if enum is not None:
            valueof["parameter_enum"] = list(enum)
        if mmin is not None:
            valueof["parameter_mmin"] = mmin
        return {
            "parameter_enable": 1,
            "saved_attribute_attributes": {"valueof": valueof},
        }

    def doc(self) -> dict:
        return {
            "patcher": {
                "fileversion": 1,
                "appversion": dict(APPVERSION),
                "classnamespace": "box",
                "rect": [float(v) for v in self.rect],
                "gridsize": [15.0, 15.0],
                "boxes": [{"box": b} for b in self._boxes],
                "lines": [
                    {"patchline": {
                        "source": [s, so],
                        "destination": [d, di],
                    }}
                    for (s, so, d, di) in self._lines
                ],
                "originid": ORIGINID,
                "dependency_cache": [],
                "autosave": 0,
            }
        }


def serialize(doc: dict) -> str:
    """Deterministic .maxpat text: sorted keys, 2-space indent, trailing NL."""
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


# --------------------------------------------------------------------------- #
# SMOKE patcher                                                               #
# --------------------------------------------------------------------------- #

def build_smoke() -> dict:
    """Build the SOMBRA_SMOKE patcher document.

    Box inventory (creation order == obj id order):
      panel chassis, live.tab (one/two), live.text (PING), textbutton (classic,
      varname smoke_classic), led lamp, readout comment, v8 sombra_ui.js, then
      the OSC out (udpsend + print tee), the two udpreceive->route->msg->v8 in
      paths, the two poll metros with their loadbang starters, and the tab->sel->
      script show/hide->thispatcher, presence-lamp, and ears-readout sub-rigs.
    """
    p = Patch(rect=(120.0, 120.0, 760.0, 520.0))

    # 1. panel — presentation chassis background
    p.box("panel", rect=(30.0, 30.0, 360.0, 210.0),
          presentation_rect=(10.0, 10.0, 360.0, 210.0),
          numinlets=1, numoutlets=0, background=1, mode=0, rounded=8,
          bgcolor=[0.18, 0.19, 0.22, 1.0])

    # 2. live.tab (two items) — parameter widget, drives the classic show/hide
    tab_attrs = p.live_param("sombra_smoke_tab", "tab", 2, 1,
                             enum=["one", "two"], unitstyle=9)
    tab = p.box("live.tab", rect=(50.0, 50.0, 140.0, 40.0),
                presentation_rect=(24.0, 24.0, 140.0, 40.0),
                varname="sombra_smoke_tab",
                numinlets=1, numoutlets=3, outlettype=["", "", "float"],
                **tab_attrs)

    # 3. live.text (PING mode button) — parameter widget, bang -> "ping" -> v8
    ping_attrs = p.live_param("sombra_smoke_ping", "ping", 1, 1,
                              enum=["PING", "PING"], unitstyle=0)
    livetext = p.box("live.text", rect=(210.0, 50.0, 60.0, 22.0),
                     presentation_rect=(180.0, 26.0, 60.0, 24.0),
                     varname="sombra_smoke_ping",
                     text="PING", texton="PING",
                     numinlets=1, numoutlets=2, outlettype=["", ""],
                     **ping_attrs)

    # 4. classic textbutton (fallback-class probe) — tab hides/shows it by varname
    classic = p.box("textbutton", text="classic", varname="smoke_classic",
                    rect=(50.0, 110.0, 90.0, 26.0),
                    presentation_rect=(24.0, 80.0, 90.0, 26.0),
                    numinlets=1, numoutlets=3, outlettype=["", "", "int"],
                    parameter_enable=0)

    # 5. led — presence lamp, driven by the v8 heartbeat path
    led = p.box("led", rect=(210.0, 110.0, 24.0, 24.0),
                presentation_rect=(260.0, 26.0, 24.0, 24.0),
                numinlets=1, numoutlets=1, outlettype=["int"])

    # 6. comment — readout, fed by [prepend set]
    comment = p.box("comment", text="(readout)", rect=(50.0, 160.0, 300.0, 20.0),
                    presentation_rect=(24.0, 130.0, 320.0, 20.0),
                    numinlets=1, numoutlets=0)

    # 7. v8 sombra_ui.js — the protocol glue: 1 inlet, 4 outlets
    v8 = p.box("newobj", text="v8 sombra_ui.js",
               rect=(50.0, 280.0, 150.0, 22.0),
               numinlets=1, numoutlets=4, outlettype=["", "", "", ""])

    # --- outlet 0: fully-qualified OSC -> udpsend (+ print tee) --------------
    udpsend = p.box("newobj", text="udpsend 127.0.0.1 3353",
                    rect=(50.0, 340.0, 170.0, 22.0),
                    numinlets=1, numoutlets=0)
    printobj = p.box("newobj", text="print SMOKE",
                     rect=(240.0, 340.0, 90.0, 22.0),
                     numinlets=1, numoutlets=0)
    p.line(v8, 0, udpsend, 0)
    p.line(v8, 0, printobj, 0)

    # --- inbound: [udpreceive 3354] -> [route /alive] -> "heartbeat" -> v8 ---
    udpr_alive = p.box("newobj", text="udpreceive 3354",
                       rect=(420.0, 50.0, 110.0, 22.0),
                       numinlets=0, numoutlets=1, outlettype=[""])
    route_alive = p.box("newobj", text="route /alive",
                        rect=(420.0, 90.0, 90.0, 22.0),
                        numinlets=1, numoutlets=2, outlettype=["", ""])
    msg_heartbeat = p.box("message", text="heartbeat",
                          rect=(420.0, 130.0, 80.0, 22.0),
                          numinlets=2, numoutlets=1, outlettype=[""])
    p.line(udpr_alive, 0, route_alive, 0)
    p.line(route_alive, 0, msg_heartbeat, 0)
    p.line(msg_heartbeat, 0, v8, 0)

    # --- inbound: [udpreceive 3355] -> [route /sombra/reply] -> prepend -> v8 -
    udpr_reply = p.box("newobj", text="udpreceive 3355",
                       rect=(560.0, 50.0, 110.0, 22.0),
                       numinlets=0, numoutlets=1, outlettype=[""])
    route_reply = p.box("newobj", text="route /sombra/reply",
                        rect=(560.0, 90.0, 140.0, 22.0),
                        numinlets=1, numoutlets=2, outlettype=["", ""])
    prepend_reply = p.box("newobj", text="prepend reply",
                          rect=(560.0, 130.0, 100.0, 22.0),
                          numinlets=1, numoutlets=1, outlettype=[""])
    p.line(udpr_reply, 0, route_reply, 0)
    p.line(route_reply, 0, prepend_reply, 0)
    p.line(prepend_reply, 0, v8, 0)

    # --- poll: [metro 2000] -> "poll_status" -> v8 --------------------------
    metro_poll = p.box("newobj", text="metro 2000",
                       rect=(420.0, 200.0, 80.0, 22.0),
                       numinlets=2, numoutlets=1, outlettype=["bang"])
    msg_poll = p.box("message", text="poll_status",
                     rect=(420.0, 240.0, 90.0, 22.0),
                     numinlets=2, numoutlets=1, outlettype=[""])
    p.line(metro_poll, 0, msg_poll, 0)
    p.line(msg_poll, 0, v8, 0)

    # --- presence tick: [metro 1000] -> "check_presence" -> v8 --------------
    metro_pres = p.box("newobj", text="metro 1000",
                       rect=(560.0, 200.0, 80.0, 22.0),
                       numinlets=2, numoutlets=1, outlettype=["bang"])
    msg_check = p.box("message", text="check_presence",
                      rect=(560.0, 240.0, 110.0, 22.0),
                      numinlets=2, numoutlets=1, outlettype=[""])
    p.line(metro_pres, 0, msg_check, 0)
    p.line(msg_check, 0, v8, 0)

    # --- [loadbang] -> "1" -> both metros' left inlet -----------------------
    loadbang = p.box("newobj", text="loadbang",
                     rect=(420.0, 320.0, 70.0, 22.0),
                     numinlets=1, numoutlets=1, outlettype=["bang"])
    msg_start_poll = p.box("message", text="1",
                           rect=(420.0, 290.0, 32.0, 22.0),
                           numinlets=2, numoutlets=1, outlettype=[""])
    msg_start_pres = p.box("message", text="1",
                           rect=(560.0, 290.0, 32.0, 22.0),
                           numinlets=2, numoutlets=1, outlettype=[""])
    p.line(loadbang, 0, msg_start_poll, 0)
    p.line(loadbang, 0, msg_start_pres, 0)
    p.line(msg_start_poll, 0, metro_poll, 0)
    p.line(msg_start_pres, 0, metro_pres, 0)

    # --- PING: live.text -> "ping" -> v8 ------------------------------------
    msg_ping = p.box("message", text="ping",
                     rect=(210.0, 90.0, 50.0, 22.0),
                     numinlets=2, numoutlets=1, outlettype=[""])
    p.line(livetext, 0, msg_ping, 0)
    p.line(msg_ping, 0, v8, 0)

    # --- tab -> [sel 0 1] -> script show/hide smoke_classic -> thispatcher ---
    sel = p.box("newobj", text="sel 0 1",
                rect=(50.0, 200.0, 60.0, 22.0),
                numinlets=2, numoutlets=3, outlettype=["bang", "bang", ""])
    msg_show = p.box("message", text="script show smoke_classic",
                     rect=(50.0, 236.0, 170.0, 22.0),
                     numinlets=2, numoutlets=1, outlettype=[""])
    msg_hide = p.box("message", text="script hide smoke_classic",
                     rect=(230.0, 236.0, 170.0, 22.0),
                     numinlets=2, numoutlets=1, outlettype=[""])
    thispatcher = p.box("newobj", text="thispatcher",
                        rect=(50.0, 400.0, 80.0, 22.0),
                        numinlets=1, numoutlets=1, outlettype=[""])
    p.line(tab, 0, sel, 0)
    p.line(sel, 0, msg_show, 0)   # item "one" (0) -> show
    p.line(sel, 1, msg_hide, 0)   # item "two" (1) -> hide
    p.line(msg_show, 0, thispatcher, 0)
    p.line(msg_hide, 0, thispatcher, 0)

    # --- presence lamp: v8:1 -> [route presence_on presence_off] -> led -----
    route_presence = p.box("newobj", text="route presence_on presence_off",
                           rect=(210.0, 160.0, 200.0, 22.0),
                           numinlets=1, numoutlets=3, outlettype=["", "", ""])
    msg_lamp_on = p.box("message", text="1",
                        rect=(210.0, 196.0, 32.0, 22.0),
                        numinlets=2, numoutlets=1, outlettype=[""])
    msg_lamp_off = p.box("message", text="0",
                         rect=(250.0, 196.0, 32.0, 22.0),
                         numinlets=2, numoutlets=1, outlettype=[""])
    p.line(v8, 1, route_presence, 0)
    p.line(route_presence, 0, msg_lamp_on, 0)
    p.line(route_presence, 1, msg_lamp_off, 0)
    p.line(msg_lamp_on, 0, led, 0)
    p.line(msg_lamp_off, 0, led, 0)

    # --- readout: v8:2 -> [route ears hands] -> [prepend set] -> comment ----
    route_readout = p.box("newobj", text="route ears hands",
                          rect=(420.0, 400.0, 120.0, 22.0),
                          numinlets=1, numoutlets=3, outlettype=["", "", ""])
    prepend_set = p.box("newobj", text="prepend set",
                        rect=(420.0, 440.0, 90.0, 22.0),
                        numinlets=1, numoutlets=1, outlettype=[""])
    p.line(v8, 2, route_readout, 0)
    p.line(route_readout, 0, prepend_set, 0)  # "ears" matched outlet -> set
    p.line(prepend_set, 0, comment, 0)

    return p.doc()


# --------------------------------------------------------------------------- #
# Contract validator                                                          #
# --------------------------------------------------------------------------- #

def _boxes_by_id(patcher: dict) -> dict:
    out = {}
    for entry in patcher.get("boxes", []):
        box = entry.get("box", {})
        bid = box.get("id")
        if bid:
            out[bid] = box
    return out


def _edges(patcher: dict) -> list:
    out = []
    for entry in patcher.get("lines", []):
        pl = entry.get("patchline", {})
        src = pl.get("source")
        dst = pl.get("destination")
        if isinstance(src, list) and isinstance(dst, list) \
                and len(src) >= 2 and len(dst) >= 2:
            out.append((src[0], src[1], dst[0], dst[1]))
    return out


def _text(box: dict) -> str:
    return str(box.get("text", ""))


def _has_edge(edges, src_ids, dst_id, outlet=None) -> bool:
    for (s, so, d, _di) in edges:
        if s in src_ids and d == dst_id and (outlet is None or so == outlet):
            return True
    return False


def _path(boxes, edges, preds, first_outlet=None) -> bool:
    """True iff a chain of boxes matching preds[0], preds[1], ... exists where
    each consecutive pair is joined by a patchline. first_outlet, if given,
    constrains the outlet of the FIRST hop (preds[0] -> preds[1])."""
    layer = [bid for bid, b in boxes.items() if preds[0](b)]
    if not layer:
        return False
    for i, pred in enumerate(preds[1:]):
        outlet = first_outlet if i == 0 else None
        nxt = []
        for bid, b in boxes.items():
            if pred(b) and _has_edge(edges, layer, bid, outlet):
                nxt.append(bid)
        if not nxt:
            return False
        layer = nxt
    return True


def contract_report(doc: dict, smoke: bool = True) -> list:
    """Pure, importable. Return a list of human-readable findings ([] == pass).

    Tolerant of Max-resaved files (extra fields, float variance): checks presence
    and wiring by object text / maxclass, never exact byte layout.
    """
    findings: list[str] = []

    if not isinstance(doc, dict) or "patcher" not in doc:
        return ["no top-level 'patcher' object"]
    patcher = doc["patcher"]
    if not isinstance(patcher, dict):
        return ["'patcher' is not an object"]
    if "appversion" not in patcher:
        findings.append("patcher missing 'appversion'")
    if not isinstance(patcher.get("boxes"), list):
        findings.append("patcher missing 'boxes' list")
    if not isinstance(patcher.get("lines"), list):
        findings.append("patcher missing 'lines' list")
    if findings:
        return findings

    boxes = _boxes_by_id(patcher)
    edges = _edges(patcher)

    # predicates ----------------------------------------------------------------
    def is_v8(b):
        return b.get("maxclass") == "newobj" and _text(b).startswith("v8 ") \
            and "sombra_ui.js" in _text(b)

    def txt_eq(s):
        return lambda b: _text(b) == s

    def txt_starts(s):
        return lambda b: _text(b).startswith(s)

    def cls(name):
        return lambda b: b.get("maxclass") == name

    # exactly one v8 referencing sombra_ui.js, with 4 outlets -------------------
    v8s = [bid for bid, b in boxes.items() if is_v8(b)]
    if len(v8s) != 1:
        findings.append(
            "expected exactly one v8 sombra_ui.js box, found %d" % len(v8s))
        return findings  # everything below hangs off the v8
    v8_id = v8s[0]
    if boxes[v8_id].get("numoutlets") != 4:
        findings.append("v8 sombra_ui.js must have 4 outlets, has %r"
                        % boxes[v8_id].get("numoutlets"))

    # v8:0 -> udpsend 127.0.0.1 3353  AND  v8:0 -> print ------------------------
    udpsend_ids = [bid for bid, b in boxes.items()
                   if _text(b) == "udpsend 127.0.0.1 3353"]
    if not udpsend_ids:
        findings.append("no [udpsend 127.0.0.1 3353] box")
    elif not _has_edge(edges, {v8_id}, udpsend_ids[0], outlet=0):
        findings.append("v8 outlet 0 is not wired to [udpsend 127.0.0.1 3353]")
    print_ids = [bid for bid, b in boxes.items() if txt_starts("print")(b)]
    if not print_ids:
        findings.append("no [print ...] box")
    elif not any(_has_edge(edges, {v8_id}, pid, outlet=0) for pid in print_ids):
        findings.append("v8 outlet 0 is not tee'd to a [print] box")

    # udpreceive 3354 -> route /alive -> "heartbeat" -> v8 ---------------------
    if not _path(boxes, edges,
                 [txt_eq("udpreceive 3354"), txt_eq("route /alive"),
                  txt_eq("heartbeat"), is_v8]):
        findings.append(
            "broken path: udpreceive 3354 -> route /alive -> 'heartbeat' -> v8")

    # udpreceive 3355 -> route /sombra/reply -> prepend reply -> v8 ------------
    if not _path(boxes, edges,
                 [txt_eq("udpreceive 3355"), txt_eq("route /sombra/reply"),
                  txt_eq("prepend reply"), is_v8]):
        findings.append(
            "broken path: udpreceive 3355 -> route /sombra/reply -> "
            "prepend reply -> v8")

    # metro 2000 -> "poll_status" -> v8 ----------------------------------------
    if not _path(boxes, edges,
                 [txt_eq("metro 2000"), txt_eq("poll_status"), is_v8]):
        findings.append("broken path: metro 2000 -> 'poll_status' -> v8")

    # metro 1000 -> "check_presence" -> v8 -------------------------------------
    if not _path(boxes, edges,
                 [txt_eq("metro 1000"), txt_eq("check_presence"), is_v8]):
        findings.append("broken path: metro 1000 -> 'check_presence' -> v8")

    # loadbang -> "1" -> both metros -------------------------------------------
    if not _path(boxes, edges,
                 [txt_eq("loadbang"), txt_eq("1"), txt_eq("metro 2000")]):
        findings.append("loadbang does not start [metro 2000] via a '1' message")
    if not _path(boxes, edges,
                 [txt_eq("loadbang"), txt_eq("1"), txt_eq("metro 1000")]):
        findings.append("loadbang does not start [metro 1000] via a '1' message")

    # live.tab -> sel -> script show/hide -> thispatcher, target varname exists -
    if not _path(boxes, edges,
                 [cls("live.tab"), txt_starts("sel "),
                  txt_starts("script show"), txt_eq("thispatcher")]):
        findings.append(
            "broken path: live.tab -> sel -> 'script show ...' -> thispatcher")
    if not _path(boxes, edges,
                 [cls("live.tab"), txt_starts("sel "),
                  txt_starts("script hide"), txt_eq("thispatcher")]):
        findings.append(
            "broken path: live.tab -> sel -> 'script hide ...' -> thispatcher")
    # the varname named by the script messages must be a real box varname
    script_targets = set()
    for b in boxes.values():
        t = _text(b)
        if t.startswith("script show ") or t.startswith("script hide "):
            script_targets.add(t.split()[-1])
    varnames = {b.get("varname") for b in boxes.values() if b.get("varname")}
    for target in script_targets:
        if target not in varnames:
            findings.append(
                "script message targets varname %r but no box declares it"
                % target)

    # presence lamp: v8:1 -> route presence... -> led --------------------------
    route_pres = [bid for bid, b in boxes.items()
                  if b.get("maxclass") == "newobj" and "presence" in _text(b)
                  and _text(b).startswith("route")]
    if not route_pres:
        findings.append("no [route presence_on presence_off] box")
    elif not _has_edge(edges, {v8_id}, route_pres[0], outlet=1):
        findings.append("v8 outlet 1 is not wired to the presence [route] box")
    else:
        if not _path(boxes, edges,
                     [txt_starts("route presence"), lambda b: True, cls("led")]):
            findings.append(
                "presence route does not reach an [led] (via its '1'/'0' msgs)")

    # readout: v8:2 -> route ears hands -> prepend set -> comment --------------
    route_ears = [bid for bid, b in boxes.items()
                  if b.get("maxclass") == "newobj"
                  and _text(b).startswith("route ") and "ears" in _text(b)]
    if not route_ears:
        findings.append("no [route ears ...] box")
    elif not _has_edge(edges, {v8_id}, route_ears[0], outlet=2):
        findings.append("v8 outlet 2 is not wired to the ears [route] box")
    if not _path(boxes, edges,
                 [txt_starts("route ears"), txt_eq("prepend set"),
                  cls("comment")]):
        findings.append(
            "broken path: route ears -> [prepend set] -> comment")

    # every live.* box: parameter_enable present, unique parameter_longname ----
    seen_longnames: dict = {}
    for bid, b in boxes.items():
        mc = b.get("maxclass", "")
        if not mc.startswith("live."):
            continue
        if "parameter_enable" not in b:
            findings.append("%s box %s missing 'parameter_enable'" % (mc, bid))
        valueof = (b.get("saved_attribute_attributes") or {}).get("valueof") or {}
        longname = valueof.get("parameter_longname")
        if not longname:
            findings.append("%s box %s missing parameter_longname" % (mc, bid))
            continue
        if longname in seen_longnames:
            findings.append("duplicate parameter_longname %r (boxes %s, %s)"
                            % (longname, seen_longnames[longname], bid))
        else:
            seen_longnames[longname] = bid

    return findings


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def _write_smoke(out_path: str) -> int:
    print("building SOMBRA_SMOKE patcher...")
    doc = build_smoke()
    text = serialize(doc)

    print("validating in-process before writing...")
    findings = contract_report(doc, smoke=True)
    if findings:
        print("REFUSING TO WRITE — contract findings:", file=sys.stderr)
        for f in findings:
            print("  - %s" % f, file=sys.stderr)
        return 1

    # determinism guard: rebuild + reserialize must be byte-identical
    if serialize(build_smoke()) != text:
        print("REFUSING TO WRITE — double build is not byte-identical",
              file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    n_boxes = len(doc["patcher"]["boxes"])
    n_lines = len(doc["patcher"]["lines"])
    print("wrote %s (%d boxes, %d patchlines)" % (out_path, n_boxes, n_lines))
    print("contract: clean")
    return 0


def _validate(path: str) -> int:
    print("validating %s ..." % path)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as exc:
        print("cannot read %s: %s" % (path, exc), file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print("%s is not valid JSON: %s" % (path, exc), file=sys.stderr)
        return 1
    findings = contract_report(doc, smoke=True)
    if findings:
        print("FINDINGS (%d):" % len(findings))
        for f in findings:
            print("  - %s" % f)
        return 1
    print("contract: clean")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate / validate the SOMBRA Max 9 patcher (.maxpat).")
    ap.add_argument("--smoke", action="store_true",
                    help="write the SMOKE patcher (default out: %s)" % DEFAULT_OUT)
    ap.add_argument("--validate", metavar="PATH",
                    help="run contract_report on a .maxpat and print findings")
    ap.add_argument("--out", metavar="PATH", default=None,
                    help="override the output path for --smoke")
    args = ap.parse_args()

    if args.validate:
        return _validate(args.validate)
    if args.smoke:
        return _write_smoke(args.out or DEFAULT_OUT)

    print("full panel not implemented yet — run --smoke", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
