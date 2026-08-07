"""Server-rendered HTML for the same ReportRenderModel export_pptx.py
consumes (app/reports/render.py's own docstring: one composition path,
two consumers) — this module's only output feeds app/reports/export_pdf.py's
Playwright print-to-PDF, never rendered directly to a browser client, so
there is no client-side JS here at all, just markup and inline CSS.

Every value that ultimately comes from vendor chat content (a deliverable
name, a risk's downstream consequence) is escaped via `html.escape` before
being placed in the page — this is genuinely untrusted text by the time it
reaches here (CLAUDE.md's own "verified in code, not trusted" posture,
applied to XSS the same way evidence_span verification applies to
provenance), even though the immediate consumer is a headless browser
producing a PDF, not an interactive page a user could be scripted against.
"""

from html import escape

from app.reports.render import ReportRenderModel, RenderSection, RenderTable

# CSS is full of literal `{`/`}` braces, which collide with str.format's own
# placeholder syntax — __PRIMARY__/__ACCENT__ are substituted via a plain
# str.replace below instead, so every other brace in this block can stay
# ordinary CSS.
_CSS = """
body { font-family: 'Helvetica Neue', Arial, sans-serif; color: #1A1A1A; margin: 40px; }
h1 { color: __PRIMARY__; margin-bottom: 0; }
.subtitle { color: #666; margin-top: 4px; }
.logo-placeholder {
  display: inline-block; padding: 6px 12px; border: 1px dashed __ACCENT__;
  color: __ACCENT__; font-size: 12px; margin-bottom: 16px;
}
section { margin-top: 32px; page-break-inside: avoid; }
h2 { color: __PRIMARY__; border-bottom: 2px solid __ACCENT__; padding-bottom: 4px; }
table { border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 13px; }
th, td { border: 1px solid #ddd; padding: 6px 8px; text-align: left; }
th { background: __PRIMARY__; color: #fff; }
.facts { width: 100%; margin-top: 8px; font-size: 14px; }
.facts td:first-child { font-weight: 600; width: 220px; }
.note { font-style: italic; color: #666; margin-top: 8px; }
.images { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 12px; }
.images figure { margin: 0; width: 220px; }
.images img { max-width: 100%; border: 1px solid #ddd; }
.images figcaption { font-size: 12px; color: #444; }
"""


def _table_html(table: RenderTable) -> str:
    headers = "".join(f"<th>{escape(h)}</th>" for h in table.headers)
    rows = "".join(
        "<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in row) + "</tr>"
        for row in table.rows
    ) or f'<tr><td colspan="{len(table.headers)}">No data</td></tr>'
    return f"<h3>{escape(table.title)}</h3><table><thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table>"


def _section_html(section: RenderSection) -> str:
    parts = [f"<h2>{escape(section.heading)}</h2>"]
    if section.facts:
        rows = "".join(
            f"<tr><td>{escape(k)}</td><td>{escape(v)}</td></tr>" for k, v in section.facts
        )
        parts.append(f"<table class='facts'><tbody>{rows}</tbody></table>")
    if section.images:
        figures = "".join(
            f"<figure><img src='{escape(url)}' /><figcaption>{escape(caption)}</figcaption></figure>"
            for caption, url in section.images
        )
        parts.append(f"<div class='images'>{figures}</div>")
    for table in section.tables:
        parts.append(_table_html(table))
    if section.note:
        parts.append(f"<p class='note'>{escape(section.note)}</p>")
    return f"<section>{''.join(parts)}</section>"


def render_html(model: ReportRenderModel) -> str:
    css = _CSS.replace("__PRIMARY__", f"#{model.template['primary_color']}").replace(
        "__ACCENT__", f"#{model.template['accent_color']}"
    )
    sections_html = "".join(_section_html(s) for s in model.sections)
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>{escape(model.project_name)} — Living WIP Report</title>
<style>{css}</style>
</head>
<body>
<div class="logo-placeholder">{escape(model.template['logo_text'])}</div>
<h1>{escape(model.project_name)}</h1>
<p class="subtitle">Living WIP report — generated {escape(model.generated_at)}</p>
{sections_html}
</body>
</html>"""
