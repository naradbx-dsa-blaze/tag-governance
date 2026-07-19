"""Tag Governance writer job — drains the write queue and applies tags.

Runs as a Databricks job (NOT in the app). Reads PENDING rows from the queue
COST-DESCENDING (so the largest spend is attributed first), groups them by
workspace, and applies each tag via writer.attempt_write, recording every change
to the audit table. Safe to re-run: SUCCEEDED/idempotent rows are skipped and
each write is a no-op if the tag already matches.

Scaling:
  - Per-workspace grouping + a bounded thread pool per workspace, so the 14
    workspaces proceed in parallel without any single one exceeding its API
    rate limit.
  - Exponential backoff on HTTP 429 / rate-limit responses.
  - Resumable: a crash leaves rows PENDING; the next run drains the rest.

Parameters (job task parameters):
  queue_table  : fully-qualified queue table
  audit_table  : fully-qualified audit table
  dry_run      : 'true' (default) → mark rows SKIPPED_DRYRUN, write intended
                 old→new to audit, mutate nothing. 'false' → live writes.
  workspace_id : optional — restrict to one workspace (M1 home-workspace mode).
  max_workers  : optional per-workspace concurrency (default 4).
  batch_id     : optional — restrict to a single batch.
"""
from __future__ import annotations

import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import spark  # available in the job runtime

import writer
import ws_clients

# Backoff for rate limits (HTTP 429 / "rate limit" in the message).
_MAX_RETRIES = 5
_BASE_DELAY = 2.0  # seconds; doubled each retry

# Flush status back to the queue every N completed rows so the app's 2s poll
# shows the bar filling/draining live. Small enough to feel real-time on the
# demo (a few dozen rows), large enough that each MERGE isn't a per-row round-trip.
PERSIST_CHUNK = 25


def _parse_argv() -> dict:
    """spark_python_task passes params as argv 'key=value' tokens."""
    out = {}
    for tok in sys.argv[1:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k.strip()] = v.strip()
    return out


_ARGV = _parse_argv()


def _param(name: str, default: str | None = None) -> str | None:
    """Read a job parameter: argv (spark_python_task) first, then widgets, then default."""
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


def _is_rate_limited(result: writer.WriteResult) -> bool:
    if result.status != "FAILED" or not result.error:
        return False
    e = result.error.lower()
    return "429" in e or "rate limit" in e or "too many requests" in e


def _apply_row(client, row, dry_run: bool) -> writer.WriteResult:
    """Attempt one row with backoff on rate limiting.

    dry_run is threaded all the way into the per-product writer, which reads the
    resource and computes old→new but performs NO mutation when dry_run is True.
    """
    def once():
        return writer.attempt_write(
            client, row["product"], row["workload_id"], row["workload_name"],
            row["tag_key"], row["tag_value"],
            is_serverless=bool(row.get("is_serverless")), dry_run=dry_run,
        )
    result = once()
    retries, delay = 0, _BASE_DELAY
    while _is_rate_limited(result) and retries < _MAX_RETRIES:
        time.sleep(delay)
        delay *= 2
        retries += 1
        result = once()
    return result


def _status_for(result: writer.WriteResult) -> str:
    """Map a WriteResult to a queue status. dry_run is carried on the result
    itself (the writer set it and did NOT mutate), so a would-be success becomes
    SKIPPED_DRYRUN while FAILED/UNSUPPORTED pass through unchanged."""
    if result.status == "SUCCEEDED" and result.dry_run:
        return "SKIPPED_DRYRUN"
    return result.status


def run():
    queue_table = _param("queue_table")
    audit_table = _param("audit_table")
    dry_run = (_param("dry_run", "true") or "true").lower() != "false"
    only_workspace = _param("workspace_id") or None
    only_batch = _param("batch_id") or None
    max_workers = int(_param("max_workers", "4") or "4")
    # smoke_test=true: prove the account SP can REACH each workspace with PENDING
    # rows (resolve a client + a cheap read), writing/enqueuing NOTHING. Lets an
    # admin validate M2 setup before any real batch. Implies no mutation.
    smoke_test = (_param("smoke_test", "false") or "false").lower() == "true"

    if not queue_table or not audit_table:
        raise SystemExit("queue_table and audit_table parameters are required")

    executed_by = "unknown"
    home_client = WorkspaceClient()
    try:
        executed_by = home_client.current_user.me().user_name
    except Exception:
        pass

    home_ws = None
    try:
        home_ws = str(home_client.get_workspace_id())
    except Exception:
        pass
    # Load account-SP creds from the secret scope (if provided) to enable M2.
    # If a scope was explicitly configured but the creds don't load, FAIL LOUD —
    # silently downgrading to M1 would tag only the home workspace and report
    # success, leaving the other workspaces' rows silently untouched.
    scope = _param("account_sp_scope") or ""
    if scope:
        from databricks.sdk.runtime import dbutils
        if not ws_clients.load_account_creds_from_scope(scope, dbutils):
            raise SystemExit(
                f"account_sp_scope='{scope}' was set but its account-SP creds could not "
                f"be loaded (missing scope or one of keys account_id/client_id/"
                f"client_secret). Refusing to silently downgrade to home-only. Fix the "
                f"scope, or clear account_sp_scope to run M1 intentionally."
            )
    resolver = ws_clients.ClientResolver(home_client, home_workspace_id=home_ws)

    # Scope the drain:
    #  - explicit workspace_id → just that workspace.
    #  - M2 (account creds present) and no workspace_id → drain ALL workspaces.
    #  - M1 (no account creds) and no workspace_id → default to home only, because
    #    the resolver can't reach foreign workspaces and must not mis-tag with the
    #    home client.
    if not only_workspace and not resolver.cross_workspace:
        if not home_ws:
            raise SystemExit(
                "workspace_id not provided, home workspace id could not be resolved, "
                "and no account-SP creds are set; refusing to run (M1 safety)."
            )
        only_workspace = home_ws
        print(f"M1 (no account creds) — defaulting to home workspace {only_workspace}.")

    def _q(v: str) -> str:  # escape single quotes for inline SQL
        return v.replace("'", "''")

    def _sql_lit(v: str) -> str:  # quoted SQL string literal
        return "'" + _q(v) + "'"

    where = ["status = 'PENDING'"]
    if only_workspace:
        where.append(f"workspace_id = '{_q(only_workspace)}'")
    if only_batch:
        where.append(f"batch_id = '{_q(only_batch)}'")
    where_sql = " AND ".join(where)

    # ATOMIC CLAIM: flip the rows we intend to drain from PENDING → RUNNING, stamped
    # with a token unique to THIS run, in a single MERGE/UPDATE. Then we only process
    # rows carrying our token. Two writer runs overlapping on the same batch each
    # claim a DISJOINT set (a row can only transition out of PENDING once), so
    # neither double-tags nor double-drains. Smoke test skips claiming (reads only).
    if not smoke_test:
        # Reclaim STALE RUNNING rows first: if an earlier run crashed mid-drain, its
        # rows are stuck RUNNING. Anything RUNNING for >30 min is presumed dead and
        # eligible to be re-claimed (idempotent writer makes re-processing safe).
        reclaim = where_sql.replace(
            "status = 'PENDING'",
            "(status = 'PENDING' OR (status = 'RUNNING' AND "
            "executed_at < current_timestamp() - INTERVAL 30 MINUTES))")
        run_token = f"run-{uuid.uuid4().hex[:16]}"

        def _claim():
            spark.sql(
                f"UPDATE {queue_table} SET status='RUNNING', last_error={_sql_lit(run_token)}, "
                f"executed_at=current_timestamp() WHERE {reclaim}")
            return spark.sql(
                f"SELECT * FROM {queue_table} WHERE status='RUNNING' "
                f"AND last_error={_sql_lit(run_token)} ORDER BY list_cost DESC NULLS LAST"
            ).collect()

        pending = _claim()
        # RACE GUARD: when the app triggers this job right after enqueuing a specific
        # batch, the INSERT may not yet be visible to this Spark session, so the
        # first claim finds nothing. Rather than falsely report "nothing to do" and
        # leave the batch stuck PENDING, retry a few times for an EXPLICIT batch_id.
        # (No retry for a full/scheduled drain — there, empty genuinely means empty.)
        if not pending and only_batch:
            for attempt in range(1, 6):  # ~ up to ~30s
                print(f"batch {only_batch}: 0 rows claimed yet, retrying "
                      f"({attempt}/5) in case the enqueue isn't visible…")
                time.sleep(5)
                pending = _claim()
                if pending:
                    break
    else:
        pending = spark.sql(
            f"SELECT * FROM {queue_table} WHERE {where_sql} "
            f"ORDER BY list_cost DESC NULLS LAST"
        ).collect()

    if not pending:
        print("No PENDING rows to process.")
        return

    # SMOKE TEST: prove reachability per workspace, mutate nothing. For each
    # distinct workspace with PENDING rows, try to resolve a client and do a cheap
    # read (current_user). Reports REACHABLE / UNREACHABLE per workspace so an
    # admin can confirm the account SP is entitled everywhere before a real run.
    if smoke_test:
        distinct_ws = sorted({str(r["workspace_id"]) for r in pending})
        print(f"SMOKE TEST — checking {len(distinct_ws)} workspace(s), no writes. "
              f"mode={'M2 (account SP)' if resolver.cross_workspace else 'M1 (home only)'}")
        ok = 0
        for wid in distinct_ws:
            try:
                cl = resolver.client_for(wid)
                who = cl.current_user.me().user_name  # cheap read proves the token works
                print(f"  ✓ REACHABLE  workspace {wid} as {who}")
                ok += 1
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ UNREACHABLE workspace {wid}: {e}")
        print(f"SMOKE TEST done: {ok}/{len(distinct_ws)} workspace(s) reachable. "
              f"Nothing was written.")
        return

    print(f"Draining {len(pending)} rows | dry_run={dry_run} | "
          f"workspace={only_workspace or 'ALL'} | workers/ws={max_workers}")

    # Group by workspace so each workspace runs in its own bounded pool.
    by_ws: dict[str, list] = {}
    for r in pending:
        by_ws.setdefault(r["workspace_id"], []).append(r.asDict())

    audit_rows: list[dict] = []
    queue_updates: list[dict] = []  # (batch_id, workload_id, tag_key) -> status/error

    def process_workspace(workspace_id: str, rows: list[dict], collect):
        # Resolve the workspace client once per workspace. If it can't be built
        # (M1 foreign workspace, or SP not assigned there), fail this workspace's
        # rows cleanly instead of crashing the run or mis-tagging via home.
        try:
            client = resolver.client_for(workspace_id)
        except Exception as e:  # noqa: BLE001
            reason = f"no client for workspace {workspace_id}: {e}"
            collect([], [{
                "batch_id": r["batch_id"], "workload_id": r["workload_id"],
                "product": r["product"], "tag_key": r["tag_key"],
                "status": "FAILED", "last_error": reason,
            } for r in rows])
            return
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_apply_row, client, row, dry_run): row for row in rows}
            for fut in as_completed(futures):
                row = futures[fut]
                result = fut.result()
                status = _status_for(result)
                update = {
                    "batch_id": row["batch_id"], "workload_id": row["workload_id"],
                    "product": row["product"], "tag_key": row["tag_key"],
                    "status": status, "last_error": result.error,
                }
                # Audit only ACTUAL changes: a live SET that ran, or a real FAILURE.
                # Dry-run "would-be" writes (result.dry_run) are not persisted —
                # the audit table is a truthful record of what happened, not intent.
                real_change = (result.status == "SUCCEEDED" and not result.dry_run and not result.noop)
                audit = []
                if real_change or result.status == "FAILED":
                    audit.append({
                        "audit_id": str(uuid.uuid4()), "batch_id": row["batch_id"],
                        "executed_by": executed_by, "workspace_id": workspace_id,
                        "product": row["product"], "workload_id": row["workload_id"],
                        "tag_key": row["tag_key"], "old_value": result.old_value,
                        "new_value": (result.new_value if real_change else None),
                        "action": "SET",
                        "status": "SUCCEEDED" if real_change else "FAILED",
                        "error": result.error,
                    })
                # Stream each row's result out immediately so it can be flushed to
                # the queue in chunks (live progress) rather than buffered to the end.
                collect(audit, [update])

    # Persist status back to the queue INCREMENTALLY (in chunks) as rows finish,
    # not once at the very end. The app polls the queue every 2s, so flushing in
    # chunks makes the progress bar fill/drain in near-real-time instead of jumping
    # from 0% to 100% when the whole job returns. This is the SAME writer job for
    # AI, rule, and manual tagging (all three enqueue into one queue), so every mode
    # gets live progress. A lock serializes the MERGEs so concurrent workspace
    # threads can't hit a Delta ConcurrentAppend conflict on the queue table.
    persist_lock = Lock()
    pending_audit: list[dict] = []
    pending_updates: list[dict] = []

    def flush(force: bool = False):
        nonlocal pending_audit, pending_updates
        with persist_lock:
            if not force and len(pending_updates) < PERSIST_CHUNK:
                return
            a, u = pending_audit, pending_updates
            pending_audit, pending_updates = [], []
            if a or u:
                # _persist signature is (queue_table, audit_table, queue_updates,
                # audit_rows, dry_run) — updates 3rd, audit 4th. Keep this order.
                _persist(queue_table, audit_table, u, a, dry_run)

    def collect(a: list[dict], u: list[dict]):
        with persist_lock:
            pending_audit.extend(a)
            pending_updates.extend(u)
            ready = len(pending_updates) >= PERSIST_CHUNK
        audit_rows.extend(a)
        queue_updates.extend(u)
        if ready:
            flush()

    with ThreadPoolExecutor(max_workers=min(len(by_ws), 14)) as ws_pool:
        ws_futures = {ws_pool.submit(process_workspace, ws, rows, collect): ws
                      for ws, rows in by_ws.items()}
        for fut in as_completed(ws_futures):
            fut.result()  # surface exceptions; results already streamed via collect()
    flush(force=True)  # persist the remainder

    summary = _summarize(queue_updates)
    print(f"Done. {summary}")


def _persist(queue_table, audit_table, queue_updates, audit_rows, dry_run):
    """Write status back to the queue and append audit rows, via temp views + MERGE.

    audit_rows already contains ONLY real changes (dry-run produces none), so
    there's no dry-run guard here. saveAsTable append resolves by column name, so
    the dict order is irrelevant; each row carries its own executed_by.
    """
    from pyspark.sql import functions as F
    from pyspark.sql.types import StructType, StructField, StringType

    # Explicit schemas: several columns (last_error, old_value, new_value, error)
    # are None on an all-success or all-remove batch, and Spark's createDataFrame
    # can't infer a type from an all-null column (CANNOT_DETERMINE_TYPE) — which
    # would crash AFTER the tag was already written, leaving the queue PENDING.
    def _schema(cols):
        return StructType([StructField(c, StringType(), True) for c in cols])

    if audit_rows:
        audit_cols = ["audit_id", "batch_id", "executed_by", "workspace_id", "product",
                      "workload_id", "tag_key", "old_value", "new_value", "action",
                      "status", "error"]
        adf = spark.createDataFrame(
            [{c: r.get(c) for c in audit_cols} for r in audit_rows], _schema(audit_cols)
        ).withColumn("executed_at", F.current_timestamp())
        adf.write.mode("append").saveAsTable(audit_table)

    if queue_updates:
        upd_cols = ["batch_id", "workload_id", "product", "tag_key", "status", "last_error"]
        udf = spark.createDataFrame(
            [{c: u.get(c) for c in upd_cols} for u in queue_updates], _schema(upd_cols))
        udf.createOrReplaceTempView("_tg_updates")
        # Match on the full natural key including product: workload_id is a
        # coalesce over id namespaces, so the same id string can appear under two
        # products; without product the MERGE could match multiple source rows.
        spark.sql(f"""
            MERGE INTO {queue_table} t
            USING _tg_updates s
            ON  t.batch_id = s.batch_id
            AND t.workload_id = s.workload_id
            AND t.product = s.product
            AND t.tag_key = s.tag_key
            WHEN MATCHED THEN UPDATE SET
              t.status = s.status,
              t.last_error = s.last_error,
              t.attempts = coalesce(t.attempts, 0) + 1,
              t.executed_at = current_timestamp()
        """)


def _summarize(queue_updates) -> str:
    counts: dict[str, int] = {}
    for u in queue_updates:
        counts[u["status"]] = counts.get(u["status"], 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


if __name__ == "__main__":
    run()
