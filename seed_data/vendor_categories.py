"""FR-VRG-02's "segment by vendor category" needs a `vendor_category`
vocabulary to segment against — `app/models/ontology.py`'s own comment has
named this category since the very first migration, but (per that session's
own note, carried forward through every session since, most recently
app/reports/schema.py's VendorStatusRow docstring) nothing has ever seeded
or populated it. The Vendor Reliability Graph (Prompt 10) is the first
session with an actual consumer for it.

Seeded vertical-scoped (vertical_id = event-production, organisation_id
NULL) from the start, not universal-core — same reasoning
seed_data/deviation_classes.py already gives for deviation_class, and the
same mistake milestone_type had to walk back later (eed5a4da79f6) once a
second vertical became real roadmap. Vendor categories ("AV supplier",
"structure contractor") are exactly as event-production-specific as
deviation classes; there is no reason to repeat the shortcut here.

Codes drawn from the vendor types CUE-PRD.md's own examples and the Living
WIP report's vendor status section actually name (LED/AV, booth structure,
F&B, floral, staffing, logistics) plus a general fallback for anything that
doesn't fit those — kept to a small, defensible set rather than an
exhaustive taxonomy invented for its own sake.
"""

# (code, label_en, label_zh)
VENDOR_CATEGORIES: list[tuple[str, str, str]] = [
    ("av_led", "AV / LED", "视听/LED"),
    ("booth_structure", "Booth & structure", "展位搭建"),
    ("fnb", "F&B", "餐饮"),
    ("floral", "Floral & decor", "花艺陈设"),
    ("staffing", "Staffing", "人员派遣"),
    ("logistics", "Logistics & freight", "物流货运"),
    ("printing_signage", "Printing & signage", "印刷标识"),
    ("venue", "Venue", "场地"),
    ("other", "Other", "其他"),
]
