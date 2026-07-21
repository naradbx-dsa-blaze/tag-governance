"""Tag application logic — dry-run preview today, live writes behind a flag.

Each billing_origin_product maps to a different resource type with its own
custom_tags write API. This module turns a (workload, desired_tags) pair into a
concrete, human-readable plan: which resource, which API, and the exact payload.

DRY_RUN is the safety switch. While True, apply() never mutates anything — it
only returns the plan. Flipping to live writes is a deliberate, separate change.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Master safety switch. Overridable via env (TAG_GOVERNANCE_DRY_RUN=false) but
# defaults to dry-run so nothing is ever mutated by accident.
DRY_RUN = os.environ.get("TAG_GOVERNANCE_DRY_RUN", "true").lower() != "false"


# product -> how to tag it
@dataclass(frozen=True)
class ResourcePlan:
    resource_type: str       # human label
    id_kind: str             # which usage_metadata id identifies it
    api_hint: str            # the SDK/REST call that would set custom_tags
    serverless_note: str = ""  # extra caveat for serverless attribution


PRODUCT_PLANS: dict[str, ResourcePlan] = {
    "JOBS": ResourcePlan(
        "Lakeflow Job", "job_id",
        "jobs.update(job_id, new_settings={'tags': {...}})",
    ),
    "DLT": ResourcePlan(
        "Lakeflow Declarative Pipeline", "dlt_pipeline_id",
        "pipelines.update(pipeline_id, configuration/clusters[].custom_tags={...})",
    ),
    "SQL": ResourcePlan(
        "SQL Warehouse", "warehouse_id",
        "warehouses.edit(id, tags=EndpointTags(custom_tags=[...]))",
        serverless_note="Serverless SQL cost attributes via budget/tag policy, not the warehouse tag.",
    ),
    "MODEL_SERVING": ResourcePlan(
        "Model Serving Endpoint", "endpoint_id",
        "serving_endpoints.patch(name, add_tags=[EndpointTag(key,value)])",
        serverless_note="Serverless serving; tags propagate to billing on next usage.",
    ),
    "VECTOR_SEARCH": ResourcePlan(
        "Vector Search Endpoint", "endpoint_id",
        "vector_search_endpoints.update_endpoint_custom_tags(name, custom_tags=[...])",
    ),
    "ALL_PURPOSE": ResourcePlan(
        "All-Purpose Cluster", "cluster_id",
        "clusters.edit(cluster_id, custom_tags={...})",
    ),
    "POOL": ResourcePlan(
        "Instance Pool", "instance_pool_id",
        "instance_pools.edit(instance_pool_id, custom_tags={...})",
        serverless_note="Pool tags propagate to the VMs launched from the pool.",
    ),
    "INTERACTIVE": ResourcePlan(
        "All-Purpose Cluster", "cluster_id",
        "clusters.edit(cluster_id, custom_tags={...})",
    ),
    "APPS": ResourcePlan(
        "Databricks App", "app_id",
        "apps.update(name, resources/user_api_scopes) — tag via budget policy",
        serverless_note="Apps compute attributes via budget policy, not free-form custom_tags.",
    ),
    "LAKEBASE": ResourcePlan(
        "Lakebase / Database Instance", "database_instance_id",
        "database.update_database_instance(name, custom_tags={...})",
        serverless_note="Often orphaned (no owner in metadata) — confirm ownership before tagging.",
    ),
}

# Products that CANNOT take a per-resource custom_tags write — mirror writer.py.
# These attribute cost via a budget/tag policy (serverless) or the pipeline
# definition (DLT), so the writer would return UNSUPPORTED. We refuse to enqueue
# them (they'd sit PENDING forever) and tell the user the real action instead.
POLICY_GOVERNED = {"APPS", "AI_GATEWAY", "INTERACTIVE", "LAKEFLOW_CONNECT"}
# DLT is taggable only via the pipeline definition, not a per-resource call.
UI_ONLY = {"DLT"}
# Reasons surfaced to the user when we refuse to enqueue.
NOT_TAGGABLE_REASON = {
    "APPS": "Databricks Apps attribute cost via a budget policy, not a per-resource "
            "tag. Assign a budget policy (Settings → Budget policies, or the "
            "databricks_budget_policy Terraform resource).",
    "AI_GATEWAY": "AI Gateway has no per-resource tag API — attribute via a budget policy.",
    "INTERACTIVE": "Interactive/background usage isn't an individually taggable resource.",
    "LAKEFLOW_CONNECT": "Managed ingestion attributes via a budget policy, not a per-resource tag.",
    "DLT": "DLT/SDP tags live in the pipeline definition (clusters[].custom_tags) — "
           "set them there, not via a per-resource tag call.",
}


def _is_taggable(product: str, is_serverless: bool = False) -> bool:
    """True only if the writer can set a per-resource tag on this product.
    Mirrors writer.attempt_write: policy-governed + DLT can't; serverless SQL can't
    (but a provisioned SQL warehouse can)."""
    if product in POLICY_GOVERNED or product in UI_ONLY:
        return False
    if product == "SQL" and is_serverless:
        return False
    return product in PRODUCT_PLANS


@dataclass
class TagPlan:
    product: str
    workload_id: str
    workload_name: str
    resource_type: str
    api_hint: str
    tags: dict
    serverless_note: str = ""
    supported: bool = True
    warnings: list = field(default_factory=list)

    @property
    def is_dry_run(self) -> bool:
        return DRY_RUN


def plan_for(product: str, workload_id: str, workload_name: str,
             tags: dict, is_serverless: bool = False) -> TagPlan:
    """Build the (non-mutating) plan describing how this workload would be tagged."""
    rp = PRODUCT_PLANS.get(product)
    if rp is None:
        return TagPlan(
            product, workload_id, workload_name,
            resource_type="Unknown / not yet supported",
            api_hint="—", tags=tags, supported=False,
            warnings=[f"No tag-write mapping for product '{product}' yet."],
        )
    # Refuse products the writer can't per-resource tag — with the real action,
    # instead of enqueuing a row that would sit PENDING/UNSUPPORTED forever.
    if not _is_taggable(product, is_serverless):
        reason = NOT_TAGGABLE_REASON.get(product)
        if product == "SQL" and is_serverless:
            reason = ("Serverless SQL attributes cost via a budget policy, not a "
                      "per-warehouse tag. A provisioned SQL warehouse can be tagged.")
        return TagPlan(
            product, workload_id, workload_name,
            resource_type=rp.resource_type, api_hint=rp.api_hint, tags=tags,
            supported=False, warnings=[reason or "Not taggable via a per-resource API."],
        )
    warnings = []
    if is_serverless and rp.serverless_note:
        warnings.append(rp.serverless_note)
    return TagPlan(
        product=product,
        workload_id=workload_id,
        workload_name=workload_name,
        resource_type=rp.resource_type,
        api_hint=rp.api_hint,
        tags=tags,
        serverless_note=rp.serverless_note,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Enqueue: record write INTENT into the queue. The app never mutates a resource;
# the writer job drains the queue. A fresh batch_id groups everything enqueued in
# one action so it can be tracked and rolled back as a unit. DRY_RUN now lives on
# the writer JOB (which decides whether to actually write), so enqueuing is always
# safe — it only inserts rows.
# ---------------------------------------------------------------------------

def _new_batch_id() -> str:
    import uuid
    return f"batch-{uuid.uuid4().hex[:12]}"


# Per-request requester email, set by the app from the Apps-forwarded identity so
# the audit/queue records WHO enqueued each batch (not a generic "app").
import contextvars
_requester = contextvars.ContextVar("requester", default="")


def set_requester(email: str) -> None:
    _requester.set(email or "")


def _requested_by() -> str:
    import os
    return _requester.get() or os.environ.get("TAG_GOVERNANCE_USER") or "app"


def enqueue_single(plan: "TagPlan", workspace_id: str, is_serverless: bool = False,
                   list_cost: float | None = None) -> dict:
    """Enqueue one workload's tags. Returns {batch_id, count} or UNSUPPORTED."""
    import db
    import queries
    if not plan.supported:
        reason = plan.warnings[0] if plan.warnings else \
            f"'{plan.product}' can't be tagged via a per-resource API."
        return {"status": "UNSUPPORTED", "message": reason}
    batch_id = _new_batch_id()
    sql = queries.enqueue_single_sql(
        batch_id=batch_id, requested_by=_requested_by(), workspace_id=workspace_id,
        product=plan.product, workload_id=plan.workload_id,
        workload_name=plan.workload_name, is_serverless=is_serverless, tags=plan.tags,
        list_cost=list_cost,
    )
    db.run_exec(sql)
    return {"status": "ENQUEUED", "batch_id": batch_id, "count": len(plan.tags),
            "workload": plan.workload_name or plan.workload_id, "tags": plan.tags}


def tag_selected(tag_key: str, workloads: list) -> dict:
    """Enqueue an EXPLICIT, user-selected list of workloads (each with its own
    value) as one batch. This is the "review the rule's matches, uncheck the ones
    that don't belong, apply only the rest" path — nothing is re-derived from a
    rule, so unchecked rows are simply never queued. Returns {batch_id, total_rows}.
    """
    import db
    import queries
    batch_id = _new_batch_id()
    sql = queries.enqueue_explicit_sql(
        batch_id=batch_id, requested_by=_requested_by(),
        tag_key=tag_key, workloads=workloads,
    )
    if sql is None:
        return {"status": "NO_ROWS", "message": "No workloads selected."}
    db.run_exec(sql)
    total = _count_batch(batch_id)
    return {"status": "ENQUEUED", "batch_id": batch_id, "total_rows": total}


def auto_tag(tag_key: str, days: int, rules: list | None = None,
             min_confidence: float = 0.8, workspaces=None,
             use_ai: bool = True) -> dict:
    """The seamless one-shot: rules first, AI suggestions for the rest, ONE batch.

    Everything enqueues under a single batch_id so it drains and rolls back as a
    unit. Does NOT run the writer — the caller triggers the writer job (dry-run by
    default) so the safety switch stays next to the code that mutates resources.

    use_ai=False makes this a PURE RULES run — the AI-suggestion step is skipped
    entirely, for customers who don't want AI-proposed tags. With no rules and
    use_ai=False, nothing is enqueued (total_rows=0).
    """
    import db
    import queries
    batch_id = _new_batch_id()
    rules = rules or []

    # Run the rule INSERT (if any). We DON'T trust run_exec's return: the
    # Databricks SQL connector reports -1 for DML rowcounts, so we count the
    # actual queued rows below instead of inferring from rowcount.
    rule_done = False
    if rules:
        rsql = queries.enqueue_bulk_sql(
            batch_id=batch_id, requested_by=_requested_by(), days=days,
            tag_keys=[tag_key], rules=rules, workspaces=workspaces,
        )
        if rsql is not None:
            db.run_exec(rsql)
            rule_done = True

    rows_after_rules = _count_batch(batch_id) if rule_done else 0

    # AI fallback: only workloads a rule didn't already cover in this batch.
    # Skipped entirely when the customer opts out of AI.
    if use_ai:
        ai_sql = queries.enqueue_from_suggestions_sql(
            batch_id=batch_id, requested_by=_requested_by(), tag_key=tag_key,
            min_confidence=min_confidence, days=days, workspaces=workspaces,
            exclude_batch_id=batch_id,
        )
        db.run_exec(ai_sql)

    total_rows = _count_batch(batch_id)
    return {"status": "ENQUEUED", "batch_id": batch_id,
            "rule_rows": rows_after_rules, "ai_rows": total_rows - rows_after_rules,
            "total_rows": total_rows, "used_ai": use_ai}


def _count_batch(batch_id: str) -> int:
    """Ground truth for how many rows a batch has — used instead of DML rowcount,
    which the Databricks SQL connector reports as -1 for INSERTs."""
    import db
    import queries
    rows = db.run_query(queries.count_queue_rows(batch_id))
    return int(rows[0]["n"]) if rows else 0
