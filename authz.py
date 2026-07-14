"""Authorization for live tag writes.

Reading and dry-runs are open to any app user. LIVE writes (which mutate real
resources across the workspace) are gated: the requesting user must belong to a
configured allowed group. Databricks Apps forwards the end user's identity and a
user-scoped token in request headers, so we check membership as THAT user.

Config (env):
  TAG_GOVERNANCE_ADMIN_GROUP  — group whose members may run live writes.
                                Unset/empty = OPEN (dev only) — logged loudly.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("tag_governance.authz")

# "OPEN" (or empty) means no restriction — dev only. Set to a real group name for prod.
_raw_group = os.environ.get("TAG_GOVERNANCE_ADMIN_GROUP", "").strip()
ADMIN_GROUP = "" if _raw_group.upper() == "OPEN" else _raw_group


def requester_email(headers) -> str:
    """The end user's email from the Apps proxy (empty if running outside Apps)."""
    return (headers.get("x-forwarded-email") or "").strip()


def _user_groups(token: str) -> set[str]:
    """Groups for the user behind `token`, via SCIM Me using their own token so
    we authorize the actual requester, not the app service principal."""
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.core import Config
    host = os.environ.get("DATABRICKS_HOST") or Config().host
    w = WorkspaceClient(host=host, token=token, auth_type="pat")
    me = w.current_user.me()
    return {g.display for g in (me.groups or []) if g.display}


def authorize_write(headers) -> tuple[bool, str]:
    """(allowed, reason). Allowed when no admin group is configured (open dev mode)
    or the requester is a member of it. Never raises — a lookup failure denies."""
    if not ADMIN_GROUP:
        log.warning("TAG_GOVERNANCE_ADMIN_GROUP unset — live writes are OPEN to any "
                    "app user. Set it before production use.")
        return True, "open (no admin group configured)"
    token = (headers.get("x-forwarded-access-token") or "").strip()
    email = requester_email(headers) or "unknown"
    if not token:
        return False, ("cannot verify group membership (no forwarded user token); "
                       "live writes require running behind Databricks Apps auth")
    try:
        groups = _user_groups(token)
    except Exception as e:  # noqa: BLE001 — fail closed
        log.warning("group lookup failed for %s: %s", email, e)
        return False, "could not verify your group membership; live write denied"
    if ADMIN_GROUP in groups:
        return True, f"{email} is in {ADMIN_GROUP}"
    return False, (f"{email} is not in '{ADMIN_GROUP}' — live tag writes are "
                   f"restricted to that group. You can still preview/dry-run.")
