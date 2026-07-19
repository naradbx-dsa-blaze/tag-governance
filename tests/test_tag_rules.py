"""Tests for the AWS Databricks tag validation rules (Phase 0)."""
import os
import sys

import pytest

# The backend modules import each other bare (sys.path seam via backend/__init__).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src",
                                "tag_governance", "backend"))

import tag_rules as tr  # noqa: E402


# ----- keys -----
def test_key_min_length():
    with pytest.raises(tr.TagValidationError):
        tr.validate_key("")


def test_key_at_127_ok_and_128_fails():
    tr.validate_key("k" * 127)              # boundary OK
    with pytest.raises(tr.TagValidationError):
        tr.validate_key("k" * 128)          # over the AWS 127 key limit


def test_key_reserved_prefixes_rejected():
    for bad in ("aws:foo", "AWS:Foo", "databricks_x", "Databricks"):
        with pytest.raises(tr.TagValidationError):
            tr.validate_key(bad)


def test_key_utf8_allowed():
    tr.validate_key("café")                 # non-ASCII must be allowed
    tr.validate_key("环境")


# ----- values -----
def test_empty_value_is_valid():
    # length-0 value is explicitly allowed by the spec.
    tr.validate_value("")


def test_value_at_255_ok_and_256_fails():
    tr.validate_value("v" * 255)
    with pytest.raises(tr.TagValidationError):
        tr.validate_value("v" * 256)


def test_none_value_rejected():
    # None means "remove" — must go through rollback, never validate as a value.
    with pytest.raises(tr.TagValidationError):
        tr.validate_value(None)


def test_value_utf8_allowed():
    tr.validate_value("naïve—value 环境")


# ----- payloads -----
def test_empty_payload_rejected_by_default():
    with pytest.raises(tr.TagValidationError):
        tr.validate_payload({})


def test_empty_payload_allowed_when_confirmed():
    # explicit confirm = the only way an empty map is accepted (no accidental delete-all).
    tr.validate_payload({}, allow_empty=True)


def test_max_20_tags_enforced():
    tr.validate_payload({f"k{i}": "v" for i in range(20)})   # exactly 20 OK
    with pytest.raises(tr.TagValidationError):
        tr.validate_payload({f"k{i}": "v" for i in range(21)})


def test_payload_validates_each_pair():
    with pytest.raises(tr.TagValidationError):
        tr.validate_payload({"k" * 200: "v"})   # bad key inside an otherwise-fine map


def test_workspace_profile_same_limits():
    tr.validate_payload({f"k{i}": "v" for i in range(20)}, tr.WORKSPACE_TAGS)
    with pytest.raises(tr.TagValidationError):
        tr.validate_payload({f"k{i}": "v" for i in range(21)}, tr.WORKSPACE_TAGS)
