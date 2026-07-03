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


# ============================================================================
# Bulk / rule-based tagging
# ============================================================================
# A rule attributes MANY workloads at once: "where <field> <op> <value>, set
# <tags>". A handful of rules can cover thousands of workloads. Matching and
# conflict detection are pure functions over the untagged-workload rows, so the
# whole preview is instant and safe (no mutation).

import fnmatch
from dataclasses import asdict

# fields a rule can match on, mapped to the row key
RULE_FIELDS = {
    "owner": "owner",
    "workspace": "workspace_id",
    "product": "product",
    "name": "workload_name",
}
RULE_OPS = ("equals", "contains", "matches")  # matches = glob, e.g. fraud-*


def _rule_matches(rule: dict, row: dict) -> bool:
    field = RULE_FIELDS.get(rule["field"])
    val = (row.get(field) or "")
    target = rule["value"]
    if val is None:
        val = ""
    val = str(val)
    op = rule["op"]
    if op == "equals":
        return val.lower() == str(target).lower()
    if op == "contains":
        return str(target).lower() in val.lower()
    if op == "matches":
        return fnmatch.fnmatch(val.lower(), str(target).lower())
    return False


def evaluate_rules(rules: list, rows: list) -> dict:
    """Apply ordered rules to workload rows (list of dicts).

    Earlier rules win on conflict (first match assigns each tag key). Returns:
      assignments: workload_id -> {key: value, ...}
      per_rule:    rule index -> #workloads it newly tagged
      conflicts:   list of (workload_id, key, kept_value, dropped_value, rule_idx)
      matched_cost / matched_count: coverage totals
    """
    assignments: dict = {}
    rule_origin: dict = {}          # (workload_id,key) -> rule_idx that set it
    per_rule = {i: 0 for i in range(len(rules))}
    conflicts = []

    for idx, rule in enumerate(rules):
        if not rule.get("value") or not rule.get("tags"):
            continue
        newly = set()
        for row in rows:
            if not _rule_matches(rule, row):
                continue
            wid = row["workload_id"]
            cur = assignments.setdefault(wid, {})
            for k, v in rule["tags"].items():
                if k in cur:
                    if cur[k] != v:
                        conflicts.append((wid, k, cur[k], v, idx))
                    # earlier rule wins; do not overwrite
                else:
                    cur[k] = v
                    rule_origin[(wid, k)] = idx
                    newly.add(wid)
        per_rule[idx] = len(newly)

    cost_by_id = {r["workload_id"]: float(r.get("untagged_cost") or 0) for r in rows}
    matched_ids = set(assignments.keys())
    return {
        "assignments": assignments,
        "per_rule": per_rule,
        "conflicts": conflicts,
        "matched_count": len(matched_ids),
        "matched_cost": sum(cost_by_id.get(w, 0) for w in matched_ids),
    }


def bulk_apply(assignments: dict, rows_by_id: dict) -> dict:
    """Apply a set of {workload_id: tags} assignments (pure-Python, no SQL).

    Kept for the client-side rule path / tests. In DRY_RUN it records the batch
    and reports per-workload results without mutating. Live bulk writes go through
    enqueue_bulk (SQL INSERT … SELECT) + the writer job, NOT this function — so it
    stays a dry-run helper rather than raising.
    """
    results = []
    for wid, tags in assignments.items():
        row = rows_by_id.get(wid, {})
        results.append({
            "workload_id": wid,
            "workload_name": row.get("workload_name") or wid,
            "product": row.get("product"),
            "tags": tags,
            "status": "APPROVED_DRY_RUN",
            "cost": float(row.get("untagged_cost") or 0),
        })
    return {
        "status": "BULK_DRY_RUN",
        "count": len(results),
        "total_cost": sum(r["cost"] for r in results),
        "results": results,
    }


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


def _requested_by() -> str:
    import os
    return os.environ.get("TAG_GOVERNANCE_USER") or "app"


def enqueue_single(plan: "TagPlan", workspace_id: str, is_serverless: bool = False,
                   list_cost: float | None = None) -> dict:
    """Enqueue one workload's tags. Returns {batch_id, count} or UNSUPPORTED."""
    import db
    import queries
    if not plan.supported:
        return {"status": "UNSUPPORTED",
                "message": f"No tag-write path for product '{plan.product}' yet."}
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


def enqueue_bulk(days: int, tag_keys: list, rules: list, workspaces=None) -> dict:
    """Enqueue all workloads matched by a rule set (SQL INSERT … SELECT).

    Expansion to one row per (workload, tag_key) happens in the warehouse, so a
    50k-workload rule never round-trips through the app. Returns the batch_id and
    how many queue rows were inserted.
    """
    import db
    import queries
    batch_id = _new_batch_id()
    sql = queries.enqueue_bulk_sql(
        batch_id=batch_id, requested_by=_requested_by(), days=days,
        tag_keys=tag_keys, rules=rules, workspaces=workspaces,
    )
    if sql is None:
        return {"status": "NO_RULES", "message": "No valid rules to enqueue."}
    inserted = db.run_exec(sql)
    # run_exec returns -1 when the driver reports no rowcount for DML; surface None
    # so the UI falls back to the previewed count instead of showing "-1".
    rows = inserted if (inserted is not None and inserted >= 0) else None
    return {"status": "ENQUEUED", "batch_id": batch_id, "rows": rows}


def approve(plan: TagPlan, workspace_id: str = "", is_serverless: bool = False,
            list_cost: float | None = None) -> dict:
    """Approve the plan by enqueuing it for the writer job.

    This no longer writes (or pretends to write) inline — it records intent into
    the queue and returns a batch_id. Whether the writer actually mutates the
    resource depends on the writer job's dry_run parameter, keeping the safety
    switch next to the code that does the writing.
    """
    if not plan.supported:
        return {"status": "UNSUPPORTED",
                "message": f"No tag-write path for product '{plan.product}' yet."}
    return enqueue_single(plan, workspace_id=workspace_id, is_serverless=is_serverless,
                          list_cost=list_cost)
