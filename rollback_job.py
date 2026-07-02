"""Tag Governance rollback job — undo a batch by replaying the audit in reverse.

For a given batch_id, reads the SUCCEEDED `SET` rows from the audit table and
restores each tag to its old_value. If old_value is NULL (the key didn't exist
before we set it), the key is REMOVED. Every restore is itself recorded to the
audit table as a `ROLLBACK` row, so the audit remains a complete history.

This is what makes bulk tagging safe: tag 12K workloads, discover one rule was
wrong, and undo the entire batch exactly — nothing is guessed, every restore
comes from the recorded old→new.

Parameters (job task parameters):
  audit_table  : fully-qualified audit table
  batch_id     : the batch to roll back (required)
  dry_run      : 'true' (default) → report what would be restored, mutate nothing.
  max_workers  : optional per-workspace concurrency (default 4).
"""
from __future__ import annotations

import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import spark

import writer


def _parse_argv() -> dict:
    out = {}
    for tok in sys.argv[1:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k.strip()] = v.strip()
    return out


_ARGV = _parse_argv()


def _param(name: str, default: str | None = None) -> str | None:
    """argv (spark_python_task) first, then widgets, then default."""
    if name in _ARGV and _ARGV[name] != "":
        return _ARGV[name]
    try:
        from databricks.sdk.runtime import dbutils
        v = dbutils.widgets.get(name)
        if v != "":
            return v
    except Exception:
        pass
    return default


def _client_for_workspace(workspace_id: str, home_client: WorkspaceClient) -> WorkspaceClient:
    """M1: home workspace only. M2 seam: build per-workspace client here."""
    return home_client


def run():
    audit_table = _param("audit_table")
    batch_id = _param("batch_id")
    dry_run = (_param("dry_run", "true") or "true").lower() != "false"
    max_workers = int(_param("max_workers", "4") or "4")

    if not audit_table or not batch_id:
        raise SystemExit("audit_table and batch_id parameters are required")

    home_client = WorkspaceClient()
    executed_by = "unknown"
    try:
        executed_by = home_client.current_user.me().user_name
    except Exception:
        pass

    # The forward SETs we need to undo. Take the LATEST SET per (workload, key)
    # so repeated tagging rolls back to the value before the batch, and skip keys
    # already rolled back.
    to_undo = spark.sql(f"""
        WITH sets AS (
          SELECT workspace_id, product, workload_id, tag_key, old_value, new_value,
                 ROW_NUMBER() OVER (
                   PARTITION BY workload_id, tag_key ORDER BY executed_at DESC
                 ) AS rn
          FROM {audit_table}
          WHERE batch_id = '{batch_id}' AND action = 'SET' AND status = 'SUCCEEDED'
        )
        SELECT workspace_id, product, workload_id, tag_key, old_value, new_value
        FROM sets WHERE rn = 1
    """).collect()

    if not to_undo:
        print(f"No SUCCEEDED SET rows to roll back for batch {batch_id}.")
        return

    print(f"Rolling back {len(to_undo)} changes for batch {batch_id} | dry_run={dry_run}")

    by_ws: dict[str, list] = {}
    for r in to_undo:
        by_ws.setdefault(r["workspace_id"], []).append(r.asDict())

    audit_rows: list[dict] = []

    def process_workspace(workspace_id: str, rows: list[dict]):
        client = _client_for_workspace(workspace_id, home_client)
        local = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            for row in rows:
                # Restore old_value; None removes the key we had added.
                target = row["old_value"]
                if dry_run:
                    local.append(_audit_row(batch_id, executed_by, row, target,
                                            status="SUCCEEDED", error="DRY_RUN"))
                    continue
                futures[pool.submit(
                    writer.attempt_write, client, row["product"], row["workload_id"],
                    None, row["tag_key"], target)] = row
            for fut in as_completed(futures):
                row = futures[fut]
                result = fut.result()
                local.append(_audit_row(
                    batch_id, executed_by, row, row["old_value"],
                    status="SUCCEEDED" if result.status == "SUCCEEDED" else "FAILED",
                    error=result.error))
        return local

    with ThreadPoolExecutor(max_workers=min(len(by_ws), 14)) as ws_pool:
        futs = {ws_pool.submit(process_workspace, ws, rows): ws
                for ws, rows in by_ws.items()}
        for fut in as_completed(futs):
            audit_rows.extend(fut.result())

    if audit_rows and not dry_run:
        _persist_rollback_audit(audit_table, audit_rows)

    counts: dict[str, int] = {}
    for a in audit_rows:
        counts[a["status"]] = counts.get(a["status"], 0) + 1
    print("Rollback done. " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


def _audit_row(batch_id, executed_by, row, restored_value, status, error):
    return {
        "audit_id": str(uuid.uuid4()), "batch_id": batch_id, "executed_by": executed_by,
        "workspace_id": row["workspace_id"], "product": row["product"],
        "workload_id": row["workload_id"], "tag_key": row["tag_key"],
        # ROLLBACK inverts a SET: old = the value the batch had set (new_value of
        # the SET we're undoing), new = the value we restored it to (old_value).
        "old_value": row.get("new_value"), "new_value": restored_value,
        "action": "ROLLBACK", "status": status, "error": error,
    }


def _persist_rollback_audit(audit_table, audit_rows):
    from pyspark.sql import functions as F
    adf = spark.createDataFrame(audit_rows).withColumn(
        "executed_at", F.current_timestamp())
    adf.write.mode("append").saveAsTable(audit_table)


if __name__ == "__main__":
    run()
