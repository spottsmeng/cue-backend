"""app/ledger/context.py — the already-logged-commitments lookup that
extraction is given before it reads a new message, and app/ledger/schema.py's
per-call narrowing of `relates_to` to the refs that lookup actually returned.

This is the half of the over-splitting fix that no prompt wording could
supply: the reason a message like "can we get written confirmation the 4pm
start still lands?" was read as a new promise is that the fact making it a
restatement lives in an earlier message the model was never shown.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.ledger.context import (
    LedgerContextItem,
    load_open_commitment_context,
    relates_to_refs,
    render_ledger_context,
    resolve_ref,
)
from app.ledger.schema import build_extraction_json_schema
from app.models import Commitment, OntologyTerm, Party
from tests.conftest import set_org_context


async def _make_commitment(
    session, org_id, project_id, *, name, state="proposed",
    vendor="Ah Seng Production", created_at=None,
):
    party = (
        await session.execute(
            select(Party).where(Party.organisation_id == org_id, Party.display_name == vendor)
        )
    ).scalar_one_or_none()
    if party is None:
        party = Party(organisation_id=org_id, display_name=vendor, type="vendor_org")
        session.add(party)
        await session.flush()
    internal = Party(organisation_id=org_id, display_name=f"internal {name}", type="internal_staff")
    session.add(internal)
    act = (
        await session.execute(
            select(OntologyTerm).where(
                OntologyTerm.category == "commitment_act", OntologyTerm.code == "commit"
            )
        )
    ).scalars().first()
    await session.flush()
    commitment = Commitment(
        project_id=project_id, party_id=party.id, counterparty_id=internal.id,
        act_type_id=act.id, state=state, deliverable_en=name, deliverable_original=name,
        confidence=0.9, field_confidence={},
    )
    if created_at is not None:
        commitment.created_at = created_at
    session.add(commitment)
    await session.flush()
    return commitment


@pytest.mark.asyncio
async def test_withdrawn_commitments_are_never_offered_back_to_the_model(
    app_session, org_and_project
):
    """"withdrawn" is the one state meaning "a human took this off the
    ledger" — offering it back invites re-opening something already retired."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    await _make_commitment(app_session, org_id, project_id, name="live one")
    await _make_commitment(app_session, org_id, project_id, name="retired one", state="withdrawn")

    items = await load_open_commitment_context(app_session, project_id=project_id)
    assert [i.deliverable_en for i in items] == ["live one"]


@pytest.mark.asyncio
async def test_pinned_commitment_survives_the_recency_window(app_session, org_and_project):
    """app/capture/pipeline.py pins the commitment a write-back reply was just
    matched to. Without the pin, a busy project pushes it out of the window and
    extraction re-reads the same reply as a brand-new promise — the duplicate
    this pin exists to prevent."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    oldest = await _make_commitment(
        app_session, org_id, project_id, name="the pinned one", created_at=base
    )
    for i in range(5):
        await _make_commitment(
            app_session, org_id, project_id, name=f"newer {i}",
            created_at=base + timedelta(hours=i + 1),
        )

    unpinned = await load_open_commitment_context(app_session, project_id=project_id, limit=3)
    assert oldest.id not in [i.commitment_id for i in unpinned]

    pinned = await load_open_commitment_context(
        app_session, project_id=project_id, limit=3, pinned_commitment_ids=(oldest.id,)
    )
    assert oldest.id in [i.commitment_id for i in pinned]
    # Refs must still be dense and unique after the pin is spliced in, or the
    # schema enum and the rendered list disagree about what "C2" means.
    refs = relates_to_refs(pinned)
    assert refs == [f"C{n}" for n in range(1, len(pinned) + 1)]


@pytest.mark.asyncio
async def test_another_projects_commitments_are_never_in_context(app_session, org_and_project):
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    await _make_commitment(app_session, org_id, project_id, name="ours")

    from app.models import Project
    ours = (
        await app_session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one()
    other = Project(
        organisation_id=org_id, vertical_id=ours.vertical_id,
        name="Other project", timezone="Asia/Singapore",
    )
    app_session.add(other)
    await app_session.flush()
    await _make_commitment(app_session, org_id, other.id, name="theirs")

    items = await load_open_commitment_context(app_session, project_id=project_id)
    assert [i.deliverable_en for i in items] == ["ours"]


def test_empty_context_renders_explicitly_not_as_a_blank():
    """A bare blank under a heading reads to a model as a truncated prompt,
    and an empty ledger is the common case on a new project."""
    assert "none" in render_ledger_context([])


def test_relates_to_enum_is_closed_to_what_was_actually_offered():
    """CLAUDE.md: "Enforce, don't ask." The set of commitments the model may
    point at is known at call time, so it belongs in the schema the decoder is
    constrained by — not in a sentence asking it to only use listed refs."""
    schema = build_extraction_json_schema(["C1", "C2"])
    enum = schema["properties"]["commitments"]["items"]["properties"]["relates_to"]["enum"]
    assert enum == [None, "C1", "C2"]

    # No context at all: the field becomes structurally impossible to fill,
    # rather than left open for the model to invent a plausible "C1".
    empty = build_extraction_json_schema([])
    assert empty["properties"]["commitments"]["items"]["properties"]["relates_to"]["enum"] == [None]

    # Narrowing is per-call and must never be written back to the shared dict.
    from app.ledger.schema import load_extraction_json_schema
    on_disk = load_extraction_json_schema()
    assert "enum" not in on_disk["properties"]["commitments"]["items"]["properties"]["relates_to"]


def test_resolve_ref_rejects_a_ref_that_was_never_offered():
    assert resolve_ref([], "C1") is None
    assert resolve_ref([], None) is None


def test_eval_harness_renders_ledger_context_identically_to_production():
    """cue-eval/run_eval.py hand-duplicates render_ledger_context because it
    is deliberately stdlib-only (no app package, no database). The whole value
    of that harness rests on it sending what production sends, so the
    duplication is pinned here rather than left to a comment: if either
    renderer changes, this fails.
    """
    import importlib.util
    from pathlib import Path

    eval_path = Path(__file__).resolve().parents[1] / "cue-eval" / "run_eval.py"
    spec = importlib.util.spec_from_file_location("cue_eval_run_eval", eval_path)
    run_eval = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_eval)

    import uuid as _uuid

    items = [
        LedgerContextItem(
            ref="C1", commitment_id=_uuid.uuid4(), vendor="Ah Seng Production",
            deliverable_en="screen install", due_at="2026-06-22T16:00:00+08:00",
            amount=None, state="proposed",
        ),
        LedgerContextItem(
            ref="C2", commitment_id=_uuid.uuid4(), vendor="Bloomworks",
            deliverable_en="main stage flowers", due_at=None,
            amount="450.00 SGD", state="committed",
        ),
    ]
    case = {
        "ledger_context": [
            {"vendor": "Ah Seng Production", "deliverable_en": "screen install",
             "due_at": "2026-06-22T16:00:00+08:00", "amount": None, "state": "proposed"},
            {"vendor": "Bloomworks", "deliverable_en": "main stage flowers",
             "due_at": None, "amount": "450.00 SGD", "state": "committed"},
        ]
    }

    assert render_ledger_context(items) == run_eval.render_ledger_context(case)
    assert render_ledger_context([]) == run_eval.render_ledger_context({})


# --- relevance selection: being old is not the same as being invisible -----


@pytest.mark.asyncio
async def test_aged_out_commitment_is_recalled_when_the_message_names_it(
    app_session, org_and_project
):
    """The CD01/CD02 cliff. Both are internal staff reacting to something
    already logged; the only difference between "links correctly 5/5" and
    "invents a commitment 5/5" is whether that commitment was in this list.
    A recency-only window turns the first into the second the moment a
    project outgrows twelve live commitments, which is every real event.
    """
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)

    # The one the message is about, oldest of all — well outside a 12-row
    # recency window.
    target = await _make_commitment(
        app_session, org_id, project_id, name="aluminium frame delivery",
        vendor="Vertex Fabrication", created_at=base,
    )
    for i in range(20):
        await _make_commitment(
            app_session, org_id, project_id, name=f"unrelated item {i}",
            vendor="Bloomworks", created_at=base + timedelta(days=i + 1),
        )

    recency_only = await load_open_commitment_context(app_session, project_id=project_id)
    assert target.id not in [i.commitment_id for i in recency_only]

    with_message = await load_open_commitment_context(
        app_session, project_id=project_id,
        message="any update on Vertex's aluminium frame delivery?",
    )
    assert target.id in [i.commitment_id for i in with_message]
    assert len(with_message) == 12  # window size is unchanged


@pytest.mark.asyncio
async def test_a_message_matching_nothing_leaves_the_recency_window_untouched(
    app_session, org_and_project
):
    """Relevance reserves slots, it does not replace the window. On a message
    that matches nothing the result has to be byte-identical to the old
    behaviour, so this can only ever add a row the old window missed."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for i in range(20):
        await _make_commitment(
            app_session, org_id, project_id, name=f"item {i}",
            vendor="Bloomworks", created_at=base + timedelta(days=i),
        )

    recency_only = await load_open_commitment_context(app_session, project_id=project_id)
    unmatched = await load_open_commitment_context(
        app_session, project_id=project_id, message="thanks, noted, will revert",
    )
    assert [i.commitment_id for i in unmatched] == [i.commitment_id for i in recency_only]


@pytest.mark.asyncio
async def test_relevance_recall_works_on_chinese_with_no_whitespace(
    app_session, org_and_project
):
    """Half this corpus is 中文 or code-switched. Whitespace tokenisation alone
    would score a Chinese deliverable at zero against a Chinese message naming
    it, so the recall this whole change buys would not exist for those
    messages — the ones the ambiguous-date and code-switched bands already
    show as weakest."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)

    target = await _make_commitment(
        app_session, org_id, project_id, name="铝框交付",
        vendor="Vertex Fabrication", created_at=base,
    )
    for i in range(20):
        await _make_commitment(
            app_session, org_id, project_id, name=f"unrelated {i}",
            vendor="Bloomworks", created_at=base + timedelta(days=i + 1),
        )

    items = await load_open_commitment_context(
        app_session, project_id=project_id, message="铝框交付的时间确认了吗？",
    )
    assert target.id in [i.commitment_id for i in items]


@pytest.mark.asyncio
async def test_refs_stay_newest_first_and_stable_across_identical_calls(
    app_session, org_and_project
):
    """The refs are positional, so an unstable window is an unstable meaning
    for "C2" — and the model is being asked to echo one back."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for i in range(20):
        await _make_commitment(
            app_session, org_id, project_id, name=f"screen install {i}",
            vendor="Ah Seng Production", created_at=base + timedelta(days=i),
        )

    first = await load_open_commitment_context(
        app_session, project_id=project_id, message="screen install update?",
    )
    second = await load_open_commitment_context(
        app_session, project_id=project_id, message="screen install update?",
    )
    assert [i.ref for i in first] == [f"C{n}" for n in range(1, 13)]
    assert [i.commitment_id for i in first] == [i.commitment_id for i in second]
    # Newest-first is preserved within the selected set.
    names = [i.deliverable_en for i in first]
    assert names[0] == "screen install 19"


@pytest.mark.asyncio
async def test_relevance_never_crowds_out_a_pinned_commitment(
    app_session, org_and_project
):
    """The write-back pin is the strongest possible signal — that commitment
    was matched to this exact message before extraction ran — so it outranks
    anything the lexical pass has an opinion about."""
    org_id, project_id = org_and_project
    await set_org_context(app_session, org_id)
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)

    pinned = await _make_commitment(
        app_session, org_id, project_id, name="catering final headcount",
        vendor="Golden Palate", created_at=base,
    )
    for i in range(20):
        await _make_commitment(
            app_session, org_id, project_id, name=f"screen install {i}",
            vendor="Ah Seng Production", created_at=base + timedelta(days=i + 1),
        )

    items = await load_open_commitment_context(
        app_session, project_id=project_id,
        pinned_commitment_ids=(pinned.id,),
        message="screen install screen install screen install",
    )
    assert pinned.id in [i.commitment_id for i in items]


def test_relates_to_enum_carries_no_type_union_alongside_it():
    """Pins the shape that made the whole `relates_to` path fail in
    production while every local test passed.

    `{"type": ["string","null"], "enum": [null, "C1"]}` is legal JSON Schema
    and Ollama accepts it, so the local eval and this suite were both green.
    Anthropic's structured-outputs validator rejects it:

        Invalid schema: Enum value None does not match declared type
        '['string', 'null']'

    Production extraction is claude-haiku-4-5, so every extraction call
    carrying ledger context 400'd — the fix for the reported bug was dead
    against the only model that runs it. An enum fixes the value space by
    itself; the type annotation was redundant wherever it was accepted.
    """
    schema = build_extraction_json_schema(["C1", "C2"])
    relates_to = schema["properties"]["commitments"]["items"]["properties"]["relates_to"]

    assert relates_to["enum"] == [None, "C1", "C2"]
    assert "type" not in relates_to

    empty = build_extraction_json_schema([])
    empty_relates_to = empty["properties"]["commitments"]["items"]["properties"]["relates_to"]
    assert empty_relates_to["enum"] == [None]
    assert "type" not in empty_relates_to


def test_eval_harness_builds_the_same_relates_to_shape_as_production():
    """cue-eval/run_eval.py hand-duplicates the schema narrowing so it can run
    with no app imports. That duplication is why the production 400 was
    reproducible in the harness at all — and why the two have to be pinned
    together, or the harness stops being able to find this class of bug."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cue-eval"))
    import run_eval

    harness = run_eval.case_schema({"ledger_context": [{}, {}]})
    production = build_extraction_json_schema(["C1", "C2"])
    path = ("properties", "commitments", "items", "properties", "relates_to")

    def dig(schema):
        for key in path:
            schema = schema[key]
        return schema

    assert dig(harness) == dig(production)


# --- the prompt's UTC offset is the project's, not Singapore's -------------


def test_rendered_prompt_is_byte_identical_to_the_old_hardcoded_singapore_one():
    """The prompt used to instruct "Resolve every time to ISO 8601 with the
    +08:00 offset" as a literal, four lines under an interpolated
    `Timezone: {timezone}` it ignored. Correct for Pico, silently eight hours
    wrong for anyone else.

    Interpolating the project's real offset is only safe to ship without a
    fresh --runs 5 if it provably changes nothing for the existing corpus —
    every cue-eval case is Asia/Singapore, which renders "+08:00", so the
    rendered prompt must come out byte-for-byte identical. That is what this
    asserts: not "the offset is right" but "no existing measurement moved".
    """
    from app.ledger.extractor import build_prompt

    context = {
        "project": "P", "client": "C", "timezone": "Asia/Singapore", "venue": "V",
        "build_up": ["2026-06-22"], "event_days": ["2026-06-24"],
        "doors": "2026-06-24T09:00:00+08:00",
        "known_milestones": [{"name": "M", "due": "2026-06-20"}], "vendors": [],
    }
    case = {
        "id": "TX", "band": "test", "lang": "en", "channel": "whatsapp",
        "party": "V", "sent_at": "2026-06-22T09:00:00+08:00",
        "message": "screen install tomorrow", "sent_weekday": None,
    }

    rendered = build_prompt(context, case)
    assert "with the +08:00 offset" in rendered
    # And the literal template placeholder is gone — a missed substitution
    # would leave the model reading "{utc_offset}" as instruction text.
    assert "{utc_offset}" not in rendered


def test_a_non_singapore_project_gets_its_own_offset_with_dst_resolved():
    """The bug this fixes, and the reason the offset is computed rather than
    asked for: a fixed string cannot express that London is +01:00 in June and
    +00:00 in December. The message's own sent time decides which."""
    from app.ledger.extractor import _utc_offset_label

    london = {"timezone": "Europe/London"}
    assert _utc_offset_label(london, {"sent_at": "2026-06-20T12:00:00+00:00"}) == "+01:00"
    assert _utc_offset_label(london, {"sent_at": "2026-12-20T12:00:00+00:00"}) == "+00:00"
    assert _utc_offset_label({"timezone": "America/New_York"},
                             {"sent_at": "2026-06-20T12:00:00+00:00"}) == "-04:00"
    # An unusable zone falls back rather than taking extraction down.
    assert _utc_offset_label({"timezone": "Not/AZone"},
                             {"sent_at": "2026-06-20T12:00:00+00:00"}) == "+08:00"


def test_eval_harness_computes_the_same_offset_as_production():
    """run_eval.py hand-duplicates the offset calculation so it can run with
    no app imports. Same pinning as render_ledger_context and the relates_to
    schema: the duplicate is allowed, drifting from production is not."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cue-eval"))
    import run_eval

    from app.ledger.extractor import _utc_offset_label

    for tz in ("Asia/Singapore", "Europe/London", "America/New_York", "Not/AZone"):
        for sent_at in ("2026-06-20T12:00:00+00:00", "2026-12-20T12:00:00+00:00"):
            ctx, case = {"timezone": tz}, {"sent_at": sent_at}
            assert run_eval.utc_offset_label(ctx, case) == _utc_offset_label(ctx, case), (tz, sent_at)
