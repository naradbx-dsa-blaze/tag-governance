"""Tag Governance live asset scanner (Phase 1).

Enumerates real resources via the SDK list APIs and reads each one's ACTUAL
current tag state, writing a row per resource to the inventory table. This is
distinct from the billing-derived workload_daily summary:

  * workload_daily answers "where is the COST" (from system.billing.usage).
  * inventory answers "what is the true TAG STATE right now" (from the resource
    APIs) — the ground truth compliance classification and remediation need.

Why a deep read: list APIs don't always return full tag state (some return a
resource stub without tags), so for products where tags aren't guaranteed on the
list payload we do a per-resource GET. When a read fails we record
tag_read_ok=FALSE with the reason — we never record an empty tag map as if the
resource were confidently untagged (that would be a silent lie, same failure
mode we fixed in the writer).

Scope: home workspace by default; all reachable workspaces under M2 (same
ClientResolver as the writer). Read-only — this job NEVER mutates a resource.

Parameters (job task parameters):
  inventory_table : fully-qualified inventory table (required)
  workspace_id    : optional — scan just this workspace
  scan_id         : optional — override the generated scan id
  products        : optional CSV filter of product keys (default: all direct-tag)
  max_workers     : optional per-workspace concurrency (default 8)
"""
from __future__ import annotations

import json
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import spark

import capability
import ws_clients

PERSIST_CHUNK = 200


def _parse_argv() -> dict:
    out = {}
    for tok in sys.argv[1:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k.strip()] = v.strip()
    return out


_ARGV = _parse_argv()


def _param(name: str, default: str | None = None) -> str | None:
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


# --------------------------------------------------------------------------- per-product enumerators
# Each enumerator yields dicts: {workload_id, workload_name, owner, tags, tag_read_ok, read_error}.
# tags is the ACTUAL current tag map. tag_read_ok=False means the state is unknown
# (read failed) — NOT that the resource has no tags.

def _scan_jobs(client):
    for j in client.jobs.list(expand_tasks=False):
        s = getattr(j, "settings", None)
        tags = dict(getattr(s, "tags", None) or {}) if s else {}
        yield {
            "workload_id": str(j.job_id),
            "workload_name": getattr(s, "name", None) if s else None,
            "owner": getattr(j, "creator_user_name", None),
            "tags": tags, "tag_read_ok": True, "read_error": None,
        }


def _scan_clusters(client):
    for c in client.clusters.list():
        # Only all-purpose clusters are a taggable ALL_PURPOSE resource; job
        # clusters are covered under their job. cluster_source JOB = skip.
        if str(getattr(c, "cluster_source", "")) not in ("ClusterSource.UI",
                                                          "ClusterSource.API", "UI", "API"):
            continue
        yield {
            "workload_id": c.cluster_id,
            "workload_name": c.cluster_name,
            "owner": getattr(c, "creator_user_name", None),
            "tags": dict(c.custom_tags or {}), "tag_read_ok": True, "read_error": None,
        }


def _scan_warehouses(client):
    for w in client.warehouses.list():
        pairs = list(w.tags.custom_tags) if (w.tags and w.tags.custom_tags) else []
        yield {
            "workload_id": str(w.id),
            "workload_name": w.name,
            "owner": getattr(w, "creator_name", None),
            "tags": {p.key: p.value for p in pairs},
            "tag_read_ok": True, "read_error": None,
        }


def _scan_serving(client):
    for ep in client.serving_endpoints.list():
        # List payload may omit tags → deep read per endpoint for true state.
        tags, ok, err = {}, True, None
        try:
            full = client.serving_endpoints.get(name=ep.name)
            tags = {t.key: t.value for t in (getattr(full, "tags", None) or [])}
        except Exception as e:  # noqa: BLE001
            ok, err = False, f"{type(e).__name__}: {e}"
        yield {
            "workload_id": getattr(ep, "id", None) or ep.name,
            "workload_name": ep.name,
            "owner": getattr(ep, "creator", None),
            "tags": tags, "tag_read_ok": ok, "read_error": err,
        }


def _scan_pipelines(client):
    # DLT tags live on cluster specs; we record presence + current cluster tags
    # (read-only) so compliance can see them even though remediation is UI_ONLY.
    for p in client.pipelines.list_pipelines():
        tags, ok, err = {}, True, None
        try:
            full = client.pipelines.get(pipeline_id=p.pipeline_id)
            clusters = getattr(getattr(full, "spec", None), "clusters", None) or []
            for cl in clusters:
                tags.update(dict(getattr(cl, "custom_tags", None) or {}))
        except Exception as e:  # noqa: BLE001
            ok, err = False, f"{type(e).__name__}: {e}"
        yield {
            "workload_id": p.pipeline_id,
            "workload_name": p.name,
            "owner": getattr(p, "creator_user_name", None),
            "tags": tags, "tag_read_ok": ok, "read_error": err,
        }


# product key -> enumerator. Only products with a real read/list path.
_SCANNERS = {
    "JOBS": _scan_jobs,
    "ALL_PURPOSE": _scan_clusters,
    "SQL": _scan_warehouses,
    "MODEL_SERVING": _scan_serving,
    "DLT": _scan_pipelines,
}


def _rows_for_product(client, workspace_id, scan_id, product):
    """Enumerate one product in one workspace → list of inventory row dicts.
    Never raises: a product whose list API fails yields one sentinel error row."""
    cap = capability.get(product)
    fallback = cap.fallback if cap else None
    direct = bool(cap.direct_tag) if cap else False
    out = []
    try:
        for r in _SCANNERS[product](client):
            tags = r.get("tags") or {}
            out.append({
                "scan_id": scan_id, "workspace_id": str(workspace_id),
                "product": product,
                "workload_id": str(r["workload_id"]),
                "workload_name": r.get("workload_name"),
                "owner": r.get("owner"),
                "tags": tags,
                "tag_keys": sorted(tags.keys()),
                "tag_count": len(tags),
                "direct_tag": direct, "fallback": fallback,
                "tag_read_ok": bool(r.get("tag_read_ok", True)),
                "read_error": r.get("read_error"),
                "raw_state": None,
            })
    except Exception as e:  # noqa: BLE001 — a failed list must not kill the sweep
        out.append({
            "scan_id": scan_id, "workspace_id": str(workspace_id), "product": product,
            "workload_id": f"__list_error__{product}", "workload_name": None, "owner": None,
            "tags": {}, "tag_keys": [], "tag_count": 0,
            "direct_tag": direct, "fallback": fallback,
            "tag_read_ok": False, "read_error": f"list failed: {type(e).__name__}: {e}",
            "raw_state": None,
        })
    return out


def run():
    inventory_table = _param("inventory_table")
    if not inventory_table:
        raise SystemExit("inventory_table parameter is required")
    only_workspace = _param("workspace_id") or None
    scan_id = _param("scan_id") or f"scan-{uuid.uuid4().hex[:12]}"
    products = [p.strip() for p in (_param("products") or "").split(",") if p.strip()]
    if not products:
        products = [p for p in _SCANNERS]  # all scannable products
    max_workers = int(_param("max_workers", "8") or "8")

    home_client = WorkspaceClient()
    home_ws = None
    try:
        home_ws = str(home_client.get_workspace_id())
    except Exception:
        pass
    scope = _param("account_sp_scope") or ""
    if scope:
        from databricks.sdk.runtime import dbutils
        if not ws_clients.load_account_creds_from_scope(scope, dbutils):
            raise SystemExit(f"account_sp_scope='{scope}' set but creds could not load.")
    resolver = ws_clients.ClientResolver(home_client, home_workspace_id=home_ws)

    # Which workspaces to scan.
    if only_workspace:
        target_ws = [only_workspace]
    elif resolver.cross_workspace:
        target_ws = [str(w.workspace_id) for w in resolver._account_client().workspaces.list()]
    else:
        if not home_ws:
            raise SystemExit("cannot determine home workspace id and no account creds")
        target_ws = [home_ws]

    print(f"scan {scan_id}: workspaces={target_ws} products={products}")

    rows_all: list[dict] = []
    persist_lock = Lock()
    pending: list[dict] = []

    def flush(force=False):
        nonlocal pending
        with persist_lock:
            if not force and len(pending) < PERSIST_CHUNK:
                return
            batch, pending = pending, []
        if batch:
            _persist(inventory_table, batch)

    def collect(rows):
        with persist_lock:
            pending.extend(rows)
            ready = len(pending) >= PERSIST_CHUNK
        rows_all.extend(rows)
        if ready:
            flush()

    def scan_workspace(wid):
        try:
            client = resolver.client_for(wid)
        except Exception as e:  # noqa: BLE001
            collect([{
                "scan_id": scan_id, "workspace_id": str(wid), "product": "__workspace__",
                "workload_id": f"__unreachable__{wid}", "workload_name": None, "owner": None,
                "tags": {}, "tag_keys": [], "tag_count": 0, "direct_tag": False,
                "fallback": None, "tag_read_ok": False,
                "read_error": f"workspace unreachable: {e}", "raw_state": None,
            }])
            return
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = {pool.submit(_rows_for_product, client, wid, scan_id, p): p
                    for p in products}
            for fut in as_completed(futs):
                collect(fut.result())

    with ThreadPoolExecutor(max_workers=min(len(target_ws), 14)) as ws_pool:
        ws_futs = {ws_pool.submit(scan_workspace, w): w for w in target_ws}
        for fut in as_completed(ws_futs):
            fut.result()
    flush(force=True)

    # Summarize.
    by_product: dict[str, int] = {}
    untagged = 0
    for r in rows_all:
        by_product[r["product"]] = by_product.get(r["product"], 0) + 1
        if r["tag_read_ok"] and r["tag_count"] == 0:
            untagged += 1
    print(f"scan {scan_id} done: {len(rows_all)} resources, {untagged} untagged. "
          f"by product: {by_product}")


def _persist(inventory_table, rows):
    """Append inventory rows via an explicit schema (avoids CANNOT_DETERMINE_TYPE
    on all-null columns — same lesson as the writer's _persist)."""
    from pyspark.sql.types import (StructType, StructField, StringType, IntegerType,
                                   BooleanType, TimestampType, MapType, ArrayType)
    from pyspark.sql import functions as F

    schema = StructType([
        StructField("scan_id", StringType(), True),
        StructField("workspace_id", StringType(), True),
        StructField("product", StringType(), True),
        StructField("workload_id", StringType(), True),
        StructField("workload_name", StringType(), True),
        StructField("owner", StringType(), True),
        StructField("tags", MapType(StringType(), StringType()), True),
        StructField("tag_keys", ArrayType(StringType()), True),
        StructField("tag_count", IntegerType(), True),
        StructField("direct_tag", BooleanType(), True),
        StructField("fallback", StringType(), True),
        StructField("tag_read_ok", BooleanType(), True),
        StructField("read_error", StringType(), True),
        StructField("raw_state", StringType(), True),
    ])
    cols = [f.name for f in schema.fields]
    df = spark.createDataFrame([{c: r.get(c) for c in cols} for r in rows], schema)
    df = df.withColumn("scanned_at", F.current_timestamp())
    df.write.mode("append").saveAsTable(inventory_table)


if __name__ == "__main__":
    run()
