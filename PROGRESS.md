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
| M4 | Foresight | `Prompt 7 — Foresight.txt` | §15 Phase 5 | M1, M3 | Done (2026-08-08) |
| M5 | Living WIP & reporting | `Prompt 8 — Living WIP and reporting.txt` | §15 Phase 4 (backend half) | M1, M2, M4 | Done (2026-08-08) |
| M6 | Ask & retrieval | `Prompt 9 — Ask and retrieval.txt` | §15 Phase 7 (backend half) | M3, M4 | Done (2026-08-08) |
| M7 | Vendor Reliability Graph | `Prompt 10 — Vendor Reliability Graph.txt` | §15 Phase 10 (backend half) | M2 | Done (2026-08-08) |
| M8 | Real channel capture | `Prompt 11 — Real channel capture.txt` | §15 Phase 1 | M2, M4 | Not started on Prompt 11's full scope (queue, normaliser, identity resolver, media pipeline, ASR, consent, gap detection) — genuinely gated on Pico credentials for WhatsApp/WeChat/Graph either way. Registry groundwork landed ahead of schedule (see M8 notes below): `channel_types` reference table + `ChannelAdapter` Protocol + FOSS adapters (Mattermost/IMAP-SMTP/Nextcloud), so the rest of this milestone builds on an open registry, not the closed enum it originally would have |
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
- Foresight (M4, now Done — see its own notes below) reads `app/twin/service.py`'s
  `compute_current`/`compute_propagation` for slack, critical path and hypothetical impact, and is
  what finally *decides* FR-LCY-02/03's automatic `committed → at_risk` / `at_risk → broken`
  transitions from that data.

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
    whichever tenant/site the config points at, so any Microsoft 365 sandbox tenant works
    identically to Pico's real one for demo purposes; swapping to Pico's tenant later is a
    credentials change, not a code change.
    **Correction (2026-08-08):** this note originally said the Microsoft 365 Developer Program
    sandbox tenant was free — that's no longer true. As of this date, enrolling requires an
    active paid Microsoft subscription (e.g. Visual Studio subscription); there is no cost-free
    path to a real `GraphSharePointAdapter` sandbox anymore. Testing this adapter for real now
    means either paying for that subscription tier or Pico's own tenant credentials — `noop`
    (already the default) is the only genuinely free path for demo/testing purposes, same as
    every other channel adapter now has to be treated (see M8's own notes once that milestone
    exists).
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
  `SpecClaim.contradicts` existed (per §4.3's schema) but nothing populated it yet as of this
  session; Foresight (M4, now Done) is what populates it — see that milestone's own notes below.
- **`/admin/export/{project_id}`'s bundle (`app/api/admin.py`) still skips documents** — its own
  comment already flagged this ("Documents skipped this session — that milestone doesn't exist yet
  ... add a `documents` key/CSV once it does"). M3 exists now; adding that key is a genuine, narrow
  follow-up for whichever future session touches `admin.py` next, not done here since Prompt 6 never
  named it as this session's job.

### M4 notes for later sessions

Closed FR-FOR-01 through 10 except FR-FOR-05 (photo verification — genuinely out of scope, per
Prompt 7's own EXPLICITLY OUT OF SCOPE section: needs real onsite photo capture and an object
store's image-matching capability that doesn't exist yet), FR-DEV-01 through 05, FR-NTF-01 through
05 except real push/email/Teams delivery (deferred to M8/M10), and FR-LCY-02/03 (the automatic
`committed → at_risk` / `at_risk → broken` transitions every earlier session left structurally
permitted but never wired to fire).

- **New module `app/foresight/`** — models, audit, threshold, risk (the shared dedup/supersede
  helper), silence, contradiction, forecast, deviation, escalation, notification, config, worker,
  schema, consequence — same per-domain-module-owns-its-models precedent `app/twin/` and
  `app/documents/` already established. Prompt 7's own instruction named the exact path
  (`app/foresight/models.py`, not `app/models/foresight.py`), consistent with that precedent.
- **`app/foresight/risk.py`'s `create_or_supersede_risk` is the single FR-FOR-10 dedup/supersede
  helper** every detector calls — silence.py, contradiction.py and forecast.py all go through it
  rather than three ad hoc checks, per the prompt's own explicit instruction. An existing *open*
  Risk for the same `(project_id, source, finding_key)` with no material change (severity/
  consequence/base_rate/detail all identical) is a no-op, not a duplicate insert; a materially
  different one retires the old row (`status='superseded'`, `superseded_by`) and inserts a fresh
  one, preserving history rather than mutating it in place.
- **Silence Radar's baseline is scoped to this project's own history with the vendor**, not the
  vendor's history across every project in the org — simpler to reason about and test; widening the
  sample once there's demand for it (a vendor genuinely fresh to one project but well-known
  elsewhere in the org) is a documented, deliberate follow-on, not an oversight
  (`app/foresight/silence.py`'s own module docstring).
- **Forecasting is a documented heuristic, not a learned model** (FR-FOR-06, same "no historical
  corpus yet" situation FR-TWN-08 already documented for the Twin): a milestone at or below the
  project's configured `forecast_slack_days` threshold, combined with either zero/negative slack on
  its own or an open Silence Radar flag on a commitment feeding it, is treated as high
  miss-likelihood. `Risk.base_rate` is always left `None` on a forecast-sourced Risk — that field is
  Silence Radar's own FR-FOR-02 requirement, and fabricating one here would be exactly the dishonest
  precision CLAUDE.md's Models table forbids. **Fixed milestones are excluded from the forecast scan
  entirely** — a fixed node's slack is definitionally zero (`app/twin/graph.py`'s forward/backward
  pass), the same reason that module's own `_binding_constraint` already excludes fixed nodes from
  candidacy; without this exclusion every fixed milestone in every project would trivially "forecast
  a miss" on every single scan (caught by writing the test for it, not by inspection).
- **`app/ledger/lifecycle.py` gained `apply_automatic_transition`** — the single execution primitive
  for FR-LCY-02/03, called by silence.py (`committed → at_risk` on silence), forecast.py (the same
  transition on forecast breach, and `at_risk → broken` on due-time-passed via
  `scan_overdue_commitments`). Reuses the exact write path `app/api/commitments.py`'s
  `transition_commitment` already established (`validate_transition`, set state, `record_audit_event`
  with `actor_id=None`, `recompute_on_commitment_transition`) rather than a parallel one. The
  commitment-state-machine DB trigger (migration `d05dce0f415d`) already permitted both transitions
  structurally — no schema change was needed there, only the orchestration to actually fire them.
- **Escalation (FR-FOR-08) is a periodic reconciliation sweep, not a Temporal workflow** —
  CUE-Tech-Stack.md §2.4 names Temporal for durable "escalation chains... human-in-the-loop flows,"
  but this session deliberately doesn't add it: a second, heavier piece of infrastructure this
  milestone's actual scope doesn't need, the same "no new infrastructure dependency needed at this
  scale" call `app/twin/graph.py`'s own module docstring already makes for a comparable tradeoff.
  `ESCALATION_CHAIN = ("project_manager", "producer", "administrator")` is a new, Foresight-local
  seniority ordering — `app/identity/service.py` has no role-hierarchy concept at all (only flat
  "does user X hold role Y"), so this doesn't extend that module, it reuses its underlying
  Membership/Delegation data model (delegation-aware routing) without inventing a new permission
  concept, per the prompt's own instruction.
- **This is the first session with a scheduled/background job** — `app/foresight/worker.py`, a real
  [arq](https://arq-docs.helpmanual.io/) worker connected to a new `valkey` docker-compose service
  (CUE-Tech-Stack.md §2.4: "Lightweight task queue — arq (Valkey-backed)"). Its one cron job,
  `run_foresight_sweep` (15-minute schedule), is a plain async function with no queue-specific
  dependency on its `ctx` argument, so it's directly callable by tests (and ops scripts) without a
  running worker or broker at all — confirmed with a real Valkey round-trip
  (`tests/test_foresight_worker.py`), not mocked. Project discovery (which projects exist, across
  every org) is the one deliberate RLS bypass in the codebase outside of test/script setup — connects
  as the schema-owner role, same as `tests/conftest.py`'s `owner_engine` and
  `scripts/extract_fixtures.py`, because there is still no service-account/agent identity in this
  codebase (M2's own notes: "a placeholder until that identity model exists"). Every actual read/
  write for a discovered project still runs through the ordinary RLS-enforced session with that
  project's own org context set.
- **Notification's only real delivery adapter is webhook** (`app/foresight/notification.py`'s
  `deliver_webhook`/`deliver_due_notifications`) — HMAC-SHA256-signed real HTTP POSTs to registered
  `WebhookSubscription` rows, brought forward the same way Documents' Prompt 6 session built a real
  SharePoint write-back ahead of its own original schedule. Push/email/Teams remain deferred to
  M8/M10 as Prompt 7 names — `Notification.delivered_via` has all four values in its enum, only
  `"webhook"` has a real sender.
- **Quiet hours (FR-NTF-04) are configured per-project, not per-user** — `QuietHoursConfig`, one row
  per project, local to `Project.timezone`; a project's operational rhythm (don't page anyone
  overnight) read as a team-wide property, the same tier `Project.timezone` itself already is. The
  "live-event window" override reuses `Project.event_start`/`event_end` directly rather than a
  second declared-window column — that pair already *is* the project's own declared live-event
  window.
- **`ForesightThreshold` (FR-FOR-07) extends `RetentionPolicy`'s "axis, axis, value; NULL broadens"
  config-table pattern** (org × vertical × region there; org × project × deviation_class here) —
  same `/admin/*`, `require_org_administrator`-gated shape `app/api/retention.py` already
  establishes, per Prompt 7's own explicit instruction not to invent a parallel mechanism.
  `DEFAULT_THRESHOLDS` (`app/foresight/threshold.py`) are the documented, honest fallback values used
  before a PM ever configures a row — not fabricated precision.
- **`deviation_class` ontology terms are seeded vertical-scoped (event-production) from the start**,
  not universal-core the way `commitment_act`/`milestone_type` originally were — `milestone_type`
  later needed a whole re-key migration (`eed5a4da79f6`) once a second vertical became roadmap-real,
  and `b32b62ca45e4`'s own comment already flagged `deviation_class` specifically as
  "construction-adjacent" (the same collision risk). `seed_data/deviation_classes.py` +
  migration `ab0dc47865c9`; `tests/conftest.py`'s `_reseed_universal_ontology_terms` was extended
  with the same vertical-scoped INSERT shape `MILESTONE_TYPES` already needed.
- **`evidence`'s exactly-one-subject CHECK widened from 4 to 5** (migration `45309b1d751f`, same
  shape `f3a1c9d7e4b2` used to widen it from 2 to 4) — `Deviation` gets the same evidence-required
  deferred-constraint-trigger mechanism as Commitment/Budget/DocumentVersion/SpecClaim (FR-DEV-02).
- **`app/api/deviations.py`'s `POST .../{id}/confirm` doubles as this resource's "update" verb** —
  item 7 lists "list · create · update · resolve"; there is no separate PATCH endpoint, since a
  confirm-with-corrections call already covers editing either an auto-drafted or an already-confirmed
  row identically (mirrors `app/api/commitments.py`'s `verify_commitment` two-step shape exactly).
- **`WebhookSubscription.secret` is shown exactly once**, in the create response
  (`WebhookSubscriptionCreated`) — never re-exposed by list/read afterward, same posture an API key
  would get.
- Full new-endpoint RLS + role-gating coverage (both properties, independently, per Prompt 7's own
  testing expectation): `tests/test_risks_api.py`, `test_deviations_api.py`, `test_webhooks_api.py`,
  `test_foresight_thresholds_api.py`. Escalation routing extends `tests/test_delegation.py`'s own
  `_member`/`_bare_user` fixture shape with a real `Delegation` row, proving delegation-aware routing
  (not just membership-based) end to end — `tests/test_foresight_escalation.py`.

### M5 notes for later sessions

Closed FR-RPT-01 through 10 (09/10 are Should-priority, implemented as a narrow
config surface per Prompt 8's own "don't over-invest" instruction — see below).

- **New module `app/reports/`** — models, schema, composer, render,
  export_html, export_pptx, export_pdf, service, schedule, templates — same
  per-domain-module-owns-its-models precedent every prior domain module
  established.
- **Only two tables**, deliberately: `ReportSnapshot` (FR-RPT-08) and
  `ReportScheduleConfig` (FR-RPT-09/10). The "current" report itself is
  recomputed fresh on every `GET .../report/current` call
  (`app/reports/composer.py`'s `compose_report`) — no cache, no "current
  report" table, per Prompt 8's own instruction and NFR-PRF-05's 30-second
  export bar being realistic without one. `ReportSnapshot` is genuinely
  immutable — a real DB trigger forbids *any* UPDATE or DELETE (not just a
  SET-NULL-on-delete carve-out like the audit-log tables have, since nothing
  else holds a FK into this table that would ever need one).
- **Every scalar figure is wrapped in a `ReportField`**
  (`app/reports/schema.py`) carrying `available`/`unavailable_reason`,
  `verification_state` and a `provenance: list[ReportProvenance]` — this is
  §8.4's "the report composer refuses to render a field without a resolvable
  provenance link" made structural, not textual: an ungrounded figure is
  `available=False` with a reason, in the response body, never a fabricated
  number or a silently missing key. `current_phase` (Challenge Brief's own
  "current project phase" field) is *always* structurally unavailable — no
  phase-tracking field exists at the project level anywhere in this schema
  (`Document.phase_term_id` tags individual documents, not the project), and
  inventing a phase-from-milestone heuristic would have been exactly the
  fabricated precision CLAUDE.md's Models table forbids elsewhere.
- **§6.10's four-field budget-summary grounding rule has exactly one
  implementation** — `app/reports/composer.py`'s `compute_budget_summary` —
  reused by both the report itself and the export verification gate
  (FR-RPT-06), so the numbers a Producer sees and the gate that blocks
  export can never diverge. `outstanding_payments` deliberately has **no
  state filter**, summing every commitment (any state, including
  `withdrawn`) where `payment_status IS DISTINCT FROM 'paid'` — the PRD's
  own §6.10 paragraph has no state carve-out for that field, and CLAUDE.md's
  "don't compute any of them differently than that paragraph specifies" was
  followed literally rather than narrowed on an assumption of what was
  "really" meant.
- **FR-RPT-05 (in-place editing) reuses the existing Commitment/Budget/
  Deviation mutation endpoints directly** (`POST .../commitments/{id}/verify`,
  `POST .../budget/revise`, `POST .../deviations/{id}/confirm`) — a
  deliberate decision **not** to build a parallel report-specific edit
  endpoint. Each of those endpoints already records the correction via the
  existing audit trail with author and timestamp, which is exactly what the
  PRD's own wording asks for; a future frontend's inline-edit controls call
  these directly, then re-fetch `GET .../report/current` to see the
  recomputed figures. No new code needed for this Must-priority item beyond
  documenting the decision.
- **Vendor status summary degrades gracefully when the Vendor Reliability
  Graph (M7) doesn't exist yet** — `app/reports/composer.py` attempts
  `from app.parties.reliability import get_reliability_metrics` in a
  try/except `ImportError` at module load; there is no `app/parties/`
  package at all yet, so this always takes the except branch today and every
  vendor's `reliability` field reports structurally unavailable, citing M7's
  own PROGRESS.md status. Starts resolving real metrics automatically the
  day M7 adds that module — no change needed here.
- **Project overview's visual references (FR-RPT-03) resolve via the only FK
  path Deliverable and Document actually share** —
  `SpecClaim.deliverable_id -> DocumentVersion -> Document` — since
  `Document` itself carries no direct `deliverable_id` (confirmed: no such
  column exists). Only a `Document.current_version_id` whose `approved_at`
  is set is embedded; an unapproved or missing current version is reported
  structurally unavailable, never a stale/unapproved image substituted in
  (FR-RPT-03: "always the current version, never a stale one"). Vendor
  status is grouped by `Party` directly, not a finer "vendor category" —
  `ontology_terms`' own `vendor_category` category is named in a comment
  (`app/models/ontology.py`) but has never been seeded or populated by any
  session, so grouping by it would invent structure that doesn't exist yet.
- **Export: one composition path, two renderers** — `app/reports/render.py`
  normalises a composed `LivingWipReportOut` into a presentation-agnostic
  `ReportRenderModel`; `export_pptx.py` (python-pptx, placeholder-branded per
  `app/reports/templates.py`'s registry — no real Pico branding available,
  the swap point is documented in that module) and `export_html.py` +
  `export_pdf.py` (Playwright print-to-PDF over server-rendered HTML) both
  read only that model, never `LivingWipReportOut` directly, per WHAT TO
  BUILD #3's own instruction.
- **FR-RPT-09/10 rides the existing arq worker** — `app/reports/schedule.py`'s
  `run_due_report_schedules` (hour-granularity match against
  `ReportScheduleConfig`, same schema-owner discovery-bypass reasoning
  `app/foresight/worker.py`'s own sweep already documents for "no
  service-account identity exists yet") is registered onto the *same*
  `WorkerSettings` in `app/foresight/worker.py` rather than a second
  broker/worker process for one lightweight periodic scan — Prompt 8's own
  "don't over-invest" instruction, taken literally.
- **Export/schedule-write endpoints are gated `ADMIN_ROLES`**
  (`{"administrator", "producer"}`, `app/identity/service.py`) — reused
  rather than inventing a report-specific role tier, since §12.2 already
  makes "Freeze & Export" a Producer-owned control and `ADMIN_ROLES` is the
  existing tier for exactly that kind of project-provisioning-adjacent
  action (channel attach, membership add). Read access (`current`,
  `snapshots`, `schedule` listing) stays at plain project-membership level
  (`get_project`), same as every other read surface in this codebase.
- Full coverage per Prompt 8's own testing expectation, `tests/test_reports_api.py`:
  budget-summary math against six commitments exercising every corner of
  §6.10's grounding rule (including the "unavailable without a baseline"
  case), export-blocked-409 naming the exact blocking commitment(s), export
  succeeding twice into two distinct, independently-readable snapshots, a
  real DB-trigger immutability check (direct `UPDATE` against the RLS-
  enforced app role still rejected), PDF export exercised end-to-end via a
  real headless Chromium, RLS via `report_snapshots`' project-join policy,
  role-gating as an independent property (`project_manager` can view but not
  export; `producer` can), FR-DEV-05's Risk-and-Deviation rollup, and the
  scheduled-runner path (a due schedule produces a real `trigger='scheduled'`
  snapshot; a second run within the same hour doesn't double-fire).

### M6 notes for later sessions

Closed FR-ASK-01 through 08. FR-ASK-06's outbound *execution* half (the actual chase/draft/reschedule)
is still out of scope until Write-back (M9) exists — but its own "do not fake an action taken
response, say it can't do that yet" clause is closed this session too, structurally, not by prompt
wording (see the intent-gating note below).

- **New module `app/ask/`** — config, embeddings (Protocol + Ollama/TEI provider factory, mirroring
  `app/llm/`'s shape but a genuinely separate interface, per Prompt 9's own instruction not to couple
  the two), models, embed_worker, retrieve, schema, answer, summarise, brief, service — same
  per-domain-module-owns-its-models precedent every prior domain module established.
- **`DocumentVersion.embedding` (left unpopulated by M3 on purpose) is now populated**, by
  `app/ask/embed_worker.py`'s periodic sweep, not synchronously on the request path. Evidence and
  AuditLog text — which have nowhere else to hold an embedding/tsvector without widening a table
  several other domains depend on — get their own new table, `RetrievalChunk` (`app/ask/models.py`),
  rather than a second embedding column bolted onto either. Not a fourth: `Commitment` text is
  reachable through its own required `Evidence` row (every commitment has at least one, per
  CLAUDE.md's hard rule), so embedding Evidence already covers "the ledger" per FR-ASK-01, without a
  separate index over `Commitment.deliverable_en`.
- **Evidence has no RLS policy of its own** (checked every migration — commitments/budgets/documents/
  document_versions/spec_claims/deviations all got a `tenant_isolation` policy; `evidence` never did,
  and it has no `project_id` column either). `app/ask/embed_worker.py`'s `_evidence_for_project`
  scopes it via an explicit join through whichever of the five subject FKs is set — the same "RLS
  plus an independent application-level check" posture `app/api/deps.py`'s
  `require_org_administrator` already documents elsewhere. Worth fixing at the source (an RLS policy
  on `evidence` via the same five-way join) if a future session touches that table again, but out of
  this session's own scope to retrofit.
- **Hybrid retrieval is one function** (`app/ask/retrieve.py`'s `hybrid_retrieve`), fusing
  `DocumentVersion` and `RetrievalChunk`'s lexical (`ts_rank`) and semantic (`pgvector` cosine
  distance) signals via Reciprocal Rank Fusion rather than trying to normalise the two onto one scale
  — RRF only needs each signal's ordering, which is the only thing either can actually be trusted to
  give.
- **FR-ASK-02's "say so, never assert" and FR-ASK-06's "say it can't do that yet, don't fake it" are
  both Pydantic-enforced structural invariants, not prompt instructions** — `AskAnswerOut`
  (`app/ask/schema.py`) has a `model_validator` that makes it impossible to construct an
  `available=True` answer with zero citations or a `refusal_kind` set, or an `available=False` answer
  that carries prose or omits a `refusal_kind`. The reasoning model proposes which retrieved excerpts
  it used, but every proposed id is checked against the real retrieval hits before being trusted
  (`app/ask/answer.py`'s `_resolve_citation` path) — a hallucinated id is silently dropped, never
  passed through, same "verified in code, not trusted" discipline CLAUDE.md sets for extraction
  evidence spans.
- **FR-ASK-06's fake-action guard is a separate, schema-constrained classification step
  (`app/ask/intent.py`'s `classify_intent`), not an instruction folded into the answer-generation
  prompt** — telling the answering model "please don't fabricate an action" inside its own prompt
  would not have been enforcement (that call's `answer` field is free text; nothing code-level would
  stop it narrating "I've sent a message to the vendor" while citing real but action-unrelated
  evidence). `answer_query` calls `classify_intent` first, on every request, and branches on its
  result in plain code: an action-shaped request is refused with `refusal_kind:
  "action_not_yet_supported"` before retrieval or the answer-generation call ever run — there is no
  execution path in which the model capable of writing that fabricated sentence is invoked for such a
  request, not a prompt asking it not to. `classify_intent` itself fails open (treated as "not an
  action") if the reasoning model can't be reached at all, same NFR-AVL-03 degrade-gracefully posture
  `_embed_question` already has for the embedding client, and for the same reason: an unreachable
  reasoning model already breaks the answer-generation call a genuine question would need, so failing
  open here doesn't introduce a new failure mode.
- **FR-ASK-08's session-boundary rule is an explicit `conversation_id`, not time-based expiry** —
  decided and documented in `app/ask/models.py`'s `AskConversation` docstring. A caller either omits
  `conversation_id` (a new conversation is created and its id returned) or passes one back; there is
  no server-side TTL. Chosen for determinism and testability over a wall-clock-dependent rule.
- **`summarise`'s five variants reuse `app/reports/schema.py`'s row/field types directly**
  (`ReportField`, `CommitmentSummary`, `DecisionLogRow`, `VendorStatusRow`) rather than a parallel set
  — `vendor_status` even reuses `app/reports/composer.py`'s own `_compose_vendor_status` function
  outright. FR-ASK-05 (outstanding actions by owner *and* by due window, in one call) is its own
  variant, deliberately not treated as already covered by `GET /commitments`'s state/party/due-window
  filters (Prompt 9's own instruction) — it groups the same open-commitment set two ways at once,
  assistant-shaped rather than a filterable table.
- **`successor-brief` is the same composer-pattern `app/reports/composer.py` established** (Prompt 8)
  — one `compose_successor_brief()` calling one per-section async helper each, reusing that module's
  own `_commitment_summary`/`_OPEN_RISK_STATUSES` rather than a second implementation.
  `deviations_and_resolutions` deliberately includes every deviation, not just open ones (a handover
  needs to see what was already resolved and how), unlike the Living WIP report's own risk/issues
  section, which is current-only by design.
- **`run_embedding_sweep` rides the existing arq worker**, same "don't stand up a second broker for
  one more periodic job" reasoning M5's own `run_due_report_schedules` already established —
  registered onto `app/foresight/worker.py`'s `WorkerSettings` alongside the other two.
- Full coverage per Prompt 9's own testing expectation (`tests/test_ask_*.py`): citation-or-refuse
  behaviour asserted on the typed fields directly (not string-sniffing), a hallucinated citation id
  dropped, the answer-generation call provably never reached when retrieval finds nothing or the
  request is action-shaped (`FakeReasoningClient.answer_calls` asserted empty — the intent
  classification call itself still runs, always; see `tests/test_ask_answer.py`), an action-shaped
  request refused with `refusal_kind="action_not_yet_supported"` without retrieval ever running
  either, `classify_intent`'s own fail-open behaviour on an unreachable model
  (`tests/test_ask_intent.py`), cross-project isolation on retrieval (two projects in the same org, a
  query never crosses), successor-brief section completeness against a project seeded with one of
  everything, RLS + role-gating as two independent properties (a `read_only` member can use every Ask
  endpoint; a non-member is 404'd, not 403'd), and follow-up/conversation-ownership (a conversation id
  from a different user is rejected).

### M7 notes for later sessions

Closed FR-VRG-01 through 05 and 07 (Must); FR-VRG-06 (Should) is a structurally-honest
placeholder, not a real pipeline — see below.

- **New module `app/parties/`** — models, compute, service, reliability, schema — same
  per-domain-module-owns-its-models precedent every prior domain module established. Prompt 10's
  own text named `app/vrg/compute.py`; the real path is `app/parties/compute.py` instead, because
  `app/reports/composer.py`'s M5-era stub already committed to `from app.parties.reliability import
  get_reliability_metrics` (its own comment: "starts resolving real metrics automatically the day
  M7 adds that module") — `app/parties/` is the name that import actually needs, and the REST
  resource is `/parties/{id}/reliability` (§11.2) either way, so this session followed the real
  constraint already in the codebase over the prompt's own suggested path.
- **The supersedes/revision-churn decision: option (a), "structurally absent, not zero."**
  Confirmed at the start of this session (grep for `supersedes` outside its own model declaration)
  that FR-LED-05 (supersession detection/linking) has never been implemented by any prior session —
  `Commitment.supersedes` has never been written to. Building a minimal supersession-linking
  mechanism as a side effect of this milestone (option b) would have meant implementing a Must-
  priority Ledger requirement (FR-LED-05) that isn't this session's job, on top of a metric that's
  supposed to consume it, not produce its input data. Instead, `app/parties/compute.py`'s
  `compute_revision_churn`/`compute_price_drift` both run a real, live structural check
  (`_supersedes_data_exists` — does *any* commitment in this org have a non-empty `supersedes`
  array right now) and report `value=None`/`unavailable_reason` naming FR-LED-05 by number when it
  doesn't, rather than a hardcoded "not implemented" flag or a fabricated `0.0`. The computation
  logic itself is real and tested (`tests/test_parties_compute.py`'s
  `test_revision_churn_and_price_drift_compute_once_supersedes_exists` hand-writes a `supersedes`
  chain directly to prove it) — the day a future session implements FR-LED-05, both metrics start
  reporting real numbers automatically, no change needed here, same "wired for the day the
  dependency arrives" idiom this milestone reused from `app/reports/composer.py`'s own
  `_VRG_AVAILABLE` guard.
- **FR-VRG-02's two missing segmentation columns were added this session, deliberately minimal**:
  `Party.vendor_category_term_id` (the `vendor_category` ontology_terms category, named in a comment
  since the very first migration but never wired to an actual column — `seed_data/vendor_categories.py`
  + migration `09bcd1591445`, vertical-scoped from the start per `deviation_class`'s own precedent,
  not repeating `milestone_type`'s original universal-core mistake) and `Party.city` (a plain nullable
  string — a vendor's city has no taxonomy or reuse need of its own, unlike vendor category).
  `Project.archetype_code` (migration `f0c711e76ab4`) is new too: `MilestoneArchetype` is a template,
  never referenced again after `materialize_archetype` copies it (M1's own notes), so nothing
  previously recorded which archetype a project actually resolved to — this column is the only place
  that survives now, set once inside `materialize_archetype` itself. Setting it on an
  already-flushed `Project` row triggers `updated_at`'s server-side `onupdate`, which expires that
  attribute — `app/api/projects.py`'s `create_project` needed the same explicit
  `session.refresh(project, attribute_names=["updated_at"])` fix `app/api/commitments.py`'s
  `_refresh_updated_at` already documents elsewhere (found by the full suite actually failing on
  `ResponseValidationError`, not by inspection).
- **`vendor_metrics` (`app/parties/models.py`'s `VendorMetric`) is append-only history, not
  upsert-in-place** — every `recompute_vendor_metrics` call (FR-VRG-03) writes a fresh row per
  (metric, `segment_event_archetype`) pair rather than updating an existing one, which is what makes
  §11.2's "history" operation (`GET /parties/{id}/reliability/history`) meaningful at all; "metrics"
  (the current snapshot) is just the latest row per segment via a `DISTINCT ON` query
  (`app/parties/reliability.py`). RLS follows Party's own org-direct pattern (`organisation_id`
  denormalised onto every row, policy shaped like `organisations`/`foresight_thresholds`), not the
  project-join shape most tenant-scoped tables use — Party itself is org-scoped, per its own
  docstring, and this milestone deliberately computes a vendor's metrics across every project they've
  touched in the org, not narrowed to one (unlike Silence Radar's own project-scoped baseline, which
  this session changed the *consumer* side of — see below — without changing that baseline's own
  narrower definition, since `tests/test_foresight_silence.py` still exercises it directly by name).
- **FR-VRG-04 is a real integration, not just a note**: `app/foresight/silence.py`'s
  `compute_vendor_baseline` now delegates to `app/parties/compute.py`'s
  `compute_median_response_time_days` (project-scoped) instead of owning that query itself, and
  `scan_silence` now prefers the *org-wide* figure (the same function, called with no project filter)
  over the project-only one, falling back only when the org-wide figure isn't computable yet.
  `tests/test_foresight_silence.py`'s `test_scan_silence_prefers_org_wide_vrg_baseline_over_project_only`
  proves this changes real behaviour, not just an internal number nobody reads: the exact fixture that
  flags a silence Risk under the old project-only baseline no longer flags once a second project's
  history for the same vendor exists in the org.
- **`app/reports/composer.py`'s `_compose_vendor_status` now actually calls
  `get_reliability_metrics`** — M5's own stub already imported it behind an `ImportError` guard and
  claimed "no change needed here" once M7 landed, but that claim was wrong: the guarded branch still
  unconditionally built an `unavailable` `ReportField` regardless of `_VRG_AVAILABLE`, never calling
  the function it had just imported. Fixed as part of this session (not deferred) since it directly
  affects this milestone's own consumer-facing surface;
  `tests/test_reports_api.py::test_vendor_status_reliability_resolves_once_vrg_has_a_metric` proves
  a real on-time-rate figure now reaches the Living WIP report. `on_time_rate` was picked as the one
  headline "reliability" scalar that section's single `ReportField` can carry — the full segmented
  metric set lives at `/parties/{id}/reliability` itself, not duplicated into the report.
- **FR-VRG-05 reuses `FINANCE_ROLES` (`{"finance", "producer"}`)**, per the Governance session's own
  note that this milestone should. Since Party — and therefore this endpoint — is org-scoped, not
  project-scoped, the existing `require_project_role` dependency doesn't apply (there's no
  `project_id` in `/parties/{id}/reliability` to check membership against); `app/api/deps.py` gained
  `require_org_finance`, the same "does this user hold role X on at least one project in their own
  org" shape `require_org_administrator` already established for `/admin/*`, checked against
  `FINANCE_ROLES` instead of `{"administrator"}`.
- **FR-VRG-06 (Should) is implemented exactly as the prompt scoped it — a filterable query parameter,
  not a pipeline** — and, on inspection, deliberately *not* wired to real data this session.
  "Cross-Pico-office aggregation" in this codebase's multi-tenant model means cross-*organisation*
  (each Pico office is its own `organisation_id` tenant boundary); genuinely aggregating a vendor's
  metrics across orgs would need both an RLS bypass (a new, deliberate exception to CUE-Tech-Stack.md
  P1, the kind this codebase has only ever made for the arq worker's own project-discovery sweep) and
  a way to know two `Party` rows in different orgs are "the same vendor" at all, which nothing in
  this schema establishes. Building either was judged out of a Should-priority item's scope for this
  session — `vendor_category`/`city`/`event_archetype` on the existing single-party endpoint are the
  real, working segmentation FR-VRG-02 asks for; FR-VRG-06 itself is intentionally left for a future
  session with an actual cross-org vendor-identity mechanism to build against.
- **FR-VRG-07 ("never expose a vendor's metrics to another vendor")** — per the prompt's own note,
  satisfied by construction (P1 has no vendor-facing surface anywhere in this codebase), proven
  explicitly rather than left as an assumption:
  `tests/test_parties_reliability_api.py::test_no_auth_header_is_401_no_vendor_facing_route` asserts
  the one route this milestone adds rejects a request with zero credentials outright (401, not even a
  403 that would imply the route is reachable to try a role against).
- Full coverage per Prompt 10's own testing expectation: exact-value metric computation against
  hand-built commitment histories (`tests/test_parties_compute.py`, including the
  supersedes-unavailable-then-available pair), the append-only-history/segmentation-by-archetype
  write path (`tests/test_parties_service.py`), RLS and role-gating as two independent properties
  plus cross-vendor isolation and the no-vendor-facing-route proof
  (`tests/test_parties_reliability_api.py`), the FR-VRG-04 Silence Radar behaviour change
  (`tests/test_foresight_silence.py`), and the FR-RPT-02 vendor-status wiring fix
  (`tests/test_reports_api.py`).

**Already done, before this table existed** (the deterministic audit this plan is built on found
these solid): PRD Phase 2 (Ledger — extraction, evidence provenance, lifecycle state machine, audit
trail) and a real slice of Phase 9 (RBAC, project-scoped membership, time-boxed delegation, its own
append-only audit trail). See `CUE-PRD.md`'s audit artifact (link kept by the user) for the full
per-requirement evidence.

### M8 notes for later sessions

Registry/schema groundwork landed ahead of Prompt 11's own scheduled start, driven by a product-
strategy clarification: CUE is a multi-tenant SaaS intended to support a different tenant's own
3rd-party channel stack, not just Pico's WhatsApp/WeChat/Microsoft 365 — the closed native
`channel_type` Postgres enum Prompt 11 would otherwise have built directly on top of could never
deliver that (adding a brand meant an `ALTER TYPE` migration touching every tenant). Before starting
the rest of Prompt 11 (queue, normaliser, identity resolver, media pipeline, ASR, consent, gap
detection, FR-DOC-09), read this section — the foundation those items build on changed shape.

- **`channel_types` reference table** (`app/models/channel_type.py`) replaces `ChannelType`, the
  native enum `app/models/project.py` used to define — same "reference data, not enums" instinct
  `ontology_terms` already established (CUE-Tech-Stack.md §5.2 has the full reasoning for why this
  is a second table, not folded into `ontology_terms` itself). `channels.type` and
  `channel_identities.channel_type` are now FKs to `channel_types.code` (natural-key FK, not
  `ontology_terms`' surrogate-id pattern); `evidence.channel` deliberately stayed a plain string, no
  FK — it's a provenance/display label, not an adapter-selection key, and forcing an FK there
  would've touched 15+ unrelated test fixtures for no behavioural benefit. `GET /channel-types` is
  the read-only discovery endpoint replacing the old `ChannelTypeLiteral`/`EvidenceChannelLiteral`
  static Literals.
- **`app/capture/adapters/`** — the `ChannelAdapter` Protocol (`fetch_backlog`/`stream`/`send`/
  `health`) Prompt 11 item 2 asked for, plus every adapter class: `FixtureAdapter` (wraps the
  existing `app/capture/fixtures.py`, kept as the permanent dev/test backend, unchanged in behaviour),
  `WhatsAppAdapter`/`WeChatWorkAdapter`/`GraphAdapter` (code-complete, credential-blocked — genuinely
  untested against real Pico infrastructure, exactly Prompt 11's own accepted state for this
  milestone), and three real, live-testable FOSS adapters standing in for the Microsoft-equivalent
  capabilities until Pico's Entra ID app registration exists: `MattermostAdapter` (team_collaboration),
  `ImapSmtpAdapter` (email), `NextcloudAdapter` (file_storage) — see CUE-PRD.md §9.3a. A single global
  `CUE_CAPTURE_BACKEND=fixture|live` switch gates all of them uniformly (not nine per-channel
  switches), so the full test suite stays credential-free regardless of which live adapters are
  configured. `app/documents/sharepoint.py` gained a matching `NextcloudWriteBackAdapter` (outbound,
  `CUE_SHAREPOINT_PROVIDER=nextcloud`) alongside its existing `noop`/`graph` providers, and its Graph
  auth was extracted into `app/core/graph_auth.py::GraphTokenProvider`/`GraphSettings` so the
  write-back adapter and the new capture-side `GraphAdapter` share one Entra ID credential
  (`CUE_GRAPH_*`) instead of `SharePointSettings` keeping its own copy — a scoped env-var rename,
  not a duplicated credential block. `tests/test_sharepoint_adapter.py` was updated for the new
  two-settings-object shape (`GraphSharePointAdapter(settings, graph_settings)`); every other
  existing test in the suite passed unmodified.
- **Not yet built**: the ingestion queue, normaliser, identity resolver, media pipeline, ASR, consent
  wiring, capture-health scheduling, gap detection/backfill, scheduled extraction windows, and
  FR-DOC-09 — Prompt 11 items 3-12, still fully open. They now consume `get_adapter(channel.type)`
  (`app/capture/adapters/registry.py`) and FK-backed `ChannelIdentity.channel_type` instead of the
  closed enum, but none of that consumption exists yet.
- **Mattermost/Nextcloud/GreenMail are real, running local instances, not just code** —
  `docker-compose.yml` gained `nextcloud`, `mattermost` (+ dedicated `mattermost_postgres`, since
  Mattermost has no SQLite support), and `greenmail` (a purpose-built open-source test mail server —
  dynamic mode, one pre-configured mailbox, real IMAP/SMTP protocol without docker-mailserver's
  domain/DKIM setup overhead). `mattermost/mattermost-team-edition` has no arm64 build — runs under
  Docker Desktop's amd64 emulation (`platform: linux/amd64`) on Apple Silicon, confirmed working via
  `docker manifest inspect`, just slower to start. Two real installer gotchas hit and fixed, both
  worth knowing before touching this compose file again:
  - Nextcloud's unattended-install entrypoint does **not** trigger from `NEXTCLOUD_ADMIN_USER`/
    `_PASSWORD` alone, even for SQLite — it needs an explicit `SQLITE_DATABASE` env var too, or it
    silently falls back to "finish setup via the web wizard" and never becomes `installed:true`.
    Confirmed by reading the image's own `/entrypoint.sh`.
  - The Mattermost image has **no shell, curl, or wget at all** (`docker exec ... sh` fails with
    "executable file not found") — a `curl`-based `healthcheck:` override silently reports
    permanently unhealthy. It ships its own image-baked `HEALTHCHECK` (`mmctl system status
    --local`); don't override it, just don't define one in compose at all.
  - Admin user, team (`cue-project`), channel (`vendor-updates`, id `ttjme9pe83d19geior9qbf5k3c`)
    and bot account (`cue-capture-bot`) were provisioned via `mmctl` (bundled in the image) — bot
    creation specifically requires a real authenticated session (`mmctl auth login`), `--local` mode
    rejects it ("This command cannot be run in local mode"). Nextcloud's app password was minted via
    `occ user:add-app-password cue --password-from-env` (full capabilities; without
    `--password-from-env` it still creates one but with reduced capabilities). `CUE`/`CUE-Capture`
    WebDAV folders were created for real via `MKCOL`, not assumed to pre-exist.
  - `backend/.env` now carries this session's real local credentials for all three (Mattermost bot
    token, Nextcloud app password, GreenMail mailbox) — `CUE_CAPTURE_BACKEND` and
    `CUE_SHAREPOINT_PROVIDER` stay at their safe `fixture`/`noop` defaults regardless; flipping either
    is a one-line change now that the credentials are already sitting there.
  - **Real gap this surfaced in `ImapSmtpAdapter` itself** (not a GreenMail-only quirk — a genuine
    adapter bug now fixed): it unconditionally called `starttls()`/used `IMAP4_SSL`, which breaks
    against any plain-text test/dev mail server. Added `imap_use_ssl`/`smtp_use_starttls` toggles
    (`app/capture/config.py`, both default `True` — real mail infrastructure always needs them; only
    a plain local test server sets them `False`). Also split `username` (AUTH identity) from a new
    `from_address` field (`From:` header / mailbox address) — GreenMail's AUTH only accepts the bare
    local part (`cue`), not the full address (`cue@cue.test`), which a single conflated field can't
    represent correctly against every real server. And `smtplib`'s default "initial response"
    optimization for the LOGIN mechanism isn't universally supported (GreenMail rejects it outright)
    — `conn.login(..., initial_response_ok=False)` is unconditionally the safer default.
  - Verified end-to-end against the real containers (send → real fetch_backlog round trip, or a real
    external upload → real fetch_backlog, plus a real health() call) for all three — not just
    "constructs without raising." `tests/test_capture_adapters_live.py` codifies this as
    `pytest.mark.skipif`-gated integration tests (skip cleanly with no local `.env` credentials
    configured; fail for real, not skip, if configured but the containers are down — the correct
    signal, not something to paper over).

## Updating this file

When a milestone completes:
1. Flip its Status cell to `Done`, with the commit/date.
2. Note anything the next milestone's prompt should know that wasn't true when it was written
   (a design decision made mid-implementation, a scope adjustment, a discovered blocker).
3. Run `uv run pytest` from `backend/` one more time and confirm it's green before flipping the
   status — a milestone marked Done that doesn't pass its own tests is worse than one left
   `Not started`, since the next session will trust this table.
