"""Trigger the writer / rollback Databricks Jobs from the app.

This is what makes auto-tagging *seamless*: instead of telling the user to run a
CLI command, the app kicks off the tag-governance-writer job itself (dry-run by
default) and hands back the run URL. The heavy, rate-limited, resumable writing
still happens in the job — the app only presses the button.

Jobs are found by NAME (tag-governance-writer / -rollback) so this keeps working
across redeploys without hardcoding job ids.
"""
from __future__ import annotations

import os
from functools import lru_cache

WRITER_JOB = "tag-governance-writer"
ROLLBACK_JOB = "tag-governance-rollback"


@lru_cache(maxsize=1)
def _client():
    from databricks.sdk import WorkspaceClient
    return WorkspaceClient()


@lru_cache(maxsize=8)
def _job_id(name: str) -> int:
    w = _client()
    for j in w.jobs.list(name=name):
        return j.job_id
    raise RuntimeError(f"Job '{name}' not found — deploy the bundle first.")


def _run_url(run_id: int) -> str:
    host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
    if not host:
        try:
            host = _client().config.host.rstrip("/")
        except Exception:
            host = ""
    return f"{host}/jobs/runs/{run_id}" if host else str(run_id)


def run_writer(batch_id: str, dry_run: bool = True) -> dict:
    """Kick off the writer job for one batch. Non-blocking — returns immediately."""
    w = _client()
    run = w.jobs.run_now(
        job_id=_job_id(WRITER_JOB),
        job_parameters={"batch_id": batch_id, "dry_run": "true" if dry_run else "false"},
    )
    return {"run_id": run.run_id, "url": _run_url(run.run_id),
            "dry_run": dry_run, "batch_id": batch_id}


def run_rollback(batch_id: str, dry_run: bool = True) -> dict:
    """Kick off the rollback job for one batch. Non-blocking."""
    w = _client()
    run = w.jobs.run_now(
        job_id=_job_id(ROLLBACK_JOB),
        job_parameters={"batch_id": batch_id, "dry_run": "true" if dry_run else "false"},
    )
    return {"run_id": run.run_id, "url": _run_url(run.run_id),
            "dry_run": dry_run, "batch_id": batch_id}
