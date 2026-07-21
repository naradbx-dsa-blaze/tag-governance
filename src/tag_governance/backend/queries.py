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

from db import Query


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

# Confidence is a stored DOUBLE; suggestion confidences are quantized to 1 decimal
# (0.2, 0.3, … 0.8). A `confidence >= min_confidence` filter at a BOUNDARY value
# (e.g. cutoff 0.8, rows stored as exactly 0.8) is fragile across SQL-driver float
# encodings: if the bound parameter arrives as 0.80000000001 it excludes every 0.8
# row and the UI shows "no confident suggestions" even though there are hundreds.
# Nudge the threshold down by half a quantization step so a cutoff of 0.8 reliably
# INCLUDES rows stored as 0.8, independent of driver precision. 0.05 is safely
# below the 0.1 spacing so it never pulls in the next bucket (0.7).
_CONF_EPSILON = 0.05


def _conf_floor(min_confidence) -> float:
    """Boundary-safe confidence threshold: `>= _conf_floor(0.8)` matches 0.8 rows."""
    return float(min_confidence) - _CONF_EPSILON

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
    bag = _Bag()
    return Query(f"""
SELECT
  COUNT(DISTINCT workload_id)                         AS workloads,
  ROUND(SUM(list_cost), 0)                            AS cost
FROM (
  SELECT workload_id, MAX(list_cost) AS list_cost
  FROM {QUEUE_TABLE}
  WHERE status = 'SUCCEEDED' AND tag_key = {bag.add(tag_key,'key')}
  GROUP BY workload_id
)
""", bag.params)


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
    bag = _Bag()
    untag_expr = _missing_keys_predicate([tag_key], _AGG_KEYS, bag)
    where = _where(days, workspaces, bag)
    plist = ", ".join(f"'{p}'" for p in _POLICY_GOVERNED_PRODUCTS)
    taggable = ", ".join(f"'{p}'" for p in _API_TAGGABLE_PRODUCTS)
    return Query(f"""
WITH wl AS (
  SELECT product, workload_id,
         MAX(is_serverless) AS is_serverless,
         SUM(list_cost)     AS cost
  FROM {SUMMARY_TABLE}
  WHERE {where}
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
""", bag.params)


# Queue statuses that make a (workload, tag_key) NOT worth recommending again:
#  - SUCCEEDED / PENDING / RUNNING: already tagged or in flight (never re-suggest).
#  - UNSUPPORTED: the product has no tag-write path (Apps, serverless SQL, …) — it
#    can NEVER succeed, so re-recommending it is pure noise.
#  - FAILED: the last attempt failed (permission denied, resource deleted). Per
#    user request we DON'T keep resurfacing these — a failed workload shouldn't
#    reappear in every scan. The retry path is explicit, not automatic: the FAILED
#    queue rows still exist, so re-running the writer on that batch retries them
#    once the underlying permission is fixed (or use a force flag to re-recommend).
_HANDLED_STATUSES = ("SUCCEEDED", "PENDING", "RUNNING", "UNSUPPORTED", "FAILED")


def _not_already_handled(tag_key, product_col="product", workload_col="workload_id",
                         bag=None, statuses=_HANDLED_STATUSES):
    """SQL boolean: True unless this (workload, tag_key) is already handled in the
    queue (see _HANDLED_STATUSES). The summary table's tag_keys is a daily
    snapshot, so right after a write it still reads 'untagged' — this queries the
    live queue so we never re-recommend a workload we've already acted on for the
    same key. This is the durable 'don't re-recommend' guard.

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
    key_sql = bag.add(tag_key, "key") if bag is not None else _sql_str(tag_key)
    status_list = ", ".join(f"'{s}'" for s in statuses)  # fixed internal constants
    return f"""NOT EXISTS (
    SELECT 1 FROM {QUEUE_TABLE} handled
    WHERE handled.workload_id = {workload_col}
      AND handled.product = {product_col}
      AND handled.tag_key = {key_sql}
      AND handled.status IN ({status_list})
  )"""


def _missing_keys_predicate(tag_keys, col="tag_keys", bag=None):
    """True when the workload is missing ALL chosen keys.

    Keys are user-chosen (sidebar). With a `bag`, each key is a bound parameter
    (no escaping needed); without one, we fall back to escaped literals for the
    static callers not yet migrated.
    """
    if not tag_keys:
        return f"size({col}) = 0"
    if bag is not None:
        markers = ", ".join(bag.add_all(tag_keys, "key"))
        return f"NOT arrays_overlap({col}, array({markers}))"
    arr = ", ".join("'" + str(k).replace("'", "''") + "'" for k in tag_keys)
    return f"NOT arrays_overlap({col}, array({arr}))"


def _where(days, workspaces, bag=None):
    """Shared WHERE clause: date window + optional workspace filter.

    With a `bag`, days and workspace ids are bound parameters; otherwise literal
    (days is always a validated int; workspace ids come from the summary table)."""
    if bag is not None:
        clause = f"usage_date >= current_date() - INTERVAL {bag.add(int(days), 'days')} DAYS"
        if workspaces:
            ids = ", ".join(bag.add_all(workspaces, "ws"))
            clause += f"\n    AND workspace_id IN ({ids})"
        return clause
    clause = f"usage_date >= current_date() - INTERVAL {int(days)} DAYS"
    if workspaces:
        ids = ", ".join(f"'{w}'" for w in workspaces)
        clause += f"\n    AND workspace_id IN ({ids})"
    return clause


def kpi_summary(days, tag_keys, workspaces=None):
    bag = _Bag()
    untag = _missing_keys_predicate(tag_keys, _AGG_KEYS, bag)
    where = _where(days, workspaces, bag)
    return Query(f"""
WITH workload AS (
  SELECT product, workload_id,
         SUM(list_cost) AS cost,
         {untag} AS is_untagged
  FROM {SUMMARY_TABLE}
  WHERE {where}
  GROUP BY product, workload_id
)
SELECT
  ROUND(SUM(cost), 0)                                              AS total_cost,
  ROUND(SUM(CASE WHEN is_untagged THEN cost ELSE 0 END), 0)        AS untagged_cost,
  ROUND(100.0 * SUM(CASE WHEN is_untagged THEN cost ELSE 0 END) / NULLIF(SUM(cost), 0), 1) AS pct_untagged,
  COUNT(DISTINCT CASE WHEN is_untagged THEN workload_id END)       AS untagged_workloads
FROM workload
""", bag.params)


def by_product(days, tag_keys, workspaces=None):
    bag = _Bag()
    untag = _missing_keys_predicate(tag_keys, _AGG_KEYS, bag)
    where = _where(days, workspaces, bag)
    return Query(f"""
WITH workload AS (
  SELECT product, workload_id,
         SUM(list_cost) AS cost,
         {untag} AS is_untagged
  FROM {SUMMARY_TABLE}
  WHERE {where}
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
""", bag.params)


def _rule_sql_predicate(rule, bag=None):
    """SQL boolean for one rule's match condition over the workload aggregate.

    Drives the ENQUEUE path, so over-matching here would tag MORE resources than
    intended. The `field` picks a column from a FIXED whitelist (never free text,
    so it can't be a parameter and doesn't need to be). The user `value`:
      - With a `bag`: bound as a parameter — no quote escaping needed. LIKE
        metacharacters (%/_/\\) in the value are still escaped INTO the parameter
        string (with ESCAPE '\\') so "data_eng"/"50%" match literally, not as
        globs. For `matches`, literal metachars are escaped first, then glob
        wildcards *→% and ?→_ are applied so they stay active.
      - Without a bag (legacy literal path): same logic via escaped SQL literals.
    """
    col = {"owner": "owner", "workspace": "workspace_id",
           "product": "product", "name": "workload_name"}[rule["field"]]
    c = f"lower(coalesce({col},''))"
    op = rule["op"]
    raw = rule["value"]
    if bag is not None:
        if op == "equals":
            return f"{c} = lower({bag.add(raw, 'rule')})"
        if op == "contains":
            return f"{c} LIKE lower({bag.add('%' + _escape_like(raw) + '%', 'rule')}) ESCAPE '\\\\'"
        if op == "matches":
            pat = _escape_like(raw).replace("*", "%").replace("?", "_")
            return f"{c} LIKE lower({bag.add(pat, 'rule')}) ESCAPE '\\\\'"
        return "false"
    # legacy literal path (kept for any un-migrated caller)
    val = raw.replace("'", "''")
    if op == "equals":
        return f"{c} = lower('{val}')"
    if op == "contains":
        return f"{c} LIKE lower('%{_escape_like(val)}%') ESCAPE '\\\\'"
    if op == "matches":
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
    bag = _Bag()
    preds = [_rule_sql_predicate(r, bag) for r in valid]  # once per rule (reused below)
    any_match = " OR ".join(f"({p})" for p in preds)
    # per-rule first-match coverage via a CASE priority ladder
    ladder = "\n".join(f"      WHEN {p} THEN {i}" for i, p in enumerate(preds))
    untag = _missing_keys_predicate(tag_keys, _AGG_KEYS, bag)
    where = _where(days, workspaces, bag)
    handled = _not_already_handled(tag_keys[0], "wl.product", "wl.workload_id", bag)
    return Query(f"""
WITH wl AS (
  SELECT workload_id,
         MAX(workspace_id)  AS workspace_id,
         MAX(workload_name) AS workload_name,
         MAX(owner)         AS owner,
         product,
         {untag} AS is_untagged,
         SUM(list_cost)     AS cost
  FROM {SUMMARY_TABLE}
  WHERE {where}
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
""", bag.params)


def bulk_rule_sample(days, tag_keys, rules, workspaces=None, limit=200):
    """The CONCRETE per-workload list a rule set would tag: name, product, owner,
    cost, AND the exact value each gets (from the first rule that matches). This is
    what a human reviews before applying, so they can catch a rule that lumps
    unrelated workloads under one team. Only taggable workloads (writer can't tag
    Apps/serverless/etc., so they'd never be enqueued — don't show them here)."""
    valid = [r for r in rules if r.get("value") and r.get("tags")]
    if not valid:
        return None
    bag = _Bag()
    preds = [_rule_sql_predicate(r, bag) for r in valid]
    any_match = " OR ".join(f"({p})" for p in preds)
    ladder = "\n".join(f"      WHEN {p} THEN {i}" for i, p in enumerate(preds))
    # map first_rule index -> the tag value it assigns for `tag_keys[0]`
    key = tag_keys[0]
    val_ladder = "\n".join(
        f"      WHEN {i} THEN {bag.add(r['tags'].get(key, ''), 'val')}"
        for i, r in enumerate(valid))
    untag = _missing_keys_predicate(tag_keys, _AGG_KEYS, bag)
    where = _where(days, workspaces, bag)
    handled = _not_already_handled(tag_keys[0], "wl.product", "wl.workload_id", bag)
    return Query(f"""
WITH wl AS (
  SELECT workload_id, product,
         MAX(workspace_id) AS workspace_id, MAX(workload_name) AS workload_name, MAX(owner) AS owner,
         MAX(is_serverless) AS is_serverless,
         MAX(usage_date) AS last_seen,
         {untag} AS is_untagged,
         SUM(list_cost) AS cost
  FROM {SUMMARY_TABLE}
  WHERE {where}
  GROUP BY product, workload_id
),
matched AS (
  SELECT workload_id, product, workspace_id, workload_name, owner,
         is_serverless, ROUND(cost,0) AS cost, last_seen,
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
       is_serverless, cost, last_seen,
       CASE first_rule
{val_ladder}
       END AS new_tag_value
FROM matched
WHERE first_rule >= 0
ORDER BY cost DESC
LIMIT {int(limit)}
""", bag.params)


def distinct_field_values(field, days, workspaces=None, limit=2000):
    """Distinct values of a rule field (owner/product/workspace/name) among
    untagged-relevant workloads, sorted alphabetically — to populate the
    rules-builder picker so a user selects a real owner instead of typing a raw
    email. Limit is high (a datalist is cheap) so alphabetical sort doesn't hide
    owners late in the alphabet."""
    col = {"owner": "owner", "product": "product",
           "workspace": "workspace_id", "name": "workload_name"}.get(field, "owner")
    bag = _Bag()
    where = _where(days, workspaces, bag)
    return Query(f"""
SELECT {col} AS value, ROUND(SUM(list_cost), 0) AS cost,
       COUNT(DISTINCT workload_id) AS workloads
FROM {SUMMARY_TABLE}
WHERE {where} AND {col} IS NOT NULL AND {col} <> ''
GROUP BY {col}
ORDER BY lower({col}) ASC
LIMIT {int(limit)}
""", bag.params)


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
    charset validation in app._validate_tag.

    NOTE: prefer bound parameters (_Bag) for any NEW user-input query — this
    literal path remains for the static queries not yet migrated. See db.Query.
    """
    s = str(v).replace("\\", "\\\\").replace("'", "''")
    s = s.replace("\x00", "").replace("\n", " ").replace("\r", " ")
    return "'" + s + "'"


class _Bag:
    """Collects NAMED bind parameters for one query and hands back their markers.

    The injection-safe alternative to _sql_str f-string interpolation: user values
    (tag keys/values, rule match text, workspace ids, day windows) are registered
    here and referenced in the SQL only as ':name' markers — the driver sends them
    as typed server-side parameters, so nothing the user typed is ever parsed as
    SQL. Column NAMES still can't be parameters (they're structural), but those are
    always chosen from fixed whitelists, never free text.

    Usage:
        bag = _Bag()
        sql = f"WHERE tag_key = {bag.add(tag_key)}"
        return Query(sql, bag.params)
    """
    def __init__(self):
        self.params: dict = {}
        self._n = 0

    def add(self, value, prefix="p") -> str:
        """Register one value; return its ':marker'."""
        name = f"{prefix}_{self._n}"
        self._n += 1
        self.params[name] = value
        return f":{name}"

    def add_all(self, values, prefix="p") -> list[str]:
        """Register a list of values; return a list of ':marker's."""
        return [self.add(v, prefix) for v in values]


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

    bag = _Bag()
    preds = [_rule_sql_predicate(r, bag) for r in valid]
    ladder = "\n".join(f"      WHEN {p} THEN {i}" for i, p in enumerate(preds))

    # Map each rule index -> its tags, expanded into (rule_idx, key, value) rows
    # so a join attaches the winning rule's tags to each workload. Keys/values are
    # bound parameters (user-supplied), not interpolated literals.
    tag_tuples = []
    for i, r in enumerate(valid):
        for k, v in r["tags"].items():
            tag_tuples.append(f"({i}, {bag.add(k, 'tk')}, {bag.add(v, 'tv')})")
    tags_values = ",\n    ".join(tag_tuples)

    untag = _missing_keys_predicate(tag_keys, _AGG_KEYS, bag)
    where = _where(days, workspaces, bag)
    return Query(f"""
INSERT INTO {QUEUE_TABLE}
  (batch_id, enqueued_at, requested_by, workspace_id, product, workload_id,
   workload_name, is_serverless, tag_key, tag_value, list_cost, status, attempts)
WITH wl AS (
  SELECT workload_id, product,
         MAX(workspace_id)  AS workspace_id,
         MAX(workload_name) AS workload_name,
         MAX(owner)         AS owner,
         MAX(is_serverless) AS is_serverless,
         {untag} AS is_untagged,
         SUM(list_cost)     AS cost
  FROM {SUMMARY_TABLE}
  WHERE {where}
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
  {bag.add(batch_id, 'batch')}      AS batch_id,
  current_timestamp()               AS enqueued_at,
  {bag.add(requested_by, 'by')}     AS requested_by,
  m.workspace_id, m.product, m.workload_id, m.workload_name, m.is_serverless,
  rt.tag_key, rt.tag_value,
  m.cost                    AS list_cost,
  'PENDING'                 AS status,
  0                         AS attempts
FROM matched m
JOIN ruletags rt ON rt.rule_idx = m.first_rule
WHERE m.first_rule >= 0
""", bag.params)


def enqueue_single_sql(batch_id, requested_by, workspace_id, product, workload_id,
                       workload_name, is_serverless, tags, list_cost=None):
    """INSERT queue rows for one workload with an explicit tag dict.

    list_cost is carried so single-enqueued rows drain cost-descending like bulk
    rows and show their real cost in the Batches tab (NULL → drained last, $0).
    """
    bag = _Bag()
    cost_sql = "NULL" if list_cost is None else f"CAST({float(list_cost)} AS DECIMAL(38,6))"
    rows = []
    for k, v in tags.items():
        rows.append(
            f"({bag.add(batch_id,'b')}, current_timestamp(), {bag.add(requested_by,'by')}, "
            f"{bag.add(workspace_id,'ws')}, {bag.add(product,'pr')}, {bag.add(workload_id,'wid')}, "
            f"{bag.add(workload_name or workload_id,'wn')}, {1 if is_serverless else 0}, "
            f"{bag.add(k,'tk')}, {bag.add(v,'tv')}, {cost_sql}, 'PENDING', 0)")
    values = ",\n  ".join(rows)
    return Query(f"""
INSERT INTO {QUEUE_TABLE}
  (batch_id, enqueued_at, requested_by, workspace_id, product, workload_id,
   workload_name, is_serverless, tag_key, tag_value, list_cost, status, attempts)
VALUES
  {values}
""", bag.params)


def enqueue_explicit_sql(batch_id, requested_by, tag_key, workloads):
    """INSERT one row per workload from an EXPLICIT, user-selected list.

    This is the "review the list, uncheck the ones that don't belong, apply only
    the rest" path — the frontend sends exactly the workloads the human confirmed,
    each with its own tag value. Unlike enqueue_bulk_sql it does NOT re-evaluate a
    rule, so unchecked rows are simply never inserted. Each workload dict:
      {workload_id, product, workspace_id, workload_name, is_serverless, tag_value, cost}
    Returns None if the list is empty.
    """
    bag = _Bag()
    rows = []
    for w in workloads:
        if not w.get("workload_id") or not w.get("tag_value"):
            continue
        cost = w.get("cost")
        cost_sql = "NULL" if cost in (None, "") else f"CAST({float(cost)} AS DECIMAL(38,6))"
        rows.append(
            f"({bag.add(batch_id,'b')}, current_timestamp(), {bag.add(requested_by,'by')}, "
            f"{bag.add(str(w.get('workspace_id') or ''),'ws')}, {bag.add(w['product'],'pr')}, "
            f"{bag.add(str(w['workload_id']),'wid')}, "
            f"{bag.add(w.get('workload_name') or str(w['workload_id']),'wn')}, "
            f"{1 if w.get('is_serverless') else 0}, "
            f"{bag.add(tag_key,'tk')}, {bag.add(w['tag_value'],'tv')}, {cost_sql}, 'PENDING', 0)")
    if not rows:
        return None
    values = ",\n  ".join(rows)
    return Query(f"""
INSERT INTO {QUEUE_TABLE}
  (batch_id, enqueued_at, requested_by, workspace_id, product, workload_id,
   workload_name, is_serverless, tag_key, tag_value, list_cost, status, attempts)
VALUES
  {values}
""", bag.params)


def count_queue_rows(batch_id):
    """Ground-truth row count for a batch — the app counts rows this way instead
    of trusting DML rowcount (the SQL connector reports -1 for INSERTs)."""
    bag = _Bag()
    return Query(f"SELECT COUNT(*) AS n FROM {QUEUE_TABLE} WHERE batch_id = {bag.add(batch_id,'b')}",
                 bag.params)


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
    bag = _Bag()
    return Query(f"""
SELECT product,
       status,
       {reason_norm}            AS reason,
       COUNT(*)                 AS rows,
       ROUND(SUM(list_cost), 0) AS cost,
       MIN(last_error)          AS sample_reason
FROM {QUEUE_TABLE}
WHERE batch_id = {bag.add(batch_id,'b')}
  AND status IN ('FAILED', 'UNSUPPORTED')
GROUP BY product, status, {reason_norm}
ORDER BY rows DESC
LIMIT {int(limit)}
""", bag.params)


# ============================================================================
# Advisory agent suggestions (ai_query hints) — read-only join for the UI
# ============================================================================

# ----------------------------------------------------------------------------
# Bulk tag FROM AI suggestions (threshold + one-click). The agent already
# proposed a cost-center per workload (annotate.sql); this lets a human enqueue
# ALL confident suggestions at once instead of typing thousands of values. The
# human still gates the batch (picks the key + confidence cutoff, reviews the
# count/cost, clicks once). A workload qualifies when it is untagged for the
# chosen key, has a real (non-"unknown") suggestion, and confidence >= cutoff.
# ----------------------------------------------------------------------------

def _suggestion_candidates_cte(tag_key, min_confidence, days, workspaces, bag):
    """Shared CTE: untagged workloads with a confident, real suggestion.

    Appends its bind params to the caller-supplied `bag` (so the caller can add
    its own params for the SELECT tail and pass one merged dict to the driver).
    Returns just the SQL text (the `WITH … cand AS (…)` prefix)."""
    # A workload is "untagged for this key" if the chosen key is absent.
    untag_expr = _missing_keys_predicate([tag_key], _AGG_KEYS, bag)
    where = _where(days, workspaces, bag)
    handled = _not_already_handled(tag_key, "wl.product", "wl.workload_id", bag)
    conf = bag.add(_conf_floor(min_confidence), "conf")
    return f"""
WITH wl AS (
  SELECT product, workload_id,
         MAX(workspace_id)  AS workspace_id,
         MAX(workload_name) AS workload_name,
         MAX(is_serverless) AS is_serverless,
         MAX(usage_date)    AS last_seen,
         SUM(list_cost)     AS cost
  FROM {SUMMARY_TABLE}
  WHERE {where}
  GROUP BY product, workload_id
  HAVING {untag_expr} AND SUM(list_cost) > 0
),
cand AS (
  SELECT wl.product, wl.workload_id, wl.workspace_id, wl.workload_name,
         wl.is_serverless, wl.cost, wl.last_seen,
         s.suggested_cost_center, s.confidence
  FROM wl
  JOIN {SUGGESTIONS_TABLE} s
    ON s.workload_id = wl.workload_id AND s.product = wl.product
  WHERE s.suggested_cost_center IS NOT NULL
    AND lower(s.suggested_cost_center) <> 'unknown'
    AND s.confidence >= {conf}
    -- Only surface workloads the writer can actually tag; policy-governed
    -- products (Apps, serverless SQL, …) would be enqueued then skipped
    -- UNSUPPORTED and reappear next time. Filter them out here.
    AND {_taggable_predicate('wl.product', 'wl.is_serverless')}
    -- Don't double-tag: skip workloads already tagged/in-flight for this key.
    -- The summary snapshot lags a live write by up to a day, so check the queue.
    AND {handled}
)"""


def suggestions_bulk_impact(tag_key, min_confidence, days, workspaces=None):
    """Aggregate impact of enqueuing all confident suggestions for `tag_key`."""
    bag = _Bag()
    cte = _suggestion_candidates_cte(tag_key, min_confidence, days, workspaces, bag)
    return Query(cte + """
SELECT COUNT(*)                              AS workloads,
       ROUND(SUM(cost), 0)                   AS cost,
       COUNT(DISTINCT suggested_cost_center) AS distinct_values
FROM cand
""", bag.params)


def policy_governed_impact(tag_key, min_confidence, days, workspaces=None):
    """Untagged workloads that have a confident suggestion but CANNOT be tagged by
    the writer (Apps, serverless SQL, …). Surfaced so the UI can explain why they
    aren't in the apply list instead of silently dropping them — they need a
    budget/tag policy, not a per-resource write."""
    bag = _Bag()
    untag_expr = _missing_keys_predicate([tag_key], _AGG_KEYS, bag)
    where = _where(days, workspaces, bag)
    conf = bag.add(_conf_floor(min_confidence), "conf")
    return Query(f"""
WITH wl AS (
  SELECT product, workload_id,
         MAX(is_serverless) AS is_serverless,
         SUM(list_cost)     AS cost
  FROM {SUMMARY_TABLE}
  WHERE {where}
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
  AND s.confidence >= {conf}
  AND NOT {_taggable_predicate('wl.product', 'wl.is_serverless')}
""", bag.params)


def suggestions_bulk_list(tag_key, min_confidence, days, workspaces=None, limit=100):
    """The CONCRETE per-workload list: exactly which workloads get tagged and with
    what value. This is what a human needs to see before clicking apply — one row
    per workload, highest-cost first, showing the tag value the AI would set."""
    bag = _Bag()
    cte = _suggestion_candidates_cte(tag_key, min_confidence, days, workspaces, bag)
    return Query(cte + f"""
SELECT product,
       workload_id,
       workspace_id,
       is_serverless,
       workload_name,
       ROUND(cost, 0)            AS cost,
       last_seen,
       suggested_cost_center     AS new_tag_value,
       ROUND(confidence, 2)      AS confidence
FROM cand
ORDER BY cost DESC
LIMIT {int(limit)}
""", bag.params)


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
    bag = _Bag()
    cte = _suggestion_candidates_cte(tag_key, min_confidence, days, workspaces, bag)
    exclude = ""
    if exclude_batch_id:
        exclude = f"""
  AND NOT EXISTS (
    SELECT 1 FROM {QUEUE_TABLE} q
    WHERE q.batch_id = {bag.add(exclude_batch_id, 'excl')}
      AND q.tag_key = {bag.add(tag_key, 'key')}
      AND q.workload_id = cand.workload_id
      AND q.product = cand.product
  )"""
    return Query(f"""
INSERT INTO {QUEUE_TABLE}
  (batch_id, enqueued_at, requested_by, workspace_id, product, workload_id,
   workload_name, is_serverless, tag_key, tag_value, list_cost, status, attempts)
{cte}
SELECT
  {bag.add(batch_id, 'batch')}      AS batch_id,
  current_timestamp()               AS enqueued_at,
  {bag.add(requested_by, 'by')}     AS requested_by,
  workspace_id, product, workload_id, workload_name, is_serverless,
  {bag.add(tag_key, 'key')}         AS tag_key,
  suggested_cost_center             AS tag_value,
  cost                              AS list_cost,
  'PENDING'                         AS status,
  0                                 AS attempts
FROM cand
WHERE 1=1{exclude}
""", bag.params)


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
