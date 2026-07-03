"""Per-workspace WorkspaceClient resolution — shared by the writer and rollback jobs.

The write jobs run in ONE home workspace but may need to tag resources in OTHER
workspaces of the same account. Reading billing is account-wide; WRITING a tag to
a resource in workspace X requires an identity valid IN workspace X.

Two modes, chosen by whether account-SP OAuth creds are present:

  M2 (cross-workspace) — env has DATABRICKS_ACCOUNT_ID + DATABRICKS_CLIENT_ID +
    DATABRICKS_CLIENT_SECRET for an ACCOUNT-level service principal. We build an
    AccountClient once and use AccountClient.get_workspace_client(ws) to mint a
    workspace-scoped client per target workspace (the SDK swaps host to the
    workspace deployment URL and re-runs OAuth against that workspace).

  M1 (home-only) — no account creds. We can only act in the home workspace. Rows
    for any OTHER workspace are refused (returned as an error) rather than written
    against the home client, which could mis-tag a resource whose id collides.

The account SP must be (a) assigned to each target workspace and (b) hold
CAN_MANAGE on the resources it edits. See README "Cross-workspace writes (M2)".
"""
from __future__ import annotations

import os

_ACCOUNT_HOST = os.environ.get(
    "DATABRICKS_ACCOUNT_HOST", "https://accounts.cloud.databricks.com")


def account_creds_present() -> bool:
    """True when the env carries account-SP OAuth creds (enables M2)."""
    return all(os.environ.get(k) for k in
               ("DATABRICKS_ACCOUNT_ID", "DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET"))


# Keys expected inside the account-SP secret scope.
_SCOPE_KEYS = {
    "DATABRICKS_ACCOUNT_ID": "account_id",
    "DATABRICKS_CLIENT_ID": "client_id",
    "DATABRICKS_CLIENT_SECRET": "client_secret",
}


def load_account_creds_from_scope(scope: str, dbutils) -> bool:
    """Populate the account-SP env vars from a Databricks secret scope.

    Secrets are read at runtime (never passed through job argv, which is logged).
    Returns True if all three creds were loaded (M2 enabled), False otherwise
    (missing scope or key → stay in M1 home-only mode). Never raises.
    """
    if not scope:
        return False
    try:
        for env_key, secret_key in _SCOPE_KEYS.items():
            os.environ[env_key] = dbutils.secrets.get(scope=scope, key=secret_key)
        return account_creds_present()
    except Exception as e:  # noqa: BLE001 — missing scope/key just means stay in M1
        print(f"Account-SP secret scope '{scope}' not fully readable ({e}); staying in M1.")
        return False


class ClientResolver:
    """Resolves workspace_id -> WorkspaceClient, caching the account client and
    per-workspace clients. One instance per job run (holds the home client)."""

    def __init__(self, home_client, home_workspace_id: str | None = None):
        self._home = home_client
        self._home_ws = str(home_workspace_id) if home_workspace_id else None
        self._account = None
        self._ws_index = None          # workspace_id(str) -> Workspace
        self._cache: dict[str, object] = {}
        self._m2 = account_creds_present()

    @property
    def cross_workspace(self) -> bool:
        return self._m2

    def _account_client(self):
        if self._account is None:
            from databricks.sdk import AccountClient
            self._account = AccountClient(
                host=_ACCOUNT_HOST,
                account_id=os.environ["DATABRICKS_ACCOUNT_ID"],
                client_id=os.environ["DATABRICKS_CLIENT_ID"],
                client_secret=os.environ["DATABRICKS_CLIENT_SECRET"],
            )
        return self._account

    def _index(self) -> dict:
        if self._ws_index is None:
            self._ws_index = {
                str(ws.workspace_id): ws
                for ws in self._account_client().workspaces.list()
            }
        return self._ws_index

    def client_for(self, workspace_id: str):
        """Return a WorkspaceClient for workspace_id, or raise if unreachable.

        Home workspace always uses the home client. Foreign workspaces require M2;
        under M1 this raises so the caller records FAILED instead of mis-tagging.
        """
        wid = str(workspace_id)
        if self._home_ws and wid == self._home_ws:
            return self._home
        if not self._m2:
            # Home-only mode: never write a foreign workspace with the home client.
            raise RuntimeError(
                f"workspace {wid} is not the home workspace and no account-SP creds "
                f"are configured (M1). Set DATABRICKS_ACCOUNT_ID/CLIENT_ID/CLIENT_SECRET "
                f"to enable cross-workspace writes."
            )
        if wid not in self._cache:
            ws = self._index().get(wid)
            if ws is None:
                raise RuntimeError(f"workspace {wid} not found in account workspace listing")
            self._cache[wid] = self._account_client().get_workspace_client(ws)
        return self._cache[wid]
