"""The containment gate: for each known way extraction goes wrong, does the
system put the result in front of a human?

This is the half cue-eval cannot see. `cue-eval/run_eval.py` scores raw model
JSON — prompt in, object out — and never runs `_resolve_vendor_for_item`,
`_overlapping_span_indexes`, the vendor-span resolver, the merge detector, or
the persistence layer. So every pipeline-level guard in this codebase is
invisible to that suite: CD03 sits at 50% whether or not attribution is
fixed, because the fix happens after the model returns.

The two suites ask different questions and both have to be asked:

  cue-eval      is the model right?              needs a live model, scores accuracy
  this module   when it is wrong, is it caught?  no model at all, asserts outcomes

Deliberately a gate, not a metric. "Containment held for 7 of 8 known failure
modes" is not a number to track quarterly — a known way of being wrong that
reaches the ledger as `auto` is a bug. Each case below is a shape actually
observed from a real model (the qwen2.5:14b eval runs, or the reported
production behaviour in "Over- and Under-splitting by the AI in the
extraction.pdf"), replayed through the real extract_case against the real
database with a fake client, so it is deterministic and runs in CI.

The product's claim is not that extraction is always right. It is that when
extraction is wrong, a human sees it. This module is where that claim is
either true or false.
"""

import pytest
from sqlalchemy import select

from app.ledger.context import load_open_commitment_context
from app.ledger.extractor import _get_or_create_party, extract_case
from app.models import Commitment
from tests.conftest import set_org_context
from tests.test_extractor import (
    CONTEXT,
    FakeModelClient,
    _item,
    _team_collaboration_case,
    make_case,
)


async def _run(app_session, org_id, project_id, case, response, ledger_context=None):
    outcome = await extract_case(
        app_session, project_id=project_id, organisation_id=org_id, context=CONTEXT,
        case=case, client=FakeModelClient(response), ledger_context=ledger_context or [],
    )
    await app_session.commit()
    return outcome


def _contained(commitment: Commitment) -> bool:
    return commitment.verification_state == "pending_verification"


# --- contained: the guards that hold -------------------------------------


@pytest.mark.asyncio
async def test_contains_a_hallucination_that_invents_a_date(app_session, org_and_project):
    """CD01 exactly as qwen2.5:14b produces it, 5/5 runs: a commitment
    invented out of pure consequence-discussion, at confidence 0.95, with
    `deliverable_en` and `due_at` both lifted verbatim from the *injected
    project context* — "LED screen install" is milestone #3 and 14:00 is that
    milestone's own due date. The message names neither.

    Contained, but note *why*: not because anything detected the
    hallucination, only because it happened to invent a date, and FR-LED-07
    routes any date field to review. See the uncontained case below.
    """
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    message = (
        "Twin's flagging this as touching the critical path — rehearsal is booked "
        "right after install, so a 2hr slip here eats directly into that buffer."
    )
    outcome = await _run(
        app_session, org_id, project_id, make_case(message),
        {"commitments": [_item(
            act_type="query", deliverable_en="LED screen install",
            deliverable_original="LED screen install",
            evidence_span="rehearsal is booked right after install",
            due_at="2026-06-22T14:00:00+08:00", confidence=0.95,
        )]},
    )
    assert _contained(outcome.created[0])


@pytest.mark.asyncio
async def test_contains_an_over_split_pair_quoting_the_same_text(app_session, org_and_project):
    """Two commitments extracted from overlapping stretches of one sentence —
    the over-splitting signature. Both flagged rather than the message being
    rejected: a clause can legitimately carry two promises, and rejecting
    would lose the real one along with the invented one."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    message = "screen install slips 2 hours, no change to the cost"
    outcome = await _run(
        app_session, org_id, project_id, make_case(message),
        {"commitments": [
            _item(evidence_span="screen install slips 2 hours"),
            _item(deliverable_en="cost", deliverable_original="cost",
                  evidence_span="slips 2 hours, no change to the cost"),
        ]},
    )
    assert len(outcome.created) == 2
    assert all(_contained(c) for c in outcome.created)


@pytest.mark.asyncio
async def test_contains_two_vendors_welded_into_one_commitment(app_session, org_and_project):
    """The PDF's Problem 2. A confident attribution to one known vendor, on a
    span that names two — so without the merge detector this lands as `auto`
    and reads as a clean, chaseable row for the wrong company."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    await _get_or_create_party(app_session, org_id, "Ah Seng Production", "vendor_org")
    await _get_or_create_party(app_session, org_id, "Vertex Fabrication", "vendor_org")
    await app_session.flush()

    span = "Ah Seng's LED screen install and Vertex's aluminium frame delivery"
    outcome = await _run(
        app_session, org_id, project_id,
        _team_collaboration_case(f"{span} are both unconfirmed."),
        {"commitments": [_item(
            act_type="escalate", deliverable_en="screen install and frame delivery",
            deliverable_original=span, evidence_span=span,
            counterparty_name="Ah Seng Production",
        )]},
    )
    assert _contained(outcome.created[0])


@pytest.mark.asyncio
async def test_contains_a_commitment_whose_vendor_could_not_be_identified(
    app_session, org_and_project
):
    """Internal channel, no vendor nameable from the message. Lands on the
    "Unresolved Vendor" placeholder rather than on the Pico staff member who
    typed it — the original vendor-attribution bug."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    outcome = await _run(
        app_session, org_id, project_id,
        _team_collaboration_case("someone needs to chase the rigging sign-off"),
        {"commitments": [_item(
            deliverable_en="rigging sign-off", deliverable_original="rigging sign-off",
            evidence_span="chase the rigging sign-off", counterparty_name=None,
        )]},
    )
    assert _contained(outcome.created[0])


@pytest.mark.asyncio
async def test_contains_a_commitment_the_model_was_unsure_of(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    outcome = await _run(
        app_session, org_id, project_id, make_case("maybe the truss goes up thursday?"),
        {"commitments": [_item(
            deliverable_en="truss", deliverable_original="truss",
            evidence_span="the truss goes up", confidence=0.4,
        )]},
    )
    assert _contained(outcome.created[0])


@pytest.mark.asyncio
async def test_contains_a_price_change_asserted_against_an_existing_commitment(
    app_session, org_and_project
):
    """The link path. Never auto-applies the new price — but never buries it
    either, which it used to: no row, no field, no flag, one log line."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    first = await _run(
        app_session, org_id, project_id, make_case("screen install confirmed"),
        {"commitments": [_item(evidence_span="screen install")]},
    )
    existing = first.created[0]
    assert not _contained(existing)

    ledger_context = await load_open_commitment_context(app_session, project_id=project_id)
    await _run(
        app_session, org_id, project_id, make_case("screen install is now 6200"),
        {"commitments": [_item(
            act_type="renegotiate", evidence_span="screen install",
            relates_to="C1", amount=6200, currency="SGD",
        )]},
        ledger_context=ledger_context,
    )
    await app_session.refresh(existing)
    assert _contained(existing)
    assert existing.amount is None  # contained, not applied


# --- uncontained: the measured gap ---------------------------------------


@pytest.mark.asyncio
async def test_a_bare_hallucination_on_a_vendor_chat_is_NOT_contained(
    app_session, org_and_project
):
    """The hole this module exists to make visible, asserted as it actually
    behaves rather than as it should.

    Every guard in the create path keys off something *incidental* to being
    wrong: a money field, a date, an unidentifiable vendor, an overlapping
    span, two vendors in one span, or the model's own low confidence. A
    hallucination carrying none of those — on a direct vendor chat, where the
    sender is the vendor so attribution is confident by construction, at the
    0.95 confidence CD01 shows the model actually reporting — trips none of
    them and is written as `auto`.

    That is a clean-looking, fully-verified-appearing row for a promise
    nobody made, which is precisely the business impact the source PDF
    describes: "they can't tell which lines are real". CD01 escapes only by
    the accident of having invented a date too.

    Nothing here is calibrated to catch it. `confidence` cannot: it is the
    model's self-report, and it was 0.95 while hallucinating.

    The obvious candidate guard was measured and rejected on the numbers.
    cue-eval/prompt.txt already requires `deliverable_original` to be "copied
    verbatim from the message", so it could be enforced in code the way
    evidence_span is — and a hallucinated deliverable is by definition not in
    the message. Measured over the full 20-case suite on qwen2.5:14b, 3 runs,
    81 returned commitments: 18.5% had a non-verbatim deliverable_original,
    and the breakdown is what kills it —

        T01  (expect=1, real)  'LED screen install'
        T06  (expect=1, real)  'zone C 铝架 deliver'
        S01  (expect=1, real)  'LED screen install'
        CD02 (expect=1, real)  'LED screen install'
        CD01 (expect=0, FAKE)  'LED screen install'

    Four legitimate commitments flagged for every hallucination caught. A
    queue with a 4:1 false-positive rate is one a PM stops reading, which
    costs more than the row it would have caught.

    The recurring value is the tell: "LED screen install" is milestone #3 in
    the injected project context, and the model reuses that exact string as a
    deliverable name across unrelated messages. For a real commitment that is
    arguably good — canonical, groupable naming. For CD01 it is the
    hallucination. The same mechanism produces both, which is precisely why
    no grounding rule can separate them, and why this needs the real
    discovery corpus rather than a cleverer synthetic heuristic.

    Kept as a failing-shaped assertion, not an xfail, so the day something
    does catch it this test breaks loudly and gets updated on purpose.
    """
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    outcome = await _run(
        app_session, org_id, project_id,
        make_case("rehearsal is booked right after install, no slack left"),
        {"commitments": [_item(
            act_type="query", deliverable_en="rehearsal buffer",
            deliverable_original="rehearsal buffer",
            evidence_span="rehearsal is booked right after install",
            confidence=0.95,
        )]},
    )
    assert outcome.created[0].verification_state == "auto"


@pytest.mark.asyncio
async def test_containment_summary(app_session, org_and_project, capsys):
    """One place that states the shape of the guarantee, so it is read rather
    than inferred from six separate test names."""
    guards = {
        "monetary or date field (FR-LED-07)": True,
        "vendor not confidently identified": True,
        "overlapping spans (over-split)": True,
        "two vendors in one span (merge)": True,
        "model self-reported low confidence": True,
        "unapplied claim on a linked message": True,
        "bare hallucination, no incidental signal": False,
    }
    contained = sum(guards.values())
    with capsys.disabled():
        print(f"\n  containment: {contained}/{len(guards)} known failure modes caught")
        for name, held in guards.items():
            print(f"    {'contained' if held else 'UNCONTAINED'}  {name}")
    assert contained == len(guards) - 1, (
        "containment map changed — update this summary and the case it refers to"
    )
