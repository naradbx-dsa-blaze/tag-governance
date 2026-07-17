#!/usr/bin/env bash
# Grant the tag-governance app's service principal everything it needs to run.
#
# WHY THIS EXISTS: the app runs as its own service principal (not you). That SP
# needs UC read/write on the tag_governance schema AND CAN_USE on the SQL
# warehouse, or every query 500s and the dashboard shows blank $ / no KPIs.
# These objects are pre-existing (not bundle-managed), so grants can't live in
# databricks.yml -- run this once after `databricks bundle deploy`.
#
# Idempotent: safe to re-run. Requires an admin/owner identity on the profile.
#
# Usage:
#   ./grant_app_sp.sh <profile> [app_name] [catalog.schema] [warehouse_id]
# Example:
#   ./grant_app_sp.sh fe-east tag-governance main.tag_governance 148ccb90800933a1

set -euo pipefail

PROFILE="${1:?usage: grant_app_sp.sh <profile> [app_name] [catalog.schema] [warehouse_id]}"
APP="${2:-tag-governance}"
SCHEMA="${3:-main.tag_governance}"
WAREHOUSE="${4:-148ccb90800933a1}"
CATALOG="${SCHEMA%%.*}"
DBX="${DATABRICKS_CLI:-databricks}"

echo "Resolving service principal for app '$APP' on profile '$PROFILE'..."
SP=$("$DBX" apps get "$APP" --profile "$PROFILE" --output json \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['service_principal_client_id'])")
echo "  app SP = $SP"

echo "Granting USE_CATALOG on $CATALOG ..."
"$DBX" api patch "/api/2.1/unity-catalog/permissions/catalog/$CATALOG" --profile "$PROFILE" \
  --json "{\"changes\":[{\"principal\":\"$SP\",\"add\":[\"USE_CATALOG\"]}]}" >/dev/null

echo "Granting USE_SCHEMA, SELECT, MODIFY on $SCHEMA ..."
"$DBX" api patch "/api/2.1/unity-catalog/permissions/schema/$SCHEMA" --profile "$PROFILE" \
  --json "{\"changes\":[{\"principal\":\"$SP\",\"add\":[\"USE_SCHEMA\",\"SELECT\",\"MODIFY\"]}]}" >/dev/null

echo "Granting CAN_USE on warehouse $WAREHOUSE ..."
"$DBX" api patch "/api/2.0/permissions/warehouses/$WAREHOUSE" --profile "$PROFILE" \
  --json "{\"access_control_list\":[{\"service_principal_name\":\"$SP\",\"permission_level\":\"CAN_USE\"}]}" >/dev/null

echo "Done. App SP $SP can now read/write $SCHEMA and query warehouse $WAREHOUSE."
