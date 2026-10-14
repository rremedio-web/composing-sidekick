"""Build the solo-audition voting page: real leads, blind, keyboard-first.

Sibling of build_booth_page.py. Live (or the exported audition_XX.mid files) is
the player; the page is the scoreboard. THREE tiers (2026-07-03, after the
gold=passable clarification): G gold ("genuinely sounds like ME" — permanent
anchor), P passable ("works, wouldn't chase it"), M meh. Provenance revealed
AFTER the vote; export feeds ingest_audition_votes.py + rank_audition --score.
When served by tools/session.py every vote also auto-saves to disk (a POST the
page fires and silently drops when opened as a bare file).

Reads tools/ideas/rank_audition_manifest.json; writes tools/ideas/audition_vote.html.
Run: python3 tools/build_audition_page.py
"""
import hashlib
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "tools/ideas/rank_audition_manifest.json")
OUT = os.path.join(ROOT, "tools/ideas/audition_vote.html")


def provenance_map(ids):
    idx = json.load(open(os.path.join(ROOT, "store/phrase_index.json")))
    phrases = idx if isinstance(idx, list) else idx.get("phrases", [])
    by_id = {p.get("id"): p for p in phrases}
    return {pid: {"project": by_id.get(pid, {}).get("project", "?"),
                  "source": by_id.get(pid, {}).get("clip_source", "?")}
            for pid in ids}


def vote_storage_key(manifest, round_id):
    """Browser storage identity for exactly one solo-audition round."""
    identity = {
        "round": round_id,
        "items": [[m["blind_id"], m["phrase_id"]] for m in manifest],
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return "audition_votes_" + digest


HTML = r"""<!doctype html>
<meta charset="utf-8">
<title>BOOTH — solo audition</title>
<style>
  :root { --bg:#141210; --panel:#1e1a16; --amber:#ffb000; --dim:#7a7168; --txt:#e8e2da; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--txt);
         font:15px/1.5 "JetBrains Mono", ui-monospace, monospace;
         display:flex; flex-direction:column; align-items:center;
         min-height:100vh; padding:32px 16px; }
  h1 { font-size:15px; letter-spacing:.35em; color:var(--amber); margin-bottom:4px; }
  #sub { color:var(--dim); font-size:12px; margin-bottom:24px; }
  #card { background:var(--panel); border:1px solid #2c2620; border-radius:10px;
          padding:28px 32px; width:min(520px,94vw); text-align:center; }
  #no { font-size:13px; color:var(--dim); letter-spacing:.2em; }
  #hint { margin:14px 0 22px; font-size:13px; color:var(--dim); }
  #hint b { color:var(--txt); }
  .btnrow { display:flex; gap:12px; justify-content:center; margin-bottom:8px; }
  button { font:inherit; border:0; border-radius:8px; padding:14px 0; width:130px;
           cursor:pointer; font-weight:700; letter-spacing:.08em; }
  .gold { background:var(--amber); color:#141210; }
  .passable { background:#4a3d28; color:var(--txt); font-weight:700; width:130px;
              padding:12px 0; font-size:14px; border:1px solid #6a5636; }
  .meh, .skip { background:#33291f; color:var(--txt); font-weight:400; width:90px;
                padding:10px 0; font-size:13px; }
  #reveal { min-height:38px; font-size:12px; color:var(--dim); margin-top:14px; }
  #reveal b { color:var(--txt); }
  #bar { width:min(520px,94vw); height:6px; background:#26211c; border-radius:3px;
         margin:22px 0 10px; overflow:hidden; }
  #fill { height:100%; background:var(--amber); width:0%; transition:width .2s; }
  #done { display:none; text-align:center; }
  textarea { width:min(520px,94vw); height:110px; background:#0f0d0b; color:var(--txt);
             border:1px solid #2c2620; border-radius:8px; padding:10px;
             font:12px ui-monospace,monospace; margin-top:10px; }
  #keys { margin-top:18px; font-size:11px; color:var(--dim); }
</style>
<h1>SOLO AUDITION</h1>
<div id="sub">fire clip <b>AUD NN</b> on the <b>SIDEKICK BOOTH</b> track in Live &middot; vote here &middot; blind until you do<br>
(the all-C4 clips on the RICARDO FEEL track are groove-timing carriers, NOT these)</div>
<div id="card">
  <div id="no"></div>
  <div id="hint">One lead, alone. <b>&#9733; Gold</b> = genuinely sounds like ME.<br>
<b>Passable</b> = works, wouldn't chase it. <b>Meh</b> = nothing happened.</div>
  <div class="btnrow"><button class="gold" onclick="vote('gold')">&#9733; GOLD</button></div>
  <div class="btnrow"><button class="passable" onclick="vote('passable')">passable</button></div>
  <div class="btnrow">
    <button class="meh" onclick="vote('meh')">meh</button>
    <button class="skip" onclick="vote('skip')">skip</button>
  </div>
  <div id="reveal"></div>
</div>
<div id="bar"><div id="fill"></div></div>
<div id="count" style="color:var(--dim);font-size:12px"></div>
<div id="done">
  <p style="margin:14px 0 6px">done — hand this to the sidekick:</p>
  <textarea id="out" readonly onclick="this.select()"></textarea><br>
  <button class="meh" style="width:auto;padding:10px 18px;margin-top:10px" onclick="dl()">download audition_votes.json</button>
</div>
<div id="keys">keys: G gold &middot; P passable &middot; M meh &middot; S skip &middot; &#8592; undo &middot;
<a href="#" onclick="reset();return false" style="color:var(--dim)">reset all votes</a></div>
<script>
const ITEMS = __ITEMS__;
const PROV = __PROV__;
const VKEY = "__VKEY__";   // manifest + round identity — new round = fresh votes
const ROUND = "__ROUND__"; // round tag; travels with every payload
let votes = JSON.parse(localStorage.getItem(VKEY) || "[]");
let i = votes.length;
if (votes.length) save();  // re-announce restored votes so a served session sees them
function save(){
  localStorage.setItem(VKEY, JSON.stringify(votes));
  // auto-save to disk when served by tools/session.py; a no-op as a bare file
  try { fetch("/save?kind=audition&vkey="+VKEY, {method:"POST",
        headers:{"Content-Type":"application/json"}, body: payload()})
        .catch(function(){}); } catch(e){}
}
function reset(){ votes = []; i = 0; save();
  document.getElementById("card").style.display = "block";
  document.getElementById("done").style.display = "none"; render(); }
function render(){
  document.getElementById("fill").style.width = (100*i/ITEMS.length)+"%";
  document.getElementById("count").textContent = i+" / "+ITEMS.length+" rated";
  if (i >= ITEMS.length) { finish(); return; }
  document.getElementById("no").textContent = "AUDITION "+String(ITEMS[i].blind_id).padStart(2,"0");
  document.getElementById("reveal").innerHTML = "";
}
function vote(v){
  if (i >= ITEMS.length) return;
  const it = ITEMS[i];
  votes.push({blind_id: it.blind_id, rating: v==="skip" ? "" : v});
  const pr = PROV[it.phrase_id] || {};
  document.getElementById("reveal").innerHTML =
      (v==="gold" ? "&#9733; " : "") + "that was <b>"+(pr.project||"?")+"</b> ("+(pr.source||"?")+")";
  i++; save(); setTimeout(render, v==="skip" ? 250 : 1300);
}
function undo(){ if (votes.length && i>0){ votes.pop(); i--; save(); render(); } }
function payload(){ return JSON.stringify({round: ROUND, ratings: votes}, null, 1); }
function finish(){
  document.getElementById("card").style.display = "none";
  document.getElementById("done").style.display = "block";
  document.getElementById("out").value = payload();
}
function dl(){
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([payload()],{type:"application/json"}));
  a.download = "audition_votes.json"; a.click();
}
document.addEventListener("keydown", e => {
  const k = e.key.toLowerCase();
  if (k==="g") vote("gold"); else if (k==="p") vote("passable");
  else if (k==="m") vote("meh");
  else if (k==="s") vote("skip"); else if (e.key==="ArrowLeft") undo();
});
render();
</script>
"""


def main():
    manifest = json.load(open(MANIFEST))
    from ingest_audition_votes import validate_audition_manifest_round
    rtag = validate_audition_manifest_round(manifest)
    items = [{"blind_id": m["blind_id"], "phrase_id": m["phrase_id"]} for m in manifest]
    prov = provenance_map([m["phrase_id"] for m in manifest])
    vkey = vote_storage_key(manifest, rtag)
    html = (HTML.replace("__ITEMS__", json.dumps(items))
                .replace("__PROV__", json.dumps(prov)).replace("__VKEY__", vkey)
                .replace("__ROUND__", rtag))
    open(OUT, "w").write(html)
    print(f"built {os.path.relpath(OUT, ROOT)} — {len(items)} solo leads; keys G/P/M/S; "
          "export feeds ingest_audition_votes.py + rank_audition --score")


if __name__ == "__main__":
    main()
