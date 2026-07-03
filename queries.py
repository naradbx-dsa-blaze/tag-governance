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

# Write-queue and audit tables — configurable for cross-account deploys, same as
# SUMMARY_TABLE. The app INSERTs intent into QUEUE_TABLE; the writer job drains it
# and records changes to AUDIT_TABLE.
QUEUE_TABLE = os.environ.get(
    "TAG_GOVERNANCE_QUEUE_TABLE",
    "users.narasimha_kamathardi.tag_governance_write_queue",
)
AUDIT_TABLE = os.environ.get(
    "TAG_GOVERNANCE_AUDIT_TABLE",
    "users.narasimha_kamathardi.tag_governance_audit",
)
# Advisory ai_query suggestions (agent hints). Read-only join for the UI.
SUGGESTIONS_TABLE = os.environ.get(
    "TAG_GOVERNANCE_SUGGESTIONS_TABLE",
    "users.narasimha_kamathardi.tag_governance_suggestions",
)

# tag_keys aggregated up from the daily rows of one workload
_AGG_KEYS = "array_distinct(flatten(collect_list(tag_keys)))"


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


# ============================================================================
# Write-queue enqueue + status (feeds the writer job)
# ============================================================================
# The app records write INTENT into QUEUE_TABLE. Bulk enqueue is a single
# INSERT … SELECT evaluated in the warehouse — matched workloads are expanded to
# one queue row per (workload, tag_key) directly in SQL, so a 50k-workload rule
# never round-trips through the browser.

def _sql_str(v):
    """Quote-escape a python string for inline SQL."""
    return "'" + str(v).replace("'", "''") + "'"


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
  WHERE is_untagged AND cost > 0
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
       SUM(CASE WHEN status='SUCCEEDED' THEN 1 ELSE 0 END)     AS succeeded,
       SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END)        AS failed,
       SUM(CASE WHEN status='UNSUPPORTED' THEN 1 ELSE 0 END)   AS unsupported,
       SUM(CASE WHEN status='SKIPPED_DRYRUN' THEN 1 ELSE 0 END) AS dry_run
FROM {QUEUE_TABLE}
GROUP BY batch_id
ORDER BY enqueued_at DESC
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


def enqueue_from_suggestions_sql(batch_id, requested_by, tag_key, min_confidence,
                                 days, workspaces=None):
    """INSERT one queue row per confident suggestion — tag_value = the AI's pick.

    This is the agent-driven bulk path: no per-workload typing. Evaluated entirely
    in the warehouse, so 1000s of workloads enqueue in one statement. The CTE must
    follow INSERT INTO (INSERT INTO t WITH cte AS (...) SELECT ...).
    """
    cte = _suggestion_candidates_cte(tag_key, min_confidence, days, workspaces)
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
"""
