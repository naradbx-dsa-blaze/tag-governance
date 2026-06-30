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

# Products that bill through serverless and are best governed by a budget/tag
# policy rather than a per-resource custom_tags write.
POLICY_GOVERNED = {"APPS"}


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
    warnings = []
    if is_serverless and rp.serverless_note:
        warnings.append(rp.serverless_note)
    if product in POLICY_GOVERNED:
        warnings.append("Recommend a budget/tag policy for durable attribution.")
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


def preview(plan: TagPlan) -> dict:
    """Non-mutating preview of what applying the plan would do."""
    return {
        "status": "DRY_RUN" if DRY_RUN else "READY",
        "message": ("Preview only — nothing modified yet."
                    if DRY_RUN else "Ready to apply to the live resource."),
        "resource": plan.resource_type,
        "workload": plan.workload_name or plan.workload_id,
        "would_call": plan.api_hint,
        "tags": plan.tags,
    }


def approve(plan: TagPlan) -> dict:
    """Approve & apply the plan.

    In DRY_RUN this records the approval and reports success WITHOUT touching any
    resource (so the demo shows the full approve → tagged flow safely). With
    DRY_RUN off, this is where the per-product SDK write goes — guarded for now so
    a flipped flag fails loud rather than silently no-op'ing.
    """
    if not plan.supported:
        return {"status": "UNSUPPORTED",
                "message": f"No tag-write path for product '{plan.product}' yet."}
    if DRY_RUN:
        return {
            "status": "APPROVED_DRY_RUN",
            "message": "Approved (dry-run) — recorded, no resource modified.",
            "resource": plan.resource_type,
            "workload": plan.workload_name or plan.workload_id,
            "would_call": plan.api_hint,
            "tags": plan.tags,
        }
    raise NotImplementedError(
        "Live tag writes are not enabled. Implement per-product SDK calls before "
        "setting TAG_GOVERNANCE_DRY_RUN=false."
    )
