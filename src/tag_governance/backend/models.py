from typing import Any

from pydantic import BaseModel

from .. import __version__


class VersionOut(BaseModel):
    version: str

    @classmethod
    def from_metadata(cls):
        return cls(version=__version__)


# --- Read models (loose dict rows from the warehouse; typed at the edges) ---
class OverviewOut(BaseModel):
    days: int
    tag_key: str
    kpi: dict[str, Any]
    products: list[dict[str, Any]]


class PreviewOut(BaseModel):
    impact: dict[str, Any]
    workloads: list[dict[str, Any]]
    excluded: dict[str, Any]


class RulePreviewOut(BaseModel):
    impact: dict[str, Any]
    workloads: list[dict[str, Any]]


class RowsOut(BaseModel):
    rows: list[dict[str, Any]]


class BatchesOut(BaseModel):
    batches: list[dict[str, Any]]


class ValuesOut(BaseModel):
    values: list[dict[str, Any]]


class WhoAmIOut(BaseModel):
    email: str
    display_name: str | None = None
    can_write: bool
    reason: str
    admin_group: str


# --- Write request models ---
class RulePreviewBody(BaseModel):
    tag_key: str = "cost_center"
    days: int = 30
    rules: list[dict[str, Any]]


class AutoTagBody(BaseModel):
    tag_key: str = "cost_center"
    days: int = 30
    min_confidence: float = 0.8
    rules: list[dict[str, Any]] | None = None
    use_ai: bool = True
    dry_run: bool = True


class TagSelectedBody(BaseModel):
    tag_key: str = "cost_center"
    workloads: list[dict[str, Any]]
    dry_run: bool = True


class ManualTagBody(BaseModel):
    product: str
    workload_id: str
    workload_name: str = ""
    workspace_id: str = ""
    is_serverless: bool = False
    tag_key: str = "cost_center"
    tag_value: str
    list_cost: float | None = None
    dry_run: bool = True


class BatchBody(BaseModel):
    batch_id: str
    dry_run: bool = True


class RunOut(BaseModel):
    """Generic result carrying an optional triggered-job run + a status payload."""
    status: str | None = None
    batch_id: str | None = None
    total_rows: int | None = None
    rule_rows: int | None = None
    ai_rows: int | None = None
    run: dict[str, Any] | None = None
    message: str | None = None
    error: str | None = None
