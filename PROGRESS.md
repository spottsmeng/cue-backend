# CUE backend — implementation progress

Tracks the milestone plan at `/Users/weemengtan/.claude/plans/parsed-cooking-valley.md` (that file is
local working notes, not committed anywhere — this table is the durable record). Each milestone has
its own standalone prompt in the project root (`Prompt 4` onward), written so a brand-new Claude Code
session with no memory of prior conversations can execute it correctly on its own.

**Before starting any `Prompt N` file below: read this table first.** If a milestone's listed
prerequisite isn't marked Done, stop and flag it rather than building on a foundation that isn't
there yet.

| # | Milestone | Prompt file | PRD phase | Depends on | Status |
|---|---|---|---|---|---|
| M1 | Production Twin | `Prompt 4 — Production Twin.txt` | §15 Phase 3 | Ledger (done) | Done (2026-08-07) |
| M2 | Governance completion | `Prompt 5 — Governance completion.txt` | §15 Phase 9 (remainder) | Identity/RBAC (done) | Done (2026-08-07) |
| M3 | Documents | `Prompt 6 — Documents.txt` | §15 Phase 8 | — | Done (2026-08-07) |
| M4 | Foresight | `Prompt 7 — Foresight.txt` | §15 Phase 5 | M1, M3 | Not started |
| M5 | Living WIP & reporting | `Prompt 8 — Living WIP and reporting.txt` | §15 Phase 4 (backend half) | M1, M2, M4 | Not started |
| M6 | Ask & retrieval | `Prompt 9 — Ask and retrieval.txt` | §15 Phase 7 (backend half) | M3, M4 | Not started |
| M7 | Vendor Reliability Graph | `Prompt 10 — Vendor Reliability Graph.txt` | §15 Phase 10 (backend half) | M2 | Not started |
| M8 | Real channel capture | `Prompt 11 — Real channel capture.txt` | §15 Phase 1 | M2, M4 | Not started — genuinely gated on Pico credentials; code is buildable now, real adapters aren't |
| M9 | Write-back | `Prompt 12 — Write-back.txt` | §15 Phase 6 | M8 | Not started |
| M10 | Hardening & observability | `Prompt 13 — Hardening and observability.txt` | §15 Phase 11 | all of the above | Not started |

### M1 notes for later sessions

M1 shipped in two passes: Prompt 4 itself, then a same-day follow-up once the product direction
clarified CUE is multi-tenant SaaS expanding beyond event-production (a renovation/interior-design
vertical is real near-term roadmap, not hypothetical) — which meant the ontology's existing 3-tier
layering (CUE-PRD.md §4.2.1: universal core / vertical pack / tenant extension) needed to actually
apply to the Twin archetype too, not just vocabulary. The notes below describe the state after both
passes; superseded first-pass decisions are not listed separately.

- **Archetype representation**: `milestone_archetypes` / `milestone_archetype_items` /
  `milestone_archetype_dependencies` are DB-backed reference data (seed content lives in
  `seed_data/event_production_archetype.py`, a plain-data package migrations *and* tests import from
  — see that package's own `__init__.py` for why it's not `alembic/versions/`-adjacent and not
  duplicated the way `COMMITMENT_ACTS`/`MILESTONE_TYPES` deliberately are), not a Python constant
  baked into `create_project`. A project's real Milestone/Dependency rows are a literal one-time copy
  materialized at creation time (`app/twin/service.py`'s `materialize_archetype`) — the template is
  never referenced again afterward, and a PM override always lands on the copy.
- **Archetype resolution now mirrors the ontology's layering** (`MilestoneArchetype.vertical_id` +
  `organisation_id`, migration `fbea5e75b4d8`), but picks the single most specific match rather than
  unioning tiers the way ontology terms do — an archetype is a whole coherent template, not a
  vocabulary word (see `app/twin/models.py`'s `MilestoneArchetype` docstring, and
  `app/twin/service.py`'s `_resolve_archetype`/`_resolve_ontology_terms` for the shared ranking logic).
  `ProjectCreate.archetype_code` lets a caller pick a specific template outright; omitted, it resolves
  the most specific `is_default` row for the project's org/vertical. No tenant-authoring UI exists yet
  (`organisation_id` is a schema hook, empty in practice) — same "mechanism exists, not a v1 feature
  commitment" posture the ontology's own tenant tier already has.
- **Archetype dependency edges are now explicit** (`milestone_archetype_dependencies`, same
  upstream/downstream/lag_days shape as the live `Dependency` table), not inferred from
  `MilestoneArchetypeItem.sequence_order`. Today's only archetype is still honestly a linear chain
  (Annex A gives a schedule, not a branching graph) — the schema change means the *next* archetype
  (a gala's parallel F&B/staging/AV tracks, say) is a data change, not another migration.
- **The project-level graph is now fully mutable, not just date-editable**: `POST`/`DELETE` on both
  `/projects/{id}/milestones` and `/projects/{id}/milestones/dependencies`, audited the same way
  `PATCH` already was (`twin_audit_log`, actions `milestone_created`/`milestone_deleted`/
  `dependency_created`/`dependency_deleted`, migration `d7def2e27c7c`). Adding a dependency is
  rejected with 422 if it would make the graph cyclic (checked via `app/twin/graph.py`'s
  `compute_twin`/`CycleError` before insert, not left for the next `/twin/current` call to discover as
  a 500). Deleting a milestone still referenced by a `Dependency` or `Deliverable` is a 409, not an
  implicit cascade — remove the edge first, a deliberate separate step.
- `twin_audit_log.milestone_id`/`dependency_id` are `ON DELETE SET NULL` (migration `e771d0318751`) so
  an audit entry documenting a deletion outlives the row it documents. That FK behaviour is
  implemented by Postgres as a real `UPDATE`, which the append-only trigger (`616998fb7f1b`)
  originally blocked unconditionally — found by actually exercising a delete under test, not by
  inspection. The trigger was redefined (same migration as the FK change, not a separate one — they're
  two halves of one fix) to allow exactly that one mutation, same carve-out shape
  `app/identity/models.py`'s `Delegation` already established for revocation.
- **`verticals` had never been seeded by any migration at all** — `ProjectCreate.vertical_code` has
  defaulted to `'event-production'` since the identity/RBAC session, and `create_project` 422s on an
  unknown code, meaning project creation was silently broken in any environment that didn't have
  someone manually INSERT the row outside of migrations (masked for tests by
  `tests/conftest.py`'s `seeded_vertical_id` fixture). Fixed by migration `a9199a78e2cf`
  (`seed_data/verticals.py`).
- The 18 `milestone_type` ontology terms were re-keyed from universal-core to vertical-pack-scoped
  (migration `eed5a4da79f6`) — CUE-PRD.md §4.2.1's own table already said "Milestone types" belongs at
  the vertical-pack tier; universal-core was Prompt 4's deliberate v1 simplification for when only one
  vertical existed, and stopped being a simplification once a second one became real. Codes are
  unchanged, so nothing that references them by code broke.
- `milestones` (from the original Ledger migration, `a895ae03ec5c`) had never been given a
  `tenant_isolation` RLS policy — latent but harmless until this session's `app/api/milestones.py`
  became the first real read/write path against it. Closed in migration `616998fb7f1b`.
  `deliverables`/`budgets` have the same latent gap and are still untouched by any endpoint — left
  alone, same reasoning Milestone had before now.
- `ProjectCreate` never actually accepted `event_start`/`event_end`, even though `Project` has always
  had the columns — every project's event date was silently NULL. Added both as optional fields, since
  FR-TWN-02's archetype seeding needs an event date to compute `planned_at` from. A project created
  without one still gets the full milestone/dependency structure, just with every `planned_at` left
  null until a PM sets it (an ordinary `PATCH .../milestones/{id}`, FR-TWN-10).
- FR-TWN-08 (learned duration distributions) is unimplemented, deliberately — see `CUE-PRD.md` §6.8's
  own paragraph on why, and don't build a fabricated distribution to make it look done when a future
  session gets here with real project history to learn from.
- Foresight (M4) can now read `app/twin/service.py`'s `compute_current`/`compute_propagation` for
  slack, critical path and hypothetical impact — it does not yet *decide* anything from them
  (FR-LCY-02/03's automatic `committed → at_risk` / `at_risk → broken` transitions are still M4's job).

### M2 notes for later sessions

Closed FR-ADM-06's "attach channels" half, 07, 08, 09, 10, 11 — 01 through 05 were already done by the
RBAC/delegation session and untouched here.

- **`FINANCE_ROLES = {"finance", "producer"}`** (`app/identity/service.py`), sitting alongside
  `WRITE_ROLES`/`ADMIN_ROLES`. Gates every Budget write and `PATCH .../payment-status` — the two
  surfaces FR-ADM-11 and FR-LED-13 both name as "Finance or Producer" / "Finance & Procurement". No
  ninth "Procurement" role was added — FR-ADM-01's role list is a closed native enum, and the PRD's
  own persona table already merges Finance and Procurement into one persona. The Vendor Reliability
  Graph milestone (M7, FR-VRG-05's "expose to Procurement with appropriate access control") should
  reuse this same constant rather than inventing its own.
- **`budgets` and `channels` got their `tenant_isolation` RLS policy for the first time this session**
  (migration `9ddb100d7e8e`) — both tables existed since the very first migration (`a895ae03ec5c`) but
  were left off its RLS section on purpose ("demonstrated on organisations/projects/commitments...not
  done exhaustively here"), and M1's own notes recorded the gap as latent-but-harmless specifically
  because nothing queried either table yet. This session is what built the first real endpoints against
  both (`app/api/budget.py`, `app/api/channels.py`), so — same rule `616998fb7f1b` applied to
  `milestones` — closing the gap was this session's job, not left for later. `deliverables` is the same
  latent shape and is *still* untouched by any endpoint after this session too; left alone, same
  reasoning, for whichever future session builds the first endpoint against it.
- **`audit_action` gained a fifth value, `payment_status_updated`** (same migration, `ALTER TYPE ...
  ADD VALUE`, mirroring `d7def2e27c7c`'s precedent for `twin_audit_action`) — FR-LED-13 requires
  `payment_status` to be structurally distinct from a `/verify` correction, so it needed its own audit
  action rather than being folded into `"corrected"`. `app/models/audit.py`'s Python-side `AuditAction`
  enum values list was updated to match, same "keep the Python list in sync even though the real ALTER
  happens via raw SQL" pattern `TwinAuditAction` already established.
- **`ConsentRecord.evidence` is a plain nullable text column, not a row in the shared polymorphic
  `evidence` table** (`app/models/governance.py`). The `evidence` table's CHECK constraint is
  hard-coded to exactly one of `{commitment_id, budget_id}`; extending it to a third subject wasn't
  needed because this session has no captured-message path to point at for most consent transitions
  anyway (the actual notice-sending flow is out of scope — FR-CAP-06's real channel adapter doesn't
  exist yet). If a future session wires up real consent-notice capture, revisit whether `evidence`
  should gain a `consent_record_id` column at that point, once there's a real message to link.
- **`ConsentRecord` is one current row per `(party_id, project_id)`, not an append-only history** —
  `POST /admin/consent/action-request` is an upsert keyed on that pair (same idempotent-by-natural-key
  shape `app/api/projects.py`'s `_grant_membership` already uses for `Membership`), not a new row per
  transition. Justified by FR-CAP-07's "honour opt-out immediately": only the *current* status is ever
  actually acted on. If a future session needs a full transition history for audit purposes, that's an
  additive change (an append-only consent event log alongside this table), not a rework of it.
- **`RetentionPolicy` is deliberately minimal** — org × vertical ("project type") × region → a single
  `retention_days` window, matching Prompt 5's own field list exactly. It has no per-record-class
  dimension, even though PRD §10.3's own table gives messages/ledger/documents/audit/consent each a
  different default and "configurable" flag. Nothing consumes this value yet (deletion automation is
  explicitly out of scope this session), so there was nothing forcing that extra axis to exist yet — a
  future session building the deletion scheduler should revisit whether `RetentionPolicy` needs a
  `record_class` column at that point, once there's a real consumer to differentiate for.
- **`require_org_administrator`** (`app/api/deps.py`), new alongside `require_project_role` — answers
  "does this user hold `administrator` on *at least one* project in this org", not "can this user act on
  *this* project". Every `/admin/*` endpoint (`app/api/admin.py`, `consent.py`, `retention.py`) is gated
  by this, not `require_project_role`, which is what lets an Administrator on project A reach project B
  through `/admin/export` even with zero membership on B — the property
  `tests/test_admin_api.py::test_org_admin_visibility_is_distinct_from_project_membership` exercises
  directly, as the testing expectation in Prompt 5 asked for by name.
- **Channels API write endpoints (attach/detach/health/reconnect) are gated `ADMIN_ROLES`**, the same
  tier `app/api/projects.py`'s `add_member` uses — FR-ADM-06 names "attach channels" as part of
  provisioning, the same admin-tier surface. There is no service-account/agent identity yet for a real
  capture agent to call `POST .../health` as (M8, Real channel capture, is genuinely gated on Pico
  credentials) — this is a placeholder until that identity model exists; M8 will need to decide whether
  a capture agent authenticates as a project member at all, or gets its own non-RBAC auth path.
- **`GET /projects/{id}/channels`** (list) exists even though PRD §11.2's own resource table only names
  "attach · detach · health · reconnect" for this resource — without a way to list a project's channels
  there'd be no way to discover a `channel_id` to detach/health/reconnect against, and FR-ADM-09's
  "surface degraded channels to Administrators" needs a listing surface to read from.
- **`/admin/export/{project_id}` and `/admin/consent/export` both support `?format=json|csv`** —
  `json` (default) is a structured bundle in one response; `csv` returns a zip of per-table CSVs for the
  project export (`project.csv`/`commitments.csv`/`evidence.csv`/`budgets.csv`/`audit_log.csv`) and a
  single CSV for the consent ledger. Documents are named in FR-ADM-10's "ledger, documents, audit" but
  skipped from the bundle — that milestone (M3) doesn't exist yet, per Prompt 5's own scope note; add a
  `documents` key/CSV once it does.

### M3 notes for later sessions

Closed FR-DOC-01 through 06 and 08 (per Prompt 6's own scope line). FR-DOC-07 (real SharePoint
write-back) was originally interface-only per Prompt 6's own EXPLICITLY OUT OF SCOPE section
(deferred to Prompt 11, Real channel capture) — a real, opt-in Graph-backed implementation was added
same-session afterward, ahead of that schedule, for competition-demo purposes (see the write-back
bullet below). FR-DOC-09 (chat-vs-approved-version drift detection) remains genuinely not closeable
— it structurally needs Prompt 11's real chat-capture pipeline to have anything to compare against,
which no demo shortcut changes.

- **Real OCR (PaddleOCR) and real document parsing (Docling) are not wired up this session** —
  `DocumentVersion.extracted_text` is a plain-text field the caller (or, today, a test) supplies
  directly at upload/version-creation time, standing in for what a real OCR/parsing pipeline would
  derive from the uploaded binary (`storage_ref`). Full-text search (FR-DOC-05, tsvector/GIN) and
  spec-claim extraction (FR-DOC-08) are both therefore only as complete as this stand-in text is —
  not a discrepancy to silently paper over, a direct, documented consequence of this session's own
  scope limit (CUE-Tech-Stack.md §2.4 names the real production choices). Wiring real OCR/parsing is
  a deliberate, separate scope item for a later hardening pass.
- **New module `app/documents/` — models, storage, sharepoint, extractor, schema, service, audit** —
  same per-domain-module-owns-its-models precedent `app/twin/models.py` and
  `app/models/governance.py` already established, per `app/models/ledger.py`'s own top-of-file scope
  note explicitly carving Document out of the Ledger core.
- **`app/documents/storage.py`'s `StorageBackend` Protocol, one real implementation
  (`MinioStorageBackend`)** — no object store existed anywhere in this codebase before this session
  (confirmed: no S3/MinIO client anywhere). `docker-compose.yml` gained a `minio` service
  (health-checked, named volume, same shape the existing `postgres` service already has). Original
  file bytes are never mutated after upload — a new version is always a new object key
  (`documents/{document_id}/v{n}/{uuid}`), never an overwrite.
- **`docker-compose.yml`'s `postgres` image changed from `postgres:17` to `pgvector/pgvector:pg17`**
  — a drop-in replacement (same major version, same data directory layout; the existing named volume
  survived the swap untouched, confirmed by re-inspecting `\dt` after recreation) — required because
  `DocumentVersion.embedding` (the pgvector column below) needs `CREATE EXTENSION vector` to exist at
  all, and the plain `postgres:17` image has no path to add that extension after the fact.
- **`app/documents/sharepoint.py`'s `SharePointAdapter` Protocol now has two implementations, not
  one** — `NoOpSharePointAdapter` (still the default; logs and returns, no external call, no
  credentials required to run this codebase at all) and `GraphSharePointAdapter`, a real
  Microsoft Graph write-back. The real implementation was originally scoped to Prompt 11 (gated on
  Pico's own tenant credentials) — brought forward same-session, post-hoc, because a competition
  demo needs to show a real write landing in a real SharePoint library, not a log line. This is a
  deliberate deviation from the milestone plan's own sequencing, not an oversight.
  - Selected via `CUE_SHAREPOINT_PROVIDER=noop|graph` (`app/documents/config.py`'s
    `SharePointSettings`), same env-driven provider-switch shape `app/llm/factory.py` already
    established for Ollama/Anthropic. Nothing about the adapter is Pico-specific — it targets
    whichever tenant/site the config points at, so a free Microsoft 365 Developer Program sandbox
    tenant works identically to Pico's real one for demo purposes; swapping to Pico's tenant later
    is a credentials change, not a code change.
  - App-only (client-credentials) auth via `msal`, `Sites.ReadWrite.All` application permission.
    `ConfidentialClientApplication` construction is deliberately lazy (first token acquisition, not
    adapter construction) — MSAL unconditionally performs a live OIDC tenant-discovery network call
    as part of building that object, which would otherwise make constructing the adapter itself (a
    FastAPI dependency resolution) a network operation; found by the adapter's own unit tests
    failing against a fake tenant id, not by inspection.
  - Writes to the *same path* per Document (`{library_folder}/{document.name}`) on every approval,
    not a new file per version — SharePoint's own built-in version history then shows the approval
    trail natively, which is both more useful to anyone opening the library directly and a more
    convincing demo than a pile of `v1`/`v2`/`v3` files.
  - A write-back failure (network blip, expired demo-tenant token) does **not** roll back the
    approval — the approval is already a real, committed fact about CUE's own state, independent of
    whether the external sync succeeded. Outcome recorded on `document_audit_log`'s
    `version_approved` detail (`sharepoint_write_back: "ok"` or `"failed: ..."`) either way, so a
    failure is visible and diagnosable, never silently swallowed.
  - Known, named limitation: files over Graph's 4 MiB simple-upload limit raise a clear error —
    resumable upload sessions aren't implemented (sized for a demo/early-production document set:
    specs, drawings, quotations — not arbitrary large media).
  - Called from `POST .../versions/{id}/approve` — approval is also FR-DOC-07's write-back trigger
    point, unchanged from the original design.
- **The shared `evidence` table (`app/models/ledger.py`) gained two more nullable subject columns**
  — `document_version_id`, `spec_claim_id` — rather than either domain inventing its own evidence
  table; the CHECK constraint widened from "exactly one of {commitment, budget}" to "exactly one of
  {commitment, budget, document_version, spec_claim}" (migration `f3a1c9d7e4b2`). `evidence` itself
  still has no RLS policy of its own — a pre-existing gap from the Ledger session, untouched by every
  session since (including this one); every query against it in this codebase, old and new, is always
  scoped by an already-RLS/role-checked parent id, never queried bare.
- **`DocumentVersion` and `SpecClaim` both get the same deferred-constraint-trigger evidence
  requirement Commitment/Budget already have** (`check_document_version_has_evidence`,
  `check_spec_claim_has_evidence`, same `DEFERRABLE INITIALLY DEFERRED` shape) — per Prompt 6's
  explicit instruction, even though PRD §4.1's own ER diagram marks `DOCUMENT_VERSION`'s evidence
  link optional (`}o--o|`). Followed the prompt, not the ER diagram, on this one point.
- **`document_audit_log`** (this domain's own append-only trail, same shape `twin_audit_log`
  established) — built with the SET-NULL-on-delete append-only carve-out from the start, not as a
  follow-up migration the way `twin_audit_log` needed one (`e771d0318751`) after the fact. One real
  design difference from `twin_audit_log`: `document_id` and `document_version_id` are **not**
  mutually exclusive on this table (a `version_created`/`version_approved` row legitimately carries
  both — parent and child, not two independent alternatives) — an early draft added a
  `NOT (both non-null)` CHECK mirroring `twin_audit_log`'s milestone/dependency one and it was wrong;
  removed before this session ended (caught by the migration's own downgrade/upgrade + full pytest
  cycle, not by inspection).
- **`Document.class_term_id` / `milestone_type_term_id` / `phase_term_id`** are the FR-DOC-03
  auto-tag targets (`POST .../documents/{id}/tag`) — three ontology_terms lookups, all nullable.
  `class_code`/`phase_code` resolve via a simple universal-core-only query (mirrors
  `app/ledger/extractor.py`'s `_get_commitment_act_term` exactly, since `deliverable_class`/`phase`
  are both seeded universal-core, migration `a7c2e5f19b34`). `milestone_type_code` does **not** use
  that same simple query — `milestone_type` was re-keyed to the event-production vertical pack by an
  earlier session (`eed5a4da79f6`), so it reuses `app/twin/service.py`'s existing project-aware,
  three-tier `get_milestone_type_term` instead (found the hard way: the simple universal-core query
  returned "not seeded" for a real, correctly-seeded term — a test caught it, not inspection).
- **FR-DOC-05: lexical search only, this session** — Postgres full-text (`tsvector`/GIN index,
  `to_tsvector('english', ...)` as a generated STORED column) over `DocumentVersion.extracted_text`.
  `DocumentVersion.embedding` (pgvector, dimension 1024 to match BGE-M3) is added but deliberately
  left unpopulated — Prompt 6's own EXPLICITLY OUT OF SCOPE note; the embedding client is Prompt 9's
  (Ask & retrieval) job, and the column exists now so that milestone needs no migration of its own.
- **FR-DOC-06: `POST /projects/{id}/archive`** (ADMIN_ROLES-gated) — the first session to ever write
  to `Project.archived_at` (confirmed at the start of this session: no prior write path existed at
  all). "Applies" the org/project's RetentionPolicy in the sense of *resolving* which policy governs
  this project (most-specific-match over org × vertical — `Project` has no `region` column of its
  own, so that axis is never narrowed) and recording that resolution on `document_audit_log` — there
  is still no deletion scheduler to hand it to (RetentionPolicy's own note from the Governance
  session: "nothing consumes this value yet"; still true). The audit trail is retained intact —
  archiving never deletes or mutates any prior `audit_log`/`twin_audit_log`/`document_audit_log` row.
- **Contradiction/spec-drift detection that *compares* spec claims across documents is not built** —
  `SpecClaim.contradicts` exists (per §4.3's schema) but nothing populates it yet; that's Foresight
  (Prompt 7), which depends on this milestone's `SpecClaim` table existing and now can proceed.
- **`/admin/export/{project_id}`'s bundle (`app/api/admin.py`) still skips documents** — its own
  comment already flagged this ("Documents skipped this session — that milestone doesn't exist yet
  ... add a `documents` key/CSV once it does"). M3 exists now; adding that key is a genuine, narrow
  follow-up for whichever future session touches `admin.py` next, not done here since Prompt 6 never
  named it as this session's job.

**Already done, before this table existed** (the deterministic audit this plan is built on found
these solid): PRD Phase 2 (Ledger — extraction, evidence provenance, lifecycle state machine, audit
trail) and a real slice of Phase 9 (RBAC, project-scoped membership, time-boxed delegation, its own
append-only audit trail). See `CUE-PRD.md`'s audit artifact (link kept by the user) for the full
per-requirement evidence.

## Updating this file

When a milestone completes:
1. Flip its Status cell to `Done`, with the commit/date.
2. Note anything the next milestone's prompt should know that wasn't true when it was written
   (a design decision made mid-implementation, a scope adjustment, a discovered blocker).
3. Run `uv run pytest` from `backend/` one more time and confirm it's green before flipping the
   status — a milestone marked Done that doesn't pass its own tests is worse than one left
   `Not started`, since the next session will trust this table.
