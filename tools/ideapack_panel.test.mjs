// Logic smoke test for tools/ideapack_panel.html — runs the panel's pure functions under node,
// no DOM. Verifies the Rating-Contract acceptance checks.   Run: node tools/ideapack_panel.test.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "ideapack_panel.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
// direct eval shares scope, so the appended line can read the script-local DEFAULT_PACK
eval(script + "\n; globalThis.__DP = DEFAULT_PACK;");
const IP = globalThis.IdeaPanel;
const DP = globalThis.__DP;

let pass = 0, fail = 0;
const ok = (cond, msg) => { if (cond) { pass++; } else { fail++; console.error("  FAIL:", msg); } };
const throws = (fn, msg) => { try { fn(); ok(false, msg + " (did not throw)"); } catch { pass++; } };

// embedded sample is a real mixed pack
ok(DP && DP.contrast_axis === "mixed", "sample contrast_axis === mixed");
ok(Array.isArray(DP.candidates) && DP.candidates.length === 3, "sample has 3 candidates");

// 1. mixed pack: stored, NOT trained
const sm = IP.scopeInfo(DP);
ok(sm.scope === "mixed" && sm.model === "none" && sm.trains === false, "mixed pack -> trainable_model none, not trained");
ok(/stored/i.test(sm.note) && !/TRAINS/.test(sm.note), "mixed pack note says stored / not trained");

// 2. single-axis lead pack: trains lead_taste
const lead = { contrast_axis: "lead", rating_contract: { rating_scope: "lead", trainable_model: "lead_taste" }, candidates: [{}, {}] };
ok(IP.scopeInfo(lead).trains === true, "lead pack -> trains lead_taste");
const harm = { contrast_axis: "harmony", rating_contract: { rating_scope: "harmony", trainable_model: "harmony_future" }, candidates: [{}] };
ok(IP.scopeInfo(harm).trains === false && /not trained/i.test(IP.scopeInfo(harm).note), "harmony_future -> stored, not trained");

// 3. explicit start_track parsing
ok(IP.parseTrack("") === null && IP.parseTrack("  ") === null, "blank start_track -> null");
ok(IP.parseTrack("-1") === null && IP.parseTrack("1.5") === null && IP.parseTrack("x") === null, "invalid start_track -> null");
ok(IP.parseTrack("0") === 0 && IP.parseTrack("12") === 12, "valid start_track -> int");

// 4. lanes: null until explicit start, then start_track + track_offset / scene
const noStart = IP.laneRows(DP, "", "0");
ok(noStart.length === 3 && noStart.every(r => r.slot === null && r.track_index === null), "no start_track -> all slots null (no clobber)");
const withStart = IP.laneRows(DP, "12", "0");
ok(withStart.map(r => r.slot).join(",") === "12/0,13/0,14/0", "start_track 12 -> slots 12/0,13/0,14/0");

// 5. rating call always echoes contrast_axis, scope/model left to server
const call = IP.buildRatingCall(DP, 0, 2, "Flowing");
ok(call.tool === "rate_candidates", "rate call tool name");
ok(call.arguments.contrast_axis === DP.contrast_axis, "rate call includes contrast_axis=pack.contrast_axis");
ok(call.arguments.best === 0 && call.arguments.worst === 2, "rate call best/worst");
ok(call.arguments.reason === "flowing", "rate call reason lowercased");
ok(call.arguments.candidates === DP.candidates, "rate call passes pack.candidates");
const keys = Object.keys(call.arguments);
ok(!keys.includes("rating_scope") && !keys.includes("trainable_model"), "rate call NEVER sends rating_scope/trainable_model (server derives)");

// 6. rating-call guards
throws(() => IP.buildRatingCall(DP, 1, 1, ""), "best === worst rejected");
throws(() => IP.buildRatingCall(DP, null, 2, ""), "missing best rejected");
const noWorst = IP.buildRatingCall(DP, 0, null, "");
ok(!("worst" in noWorst.arguments) && noWorst.arguments.reason === null, "worst optional; empty reason -> null");

// 7. handoff blocked without explicit start_track
throws(() => IP.buildHandoffCall(DP, "", "0"), "handoff without start_track rejected");
const ho = IP.buildHandoffCall(DP, "12", "0");
ok(ho.tool === "prepare_idea_pack_audition_lanes" && ho.arguments.confirm === true
   && ho.arguments.start_track === 12 && ho.arguments.scene_index === 0 && ho.arguments.idea_pack === DP,
   "handoff call shape (confirm=true, explicit start_track)");

// 7b. REGRESSION (the 4-lens major): cards must be driven by candidates[], enriched from ideas[] BY ID,
//     so a pack whose ideas[] is reordered/shorter than candidates[] never rates the wrong candidate.
const cands = [{ id: "c-A", notes: [1], the_move: "A" }, { id: "c-B", notes: [1, 2], the_move: "B" }, { id: "c-C", notes: [1], the_move: "C" }];
const ideasRev = [
  { id: "idea-3", title: "TITLE-C", base_score: 0.3, candidate: { id: "c-C" } },
  { id: "idea-2", title: "TITLE-B", base_score: 0.2, candidate: { id: "c-B" } },
  { id: "idea-1", title: "TITLE-A", base_score: 0.1, candidate: { id: "c-A" } },
];
const dpk = { contrast_axis: "lead", rating_contract: { rating_scope: "lead", trainable_model: "lead_taste" }, candidates: cands, ideas: ideasRev };
const disp = IP.displayCandidates(dpk);
ok(disp.map(d => d.id).join(",") === "c-A,c-B,c-C", "display list follows candidates[] order, not ideas[]");
ok(disp[0].title === "TITLE-A" && disp[2].title === "TITLE-C", "display enriched by matching idea id (not position)");
ok(disp[0].n_notes === 1 && disp[1].n_notes === 2, "n_notes from candidate notes");
const rc = IP.buildRatingCall(dpk, 0, 2, "x");
ok(rc.arguments.candidates[0].id === "c-A" && rc.arguments.candidates[2].id === "c-C",
   "picking card 0 rates candidates[0] even with ideas[] reversed (no index aliasing)");
// candidates[] longer than ideas[] -> every candidate still shown
const dpk2 = { contrast_axis: "lead", candidates: [{ id: "c1", notes: [] }, { id: "c2", notes: [] }], ideas: [{ id: "i1", candidate: { id: "c1" }, title: "T1" }] };
ok(IP.displayCandidates(dpk2).length === 2, "all candidates shown even when ideas[] is shorter");
// lane preview must NOT fabricate a track_offset
const lr = IP.laneRows({ audition_lanes: [{ idea_id: "x", clip_name: "x" }] }, "10", "0");
ok(lr[0].slot === null && lr[0].missing_offset === true, "missing track_offset -> slot null, flagged (no fabricated target)");

// 8. structural: the file exposes no user input for rating_scope, and never auto-writes
ok(!/id="ratingScope"|id="rating_scope"/.test(html), "no rating_scope input in the UI");
ok(/never writes to Ableton/i.test(html), "panel states it never writes to Ableton");

console.log(`\n${fail === 0 ? "PASS" : "FAIL"} — ${pass} checks passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
