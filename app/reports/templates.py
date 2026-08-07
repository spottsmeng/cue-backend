# FR-RPT-07/10: "consistent Pico corporate branding" and "templates per
# client where a client mandates its own format." Real Pico branding assets
# are not available (Prompt 8's own EXPLICITLY OUT OF SCOPE section) — every
# entry here is a clearly-marked placeholder, not a fabricated Pico logo or
# client template. Swapping in a real template (Pico's own, or a specific
# client's mandated format) is adding a new dict entry here plus, for a real
# logo, replacing PLACEHOLDER_LOGO_TEXT's spot in app/reports/render.py /
# export_pptx.py with an actual image asset — the seam these two modules
# already read `resolve_template()` through, not a rewrite.

DEFAULT_TEMPLATE_CODE = "cue_placeholder"

REPORT_TEMPLATES: dict[str, dict] = {
    "cue_placeholder": {
        "name": "CUE placeholder branding",
        "primary_color": "1F2A44",  # hex, no '#' — python-pptx's RGBColor wants it bare
        "accent_color": "2E7D6B",
        "text_color": "1A1A1A",
        # Real template swap point: replace this with an actual Pico (or
        # named client) wordmark/logo image once one is available.
        "logo_text": "[PLACEHOLDER LOGO — REAL PICO BRANDING NOT YET AVAILABLE]",
    },
}


def resolve_template(template_code: str | None) -> dict:
    """Falls back to the placeholder default for an unknown/omitted code —
    same "config, not a resolver that ever hard-fails on a missing template"
    posture app/foresight/threshold.py's DEFAULT_THRESHOLDS establishes,
    rather than 404ing an export over a cosmetic lookup miss."""
    return REPORT_TEMPLATES.get(template_code or DEFAULT_TEMPLATE_CODE, REPORT_TEMPLATES[DEFAULT_TEMPLATE_CODE])
