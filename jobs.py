"""Trigger the writer / rollback Databricks Jobs from the app.

This is what makes auto-tagging *seamless*: instead of telling the user to run a
CLI command, the app kicks off the tag-governance-writer job itself (dry-run by
default) and hands back the run URL. The heavy, rate-limited, resumable writing
still happens in the job — the app only presses the button.

Jobs are found by NAME (tag-governance-writer / -rollback) so this keeps working
across redeploys without hardcoding job ids.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

log = logging.getLogger("tag_governance.jobs")

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


def _host() -> str:
    host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
    if not host:
        try:
            host = _client().config.host.rstrip("/")
        except Exception:
            host = ""
    # Guarantee a scheme — without https:// the browser treats the URL as a
    # relative path and the run link 404s. config.host / the env var sometimes
    # arrive bare (host only).
    if host and not host.startswith("http"):
        host = "https://" + host
    return host


def _run_url(job_id: int, run_id: int) -> str:
    """A run URL that actually resolves. Databricks run pages are keyed by BOTH
    job_id and run_id — /jobs/runs/{run_id} alone 404s. Use the canonical
    /jobs/{job_id}/runs/{run_id} form."""
    host = _host()
    return f"{host}/jobs/{job_id}/runs/{run_id}" if host else str(run_id)


def run_writer(batch_id: str, dry_run: bool = True) -> dict:
    """Kick off the writer job for one batch. Non-blocking — returns immediately."""
    w = _client()
    job_id = _job_id(WRITER_JOB)
    run = w.jobs.run_now(
        job_id=job_id,
        job_parameters={"batch_id": batch_id, "dry_run": "true" if dry_run else "false"},
    )
    log.info("writer triggered batch=%s dry_run=%s run_id=%s", batch_id, dry_run, run.run_id)
    return {"run_id": run.run_id, "url": _run_url(job_id, run.run_id),
            "dry_run": dry_run, "batch_id": batch_id}


def run_rollback(batch_id: str, dry_run: bool = True) -> dict:
    """Kick off the rollback job for one batch. Non-blocking."""
    w = _client()
    job_id = _job_id(ROLLBACK_JOB)
    run = w.jobs.run_now(
        job_id=job_id,
        job_parameters={"batch_id": batch_id, "dry_run": "true" if dry_run else "false"},
    )
    log.info("rollback triggered batch=%s dry_run=%s run_id=%s", batch_id, dry_run, run.run_id)
    return {"run_id": run.run_id, "url": _run_url(job_id, run.run_id),
            "dry_run": dry_run, "batch_id": batch_id}
