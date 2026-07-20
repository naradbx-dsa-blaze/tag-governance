"""Tests that the dynamic (user-input) query builders use BOUND PARAMETERS
rather than interpolating user values into the SQL text.

This locks in the query-builder refactor: a regression back to f-string
interpolation (the class of bug that caused the correlated-subquery collision
and the escaping foot-guns) would fail these tests, because the user value would
reappear in the SQL string instead of only in the params dict.
"""
import os
import sys

# Backend modules import each other bare (sys.path seam via backend/__init__).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src",
                                "tag_governance", "backend"))
# queries.py reads table names from env at import — set sentinels so it imports.
os.environ.setdefault("TAG_GOVERNANCE_SUMMARY_TABLE", "cat.sch.workload_daily")
os.environ.setdefault("TAG_GOVERNANCE_QUEUE_TABLE", "cat.sch.write_queue")
os.environ.setdefault("TAG_GOVERNANCE_AUDIT_TABLE", "cat.sch.audit")
os.environ.setdefault("TAG_GOVERNANCE_SUGGESTIONS_TABLE", "cat.sch.suggestions")
os.environ.setdefault("TAG_GOVERNANCE_INVENTORY_TABLE", "cat.sch.inventory")

import db  # noqa: E402
import queries  # noqa: E402

# A value packed with SQL-hostile characters: quote, LIKE metachars, backslash.
NASTY = "O'Brien%_\\-- drop"


def _is_query(q):
    assert isinstance(q, db.Query), f"expected Query, got {type(q)}"
    return q


def test_kpi_summary_binds_tag_key_and_days():
    q = _is_query(queries.kpi_summary(30, [NASTY]))
    assert NASTY not in q.sql            # value not interpolated
    assert NASTY in q.params.values()    # value is a bound param
    assert 30 in q.params.values()


def test_rule_impact_binds_rule_value_not_interpolated():
    rules = [{"field": "owner", "op": "contains", "value": NASTY,
              "tags": {"cost_center": "platform"}}]
    q = _is_query(queries.bulk_rule_impact(30, ["cost_center"], rules))
    assert NASTY not in q.sql
    assert "O'Brien" not in q.sql   # the quote never reaches the SQL text
    # the value lives in a bound param (LIKE metachars escaped into the pattern),
    # so a recognizable, non-metachar slice of it is present in params, not the SQL.
    assert any("Brien" in str(v) for v in q.params.values())


def test_not_already_handled_requires_qualified_columns():
    # Bare column names would bind to the inner `handled` alias and silently break
    # the guard — the helper must reject them.
    import pytest
    with pytest.raises(AssertionError):
        queries._not_already_handled("cost_center", "product", "workload_id")
    # qualified is fine
    sql = queries._not_already_handled("cost_center", "wl.product", "wl.workload_id")
    assert "wl.workload_id" in sql


def test_enqueue_explicit_binds_tag_value():
    wl = [{"workload_id": "123", "product": "JOBS", "workspace_id": "w",
           "workload_name": "n", "is_serverless": False, "tag_value": NASTY, "cost": 5.0}]
    q = _is_query(queries.enqueue_explicit_sql("b", "me", "cost_center", wl))
    assert NASTY not in q.sql
    assert NASTY in q.params.values()


def test_batch_detail_binds_batch_id():
    q = _is_query(queries.batch_failure_breakdown("batch-'; DROP TABLE x --"))
    assert "DROP TABLE" not in q.sql
    assert "batch-'; DROP TABLE x --" in q.params.values()


def test_static_query_still_plain_string():
    # freshness() takes no user input — left as a plain str, must not have regressed.
    assert isinstance(queries.freshness(), str)
