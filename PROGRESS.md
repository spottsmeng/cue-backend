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
| M8 | Real channel capture | `Prompt 11 — Real channel capture.txt`, `Prompt 11b — Real channel capture (continued).txt` | §15 Phase 1 | M2, M4 | **Done**, with the same per-adapter honesty this milestone has had since item 2: items 1, 3–12 all genuinely built and tested this session (see M8 notes below) — real for everything not credential-blocked (Mattermost/IMAP-SMTP/Nextcloud capture, identity resolution, party-org effective-dating, consent, capture health, gap reconciliation, scheduled windows, FR-DOC-09 drift) or dependency-blocked in this sandbox (PaddleOCR, FunASR SenseVoice — real FOSS substitutes run instead, see below); code-complete/credential-blocked for WhatsApp/WeChat/Graph, unchanged from item 2 |
| M9 | Write-back | `Prompt 12 — Write-back.txt` | §15 Phase 6 | M8 | Done (2026-08-09) |
| M10 | Hardening & observability | `Prompt 13 — Hardening and observability.txt` | §15 Phase 11 | all of the above | Done (2026-08-09) |
| M11 | Layer B Channel Picker | `Layer B Channel Picker — Implementation Prompt.txt` | §11.2/FR-ADM-06 (gap closure) | M8 | **Done (2026-08-17), live-verified against the real linked WhatsApp account** — see its own section below |

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
- **Items 1, 3–12 (Prompt 11b) are now built**, in a follow-up session — see "M8 items 1, 3-12 notes"
  below for the full account. They consume `get_adapter(channel.type)`
  (`app/capture/adapters/registry.py`) and FK-backed `ChannelIdentity.channel_type` instead of the
  closed enum, as this section originally anticipated.
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
- **`backend/.env`'s email block now points at a real Google Workspace mailbox (credentials in
  `.env`, gitignored — not named here), not `greenmail`** — driven by an explicit product requirement: dev/test/demo
  need genuine send-and-receive round trips, not protocol-level-only testing against a closed-loop
  double (GreenMail accepts SMTP and stores in memory; it never relays to or receives from the real
  mail network in either direction — confirmed, not a config gap). Gmail's IMAP/SMTP is TLS-secured
  by default, matching `ImapSmtpSettings`' own secure defaults exactly (`imap_use_ssl`/
  `smtp_use_starttls` both `True`) — the swap touched zero adapter code, only `.env` values, which is
  itself the proof the protocol-not-vendor design argument actually holds.
  - **Real test bug this surfaced**: `test_imap_smtp_send_and_fetch_backlog_round_trip` hardcoded
    `to_external_id="cue@cue.test"` — harmless against greenmail (one dynamic mailbox, any address
    resolves to it), silently wrong against a real external mailbox (the send would succeed but land
    nowhere the test's own IMAP fetch would ever see, since a real mail system has no such address).
    Fixed to self-send to `settings.from_address or settings.username` — the same shape works
    correctly against either backend.
  - GreenMail's config is kept, commented, directly below the Gmail block in `.env` — the fast,
    no-live-inbox-traffic fallback for routine dev loops (rate limits, working offline) without
    touching any code, same one-block swap in reverse.
  - Tradeoff, stated plainly: every `uv run pytest` run now sends and receives one real email —
    `uv run pytest` went from ~32s to ~67s, entirely Gmail's real network round trip, and a real
    `Quote ready, live-test-<hash>` message lands in a real inbox on every run. Accepted deliberately,
    not overlooked — matches the explicit "if a message needs to be sent/received, I need this to
    genuinely happen" requirement driving this swap.

### M8 items 1, 3-12 notes for later sessions

Follow-up session, driven by `Prompt 11b — Real channel capture (continued).txt`. Read that file and
`Prompt 11 — Real channel capture.txt` in full before touching anything in `app/capture/` — this
section summarizes what landed, not a replacement for either prompt. Baseline at the start of this
session: 363 passing. At the end: **447 passing**, real Postgres/MinIO/Valkey/Mattermost/Nextcloud/
GreenMail infrastructure throughout, zero mocks.

- **Item 1 — Message model + migration** (`app/capture/models.py`, migration `b6e4a1c9f235`):
  `Message` (the canonical envelope FR-NRM-01 asks for), `MessageMedia` (FR-CAP-12's original-vs-
  derived split — `source_uri`/`storage_key`/`thumbnail_key`/`ocr_text`/`exif`/`transcript*`),
  `ChannelHealthEvent` (item 9's durable history) and `PartyOrganisationMapping` (item 5) all landed
  in this one migration, since item 1's own schema is what every later item builds on.
  `Evidence.message_id` finally got its real FK (`ix_evidence_message_id`,
  `evidence_message_id_fkey`) — resolved by `app/capture/pipeline.py`'s `_finalise_message_evidence`,
  not by `app/ledger/extractor.py` itself (left untouched, per CLAUDE.md/Prompt 11b). `channel_id`
  gained a `source_uri`/`fetch_media` addition mid-session — see item 6's own bullet for why item 2's
  `ChannelAdapter` Protocol needed a genuinely new method.
- **Item 4 — Identity resolver** (`app/capture/identity.py`): real confidence ladder — exact
  `channel_identities` match (1.0) > case-insensitive display-name match to an existing Party (0.6,
  deliberately low, FR-LED-07-style "needs a human look") > brand-new Party (1.0, unambiguous first
  sighting). `set_manual_identity_override` + `/admin/channel-identities` (`app/api/identities.py`)
  close FR-NRM-03's "manual override" half. Deliberately does **not** touch
  `app/ledger/extractor.py`'s `_get_or_create_party` — that exact-string path stays the FixtureAdapter/
  cue-eval path unchanged, per Prompt 11's own instruction; the two converge on the same Party row via
  matching `display_name`, not by one calling the other (see `app/capture/extraction_bridge.py`).
- **Item 5 — Party-org effective dating** (`app/parties/organisation_mapping.py` + the
  `party_organisation_mappings` table from item 1's migration): open-ended-range effective-dating
  (`effective_to IS NULL` = current), enforced at the service layer (closes the prior mapping before
  opening a new one), person/vendor_org type-checked in code. `/parties/{id}/organisation` (GET
  current+history, POST to set) added to `app/api/parties.py`, admin-gated.
- **Item 8 — Consent** (`app/capture/consent.py`): `post_consent_notice` calls
  `ChannelAdapter.send()` with a real bilingual (EN/ZH) notice and upserts `ConsentRecord`;
  `is_opted_out` is the normaliser's own gate. `app/api/consent.py`'s `consent_action_request` was
  refactored to share the same `upsert_consent_record` rather than keeping a second copy.
- **Item 3 — Ingestion queue** (`app/capture/normalise.py`, `pipeline.py`, `worker.py`): arq (already
  in this codebase, M4) reused, not a second queue. `detect_language` is a real Unicode-script-ratio
  heuristic (Han vs. Latin-word density), not a stub — it's the one approach that actually answers
  FR-NRM-02's "is this code-switched" question, which a single-best-guess language-ID library
  structurally can't. Per-channel ordering is `_job_id`-based mutual exclusion
  (`enqueue_channel_ingestion`'s `f"ingest-channel-{channel_id}"`), not a per-channel queue arq doesn't
  have — documented in `worker.py`'s own module docstring, including why. At-least-once safety is
  FR-CAP-11's `payload_hash` dedup (`messages_project_payload_hash_key`) plus
  `extraction_attempted_at`/`storage_key` idempotency guards at every downstream step. Real captured
  messages feed the *same*, untouched `extract_case` fixture cases always used
  (`app/capture/extraction_bridge.py` builds the `ProjectContext`/`FixtureCase` shapes from a real
  `Project`/`Message` instead of `cases.json`).
- **Item 6 — Media pipeline** (`app/capture/media.py`, `media_pipeline.py`): reuses
  `app/documents/storage.py`'s `StorageBackend` (real MinIO), never a second abstraction.
  **Real, working, tested**: EXIF (Pillow, already a transitive dep — datetime + GPS decimal-degree
  math verified in tests), thumbnails (Pillow), OCR via **Tesseract** (`pytesseract`, a genuine FOSS
  substitute — the system `tesseract` binary was already present in this sandbox), PDF text
  (`pdftotext`, poppler, also already present), DOCX/PPTX/XLSX text (`python-docx`/`openpyxl`, both
  added; `python-pptx` was already a dependency). **CUE-Tech-Stack.md §2.4's actual named production
  choice is PaddleOCR/Docling, not these** — `PaddleOCRClient` exists in `app/capture/media.py`,
  lazy-imports `paddleocr`, and is **dependency-blocked in this sandbox** (paddlepaddle's full
  install was judged too heavy for this session's time budget) — same "code-complete,
  X-blocked" honesty this milestone has had since item 2, applied to a library instead of a
  credential. `get_default_ocr_client()` prefers Paddle if it's ever installed, falls back to
  Tesseract otherwise. **Every adapter gained a real `fetch_media(channel, uri) -> bytes` method**
  (item 2's `ChannelAdapter` Protocol didn't anticipate needing one) — real and live-tested for
  Mattermost (`GET /files/{id}`), Nextcloud (`NextcloudWebDavClient.get_by_href` — a real href-vs-
  relative-path bug was caught and fixed by the live test, not by inspection), and IMAP/SMTP (a
  genuinely new capability: attachment extraction + `fetch_media` re-locates by `Message-ID`, since
  IMAP sequence numbers aren't stable across connections); WeChat Work's raises `NotImplementedError`
  (media is E2E-encrypted the same as its text, same wall `_decrypt_archive_chunk` already hit);
  Graph's works for `sharepoint` only (`teams`/`outlook` attachment extraction is a documented,
  bounded gap — that whole adapter is credential-blocked/untested regardless). `documents/service.py`
  now **really** derives `DocumentVersion.extracted_text` when a caller doesn't supply it
  (`_derive_extracted_text`), closing the Documents session's own "OCR/parsing not yet wired"
  limitation — verified via a real PDF upload through the actual `/documents` endpoint.
- **Item 7 — ASRClient Protocol** (`app/capture/asr.py`): `FasterWhisperClient` is **genuinely
  installed and tested** — real speech (macOS `say` + `afconvert`, both system tools, no new
  dependency) transcribed by a real `faster-whisper` model (`tiny`, chosen deliberately small for a
  fast dev/CI download; a real deployment should size this against FR-VOI-06 measurements once there's
  a corpus). Per-utterance confidence (FR-VOI-04) via `exp(avg_logprob)`. `SenseVoiceClient`
  (CUE-Tech-Stack.md's actual named Chinese choice) is **dependency-blocked** the same way
  PaddleOCR is — `funasr` pulls in the full PyTorch stack, judged too heavy for this session;
  `get_default_asr_client` falls back to FasterWhisper for Chinese/code-switched hints too.
  Wired into `media_pipeline.py`'s `voice_note` branch; a pure-voice message's transcript is copied
  onto `Message.text` so extraction has something to read (verified end to end: real synthesized
  speech → real ASR → real scripted-LLM extraction → real `Commitment` + `Evidence`, with
  `Evidence.media_ref` a real signed MinIO URL and `Evidence.transcript_confidence` populated —
  FR-VOI-05).
- **Item 9 — Capture health** (`app/capture/health.py`): every adapter's `health()` now actually gets
  called, on a 15-minute arq cron (`run_capture_health_sweep`) — FR-CAP-09's own SLA number, not this
  file's usual "arbitrary starting point" disclaimer. Writes directly to `Channel.healthy` /
  `ChannelHealthEvent` rather than making an authenticated HTTP self-call to the existing
  `POST /channels/{id}/health` — that endpoint's own docstring already named the reason (no
  service-account/agent identity exists yet). A healthy→unhealthy transition logs at ERROR
  (`CAPTURE HEALTH DEGRADED`); routed as a direct log, not a Foresight `Notification`, since
  `Notification`'s own CHECK constraint requires a risk/deviation/commitment subject a channel-health
  event structurally isn't — Prompt 11b's own text names "a direct log/alert otherwise" as the
  accepted fallback. `GET /channels/{id}/health/history` closes the Governance session's own noted gap
  ("no consumer of channel health history exists yet").
- **Item 10 — Gap detection/backfill** (`app/capture/reconciliation.py`): gap = a channel's own
  recent cadence (median of its last 10 messages' inter-arrival gaps) exceeded by 3x, floored at 1
  hour — the same per-entity-baseline idiom Silence Radar already established, applied at the
  channel-transport layer. Backfill is a plain call into `pipeline.py`'s `ingest_channel_backlog`
  (item 3's own function) with `since` rewound by one baseline gap — "each adapter's `fetch_backlog()`
  is the backfill path" is Prompt 11's own instruction, not a separate recovery mechanism. Runs every
  30 minutes (deliberately less frequent than the health check — a real `fetch_backlog()` call is
  heavier than a health ping).
- **Item 11 — Scheduled extraction windows** (Should; `ChannelExtractionSchedule`, migration
  `c9f2a6e5d1b7`, `app/capture/schedule.py`): mirrors `app/reports/models.py`'s
  `ReportScheduleConfig`/`schedule.py` shape closely (interval-minutes instead of day/hour-of-week).
  Deliberately minimal per Prompt 11's own "don't over-invest" instruction — schema + the arq reader
  only, no admin CRUD API in this pass (same "mechanism exists, not a v1 feature commitment" posture
  `MilestoneArchetype.organisation_id` already has elsewhere).
- **Item 12 — FR-DOC-09** (`app/documents/drift.py`): best-effort filename resolution
  (case-insensitive exact match — deliberately not fuzzy, a wrong match would be worse than no check)
  from a document-kind `MessageMedia` to a `Document`, then a real SHA-256 comparison against the
  approved version's stored bytes. A mismatch raises a real `Risk` (`source="contradiction"` — the
  existing enum value, not a new one added for this one caller) through the *same*
  `create_or_supersede_risk` → `dispatch_event` → `draft_deviation` chain
  `app/foresight/contradiction.py` already established, landing a real, FR-FOR-10-deduplicated
  `Deviation` (`class_code="spec_drift"`). Wired into `media_pipeline.py`'s `document` branch, so it
  runs the moment a circulated file's bytes are safely stored.
- **New dependencies added this session** (all lightweight — no torch, no paddlepaddle):
  `pytesseract`, `python-docx`, `openpyxl`, `faster-whisper` (pulls in `ctranslate2`/`av`/`onnxruntime`,
  all precompiled wheels, no build step). `PIL`/Pillow was already present transitively.
- **Two real, live infrastructure bugs this session's own tests caught, not inspection**:
  `NextcloudWebDavClient.get(remote_path)` double-prefixed `_dav_root` when given a PROPFIND `href`
  (already-absolute) instead of a caller-relative path — fixed by adding `get_by_href`, kept `get`
  unchanged for the write-back adapter's own relative-path calls. `ImapSmtpAdapter` never populated
  `RawCapturedMessage.media` at all before this session (no attachment extraction existed) — real gap,
  now closed with a live round-trip test against a real sent email with a real attachment.
- **Every genuinely credential-blocked adapter stayed credential-blocked** — WhatsApp, WeChat Work and
  Graph's `teams`/`outlook` are unchanged from item 2's own honest state. Graph's `sharepoint` gained a
  real `fetch_media` implementation (code-complete, same credential-blocked status as the rest of that
  adapter — genuinely untestable in this environment, not run against anything live).

### M9 notes for later sessions

Closed FR-WBK-01 through 08 in full. Builds entirely on M8's `ChannelAdapter.send()` — no new
channel-level capability, exactly as Prompt 12 scoped it. FR-ASK-06's conversational-action wiring
(chase a vendor via a natural-language Ask command) is the one named follow-up left undone, per
Prompt 12's own EXPLICITLY OUT OF SCOPE section.

- **New module `app/writeback/`** — models, schema, language, compose, rate_limit, audit, service,
  reply — same per-domain-module-owns-its-models precedent every prior domain module established.
- **`OutboundMessage` is always tied to the specific commitment decision it confirms**
  (`commitment_id` NOT NULL) — both because that is genuinely what FR-WBK-01 asks for (PRD §5.2's
  sequence diagram: "PM confirms decision -> write-back") and because `app/ledger/audit.py`'s
  `record_audit_event`, which FR-WBK-08 requires every send to go through, itself requires a
  non-NULL `commitment_id` (`app/models/audit.py`'s `AuditLog` docstring — confirmed before
  designing around it, not discovered the hard way). `channel_id`/`to_external_id`/`language` are
  all resolved once at draft time from the commitment's own real-capture Evidence (`Evidence.message_id
  -> Message.channel_id` — item 1 of M8's own migration is what finally made that FK real) and frozen
  on the row thereafter, never re-derived at send time.
- **Draft/authorise/send are three structurally separate service calls, not one endpoint with a
  flag** (`app/writeback/service.py`'s `draft_writeback`/`authorise_writeback`/`send_writeback`),
  mirroring FR-LED-08's three-tap verification precedent per Prompt 12's own instruction. A DB CHECK
  constraint (`outbound_message_status_field_consistency`) enforces the same invariant a second time
  at the row level — `authorised_by`/`authorised_at`/`sent_at` can only be non-NULL in the status
  each implies, so a direct UPDATE bypassing the service layer can't desynchronise them either.
- **FR-WBK-04's rate ceiling is enforced by locking the `Channel` row itself**
  (`SELECT ... FOR UPDATE`, `app/writeback/rate_limit.py`'s `reserve_send_slot`), not any
  `OutboundMessage` row — there may be zero, one or several already-authorised-but-unsent rows for a
  channel at once, none of which is a valid lock target on its own. The second of two concurrent
  `send` calls for the same channel blocks on the lock until the first commits or rolls back, then
  re-counts and correctly sees the true state — a real transactional check-then-insert, proven with
  two genuinely independent `AsyncSession`s racing via `asyncio.gather`
  (`tests/test_writeback_rate_limit.py::test_concurrent_sends_only_one_succeeds_once_ceiling_is_hit`),
  not simulated. The ceiling itself is `Project.writeback_daily_ceiling` (new column, default 1),
  changed only via `PATCH /projects/{id}/writeback/config` (`ADMIN_ROLES`-gated) and logged to this
  domain's own new `WritebackAuditLog` — a ceiling change has no commitment to hang the shared,
  commitment-scoped `AuditLog` off of, so it gets the same narrow, append-only, per-domain audit
  table `ForesightAuditLog`/`DocumentAuditLog`/`TwinAuditLog` already established, rather than
  widening the shared one for one non-commitment event.
- **FR-WBK-02's language resolution reuses `app/capture/normalise.py`'s `detect_language` outright**
  (`app/writeback/language.py`'s `resolve_channel_language`) — no second language-detection
  mechanism. "The group's prevailing traffic" is read as the last 20 messages on that channel, any
  author, concatenated and run through the same script-ratio heuristic capture already uses; falls
  back to the commitment's own founding `Evidence.language` only when the channel has no real-capture
  message history at all (a channel captured before M8, or in a sandbox with no traffic yet).
- **FR-WBK-03's composition is schema-enforced, not prompt-requested** — CLAUDE.md's "enforce, don't
  ask" discipline, applied here the same way as extraction: `COMPOSE_DRAFT_JSON_SCHEMA` constrains
  the model's output shape, and `app/writeback/compose.py`'s `compose_draft` additionally verifies in
  code (never trusted) that the returned text actually ends in a question mark (ASCII or full-width),
  raising `ComposeError` rather than silently drafting non-question prose. `get_client("reasoning")`,
  not `"extraction"` — composing a confirmation question is a generation/judgment task over a
  commitment's current fields, the same role distinction `app/foresight/contradiction.py` already
  draws for a comparable "not extraction" call.
- **FR-WBK-06/07's reply handling rides `app/capture/pipeline.py`'s existing ingestion pipeline**
  (`app/writeback/reply.py`'s `handle_potential_reply`, called from `ingest_raw_message` right after
  a new `Message` is durably captured, before extraction runs) — no second inbound path. A reply is
  matched to the most recent `sent`, not-yet-replied-to `OutboundMessage` on that channel sent before
  the inbound message arrived; FR-WBK-04's own "at most one message per group per day" means there is
  normally at most one candidate, so this is a defensive tie-break, not load-bearing logic. A
  parseable reply that resolves to a valid transition goes through the *exact* write path a manual
  transition uses (`validate_transition`, `record_audit_event` with a `"vendor reply"` detail,
  `recompute_on_commitment_transition`, `recompute_vendor_metrics` — `actor_id=None`, same
  system-triggered convention `app/ledger/extractor.py` and `apply_automatic_transition` already
  establish). An unparseable reply, or one that would imply an invalid transition
  (`app/ledger/lifecycle.py`'s `validate_transition` raising `InvalidTransition`), is never forced
  through — both escalate identically, via `app/foresight/notification.py`'s existing
  `default_recipients`/`create_notification` (Foresight, M4, is Done, so the real path was used, not
  the minimal-Notification fallback Prompt 12 named for the case it hadn't run yet).
- **Reply parsing's `to_state` is deliberately unconstrained in the LLM schema** (a free string, not
  `CommitmentState`'s own enum) — a model that doesn't already know the commitment's current state
  can't reliably pick from that state machine's full vocabulary anyway, and
  `app/ledger/lifecycle.py`'s `validate_transition` is what actually decides validity afterward;
  constraining the schema more tightly would just move the same failure mode one layer earlier
  without removing it.
- **`AuditAction` gained a sixth value, `outbound_sent`** (migration `9197d521030d`, same
  `ALTER TYPE ... ADD VALUE` pattern `d7def2e27c7c`/`9ddb100d7e8e` already established, with the
  matching Python-side `Enum` list update in `app/models/audit.py` — same "keep the Python list in
  sync even though the real ALTER happens via raw SQL" pattern those two migrations' own notes
  document) — FR-WBK-08 needed its own action value rather than overloading `"state_transition"`,
  since a send and a reply-driven transition are two distinct events that can both happen against the
  same commitment.
- Full coverage per Prompt 12's own testing expectation: rate-ceiling enforcement including the real
  concurrent-race case above, `send`-without-a-prior-`authorise` as an explicit negative test
  (`tests/test_writeback_service.py::test_send_requires_prior_authorise`), reply-parses-to-transition
  vs. reply-fails-and-escalates as two clearly separate cases plus a third for the
  invalid-transition-implied variant (`tests/test_writeback_reply.py`), RLS and role-gating as two
  independent properties (`tests/test_writeback_api.py`, same `..._are_isolated_via_project_join_rls`
  / `test_read_only_member_can_...` shape `tests/test_risks_api.py` already established), and a full
  draft -> authorise -> send -> history cycle exercised through the real ASGI app with the LLM call
  monkeypatched at the same seam `app/writeback/compose.py` calls (`tests/test_ask_api.py`'s own
  "fakes injected below the HTTP layer" idiom) — 464 passing at the end of this session (447 at the
  start), zero mocks of the database/RLS/lock behaviour itself.

### M10 notes for later sessions

Prompt 13's own premise for item 1 (CI runner-availability failure) was stale — live `gh api`/
`gh run list` evidence showed Actions enabled and check-runs already producing on every push, with a
full healthy history back to 2026-08-06. The real, current, reproducible failure was two root causes:
`pytest.yml` never installed `tesseract-ocr`/`poppler-utils` on the runner (4 of 6 failures cascaded
from that), and one consent test constructed `NextcloudAdapter()` from ambient env/`.env`, which CI
never provides. Fixed both directly rather than chasing the stale "runner not acquired" framing;
confirmed via a real pushed commit (`5957f56`) and `gh api .../check-runs` showing
`conclusion: success` — not a local simulation.

- **Cost accounting (NFR-OBS-03) is a lightweight internal table (`llm_usage_events`), a deliberate
  substitute for Langfuse** — the tuned choice this session's budget stretched to, per the prompt's own
  explicit permission when standing up Langfuse is too heavy. `ModelClient.complete` now returns
  `tuple[str, LLMUsage]` instead of bare `str` (a real, once-only protocol change touching all 7
  production call sites and every test's fake client — `OllamaClient`/`AnthropicClient` were
  discarding real usage data already present in the raw API response, not a "no-op when
  unconfigured" case). `app/llm/cost.py`'s `record_llm_usage` is best-effort inside a SAVEPOINT
  (`session.begin_nested()`) — a recording failure rolls back only the usage row, never the caller's
  own already-good work in the same transaction (covered directly by
  `tests/test_llm_cost.py::test_record_llm_usage_failure_is_swallowed_and_does_not_poison_the_transaction`).
  Swap-out to real Langfuse: replace that function's body with a `langfuse.generation(...)` call,
  gated the same no-op-when-unconfigured way as `OTEL_EXPORTER_OTLP_ENDPOINT` below.
- **Tracing/metrics (NFR-OBS-01/02) — `app/observability/otel.py`**, genuinely no-op (no SDK
  provider installed, no instrumentation library patches anything — not even httpx globally) when
  `OTEL_EXPORTER_OTLP_ENDPOINT` is unset, confirmed by the full suite passing with it unset
  throughout. Verified end-to-end against a real local collector (`docker compose --profile
  observability up -d otel` — `grafana/otel-lgtm`, a single all-in-one image, not the multi-container
  SigNoz stack CUE-Tech-Stack.md §2.6 names as the actual production default; chosen here specifically
  because standing it up *and verifying it end-to-end* mattered more this session than matching that
  doc's own preference — see `docker-compose.yml`'s own comment on the `otel` service for the
  reasoning; a real deployment should still default to SigNoz): emitted a real span, queried Tempo's
  API inside the container, found it. `app/capture/health.py`'s `check_channel_health` now emits a
  gauge (`cue.capture.channel_health`) on *every* check, not just healthy→unhealthy transitions (the
  ERROR log FR-CAP-09 already had since M8) — an uptime percentage needs the full 1/0 series.
- **Drift detection (NFR-OBS-05 + FR-VOI-06) — `app/observability/drift.py`**, two arq cron jobs
  (`run_extraction_drift_check` daily, `run_asr_drift_check` monthly per the prompt's own explicit
  number for that capability) registered on the one existing worker process
  (`app/foresight/worker.py`), not a second one. Both run the relevant `cue-eval/` harness as a
  subprocess (`run_eval.py --json` / new `asr_eval.py --json`, a small additive `JSON_SUMMARY:` line
  on the former) and, on regression, raise a Risk through a genuinely new `RiskSource` value,
  `'model_drift'` (migration `0a76bb463d69`) — deliberately not reusing `app/documents/drift.py`'s
  unrelated `source="contradiction"` "drift" (a circulated file differing from its approved
  `DocumentVersion` by hash). A model-accuracy regression is platform-wide, but this codebase has no
  platform-level Risk/Notification concept anywhere — every existing detector is project-scoped — so
  `_notify_all_projects` raises/supersedes a Risk per active project rather than inventing a one-off
  exception; `create_or_supersede_risk`'s own dedup means a sustained regression doesn't re-notify
  every single tick.
  - `run_extraction_drift_check` derives provider/model from `get_llm_settings()` at call time, not
    hardcoded to Anthropic — today that's `ollama`/`qwen2.5:14b` (the default), so it runs at zero
    Anthropic cost until production config actually switches at go-live, honoring this project's own
    zero-Anthropic-spend-until-go-live posture, and starts covering the real production model
    automatically the day that config flips.
  - `run_asr_drift_check` needed a held-out labelled audio set that didn't exist — built a small one
    (`cue-eval/asr_cases.json`, 6 domain-realistic sentences, macOS `say`-synthesized) and a sibling
    harness (`cue-eval/asr_eval.py`) measuring `FasterWhisperClient` (the only real, installed
    `ASRClient`) via edit-distance WER/CER. Explicitly a small synthetic corpus, not real Pico vendor
    audio — same honesty posture as every other "genuine FOSS substitute, not a claim of equivalence"
    decision this project has made before. First real run: English WER 6.7% (inside PRD §8.1's ≤8%
    target); Chinese CER 45.4%, but a *named, understood* confound, not "Chinese ASR is broken" —
    `FasterWhisperClient`'s `tiny` model outputs Traditional Chinese characters even for zh_CN speech,
    so a per-character diff against the corpus's Simplified reference counts every script-variant
    character as an error; a fair CER needs Simplified/Traditional normalization (e.g. OpenCC) before
    comparing, deliberately not added for a 6-case harness. Flagged in `asr_eval.py`'s own output, not
    silently absorbed into "Chinese ASR fails."
- **Secrets (NFR-SEC-03) — `docs/secrets-openbao-migration.md`**, plus a real (if dev-mode) OpenBao
  container behind the same `profiles: [observability]` opt-in as `otel`. Verified end-to-end this
  session: wrote a secret to the documented KV path, applied the documented policy
  (`docs/openbao/cue-backend-policy.hcl`), read it back inside the container. `.env.example` was
  badly out of date (9 documented vars vs. ~50 actually in use) — refreshed to match the real surface,
  categorized by whether OpenBao migration is actually warranted (real secret vs. connection config vs.
  behavioural switch — most of `.env`'s vars are the latter two, and moving them into a vault would be
  security theatre, not hardening).
- **Load testing (NFR-PRF) — `backend/loadtest/`**, k6 (not locust — no new Python dependency, and
  `thresholds` turns NFR-PRF-02 into a pass/fail assertion the run itself checks). `loadtest/seed.py`
  provisions an org/project/parties directly via the ORM (no public org-creation REST endpoint exists
  — a real deployment provisions tenants out of band). Ran for real against a live `uvicorn` process:
  100% checks passed, p95 create/verify/transition all under 25ms at 3 VUs/10s against fixture-scale
  data. Per the prompt's own explicit instruction: this validates NFR-PRF-02 (Twin recomputation
  ≤10s, since `/transitions` does that recompute synchronously in-request) and "the pipeline doesn't
  fall over" — it does **not** validate NFR-PRF-01 (capture-to-ledger latency), which needs real
  message volume M8's credential-blocked channels can't yet supply. `loadtest/README.md` says this
  explicitly; don't let a future session cite this harness as NFR-PRF-01 proof.
- **Dependabot (NFR-SEC-05)** — `.github/dependabot.yml`, `uv` + `github-actions` ecosystems, weekly.
  The other half of NFR-SEC-05 (an annual penetration test) needs an external firm, not code —
  unchanged, out of scope here as the prompt itself names.
- **A real, pre-existing test-suite flake found and fixed along the way, not introduced by this
  session**: `tests/conftest.py`'s `app_session` fixture now resets `app.current_org_id` on
  acquisition. Root cause, confirmed two ways — (1) reproduced on the pre-M10 codebase via `git
  stash`, using a file pair with zero relation to this session's own work
  (`test_writeback_rate_limit.py` + `test_audit_log.py`, order-dependent, not present in the natural
  alphabetical full-suite run); (2) bisected this session's own new drift tests down to the exact
  mechanism (a test leaving its own `app_session` transaction open while a job function under test
  opens additional `async_session_factory()` sessions internally — `run_capture_health_sweep`'s own
  existing tests happen to commit `app_session` before calling the sweep, which is what avoided this
  all along, not robustness). The suite was never actually protected against this by design — only by
  alphabetical ordering luck, which any new test file (this session's, or a future one) can perturb.
  The fixture-level reset fixes the general class; `tests/test_observability_drift.py`'s own tests
  additionally follow the same "commit before calling into a job that opens its own sessions" pattern
  `test_capture_health.py` already established, now made explicit rather than accidental.
- Full coverage: `uv run pytest` green (480 passing, up from 464 at the start of this session — 16 new
  tests: cost accounting, both drift jobs' regression/no-regression/infra-failure/dedup paths, OTel
  no-op posture, the capture-health gauge), confirmed deterministic across repeated runs, before and
  after every instrumentation change — proving the no-op posture holds, not just asserting it does.

**Overall CUE-PRD.md backend implementation state, end of M10 (the last milestone in this plan):**
M1–M10 all Done. Every PRD capability area (§4 Ledger, §5 Twin, §6 Foresight/Documents/Ask/Reports/
Write-back/Governance, §7 NFRs to the extent this backend-only, no-live-deployment session could
reach them) has real, tested code behind it — no capability is a stub or a fabricated-looking demo
path. What remains genuinely open, by design, not oversight:
- **Credential-blocked, code-complete**: WhatsApp, WeChat Work, Graph (`teams`/`outlook`) — real Pico
  infrastructure access was never available in this environment; live testing against it is the
  natural first task once it is.
- **Dependency-blocked, real FOSS substitutes running instead**: PaddleOCR (Tesseract runs), FunASR
  SenseVoice (FasterWhisper runs, with the Chinese-transcription caveat this session's own ASR drift
  check just measured and named).
- **Structurally deferred, named with their own reasons in the milestone notes above**: FR-LED-05
  (supersession linking — `compute_revision_churn`/`compute_price_drift` report `value=None` until it
  lands), FR-TWN-08 (learned duration distributions), FR-FOR-05 (photo verification), full
  push/email/Teams `Notification` delivery (webhook is the only real adapter), FR-ASK-06's
  conversational-action execution.
- **Deployment/infrastructure, not backend code** — named explicitly rather than silently skipped,
  per this milestone's own EXPLICITLY OUT OF SCOPE section: multi-region data planes, confirmed
  production volume + headroom (SCL-01, blocked on a real number from Pico, §16 open question 1),
  independent horizontal scaling, 99.5%/99.9% measured availability, RPO/RTO, TLS/AES-256/per-tenant
  keys, signed media URLs, capture-agent credential isolation, and an actual penetration test.

### Frontend-enablement additions (post-M10, 2026-08-09)

Not a new milestone — M1–M10 above is still the complete PRD build plan, and this table's own
row structure isn't extended for it. These are five small, scoped backend additions made while
starting `frontend/PROGRESS.md`'s own plan (a parallel, frontend-only milestone table — see that
file), each one a gap the frontend audit found and closed on the spot rather than leaving as a
surprise for whichever `Prompt FN` session hit it first, per this project's own established
"close the gap you find, document it" pattern (the same posture Documents' Prompt 6 session used
for its early SharePoint write-back, or M8's `channel_types` reference-table pivot).

- **`POST /auth/dev-login`** (`app/api/auth.py`) — there was no way to authenticate an HTTP
  request against this API at all before this addition; `mint_local_token`
  (`app/identity/tokens.py`) existed only as a function `tests/conftest.py` called directly. This
  endpoint is that same function, reachable over HTTP, gated hard on
  `CUE_AUTH_PROVIDER=local` (404s otherwise) — it mints a token for any `organisation_id`/`email`
  a caller names, no credential check of any kind, so the guard is the only thing standing between
  this and a real tenant-isolation failure if it were ever reachable against `oidc`. See that
  file's own module docstring for the full reasoning. `tests/test_auth_dev_login.py`.
- **CORS** (`app/core/config.py`'s new `cors_origins`/`cors_origins_list`, wired in `main.py`) —
  no `CORSMiddleware` existed anywhere in this codebase; a browser on `localhost:3000` could not
  call this API from any origin regardless of auth correctness. `CUE_CORS_ORIGINS` (comma-
  separated, default `http://localhost:3000`), `.env.example` updated.
- **`GET /projects/{id}/ontology-terms?category=X`** (`app/api/ontology.py`, backed by a new
  public `list_ontology_terms` in `app/twin/service.py` that wraps the existing private
  `_resolve_ontology_terms` — the same three-tier resolution `get_milestone_type_term` already
  exposed for the single-code case, now exposed for "give me the whole set") — every `*_code`
  field across this API (`MilestoneCreate.type_code`, `CommitmentCreate.act_type`,
  `DeviationCreate.class_code`, `DocumentCreate`/`DocumentTagRequest`'s `class_code`/`phase_code`)
  assumed the caller already knew the valid codes; there was no discovery endpoint for any of
  them. Plain project-membership-gated (same tier as `GET .../milestones`), since this is
  reference data any project member needs to build a form, not an admin action.
  `tests/test_ontology_terms_api.py`, including a real tenant-extension-shadows-platform-term
  case.
- **`GET /parties`** (`app/api/parties.py`'s new `list_router`) — `/parties/{id}/reliability`
  needs a `party_id` the caller already knows; there was no route in this codebase that could tell
  a caller which `party_id`s exist at all. Same `require_org_finance` gate and same explicit
  `organisation_id` filter (`parties` still has no RLS policy of its own — a pre-existing gap,
  unchanged by this addition) as the reliability endpoints it sits beside. Filterable by
  `type`/`city`/`vendor_category` (the last one a direct join on `Party.vendor_category_term_id`,
  not a three-tier resolution — a party's own category was already resolved to one specific row at
  assignment time). `tests/test_parties_list_api.py`, including cross-organisation isolation as
  its own explicit property.
- **`GET /admin/cost-summary`** (`app/api/admin.py`) — `llm_usage_events` (NFR-OBS-03, built in
  M10 as the deliberate Langfuse substitute) has been written to by every extraction/ask/
  contradiction/write-back call since the Hardening session, but nothing ever read it back over
  the API — PRD §13's own "cost per active project" row had no real surface. `require_org_
  administrator`-gated; aggregates by `(project_id, provider, model)`; relies on
  `llm_usage_events`' own `tenant_isolation` RLS policy rather than an explicit filter, since that
  table (unlike `parties`) does have one. `estimated_cost_usd=None` on a row/total means genuinely
  unknown (an unrecognised model), never confused with a real `$0.00` (which is what a self-hosted
  Ollama call honestly reports) — see `CostSummaryRow`'s own docstring in `app/api/schemas.py`.
  `tests/test_admin_api.py`.

All five are real, tested additions, not stubs — `uv run pytest` green afterward: 498 passing (up
from 480 at the end of M10 — 18 new tests: 4 for `/auth/dev-login`, 5 for `/ontology-terms`, 5 for
`GET /parties`, 4 for `/admin/cost-summary`). `frontend/PROGRESS.md`'s `Prompt F0`/`F2`/`F6`/`F8`
originally documented these as gaps
for whichever frontend session reached them to close; those files have been updated to point at
the real endpoints above instead.

### `scripts/seed_dev_data.py` (added during frontend F0, 2026-08-09)

Not a sixth item above — this is the one piece of backend work `Prompt F0 — Frontend Foundations.txt`
itself always expected a frontend session to add (its own "WHAT TO BUILD" #1), not a gap this table's
addendum found. Seeds one organisation, one `event-production-default`-archetype project, and one
`User`+`Membership` per FR-ADM-01 role, for a human (or `frontend/e2e/global-setup.ts`) to paste into
`/login`. Full story, including a real bug it surfaced in how `POST /auth/dev-login`'s
subject-is-always-the-email minting interacts with `resolve_user`'s `(issuer, external_subject)`
lookup, is in `frontend/PROGRESS.md`'s own F0 notes — not duplicated here since this table's own
convention is one paragraph per row, not a running log of every script in the repo.

### Frontend-enablement additions, round 2 (during frontend F1, 2026-08-09)

Same pattern as the five post-M10 additions above — gaps `Prompt F1 — Living WIP, Verification
and Write-back.txt` found while reading the surfaces it builds against, closed on the spot:

- **`EvidenceOut.media_ref`** (`app/api/schemas.py`) — `Evidence.media_ref` (a signed, expiring
  URI) has been populated since M8's capture pipeline set it on voice-note evidence
  (`app/capture/pipeline.py`), but no response schema ever exposed it, so FR-VOI-05 ("retain
  original audio and make it playable from any evidence link") had no API surface at all — F1's
  own evidence viewer (original text + translation toggle + audio playback) had nothing to play.
  Additive field, `str | None`, no migration needed.
- **`GET /projects/{project_id}/members/me`** (`app/api/projects.py`, `EffectiveRoleOut` in
  `app/api/schemas.py`) — F1's payment-status/budget-revise controls are Finance/Producer-only,
  and `app/api/deps.py`'s `require_project_role` docstring names "which actions to *show* as
  available" as a legitimate client-side judgment call (a UX nicety, never a security boundary —
  every mutating endpoint still independently re-derives and enforces its own role gate). Nothing
  before this let a non-admin caller learn their own effective role set on a project at all; this
  wraps `app.identity.service.effective_roles` (already used by `require_project_role` itself)
  behind the same any-membership read-access tier every other project-scoped GET uses. Not a
  security-relevant endpoint on its own — it returns exactly what the caller already implicitly
  proved by authenticating, nothing about any other user. `tests/test_frontend_enablement_f1.py`,
  5 new tests (2 for `media_ref`, 3 for `/members/me` including the non-member-404 case).

- **`PATCH /projects/{project_id}/writeback/{outbound_id}`** (`WritebackDraftUpdate` in
  `app/writeback/schema.py`, `edit_writeback_draft` in `app/writeback/service.py`) — F1's own "WHAT
  TO BUILD" #4 names "shows the composed question for human review/**edit** before authorisation";
  review already had a surface (`GET`), edit didn't — draft/authorise/send had no fourth call for
  it. `draft_text`-only, `WRITE_ROLES`-gated same as draft/authorise/send, and only while `status ==
  "draft"` (409 otherwise, mirroring `authorise_writeback`'s own guard) — once authorised, the text
  a human signed off on is frozen, same reasoning `OutboundMessage`'s docstring gives for freezing
  `to_external_id`/`language`/`channel_id` at draft time. Not logged to `WritebackAuditLog` (would
  need a new native-enum value on `WritebackAuditAction`, i.e. a migration, for what this module's
  own docstring already treats as a non-event — "only a real `send` is logged... per Prompt 12 item
  6"); the row's own `updated_at` is the edit trail. `tests/test_writeback_api.py`'s new
  `test_edit_draft_before_authorise_then_locked_after`.

`uv run pytest` green afterward: 504 passing (up from 498).

**`scripts/seed_dev_data.py` extended** (same file F0 added, not a new script): F1's own TESTING
EXPECTATION explicitly prefers extending this seed over hand-crafting one-off fixtures for "a
commitment already sitting in `pending_verification` to test against." Added: a vendor + internal
`Party`, a `whatsapp` `Channel` + `ChannelIdentity` (write-back's `_resolve_writeback_target` needs
real-capture evidence — a manually-entered commitment can never be drafted against, per
`WritebackTargetUnresolved`'s own docstring), a `Message` + real-capture `Evidence` (bilingual:
Chinese original, English translation, exercising P7's toggle), one `pending_verification` monetary
`Commitment` off that evidence (exercises verify-end-to-end, the export-block 409, and write-back
draft in one fixture), one `human_verified` `Commitment` for section variety, a `Budget` baseline
(so the budget summary and the export gate are both resolvable rather than "no baseline"), and one
`auto_drafted` `Deviation` off the pending commitment (via `app/foresight/deviation.py`'s own
`draft_deviation`, not hand-built) for the risk-and-issues section and F1's deviation-confirm
action. Verified against the live API post-seed: `GET .../report/current` renders all of the above,
`POST .../report/export` 409s with the expected `blocking_commitments` entry, `GET
.../members/me` returns the right role per seeded user.

### Frontend-enablement additions, round 3 (during frontend F3, 2026-08-10)

Same pattern as rounds 1–2 above — two gaps `Prompt F3 — Foresight, Risks and Deviations.txt`
found while wiring the Foresight surface against, closed on the spot rather than left as a second
documented workaround:

- **`OntologyTermOut.id`** (`app/api/schemas.py`) — the response was deliberately keyed by `code`
  alone (every `*_code` write field takes the stable code, never the internal id), but that
  reasoning only covers *picking* a term to write, not resolving an already-persisted `*_term_id`
  FK (`DeviationOut.class_term_id`, `MilestoneOut.type_term_id`) back to a label. F2 hit the
  identical gap for `type_term_id` first and worked around it by never displaying a milestone's
  type at all (`frontend/PROGRESS.md`'s own F2 notes); F3 hit it again for `class_term_id` and
  closed it properly instead of adding a second, independent workaround. Purely additive field
  (`uuid.UUID`, required) — no existing caller reads a fixed key set or breaks on a new one; the
  stale "deliberately omitted" docstring is rewritten to explain both needs. `app/api/ontology.py`
  now passes `id=t.id` through; `sort_order` (already a required field, easy to miss reading the
  response construction inline) is preserved unchanged.
- **`GET /projects/{project_id}/members`** (`app/api/projects.py`, `ProjectMemberOut` in
  `app/api/schemas.py`) — `DeviationResolveRequest.resolution_owner` (FR-DEV-03) is a required user
  id, but nothing let an ordinary write-role member (the same tier that's actually allowed to call
  `POST .../resolve`) look up a colleague's id by name. Only `GET /admin/roles` came close, and
  that's org-admin-gated *and* carries no display_name/email — `MembershipOut` (raw `user_id`) and
  `UserOut` (org-admin-only) both existed, but nothing joined them for a project-scoped, any-
  member-readable read. `ProjectMemberOut` is that join (`Membership.user_id`/`role` × `User.
  display_name`/`email`), one explicit query (no `relationship()` between the two tables, same "one
  explicit select" style `app/api/deviations.py`'s `_to_out` already establishes), same any-
  membership read tier `/members/me` uses. Not a new security boundary — every project member can
  already infer their own project's roster from other project-scoped reads; this only adds a
  name/email response shape for it.

`tests/test_frontend_enablement_f3.py`, 4 new tests: `OntologyTermOut.id` matches the real row
(queried directly, not just "some UUID present"); `GET .../members` returns every member with
correct name/email/role (covering both a null and a real `display_name`); the same non-member-404
gate `/members/me` already established; and a cross-project isolation check (`memberships` has no
`organisation_id` column of its own — this confirms the endpoint's own `WHERE project_id = ...`
clause, independent of RLS, never leaks a second project's members). `uv run pytest` green
afterward: 508 passing (up from 504).

### Frontend-enablement addition, round 4 (during frontend F4, 2026-08-10)

One gap `Prompt F4 — Documents.txt` found while wiring the spec-claims view — same "close it on the
spot, document it" pattern as rounds 1–3:

- **`GET /projects/{project_id}/documents/spec-claims/{spec_claim_id}`** (`app/api/documents.py`,
  `SpecClaimResolvedOut` in `app/api/schemas.py`) — `SpecClaim.contradicts` (FR-DOC-08) can point at
  a claim on a *different* document version than the one currently being viewed:
  `app/foresight/contradiction.py`'s own detector compares claims project-wide by shared
  `deliverable_id`/`location_code`, never restricted to a single version. `GET
  .../versions/{id}/spec-claims` only ever returns claims for one version, so a `contradicts` target
  outside that list was otherwise an unresolvable UUID with no document to reach — the same "id
  surfaced with no paired resolver" gap shape as round 3's `OntologyTermOut.id`, just for a
  different field. `SpecClaimResolvedOut` extends `SpecClaimOut` with `document_id`/`document_name`/
  `document_version_no` — enough to render "conflicts with X at Y, from document Z" and link out to
  it — without touching `SpecClaim`'s own field set (CUE-PRD.md §4.3's schema, already fixed).
  Registered ahead of the router's existing `/{document_id}` route (same `/search`-before-
  `/{document_id}` precedent already in this file) so `"spec-claims"` is never mistaken for a
  `document_id` path segment.

`tests/test_frontend_enablement_f4.py`, 2 new tests: a cross-document `contradicts` pair resolves
to the target claim's real document identity (not just "some UUID present"); a claim requested
under a project it doesn't belong to 404s, the same project-scoping shape every other document
endpoint already has. `uv run pytest` green afterward: 510 passing (up from 508).

**`scripts/seed_dev_data.py` extended again**, same file, same "extend the seed over hand-crafting a
one-off fixture" preference F1's own TESTING EXPECTATION set. F4's own TESTING EXPECTATION needed "a
document version seeded with at least one `contradicts` pair" — `app/foresight/contradiction.py`'s
real detector only ever fires from the arq worker's periodic sweep on real elapsed time, the same
"not something Playwright can wait on" gap F3's own `risk_silence`/`risk_forecast` ORM-direct
fixtures already work around, so this follows suit: two `Document`+`DocumentVersion`+`SpecClaim`
rows (`quotation.pdf`/`shop-drawing.pdf`, both location `H`, dimension `2040mm x 1040mm` vs. `2000mm
x 1040mm`), `contradicts` wired directly rather than waiting on a sweep. `storage_ref` points at no
real MinIO object on purpose — this fixture pair only backs the spec-claims contradiction view;
`e2e/documents.spec.ts`'s own upload/version/approve test uploads a fresh document through the real
UI instead, exercising the real `StorageBackend` for that path.

### Frontend-enablement addition, round 5 (during frontend F5, 2026-08-10)

One gap `Prompt F5 — Ask and Successor Brief.txt` found while wiring citation routing — same
"close it on the spot, document it" pattern as rounds 1–4:

- **`GET /projects/{project_id}/documents/versions/{version_id}`** (`app/api/documents.py`, no new
  response schema — `DocumentVersionOut` already carries `document_id`). Ask's `Citation`
  (`app/ask/schema.py`) with `source_type == "document_version"` only ever carries the
  DocumentVersion's own id — confirmed by reading `app/ask/answer.py`'s `_resolve_citation`
  directly, not assumed. Every other version route in this router is nested under
  `/{document_id}/versions/{version_id}` and needs both ids, which the citation doesn't have — the
  same "an id surfaced with no paired way to resolve it" gap shape frontend/CLAUDE.md's own
  gap-audit section names (Class A), just for a different field than round 4's
  `SpecClaimResolvedOut`. Registered ahead of the router's existing `/{document_id}` route (same
  `/search`-before-`/{document_id}` precedent already in this file) so `"versions"` is never
  mistaken for a `document_id` path segment.

`tests/test_frontend_enablement_f5.py`, 3 new tests: a version resolves by its own id alone,
carrying its real parent `document_id`; an unknown version id 404s; a real version id under a
project it doesn't belong to 404s (same project-scoping shape round 4's spec-claim resolver test
asserts). `uv run pytest` green afterward: 513 passing (up from 510).

**One gap found and deliberately left open, not closed** — `audit_log`-typed citations have no
routing target at all, not just no actor name (F5's own prompt had already flagged the actor-name
half as a known, worse-than-Class-A gap: `Citation.label` is always null for this type,
`AuditLog.actor_id` has no resolver anywhere). Reading `_resolve_citation` further while wiring the
frontend surfaced a second half of the same gap: the `Citation` for an `audit_log` hit carries only
the `AuditLog` row's own id — never `commitment_id`, which the row does have (`app/models/audit.py`:
`NOT NULL`) — and no endpoint resolves one to the other, so there is no way to route to the
commitment the log entry is even about. Would need a small resolver in the same shape as the
document_version one above (`GET .../audit-log/{audit_log_id}` returning just enough to route,
project-membership-gated the same way Ask itself is). Not added this session — nothing in F5's own
WHAT TO BUILD specifically depended on it, and its own NON-OBVIOUS note already anticipated leaving
this one as a named gap rather than routed around. Documented in frontend/PROGRESS.md's F5 notes;
worth closing whenever a later session actually needs to open an audit_log citation, not before.

**`scripts/seed_dev_data.py` extended once more**, same file: ends with a real call to
`app/ask/embed_worker.py`'s `run_embedding_sweep()` — the only thing that ever populates
`RetrievalChunk` (Evidence/AuditLog text embeddings) or `DocumentVersion.embedding`, normally an arq
cron tick on real elapsed time, the same "not something Playwright can wait on" gap rounds 3/4's own
fixture work already routes around, applied here to Ask's retrieval index instead of a
foresight/documents fixture. `run_embedding_sweep` is directly callable with no running worker (its
own docstring) so no new machinery was needed, just the one call. Confirmed working end-to-end this
session (373 rows embedded from one seed run) — but only after also pulling `bge-m3` into this
sandbox's local Ollama, which hadn't been pulled yet (`app/ask/config.py`'s `EmbeddingSettings`
default, matching `DocumentVersion.embedding`/`RetrievalChunk.embedding`'s hardcoded `Vector(1024)`
column width; `nomic-embed-text`, already present locally, is 768-dim and isn't a drop-in
substitute). Environment state, not a code change — noted here in case a later fresh sandbox needs
the same one-time pull.

### Architecture correction, post-F5 (2026-08-10) — real Ollama is never used in CI, period

The round above's own framing ("pull bge-m3 into CI," "widen CI timeouts for real-model latency")
was wrong in a way worth recording plainly, not quietly editing away. This project's own dev
strategy (CLAUDE.md's Models table) scopes real Ollama models to the developer's own local
machine only — dev, test, demo — switching to a frontier model (Anthropic) for production once
the app is solid, specifically to control cost. A GitHub Actions CI runner is neither of those
places. Installing and running real Ollama models in `frontend/.github/workflows/ci.yml`'s `e2e`
job (first for F1's write-back spec, before this session; extended much further by this session
for F5) was a boundary violation regardless of how the timeout budget was tuned — chasing GPU
runners or bigger timeouts to make that architecture fast enough would have papered over the
actual mistake, not fixed it.

**The real fix: `FakeClient` (`app/llm/client.py`) and `FakeEmbeddingClient`
(`app/ask/embeddings.py`)** — a third provider option (`CUE_LLM_EXTRACTION_PROVIDER=fake`,
`CUE_LLM_REASONING_PROVIDER=fake`, `CUE_EMBED_PROVIDER=fake`) alongside the existing
`ollama`/`anthropic`/`tei` choices, wired into `get_client()`/`get_embedding_client()` the same
env-driven, provider-not-model way those already switch. CI now never talks to a model at all.
`FakeClient` is schema-aware (branches on `schema["required"]`, since a real end-to-end run makes
many different real calls in one session, not one fixed canned response the way each pytest
file's own local `FakeModelClient` gets away with): a keyword check against the intent-
classification schema's own trailing `Message: ` field (never the prompt's static example text,
which names "chase the vendor" as an example and would otherwise always match); an honest word-
overlap judgment between the question and each retrieved excerpt for Ask's answer-generation
schema (see the note below on why this couldn't be an unconditional "yes"); a fixed valid closed
question for write-back's compose-draft schema; and a type-conformant synthesized stub for
anything else, so an unanticipated schema shape never crashes a test with a validation error.
`FakeEmbeddingClient` returns a deterministic, hash-seeded 1024-dim vector per text (matching
`Vector(1024)`) — reproducible, but explicitly not a claim of real semantic similarity.

**Why the answer-generation fake had to make a real relevance judgment, not just "always
confident, cite everything":** first implementation always returned `has_support: true`, citing
every excerpt id found in the prompt. That broke `no_citable_source` entirely once
`FakeEmbeddingClient` was in play — a fake (or real) embedding-based semantic search always
returns its *closest* vector regardless of true relevance, the same way real cosine-distance
ranking would for a genuinely unrelated question, so `hits` is never actually empty once the
corpus is embedded. "A hit exists" stopped being able to stand in for "the excerpts answer this
question." Fixed with a real (if simple) word-overlap check between the question and each
excerpt's own text — an honest judgment, not a shortcut, matching this codebase's own "no
fabricated placeholder" discipline applied to the fake's own behaviour.

Verified locally before pushing, not assumed: the full `pnpm test:e2e` suite (25 specs, both repos,
real Postgres/MinIO, `CUE_LLM_*_PROVIDER=fake` + `CUE_EMBED_PROVIDER=fake`) passes at CI's own
actual concurrency (`--workers=2`, matching a 2-vCPU runner) in well under a minute, every Ask spec
resolving in under a second — down from 17+ minutes and a real, reproducible failure with Ollama in
the loop. `tests/test_fake_llm_client.py` (11 tests) and an extension to `tests/test_llm_factory.py`
cover both fakes directly; full `uv run pytest` green at 524 passing (up from 513).

Model *quality* — a genuinely different question from "does the wiring work" — still belongs in
`backend/.github/workflows/cue-eval.yml`'s own separate, non-blocking evaluation workflow. Nothing
about that changed; this correction only removed a real model from a place it should never have
been running in the first place.

### Frontend-enablement addition, round 6 (during frontend F6, 2026-08-10)

One gap `Prompt F6 — Vendor Reliability Graph.txt` found while wiring the vendor detail page — same
"close it on the spot, document it" pattern as rounds 1–5:

- **`GET /parties/{party_id}`** (`app/api/parties.py`'s `list_router`, no new response schema —
  `PartyOut` already exists for the list operation). Before this, the only way to learn a party's
  own `display_name`/`type`/`city` was `GET /parties` (the whole org-wide directory) — there was no
  single-resource read at all, unlike every other entity this frontend plan touches. A vendor detail
  page needs this party's own fields to render a header, and specifically needs `type` to decide
  whether FR-NRM-04's organisation-mapping section even applies (person-only) — re-fetching and
  linear-scanning the entire directory client-side for one row was the only alternative. Same
  `require_org_finance` gate, same `_get_party` org-scoped 404 shape every other route in this module
  already uses — no new access tier. `tests/test_parties_list_api.py`, 3 new tests (found by id,
  404 for another organisation's party, 403 for a non-finance role).
- **`ProjectOut.archetype_code`** (`app/api/schemas.py`) — `Project.archetype_code` is FR-VRG-02's
  own "event archetype" segmentation axis (the column's own docstring says so verbatim), set once at
  `materialize_archetype` time, but no response schema ever exposed it — confirmed by grep before
  assuming, the same way round 1–5's gaps were each confirmed against the code rather than the
  prompt's own claims. `/parties/{id}/reliability`'s `event_archetype` query param had no way for a
  caller to discover which values are real without this — `Project.archetype_code` is free text, not
  an `ontology_terms` vocabulary, so there's no `GET .../ontology-terms?category=event_archetype`
  equivalent either; `GET /projects` (already org-member-readable, no new gate) is now the only
  discovery path, and frontend/PROGRESS.md's F6 notes name this explicitly rather than fabricate a
  dropdown. Purely additive, no migration (the column already existed) — every existing `ProjectOut`
  caller unaffected, confirmed by the full suite staying green.

`uv run pytest` green afterward: 528 passing (up from 524).

**One pre-existing, unrelated gap re-confirmed while verifying this round, not caused by it:**
running the full frontend `pnpm test:e2e` suite locally against a freshly-restarted backend without
`CUE_LLM_*_PROVIDER=fake`/`CUE_EMBED_PROVIDER=fake` set (this session's own restart, needed to pick
up the two additions above) reproduced exactly the failure mode the "Architecture correction,
post-F5" section above already diagnoses: `e2e/living-wip.spec.ts`'s write-back draft assertion and
most of `e2e/ask.spec.ts` time out against real local Ollama inference, because both spec files'
own comments now assume the fake client (post-F5 cleanup removed their old generous local
timeouts). Setting the three `*_PROVIDER=fake` env vars on the backend process (matching
`.github/workflows/ci.yml` exactly) fixed `living-wip.spec.ts` fully and fixed all but one
`ask.spec.ts` spec; that one remaining failure (`each of the five summary variants...`, a decision-
history text match) reproduces even with fake providers on the API server, most plausibly because
`e2e/global-setup.ts` spawns `scripts/seed_dev_data.py` as a *separate* process that doesn't inherit
whatever env a developer's shell happens to export to the API server — the seed's own
`run_embedding_sweep()` call would then embed with whatever provider *that* process defaults to.
Not investigated further or fixed this session: it's an F5 surface, pre-dates this round's own two
additions (neither touches Ask, embeddings, or decision-log content), and reproduces identically on
a checkout without this round's changes. Worth a future session exporting `CUE_LLM_*_PROVIDER=fake`
for both processes at once (or teaching `global-setup.ts` to pass it through explicitly) rather than
relying on ambient shell state neither process is guaranteed to share.

### FR-LED-05: commitment supersession detection, AI-proposed/human-confirmed (post-F6, 2026-08-10)

Not a numbered milestone — a real feature addition made on direct request while F6 (Vendor
Reliability Graph) was already Done, to close the one gap in that surface a Procurement user would
actually notice live: `revision_churn`/`price_drift_pct` showing `available=False` for every vendor,
forever, because `Commitment.supersedes` (FR-LED-05) had never been populated by any path in this
build — confirmed by grep before this session, the same discipline every other gap-closing round in
this file already follows.

**Design decision, made explicitly rather than assumed**: not fully automatic linking (an unreviewed
edit to a *different* commitment's own `supersedes` array is exactly the kind of thing CLAUDE.md's
"no commitment without evidence... verified in code, not trusted" already argues against), and not
purely manual either (nobody reliably notices and links a revision by hand). AI-proposes, human-
confirms — the same shape already proven three times elsewhere in this codebase (`Deviation`'s
`auto_drafted -> confirmed`, `OutboundMessage`'s `draft -> authorised -> sent`, `Commitment`'s own
`pending_verification -> human_verified`), reused rather than reinvented.

**New table, `commitment_supersession_candidates`** (`app/models/ledger.py`,
`CommitmentSupersessionCandidate`) — `commitment_id` (the newer commitment), `supersedes_commitment_id`
(the older one it may revise), `reasoning` (the model's own stated judgment, verbatim), `status`
(`pending`/`confirmed`/`rejected`), `reviewed_by`/`reviewed_at`. Direct-`project_id`-column RLS, same
shape `risks`/`deviations`/`notifications` already use (migration `f4b7c2a91e3d`, hand-authored
matching this project's own established migration style, not raw `alembic revision --autogenerate`
output — the autogenerate pass pulled in ~500 lines of unrelated pre-existing schema drift and was
discarded).

**`app/ledger/supersession.py`** — `find_candidate_priors` (plain SQL: same vendor, same
case-insensitive `deliverable_en`, recorded strictly before, capped at 3 most recent — cheap, and the
common case of a genuinely new commitment returns nothing without ever reaching the model at all);
`propose_supersession_candidates` (one `get_client("reasoning")` call per candidate found, only ever
writes a row for a "yes" verdict, same "only surface a real finding" posture `app/foresight/risk.py`'s
detectors already hold); `confirm_supersession_candidate` (the only path that ever mutates
`Commitment.supersedes` — reassigns the whole list, never an in-place `.append`, so SQLAlchemy's
change tracking actually fires — and triggers a fresh `recompute_vendor_metrics` immediately, so
`revision_churn`/`price_drift_pct` reflect the new link the instant a human confirms it);
`reject_supersession_candidate` (status only, no `Commitment` change, kept as its own honest record
rather than deleted).

**Hooked into both places a new `Commitment` can ever be created** — `app/ledger/extractor.py`'s
`extract_case` (passing `cue-eval/schema.json`'s own `price_changed` field through as a hint; that
field already existed, tuned, in the extraction schema, but nothing downstream had ever consumed it
before this — confirmed by grep, not touched itself, so this closes a real gap without touching the
tuned extraction prompt CLAUDE.md's own discipline protects) and `app/api/commitments.py`'s manual
`create_commitment` (a PM logging a renegotiated price by hand deserves the same detection an
extracted one gets).

**New router, `app/api/supersession.py`** — `GET/POST .../commitments/supersession-candidates`,
`WRITE_ROLES`-gated confirm/reject, `409` on double-confirm/reject rather than a silent no-op. **Real
routing bug caught by the test suite, not assumed**: registering this router *after*
`commitments_router` in `main.py` let `GET .../commitments/{commitment_id}`'s wildcard path segment
swallow `.../commitments/supersession-candidates` first (a literal string matched the `{commitment_id}`
UUID param position and 422'd) — the identical class of bug `.../documents/search` vs.
`.../documents/{document_id}` already hit and fixed the same way (round 5's own notes); fixed by
registering the more specific router first, same precedent.

**`app/llm/client.py`'s `FakeClient` gained a fourth named schema branch** — without it, CI (which
never runs real Ollama, per the "Architecture correction" entry above) would synthesize
`supersedes: false` for this new schema by default (the generic fallback returns `False` for any
unrecognised boolean field), silently producing zero candidates from the seed script's own real
revision fixture in every CI run. `_fake_supersession` makes an honest amount-comparison judgment
(parses both commitments' amounts straight from the prompt's own fixed template; genuinely differing
means "yes," identical means "no") — same "an honest judgment, not an unconditional yes" discipline
the Ask answer-generation branch already established, for the identical reason: an always-`true` fake
would never exercise this feature's own reject path in CI.

**Dev-seed extension, on `second_vendor` ("Nimbus Event Staffing Pte Ltd"), deliberately not
`vendor` ("Golden Sound & Light Pte Ltd")** — a real cross-suite interaction the frontend's own e2e
run caught: both spec files (`e2e/vendors.spec.ts`, `e2e/supersession.spec.ts`) share one seeded org
for a whole Playwright invocation, and confirming a candidate on the same vendor F6's own suite
asserts stays `revision_churn`/`price_drift_pct`-`unavailable` would make that earlier, independently-
correct assertion false depending on run order. Separate vendors keep both fixtures independent
regardless of which file Playwright happens to run first. **A second real bug this same verification
pass caught**: the original and revision commitments were both inserted in the same, still-uncommitted
transaction — `created_at` is `server_default=func.now()`, fixed for the lifetime of one Postgres
transaction, so `find_candidate_priors`'s own "recorded strictly before" filter found zero candidates
until a real intermediate `session.commit()` was added between them (same class of bug the two
`recompute_vendor_metrics` calls earlier in this same script already had to work around, for the
identical reason). **A third**: the revision commitment's `verification_state` was originally
`pending_verification`, silently breaking `e2e/living-wip.spec.ts`'s own already-established assertion
("zero 'Pending verification' badges remain after verifying the one known commitment") once a second,
never-verified pending commitment existed anywhere else in the same shared seeded project — caught by
running the *full* suite, not just this feature's own two new specs, and fixed by seeding it
`human_verified` instead (nothing about this fixture's own test needs it pending).

**Frontend-enablement fix closed in the same pass, not left as documented — `GET /parties/{id}/
organisation`(`/current`) were `require_org_administrator`-only**, a real, live gate mismatch F6's own
gap-audit had found and documented but not fixed (a Finance/Producer user could see every other
section of a vendor's detail page and still 403 on this one). New `require_org_finance_or_administrator`
dependency (`app/api/deps.py`) — union of both role sets — on the two GET operations; the write
(`POST .../organisation`, a genuine FR-NRM-04 roster-management action) stays administrator-only,
unchanged. `tests/test_party_organisation_mapping.py`, 2 new tests (a finance-only user can now read
both GETs but still 403s on the write; a project_manager-only user — neither tier — still 403s on
reads too, confirming the gate was widened, not opened to everyone).

Full `uv run pytest` green throughout this work: 544 passing (up from 528 after F6's own round 6).
Verified against the real running backend at every step, not assumed — `curl`, not just tests: a
freshly seeded organisation's real Ollama call produced a correctly-reasoned candidate
("...both commitments have identical descriptions... differ in amount (from 18500.0 SGD to 21000.0
SGD)... strongly indicates a revision"), confirming it made `revision_churn`/`price_drift_pct` flip
from `available=False` to real computed values (`price_drift_pct: 13.51%`, matching
`(21000-18500)/18500*100` exactly) in the same live session. `e2e/supersession.spec.ts` (frontend)
covers the same flow through the real UI, both with real Ollama and with `FakeClient`'s new branch
(CI parity, `CUE_LLM_*_PROVIDER=fake`) — see frontend/PROGRESS.md's own notes for the UI side.

### Frontend-enablement addition, round 7 (during frontend F7, 2026-08-11)

One gap `Prompt F7 — Admin console.txt` found while wiring the Budget baseline/revise screen, same
"close it on the spot, document it" pattern as rounds 1–6:

- **`GET /projects/{project_id}/budget/history`** (`app/api/budget.py`, no new response schema —
  `BudgetOut` already exists). Before this, `GET .../budget` only ever returned the single current
  row — `BudgetOut.revision_of` was a real Class A id-with-no-resolver (frontend/CLAUDE.md's own gap
  shape): a caller could see *that* the current baseline revises a prior one, never what that prior
  amount actually was. FR-ADM-11's own "full audit trail and revision history on scope-approved
  changes" language was already true at the row level (`revise_budget` never mutates a prior row,
  only flips `is_current`) but had no read surface. Same access tier as the existing current-budget
  read (`Depends(get_project)`, any project member) — this is a superset of the same information, not
  a more sensitive one. Most-recent-first, same order convention `list_writeback_history`/
  `channel_health_history` already use. `tests/test_budget_api.py`, 2 new tests (baseline + revision
  both present in the right order with `revision_of` linking them; an honest empty list when no
  baseline exists yet).

`uv run pytest` green afterward. `pnpm generate:api` re-run against the live reload; frontend's
`lib/api/schema.gen.ts` picked up the new operation with no other diff.

A second gap, same round, found while wiring the channel-identity-override and organisation-mapping
write screens:

- **`GET /parties` and `GET /parties/{party_id}` widened from `require_org_finance` to
  `require_org_finance_or_administrator`** (`app/api/parties.py`'s `list_router`). Both
  FR-NRM-03's manual identity-override screen and FR-NRM-04's organisation-mapping write control are
  `require_org_administrator`-only actions, but each needs to *pick* a party from a real directory
  first — the only party list in the API was finance/producer-gated, so a pure administrator (neither
  finance nor producer) would 403 trying to name a party for either action. The mirror image of round
  6's own fix (there, an administrator-only gate was stricter than the finance-gated page around it;
  here a finance-only gate is stricter than the administrator-only actions that need it) — same
  `require_org_finance_or_administrator` dependency, already existing for exactly this shape of
  mismatch, reused rather than a new one invented. `tests/test_parties_list_api.py`, 2 new tests
  (administrator-without-finance-or-producer succeeds on both routes); the existing
  finance-or-producer-required tests are unaffected since `project_manager` (their own probe role) is
  neither.

`uv run pytest` green: 548 passing (up from 546 after the budget-history addition above).

A third gap, same round, found while wiring the org-wide Delegations screen:

- **`GET /admin/projects`** (`app/api/admin.py`, no new response schema — `ProjectOut` already
  exists). `GET /admin/delegations`/`GET /admin/roles` can both return rows whose `project_id`
  refers to a project the calling Administrator was never a member of —
  `require_org_administrator`'s own docstring says exactly this ("free to read/list across every
  project in that organisation, not just ones they happen to be a member of"), already exercised by
  `test_org_admin_visibility_is_distinct_from_project_membership` for `/admin/export`. The only
  project listing before this, `GET /projects`, is FR-ADM-02's own membership-filtered view — it
  cannot resolve a project_id the caller doesn't belong to, a real Class A id-with-no-resolver on the
  delegations/roles audit screens specifically (frontend/CLAUDE.md's own gap shape). No explicit
  organisation filter needed — `projects` carries its own direct-column `tenant_isolation` RLS policy,
  same as `users`. `tests/test_admin_api.py`, 1 new test (an Administrator on project A only still
  resolves project B by name via this endpoint).

`uv run pytest` green: 549 passing (up from 548). `pnpm generate:api` re-run; no other diff.

### CI hardening, post-F7 (2026-08-11): two real, root-caused bugs, not flakiness

Found by actually reading a failing GitHub Actions run's logs rather than assuming "CI is just
flaky" — both reproduced locally once the same conditions were recreated, and both are fixed, not
worked around:

- **`backend/.github/workflows/pytest.yml` never set `CUE_LLM_*_PROVIDER`/`CUE_EMBED_PROVIDER`.**
  `app/llm/config.py`'s `LLMSettings` defaults both provider roles to `"ollama"` when unset, so any
  test exercising a real LLM call (FR-LED-05's `propose_supersession_candidates`, wired into
  `create_commitment` since the post-F6 round) implicitly required a real Ollama this runner never
  has, failing with `httpx.ConnectError: All connection attempts failed`. `FakeClient`/
  `FakeEmbeddingClient` already exist and are schema-aware for this exact call (the "Architecture
  correction" already applied to frontend's own Playwright CI job, post-F5) — just never wired into
  this workflow. Fixed by adding the same three env vars that workflow already sets. Verified
  locally with them set: 549 passing before the fix below, 551 after.
- **`MinioStorageBackend.signed_url` never ensured the bucket exists — only `put` did**
  (`app/documents/storage.py`). Invisible against a long-lived local MinIO (its named volume already
  has the bucket from some earlier real upload), but a fresh/ephemeral MinIO — a CI run's own
  `docker compose up -d --wait`, empty volume — 500s with `minio.error.S3Error: NoSuchBucket` the
  instant anything resolves a signed URL before any real upload has happened. `scripts/
  seed_dev_data.py`'s own two seeded `DocumentVersion` rows hit this exactly: their `storage_ref`
  points at no real object *by design* (that script's own comment: "this fixture never exercises
  download/approve on these two rows"), a call nobody anticipated when F5's Ask citation resolution
  (`GET .../documents/versions/{id}` → `_to_version_out` → `signed_url`) was added later. Root-caused
  by pulling and reading the actual failing frontend CI run's backend log artifact (a real
  `minio.error.S3Error` traceback, not inferred), not assumed from "ask.spec.ts is flaky" — the
  specific failing test differed run to run because whichever seeded-citation test happened to run
  before `e2e/documents.spec.ts`'s own real-upload test (which incidentally creates the bucket as a
  side effect) lost the race. Fixed by calling the same idempotent `_ensure_bucket_sync` check `put`
  already makes. `tests/test_storage.py`, 2 new tests against the real MinIO — a signed URL against a
  guaranteed-fresh, never-written-to bucket (name randomised so no earlier test/session's own bucket
  can mask the bug), and the ordinary put-then-sign path stays correct. Confirmed the first test
  actually catches the bug: reverted the fix, watched it fail with the identical `NoSuchBucket`
  error, restored it, watched it pass.

`uv run pytest` green: 551 passing (up from 549). Neither bug was caused by F7 — both reproduced
identically on commits before it — but both were found while investigating F7's own first CI run, so
recorded here rather than left as a mystery for whoever hits them next.

### Frontend-enablement addition, round 8 (during frontend F9, 2026-08-11)

F9 (frontend Hardening) needed a genuinely new capability, not a resolver gap on an existing one:
NFR-ACC-03's high-contrast mode has to be "persisted per-user, not just per-session" (the prompt's
own wording, deliberately distinct from the frontend's existing theme toggle, a device-local
`localStorage` preference with no backend row at all) — there was no per-user settings surface of
any kind in this API before this round.

- **`GET/PATCH /users/me`** (new `app/api/users.py`, `UserMeOut`/`UserPreferencesUpdate` in
  `app/api/schemas.py`, `User.high_contrast` — migration `5d68b34f2fa8`). Both depend on the
  existing `get_current_user` — no new auth plumbing needed, this is "who am I and what are my own
  settings," reachable by any authenticated user about themselves, not the org-admin-gated `UserOut`
  directory listing. `tests/test_users_me.py`, 4 tests (default value, flip-and-persist, no
  cross-user leakage via a second real user, 401 unauthenticated).

**A real bug in this round's own first implementation, found by a real e2e run against a real
server, not caught by its own unit test.** `update_me` originally called `session.commit()` then
`session.refresh(user)` — backwards. `app/core/db.py`'s own `get_session` docstring: `app.
current_org_id` is set `is_local=true`, scoped to the request's *transaction*, specifically so it
can't leak across a pooled connection into a different request; `commit()` ends that transaction, so
a `refresh()` called *after* it runs its own SELECT with no RLS context at all and finds zero rows
(`sqlalchemy.exc.InvalidRequestError: Could not refresh instance`). `app/api/milestones.py`'s
`update_milestone` already established the correct order (flush, refresh, *then* commit) for exactly
this reason — this endpoint just hadn't followed it, an oversight this round's own author made and
caught themselves, not a pre-existing bug inherited from elsewhere. In the browser this surfaced as
an opaque CORS failure (a 500 response carries no CORS headers here, and Chrome reports "blocked by
CORS policy" for any cross-origin response missing them — the real 500 was only visible in the
server's own log). Fixed by matching `update_milestone`'s own order.

Worth naming plainly: **`tests/test_users_me.py`'s own in-process `ASGITransport` test never
reproduced this bug at all**, before or after the fix — it kept passing throughout, on both the
broken and the corrected version. Only a real e2e run against a real running `uvicorn` process (the
frontend's own `e2e/hardening.spec.ts`) caught it, the same category of test-vs-real-server
divergence this file's own M10 notes already document for a different fixture
(`app_session` transaction-scoping flakiness) — recorded here as a second, independent instance of
the same lesson, not assumed to be a one-off.

`uv run pytest` green: 555 passing (up from 551).

## M11 — Layer B Channel Picker (2026-08-17)

Closed the gap `Layer B Channel Picker — Implementation Prompt.txt` names: a PM had no working way to
attach a WhatsApp channel, because the attach form asked for a "group name" but actually required the
raw WhatsApp group JID, which WhatsApp never shows a human anywhere. Layer A's own side of this
(`GET /conversations`, `POST /conversations/allowlist/add|remove`, `layer-A/src/api/machine/index.ts`)
was already built, tested, and live-verified going into this session (`layer-A/PROGRESS.md`'s own
"Machine API conversation discovery + allowlist coupling" section) — this milestone is the backend/
frontend half that actually calls it.

**What was built:**

- **`WhatsAppAdapter.list_conversations`/`add_to_allowlist`/`remove_from_allowlist`**
  (`app/capture/adapters/whatsapp.py`) — three new methods alongside the existing
  `fetch_backlog`/`send`/`health`/`fetch_media`, calling Layer A's Machine API the same
  bearer-token way. Unlike the existing methods, none of these take a `Channel` — there's no
  channel to key off of yet before an attach happens, matching Layer A's own "discovery has no
  `group_id` to resolve from yet" design.
- **`GET /projects/{project_id}/channels/whatsapp/conversations`** (new route,
  `app/api/channels.py`) — a live proxy over `list_conversations`, gated the same `ADMIN_ROLES`
  tier as `attach_channel`. Deliberately not cached beyond the request: Layer A resolves
  conversation names in real time, not from a static list, so this route doesn't paper over that
  with server-side caching either. A genuinely ambiguous Layer A account (more than one configured)
  surfaces as a real 503, not a silent empty list.
- **`attach_channel` now grants Layer A's allowlist as part of the same request** for
  `type="whatsapp"` — the allowlist grant runs *before* the `Channel` row is inserted, so a failed
  grant (Layer A misconfigured, unreachable, or genuinely ambiguous) never leaves a `Channel` that
  silently captures nothing. `ChannelCreate` gained `display_name` (generic, not WhatsApp-only) —
  the picker's own resolved label, cached at attach time rather than re-resolved live on every
  channels-list read (see `Channel.display_name`'s own docstring, `app/models/project.py`, for the
  full tradeoff — a real design choice with real tradeoffs, made explicitly, not defaulted into).
  `external_ref` stays the one generic identifier field for every channel type; the picker just
  supplies a real jid to it instead of a hand-typed value.
- **`detach_channel` now revokes Layer A's allowlist first, then deletes the `Channel` row** — the
  route already existed (`Layer B Channel Picker`'s own READ THIS FIRST note said to check before
  assuming it needed building); this session added the missing other half of the coupling. Same
  fail-closed ordering as attach: a Layer A failure leaves the `Channel` row intact and retryable
  rather than orphaning a still-active capture grant.
- **Migration `a1c3e6f9d2b7`** — `channels.display_name`, nullable, additive.

**A stale claim corrected while in the file it was in**: `WhatsAppAdapter`'s own class docstring said
"genuinely credential-blocked... code-complete, never live-tested," true when M8 wrote it but false by
the time this session's own live test ran against it — corrected in place rather than left for a third
session to trip over (the exact "verify against the real current behaviour" lesson this prompt's own
NON-OBVIOUS section names twice).

**Tested, for real, three ways:**

1. `tests/test_layer_a_contract.py` — three new tests (`list_conversations`, `add_to_allowlist`
   without disturbing an already-designated conversation, `remove_from_allowlist`) against the real
   `contract-server.ts` subprocess, same pattern the file's existing tests already use.
   `layer-A/test/contract-server.ts` was extended to seed two known conversations for
   `FixtureConnector` — a test-fixture addition, not a change to Layer A's real discovery/allowlist
   machinery itself.
2. `tests/test_channels_api.py` — attach/detach error-path tests (Layer A unconfigured → 503, no
   `Channel` row left behind; no `external_ref` → allowlist call correctly skipped), using
   `monkeypatch.setattr` on the settings getter (not `os.environ` — `WhatsAppSettings` reads `.env`
   directly, same fact `test_capture_adapters_live.py` already documents). The pre-existing
   `test_attach_channel` switched from `type="whatsapp"` to `type="wechat"` — with the new allowlist
   coupling, a hand-typed whatsapp `external_ref` in a generic unit test would now hit real Layer A
   with a nonsense jid.
3. **`tests/test_channels_whatsapp_live.py` — a real end-to-end test against the real linked WhatsApp
   account**, same account `layer-A/PROGRESS.md`'s "WhatsApp account linking" section describes,
   genuinely reachable in this session's environment (Layer A was already running on port 4100, real
   `.env` credentials configured). Picks a real, currently-undesignated conversation from the picker
   endpoint, attaches it, confirms Layer A's own allowlist actually flips to `designated: true` (not
   just the local `Channel` row — the exact gap this whole prompt exists to close), detaches, confirms
   it flips back. Every mutation reversed in a `finally` block regardless of outcome. Same
   skip-cleanly-if-unconfigured/fail-for-real-if-unreachable convention as
   `test_capture_adapters_live.py`.

**A real, reproducible test-infrastructure bug found and fixed during this work, not by this
milestone's own logic but by writing test 3 above**: an early draft of `test_channels_whatsapp_live.py`
called `set_org_context(app_session, org_id)` redundantly — `authed_org_and_project` already sets it.
That extra call, executed after the fixture's own commit with no matching commit/rollback of its own,
left a session-scoped (`is_local=False`) `app.current_org_id` GUC sitting on the pooled connection at
fixture teardown; a *later, unrelated* test in the same `uv run pytest` process that happened to draw
that exact connection back out of the pool then failed a real RLS check
(`tests/test_parties_compute.py`, `tests/test_foresight_*.py`, `tests/test_lifecycle.py` — 19 failures
across the full suite, none of them in this milestone's own files). Confirmed deterministic and
isolated to that one redundant line (removing it alone fixed all 19, twice, in isolation). This is the
same class of pool-checkout-order flakiness `tests/conftest.py`'s own `app_session` fixture docstring
already names as "latent... not introduced by this session" — a second, real instance of it, not a new
bug in the mitigation itself. Left named here rather than only in the test file's own comment, since
the next person hitting a mysterious RLS failure several files away from what they actually touched
should be able to find this.

`uv run pytest` green: 569 passing (up from 555; +1 pre-existing failure unrelated to this work —
`test_capture_worker.py::test_enqueue_is_deduplicated_per_channel_against_real_valkey` fails because
`docker-compose.yml`'s `valkey` service isn't port-mapped to the host in this environment, confirmed
by `docker ps`/`nc` directly; not something this milestone's changes touch or could fix).

## Capture debug console + a real production org-context concurrency bug (2026-08-17)

Requested directly, not part of any numbered prompt: a manual "pull now" trigger and a raw-capture
debug view for a channel, gated the same `ADMIN_ROLES` tier as the rest of the channels router,
meant to run for real in production (not a throwaway dev-only tool).

**What was built:**

- **`GET /projects/{project_id}/channels/{channel_id}/messages`** — real `Message` rows
  (`app/capture/models.py`), independent of extraction: a message shows up here the moment capture
  durably writes it (NFR-AVL-02), whether or not `extraction_attempted_at` is set or produced a
  Commitment. Chronological, oldest first.
- **`POST /projects/{project_id}/channels/{channel_id}/capture/pull-now`** — the "manual 'poll now'
  admin action" `app/capture/worker.py`'s own module docstring already named as a real, anticipated
  consumer of `enqueue_channel_ingestion`; this is that consumer. Enqueues the same real arq job the
  scheduled worker itself uses (`since=None`), never a second ingestion path. Validates `channel_id`
  actually belongs to the calling `project` before enqueueing — `enqueue_channel_ingestion` itself
  takes a bare channel id with no project/org check, so skipping this would let an admin of one
  project trigger ingestion for a channel belonging to a completely different project by guessing its
  id (caught in review, before it shipped).

**A real, live-discovered production bug, fixed, not just noted:** the first live "pull now" run
against the real linked WhatsApp account failed on the *second* message of the backlog with
`InsufficientPrivilegeError: new row violates row-level security policy for table "messages"` — the
first message had already committed correctly, under the right org. Root cause: `app/capture/
worker.py` and `app/foresight/worker.py`'s own `_set_org_context` helpers set `app.current_org_id`
with `is_local=false` (session-scoped), deliberately, because one job/sweep makes several sequential
commits and the value needs to survive all of them. But SQLAlchemy's `AsyncSession` releases its
DBAPI connection back to the engine's pool on every `commit()` — confirmed directly, not assumed,
against a real connection's own `pg_backend_pid()` — so a session-scoped value set once at job start
survives on the *physical connection* after release, and the next thing to check that connection out
of the shared pool (arq's own cron jobs and on-demand jobs all run concurrently in one worker
process) silently inherits it. A long-running `ingest_channel_job` (slow because of real per-message
LLM extraction) overlapping a scheduled cron tick is exactly this scenario, and it isn't rare — it's
the normal case for any backlog with more than a couple of messages.

**The fix**: `app/core/db.py` gained two primitives —

- `set_local_org_context(session, org_id)` — a bare `SET LOCAL` (`is_local=true`) call, for a caller
  with its own custom commit/rollback branches.
- `org_scoped_transaction(session, org_id)` — an async context manager wrapping the common "set,
  work, commit" shape. `is_local=true` means Postgres resets it automatically at the end of *every*
  transaction (commit or rollback), so it can never survive onto a connection returned to the pool —
  the same guarantee `app/api/deps.py`'s `get_current_user` already relies on for the single-
  transaction, per-request case. The only behavioural change a multi-commit caller needs is
  re-asserting it before *each* unit of work about to be committed, not once per session.

Applied everywhere the same shape existed — every `is_local=false` call site inside a *long-running,
multi-job worker process* (not the two standalone one-shot CLI scripts, `scripts/seed_dev_data.py`/
`scripts/extract_fixtures.py`, which hold their own dedicated pool for a short process lifetime and
never race a concurrent job for it — left unchanged, with the reasoning now recorded in `get_session`'s
own docstring):

- `app/capture/pipeline.py`'s `ingest_channel_backlog` — the actual root cause, one commit per
  message.
- `app/capture/worker.py`'s `ingest_channel_job` — its own initial channel/project reads.
- `app/foresight/worker.py`'s `run_project_sweep` (six commits per project) and `run_foresight_sweep`'s
  own project read.
- `app/reports/schedule.py`'s `run_due_report_schedules` and `app/ask/embed_worker.py`'s
  `run_embedding_sweep` — each currently makes only one commit per session, so neither was actually
  exposed to this specific race yet, but both were converted anyway: the old pattern is a landmine for
  the next edit that adds a second commit, not a real safety margin today.
- `app/capture/schedule.py`'s `run_due_extraction_schedules` — calls the same `ingest_channel_backlog`
  as the fix above, plus its own trailing `config.last_run_at` write needed its own re-assertion (that
  statement runs *after* `ingest_channel_backlog`'s own last per-message commit already released the
  connection).

**Proven with a real, adversarial concurrency test, not just the live run above**:
`tests/test_org_context_concurrency.py` builds its own `pool_size=1, max_overflow=0` engine against
the real test database and drives two sessions for two different orgs through several commits each,
round by round via `asyncio.gather`, so both are genuinely contending for the pool's one connection at
the same real point in time every round. One test reproduces the *old* pattern failing deterministically
(a real RLS rejection, confirmed empirically that naive `asyncio.gather` without a forced scheduler
handoff doesn't actually interleave the two tasks under this driver — a bare `asyncio.sleep(0.01)` per
round does, verified directly against `pg_backend_pid()` before trusting the reproduction); the other
proves `org_scoped_transaction` is safe under the identical setup, reading the result back via the
schema-owner connection to confirm every row landed under its own correct org, not just that nothing
raised. First draft of this test wrote `Party` rows and never failed *either* version — `parties` turned
out to have no RLS policy at all (confirmed against `alembic/versions/a895ae03ec5c_initial_schema.py`);
switched to `Project`, which genuinely has one.

**Verified for real against the live linked WhatsApp account, after the fix**: same channel, same
6-message real fixture backlog that previously failed on message 2 — restarted the backend + a real
`arq` worker process from a clean Valkey queue (`docker exec cue-backend-valkey-1 valkey-cli FLUSHALL`,
this environment's own dev queue, not production), triggered `pull-now`, all 6 messages landed with
`extraction_attempted_at` set on every one, zero RLS errors.

**Two separate, real issues surfaced by this same live run, deliberately not fixed here (out of scope
for an org-context bug fix, named plainly rather than silently left implicit):**

1. **arq's default 300s `job_timeout` is too short for a real, LLM-extraction-heavy backlog pull.**
   The live verification run's first attempt was killed by arq's own timeout mid-flight (Ollama
   extraction across 6 messages took longer), which left a `MissingGreenlet` error on cleanup and one
   `KeyError` in arq's own internal job-tracking dict — arq's automatic retry then completed the job
   correctly on the second attempt, and the worker process itself never crashed, but this is a real,
   reproducible operational rough edge for any channel with a non-trivial backlog, not a one-off.
   `WorkerSettings`/`ingest_channel_job`'s own per-job timeout is the fix surface; not touched this
   session.
2. **`detach_channel` (`app/api/channels.py`) 500s instead of a clean error once a channel has real
   captured `Message` rows** — `messages.channel_id`'s FK has no `ON DELETE` behaviour, so
   `session.delete(channel)` raises a raw `ForeignKeyViolationError` that reaches the client as an
   unhandled 500. Never hit before because no existing test attaches a channel, lets the real pipeline
   capture messages for it, and *then* detaches it in the same flow — this session's own live
   verification was the first thing to do exactly that. The real fix is a product decision (cascade-
   delete a channel's message history on detach, refuse detach while messages exist, or soft-delete the
   channel instead of a hard `DELETE`), not a one-line patch — named here, not silently patched around.

`uv run pytest` green: 577 passing (up from 575; the 2 new concurrency tests, no regressions).

## Capture debug console, part 2: real job status, not "refresh and hope" (2026-08-17)

Direct, real-user feedback on the debug console shipped in the entry just above: a static "Pull
queued — this runs in the background; refresh in a moment" message with no actual state is bad UX
for an action that can take minutes — no way to tell "still running" from "stuck" from "done" short
of clicking Refresh repeatedly and guessing. Fixed by exposing arq's own real job state instead of
inventing a second, parallel status of this API's own.

**What was built:**

- **`GET /projects/{project_id}/channels/{channel_id}/capture/status`** — reads `arq.jobs.Job`
  directly (`Job(channel_job_id(channel_id), redis)`, the same deterministic per-channel id
  `enqueue_channel_ingestion`'s own dedup already uses — pulled out as `app/capture/worker.py`'s own
  `channel_job_id()` function so the two call sites can't drift apart). Reports
  `not_found`/`deferred`/`queued`/`in_progress`/`complete` (arq's own `JobStatus` values verbatim),
  and once `complete`: `success` + either the real `IngestionSummary` fields (`received`,
  `new_messages`, `duplicates`, ...) on success, `error` (a real `str(exception)`, never a raw
  traceback) on failure, or `skipped`/`skip_reason` for the job's own early-exit case (channel/project
  deleted between enqueue and pickup). `pull_channel_now`'s own redis-pool boilerplate was pulled out
  into a shared `_arq_redis()` context manager (`app/api/channels.py`) once a second endpoint needed
  the identical connect/cleanup.

**A real, live-discovered secondary bug, found *because* this new endpoint made a previously-silent
failure visible for the first time, and fixed, not just noted:** the first real attempt to watch a
fresh pull through to completion (a genuinely new backlog, real Ollama extraction, no cached
duplicates to short-circuit it) surfaced `sqlalchemy.exc.MissingGreenlet` — arq's own worker-wide
default `job_timeout=300` had been silently killing `ingest_channel_job` mid-flight and retrying it
this whole time (this file's own prior entry already named the 300s ceiling as too short for
LLM-heavy pulls and deliberately left it unfixed, out of scope for that session's own org-context bug;
this session is where it actually blocked forward progress, so it got fixed here instead of deferred
a second time). A job arq cancels for exceeding its timeout doesn't fail cleanly — the cancelled
greenlet-bridged async DB call is left corrupted, and the *next* statement on that same job's session
raises `MissingGreenlet`, not a clean timeout error. Fixed with `arq.func`'s per-function `timeout=`
override (`app/foresight/worker.py`'s `WorkerSettings`, 30 minutes, applied only to
`ingest_channel_job` and `run_due_extraction_schedules` — the two paths that actually call
`ingest_channel_backlog` — not a worker-wide bump every fast cron sweep would inherit for no reason).
One real, confirmed-against-arq's-own-source subtlety along the way: `arq.cron()`'s own `coroutine`
argument requires a real `inspect.iscoroutinefunction` and raises if handed an `arq.func`-wrapped
`Function` object instead, so `run_due_extraction_schedules` (registered both ways — on-demand via
`functions`, scheduled via `cron_jobs`) needed its per-function timeout applied two different ways: `arq.func(..., timeout=...)` for the `functions` entry, `cron(..., timeout=...)` (that call's own
native kwarg) for the `cron_jobs` entry — not the same wrapped object reused in both places, which
would have raised at class-definition time.

**Tested, for real:**

1. `tests/test_channels_api.py` — four new tests: `not_found` before any pull; a genuinely `complete`
   pull with the real result counts, run against a **real, burst-mode `arq.worker.Worker`** (not a
   direct call to `ingest_channel_job`, which would never populate arq's own job-result state at all
   — that distinction is the whole point of what this endpoint reads); a genuine `complete, success=
   False` failure with a real error message, from a client that genuinely raises; the same
   cross-project ownership check the other channel-scoped endpoints already enforce. The "real
   completed pull" test needed `app.ledger.extractor.get_client` monkeypatched to the real `FakeClient`
   (app/llm/client.py, CI's own extraction stand-in) — arq's own dispatch only ever passes the
   positional args `enqueue_channel_ingestion` gave it, so `ingest_channel_job`'s own keyword-only
   `client=` test seam (used everywhere else in this suite to avoid live Ollama) isn't reachable
   through a real arq dispatch; this is the one test in the suite that has to reach one level further.
2. **A real, live regression found and fixed by this session's own test-running process, not the
   feature's own code**: running the full `uv run pytest` while a live `arq` worker was *also* running
   in the background (started earlier in this session for live UI verification) made the new
   `test_capture_status_reports_a_real_completed_pull` fail intermittently — the live worker and the
   test's own burst-mode `Worker` were racing for the same jobs on the same real Valkey queue (arq/
   Valkey isn't test-isolated the way Postgres is — every test in this suite that touches it already
   implicitly assumes it's the only consumer). Confirmed by stopping the live worker and re-running:
   581/581 clean, both with and without the fix in isolation — a testing-hygiene issue, not a bug in
   the endpoint or the timeout fix, but worth naming so a future session doesn't chase a phantom
   flake without knowing why.
3. **Live-verified against a real backlog through to genuine completion**, not just short pulls that
   short-circuit on already-captured duplicates: watched a fresh channel's pull transition `queued` →
   `in_progress` → `complete` with real counts, confirming the 30-minute timeout actually gives real
   Ollama extraction enough room where 300s previously did not.

`uv run pytest` green: 581 passing (up from 577; 4 new tests, no regressions once the live-worker/
test-suite race above was understood and controlled for).

## Blind Spots: eight unwired-field gaps closed (2026-08-17)

`CUE Blind Spots — Implementation Prompt.txt`'s own audit — a producer genuinely computes/records
something, no screen/export/query anywhere ever reads it back — closed for all eight, in the
prompt's own impact order. Every fix additive (a new call, a new column, a new response field);
no producer logic touched, per that file's own explicit scope boundary.

**1. Deviation event dispatch** (`app/foresight/deviation.py`). `dispatch_event(session,
project=project, event_type="deviation", deviation=deviation)` added once inside `draft_deviation`
(both real callers, `contradiction.py`/`forecast.py`, get it for free) and once each in
`confirm_deviation`/`resolve_deviation`, which now take `project: Project` as a required keyword
param threaded from `app/api/deviations.py`'s two endpoints (their sole callers already had it in
scope — not re-fetched by `deviation.project_id`). `create_manual_deviation` deliberately left
alone: the prompt's own fix-location bullets name only these three functions, and a PM's own
immediately-`confirmed` manual entry has no one to notify that isn't the actor themselves.
Tested end to end (`tests/test_deviations_api.py`): auto-draft, confirm-endpoint and
resolve-endpoint each assert a real `Notification` row with `deviation_id` set and
`_event_type_for_notification` resolving to `"deviation"`, reusing `authed_org_and_project`'s own
administrator membership as the real fallback recipient (`default_recipients`'
project_manager-or-administrator rule) rather than provisioning a redundant PM fixture.

**2. Consent notice on real capture** (`app/capture/normalise.py`, `app/capture/identity.py`).
Design decision, made explicitly rather than defaulted into: `IdentityResolution` now carries the
resolved `Party` object (`party: Party | None`), populated only on the `created_new_identity=True`
branches (steps 2/3 of `resolve_identity`, which already have the ORM object in hand from
linking/minting it — free, no extra query) and left `None` on the common fast path (step 1, an
identity that already existed), since nothing on that hot path needs it and fetching it there would
cost a needless second query on every repeat sender's every message. `normalise_and_ingest` now
calls `post_consent_notice` when `resolution.created_new_identity` is true, after the opt-out gate
(so an already-opted-out party linked via a new identity is never sent a notice) and before the
`Message` insert. Wrapped in `session.begin_nested()` (mirrors `app/llm/cost.py`'s
`record_llm_usage` SAVEPOINT pattern) — a plain try/except around the call was not enough on its
own, since a failed `flush()` inside `upsert_consent_record` would otherwise leave the *outer*
transaction unusable for the `Message` insert that follows, defeating NFR-AVL-02 worse than not
catching at all. `NotImplementedError` (a file_storage-capability channel with no `send()` concept)
logs at `info`; anything else logs at `warning`; neither aborts ingestion.
Tested (`tests/test_capture_normalise.py`): a genuinely new party ingested through
`normalise_and_ingest` leaves a real `ConsentRecord` in `pending` state with `notice_sent_at` set;
a second message from the same, now-known sender does not create a second record.

**3. `WritebackAuditLog`/`DocumentAuditLog` via `_export_bundle`** (`app/api/admin.py`). Two new
keys, same `_row(obj, fields)` shape as `audit_log` immediately above them — `document_audit_log`
(`id, project_id, document_id, document_version_id, action, actor_id, occurred_at, detail`) and
`writeback_audit_log` (`id, project_id, action, actor_id, occurred_at, detail`). Also added
`audit_log.detail` and `evidence.transcript_confidence` to that same function's existing field
lists (both real columns simply absent from these hand-written lists). Tested
(`tests/test_admin_api.py`): a real document upload (two real `DocumentAuditLog` rows,
`document_created`/`version_created`, with real `detail`) and a real `PATCH .../writeback/config`
call (one real `WritebackAuditLog` row with `detail={"before": 1, "after": 25}`) both come back
through `GET /admin/export/{project_id}`, JSON and CSV.

**4. `SpecClaim.confidence`** (`app/documents/models.py`, `app/documents/extractor.py`,
`app/api/schemas.py`, `app/api/documents.py`). Schema-constraint reading, stated explicitly per the
prompt's own instruction: `SpecClaim`'s docstring "do not add or rename fields" is about §4.3's own
claim-*content* schema (Location/Description/Dimension/Finishing/Qty/Status — i.e. `attribute`/
`value`/`location_code`), not extraction metadata about the claim — the same carve-out
`Commitment.confidence` already relies on without touching Commitment's own §4.2 field set. Read it
that way; added `confidence: float | None`, nullable (no existing row has one to backfill), via
`alembic/versions/06ed0141ee07_add_spec_claim_confidence.py` (mirrors `f3f332c90366`'s style — the
most recent additive-column migration at the time). Wired through `extract_spec_claims`
(`item.confidence` was already being extracted, just never persisted), exposed on `SpecClaimOut`
(inherited by `SpecClaimResolvedOut` automatically). Tested: `tests/test_document_extractor.py`
asserts the persisted row's `confidence` matches the fake extraction client's own canned value;
`tests/test_documents_api.py` asserts a real `GET .../spec-claims` response round-trips it.

**5. `Evidence.transcript_confidence` in `EvidenceOut`** (`app/api/schemas.py`). Same shape as
`media_ref` immediately above it in that class — column existed since M8's voice-note pipeline, no
schema ever exposed it. Tested by extending `tests/test_frontend_enablement_f1.py`'s own
`media_ref` round-trip tests (both the populated and the null case) to assert this field too, rather
than inventing a new test.

**6. `Message.identity_confidence`/`identity_manually_verified` in `MessageOut`**
(`app/api/schemas.py`). Straightforward additive fields — `MessageOut` already reads via
`from_attributes=True` off the real ORM row, so no endpoint code changed. Explicit decision on the
review-surface question the prompt asked to be answered, not defaulted: **no dedicated filter/sort
endpoint added this round** — exposing the raw fields is sufficient for now. A real "review a
low-confidence match" workflow (who reviews it, what correcting it even means beyond
`set_manual_identity_override`, which already exists) is a product-scoped feature in its own right,
not a natural extension of an additive schema-field task, and every one of this file's items is
scoped as "make the data retrievable," not "build a workflow around it." Tested by extending
`tests/test_channels_api.py`'s existing real-capture debug-console test to assert both fields on
every message (`1.0`/`False` — the real capture path never passes `display_name` to
`resolve_identity`, so the 0.6 display-name-match branch never fires for real capture; only manual
`set_manual_identity_override` corrections would produce a different value).

**7. `DecisionLogRow.detail` end to end** (`app/reports/schema.py`, `app/reports/composer.py`,
`app/ask/brief.py`). `AuditLog.detail` already had a real consumer (`app/ask/embed_worker.py`'s
`_audit_log_text`, for Ask's semantic search) — the actual gap was narrower: the Decision Log
report section and the Successor Brief never read it. Added `detail: dict` to `DecisionLogRow`,
populated from `a.detail` in both builders. **A fourth, real construction site the prompt's own
"three call sites" framing missed, found and fixed in the same pass rather than left to break at
runtime**: `app/ask/summarise.py`'s `_decision_log_row` (feeds `compose_period_digest`, Ask's
Period Digest) builds the same `DecisionLogRow` and would have raised a `ValidationError` the
moment `detail` became a required field with no default — fixed the same way, which also closes
this same gap for the Period Digest as a side effect. Tested: `tests/test_reports_api.py` drives a
real commitment correction through `POST .../verify` and asserts the same `{"changes": {...}}`
diff appears in `GET .../report/current`'s decision log; `tests/test_ask_brief.py` extends its
existing full-section test to assert the same diff reaches `compose_successor_brief`'s
`decision_history`.

`uv run pytest` green: **591 passing** (583 immediately prior to this round + 8 new tests across
these seven items, no regressions — items 3 and 4 in the prompt's own audit numbering share one
build step above, "3," per its own "share one natural fix location" note, hence seven build steps
covering eight named gaps).

## Blind Spots item 3, real UX follow-up: a document-scoped Activity endpoint (2026-08-17)

Validating the Blind Spots round above against the real product (not just `uv run pytest`) surfaced
a legitimate frontend gap on item 3's document half: `DocumentAuditLog` was reachable only through
`/admin/export`'s whole-project, org-admin-only bundle — correct per FR-ADM-10, but no business user
could see "what happened to this document" without downloading a JSON/CSV file and searching it by
hand. Decided (with the user) to add a real, human-readable Activity view scoped to one document
(`frontend/PROGRESS.md`'s matching entry has the frontend half and the full reasoning for that
scope choice over a project-wide activity page) — `WritebackAuditLog` deliberately left export-only
for this round, per that same decision.

**`GET /projects/{project_id}/documents/{document_id}/audit-log`** (`app/api/documents.py`) — new
`DocumentAuditLogOut` schema (`app/api/schemas.py`), same read tier as `/lineage` immediately above
it (`Depends(get_project)`, any project member — this is "my own document's history," not an admin
action). Filters to `document_id == this document`, which naturally excludes `project_archived` rows
(`document_id` NULL, a project-wide fact) — deliberately out of scope for a document-scoped view.
Most-recent-first.

**A real, honest limitation named rather than glossed over**: `document_created` and `version_created`
are written in the *same* upload transaction (`documents_service.create_document`), and Postgres's
`now()` is fixed for the whole transaction, not re-evaluated per statement — both rows land on the
identical `occurred_at`, so their relative order in a strict `ORDER BY occurred_at DESC` is genuinely
undefined (confirmed by a real test failure, not assumed: the first draft of the test below asserted
a strict total order and failed non-deterministically). Documented on the endpoint's own docstring;
the test asserts only what's actually guaranteed (the two genuinely-separate-transaction events sort
correctly; the same-transaction pair is asserted as a set, not an order).

**Tested for real** (`tests/test_documents_api.py`): a document taken through upload → approve →
tag returns four real rows with real `detail` values (`version_no`, `sharepoint_write_back`,
`changes`) in the guaranteed-correct partial order; a freshly-uploaded, untouched document never
returns empty (`document_created`/`version_created` always exist — a document with no history can't
exist).

`uv run pytest` green: **593 passing** (up from 591; 2 new tests, no regressions).

## Updating this file

When a milestone completes:
1. Flip its Status cell to `Done`, with the commit/date.
2. Note anything the next milestone's prompt should know that wasn't true when it was written
   (a design decision made mid-implementation, a scope adjustment, a discovered blocker).
3. Run `uv run pytest` from `backend/` one more time and confirm it's green before flipping the
   status — a milestone marked Done that doesn't pass its own tests is worse than one left
   `Not started`, since the next session will trust this table.
