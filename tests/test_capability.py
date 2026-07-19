"""Tests for the declarative capability registry (Phase 1).

Locks the invariants that used to be hand-maintained in two files, so an edit to
the registry can't silently diverge writer.py from queries.py.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src",
                                "tag_governance", "backend"))

import capability as cap  # noqa: E402


def test_policy_governed_matches_known_set():
    assert set(cap.policy_governed_products()) == {
        "APPS", "AI_GATEWAY", "INTERACTIVE", "LAKEFLOW_CONNECT"}


def test_api_taggable_matches_writer_set():
    # Products with a real per-resource writer, excluding UI-only (DLT) and the
    # serverless-dependent SQL surface.
    api = tuple(p for p in cap.api_taggable_products()
                if not (cap.get(p) and (cap.get(p).ui_only or cap.get(p).serverless_differs)))
    assert set(api) == {
        "JOBS", "ALL_PURPOSE", "MODEL_SERVING", "LAKEBASE", "VECTOR_SEARCH", "DATABASE"}


def test_policy_reasons_cover_governed_plus_sql():
    assert set(cap.policy_reasons().keys()) == {
        "APPS", "AI_GATEWAY", "INTERACTIVE", "LAKEFLOW_CONNECT", "SQL"}


def test_every_direct_tag_product_has_a_tag_method():
    for p, c in cap.CAPABILITIES.items():
        if c.direct_tag:
            assert c.tag_method, f"{p} is direct_tag but has no tag_method"


def test_non_direct_products_have_a_fallback():
    for p, c in cap.CAPABILITIES.items():
        if not c.direct_tag:
            assert c.fallback in (cap.BUDGET_POLICY, cap.CREATE_TIME,
                                  cap.UI_ONLY, cap.EXCEPTION_QUEUE), \
                f"{p} not direct_tag but fallback={c.fallback!r}"


def test_sql_is_the_only_serverless_dependent_surface():
    flagged = [p for p, c in cap.CAPABILITIES.items() if c.serverless_differs]
    assert flagged == ["SQL"]


def test_matrix_is_json_serializable_and_complete():
    m = cap.matrix()
    assert len(m) == len(cap.CAPABILITIES)
    required = {"product", "label", "read_path", "write_path", "direct_tag",
                "create_time_only", "policy_driven", "ui_only", "rollback",
                "fallback", "serverless_differs", "reason"}
    for row in m:
        assert required <= set(row.keys())
