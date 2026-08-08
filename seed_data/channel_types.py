"""Platform-shipped channel_types rows (organisation_id NULL). Codes reuse
CUE-PRD.md §9.1/§9.2's two real, non-negotiable mechanisms (whatsapp,
wechat) plus the FOSS substitutes actually stood up for the Microsoft-
equivalent capabilities before Pico credentials exist (mattermost,
imap_smtp, nextcloud) and the reserved codes for the real Graph adapters
once those credentials arrive (teams, outlook, sharepoint) — activating
Graph later is a channel_types/credentials configuration change, not a
migration, per CUE-PRD.md §9.3a. `manual` keeps FR-LED-10's non-integration
provenance value, capability=None — it never has a live adapter."""

CHANNEL_TYPES: list[tuple[str, str | None]] = [
    ("whatsapp", "external_vendor_chat"),
    ("wechat", "external_vendor_chat"),
    ("teams", "team_collaboration"),
    ("outlook", "email"),
    ("sharepoint", "file_storage"),
    ("manual", None),
    ("mattermost", "team_collaboration"),
    ("imap_smtp", "email"),
    ("nextcloud", "file_storage"),
]
