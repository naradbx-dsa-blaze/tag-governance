# Tag Governance — Untagged Consumption Scanner

A Databricks App that scans consumption across **every billing product and every
workspace in an account**, finds the workloads that aren't tagged for chargeback,
ranks them by cost, and lets you apply your own tag keys in place — with a safe
**dry-run preview → approve** flow before anything is mutated.

> Built for the chargeback problem: consumption is high, but workloads were never
> tagged, so cost can't be attributed back to teams. Account-wide and
> customer-portable — deploy it into any Databricks account in minutes.

**Live demo (e2-demo-field-eng):** https://tag-governance-1444828305810485.aws.databricksapps.com

---

## What it does

| Step | Detail |
|------|--------|
| **Scan** | Reads `system.billing.usage` (last *N* days), joins current list prices, flags every workload **missing your chargeback keys**. |
| **Rank** | Groups usage by resource id (`job_id`, `warehouse_id`, `endpoint_id`, `dlt_pipeline_id`, …) and sorts by untagged cost — fix the expensive gaps first. |
| **Per workspace** | `system.billing.usage` is **account-wide**, so the app sees every workspace with no extra login. Filter to one/all, and see which workspaces carry the most unattributable spend. |
| **Suggest owner** | Pulls `identity_metadata` so even untagged workloads come with a likely owner — a one-click starting value. |
| **Tag in place** | Pick your own keys, **① Preview** the exact per-resource API call, **② Approve & apply**. Approvals collect in a session queue with a running "spend now attributable" total. |
| **Bulk tag (rules)** | For 1000s of workloads: define a few **rules** (`owner equals X → team=Y`, `name matches fraud-* → team=risk`). Each rule attributes every match at once; earlier rules win on conflict. Preview match count, cost coverage, per-rule breakdown, a sample, and conflicts before applying. |
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
                                     annotate.sql (ai_query) ──► tag_governance_suggestions
                                                                   (advisory hints)

WRITE PATH (app never mutates a resource):
  app  ──enqueue──►  tag_governance_write_queue  ──drain──►  writer_job.py
  (INSERT intent)         (PENDING rows)          (cost-desc,   ├─ GET→MERGE→PUT tag
                                                   per-WS,      └─ audit old→new
                                                   429 backoff)     │
  Batches & Rollback tab ◄── status + audit ◄── tag_governance_audit ◄┘
  rollback_job.py ──replay audit in reverse──► restore pre-batch values
```

- **`materialize.sql`** collapses millions of raw usage rows into one row per
  `usage_date × workspace_id × product × workload`, with cost pre-joined and the
  **set of tag keys** stored as an array. This is why the app loads in sub-second
  time instead of scanning raw billing on every open.
- **`tag-governance-refresh`** job rebuilds the table daily (06:00).
- The app computes "untagged" at query time via `NOT arrays_overlap(tag_keys, <your keys>)`,
  so the table is **key-agnostic** — users pick any keys at runtime.

### Scaling (tested)

- **Materialization** collapses millions of raw usage rows to one row per workload-day,
  so the dashboard reads a small table (sub-second over 30 days).
- **Bulk rules are evaluated in the warehouse**, not the app. Even at **50k+ untagged
  workloads** the app only pulls aggregates + a 50-row sample (1–2s), never the full set —
  so adding workloads doesn't bloat the client payload or slow the browser. (Verified on a
  50,662-workload fleet.)
- **Live bulk writes run as a Databricks job** (batched, concurrent, per-product), not
  inline in the request — so writing thousands of resources scales and survives partial
  failure with per-workload results.

### Files

| File | Purpose |
|------|---------|
| `app.py` | Streamlit UI — Scan / Tag & Verify / Bulk / Batches & Rollback / How-it-works |
| `db.py` | Cached SQL connection + `run_query` (read) + `run_exec` (enqueue write) |
| `queries.py` | SQL builders — reads/enqueue/status/suggestions; reads `TAG_GOVERNANCE_*_TABLE` |
| `materialize.sql` | Builds the summary table — `CREATE TABLE IDENTIFIER(:summary_table)` |
| `schema.sql` | Creates the write-queue + audit tables (`IF NOT EXISTS`) |
| `annotate.sql` | Advisory `ai_query` classifier → `tag_governance_suggestions` (hints only) |
| `tagging.py` | Per-product API map + `plan_for`/`preview` + `enqueue_single`/`enqueue_bulk` |
| `writer.py` | Per-product live tag writes (GET→MERGE→PUT, idempotent, best-effort) |
| `writer_job.py` | Drains the queue cost-descending, per-workspace + 429 backoff, writes audit |
| `ws_clients.py` | Resolves workspace_id → WorkspaceClient (M1 home-only / M2 account-SP); shared by both jobs |
| `setup_m2.sh` | Account-admin script: provision the account SP + secret scope, then smoke-test M2 reachability |
| `rollback_job.py` | Undoes a batch by replaying the audit in reverse |
| `app.yaml` | Apps runtime config |
| `databricks.yml` | DAB bundle (app + refresh/writer/rollback jobs) with `*_table` variables |
| `requirements.txt` | streamlit, databricks-sql-connector, databricks-sdk, pandas |

---

## Customer readiness checklist

An honest view of what's proven vs. what needs a one-time setup, so there are no
surprises in front of a customer.

| Capability | Status | Notes |
|------------|--------|-------|
| **Scans account-wide, all products** | ✅ Proven | `system.billing.usage` is account-wide; workspaces auto-discover. |
| **Scales to the full fleet** | ✅ Proven | Enqueue = one warehouse `INSERT…SELECT` (measured 6,170 rows, no client round-trip); rules evaluated in-warehouse over 50k+ workloads. |
| **Safe writes (queue → job → audit → rollback)** | ✅ Proven | App never mutates; writer job is idempotent, cost-descending, 429-backoff, resumable. Dry-run truly writes nothing. Verified on a live job. |
| **Rollback** | ✅ Proven | Restores every tag to its pre-batch value (or removes a key it added). Verified live. |
| **Portable to any account** | ✅ By design | No account-specific values in code — only bundle-var defaults. Change `warehouse_id` + table vars; touch zero code. |
| **Tag writes: Jobs / clusters / warehouses / serving** | ✅ Proven (M1) | Live-verified in the home workspace. ~59% of untagged cost. |
| **Lakebase / Vector Search writes** | ⚠️ SDK-gated | Work on a current `databricks-sdk`; older runtimes return `UNSUPPORTED` (feature-detected, never crash). Confirm the job runtime SDK version. |
| **Cross-workspace writes (M2)** | ⚠️ Code-verified + scripted setup + smoke test | M1 (home) fully proven. M2 is code-complete, thread-safe, and fails loud on misconfig; it needs a one-time account-SP setup (`setup_m2.sh`) which the admin validates with a no-write smoke test (`smoke_test=true`). Not yet run against a live foreign workspace by us. See "Cross-workspace writes (M2)". |
| **Serverless SQL / Apps (~41% of spend)** | ℹ️ Reported, not tagged | Not per-resource taggable — attribute via **budget/tag policy**. The tool flags these as `UNSUPPORTED` with a reason (honest, not silent). |
| **Advisory AI suggestions** | ✅ Proven | `ai_query` hint column + one-click "Bulk tag (AI)"; conservative (low-confidence `unknown` for cryptic workloads); never writes. Weekly job to limit inference cost; tunable/optional — see "Cost knob" below. |

**Ship-ready today at the M1 tier + honest reporting.** Full-account M2 is one
documented admin step (provision + entitle the account SP) away from live.

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

## Applying tags: queue → writer job → audit → rollback

The app **never mutates a resource.** Queuing (single or bulk) inserts intent rows
into `tag_governance_write_queue`. A separate **writer job** applies them; a
**rollback job** undoes a batch. This keeps writes safe, resumable, rate-limited,
and reversible — and it scales to the full fleet.

### One-time: create the queue + audit tables
```bash
databricks bundle deploy               # ships app + jobs + files
# create the write-queue and audit tables (idempotent)
databricks sql query --warehouse <id> --file schema.sql \
  --param queue_table=<cat.sch.tag_governance_write_queue> \
  --param audit_table=<cat.sch.tag_governance_audit>
```

### Apply a batch (dry-run first — always)
```bash
# 1. Dry-run: writes NOTHING, logs the intended old→new for inspection
databricks bundle run tag-governance-writer -- \
  --python-params dry_run=true batch_id=<BATCH_ID>

# 2. Live (M1 = home workspace only): performs the real per-product writes
databricks bundle run tag-governance-writer -- \
  --python-params dry_run=false batch_id=<BATCH_ID> workspace_id=<HOME_WS_ID>
```
The writer drains **cost-descending** (largest spend first), runs **per-workspace in
parallel** with exponential backoff on HTTP 429, is **idempotent** (a tag already at
the target value is a no-op), and records **old→new** for every change to the audit
table. Re-running after a crash simply drains the remaining `PENDING` rows.

### Roll a batch back
```bash
databricks bundle run tag-governance-rollback -- \
  --python-params dry_run=false batch_id=<BATCH_ID>
```
Restores every tag to its pre-batch value from the audit log (removes the key if it
didn't exist before), writing `ROLLBACK` audit rows.

### What is and isn't taggable (be honest with the customer)
Roughly **59% of untagged cost is directly taggable** (Jobs, clusters, non-serverless
warehouses, Model Serving, and — on a current SDK — Lakebase & Vector Search). The
other **~41%** (serverless SQL, Apps) attributes via **budget/tag policy**, not a
per-resource `custom_tags` write — the writer reports these as `UNSUPPORTED` with a
reason rather than pretending to tag them. Pair the backlog cleanup with **tag/budget
policies** for that portion.

> **SDK note:** `writer.py` feature-detects APIs (`hasattr`). On older SDKs the
> `database` (Lakebase) and Vector Search tag calls are absent and return
> `UNSUPPORTED`; on the current Apps/job runtime they work. Keep the job environment
> on a recent `databricks-sdk` to tag those products.

### Cross-workspace writes (M2)
Reading billing is account-wide, but *writing* a tag to a resource in workspace X
needs an identity valid **in workspace X**. The jobs support two modes automatically,
chosen by whether account-SP creds are available (`ws_clients.ClientResolver`):

- **M1 (default, no setup):** writes only the **home** workspace. Rows for any other
  workspace are marked `FAILED` (never mis-tagged with the home client). If you don't
  pass `workspace_id`, the writer defaults to the home workspace.
- **M2 (cross-workspace):** with an account-level service principal configured, the
  writer resolves a per-workspace `WorkspaceClient` for every workspace via
  `AccountClient.get_workspace_client(...)` and can drain **all** workspaces at once.

**Fastest path — run the setup script.** `setup_m2.sh` does all of the below as an
account admin (create SP → OAuth secret → assign to workspaces → store secret scope)
and ends with a **smoke test** that proves the SP can reach every workspace *without
tagging anything*. Edit the vars at the top, then:
```bash
./setup_m2.sh
```

**Validate before any real run — smoke test (writes nothing):**
```bash
databricks bundle run tag-governance-writer -- --python-params smoke_test=true
```
It prints `✓ REACHABLE workspace <id>` for each workspace that has queued rows, or
`✗ UNREACHABLE` (with the reason) if the SP isn't assigned there yet. This is the
one-command answer to "is M2 wired up correctly?" — run it until every workspace is
reachable, *then* do a real batch.

**Enable M2 manually (what the script automates):**
1. **Create an account-level service principal** (Account Console → User management →
   Service principals) and generate an **OAuth secret** (client ID + secret) for it.
2. **Assign it to each target workspace** (Account Console → Workspaces → *Permissions*),
   and grant it **`CAN_MANAGE`** on the resources it will tag (jobs, clusters, SQL
   warehouses, serving endpoints). Workspace-admin is the simplest blanket grant. Tag
   edits are *write* ops — read-only entitlements are not enough.
3. **Store the creds in a Databricks secret scope** (keys exactly `account_id`,
   `client_id`, `client_secret`):
   ```bash
   databricks secrets create-scope tag_governance_acct_sp
   databricks secrets put-secret tag_governance_acct_sp account_id     # your account UUID
   databricks secrets put-secret tag_governance_acct_sp client_id      # SP OAuth client id
   databricks secrets put-secret tag_governance_acct_sp client_secret  # SP OAuth secret
   ```
4. **Point the jobs at the scope** — set the bundle var (or pass per-run):
   ```bash
   databricks bundle deploy --var account_sp_scope=tag_governance_acct_sp
   # then drain ALL workspaces (omit workspace_id):
   databricks bundle run tag-governance-writer -- --python-params dry_run=false
   ```

The scope **name** is passed as a job parameter; the secrets themselves are read at
runtime via `dbutils.secrets` and never appear in job argv or logs. If the scope is
empty or unreadable, the jobs stay in M1. Do **not** store PATs in the app.

> **Seam:** all of this lives in `ws_clients.py` (shared by the writer and rollback
> jobs). The per-product write code in `writer.py` is unchanged between M1 and M2.

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

### Cost knob: AI-suggestion cadence

The advisory `ai_query` classifier (`annotate.sql`) is the **only recurring inference
cost** — it runs over the whole fleet (~50k workloads). It's deliberately a **separate
weekly job** (`tag-governance-annotate`, Mondays 07:13 ET), *not* part of the daily
summary refresh, because team→workload attribution barely changes day to day. The daily
`tag-governance-refresh` is cheap SQL only.

Tune it to the customer's appetite — it's a schedule, not a hardcode:

| Want | Change |
|------|--------|
| **Cheaper** | Edit `tag-governance-annotate` cron to monthly (`0 13 7 1 * ?`), or pause the job and run it on demand (`databricks bundle run tag-governance-annotate`). |
| **Fresher** | Bump the cron toward daily. |
| **Cheapest** | Skip it entirely — the app degrades gracefully: the 🤖 hint column and "Bulk tag (AI)" view just show nothing, everything else (scan, rules-based bulk, writes, rollback) works unchanged. |

Model choice is also a knob: `annotate_model` (bundle var) — a smaller/cheaper endpoint
lowers per-row cost.
