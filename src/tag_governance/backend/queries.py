"""SQL queries for the Tag Governance app.

These read from the PRE-AGGREGATED summary table
  users.narasimha_kamathardi.tag_governance_workload_daily
(one row per usage_date × workspace_id × product × workload, with cost pre-joined
and the set of tag keys present), built by materialize.sql and refreshed daily.

Because the heavy scan + price join is already done, these queries hit a small
table and finish in sub-second time. "Untagged" is computed at query time from
the user's chosen keys via array logic, so the table stays key-agnostic.

Args:
  days: validated int lookback window.
  tag_keys: list of user-chosen chargeback keys.
  workspaces: optional list of workspace_id strings to filter to (None/[] = all).
"""

import os


def _table_env(var: str) -> str:
    """Resolve a fully-qualified table name from env. The bundle always sets these
    per-target; if one is missing we DON'T silently fall back to a demo schema
    (which would read the wrong account's data) — we point at an obvious sentinel
    so the failure names the misconfigured variable instead of tagging blind."""
    val = os.environ.get(var, "").strip()
    return val or f"__UNSET_{var}__"


# All table locations are per-deploy env vars (set by the bundle), so the app is
# portable to any account with zero code change. See databricks.yml variables.
SUMMARY_TABLE = _table_env("TAG_GOVERNANCE_SUMMARY_TABLE")
QUEUE_TABLE = _table_env("TAG_GOVERNANCE_QUEUE_TABLE")
AUDIT_TABLE = _table_env("TAG_GOVERNANCE_AUDIT_TABLE")
SUGGESTIONS_TABLE = _table_env("TAG_GOVERNANCE_SUGGESTIONS_TABLE")
INVENTORY_TABLE = _table_env("TAG_GOVERNANCE_INVENTORY_TABLE")

# tag_keys aggregated up from the daily rows of one workload
_AGG_KEYS = "array_distinct(flatten(collect_list(tag_keys)))"

# Capability facts (which products are policy-governed vs API-taggable) come from
# the declarative registry in capability.py — the SINGLE source of truth shared
# with writer.py. Previously duplicated here and could drift out of sync.
import capability  # noqa: E402

# Always-policy-governed products (excludes SQL, which is only policy-governed when
# serverless — handled by the is_serverless flag below).
_POLICY_GOVERNED_PRODUCTS = capability.policy_governed_products()


def _taggable_predicate(product_col="product", serverless_col="is_serverless"):
    """SQL boolean: True only for workloads the writer can actually tag.

    Excludes always-policy-governed products and serverless SQL — the exact rule
    writer.attempt_write applies before dispatch.
    """
    plist = ", ".join(f"'{p}'" for p in _POLICY_GOVERNED_PRODUCTS)
    return (f"({product_col} NOT IN ({plist}) "
            f"AND NOT ({product_col} = 'SQL' AND {serverless_col} = 1))")


# Products with a working per-resource tag WRITER. Derived from the registry
# (direct_tag=True), minus: DLT whose "writer" only returns UNSUPPORTED (ui_only),
# and SQL whose taggability depends on the is_serverless flag (serverless_differs)
# and so is handled by _taggable_predicate, not this flat list.
_API_TAGGABLE_PRODUCTS = tuple(
    p for p in capability.api_taggable_products()
    if not (capability.get(p) and
            (capability.get(p).ui_only or capability.get(p).serverless_differs))
)


def tagged_live_adjustment(tag_key):
    """Live 'tagged since the snapshot' credit for the KPI.

    The summary table is a daily snapshot, so it still counts a workload as
    untagged right after we tag it (and system.billing.usage lags hours, so
    rebuilding the summary wouldn't help either). The QUEUE, however, records
    every SUCCEEDED tag for `tag_key` in real time with the workload's cost — so
    we subtract that from the snapshot's untagged total to make the number move
    within seconds of a writer run. DISTINCT workload so re-tags don't double-count.
    """
    return f"""
SELECT
  COUNT(DISTINCT workload_id)                         AS workloads,
  ROUND(SUM(list_cost), 0)                            AS cost
FROM (
  SELECT workload_id, MAX(list_cost) AS list_cost
  FROM {QUEUE_TABLE}
  WHERE status = 'SUCCEEDED' AND tag_key = {_sql_str(tag_key)}
  GROUP BY workload_id
)
"""


def not_taggable_breakdown(tag_key, days, workspaces=None):
    """Untagged spend the WRITER can't set via a per-resource custom_tags call —
    but that is still attributable another way. Grouped by product with the correct
    action. (Verified against Databricks docs: usage-detail-tags + budget-policies.)

    Three buckets:
      - POLICY_GOVERNED: serverless (Apps, serverless SQL, model serving, serverless
        jobs, Lakeflow pipelines, Lakebase, AI Gateway). NOT untaggable — attributed
        via a BUDGET POLICY (serverless usage policy). Set in the UI when creating/
        editing the resource, or programmatically with the databricks_budget_policy
        Terraform resource. Tags flow to system.billing.usage.custom_tags.
      - UI_ONLY (DLT/SDP): tags live on the pipeline's clusters[].custom_tags — set
        in the pipeline definition, not a standalone per-resource tag call.
      - NO_API (Agent Bricks, Supervisor Agent, AI Functions, …): no documented tag
        path today — verify in the resource UI / current docs.
    Everything the writer CAN tag per-resource via API is excluded (normal flow)."""
    untag_expr = _missing_keys_predicate([tag_key], _AGG_KEYS)
    plist = ", ".join(f"'{p}'" for p in _POLICY_GOVERNED_PRODUCTS)
    taggable = ", ".join(f"'{p}'" for p in _API_TAGGABLE_PRODUCTS)
    return f"""
WITH wl AS (
  SELECT product, workload_id,
         MAX(is_serverless) AS is_serverless,
         SUM(list_cost)     AS cost
  FROM {SUMMARY_TABLE}
  WHERE {_where(days, workspaces)}
  GROUP BY product, workload_id
  HAVING {untag_expr} AND SUM(list_cost) > 0
),
classified AS (
  SELECT product,
         CASE
           WHEN product IN ({plist})                  THEN 'POLICY_GOVERNED'
           WHEN product = 'SQL' AND is_serverless = 1  THEN 'POLICY_GOVERNED'
           WHEN product = 'DLT'                        THEN 'UI_ONLY'
           WHEN product = 'SQL' AND is_serverless = 0  THEN 'TAGGABLE'
           WHEN product IN ({taggable})                THEN 'TAGGABLE'
           ELSE 'NO_API'
         END AS reason,
         workload_id, cost
  FROM wl
)
SELECT product,
       reason,
       COUNT(*)            AS workloads,
       ROUND(SUM(cost), 0) AS cost
FROM classified
WHERE reason <> 'TAGGABLE'
GROUP BY product, reason
ORDER BY cost DESC
"""


def _not_already_handled(tag_key, product_col="product", workload_col="workload_id"):
    """SQL boolean: True unless this (workload, tag_key) is ALREADY tagged or in
    flight in the queue. The summary table's tag_keys is a daily snapshot, so
    right after a write it still reads 'untagged' — this queries the live queue so
    we never re-enqueue a workload already SUCCEEDED or still PENDING/RUNNING for
    the same key. (FAILED/UNSUPPORTED rows are NOT excluded, so a fixed permission
    can be retried.) This is the durable 'don't double-tag' guard.

    IMPORTANT: pass product_col/workload_col QUALIFIED with the outer table alias
    (e.g. "wl.product", "wl.workload_id"). The subquery aliases the queue as
    `handled`, so a BARE "product"/"workload_id" would bind to the INNER table
    (self-reference), making the predicate match on any non-empty queue and
    silently exclude every row. We assert qualification to fail loudly instead.
    """
    for col in (product_col, workload_col):
        assert "." in col, (
            f"_not_already_handled needs a qualified outer column, got {col!r} — "
            "an unqualified name binds to the inner `handled` table and breaks the guard.")
    return f"""NOT EXISTS (
    SELECT 1 FROM {QUEUE_TABLE} handled
    WHERE handled.workload_id = {workload_col}
      AND handled.product = {product_col}
      AND handled.tag_key = {_sql_str(tag_key)}
      AND handled.status IN ('SUCCEEDED', 'PENDING', 'RUNNING')
  )"""


def _missing_keys_predicate(tag_keys, col="tag_keys"):
    """True when the workload is missing ALL chosen keys.

    Keys are user-chosen (sidebar), so escape single quotes to avoid a broken
    literal / injection on a key name like O'Brien-cost.
    """
    if not tag_keys:
        return f"size({col}) = 0"
    arr = ", ".join("'" + str(k).replace("'", "''") + "'" for k in tag_keys)
    return f"NOT arrays_overlap({col}, array({arr}))"


def _where(days, workspaces):
    """Shared WHERE clause: date window + optional workspace filter."""
    clause = f"usage_date >= current_date() - INTERVAL {days} DAYS"
    if workspaces:
        ids = ", ".join(f"'{w}'" for w in workspaces)
        clause += f"\n    AND workspace_id IN ({ids})"
    return clause


def kpi_summary(days, tag_keys, workspaces=None):
    return f"""
WITH workload AS (
  SELECT product, workload_id,
         SUM(list_cost) AS cost,
         {_missing_keys_predicate(tag_keys, _AGG_KEYS)} AS is_untagged
  FROM {SUMMARY_TABLE}
  WHERE {_where(days, workspaces)}
  GROUP BY product, workload_id
)
SELECT
  ROUND(SUM(cost), 0)                                              AS total_cost,
  ROUND(SUM(CASE WHEN is_untagged THEN cost ELSE 0 END), 0)        AS untagged_cost,
  ROUND(100.0 * SUM(CASE WHEN is_untagged THEN cost ELSE 0 END) / NULLIF(SUM(cost), 0), 1) AS pct_untagged,
  COUNT(DISTINCT CASE WHEN is_untagged THEN workload_id END)       AS untagged_workloads
FROM workload
"""


def by_product(days, tag_keys, workspaces=None):
    return f"""
WITH workload AS (
  SELECT product, workload_id,
         SUM(list_cost) AS cost,
         {_missing_keys_predicate(tag_keys, _AGG_KEYS)} AS is_untagged
  FROM {SUMMARY_TABLE}
  WHERE {_where(days, workspaces)}
  GROUP BY product, workload_id
)
SELECT product,
       ROUND(SUM(cost), 0)                                       AS total_cost,
       ROUND(SUM(CASE WHEN is_untagged THEN cost ELSE 0 END), 0) AS untagged_cost,
       ROUND(SUM(CASE WHEN NOT is_untagged THEN cost ELSE 0 END), 0) AS tagged_cost,
       ROUND(100.0 * SUM(CASE WHEN is_untagged THEN cost ELSE 0 END) / NULLIF(SUM(cost), 0), 1) AS pct_untagged
FROM workload
GROUP BY product
HAVING total_cost > 0
ORDER BY untagged_cost DESC
"""


def by_workspace(days, tag_keys, workspaces=None):
    """Untagged spend per workspace — the multi-workspace view."""
    return f"""
WITH workload AS (
  SELECT workspace_id, workload_id,
         SUM(list_cost) AS cost,
         {_missing_keys_predicate(tag_keys, _AGG_KEYS)} AS is_untagged
  FROM {SUMMARY_TABLE}
  WHERE {_where(days, workspaces)}
  GROUP BY workspace_id, workload_id
),
agg AS (
  SELECT workspace_id,
         ROUND(SUM(cost), 0)                                       AS total_cost,
         ROUND(SUM(CASE WHEN is_untagged THEN cost ELSE 0 END), 0) AS untagged_cost,
         ROUND(100.0 * SUM(CASE WHEN is_untagged THEN cost ELSE 0 END) / NULLIF(SUM(cost), 0), 1) AS pct_untagged,
         COUNT(DISTINCT CASE WHEN is_untagged THEN workload_id END) AS untagged_workloads
  FROM workload GROUP BY workspace_id
)
SELECT a.workspace_id,
       COALESCE(ws.workspace_name, a.workspace_id) AS workspace_name,
       a.total_cost, a.untagged_cost, a.pct_untagged, a.untagged_workloads
FROM agg a
LEFT JOIN system.access.workspaces_latest ws ON a.workspace_id = ws.workspace_id
WHERE a.total_cost > 0
ORDER BY a.untagged_cost DESC
"""


def leaderboard(days, tag_keys, workspaces=None, limit=200):
    untag_expr = _missing_keys_predicate(tag_keys, _AGG_KEYS)
    return f"""
SELECT product,
       workload_id,
       MAX(workspace_id)                           AS workspace_id,
       MAX(workload_name)                          AS workload_name,
       MAX(owner)                                  AS owner,
       MAX(is_serverless)                          AS is_serverless,
       ROUND(SUM(list_cost), 0)                    AS untagged_cost
FROM {SUMMARY_TABLE}
WHERE {_where(days, workspaces)}
GROUP BY product, workload_id
HAVING {untag_expr} AND SUM(list_cost) > 0
ORDER BY untagged_cost DESC
LIMIT {limit}
"""


def untagged_workloads(days, tag_keys, workspaces=None, limit=10000):
    """ALL untagged workloads with the attributes bulk rules match on.

    Returns one row per workload with owner / workspace / product / name / cost,
    so rule matching + conflict detection can run client-side over the full set.
    """
    untag_expr = _missing_keys_predicate(tag_keys, _AGG_KEYS)
    return f"""
SELECT product,
       workload_id,
       MAX(workspace_id)                AS workspace_id,
       MAX(workload_name)               AS workload_name,
       MAX(owner)                       AS owner,
       MAX(is_serverless)               AS is_serverless,
       ROUND(SUM(list_cost), 0)         AS untagged_cost
FROM {SUMMARY_TABLE}
WHERE {_where(days, workspaces)}
GROUP BY product, workload_id
HAVING {untag_expr} AND SUM(list_cost) > 0
ORDER BY untagged_cost DESC
LIMIT {limit}
"""


def _rule_sql_predicate(rule):
    """SQL boolean for one rule's match condition over the workload aggregate.

    Drives the ENQUEUE path (enqueue_bulk_sql), so over-matching here means the
    writer job tags MORE resources than intended — escaping must be exact:
      - single quotes doubled (SQL literal safety)
      - LIKE metacharacters %/_/\\ escaped, with an explicit ESCAPE '\\' clause,
        so a user value like "data_eng" or "50%" matches literally, not as a glob.
    For the `matches` (glob) op we escape literal %/_/\\ FIRST, then translate the
    glob wildcards *→% and ?→_ so they stay active.
    """
    col = {"owner": "owner", "workspace": "workspace_id",
           "product": "product", "name": "workload_name"}[rule["field"]]
    val = rule["value"].replace("'", "''")
    c = f"lower(coalesce({col},''))"
    if rule["op"] == "equals":
        return f"{c} = lower('{val}')"
    if rule["op"] == "contains":
        lit = _escape_like(val)
        return f"{c} LIKE lower('%{lit}%') ESCAPE '\\\\'"
    if rule["op"] == "matches":  # glob -> LIKE
        like = _escape_like(val).replace("*", "%").replace("?", "_")
        return f"{c} LIKE lower('{like}') ESCAPE '\\\\'"
    return "false"


def _escape_like(val):
    """Escape LIKE metacharacters so they match literally (with ESCAPE '\\').

    Backslash first (so we don't double-escape the escapes we add), then %/_.
    `val` has already had single quotes doubled by the caller.
    """
    return val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def bulk_rule_impact(days, tag_keys, rules, workspaces=None):
    """Evaluate rules ENTIRELY in SQL — returns aggregate impact, no row dump.

    A workload is 'covered' if it matches ANY rule. This computes coverage at the
    warehouse so the app never pulls the (potentially 50k+) full row set. Earlier
    rules win on conflict, but for COVERAGE/COST totals only first-match matters,
    so a single OR over the rules is exact for the headline numbers.
    """
    valid = [r for r in rules if r.get("value") and r.get("tags")]
    if not valid:
        return None
    any_match = " OR ".join(f"({_rule_sql_predicate(r)})" for r in valid)
    # per-rule first-match coverage via a CASE priority ladder
    ladder = "\n".join(
        f"      WHEN {_rule_sql_predicate(r)} THEN {i}" for i, r in enumerate(valid))
    handled = _not_already_handled(tag_keys[0], "wl.product", "wl.workload_id")
    return f"""
WITH wl AS (
  SELECT workload_id,
         MAX(workspace_id)  AS workspace_id,
         MAX(workload_name) AS workload_name,
         MAX(owner)         AS owner,
         product,
         {_missing_keys_predicate(tag_keys, _AGG_KEYS)} AS is_untagged,
         SUM(list_cost)     AS cost
  FROM {SUMMARY_TABLE}
  WHERE {_where(days, workspaces)}
  GROUP BY product, workload_id
),
-- Exclude workloads already tagged/in-flight for this key in the live queue: the
-- summary snapshot lags a write by up to a day, so filter on the queue too.
untagged AS (SELECT * FROM wl WHERE is_untagged AND cost > 0 AND {handled}),
matched AS (
  SELECT *,
         CASE
{ladder}
           ELSE -1 END AS first_rule
  FROM untagged
  WHERE {any_match}
)
SELECT
  (SELECT COUNT(*) FROM untagged)                    AS total_untagged,
  (SELECT ROUND(SUM(cost),0) FROM untagged)          AS total_untagged_cost,
  COUNT(*)                                           AS matched_count,
  ROUND(SUM(cost),0)                                 AS matched_cost
FROM matched
"""


def bulk_rule_per_rule(days, tag_keys, rules, workspaces=None):
    """Per-rule first-match coverage (how many workloads each rule newly tags)."""
    valid = [r for r in rules if r.get("value") and r.get("tags")]
    if not valid:
        return None
    ladder = "\n".join(
        f"      WHEN {_rule_sql_predicate(r)} THEN {i}" for i, r in enumerate(valid))
    handled = _not_already_handled(tag_keys[0], "wl.product", "wl.workload_id")
    return f"""
WITH wl AS (
  SELECT workload_id, product,
         {_missing_keys_predicate(tag_keys, _AGG_KEYS)} AS is_untagged,
         SUM(list_cost) AS cost,
         MAX(owner) AS owner, MAX(workspace_id) AS workspace_id, MAX(workload_name) AS workload_name
  FROM {SUMMARY_TABLE}
  WHERE {_where(days, workspaces)}
  GROUP BY product, workload_id
),
matched AS (
  SELECT cost, CASE
{ladder}
           ELSE -1 END AS first_rule
  FROM wl WHERE is_untagged AND cost > 0 AND {handled}
)
SELECT first_rule, COUNT(*) AS workloads, ROUND(SUM(cost),0) AS cost
FROM matched WHERE first_rule >= 0
GROUP BY first_rule ORDER BY first_rule
"""


def bulk_rule_sample(days, tag_keys, rules, workspaces=None, limit=200):
    """The CONCRETE per-workload list a rule set would tag: name, product, owner,
    cost, AND the exact value each gets (from the first rule that matches). This is
    what a human reviews before applying, so they can catch a rule that lumps
    unrelated workloads under one team. Only taggable workloads (writer can't tag
    Apps/serverless/etc., so they'd never be enqueued — don't show them here)."""
    valid = [r for r in rules if r.get("value") and r.get("tags")]
    if not valid:
        return None
    any_match = " OR ".join(f"({_rule_sql_predicate(r)})" for r in valid)
    ladder = "\n".join(
        f"      WHEN {_rule_sql_predicate(r)} THEN {i}" for i, r in enumerate(valid))
    # map first_rule index -> the tag value it assigns for `tag_keys[0]`
    key = tag_keys[0]
    val_ladder = "\n".join(
        f"      WHEN {i} THEN {_sql_str(r['tags'].get(key, ''))}"
        for i, r in enumerate(valid))
    handled = _not_already_handled(tag_keys[0], "wl.product", "wl.workload_id")
    return f"""
WITH wl AS (
  SELECT workload_id, product,
         MAX(workspace_id) AS workspace_id, MAX(workload_name) AS workload_name, MAX(owner) AS owner,
         MAX(is_serverless) AS is_serverless,
         {_missing_keys_predicate(tag_keys, _AGG_KEYS)} AS is_untagged,
         SUM(list_cost) AS cost
  FROM {SUMMARY_TABLE}
  WHERE {_where(days, workspaces)}
  GROUP BY product, workload_id
),
matched AS (
  SELECT workload_id, product, workspace_id, workload_name, owner,
         is_serverless, ROUND(cost,0) AS cost,
         CASE
{ladder}
           ELSE -1 END AS first_rule
  FROM wl
  WHERE is_untagged AND cost > 0 AND ({any_match})
    AND {_taggable_predicate('product', 'is_serverless')}
    -- Don't surface workloads already tagged/in-flight for this key (queue lags snapshot).
    AND {handled}
)
SELECT workload_id, product, workspace_id, workload_name, owner,
       is_serverless, cost,
       CASE first_rule
{val_ladder}
       END AS new_tag_value
FROM matched
WHERE first_rule >= 0
ORDER BY cost DESC
LIMIT {limit}
"""


def workspace_options(days):
    """All workspaces in the table (for the sidebar picker), ranked by spend, with names."""
    return f"""
WITH s AS (
  SELECT workspace_id, ROUND(SUM(list_cost), 0) AS cost
  FROM {SUMMARY_TABLE}
  WHERE usage_date >= current_date() - INTERVAL {days} DAYS
  GROUP BY workspace_id
)
SELECT s.workspace_id,
       COALESCE(ws.workspace_name, s.workspace_id) AS workspace_name,
       s.cost
FROM s
LEFT JOIN system.access.workspaces_latest ws ON s.workspace_id = ws.workspace_id
ORDER BY s.cost DESC
"""


def distinct_tag_keys(days, workspaces=None):
    """All tag keys currently in use — to seed the picker."""
    return f"""
SELECT tag_key, COUNT(DISTINCT workload_id) AS workloads
FROM (
  SELECT workload_id, explode(tag_keys) AS tag_key
  FROM {SUMMARY_TABLE}
  WHERE {_where(days, workspaces)}
)
GROUP BY tag_key
ORDER BY workloads DESC
LIMIT 50
"""


def distinct_field_values(field, days, workspaces=None, limit=2000):
    """Distinct values of a rule field (owner/product/workspace/name) among
    untagged-relevant workloads, sorted alphabetically — to populate the
    rules-builder picker so a user selects a real owner instead of typing a raw
    email. Limit is high (a datalist is cheap) so alphabetical sort doesn't hide
    owners late in the alphabet."""
    col = {"owner": "owner", "product": "product",
           "workspace": "workspace_id", "name": "workload_name"}.get(field, "owner")
    return f"""
SELECT {col} AS value, ROUND(SUM(list_cost), 0) AS cost,
       COUNT(DISTINCT workload_id) AS workloads
FROM {SUMMARY_TABLE}
WHERE {_where(days, workspaces)} AND {col} IS NOT NULL AND {col} <> ''
GROUP BY {col}
ORDER BY lower({col}) ASC
LIMIT {limit}
"""


def freshness():
    """Max usage_date in the summary table, to show 'data as of' in the UI."""
    return f"SELECT MAX(usage_date) AS as_of, COUNT(*) AS rows FROM {SUMMARY_TABLE}"


# ============================================================================
# Write-queue enqueue + status (feeds the writer job)
# ============================================================================
# The app records write INTENT into QUEUE_TABLE. Bulk enqueue is a single
# INSERT … SELECT evaluated in the warehouse — matched workloads are expanded to
# one queue row per (workload, tag_key) directly in SQL, so a 50k-workload rule
# never round-trips through the browser.

def _sql_str(v):
    """Quote-escape a python string as a SQL literal. Doubles single quotes AND
    escapes backslashes — Databricks/Spark honors C-style backslash escapes in
    string literals, so a lone trailing backslash could otherwise escape the
    closing quote. Also strips NUL/newlines. Belt-and-suspenders with the API-side
    charset validation in app._validate_tag."""
    s = str(v).replace("\\", "\\\\").replace("'", "''")
    s = s.replace("\x00", "").replace("\n", " ").replace("\r", " ")
    return "'" + s + "'"


def enqueue_bulk_sql(batch_id, requested_by, days, tag_keys, rules, workspaces=None):
    """INSERT one queue row per (matched workload, tag_key) for a rule set.

    Reuses the exact untagged + first-match-wins logic of bulk_rule_impact: a
    workload is tagged by the FIRST rule (by priority) whose predicate it matches,
    and gets that rule's tags. Earlier rules win, matching those UI numbers.
    Returns None if there are no valid rules.
    """
    valid = [r for r in rules if r.get("value") and r.get("tags")]
    if not valid:
        return None

    ladder = "\n".join(
        f"      WHEN {_rule_sql_predicate(r)} THEN {i}" for i, r in enumerate(valid))

    # Map each rule index -> its tags, expanded into (rule_idx, key, value) rows
    # so a LATERAL join attaches the winning rule's tags to each workload.
    tag_tuples = []
    for i, r in enumerate(valid):
        for k, v in r["tags"].items():
            tag_tuples.append(f"({i}, {_sql_str(k)}, {_sql_str(v)})")
    tags_values = ",\n    ".join(tag_tuples)

    return f"""
INSERT INTO {QUEUE_TABLE}
  (batch_id, enqueued_at, requested_by, workspace_id, product, workload_id,
   workload_name, is_serverless, tag_key, tag_value, list_cost, status, attempts)
WITH wl AS (
  SELECT workload_id, product,
         MAX(workspace_id)  AS workspace_id,
         MAX(workload_name) AS workload_name,
         MAX(owner)         AS owner,
         MAX(is_serverless) AS is_serverless,
         {_missing_keys_predicate(tag_keys, _AGG_KEYS)} AS is_untagged,
         SUM(list_cost)     AS cost
  FROM {SUMMARY_TABLE}
  WHERE {_where(days, workspaces)}
  GROUP BY product, workload_id
),
matched AS (
  SELECT *, CASE
{ladder}
           ELSE -1 END AS first_rule
  FROM wl
  -- Only enqueue workloads the writer can actually per-resource tag. Without
  -- this, a rule matching e.g. an APPS workload would enqueue a row that sits
  -- PENDING forever (the writer can't tag Apps — they're budget-policy governed).
  WHERE is_untagged AND cost > 0
    AND {_taggable_predicate('product', 'is_serverless')}
),
ruletags(rule_idx, tag_key, tag_value) AS (
  VALUES
    {tags_values}
)
SELECT
  {_sql_str(batch_id)}      AS batch_id,
  current_timestamp()       AS enqueued_at,
  {_sql_str(requested_by)}  AS requested_by,
  m.workspace_id, m.product, m.workload_id, m.workload_name, m.is_serverless,
  rt.tag_key, rt.tag_value,
  m.cost                    AS list_cost,
  'PENDING'                 AS status,
  0                         AS attempts
FROM matched m
JOIN ruletags rt ON rt.rule_idx = m.first_rule
WHERE m.first_rule >= 0
"""


def enqueue_single_sql(batch_id, requested_by, workspace_id, product, workload_id,
                       workload_name, is_serverless, tags, list_cost=None):
    """INSERT queue rows for one workload with an explicit tag dict.

    list_cost is carried so single-enqueued rows drain cost-descending like bulk
    rows and show their real cost in the Batches tab (NULL → drained last, $0).
    """
    cost_sql = "NULL" if list_cost is None else f"CAST({float(list_cost)} AS DECIMAL(38,6))"
    rows = []
    for k, v in tags.items():
        rows.append(
            f"({_sql_str(batch_id)}, current_timestamp(), {_sql_str(requested_by)}, "
            f"{_sql_str(workspace_id)}, {_sql_str(product)}, {_sql_str(workload_id)}, "
            f"{_sql_str(workload_name or workload_id)}, {1 if is_serverless else 0}, "
            f"{_sql_str(k)}, {_sql_str(v)}, {cost_sql}, 'PENDING', 0)")
    values = ",\n  ".join(rows)
    return f"""
INSERT INTO {QUEUE_TABLE}
  (batch_id, enqueued_at, requested_by, workspace_id, product, workload_id,
   workload_name, is_serverless, tag_key, tag_value, list_cost, status, attempts)
VALUES
  {values}
"""


def enqueue_explicit_sql(batch_id, requested_by, tag_key, workloads):
    """INSERT one row per workload from an EXPLICIT, user-selected list.

    This is the "review the list, uncheck the ones that don't belong, apply only
    the rest" path — the frontend sends exactly the workloads the human confirmed,
    each with its own tag value. Unlike enqueue_bulk_sql it does NOT re-evaluate a
    rule, so unchecked rows are simply never inserted. Each workload dict:
      {workload_id, product, workspace_id, workload_name, is_serverless, tag_value, cost}
    Returns None if the list is empty.
    """
    rows = []
    for w in workloads:
        if not w.get("workload_id") or not w.get("tag_value"):
            continue
        cost = w.get("cost")
        cost_sql = "NULL" if cost in (None, "") else f"CAST({float(cost)} AS DECIMAL(38,6))"
        rows.append(
            f"({_sql_str(batch_id)}, current_timestamp(), {_sql_str(requested_by)}, "
            f"{_sql_str(str(w.get('workspace_id') or ''))}, {_sql_str(w['product'])}, "
            f"{_sql_str(str(w['workload_id']))}, "
            f"{_sql_str(w.get('workload_name') or str(w['workload_id']))}, "
            f"{1 if w.get('is_serverless') else 0}, "
            f"{_sql_str(tag_key)}, {_sql_str(w['tag_value'])}, {cost_sql}, 'PENDING', 0)")
    if not rows:
        return None
    values = ",\n  ".join(rows)
    return f"""
INSERT INTO {QUEUE_TABLE}
  (batch_id, enqueued_at, requested_by, workspace_id, product, workload_id,
   workload_name, is_serverless, tag_key, tag_value, list_cost, status, attempts)
VALUES
  {values}
"""


def count_queue_rows(batch_id):
    """Ground-truth row count for a batch — the app counts rows this way instead
    of trusting DML rowcount (the SQL connector reports -1 for INSERTs)."""
    return f"SELECT COUNT(*) AS n FROM {QUEUE_TABLE} WHERE batch_id = {_sql_str(batch_id)}"


def batch_status(batch_id):
    """Per-status counts + cost for one batch — headline for the Batches tab."""
    return f"""
SELECT status,
       COUNT(*)                          AS rows,
       COUNT(DISTINCT workload_id)       AS workloads,
       ROUND(SUM(list_cost), 0)          AS cost
FROM {QUEUE_TABLE}
WHERE batch_id = {_sql_str(batch_id)}
GROUP BY status
ORDER BY status
"""


def batch_rows(batch_id, limit=500):
    """Per-workload rows for a batch, with status + any error (for the detail table)."""
    return f"""
SELECT product, workload_id, workload_name, tag_key, tag_value,
       status, last_error, ROUND(list_cost, 0) AS cost
FROM {QUEUE_TABLE}
WHERE batch_id = {_sql_str(batch_id)}
ORDER BY list_cost DESC NULLS LAST
LIMIT {limit}
"""


def recent_batches(limit=25):
    """Most recent batches with rollup status, for the Batches & Rollback tab."""
    return f"""
SELECT batch_id,
       MAX(enqueued_at)                                        AS enqueued_at,
       MAX(requested_by)                                       AS requested_by,
       COUNT(*)                                                AS rows,
       COUNT(DISTINCT workload_id)                             AS workloads,
       ROUND(SUM(list_cost), 0)                                AS cost,
       SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END)       AS pending,
       SUM(CASE WHEN status='RUNNING' THEN 1 ELSE 0 END)       AS running,
       SUM(CASE WHEN status='SUCCEEDED' THEN 1 ELSE 0 END)     AS succeeded,
       SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END)        AS failed,
       SUM(CASE WHEN status='UNSUPPORTED' THEN 1 ELSE 0 END)   AS unsupported,
       SUM(CASE WHEN status='SKIPPED_DRYRUN' THEN 1 ELSE 0 END) AS dry_run,
       SUM(CASE WHEN status='ROLLED_BACK' THEN 1 ELSE 0 END)   AS rolled_back
FROM {QUEUE_TABLE}
GROUP BY batch_id
ORDER BY enqueued_at DESC
LIMIT {limit}
"""


def batch_failure_breakdown(batch_id, limit=50):
    """Per (product, status, reason) rollup — so the batch view can show WHY rows
    didn't tag (e.g. PermissionDenied on jobs you don't own, ResourceDoesNotExist
    for deleted resources, UNSUPPORTED product limits), instead of a bare 'FAILED'
    count that reads like success.

    Groups by a NORMALIZED reason (the error class / leading phrase, not the full
    message which embeds a unique resource id) so several different failure causes
    within one product each get their own row + count, rather than collapsing to a
    single MIN() sample that hides the rest."""
    # Normalize: take the text up to the first ':' (the exception class / phrase),
    # so "PermissionDenied: job 123 ..." and "PermissionDenied: job 456 ..." group
    # together, while a genuinely different cause stays separate. Full example kept
    # as sample_reason for the tooltip/first-row detail.
    reason_norm = "COALESCE(NULLIF(TRIM(SPLIT(last_error, ':')[0]), ''), 'unknown')"
    return f"""
SELECT product,
       status,
       {reason_norm}            AS reason,
       COUNT(*)                 AS rows,
       ROUND(SUM(list_cost), 0) AS cost,
       MIN(last_error)          AS sample_reason
FROM {QUEUE_TABLE}
WHERE batch_id = {_sql_str(batch_id)}
  AND status IN ('FAILED', 'UNSUPPORTED')
GROUP BY product, status, {reason_norm}
ORDER BY rows DESC
LIMIT {limit}
"""


def audit_for_batch(batch_id, limit=500):
    """Audit history for a batch — the old→new record, for verify + rollback view."""
    return f"""
SELECT executed_at, action, product, workload_id, tag_key,
       old_value, new_value, status, error
FROM {AUDIT_TABLE}
WHERE batch_id = {_sql_str(batch_id)}
ORDER BY executed_at DESC
LIMIT {limit}
"""


# ============================================================================
# Advisory agent suggestions (ai_query hints) — read-only join for the UI
# ============================================================================

def suggestions_join(days, tag_keys, workspaces=None, limit=200):
    """Leaderboard of untagged workloads WITH the advisory ai_query suggestion.

    LEFT JOINs the suggestions table so workloads without a suggestion still show.
    The suggestion is a HINT only — the human decides whether to act on it.
    """
    untag_expr = _missing_keys_predicate(tag_keys, _AGG_KEYS)
    return f"""
WITH wl AS (
  SELECT product, workload_id,
         MAX(workspace_id)               AS workspace_id,
         MAX(workload_name)              AS workload_name,
         MAX(owner)                      AS owner,
         MAX(is_serverless)              AS is_serverless,
         ROUND(SUM(list_cost), 0)        AS untagged_cost
  FROM {SUMMARY_TABLE}
  WHERE {_where(days, workspaces)}
  GROUP BY product, workload_id
  HAVING {untag_expr} AND SUM(list_cost) > 0
)
SELECT wl.product, wl.workload_id, wl.workspace_id, wl.workload_name, wl.owner,
       wl.is_serverless, wl.untagged_cost,
       s.suggested_cost_center, s.confidence, s.rationale
FROM wl
LEFT JOIN {SUGGESTIONS_TABLE} s
  ON s.workload_id = wl.workload_id AND s.product = wl.product
ORDER BY wl.untagged_cost DESC
LIMIT {limit}
"""


# ----------------------------------------------------------------------------
# Bulk tag FROM AI suggestions (threshold + one-click). The agent already
# proposed a cost-center per workload (annotate.sql); this lets a human enqueue
# ALL confident suggestions at once instead of typing thousands of values. The
# human still gates the batch (picks the key + confidence cutoff, reviews the
# count/cost, clicks once). A workload qualifies when it is untagged for the
# chosen key, has a real (non-"unknown") suggestion, and confidence >= cutoff.
# ----------------------------------------------------------------------------

def _suggestion_candidates_cte(tag_key, min_confidence, days, workspaces):
    """Shared CTE: untagged workloads with a confident, real suggestion."""
    # A workload is "untagged for this key" if the chosen key is absent.
    untag_expr = _missing_keys_predicate([tag_key], _AGG_KEYS)
    return f"""
WITH wl AS (
  SELECT product, workload_id,
         MAX(workspace_id)  AS workspace_id,
         MAX(workload_name) AS workload_name,
         MAX(is_serverless) AS is_serverless,
         SUM(list_cost)     AS cost
  FROM {SUMMARY_TABLE}
  WHERE {_where(days, workspaces)}
  GROUP BY product, workload_id
  HAVING {untag_expr} AND SUM(list_cost) > 0
),
cand AS (
  SELECT wl.product, wl.workload_id, wl.workspace_id, wl.workload_name,
         wl.is_serverless, wl.cost,
         s.suggested_cost_center, s.confidence
  FROM wl
  JOIN {SUGGESTIONS_TABLE} s
    ON s.workload_id = wl.workload_id AND s.product = wl.product
  WHERE s.suggested_cost_center IS NOT NULL
    AND lower(s.suggested_cost_center) <> 'unknown'
    AND s.confidence >= {float(min_confidence)}
    -- Only surface workloads the writer can actually tag; policy-governed
    -- products (Apps, serverless SQL, …) would be enqueued then skipped
    -- UNSUPPORTED and reappear next time. Filter them out here.
    AND {_taggable_predicate('wl.product', 'wl.is_serverless')}
    -- Don't double-tag: skip workloads already tagged/in-flight for this key.
    -- The summary snapshot lags a live write by up to a day, so check the queue.
    AND {_not_already_handled(tag_key, 'wl.product', 'wl.workload_id')}
)"""


def suggestions_bulk_impact(tag_key, min_confidence, days, workspaces=None):
    """Aggregate impact of enqueuing all confident suggestions for `tag_key`."""
    cte = _suggestion_candidates_cte(tag_key, min_confidence, days, workspaces)
    return cte + """
SELECT COUNT(*)                              AS workloads,
       ROUND(SUM(cost), 0)                   AS cost,
       COUNT(DISTINCT suggested_cost_center) AS distinct_values
FROM cand
"""


def policy_governed_impact(tag_key, min_confidence, days, workspaces=None):
    """Untagged workloads that have a confident suggestion but CANNOT be tagged by
    the writer (Apps, serverless SQL, …). Surfaced so the UI can explain why they
    aren't in the apply list instead of silently dropping them — they need a
    budget/tag policy, not a per-resource write."""
    untag_expr = _missing_keys_predicate([tag_key], _AGG_KEYS)
    return f"""
WITH wl AS (
  SELECT product, workload_id,
         MAX(is_serverless) AS is_serverless,
         SUM(list_cost)     AS cost
  FROM {SUMMARY_TABLE}
  WHERE {_where(days, workspaces)}
  GROUP BY product, workload_id
  HAVING {untag_expr} AND SUM(list_cost) > 0
)
SELECT COUNT(*)               AS workloads,
       ROUND(SUM(wl.cost), 0) AS cost
FROM wl
JOIN {SUGGESTIONS_TABLE} s
  ON s.workload_id = wl.workload_id AND s.product = wl.product
WHERE s.suggested_cost_center IS NOT NULL
  AND lower(s.suggested_cost_center) <> 'unknown'
  AND s.confidence >= {float(min_confidence)}
  AND NOT {_taggable_predicate('wl.product', 'wl.is_serverless')}
"""


def suggestions_bulk_breakdown(tag_key, min_confidence, days, workspaces=None, limit=50):
    """Per-suggested-value coverage, so the human sees WHAT would be assigned."""
    cte = _suggestion_candidates_cte(tag_key, min_confidence, days, workspaces)
    return cte + f"""
SELECT suggested_cost_center AS value,
       COUNT(*)              AS workloads,
       ROUND(SUM(cost), 0)   AS cost,
       ROUND(MIN(confidence), 2) AS min_conf
FROM cand
GROUP BY suggested_cost_center
ORDER BY cost DESC
LIMIT {limit}
"""


def suggestions_bulk_list(tag_key, min_confidence, days, workspaces=None, limit=100):
    """The CONCRETE per-workload list: exactly which workloads get tagged and with
    what value. This is what a human needs to see before clicking apply — one row
    per workload, highest-cost first, showing the tag value the AI would set."""
    cte = _suggestion_candidates_cte(tag_key, min_confidence, days, workspaces)
    return cte + f"""
SELECT product,
       workload_id,
       workspace_id,
       is_serverless,
       workload_name,
       ROUND(cost, 0)            AS cost,
       suggested_cost_center     AS new_tag_value,
       ROUND(confidence, 2)      AS confidence
FROM cand
ORDER BY cost DESC
LIMIT {limit}
"""


def enqueue_from_suggestions_sql(batch_id, requested_by, tag_key, min_confidence,
                                 days, workspaces=None, exclude_batch_id=None):
    """INSERT one queue row per confident suggestion — tag_value = the AI's pick.

    This is the agent-driven bulk path: no per-workload typing. Evaluated entirely
    in the warehouse, so 1000s of workloads enqueue in one statement. The CTE must
    follow INSERT INTO (INSERT INTO t WITH cte AS (...) SELECT ...).

    exclude_batch_id: if given, workloads already queued for `tag_key` in that
    batch are skipped — this is how the "rules first, AI for the rest" auto-tag
    flow avoids double-tagging a workload a rule already covered.
    """
    cte = _suggestion_candidates_cte(tag_key, min_confidence, days, workspaces)
    exclude = ""
    if exclude_batch_id:
        exclude = f"""
  AND NOT EXISTS (
    SELECT 1 FROM {QUEUE_TABLE} q
    WHERE q.batch_id = {_sql_str(exclude_batch_id)}
      AND q.tag_key = {_sql_str(tag_key)}
      AND q.workload_id = cand.workload_id
      AND q.product = cand.product
  )"""
    return f"""
INSERT INTO {QUEUE_TABLE}
  (batch_id, enqueued_at, requested_by, workspace_id, product, workload_id,
   workload_name, is_serverless, tag_key, tag_value, list_cost, status, attempts)
{cte}
SELECT
  {_sql_str(batch_id)}      AS batch_id,
  current_timestamp()       AS enqueued_at,
  {_sql_str(requested_by)}  AS requested_by,
  workspace_id, product, workload_id, workload_name, is_serverless,
  {_sql_str(tag_key)}       AS tag_key,
  suggested_cost_center     AS tag_value,
  cost                      AS list_cost,
  'PENDING'                 AS status,
  0                         AS attempts
FROM cand
WHERE 1=1{exclude}
"""


# --------------------------------------------------------------------- inventory (Phase 1 live scan)
def inventory_summary():
    """Per-product rollup of the LATEST live scan: total resources, how many are
    truly untagged (deep read succeeded and tag_count=0), and how many reads
    failed (state unknown, not counted as untagged). Reads the most recent
    scan_id only, so stale scans don't mix in."""
    return f"""
WITH latest AS (
  SELECT max(scan_id) AS scan_id FROM {INVENTORY_TABLE}
)
SELECT i.product,
       count(*)                                                       AS resources,
       sum(CASE WHEN i.tag_read_ok AND i.tag_count = 0 THEN 1 ELSE 0 END) AS untagged,
       sum(CASE WHEN i.tag_count > 0 THEN 1 ELSE 0 END)               AS tagged,
       sum(CASE WHEN NOT i.tag_read_ok THEN 1 ELSE 0 END)             AS read_failed,
       max(i.fallback)                                                AS fallback
FROM {INVENTORY_TABLE} i
JOIN latest l ON i.scan_id = l.scan_id
WHERE i.workload_id NOT LIKE '\\_\\_%'          -- exclude sentinel error rows
GROUP BY i.product
ORDER BY resources DESC
"""


def inventory_meta():
    """Latest scan id + when it ran + total resources, for the UI header."""
    return f"""
WITH latest AS (SELECT max(scan_id) AS scan_id FROM {INVENTORY_TABLE})
SELECT l.scan_id,
       max(i.scanned_at) AS scanned_at,
       count(*)          AS resources
FROM {INVENTORY_TABLE} i JOIN latest l ON i.scan_id = l.scan_id
GROUP BY l.scan_id
"""
