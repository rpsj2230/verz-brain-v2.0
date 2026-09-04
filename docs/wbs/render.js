const fs = require("fs");
const SCH = require(__dirname + "/schedule.js");
const SPR = {LAUNCH:{},WAVE:{},NAMES:{},START:"2026-09-08",LEAVES_PER_TRACK_DAY:14,INTEGRATION_DAYS:1};
const MODS = [].concat(require(__dirname + "/wbs-a.js"), require(__dirname + "/wbs-b.js"), require(__dirname + "/wbs-c.js"), require(__dirname + "/wbs-d.js"), require(__dirname + "/wbs-e.js"), require(__dirname + "/wbs-f.js"));
// ---- scheduling helpers
function wd(d,n){const x=new Date(d);let i=0;while(i<n){x.setDate(x.getDate()+1);const g=x.getDay();if(g!==0&&g!==6)i++}return x}
function iso(d){return d.toISOString().slice(0,10)}
function fmt(d){return d.toLocaleDateString("en-GB",{day:"2-digit",month:"short"})}


// ---- normalise: a leaf is a string; {n,k:[...]} has children, to any depth
function norm(node) {
  if (typeof node === "string") return { n: node, k: [] };
  return { n: node.n, k: (node.k || node.s || []).map(norm) };
}
const TREE = MODS.map(m => ({
  id: m.id, name: m.name, wave: (SCH.WAVE[m.id] !== undefined ? SCH.WAVE[m.id] : m.wave),
  k: m.tasks.map(t => norm({ n: t.n, k: t.s }))
}));

let LEAVES = 0, NODES = 0, MAXD = 0;
function walk(n, d) { NODES++; MAXD = Math.max(MAXD, d); if (!n.k.length) LEAVES++; else n.k.forEach(c => walk(c, d + 1)); }
TREE.forEach(m => m.k.forEach(t => walk(t, 2)));

const waves = {};
TREE.forEach(m => { let c = 0; m.k.forEach(t => { const f = n => n.k.length ? n.k.forEach(f) : c++; f(t); }); waves[m.wave] = (waves[m.wave] || 0) + c; });

// ---- wave windows
const modLeaves = {};
TREE.forEach(m => { let c = 0; m.k.forEach(t => { const f = n => n.k.length ? n.k.forEach(f) : c++; f(t); }); modLeaves[m.id] = c; });
const isL = id => false;
TREE.forEach(m => { m.launch = isL(m.id); m.trim = SPR.LAUNCH[m.id] || 0; if (m.launch) m.lwave = SPR.WAVE[m.id]; });
const waveIds = Object.keys(waves).map(Number).sort((a,b)=>a-b);
const LWIN = {};
let lc = new Date(SPR.START + "T00:00:00Z");
const lwaveIds = [...new Set(Object.values(SPR.WAVE))].sort((a,b)=>a-b);
if(!lwaveIds.length){lwaveIds.push(0);LWIN[0]={start:new Date(SPR.START+"T00:00:00Z"),end:new Date(SPR.START+"T00:00:00Z"),days:0,tracks:0,leaves:0}}
for (const w of lwaveIds) {
  const mods = TREE.filter(m => m.launch && m.lwave === w);
  const biggest = Math.max.apply(null, mods.map(m => Math.round(modLeaves[m.id] * m.trim)));
  const days = Math.ceil(biggest / SPR.LEAVES_PER_TRACK_DAY) + SPR.INTEGRATION_DAYS;
  const start = new Date(lc); const end = wd(start, days - 1);
  LWIN[w] = { start, end, days, tracks: mods.length, leaves: mods.reduce((a,m)=>a+Math.round(modLeaves[m.id]*m.trim),0) };
  lc = wd(end, 1);
}
const LAUNCH_END = LWIN[lwaveIds[lwaveIds.length-1]].end;
const LAUNCH_TASKS = Object.values(LWIN).reduce((a,w)=>a+w.leaves,0);
const LAUNCH_DAYS = Object.values(LWIN).reduce((a,w)=>a+w.days,0);
const WIN = {};
let cursor = new Date(SCH.START + "T00:00:00Z");
for (const w of waveIds) {
  const mods = TREE.filter(m => m.wave === w);
  const tracks = mods.reduce((a,m)=>a+(SCH.SPLIT[m.id]||1), 0);

  // A wave takes the longer of two things, and until 2026-09-05 it only took the first.
  //
  //   1. its longest single track, since a module cannot be finished faster than its
  //      own critical path however many other tracks are running
  //   2. its total work divided by how much can actually run at once
  //
  // TRACK_CAP existed in schedule.js and was never read here, so splitting a module more
  // finely always made the date earlier and nothing ever said "that needs more tracks
  // than we have". Every date quoted before this rested on a limit nothing enforced.
  const biggest = Math.max.apply(null, mods.map(m => Math.ceil(modLeaves[m.id] / (SCH.SPLIT[m.id]||1))));
  const byLongestTrack = Math.ceil(biggest / SCH.LEAVES_PER_TRACK_DAY);
  const concurrent = Math.min(tracks, SCH.TRACK_CAP || tracks);
  const byCapacity = Math.ceil(waves[w] / (concurrent * SCH.LEAVES_PER_TRACK_DAY));
  const days = Math.max(byLongestTrack, byCapacity) + SCH.INTEGRATION_DAYS;

  const start = new Date(cursor);
  const end = wd(start, days - 1);
  WIN[w] = {
    start, end, days, tracks, leaves: waves[w],
    concurrent,
    limitedBy: byCapacity > byLongestTrack ? "capacity" : "longest track",
  };
  cursor = wd(end, 1);
}
const PROJ_END = WIN[waveIds[waveIds.length-1]].end;
// per-leaf target dates: distribute across the module's window
const DATE = {};
TREE.forEach(m => {
  const win = WIN[m.wave];
  const total = modLeaves[m.id] || 1;
  let i = 0;
  const assign = (node, id) => {
    if (!node.k.length) { i++; const day = Math.min(win.days - 1, Math.ceil(i / SCH.LEAVES_PER_TRACK_DAY) - 1); DATE[id] = wd(win.start, Math.max(0, day)); return; }
    node.k.forEach((c, j) => assign(c, id + "." + (j + 1)));
  };
  m.k.forEach((t, j) => assign(t, m.id + "." + (j + 1)));
});

const esc = x => String(x).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

function renderNode(node, id, depth) {
  const leaf = node.k.length === 0;
  let h = `<li class="n d${depth}${leaf ? " leaf" : ""}" data-id="${id}">`;
  h += `<div class="row">`;
  const dt = DATE[id];
  h += leaf ? `<input type="checkbox" class="cb" id="cb-${id}" data-id="${id}" data-due="${dt?iso(dt):""}"><label class="lbl" for="cb-${id}"><span class="tid">${id}</span><span class="txt">${esc(node.n)}</span><span class="due" data-due="${dt?iso(dt):""}">${dt?fmt(dt):""}</span></label>`
            : `<span class="tid">${id}</span><span class="txt grp">${esc(node.n)}</span><span class="prog" data-for="${id}"></span>`;
  h += `</div>`;
  if (!leaf) {
    h += `<ul class="c">`;
    node.k.forEach((c, i) => { h += renderNode(c, id + "." + (i + 1), depth + 1); });
    h += `</ul>`;
  }
  return h + `</li>`;
}

let body = "", toc = "";
for (const m of TREE) {
  let c = 0; m.k.forEach(t => { const f = n => n.k.length ? n.k.forEach(f) : c++; f(t); });
  toc += `<li data-launch="${m.launch?1:0}"><a href="#${m.id}"><span class="n">${m.id}</span>${m.launch?'<span class="ldot" title="in the 30-day launch"></span>':''}${esc(m.name)}</a><span class="ct" data-modcount="${m.id}">${c}</span></li>\n`;
  body += `<section id="${m.id}" data-mod="${m.id}" data-launch="${m.launch?1:0}">\n<h2><span class="sn">${m.id}</span>${esc(m.name)}</h2>\n`;
  body += `<div class="mmeta">${m.launch?`<span class="chip launch">LAUNCH L${m.lwave} · ${SPR.NAMES[m.lwave]}</span><span class="chip date">${fmt(LWIN[m.lwave].start)} – ${fmt(LWIN[m.lwave].end)}</span>${m.trim<1?`<span class="chip trim">${Math.round(m.trim*100)}% in launch</span>`:""}`:`<span class="chip wave">wave ${m.wave} · ${SCH.NAMES[m.wave]||""}</span><span class="chip date">${fmt(WIN[m.wave].start)} – ${fmt(WIN[m.wave].end)}</span>`}<span class="chip neu">${c} items</span><span class="chip modprog" data-modprog="${m.id}">0%</span></div>\n`;
  body += `<ul class="tree">`;
  m.k.forEach((t, i) => { body += renderNode(t, m.id + "." + (i + 1), 2); });
  body += `</ul>\n</section>\n`;
}

let waveRows = "";
for (const w of Object.keys(waves).sort()) {
  const ms = TREE.filter(m => m.wave == w).map(m => m.id).join(" · ");
  const win = WIN[w];
  waveRows += `<tr><td class="k">W${w} · ${SCH.NAMES[w]||""}</td><td class="m">${fmt(win.start)} – ${fmt(win.end)}</td><td class="m">${win.tracks}</td><td class="m" data-wavecount="${w}">${waves[w]}</td><td class="m" data-wavedone="${w}">0</td><td>${ms}</td></tr>
`;
}

const html = `<title>Company Brain Task Tracker</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Libre+Franklin:wght@400;500;600&family=Newsreader:opsz,wght@6..72,400;6..72,600&display=swap">
<style>
:root{--paper:#F2F2F6;--surface:#FFFFFF;--sunk:#E7E7EE;--ink:#15171F;--body:#2F3141;--muted:#5E6076;--rule:#D5D5DE;--accent:#3F35A4;--accent-w:#E6E3F6;--ok:#1E7A4E;--ok-bg:#DCF0E4;--warn:#8A5A12;--warn-bg:#F3E9D9;
--display:"Newsreader",Georgia,serif;--sans:"Libre Franklin",system-ui,Arial,sans-serif;--mono:"JetBrains Mono",ui-monospace,Menlo,monospace;--wide:1180px}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--paper:#0D0E15;--surface:#161825;--sunk:#1D1F2E;--ink:#ECECF3;--body:#C7C8D7;--muted:#8F91A7;--rule:#2A2C3D;--accent:#9A8FE8;--accent-w:#211D44;--ok:#5FC08D;--ok-bg:#12291F;--warn:#D4A653;--warn-bg:#2D2311}}
:root[data-theme="dark"]{--paper:#0D0E15;--surface:#161825;--sunk:#1D1F2E;--ink:#ECECF3;--body:#C7C8D7;--muted:#8F91A7;--rule:#2A2C3D;--accent:#9A8FE8;--accent-w:#211D44;--ok:#5FC08D;--ok-bg:#12291F;--warn:#D4A653;--warn-bg:#2D2311}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--body);font-family:var(--sans);font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.page{max-width:var(--wide);margin:0 auto;padding:0 22px 110px}
header.top{padding:44px 0 20px}
.kicker{font-family:var(--mono);font-size:10.5px;letter-spacing:.15em;text-transform:uppercase;color:var(--accent)}
h1{font-family:var(--display);font-weight:600;font-size:clamp(32px,4.6vw,48px);line-height:1.02;letter-spacing:-.02em;color:var(--ink);margin:6px 0 10px}
.sub{font-family:var(--display);font-size:17.5px;line-height:1.5;max-width:66ch;margin:0 0 18px}

/* sticky tracker */
.bar{position:sticky;top:0;z-index:30;background:var(--surface);border:1px solid var(--rule);border-radius:3px;padding:13px 16px;margin-bottom:16px;box-shadow:0 2px 10px rgba(0,0,0,.06)}
.barTop{display:flex;flex-wrap:wrap;align-items:baseline;gap:14px;justify-content:space-between}
.pct{font-family:var(--display);font-size:34px;font-weight:600;line-height:1;color:var(--accent)}
.pct small{font-size:15px;color:var(--muted);font-weight:400;margin-left:4px}
.stats{display:flex;gap:16px;flex-wrap:wrap;font-family:var(--mono);font-size:11px;color:var(--muted)}
.stats b{color:var(--ink);font-weight:700}
.srcBadge{font-family:var(--mono,monospace);font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:#7a716c;padding:2px 0;display:inline-block}
.srcBadge.live{color:#14724a;font-weight:600}
.cb:disabled+.lbl{cursor:default}
.track{height:9px;background:var(--sunk);border-radius:99px;overflow:hidden;margin-top:11px}
.fill{height:100%;background:var(--accent);width:0%;border-radius:99px;transition:width .25s ease}
.barBot{display:flex;flex-wrap:wrap;gap:8px;margin-top:11px;align-items:center}
.wchip{font-family:var(--mono);font-size:9.5px;padding:3px 8px;border-radius:2px;background:var(--sunk);color:var(--muted);white-space:nowrap}
.wchip b{color:var(--ink)}
.wchip.done{background:var(--ok-bg);color:var(--ok)}
.now{font-size:12.5px;color:var(--body);margin-top:9px;padding-top:9px;border-top:1px dashed var(--rule)}
.now b{color:var(--ink)}
.now .cur{color:var(--accent);font-weight:600}
.acts{display:flex;gap:7px;margin-left:auto}
button{font-family:var(--mono);font-size:10px;letter-spacing:.04em;text-transform:uppercase;padding:5px 10px;border:1px solid var(--rule);background:var(--paper);color:var(--body);border-radius:2px;cursor:pointer}
button:hover{border-color:var(--accent);color:var(--accent)}
button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

nav.toc{border:1px solid var(--rule);background:var(--surface);padding:14px 18px;margin-bottom:8px}
nav.toc b{font-family:var(--mono);font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);display:block;margin-bottom:7px}
nav.toc ol{margin:0;padding:0;list-style:none;columns:3;column-gap:24px}
nav.toc li{font-size:12px;line-height:1.75;break-inside:avoid;display:flex;justify-content:space-between;gap:6px}
nav.toc a{color:var(--body);text-decoration:none;flex:1}
nav.toc a:hover{color:var(--accent)}
nav.toc .n{font-family:var(--mono);font-size:9.5px;color:var(--accent);margin-right:5px}
nav.toc .ct{font-family:var(--mono);font-size:9.5px;color:var(--muted)}

section{padding-top:30px;scroll-margin-top:150px}
h2{font-family:var(--display);font-weight:600;font-size:22px;line-height:1.15;color:var(--ink);margin:0 0 7px;display:flex;gap:10px;align-items:baseline}
h2 .sn{font-family:var(--mono);font-size:11.5px;font-weight:700;color:var(--accent);flex:none}
.mmeta{display:flex;gap:6px;margin-bottom:9px;flex-wrap:wrap}
.chip{font-family:var(--mono);font-size:9px;font-weight:500;letter-spacing:.04em;padding:2px 7px;border-radius:2px;display:inline-block}
.chip.neu{background:var(--sunk);color:var(--muted)}
.chip.wave{background:var(--accent-w);color:var(--accent)}
.chip.modprog{background:var(--sunk);color:var(--muted)}
.chip.modprog.full{background:var(--ok-bg);color:var(--ok)}

ul.tree,ul.c{list-style:none;margin:0;padding:0}
ul.c{margin-left:15px;border-left:1px solid var(--rule);padding-left:11px}
li.n{margin:1px 0}
.row{display:flex;align-items:flex-start;gap:8px;padding:2px 0}
.tid{font-family:var(--mono);font-size:9.5px;color:var(--muted);flex:none;min-width:84px;padding-top:3px}
.txt{font-size:13.5px;line-height:1.45}
.txt.grp{font-weight:600;color:var(--ink)}
li.d2>.row .txt.grp{font-size:14.5px}
li.d3>.row .txt.grp{font-size:13.5px;color:var(--body)}
.cb{margin-top:3px;flex:none;width:15px;height:15px;accent-color:var(--ok);cursor:pointer}
.lbl{display:flex;gap:8px;cursor:pointer;flex:1}
li.leaf.done .txt{color:var(--muted);text-decoration:line-through;text-decoration-color:var(--ok)}
li.leaf.done .lbl::after{content:"DONE";font-family:var(--mono);font-size:8.5px;font-weight:700;letter-spacing:.06em;color:var(--ok);background:var(--ok-bg);padding:2px 5px;border-radius:2px;align-self:flex-start;margin-top:1px;flex:none}
.prog{font-family:var(--mono);font-size:9.5px;color:var(--muted);flex:none;padding-top:3px}
.prog.full{color:var(--ok);font-weight:700}
.due{font-family:var(--mono);font-size:9.5px;color:var(--muted);flex:none;padding-top:3px;margin-left:auto;white-space:nowrap}
.due.late{color:#B4342E;font-weight:700}
li.leaf.done .due{color:var(--ok)}
.chip.date{background:var(--sunk);color:var(--muted)}
.chip.launch{background:var(--ok-bg);color:var(--ok);font-weight:700}
.chip.phase2{background:var(--sunk);color:var(--muted)}
.chip.trim{background:var(--warn-bg);color:var(--warn)}
.ldot{display:inline-block;width:6px;height:6px;border-radius:99px;background:var(--ok);margin-right:5px;vertical-align:middle}
body.lonly section[data-launch="0"]{display:none}
body.lonly nav.toc li[data-launch="0"]{display:none}
button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.sched{display:flex;gap:14px;flex-wrap:wrap;font-family:var(--mono);font-size:10.5px;color:var(--muted);margin-top:9px;padding-top:9px;border-top:1px dashed var(--rule)}
.sched b{color:var(--ink)}
.sched .late{color:#B4342E}

table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
thead th{font-family:var(--mono);font-weight:500;font-size:9px;letter-spacing:.11em;text-transform:uppercase;color:var(--muted);text-align:left;padding:0 10px 6px 0;border-bottom:2px solid var(--ink)}
tbody td{padding:6px 10px 6px 0;border-bottom:1px solid var(--rule)}
td.k{color:var(--ink);font-weight:600;width:11%}
td.m{font-family:var(--mono);font-size:11.5px;color:var(--ink);width:9%}
.rule{border-left:4px solid var(--accent);background:var(--surface);padding:12px 16px;max-width:78ch;font-family:var(--display);font-size:17px;line-height:1.45;color:var(--ink);margin-top:12px}
p{margin:0;max-width:72ch}p+p{margin-top:9px}
footer{margin-top:48px;padding-top:15px;border-top:1px solid var(--rule);font-family:var(--mono);font-size:10px;line-height:1.7;color:var(--muted);max-width:86ch}
@media(max-width:760px){.page{padding:0 13px 70px}nav.toc ol{columns:1}.tid{min-width:70px;font-size:9px}ul.c{margin-left:8px;padding-left:7px}}
@media (prefers-reduced-motion:reduce){.fill{transition:none}}
</style>
<div class="page">
<header class="top">
<div class="kicker">Companion to the architecture · complete work breakdown</div>
<h1>Task Tracker</h1>
<p class="sub">Every task required to build the Company Brain, derived module by module from the architecture. Tick items as they go live. Finishing this list is what "the system is ready" means.</p>

<div class="bar">
  <div class="barTop">
    <div><span class="pct" id="pct">0%</span><small id="pctLbl">complete</small></div>
    <div class="stats">
      <span><b id="cDone">0</b> done</span>
      <span><b id="cTotal">${LEAVES}</b> total</span>
      <span><b id="cLeft">${LEAVES}</b> remaining</span>
      <span><b id="cMods">0</b>/${TREE.length} modules complete</span>
    </div>
    <div class="acts">
      <button id="bExpand">Expand all</button>
      <button id="bCollapse">Collapse</button>
      <button id="bExport">Export</button>
      <button id="bReset">Reset</button>
    </div>
  </div>
  <div class="track"><div class="fill" id="fill"></div></div>
  <div class="barBot" id="waveChips"></div>
  <div class="now" id="now"><b>Current wave:</b> <span class="cur">not started</span></div>
  <div><span class="srcBadge" id="srcBadge">checking source…</span></div>
  <div class="sched"><span>Start <b>${fmt(WIN[waveIds[0]].start)}</b></span><span>Target finish <b>${fmt(PROJ_END)}</b></span><span><b>${waveIds.length}</b> waves</span><span><b>${Math.max.apply(null,waveIds.map(w=>WIN[w].tracks))}</b> max parallel tracks</span><span id="lateBox"></span></div>
</div>

<nav class="toc"><b>Modules · number is the leaf-task count</b><ol>
${toc}</ol></nav>
</header>

<section id="how">
<h2><span class="sn">§</span>How to use this</h2>
<div class="rule">Every leaf is a deliverable with a checkbox. A module is complete when every leaf under it is ticked <em>and</em> its invariants pass. Nothing is done because it looks finished.</div>
<p>Ids run four and five levels deep — <code>M0.2.1</code> is a subtask, <code>M31.1.1.3</code> a step within one. Put the id in the commit message and the traceability file so progress is a query rather than a meeting.</p>
<p>Ticks are stored in this browser only, so each person tracks their own view. Use <strong>Export</strong> to produce a JSON snapshot for the traceability file or to share with the team.</p>
<table><thead><tr><th>Wave</th><th>Dates</th><th>Tracks</th><th>Items</th><th>Done</th><th>Modules</th></tr></thead><tbody>
${waveRows}</tbody></table>
</section>

${body}

<footer>
Derived from the Company Brain architecture, module by module, so coverage is traceable rather than asserted. ${TREE.length} modules, ${NODES} nodes, ${LEAVES} leaf tasks, maximum depth ${MAXD}. Counts are generated from the source data rather than typed, so they cannot drift from the list. A coverage audit against the architecture found and closed gaps in component deployment, role surfaces, onboarding, localisation and accessibility; those are modules M31 to M35.
</footer>
</div>
<script>
(function(){
  var KEY="verz-brain-wbs-v2";
  var state={};
  try{state=JSON.parse(localStorage.getItem(KEY)||"{}")}catch(e){state={}}
  var boxes=[].slice.call(document.querySelectorAll(".cb"));
  var total=boxes.length;
  var waveOf={};
  [].slice.call(document.querySelectorAll("section[data-mod]")).forEach(function(s){
    var w=s.querySelector(".chip.wave").textContent.replace("wave ","").trim();
    waveOf[s.getAttribute("data-mod")]=w;
  });

  function save(){try{localStorage.setItem(KEY,JSON.stringify(state))}catch(e){}}

  function refresh(){
    var done=0, byWave={}, byMod={};
    boxes.forEach(function(b){
      var id=b.getAttribute("data-id");
      var mod=id.split(".")[0];
      var w=waveOf[mod];
      byWave[w]=byWave[w]||{d:0,t:0}; byWave[w].t++;
      byMod[mod]=byMod[mod]||{d:0,t:0}; byMod[mod].t++;
      if(state[id]){done++;byWave[w].d++;byMod[mod].d++;b.checked=true;b.closest("li.leaf").classList.add("done")}
      else{b.checked=false;b.closest("li.leaf").classList.remove("done")}
    });
    var today=new Date();today.setHours(0,0,0,0);
    var late=0;
    boxes.forEach(function(b){
      var li=b.closest("li.leaf"), d=b.getAttribute("data-due");
      var el=li.querySelector(".due");
      if(!d||!el)return;
      var due=new Date(d+"T00:00:00");
      var over=!state[b.getAttribute("data-id")]&&due<today;
      el.classList.toggle("late",over);
      if(over)late++;
    });
    var lb=document.getElementById("lateBox");
    if(lb)lb.innerHTML=late?'<span class="late"><b>'+late+'</b> overdue</span>':'<b>0</b> overdue';
    var p=total?Math.round(done/total*100):0;
    document.getElementById("pct").textContent=p+"%";
    document.getElementById("fill").style.width=p+"%";
    document.getElementById("cDone").textContent=done;
    document.getElementById("cLeft").textContent=total-done;

    // group progress
    [].slice.call(document.querySelectorAll(".prog")).forEach(function(el){
      var pre=el.getAttribute("data-for")+".";
      var t=0,d=0;
      boxes.forEach(function(b){var id=b.getAttribute("data-id");if(id.indexOf(pre)===0){t++;if(state[id])d++}});
      el.textContent=t?d+"/"+t:"";
      el.classList.toggle("full",t>0&&d===t);
    });

    // module chips
    var modsDone=0;
    Object.keys(byMod).forEach(function(m){
      var el=document.querySelector('[data-modprog="'+m+'"]');
      var v=byMod[m].t?Math.round(byMod[m].d/byMod[m].t*100):0;
      if(el){el.textContent=v+"%";el.classList.toggle("full",v===100)}
      if(v===100)modsDone++;
    });
    document.getElementById("cMods").textContent=modsDone;

    // wave chips + table
    var chips="",current=null;
    Object.keys(byWave).sort().forEach(function(w){
      var o=byWave[w],v=o.t?Math.round(o.d/o.t*100):0;
      chips+='<span class="wchip'+(v===100?' done':'')+'">Wave '+w+' <b>'+o.d+'/'+o.t+'</b> '+v+'%</span>';
      var dc=document.querySelector('[data-wavedone="'+w+'"]');if(dc)dc.textContent=o.d;
      if(current===null&&v<100)current=w;
    });
    document.getElementById("waveChips").innerHTML=chips;

    // next tasks
    var nxt=[];
    for(var i=0;i<boxes.length&&nxt.length<3;i++){
      var id=boxes[i].getAttribute("data-id");
      if(!state[id]&&waveOf[id.split(".")[0]]===current){
        nxt.push(id+" "+boxes[i].parentNode.querySelector(".txt").textContent);
      }
    }
    var el=document.getElementById("now");
    if(current===null){el.innerHTML='<b>All waves complete.</b> Run the invariant suite and the restore drill before calling it live.';}
    else{el.innerHTML='<b>Current wave:</b> <span class="cur">Wave '+current+'</span> &nbsp;·&nbsp; <b>Next up:</b> '+(nxt.length?nxt.map(function(x){return x}).join(' &nbsp;|&nbsp; '):'nothing queued in this wave');}
  }

  boxes.forEach(function(b){
    b.addEventListener("change",function(){
      var id=b.getAttribute("data-id");
      if(b.checked)state[id]=1;else delete state[id];
      save();refresh();
    });
  });

  document.getElementById("bExpand").addEventListener("click",function(){
    [].slice.call(document.querySelectorAll("ul.c")).forEach(function(u){u.hidden=false});
  });
  document.getElementById("bCollapse").addEventListener("click",function(){
    [].slice.call(document.querySelectorAll("li.d2 > ul.c")).forEach(function(u){u.hidden=!u.hidden});
  });
  document.getElementById("bExport").addEventListener("click",function(){
    var out={generated:"snapshot",done:Object.keys(state).sort(),total:total,completed:Object.keys(state).length};
    var w=window.open("","_blank");
    if(w){w.document.write("<pre>"+JSON.stringify(out,null,2).replace(/</g,"&lt;")+"</pre>");w.document.close()}
  });
  document.getElementById("bReset").addEventListener("click",function(){
    if(confirm("Clear all ticks in this browser?")){state={};save();refresh()}
  });

  // Server truth. When this page is served by the application, progress comes from
  // merged commits and not from whatever this browser happens to remember. A tick is
  // then evidence rather than a claim, which is the whole point of generating it.
  // Opened as a local file the fetch simply fails and the localStorage behaviour above
  // stays, so the file still works offline.
  fetch("/api/status.json", {cache:"no-store"}).then(function(r){
    if(!r.ok) throw new Error("no status");
    return r.json();
  }).then(function(st){
    state={};
    (st.done_task_ids||[]).forEach(function(id){state[id]=1});
    boxes.forEach(function(b){b.disabled=true;b.title="Set by merged commits, not by hand"});
    var rb=document.getElementById("bReset"); if(rb) rb.remove();
    var badge=document.getElementById("srcBadge");
    if(badge){
      badge.textContent="live from commit "+st.commit;
      badge.className="srcBadge live";
    }
    refresh();
  }).catch(function(){
    var badge=document.getElementById("srcBadge");
    if(badge) badge.textContent="local file · ticks saved in this browser only";
    refresh();
  });
})();
</script>`;

fs.writeFileSync(__dirname + "/../tracker.html", html);
console.log("modules", TREE.length, "| nodes", NODES, "| LEAF TASKS", LEAVES, "| max depth", MAXD);
console.log("leaves by wave:", JSON.stringify(waves));
