"""AWS Databricks tag validation — spec-accurate rules.

Databricks tag constraints on AWS (and the workspace/resource split):

  * At most 20 tags per resource.
  * Keys are unique; exactly one value per key.
  * Key length 1..127; value length 0..255 (empty value IS allowed).
  * UTF-8 is allowed in keys and values.
  * An empty tag payload must NEVER be treated as "delete all tags" unless the
    caller explicitly confirms it (guards against wiping a resource's tags by
    sending {}).

This module is the single source of truth for those rules. Two profiles are
provided because workspace tags and per-resource (jobs/compute/…) tags have the
same numeric limits today but are conceptually distinct write surfaces; keeping
them separate lets the limits diverge later without touching callers.

Raises ``TagValidationError`` (caught at the API boundary → HTTP 400). Never
mutates input.
"""
from __future__ import annotations

from dataclasses import dataclass

# Databricks-reserved / AWS-reserved key prefixes that customers may not set.
# "aws:" is reserved by AWS; "databricks" by the platform. Kept minimal and
# documented so the list is auditable rather than a magic constant.
_RESERVED_KEY_PREFIXES = ("aws:", "databricks")


class TagValidationError(ValueError):
    """A tag key/value/payload violated the AWS Databricks tag rules."""


@dataclass(frozen=True)
class TagProfile:
    """Numeric + charset limits for one tag write surface."""
    name: str
    max_tags: int = 20
    key_min: int = 1
    key_max: int = 127
    value_min: int = 0
    value_max: int = 255
    allow_reserved_prefixes: bool = False


# Per-resource tags (jobs, clusters, warehouses, serving, …).
RESOURCE_TAGS = TagProfile(name="resource")
# Workspace-level tags. Same numeric limits on AWS today; separate profile so
# the workspace surface can enforce its own rules independently going forward.
WORKSPACE_TAGS = TagProfile(name="workspace")


def _utf8_len(s: str) -> int:
    """Length in Unicode code points. UTF-8 is allowed, so we validate by
    character count (matching how the tag length limits are expressed), not by
    encoded byte count."""
    return len(s)


def validate_key(key: str, profile: TagProfile = RESOURCE_TAGS) -> None:
    if not isinstance(key, str):
        raise TagValidationError(f"Tag key must be a string, got {type(key).__name__}.")
    n = _utf8_len(key)
    if n < profile.key_min or n > profile.key_max:
        raise TagValidationError(
            f"Tag key {key!r} length {n} out of range "
            f"[{profile.key_min}, {profile.key_max}]."
        )
    if not profile.allow_reserved_prefixes:
        low = key.lower()
        for pfx in _RESERVED_KEY_PREFIXES:
            if low.startswith(pfx):
                raise TagValidationError(
                    f"Tag key {key!r} uses reserved prefix {pfx!r}."
                )


def validate_value(value: str, profile: TagProfile = RESOURCE_TAGS) -> None:
    # Empty string is a VALID value (length 0). None is not — that's "remove",
    # which callers must express explicitly, not via validation.
    if value is None or not isinstance(value, str):
        raise TagValidationError(
            "Tag value must be a string (use the remove/rollback path to delete a key)."
        )
    n = _utf8_len(value)
    if n < profile.value_min or n > profile.value_max:
        raise TagValidationError(
            f"Tag value {value!r} length {n} out of range "
            f"[{profile.value_min}, {profile.value_max}]."
        )


def validate_pair(key: str, value: str, profile: TagProfile = RESOURCE_TAGS) -> None:
    """Validate one key/value pair against a profile."""
    validate_key(key, profile)
    validate_value(value, profile)


def validate_payload(
    tags: dict[str, str],
    profile: TagProfile = RESOURCE_TAGS,
    *,
    allow_empty: bool = False,
) -> None:
    """Validate a full tag map destined for one resource.

    Enforces the max-tag ceiling, unique keys (dict guarantees this, but we
    reject non-dict input), per-pair rules, AND the empty-payload guard: an
    empty ``tags`` is rejected unless ``allow_empty=True`` is passed to signal
    the caller has explicitly confirmed a clear-all. This is the guardrail
    against silently wiping a resource's tags with ``{}``.
    """
    if not isinstance(tags, dict):
        raise TagValidationError(f"Tag payload must be a map, got {type(tags).__name__}.")
    if not tags:
        if allow_empty:
            return
        raise TagValidationError(
            "Empty tag payload rejected: this would not add any tag, and is "
            "never treated as delete-all unless explicitly confirmed."
        )
    if len(tags) > profile.max_tags:
        raise TagValidationError(
            f"{len(tags)} tags exceeds the {profile.max_tags}-tag limit for "
            f"{profile.name} tags."
        )
    for k, v in tags.items():
        validate_pair(k, v, profile)
