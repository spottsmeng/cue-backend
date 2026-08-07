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
| M2 | Governance completion | `Prompt 5 — Governance completion.txt` | §15 Phase 9 (remainder) | Identity/RBAC (done) | Not started |
| M3 | Documents | `Prompt 6 — Documents.txt` | §15 Phase 8 | — | Not started |
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
