-- Tag Governance — pre-aggregated workload summary.
-- Collapses system.billing.usage (millions of rows) into one row per
-- (usage_date, product, workload_id), with cost pre-joined to current list price
-- and the SET OF TAG KEYS present on the workload. The app filters this small
-- table by date window and computes "missing my chargeback keys" via array logic,
-- so the UI loads in sub-second time instead of scanning the raw table.
--
-- Refreshed daily by the tag-governance-refresh job. Window = 90 days to cover
-- the app's max lookback slider.
--
-- The target table name comes from the :summary_table job parameter (set from
-- the bundle var), so the same script deploys into any account. When run ad-hoc
-- without the parameter, set it via: SET VAR summary_table = '<catalog.schema.table>';
CREATE OR REPLACE TABLE IDENTIFIER(:summary_table) AS
WITH base AS (
  SELECT
    usage_date,
    workspace_id,
    billing_origin_product AS product,
    coalesce(
      usage_metadata.job_id, usage_metadata.warehouse_id, usage_metadata.dlt_pipeline_id,
      usage_metadata.endpoint_id, usage_metadata.app_id, usage_metadata.index_id,
      usage_metadata.database_instance_id, usage_metadata.cluster_id
    ) AS workload_id,
    coalesce(
      usage_metadata.job_name, usage_metadata.endpoint_name, usage_metadata.app_name,
      usage_metadata.run_name, usage_metadata.dlt_pipeline_id, usage_metadata.warehouse_id,
      usage_metadata.cluster_id, usage_metadata.database_instance_id
    ) AS workload_name,
    coalesce(identity_metadata.owned_by, identity_metadata.run_as, identity_metadata.created_by) AS owner,
    product_features.is_serverless AS is_serverless,
    sku_name,
    usage_quantity AS dbus,
    map_keys(custom_tags) AS tag_keys
  FROM system.billing.usage
  WHERE usage_date >= current_date() - INTERVAL 90 DAYS
    AND usage_unit = 'DBU'
),
price AS (
  SELECT sku_name, pricing.default AS rate
  FROM system.billing.list_prices
  WHERE price_end_time IS NULL
)
SELECT
  b.usage_date,
  b.workspace_id,
  b.product,
  b.workload_id,
  MAX(b.workload_name)                                   AS workload_name,
  MAX(b.owner)                                           AS owner,
  MAX(CASE WHEN b.is_serverless THEN 1 ELSE 0 END)       AS is_serverless,
  array_distinct(flatten(collect_list(b.tag_keys)))      AS tag_keys,
  SUM(b.dbus)                                            AS dbus,
  SUM(b.dbus * COALESCE(p.rate, 0))                      AS list_cost
FROM base b
LEFT JOIN price p ON b.sku_name = p.sku_name
WHERE b.workload_id IS NOT NULL
GROUP BY b.usage_date, b.workspace_id, b.product, b.workload_id
