"""FR-TWN-09's expert-seeded default for the `event-production` vertical —
converted directly from the Challenge Brief's own Annex A (pages 8-9, read
directly, not paraphrased from CUE-PRD.md's summary of it): the Project
Timeline (Design and Layout / Content & Media / Misc) and the Operation
Schedule & Overtime table.

This is content, reviewed and edited like any other reference data — not
migration plumbing. A migration imports `ARCHETYPE_ITEMS` /
`ARCHETYPE_DEPENDENCIES` and inserts them; it does not itself decide what the
timeline looks like. Editing this file changes what a *new* project seeds
from; it never reaches back into an already-materialized project's own
Milestone/Dependency rows (those are a literal copy, made once, at creation
time — see app/twin/models.py's MilestoneArchetype docstring).

`day_offset` is days relative to the project's event_start (day 0 = doors).
Anchored to real Annex A dates:
  fnb_confirmation           30 Mar  -> -86  (independently earliest, per
                                               CUE-PRD.md §4.2's own note)
  design_approval             30 Apr -> -55  (booth design layout confirmation)
  artwork_submission          14 May -> -41  (graphics artwork/photos + new
                                               co-exhibitor logo)
  test_print                  31 May -> -25  (graphics test print approval)
  content_approval            19 Jun ->  -6  (final video assets/sequence
                                               approved to load into screen)
  contractor_move_in          22 Jun ->  -3
  exhibitor_check_in          23 Jun ->  -2
  exhibits_ready       23 Jun 22:00 ->  -1  (rounded to the day before doors)
  doors                        24 Jun ->   0  (exhibition opens — FIXED)
  strike / crate_collection    26 Jun ->  +2  (teardown period)

Not in Annex A, interpolated to a defensible slot in the same sequence
(permit issuance, shop drawing confirmation, rigging, install, content load,
rehearsal, load-in) — flagged here rather than silently blended in with the
real anchors above, per CLAUDE.md's evidence discipline applied to this
file's own reasoning, not just extracted commitments.

`ARCHETYPE_DEPENDENCIES` is a real (if, honestly, still linear) dependency
graph — edges by type_code, not by list position, so reordering
ARCHETYPE_ITEMS doesn't silently rewire the graph. Annex A gives us an
ordered schedule, not a branching dependency graph with named predecessors
per step, so a linear chain is what's honestly sourced from it; the *shape*
of this data (explicit upstream/downstream/lag_days edges, the same triple
the live `Dependency` model uses) is what makes a future archetype with real
parallel tracks (e.g. a gala's F&B/staging/AV running concurrently) an
edits-only change, not a schema change.
"""

ARCHETYPE_CODE = "event-production-default"
ARCHETYPE_NAME = "Event production — default (Annex A)"

# (type_code, name, day_offset, is_fixed)
ARCHETYPE_ITEMS: list[tuple[str, str, int, bool]] = [
    ("fnb_confirmation", "F&B confirmation", -86, False),
    ("design_approval", "Booth design layout confirmation", -55, False),
    ("shop_drawing_confirmation", "Shop drawing confirmation", -48, False),  # interpolated
    ("artwork_submission", "Graphics artwork and co-exhibitor logo submission", -41, False),
    ("test_print", "Graphics test print approval", -25, False),
    ("permit_issuance", "Permit issuance", -10, False),  # interpolated
    ("content_approval", "Final video assets and sequence approved", -6, False),
    ("contractor_move_in", "Nominated stand contractors move-in", -3, False),
    ("load_in", "Exhibits move in", -3, False),
    ("rigging", "Rigging", -3, False),  # interpolated, concurrent with move-in window
    ("install", "Booth and graphics install", -2, False),  # interpolated
    ("exhibitor_check_in", "Exhibitor check-in and badge collection", -2, False),
    ("content_load", "Content load into screens", -2, False),  # interpolated
    ("exhibits_ready", "All exhibits ready for display", -1, False),
    ("rehearsal", "Rehearsal", -1, False),  # interpolated
    ("doors", "Exhibition opens", 0, True),
    ("crate_collection", "Forwarder delivers/collects crates", 2, False),
    ("strike", "Booth dismantling", 2, False),
]

# (upstream_type_code, downstream_type_code, lag_days) — lag_days is each
# consecutive pair's exact day_offset gap above; stored explicitly rather
# than derived at migration time, since a future non-linear archetype's
# edges won't all be "the next item in the list" and its lag won't always be
# a plain offset subtraction once branches exist.
ARCHETYPE_DEPENDENCIES: list[tuple[str, str, int]] = [
    ("fnb_confirmation", "design_approval", 31),
    ("design_approval", "shop_drawing_confirmation", 7),
    ("shop_drawing_confirmation", "artwork_submission", 7),
    ("artwork_submission", "test_print", 16),
    ("test_print", "permit_issuance", 15),
    ("permit_issuance", "content_approval", 4),
    ("content_approval", "contractor_move_in", 3),
    ("contractor_move_in", "load_in", 0),
    ("load_in", "rigging", 0),
    ("rigging", "install", 1),
    ("install", "exhibitor_check_in", 0),
    ("exhibitor_check_in", "content_load", 0),
    ("content_load", "exhibits_ready", 1),
    ("exhibits_ready", "rehearsal", 0),
    ("rehearsal", "doors", 1),
    ("doors", "crate_collection", 2),
    ("crate_collection", "strike", 0),
]
