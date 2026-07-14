"""Core-logic tests for tag-governance.

Uses stdlib unittest (no pip needed) so it runs anywhere, incl. CI. Covers the
security- and correctness-critical pure logic: SQL escaping, tag validation,
taggability parity with the writer, plan rejection, and tag-merge semantics.
Nothing here touches a warehouse — it's all string/logic assertions.

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

# Import the app modules with table env vars set to harmless placeholders so
# queries.py resolves without a live workspace.
os.environ.setdefault("TAG_GOVERNANCE_SUMMARY_TABLE", "t.t.summary")
os.environ.setdefault("TAG_GOVERNANCE_QUEUE_TABLE", "t.t.queue")
os.environ.setdefault("TAG_GOVERNANCE_AUDIT_TABLE", "t.t.audit")
os.environ.setdefault("TAG_GOVERNANCE_SUGGESTIONS_TABLE", "t.t.sugg")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import queries      # noqa: E402
import tagging      # noqa: E402
import writer       # noqa: E402


class TestSqlEscaping(unittest.TestCase):
    def test_sql_str_doubles_quotes(self):
        self.assertEqual(queries._sql_str("O'Brien"), "'O''Brien'")

    def test_sql_str_neutralizes_injection(self):
        out = queries._sql_str("x'; DROP TABLE t; --")
        # wrapped in quotes, and the embedded quote is doubled so it can't break out
        self.assertTrue(out.startswith("'") and out.endswith("'"))
        self.assertIn("x''; DROP TABLE t; --", out)

    def test_sql_str_escapes_backslash(self):
        # trailing backslash must not escape the closing quote
        self.assertEqual(queries._sql_str("a\\"), "'a\\\\'")

    def test_sql_str_strips_newlines_nul(self):
        self.assertNotIn("\n", queries._sql_str("a\nb"))
        self.assertNotIn("\x00", queries._sql_str("a\x00b"))


class TestTagValidation(unittest.TestCase):
    def setUp(self):
        # imported lazily so app.py (FastAPI) import cost is paid once
        import app
        self.app = app

    def test_accepts_legit_values(self):
        for v in ["data-eng", "cost_center_1", "team/ml", "a.b:c", "Finance 2025"]:
            self.app._validate_tag("cost_center", v)  # should not raise

    def test_rejects_injection(self):
        with self.assertRaises(self.app.BadInput):
            self.app._validate_tag("cost_center", "x'; DROP TABLE t; --")

    def test_rejects_empty_and_overlong(self):
        with self.assertRaises(self.app.BadInput):
            self.app._validate_tag("cost_center", "")
        with self.assertRaises(self.app.BadInput):
            self.app._validate_tag("cost_center", "x" * 256)

    def test_rejects_newline(self):
        with self.assertRaises(self.app.BadInput):
            self.app._validate_tag("cost_center", "a\nb")


class TestTaggabilityParity(unittest.TestCase):
    """queries._is_taggable must agree with writer.attempt_write's dispatch:
    the app must not enqueue what the writer would reject."""

    def test_policy_governed_not_taggable(self):
        for p in ("APPS", "AI_GATEWAY", "INTERACTIVE", "LAKEFLOW_CONNECT"):
            self.assertFalse(tagging._is_taggable(p), p)

    def test_serverless_sql_not_taggable_but_provisioned_is(self):
        self.assertFalse(tagging._is_taggable("SQL", is_serverless=True))
        self.assertTrue(tagging._is_taggable("SQL", is_serverless=False))

    def test_dlt_not_taggable(self):
        self.assertFalse(tagging._is_taggable("DLT"))

    def test_jobs_and_clusters_taggable(self):
        self.assertTrue(tagging._is_taggable("JOBS"))
        self.assertTrue(tagging._is_taggable("ALL_PURPOSE"))

    def test_writer_policy_set_matches(self):
        # every product tagging treats as policy-governed is UNSUPPORTED in writer
        for p in tagging.POLICY_GOVERNED:
            self.assertIn(p, writer.POLICY_GOVERNED_REASON, p)


class TestPlanRejection(unittest.TestCase):
    def test_plan_for_rejects_apps_with_reason(self):
        p = tagging.plan_for("APPS", "id1", "an-app", {"cost_center": "x"})
        self.assertFalse(p.supported)
        self.assertTrue(p.warnings and "budget policy" in p.warnings[0].lower())

    def test_plan_for_rejects_serverless_sql(self):
        p = tagging.plan_for("SQL", "w1", "wh", {"cost_center": "x"}, is_serverless=True)
        self.assertFalse(p.supported)

    def test_plan_for_accepts_jobs(self):
        p = tagging.plan_for("JOBS", "123", "job", {"cost_center": "x"})
        self.assertTrue(p.supported)

    def test_plan_for_unknown_product_unsupported(self):
        p = tagging.plan_for("WISŌ_MADE_UP", "x", "y", {"cost_center": "z"})
        self.assertFalse(p.supported)


class TestApplyMerge(unittest.TestCase):
    def test_set_new_key(self):
        old, new, noop = writer._apply_merge({"a": "1"}, "cost_center", "data-eng")
        self.assertIsNone(old)
        self.assertEqual(new, {"a": "1", "cost_center": "data-eng"})
        self.assertFalse(noop)

    def test_overwrite_existing_value(self):
        old, new, noop = writer._apply_merge({"cost_center": "old"}, "cost_center", "new")
        self.assertEqual(old, "old")
        self.assertEqual(new["cost_center"], "new")
        self.assertFalse(noop)

    def test_noop_when_value_identical(self):
        old, new, noop = writer._apply_merge({"cost_center": "x"}, "cost_center", "x")
        self.assertTrue(noop)

    def test_remove_key(self):
        old, new, noop = writer._apply_merge({"cost_center": "x", "b": "y"}, "cost_center", None)
        self.assertEqual(old, "x")
        self.assertNotIn("cost_center", new)
        self.assertIn("b", new)
        self.assertFalse(noop)

    def test_preserves_other_tags(self):
        old, new, noop = writer._apply_merge(
            {"created_by": "ci", "team": "x"}, "cost_center", "data-eng")
        self.assertEqual(new["created_by"], "ci")
        self.assertEqual(new["team"], "x")


class TestRulePredicate(unittest.TestCase):
    def test_equals_escapes_quote(self):
        sql = queries._rule_sql_predicate(
            {"field": "owner", "op": "equals", "value": "o'brien", "tags": {}})
        self.assertIn("o''brien", sql)

    def test_contains_uses_like_escape(self):
        sql = queries._rule_sql_predicate(
            {"field": "name", "op": "contains", "value": "data_eng", "tags": {}})
        self.assertIn("LIKE", sql)
        self.assertIn("ESCAPE", sql)  # underscore must be escaped, not a wildcard

    def test_taggable_predicate_excludes_policy_products(self):
        pred = queries._taggable_predicate("product", "is_serverless")
        for p in ("APPS", "AI_GATEWAY", "INTERACTIVE", "LAKEFLOW_CONNECT"):
            self.assertIn(f"'{p}'", pred)


if __name__ == "__main__":
    unittest.main(verbosity=2)
