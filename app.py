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
    """What auto-tag WOULD do: total impact + the CONCRETE per-workload list, so
    you can see exactly which workloads get tagged and with what value first."""
    try:
        impact = db.run_query(queries.suggestions_bulk_impact(tag_key, min_confidence, days))
        workloads = db.run_query(
            queries.suggestions_bulk_list(tag_key, min_confidence, days, limit=200))
        excluded = db.run_query(
            queries.policy_governed_impact(tag_key, min_confidence, days))
        return {"impact": impact[0] if impact else {}, "workloads": workloads,
                "excluded": excluded[0] if excluded else {}}
    except Exception as e:  # noqa: BLE001
        return _err(e)


class AutoTagBody(BaseModel):
    tag_key: str = "cost_center"
    days: int = DEFAULT_DAYS
    min_confidence: float = 0.8
    rules: list | None = None
    use_ai: bool = True
    dry_run: bool = True


@app.post("/api/auto-tag")
def auto_tag(body: AutoTagBody):
    """Seamless: enqueue rules (+ optional AI fallback) as one batch, run writer.

    use_ai=False → pure rules, no AI-proposed tags (for AI-averse customers)."""
    try:
        result = tagging.auto_tag(
            tag_key=body.tag_key, days=body.days, rules=body.rules,
            min_confidence=body.min_confidence, use_ai=body.use_ai,
        )
        if result.get("total_rows", 0) == 0:
            hint = ("no rules matched" if not body.use_ai
                    else "no rules hit and no confident suggestions at this cutoff")
            return {**result, "run": None, "message": f"Nothing matched — {hint}."}
        run = jobs.run_writer(result["batch_id"], dry_run=body.dry_run)
        return {**result, "run": run}
    except Exception as e:  # noqa: BLE001
        return _err(e)


class RulePreviewBody(BaseModel):
    tag_key: str = "cost_center"
    days: int = DEFAULT_DAYS
    rules: list


@app.post("/api/rule-preview")
def rule_preview(body: RulePreviewBody):
    """What a rule set would tag: aggregate impact + the CONCRETE per-workload list
    (name/product/owner/cost + the exact value each gets), so a human can catch a
    rule that mislabels unrelated workloads before applying. No AI, no writes."""
    try:
        impact = db.run_query(
            queries.bulk_rule_impact(body.days, [body.tag_key], body.rules))
        sample = db.run_query(
            queries.bulk_rule_sample(body.days, [body.tag_key], body.rules, limit=200))
        return {"impact": impact[0] if impact else {}, "workloads": sample}
    except Exception as e:  # noqa: BLE001
        return _err(e)


class ManualTagBody(BaseModel):
    product: str
    workload_id: str
    workload_name: str = ""
    workspace_id: str = ""
    is_serverless: bool = False
    tag_key: str = "cost_center"
    tag_value: str
    list_cost: float | None = None
    dry_run: bool = True


@app.post("/api/manual-tag")
def manual_tag(body: ManualTagBody):
    """Tag ONE workload with an explicitly typed value — no AI, no rules."""
    try:
        plan = tagging.plan_for(
            product=body.product, workload_id=body.workload_id,
            workload_name=body.workload_name, tags={body.tag_key: body.tag_value},
            is_serverless=body.is_serverless,
        )
        res = tagging.enqueue_single(
            plan, workspace_id=body.workspace_id,
            is_serverless=body.is_serverless, list_cost=body.list_cost)
        if res.get("status") != "ENQUEUED":
            return res
        run = jobs.run_writer(res["batch_id"], dry_run=body.dry_run)
        return {**res, "run": run}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.get("/api/batches")
def batches(limit: int = 25):
    try:
        return {"batches": db.run_query(queries.recent_batches(limit))}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.get("/api/field-values")
def field_values(field: str = "owner", days: int = DEFAULT_DAYS):
    """Distinct values of a rule field (owner/product/workspace/name) for the
    rules-builder picker, so users select real values instead of typing emails."""
    try:
        return {"values": db.run_query(queries.distinct_field_values(field, days))}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.get("/api/not-taggable")
def not_taggable(days: int = DEFAULT_DAYS, tag_key: str = "cost_center"):
    """Untagged spend that can't be API-tagged even as admin — needs manual action."""
    try:
        return {"rows": db.run_query(queries.not_taggable_breakdown(tag_key, days))}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.get("/api/batch-detail")
def batch_detail(batch_id: str):
    """Why rows in a batch didn't tag — per-product FAILED/UNSUPPORTED + reason."""
    try:
        return {"breakdown": db.run_query(queries.batch_failure_breakdown(batch_id))}
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
  button.mode { background:#1a2130; border:1px solid #2a3342; margin-top:0; }
  button.mode.active { background:#3b6fe0; border-color:#3b6fe0; }
  .pane { margin-top:16px; }
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
    <h2>Tag workloads</h2>
    <div class="card">
      <div class="row" style="gap:10px">
        <button class="mode" id="mode-ai"    onclick="setMode('ai')">🤖 AI suggestions</button>
        <button class="mode" id="mode-rules" onclick="setMode('rules')">📋 Rules (no AI)</button>
        <button class="mode" id="mode-manual" onclick="setMode('manual')">✏️ Manual (one workload)</button>
      </div>

      <!-- AI MODE -->
      <div id="pane-ai" class="pane">
        <p class="note"><b>AI proposes a value</b> for each untagged workload above the
           confidence cutoff. Review the exact list, then apply. One batch, reversible.</p>
        <div class="row">
          <div><label>Confidence cutoff</label>
            <select id="conf"><option>0.6</option><option>0.7</option>
              <option selected>0.8</option><option>0.9</option></select></div>
          <button class="ghost" onclick="doPreview()">1️⃣ Preview which workloads</button>
        </div>
        <div id="previewBox"></div>
        <div id="applyRow" style="display:none">
          <div class="toggle"><input type="checkbox" id="dryRun">
            <label for="dryRun" style="margin:0">Dry run only (preview, writes nothing)</label></div>
          <button id="goBtn" onclick="doAutoTag()">2️⃣ Apply tags to these workloads</button>
        </div>
      </div>

      <!-- RULES MODE (no AI) -->
      <div id="pane-rules" class="pane" style="display:none">
        <p class="note"><b>Deterministic rules</b> — no AI. A workload is tagged when a
           field matches. Earlier rules win. Add one or more, preview the impact, apply.</p>
        <table id="rulesTable"><tr><th>If field</th><th>operator</th><th>value</th>
          <th>→ set value</th><th></th></tr></table>
        <button class="ghost" onclick="addRule()">+ Add rule</button>
        <div class="toggle"><input type="checkbox" id="rulesUseAi">
          <label for="rulesUseAi" style="margin:0">Also use AI for workloads no rule matched (off = pure rules)</label></div>
        <div class="row" style="margin-top:6px">
          <button onclick="doRulePreview()">1️⃣ Show the workloads this will tag</button>
        </div>
        <p class="note">You must review the exact workloads before you can apply — a rule
           can lump unrelated workloads under one team.</p>
        <div id="rulePreviewBox"></div>
        <div id="rulesApplyRow" style="display:none">
          <div class="toggle"><input type="checkbox" id="rulesDryRun">
            <label for="rulesDryRun" style="margin:0">Dry run only (preview, writes nothing)</label></div>
          <button onclick="doRulesApply()">2️⃣ Apply tags to the workloads above</button>
        </div>
      </div>

      <!-- MANUAL MODE -->
      <div id="pane-manual" class="pane" style="display:none">
        <p class="note"><b>Tag a single workload</b> with a value you type — no AI, no rules.</p>
        <div class="row">
          <div><label>Product</label><input id="mProduct" placeholder="JOBS"></div>
          <div><label>Workload id</label><input id="mId" placeholder="job_id / warehouse_id / …"></div>
          <div><label>Name (optional)</label><input id="mName"></div>
        </div>
        <div class="row">
          <div><label>Tag value</label><input id="mValue" placeholder="e.g. data-eng"></div>
          <div class="toggle" style="margin-top:22px"><input type="checkbox" id="mDryRun">
            <label for="mDryRun" style="margin:0">Dry run only (preview, writes nothing)</label></div>
        </div>
        <button onclick="doManual()">Tag this workload</button>
      </div>

      <div id="result"></div>
    </div>
  </section>

  <section>
    <h2>Tagged a different way <span class="note">(not a per-resource tag write — see how, per bucket)</span></h2>
    <div id="notTaggable"><div class="muted">Loading…</div></div>
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
// LIVE apply confirmation. dry_run skips the prompt (nothing is written).
function confirmLive(dryRun, what){
  if(dryRun) return true;
  return confirm("Apply tags for REAL to "+what+"?\\n\\nThis writes tags to live "+
                 "resources (reversible via rollback). Cancel to stop.");
}
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
  $("#applyRow").style.display = "none";
  const u = "/api/preview"+q()+"&min_confidence="+$("#conf").value;
  const r = await (await fetch(u)).json();
  if(r.error){ $("#previewBox").innerHTML='<div class="banner warn">'+r.error+'</div>'; return; }
  const i = r.impact||{}, wl = r.workloads||[], ex = r.excluded||{};
  if(!wl.length){
    $("#previewBox").innerHTML = '<div class="banner warn">No <b>taggable</b> workloads have a '+
      'confident suggestion at this cutoff. Lower the cutoff, or (re)run the annotate job.</div>';
    return;
  }
  let html = `<div class="banner info">This will tag <b>${(i.workloads||0).toLocaleString()}</b> `+
             `workloads (<b>${money(i.cost)}</b>) with tag key `+
             `<span class=pill>${$("#tagKey").value}</span>. Exact list`+
             (wl.length<i.workloads ? ` (top ${wl.length} by cost)` : "")+`:</div>`;
  if((ex.workloads||0) > 0)
    html += `<div class="banner warn">Not shown: <b>${ex.workloads.toLocaleString()}</b> workloads `+
            `(<b>${money(ex.cost)}</b>) are <b>policy-governed</b> (Apps, serverless SQL, …) — `+
            `they can't take a per-resource tag and need a budget/tag policy instead. `+
            `That's why they don't appear here and never get "tagged".</div>`;
  html += "<div style='max-height:340px;overflow:auto'><table>"+
          "<tr><th>Workload</th><th>Product</th><th class=num>Cost</th>"+
          "<th>Will be tagged</th><th class=num>Conf</th></tr>";
  for(const w of wl)
    html += `<tr><td>${w.workload_name||w.workload_id}</td><td>${w.product}</td>`+
            `<td class=num>${money(w.cost)}</td>`+
            `<td><span class=pill>${$("#tagKey").value} = ${w.new_tag_value}</span></td>`+
            `<td class=num>${w.confidence}</td></tr>`;
  html += "</table></div>";
  $("#previewBox").innerHTML = html;
  $("#applyRow").style.display = "block";   // reveal Apply only after a preview
}

async function doAutoTag(){
  const dry = $("#dryRun").checked;
  if(!confirmLive(dry, "the previewed workloads")) return;
  $("#goBtn").disabled = true;
  $("#result").innerHTML = '<div class="note">Enqueuing + starting writer…</div>';
  const body = { tag_key:$("#tagKey").value, days:+$("#days").value,
                 min_confidence:+$("#conf").value, dry_run:dry };
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

// ---- mode switcher -------------------------------------------------------
function setMode(m){
  for(const x of ["ai","rules","manual"]){
    $("#pane-"+x).style.display = (x===m)?"block":"none";
    $("#mode-"+x).classList.toggle("active", x===m);
  }
  $("#result").innerHTML = "";
}

// ---- rules mode (no AI) --------------------------------------------------
let _rid = 0;
function addRule(){
  const tb = $("#rulesTable");
  const id = ++_rid;
  const tr = document.createElement("tr");
  // The value box is backed by a <datalist> of REAL values for the chosen field
  // (owner emails, product names, …) so you pick from actual data instead of
  // typing a raw email. Changing the field refetches the list.
  tr.innerHTML =
    `<td><select class=rf onchange="fillFieldValues(${id}, this.value); rulesDirty()">`+
    `<option>owner</option><option>name</option><option>product</option>`+
    `<option>workspace</option></select></td>`+
    `<td><select class=ro onchange="rulesDirty()"><option>contains</option><option>equals</option>`+
    `<option>matches</option></select></td>`+
    `<td><input class=rv list="dl-${id}" placeholder="pick or type…" oninput="rulesDirty()">`+
    `<datalist id="dl-${id}"></datalist></td>`+
    `<td><input class=rt placeholder="tag value e.g. data-eng" oninput="rulesDirty()"></td>`+
    `<td><button class=ghost style="margin:0;padding:4px 9px" onclick="this.closest('tr').remove(); rulesDirty()">✕</button></td>`;
  tb.appendChild(tr);
  fillFieldValues(id, "owner");   // preload owner values
  rulesDirty();
}
// Any rule edit invalidates a prior preview — hide Apply until re-previewed, so
// you can never apply a rule set you haven't just seen the workloads for.
function rulesDirty(){
  const ar = $("#rulesApplyRow"); if(ar) ar.style.display = "none";
  const pb = $("#rulePreviewBox"); if(pb) pb.innerHTML = "";
}
async function fillFieldValues(id, field){
  const dl = document.getElementById("dl-"+id); if(!dl) return;
  dl.innerHTML = "";
  const r = await (await fetch("/api/field-values?field="+field+"&days="+$("#days").value)).json();
  if(r.error || !r.values) return;
  for(const v of r.values){
    const o = document.createElement("option");
    o.value = v.value; o.label = money(v.cost)+" · "+v.workloads+" workloads";
    dl.appendChild(o);
  }
}
function collectRules(){
  const rules = [];
  for(const tr of $("#rulesTable").querySelectorAll("tr")){
    const f=tr.querySelector(".rf"), o=tr.querySelector(".ro"),
          v=tr.querySelector(".rv"), t=tr.querySelector(".rt");
    if(f && v && v.value && t && t.value)
      rules.push({field:f.value, op:o.value, value:v.value, tags:{[$("#tagKey").value]:t.value}});
  }
  return rules;
}
async function doRulePreview(){
  const rules = collectRules();
  if(!rules.length){ $("#rulePreviewBox").innerHTML='<div class="banner warn">Add at least one complete rule.</div>'; return; }
  $("#rulePreviewBox").innerHTML='<div class="note">Computing…</div>';
  const r = await (await fetch("/api/rule-preview",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({tag_key:$("#tagKey").value, days:+$("#days").value, rules})})).json();
  if(r.error){ $("#rulePreviewBox").innerHTML='<div class="banner warn">'+r.error+'</div>'; return; }
  const i=r.impact||{}, wl=r.workloads||[];
  let html =
    `<div class="banner info">These rules match <b>${(i.matched_count||0).toLocaleString()}</b> `+
    `of ${(i.total_untagged||0).toLocaleString()} untagged workloads `+
    `(<b>${money(i.matched_cost)}</b> of ${money(i.total_untagged_cost)}). `+
    `<b>Review the exact list below</b> — make sure a rule isn't lumping unrelated `+
    `workloads under one team.</div>`;
  if(wl.length){
    html += "<div style='max-height:340px;overflow:auto'><table>"+
            "<tr><th>Workload</th><th>Product</th><th>Owner</th>"+
            "<th class=num>Cost</th><th>Will be tagged</th></tr>";
    for(const w of wl)
      html += `<tr><td>${w.workload_name||'—'}</td><td>${w.product}</td>`+
              `<td class=muted>${w.owner||'—'}</td><td class=num>${money(w.cost)}</td>`+
              `<td><span class=pill>${$("#tagKey").value} = ${w.new_tag_value}</span></td></tr>`;
    html += "</table></div>";
    if(wl.length < (i.matched_count||0))
      html += `<div class=note>Showing top ${wl.length} by cost of ${i.matched_count}.</div>`;
  }
  $("#rulePreviewBox").innerHTML = html;
  $("#rulesApplyRow").style.display="block";
}
async function doRulesApply(){
  const rules = collectRules();
  if(!rules.length){ return; }
  const dry = $("#rulesDryRun").checked;
  if(!confirmLive(dry, "every workload these rules match")) return;
  $("#result").innerHTML='<div class="note">Enqueuing rules + starting writer…</div>';
  const body = { tag_key:$("#tagKey").value, days:+$("#days").value, rules,
                 use_ai:$("#rulesUseAi").checked, dry_run:dry };
  const r = await (await fetch("/api/auto-tag",{method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)})).json();
  if(r.error){ $("#result").innerHTML='<div class="banner warn">'+r.error+'</div>'; return; }
  if(!r.run){ $("#result").innerHTML='<div class="banner warn">'+(r.message||"Nothing matched")+'</div>'; return; }
  const md = r.run.dry_run?"<b>dry-run</b>":"<b>LIVE</b>";
  $("#result").innerHTML=`<div class="banner info">Batch <span class=pill>${r.batch_id}</span> — `+
    `${r.rule_rows} by rules${r.ai_rows?`, ${r.ai_rows} by AI`:" (no AI)"}. `+
    `Writer started in ${md}: <a href="${r.run.url}" target=_blank>view run →</a></div>`;
  loadBatches();
}

// ---- manual mode ---------------------------------------------------------
async function doManual(){
  const body = { product:$("#mProduct").value.trim().toUpperCase(),
    workload_id:$("#mId").value.trim(), workload_name:$("#mName").value.trim(),
    tag_key:$("#tagKey").value, tag_value:$("#mValue").value.trim(),
    dry_run:$("#mDryRun").checked };
  if(!body.product||!body.workload_id||!body.tag_value){
    $("#result").innerHTML='<div class="banner warn">Product, workload id, and tag value are required.</div>'; return; }
  if(!confirmLive(body.dry_run, "workload "+body.workload_id)) return;
  $("#result").innerHTML='<div class="note">Enqueuing + starting writer…</div>';
  const r = await (await fetch("/api/manual-tag",{method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)})).json();
  if(r.error){ $("#result").innerHTML='<div class="banner warn">'+r.error+'</div>'; return; }
  if(r.status && r.status!=="ENQUEUED"){ $("#result").innerHTML='<div class="banner warn">'+(r.message||r.status)+'</div>'; return; }
  const md = r.run.dry_run?"<b>dry-run</b>":"<b>LIVE</b>";
  $("#result").innerHTML=`<div class="banner info">Batch <span class=pill>${r.batch_id}</span> — `+
    `1 workload queued. Writer started in ${md}: <a href="${r.run.url}" target=_blank>view run →</a></div>`;
  loadBatches();
}

async function loadBatches(){
  const r = await (await fetch("/api/batches?limit=15")).json();
  if(r.error){ $("#batches").innerHTML=""; return; }
  let t = "<tr><th>Batch</th><th class=num>Cost</th><th>Outcome</th><th></th></tr>";
  for(const b of (r.batches||[])){
    // Honest one-line outcome: tagged vs failed vs pending vs can't-tag.
    const parts = [];
    if(b.succeeded) parts.push(`<span class=ok>${b.succeeded} tagged</span>`);
    if(b.failed)    parts.push(`<span style="color:#ff6b6b">${b.failed} failed</span>`);
    if(b.unsupported) parts.push(`<span class=muted>${b.unsupported} can't tag</span>`);
    if(b.dry_run)   parts.push(`<span class=muted>${b.dry_run} dry-run</span>`);
    if(b.pending)   parts.push(`<span style="color:#f2c14e">${b.pending} not run yet</span>`);
    const outcome = parts.join(" · ") || "—";
    const showWhy = (b.failed || b.unsupported);
    t += `<tr><td><span class=pill>${b.batch_id}</span></td>`+
         `<td class=num>${money(b.cost)}</td>`+
         `<td>${outcome}</td>`+
         `<td style="white-space:nowrap">`+
         (showWhy ? `<button class=ghost style="margin:0;padding:5px 10px" `+
           `onclick="toggleWhy('${b.batch_id}')">Why?</button> ` : "")+
         `<button class=ghost style="margin:0;padding:5px 10px" `+
         `onclick="doRollback('${b.batch_id}')">Rollback</button></td></tr>`;
    t += `<tr id="why-${b.batch_id}" style="display:none"><td colspan=4></td></tr>`;
  }
  $("#batches").innerHTML = t;
}

async function toggleWhy(id){
  const row = document.getElementById("why-"+id);
  const cell = row.querySelector("td");
  if(row.style.display !== "none"){ row.style.display = "none"; return; }
  row.style.display = "table-row";
  cell.innerHTML = '<div class="note">Loading reasons…</div>';
  const r = await (await fetch("/api/batch-detail?batch_id="+encodeURIComponent(id))).json();
  if(r.error){ cell.innerHTML='<div class="banner warn">'+r.error+'</div>'; return; }
  const b = r.breakdown||[];
  if(!b.length){ cell.innerHTML='<div class="note">No failures recorded.</div>'; return; }
  let h = '<table style="margin:6px 0"><tr><th>Product</th><th>Status</th>'+
          '<th class=num>Rows</th><th class=num>Cost</th><th>Reason</th></tr>';
  for(const x of b)
    h += `<tr><td>${x.product}</td><td>${x.status}</td><td class=num>${x.rows}</td>`+
         `<td class=num>${money(x.cost)}</td><td class=muted style="max-width:520px">`+
         `${(x.sample_reason||"").replace(/</g,"&lt;")}</td></tr>`;
  h += "</table>";
  cell.innerHTML = h;
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
const REASON_INFO = {
  POLICY_GOVERNED: {
    label: "Attributed via budget policy (serverless)",
    color: "#f2c14e",
    action: "These serverless workloads (Databricks Apps, serverless SQL, model "+
            "serving, serverless jobs, pipelines, Lakebase) are tagged by "+
            "assigning a <b>budget policy</b> — not a per-resource tag. In the UI: "+
            "select a serverless usage policy when creating/editing the resource. "+
            "Programmatic: the <code>databricks_budget_policy</code> Terraform "+
            "resource (custom_tags up to 20) creates the policy; assign it to users/"+
            "resources. Tags then flow to system.billing.usage.custom_tags. "+
            "<i>This writer doesn't manage budget policies — do it via Terraform or UI.</i>"
  },
  UI_ONLY: {
    label: "Set in the pipeline definition",
    color: "#7fb6ff",
    action: "DLT/SDP tags live on the pipeline's cluster specs "+
            "(<code>clusters[].custom_tags</code>). Set them in the pipeline settings "+
            "UI or the pipeline definition (API/Terraform pipeline resource) — not "+
            "via a standalone per-resource tag call."
  },
  NO_API: {
    label: "No documented tag path",
    color: "#ff8585",
    action: "No per-resource tag API or budget-policy coverage is documented for "+
            "these today. Check the resource's own settings UI, or confirm against "+
            "current Databricks docs before assuming."
  },
};

async function loadNotTaggable(){
  const r = await (await fetch("/api/not-taggable"+q())).json();
  if(r.error){ $("#notTaggable").innerHTML='<div class="banner warn">'+r.error+'</div>'; return; }
  const rows = r.rows||[];
  if(!rows.length){ $("#notTaggable").innerHTML='<div class="note">Nothing — all untagged spend is API-taggable.</div>'; return; }
  // Group by reason
  const byReason = {};
  for(const x of rows){ (byReason[x.reason] ||= []).push(x); }
  let html = "";
  for(const reason of ["POLICY_GOVERNED","UI_ONLY","NO_API"]){
    const grp = byReason[reason]; if(!grp) continue;
    const info = REASON_INFO[reason];
    const totCost = grp.reduce((s,x)=>s+(x.cost||0),0);
    const totWl = grp.reduce((s,x)=>s+(x.workloads||0),0);
    html += `<div class="card" style="border-left:4px solid ${info.color};margin-bottom:12px">`+
            `<b>${info.label}</b> — ${totWl.toLocaleString()} workloads · ${money(totCost)}<br>`+
            `<span class=note>${info.action}</span>`+
            `<table style="margin-top:8px"><tr><th>Product</th><th class=num>Workloads</th><th class=num>Cost</th></tr>`;
    for(const x of grp)
      html += `<tr><td>${x.product}</td><td class=num>${x.workloads}</td><td class=num>${money(x.cost)}</td></tr>`;
    html += "</table></div>";
  }
  $("#notTaggable").innerHTML = html;
}

$("#days").addEventListener("change", loadNotTaggable);
$("#tagKey").addEventListener("change", loadNotTaggable);
setMode('ai'); addRule(); loadOverview(); loadBatches(); loadNotTaggable();
</script>
</body></html>"""
