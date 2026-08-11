"""The user interface, as a single self-contained page.

Held as a Python string rather than a data file so the packaged application is
one executable with nothing to locate at runtime — a missing asset path is the
most common way a packaged app breaks on a machine that is not the developer's.

No external stylesheet, font, script or image: the page loads from the local
process and reaches nothing on the network.
"""

from __future__ import annotations

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CCP Forge</title>
<style>
  :root {
    --bg:#0f1115; --panel:#171a21; --panel2:#1d212a; --line:#2a2f3a;
    --text:#e6e9ef; --dim:#9aa3b2; --accent:#4f8cff; --accent2:#22c55e;
    --warn:#f59e0b; --err:#ef4444; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--bg); color:var(--text);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  }
  header {
    display:flex; align-items:center; gap:12px; padding:14px 20px;
    border-bottom:1px solid var(--line); background:var(--panel); position:sticky; top:0; z-index:5;
  }
  .logo { width:26px;height:26px;border-radius:6px;background:linear-gradient(135deg,var(--accent),#8b5cf6); }
  h1 { font-size:16px; margin:0; font-weight:650; letter-spacing:.2px; }
  .sub { color:var(--dim); font-size:12px; }
  .spacer { flex:1; }
  .pill { font-size:11px; padding:3px 9px; border-radius:999px; border:1px solid var(--line); color:var(--dim); }
  .pill.ok { color:var(--accent2); border-color:#1f4d33; background:#0f2a1c; }
  .pill.busy { color:var(--warn); border-color:#4d3a12; background:#2a1f0c; }
  .pill.err { color:var(--err); border-color:#4d1f1f; background:#2a0f0f; }
  main { padding:20px; max-width:1180px; margin:0 auto; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:18px; margin-bottom:16px; }
  .card h2 { margin:0 0 4px; font-size:14px; font-weight:650; }
  .card p.hint { margin:0 0 14px; color:var(--dim); font-size:12.5px; }
  label { display:block; font-size:12px; color:var(--dim); margin:0 0 5px; }
  input[type=text], input[type=number], select {
    width:100%; padding:9px 11px; background:var(--panel2); color:var(--text);
    border:1px solid var(--line); border-radius:7px; font-size:13px; font-family:inherit;
  }
  input:focus, select:focus { outline:none; border-color:var(--accent); }
  button {
    padding:9px 15px; border-radius:7px; border:1px solid var(--line);
    background:var(--panel2); color:var(--text); font-size:13px; cursor:pointer; font-family:inherit;
  }
  button:hover:not(:disabled) { border-color:var(--accent); }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }
  button.primary:hover:not(:disabled) { filter:brightness(1.1); }
  button:disabled { opacity:.45; cursor:not-allowed; }
  .row { display:flex; gap:10px; align-items:flex-end; flex-wrap:wrap; }
  .row > * { flex:1; min-width:120px; }
  .row > button { flex:0 0 auto; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
  .stat { background:var(--panel2); border:1px solid var(--line); border-radius:8px; padding:12px; }
  .stat .k { font-size:11px; color:var(--dim); text-transform:uppercase; letter-spacing:.5px; }
  .stat .v { font-size:19px; font-weight:650; margin-top:3px; }
  .stat .v.good { color:var(--accent2); }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th, td { text-align:left; padding:7px 9px; border-bottom:1px solid var(--line); }
  th { color:var(--dim); font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.4px; }
  tbody tr { cursor:pointer; }
  tbody tr:hover { background:var(--panel2); }
  tbody tr.sel { background:#12233f; }
  .mono { font-family:var(--mono); font-size:12px; }
  .scroll { max-height:300px; overflow:auto; border:1px solid var(--line); border-radius:8px; }
  pre.out {
    background:#0b0d11; border:1px solid var(--line); border-radius:8px; padding:12px;
    font-family:var(--mono); font-size:12px; max-height:260px; overflow:auto; white-space:pre-wrap;
    word-break:break-all; margin:10px 0 0;
  }
  .mode { display:inline-block; padding:4px 10px; border-radius:6px; font-size:11px; font-weight:700; letter-spacing:.6px; }
  .mode.sel { background:#0f2a1c; color:var(--accent2); border:1px solid #1f4d33; }
  .mode.full { background:#2a1f0c; color:var(--warn); border:1px solid #4d3a12; }
  .bar { height:6px; background:var(--panel2); border-radius:99px; overflow:hidden; margin-top:8px; }
  .bar > i { display:block; height:100%; background:var(--accent); transition:width .2s; }
  .err-box { border:1px solid #4d1f1f; background:#1a0f10; border-radius:8px; padding:14px; }
  .err-box .t { font-weight:650; color:#fca5a5; margin-bottom:6px; }
  .err-box .l { font-size:12.5px; color:var(--dim); margin-top:5px; }
  .err-box .l b { color:var(--text); font-weight:600; }
  .hide { display:none !important; }
  .drop { border:1.5px dashed var(--line); border-radius:10px; padding:22px; text-align:center; color:var(--dim); font-size:13px; }
  .drop.hot { border-color:var(--accent); background:#12233f; color:var(--text); }
  .tabs { display:flex; gap:5px; margin-bottom:14px; flex-wrap:wrap; }
  .tabs button.on { background:var(--accent); border-color:var(--accent); color:#fff; }
  .note { font-size:11.5px; color:var(--dim); margin-top:9px; }
  .kv { display:flex; justify-content:space-between; padding:5px 0; border-bottom:1px solid var(--line); font-size:12.5px; }
  .kv span:first-child { color:var(--dim); }
  .kv .mono { font-size:11.5px; }
</style>
</head>
<body>
<header>
  <div class="logo"></div>
  <div>
    <h1>CCP Forge</h1>
    <div class="sub">Copy · Change · Paste — representation and selective access</div>
  </div>
  <div class="spacer"></div>
  <span id="state" class="pill">no input</span>
</header>

<main>
  <!-- ERROR -->
  <div id="errCard" class="card hide">
    <div class="err-box">
      <div class="t" id="errTitle"></div>
      <div id="errWhat"></div>
      <div class="l"><b>Why:</b> <span id="errWhy"></span></div>
      <div class="l"><b>What to do:</b> <span id="errNext"></span></div>
    </div>
    <div style="margin-top:12px"><button onclick="dismissError()">Dismiss</button></div>
  </div>

  <!-- START -->
  <div id="startCard" class="card">
    <h2>Choose input</h2>
    <p class="hint">Point CCP at a project folder, or drop a .zip archive. CCP analyses it,
      builds the representation, and verifies every unit automatically.</p>
    <div class="row">
      <div style="flex:3">
        <label for="path">Project folder or .zip path</label>
        <input type="text" id="path" placeholder="/path/to/project" spellcheck="false">
      </div>
      <button class="primary" id="buildBtn" onclick="startBuild()">Build artifact</button>
      <button id="analyseBtn" onclick="analyse()">Analyse only</button>
    </div>
    <div class="drop" id="drop" style="margin-top:14px">Drop a <b>.zip</b> here to load it</div>
    <div style="margin-top:16px" class="row">
      <div style="flex:3">
        <label for="openPath">…or open an artifact you saved earlier (.ccp)</label>
        <input type="text" id="openPath" placeholder="/path/to/artifact.ccp" spellcheck="false">
      </div>
      <button onclick="openArtifact()">Open artifact</button>
    </div>
    <div id="recents" style="margin-top:14px"></div>
    <div class="note" id="limits"></div>
  </div>

  <!-- PROGRESS -->
  <div id="progCard" class="card hide">
    <h2>Building</h2>
    <div id="progMsg" class="sub"></div>
    <div class="bar"><i id="progBar" style="width:0%"></i></div>
    <div style="margin-top:12px"><button onclick="cancelBuild()">Cancel</button></div>
  </div>

  <!-- ANALYSIS -->
  <div id="anaCard" class="card hide">
    <h2>Input analysis</h2>
    <div class="grid" id="anaGrid"></div>
    <div class="note" id="anaSkipped"></div>
  </div>

  <!-- OVERVIEW -->
  <div id="artCard" class="card hide">
    <h2 id="artLabel">Artifact</h2>
    <p class="hint mono" id="artSource"></p>
    <div class="grid" id="artGrid"></div>
    <div class="row" style="margin-top:16px">
      <div style="flex:3">
        <label for="savePath">Save artifact to</label>
        <input type="text" id="savePath" spellcheck="false">
      </div>
      <button onclick="saveArtifact()">Save</button>
      <button onclick="verifyArtifact()">Verify</button>
      <button onclick="closeArtifact()">Close</button>
    </div>
    <div id="verifyOut" class="note"></div>
  </div>

  <!-- UNITS -->
  <div id="unitsCard" class="card hide">
    <div class="tabs">
      <button id="tabUnits" class="on" onclick="showTab('units')">Units</button>
      <button id="tabGroups" onclick="showTab('groups')">Shared-base groups</button>
    </div>
    <div id="unitsPane">
      <div class="row" style="margin-bottom:10px">
        <div style="flex:4"><input type="text" id="q" placeholder="Filter units…" oninput="loadUnits()" spellcheck="false"></div>
        <span class="sub" id="unitCount" style="flex:0 0 auto;align-self:center"></span>
      </div>
      <div class="scroll">
        <table>
          <thead><tr><th>Unit</th><th>Kind</th><th>Size</th><th>Stored</th><th>Instr</th></tr></thead>
          <tbody id="unitRows"></tbody>
        </table>
      </div>
    </div>
    <div id="groupsPane" class="hide">
      <div class="scroll">
        <table>
          <thead><tr><th>Base unit</th><th>Base size</th><th>Units sharing it</th></tr></thead>
          <tbody id="groupRows"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- READ -->
  <div id="readCard" class="card hide">
    <h2>Read <span class="mono" id="selUid"></span></h2>
    <p class="hint" id="selMeta"></p>
    <div class="row">
      <div><label for="off">Offset</label><input type="number" id="off" value="0" min="0"></div>
      <div><label for="len">Length</label><input type="number" id="len" value="256" min="0"></div>
      <button class="primary" onclick="doRead()">Selective read</button>
      <button onclick="doMaterialize()">Full materialization</button>
    </div>
    <div class="note">A <b>selective read</b> executes only the instructions covering the window.
      A <b>full materialization</b> rebuilds the whole unit and checks it against its digest.</div>

    <div id="resBox" class="hide">
      <div style="margin-top:16px"><span id="resMode" class="mode"></span></div>
      <div class="grid" id="resGrid" style="margin-top:12px"></div>
      <pre class="out" id="resOut"></pre>
      <div class="row" style="margin-top:12px">
        <div style="flex:3"><label for="expDir">Export this unit's exact bytes to folder</label>
          <input type="text" id="expDir" spellcheck="false"></div>
        <button onclick="exportUnit()">Export unit</button>
      </div>
      <div class="note" id="expOut"></div>
    </div>
  </div>
</main>

<script>
"use strict";
let SEL = null, POLL = null, LIMITS = null;

function fmt(n) {
  if (n === null || n === undefined) return "—";
  const u = ["B","KB","MB","GB"]; let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length-1) { v /= 1024; i++; }
  return i === 0 ? v + " B" : v.toFixed(2) + " " + u[i];
}
function pct(x) { return (x === null || x === undefined) ? "—" : (x*100).toFixed(2) + "%"; }
function el(id) { return document.getElementById(id); }
function show(id, on) { el(id).classList.toggle("hide", !on); }

async function api(path, opts) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch (e) { body = null; }
  if (!res.ok) {
    const err = (body && body.error) || {title:"Request failed", what:res.status+" "+res.statusText, why:"The application could not complete the request.", next:"Try again."};
    showError(err);
    throw new Error(err.title);
  }
  return body;
}
function showError(e) {
  el("errTitle").textContent = e.title || "Error";
  el("errWhat").textContent = e.what || "";
  el("errWhy").textContent = e.why || "";
  el("errNext").textContent = e.next || "";
  show("errCard", true);
  window.scrollTo({top:0, behavior:"smooth"});
}
function dismissError() { show("errCard", false); api("/api/error/clear", {method:"POST"}).catch(()=>{}); }

function setState(s) {
  const p = el("state"); p.textContent = s.replace("_"," ");
  p.className = "pill" + (s==="ready" ? " ok" : (s==="error" ? " err" :
    (["analysing","building","verifying","reading","materializing","exporting"].includes(s) ? " busy" : "")));
}

async function refresh() {
  const s = await api("/api/state");
  setState(s.state);
  LIMITS = s.limits;
  el("limits").textContent = "Limits: up to " + s.limits.max_units + " files, " +
    fmt(s.limits.max_total_bytes) + " total, " + fmt(s.limits.max_unit_bytes) + " per file. " +
    "Selective reads capped at " + fmt(s.limits.max_selective_read_bytes) + ".";

  const running = s.status && s.status.running;
  show("progCard", running);
  if (running) {
    el("progMsg").textContent = (s.status.stage || "") + " — " + (s.status.message || "");
    el("progBar").style.width = ((s.status.fraction || 0) * 100) + "%";
  }
  if (s.error) showError(s.error); else show("errCard", false);

  if (s.analysis) {
    show("anaCard", true);
    el("anaGrid").innerHTML = stat("Kind", s.analysis.kind) + stat("Files", s.analysis.unit_count) +
      stat("Total size", fmt(s.analysis.total_bytes)) + stat("Largest file", fmt(s.analysis.largest_unit_bytes));
    el("anaSkipped").textContent = s.analysis.skipped.length
      ? "Skipped " + s.analysis.skipped.length + ": " + s.analysis.skipped.slice(0,5).map(x=>x.name+" ("+x.reason+")").join(", ")
      : "";
  } else show("anaCard", false);

  const a = s.artifact;
  show("artCard", !!a); show("unitsCard", !!a);
  if (a) {
    el("artLabel").textContent = a.label;
    el("artSource").textContent = a.source;
    el("artGrid").innerHTML =
      stat("Original", fmt(a.original_bytes)) +
      stat("Representation", fmt(a.artifact_bytes)) +
      stat("Saving", pct(a.saving), true) +
      stat("Units", a.units) +
      stat("Shared bases", a.groups) +
      stat("Verified", a.verified ? "yes" : "not yet", a.verified);
    if (!el("savePath").value) el("savePath").value = (s.output_dir || "") + "/" + a.label + ".ccp";
    if (!el("expDir").value) el("expDir").value = (s.output_dir || "") + "/exported";
    if (!el("unitRows").children.length) loadUnits();
  } else { show("readCard", false); SEL = null; }

  if (!running && POLL) { clearInterval(POLL); POLL = null; }
  return s;
}
function stat(k, v, good) {
  return '<div class="stat"><div class="k">'+k+'</div><div class="v'+(good?" good":"")+'">'+v+'</div></div>';
}

async function loadRecents() {
  const r = await api("/api/recents");
  if (!r.recents.length) { el("recents").innerHTML = ""; return; }
  el("recents").innerHTML = '<label>Recent artifacts</label>' + r.recents.map(x =>
    '<div class="kv"><span class="mono">'+esc(x.path)+'</span><span>'+
    '<button onclick="openPath('+JSON.stringify(x.path).replace(/"/g,"&quot;")+')">Open</button></span></div>').join("");
}
function esc(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function openPath(p) { el("openPath").value = p; openArtifact(); }

async function analyse() {
  await api("/api/analyse", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({path: el("path").value})});
  refresh();
}
async function startBuild() {
  el("buildBtn").disabled = true;
  try {
    await api("/api/build", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({path: el("path").value})});
    if (POLL) clearInterval(POLL);
    POLL = setInterval(refresh, 250);
    refresh();
  } finally { el("buildBtn").disabled = false; }
}
async function cancelBuild() { await api("/api/cancel", {method:"POST"}); refresh(); }
async function openArtifact() {
  await api("/api/open", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({path: el("openPath").value})});
  el("unitRows").innerHTML = ""; refresh();
}
async function closeArtifact() { await api("/api/close", {method:"POST"}); el("unitRows").innerHTML=""; refresh(); }
async function saveArtifact() {
  const r = await api("/api/save", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({path: el("savePath").value, overwrite: true})});
  el("verifyOut").textContent = "Saved to " + r.path;
  loadRecents(); refresh();
}
async function verifyArtifact() {
  const r = await api("/api/verify", {method:"POST"});
  el("verifyOut").textContent = r.ok
    ? "Verified " + r.units_ok + "/" + r.units_checked + " units (" + fmt(r.bytes_verified) + ") in " + (r.elapsed_seconds*1000).toFixed(1) + " ms"
    : "FAILED: " + r.failures.length + " unit(s) did not match their digest";
  refresh();
}

function showTab(which) {
  el("tabUnits").classList.toggle("on", which==="units");
  el("tabGroups").classList.toggle("on", which==="groups");
  show("unitsPane", which==="units"); show("groupsPane", which==="groups");
  if (which==="groups") loadGroups();
}
async function loadUnits() {
  const r = await api("/api/units?q=" + encodeURIComponent(el("q").value) + "&limit=300");
  el("unitCount").textContent = r.total + " unit(s)";
  el("unitRows").innerHTML = r.units.map(u =>
    '<tr onclick="selectUnit('+JSON.stringify(u.uid).replace(/"/g,"&quot;")+')" data-uid="'+esc(u.uid)+'">'+
    '<td class="mono">'+esc(u.uid)+'</td><td>'+u.kind+'</td><td>'+fmt(u.size)+'</td>'+
    '<td>'+fmt(u.stored_bytes)+'</td><td>'+u.instructions+'</td></tr>').join("");
}
async function loadGroups() {
  const r = await api("/api/groups");
  el("groupRows").innerHTML = r.groups.map(g =>
    '<tr><td class="mono">'+esc(g.base_uid)+'</td><td>'+fmt(g.base_size)+'</td><td>'+g.member_count+'</td></tr>').join("")
    || '<tr><td colspan="3" class="sub">No shared bases in this artifact.</td></tr>';
}
async function selectUnit(uid) {
  SEL = uid;
  const d = await api("/api/unit?uid=" + encodeURIComponent(uid));
  el("selUid").textContent = uid;
  el("selMeta").textContent = d.kind + " · " + fmt(d.size) + " · stored " + fmt(d.stored_bytes) +
    " · " + d.instructions + " instruction(s)" + (d.base_uid ? " · base: " + d.base_uid : "");
  show("readCard", true); show("resBox", false);
  for (const tr of el("unitRows").children) tr.classList.toggle("sel", tr.dataset.uid === uid);
  el("readCard").scrollIntoView({behavior:"smooth", block:"nearest"});
}
async function doRead() {
  if (!SEL) return;
  const r = await api("/api/read", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({uid: SEL, offset: Number(el("off").value||0), length: Number(el("len").value||0)})});
  renderResult(r);
}
async function doMaterialize() {
  if (!SEL) return;
  const r = await api("/api/materialize", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({uid: SEL})});
  renderResult(r);
}
function renderResult(r) {
  show("resBox", true);
  const m = r.metrics, sel = r.mode === "selective";
  const mode = el("resMode");
  mode.className = "mode " + (sel ? "sel" : "full");
  mode.textContent = sel ? "SELECTIVE RECONSTRUCTION" : "FULL MATERIALIZATION";
  el("resGrid").innerHTML =
    stat("Requested", fmt(m.bytes_requested)) +
    stat("Returned", fmt(m.bytes_returned)) +
    stat("Bytes touched", fmt(m.bytes_touched), sel) +
    stat("Work ratio", m.work_ratio === null ? "—" : m.work_ratio.toFixed(3)) +
    stat("Units touched", m.units_touched) +
    stat("Instructions", m.instructions_visited + " / " + m.instructions_total) +
    stat("Verification", m.verification === "digest_checked" ? "digest checked" : "slice (unverified)",
         m.verification === "digest_checked") +
    stat("Elapsed", (m.elapsed_seconds*1000).toFixed(3) + " ms");
  const p = r.preview;
  el("resOut").textContent = p.is_text && p.text ? p.text
    : (p.hex.match(/.{1,2}/g) || []).join(" ") + (p.truncated ? "\n… preview truncated" : "");
}
async function exportUnit() {
  if (!SEL) return;
  const r = await api("/api/export", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({uid: SEL, dir: el("expDir").value, overwrite: true})});
  el("expOut").textContent = "Wrote " + fmt(r.bytes) + " to " + r.path + " (digest checked)";
}

// drag & drop a .zip
const drop = el("drop");
["dragenter","dragover"].forEach(e => drop.addEventListener(e, ev => {
  ev.preventDefault(); drop.classList.add("hot"); }));
["dragleave","drop"].forEach(e => drop.addEventListener(e, ev => {
  ev.preventDefault(); drop.classList.remove("hot"); }));
drop.addEventListener("drop", async ev => {
  const f = ev.dataTransfer.files[0];
  if (!f) return;
  drop.textContent = "Uploading " + f.name + "…";
  const buf = await f.arrayBuffer();
  try {
    const r = await api("/api/upload?name=" + encodeURIComponent(f.name), {method:"POST", body: buf});
    el("path").value = r.path;
    drop.textContent = f.name + " ready — press Build artifact";
  } catch (e) { drop.textContent = "Drop a .zip here to load it"; }
});

refresh().then(loadRecents);
</script>
</body>
</html>
"""
