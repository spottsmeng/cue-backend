"""WHAT TO BUILD #3: "one composition path feeding both export formats, not
two." This module is that one path — it normalises a composed
LivingWipReportOut (app/reports/schema.py) into a presentation-agnostic
`ReportRenderModel` of plain section/table/row data. app/reports/
export_pptx.py and export_html.py both consume this same model and never
read LivingWipReportOut directly, so a change to how a figure is
presented (a new column, a reordered section) happens once, here — neither
exporter needs a matching change to stay in sync with the other.
"""

from dataclasses import dataclass, field

from app.reports.schema import LivingWipReportOut, ReportField


@dataclass
class RenderTable:
    title: str
    headers: list[str]
    rows: list[list[str]]


@dataclass
class RenderSection:
    heading: str
    facts: list[tuple[str, str]] = field(default_factory=list)
    tables: list[RenderTable] = field(default_factory=list)
    images: list[tuple[str, str]] = field(default_factory=list)  # (caption, url)
    note: str | None = None


@dataclass
class ReportRenderModel:
    project_name: str
    generated_at: str
    template: dict
    sections: list[RenderSection]


def _field_str(f: ReportField, *, currency: str | None = None) -> str:
    if not f.available:
        return f"— ({f.unavailable_reason})"
    value = f.value
    if currency is not None and isinstance(value, (int, float)):
        value = f"{currency} {value:,.2f}"
    flag = ""
    if f.verification_state == "pending_verification":
        flag = "  [UNVERIFIED]"
    elif f.verification_state == "human_corrected":
        flag = "  [corrected]"
    return f"{value}{flag}"


def _dt(value) -> str:
    return value.isoformat() if value is not None else "—"


def build_render_model(report: LivingWipReportOut, template: dict) -> ReportRenderModel:
    overview = report.project_overview
    overview_section = RenderSection(
        heading="1. Project Overview",
        facts=[
            ("Project", _field_str(overview.project_name)),
            ("Client", _field_str(overview.client_name)),
            ("Venue", _field_str(overview.venue)),
            ("Event start", _field_str(overview.event_start)),
            ("Event end", _field_str(overview.event_end)),
            ("Current phase", _field_str(overview.current_phase)),
        ],
        images=[
            (ref.deliverable_name, ref.image_url)
            for ref in overview.visual_references
            if ref.available and ref.image_url
        ],
        note=(
            f"{overview.deliverables_without_visual_reference} deliverable(s) have no visual "
            "reference on file yet."
            if overview.deliverables_without_visual_reference
            else None
        ),
    )

    milestone_section = RenderSection(
        heading="2. Milestone Tracker",
        tables=[
            RenderTable(
                title="Key milestones",
                headers=["Milestone", "Planned", "Actual", "Status", "Slack (days)"],
                rows=[
                    [
                        row.name,
                        _dt(row.planned_at),
                        _dt(row.actual_at),
                        row.status.replace("_", " "),
                        f"{row.slack_days:.1f}" if row.slack_days is not None else "—",
                    ]
                    for row in report.milestone_tracker
                ],
            )
        ],
    )

    vendor_section = RenderSection(
        heading="3. Vendor Status Summary",
        tables=[
            RenderTable(
                title="Vendors",
                headers=["Vendor", "Outstanding Actions", "Confirmed Deliverables", "Reliability"],
                rows=[
                    [
                        v.party_name,
                        "; ".join(c.deliverable_en for c in v.outstanding_actions) or "—",
                        "; ".join(c.deliverable_en for c in v.confirmed_deliverables) or "—",
                        _field_str(v.reliability),
                    ]
                    for v in report.vendor_status.vendors
                ],
            )
        ],
    )

    budget = report.budget_summary
    budget_section = RenderSection(
        heading="4. Budget Summary",
        facts=[
            ("Approved budget", _field_str(budget.approved_budget, currency=budget.currency)),
            ("Committed spend", _field_str(budget.committed_spend, currency=budget.currency)),
            ("Outstanding payments", _field_str(budget.outstanding_payments, currency=budget.currency)),
            ("Variance", _field_str(budget.variance, currency=budget.currency)),
        ],
    )

    risk_section = RenderSection(
        heading="5. Risk and Issues Log",
        tables=[
            RenderTable(
                title="Risks",
                headers=["Severity", "Status", "Source", "Downstream consequence", "Base rate"],
                rows=[
                    [
                        r.severity,
                        r.status,
                        r.source,
                        r.downstream_consequence,
                        f"{r.base_rate:.0%}" if r.base_rate is not None else "—",
                    ]
                    for r in report.risk_and_issues.risks
                ],
            ),
            RenderTable(
                title="Deviations",
                headers=["Class", "Status", "Description"],
                rows=[
                    [d.class_code or "—", d.status, d.description_en]
                    for d in report.risk_and_issues.deviations
                ],
            ),
        ],
    )

    decisions = report.decision_and_approval_log
    decision_section = RenderSection(
        heading="6. Decision and Approval Log",
        tables=[
            RenderTable(
                title="Decisions",
                headers=["When", "Action", "From → To"],
                rows=[
                    [
                        _dt(d.occurred_at),
                        d.action.replace("_", " "),
                        f"{d.from_state or '—'} → {d.to_state or '—'}",
                    ]
                    for d in decisions.decisions
                ],
            ),
            RenderTable(
                title="Outstanding approvals",
                headers=["Deliverable", "Due"],
                rows=[[a.deliverable_en, _dt(a.due_at)] for a in decisions.outstanding_approvals],
            ),
        ],
    )

    next_steps = report.next_steps
    next_steps_section = RenderSection(
        heading="7. Next Steps",
        tables=[
            RenderTable(
                title="Open commitments",
                headers=["Deliverable", "State", "Due"],
                rows=[[c.deliverable_en, c.state, _dt(c.due_at)] for c in next_steps.open_commitments],
            ),
            RenderTable(
                title="Open milestones",
                headers=["Milestone", "Planned"],
                rows=[[m.name, _dt(m.planned_at)] for m in next_steps.open_milestones],
            ),
        ],
    )

    return ReportRenderModel(
        project_name=overview.project_name.value or "Untitled project",
        generated_at=report.generated_at.isoformat(),
        template=template,
        sections=[
            overview_section,
            milestone_section,
            vendor_section,
            budget_section,
            risk_section,
            decision_section,
            next_steps_section,
        ],
    )
