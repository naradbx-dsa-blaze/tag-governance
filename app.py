"""Tag Governance — seamless auto-tagging for Databricks chargeback.

A lightweight FastAPI app (no Streamlit, no npm). It shows how much spend is
untagged, then lets you AUTO-TAG a chargeback key in one action: rules first,
AI suggestions for the rest, enqueued as one batch and applied by the writer job
(dry-run by default). Batches are listed and one-click reversible.

The app never mutates a resource itself — it enqueues intent and triggers the
writer/rollback jobs, which do the safe, audited, resumable writes.
"""
from __future__ import annotations

import os
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import db
import jobs
import queries
import tagging

app = FastAPI(title="Tag Governance")

# Env-configured defaults (same vars the bundle already sets).
DEFAULT_DAYS = int(os.environ.get("TAG_GOVERNANCE_DAYS", "30"))


def _err(e: Exception, status: int = 500) -> JSONResponse:
    return JSONResponse({"error": str(e)}, status_code=status)


# ----------------------------------------------------------------------------- API
@app.get("/api/overview")
def overview(days: int = DEFAULT_DAYS, tag_key: str = "cost_center"):
    """Headline KPIs + untagged spend by product."""
    try:
        kpi = db.run_query(queries.kpi_summary(days, [tag_key]))
        products = db.run_query(queries.by_product(days, [tag_key]))
        return {"days": days, "tag_key": tag_key,
                "kpi": kpi[0] if kpi else {}, "products": products}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.get("/api/preview")
def preview(days: int = DEFAULT_DAYS, tag_key: str = "cost_center",
            min_confidence: float = 0.8):
    """What auto-tag WOULD do from AI suggestions (impact + value breakdown)."""
    try:
        impact = db.run_query(queries.suggestions_bulk_impact(tag_key, min_confidence, days))
        breakdown = db.run_query(
            queries.suggestions_bulk_breakdown(tag_key, min_confidence, days, limit=25))
        return {"impact": impact[0] if impact else {}, "breakdown": breakdown}
    except Exception as e:  # noqa: BLE001
        return _err(e)


class AutoTagBody(BaseModel):
    tag_key: str = "cost_center"
    days: int = DEFAULT_DAYS
    min_confidence: float = 0.8
    rules: list | None = None
    dry_run: bool = True


@app.post("/api/auto-tag")
def auto_tag(body: AutoTagBody):
    """Seamless: enqueue rules + AI fallback as one batch, then run the writer."""
    try:
        result = tagging.auto_tag(
            tag_key=body.tag_key, days=body.days, rules=body.rules,
            min_confidence=body.min_confidence,
        )
        if result.get("total_rows", 0) == 0:
            return {**result, "run": None,
                    "message": "Nothing matched — no rules hit and no confident "
                               "suggestions at this cutoff."}
        run = jobs.run_writer(result["batch_id"], dry_run=body.dry_run)
        return {**result, "run": run}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.get("/api/batches")
def batches(limit: int = 25):
    try:
        return {"batches": db.run_query(queries.recent_batches(limit))}
    except Exception as e:  # noqa: BLE001
        return _err(e)


class BatchBody(BaseModel):
    batch_id: str
    dry_run: bool = True


@app.post("/api/rollback")
def rollback(body: BatchBody):
    try:
        return {"run": jobs.run_rollback(body.batch_id, dry_run=body.dry_run)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ----------------------------------------------------------------------------- UI
@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(INDEX_HTML)


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tag Governance</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
         background:#0e1117; color:#e8eefc; }
  header { padding:22px 28px; border-bottom:1px solid #222836;
           display:flex; align-items:baseline; gap:14px; }
  header h1 { margin:0; font-size:1.4rem; letter-spacing:-.01em; }
  header .sub { color:#8b95a7; font-size:.9rem; }
  main { max-width:1080px; margin:0 auto; padding:26px 28px 60px; }
  section { margin-bottom:34px; }
  h2 { font-size:1.05rem; color:#cbd5e6; border-bottom:1px solid #1e2430;
       padding-bottom:8px; }
  .kpis { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }
  .kpi { background:#161b24; border:1px solid #232b39; border-left:4px solid #5b8def;
         border-radius:12px; padding:16px 18px; }
  .kpi.bad { border-left-color:#ff5b5b; } .kpi.warn { border-left-color:#f2b53c; }
  .kpi .lbl { color:#8b95a7; font-size:.74rem; text-transform:uppercase;
              letter-spacing:.04em; }
  .kpi .val { font-size:1.9rem; font-weight:800; margin-top:4px; }
  table { width:100%; border-collapse:collapse; font-size:.9rem; }
  th,td { text-align:left; padding:7px 10px; border-bottom:1px solid #1c2330; }
  th { color:#8b95a7; font-weight:600; font-size:.78rem; text-transform:uppercase; }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  .card { background:#141922; border:1px solid #222a38; border-radius:12px; padding:18px 20px; }
  label { display:block; font-size:.82rem; color:#a9b3c4; margin:12px 0 4px; }
  input,select { background:#0e1117; color:#e8eefc; border:1px solid #2a3342;
                 border-radius:8px; padding:8px 10px; font:inherit; width:220px; }
  .row { display:flex; gap:22px; flex-wrap:wrap; align-items:flex-end; }
  button { background:#3b6fe0; color:#fff; border:0; border-radius:9px;
           padding:10px 18px; font:inherit; font-weight:600; cursor:pointer; margin-top:16px; }
  button.ghost { background:#222b3a; }
  button:disabled { opacity:.5; cursor:default; }
  .toggle { display:flex; align-items:center; gap:8px; margin-top:16px; font-size:.88rem; }
  .note { color:#8b95a7; font-size:.85rem; }
  .pill { display:inline-block; padding:2px 9px; border-radius:999px; font-size:.72rem;
          font-weight:700; background:#1d2c3a; color:#7fb6ff; }
  .ok { color:#5fd08a; } .muted { color:#6b7280; }
  a { color:#7fb6ff; }
  #result { margin-top:16px; }
  .banner { border-radius:10px; padding:12px 14px; margin-top:14px; font-size:.9rem; }
  .banner.info { background:#132030; border:1px solid #244055; }
  .banner.warn { background:#2a2410; border:1px solid #5b4b1e; }
</style>
</head>
<body>
<header>
  <h1>🏷️ Tag Governance</h1>
  <span class="sub">Find untagged spend. Attribute it to teams — automatically.</span>
</header>
<main>

  <section>
    <div class="row">
      <div><label>Tag key</label><input id="tagKey" value="cost_center"></div>
      <div><label>Lookback (days)</label>
        <select id="days"><option>7</option><option>14</option>
          <option selected>30</option><option>60</option><option>90</option></select></div>
    </div>
  </section>

  <section>
    <h2>Untagged spend</h2>
    <div class="kpis" id="kpis"><div class="muted">Loading…</div></div>
    <div style="margin-top:16px"><table id="products"></table></div>
  </section>

  <section>
    <h2>Auto-tag</h2>
    <div class="card">
      <p class="note">Rules run first; the AI fills in the rest above the confidence
         cutoff. Everything goes into <b>one batch</b> the writer job applies — reversible
         in one click below.</p>
      <div class="row">
        <div><label>AI confidence cutoff</label>
          <select id="conf"><option>0.6</option><option>0.7</option>
            <option selected>0.8</option><option>0.9</option></select></div>
        <button class="ghost" onclick="doPreview()">Preview impact</button>
      </div>
      <div id="previewBox"></div>
      <div class="toggle">
        <input type="checkbox" id="dryRun" checked>
        <label for="dryRun" style="margin:0">Dry run (logs intended changes, writes nothing)</label>
      </div>
      <button id="goBtn" onclick="doAutoTag()">Auto-tag now</button>
      <div id="result"></div>
    </div>
  </section>

  <section>
    <h2>Batches &amp; rollback</h2>
    <table id="batches"></table>
  </section>

</main>
<script>
const $ = s => document.querySelector(s);
const money = v => v==null ? "—" : (Math.abs(v)>=1e6 ? "$"+(v/1e6).toFixed(1)+"M"
                    : Math.abs(v)>=1e3 ? "$"+(v/1e3).toFixed(1)+"K" : "$"+Math.round(v));
const q = () => "?days="+$("#days").value+"&tag_key="+encodeURIComponent($("#tagKey").value);

async function loadOverview(){
  const r = await (await fetch("/api/overview"+q())).json();
  if(r.error){ $("#kpis").innerHTML = '<div class="banner warn">'+r.error+'</div>'; return; }
  const k = r.kpi||{};
  $("#kpis").innerHTML =
    kpi("Untagged spend", money(k.untagged_cost), "bad") +
    kpi("% untagged", (k.pct_untagged??"—")+"%", "warn") +
    kpi("Untagged workloads", (k.untagged_workloads??"—").toLocaleString(), "");
  let t = "<tr><th>Product</th><th class=num>Untagged</th><th class=num>Total</th><th class=num>% untagged</th></tr>";
  for(const p of (r.products||[]))
    t += `<tr><td>${p.product}</td><td class=num>${money(p.untagged_cost)}</td>`+
         `<td class=num>${money(p.total_cost)}</td><td class=num>${p.pct_untagged}%</td></tr>`;
  $("#products").innerHTML = t;
}
const kpi = (l,v,c)=>`<div class="kpi ${c}"><div class=lbl>${l}</div><div class=val>${v}</div></div>`;

async function doPreview(){
  $("#previewBox").innerHTML = '<div class="note">Computing…</div>';
  const u = "/api/preview"+q()+"&min_confidence="+$("#conf").value;
  const r = await (await fetch(u)).json();
  if(r.error){ $("#previewBox").innerHTML='<div class="banner warn">'+r.error+'</div>'; return; }
  const i = r.impact||{};
  let html = `<div class="banner info">AI would tag <b>${(i.workloads||0).toLocaleString()}</b> `+
             `workloads (<b>${money(i.cost)}</b>) across ${i.distinct_values||0} values.</div>`;
  if((r.breakdown||[]).length){
    html += "<table><tr><th>Value</th><th class=num>Workloads</th><th class=num>Cost</th><th class=num>Min conf</th></tr>";
    for(const b of r.breakdown)
      html += `<tr><td>${b.value}</td><td class=num>${b.workloads}</td>`+
              `<td class=num>${money(b.cost)}</td><td class=num>${b.min_conf}</td></tr>`;
    html += "</table>";
  }
  $("#previewBox").innerHTML = html;
}

async function doAutoTag(){
  $("#goBtn").disabled = true;
  $("#result").innerHTML = '<div class="note">Enqueuing + starting writer…</div>';
  const body = { tag_key:$("#tagKey").value, days:+$("#days").value,
                 min_confidence:+$("#conf").value, dry_run:$("#dryRun").checked };
  const r = await (await fetch("/api/auto-tag",{method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)})).json();
  $("#goBtn").disabled = false;
  if(r.error){ $("#result").innerHTML='<div class="banner warn">'+r.error+'</div>'; return; }
  if(!r.run){ $("#result").innerHTML='<div class="banner warn">'+(r.message||"Nothing to do")+'</div>'; return; }
  const mode = r.run.dry_run ? "<b>dry-run</b> (nothing written)" : "<b>LIVE</b>";
  $("#result").innerHTML =
    `<div class="banner info">Batch <span class=pill>${r.batch_id}</span> — `+
    `${r.rule_rows} by rules, ${r.ai_rows} by AI. `+
    `Writer started in ${mode}: <a href="${r.run.url}" target=_blank>view run →</a></div>`;
  loadBatches();
}

async function loadBatches(){
  const r = await (await fetch("/api/batches?limit=15")).json();
  if(r.error){ $("#batches").innerHTML=""; return; }
  let t = "<tr><th>Batch</th><th class=num>Rows</th><th class=num>Cost</th>"+
          "<th class=num>Pending</th><th class=num>Done</th><th class=num>Failed</th><th></th></tr>";
  for(const b of (r.batches||[])){
    t += `<tr><td><span class=pill>${b.batch_id}</span></td>`+
         `<td class=num>${b.rows}</td><td class=num>${money(b.cost)}</td>`+
         `<td class=num>${b.pending}</td><td class=num class=ok>${b.succeeded}</td>`+
         `<td class=num>${b.failed}</td>`+
         `<td><button class=ghost style="margin:0;padding:5px 12px" `+
         `onclick="doRollback('${b.batch_id}')">Rollback</button></td></tr>`;
  }
  $("#batches").innerHTML = t;
}

async function doRollback(id){
  if(!confirm("Roll back "+id+"? (starts as dry-run)")) return;
  const r = await (await fetch("/api/rollback",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({batch_id:id, dry_run:true})})).json();
  if(r.error){ alert(r.error); return; }
  alert("Rollback started (dry-run): "+r.run.url);
}

$("#days").onchange = loadOverview;
$("#tagKey").onchange = loadOverview;
loadOverview(); loadBatches();
</script>
</body></html>"""
