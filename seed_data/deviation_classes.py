"""FR-DEV-01's "dedicated structured fields for scope changes, delays and
corrective actions" needs a `deviation_class` vocabulary to classify against
— CUE-PRD.md §4.2's ontology table names the category but (per
b32b62ca45e4's own comment) explicitly left it unseeded: "the
construction-adjacent deviation_class terms are a separate seed, not done
here." Foresight (Prompt 7) is that separate seed.

Seeded vertical-scoped (vertical_id = event-production, organisation_id
NULL) from the start, not universal-core — unlike commitment_act/
milestone_type's original v1 shortcut (83b72f2a9175's own comment: "seeded
universal core ... even though CUE-PRD.md §4.2.1's layering table places
this in the vertical pack"), which milestone_type later had to walk back via
a re-key migration (eed5a4da79f6) once a second vertical became a real
near-term roadmap item (see backend/PROGRESS.md's M1 notes). b32b62ca45e4's
own comment already flags these specifically as "construction-adjacent" —
the same collision risk that motivated that re-key is foreseeable here on
day one, so there is no reason to repeat the shortcut just to re-key it
again later.

Codes drawn from CUE-PRD.md §4.1's DEVIATION entity ("scope change | delay |
corrective action") plus the two failure modes Silence Radar and the
lifecycle state machine actually produce (silence and a broken commitment
are deviations from plan too, not just PM-logged ones) — kept to a small,
defensible set rather than an exhaustive taxonomy invented for its own sake.
"""

# (code, label_en, label_zh)
DEVIATION_CLASSES: list[tuple[str, str, str]] = [
    ("scope_change", "Scope change", "范围变更"),
    ("delay", "Delay", "延误"),
    ("corrective_action", "Corrective action", "纠正措施"),
    ("silence", "Vendor silence", "供应商失联"),
    ("spec_drift", "Spec drift / contradiction", "规格偏差"),
    ("broken_commitment", "Broken commitment", "承诺失败"),
]
