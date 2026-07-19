from __future__ import annotations

import logging
import os
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse

from .core import Dependencies, create_router
from .models import (
    AutoTagBody, BatchBody, BatchesOut, ManualTagBody, OverviewOut, PreviewOut,
    RowsOut, RulePreviewBody, RulePreviewOut, RunOut, TagSelectedBody, ValuesOut,
    VersionOut, WhoAmIOut,
)

# The framework-agnostic logic modules (siblings on sys.path via __init__).
import authz
import capability
import db
import jobs
import queries
import tag_rules
import tagging

log = logging.getLogger("tag_governance.router")
router = create_router()

DEFAULT_DAYS = int(os.environ.get("TAG_GOVERNANCE_DAYS", "30"))


class BadInput(Exception):
    pass


def _validate_tag(key: str, value: str):
    """Validate one key/value pair against the AWS resource-tag rules
    (key 1-127, value 0-255, UTF-8, no reserved prefixes). Empty value is
    valid; None is not (removal goes through the rollback path)."""
    try:
        tag_rules.validate_pair(key, value, tag_rules.RESOURCE_TAGS)
    except tag_rules.TagValidationError as e:
        raise BadInput(str(e)) from e


def _err(e: Exception, status: int = 500) -> JSONResponse:
    eid = uuid.uuid4().hex[:12]
    log.exception("unhandled error [%s]: %s", eid, e)
    return JSONResponse({"error": f"Something went wrong. Ref id: {eid}"}, status_code=status)


def _gate(request: Request, dry_run: bool, action: str):
    """Stamp requester + authorize LIVE writes. Returns a 403 response or None."""
    tagging.set_requester(authz.requester_email(request.headers))
    if dry_run:
        log.info("action=%s mode=dry_run", action)
        return None
    ok, reason = authz.authorize_write(request.headers)
    log.info("action=%s mode=LIVE ok=%s reason=%s", action, ok, reason)
    return None if ok else JSONResponse({"error": reason, "denied": True}, status_code=403)


# --------------------------------------------------------------------- meta
@router.get("/version", response_model=VersionOut, operation_id="version")
async def version():
    return VersionOut.from_metadata()


@router.get("/whoami", response_model=WhoAmIOut, operation_id="whoami")
def whoami(request: Request):
    h = request.headers
    ok, reason = authz.authorize_write(h)
    return WhoAmIOut(email=authz.requester_email(h),
                     display_name=h.get("x-forwarded-preferred-username"),
                     can_write=ok, reason=reason,
                     admin_group=authz.ADMIN_GROUP or "(open — dev mode)")


@router.get("/capabilities", response_model=RowsOut, operation_id="capabilities")
def capabilities():
    """The resource capability matrix: for each product, its read/write path,
    direct-tag / create-time / policy / UI flags, rollback support, and the
    fallback pattern. Single source of truth (capability.py) — the UI renders
    this so the tag-ability story is never hand-maintained in two places."""
    return RowsOut(rows=capability.matrix())


# --------------------------------------------------------------------- reads
@router.get("/overview", response_model=OverviewOut, operation_id="overview")
def overview(days: int = DEFAULT_DAYS, tag_key: str = "cost_center"):
    kpi = db.run_query(queries.kpi_summary(days, [tag_key]))
    products = db.run_query(queries.by_product(days, [tag_key]))
    base = kpi[0] if kpi else {}
    adj = db.run_query(queries.tagged_live_adjustment(tag_key))
    a = adj[0] if adj else {}
    tagged_cost = float(a.get("cost") or 0)
    tagged_n = int(a.get("workloads") or 0)
    bu = float(base.get("untagged_cost") or 0)
    bt = float(base.get("total_cost") or 0)
    bn = int(base.get("untagged_workloads") or 0)
    live = max(0.0, bu - tagged_cost)
    base["untagged_cost"] = round(live)
    base["untagged_workloads"] = max(0, bn - tagged_n)
    base["pct_untagged"] = round(100.0 * live / bt, 1) if bt else 0
    base["tagged_live_cost"] = round(tagged_cost)
    base["tagged_live_workloads"] = tagged_n
    return OverviewOut(days=days, tag_key=tag_key, kpi=base, products=products)


@router.get("/preview", response_model=PreviewOut, operation_id="aiPreview")
def preview(days: int = DEFAULT_DAYS, tag_key: str = "cost_center", min_confidence: float = 0.8):
    impact = db.run_query(queries.suggestions_bulk_impact(tag_key, min_confidence, days))
    workloads = db.run_query(queries.suggestions_bulk_list(tag_key, min_confidence, days, limit=200))
    excluded = db.run_query(queries.policy_governed_impact(tag_key, min_confidence, days))
    return PreviewOut(impact=impact[0] if impact else {}, workloads=workloads,
                      excluded=excluded[0] if excluded else {})


@router.get("/batches", response_model=BatchesOut, operation_id="batches")
def batches(limit: int = 25):
    return BatchesOut(batches=db.run_query(queries.recent_batches(limit)))


@router.get("/batch-detail", response_model=RowsOut, operation_id="batchDetail")
def batch_detail(batch_id: str):
    return RowsOut(rows=db.run_query(queries.batch_failure_breakdown(batch_id)))


@router.get("/not-taggable", response_model=RowsOut, operation_id="notTaggable")
def not_taggable(days: int = DEFAULT_DAYS, tag_key: str = "cost_center"):
    return RowsOut(rows=db.run_query(queries.not_taggable_breakdown(tag_key, days)))


@router.get("/field-values", response_model=ValuesOut, operation_id="fieldValues")
def field_values(field: str = "owner", days: int = DEFAULT_DAYS):
    return ValuesOut(values=db.run_query(queries.distinct_field_values(field, days)))


# --------------------------------------------------------------------- writes
@router.post("/rule-preview", response_model=RulePreviewOut, operation_id="rulePreview")
def rule_preview(body: RulePreviewBody):
    impact = db.run_query(queries.bulk_rule_impact(body.days, [body.tag_key], body.rules))
    sample = db.run_query(queries.bulk_rule_sample(body.days, [body.tag_key], body.rules, limit=500))
    return RulePreviewOut(impact=impact[0] if impact else {}, workloads=sample)


@router.post("/auto-tag", response_model=RunOut, operation_id="autoTag")
def auto_tag(body: AutoTagBody, request: Request):
    denied = _gate(request, body.dry_run, "auto-tag")
    if denied:
        return denied
    try:
        for r in (body.rules or []):
            for k, v in (r.get("tags") or {}).items():
                _validate_tag(k, v)
    except BadInput as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    result = tagging.auto_tag(tag_key=body.tag_key, days=body.days, rules=body.rules,
                              min_confidence=body.min_confidence, use_ai=body.use_ai)
    if result.get("total_rows", 0) == 0:
        return RunOut(**result, run=None, message="Nothing matched.")
    run = jobs.run_writer(result["batch_id"], dry_run=body.dry_run)
    return RunOut(**result, run=run)


@router.post("/tag-selected", response_model=RunOut, operation_id="tagSelected")
def tag_selected(body: TagSelectedBody, request: Request):
    denied = _gate(request, body.dry_run, "tag-selected")
    if denied:
        return denied
    try:
        for w in (body.workloads or []):
            _validate_tag(body.tag_key, (w or {}).get("tag_value"))
    except BadInput as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    result = tagging.tag_selected(body.tag_key, body.workloads)
    if result.get("status") != "ENQUEUED" or result.get("total_rows", 0) == 0:
        return RunOut(**result, run=None, message=result.get("message", "No workloads selected."))
    run = jobs.run_writer(result["batch_id"], dry_run=body.dry_run)
    return RunOut(**result, run=run)


@router.post("/manual-tag", response_model=RunOut, operation_id="manualTag")
def manual_tag(body: ManualTagBody, request: Request):
    denied = _gate(request, body.dry_run, "manual-tag")
    if denied:
        return denied
    try:
        _validate_tag(body.tag_key, body.tag_value)
    except BadInput as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    plan = tagging.plan_for(product=body.product, workload_id=body.workload_id,
                            workload_name=body.workload_name,
                            tags={body.tag_key: body.tag_value},
                            is_serverless=body.is_serverless)
    res = tagging.enqueue_single(plan, workspace_id=body.workspace_id,
                                 is_serverless=body.is_serverless, list_cost=body.list_cost)
    if res.get("status") != "ENQUEUED":
        return RunOut(**res, run=None)
    run = jobs.run_writer(res["batch_id"], dry_run=body.dry_run)
    return RunOut(**res, run=run)


@router.post("/rollback", response_model=RunOut, operation_id="rollback")
def rollback(body: BatchBody, request: Request):
    denied = _gate(request, body.dry_run, "rollback")
    if denied:
        return denied
    run = jobs.run_rollback(body.batch_id, dry_run=body.dry_run)
    return RunOut(status="STARTED", batch_id=body.batch_id, run=run)
