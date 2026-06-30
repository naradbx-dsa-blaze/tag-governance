# Tag Governance — Untagged Consumption Scanner

A Databricks App that scans consumption across **every billing product and every
workspace in an account**, finds the workloads that aren't tagged for chargeback,
ranks them by cost, and lets you apply your own tag keys in place — with a safe
**dry-run preview → approve** flow before anything is mutated.

> Built for the chargeback problem: consumption is high, but workloads were never
> tagged, so cost can't be attributed back to teams. Account-wide and
> customer-portable — deploy it into any Databricks account in minutes.

---

## What it does

| Step | Detail |
|------|--------|
| **Scan** | Reads `system.billing.usage` (last *N* days), joins current list prices, flags every workload **missing your chargeback keys**. |
| **Rank** | Groups usage by resource id (`job_id`, `warehouse_id`, `endpoint_id`, `dlt_pipeline_id`, …) and sorts by untagged cost — fix the expensive gaps first. |
| **Per workspace** | `system.billing.usage` is **account-wide**, so the app sees every workspace with no extra login. Filter to one/all, and see which workspaces carry the most unattributable spend. |
| **Suggest owner** | Pulls `identity_metadata` so even untagged workloads come with a likely owner — a one-click starting value. |
| **Tag in place** | Pick your own keys, **① Preview** the exact per-resource API call, **② Approve & apply**. Approvals collect in a session queue with a running "spend now attributable" total. |
| **Verify** | Tag lands on the resource immediately; billing attribution follows on future usage (system tables lag ~hours). |

**Dry-run by default** — approving *records* the decision without mutating. Live
writes are a deliberate, separate switch (see below).

---

## Architecture

```
system.billing.usage  ──┐
(account-wide, all WS)   │   materialize.sql (daily job)
system.billing.list_prices ─► tag_governance_workload_daily  ──► Streamlit app
system.access.workspaces_latest                (pre-aggregated)      (sub-second reads)
```

- **`materialize.sql`** collapses millions of raw usage rows into one row per
  `usage_date × workspace_id × product × workload`, with cost pre-joined and the
  **set of tag keys** stored as an array. This is why the app loads in sub-second
  time instead of scanning raw billing on every open.
- **`tag-governance-refresh`** job rebuilds the table daily (06:00).
- The app computes "untagged" at query time via `NOT arrays_overlap(tag_keys, <your keys>)`,
  so the table is **key-agnostic** — users pick any keys at runtime.

### Files

| File | Purpose |
|------|---------|
| `app.py` | Streamlit UI — Scan / Tag & Verify / How-it-works tabs |
| `db.py` | Cached SQL connection + `run_query` |
| `queries.py` | SQL builders; reads `TAG_GOVERNANCE_SUMMARY_TABLE` |
| `materialize.sql` | Builds the summary table — `CREATE TABLE IDENTIFIER(:summary_table)` |
| `tagging.py` | Per-product tag-write API map + preview/approve (dry-run guarded) |
| `app.yaml` | Apps runtime config |
| `databricks.yml` | DAB bundle (app + refresh job) with `warehouse_id` / `summary_table` variables |
| `requirements.txt` | streamlit, databricks-sql-connector, databricks-sdk, pandas |

---

## Deploying into a NEW account (e.g. a customer)

The app travels to the customer's account — the data never leaves. It runs as a
service principal in **their** account and reads **their** account-wide billing,
so it automatically discovers all their workspaces. **They configure two values.**

### 1. Prerequisites
- Databricks CLI v0.230+ (`databricks -v`)
- A SQL warehouse in the target account
- Permission to deploy bundles + create an app and a job

### 2. Authenticate to the target account
```bash
databricks auth login --host https://<their-workspace>.cloud.databricks.com --profile customer
```

### 3. Set the two variables
Either edit the `variables:` block in `databricks.yml`, pass `--var` on the CLI,
or add a target (recommended). Uncomment and edit the example target in `databricks.yml`:

```yaml
targets:
  customer:
    workspace:
      host: https://<their-workspace>.cloud.databricks.com
    variables:
      warehouse_id: <their-warehouse-id>
      summary_table: <their_catalog>.<their_schema>.tag_governance_workload_daily
```

| Variable | What to set it to |
|----------|-------------------|
| `warehouse_id` | Any SQL warehouse in their account |
| `summary_table` | A UC table the app + job will own (their catalog/schema) |

### 4. Grant the app's service principal access
After the first `deploy`, the app's SP is created. Grant it:

```sql
-- read billing + workspace metadata (account-wide system tables)
GRANT SELECT ON SCHEMA system.billing  TO `<app-service-principal>`;
GRANT SELECT ON SCHEMA system.access   TO `<app-service-principal>`;

-- write the summary table
GRANT USE CATALOG ON CATALOG <their_catalog>            TO `<app-service-principal>`;
GRANT USE SCHEMA, CREATE TABLE ON SCHEMA <their_catalog>.<their_schema>
                                                        TO `<app-service-principal>`;
```
And `CAN_USE` on the warehouse (handled by the bundle's app resource).

> Reading system tables requires the metastore admin to have enabled the
> `system.billing` and `system.access` schemas. Most accounts have these on.

### 5. Deploy, build the table, run
```bash
databricks bundle deploy --target customer --profile customer
databricks bundle run   tag-governance-refresh --target customer --profile customer  # build the table now
databricks bundle run   tag-governance          --target customer --profile customer  # start the app
```

The refresh job will also run **daily at 06:00** to keep the table current. The
app shows a "data as of <date>" stamp so users know the freshness.

That's it — the workspace list, billing data, and friendly names all populate
automatically from their account.

---

## Running locally (dev)
```bash
export DATABRICKS_CONFIG_PROFILE=customer
export DATABRICKS_WAREHOUSE_ID=<their-warehouse-id>
export TAG_GOVERNANCE_SUMMARY_TABLE=<their_catalog>.<their_schema>.tag_governance_workload_daily
pip install -r requirements.txt
streamlit run app.py
```

---

## Dry-run → live tag writes

The app ships in **dry-run** (`TAG_GOVERNANCE_DRY_RUN=true`). Approving records
the decision and marks the workload Tagged **without mutating any resource**.

To enable real writes:
1. Implement the per-product SDK calls in `tagging.approve()` (the API per product
   is already mapped in `PRODUCT_PLANS`: jobs, pipelines, warehouses, serving &
   vector-search endpoints, clusters, Lakebase).
2. Set `TAG_GOVERNANCE_DRY_RUN=false`.

> **Cross-workspace note:** reading billing is account-wide, but *writing* a tag to
> a job in workspace X requires an identity with write permission **in workspace X**.
> For multi-workspace writes, use per-workspace service principals / OAuth — do not
> store PATs in the app.

---

## Notes & gotchas

- **"Untagged" = missing *your* chosen keys**, not "zero tags." Most tags already in
  billing are system-injected (`bp`, `EndpointId`, `ServingType`, `BudgetPolicyId`,
  `RemoveAfter`) — not governance tags.
- **Serverless** products (serverless SQL, Apps) attribute cost via **budget/tag
  policies**, not free-form `custom_tags`. The app flags these per workload.
- **Streamlit on Apps:** `${DATABRICKS_APP_PORT}` is not expanded inside an app.yaml
  command array — the command is wrapped in `sh -c` so the shell expands it.
- Pair this backlog cleanup with **tag policies** so new workloads can't go untagged —
  the app clears the backlog, policy stops the bleeding.
