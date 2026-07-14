"""Databricks SQL connectivity for the Tag Governance app.

Framework-agnostic (no Streamlit). A lazily-opened connection is reused so we
don't re-do the OAuth handshake per request, but it's guarded so a DROPPED
connection (warehouse idle-stop, network blip, token refresh) self-heals: a
failing statement invalidates the cached connection and retries once on a fresh
one, instead of wedging the app until restart.

Auth model:
  - On Databricks Apps, the service principal creds are auto-injected and
    databricks.sdk.core.Config() picks them up; the warehouse id arrives via the
    DATABRICKS_WAREHOUSE_ID env var (declared as an app resource).
  - Locally, Config() resolves DATABRICKS_CONFIG_PROFILE / the default profile.
"""
from __future__ import annotations

import logging
import os
import threading
from decimal import Decimal

log = logging.getLogger("tag_governance.db")

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


def _open():
    from databricks import sql
    from databricks.sdk.core import Config
    cfg = Config()
    return sql.connect(
        server_hostname=cfg.host,
        http_path=_http_path(),
        credentials_provider=lambda: cfg.authenticate,
    )


def get_connection(force_new: bool = False):
    """Return the shared SQL connection, opening it if needed (thread-safe).
    force_new drops any cached connection first (used by the reconnect path)."""
    global _conn
    if force_new:
        with _lock:
            _close_locked()
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is None:
            _conn = _open()
    return _conn


def _close_locked():
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:  # noqa: BLE001 — best effort
            pass
        _conn = None


def _coerce(v):
    """Decimals -> float so JSON serialization is clean; leave everything else."""
    return float(v) if isinstance(v, Decimal) else v


def _looks_like_conn_error(e: Exception) -> bool:
    s = f"{type(e).__name__}: {e}".lower()
    return any(k in s for k in (
        "closed", "broken", "connection", "session", "timeout", "expired",
        "reset", "eof", "unauthenticated", "token"))


def _run(sql_text: str, fn):
    """Execute fn(cursor) with one reconnect retry on a connection-type failure."""
    for attempt in (1, 2):
        conn = get_connection(force_new=(attempt == 2))
        try:
            with conn.cursor() as cur:
                cur.execute(sql_text)
                return fn(cur)
        except Exception as e:  # noqa: BLE001
            if attempt == 1 and _looks_like_conn_error(e):
                log.warning("SQL connection error (%s) — reconnecting and retrying once", e)
                continue
            raise


def run_query(sql_text: str) -> list[dict]:
    """Run a read query and return a list of dict rows (JSON-friendly)."""
    def _read(cur):
        cols = [c[0] for c in cur.description]
        return [{c: _coerce(v) for c, v in zip(cols, r)} for r in cur.fetchall()]
    return _run(sql_text, _read)


def run_exec(sql_text: str) -> int:
    """Run a write statement (INSERT/DELETE/MERGE). Returns affected rows or -1."""
    def _exec(cur):
        try:
            return cur.rowcount if cur.rowcount is not None else -1
        except Exception:  # noqa: BLE001
            return -1
    return _run(sql_text, _exec)
