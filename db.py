"""Databricks SQL connectivity for the Tag Governance app.

Framework-agnostic (no Streamlit). A single module-level connection is reused so
we don't re-do the OAuth handshake on every request.

Auth model:
  - On Databricks Apps, the service principal credentials are auto-injected and
    databricks.sdk.core.Config() picks them up; the warehouse id arrives via the
    DATABRICKS_WAREHOUSE_ID env var (declared as an app resource).
  - Locally, Config() resolves DATABRICKS_CONFIG_PROFILE / the default profile.
"""
from __future__ import annotations

import os
import threading
from decimal import Decimal

_conn = None
_lock = threading.Lock()


def _http_path() -> str:
    wid = os.environ.get("DATABRICKS_WAREHOUSE_ID")
    if not wid:
        raise RuntimeError(
            "DATABRICKS_WAREHOUSE_ID is not set. Locally: export it before running; "
            "on Databricks Apps: declare a sql-warehouse resource."
        )
    return f"/sql/1.0/warehouses/{wid}"


def get_connection():
    """Lazily open one shared SQL connection (thread-safe)."""
    global _conn
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is None:
            from databricks import sql
            from databricks.sdk.core import Config

            cfg = Config()
            _conn = sql.connect(
                server_hostname=cfg.host,
                http_path=_http_path(),
                credentials_provider=lambda: cfg.authenticate,
            )
    return _conn


def _coerce(v):
    """Decimals -> float so JSON serialization is clean; leave everything else."""
    return float(v) if isinstance(v, Decimal) else v


def run_query(sql_text: str) -> list[dict]:
    """Run a read query and return a list of dict rows (JSON-friendly)."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(sql_text)
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
    return [{c: _coerce(v) for c, v in zip(cols, r)} for r in rows]


def run_exec(sql_text: str) -> int:
    """Run a write statement (INSERT/DELETE/MERGE). Returns affected rows or -1."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(sql_text)
        try:
            return cur.rowcount if cur.rowcount is not None else -1
        except Exception:
            return -1
