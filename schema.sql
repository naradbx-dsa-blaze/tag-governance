-- Tag Governance — write-queue + audit schema.
--
-- The app never mutates a resource directly. It records write INTENT into the
-- queue table; a separate writer job drains the queue, performs the SDK writes,
-- and records every change (old -> new) into the audit table. Rollback replays
-- the audit in reverse. This makes writes safe, resumable, and reversible.
--
-- Table names come from job/session parameters (set from bundle vars) so the
-- same script deploys into any account:
--   SET VAR queue_table = '<catalog.schema.tag_governance_write_queue>';
--   SET VAR audit_table = '<catalog.schema.tag_governance_audit>';
-- CREATE TABLE IF NOT EXISTS is used so re-running the schema never drops data.

-- ---------------------------------------------------------------------------
-- Write queue: one row per (workload, tag_key) the user asked to set. A batch_id
-- groups everything enqueued in one action so it can be tracked and rolled back
-- as a unit. status walks PENDING -> RUNNING -> SUCCEEDED / FAILED / UNSUPPORTED
-- / SKIPPED_DRYRUN. list_cost is carried so the writer can drain cost-descending.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS IDENTIFIER(:queue_table) (
  batch_id       STRING    NOT NULL COMMENT 'Groups one enqueue action; the rollback unit',
  enqueued_at    TIMESTAMP NOT NULL COMMENT 'When the intent was recorded',
  requested_by   STRING             COMMENT 'App user (or SP) that enqueued this',
  workspace_id   STRING    NOT NULL COMMENT 'Workspace the resource lives in (write target)',
  product        STRING    NOT NULL COMMENT 'billing_origin_product — selects the write API',
  workload_id    STRING    NOT NULL COMMENT 'Concrete resource id (job_id/warehouse_id/cluster_id/endpoint_id/...)',
  workload_name  STRING             COMMENT 'Human-readable name for display',
  is_serverless  INT                COMMENT '1 if serverless — selects policy-governed handling (e.g. serverless SQL)',
  tag_key        STRING    NOT NULL COMMENT 'Tag key to set',
  tag_value      STRING    NOT NULL COMMENT 'Tag value to set',
  list_cost      DECIMAL(38,6)      COMMENT '30d list cost of the workload — drain order',
  status         STRING    NOT NULL COMMENT 'PENDING|RUNNING|SUCCEEDED|FAILED|UNSUPPORTED|SKIPPED_DRYRUN',
  attempts       INT                COMMENT 'Number of write attempts so far',
  last_error     STRING             COMMENT 'Last failure reason, if any',
  executed_at    TIMESTAMP          COMMENT 'When the writer last acted on this row'
)
USING DELTA
COMMENT 'Tag Governance write-intent queue — drained by the tag-governance-writer job';

-- ---------------------------------------------------------------------------
-- Audit: an immutable record of every tag change the writer actually made,
-- capturing old_value -> new_value so a batch can be rolled back exactly.
-- action = SET for forward writes, ROLLBACK for restores.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS IDENTIFIER(:audit_table) (
  audit_id     STRING    NOT NULL COMMENT 'Unique id for this audit record',
  batch_id     STRING    NOT NULL COMMENT 'The batch this change belongs to',
  executed_at  TIMESTAMP NOT NULL COMMENT 'When the change was applied',
  executed_by  STRING             COMMENT 'Identity that performed the write (SP/user)',
  workspace_id STRING    NOT NULL COMMENT 'Workspace the resource lives in',
  product      STRING    NOT NULL COMMENT 'billing_origin_product',
  workload_id  STRING    NOT NULL COMMENT 'Concrete resource id',
  tag_key      STRING    NOT NULL COMMENT 'Tag key changed',
  old_value    STRING             COMMENT 'Value before the change (NULL if key was absent)',
  new_value    STRING             COMMENT 'Value after the change (NULL if key was removed)',
  action       STRING    NOT NULL COMMENT 'SET | ROLLBACK',
  status       STRING    NOT NULL COMMENT 'SUCCEEDED | FAILED',
  error        STRING             COMMENT 'Failure reason, if any'
)
USING DELTA
COMMENT 'Tag Governance audit log — old->new for every applied change, enables rollback';
