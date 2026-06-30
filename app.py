"""Tag Governance — scan untagged Databricks workloads by cost, tag in place.

Solves the chargeback problem: consumption is high but workloads aren't tagged,
so cost can't be attributed to teams. This app scans system.billing.usage across
ALL products, ranks untagged workloads by spend, and lets you apply your own
chargeback tag keys in place (dry-run preview today), then verify.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

import db
import queries
import tagging

st.set_page_config(
    page_title="Tag Governance",
    page_icon="🏷️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ----------------------------------------------------------------------------- styling
st.markdown(
    """
    <style>
      .block-container {padding-top: 2rem; max-width: 1400px;}
      /* KPI cards — custom, colour-coded by meaning */
      .kpi {
        background: linear-gradient(180deg, #1b1f27 0%, #15181f 100%);
        border: 1px solid #2a2f3a; border-left-width: 5px; border-radius: 14px;
        padding: 16px 18px; box-shadow: 0 1px 3px rgba(0,0,0,.35); height: 100%;
      }
      .kpi .lbl {color:#9aa4b2; font-weight:600; font-size:.82rem; letter-spacing:.03em;
                 text-transform:uppercase; margin:0 0 6px 0;}
      .kpi .val {font-size:2.1rem; font-weight:800; line-height:1.1; margin:0;}
      .kpi .sub {color:#6b7280; font-size:.78rem; margin:4px 0 0 0;}
      .kpi-neutral {border-left-color:#5b8def;} .kpi-neutral .val {color:#e8eefc;}
      .kpi-bad     {border-left-color:#ff5b5b;} .kpi-bad .val     {color:#ff6b6b;}
      .kpi-warn    {border-left-color:#f2b53c;} .kpi-warn .val    {color:#f2c14e;}
      /* badges */
      .badge {display:inline-block; padding:2px 10px; border-radius:999px;
              font-size:.72rem; font-weight:700; letter-spacing:.03em;}
      .badge-untagged {background:#3a1d1d; color:#ff8585; border:1px solid #5b2b2b;}
      .badge-srvless  {background:#1d2c3a; color:#7fb6ff; border:1px solid #2b425b;}
      .hero-sub {color:#9aa4b2; font-size:.95rem; margin-top:-8px;}
      h1, h2, h3 {letter-spacing:-.01em;}
      .prod-pill {font-weight:700;}
    </style>
    """,
    unsafe_allow_html=True,
)

PRODUCT_LABELS = {
    "JOBS": "Jobs", "DLT": "SDP / DLT", "SQL": "SQL", "MODEL_SERVING": "Model Serving",
    "VECTOR_SEARCH": "Vector Search", "ALL_PURPOSE": "All-Purpose", "INTERACTIVE": "Interactive",
    "APPS": "Apps", "LAKEBASE": "Lakebase", "DATA_QUALITY_MONITORING": "Lakehouse Monitoring",
}


def fmt_money(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:,.0f}"


# ----------------------------------------------------------------------------- sidebar
st.sidebar.title("🏷️ Tag Governance")
st.sidebar.caption("Find untagged spend. Attribute it to teams.")

days = st.sidebar.select_slider(
    "Lookback window", options=[7, 14, 30, 60, 90], value=30,
    help="Days of system.billing.usage to scan.",
)

# Workspace picker — billing is account-wide, so this slices the same data by
# workspace_id (no per-workspace login needed). Empty selection = all workspaces.
st.sidebar.subheader("Workspaces")
ws_label_to_id = {}
try:
    ws_df = db.run_query(queries.workspace_options(days))
    for _, r in ws_df.iterrows():
        ws_label_to_id[f"{r['workspace_name']}  ({fmt_money(r['cost'])})"] = str(r["workspace_id"])
except Exception:
    ws_df = None
ws_picked_labels = st.sidebar.multiselect(
    "Filter to workspaces", options=list(ws_label_to_id.keys()),
    default=[], help="Leave empty to scan ALL workspaces in the account.",
)
workspaces = [ws_label_to_id[l] for l in ws_picked_labels] or None
# id -> friendly name, for labelling tables elsewhere
WS_NAMES = {}
if ws_df is not None:
    for _, r in ws_df.iterrows():
        WS_NAMES[str(r["workspace_id"])] = r["workspace_name"]
st.sidebar.caption(
    f"Scanning **all {len(ws_label_to_id)} workspaces**" if not workspaces
    else f"Scanning **{len(workspaces)}** of {len(ws_label_to_id)} workspaces"
)

# Seed the key picker from tags actually in use, but let users add their own.
try:
    existing_keys_df = db.run_query(queries.distinct_tag_keys(days, workspaces))
    existing_keys = existing_keys_df["tag_key"].tolist()
except Exception:
    existing_keys = []

DEFAULT_SUGGESTED = ["cost_center", "team", "business_unit", "project", "environment"]
suggested = DEFAULT_SUGGESTED + [k for k in existing_keys if k not in DEFAULT_SUGGESTED]

st.sidebar.subheader("Your chargeback keys")
st.sidebar.caption("A workload counts as **untagged** if it's missing *these* keys. Pick or type your own.")
tag_keys = st.sidebar.multiselect(
    "Required tag keys", options=suggested, default=["cost_center", "team"],
    help="These are the keys Bayada wants on every workload for chargeback.",
)
if tagging.DRY_RUN:
    st.sidebar.info("🔒 **Dry-run mode** — applying tags previews the change; nothing is modified.")

# ----------------------------------------------------------------------------- header
st.title("Untagged Consumption Scanner")

# Freshness of the pre-aggregated summary table (data is materialized daily, so
# the app loads instantly rather than scanning raw billing on every open).
as_of_txt = ""
try:
    fr = db.run_query(queries.freshness())
    if not fr.empty and fr.iloc[0]["as_of"] is not None:
        as_of_txt = f" · data as of <b>{fr.iloc[0]['as_of']}</b>"
except Exception:
    pass

st.markdown(
    f"<p class='hero-sub'>Last <b>{days} days</b> across every billing product · "
    f"untagged = missing {', '.join(f'<code>{k}</code>' for k in tag_keys) or '<i>any tag</i>'}"
    f"{as_of_txt}</p>",
    unsafe_allow_html=True,
)

# ----------------------------------------------------------------------------- KPIs
kpi = db.run_query(queries.kpi_summary(days, tag_keys, workspaces))
row = kpi.iloc[0] if not kpi.empty else pd.Series(dtype="float64")

def kpi_card(col, label, value, tone, sub=""):
    col.markdown(
        f"<div class='kpi kpi-{tone}'><p class='lbl'>{label}</p>"
        f"<p class='val'>{value}</p>"
        + (f"<p class='sub'>{sub}</p>" if sub else "")
        + "</div>",
        unsafe_allow_html=True,
    )

pct = row.get("pct_untagged")
nwk = row.get("untagged_workloads")
c1, c2, c3, c4 = st.columns(4)
kpi_card(c1, "💰 Total spend (scanned)", fmt_money(row.get("total_cost")),
         "neutral", "list price · all products")
kpi_card(c2, "🚩 Untagged spend", fmt_money(row.get("untagged_cost")),
         "bad", "can't be charged back")
kpi_card(c3, "🚩 % untagged", f"{pct:.0f}%" if pd.notna(pct) else "—",
         "bad", "of total spend")
kpi_card(c4, "📦 Untagged workloads", f"{int(nwk):,}" if pd.notna(nwk) else "—",
         "warn", "need a tag")

st.divider()

tab_scan, tab_tag, tab_about = st.tabs(["📊  Scan", "🏷️  Tag & Verify", "ℹ️  How it works"])

# ============================================================================= SCAN
with tab_scan:
    left, right = st.columns([1, 1])

    prod = db.run_query(queries.by_product(days, tag_keys, workspaces))
    if not prod.empty:
        prod["Product"] = prod["product"].map(lambda p: PRODUCT_LABELS.get(p, p))
        with left:
            st.subheader("Untagged spend by product")
            chart_df = prod.set_index("Product")[["untagged_cost", "tagged_cost"]]
            chart_df.columns = ["Untagged", "Tagged"]
            # red = untagged (the problem), green = tagged (attributed)
            st.bar_chart(chart_df, color=["#ff6b6b", "#2ecc71"], height=360)
        with right:
            st.subheader("Where the gaps are")
            show = prod[["Product", "total_cost", "untagged_cost", "pct_untagged"]].copy()
            show.columns = ["Product", "Total $", "Untagged $", "% Untagged"]
            st.dataframe(
                show, hide_index=True, height=360, use_container_width=True,
                column_config={
                    "Total $": st.column_config.NumberColumn(format="$%d"),
                    "Untagged $": st.column_config.NumberColumn(format="$%d"),
                    "% Untagged": st.column_config.ProgressColumn(
                        format="%.0f%%", min_value=0, max_value=100),
                },
            )

    # ---- by workspace ------------------------------------------------------
    wsdf = db.run_query(queries.by_workspace(days, tag_keys, workspaces))
    if not wsdf.empty and len(wsdf) > 1:
        st.subheader("Untagged spend by workspace")
        st.caption("Which workspaces carry the most unattributable spend.")
        wl, wr = st.columns([1, 1])
        with wl:
            ws_chart = wsdf.set_index("workspace_name")[["untagged_cost"]]
            ws_chart.columns = ["Untagged $"]
            st.bar_chart(ws_chart, color="#ff6b6b", height=320, horizontal=True)
        with wr:
            wshow = wsdf[["workspace_name", "total_cost", "untagged_cost",
                          "pct_untagged", "untagged_workloads"]].copy()
            wshow.columns = ["Workspace", "Total $", "Untagged $", "% Untagged", "# Untagged"]
            st.dataframe(
                wshow, hide_index=True, height=320, use_container_width=True,
                column_config={
                    "Total $": st.column_config.NumberColumn(format="$%d"),
                    "Untagged $": st.column_config.NumberColumn(format="$%d"),
                    "% Untagged": st.column_config.ProgressColumn(
                        format="%.0f%%", min_value=0, max_value=100),
                },
            )

    st.subheader("Untagged workloads — ranked by cost")
    st.caption("The money you can't charge back yet. Tackle the top of this list first.")
    lb = db.run_query(queries.leaderboard(days, tag_keys, workspaces))
    if lb.empty:
        st.success("🎉 No untagged workloads found for these keys in this window.")
    else:
        disp = lb.copy()
        disp["Product"] = disp["product"].map(lambda p: PRODUCT_LABELS.get(p, p))
        disp["Workspace"] = disp["workspace_id"].astype(str).map(lambda w: WS_NAMES.get(w, w))
        disp["Workload"] = disp["workload_name"].fillna(disp["workload_id"])
        disp["Owner"] = disp["owner"].fillna("— unknown —")
        disp["Serverless"] = disp["is_serverless"].map({1: "✓", 0: ""})
        disp = disp[["Product", "Workspace", "Workload", "Owner", "Serverless", "untagged_cost", "workload_id"]]
        disp.columns = ["Product", "Workspace", "Workload", "Suggested owner", "Srvless", "Untagged $", "workload_id"]
        st.dataframe(
            disp, hide_index=True, use_container_width=True, height=460,
            column_config={
                "Untagged $": st.column_config.NumberColumn(format="$%d"),
                "workload_id": None,  # hidden, used by Tag tab
            },
        )
        st.session_state["leaderboard"] = lb

# ============================================================================= TAG
with tab_tag:
    lb = st.session_state.get("leaderboard")
    if lb is None or lb.empty:
        st.info("Run a scan first — then pick a workload here to tag it.")
    else:
        lb = lb.copy()
        lb["label"] = (
            lb["product"].map(lambda p: PRODUCT_LABELS.get(p, p)) + " · "
            + lb["workload_name"].fillna(lb["workload_id"]).astype(str)
            + "  (" + lb["untagged_cost"].map(fmt_money) + ")"
        )
        pick = st.selectbox("Pick an untagged workload", lb["label"].tolist())
        sel = lb[lb["label"] == pick].iloc[0]

        meta_l, meta_r = st.columns([2, 1])
        with meta_l:
            st.markdown(f"### {sel['workload_name'] or sel['workload_id']}")
            st.markdown(
                f"<span class='badge badge-untagged'>UNTAGGED</span>&nbsp;"
                + (f"<span class='badge badge-srvless'>SERVERLESS</span>"
                   if sel["is_serverless"] == 1 else ""),
                unsafe_allow_html=True,
            )
            st.caption(f"`{sel['workload_id']}` · {PRODUCT_LABELS.get(sel['product'], sel['product'])}")
        with meta_r:
            st.metric("Untagged cost", fmt_money(sel["untagged_cost"]))

        # approvals recorded this session, keyed by workload_id
        approvals = st.session_state.setdefault("approvals", {})
        wid = sel["workload_id"]
        already = approvals.get(wid)

        st.markdown("#### Assign chargeback tags")
        if already:
            st.success(f"✅ **Approved** — tags { already['tags'] } "
                       f"{'(dry-run, recorded)' if tagging.DRY_RUN else 'applied'}.")

        if not tag_keys:
            st.warning("Choose your chargeback keys in the sidebar first.")
        else:
            with st.form("tagform"):
                suggested_owner = sel["owner"] if pd.notna(sel["owner"]) else ""
                values = {}
                cols = st.columns(min(len(tag_keys), 3))
                for i, k in enumerate(tag_keys):
                    default = suggested_owner if k in ("team", "owner") else ""
                    values[k] = cols[i % len(cols)].text_input(
                        k, value=default, placeholder=f"value for {k}")
                submitted = st.form_submit_button("1 · Preview tag change", type="primary")

            if submitted:
                tags = {k: v for k, v in values.items() if v.strip()}
                if not tags:
                    st.error("Enter at least one tag value.")
                else:
                    # stash the previewed plan so the Approve button (outside the
                    # form) can act on it without re-running the form
                    st.session_state["pending_plan"] = {
                        "product": sel["product"], "workload_id": wid,
                        "workload_name": sel["workload_name"] or wid,
                        "tags": tags, "is_serverless": int(sel["is_serverless"] == 1),
                    }

            pend = st.session_state.get("pending_plan")
            if pend and pend["workload_id"] == wid:
                plan = tagging.plan_for(
                    product=pend["product"], workload_id=pend["workload_id"],
                    workload_name=pend["workload_name"], tags=pend["tags"],
                    is_serverless=bool(pend["is_serverless"]),
                )
                pv = tagging.preview(plan)
                st.markdown("---")
                st.markdown(f"**Preview · {plan.resource_type}**")
                cc1, cc2 = st.columns(2)
                with cc1:
                    st.markdown("**Tags to apply**")
                    st.json(pend["tags"])
                with cc2:
                    st.markdown("**API call that will run**")
                    st.code(plan.api_hint, language="python")
                for w in plan.warnings:
                    st.warning(w)

                # ---- the approval step -------------------------------------
                if tagging.DRY_RUN:
                    st.info("🔒 Dry-run: approving **records** the change and marks the "
                            "workload Tagged — it does **not** modify the resource.")
                approve_label = ("2 · Approve & apply" if not tagging.DRY_RUN
                                 else "2 · Approve (dry-run)")
                if not plan.supported:
                    st.error(plan.warnings[0] if plan.warnings else "Unsupported product.")
                elif st.button(approve_label, type="primary", key=f"approve_{wid}"):
                    res = tagging.approve(plan)
                    approvals[wid] = {
                        "tags": pend["tags"], "product": pend["product"],
                        "workload_name": pend["workload_name"],
                        "status": res["status"], "cost": float(sel["untagged_cost"]),
                    }
                    st.session_state.pop("pending_plan", None)
                    st.rerun()

                # status lifecycle
                st.markdown("**Status**")
                done = "🟢" if already else "⚪"
                st.markdown(
                    f"🔴 Untagged &nbsp;→&nbsp; 🟡 Previewed &nbsp;→&nbsp; {done} "
                    f"{'**Tagged**' if already else 'Tagged'} &nbsp;→&nbsp; ⚪ Reflected in billing"
                )
                st.caption(
                    "Once live, the tag is set on the resource immediately, but it only "
                    "appears in system.billing.usage on **future** usage (billing tables lag ~hours)."
                )

        # ---- approval queue ---------------------------------------------------
        if approvals:
            st.markdown("---")
            st.markdown(f"#### Approved this session ({len(approvals)})")
            q = pd.DataFrame([
                {"Workload": v["workload_name"],
                 "Product": PRODUCT_LABELS.get(v["product"], v["product"]),
                 "Tags": ", ".join(f"{k}={val}" for k, val in v["tags"].items()),
                 "Cost attributed": fmt_money(v["cost"]),
                 "Status": "Tagged (dry-run)" if "DRY_RUN" in v["status"] else "Tagged"}
                for v in approvals.values()
            ])
            st.dataframe(q, hide_index=True, use_container_width=True)
            attributed = sum(v["cost"] for v in approvals.values())
            st.metric("Spend now attributable", fmt_money(attributed))

# ============================================================================= ABOUT
with tab_about:
    st.markdown(
        """
### How this works

1. **Scan** — reads `system.billing.usage` (last *N* days), joins current list prices,
   and flags every workload **missing your chargeback keys**. Cost is computed from DBUs ×
   current SKU rate.
2. **Rank** — workloads are grouped by their resource id (`job_id`, `warehouse_id`,
   `endpoint_id`, `dlt_pipeline_id`, …) and sorted by untagged cost, so you fix the
   expensive gaps first.
   - **Multi-workspace**: `system.billing.usage` is account-wide, so the app sees every
     workspace with no extra login. Use the sidebar **Workspaces** filter to scope to one,
     several, or all — and the *Untagged spend by workspace* view shows which workspaces
     carry the most unattributable spend.
3. **Suggest owner** — even with no tags, `identity_metadata` often tells us who owns/runs
   the workload — a one-click starting point for the `team`/`owner` value.
4. **Tag in place (two steps)** — pick your own keys and enter values, then **① Preview**
   the exact per-resource API call, then **② Approve & apply**. Approvals are collected in
   a session queue so you can see everything you've signed off. **Dry-run today** (approve
   records the decision without mutating); flipping `TAG_GOVERNANCE_DRY_RUN=false` makes the
   same Approve button perform the real per-product write.
5. **Verify** — the tag lands on the resource immediately; billing attribution follows on
   future usage.

**Why per-product matters:** Jobs, SQL warehouses, pipelines, serving & vector-search
endpoints each have their own `custom_tags` API. Serverless products (serverless SQL, Apps)
attribute cost via **budget/tag policies**, not free-form tags — the app flags these.

**Going forward:** pair this cleanup with **tag policies** so new workloads can't go
untagged — the app clears the backlog, policy stops the bleeding.
        """
    )
