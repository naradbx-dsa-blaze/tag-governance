"""Declarative resource capability registry — the single source of truth for
what each Databricks billing product supports for tagging.

Before this module, capability facts were split across writer.py (`_WRITERS`,
`POLICY_GOVERNED_REASON`) and queries.py (`_API_TAGGABLE_PRODUCTS`,
`_POLICY_GOVERNED_PRODUCTS`) and could drift out of sync. Everything now derives
from CAPABILITIES here:

  * writer.attempt_write dispatches by `write_path` / `tag_method` (still the code
    functions, but gated by this registry's `direct_tag` + `fallback`).
  * queries.* classification predicates derive their product lists from here.
  * the API exposes /capabilities so the UI can render the matrix and explain
    exactly why a surface is or isn't tag-able.

Fallback patterns (spec §9) for surfaces without post-hoc direct tagging:
  DIRECT          - a per-resource custom_tags write API exists (the writer uses it)
  BUDGET_POLICY   - serverless/managed; attribute via a budget/tag policy
  CREATE_TIME     - tags can only be set at create time (immutable after)
  UI_ONLY         - no safe API; edit in the resource's UI definition
  EXCEPTION_QUEUE - no path at all; route to the exception queue for manual handling
"""
from __future__ import annotations

from dataclasses import dataclass, field


# Fallback / remediation pattern for a product (see module docstring).
DIRECT = "DIRECT"
BUDGET_POLICY = "BUDGET_POLICY"
CREATE_TIME = "CREATE_TIME"
UI_ONLY = "UI_ONLY"
EXCEPTION_QUEUE = "EXCEPTION_QUEUE"


@dataclass(frozen=True)
class Capability:
    """How one billing product can (or cannot) be tagged."""
    product: str
    label: str
    read_path: str            # how we read current tag state (SDK call / "billing only")
    write_path: str           # how a tag is written, or "" if none
    tag_method: str           # writer._WRITERS key that handles it, or "" if none
    direct_tag: bool          # a per-resource custom_tags write API exists
    create_time_only: bool    # tags settable only at create time
    policy_driven: bool       # attributed via budget/tag policy, not a resource tag
    ui_only: bool             # no safe API; must edit in the resource UI definition
    rollback: bool            # a prior tag state can be restored (removal supported)
    fallback: str             # remediation pattern when direct_tag is False
    reason: str = ""          # human explanation (shown in the not-taggable panel)
    serverless_differs: bool = False  # True when is_serverless flips taggability (SQL)


# NOTE: SQL is special — non-serverless warehouses are DIRECT-taggable, serverless
# SQL is BUDGET_POLICY. `serverless_differs=True` tells callers to branch on the
# is_serverless flag (writer.attempt_write and queries._taggable_predicate do).
CAPABILITIES: dict[str, Capability] = {
    "JOBS": Capability(
        product="JOBS", label="Jobs",
        read_path="jobs.get(job_id).settings.tags",
        write_path="jobs.update(tags=…) / jobs.reset for removal",
        tag_method="JOBS", direct_tag=True, create_time_only=False,
        policy_driven=False, ui_only=False, rollback=True, fallback=DIRECT),
    "ALL_PURPOSE": Capability(
        product="ALL_PURPOSE", label="All-purpose compute",
        read_path="clusters.get(cluster_id).custom_tags",
        write_path="clusters.edit(custom_tags=…) [full-spec GET→MERGE→PUT]",
        tag_method="ALL_PURPOSE", direct_tag=True, create_time_only=False,
        policy_driven=False, ui_only=False, rollback=True, fallback=DIRECT),
    "SQL": Capability(
        product="SQL", label="SQL warehouses",
        read_path="warehouses.get(id).tags.custom_tags",
        write_path="warehouses.edit(tags=…) [non-serverless only]",
        tag_method="SQL", direct_tag=True, create_time_only=False,
        policy_driven=False, ui_only=False, rollback=True, fallback=DIRECT,
        serverless_differs=True,
        reason="Non-serverless warehouses take a direct tag; serverless SQL is "
               "attributed via a budget/tag policy, not a per-warehouse tag."),
    "DATABASE": Capability(
        product="DATABASE", label="Database (SQL warehouse path)",
        read_path="warehouses.get(id).tags.custom_tags",
        write_path="warehouses.edit(tags=…)",
        tag_method="DATABASE", direct_tag=True, create_time_only=False,
        policy_driven=False, ui_only=False, rollback=True, fallback=DIRECT),
    "MODEL_SERVING": Capability(
        product="MODEL_SERVING", label="Model serving",
        read_path="serving_endpoints.get(name).tags",
        write_path="serving_endpoints.patch(add_tags/delete_tags)",
        tag_method="MODEL_SERVING", direct_tag=True, create_time_only=False,
        policy_driven=False, ui_only=False, rollback=True, fallback=DIRECT),
    "LAKEBASE": Capability(
        product="LAKEBASE", label="Lakebase",
        read_path="database.get_database_instance(name).custom_tags [SDK-gated]",
        write_path="database.update_database_instance(custom_tags=…) [SDK-gated]",
        tag_method="LAKEBASE", direct_tag=True, create_time_only=False,
        policy_driven=False, ui_only=False, rollback=True, fallback=DIRECT,
        reason="Requires a databricks-sdk new enough to expose the database API; "
               "older runtimes report UNSUPPORTED."),
    "VECTOR_SEARCH": Capability(
        product="VECTOR_SEARCH", label="Vector Search",
        read_path="(no cheap tag read in current SDK)",
        write_path="vector_search_endpoints.update_endpoint_custom_tags [SDK-gated]",
        tag_method="VECTOR_SEARCH", direct_tag=True, create_time_only=False,
        policy_driven=False, ui_only=False, rollback=False, fallback=DIRECT,
        reason="SDK-gated; older SDKs have no VS tag API. No cheap read of current "
               "tags, so old_value is unknown (rollback not guaranteed)."),
    "DLT": Capability(
        product="DLT", label="Lakeflow Declarative Pipelines (DLT/SDP)",
        read_path="pipelines.get(pipeline_id).spec.clusters[].custom_tags",
        write_path="", tag_method="", direct_tag=False, create_time_only=False,
        policy_driven=False, ui_only=True, rollback=False, fallback=UI_ONLY,
        reason="Tags live on the pipeline's cluster specs; set them in the pipeline "
               "definition (clusters[].custom_tags), not via a per-resource write."),
    # ---- policy-governed / no-API surfaces (never attempted; reported honestly) ----
    "APPS": Capability(
        product="APPS", label="Databricks Apps",
        read_path="billing only", write_path="", tag_method="",
        direct_tag=False, create_time_only=False, policy_driven=True, ui_only=False,
        rollback=False, fallback=BUDGET_POLICY,
        reason="Apps attribute compute via a budget policy; no per-app custom_tags API."),
    "AI_GATEWAY": Capability(
        product="AI_GATEWAY", label="AI Gateway",
        read_path="billing only", write_path="", tag_method="",
        direct_tag=False, create_time_only=False, policy_driven=True, ui_only=False,
        rollback=False, fallback=BUDGET_POLICY,
        reason="No per-resource custom_tags write API; attribute via policy."),
    "INTERACTIVE": Capability(
        product="INTERACTIVE", label="Interactive / background",
        read_path="billing only", write_path="", tag_method="",
        direct_tag=False, create_time_only=False, policy_driven=True, ui_only=False,
        rollback=False, fallback=EXCEPTION_QUEUE,
        reason="Not an individually taggable resource; route to the exception queue."),
    "LAKEFLOW_CONNECT": Capability(
        product="LAKEFLOW_CONNECT", label="Lakeflow Connect (managed ingestion)",
        read_path="billing only", write_path="", tag_method="",
        direct_tag=False, create_time_only=False, policy_driven=True, ui_only=False,
        rollback=False, fallback=BUDGET_POLICY,
        reason="Managed ingestion attributes via policy, not a per-resource tag."),
}


# ---- derived views (so writer.py / queries.py reference ONE source) ----

def api_taggable_products() -> tuple[str, ...]:
    """Products with a direct per-resource tag write API (writer will attempt)."""
    return tuple(p for p, c in CAPABILITIES.items() if c.direct_tag)


def policy_governed_products() -> tuple[str, ...]:
    """Products attributed via budget/tag policy — never a per-resource write.
    Excludes SQL: SQL is only policy-governed when serverless (branch on the flag)."""
    return tuple(p for p, c in CAPABILITIES.items()
                 if c.policy_driven and not c.serverless_differs)


def policy_reasons() -> dict[str, str]:
    """product -> reason, for the surfaces that are (always) policy-governed,
    plus SQL (serverless case). Mirrors the old writer.POLICY_GOVERNED_REASON."""
    out = {p: CAPABILITIES[p].reason for p in policy_governed_products()}
    out["SQL"] = CAPABILITIES["SQL"].reason
    return out


def tag_method(product: str) -> str:
    """writer._WRITERS key for a product, or '' if none."""
    c = CAPABILITIES.get(product)
    return c.tag_method if c else ""


def get(product: str) -> Capability | None:
    return CAPABILITIES.get(product)


def matrix() -> list[dict]:
    """Full capability matrix as plain dicts, for the /capabilities API + UI."""
    return [
        {
            "product": c.product, "label": c.label,
            "read_path": c.read_path, "write_path": c.write_path,
            "direct_tag": c.direct_tag, "create_time_only": c.create_time_only,
            "policy_driven": c.policy_driven, "ui_only": c.ui_only,
            "rollback": c.rollback, "fallback": c.fallback,
            "serverless_differs": c.serverless_differs, "reason": c.reason,
        }
        for c in CAPABILITIES.values()
    ]
