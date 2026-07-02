-- Tag Governance — advisory ai_query annotation (the "agent").
--
-- Classifies each untagged workload with a SUGGESTED cost-center, a confidence,
-- and a one-line rationale, using only the signals we already have: workload
-- name, owner, product, workspace. This is where hand-written rules can't reach —
-- the ~28% of workloads with a null owner and ~30% with a null name.
--
-- ADVISORY ONLY. Nothing here writes a tag or enqueues anything. The app LEFT
-- JOINs this table (queries.suggestions_join) to show the suggestion as a hint
-- next to each workload; a human still authors the actual rules. The agent
-- decides what a tag COULD be, never what it IS.
--
-- One warehouse-side pass over the fleet (~pennies), so it scales to 50k
-- workloads without any serving endpoint or eval infra. Refresh on demand or
-- alongside the daily materialize job.
--
-- Parameters:
--   summary_table     : the pre-aggregated workload table (read)
--   suggestions_table : where suggestions are written (CREATE OR REPLACE)
--   annotate_model    : the ai_query model/endpoint name
-- Ad-hoc: SET VAR summary_table='...'; SET VAR suggestions_table='...'; SET VAR annotate_model='databricks-meta-llama-3-3-70b-instruct';

CREATE OR REPLACE TABLE IDENTIFIER(:suggestions_table) AS
WITH wl AS (
  -- One row per workload (collapse the daily rows), keeping the signals the
  -- classifier reasons over. Only untagged-relevant workloads with real cost.
  SELECT
    product,
    workload_id,
    MAX(workload_name) AS workload_name,
    MAX(owner)         AS owner,
    MAX(workspace_id)  AS workspace_id,
    SUM(list_cost)     AS cost
  FROM IDENTIFIER(:summary_table)
  WHERE usage_date >= current_date() - INTERVAL 30 DAYS
  GROUP BY product, workload_id
  HAVING SUM(list_cost) > 0
),
scored AS (
  SELECT
    product, workload_id, workload_name, owner, workspace_id, cost,
    ai_query(
      :annotate_model,
      CONCAT(
        'You assign a cost-center (owning team) to a Databricks workload for ',
        'internal chargeback. Use the signals; if they are weak, say "unknown" ',
        'with low confidence rather than guessing. ',
        'Workload name: ', COALESCE(workload_name, '(none)'),
        '; owner: ', COALESCE(owner, '(none)'),
        '; product: ', product,
        '. Pick a short lowercase team slug (e.g. data-eng, data-science, ',
        'marketing, finance, platform, sales-eng) or "unknown".'
      ),
      responseFormat =>
        '{"type": "json_schema", "json_schema": {"name": "cc", "schema": {"type": "object", "properties": {"cost_center": {"type": "string"}, "confidence": {"type": "number"}, "rationale": {"type": "string"}}, "required": ["cost_center", "confidence", "rationale"]}}}'
    ) AS raw
  FROM wl
)
SELECT
  product,
  workload_id,
  workload_name,
  owner,
  workspace_id,
  cost                                                AS list_cost,
  from_json(raw, 'STRUCT<cost_center:STRING, confidence:DOUBLE, rationale:STRING>').cost_center AS suggested_cost_center,
  from_json(raw, 'STRUCT<cost_center:STRING, confidence:DOUBLE, rationale:STRING>').confidence  AS confidence,
  from_json(raw, 'STRUCT<cost_center:STRING, confidence:DOUBLE, rationale:STRING>').rationale   AS rationale,
  current_timestamp()                                 AS annotated_at
FROM scored;
