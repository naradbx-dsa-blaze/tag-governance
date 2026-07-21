# Tag Governance 🏷️

A Databricks App that finds your **untagged spend** and lets you tag it — so every
dollar of usage can be charged back to the right team.

---

## Why tagging matters

Databricks usage shows up in your billing system tagged with whatever `custom_tags`
each resource carries. If a job, cluster, or warehouse has **no `cost_center` tag**,
its cost lands in an "unattributed" bucket — you know you spent the money, but not
*which team* to bill for it.

Without a tagging strategy you get:

- **No chargeback** — finance can't split the bill by team, product, or environment.
- **No accountability** — the teams driving cost don't see their own number.
- **Blind spots** — runaway spend hides in the untagged pile.

This app closes that gap: it ranks untagged spend by dollars, suggests or lets you
choose the right tag, and applies it safely and reversibly.

---

## What the app does ✅

- **Shows your untagged spend** — total $, % untagged, and a per-product breakdown,
  over a 7/14/30/60/90-day window.
- **Three ways to tag:**
  - 🤖 **AI suggestions** — an AI proposes a `cost_center` per workload; you review
    and apply the confident ones in one click.
  - 📋 **Rules** — e.g. *"owner contains `data-eng` → `cost_center = data-engineering`"* —
    preview exactly which workloads match, then apply.
  - ✏️ **Manual** — tag one workload directly.
- **Filter & sort** what you're about to tag by workload name, minimum cost, and
  recency (AI + rule modes).
- **Applies tags safely** — every tag goes through a queue → writer job → audit log,
  so writes are batched, idempotent, and **fully reversible** (one-click rollback).
- **Live progress** — watch the tag count and attributed $ climb as the job runs.
- **Honest failures** — if a tag can't be applied, it tells you *why* (no permission,
  resource deleted, or the product isn't API-taggable) instead of silently failing.
- **Tags these resource types directly:** Jobs, all-purpose clusters, SQL warehouses
  (non-serverless), instance pools, model serving, Lakebase, Vector Search.

## What the app does **not** do ❌

- **Serverless / policy-governed spend** (Databricks Apps, serverless SQL, AI Gateway,
  serverless jobs, Lakeflow pipelines) — these aren't tagged per-resource; they're
  attributed via a **budget policy**. The app shows this spend and explains it, but
  you set the policy in the UI or with Terraform, not here.
- **DLT / Lakeflow pipelines** — tags live in the pipeline definition; the app flags
  them but doesn't edit pipeline code.
- **Change anything but tags** — it only touches `custom_tags`. Your schedules, code,
  clusters, and configs are never modified.
- **Tag across workspaces by itself** — by default it tags the workspace it runs in.
  Cross-workspace tagging needs an account service principal (see the deploy notes).

---

## Deploy it in your environment 🚀

You'll need the [Databricks CLI](https://docs.databricks.com/dev-tools/cli/) and
admin rights in the target workspace.

**1. Point the app at your workspace.** In `databricks.yml`, set the two values under
`variables` (or pass them with `--var` at deploy time):

- `warehouse_id` — any SQL warehouse in your workspace (the app queries billing through it)
- `summary_table` — a catalog.schema.table you own, e.g. `main.tag_governance.workload_daily`
  (the other table names default alongside it)

**2. Deploy.**

```bash
databricks bundle deploy -p <your-profile>
databricks bundle run tag-governance-app -p <your-profile>
```

**3. Grant the app's service principal access** (one time — the app runs as its own
identity, not as you):

```bash
./grant_app_sp.sh <your-profile> tag-governance <catalog.schema> <warehouse_id>
```

This gives the app read/write on your `tag_governance` schema and `CAN_USE` on the
warehouse. It's idempotent — safe to re-run. *(A post-deploy hook also runs this
automatically, but run it by hand if the dashboard comes up blank.)*

**4. Build the data + suggestions.** Run these jobs (deployed with the app):

- `tag-governance-refresh` — builds the untagged-spend summary (daily).
- `tag-governance-annotate` — generates AI tag suggestions (weekly, optional).
- `tag-governance-scan` — inventories live resources incl. instance pools (optional).

Open the app URL from the deploy output and you're live.

### Notes

- **Who the writer runs as decides what it can tag.** A regular user can only tag
  resources they own; a **workspace admin** (or admin service principal) can tag
  everything. Policy-governed products are never API-taggable regardless.
- **Multiple workspaces?** Set `account_sp_scope` to a secret scope holding an account
  service principal's OAuth creds to tag across all workspaces. Otherwise it tags the
  home workspace only.
- **Restrict live writes** to a group by setting the `admin_group` variable (default
  `OPEN` = anyone can write, fine for a demo).
