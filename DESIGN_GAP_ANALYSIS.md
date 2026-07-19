# Auto-Tagging Platform — Gap Analysis

Maps the target design spec (capability-aware AWS Databricks auto-tagging platform)
against what the current `tag-governance` app already implements. Verdict per
requirement: **✅ Have** · **🟡 Partial** · **❌ Missing**. A phased build order follows.

> Context: current app is a Databricks App (apx: React + FastAPI) with a
> queue→writer-job→audit→rollback-job backend, deployed on fe-east (Azure) and
> e2-demo (AWS). Backend modules: `writer.py` (capability matrix), `writer_job.py`,
> `rollback_job.py`, `queries.py`, `tagging.py`, `jobs.py`, `authz.py`, `router.py`.

---

## 1. Components

| Spec component | Status | Where it lives today / what's missing |
|---|---|---|
| **Scanner / inventory** | 🟡 | `queries.py` scans the pre-aggregated `workload_daily` summary (built from `system.billing.usage` by the refresh job). This is a **cost/billing-derived** inventory, NOT a live asset scan. Missing: direct enumeration of jobs/pipelines/clusters/warehouses via list APIs; **workspace-level** assets entirely absent. |
| **Capability registry** | 🟡 | `writer._WRITERS` + `_POLICY_GOVERNED_REASON` + `attempt_write()` dispatch. It's a code dict, not a declarative/queryable registry, and it conflates "how to write" with "capability metadata". No explicit read-path or create-time-only flags. |
| **Tag policy engine** | ❌ | No policy model (required keys, allowed values, per-team rules). Rules today are ad-hoc user input in `tagging.auto_tag(rules=...)`, evaluated in SQL — not governed policy. |
| **Recommendation engine** | ✅ | `annotate.sql` runs `ai_query` over the fleet → `suggestions` table (cost-center + confidence). Surfaced via `/preview`. |
| **Remediation orchestrator** | ✅ | `writer_job.py`: cost-descending drain, per-workspace concurrency, 429 backoff, RUNNING-claim guard, incremental persist. |
| **Rollback store** | 🟡 | `audit` table records old→new per change; `rollback_job.py` replays in reverse. Missing: explicit **pre-change snapshot** of full tag state (only the single changed key's old_value is stored). |
| **Audit log** | ✅ | `audit` table: every SET/ROLLBACK with old/new/status/error/executed_by/at. |
| **Admin UI** | 🟡 | React SPA: KPIs, 3 tag modes, batches+rollback, not-taggable panel. Missing: compliance dashboard, policy simulator, exceptions dashboard, approval flow. |

## 2. Workflows

| Spec | Status | Notes |
|---|---|---|
| Inventory | 🟡 | Cost-derived only (see Scanner). |
| Compliance classification | ❌ | No "compliant / non-compliant vs policy" concept. Only "untagged for key X". |
| Dry-run previews | ✅ | `dry_run` threaded through writer/rollback; `/rule-preview`, `/preview` show impact + per-workload sample. |
| Approval workflows | ❌ | No enqueue→approve→apply gate. Writes are gated only by group membership (`authz`), not a review step. |
| Execution history | ✅ | `/batches`, `/batch-detail`, `batch_failure_breakdown`. |
| Rollback | 🟡 | Batch rollback works. Missing: single-resource rollback, rollback preview, partial-rollback reporting (partially there via audit), drift detection. |
| Exceptions | ❌ | No exception queue for unsupported/deferred assets. |

## 3. Resource capability matrix

**Have (in `writer.py`), but incomplete and not declarative:**

| Product | Write path today | Direct tag | Create-time-only | Policy-driven | UI-only | Rollback |
|---|---|---|---|---|---|---|
| JOBS | `jobs.update`(set)/`jobs.reset`(remove) | ✅ | – | – | – | ✅ |
| ALL_PURPOSE cluster | `clusters.edit` get→merge→put | ✅ | – | – | – | ✅ |
| SQL warehouse (non-serverless) | `warehouses.edit` | ✅ | – | – | – | ✅ |
| SQL (serverless) | — | ❌ | – | ✅ budget policy | – | – |
| DATABASE / LAKEBASE | `database.update_database_instance` (if SDK) | 🟡 SDK-gated | – | – | – | ✅ |
| MODEL_SERVING | `serving.patch` by name | ✅ | – | – | – | ✅ |
| VECTOR_SEARCH | SDK-gated (often UNSUPPORTED) | 🟡 | – | – | – | 🟡 |
| DLT / pipelines | UNSUPPORTED (no safe API) | ❌ | – | – | ✅ edit in UI | ❌ |
| APPS | — | ❌ | – | ✅ budget policy | – | – |
| AI_GATEWAY / AI_FUNCTIONS / etc. | — | ❌ | – | 🟡 | – | – |
| **WORKSPACE** | **absent** | ? | – | – | – | – |

**Missing from the matrix vs spec:** explicit **read path** per asset; **create-time-only**
column (none modeled); **workspace tags** (the spec's headline AWS case — max 20, key/value
length rules — is not implemented at all); a machine-readable form (it's a Python dict).

## 4. AWS workspace tag rules

| Rule | Status |
|---|---|
| Max 20 tags | ❌ not enforced |
| Unique keys | 🟡 implicit (map) |
| One value per key | ✅ (map semantics) |
| Key length 1–127 | 🟡 validator allows to 255, not 127; no min-1 check |
| Value length 0–255 | 🟡 validator caps 255 but **rejects empty** (`value is None or not match`) — spec allows length 0 |
| UTF-8 allowed | ❌ `_TAG_RE` is a restricted ASCII set, not UTF-8 |
| Never treat empty payload as delete-all unless confirmed | ❌ not modeled (writer merges; no "replace whole map" path from UI) |

→ `router._validate_tag` needs a rewrite to match the AWS spec precisely, and workspace
tagging needs its own validation profile (20-tag ceiling, 1–127 key).

## 5. Discovery depth (list APIs may not return full tag state)

❌ **Not handled.** Inventory comes from billing, and writers do a per-resource GET only
at write time (get→merge→put for clusters/warehouses). There is no "deep read to
reconcile true tag state before remediation" pass. Drift detection absent.

## 6. Mutation vs cost-attribution separation

✅ **Conceptually present and documented.** `_POLICY_GOVERNED_REASON` + `not_taggable_breakdown`
split true tag mutation from budget-policy attribution (serverless SQL, Apps, AI Gateway),
with doc-cited guidance. Missing: actually *doing* budget-policy attachment
(`databricks_budget_policy` Terraform / API) — today it only recommends.

## 7. Strict rollback

| Spec | Status |
|---|---|
| Pre-change snapshots | 🟡 per-key old_value only, not full-map snapshot |
| Diffs | ✅ audit old→new |
| Idempotent execution | ✅ writer noops when key==value; claim-token guard |
| Single-resource rollback | ❌ batch-only |
| Batch rollback | ✅ |
| Drift detection | ❌ |
| Rollback preview | 🟡 dry_run exists but UI doesn't surface a "what will be restored" list |
| Partial-rollback reporting | 🟡 audit shows per-row FAILED, UI now notes "N couldn't be removed" |

## 8. Edge cases

| Case | Status |
|---|---|
| Missing permissions | ✅ PermissionDenied → FAILED, honest UI breakdown |
| Rate limits | ✅ exp backoff (429/string-match — fragile, noted) |
| Stale inventory | 🟡 double-tag guard handles the daily-snapshot lag |
| Concurrent edits | 🟡 RUNNING-claim token prevents double-drain; no per-resource optimistic concurrency |
| Unsupported assets | ✅ UNSUPPORTED status + reason |
| Ambiguous empty-tag updates | ❌ not modeled |
| Partial failures / retries | ✅ per-row status, resumable queue |
| Duplicate execution requests | ✅ claim token + idempotent writes |
| Rollback after drift | ❌ no drift check before rollback |

## 9. Fallback pattern for unsupported surfaces

🟡 Currently **classified and explained** (`not_taggable_breakdown`: POLICY_GOVERNED /
UI_ONLY / NO_API buckets with the exact manual action). Missing the **active** fallbacks:
create-time enforcement, automated policy attachment, and an exception queue to track them.

## 10. Admin UI panels

| Panel | Status |
|---|---|
| Inventory view | 🟡 product rollup, not asset-level list |
| Compliance dashboard | ❌ |
| Recommendations table | ✅ AI preview |
| Diff preview | 🟡 rule/AI preview shows target values |
| Dry-run panel | ✅ |
| Approval flow | ❌ |
| Execution history | ✅ batches |
| Rollback console | ✅ |
| Exceptions dashboard | ❌ |
| Policy simulator | ❌ |

## 11. Internal APIs

Have: `/overview` (inventory-ish), `/preview` (recommend), `/rule-preview` (plan),
`/auto-tag` `/tag-selected` `/manual-tag` (apply), `/rollback`, `/batches` `/batch-detail`
(history), `/not-taggable`, `/field-values`, `/whoami`.
Missing named endpoints: **scan**, **policy-evaluate**, **remediation-plan-generate**
(separate from apply), **rollback-preview**, **exception-handle**, **policy-simulate**.

## 12. Non-functionals

| Area | Status |
|---|---|
| Security / least privilege | 🟡 app-SP + per-workspace client resolver (M1/M2); write-gate by group. Least-privilege not formalized. |
| Service-principal model | ✅ M1 home + M2 account-SP cross-workspace (`ws_clients.py`) |
| Auditability | ✅ audit table |
| Observability | 🟡 structured logs with error ref-ids; no metrics/traces |
| SLOs / alerts | ❌ |
| DR for snapshot + audit data | ❌ (Delta tables, no backup/restore policy) |

---

## Biggest gaps (what's genuinely net-new)

1. **Workspace-level tagging** — the spec's headline AWS case is entirely absent (inventory + writer + the 20-tag/1–127 validation profile).
2. **Tag policy engine + compliance classification** — no notion of "policy" or "compliant". This is the backbone of the spec.
3. **Live asset scanner** — today's inventory is billing-derived; the spec wants list-API enumeration with deep tag-state reads.
4. **Exception queue + active fallbacks** (create-time enforcement, budget-policy attachment) — currently classified but not actioned.
5. **Approval workflow** and **policy simulator** UI.
6. **Snapshot-based strict rollback** (full-map snapshot, single-resource, preview, drift detection).
7. **Correct AWS tag validation** — current `_validate_tag` diverges (255 vs 127 key, rejects empty value, ASCII-only not UTF-8, no 20-tag ceiling).

## Already solid (keep, don't rebuild)

Queue→writer→audit→rollback architecture; cost-descending resumable drain; capability
dispatch with UNSUPPORTED handling; AI recommendation via `ai_query`; dry-run;
policy-governed vs taggable separation with doc-cited guidance; M1/M2 cross-workspace SP
model; honest failure reporting; incremental real-time progress.

---

## Proposed phased build order

- **Phase 0 (fix, small):** Correct `_validate_tag` to the AWS spec; add a workspace-tag
  validation profile (20 max, key 1–127, value 0–255, UTF-8, explicit empty-payload confirm).
- **Phase 1 (read-only discovery):** Real scanner over list APIs (jobs/pipelines/clusters/
  warehouses/**workspaces**) with deep tag-state reads → an `inventory` table separate from
  billing. Declarative capability registry (read path, write path, direct/create-time/policy/
  UI/rollback flags) replacing the `_WRITERS` dict.
- **Phase 2 (policy + compliance):** Tag policy model (required keys, allowed values, scope) +
  compliance classification pass + compliance dashboard + policy simulator.
- **Phase 3 (controlled remediation):** Split remediation-plan-generate from apply; approval
  workflow; exception queue with active fallbacks (create-time, budget-policy attachment).
- **Phase 4 (strict rollback + NFR):** Full-map snapshots, single-resource + preview + drift
  detection; SLOs/alerts/observability; DR for audit+snapshot tables.
