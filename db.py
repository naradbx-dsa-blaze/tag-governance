"""Databricks SQL connectivity for the Tag Governance app.

Uses a single cached connection (st.cache_resource) so Streamlit's per-interaction
re-runs don't exhaust the connection pool with repeated OAuth handshakes.

Auth model:
  - On Databricks Apps, the service principal credentials are auto-injected and
    databricks.sdk.core.Config() picks them up; the warehouse id arrives via the
    DATABRICKS_WAREHOUSE_ID env var (declared as a resource in app.yaml).
  - Locally, Config() resolves the DATABRICKS_CONFIG_PROFILE / default profile.
"""
from __future__ import annotations

import os
import pandas as pd
import streamlit as st


def _http_path() -> str:
    wid = os.environ.get("DATABRICKS_WAREHOUSE_ID")
    if not wid:
        raise RuntimeError(
            "DATABRICKS_WAREHOUSE_ID is not set. Locally: export it before running; "
            "on Databricks Apps: declare a sql-warehouse resource in app.yaml."
        )
    return f"/sql/1.0/warehouses/{wid}"


@st.cache_resource(show_spinner=False)
def get_connection():
    from databricks import sql
    from databricks.sdk.core import Config

    cfg = Config()
    return sql.connect(
        server_hostname=cfg.host,
        http_path=_http_path(),
        credentials_provider=lambda: cfg.authenticate,
    )


@st.cache_data(ttl=300, show_spinner="Scanning workloads…")
def run_query(sql_text: str) -> pd.DataFrame:
    """Run a read query and return a DataFrame. Cached for 5 min per distinct SQL."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(sql_text)
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=cols)
    # Warehouse returns DECIMAL for cost/pct columns; Streamlit charts can't infer
    # a vega type from Python Decimal. Coerce decimals to float so charts/metrics
    # render cleanly without UserWarnings.
    from decimal import Decimal
    for c in df.columns:
        if len(df) and isinstance(df[c].iloc[0], Decimal):
            df[c] = df[c].astype(float)
    return df
