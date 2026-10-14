import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../tools/m4l/sidekick_bridge.js", import.meta.url), "utf8");

function load({occupied = false, clipId = 901, initialArm = 0} = {}) {
  const calls = [];
  const tasks = [];
  let hasClip = occupied;
  let trackArm = initialArm;
  class LiveAPI {
    constructor(path) { this.path = String(path); this.id = this.path; calls.push(["construct", this.path]); }
    get(prop) {
      calls.push(["get", this.path, prop]);
      if (this.path === "live_app" && prop === "name") return "Live";
      if (this.path === "live_set" && prop === "tempo") return [120];
      if (this.path.includes("live_set tracks 9") && prop === "name") return "SIDEKICK EARS";
      if (this.path.includes("live_set tracks 4") && prop === "name") return "SOURCE";
      if (this.path.includes("tracks 9") && prop === "arm") return [trackArm];
      if (this.path.includes("clip_slots 2") && prop === "has_clip") return hasClip ? 1 : 0;
      if (this.path.includes("clip_slots 2") && prop === "clip") return `id ${clipId}`;
      if (prop === "class_name") return "Dummy";
      if (prop === "name") return "Dummy";
      if (prop === "min") return 0;
      if (prop === "max") return 1;
      return 0;
    }
    getcount(prop) { calls.push(["getcount", this.path, prop]); return 0; }
    set(prop, value) {
      calls.push(["set", this.path, prop, value]);
      if (this.path.includes("tracks 9") && prop === "arm") trackArm = value;
    }
    call(method, ...args) {
      calls.push(["call", this.path, method, ...args]);
      if (method === "fire") hasClip = true;
      if (method === "delete_clip") hasClip = false;
      return method === "get_major_version" ? 12 : "id 901";
    }
  }
  class Task {
    constructor(fn) { this.fn = fn; this.cancelled = false; tasks.push(this); }
    schedule(ms) { this.ms = ms; calls.push(["schedule", ms]); }
    cancel() { this.cancelled = true; calls.push(["task-cancel"]); }
    freepeer() { calls.push(["task-freepeer"]); }
    fire() { this.fn(); }
  }
  const context = { LiveAPI, Task, calls, tasks, console, outlet() {}, arrayfromargs() {} };
  vm.createContext(context);
  vm.runInContext(source, context, {filename: "sidekick_bridge.js"});
  return context;
}

{
  const c = load();
  assert.throws(() => c.doOp({op: "capture_start", capture_id: "bad", track_index: 4, scene_index: 2, duration_ms: 20}));
  assert.throws(() => c.doOp({op: "capture_start", capture_id: "x", track_index: 9, scene_index: 2, duration_ms: 20, source_track: 4}));
}
{
  const c = load({occupied: true});
  assert.throws(() => c.doOp({op: "capture_start", capture_id: "x", track_index: 9, scene_index: 2, duration_ms: 20}));
}
{
  const c = load();
  const start = {op: "capture_start", capture_id: "x", track_index: 9, scene_index: 2, duration_ms: 2000};
  assert.equal(c.doOp(start).state, "recording");
  assert.deepEqual(c.calls.find((x) => x[0] === "call" && x[2] === "fire").slice(3), [4, 0]);
  assert.equal(c.tasks[0].ms, 2250);
  assert.equal(c.doOp(start).state, "recording");
  assert.throws(() => c.doOp({...start, capture_id: "conflict"}));
  assert.throws(() => c.doOp({op: "capture_finalize", capture_id: "x"}));
  assert.equal(c.tasks.length, 1);
  c.tasks[0].fire();
  assert.equal(c.doOp({op: "capture_status", capture_id: "x"}).state, "ready");
  assert.ok(c.captureHistory.x.slot);
  const result = c.doOp({op: "capture_finalize", capture_id: "x"});
  assert.deepEqual(JSON.parse(JSON.stringify(result)), {capture_id: "x", path: "t9/s2", clip_id: 901, state: "finalized", cleanup: {arm_restored: true, task_cancelled: false, stopped: true}});
  const stopIndex = c.calls.findIndex((x) => x[2] === "stop_all_clips");
  const restoreIndex = c.calls.findIndex((x, i) => i > stopIndex && x[0] === "set" && x[2] === "arm");
  assert.ok(stopIndex >= 0 && restoreIndex > stopIndex);
  assert.equal(c.calls.some((x) => typeof x[1] === "string" && x[1].includes("tracks 4")), false);
  assert.equal(c.calls.some((x) => typeof x[1] === "string" && x[1].includes(" scenes ")), false);
  assert.equal(c.captureHistory.x.track, null);
  assert.equal(c.captureHistory.x.slot, null);
  assert.equal(c.doOp({op: "get", path: "live_set tracks 9", prop: "arm"}).value[0], 0);
}
{
  const c = load();
  c.doOp({op: "capture_start", capture_id: "x", track_index: 9, scene_index: 2, duration_ms: 20});
  const task = c.tasks[0];
  const cancelled = c.doOp({op: "capture_cancel", capture_id: "x"});
  assert.equal(task.cancelled, true);
  assert.equal(cancelled.state, "cancelled");
  assert.equal(c.doOp({op: "capture_cancel", capture_id: "x"}).state, "cancelled");
  assert.equal(c.calls.filter((x) => x[2] === "delete_clip").length, 1);
  const stop = c.calls.find((x) => x[2] === "stop_all_clips");
  assert.equal(stop[1], "live_set tracks 9");
  assert.equal(c.captureHistory.x.track, null);
  assert.equal(c.captureHistory.x.slot, null);
  assert.equal(c.doOp({op: "get", path: "live_set tracks 9", prop: "arm"}).value[0], 0);
}
{
  const c = load({occupied: true});
  assert.doesNotThrow(() => c.doOp({
    op: "call", path: "live_set tracks 9 clip_slots 2",
    method: "delete_clip", args: [],
  }));
  assert.equal(c.doOp({op: "get", path: "live_set tracks 9 clip_slots 2", prop: "has_clip"}).value, 0);
  assert.throws(() => c.doOp({
    op: "call", path: "live_set tracks 4 clip_slots 2",
    method: "delete_clip", args: [],
  }));
  assert.throws(() => c.doOp({
    op: "call", path: "live_set tracks 9 clip_slots 2",
    method: "fire", args: [],
  }));
}
{
  const c = load({clipId: 0});
  c.doOp({op: "capture_start", capture_id: "zero", track_index: 9, scene_index: 2, duration_ms: 20});
  c.tasks[0].fire();
  assert.throws(() => c.doOp({op: "capture_finalize", capture_id: "zero"}));
  assert.equal(c.captureHistory.zero.state, "failed");
}
{
  const c = load();
  c.MAX_CAPTURE_HISTORY = 2;
  for (let i = 0; i < 4; i++) {
    const id = `h${i}`;
    c.doOp({op: "capture_start", capture_id: id, track_index: 9, scene_index: 2, duration_ms: 20});
    c.doOp({op: "capture_cancel", capture_id: id});
  }
  assert.ok(Object.keys(c.captureHistory).length <= 2);
  assert.equal(Object.getPrototypeOf(c.captureHistory), null);
  assert.throws(() => c.doOp({op: "capture_status", capture_id: "h0"}));
}
// Existing bridge operations still execute under the same mocked Live API.
{
  const c = load();
  for (const op of [
    {op: "ping"}, {op: "assert_track_name", track_index: 9, expect: "SIDEKICK EARS"},
    {op: "get", path: "live_set", prop: "tempo"}, {op: "get_id", path: "id 42"},
    {op: "list_params", track_index: 9}, {op: "create_envelope", clip_id: 1, param_id: 2},
    {op: "insert_steps_last", points: [[0, 0.25, 1]]}, {op: "clear_envelopes", clip_id: 1},
    {op: "list_grooves"}, {op: "set_clip_groove", clip_id: 1, groove_id: 2},
    {op: "clear_clip_groove", clip_id: 1},
  ]) assert.doesNotThrow(() => c.doOp(op));
}

console.log("sidekick bridge capture tests: OK");
