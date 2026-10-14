"""Build the Booth voting page: one self-contained HTML scoreboard.

Live is the player (fire clips in the BOOTH tracks); the page is the blind
scoreboard: keyboard voting (A / B / T tie / S skip), G golds the winner's
source phrase, provenance revealed only AFTER each vote, progress bar, and a
one-click export of the completed manifest + gold list for the sidekick to
ingest and score.

Reads tools/ideas/pairwise_manifest.json (+ phrase provenance from the index)
and writes tools/ideas/booth_vote.html. Rebuild any time; votes live in the
browser's localStorage until exported.

Run: python3 tools/build_booth_page.py
"""
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "tools/ideas/pairwise_manifest.json")
OUT = os.path.join(ROOT, "tools/ideas/booth_vote.html")
SCOREBOARD = os.path.join(ROOT, "store/scoreboard.json")


def scoreboard_line():
    """Past-rounds EAR-vs-MODEL tally. Structurally past-only: scoreboard entries
    are appended at ingest, after voting — the page NEVER reads the predictions
    sidecar, so nothing about the current round can leak here."""
    if not os.path.exists(SCOREBOARD):
        return ""
    sb = json.load(open(SCOREBOARD))
    rounds, rule = sb.get("rounds", []), sb.get("promotion_rule", {})
    if not rounds:
        return ""
    from ingest_booth_votes import promotion_status
    st = promotion_status(rounds, rule)
    cells = " &middot; ".join(
        "%s %d/%d (%.2f)" % (r.get("round_id", "?").replace("booth_", ""),
                             r.get("n_agree", 0), r.get("n_random", 0),
                             r.get("agreement") or 0.0)
        for r in rounds[-4:])
    return ("EAR vs MODEL (random pairs, past rounds): %s &middot; "
            "streak %d/%d toward promotion (rule: &ge;%.1f on &ge;%d, %d straight)"
            % (cells, st["qualifying_streak"], rule.get("consecutive_rounds", 3),
               rule.get("min_agreement", 0.7), rule.get("min_random_pairs", 5),
               rule.get("consecutive_rounds", 3)))


def provenance_map(ids):
    idx = json.load(open(os.path.join(ROOT, "store/phrase_index.json")))
    phrases = idx if isinstance(idx, list) else idx.get("phrases", [])
    by_id = {p.get("id"): p for p in phrases}
    return {pid: {
        "project": by_id.get(pid, {}).get("project", "?"),
        "source": by_id.get(pid, {}).get("clip_source", "?"),
        "tempo": by_id.get(pid, {}).get("tempo"),
        "n_notes": by_id.get(pid, {}).get("n_notes"),
    } for pid in ids}


def vote_storage_key(manifest, round_id):
    """Browser storage identity for exactly one frozen Booth round."""
    identity = {
        "round_id": round_id,
        "pairs": [[m["pair_id"], m["A_phrase_id"], m["B_phrase_id"]]
                  for m in manifest],
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return "booth_votes_" + digest


HTML = r"""<!doctype html>
<meta charset="utf-8">
<title>BOOTH — blind pairs</title>
<style>
  :root { --bg:#141210; --panel:#1e1a16; --amber:#ffb000; --teal:#00c2b8;
          --dim:#7a7168; --txt:#e8e2da; --red:#ff5a36; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--txt);
         font:15px/1.5 "JetBrains Mono", ui-monospace, monospace;
         display:flex; flex-direction:column; align-items:center;
         min-height:100vh; padding:32px 16px; }
  h1 { font-size:15px; letter-spacing:.35em; color:var(--amber); margin-bottom:4px; }
  #sub { color:var(--dim); font-size:12px; margin-bottom:24px; }
  #card { background:var(--panel); border:1px solid #2c2username62620; border-radius:10px;
          padding:28px 32px; width:min(560px, 94vw); text-align:center;
          box-shadow:0 8px 40px #0008; }
  #pairno { font-size:13px; color:var(--dim); letter-spacing:.2em; }
  #hint { margin:14px 0 22px; font-size:13px; color:var(--dim); }
  #hint b { color:var(--txt); }
  .btnrow { display:flex; gap:12px; justify-content:center; margin-bottom:14px; }
  button { font:inherit; border:0; border-radius:8px; padding:16px 0; width:120px;
           cursor:pointer; font-weight:700; letter-spacing:.1em; }
  .A { background:var(--amber); color:#141210; }
  .B { background:var(--teal); color:#141210; }
  .tie, .skip { background:#33291f; color:var(--txt); width:84px; padding:10px 0;
                font-weight:400; font-size:13px; }
  .gold { background:none; border:1px dashed var(--amber); color:var(--amber);
          width:auto; padding:8px 14px; font-size:12px; }
  .gold.on { background:var(--amber); color:#141210; border-style:solid; }
  #reveal { min-height:44px; font-size:12px; color:var(--dim); margin-top:16px; }
  #reveal b { color:var(--txt); }
  #bar { width:min(560px,94vw); height:6px; background:#26211c; border-radius:3px;
         margin:22px 0 10px; overflow:hidden; }
  #fill { height:100%; background:var(--amber); width:0%; transition:width .2s; }
  #done { display:none; text-align:center; }
  textarea { width:min(560px,94vw); height:120px; background:#0f0d0b; color:var(--txt);
             border:1px solid #2c2620; border-radius:8px; padding:10px;
             font:12px ui-monospace, monospace; margin-top:10px; }
  #keys { margin-top:20px; font-size:11px; color:var(--dim); }
  #board { color:var(--dim); font-size:11px; margin:-14px 0 20px; max-width:min(560px,94vw);
           text-align:center; }
</style>
<h1>THE BOOTH</h1>
<div id="sub">fire the clips in Live &middot; vote here &middot; blind until you vote</div>
<div id="board">__SCOREBOARD__</div>
<div id="card">
  <div id="pairno"></div>
  <div id="hint">Fire clip <b id="clipA">P?? A</b> (amber) then <b id="clipB">P?? B</b> (teal)
      on the <b>SIDEKICK BOOTH</b> track.<br>
      Which one is more <b>alive</b> &mdash; which would you keep?</div>
  <div class="btnrow">
    <button class="A" onclick="vote('A')">A</button>
    <button class="B" onclick="vote('B')">B</button>
  </div>
  <div class="btnrow">
    <button class="tie" onclick="vote('tie')">tie</button>
    <button class="skip" onclick="vote('skip')">skip</button>
    <button class="gold" id="goldbtn" onclick="toggleGold()">&#9733; gold the winner</button>
  </div>
  <div id="reveal"></div>
</div>
<div id="bar"><div id="fill"></div></div>
<div id="count" style="color:var(--dim);font-size:12px"></div>
<div id="done">
  <p style="margin:14px 0 6px">done. copy this to the sidekick (or it's auto-saved below):</p>
  <textarea id="out" readonly onclick="this.select()"></textarea><br>
  <button class="tie" style="width:auto;padding:10px 18px;margin-top:10px" onclick="download()">download booth_votes.json</button>
</div>
<div id="keys">keys: A &middot; B &middot; T tie &middot; S skip &middot; G gold-last-winner &middot; &#8592; undo</div>
<script>
const PAIRS = __PAIRS__;
const PROV = __PROV__;
const VKEY = "__VKEY__";   // manifest + frozen round identity — new round = fresh votes
const ROUND = "__ROUND__"; // predictions-sidecar round_id; travels with every payload
let i = 0;
let votes = JSON.parse(localStorage.getItem(VKEY) || "[]");
let golds = new Set(JSON.parse(localStorage.getItem(VKEY + "_golds") || "[]"));
i = votes.length;
if (votes.length) save();  // re-announce restored votes so a served session sees them

function save() {
  localStorage.setItem(VKEY, JSON.stringify(votes));
  localStorage.setItem(VKEY + "_golds", JSON.stringify([...golds]));
  // auto-save to disk when served by tools/session.py; a no-op as a bare file
  try { fetch("/save?kind=booth&vkey=" + VKEY, {method: "POST",
        headers: {"Content-Type": "application/json"}, body: payload()})
        .catch(function(){}); } catch (e) {}
}
function render() {
  document.getElementById("fill").style.width = (100 * i / PAIRS.length) + "%";
  document.getElementById("count").textContent = i + " / " + PAIRS.length + " voted";
  if (i >= PAIRS.length) { finish(); return; }
  const p = PAIRS[i];
  const nn = String(p.pair_id).padStart(2, "0");
  document.getElementById("pairno").textContent = "PAIR " + nn;
  document.getElementById("clipA").textContent = "P" + nn + " A";
  document.getElementById("clipB").textContent = "P" + nn + " B";
  document.getElementById("reveal").innerHTML = "";
  document.getElementById("goldbtn").classList.remove("on");
}
function reveal(p, v) {
  const pa = PROV[p.A_phrase_id] || {}, pb = PROV[p.B_phrase_id] || {};
  const w = v === "A" ? pa : v === "B" ? pb : null;
  let txt = "A was <b>" + (pa.project || "?") + "</b> (" + (pa.source || "?") + ") &middot; " +
            "B was <b>" + (pb.project || "?") + "</b> (" + (pb.source || "?") + ")";
  if (w) txt = "you kept <b>" + (w.project || "?") + "</b> &middot; " + txt;
  document.getElementById("reveal").innerHTML = txt;
}
function vote(v) {
  if (i >= PAIRS.length) return;
  const p = PAIRS[i];
  votes.push({ pair_id: p.pair_id, winner: v === "skip" ? "" : v });
  reveal(p, v);
  i++;
  save();
  setTimeout(render, v === "skip" ? 250 : 1400);
}
function toggleGold() {
  if (!votes.length) return;
  const last = votes[votes.length - 1];
  const p = PAIRS.find(x => x.pair_id === last.pair_id);
  const wid = last.winner === "A" ? p.A_phrase_id : last.winner === "B" ? p.B_phrase_id : null;
  if (!wid) return;
  if (golds.has(wid)) golds.delete(wid); else golds.add(wid);
  document.getElementById("goldbtn").classList.toggle("on", golds.has(wid));
  save();
}
function undo() {
  if (!votes.length || i === 0) return;
  const last = votes.pop(); i--;
  const p = PAIRS.find(x => x.pair_id === last.pair_id);
  if (p) { golds.delete(p.A_phrase_id); golds.delete(p.B_phrase_id); }
  save(); render();
}
function payload() {
  return JSON.stringify({ round_id: ROUND, votes: votes, golds: [...golds] }, null, 1);
}
function finish() {
  document.getElementById("card").style.display = "none";
  document.getElementById("done").style.display = "block";
  document.getElementById("out").value = payload();
}
function download() {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([payload()], { type: "application/json" }));
  a.download = "booth_votes.json";
  a.click();
}
document.addEventListener("keydown", e => {
  const k = e.key.toLowerCase();
  if (k === "a") vote("A"); else if (k === "b") vote("B");
  else if (k === "t") vote("tie"); else if (k === "s") vote("skip");
  else if (k === "g") toggleGold(); else if (e.key === "ArrowLeft") undo();
});
render();
</script>
"""


def main():
    manifest = json.load(open(MANIFEST))
    ids = sorted({m["A_phrase_id"] for m in manifest} | {m["B_phrase_id"] for m in manifest})
    pairs = [{"pair_id": m["pair_id"], "A_phrase_id": m["A_phrase_id"],
              "B_phrase_id": m["B_phrase_id"]} for m in manifest]
    preds_path = os.path.join(ROOT, "tools/ideas/pairwise_predictions.json")
    predictions = json.load(open(preds_path)) if os.path.exists(preds_path) else None
    from ingest_booth_votes import validate_booth_round_artifacts
    round_id = validate_booth_round_artifacts(manifest, predictions)
    vkey = vote_storage_key(manifest, round_id)
    html = (HTML.replace("__PAIRS__", json.dumps(pairs))
                .replace("__PROV__", json.dumps(provenance_map(ids)))
                .replace("__VKEY__", vkey)
                .replace("__ROUND__", round_id)
                .replace("__SCOREBOARD__", scoreboard_line())
                .replace("#2c2username62620", "#2c2620"))
    open(OUT, "w").write(html)
    print(f"built {os.path.relpath(OUT, ROOT)} — open it in a browser, vote with A/B/T/S/G,")
    print("then paste or drop booth_votes.json back to the sidekick to score + gold.")


if __name__ == "__main__":
    main()
