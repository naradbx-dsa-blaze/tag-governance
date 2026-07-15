"""Per-product live tag writes — the deterministic core of Tag Governance.

Run by the writer job (writer_job.py), NEVER imported by the Streamlit app. The
app only records intent into the queue; this module is the only place a resource
is actually mutated.

Design rules that make writes safe:
  - GET → MERGE → PUT. We read the resource's current custom_tags, add/overwrite
    only our one key, and write the whole thing back. We never blind-replace, so
    existing tags (system or other governance keys) survive.
  - Idempotent. If the key already equals the target value, it's a no-op success
    — reruns and retries are safe.
  - Best-effort. Every product is attempted; anything we can't tag (serverless
    SQL / Apps, or an API the installed SDK doesn't expose) returns UNSUPPORTED
    with a concrete reason rather than crashing the batch.
  - Auditable. attempt_write returns the old→new value so the caller can record
    it to the audit table for rollback.

Resource id ← workload_id. materialize.sql builds workload_id as
  coalesce(job_id, warehouse_id, dlt_pipeline_id, endpoint_id, app_id, index_id,
           database_instance_id, cluster_id)
so (product, workload_id) uniquely locates the resource. For MODEL_SERVING the
patch API keys on the endpoint NAME, which materialize.sql carries as
workload_name — passed through as `name` below.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WriteResult:
    """Outcome of a single (workload, tag_key) write attempt."""
    status: str            # SUCCEEDED | FAILED | UNSUPPORTED
    old_value: str | None  # value of the key before (None if absent)
    new_value: str | None  # value of the key after (None if not written)
    error: str | None = None
    noop: bool = False     # key already had the target value
    dry_run: bool = False  # computed old→new but did NOT mutate the resource

    @classmethod
    def unsupported(cls, reason: str) -> "WriteResult":
        return cls(status="UNSUPPORTED", old_value=None, new_value=None, error=reason)

    @classmethod
    def failed(cls, reason: str, old: str | None = None) -> "WriteResult":
        return cls(status="FAILED", old_value=old, new_value=None, error=reason)

    @classmethod
    def ok(cls, old: str | None, new: str, noop: bool = False,
           dry_run: bool = False) -> "WriteResult":
        return cls(status="SUCCEEDED", old_value=old, new_value=new,
                   noop=noop, dry_run=dry_run)


# Products that bill through serverless / policy attribution and cannot take a
# per-resource custom_tags write. These are honestly reported, not attempted.
POLICY_GOVERNED_REASON = {
    "SQL": "Serverless SQL attributes cost via budget/tag policy, not a per-warehouse tag. "
           "Governed by a tag policy, not a resource write.",
    "APPS": "Databricks Apps attribute compute via budget policy; no per-app custom_tags API.",
    "AI_GATEWAY": "AI Gateway usage has no per-resource custom_tags write API.",
    "INTERACTIVE": "Interactive/background usage is not an individually taggable resource.",
    "LAKEFLOW_CONNECT": "Managed ingestion attributes via policy, not a per-resource tag.",
}


# --------------------------------------------------------------------------- per-product writers
# Each writer does GET → MERGE → PUT and returns a WriteResult. They assume the
# key is genuinely taggable for that product; POLICY_GOVERNED handling happens in
# attempt_write before dispatch.

def _apply_merge(current: dict, key: str, value: str | None):
    """Merge one key into a tags dict. value=None removes the key (for rollback).

    Returns (old_value, new_dict, noop). noop is True when the dict already has
    the desired state (key==value, or key already absent when removing).
    """
    old = current.get(key)
    if value is None:
        if key not in current:
            return old, current, True
        new = dict(current)
        new.pop(key, None)
        return old, new, False
    if old == value:
        return old, current, True
    new = dict(current)
    new[key] = value
    return old, new, False


def _write_job(client, workload_id: str, name: str, key: str, value: str | None,
               dry_run: bool = False) -> WriteResult:
    try:
        job_id = int(workload_id)
    except (TypeError, ValueError):
        return WriteResult.failed(f"JOBS workload_id '{workload_id}' is not an int job_id")
    job = client.jobs.get(job_id=job_id)
    settings = job.settings
    current = dict(settings.tags or {}) if settings else {}
    old, merged, noop = _apply_merge(current, key, value)
    if noop:
        return WriteResult.ok(old, value, noop=True)
    if dry_run:
        return WriteResult.ok(old, value, dry_run=True)
    # Two write modes, picked by whether we're SETTING or REMOVING a key:
    #  - SET (value is not None): jobs.update with ONLY tags in new_settings. This
    #    is a PARTIAL merge — it changes nothing but the tags map, so the owner's
    #    schedule/tasks/clusters/params are untouched. Safest for tagging jobs we
    #    don't own. (Verified: update preserves all other settings on a live job.)
    #  - REMOVE (value is None, i.e. rollback): update MERGES and can't drop a key,
    #    so we fall back to jobs.reset (full settings REPLACE) with the complete
    #    settings and only .tags swapped, which sets tags to exactly `merged`.
    from databricks.sdk.service.jobs import JobSettings
    if value is not None:
        client.jobs.update(job_id=job_id, new_settings=JobSettings(tags=merged))
    else:
        settings.tags = merged
        client.jobs.reset(job_id=job_id, new_settings=settings)
    return WriteResult.ok(old, value)


def _write_cluster(client, workload_id: str, name: str, key: str, value: str | None,
                   dry_run: bool = False) -> WriteResult:
    c = client.clusters.get(cluster_id=workload_id)
    current = dict(c.custom_tags or {})
    old, merged, noop = _apply_merge(current, key, value)
    if noop:
        return WriteResult.ok(old, value, noop=True)
    if dry_run:
        return WriteResult.ok(old, value, dry_run=True)
    # clusters.edit requires the full spec; carry forward the identifying fields.
    client.clusters.edit(
        cluster_id=workload_id,
        spark_version=c.spark_version,
        node_type_id=c.node_type_id,
        num_workers=c.num_workers,
        autoscale=c.autoscale,
        custom_tags=merged,
    )
    return WriteResult.ok(old, value)


def _write_warehouse(client, workload_id: str, name: str, key: str, value: str | None,
                     dry_run: bool = False) -> WriteResult:
    from databricks.sdk.service.sql import EndpointTags, EndpointTagPair
    w = client.warehouses.get(id=workload_id)
    pairs = list(w.tags.custom_tags) if (w.tags and w.tags.custom_tags) else []
    current = {p.key: p.value for p in pairs}
    old, merged, noop = _apply_merge(current, key, value)
    if noop:
        return WriteResult.ok(old, value, noop=True)
    if dry_run:
        return WriteResult.ok(old, value, dry_run=True)
    tags = EndpointTags(custom_tags=[EndpointTagPair(key=k, value=v) for k, v in merged.items()])
    client.warehouses.edit(id=workload_id, tags=tags)
    return WriteResult.ok(old, value)


def _write_serving(client, workload_id: str, name: str, key: str, value: str | None,
                   dry_run: bool = False) -> WriteResult:
    from databricks.sdk.service.serving import EndpointTag
    ep_name = name or workload_id  # patch keys on endpoint name (carried as workload_name)
    # Do NOT swallow a GET failure into current={}: that would record old_value=None
    # for a resource that may actually have a prior value, and a later rollback would
    # then delete a tag it should have restored. Let a GET error propagate to
    # attempt_write's boundary → FAILED (safe: nothing written, nothing mis-audited).
    ep = client.serving_endpoints.get(name=ep_name)
    tags = getattr(ep, "tags", None) or []
    current = {t.key: t.value for t in tags}
    old, _, noop = _apply_merge(current, key, value)
    if noop:
        return WriteResult.ok(old, value, noop=True)
    if dry_run:
        return WriteResult.ok(old, value, dry_run=True)
    if value is None:
        # rollback: remove the key we added.
        client.serving_endpoints.patch(name=ep_name, delete_tags=[key])
    else:
        # patch merges: add_tags upserts the key without touching others.
        client.serving_endpoints.patch(name=ep_name, add_tags=[EndpointTag(key=key, value=value)])
    return WriteResult.ok(old, value)


def _write_lakebase(client, workload_id: str, name: str, key: str, value: str | None,
                    dry_run: bool = False) -> WriteResult:
    # database service is only in newer SDKs — feature-detect so old runtimes
    # report UNSUPPORTED instead of raising AttributeError.
    db = getattr(client, "database", None)
    if db is None or not hasattr(db, "update_database_instance"):
        return WriteResult.unsupported(
            "Installed databricks-sdk has no database API; upgrade the SDK to tag Lakebase."
        )
    try:
        inst = db.get_database_instance(name=name or workload_id)
        current = dict(getattr(inst, "custom_tags", None) or {})
        old, merged, noop = _apply_merge(current, key, value)
        if noop:
            return WriteResult.ok(old, value, noop=True)
        if dry_run:
            return WriteResult.ok(old, value, dry_run=True)
        db.update_database_instance(name=name or workload_id, custom_tags=merged)
        return WriteResult.ok(old, value)
    except Exception as e:  # noqa: BLE001 — best-effort, surface the reason
        return WriteResult.failed(f"Lakebase tag write failed: {e}")


def _write_vector_search(client, workload_id: str, name: str, key: str, value: str,
                         dry_run: bool = False) -> WriteResult:
    vs = getattr(client, "vector_search_endpoints", None)
    fn = getattr(vs, "update_endpoint_custom_tags", None) if vs else None
    if fn is None:
        return WriteResult.unsupported(
            "Installed databricks-sdk has no Vector Search tag API; upgrade the SDK to tag it."
        )
    if dry_run:
        # No cheap read of current VS tags here, so old_value is unknown in dry-run.
        return WriteResult.ok(None, value, dry_run=True)
    try:
        fn(endpoint_name=name or workload_id, custom_tags={key: value})
        return WriteResult.ok(None, value)
    except Exception as e:  # noqa: BLE001
        return WriteResult.failed(f"Vector Search tag write failed: {e}")


def _write_pipeline(client, workload_id: str, name: str, key: str, value: str,
                    dry_run: bool = False) -> WriteResult:
    # DLT has no top-level tags field; tags live on the pipeline's cluster specs.
    # Editing cluster specs safely requires replaying the full pipeline definition,
    # which is risky to do blind. Report UNSUPPORTED with guidance rather than
    # partially rewrite a pipeline spec.
    return WriteResult.unsupported(
        "DLT/SDP tags live on the pipeline's cluster specs; set them in the pipeline "
        "definition (clusters[].custom_tags), not via a per-resource tag write."
    )


# product -> writer function. INTERACTIVE is intentionally NOT here: it's in
# POLICY_GOVERNED_REASON (billed as background/interactive usage, not an
# individually taggable resource), so attempt_write short-circuits it to
# UNSUPPORTED before dispatch. All-purpose clusters come through as ALL_PURPOSE.
_WRITERS = {
    "JOBS": _write_job,
    "ALL_PURPOSE": _write_cluster,
    "SQL": _write_warehouse,          # non-serverless warehouses only (see attempt_write)
    "DATABASE": _write_warehouse,
    "MODEL_SERVING": _write_serving,
    "LAKEBASE": _write_lakebase,
    "VECTOR_SEARCH": _write_vector_search,
    "DLT": _write_pipeline,
}


def attempt_write(client, product: str, workload_id: str, workload_name: str,
                  key: str, value: str | None, is_serverless: bool = False,
                  dry_run: bool = False) -> WriteResult:
    """Best-effort: tag one resource, or return UNSUPPORTED/FAILED with a reason.

    value=None means REMOVE the key (used by rollback to restore a previously
    absent key). dry_run=True reads the resource and computes old→new but does
    NOT mutate it (status SUCCEEDED with dry_run=True). Never raises — a bad row
    must not kill the batch. Serverless SQL and Apps are reported as
    policy-governed; unknown products and SDK gaps are UNSUPPORTED.
    """
    # Serverless SQL is policy-governed even though non-serverless SQL warehouses
    # are taggable — decide by the flag before dispatch.
    if product == "SQL" and is_serverless:
        return WriteResult.unsupported(POLICY_GOVERNED_REASON["SQL"])
    if product in POLICY_GOVERNED_REASON and product != "SQL":
        return WriteResult.unsupported(POLICY_GOVERNED_REASON[product])

    writer = _WRITERS.get(product)
    if writer is None:
        return WriteResult.unsupported(f"No tag-write path for product '{product}'.")
    try:
        return writer(client, workload_id, workload_name, key, value, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001 — best-effort boundary
        return WriteResult.failed(f"{type(e).__name__}: {e}")
