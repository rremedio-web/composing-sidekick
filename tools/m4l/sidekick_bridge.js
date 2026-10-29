// SIDEKICK BRIDGE — dumb LiveAPI executor for the composing sidekick.
// v8 object, Live 12.2+ (Max 9). No logic, no clock, no rng: it receives ops,
// runs them against the Live Object Model, and replies. Delete the device to
// disable everything.
//
// Patcher wiring (SIDEKICK_BRIDGE.amxd, MIDI effect):
//   [udpreceive 3351] -> [v8 sidekick_bridge.js] -> [udpsend 127.0.0.1 3352]
// Transport: OSC message "/sk <base64url(JSON)>"; reply mirrors the shape.
// Batch aborts on first error. insert_steps_last targets the envelope created by
// the most recent create_envelope (state survives across datagrams).
//
// Policy: writes are limited to SIDEKICK tracks. Clip ops that take a raw Live
// clip id (create_envelope, clear_envelopes, set_clip_groove, clear_clip_groove)
// resolve the clip's owning track and refuse unless its name is "SIDEKICK" or
// starts with "SIDEKICK " (the same rule as clip_mutation._is_sidekick_name).
//
// Network: the bridge has no authentication. Whoever can send a UDP datagram to
// port 3351 can run these ops. The listening address is set by [udpreceive] in
// the .amxd patcher, not by this script, and nothing in this repo limits it to
// localhost: treat it as reachable from the network. Do not load the device on
// a shared or untrusted network.

autowatch = 1;

var B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
var lastEnvelopeId = 0;
// Capture state is intentionally local to this device.  It never resolves or
// touches a source track: the only Live objects below are the named Ears track
// and its requested target slot.
var capture = null;
var captureHistory = Object.create(null);
var captureHistoryOrder = [];
var MAX_CAPTURE_HISTORY = 64;

function rememberCapture(c) {
    if (!captureHistory[c.capture_id]) captureHistoryOrder.push(c.capture_id);
    captureHistory[c.capture_id] = c;
    while (captureHistoryOrder.length > MAX_CAPTURE_HISTORY) {
        var victim = captureHistoryOrder[0];
        if (capture && victim === capture.capture_id) {
            captureHistoryOrder.shift(); captureHistoryOrder.push(victim);
            continue;
        }
        captureHistoryOrder.shift();
        delete captureHistory[victim];
    }
}

function captureError(msg) { throw new Error(String(msg).replace(/(?:[A-Za-z]:)?[\\/][^ ]+/g, "capture operation failed")); }
function captureId(op) {
    if (typeof op.capture_id !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/.test(op.capture_id)) captureError("invalid capture id");
    return op.capture_id;
}
function captureInt(value, label) {
    if (typeof value !== "number" || !isFinite(value) || Math.floor(value) !== value || value < 0) captureError("invalid " + label);
    return value;
}
function captureTarget(op) {
    var ti = op.track_index;
    if (ti === undefined) ti = op.target_track_index;
    var si = op.scene_index;
    if (si === undefined) si = op.target_scene;
    ti = captureInt(ti, "target track"); si = captureInt(si, "target scene");
    var track = new LiveAPI("live_set tracks " + ti);
    var name = String(track.get("name"));
    if (name !== "SIDEKICK EARS") captureError("target track policy");
    if (op.target_track_name !== undefined && String(op.target_track_name) !== "SIDEKICK EARS") captureError("target track policy");
    return { track_index: ti, scene_index: si, track: track,
             slot: new LiveAPI("live_set tracks " + ti + " clip_slots " + si) };
}
function captureHasClip(slot) {
    var has = slot.get("has_clip");
    return has === 1 || has === true || String(has) === "1" || String(has).toLowerCase() === "true";
}
function captureClipId(slot) {
    try {
        var raw = slot.get("clip");
        var id = parseInt(String(raw).replace(/\D/g, ""), 10);
        return isNaN(id) || id <= 0 ? null : id;
    } catch (e) { return null; }
}
function captureArm(track, value) {
    if (Array.isArray(value) && value.length === 1) value = value[0];
    track.set("arm", value ? 1 : 0);
}
function captureRecordBeats(durationMs) {
    var tempo = parseFloat(String(new LiveAPI("live_set").get("tempo")));
    if (!isFinite(tempo) || tempo <= 0) captureError("invalid tempo");
    return durationMs * tempo / 60000;
}
function captureFreeTask(c) {
    try { if (c.task && c.task.freepeer) c.task.freepeer(); } catch (e) { /* best effort */ }
    c.task = null;
}
function releaseCapturePeers(c) {
    c.track = null;
    c.slot = null;
}
function captureCleanup(c, removePartial) {
    var receipt = { task_cancelled: false, stopped: false, deleted: false, arm_restored: false };
    if (c.task && c.pending) {
        try { c.task.cancel(); receipt.task_cancelled = true; }
        catch (e) { receipt.task_cancel_error = "task cleanup failed"; }
        c.pending = false;
        captureFreeTask(c);
    }
    try { c.track.call("stop_all_clips"); receipt.stopped = true; } catch (e2) { receipt.stop_error = "stop cleanup failed"; }
    if (removePartial && c.slot && c.originally_empty) {
        try {
            if (captureHasClip(c.slot)) { c.slot.call("delete_clip"); receipt.deleted = true; }
        } catch (e3) { receipt.delete_error = "clip cleanup failed"; }
    }
    try { captureArm(c.track, c.prior_arm); receipt.arm_restored = true; } catch (e4) { receipt.arm_restore_error = "arm cleanup failed"; }
    return receipt;
}
function captureStatus(c) {
    return { capture_id: c.capture_id, state: c.state };
}
function captureStart(op) {
    var cid = captureId(op);
    var duration = captureInt(op.duration_ms, "duration");
    if (!duration) captureError("duration must be positive");
    // Reject source instructions at this low-level boundary, even if a future
    // caller happens to supply otherwise harmless values.
    if (op.source_track !== undefined || op.source_scene !== undefined || op.source_track_index !== undefined || op.source_scene_index !== undefined || op.launch_source_scene !== undefined) captureError("source target is not allowed");
    var target = captureTarget(op);
    if (capture && capture.state === "recording") {
        if (capture.capture_id === cid && capture.track_index === target.track_index && capture.scene_index === target.scene_index && capture.duration_ms === duration) return captureStatus(capture);
        captureError("another capture is active");
    }
    if (captureHistory[cid]) {
        var prior = captureHistory[cid];
        if (prior.track_index !== target.track_index || prior.scene_index !== target.scene_index || prior.duration_ms !== duration) captureError("conflicting capture retry");
        return captureStatus(prior);
    }
    if (captureHasClip(target.slot)) captureError("target slot is occupied");
    var c = { capture_id: cid, track_index: target.track_index, scene_index: target.scene_index,
              duration_ms: duration, track: target.track, slot: target.slot,
              originally_empty: true, prior_arm: target.track.get("arm"), pending: true, state: "recording" };
    capture = c; rememberCapture(c);
    try {
        captureArm(c.track, true);
        c.slot.call("fire", captureRecordBeats(duration), 0);
        c.task = new Task(function() {
            try {
                // The state flips only after stop and arm restoration complete.
                c.track.call("stop_all_clips");
                c.pending = false;
                captureArm(c.track, c.prior_arm);
                c.state = "ready";
                c.task_needs_free = true;
            } catch (e) {
                c.pending = false;
                c.cleanup = captureCleanup(c, true);
                c.state = "failed";
                c.task_needs_free = true;
            }
        }, this);
        // Fixed-length recording commits the clip at `duration`; stop on the
        // following scheduler tick so finalize never races that commit.
        c.task.schedule(duration + 250);
        return captureStatus(c);
    } catch (e2) {
        c.cleanup = captureCleanup(c, true);
        c.state = "failed";
        releaseCapturePeers(c);
        captureError("capture start failed");
    }
}
function captureStatusOp(op) {
    var cid = captureId(op);
    var c = captureHistory[cid];
    if (!c) captureError("unknown capture");
    var out = captureStatus(c);
    if (c.task_needs_free) { captureFreeTask(c); c.task_needs_free = false; }
    if ((c.state === "finalized" || c.state === "cancelled" || c.state === "failed") && !c.pending && !c.task) releaseCapturePeers(c);
    return out;
}
function captureFinalize(op) {
    var cid = captureId(op), c = captureHistory[cid];
    if (!c) captureError("unknown capture");
    if (c.state === "finalized") return { capture_id: cid, path: "t" + c.track_index + "/s" + c.scene_index, clip_id: c.clip_id, state: c.state, cleanup: c.cleanup || {} };
    if (c.state !== "ready") captureError("capture is not ready");
    try {
        if (c.task_needs_free) { captureFreeTask(c); c.task_needs_free = false; }
        // Read back only after the scheduled stop callback has completed.
        if (!captureHasClip(c.slot)) captureError("capture clip is missing");
        c.clip_id = captureClipId(c.slot);
        if (c.clip_id === null) captureError("capture clip id is missing");
        c.cleanup = { arm_restored: true, task_cancelled: false, stopped: true };
        c.state = "finalized";
        var finalized = { capture_id: cid, path: "t" + c.track_index + "/s" + c.scene_index, clip_id: c.clip_id, state: c.state, cleanup: c.cleanup };
        releaseCapturePeers(c);
        return finalized;
    } catch (e) {
        c.cleanup = captureCleanup(c, true);
        c.state = "failed";
        releaseCapturePeers(c);
        captureError("capture finalize failed");
    }
}
function captureCancel(op) {
    var cid = captureId(op), c = captureHistory[cid];
    if (!c) captureError("unknown capture");
    if (c.state === "cancelled") return { capture_id: cid, state: c.state, cleanup: c.cleanup || {} };
    if (c.state === "finalized") captureError("finalized capture cannot be cancelled");
    if (c.state === "failed" && c.cleanup) {
        if (c.task_needs_free) { captureFreeTask(c); c.task_needs_free = false; }
        releaseCapturePeers(c);
        return { capture_id: cid, state: c.state, cleanup: c.cleanup };
    }
    c.cleanup = captureCleanup(c, true);
    if (c.task_needs_free) { captureFreeTask(c); c.task_needs_free = false; }
    c.state = "cancelled";
    var cancelled = { capture_id: cid, state: c.state, cleanup: c.cleanup };
    releaseCapturePeers(c);
    return cancelled;
}

function b64urlEncode(str) {
    var out = "", i = 0;
    var bytes = [];
    for (var c = 0; c < str.length; c++) {
        var code = str.charCodeAt(c);
        if (code < 128) bytes.push(code);
        else if (code < 2048) { bytes.push(192 | (code >> 6), 128 | (code & 63)); }
        else { bytes.push(224 | (code >> 12), 128 | ((code >> 6) & 63), 128 | (code & 63)); }
    }
    while (i < bytes.length) {
        var b0 = bytes[i++], b1 = i < bytes.length ? bytes[i++] : NaN, b2 = i < bytes.length ? bytes[i++] : NaN;
        out += B64.charAt(b0 >> 2);
        out += B64.charAt(((b0 & 3) << 4) | (isNaN(b1) ? 0 : b1 >> 4));
        if (!isNaN(b1)) out += B64.charAt(((b1 & 15) << 2) | (isNaN(b2) ? 0 : b2 >> 6));
        if (!isNaN(b2)) out += B64.charAt(b2 & 63);
    }
    return out;
}

function b64urlDecode(tok) {
    var bytes = [], buf = 0, bits = 0;
    for (var i = 0; i < tok.length; i++) {
        var v = B64.indexOf(tok.charAt(i));
        if (v < 0) continue;
        buf = (buf << 6) | v; bits += 6;
        if (bits >= 8) { bits -= 8; bytes.push((buf >> bits) & 255); }
    }
    var out = "", j = 0;
    while (j < bytes.length) {
        var b = bytes[j++];
        if (b < 128) out += String.fromCharCode(b);
        else if (b >= 192 && b < 224) out += String.fromCharCode(((b & 31) << 6) | (bytes[j++] & 63));
        else out += String.fromCharCode(((b & 15) << 12) | ((bytes[j++] & 63) << 6) | (bytes[j++] & 63));
    }
    return out;
}

function idOf(api) {
    return parseInt(String(api.id).replace(/\D/g, ""), 10);
}

// Resolve a raw clip id to a LiveAPI clip that lives on a SIDEKICK track, or throw.
function sidekickClip(clipId) {
    var raw = String(clipId);
    if (!/^[0-9]+$/.test(raw) || parseInt(raw, 10) <= 0) throw new Error("policy: invalid clip id");
    var clip = new LiveAPI("id " + raw);
    var path = String(clip.unquotedpath || clip.path || "").replace(/^"|"$/g, "");
    var m = /^live_set tracks ([0-9]+) (?:clip_slots [0-9]+ clip|arrangement_clips [0-9]+)$/.exec(path);
    if (!m) throw new Error("policy: clip " + raw + " is not a clip on a track");
    var name = String(new LiveAPI("live_set tracks " + m[1]).get("name"));
    if (name !== "SIDEKICK" && name.indexOf("SIDEKICK ") !== 0) {
        throw new Error("policy: clip " + raw + " is not on a SIDEKICK track");
    }
    return clip;
}

function doOp(op) {
    switch (op.op) {
        case "capture_start": return captureStart(op);
        case "capture_status": return captureStatusOp(op);
        case "capture_finalize": return captureFinalize(op);
        case "capture_cancel": return captureCancel(op);
        case "ping":
            return { live: String(new LiveAPI("live_app").call("get_major_version")) };
        case "assert_track_name": {
            var t = new LiveAPI("live_set tracks " + op.track_index);
            var nm = String(t.get("name"));
            if (nm !== op.expect) throw new Error("policy: track " + op.track_index + " is '" + nm + "', not '" + op.expect + "'");
            return { track: nm };
        }
        case "call": {
            var match = /^live_set tracks ([0-9]+) clip_slots ([0-9]+)$/.exec(String(op.path || ""));
            if (!match || op.method !== "delete_clip" || !Array.isArray(op.args) || op.args.length !== 0) {
                throw new Error("policy: bridge call is restricted to deleting a SIDEKICK EARS clip");
            }
            var callTrack = new LiveAPI("live_set tracks " + match[1]);
            if (String(callTrack.get("name")) !== "SIDEKICK EARS") {
                throw new Error("policy: bridge call target is not SIDEKICK EARS");
            }
            var callSlot = new LiveAPI(String(op.path));
            callSlot.call("delete_clip");
            return { deleted: true };
        }
        case "get": {
            var g = new LiveAPI(op.path);
            return { value: g.get(op.prop) };
        }
        case "get_id": {
            var gi = new LiveAPI(op.path);
            return { id: idOf(gi) };
        }
        case "list_params": {
            var out = [];
            var tr = new LiveAPI("live_set tracks " + op.track_index);
            var nDev = tr.getcount("devices");
            for (var d = 0; d < nDev; d++) {
                var dev = new LiveAPI("live_set tracks " + op.track_index + " devices " + d);
                var entry = { device_index: d, class_name: String(dev.get("class_name")),
                              name: String(dev.get("name")), params: [] };
                var nPar = dev.getcount("parameters");
                for (var p = 0; p < nPar; p++) {
                    var par = new LiveAPI("live_set tracks " + op.track_index + " devices " + d + " parameters " + p);
                    entry.params.push({ index: p, name: String(par.get("name")),
                                        id: idOf(par),
                                        min: parseFloat(par.get("min")), max: parseFloat(par.get("max")) });
                }
                out.push(entry);
            }
            return { devices: out };
        }
        case "create_envelope": {
            var clip = sidekickClip(op.clip_id);
            var envId = clip.call("create_automation_envelope", "id " + op.param_id);
            lastEnvelopeId = parseInt(String(envId).replace(/\D/g, ""), 10);
            if (!lastEnvelopeId) throw new Error("create_automation_envelope returned " + envId + " (arrangement clip or bad param?)");
            return { envelope_id: lastEnvelopeId, label: op.label || "" };
        }
        case "insert_steps_last": {
            if (!lastEnvelopeId) throw new Error("no envelope created yet");
            var env = new LiveAPI("id " + lastEnvelopeId);
            for (var k = 0; k < op.points.length; k++) {
                var pt = op.points[k];
                env.call("insert_step", pt[0], pt[1], pt[2]);
            }
            return { inserted: op.points.length, envelope_id: lastEnvelopeId, label: op.label || "" };
        }
        case "clear_envelopes": {
            var c = sidekickClip(op.clip_id);
            c.call("clear_all_envelopes");
            return { cleared: true };
        }
        case "list_grooves": {
            var pool = new LiveAPI("live_set groove_pool");
            var n = pool.getcount("grooves");
            var grooves = [];
            for (var gidx = 0; gidx < n; gidx++) {
                var gr = new LiveAPI("live_set groove_pool grooves " + gidx);
                grooves.push({ index: gidx, id: idOf(gr), name: String(gr.get("name")) });
            }
            return { grooves: grooves };
        }
        case "set_clip_groove": {
            var c2 = sidekickClip(op.clip_id);
            c2.set("groove", ["id", op.groove_id]);
            return { set: true };
        }
        case "clear_clip_groove": {
            var c3 = sidekickClip(op.clip_id);
            c3.set("groove", ["id", 0]);
            return { cleared: true };
        }
        default:
            throw new Error("unknown op " + op.op);
    }
}

function reply(obj) {
    outlet(0, "/sk", b64urlEncode(JSON.stringify(obj)));
}

function anything() {
    var args = arrayfromargs(messagename, arguments);
    if (String(args[0]) !== "/sk" || args.length < 2) return;
    var req;
    try { req = JSON.parse(b64urlDecode(String(args[1]))); }
    catch (e) { reply({ id: null, ok: false, error: "bad payload: " + e.message }); return; }
    var results = [], ok = true;
    for (var i = 0; i < (req.ops || []).length; i++) {
        try { results.push(doOp(req.ops[i])); }
        catch (e) { ok = false; results.push({ error: String(e.message || e), op_index: i }); break; }
    }
    reply({ id: req.id, part: req.part, ok: ok, results: results });
}
