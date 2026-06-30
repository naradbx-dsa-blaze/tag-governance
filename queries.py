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

# The summary table location is configurable so the app can be deployed into any
# account (e.g. Bayada's) by setting TAG_GOVERNANCE_SUMMARY_TABLE — no code change.
SUMMARY_TABLE = os.environ.get(
    "TAG_GOVERNANCE_SUMMARY_TABLE",
    "users.narasimha_kamathardi.tag_governance_workload_daily",
)

# tag_keys aggregated up from the daily rows of one workload
_AGG_KEYS = "array_distinct(flatten(collect_list(tag_keys)))"


def _missing_keys_predicate(tag_keys, col="tag_keys"):
    """True when the workload is missing ALL chosen keys."""
    if not tag_keys:
        return f"size({col}) = 0"
    arr = ", ".join(f"'{k}'" for k in tag_keys)
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
    """SQL boolean for one rule's match condition over the workload aggregate."""
    col = {"owner": "owner", "workspace": "workspace_id",
           "product": "product", "name": "workload_name"}[rule["field"]]
    val = rule["value"].replace("'", "''")
    c = f"lower(coalesce({col},''))"
    if rule["op"] == "equals":
        return f"{c} = lower('{val}')"
    if rule["op"] == "contains":
        return f"{c} LIKE lower('%{val}%')"
    if rule["op"] == "matches":  # glob -> LIKE
        like = val.replace("%", "\\%").replace("_", "\\_").replace("*", "%").replace("?", "_")
        return f"{c} LIKE lower('{like}')"
    return "false"


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
untagged AS (SELECT * FROM wl WHERE is_untagged AND cost > 0),
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
  FROM wl WHERE is_untagged AND cost > 0
)
SELECT first_rule, COUNT(*) AS workloads, ROUND(SUM(cost),0) AS cost
FROM matched WHERE first_rule >= 0
GROUP BY first_rule ORDER BY first_rule
"""


def bulk_rule_sample(days, tag_keys, rules, workspaces=None, limit=50):
    """A small sample of matched workloads, with which rule (priority) tags each."""
    valid = [r for r in rules if r.get("value") and r.get("tags")]
    if not valid:
        return None
    any_match = " OR ".join(f"({_rule_sql_predicate(r)})" for r in valid)
    ladder = "\n".join(
        f"      WHEN {_rule_sql_predicate(r)} THEN {i}" for i, r in enumerate(valid))
    return f"""
WITH wl AS (
  SELECT workload_id, product,
         MAX(workspace_id) AS workspace_id, MAX(workload_name) AS workload_name, MAX(owner) AS owner,
         {_missing_keys_predicate(tag_keys, _AGG_KEYS)} AS is_untagged,
         SUM(list_cost) AS cost
  FROM {SUMMARY_TABLE}
  WHERE {_where(days, workspaces)}
  GROUP BY product, workload_id
)
SELECT product, workload_name, owner, ROUND(cost,0) AS cost,
       CASE
{ladder}
         ELSE -1 END AS first_rule
FROM wl
WHERE is_untagged AND cost > 0 AND ({any_match})
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


def freshness():
    """Max usage_date in the summary table, to show 'data as of' in the UI."""
    return f"SELECT MAX(usage_date) AS as_of, COUNT(*) AS rows FROM {SUMMARY_TABLE}"
