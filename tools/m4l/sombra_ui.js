// SOMBRA UI — device-side protocol glue for the three-tab pedalboard chassis.
// v8 object, Live 12.2+ (Max 9). ES5 only. This file is LOGIC for a future
// SOMBRA.amxd; it cannot run headless — wire it in Max as documented below.
//
// PURPOSE
//   Map the FINISH / JAM / HANDS tab controls to the documented /sombra/* OSC
//   addresses on UDP 3353; poll /sombra/status for ears + hands readout lines;
//   drive the presence lamp from the MCP server's /alive heartbeat on UDP 3354.
//
// PATCHER WIRING (SOMBRA.amxd — not tracked in-repo yet)
//
//   INLET 0 (anything): the ONLY inlet — every message arrives here via
//     anything(): tab buttons, the heartbeat, replies, and the poll tick.
//     tab button events from message boxes / live.text:
//       "deal" | "answer <gear>" | "keep" | "drop" | "lay" | "endings"
//       "ping" | "sticky <0|1|toggle>" | "audition <0|1|toggle>"
//       "reset_ears" | "poll_status"
//     from the network (via the [route] objects below):
//       "heartbeat" | "reply <payload>"
//
//   OUTLET 0: fully-qualified OSC messages -> [udpsend 127.0.0.1 3353]
//     addresses are emitted already qualified (NO [prepend /sombra]); wire
//     outlet 0 straight to udpsend. e.g. "/sombra/deal", "/sombra/answer 2",
//     "/sombra/sticky toggle", "/sombra/ears/audition toggle"
//
//   OUTLET 1: lamp state symbols for UI color widgets
//     "presence_on" | "presence_off" | "ears_live" | "ears_blind"
//     "sticky_on" | "sticky_off" | "audition_on" | "audition_off"
//
//   OUTLET 2: readout text — two tagged messages from device_readout,
//     "ears <ears_line>" then "hands <hands_line>", for [route ears hands]
//
//   OUTLET 3: gear microcopy — six one-liners from /sombra/status gear_names
//     sent as JSON string when status poll succeeds
//
//   [udpreceive 3354] -> [route /alive] -> "heartbeat" -> inlet 0
//   [udpreceive 3355] -> [route /sombra/reply] -> "reply <payload>" -> inlet 0
//     (the listener sends every /sombra/reply to the fixed reply port 3355)
//
//   [metro <poll_ms>] -> inlet 0 "poll_status"  (suggest 2000 ms while session up)
//
// HOUSE STYLE: matches tools/m4l/sidekick_bridge.js — dumb glue, no taste logic.

autowatch = 1;
inlets = 1;
outlets = 4;

var EVENT_HOST = "127.0.0.1";
var EVENT_PORT = 3353;
var LAMP_PORT = 3354;
var HEARTBEAT_TIMEOUT_MS = 5000;
var POLL_MS = 2000;

var presenceAlive = false;
var lastHeartbeatMs = 0;
var earsLive = false;
var stickyOn = false;
var earsLine = "";
var handsLine = "";

// --- OSC helpers (outlet 0 carries address + args for prepend/udpsend) ---------

function sendOsc(address) {
    var args = arrayfromargs(arguments);
    args.shift();
    if (args.length === 0) {
        outlet(0, address);
    } else if (args.length === 1) {
        outlet(0, address, args[0]);
    } else {
        outlet(0, address, args);
    }
}

function emitLamp(symbol) {
    outlet(1, symbol);
}

function emitReadout() {
    // two tagged messages so [route ears hands] can split them — a bare
    // list of space-containing symbols can't be routed downstream.
    outlet(2, "ears", earsLine);
    outlet(2, "hands", handsLine);
}

// --- presence lamp: /alive on UDP 3354 from sombra_listener -------------------

function markHeartbeat() {
    lastHeartbeatMs = Date.now();
    if (!presenceAlive) {
        presenceAlive = true;
        emitLamp("presence_on");
    }
}

function checkPresence() {
    var stale = (Date.now() - lastHeartbeatMs) > HEARTBEAT_TIMEOUT_MS;
    if (presenceAlive && stale) {
        presenceAlive = false;
        emitLamp("presence_off");
    }
}

// --- status poll: /sombra/status reply JSON on /sombra/reply ------------------

function applyStatusPayload(raw) {
    var data;
    try {
        data = JSON.parse(String(raw));
    } catch (e) {
        return;
    }
    if (!data || typeof data !== "object") {
        return;
    }
    if (data.ears_line) {
        earsLine = String(data.ears_line);
        earsLive = earsLine.indexOf("no ears") !== 0;
        emitLamp(earsLive ? "ears_live" : "ears_blind");
    }
    if (data.hands_line) {
        handsLine = String(data.hands_line);
        // hold on iff the line starts with "holding" (mid-hold) or
        // "ready to hold" (armed); "not holding" / "hold off" mean off.
        stickyOn = handsLine.indexOf("holding") === 0 ||
                   handsLine.indexOf("ready to hold") === 0;
        emitLamp(stickyOn ? "sticky_on" : "sticky_off");
    }
    if (typeof data.audition === "boolean") {
        emitLamp(data.audition ? "audition_on" : "audition_off");
    }
    if (data.gear_names) {
        outlet(3, JSON.stringify(data.gear_names));
    }
    emitReadout();
}

function pollStatus() {
    sendOsc("/sombra/status");
}

// --- tab button routing (inlet 0) -----------------------------------------------

function routeButton(cmd) {
    var parts = String(cmd).trim().split(/\s+/);
    var verb = parts[0];
    var arg = parts.length > 1 ? parts[1] : null;

    if (verb === "deal") {
        sendOsc("/sombra/deal");
        return;
    }
    if (verb === "answer") {
        sendOsc("/sombra/answer", parseInt(arg, 10) || 2);
        return;
    }
    if (verb === "keep") {
        sendOsc("/sombra/keep");
        return;
    }
    if (verb === "drop") {
        sendOsc("/sombra/drop");
        return;
    }
    if (verb === "lay") {
        sendOsc("/sombra/lay");
        return;
    }
    if (verb === "endings") {
        sendOsc("/sombra/endings");
        return;
    }
    if (verb === "ping") {
        sendOsc("/sombra/ping", "sombra");
        return;
    }
    if (verb === "sticky") {
        sendOsc("/sombra/sticky", arg || "toggle");
        return;
    }
    if (verb === "audition") {
        sendOsc("/sombra/ears/audition", arg || "toggle");
        return;
    }
    if (verb === "reset_ears") {
        sendOsc("/sombra/ears/reset");
        return;
    }
    if (verb === "poll_status") {
        pollStatus();
        return;
    }
}

// --- Max message handlers -------------------------------------------------------

function bang() {
    pollStatus();
}

function heartbeat() {
    markHeartbeat();
}

function reply() {
    // Max routes "reply <payload>" here (named handlers shadow anything()),
    // so this must mirror anything()'s reply branch: payload only, no selector.
    var msgArgs = arrayfromargs(arguments);
    if (msgArgs.length >= 1) {
        if (typeof msgArgs[0] === "string" && msgArgs[0].charAt(0) === "{") {
            applyStatusPayload(msgArgs[0]);
        } else {
            applyStickyReply(msgArgs[0]);
        }
    }
}

function applyStickyReply(text) {
    // sticky_announcement replies all start "hold on" (armed/mid-hold) or
    // "hold off"; other replies (ears audition/reset) are handled elsewhere.
    var s = String(text);
    if (s.indexOf("hold on") === 0) {
        handsLine = s;
        stickyOn = true;
        emitLamp("sticky_on");
        emitReadout();
    } else if (s.indexOf("hold off") === 0) {
        handsLine = s;
        stickyOn = false;
        emitLamp("sticky_off");
        emitReadout();
    }
}

function anything() {
    var head = String(messagename);
    var msgArgs = arrayfromargs(arguments);

    if (head === "heartbeat") {
        markHeartbeat();
        return;
    }
    if (head === "reply" && msgArgs.length >= 1) {
        if (typeof msgArgs[0] === "string" && msgArgs[0].charAt(0) === "{") {
            applyStatusPayload(msgArgs[0]);
        } else {
            applyStickyReply(msgArgs[0]);
        }
        return;
    }
    if (head === "poll_status") {
        pollStatus();
        return;
    }
    // rebuild the full command: v8 splits "answer 3" into selector + args, so
    // routeButton must see "answer 3", not just "answer" (which loses the gear).
    var full = head;
    if (msgArgs.length > 0) {
        full = head + " " + msgArgs.join(" ");
    }
    routeButton(full);
}

// --- housekeeping metro (call from patcher: t check_presence) -------------------

function check_presence() {
    checkPresence();
}

function set_poll_ms(ms) {
    POLL_MS = Math.max(500, parseInt(ms, 10) || POLL_MS);
}
