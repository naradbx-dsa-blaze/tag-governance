#!/usr/bin/env bash
# =============================================================================
# Tag Governance — M2 cross-workspace setup + smoke test
#
# Run this ONCE, as a Databricks ACCOUNT ADMIN, to enable the writer/rollback
# jobs to tag resources across ALL workspaces in the account (not just the home
# workspace they're deployed in).
#
# It is idempotent-ish and STOPS on the first error. It never tags a resource —
# the final step is a dry/smoke run that only proves reachability.
#
# What it does:
#   1. Create an account-level service principal (the cross-workspace identity).
#   2. Generate an OAuth (M2M) secret for it.
#   3. Assign it to each target workspace as a workspace admin.
#   4. Store account_id + client_id + client_secret in a Databricks secret scope.
#   5. Point the bundle jobs at that scope and run a SMOKE TEST (no writes).
#
# After this, grant the SP CAN_MANAGE on the resources you intend to tag (jobs,
# clusters, SQL warehouses, serving endpoints) — workspace-admin (step 3) is the
# simplest blanket grant and usually covers it.
#
# Prereqs: Databricks CLI v0.230+, authenticated to the ACCOUNT
#   (databricks auth login --host https://accounts.cloud.databricks.com \
#      --account-id <ACCOUNT_ID>), and a workspace profile for the deploy workspace.
# =============================================================================
set -euo pipefail

# ---- EDIT THESE ------------------------------------------------------------
ACCOUNT_ID="${ACCOUNT_ID:-<your-account-uuid>}"
SP_DISPLAY_NAME="${SP_DISPLAY_NAME:-tag-governance-writer}"
SECRET_SCOPE="${SECRET_SCOPE:-tag_governance_acct_sp}"
# Workspace IDs the writer should reach (space-separated). Find them in the
# Account Console → Workspaces, or: databricks account workspaces list
TARGET_WORKSPACE_IDS="${TARGET_WORKSPACE_IDS:-<ws_id_1> <ws_id_2>}"
# Profile for the WORKSPACE where the app/jobs are deployed (holds the scope).
WORKSPACE_PROFILE="${WORKSPACE_PROFILE:-DEFAULT}"
# Account-level CLI profile (accounts.cloud.databricks.com).
ACCOUNT_PROFILE="${ACCOUNT_PROFILE:-ACCOUNT}"
# ---------------------------------------------------------------------------

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[[ "$ACCOUNT_ID" == "<your-account-uuid>" ]] && die "Edit ACCOUNT_ID (and the other vars) at the top of this script first."

say "1/5  Create account service principal '$SP_DISPLAY_NAME'"
# Reuse if it already exists (match by displayName), else create.
SP_APP_ID=$(databricks account service-principals list --profile "$ACCOUNT_PROFILE" --output json \
  | python3 -c "import sys,json;[print(s['applicationId']) for s in json.load(sys.stdin) if s.get('displayName')=='$SP_DISPLAY_NAME']" | head -1 || true)
if [[ -z "${SP_APP_ID:-}" ]]; then
  SP_JSON=$(databricks account service-principals create --profile "$ACCOUNT_PROFILE" \
    --json "{\"displayName\":\"$SP_DISPLAY_NAME\"}")
  SP_APP_ID=$(echo "$SP_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['applicationId'])")
  SP_ID=$(echo "$SP_JSON"     | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
  echo "created SP applicationId=$SP_APP_ID (internal id=$SP_ID)"
else
  SP_ID=$(databricks account service-principals list --profile "$ACCOUNT_PROFILE" --output json \
    | python3 -c "import sys,json;[print(s['id']) for s in json.load(sys.stdin) if s.get('applicationId')=='$SP_APP_ID']" | head -1)
  echo "reusing existing SP applicationId=$SP_APP_ID (internal id=$SP_ID)"
fi

say "2/5  Generate an OAuth secret for the SP"
SECRET_JSON=$(databricks account service-principal-secrets create "$SP_ID" --profile "$ACCOUNT_PROFILE")
CLIENT_SECRET=$(echo "$SECRET_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['secret'])")
[[ -z "$CLIENT_SECRET" ]] && die "Failed to read the generated client secret."
echo "OAuth secret generated (shown once — this script stores it in step 4)."

say "3/5  Assign the SP to each target workspace (workspace admin)"
for WS in $TARGET_WORKSPACE_IDS; do
  echo "  assigning to workspace $WS ..."
  databricks account workspace-assignment update "$WS" "$SP_ID" --profile "$ACCOUNT_PROFILE" \
    --json '{"permissions":["ADMIN"]}' >/dev/null
done
echo "assigned to: $TARGET_WORKSPACE_IDS"
echo "NOTE: workspace ADMIN covers tag writes. If you prefer least-privilege, grant"
echo "      the SP CAN_MANAGE on the specific jobs/clusters/warehouses/endpoints instead."

say "4/5  Store creds in secret scope '$SECRET_SCOPE' (workspace)"
databricks secrets create-scope "$SECRET_SCOPE" --profile "$WORKSPACE_PROFILE" 2>/dev/null || echo "(scope already exists)"
databricks secrets put-secret "$SECRET_SCOPE" account_id    --string-value "$ACCOUNT_ID"    --profile "$WORKSPACE_PROFILE"
databricks secrets put-secret "$SECRET_SCOPE" client_id     --string-value "$SP_APP_ID"     --profile "$WORKSPACE_PROFILE"
databricks secrets put-secret "$SECRET_SCOPE" client_secret --string-value "$CLIENT_SECRET" --profile "$WORKSPACE_PROFILE"
echo "stored account_id / client_id / client_secret in scope '$SECRET_SCOPE'."

say "5/5  Point the bundle at the scope + SMOKE TEST (no writes)"
echo "Deploy with the scope wired in:"
echo "    databricks bundle deploy --var account_sp_scope=$SECRET_SCOPE"
echo
echo "Then run the writer in smoke-test mode — it only checks that the SP can REACH"
echo "each workspace that has queued rows; it tags nothing:"
echo "    databricks bundle run tag-governance-writer -- \\"
echo "      --python-params smoke_test=true"
echo
echo "Expect one '✓ REACHABLE workspace <id> as <sp>' line per workspace. Any"
echo "'✗ UNREACHABLE' means the SP isn't assigned there (redo step 3 for that ws)."
echo
echo "When all workspaces are REACHABLE, do a real dry run, then go live:"
echo "    databricks bundle run tag-governance-writer -- --python-params dry_run=true   # writes nothing, logs intended old→new"
echo "    databricks bundle run tag-governance-writer -- --python-params dry_run=false  # applies across all workspaces"
echo
say "M2 setup complete."
