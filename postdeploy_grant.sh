#!/usr/bin/env bash
# Postdeploy hook: re-assert the app SP's grants after every `databricks bundle deploy`.
#
# WHY: the app runs as its own service principal, which needs CAN_USE on the SQL
# warehouse + UC read/write on the tag_governance schema. Those objects are
# pre-existing (not bundle-managed), so a redeploy / SP re-provision can silently
# drop the grants -> every query 500s -> blank KPIs. This closes that gap so no
# one has to remember grant_app_sp.sh.
#
# Lives in a .sh file (not inline in databricks.yml) because DAB `scripts` forbid
# ${...} interpolation, and we want env-var defaulting here.
#
# Override per customer target by exporting before `bundle deploy`:
#   TG_GRANT_PROFILE, TG_GRANT_SCHEMA, TG_GRANT_WAREHOUSE
set -uo pipefail

PROFILE="${TG_GRANT_PROFILE:-${DATABRICKS_CONFIG_PROFILE:-fe-east}}"
SCHEMA="${TG_GRANT_SCHEMA:-main.tag_governance}"
WAREHOUSE="${TG_GRANT_WAREHOUSE:-148ccb90800933a1}"

cd "$(dirname "$0")"
# Best-effort: a non-admin deployer can't grant; don't fail the whole deploy.
./grant_app_sp.sh "$PROFILE" tag-governance "$SCHEMA" "$WAREHOUSE" || {
  echo "[postdeploy_grant] grant step skipped/failed (need admin?). Run grant_app_sp.sh manually if the app shows blank KPIs." >&2
}
