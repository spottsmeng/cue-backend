"""FR-RPT-07: PDF export via Playwright print-to-PDF over the server-rendered
HTML from app/reports/export_html.py — the same composition
(app/reports/render.py's ReportRenderModel) the PPTX exporter reads, per
WHAT TO BUILD #3's "one composition path feeding both export formats."

A fresh Chromium instance per export (no long-lived browser/context pooled
across requests) — simplest correct thing at this session's data scale
(NFR-PRF-05's 30-second target; a cold Chromium launch is well under a
second), and avoids a shared-mutable-browser-instance lifecycle concern a
FastAPI app with no explicit startup/shutdown hook for it would otherwise
have to manage.
"""

from playwright.async_api import async_playwright

from app.reports.render import ReportRenderModel
from app.reports.export_html import render_html


async def render_pdf(model: ReportRenderModel) -> bytes:
    html = render_html(model)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="load")
            return await page.pdf(format="A4", print_background=True, margin={
                "top": "20px", "bottom": "20px", "left": "20px", "right": "20px",
            })
        finally:
            await browser.close()
